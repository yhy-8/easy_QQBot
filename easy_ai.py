import aiohttp
import datetime
import aiosqlite
import re
import asyncio
import base64
from pathlib import Path
from nonebot import on_message, get_driver
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment, GroupMessageEvent, Message
from nonebot.exception import FinishedException

# ================= 配置区域 =================
ALLOWED_GROUPS = [12345678] #白名单群
DB_PATH = "/qqbot/chat_history.db"  # SQLite 数据库文件路径

# 图片本地缓存目录配置
# 1. 如果代码和 NapCat 在同一台电脑/同一个 Docker 容器内，请保持留空 ""，程序会自动读取绝对路径。
# 2. 如果是跨 Docker 容器部署，导致路径不通，请在此填入挂载到当前容器的绝对路径（例如 "/napcat/xxx/images"）
IMAGE_BASE_DIR = ""

MODELS_CONFIG = {
    "default": {
        "api_key": "",
        "api_url": "https://api.deepseek.com/chat/completions",
        "name": "ds-chat",
        "api_type": "openai",
        "model_id": "deepseek-chat",  # DeepSeek 需要在 body 传入这个
        "vision": False
    },
    "A": {
        "api_key": "",
        "api_url": "https://api.deepseek.com/chat/completions", # 注意: reasoner 也是这个端点
        "name": "ds-reasoner",
        "api_type": "openai",
        "model_id": "deepseek-reasoner",
        "vision": False
    },
    "B": {
        "api_key": "",
        "api_url": "",
        "name": "gemini-3-flash",
        "api_type": "gemini",
        "vision": True
    },
    "C": {
        "api_key": "",
        "api_url": "",
        "name": "gemini-3.1-pro",
        "api_type": "gemini",
        "vision": True
    }
}


# ========== 数据库初始化 ==========
driver = get_driver()
@driver.on_startup
async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        # 开启 WAL 模式
        await db.execute('PRAGMA journal_mode=WAL;')

        # 创建全局昵称记录表
        await db.execute('''
            CREATE TABLE IF NOT EXISTS "user_info" (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                last_speak_time INTEGER
            )
        ''')

        for group_id in ALLOWED_GROUPS:
            table_name = f"group_{group_id}"
            # 创建聊天记录表
            await db.execute(f'''
                CREATE TABLE IF NOT EXISTS "{table_name}" (
                    message_id TEXT UNIQUE,
                    timestamp INTEGER,
                    sender_name TEXT,
                    user_id TEXT,
                    content TEXT
                )
            ''')

        await db.commit()
    print("[AI Chat] 数据库初始化完成")


# ========== 辅助函数：动态获取聊天记录数 ==========
async def get_dynamic_history_length(group_id: int) -> int:
    """统计近期消息密度并让大模型决定要读取的历史消息数量"""
    default=80 #默认值

    table_name = f"group_{group_id}"
    now_ts = int(datetime.datetime.now().timestamp())
    rows = []
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
            async with db.execute(f'SELECT timestamp FROM "{table_name}" WHERE timestamp > ? ORDER BY timestamp DESC, rowid DESC',
                                  (now_ts - 7200,)) as cursor:
                rows = await cursor.fetchall()
    except Exception as e:  # 捕获 aiosqlite 的异常
        print(f"[AI Chat]数据库查询异常 {e}")
        rows = []

    # 如果两小时内没有任何消息，直接返回兜底值，不浪费 API Token
    if not rows:
        return default

    # 统计各个时间段的消息量
    stats = {
        "最近10分钟": 0,
        "10-30分钟前": 0,
        "30-60分钟前": 0,
        "1-2小时前": 0
    }

    for (ts,) in rows:
        diff = now_ts - ts
        if diff <= 600:
            stats["最近10分钟"] += 1
        elif diff <= 1800:
            stats["10-30分钟前"] += 1
        elif diff <= 3600:
            stats["30-60分钟前"] += 1
        else:
            stats["1-2小时前"] += 1

    stats_text = ", ".join([f"{k}: {v}条" for k, v in stats.items()])

    # 构建给默认模型的 Prompt
    prompt = (
        f"你是一个用于判断群聊上下文长度的控制程序。以下是当前群聊最近2小时的活跃度统计：\n"
        f"[{stats_text}]\n"
        f"这是两个和一个ai组成的群，你需要决定接下来我需要提取多少条历史记录作为上下文给大模型。"
        f"要求短时间内信息多的话尽可能包括半小时到一小时的数据;相反可以小一些，控制在100左右；数字最大可以到1000甚至更多。"
        f"请只回复一个纯数字，不要包含任何其他字符！"
    )

    # 调用 default 模型 (按你的配置是 DeepSeek)
    default_config = MODELS_CONFIG["default"]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {default_config['api_key']}"
    }
    payload = {
        "model": default_config.get("model_id", "deepseek-chat"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0.1  # 降低温度，让输出更稳定
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(default_config["api_url"], headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reply = data["choices"][0]["message"]["content"].strip()
                    match = re.search(r'\d+', reply)
                    if match:
                        num = int(match.group())
                        # 兜底限制
                        return max(30, min(500, num))
    except Exception as e:
        print(f"[AI Chat] 获取动态上下文长度失败: {e}")

    # 如果请求失败或没有拿到数字，返回一个默认值
    return default


# ========== 辅助函数：解析消息为纯文本/占位符 ==========
async def parse_message_content(bot: Bot, group_id: int, raw_message) -> str:
    if isinstance(raw_message, str):
        clean_text = re.sub(r'\[CQ:[^\]]+\]', '[媒体/表情]', raw_message)
        return clean_text.strip()

    text_parts = []
    # 2. 统一处理：只要是可迭代对象 (包括 list 和 Message)，就直接遍历
    if hasattr(raw_message, "__iter__"):
        for seg in raw_message:
            # 核心修复：安全提取 type 和 data，兼容 dict 和 MessageSegment 对象
            if isinstance(seg, dict):
                seg_type = seg.get("type", "")
                seg_data = seg.get("data", {})
            else:
                seg_type = getattr(seg, "type", "")
                seg_data = getattr(seg, "data", {})

            # 过滤掉无法解析的脏数据
            if not seg_type:
                continue

            # 组装纯文本
            if seg_type == "text":
                text_parts.append(seg_data.get("text", ""))
            elif seg_type == "reply":
                reply_id = seg_data.get("id")
                try:
                    # 向框架请求原消息，获取真实时间和发送者
                    reply_msg = await bot.get_msg(message_id=reply_id)
                    r_time = reply_msg.get("time")
                    r_sender = reply_msg.get("sender", {}).get("nickname", "未知")
                    text_parts.append(f"[引用回复(时间：{r_time}，发言人：{r_sender})]")
                except Exception:
                    text_parts.append("[引用回复(获取信息失败)]")
            elif seg_type == "image":
                # 尝试获取 summary，如果没有则默认空字符串
                summary = seg_data.get("summary", "").strip()
                # 如果 summary 真的有内容（比如 "[动画表情]"），就用它；否则用 "[图片]"
                text_parts.append(summary if summary else "[图片]")
            elif seg_type in ["face", "mface", "bface"]:
                summary = seg_data.get("summary", "").strip()
                text_parts.append(summary if summary else "[表情包]")
            elif seg_type == "record":
                text_parts.append("[语音]")
            elif seg_type == "video":
                text_parts.append("[视频]")
            elif seg_type == "file":
                file_name = seg_data.get("name") or seg_data.get("file") or seg_data.get("id") or "未知文件"
                text_parts.append(f"[文件: {file_name}]")
            elif seg_type == "forward":
                text_parts.append("[聊天记录]")
            elif seg_type == "node":
                text_parts.append("[合并转发节点]")
            elif seg_type in ["json", "xml"]:
                text_parts.append("[分享了卡片/链接]")
            elif seg_type == "at":
                qq_id = seg_data.get('qq', '某人')
                if qq_id == "all":
                    text_parts.append("[@全体成员]")
                elif str(qq_id).isdigit():
                    try:
                        # 主动向框架请求被艾特人的群信息
                        member_info = await bot.get_group_member_info(group_id=group_id, user_id=int(qq_id),
                                                                      no_cache=False)
                        # 只取 nickname，忽略 card。如果都拿不到，退回到 qq_id
                        name = member_info.get("nickname") or qq_id
                        text_parts.append(f"[@{name}]")
                    except Exception:
                        # 如果获取失败（如退群、网络错误），兜底使用 QQ 号
                        text_parts.append(f"[@{qq_id}]")
                else:
                    text_parts.append(f"[@{qq_id}]")
            else:
                text_parts.append(f"[{seg_type}]")

    return "".join(text_parts).strip()


# ========== 辅助函数：统一发送并存入数据库 ==========
async def send_and_save(bot: Bot, event: GroupMessageEvent, matcher, msg, is_finish: bool = False):
    content_to_save = await parse_message_content(bot, event.group_id, msg)

    try:
        send_result = await matcher.send(msg)

        if isinstance(send_result, dict) and "message_id" in send_result:
            bot_msg_id = send_result["message_id"]
            bot_timestamp = int(datetime.datetime.now().timestamp())

            # 动态获取机器人的 QQ 昵称和 QQ 号
            try:
                bot_info = await bot.get_login_info()
                bot_name = bot_info.get("nickname", "AI助手")
                bot_user_id = str(bot_info.get("user_id", bot.self_id))
            except Exception:
                bot_name = "AI助手"
                bot_user_id = str(bot.self_id)

            # 增加 bot_user_id 传入
            await insert_message_to_db(bot_msg_id, event.group_id, bot_timestamp, bot_name, bot_user_id, content_to_save)
    except Exception as e:
        print(f"[AI Chat] 消息发送或存库失败: {e}")

    if is_finish:
        await matcher.finish()


# ========== 辅助函数：异步写入数据库 ==========
async def insert_message_to_db(msg_id, group_id, timestamp, sender_name, user_id, content):
    if not content or group_id not in ALLOWED_GROUPS:
        return

    table_name = f"group_{group_id}"
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
            # 1. 写入聊天记录表
            sql_chat = f'INSERT OR IGNORE INTO "{table_name}" (message_id, timestamp, sender_name, user_id, content) VALUES (?, ?, ?, ?, ?)'
            await db.execute(sql_chat, (str(msg_id), int(timestamp), sender_name, str(user_id), content))

            # 2. 写入或更新昵称表
            sql_user = '''
                INSERT INTO "user_info" (user_id, nickname, last_speak_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname=excluded.nickname,
                    last_speak_time=excluded.last_speak_time
            '''
            await db.execute(sql_user, (str(user_id), sender_name, int(timestamp)))

            await db.commit()
    except Exception as e:
        print(f"[AI Chat] 数据库错误，异步写入失败: {e}")


# ========== 辅助函数：通过 file_id 获取本地图片并转换为 Base64 ==========
async def get_local_image_as_base64(bot: Bot, file_id: str, max_retries: int = 5, wait_time: float = 1.0) -> str:
    if not file_id: return None
    try:
        # 1. 调用 OneBot 标准接口获取图片信息
        img_info = await bot.get_image(file=file_id)
        file_path_str = img_info.get("file", "")

        if not file_path_str:
            return None

        # 2. 路径策略判断：自动 vs 手动覆盖
        raw_path = Path(file_path_str)
        if IMAGE_BASE_DIR:
            # 【手动模式】遇到了 Docker 隔离，直接提取图片文件名(raw_path.name)，拼接到配置的映射目录下
            file_path = Path(IMAGE_BASE_DIR) / raw_path.name
        else:
            # 【自动模式】留空则完全信任 NapCat 返回的底层绝对路径
            file_path = raw_path

        # 3. 轮询等待文件落地，确保文件大小大于 0 字节
        for attempt in range(max_retries):
            if file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0:
                break
            await asyncio.sleep(wait_time)
        else:
            print(f"[AI Chat] 等待本地图片落地超时，预期路径: {file_path}")
            return None

        # 4. 使用线程池读取文件
        loop = asyncio.get_event_loop()
        def read_file():
            return base64.b64encode(file_path.read_bytes()).decode('utf-8')

        return await loop.run_in_executor(None, read_file)

    except Exception as e:
        print(f"[AI Chat] 读取本地图片转Base64失败: {e}")
    return None


# ========== 辅助函数：专供AI理解的富文本与图片提取(分离下载) ==========
async def extract_text_and_image_ids(bot: Bot, group_id: int, raw_message) -> tuple[str, list]:
    """返回：(富文本字符串, 图片file_id列表)"""
    text_parts = []
    image_ids = []

    if hasattr(raw_message, "__iter__"):
        for seg in raw_message:
            seg_type = seg.get("type", "") if isinstance(seg, dict) else getattr(seg, "type", "")
            seg_data = seg.get("data", {}) if isinstance(seg, dict) else getattr(seg, "data", {})

            if not seg_type: continue

            if seg_type == "text":
                text_parts.append(seg_data.get("text", ""))
            elif seg_type == "at":
                qq_id = str(seg_data.get('qq', ''))
                # 排除 @ 机器人自己
                if qq_id != str(bot.self_id):
                    if qq_id == "all":
                        text_parts.append("[@全体成员]")
                    elif qq_id.isdigit():
                        try:
                            # 像前面一样，主动向框架请求被艾特人的群昵称
                            member_info = await bot.get_group_member_info(group_id=group_id, user_id=int(qq_id),
                                                                          no_cache=False)
                            name = member_info.get("nickname") or qq_id
                            text_parts.append(f"[@{name}]")
                        except Exception:
                            # 获取失败兜底用 QQ 号
                            text_parts.append(f"[@{qq_id}]")
                    else:
                        text_parts.append(f"[@{qq_id}]")
            elif seg_type == "image":
                # 过滤主消息体中的表情包图片
                summary = seg_data.get("summary", "").strip()
                if summary:
                    # 如果有 summary（如 [动画表情]），则视为表情，不提取 file_id
                    text_parts.append(summary)
                else:
                    text_parts.append("[图片]")
                    if "file" in seg_data:
                        image_ids.append(seg_data["file"])
            elif seg_type in ["face", "mface", "bface"]:
                summary = seg_data.get("summary", "").strip()
                text_parts.append(summary if summary else "[表情包]")
            elif seg_type == "reply":
                reply_id = seg_data.get("id")
                try:
                    reply_msg = await bot.get_msg(message_id=reply_id)
                    r_time_str = datetime.datetime.fromtimestamp(reply_msg.get("time", 0)).strftime("%m-%d %H:%M:%S")
                    r_sender = reply_msg.get("sender", {}).get("nickname", "未知")

                    r_text_content = ""
                    for r_seg in reply_msg.get("message", []):
                        r_type = r_seg.get("type", "")
                        r_data = r_seg.get("data", {})
                        if r_type == "text":
                            r_text_content += r_data.get("text", "")
                        elif r_type == "image":
                            r_summary = r_data.get("summary", "").strip()
                            if r_summary:
                                r_text_content += r_summary
                            else:
                                r_text_content += "[图片]"
                                if "file" in r_data:
                                    image_ids.append(r_data["file"])
                        elif r_type in ["face", "mface", "bface"]:
                            r_summary = r_data.get("summary", "").strip()
                            r_text_content += (r_summary if r_summary else "[表情包]")
                        elif r_type == "file":
                            r_text_content += f"[文件：{r_data.get('name', '未知')}]"
                        elif r_type == "record":
                            r_text_content += "[语音]"
                        elif r_type == "video":
                            r_text_content += "[视频]"
                        elif r_type == "forward":
                            r_text_content += "[聊天记录]"
                        elif r_type == "node":
                            r_text_content += "[合并转发节点]"
                        elif r_type in ["json", "xml"]:
                            r_text_content += "[分享了卡片/链接]"
                        else:
                            r_text_content += f"[{r_type}]"

                    text_parts.append(f"\n[引用回复（时间：{r_time_str}，发言人：{r_sender}，内容：{r_text_content}）]\n")
                except Exception as e:
                    text_parts.append("[引用回复(获取信息失败)]")
            elif seg_type == "file":
                text_parts.append(f"[文件: {seg_data.get('name') or seg_data.get('file') or '未知文件'}]")
            elif seg_type == "record":
                text_parts.append("[语音]")
            elif seg_type == "video":
                text_parts.append("[视频]")
            elif seg_type == "forward":
                text_parts.append("[聊天记录]")
            elif seg_type == "node":
                text_parts.append("[合并转发节点]")
            elif seg_type in ["json", "xml"]:
                text_parts.append("[分享了卡片/链接]")
            else:
                text_parts.append(f"[{seg_type}]")

    return "".join(text_parts).strip(), image_ids


# ========== 1. 机器人启动时自动拉取同步历史记录 ==========
driver = get_driver()
@driver.on_bot_connect
async def sync_history_on_startup(bot: Bot):
    for group_id in ALLOWED_GROUPS:
        try:
            res = await bot.get_group_msg_history(group_id=group_id)
            messages = res.get("messages", []) if isinstance(res, dict) else res

            success_count = 0
            for msg in messages:
                try:
                    msg_id = msg.get("message_id")
                    timestamp = msg.get("time", 0)

                    # 提取 sender 信息
                    sender = msg.get("sender", {})
                    sender_name = sender.get("nickname", "未知")
                    user_id = str(sender.get("user_id", "未知"))

                    content = await parse_message_content(bot, group_id, msg.get("message", ""))

                    if msg_id and content:
                        await insert_message_to_db(msg_id, group_id, timestamp, sender_name, user_id, content)
                        success_count += 1
                except Exception as inner_e:
                    print(f"[AI Chat] 解析单条历史消息失败: {inner_e}")
                    continue

            print(f"[AI Chat] 群 {group_id} 启动历史同步完成，成功处理 {success_count} 条记录。")
        except Exception as e:
            print(f"[AI Chat] 群 {group_id} 抓取历史记录接口请求失败: {e}")


# ========== 2. 实时被动记录白名单群聊 ==========
record_handler = on_message(priority=1, block=False)
@record_handler.handle()
async def record_chat_history(bot: Bot, event: Event):
    if not isinstance(event, GroupMessageEvent):
        return
    if event.group_id not in ALLOWED_GROUPS:
        return
    # 如果消息是 @机器人的，跳过被动记录，避免与 chat_handler 竞争写入
    if event.is_tome():
        return

    sender_name = event.sender.nickname if event.sender and event.sender.nickname else str(event.user_id)
    user_id = str(event.user_id)
    content = await parse_message_content(bot, event.group_id, event.original_message)

    await insert_message_to_db(event.message_id, event.group_id, event.time, sender_name, user_id, content)


# ========== 3. 处理用户的 @ 提问 ==========
chat_handler = on_message(rule=to_me(), priority=50, block=True)

@chat_handler.handle()
async def handle_ai_chat(bot: Bot, event: Event):
    if not isinstance(event, GroupMessageEvent):
        await chat_handler.finish("抱歉，当前功能仅限群聊使用哦")
        return

    if event.group_id not in ALLOWED_GROUPS:
        return

    # 抢在机器人回复前，强制先把用户的触发消息存库
    sender_name = event.sender.nickname if event.sender and event.sender.nickname else str(event.user_id)
    user_msg_content = await parse_message_content(bot, event.group_id, event.original_message)
    await insert_message_to_db(event.message_id, event.group_id, event.time, sender_name, str(event.user_id),user_msg_content)

    # 如果只是引用而没有手动 @，则在此中断，不触发 AI 回复
    has_at = any(seg.type == "at" and str(seg.data.get("qq")) == str(bot.self_id)
                 for seg in event.original_message)
    if not has_at:
        await chat_handler.finish()

    # 提取纯文本以便先判断触发了哪个模型
    plain_text = event.get_plaintext().strip()
    selected_model_key = "default"
    prefix_to_remove = ""
    for key in MODELS_CONFIG.keys():
        if key == "default": continue
        prefix = f"/{key}"
        if plain_text.startswith(prefix):
            selected_model_key = key
            prefix_to_remove = prefix
            break

    model_config = MODELS_CONFIG.get(selected_model_key, MODELS_CONFIG["default"])
    current_api_key = model_config["api_key"]
    current_api_url = model_config["api_url"]
    is_vision_enabled = model_config.get("vision", False)

    # 1. 提取富文本内容与图片 ID
    rich_user_input, image_ids = await extract_text_and_image_ids(bot, event.group_id, event.original_message)

    if prefix_to_remove:
        rich_user_input = rich_user_input.replace(prefix_to_remove, "", 1).strip()
    user_input = rich_user_input.strip()

    # 2. 校验 1：啥都没有输入也没有图片
    if not user_input and not image_ids:
        await send_and_save(bot, event, chat_handler,MessageSegment.at(event.user_id) + f"（模型：{model_config['name']}） 何意味", is_finish=True)
        return

    # 3. 校验 2：带了图片但当前模型不支持 Vision
    if image_ids and not is_vision_enabled:
        err_msg = MessageSegment.at(event.user_id) + f"（模型：{model_config['name']}）该模型不具备图片识别能力！"
        await send_and_save(bot, event, chat_handler, err_msg, is_finish=True)
        return

    # 4. 通过校验，立刻返回等待提示
    ack_msg = MessageSegment.at(event.user_id) + MessageSegment.text(
        f"（模型：{model_config['name']}，IMG：{'True' if image_ids else 'False'}）等待API回复……"
    )
    await send_and_save(bot, event, chat_handler, ack_msg, is_finish=False)

    # 5. 提示已发出，开始读取本地图片转 Base64
    base64_images = []
    if image_ids:
        for file_id in image_ids:
            b64 = await get_local_image_as_base64(bot, file_id)
            if b64:
                base64_images.append(b64)

    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = f"{event.sender.nickname}({event.user_id})" if event.sender and event.sender.nickname else str(event.user_id)

    # 从数据库获取上下文历史
    dynamic_limit = await get_dynamic_history_length(event.group_id)
    table_name = f"group_{event.group_id}"
    rows = []
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
            query = f'SELECT timestamp, sender_name, content FROM "{table_name}" WHERE message_id != ? ORDER BY timestamp DESC, rowid DESC LIMIT ?'
            async with db.execute(query, (str(event.message_id), dynamic_limit)) as cursor:
                rows = await cursor.fetchall()
    except Exception as e:
        print(f"[AI Chat]数据库提取异常： {e}")
        rows = []
    rows.reverse()

    def convert_reply_time(match):
        try:
            ts = int(match.group(1))
            dt_str = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
            return f"[引用回复(时间：{dt_str}，发言人：{match.group(2)})]"
        except ValueError:
            return match.group(0)

    history_lines = []
    for row in rows:
        msg_time = datetime.datetime.fromtimestamp(row[0]).strftime("%m-%d %H:%M")
        nname = row[1]
        text_content = row[2]
        text_content = re.sub(r'\[引用回复\(时间：(\d+)，发言人：(.*?)\)\]', convert_reply_time, text_content)
        history_lines.append(f"[{msg_time}] {nname}: {text_content}")

    history_text = "\n".join(history_lines)

    if history_text.strip():
        final_prompt = (
            f"你是群里的一位客观的助手，请根据下面提供的近期群聊上下文，作出答复。要求：不要说出用户的id和名称，不要使用markdown，使用纯文本输出。\n"
            f"--- 真实群聊历史记录 ---\n"
            f"{history_text}\n"
            f"------------------------\n\n"
            f"现在是 {current_time}，用户 {user_name} 正在向你提问：\n"
            f"{user_input}\n"
        )
    else:
        final_prompt = (
            f"你是群里的一位客观的助手，请作出答复。要求：不要说出用户的id和名称，不要使用markdown，使用纯文本输出。\n"
            f"现在是 {current_time}，用户 {user_name} 正在向你提问：\n"
            f"{user_input}\n"
        )

    # 如果有图片，打上“当前附件”的强力思想钢印
    if is_vision_enabled and base64_images:
        final_prompt += "\n[系统重要提示：用户本次提问附带了视觉图片。请结合你的视觉能力回答上述问题。请明确：这些图片是该用户当下的提问附件，绝不是历史聊天记录中的杂图！]"

    api_type = model_config.get("api_type", "gemini")

    # Payload 组装

    if api_type == "openai":
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }

        # 组装 openai 兼容的内容数组
        user_message_content = []
        if is_vision_enabled and base64_images:
            user_message_content.append({"type": "text", "text": final_prompt})
            for b64 in base64_images:
                user_message_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                })
        else:
            user_message_content = final_prompt

        payload = {
            "model": model_config.get("model_id", "deepseek-chat"),
            "messages": [
                {"role": "user", "content": user_message_content}
            ],
            "stream": False
        }

    else:
        # Gemini 格式
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": current_api_key
        }

        parts = [{"text": final_prompt}]
        if is_vision_enabled and base64_images:
            for b64 in base64_images:
                parts.append({
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": b64
                    }
                })

        payload = {
            "contents": [{
                "role": "user",
                "parts": parts
            }]
        }

    # 发送请求并根据格式解析返回结果
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(current_api_url, headers=headers, json=payload, timeout=300) as resp:
                if resp.status != 200:
                    err_msg = await resp.text()
                    err_msg_text = MessageSegment.at(event.user_id) + f"\n（模型：{model_config['name']}）请求失败，状态码: {resp.status} \n错误信息: {err_msg}"
                    await send_and_save(bot, event, chat_handler, err_msg_text, is_finish=True)
                    return

                data = await resp.json()
                # 动态解析返回值
                if api_type == "openai":
                    # Openai 格式解析
                    reply_text = data["choices"][0]["message"]["content"].strip()
                else:
                    # Gemini 格式解析
                    reply_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        prefix_hint = f"模型：{model_config['name']}，浏览记录条数：{dynamic_limit}，浏览图片数：{len(base64_images)}\n"
        msg = MessageSegment.at(event.user_id) + "\n" + MessageSegment.text(f"{prefix_hint}{reply_text}")
        await send_and_save(bot, event, chat_handler, msg, is_finish=True)

    except asyncio.TimeoutError:
        await send_and_save(bot, event, chat_handler, MessageSegment.at(event.user_id) + f"\n（模型：{model_config['name']}）请求超时，请稍后再试", is_finish=True)
    except FinishedException:
        raise
    except Exception as e:
        await send_and_save(bot, event, chat_handler, MessageSegment.at(event.user_id) + f"\n（模型：{model_config['name']}）调用出错 \n错误信息：{e}", is_finish=True)
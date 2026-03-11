import aiohttp
import datetime
import aiosqlite
import re
import asyncio
from nonebot import on_message, get_driver
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment, GroupMessageEvent, Message
from nonebot.exception import FinishedException

# ================= 配置区域 =================
ALLOWED_GROUPS = [12345678] #白名单群
DB_PATH = "/qqbot/chat_history.db"  # SQLite 数据库文件路径

MODELS_CONFIG = {
    "default": {
        "api_key": "",
        "api_url": "https://api.deepseek.com/chat/completions",
        "name": "ds-chat",
        "api_type": "openai",
        "model_id": "deepseek-chat"  # DeepSeek 需要在 body 传入这个
    },
    "A": {
        "api_key": "",
        "api_url": "https://api.deepseek.com/chat/completions", # 注意: reasoner 也是这个端点
        "name": "ds-reasoner",
        "api_type": "openai",
        "model_id": "deepseek-reasoner"
    },
    "B": {
        "api_key": "",
        "api_url": "",
        "name": "gemini-3-flash",
        "api_type": "gemini"
    },
    "C": {
        "api_key": "",
        "api_url": "",
        "name": "gemini-3.1-pro",
        "api_type": "gemini"
    }
}


# ========== 数据库初始化 ==========
driver = get_driver()
@driver.on_startup
async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        # 开启 WAL 模式
        await db.execute('PRAGMA journal_mode=WAL;')

        for group_id in ALLOWED_GROUPS:
            table_name = f"group_{group_id}"
            await db.execute(f'''
                CREATE TABLE IF NOT EXISTS "{table_name}" (
                    message_id TEXT UNIQUE,
                    timestamp INTEGER,
                    sender_name TEXT,
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
            async with db.execute(f'SELECT timestamp FROM "{table_name}" WHERE timestamp > ? ORDER BY timestamp DESC',
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
        f"要求短时间内信息多的话尽可能包括半小时到一小时的数据;相反可以小一些，控制在150左右；数字最大可以到1000甚至更多。"
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
def parse_message_content(raw_message) -> str:
    # 1. 处理纯字符串 (如旧版 CQ 码)
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
            elif seg_type == "image":
                text_parts.append(seg_data.get("summary", "[图片]"))
            elif seg_type in ["face", "mface", "bface"]:
                text_parts.append(seg_data.get("summary", "[表情包]"))
            elif seg_type == "record":
                text_parts.append("[语音]")
            elif seg_type == "video":
                text_parts.append("[视频]")
            elif seg_type in ["json", "xml"]:
                text_parts.append("[分享了卡片/链接]")
            elif seg_type == "at":
                text_parts.append(f"[@{seg_data.get('qq', '某人')}]")

    return "".join(text_parts).strip()


# ========== 辅助函数：异步写入数据库 ==========
async def insert_message_to_db(msg_id, group_id, timestamp, sender_name, content):
    if not content or group_id not in ALLOWED_GROUPS:
        return

    table_name = f"group_{group_id}"
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
            sql = f'INSERT OR IGNORE INTO "{table_name}" (message_id, timestamp, sender_name, content) VALUES (?, ?, ?, ?)'
            await db.execute(sql, (str(msg_id), int(timestamp), sender_name, content))
            await db.commit()
    except Exception as e:
        print(f"[AI Chat] 数据库错误，异步写入表 {table_name} 失败: {e}")


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
                # 给单条消息加 try，哪怕一条解析烂了，下一条还能接着跑
                try:
                    msg_id = msg.get("message_id")
                    timestamp = msg.get("time", 0)
                    sender_name = msg.get("sender", {}).get("nickname", "未知")
                    content = parse_message_content(msg.get("message", ""))

                    if msg_id and content:
                        await insert_message_to_db(msg_id, group_id, timestamp, sender_name, content)
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
async def record_chat_history(event: Event):
    if not isinstance(event, GroupMessageEvent):
        return
    if event.group_id not in ALLOWED_GROUPS:
        return

    sender_name = event.sender.nickname if event.sender and event.sender.nickname else str(event.user_id)
    content = parse_message_content(event.message)

    await insert_message_to_db(event.message_id, event.group_id, event.time, sender_name, content)


# ========== 3. 处理用户的 @ 提问 ==========
chat_handler = on_message(rule=to_me(), priority=50, block=True)

@chat_handler.handle()
async def handle_ai_chat(bot: Bot, event: Event):
    if not isinstance(event, GroupMessageEvent):
        await chat_handler.finish("抱歉，当前功能仅限群聊使用哦")
        return

    if event.group_id not in ALLOWED_GROUPS:
        return

    user_input = event.get_plaintext().strip()
    if not user_input:
        await chat_handler.finish(MessageSegment.at(event.user_id)+" 何意味")
        return

    selected_model_key = "default"
    for prefix in ["/A", "/B", "/C"]:
        if user_input.startswith(prefix):
            selected_model_key = prefix[1:]
            user_input = user_input[len(prefix):].strip()
            break

    model_config = MODELS_CONFIG.get(selected_model_key, MODELS_CONFIG["default"])
    current_api_key = model_config["api_key"]
    current_api_url = model_config["api_url"]

    if not user_input:
        await chat_handler.finish(MessageSegment.at(event.user_id)+f" （模型：{model_config['name']}）你没有输入要问的问题哦！")

    # 快速回复一条
    ack_msg = MessageSegment.at(event.user_id) + MessageSegment.text(
        f"（模型：{model_config['name']}）等待API回复……"
    )
    await chat_handler.send(ack_msg)

    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = f"{event.sender.nickname}({event.user_id})" if event.sender and event.sender.nickname else str(
        event.user_id)

    # 动态获取要查询的记录条数
    dynamic_limit = await get_dynamic_history_length(event.group_id)
    # 从 SQLite 数据库读取历史记录
    table_name = f"group_{event.group_id}"
    rows = []
    try:
        async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
            query = f'SELECT timestamp, sender_name, content FROM "{table_name}" WHERE message_id != ? ORDER BY timestamp DESC LIMIT ?'
            async with db.execute(query, (str(event.message_id), dynamic_limit)) as cursor:
                rows = await cursor.fetchall()
    except Exception as e:
        print(f"[AI Chat]数据库提取异常： {e}")
        rows = []
    # 数据库取出来是倒序的（最新的在前面），需要翻转成正序以便大模型阅读
    rows.reverse()

    history_lines = []
    for row in rows:
        msg_time = datetime.datetime.fromtimestamp(row[0]).strftime("%m-%d %H:%M")
        nname = row[1]
        text_content = row[2]
        history_lines.append(f"[{msg_time}] {nname}: {text_content}")

    history_text = "\n".join(history_lines)

    if history_text.strip():
        final_prompt = (
            f"你是群里的一位客观的助手，请根据下面提供的近期群聊上下文，作出答复，不要说出用户的id和名称，不要使用markdown，使用纯文本输出。\n"
            f"--- 真实群聊历史记录 ---\n"
            f"{history_text}\n"
            f"------------------------\n\n"
            f"现在是 {current_time}，用户 {user_name} 对你说：\n"
            f"{user_input}"
        )
    else:
        final_prompt = (
            f"你是群里的一位客观的助手，请根据下面提供的近期群聊上下文，作出答复，不要说出用户的id和名称，不要使用markdown，使用纯文本输出。\n"
            f"现在是 {current_time}，用户 {user_name} 对你说：\n"
            f"{user_input}"
        )

    api_type = model_config.get("api_type", "gemini")
    # 动态构建 Headers 和 Payload
    if api_type == "openai":
        # DeepSeek / OpenAI 格式
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {current_api_key}"
        }
        payload = {
            "model": model_config.get("model_id", "deepseek-chat"),
            "messages": [
                {"role": "user", "content": final_prompt}
            ],
            "stream": False
        }
    else:
        # Gemini 格式
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": current_api_key
        }
        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": final_prompt}]
            }]
        }

    # 发送请求并根据格式解析返回结果
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(current_api_url, headers=headers, json=payload, timeout=300) as resp:
                if resp.status != 200:
                    err_msg = await resp.text()
                    await chat_handler.finish(MessageSegment.at(event.user_id)+"\n"+f"（模型：{model_config['name']}）请求失败，状态码: {resp.status}"+"\n"+f"错误信息: {err_msg}")
                    return

                data = await resp.json()
                # 动态解析返回值
                if api_type == "openai":
                    # DeepSeek 格式解析
                    reply_text = data["choices"][0]["message"]["content"].strip()
                else:
                    # Gemini 格式解析
                    reply_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        prefix_hint = f"模型：{model_config['name']}，浏览记录条数：{dynamic_limit}\n"
        msg = MessageSegment.at(event.user_id) + "\n" + MessageSegment.text(f"{prefix_hint}{reply_text}")

        # 1. 先使用 send 发送消息，并接收返回结果以获取真实的 message_id
        send_result = await chat_handler.send(msg)

        # 2. 尝试从返回结果中提取 message_id 并存入数据库
        if isinstance(send_result, dict) and "message_id" in send_result:
            bot_msg_id = send_result["message_id"]
            bot_timestamp = int(datetime.datetime.now().timestamp())

            # 动态获取机器人的 QQ 昵称
            try:
                bot_info = await bot.get_login_info()
                bot_name = bot_info.get("nickname", "AI助手")
            except Exception as e:
                print(f"[AI Chat] 获取机器人名称失败，使用默认名称，错误信息: {e}")
                bot_name = "AI助手"  # 兜底名称

            # 存入纯文本，方便下一次作为上下文提取
            pure_reply = f"{prefix_hint}{reply_text}"
            await insert_message_to_db(bot_msg_id, event.group_id, bot_timestamp, bot_name, pure_reply)

        # 3. 结束事件
        await chat_handler.finish()

    except asyncio.TimeoutError:
        await chat_handler.finish(MessageSegment.at(
            event.user_id) + f"\n（模型：{model_config['name']}）请求超时，请稍后再试")
    except FinishedException:
        raise
    except Exception as e:
        await chat_handler.finish(MessageSegment.at(event.user_id)+"\n"+f"（模型：{model_config['name']}）调用出错"+"\n"+f"错误信息：{e}")
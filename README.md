# easy_QQBot
部署环境Debian13.3 x64  

基于**nonebot**+**napcat**实现
## 功能说明
一个nonebot插件  
简单接入ai的QQBot  
具备简单的模型可选能力  
依据时间尺度的对话频率，结合ai动态调整聊天记录浏览范围

## 搭建说明
### napcat安装及配置
官方文档：https://napneko.github.io/guide/napcat  

运行`curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh --docker n --cli y`  
参数解释：安装TUI-CLI、不使用docker  
安装安成后直接运行`napcat`进入可视化界面，配置QQ号，启用ws反代，设置地址、端口和token即可（这里需要和下面的nonebot一致）  
比如：`ws://127.0.0.1:8080/onebot/v11/ws`

### nonebot安装及配置
官方文档：https://nonebot.dev/docs/  

**安装说明**  
选一个文件夹作为安装文件夹  
安装虚拟环境:`python3 -m venv venv`  
激活虚拟环境：`source .venv/bin/activate`  
安装：`pip install nb-cli nonebot2 nonebot-adapter-onebot`  
安装的时候是可视化交互，选择**OneBot V11**协议；然后选择**Current project**，也就是当前目录，其他自行研究  

**配置说明**  
去到nonebot安装目录下，找到`.env`文件,在其中添加或修改
```commandline
HOST=127.0.0.1
PORT=8082
ONEBOT_ACCESS_TOKEN="这里填你刚才在NapCat里写的那个13位以上的Token"
COMMAND_START=["/", ""]
```

### 插件安装及配置
这个插件需要额外安装库  
`pip install "nonebot2[fastapi]"`  
`pip install aiohttp`  
在**easy_ai.py**里配置**apikey**和**api站点**，设置**白名单群**。   
在nonebot安装目录下找到**plugins**文件夹，将**easy_ai.py**放进去即可。   
在虚拟环境下执行`nb run`即可在当前命令窗口运行。  
等待nonebot和napcat通信成功后，@对应qq即可触发ai回复。   
注意：挂载服务（systemctl）的时候需要留意虚拟环境，建议指定虚拟环境运行，本质还是`nb run`





# 菲比啾比 · 抖音 / QQ 双端角色

一个会自己刷抖音的角色。

她住在两个地方：QQ 和抖音私信。你在哪边跟她说话都行，她会回；你让她去看点什么、发点什么，她真的会打开浏览器去做，而不是嘴上答应。

不是演的。她背后开着一个真实的 Edge 窗口，在刷真实的抖音网页版。

---

## 她能干什么

- **自己刷抖音**：滚推荐流，挑几条看看，回来跟你说刷到了什么、哪条让她停下来
- **抖音私信聊天**：你在私信里发消息，她按自己的性格回你，可以连发好几条，不是一问一答的客服腔
- **把视频分享给你**：从推荐流里挑一条，直接发一张**视频卡片**到你私信里，点开就能看（不是贴一串链接）
- **看你发给她的视频**：你分享一个视频过去，她会打开、听完里面说的话、看几帧画面，然后跟你聊内容，而不是只念标题
- **点赞、收藏、捡表情包**：刷到喜欢的会留下；评论区看到好玩的表情图会捡进自己的库，聊天时发给你
- **会拒绝，但不会装不会**：不想干、懒得动、跟你谈条件都可以，但不会说「我没有这个功能」

---

## 跑起来需要什么

| 东西 | 用来干嘛 | 说明 |
| --- | --- | --- |
| Windows 10 / 11 | 运行环境 | 要用本机的 Edge，所以只支持 Windows |
| Docker Desktop | 跑 AstrBot | 装好并启动，保持运行 |
| Python 3.11 以上 | 跑抖音这边 | 装的时候记得勾「Add to PATH」 |
| ffmpeg | 处理视频 | 装完要能在命令行里直接敲 `ffmpeg` |
| Edge 浏览器 | 刷抖音用的就是这个 | Windows 自带，不用另装 |
| 一个 QQ 小号 | 给她登录 | 小号比大号安全，风控概率低 |
| 一个抖音账号 | 给她登录 | 首次启动扫码，之后不用再扫 |

还需要一个模型 API Key（默认用 DeepSeek），下面会写在哪填。

---

## 怎么装

从头到尾八步。每一步都有「做完应该看到什么」，对不上就先停在那一步。

### 第 1 步：起 AstrBot

先确认 Docker Desktop 已经启动（任务栏那条鲸鱼不动了就是好了），然后开 PowerShell：

```powershell
docker run -d --name astrbot `
  -p 6185:6185 -p 6199:6199 `
  -e TZ=Asia/Shanghai `
  -v "D:/astrbot/data:/AstrBot/data" `
  soulter/astrbot:latest
```

`D:/astrbot/data` 换成你想放数据的位置，**这个目录别删**，插件、人格、记忆都在里面。

拉镜像慢就换国内源，把镜像名换成 `m.daocloud.io/docker.io/soulter/astrbot:latest`。

起完看初始密码：

```powershell
docker logs astrbot | Select-String "密码|password"
```

浏览器打开 `http://127.0.0.1:6185`，用这个密码登录。

> 6185 是管理面板，6199 留给 QQ 用，不要改。

### 第 2 步：把 QQ 接上（NapCat）

1. 下载 NapCat 自包含版，解压，运行 `napcat.bat`
2. 弹出的窗口里扫码登录 **QQ 小号**
3. 记下它的 WebUI 地址，一般是 `http://127.0.0.1:6099`

然后让 NapCat 主动连 AstrBot：

- NapCat WebUI → 网络配置 → 新建 **WebSocket 客户端**
- 地址填 `ws://127.0.0.1:6199/ws`，消息格式选 `array`，启用
- 回到 AstrBot 面板 → 平台适配器 → 新建 → 选 **QQ(aiocqhttp)** → 端口 `6199`

做完在 QQ 里给小号发一句「在吗」。如果 AstrBot 的日志里能看到这条消息，链路就通了（这时候插件还没装，回你的是 AstrBot 自带的默认助手，答得很怪是正常的）。

### 第 3 步：配模型

AstrBot 面板 → 服务提供商 → 新增 → 选 **OpenAI 兼容**：

| 项 | 值 |
| --- | --- |
| Base URL | `https://api.deepseek.com/v1` |
| 模型 | `deepseek-chat` |
| API Key | 你自己的 |

填完点「测试」，能出字就保存。

### 第 4 步：装插件

到 AstrBot 的数据目录下，进 `plugins` 文件夹（就是刚才挂载的 `D:/astrbot/data/plugins`）：

1. **本项目插件**：把仓库里的 `feibijiubi/` 整个文件夹复制进去
2. **记忆大脑**：把 `astrbot_plugin_memory_brain/` 整个文件夹复制进去。它负责长期记忆，不在本仓库里，得单独拿到；不装也能跑，只是记性差一些

放好后目录长这样：

```
D:/astrbot/data/plugins/
  feibijiubi/
  astrbot_plugin_memory_brain/
```

然后重启容器：

```powershell
docker restart astrbot
```

重启后翻日志，应该能看到 `[feibijiubi]` 开头的行：

```powershell
docker logs astrbot | Select-String "feibijiubi"
```

> 记忆大脑自带 SQLite，不用另装数据库。插件的数据存在 `data/plugin_data/feibijiubi/` 下。

### 第 5 步：起抖音这边

打开 PowerShell，进到仓库里的 `host` 目录：

```powershell
cd <仓库路径>/host
pip install -r ../requirements.txt
python -m playwright install msedge
python service.py
```

第一次跑会弹出一个 Edge 窗口，停在抖音登录页。**手动扫码登录**，登录完不用管它，这个窗口就是她以后刷视频用的。

看到 `ready` 就说明起来了。另开一个窗口确认一下：

```powershell
curl.exe http://127.0.0.1:8791/health
```

能返回内容就是好的。

这一步是**前台**跑，图的是能看见窗口和日志，方便扫码、排查。等第 6、7 步都验完，再按第 8 步换成后台常驻。

两个注意：

- `host/edge_profile/` 这个目录存的是抖音登录态，**别删、也别用别的浏览器占它**。删了就要重新扫码。
- 如果你把服务放在另一台机器上，或者改了端口，插件那边也要跟着改（见下面「配置」）。

### 第 6 步：给她一个人格

AstrBot 面板 → 人格 → 新建：

1. 名字填「菲比啾比」
2. 内容把仓库里 `菲比啾比-人格.md` 的正文**整段**粘进去，别截断
3. 保存后点「设为默认」

她读人格的顺序是：先看 `data/cmd_config.json` 里 `agent_runner.config.persona.persona_id` 指向哪一条，取到就用；取不到就退一步，用人格库里最长的那条。所以一般不用管 ID——想严格对上，就把那个 `persona_id` 改成你新建这条的 ID。

### 第 7 步：确认她活着

按这个顺序试，每一步只验一件事，卡在哪一步就修哪一步：

| 试什么 | 应该发生什么 | 说明什么 |
| --- | --- | --- |
| QQ 私聊发「在吗」 | 她回你 | QQ 链路通 |
| QQ 发「刷会儿抖音」 | 她回几条刚刷到的视频 | 浏览器通 |
| 抖音私信发一句 | 十几秒后她回你 | 私信轮询通 |
| 抖音私信分享一个视频，再问「看完没」 | 她先说一句「我看看」，随后讲视频里说了什么 | 视频理解通 |
| 隔十来分钟再跟她说句话 | 她接得上之前聊的内容 | 记忆通 |

### 第 8 步：让她开机自己起来（建议）

不挂这个，电脑重启后她就没了。

先回到第 5 步那个窗口按 `Ctrl+C` 停掉（不然端口和浏览器会被占着），然后：

```powershell
cd <仓库路径>/host
python start_service.py
```

它会把服务起成一个独立进程——这个 PowerShell 关掉也不影响；然后等大约 40 秒，确认真起来了才退出。服务自己在后台挂一个守着的进程：每 30 秒看一眼还活着没，掉了就重新拉起来；AstrBot 的 6185 掉了会顺手重启容器。

想开机自启，就把它加进启动项（Win+R 输入 `shell:startup`，把 `start_service.py` 的快捷方式丢进去，目标写成 `pythonw.exe <路径>\start_service.py`）。

---

## 怎么用

不用记命令，正常说话就行：

- 「刷会儿抖音，回来说说看到啥」
- 「分享个视频给我」
- 「这个视频看了吗」
- 「发个表情包」

她判得出来你是在聊天还是在派活。想直接点名也行，这些命令一定生效：

| 命令 | 干什么 |
| --- | --- |
| `/刷抖音` `/刷会抖音` | 刷一轮推荐流，回来讲刷到了什么 |
| `/分享视频给我` | 挑一条，以视频卡片发到你现在说话的这一边 |
| `/看分享` | 打开你最后发来的那个视频，看完再说 |
| `/表情包` | 从她自己的库里挑一张发你 |
| `/捡图` | 去评论区捡表情图 |
| `/发说说` | 在 QQ 空间发一条 |

---

## 配置

一般不用碰。要改就设环境变量，**宿主服务和插件两边的口令必须一样**。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FEIBI_TOKEN` | `feibi-dy-2026` | 两边通信的口令，**部署时请改掉**（插件侧在 `main.py` 顶部的 `TOKEN`，也可以给容器设同名环境变量） |
| `FEIBI_PORT` | `8791` | 抖音服务的端口，改了要同步改 `main.py` 顶部的 `SERVICE_URLS` |
| `ASTRBOT_DATA` | 自动找 | AstrBot 的 data 目录，找不到时手动指定 |
| `DEEPSEEK_API_KEY` | 无 | 看画面用的；不填就从 AstrBot 的配置里读 |
| `FEIBI_VISION_MODEL` | `deepseek-flash` | 看画面用的模型 |

抖音服务监听在本机 `8791`，只靠这个口令保护，**不要把它暴露到公网**。

---

## 致谢

她不是一个人搭起来的，有两份东西必须写清楚：

**记忆大脑**（`astrbot_plugin_memory_brain`）
长期记忆靠它：分类型存储、混合检索、渐进遗忘，自带 SQLite。本项目一行没改它的代码，只是把她接了进去。

**大肥鱼**：[whyever77/dafeiyu-astrbot](https://github.com/whyever77/dafeiyu-astrbot)
一个把「角色住进 QQ 里」这件事做到很完整的开源项目。人格怎么写、表情怎么处理、图片和语音怎么看、AstrBot + NapCat + DeepSeek 这套组合怎么配，都参考了它的做法。

还有底下这些：

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 聊天框架
- [NapCat](https://github.com/NapNeko/NapCatQQ) — QQ 协议端
- [Playwright](https://playwright.dev/) — 驱动浏览器
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 视频里的语音转文字

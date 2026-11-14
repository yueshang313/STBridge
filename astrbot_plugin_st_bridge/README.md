# astrbot_plugin_st_bridge

STBridge 的核心和 AstrBot 插件：把 QQ 消息转给 SillyTavern（ST）生成回复，把回复拆成中文文字和日文语音文本，再由内置的语音合成（Irodori 或 GPT-SoVITS，见下文“语音合成”）合成语音。核心内置一个管理控制台。QQ 端可以走 AstrBot 插件，也可以让 NapCat 直接连核心（见“聊天平台”）。这份文档面向开发者；普通用户请看仓库根目录的 README.md 和 docs/。

## 组成

| 组件 | 地址 | 说明 |
| --- | --- | --- |
| 桥接服务 | `127.0.0.1:11452` | ST 扩展 `st_bridge_client` 轮询这里领取消息、回传回复；受恢复闸门控制 |
| 控制台 | `127.0.0.1:11453` | 网页管理总览、语音、角色和情绪、表情包、定时计划、唱歌、好感度、ST 注入、接入、安装与路径、系统；不受恢复闸门控制 |
| 连接器 | `127.0.0.1:11454`（WebSocket） | AstrBot 插件（以及以后的其他连接器）连接核心的端口，见下文“核心是单独的进程” |
| NapCat | `127.0.0.1:11455`（WebSocket，路径 `/onebot`） | NapCat（OneBot v11 反向 WebSocket）连接核心的端口，见下文“聊天平台：AstrBot 或 NapCat” |

这些服务都只监听本机回环地址。桥接服务和控制台不需要令牌；连接器端口用数据目录里自动生成的 `connector_token` 验证，NapCat 端口用同样自动生成的 `onebot_token`（NapCat 以 `Authorization: Bearer` 带上），两个端口都拒绝带 `Origin` 的请求（网页不能冒充连接器）。控制台启动失败（端口被占用、数据目录不可用）只记一条日志，不影响桥接。配置文件损坏不再算启动失败：核心把坏文件改名为 `config.json.broken-<时间>`，依次改用 `config.json.bak`、默认设置，控制台顶部出现红条说明用了哪一份（“知道了”可以关掉，保存一次设置也会清掉）；`affinity.json` 同样处理（读取容忍 BOM，每次写入前先存 `.bak`）。

## 核心是单独的进程

从这一版起，桥接的核心（ST 接口、控制台、排队、好感度、点歌、定时计划、语音、翻唱……）是一个单独的进程，AstrBot 插件只是连接器：

- **启动和停止**：插件**只连接、不启动核心**。核心是发布包里 `start.bat` 启动的独立程序（嵌入版 Python，和 AstrBot 无关）；先开后开 AstrBot 都行，插件每次连接前读插件目录里的 `core_link.json`（内容 `{"data_dir": "<发布目录>\\data"}`，由控制台的“安装插件”写入）找到核心的数据目录，从那里读连接令牌和端口，核心没开就每隔几秒重连一次，日志里只提示一次“STBridge 核心未运行，请先运行 start.bat”（原因变了才再提示）。插件卸载或重载只断开连接，**绝不关闭核心**。连上后如果核心的版本或构建号（核心目录里所有代码和前端文件的哈希，握手时带上）和插件自带的那份不一致，只记一条警告“插件和核心版本不一致，请在控制台重装插件”，照常工作，不替换、不关闭核心。核心的日志在 start.bat 的窗口和数据目录的 `logs/bridge.log` 里，不再转进 AstrBot 后台日志。
- **核心不在时**：本来会交给 ST 的私聊和群聊消息，插件回复核心上次握手时留下的“桥接当前不可用……”那句话（从没握手成功过就用内置文案）；`/stbridge_ping` 照旧由插件自己回复。连接会自动重连。
- **日志**：核心每天一个文件，写在数据目录的 `logs/bridge.log`（前一天的文件带日期后缀），保留最近 7 天。日志里没有聊天原文，用户 ID 只记哈希。控制台“系统 → 日志”能看最近 100 / 300 / 1000 行，也能把全部日志文件打成 zip 下载。
- **主人 ID**：`operator_platform_id`、`operator_sender_id` 已经移到控制台“接入”页的“主人”卡片，修改后立即生效。第一次启动时核心会从 AstrBot 插件配置里导入一次，之后只认控制台里的值。主人不属于总预设（它是部署信息，不是场景）。好感度里“主人”那一条跟着主人 ID 走：换了 ID，就会换成新 ID 对应的记录。
- **端口**：新增“连接器端口”（默认 11454），和其他三个端口一起校验，保存后重启核心（关掉 start.bat 的窗口再运行一次）才生效；插件会读新端口自动重连。控制台“总览”的健康链路里有一个“QQ 平台”节点，侧栏底部也写着当前聊天平台，显示它的连接状态（AstrBot：连接器名称和版本；NapCat：小号 QQ 号），或“未连接”。端口一共五个（新增“NapCat 端口”，默认 11455）。再运行一次 start.bat（核心已在运行或端口被别的程序占用）会打印一行“核心启动失败：端口被占用……”并以非 0 退出，不会影响已在运行的核心。

### 访问控制

- **桥接服务**：只接受 `Origin` 为 `http://localhost:11451`（ST 页面）且 `Host` 为 `127.0.0.1` 的请求，业务 POST 必须是 `application/json`。浏览器无法伪造 Origin；ST 页面上的脚本本来就能读到任何放在页面里的令牌，所以令牌不提供额外保护。旧版扩展发送的 `Authorization` 头会被忽略。
- **控制台**：只接受 `Host` 为 `127.0.0.1:11453` 或 `localhost:11453` 的请求（防 DNS rebinding）；所有非 GET 请求如果带有 `Origin`，必须是 `http://127.0.0.1:11453` 或 `http://localhost:11453`，否则返回 `403 forbidden_origin`（防跨站表单和脚本提交）。不带 Origin 的请求只可能来自本机的非浏览器程序，予以放行。GET 响应不带 CORS 头，其他网页读不到内容。

## 安装与路径页（控制台）

控制台的“安装与路径”页管这台电脑上的安装：

- **路径**：SillyTavern、AstrBot、NapCat（只记启动文件）、GPT-SoVITS、Irodori（目录 + 服务端目录）、RVC，都可以留空；每项显示“有效 / 无效 + 原因”。ST 和 AstrBot 的检查接受两种目录布局（ST 程序目录或数据目录，`config.yaml` 的 `dataRoot` 也会看；AstrBot 的 `data\plugins` 或 `core\data\plugins`）。GPT-SoVITS / Irodori / RVC 的路径还是存在原来的设置里（`tts.*.launch_dir`、`tts.irodori.server_dir`、`cover.rvc_root`），语音页和唱歌页里只显示，不能改。ST / AstrBot / NapCat 存在新的 `install` 段。
- **总预设不含路径**：导出的预设没有这些路径（GPT-SoVITS / Irodori 的目录、Irodori 的服务端目录和 LoRA 目录、RVC 目录、`install` 段），应用预设时保留本机的路径（和端口一样）。
- **安装 / 重装 / 卸载** AstrBot 插件和 ST 扩展：从发布包的 `addons\` 复制到检查出来的 `plugins` / `extensions` 目录里固定名字的文件夹（`astrbot_plugin_st_bridge`、`st_bridge_client`）；已存在的先整个备份到 `data\backups\astrbot-plugin-<时间>` / `st-extension-<时间>`（每种备份只留最新 3 份，`INSTALL_BACKUP_KEEP`，只认 `<标签>-<YYYYMMDD-HHMMSS>[-N]` 这种名字的文件夹，别的文件夹不碰；控制台“系统 › 存储清理”的“插件 / 扩展备份”一行显示份数和大小，“只保留最新一份”按钮对应 `POST /api/cleanup/install-backups/clear`，`GET /api/cleanup` 带 `install_backups`）；插件里写 `core_link.json`。不碰 AstrBot 的 `plugin_data`。开发布局（没有 `addons\`）里这些按钮是禁用的。
- **从旧插件导入数据**：先预览（文件和大小、会替换哪些、不复制哪些），确认后复制；日志、临时目录和旧备份不复制，来源只读；这里已有的同名文件先备份到 `data\backups\pre-import-<时间>`；两个令牌文件会复制；配置里指向旧数据目录的绝对路径（比如参考音频）会改到新目录；这台电脑上已填的 ST / AstrBot / NapCat 路径（`install` 段）会保留，不会被旧配置覆盖。确认导入后核心会自动重启（退出码 75，`start.bat` 重新启动它；核心的内存里还是旧状态，所以重启前会拒绝一切保存，避免盖掉刚导入的文件）；不是 `start.bat` 启动的核心只是退出，页面提示重新启动。
- **额外依赖**：一张小表（现在只有 pedalboard，装进 `<RVC>\runtime\python.exe`），后台安装，同一时间一个任务；pip 镜像可改，留空用官方源。
untime\python.exe`），后台安装，同一时间一个任务；pip 镜像可改，留空用官方源。
- **启动 NapCat**：设置了启动文件才显示，在新的命令行窗口里运行它，不读 NapCat 的任何文件。
- **重启核心**：系统页（端口旁）和导入完成后有“重启核心”按钮；核心以退出码 75 退出，`start.bat` 看到 75 就重新启动（只在第一次打开浏览器）；不是 `start.bat` 启动的核心只是退出，页面会提示重新启动。

## 配置

AstrBot 管理面板的插件配置（`_conf_schema.json`）里还有 `operator_platform_id`、`operator_sender_id`，但它们现在**只在第一次启动时导入一次**；主人 ID 以后在控制台“接入”页的“主人”里改（见上文）。

旧版本的 `bridge_token`、`console_token` 已删除；配置文件里残留的值会被忽略。

## 消息桥接

- **私聊**：只接受配置的操作者发来的纯文本消息。
- **群聊**：AstrBot 路径只处理 QQ 官方机器人（`qq_official`）群里 @ 机器人或唤醒前缀触发的纯文本消息（NapCat 路径见下文“聊天平台”）；发给 ST 的文本会加上“【群友】昵称（序号）：”前缀（前缀可在“好感度”页改），用来区分发言人。
- 同一时间只有一个请求在 ST 里生成，其余的排队：多人同时发消息时按到达顺序依次处理，同一人排队期间连发的消息合并成一条；排队上限、最长等待和回复时限在控制台“接入”页的“消息排队”里设置（`queue.max_waiting`、`queue.max_wait_seconds`、`queue.reply_timeout_seconds`；默认 10 条 / 300 秒 / 180 秒，回复时限范围 60～600 秒），超出时回复“回复文案”里对应的固定话。请求发给 ST 后等回复的时限就是 `queue.reply_timeout_seconds`，排队名额被占住不放时的强制释放时间 = 时限 + 120 秒（`QUEUE_HOLD_EXTRA_SECONDS`）；一条回复的正文上限是 128 KiB（核心 `http_protocol.DEFAULT_MAX_REPLY_BYTES` 和扩展 `bridge_client.mjs` 的 `MAX_REPLY_BYTES` 要一起改）；ST 上一次生成没有确认结束前不会发下一个。
- 命令（如 `/stbridge_ping`）不会被当作聊天内容转发。

### 恢复闸门

核心每次启动后处于 `RECOVERY_REQUIRED` 状态，桥接业务接口返回 `503 recovery_required`，防止重启前遗留的 ST 生成被当成新请求的回复。

闸门由 ST 扩展**自动解除**：扩展收到 503 时（或启动轮询时），如果 ST 没有在生成、扩展手里也没有未完成的请求，就调用 `POST /v1/session/ready`（`{"generating": false, "active_request": false}`），闸门随即开放，日志记 `[STBridge] Recovery confirmed by ST client`。ST 正忙时扩展会等生成结束后再报告。重启前扩展手里的旧请求在新的核心里已不存在，扩展会放弃它并回到空闲。

`/stbridge_recover` 保留为人工兜底：操作者私聊发送（AstrBot 下需带唤醒前缀，如 `/琴音 stbridge_recover`）也可以解除闸门。`/stbridge_ping` 用于确认插件已加载。

### 在岗 ST 页面

浏览器里可能同时开着不止一个 ST 页面。ST 扩展每次加载页面都生成一个随机的 `client_id`，作为查询参数 `client_id` 带在 `/v1/session/ready`、`/v1/requests/pending` 和每个针对某个请求的调用上（`http_protocol.ActivePageLease`）。核心只让一个“在岗页面”领任务：

- 没有已领取的请求时，新报到的页面**立即接管**；有请求正在处理（已领取未结束）时，在岗页面不换，新页面排成候选，等这一条结束再交接（生成中不切）。
- 不在岗的页面，`pending` 返回 `{"active": false}`，`claim` 返回 `409 not_active_client`；别的页面对已被领取的请求提交结果、报告等，返回 `409 not_claim_owner`。扩展面板据此显示“另一个 ST 页面正在连接核心，本页不会接收消息”。
- 被顶下去的页面，要等在岗页面静默超过 10 秒（`ACTIVE_PAGE_LEASE_SECONDS`）才能重新接手。没带 `client_id` 的旧版扩展按单一客户端处理，行为和以前一样。
- 生成中刷新 ST 后，原来的“生成未确认”标记会被清掉；请求过期满 10 分钟，创建新请求时也会顺带清掉它。
- 核心只保证**只有一个页面收到消息**，管不了两个页面手动编辑同一个聊天时 ST 自己的互相覆盖。

### 回复格式

ST 的回复要把最终内容放进一对 `<game>…</game>`（模型的思考写在 `<game>` 之前；没有 `<game>` 时按旧方式处理整段）。`<game>` 里依次是：情绪标签，然后是一组或多组台词，每组是一段 `☆‹日文›` 加一对英文双引号 `"中文"`：

- 中文作为 QQ 文字回复；
- 日文写入事件 extra，由核心在发送前合成语音；
- 一条回复里有多组台词时，默认合并成一条消息（中文换行连接、日文合成一段语音）；在“语音 › 通用设置”打开“分条发”后，每组各发一条（各自的语音和情绪标签，最多条数和间隔可设）；
- 格式不符（没有可提取的台词对）时日志记 `[STBridge] Reply rejected (InvalidBilingualReply)`，QQ 收到“回复文案”里的 `invalid_reply`（默认“（这次回复的格式不对，没能送达，请再说一次）”），不合成语音。

请求超时、ST 生成失败或桥接停止时，QQ 会收到对应的中文提示（例如“请求已超时……”），同样不合成语音。

处理顺序（有测试钉住，不要随意调换）：先选出 `<game>` 块（跳过成对的 `<think…>`、`<comment>`、`<details>` 区域里的 `<game>`，取最后一个完整块；块之前的内容丢弃），好感度的 `_.add` 从块内和 `</game>` 之后的文字里收集；块内再删除 MVU 的 `<UpdateVariable>…</UpdateVariable>`（不区分大小写；只有开头没有结尾时删到文末）和 `<StatusPlaceHolderImpl/>`，取情绪标签，最后拆分双语。块里的分析文字常带英文引号，先删除才不会让双语检查失败。用户发来的文字和群友昵称在进入 ST 之前会把 `{{` 换成全角、把 `<game`、`</game`、`<UpdateVariable` 的 `<` 换成全角 `＜`，防止别人往 ST 里塞宏或假标签。

## 聊天平台：AstrBot 或 NapCat

核心同时有两条通往聊天软件的路，但**同一时间只有一条在工作**，在控制台“接入”页的“聊天平台”里二选一（默认 AstrBot，保存后立即切换，不用重载，也不会碰 ST）：

- **AstrBot**：QQ 官方机器人，经 AstrBot 插件连接（和以前完全一样）。
- **NapCat**：QQ 小号，用 NapCat（OneBot v11）直接连核心，不需要 AstrBot。

两个平台是两个独立的角色实例：好感度的键带平台 ID（NapCat 固定是 `napcat`），主人也各算各的，不做身份合并。没在工作的那个平台，它的消息被静默忽略（连接照样保持，状态照样显示）；定时任务的目标只能发到当前平台的会话，另一个平台的会话会被跳过并记日志（`result=skip:OtherPlatform`）。总预设不包含平台选择、白名单、NapCat 主人和令牌。

### 使用 NapCat 的步骤

1. **启动核心**：双击发布包里的 `start.bat`（嵌入版 Python，窗口里看日志，关窗口或 Ctrl+C 退出；`--open-console` 让它启动后自动打开控制台）。用 AstrBot 时不需要别的操作：插件会自动连上（先开 AstrBot 也行）。不要同时启动两个核心：它们要用同样的端口，第二个会说明原因后退出。
2. **在 NapCat WebUI 填写**：网络配置 → 新建 → **WebSocket 客户端**，URL 填 `ws://127.0.0.1:11455/onebot`，Token 填控制台“接入”页“NapCat 连接”里显示的令牌（两项都有复制按钮），**消息格式选 array**，保存时启用。连上后控制台显示“已连接”和小号 QQ 号。收到非 array 格式的消息时会忽略并在控制台提示。
3. **设置主人和白名单**：在“接入”页的“聊天平台”里填 NapCat 主人 QQ 号和群白名单（群号，每行一个）。
4. **切换**：把“使用的平台”改成 NapCat 并保存。

### NapCat 平台的行为

- **私聊**：只有主人的私聊会交给 ST，其他人的私聊（包括命令）不回复。主人私聊开头的“/昵称”会被去掉；命令可以带前缀也可以不带。
- **群聊**：只在白名单里的群回复（名单为空就一个群也不回），名单外的群消息完全忽略，连定时目标也不记。被 @ 机器人，或消息以“/昵称”“昵称”开头（昵称就是当前角色的昵称）才会唤醒，开头的前缀会被去掉再交给 ST。排队后回复的“@昵称”规则不变（仍是文字）。
- **命令**：`stbridge_ping`、`好感度`、`好感度设置 <值>`、`stbridge_recover`、`唱 歌名`，由核心自己解析（群里要先唤醒）。
- **消息内容**：只取文字（按顺序拼接），@ 机器人自己的部分去掉；图片、语音、表情、回复等先忽略；没有文字就不处理。机器人自己发的消息、通知和好友 / 加群请求都不处理（不会自动同意任何请求）。
- **发送**：文字和图片合成一条，语音单独一条，顺序和 AstrBot 路径一致；文件用本地绝对路径（正斜杠）交给 NapCat，语音由 NapCat 自己转成 24 kHz silk。没有被动回复窗口，点歌的两种投递方式都直接发。表情包按普通图片发送（GIF 能否正常显示要在主机上实测）。
- **端口被占用**：NapCat 端口（默认 11455）打不开时核心照常启动，只记一条 error 日志；控制台“接入 → NapCat 连接”和总览显示“端口 11455 被占用，NapCat 不可用”，AstrBot 不受影响。换个端口后重载插件即可。
- **安全**：NapCat 端口只监听 127.0.0.1，带 `Origin` 的请求被拒绝，令牌错误返回 401；同一时间只保留一个 NapCat 连接，新连接顶替旧连接。日志里没有聊天原文，QQ 号和群号只记哈希，令牌不进日志。

## 好感度（每个 QQ 用户一个数值）

角色卡基于 MVU，所有人共用一个 `<user>`，`stat_data.<ST 角色名>.好感度` 原本也只有一个。插件改为按 QQ 用户分别记录，每轮切换发言人：

1. 创建桥接请求时，查出发言人的好感度（新用户为初始值），随请求交给 ST 扩展（领取接口返回 `affinity: {character, value}`）。群聊发给 ST 的前缀变为 `昵称（n）[好感度:45]：消息`，私聊不加前缀。
2. ST 扩展在追加用户消息之后、开始生成之前，把这个数值写入该用户消息楼层的 `stat_data.<ST 角色名>.好感度[0]`（数组第二项的描述保留），世界书的 `getvar` 和预设的 `{{get_message_variable::…}}` 都读这一楼，因此会选中这位发言人对应的档位。
3. 回复中 `<UpdateVariable>` 块里针对该角色的 `_.add('<ST 角色名>.好感度', N)`（可带 `[0]`、单双引号、小数四舍五入，多次求和）和 `_.set('路径', [旧值,] 新值)`（按新值减当前值）会被解析为增减量，先限制在 ±`max_delta` 以内，结果再限制在 0～100，存到发言人名下。其他角色的指令忽略，仍由 MVU 自己管理、大家共用。
4. **只有请求成功且双语回复送达时才更新**；失败、超时、格式不合法时不改动。

每次送达记一条日志（`user` 是 `平台ID:发送者ID` 的 SHA-256 前 8 位，不含昵称和文本）：

```
[STBridge] Affinity user=3f2a9c01 before=45 parsed=+7 applied=+5 after=50
[STBridge] Affinity user=3f2a9c01 before=45 parsed=none applied=+0 after=45
```

**限制**：QQ 官方机器人在私聊里给出的是 `user_openid`，在群聊里是该群的 `member_openid`，同一个人在私聊和各个群里的 ID 都不同，所以会被当作不同用户分别记录，插件不做合并。

### 指令

- `/琴音 好感度`（“琴音”是默认角色的唤醒前缀）：查询自己的好感度，私聊和群聊都可用，例如 `琴音对你的好感度：45（避风港）`。
- `/琴音 好感度设置 <0～100>`：仅限操作者私聊使用，设置操作者自己的数值，方便测试。

指令不会被转发给 ST。

### 设置与数据

在控制台“好感度”页面查看和修改：用户列表（ID 显示为哈希、最近昵称、数值、档位、更新时间，数值可直接修改），以及角色名、显示名、新用户初始值（默认 0）、每次变化上限（默认 ±5）和档位表。好感度分两条轨道：**所有者**（主人，0～100）和**群友**（每人一份，范围、变量名和发言前缀在“好感度”页设置），各有自己的档位；档位名和态度描述通过 `{{stbridge_affinity_tier}}` / `{{stbridge_affinity_hint}}` 宏发给模型（见下文“ST 注入”）。数据保存在核心数据目录的 `affinity.json`；设置保存在控制台 `config.json` 的 `affinity` 小节，旧配置会自动补上默认值。

ST 扩展面板里的“好感度注入诊断”会把测试数值写入最新楼层、用与世界书相同的 `getvar` 读回、再恢复原值，用于确认注入是否生效。

## ST 注入（R5）

STbridge 需要模型遵守一组固定规则：情绪标签、`<game>` 标签、中日双语、唱歌标签、好感度更新。这些规则做成**可编辑的条目**，由 ST 扩展写进当前的 ST 预设和一个全局世界书，不用使用者手写预设。

- **条目**：存在核心配置的 `injection` 小节里，内置 9 条（对话文风、身份识别、字数要求、中日双语、game 标签、情绪标签、唱歌标签；以及写进世界书的好感度态度、好感度更新规则），可以关掉、删除、调整顺序、新增自己的；删掉的内置条目能在控制台“ST 注入”页点“恢复内置条目”找回。每条有目标（预设或世界书）、位置（默认放在 Chat History 之后，也可以指定深度）和角色（system / user / assistant）。内置条目的默认文字在 `st_bridge_core/st_injection.py`。
- **控制台**：“ST 注入”页编辑条目和世界书名称（默认 `STbridge`），页面底部列出可用的宏。改完保存后条目只存在核心里，**ST 里什么都不会变**，要到 ST 的 “ST Bridge” 扩展面板的 **“注入”** 标签点 **“同步”**。
- **同步**：扩展通过 `GET /v1/injection` 取条目；改预设前先 `POST /v1/injection/backup` 让核心备份当前预设（`data\backups\st-presets\<预设名>-<时间>.json`，保留最新 20 份）；然后把预设条目写成标识符 `stbridge_<id>` 的 prompt，把世界书条目写成 `comment` 为 `STbridge:<id>` 的条目，并把世界书设为全局。**只增删带这些标记的条目**，使用者自己的预设条目和世界书不动。只支持 Chat Completion 预设，当前不是 Chat Completion 时预设部分跳过。
- **移除**：扩展面板的 **“移除”** 只删除带标记的条目，随时可以恢复原样。
- **状态**：“注入”标签会显示预设里已注入几条、和控制台是否一致，世界书是否存在、是否绑定为全局。

### 宏

条目里可以写 `{{stbridge_<key>}}`，ST 每次生成时换成当前这一轮的值。值由核心随每个请求下发（`claim` 里的 `macros`，每个值是不超过 1000 字的字符串）；不是从 QQ 来的生成用最近一次的值。扩展内置注册下面这些键，核心下发的其他键在第一次出现时注册。ST 自己的 `{{char}}`、`{{user}}` 照常可用。用户文字里的 `{{ }}` 会先被转成全角，不会被当成宏展开。

| 宏 | 含义 |
| --- | --- |
| `{{stbridge_emotions}}` | 当前角色在“角色和情绪”页配置的情绪，用“、”连接 |
| `{{stbridge_group_tag}}` | 群友发言前缀（“好感度”页“群友发言前缀”） |
| `{{stbridge_speaker}}` | 本轮发言人：群友是它的昵称标签，主人留空时由扩展填成 ST 当前用户名 |
| `{{stbridge_affinity}}` | 本轮发言人的好感度数值 |
| `{{stbridge_affinity_tier}}` | 本轮发言人所在档位的名字 |
| `{{stbridge_affinity_hint}}` | 本轮发言人所在档位的态度描述（“好感度”页每档的“态度描述”） |
| `{{stbridge_affinity_var}}` | 本轮更新好感度用的变量路径：主人是“ST 角色名.好感度”，群友是群友变量名 |
| `{{stbridge_affinity_range}}` | 好感度的范围，例如 0～100 |
| `{{stbridge_affinity_step}}` | 单次最大变化 |
| `{{stbridge_song_allowed}}` | 本轮是否允许唱歌：是 / 否（点歌功能关闭、好感度不够、次数用完、定时主动消息都是“否”） |
| `{{stbridge_mood}}` | 角色最近的心情走向（所有人和定时消息都会影响），带距今时间和是否已经平复 |
| `{{stbridge_owner_mood}}` | 角色对主人的心情走向（只有主人私聊和发给主人的定时消息会影响） |

宏的键表和说明在 `st_injection.py` 的 `MACRO_KEYS` / `MACRO_DESCRIPTIONS`，扩展一侧的备用键表在 `st_bridge_client/injection.mjs`。


## 语音合成

语音合成在核心里完成：回复发送前合成，排队名额要等语音合成并发送完才释放。只处理桥接产出的回复。出错时文字照常发出，日志只记录错误类型（Irodori 请求失败记状态码和错误类型）。

所有语音设置都在控制台里改，保存在控制台 `config.json` 的 `tts` 小节，**不再放在 AstrBot 的插件配置页**；旧配置缺这一小节时自动使用默认值：

- `engine`：`irodori`（默认）或 `gpt_sovits`。在控制台“语音”页的“语音引擎”里切换，**立即生效，不需要重载插件**；同时会关闭插件自己为旧引擎启动的服务（释放显存），外部启动的服务不受影响。
- `common`：`auto_start`、`stop_on_exit`、`start_timeout`、`tts_timeout_minutes`、`voice_wait_seconds`、`prob`、`text_limit`、`cooldown`、`send_text_with_audio`。
- `irodori`：`host`、`port`、`launch_dir`、`server_dir`、`hf_checkpoint`、`lora_adapter`、`num_steps`（0 表示不传）、`cfg_scale_text`、`cfg_scale_caption`、`default_caption`。
- `gpt_sovits`：`host`、`port`、`launch_dir`、`gpt_model_path`、`sovits_model_path`、`ref_audio_path`、`prompt_text`、`prompt_text_lang`、`text_lang`、`top_k`、`top_p`、`temperature`、`speed_factor`、`split_sentence`。情绪 profile 和“模型”里的模型路径优先，这里的参考音频、采样参数和模型路径只是没有可用 profile 时的兜底。

修改某个引擎的启动参数（`host`、`port`、`launch_dir`，Irodori 还有 `server_dir`、`hf_checkpoint`）会停掉插件自己为它启动的服务，下一次需要语音时按新参数重新拉起；其它参数立即生效，不重启服务。

**Irodori**：`POST /v1/audio/speech`，`input` 为 `<emoji><日文>`，`irodori` 选项固定带 `no_ref=true`、`chunking_enabled=false`（句首表情只作用于第一段）、`cfg_scale_text`、`cfg_scale_caption`（显式传，服务端默认是 3.0），`lora_adapter` 为空时不传，`caption` 取情绪的 caption（没有则用 `default_caption`，都为空不传），`num_steps` 大于 0 才传。不使用参考音频，不调用 `/set_model`。

**语音等待上限与启动失败冷却**：文字和语音一起发时，语音合成最多等 `tts.common.voice_wait_seconds`（默认 60，范围 10～300，控制台“语音 › 通用设置”里改），超时就只发文字，迟到的音频丢弃。语音服务启动失败后进入 5 分钟冷却（`START_FAILURE_COOLDOWN_SECONDS`）：冷却期内需要语音时直接只发文字，不再每条消息都去拉服务；控制台“语音”页的“启动服务”按钮不受冷却限制，并清掉冷却；控制台健康链里语音灯在冷却期显示红色，`GET /api/tts/service` 的状态里带 `cooldown_seconds`。

**服务自动启停**：需要语音而端口不通时，`auto_start` 开启就先启动当前引擎的服务（GPT-SoVITS：`<launch_dir>\runtime\python.exe api.py -a <host> -p <port>`；Irodori：`<launch_dir>\.venv\Scripts\python.exe -m irodori_openai_tts --host <host> --port <port>`，环境变量 `PYTHONPATH=<server_dir>\src`、`HF_HUB_CACHE=<launch_dir>\model_cache\hub`、`IRODORI_HF_CHECKPOINT`、`IRODORI_PRELOAD=true`、`IRODORI_COMPILE_MODEL=false`、`IRODORI_VOICES_DIR=<server_dir>\voices`）。Irodori 预加载完模型才开放端口，端口通即就绪。只管理自己启动的进程；`stop_on_exit` 开启时随插件关闭而停止，并放入 Windows Job Object（AstrBot 异常退出时服务也会退出）。服务输出写入核心数据目录的 `tts_service.log`（上一份保留为 `tts_service.log.1`），临时语音写入 `temp_audio\`，60 秒后删除。

控制台接口（沿用 Host 白名单和 Origin 检查）：`GET /api/tts`（设置和当前引擎）、`POST /api/tts/settings`（整段校验并保存，立即生效）、`GET /api/tts/service`（当前引擎的服务状态和日志末尾）、`POST /api/tts/service/start`、`POST /api/tts/service/stop`、`POST /api/tts/test`（用当前引擎合成一句话，不保存、不发送：请求体可带 `text`、`character`、`emotion`；`emotion_profile` 是某个情绪**未保存**的表单字段，只用于这一次合成，必须同时给 `emotion`；`return_audio: true` 时响应多带 `content_type` 和 `audio_base64`，控制台的“试听”按钮用它，不带则响应和以前一样，自检脚本依赖这一点；失败响应带 `error`、`status`、`error_type`，不带路径；语音服务在启动失败冷却期时带 `cooldown_seconds`）。

## 情绪标签

ST 回复中可以出现情绪标签：

```
[情绪:轻快]
```

- 冒号可以是半角 `:` 或全角 `：`，标签内两侧允许空白，可以出现在回复任何位置。
- 所有标签会在解析双语格式**之前**被删除，不会出现在 QQ 文字里，也不会被合成语音。
- 有多个标签时使用第一个，并记一条警告（只记数量）。
- 情绪名只能包含字母、数字、`_`、`-` 和汉字，最长 40 字符；不合法的标签被删除，并当作没有标签。

### 参数查找与回退

当前只支持单角色：角色取控制台中的“默认角色”。候选情绪依次为：

1. 回复中标签指定的情绪；
2. 角色的默认情绪；
3. 全局默认情绪。

对每个候选情绪 E：

- **仅风格（Irodori）**：手动配置了 E 的 `caption` 或 `emoji`，但没有可用的参考音频（前两项都不满足）→ 返回只含风格的参数（`source=style`，`candidates=0`、`pick=0`）。前两项选中的参数也会带上同名手动配置的 `caption` 和 `emoji`。
- **手动覆盖**：角色下手动配置了 E，且参考音频路径非空、文件存在 → 使用手动配置；
- **目录识别**：否则，在角色所绑定模型的参考音频根目录下找 `E` 子文件夹，从可用音频中**随机选一段**。参考文本取文件名（去掉扩展名）；推理参数取手动配置中 E 的参数，没有时用默认值；
- 都不满足 → 尝试下一个候选。

三个候选都不可用、没有默认角色或角色不存在时，不附带参数，GPT-SoVITS 使用控制台“语音引擎”里配置的固定参考音频，Irodori 使用默认 caption。任何一步出错都不会影响文字回复。目录在每次请求时现场扫描，增删文件立即生效。

参考音频目录结构（文件夹名即情绪名，文件名即参考文本）：

```
<参考音频根目录>\
  开心\そんなすごいアイドルが……あたしを見てくれてるんです。.wav
  害羞\…….wav
  伤心\            （空文件夹：该情绪不可用）
```

WAV 时长必须在 3～10 秒之间，超出范围或文件头无法读取的音频不参与随机选择，但会在控制台中标红显示。模型没有设置参考音频根目录时，使用核心数据目录下的 `audio/<模型名>/`。

选中的参数写入事件 extra `astrbot_plugin_st_bridge.tts_profile`，内容为全部推理参数（`ref_audio_path`、`prompt_text`、`prompt_text_lang`、`text_lang`、`top_k`、`top_p`、`temperature`、`speed_factor`、`split_sentence`），Irodori 风格 `caption`、`emoji`，加上 `emotion`、`character`、`source`（`manual`、`directory` 或 `style`），以及角色绑定模型的 `gpt_model_path`、`sovits_model_path`（两者都配置时才附带）。

每次回复记一条日志，例如：

```
[STBridge] TTS emotion requested=开心 used=开心 source=directory candidates=5 pick=3 fallback=False reason=none
```

`candidates` 是本次可供随机选择的音频数量；`pick` 是选中的第几段（从 1 开始，按文件名排序后只数可用的音频；手动配置时为 1），控制台“自动识别的情绪”里每段音频前的 `#n` 与它对应。`reason` 的取值：`NoTag`（没有标签）、`InvalidTag`（标签名不合法）、`UnknownEmotion`（既没有手动配置也没有同名文件夹）、`MissingAudio`（有配置或文件夹，但没有可用音频）、`NoCharacter`（没有默认角色）、`StoreUnavailable`（控制台配置不可用）。日志不包含回复文本和文件路径。

## 控制台

在运行核心的电脑上用浏览器打开 <http://127.0.0.1:11453/> 即可使用，不需要令牌（访问限制见上文“访问控制”）。左边的导航是“总览”加四组（角色、功能、接入、系统）共 11 页，侧栏顶部是角色名头像和搜索框（Ctrl+K 搜任何设置，索引每次打开时从页面现生成），底部是核心状态、版本、当前聊天平台和“总预设”切换器：

- **总览**：最上面是“开始使用”清单（6 步带进度条，每步自动打勾；状态存在配置的 `console.onboarding`，由 `GET/POST /api/onboarding` 读写，不属于总预设）；接着是状态区——健康链路（平台 → 核心 → ST 扩展 → 模型回复 → 语音 → 翻唱）和一句话结论，有问题时所有页面顶部都会出现红色 / 黄色横幅，浏览器标签页标题加 ⚠。下面是“今天”的 5 张卡片（回复数私 / 群、主动搭话、唱歌数、心情、排队，点击跳到对应页面），然后是安装状态（一切正常时收起），最后是功能开关一览。核心统计来自 `/api/status`（`replies` 最近 20 次模型回复的结果和耗时、`today` 当天计数），所有页面都轮询它，浏览器标签页不可见时暂停。
- **语音**：引擎切换（GPT-SoVITS / Irodori，立即生效）、当前引擎服务的状态与启停、日志最后 30 行、通用设置（自动启停、触发概率、冷却、分条发、合成超时）、两个引擎各自的服务与生成参数、GPT-SoVITS 的模型管理。外部启动的服务显示为“外部启动，插件不管理”，不能从这里关闭。
- **角色和情绪**：角色（昵称、ST 角色名、绑定模型、默认情绪）、心情（整体和对主人的，可重置）、每种情绪的 caption / 表情 / 参考音频、可复制到 ST 预设的提示词。
- **表情包**：按情绪分组管理图片，发送概率与冷却。
- **定时计划**：定时计划、主动搭话（只对主人私聊）、发送目标。
- **唱歌**：歌库与在线搜索、翻唱参数、聊天点歌规则、RVC 服务与翻唱缓存。
- **好感度**：所有者 / 群友两条轨道的设置与档位、用户列表（数值可直接改）。
- **ST 注入**：见上文“ST 注入”。
- **安装与路径**：见上文“安装与路径页”。
- **接入**：聊天平台（AstrBot / NapCat 切换、NapCat 群白名单、NapCat 主人 QQ 号）、NapCat 连接信息、主人（认领 / 手动填写）、消息排队。
- **系统**：常规（端口）、回复文案、日志（关键字和级别筛选、自动刷新开关、导出诊断包）、存储清理（含插件 / 扩展安装备份）、关于。
- **总预设**：不是页面，在侧栏底部，点开是弹窗（保存、切换、导入导出，切换前自动存档最近 5 个；没有预设时名字写“未选择”）。

设置项是行式布局：左边是名称、灰色说明和配置键（来自各设置块的 `collect()` 键，同一份映射也喂给 Ctrl+K 搜索），右边是控件；每个设置模块头部有“恢复默认”，读 `GET /api/config/defaults`（只读，返回 `default_config()`，翻唱设置走 `public_cover_settings`，不含任何用户数据）把本模块字段改回出厂值并进入“未保存”，路径、目录和密钥类字段不恢复。设置页最宽 800px，歌库、日志、表情包图库、好感度用户列表和总览保持宽版（`data-width` / `data-tabbed`）。页顶长说明收在标题旁的“?”里，展开状态按页面记在 localStorage。

**诊断包**：`GET /api/diagnostics/download` 返回一个 zip（日志页的“导出诊断包”，`diagnostics.py`）：`说明.txt`、`版本信息.json`、`状态快照.json`、`服务状态.json`、`配置摘要.json` 和 `日志/`。脱敏是故意从严的：设置里只有数字、布尔值和一小串“种类词”（`ENUM_KEYS`）原样保留，其余文字一律写成“已设置 / 未设置”，名字和数字的列表、字典只剩个数；再把设置里出现过的密钥、路径、ID 类的值（`sensitive_values`）、这台电脑的信息（核心目录、数据目录、家目录、`USERNAME`、`COMPUTERNAME` 等，`environment_values`）和通用形式的 Windows 用户目录路径（用户名所在的那一段）、`/home/<名>` 从包里每一段文字（日志也一样）里抹掉。永远不进包：聊天内容、好感度数据、数据目录里的其他文件。

路径输入框旁有“浏览…”按钮，由控制台服务端列出本机目录（只列名称，不读取文件内容）。所有设置保存在核心数据目录（发布目录里的 `data\`）的 `config.json`，页面底部的“保存”栏保存当前页的改动，保存后立即生效，不需要重启核心（端口除外）。配置文件损坏时核心不会覆盖它：坏文件改名保留（`config.json.broken-<时间>`），改用 `.bak`、再不行用默认设置，并在控制台顶部给出红条（`GET /api/status` 的 `data_recovery`，`POST /api/recovery/dismiss` 关掉）。

前端的纯逻辑在 `st_bridge_core/console_frontend/js/logic.js`，测试：`st_bridge_core/console_frontend/tests/logic.test.mjs`，在 `st_bridge_core/console_frontend` 目录下运行 `node --test tests/logic.test.mjs`。

控制台前端的样式是本项目自有的（深色·靛蓝主题）：设计变量和页面框架在 `css/theme.css`，组件在 `css/components.css`，只用系统字体，不加载外部资源。

## 代码结构

插件分成两层：

- `main.py`、`astrbot_platform.py`、`core_link.py`、`astrbot_api.py`：AstrBot 连接器（适配层）。`core_link.py` 只负责按 `core_link.json` 找到核心并保持连接（重连、日志去重、版本不一致警告），不启动也不关闭核心。`@filter` 处理器只能放在 `main.py`（AstrBot 按定义模块绑定处理器），`terminate` 必须定义在 `STBridgePlugin` 类自己身上。适配层把 AstrBot 的 event 转成平台无关的消息，把核心产出的回复批转回 AstrBot 的消息链，并实现核心需要的平台能力（额外发送、主动发送、QQ 官方图片预上传、唤醒前缀、AstrBot 临时目录）。
- `st_bridge_core/`：平台无关的核心（桥接协议、排队、好感度、点歌、定时计划、TTS、控制台及其前端 `console_frontend/`、翻唱服务及 `cover_worker/`）。只依赖标准库、aiohttp、numpy、httpx（见 `st_bridge_core/requirements.txt`），不导入 astrbot / botpy；`tests/test_core_boundary.py` 用 ast 强制检查。核心用相对导入（AstrBot 重载只会清掉插件包名下的模块）。核心和连接器之间只传可序列化的数据（`platform_api.py` 里的 `IncomingMessage`、`Segment`、`ReplyBatch`）和字符串 id，通过本机 WebSocket 通信（`connector_protocol.py` 是协议和 RPC 层，`connector_hub.py` 是核心一侧，`connector_client.py` 是任何连接器都能用的客户端），平台能力由连接器实现 `PlatformAPI`。核心可以不依赖插件，用 `python -m st_bridge_core --data-dir <目录>` 单独运行。

## 测试

需要 Python 3.12 和 `st_bridge_core/requirements.txt` 里的依赖（aiohttp、numpy、httpx）。在插件目录（`astrbot_plugin_st_bridge/`）下运行：

```powershell
python -B -m unittest discover -s tests -v
```

测试用假的 AstrBot 模块，只启动临时的回环端口和临时数据目录，不连接 QQ、ST 或 GPT-SoVITS。发布包相关的端到端测试（`tests/test_release_e2e.py`）需要 `release/cache`（构建过一次发布包就有），没有时整组跳过。完整一遍要十几分钟，运行期间不要同时跑别的重负载，计时类用例会抖。

控制台前端和 ST 扩展的测试用 Node：在 `st_bridge_core/console_frontend` 里运行 `node --test tests/logic.test.mjs`，在 `st_bridge_client` 里运行 `node --test tests/bridge_client.test.mjs`（在本地磁盘上运行，不要在网络共享路径上运行）。

端到端测试（真实核心进程 + 假 ST 扩展 + 假聊天连接器 + 假 Irodori TTS，约 40 秒，需要 aiohttp / numpy / httpx）也可以单独运行：执行 `tests/run_e2e.py`（需要在仓库里运行，发布包里不带 tests）。它覆盖 10 个场景：私聊文字+语音+表情包、群聊前缀、排队与 @、好感度更新、点歌列表、定时消息、连接器断线重连、核心重启、ST 不在线，以及 NapCat（假 NapCat 客户端：握手被拒、私聊文字+语音+表情包、白名单群、定时消息、双向切换）。

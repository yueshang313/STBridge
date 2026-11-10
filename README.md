# STBridge

**English summary.** STBridge connects QQ (through NapCat or AstrBot) to [SillyTavern](https://github.com/SillyTavern/SillyTavern), so a SillyTavern character can chat with you and your friends on QQ, with optional voice replies, stickers, song covers (RVC), scheduled messages and a local web console. It runs on **Windows only**, ships as a ready-to-run zip with its own embedded Python, and is released under the **AGPL-3.0**. The documentation is written in Chinese; start with [docs/quickstart.md](docs/quickstart.md).

---

STBridge 把 SillyTavern（下面简称 ST）里的角色带到 QQ 上。你在 QQ 上给机器人发消息，STBridge 把消息转给 ST，由角色写好回复，再发回 QQ。

QQ 这一侧有两种接法，选一种就行：NapCat（用一个 QQ 小号当机器人），或者 AstrBot（QQ 官方机器人）。

默认角色是《学园偶像大师》里的藤田琴音。想换成别的角色，请看[换角色指南](docs/character.md)。

## 能做什么

- **私聊和群聊**：私聊只回主人。在群里，有人 @ 它或者叫它的名字，它才会回，而且分得清是谁在说话。
- **好感度**：每个 QQ 用户各有一份好感度，会随着聊天升降，角色对不同的人态度也不一样。
- **语音和表情包**：每条回复都带着情绪，语音的语气、发哪张表情包都跟着情绪走。语音支持 Irodori 和 GPT-SoVITS。
- **点歌翻唱**：在聊天里让角色唱首歌，用 RVC 唱成角色的声音。答不答应，看好感度和你定的规则。
- **定时消息**：早安、晚安、提醒吃饭喝水，到点由角色用自己的口吻发给你。
- **主动搭话**：你一段时间没说话，角色会主动来找你。每天有次数上限，也有免打扰时间。
- **排队**：好几个人同时说话时，按先来后到回复；同一个人在排队期间连发的几条，会合成一条处理。
- **网页控制台**：在浏览器里改几乎所有设置，不用碰配置文件。
- **总预设**：把整套设置存成预设，随时切换，也能导出给别人。

## 工作方式

```
  QQ ── NapCat（QQ 小号）──┐
                           ├─→  STBridge 核心  ←→  ST 扩展  ←→  SillyTavern  ←→  模型 API
  QQ ── AstrBot 插件 ──────┘     （start.bat）     （st_bridge_client）
                                     │
                                     ├─ 可选：Irodori TTS 或 GPT-SoVITS（语音）
                                     └─ 可选：RVC（翻唱）
```

- **STBridge 核心**：独立运行的主程序，自带 Python，双击 `start.bat` 就能启动。控制台也由它提供。
- **ST 扩展**：装在 ST 里，把 QQ 那边的消息交给 ST 生成回复，再把回复送回核心。
- **AstrBot 插件**：只有用 AstrBot 时才需要，负责把 AstrBot 收到的消息转给核心。

## 运行环境

- **只支持 Windows。**
- **SillyTavern**：需要你自己装好，配好模型 API 和角色。我们在 1.19.0 上测试过。
- **NapCat 或 AstrBot**：选一个，自己装好并登录。
- **语音和翻唱（可选）**：语音用 Irodori TTS 或 GPT-SoVITS，翻唱用 RVC。这些程序和模型都要你自己准备。
- **酒馆助手（可选）**：也叫 TavernHelper 或 JS-Slash-Runner。不装也能正常使用；装了以后，STBridge 会把好感度同步写进 ST 的 `stat_data` 变量，方便你在自己的世界书或 MVU 里使用。
- **模型 API**：在 ST 里配置。STBridge 本身不提供，也不转发任何模型服务。

## 下载和快速开始

1. 在本仓库的 Releases 页面下载最新的 `STBridge-<版本号>.zip`。
2. 解压后双击 `start.bat`，浏览器会自动打开控制台。
3. 接下来填路径、装 ST 扩展、连 QQ、设主人，跟着[快速开始](docs/quickstart.md)一步步做就行。

## 风险与声明

使用前，请花一分钟读完这几条：

- **私聊和群聊用的是同一个 ST 聊天。** 群友的话和你的私聊会进同一段聊天记录，所以角色有可能在群里说漏私聊里的事，比如提到你们私下的约定。STBridge 不会替你做这层隔离，需要你在 ST 的预设里自己加规则，比如“私下的事不要在群里提”。
- **NapCat 不是腾讯官方的客户端。** 它用的是第三方实现的协议，账号有被限制甚至封禁的风险。请用小号，别用你的主号。
- **聊天内容会发给你在 ST 里配置的模型服务商。** 会发哪些内容、对方怎么处理，取决于你用的是哪家服务商。
- **控制台只能在本机访问（127.0.0.1），而且没有密码。** 请不要用端口转发、内网穿透之类的方式把它暴露到公网上。
- **版权。** 这是一个爱好者项目，和角色、歌曲的版权方都没有关系。仓库里没有任何角色素材，角色卡、参考音频、表情包、RVC 和 LoRA 模型都需要你自己准备。默认角色藤田琴音出自《学园偶像大师》，版权归原权利人所有。翻唱和点歌生成的音频，版权责任由使用者自己承担。

## 文档

| 文档 | 写给谁 | 内容 |
| --- | --- | --- |
| [docs/quickstart.md](docs/quickstart.md) | 普通用户 | 从零开始，到在 QQ 上收到第一条回复 |
| [docs/character.md](docs/character.md) | 想换角色的用户 | 把琴音换成别的角色要改哪些地方 |
| [astrbot_plugin_st_bridge/README.md](astrbot_plugin_st_bridge/README.md) | 开发者 | 核心、通信协议、注入和各项功能的详细说明 |
| [release/README.md](release/README.md) | 开发者 | 怎么打发布包、怎么核对 |

## 开发者

仓库结构：

| 目录 | 说明 |
| --- | --- |
| `astrbot_plugin_st_bridge/` | AstrBot 插件（`main.py` 等适配层）、与平台无关的核心（`st_bridge_core/`，控制台前端在 `console_frontend/` 里），以及测试 `tests/` |
| `st_bridge_client/` | ST 扩展 |
| `release/` | 发布包的构建和核对脚本 |
| `docs/` | 用户文档 |
| `LICENSE`、`THIRD_PARTY_NOTICES.md` | 许可证和第三方组件说明 |

- **跑测试**：需要 Python 3.12，以及 `astrbot_plugin_st_bridge/st_bridge_core/requirements.txt` 里的依赖。在 `astrbot_plugin_st_bridge/` 目录下运行 `python -B -m unittest discover -s tests`。控制台前端和 ST 扩展的测试用 Node 跑，具体见[插件 README](astrbot_plugin_st_bridge/README.md) 的“测试”一节。
- **打发布包**：运行 `py -3.12 release/build_release.py`，说明见 [release/README.md](release/README.md)。
- **单独运行核心**：在 `st_bridge_core` 的上一级目录里运行 `python -m st_bridge_core --data-dir <数据目录>`。

## 许可

STBridge 以 **GNU AGPL-3.0** 许可发布，全文见 [LICENSE](LICENSE)。发布包里附带的第三方组件（嵌入版 Python 和各个依赖包）及其许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

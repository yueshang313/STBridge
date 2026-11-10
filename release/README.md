# 发布包构建（release/）

把核心、AstrBot 插件、ST 扩展和一个自带的嵌入版 Python 打成一个可以解压即用的文件夹和 zip。只支持 Windows。

## 用法

```
py -3.12 release/build_release.py            # 产出 release/dist/STBridge-<版本>/ 和同名 .zip
py -3.12 release/build_release.py --no-zip   # 只要文件夹
py -3.12 release/build_release.py --out D:\out --cache D:\cache
```

- 需要联网（第一次）：下载 Python 嵌入版 zip（校验 sha256）和依赖 wheel（`pip download`，校验 hash），都存在 `release/cache/`，之后再构建不会重复下载。
- 版本号 = `st_bridge_core/version.py` 里的 `CORE_VERSION`。
- `release/dist/` 和 `release/cache/` 不进 git。

## 产物结构

```
STBridge-<版本>\
  start.bat                          双击启动核心并打开控制台（纯 ASCII、CRLF，路径都是相对的）
  README.txt                         给用户的三步说明
  README.md、docs\                   项目 README 和用户文档（快速开始、换角色指南），从仓库根目录原样拷进来
  LICENSE、THIRD_PARTY_NOTICES.md    许可证和第三方组件说明
  python\                            Python 3.12.x 嵌入版 + 依赖（Lib\site-packages）
  core\st_bridge_core\               核心
  addons\astrbot_plugin_st_bridge\   AstrBot 插件（带一份 st_bridge_core 当库用）
  addons\st_bridge_client\           ST 扩展
```

`data\` 不在包里，核心第一次启动时创建（配置、令牌、日志、歌曲等都在那里）。包里没有 `tests`、`__pycache__`、`data`、`start_core.bat`。

## 嵌入版 Python 的注意点

嵌入版**忽略 `PYTHONPATH` 和当前目录**，只读 `python312._pth`。构建脚本把它改成：

```
python312.zip
.
Lib\site-packages
..\core
import site
```

所以 `start.bat` 不需要 `cd`，从哪里启动都能 `-m st_bridge_core`。

## 固定版本

- Python：`build_release.py` 里的 `PYTHON_VERSION`、`EMBED_SHA256`（sha256 在下载后核对过，md5 和 python.org 发布页一致）。
- 依赖：`requirements-lock.txt`，每个包都带 sha256，用 `pip download --require-hashes --no-deps` 取 `win_amd64` / `cp312` 的二进制 wheel，再直接解压进 `site-packages`（嵌入版没有 pip，也不需要）。直接依赖只有 `aiohttp`、`numpy`、`httpx`（`st_bridge_core/requirements.txt`）。更新依赖：改版本、重新下载、用 wheel 的文件名和 sha256 重写这个文件。

## 测试

`astrbot_plugin_st_bridge/tests/test_release.py` 用假的 zip / wheel 测装配逻辑（不联网）：目录结构、排除规则、`._pth`、`start.bat` 格式、zip 命名、下载校验与缓存、锁文件完整。

## 部署与更新

发布包的内容（`start.bat`、`README.txt`、`README.md`、`docs\`、`python\`、`core\`、`addons\`；运行后多出 `data\`）直接放在你选的文件夹里。以后更新 = 把新文件覆盖进去，**`data\` 不动**，然后在控制台“安装与路径”页重装插件和扩展。

## 核对：verify_release.py

```
py -3.12 release/verify_release.py <STbridge 路径> [--plugin-dir <已安装的插件目录>] [--extension-dir <已安装的扩展目录>]
```

用 HEAD 和缓存（不联网）重新组装一份，逐个文件比对 sha256，输出 `files N, diff N, extra [...]`（有差异时列出 changed / missing）。**`data\` 整个跳过，不列目录**；`__pycache__` 忽略；插件目录里不比 `core_link.json`（另外检查它有 `data_dir`）。退出码 0 = 一致，1 = 有差异，2 = 没有缓存。

## 发布包端到端测试

`astrbot_plugin_st_bridge/tests/test_release_e2e.py`：从缓存构建 → 解压到空目录 → 用真的 `start.bat`（包里的 Python）启动核心 → 控制台接口：保存路径、预览并导入一份假的旧 plugin_data、确认导入后核心自己重启（退出码 75，start.bat 再启动一次）、安装插件和 ST 扩展 → 用**装好的插件目录**里的 `core_link.py` 和 `st_bridge_core` 连接核心（不能有版本不一致的警告），经假 ST 收到一条私聊的回复 → 重装（有备份）、卸载 → 关闭核心（start.bat 退出码 0）。约 20 秒。需要 `release/cache`（构建过一次就有）；没有缓存时整组跳过。单独运行：`python -m unittest test_release_e2e`（在 tests 目录里）。


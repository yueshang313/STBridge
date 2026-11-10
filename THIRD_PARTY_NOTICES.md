# 第三方组件说明（Third-party notices）

STBridge 本身以 **GNU AGPL-3.0** 发布，全文见根目录 `LICENSE`。

发布包（`STBridge-<版本>\`）里还带了下面这些第三方组件。许可证名称取自各 wheel 的 `METADATA`
（`License-Expression` / `License` / `Classifier`），嵌入版 Python 取自它自带的 `LICENSE.txt`。
许可证全文的位置都相对于发布包根目录；Python 包的许可证文件在
`python\Lib\site-packages\<包名>-<版本>.dist-info\licenses\` 下，和包一起保留，没有删除。

| 组件 | 版本 | 许可证 | 许可证全文位置 |
| --- | --- | --- | --- |
| CPython（嵌入版，Windows amd64） | 3.12.10 | PSF-2.0（Python Software Foundation License Version 2） | `python\LICENSE.txt`（其中“Additional Conditions for this Windows binary build”列出了它捆绑的 OpenSSL、libffi、SQLite 等组件的条款） |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | `aiohappyeyeballs-2.7.1.dist-info\licenses\LICENSE` |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT（MIT 部分是它自带的 llhttp） | `aiohttp-3.14.5.dist-info\licenses\LICENSE.txt`、`...\vendor\llhttp\LICENSE` |
| aiosignal | 1.4.0 | Apache-2.0（METADATA 写作 “Apache 2.0”，Classifier 为 Apache Software License） | `aiosignal-1.4.0.dist-info\licenses\LICENSE` |
| anyio | 4.16.0 | MIT | `anyio-4.16.0.dist-info\licenses\LICENSE` |
| attrs | 26.1.0 | MIT | `attrs-26.1.0.dist-info\licenses\LICENSE` |
| certifi | 2026.10.23 | MPL-2.0 | `certifi-2026.10.23.dist-info\licenses\LICENSE` |
| frozenlist | 1.8.0 | Apache-2.0 | `frozenlist-1.8.0.dist-info\licenses\LICENSE` |
| h11 | 0.16.0 | MIT | `h11-0.16.0.dist-info\licenses\LICENSE.txt` |
| httpcore | 1.0.9 | BSD-3-Clause | `httpcore-1.0.9.dist-info\licenses\LICENSE.md` |
| httpx | 0.28.1 | BSD-3-Clause | `httpx-0.28.1.dist-info\licenses\LICENSE.md` |
| idna | 3.20 | BSD-3-Clause | `idna-3.20.dist-info\licenses\LICENSE.md` |
| multidict | 7.2.1 | Apache-2.0 | `multidict-7.2.1.dist-info\licenses\LICENSE` |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | `numpy-2.5.4.dist-info\licenses\LICENSE.txt`，以及同目录下 `numpy\...` 各子目录里它内置的第三方代码的许可证文件 |
| propcache | 0.5.4 | Apache-2.0 | `propcache-0.5.4.dist-info\licenses\LICENSE`、`...\NOTICE` |
| typing-extensions | 4.16.0 | PSF-2.0 | `typing_extensions-4.16.0.dist-info\licenses\LICENSE` |
| yarl | 1.25.1 | Apache-2.0 | `yarl-1.25.1.dist-info\licenses\LICENSE`、`...\NOTICE` |

说明：

- numpy 的 Windows wheel 还捆绑了 OpenBLAS（`numpy.libs\libscipy_openblas*.dll`，BSD-3-Clause）等动态库；
  它们的许可证写在 numpy 的 `LICENSE.txt` 末尾（“This binary distribution of NumPy also bundles the following software”）。
- 版本与 `release/requirements-lock.txt` 一致，由 `release/build_release.py` 按该文件下载并校验哈希后打进发布包。
- 发布包里的 `addons\` 是本项目自己的代码（AstrBot 插件、SillyTavern 扩展），同样适用 AGPL-3.0。
- GPT-SoVITS、Irodori-TTS、RVC、AstrBot、SillyTavern、NapCat 等外部程序不在发布包里，由使用者自行安装，各自适用自己的许可证。

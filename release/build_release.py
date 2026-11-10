"""Build the STBridge release folder (and its zip).

    py -3.12 release/build_release.py [--out release/dist] [--cache release/cache] [--no-zip]

Result: ``<out>/STBridge-<CORE_VERSION>/`` and ``<out>/STBridge-<CORE_VERSION>.zip``::

    start.bat                          starts the core and opens the console
    README.txt                         the three steps
    README.md, docs/                   the project README and the user documentation (Chinese)
    python/                            embeddable Python 3.12 with the core's dependencies in Lib/site-packages
    core/st_bridge_core/               the core
    addons/astrbot_plugin_st_bridge/   the AstrBot plugin (carries its own copy of the core as a library)
    addons/st_bridge_client/           the SillyTavern extension

``data/`` is not part of the package: the core creates it on its first start.

Only ``download`` / ``fetch_*`` touch the network; ``assemble`` works on files it is given (the tests feed it
fake archives).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "astrbot_plugin_st_bridge"
CLIENT_NAME = "st_bridge_client"

PYTHON_VERSION = "3.12.10"
EMBED_NAME = f"python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{EMBED_NAME}"
# sha256 of the file as downloaded 2026-11-06; its md5 (fe8ef205f2e9c3ba44d0cf9954e1abd3) matches the one on
# https://www.python.org/downloads/release/python-31210/
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

LOCK_FILE = Path(__file__).resolve().parent / "requirements-lock.txt"

# Directories and files that never go into the package.
SKIPPED_DIRS = {"__pycache__", "tests", "data", "node_modules", ".git"}
SKIPPED_FILES = {"start_core.bat"}
SKIPPED_SUFFIXES = {".pyc", ".pyo", ".log"}

# Files from the repo root that every release carries (the project license and the third-party notices).
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")

# The documentation every release carries: the repo's README.md and the docs/ folder (quickstart, character guide).
DOC_FILES = ("README.md",)
DOC_DIRS = ("docs",)

START_BAT = "\r\n".join(
    [
        "@echo off",
        "setlocal",
        "chcp 65001 >nul",
        "title STBridge core",
        'set "ROOT=%~dp0"',
        'set "OPEN=--open-console"',
        ":run",
        '"%ROOT%python\\python.exe" -X utf8 -m st_bridge_core --data-dir "%ROOT%data" --launched-by start.bat %OPEN%',
        'set "CODE=%ERRORLEVEL%"',
        # Exit code 75: the console asked for a restart. Start again (without opening the browser once more).
        'if "%CODE%"=="75" goto restart',
        "echo.",
        "echo STBridge core exited with code %CODE%.",
        "pause",
        "exit /b %CODE%",
        ":restart",
        'set "OPEN="',
        "echo.",
        "echo STBridge core is restarting...",
        "goto run",
        "",
    ]
)

README_TEXT = """\
STBridge {version}
==================

一、启动
  双击 start.bat。核心会启动并自动用浏览器打开控制台（http://127.0.0.1:11453/）。
  核心跑在 start.bat 打开的那个黑色窗口里，关掉窗口（或按 Ctrl+C）就是退出。
  数据（配置、令牌、日志、歌曲、表情包）都放在这个文件夹里的 data\\ 目录，首次启动时自动创建。

二、在控制台里填路径
  把你电脑上装好的东西的位置填进去（没有的留空即可）：
  SillyTavern（必须有）、GPT-SoVITS、Irodori、RVC、AstrBot、NapCat。

三、安装插件和扩展
  addons\\astrbot_plugin_st_bridge   AstrBot 插件（用 AstrBot 的才需要）
  addons\\st_bridge_client           SillyTavern 扩展
  在控制台里点安装，或者手动把这两个文件夹复制到对应位置：
    AstrBot：<AstrBot>\\data\\plugins\\
    SillyTavern：<SillyTavern>\\data\\<用户名>\\extensions\\
  AstrBot 插件只负责连接核心，不会启动它：先运行 start.bat，插件会自动连上。

说明
  python\\ 是自带的 Python {python_version}，和你电脑上其他 Python、AstrBot 都没有关系。
  只支持 Windows。
"""


# ---- versions ----------------------------------------------------------------------------------------
def read_core_version(repo: Path = REPO) -> str:
    text = (Path(repo) / PLUGIN_NAME / "st_bridge_core" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^CORE_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("CORE_VERSION not found in version.py")
    return match.group(1)


def release_name(version: str) -> str:
    return f"STBridge-{version}"


# ---- network (kept apart so the assembly can be tested without it) -----------------------------------
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as handle:  # noqa: S310 - fixed https URL
        shutil.copyfileobj(response, handle)
    partial.replace(destination)


def fetch_embedded_python(cache: Path, url: str = EMBED_URL, sha256: str = EMBED_SHA256) -> Path:
    """The embeddable Python zip in the cache, downloaded when missing; its sha256 must match."""
    path = Path(cache) / Path(url).name
    for attempt in (1, 2):
        if path.is_file() and sha256_of(path) == sha256:
            return path
        if attempt == 2:
            break
        path.unlink(missing_ok=True)
        print(f"downloading {url}")
        download(url, path)
    got = sha256_of(path) if path.is_file() else "nothing"
    raise RuntimeError(f"{path.name}: sha256 is {got}, expected {sha256}")


def fetch_wheels(cache: Path, lock: Path = LOCK_FILE) -> Path:
    """Download the pinned wheels (Windows, CPython 3.12, binaries only, hashes checked by pip) into the cache."""
    target = Path(cache) / "wheels"
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pip", "download",
        "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
        "--no-deps", "--require-hashes", "--no-input", "--disable-pip-version-check",
        "-r", str(lock), "-d", str(target),
    ]
    subprocess.run(command, check=True)
    return target


# ---- assembly ----------------------------------------------------------------------------------------
def _ignore(directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in SKIPPED_DIRS
        or name in SKIPPED_FILES
        or Path(name).suffix in SKIPPED_SUFFIXES
    }


def copy_tree(source: Path, destination: Path) -> None:
    """Copy a source folder without tests, caches, runtime data or start scripts."""
    shutil.copytree(source, destination, ignore=_ignore)


def patch_pth(python_dir: Path) -> Path:
    """Make the embedded Python find its libraries and the core by itself.

    The embeddable distribution ignores PYTHONPATH and the current directory and reads only ``python3xx._pth``
    (paths relative to that file). It is rewritten as: its own zip, the folder, ``Lib\\site-packages``, ``..\\core``,
    and ``import site`` switched on.
    """
    candidates = sorted(Path(python_dir).glob("python*._pth"))
    if len(candidates) != 1:
        raise RuntimeError(f"expected one python*._pth in {python_dir}, found {len(candidates)}")
    path = candidates[0]
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "import site":
            continue
        kept.append(stripped)
    for extra in (".", "Lib\\site-packages", "..\\core"):
        if extra not in kept:
            kept.append(extra)
    kept.append("import site")
    path.write_text("\r\n".join(kept) + "\r\n", encoding="utf-8", newline="")
    return path


def unpack_wheel(wheel: Path, site_packages: Path) -> None:
    """Unzip a wheel into site-packages (what pip would do for a pure or binary wheel without scripts)."""
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            parts = Path(name).parts
            if info.is_dir():
                continue
            if parts[0].endswith(".data"):
                if len(parts) > 2 and parts[1] in ("purelib", "platlib"):
                    relative = Path(*parts[2:])
                else:
                    raise RuntimeError(f"{wheel.name}: unsupported wheel data entry {name}")
            else:
                relative = Path(name)
            target = (site_packages / relative).resolve()
            if site_packages.resolve() not in target.parents:
                raise RuntimeError(f"{wheel.name}: unsafe path {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)


def write_text_file(path: Path, text: str, *, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def assemble(
    repo: Path,
    release_dir: Path,
    embedded_zip: Path,
    wheel_dir: Path,
    version: str,
    python_version: str = PYTHON_VERSION,
) -> Path:
    """Build the release folder from the repo, the Python zip and the wheels; returns ``release_dir``."""
    repo, release_dir = Path(repo), Path(release_dir)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)

    python_dir = release_dir / "python"
    with zipfile.ZipFile(embedded_zip) as archive:
        archive.extractall(python_dir)
    patch_pth(python_dir)
    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    wheels = sorted(Path(wheel_dir).glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"no wheels in {wheel_dir}")
    for wheel in wheels:
        unpack_wheel(wheel, site_packages)

    plugin = repo / PLUGIN_NAME
    copy_tree(plugin / "st_bridge_core", release_dir / "core" / "st_bridge_core")
    copy_tree(plugin, release_dir / "addons" / PLUGIN_NAME)
    copy_tree(repo / CLIENT_NAME, release_dir / "addons" / CLIENT_NAME)

    for name in LEGAL_FILES + DOC_FILES:
        if (repo / name).is_file():
            shutil.copyfile(repo / name, release_dir / name)
    for name in DOC_DIRS:
        if (repo / name).is_dir():
            copy_tree(repo / name, release_dir / name)

    # Plain ASCII, CRLF: a batch file with other characters shows garbage in the console.
    (release_dir / "start.bat").write_bytes(START_BAT.encode("ascii"))
    write_text_file(
        release_dir / "README.txt",
        README_TEXT.format(version=version, python_version=python_version).replace("\n", "\r\n"),
        bom=True,
    )
    return release_dir


def make_zip(release_dir: Path) -> Path:
    """``<release_dir>.zip`` holding the folder (its name is the top-level entry)."""
    release_dir = Path(release_dir)
    target = release_dir.parent / (release_dir.name + ".zip")  # not with_suffix: "STBridge-1.0.0" already has a ".0"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(release_dir.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(release_dir.name) / path.relative_to(release_dir)).as_posix())
    return target


def folder_size(path: Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the STBridge release folder and zip.")
    parser.add_argument("--out", type=Path, default=REPO / "release" / "dist")
    parser.add_argument("--cache", type=Path, default=REPO / "release" / "cache")
    parser.add_argument("--no-zip", action="store_true", help="only build the folder")
    args = parser.parse_args(argv)

    missing = [name for name in LEGAL_FILES + DOC_FILES if not (REPO / name).is_file()]
    missing += [name + "/" for name in DOC_DIRS if not (REPO / name).is_dir()]
    if missing:
        raise SystemExit(f"missing in the repo root, a release must carry them: {', '.join(missing)}")
    version = read_core_version()
    embedded = fetch_embedded_python(args.cache)
    wheels = fetch_wheels(args.cache)
    release_dir = assemble(REPO, args.out / release_name(version), embedded, wheels, version)
    print(f"folder: {release_dir} ({folder_size(release_dir) / 1e6:.1f} MB)")
    if not args.no_zip:
        archive = make_zip(release_dir)
        print(f"zip:    {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

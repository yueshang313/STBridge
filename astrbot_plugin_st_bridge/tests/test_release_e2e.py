"""R4c: the release package from scratch, the way the user will meet it.

    build (from the cache, no network) -> unzip into an empty folder -> start.bat (the real one, the package's own
    Python) -> console HTTP: paths, import preview + import of a fake old plugin_data, restart (exit code 75) ->
    install the plugin and the extension -> the INSTALLED plugin's core_link.py / st_bridge_core connect to the core
    and a private message gets a reply through a fake SillyTavern -> reinstall (backup) -> uninstall -> shutdown.

Needs the release cache (``release/cache``: the embeddable Python zip and the wheels, made by one run of
``release/build_release.py``; or the folder named in ``STBRIDGE_RELEASE_CACHE``). Without it the whole module is
skipped (it is part of the normal full run when the cache is there; alone: ``python -m unittest test_release_e2e``).
Windows only: it runs ``start.bat``. It never reads anything outside its own temporary folder.
"""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import re
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import aiohttp

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service import tree_digest
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION, compute_build_id

REPO = PLUGIN_ROOT.parent
SCRIPT = REPO / "release" / "build_release.py"
CACHE = Path(os.environ.get("STBRIDGE_RELEASE_CACHE") or REPO / "release" / "cache")
OWNER = ("qq-platform", "owner-1")
BILINGUAL = '☆‹「{jp}」›\n"{zh}"'
TIMINGS = []


def load_script():
    spec = importlib.util.spec_from_file_location("build_release_e2e", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_problem():
    if sys.platform != "win32":
        return "start.bat is a Windows batch file"
    if not SCRIPT.is_file():
        return "release/build_release.py is not in this tree"
    br = load_script()
    embedded = CACHE / br.EMBED_NAME
    if not embedded.is_file() or br.sha256_of(embedded) != br.EMBED_SHA256:
        return f"no release cache at {CACHE} (run release/build_release.py once)"
    if not list((CACHE / "wheels").glob("*.whl")):
        return f"no wheels in {CACHE / 'wheels'} (run release/build_release.py once)"
    return ""


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode() + (b"D" if path.is_dir() else path.read_bytes()))
    return digest.hexdigest()


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class FakeLogger:
    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


class LeanSt:
    """The ST extension's side of the bridge protocol (the same as the fake ST of test_e2e), for given ports."""

    def __init__(self, bridge_port, st_port):
        self.base = f"http://127.0.0.1:{bridge_port}"
        self.headers = {"Origin": f"http://localhost:{st_port}"}
        self.seen = []
        self.online = asyncio.Event()
        self._session = None
        self._task = None

    async def start(self):
        self._session = aiohttp.ClientSession(headers=self.headers)
        self._task = asyncio.create_task(self._loop())
        await asyncio.wait_for(self.online.wait(), 30)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session is not None:
            await self._session.close()

    async def _call(self, method, path, body=None):
        async with self._session.request(method, self.base + path, json=body) as response:
            if response.status == 204:
                return 204, None
            try:
                return response.status, await response.json(content_type=None)
            except Exception:
                return response.status, None

    async def _loop(self):
        while True:
            try:
                status, data = await self._call("GET", "/v1/requests/pending")
                if status == 503:
                    await self._call("POST", "/v1/session/ready", {"generating": False, "active_request": False})
                    continue
                self.online.set()
                if status != 200 or not data:
                    await asyncio.sleep(0.03)
                    continue
                request_id = data["request_id"]
                status, claim = await self._call("POST", f"/v1/requests/{request_id}/claim", {"claim_attempt_id": uuid.uuid4().hex})
                if status != 200:
                    continue
                self.seen.append(claim)
                await self._call("POST", f"/v1/requests/{request_id}/generation", {"state": "RUNNING"})
                await self._call("POST", f"/v1/requests/{request_id}/generation", {"state": "FINISHED"})
                await self._call("POST", f"/v1/requests/{request_id}/result", {"reply": BILINGUAL.format(jp="はい", zh="好的")})
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.05)


def timed(method):
    def wrapper(self):
        started = time.monotonic()
        try:
            return method(self)
        finally:
            TIMINGS.append((method.__name__, time.monotonic() - started))

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


@unittest.skipIf(bool(cache_problem()), cache_problem() or "")
class ReleaseFromScratchTests(unittest.TestCase):
    """Numbered steps that share one running core; each depends on the ones before it."""

    # ---- fixture ------------------------------------------------------------------------------------
    @classmethod
    def setUpClass(cls):
        started = time.monotonic()
        cls.br = load_script()
        cls.temp = tempfile.TemporaryDirectory(prefix="stbridge-r4c-")
        cls.base = Path(cls.temp.name)
        built = cls.base / "built" / f"STBridge-{CORE_VERSION}"
        cls.br.assemble(REPO, built, CACHE / cls.br.EMBED_NAME, CACHE / "wheels", CORE_VERSION)
        archive = cls.br.make_zip(built)
        cls.zip_name = archive.name
        extract = cls.base / "unzipped"
        extract.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extract)
        cls.root = extract / f"STBridge-{CORE_VERSION}"
        cls.data = cls.root / "data"
        cls.process = None
        cls.output = []
        names = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
        cls.ports = dict(zip(names, free_ports(len(names))))
        cls.make_environment()
        TIMINGS.append(("setUpClass (build + zip + unzip + fake machine)", time.monotonic() - started))

    @classmethod
    def tearDownClass(cls):
        if cls.process is not None and cls.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(cls.process.pid), "/T", "/F"], capture_output=True)
        for name in [key for key in sys.modules if key.startswith("r4c_installed_plugin")]:
            del sys.modules[name]
        cls.temp.cleanup()
        print("\nrelease e2e timings:")
        for name, seconds in TIMINGS:
            print(f"  {seconds:6.1f} s  {name}")

    @classmethod
    def make_environment(cls):
        """A fake machine: ST (launcher layout), an AstrBot instance with an old plugin_data, a browser that does nothing."""
        machine = cls.base / "machine"
        cls.st_root = machine / "SillyTavern Launcher GUI"
        (cls.st_root / "data" / "st_data" / "default-user" / "extensions").mkdir(parents=True)
        (cls.st_root / "data" / "sillytavern").mkdir(parents=True)
        cls.astrbot = machine / "AstrBot" / "instances" / "227764de-9e52-44de-a90c-e83e18ac6f4a"
        cls.plugins = cls.astrbot / "core" / "data" / "plugins"
        cls.plugins.mkdir(parents=True)
        cls.old = cls.astrbot / "core" / "data" / "plugin_data" / "astrbot_plugin_st_bridge"
        audio = touch(cls.old / "audio" / "kotone" / "happy.wav", "RIFF-old-wav")
        config = default_config()
        config["network"] = dict(cls.ports)
        config["tts"]["common"].update(auto_start=False, prob=0.0)
        config["stickers"].update(enabled=False)
        config["owner"] = {"platform_id": OWNER[0], "sender_id": OWNER[1], "imported": True}
        config["default_character"] = "琴音"
        config["characters"] = {"琴音": {"nickname": "琴音", "st_name": "藤田琴音", "emotions": {"happy": {"ref_audio_path": str(audio)}}}}
        config["models"] = {"kotone": {"reference_audio_root": str(cls.old / "audio")}}
        config["tts"]["irodori"].update(launch_dir=r"D:\Irodori-TTS", server_dir=r"D:\Irodori-TTS\Irodori-TTS-Server", lora_adapter=r"D:\Irodori-TTS\lora")
        config["tts"]["gpt_sovits"]["ref_audio_path"] = str(audio).replace("\\", "/")
        config["cover"]["rvc_root"] = r"E:\Tools\RVC"
        touch(cls.old / "config.json", json.dumps(config, ensure_ascii=False, indent=2))
        touch(cls.old / "connector_token", "old-connector-token-0123456789\n")
        touch(cls.old / "onebot_token", "old-onebot-token-0123456789\n")
        AffinityStore(cls.old).set_value(f"{OWNER[0]}:{OWNER[1]}", 66, "主人")
        touch(cls.old / "logs" / "bridge.log", "old log")
        touch(cls.old / "temp_audio" / "t.wav", "temp")
        cls.old_hash = tree_hash(cls.old)
        # The package starts with a data folder that has nothing but the settings of this test run (its ports).
        store = ConfigStore(cls.data)
        store.config["network"] = dict(cls.ports)
        store.config["tts"]["common"].update(auto_start=False, prob=0.0)
        (cls.data / "config.json").write_text(json.dumps(store.config, ensure_ascii=False, indent=2), encoding="utf-8")
        for stray in cls.data.iterdir():
            if stray.name != "config.json":
                import shutil

                shutil.rmtree(stray) if stray.is_dir() else stray.unlink()
        cls.browser_marker = cls.base / "browser-opened.txt"
        browser = cls.base / "nobrowser.bat"
        browser.write_bytes(f'@echo off\r\necho %* >> "{cls.browser_marker}"\r\n'.encode("ascii"))
        cls.browser = browser

    # ---- helpers ------------------------------------------------------------------------------------
    def start_bat(self):
        env = dict(os.environ)
        env["BROWSER"] = str(self.browser)  # --open-console must not open a real window here
        env.pop("PYTHONPATH", None)
        type(self).process = subprocess.Popen(
            ["cmd", "/c", str(self.root / "start.bat")], cwd=str(self.root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        process = self.process

        def read():
            for raw in process.stdout:
                type(self).output.append(raw.decode("utf-8", errors="replace").rstrip())

        threading.Thread(target=read, daemon=True).start()

    def logged(self, text):
        return sum(text in line for line in type(self).output)

    def wait_for(self, condition, timeout=90, what="the condition"):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"{what} was not reached in time; core output:\n" + "\n".join(type(self).output[-25:]))
            time.sleep(0.1)

    def api(self, method, path, body=None, expect=200):
        url = f"http://127.0.0.1:{self.ports['console_port']}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, payload = response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            status, payload = error.code, json.loads(error.read().decode("utf-8") or "{}")
        self.assertEqual(status, expect, f"{method} {path}: {payload}")
        return payload

    def healthy(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.ports['console_port']}/api/health", timeout=2) as response:
                return response.status == 200
        except OSError:
            return False

    # ---- 1. the package -----------------------------------------------------------------------------
    @timed
    def test_01_the_unzipped_package_is_what_the_user_gets(self):
        self.assertTrue(self.zip_name.startswith("STBridge-") and self.zip_name.endswith(".zip"))
        for name in ("start.bat", "README.txt", "python/python.exe", "python/python312._pth", "core/st_bridge_core/__main__.py",
                     "addons/astrbot_plugin_st_bridge/main.py", "addons/astrbot_plugin_st_bridge/metadata.yaml",
                     "addons/st_bridge_client/manifest.json"):
            self.assertTrue((self.root / name).is_file(), name)
        for folder in ("core", "addons"):
            self.assertFalse(list((self.root / folder).rglob("tests")), folder)  # (other people's packages keep theirs)
        self.assertFalse((self.root / "addons" / "astrbot_plugin_st_bridge" / "start_core.bat").exists())
        self.assertEqual(
            compute_build_id(self.root / "core" / "st_bridge_core"),
            compute_build_id(self.root / "addons" / "astrbot_plugin_st_bridge" / "st_bridge_core"),
        )
        self.assertEqual({path.name for path in self.data.iterdir()}, {"config.json"})

    def test_01b_the_package_carries_its_license_and_every_locked_packages_license_files(self):
        license_text = (self.root / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3", license_text)
        self.assertTrue((self.root / "THIRD_PARTY_NOTICES.md").is_file())
        self.assertIn("docs/quickstart.md", (self.root / "README.md").read_text(encoding="utf-8"))
        for name in ("docs/quickstart.md", "docs/character.md"):
            self.assertTrue((self.root / name).is_file(), name)
        self.assertTrue((self.root / "python" / "LICENSE.txt").is_file())  # CPython's own license, from the embedded zip
        site = self.root / "python" / "Lib" / "site-packages"
        lock = (REPO / "release" / "requirements-lock.txt").read_text(encoding="utf-8")
        pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock, re.MULTILINE)
        self.assertEqual(len(pins), 16)
        for name, version in pins:
            folder = site / f"{name.replace('-', '_')}-{version}.dist-info"
            self.assertTrue(folder.is_dir(), f"no dist-info for {name} {version}")
            kept = [path for path in folder.rglob("*") if path.is_file() and any(
                word in path.name.upper() for word in ("LICENSE", "COPYING", "NOTICE"))]
            self.assertTrue(kept, f"{name} {version}: no license file kept in {folder.name}")  # (no known exceptions today)

    # ---- 2. start.bat -------------------------------------------------------------------------------
    @timed
    def test_02_start_bat_starts_the_core_with_the_packages_python(self):
        self.start_bat()
        self.wait_for(self.healthy, what="the console")
        self.wait_for(lambda: self.logged("[STBridge] Core started"), 30, "the start line")
        line = next(item for item in type(self).output if "Core started" in item)
        self.assertIn("launched_by=start.bat", line)
        self.assertIn(f"connector_port={self.ports['connector_port']}", line)
        names = {path.name for path in self.data.iterdir()}
        self.assertTrue({"config.json", "connector_token", "onebot_token", "logs"} <= names, names)
        self.assertTrue((self.data / "logs" / "bridge.log").is_file())
        overview = self.api("GET", "/api/install")
        self.assertEqual(Path(overview["data_dir"]), self.data.resolve())
        self.assertTrue(overview["package"]["available"])  # a release layout: the install buttons work
        self.wait_for(self.browser_marker.is_file, 10, "the browser being asked to open the console")
        opened = self.browser_marker.read_text(encoding="utf-8", errors="replace")
        self.assertIn(f"http://127.0.0.1:{self.ports['console_port']}/", opened)

    # ---- 3. paths -----------------------------------------------------------------------------------
    @timed
    def test_03_paths_are_saved_and_checked(self):
        body = self.api("POST", "/api/install/paths", {"st_dir": str(self.st_root), "astrbot_dir": str(self.astrbot)})
        self.assertEqual(body["checks"]["st"]["state"], "valid")
        self.assertTrue(body["checks"]["st"]["extensions_dir"].endswith(r"data\st_data\default-user\extensions"), body["checks"]["st"])
        self.assertEqual(body["checks"]["astrbot"]["plugins_dir"], str(self.plugins))
        self.assertEqual(Path(body["import_source"]), self.old)
        self.assertEqual(body["plugin"]["state"], "missing")
        self.assertEqual(body["extension"]["state"], "missing")

    # ---- 4. import ----------------------------------------------------------------------------------
    @timed
    def test_04_the_old_plugin_data_is_previewed_and_imported_read_only(self):
        preview = self.api("POST", "/api/install/import/preview", {"source": str(self.old)})
        self.assertEqual({item["name"] for item in preview["items"]}, {"config.json", "affinity.json", "affinity.json.bak", "connector_token", "onebot_token", "audio"})
        self.assertEqual(set(preview["skipped"]), {"logs", "temp_audio"})
        self.assertGreaterEqual(preview["path_references"], 3)
        self.assertTrue({"config.json", "connector_token", "onebot_token"} <= set(preview["replaces"]), preview["replaces"])
        self.api("POST", "/api/install/import", {"source": str(self.old)}, expect=400)  # not confirmed
        result = self.api("POST", "/api/install/import", {"source": str(self.old), "confirm": True})
        self.assertTrue(result["restart_required"] and result["restart_available"] and result["kept_install"])
        self.assertTrue(result["restarting"] and result["auto"])  # the core restarts by itself (start.bat starts it again)
        self.assertEqual(result["rewritten_paths"], preview["path_references"])
        self.assertEqual((self.data / "connector_token").read_text(), "old-connector-token-0123456789\n")
        self.assertEqual((self.data / "onebot_token").read_text(), "old-onebot-token-0123456789\n")
        self.assertEqual((self.data / "audio" / "kotone" / "happy.wav").read_text(), "RIFF-old-wav")
        self.assertTrue((self.data / "affinity.json").is_file())
        self.assertFalse((self.data / "logs" / "bridge.log").read_text(encoding="utf-8").startswith("old log"))
        self.assertFalse((self.data / "temp_audio").exists())
        config = json.loads((self.data / "config.json").read_text(encoding="utf-8"))
        new_audio = self.data / "audio" / "kotone" / "happy.wav"
        self.assertEqual(config["characters"]["琴音"]["emotions"]["happy"]["ref_audio_path"], str(new_audio))
        self.assertEqual(config["models"]["kotone"]["reference_audio_root"], str(self.data / "audio"))
        self.assertEqual(Path(config["tts"]["gpt_sovits"]["ref_audio_path"]), new_audio)
        self.assertEqual(config["tts"]["irodori"]["launch_dir"], r"D:\Irodori-TTS")  # the old settings came along
        self.assertEqual(config["cover"]["rvc_root"], r"E:\Tools\RVC")
        self.assertEqual((config["install"]["st_dir"], config["install"]["astrbot_dir"]), (str(self.st_root), str(self.astrbot)))
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.data / "backups")
        self.assertTrue((backup / "config.json").is_file())
        self.assertEqual(tree_hash(self.old), self.old_hash)  # the old plugin_data is untouched

    # ---- 5. restart ---------------------------------------------------------------------------------
    @timed
    def test_05_the_import_restarted_the_core_by_itself_and_start_bat_ran_it_again(self):
        # No button was pressed: confirming the import was enough (exit code 75, start.bat loops).
        self.wait_for(lambda: self.logged("[STBridge] Core started") == 2 and self.healthy(), 120, "the second start")
        self.assertTrue(self.logged("Restart requested from the console") >= 1)
        self.assertTrue(self.logged("restarting") >= 1)
        self.assertIsNone(self.process.poll())  # the same start.bat is still running
        self.assertEqual(len(self.browser_marker.read_text(encoding="utf-8", errors="replace").splitlines()), 1)  # the browser opened once
        overview = self.api("GET", "/api/install")
        self.assertEqual(overview["settings"]["st_dir"], str(self.st_root))
        self.assertEqual(overview["settings"]["irodori_dir"], r"D:\Irodori-TTS")
        self.assertEqual(overview["settings"]["rvc_dir"], r"E:\Tools\RVC")
        status = self.api("GET", "/api/status")
        self.assertEqual(status["platform"], "astrbot")

    # ---- 6. install plugin and extension --------------------------------------------------------------
    @timed
    def test_06_the_plugin_and_the_extension_are_installed_from_the_package(self):
        plugin = self.api("POST", "/api/install/plugin", {"action": "install"})
        target = self.plugins / "astrbot_plugin_st_bridge"
        self.assertEqual(Path(plugin["target"]), target)
        self.assertEqual(json.loads((target / "core_link.json").read_text(encoding="utf-8")), {"data_dir": str(self.data.resolve())})
        self.assertEqual(tree_digest(target), tree_digest(self.root / "addons" / "astrbot_plugin_st_bridge"))
        self.assertEqual(compute_build_id(target / "st_bridge_core"), compute_build_id(self.root / "core" / "st_bridge_core"))
        self.assertEqual(plugin["overview"]["plugin"]["state"], "installed")
        extension = self.api("POST", "/api/install/st-extension", {"action": "install"})
        ext_target = self.st_root / "data" / "st_data" / "default-user" / "extensions" / "st_bridge_client"
        self.assertEqual(Path(extension["target"]), ext_target)
        self.assertEqual(tree_digest(ext_target), tree_digest(self.root / "addons" / "st_bridge_client"))
        self.assertEqual(extension["overview"]["extension"]["state"], "installed")
        self.assertIsNone(plugin["backup"] or extension["backup"])  # nothing was there before
        self.assertEqual(tree_hash(self.old), self.old_hash)

    # ---- 7. the installed plugin talks to the core ------------------------------------------------------
    @timed
    def test_07_the_installed_plugin_connects_without_a_version_warning_and_a_message_gets_a_reply(self):
        installed = self.plugins / "astrbot_plugin_st_bridge"
        name = "r4c_installed_plugin"
        package = types.ModuleType(name)
        package.__path__ = [str(installed)]
        sys.modules[name] = package
        link_module = importlib.import_module(f"{name}.core_link")
        platform_api = importlib.import_module(f"{name}.st_bridge_core.platform_api")
        protocol = importlib.import_module(f"{name}.st_bridge_core.connector_protocol")
        self.assertEqual(Path(link_module.__file__).parent, installed)

        class Platform(platform_api.PlatformAPI):
            async def send(self, ref, segments):
                pass

            async def send_to(self, conversation_id, segments):
                return True

            def wake_prefixes(self):
                return ["/"]

        async def scenario():
            logger = FakeLogger()
            link = link_module.CoreLink(
                platform=Platform(), plugin_dir=installed, logger=logger,
                capabilities=lambda: {"wake_prefixes": ["/"], "extra_temp_dir": None}, import_owner=lambda: None,
                retry_delays=(0.1, 0.3),
            )
            st = LeanSt(self.ports["bridge_port"], self.ports["st_port"])
            await link.start()
            try:
                self.assertTrue(await link.client.wait_connected(60), logger.warnings)
                hello = link.client.last_hello
                self.assertEqual(hello["core_version"], CORE_VERSION)
                self.assertEqual(hello["launched_by"], "start.bat")
                self.assertEqual(hello["build_id"], compute_build_id(installed / "st_bridge_core"))
                self.assertEqual(hello["build_id"], compute_build_id(self.root / "core" / "st_bridge_core"))
                self.assertEqual([line for line in logger.warnings if "版本不一致" in line], [])
                self.assertEqual(logger.errors, [])
                await st.start()
                message = platform_api.IncomingMessage(
                    platform="e2e", platform_id=OWNER[0], chat_type=platform_api.PRIVATE, session_id="session-owner",
                    conversation_id="conv-owner", sender_id=OWNER[1], sender_name="主人", group_id=None,
                    text="你好", message_str="你好", ref="ref-1", command=None, wake_prefixes=["/"],
                )
                body = {"message": protocol.message_to_dict(message)}
                self.assertTrue(await link.client.call("accept_private", body))
                batches = [protocol.batch_from_dict(item) async for item in link.client.stream("handle_private", body)]
                self.assertTrue(batches)
                text = " ".join(str(segment) for batch in batches for segment in batch.segments)
                self.assertIn("好的", text)
                self.assertEqual(len(st.seen), 1)
                self.assertIn("你好", json.dumps(st.seen[0], ensure_ascii=False))
                # the console sees the installed plugin as the connected connector
                connector = self.api("GET", "/api/status")["connector"]
                self.assertTrue(connector["connected"])
                self.assertEqual(self.api("GET", "/api/install")["plugin"]["state"], "installed")
            finally:
                await st.stop()
                await link.stop()
            return logger

        logger = asyncio.run(scenario())
        self.assertEqual([line for line in logger.warnings if "版本不一致" in line], [])

    # ---- 8. reinstall and uninstall -------------------------------------------------------------------
    @timed
    def test_08_reinstall_makes_a_backup_and_uninstall_leaves_the_folders_clean(self):
        target = self.plugins / "astrbot_plugin_st_bridge"
        ext_target = self.st_root / "data" / "st_data" / "default-user" / "extensions" / "st_bridge_client"
        neighbour = touch(self.plugins / "other_plugin" / "main.py")
        (target / "main.py").write_text("# edited by hand", encoding="utf-8")
        again = self.api("POST", "/api/install/plugin", {"action": "install"})
        backup = Path(again["backup"])
        self.assertTrue(backup.name.startswith("astrbot-plugin-"))
        self.assertEqual((backup / "main.py").read_text(encoding="utf-8"), "# edited by hand")
        self.assertTrue((backup / "core_link.json").is_file())
        self.assertNotEqual((target / "main.py").read_text(encoding="utf-8"), "# edited by hand")
        self.api("POST", "/api/install/st-extension", {"action": "install"})
        removed = self.api("POST", "/api/install/plugin", {"action": "uninstall"})
        removed_ext = self.api("POST", "/api/install/st-extension", {"action": "uninstall"})
        self.assertFalse(target.exists())
        self.assertFalse(ext_target.exists())
        self.assertTrue(neighbour.is_file())
        self.assertEqual(self.api("GET", "/api/install")["plugin"]["state"], "missing")
        backups = sorted(path.name for path in (self.data / "backups").iterdir())
        self.assertTrue(Path(removed["backup"]).is_dir() and Path(removed_ext["backup"]).is_dir())
        self.assertEqual(sum(name.startswith("astrbot-plugin-") for name in backups), 2, backups)
        self.assertEqual(sum(name.startswith("st-extension-") for name in backups), 2, backups)
        self.assertEqual(sum(name.startswith("pre-import-") for name in backups), 1, backups)
        self.assertEqual(tree_hash(self.old), self.old_hash)

    # ---- 9. shutdown --------------------------------------------------------------------------------
    @timed
    def test_09_a_normal_shutdown_ends_start_bat_with_code_0(self):
        sys.path.insert(0, str(self.root / "core"))
        try:
            from st_bridge_core.connector_client import ConnectorClient  # the package's copy of the core
            from st_bridge_core.connector_ws import connect_websocket, read_token
            from st_bridge_core.platform_api import PlatformAPI
        finally:
            sys.path.remove(str(self.root / "core"))
            for key in [name for name in sys.modules if name == "st_bridge_core" or name.startswith("st_bridge_core.")]:
                del sys.modules[key]

        async def shutdown():
            client = ConnectorClient(
                name="e2e-shutdown", version=CORE_VERSION, platform=PlatformAPI(),
                connect=lambda: connect_websocket(self.ports["connector_port"]), token=lambda: read_token(self.data),
            )
            self.assertTrue(await client.connect_once())
            try:
                await client.call("shutdown", {})
            finally:
                await client.stop()

        asyncio.run(shutdown())
        self.wait_for(lambda: self.process.poll() is not None, 60, "start.bat ending")
        self.assertEqual(self.process.returncode, 0)
        self.assertTrue(self.logged("[STBridge] Core stopped") >= 2)
        self.assertTrue(self.logged("exited with code 0") >= 1)
        self.assertFalse(port_open(self.ports["console_port"]))


if __name__ == "__main__":
    unittest.main()

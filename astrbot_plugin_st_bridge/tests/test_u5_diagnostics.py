"""U5-2: the diagnostics zip (redaction) and the install backups (keep the newest few, touch nothing else)."""

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.diagnostics import (
    HIDDEN,
    build_diagnostics_zip,
    environment_values,
    redact_config,
    scrub_text,
    sensitive_values,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, default_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service import InstallError, InstallService, check_astrbot
from test_console_server import _unused_loopback_port

# Everything below must never be found anywhere in the zip.
SECRETS = {
    "key": "sk-FAKESECRETKEY-abc123XYZ",
    "token": "tok-FAKE-ONEBOT-TOKEN-777",
    "rvc": "D:\\FakeSecret\\RVC-Folder",
    "st": "E:\\FakePrivate\\SillyTavern",
    "astrbot": "F:\\FakePrivate\\AstrBot",
    "irodori": "G:\\FakeVoice\\Irodori-TTS",
    "audio": "H:\\FakeAudio\\ref\\voice.wav",
    "qq": "123456789",
    "platform_id": "FAKE_PLATFORM_ID_5566",
    "sender_id": "FAKEOPENID0123456789ABCDEF",
    "group_a": "30001777",
    "group_b": "30002888",
    "nickname_secret": "秘密的昵称不能出现",
}


def fake_config():
    config = default_config()
    config["owner"] = {"platform_id": SECRETS["platform_id"], "sender_id": SECRETS["sender_id"], "imported": True}
    config["chat_platform"] = {"active": "napcat", "napcat_owner": SECRETS["qq"], "group_whitelist": [SECRETS["group_a"], SECRETS["group_b"]]}
    config["cover"]["music_api_key"] = SECRETS["key"]
    config["cover"]["rvc_root"] = SECRETS["rvc"]
    config["install"]["st_dir"] = SECRETS["st"]
    config["install"]["astrbot_dir"] = SECRETS["astrbot"]
    config["tts"]["irodori"]["launch_dir"] = SECRETS["irodori"]
    config["tts"]["irodori"]["token"] = SECRETS["token"]
    config["texts"]["queue_full"] = SECRETS["nickname_secret"]
    name = next(iter(config["characters"]))
    config["characters"][name]["emotions"]["开心"]["ref_audio_path"] = SECRETS["audio"]
    config["schedule"]["groups"] = {SECRETS["group_a"]: {"alias": "小群", "last_seen": "x"}}
    return config


def zip_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return "\n".join(archive.read(name).decode("utf-8", errors="replace") for name in archive.namelist())


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "bridge.log").write_text(
            f"2026-11-12 10:00:00 INFO [STBridge] 启动 Irodori 服务 dir={SECRETS['irodori']} port=8088\n"
            f"2026-11-12 10:00:01 WARNING [STBridge] owner {SECRETS['sender_id']} in group {SECRETS['group_a']}\n"
            f"2026-11-12 10:00:02 INFO [STBridge] path {SECRETS['st'].replace(chr(92), '/')}/data\n",
            encoding="utf-8",
        )
        (logs / "bridge.log.2026-11-11").write_text("2026-11-11 09:00:00 INFO [STBridge] ok\n", encoding="utf-8")
        (self.root / "affinity.json").write_text(json.dumps({"version": 3, "users": {SECRETS["qq"]: {"value": 9}}}), encoding="utf-8")
        (self.root / "config.json").write_text("{}", encoding="utf-8")

    def build(self, **kwargs):
        config = fake_config()
        status = {
            "bridge": {"gate": "RECOVERED", "last_poll_age": 1.2},
            "onebot": {"connected": True, "self_id": SECRETS["qq"], "since": "x"},
            "replies": {"private": 3, "group": 4},
            "platform": "napcat",
        }
        services = {
            "语音": {"state": "stopped", "engine": "irodori", "message": f"未设置 {SECRETS['irodori']}", "launch_dir": SECRETS["irodori"], "log_tail": [f"line {SECRETS['sender_id']}"]},
            "翻唱": {"state": "running", "launch_dir": SECRETS["rvc"]},
        }
        install = {"checks": {"st": {"state": "valid", "message": SECRETS["st"]}}, "extension": {"state": "installed", "message": SECRETS["st"]}, "plugin": {"state": "missing"}}
        return build_diagnostics_zip(data_dir=self.root, config=config, status=status, services=services, install=install, now=datetime(2026, 11, 12, 10, 30, 0), **kwargs)

    def test_no_secret_path_or_id_is_anywhere_in_the_zip(self):
        text = zip_text(self.build())
        for name, value in SECRETS.items():
            with self.subTest(name=name):
                self.assertNotIn(value, text)
                self.assertNotIn(value.replace("\\", "/"), text)
                self.assertNotIn(value.replace("\\", "\\\\"), text)

    def test_the_zip_has_the_promised_files_and_nothing_else(self):
        with zipfile.ZipFile(io.BytesIO(self.build())) as archive:
            names = sorted(archive.namelist())
        self.assertEqual(names, sorted(["说明.txt", "版本信息.json", "状态快照.json", "服务状态.json", "配置摘要.json", "日志/bridge.log", "日志/bridge.log.2026-11-11"]))
        # neither the affinity data nor the config file of the data directory
        self.assertFalse(any("affinity" in name or name == "config.json" for name in names))

    def test_the_readme_says_when_and_what(self):
        with zipfile.ZipFile(io.BytesIO(self.build())) as archive:
            readme = archive.read("说明.txt").decode("utf-8")
        self.assertIn("2026-11-12 10:30:00", readme)
        self.assertIn("里面有什么", readme)
        self.assertIn("里面没有什么", readme)
        self.assertIn("聊天内容", readme)

    def test_the_logs_stay_readable_and_only_the_sensitive_parts_are_hidden(self):
        with zipfile.ZipFile(io.BytesIO(self.build())) as archive:
            log = archive.read("日志/bridge.log").decode("utf-8")
            old = archive.read("日志/bridge.log.2026-11-11").decode("utf-8")
        self.assertIn("启动 Irodori 服务 dir=" + HIDDEN + " port=8088", log)
        self.assertIn("WARNING [STBridge] owner", log)
        self.assertEqual(old, "2026-11-11 09:00:00 INFO [STBridge] ok\n")

    def test_the_summary_keeps_numbers_flags_and_kinds_and_states_what_is_set(self):
        summary = redact_config(fake_config())
        self.assertEqual(summary["chat_platform"]["active"], "napcat")
        self.assertEqual(summary["chat_platform"]["group_whitelist"], {"数量": 2})
        self.assertEqual(summary["chat_platform"]["napcat_owner"], "已设置")
        self.assertEqual(summary["owner"]["platform_id"], "已设置")
        self.assertEqual(summary["cover"]["music_api_key"], "已设置")
        self.assertEqual(summary["cover"]["rvc_root"], "已设置")
        self.assertEqual(summary["install"]["astrbot_dir"], "已设置")
        self.assertEqual(summary["tts"]["engine"], "irodori")
        self.assertEqual(summary["queue"]["max_waiting"], 10)
        self.assertEqual(summary["tts"]["common"]["prob"], 100.0)
        self.assertIs(summary["tts"]["common"]["split_messages"], True)
        self.assertEqual(summary["characters"], {"数量": 1})
        self.assertEqual(summary["schedule"]["groups"], {"数量": 1})
        empty = default_config()
        self.assertEqual(redact_config(empty)["owner"]["sender_id"], "未设置")

    def test_the_status_snapshot_drops_the_napcat_account_but_keeps_the_rest(self):
        with zipfile.ZipFile(io.BytesIO(self.build())) as archive:
            status = json.loads(archive.read("状态快照.json"))
            services = json.loads(archive.read("服务状态.json"))
        self.assertNotIn("self_id", status["onebot"])
        self.assertEqual(status["onebot"]["connected"], True)
        self.assertEqual(status["replies"], {"private": 3, "group": 4})
        self.assertEqual(services["语音"]["state"], "stopped")
        self.assertNotIn("launch_dir", services["语音"])
        self.assertNotIn("log_tail", services["语音"])
        self.assertEqual(services["安装页"], {"路径检查": {"st": "valid"}, "ST 扩展": "installed", "AstrBot 插件": "missing"})

    def test_user_names_and_the_computer_name_are_gone_even_when_they_are_not_in_the_settings(self):
        """A stack trace, the core's own folder, the data folder, the Python path: all carry the Windows user name."""
        import os
        from unittest.mock import patch as mock_patch

        env = {"COMPUTERNAME": "FAKEPC-HOST-99", "USERNAME": "fakeuser_zhang", "USERPROFILE": "C:\\Users\\fakeuser_zhang"}
        trace = (
            "Traceback (most recent call last):\n"
            '  File "C:\\Users\\假名Zhang\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\x.py", line 3, in run\n'
            '  File "c:/users/假名Zhang/AppData/Roaming/y.py", line 9\n'
            '  File "D:\\\\Users\\\\jsonescaped_name\\\\z.py"\n'
            "  at /home/linuxfake/app/main.py and /Users/macfake/Library\n"
            "RuntimeError: host FAKEPC-HOST-99 refused for fakeuser_zhang\n"
        )
        data_dir_line = f"data dir is {self.root} here\n"
        (self.root / "logs" / "bridge.log").write_text(
            "2026-11-12 10:00:00,100 ERROR [STBridge] boom\n" + trace + data_dir_line, encoding="utf-8"
        )
        status = {"bridge": {"note": 'C:\\Users\\假名Zhang\\AppData\\status.py'}, "replies": {"private": 1}}
        services = {"语音": {"state": "failed", "message": 'launch failed in C:\\Users\\假名Zhang\\Irodori\\run.py on FAKEPC-HOST-99'}}
        fake_home = Path("Z:/Users/fakehome_name")
        with mock_patch.dict(os.environ, env), mock_patch.object(Path, "home", return_value=fake_home):
            data = build_diagnostics_zip(data_dir=self.root, config=fake_config(), status=status, services=services, install=None, now=datetime(2026, 11, 12))
        text = zip_text(data)
        for needle in ("假名Zhang", "FAKEPC-HOST-99", "fakeuser_zhang", "jsonescaped_name", "linuxfake", "macfake", "fakehome_name", str(self.root)):
            self.assertNotIn(needle, text, needle)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            log = archive.read("日志/bridge.log").decode("utf-8")
            status_json = archive.read("状态快照.json").decode("utf-8")
            services_json = archive.read("服务状态.json").decode("utf-8")
            versions_json = archive.read("版本信息.json").decode("utf-8")
        # the drive, "Users" and the rest of the line stay readable
        self.assertIn("C:\\Users\\" + HIDDEN + "\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\x.py", log)
        self.assertIn("/home/" + HIDDEN + "/app/main.py", log)
        self.assertIn("RuntimeError: host " + HIDDEN + " refused for " + HIDDEN, log)
        self.assertIn("Users", status_json)
        self.assertIn(HIDDEN, status_json)
        self.assertIn(HIDDEN, services_json)
        self.assertIn("launch failed in", services_json)
        self.assertNotIn("假名Zhang", versions_json)

    def test_the_scrubber_alone_handles_json_escaped_and_mixed_slashes(self):
        for raw in (
            "C:\\Users\\bob\\x", "C:/Users/bob/x", "c:\\\\Users\\\\bob\\\\x", "E:\\Users\\bob smith\\x", "/home/bob/x",
        ):
            with self.subTest(raw=raw):
                scrubbed = scrub_text(raw, [])
                self.assertNotIn("bob", scrubbed)
                self.assertIn(HIDDEN, scrubbed)
        # plain text that merely mentions the word is left alone
        self.assertEqual(scrub_text("many Users on one machine", []), "many Users on one machine")
        self.assertEqual(scrub_text("C:\\Program Files\\x", []), "C:\\Program Files\\x")

    def test_environment_values_skip_short_and_generic_names(self):
        import os
        from unittest.mock import patch as mock_patch

        with mock_patch.dict(os.environ, {"USERNAME": "ab", "COMPUTERNAME": "Admin", "USER": "FAKEUSERX", "LOGNAME": "", "USERPROFILE": "C:\\Users\\FAKEUSERX"}):
            values = environment_values(self.root)
        self.assertNotIn("ab", values)
        self.assertNotIn("Admin", values)
        self.assertIn("FAKEUSERX", values)
        self.assertIn("C:\\Users\\FAKEUSERX", values)
        self.assertIn(str(self.root), values)
        self.assertEqual(values, sorted(values, key=len, reverse=True))

    def test_scrub_and_collect_helpers(self):
        values = sensitive_values(fake_config())
        for needle in (SECRETS["key"], SECRETS["rvc"], SECRETS["qq"], SECRETS["group_a"], SECRETS["sender_id"]):
            self.assertIn(needle, values)
        self.assertEqual(values, sorted(values, key=len, reverse=True))
        self.assertEqual(scrub_text("a D:\\x\\y b D:/x/y c", ["D:\\x\\y"]), f"a {HIDDEN} b {HIDDEN} c")
        # words that are only kinds of things are not collected (they would blank half the log)
        self.assertNotIn("irodori", values)
        self.assertNotIn("napcat", values)

    def test_a_missing_part_does_not_break_the_zip(self):
        data = build_diagnostics_zip(data_dir=self.root / "nope", config={}, status=None, services=None, install=None)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn("说明.txt", archive.namelist())
            self.assertEqual(json.loads(archive.read("状态快照.json")), None)


class InstallBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "release" / "data"
        self.data.mkdir(parents=True)
        self.addons = root / "release" / "addons"
        for relative, content in (
            ("astrbot_plugin_st_bridge/metadata.yaml", "name: bridge"),
            ("astrbot_plugin_st_bridge/main.py", "# plugin"),
            ("astrbot_plugin_st_bridge/st_bridge_core/version.py", 'CORE_VERSION = "9"\n'),
            ("st_bridge_client/manifest.json", "{}"),
            ("st_bridge_client/index.js", "// ext"),
        ):
            path = self.addons / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.astrbot = root / "AstrBot"
        (self.astrbot / "data" / "plugins").mkdir(parents=True)
        self.st = root / "ST"
        (self.st / "data" / "default-user" / "extensions").mkdir(parents=True)
        self.tick = [0]

        def clock():
            self.tick[0] += 1
            return datetime(2026, 11, 12, 10, 0, 0).replace(second=0) if False else datetime(2026, 11, 12, 10, 0, 0) + __import__("datetime").timedelta(seconds=self.tick[0])

        self.logs = []

        class Logger:
            def warning(inner, message):
                self.logs.append(message)

            info = debug = error = warning

        self.service = InstallService(self.data, addons_dir=self.addons, clock=clock, logger=Logger())
        self.ab = check_astrbot(str(self.astrbot))
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service import check_st

        self.stc = check_st(str(self.st))
        self.backups = self.data / "backups"

    def names(self, prefix):
        return sorted(p.name for p in self.backups.iterdir() if p.name.startswith(prefix)) if self.backups.exists() else []

    def test_only_the_newest_three_backups_of_a_label_stay(self):
        for _ in range(7):
            self.service.install_plugin(self.ab)
        kept = self.names("astrbot-plugin-")
        self.assertEqual(len(kept), 3)
        # the newest are the ones kept: names carry the time, and later installs have later times
        self.assertEqual(kept, sorted(kept))
        self.assertEqual(self.service.backup_usage()["labels"]["astrbot-plugin"]["count"], 3)

    def test_the_plugin_and_the_extension_are_counted_separately(self):
        for _ in range(5):
            self.service.install_plugin(self.ab)
        for _ in range(2):
            self.service.install_extension(self.stc)
        self.assertEqual(len(self.names("astrbot-plugin-")), 3)
        self.assertEqual(len(self.names("st-extension-")), 1)  # the first extension install had nothing to back up
        usage = self.service.backup_usage()
        self.assertEqual((usage["count"], usage["labels"]["st-extension"]["count"]), (4, 1))
        self.assertGreater(usage["bytes"], 0)

    def test_other_folders_in_backups_are_never_touched(self):
        for name in ("st-presets", "astrbot-plugin-notes", "st-extension-old", "astrbot-plugin-20261111-0000", "random"):
            (self.backups / name).mkdir(parents=True)
            (self.backups / name / "keep.txt").write_text("x", encoding="utf-8")
        (self.backups / "astrbot-plugin-20261101-000000.txt").write_text("a file, not a folder", encoding="utf-8")
        for _ in range(6):
            self.service.install_plugin(self.ab)
        self.service.clear_install_backups(1)
        for name in ("st-presets", "astrbot-plugin-notes", "st-extension-old", "astrbot-plugin-20261111-0000", "random"):
            self.assertTrue((self.backups / name / "keep.txt").exists(), name)
        self.assertTrue((self.backups / "astrbot-plugin-20261101-000000.txt").exists())
        self.assertEqual(len(self.names("astrbot-plugin-2026110")), 1)  # the stray file only
        import re

        real = [n for n in self.names("astrbot-plugin-") if re.fullmatch(r"astrbot-plugin-\d{8}-\d{6}(-\d+)?", n) and (self.backups / n).is_dir()]
        self.assertEqual(len(real), 1)

    def test_a_backup_that_cannot_be_deleted_does_not_fail_the_install(self):
        for _ in range(4):
            self.service.install_plugin(self.ab)
        real = __import__("shutil").rmtree

        def locked(path, *args, **kwargs):
            if "astrbot-plugin-" in str(path) and "backups" in str(path):
                raise PermissionError(13, "in use")
            return real(path, *args, **kwargs)

        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service.shutil.rmtree", locked):
            result = self.service.install_plugin(self.ab)  # must not raise
        self.assertIn("重载插件", result["message"])
        self.assertTrue(any("could not be deleted" in line for line in self.logs))
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "installed")
        # the next install (nothing locked any more) tidies up
        self.service.install_plugin(self.ab)
        self.assertEqual(len(self.names("astrbot-plugin-")), 3)

    def test_the_manual_button_keeps_only_the_newest_of_each_label(self):
        for _ in range(5):
            self.service.install_plugin(self.ab)
        for _ in range(4):
            self.service.install_extension(self.stc)
        before = self.service.backup_usage()
        result = self.service.clear_install_backups(1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["removed"], before["count"] - 2)
        after = self.service.backup_usage()
        self.assertEqual((after["labels"]["astrbot-plugin"]["count"], after["labels"]["st-extension"]["count"]), (1, 1))
        self.assertEqual(result["bytes"], before["bytes"] - after["bytes"])
        self.assertEqual(self.service.clear_install_backups(1)["removed"], 0)

    def test_the_install_result_does_not_change(self):
        first = self.service.install_plugin(self.ab)
        self.assertIsNone(first["backup"])
        second = self.service.install_plugin(self.ab)
        self.assertTrue(Path(second["backup"]).exists())


class ConsoleBackupAndDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ConfigStore(self.root)
        config = fake_config()

        def update(current):
            for section in ("owner", "chat_platform", "cover", "install", "tts", "texts"):
                current[section] = config[section]

        await self.store.update(update)
        (self.root / "logs").mkdir()
        (self.root / "logs" / "bridge.log").write_text(f"2026-11-12 10:00:00 INFO [STBridge] dir={SECRETS['irodori']}\n", encoding="utf-8")
        self.install = InstallService(self.root, addons_dir=None)
        (self.root / "backups" / "st-presets").mkdir(parents=True)
        for label in ("astrbot-plugin", "st-extension"):
            for i in range(4):
                folder = self.root / "backups" / f"{label}-2026111{i}-100000"
                folder.mkdir()
                (folder / "f.txt").write_text("x" * 100, encoding="utf-8")
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.storage_cleanup import CleanupService

        self.cleanup = CleanupService(self.store, temp_dirs=[], logger=None) if False else None
        self.port = _unused_loopback_port()
        self.service = ConsoleService(create_console_app(self.store, install=self.install, cleanup=_FakeCleanup()), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def test_the_cleanup_overview_has_the_install_backups_and_the_button_clears_them(self):
        async with self.client.get(f"http://127.0.0.1:{self.port}/api/cleanup") as response:
            body = await response.json()
        usage = body["install_backups"]
        self.assertEqual((usage["count"], usage["labels"]["astrbot-plugin"]["count"], usage["labels"]["st-extension"]["count"]), (8, 4, 4))
        self.assertEqual(usage["bytes"], 800)
        async with self.client.post(f"http://127.0.0.1:{self.port}/api/cleanup/install-backups/clear", json={}) as response:
            self.assertEqual(response.status, 200)
            result = await response.json()
        self.assertEqual((result["removed"], result["failed"], result["bytes"]), (6, 0, 600))
        self.assertEqual(result["install_backups"]["count"], 2)
        self.assertTrue((self.root / "backups" / "st-presets").is_dir())
        # the newest of each label is what stayed
        self.assertTrue((self.root / "backups" / "astrbot-plugin-20261113-100000").is_dir())
        self.assertFalse((self.root / "backups" / "astrbot-plugin-20261110-100000").exists())

    async def test_the_diagnostics_download_is_a_zip_without_any_secret(self):
        async with self.client.get(f"http://127.0.0.1:{self.port}/api/diagnostics/download") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "application/zip")
            self.assertIn("st-bridge-diagnostics.zip", response.headers["Content-Disposition"])
            data = await response.read()
        text = zip_text(data)
        for name, value in SECRETS.items():
            self.assertNotIn(value, text, name)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn("日志/bridge.log", archive.namelist())
            self.assertIn("配置摘要.json", archive.namelist())
            status = json.loads(archive.read("状态快照.json"))
        self.assertIn("bridge", status)


class _FakeCleanup:
    async def overview(self):
        return {"stats": {}, "last_auto": {}}


if __name__ == "__main__":
    unittest.main()

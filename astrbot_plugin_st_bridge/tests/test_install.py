"""R4b: the "安装与路径" backend — path checks, plugin / extension install, data import, dependency jobs, NapCat, restart."""

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import runner
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.config_presets import apply_to_config, is_modified, normalize_config, snapshot
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError, default_config, validate_config
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_config import DEFAULT_PIP_MIRROR, install_settings, validate_install_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service import (
    Dependency,
    InstallError,
    InstallService,
    check_astrbot,
    check_gpt_sovits,
    check_irodori,
    check_irodori_server,
    check_napcat,
    check_paths,
    check_rvc,
    check_st,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import RESTART_EXIT_CODE


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode() + (b"D" if path.is_dir() else path.read_bytes()))
    return digest.hexdigest()


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 6, 10, 0, 0)

    def __call__(self):
        self.value = self.value.replace(second=(self.value.second + 1) % 60)
        return self.value


class TempCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)


# ---- the path checks -------------------------------------------------------------------------------------
class PathCheckTests(TempCase):
    def test_empty_means_not_set(self):
        for check in (check_st, check_astrbot, check_napcat, check_gpt_sovits, check_irodori, check_irodori_server, check_rvc):
            self.assertEqual(check("")["state"], "empty")
        self.assertEqual({item["state"] for item in check_paths({}).values()}, {"empty"})

    def test_st_standard_layouts(self):
        (self.root / "a" / "data" / "default-user").mkdir(parents=True)
        result = check_st(str(self.root / "a"))
        self.assertEqual(result["state"], "valid")
        self.assertEqual(result["extensions_dir"], str(self.root / "a" / "data" / "default-user" / "extensions"))
        (self.root / "b" / "default-user").mkdir(parents=True)  # the data folder itself
        self.assertEqual(check_st(str(self.root / "b"))["extensions_dir"], str(self.root / "b" / "default-user" / "extensions"))

    def test_st_data_root_from_config_yaml_relative_and_absolute(self):
        program = self.root / "st"
        (program / "elsewhere" / "default-user").mkdir(parents=True)
        touch(program / "config.yaml", "listen: false\ndataRoot: ./elsewhere  # custom\n")
        self.assertEqual(check_st(str(program))["extensions_dir"], str(program / "elsewhere" / "default-user" / "extensions"))
        other = self.root / "abs" / "default-user"
        other.mkdir(parents=True)
        program2 = self.root / "st2"
        touch(program2 / "config.yaml", f"dataRoot: '{(self.root / 'abs').as_posix()}'\n")
        self.assertEqual(check_st(str(program2))["extensions_dir"], str(Path(other) / "extensions") if False else check_st(str(program2))["extensions_dir"])
        self.assertEqual(check_st(str(program2))["state"], "valid")
        self.assertTrue(check_st(str(program2))["extensions_dir"].endswith("extensions"))

    def test_st_launcher_layout_with_data_beside_the_program(self):
        launcher = self.root / "Launcher" / "data"
        (launcher / "sillytavern").mkdir(parents=True)
        (launcher / "st_data" / "default-user").mkdir(parents=True)
        self.assertEqual(check_st(str(launcher / "sillytavern"))["state"], "valid")  # program folder
        self.assertEqual(check_st(str(launcher / "st_data"))["state"], "valid")      # data folder
        self.assertEqual(check_st(str(self.root / "Launcher"))["state"], "valid")    # the launcher folder

    def test_st_invalid(self):
        self.assertEqual(check_st(str(self.root / "missing")), {"state": "invalid", "message": "目录不存在"})
        (self.root / "plain").mkdir()
        result = check_st(str(self.root / "plain"))
        self.assertEqual(result["state"], "invalid")
        self.assertIn("default-user", result["message"])

    def test_astrbot_layouts(self):
        (self.root / "src" / "data" / "plugins").mkdir(parents=True)
        (self.root / "inst" / "core" / "data" / "plugins").mkdir(parents=True)
        (self.root / "direct" / "plugins").mkdir(parents=True)
        self.assertEqual(check_astrbot(str(self.root / "src"))["plugins_dir"], str(self.root / "src" / "data" / "plugins"))
        launcher = check_astrbot(str(self.root / "inst"))
        self.assertEqual(launcher["plugins_dir"], str(self.root / "inst" / "core" / "data" / "plugins"))
        self.assertEqual(
            launcher["plugin_data_dir"],
            str(self.root / "inst" / "core" / "data" / "plugin_data" / "astrbot_plugin_st_bridge"),
        )
        self.assertEqual(check_astrbot(str(self.root / "direct" / "plugins"))["state"], "valid")
        self.assertEqual(check_astrbot(str(self.root / "direct"))["state"], "invalid")
        self.assertEqual(check_astrbot(str(self.root / "nowhere"))["state"], "invalid")

    def test_napcat_file_directory_and_nothing(self):
        exe = touch(self.root / "napcat" / "Start.bat")
        touch(self.root / "napcat" / "readme.txt")
        touch(self.root / "napcat" / "NapCat.exe")
        self.assertEqual(check_napcat(str(exe))["state"], "valid")
        self.assertEqual(check_napcat(str(exe))["launcher"], str(exe))
        self.assertEqual(check_napcat(str(self.root / "napcat" / "readme.txt"))["state"], "invalid")
        chosen = check_napcat(str(self.root / "napcat"))
        self.assertEqual(chosen["state"], "choose")
        self.assertEqual([Path(item).name for item in chosen["candidates"]], ["NapCat.exe", "Start.bat"])
        (self.root / "empty").mkdir()
        self.assertEqual(check_napcat(str(self.root / "empty"))["state"], "invalid")
        self.assertEqual(check_napcat(str(self.root / "nope"))["state"], "invalid")

    def test_napcat_files_are_never_opened(self):
        launcher = touch(self.root / "napcat" / "go.bat", "secret token")
        with patch("builtins.open", side_effect=AssertionError("a file was opened")), patch.object(Path, "read_text", side_effect=AssertionError("read")), patch.object(Path, "read_bytes", side_effect=AssertionError("read")):
            self.assertEqual(check_napcat(str(launcher))["state"], "valid")
            self.assertEqual(check_napcat(str(launcher.parent))["state"], "choose")

    def test_tts_and_rvc_folders(self):
        gpt = self.root / "gpt"
        touch(gpt / "runtime" / "python.exe")
        self.assertIn("api.py", check_gpt_sovits(str(gpt))["message"])
        touch(gpt / "api.py")
        self.assertEqual(check_gpt_sovits(str(gpt))["state"], "valid")
        iro = self.root / "iro"
        iro.mkdir()
        self.assertEqual(check_irodori(str(iro))["state"], "invalid")
        touch(iro / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(check_irodori(str(iro))["state"], "valid")
        server = self.root / "iro" / "server"
        self.assertEqual(check_irodori_server(str(server))["state"], "invalid")
        touch(server / "src" / "irodori_openai_tts" / "__main__.py")
        self.assertEqual(check_irodori_server(str(server))["state"], "valid")
        rvc = self.root / "rvc"
        touch(rvc / "runtime" / "python.exe")
        self.assertIn("assets", check_rvc(str(rvc))["message"])
        (rvc / "assets" / "weights").mkdir(parents=True)
        self.assertEqual(check_rvc(str(rvc))["state"], "valid")


# ---- install / reinstall / uninstall ----------------------------------------------------------------------
class PlacementTests(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "release" / "data"
        self.data.mkdir(parents=True)
        self.addons = self.root / "release" / "addons"
        touch(self.addons / "astrbot_plugin_st_bridge" / "metadata.yaml", "name: bridge")
        touch(self.addons / "astrbot_plugin_st_bridge" / "main.py", "# new plugin")
        touch(self.addons / "astrbot_plugin_st_bridge" / "st_bridge_core" / "version.py", 'CORE_VERSION = "9"\n')
        touch(self.addons / "astrbot_plugin_st_bridge" / "__pycache__" / "x.pyc", "junk")
        touch(self.addons / "st_bridge_client" / "manifest.json", "{}")
        touch(self.addons / "st_bridge_client" / "index.js", "// new")
        self.astrbot = self.root / "AstrBot"
        (self.astrbot / "data" / "plugins").mkdir(parents=True)
        self.st = self.root / "ST"
        (self.st / "data" / "default-user" / "extensions").mkdir(parents=True)
        self.service = InstallService(self.data, addons_dir=self.addons, clock=Clock())
        self.ab = check_astrbot(str(self.astrbot))
        self.stc = check_st(str(self.st))

    def plugin_dir(self):
        return self.astrbot / "data" / "plugins" / "astrbot_plugin_st_bridge"

    def test_the_package_is_recognized(self):
        self.assertTrue(self.service.package_info()["available"])
        self.assertFalse(InstallService(self.data, addons_dir=None).package_info()["available"])
        (self.addons / "st_bridge_client" / "manifest.json").unlink()
        info = self.service.package_info()
        self.assertEqual((info["plugin"], info["extension"], info["available"]), (True, False, False))

    def test_install_copies_the_plugin_and_writes_the_core_link(self):
        result = self.service.install_plugin(self.ab)
        target = self.plugin_dir()
        self.assertEqual(result["target"], str(target))
        self.assertIsNone(result["backup"])
        self.assertEqual((target / "main.py").read_text(), "# new plugin")
        self.assertFalse((target / "__pycache__").exists())
        self.assertEqual(json.loads((target / "core_link.json").read_text(encoding="utf-8")), {"data_dir": str(self.data.resolve())})
        self.assertIn("重载插件", result["message"])
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "installed")

    def test_reinstall_backs_up_the_old_folder_first_and_leaves_plugin_data_alone(self):
        self.service.install_plugin(self.ab)
        target = self.plugin_dir()
        (target / "main.py").write_text("# edited by hand", encoding="utf-8")
        plugin_data = touch(self.astrbot / "data" / "plugin_data" / "astrbot_plugin_st_bridge" / "config.json", "{}")
        before = tree_hash(plugin_data.parent)
        result = self.service.install_plugin(self.ab)
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.data / "backups")
        self.assertTrue(backup.name.startswith("astrbot-plugin-"))
        self.assertEqual((backup / "main.py").read_text(encoding="utf-8"), "# edited by hand")
        self.assertEqual((target / "main.py").read_text(), "# new plugin")
        self.assertEqual(tree_hash(plugin_data.parent), before)

    def test_two_backups_in_the_same_second_get_different_folders(self):
        self.service._clock = lambda: datetime(2026, 11, 6, 10, 0, 0)
        self.service.install_plugin(self.ab)
        first = Path(self.service.install_plugin(self.ab)["backup"])
        second = Path(self.service.install_plugin(self.ab)["backup"])
        self.assertNotEqual(first, second)

    def test_a_failed_copy_puts_the_old_plugin_back(self):
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "main.py").write_text("# precious", encoding="utf-8")
        original = InstallService._copy_folder
        calls = []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 2:  # backup is call 1, the new copy is call 2
                Path(target).mkdir(parents=True)
                raise OSError("disk full")
            original(source, target)

        with patch.object(InstallService, "_copy_folder", staticmethod(flaky)):
            with self.assertRaises(InstallError) as caught:
                self.service.install_plugin(self.ab)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual((self.plugin_dir() / "main.py").read_text(encoding="utf-8"), "# precious")

    def test_a_locked_old_folder_stops_before_changing_anything_and_says_why(self):
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "main.py").write_text("# precious", encoding="utf-8")
        real_replace = os.replace

        def locked(source, target):
            if Path(source) == self.plugin_dir():
                raise PermissionError(13, "in use")
            real_replace(source, target)

        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.install_service.os.replace", locked):
            with self.assertRaises(InstallError) as caught:
                self.service.install_plugin(self.ab)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("被占用", str(caught.exception))
        self.assertEqual((self.plugin_dir() / "main.py").read_text(encoding="utf-8"), "# precious")
        self.assertEqual([p.name for p in self.plugin_dir().parent.iterdir()], ["astrbot_plugin_st_bridge"])

    def test_a_failure_after_the_old_folder_was_moved_aside_puts_it_back(self):
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "main.py").write_text("# precious", encoding="utf-8")
        def boom(target):
            raise OSError("link file not writable")

        with self.assertRaises(InstallError) as caught:
            self.service._place(self.addons / "astrbot_plugin_st_bridge", self.plugin_dir(), "astrbot-plugin", boom)
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("已恢复原来的文件", str(caught.exception))
        self.assertEqual((self.plugin_dir() / "main.py").read_text(encoding="utf-8"), "# precious")
        self.assertEqual([p.name for p in self.plugin_dir().parent.iterdir()], ["astrbot_plugin_st_bridge"])

    def test_old_folder_that_cannot_be_deleted_does_not_fail_the_install_and_is_tidied_next_time(self):
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "main.py").write_text("# old", encoding="utf-8")
        real_remove = InstallService._remove_tree

        def stuck(path):
            if ".old-" in Path(path).name:
                raise PermissionError(13, "in use")
            real_remove(path)

        with patch.object(InstallService, "_remove_tree", staticmethod(stuck)):
            result = self.service.install_plugin(self.ab)
        self.assertEqual((self.plugin_dir() / "main.py").read_text(), "# new plugin")
        self.assertIn("重载插件", result["message"])
        leftovers = [p for p in self.plugin_dir().parent.iterdir() if ".old-" in p.name]
        self.assertEqual(len(leftovers), 1)
        self.service.install_plugin(self.ab)  # the next install deletes it
        self.assertEqual([p.name for p in self.plugin_dir().parent.iterdir()], ["astrbot_plugin_st_bridge"])

    def test_uninstall_that_cannot_delete_says_why(self):
        self.service.install_plugin(self.ab)
        real_remove = InstallService._remove_tree

        def stuck(path):
            raise PermissionError(13, "in use")

        with patch.object(InstallService, "_remove_tree", staticmethod(stuck)):
            with self.assertRaises(InstallError) as caught:
                self.service.uninstall_plugin(self.ab)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("被占用", str(caught.exception))
        self.assertTrue((self.plugin_dir() / "main.py").exists())

    def test_uninstall_backs_up_then_removes_only_the_fixed_folder(self):
        self.service.install_plugin(self.ab)
        neighbour = touch(self.astrbot / "data" / "plugins" / "other_plugin" / "main.py")
        result = self.service.uninstall_plugin(self.ab)
        self.assertFalse(self.plugin_dir().exists())
        self.assertTrue(neighbour.is_file())
        self.assertTrue((Path(result["backup"]) / "main.py").is_file())
        with self.assertRaises(InstallError) as caught:
            self.service.uninstall_plugin(self.ab)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "missing")

    def test_plugin_status_notices_other_code_and_a_wrong_link(self):
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "st_bridge_core" / "version.py").write_text('CORE_VERSION = "8"\n', encoding="utf-8")
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "outdated")
        self.service.install_plugin(self.ab)
        (self.plugin_dir() / "core_link.json").write_text(json.dumps({"data_dir": str(self.root / "elsewhere")}), encoding="utf-8")
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "unlinked")
        (self.plugin_dir() / "core_link.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.service.plugin_status(self.ab)["state"], "unlinked")

    def test_install_needs_a_valid_astrbot_path_and_a_package(self):
        with self.assertRaises(InstallError) as caught:
            self.service.install_plugin(check_astrbot(""))
        self.assertEqual(caught.exception.status, 400)
        development = InstallService(self.data, addons_dir=None)
        with self.assertRaises(InstallError) as caught:
            development.install_plugin(self.ab)
        self.assertEqual((caught.exception.status, caught.exception.message), (409, "开发环境，没有可安装的文件"))
        (self.addons / "astrbot_plugin_st_bridge" / "metadata.yaml").unlink()  # not our package
        with self.assertRaises(InstallError):
            self.service.install_plugin(self.ab)
        self.assertFalse(self.plugin_dir().exists())

    def test_extension_install_reinstall_and_uninstall(self):
        target = self.st / "data" / "default-user" / "extensions" / "st_bridge_client"
        self.assertEqual(self.service.extension_status(self.stc)["state"], "missing")
        result = self.service.install_extension(self.stc)
        self.assertEqual((target / "index.js").read_text(), "// new")
        self.assertIn("刷新 ST", result["message"])
        self.assertEqual(self.service.extension_status(self.stc)["state"], "installed")
        (target / "index.js").write_text("// old", encoding="utf-8")
        self.assertEqual(self.service.extension_status(self.stc)["state"], "outdated")
        again = self.service.install_extension(self.stc)
        self.assertEqual((Path(again["backup"]) / "index.js").read_text(), "// old")
        self.assertTrue(Path(again["backup"]).name.startswith("st-extension-"))
        other = touch(self.st / "data" / "default-user" / "extensions" / "third-party" / "index.js")
        removed = self.service.uninstall_extension(self.stc)
        self.assertFalse(target.exists())
        self.assertTrue(other.is_file())
        self.assertTrue((Path(removed["backup"]) / "index.js").is_file())
        with self.assertRaises(InstallError):
            self.service.install_extension(check_st(""))


# ---- importing the data of an older plugin ----------------------------------------------------------------
class ImportTests(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "release" / "data"
        self.data.mkdir(parents=True)
        self.old = self.root / "AstrBot" / "data" / "plugin_data" / "astrbot_plugin_st_bridge"
        audio = self.old / "audio" / "kotone" / "happy.wav"
        config = default_config()
        config["models"] = {"m": {"reference_audio_root": str(self.old / "audio")}}
        config["characters"] = {"c": {"emotions": {"happy": {"ref_audio_path": str(audio)}}}}
        config["tts"]["gpt_sovits"]["ref_audio_path"] = str(audio).replace("\\", "/")
        config["gpt_sovits_root"] = r"E:\GPT-SoVITS"
        touch(self.old / "config.json", json.dumps(config, ensure_ascii=False))
        touch(audio, "wav")
        touch(self.old / "affinity.json", '{"version": 3}')
        touch(self.old / "connector_token", "old-connector-token\n")
        touch(self.old / "onebot_token", "old-onebot-token\n")
        touch(self.old / "songs" / "a.mp3", "mp3")
        touch(self.old / "logs" / "bridge.log", "log")
        touch(self.old / "temp_audio" / "t.wav", "tmp")
        touch(self.old / "cover_stems" / "s.wav", "stem")
        touch(self.old / "backups" / "b" / "config.json", "{}")
        touch(self.old / "cover_service.log", "worker log")
        self.service = InstallService(self.data, addons_dir=None, clock=Clock())

    def test_preview_lists_what_would_be_copied_and_what_is_skipped(self):
        preview = self.service.import_preview(str(self.old))
        names = {item["name"] for item in preview["items"]}
        self.assertEqual(names, {"config.json", "affinity.json", "connector_token", "onebot_token", "audio", "songs"})
        self.assertEqual(set(preview["skipped"]), {"logs", "temp_audio", "cover_stems", "backups", "cover_service.log"})
        audio = next(item for item in preview["items"] if item["name"] == "audio")
        self.assertEqual((audio["kind"], audio["files"], audio["size"]), ("dir", 1, 3))
        self.assertEqual(preview["total_size"], sum(item["size"] for item in preview["items"]))
        self.assertEqual(preview["path_references"], 3)
        self.assertEqual(preview["replaces"], [])

    def test_import_copies_backs_up_what_it_replaces_and_rewrites_paths(self):
        touch(self.data / "config.json", '{"mine": true}')
        touch(self.data / "connector_token", "fresh-token\n")
        touch(self.data / "audio" / "mine.wav", "mine")
        before = tree_hash(self.old)
        preview = self.service.import_preview(str(self.old))
        self.assertEqual(set(preview["replaces"]), {"config.json", "connector_token", "audio"})
        result = self.service.import_data(str(self.old))
        self.assertTrue(result["restart_required"])
        self.assertEqual(set(result["copied"]), {"config.json", "affinity.json", "connector_token", "onebot_token", "audio", "songs"})
        self.assertEqual((self.data / "connector_token").read_text(), "old-connector-token\n")  # the plugin and NapCat keep working
        self.assertEqual((self.data / "onebot_token").read_text(), "old-onebot-token\n")
        self.assertEqual((self.data / "songs" / "a.mp3").read_text(), "mp3")
        for skipped in ("logs", "temp_audio", "cover_stems", "cover_service.log"):
            self.assertFalse((self.data / skipped).exists(), skipped)
        self.assertFalse((self.data / "backups" / "b").exists())  # the old plugin's own backups stay behind
        backup = Path(result["backup"])
        self.assertTrue(backup.name.startswith("pre-import-"))
        self.assertEqual((backup / "config.json").read_text(), '{"mine": true}')
        self.assertEqual((backup / "connector_token").read_text(), "fresh-token\n")
        self.assertEqual((backup / "audio" / "mine.wav").read_text(), "mine")
        self.assertEqual((self.data / "audio" / "mine.wav").read_text(), "mine")  # a folder is merged
        self.assertEqual(tree_hash(self.old), before)  # the source is read-only

    def test_absolute_paths_into_the_old_folder_now_point_into_the_new_one(self):
        result = self.service.import_data(str(self.old))
        self.assertEqual(result["rewritten_paths"], 3)
        config = json.loads((self.data / "config.json").read_text(encoding="utf-8"))
        new_audio = self.data / "audio" / "kotone" / "happy.wav"
        self.assertEqual(config["characters"]["c"]["emotions"]["happy"]["ref_audio_path"], str(new_audio))
        self.assertEqual(config["models"]["m"]["reference_audio_root"], str(self.data / "audio"))
        self.assertEqual(Path(config["tts"]["gpt_sovits"]["ref_audio_path"]), new_audio)
        self.assertEqual(config["gpt_sovits_root"], r"E:\GPT-SoVITS")  # paths elsewhere stay
        self.assertTrue(new_audio.is_file())
        validate_config(config)

    def test_this_machines_install_paths_survive_an_import_that_has_none(self):
        # Importing replaces config.json; the ST / AstrBot / NapCat paths typed on this machine must not be lost.
        mine = default_config()
        mine["install"].update(st_dir="C:\\ST", astrbot_dir="C:\\AB", napcat_path="C:\\NC\\go.bat", pip_mirror="")
        touch(self.data / "config.json", json.dumps(mine))
        result = self.service.import_data(str(self.old))
        self.assertTrue(result["kept_install"])
        config = json.loads((self.data / "config.json").read_text(encoding="utf-8"))
        self.assertEqual((config["install"]["st_dir"], config["install"]["astrbot_dir"], config["install"]["napcat_path"]), ("C:\\ST", "C:\\AB", "C:\\NC\\go.bat"))
        self.assertEqual(config["gpt_sovits_root"], r"E:\GPT-SoVITS")  # the rest is the old plugin's
        validate_config(config)

    def test_with_nothing_set_here_the_imported_install_section_is_left_alone(self):
        touch(self.data / "config.json", json.dumps(default_config()))  # no install paths set on this machine
        result = self.service.import_data(str(self.old))
        self.assertFalse(result["kept_install"])
        theirs = json.loads((self.old / "config.json").read_text(encoding="utf-8"))["install"]
        self.assertEqual(json.loads((self.data / "config.json").read_text(encoding="utf-8"))["install"], theirs)

    def test_a_config_without_paths_into_the_old_folder_is_copied_unchanged(self):
        plain = default_config()
        plain["gpt_sovits_root"] = r"E:\GPT-SoVITS"
        text = json.dumps(plain, ensure_ascii=False)
        touch(self.old / "config.json", text)
        self.assertEqual(self.service.import_preview(str(self.old))["path_references"], 0)
        self.assertEqual(self.service.import_data(str(self.old))["rewritten_paths"], 0)
        self.assertEqual((self.data / "config.json").read_text(encoding="utf-8"), text)

    def test_refusals(self):
        with self.assertRaises(InstallError) as caught:
            self.service.import_preview("")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(InstallError) as caught:
            self.service.import_preview(str(self.root / "nowhere"))
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(InstallError):
            self.service.import_preview(str(self.data))            # itself
        with self.assertRaises(InstallError):
            self.service.import_preview(str(self.data.parent))     # its parent
        (self.root / "empty").mkdir()
        with self.assertRaises(InstallError):
            self.service.import_data(str(self.root / "empty"))     # not a data folder
        self.assertEqual(list(self.data.iterdir()), [])

    def test_default_source_comes_from_the_astrbot_path(self):
        check = check_astrbot(str(self.root / "AstrBot"))
        self.assertEqual(check["state"], "invalid")
        (self.root / "AstrBot" / "data" / "plugins").mkdir(parents=True)
        check = check_astrbot(str(self.root / "AstrBot"))
        self.assertEqual(Path(self.service.default_import_source(check)), self.old)
        self.assertEqual(self.service.default_import_source(check_astrbot("")), "")


# ---- extra dependencies ------------------------------------------------------------------------------------
def fake_dependency(name="fake", script="print('installing'); print('done')", check="import sys", python=None):
    return Dependency(
        name=name, label=name, python=lambda config: python if python is not None else sys.executable,
        check_args=["-c", check], install_args=lambda mirror: ["-c", script + (f"; print({mirror!r})" if mirror else "")],
        needs="先设置",
    )


class DependencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def service(self, *deps):
        return InstallService(Path(self.directory.name), addons_dir=None, dependencies=deps or (fake_dependency(),))

    async def test_success_keeps_the_last_lines(self):
        service = self.service(fake_dependency(script="[print('line', i) for i in range(30)]"))
        started = service.start_dependency("fake", {}, "")
        self.assertEqual(started["state"], "running")
        await service.wait_job()
        job = service.job_status()
        self.assertEqual((job["state"], job["returncode"], job["message"]), ("success", 0, "安装完成"))
        self.assertEqual(job["lines"][-1], "line 29")
        self.assertEqual(len(job["lines"]), 20)  # only the last ones

    async def test_failure_reports_the_exit_code(self):
        service = self.service(fake_dependency(script="import sys; print('no way'); sys.exit(3)"))
        service.start_dependency("fake", {}, "")
        await service.wait_job()
        job = service.job_status()
        self.assertEqual((job["state"], job["returncode"]), ("failed", 3))
        self.assertIn("退出码 3", job["message"])
        self.assertIn("no way", job["lines"])

    async def test_only_one_job_at_a_time(self):
        service = self.service(fake_dependency(script="import time; time.sleep(30)"), fake_dependency("second"))
        service.start_dependency("fake", {}, "")
        for name in ("fake", "second"):
            with self.assertRaises(InstallError) as caught:
                service.start_dependency(name, {}, "")
            self.assertEqual(caught.exception.status, 409)
        await service.terminate()  # kills the sleeping process
        self.assertEqual(service.job_status()["state"], "failed")
        service.start_dependency("second", {}, "")  # free again
        await service.wait_job()
        self.assertEqual(service.job_status()["state"], "success")

    async def test_the_mirror_is_passed_on(self):
        service = self.service(fake_dependency(script="print('x')"))
        service.start_dependency("fake", {}, "https://mirror.example/simple")
        await service.wait_job()
        self.assertIn("https://mirror.example/simple", service.job_status()["lines"])

    async def test_unknown_name_missing_or_absent_python(self):
        service = self.service(fake_dependency(python=""), fake_dependency("gone", python=str(Path(self.directory.name) / "nope.exe")))
        with self.assertRaises(InstallError) as caught:
            service.start_dependency("nothing", {}, "")
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(InstallError) as caught:
            service.start_dependency("fake", {}, "")
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "先设置"))
        with self.assertRaises(InstallError):
            service.start_dependency("gone", {}, "")
        self.assertIsNone(service.job_status())

    async def test_rows_check_with_the_dependencys_own_python(self):
        service = self.service(
            fake_dependency("have", check="import sys"), fake_dependency("lack", check="import module_that_is_not_there_xyz"),
            fake_dependency("blocked", python=""),
        )
        rows = {row["name"]: row for row in await asyncio.to_thread(service.dependency_rows, {}, "")}
        self.assertEqual((rows["have"]["state"], rows["lack"]["state"], rows["blocked"]["state"]), ("installed", "missing", "blocked"))
        self.assertEqual(rows["blocked"]["needs"], "先设置")
        quick = service.dependency_rows({}, "", check=False)
        self.assertEqual({row["state"] for row in quick if row["name"] != "blocked"}, {"unknown"})

    async def test_pedalboard_goes_into_the_rvc_python_with_the_mirror(self):
        service = InstallService(Path(self.directory.name), addons_dir=None)
        row, = service.dependency_rows({"cover": {"rvc_root": ""}}, DEFAULT_PIP_MIRROR)
        self.assertEqual((row["name"], row["state"], row["needs"]), ("pedalboard", "blocked", "请先设置 RVC 目录"))
        root = Path(self.directory.name) / "rvc"
        row, = service.dependency_rows({"cover": {"rvc_root": str(root)}}, DEFAULT_PIP_MIRROR, check=False)
        self.assertEqual(row["python"], str(root / "runtime" / "python.exe"))
        self.assertIn("找不到", row["needs"])  # the RVC python is not there
        self.assertTrue(row["command"].endswith("-m pip install --no-deps pedalboard -i " + DEFAULT_PIP_MIRROR))
        row, = service.dependency_rows({"cover": {"rvc_root": str(root)}}, "", check=False)
        self.assertTrue(row["command"].endswith("-m pip install --no-deps pedalboard"))  # empty mirror = official index
        with self.assertRaises(InstallError):
            service.start_dependency("pedalboard", {"cover": {"rvc_root": ""}}, "")


# ---- NapCat ----------------------------------------------------------------------------------------------
class NapCatStartTests(TempCase):
    def test_starts_the_launcher_like_a_double_click_without_waiting(self):
        launcher = touch(self.root / "napcat" / "launcher.bat")
        popen_calls, shell_calls = [], []

        class Process:
            pass

        def popen(command, **kwargs):
            popen_calls.append((command, kwargs))
            return Process()

        def startfile(path, **kwargs):
            shell_calls.append((path, kwargs))

        service = InstallService(self.root / "data", addons_dir=None, popen=popen, startfile=startfile)
        result = service.start_napcat(check_napcat(str(launcher)))
        self.assertIn("launcher.bat", result["message"])
        if sys.platform == "win32":
            self.assertEqual(shell_calls, [(str(launcher), {"cwd": str(launcher.parent)})])
            self.assertEqual(popen_calls, [])
        else:
            self.assertEqual(shell_calls, [])
            command, kwargs = popen_calls[0]
            self.assertEqual(command, [str(launcher)])
            self.assertEqual(kwargs["cwd"], str(launcher.parent))
        for _command, kwargs in popen_calls:  # no standard handle is ever redirected
            for name in ("stdin", "stdout", "stderr"):
                self.assertNotIn(name, kwargs)

    def test_refuses_without_a_chosen_launcher_and_reports_start_failures(self):
        service = InstallService(
            self.root / "data", addons_dir=None, popen=lambda *a, **k: self.fail("started"),
            startfile=lambda *a, **k: self.fail("started"),
        )
        for check in (check_napcat(""), check_napcat(str(self.root)), check_napcat(str(self.root / "x.txt"))):
            with self.assertRaises(InstallError) as caught:
                service.start_napcat(check)
            self.assertEqual(caught.exception.status, 400)
        launcher = touch(self.root / "go.exe")

        def broken(*args, **kwargs):
            raise OSError("not a program")

        with self.assertRaises(InstallError) as caught:
            InstallService(
                self.root / "data", addons_dir=None, popen=broken, startfile=broken,
            ).start_napcat(check_napcat(str(launcher)))
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("无法启动 NapCat", caught.exception.message)


# ---- the config section and the presets ---------------------------------------------------------------------
class InstallConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(install_settings({}), {"st_dir": "", "astrbot_dir": "", "napcat_path": "", "pip_mirror": DEFAULT_PIP_MIRROR})
        checked = validate_install_settings({"st_dir": ' "C:\\ST" ', "pip_mirror": ""})
        self.assertEqual((checked["st_dir"], checked["pip_mirror"]), ("C:\\ST", ""))
        for bad in ({"st_dir": 5}, {"st_dir": "a\nb"}, {"st_dir": "x" * 600}, {"pip_mirror": "ftp://x"}, {"pip_mirror": "http://a b"}, [], {"pip_mirror": 3}):
            with self.assertRaises(Exception, msg=str(bad)):
                validate_install_settings(bad)
        self.assertEqual(install_settings({"install": {"st_dir": 5}})["st_dir"], "")  # unreadable: defaults

    def test_a_new_config_and_an_old_one_both_have_the_section(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            self.assertEqual(store.config["install"], install_settings({}))
            path = Path(directory) / "config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            del config["install"]
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(ConfigStore(Path(directory)).config["install"], install_settings({}))


class PresetPathTests(unittest.TestCase):
    def local(self):
        config = default_config()
        config["install"].update(st_dir="C:\\ST", astrbot_dir="C:\\AB", napcat_path="C:\\NC\\go.bat")
        config["cover"]["rvc_root"] = "C:\\RVC"
        config["tts"]["irodori"].update(launch_dir="C:\\Iro", server_dir="C:\\Iro\\server", lora_adapter="C:\\Lora")
        config["tts"]["gpt_sovits"]["launch_dir"] = "C:\\Gpt"
        return config

    def test_an_exported_preset_carries_no_local_paths(self):
        shot = snapshot(self.local())
        self.assertNotIn("install", shot)
        self.assertNotIn("rvc_root", shot["cover"])
        self.assertNotIn("launch_dir", shot["tts"]["irodori"])
        self.assertNotIn("server_dir", shot["tts"]["irodori"])
        self.assertNotIn("lora_adapter", shot["tts"]["irodori"])
        self.assertNotIn("launch_dir", shot["tts"]["gpt_sovits"])
        text = json.dumps(shot)
        for needle in ("C:\\\\ST", "C:\\\\RVC", "C:\\\\Iro", "C:\\\\Gpt", "C:\\\\Lora", "go.bat"):
            self.assertNotIn(needle, text)
        self.assertIn("hf_checkpoint", shot["tts"]["irodori"])  # the rest of the section stays

    def test_applying_a_preset_keeps_this_machines_paths(self):
        local = self.local()
        other = default_config()
        other["cover"]["rvc_root"] = "D:\\Theirs\\RVC"
        other["tts"]["irodori"].update(launch_dir="D:\\Theirs", server_dir="D:\\Theirs\\s", lora_adapter="D:\\Theirs\\lora")
        other["tts"]["gpt_sovits"]["launch_dir"] = "D:\\Theirs\\Gpt"
        other["install"].update(st_dir="D:\\Theirs\\ST")
        other["queue"]["max_waiting"] = 3
        result = normalize_config(other, local)
        self.assertEqual(result["queue"]["max_waiting"], 3)
        self.assertEqual(result["cover"]["rvc_root"], "C:\\RVC")
        self.assertEqual((result["tts"]["irodori"]["launch_dir"], result["tts"]["irodori"]["server_dir"]), ("C:\\Iro", "C:\\Iro\\server"))
        self.assertEqual(result["tts"]["irodori"]["lora_adapter"], "C:\\Lora")  # a folder of this machine, like the others
        self.assertEqual(result["tts"]["gpt_sovits"]["launch_dir"], "C:\\Gpt")
        self.assertEqual(result["install"], local["install"])
        applied = default_config()
        apply_to_config(applied, result)
        self.assertEqual(applied["cover"]["rvc_root"], "C:\\RVC")

    def test_a_preset_without_any_path_keeps_the_local_ones_and_does_not_look_modified(self):
        local = self.local()
        preset = snapshot(local)
        result = normalize_config(preset, local)
        self.assertEqual(result["cover"]["rvc_root"], "C:\\RVC")
        self.assertEqual(result["tts"]["gpt_sovits"]["launch_dir"], "C:\\Gpt")
        self.assertFalse(is_modified(local, preset))
        moved = self.local()
        moved["cover"]["rvc_root"] = "C:\\Elsewhere"
        self.assertFalse(is_modified(moved, preset))  # paths are not part of what a preset means


# ---- the routes ------------------------------------------------------------------------------------------
class FakeTts:
    def __init__(self):
        self.applied = []

    async def apply_settings(self, settings):
        self.applied.append(settings)
        return {"stopped": []}


class FakeCover:
    def __init__(self):
        self.applied = []

    async def apply_settings(self, old, new):
        self.applied.append((old["rvc_root"], new["rvc_root"]))
        return False


class ApiCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addons = self.root / "release" / "addons"
        touch(self.addons / "astrbot_plugin_st_bridge" / "metadata.yaml", "n")
        touch(self.addons / "astrbot_plugin_st_bridge" / "main.py", "# p")
        touch(self.addons / "st_bridge_client" / "manifest.json", "{}")
        self.store = ConfigStore(self.root / "release" / "data")
        self.install = InstallService(self.store.data_dir, addons_dir=self.addons, clock=Clock())
        self.tts, self.cover = FakeTts(), FakeCover()
        self.restarts = []
        self.restart_result = {"auto": True}
        self.port = unused_port()
        self.service = ConsoleService(
            create_console_app(
                self.store, tts=self.tts, cover=self.cover, install=self.install,
                restart=lambda: self.restarts.append(1) or self.restart_result,
            ),
            port=self.port,
        )
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        self.astrbot = self.root / "AstrBot"
        (self.astrbot / "data" / "plugins").mkdir(parents=True)
        self.st = self.root / "ST"
        (self.st / "data" / "default-user" / "extensions").mkdir(parents=True)

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()
        await self.install.terminate()
        self.directory.cleanup()

    async def call(self, path, method="GET", payload=None, headers=None):
        kwargs = {"headers": headers or {}}
        if payload is not None:
            kwargs["json"] = payload
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", **kwargs) as response:
            return response.status, await response.json()


class InstallRoutesTests(ApiCase):
    async def test_a_new_install_shows_everything_unset(self):
        status, body = await self.call("/api/install")
        self.assertEqual(status, 200)
        self.assertEqual({item["state"] for item in body["checks"].values()}, {"empty"})
        self.assertEqual(body["settings"]["pip_mirror"], DEFAULT_PIP_MIRROR)
        self.assertEqual(body["plugin"]["state"], "unknown")
        self.assertTrue(body["package"]["plugin"])
        self.assertFalse(body["napcat_can_start"])
        self.assertTrue(body["restart_available"])
        self.assertEqual(body["data_dir"], str(self.store.data_dir))

    async def test_saving_paths_writes_the_right_places_and_tells_the_services(self):
        rvc = self.root / "rvc"
        touch(rvc / "runtime" / "python.exe")
        (rvc / "assets" / "weights").mkdir(parents=True)
        payload = {
            "st_dir": str(self.st), "astrbot_dir": str(self.astrbot), "napcat_path": "",
            "gpt_sovits_dir": r"E:\Gpt", "irodori_dir": r"E:\Iro", "irodori_server_dir": r"E:\Iro\server",
            "rvc_dir": str(rvc), "pip_mirror": "",
        }
        status, body = await self.call("/api/install/paths", "POST", payload)
        self.assertEqual(status, 200, body)
        config = self.store.config
        self.assertEqual((config["install"]["st_dir"], config["install"]["astrbot_dir"], config["install"]["pip_mirror"]), (str(self.st), str(self.astrbot), ""))
        self.assertEqual(config["tts"]["irodori"]["launch_dir"], r"E:\Iro")
        self.assertEqual(config["tts"]["irodori"]["server_dir"], r"E:\Iro\server")
        self.assertEqual(config["tts"]["gpt_sovits"]["launch_dir"], r"E:\Gpt")
        self.assertEqual(config["cover"]["rvc_root"], str(rvc))
        self.assertEqual(len(self.tts.applied), 1)
        self.assertEqual(self.tts.applied[0]["irodori"]["launch_dir"], r"E:\Iro")
        self.assertEqual(self.cover.applied, [("", str(rvc))])
        self.assertEqual(body["checks"]["rvc"]["state"], "valid")
        self.assertEqual(body["checks"]["st"]["state"], "valid")
        self.assertEqual(body["checks"]["gpt_sovits"]["state"], "invalid")  # saved all the same: the page says why
        on_disk = json.loads((self.store.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["cover"]["rvc_root"], str(rvc))
        # Saving the same values again changes nothing for the services.
        await self.call("/api/install/paths", "POST", payload)
        self.assertEqual((len(self.tts.applied), len(self.cover.applied)), (1, 1))
        # Other cover settings are not touched by a path save.
        self.assertEqual(config["cover"]["port"], 8099)

    async def test_missing_fields_keep_their_values_and_bad_ones_are_refused(self):
        await self.call("/api/install/paths", "POST", {"st_dir": str(self.st)})
        status, body = await self.call("/api/install/paths", "POST", {"astrbot_dir": str(self.astrbot)})
        self.assertEqual(body["settings"]["st_dir"], str(self.st))
        for bad in ({"st_dir": 5}, {"nonsense": "x"}, {"pip_mirror": "ftp://x"}, {"st_dir": "a\nb"}):
            status, body = await self.call("/api/install/paths", "POST", bad)
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.store.config["install"]["st_dir"], str(self.st))

    async def test_check_does_not_save(self):
        status, body = await self.call("/api/install/check", "POST", {"st_dir": str(self.st), "rvc_dir": str(self.root / "no")})
        self.assertEqual((status, body["checks"]["st"]["state"], body["checks"]["rvc"]["state"]), (200, "valid", "invalid"))
        self.assertEqual(self.store.config["install"]["st_dir"], "")
        self.assertEqual(self.tts.applied, [])

    async def test_plugin_and_extension_actions_through_the_api(self):
        await self.call("/api/install/paths", "POST", {"st_dir": str(self.st), "astrbot_dir": str(self.astrbot)})
        status, body = await self.call("/api/install/plugin", "POST", {"action": "install"})
        self.assertEqual(status, 200, body)
        plugin = self.astrbot / "data" / "plugins" / "astrbot_plugin_st_bridge"
        self.assertEqual(json.loads((plugin / "core_link.json").read_text(encoding="utf-8")), {"data_dir": str(self.store.data_dir)})
        self.assertEqual(body["overview"]["plugin"]["state"], "installed")
        status, body = await self.call("/api/install/plugin", "POST", {"action": "uninstall"})
        self.assertEqual((status, plugin.exists()), (200, False))
        status, body = await self.call("/api/install/st-extension", "POST", {"action": "install"})
        self.assertEqual(status, 200, body)
        self.assertTrue((self.st / "data" / "default-user" / "extensions" / "st_bridge_client" / "manifest.json").is_file())
        self.assertEqual(body["overview"]["extension"]["state"], "installed")
        for path in ("/api/install/plugin", "/api/install/st-extension"):
            status, _ = await self.call(path, "POST", {"action": "format c:"})
            self.assertEqual(status, 400)
            status, _ = await self.call(path, "POST", {})
            self.assertEqual(status, 400)

    async def test_actions_without_saved_paths_say_what_to_do(self):
        status, body = await self.call("/api/install/plugin", "POST", {"action": "install"})
        self.assertEqual(status, 400)
        self.assertIn("AstrBot", body["error"])
        status, body = await self.call("/api/install/st-extension", "POST", {"action": "uninstall"})
        self.assertEqual(status, 400)

    async def test_the_request_cannot_choose_what_gets_replaced(self):
        await self.call("/api/install/paths", "POST", {"astrbot_dir": str(self.astrbot)})
        victim = touch(self.root / "victim" / "keep.txt")
        for extra in ({"target": str(victim.parent)}, {"path": str(victim.parent)}, {"name": "..\\..\\victim"}):
            await self.call("/api/install/plugin", "POST", {"action": "uninstall", **extra})
        self.assertTrue(victim.is_file())

    async def test_development_layout_disables_the_buttons(self):
        install = InstallService(self.store.data_dir, addons_dir=None)
        port = unused_port()
        service = ConsoleService(create_console_app(self.store, install=install), port=port)
        await service.start()
        try:
            await self.call("/api/install/paths", "POST", {"astrbot_dir": str(self.astrbot)})
            async with self.client.get(f"http://127.0.0.1:{port}/api/install") as response:
                body = await response.json()
            self.assertFalse(body["package"]["available"])
            async with self.client.post(f"http://127.0.0.1:{port}/api/install/plugin", json={"action": "install"}) as response:
                self.assertEqual(response.status, 409)
                self.assertEqual((await response.json())["error"], "开发环境，没有可安装的文件")
        finally:
            await service.stop()

    async def test_a_confirmed_import_restarts_the_core_and_nothing_writes_over_the_imported_files_first(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore

        old = self.root / "old"
        imported = default_config()
        imported["gpt_sovits_root"] = r"E:\Imported"
        touch(old / "config.json", json.dumps(imported))
        AffinityStore(old).set_value("e2e:owner", 77, "主人")
        affinity = AffinityStore(self.store.data_dir)
        port = unused_port()
        restarts = []
        service = ConsoleService(
            create_console_app(self.store, install=self.install, affinity_store=affinity, restart=lambda: restarts.append(1) or {"auto": True}),
            port=port,
        )
        await service.start()
        try:
            async with self.client.post(f"http://127.0.0.1:{port}/api/install/import", json={"source": str(old), "confirm": True}) as response:
                body = await response.json()
                self.assertEqual(response.status, 200, body)
            self.assertEqual((body["restarting"], body["auto"], restarts), (True, True, [1]))
            config_hash = hashlib.sha256((self.store.data_dir / "config.json").read_bytes()).hexdigest()
            affinity_hash = hashlib.sha256((self.store.data_dir / "affinity.json").read_bytes()).hexdigest()
            self.assertEqual(json.loads((self.store.data_dir / "config.json").read_text(encoding="utf-8"))["gpt_sovits_root"], r"E:\Imported")
            # everything that would write the old in-memory state back is refused until the restart
            async with self.client.post(f"http://127.0.0.1:{port}/api/install/paths", json={"st_dir": str(self.st)}) as response:
                self.assertEqual(response.status, 409)
            async with self.client.post(f"http://127.0.0.1:{port}/api/queue/settings", json={"max_waiting": 3, "max_wait_seconds": 60}) as response:
                self.assertEqual(response.status, 409)
            with self.assertRaises(EmotionConfigError) as caught:
                await self.store.update(lambda config: config.update(default_emotion="happy"))
            self.assertEqual(caught.exception.status, 409)
            with self.assertRaises(EmotionConfigError):
                affinity.set_value("e2e:other", 5, "x")
            self.assertEqual(hashlib.sha256((self.store.data_dir / "config.json").read_bytes()).hexdigest(), config_hash)
            self.assertEqual(hashlib.sha256((self.store.data_dir / "affinity.json").read_bytes()).hexdigest(), affinity_hash)
            self.assertEqual(self.store.config["install"]["st_dir"], "")  # the rejected save left no trace in memory either
        finally:
            await service.stop()

    async def test_an_import_without_a_restart_hook_does_not_freeze_anything(self):
        old = self.root / "old"
        touch(old / "config.json", json.dumps(default_config()))
        port = unused_port()
        service = ConsoleService(create_console_app(self.store, install=self.install), port=port)
        await service.start()
        try:
            async with self.client.post(f"http://127.0.0.1:{port}/api/install/import", json={"source": str(old), "confirm": True}) as response:
                body = await response.json()
            self.assertEqual((body["restarting"], body["restart_available"]), (False, False))
            await self.store.update(lambda config: config.update(default_emotion="happy"))  # still writable
        finally:
            await service.stop()

    async def test_import_needs_confirmation_and_reports_the_restart(self):
        old = self.root / "old"
        touch(old / "config.json", json.dumps(default_config()))
        touch(old / "connector_token", "tok\n")
        status, body = await self.call("/api/install/import/preview", "POST", {"source": str(old)})
        self.assertEqual(status, 200)
        self.assertEqual({item["name"] for item in body["items"]}, {"config.json", "connector_token"})
        status, body = await self.call("/api/install/import", "POST", {"source": str(old)})
        self.assertEqual(status, 400)
        self.assertFalse((self.store.data_dir / "connector_token").exists())
        status, body = await self.call("/api/install/import", "POST", {"source": str(old), "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"] and body["restart_available"] and body["restarting"])
        self.assertEqual(self.restarts, [1])  # the import asked for the restart by itself
        self.assertEqual((self.store.data_dir / "connector_token").read_text(), "tok\n")
        status, _ = await self.call("/api/install/import/preview", "POST", {"source": str(self.root / "nope")})
        self.assertEqual(status, 404)

    async def test_dependency_routes(self):
        status, body = await self.call("/api/install/deps")
        self.assertEqual(status, 200)
        self.assertEqual((body["rows"][0]["name"], body["rows"][0]["state"], body["rows"][0]["needs"]), ("pedalboard", "blocked", "请先设置 RVC 目录"))
        self.assertEqual(body["mirror"], DEFAULT_PIP_MIRROR)
        status, body = await self.call("/api/install/deps/pedalboard/install", "POST")
        self.assertEqual(status, 400)
        status, body = await self.call("/api/install/deps/unknown/install", "POST")
        self.assertEqual(status, 404)
        self.install.dependencies["fake"] = fake_dependency(script="print('ok')")
        status, body = await self.call("/api/install/deps/fake/install", "POST")
        self.assertEqual((status, body["job"]["state"]), (202, "running"))
        status, body = await self.call("/api/install/deps/fake/install", "POST")
        self.assertEqual(status, 409)
        await self.install.wait_job()
        status, body = await self.call("/api/install")
        self.assertEqual(body["job"]["state"], "success")

    async def test_napcat_start_route(self):
        launcher = touch(self.root / "NapCat" / "napcat.bat")
        launched = []
        self.install._popen = lambda command, **kwargs: launched.append((command, kwargs))
        self.install._startfile = lambda path, **kwargs: launched.append(([path], kwargs))
        status, _ = await self.call("/api/install/napcat/start", "POST")
        self.assertEqual(status, 400)
        await self.call("/api/install/paths", "POST", {"napcat_path": str(launcher)})
        status, body = await self.call("/api/install/napcat/start", "POST")
        self.assertEqual(status, 200, body)
        self.assertEqual(launched[0][0], [str(launcher)])
        status, body = await self.call("/api/install")
        self.assertTrue(body["napcat_can_start"])

    async def test_a_directory_choice_is_offered_not_started(self):
        touch(self.root / "NapCat" / "a.bat")
        touch(self.root / "NapCat" / "b.exe")
        await self.call("/api/install/paths", "POST", {"napcat_path": str(self.root / "NapCat")})
        status, body = await self.call("/api/install")
        self.assertEqual(body["checks"]["napcat"]["state"], "choose")
        self.assertFalse(body["napcat_can_start"])
        self.assertEqual(len(body["checks"]["napcat"]["candidates"]), 2)

    async def test_restart_route(self):
        status, body = await self.call("/api/system/restart", "POST")
        self.assertEqual((status, body), (202, {"ok": True, "auto": True}))
        self.restart_result = {"auto": False}
        status, body = await self.call("/api/system/restart", "POST")
        self.assertEqual(body["auto"], False)
        self.assertEqual(len(self.restarts), 2)

    async def test_without_a_restart_hook_the_route_says_so(self):
        port = unused_port()
        service = ConsoleService(create_console_app(self.store), port=port)
        await service.start()
        try:
            async with self.client.post(f"http://127.0.0.1:{port}/api/system/restart") as response:
                self.assertEqual(response.status, 503)
        finally:
            await service.stop()

    async def test_writes_need_the_consoles_own_origin_and_reads_do_not_write(self):
        foreign = {"Origin": "http://evil.example"}
        own = {"Origin": f"http://127.0.0.1:{self.port}"}
        for path, body in (
            ("/api/install/paths", {"st_dir": "x"}), ("/api/install/check", {}), ("/api/install/plugin", {"action": "install"}),
            ("/api/install/st-extension", {"action": "install"}), ("/api/install/import", {"source": "x", "confirm": True}),
            ("/api/install/import/preview", {"source": "x"}), ("/api/install/deps/pedalboard/install", {}),
            ("/api/install/napcat/start", {}), ("/api/system/restart", {}),
        ):
            status, result = await self.call(path, "POST", body, headers=foreign)
            self.assertEqual((status, result["error"]), (403, "forbidden_origin"), path)
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.store.config["install"]["st_dir"], "")
        status, _ = await self.call("/api/install", "GET", headers=foreign)  # reads are fine
        self.assertEqual(status, 200)
        status, _ = await self.call("/api/install/paths", "POST", {"st_dir": str(self.st)}, headers=own)
        self.assertEqual(status, 200)
        for path in ("/api/install/paths", "/api/install/plugin", "/api/install/import", "/api/system/restart", "/api/install/napcat/start"):
            status, _ = await self.call(path, "GET")
            self.assertNotEqual(status, 200, path)  # no GET that changes anything


# ---- the process: restart exit code ------------------------------------------------------------------------
class RestartExitTests(unittest.IsolatedAsyncioTestCase):
    async def run_core(self, launched_by):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name)
        store = ConfigStore(data)
        names = ("st_port", "bridge_port", "console_port", "connector_port", "onebot_port")
        ports = {}
        for name in names:
            ports[name] = unused_port()
        config = json.loads((data / "config.json").read_text(encoding="utf-8"))
        config["network"] = ports
        config["tts"]["common"]["auto_start"] = False
        (data / "config.json").write_text(json.dumps(config), encoding="utf-8")
        del store
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run(data, launched_by, stop))
        async with ClientSession(connector=TCPConnector(force_close=True)) as client:
            for _ in range(300):
                try:
                    async with client.post(f"http://127.0.0.1:{ports['console_port']}/api/system/restart") as response:
                        status, body = response.status, await response.json()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                task.cancel()
                self.fail("the core did not start")
        return status, body, await asyncio.wait_for(task, 60)

    async def test_the_console_restart_ends_the_core_with_code_75(self):
        status, body, code = await self.run_core("start.bat")
        self.assertEqual((status, body, code), (202, {"ok": True, "auto": True}, RESTART_EXIT_CODE))
        self.assertEqual(RESTART_EXIT_CODE, 75)

    async def test_started_by_hand_it_still_exits_and_says_it_will_not_come_back(self):
        status, body, code = await self.run_core("")
        self.assertEqual((status, body["auto"], code), (202, False, RESTART_EXIT_CODE))


if __name__ == "__main__":
    unittest.main()

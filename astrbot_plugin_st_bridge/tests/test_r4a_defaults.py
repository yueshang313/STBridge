"""R4a: no personal paths or model names in the defaults. Empty means "not set up"; saved values stay as they are."""

import json
import re
import os
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, FormData, TCPConnector

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import (
    auto_index,
    cover_settings,
    default_cover_settings,
    list_models,
    list_separation_models,
    validate_cover_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import (
    RVC_ROOT_NOT_SET_TEXT,
    CoverError,
    CoverService,
    probe_duration,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    default_config,
    default_tts_settings,
    validate_tts_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import DIR_NOT_SET_TEXT, ServiceManager, ServiceStartError
from test_cover_service import FakeLogger

OLD_PATHS = {
    "rvc_root": r"E:\Tools\RVC",
    "model": "kotoneV2_e150_s15450.pth",
}
OLD_IRODORI = {
    "launch_dir": r"D:\Irodori-TTS",
    "server_dir": r"D:\Irodori-TTS\Irodori-TTS-Server",
    "lora_adapter": r"D:\Irodori-TTS\lora\kotone_lora\small",
}
OLD_SOVITS = r"E:\Tools\GPT-SoVITS"


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DefaultsAreEmptyTests(unittest.TestCase):
    def test_cover_defaults(self):
        settings = default_cover_settings()
        self.assertEqual((settings["rvc_root"], settings["model"]), ("", ""))
        self.assertEqual(validate_cover_settings({})["rvc_root"], "")  # empty validates: "not set up"
        self.assertEqual(cover_settings({})["rvc_root"], "")

    def test_tts_defaults(self):
        tts = default_tts_settings()
        self.assertEqual([tts["irodori"][key] for key in ("launch_dir", "server_dir", "lora_adapter")], ["", "", ""])
        self.assertEqual(tts["gpt_sovits"]["launch_dir"], "")
        # The public model name is not personal and stays.
        self.assertEqual(tts["irodori"]["hf_checkpoint"], "Aratako/Irodori-TTS-v4.1-Small")
        checked = validate_tts_settings(tts)
        self.assertEqual((checked["irodori"]["launch_dir"], checked["gpt_sovits"]["launch_dir"]), ("", ""))

    def test_a_new_install_gets_empty_values_and_no_personal_text_anywhere_in_the_code(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            text = (Path(directory) / "config.json").read_text(encoding="utf-8")
        self.assertEqual((store.config["cover"]["rvc_root"], store.config["cover"]["model"]), ("", ""))
        for needle in ("D:\\\\RVC", "D:\\\\Irodori", "D:\\\\GPT-SoVITS", "kotoneV2_e150"):
            self.assertNotIn(needle, text)
        sources = [PLUGIN_ROOT / "st_bridge_core" / name for name in ("cover_config.py", "emotion_config.py")]
        sources.append(PLUGIN_ROOT / "st_bridge_core" / "console_frontend" / "js" / "logic.js")
        for path in sources:
            body = path.read_text(encoding="utf-8")
            for needle in ("D:\\RVC", "D:\\Irodori", "D:\\GPT-SoVITS"):
                self.assertNotIn(needle, body, f"{needle} in {path.name}")
            # no user profile path of anybody's machine
            self.assertIsNone(re.search(r"[A-Za-z]:\\Users\\", body, re.IGNORECASE), f"a user profile path in {path.name}")


class SavedValuesStayTests(unittest.TestCase):
    def write_config(self, directory, mutate):
        root = Path(directory)
        ConfigStore(root)  # a complete config first
        path = root / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        mutate(config)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return root

    def test_an_existing_config_keeps_every_personal_value(self):
        def mutate(config):
            config["cover"].update(OLD_PATHS)
            config["tts"]["irodori"].update(OLD_IRODORI)
            config["tts"]["gpt_sovits"]["launch_dir"] = OLD_SOVITS

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(self.write_config(directory, mutate))
            config = store.config
            self.assertEqual({key: config["cover"][key] for key in OLD_PATHS}, OLD_PATHS)
            self.assertEqual({key: config["tts"]["irodori"][key] for key in OLD_IRODORI}, OLD_IRODORI)
            self.assertEqual(config["tts"]["gpt_sovits"]["launch_dir"], OLD_SOVITS)
            self.assertEqual(cover_settings(config)["rvc_root"], OLD_PATHS["rvc_root"])
            checked = validate_tts_settings(config["tts"])
            self.assertEqual(checked["irodori"]["server_dir"], OLD_IRODORI["server_dir"])
            self.assertEqual(checked["gpt_sovits"]["launch_dir"], OLD_SOVITS)
            # Nothing was rewritten with other values.
            on_disk = json.loads((Path(directory) / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["cover"]["rvc_root"], OLD_PATHS["rvc_root"])

    def test_keys_missing_from_an_old_config_are_filled_with_empty_not_with_the_old_personal_path(self):
        def mutate(config):
            del config["cover"]["rvc_root"], config["cover"]["model"]
            del config["tts"]["irodori"]["launch_dir"], config["tts"]["irodori"]["lora_adapter"]
            del config["tts"]["gpt_sovits"]["launch_dir"]

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(self.write_config(directory, mutate))
            self.assertEqual((store.config["cover"]["rvc_root"], store.config["cover"]["model"]), ("", ""))
            self.assertEqual(store.config["tts"]["irodori"]["launch_dir"], "")
            self.assertEqual(store.config["tts"]["gpt_sovits"]["launch_dir"], "")

    def test_a_blank_saved_directory_stays_blank(self):
        tts = default_tts_settings()
        tts["irodori"]["launch_dir"] = "   "
        tts["gpt_sovits"]["launch_dir"] = ""
        checked = validate_tts_settings(tts)
        self.assertEqual((checked["irodori"]["launch_dir"], checked["gpt_sovits"]["launch_dir"]), ("", ""))

    def test_a_preset_never_changes_this_machines_paths(self):
        # R4b: the folders belong to the machine. A preset (even an old one that carries them) leaves the local ones alone.
        local = default_config()
        local["cover"].update(OLD_PATHS)
        local["tts"]["irodori"].update(OLD_IRODORI)
        local["tts"]["gpt_sovits"]["launch_dir"] = OLD_SOVITS
        preset = default_config()
        preset["cover"]["rvc_root"] = r"E:\Theirs\RVC"
        preset["tts"]["irodori"].update(launch_dir=r"E:\Theirs\Iro", server_dir=r"E:\Theirs\Iro\s")
        preset["tts"]["gpt_sovits"]["launch_dir"] = r"E:\Theirs\Gpt"
        result = config_presets.normalize_config(preset, local)
        self.assertEqual(result["cover"]["rvc_root"], OLD_PATHS["rvc_root"])
        self.assertEqual({key: result["tts"]["irodori"][key] for key in OLD_IRODORI if key != "lora_adapter"}, {k: v for k, v in OLD_IRODORI.items() if k != "lora_adapter"})
        self.assertEqual(result["tts"]["gpt_sovits"]["launch_dir"], OLD_SOVITS)
        # a machine with no paths set stays that way
        bare = config_presets.normalize_config(preset, default_config())
        self.assertEqual((bare["cover"]["rvc_root"], bare["tts"]["irodori"]["launch_dir"], bare["tts"]["gpt_sovits"]["launch_dir"]), ("", "", ""))


class EmptyRvcRootTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = ConfigStore(self.root / "data")
        self.logger = FakeLogger()
        self.cover = CoverService(
            data_dir=self.root / "data",
            get_settings=lambda: cover_settings(self.store.config),
            logger=self.logger,
            worker_script=self.root / "worker.py",
        )
        self.port = _unused_port()
        self.service = ConsoleService(create_console_app(self.store, cover=self.cover), port=self.port)
        await self.service.start()
        self.client = ClientSession(connector=TCPConnector(force_close=True))
        # A folder that looks like an RVC folder sits in the current directory: it must never be picked up.
        self.previous_cwd = os.getcwd()
        self.trap = self.root / "trap"
        (self.trap / "assets" / "weights").mkdir(parents=True)
        (self.trap / "assets" / "weights" / "trap.pth").write_bytes(b"w")
        (self.trap / "runtime").mkdir()
        (self.trap / "runtime" / "python.exe").write_bytes(b"")
        os.chdir(self.trap)

    async def asyncTearDown(self):
        os.chdir(self.previous_cwd)
        await self.client.close()
        await self.service.stop()
        self.directory.cleanup()

    async def call(self, path, method="GET", payload=None):
        kwargs = {"json": payload} if payload is not None else {}
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", **kwargs) as response:
            return response.status, await response.json()

    def test_the_scanners_return_nothing_for_an_empty_root(self):
        self.assertEqual(list_models(""), [])
        self.assertTrue(all(not entry["available"] for table in list_separation_models("").values() for entry in table))
        self.assertEqual(auto_index("", "x.pth"), "")
        self.assertEqual(probe_duration("", Path("a.mp3")), 0.0)

    async def test_the_cover_page_loads_and_says_the_folder_is_not_set(self):
        status, body = await self.call("/api/cover")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["rvc_root"], "")
        self.assertEqual((body["models"], body["auto_index"]), ([], ""))  # not the trap folder in the cwd
        self.assertEqual(body["service"]["state"], "stopped")
        self.assertEqual(body["service"]["message"], RVC_ROOT_NOT_SET_TEXT)
        self.assertEqual(RVC_ROOT_NOT_SET_TEXT, "未设置 RVC 目录")

    async def test_saving_an_empty_folder_is_accepted_and_starting_the_service_is_not_a_500(self):
        status, body = await self.call("/api/cover/settings", "POST", {**self.store.config["cover"], "rvc_root": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["rvc_root"], "")
        status, body = await self.call("/api/cover/service/start", "POST")
        self.assertEqual((status, body["error"]), (502, DIR_NOT_SET_TEXT))  # the usual refusal, not a 500
        status, body = await self.call("/api/cover")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"]["message"], RVC_ROOT_NOT_SET_TEXT)  # nothing was launched from the cwd

    async def test_adding_a_song_and_singing_say_what_is_missing(self):
        form = FormData()
        form.add_field("file", b"fLaC" + b"\x00" * 64, filename="a.flac")
        async with self.client.post(f"http://127.0.0.1:{self.port}/api/cover/songs/upload", data=form) as response:
            self.assertEqual(response.status, 400)
            self.assertIn("RVC folder is not set", (await response.json())["error"])
        self.assertEqual(self.cover.library.list(), [])
        song = self.cover.library.add("b.flac", b"fLaC" + b"\x00" * 64, {"title": "b"})
        await self.store.update(lambda config: config["cover"].update(model="m.pth"))
        with self.assertRaises(CoverError) as caught:
            await self.cover.sing(song["id"])
        self.assertEqual(caught.exception.kind, "RvcRootNotSet")


class EmptyLaunchDirTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        self.logger = FakeLogger()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ServiceManager(
            host="127.0.0.1", port=_unused_port(), launch_dir=kwargs.pop("launch_dir", ""), start_timeout=5,
            stop_on_exit=False, data_dir=Path(directory.name), logger=self.logger, **kwargs,
        )

    async def test_an_empty_directory_is_reported_and_nothing_is_launched(self):
        launches = []
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as trap:
            (Path(trap) / "runtime").mkdir()
            (Path(trap) / "runtime" / "python.exe").write_bytes(b"")
            os.chdir(trap)
            self.addCleanup(os.chdir, previous)
            manager = self.make(launcher=lambda *args: launches.append(args))
            status = await manager.status()
            self.assertEqual((status["state"], status["message"]), ("stopped", DIR_NOT_SET_TEXT))
            with self.assertRaises(ServiceStartError) as caught:
                await manager.ensure_started()
            self.assertIn(DIR_NOT_SET_TEXT, str(caught.exception))
            status = await manager.status()
            self.assertEqual((status["state"], status["message"]), ("failed", DIR_NOT_SET_TEXT))
            os.chdir(previous)
        self.assertEqual(launches, [])
        self.assertEqual(DIR_NOT_SET_TEXT, "未设置目录")

    async def test_a_set_directory_that_does_not_exist_still_says_so(self):
        manager = self.make(launch_dir=r"Z:\nowhere")
        with self.assertRaises(ServiceStartError) as caught:
            await manager.ensure_started()
        self.assertIn("启动目录不存在", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

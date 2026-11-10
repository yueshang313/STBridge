import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore, EmotionConfigError, default_config
from test_console_server import _unused_loopback_port


class FakeTts:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def apply_settings(self, settings):
        self.calls.append(copy.deepcopy(settings))
        if self.fail:
            raise RuntimeError("boom")
        return {"stopped": ["irodori"]}


class FakeCover:
    def __init__(self):
        self.calls = []

    async def apply_settings(self, old, new):
        self.calls.append((copy.deepcopy(old), copy.deepcopy(new)))
        return True


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 12, 0, 0)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


def _config_with_secrets():
    config = default_config()
    config["network"] = {"st_port": 12000, "bridge_port": 12001, "console_port": 12002}
    config["cover"]["music_api_key"] = "SECRET-KEY-123"
    config["texts_notice"] = {"replaced": ["x"], "backup": "config.json.bak-g1"}
    return config


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_leaves_out_ports_key_and_notice(self):
        config = _config_with_secrets()
        shot = config_presets.snapshot(config)
        self.assertNotIn("network", shot)
        self.assertNotIn("texts_notice", shot)
        self.assertNotIn("music_api_key", shot["cover"])
        self.assertNotIn("SECRET-KEY-123", json.dumps(shot))
        self.assertEqual(config["cover"]["music_api_key"], "SECRET-KEY-123")  # the source is untouched
        without_paths = copy.deepcopy(config["tts"])  # the folders of this machine's TTS installs stay out of a preset
        for group, field in config_presets.EXCLUDED_TTS_FIELDS:
            del without_paths[group][field]
        self.assertEqual(shot["tts"], without_paths)

    def test_normalize_keeps_the_local_network_and_key(self):
        local = _config_with_secrets()
        other = default_config()
        other["network"] = {"st_port": 1111, "bridge_port": 1112, "console_port": 1113}
        other["cover"]["music_api_key"] = "THEIR-KEY"
        other["queue"]["max_waiting"] = 3
        result = config_presets.normalize_config(other, local)
        self.assertEqual(result["network"], local["network"])
        self.assertEqual(result["cover"]["music_api_key"], "SECRET-KEY-123")
        self.assertEqual(result["queue"]["max_waiting"], 3)
        self.assertNotIn("texts_notice", result)

    def test_an_old_structure_goes_through_the_g1_migration(self):
        old = default_config()
        for section in ("network", "texts"):
            del old[section]
        old["characters"] = {"琴音": {"emotions": {}}}
        old["default_character"] = "琴音"
        old["affinity"] = {**old["affinity"], "character": "琴音", "display_name": "琴音酱"}
        old["song_request"]["accept_texts"] = ["好的，唱《{歌名}》"]
        result = config_presets.normalize_config(old, default_config())
        self.assertEqual(result["characters"]["琴音"]["nickname"], "琴音酱")
        self.assertNotIn("display_name", result["affinity"])
        self.assertIn("texts", result)
        self.assertIn("network", result)

    def test_an_invalid_config_is_a_400(self):
        broken = default_config()
        broken["queue"]["max_waiting"] = "many"
        with self.assertRaises(EmotionConfigError) as caught:
            config_presets.normalize_config(broken, default_config())
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(EmotionConfigError):
            config_presets.normalize_config({"version": 2}, default_config())

    def test_port_clash_with_the_local_network_is_refused(self):
        local = _config_with_secrets()
        clash = default_config()
        clash["tts"]["irodori"]["port"] = 12001  # the local bridge port
        with self.assertRaises(EmotionConfigError):
            config_presets.normalize_config(clash, local)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.presets = config_presets.PresetStore(Path(self.directory.name), clock=self.clock)
        self.config = _config_with_secrets()

    def test_save_lists_and_makes_current(self):
        self.assertEqual(self.presets.list(self.config)["presets"], [])
        summary = self.presets.save_new("日常", self.config)
        listing = self.presets.list(self.config)
        self.assertEqual(listing["current"], summary["id"])
        self.assertEqual(listing["current_name"], "日常")
        self.assertIs(listing["modified"], False)
        self.config["queue"]["max_waiting"] = 2
        self.assertIs(self.presets.list(self.config)["modified"], True)

    def test_the_file_is_the_export_and_holds_no_secret(self):
        summary = self.presets.save_new("日常", self.config)
        raw, name = self.presets.export_bytes(summary["id"])
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(name, "日常")
        self.assertEqual(document["format"], "stbridge-preset")
        self.assertEqual(document["format_version"], 1)
        self.assertNotIn("SECRET-KEY-123", raw.decode("utf-8"))
        self.assertNotIn("network", document["config"])
        files = sorted(path.name for path in (Path(self.directory.name) / "presets").iterdir())
        self.assertIn(summary["id"] + ".json", files)

    def test_bad_names_and_duplicates_are_refused(self):
        for bad in ("", "   ", "../x", "a/b", "a\\b", "x" * 41, None, 5, "tab\there"):
            with self.assertRaises(EmotionConfigError, msg=repr(bad)) as caught:
                self.presets.save_new(bad, self.config)
            self.assertEqual(caught.exception.status, 400)
        self.presets.save_new("日常", self.config)
        with self.assertRaises(EmotionConfigError):
            self.presets.save_new("日常", self.config)
        self.presets.save_new("x" * 40, self.config)

    def test_ids_are_checked(self):
        for bad in ("../config", "p1", "pzzzzzzzzzzzzzzzz", "", "p0123456789abcdef/../x"):
            with self.assertRaises(EmotionConfigError) as caught:
                self.presets.load(bad)
            self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(EmotionConfigError):
            self.presets.load("p0123456789abcdef")

    def test_rename_overwrite_and_delete(self):
        first = self.presets.save_new("A", self.config)
        second = self.presets.save_new("B", self.config)
        with self.assertRaises(EmotionConfigError):
            self.presets.update(first["id"], name="B")
        self.presets.update(first["id"], name="C")
        self.config["queue"]["max_waiting"] = 2
        self.presets.update(first["id"], config=self.config)
        listing = self.presets.list(self.config)
        self.assertEqual(listing["current"], first["id"])
        self.assertIs(listing["modified"], False)
        self.assertEqual([item["name"] for item in listing["presets"]], ["C", "B"])
        self.presets.delete(first["id"])
        listing = self.presets.list(self.config)
        self.assertIsNone(listing["current"])
        self.assertIsNone(listing["modified"])
        self.assertEqual(len(listing["presets"]), 1)
        with self.assertRaises(EmotionConfigError):
            self.presets.delete(first["id"])
        self.assertEqual(second["id"], listing["presets"][0]["id"])

    def test_archives_keep_the_newest_five_and_leave_user_presets(self):
        mine = self.presets.save_new("日常", self.config)
        ids = [self.presets.archive(self.config)["id"] for _ in range(7)]
        listing = self.presets.list(self.config)
        self.assertEqual(len(listing["autos"]), 5)
        self.assertEqual([item["id"] for item in listing["autos"]], ids[:1:-1])
        self.assertTrue(all(item["name"].startswith("切换前-2026-11-04 ") for item in listing["autos"]))
        self.assertEqual([item["id"] for item in listing["presets"]], [mine["id"]])

    def test_a_damaged_file_is_marked_not_fatal(self):
        good = self.presets.save_new("日常", self.config)
        broken = Path(self.directory.name) / "presets" / "p0123456789abcdef.json"
        broken.write_text("{not json", encoding="utf-8")
        listing = self.presets.list(self.config)
        self.assertEqual(len(listing["presets"]), 2)
        self.assertEqual([item["broken"] for item in listing["presets"] if item["id"] == "p0123456789abcdef"], [True])
        self.assertEqual(listing["current"], good["id"])
        self.presets.delete("p0123456789abcdef")

    def test_import_rules(self):
        document = {
            "format": "stbridge-preset", "format_version": 1, "name": "朋友的", "kind": "auto",
            "config": {**copy.deepcopy(default_config()), "network": {"st_port": 1, "bridge_port": 2, "console_port": 3}},
        }
        document["config"]["cover"]["music_api_key"] = "THEIR-KEY"
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        first = self.presets.import_document(raw, self.config)
        second = self.presets.import_document(raw, self.config)
        self.assertEqual(first["name"], "朋友的")
        self.assertEqual(second["name"], "朋友的 (2)")
        self.assertEqual(first["kind"], "user")
        stored = self.presets.load(first["id"])
        self.assertNotIn("network", stored["config"])
        self.assertNotIn("THEIR-KEY", json.dumps(stored))
        self.assertIsNone(self.presets.current_id())  # imported, not applied
        for bad, status in (
            (b"not json", 400),
            (json.dumps({"format": "other"}).encode(), 400),
            (json.dumps({**document, "format_version": 2}).encode(), 400),
            (json.dumps({**document, "config": []}).encode(), 400),
            (json.dumps({**document, "config": {"version": 1, "models": [], "characters": {}}}).encode(), 400),
            (b"x" * (config_presets.MAX_IMPORT_BYTES + 1), 413),
        ):
            with self.assertRaises(EmotionConfigError, msg=bad[:30]) as caught:
                self.presets.import_document(bad, self.config)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(len(self.presets.list(self.config)["presets"]), 2)

    def test_missing_paths_lists_every_kind_of_path_field(self):
        config = default_config()
        with tempfile.TemporaryDirectory() as present:
            config["gpt_sovits_root"] = present
            config["models"] = {"m": {"gpt_model_path": "Z:/nope/g.ckpt", "sovits_model_path": present,
                                      "reference_audio_root": "Z:/nope/audio"}}
            config["characters"] = {"c": {"emotions": {"happy": {"ref_audio_path": "Z:/nope/a.wav"}}}}
            config["tts"]["gpt_sovits"]["ref_audio_path"] = "Z:/nope/r.wav"
            config["cover"]["rvc_root"] = "Z:/nope/rvc"
            config["tts"]["irodori"]["launch_dir"] = "Z:/nope/irodori"
            found = {item["field"]: item["path"] for item in config_presets.missing_paths(config)}
        self.assertNotIn("gpt_sovits_root", found)
        self.assertIn("models.m.gpt_model_path", found)
        self.assertIn("models.m.sovits_model_path", found)  # a directory where a file is expected
        self.assertIn("models.m.reference_audio_root", found)
        self.assertIn("characters.c.emotions.happy.ref_audio_path", found)
        self.assertIn("tts.gpt_sovits.ref_audio_path", found)
        self.assertIn("cover.rvc_root", found)
        self.assertIn("tts.irodori.launch_dir", found)

    def test_unset_paths_are_not_reported_as_missing(self):
        found = {item["field"] for item in config_presets.missing_paths(default_config())}
        for field in ("cover.rvc_root", "tts.irodori.launch_dir", "tts.irodori.server_dir", "tts.gpt_sovits.launch_dir"):
            self.assertNotIn(field, found)


class ConsoleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = _config_with_secrets()
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.store = ConfigStore(self.root)
        self.tts = FakeTts()
        self.cover = FakeCover()
        self.port = _unused_loopback_port()
        self.service = ConsoleService(create_console_app(self.store, tts=self.tts, cover=self.cover), port=self.port)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None, data=None):
        kwargs = {"json": payload} if data is None else {"data": data}
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", **kwargs) as response:
            text = await response.text()
            return response.status, (json.loads(text) if text.startswith("{") else text), response

    def disk_config(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))

    async def test_empty_first_run_creates_nothing(self):
        status, body, _ = await self.call("/api/presets")
        self.assertEqual(status, 200)
        self.assertEqual(body["presets"], [])
        self.assertEqual(body["autos"], [])
        self.assertIsNone(body["current"])
        self.assertFalse((self.root / "presets").exists())

    async def test_apply_swaps_settings_keeps_network_and_key_and_runs_side_effects(self):
        status, saved, _ = await self.call("/api/presets", "POST", {"name": "原样"})
        self.assertEqual(status, 200)
        # Change things, then go back.
        await self.call("/api/queue/settings", "POST", {**self.store.config["queue"], "max_waiting": 2})
        self.assertEqual(self.store.config["queue"]["max_waiting"], 2)
        _, listing, _ = await self.call("/api/presets")
        self.assertTrue(listing["modified"])
        before_calls = len(self.tts.calls)
        status, body, _ = await self.call(f"/api/presets/{saved['preset']['id']}/apply", "POST", {})
        self.assertEqual(status, 200, body)
        self.assertNotEqual(self.store.config["queue"]["max_waiting"], 2)
        self.assertEqual(self.store.config["network"], {"st_port": 12000, "bridge_port": 12001, "console_port": 12002})
        self.assertEqual(self.store.config["cover"]["music_api_key"], "SECRET-KEY-123")
        self.assertEqual(self.disk_config()["cover"]["music_api_key"], "SECRET-KEY-123")
        self.assertEqual(len(self.tts.calls), before_calls + 1)
        self.assertEqual(self.tts.calls[-1], self.store.config["tts"])
        self.assertEqual(len(self.cover.calls), 1)
        self.assertEqual(self.cover.calls[0][1]["music_api_key"], "SECRET-KEY-123")
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["notes"]), 2)
        self.assertEqual(body["archive"]["kind"], "auto")
        self.assertTrue(body["archive"]["name"].startswith("切换前-"))
        self.assertEqual(body["list"]["current"], saved["preset"]["id"])
        self.assertIs(body["list"]["modified"], False)
        # the archive holds the settings from before the switch
        archive = (await self.call(f"/api/presets/{body['archive']['id']}/export"))[1]
        self.assertEqual(archive["config"]["queue"]["max_waiting"], 2)

    async def test_a_failing_side_effect_is_a_warning_not_a_rollback(self):
        _, saved, _ = await self.call("/api/presets", "POST", {"name": "原样"})
        await self.call("/api/queue/settings", "POST", {**self.store.config["queue"], "max_waiting": 2})
        self.tts.fail = True
        status, body, _ = await self.call(f"/api/presets/{saved['preset']['id']}/apply", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["warnings"]), 1)
        self.assertNotEqual(self.store.config["queue"]["max_waiting"], 2)

    async def test_an_invalid_preset_changes_nothing_and_leaves_no_archive(self):
        _, saved, _ = await self.call("/api/presets", "POST", {"name": "坏的"})
        path = self.root / "presets" / (saved["preset"]["id"] + ".json")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["config"]["queue"]["max_waiting"] = "many"
        path.write_text(json.dumps(document), encoding="utf-8")
        before = self.disk_config()
        status, body, _ = await self.call(f"/api/presets/{saved['preset']['id']}/apply", "POST", {})
        self.assertEqual(status, 400)
        self.assertEqual(self.disk_config(), before)
        self.assertEqual(self.tts.calls, [])
        _, listing, _ = await self.call("/api/presets")
        self.assertEqual(listing["autos"], [])

    async def test_a_preset_from_before_g1_applies(self):
        old = default_config()
        del old["network"], old["texts"]
        old["characters"] = {"琴音": {"emotions": {}}}
        old["default_character"] = "琴音"
        old["affinity"] = {**old["affinity"], "character": "琴音", "display_name": "琴音酱"}
        document = {"format": "stbridge-preset", "format_version": 1, "name": "旧版", "config": old}
        status, body, _ = await self.call("/api/presets/import", "POST", data=json.dumps(document, ensure_ascii=False).encode())
        self.assertEqual(status, 200, body)
        status, applied, _ = await self.call(f"/api/presets/{body['preset']['id']}/apply", "POST", {})
        self.assertEqual(status, 200, applied)
        self.assertEqual(self.store.config["characters"]["琴音"]["nickname"], "琴音酱")
        self.assertEqual(self.store.config["network"]["bridge_port"], 12001)

    async def test_import_reports_missing_paths_and_does_not_apply(self):
        config = default_config()
        config["tts"]["gpt_sovits"]["ref_audio_path"] = "Z:/nope/r.wav"
        document = {"format": "stbridge-preset", "format_version": 1, "name": "朋友", "config": config}
        status, body, _ = await self.call("/api/presets/import", "POST", data=json.dumps(document).encode())
        self.assertEqual(status, 200, body)
        self.assertIn("tts.gpt_sovits.ref_audio_path", [item["field"] for item in body["missing_paths"]])
        self.assertEqual(self.store.config["network"]["bridge_port"], 12001)
        self.assertEqual(self.tts.calls, [])
        _, listing, _ = await self.call("/api/presets")
        self.assertIsNone(listing["current"])
        self.assertEqual([item["name"] for item in listing["presets"]], ["朋友"])
        for payload, expected in ((b"x" * (config_presets.MAX_IMPORT_BYTES + 5), 413), (b"[1]", 400), (b"nope", 400)):
            status, _, _ = await self.call("/api/presets/import", "POST", data=payload)
            self.assertEqual(status, expected)

    async def test_a_large_preset_file_is_imported_whole(self):
        # Well past one TCP / HTTP chunk: the body must be read to its end, not just what is buffered.
        config = default_config()
        config["characters"] = {
            f"角色{index}": {"emotions": {f"e{n}": {"caption": "长" * 250, "prompt_text": "话" * 1500} for n in range(8)}}
            for index in range(20)
        }
        document = {"format": "stbridge-preset", "format_version": 1, "name": "很大的预设", "config": config}
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(raw), 300 * 1024)
        self.assertLess(len(raw), config_presets.MAX_IMPORT_BYTES)
        status, body, _ = await self.call("/api/presets/import", "POST", data=raw)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["preset"]["name"], "很大的预设")
        stored = json.loads((self.root / "presets" / (body["preset"]["id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(len(stored["config"]["characters"]), 20)
        self.assertEqual(stored["config"]["characters"]["角色19"]["emotions"]["e7"]["prompt_text"], "话" * 1500)

    async def test_a_chunked_upload_without_content_length_over_the_limit_is_413(self):
        async def chunks():
            piece = b" " * (64 * 1024)
            for _ in range(20):  # 1.25 MB
                yield piece

        status, body, _ = await self.call("/api/presets/import", "POST", data=chunks())
        self.assertEqual(status, 413, body)
        _, listing, _ = await self.call("/api/presets")
        self.assertEqual(listing["presets"], [])

    async def test_export_download_name_is_rfc5987_and_ids_are_checked(self):
        _, saved, _ = await self.call("/api/presets", "POST", {"name": "日常 设置"})
        status, text, response = await self.call(f"/api/presets/{saved['preset']['id']}/export")
        self.assertEqual(status, 200)
        disposition = response.headers["Content-Disposition"]
        self.assertIn("filename*=UTF-8''%E6%97%A5%E5%B8%B8%20%E8%AE%BE%E7%BD%AE.json", disposition)
        self.assertEqual(text["name"], "日常 设置")
        for bad in ("p1", "..%2Fconfig", "p0123456789abcdef"):
            for method, suffix in (("GET", "/export"), ("POST", "/apply"), ("DELETE", ""), ("PUT", "")):
                status, _, _ = await self.call(f"/api/presets/{bad}{suffix}", method, {"name": "x"})
                self.assertIn(status, (404, 400), (bad, method))

    async def test_save_rename_overwrite_delete_over_http(self):
        status, _, _ = await self.call("/api/presets", "POST", {"name": "../evil"})
        self.assertEqual(status, 400)
        status, first, _ = await self.call("/api/presets", "POST", {"name": "A"})
        status, _, _ = await self.call("/api/presets", "POST", {"name": "A"})
        self.assertEqual(status, 400)
        pid = first["preset"]["id"]
        status, _, _ = await self.call(f"/api/presets/{pid}", "PUT", {"name": "B"})
        self.assertEqual(status, 200)
        status, _, _ = await self.call(f"/api/presets/{pid}", "PUT", {"use_current": "yes"})
        self.assertEqual(status, 400)
        await self.call("/api/queue/settings", "POST", {**self.store.config["queue"], "max_waiting": 2})
        status, _, _ = await self.call(f"/api/presets/{pid}", "PUT", {"use_current": True})
        self.assertEqual(status, 200)
        _, listing, _ = await self.call("/api/presets")
        self.assertEqual(listing["presets"][0]["name"], "B")
        self.assertFalse(listing["modified"])
        status, _, _ = await self.call(f"/api/presets/{pid}", "DELETE")
        self.assertEqual(status, 200)
        _, listing, _ = await self.call("/api/presets")
        self.assertEqual(listing["presets"], [])
        self.assertIsNone(listing["current"])

    async def test_a_damaged_file_does_not_break_the_list(self):
        await self.call("/api/presets", "POST", {"name": "A"})
        (self.root / "presets" / "p0123456789abcdef.json").write_text("garbage", encoding="utf-8")
        status, listing, _ = await self.call("/api/presets")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["presets"]), 2)
        status, _, _ = await self.call("/api/presets/p0123456789abcdef/apply", "POST", {})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()

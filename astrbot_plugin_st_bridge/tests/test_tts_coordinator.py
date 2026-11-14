import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import default_tts_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_coordinator import TtsCoordinator
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import FAILED, RUNNING, STOPPED, ServiceStartError
from test_tts_service import FakeHost, FakeJob, RecordingLogger


class CoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.logger = RecordingLogger()
        self.host = FakeHost()
        # launch trees for both engines
        self.iro_dir = self.root / "Irodori-TTS"
        self.iro_server = self.iro_dir / "Irodori-TTS-Server"
        (self.iro_dir / ".venv" / "Scripts").mkdir(parents=True)
        (self.iro_dir / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        entry = self.iro_server / "src" / "irodori_openai_tts" / "__main__.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        self.gpt_dir = self.root / "GPT-SoVITS"
        (self.gpt_dir / "runtime").mkdir(parents=True)
        (self.gpt_dir / "runtime" / "python.exe").write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, engine="irodori"):
        settings = default_tts_settings()
        settings["engine"] = engine
        settings["irodori"].update(launch_dir=str(self.iro_dir), server_dir=str(self.iro_server))
        settings["gpt_sovits"].update(launch_dir=str(self.gpt_dir))
        return settings

    def make(self, engine="irodori", settings=None):
        return TtsCoordinator(
            settings or self.settings(engine),
            data_dir=self.data_dir,
            logger=self.logger,
            manager_kwargs=dict(
                launcher=self.host.launcher,
                probe=self.host.probe,
                killer=self.host.killer,
                job_factory=FakeJob,
                clock=self.host.clock,
                sleep=self.host.sleep,
            ),
        )


class ManagerConfigurationTests(CoordinatorCase):
    def test_irodori_manager_uses_the_venv_python_and_server_environment(self):
        tts = self.make("irodori")
        manager = tts.manager()
        self.assertEqual(manager.service_name, "Irodori")
        self.assertEqual(
            manager.command,
            [os.path.join(str(self.iro_dir), ".venv", "Scripts", "python.exe"),
             "-m", "irodori_openai_tts", "--host", "127.0.0.1", "--port", "8088"],
        )
        env = manager._extra_env
        self.assertEqual(env["PYTHONPATH"], os.path.join(str(self.iro_server), "src"))
        self.assertEqual(env["HF_HUB_CACHE"], os.path.join(str(self.iro_dir), "model_cache", "hub"))
        self.assertEqual(env["IRODORI_HF_CHECKPOINT"], "Aratako/Irodori-TTS-v4.1-Small")
        self.assertEqual(env["IRODORI_PRELOAD"], "true")
        self.assertEqual(env["IRODORI_COMPILE_MODEL"], "false")
        self.assertEqual(env["IRODORI_VOICES_DIR"], os.path.join(str(self.iro_server), "voices"))
        self.assertEqual(
            manager._required_files,
            [os.path.join(str(self.iro_server), "src", "irodori_openai_tts", "__main__.py")],
        )
        self.assertIsNone(manager._on_restart)
        self.assertEqual(manager.log_path, self.data_dir / "tts_service.log")

    def _cache_repo(self, repo, files, *, commit="abc123", ref=True):
        repo_dir = self.iro_dir / "model_cache" / "hub" / ("models--" + repo.replace("/", "--"))
        snapshot = repo_dir / "snapshots" / commit
        for name in files:
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        snapshot.mkdir(parents=True, exist_ok=True)
        if ref:
            (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
            (repo_dir / "refs" / "main").write_text(commit, encoding="utf-8")

    def _cache_everything(self, checkpoint="Aratako/Irodori-TTS-v4.1-Small"):
        self._cache_repo(checkpoint, ["model.safetensors", "tokenizer/tokenizer.json"])
        self._cache_repo("Aratako/Semantic-DACVAE-Japanese-32dim", ["weights.pth"])
        self._cache_repo("sony/silentcipher", ["44_1_khz/73999_iteration/hparams.yaml"])

    def test_irodori_starts_offline_when_every_model_is_cached(self):
        self._cache_everything()
        env = self.make("irodori").manager()._extra_env
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")

    def test_irodori_stays_online_when_anything_is_missing(self):
        self.assertNotIn("HF_HUB_OFFLINE", self.make("irodori").manager()._extra_env)  # empty cache
        for missing in ("Aratako/Irodori-TTS-v4.1-Small", "Aratako/Semantic-DACVAE-Japanese-32dim", "sony/silentcipher"):
            with self.subTest(missing=missing):
                import shutil
                shutil.rmtree(self.iro_dir / "model_cache", ignore_errors=True)
                self._cache_everything()
                shutil.rmtree(self.iro_dir / "model_cache" / "hub" / ("models--" + missing.replace("/", "--")))
                self.assertNotIn("HF_HUB_OFFLINE", self.make("irodori").manager()._extra_env)

    def test_irodori_stays_online_when_a_download_was_cut_short(self):
        self._cache_everything()
        snapshot = self.iro_dir / "model_cache" / "hub" / "models--Aratako--Irodori-TTS-v4.1-Small" / "snapshots" / "abc123"
        (snapshot / "model.safetensors").unlink()  # tokenizer arrived, the weights did not
        self.assertNotIn("HF_HUB_OFFLINE", self.make("irodori").manager()._extra_env)
        self._cache_everything()
        (self.iro_dir / "model_cache" / "hub" / "models--sony--silentcipher" / "refs" / "main").unlink()
        self.assertNotIn("HF_HUB_OFFLINE", self.make("irodori").manager()._extra_env)

    def test_irodori_with_a_local_checkpoint_path_is_never_forced_offline(self):
        self._cache_everything()
        local = self.root / "my-checkpoint"
        local.mkdir()
        for checkpoint in (str(local), "D:\\models\\kotone", "./local/ckpt", "just-a-name"):
            with self.subTest(checkpoint=checkpoint):
                settings = self.settings("irodori")
                settings["irodori"]["hf_checkpoint"] = checkpoint
                self.assertNotIn("HF_HUB_OFFLINE", self.make(settings=settings).manager()._extra_env)

    def test_irodori_checkpoint_other_than_the_default_needs_its_own_cache(self):
        self._cache_everything()
        settings = self.settings("irodori")
        settings["irodori"]["hf_checkpoint"] = "someone/other-model"
        self.assertNotIn("HF_HUB_OFFLINE", self.make(settings=settings).manager()._extra_env)
        self._cache_repo("someone/other-model", ["model.safetensors", "tokenizer/t.json"])
        self.assertEqual(self.make(settings=settings).manager()._extra_env["HF_HUB_OFFLINE"], "1")

    def test_gpt_sovits_manager_keeps_the_original_command_and_model_reset(self):
        tts = self.make("gpt_sovits")
        manager = tts.manager()
        self.assertEqual(manager.service_name, "GPT-SoVITS")
        self.assertEqual(manager.command[1:], ["api.py", "-a", "127.0.0.1", "-p", "9880"])
        self.assertEqual(manager._extra_env, {})
        tts._current_model = {"gpt_model_path": "a", "sovits_model_path": "b"}
        manager._on_restart()
        self.assertIsNone(tts._current_model)

    def test_managers_are_created_lazily_and_once_per_engine(self):
        tts = self.make("irodori")
        self.assertEqual(tts._managers, {})
        first = tts.manager()
        self.assertIs(tts.manager(), first)
        self.assertIs(tts.manager("irodori"), first)
        self.assertEqual(list(tts._managers), ["irodori"])
        self.assertIsNot(tts.manager("gpt_sovits"), first)
        with self.assertRaises(ValueError):
            tts.manager("nope")

    def test_common_settings_are_applied_to_the_manager(self):
        settings = self.settings()
        settings["common"].update(start_timeout=77, stop_on_exit=False)
        manager = self.make(settings=settings).manager()
        self.assertEqual((manager.start_timeout, manager.stop_on_exit), (77, False))

    def test_timeout_and_temp_dir_derive_from_settings(self):
        settings = self.settings()
        settings["common"]["tts_timeout_minutes"] = 7
        tts = self.make(settings=settings)
        self.assertEqual(tts.tts_timeout, 420)
        self.assertEqual(tts.temp_dir, self.data_dir / "temp_audio")


class ServiceControlTests(CoordinatorCase):
    def test_status_reports_engine_and_service_name(self):
        tts = self.make("irodori")
        status = asyncio.run(tts.status())
        self.assertEqual((status["engine"], status["service_name"], status["state"]), ("irodori", "Irodori", STOPPED))
        self.assertEqual(status["port"], 8088)

    def test_start_then_stop_the_current_engine_service(self):
        tts = self.make("irodori")

        async def scenario():
            await tts.start_service()
            running = await tts.status()
            stopped = await tts.stop_service()
            return running, stopped, await tts.status()

        running, stopped, after = asyncio.run(scenario())
        self.assertEqual((running["state"], running["pid"]), (RUNNING, 4321))
        self.assertTrue(stopped["ok"])
        self.assertEqual(after["state"], STOPPED)
        self.assertEqual(self.host.kills, [4321])

    def test_stop_refuses_an_external_service(self):
        self.host.port_open = True
        tts = self.make("irodori")
        result = asyncio.run(tts.stop_service())
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.kills, [])

    def test_start_failure_raises_service_start_error(self):
        self.host.exit_code = 2
        tts = self.make("irodori")

        async def scenario():
            with self.assertRaises(ServiceStartError):
                await tts.start_service()
            return await tts.status()

        status = asyncio.run(scenario())
        self.assertEqual(status["state"], FAILED)
        self.assertIn("退出码 2", status["message"])

    def test_terminate_stops_every_managed_service(self):
        tts = self.make("irodori")

        async def scenario():
            await tts.start_service()
            await tts.terminate()

        asyncio.run(scenario())
        self.assertEqual(self.host.kills, [4321])


class ApplySettingsTests(CoordinatorCase):
    def started(self, tts, engine=None):
        async def go():
            await tts.manager(engine).ensure_started()

        asyncio.run(go())

    def test_unchanged_settings_stop_nothing(self):
        tts = self.make("irodori")
        self.started(tts)
        outcome = asyncio.run(tts.apply_settings(copy.deepcopy(tts.settings)))
        self.assertEqual(outcome, {"stopped": []})
        self.assertEqual(self.host.kills, [])
        self.assertEqual(len(self.host.launches), 1)

    def test_switching_engine_stops_the_old_engines_own_service(self):
        tts = self.make("irodori")
        self.started(tts)
        new = copy.deepcopy(tts.settings)
        new["engine"] = "gpt_sovits"
        outcome = asyncio.run(tts.apply_settings(new))
        self.assertEqual(outcome, {"stopped": ["irodori"]})
        self.assertEqual(self.host.kills, [4321])
        self.assertEqual(tts.engine, "gpt_sovits")
        self.assertEqual(tts.manager().service_name, "GPT-SoVITS")
        # the stopped manager is kept (same parameters) and reports a stopped service
        self.assertEqual(asyncio.run(tts.manager("irodori").status())["state"], STOPPED)
        self.assertTrue(any("engine switched to GPT-SoVITS" in m for m in self.logger.messages("info")))

    def test_switching_engine_never_touches_an_external_service(self):
        self.host.port_open = True
        tts = self.make("irodori")
        asyncio.run(tts.manager().ensure_started())  # detects the external service
        new = copy.deepcopy(tts.settings)
        new["engine"] = "gpt_sovits"
        outcome = asyncio.run(tts.apply_settings(new))
        self.assertEqual(outcome, {"stopped": []})
        self.assertEqual(self.host.kills, [])
        self.assertEqual(self.host.launches, [])

    def test_switching_without_a_started_manager_is_a_plain_setting_change(self):
        tts = self.make("irodori")
        new = copy.deepcopy(tts.settings)
        new["engine"] = "gpt_sovits"
        self.assertEqual(asyncio.run(tts.apply_settings(new)), {"stopped": []})
        self.assertEqual(tts.engine, "gpt_sovits")

    def test_changed_launch_parameters_stop_and_rebuild_that_engine(self):
        tts = self.make("irodori")
        self.started(tts)
        old_manager = tts.manager()
        new = copy.deepcopy(tts.settings)
        new["irodori"]["port"] = 9001
        outcome = asyncio.run(tts.apply_settings(new))
        self.assertEqual(outcome, {"stopped": ["irodori"]})
        self.assertEqual(self.host.kills, [4321])
        rebuilt = tts.manager()
        self.assertIsNot(rebuilt, old_manager)
        self.assertEqual(rebuilt.port, 9001)
        self.assertEqual(rebuilt.command[-1], "9001")
        self.assertTrue(any("launch settings changed" in m for m in self.logger.messages("info")))

    def test_each_launch_parameter_counts_as_a_launch_change(self):
        changes = (
            ("irodori", "host", "127.0.0.2"), ("irodori", "launch_dir", str(self.root)),
            ("irodori", "server_dir", str(self.root)), ("irodori", "hf_checkpoint", "other/model"),
        )
        for section, key, value in changes:
            with self.subTest(key=key):
                self.host = FakeHost()
                tts = self.make("irodori")
                self.started(tts)
                new = copy.deepcopy(tts.settings)
                new[section][key] = value
                self.assertEqual(asyncio.run(tts.apply_settings(new)), {"stopped": ["irodori"]})

    def test_non_launch_parameters_keep_the_running_service(self):
        tts = self.make("irodori")
        self.started(tts)
        manager = tts.manager()
        new = copy.deepcopy(tts.settings)
        new["irodori"].update(num_steps=20, default_caption="x", lora_adapter="", cfg_scale_caption=5.0)
        new["common"].update(start_timeout=99, stop_on_exit=False, prob=10.0, send_text_with_audio=True)
        outcome = asyncio.run(tts.apply_settings(new))
        self.assertEqual(outcome, {"stopped": []})
        self.assertEqual(self.host.kills, [])
        self.assertIs(tts.manager(), manager)
        self.assertEqual((manager.start_timeout, manager.stop_on_exit), (99, False))
        self.assertEqual(tts.settings["irodori"]["num_steps"], 20)
        self.assertEqual(tts.common["prob"], 10.0)

    def test_gpt_sovits_launch_change_resets_the_model_cache(self):
        tts = self.make("gpt_sovits")
        self.started(tts)
        tts._current_model = {"gpt_model_path": "a", "sovits_model_path": "b"}
        new = copy.deepcopy(tts.settings)
        new["gpt_sovits"]["port"] = 9990
        self.assertEqual(asyncio.run(tts.apply_settings(new)), {"stopped": ["gpt_sovits"]})
        self.assertIsNone(tts._current_model)

    def test_gpt_sovits_model_path_change_does_not_restart_but_reselects(self):
        tts = self.make("gpt_sovits")
        self.started(tts)
        new = copy.deepcopy(tts.settings)
        new["gpt_sovits"].update(gpt_model_path=r"D:\a.ckpt", sovits_model_path=r"D:\a.pth")
        self.assertEqual(asyncio.run(tts.apply_settings(new)), {"stopped": []})
        self.assertEqual(self.host.kills, [])
        self.assertEqual(tts._target_model(None), {"gpt_model_path": r"D:\a.ckpt", "sovits_model_path": r"D:\a.pth"})

    def test_switch_back_starts_the_engine_again_with_current_parameters(self):
        tts = self.make("irodori")
        self.started(tts)
        to_gpt = copy.deepcopy(tts.settings)
        to_gpt["engine"] = "gpt_sovits"
        asyncio.run(tts.apply_settings(to_gpt))
        back = copy.deepcopy(tts.settings)
        back["engine"] = "irodori"
        asyncio.run(tts.apply_settings(back))
        self.started(tts)
        self.assertEqual(len(self.host.launches), 2)
        self.assertEqual(tts.engine, "irodori")

    def test_invalid_settings_are_rejected_and_nothing_changes(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import EmotionConfigError

        tts = self.make("irodori")
        self.started(tts)
        before = copy.deepcopy(tts.settings)
        bad = copy.deepcopy(before)
        bad["engine"] = "nope"
        with self.assertRaises(EmotionConfigError):
            asyncio.run(tts.apply_settings(bad))
        self.assertEqual(tts.settings, before)
        self.assertEqual(self.host.kills, [])

    def test_public_settings_is_a_copy(self):
        tts = self.make("irodori")
        copy_ = tts.public_settings()
        copy_["engine"] = "gpt_sovits"
        self.assertEqual(tts.engine, "irodori")


if __name__ == "__main__":
    unittest.main()

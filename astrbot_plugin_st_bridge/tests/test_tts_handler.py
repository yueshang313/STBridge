"""The voice step of the bridge core (ported from the gpt_sovits plugin tests).

The core synthesizes the voice before it hands the reply batch to the adapter; these tests drive that step
(`_voice_reply`) and turn its segments into the AstrBot chain the same way the adapter does."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import default_tts_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_clients import IrodoriRequestError, ModelSelectionError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_coordinator import TtsCoordinator
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import ServiceStartError
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import _load_plugin_module
from test_tts_service import FakeHost, FakeJob

MODEL_A = {"gpt_model_path": r"D:\GPT\a.ckpt", "sovits_model_path": r"D:\SoVITS\a.pth"}
MODEL_B = {"gpt_model_path": r"D:\GPT\b.ckpt", "sovits_model_path": r"D:\SoVITS\b.pth"}
CONFIG_MODEL = {"gpt_model_path": r"D:\GPT\cfg.ckpt", "sovits_model_path": r"D:\SoVITS\cfg.pth"}


def _profile(**overrides):
    profile = {
        "ref_audio_path": r"D:\GPT-SoVITS\ckyp\ktn_轻快.wav",
        "prompt_text": "やったね",
        "prompt_text_lang": "日文",
        "text_lang": "日文",
        "top_k": 15,
        "top_p": 0.9,
        "temperature": 0.8,
        "speed_factor": 1.25,
        "split_sentence": False,
        "emotion": "轻快",
        "character": "琴音",
    }
    profile.update(overrides)
    return profile


def _style_profile(**overrides):
    profile = {
        "ref_audio_path": "",
        "caption": "嬉しそうに話している。",
        "emoji": "😊",
        "emotion": "开心",
        "character": "琴音",
        "source": "style",
    }
    profile.update(overrides)
    return profile


class FakeResult:
    def __init__(self, chain):
        self.chain = chain


class FakeEvent:
    """The reply as the adapter would send it: ``result`` starts as the plain text and is replaced by the
    voiced chain when the core produced one. ``tts_text`` is the Japanese text (None: nothing to voice)."""

    def __init__(self, tts_text, profile, result, session_id="test-session"):
        self.tts_text = tts_text
        self.profile = profile
        self.result = result
        self.session_id = session_id

    def chain_result(self, chain):
        return FakeResult(list(chain))


async def _async_bytes(value):
    return value


class HandlerCase(unittest.TestCase):
    engine = "gpt_sovits"

    def setUp(self):
        self.module = _load_plugin_module()
        self.logger = self.module.test_logger
        self.Plain = self.module.TestPlain
        self.Record = self.module.TestRecord
        self.tmp = tempfile.TemporaryDirectory()
        self.host = FakeHost()
        self.host.port_open = True
        settings = default_tts_settings()
        settings["engine"] = self.engine
        settings["common"].update(send_text_with_audio=False, split_messages=False)  # the behaviour these cases were written for
        self.tts = TtsCoordinator(
            settings,
            data_dir=Path(self.tmp.name),
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
        self.plugin = self.module.STBridgePlugin(context=None, config={})
        self.ReplyBatch = sys.modules[f"{self.module.__package__}.st_bridge_core.platform_api"].ReplyBatch
        self.plugin._core._tts = self.tts
        self.calls = []
        self.install_fakes()

    def tearDown(self):
        self.tmp.cleanup()

    def install_fakes(self):
        async def set_model(payload):
            self.calls.append(("set_model", dict(payload)))

        async def post(payload):
            self.calls.append(("infer", payload))
            return b"wav-bytes"

        async def post_irodori(payload):
            self.calls.append(("irodori", payload))
            return b"wav-bytes"

        self.tts._set_model = set_model
        self.tts._post = post
        self.tts._post_irodori = post_irodori

    # ---- helpers ----
    def set_common(self, **values):
        self.tts.settings["common"].update(values)

    def set_gpt(self, **values):
        self.tts.settings["gpt_sovits"].update(values)

    def set_irodori(self, **values):
        self.tts.settings["irodori"].update(values)

    def event(self, profile=None, text="中文回复", tts_text="「日本語」", marked=True):
        return FakeEvent(tts_text if marked else None, profile if marked else None, FakeResult([self.Plain(text)]))

    async def voice_event(self, event):
        plain = "".join(item.text for item in event.result.chain)
        segments = await self.plugin._core._voice_reply(plain, event.tts_text, event.profile, event.session_id)
        if segments:
            event.result = self.plugin._platform.result(event, self.ReplyBatch(tuple(segments)))
        return event

    def run_event(self, event):
        return asyncio.run(self.voice_event(event))

    def calls_of(self, kind):
        return [payload for name, payload in self.calls if name == kind]

    def errors(self):
        return list(self.logger.errors)

    def assert_record_only(self, event):
        self.assertEqual(len(event.result.chain), 1)
        self.assertIsInstance(event.result.chain[0], self.Record)

    def assert_text_kept(self, event, original):
        self.assertEqual(event.result.chain, [original])


class DecorationTests(HandlerCase):
    def test_there_is_no_decorating_stage_handler_any_more(self):
        self.assertFalse(hasattr(self.module.STBridgePlugin, "stbridge_decorate_tts"))

    def test_unmarked_event_does_not_call_tts_or_change_result(self):
        event = self.event(marked=False)
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls, [])

    def test_missing_coordinator_leaves_the_reply_alone(self):
        self.plugin._core._tts = None
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)

    def test_marked_bridge_reply_replaces_plain_with_record(self):
        event = self.run_event(self.event(text="ST reply"))
        self.assert_record_only(event)
        record = event.result.chain[0]
        self.assertEqual(Path(record.file).read_bytes(), b"wav-bytes")
        self.assertEqual(record.text, "")
        self.assertEqual(Path(record.file).parent, Path(self.tmp.name) / "temp_audio")

    def test_record_file_is_24k_mono_16bit_and_garbage_audio_is_sent_as_is(self):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes((1000).to_bytes(2, "little") * 9600)

        async def post(payload):
            return buffer.getvalue()

        self.tts._post = post
        self.tts._post_irodori = post
        event = self.run_event(self.event(text="ST reply"))
        with wave.open(event.result.chain[0].file, "rb") as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (24000, 1, 2))
            self.assertEqual(reader.getnframes(), 2400)

    def test_empty_model_paths_reuse_api_default_without_set_model(self):
        event = self.run_event(self.event(text="中文回复", tts_text="「日本語の返事」"))
        self.assertEqual(self.calls_of("set_model"), [])
        payload, = self.calls_of("infer")
        self.assertEqual(payload["text"], "「日本語の返事」")
        self.assertIsNone(self.tts._current_model)
        self.assert_record_only(event)

    def test_partial_model_path_configuration_fails_before_selection_or_inference(self):
        # Validation rejects this in the console, but a hand-edited state must still fail closed.
        self.set_gpt(gpt_model_path="only-gpt.ckpt", sovits_model_path="")
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.errors(), ["[STBridge] Model selection failed (ModelSelectionError)"])

    def test_configured_model_is_selected_once_before_inference(self):
        self.set_gpt(**CONFIG_MODEL)
        self.run_event(self.event())
        self.run_event(self.event())
        self.assertEqual([name for name, _ in self.calls], ["set_model", "infer", "infer"])
        self.assertEqual(self.tts._current_model, CONFIG_MODEL)

    def test_concurrent_first_replies_select_configured_model_once(self):
        self.set_gpt(**CONFIG_MODEL)
        selected = []

        async def select_model(payload):
            selected.append(payload)
            await asyncio.sleep(0)

        self.tts._set_model = select_model

        async def run_both():
            await asyncio.gather(self.tts._ensure_model_selected(), self.tts._ensure_model_selected())

        asyncio.run(run_both())
        self.assertEqual(selected, [CONFIG_MODEL])
        self.assertEqual(self.tts._current_model, CONFIG_MODEL)

    def test_model_selection_failure_keeps_text_and_skips_inference(self):
        self.set_gpt(**CONFIG_MODEL)

        async def failing(_payload):
            raise RuntimeError("private model path")

        self.tts._set_model = failing
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls_of("infer"), [])
        self.assertEqual(self.errors(), ["[STBridge] Model selection failed (ModelSelectionError)"])
        self.assertNotIn("private model path", self.errors()[0])
        self.assertIsNone(self.tts._current_model)

    def test_unknown_language_keeps_text_and_skips_model_selection_and_inference(self):
        self.set_gpt(prompt_text_lang="unknown-private-label", **CONFIG_MODEL)
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("UnsupportedPromptLanguageError", self.errors()[0])
        self.assertNotIn("unknown-private-label", self.errors()[0])

    def test_send_text_with_audio_retains_a_text_fallback(self):
        self.set_common(send_text_with_audio=True)
        event = self.run_event(self.event(text="reply to retain"))
        self.assertEqual(len(event.result.chain), 2)
        self.assertIsInstance(event.result.chain[0], self.Plain)
        self.assertEqual(event.result.chain[0].text, "reply to retain")
        self.assertIsInstance(event.result.chain[1], self.Record)

    def test_send_text_with_audio_false_sends_only_the_record(self):
        self.set_common(send_text_with_audio=False)
        self.assert_record_only(self.run_event(self.event(text="reply")))

    def test_bilingual_bridge_sends_chinese_plain_and_synthesizes_japanese_only(self):
        chinese_text = "……那个啊，真的！😊"
        japanese_text = "「……あのさ」『本当に？』♪"
        self.set_common(send_text_with_audio=True)
        event = self.run_event(self.event(text=chinese_text, tts_text=japanese_text))
        payload, = self.calls_of("infer")
        self.assertEqual(payload["text"], japanese_text)
        self.assertNotIn(chinese_text, payload["text"])
        plains = [c for c in event.result.chain if isinstance(c, self.Plain)]
        records = [c for c in event.result.chain if isinstance(c, self.Record)]
        self.assertEqual([c.text for c in plains], [chinese_text])
        self.assertEqual(len(records), 1)

    def test_bridge_marker_without_japanese_text_keeps_chinese_and_skips_tts(self):
        event = FakeEvent(None, None, FakeResult([self.Plain("中文回复")]))
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls, [])

    def test_tts_failure_keeps_original_plain_reply_and_logs_only_error_type(self):
        secret = "test-only-sensitive-error-detail"

        async def failed_post(_):
            raise RuntimeError(secret)

        self.tts._post = failed_post
        self.set_common(send_text_with_audio=True)
        event = self.event(text="中文回复", tts_text="「日本語」")
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("OtherError", self.errors()[0])
        self.assertNotIn(secret, self.errors()[0])

    def test_connect_failure_keeps_original_plain_reply(self):
        class ConnectError(Exception):
            pass

        async def failed_post(_):
            raise ConnectError("private endpoint and token detail")

        self.tts._post = failed_post
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertIn("ConnectError", self.errors()[0])
        self.assertNotIn("private endpoint", self.errors()[0])

    def test_record_preparation_failure_keeps_text_and_removes_partial_audio(self):
        event = self.event()
        original = event.result.chain[0]
        with patch.object(submodule(self.module, "mixin_tts").Segment, "voice", side_effect=RuntimeError("sensitive preparation detail")):
            self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(list((Path(self.tmp.name) / "temp_audio").iterdir()), [])
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("OtherError", self.errors()[0])
        self.assertNotIn("sensitive preparation detail", self.errors()[0])

    def test_probability_zero_skips_tts(self):
        self.set_common(prob=0.0)
        with patch.object(self.module.random, "random", return_value=0.5):
            event = self.run_event(self.event())
        self.assertEqual(self.calls, [])
        self.assertIsInstance(event.result.chain[0], self.Plain)

    def test_cooldown_blocks_a_second_reply_in_the_same_session(self):
        self.set_common(cooldown=60)
        first = self.run_event(self.event())
        second = self.run_event(self.event())
        self.assert_record_only(first)
        self.assertIsInstance(second.result.chain[0], self.Plain)
        self.assertEqual(len(self.calls_of("infer")), 1)

    def test_text_limit_truncates_the_synthesized_text(self):
        self.set_common(text_limit=3)
        self.run_event(self.event(tts_text="「日本語の返事」"))
        payload, = self.calls_of("infer")
        self.assertEqual(payload["text"], "「日本")
        self.assertIn("[STBridge] TTS text exceeded the configured limit", self.logger.warnings)

    def test_temp_audio_is_removed_after_the_delay(self):
        event = self.run_event(self.event())
        path = Path(event.result.chain[0].file)
        self.assertTrue(path.exists())
        asyncio.run(type(self.plugin._core)._cleanup_tts_file(path, delay=0))
        self.assertFalse(path.exists())


class GptSovitsProfileTests(HandlerCase):
    def setUp(self):
        super().setUp()
        self.set_gpt(
            ref_audio_path=r"D:\config\reference.wav",
            prompt_text="config prompt",
            prompt_text_lang="日语",
            text_lang="日语",
        )

    def test_profile_parameters_drive_inference(self):
        event = self.run_event(self.event(_profile()))
        payload, = self.calls_of("infer")
        self.assertEqual(payload["refer_wav_path"], r"D:\GPT-SoVITS\ckyp\ktn_轻快.wav")
        self.assertEqual(payload["prompt_text"], "やったね")
        self.assertEqual(payload["speed"], 1.25)
        self.assertEqual(payload["text"], "「日本語」")
        self.assert_record_only(event)
        self.assertEqual(self.logger.infos, ["[STBridge] TTS settings source=profile model_switched=False"])

    def test_without_profile_configuration_is_used(self):
        event = self.run_event(self.event())
        payload, = self.calls_of("infer")
        self.assertEqual(payload["refer_wav_path"], r"D:\config\reference.wav")
        self.assertEqual(payload["prompt_text"], "config prompt")
        self.assertEqual(payload["prompt_language"], "日文")
        self.assertEqual(payload["speed"], 1.0)
        self.assertEqual(self.calls_of("set_model"), [])
        self.assert_record_only(event)
        self.assertEqual(self.logger.infos, ["[STBridge] TTS settings source=config model_switched=False"])

    def test_invalid_profile_falls_back_to_configuration(self):
        self.run_event(self.event(_profile(text_lang="klingon", **MODEL_A)))
        payload, = self.calls_of("infer")
        self.assertEqual(payload["refer_wav_path"], r"D:\config\reference.wav")
        self.assertEqual(self.calls_of("set_model"), [])

    def test_irodori_path_is_never_used_for_the_gpt_sovits_engine(self):
        self.run_event(self.event(_style_profile()))
        self.run_event(self.event(_profile()))
        self.assertEqual(self.calls_of("irodori"), [])
        self.assertEqual(len(self.calls_of("infer")), 2)

    def test_style_only_profile_falls_back_to_configuration(self):
        self.run_event(self.event(_style_profile()))
        payload, = self.calls_of("infer")
        self.assertEqual(payload["refer_wav_path"], r"D:\config\reference.wav")

    def test_profile_models_switch_only_when_the_pair_changes(self):
        for model in (MODEL_A, MODEL_A, MODEL_B, MODEL_B, MODEL_A):
            self.run_event(self.event(_profile(**model)))
        self.assertEqual(self.calls_of("set_model"), [MODEL_A, MODEL_B, MODEL_A])
        self.assertEqual(len(self.calls_of("infer")), 5)
        self.assertEqual(self.tts._current_model, MODEL_A)
        switched = [message.endswith("model_switched=True") for message in self.logger.infos]
        self.assertEqual(switched, [True, False, True, False, True])

    def test_model_is_selected_before_inference(self):
        self.run_event(self.event(_profile(**MODEL_A)))
        self.assertEqual([name for name, _ in self.calls], ["set_model", "infer"])

    def test_first_synthesis_after_start_always_selects_configured_model(self):
        self.set_gpt(**CONFIG_MODEL)
        self.run_event(self.event())
        self.run_event(self.event(_profile()))
        self.assertEqual(self.calls_of("set_model"), [CONFIG_MODEL])

    def test_profile_without_models_uses_configured_models(self):
        self.set_gpt(**CONFIG_MODEL)
        self.run_event(self.event(_profile(**MODEL_A)))
        self.run_event(self.event(_profile()))
        self.run_event(self.event())
        self.assertEqual(self.calls_of("set_model"), [MODEL_A, CONFIG_MODEL])

    def test_no_models_anywhere_never_calls_set_model(self):
        self.run_event(self.event(_profile()))
        self.run_event(self.event())
        self.assertEqual(self.calls_of("set_model"), [])
        self.assertIsNone(self.tts._current_model)

    def test_profile_models_bypass_incomplete_configuration(self):
        self.set_gpt(gpt_model_path="only-gpt.ckpt", sovits_model_path="")
        event = self.run_event(self.event(_profile(**MODEL_A)))
        self.assertEqual(self.calls_of("set_model"), [MODEL_A])
        self.assert_record_only(event)
        self.assertEqual(self.errors(), [])

        event = self.run_event(self.event(_profile()))
        self.assertEqual(len(self.calls_of("infer")), 1)
        self.assertIsInstance(event.result.chain[0], self.Plain)
        self.assertEqual(self.errors(), ["[STBridge] Model selection failed (ModelSelectionError)"])

    def test_switch_failure_keeps_text_and_retries_next_time(self):
        self.tts._current_model = dict(MODEL_A)
        attempts = []

        async def failing_set_model(payload):
            attempts.append(dict(payload))
            raise RuntimeError(r"secret D:\SoVITS\b.pth")

        self.tts._set_model = failing_set_model
        event = self.event(_profile(**MODEL_B), text="保留的中文")
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls_of("infer"), [])
        self.assertEqual(self.tts._current_model, MODEL_A)
        self.assertEqual(self.errors(), ["[STBridge] Model selection failed (ModelSelectionError)"])

        async def working_set_model(payload):
            attempts.append(dict(payload))

        self.tts._set_model = working_set_model
        self.run_event(self.event(_profile(**MODEL_B)))
        self.assertEqual(attempts, [MODEL_B, MODEL_B])
        self.assertEqual(self.tts._current_model, MODEL_B)

    def test_api_rejection_is_a_model_selection_failure(self):
        async def rejected(_payload):
            raise ModelSelectionError("GPT-SoVITS API rejected model selection")

        self.tts._set_model = rejected
        event = self.event(_profile(**MODEL_A))
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertIsNone(self.tts._current_model)

    def test_concurrent_requests_for_the_same_pair_switch_once(self):
        selected = []

        async def slow_set_model(payload):
            selected.append(dict(payload))
            await asyncio.sleep(0)

        self.tts._set_model = slow_set_model

        async def run_both():
            return await asyncio.gather(
                self.tts._ensure_model_selected(_profile(**MODEL_A)),
                self.tts._ensure_model_selected(_profile(**MODEL_A)),
            )

        self.assertEqual(sorted(asyncio.run(run_both())), [False, True])
        self.assertEqual(selected, [MODEL_A])

    def test_send_text_with_audio_true_and_false_with_profile(self):
        for keep_text in (False, True):
            with self.subTest(send_text_with_audio=keep_text):
                self.set_common(send_text_with_audio=keep_text)
                event = self.run_event(self.event(_profile(), text="中文回复"))
                if keep_text:
                    self.assertEqual(len(event.result.chain), 2)
                    self.assertIsInstance(event.result.chain[0], self.Plain)
                    self.assertEqual(event.result.chain[0].text, "中文回复")
                    self.assertIsInstance(event.result.chain[1], self.Record)
                else:
                    self.assert_record_only(event)

    def test_logs_never_contain_paths_or_text(self):
        self.set_gpt(**CONFIG_MODEL)
        self.run_event(self.event(_profile(**MODEL_A), text="中文秘密", tts_text="「日本語の秘密」"))
        self.run_event(self.event(text="中文秘密", tts_text="「日本語の秘密」"))
        logs = " ".join(self.logger.infos + self.logger.warnings + self.logger.errors)
        for secret in ("D:\\", "ktn_", "秘密", "やったね", "config prompt", "reference.wav"):
            self.assertNotIn(secret, logs)


class OnDemandServiceTests(HandlerCase):
    def setUp(self):
        super().setUp()
        self.host.port_open = False
        launch_dir = Path(self.tmp.name) / "GPT-SoVITS"
        (launch_dir / "runtime").mkdir(parents=True)
        (launch_dir / "runtime" / "python.exe").write_bytes(b"")
        self.set_gpt(launch_dir=str(launch_dir), **MODEL_A)

    def test_two_tts_start_the_service_once_and_wait_for_it(self):
        self.tts._current_model = dict(MODEL_A)  # cached before the API was down
        events = [self.event(), self.event()]

        async def scenario():
            await asyncio.gather(*(self.voice_event(e) for e in events))

        asyncio.run(scenario())
        self.assertEqual(len(self.host.launches), 1)
        self.assertTrue(all(isinstance(e.result.chain[0], self.Record) for e in events))
        self.assertEqual(len(self.calls_of("infer")), 2)
        # the fresh API had no model selected, so /set_model ran again (once)
        self.assertEqual(self.calls_of("set_model"), [MODEL_A])
        self.assertEqual(self.tts._current_model, MODEL_A)

    def test_port_down_resets_cached_model_even_without_auto_start(self):
        self.set_common(auto_start=False)
        self.tts._current_model = dict(MODEL_A)
        event = self.run_event(self.event())
        self.assertEqual(self.host.launches, [])
        self.assertEqual(self.calls_of("set_model"), [MODEL_A])
        self.assert_record_only(event)

    def test_auto_start_off_keeps_the_existing_failure_path(self):
        self.set_common(auto_start=False)

        async def refused(_):
            raise ConnectionError("refused")

        self.tts._post = refused
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assertEqual(self.host.launches, [])
        self.assert_text_kept(event, original)
        self.assertTrue(any("conversion failed" in m for m in self.errors()))

    def test_start_failure_sends_text_and_logs_one_error(self):
        self.host.exit_code = 1
        event = self.event()
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls_of("infer"), [])
        self.assertTrue(any("ServiceStartError" in m for m in self.errors()))

    def test_running_service_is_not_restarted(self):
        self.host.port_open = True
        event = self.run_event(self.event())
        self.assertEqual(self.host.launches, [])
        self.assert_record_only(event)

    def test_plugin_terminate_stops_only_a_managed_service(self):
        async def scenario():
            await self.tts.start_service()
            await self.plugin.terminate()

        asyncio.run(scenario())
        self.assertEqual(self.host.kills, [4321])
        self.assertIsNone(self.plugin._core._tts)


def _wav_48k_stereo():
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes((1000).to_bytes(2, "little") * 9600)
    return buffer.getvalue()


def _record_format(path):
    import wave

    with wave.open(path, "rb") as reader:
        return reader.getframerate(), reader.getnchannels(), reader.getsampwidth()


class IrodoriOutputModeTests(HandlerCase):
    engine = "irodori"

    def synth_48k(self):
        async def post(payload):
            return _wav_48k_stereo()

        self.tts._post = post
        self.tts._post_irodori = post

    def test_native_mode_sends_the_engine_audio_untouched(self):
        self.synth_48k()
        self.tts.settings["irodori"]["output_mode"] = "native"
        event = self.run_event(self.event(_style_profile()))
        self.assertEqual(Path(event.result.chain[0].file).read_bytes(), _wav_48k_stereo())

    def test_qq_24k_mode_and_the_default_convert(self):
        self.synth_48k()
        self.assertEqual(self.tts.settings["irodori"]["output_mode"], "qq_24k")
        event = self.run_event(self.event(_style_profile()))
        self.assertEqual(_record_format(event.result.chain[0].file), (24000, 1, 2))


class GptSovitsAlwaysConvertsTests(HandlerCase):
    engine = "gpt_sovits"

    def test_the_irodori_field_does_not_apply_to_gpt_sovits(self):
        async def post(payload):
            return _wav_48k_stereo()

        self.tts._post = post
        self.tts.settings["irodori"]["output_mode"] = "native"
        event = self.run_event(self.event(text="ST reply"))
        self.assertEqual(_record_format(event.result.chain[0].file), (24000, 1, 2))


class IrodoriHandlerTests(HandlerCase):
    engine = "irodori"

    def test_style_profile_produces_one_request_and_a_record(self):
        event = self.run_event(self.event(_style_profile()))
        self.assert_record_only(event)
        payload, = self.calls_of("irodori")
        self.assertEqual(payload["input"], "😊「日本語」")
        self.assertEqual(payload["irodori"]["caption"], "嬉しそうに話している。")
        self.assertNotIn("lora_adapter", payload["irodori"])  # none is configured by default
        self.assertEqual(Path(event.result.chain[0].file).read_bytes(), b"wav-bytes")
        self.assertEqual(self.calls_of("infer"), [])
        self.assertEqual(self.calls_of("set_model"), [])
        self.assertIn("[STBridge] TTS settings engine=irodori source=style emoji=True caption=True", self.logger.infos)
        logs = " ".join(self.logger.infos + self.logger.warnings + self.logger.errors)
        self.assertNotIn("嬉しそう", logs)
        self.assertNotIn("😊", logs)

    def test_event_without_profile_uses_the_default_caption(self):
        self.set_irodori(default_caption="既定のキャプション")
        event = self.run_event(self.event())
        self.assert_record_only(event)
        payload, = self.calls_of("irodori")
        self.assertEqual(payload["input"], "「日本語」")
        self.assertEqual(payload["irodori"]["caption"], "既定のキャプション")

    def test_directory_profile_with_audio_sends_no_reference(self):
        self.run_event(self.event(_style_profile(ref_audio_path=r"D:\ref\a.wav", source="directory")))
        payload, = self.calls_of("irodori")
        self.assertNotIn("ref_wav", str(payload))
        self.assertIs(payload["irodori"]["no_ref"], True)

    def test_error_response_keeps_the_text_and_logs_status_and_type_only(self):
        async def failing(payload):
            raise IrodoriRequestError(500, "server_error")

        self.tts._post_irodori = failing
        event = self.event(_style_profile())
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.errors(), ["[STBridge] Irodori synthesis failed (status=500 type=server_error)"])
        self.assertFalse((Path(self.tmp.name) / "temp_audio").exists() and list((Path(self.tmp.name) / "temp_audio").glob("*.wav")))

    def test_service_start_failure_keeps_the_text(self):
        self.host.port_open = False
        self.host.exit_code = 1
        event = self.event(_style_profile())
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertEqual(self.calls_of("irodori"), [])
        self.assertTrue(any("ServiceStartError" in m for m in self.errors()))
        self.assertTrue(isinstance(ServiceStartError("x"), Exception))


class PluginWiringTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_plugin_module()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_builds_the_coordinator_from_the_console_config(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore

        plugin = self.module.STBridgePlugin(context=None, config={})
        self.assertIsNone(plugin._core._tts)
        plugin._core._emotion_store = ConfigStore(Path(self.tmp.name))
        plugin._core._emotion_store.config["tts"]["engine"] = "gpt_sovits"
        plugin._core._setup_tts()
        self.assertEqual(plugin._core._tts.engine, "gpt_sovits")
        self.assertEqual(plugin._core._tts.temp_dir, Path(self.tmp.name).resolve() / "temp_audio")
        first = plugin._core._tts
        plugin._core._setup_tts()
        self.assertIs(plugin._core._tts, first)

    def test_setup_without_a_store_leaves_tts_disabled(self):
        plugin = self.module.STBridgePlugin(context=None, config={})
        plugin._core._setup_tts()
        self.assertIsNone(plugin._core._tts)

    def test_setup_failure_only_logs(self):
        plugin = self.module.STBridgePlugin(context=None, config={})
        plugin._core._emotion_store = type("Broken", (), {"config": {"tts": {}}, "data_dir": None})()
        with patch.object(submodule(self.module, "mixin_lifecycle"), "TtsCoordinator", side_effect=RuntimeError("boom")):
            plugin._core._setup_tts()
        self.assertIsNone(plugin._core._tts)
        self.assertEqual(self.module.test_logger.errors, ["[STBridge] TTS unavailable (TtsSetupFailed: RuntimeError)"])

    def test_terminate_closes_the_coordinator_even_when_it_fails(self):
        plugin = self.module.STBridgePlugin(context=None, config={})

        class Broken:
            async def terminate(self):
                raise OSError("busy")

        plugin._core._tts = Broken()
        asyncio.run(plugin.terminate())
        self.assertIsNone(plugin._core._tts)
        self.assertEqual(self.module.test_logger.errors, ["[STBridge] TTS service stop failed (TtsStopFailed: OSError)"])


if __name__ == "__main__":
    unittest.main()

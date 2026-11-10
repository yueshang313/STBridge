import asyncio
import unittest

from aiohttp import web

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_clients import (
    IrodoriRequestError,
    ModelSelectionError,
    build_api_base_url,
    build_api_py_model_payload,
    build_api_py_payload,
    build_irodori_payload,
    build_profile_tts_settings,
    extract_wav_response,
    map_api_py_language,
    post_api_py_audio,
    post_api_py_model,
    post_irodori_audio,
    profile_model_payload,
    safe_error_type,
    usable_gpt_profile,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import TTS_LANGUAGES


class FakeResponse:
    def __init__(
        self,
        content=b"wav-data",
        content_type="audio/wav",
        error=None,
        json_body=None,
    ):
        self.content = content
        self.headers = {"content-type": content_type}
        self.error = error
        self.json_body = json_body
        self.raise_calls = 0

    def raise_for_status(self):
        self.raise_calls += 1
        if self.error:
            raise self.error

    def json(self):
        return self.json_body


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, *, json):
        self.calls.append((url, json))
        return self.response


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



SETTINGS = {
    "lora_adapter": r"D:\Irodori-TTS\lora\kotone_lora\small",
    "num_steps": 0,
    "cfg_scale_text": 3.0,
    "cfg_scale_caption": 4.0,
    "default_caption": "",
}


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


class GptSovitsClientTests(unittest.TestCase):
    def test_api_address_is_normalized_and_validated(self):
        self.assertEqual(
            build_api_base_url(" 127.0.0.1 ", "9880"),
            "http://127.0.0.1:9880",
        )
        self.assertEqual(
            build_api_base_url("::1", 9880),
            "http://[::1]:9880",
        )
        with self.assertRaises(ValueError):
            build_api_base_url("", 9880)
        with self.assertRaises(ValueError):
            build_api_base_url("127.0.0.1/path", 9880)
        with self.assertRaises(ValueError):
            build_api_base_url("127.0.0.1", 70000)

    def test_model_selection_requires_a_complete_pair_and_supports_reuse(self):
        self.assertIsNone(build_api_py_model_payload("", ""))
        gpt_path = r"C:\GPT-SoVITS-v2pro\GPT_weights\voice.ckpt"
        sovits_path = r"C:\GPT-SoVITS-v2pro\SoVITS_weights\voice.pth"
        self.assertEqual(
            build_api_py_model_payload(f" {gpt_path} ", f" {sovits_path} "),
            {
                "gpt_model_path": gpt_path,
                "sovits_model_path": sovits_path,
            },
        )
        for gpt_value, sovits_value in (("gpt.ckpt", ""), ("", "sovits.pth")):
            with self.subTest(gpt_value=bool(gpt_value), sovits_value=bool(sovits_value)):
                with self.assertRaises(ModelSelectionError):
                    build_api_py_model_payload(gpt_value, sovits_value)


    def test_payload_maps_plugin_settings_to_api_py_contract(self):
        payload = build_api_py_payload(
            "reply text",
            {
                "ref_audio_path": "reference.wav",
                "prompt_text": "reference transcript",
                "prompt_text_lang": "英语",
                "text_lang": "日语",
                "top_k": 12,
                "top_p": 0.8,
                "temperature": 0.9,
                "speed_facter": 1.1,
                "split_sentence": True,
            },
        )

        self.assertEqual(payload["refer_wav_path"], "reference.wav")
        self.assertEqual(payload["prompt_language"], "英文")
        self.assertEqual(payload["text_language"], "日文")
        self.assertEqual(payload["speed"], 1.1)
        self.assertEqual(payload["text"], "reply text")
        self.assertIn("cut_punc", payload)
        self.assertNotIn("prompt_text_lang", payload)
        self.assertNotIn("text_lang", payload)
        self.assertNotIn("speed_facter", payload)
        self.assertNotIn("audio_url", payload)


    def test_plugin_language_aliases_match_installed_api_labels(self):
        self.assertEqual(
            map_api_py_language("英语", "prompt_text_lang"),
            "英文",
        )
        self.assertEqual(
            map_api_py_language("日语", "prompt_text_lang"),
            "日文",
        )
        self.assertEqual(
            map_api_py_language("韩语", "text_lang"),
            "韩文",
        )

    def test_api_native_language_labels_are_preserved(self):
        self.assertEqual(map_api_py_language(" 中文 ", "prompt_text_lang"), "中文")
        self.assertEqual(map_api_py_language("all_ja", "text_lang"), "all_ja")

    def test_unknown_languages_raise_field_specific_errors(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_clients import (
            UnsupportedPromptLanguageError,
            UnsupportedTextLanguageError,
        )

        with self.assertRaisesRegex(
            UnsupportedPromptLanguageError,
            "prompt_text_lang",
        ) as prompt_error:
            map_api_py_language("unknown-prompt-label", "prompt_text_lang")
        with self.assertRaisesRegex(
            UnsupportedTextLanguageError,
            "text_lang",
        ) as text_error:
            map_api_py_language("unknown-text-label", "text_lang")

        self.assertNotIn("unknown-prompt-label", str(prompt_error.exception))
        self.assertNotIn("unknown-text-label", str(text_error.exception))

    def test_unsplit_payload_disables_punctuation_split(self):
        payload = build_api_py_payload("text", {"split_sentence": False})
        self.assertEqual(payload["cut_punc"], "")



    def test_api_py_audio_post_uses_root_route_and_returns_audio_bytes(self):
        response = FakeResponse()
        client = FakeClient(response)
        factory_calls = []

        def client_factory(**kwargs):
            factory_calls.append(kwargs)
            return client

        result = asyncio.run(
            post_api_py_audio(
                client_factory,
                "http://127.0.0.1:9880/",
                {"text": "safe test"},
                300,
            )
        )

        self.assertEqual(result, b"wav-data")
        self.assertEqual(client.calls, [("http://127.0.0.1:9880/", {"text": "safe test"})])
        self.assertEqual(factory_calls, [{"timeout": 300}])
        self.assertEqual(response.raise_calls, 1)

    def test_model_selection_posts_paths_to_api_set_model(self):
        response = FakeResponse(json_body={"code": 0, "message": "Success"})
        client = FakeClient(response)
        result = asyncio.run(
            post_api_py_model(
                lambda **_: client,
                "http://127.0.0.1:9880/",
                {
                    "gpt_model_path": "gpt.ckpt",
                    "sovits_model_path": "sovits.pth",
                },
                300,
            )
        )

        self.assertIsNone(result)
        self.assertEqual(
            client.calls,
            [
                (
                    "http://127.0.0.1:9880/set_model",
                    {
                        "gpt_model_path": "gpt.ckpt",
                        "sovits_model_path": "sovits.pth",
                    },
                )
            ],
        )

    def test_model_selection_rejects_non_success_api_result(self):
        response = FakeResponse(json_body={"code": 400})
        with self.assertRaises(ModelSelectionError):
            asyncio.run(
                post_api_py_model(
                    lambda **_: FakeClient(response),
                    "http://127.0.0.1:9880",
                    {
                        "gpt_model_path": "gpt.ckpt",
                        "sovits_model_path": "sovits.pth",
                    },
                    300,
                )
            )

    def test_http_error_and_non_audio_response_fail_closed(self):
        class HTTPStatusError(Exception):
            pass

        with self.assertRaises(HTTPStatusError):
            extract_wav_response(
                FakeResponse(error=HTTPStatusError("sensitive body omitted"))
            )
        with self.assertRaises(ValueError):
            extract_wav_response(FakeResponse(content_type="application/json"))
        with self.assertRaises(ValueError):
            extract_wav_response(FakeResponse(content_type="audio/ogg"))
        with self.assertRaises(ValueError):
            extract_wav_response(FakeResponse(content=b""))

    def test_connection_error_is_preserved_as_a_connection_failure(self):
        class ConnectError(Exception):
            pass

        class FailingClient(FakeClient):
            async def post(self, url, *, json):
                self.calls.append((url, json))
                raise ConnectError("private endpoint detail")

        with self.assertRaises(ConnectError) as caught:
            asyncio.run(
                post_api_py_audio(
                    lambda **_: FailingClient(FakeResponse()),
                    "http://127.0.0.1:9880",
                    {"text": "test"},
                    300,
                )
            )
        self.assertEqual(safe_error_type(caught.exception), "ConnectError")
        self.assertNotIn("private endpoint detail", safe_error_type(caught.exception))

    def test_diagnostic_error_type_never_includes_exception_message(self):
        error = RuntimeError("token and transcript must not appear")
        self.assertEqual(safe_error_type(error), "OtherError")
        self.assertNotIn("token", safe_error_type(error))
        self.assertNotIn("transcript", safe_error_type(error))


    def test_every_console_language_option_maps_to_an_api_accepted_label(self):
        api_language_keys = {
            "中文", "粤语", "英文", "日文", "韩文", "中英混合", "粤英混合",
            "日英混合", "韩英混合", "多语种混合", "多语种混合(粤语)",
            "all_zh", "all_yue", "en", "all_ja", "all_ko", "zh", "yue", "ja", "ko", "auto", "auto_yue",
        }
        for option in sorted(TTS_LANGUAGES):
            with self.subTest(option=option):
                self.assertIn(map_api_py_language(option, "text_lang"), api_language_keys)


class ProfileParsingTests(unittest.TestCase):
    def test_valid_profile_is_returned_as_a_copy(self):
        profile = _profile()
        result = usable_gpt_profile(profile)
        self.assertEqual(result, profile)
        self.assertIsNot(result, profile)

    def test_japanese_language_labels_are_both_accepted(self):
        for prompt_lang, text_lang in (("日文", "日文"), ("日语", "日语"), ("日语", "日英混合")):
            with self.subTest(prompt_lang=prompt_lang, text_lang=text_lang):
                profile = _profile(prompt_text_lang=prompt_lang, text_lang=text_lang)
                self.assertIsNotNone(usable_gpt_profile(profile))
                payload = build_api_py_payload("テキスト", build_profile_tts_settings(profile))
                self.assertEqual(payload["prompt_language"], "日文")
                self.assertIn(payload["text_language"], {"日文", "日英混合"})

    def test_invalid_profiles_are_ignored(self):
        cases = (
            None,
            "not a dict",
            [("ref_audio_path", "a.wav")],
            _profile(ref_audio_path=""),
            _profile(ref_audio_path="   "),
            _profile(ref_audio_path=None),
            _profile(prompt_text_lang="klingon"),
            _profile(text_lang=None),
            {"ref_audio_path": "a.wav"},
            _style_profile(),
        )
        for profile in cases:
            with self.subTest(profile=profile):
                self.assertIsNone(usable_gpt_profile(profile))

    def test_speed_factor_is_translated_to_plugin_setting_name(self):
        settings = build_profile_tts_settings(_profile())
        self.assertEqual(settings["speed_facter"], 1.25)
        self.assertNotIn("speed_factor", settings)
        payload = build_api_py_payload("テキスト", settings)
        self.assertEqual(payload["speed"], 1.25)
        self.assertEqual(payload["refer_wav_path"], r"D:\GPT-SoVITS\ckyp\ktn_轻快.wav")
        self.assertEqual(payload["prompt_text"], "やったね")
        self.assertEqual(payload["top_k"], 15)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["temperature"], 0.8)
        self.assertEqual(payload["cut_punc"], "")
        for key in ("speed_factor", "speed_facter", "emotion", "character", "gpt_model_path"):
            self.assertNotIn(key, payload)

    def test_missing_optional_fields_use_plugin_defaults(self):
        settings = build_profile_tts_settings(
            {"ref_audio_path": "a.wav", "prompt_text_lang": "日文", "text_lang": "日文"}
        )
        payload = build_api_py_payload("テキスト", settings)
        self.assertEqual(payload["speed"], 1.0)
        self.assertEqual(payload["top_k"], 10)
        self.assertEqual(payload["prompt_text"], "")
        self.assertNotEqual(payload["cut_punc"], "")

    def test_profile_model_pair_requires_both_paths(self):
        self.assertEqual(profile_model_payload(_profile(**MODEL_A)), MODEL_A)
        self.assertIsNone(profile_model_payload(_profile()))
        self.assertIsNone(profile_model_payload(None))
        self.assertIsNone(profile_model_payload(_profile(gpt_model_path=r"D:\a.ckpt")))
        self.assertIsNone(profile_model_payload(_profile(gpt_model_path="", sovits_model_path=r"D:\a.pth")))
        self.assertIsNone(profile_model_payload(_profile(gpt_model_path=1, sovits_model_path=2)))


class PayloadTests(unittest.TestCase):
    def test_emoji_is_prefixed_and_options_are_fixed(self):
        payload = build_irodori_payload("こんにちは。", _style_profile(), SETTINGS)
        self.assertEqual(payload["model"], "irodori-tts")
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(payload["input"], "😊こんにちは。")
        options = payload["irodori"]
        self.assertIs(options["no_ref"], True)
        self.assertIs(options["chunking_enabled"], False)
        self.assertEqual(options["caption"], "嬉しそうに話している。")
        self.assertEqual(options["lora_adapter"], SETTINGS["lora_adapter"])
        self.assertEqual(options["cfg_scale_text"], 3.0)
        self.assertEqual(options["cfg_scale_caption"], 4.0)
        self.assertNotIn("num_steps", options)
        self.assertNotIn("voice", payload)

    def test_empty_emoji_leaves_text_untouched(self):
        for emoji in ("", "   ", None):
            payload = build_irodori_payload("こんにちは。", _style_profile(emoji=emoji), SETTINGS)
            self.assertEqual(payload["input"], "こんにちは。")

    def test_caption_falls_back_to_default_then_is_omitted(self):
        profile = _style_profile(caption="")
        with_default = build_irodori_payload("a", profile, {**SETTINGS, "default_caption": " 普段の調子。 "})
        self.assertEqual(with_default["irodori"]["caption"], "普段の調子。")
        without = build_irodori_payload("a", profile, SETTINGS)
        self.assertNotIn("caption", without["irodori"])
        # a profile caption wins over the default
        both = build_irodori_payload("a", _style_profile(), {**SETTINGS, "default_caption": "既定"})
        self.assertEqual(both["irodori"]["caption"], "嬉しそうに話している。")

    def test_no_profile_uses_default_caption_and_no_emoji(self):
        payload = build_irodori_payload("a", None, {**SETTINGS, "default_caption": "既定"})
        self.assertEqual(payload["input"], "a")
        self.assertEqual(payload["irodori"]["caption"], "既定")

    def test_num_steps_only_when_positive(self):
        for steps, expected in ((0, None), (-3, None), (None, None), (28, 28), ("12", 12)):
            with self.subTest(steps=steps):
                options = build_irodori_payload("a", None, {**SETTINGS, "num_steps": steps})["irodori"]
                self.assertEqual(options.get("num_steps"), expected)

    def test_empty_lora_is_omitted(self):
        for lora in ("", "  ", None):
            options = build_irodori_payload("a", None, {**SETTINGS, "lora_adapter": lora})["irodori"]
            self.assertNotIn("lora_adapter", options)

    def test_cfg_scales_are_always_sent_as_floats(self):
        options = build_irodori_payload(
            "a", None, {**SETTINGS, "cfg_scale_text": "2.5", "cfg_scale_caption": 5}
        )["irodori"]
        self.assertEqual((options["cfg_scale_text"], options["cfg_scale_caption"]), (2.5, 5.0))


class RealServerPostTests(unittest.TestCase):
    """post_irodori_audio against a real HTTP server with real httpx."""

    def _run(self, handler, payload=None):
        import httpx

        async def scenario():
            app = web.Application()
            app.router.add_post("/v1/audio/speech", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                return await post_irodori_audio(
                    httpx.AsyncClient, f"http://127.0.0.1:{port}/", payload or {"input": "x"}, 10
                )
            finally:
                await runner.cleanup()

        return asyncio.run(scenario())

    def test_wav_response_is_returned_and_json_body_is_sent(self):
        seen = {}

        async def handler(request):
            seen["json"] = await request.json()
            return web.Response(body=b"RIFFfake", content_type="audio/wav")

        payload = build_irodori_payload("こんにちは", _style_profile(), SETTINGS)
        self.assertEqual(self._run(handler, payload), b"RIFFfake")
        self.assertEqual(seen["json"], payload)

    def test_error_status_exposes_only_status_and_type(self):
        async def handler(request):
            return web.json_response(
                {"error": {"message": "secret D:\\path\\ktn failure", "type": "server_error"}}, status=500
            )

        with self.assertRaises(IrodoriRequestError) as caught:
            self._run(handler)
        self.assertEqual((caught.exception.status, caught.exception.error_type), (500, "server_error"))
        self.assertNotIn("secret", str(caught.exception))

    def test_non_json_error_and_non_wav_success_are_rejected(self):
        async def broken(request):
            return web.Response(text="<html>oops</html>", status=502)

        async def not_audio(request):
            return web.json_response({"ok": True})

        async def empty(request):
            return web.Response(body=b"", content_type="audio/wav")

        with self.assertRaises(IrodoriRequestError) as caught:
            self._run(broken)
        self.assertEqual((caught.exception.status, caught.exception.error_type), (502, "unknown"))
        for handler in (not_audio, empty):
            with self.assertRaises(IrodoriRequestError) as caught:
                self._run(handler)
            self.assertEqual((caught.exception.status, caught.exception.error_type), (200, "invalid_audio"))


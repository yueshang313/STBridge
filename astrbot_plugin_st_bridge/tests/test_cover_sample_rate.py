"""V1b: the cover output sample rate (24 kHz mono default / 44.1 kHz stereo) in config, job params, worker and presets."""

import unittest

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_config import (
    cover_settings,
    default_cover_settings,
    validate_cover_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_library import cache_key
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import job_params, params_summary
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import EmotionConfigError, default_config
from test_cover_service import ServiceCase
from test_cover_worker import request, worker


class ConfigTests(unittest.TestCase):
    def test_default_is_24k(self):
        self.assertEqual(default_cover_settings()["output_sample_rate"], 24000)
        self.assertEqual(validate_cover_settings({})["output_sample_rate"], 24000)

    def test_both_allowed_values_are_accepted(self):
        for rate in (24000, 44100):
            self.assertEqual(validate_cover_settings({"output_sample_rate": rate})["output_sample_rate"], rate)

    def test_other_values_are_a_400(self):
        for bad in (48000, 22050, 0, -1, "24000", 24000.5, None, True, [24000]):
            with self.subTest(bad=bad):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_cover_settings({"output_sample_rate": bad})
                self.assertEqual(caught.exception.status, 400)

    def test_reading_a_missing_or_broken_value_falls_back_to_the_default_and_keeps_the_rest(self):
        for raw in ({"port": 8123}, {"port": 8123, "output_sample_rate": 12345}, {"port": 8123, "output_sample_rate": "x"}):
            with self.subTest(raw=raw):
                settings = cover_settings({"cover": raw})
                self.assertEqual((settings["output_sample_rate"], settings["port"]), (24000, 8123))
        self.assertEqual(cover_settings({"cover": {"output_sample_rate": 44100}})["output_sample_rate"], 44100)

    def test_an_old_preset_without_the_field_gets_the_default(self):
        old = default_config()
        del old["cover"]["output_sample_rate"]
        result = config_presets.normalize_config(old, default_config())
        self.assertEqual(result["cover"]["output_sample_rate"], 24000)
        explicit = default_config()
        explicit["cover"]["output_sample_rate"] = 44100
        self.assertEqual(config_presets.normalize_config(explicit, default_config())["cover"]["output_sample_rate"], 44100)


class JobParamTests(unittest.TestCase):
    def test_rate_is_part_of_the_job_params_and_the_cache_key(self):
        low = job_params(validate_cover_settings({}), "", 0.0, 60.0)
        high = job_params(validate_cover_settings({"output_sample_rate": 44100}), "", 0.0, 60.0)
        self.assertEqual((low["output_sample_rate"], high["output_sample_rate"]), (24000, 44100))
        self.assertNotEqual(cache_key("abc", low), cache_key("abc", high))
        self.assertEqual(cache_key("abc", low), cache_key("abc", dict(low)))

    def test_summary_shows_the_rate(self):
        self.assertIn("24 kHz", params_summary(job_params(validate_cover_settings({}), "", 0.0, 60.0)))
        self.assertIn("44.1 kHz", params_summary(job_params(validate_cover_settings({"output_sample_rate": 44100}), "", 0.0, 60.0)))


class WorkerTests(unittest.TestCase):
    def mix(self, **kwargs):
        return worker.build_mix_command("ffmpeg", "v.wav", "i.wav", "out.wav", 0, 0, **kwargs)

    def test_24k_is_mono_and_44k_is_stereo_while_the_mix_graph_stays_44k(self):
        for rate, channels in ((24000, "1"), (44100, "2")):
            with self.subTest(rate=rate):
                command = self.mix(output_rate=rate)
                self.assertEqual(command[command.index("-ar") + 1], str(rate))
                self.assertEqual(command[command.index("-ac") + 1], channels)
                graph = command[command.index("-filter_complex") + 1]
                self.assertEqual(graph.count("aresample=44100"), 2)
                self.assertEqual(command[-1], "out.wav")

    def test_default_output_is_still_44k_stereo(self):
        command = self.mix()
        self.assertEqual((command[command.index("-ar") + 1], command[command.index("-ac") + 1]), ("44100", "2"))

    def test_request_validation(self):
        self.assertEqual(worker.validate_cover_request(request())["output_sample_rate"], 44100)  # older callers
        for rate in (24000, 44100):
            self.assertEqual(worker.validate_cover_request(request(output_sample_rate=rate))["output_sample_rate"], rate)
        for bad in (48000, "24000", True, None, 0):
            with self.subTest(bad=bad):
                with self.assertRaises(worker.CoverError) as caught:
                    worker.validate_cover_request(request(output_sample_rate=bad))
                self.assertEqual(caught.exception.status, 400)


class ServicePayloadTests(ServiceCase):
    async def test_the_worker_gets_the_rate_and_a_new_rate_misses_the_cache(self):
        song = await self.add_song()
        await self.service.sing(song["id"])
        self.assertEqual(self.posts[0][1]["output_sample_rate"], 24000)
        self.settings = validate_cover_settings({**self.settings, "output_sample_rate": 44100})
        again = await self.service.sing(song["id"])
        self.assertEqual(len(self.posts), 2)  # not served from the 24 kHz cache entry
        self.assertEqual(self.posts[1][1]["output_sample_rate"], 44100)
        self.assertFalse(again.get("cached", False))


if __name__ == "__main__":
    unittest.main()

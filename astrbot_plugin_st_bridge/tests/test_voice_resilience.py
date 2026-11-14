"""101e: the voice never holds the text back for long (wait limit) and a broken start backs off (cooldown)."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    default_tts_settings,
    validate_tts_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_coordinator import (
    START_FAILURE_COOLDOWN_SECONDS,
    TtsCoordinator,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.tts_service import ServiceStartError
from test_tts_coordinator import CoordinatorCase
from test_tts_handler import HandlerCase


class VoiceWaitSettingTests(unittest.TestCase):
    def test_default_and_range(self):
        self.assertEqual(default_tts_settings()["common"]["voice_wait_seconds"], 60)
        for value in (10, 300):
            settings = default_tts_settings()
            settings["common"]["voice_wait_seconds"] = value
            self.assertEqual(validate_tts_settings(settings)["common"]["voice_wait_seconds"], value)
        for value in (9, 301, "x"):
            settings = default_tts_settings()
            settings["common"]["voice_wait_seconds"] = value
            with self.assertRaises(Exception):
                validate_tts_settings(settings)


class VoiceWaitLimitTests(HandlerCase):
    engine = "irodori"

    def test_slow_voice_sends_the_text_only_and_the_synthesis_is_cancelled(self):
        self.set_common(voice_wait_seconds=0.05, send_text_with_audio=True)
        state = {"cancelled": False, "finished": False}

        async def slow(text, profile):
            try:
                await asyncio.sleep(5)
                state["finished"] = True
                return b"late-audio"
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        self.tts.synthesize = slow
        event = self.event(text="ST reply")
        original = event.result.chain[0]
        self.run_event(event)
        self.assert_text_kept(event, original)
        self.assertTrue(state["cancelled"])
        self.assertFalse(state["finished"])
        # Nothing was written for the late audio.
        temp = Path(self.tmp.name) / "temp_audio"
        self.assertEqual(list(temp.glob("*")) if temp.exists() else [], [])
        self.assertTrue(any("VoiceWaitTimeout" in line for line in self.logger.warnings))

    def test_a_voice_in_time_is_still_sent(self):
        self.set_common(voice_wait_seconds=5)
        event = self.run_event(self.event(text="ST reply"))
        self.assert_record_only(event)


class StartCooldownTests(CoordinatorCase):
    def make_with_clock(self):
        self.now = [100.0]
        return TtsCoordinator(
            self.settings("irodori"),
            data_dir=self.data_dir,
            logger=self.logger,
            manager_kwargs=dict(
                launcher=self.host.launcher,
                probe=self.host.probe,
                killer=self.host.killer,
                job_factory=FakeJobProxy,
                clock=self.host.clock,
                sleep=self.host.sleep,
            ),
            clock=lambda: self.now[0],
        )

    def test_failed_start_pauses_retries_then_allows_one_again(self):
        self.host.exit_code = 2
        tts = self.make_with_clock()

        async def scenario():
            with self.assertRaises(ServiceStartError):
                await tts.ensure_service_ready()
            launches = len(self.host.launches)
            self.assertGreater(tts.start_cooldown_remaining(), 0)
            self.assertEqual((await tts.status())["cooldown_seconds"], round(START_FAILURE_COOLDOWN_SECONDS))
            # Inside the pause: refused at once, nothing is launched.
            self.now[0] += 60
            with self.assertRaises(ServiceStartError) as caught:
                await tts.ensure_service_ready()
            self.assertIn("才会再试", str(caught.exception))
            self.assertEqual(len(self.host.launches), launches)
            # After the pause the next message tries again.
            self.now[0] += START_FAILURE_COOLDOWN_SECONDS
            self.assertEqual(tts.start_cooldown_remaining(), 0)
            with self.assertRaises(ServiceStartError):
                await tts.ensure_service_ready()
            self.assertEqual(len(self.host.launches), launches + 1)

        asyncio.run(scenario())

    def test_manual_start_ignores_and_clears_the_pause(self):
        self.host.exit_code = 2
        tts = self.make_with_clock()

        async def scenario():
            with self.assertRaises(ServiceStartError):
                await tts.ensure_service_ready()
            self.assertGreater(tts.start_cooldown_remaining(), 0)
            self.host.exit_code = None
            await tts.start_service()
            self.assertEqual(tts.start_cooldown_remaining(), 0)
            self.assertEqual((await tts.status())["cooldown_seconds"], 0)

        asyncio.run(scenario())

    def test_a_service_that_is_up_is_used_during_the_pause(self):
        self.host.exit_code = 2
        tts = self.make_with_clock()

        async def scenario():
            with self.assertRaises(ServiceStartError):
                await tts.ensure_service_ready()
            self.host.port_open = True  # somebody started it by hand
            await tts.ensure_service_ready()

        asyncio.run(scenario())


from test_tts_service import FakeJob as FakeJobProxy  # noqa: E402

if __name__ == "__main__":
    unittest.main()

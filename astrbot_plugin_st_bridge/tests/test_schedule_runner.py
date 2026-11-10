"""T1a: the proactive (scheduled) turn in the plugin: ST request, fallback, voice/text/sticker sends."""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_service import ScheduleTargets, group_key_for
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.sticker_store import StickerStore
from test_plugin_lifecycle import (
    _bridge_event,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

OWNER_UMO = "qq_official:FriendMessage:owner-openid-secret"
GROUP_UMO = "qq_official:GroupMessage:group-openid-secret"
HAPPY = "开心"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
REPLY = f"[情绪:{HAPPY}]☆‹「おはよう」›\n\"早安，制作人。\""
NOW = datetime(2026, 11, 2, 7, 30)


def plan(**overrides):
    value = {
        "id": "0a1b2c3d",
        "name": "早安",
        "enabled": True,
        "kind": "daily",
        "time": "07:30",
        "weekdays": [],
        "jitter_minutes": 0,
        "target": {"type": "private"},
        "prompt": "向制作人道早安",
        "fallback_texts": ["制作人，早安。", "第二句"],
    }
    value.update(overrides)
    return value


class FakeTts:
    def __init__(self, temp_dir, *, prob=100, cooldown=0, text_limit=0, send_text_with_audio=True):
        self.common = {
            "prob": prob,
            "cooldown": cooldown,
            "text_limit": text_limit,
            "send_text_with_audio": send_text_with_audio,
        }
        self.temp_dir = Path(temp_dir)
        self.calls = []
        self.error = None

    async def synthesize(self, text, profile):
        self.calls.append((text, profile))
        if self.error is not None:
            raise self.error
        return b"RIFF-fake-wav"


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        data = Path(self.directory.name)
        self.plugin = _ready_plugin(self.module)
        self.plugin._core._emotion_store = ConfigStore(data)
        self.plugin._core._sticker_store = StickerStore(data)
        self.plugin._core._sticker_store.add(HAPPY, "a.png", PNG)
        self.plugin._core._sticker_random = lambda: 0.0
        self.plugin._core._schedule_now = lambda: NOW
        self.plugin._core._schedule_random = lambda: 0.0
        self.plugin._core._schedule_choice = lambda items: items[0]
        self.targets = ScheduleTargets(data, clock=lambda: NOW)
        self.targets.record_owner(OWNER_UMO)
        self.group_key = self.targets.record_group(GROUP_UMO)
        self.plugin._core._schedule_targets = self.targets
        self.tts = FakeTts(data / "tts_tmp")
        self.plugin._core._tts = self.tts
        self.sends = []
        self.send_error = {}
        self.seen_files = []
        self.attempts = 0

        async def send_message(umo, chain):
            kind = type(chain.chain[0]).__name__
            if kind in self.send_error:
                raise self.send_error[kind]
            if kind == "FakeRecord":
                path = Path(chain.chain[0].file)
                self.seen_files.append((path, path.exists()))
                if getattr(self, "voice_probe", None):
                    self.voice_probe(str(path))
            self.sends.append((umo, kind, chain.chain))
            return True

        self.plugin.context = SimpleNamespace(send_message=send_message)
        self.module.test_logger.infos.clear()
        self.module.test_logger.errors.clear()

    async def run_plan(self, the_plan=None, *, reply=REPLY, forced=False, st="reply", keep_last=False):
        the_plan = the_plan or plan()
        if not keep_last:
            # L1d: a reply too like the session's last message is dropped; these tests run the same REPLY
            # again and again, so each run starts as if nothing had been said (the duplicate rule has its own tests).
            self.targets._last_text.clear()
        task = asyncio.create_task(self.plugin._core._run_proactive(the_plan, forced))
        manager = self.plugin._core._request_state_manager
        request = None
        if st is not None:
            request = await _wait_for_request(manager)
            if st == "reply":
                self.attempts += 1
                manager.claim_request(request.request_id, f"attempt-{self.attempts}")
                manager.mark_generation_started(request.request_id)
                manager.confirm_generation_finished(request.request_id)
                manager.complete_request(request.request_id, reply)
            else:
                manager.fail_request(request.request_id, "generation_failed")
        result = await asyncio.wait_for(task, 10)
        return result, request

    def kinds(self):
        return [kind for _umo, kind, _chain in self.sends]

    def schedule_logs(self):
        return [line for line in self.module.test_logger.infos if "Schedule plan=" in line]

    async def test_request_is_proactive_with_the_narrator_text(self):
        result, request = await self.run_plan()
        self.assertEqual(request.kind, "proactive")
        self.assertEqual(
            request.content,
            "（系统提示：现在是 2026-11-02 星期一 07:30。向制作人道早安"
            "这是一条独立的主动消息，不是在回复上一条，不要重复你之前说过的话。）",
        )
        self.assertEqual(result["result"], "sent")

    async def test_group_plan_adds_the_group_hint_and_targets_the_group(self):
        group_plan = plan(target={"type": "group", "key": self.group_key})
        result, request = await self.run_plan(group_plan)
        self.assertTrue(request.content.endswith("（这条消息会发到群里）"))
        self.assertEqual({umo for umo, _kind, _chain in self.sends}, {GROUP_UMO})
        self.assertEqual(result["result"], "sent")

    async def test_private_plan_goes_to_the_owner_session(self):
        await self.run_plan()
        self.assertEqual({umo for umo, _kind, _chain in self.sends}, {OWNER_UMO})

    async def test_voice_text_sticker_are_three_separate_sends_in_that_order(self):
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain", "FakeImage"])
        for _umo, _kind, chain in self.sends:
            self.assertEqual(len(chain), 1)
        self.assertEqual(self.sends[1][2][0].text, "早安，制作人。")
        self.assertTrue(self.sends[2][2][0] is not None)
        self.assertEqual(
            {key: result[key] for key in ("result", "voice", "sticker")},
            {"result": "sent", "voice": "sent", "sticker": "sent"},
        )
        self.assertIn("おはよう", self.tts.calls[0][0])

    async def test_voice_file_exists_while_sending_and_is_cleaned_up(self):
        await self.run_plan()
        (path, existed), = self.seen_files
        self.assertTrue(existed)
        self.assertFalse(path.exists())

    async def test_voice_file_is_24k_mono_16bit(self):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(32000)
            writer.writeframes((1000).to_bytes(2, "little") * 3200)

        async def synthesize(text, profile):
            return buffer.getvalue()

        self.tts.synthesize = synthesize
        sizes = []

        def record(path_text):
            with wave.open(path_text, "rb") as reader:
                sizes.append((reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()))

        self.voice_probe = record
        await self.run_plan()
        self.assertEqual(sizes, [(24000, 1, 2, 2400)])

    async def _voice_format_with(self, engine, output_mode):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes((1000).to_bytes(2, "little") * 4800)

        async def synthesize(text, profile):
            return buffer.getvalue()

        self.tts.synthesize = synthesize
        self.tts.settings = {"engine": engine, "irodori": {"output_mode": output_mode}}
        formats = []

        def record(path_text):
            with wave.open(path_text, "rb") as reader:
                formats.append((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()))

        self.voice_probe = record
        await self.run_plan()
        return formats

    async def test_irodori_native_mode_keeps_the_48k_voice(self):
        self.assertEqual(await self._voice_format_with("irodori", "native"), [(48000, 1, 2)])

    async def test_irodori_qq_24k_mode_converts(self):
        self.assertEqual(await self._voice_format_with("irodori", "qq_24k"), [(24000, 1, 2)])

    async def test_gpt_sovits_converts_whatever_the_irodori_field_says(self):
        self.assertEqual(await self._voice_format_with("gpt_sovits", "native"), [(24000, 1, 2)])

    async def test_voice_file_is_cleaned_up_when_the_send_fails(self):
        self.send_error["FakeRecord"] = RuntimeError("platform error")
        result, _ = await self.run_plan()
        self.assertEqual(result["voice"], "failed:RuntimeError")
        self.assertEqual(list(self.tts.temp_dir.glob("*.wav")), [])

    async def test_affinity_changes_in_the_reply_are_not_applied(self):
        def boom(*args, **kwargs):
            raise AssertionError("affinity must not be applied")

        self.plugin._core._apply_affinity = boom
        reply = REPLY + "\n<UpdateVariable><JSONPatch>[{\"op\":\"add\",\"path\":\"/藤田琴音/好感度/0\",\"value\":5}]</JSONPatch></UpdateVariable>"
        result, _ = await self.run_plan(reply=reply)
        self.assertEqual(result["result"], "sent")

    async def test_owner_affinity_is_handed_to_st_like_a_private_turn(self):
        self.plugin._core._affinity_store = AffinityStore(
            Path(self.directory.name), owner_key=self.plugin._core._owner_key(), group_initial_value=0
        )
        _, request = await self.run_plan()
        # Nobody asked for a song in a scheduled message: the permission variable is always 0.
        self.assertEqual(len(request.affinity_writes), 3)
        self.assertIsNotNone(request.affinity_writes[0]["value"])
        self.assertIsNone(request.affinity_writes[1]["value"])
        self.assertEqual(request.affinity_writes[2], {"path": ["点歌许可"], "value": 0})

    async def test_the_permission_stays_zero_even_when_song_requests_are_possible(self):
        self.plugin._core._affinity_store = AffinityStore(
            Path(self.directory.name), owner_key=self.plugin._core._owner_key(), group_initial_value=0
        )
        self.plugin._core._cover = SimpleNamespace()  # a cover service exists: a chat turn would be allowed
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 1)
        _, request = await self.run_plan()
        self.assertEqual(request.affinity_writes[2], {"path": ["点歌许可"], "value": 0})
        self.assertEqual(len(request.affinity_writes), 3)

    async def test_a_sing_tag_in_a_scheduled_reply_is_stripped_and_does_nothing(self):
        self.plugin._core._cover = SimpleNamespace()
        reply = REPLY.replace("]☆", "][唱歌:晴天]☆", 1)
        result, _ = await self.run_plan(reply=reply)
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.sends[1][2][0].text, "早安，制作人。")
        self.assertNotIn("唱歌", self.tts.calls[0][0])
        self.assertEqual(self.plugin._core._song_pending, {})

    async def test_group_turn_hands_over_owner_value_and_null_group_value(self):
        self.plugin._core._affinity_store = AffinityStore(
            Path(self.directory.name), owner_key=self.plugin._core._owner_key(), group_initial_value=0
        )
        _, request = await self.run_plan(plan(target={"type": "group", "key": self.group_key}))
        self.assertIsNotNone(request.affinity_writes[0]["value"])
        self.assertIsNone(request.affinity_writes[1]["value"])

    # The last group's mood (开心) is the one with a sticker.
    async def _mood_setup(self):
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mood_store import MoodStore

        core = self.plugin._core
        await core._emotion_store.update(lambda config: config["characters"].update({
            "琴音": {"name": "琴音", "model_name": "", "default_emotion": HAPPY, "emotions": {HAPPY: {}}}
        }) or config.update(default_character="琴音"))
        core._mood_store = MoodStore(Path(self.directory.name) / "mood_data")
        return core._mood_store

    async def test_mood_private_plan_records_both_lists_and_group_plan_only_the_overall(self):
        moods = await self._mood_setup()
        await self.run_plan()
        self.assertEqual([e["emotion"] for e in moods.entries("global")], [HAPPY])
        self.assertEqual([e["emotion"] for e in moods.entries("owner")], [HAPPY])
        await self.run_plan(plan(target={"type": "group", "key": self.group_key}))
        self.assertEqual(len(moods.entries("global")), 2)
        self.assertEqual(len(moods.entries("owner")), 1)

    async def test_mood_invalid_reply_and_failed_st_record_nothing_but_a_failed_send_still_does(self):
        moods = await self._mood_setup()
        await self.run_plan(reply="没有台词")
        await self.run_plan(st="fail")
        self.assertEqual((moods.entries("global"), moods.entries("owner")), ([], []))
        self.send_error["FakePlain"] = RuntimeError("down")
        self.send_error["FakeRecord"] = RuntimeError("down")
        result, _ = await self.run_plan()
        self.assertTrue(result["result"].startswith("failed:"))
        self.assertEqual([e["emotion"] for e in moods.entries("owner")], [HAPPY])

    async def test_mood_macros_reach_the_scheduled_request_even_for_a_group_plan(self):
        moods = await self._mood_setup()
        moods.record(HAPPY, ("global", "owner"))
        _, request = await self.run_plan(plan(target={"type": "group", "key": self.group_key}))
        self.assertIn(HAPPY, request.macros["mood"])
        self.assertIn(HAPPY, request.macros["owner_mood"])

    TWO_GROUPS = f"[情绪:生气]☆‹「おはよう」›\"早安\"[情绪:{HAPPY}]☆‹「ねえ」›\"喂\""

    def _split(self, on=True, **values):
        self.tts.common.update(split_messages=on, split_interval=2.0, split_max=3, **values)
        self.pauses = []

        async def pause(seconds):
            self.pauses.append(seconds)

        self.plugin._core._split_pause = pause

    async def test_several_groups_merge_into_one_voice_and_text_by_default(self):
        self._split(False)
        result, _ = await self.run_plan(reply=self.TWO_GROUPS)
        # Merged: the first tag (生气, no sticker) is the reply's emotion.
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain"])
        self.assertEqual(self.sends[1][2][0].text, "早安\n喂")
        self.assertEqual(self.tts.calls[0][0], "「おはよう」「ねえ」")
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.pauses, [])

    async def test_split_sends_voice_then_text_per_group_and_the_sticker_last(self):
        self._split(True)
        result, _ = await self.run_plan(reply=self.TWO_GROUPS)
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain", "FakeRecord", "FakePlain", "FakeImage"])
        self.assertEqual([self.sends[1][2][0].text, self.sends[3][2][0].text], ["早安", "喂"])
        self.assertEqual([call[0] for call in self.tts.calls], ["「おはよう」", "「ねえ」"])
        self.assertEqual(self.pauses, [2.0])
        self.assertEqual(
            {key: result[key] for key in ("result", "voice", "sticker")},
            {"result": "sent", "voice": "sent", "sticker": "sent"},
        )

    async def test_split_gates_the_voice_once_for_the_whole_reply(self):
        self._split(True)
        self.plugin._core._schedule_random = lambda: 0.99
        self.tts.common["prob"] = 50
        result, _ = await self.run_plan(reply=self.TWO_GROUPS)
        self.assertEqual(self.kinds(), ["FakePlain", "FakePlain", "FakeImage"])
        self.assertEqual(result["voice"], "skip:Probability")
        self.assertEqual(result["result"], "sent")
        self.assertEqual(self.tts.calls, [])

    async def test_split_respects_the_group_limit_and_text_with_audio_off(self):
        self._split(True, send_text_with_audio=False)
        self.tts.common["split_max"] = 2
        reply = '[情绪:开心]☆‹a›"1"☆‹b›"2"☆‹c›"3"'
        result, _ = await self.run_plan(reply=reply)
        self.assertEqual(self.kinds(), ["FakeRecord", "FakeRecord", "FakeImage"])
        self.assertEqual([call[0] for call in self.tts.calls], ["a", "b。c"])

    async def test_split_failure_of_one_group_is_reported_but_the_rest_go_on(self):
        self._split(True)
        self.send_error["FakePlain"] = RuntimeError("down")
        result, _ = await self.run_plan(reply=self.TWO_GROUPS)
        self.assertEqual(result["result"], "failed:RuntimeError")
        self.assertEqual(self.kinds(), ["FakeRecord", "FakeRecord", "FakeImage"])

    async def test_send_text_with_audio_false_skips_the_text_after_a_voice(self):
        self.tts.common["send_text_with_audio"] = False
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakeRecord", "FakeImage"])
        self.assertEqual(result["result"], "sent")

    async def test_send_text_with_audio_false_still_sends_text_without_a_voice(self):
        self.tts.common["send_text_with_audio"] = False
        self.tts.error = RuntimeError("synthesis failed")
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakePlain", "FakeImage"])
        self.assertEqual(result["voice"], "failed:RuntimeError")

    async def test_voice_probability_and_cooldown_skip_only_the_voice(self):
        self.plugin._core._schedule_random = lambda: 0.99
        self.tts.common["prob"] = 50
        result, _ = await self.run_plan()
        self.assertEqual(result["voice"], "skip:Probability")
        self.assertEqual(self.kinds(), ["FakePlain", "FakeImage"])

        self.sends.clear()
        self.plugin._core._schedule_random = lambda: 0.0
        self.tts.common["prob"] = 100
        self.tts.common["cooldown"] = 600
        first, _ = await self.run_plan()
        self.assertEqual(first["voice"], "sent")
        second, _ = await self.run_plan()
        self.assertEqual(second["voice"], "skip:Cooldown")

    async def test_forced_run_ignores_probability_and_cooldown(self):
        self.plugin._core._schedule_random = lambda: 0.99
        self.tts.common["prob"] = 0
        self.tts.common["cooldown"] = 600
        self.plugin._core._sticker_random = lambda: 0.999
        await self.run_plan(forced=True)
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain", "FakeImage"])
        self.sends.clear()
        await self.run_plan(forced=True)
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain", "FakeImage"])

    async def test_voice_failure_never_stops_text_and_sticker(self):
        self.tts.error = RuntimeError("secret-detail")
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakePlain", "FakeImage"])
        self.assertEqual(result["voice"], "failed:RuntimeError")
        self.assertEqual(result["result"], "sent")
        self.assertTrue(self.module.test_logger.errors)
        self.assertFalse(any("secret-detail" in line for line in self.module.test_logger.errors))

    async def test_text_failure_never_stops_the_sticker(self):
        self.send_error["FakePlain"] = RuntimeError("no")
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakeRecord", "FakeImage"])
        self.assertEqual(result["result"], "failed:RuntimeError")
        self.assertEqual(result["sticker"], "sent")

    async def test_sticker_failure_never_changes_voice_or_text(self):
        self.send_error["FakeImage"] = ValueError("upload")
        result, _ = await self.run_plan()
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain"])
        self.assertEqual(result["sticker"], "failed:OtherError")
        self.assertEqual(result["result"], "sent")

    async def test_sticker_skip_reasons(self):
        result, _ = await self.run_plan(reply="☆‹「おはよう」›\n\"早安\"")
        self.assertEqual(result["sticker"], "skip:NoTag")
        self.sends.clear()
        result, _ = await self.run_plan(reply="[情绪:生气]☆‹「おはよう」›\n\"早安\"")
        self.assertEqual(result["sticker"], "skip:NoImages")
        self.assertEqual(self.kinds(), ["FakeRecord", "FakePlain"])

    async def test_sticker_uses_a_per_target_cooldown(self):
        first, _ = await self.run_plan()
        self.assertEqual(first["sticker"], "sent")
        self.sends.clear()
        second, _ = await self.run_plan()
        self.assertEqual(second["sticker"], "skip:Cooldown")
        self.sends.clear()
        other, _ = await self.run_plan(plan(target={"type": "group", "key": self.group_key}))
        self.assertEqual(other["sticker"], "sent")

    async def test_st_failure_sends_a_fallback_text_only(self):
        result, _ = await self.run_plan(st="fail")
        self.assertEqual(self.kinds(), ["FakePlain"])
        self.assertEqual(self.sends[0][2][0].text, "制作人，早安。")
        self.assertEqual(result["result"], "fallback")
        self.assertEqual((result["voice"], result["sticker"]), ("skip:Fallback", "skip:Fallback"))
        self.assertEqual(self.tts.calls, [])

    async def test_invalid_reply_format_counts_as_a_failure(self):
        result, _ = await self.run_plan(reply="没有双语格式的回复")
        self.assertEqual(result["result"], "fallback")
        self.assertEqual(self.kinds(), ["FakePlain"])

    async def test_no_fallback_text_means_log_only(self):
        result, _ = await self.run_plan(plan(fallback_texts=[]), st="fail")
        self.assertEqual(self.sends, [])
        self.assertEqual(result["result"], "skip:NoFallbackText")

    async def test_fallback_send_failure_is_reported(self):
        self.send_error["FakePlain"] = RuntimeError("no")
        result, _ = await self.run_plan(st="fail")
        self.assertEqual(result["result"], "failed:RuntimeError")

    async def test_closed_queue_and_stopped_bridge_use_the_fallback(self):
        self.plugin._core._queue.close()
        result, request = await self.run_plan(st=None)
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())
        self.assertEqual(result["result"], "fallback")

    async def test_unavailable_bridge_uses_the_fallback_without_a_request(self):
        self.plugin._core._http_service = SimpleNamespace(state=HttpServiceState.STOPPED)
        result, _ = await self.run_plan(st=None)
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())
        self.assertEqual(result["result"], "fallback")

    async def test_full_queue_uses_the_fallback(self):
        self.plugin._core._queue.enqueue("someone", "waiting", max_waiting=20)
        await self.plugin._core._emotion_store.update(lambda config: config["queue"].update(max_waiting=1))
        result, _ = await self.run_plan(st=None)
        self.assertEqual(result["result"], "fallback")

    async def test_queue_slot_is_released_after_the_turn(self):
        await self.run_plan()
        self.assertFalse(self.plugin._core._queue.snapshot()["active"])
        await self.run_plan()  # a second turn is not blocked

    async def test_missing_sessions_are_skipped(self):
        empty = ScheduleTargets(Path(self.directory.name) / "none")
        self.plugin._core._schedule_targets = empty
        result, _ = await self.run_plan(st=None)
        self.assertEqual(result["result"], "skip:NoOwnerSession")
        result, _ = await self.run_plan(plan(target={"type": "group", "key": "deadbeef"}), st=None)
        self.assertEqual(result["result"], "skip:UnknownGroup")
        self.assertEqual(self.sends, [])

    async def test_platform_not_found_is_a_failure(self):
        async def not_found(umo, chain):
            return False

        self.plugin.context = SimpleNamespace(send_message=not_found)
        result, _ = await self.run_plan()
        self.assertEqual(result["result"], "failed:RuntimeError")

    async def test_log_line_has_all_fields_and_no_session_ids(self):
        await self.run_plan()
        (line,) = self.schedule_logs()
        self.assertRegex(
            line,
            r"^\[STBridge\] Schedule plan=0a1b2c3d name=早安 target=private result=sent voice=sent "
            r"sticker=sent st_ms=\d+ total_ms=\d+$",
        )
        group_plan = plan(target={"type": "group", "key": self.group_key})
        await self.run_plan(group_plan)
        self.assertIn(f"target=group:{self.group_key}", self.schedule_logs()[-1])
        for text in self.module.test_logger.infos + self.module.test_logger.warnings + self.module.test_logger.errors:
            self.assertNotIn("secret", text)
            self.assertNotIn("openid", text)

    async def test_text_is_never_prefixed_with_a_mention(self):
        await self.run_plan(plan(target={"type": "group", "key": self.group_key}))
        self.assertEqual(self.sends[1][2][0].text, "早安，制作人。")

    async def test_cancellation_propagates(self):
        task = asyncio.create_task(self.plugin._core._run_proactive(plan(), False))
        await _wait_for_request(self.plugin._core._request_state_manager)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TargetRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = _ready_plugin(self.module)
        self.plugin._core._emotion_store = ConfigStore(Path(self.directory.name))
        self.targets = ScheduleTargets(Path(self.directory.name), clock=lambda: NOW)
        self.plugin._core._schedule_targets = self.targets

    async def test_owner_private_message_records_the_owner_session(self):
        event = _bridge_event(self.module, "hi", components=[])
        event.unified_msg_origin = OWNER_UMO
        results = [item async for item in self.plugin.stbridge_private_message(event)]
        self.assertEqual(self.targets.conversation_for({"type": "private"}), OWNER_UMO)
        self.assertEqual(self.targets.last_activity(OWNER_UMO), NOW)
        self.assertEqual(len(results), 1)

    async def test_other_private_users_are_not_recorded(self):
        event = _bridge_event(self.module, "hi", sender_id="stranger", components=[])
        event.unified_msg_origin = "qq_official:FriendMessage:stranger"
        results = [item async for item in self.plugin.stbridge_private_message(event)]
        self.assertEqual(results, [])
        self.assertFalse(self.targets.has_owner)

    async def test_group_messages_record_the_group_even_without_an_at(self):
        event = _group_bridge_event(self.module, "hi")
        event.unified_msg_origin = GROUP_UMO
        event.is_at_or_wake_command = False
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            results = [item async for item in self.plugin.stbridge_group_message(event)]
        self.assertEqual(results, [])
        key = group_key_for(GROUP_UMO)
        self.assertEqual(self.targets.conversation_for({"type": "group", "key": key}), GROUP_UMO)
        self.assertEqual(self.targets.last_activity(GROUP_UMO), NOW)

    async def test_recording_failures_never_break_the_handler(self):
        class Broken:
            def record_owner(self, umo):
                raise OSError("disk full")

        self.plugin._core._schedule_targets = Broken()
        event = _bridge_event(self.module, "hi", components=[])
        event.unified_msg_origin = OWNER_UMO
        results = [item async for item in self.plugin.stbridge_private_message(event)]
        self.assertEqual(len(results), 1)

    async def test_events_without_an_origin_are_ignored(self):
        event = _bridge_event(self.module, "hi", components=[])
        [item async for item in self.plugin.stbridge_private_message(event)]
        self.assertFalse(self.targets.has_owner)


class SchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_starts_the_scheduler_and_terminate_stops_it(self):
        module = _load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            plugin = _ready_plugin(module)
            plugin._core._emotion_store = ConfigStore(Path(directory))
            plugin._core._setup_schedule()
            self.assertIsNotNone(plugin._core._scheduler)
            self.assertIsNotNone(plugin._core._schedule_targets)
            loop_task = plugin._core._scheduler._loop_task
            self.assertFalse(loop_task.done())
            await plugin._core._stop_schedule()
            self.assertTrue(loop_task.done())
            self.assertIsNone(plugin._core._scheduler)

    async def test_setup_failure_only_disables_scheduling(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        plugin._core._emotion_store = SimpleNamespace(data_dir=None, config={})
        plugin._core._setup_schedule()
        self.assertIsNone(plugin._core._scheduler)
        self.assertTrue(any("Schedule unavailable" in line for line in module.test_logger.errors))


if __name__ == "__main__":
    unittest.main()

"""L1b: several ☆‹日文› + "中文" groups in one reply (parsing, merged/split delivery, scheduled messages, settings)."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.bridge_types import (
    cap_pairs,
    extract_bilingual_reply,
    extract_reply_pairs,
    merge_pairs,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import config_presets
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    EmotionConfigError,
    default_config,
    default_tts_settings,
    validate_tts_settings,
)
from test_plugin_lifecycle import (
    RecordingTts,
    _bridge_event,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

TWO = '☆‹「一」›"甲"☆‹「二」›"乙"'


class ParseTests(unittest.TestCase):
    def pairs(self, text):
        result = extract_reply_pairs(text)
        return None if result is None else result

    def test_single_group_matches_the_old_parser(self):
        cases = (
            '[情绪:开心]☆‹「やった」›\n"太好了"',
            '☆‹「やった」›\n"太好了"[情绪:开心]',
            '☆‹「やった」›\n"太好了"',
            '"中文先出现" 与日语无换行☆‹「後です」›',
            '<game>背景</game>☆‹日本語本文›<X/>"中文，标点！😊"',
        )
        for text in cases:
            with self.subTest(text=text):
                pairs, tags = self.pairs(text)
                self.assertEqual(len(pairs), 1)
                old = extract_bilingual_reply(
                    __import__("re").sub(r"\[情绪[:：][^\]]*\]", "", text)
                )
                self.assertEqual(pairs[0][:2], old)

    def test_single_group_emotion_is_the_first_tag_wherever_it_sits(self):
        self.assertEqual(self.pairs('[情绪:开心]☆‹日›"中"')[0][0][2], "开心")
        self.assertEqual(self.pairs('☆‹日›"中"[情绪:生气]')[0][0][2], "生气")
        self.assertEqual(self.pairs('☆‹日[情绪:害羞]›"中"[情绪:生气]')[0][0][2], "害羞")
        self.assertIsNone(self.pairs('☆‹日›"中"')[0][0][2])
        pairs, tags = self.pairs('[情绪:开心]☆‹日›"中"[情绪:生气]')
        self.assertEqual(tags, ["开心", "生气"])

    def test_two_and_three_groups(self):
        pairs, _ = self.pairs(TWO)
        self.assertEqual([p[:2] for p in pairs], [("「一」", "甲"), ("「二」", "乙")])
        pairs, _ = self.pairs('☆‹a›"1"\n☆‹b›"2"\n乱写 ☆‹c›"3" 结尾')
        self.assertEqual([p[:2] for p in pairs], [("a", "1"), ("b", "2"), ("c", "3")])

    def test_each_group_takes_its_own_emotion(self):
        pairs, tags = self.pairs('[情绪:开心]☆‹a›"1"[情绪:生气]☆‹b›"2"[情绪:伤心]☆‹c›"3"')
        self.assertEqual([p[2] for p in pairs], ["开心", "生气", "伤心"])
        self.assertEqual(tags, ["开心", "生气", "伤心"])

    def test_a_group_without_a_tag_keeps_the_previous_emotion(self):
        pairs, _ = self.pairs('[情绪:开心]☆‹a›"1"☆‹b›"2"[情绪:生气]☆‹c›"3"')
        self.assertEqual([p[2] for p in pairs], ["开心", "开心", "生气"])

    def test_the_last_tag_before_a_group_wins(self):
        pairs, _ = self.pairs('☆‹a›"1"[情绪:开心][情绪:生气]☆‹b›"2"')
        self.assertEqual([p[2] for p in pairs], ["开心", "生气"])  # group 1: first tag overall

    def test_first_group_without_a_leading_tag_uses_the_first_tag_overall(self):
        pairs, _ = self.pairs('☆‹a›"1"[情绪:生气]☆‹b›"2"')
        self.assertEqual([p[2] for p in pairs], ["生气", "生气"])

    def test_tag_between_the_groups_text_counts_for_the_next_group(self):
        pairs, _ = self.pairs('[情绪:开心]☆‹a›"1" 然后 [情绪:伤心] 再说☆‹b›"2"')
        self.assertEqual([p[2] for p in pairs], ["开心", "伤心"])

    def test_invalid_replies(self):
        cases = (
            "no markers",
            '☆‹a›"1"☆‹b›',                # a group without Chinese
            '☆‹a›"1" "2"☆‹b›"3"',          # two quote pairs between two ☆‹
            '"1"☆‹a›"2"☆‹b›"3"',           # Chinese first in a multi-group reply
            '☆‹a›"1"☆‹b›"2" "3"',          # an extra pair
            '☆‹a›"1"☆‹b›"2',               # unbalanced quote
            '☆‹a›"1"☆‹b "2"',              # missing ›
            '☆‹a›"1"☆‹   ›"2"',            # empty Japanese
            '☆‹a›"1"☆‹b›"  "',             # empty Chinese
            '☆‹a☆‹b››"1""2"',              # nested
            '☆‹a"x"b›"1"',                 # a quote inside the Japanese
            '',
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(extract_reply_pairs(text))
        self.assertIsNone(extract_reply_pairs(None))

    def test_merge_adds_a_full_stop_only_when_needed(self):
        merged = merge_pairs([("おはよう", "早", "开心"), ("元気？", "好吗", "生气"), ("うん", "嗯", None)])
        self.assertEqual(merged, ("おはよう。元気？うん", "早\n好吗\n嗯", "开心"))
        merged = merge_pairs([("あ」", "a", None), ("い…", "b", None), ("う", "c", None)])
        self.assertEqual(merged[0], "あ」い…う")
        self.assertEqual(merged[1], "a\nb\nc")

    def test_cap_merges_the_tail_into_the_last_group(self):
        pairs = [("a", "1", "x"), ("b", "2", "y"), ("c", "3", "z"), ("d", "4", None)]
        self.assertEqual(cap_pairs(pairs, 10), pairs)
        self.assertEqual(cap_pairs(pairs, 4), pairs)
        self.assertEqual(cap_pairs(pairs, 3), [pairs[0], pairs[1], ("c。d", "3\n4", "z")])
        self.assertEqual(cap_pairs(pairs, 1), [("a。b。c。d", "1\n2\n3\n4", "x")])
        self.assertEqual(cap_pairs(pairs, 0), cap_pairs(pairs, 1))


class DeliveryBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.plugin = _ready_plugin(self.module, voice=True)
        self.core = self.plugin._core
        self.tts = self.core._tts
        self.log = []
        self.pauses = []
        self.core._tts_session_state.clear() if hasattr(self.core, "_tts_session_state") else None
        original = self.tts.synthesize

        async def synthesize(text, profile):
            self.log.append(f"synth:{text}")
            return await original(text, profile)

        self.tts.synthesize = synthesize
        self.profile_requests = []

        def select(tags):
            self.profile_requests.append(list(tags))
            return {"emotion": tags[0] if tags else None}

        self.core._select_tts_profile = select

        async def pause(seconds):
            self.pauses.append(seconds)

        self.core._split_pause = pause

        async def prepare(msg, tags):
            self.log.append(f"sticker-prepare:{tags}")
            return SimpleNamespace(tags=list(tags))

        async def finish(msg, plan):
            self.log.append("sticker-sent")

        self.core._prepare_sticker = prepare
        self.core._finish_sticker = finish

    def set_split(self, on=True, interval=1.5, limit=3, **common):
        self.tts.common.update(split_messages=on, split_interval=interval, split_max=limit, **common)

    async def reply(self, text, *, on_item=None, sender="qq-user"):
        event = _bridge_event(self.module, "hi", sender_id=sender)
        items = []

        async def consume():
            async for item in self.plugin.stbridge_private_message(event):
                self.log.append(f"item{len(items)}")
                items.append(item)
                if on_item is not None:
                    await on_item(len(items) - 1)
                self.log.append(f"sent{len(items) - 1}")

        task = asyncio.create_task(consume())
        manager = self.core._request_state_manager
        request = await _wait_for_request(manager)
        self.attempts = getattr(self, "attempts", 0) + 1
        manager.claim_request(request.request_id, f"attempt-{self.attempts}")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, text)
        await task
        return items

    def has_voice(self, item):
        return any(isinstance(part, self.module.TestRecord) for part in getattr(item, "chain", []))


class MergedDeliveryTests(DeliveryBase):
    async def test_merged_into_one_message_with_joined_text_and_voice(self):
        items = await self.reply('[情绪:开心]☆‹おはよう›"早"[情绪:生气]☆‹元気？›"好吗"')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "早\n好吗")
        self.assertEqual(self.tts.calls[0][0], "おはよう。元気？")
        self.assertEqual(len(self.tts.calls), 1)
        self.assertEqual(self.profile_requests, [["开心", "生气"]])  # the first tag, as before
        self.assertEqual(self.pauses, [])

    async def test_merged_text_limit_applies_to_the_joined_japanese(self):
        self.set_split(False, text_limit=5)
        await self.reply(TWO)
        self.assertEqual(self.tts.calls[0][0], "「一」「二")

    async def test_split_off_is_the_default_without_the_new_keys(self):
        for key in ("split_messages", "split_interval", "split_max"):
            self.tts.common.pop(key, None)
        items = await self.reply(TWO)
        self.assertEqual(len(items), 1)

    async def test_single_group_is_identical_in_split_mode(self):
        self.set_split(True)
        items = await self.reply('[情绪:开心]☆‹「一」›"甲"')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "甲")
        self.assertEqual(self.profile_requests, [["开心"]])
        self.assertEqual(self.pauses, [])
        self.assertIn("sticker-prepare:['开心']", self.log)

    async def test_pairs_logged_for_multi_group_replies(self):
        self.module.test_logger.infos.clear()
        await self.reply(TWO)
        self.assertIn("[STBridge] Reply pairs=2 mode=merged", self.module.test_logger.infos)
        self.module.test_logger.infos.clear()
        self.set_split(True)
        await self.reply(TWO)
        self.assertTrue(any(line.startswith("[STBridge] Reply pairs=2 mode=split") for line in self.module.test_logger.infos))


class SplitDeliveryTests(DeliveryBase):
    async def test_one_message_per_group_each_with_a_voice_in_order(self):
        self.set_split(True, interval=2.0)
        items = await self.reply('[情绪:开心]☆‹一›"甲"[情绪:生气]☆‹二›"乙"[情绪:伤心]☆‹三›"丙"')
        self.assertEqual([item.text for item in items], ["甲", "乙", "丙"])
        self.assertTrue(all(self.has_voice(item) for item in items))
        self.assertEqual([call[0] for call in self.tts.calls], ["一", "二", "三"])
        self.assertEqual([call[1]["emotion"] for call in self.tts.calls], ["开心", "生气", "伤心"])
        self.assertEqual(self.pauses, [2.0, 2.0])

    async def test_mention_only_on_the_first_group(self):
        self.set_split(True)
        self.core._process_mention = None
        # drive _deliver_groups directly: the mention is the queue's business, here it is just a prefix
        msg = SimpleNamespace(session_id="s")
        batches = [b async for b in self.core._deliver_groups(msg, [("一", "甲", None), ("二", "乙", None)], "@小明 ", True, 0)]
        texts = ["".join(getattr(p, "text", "") or "" for p in b.segments) for b in batches]
        self.assertEqual(texts, ["@小明 甲", "乙"])
        self.assertEqual([call[0] for call in self.tts.calls], ["一", "二"])  # the voice text never gets the prefix

    async def test_send_text_with_audio_off_sends_only_voices(self):
        self.set_split(True, send_text_with_audio=False)
        items = await self.reply(TWO)
        self.assertEqual([item.text for item in items], ["", ""])
        self.assertTrue(all(self.has_voice(item) for item in items))

    async def test_group_without_a_tag_keeps_the_previous_emotion_for_its_voice(self):
        self.set_split(True)
        await self.reply('[情绪:开心]☆‹一›"甲"☆‹二›"乙"')
        self.assertEqual(self.profile_requests, [["开心"], ["开心"]])

    async def test_probability_zero_sends_text_only_for_every_group(self):
        self.set_split(True, prob=0)
        items = await self.reply(TWO)
        self.assertEqual([item.text for item in items], ["甲", "乙"])
        self.assertFalse(any(self.has_voice(item) for item in items))
        self.assertEqual(self.tts.calls, [])
        self.assertEqual(self.pauses, [1.5])

    async def test_cooldown_is_judged_once_per_reply(self):
        self.set_split(True, cooldown=600)
        first = await self.reply(TWO)
        self.assertTrue(all(self.has_voice(item) for item in first))
        self.assertEqual(len(self.tts.calls), 2)  # two voices, one cooldown stamp
        second = await self.reply(TWO)
        self.assertFalse(any(self.has_voice(item) for item in second))
        self.assertEqual(len(self.tts.calls), 2)

    async def test_probability_rolled_once_per_reply(self):
        self.set_split(True, prob=50)
        rolls = []
        real = __import__("random").random

        def roll():
            rolls.append(1)
            return 0.1

        from unittest.mock import patch
        with patch("data.plugins.astrbot_plugin_st_bridge.st_bridge_core.mixin_tts.random.random", roll):
            items = await self.reply('☆‹a›"1"☆‹b›"2"☆‹c›"3"')
        self.assertEqual(len(rolls), 1)
        self.assertTrue(all(self.has_voice(item) for item in items))

    async def test_a_failed_synthesis_only_costs_that_group_its_voice(self):
        self.set_split(True, send_text_with_audio=False)
        original = self.tts.synthesize

        async def flaky(text, profile):
            if text == "二":
                raise RuntimeError("boom")
            return await original(text, profile)

        self.tts.synthesize = flaky
        items = await self.reply('☆‹一›"甲"☆‹二›"乙"☆‹三›"丙"')
        self.assertEqual([item.text for item in items], ["", "乙", ""])  # a failed group still sends its text
        self.assertEqual([self.has_voice(item) for item in items], [True, False, True])

    async def test_over_the_limit_the_rest_merges_into_the_last_group(self):
        self.set_split(True, limit=2)
        items = await self.reply('[情绪:开心]☆‹a›"1"[情绪:生气]☆‹b›"2"[情绪:伤心]☆‹c›"3"')
        self.assertEqual([item.text for item in items], ["1", "2\n3"])
        self.assertEqual([call[0] for call in self.tts.calls], ["a", "b。c"])
        self.assertEqual([call[1]["emotion"] for call in self.tts.calls], ["开心", "生气"])

    async def test_limit_one_behaves_like_merged(self):
        self.set_split(True, limit=1)
        items = await self.reply(TWO)
        self.assertEqual([item.text for item in items], ["甲\n乙"])
        self.assertEqual(self.pauses, [])

    async def test_sticker_follows_the_last_group_and_goes_last(self):
        self.set_split(True)
        await self.reply('[情绪:开心]☆‹a›"1"[情绪:生气]☆‹b›"2"')
        self.assertIn("sticker-prepare:['生气']", self.log)
        self.assertEqual(self.log[-1], "sticker-sent")
        self.assertGreater(self.log.index("sticker-sent"), self.log.index("sent1"))
        self.assertLess(self.log.index("sticker-prepare:['生气']"), self.log.index("synth:a"))

    async def test_the_next_voice_is_synthesized_while_the_previous_group_is_sent(self):
        self.set_split(True)

        async def slow_send(index):
            await asyncio.sleep(0.02)

        await self.reply('☆‹a›"1"☆‹b›"2"☆‹c›"3"', on_item=slow_send)
        self.assertLess(self.log.index("synth:b"), self.log.index("sent0"))
        self.assertLess(self.log.index("synth:c"), self.log.index("sent1"))
        self.assertLess(self.log.index("item0"), self.log.index("item1"))

    async def test_syntheses_never_overlap_and_the_next_starts_before_the_previous_yield(self):
        self.set_split(True)
        running, overlaps, events = [], [], []
        original = self.tts.synthesize

        async def tracked(text, profile):
            running.append(text)
            events.append(f"start:{text}")
            if len(running) > 1:
                overlaps.append(list(running))
            await asyncio.sleep(0.02)
            result = await original(text, profile)
            running.remove(text)
            events.append(f"end:{text}")
            return result

        self.tts.synthesize = tracked

        async def on_item(index):
            events.append(f"yield:{index}")
            await asyncio.sleep(0.05)

        items = await self.reply('☆‹a›"1"☆‹b›"2"☆‹c›"3"', on_item=on_item)
        self.assertEqual(len(items), 3)
        self.assertEqual(overlaps, [])
        self.assertLess(events.index("end:a"), events.index("start:b"))
        self.assertLess(events.index("end:b"), events.index("start:c"))
        self.assertLess(events.index("start:b"), events.index("yield:0"))
        self.assertLess(events.index("start:c"), events.index("yield:1"))

    async def test_interval_waits_for_the_pending_synthesis_too(self):
        self.set_split(True, interval=0.0)
        release = asyncio.Event()
        original = self.tts.synthesize

        async def gated(text, profile):
            if text == "b":
                await release.wait()
            return await original(text, profile)

        self.tts.synthesize = gated

        async def on_item(index):
            if index == 0:
                asyncio.get_running_loop().call_later(0.05, release.set)

        items = await self.reply('☆‹a›"1"☆‹b›"2"', on_item=on_item)
        self.assertTrue(release.is_set())
        self.assertEqual(len(items), 2)
        self.assertTrue(self.has_voice(items[1]))

    async def test_a_closed_delivery_cancels_the_voice_it_started_ahead(self):
        self.set_split(True)
        started = asyncio.Event()
        cancelled = []
        original = self.tts.synthesize

        async def hanging(text, profile):
            if text == "b":
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.append(text)
                    raise
            return await original(text, profile)

        self.tts.synthesize = hanging
        msg = SimpleNamespace(session_id="s")
        generator = self.core._deliver_groups(msg, [("a", "1", None), ("b", "2", None)], "", True, 0)
        first = await generator.__anext__()
        self.assertEqual(first.segments[0].text if hasattr(first.segments[0], "text") else "", "1")
        await started.wait()
        await generator.aclose()
        await asyncio.sleep(0)
        self.assertEqual(cancelled, ["b"])


class ParseReplyTests(DeliveryBase):
    async def test_game_block_mvu_and_sing_tag_with_several_groups(self):
        text = "\n".join([
            '想想 ☆‹仮›"仮"',
            '<game>',
            '[情绪:开心]☆‹a[唱歌:晴天]›"1"',
            "<UpdateVariable><Analysis>网友说了\"很可爱\"</Analysis>",
            "_.add('藤田琴音.好感度', 3);//开心",
            "</UpdateVariable>",
            '[情绪:生气]☆‹b›"2[唱歌:晴天]"',
            '</game>尾巴 "x"',
        ])
        parsed = self.core._parse_reply(text, self.module.OWNER_TRACK)
        self.assertIsNotNone(parsed)
        self.assertEqual([pair[:2] for pair in parsed.pairs], [("a", "1"), ("b", "2")])
        self.assertEqual([pair[2] for pair in parsed.pairs], ["开心", "生气"])
        self.assertEqual((parsed.japanese_text, parsed.chinese_text), ("a。b", "1\n2"))
        self.assertEqual(parsed.emotion_tags, ["开心", "生气"])
        self.assertEqual(parsed.sing_name, "晴天")
        self.assertIsNotNone(parsed.affinity_update)

    async def test_invalid_multi_group_reply_still_gets_the_invalid_notice(self):
        items = await self.reply('☆‹a›"1"☆‹b›')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, self.core._reply_text("invalid_reply"))


class SplitSongOrderTests(DeliveryBase):
    async def test_the_song_tag_is_handled_after_every_group_and_the_sticker(self):
        self.set_split(True)
        core = self.core
        core._tag_song_request = lambda msg, request, name: ("preset", name)
        core._start_song_tag_search = lambda speaker, name: object() if name else None

        async def finish(msg, speaker, name, search, preset):
            self.log.append(f"song:{name}")

        core._finish_song_tag = finish
        await self.reply('[情绪:开心]☆‹a›"1"[唱歌:晴天]☆‹b›"2"')
        self.assertEqual(self.log[-1], "song:晴天")
        self.assertLess(self.log.index("sticker-sent"), self.log.index("song:晴天"))
        self.assertLess(self.log.index("sent1"), self.log.index("sticker-sent"))


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        common = default_tts_settings()["common"]
        self.assertEqual((common["split_messages"], common["split_interval"], common["split_max"]), (True, 1.5, 3))

    def merged(self, **common):
        settings = default_tts_settings()
        settings["common"].update(common)
        return settings

    def test_old_config_without_the_keys_gets_the_defaults(self):
        settings = default_tts_settings()
        for key in ("split_messages", "split_interval", "split_max"):
            del settings["common"][key]
        common = validate_tts_settings(settings)["common"]
        self.assertEqual((common["split_messages"], common["split_interval"], common["split_max"]), (True, 1.5, 3))

    def test_ranges(self):
        for field, value in (
            ("split_messages", "yes"), ("split_messages", 1), ("split_interval", -0.1), ("split_interval", 10.1),
            ("split_interval", True), ("split_interval", "x"), ("split_max", 0), ("split_max", 11), ("split_max", True),
            ("split_max", "x"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(EmotionConfigError):
                    validate_tts_settings(self.merged(**{field: value}))
        for field, value in (("split_interval", 0), ("split_interval", 10), ("split_max", 1), ("split_max", 10)):
            validate_tts_settings(self.merged(**{field: value}))

    def test_interval_is_normalised_to_one_decimal(self):
        common = validate_tts_settings(self.merged(split_interval=1.26, split_messages=True, split_max="4"))["common"]
        self.assertEqual((common["split_interval"], common["split_messages"], common["split_max"]), (1.3, True, 4))

    def test_presets_carry_the_new_fields(self):
        config = default_config()
        config["tts"]["common"].update(split_messages=True, split_interval=2.5, split_max=5)
        shot = config_presets.snapshot(config)
        self.assertEqual(
            (shot["tts"]["common"]["split_messages"], shot["tts"]["common"]["split_interval"], shot["tts"]["common"]["split_max"]),
            (True, 2.5, 5),
        )
        restored = config_presets.normalize_config(shot, default_config())
        self.assertEqual(restored["tts"]["common"]["split_max"], 5)
        self.assertIs(restored["tts"]["common"]["split_messages"], True)
        # an old preset without the keys applies with the defaults
        for key in ("split_messages", "split_interval", "split_max"):
            del shot["tts"]["common"][key]
        old = config_presets.normalize_config(shot, default_config())
        self.assertEqual(old["tts"]["common"]["split_max"], 3)
        self.assertEqual(old["tts"]["common"]["split_interval"], 1.5)


if __name__ == "__main__":
    unittest.main()

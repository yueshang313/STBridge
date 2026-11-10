"""P7c: chat song requests in the plugin (permission variable, tag / command / link triggers, pick flow, delivery)."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore, group_key, user_hash, user_key
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.cover_service import CoverError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.music_source import MusicError
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import validate_affinity_writes
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import default_song_request_settings
from test_plugin_lifecycle import submodule
from test_plugin_lifecycle import (
    RecordingTts,
    _bridge_event,
    _collect,
    _complete_bridge_reply,
    _group_bridge_event,
    _load_plugin_module,
    _ready_plugin,
    _wait_for_request,
)

OWNER_UMO = "qq_official:FriendMessage:owner-openid"
GROUP_UMO = "qq_official:GroupMessage:group-openid"
OWNER_KEY = user_key("qq-platform", "qq-user")
MEMBER_KEY = group_key(user_key("qq-platform", "member-a"))
OTHER_KEY = group_key(user_key("qq-platform", "member-b"))
PERMISSION = {"path": ["点歌许可"]}
LIB = {"kind": "library", "song_id": "s1", "name": "晴天", "artist": "周杰伦", "album": "", "label": "歌库"}
WY = {"kind": "online", "source": "wy", "source_id": "100", "name": "晴天", "artist": "周杰伦", "album": "叶惠美",
      "label": "网易云"}
KG = {"kind": "online", "source": "kg", "source_id": "k200", "name": "晴天", "artist": "周杰伦", "album": "",
      "label": "酷狗"}
ACCEPT = default_song_request_settings()["accept_texts"][0]
REFUSE = default_song_request_settings()["refuse_texts"][0]
FAIL = default_song_request_settings()["fail_texts"][0]
TAG_REPLY = '[情绪:开心][唱歌:晴天]☆‹いいよ›"好呀"'


def online(source, song_id, name="晴天", artist="周杰伦", album=""):
    return {"source": source, "id": song_id, "name": name, "artist": artist, "album": album}


class FakeCover:
    def __init__(self, directory):
        self.calls = []
        self.sing_args = []
        self.songs = {"s1": {"id": "s1", "title": "晴天", "artist": "周杰伦"}}
        self.matches = []
        self.results = {"wy": [online("wy", "100", album="叶惠美")], "kg": [online("kg", "k200")]}
        self.search_error = None
        self.resolve_error = None
        self.sing_error = None
        self.gate = None
        # A started delivery waits here until the test lets it go (finish_delivery): the handler's reply now
        # reaches the test a few loop turns later than before, so without it a quick delivery would already be over.
        self.hold = asyncio.Event()
        self.path = Path(directory) / "cover.wav"
        self.path.write_bytes(b"RIFF")
        self.library = SimpleNamespace(get=lambda song_id: self.songs.get(song_id))

    def library_matches(self, name, artist=None):
        self.calls.append(("matches", name, artist))
        return list(self.matches)

    async def search_online(self, name, source):
        self.calls.append(("search", name, source))
        if self.search_error is not None:
            raise self.search_error
        return {"results": list(self.results.get(source, [])), "errors": []}

    async def resolve_online_song(self, source, source_id, name, artist=""):
        await self.hold.wait()
        self.calls.append(("resolve", source, source_id, name, artist))
        if self.resolve_error is not None:
            raise self.resolve_error
        return {"id": f"{source}-{source_id}", "title": name}

    async def sing(self, song_id, *, f0_up_key=None, inst_pitch=None):
        await self.hold.wait()
        self.calls.append(("sing", song_id))
        self.sing_args.append((f0_up_key, inst_pitch))
        if self.gate is not None:
            await self.gate.wait()
        if self.sing_error is not None:
            raise self.sing_error
        return {"cached": False, "cover": {"key": "abc123"}}

    def cover_file(self, sung):
        return self.path


class SongFlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        data = Path(self.directory.name)
        self.plugin = _ready_plugin(self.module)
        self.store = ConfigStore(data)
        self.plugin._core._emotion_store = self.store
        self.plugin._core._affinity_store = AffinityStore(data, owner_key=OWNER_KEY, group_initial_value=0)
        self.plugin._core._affinity_store.set_value(OWNER_KEY, 50)
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 30, low=-50, high=100)
        self.cover = FakeCover(data)
        self.plugin._core._cover = self.cover
        self.plugin._core._song_choice = lambda items: items[0]
        self.plugin._core._song_random = lambda: 0.99
        # The reply now reaches the test after a few more loop turns, so a quick delivery may already be over:
        # remember each started delivery task to be able to wait for it.
        self.delivery_tasks = {}
        start_delivery = self.plugin._core._start_song_delivery

        def tracked_start(umo, key, *args, **kwargs):
            start_delivery(umo, key, *args, **kwargs)
            self.delivery_tasks[key] = self.plugin._core._song_tasks.get(key)

        self.plugin._core._start_song_delivery = tracked_start
        self.now = 1000.0
        self.plugin._core._song_clock = lambda: self.now
        self.sends = []
        self.send_error = None

        async def send_message(umo, chain):
            if self.send_error is not None:
                raise self.send_error
            self.sends.append((umo, chain.chain))
            return True

        self.plugin.context = SimpleNamespace(send_message=send_message)
        self.module.test_logger.infos.clear()
        self.module.test_logger.warnings.clear()
        self.event_send_error = None
        self.last_event = None
        patcher = patch.object(submodule(self.module, "mixin_song"), "SONG_PASSIVE_SETTLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The P7c behaviour (context.send_message) is what most tests here cover; PassiveDeliveryTests switch.
        await self.set_song_settings(delivery_mode=self.delivery_mode)

    delivery_mode = "proactive"

    async def set_song_settings(self, **changes):
        await self.store.update(lambda config: config["song_request"].update(changes))

    async def set_texts(self, **changes):
        await self.store.update(lambda config: config["texts"].update(changes))

    def make_event(self, text, *, group=False, sender="member-a", name="小明"):
        if group:
            event = _group_bridge_event(self.module, text, sender_id=sender, sender_name=name)
        else:
            event = _bridge_event(self.module, text)
        event.unified_msg_origin = GROUP_UMO if group else OWNER_UMO
        event.sent = []
        event.send_error = None
        self.last_event = event

        async def send(chain):
            if event.send_error is not None:
                raise event.send_error
            if self.event_send_error is not None:
                raise self.event_send_error
            event.sent.append(chain)

        event.send = send
        return event

    async def run_handler(self, handler, event, **kwargs):
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            return await _collect(handler, event)

    async def say(self, text, *, group=False, sender="member-a"):
        """A user message through the real handler; the reply texts (no ST request may be needed)."""
        event = self.make_event(text, group=group, sender=sender)
        handler = self.plugin.stbridge_group_message if group else self.plugin.stbridge_private_message
        task = asyncio.create_task(self.run_handler(handler, event))
        for _ in range(3000):
            await asyncio.sleep(0)
            if task.done():
                break
        if not task.done():
            # It went to ST: fail the request so the handler ends, and report that.
            request = self.plugin._core._request_state_manager.peek_pending_request()
            self.assertIsNotNone(request, "handler neither answered nor created an ST request")
            self.plugin._core._request_state_manager.fail_request(request.request_id, "test_cleanup")
            await task
            return None
        return [item.text for item in task.result()]

    def seed(self, entries=None, *, group=False, query="晴天", via="tag"):
        entries = [LIB, WY, KG] if entries is None else entries
        self.plugin._core._song_register(
            GROUP_UMO if group else OWNER_UMO, MEMBER_KEY if group else OWNER_KEY, query, entries, group, via
        )

    async def finish_delivery(self, key):
        task = self.delivery_tasks.get(key)
        self.assertIsNotNone(task, "no delivery task was started")
        self.cover.hold.set()
        await task
        self.cover.hold.clear()

    def song_logs(self):
        return [line for line in self.module.test_logger.infos if "SongRequest" in line]


class PermissionWriteTests(SongFlowBase):
    async def peek_writes(self, event, handler=None):
        task = asyncio.create_task(_collect(handler or self.plugin.stbridge_private_message, event))
        request = await _wait_for_request(self.plugin._core._request_state_manager)
        writes = request.affinity_writes
        self.plugin._core._request_state_manager.fail_request(request.request_id, "generation_failed")
        await task
        return writes

    async def test_private_turn_carries_the_permission_as_the_third_write(self):
        writes = await self.peek_writes(self.make_event("你好"))
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[2], {"path": ["点歌许可"], "value": 1})
        validate_affinity_writes(list(writes))

    async def test_group_turn_carries_the_permission_too(self):
        event = self.make_event("你好", group=True)
        with patch.dict(sys.modules, self.module.TestBotpyModules):
            writes = await self.peek_writes(event, self.plugin.stbridge_group_message)
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[2], {"path": ["点歌许可"], "value": 1})
        validate_affinity_writes(list(writes))

    async def test_value_is_zero_when_the_feature_is_off_or_unavailable(self):
        speaker = self.plugin._core._speaker("qq-platform", "qq-user", "n")
        self.assertEqual(speaker.writes[2]["value"], 1)
        await self.set_song_settings(enabled=False)
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 0)
        await self.set_song_settings(enabled=True)
        await self.store.update(lambda config: config["cover"].update(enabled=False))
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 0)
        await self.store.update(lambda config: config["cover"].update(enabled=True))
        self.plugin._core._cover = None
        speaker = self.plugin._core._speaker("qq-platform", "qq-user", "n")
        self.assertEqual(speaker.writes[2]["value"], 0)
        self.assertIs(speaker.sing_allowed, False)

    async def test_group_threshold_boundary(self):
        for value, expected in ((20, 0), (21, 1), (-50, 0), (100, 1)):
            with self.subTest(value=value):
                self.plugin._core._affinity_store.set_value(MEMBER_KEY, value, low=-50, high=100)
                speaker = self.plugin._core._speaker("qq-platform", "member-a", "n", group=True)
                self.assertEqual(speaker.writes[2], {"path": ["点歌许可"], "value": expected})
                self.assertEqual(speaker.sing_allowed, bool(expected))

    async def test_owner_threshold_is_its_own_setting(self):
        await self.set_song_settings(owner_min_affinity=51)
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 0)
        await self.set_song_settings(owner_min_affinity=50)
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 1)
        # the group threshold does not apply to the owner and vice versa
        await self.set_song_settings(group_min_affinity=100)
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 1)

    async def test_refuse_percent_rolls_once_per_turn_and_hundred_always_refuses(self):
        rolls = []
        self.plugin._core._song_random = lambda: rolls.append(1) or 0.5
        await self.set_song_settings(refuse_percent=51)
        speaker = self.plugin._core._speaker("qq-platform", "qq-user", "n")
        self.assertEqual(speaker.writes[2]["value"], 0)
        self.assertEqual(len(rolls), 1)
        await self.set_song_settings(refuse_percent=50)
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 1)
        await self.set_song_settings(refuse_percent=100)
        self.plugin._core._song_random = lambda: 0.0
        self.assertEqual(self.plugin._core._speaker("qq-platform", "qq-user", "n").writes[2]["value"], 0)

    async def test_no_roll_when_the_threshold_already_fails(self):
        rolls = []
        self.plugin._core._song_random = lambda: rolls.append(1) or 0.99
        await self.set_song_settings(refuse_percent=10, owner_min_affinity=90)
        self.plugin._core._speaker("qq-platform", "qq-user", "n")
        self.assertEqual(rolls, [])

    async def test_affinity_command_does_not_roll_or_add_the_write(self):
        rolls = []
        self.plugin._core._song_random = lambda: rolls.append(1) or 0.5
        speaker = self.plugin._core._message_speaker(self.plugin._platform.incoming(self.make_event("/好感度")), song_permission=False)
        self.assertEqual(len(speaker.writes), 2)
        self.assertIsNone(speaker.sing_allowed)
        self.assertEqual(rolls, [])
        result, = await _collect(self.plugin.stbridge_affinity, self.make_event("/好感度"))
        self.assertIn("好感度", result.text)
        self.assertEqual(rolls, [])

    async def test_without_an_affinity_store_the_owner_may_and_members_may_not(self):
        self.plugin._core._affinity_store = None
        owner = self.plugin._core._speaker("qq-platform", "qq-user", "n")
        member = self.plugin._core._speaker("qq-platform", "member-a", "n", group=True)
        self.assertIsNone(owner.writes)
        self.assertTrue(owner.sing_allowed)
        self.assertFalse(member.sing_allowed)


class TagPathTests(SongFlowBase):
    async def run_tag_turn(self, event, reply, timeline=None, handler=None):
        timeline = timeline if timeline is not None else []
        original_send = event.send

        async def send(chain):
            timeline.append(("send", chain.chain[0].text))
            await original_send(chain)

        event.send = send

        async def consume():
            results = []
            with patch.dict(sys.modules, self.module.TestBotpyModules):
                async for item in (handler or self.plugin.stbridge_private_message)(event):
                    timeline.append(("yield", item.text))
                    results.append(item.text)
            return results

        task = asyncio.create_task(consume())
        request = await _wait_for_request(self.plugin._core._request_state_manager)
        manager = self.plugin._core._request_state_manager
        manager.claim_request(request.request_id, "attempt")
        manager.mark_generation_started(request.request_id)
        manager.confirm_generation_finished(request.request_id)
        manager.complete_request(request.request_id, reply)
        return await task

    async def test_the_list_follows_the_reply_and_is_not_voiced(self):
        self.plugin._core._tts = RecordingTts()
        event = self.make_event("唱首歌吧")
        timeline = []
        results = await self.run_tag_turn(event, TAG_REPLY, timeline)
        self.assertEqual(results, ["好呀"])
        # the tag is gone from both the Chinese text and the Japanese text handed to TTS
        self.assertEqual(self.plugin._core._tts.last_text, "いいよ")
        self.assertEqual([kind for kind, _ in timeline], ["yield", "send"])
        listing = event.sent[0].chain[0].text
        self.assertTrue(listing.startswith("《晴天》找到这些版本："))
        self.assertIn("[网易云]", listing)
        self.assertIn("[酷狗]", listing)
        self.assertNotIn("群里请", listing)
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual((pending.query, pending.via, pending.group), ("晴天", "tag", False))
        self.assertEqual([entry["label"] for entry in pending.entries], ["网易云", "酷狗"])
        self.assertEqual(pending.expires, 1000.0 + 180)
        self.assertIn(("search", "晴天", "wy"), self.cover.calls)
        self.assertIn(("search", "晴天", "kg"), self.cover.calls)

    async def test_group_list_has_the_at_hint_and_a_group_keyed_pending(self):
        event = self.make_event("唱首歌吧", group=True)
        results = await self.run_tag_turn(event, TAG_REPLY, handler=self.plugin.stbridge_group_message)
        self.assertEqual(len(results), 1)
        self.assertIn("群里请 @我 再回复哦。", event.sent[0].chain[0].text)
        self.assertTrue(self.plugin._core._song_pending[(GROUP_UMO, MEMBER_KEY)].group)

    async def test_not_allowed_ignores_the_tag_but_still_strips_it(self):
        self.plugin._core._tts = RecordingTts()
        await self.set_song_settings(owner_min_affinity=100)
        event = self.make_event("唱首歌吧")
        results = await self.run_tag_turn(event, TAG_REPLY)
        self.assertEqual(results, ["好呀"])
        self.assertEqual(self.plugin._core._tts.last_text, "いいよ")
        self.assertEqual(event.sent, [])
        self.assertEqual(self.cover.calls, [])
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertTrue(any("via=tag" in line and "skip:NotAllowed" in line for line in self.song_logs()))

    async def test_a_reply_without_a_tag_sends_no_list(self):
        event = self.make_event("你好")
        results = await self.run_tag_turn(event, '[情绪:开心]☆‹やあ›"你好"')
        self.assertEqual(results, ["你好"])
        self.assertEqual(event.sent, [])
        self.assertEqual(self.cover.calls, [])

    async def test_search_failure_does_not_touch_the_reply_and_sends_the_fail_line(self):
        self.cover.search_error = RuntimeError("boom")
        event = self.make_event("唱首歌吧")
        results = await self.run_tag_turn(event, TAG_REPLY)
        self.assertEqual(results, ["好呀"])
        self.assertEqual([chain.chain[0].text for chain in event.sent], [FAIL])
        self.assertEqual(self.plugin._core._song_pending, {})

    async def test_nothing_found_sends_the_fail_line(self):
        self.cover.results = {"wy": [], "kg": []}
        event = self.make_event("唱首歌吧")
        await self.run_tag_turn(event, TAG_REPLY)
        self.assertEqual([chain.chain[0].text for chain in event.sent], [FAIL])

    async def test_a_failing_event_send_never_breaks_the_turn(self):
        event = self.make_event("唱首歌吧")
        event.send_error = RuntimeError("platform error")
        results = await self.run_tag_turn(event, TAG_REPLY)
        self.assertEqual(results, ["好呀"])
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertTrue(any("SongRequest list failed" in line for line in self.module.test_logger.warnings))

    async def test_a_slow_search_is_cut_off_after_the_timeout(self):
        async def never(name, source):
            await asyncio.Event().wait()

        self.cover.search_online = never
        with patch.object(submodule(self.module, "mixin_song"), "SONG_SEARCH_TIMEOUT_SECONDS", 0.05):
            event = self.make_event("唱首歌吧")
            results = await self.run_tag_turn(event, TAG_REPLY)
        self.assertEqual(results, ["好呀"])
        self.assertEqual([chain.chain[0].text for chain in event.sent], [FAIL])

    async def test_scheduled_replies_strip_the_tag_and_ignore_it(self):
        parsed = self.plugin._core._parse_reply(TAG_REPLY, self.module.OWNER_TRACK)
        self.assertEqual((parsed.japanese_text, parsed.chinese_text, parsed.sing_name), ("いいよ", "好呀", "晴天"))
        self.assertEqual(self.cover.calls, [])

    async def test_tag_inside_the_dialogue_leaves_both_texts(self):
        parsed = self.plugin._core._parse_reply('☆‹いいよ[唱歌:晴天]›"好呀[唱歌：晴天]"', self.module.OWNER_TRACK)
        self.assertEqual((parsed.japanese_text, parsed.chinese_text), ("いいよ", "好呀"))


class PickFlowTests(SongFlowBase):
    async def test_a_number_starts_the_delivery_without_asking_st(self):
        self.seed()
        replies = await self.say("2")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "晴天")])
        self.assertNotIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.calls, [("resolve", "wy", "100", "晴天", "周杰伦"), ("sing", "wy-100")])
        (umo, chain), = self.sends
        self.assertEqual(umo, OWNER_UMO)
        self.assertEqual(len(chain), 1)
        self.assertEqual(type(chain[0]).__name__, "FakeRecord")
        self.assertEqual(chain[0].file, str(self.cover.path))
        self.assertTrue(self.cover.path.exists())
        line, = self.song_logs()
        self.assertIn(f"user={user_hash(OWNER_KEY)} via=tag pick=wy result=sent mode=proactive total_ms=", line)

    async def test_picking_a_library_song_uses_it_directly(self):
        self.seed()
        await self.say("1")
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual([call[0] for call in self.cover.calls], ["sing"])
        self.assertEqual(self.cover.calls[0], ("sing", "s1"))
        self.assertIn("pick=lib", self.song_logs()[0])

    async def test_out_of_range_keeps_the_list(self):
        self.seed()
        replies = await self.say("9")
        self.assertEqual(replies, [self.module.SONG_PICK_RANGE_TEXT])
        self.assertIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)
        self.assertEqual(self.plugin._core._song_tasks, {})

    async def test_research_with_extra_info_sends_a_new_list_and_resets_the_timer(self):
        self.seed()
        self.now = 1100.0
        self.cover.results = {"wy": [online("wy", "300", artist="五月天")], "kg": []}
        replies = await self.say("0 五月天")
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("《晴天》找到这些版本："))
        self.assertIn("五月天", replies[0])
        self.assertIn(("matches", "晴天", "五月天"), self.cover.calls)
        self.assertIn(("search", "晴天 五月天", "wy"), self.cover.calls)
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual(pending.expires, 1100.0 + 180)
        self.assertEqual([entry["source_id"] for entry in pending.entries], ["300"])
        self.assertEqual(pending.via, "tag")

    async def test_research_without_results_keeps_the_old_list(self):
        self.seed()
        self.cover.results = {"wy": [], "kg": []}
        replies = await self.say("0，没人唱过")
        self.assertEqual(replies, [self.module.SONG_RESEARCH_EMPTY_TEXT])
        self.assertEqual(len(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].entries), 3)

    async def test_a_lone_zero_explains_how_to_add_info(self):
        self.seed()
        self.assertEqual(await self.say("0"), [self.module.SONG_RESEARCH_HINT])
        self.assertEqual(await self.say("０："), [self.module.SONG_RESEARCH_HINT])
        self.assertIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)

    async def test_cancel_ends_the_list(self):
        self.seed()
        self.assertEqual(await self.say("取消"), [self.module.SONG_CANCEL_TEXT])
        self.assertEqual(self.plugin._core._song_pending, {})

    async def test_after_the_timeout_a_number_is_ordinary_chat(self):
        self.seed()
        self.now = 1000.0 + 180
        self.assertIsNone(await self.say("2"))
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertEqual(self.plugin._core._song_tasks, {})

    async def test_other_text_goes_to_st_and_the_list_stays_valid(self):
        self.seed()
        self.assertIsNone(await self.say("你觉得哪个版本好？"))
        self.assertIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)
        self.assertEqual(await self.say("取消"), [self.module.SONG_CANCEL_TEXT])

    async def test_somebody_elses_number_is_ordinary_chat(self):
        self.seed(group=True)
        self.assertIsNone(await self.say("2", group=True, sender="member-b"))
        self.assertIn((GROUP_UMO, MEMBER_KEY), self.plugin._core._song_pending)
        self.assertEqual(self.plugin._core._song_tasks, {})
        replies = await self.say("2", group=True, sender="member-a")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "晴天")])

    async def test_two_requesters_keep_separate_lists(self):
        self.seed(group=True)
        self.plugin._core._song_register(GROUP_UMO, OTHER_KEY, "夜曲", [KG], True, "command")
        replies = await self.say("1", group=True, sender="member-b")
        self.assertEqual(len(replies), 1)
        self.assertIn((GROUP_UMO, MEMBER_KEY), self.plugin._core._song_pending)
        await self.finish_delivery(OTHER_KEY)
        self.assertEqual(self.cover.calls[0], ("resolve", "kg", "k200", "晴天", "周杰伦"))

    async def test_a_new_list_replaces_the_old_one(self):
        self.seed([WY])
        self.seed([KG, KG])
        self.assertEqual(len(self.plugin._core._song_pending), 1)
        self.assertEqual(len(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].entries), 2)

    async def test_a_pick_while_the_previous_song_is_still_being_made_is_told_to_wait(self):
        self.cover.gate = asyncio.Event()
        self.seed()
        await self.say("2")
        await asyncio.sleep(0)
        self.seed()
        self.assertEqual(await self.say("3"), [self.module.SONG_BUSY_TEXT])
        self.assertIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)
        self.cover.gate.set()
        await self.finish_delivery(OWNER_KEY)
        await asyncio.sleep(0)
        self.assertEqual(self.plugin._core._song_tasks, {})
        self.assertEqual(await self.say("3"), [ACCEPT.replace("{歌名}", "晴天")])
        await self.finish_delivery(OWNER_KEY)

    async def test_replies_do_not_go_through_the_queue_or_st(self):
        self.seed()
        await self.say("2")
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())
        self.assertEqual(self.plugin._core._queue.waiting_count, 0)
        await self.finish_delivery(OWNER_KEY)

    async def test_custom_accept_text_fills_the_song_name(self):
        await self.set_texts(accept_texts=["来了《{歌名}》"])
        entry = dict(WY, name="夜曲")
        self.seed([entry])
        self.assertEqual(await self.say("1"), ["来了《夜曲》"])
        await self.finish_delivery(OWNER_KEY)


class DeliveryFailureTests(SongFlowBase):
    async def pick_and_wait(self):
        self.seed()
        await self.say("2")
        await self.finish_delivery(OWNER_KEY)
        await asyncio.sleep(0)

    def texts(self):
        return [chain[0].text for _umo, chain in self.sends if type(chain[0]).__name__ == "FakePlain"]

    async def test_download_failures_send_the_fail_line(self):
        for error in (CoverError("preview_only"), CoverError("MusicKeyNotSet"), MusicError("timeout"),
                      RuntimeError("boom")):
            with self.subTest(error=repr(error)):
                self.sends.clear()
                self.module.test_logger.infos.clear()
                self.cover.resolve_error = error
                await self.pick_and_wait()
                self.assertEqual(self.texts(), [FAIL])
                kind = getattr(error, "kind", type(error).__name__)
                self.assertIn(f"result=failed:{kind}", self.song_logs()[0])
                self.assertEqual(self.plugin._core._song_tasks, {})

    async def test_sing_failure_sends_the_fail_line(self):
        self.cover.sing_error = CoverError("ServiceStartFailed")
        await self.pick_and_wait()
        self.assertEqual(self.texts(), [FAIL])

    async def test_queue_full_has_its_own_line(self):
        self.cover.sing_error = CoverError("QueueFull")
        await self.pick_and_wait()
        self.assertEqual(self.texts(), [self.module.SONG_QUEUE_FULL_TEXT])

    async def test_failure_of_the_failure_line_is_only_logged(self):
        self.cover.sing_error = CoverError("Timeout")
        self.send_error = RuntimeError("platform")
        await self.pick_and_wait()
        self.assertTrue(any("failure line not sent" in line for line in self.module.test_logger.warnings))

    async def test_a_failed_voice_send_sends_the_fail_line(self):
        calls = []

        async def send_message(umo, chain):
            calls.append(type(chain.chain[0]).__name__)
            if calls == ["FakeRecord"]:
                raise RuntimeError("voice rejected")
            self.sends.append((umo, chain.chain))
            return True

        self.plugin.context = SimpleNamespace(send_message=send_message)
        await self.pick_and_wait()
        self.assertEqual(calls, ["FakeRecord", "FakePlain"])
        self.assertEqual(self.texts(), [FAIL])

    async def test_unavailable_platform_counts_as_a_failure(self):
        async def send_message(umo, chain):
            self.sends.append((umo, chain.chain))
            return False if type(chain.chain[0]).__name__ == "FakeRecord" else True

        self.plugin.context = SimpleNamespace(send_message=send_message)
        await self.pick_and_wait()
        self.assertEqual(self.texts(), [FAIL])

    async def test_terminate_cancels_running_deliveries_and_drops_lists(self):
        self.cover.gate = asyncio.Event()
        self.seed()
        await self.say("2")
        await asyncio.sleep(0)
        task = self.plugin._core._song_tasks[OWNER_KEY]
        self.seed()
        self.plugin._core._http_service = None
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.plugin._core._song_tasks, {})
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertEqual(self.sends, [])


class PassiveDeliveryTests(SongFlowBase):
    """delivery_mode "passive": the voice and the failure lines are replies to the message that picked the song."""

    delivery_mode = "passive"

    def kinds(self, event):
        return [type(chain.chain[0]).__name__ for chain in event.sent]

    async def test_group_pick_replies_to_the_picking_message_not_send_message(self):
        self.seed(group=True)
        await self.say("2", group=True)
        event = self.last_event
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(self.kinds(event), ["FakeRecord"])
        self.assertEqual(event.sent[0].chain[0].file, str(self.cover.path))
        self.assertEqual(self.sends, [])
        self.assertIn("via=tag pick=wy result=sent mode=passive total_ms=", self.song_logs()[0])

    async def test_private_pick_uses_event_send_too(self):
        self.seed()
        await self.say("1")
        event = self.last_event
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.kinds(event), ["FakeRecord"])
        self.assertEqual(self.sends, [])

    async def test_group_event_send_failure_does_not_fall_back(self):
        self.event_send_error = RuntimeError("window passed")
        self.seed(group=True)
        await self.say("2", group=True)
        await self.finish_delivery(MEMBER_KEY)
        await asyncio.sleep(0)
        self.assertEqual(self.sends, [])
        self.assertIn("result=failed:RuntimeError mode=passive", self.song_logs()[0])
        self.assertTrue(any("failure line not sent" in line for line in self.module.test_logger.warnings))

    async def test_private_event_send_failure_falls_back_to_a_proactive_send(self):
        self.event_send_error = RuntimeError("rejected")
        self.seed()
        await self.say("1")
        await self.finish_delivery(OWNER_KEY)
        (umo, chain), = self.sends
        self.assertEqual((umo, type(chain[0]).__name__, chain[0].file), (OWNER_UMO, "FakeRecord", str(self.cover.path)))
        self.assertIn("result=sent mode=proactive_fallback", self.song_logs()[0])

    async def test_failure_lines_go_through_event_send(self):
        self.seed(group=True)
        self.cover.resolve_error = CoverError("preview_only")
        await self.say("2", group=True)
        event = self.last_event
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual([chain.chain[0].text for chain in event.sent], [FAIL])
        self.assertEqual(self.sends, [])
        self.assertIn("result=failed:preview_only mode=passive", self.song_logs()[0])

    async def test_queue_full_line_goes_through_event_send(self):
        self.seed(group=True)
        self.cover.sing_error = CoverError("QueueFull")
        await self.say("2", group=True)
        event = self.last_event
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual([chain.chain[0].text for chain in event.sent], [self.module.SONG_QUEUE_FULL_TEXT])
        self.assertEqual(self.sends, [])

    async def test_link_path_replies_to_the_link_message(self):
        async def detail(song_id, **kwargs):
            return {"name": "向云端", "artist": "", "album": ""}

        with patch.object(submodule(self.module, "mixin_song"), "fetch_netease_song", detail):
            await self.say("https://music.163.com/song?id=5", group=True)
            event = self.last_event
            await self.finish_delivery(MEMBER_KEY)
        # the accept line is the handler's own reply; the voice is the only event.send
        self.assertEqual(self.kinds(event), ["FakeRecord"])
        self.assertEqual(self.sends, [])
        self.assertIn("via=link pick=wy result=sent mode=passive", self.song_logs()[0])

    async def test_switching_the_setting_changes_the_next_delivery(self):
        self.seed()
        await self.say("1")
        first = self.last_event
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual((self.kinds(first), len(self.sends)), (["FakeRecord"], 0))
        await self.set_song_settings(delivery_mode="proactive")
        self.seed()
        await self.say("1")
        second = self.last_event
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual((self.kinds(second), len(self.sends)), ([], 1))
        self.assertIn("mode=proactive total_ms=", self.song_logs()[1])
        await self.set_song_settings(delivery_mode="passive")
        self.seed()
        await self.say("1")
        third = self.last_event
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual((self.kinds(third), len(self.sends)), (["FakeRecord"], 1))

    async def test_a_passive_send_waits_for_the_picking_reply_to_go_out(self):
        stamps = []
        self.seed()
        with patch.object(submodule(self.module, "mixin_song"), "SONG_PASSIVE_SETTLE_SECONDS", 0.2):
            await self.say("1")
            started = asyncio.get_running_loop().time()
            event = self.last_event
            original_send = event.send

            async def send(chain):
                stamps.append(asyncio.get_running_loop().time() - started)
                await original_send(chain)

            event.send = send
            await self.finish_delivery(OWNER_KEY)
        self.assertEqual(len(stamps), 1)
        self.assertGreaterEqual(stamps[0], 0.15)

    async def test_a_new_install_defaults_to_passive(self):
        fresh = ConfigStore(Path(self.directory.name) / "fresh")
        self.assertEqual(fresh.config["song_request"]["delivery_mode"], "passive")


class ProactiveModeIsUnchangedTests(SongFlowBase):
    """delivery_mode "proactive" is exactly the P7c path: send_message only, event.send never used."""

    async def test_voice_and_failure_lines_use_send_message_only(self):
        self.event_send_error = AssertionError("event.send must not be used in proactive mode")
        self.seed(group=True)
        await self.say("2", group=True)
        event = self.last_event
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual(event.sent, [])
        self.assertEqual([type(chain[0]).__name__ for _umo, chain in self.sends], ["FakeRecord"])
        self.assertEqual(self.sends[0][0], GROUP_UMO)
        self.sends.clear()
        self.cover.sing_error = CoverError("Timeout")
        self.seed(group=True)
        await self.say("2", group=True)
        await self.finish_delivery(MEMBER_KEY)
        self.assertEqual([chain[0].text for _umo, chain in self.sends], [FAIL])
        self.assertTrue(all("mode=proactive total_ms=" in line for line in self.song_logs() if "total_ms" in line))

    async def test_a_failed_group_send_never_retries_through_the_event(self):
        async def send_message(umo, chain):
            raise RuntimeError("主动消息失败, 无权限")

        self.plugin.context = SimpleNamespace(send_message=send_message)
        self.seed(group=True)
        await self.say("2", group=True)
        event = self.last_event
        await self.finish_delivery(MEMBER_KEY)
        await asyncio.sleep(0)
        self.assertEqual(event.sent, [])


class LinkTests(SongFlowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.detail_calls = []
        self.short_calls = []
        self.detail = {"name": "向云端", "artist": "小霞/海生", "album": "向云端"}
        self.detail_error = None
        self.short_result = "https://music.163.com/song?id=777"
        self.short_error = None

        async def fake_detail(song_id, **kwargs):
            self.detail_calls.append(song_id)
            if self.detail_error is not None:
                raise self.detail_error
            return dict(self.detail)

        async def fake_short(url, **kwargs):
            self.short_calls.append(url)
            if self.short_error is not None:
                raise self.short_error
            return self.short_result

        for name, fake in (("fetch_netease_song", fake_detail), ("resolve_netease_short_link", fake_short)):
            patcher = patch.object(submodule(self.module, "mixin_song"), name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_long_link_is_sung_directly_without_a_list_or_st(self):
        replies = await self.say("看看这个 https://music.163.com/song?id=2049512697")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "向云端")])
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.detail_calls, ["2049512697"])
        self.assertEqual(self.cover.calls[0], ("resolve", "wy", "2049512697", "向云端", "小霞/海生"))
        self.assertEqual(type(self.sends[0][1][0]).__name__, "FakeRecord")
        self.assertEqual([call for call in self.cover.calls if call[0] == "search"], [])
        line = self.song_logs()[0]
        self.assertIn("via=link pick=wy result=sent", line)
        self.assertNotIn("163.com", line)

    async def test_a_short_link_is_resolved_first(self):
        replies = await self.say("分享单曲：向云端 http://163cn.tv/AbC123 (来自网易云音乐)")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "向云端")])
        self.assertEqual(self.short_calls, ["http://163cn.tv/AbC123"])
        self.assertEqual(self.detail_calls, ["777"])
        await self.finish_delivery(OWNER_KEY)

    async def test_an_unresolvable_short_link_gets_the_fail_line(self):
        for error, result in ((MusicError("timeout"), None), (None, "https://music.163.com/playlist?id=5"),
                              (None, "https://music.163.com/#/album?id=5")):
            with self.subTest(error=error, result=result):
                self.short_error = error
                if result:
                    self.short_result = result
                self.assertEqual(await self.say("http://163cn.tv/zzz"), [FAIL])
                self.assertEqual(self.plugin._core._song_tasks, {})

    async def test_a_failing_detail_lookup_falls_back_to_a_generic_name(self):
        self.detail_error = MusicError("timeout")
        replies = await self.say("https://music.163.com/song?id=55")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "网易云歌曲 55")])
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.calls[0][1:], ("wy", "55", "网易云歌曲 55", ""))

    async def test_not_allowed_gets_the_refusal_and_nothing_starts(self):
        await self.set_song_settings(owner_min_affinity=100)
        self.assertEqual(await self.say("https://music.163.com/song?id=5"), [REFUSE])
        self.assertEqual(self.plugin._core._song_tasks, {})
        self.assertEqual(self.detail_calls, [])
        self.assertTrue(any("via=link" in line and "skip:NotAllowed" in line for line in self.song_logs()))

    async def test_group_member_below_the_threshold_is_refused(self):
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 20, low=-50, high=100)
        self.assertEqual(await self.say("https://music.163.com/song?id=5", group=True), [REFUSE])
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 21, low=-50, high=100)
        self.assertEqual(await self.say("https://music.163.com/song?id=5", group=True),
                         [ACCEPT.replace("{歌名}", "向云端")])
        await self.finish_delivery(MEMBER_KEY)

    async def test_playlists_and_albums_are_ordinary_chat(self):
        self.assertIsNone(await self.say("https://music.163.com/playlist?id=5"))
        self.assertIsNone(await self.say("https://music.163.com/#/album?id=5"))

    async def test_with_the_feature_off_a_link_is_ordinary_chat(self):
        await self.set_song_settings(enabled=False)
        self.assertIsNone(await self.say("https://music.163.com/song?id=5"))
        self.assertEqual(self.detail_calls, [])

    async def test_a_link_while_busy_is_told_to_wait(self):
        self.cover.gate = asyncio.Event()
        await self.say("https://music.163.com/song?id=5")
        await asyncio.sleep(0)
        self.assertEqual(await self.say("https://music.163.com/song?id=6"), [self.module.SONG_BUSY_TEXT])
        self.cover.gate.set()
        await self.finish_delivery(OWNER_KEY)

    async def test_a_link_is_handled_even_while_a_list_is_pending(self):
        self.seed()
        replies = await self.say("https://music.163.com/song?id=5")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "向云端")])
        await self.finish_delivery(OWNER_KEY)


class CommandTests(SongFlowBase):
    async def sing(self, text, *, group=False, sender="member-a"):
        event = self.make_event(text, group=group, sender=sender)
        results = await self.run_handler(self.plugin.stbridge_sing, event)
        return [item.text for item in results]

    async def test_the_command_is_registered_as_chang(self):
        self.assertEqual(self.plugin.stbridge_sing.command_name, "唱")

    async def test_private_owner_gets_the_list_directly(self):
        replies = await self.sing("唱 晴天 周杰伦")
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("《晴天 周杰伦》找到这些版本："))
        self.assertIn(("search", "晴天 周杰伦", "wy"), self.cover.calls)
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual((pending.query, pending.via), ("晴天 周杰伦", "command"))

    async def test_the_wake_prefix_is_tolerated(self):
        for text in ("/琴音 唱 晴天", "/唱 晴天", "唱晴天", "  唱   晴天  "):
            with self.subTest(text=text):
                self.cover.calls.clear()
                replies = await self.sing(text)
                self.assertTrue(replies[0].startswith("《晴天》找到这些版本："), replies)

    async def test_the_usage_line_comes_from_the_texts_config_with_the_nickname(self):
        def edit(config):
            config["characters"]["小夜"] = {
                "name": "小夜", "model_name": "", "default_emotion": "neutral", "emotions": {},
                "nickname": "夜夜", "st_name": "XiaoYe",
            }
            config["default_character"] = "小夜"
            config["texts"]["song_usage"] = "{昵称}教你：唱 歌名"

        await self.store.update(edit)
        self.assertEqual(await self.sing("唱"), ["夜夜教你：唱 歌名"])
        # the wake word is the nickname now
        self.assertTrue((await self.sing("/夜夜 唱 晴天"))[0].startswith("《晴天》找到这些版本："))

    async def test_empty_name_gets_the_usage(self):
        self.assertEqual(await self.sing("唱"), [self.module.SONG_USAGE_TEXT])
        self.assertEqual(await self.sing("唱   "), [self.module.SONG_USAGE_TEXT])
        self.assertEqual(self.cover.calls, [])

    async def test_not_allowed_gets_the_refusal(self):
        await self.set_song_settings(owner_min_affinity=100)
        self.assertEqual(await self.sing("唱 晴天"), [REFUSE])
        self.assertEqual(self.plugin._core._song_pending, {})
        self.assertEqual(self.cover.calls, [])

    async def test_feature_off_is_a_refusal_too(self):
        await self.set_song_settings(enabled=False)
        self.assertEqual(await self.sing("唱 晴天"), [REFUSE])

    async def test_no_results_gets_the_fail_line(self):
        self.cover.results = {"wy": [], "kg": []}
        self.assertEqual(await self.sing("唱 晴天"), [FAIL])
        self.assertEqual(self.plugin._core._song_pending, {})

    async def test_private_chat_from_anyone_but_the_operator_gets_no_reply(self):
        event = self.make_event("唱 晴天")
        event.sender_id = "stranger"
        self.assertEqual(await self.run_handler(self.plugin.stbridge_sing, event), [])
        self.assertEqual(self.cover.calls, [])

    async def test_group_members_use_the_member_track(self):
        replies = await self.sing("唱 晴天", group=True)
        self.assertIn("群里请 @我 再回复哦。", replies[0])
        self.assertTrue(self.plugin._core._song_pending[(GROUP_UMO, MEMBER_KEY)].group)
        self.plugin._core._affinity_store.set_value(MEMBER_KEY, 20, low=-50, high=100)
        self.assertEqual(await self.sing("唱 晴天", group=True), [REFUSE])

    async def test_command_then_pick_delivers(self):
        await self.sing("唱 晴天")
        replies = await self.say("2")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "晴天")])
        await self.finish_delivery(OWNER_KEY)
        self.assertIn("via=command", self.song_logs()[0])
        self.assertEqual(type(self.sends[0][1][0]).__name__, "FakeRecord")

    async def test_the_command_does_not_ask_st(self):
        await self.sing("唱 晴天")
        self.assertIsNone(self.plugin._core._request_state_manager.peek_pending_request())


class PitchPresetFlowTests(SongFlowBase):
    """P7e: keywords in the request pick a pitch preset (vocal / overall shifts for the cover)."""

    MALE = (12, 0)
    UP = (0, 2)

    async def sing_command(self, text, *, group=False):
        event = self.make_event(text, group=group)
        results = await self.run_handler(self.plugin.stbridge_sing, event)
        return [item.text for item in results]

    async def test_command_keyword_is_removed_from_the_search_and_kept_as_preset(self):
        replies = await self.sing_command("唱 男声 晴天")
        self.assertTrue(replies[0].startswith("《晴天》找到这些版本："), replies)
        self.assertIn(("search", "晴天", "wy"), self.cover.calls)
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual(pending.query, "晴天")
        self.assertEqual(pending.preset["name"], "男声歌")

    async def test_command_without_a_keyword_has_no_preset(self):
        await self.sing_command("唱 晴天")
        self.assertIsNone(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].preset)

    async def test_command_with_only_a_keyword_is_the_usage_reply(self):
        self.assertEqual(await self.sing_command("唱 男声"), [self.module.SONG_USAGE_TEXT])
        self.assertEqual(await self.sing_command("唱 升调，"), [self.module.SONG_USAGE_TEXT])
        self.assertEqual(self.cover.calls, [])

    async def test_the_preset_reaches_the_cover_as_vocal_and_overall_values(self):
        await self.sing_command("唱 男声 晴天")
        replies = await self.say("2")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "晴天")])
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [self.MALE])
        self.assertIn("preset=男声歌", self.song_logs()[-1])

    async def test_no_preset_passes_none_and_logs_none(self):
        await self.sing_command("唱 晴天")
        await self.say("2")
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [(None, None)])
        self.assertIn("preset=none", self.song_logs()[-1])

    async def test_words_after_the_number_win_over_the_request_preset(self):
        await self.sing_command("唱 男声 晴天")
        replies = await self.say("2 升调")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "晴天")])
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [self.UP])
        self.assertIn("preset=升调", self.song_logs()[-1])

    async def test_a_number_with_a_preset_word_works_without_an_earlier_preset(self):
        await self.sing_command("唱 晴天")
        await self.say("2 降调")
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [(0, -2)])

    async def test_words_after_the_number_that_name_no_preset_are_ordinary_chat(self):
        await self.sing_command("唱 男声 晴天")
        # not a pick: it goes to ST (say() reports None) and the list stays open
        self.assertIsNone(await self.say("2 那首不错"))
        self.assertIn((OWNER_UMO, OWNER_KEY), self.plugin._core._song_pending)
        self.assertEqual(self.plugin._core._song_tasks, {})
        self.assertEqual(self.cover.sing_args, [])

    async def test_plain_number_keeps_the_pending_preset(self):
        await self.sing_command("唱 男声 晴天")
        await self.say("2")
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [self.MALE])

    async def test_research_keeps_the_preset_and_a_new_keyword_replaces_it(self):
        await self.sing_command("唱 男声 晴天")
        replies = await self.say("0 周杰伦")
        self.assertTrue(replies[0].startswith("《晴天》找到这些版本："))
        self.assertEqual(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].preset["name"], "男声歌")
        self.cover.calls.clear()
        await self.say("0 叶惠美 降调")
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual(pending.preset["name"], "降调")
        self.assertIn(("search", "晴天 叶惠美", "wy"), self.cover.calls)  # the keyword is not searched for

    async def test_research_with_only_a_keyword_searches_again_with_the_new_preset(self):
        await self.sing_command("唱 晴天")
        await self.say("0 升调")
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual(pending.preset["name"], "升调")
        self.assertEqual(pending.query, "晴天")

    async def test_tag_path_uses_the_users_own_words_not_the_prefixed_request_text(self):
        event = self.make_event("来首男声的歌吧", group=True)
        # the bridge text sent to ST starts with the speaker prefix; the preset must come from the raw message
        results = await TagPathTests.run_tag_turn(self, event, TAG_REPLY, handler=self.plugin.stbridge_group_message)
        self.assertEqual(len(results), 1)
        pending = self.plugin._core._song_pending[(GROUP_UMO, MEMBER_KEY)]
        self.assertEqual(pending.preset["name"], "男声歌")
        self.assertEqual(pending.query, "晴天")  # the name from ST is untouched

    async def test_tag_path_without_a_keyword_has_no_preset(self):
        event = self.make_event("唱首歌吧")
        await TagPathTests.run_tag_turn(self, event, TAG_REPLY)
        self.assertIsNone(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].preset)

    async def test_tag_name_containing_a_keyword_is_cleaned(self):
        event = self.make_event("唱首歌吧")
        await TagPathTests.run_tag_turn(self, event, '[情绪:开心][唱歌:男声 晴天]☆‹いいよ›"好呀"')
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual(pending.query, "晴天")
        self.assertEqual(pending.preset["name"], "男声歌")

    async def test_tag_name_that_is_only_a_keyword_keeps_its_name(self):
        event = self.make_event("唱首歌吧")
        await TagPathTests.run_tag_turn(self, event, '[情绪:开心][唱歌:升调]☆‹いいよ›"好呀"')
        self.assertEqual(self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)].query, "升调")

    async def test_link_path_matches_the_whole_message(self):
        detail_calls = []

        async def fake_detail(song_id, **kwargs):
            detail_calls.append(song_id)
            return {"name": "向云端", "artist": "小霞/海生", "album": "向云端"}

        with patch.object(submodule(self.module, "mixin_song"), "fetch_netease_song", fake_detail):
            replies = await self.say("男声版 https://music.163.com/song?id=2049512697")
        self.assertEqual(replies, [ACCEPT.replace("{歌名}", "向云端")])
        self.assertEqual(detail_calls, ["2049512697"])  # the link is parsed from the untouched text
        await self.finish_delivery(OWNER_KEY)
        self.assertEqual(self.cover.sing_args, [self.MALE])
        self.assertIn("via=link", self.song_logs()[-1])
        self.assertIn("preset=男声歌", self.song_logs()[-1])

    async def test_no_presets_means_keywords_do_nothing(self):
        await self.store.update(lambda config: config["cover"].update(pitch_presets=[]))
        await self.sing_command("唱 男声 晴天")
        pending = self.plugin._core._song_pending[(OWNER_UMO, OWNER_KEY)]
        self.assertEqual((pending.query, pending.preset), ("男声 晴天", None))


if __name__ == "__main__":
    unittest.main()

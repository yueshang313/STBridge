import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.affinity_store import AffinityStore, user_key
from test_plugin_lifecycle import (
    _bridge_event,
    _complete_bridge_reply,
    _load_plugin_module,
    _ready_plugin,
)


THINKING = (
    '先想想："曼联"是什么？用户说了"是不是"，我要用 ☆‹仮›"仮" 的格式。\n'
    "[情绪:开心] 不对，应该认真。<UpdateVariable>_.add('藤田琴音.好感度', 99);\n"
    "字面的 <game> 还没有结束。\n"
)
BODY = (
    "\n[情绪:认真]\n"
    "☆‹「マンチェスター・ユナイテッドですね。…ファンがたくさんいるんですよ」›\n"
    '"是曼联对吧。…在全世界都有非常多的粉丝呢"\n'
)
MVU = (
    "<UpdateVariable><Analysis>网友说了\"很可爱\"</Analysis>\n"
    "_.add('藤田琴音.好感度', 3);//开心\n</UpdateVariable>"
)
JAPANESE = "「マンチェスター・ユナイテッドですね。…ファンがたくさんいるんですよ」"
CHINESE = "是曼联对吧。…在全世界都有非常多的粉丝呢"


class SplitGameBlockTests(unittest.TestCase):
    def setUp(self):
        self.split = _load_plugin_module().split_game_block

    def test_body_and_tail_around_the_last_game_block(self):
        self.assertEqual(self.split("思维链<game>正文</game>尾巴"), ("正文", "尾巴"))

    def test_last_opening_tag_wins_and_earlier_text_is_dropped(self):
        self.assertEqual(self.split("<game>旧</game>x<game>新</game>y"), ("新", "y"))
        self.assertEqual(self.split("a<game>b<game>c</game>d"), ("c", "d"))

    def test_missing_closing_tag_runs_to_the_end(self):
        self.assertEqual(self.split("想<game>被截断的正文"), ("被截断的正文", ""))

    def test_tags_ignore_case_and_inner_whitespace(self):
        self.assertEqual(self.split("<GAME >甲</Game >乙"), ("甲", "乙"))

    def test_only_the_first_closing_tag_after_the_opening_ends_the_body(self):
        self.assertEqual(self.split("<game>甲</game>乙</game>丙"), ("甲", "乙</game>丙"))

    def test_no_game_tag_or_non_string_returns_none(self):
        self.assertIsNone(self.split("没有标签"))
        self.assertIsNone(self.split("</game> only a close"))
        self.assertIsNone(self.split(None))


class GameBlockDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_plugin_module()
        self.logger = self.module.test_logger
        for messages in (self.logger.warnings, self.logger.errors, self.logger.infos):
            messages.clear()
        self._directory = tempfile.TemporaryDirectory(prefix="st-bridge-game-")
        self.plugin = _ready_plugin(self.module, voice=True)
        self.plugin._core._affinity_store = AffinityStore(Path(self._directory.name))

    async def asyncTearDown(self):
        self._directory.cleanup()

    def affinity(self):
        return self.plugin._core._affinity_store.get(user_key("qq-platform", "qq-user"), 0)

    async def deliver(self, reply):
        event = _bridge_event(self.module, "hello", sender_name="操作者")
        results = await _complete_bridge_reply(self.plugin, event, reply)
        return event, [result.text for result in results]

    async def test_reasoning_with_quotes_tags_and_update_lookalikes_is_ignored(self):
        reply = THINKING + "<game>" + BODY + "</game>\n" + MVU
        event, texts = await self.deliver(reply)
        self.assertEqual(texts, [CHINESE])
        self.assertEqual(self.plugin._core._tts.last_text, JAPANESE)
        self.assertIs(self.plugin._core._tts.eligible, True)
        self.assertEqual(self.affinity(), 3)
        emotion_logs = [line for line in self.logger.infos if "TTS emotion" in line]
        self.assertEqual(len(emotion_logs), 1)
        self.assertIn("requested=认真", emotion_logs[0])
        game_logs = [line for line in self.logger.infos if "game block" in line]
        self.assertEqual(game_logs, [f"[STBridge] Reply game block found (thinking_chars={len(THINKING)})"])
        joined = "".join(self.logger.infos + self.logger.warnings)
        for secret in ("曼联", "マンチェスター", "先想想"):
            self.assertNotIn(secret, joined)

    async def test_update_block_inside_the_body_is_also_counted(self):
        reply = "想<game>" + BODY + MVU + "</game>"
        _, texts = await self.deliver(reply)
        self.assertEqual(texts, [CHINESE])
        self.assertEqual(self.affinity(), 3)

    async def test_last_game_block_is_used_when_there_are_several(self):
        reply = "<game>☆‹旧›\"旧的\"</game>中间<game>" + BODY + "</game>" + MVU
        _, texts = await self.deliver(reply)
        self.assertEqual(texts, [CHINESE])
        self.assertEqual(self.affinity(), 3)

    async def test_truncated_output_without_a_closing_tag_still_delivers(self):
        _, texts = await self.deliver(THINKING + "<game>" + BODY)
        self.assertEqual(texts, [CHINESE])
        self.assertEqual(self.affinity(), 0)

    async def test_emotion_tag_after_the_game_block_does_not_count(self):
        reply = "<game>" + BODY.replace("[情绪:认真]\n", "") + "</game>[情绪:生气]\n" + MVU
        event, texts = await self.deliver(reply)
        self.assertEqual(texts, [CHINESE])
        emotion_logs = [line for line in self.logger.infos if "TTS emotion" in line]
        self.assertIn("requested=none", emotion_logs[0])

    async def test_extra_quotes_in_the_body_are_still_rejected(self):
        reply = '想<game>☆‹日本語›"中文" 还有"多余"的引号</game>' + MVU
        event, texts = await self.deliver(reply)
        self.assertEqual(texts, [self.module.INVALID_REPLY_TEXT])
        self.assertIsNone(self.plugin._core._tts.eligible)
        self.assertEqual(self.affinity(), 0)

    async def test_reply_without_a_game_block_keeps_the_old_behavior_and_logs(self):
        _, texts = await self.deliver('☆‹日本語›"中文"')
        self.assertEqual(texts, ["中文"])
        self.assertEqual([line for line in self.logger.infos if "game block" in line], [])


if __name__ == "__main__":
    unittest.main()

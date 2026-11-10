"""P7c: pure song-request logic (settings, links, sing tag, pick list, replies) and the music_source helpers."""

import unittest
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import music_source
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    EmotionConfigError,
    validate_affinity_settings,
    validate_queue_settings,
    validate_sticker_settings,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.schedule_config import validate_schedule_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.music_source import (
    MusicError,
    is_instrumental,
    is_netease_host,
    parse_netease_detail,
    pick_result,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import (
    build_song_list,
    default_song_request_settings,
    fill_song_name,
    parse_netease_link,
    match_pitch_preset,
    parse_pick,
    song_request_settings,
    strip_sing_tags,
    validate_song_request_settings,
)


def online(source, song_id, name, artist="歌手", album=""):
    return {"source": source, "id": str(song_id), "name": name, "artist": artist, "album": album}


class InstrumentalTests(unittest.TestCase):
    def test_every_word_matches(self):
        for name in ("晴天 伴奏", "晴天（纯音乐）", "晴天 消音版", "晴天 钢琴版", "晴天 轻音乐", "晴天 八音盒",
                     "Song Instrumental", "Song (Inst.)", "Song KARAOKE", "Song Piano Ver", "Song off vocal",
                     "Song off-vocal", "Song offvocal", "Song No Vocal", "Song no-vocal", "Song novocal"):
            with self.subTest(name=name):
                self.assertTrue(is_instrumental(name), name)

    def test_english_words_need_word_boundaries(self):
        for name in ("Instinct", "Pianoman", "Installation", "Instagram", "晴天", "Hello Vocal", ""):
            with self.subTest(name=name):
                self.assertFalse(is_instrumental(name), name)

    def test_the_album_counts_too(self):
        self.assertTrue(is_instrumental("晴天", "叶惠美 伴奏集"))
        self.assertTrue(is_instrumental("晴天", "Best Of Piano"))
        self.assertFalse(is_instrumental("晴天", "叶惠美"))
        self.assertFalse(is_instrumental(None, None))

    def test_pick_result_skips_instrumental_versions(self):
        results = [online("wy", 1, "晴天 (伴奏)"), online("wy", 2, "晴天", album="Piano Collection"), online("wy", 3, "晴天")]
        self.assertEqual(pick_result(results, "晴天", None)["id"], "3")
        self.assertIsNone(pick_result([online("wy", 1, "晴天 伴奏")], "晴天", None))

    def test_the_accompaniment_word_is_no_longer_just_a_sort_hint(self):
        self.assertFalse(music_source.is_variant("晴天 伴奏", ""))


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = default_song_request_settings()
        self.assertEqual(
            {key: settings[key] for key in ("enabled", "owner_min_affinity", "group_min_affinity", "refuse_percent",
                                           "list_timeout_seconds", "per_source")},
            {"enabled": True, "owner_min_affinity": 0, "group_min_affinity": 21, "refuse_percent": 0,
             "list_timeout_seconds": 180, "per_source": 5},
        )
        self.assertEqual(len(settings["accept_texts"]), 2)
        self.assertEqual(len(settings["refuse_texts"]), 2)
        self.assertEqual(len(settings["fail_texts"]), 1)
        self.assertEqual(validate_song_request_settings(settings), settings)
        self.assertEqual(validate_song_request_settings({}), settings)

    def test_delivery_mode(self):
        self.assertEqual(default_song_request_settings()["delivery_mode"], "passive")
        for mode in ("passive", "proactive"):
            self.assertEqual(validate_song_request_settings({"delivery_mode": mode})["delivery_mode"], mode)
        for bad in ("", "Passive", "active", None, 1, True, ["passive"]):
            with self.subTest(bad=bad):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_song_request_settings({"delivery_mode": bad})
                self.assertEqual(caught.exception.status, 400)

    def test_an_older_config_without_the_field_gets_the_default(self):
        old = default_song_request_settings()
        del old["delivery_mode"]
        self.assertEqual(song_request_settings({"song_request": old})["delivery_mode"], "passive")
        self.assertEqual(validate_song_request_settings(old)["delivery_mode"], "passive")

    def test_default_lists_are_copies(self):
        first = default_song_request_settings()
        first["accept_texts"].append("x")
        self.assertEqual(len(default_song_request_settings()["accept_texts"]), 2)

    def test_bad_numbers_are_400(self):
        bad = [("owner_min_affinity", -1), ("owner_min_affinity", 101), ("group_min_affinity", -51),
               ("group_min_affinity", 101), ("refuse_percent", 101), ("refuse_percent", -1),
               ("list_timeout_seconds", 59), ("list_timeout_seconds", 601), ("per_source", 0), ("per_source", 11),
               ("per_source", 2.5), ("refuse_percent", True), ("per_source", "5"), ("refuse_percent", None),
               ("refuse_percent", float("inf")), ("refuse_percent", float("-inf")), ("list_timeout_seconds", float("nan"))]
        for field, value in bad:
            with self.subTest(field=field, value=value):
                with self.assertRaises(EmotionConfigError) as caught:
                    validate_song_request_settings({field: value})
                self.assertEqual(caught.exception.status, 400)

    def test_group_floor_may_be_negative_and_float_integers_are_accepted(self):
        result = validate_song_request_settings({"group_min_affinity": -50, "refuse_percent": 30.0})
        self.assertEqual(result["group_min_affinity"], -50)
        self.assertEqual(result["refuse_percent"], 30)
        self.assertIsInstance(result["refuse_percent"], int)

    def test_enabled_must_be_a_bool(self):
        for value in (1, "true", None):
            with self.assertRaises(EmotionConfigError):
                validate_song_request_settings({"enabled": value})
        self.assertFalse(validate_song_request_settings({"enabled": False})["enabled"])

    def test_text_lists(self):
        for field in ("accept_texts", "refuse_texts", "fail_texts"):
            for value in ([], ["a"] * 11, [""], ["   "], ["x" * 101], [1], "text", None, ["ok", None]):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(EmotionConfigError):
                        validate_song_request_settings({field: value})
            good = validate_song_request_settings({field: ["  一句  ", "x" * 100]})
            self.assertEqual(good[field], ["一句", "x" * 100])
            self.assertEqual(len(validate_song_request_settings({field: ["a"] * 10})[field]), 10)

    def test_non_object_is_400(self):
        with self.assertRaises(EmotionConfigError):
            validate_song_request_settings([])

    def test_loader_falls_back_to_defaults(self):
        self.assertEqual(song_request_settings({}), default_song_request_settings())
        self.assertEqual(song_request_settings({"song_request": {"per_source": 99}}), default_song_request_settings())
        self.assertEqual(song_request_settings({"song_request": {"per_source": 3}})["per_source"], 3)
        self.assertEqual(song_request_settings(None), default_song_request_settings())

    def test_song_name_placeholder_is_a_plain_replace(self):
        self.assertEqual(fill_song_name("《{歌名}》{x}", "晴天"), "《晴天》{x}")
        self.assertEqual(fill_song_name("好", "晴天"), "好")
        self.assertEqual(fill_song_name("{歌名}", "{0}"), "{0}")


class LinkTests(unittest.TestCase):
    def test_long_links(self):
        cases = {
            "https://music.163.com/song?id=2049512697": "2049512697",
            "https://music.163.com/#/song?id=123&userid=9": "123",
            "https://y.music.163.com/m/song?id=77&uct2=abc": "77",
            "https://music.163.com/song/55?x=1": "55",
            "http://music.163.com/m/song/66": "66",
            "https://music.163.com/#/song?userid=1&id=88": "88",
        }
        for text, song_id in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_netease_link(text), ("id", song_id))

    def test_links_inside_share_text(self):
        text = "分享歌手周杰伦的单曲《晴天》: https://y.music.163.com/m/song?id=186016&uct2=U2FsdGVk (来自@网易云音乐)"
        self.assertEqual(parse_netease_link(text), ("id", "186016"))
        text = "分享单曲：向云端 http://163cn.tv/AbC123 (来自网易云音乐)"
        self.assertEqual(parse_netease_link(text), ("short", "http://163cn.tv/AbC123"))
        self.assertEqual(parse_netease_link("听听这首 https://163cn.tv/xYz9，好听"), ("short", "https://163cn.tv/xYz9"))

    def test_only_songs_count(self):
        for text in ("https://music.163.com/#/album?id=5", "https://music.163.com/playlist?id=5",
                     "https://y.music.163.com/m/playlist?id=9", "https://music.163.com/#/mv?id=7",
                     "https://music.163.com/#/artist?id=7", "https://example.com/song?id=1",
                     "https://evil-music.163.com.example.com/song?id=1", "music.163.com/song?id=5",
                     "看看 https://www.bilibili.com/video/BV1", "晴天", "", None, 5):
            with self.subTest(text=text):
                self.assertIsNone(parse_netease_link(text))

    def test_the_first_song_link_wins_over_a_playlist(self):
        text = "https://music.163.com/playlist?id=1 and https://music.163.com/song?id=2"
        self.assertEqual(parse_netease_link(text), ("id", "2"))

    def test_hosts(self):
        self.assertTrue(is_netease_host("music.163.com"))
        self.assertTrue(is_netease_host("y.music.163.com"))
        self.assertTrue(is_netease_host("163cn.tv"))
        self.assertTrue(is_netease_host("MUSIC.163.COM."))
        self.assertFalse(is_netease_host("evilmusic.163.com.cn"))
        self.assertFalse(is_netease_host("notmusic.163.com"))
        self.assertFalse(is_netease_host("163.com"))
        self.assertFalse(is_netease_host(""))


class SingTagTests(unittest.TestCase):
    def test_tag_is_removed_and_name_returned(self):
        self.assertEqual(strip_sing_tags("[唱歌:晴天]\n☆‹行くよ›"), ("\n☆‹行くよ›", "晴天"))
        self.assertEqual(strip_sing_tags("a[唱歌：《晴天》]b"), ("ab", "晴天"))
        self.assertEqual(strip_sing_tags("[唱歌: 「 向云端 」 ]"), ("", "向云端"))
        self.assertEqual(strip_sing_tags('[唱歌:"Lemon"]'), ("", "Lemon"))

    def test_all_tags_are_removed_and_the_first_valid_name_wins(self):
        cleaned, name = strip_sing_tags("[唱歌:   ][唱歌:甲] mid [唱歌:乙]")
        self.assertEqual(cleaned, " mid ")
        self.assertEqual(name, "甲")

    def test_no_or_invalid_tags(self):
        self.assertEqual(strip_sing_tags("平常的回复"), ("平常的回复", None))
        self.assertEqual(strip_sing_tags("[唱歌:《》]"), ("", None))
        self.assertEqual(strip_sing_tags("[唱歌:" + "字" * 61 + "]"), ("[唱歌:" + "字" * 61 + "]", None))
        self.assertEqual(strip_sing_tags(None), ("", None))


class PickParseTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse_pick("3"), ("pick", 3, ""))
        self.assertEqual(parse_pick("  12 "), ("pick", 12, ""))
        self.assertEqual(parse_pick("３"), ("pick", 3, ""))
        self.assertEqual(parse_pick("１２"), ("pick", 12, ""))
        self.assertEqual(parse_pick("05"), ("pick", 5, ""))

    def test_research(self):
        self.assertEqual(parse_pick("0 周杰伦"), ("research", "周杰伦"))
        self.assertEqual(parse_pick("0，周杰伦"), ("research", "周杰伦"))
        self.assertEqual(parse_pick("０：叶惠美 专辑"), ("research", "叶惠美 专辑"))
        self.assertEqual(parse_pick("0,jay"), ("research", "jay"))
        self.assertEqual(parse_pick("0:  "), ("research_empty",))
        self.assertEqual(parse_pick("0"), ("research_empty",))
        self.assertEqual(parse_pick("０"), ("research_empty",))

    def test_cancel(self):
        self.assertEqual(parse_pick("取消"), ("cancel",))
        self.assertEqual(parse_pick(" 取消 "), ("cancel",))

    def test_everything_else_is_not_a_reply(self):
        for text in ("0周杰伦", "100", "123", "第3个", "3首", "好的", "取消吧", "", "   ", "-1", "1.5", None, 3):
            with self.subTest(text=text):
                self.assertIsNone(parse_pick(text))


class PickExtraTests(unittest.TestCase):
    def test_number_with_words(self):
        self.assertEqual(parse_pick("2 男声"), ("pick", 2, "男声"))
        self.assertEqual(parse_pick("２　男声"), ("pick", 2, "男声"))  # full-width digit and blank
        self.assertEqual(parse_pick("  3   升调 "), ("pick", 3, "升调"))
        self.assertEqual(parse_pick("12 低一点 好不好"), ("pick", 12, "低一点 好不好"))

    def test_zero_with_words_is_still_a_research(self):
        self.assertEqual(parse_pick("0 男声"), ("research", "男声"))
        self.assertEqual(parse_pick("0"), ("research_empty",))
        self.assertEqual(parse_pick("取消"), ("cancel",))

    def test_not_a_pick(self):
        for text in ("2号", "2首男声", "100 男声", "第2个 男声", "男声 2"):
            with self.subTest(text=text):
                self.assertIsNone(parse_pick(text))


PRESETS = [
    {"name": "男声歌", "vocal": 12, "inst": 0, "keywords": ["男声", "男生", "男歌手"]},
    {"name": "升调", "vocal": 0, "inst": 2, "keywords": ["升调", "高一点", "Key Up"]},
    {"name": "降调", "vocal": 0, "inst": -2, "keywords": ["降调", "低一点"]},
]


class PitchPresetMatchTests(unittest.TestCase):
    def test_keyword_is_found_and_removed(self):
        preset, cleaned = match_pitch_preset("男声 晴天", PRESETS)
        self.assertEqual((preset["name"], cleaned), ("男声歌", "晴天"))
        self.assertEqual(match_pitch_preset("晴天 男生版", PRESETS)[1], "晴天 版")
        self.assertEqual(match_pitch_preset("晴天，降调", PRESETS)[1], "晴天")
        self.assertEqual(match_pitch_preset("高一点、晴天", PRESETS)[1], "晴天")

    def test_no_match_returns_the_text_untouched(self):
        self.assertEqual(match_pitch_preset("晴天  周杰伦", PRESETS), (None, "晴天  周杰伦"))
        self.assertEqual(match_pitch_preset("晴天", []), (None, "晴天"))
        self.assertEqual(match_pitch_preset("", PRESETS), (None, ""))
        self.assertEqual(match_pitch_preset(None, PRESETS), (None, ""))

    def test_nfkc_and_case_insensitive(self):
        self.assertEqual(match_pitch_preset("ＫＥＹ　ＵＰ 晴天", PRESETS)[0]["name"], "升调")  # full-width
        self.assertEqual(match_pitch_preset("key up 晴天", PRESETS)[1], "晴天")
        self.assertEqual(match_pitch_preset("晴天 KEY UP", PRESETS)[1], "晴天")

    def test_the_original_text_around_the_keyword_is_kept(self):
        # only the keyword is cut; full-width letters elsewhere stay as written
        self.assertEqual(match_pitch_preset("ＡＢＣ 男声", PRESETS)[1], "ＡＢＣ")

    def test_first_preset_then_first_keyword_wins(self):
        preset, cleaned = match_pitch_preset("升调 男声 晴天", PRESETS)
        self.assertEqual(preset["name"], "男声歌")  # preset order, not position in the text
        self.assertEqual(cleaned, "升调 晴天")  # only that one keyword is removed
        self.assertEqual(match_pitch_preset("男歌手 男声 x", PRESETS)[1], "男歌手 x")  # keyword order inside a preset

    def test_only_the_first_occurrence_is_removed(self):
        self.assertEqual(match_pitch_preset("男声 晴天 男声", PRESETS)[1], "晴天 男声")

    def test_whitespace_and_separators_are_tidied(self):
        self.assertEqual(match_pitch_preset("晴天   男声   周杰伦", PRESETS)[1], "晴天 周杰伦")
        self.assertEqual(match_pitch_preset("，男声，", PRESETS)[1], "")
        self.assertEqual(match_pitch_preset("男声", PRESETS)[1], "")


class ListTests(unittest.TestCase):
    def test_order_labels_and_format(self):
        library = [{"id": "s1", "title": "晴天", "artist": "周杰伦"}]
        results = {
            "wy": [online("wy", 1, "晴天", "周杰伦", "叶惠美"), online("wy", 2, "晴天", "某人")],
            "kg": [online("kg", "k1", "晴天", "周杰伦", "叶惠美")],
        }
        entries, text = build_song_list("晴天", library, results, 5)
        self.assertEqual([item["label"] for item in entries], ["歌库", "网易云", "网易云", "酷狗"])
        self.assertEqual(entries[0], {"kind": "library", "song_id": "s1", "name": "晴天", "artist": "周杰伦",
                                      "album": "", "label": "歌库"})
        self.assertEqual(entries[1]["kind"], "online")
        self.assertEqual((entries[1]["source"], entries[1]["source_id"]), ("wy", "1"))
        self.assertEqual(text.splitlines(), [
            "《晴天》找到这些版本：",
            "1. 晴天 — 周杰伦[歌库]",
            "2. 晴天 — 周杰伦（叶惠美）[网易云]",
            "3. 晴天 — 某人[网易云]",
            "4. 晴天 — 周杰伦（叶惠美）[酷狗]",
            "回复序号选歌；回复“0 歌手或专辑”重新搜；回复“取消”结束（3分钟内有效）。",
        ])

    def test_missing_artist_and_album_are_left_out(self):
        _entries, text = build_song_list("x", [], {"wy": [online("wy", 1, "名", "", "")]}, 5)
        self.assertIn("1. 名[网易云]", text.splitlines())
        _entries, text = build_song_list("x", [], {"wy": [online("wy", 1, "名", "", "专")]}, 5)
        self.assertIn("1. 名（专）[网易云]", text.splitlines())

    def test_per_source_limit_applies_after_filtering(self):
        wy = [online("wy", 1, "晴天 伴奏"), *[online("wy", n, f"晴天{n}") for n in range(2, 12)]]
        kg = [online("kg", n, f"歌{n}") for n in range(1, 4)]
        entries, _text = build_song_list("晴天", [], {"wy": wy, "kg": kg}, 5)
        self.assertEqual([item["name"] for item in entries], ["晴天2", "晴天3", "晴天4", "晴天5", "晴天6", "歌1", "歌2", "歌3"])
        entries, _text = build_song_list("晴天", [], {"wy": wy, "kg": kg}, 2)
        self.assertEqual(len(entries), 4)

    def test_instrumental_entries_are_excluded_everywhere(self):
        library = [{"id": "s1", "title": "晴天 伴奏", "artist": "a"}, {"id": "s2", "title": "晴天", "artist": "b"}]
        results = {"wy": [online("wy", 1, "晴天 Instrumental"), online("wy", 2, "晴天", album="钢琴曲集"),
                          online("wy", 3, "晴天")],
                   "kg": [online("kg", 4, "晴天 (off vocal)")]}
        entries, _text = build_song_list("晴天", library, results, 5)
        self.assertEqual([(item["label"], item["name"]) for item in entries], [("歌库", "晴天"), ("网易云", "晴天")])
        self.assertEqual(entries[0]["song_id"], "s2")

    def test_a_library_song_is_not_listed_twice(self):
        library = [{"id": "s1", "title": "晴天", "artist": "周杰伦", "source": "wy", "source_id": "1"}]
        entries, _text = build_song_list("晴天", library, {"wy": [online("wy", 1, "晴天"), online("wy", 2, "晴天")]}, 5)
        self.assertEqual([item.get("source_id") for item in entries], [None, "2"])

    def test_truncation(self):
        library = [{"id": "s1", "title": "名" * 40, "artist": "歌" * 30}]
        entries, text = build_song_list("q", library, {"wy": [online("wy", 1, "n" * 40, "a" * 30, "b" * 30)]}, 5)
        self.assertEqual(entries[0]["name"], "名" * 29 + "…")
        self.assertEqual(entries[0]["artist"], "歌" * 19 + "…")
        self.assertEqual(entries[1]["name"], "n" * 29 + "…")
        self.assertEqual(entries[1]["artist"], "a" * 19 + "…")
        self.assertEqual(entries[1]["album"], "b" * 19 + "…")
        self.assertIn("1. " + "名" * 29 + "…", text)

    def test_empty_results_give_an_empty_list(self):
        self.assertEqual(build_song_list("x", [], {}, 5), ([], ""))
        self.assertEqual(build_song_list("x", [], {"wy": [online("wy", 1, "x 伴奏")], "kg": []}, 5), ([], ""))

    def test_group_tail_and_minutes_follow_the_settings(self):
        _entries, text = build_song_list("x", [], {"wy": [online("wy", 1, "x")]}, 5, group=True, timeout_seconds=300)
        lines = text.splitlines()
        self.assertTrue(lines[-1].endswith("（5分钟内有效）。群里请 @我 再回复哦。"))
        _entries, text = build_song_list("x", [], {"wy": [online("wy", 1, "x")]}, 5, group=False, timeout_seconds=90)
        self.assertTrue(text.splitlines()[-1].endswith("（2分钟内有效）。"))
        self.assertNotIn("@我", text)

    def test_unknown_sources_are_ignored(self):
        entries, _text = build_song_list("x", [], {"qq": [online("qq", 1, "x")], "wy": [online("wy", 2, "x")]}, 5)
        self.assertEqual(len(entries), 1)


class NonFiniteNumberTests(unittest.TestCase):
    """int(Infinity) / int(NaN) used to raise (a 500 in the console); they are plain 400s now."""

    def test_every_older_integer_check_rejects_infinity_and_nan(self):
        checks = [
            (validate_affinity_settings, "max_delta"),
            (validate_affinity_settings, "initial_value"),
            (validate_queue_settings, "max_waiting"),
            (validate_sticker_settings, "prob"),
            (validate_schedule_settings, "quiet_minutes"),
        ]
        for validate, field in checks:
            for number in (float("inf"), float("-inf"), float("nan")):
                with self.subTest(validate=validate.__name__, field=field, number=number):
                    with self.assertRaises(EmotionConfigError) as caught:
                        validate({field: number})
                    self.assertEqual(caught.exception.status, 400)


class NetEaseHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_detail_parsing(self):
        body = {"songs": [{"name": " 向云端 ", "artists": [{"name": "小霞"}, {"name": "海生"}],
                           "album": {"name": "向云端"}}], "code": 200}
        self.assertEqual(parse_netease_detail(body), {"name": "向云端", "artist": "小霞/海生", "album": "向云端"})
        body = {"songs": [{"name": "x", "ar": [{"name": "a"}], "al": {"name": "b"}}]}
        self.assertEqual(parse_netease_detail(body), {"name": "x", "artist": "a", "album": "b"})
        self.assertEqual(parse_netease_detail({"songs": [{"name": "x"}]}), {"name": "x", "artist": "", "album": ""})
        for bad in ({"songs": []}, {"songs": [{"name": " "}]}, {"songs": ["x"]}, {}, None, []):
            self.assertIsNone(parse_netease_detail(bad))

    async def test_fetch_rejects_a_non_numeric_id_without_any_request(self):
        with self.assertRaises(MusicError):
            await music_source.fetch_netease_song("1;DROP")

    async def test_short_link_follows_redirects_by_hand_and_stays_on_netease(self):
        calls = []

        class Response:
            def __init__(self, status, location=None):
                self.status = status
                self.headers = {"Location": location} if location else {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self, hops):
                self.hops = hops

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, **kwargs):
                calls.append((url, kwargs.get("allow_redirects")))
                return Response(*self.hops.pop(0))

        async def run(hops):
            calls.clear()
            with patch.object(music_source.aiohttp, "ClientSession", lambda **kwargs: Session(list(hops))):
                return await music_source.resolve_netease_short_link("https://163cn.tv/abc")

        final = await run([(302, "https://y.music.163.com/m/song?id=5")])
        self.assertEqual(final, "https://y.music.163.com/m/song?id=5")
        self.assertEqual(calls, [("https://163cn.tv/abc", False)])
        final = await run([(302, "/next"), (301, "https://music.163.com/song?id=6")])
        self.assertEqual(final, "https://music.163.com/song?id=6")
        self.assertEqual(len(calls), 2)
        with self.assertRaises(MusicError) as caught:
            await run([(302, "https://evil.example.com/x")])
        self.assertEqual(caught.exception.kind, "foreign_host")
        with self.assertRaises(MusicError):
            await run([(200, None)])
        with self.assertRaises(MusicError) as caught:
            await run([(302, "https://163cn.tv/a")] * 5)
        self.assertEqual(caught.exception.kind, "too_many_redirects")
        with self.assertRaises(MusicError):
            await music_source.resolve_netease_short_link("https://evil.example.com/x")


if __name__ == "__main__":
    unittest.main()

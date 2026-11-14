import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import reply_texts
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import (
    ConfigStore,
    EmotionConfigError,
    active_identity,
    affinity_settings,
    affinity_track,
    default_config,
    default_nickname,
    default_st_name,
    migrate_g1,
    stored_affinity_defaults,
    validate_config,
    validate_nickname,
    validate_st_name,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import song_request_settings
from test_console_server import _unused_loopback_port
from test_plugin_lifecycle import _load_plugin_module, _ready_plugin

# The 12 lines that used to be written into the code, and the song lines that already lived in the config,
# exactly as they were sent before G1 (with the default nickname).
ORIGINAL_TEXTS = {
    "invalid_reply": "（这次回复的格式不对，没能送达，请再说一次）",
    "too_long": "消息超过桥接文本长度限制，未创建请求。",
    "unavailable": "琴音现在不在线哦，晚点再来找我～",  # H1: in the character's voice
    "queue_full": "现在找琴音的人太多啦，请稍后再来～",
    "queue_timeout": "排队太久了，这条没能处理，请稍后再说一次。",
    "song_busy": "上一首还在准备中，等我唱完这首哦～",
    "song_queue_full": "现在点歌的人太多了，等一会儿再来吧",
    "song_usage": "用法：唱 歌名（比如：唱 晴天）",
    "song_cancel": "好，那下次再点吧～",
    "song_research_hint": "0 后面写上歌手或专辑，比如：0 周杰伦",
    "song_research_empty": "没搜到更合适的，换个歌手或专辑再试试～",
    "song_pick_range": "没有这个序号哦，请回复列表里有的序号～",
    "accept_texts": [
        "好～《{歌名}》对吧，我去准备一下，稍等我一会儿！",
        "收到！《{歌名}》……让我先练一下，马上就好～",
    ],
    "refuse_texts": ["诶……今天嗓子不太在状态，下次再唱给你听吧。", "唱歌的话……再让我多练习一阵子嘛。"],
    "fail_texts": ["呜……这首没能唱好，换一首试试吧？"],
    "limit_text": "今天已经唱了{次数}首啦，嗓子要休息一下，明天再来吧～",
}
# Added later (101a-E): the line said after the owner was claimed with a code.
LATER_TEXTS = {"owner_claimed": "认领成功，以后你就是主人啦。"}


def pre_g1_config(**changes):
    """A config as written before G1 (affinity holds the names, song_request holds the texts)."""
    config = default_config()
    config["characters"], config["default_character"] = {}, ""  # a pre-G1 config has no starter character
    config["affinity"] = {**stored_affinity_defaults(), "character": "藤田琴音", "display_name": "琴音"}
    config["song_request"].update(
        {"accept_texts": ORIGINAL_TEXTS["accept_texts"], "refuse_texts": ORIGINAL_TEXTS["refuse_texts"],
         "fail_texts": ORIGINAL_TEXTS["fail_texts"], "limit_text": ORIGINAL_TEXTS["limit_text"]}
    )
    del config["texts"]
    config.update(changes)
    return config


class ReplyTextsTests(unittest.TestCase):
    def test_defaults_with_the_default_nickname_are_word_for_word_what_was_hard_coded(self):
        defaults = reply_texts.default_texts()
        self.assertEqual(set(defaults), set(ORIGINAL_TEXTS) | set(LATER_TEXTS))
        self.assertEqual(defaults["owner_claimed"], LATER_TEXTS["owner_claimed"])
        for key, original in ORIGINAL_TEXTS.items():
            if isinstance(original, list):
                self.assertEqual([reply_texts.fill_nickname(item, "琴音") for item in defaults[key]], original, key)
            else:
                self.assertEqual(reply_texts.fill_nickname(defaults[key], "琴音"), original, key)
        # only the one line that named the character carries the placeholder
        self.assertEqual([key for key, value in defaults.items() if "{昵称}" in json.dumps(value, ensure_ascii=False)], ["unavailable", "queue_full"])

    def test_the_named_constants_in_the_code_equal_the_old_literals(self):
        module = _load_plugin_module()
        for name, key in (
            ("INVALID_REPLY_TEXT", "invalid_reply"), ("TOO_LONG_TEXT", "too_long"), ("UNAVAILABLE_TEXT", "unavailable"),
            ("QUEUE_FULL_TEXT", "queue_full"), ("QUEUE_TIMEOUT_TEXT", "queue_timeout"), ("SONG_BUSY_TEXT", "song_busy"),
            ("SONG_QUEUE_FULL_TEXT", "song_queue_full"), ("SONG_USAGE_TEXT", "song_usage"), ("SONG_CANCEL_TEXT", "song_cancel"),
            ("SONG_RESEARCH_HINT", "song_research_hint"), ("SONG_RESEARCH_EMPTY_TEXT", "song_research_empty"),
            ("SONG_PICK_RANGE_TEXT", "song_pick_range"),
        ):
            self.assertEqual(getattr(module, name), ORIGINAL_TEXTS[key], name)

    def test_meta_lists_every_text_with_its_placeholders(self):
        meta = reply_texts.text_meta()
        self.assertEqual([item["key"] for item in meta], [*ORIGINAL_TEXTS, *LATER_TEXTS])
        by_key = {item["key"]: item for item in meta}
        self.assertEqual(by_key["accept_texts"]["placeholders"], ["{昵称}", "{歌名}"])
        self.assertEqual(by_key["limit_text"]["placeholders"], ["{昵称}", "{次数}"])
        self.assertEqual(by_key["queue_full"]["placeholders"], ["{昵称}"])
        self.assertEqual({item["group"] for item in meta}, {"排队与出错", "点歌", "认领主人"})
        self.assertEqual(by_key["fail_texts"]["kind"], "lines")

    def test_unknown_placeholders_are_rejected_and_named(self):
        with self.assertRaises(EmotionConfigError) as caught:
            reply_texts.validate_texts({"queue_full": "现在找{昵名}的人太多啦"})
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("queue_full", caught.exception.message)
        self.assertIn("{昵名}", caught.exception.message)
        # {歌名} is only for the song lines, {次数} only for the limit line
        for key, value in (("queue_full", "{歌名}"), ("song_usage", "{次数}"), ("limit_text", "{歌名}"), ("accept_texts", ["{次数}"])):
            with self.assertRaises(EmotionConfigError, msg=key):
                reply_texts.validate_texts({key: value})
        for bad in ("{}", "{ 昵称 }", "{昵称"[:2] + "}"):
            with self.assertRaises(EmotionConfigError, msg=bad):
                reply_texts.validate_texts({"too_long": bad})

    def test_allowed_placeholders_and_other_braces_in_plain_text(self):
        good = reply_texts.validate_texts({
            "queue_full": "{昵称}很忙", "accept_texts": ["《{歌名}》给{昵称}", "好"], "limit_text": "{昵称}：今天{次数}首",
            "song_usage": "  用法  ",
        })
        self.assertEqual(good["queue_full"], "{昵称}很忙")
        self.assertEqual(good["song_usage"], "用法")
        self.assertEqual(good["accept_texts"], ["《{歌名}》给{昵称}", "好"])
        # a missing key takes its default
        self.assertEqual(good["too_long"], ORIGINAL_TEXTS["too_long"])

    def test_length_and_count_rules(self):
        for bad in ({"too_long": ""}, {"too_long": "x" * 101}, {"too_long": 5}, {"accept_texts": []},
                    {"accept_texts": ["ok"] * 11}, {"accept_texts": "text"}, {"fail_texts": [""]}, {"refuse_texts": ["x" * 101]}):
            with self.assertRaises(EmotionConfigError, msg=str(bad)):
                reply_texts.validate_texts(bad)
        with self.assertRaises(EmotionConfigError):
            reply_texts.validate_texts([1])
        self.assertEqual(reply_texts.validate_texts({"too_long": "x" * 100})["too_long"], "x" * 100)
        # unknown keys are dropped, not stored
        self.assertNotIn("nope", reply_texts.validate_texts({"nope": "x"}))

    def test_fill_helpers_are_plain_replaces(self):
        self.assertEqual(reply_texts.fill_nickname("找{昵称}{x}", "小夜"), "找小夜{x}")
        self.assertEqual(reply_texts.fill_nickname("找{昵称}", "{0}"), "找{0}")
        self.assertEqual(reply_texts.fill_song_name("《{歌名}》", "晴天"), "《晴天》")
        self.assertEqual(reply_texts.fill_limit("{次数}首", 5), "5首")
        self.assertEqual(reply_texts.texts_settings({}), reply_texts.default_texts())
        self.assertEqual(reply_texts.texts_settings({"texts": {"too_long": "{坏}"}}), reply_texts.default_texts())


class IdentityTests(unittest.TestCase):
    def test_nickname_rules(self):
        self.assertEqual(validate_nickname("  小夜  "), "小夜")
        self.assertEqual(validate_nickname("a" * 20), "a" * 20)
        for bad in ("", "   ", "a" * 21, "小 夜", "小\n夜", "小{夜}", "小}", 5, None):
            with self.assertRaises(EmotionConfigError, msg=repr(bad)):
                validate_nickname(bad)

    def test_st_name_is_one_stat_data_key(self):
        self.assertEqual(validate_st_name("藤田琴音"), "藤田琴音")
        for bad in ("", "a.b", "a b", "x" * 41, None):
            with self.assertRaises(EmotionConfigError, msg=repr(bad)):
                validate_st_name(bad)
        self.assertEqual(default_st_name("Role 1-A"), "Role1A")
        self.assertEqual(default_st_name("!!!"), "角色")
        self.assertEqual(default_nickname("小 夜{x}"), "小夜x")
        self.assertEqual(default_nickname("   "), "角色")

    def test_active_identity_of_the_default_character_then_the_first_then_the_defaults(self):
        config = {
            "default_character": "B",
            "characters": {
                "A": {"nickname": "甲", "st_name": "角A"},
                "B": {"nickname": "乙", "st_name": "角B"},
            },
        }
        self.assertEqual(active_identity(config), {"st_name": "角B", "nickname": "乙"})
        config["default_character"] = ""
        self.assertEqual(active_identity(config), {"st_name": "角A", "nickname": "甲"})
        config["default_character"] = "gone"
        self.assertEqual(active_identity(config)["nickname"], "甲")
        # a character without the fields (written by hand): names derived from its own name
        self.assertEqual(active_identity({"default_character": "Role 1", "characters": {"Role 1": {}}}), {"st_name": "Role1", "nickname": "Role1"})
        # no character: the legacy affinity names, else the built-in defaults
        self.assertEqual(active_identity({"characters": {}, "affinity": {"character": "老名", "display_name": "老昵"}}), {"st_name": "老名", "nickname": "老昵"})
        self.assertEqual(active_identity({}), {"st_name": "藤田琴音", "nickname": "琴音"})
        self.assertEqual(active_identity(None), {"st_name": "藤田琴音", "nickname": "琴音"})

    def test_affinity_settings_take_the_names_from_the_active_character(self):
        config = default_config()
        config["characters"] = {"小夜": {"name": "小夜", "model_name": "", "default_emotion": "neutral", "emotions": {}, "nickname": "夜夜", "st_name": "小夜"}}
        config["default_character"] = "小夜"
        settings = affinity_settings(config)
        self.assertEqual((settings["character"], settings["display_name"]), ("小夜", "夜夜"))
        self.assertEqual(affinity_track(settings, "owner")["path"], ["小夜", "好感度"])
        self.assertNotIn("character", config["affinity"])
        validate_config(config)

    def test_new_configs_do_not_store_the_names_in_affinity(self):
        config = default_config()
        self.assertNotIn("character", config["affinity"])
        self.assertNotIn("display_name", config["affinity"])
        self.assertEqual(config["texts"], reply_texts.default_texts())


class MigrationTests(unittest.TestCase):
    def write(self, root, config):
        (root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def characters(self):
        return {
            "藤田琴音": {"name": "藤田琴音", "model_name": "", "default_emotion": "neutral", "emotions": {}},
            "路人": {"name": "路人", "model_name": "", "default_emotion": "neutral", "emotions": {}},
        }

    def test_names_move_to_the_characters_and_song_texts_to_texts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = ["自己写的《{歌名}》", "第二句"]
            config = pre_g1_config(characters=self.characters(), default_character="路人")
            config["affinity"].update({"character": "我的角色", "display_name": "小路"})
            config["song_request"]["accept_texts"] = custom
            config["song_request"]["limit_text"] = "满了{次数}"
            self.write(root, config)
            original_bytes = (root / "config.json").read_bytes()

            store = ConfigStore(root)
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
            for loaded in (store.config, saved):
                # the default character carries the old affinity names, the others their own name
                self.assertEqual(loaded["characters"]["路人"]["st_name"], "我的角色")
                self.assertEqual(loaded["characters"]["路人"]["nickname"], "小路")
                self.assertEqual(loaded["characters"]["藤田琴音"]["st_name"], "藤田琴音")
                self.assertEqual(loaded["characters"]["藤田琴音"]["nickname"], "藤田琴音")
                self.assertNotIn("character", loaded["affinity"])
                self.assertNotIn("display_name", loaded["affinity"])
                # the user's own text arrives unchanged; the rest of the section is the defaults
                self.assertEqual(loaded["texts"]["accept_texts"], custom)
                self.assertEqual(loaded["texts"]["limit_text"], "满了{次数}")
                self.assertEqual(loaded["texts"]["fail_texts"], ORIGINAL_TEXTS["fail_texts"])
                self.assertEqual(loaded["texts"]["queue_full"], reply_texts.default_texts()["queue_full"])
                for field in ("accept_texts", "refuse_texts", "fail_texts", "limit_text"):
                    self.assertNotIn(field, loaded["song_request"])
            # the file as it was before the migration is kept
            self.assertEqual((root / "config.json.bak-g1").read_bytes(), original_bytes)
            self.assertEqual(affinity_settings(store.config)["character"], "我的角色")
            self.assertEqual(song_request_settings(store.config)["accept_texts"], custom)

    def test_a_second_load_changes_nothing_and_keeps_the_first_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, pre_g1_config(characters=self.characters(), default_character="藤田琴音"))
            ConfigStore(root)
            backup = (root / "config.json.bak-g1").read_bytes()
            migrated = (root / "config.json").read_bytes()
            ConfigStore(root)
            self.assertEqual((root / "config.json.bak-g1").read_bytes(), backup)
            self.assertEqual((root / "config.json").read_bytes(), migrated)

    def test_an_existing_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, pre_g1_config(characters=self.characters(), default_character="藤田琴音"))
            (root / "config.json.bak-g1").write_text("an older backup", encoding="utf-8")
            store = ConfigStore(root)
            self.assertEqual((root / "config.json.bak-g1").read_text(encoding="utf-8"), "an older backup")
            self.assertEqual(store.config["characters"]["藤田琴音"]["nickname"], "琴音")

    def test_without_characters_the_affinity_names_stay_and_only_the_texts_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = pre_g1_config()
            config["affinity"].update({"character": "旧名", "display_name": "旧昵"})
            self.write(root, config)
            store = ConfigStore(root)
            self.assertEqual(store.config["affinity"]["character"], "旧名")
            self.assertEqual(active_identity(store.config), {"st_name": "旧名", "nickname": "旧昵"})
            self.assertEqual(store.config["texts"]["accept_texts"], ORIGINAL_TEXTS["accept_texts"])
            self.assertTrue((root / "config.json.bak-g1").exists())

    def test_a_config_that_needs_no_migration_is_left_alone_and_gets_no_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config()
            config["characters"] = {"A": {"name": "A", "model_name": "", "default_emotion": "neutral", "emotions": {}, "nickname": "甲", "st_name": "角A"}}
            config["default_character"] = "A"
            self.write(root, config)
            before = (root / "config.json").read_bytes()
            ConfigStore(root)
            self.assertEqual((root / "config.json").read_bytes(), before)
            self.assertFalse((root / "config.json.bak-g1").exists())
            self.assertFalse(migrate_g1(json.loads(before)))

    def test_a_config_older_than_the_texts_section_gets_the_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config()
            del config["texts"], config["song_request"]
            self.write(root, config)
            store = ConfigStore(root)
            self.assertEqual(store.config["texts"], reply_texts.default_texts())
            self.assertEqual(song_request_settings(store.config)["limit_text"], ORIGINAL_TEXTS["limit_text"])

    def test_the_whole_pre_g1_behaviour_is_unchanged_for_an_untouched_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, pre_g1_config(characters=self.characters(), default_character="藤田琴音"))
            store = ConfigStore(root)
            # the character in use before: ST name 藤田琴音 and nickname 琴音 (the old affinity defaults)
            self.assertEqual(active_identity(store.config), {"st_name": "藤田琴音", "nickname": "琴音"})
            texts = reply_texts.texts_settings(store.config)
            for key, original in ORIGINAL_TEXTS.items():
                if isinstance(original, list):
                    self.assertEqual([reply_texts.fill_nickname(item, "琴音") for item in texts[key]], original)
                else:
                    self.assertEqual(reply_texts.fill_nickname(texts[key], "琴音"), original)
            settings = affinity_settings(store.config)
            self.assertEqual((settings["character"], settings["display_name"]), ("藤田琴音", "琴音"))
            self.assertEqual(settings["group"]["role_tag"], "【群友】")
            self.assertEqual(song_request_settings(store.config)["permission_variable"], "点歌许可")


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


class UpgradeWithOddTextsTests(unittest.TestCase):
    """Song texts written before G1 were never checked for braces: the upgrade must not fail on them."""

    ODD = {
        "accept_texts": ["好{笑}《{歌名}》", "第二句"],  # {笑} is not a placeholder
        "refuse_texts": ["不行《{歌名}》"],  # refusals do not know the song
        "fail_texts": ["x" * 101],  # too long
        "limit_text": "今天{次数}首满了{昵称}",  # fine
    }

    def write_odd(self, root):
        config = pre_g1_config(characters={"琴": {"name": "琴", "model_name": "", "default_emotion": "neutral", "emotions": {}}}, default_character="琴")
        config["song_request"].update(self.ODD)
        (root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return (root / "config.json").read_bytes()

    def test_loading_succeeds_and_only_the_bad_texts_become_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.write_odd(root)
            logger = RecordingLogger()
            store = ConfigStore(root, logger=logger)
            texts = store.config["texts"]
            defaults = reply_texts.default_texts()
            self.assertEqual(texts["accept_texts"], defaults["accept_texts"])
            self.assertEqual(texts["refuse_texts"], defaults["refuse_texts"])
            self.assertEqual(texts["fail_texts"], defaults["fail_texts"])
            self.assertEqual(texts["limit_text"], "今天{次数}首满了{昵称}")  # the good one is carried over
            self.assertEqual(sorted(store.config["texts_notice"]["replaced"]), ["accept_texts", "fail_texts", "refuse_texts"])
            self.assertEqual(store.config["texts_notice"]["backup"], "config.json.bak-g1")
            # the file as it was is kept
            self.assertEqual((root / "config.json.bak-g1").read_bytes(), original)
            # one warning per replaced text: key and reason, never the wording
            self.assertEqual(len(logger.warnings), 3)
            joined = "\n".join(logger.warnings)
            for key, reason in (("accept_texts", "unknown_placeholder"), ("refuse_texts", "unknown_placeholder"), ("fail_texts", "length")):
                self.assertIn(f"key={key} reason={reason}", joined)
            for secret in ("笑", "不行", "第二句", "xxx"):
                self.assertNotIn(secret, joined)
            # the written file loads again without a second migration
            again = ConfigStore(root, logger=RecordingLogger())
            self.assertEqual(again.config["texts"], texts)
            self.assertEqual((root / "config.json.bak-g1").read_bytes(), original)

    def test_a_store_without_a_logger_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_odd(root)
            self.assertEqual(ConfigStore(root).config["texts"]["refuse_texts"], reply_texts.default_texts()["refuse_texts"])

    def test_whatever_the_old_texts_hold_the_upgrade_does_not_fail(self):
        bad_lines = ([""], [5], [], "text", None, ["a"] * 11, [" "], ["{x}"], ["{}"])
        bad_text = (None, "", 5, ["a"], "x" * 101, "{x}", "{歌名}")
        cases = [(field, bad) for field in ("accept_texts", "refuse_texts", "fail_texts") for bad in bad_lines]
        cases += [("limit_text", bad) for bad in bad_text]
        for field, bad in cases:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = pre_g1_config(characters={"琴": {"name": "琴", "model_name": "", "default_emotion": "neutral", "emotions": {}}}, default_character="琴")
                config["song_request"][field] = bad
                (root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                store = ConfigStore(root, logger=RecordingLogger())
                self.assertEqual(store.config["texts"][field], reply_texts.default_texts()[field], (field, bad))
                self.assertEqual(store.config["texts_notice"]["replaced"], [field], (field, bad))

    def test_without_odd_texts_there_is_no_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = pre_g1_config(characters={"琴": {"name": "琴", "model_name": "", "default_emotion": "neutral", "emotions": {}}}, default_character="琴")
            (root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            logger = RecordingLogger()
            store = ConfigStore(root, logger=logger)
            self.assertNotIn("texts_notice", store.config)
            self.assertEqual(logger.warnings, [])

    def test_texts_are_read_one_by_one(self):
        config = {"texts": {"queue_full": "{昵称}正忙", "too_long": "{坏}", "accept_texts": ["好《{歌名}》", "{笑}"], "song_usage": 5, "fail_texts": []}}
        texts = reply_texts.texts_settings(config)
        defaults = reply_texts.default_texts()
        self.assertEqual(texts["queue_full"], "{昵称}正忙")  # the good one stays
        self.assertEqual(texts["too_long"], defaults["too_long"])
        self.assertEqual(texts["accept_texts"], defaults["accept_texts"])
        self.assertEqual(texts["song_usage"], defaults["song_usage"])
        self.assertEqual(texts["fail_texts"], defaults["fail_texts"])
        self.assertEqual(texts["limit_text"], defaults["limit_text"])
        self.assertEqual(reply_texts.texts_settings({"texts": "broken"}), defaults)
        self.assertEqual(reply_texts.texts_settings(None), defaults)
        texts, replaced = reply_texts.lenient_texts(config["texts"])
        self.assertEqual(sorted(replaced), [("accept_texts", "unknown_placeholder"), ("fail_texts", "count"), ("song_usage", "type"), ("too_long", "unknown_placeholder")])

    def test_a_config_with_an_odd_texts_section_still_validates(self):
        config = default_config()
        config["texts"] = {"queue_full": "{坏}", "accept_texts": "nope"}
        validate_config(config)
        config["texts"] = "garbage"
        validate_config(config)
        self.assertEqual(reply_texts.texts_settings(config), reply_texts.default_texts())


class ConsoleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConfigStore(Path(self.directory.name))
        self.store.config["characters"], self.store.config["default_character"] = {}, ""  # these tests start without the starter character
        self.port = _unused_loopback_port()
        self.service = ConsoleService(create_console_app(self.store, wake_prefix=lambda: ["/", "~"]), port=self.port)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", payload=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", json=payload) as response:
            return response.status, await response.json()

    async def test_a_character_carries_a_nickname_and_an_st_name(self):
        status, body = await self.call("/api/characters", "POST", {"name": "小夜", "nickname": "夜夜", "st_name": "XiaoYe"})
        self.assertEqual(status, 200)
        self.assertEqual((body["character"]["nickname"], body["character"]["st_name"]), ("夜夜", "XiaoYe"))
        # both default to the character's own name, and keep their value when an edit leaves them out
        status, body = await self.call("/api/characters", "POST", {"name": "路人"})
        self.assertEqual((body["character"]["nickname"], body["character"]["st_name"]), ("路人", "路人"))
        status, body = await self.call("/api/characters", "POST", {"name": "小夜", "model_name": ""})
        self.assertEqual((body["character"]["nickname"], body["character"]["st_name"]), ("夜夜", "XiaoYe"))
        settings = affinity_settings(self.store.config)
        self.assertEqual((settings["character"], settings["display_name"]), ("XiaoYe", "夜夜"))

    async def test_bad_nicknames_and_st_names_are_400(self):
        for payload in ({"nickname": "小 夜"}, {"nickname": ""}, {"nickname": "x" * 21}, {"nickname": "{昵称}"}, {"st_name": "a.b"}, {"st_name": ""}):
            status, _ = await self.call("/api/characters", "POST", {"name": "小夜", **payload})
            self.assertEqual(status, 400, str(payload))
        self.assertNotIn("小夜", self.store.config["characters"])

    async def test_affinity_settings_never_store_the_names(self):
        await self.call("/api/characters", "POST", {"name": "小夜", "nickname": "夜夜", "st_name": "小夜"})
        status, body = await self.call("/api/affinity/settings", "POST", {"character": "别的", "display_name": "别的", "max_delta": 3})
        self.assertEqual(status, 200)
        self.assertEqual((body["settings"]["character"], body["settings"]["display_name"]), ("小夜", "夜夜"))
        self.assertNotIn("character", self.store.config["affinity"])
        self.assertNotIn("display_name", self.store.config["affinity"])
        self.assertEqual(self.store.config["affinity"]["max_delta"], 3)

    async def test_role_tag_and_permission_variable_are_validated(self):
        status, body = await self.call("/api/affinity/settings", "POST", {"group": {"role_tag": "【粉丝】"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["affinity"]["group"]["role_tag"], "【粉丝】")
        for tag in ("", "x" * 11, "a\nb"):
            status, _ = await self.call("/api/affinity/settings", "POST", {"group": {"role_tag": tag}})
            self.assertEqual(status, 400, repr(tag))
        status, _ = await self.call("/api/cover/song-request/settings", "POST", {"permission_variable": "点歌权限"})
        self.assertEqual(status, 503)  # no cover service in this app; the setting itself is covered by the validator
        from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.song_request import validate_song_request_settings
        self.assertEqual(validate_song_request_settings({"permission_variable": " 点歌权限 "})["permission_variable"], "点歌权限")
        for name in ("", "a.b", "a[0]", "a]", "x" * 41, 5):
            with self.assertRaises(EmotionConfigError, msg=repr(name)):
                validate_song_request_settings({"permission_variable": name})

    async def test_texts_endpoints(self):
        status, body = await self.call("/api/texts")
        self.assertEqual(status, 200)
        self.assertEqual(body["texts"], reply_texts.default_texts())
        self.assertEqual(body["defaults"], reply_texts.default_texts())
        self.assertEqual(body["nickname"], "琴音")
        self.assertEqual(len(body["meta"]), 17)
        edited = {**body["texts"], "queue_full": "{昵称}在忙", "fail_texts": ["a", "b"]}
        status, saved = await self.call("/api/texts", "POST", edited)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.config["texts"], saved["texts"])
        self.assertEqual(ConfigStore(Path(self.directory.name)).config["texts"]["queue_full"], "{昵称}在忙")
        status, body = await self.call("/api/texts", "POST", {**edited, "queue_full": "{昵名}在忙"})
        self.assertEqual(status, 400)
        self.assertIn("queue_full", body["error"])
        self.assertIn("{昵名}", body["error"])
        self.assertEqual(self.store.config["texts"]["queue_full"], "{昵称}在忙")

    async def test_the_upgrade_notice_is_shown_until_the_texts_are_saved(self):
        self.store.config["texts_notice"] = {"replaced": ["refuse_texts"], "backup": "config.json.bak-g1"}
        status, body = await self.call("/api/texts")
        self.assertEqual(body["notice"], {"replaced": ["refuse_texts"], "backup": "config.json.bak-g1"})
        status, _ = await self.call("/api/texts", "POST", body["texts"])
        self.assertEqual(status, 200)
        self.assertNotIn("texts_notice", self.store.config)
        status, body = await self.call("/api/texts")
        self.assertIsNone(body["notice"])

    async def test_wake_prefix_is_reported_with_the_status(self):
        status, body = await self.call("/api/status")
        self.assertEqual((status, body["wake_prefix"]), (200, ["/", "~"]))

    async def test_a_failing_provider_reports_nothing(self):
        def broken():
            raise RuntimeError("no config")

        service = ConsoleService(create_console_app(self.store, wake_prefix=broken), port=_unused_loopback_port())
        await service.start()
        try:
            async with self.client.get(f"http://127.0.0.1:{service._port}/api/status") as response:
                self.assertEqual((await response.json())["wake_prefix"], None)
        finally:
            await service.stop()


class PluginTextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.module = _load_plugin_module()
        self.plugin = _ready_plugin(self.module)
        self.store = ConfigStore(Path(self.directory.name))
        self.plugin._core._emotion_store = self.store

    def use_character(self, nickname="小夜", st_name="XiaoYe"):
        self.store.config["characters"] = {"小夜": {"name": "小夜", "model_name": "", "default_emotion": "neutral", "emotions": {}, "nickname": nickname, "st_name": st_name}}
        self.store.config["default_character"] = "小夜"

    def test_reply_texts_come_from_the_config_with_the_nickname_put_in(self):
        self.assertEqual(self.plugin._core._reply_text("queue_full"), ORIGINAL_TEXTS["queue_full"])
        self.use_character("小夜")
        self.assertEqual(self.plugin._core._reply_text("queue_full"), "现在找小夜的人太多啦，请稍后再来～")
        self.store.config["texts"]["song_usage"] = "{昵称}说：用法 唱 歌名"
        self.assertEqual(self.plugin._core._reply_text("song_usage"), "小夜说：用法 唱 歌名")
        self.assertEqual(self.plugin._core._reply_text("too_long"), ORIGINAL_TEXTS["too_long"])

    def test_song_lines_fill_the_nickname_the_song_name_and_the_limit(self):
        self.use_character("小夜")
        self.store.config["texts"]["accept_texts"] = ["{昵称}来唱《{歌名}》"]
        self.store.config["texts"]["limit_text"] = "{昵称}说今天{次数}首满了"
        self.plugin._core._song_choice = lambda items: items[0]
        self.assertEqual(self.plugin._core._song_text("accept_texts", "晴天"), "小夜来唱《晴天》")
        self.store.config["song_request"]["daily_limit"] = 7
        self.assertEqual(self.plugin._core._song_refusal_text("quota", "晴天"), "小夜说今天7首满了")

    def test_the_song_permission_variable_and_group_tag_are_configurable(self):
        self.assertEqual(self.plugin._core._song_permission_path(), ["点歌许可"])
        self.assertEqual(self.plugin._core._group_role_tag(), "【群友】")
        self.store.config["song_request"]["permission_variable"] = "点歌权限"
        self.store.config["affinity"]["group"]["role_tag"] = "【粉丝】"
        self.assertEqual(self.plugin._core._song_permission_path(), ["点歌权限"])
        self.assertEqual(self.plugin._core._group_role_tag(), "【粉丝】")
        origin = self.module._Origin("p", "u", "小明", group_label="小明")
        speaker = self.module._Speaker("k", "小明", "group", 3)
        self.assertEqual(self.plugin._core._compose_content(origin, speaker, "你好"), "【群友】小明[好感度:3]：你好")
        self.assertEqual(self.plugin._core._compose_content(origin, speaker, "你好", self.plugin._core._group_role_tag()), "【粉丝】小明[好感度:3]：你好")

    def test_wake_prefixes_are_read_from_astrbots_top_level_setting(self):
        self.assertIsNone(self.plugin._platform.wake_prefixes())  # no context in the test double
        self.plugin.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/", "~"], "provider_settings": {"wake_prefix": "llm!"}})
        self.assertEqual(self.plugin._platform.wake_prefixes(), ["/", "~"])
        self.plugin.context = SimpleNamespace(get_config=lambda: {"wake_prefix": "!"})
        self.assertEqual(self.plugin._platform.wake_prefixes(), ["!"])

        def broken():
            raise RuntimeError("boom")

        self.plugin.context = SimpleNamespace(get_config=broken)
        self.assertIsNone(self.plugin._platform.wake_prefixes())
        self.plugin.context = SimpleNamespace(get_config=lambda: {})
        self.assertIsNone(self.plugin._platform.wake_prefixes())

    def sing_name(self, text):
        return self.plugin._core._sing_command_name(SimpleNamespace(message_str=text, wake_prefixes=self.plugin._platform.wake_prefixes()))

    def test_the_sing_command_drops_the_wake_prefix_and_the_nickname(self):
        # default: "/" and the default nickname, as before
        for text in ("唱 晴天", "/唱 晴天", "/ 唱 晴天", "/琴音 唱 晴天", "琴音唱 晴天", "  /琴音  唱   晴天  "):
            self.assertEqual(self.sing_name(text), "晴天", text)
        self.assertEqual(self.sing_name("唱"), "")
        self.assertEqual(self.sing_name("唱 唱歌的人"), "唱歌的人")
        self.use_character("小夜")
        self.assertEqual(self.sing_name("/小夜 唱 晴天"), "晴天")
        self.assertEqual(self.sing_name("/琴音 唱 晴天"), "/琴音 唱 晴天")  # the old nickname no longer counts
        self.assertEqual(self.sing_name("唱 小夜曲"), "小夜曲")

    def test_several_wake_prefixes_longest_first_and_special_characters_in_the_nickname(self):
        self.plugin.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/", "/!", "!"]})
        for text in ("/!小夜 唱 晴天", "!小夜唱 晴天", "/小夜 唱 晴天", "小夜 唱 晴天"):
            self.use_character("小夜")
            self.assertEqual(self.sing_name(text), "晴天", text)
        self.use_character("a.b*(c)")
        self.plugin.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/", "%+", "["]})
        for text in ("/a.b*(c) 唱 晴天", "%+a.b*(c)唱 晴天", "[a.b*(c) 唱 晴天"):
            self.assertEqual(self.sing_name(text), "晴天", text)
        # the nickname is matched literally, not as a pattern
        self.assertEqual(self.sing_name("/axb 唱 晴天"), "/axb 唱 晴天")
        # unreadable setting: "/" as before
        self.plugin.context = None
        self.assertEqual(self.sing_name("/a.b*(c) 唱 晴天"), "晴天")


if __name__ == "__main__":
    unittest.main()

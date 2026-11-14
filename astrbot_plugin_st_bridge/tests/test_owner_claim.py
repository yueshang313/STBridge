"""101a-E: claiming the owner with a one-time 6-digit code (AstrBot and NapCat paths, console API)."""

import tempfile
import unittest
from pathlib import Path

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core import reply_texts
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.chat_platform import NAPCAT_PLATFORM_ID, chat_platform_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.connector_hub import RemotePlatform
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.console_server import ConsoleService, create_console_app
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.core import BridgeCore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.emotion_config import ConfigStore
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.owner_claim import (
    CLAIM_TTL_SECONDS,
    OwnerClaim,
    claim_candidate,
    mask_id,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.owner_config import owner_settings
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.platform_api import GROUP, PRIVATE, IncomingMessage
from test_onebot import OWNER, STRANGER, OneBotCase, at_segment, group_event, private_event, text_segment
from test_owner_and_logs import free_port

CLAIMED_TEXT = "认领成功，以后你就是主人啦。"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ClaimLogicTests(unittest.TestCase):
    def test_codes_are_six_digits_single_use_and_a_new_one_voids_the_old_one(self):
        claim = OwnerClaim()
        first = claim.start()
        self.assertRegex(first["code"], r"^\d{6}$")
        self.assertEqual(first["expires_in"], CLAIM_TTL_SECONDS)
        second = claim.start()
        if second["code"] != first["code"]:
            self.assertFalse(claim.consume(first["code"]))
        self.assertTrue(claim.consume(second["code"]))
        self.assertFalse(claim.consume(second["code"]))  # one use only
        self.assertEqual(claim.status(), {"state": "idle"})

    def test_a_code_expires_after_five_minutes(self):
        clock = FakeClock()
        claim = OwnerClaim(clock=clock)
        code = claim.start()["code"]
        clock.now += CLAIM_TTL_SECONDS - 1
        status = claim.status()
        self.assertEqual((status["state"], status["code"], status["expires_in"]), ("pending", code, 1))
        clock.now += 1
        self.assertFalse(claim.consume(code))
        self.assertEqual(claim.status(), {"state": "expired"})

    def test_what_counts_as_the_code(self):
        prefixes = ["/琴音", "琴音", "/"]
        for text in ("123456", "  123456\n", "/123456", "/ 123456", "/琴音 123456", "琴音123456", "/琴音123456"):
            with self.subTest(text=text):
                self.assertEqual(claim_candidate(text, prefixes), "123456")
        for text in ("12345", "1234567", "123 456", "我是 123456", "123456 谢谢", "１２３４５６", "abcdef", "", None, "//123456x"):
            with self.subTest(text=text):
                self.assertIsNone(claim_candidate(text, prefixes))

    def test_ids_are_masked(self):
        self.assertEqual(mask_id("12345678901"), "12******01")
        self.assertEqual(mask_id("AB"), "**")
        self.assertNotIn("abcdef0123456789", mask_id("abcdef0123456789abcdef0123456789"))
        self.assertEqual(mask_id("default_1234567890"), "de******90")

    def test_the_default_reply_text_is_editable_config(self):
        self.assertEqual(reply_texts.default_texts()["owner_claimed"], CLAIMED_TEXT)
        self.assertIn("owner_claimed", reply_texts.TEXT_KEYS)
        self.assertEqual(reply_texts.validate_texts({"owner_claimed": "好的，{昵称}记住你了"})["owner_claimed"], "好的，{昵称}记住你了")


class AstrBotClaimCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = FakeClock()
        self.core = BridgeCore(RemotePlatform(), lambda: self.root)
        self.core._emotion_store = ConfigStore(self.root)
        self.core._owner_claim = OwnerClaim(clock=self.clock)
        self.touched = []
        self.core._touch_conversation = lambda *args, **kwargs: self.touched.append(args)

    def message(self, text, *, sender="openid-stranger", platform_id="default_1234567890", chat=PRIVATE, prefixes=("/",)):
        return IncomingMessage(
            platform="qq_official", platform_id=platform_id, chat_type=chat, session_id=sender,
            conversation_id=f"{platform_id}:FriendMessage:{sender}", sender_id=sender, sender_name="n",
            group_id="g" if chat == GROUP else None, text=text, message_str=text, wake_prefixes=list(prefixes),
        )

    async def run_private(self, msg):
        """What the plugin does: accept_private, then handle_private for an accepted message."""
        if not self.core.accept_private(msg):
            return None
        return [batch async for batch in self.core.handle_private(msg)]

    def owner(self):
        return owner_settings(self.core._emotion_store.config)

    async def test_claiming_sets_the_owner_and_answers_with_the_fixed_text(self):
        code = self.core.owner_claim_start()["code"]
        batches = await self.run_private(self.message(code))
        self.assertEqual([batch.segments[0].text for batch in batches], [CLAIMED_TEXT])
        self.assertEqual(self.owner(), {"platform_id": "default_1234567890", "sender_id": "openid-stranger", "imported": True})
        status = self.core.owner_claim_status()
        self.assertEqual(status["state"], "claimed")
        self.assertEqual(status["current"]["masked_id"], "op******er")
        self.assertNotIn("openid-stranger", str(status))
        # The new owner is the owner right away.
        self.assertTrue(self.core.accept_private(self.message("你好")))
        self.assertEqual(self.touched, [])  # no activity, no counters

    async def test_the_code_works_once_and_a_second_message_is_an_ordinary_one(self):
        code = self.core.owner_claim_start()["code"]
        await self.run_private(self.message(code))
        self.assertFalse(self.core.accept_private(self.message(code, sender="someone-else")))
        self.assertEqual(self.owner()["sender_id"], "openid-stranger")

    async def test_an_existing_owner_is_replaced(self):
        await self.core._emotion_store.update(lambda config: config.update(owner={"platform_id": "old", "sender_id": "old-me", "imported": True}))
        started = self.core.owner_claim_start()
        self.assertTrue(started["replaces"])
        await self.run_private(self.message(started["code"], sender="new-me", platform_id="new-platform"))
        self.assertEqual(self.owner(), {"platform_id": "new-platform", "sender_id": "new-me", "imported": True})
        self.assertFalse(self.core.accept_private(self.message("你好", sender="old-me", platform_id="old")))

    async def test_the_codes_with_slash_and_wake_word_prefixes_work(self):
        for text_of in (lambda code: f"/{code}", lambda code: f"/琴音 {code}", lambda code: f"  {code}\n"):
            with self.subTest(form=text_of("000000")):
                core = BridgeCore(RemotePlatform(), lambda: self.root)
                core._emotion_store = ConfigStore(self.root)
                code = core.owner_claim_start()["code"]
                msg = self.message(text_of(code), prefixes=("/琴音", "/"))
                self.assertTrue(core.accept_private(msg))

    async def test_wrong_digits_and_other_text_are_ordinary_messages(self):
        code = self.core.owner_claim_start()["code"]
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        for text in (wrong, "12345", f"{code}1", f"我的验证码是{code}", "你好"):
            with self.subTest(text=text):
                self.assertFalse(self.core.accept_private(self.message(text)))
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")  # still waiting for the right one
        self.assertEqual(self.owner()["sender_id"], "")

    async def test_a_wrong_number_from_the_owner_is_normal_chat(self):
        await self.core._emotion_store.update(lambda config: config.update(owner={"platform_id": "p", "sender_id": "me", "imported": True}))
        code = self.core.owner_claim_start()["code"]
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        msg = self.message(wrong, sender="me", platform_id="p")
        self.assertTrue(self.core.accept_private(msg))
        self.assertIsNone(self.core._claim_take(msg))  # goes to ST like any message
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")

    async def test_group_messages_never_claim(self):
        code = self.core.owner_claim_start()["code"]
        self.assertFalse(self.core._claim_try(self.message(code, chat=GROUP)))
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")
        self.assertEqual(self.owner()["sender_id"], "")

    async def test_an_expired_code_does_nothing(self):
        code = self.core.owner_claim_start()["code"]
        self.clock.now += CLAIM_TTL_SECONDS
        self.assertFalse(self.core.accept_private(self.message(code)))
        self.assertEqual(self.core.owner_claim_status()["state"], "expired")

    async def test_a_restart_forgets_the_code(self):
        code = self.core.owner_claim_start()["code"]
        fresh = BridgeCore(RemotePlatform(), lambda: self.root)
        fresh._emotion_store = ConfigStore(self.root)
        self.assertFalse(fresh.accept_private(self.message(code)))

    async def test_nothing_is_claimable_without_pressing_the_button(self):
        self.assertFalse(self.core.accept_private(self.message("123456")))
        self.assertEqual(self.core.owner_claim_status()["state"], "idle")

    async def test_napcat_messages_do_not_claim_while_astrbot_is_active(self):
        code = self.core.owner_claim_start()["code"]
        msg = self.message(code, sender="30001", platform_id=NAPCAT_PLATFORM_ID)
        self.assertFalse(self.core.accept_private(msg))
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")

    async def test_a_leftover_claim_does_not_swallow_a_later_ordinary_message(self):
        code = self.core.owner_claim_start()["code"]
        msg = self.message(code)
        self.assertTrue(self.core.accept_private(msg))  # accepted but never handled (the plugin dropped it)
        later = self.message("你好", sender="openid-stranger")
        self.assertIsNone(self.core._claim_take(later))

    async def test_a_failed_write_leaves_the_owner_alone_and_says_nothing(self):
        code = self.core.owner_claim_start()["code"]

        async def broken(callback):
            raise RuntimeError("disk full")

        self.core._emotion_store.update = broken
        batches = await self.run_private(self.message(code))
        self.assertEqual(batches, [])
        self.assertEqual(self.owner()["sender_id"], "")
        self.assertEqual(self.core.owner_claim_status()["state"], "failed")


class NapCatClaimTests(OneBotCase):
    async def start_claim(self):
        return self.core.owner_claim_start()["code"]

    async def test_a_stranger_claims_napcat_ownership_and_gets_the_fixed_answer(self):
        code = await self.start_claim()
        await self.say(private_event(code, user=STRANGER))
        await self.wait_for(lambda: len(self.ws.actions) == 1)
        (action,) = self.ws.actions
        self.assertEqual((action["action"], action["params"]["user_id"]), ("send_private_msg", int(STRANGER)))
        self.assertEqual(action["params"]["message"], [{"type": "text", "data": {"text": CLAIMED_TEXT}}])
        self.assertEqual(chat_platform_settings(self.core._emotion_store.config)["napcat_owner"], STRANGER)
        self.assertEqual(chat_platform_settings(self.core._emotion_store.config)["group_whitelist"], ["30001"])
        self.assertIsNone(self.manager.peek_pending_request())  # nothing went to ST
        self.assertEqual(self.core.owner_claim_status()["state"], "claimed")
        # The old owner is a stranger now.
        await self.say(private_event("你好", user=OWNER))
        await self.quiet_after_claim()

    async def quiet_after_claim(self):
        self.assertIsNone(self.manager.peek_pending_request())
        self.assertEqual(len(self.ws.actions), 1)

    async def test_slash_and_nickname_prefixes_work(self):
        code = await self.start_claim()
        await self.say(private_event(f"/琴音 {code}", user=STRANGER))
        await self.wait_for(lambda: len(self.ws.actions) == 1)
        self.assertEqual(chat_platform_settings(self.core._emotion_store.config)["napcat_owner"], STRANGER)

    async def test_a_group_message_with_the_code_is_not_a_claim_and_does_not_reach_st(self):
        code = await self.start_claim()
        await self.say(group_event([at_segment("10000"), text_segment(f" {code}")], user=STRANGER))
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")
        self.assertEqual(chat_platform_settings(self.core._emotion_store.config)["napcat_owner"], OWNER)

    async def test_the_claim_message_never_reaches_st_even_for_the_new_owner(self):
        code = await self.start_claim()
        await self.say(private_event(code, user=OWNER))  # the current owner claims again: still just the fixed answer
        await self.wait_for(lambda: len(self.ws.actions) == 1)
        self.assertIsNone(self.manager.peek_pending_request())

    async def test_strangers_messages_stay_ignored_while_a_claim_waits(self):
        await self.start_claim()
        await self.say(private_event("你好", user=STRANGER))
        await self.say(private_event("123456", user=STRANGER))  # almost certainly not the code
        if self.core._owner_claim.status()["state"] == "pending":
            await self.quiet()


class AstrBotActiveClaimTests(OneBotCase):
    active = "astrbot"

    async def test_napcat_events_are_ignored_while_astrbot_is_active_even_with_the_code(self):
        code = self.core.owner_claim_start()["code"]
        await self.say(private_event(code, user=STRANGER))
        await self.quiet()
        self.assertEqual(self.core._owner_claim.status()["state"], "pending")


class ClaimApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import ClientSession

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.core = BridgeCore(RemotePlatform(), lambda: self.root)
        self.core._emotion_store = ConfigStore(self.root)
        self.port = free_port()
        app = create_console_app(
            self.core._emotion_store,
            owner_claim_start=self.core.owner_claim_start,
            owner_claim_status=self.core.owner_claim_status,
        )
        self.service = ConsoleService(app, port=self.port)
        await self.service.start()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.stop()

    async def call(self, path, method="GET", headers=None):
        async with self.client.request(method, f"http://127.0.0.1:{self.port}{path}", headers=headers) as response:
            return response.status, await response.json()

    async def test_start_and_poll_the_claim(self):
        status, body = await self.call("/api/owner/claim")
        self.assertEqual((status, body["state"], body["active_platform"], body["current"]), (200, "idle", "astrbot", None))
        status, started = await self.call("/api/owner/claim", "POST")
        self.assertEqual((status, started["expires_in"], started["replaces"]), (200, CLAIM_TTL_SECONDS, False))
        self.assertRegex(started["code"], r"^\d{6}$")
        status, body = await self.call("/api/owner/claim")
        self.assertEqual((body["state"], body["code"]), ("pending", started["code"]))
        self.assertLessEqual(body["expires_in"], CLAIM_TTL_SECONDS)

    async def test_the_origin_check_covers_the_claim_routes(self):
        status, body = await self.call("/api/owner/claim", "POST", headers={"Origin": "http://evil.example"})
        self.assertEqual((status, body["error"]), (403, "forbidden_origin"))
        self.assertEqual(self.core._owner_claim.status()["state"], "idle")
        status, _ = await self.call("/api/owner/claim", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()

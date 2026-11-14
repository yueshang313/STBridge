"""101e: client_id, the single on-duty ST page, and the stuck "generation unconfirmed" mark."""

import unittest

from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    ACTIVE_PAGE_LEASE_SECONDS,
    ActivePageLease,
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import (
    UNCONFIRMED_GUARD_MAX_AGE_SECONDS,
    RequestBusyError,
    RequestStateManager,
    RequestStatus,
)

ORIGIN = "http://localhost:11451"
IDLE = {"generating": False, "active_request": False}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LeaseUnitTests(unittest.TestCase):
    def test_first_page_is_on_duty_and_newcomer_takes_over_when_idle(self):
        lease = ActivePageLease(10)
        self.assertEqual(lease.touch("a", 0, False), (True, True))
        self.assertEqual(lease.touch("a", 1, False), (True, False))
        self.assertEqual(lease.touch("b", 2, False), (True, True))
        # The superseded page does not take it back while the new page keeps polling.
        self.assertEqual(lease.touch("a", 3, False), (False, False))
        self.assertEqual(lease.touch("b", 4, False), (True, False))

    def test_superseded_page_returns_after_the_lease_runs_out(self):
        lease = ActivePageLease(10)
        lease.touch("a", 0, False)
        lease.touch("b", 1, False)
        self.assertEqual(lease.touch("a", 5, False)[0], False)
        self.assertEqual(lease.touch("a", 12, False)[0], True)

    def test_busy_page_keeps_its_place_and_hands_over_afterwards(self):
        lease = ActivePageLease(10)
        lease.touch("a", 0, False)
        self.assertEqual(lease.touch("b", 50, True), (False, False))
        self.assertEqual(lease.touch("a", 55, True), (True, False))
        # The request ended: the waiting page is promoted on the next call from anyone.
        self.assertEqual(lease.touch("b", 56, False), (True, True))
        self.assertEqual(lease.touch("a", 57, False)[0], False)


class ActivePageHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.manager = RequestStateManager(timeout_seconds=60, clock=self.clock)
        gate = BridgeRecoveryGate()
        gate.confirm_recovery()
        app = create_app(
            self.manager,
            HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452),
            clock=self.clock,
            recovery_gate=gate,
        )
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def call(self, method, path, cid=None, json=None):
        self.clock.advance(1.5)  # past the 1 s poll limit
        url = path if cid is None else f"{path}?client_id={cid}"
        response = await self.client.request(method, url, json=json, headers={"Origin": ORIGIN})
        body = None if response.status == 204 else await response.json()
        return response.status, body

    async def pending(self, cid):
        return await self.call("GET", "/v1/requests/pending", cid)

    async def claim(self, cid, request_id, attempt="attempt-1"):
        return await self.call("POST", f"/v1/requests/{request_id}/claim", cid, {"claim_attempt_id": attempt})

    async def generation(self, cid, request_id, state):
        return await self.call("POST", f"/v1/requests/{request_id}/generation", cid, {"state": state})

    async def test_invalid_client_id_is_refused(self):
        self.assertEqual(await self.pending("bad id!"), (400, {"error": "invalid_client_id"}))

    async def test_page_without_client_id_still_works(self):
        request = self.manager.create_request("hi")
        status, body = await self.pending(None)
        self.assertEqual((status, body["request_id"]), (200, request.request_id))
        self.assertEqual((await self.claim(None, request.request_id))[0], 200)

    async def test_newcomer_takes_over_and_old_page_cannot_claim(self):
        self.assertEqual(await self.pending("pageA"), (204, None))
        self.assertEqual(await self.pending("pageB"), (204, None))
        self.assertEqual(
            await self.pending("pageA"),
            (200, {"active": False, "reason": "other_client_active"}),
        )
        request = self.manager.create_request("hello")
        status, body = await self.pending("pageB")
        self.assertEqual((status, body["request_id"]), (200, request.request_id))
        self.assertEqual(
            await self.claim("pageA", request.request_id),
            (409, {"error": "not_active_client"}),
        )
        self.assertEqual((await self.claim("pageB", request.request_id))[0], 200)

    async def test_generating_page_is_not_replaced_until_the_request_ends(self):
        await self.pending("pageA")
        request = self.manager.create_request("hello")
        rid = request.request_id
        await self.claim("pageA", rid)
        await self.generation("pageA", rid, "RUNNING")
        # A newcomer must wait; the old page also stays on duty with silence of over 10 s.
        self.assertEqual(
            await self.pending("pageB"),
            (200, {"active": False, "reason": "other_client_active"}),
        )
        self.clock.advance(ACTIVE_PAGE_LEASE_SECONDS + 5)
        self.assertEqual(
            await self.pending("pageB"),
            (200, {"active": False, "reason": "other_client_active"}),
        )
        self.assertEqual((await self.generation("pageA", rid, "FINISHED"))[0], 200)
        status, _ = await self.call("POST", f"/v1/requests/{rid}/result", "pageA", {"reply": "ok"})
        self.assertEqual(status, 200)
        # Done: the waiting page takes over on its next poll, the old page is turned away.
        self.assertEqual(await self.pending("pageB"), (204, None))
        self.assertEqual(
            await self.pending("pageA"),
            (200, {"active": False, "reason": "other_client_active"}),
        )

    async def test_other_page_cannot_report_on_a_claimed_request(self):
        await self.pending("pageA")
        rid = self.manager.create_request("hello").request_id
        await self.claim("pageA", rid)
        await self.generation("pageA", rid, "RUNNING")
        for method, suffix, body in (
            ("POST", "generation", {"state": "FINISHED"}),
            ("POST", "result", {"reply": "x"}),
            ("POST", "failure", {"error_code": "extension_error"}),
            ("POST", "reject", {"error_code": "st_busy"}),
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    await self.call(method, f"/v1/requests/{rid}/{suffix}", "pageB", body),
                    (409, {"error": "not_claim_owner"}),
                )
        self.assertEqual(self.manager.get_request(rid).status, RequestStatus.CLAIMED)

    async def test_new_page_clears_a_dead_pages_unconfirmed_mark(self):
        """Review #1: claim, start generating, nobody ever confirms, then the page is refreshed."""
        await self.pending("pageA")
        rid = self.manager.create_request("hello").request_id
        await self.claim("pageA", rid)
        await self.generation("pageA", rid, "RUNNING")
        self.clock.advance(61)  # the request expires; the mark stays
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("again")
        status, body = await self.call("POST", "/v1/session/ready", "pageB", IDLE)
        self.assertEqual((status, body["active"]), (200, True))
        self.assertEqual(self.manager.create_request("again").status, RequestStatus.PENDING)

    async def test_mark_is_kept_while_the_same_page_may_still_be_generating(self):
        await self.pending("pageA")
        rid = self.manager.create_request("hello").request_id
        await self.claim("pageA", rid)
        await self.generation("pageA", rid, "RUNNING")
        self.clock.advance(61)
        await self.pending("pageA")  # the owner is still the on-duty page
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("again")

    async def test_old_page_returns_when_the_new_one_goes_silent(self):
        await self.pending("pageA")
        await self.pending("pageB")
        self.clock.advance(ACTIVE_PAGE_LEASE_SECONDS + 1)
        self.assertEqual(await self.pending("pageA"), (204, None))
        self.assertEqual(
            await self.pending("pageB"),
            (200, {"active": False, "reason": "other_client_active"}),
        )


class AgedGuardTests(unittest.TestCase):
    def test_expired_request_unconfirmed_mark_clears_by_itself_after_ten_minutes(self):
        clock = FakeClock()
        manager = RequestStateManager(timeout_seconds=60, clock=clock)
        request = manager.create_request("hi")
        manager.claim_request(request.request_id, "attempt-1")
        manager.mark_generation_started(request.request_id)
        clock.advance(61)
        with self.assertRaises(RequestBusyError):
            manager.create_request("again")
        clock.advance(UNCONFIRMED_GUARD_MAX_AGE_SECONDS - 5)
        with self.assertRaises(RequestBusyError):
            manager.create_request("again")
        clock.advance(10)
        self.assertEqual(manager.create_request("again").status, RequestStatus.PENDING)


if __name__ == "__main__":
    unittest.main()

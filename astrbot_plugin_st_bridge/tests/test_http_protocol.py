import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import (
    BridgeRecoveryGate,
    HttpProtocolConfig,
    create_app,
)
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import (
    GenerationStatus,
    RequestBusyError,
    RequestStateManager,
    RequestStatus,
)


ORIGIN = "http://localhost:11451"
# Older clients may still send a bearer header; the server must ignore it.
STALE_AUTHORIZATION = "Bearer stale-token-from-an-older-client"


class OpenRecoveryGate(BridgeRecoveryGate):
    def allows_business_requests(self):
        return True


def create_open_app(*args, **kwargs):
    kwargs.setdefault("recovery_gate", OpenRecoveryGate())
    return create_app(*args, **kwargs)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class HttpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.manager = RequestStateManager(timeout_seconds=10, clock=self.clock)
        self.config = HttpProtocolConfig(
            allowed_origin=ORIGIN,
            listen_port=11452,
        )
        app = create_open_app(
            self.manager,
            self.config,
            clock=self.clock,
        )
        self.server = TestServer(app, host="127.0.0.1")
        self.client = TestClient(self.server)
        self._pending_tasks_before_server = self._pending_non_current_tasks()
        await self.client.start_server()

    async def asyncTearDown(self):
        await self._close_test_client(
            self.client,
            self.server,
            self._pending_tasks_before_server,
        )

    @staticmethod
    def _pending_non_current_tasks():
        current_task = asyncio.current_task()
        return {
            task
            for task in asyncio.all_tasks()
            if task is not current_task and not task.done()
        }

    async def _close_test_client(self, client, server, pending_tasks_before):
        await client.close()
        await asyncio.sleep(0)
        self.assertTrue(server.closed)
        self.assertIsNotNone(server.runner)
        self.assertIsNone(server.runner._server)
        self.assertEqual(self._pending_non_current_tasks(), pending_tasks_before)

    def headers(self, *, origin=ORIGIN):
        return {"Origin": origin}

    async def post_json(self, path, payload, *, origin=ORIGIN):
        return await self.client.post(
            path,
            json=payload,
            headers=self.headers(origin=origin),
        )

    async def test_health_and_business_routes_need_no_token(self):
        response = await self.client.get("/v1/health", headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True})
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertNotIn("WWW-Authenticate", response.headers)

        for headers in (
            self.headers(),
            {**self.headers(), "Authorization": STALE_AUTHORIZATION},
            {**self.headers(), "Authorization": "malformed"},
        ):
            with self.subTest(headers=sorted(headers)):
                self.clock.advance(self.config.min_poll_interval_seconds)
                pending = await self.client.get("/v1/requests/pending", headers=headers)
                self.assertEqual(pending.status, 204)

    async def test_unapproved_origin_is_denied_without_cors_allow_origin(self):
        response = await self.client.get(
            "/v1/health",
            headers=self.headers(origin="http://attacker.example"),
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(response.headers["Vary"], "Origin")

    async def test_options_preflight_is_exact_and_has_no_credentials_or_wildcard(self):
        response = await self.client.options(
            "/v1/requests/pending",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(response.headers["Vary"], "Origin")
        self.assertEqual(
            response.headers["Access-Control-Allow-Methods"],
            "GET, POST, OPTIONS",
        )
        self.assertEqual(
            response.headers["Access-Control-Allow-Headers"],
            "Authorization, Content-Type",
        )
        self.assertNotIn("Access-Control-Allow-Credentials", response.headers)
        self.assertNotEqual(response.headers["Access-Control-Allow-Origin"], "*")

        denied = await self.client.options(
            "/v1/requests/pending",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(denied.status, 403)

    async def test_invalid_json_and_fields_are_rejected(self):
        malformed = await self.client.post(
            "/v1/requests/request-1/result",
            data="{",
            headers={**self.headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(await malformed.json(), {"error": "invalid_json"})

        missing = await self.post_json("/v1/requests/request-1/result", {})
        extra = await self.post_json(
            "/v1/requests/request-1/result",
            {"reply": "ok", "unexpected": "field"},
        )
        self.assertEqual(missing.status, 400)
        self.assertEqual(extra.status, 400)

        wrong_type = await self.post_json(
            "/v1/requests/request-1/generation",
            {"state": "UNKNOWN"},
        )
        self.assertEqual(wrong_type.status, 422)

    async def test_overlong_json_integer_is_a_bounded_client_error(self):
        body = '{"reply":' + ("9" * 5000) + "}"
        response = await self.client.post(
            "/v1/requests/request-1/result",
            data=body,
            headers={**self.headers(), "Content-Type": "application/json"},
        )

        self.assertEqual(response.status, 400)
        response_text = await response.text()
        self.assertEqual(response_text, '{"error": "invalid_json"}')
        self.assertNotIn(body, response_text)
        self.assertNotIn("Traceback", response_text)
        self.assertNotIn("ValueError", response_text)

    async def test_unexpected_internal_error_is_sanitized_and_service_recovers(self):
        failure_text = "PRIVATE_FAILURE_TEXT"
        private_path = "C:\\private\\bridge\\internal.py"
        private_token = "PRIVATE_INTERNAL_TOKEN"
        test_clock = self.clock

        class FailOnceManager(RequestStateManager):
            def __init__(self):
                super().__init__(timeout_seconds=10, clock=test_clock)
                self.fail_next_poll = True

            def peek_pending_request(self):
                if self.fail_next_poll:
                    self.fail_next_poll = False
                    raise RuntimeError(
                        f"{failure_text} {private_path} {private_token}"
                    )
                return super().peek_pending_request()

        manager = FailOnceManager()
        app = create_open_app(
            manager,
            self.config,
            clock=self.clock,
        )
        server = TestServer(app, host="127.0.0.1")
        client = TestClient(server)
        pending_tasks_before = self._pending_non_current_tasks()
        await client.start_server()
        try:
            failed = await client.get(
                "/v1/requests/pending",
                headers=self.headers(),
            )
            self.assertEqual(failed.status, 500)
            failed_text = await failed.text()
            self.assertEqual(failed_text, '{"error": "internal_error"}')
            for private_value in (
                failure_text,
                private_path,
                private_token,
                "Traceback",
                "RuntimeError",
            ):
                self.assertNotIn(private_value, failed_text)

            self.clock.advance(self.config.min_poll_interval_seconds)
            recovered = await client.get(
                "/v1/requests/pending",
                headers=self.headers(),
            )
            self.assertEqual(recovered.status, 204)

            invalid_json = await client.post(
                "/v1/requests/request-1/result",
                data="{",
                headers={**self.headers(), "Content-Type": "application/json"},
            )
            self.assertEqual(invalid_json.status, 400)

            oversized_body = await client.post(
                "/v1/requests/request-1/result",
                data=b"{" + b" " * self.config.max_body_bytes,
                headers={**self.headers(), "Content-Type": "application/json"},
            )
            self.assertEqual(oversized_body.status, 413)

            wrong_origin = await client.get(
                "/v1/health",
                headers=self.headers(origin="http://attacker.example"),
            )
            self.assertEqual(wrong_origin.status, 403)
        finally:
            await self._close_test_client(client, server, pending_tasks_before)

    async def test_request_body_and_message_text_limits(self):
        oversized_body = await self.client.post(
            "/v1/requests/request-1/result",
            data=b"{" + b" " * (64 * 1024),
            headers={**self.headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(oversized_body.status, 413)

        request = self.manager.create_request("x" * (self.config.max_request_text_bytes + 1))
        pending = await self.client.get(
            "/v1/requests/pending",
            headers=self.headers(),
        )
        self.assertEqual(pending.status, 200)
        self.assertEqual(await pending.json(), {"request_id": request.request_id})

        claim = await self.post_json(
            f"/v1/requests/{request.request_id}/claim",
            {"claim_attempt_id": "claim-large"},
        )
        self.assertEqual(claim.status, 413)
        self.assertEqual(
            self.manager.get_request(request.request_id).status,
            RequestStatus.FAILED,
        )

    async def test_unknown_request_id_and_no_create_route(self):
        response = await self.client.get(
            "/v1/requests/unknown-id",
            headers=self.headers(),
        )
        self.assertEqual(response.status, 404)

        create = await self.post_json("/v1/requests", {"content": "not accepted"})
        self.assertEqual(create.status, 404)

    async def test_pending_claim_and_same_attempt_retry(self):
        request = self.manager.create_request("private request text")
        pending = await self.client.get(
            "/v1/requests/pending",
            headers=self.headers(),
        )
        self.assertEqual(await pending.json(), {"request_id": request.request_id})

        path = f"/v1/requests/{request.request_id}/claim"
        first = await self.post_json(path, {"claim_attempt_id": "claim-a"})
        second = await self.post_json(path, {"claim_attempt_id": "claim-a"})
        first_body = await first.json()
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(await second.json(), first_body)
        self.assertEqual(first_body["content"], "private request text")
        self.assertEqual(first_body["status"], "CLAIMED")

    async def test_different_claim_attempts_compete_and_cross_request_reuse_fails(self):
        first_request = self.manager.create_request("first")
        path = f"/v1/requests/{first_request.request_id}/claim"
        responses = await asyncio.gather(
            self.post_json(path, {"claim_attempt_id": "claim-one"}),
            self.post_json(path, {"claim_attempt_id": "claim-two"}),
        )
        self.assertCountEqual([response.status for response in responses], [200, 409])

        winner = self.manager.get_request(first_request.request_id).claim_attempt_id
        started = await self.post_json(
            f"/v1/requests/{first_request.request_id}/generation",
            {"state": "RUNNING"},
        )
        self.assertEqual(started.status, 200)
        finished = await self.post_json(
            f"/v1/requests/{first_request.request_id}/generation",
            {"state": "FINISHED"},
        )
        self.assertEqual(finished.status, 200)
        completed = await self.post_json(
            f"/v1/requests/{first_request.request_id}/result",
            {"reply": "first reply"},
        )
        self.assertEqual(completed.status, 200)

        second_request = self.manager.create_request("second")
        second_path = f"/v1/requests/{second_request.request_id}/claim"
        reused = await self.post_json(
            second_path,
            {"claim_attempt_id": winner},
        )
        self.assertEqual(reused.status, 409)
        fresh = await self.post_json(
            second_path,
            {"claim_attempt_id": "claim-fresh"},
        )
        self.assertEqual(fresh.status, 200)

    async def test_status_generation_result_and_duplicate_result(self):
        request = self.manager.create_request("status test message")
        path = f"/v1/requests/{request.request_id}"
        await self.post_json(
            f"{path}/claim",
            {"claim_attempt_id": "claim-status"},
        )

        unknown_result = await self.post_json(
            f"{path}/result",
            {"reply": "too early"},
        )
        self.assertEqual(unknown_result.status, 409)
        self.assertEqual(
            self.manager.get_request(request.request_id).generation_status,
            GenerationStatus.UNKNOWN,
        )
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("still guarded")

        started = await self.post_json(f"{path}/generation", {"state": "RUNNING"})
        self.assertEqual(started.status, 200)
        status = await self.client.get(path, headers=self.headers())
        status_body = await status.json()
        self.assertEqual(status_body["generation_status"], GenerationStatus.RUNNING.value)
        self.assertNotIn("content", status_body)
        self.assertNotIn("reply", status_body)

        running_result = await self.post_json(
            f"{path}/result",
            {"reply": "still generating"},
        )
        self.assertEqual(running_result.status, 409)
        self.assertEqual(
            self.manager.get_request(request.request_id).generation_status,
            GenerationStatus.RUNNING,
        )
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("still guarded")

        duplicate_start = await self.post_json(
            f"{path}/generation",
            {"state": "RUNNING"},
        )
        self.assertEqual(duplicate_start.status, 409)
        self.assertEqual(
            self.manager.get_request(request.request_id).generation_status,
            GenerationStatus.RUNNING,
        )

        finished = await self.post_json(
            f"{path}/generation",
            {"state": "FINISHED"},
        )
        duplicate_finish = await self.post_json(
            f"{path}/generation",
            {"state": "FINISHED"},
        )
        self.assertEqual(finished.status, 200)
        self.assertEqual(duplicate_finish.status, 200)

        result_path = f"{path}/result"
        first = await self.post_json(result_path, {"reply": "new reply"})
        self.assertEqual(first.status, 200)
        self.assertEqual((await first.json())["status"], RequestStatus.SUCCEEDED.value)
        duplicate = await self.post_json(result_path, {"reply": "duplicate reply"})
        self.assertEqual(duplicate.status, 409)
        self.assertNotIn("new reply", await first.text())
        self.assertNotIn("duplicate reply", await duplicate.text())

    async def test_expired_late_result_keeps_running_generation_guard_until_finished(self):
        request = self.manager.create_request("late result")
        path = f"/v1/requests/{request.request_id}"
        await self.post_json(f"{path}/claim", {"claim_attempt_id": "claim-late"})
        self.assertEqual(
            (await self.post_json(f"{path}/generation", {"state": "RUNNING"})).status,
            200,
        )
        self.clock.advance(10)

        late = await self.post_json(f"{path}/result", {"reply": "too late"})
        self.assertEqual(late.status, 410)
        expired = self.manager.get_request(request.request_id)
        self.assertEqual(expired.status, RequestStatus.EXPIRED)
        self.assertEqual(expired.generation_status, GenerationStatus.RUNNING)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("blocked")

        finished = await self.post_json(
            f"{path}/generation",
            {"state": "FINISHED"},
        )
        self.assertEqual(finished.status, 200)

        late_after_finish = await self.post_json(
            f"{path}/result",
            {"reply": "expired reply remains rejected"},
        )
        self.assertEqual(late_after_finish.status, 410)
        expired_after_finish = self.manager.get_request(request.request_id)
        self.assertEqual(expired_after_finish.status, RequestStatus.EXPIRED)
        self.assertEqual(
            expired_after_finish.generation_status,
            GenerationStatus.FINISHED,
        )
        self.assertEqual(
            self.manager.create_request("allowed").status,
            RequestStatus.PENDING,
        )

    async def test_expired_unknown_generation_requires_not_started_confirmation(self):
        request = self.manager.create_request("unknown generation")
        path = f"/v1/requests/{request.request_id}"
        await self.post_json(f"{path}/claim", {"claim_attempt_id": "claim-unknown"})
        self.clock.advance(10)

        late = await self.post_json(f"{path}/result", {"reply": "too late"})
        self.assertEqual(late.status, 410)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("blocked")

        confirmed = await self.post_json(
            f"{path}/generation",
            {"state": "NOT_STARTED"},
        )
        self.assertEqual(confirmed.status, 200)
        duplicate_confirmation = await self.post_json(
            f"{path}/generation",
            {"state": "NOT_STARTED"},
        )
        self.assertEqual(duplicate_confirmation.status, 410)
        expired = self.manager.get_request(request.request_id)
        self.assertEqual(expired.status, RequestStatus.EXPIRED)
        self.assertEqual(expired.generation_status, GenerationStatus.NOT_STARTED)
        self.assertEqual(
            self.manager.create_request("allowed").status,
            RequestStatus.PENDING,
        )

    async def test_reject_failure_and_error_codes_are_bounded(self):
        request = self.manager.create_request("reject me")
        path = f"/v1/requests/{request.request_id}/reject"
        invalid = await self.post_json(path, {"error_code": "internal stack text"})
        self.assertEqual(invalid.status, 422)
        rejected = await self.post_json(path, {"error_code": "binding_mismatch"})
        self.assertEqual(rejected.status, 200)
        self.assertEqual((await rejected.json())["status"], RequestStatus.REJECTED.value)

        failed_request = self.manager.create_request("fail me")
        failed_path = f"/v1/requests/{failed_request.request_id}"
        await self.post_json(
            f"{failed_path}/claim",
            {"claim_attempt_id": "claim-failure"},
        )
        failed = await self.post_json(
            f"{failed_path}/failure",
            {"error_code": "generation_failed"},
        )
        self.assertEqual(failed.status, 200)
        self.assertEqual((await failed.json())["status"], RequestStatus.FAILED.value)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("blocked")
        not_started = await self.post_json(
            f"{failed_path}/generation",
            {"state": "NOT_STARTED"},
        )
        self.assertEqual(not_started.status, 200)

    async def test_poll_rate_limit_uses_injected_clock(self):
        first = await self.client.get("/v1/requests/pending", headers=self.headers())
        self.assertEqual(first.status, 204)
        limited = await self.client.get("/v1/requests/pending", headers=self.headers())
        self.assertEqual(limited.status, 429)
        self.assertEqual(limited.headers["Retry-After"], "1")

        self.clock.advance(self.config.min_poll_interval_seconds)
        allowed = await self.client.get("/v1/requests/pending", headers=self.headers())
        self.assertEqual(allowed.status, 204)

    async def test_no_error_or_status_response_or_protocol_log_echoes_secrets(self):
        request_text = "PRIVATE_FULL_REQUEST_BODY"
        reply_text = "PRIVATE_FULL_REPLY_BODY"
        request = self.manager.create_request(request_text)
        path = f"/v1/requests/{request.request_id}"

        with self.assertNoLogs("http_protocol", level="DEBUG"):
            wrong_origin = await self.client.post(
                f"{path}/claim",
                json={"claim_attempt_id": "claim-safe"},
                headers={"Origin": "http://attacker.example"},
            )
            self.assertEqual(wrong_origin.status, 403)
            health = await self.client.get("/v1/health", headers=self.headers())
            self.assertEqual(health.status, 200)
            await self.post_json(f"{path}/claim", {"claim_attempt_id": "claim-safe"})
            await self.post_json(f"{path}/generation", {"state": "RUNNING"})
            await self.post_json(f"{path}/generation", {"state": "FINISHED"})
            result = await self.post_json(f"{path}/result", {"reply": reply_text})
            self.assertEqual(result.status, 200)
            status = await self.client.get(path, headers=self.headers())

        for response in (wrong_origin, health, result, status):
            text = await response.text()
            self.assertNotIn(request_text, text)
            self.assertNotIn(reply_text, text)


class HttpProtocolConfigurationTests(unittest.TestCase):
    def test_config_rejects_non_loopback_or_non_exact_origins(self):
        with self.assertRaises(ValueError):
            HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452, listen_host="0.0.0.0")
        with self.assertRaises(ValueError):
            HttpProtocolConfig(allowed_origin="*", listen_port=11452)
        with self.assertRaises(ValueError):
            HttpProtocolConfig(allowed_origin=f"{ORIGIN}/path", listen_port=11452)


class RecoveryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_instance_defaults_to_recovery_required_for_all_business_routes(self):
        clock = FakeClock()
        manager = RequestStateManager(timeout_seconds=10, clock=clock)
        request = manager.create_request("pending test request")
        gate = BridgeRecoveryGate()
        self.assertEqual(gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
        app = create_app(
            manager,
            HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452),
            clock=clock,
            recovery_gate=gate,
        )
        server = TestServer(app, host="127.0.0.1")
        client = TestClient(server)
        pending_tasks_before = HttpProtocolTests._pending_non_current_tasks()
        await client.start_server()
        try:
            health = await client.get("/v1/health", headers={"Origin": ORIGIN})
            self.assertEqual(health.status, 200)

            business_requests = (
                client.get("/v1/requests/pending", headers={"Origin": ORIGIN}),
                client.get(f"/v1/requests/{request.request_id}", headers={"Origin": ORIGIN}),
                client.post(
                    f"/v1/requests/{request.request_id}/claim",
                    json={"claim_attempt_id": "isolated-claim"},
                    headers={"Origin": ORIGIN},
                ),
                client.post(
                    f"/v1/requests/{request.request_id}/generation",
                    json={"state": "RUNNING"},
                    headers={"Origin": ORIGIN},
                ),
                client.post(
                    f"/v1/requests/{request.request_id}/result",
                    json={"reply": "must not be accepted"},
                    headers={"Origin": ORIGIN},
                ),
                client.post(
                    f"/v1/requests/{request.request_id}/reject",
                    json={"error_code": "chat_changed"},
                    headers={"Origin": ORIGIN},
                ),
                client.post(
                    f"/v1/requests/{request.request_id}/failure",
                    json={"error_code": "generation_failed"},
                    headers={"Origin": ORIGIN},
                ),
            )
            responses = await asyncio.gather(*business_requests)
            for response in responses:
                self.assertEqual(response.status, 503)
                self.assertEqual(await response.json(), {"error": "recovery_required"})

            unchanged = manager.get_request(request.request_id)
            self.assertEqual(unchanged.status, RequestStatus.PENDING)
            self.assertEqual(unchanged.generation_status, GenerationStatus.NOT_STARTED)
        finally:
            await client.close()
            await asyncio.sleep(0)
            self.assertTrue(server.closed)
            self.assertIsNotNone(server.runner)
            self.assertIsNone(server.runner._server)
            current_task = asyncio.current_task()
            pending_tasks_after = {
                task
                for task in asyncio.all_tasks()
                if task is not current_task and not task.done()
            }
            self.assertEqual(pending_tasks_after, pending_tasks_before)

    async def test_fresh_gate_does_not_inherit_prior_instance_state(self):
        old_instance_gate = BridgeRecoveryGate()
        old_instance_gate.begin_shutdown()
        old_instance_gate.mark_stopped()

        new_instance_gate = BridgeRecoveryGate()
        self.assertEqual(new_instance_gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
        self.assertIs(new_instance_gate.allows_business_requests(), False)

    async def test_explicit_confirmation_opens_only_the_current_gate(self):
        gate = BridgeRecoveryGate()
        self.assertTrue(gate.confirm_recovery())
        self.assertEqual(gate.state, BridgeRecoveryGate.RECOVERED)
        self.assertTrue(gate.allows_business_requests())
        self.assertTrue(gate.confirm_recovery())

        next_instance = BridgeRecoveryGate()
        self.assertEqual(next_instance.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
        self.assertFalse(next_instance.allows_business_requests())

        next_instance.begin_shutdown()
        self.assertFalse(next_instance.confirm_recovery())


class NoTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_authorization_is_ignored_and_origin_still_required(self):
        manager = RequestStateManager(timeout_seconds=10)
        config = HttpProtocolConfig(allowed_origin=ORIGIN, listen_port=11452)
        app = create_open_app(manager, config)
        server = TestServer(app, host="127.0.0.1")
        client = TestClient(server)
        await client.start_server()
        try:
            ok = await client.get(
                "/v1/requests/pending",
                headers={"Origin": ORIGIN, "Authorization": STALE_AUTHORIZATION},
            )
            self.assertEqual(ok.status, 204)
            for headers in ({}, {"Origin": "http://attacker.example"}, {"Origin": "null"}):
                with self.subTest(headers=headers):
                    denied = await client.get("/v1/requests/pending", headers=headers)
                    self.assertEqual(denied.status, 403)
                    self.assertEqual(await denied.json(), {"error": "origin_denied"})
        finally:
            await client.close()
            await asyncio.sleep(0)
            self.assertTrue(server.closed)
            self.assertIsNone(server.runner._server)


class LoopbackAliasOriginTests(unittest.IsolatedAsyncioTestCase):
    """101d: ST opened as http://127.0.0.1:<port> must work as well as http://localhost:<port>."""

    LOCALHOST = "http://localhost:11451"
    LOOPBACK = "http://127.0.0.1:11451"

    async def asyncSetUp(self):
        config = HttpProtocolConfig(
            allowed_origin=self.LOCALHOST,
            extra_origins=(self.LOOPBACK,),
            listen_port=11452,
        )
        app = create_open_app(RequestStateManager(timeout_seconds=10), config)
        self.server = TestServer(app, host="127.0.0.1")
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_both_aliases_pass_and_the_matching_origin_is_echoed(self):
        for origin in (self.LOCALHOST, self.LOOPBACK):
            with self.subTest(origin=origin):
                response = await self.client.get("/v1/health", headers={"Origin": origin})
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
                preflight = await self.client.options(
                    "/v1/session/ready",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                self.assertEqual(preflight.status, 204)
                self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], origin)

    async def test_other_ports_and_hosts_are_still_denied(self):
        for origin in (
            "http://localhost:11999",
            "http://127.0.0.1:11999",
            "http://127.0.0.2:11451",
            "http://[::1]:11451",
            "https://127.0.0.1:11451",
            "http://attacker.example",
        ):
            with self.subTest(origin=origin):
                response = await self.client.get("/v1/health", headers={"Origin": origin})
                self.assertEqual(response.status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        missing = await self.client.get("/v1/health")
        self.assertEqual(missing.status, 403)

    def test_extra_origins_must_be_exact_loopback_origins(self):
        with self.assertRaises(ValueError):
            HttpProtocolConfig(allowed_origin=self.LOCALHOST, extra_origins=("*",), listen_port=11452)
        with self.assertRaises(ValueError):
            HttpProtocolConfig(allowed_origin=self.LOCALHOST, extra_origins=("http://evil.example",), listen_port=11452)


if __name__ == "__main__":
    unittest.main()

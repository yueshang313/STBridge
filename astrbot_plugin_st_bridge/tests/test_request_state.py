import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import (
    GenerationStatus,
    InvalidTransitionError,
    RequestBusyError,
    RequestStateManager,
    RequestStatus,
    UnknownRequestError,
)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RequestStateManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.manager = RequestStateManager(timeout_seconds=10, clock=self.clock)

    def test_create_request_succeeds(self):
        request = self.manager.create_request("hello")

        self.assertTrue(request.request_id)
        self.assertEqual(request.content, "hello")
        self.assertEqual(request.status, RequestStatus.PENDING)
        self.assertEqual(request.created_at, 100.0)
        self.assertEqual(request.expires_at, 110.0)
        self.assertIsNone(request.reply)

    def test_second_request_is_rejected_without_queueing(self):
        first = self.manager.create_request("first")

        with self.assertRaises(RequestBusyError):
            self.manager.create_request("second")

        self.assertEqual(self.manager.peek_pending_request().request_id, first.request_id)

    def test_same_claim_attempt_retry_is_idempotent(self):
        request = self.manager.create_request("hello")

        claimed = self.manager.claim_request(request.request_id, "claim-1")
        retried = self.manager.claim_request(request.request_id, "claim-1")

        self.assertEqual(claimed.status, RequestStatus.CLAIMED)
        self.assertEqual(claimed.generation_status, GenerationStatus.UNKNOWN)
        self.assertEqual(claimed.claim_attempt_id, "claim-1")
        self.assertEqual(retried, claimed)
        with self.assertRaises(InvalidTransitionError):
            self.manager.claim_request(request.request_id, "claim-2")

    def test_successful_result_is_accepted_once(self):
        request = self.manager.create_request("hello")
        self.manager.claim_request(request.request_id, "claim-result")

        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "reply")
        self.assertEqual(
            self.manager.get_request(request.request_id).generation_status,
            GenerationStatus.UNKNOWN,
        )
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("still guarded")

        self.manager.mark_generation_started(request.request_id)
        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "reply")
        self.assertEqual(
            self.manager.get_request(request.request_id).generation_status,
            GenerationStatus.RUNNING,
        )
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("still guarded")

        self.manager.confirm_generation_finished(request.request_id)
        completed = self.manager.complete_request(request.request_id, "reply")

        self.assertEqual(completed.status, RequestStatus.SUCCEEDED)
        self.assertEqual(completed.reply, "reply")
        self.assertEqual(completed.generation_status, GenerationStatus.FINISHED)
        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "duplicate")
        self.assertEqual(
            self.manager.get_request(request.request_id).reply,
            "reply",
        )

    def test_wrong_request_id_is_rejected(self):
        request = self.manager.create_request("hello")

        with self.assertRaises(UnknownRequestError):
            self.manager.claim_request("wrong-id", "claim-wrong")
        with self.assertRaises(UnknownRequestError):
            self.manager.complete_request("wrong-id", "reply")

        self.assertEqual(
            self.manager.get_request(request.request_id).status,
            RequestStatus.PENDING,
        )

    def test_expired_request_cannot_be_claimed(self):
        request = self.manager.create_request("hello")
        self.clock.advance(10)

        with self.assertRaises(InvalidTransitionError):
            self.manager.claim_request(request.request_id, "claim-expired")

        self.assertEqual(
            self.manager.get_request(request.request_id).status,
            RequestStatus.EXPIRED,
        )

    def test_late_result_is_rejected_after_expiration(self):
        request = self.manager.create_request("hello")
        self.manager.claim_request(request.request_id, "claim-late")
        self.manager.mark_generation_started(request.request_id)
        self.clock.advance(10)

        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "late reply")

        expired = self.manager.get_request(request.request_id)
        self.assertEqual(expired.status, RequestStatus.EXPIRED)
        self.assertEqual(expired.generation_status, GenerationStatus.RUNNING)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("next")

        finished = self.manager.confirm_generation_finished(request.request_id)
        self.assertEqual(finished.generation_status, GenerationStatus.FINISHED)
        next_request = self.manager.create_request("next")
        self.assertEqual(next_request.status, RequestStatus.PENDING)

    def test_illegal_transition_and_terminal_state_are_rejected(self):
        request = self.manager.create_request("hello")

        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "reply")
        self.assertEqual(
            self.manager.get_request(request.request_id).status,
            RequestStatus.PENDING,
        )

        self.manager.reject_request(request.request_id, "binding_mismatch")
        rejected = self.manager.get_request(request.request_id)
        with self.assertRaises(InvalidTransitionError):
            self.manager.fail_request(request.request_id, "late_failure")
        self.assertEqual(self.manager.get_request(request.request_id), rejected)
        with self.assertRaises(InvalidTransitionError):
            self.manager.claim_request(request.request_id, "claim-terminal")

    def test_next_request_can_be_created_after_terminal_state(self):
        first = self.manager.create_request("first")
        self.manager.fail_request(first.request_id, "test_failure")

        second = self.manager.create_request("second")

        self.assertNotEqual(first.request_id, second.request_id)
        self.assertEqual(second.status, RequestStatus.PENDING)

    def test_parallel_creation_has_only_one_winner(self):
        worker_count = 12
        barrier = threading.Barrier(worker_count)

        def create(index):
            barrier.wait()
            try:
                return self.manager.create_request(f"message-{index}")
            except RequestBusyError:
                return None

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(create, range(worker_count)))

        winners = [request for request in results if request is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            self.manager.peek_pending_request().request_id,
            winners[0].request_id,
        )

    def test_parallel_claim_attempts_have_only_one_winner(self):
        request = self.manager.create_request("hello")
        attempt_ids = [f"claim-{index}" for index in range(12)]
        barrier = threading.Barrier(len(attempt_ids))

        def claim(attempt_id):
            barrier.wait()
            try:
                return self.manager.claim_request(request.request_id, attempt_id)
            except InvalidTransitionError:
                return None

        with ThreadPoolExecutor(max_workers=len(attempt_ids)) as pool:
            results = list(pool.map(claim, attempt_ids))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0].claim_attempt_id, attempt_ids)
        self.assertEqual(
            self.manager.get_request(request.request_id),
            winners[0],
        )

    def test_claim_attempt_id_cannot_be_reused_for_another_request(self):
        manager = RequestStateManager(
            timeout_seconds=10,
            clock=self.clock,
            history_limit=2,
        )
        first = manager.create_request("first")
        manager.claim_request(first.request_id, "claim-reused")
        manager.confirm_generation_finished(first.request_id)
        manager.complete_request(first.request_id, "reply")

        second = manager.create_request("second")
        self.assertEqual(
            manager.get_request(first.request_id).status,
            RequestStatus.SUCCEEDED,
        )
        with self.assertRaises(InvalidTransitionError):
            manager.claim_request(second.request_id, "claim-reused")

        self.assertEqual(
            manager.get_request(second.request_id).status,
            RequestStatus.PENDING,
        )
        claimed = manager.claim_request(second.request_id, "claim-new")
        self.assertEqual(claimed.status, RequestStatus.CLAIMED)

    def test_claim_attempt_bindings_are_pruned_with_bounded_history(self):
        history_limit = 3
        manager = RequestStateManager(
            timeout_seconds=10,
            clock=self.clock,
            history_limit=history_limit,
        )

        for index in range(12):
            request = manager.create_request(f"message-{index}")
            claim_attempt_id = f"claim-{index}"
            manager.claim_request(request.request_id, claim_attempt_id)
            manager.confirm_generation_finished(request.request_id)
            manager.complete_request(request.request_id, f"reply-{index}")

            self.assertLessEqual(len(manager._requests), history_limit)
            self.assertLessEqual(len(manager._claim_attempt_requests), history_limit)
            self.assertEqual(
                manager._claim_attempt_requests,
                {
                    item.claim_attempt_id: item.request_id
                    for item in manager._requests.values()
                    if item.claim_attempt_id is not None
                },
            )

        self.assertNotIn("claim-0", manager._claim_attempt_requests)
        next_request = manager.create_request("after pruning")
        reused = manager.claim_request(next_request.request_id, "claim-0")
        self.assertEqual(reused.status, RequestStatus.CLAIMED)

    def test_expired_unknown_generation_stays_guarded_until_confirmed_not_started(self):
        request = self.manager.create_request("hello")
        self.manager.claim_request(request.request_id, "claim-unknown")
        self.clock.advance(10)

        with self.assertRaises(InvalidTransitionError):
            self.manager.complete_request(request.request_id, "late reply")

        expired = self.manager.get_request(request.request_id)
        self.assertEqual(expired.status, RequestStatus.EXPIRED)
        self.assertEqual(expired.generation_status, GenerationStatus.UNKNOWN)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("next")

        confirmed = self.manager.confirm_generation_not_started(request.request_id)
        self.assertEqual(confirmed.generation_status, GenerationStatus.NOT_STARTED)
        self.assertEqual(
            self.manager.create_request("next").status,
            RequestStatus.PENDING,
        )

    def test_rejected_claim_can_confirm_generation_never_started(self):
        request = self.manager.create_request("hello")
        self.manager.claim_request(request.request_id, "claim-rejected")
        rejected = self.manager.reject_request(request.request_id, "binding_mismatch")

        self.assertEqual(rejected.generation_status, GenerationStatus.UNKNOWN)
        with self.assertRaises(RequestBusyError):
            self.manager.create_request("next")

        confirmed = self.manager.confirm_generation_not_started(request.request_id)
        with self.assertRaises(InvalidTransitionError):
            self.manager.confirm_generation_not_started(request.request_id)
        self.assertEqual(self.manager.get_request(request.request_id), confirmed)
        self.assertEqual(
            self.manager.create_request("next").status,
            RequestStatus.PENDING,
        )


if __name__ == "__main__":
    unittest.main()

"""In-memory request state management for the ST bridge."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable
from uuid import uuid4

from .emotion_config import is_safe_path_segment
from .st_injection import validate_macros


class RequestStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class GenerationStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    UNKNOWN = "UNKNOWN"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class RequestManagerError(Exception):
    """Base exception for request state errors."""


class RequestBusyError(RequestManagerError):
    """Raised when the single request or generation slot is occupied."""


class UnknownRequestError(RequestManagerError):
    """Raised when a request ID is unknown or no longer retained."""


class InvalidTransitionError(RequestManagerError):
    """Raised when an operation is invalid for the request's current state."""


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    request_id: str
    content: str
    status: RequestStatus
    created_at: float
    expires_at: float
    reply: str | None = None
    error_code: str | None = None
    generation_status: GenerationStatus = GenerationStatus.NOT_STARTED
    claim_attempt_id: str | None = None
    # [{"path": [str, ...], "value": int | None}]: MVU stat_data values handed to ST on claim.
    affinity_writes: tuple[dict, ...] | None = None
    # "chat" for QQ user messages; "proactive" for scheduled turns (ST gets a narrator line).
    kind: str = "chat"
    # {"emotions": "...", ...}: this turn's values of the {{stbridge_*}} macros (R5); None for older callers.
    macros: dict | None = None


REQUEST_KINDS = frozenset({"chat", "proactive"})
MAX_AFFINITY_WRITES = 4
MAX_AFFINITY_PATH_SEGMENTS = 3
AFFINITY_VALUE_LIMIT = 100


def validate_affinity_writes(writes: object) -> tuple[dict, ...] | None:
    """Check and copy the stat_data writes; None means "no affinity writes"."""
    if writes is None:
        return None
    if not isinstance(writes, (list, tuple)) or len(writes) > MAX_AFFINITY_WRITES:
        raise ValueError("invalid affinity writes")
    checked = []
    for write in writes:
        if not isinstance(write, dict) or set(write) != {"path", "value"}:
            raise ValueError("invalid affinity write")
        path = write["path"]
        if (
            not isinstance(path, (list, tuple))
            or not 1 <= len(path) <= MAX_AFFINITY_PATH_SEGMENTS
            or not all(is_safe_path_segment(segment) for segment in path)
        ):
            raise ValueError("invalid affinity write path")
        value = write["value"]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -AFFINITY_VALUE_LIMIT <= value <= AFFINITY_VALUE_LIMIT
        ):
            raise ValueError("invalid affinity write value")
        checked.append({"path": list(path), "value": value})
    return tuple(checked)


_TERMINAL_STATUSES = frozenset(
    {
        RequestStatus.SUCCEEDED,
        RequestStatus.REJECTED,
        RequestStatus.FAILED,
        RequestStatus.EXPIRED,
    }
)
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SAFE_CLAIM_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class RequestStateManager:
    """Manage one active bridge request without persistence or I/O."""

    def __init__(
        self,
        timeout_seconds: float = 120.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        request_id_factory: Callable[[], str] | None = None,
        history_limit: int = 32,
    ) -> None:
        self._validate_timeout(timeout_seconds)
        if history_limit < 1:
            raise ValueError("history_limit must be at least 1")

        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._history_limit = history_limit
        self._lock = threading.Lock()
        self._requests: dict[str, BridgeRequest] = {}
        self._active_request_id: str | None = None
        self._unconfirmed_generation_id: str | None = None
        # Bindings are retained only while their owning request remains in history.
        self._claim_attempt_requests: dict[str, str] = {}

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than 0")

    @staticmethod
    def _validate_code(error_code: str) -> None:
        if not isinstance(error_code, str) or not _SAFE_CODE.fullmatch(error_code):
            raise ValueError("error_code must be a short identifier")

    @staticmethod
    def _validate_claim_attempt_id(claim_attempt_id: str) -> None:
        if not isinstance(claim_attempt_id, str) or not _SAFE_CLAIM_ATTEMPT_ID.fullmatch(
            claim_attempt_id
        ):
            raise ValueError("claim_attempt_id must be a short identifier")

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite number")
        return now

    def _lookup_locked(self, request_id: str) -> BridgeRequest:
        request = self._requests.get(request_id)
        if request is None:
            raise UnknownRequestError("unknown request_id")
        return request

    def _store_locked(self, request: BridgeRequest) -> BridgeRequest:
        self._requests.pop(request.request_id, None)
        self._requests[request.request_id] = request
        self._prune_history_locked()
        return request

    def _prune_history_locked(self) -> None:
        while len(self._requests) > self._history_limit:
            removable_id = next(
                (
                    request_id
                    for request_id, request in self._requests.items()
                    if request.status in _TERMINAL_STATUSES
                    and request_id != self._active_request_id
                    and request_id != self._unconfirmed_generation_id
                ),
                None,
            )
            if removable_id is None:
                return
            removed = self._requests.pop(removable_id)
            if removed.claim_attempt_id is not None:
                bound_request_id = self._claim_attempt_requests.get(
                    removed.claim_attempt_id
                )
                if bound_request_id == removable_id:
                    del self._claim_attempt_requests[removed.claim_attempt_id]

    def _expire_active_locked(self, now: float) -> BridgeRequest | None:
        request_id = self._active_request_id
        if request_id is None:
            return None

        request = self._requests[request_id]
        if request.status not in {RequestStatus.PENDING, RequestStatus.CLAIMED}:
            self._active_request_id = None
            return None
        if now < request.expires_at:
            return None

        expired = self._store_locked(replace(request, status=RequestStatus.EXPIRED))
        self._active_request_id = None
        return expired

    def create_request(
        self,
        content: str,
        *,
        timeout_seconds: float | None = None,
        affinity_writes: list[dict] | None = None,
        kind: str = "chat",
        macros: dict | None = None,
    ) -> BridgeRequest:
        """Create a request, rejecting immediately if either slot is occupied."""
        if kind not in REQUEST_KINDS:
            raise ValueError("invalid request kind")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        timeout = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        self._validate_timeout(timeout)
        writes = validate_affinity_writes(affinity_writes)
        checked_macros = validate_macros(macros)

        with self._lock:
            now = self._now()
            self._expire_active_locked(now)
            if self._active_request_id is not None:
                raise RequestBusyError("an active request already exists")
            if self._unconfirmed_generation_id is not None:
                raise RequestBusyError(
                    "a prior ST generation has not been confirmed finished"
                )

            request_id = self._request_id_factory()
            if (
                not isinstance(request_id, str)
                or not request_id
                or request_id in self._requests
            ):
                raise RuntimeError("request_id_factory returned an invalid or reused ID")

            request = BridgeRequest(
                request_id=request_id,
                content=content,
                status=RequestStatus.PENDING,
                created_at=now,
                expires_at=now + float(timeout),
                affinity_writes=writes,
                kind=kind,
                macros=checked_macros,
            )
            self._active_request_id = request_id
            return self._store_locked(request)

    def get_request(self, request_id: str) -> BridgeRequest:
        """Return a snapshot and apply lazy expiration if its deadline passed."""
        with self._lock:
            self._expire_active_locked(self._now())
            return self._lookup_locked(request_id)

    def peek_pending_request(self) -> BridgeRequest | None:
        """Return the pending request, if any, without claiming it."""
        with self._lock:
            self._expire_active_locked(self._now())
            if self._active_request_id is None:
                return None
            request = self._requests[self._active_request_id]
            return request if request.status == RequestStatus.PENDING else None

    def generation_in_progress(self) -> bool:
        """True while ST holds a claimed request or an earlier generation is still unconfirmed."""
        with self._lock:
            self._expire_active_locked(self._now())
            request_id = self._active_request_id
            if request_id is not None and self._requests[request_id].status == RequestStatus.CLAIMED:
                return True
            return self._unconfirmed_generation_id is not None

    def expire_due_request(self) -> BridgeRequest | None:
        """Expire the active request if its deadline has passed; no timer is started."""
        with self._lock:
            return self._expire_active_locked(self._now())

    def claim_request(
        self,
        request_id: str,
        claim_attempt_id: str,
    ) -> BridgeRequest:
        """Claim a pending request, allowing retries of the same claim attempt."""
        self._validate_claim_attempt_id(claim_attempt_id)
        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)

            bound_request_id = self._claim_attempt_requests.get(claim_attempt_id)
            if bound_request_id is not None and bound_request_id != request_id:
                raise InvalidTransitionError(
                    "claim_attempt_id is already bound to another request"
                )
            if (
                request.status == RequestStatus.CLAIMED
                and request.claim_attempt_id == claim_attempt_id
            ):
                return request
            if request.status != RequestStatus.PENDING:
                raise InvalidTransitionError(
                    f"cannot claim request in {request.status.value} state"
                )
            if self._active_request_id != request_id:
                raise InvalidTransitionError("request is not the active request")

            claimed = self._store_locked(
                replace(
                    request,
                    status=RequestStatus.CLAIMED,
                    generation_status=GenerationStatus.UNKNOWN,
                    claim_attempt_id=claim_attempt_id,
                )
            )
            self._unconfirmed_generation_id = request_id
            self._claim_attempt_requests[claim_attempt_id] = request_id
            return claimed

    def complete_request(self, request_id: str, reply: str) -> BridgeRequest:
        """Accept a reply only after generation has explicitly been confirmed finished."""
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("reply must be a non-empty string")

        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)
            self._require_status_locked(request, {RequestStatus.CLAIMED})
            if request.generation_status != GenerationStatus.FINISHED:
                raise InvalidTransitionError(
                    "generation must be confirmed finished before accepting a reply"
                )
            completed = self._store_locked(
                replace(
                    request,
                    status=RequestStatus.SUCCEEDED,
                    reply=reply,
                )
            )
            self._active_request_id = None
            self._clear_generation_guard_locked(request_id)
            return completed

    def reject_request(self, request_id: str, error_code: str) -> BridgeRequest:
        """Reject a pending or claimed request with a non-sensitive code."""
        return self._finish_request(
            request_id,
            RequestStatus.REJECTED,
            error_code,
        )

    def fail_request(self, request_id: str, error_code: str) -> BridgeRequest:
        """Fail a pending or claimed request with a non-sensitive code."""
        return self._finish_request(request_id, RequestStatus.FAILED, error_code)

    def _finish_request(
        self,
        request_id: str,
        status: RequestStatus,
        error_code: str,
    ) -> BridgeRequest:
        self._validate_code(error_code)
        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)
            self._require_status_locked(
                request,
                {RequestStatus.PENDING, RequestStatus.CLAIMED},
            )
            finished = self._store_locked(
                replace(request, status=status, error_code=error_code)
            )
            if self._active_request_id == request_id:
                self._active_request_id = None
            return finished

    def mark_generation_started(self, request_id: str) -> BridgeRequest:
        """Record that the claimed ST request has begun generation."""
        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)
            if (
                self._unconfirmed_generation_id != request_id
                or request.generation_status != GenerationStatus.UNKNOWN
            ):
                raise InvalidTransitionError("generation cannot start in this state")
            return self._store_locked(
                replace(request, generation_status=GenerationStatus.RUNNING)
            )

    def confirm_generation_not_started(self, request_id: str) -> BridgeRequest:
        """Clear uncertainty when ST confirms it never began this request."""
        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)
            if request.generation_status != GenerationStatus.UNKNOWN:
                raise InvalidTransitionError(
                    "generation is not awaiting a not-started confirmation"
                )
            confirmed = self._store_locked(
                replace(request, generation_status=GenerationStatus.NOT_STARTED)
            )
            self._clear_generation_guard_locked(request_id)
            return confirmed

    def confirm_generation_finished(self, request_id: str) -> BridgeRequest:
        """Confirm generation is no longer running, even for expired requests."""
        with self._lock:
            self._expire_active_locked(self._now())
            request = self._lookup_locked(request_id)
            if request.generation_status == GenerationStatus.FINISHED:
                return request
            if request.generation_status not in {
                GenerationStatus.UNKNOWN,
                GenerationStatus.RUNNING,
            }:
                raise InvalidTransitionError("generation is not awaiting completion")
            confirmed = self._store_locked(
                replace(request, generation_status=GenerationStatus.FINISHED)
            )
            self._clear_generation_guard_locked(request_id)
            return confirmed

    def _clear_generation_guard_locked(self, request_id: str) -> None:
        if self._unconfirmed_generation_id == request_id:
            self._unconfirmed_generation_id = None

    @staticmethod
    def _require_status_locked(
        request: BridgeRequest,
        allowed_statuses: set[RequestStatus],
    ) -> None:
        if request.status not in allowed_statuses:
            raise InvalidTransitionError(
                f"cannot finish request in {request.status.value} state"
            )


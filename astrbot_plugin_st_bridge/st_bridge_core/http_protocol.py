"""Authenticated local HTTP protocol for the SillyTavern bridge.

Default limits are a 64 KiB JSON body, 8 KiB request text, 128 KiB reply,
128-character request IDs, and one pending-request poll per second. A caller
must pass an explicit listen port; the bind host is restricted to 127.0.0.1.
Access is limited by the exact ST Origin and the loopback Host header; there is
no bearer token, because any script on the ST page could read one anyway.
This module builds an aiohttp application but does not start a listener.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from aiohttp import web

from .request_state import (
    BridgeRequest,
    GenerationStatus,
    InvalidTransitionError,
    RequestStateManager,
    RequestStatus,
    UnknownRequestError,
)


DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_MAX_REQUEST_TEXT_BYTES = 8 * 1024
DEFAULT_MAX_REPLY_BYTES = 128 * 1024
DEFAULT_MAX_REQUEST_ID_CHARS = 128
MAX_PRESET_BACKUP_BYTES = 8 * 1024 * 1024  # a whole ST preset, R5
DEFAULT_MIN_POLL_INTERVAL_SECONDS = 1.0

ALLOWED_METHODS = frozenset({"GET", "POST"})
# Authorization stays allowed so older clients still pass preflight; its value is ignored.
ALLOWED_REQUEST_HEADERS = frozenset({"authorization", "content-type"})
ALLOWED_GENERATION_STATES = frozenset({"RUNNING", "NOT_STARTED", "FINISHED"})
REJECTION_CODES = frozenset({"binding_mismatch", "st_busy", "chat_changed"})
FAILURE_CODES = frozenset(
    {
        "generation_failed",
        "generation_timeout",
        "extension_error",
        "request_too_large",
    }
)


@dataclass(frozen=True, slots=True)
class HttpProtocolConfig:
    """HTTP policy and future bind settings. The app builder opens no socket."""

    allowed_origin: str
    listen_port: int
    extra_origins: tuple[str, ...] = ()
    listen_host: str = "127.0.0.1"
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_request_text_bytes: int = DEFAULT_MAX_REQUEST_TEXT_BYTES
    max_reply_bytes: int = DEFAULT_MAX_REPLY_BYTES
    max_request_id_chars: int = DEFAULT_MAX_REQUEST_ID_CHARS
    min_poll_interval_seconds: float = DEFAULT_MIN_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.listen_host != "127.0.0.1":
            raise ValueError("listen_host must be 127.0.0.1")
        if (
            isinstance(self.listen_port, bool)
            or not isinstance(self.listen_port, int)
            or not 1 <= self.listen_port <= 65535
        ):
            raise ValueError("listen_port must be an explicit TCP port")
        _validate_origin(self.allowed_origin)
        for extra in self.extra_origins:
            _validate_origin(extra)
        for name in (
            "max_body_bytes",
            "max_request_text_bytes",
            "max_reply_bytes",
            "max_request_id_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.min_poll_interval_seconds, bool)
            or not isinstance(self.min_poll_interval_seconds, (int, float))
            or not math.isfinite(self.min_poll_interval_seconds)
            or self.min_poll_interval_seconds <= 0
        ):
            raise ValueError("min_poll_interval_seconds must be greater than 0")


def _origin_allowed(origin: str | None, config: HttpProtocolConfig) -> bool:
    return origin is not None and (origin == config.allowed_origin or origin in config.extra_origins)


class _ProtocolError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class BridgeRecoveryGate:
    """Fail-closed business gate for a newly loaded plugin instance."""

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERED = "RECOVERED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"

    def __init__(self) -> None:
        self._state = self.RECOVERY_REQUIRED

    @property
    def state(self) -> str:
        return self._state

    def allows_business_requests(self) -> bool:
        return self._state == self.RECOVERED

    def confirm_recovery(self) -> bool:
        """Open the gate; used by the operator command and the ST idle report."""
        if self._state == self.RECOVERY_REQUIRED:
            self._state = self.RECOVERED
            return True
        return self._state == self.RECOVERED

    def begin_shutdown(self) -> None:
        if self._state != self.STOPPED:
            self._state = self.STOPPING

    def mark_stopped(self) -> None:
        self._state = self.STOPPED


# --- Which ST page is "on duty" (101e) -----------------------------------------------------
# Every ST page load makes a random client_id and sends it as the `client_id` query parameter
# on /v1/session/ready, /v1/requests/pending and every per-request call. Only the on-duty page
# may receive messages; the others get an empty `pending` marked {"active": false} and their
# `claim` is refused with 409 not_active_client. A page without client_id (an older extension)
# is the single client "" and behaves as before.
#
# Rules: a page that reports in while no request is claimed takes over at once; while a claimed
# request is unfinished the on-duty page keeps its place and the newcomer waits (candidate)
# until the request ends. A superseded page only gets back in after the on-duty page has been
# silent for ACTIVE_PAGE_LEASE_SECONDS. The bridge only guarantees ONE page receives messages;
# it cannot stop ST itself from overwriting a chat that two pages edit by hand.
ACTIVE_PAGE_LEASE_SECONDS = 10.0
LEGACY_CLIENT_ID = ""
_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_TRACKED_CLIENTS = 64


class ActivePageLease:
    """Pure bookkeeping for the on-duty ST page; the caller supplies the clock reading."""

    def __init__(self, lease_seconds: float = ACTIVE_PAGE_LEASE_SECONDS) -> None:
        self.lease_seconds = lease_seconds
        self.active: str | None = None
        self._seen = 0.0
        self._candidate: str | None = None
        self._candidate_seen = 0.0
        self._superseded: dict[str, None] = {}
        self.owners: dict[str, str] = {}

    def _take(self, client_id: str, now: float) -> None:
        if self.active is not None and self.active != client_id:
            self._superseded[self.active] = None
            while len(self._superseded) > _MAX_TRACKED_CLIENTS:
                self._superseded.pop(next(iter(self._superseded)))
        self._superseded.pop(client_id, None)
        self.active = client_id
        self._seen = now
        self._candidate = None

    def touch(self, client_id: str, now: float, busy: bool) -> tuple[bool, bool]:
        """Record a call from `client_id`. Returns (is_on_duty, on_duty_page_changed)."""
        changed = False
        if self._candidate is not None and not busy:
            candidate, self._candidate = self._candidate, None
            if candidate != self.active and now - self._candidate_seen <= self.lease_seconds:
                self._take(candidate, now)
                changed = True
        if client_id == self.active:
            self._seen = now
            return True, changed
        if self.active is None:
            self._take(client_id, now)
            return True, True
        if busy:
            if client_id not in self._superseded:
                self._candidate = client_id
                self._candidate_seen = now
            return False, changed
        stale = now - self._seen > self.lease_seconds
        if stale or client_id not in self._superseded:
            self._take(client_id, now)
            return True, True
        return False, changed

    def bind_owner(self, request_id: str, client_id: str) -> None:
        self.owners[request_id] = client_id
        while len(self.owners) > _MAX_TRACKED_CLIENTS:
            self.owners.pop(next(iter(self.owners)))


@dataclass(slots=True)
class _RuntimeState:
    last_pending_poll_at: float | None = None
    poll_times: dict[str, float] = field(default_factory=dict)
    lease: ActivePageLease = field(default_factory=ActivePageLease)


_CONFIG_KEY = web.AppKey("bridge_http_config", HttpProtocolConfig)
_MANAGER_KEY = web.AppKey("bridge_request_manager", RequestStateManager)
_RECOVERY_CALLBACK_KEY = web.AppKey("bridge_recovery_callback", Callable[[], None])
_RUNTIME_KEY = web.AppKey("bridge_http_runtime", _RuntimeState)
_CLOCK_KEY = web.AppKey("bridge_http_clock", Callable[[], float])
_RECOVERY_GATE_KEY = web.AppKey("bridge_recovery_gate", BridgeRecoveryGate)
_INJECTION_KEY = web.AppKey("bridge_injection_provider", Callable[[], dict])
_PRESET_BACKUP_KEY = web.AppKey("bridge_preset_backup", Callable[[str, str], str])


def bridge_runtime_status(app: web.Application) -> dict[str, object]:
    """Read-only snapshot for the console: gate state and how long ago ST last polled."""
    last = app[_RUNTIME_KEY].last_pending_poll_at
    age = None if last is None else max(0.0, round(app[_CLOCK_KEY]() - last, 1))
    return {"state": app[_RECOVERY_GATE_KEY].state, "last_poll_age": age}


def _validate_origin(origin: str) -> None:
    if not isinstance(origin, str) or origin in {"*", "null"}:
        raise ValueError("allowed_origin must be one exact HTTP origin")
    try:
        parts = urlsplit(origin)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("allowed_origin is invalid") from exc
    if (
        parts.scheme not in {"http", "https"}
        or parts.hostname is None
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or origin != f"{parts.scheme}://{parts.netloc}"
    ):
        raise ValueError("allowed_origin must be one exact HTTP origin")


def _json_response(data: dict[str, object], status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error_response(status: int, code: str) -> web.Response:
    return _json_response({"error": code}, status=status)


def _valid_host_header(request: web.Request, config: HttpProtocolConfig) -> bool:
    values = request.headers.getall("Host", [])
    if len(values) != 1:
        return False
    try:
        parts = urlsplit(f"//{values[0]}")
        _ = parts.port
    except ValueError:
        return False
    return (
        parts.hostname == config.listen_host
        and parts.username is None
        and parts.password is None
        and not parts.path
        and not parts.query
        and not parts.fragment
    )


def _business_requests_allowed(gate: BridgeRecoveryGate) -> bool:
    try:
        result = gate.allows_business_requests()
    except Exception:
        return False
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            try:
                result.close()
            except Exception:
                pass
        return False
    return result is True


def _add_response_headers(
    response: web.Response,
    origin: str | None,
    config: HttpProtocolConfig,
) -> web.Response:
    response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if _origin_allowed(origin, config):
        response.headers["Access-Control-Allow-Origin"] = origin
    return response


def _preflight_response(request: web.Request) -> web.Response:
    requested_method = request.headers.get("Access-Control-Request-Method", "")
    if requested_method not in ALLOWED_METHODS:
        return _error_response(403, "preflight_denied")

    requested_headers = request.headers.get("Access-Control-Request-Headers", "")
    headers = {
        item.strip().lower()
        for item in requested_headers.split(",")
        if item.strip()
    }
    if not headers.issubset(ALLOWED_REQUEST_HEADERS):
        return _error_response(403, "preflight_denied")

    response = web.Response(status=204)
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Max-Age"] = "300"
    return response


# Reachable while the gate is closed: liveness and the ST idle report that opens it.
_GATE_EXEMPT_ROUTES = frozenset({
    ("GET", "/v1/health"),
    ("POST", "/v1/session/ready"),
    # R5: the extension panel reads the entry list and backs the preset up whatever the gate says.
    ("GET", "/v1/injection"),
    ("POST", "/v1/injection/backup"),
})


@web.middleware
async def _security_middleware(
    request: web.Request,
    handler: Callable[[web.Request], object],
) -> web.Response:
    config = request.app[_CONFIG_KEY]
    origin_values = request.headers.getall("Origin", [])
    origin = origin_values[0] if len(origin_values) == 1 else None

    if not _origin_allowed(origin, config):
        response = _error_response(403, "origin_denied")
    elif not _valid_host_header(request, config):
        response = _error_response(403, "host_denied")
    elif request.method == "OPTIONS":
        response = _preflight_response(request)
    else:
        if request.method not in ALLOWED_METHODS:
            response = _error_response(405, "method_not_allowed")
        elif (request.method, request.path) not in _GATE_EXEMPT_ROUTES and not (
            _business_requests_allowed(request.app[_RECOVERY_GATE_KEY])
        ):
            response = _error_response(503, "recovery_required")
        else:
            try:
                response = await handler(request)  # type: ignore[misc]
            except _ProtocolError as exc:
                response = _error_response(exc.status, exc.code)
            except Exception:
                response = _error_response(500, "internal_error")

    return _add_response_headers(response, origin, config)


async def _read_json_object(
    request: web.Request,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if request.content_type != "application/json":
        raise _ProtocolError(415, "content_type_required")
    try:
        value = await request.json(loads=json.loads)
    except web.HTTPRequestEntityTooLarge as exc:
        raise _ProtocolError(413, "body_too_large") from exc
    except (ValueError, RecursionError, web.HTTPBadRequest) as exc:
        raise _ProtocolError(400, "invalid_json") from exc
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise _ProtocolError(400, "invalid_fields")
    return value


def _text_byte_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _ProtocolError(400, "invalid_text") from exc


def _validate_request_id(request: web.Request, config: HttpProtocolConfig) -> str:
    request_id = request.match_info.get("request_id", "")
    if not request_id or len(request_id) > config.max_request_id_chars:
        raise _ProtocolError(404, "request_not_found")
    return request_id


def _status_response(request: BridgeRequest) -> web.Response:
    payload: dict[str, object] = {
        "request_id": request.request_id,
        "status": request.status.value,
        "generation_status": request.generation_status.value,
    }
    if request.error_code is not None:
        payload["error_code"] = request.error_code
    return _json_response(payload)


def _raise_state_conflict(
    manager: RequestStateManager,
    request_id: str,
) -> None:
    try:
        current = manager.get_request(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    if current.status == RequestStatus.EXPIRED:
        raise _ProtocolError(410, "request_expired")
    raise _ProtocolError(409, "invalid_request_state")


def _client_id(request: web.Request) -> str:
    values = request.query.getall("client_id", [])
    if not values:
        return LEGACY_CLIENT_ID
    if len(values) != 1 or not _CLIENT_ID_PATTERN.fullmatch(values[0]):
        raise _ProtocolError(400, "invalid_client_id")
    return values[0]


def _touch_client(request: web.Request, client_id: str) -> bool:
    """Count this call for the on-duty page bookkeeping; True when `client_id` is on duty."""
    runtime = request.app[_RUNTIME_KEY]
    manager = request.app[_MANAGER_KEY]
    now = request.app[_CLOCK_KEY]()
    active, _ = runtime.lease.touch(client_id, now, manager.has_claimed_request())
    if active:
        # A request an earlier page claimed and never finished must not block this page for good.
        owners = runtime.lease.owners
        manager.clear_stale_generation_guard(
            lambda request_id: owners.get(request_id, client_id) != client_id
        )
    return active


def _require_owner(request: web.Request, request_id: str) -> None:
    """Only the page that claimed a request may report on it."""
    owner = request.app[_RUNTIME_KEY].lease.owners.get(request_id)
    if owner is not None and owner != _client_id(request):
        raise _ProtocolError(409, "not_claim_owner")


async def _health(_: web.Request) -> web.Response:
    return _json_response({"ok": True})


async def _session_ready(request: web.Request) -> web.Response:
    """Open a fresh gate when the ST client reports that it is idle."""
    payload = await _read_json_object(request, frozenset({"generating", "active_request"}))
    generating = payload["generating"]
    active_request = payload["active_request"]
    if not isinstance(generating, bool) or not isinstance(active_request, bool):
        raise _ProtocolError(422, "invalid_ready_state")

    client_id = _client_id(request)
    gate = request.app[_RECOVERY_GATE_KEY]
    state = gate.state
    if state in {BridgeRecoveryGate.STOPPING, BridgeRecoveryGate.STOPPED}:
        raise _ProtocolError(503, "bridge_stopping")
    active = _touch_client(request, client_id)
    if state != BridgeRecoveryGate.RECOVERY_REQUIRED:
        return _json_response({"state": state, "active": active})
    if generating or active_request:
        return _json_response({"state": state, "reason": "client_busy", "active": active})
    if not gate.confirm_recovery():
        return _json_response({"state": gate.state, "active": active})
    callback = request.app[_RECOVERY_CALLBACK_KEY]
    if callback is not None:
        try:
            callback()
        except Exception:
            pass
    return _json_response({"state": gate.state, "active": active})


async def _pending(request: web.Request) -> web.Response:
    config = request.app[_CONFIG_KEY]
    runtime = request.app[_RUNTIME_KEY]
    now = request.app[_CLOCK_KEY]()
    client_id = _client_id(request)
    previous = runtime.poll_times.get(client_id)
    if previous is not None and now - previous < config.min_poll_interval_seconds:
        retry_after = max(1, math.ceil(config.min_poll_interval_seconds - (now - previous)))
        response = _error_response(429, "poll_rate_limited")
        response.headers["Retry-After"] = str(retry_after)
        return response
    runtime.last_pending_poll_at = now
    runtime.poll_times.pop(client_id, None)
    runtime.poll_times[client_id] = now
    while len(runtime.poll_times) > _MAX_TRACKED_CLIENTS:
        runtime.poll_times.pop(next(iter(runtime.poll_times)))

    manager = request.app[_MANAGER_KEY]
    if not _touch_client(request, client_id):
        return _json_response({"active": False, "reason": "other_client_active"})
    pending = manager.peek_pending_request()
    if pending is None:
        return web.Response(status=204)
    return _json_response({"request_id": pending.request_id})


async def _claim(request: web.Request) -> web.Response:
    config = request.app[_CONFIG_KEY]
    manager = request.app[_MANAGER_KEY]
    request_id = _validate_request_id(request, config)
    client_id = _client_id(request)
    if not _touch_client(request, client_id):
        raise _ProtocolError(409, "not_active_client")
    payload = await _read_json_object(request, frozenset({"claim_attempt_id"}))
    claim_attempt_id = payload["claim_attempt_id"]
    if not isinstance(claim_attempt_id, str):
        raise _ProtocolError(400, "invalid_claim_attempt_id")

    try:
        current = manager.get_request(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    if current.status == RequestStatus.EXPIRED:
        raise _ProtocolError(410, "request_expired")
    if (
        current.status == RequestStatus.PENDING
        and _text_byte_length(current.content) > config.max_request_text_bytes
    ):
        try:
            manager.fail_request(request_id, "request_too_large")
        except UnknownRequestError as exc:
            raise _ProtocolError(404, "request_not_found") from exc
        except InvalidTransitionError:
            _raise_state_conflict(manager, request_id)
        raise _ProtocolError(413, "request_text_too_large")

    try:
        claimed = manager.claim_request(request_id, claim_attempt_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    except ValueError as exc:
        raise _ProtocolError(400, "invalid_claim_attempt_id") from exc
    except InvalidTransitionError:
        _raise_state_conflict(manager, request_id)

    request.app[_RUNTIME_KEY].lease.bind_owner(request_id, client_id)
    payload: dict[str, object] = {
        "request_id": claimed.request_id,
        "status": claimed.status.value,
        "content": claimed.content,
    }
    if claimed.kind == "proactive":
        # Only scheduled turns carry the field, so older clients and normal requests are unchanged.
        payload["kind"] = "proactive"
    if claimed.affinity_writes is not None:
        payload["affinity_writes"] = [
            {"path": list(write["path"]), "value": write["value"]}
            for write in claimed.affinity_writes
        ]
    if claimed.macros is not None:
        payload["macros"] = dict(claimed.macros)
    return _json_response(payload)


async def _get_status(request: web.Request) -> web.Response:
    manager = request.app[_MANAGER_KEY]
    request_id = _validate_request_id(request, request.app[_CONFIG_KEY])
    try:
        current = manager.get_request(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    return _status_response(current)


async def _generation(request: web.Request) -> web.Response:
    manager = request.app[_MANAGER_KEY]
    request_id = _validate_request_id(request, request.app[_CONFIG_KEY])
    _require_owner(request, request_id)
    payload = await _read_json_object(request, frozenset({"state"}))
    state = payload["state"]
    if not isinstance(state, str) or state not in ALLOWED_GENERATION_STATES:
        raise _ProtocolError(422, "invalid_generation_state")

    try:
        if state == GenerationStatus.RUNNING.value:
            updated = manager.mark_generation_started(request_id)
        elif state == GenerationStatus.NOT_STARTED.value:
            updated = manager.confirm_generation_not_started(request_id)
        else:
            updated = manager.confirm_generation_finished(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    except InvalidTransitionError:
        _raise_state_conflict(manager, request_id)

    return _status_response(updated)


async def _result(request: web.Request) -> web.Response:
    config = request.app[_CONFIG_KEY]
    manager = request.app[_MANAGER_KEY]
    request_id = _validate_request_id(request, config)
    _require_owner(request, request_id)
    payload = await _read_json_object(request, frozenset({"reply"}))
    reply = payload["reply"]
    if not isinstance(reply, str) or not reply.strip():
        raise _ProtocolError(422, "reply_required")
    if _text_byte_length(reply) > config.max_reply_bytes:
        raise _ProtocolError(413, "reply_too_large")

    try:
        current = manager.get_request(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    if current.status == RequestStatus.EXPIRED:
        raise _ProtocolError(410, "request_expired")
    if current.status != RequestStatus.CLAIMED:
        raise _ProtocolError(409, "invalid_request_state")
    if current.generation_status != GenerationStatus.FINISHED:
        raise _ProtocolError(409, "generation_not_confirmed")

    try:
        completed = manager.complete_request(request_id, reply)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    except InvalidTransitionError:
        _raise_state_conflict(manager, request_id)
    except ValueError as exc:
        raise _ProtocolError(422, "reply_required") from exc
    return _status_response(completed)


def _finish_request(
    request: web.Request,
    request_id: str,
    error_code: str,
    *,
    reject: bool,
) -> BridgeRequest:
    manager = request.app[_MANAGER_KEY]
    try:
        current = manager.get_request(request_id)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    if current.status == RequestStatus.EXPIRED:
        raise _ProtocolError(410, "request_expired")

    try:
        if reject:
            return manager.reject_request(request_id, error_code)
        return manager.fail_request(request_id, error_code)
    except UnknownRequestError as exc:
        raise _ProtocolError(404, "request_not_found") from exc
    except InvalidTransitionError:
        _raise_state_conflict(manager, request_id)
    except ValueError as exc:
        raise _ProtocolError(422, "invalid_error_code") from exc


async def _reject(request: web.Request) -> web.Response:
    request_id = _validate_request_id(request, request.app[_CONFIG_KEY])
    _require_owner(request, request_id)
    payload = await _read_json_object(request, frozenset({"error_code"}))
    error_code = payload["error_code"]
    if not isinstance(error_code, str) or error_code not in REJECTION_CODES:
        raise _ProtocolError(422, "invalid_error_code")
    return _status_response(_finish_request(request, request_id, error_code, reject=True))


async def _failure(request: web.Request) -> web.Response:
    request_id = _validate_request_id(request, request.app[_CONFIG_KEY])
    _require_owner(request, request_id)
    payload = await _read_json_object(request, frozenset({"error_code"}))
    error_code = payload["error_code"]
    if not isinstance(error_code, str) or error_code not in FAILURE_CODES:
        raise _ProtocolError(422, "invalid_error_code")
    return _status_response(_finish_request(request, request_id, error_code, reject=False))


async def _injection(request: web.Request) -> web.Response:
    """R5: the entry list (and default macro values) for the extension's inject / sync button."""
    provider = request.app.get(_INJECTION_KEY)
    if provider is None:
        raise _ProtocolError(503, "injection_unavailable")
    try:
        data = provider()
    except Exception as exc:
        raise _ProtocolError(500, "internal_error") from exc
    return _json_response(data)


async def _read_limited_json(request: web.Request, limit: int) -> object:
    """A JSON body of at most `limit` bytes (a preset is far bigger than the protocol's usual bodies)."""
    if request.content_type != "application/json":
        raise _ProtocolError(415, "content_type_required")
    declared = request.content_length
    if declared is not None and declared > limit:
        raise _ProtocolError(413, "body_too_large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise _ProtocolError(413, "body_too_large")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise _ProtocolError(400, "invalid_json") from exc


async def _injection_backup(request: web.Request) -> web.Response:
    """R5: keep a copy of the ST preset the extension is about to change (data/backups/st-presets)."""
    saver = request.app.get(_PRESET_BACKUP_KEY)
    if saver is None:
        raise _ProtocolError(503, "injection_unavailable")
    body = await _read_limited_json(request, MAX_PRESET_BACKUP_BYTES)
    if not isinstance(body, dict) or set(body) != {"name", "preset"}:
        raise _ProtocolError(400, "invalid_fields")
    name, preset = body["name"], body["preset"]
    if not isinstance(name, str) or not isinstance(preset, dict):
        raise _ProtocolError(422, "invalid_preset")
    try:
        saved = saver(name, json.dumps(preset, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise _ProtocolError(500, "backup_failed") from exc
    return _json_response({"file": saved})


async def _not_found(_: web.Request) -> web.Response:
    raise _ProtocolError(404, "route_not_found")


async def _options(_: web.Request) -> web.Response:
    return web.Response(status=204)


def create_app(
    state_manager: RequestStateManager,
    config: HttpProtocolConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    recovery_gate: BridgeRecoveryGate | None = None,
    on_client_recovery: Callable[[], None] | None = None,
    injection_provider: Callable[[], dict] | None = None,
    preset_backup: Callable[[str, str], str] | None = None,
    lease_seconds: float = ACTIVE_PAGE_LEASE_SECONDS,
) -> web.Application:
    """Build the protocol app without opening a listening socket."""
    app = web.Application(
        client_max_size=config.max_body_bytes,
        middlewares=[_security_middleware],
    )
    app[_CONFIG_KEY] = config
    app[_MANAGER_KEY] = state_manager
    app[_RECOVERY_CALLBACK_KEY] = on_client_recovery
    app[_RUNTIME_KEY] = _RuntimeState(lease=ActivePageLease(lease_seconds))
    app[_CLOCK_KEY] = clock
    app[_RECOVERY_GATE_KEY] = recovery_gate or BridgeRecoveryGate()
    if injection_provider is not None:
        app[_INJECTION_KEY] = injection_provider
    if preset_backup is not None:
        app[_PRESET_BACKUP_KEY] = preset_backup

    app.router.add_route("OPTIONS", "/{tail:.*}", _options)
    app.router.add_get("/v1/health", _health, allow_head=False)
    app.router.add_post("/v1/session/ready", _session_ready)
    app.router.add_get("/v1/injection", _injection, allow_head=False)
    app.router.add_post("/v1/injection/backup", _injection_backup)
    app.router.add_get("/v1/requests/pending", _pending, allow_head=False)
    app.router.add_post("/v1/requests/{request_id}/claim", _claim)
    app.router.add_get("/v1/requests/{request_id}", _get_status, allow_head=False)
    app.router.add_post("/v1/requests/{request_id}/generation", _generation)
    app.router.add_post("/v1/requests/{request_id}/result", _result)
    app.router.add_post("/v1/requests/{request_id}/reject", _reject)
    app.router.add_post("/v1/requests/{request_id}/failure", _failure)
    app.router.add_route("*", "/{tail:.*}", _not_found)
    return app

"""Authenticated local HTTP protocol for the SillyTavern bridge.

Default limits are a 64 KiB JSON body, 8 KiB request text, 32 KiB reply,
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
import time
from dataclasses import dataclass
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
DEFAULT_MAX_REPLY_BYTES = 32 * 1024
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


@dataclass(slots=True)
class _RuntimeState:
    last_pending_poll_at: float | None = None


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
    if origin == config.allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = config.allowed_origin
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

    if origin != config.allowed_origin:
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


async def _health(_: web.Request) -> web.Response:
    return _json_response({"ok": True})


async def _session_ready(request: web.Request) -> web.Response:
    """Open a fresh gate when the ST client reports that it is idle."""
    payload = await _read_json_object(request, frozenset({"generating", "active_request"}))
    generating = payload["generating"]
    active_request = payload["active_request"]
    if not isinstance(generating, bool) or not isinstance(active_request, bool):
        raise _ProtocolError(422, "invalid_ready_state")

    gate = request.app[_RECOVERY_GATE_KEY]
    state = gate.state
    if state in {BridgeRecoveryGate.STOPPING, BridgeRecoveryGate.STOPPED}:
        raise _ProtocolError(503, "bridge_stopping")
    if state != BridgeRecoveryGate.RECOVERY_REQUIRED:
        return _json_response({"state": state})
    if generating or active_request:
        return _json_response({"state": state, "reason": "client_busy"})
    if not gate.confirm_recovery():
        return _json_response({"state": gate.state})
    callback = request.app[_RECOVERY_CALLBACK_KEY]
    if callback is not None:
        try:
            callback()
        except Exception:
            pass
    return _json_response({"state": gate.state})


async def _pending(request: web.Request) -> web.Response:
    config = request.app[_CONFIG_KEY]
    runtime = request.app[_RUNTIME_KEY]
    now = request.app[_CLOCK_KEY]()
    previous = runtime.last_pending_poll_at
    if previous is not None and now - previous < config.min_poll_interval_seconds:
        retry_after = max(1, math.ceil(config.min_poll_interval_seconds - (now - previous)))
        response = _error_response(429, "poll_rate_limited")
        response.headers["Retry-After"] = str(retry_after)
        return response
    runtime.last_pending_poll_at = now

    manager = request.app[_MANAGER_KEY]
    pending = manager.peek_pending_request()
    if pending is None:
        return web.Response(status=204)
    return _json_response({"request_id": pending.request_id})


async def _claim(request: web.Request) -> web.Response:
    config = request.app[_CONFIG_KEY]
    manager = request.app[_MANAGER_KEY]
    request_id = _validate_request_id(request, config)
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
    payload = await _read_json_object(request, frozenset({"error_code"}))
    error_code = payload["error_code"]
    if not isinstance(error_code, str) or error_code not in REJECTION_CODES:
        raise _ProtocolError(422, "invalid_error_code")
    return _status_response(_finish_request(request, request_id, error_code, reject=True))


async def _failure(request: web.Request) -> web.Response:
    request_id = _validate_request_id(request, request.app[_CONFIG_KEY])
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
) -> web.Application:
    """Build the protocol app without opening a listening socket."""
    app = web.Application(
        client_max_size=config.max_body_bytes,
        middlewares=[_security_middleware],
    )
    app[_CONFIG_KEY] = config
    app[_MANAGER_KEY] = state_manager
    app[_RECOVERY_CALLBACK_KEY] = on_client_recovery
    app[_RUNTIME_KEY] = _RuntimeState()
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

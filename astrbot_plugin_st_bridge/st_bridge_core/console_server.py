"""Loopback aiohttp service for the emotion management console.

The console is independent of the bridge business listener: it ignores the
bridge recovery gate, and its failures never affect bridge request handling.
There is no token. Requests must carry a loopback Host header (against DNS
rebinding), and state-changing requests that carry an Origin must come from the
console's own origin (against cross-site form and fetch submissions).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import mimetypes
import io
import os
import time
import urllib.parse
import wave
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from .affinity_store import AffinityStore
from .config_presets import (
    MAX_IMPORT_BYTES,
    PresetStore,
    apply_to_config,
    missing_paths,
    normalize_config,
)
from .fs_browser import browse, start_directory
from .install_service import InstallService
from .cover_config import (
    auto_index,
    cover_settings,
    public_cover_settings,
    list_models,
    list_separation_models,
    validate_cover_settings,
)
from .cover_library import SONG_MAX_BYTES, audio_content_type
from .cover_service import CoverError
from .music_source import SOURCES
from .song_request import song_request_settings, validate_song_request_settings
from .diagnostics import build_diagnostics_zip
from .storage_cleanup import cleanup_settings, validate_cleanup_settings
from .proactive_chat_config import validate_proactive_chat
from .schedule_config import (
    MAX_PLANS,
    new_plan_id,
    schedule_section,
    template_list,
    validate_plan,
    validate_schedule_settings,
)
from .sticker_store import MAX_STICKER_BYTES, StickerStore, sticker_content_type
from .tts_service import ServiceStartError
from .emotion_config import (
    LANGUAGES,
    MEDIA_TYPES,
    ConfigStore,
    EmotionConfigError,
    GROUP_TRACK,
    OWNER_TRACK,
    active_identity,
    affinity_settings,
    affinity_tier_name,
    affinity_track,
    default_config,
    default_nickname,
    default_st_name,
    mood_settings,
    ONBOARDING_KEYS,
    console_settings,
    queue_settings,
    validate_onboarding,
    sticker_settings,
    validate_mood_settings,
    validate_sticker_settings,
    validate_affinity_settings,
    validate_queue_settings,
    validate_tts_settings,
    clean_path,
    default_emotion,
    safe_audio_stem,
    safe_emotion,
    safe_name,
    select_tts_profile,
    unique_audio_path,
    validate_emotion_profile,
    validate_nickname,
    validate_st_name,
)
from .network_config import network_settings, reserved_ports, validate_network_settings
from .log_files import LINE_CHOICES, clean_line_count, log_files, tail_lines, zip_logs
from .chat_platform import chat_platform_settings, validate_chat_platform_settings
from .owner_config import owner_settings, validate_owner_settings
from .st_injection import (
    MACRO_DESCRIPTIONS,
    MACRO_KEYS,
    default_injection_settings,
    injection_settings,
    validate_injection_settings,
)
from .reply_texts import default_texts, text_meta, texts_settings, validate_texts
from .song_request import TEXT_FIELDS as _SONG_TEXT_FIELDS


CONSOLE_LISTEN_HOST = "127.0.0.1"
CONSOLE_LISTEN_PORT = 11453
FRONTEND_DIR = Path(__file__).resolve().parent / "console_frontend"
MAX_BODY_BYTES = 128 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 64 * 1024

_STORE_KEY = web.AppKey("console_store", ConfigStore)
_FRONTEND_KEY = web.AppKey("console_frontend_dir", Path)
_ALLOWED_HOSTS_KEY = web.AppKey("console_allowed_hosts", frozenset)
_ALLOWED_ORIGINS_KEY = web.AppKey("console_allowed_origins", frozenset)
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_LOGGER_KEY = web.AppKey("console_logger", Any)
_AFFINITY_KEY = web.AppKey("console_affinity_store", Any)
_MOOD_KEY = web.AppKey("console_mood_store", Any)
_QUEUE_STATUS_KEY = web.AppKey("console_queue_status", Any)
_BRIDGE_STATUS_KEY = web.AppKey("console_bridge_status", Any)
_REPLY_STATS_KEY = web.AppKey("console_reply_stats", Any)
_TODAY_STATS_KEY = web.AppKey("console_today_stats", Any)
_WAKE_PREFIX_KEY = web.AppKey("console_wake_prefix", Any)
_CONNECTOR_STATUS_KEY = web.AppKey("console_connector_status", Any)
_NETWORK_IN_USE_KEY = web.AppKey("console_network_in_use", Any)
_ONEBOT_INFO_KEY = web.AppKey("console_onebot_info", Any)
_TTS_KEY = web.AppKey("console_tts", Any)
_STICKER_KEY = web.AppKey("console_stickers", Any)
_SCHEDULE_TARGETS_KEY = web.AppKey("console_schedule_targets", Any)
_SCHEDULER_KEY = web.AppKey("console_scheduler", Any)
_CHAT_SCHEDULER_KEY = web.AppKey("console_chat_scheduler", Any)
_PRESETS_KEY = web.AppKey("console_presets", PresetStore)
_PRESETS_LOCK_KEY = web.AppKey("console_presets_lock", asyncio.Lock)
MAX_ALIAS_CHARS = 40
_COVER_KEY = web.AppKey("console_cover", Any)
_CLEANUP_KEY = web.AppKey("console_cleanup", Any)
_INSTALL_KEY = web.AppKey("console_install", Any)
_RESTART_KEY = web.AppKey("console_restart", Any)
_TEST_CHAT_KEY = web.AppKey("console_test_chat", Any)
_CLAIM_START_KEY = web.AppKey("console_owner_claim_start", Any)
_CLAIM_STATUS_KEY = web.AppKey("console_owner_claim_status", Any)

_dumps = functools.partial(json.dumps, ensure_ascii=False)


def _json(payload: Any, status: int = 200) -> web.Response:
    response = web.json_response(payload, status=status, dumps=_dumps)
    response.headers["Cache-Control"] = "no-store"
    return response


def _allowed_host(request: web.Request) -> bool:
    # Reject DNS-rebinding hosts before routing.
    values = request.headers.getall("Host", [])
    allowed = request.app.get(_ALLOWED_HOSTS_KEY, frozenset())
    return len(values) == 1 and values[0].lower() in allowed


def _allowed_origin(request: web.Request) -> bool:
    """CSRF check for state-changing requests.

    Browsers always send Origin on cross-site POST/PUT/DELETE, so a missing
    Origin can only come from a local non-browser client and is allowed.
    """
    if request.method in _SAFE_METHODS:
        return True
    values = request.headers.getall("Origin", [])
    if not values:
        return True
    allowed = request.app.get(_ALLOWED_ORIGINS_KEY, frozenset())
    return len(values) == 1 and values[0].lower() in allowed


@web.middleware
async def _console_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        if not _allowed_host(request):
            return _json({"error": "forbidden_host"}, 403)
        if not _allowed_origin(request):
            return _json({"error": "forbidden_origin"}, 403)
        return await handler(request)
    except EmotionConfigError as exc:
        return _json({"error": exc.message}, exc.status)
    except web.HTTPRequestEntityTooLarge:
        return _json({"error": "request body too large"}, 413)
    except Exception as exc:
        try:
            request.app[_LOGGER_KEY].error(
                f"[STBridge] Console request failed ({type(exc).__name__})"
            )
        except Exception:
            pass
        return _json({"error": "internal_error"}, 500)


async def _json_body(request: web.Request) -> dict[str, Any]:
    body = await request.read()
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmotionConfigError(400, "invalid JSON") from exc
    if not isinstance(value, dict):
        raise EmotionConfigError(400, "JSON object required")
    return value


async def _multipart(request: web.Request) -> dict[str, Any]:
    if not request.content_type.startswith("multipart/form-data"):
        raise EmotionConfigError(415, "multipart/form-data required")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise EmotionConfigError(413, "request body too large")
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError) as exc:
        raise EmotionConfigError(400, "invalid multipart body") from exc

    result: dict[str, Any] = {}
    total = 0
    while True:
        part = await reader.next()
        if part is None:
            break
        if not isinstance(part, BodyPartReader):
            raise EmotionConfigError(400, "nested multipart is not supported")
        chunks: list[bytes] = []
        while True:
            chunk = await part.read_chunk(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise EmotionConfigError(413, "request body too large")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not part.name:
            continue
        if part.filename:
            entry = {"filename": part.filename, "content": content}
            previous = result.get(part.name)
            if isinstance(previous, list):
                previous.append(entry)
            elif isinstance(previous, dict):
                # A repeated file field (multi-select upload) becomes a list.
                result[part.name] = [previous, entry]
            else:
                result[part.name] = entry
        else:
            try:
                result[part.name] = content.decode(part.get_charset("utf-8"))
            except (LookupError, UnicodeDecodeError) as exc:
                raise EmotionConfigError(400, "invalid form field") from exc
    return result


def _static_file(path: Path, content_type: str) -> web.Response:
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise EmotionConfigError(404, "file not found") from exc
    return web.Response(body=body, headers={"Content-Type": content_type})


async def _index(request: web.Request) -> web.Response:
    return _static_file(request.app[_FRONTEND_KEY] / "index.html", "text/html; charset=utf-8")


# Fixed types for the assets we ship: ES module scripts are refused by browsers unless the
# type is JavaScript, and mimetypes reads the Windows registry (".js" may be text/plain there).
_STATIC_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


def _static_content_type(name: str) -> str:
    fixed = _STATIC_CONTENT_TYPES.get(Path(name).suffix.lower())
    return fixed or mimetypes.guess_type(name)[0] or "application/octet-stream"


async def _static(request: web.Request) -> web.Response:
    frontend = request.app[_FRONTEND_KEY].resolve()
    candidate = (frontend / request.match_info["tail"]).resolve()
    if frontend not in candidate.parents or not candidate.is_file():
        raise EmotionConfigError(404, "asset not found")
    return _static_file(candidate, _static_content_type(candidate.name))


async def _health(_: web.Request) -> web.Response:
    return _json({"ok": True})


async def _get_config(request: web.Request) -> web.Response:
    return _json(request.app[_STORE_KEY].public())


async def _get_config_defaults(request: web.Request) -> web.Response:
    """The factory settings, read-only: what a module's "恢复默认" button fills in (nothing user-specific)."""
    result = default_config()
    if isinstance(result.get("cover"), dict):
        result["cover"] = public_cover_settings(result["cover"])
    return _json(result)


async def _list_models(request: web.Request) -> web.Response:
    return _json({"models": request.app[_STORE_KEY].models()})


async def _save_model(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    name = safe_name(data.get("name"), "model name")
    gpt = clean_path(data.get("gpt_model_path"), "gpt_model_path")
    sovits = clean_path(data.get("sovits_model_path"), "sovits_model_path")
    if gpt and not gpt.lower().endswith(".ckpt"):
        raise EmotionConfigError(400, "GPT model path must end with .ckpt")
    if sovits and not sovits.lower().endswith(".pth"):
        raise EmotionConfigError(400, "SoVITS model path must end with .pth")
    reference_root = clean_path(data.get("reference_audio_root"), "reference_audio_root")

    def update(config):
        config["models"][name] = {
            "name": name,
            "gpt_model_path": gpt,
            "sovits_model_path": sovits,
            "reference_audio_root": reference_root,
        }
        config.setdefault("default_character", "")

    await store.update(update)
    return _json({"model": store.model_status(store.config["models"][name])})


async def _delete_model(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    name = safe_name(request.match_info["name"], "model name")

    def update(config):
        if name not in config["models"]:
            raise EmotionConfigError(404, "model not found")
        del config["models"][name]
        for character in config["characters"].values():
            if character.get("model_name") == name:
                character["model_name"] = ""

    await store.update(update)
    return _json({"status": "deleted"})


async def _list_characters(request: web.Request) -> web.Response:
    return _json({"characters": list(request.app[_STORE_KEY].config["characters"].values())})


async def _save_character(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    name = safe_name(data.get("name"), "character name")
    model_name = data.get("model_name", "")
    if not isinstance(model_name, str):
        raise EmotionConfigError(400, "invalid model_name")
    if model_name and model_name not in store.config["models"]:
        raise EmotionConfigError(400, "model not found")
    default = safe_emotion(data.get("default_emotion", "neutral"))
    existing = store.config["characters"].get(name, {})
    nickname = validate_nickname(data.get("nickname", existing.get("nickname") or default_nickname(name)))
    st_name = validate_st_name(data.get("st_name", existing.get("st_name") or default_st_name(name)))
    emotions = existing.get("emotions", {})
    if isinstance(data.get("emotions"), dict):
        emotions = data["emotions"]
    validated = {
        safe_emotion(emotion): validate_emotion_profile(profile)
        for emotion, profile in emotions.items()
    }

    def update(config):
        config["characters"][name] = {
            "name": name,
            "model_name": model_name.strip(),
            "default_emotion": default,
            "nickname": nickname,
            "st_name": st_name,
            "emotions": validated,
        }
        if not config.get("default_character"):
            config["default_character"] = name

    await store.update(update)
    return _json({"character": store.config["characters"][name]})


async def _delete_character(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    name = safe_name(request.match_info["name"], "character name")

    def update(config):
        if name not in config["characters"]:
            raise EmotionConfigError(404, "character not found")
        del config["characters"][name]
        if config.get("default_character") == name:
            config["default_character"] = next(iter(config["characters"]), "")

    await store.update(update)
    return _json({"status": "deleted"})


async def _save_emotion(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    character_name = safe_name(request.match_info["name"], "character name")
    emotion = safe_emotion(request.match_info["emotion"])
    profile = validate_emotion_profile(await _json_body(request))

    def update(config):
        character = config["characters"].get(character_name)
        if character is None:
            raise EmotionConfigError(404, "character not found")
        character.setdefault("emotions", {})[emotion] = profile

    await store.update(update)
    return _json({"emotion": profile})


async def _delete_emotion(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    character_name = safe_name(request.match_info["name"], "character name")
    emotion = safe_emotion(request.match_info["emotion"])

    def update(config):
        character = config["characters"].get(character_name)
        if character is None:
            raise EmotionConfigError(404, "character not found")
        if emotion not in character.get("emotions", {}):
            raise EmotionConfigError(404, "emotion not found")
        if emotion == character.get("default_emotion"):
            raise EmotionConfigError(400, "cannot delete the default emotion")
        del character["emotions"][emotion]

    await store.update(update)
    return _json({"status": "deleted"})


async def _save_defaults(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    character_name = (
        safe_name(data.get("character", ""), "character name")
        if data.get("character")
        else ""
    )
    emotion = safe_emotion(data.get("emotion", "neutral"))

    def update(config):
        if character_name and character_name not in config["characters"]:
            raise EmotionConfigError(404, "character not found")
        config["default_character"] = character_name
        config["default_emotion"] = emotion

    await store.update(update)
    return _json({"status": "saved"})


async def _save_gpt_sovits_root(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    root = clean_path(data.get("path"), "gpt_sovits_root")
    if root and not await asyncio.to_thread(Path(root).expanduser().is_dir):
        raise EmotionConfigError(400, "directory not found")

    def update(config):
        config["gpt_sovits_root"] = root

    await store.update(update)
    return _json({"gpt_sovits_root": root})


async def _browse_filesystem(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    path = request.query.get("path", "")
    mode = request.query.get("mode", "dir")

    def run():
        start = None if path.strip() else start_directory(store.config)
        return browse(path, mode, start=start)

    return _json(await asyncio.to_thread(run))


async def _list_audio(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    character_name = safe_name(request.query.get("character", ""), "character name")
    character = store.config["characters"].get(character_name)
    if not character:
        raise EmotionConfigError(404, "character not found")
    return _json({"audios": store.list_audio(character.get("model_name", ""))})


async def _upload_audio(request: web.Request) -> web.Response:
    """Save to <root>/<emotion>/<reference text>.<ext>; discovery picks it up."""
    store = request.app[_STORE_KEY]
    fields = await _multipart(request)
    character_name = safe_name(fields.get("character", ""), "character name")
    character = store.config["characters"].get(character_name)
    if not character or not character.get("model_name"):
        raise EmotionConfigError(400, "character must be bound to a model")
    model_name = character["model_name"]
    emotion = safe_emotion(fields.get("emotion", "neutral"))
    file_info = fields.get("file")
    if not isinstance(file_info, dict) or not file_info.get("filename"):
        raise EmotionConfigError(400, "audio file required")
    original = os.path.basename(str(file_info["filename"]).replace("\\", "/"))
    suffix = Path(original).suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise EmotionConfigError(400, "unsupported audio format")
    prompt_text = fields.get("prompt_text", "")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        prompt_text = Path(original).stem
    model_root = store.model_audio_root(model_name)
    directory = model_root / emotion
    directory.mkdir(parents=True, exist_ok=True)
    destination = unique_audio_path(directory, safe_audio_stem(prompt_text), suffix)
    await asyncio.to_thread(destination.write_bytes, file_info["content"])
    return _json(
        {
            "path": str(destination),
            "relative_path": destination.relative_to(model_root).as_posix(),
        }
    )


async def _discovered_emotions(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    character_name = safe_name(request.query.get("character", ""), "character name")
    character = store.config["characters"].get(character_name)
    if not character:
        raise EmotionConfigError(404, "character not found")
    model_name = character.get("model_name", "")
    emotions = store.discovered(model_name) if model_name else []
    return _json({"model": model_name, "emotions": emotions})


async def _delete_audio(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    model_name = safe_name(request.query.get("model", ""), "model name")
    audio_path = store.resolve_audio(model_name, request.query.get("path", ""))
    audio_path.unlink()
    return _json({"status": "deleted"})


async def _stream_audio(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    model_name = safe_name(request.query.get("model", ""), "model name")
    audio_path = store.resolve_audio(model_name, request.query.get("path", ""))
    content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    return _static_file(audio_path, content_type)


async def _defaults(_: web.Request) -> web.Response:
    return _json({"emotion": default_emotion(), "languages": sorted(LANGUAGES)})


def _affinity_store(request: web.Request) -> AffinityStore:
    store = request.app[_AFFINITY_KEY]
    if store is None:
        raise EmotionConfigError(503, "affinity unavailable")
    return store


def _track_ranges(settings: dict[str, Any]) -> dict[str, tuple[int, int]]:
    ranges = {}
    for track in (OWNER_TRACK, GROUP_TRACK):
        info = affinity_track(settings, track)
        ranges[track] = (info["min"], info["max"])
    return ranges


async def _get_affinity(request: web.Request) -> web.Response:
    settings = affinity_settings(request.app[_STORE_KEY].config)
    users = _affinity_store(request).users(_track_ranges(settings))
    for user in users:
        user["tier"] = affinity_tier_name(affinity_track(settings, user["track"]), user["value"])
    return _json({"settings": settings, "users": users})


async def _save_affinity_settings(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_affinity_settings(await _json_body(request))
    # The character names live on the characters now (情绪 page); they are never stored here.
    del settings["character"], settings["display_name"]

    def update(config):
        config["affinity"] = settings

    await store.update(update)
    return _json({"settings": affinity_settings(store.config)})


async def _set_affinity_value(request: web.Request) -> web.Response:
    affinity = _affinity_store(request)
    data = await _json_body(request)
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise EmotionConfigError(400, "invalid affinity value")
    user_id = request.match_info["user_id"]
    settings = affinity_settings(request.app[_STORE_KEY].config)
    # The value is checked against the range of the track the user belongs to.
    key = await asyncio.to_thread(
        affinity.set_value_by_hash, user_id, value, _track_ranges(settings)
    )
    track = OWNER_TRACK if affinity.is_owner(key) else GROUP_TRACK
    return _json(
        {
            "id": user_id,
            "track": track,
            "value": value,
            "tier": affinity_tier_name(affinity_track(settings, track), value),
        }
    )


def _mood_view(request: web.Request) -> dict[str, Any]:
    moods = request.app[_MOOD_KEY]
    settings = mood_settings(request.app[_STORE_KEY].config)
    view: dict[str, Any] = {"settings": settings, "available": moods is not None}
    for track in ("global", "owner"):
        if moods is None:
            view[track] = {"text": "", "count": 0}
        else:
            view[track] = {"text": moods.text(track, settings), "count": len(moods.entries(track))}
    return view


async def _get_mood(request: web.Request) -> web.Response:
    return _json(_mood_view(request))


async def _save_mood_settings(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_mood_settings(await _json_body(request))

    def update(config):
        config["mood"] = settings

    await store.update(update)
    return _json(_mood_view(request))


async def _reset_mood(request: web.Request) -> web.Response:
    moods = request.app[_MOOD_KEY]
    if moods is None:
        raise EmotionConfigError(503, "mood unavailable")
    track = (await _json_body(request)).get("track")
    if track not in ("global", "owner"):
        raise EmotionConfigError(400, "invalid mood list")
    await asyncio.to_thread(moods.clear, track)
    return _json(_mood_view(request))


async def _test_chat(request: web.Request) -> web.Response:
    """A1: run one message through the normal chat flow with no side effect (the core does the work)."""
    runner = request.app[_TEST_CHAT_KEY]
    if runner is None:
        raise EmotionConfigError(503, "test chat unavailable")
    if request.content_type != "application/json":
        raise EmotionConfigError(415, "application/json required")
    result = await runner(await _json_body(request))
    if isinstance(result, dict) and result.get("result") == "ok" and result.get("replies"):
        await _mark_onboarding(request, {"tested": True})
    return _json(result)


async def _mark_onboarding(request: web.Request, changes: dict[str, bool]) -> dict[str, bool]:
    """Set checklist flags in the config (only writes when something changes); returns the flags."""
    store = request.app[_STORE_KEY]
    current = console_settings(store.config)["onboarding"]
    wanted = validate_onboarding({**current, **changes})
    if wanted != current:
        def update(config):
            config["console"] = {**console_settings(config), "onboarding": wanted}

        await store.update(update)
    return wanted


async def _get_onboarding(request: web.Request) -> web.Response:
    return _json({"onboarding": console_settings(request.app[_STORE_KEY].config)["onboarding"]})


async def _save_onboarding(request: web.Request) -> web.Response:
    """The checklist's own flags: tested (set by a good test chat), voice_skipped, hidden, completed."""
    data = await _json_body(request)
    if not data or any(not isinstance(value, bool) for value in data.values()):
        raise EmotionConfigError(400, "invalid onboarding")
    unknown = set(data) - set(ONBOARDING_KEYS)
    if unknown:
        raise EmotionConfigError(400, "unknown onboarding key")
    return _json({"onboarding": await _mark_onboarding(request, data)})


def _recovery_notices(request: web.Request) -> list[dict[str, Any]]:
    """Data files that were unusable and replaced by a backup or defaults (101e); shown as a red bar."""
    notices: list[dict[str, Any]] = []
    store = request.app[_STORE_KEY]
    if getattr(store, "recovery", None):
        notices.append({"file": "config.json", **store.recovery})
    affinity = request.app[_AFFINITY_KEY]
    if affinity is not None and getattr(affinity, "recovery", None):
        notices.append({"file": "affinity.json", **affinity.recovery})
    return notices


async def _dismiss_recovery(request: web.Request) -> web.Response:
    """The red bar's "知道了" button: forget the notices (the renamed broken files stay on disk)."""
    request.app[_STORE_KEY].recovery = None
    affinity = request.app[_AFFINITY_KEY]
    if affinity is not None:
        affinity.recovery = None
    return _json({"data_recovery": []})


async def _get_status(request: web.Request) -> web.Response:
    provider = request.app[_BRIDGE_STATUS_KEY]
    wake = request.app[_WAKE_PREFIX_KEY]
    try:
        prefixes = wake() if wake is not None else None
    except Exception:
        prefixes = None
    connector = request.app[_CONNECTOR_STATUS_KEY]
    try:
        connected = connector() if connector is not None else None
    except Exception:
        connected = None
    stats = request.app[_REPLY_STATS_KEY]
    try:
        replies = stats() if stats is not None else None
    except Exception:
        replies = None
    today_provider = request.app[_TODAY_STATS_KEY]
    try:
        today = today_provider() if today_provider is not None else None
    except Exception:
        today = None
    return _json({
        "bridge": provider() if provider is not None else None,
        "replies": replies,
        "today": today,
        "wake_prefix": prefixes,
        "connector": connected,
        "platform": chat_platform_settings(request.app[_STORE_KEY].config)["active"],
        "onebot": _onebot_status(request),
        "data_recovery": _recovery_notices(request),
    })


def _onebot_info(request: web.Request) -> dict[str, Any] | None:
    provider = request.app[_ONEBOT_INFO_KEY]
    try:
        info = provider() if provider is not None else None
    except Exception:
        return None
    return info if isinstance(info, dict) and info else None


def _onebot_status(request: web.Request) -> dict[str, Any] | None:
    info = _onebot_info(request)
    return info.get("status") if info is not None else None


def _chat_platform_payload(request: web.Request, settings: dict[str, Any]) -> dict[str, Any]:
    """The platform choice and what NapCat needs: URL and token (the console is reachable from this computer only)."""
    info = _onebot_info(request) or {}
    return {
        "settings": settings,
        "onebot": {"url": info.get("url"), "token": info.get("token"), "port": info.get("port"), "status": info.get("status")},
        "wake_prefix": None,
    }


async def _get_chat_platform(request: web.Request) -> web.Response:
    payload = _chat_platform_payload(request, chat_platform_settings(request.app[_STORE_KEY].config))
    wake = request.app[_WAKE_PREFIX_KEY]
    try:
        payload["wake_prefix"] = wake() if wake is not None else None
    except Exception:
        payload["wake_prefix"] = None
    return _json(payload)


async def _save_chat_platform(request: web.Request) -> web.Response:
    """Platform, group whitelist and NapCat owner; a switch takes effect at once (the core reads it live)."""
    store = request.app[_STORE_KEY]
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise EmotionConfigError(400, "chat_platform settings must be an object")
    settings = validate_chat_platform_settings(body)

    def update(config):
        config["chat_platform"] = settings

    await store.update(update)
    return _json(_chat_platform_payload(request, settings))


def _network_payload(request: web.Request, settings: dict[str, int]) -> dict[str, Any]:
    provider = request.app[_NETWORK_IN_USE_KEY]
    in_use = provider() if provider is not None else None
    return {
        "settings": settings,
        "in_use": in_use,
        # The ports are read when the plugin starts: a saved change waits for the next reload.
        "reload_required": in_use is not None and in_use != settings,
        "console_url": f"http://127.0.0.1:{settings['console_port']}",
    }


async def _get_network(request: web.Request) -> web.Response:
    return _json(_network_payload(request, network_settings(request.app[_STORE_KEY].config)))


async def _save_network(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    body = await _json_body(request)
    settings = validate_network_settings(body, tts=store.config.get("tts"), cover=store.config.get("cover"))

    def update(config):
        config["network"] = settings

    await store.update(update)
    return _json(_network_payload(request, settings))


async def _get_logs(request: web.Request) -> web.Response:
    """The latest lines of today's core log (100 / 300 / 1000 lines)."""
    data_dir = request.app[_STORE_KEY].data_dir
    lines = clean_line_count(request.query.get("lines"))
    return _json({
        "lines": tail_lines(data_dir, lines),
        "count": lines,
        "choices": list(LINE_CHOICES),
        "files": [path.name for path in log_files(data_dir)],
    })


async def _download_logs(request: web.Request) -> web.Response:
    """Every kept log file (today and the six days before) in one zip."""
    body = await asyncio.to_thread(zip_logs, request.app[_STORE_KEY].data_dir)
    return web.Response(
        body=body,
        content_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="st-bridge-logs.zip"'},
    )


async def _get_owner(request: web.Request) -> web.Response:
    return _json({"owner": owner_settings(request.app[_STORE_KEY].config)})


async def _save_owner(request: web.Request) -> web.Response:
    """The owner takes effect at once; saving also marks the one-time import as done."""
    store = request.app[_STORE_KEY]
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise EmotionConfigError(400, "owner settings must be an object")
    settings = validate_owner_settings({"platform_id": body.get("platform_id"), "sender_id": body.get("sender_id"), "imported": True})

    def update(config):
        config["owner"] = settings

    await store.update(update)
    return _json({"owner": settings})


async def _get_owner_claim(request: web.Request) -> web.Response:
    """The claim state the console polls: idle / pending (code + seconds left) / claimed / expired / failed."""
    status = request.app[_CLAIM_STATUS_KEY]
    if status is None:
        raise EmotionConfigError(503, "owner claim unavailable")
    return _json(status())


async def _start_owner_claim(request: web.Request) -> web.Response:
    """A new 6-digit code, valid for 5 minutes; the previous code stops working."""
    start = request.app[_CLAIM_START_KEY]
    if start is None:
        raise EmotionConfigError(503, "owner claim unavailable")
    return _json(start())


def _injection_view(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "settings": injection_settings(config),
        "defaults": default_injection_settings(),
        "macros": [{"key": key, "description": MACRO_DESCRIPTIONS[key]} for key in MACRO_KEYS],
    }


async def _get_st_injection(request: web.Request) -> web.Response:
    return _json(_injection_view(request.app[_STORE_KEY].config))


async def _save_st_injection(request: web.Request) -> web.Response:
    """The entry list the ST extension writes into the preset / world book when its sync button is pressed."""
    store = request.app[_STORE_KEY]
    settings = validate_injection_settings(await _json_body(request))

    def update(config):
        config["st_injection"] = settings

    await store.update(update)
    return _json(_injection_view(store.config))


async def _get_texts(request: web.Request) -> web.Response:
    config = request.app[_STORE_KEY].config
    return _json({
        "texts": texts_settings(config),
        "defaults": default_texts(),
        "meta": text_meta(),
        "nickname": active_identity(config)["nickname"],
        # Set by the upgrade when it had to replace a text; a save from the card clears it.
        "notice": config.get("texts_notice"),
    })


async def _save_texts(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    texts = validate_texts(await _json_body(request))

    def update(config):
        config["texts"] = texts
        config.pop("texts_notice", None)

    await store.update(update)
    return _json({"texts": texts})


async def _get_queue(request: web.Request) -> web.Response:
    settings = queue_settings(request.app[_STORE_KEY].config)
    provider = request.app[_QUEUE_STATUS_KEY]
    status = provider() if provider is not None else {"active": False, "waiting": []}
    return _json({
        "settings": settings,
        "active": status["active"],
        "waiting_count": len(status["waiting"]),
        "waiting": status["waiting"],
    })


async def _save_queue_settings(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_queue_settings(await _json_body(request))

    def update(config):
        config["queue"] = settings

    await store.update(update)
    return _json({"settings": settings})


def _sticker_store(request: web.Request) -> StickerStore:
    store = request.app[_STICKER_KEY]
    if store is None:
        raise EmotionConfigError(503, "stickers unavailable")
    return store


def _sticker_emotions(config: dict[str, Any]) -> list[str]:
    """Emotion names of the default character: the groups the sticker page shows."""
    characters = config.get("characters", {})
    character = characters.get(config.get("default_character", ""))
    if character is None and characters:
        character = next(iter(characters.values()))
    return list((character or {}).get("emotions", {}))


async def _get_stickers(request: web.Request) -> web.Response:
    store = _sticker_store(request)
    config = request.app[_STORE_KEY].config
    groups = await asyncio.to_thread(store.groups, _sticker_emotions(config))
    return _json({
        "settings": sticker_settings(config),
        "max_bytes": MAX_STICKER_BYTES,
        "groups": groups,
    })


async def _save_sticker_settings(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_sticker_settings(await _json_body(request))

    def update(config):
        config["stickers"] = settings

    await store.update(update)
    return _json({"settings": settings})


async def _upload_stickers(request: web.Request) -> web.Response:
    """One or more images (multipart field `file`, repeatable) into one emotion group."""
    stickers = _sticker_store(request)
    fields = await _multipart(request)
    emotion = safe_emotion(fields.get("emotion", ""))
    files = fields.get("file")
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list) or not files:
        raise EmotionConfigError(400, "image file required")
    saved, rejected = [], []
    for info in files:
        try:
            path = await asyncio.to_thread(
                stickers.add, emotion, info.get("filename"), info["content"]
            )
            saved.append(path.name)
        except EmotionConfigError as error:
            rejected.append({
                "filename": os.path.basename(str(info.get("filename", "")).replace("\\", "/")),
                "error": error.message,
            })
    if not saved:
        raise EmotionConfigError(400, rejected[0]["error"])
    return _json({"saved": saved, "rejected": rejected})


async def _delete_sticker(request: web.Request) -> web.Response:
    stickers = _sticker_store(request)
    emotion = safe_emotion(request.query.get("emotion", ""))
    path = stickers.resolve(emotion, request.query.get("name", ""))
    await asyncio.to_thread(path.unlink)
    return _json({"status": "deleted"})


async def _stream_sticker(request: web.Request) -> web.Response:
    stickers = _sticker_store(request)
    emotion = safe_emotion(request.query.get("emotion", ""))
    path = stickers.resolve(emotion, request.query.get("name", ""))
    return _static_file(path, sticker_content_type(path))


def _public_plan(request: web.Request, plan: dict[str, Any]) -> dict[str, Any]:
    scheduler = request.app[_SCHEDULER_KEY]
    item = dict(plan)
    next_fire = scheduler.next_fire(plan) if scheduler is not None and plan["enabled"] else None
    item["next_fire"] = next_fire.isoformat(timespec="minutes") if next_fire is not None else None
    item["running"] = bool(scheduler is not None and scheduler.is_running(plan["id"]))
    return item


def _chat_view(request: web.Request, section: dict[str, Any]) -> dict[str, Any]:
    chat = request.app[_CHAT_SCHEDULER_KEY]
    return {
        "settings": section["proactive_chat"],
        "status": chat.snapshot() if chat is not None else None,
        "available": chat is not None,
    }


async def _get_schedule_chat(request: web.Request) -> web.Response:
    return _json(_chat_view(request, schedule_section(request.app[_STORE_KEY].config)))


async def _save_schedule_chat(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_proactive_chat(await _json_body(request))

    def update(config):
        section = schedule_section(config)
        section["proactive_chat"] = settings
        config["schedule"] = section

    await store.update(update)
    return _json(_chat_view(request, schedule_section(store.config)))


async def _test_schedule_chat(request: web.Request) -> web.Response:
    """One owner-chat message now (ignores time, daily limit and silence; still goes through the queue)."""
    chat = request.app[_CHAT_SCHEDULER_KEY]
    if chat is None:
        raise EmotionConfigError(503, "scheduler unavailable")
    try:
        result = await chat.run_now()
    except RuntimeError as error:
        raise EmotionConfigError(409, "proactive chat is already running") from error
    return _json({"result": result})


async def _get_schedule(request: web.Request) -> web.Response:
    section = schedule_section(request.app[_STORE_KEY].config)
    targets = request.app[_SCHEDULE_TARGETS_KEY]
    return _json({
        "chat": _chat_view(request, section),
        "settings": section["settings"],
        "plans": [_public_plan(request, plan) for plan in section["plans"]],
        "targets": {
            "owner_known": bool(targets is not None and targets.has_owner),
            "groups": targets.groups() if targets is not None else [],
        },
        "templates": template_list(),
        "scheduler_available": request.app[_SCHEDULER_KEY] is not None,
    })


async def _save_schedule_settings(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    settings = validate_schedule_settings(await _json_body(request))

    def update(config):
        section = schedule_section(config)
        section["settings"] = settings
        config["schedule"] = section

    await store.update(update)
    return _json({"settings": settings})


async def _save_schedule_plan(request: web.Request) -> web.Response:
    """Create a plan (no id) or update one (id of an existing plan)."""
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    plan_id = data.get("id")
    existing = {plan["id"] for plan in schedule_section(store.config)["plans"]}
    if plan_id in (None, ""):
        plan_id = new_plan_id()
        while plan_id in existing:
            plan_id = new_plan_id()
        creating = True
    else:
        creating = False
        if plan_id not in existing:
            raise EmotionConfigError(404, "plan not found")
    plan = validate_plan({**data, "id": plan_id})
    targets = request.app[_SCHEDULE_TARGETS_KEY]
    if plan["target"]["type"] == "group" and (
        targets is None or not targets.has_group(plan["target"]["key"])
    ):
        raise EmotionConfigError(400, "unknown group")

    def update(config):
        section = schedule_section(config)
        plans = section["plans"]
        if creating:
            if len(plans) >= MAX_PLANS:
                raise EmotionConfigError(400, "too many plans")
            plans.append(plan)
        else:
            plans[:] = [plan if item["id"] == plan_id else item for item in plans]
        config["schedule"] = section

    await store.update(update)
    return _json({"plan": _public_plan(request, plan)})


async def _delete_schedule_plan(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    plan_id = request.match_info["plan_id"]

    def update(config):
        section = schedule_section(config)
        remaining = [plan for plan in section["plans"] if plan["id"] != plan_id]
        if len(remaining) == len(section["plans"]):
            raise EmotionConfigError(404, "plan not found")
        section["plans"] = remaining
        config["schedule"] = section

    await store.update(update)
    return _json({"status": "deleted"})


async def _test_schedule_plan(request: web.Request) -> web.Response:
    """Run one complete proactive turn now (ignores time, quiet rules and the plan's switch)."""
    scheduler = request.app[_SCHEDULER_KEY]
    if scheduler is None:
        raise EmotionConfigError(503, "scheduler unavailable")
    plan_id = request.match_info["plan_id"]
    plan = next(
        (item for item in schedule_section(request.app[_STORE_KEY].config)["plans"] if item["id"] == plan_id),
        None,
    )
    if plan is None:
        raise EmotionConfigError(404, "plan not found")
    try:
        result = await scheduler.run_now(plan)
    except RuntimeError as error:
        raise EmotionConfigError(409, "plan is already running") from error
    return _json({"result": result})


async def _save_schedule_target(request: web.Request) -> web.Response:
    targets = request.app[_SCHEDULE_TARGETS_KEY]
    if targets is None:
        raise EmotionConfigError(503, "scheduler unavailable")
    data = await _json_body(request)
    alias = data.get("alias", "")
    if not isinstance(alias, str) or len(alias.strip()) > MAX_ALIAS_CHARS:
        raise EmotionConfigError(400, "invalid alias")
    key = request.match_info["key"]
    if not targets.set_alias(key, alias.strip()):
        raise EmotionConfigError(404, "group not found")
    return _json({"key": key, "alias": alias.strip()})


def _cover_service(request: web.Request) -> Any:
    cover = request.app[_COVER_KEY]
    if cover is None:
        raise EmotionConfigError(503, "cover unavailable")
    return cover


def _public_song(song: dict[str, Any]) -> dict[str, Any]:
    public = {key: song[key] for key in ("id", "title", "filename", "ext", "size", "duration", "clip_start", "added")}
    public.update({key: song[key] for key in ("artist", "source", "source_id") if song.get(key)})
    return public


async def _get_cover(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    settings = cover_settings(request.app[_STORE_KEY].config)
    root = settings["rvc_root"]
    models, separation, index = await asyncio.gather(
        asyncio.to_thread(list_models, root),
        asyncio.to_thread(list_separation_models, root),
        asyncio.to_thread(auto_index, root, settings["model"]),
    )
    return _json({
        "settings": public_cover_settings(settings),
        "models": models,
        "separation_models": separation["separation"],
        "dereverb_models": separation["dereverb"],
        "auto_index": Path(index).name if index else "",
        "service": await cover.status(),
        "songs": [_public_song(song) for song in cover.library.list()],
        "covers": cover.cache.list(),
        "max_song_bytes": SONG_MAX_BYTES,
        "song_request": song_request_settings(request.app[_STORE_KEY].config),
    })


async def _save_song_request_settings(request: web.Request) -> web.Response:
    _cover_service(request)
    store = request.app[_STORE_KEY]
    settings = validate_song_request_settings(await _json_body(request))
    # The four reply texts live in the texts section (系统 › 回复文案); a body that still carries them is not an edit of them.
    for field in (*_SONG_TEXT_FIELDS, "limit_text"):
        del settings[field]

    def update(config):
        config["song_request"] = settings

    await store.update(update)
    return _json({"settings": song_request_settings(store.config)})


async def _save_cover_settings(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    store = request.app[_STORE_KEY]
    old = cover_settings(store.config)
    data = await _json_body(request)
    # The key never comes back to the browser: an empty or missing field keeps the saved one.
    if data.get("clear_music_api_key") is True:
        data["music_api_key"] = ""
    elif not data.get("music_api_key"):
        data["music_api_key"] = old["music_api_key"]
    settings = validate_cover_settings(data, reserved=reserved_ports(store.config))

    def update(config):
        config["cover"] = settings

    await store.update(update)
    stopped = await cover.apply_settings(old, settings)
    return _json({"settings": public_cover_settings(settings), "stopped": stopped})


async def _get_cover_service(request: web.Request) -> web.Response:
    return _json(await _cover_service(request).status())


async def _start_cover_service(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    try:
        await cover.start_service()
    except ServiceStartError as error:
        return _json({"error": str(error), "service": await cover.status()}, 502)
    return _json(await cover.status())


async def _stop_cover_service(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    result = await cover.stop_service()
    if not result["ok"]:
        return _json({"error": result["message"], "service": await cover.status()}, 409)
    return _json(await cover.status())


async def _upload_songs(request: web.Request) -> web.Response:
    """One or more songs (multipart field `file`, repeatable)."""
    cover = _cover_service(request)
    fields = await _multipart(request)
    files = fields.get("file")
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list) or not files:
        raise EmotionConfigError(400, "audio file required")
    saved, rejected = [], []
    for info in files:
        name = os.path.basename(str(info.get("filename", "")).replace("\\", "/"))
        try:
            song = await cover.add_song(info.get("filename"), info["content"])
            saved.append(_public_song(song))
        except EmotionConfigError as error:
            rejected.append({"filename": name, "error": error.message})
    if not saved:
        raise EmotionConfigError(400, rejected[0]["error"])
    return _json({"saved": saved, "rejected": rejected})


async def _update_song(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    data = await _json_body(request)
    kwargs: dict[str, Any] = {}
    if "title" in data:
        kwargs["title"] = data["title"]
    if "clip_start" in data:
        kwargs["clip_start"] = data["clip_start"]
    song = await asyncio.to_thread(lambda: cover.library.update(request.match_info["song_id"], **kwargs))
    return _json({"song": _public_song(song)})


async def _delete_song(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    await asyncio.to_thread(cover.library.delete, request.match_info["song_id"])
    return _json({"status": "deleted"})


async def _stream_song(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    song = cover.library.get(request.match_info["song_id"])
    if song is None:
        raise EmotionConfigError(404, "song not found")
    path = cover.library.path(song)
    return _static_file(path, audio_content_type(path))


async def _test_sing(request: web.Request) -> web.Response:
    """Sing (or reuse) a cover of one song; waits for the result."""
    cover = _cover_service(request)
    data = await _json_body(request)
    pitch = data.get("f0_up_key")
    if pitch is not None and (isinstance(pitch, bool) or not isinstance(pitch, int) or not -12 <= pitch <= 12):
        raise EmotionConfigError(400, "invalid pitch shift")
    overall = data.get("inst_pitch")
    if overall is not None and (isinstance(overall, bool) or not isinstance(overall, int) or not -12 <= overall <= 12):
        raise EmotionConfigError(400, "invalid overall pitch shift")
    try:
        outcome = await cover.sing(str(data.get("song_id", "")), f0_up_key=pitch, inst_pitch=overall)
    except CoverError as error:
        status = 404 if error.kind == "SongNotFound" else 429 if error.kind == "QueueFull" else 502
        return _json({"error": error.kind}, status)
    return _json(outcome)


def _cleanup_service(request: web.Request) -> Any:
    cleanup = request.app[_CLEANUP_KEY]
    if cleanup is None:
        raise EmotionConfigError(503, "cleanup unavailable")
    return cleanup


def _cover_cache_usage(request: web.Request) -> dict[str, int]:
    cover = request.app[_COVER_KEY]
    if cover is None:
        return {"entries": 0, "bytes": 0}
    entries, size = cover.cache.usage()
    return {"entries": entries, "bytes": size}


async def _get_cleanup(request: web.Request) -> web.Response:
    cleanup = _cleanup_service(request)
    overview = await cleanup.overview()
    return _json({
        "settings": cleanup_settings(request.app[_STORE_KEY].config),
        **overview,
        "cover_cache": await asyncio.to_thread(_cover_cache_usage, request),
        "install_backups": await asyncio.to_thread(request.app[_INSTALL_KEY].backup_usage),
    })


async def _clear_install_backups(request: web.Request) -> web.Response:
    """The 清理 page's button: only the newest backup of the plugin and of the extension stays."""
    result = await asyncio.to_thread(request.app[_INSTALL_KEY].clear_install_backups, 1)
    return _json({**result, "install_backups": await asyncio.to_thread(request.app[_INSTALL_KEY].backup_usage)})


async def _download_diagnostics(request: web.Request) -> web.Response:
    """The diagnostics zip (U5-2): recent logs, state snapshots, versions and a redacted settings summary."""
    from .console_install import _overview as install_overview  # late: that module imports this one

    store = request.app[_STORE_KEY]

    async def snapshot(producer):
        try:
            return await producer()
        except Exception as error:
            return {"error": type(error).__name__}

    async def status_part():
        return json.loads((await _get_status(request)).text)

    async def install_part():
        return await install_overview(request)

    async def tts_part():
        return await _tts_coordinator(request).status()

    async def cover_part():
        return await _cover_service(request).status()

    status = await snapshot(status_part)
    install = await snapshot(install_part)
    services = {"语音": await snapshot(tts_part), "翻唱": await snapshot(cover_part)}
    body = await asyncio.to_thread(
        build_diagnostics_zip,
        data_dir=store.data_dir, config=store.config, status=status, services=services, install=install,
    )
    return web.Response(
        body=body,
        content_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="st-bridge-diagnostics.zip"'},
    )


async def _save_cleanup_settings(request: web.Request) -> web.Response:
    _cleanup_service(request)
    store = request.app[_STORE_KEY]
    settings = validate_cleanup_settings(await _json_body(request))

    def update(config):
        config["cleanup"] = settings

    await store.update(update)
    return _json({"settings": settings})


async def _run_cleanup(request: web.Request) -> web.Response:
    return _json(await _cleanup_service(request).run("manual"))


async def _clear_cover_cache(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    try:
        outcome = await cover.clear_cache()
    except CoverError as error:
        if error.kind == "Busy":
            return _json({"error": "翻唱进行中"}, 409)
        raise
    return _json(outcome)


_ONLINE_ERROR_STATUS = {
    "not_found": 404, "MusicKeyNotSet": 400, "preview_only": 422, "invalid source": 400,
}


def _online_error(error: CoverError) -> web.Response:
    return _json({"error": error.kind}, _ONLINE_ERROR_STATUS.get(error.kind, 502))


def _online_text(data: dict[str, Any], field: str, *, required: bool = True) -> str:
    value = data.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise EmotionConfigError(400, f"invalid {field}")
    text = " ".join(str(value).split())
    if (required and not text) or len(text) > 200:
        raise EmotionConfigError(400, f"invalid {field}")
    return text


async def _online_search(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    data = await _json_body(request)
    name = _online_text(data, "name")
    source = _online_text(data, "source")
    if source != "auto" and source not in SOURCES:
        raise EmotionConfigError(400, "invalid source")
    try:
        return _json(await cover.search_online(name, source))
    except CoverError as error:
        return _online_error(error)


async def _online_download(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    data = await _json_body(request)
    source = _online_text(data, "source")
    if source not in SOURCES:
        raise EmotionConfigError(400, "invalid source")
    try:
        song = await cover.download_online(
            source, _online_text(data, "id"), _online_text(data, "name"), _online_text(data, "artist", required=False)
        )
    except CoverError as error:
        return _online_error(error)
    return _json({"song": _public_song(song)})


async def _online_fetch(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    data = await _json_body(request)
    try:
        outcome = await cover.find_or_fetch(_online_text(data, "name"), _online_text(data, "artist", required=False) or None)
    except CoverError as error:
        return _online_error(error)
    return _json({**outcome, "song": _public_song(outcome["song"])})


async def _stream_cover(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    key = request.match_info["key"]
    if cover.cache.get(key) is None:
        raise EmotionConfigError(404, "cover not found")
    return _static_file(cover.cache.path_for(key), "audio/wav")


async def _delete_cover(request: web.Request) -> web.Response:
    cover = _cover_service(request)
    await asyncio.to_thread(cover.cache.delete, request.match_info["key"])
    return _json({"status": "deleted"})


def _tts_coordinator(request: web.Request):
    tts = request.app[_TTS_KEY]
    if tts is None:
        raise EmotionConfigError(503, "tts unavailable")
    return tts


async def _get_tts(request: web.Request) -> web.Response:
    tts = _tts_coordinator(request)
    return _json({"settings": tts.public_settings(), "engine": tts.engine})


async def _save_tts_settings(request: web.Request) -> web.Response:
    tts = _tts_coordinator(request)
    store = request.app[_STORE_KEY]
    settings = validate_tts_settings(await _json_body(request), network=network_settings(store.config))

    def update(config):
        config["tts"] = settings

    await store.update(update)
    outcome = await tts.apply_settings(settings)
    return _json({"settings": tts.public_settings(), "engine": tts.engine, "stopped": outcome["stopped"]})


async def _get_tts_service(request: web.Request) -> web.Response:
    return _json(await _tts_coordinator(request).status())


async def _start_tts_service(request: web.Request) -> web.Response:
    tts = _tts_coordinator(request)
    try:
        await tts.start_service()
    except ServiceStartError as error:
        return _json({"error": str(error), "service": await tts.status()}, 502)
    return _json(await tts.status())


async def _stop_tts_service(request: web.Request) -> web.Response:
    tts = _tts_coordinator(request)
    result = await tts.stop_service()
    if not result["ok"]:
        return _json({"error": result["message"], "service": await tts.status()}, 409)
    return _json(await tts.status())


TTS_TEST_TEXT = "こんにちは、音声のテストです。"
TTS_TEST_MAX_CHARS = 200


def _wav_seconds(audio: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as handle:
            return round(handle.getnframes() / float(handle.getframerate()), 2)
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def _tts_error_details(error: BaseException) -> dict:
    """The HTTP status (and Irodori's error_type) of a failed synthesis; never the message, which can hold paths."""
    details: dict = {}
    status = getattr(error, "status", None)
    if status is None:  # httpx.HTTPStatusError from GPT-SoVITS keeps it on the response
        status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        details["status"] = status
    error_type = getattr(error, "error_type", None)
    if isinstance(error_type, str) and error_type and len(error_type) <= 80:
        details["error_type"] = error_type
    return details


async def _test_tts(request: web.Request) -> web.Response:
    """Synthesize one sentence with the current engine and report what came back (nothing is saved or sent)."""
    tts = _tts_coordinator(request)
    store = request.app[_STORE_KEY]
    raw = await request.read()
    data = await _json_body(request) if raw.strip() else {}
    text = data.get("text", TTS_TEST_TEXT)
    if not isinstance(text, str) or not text.strip() or len(text) > TTS_TEST_MAX_CHARS:
        raise EmotionConfigError(400, "invalid text")
    return_audio = data.get("return_audio", False)
    if not isinstance(return_audio, bool):
        raise EmotionConfigError(400, "invalid return_audio")
    config = store.config
    character = data.get("character")
    if character is not None:
        if not isinstance(character, str) or character not in config.get("characters", {}):
            raise EmotionConfigError(404, "character not found")
        config = {**config, "default_character": character}
    emotion = data.get("emotion")
    if emotion is not None:
        emotion = safe_emotion(emotion)
    override = data.get("emotion_profile")
    if override is not None:
        # Unsaved form values of one emotion: checked like a save, used for this synthesis only (nothing is written).
        if emotion is None:
            raise EmotionConfigError(400, "emotion required with emotion_profile")
        if not isinstance(override, dict) or set(override) - set(default_emotion()):
            raise EmotionConfigError(400, "invalid emotion_profile")
        name = config.get("default_character")
        character_data = config.get("characters", {}).get(name)
        if not isinstance(character_data, dict):
            raise EmotionConfigError(404, "character not found")
        saved = (character_data.get("emotions") or {}).get(emotion, {})
        checked = validate_emotion_profile({**(saved if isinstance(saved, dict) else {}), **override})
        config = {
            **config,
            "characters": {
                **config["characters"],
                name: {**character_data, "emotions": {**(character_data.get("emotions") or {}), emotion: checked}},
            },
        }
    selection = select_tts_profile(config, emotion, default_audio_dir=store.audio_dir)
    started = time.monotonic()
    try:
        audio = await tts.synthesize(text.strip(), selection.profile)
    except ServiceStartError as error:
        failure = {"ok": False, "error": "ServiceStartError", "message": str(error), "engine": tts.engine}
        cooldown = round(tts.start_cooldown_remaining())
        if cooldown > 0:
            failure["cooldown_seconds"] = cooldown
        return _json(failure, 502)
    except Exception as error:  # the type only: the message can hold paths
        return _json({"ok": False, "error": type(error).__name__, "engine": tts.engine, **_tts_error_details(error)}, 502)
    result = {
        "ok": True,
        "engine": tts.engine,
        "bytes": len(audio),
        "duration_seconds": _wav_seconds(audio),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "emotion": selection.emotion,
        "profile_source": selection.source,
    }
    if return_audio:
        # Only when asked (the console's 试听 button); the selftest and other callers get the same reply as before.
        result["content_type"] = "audio/wav"
        result["audio_base64"] = base64.b64encode(audio).decode("ascii")
    return _json(result)


# ---- 总预设 (whole-config presets) ---------------------------------------------------------------------


def _presets(request: web.Request) -> PresetStore:
    return request.app[_PRESETS_KEY]


async def _list_presets(request: web.Request) -> web.Response:
    return _json(_presets(request).list(request.app[_STORE_KEY].config))


async def _save_preset(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    async with request.app[_PRESETS_LOCK_KEY]:
        summary = _presets(request).save_new(data.get("name"), store.config)
    return _json({"preset": summary})


async def _update_preset(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    data = await _json_body(request)
    use_current = data.get("use_current")
    if use_current is not None and not isinstance(use_current, bool):
        raise EmotionConfigError(400, "invalid use_current")
    async with request.app[_PRESETS_LOCK_KEY]:
        summary = _presets(request).update(
            request.match_info["preset_id"],
            name=data.get("name"),
            config=store.config if use_current else None,
        )
    return _json({"preset": summary})


async def _delete_preset(request: web.Request) -> web.Response:
    async with request.app[_PRESETS_LOCK_KEY]:
        _presets(request).delete(request.match_info["preset_id"])
    return _json({"status": "deleted"})


async def _apply_preset(request: web.Request) -> web.Response:
    """Switch every setting to a preset: archive the current ones, replace them, run the save side effects."""
    store = request.app[_STORE_KEY]
    presets = _presets(request)
    async with request.app[_PRESETS_LOCK_KEY]:
        preset_id = request.match_info["preset_id"]
        document = presets.load(preset_id)
        # Refused here (400) before anything is written, so a bad preset leaves no archive behind.
        new_config = normalize_config(document["config"], store.config)
        old_cover = cover_settings(store.config)
        archive = presets.archive(store.config, protect=preset_id)

        def update(config):
            apply_to_config(config, new_config)

        await store.update(update)
        presets.mark_applied(preset_id)
        warnings: list[str] = []
        notes: list[str] = []
        tts = request.app[_TTS_KEY]
        if tts is not None:
            try:
                outcome = await tts.apply_settings(store.config["tts"])
                if outcome.get("stopped"):
                    notes.append("语音服务已停止，下次使用时按新设置启动")
            except Exception as exc:
                warnings.append(f"语音设置未能生效（{type(exc).__name__}），请在语音页检查")
        cover = request.app[_COVER_KEY]
        if cover is not None:
            try:
                if await cover.apply_settings(old_cover, cover_settings(store.config)):
                    notes.append("翻唱服务已停止，下次使用时按新设置启动")
            except Exception as exc:
                warnings.append(f"翻唱设置未能生效（{type(exc).__name__}），请在唱歌页检查")
    missing = await asyncio.to_thread(missing_paths, store.config)
    return _json({
        "status": "applied",
        "archive": archive,
        "warnings": warnings,
        "notes": notes,
        "missing_paths": missing,
        "list": presets.list(store.config),
    })


async def _export_preset(request: web.Request) -> web.Response:
    raw, name = _presets(request).export_bytes(request.match_info["preset_id"])
    quoted = urllib.parse.quote(f"{name}.json", safe="")
    response = web.Response(body=raw, content_type="application/json", charset="utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename=\"preset.json\"; filename*=UTF-8''{quoted}"
    response.headers["Cache-Control"] = "no-store"
    return response


async def _import_preset(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    if request.content_length is not None and request.content_length > MAX_IMPORT_BYTES:
        raise EmotionConfigError(413, "preset file too large")
    # StreamReader.read(n) returns whatever is buffered, not n bytes: read chunks until the end of the body.
    received = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        received.extend(chunk)
        if len(received) > MAX_IMPORT_BYTES:
            raise EmotionConfigError(413, "preset file too large")
    raw = bytes(received)
    async with request.app[_PRESETS_LOCK_KEY]:
        summary = _presets(request).import_document(raw, store.config)
        document = _presets(request).load(summary["id"])
    missing = await asyncio.to_thread(missing_paths, document["config"])
    return _json({"preset": summary, "missing_paths": missing})


async def _not_found(_: web.Request) -> web.Response:
    raise EmotionConfigError(404, "not found")


def create_console_app(
    store: ConfigStore,
    *,
    frontend_dir: Path = FRONTEND_DIR,
    logger: Any = None,
    affinity_store: AffinityStore | None = None,
    mood: Any = None,
    queue_status: Callable[[], dict[str, Any]] | None = None,
    bridge_status: Callable[[], dict[str, Any]] | None = None,
    reply_stats: Callable[[], dict[str, Any]] | None = None,
    today_stats: Callable[[], dict[str, Any]] | None = None,
    wake_prefix: Callable[[], list[str] | None] | None = None,
    connector_status: Callable[[], dict[str, Any]] | None = None,
    network_in_use: Callable[[], dict[str, int] | None] | None = None,
    onebot_info: Callable[[], dict[str, Any]] | None = None,
    tts: Any = None,
    stickers: StickerStore | None = None,
    schedule_targets: Any = None,
    scheduler: Any = None,
    chat_scheduler: Any = None,
    cover: Any = None,
    cleanup: Any = None,
    install: Any = None,
    restart: Callable[[], dict | None] | None = None,
    test_chat: Callable[[Any], Any] | None = None,
    owner_claim_start: Callable[[], dict] | None = None,
    owner_claim_status: Callable[[], dict] | None = None,
) -> web.Application:
    """Build the console app without opening a listening socket."""
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        middlewares=[_console_middleware],
    )
    app[_STORE_KEY] = store
    app[_FRONTEND_KEY] = Path(frontend_dir)
    app[_LOGGER_KEY] = logger if logger is not None else logging.getLogger(__name__)
    app[_AFFINITY_KEY] = affinity_store
    app[_MOOD_KEY] = mood
    app[_QUEUE_STATUS_KEY] = queue_status
    app[_BRIDGE_STATUS_KEY] = bridge_status
    app[_REPLY_STATS_KEY] = reply_stats
    app[_TODAY_STATS_KEY] = today_stats
    app[_WAKE_PREFIX_KEY] = wake_prefix
    app[_CONNECTOR_STATUS_KEY] = connector_status
    app[_NETWORK_IN_USE_KEY] = network_in_use
    app[_ONEBOT_INFO_KEY] = onebot_info
    app[_TTS_KEY] = tts
    app[_STICKER_KEY] = stickers if stickers is not None else StickerStore(store.data_dir)
    app[_SCHEDULE_TARGETS_KEY] = schedule_targets
    app[_SCHEDULER_KEY] = scheduler
    app[_CHAT_SCHEDULER_KEY] = chat_scheduler
    app[_COVER_KEY] = cover
    app[_CLEANUP_KEY] = cleanup
    app[_INSTALL_KEY] = install if install is not None else InstallService(store.data_dir)
    app[_RESTART_KEY] = restart
    app[_TEST_CHAT_KEY] = test_chat
    app[_CLAIM_START_KEY] = owner_claim_start
    app[_CLAIM_STATUS_KEY] = owner_claim_status
    app[_PRESETS_KEY] = PresetStore(store.data_dir)
    app[_PRESETS_LOCK_KEY] = asyncio.Lock()

    router = app.router
    router.add_get("/", _index, allow_head=False)
    router.add_get("/static/{tail:.+}", _static, allow_head=False)
    router.add_get("/api/health", _health, allow_head=False)
    router.add_get("/api/config", _get_config, allow_head=False)
    router.add_post("/api/recovery/dismiss", _dismiss_recovery)
    router.add_get("/api/onboarding", _get_onboarding, allow_head=False)
    router.add_post("/api/onboarding", _save_onboarding)
    router.add_get("/api/config/defaults", _get_config_defaults, allow_head=False)
    router.add_post("/api/config/defaults", _save_defaults)
    router.add_post("/api/config/gpt_sovits_root", _save_gpt_sovits_root)
    router.add_get("/api/fs/list", _browse_filesystem, allow_head=False)
    router.add_get("/api/affinity", _get_affinity, allow_head=False)
    router.add_post("/api/affinity/settings", _save_affinity_settings)
    router.add_put("/api/affinity/users/{user_id}", _set_affinity_value)
    router.add_get("/api/mood", _get_mood, allow_head=False)
    router.add_post("/api/mood/settings", _save_mood_settings)
    router.add_post("/api/mood/reset", _reset_mood)
    router.add_get("/api/status", _get_status, allow_head=False)
    router.add_post("/api/test/chat", _test_chat)
    router.add_get("/api/st-injection", _get_st_injection, allow_head=False)
    router.add_post("/api/st-injection", _save_st_injection)
    router.add_get("/api/texts", _get_texts, allow_head=False)
    router.add_get("/api/network", _get_network, allow_head=False)
    router.add_post("/api/network", _save_network)
    router.add_get("/api/logs", _get_logs, allow_head=False)
    router.add_get("/api/logs/download", _download_logs, allow_head=False)
    router.add_get("/api/owner", _get_owner, allow_head=False)
    router.add_post("/api/owner", _save_owner)
    router.add_get("/api/owner/claim", _get_owner_claim, allow_head=False)
    router.add_post("/api/owner/claim", _start_owner_claim)
    router.add_get("/api/chat-platform", _get_chat_platform, allow_head=False)
    router.add_post("/api/chat-platform", _save_chat_platform)
    router.add_post("/api/texts", _save_texts)
    router.add_get("/api/queue", _get_queue, allow_head=False)
    router.add_post("/api/queue/settings", _save_queue_settings)
    router.add_get("/api/tts", _get_tts, allow_head=False)
    router.add_post("/api/tts/settings", _save_tts_settings)
    router.add_get("/api/tts/service", _get_tts_service, allow_head=False)
    router.add_post("/api/tts/service/start", _start_tts_service)
    router.add_post("/api/tts/service/stop", _stop_tts_service)
    router.add_post("/api/tts/test", _test_tts)
    router.add_get("/api/stickers", _get_stickers, allow_head=False)
    router.add_post("/api/stickers/settings", _save_sticker_settings)
    router.add_post("/api/stickers/upload", _upload_stickers)
    router.add_delete("/api/stickers", _delete_sticker)
    router.add_get("/api/stickers/file", _stream_sticker, allow_head=False)
    router.add_get("/api/schedule", _get_schedule, allow_head=False)
    router.add_post("/api/schedule/settings", _save_schedule_settings)
    router.add_get("/api/schedule/chat", _get_schedule_chat, allow_head=False)
    router.add_post("/api/schedule/chat", _save_schedule_chat)
    router.add_post("/api/schedule/chat/test", _test_schedule_chat)
    router.add_post("/api/schedule/plans", _save_schedule_plan)
    router.add_delete("/api/schedule/plans/{plan_id}", _delete_schedule_plan)
    router.add_post("/api/schedule/plans/{plan_id}/test", _test_schedule_plan)
    router.add_post("/api/schedule/targets/{key}", _save_schedule_target)
    router.add_get("/api/cover", _get_cover, allow_head=False)
    router.add_post("/api/cover/settings", _save_cover_settings)
    router.add_post("/api/cover/song-request/settings", _save_song_request_settings)
    router.add_get("/api/cover/service", _get_cover_service, allow_head=False)
    router.add_post("/api/cover/service/start", _start_cover_service)
    router.add_post("/api/cover/service/stop", _stop_cover_service)
    router.add_post("/api/cover/songs/upload", _upload_songs)
    router.add_post("/api/cover/songs/{song_id}", _update_song)
    router.add_delete("/api/cover/songs/{song_id}", _delete_song)
    router.add_get("/api/cover/songs/{song_id}/audio", _stream_song, allow_head=False)
    router.add_post("/api/cover/test", _test_sing)
    router.add_get("/api/cleanup", _get_cleanup, allow_head=False)
    router.add_post("/api/cleanup/run", _run_cleanup)
    router.add_post("/api/cleanup/settings", _save_cleanup_settings)
    router.add_post("/api/cleanup/covers/clear", _clear_cover_cache)
    router.add_post("/api/cleanup/install-backups/clear", _clear_install_backups)
    router.add_get("/api/diagnostics/download", _download_diagnostics, allow_head=False)
    router.add_post("/api/cover/online/search", _online_search)
    router.add_post("/api/cover/online/download", _online_download)
    router.add_post("/api/cover/online/fetch", _online_fetch)
    router.add_get("/api/cover/covers/{key}/audio", _stream_cover, allow_head=False)
    router.add_delete("/api/cover/covers/{key}", _delete_cover)
    router.add_get("/api/presets", _list_presets, allow_head=False)
    router.add_post("/api/presets", _save_preset)
    router.add_post("/api/presets/import", _import_preset)
    router.add_put("/api/presets/{preset_id}", _update_preset)
    router.add_delete("/api/presets/{preset_id}", _delete_preset)
    router.add_post("/api/presets/{preset_id}/apply", _apply_preset)
    router.add_get("/api/presets/{preset_id}/export", _export_preset, allow_head=False)
    router.add_get("/api/defaults", _defaults, allow_head=False)
    router.add_get("/api/models", _list_models, allow_head=False)
    router.add_post("/api/models", _save_model)
    router.add_delete("/api/models/{name}", _delete_model)
    router.add_get("/api/characters", _list_characters, allow_head=False)
    router.add_post("/api/characters", _save_character)
    router.add_delete("/api/characters/{name}", _delete_character)
    router.add_put("/api/characters/{name}/emotions/{emotion}", _save_emotion)
    router.add_post("/api/characters/{name}/emotions/{emotion}", _save_emotion)
    router.add_delete("/api/characters/{name}/emotions/{emotion}", _delete_emotion)
    router.add_get("/api/audio", _list_audio, allow_head=False)
    router.add_delete("/api/audio", _delete_audio)
    router.add_post("/api/audio/upload", _upload_audio)
    router.add_get("/api/audio/stream", _stream_audio, allow_head=False)
    router.add_get("/api/emotions/discovered", _discovered_emotions, allow_head=False)
    from .console_install import register_install_routes  # late: that module imports this one

    register_install_routes(router)
    router.add_route("*", "/{tail:.*}", _not_found)
    return app


class ConsoleServiceState(str, Enum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class ConsoleService:
    """Own one loopback runner/site pair for the console app."""

    def __init__(
        self,
        app: web.Application,
        *,
        port: int = CONSOLE_LISTEN_PORT,
        runner_factory: Callable[..., web.AppRunner] = web.AppRunner,
        site_factory: Callable[..., web.TCPSite] = web.TCPSite,
    ) -> None:
        self._app = app
        self._port = port
        self._runner_factory = runner_factory
        self._site_factory = site_factory
        self._state = ConsoleServiceState.NEW
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def state(self) -> ConsoleServiceState:
        return self._state

    @property
    def site(self) -> web.TCPSite | None:
        return self._site

    async def start(self) -> None:
        if self._state is not ConsoleServiceState.NEW:
            raise RuntimeError(f"cannot start console in {self._state.value}")
        # The app is frozen by runner.setup(), so bind the Host allowlist first.
        self._app[_ALLOWED_HOSTS_KEY] = frozenset(
            {f"{CONSOLE_LISTEN_HOST}:{self._port}", f"localhost:{self._port}"}
        )
        self._app[_ALLOWED_ORIGINS_KEY] = frozenset(
            {f"http://{CONSOLE_LISTEN_HOST}:{self._port}", f"http://localhost:{self._port}"}
        )
        runner = self._runner_factory(self._app, handle_signals=False, access_log=None)
        await runner.setup()
        try:
            site = self._site_factory(
                runner,
                host=CONSOLE_LISTEN_HOST,
                port=self._port,
                reuse_address=False,
                reuse_port=False,
            )
            await site.start()
        except BaseException:
            await asyncio.shield(runner.cleanup())
            raise
        self._runner = runner
        self._site = site
        self._state = ConsoleServiceState.RUNNING

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self._state = ConsoleServiceState.STOPPED
        if runner is not None:
            await asyncio.shield(runner.cleanup())

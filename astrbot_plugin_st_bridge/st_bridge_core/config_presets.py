"""Whole-config presets (总预设): save, list, apply, import and export every setting at once.

A preset is one small JSON file in ``<plugin_data>/presets/``; the file IS the export format. It holds the
whole config.json minus what is local to this machine: the ports (``network``), the owner, the chat platform choice and its group whitelist, the music API key, and the
upgrade notice. Runtime data (affinity values, song library, covers, quota ...) and files never live in
config.json, so they are never part of a preset. File names are random ids, never the preset name.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .emotion_config import (
    EmotionConfigError,
    fill_config_defaults,
    migrate_g1,
    validate_config,
    _replace_with_retry,
)

FORMAT = "stbridge-preset"
FORMAT_VERSION = 1
MAX_NAME_CHARS = 40
MAX_IMPORT_BYTES = 1024 * 1024
MAX_AUTO_PRESETS = 5
MAX_PRESETS = 100
# Sections that stay local to this machine: a preset neither carries them out nor changes them.
EXCLUDED_SECTIONS = ("network", "owner", "chat_platform", "install", "texts_notice", "auth_token")
EXCLUDED_COVER_FIELDS = ("music_api_key", "rvc_root")
# The folders of the local TTS / RVC installs are machine-local too: a preset neither carries nor changes them.
EXCLUDED_TTS_FIELDS = (
    ("irodori", "launch_dir"), ("irodori", "server_dir"), ("irodori", "lora_adapter"), ("gpt_sovits", "launch_dir"),
)
KIND_USER = "user"
KIND_AUTO = "auto"

_ID_RE = re.compile(r"^p[0-9a-f]{16}$")
_BAD_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]")
STATE_FILE = "_state.json"


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def clean_name(value: Any) -> str:
    """A user-typed preset name: 1-40 characters, no slashes, no control characters, no '..'."""
    if not isinstance(value, str):
        raise EmotionConfigError(400, "invalid preset name")
    name = value.strip()
    if not name or len(name) > MAX_NAME_CHARS or _BAD_NAME_RE.search(name) or ".." in name:
        raise EmotionConfigError(400, "invalid preset name")
    return name


def _lenient_name(value: Any) -> str:
    """The name of an imported file: made usable instead of refused."""
    text = value if isinstance(value, str) else ""
    text = _BAD_NAME_RE.sub("_", text).replace("..", "_").strip()
    return text[:MAX_NAME_CHARS].strip() or "导入的预设"


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """The part of a config a preset holds: everything but the machine-local items."""
    result = copy.deepcopy(config)
    for section in EXCLUDED_SECTIONS:
        result.pop(section, None)
    cover = result.get("cover")
    if isinstance(cover, dict):
        for field in EXCLUDED_COVER_FIELDS:
            cover.pop(field, None)
    tts = result.get("tts")
    if isinstance(tts, dict):
        for group, field in EXCLUDED_TTS_FIELDS:
            if isinstance(tts.get(group), dict):
                tts[group].pop(field, None)
    return result


def _drop_local_paths(candidate: dict[str, Any]) -> None:
    cover = candidate.get("cover")
    if isinstance(cover, dict):
        cover.pop("rvc_root", None)
    tts = candidate.get("tts")
    if isinstance(tts, dict):
        for group, field in EXCLUDED_TTS_FIELDS:
            if isinstance(tts.get(group), dict):
                tts[group].pop(field, None)


def _restore_local_tts_paths(candidate: dict[str, Any], local_config: dict[str, Any]) -> None:
    local_tts = local_config.get("tts") if isinstance(local_config.get("tts"), dict) else {}
    tts = candidate.get("tts")
    if not isinstance(tts, dict):
        return
    for group, field in EXCLUDED_TTS_FIELDS:
        local_group = local_tts.get(group) if isinstance(local_tts.get(group), dict) else {}
        if isinstance(tts.get(group), dict):
            tts[group][field] = local_group.get(field, "") if isinstance(local_group.get(field, ""), str) else ""


def normalize_config(preset_config: Any, local_config: dict[str, Any]) -> dict[str, Any]:
    """A complete, validated config from a preset's settings and this machine's own network / key.

    Runs the same upgrade steps as loading config.json (G1 migration, default sections), so a preset saved
    by an older version applies too. Raises EmotionConfigError(400) when the result is not acceptable.
    """
    if not isinstance(preset_config, dict):
        raise EmotionConfigError(400, "preset config must be an object")
    candidate = copy.deepcopy(preset_config)
    for section in EXCLUDED_SECTIONS:
        candidate.pop(section, None)
    local_cover = local_config.get("cover") if isinstance(local_config.get("cover"), dict) else {}
    try:
        migrate_g1(candidate, [])
        candidate["network"] = copy.deepcopy(local_config.get("network", {}))
        if not candidate["network"]:
            del candidate["network"]
        if isinstance(local_config.get("owner"), dict):
            candidate["owner"] = copy.deepcopy(local_config["owner"])
        if isinstance(local_config.get("chat_platform"), dict):
            candidate["chat_platform"] = copy.deepcopy(local_config["chat_platform"])
        if isinstance(local_config.get("install"), dict):
            candidate["install"] = copy.deepcopy(local_config["install"])
        # A preset from before R4b may carry these paths: they are dropped, this machine's own are kept.
        _drop_local_paths(candidate)
        fill_config_defaults(candidate)
        if isinstance(candidate.get("cover"), dict):
            for field in EXCLUDED_COVER_FIELDS:
                candidate["cover"].pop(field, None)
            candidate["cover"]["music_api_key"] = local_cover.get("music_api_key", "")
            candidate["cover"]["rvc_root"] = local_cover.get("rvc_root", "")
        _restore_local_tts_paths(candidate, local_config)
        validate_config(candidate)
    except EmotionConfigError:
        raise
    except (RuntimeError, TypeError, ValueError, AttributeError, KeyError) as exc:
        raise EmotionConfigError(400, f"preset config is not valid: {exc}") from exc
    return candidate


def apply_to_config(config: dict[str, Any], new_config: dict[str, Any]) -> None:
    """Replace everything in ``config`` (in place) by a normalized preset config."""
    config.clear()
    config.update(copy.deepcopy(new_config))


def is_modified(current_config: dict[str, Any], preset_config: Any) -> bool:
    """Whether the current settings differ from a preset (compared after the same normalization)."""
    try:
        wanted = snapshot(normalize_config(preset_config, current_config))
    except EmotionConfigError:
        return True
    return snapshot(current_config) != wanted


def _path_fields(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(field label, path text, kind 'file'|'dir'|'any') for every path the settings mention."""
    found: list[tuple[str, str, str]] = []

    def add(label: str, value: Any, kind: str) -> None:
        if isinstance(value, str) and value.strip():
            found.append((label, value.strip(), kind))

    add("gpt_sovits_root", config.get("gpt_sovits_root"), "dir")
    for name, model in (config.get("models") or {}).items():
        if not isinstance(model, dict):
            continue
        add(f"models.{name}.gpt_model_path", model.get("gpt_model_path"), "file")
        add(f"models.{name}.sovits_model_path", model.get("sovits_model_path"), "file")
        add(f"models.{name}.reference_audio_root", model.get("reference_audio_root"), "dir")
    for name, character in (config.get("characters") or {}).items():
        if not isinstance(character, dict):
            continue
        for emotion, profile in (character.get("emotions") or {}).items():
            if isinstance(profile, dict):
                add(f"characters.{name}.emotions.{emotion}.ref_audio_path", profile.get("ref_audio_path"), "file")
    tts = config.get("tts") if isinstance(config.get("tts"), dict) else {}
    irodori = tts.get("irodori") if isinstance(tts.get("irodori"), dict) else {}
    add("tts.irodori.launch_dir", irodori.get("launch_dir"), "dir")
    add("tts.irodori.server_dir", irodori.get("server_dir"), "dir")
    add("tts.irodori.lora_adapter", irodori.get("lora_adapter"), "any")
    sovits = tts.get("gpt_sovits") if isinstance(tts.get("gpt_sovits"), dict) else {}
    add("tts.gpt_sovits.launch_dir", sovits.get("launch_dir"), "dir")
    add("tts.gpt_sovits.gpt_model_path", sovits.get("gpt_model_path"), "file")
    add("tts.gpt_sovits.sovits_model_path", sovits.get("sovits_model_path"), "file")
    add("tts.gpt_sovits.ref_audio_path", sovits.get("ref_audio_path"), "file")
    cover = config.get("cover") if isinstance(config.get("cover"), dict) else {}
    add("cover.rvc_root", cover.get("rvc_root"), "dir")
    add("cover.index_path", cover.get("index_path"), "file")
    root = cover.get("rvc_root")
    model = cover.get("model")
    if isinstance(root, str) and root.strip() and isinstance(model, str) and model.strip():
        add("cover.model", str(Path(root.strip()) / "assets" / "weights" / model.strip()), "file")
    return found


def missing_paths(config: dict[str, Any]) -> list[dict[str, str]]:
    """The paths the settings mention that this machine cannot find (blocking filesystem calls)."""
    result = []
    for label, text, kind in _path_fields(config):
        try:
            path = Path(text).expanduser()
            ok = path.is_file() if kind == "file" else path.is_dir() if kind == "dir" else path.exists()
        except OSError:
            ok = False
        if not ok:
            result.append({"field": label, "path": text})
    return result


def parse_document(document: Any) -> dict[str, Any]:
    """Check a preset file's envelope; the config inside is checked by normalize_config."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise EmotionConfigError(400, "not a preset file")
    version = document.get("format_version")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        raise EmotionConfigError(400, "unsupported preset format version")
    if not isinstance(document.get("config"), dict):
        raise EmotionConfigError(400, "preset config must be an object")
    return document


class PresetStore:
    """The presets directory: one json per preset plus a small state file for the current one."""

    def __init__(self, data_dir: Path, *, clock: Callable[[], datetime] = datetime.now):
        self.directory = Path(data_dir) / "presets"
        self._clock = clock

    # -- files ---------------------------------------------------------------------------------------

    def _path(self, preset_id: str) -> Path:
        if not valid_id(preset_id):
            raise EmotionConfigError(404, "preset not found")
        return self.directory / f"{preset_id}.json"

    def _write_json(self, path: Path, document: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="preset.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self, preset_id: str) -> dict[str, Any]:
        path = self._path(preset_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return parse_document(json.load(handle))
        except FileNotFoundError as exc:
            raise EmotionConfigError(404, "preset not found") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmotionConfigError(409, "preset file is damaged") from exc

    def _ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.glob("p*.json") if valid_id(path.stem))

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="microseconds")

    def _new_id(self) -> str:
        while True:
            candidate = "p" + secrets.token_hex(8)
            if not self._path(candidate).exists():
                return candidate

    # -- state ---------------------------------------------------------------------------------------

    def current_id(self) -> str | None:
        try:
            with (self.directory / STATE_FILE).open("r", encoding="utf-8") as handle:
                value = json.load(handle).get("current")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return None
        return value if valid_id(value) and self._path(value).exists() else None

    def _set_current(self, preset_id: str | None) -> None:
        self._write_json(self.directory / STATE_FILE, {"current": preset_id})

    # -- listing -------------------------------------------------------------------------------------

    def _summary(self, preset_id: str, current: str | None) -> dict[str, Any]:
        try:
            document = self._read(preset_id)
        except EmotionConfigError:
            return {"id": preset_id, "name": "(损坏的预设文件)", "kind": KIND_USER, "broken": True,
                    "created_at": "", "updated_at": "", "current": preset_id == current}
        kind = KIND_AUTO if document.get("kind") == KIND_AUTO else KIND_USER
        return {
            "id": preset_id,
            "name": _lenient_name(document.get("name")),
            "kind": kind,
            "broken": False,
            "created_at": str(document.get("created_at") or ""),
            "updated_at": str(document.get("updated_at") or document.get("created_at") or ""),
            "current": preset_id == current,
        }

    def list(self, current_config: dict[str, Any]) -> dict[str, Any]:
        current = self.current_id()
        items = [self._summary(preset_id, current) for preset_id in self._ids()]
        users = sorted((item for item in items if item["kind"] == KIND_USER),
                       key=lambda item: (item["created_at"], item["id"]))
        autos = sorted((item for item in items if item["kind"] == KIND_AUTO),
                       key=lambda item: (item["created_at"], item["id"]), reverse=True)
        modified: bool | None = None
        current_name = None
        if current is not None:
            current_name = next((item["name"] for item in items if item["id"] == current), None)
            try:
                modified = is_modified(current_config, self._read(current)["config"])
            except EmotionConfigError:
                modified = True
        return {"current": current, "current_name": current_name, "modified": modified,
                "presets": users, "autos": autos}

    def load(self, preset_id: str) -> dict[str, Any]:
        return self._read(preset_id)

    def export_bytes(self, preset_id: str) -> tuple[bytes, str]:
        """The file as stored (checked to be a preset) and the name to download it as."""
        document = self._read(preset_id)
        return self._path(preset_id).read_bytes(), _lenient_name(document.get("name"))

    # -- writing -------------------------------------------------------------------------------------

    def _name_taken(self, name: str, *, except_id: str | None = None) -> bool:
        wanted = name.casefold()
        for preset_id in self._ids():
            if preset_id == except_id:
                continue
            item = self._summary(preset_id, None)
            if item["kind"] == KIND_USER and not item["broken"] and item["name"].casefold() == wanted:
                return True
        return False

    def _document(self, name: str, kind: str, config: dict[str, Any], created: str | None = None) -> dict[str, Any]:
        now = self._stamp()
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "name": name,
            "kind": kind,
            "created_at": created or now,
            "updated_at": now,
            "config": snapshot(config),
        }

    def _check_capacity(self) -> None:
        if len(self._ids()) >= MAX_PRESETS + MAX_AUTO_PRESETS + 1:
            raise EmotionConfigError(400, "too many presets")

    def save_new(self, name: Any, config: dict[str, Any]) -> dict[str, Any]:
        """Store the current settings as a new named preset and make it the current one."""
        name = clean_name(name)
        if self._name_taken(name):
            raise EmotionConfigError(400, "preset name already exists")
        self._check_capacity()
        preset_id = self._new_id()
        self._write_json(self._path(preset_id), self._document(name, KIND_USER, config))
        self._set_current(preset_id)
        return self._summary(preset_id, preset_id)

    def update(self, preset_id: str, *, name: Any = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rename a preset and / or overwrite it with the current settings (which makes it the current one)."""
        document = self._read(preset_id)
        kind = KIND_AUTO if document.get("kind") == KIND_AUTO else KIND_USER
        if name is not None:
            if kind == KIND_AUTO:
                raise EmotionConfigError(400, "an automatic archive cannot be renamed")
            name = clean_name(name)
            if self._name_taken(name, except_id=preset_id):
                raise EmotionConfigError(400, "preset name already exists")
        elif config is None:
            raise EmotionConfigError(400, "nothing to change")
        new_name = name if name is not None else document.get("name")
        new_config = config if config is not None else document["config"]
        updated = self._document(new_name, kind, new_config, created=document.get("created_at"))
        if config is None:
            updated["config"] = document["config"]
        self._write_json(self._path(preset_id), updated)
        if config is not None:
            self._set_current(preset_id)
        return self._summary(preset_id, self.current_id())

    def delete(self, preset_id: str) -> None:
        """Remove a preset; deleting the current one leaves the settings as they are, with no current preset."""
        path = self._path(preset_id)
        if not path.exists():
            raise EmotionConfigError(404, "preset not found")
        was_current = self.current_id() == preset_id
        path.unlink()
        if was_current:
            self._set_current(None)

    def archive(self, config: dict[str, Any], *, protect: str | None = None) -> dict[str, Any]:
        """Save the current settings as an automatic '切换前' archive; only the newest few are kept."""
        stamp = self._clock().strftime("%Y-%m-%d %H:%M:%S")
        preset_id = self._new_id()
        self._write_json(self._path(preset_id), self._document(f"切换前-{stamp}", KIND_AUTO, config))
        autos = [item for item in (self._summary(i, None) for i in self._ids()) if item["kind"] == KIND_AUTO]
        autos.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        for item in autos[MAX_AUTO_PRESETS:]:
            if item["id"] not in (preset_id, protect):
                try:
                    self._path(item["id"]).unlink()
                except OSError:
                    pass
        return self._summary(preset_id, self.current_id())

    def mark_applied(self, preset_id: str) -> None:
        self._set_current(preset_id if self._path(preset_id).exists() else None)

    def import_document(self, raw: bytes, local_config: dict[str, Any]) -> dict[str, Any]:
        """Add a preset file to the list (never applies it). The settings must be acceptable on this machine."""
        if len(raw) > MAX_IMPORT_BYTES:
            raise EmotionConfigError(413, "preset file too large")
        try:
            document = parse_document(json.loads(raw.decode("utf-8-sig")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmotionConfigError(400, "invalid JSON") from exc
        normalized = normalize_config(document["config"], local_config)
        self._check_capacity()
        name = base = _lenient_name(document.get("name"))
        counter = 2
        while self._name_taken(name):
            suffix = f" ({counter})"
            name = base[: MAX_NAME_CHARS - len(suffix)] + suffix
            counter += 1
        preset_id = self._new_id()
        self._write_json(self._path(preset_id), self._document(name, KIND_USER, normalized))
        return self._summary(preset_id, self.current_id())

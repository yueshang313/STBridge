"""Song cover settings (console config `cover` section) and RVC folder scanning.

No web-framework dependency; errors are EmotionConfigError so the console maps them to responses.
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .network_config import reserved_ports
from .emotion_config import EmotionConfigError
from .music_source import DEFAULT_SOURCES, SOURCES

# Ports other parts of the setup use with the default network settings: bridge 11452, ST 11451, console 11453,
# connector 11454, NapCat 11455, Irodori 8088, GPT-SoVITS 9880. The live set follows the network settings (network_config.reserved_ports).
RESERVED_PORTS = frozenset({8088, 9880, 11451, 11452, 11453, 11454, 11455})
F0_METHODS = ("rmvpe", "fcpe", "pm")
# Algorithms RVC 2026 dropped: a saved config that still names one is read as rmvpe (rejecting it would
# make cover_settings() fall back to the defaults and lose the music API key).
LEGACY_F0_METHODS = ("harvest", "crepe")
# MSST separation models of RVC 2026: id -> (label, checkpoint file under assets/pymss_weights).
SEPARATION_MODELS = {
    "vocals-bs-roformer-368": ("去伴奏", "model_bs_roformer_ep_368_sdr_12.9628.ckpt"),
    "vocals-bs-roformer-317": ("去伴奏（激进）", "model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
}
DEREVERB_MODELS = {
    "dereverb-less-aggressive-18.8050": ("去混响", "dereverb_mel_band_roformer_less_aggressive_anvuew_sdr_18.8050.ckpt"),
    "dereverb-anvuew-19.1729": ("去混响（激进）", "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt"),
}
MAX_PATH_CHARS = 260
_MODEL_SUFFIX_RE = re.compile(r"_e\d+_s\d+$")


MAX_PITCH_PRESETS = 10
PRESET_NAME_MAX = 20
KEYWORDS_MAX = 10
KEYWORD_MAX = 10


def default_pitch_presets() -> list[dict[str, Any]]:
    """Keyword-triggered pitch presets: vocal = extra vocal shift, inst = overall shift (accompaniment too)."""
    return [
        {"name": "男声歌", "vocal": 12, "inst": 0, "keywords": ["男声", "男生", "男歌手"]},
        {"name": "升调", "vocal": 0, "inst": 2, "keywords": ["升调", "高一点", "变声"]},
        {"name": "降调", "vocal": 0, "inst": -2, "keywords": ["降调", "低一点"]},
    ]


def keyword_key(keyword: str) -> str:
    """How keywords are compared and matched: NFKC, case-insensitive."""
    return unicodedata.normalize("NFKC", keyword).casefold()


def _pitch_presets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_PITCH_PRESETS:
        raise EmotionConfigError(400, "invalid pitch presets")
    presets: list[dict[str, Any]] = []
    names: set[str] = set()
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise EmotionConfigError(400, "invalid pitch preset")
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= PRESET_NAME_MAX:
            raise EmotionConfigError(400, "invalid pitch preset name")
        name = name.strip()
        if name in names:
            raise EmotionConfigError(400, "duplicate pitch preset name")
        names.add(name)
        vocal = _number(item.get("vocal"), "preset vocal pitch", -12, 12, integer=True)
        inst = _number(item.get("inst"), "preset overall pitch", -12, 12, integer=True)
        raw = item.get("keywords")
        if not isinstance(raw, list) or not 1 <= len(raw) <= KEYWORDS_MAX:
            raise EmotionConfigError(400, "invalid pitch preset keywords")
        keywords: list[str] = []
        for word in raw:
            if not isinstance(word, str) or not 1 <= len(word.strip()) <= KEYWORD_MAX:
                raise EmotionConfigError(400, "invalid pitch preset keyword")
            word = word.strip()
            key = keyword_key(word)
            if key in seen:
                raise EmotionConfigError(400, "duplicate pitch preset keyword")
            seen.add(key)
            keywords.append(word)
        presets.append({"name": name, "vocal": vocal, "inst": inst, "keywords": keywords})
    return presets


def default_cover_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "rvc_root": "",
        "port": 8099,
        "model": "",
        "index_path": "",
        "f0_up_key": 0,
        "inst_pitch": 0,
        "pitch_presets": default_pitch_presets(),
        "f0_method": "rmvpe",
        "index_rate": 0.75,
        "rms_mix_rate": 0.25,
        "protect": 0.33,
        "sep_model": "vocals-bs-roformer-368",
        "dereverb_model": "dereverb-less-aggressive-18.8050",
        "vocal_reverb": 0,
        "keep_stems": False,
        "clip_seconds": 60,
        "default_start_percent": 30,
        "vocal_gain_db": 0,
        "inst_gain_db": -5,
        "output_sample_rate": DEFAULT_OUTPUT_SAMPLE_RATE,
        "idle_stop_minutes": 10,
        "job_timeout_seconds": 600,
        "music_api_key": "",
        "sources": list(DEFAULT_SOURCES),
        "min_full_seconds": 90,
        "download_timeout_seconds": 120,
    }


OUTPUT_SAMPLE_RATES = (24000, 44100)  # 24 kHz mono (default, what QQ ends up with); 44.1 kHz stereo is better quality but is brought down to 24 kHz when sent
DEFAULT_OUTPUT_SAMPLE_RATE = 24000


def _output_sample_rate(value: Any) -> int:
    if isinstance(value, bool) or value not in OUTPUT_SAMPLE_RATES:
        raise EmotionConfigError(400, "invalid output sample rate")
    return int(value)


def _number(value: Any, label: str, low: float, high: float, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EmotionConfigError(400, f"invalid {label}")
    if integer and value != int(value):
        raise EmotionConfigError(400, f"invalid {label}")
    if not low <= value <= high:
        raise EmotionConfigError(400, f"invalid {label}")
    return int(value) if integer else float(value)


def _path(value: Any, label: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip().strip('"')
    if (required and not text) or len(text) > MAX_PATH_CHARS or "\x00" in text:
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def _file_name(value: Any, label: str, suffix: str | None, *, required: bool) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, f"invalid {label}")
    text = value.strip()
    if not text:
        if required:
            raise EmotionConfigError(400, f"invalid {label}")
        return ""
    if (
        len(text) > 120
        or "/" in text
        or "\\" in text
        or "\x00" in text
        or text in (".", "..")
        or (suffix is not None and not text.lower().endswith(suffix))
    ):
        raise EmotionConfigError(400, f"invalid {label}")
    return text


def _music_key(value: Any) -> str:
    if not isinstance(value, str):
        raise EmotionConfigError(400, "invalid music api key")
    text = value.strip()
    if len(text) > 200 or any(c.isspace() or ord(c) < 32 for c in text):
        raise EmotionConfigError(400, "invalid music api key")
    return text


def _sources(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > len(SOURCES):
        raise EmotionConfigError(400, "invalid music sources")
    if any(not isinstance(item, str) or item not in SOURCES for item in value) or len(set(value)) != len(value):
        raise EmotionConfigError(400, "invalid music sources")
    return list(value)


def public_cover_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings for the console: the music API key is replaced by key_set and its last 4 characters."""
    key = settings.get("music_api_key") or ""
    public = {name: value for name, value in settings.items() if name != "music_api_key"}
    public["key_set"] = bool(key)
    public["key_tail"] = key[-4:] if len(key) >= 8 else ""
    return public


def validate_cover_settings(settings: Any, reserved: frozenset[int] = RESERVED_PORTS) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise EmotionConfigError(400, "cover settings must be an object")
    merged = {**default_cover_settings(), **settings}
    if not isinstance(merged.get("enabled"), bool):
        raise EmotionConfigError(400, "invalid cover enabled")
    port = _number(merged.get("port"), "cover port", 1024, 65535, integer=True)
    if port in reserved:
        raise EmotionConfigError(400, "cover port is used by another service")
    f0_method = merged.get("f0_method")
    if f0_method in LEGACY_F0_METHODS:
        f0_method = "rmvpe"
    if f0_method not in F0_METHODS:
        raise EmotionConfigError(400, "invalid cover f0 method")
    sep_model = merged.get("sep_model")
    if not isinstance(sep_model, str) or sep_model not in SEPARATION_MODELS:
        raise EmotionConfigError(400, "invalid cover separation model")
    dereverb_model = merged.get("dereverb_model")
    if not isinstance(dereverb_model, str) or (dereverb_model and dereverb_model not in DEREVERB_MODELS):
        raise EmotionConfigError(400, "invalid cover dereverb model")
    if not isinstance(merged.get("keep_stems"), bool):
        raise EmotionConfigError(400, "invalid cover keep stems")
    return {
        "enabled": merged["enabled"],
        "rvc_root": _path(merged.get("rvc_root"), "rvc root", required=False),  # "" = not set up yet
        "port": port,
        "model": _file_name(merged.get("model"), "cover model", ".pth", required=False),
        "index_path": _path(merged.get("index_path"), "index path", required=False),
        "f0_up_key": _number(merged.get("f0_up_key"), "pitch shift", -12, 12, integer=True),
        "inst_pitch": _number(merged.get("inst_pitch"), "instrument pitch", -12, 12, integer=True),
        "pitch_presets": _pitch_presets(merged.get("pitch_presets")),
        "f0_method": f0_method,
        "index_rate": _number(merged.get("index_rate"), "index rate", 0.0, 1.0),
        "rms_mix_rate": _number(merged.get("rms_mix_rate"), "rms mix rate", 0.0, 1.0),
        "protect": _number(merged.get("protect"), "protect", 0.0, 0.5),
        "sep_model": sep_model,
        "dereverb_model": dereverb_model,
        "vocal_reverb": _number(merged.get("vocal_reverb"), "vocal reverb", 0, 100, integer=True),
        "keep_stems": merged["keep_stems"],
        "clip_seconds": _number(merged.get("clip_seconds"), "clip seconds", 0, 600, integer=True),
        "default_start_percent": _number(
            merged.get("default_start_percent"), "default start percent", 0, 90, integer=True
        ),
        "vocal_gain_db": _number(merged.get("vocal_gain_db"), "vocal gain", -30, 30),
        "inst_gain_db": _number(merged.get("inst_gain_db"), "instrument gain", -30, 30),
        "output_sample_rate": _output_sample_rate(merged.get("output_sample_rate")),
        "idle_stop_minutes": _number(merged.get("idle_stop_minutes"), "idle stop minutes", 0, 1440, integer=True),
        "job_timeout_seconds": _number(
            merged.get("job_timeout_seconds"), "job timeout", 30, 3600, integer=True
        ),
        "music_api_key": _music_key(merged.get("music_api_key")),
        "sources": _sources(merged.get("sources")),
        "min_full_seconds": _number(merged.get("min_full_seconds"), "min full seconds", 0, 600, integer=True),
        "download_timeout_seconds": _number(
            merged.get("download_timeout_seconds"), "download timeout", 10, 600, integer=True
        ),
    }


def cover_settings(config: Any) -> dict[str, Any]:
    """Validated cover settings from a config, falling back to the defaults."""
    try:
        raw = config.get("cover", {})
        if isinstance(raw, dict) and (
            isinstance(raw.get("output_sample_rate"), bool) or raw.get("output_sample_rate") not in OUTPUT_SAMPLE_RATES
        ):
            raw = {**raw, "output_sample_rate": DEFAULT_OUTPUT_SAMPLE_RATE}  # missing / invalid: default, keep the rest
        return validate_cover_settings(raw, reserved=reserved_ports(config))
    except (AttributeError, EmotionConfigError):
        return default_cover_settings()


# ---- RVC folder scanning (works while the worker is not running) ----------------------


def _list_files(directory: Path, suffix: str) -> list[str]:
    try:
        return sorted(
            entry.name for entry in directory.iterdir() if entry.is_file() and entry.name.lower().endswith(suffix)
        )
    except OSError:
        return []


def list_models(rvc_root: str) -> list[str]:
    """Voice model weights (`assets/weights/*.pth`); none while the RVC folder is not set."""
    if not rvc_root:
        return []
    return _list_files(Path(rvc_root) / "assets" / "weights", ".pth")


def list_separation_models(rvc_root: str) -> dict[str, list[dict[str, Any]]]:
    """The separation / dereverb models the worker knows, each marked with whether its checkpoint is installed."""
    present = set(_list_files(Path(rvc_root) / "assets" / "pymss_weights", ".ckpt")) if rvc_root else set()

    def entries(table: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
        return [
            {"id": model_id, "label": label, "available": ckpt in present}
            for model_id, (label, ckpt) in table.items()
        ]

    return {"separation": entries(SEPARATION_MODELS), "dereverb": entries(DEREVERB_MODELS)}


def model_stem(model: str) -> str:
    """`myvoice_e150_s15450.pth` -> `myvoice` (the folder name under `logs/`)."""
    stem = model[: -len(".pth")] if model.lower().endswith(".pth") else model
    return _MODEL_SUFFIX_RE.sub("", stem)


def auto_index(rvc_root: str, model: str) -> str:
    """The index RVC 2026 would pick for a model (its `get_index_path_from_model`), or "" when there is none.

    Searches `assets/indices`, then `logs`, recursively; `trained` indexes are skipped. A file matches when its
    name starts with `<exp>_added_`, contains `_<exp>_v1` / `_<exp>_v2`, or contains the whole model stem
    (case-insensitive). Order: standard match, `assets/indices` first, newest, then path.
    """
    if not model or not rvc_root:
        return ""
    experiment = model_stem(model).lower()
    stem = (model[: -len(".pth")] if model.lower().endswith(".pth") else model).lower()
    if not experiment:
        return ""
    roots = [Path(rvc_root) / "assets" / "indices", Path(rvc_root) / "logs"]
    candidates = []
    for position, root in enumerate(roots):
        for directory, _subdirs, names in os.walk(root):
            for name in names:
                lower = name.lower()
                if not lower.endswith(".index") or "trained" in lower:
                    continue
                index_stem = lower[: -len(".index")]
                standard = (
                    index_stem.startswith(experiment + "_added_")
                    or f"_{experiment}_v1" in index_stem
                    or f"_{experiment}_v2" in index_stem
                )
                if not (standard or stem in index_stem):
                    continue
                path = os.path.join(directory, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                candidates.append(((0 if standard else 1, position, -mtime, path.lower()), path))
    return min(candidates, default=(None, ""), key=lambda item: item[0])[1]


def resolve_index(settings: dict[str, Any]) -> str:
    """The index the job uses: the manual path, else the auto-matched one."""
    return settings["index_path"] or auto_index(settings["rvc_root"], settings["model"])

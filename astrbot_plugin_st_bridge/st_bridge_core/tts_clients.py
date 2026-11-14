"""TTS request builders and HTTP clients: GPT-SoVITS api.py and Irodori-TTS-Server."""

from collections.abc import Mapping
import ipaddress


_API_PY_LANGUAGE_ALIASES = {
    "英语": "英文",
    "日语": "日文",
    "韩语": "韩文",
}
_API_PY_LANGUAGE_KEYS = frozenset(
    {
        "中文",
        "粤语",
        "英文",
        "日文",
        "韩文",
        "中英混合",
        "粤英混合",
        "日英混合",
        "韩英混合",
        "多语种混合",
        "多语种混合(粤语)",
        "all_zh",
        "all_yue",
        "en",
        "all_ja",
        "all_ko",
        "zh",
        "yue",
        "ja",
        "ko",
        "auto",
        "auto_yue",
    }
)


class UnsupportedPromptLanguageError(ValueError):
    pass


class UnsupportedTextLanguageError(ValueError):
    pass


class ModelSelectionError(ValueError):
    pass


def build_api_base_url(host: str, port: int) -> str:
    normalized_host = str(host).strip()
    if not normalized_host or any(char in normalized_host for char in "/?#@"):
        raise ValueError("Invalid GPT-SoVITS API host")

    if normalized_host.startswith("[") and normalized_host.endswith("]"):
        normalized_host = normalized_host[1:-1]
    if ":" in normalized_host:
        try:
            ipaddress.ip_address(normalized_host)
        except ValueError as error:
            raise ValueError("Invalid GPT-SoVITS API host") from error
        normalized_host = f"[{normalized_host}]"

    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid GPT-SoVITS API port") from error
    if not 1 <= normalized_port <= 65535:
        raise ValueError("Invalid GPT-SoVITS API port")

    return f"http://{normalized_host}:{normalized_port}"


def usable_gpt_profile(profile) -> dict | None:
    """Return a Bridge emotion profile GPT-SoVITS can use, or None (no audio or bad language)."""
    if not isinstance(profile, dict):
        return None
    ref_audio_path = profile.get("ref_audio_path")
    if not isinstance(ref_audio_path, str) or not ref_audio_path.strip():
        return None
    try:
        map_api_py_language(profile.get("prompt_text_lang"), "prompt_text_lang")
        map_api_py_language(profile.get("text_lang"), "text_lang")
    except ValueError:
        return None
    return dict(profile)


def build_profile_tts_settings(profile: Mapping[str, object]) -> dict[str, object]:
    """Translate a Bridge profile into this plugin's setting names.

    The Bridge uses speed_factor; this plugin's settings use speed_facter.
    """
    prompt_text = profile.get("prompt_text", "")
    return {
        "ref_audio_path": str(profile["ref_audio_path"]).strip(),
        "prompt_text": prompt_text if isinstance(prompt_text, str) else "",
        "prompt_text_lang": profile["prompt_text_lang"],
        "text_lang": profile["text_lang"],
        "top_k": profile.get("top_k", 10),
        "top_p": profile.get("top_p", 1.0),
        "temperature": profile.get("temperature", 1.0),
        "speed_facter": profile.get("speed_factor", 1.0),
        "split_sentence": profile.get("split_sentence", True),
    }


def profile_model_payload(profile: Mapping[str, object] | None) -> dict[str, str] | None:
    """Return the profile's model pair only when both paths are present."""
    if not profile:
        return None
    gpt_path = profile.get("gpt_model_path")
    sovits_path = profile.get("sovits_model_path")
    if not (
        isinstance(gpt_path, str) and gpt_path.strip()
        and isinstance(sovits_path, str) and sovits_path.strip()
    ):
        return None
    return build_api_py_model_payload(gpt_path, sovits_path)


def map_api_py_language(language: object, field: str) -> str:
    error_type = {
        "prompt_text_lang": UnsupportedPromptLanguageError,
        "text_lang": UnsupportedTextLanguageError,
    }.get(field, ValueError)
    if not isinstance(language, str) or not language.strip():
        raise error_type(f"Unsupported GPT-SoVITS language setting: {field}")

    normalized = language.strip().lower()
    api_language = _API_PY_LANGUAGE_ALIASES.get(normalized, normalized)
    if api_language not in _API_PY_LANGUAGE_KEYS:
        raise error_type(f"Unsupported GPT-SoVITS language setting: {field}")
    return api_language


def build_api_py_payload(
    text: str,
    config: Mapping[str, object],
) -> dict[str, object]:
    split_punctuation = "，。？！；：、,.?!;…"
    return {
        "refer_wav_path": config.get("ref_audio_path", ""),
        "prompt_text": config.get("prompt_text", ""),
        "prompt_language": map_api_py_language(
            config.get("prompt_text_lang", "中文"), "prompt_text_lang"
        ),
        "text": text,
        "text_language": map_api_py_language(
            config.get("text_lang", "中英混合"), "text_lang"
        ),
        "cut_punc": split_punctuation if config.get("split_sentence", True) else "",
        "top_k": int(config.get("top_k", 10)),
        "top_p": float(config.get("top_p", 1.0)),
        "temperature": float(config.get("temperature", 1.0)),
        "speed": float(config.get("speed_facter", 1.0)),
        "inp_refs": [],
        "sample_steps": 16,
        "if_sr": False,
    }


def build_api_py_model_payload(
    gpt_model_path: str | None,
    sovits_model_path: str | None,
) -> dict[str, str] | None:
    gpt_path = gpt_model_path.strip() if isinstance(gpt_model_path, str) else ""
    sovits_path = sovits_model_path.strip() if isinstance(sovits_model_path, str) else ""

    if not gpt_path and not sovits_path:
        return None
    if not gpt_path or not sovits_path:
        raise ModelSelectionError("Both GPT and SoVITS model paths are required")

    return {
        "gpt_model_path": gpt_path,
        "sovits_model_path": sovits_path,
    }


def extract_wav_response(response) -> bytes:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    media_type = content_type.lower().split(";", 1)[0].strip()
    if media_type not in {"audio/wav", "audio/x-wav", "audio/wave"}:
        raise ValueError("TTS API response is not WAV audio")
    audio_data = response.content
    if not isinstance(audio_data, bytes) or not audio_data:
        raise ValueError("TTS API returned empty audio")
    return audio_data


def local_http_client(**kwargs):
    """httpx client for services on this machine: never routed through the system proxy (trust_env=False)."""
    import httpx

    return httpx.AsyncClient(trust_env=False, **kwargs)


async def post_api_py_audio(
    client_factory,
    base_url: str,
    payload: Mapping[str, object],
    timeout_seconds: int,
) -> bytes:
    async with client_factory(timeout=timeout_seconds) as client:
        response = await client.post(f"{base_url.rstrip('/')}/", json=dict(payload))
        return extract_wav_response(response)


async def post_api_py_model(
    client_factory,
    base_url: str,
    payload: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    async with client_factory(timeout=timeout_seconds) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/set_model",
            json=dict(payload),
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping) or result.get("code") != 0:
            raise ModelSelectionError("GPT-SoVITS API rejected model selection")


def safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name in {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        return name
    if name in {
        "HTTPStatusError",
        "ValueError",
        "UnsupportedPromptLanguageError",
        "UnsupportedTextLanguageError",
        "ModelSelectionError",
        "OSError",
        "TypeError",
    }:
        return name
    return "OtherError"


IRODORI_MODEL = "irodori-tts"
SPEECH_PATH = "/v1/audio/speech"
_WAV_MEDIA_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})


class IrodoriRequestError(Exception):
    """The server answered with an error (or something that is not WAV audio)."""

    def __init__(self, status: int, error_type: str):
        super().__init__(f"Irodori request failed status={status} type={error_type}")
        self.status = status
        self.error_type = error_type


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_irodori_payload(
    text: str,
    profile: Mapping[str, object] | None,
    settings: Mapping[str, object],
) -> dict[str, object]:
    """Build the speech request: emoji + text, caption from the profile or the default."""
    profile = profile or {}
    emoji = _clean(profile.get("emoji"))
    caption = _clean(profile.get("caption")) or _clean(settings.get("default_caption"))
    options: dict[str, object] = {
        "no_ref": True,
        "chunking_enabled": False,
        "cfg_scale_text": float(settings.get("cfg_scale_text", 3.0)),
        "cfg_scale_caption": float(settings.get("cfg_scale_caption", 4.0)),
    }
    lora_adapter = _clean(settings.get("lora_adapter"))
    if lora_adapter:
        options["lora_adapter"] = lora_adapter
    if caption:
        options["caption"] = caption
    num_steps = int(settings.get("num_steps", 0) or 0)
    if num_steps > 0:
        options["num_steps"] = num_steps
    return {
        "model": IRODORI_MODEL,
        "input": f"{emoji}{text}" if emoji else text,
        "response_format": "wav",
        "irodori": options,
    }


def _error_type(response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, Mapping) else None
        kind = error.get("type") if isinstance(error, Mapping) else None
    except Exception:
        return "unknown"
    if isinstance(kind, str) and kind.isidentifier() and len(kind) <= 40:
        return kind
    return "unknown"


async def post_irodori_audio(
    client_factory,
    base_url: str,
    payload: Mapping[str, object],
    timeout_seconds: int,
) -> bytes:
    async with client_factory(timeout=timeout_seconds) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}{SPEECH_PATH}", json=dict(payload)
        )
        if response.status_code != 200:
            raise IrodoriRequestError(response.status_code, _error_type(response))
        media_type = response.headers.get("content-type", "").lower().split(";", 1)[0].strip()
        audio = response.content
        if media_type not in _WAV_MEDIA_TYPES or not isinstance(audio, bytes) or not audio:
            raise IrodoriRequestError(response.status_code, "invalid_audio")
        return audio

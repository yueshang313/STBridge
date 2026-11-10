"""Voice format for QQ: 24 kHz mono 16-bit WAV (pure, numpy only).

AstrBot encodes a WAV to silk at the WAV's own sample rate when that rate is one of 8/12/16/24/32/48 kHz, and QQ
desktop cannot play a 48 kHz silk, so a 48 kHz TTS result is resampled to 24 kHz first. (NapCat resamples every
voice to 24 kHz mono itself, so on that route the conversion changes nothing audible.)
`to_qq_voice_wav` converts the TTS audio in memory; on any problem it returns the original bytes unchanged
(a voice in the wrong format is better than no voice).
"""

from __future__ import annotations

import io
import math
import wave
from typing import Any

TARGET_RATE = 24000
_MAX_PHASES = 1024  # sample rates whose ratio to 24 kHz needs more polyphase branches are left alone
_STOP_ATTENUATION_DB = 60.0
_PASS_EDGE = 0.45  # of the target rate: 10.8 kHz
_STOP_EDGE = 0.55  # 13.2 kHz, so nothing above the target Nyquist folds back into the passband


def _kaiser_beta(attenuation: float) -> float:
    if attenuation > 50:
        return 0.1102 * (attenuation - 8.7)
    return 0.5842 * (attenuation - 21) ** 0.4 + 0.07886 * (attenuation - 21)


def _phase_filters(np: Any, src_rate: int, up: int, down: int) -> tuple[Any, int]:
    """Kaiser-windowed sinc low-pass cut at the target band, split into `up` polyphase branches.

    Returns (taps[up, 2K], K): branch r holds the weights of the 2K source samples around output position
    r*down/up, normalised to unity gain at DC.
    """
    ratio = TARGET_RATE / src_rate  # < 1
    cutoff = (_PASS_EDGE + _STOP_EDGE) / 2 * ratio  # cycles per source sample
    width = (_STOP_EDGE - _PASS_EDGE) * ratio
    length = math.ceil((_STOP_ATTENUATION_DB - 8) / (2.285 * 2 * math.pi * width))
    half = max(4, length // 2 + 1)
    beta = _kaiser_beta(_STOP_ATTENUATION_DB)
    taps = np.zeros((up, 2 * half), dtype=np.float64)
    offsets = np.arange(-half + 1, half + 1, dtype=np.float64)  # source sample j relative to floor(position)
    for r in range(up):
        frac = ((r * down) % up) / up
        x = offsets - frac
        window = np.i0(beta * np.sqrt(np.clip(1 - (x / half) ** 2, 0.0, None))) / np.i0(beta)
        branch = 2 * cutoff * np.sinc(2 * cutoff * x) * window
        taps[r] = branch / branch.sum()
    return taps.astype(np.float32), half


def _resample_down(np: Any, signal: Any, src_rate: int) -> Any:
    """Resample a float32 mono signal from `src_rate` (> 24 kHz) to 24 kHz, with anti-aliasing."""
    divisor = math.gcd(TARGET_RATE, src_rate)
    up, down = TARGET_RATE // divisor, src_rate // divisor
    if up > _MAX_PHASES:
        raise ValueError("unsupported sample rate ratio")
    n_out = int(round(len(signal) * TARGET_RATE / src_rate))
    if n_out <= 0:
        return signal[:0]
    taps, half = _phase_filters(np, src_rate, up, down)
    padded = np.zeros(len(signal) + 2 * half + 2 * down + 2, dtype=np.float32)
    padded[half - 1 : half - 1 + len(signal)] = signal
    view = np.lib.stride_tricks.sliding_window_view(padded, 2 * half)
    out = np.empty(n_out, dtype=np.float32)
    for r in range(up):
        base = (r * down) // up  # floor of the source position of output r (+ m*down for output r + m*up)
        count = len(range(r, n_out, up))
        if count == 0:
            continue
        out[r::up] = view[base : base + count * down : down][:count] @ taps[r]
    return out


def _decode(np: Any, frames: bytes, width: int, channels: int) -> Any:
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("unsupported sample width")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return data


def voice_bytes_for_send(data: bytes, tts: Any, logger: Any = None) -> bytes:
    """The audio to hand to the platform: converted to 24 kHz mono, except Irodori set to its native output."""
    settings = getattr(tts, "settings", None)
    if isinstance(settings, dict) and settings.get("engine") == "irodori":
        irodori = settings.get("irodori")
        if isinstance(irodori, dict) and irodori.get("output_mode") == "native":
            return data
    return to_qq_voice_wav(data, logger)


def to_qq_voice_wav(data: bytes, logger: Any = None) -> bytes:
    """Convert a WAV to 24 kHz mono 16-bit (never up-sampling); return `data` itself if it is already so
    or cannot be converted (a warning is logged then)."""
    try:
        import numpy as np

        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
        if channels < 1 or rate < 1:
            raise ValueError("invalid wav header")
        if channels == 1 and width == 2 and rate <= TARGET_RATE:
            return data
        signal = _decode(np, frames, width, channels)
        out_rate = rate
        if rate > TARGET_RATE:
            signal = _resample_down(np, signal, rate)
            out_rate = TARGET_RATE
        pcm = np.clip(np.rint(signal * 32768.0), -32768, 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(out_rate)
            writer.writeframes(pcm.tobytes())
        return buffer.getvalue()
    except Exception as error:
        if logger is not None:
            try:
                logger.warning(f"[STBridge] Voice format conversion skipped ({type(error).__name__})")
            except Exception:
                pass
        return data

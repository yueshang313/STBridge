"""AI song covers: runs the RVC worker on demand and turns a library song into a cover (with a cache).

Jobs run one at a time; up to MAX_WAITING more wait. The worker process is started when the first job
needs it and stopped after it has been idle for a while (to give the GPU memory back).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .cover_config import DEREVERB_MODELS, SEPARATION_MODELS, model_stem, resolve_index
from .cover_library import SONG_MAX_BYTES, CoverCache, SongLibrary, cache_key
from .emotion_config import EmotionConfigError
from .music_source import MusicClient, MusicError, SOURCE_LABELS, SEARCH_LIMIT, normalize_text, pick_result, redact
from .tts_service import FAILED, STOPPED, ServiceManager, ServiceStartError

MAX_WAITING = 3
WORKER_START_TIMEOUT_SECONDS = 180.0
IDLE_CHECK_SECONDS = 30.0
WORKER_SCRIPT = Path(__file__).resolve().parent / "cover_worker" / "rvc_cover_worker.py"
LOG_FILE_NAME = "cover_service.log"
RVC_ROOT_NOT_SET_TEXT = "未设置 RVC 目录"
_KIND_RE = re.compile(r"[^A-Za-z0-9:_]")
_STEM_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\s]+')
STEMS_DIR_NAME = "cover_stems"


class CoverError(Exception):
    """A cover job failure; `kind` is a short, path-free name for the log and the console."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _safe_kind(value: Any) -> str:
    text = _KIND_RE.sub("", str(value or ""))[:60]
    return text or "WorkerError"


def probe_duration(rvc_root: str, path: Path) -> float:
    """Seconds of an audio file via the RVC package's own ffprobe; 0.0 when it cannot be read."""
    if not rvc_root:
        return 0.0
    probe = Path(rvc_root) / "ffprobe.exe"
    try:
        completed = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
            timeout=30,
        )
        return float(completed.stdout.decode("ascii", "ignore").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


async def post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    """POST JSON to the worker; returns (status, body)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, json=payload) as response:
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                body = {}
            return response.status, body if isinstance(body, dict) else {}


def clip_window(song: dict[str, Any], settings: dict[str, Any]) -> tuple[float, float]:
    """(start, seconds) of the part to sing: the song's own start, else a percentage into the song."""
    duration = float(song.get("duration") or 0)
    seconds = float(settings["clip_seconds"])
    if seconds <= 0 or duration <= 0:
        return 0.0, 0.0 if seconds <= 0 else seconds
    if song.get("clip_start") is not None:
        start = float(song["clip_start"])
    else:
        start = duration * settings["default_start_percent"] / 100.0
    # Keep the whole clip inside the song.
    start = max(0.0, min(start, duration - seconds))
    return round(start, 2), round(min(seconds, duration - start), 2)


def effective_pitch(settings: dict[str, Any], vocal: int | None = None, inst: int | None = None) -> tuple[int, int]:
    """(f0_up_key, inst_pitch) the worker gets: the overall shift moves everything, the vocal shift only the voice.

    `vocal` / `inst` default to the saved settings (None = not overridden). The voice ends up shifted by
    vocal + overall, the accompaniment by the overall shift alone.
    """
    vocal_shift = settings["f0_up_key"] if vocal is None else vocal
    overall = settings["inst_pitch"] if inst is None else inst
    return vocal_shift + overall, overall


def job_params(settings: dict[str, Any], index_path: str, start: float, seconds: float) -> dict[str, Any]:
    """Everything that changes the audio of a cover (the cache key covers exactly this)."""
    return {
        "model": settings["model"],
        "index_path": index_path,
        "f0_up_key": settings["f0_up_key"],
        "inst_pitch": settings["inst_pitch"],
        "f0_method": settings["f0_method"],
        "index_rate": settings["index_rate"],
        "rms_mix_rate": settings["rms_mix_rate"],
        "protect": settings["protect"],
        "sep_model": settings["sep_model"],
        "dereverb_model": settings["dereverb_model"],
        "vocal_reverb": settings["vocal_reverb"],
        "clip_start": start,
        "clip_seconds": seconds,
        "vocal_gain_db": settings["vocal_gain_db"],
        "inst_gain_db": settings["inst_gain_db"],
        "output_sample_rate": settings["output_sample_rate"],
    }


def params_summary(params: dict[str, Any]) -> str:
    pitch = params["f0_up_key"]
    inst = f" · 伴奏 {params['inst_pitch']:+d}" if params["inst_pitch"] else ""
    window = (
        "整首" if not params["clip_seconds"]
        else f"{params['clip_start']:g}s 起 {params['clip_seconds']:g}s"
    )
    separation = SEPARATION_MODELS.get(params["sep_model"], (params["sep_model"],))[0]
    if params["dereverb_model"]:
        separation += " + " + DEREVERB_MODELS.get(params["dereverb_model"], (params["dereverb_model"],))[0]
    reverb = f" · 混响 {params['vocal_reverb']}" if params["vocal_reverb"] > 0 else ""
    rate = f" · {params.get('output_sample_rate', 44100) / 1000:g} kHz"
    return (
        f"{model_stem(params['model'])} · 人声 {pitch:+d}{inst} · {params['f0_method']} · "
        f"索引 {params['index_rate']:g} · {separation}{reverb} · {window}{rate}"
    )


def stems_dir_for(data_dir: Path, title: str, now: datetime) -> Path:
    """`<data_dir>/cover_stems/<yyyyMMdd-HHmmss>_<safe title>/` for the intermediate files of one cover."""
    safe = _STEM_NAME_RE.sub("_", str(title or "")).strip("_. ")[:40] or "song"
    return Path(data_dir) / STEMS_DIR_NAME / f"{now:%Y%m%d-%H%M%S}_{safe}"


class CoverService:
    def __init__(
        self,
        *,
        data_dir: Path,
        get_settings: Callable[[], dict[str, Any]],
        logger: Any,
        worker_script: Path = WORKER_SCRIPT,
        manager_factory: Callable[..., Any] = ServiceManager,
        post: Callable[[str, dict[str, Any], float], Awaitable[tuple[int, dict[str, Any]]]] = post_json,
        probe: Callable[[str, Path], float] = probe_duration,
        clock: Callable[[], float] = time.monotonic,
        music: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.library = SongLibrary(self.data_dir)
        self.cache = CoverCache(self.data_dir)
        self._get_settings = get_settings
        self._logger = logger
        self._worker_script = Path(worker_script)
        self._manager_factory = manager_factory
        self._post = post
        self._probe = probe
        self._clock = clock
        self._manager: Any = None
        self._manager_key: tuple[str, int] | None = None
        self._lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()
        self._music = music or MusicClient(lambda: self._get_settings().get("music_api_key", ""))
        self._queued = 0  # running + waiting
        self._last_activity = clock()
        self._idle_task: asyncio.Task | None = None

    # ---- worker process

    def _wanted_key(self, settings: dict[str, Any]) -> tuple[str, int]:
        return settings["rvc_root"], settings["port"]

    def manager(self) -> Any:
        """The service manager for the current RVC root and port (rebuilt when either changed)."""
        settings = self._get_settings()
        key = self._wanted_key(settings)
        if self._manager is None or self._manager_key != key:
            root = settings["rvc_root"]
            self._manager = self._manager_factory(
                host="127.0.0.1",
                port=settings["port"],
                launch_dir=root,
                start_timeout=WORKER_START_TIMEOUT_SECONDS,
                stop_on_exit=True,
                data_dir=self.data_dir,
                logger=self._logger,
                service_name="RVC",
                python_path=str(Path(root) / "runtime" / "python.exe"),
                args=[str(self._worker_script), "--host", "127.0.0.1", "--port", str(settings["port"])],
                required_files=[str(self._worker_script)],
                log_name=LOG_FILE_NAME,
            )
            self._manager_key = key
        return self._manager

    async def status(self) -> dict[str, Any]:
        status = await self.manager().status()
        if not self._get_settings()["rvc_root"] and status["state"] in (STOPPED, FAILED):
            status["message"] = RVC_ROOT_NOT_SET_TEXT
        status["busy"] = self._queued > 0
        status["waiting"] = max(0, self._queued - 1)
        return status

    async def start_service(self) -> str:
        return await self.manager().ensure_started()

    async def stop_service(self) -> dict[str, Any]:
        return await self.manager().stop()

    async def apply_settings(self, old: dict[str, Any], new: dict[str, Any]) -> bool:
        """After saving: a changed RVC root or port stops the old worker. Returns whether it did."""
        if self._wanted_key(old) == self._wanted_key(new):
            return False
        manager = self._manager
        self._manager = None
        self._manager_key = None
        if manager is None:
            return False
        result = await manager.stop()
        return bool(result.get("ok"))

    # ---- idle stop

    def start_idle_watch(self, interval: float = IDLE_CHECK_SECONDS) -> None:
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_loop(interval))

    async def _idle_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.idle_check()
            except Exception as error:
                self._log(f"[STBridge] Cover idle check failed ({type(error).__name__})", "error")

    async def idle_check(self) -> bool:
        """Stop the worker this plugin started when it has been idle long enough; True if it did."""
        minutes = self._get_settings()["idle_stop_minutes"]
        manager = self._manager
        if minutes <= 0 or manager is None or self._queued > 0:
            return False
        if self._clock() - self._last_activity < minutes * 60:
            return False
        result = await manager.stop()
        if result.get("ok"):
            self._log(f"[STBridge] Cover worker stopped after {minutes} minutes idle")
        return bool(result.get("ok"))

    async def terminate(self) -> None:
        task, self._idle_task = self._idle_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        manager = self._manager
        if manager is not None:
            await manager.terminate()

    def _log(self, message: str, level: str = "info") -> None:
        try:
            getattr(self._logger, level)(message)
        except Exception:
            pass

    # ---- library

    async def add_song(self, filename: Any, content: bytes, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._get_settings()["rvc_root"]:
            raise EmotionConfigError(400, "RVC folder is not set (set it on the cover page first)")
        song = await asyncio.to_thread(self.library.add, filename, content, meta)
        settings = self._get_settings()
        duration = await asyncio.to_thread(self._probe, settings["rvc_root"], self.library.path(song))
        if duration <= 0:
            self.library.remove_quietly(song["id"])
            raise EmotionConfigError(400, "cannot read the audio (is the RVC folder set correctly?)")
        await asyncio.to_thread(self.library.set_duration, song["id"], duration)
        return self.library.get(song["id"]) or song

    async def clear_cache(self) -> dict[str, int]:
        """Delete all finished covers (songs are never touched). Raises CoverError("Busy") while a job runs or waits."""
        if self._queued > 0:
            raise CoverError("Busy")
        async with self._lock:
            if self._queued > 0:
                raise CoverError("Busy")
            removed, freed = await asyncio.to_thread(self.cache.clear)
        self._log(f"[STBridge] Cover cache cleared files={removed} bytes={freed}")
        return {"removed": removed, "bytes": freed}

    # ---- online songs

    def _music_key(self) -> str:
        return str(self._get_settings().get("music_api_key") or "")

    def _music_log(self, message: str, level: str = "info") -> None:
        self._log(redact(message, self._music_key()), level)

    def find_in_library(self, name: str, artist: str | None = None) -> dict[str, Any] | None:
        """A library song whose normalized title equals `name` (and whose artist matches, when both are known)."""
        wanted, wanted_artist = normalize_text(name), normalize_text(artist)
        if not wanted:
            return None
        matches = [song for song in self.library.list() if normalize_text(song.get("title")) == wanted]
        if wanted_artist:
            exact = [song for song in matches if wanted_artist in normalize_text(song.get("artist"))]
            matches = exact or [song for song in matches if not song.get("artist")]
        return matches[0] if matches else None

    def library_matches(self, name: str, artist: str | None = None) -> list[dict[str, Any]]:
        """Every library song titled `name`; with `artist` only those whose artist contains it."""
        wanted, wanted_artist = normalize_text(name), normalize_text(artist)
        if not wanted:
            return []
        matches = [song for song in self.library.list() if normalize_text(song.get("title")) == wanted]
        if wanted_artist:
            matches = [song for song in matches if wanted_artist in normalize_text(song.get("artist"))]
        return matches

    async def resolve_online_song(self, source: str, source_id: str, name: str, artist: str = "") -> dict[str, Any]:
        """The library song of an online search result: the stored one, or a fresh download."""
        existing = self.library.find_by_source(source, str(source_id))
        if existing is not None and self.library.path(existing).is_file():
            return existing
        return await self.download_online(source, str(source_id), name, artist)

    def cover_file(self, sung: dict[str, Any]) -> Path:
        """The cached wav of a `sing` result."""
        return self.cache.path_for(sung["cover"]["key"])

    def _require_key(self) -> None:
        if not self._music_key().strip():
            raise CoverError("MusicKeyNotSet")

    async def search_online(self, name: str, source: str) -> dict[str, Any]:
        """Search one source, or (`auto`) every configured source and merge the results in source order."""
        self._require_key()
        settings = self._get_settings()
        sources = list(settings["sources"]) if source == "auto" else [source]
        outcomes = await asyncio.gather(
            *(self._music.search(name, item, SEARCH_LIMIT) for item in sources), return_exceptions=True
        )
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item, outcome in zip(sources, outcomes):
            if isinstance(outcome, MusicError):
                errors.append({"source": item, "error": outcome.kind})
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.extend(outcome)
        for item in errors:
            self._music_log(f"[STBridge] Cover online search source={item['source']} failed ({item['error']})", "warning")
        return {"results": results, "errors": errors}

    async def _store_online(self, content: bytes, *, name: str, artist: str, source: str, source_id: str) -> tuple[dict[str, Any], bool]:
        """Add downloaded bytes to the library; (song, newly_added). The same file already there is not a failure."""
        existing = self.library.find_by_sha(hashlib.sha256(content).hexdigest())
        if existing is not None:
            return existing, False
        meta = {"title": name, "artist": artist, "source": source, "source_id": source_id}
        try:
            return await self.add_song(f"{name}.audio", content, meta), True
        except EmotionConfigError as error:
            if error.status == 409:
                existing = self.library.find_by_sha(hashlib.sha256(content).hexdigest())
                if existing is not None:
                    return existing, False
            raise

    async def _fetch_one(self, source: str, source_id: str, name: str, artist: str) -> dict[str, Any]:
        """Download one online song into the library; a preview clip is removed again (CoverError preview_only)."""
        settings = self._get_settings()
        try:
            url = await self._music.get_url(source_id, source)
            content = await self._music.download(
                url, max_bytes=SONG_MAX_BYTES, timeout=float(settings["download_timeout_seconds"])
            )
        except MusicError as error:
            raise CoverError(error.kind) from None
        song, added = await self._store_online(content, name=name, artist=artist, source=source, source_id=source_id)
        if added and float(song.get("duration") or 0) < settings["min_full_seconds"]:
            await asyncio.to_thread(self.library.remove_quietly, song["id"])
            raise CoverError("preview_only")
        return song

    async def download_online(self, source: str, source_id: str, name: str, artist: str = "") -> dict[str, Any]:
        """Console download of a chosen search result."""
        self._require_key()
        if source not in SOURCE_LABELS:
            raise CoverError("invalid source")
        async with self._fetch_lock:
            try:
                song = await self._fetch_one(source, str(source_id), name, artist)
            except CoverError as error:
                self._music_log(f"[STBridge] Cover online download source={source} failed ({error.kind})", "warning")
                raise
            except EmotionConfigError as error:
                self._music_log(f"[STBridge] Cover online download source={source} rejected ({error.message})", "warning")
                raise
        self._music_log(f"[STBridge] Cover online download source={source} ok title={song['title'][:20]}")
        return song

    async def find_or_fetch(self, name: str, artist: str | None = None) -> dict[str, Any]:
        """The library song for a title; otherwise fetched from the sources in order (first success wins).

        Returns {"song", "source" ("library" or the source code), "downloaded"}. Raises CoverError.
        """
        name = " ".join(str(name or "").split())
        artist = " ".join(str(artist or "").split()) or None
        if not normalize_text(name):
            raise CoverError("not_found")
        async with self._fetch_lock:
            found = self.find_in_library(name, artist)
            if found is not None:
                return {"song": found, "source": "library", "downloaded": False}
            self._require_key()
            for source in list(self._get_settings()["sources"]):
                try:
                    results = await self._music.search(name, source, SEARCH_LIMIT)
                    picked = pick_result(results, name, artist)
                    if picked is None:
                        self._music_log(f"[STBridge] Cover online source={source} no match")
                        continue
                    song = await self._fetch_one(source, picked["id"], picked["name"], picked["artist"])
                except (MusicError, CoverError) as error:
                    kind = error.kind
                    self._music_log(f"[STBridge] Cover online source={source} failed ({kind})", "warning")
                    continue
                except EmotionConfigError as error:
                    self._music_log(f"[STBridge] Cover online source={source} rejected ({error.message})", "warning")
                    continue
                self._music_log(f"[STBridge] Cover online source={source} ok title={song['title'][:20]}")
                return {"song": song, "source": source, "downloaded": True}
        raise CoverError("not_found")

    # ---- sing

    async def sing(
        self, song_id: str, *, f0_up_key: int | None = None, inst_pitch: int | None = None
    ) -> dict[str, Any]:
        """Produce (or reuse) the cover of a library song. Raises CoverError."""
        started = self._clock()
        settings = dict(self._get_settings())
        settings["f0_up_key"], settings["inst_pitch"] = effective_pitch(settings, f0_up_key, inst_pitch)
        song = self.library.get(song_id)
        title = (song or {}).get("title", "?")
        label = title[:20]
        key = "none"
        result = "failed:OtherError"
        timings: dict[str, Any] = {}
        try:
            if not settings["enabled"]:
                raise CoverError("Disabled")
            if not settings["rvc_root"]:
                raise CoverError("RvcRootNotSet")
            if not settings["model"]:
                raise CoverError("NoModel")
            if song is None:
                raise CoverError("SongNotFound")
            song_path = self.library.path(song)
            if not song_path.is_file():
                raise CoverError("SongFileMissing")
            start, seconds = clip_window(song, settings)
            params = job_params(settings, resolve_index(settings), start, seconds)
            key = cache_key(song["sha256"], params)
            hit = self.cache.get(key)
            if hit is not None:
                result = "cached"
                return {"cached": True, "cover": hit, "timings": hit.get("timings", {})}
            if self._queued >= MAX_WAITING + 1:
                raise CoverError("QueueFull")
            self._queued += 1
            try:
                async with self._lock:
                    hit = self.cache.get(key)  # an identical job may have finished while we waited
                    if hit is not None:
                        result = "cached"
                        return {"cached": True, "cover": hit, "timings": hit.get("timings", {})}
                    timings = await self._run_job(settings, song_path, key, params, title)
            finally:
                self._queued -= 1
                self._last_activity = self._clock()
            entry = self.cache.add(
                key,
                song_id=song["id"],
                title=title,
                summary=params_summary(params),
                duration=timings.get("duration", 0),
                timings=timings,
            )
            result = "ok"
            return {"cached": False, "cover": entry, "timings": entry["timings"]}
        except CoverError as error:
            result = f"failed:{error.kind}"
            raise
        except asyncio.CancelledError:
            result = "failed:Cancelled"
            raise
        except Exception as error:
            result = f"failed:{type(error).__name__}"
            raise CoverError(type(error).__name__) from error
        finally:
            total = int((self._clock() - started) * 1000)
            self._log(
                f"[STBridge] Cover song={label} model={model_stem(settings['model']) or 'none'} key={key[:8]} "
                f"result={result} sep_ms={timings.get('sep_ms', 0)} dereverb_ms={timings.get('dereverb_ms', 0)} vc_ms={timings.get('vc_ms', 0)} "
                f"mix_ms={timings.get('mix_ms', 0)} total_ms={timings.get('total_ms', total)}"
            )

    async def _run_job(
        self, settings: dict[str, Any], song_path: Path, key: str, params: dict[str, Any], title: str = ""
    ) -> dict[str, Any]:
        manager = self.manager()
        try:
            await manager.ensure_started()
        except ServiceStartError as error:
            raise CoverError("ServiceStartFailed") from error
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        part = self.cache.partial_path(key)
        final = self.cache.path_for(key)
        payload = {"input_path": str(song_path), "output_path": str(part), **params}
        if settings.get("keep_stems"):
            # Not part of `params`: keeping the intermediate files does not change the cover (or its cache key).
            payload["stems_dir"] = str(stems_dir_for(self.data_dir, title, datetime.now()))
        url = f"http://127.0.0.1:{settings['port']}/cover"
        try:
            status, body = await self._post(url, payload, float(settings["job_timeout_seconds"]))
        except asyncio.TimeoutError as error:
            # The worker may still be crunching: stop it so the next job starts clean.
            await manager.stop()
            part.unlink(missing_ok=True)
            raise CoverError("Timeout") from error
        except (aiohttp.ClientError, OSError) as error:
            part.unlink(missing_ok=True)
            raise CoverError("WorkerUnreachable") from error
        if status != 200 or not body.get("ok"):
            part.unlink(missing_ok=True)
            raise CoverError(_safe_kind(body.get("error") if body else f"HTTP{status}"))
        if not part.is_file():
            raise CoverError("OutputMissing")
        os.replace(part, final)
        return {name: body[name] for name in ("sep_ms", "dereverb_ms", "pitch_ms", "vc_ms", "mix_ms", "total_ms", "duration") if name in body}

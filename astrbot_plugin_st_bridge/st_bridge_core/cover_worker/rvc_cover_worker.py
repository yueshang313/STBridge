r"""RVC cover worker: a tiny HTTP service the bridge starts with the RVC package's own Python.

Run it with ``<rvc_root>\runtime\python.exe rvc_cover_worker.py --host 127.0.0.1 --port N`` and with the
RVC root as the working directory (RVC's own scripts find their packages through ``os.getcwd()``).

Targets the RVC 2026 package (Python 3.12 runtime, Roformer/MSST separation, ``infer.vc.modules.VC``) but the
pure helpers also import under any recent Python, so the bridge's tests can exercise them: every heavy import
(torch, RVC, fastapi) happens inside a function.

Pipeline: cut a clip -> remove the accompaniment (MSST) -> remove the reverb from the vocals (MSST, optional)
-> RVC conversion -> mix with the accompaniment of the first step.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SAMPLE_RATE = 44100
MAX_CLIP_SECONDS = 600
# Separation models the worker may load (MSST model ids of the RVC package).
SEPARATION_MODELS = ("vocals-bs-roformer-368", "vocals-bs-roformer-317")
DEREVERB_MODELS = ("dereverb-less-aggressive-18.8050", "dereverb-anvuew-19.1729")
F0_METHODS = ("rmvpe", "fcpe", "pm")
# Names under which the intermediate files are kept when the request has a stems_dir.
STEM_FILES = ("clip.wav", "vocals.wav", "instrumental.wav", "instrumental_shifted.wav", "dry.wav", "converted.wav")


class CoverError(Exception):
    """A failure with a short, path-free type name the bridge can show."""

    def __init__(self, kind, status=500):
        super().__init__(kind)
        self.kind = kind
        self.status = status


# ---------------------------------------------------------------- pure helpers


def parse_args(argv):
    """The worker's own command line; unknown arguments are an error (RVC's parsers see none)."""
    parser = argparse.ArgumentParser(prog="rvc_cover_worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("the worker only listens on 127.0.0.1")
    if not 1 <= args.port <= 65535:
        raise SystemExit("invalid port")
    return args


def _number(data, key, default, low, high, integer=False):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoverError("InvalidParameter:" + key, 400)
    if integer and value != int(value):
        raise CoverError("InvalidParameter:" + key, 400)
    if not low <= value <= high:
        raise CoverError("InvalidParameter:" + key, 400)
    return int(value) if integer else float(value)


def _text(data, key, required=True):
    value = data.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise CoverError("InvalidParameter:" + key, 400)
    return value.strip()


def _output_rate(data):
    """Final output rate: 24000 (mono) or 44100 (stereo); a missing field means 44100 (older callers)."""
    value = data.get("output_sample_rate", SAMPLE_RATE)
    if isinstance(value, bool) or value not in (24000, 44100):
        raise CoverError("InvalidParameter:output_sample_rate", 400)
    return int(value)


def validate_cover_request(data):
    """Check and normalize one /cover body (a CoverError with status 400 on a bad value)."""
    if not isinstance(data, dict):
        raise CoverError("InvalidRequest", 400)
    model = _text(data, "model")
    if os.path.basename(model) != model or not model.lower().endswith(".pth"):
        raise CoverError("InvalidParameter:model", 400)
    sep_model = _text(data, "sep_model")
    if sep_model not in SEPARATION_MODELS:
        raise CoverError("InvalidParameter:sep_model", 400)
    dereverb_model = _text(data, "dereverb_model", required=False)
    if dereverb_model and dereverb_model not in DEREVERB_MODELS:
        raise CoverError("InvalidParameter:dereverb_model", 400)
    f0_method = _text(data, "f0_method") if "f0_method" in data else "rmvpe"
    if f0_method not in F0_METHODS:
        raise CoverError("InvalidParameter:f0_method", 400)
    stems_dir = _text(data, "stems_dir", required=False)
    if stems_dir and not os.path.isabs(stems_dir):
        raise CoverError("InvalidParameter:stems_dir", 400)
    return {
        "input_path": _text(data, "input_path"),
        "output_path": _text(data, "output_path"),
        "model": model,
        "index_path": _text(data, "index_path", required=False),
        "f0_up_key": _number(data, "f0_up_key", 0, -24, 24, integer=True),
        "inst_pitch": _number(data, "inst_pitch", 0, -12, 12, integer=True),
        "f0_method": f0_method,
        "index_rate": _number(data, "index_rate", 0.75, 0.0, 1.0),
        "rms_mix_rate": _number(data, "rms_mix_rate", 0.25, 0.0, 1.0),
        "protect": _number(data, "protect", 0.33, 0.0, 0.5),
        "sep_model": sep_model,
        "dereverb_model": dereverb_model,
        "vocal_reverb": _number(data, "vocal_reverb", 0, 0, 100, integer=True),
        "stems_dir": stems_dir,
        "clip_start": _number(data, "clip_start", 0, 0, 86400),
        "clip_seconds": _number(data, "clip_seconds", 60, 0, MAX_CLIP_SECONDS * 6),
        "vocal_gain_db": _number(data, "vocal_gain_db", 0, -30, 30),
        "inst_gain_db": _number(data, "inst_gain_db", 0, -30, 30),
        "output_sample_rate": _output_rate(data),
    }


def build_clip_command(ffmpeg, input_path, output_path, start, seconds):
    """Cut a clip and convert it to 44.1 kHz stereo 16-bit wav (cut first, so the separators only see the clip)."""
    command = [ffmpeg, "-y", "-v", "error"]
    if start > 0:
        command += ["-ss", "%.3f" % start]
    command += ["-i", input_path, "-vn"]
    if seconds > 0:
        command += ["-t", "%.3f" % seconds]
    command += ["-ac", "2", "-ar", str(SAMPLE_RATE), "-acodec", "pcm_s16le", output_path]
    return command


def reverb_filter(vocal_reverb):
    """The aecho filter for the converted vocals: "" when off, else a short multi-tap echo scaled 0-100."""
    if vocal_reverb <= 0:
        return ""
    strength = min(float(vocal_reverb), 100.0) / 100.0
    return ",aecho=0.8:0.9:40|90|150|240:%.3f|%.3f|%.3f|%.3f" % (
        0.5 * strength, 0.4 * strength, 0.3 * strength, 0.2 * strength,
    )


def build_mix_command(
    ffmpeg, vocal_path, instrument_path, output_path, vocal_gain_db, inst_gain_db, vocal_reverb=0,
    output_rate=SAMPLE_RATE,
):
    """Mix the converted vocals with the accompaniment; both are resampled to 44.1 kHz stereo first.

    The final output is 44.1 kHz stereo, or 24 kHz mono when `output_rate` is 24000 (QQ voices end up 24 kHz mono anyway).
    """
    graph = (
        "[0:a]aresample=%d,aformat=channel_layouts=stereo,volume=%.2fdB%s[v];"
        "[1:a]aresample=%d,aformat=channel_layouts=stereo,volume=%.2fdB[i];"
        # amix halves every input; volume=2 puts the level back, the limiter guards against clipping.
        "[v][i]amix=inputs=2:duration=longest:dropout_transition=0,volume=2,alimiter=limit=0.95[o]"
    ) % (SAMPLE_RATE, vocal_gain_db, reverb_filter(vocal_reverb), SAMPLE_RATE, inst_gain_db)
    return [
        ffmpeg, "-y", "-v", "error", "-i", vocal_path, "-i", instrument_path,
        "-filter_complex", graph, "-map", "[o]", "-ac", "1" if output_rate == 24000 else "2", "-ar", str(output_rate),
        "-acodec", "pcm_s16le", output_path,
    ]


def error_kind(error):
    """A short path-free name for an exception."""
    if isinstance(error, CoverError):
        return error.kind
    return type(error).__name__


def conversion_ok(result):
    """Whether vc_single returned audio: (info, (sample_rate, audio)); a failure gives (info, (None, None))."""
    try:
        sample_rate, audio = result[1]
    except (TypeError, IndexError, ValueError):
        return False
    return sample_rate is not None and audio is not None


# ---------------------------------------------------------------- the engine (heavy parts)


def run_command(command, kind):
    """Run an ffmpeg command; on failure raise CoverError(kind) without exposing the command line."""
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise CoverError(kind + "Unavailable")
    if completed.returncode != 0:
        raise CoverError(kind + "Failed")


def probe_duration(ffmpeg, path):
    """Seconds of an audio file, or 0.0 when ffprobe cannot tell."""
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe") if os.path.dirname(ffmpeg) else "ffprobe"
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return float(completed.stdout.decode("ascii", "ignore").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def shift_instrumental(source, destination, semitones):
    """Pitch-shift the accompaniment by `semitones` without changing its speed (pedalboard, imported only here)."""
    try:
        import pedalboard
    except Exception:
        raise CoverError("PitchShiftUnavailable")
    try:
        import soundfile

        audio, rate = soundfile.read(source, dtype="float32", always_2d=True)  # samples x channels
        board = pedalboard.Pedalboard([pedalboard.PitchShift(semitones=semitones)])
        shifted = board(audio.T, rate)  # pedalboard works on channels x samples
        soundfile.write(destination, shifted.T, rate, subtype="FLOAT")
    except CoverError:
        raise
    except Exception:
        raise CoverError("PitchShiftFailed")
    if not os.path.isfile(destination):
        raise CoverError("PitchShiftFailed")


def keep_stems(stems_dir, files):
    """Copy {file name: source path} into stems_dir; any failure is swallowed (returns whether all were copied)."""
    try:
        os.makedirs(stems_dir, exist_ok=True)
        for name, source in files.items():
            if source and os.path.isfile(source):
                shutil.copyfile(source, os.path.join(stems_dir, name))
        return True
    except OSError:
        return False


class Engine:
    """Loads RVC and the two Roformer separators once and keeps the last model of each; one job at a time."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.ffmpeg = os.path.join(root, "ffmpeg.exe") if os.path.isfile(os.path.join(root, "ffmpeg.exe")) else "ffmpeg"
        self._vc = None
        self._vc_model = None
        # stage -> (model id, separator); stages are "sep" (accompaniment) and "dereverb"
        self._separators = {}
        self._config = None
        self._imports = None

    def _load_imports(self):
        if self._imports is None:
            from configs.config import Config  # noqa: E402  (needs cwd on sys.path and a clean argv)
            from infer.vc.modules import VC
            from tools.pymss_webui import MSSTBatchSeparator, resolve_model

            self._config = Config()
            self._imports = (VC, MSSTBatchSeparator, resolve_model)
        return self._imports

    def _get_vc(self, model):
        VC = self._load_imports()[0]
        if self._vc is None:
            self._vc = VC(self._config)
        if self._vc_model != model:
            self._vc.get_vc(model)
            self._vc_model = model
        return self._vc

    def _get_separator(self, stage, model_id, desired_root, secondary_root):
        """The cached separator of a stage for model_id, pointed at this job's output directories."""
        _, MSSTBatchSeparator, resolve_model = self._load_imports()
        cached = self._separators.get(stage)
        if cached is not None and cached[0] == model_id:
            separator = cached[1]
            separator.desired_root = os.path.abspath(desired_root)
            separator.secondary_root = os.path.abspath(secondary_root)
            os.makedirs(separator.desired_root, exist_ok=True)
            os.makedirs(separator.secondary_root, exist_ok=True)
            return separator
        if cached is not None:
            self._separators.pop(stage, None)
            try:
                cached[1].close()
            except Exception:
                pass
            self._free_gpu()
        spec = resolve_model(model_id)
        weights = os.getenv("weight_pymss_root") or "assets/pymss_weights"
        if not os.path.isfile(os.path.join(weights, spec.model_file)) or not os.path.isfile(
            os.path.join(weights, spec.config_file)
        ):
            raise CoverError("SepModelNotFound", 404)
        separator = MSSTBatchSeparator(spec, "wav", desired_root, secondary_root)
        self._separators[stage] = (model_id, separator)
        return separator

    def _separate(self, stage, model_id, source, work_dir):
        """(main result path, residual path) of one separation; CoverError on failure."""
        desired_root = os.path.join(work_dir, "main")
        secondary_root = os.path.join(work_dir, "rest")
        separator = self._get_separator(stage, model_id, desired_root, secondary_root)
        try:
            outputs = separator.separate_file(source).get("outputs") or []
        except CoverError:
            raise
        except Exception:
            raise CoverError("SeparationFailed")
        if len(outputs) < 2 or not all(path and os.path.isfile(path) for path in outputs[:2]):
            raise CoverError("SeparationOutputMissing")
        return outputs[0], outputs[1]

    def _free_gpu(self):
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def cover(self, request):
        """Run the whole pipeline; returns the timings (ms) and the output length (seconds)."""
        try:
            self._load_imports()
        except Exception:
            raise CoverError("RvcImportFailed")
        weights = os.path.join(os.getenv("weight_root") or "assets/weights", request["model"])
        if not os.path.isfile(weights):
            raise CoverError("ModelNotFound", 404)
        if not os.path.isfile(request["input_path"]):
            raise CoverError("InputNotFound", 404)
        temp = tempfile.mkdtemp(prefix="stbridge_cover_")
        started = time.time()
        timings = {}
        stems = {}
        try:
            clip = os.path.join(temp, "clip.wav")
            run_command(
                build_clip_command(self.ffmpeg, request["input_path"], clip, request["clip_start"], request["clip_seconds"]),
                "Clip",
            )
            if not os.path.isfile(clip):
                raise CoverError("ClipFailed")
            stems["clip.wav"] = clip

            mark = time.time()
            vocal, instrument = self._separate("sep", request["sep_model"], clip, os.path.join(temp, "sep"))
            stems["vocals.wav"], stems["instrumental.wav"] = vocal, instrument
            timings["sep_ms"] = int((time.time() - mark) * 1000)

            dry = vocal
            timings["dereverb_ms"] = 0
            if request["dereverb_model"]:
                mark = time.time()
                dry, _reverb = self._separate("dereverb", request["dereverb_model"], vocal, os.path.join(temp, "dereverb"))
                stems["dry.wav"] = dry
                timings["dereverb_ms"] = int((time.time() - mark) * 1000)

            mark = time.time()
            try:
                vc = self._get_vc(request["model"])
                result = vc.vc_single(
                    0, dry, request["f0_up_key"], request["f0_method"],
                    request["index_path"] or "", request["index_rate"], 0,
                    request["rms_mix_rate"], request["protect"],
                )
            except CoverError:
                raise
            except Exception:
                raise CoverError("ConversionFailed")
            # vc_single does not raise: a failure comes back as (info, (None, None)).
            if not conversion_ok(result):
                raise CoverError("ConversionFailed")
            import soundfile

            converted = os.path.join(temp, "converted.wav")
            soundfile.write(converted, result[1][1], result[1][0])
            stems["converted.wav"] = converted
            timings["vc_ms"] = int((time.time() - mark) * 1000)

            if request["inst_pitch"]:
                mark = time.time()
                shifted = os.path.join(temp, "instrumental_shifted.wav")
                shift_instrumental(instrument, shifted, request["inst_pitch"])
                instrument = shifted
                stems["instrumental_shifted.wav"] = shifted
                timings["pitch_ms"] = int((time.time() - mark) * 1000)

            mark = time.time()
            os.makedirs(os.path.dirname(request["output_path"]) or ".", exist_ok=True)
            run_command(
                build_mix_command(
                    self.ffmpeg, converted, instrument, request["output_path"],
                    request["vocal_gain_db"], request["inst_gain_db"], request["vocal_reverb"],
                    request["output_sample_rate"],
                ),
                "Mix",
            )
            if not os.path.isfile(request["output_path"]):
                raise CoverError("MixFailed")
            timings["mix_ms"] = int((time.time() - mark) * 1000)
            timings["total_ms"] = int((time.time() - started) * 1000)
            timings["duration"] = round(probe_duration(self.ffmpeg, request["output_path"]), 2)
            return timings
        finally:
            if request.get("stems_dir"):
                keep_stems(request["stems_dir"], stems)
            shutil.rmtree(temp, ignore_errors=True)
            self._free_gpu()


# ---------------------------------------------------------------- HTTP app


def create_app(engine):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True, "busy": engine.lock.locked()}

    @app.post("/cover")
    async def cover(request: Request):
        try:
            data = validate_cover_request(await request.json())
        except CoverError as error:
            return JSONResponse({"error": error.kind}, status_code=error.status)
        except Exception:
            return JSONResponse({"error": "InvalidRequest"}, status_code=400)
        if not engine.lock.acquire(blocking=False):
            return JSONResponse({"error": "Busy"}, status_code=409)

        def work():
            try:
                return engine.cover(data)
            finally:
                engine.lock.release()

        try:
            timings = await run_in_threadpool(work)
        except CoverError as error:
            return JSONResponse({"error": error.kind}, status_code=error.status)
        except Exception as error:
            return JSONResponse({"error": error_kind(error)}, status_code=500)
        return {"ok": True, **timings}

    return app


def set_rvc_environment():
    """The variables the RVC 2026 package reads (its webui sets the same defaults); must run before importing RVC."""
    # RVC's load_audio runs a bare "ffmpeg": make the package's own ffmpeg.exe findable even when the
    # working directory is not searched (it is the same binary the worker itself uses for clip and mix).
    if os.path.isfile(os.path.join(os.getcwd(), "ffmpeg.exe")):
        os.environ["PATH"] = os.getcwd() + os.pathsep + os.environ.get("PATH", "")
    for name, value in (
        ("weight_root", "assets/weights"),
        ("weight_pymss_root", "assets/pymss_weights"),
        ("index_root", "logs"),
        ("outside_index_root", "assets/indices"),
        ("rmvpe_root", "assets/rmvpe"),
        ("RVC_CUDA_GRAPH", "0"),
        ("OPENBLAS_NUM_THREADS", "1"),
    ):
        os.environ.setdefault(name, value)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # RVC's own argument parsers read sys.argv: hand them an empty command line.
    sys.argv = sys.argv[:1]
    sys.path.append(os.getcwd())
    set_rvc_environment()
    import uvicorn

    engine = Engine(os.getcwd())
    uvicorn.run(create_app(engine), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""P7a/P7d: the RVC cover worker's pure helpers and its pipeline (heavy parts replaced by fakes)."""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

WORKER_PATH = Path(__file__).resolve().parents[1] / "st_bridge_core" / "cover_worker" / "rvc_cover_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("rvc_cover_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_worker()


def request(**overrides):
    data = {
        "input_path": "in.mp3",
        "output_path": "out.wav",
        "model": "kotoneV2_e150_s15450.pth",
        "sep_model": "vocals-bs-roformer-368",
        "dereverb_model": "dereverb-less-aggressive-18.8050",
    }
    data.update(overrides)
    return data


class ImportTests(unittest.TestCase):
    def test_importing_the_worker_needs_no_heavy_packages(self):
        code = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('w', r'{WORKER_PATH}');"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "print(sorted(name for name in ('torch','fastapi','uvicorn','librosa','dotenv','configs') if name in sys.modules))"
        )
        import subprocess

        output = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(output.stdout.strip(), "[]", output.stderr)

    def test_source_parses(self):
        import ast

        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"), feature_version=(3, 12))
        self.assertIsNotNone(tree)
        source = WORKER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("match ", source.replace("matches", ""))


class ArgsTests(unittest.TestCase):
    def test_own_arguments(self):
        args = worker.parse_args(["--host", "127.0.0.1", "--port", "8099"])
        self.assertEqual((args.host, args.port), ("127.0.0.1", 8099))
        self.assertEqual(worker.parse_args(["--port", "9000"]).host, "127.0.0.1")

    def test_bad_arguments_are_refused(self):
        for argv in (["--port", "0"], ["--port", "70000"], ["--host", "0.0.0.0", "--port", "8099"], [], ["--port", "8099", "--bogus"]):
            with self.assertRaises(SystemExit):
                worker.parse_args(argv)

    def test_main_hands_rvc_an_empty_command_line(self):
        calls = []
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = lambda app, **kwargs: calls.append((app, kwargs))
        seen = {}

        class FakeEngine:
            def __init__(self, root):
                seen["argv_at_engine"] = list(sys.argv)

        saved_argv, saved_path = list(sys.argv), list(sys.path)
        try:
            with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
                    patch.object(worker, "Engine", FakeEngine), \
                    patch.object(worker, "create_app", lambda engine: "app"):
                sys.argv = ["worker.py", "--port", "8123"]
                worker.main()
                self.assertEqual(sys.argv, ["worker.py"])
                self.assertIn(os.getcwd(), sys.path)
        finally:
            sys.argv, sys.path[:] = saved_argv, saved_path
        self.assertEqual(seen["argv_at_engine"], ["worker.py"])
        self.assertEqual(calls[0][1]["host"], "127.0.0.1")
        self.assertEqual(calls[0][1]["port"], 8123)


class ValidationTests(unittest.TestCase):
    def test_defaults_and_normalisation(self):
        data = worker.validate_cover_request(request(f0_up_key=5, clip_seconds=30, vocal_gain_db=-3))
        self.assertEqual(data["f0_up_key"], 5)
        self.assertEqual(data["f0_method"], "rmvpe")
        self.assertEqual(data["index_path"], "")
        self.assertEqual(data["index_rate"], 0.75)
        self.assertEqual(data["clip_seconds"], 30.0)
        self.assertEqual(data["vocal_gain_db"], -3.0)
        self.assertEqual(data["sep_model"], "vocals-bs-roformer-368")
        self.assertEqual(data["dereverb_model"], "dereverb-less-aggressive-18.8050")
        self.assertEqual((data["vocal_reverb"], data["stems_dir"]), (0, ""))
        self.assertNotIn("filter_radius", data)
        self.assertNotIn("uvr_model", data)

    def test_new_fields(self):
        stems = os.path.abspath(os.path.join(os.sep, "stems", "x"))
        data = worker.validate_cover_request(
            request(sep_model="vocals-bs-roformer-317", dereverb_model="", vocal_reverb=40, stems_dir=stems, f0_method="fcpe")
        )
        self.assertEqual(data["sep_model"], "vocals-bs-roformer-317")
        self.assertEqual(data["dereverb_model"], "")
        self.assertEqual((data["vocal_reverb"], data["stems_dir"], data["f0_method"]), (40, stems, "fcpe"))
        self.assertEqual(worker.validate_cover_request(request(dereverb_model="dereverb-anvuew-19.1729"))["dereverb_model"],
                         "dereverb-anvuew-19.1729")

    def test_bad_values_are_400_with_a_short_kind(self):
        bad = [
            request(model="../x.pth"),
            request(model="x.bin"),
            request(model=""),
            request(sep_model="a/b"),
            request(sep_model="karaoke-mel-roformer-10.1956"),
            request(sep_model="HP5_only_main_vocal"),
            request(dereverb_model="vocals-bs-roformer-368"),
            request(dereverb_model="nope"),
            request(f0_method="magic"),
            request(f0_method="harvest"),
            request(f0_method="crepe"),
            request(f0_up_key=25),
            request(f0_up_key=-25),
            request(inst_pitch=13),
            request(inst_pitch=1.5),
            request(inst_pitch=True),
            request(f0_up_key=1.5),
            request(f0_up_key=True),
            request(index_rate=1.1),
            request(protect=0.6),
            request(vocal_reverb=101),
            request(vocal_reverb=-1),
            request(vocal_reverb=1.5),
            request(stems_dir="relative/dir"),
            request(clip_start=-1),
            request(input_path=""),
            request(vocal_gain_db=99),
        ]
        for data in bad:
            with self.assertRaises(worker.CoverError, msg=str(data)) as caught:
                worker.validate_cover_request(data)
            self.assertEqual(caught.exception.status, 400)
            self.assertNotIn("\\", caught.exception.kind)
        missing = request()
        del missing["sep_model"]
        with self.assertRaises(worker.CoverError) as caught:
            worker.validate_cover_request(missing)
        self.assertEqual(caught.exception.kind, "InvalidParameter:sep_model")
        with self.assertRaises(worker.CoverError):
            worker.validate_cover_request([])


OLD_MIX_GRAPH = (
    "[0:a]aresample=44100,aformat=channel_layouts=stereo,volume=2.50dB[v];"
    "[1:a]aresample=44100,aformat=channel_layouts=stereo,volume=-4.00dB[i];"
    "[v][i]amix=inputs=2:duration=longest:dropout_transition=0,volume=2,alimiter=limit=0.95[o]"
)


class CommandTests(unittest.TestCase):
    def test_clip_command_cuts_first_and_converts(self):
        command = worker.build_clip_command("ffmpeg.exe", "in.mp3", "clip.wav", 12.5, 60)
        self.assertEqual(command[0], "ffmpeg.exe")
        self.assertLess(command.index("-ss"), command.index("-i"))  # seek before the input: fast cut
        self.assertEqual(command[command.index("-ss") + 1], "12.500")
        self.assertEqual(command[command.index("-t") + 1], "60.000")
        self.assertEqual(command[command.index("-ar") + 1], "44100")
        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertEqual(command[-1], "clip.wav")

    def test_whole_song_has_no_cut(self):
        command = worker.build_clip_command("ffmpeg", "in.mp3", "clip.wav", 0, 0)
        self.assertNotIn("-ss", command)
        self.assertNotIn("-t", command)

    def test_mix_command_applies_gains_and_resamples_both(self):
        command = worker.build_mix_command("ffmpeg", "v.wav", "i.wav", "out.wav", 2.5, -4)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("volume=2.50dB", graph)
        self.assertIn("volume=-4.00dB", graph)
        self.assertEqual(graph.count("aresample=44100"), 2)
        self.assertIn("amix=inputs=2", graph)
        self.assertEqual(command[command.index("-map") + 1], "[o]")
        self.assertEqual(command[-1], "out.wav")
        self.assertEqual(command.count("-i"), 2)
        self.assertEqual(command[command.index("-i") + 1], "v.wav")

    def test_mix_graph_without_reverb_is_the_old_graph_byte_for_byte(self):
        for reverb in (None, 0):
            args = ("ffmpeg", "v.wav", "i.wav", "out.wav", 2.5, -4)
            command = worker.build_mix_command(*args) if reverb is None else worker.build_mix_command(*args, vocal_reverb=reverb)
            self.assertEqual(command[command.index("-filter_complex") + 1], OLD_MIX_GRAPH)

    def test_vocal_reverb_only_touches_the_vocal_branch(self):
        weak = worker.build_mix_command("ffmpeg", "v.wav", "i.wav", "o.wav", 0, 0, vocal_reverb=10)
        strong = worker.build_mix_command("ffmpeg", "v.wav", "i.wav", "o.wav", 0, 0, vocal_reverb=100)
        graphs = [c[c.index("-filter_complex") + 1] for c in (weak, strong)]
        for graph in graphs:
            vocal, rest = graph.split("[v];", 1)
            self.assertIn("aecho=", vocal)
            self.assertNotIn("aecho", rest)
            self.assertTrue(rest.endswith("alimiter=limit=0.95[o]"))
        self.assertNotEqual(graphs[0], graphs[1])
        self.assertIn(":0.500|0.400|0.300|0.200", graphs[1])

    def test_no_shell_strings(self):
        for command in (
            worker.build_clip_command("ffmpeg", "a b.mp3", "o.wav", 1, 2),
            worker.build_mix_command("ffmpeg", "a b.wav", "c d.wav", "o.wav", 0, 0, 50),
        ):
            self.assertTrue(all(isinstance(part, str) for part in command))


class ConversionResultTests(unittest.TestCase):
    def test_conversion_ok(self):
        self.assertTrue(worker.conversion_ok(("info", (40000, [0, 1]))))
        self.assertFalse(worker.conversion_ok(("error", (None, None))))
        self.assertFalse(worker.conversion_ok(("info", (40000, None))))
        self.assertFalse(worker.conversion_ok(None))
        self.assertFalse(worker.conversion_ok(("info", None)))
        self.assertFalse(worker.conversion_ok(()))


class FakeSeparator:
    """Stands in for MSSTBatchSeparator: writes <stem>_<suffix>.wav into the roots it is pointed at."""

    def __init__(self, model_id, main_suffix, rest_suffix, log):
        self.model_id = model_id
        self.suffixes = (main_suffix, rest_suffix)
        self.log = log
        self.desired_root = self.secondary_root = None
        self.closed = False

    def separate_file(self, path):
        self.log.append((self.model_id, path))
        stem = Path(path).stem
        outputs = []
        for root, suffix in zip((self.desired_root, self.secondary_root), self.suffixes):
            os.makedirs(root, exist_ok=True)
            target = Path(root, f"{stem}_{suffix}.wav")
            target.write_bytes(self.model_id.encode())
            outputs.append(str(target))
        return {"outputs": outputs}

    def close(self):
        self.closed = True


class FakeVc:
    def __init__(self, result=("info", (40000, [0, 1]))):
        self.calls = []
        self.result = result

    def vc_single(self, *args):
        self.calls.append(args)
        return self.result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "assets" / "weights").mkdir(parents=True)
        (self.root / "assets" / "weights" / "kotoneV2_e150_s15450.pth").write_bytes(b"w")
        self.input = self.root / "song.mp3"
        self.input.write_bytes(b"x")
        self.output = self.root / "out" / "cover.wav"
        self.stems = self.root / "stems" / "run1"
        self.engine = worker.Engine(str(self.root))
        self.separations = []
        self.vc = FakeVc()
        self.engine._load_imports = lambda: None
        self.engine._get_separator = self.fake_get_separator
        self.engine._get_vc = lambda model: self.vc
        self.commands = []
        self.written = []
        fake_soundfile = types.ModuleType("soundfile")
        fake_soundfile.write = lambda path, data, rate: (self.written.append((path, rate)), Path(path).write_bytes(b"c"))
        patches = [
            patch.dict(sys.modules, {"soundfile": fake_soundfile}),
            patch.dict(os.environ, {"weight_root": str(self.root / "assets" / "weights")}),
            patch.object(worker, "run_command", self.fake_run),
            patch.object(worker, "probe_duration", lambda ffmpeg, path: 12.34),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_get_separator(self, stage, model_id, desired_root, secondary_root):
        suffixes = ("noreverb", "reverb") if stage == "dereverb" else ("vocals", "instrumental")
        separator = FakeSeparator(model_id, suffixes[0], suffixes[1], self.separations)
        separator.desired_root, separator.secondary_root = desired_root, secondary_root
        return separator

    def fake_run(self, command, kind):
        self.commands.append((kind, command))
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[-1]).write_bytes(b"audio")

    def data(self, **overrides):
        return worker.validate_cover_request(
            request(input_path=str(self.input), output_path=str(self.output), index_path="idx.index", **overrides)
        )

    def test_full_run_order_and_timings(self):
        timings = self.engine.cover(self.data(f0_up_key=3, clip_start=20, clip_seconds=45))
        self.assertEqual([kind for kind, _ in self.commands], ["Clip", "Mix"])
        clip = self.commands[0][1]
        self.assertEqual(clip[clip.index("-ss") + 1], "20.000")
        self.assertEqual(clip[clip.index("-t") + 1], "45.000")
        # stage 1 separates the clip, stage 2 de-reverbs the vocals of stage 1
        self.assertEqual([model for model, _ in self.separations], ["vocals-bs-roformer-368", "dereverb-less-aggressive-18.8050"])
        self.assertTrue(self.separations[0][1].endswith("clip.wav"))
        self.assertTrue(self.separations[1][1].endswith("clip_vocals.wav"))
        args = self.vc.calls[0]
        self.assertEqual(len(args), 9)
        self.assertEqual(args[0], 0)
        self.assertTrue(args[1].endswith("clip_vocals_noreverb.wav"))  # the dry vocals feed RVC
        self.assertEqual(args[2:], (3, "rmvpe", "idx.index", 0.75, 0, 0.25, 0.33))
        self.assertEqual(self.written[0][1], 40000)
        self.assertTrue(self.output.is_file())
        self.assertEqual(set(timings), {"sep_ms", "dereverb_ms", "vc_ms", "mix_ms", "total_ms", "duration"})
        self.assertEqual(timings["duration"], 12.34)

    def test_dereverb_can_be_skipped(self):
        timings = self.engine.cover(self.data(dereverb_model=""))
        self.assertEqual([model for model, _ in self.separations], ["vocals-bs-roformer-368"])
        self.assertTrue(self.vc.calls[0][1].endswith("clip_vocals.wav"))
        self.assertEqual(timings["dereverb_ms"], 0)

    def test_index_missing_is_passed_as_empty_string(self):
        self.engine.cover(worker.validate_cover_request(
            request(input_path=str(self.input), output_path=str(self.output))
        ))
        self.assertEqual(self.vc.calls[0][4], "")

    def test_mix_uses_the_accompaniment_the_converted_vocal_and_the_reverb(self):
        self.engine.cover(self.data(vocal_gain_db=1, inst_gain_db=-2, vocal_reverb=30))
        mix = self.commands[1][1]
        self.assertTrue(mix[mix.index("-i") + 1].endswith("converted.wav"))
        second = mix.index("-i", mix.index("-i") + 1)
        self.assertTrue(mix[second + 1].endswith("clip_instrumental.wav"))  # the accompaniment of stage 1
        graph = mix[mix.index("-filter_complex") + 1]
        self.assertIn("volume=1.00dB", graph)
        self.assertIn("aecho=", graph)

    def test_stems_are_kept_only_when_asked(self):
        self.engine.cover(self.data())
        self.assertFalse(self.stems.exists())
        self.engine.cover(self.data(stems_dir=str(self.stems)))
        self.assertEqual(
            sorted(path.name for path in self.stems.iterdir()),
            sorted(set(worker.STEM_FILES) - {"instrumental_shifted.wav"}),
        )
        self.assertEqual((self.stems / "instrumental.wav").read_bytes(), b"vocals-bs-roformer-368")
        self.assertEqual((self.stems / "dry.wav").read_bytes(), b"dereverb-less-aggressive-18.8050")

    def test_stems_without_dereverb_have_no_dry_file(self):
        self.engine.cover(self.data(dereverb_model="", stems_dir=str(self.stems)))
        self.assertEqual(
            sorted(path.name for path in self.stems.iterdir()),
            ["clip.wav", "converted.wav", "instrumental.wav", "vocals.wav"],
        )

    def test_a_stems_failure_does_not_fail_the_cover(self):
        with patch.object(worker.shutil, "copyfile", side_effect=OSError("disk full")):
            timings = self.engine.cover(self.data(stems_dir=str(self.stems)))
        self.assertTrue(self.output.is_file())
        self.assertEqual(timings["duration"], 12.34)

    def test_temp_files_are_removed_even_on_failure(self):
        before = set(Path(tempfile.gettempdir()).glob("stbridge_cover_*"))
        self.engine.cover(self.data())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("stbridge_cover_*")), before)
        self.vc.result = ("error", (None, None))
        with self.assertRaises(worker.CoverError) as caught:
            self.engine.cover(self.data())
        self.assertEqual(caught.exception.kind, "ConversionFailed")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("stbridge_cover_*")), before)

    def test_error_kinds_never_contain_paths(self):
        with self.assertRaises(worker.CoverError) as caught:
            self.engine.cover(self.data(model="missing.pth"))
        self.assertEqual((caught.exception.kind, caught.exception.status), ("ModelNotFound", 404))
        self.input.unlink()
        with self.assertRaises(worker.CoverError) as caught:
            self.engine.cover(self.data())
        self.assertEqual(caught.exception.kind, "InputNotFound")

    def test_separation_and_conversion_failures_are_named(self):
        def boom(*args, **kwargs):
            raise RuntimeError(str(self.root) + " secret")

        original = FakeSeparator.separate_file
        FakeSeparator.separate_file = boom
        try:
            with self.assertRaises(worker.CoverError) as caught:
                self.engine.cover(self.data())
        finally:
            FakeSeparator.separate_file = original
        self.assertEqual(caught.exception.kind, "SeparationFailed")
        self.assertNotIn(str(self.root), caught.exception.kind)
        self.vc.vc_single = boom
        with self.assertRaises(worker.CoverError) as caught:
            self.engine.cover(self.data())
        self.assertEqual(caught.exception.kind, "ConversionFailed")

    def test_missing_separation_output(self):
        original = FakeSeparator.separate_file
        FakeSeparator.separate_file = lambda self, path: {"outputs": [str(Path(path).with_name("nothing.wav"))]}
        try:
            with self.assertRaises(worker.CoverError) as caught:
                self.engine.cover(self.data())
        finally:
            FakeSeparator.separate_file = original
        self.assertEqual(caught.exception.kind, "SeparationOutputMissing")

    def test_error_kind_helper(self):
        self.assertEqual(worker.error_kind(worker.CoverError("X")), "X")
        self.assertEqual(worker.error_kind(ValueError("path C:\\secret")), "ValueError")


class InstPitchValidationTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(worker.validate_cover_request(request())["inst_pitch"], 0)
        self.assertEqual(worker.validate_cover_request(request(inst_pitch=-12))["inst_pitch"], -12)
        self.assertEqual(worker.validate_cover_request(request(inst_pitch=12))["inst_pitch"], 12)
        self.assertEqual(worker.validate_cover_request(request(f0_up_key=24))["f0_up_key"], 24)
        self.assertEqual(worker.validate_cover_request(request(f0_up_key=-24))["f0_up_key"], -24)
        for bad in (13, -13, 1.5, True, "2", None):
            with self.assertRaises(worker.CoverError, msg=str(bad)) as caught:
                worker.validate_cover_request(request(inst_pitch=bad))
            self.assertEqual((caught.exception.kind, caught.exception.status), ("InvalidParameter:inst_pitch", 400))


class FakeArray:
    """Enough of a numpy array for the pitch step: .T flips a marker."""

    def __init__(self, transposed=False):
        self.transposed = transposed

    @property
    def T(self):
        return FakeArray(not self.transposed)


class PitchShiftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = os.path.join(self.directory.name, "inst.wav")
        self.target = os.path.join(self.directory.name, "shifted.wav")
        Path(self.source).write_bytes(b"x")

    def fake_modules(self):
        seen = {}

        class PitchShift:
            def __init__(self, semitones):
                seen["semitones"] = semitones

        class Pedalboard:
            def __init__(self, plugins):
                seen["plugins"] = plugins

            def __call__(self, audio, rate):
                seen["call"] = (audio.transposed, rate)  # pedalboard wants channels x samples
                return FakeArray(transposed=False)

        pedalboard = types.ModuleType("pedalboard")
        pedalboard.PitchShift, pedalboard.Pedalboard = PitchShift, Pedalboard
        soundfile = types.ModuleType("soundfile")

        def read(path, dtype=None, always_2d=False):
            seen["read"] = (path, dtype, always_2d)
            return FakeArray(transposed=False), 44100  # samples x channels

        def write(path, data, rate, subtype=None):
            seen["write"] = (data.transposed, rate, subtype)  # flipped back to samples x channels
            Path(path).write_bytes(b"w")

        soundfile.read, soundfile.write = read, write
        return {"pedalboard": pedalboard, "soundfile": soundfile}, seen

    def test_shifts_with_pedalboard_in_the_right_shape(self):
        modules, seen = self.fake_modules()
        with patch.dict(sys.modules, modules):
            worker.shift_instrumental(self.source, self.target, 2)
        self.assertEqual(seen["semitones"], 2)
        self.assertEqual(seen["read"], (self.source, "float32", True))
        self.assertEqual(seen["call"], (True, 44100))
        self.assertEqual(seen["write"], (True, 44100, "FLOAT"))
        self.assertTrue(os.path.isfile(self.target))

    def test_missing_pedalboard_is_named(self):
        with patch.dict(sys.modules, {"pedalboard": None}):
            with self.assertRaises(worker.CoverError) as caught:
                worker.shift_instrumental(self.source, self.target, 2)
        self.assertEqual(caught.exception.kind, "PitchShiftUnavailable")

    def test_a_failure_while_shifting_is_named_without_paths(self):
        modules, _ = self.fake_modules()

        def boom(*args, **kwargs):
            raise RuntimeError(self.source + " secret")

        modules["soundfile"].read = boom
        with patch.dict(sys.modules, modules):
            with self.assertRaises(worker.CoverError) as caught:
                worker.shift_instrumental(self.source, self.target, 2)
        self.assertEqual(caught.exception.kind, "PitchShiftFailed")


class InstPitchPipelineTests(PipelineTests):
    def setUp(self):
        super().setUp()
        self.shifts = []

        def fake_shift(source, destination, semitones):
            self.shifts.append((source, destination, semitones))
            Path(destination).write_bytes(b"shifted")

        patcher = patch.object(worker, "shift_instrumental", fake_shift)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_zero_never_touches_pedalboard(self):
        with patch.dict(sys.modules, {"pedalboard": None}):  # importing it would raise
            timings = self.engine.cover(self.data(inst_pitch=0))
        self.assertEqual(self.shifts, [])
        self.assertNotIn("pitch_ms", timings)
        mix = self.commands[1][1]
        second = mix.index("-i", mix.index("-i") + 1)
        self.assertTrue(mix[second + 1].endswith("clip_instrumental.wav"))

    def test_the_real_shift_function_is_not_reached_when_zero(self):
        # with the real function back in place, a missing pedalboard must not matter for inst_pitch=0
        real = patch.object(worker, "shift_instrumental", self.real_shift)
        real.start()
        self.addCleanup(real.stop)
        with patch.dict(sys.modules, {"pedalboard": None}):
            self.engine.cover(self.data(inst_pitch=0))
            with self.assertRaises(worker.CoverError) as caught:
                self.engine.cover(self.data(inst_pitch=2))
        self.assertEqual(caught.exception.kind, "PitchShiftUnavailable")

    real_shift = staticmethod(worker.shift_instrumental)

    def test_nonzero_shifts_the_accompaniment_before_the_mix(self):
        timings = self.engine.cover(self.data(inst_pitch=-2))
        self.assertEqual(len(self.shifts), 1)
        source, destination, semitones = self.shifts[0]
        self.assertTrue(source.endswith("clip_instrumental.wav"))
        self.assertTrue(destination.endswith("instrumental_shifted.wav"))
        self.assertEqual(semitones, -2)
        mix = self.commands[1][1]
        second = mix.index("-i", mix.index("-i") + 1)
        self.assertTrue(mix[second + 1].endswith("instrumental_shifted.wav"))
        self.assertIn("pitch_ms", timings)

    def test_the_shifted_accompaniment_is_kept_with_the_stems(self):
        self.engine.cover(self.data(inst_pitch=3, stems_dir=str(self.stems)))
        self.assertEqual(sorted(path.name for path in self.stems.iterdir()), sorted(worker.STEM_FILES))
        self.assertEqual((self.stems / "instrumental_shifted.wav").read_bytes(), b"shifted")
        self.assertEqual((self.stems / "instrumental.wav").read_bytes(), b"vocals-bs-roformer-368")

    def test_a_pitch_failure_fails_the_job_with_its_name(self):
        def fail(source, destination, semitones):
            raise worker.CoverError("PitchShiftUnavailable")

        with patch.object(worker, "shift_instrumental", fail):
            with self.assertRaises(worker.CoverError) as caught:
                self.engine.cover(self.data(inst_pitch=1))
        self.assertEqual(caught.exception.kind, "PitchShiftUnavailable")


class SeparatorCacheTests(unittest.TestCase):
    """The real Engine._get_separator with the RVC modules faked."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.weights = self.root / "pymss"
        self.weights.mkdir()
        self.created = []
        created = self.created

        class Separator:
            def __init__(self, spec, output_format, desired_root, secondary_root):
                self.spec, self.format = spec, output_format
                self.desired_root, self.secondary_root = desired_root, secondary_root
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        spec = lambda name: types.SimpleNamespace(model_id=name, model_file=name + ".ckpt", config_file=name + ".yaml")
        self.engine = worker.Engine(str(self.root))
        self.engine._imports = (object, Separator, spec)
        patcher = patch.dict(os.environ, {"weight_pymss_root": str(self.weights)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, name):
        (self.weights / (name + ".ckpt")).write_bytes(b"c")
        (self.weights / (name + ".yaml")).write_bytes(b"y")

    def test_cached_per_stage_and_replaced_on_model_change(self):
        for name in ("a", "b", "r"):
            self.install(name)
        first = self.engine._get_separator("sep", "a", str(self.root / "o1"), str(self.root / "s1"))
        again = self.engine._get_separator("sep", "a", str(self.root / "o2"), str(self.root / "s2"))
        self.assertIs(first, again)
        self.assertEqual(again.desired_root, os.path.abspath(self.root / "o2"))  # re-pointed at the new job's folders
        other_stage = self.engine._get_separator("dereverb", "r", str(self.root / "o3"), str(self.root / "s3"))
        self.assertIsNot(first, other_stage)
        self.assertFalse(first.closed)
        replaced = self.engine._get_separator("sep", "b", str(self.root / "o4"), str(self.root / "s4"))
        self.assertTrue(first.closed)
        self.assertFalse(other_stage.closed)
        self.assertEqual((replaced.spec.model_id, replaced.format), ("b", "wav"))

    def test_missing_checkpoint_is_404(self):
        (self.weights / "a.ckpt").write_bytes(b"c")  # yaml missing
        with self.assertRaises(worker.CoverError) as caught:
            self.engine._get_separator("sep", "a", str(self.root / "o"), str(self.root / "s"))
        self.assertEqual((caught.exception.kind, caught.exception.status), ("SepModelNotFound", 404))
        self.assertEqual(self.created, [])


class EnvironmentTests(unittest.TestCase):
    def test_rvc_environment_defaults_and_overrides(self):
        names = ("weight_root", "weight_pymss_root", "index_root", "outside_index_root", "rmvpe_root",
                 "RVC_CUDA_GRAPH", "OPENBLAS_NUM_THREADS")
        with patch.dict(os.environ, {}, clear=False):
            for name in names:
                os.environ.pop(name, None)
            os.environ["index_root"] = "custom"
            worker.set_rvc_environment()
            self.assertEqual(os.environ["rmvpe_root"], "assets/rmvpe")
            self.assertEqual(os.environ["weight_pymss_root"], "assets/pymss_weights")
            self.assertEqual(os.environ["outside_index_root"], "assets/indices")
            self.assertEqual(os.environ["weight_root"], "assets/weights")
            self.assertEqual(os.environ["RVC_CUDA_GRAPH"], "0")
            self.assertEqual(os.environ["index_root"], "custom")  # setdefault never overrides

    def test_main_sets_the_environment_before_anything_else_loads(self):
        order = []
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.run = lambda app, **kwargs: None

        class FakeEngine:
            def __init__(self, root):
                order.append("engine")

        saved_argv, saved_path = list(sys.argv), list(sys.path)
        try:
            with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
                    patch.object(worker, "Engine", FakeEngine), \
                    patch.object(worker, "create_app", lambda engine: "app"), \
                    patch.object(worker, "set_rvc_environment", lambda: order.append("env")):
                sys.argv = ["worker.py", "--port", "8123"]
                worker.main()
        finally:
            sys.argv, sys.path[:] = saved_argv, saved_path
        self.assertEqual(order, ["env", "engine"])

    def test_no_dotenv_and_no_uvr_in_the_source(self):
        source = WORKER_PATH.read_text(encoding="utf-8")
        for word in ("dotenv", "uvr5", "AudioPre", "filter_radius", "uvr_model"):
            self.assertNotIn(word, source)

    def test_load_imports_uses_the_new_modules(self):
        fake_config_module = types.ModuleType("configs.config")
        fake_config_module.Config = lambda: "config"
        modules = {"configs": types.ModuleType("configs"), "configs.config": fake_config_module}
        for name in ("infer", "infer.vc", "tools"):
            modules[name] = types.ModuleType(name)
        modules["infer.vc.modules"] = types.ModuleType("infer.vc.modules")
        modules["infer.vc.modules"].VC = "VC"
        modules["tools.pymss_webui"] = types.ModuleType("tools.pymss_webui")
        modules["tools.pymss_webui"].MSSTBatchSeparator = "MSST"
        modules["tools.pymss_webui"].resolve_model = "resolve"
        engine = worker.Engine(".")
        with patch.dict(sys.modules, modules):
            self.assertEqual(engine._load_imports(), ("VC", "MSST", "resolve"))
        self.assertEqual(engine._config, "config")


if __name__ == "__main__":
    unittest.main()

"""R4c: release/verify_release.py — the comparison logic on fake releases (no network, no cache)."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

REPO = PLUGIN_ROOT.parent
SCRIPT = REPO / "release" / "verify_release.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "release/verify_release.py is not in this tree")
class VerifyReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vr = load("verify_release_under_test", SCRIPT)
        cls.br = cls.vr.br

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        embedded = self.root / "embed.zip"
        with zipfile.ZipFile(embedded, "w") as archive:
            archive.writestr("python.exe", b"MZ")
            archive.writestr("python312._pth", "python312.zip\r\n.\r\n#import site\r\n")
        wheels = self.root / "wheels"
        wheels.mkdir()
        with zipfile.ZipFile(wheels / "alpha-1.0-py3-none-any.whl", "w") as archive:
            archive.writestr("alpha/__init__.py", "V = 1\n")
        repo = self.root / "repo"
        for relative, text in {
            "astrbot_plugin_st_bridge/main.py": "# plugin", "astrbot_plugin_st_bridge/metadata.yaml": "n",
            "astrbot_plugin_st_bridge/st_bridge_core/version.py": 'CORE_VERSION = "9.9.9"\n',
            "st_bridge_client/manifest.json": "{}", "st_bridge_client/index.js": "// c",
        }.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.reference = self.br.assemble(repo, self.root / "ref" / "STBridge-9.9.9", embedded, wheels, "9.9.9")
        self.deployed = self.root / "deployed"
        shutil.copytree(self.reference, self.deployed)

    def test_identical_folders_are_clean_and_data_is_never_listed(self):
        (self.deployed / "data").mkdir()
        (self.deployed / "data" / "config.json").write_text("{}", encoding="utf-8")
        seen = []
        real = os.scandir

        def recording(path=".", *args, **kwargs):
            seen.append(str(path))
            return real(path, *args, **kwargs)

        with patch("os.scandir", recording):
            outcome = self.vr.compare_release(self.reference, self.deployed)
        self.assertTrue(self.vr.is_clean(outcome), outcome)
        self.assertGreater(len(outcome["files"]), 5)
        data = str(self.deployed / "data")
        self.assertFalse([path for path in seen if path == data or path.startswith(data + os.sep)], "data\\ was listed")
        self.assertEqual(self.vr.summary("release", outcome).splitlines()[0], f"release: files {len(outcome['files'])}, diff 0, extra []")

    def test_changed_missing_and_extra_files_are_named(self):
        (self.deployed / "start.bat").write_bytes(b"@echo changed\r\n")
        (self.deployed / "README.txt").unlink()
        (self.deployed / "core" / "extra.py").write_text("x", encoding="utf-8")
        outcome = self.vr.compare_release(self.reference, self.deployed)
        self.assertEqual(outcome["changed"], ["start.bat"])
        self.assertEqual(outcome["missing"], ["README.txt"])
        self.assertEqual(outcome["extra"], ["core/extra.py"])
        text = self.vr.summary("release", outcome)
        self.assertIn("diff 2, extra ['core/extra.py']", text)
        self.assertIn("changed: ['start.bat']", text)
        self.assertIn("missing: ['README.txt']", text)
        self.assertFalse(self.vr.is_clean(outcome))

    def test_pycache_files_written_by_a_running_core_are_ignored(self):
        cache = self.deployed / "core" / "st_bridge_core" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "x.cpython-312.pyc").write_bytes(b"pyc")
        (self.deployed / "core" / "stray.pyc").write_bytes(b"pyc")
        self.assertTrue(self.vr.is_clean(self.vr.compare_release(self.reference, self.deployed)))

    def test_the_installed_plugin_equals_the_packages_addon_apart_from_core_link(self):
        plugin = self.root / "AstrBot" / "plugins" / "astrbot_plugin_st_bridge"
        shutil.copytree(self.reference / "addons" / "astrbot_plugin_st_bridge", plugin)
        (plugin / "core_link.json").write_text(json.dumps({"data_dir": "C:\\x\\data"}), encoding="utf-8")
        (plugin / "__pycache__").mkdir()
        (plugin / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"x")
        outcome = self.vr.compare_folder(self.reference / "addons" / "astrbot_plugin_st_bridge", plugin, self.vr.PLUGIN_SKIPPED_FILES)
        self.assertTrue(self.vr.is_clean(outcome), outcome)
        self.assertEqual(self.vr.check_core_link(plugin), "")
        (plugin / "main.py").write_text("# old plugin", encoding="utf-8")
        outcome = self.vr.compare_folder(self.reference / "addons" / "astrbot_plugin_st_bridge", plugin, self.vr.PLUGIN_SKIPPED_FILES)
        self.assertEqual(outcome["changed"], ["main.py"])
        (plugin / "core_link.json").write_text("{}", encoding="utf-8")
        self.assertIn("no data_dir", self.vr.check_core_link(plugin))
        (plugin / "core_link.json").unlink()
        self.assertIn("missing", self.vr.check_core_link(plugin))

    def test_a_missing_cache_is_reported_not_crashed(self):
        with self.assertRaises(FileNotFoundError):
            self.vr.cached_inputs(self.root / "no-cache")
        code = self.vr.main([str(self.deployed), "--cache", str(self.root / "no-cache")])
        self.assertEqual(code, 2)

    def test_a_missing_deployed_folder_is_exit_1(self):
        self.assertEqual(self.vr.main([str(self.root / "nowhere")]), 1)

    def test_the_deployed_folder_must_be_given(self):
        self.assertFalse(hasattr(self.vr, "DEFAULT_DEPLOYED"))
        with contextlib.redirect_stderr(io.StringIO()) as usage:
            with self.assertRaises(SystemExit) as raised:
                self.vr.main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("deployed", usage.getvalue())


if __name__ == "__main__":
    unittest.main()

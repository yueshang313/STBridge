"""The build id: a hash of the core's code on disk, used to tell whether a running core runs the code that is installed."""

import shutil
import tempfile
import unittest
from pathlib import Path

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import compute_build_id, running_build_id

CORE = PLUGIN_ROOT / "st_bridge_core"


class BuildIdTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.copy = Path(self.directory.name) / "st_bridge_core"
        shutil.copytree(CORE, self.copy, ignore=shutil.ignore_patterns("__pycache__"))

    def test_it_is_stable_and_the_running_one_matches_the_disk(self):
        first = compute_build_id(CORE)
        self.assertEqual(first, compute_build_id(CORE))
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertEqual(running_build_id(), first)
        self.assertEqual(compute_build_id(self.copy), first)  # a copy of the same files in another place

    def test_a_changed_python_or_front_end_file_changes_it(self):
        before = compute_build_id(self.copy)
        with open(self.copy / "core.py", "a", encoding="utf-8") as handle:
            handle.write("# changed\n")
        changed = compute_build_id(self.copy)
        self.assertNotEqual(changed, before)
        with open(self.copy / "console_frontend" / "js" / "app.js", "a", encoding="utf-8") as handle:
            handle.write("// changed\n")
        self.assertNotEqual(compute_build_id(self.copy), changed)

    def test_a_new_or_removed_or_renamed_file_changes_it(self):
        before = compute_build_id(self.copy)
        (self.copy / "extra.py").write_text("x = 1\n", encoding="utf-8")
        added = compute_build_id(self.copy)
        self.assertNotEqual(added, before)
        (self.copy / "extra.py").rename(self.copy / "extra2.py")
        self.assertNotEqual(compute_build_id(self.copy), added)
        (self.copy / "extra2.py").unlink()
        self.assertEqual(compute_build_id(self.copy), before)

    def test_tests_caches_and_other_file_types_do_not_count(self):
        before = compute_build_id(self.copy)
        (self.copy / "__pycache__").mkdir(exist_ok=True)
        (self.copy / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"cache")
        (self.copy / "console_frontend" / "tests" / "extra.test.mjs").write_text("test\n", encoding="utf-8")
        (self.copy / "notes.txt").write_text("not code\n", encoding="utf-8")
        (self.copy / "data.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(compute_build_id(self.copy), before)


if __name__ == "__main__":
    unittest.main()

"""R4a: the release build script (release/build_release.py) — assembly logic with fake archives, no network."""

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from package_context import PLUGIN_ROOT, install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.version import CORE_VERSION

REPO = PLUGIN_ROOT.parent
SCRIPT = REPO / "release" / "build_release.py"


def load_script():
    spec = importlib.util.spec_from_file_location("build_release_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "release/build_release.py is not in this tree")
class ReleaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.br = load_script()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    # ---- fake inputs
    def fake_embedded_zip(self, pth="python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n"):
        path = self.root / "embed.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("python.exe", b"MZ fake")
            archive.writestr("python312.dll", b"dll")
            archive.writestr("python312.zip", b"PK")
            archive.writestr("python312._pth", pth)
        return path

    def fake_wheels(self):
        wheels = self.root / "wheels"
        wheels.mkdir()
        with zipfile.ZipFile(wheels / "alpha-1.0-py3-none-any.whl", "w") as archive:
            archive.writestr("alpha/__init__.py", "VALUE = 1\n")
            archive.writestr("alpha-1.0.dist-info/METADATA", "Name: alpha\n")
        with zipfile.ZipFile(wheels / "beta-2.0-cp312-cp312-win_amd64.whl", "w") as archive:
            archive.writestr("beta/_speedups.pyd", b"MZ")
            archive.writestr("beta-2.0.data/purelib/beta_extra.py", "X = 2\n")
        return wheels

    def fake_repo(self):
        repo = self.root / "repo"
        plugin = repo / "astrbot_plugin_st_bridge"
        for relative, text in {
            "main.py": "# plugin",
            "metadata.yaml": "name: x",
            "start_core.bat": "@echo off",
            "core_link.py": "# link",
            "st_bridge_core/__init__.py": "",
            "st_bridge_core/version.py": 'CORE_VERSION = "9.8.7"\n',
            "st_bridge_core/console_frontend/index.html": "<html>",
            "st_bridge_core/console_frontend/tests/logic.test.mjs": "// test",
            "st_bridge_core/__pycache__/x.pyc": "pyc",
            "tests/test_a.py": "# test",
            "data/cmd_config.json": "{}",
            "__pycache__/main.pyc": "pyc",
        }.items():
            path = plugin / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        client = repo / "st_bridge_client"
        for relative in ("index.js", "manifest.json", "tests/bridge_client.test.mjs", "__pycache__/z.pyc"):
            path = client / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        return repo

    def all_files(self, folder):
        return {path.relative_to(folder).as_posix() for path in Path(folder).rglob("*") if path.is_file()}

    # ---- versions
    def test_the_version_comes_from_the_core(self):
        self.assertEqual(self.br.read_core_version(), CORE_VERSION)
        self.assertEqual(self.br.read_core_version(self.fake_repo()), "9.8.7")
        self.assertEqual(self.br.release_name("9.8.7"), "STBridge-9.8.7")

    def test_the_python_pin_is_exact_and_hashed(self):
        self.assertRegex(self.br.PYTHON_VERSION, r"^3\.12\.\d+$")
        self.assertRegex(self.br.EMBED_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn(self.br.PYTHON_VERSION, self.br.EMBED_URL)
        self.assertTrue(self.br.EMBED_URL.startswith("https://www.python.org/"))

    # ---- the embedded Python's path file
    def test_pth_gets_site_packages_the_core_and_import_site(self):
        python_dir = self.root / "python"
        python_dir.mkdir()
        (python_dir / "python312._pth").write_text("python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n", encoding="utf-8")
        self.br.patch_pth(python_dir)
        text = (python_dir / "python312._pth").read_bytes().decode("utf-8")
        self.assertEqual(text, "python312.zip\r\n.\r\nLib\\site-packages\r\n..\\core\r\nimport site\r\n")
        self.br.patch_pth(python_dir)  # idempotent
        self.assertEqual((python_dir / "python312._pth").read_bytes().decode("utf-8"), text)

    def test_pth_that_already_imports_site_ends_with_a_single_import_site(self):
        python_dir = self.root / "python"
        python_dir.mkdir()
        (python_dir / "python312._pth").write_text("python312.zip\n.\nimport site\n", encoding="utf-8")
        self.br.patch_pth(python_dir)
        lines = (python_dir / "python312._pth").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("import site"), 1)
        self.assertEqual(lines[-1], "import site")

    def test_a_missing_or_ambiguous_pth_is_an_error(self):
        python_dir = self.root / "python"
        python_dir.mkdir()
        with self.assertRaises(RuntimeError):
            self.br.patch_pth(python_dir)

    # ---- wheels
    def test_wheels_are_unpacked_into_site_packages(self):
        site = self.root / "site"
        site.mkdir()
        for wheel in self.fake_wheels().glob("*.whl"):
            self.br.unpack_wheel(wheel, site)
        self.assertEqual(
            self.all_files(site),
            {"alpha/__init__.py", "alpha-1.0.dist-info/METADATA", "beta/_speedups.pyd", "beta_extra.py"},
        )

    def test_unsafe_or_unsupported_wheel_entries_are_refused(self):
        site = self.root / "site"
        site.mkdir()
        evil = self.root / "evil-1.0-py3-none-any.whl"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../escape.py", "x")
        with self.assertRaises(RuntimeError):
            self.br.unpack_wheel(evil, site)
        scripts = self.root / "scripts-1.0-py3-none-any.whl"
        with zipfile.ZipFile(scripts, "w") as archive:
            archive.writestr("scripts-1.0.data/scripts/tool.exe", "x")
        with self.assertRaises(RuntimeError):
            self.br.unpack_wheel(scripts, site)
        self.assertFalse((self.root / "escape.py").exists())

    # ---- the whole folder
    def test_the_folder_has_the_agreed_layout_and_nothing_extra(self):
        repo = self.fake_repo()
        out = self.root / "out" / "STBridge-9.8.7"
        result = self.br.assemble(repo, out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        self.assertEqual(result, out)
        files = self.all_files(out)
        self.assertEqual(
            files,
            {
                "start.bat",
                "README.txt",
                "python/python.exe",
                "python/python312.dll",
                "python/python312.zip",
                "python/python312._pth",
                "python/Lib/site-packages/alpha/__init__.py",
                "python/Lib/site-packages/alpha-1.0.dist-info/METADATA",
                "python/Lib/site-packages/beta/_speedups.pyd",
                "python/Lib/site-packages/beta_extra.py",
                "core/st_bridge_core/__init__.py",
                "core/st_bridge_core/version.py",
                "core/st_bridge_core/console_frontend/index.html",
                "addons/astrbot_plugin_st_bridge/main.py",
                "addons/astrbot_plugin_st_bridge/metadata.yaml",
                "addons/astrbot_plugin_st_bridge/core_link.py",
                "addons/astrbot_plugin_st_bridge/st_bridge_core/__init__.py",
                "addons/astrbot_plugin_st_bridge/st_bridge_core/version.py",
                "addons/astrbot_plugin_st_bridge/st_bridge_core/console_frontend/index.html",
                "addons/st_bridge_client/index.js",
                "addons/st_bridge_client/manifest.json",
            },
        )
        self.assertFalse((out / "data").exists())  # the core creates it on its first start
        pth = (out / "python" / "python312._pth").read_text(encoding="utf-8")
        self.assertIn("..\\core", pth)
        self.assertIn("Lib\\site-packages", pth)

    def test_license_and_notices_are_copied_from_the_repo_root_when_present(self):
        repo = self.fake_repo()
        (repo / "LICENSE").write_text("LICENSE TEXT\n", encoding="utf-8")
        (repo / "THIRD_PARTY_NOTICES.md").write_text("# notices\n", encoding="utf-8")
        out = self.root / "out" / "STBridge-legal"
        self.br.assemble(repo, out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        self.assertEqual((out / "LICENSE").read_text(encoding="utf-8"), "LICENSE TEXT\n")
        self.assertEqual((out / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"), "# notices\n")
        self.assertEqual(set(self.br.LEGAL_FILES), {"LICENSE", "THIRD_PARTY_NOTICES.md"})

    def test_readme_and_docs_are_copied_from_the_repo_root_when_present(self):
        repo = self.fake_repo()
        (repo / "README.md").write_text("# readme\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "quickstart.md").write_text("# quickstart\n", encoding="utf-8")
        (repo / "docs" / "character.md").write_text("# character\n", encoding="utf-8")
        out = self.root / "out" / "STBridge-docs"
        self.br.assemble(repo, out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        self.assertEqual((out / "README.md").read_text(encoding="utf-8"), "# readme\n")
        self.assertEqual((out / "docs" / "quickstart.md").read_text(encoding="utf-8"), "# quickstart\n")
        self.assertEqual((out / "docs" / "character.md").read_text(encoding="utf-8"), "# character\n")
        self.assertEqual(self.br.DOC_FILES, ("README.md",))
        self.assertEqual(self.br.DOC_DIRS, ("docs",))

    def test_the_real_readme_and_docs_exist_link_each_other_and_carry_no_developer_paths(self):
        docs = REPO / "docs"
        pages = [REPO / "README.md", docs / "quickstart.md", docs / "character.md"]
        for page in pages:
            self.assertTrue(page.is_file(), page)
        root = pages[0].read_text(encoding="utf-8")
        self.assertIn("docs/quickstart.md", root)
        self.assertIn("docs/character.md", root)
        self.assertIn("AGPL-3.0", root)
        self.assertIn("Windows only", root)  # the English summary at the top
        for page in pages + [PLUGIN_ROOT / "README.md", REPO / "release" / "README.md"]:
            text = page.read_text(encoding="utf-8")
            for word in ("vmware-host", "Shared Folders", "yuesh", "C:\\Users\\", "gmail", "_deployment_backups"):
                self.assertNotIn(word, text, f"{page.name}: {word}")
        # every relative link in the three user-facing pages points at a file in the repo
        for page in pages:
            for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", page.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://")):
                    continue
                self.assertTrue((page.parent / target).exists(), f"{page.name}: broken link {target}")

    def test_the_repo_ships_the_agpl_text_and_notices_for_every_locked_package(self):
        license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 19 November 2007", license_text)
        self.assertGreater(len(license_text.splitlines()), 600)  # the whole text, not a summary
        notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        lock = (REPO / "release" / "requirements-lock.txt").read_text(encoding="utf-8")
        pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock, re.MULTILINE)
        self.assertEqual(len(pins), 16)
        for name, version in pins:
            row = next((line for line in notices.splitlines() if line.startswith("| ") and f"| {version} |" in line
                        and line.split("|")[1].strip().lower().replace("_", "-") == name.lower().replace("_", "-")), None)
            self.assertIsNotNone(row, f"{name} {version} is not in THIRD_PARTY_NOTICES.md")
        self.assertIn("3.12.10", notices)  # the embedded Python
        self.assertIn(self.br.PYTHON_VERSION, notices)

    def test_the_real_repo_goes_in_without_tests_caches_or_the_old_start_script(self):
        out = self.root / "out" / "STBridge-real"
        self.br.assemble(REPO, out, self.fake_embedded_zip(), self.fake_wheels(), CORE_VERSION)
        files = self.all_files(out)
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", (out / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("Version 3", (out / "LICENSE").read_text(encoding="utf-8"))
        self.assertTrue((out / "THIRD_PARTY_NOTICES.md").is_file())
        for name in ("README.md", "docs/quickstart.md", "docs/character.md"):
            self.assertIn(name, files)
            self.assertEqual((out / name).read_bytes(), (REPO / name).read_bytes(), name)
        for path in files:
            parts = path.split("/")
            self.assertNotIn("tests", parts, path)
            self.assertNotIn("__pycache__", parts, path)
            self.assertNotIn("data", parts[:3], path)
            self.assertFalse(path.endswith((".pyc", ".log")), path)
        self.assertNotIn("addons/astrbot_plugin_st_bridge/start_core.bat", files)
        self.assertNotIn("addons/astrbot_plugin_st_bridge/core_process.py", files)
        for needed in (
            "core/st_bridge_core/__main__.py",
            "core/st_bridge_core/runner.py",
            "core/st_bridge_core/console_frontend/index.html",
            "core/st_bridge_core/cover_worker/rvc_cover_worker.py",
            "addons/astrbot_plugin_st_bridge/main.py",
            "addons/astrbot_plugin_st_bridge/core_link.py",
            "addons/astrbot_plugin_st_bridge/st_bridge_core/runner.py",
            "addons/astrbot_plugin_st_bridge/_conf_schema.json",
            "addons/st_bridge_client/index.js",
            "addons/st_bridge_client/bridge_client.mjs",
            "addons/st_bridge_client/manifest.json",
        ):
            self.assertIn(needed, files)
        # The core in the package and the plugin's library copy are the same files.
        core = {p[len("core/st_bridge_core/"):] for p in files if p.startswith("core/st_bridge_core/")}
        library = {p[len("addons/astrbot_plugin_st_bridge/st_bridge_core/"):] for p in files if p.startswith("addons/astrbot_plugin_st_bridge/st_bridge_core/")}
        self.assertEqual(core, library)
        self.assertEqual(
            (out / "core" / "st_bridge_core" / "version.py").read_bytes(),
            (out / "addons" / "astrbot_plugin_st_bridge" / "st_bridge_core" / "version.py").read_bytes(),
        )

    def test_assembling_again_replaces_the_old_folder(self):
        out = self.root / "out" / "STBridge-x"
        out.mkdir(parents=True)
        (out / "stale.txt").write_text("old", encoding="utf-8")
        self.br.assemble(self.fake_repo(), out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        self.assertFalse((out / "stale.txt").exists())

    def test_no_wheels_is_an_error(self):
        empty = self.root / "none"
        empty.mkdir()
        with self.assertRaises(RuntimeError):
            self.br.assemble(self.fake_repo(), self.root / "out", self.fake_embedded_zip(), empty, "1")

    # ---- start.bat / README
    def test_start_bat_is_ascii_crlf_and_relative(self):
        out = self.root / "out" / "STBridge-bat"
        self.br.assemble(self.fake_repo(), out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        raw = (out / "start.bat").read_bytes()
        raw.decode("ascii")  # no other characters: they would be garbled in the console
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertTrue(raw.endswith(b"\r\n"))
        text = raw.decode("ascii")
        self.assertIn("chcp 65001", text)
        self.assertIn("%~dp0", text)
        self.assertIn('"%ROOT%python\\python.exe" -X utf8 -m st_bridge_core --data-dir "%ROOT%data" --launched-by start.bat %OPEN%', text)
        self.assertIn('set "OPEN=--open-console"', text)
        self.assertIn('if "%CODE%"=="75" goto restart', text)  # the console's restart button
        self.assertIn("goto run", text)
        self.assertIn("pause", text)
        self.assertIn("%ERRORLEVEL%", text)
        self.assertNotRegex(text, r"[A-Za-z]:\\")  # no absolute path
        self.assertNotIn("\\users\\", text.lower())  # nobody's profile folder

    @unittest.skipUnless(sys.platform == "win32", "start.bat is a Windows batch file")
    def test_start_bat_starts_again_on_exit_code_75_and_pauses_on_other_codes(self):
        # Run the real script text with a stand-in for python.exe that asks for a restart once.
        real = 'call "%ROOT%fake.cmd"'
        command = '"%ROOT%python\\python.exe" -X utf8 -m st_bridge_core --data-dir "%ROOT%data" --launched-by start.bat %OPEN%'
        script = self.br.START_BAT
        self.assertIn(command, script)
        script = script.replace(command, real + " %OPEN%").replace("\r\npause\r\n", "\r\n")
        for first, second, expected in ((75, 0, "exited with code 0"), (75, 3, "exited with code 3")):
            with self.subTest(second=second):
                folder = self.root / f"bat{second}"
                folder.mkdir()
                (folder / "start.bat").write_bytes(script.encode("ascii"))
                (folder / "fake.cmd").write_bytes(
                    (
                        "@echo off\r\n"
                        'echo %* >> "%~dp0args.txt"\r\n'
                        'if exist "%~dp0ran.txt" exit /b ' + str(second) + "\r\n"
                        'echo x > "%~dp0ran.txt"\r\n'
                        "exit /b " + str(first) + "\r\n"
                    ).encode("ascii")
                )
                done = subprocess.run(
                    ["cmd", "/c", str(folder / "start.bat")], stdin=subprocess.DEVNULL, capture_output=True, timeout=60, cwd=str(folder),
                )
                output = done.stdout.decode("ascii", "replace")
                self.assertEqual(done.returncode, second, output)
                self.assertEqual(output.count("restarting"), 1, output)
                self.assertIn(expected, output)
                runs = (folder / "args.txt").read_text(encoding="ascii").splitlines()
                self.assertEqual(len(runs), 2)
                self.assertIn("--open-console", runs[0])
                self.assertNotIn("--open-console", runs[1])  # the browser opens once, not on every restart

    def test_readme_is_utf8_with_bom_and_names_the_three_steps(self):
        out = self.root / "out" / "STBridge-readme"
        self.br.assemble(self.fake_repo(), out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        raw = (out / "README.txt").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        for needle in ("start.bat", "9.8.7", self.br.PYTHON_VERSION, "astrbot_plugin_st_bridge", "st_bridge_client", "data\\"):
            self.assertIn(needle, text)
        self.assertEqual(text.count("\r\n"), text.count("\n"))

    # ---- zip
    def test_the_zip_holds_the_folder_under_its_own_name(self):
        out = self.root / "out" / "STBridge-9.8.7"
        self.br.assemble(self.fake_repo(), out, self.fake_embedded_zip(), self.fake_wheels(), "9.8.7")
        archive_path = self.br.make_zip(out)
        self.assertEqual(archive_path, self.root / "out" / "STBridge-9.8.7.zip")  # not "STBridge-9.8.zip"
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
        self.assertTrue(all(name.startswith("STBridge-9.8.7/") for name in names))
        self.assertIn("STBridge-9.8.7/start.bat", names)
        self.assertEqual(len(names), len(self.all_files(out)))

    # ---- download cache (a local file stands in for the server)
    def test_the_python_download_is_checked_and_cached(self):
        source = self.fake_embedded_zip()
        digest = self.br.sha256_of(source)
        cache = self.root / "cache"
        url = source.as_uri()
        first = self.br.fetch_embedded_python(cache, url, digest)
        self.assertEqual(first.read_bytes(), source.read_bytes())
        source.unlink()  # a valid cached file is not downloaded again
        self.assertEqual(self.br.fetch_embedded_python(cache, url, digest), first)

    def test_a_download_with_the_wrong_hash_is_refused(self):
        source = self.fake_embedded_zip()
        with self.assertRaises(RuntimeError) as caught:
            self.br.fetch_embedded_python(self.root / "cache", source.as_uri(), "0" * 64)
        self.assertIn("sha256", str(caught.exception))

    def test_a_damaged_cache_file_is_downloaded_again(self):
        source = self.fake_embedded_zip()
        digest = self.br.sha256_of(source)
        cache = self.root / "cache"
        cache.mkdir()
        (cache / source.name).write_bytes(b"broken")
        path = self.br.fetch_embedded_python(cache, source.as_uri(), digest)
        self.assertEqual(path.read_bytes(), source.read_bytes())

    # ---- the pinned dependencies
    def test_the_lock_file_pins_and_hashes_every_package(self):
        text = self.br.LOCK_FILE.read_text(encoding="utf-8")
        entries = re.findall(r"^([A-Za-z0-9_.-]+)==([0-9][^\s\\]*)\s*\\\s*\n\s+--hash=sha256:([0-9a-f]{64})\s*$", text, re.MULTILINE)
        names = {name.lower() for name, _version, _digest in entries}
        self.assertTrue({"aiohttp", "numpy", "httpx"} <= names, names)
        self.assertEqual(len(entries), sum(1 for line in text.splitlines() if "==" in line and not line.startswith("#")))
        core_requirements = (PLUGIN_ROOT / "st_bridge_core" / "requirements.txt").read_text(encoding="utf-8").split()
        for name in core_requirements:
            self.assertIn(name.lower(), names)

    def test_generated_folders_are_not_tracked(self):
        ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/release/dist/", ignored)
        self.assertIn("/release/cache/", ignored)


if __name__ == "__main__":
    unittest.main()

"""The core package must stay free of AstrBot: only the standard library, aiohttp, numpy, httpx and its own modules."""

import ast
import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "st_bridge_core"
ALLOWED_THIRD_PARTY = {"aiohttp", "numpy", "httpx"}  # httpx: the TTS clients import it where they use it
# Names that mean AstrBot / QQ Official concepts; none of them may appear as an identifier or import in the core.
FORBIDDEN_NAMES = {
    "astrbot", "botpy", "AstrMessageEvent", "MessageChain", "MessageType",
    "unified_msg_origin", "handlers_parsed_params", "should_call_llm", "stop_event",
}
# cover_worker/ holds a standalone script that runs inside the RVC runtime, not inside the plugin process.
EXEMPT_DIRECTORIES = {"cover_worker"}


def core_sources():
    for path in sorted(CORE.rglob("*.py")):
        if EXEMPT_DIRECTORIES & set(path.relative_to(CORE).parts):
            continue
        yield path


class CoreBoundaryTests(unittest.TestCase):
    def test_the_core_package_exists_and_has_sources(self):
        self.assertTrue((CORE / "__init__.py").is_file())
        self.assertGreater(len(list(core_sources())), 20)

    def test_imports_are_standard_library_aiohttp_numpy_or_inside_the_core(self):
        problems = []
        for path in core_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 1:
                        continue  # a sibling module of the core package
                    if node.level > 1:
                        problems.append(f"{path.name}:{node.lineno}: relative import leaves the core package")
                        continue
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    root = module.split(".")[0]
                    if root not in sys.stdlib_module_names and root not in ALLOWED_THIRD_PARTY:
                        problems.append(f"{path.name}:{node.lineno}: imports {module}")
        self.assertEqual(problems, [])

    def test_no_astrbot_concepts_in_identifiers(self):
        problems = []
        for path in core_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.Attribute):
                    names = [node.attr]
                elif isinstance(node, ast.arg):
                    names = [node.arg]
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names = [node.name]
                elif isinstance(node, ast.alias):
                    names = [node.name.split(".")[-1]]
                for name in names:
                    if name in FORBIDDEN_NAMES:
                        problems.append(f"{path.name}: uses {name}")
        self.assertEqual(problems, [])

    def test_the_core_declares_its_dependencies(self):
        text = (CORE / "requirements.txt").read_text(encoding="utf-8").lower()
        for name in ALLOWED_THIRD_PARTY:
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()

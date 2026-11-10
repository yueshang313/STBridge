"""Check a deployed release folder against what the repository (HEAD) builds.

    py -3.12 release/verify_release.py DEPLOYED_DIR [--plugin-dir DIR] [--extension-dir DIR] [--cache DIR]

DEPLOYED_DIR (the folder the release was unpacked or copied into) is required; without it the usage is printed and the
exit code is 2. The reference is assembled again from the working tree
and the cache (``release/cache``; no network), then every file of ``start.bat``, ``README.txt``, ``python\\``,
``core\\`` and ``addons\\`` is compared by sha256. Output: ``files N, diff N, extra [...]`` (and the names).

``data\\`` of the deployed folder is skipped completely: it is never listed or opened (the running core and the
share's file locks do not mix with reading it). ``__pycache__`` folders are ignored everywhere (Python writes them
when the core runs). With ``--plugin-dir`` / ``--extension-dir`` the installed AstrBot plugin / ST extension is
compared with ``addons\\astrbot_plugin_st_bridge`` / ``addons\\st_bridge_client`` of the reference (the plugin's
``core_link.json`` is skipped: it is written by the installer, its content is checked on its own).

Exit code 0 when everything matches, 1 otherwise, 2 when the reference cannot be built (no cache).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br  # noqa: E402

SKIPPED_DIRS = {"__pycache__"}
SKIPPED_SUFFIXES = (".pyc", ".pyo")
TOP_LEVEL_SKIPPED = {"data"}  # of the deployed folder: never listed
PLUGIN_SKIPPED_FILES = {"core_link.json"}


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(root: Path, *, skip_top: frozenset[str] | set[str] = frozenset(), skip_files: frozenset[str] | set[str] = frozenset()) -> dict[str, str]:
    """{relative posix path: sha256} of the files under ``root``.

    Folders named in ``skip_top`` (directly under ``root``) and ``__pycache__`` folders are not entered, so they are
    never listed; ``skip_files`` names top-level files to leave out.
    """
    root = Path(root)
    result: dict[str, str] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            listing = sorted(entries, key=lambda entry: entry.name)
        for entry in listing:
            relative = Path(entry.path).relative_to(root)
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIPPED_DIRS or (directory == root and entry.name in skip_top):
                    continue
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                if entry.name.endswith(SKIPPED_SUFFIXES) or (directory == root and entry.name in skip_files):
                    continue
                result[relative.as_posix()] = file_hash(entry.path)
    return result


def compare(reference: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """diff = different or missing, extra = only in ``actual``."""
    diff = sorted(name for name, digest in reference.items() if actual.get(name) != digest)
    changed = [name for name in diff if name in actual]
    missing = [name for name in diff if name not in actual]
    extra = sorted(set(actual) - set(reference))
    return {"files": sorted(reference), "changed": changed, "missing": missing, "extra": extra}


def summary(label: str, outcome: dict[str, list[str]]) -> str:
    diff = len(outcome["changed"]) + len(outcome["missing"])
    lines = [f"{label}: files {len(outcome['files'])}, diff {diff}, extra {outcome['extra']}"]
    if outcome["changed"]:
        lines.append(f"  changed: {outcome['changed']}")
    if outcome["missing"]:
        lines.append(f"  missing: {outcome['missing']}")
    return "\n".join(lines)


def is_clean(outcome: dict[str, list[str]]) -> bool:
    return not (outcome["changed"] or outcome["missing"] or outcome["extra"])


def compare_release(reference_root: Path, deployed: Path) -> dict[str, list[str]]:
    return compare(hash_tree(reference_root), hash_tree(deployed, skip_top=TOP_LEVEL_SKIPPED))


def compare_folder(reference_folder: Path, installed: Path, skip_files: set[str] = frozenset()) -> dict[str, list[str]]:
    return compare(hash_tree(reference_folder), hash_tree(installed, skip_files=skip_files))


def check_core_link(plugin_dir: Path) -> str:
    """The installed plugin's core_link.json must name a data folder (its content is written by the installer)."""
    try:
        value = json.loads((Path(plugin_dir) / "core_link.json").read_text(encoding="utf-8")).get("data_dir")
    except (OSError, ValueError, AttributeError):
        return "core_link.json is missing or unreadable"
    return "" if isinstance(value, str) and value else "core_link.json has no data_dir"


def cached_inputs(cache: Path) -> tuple[Path, Path]:
    embedded = Path(cache) / br.EMBED_NAME
    wheels = Path(cache) / "wheels"
    if not embedded.is_file() or br.sha256_of(embedded) != br.EMBED_SHA256 or not list(wheels.glob("*.whl")):
        raise FileNotFoundError(f"no usable release cache in {cache} (run release/build_release.py once)")
    return embedded, wheels


def build_reference(repo: Path, cache: Path, destination: Path) -> Path:
    embedded, wheels = cached_inputs(cache)
    version = br.read_core_version(repo)
    return br.assemble(repo, destination / br.release_name(version), embedded, wheels, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare a deployed release folder with what HEAD builds.")
    parser.add_argument("deployed", help="the folder the release was unpacked or copied into")
    parser.add_argument("--plugin-dir", help="the installed AstrBot plugin folder (…\\plugins\\astrbot_plugin_st_bridge)")
    parser.add_argument("--extension-dir", help="the installed ST extension folder (…\\extensions\\st_bridge_client)")
    parser.add_argument("--cache", type=Path, default=br.REPO / "release" / "cache")
    parser.add_argument("--repo", type=Path, default=br.REPO)
    args = parser.parse_args(argv)

    deployed = Path(args.deployed)
    if not deployed.is_dir():
        print(f"not a folder: {deployed}")
        return 1
    with tempfile.TemporaryDirectory(prefix="stbridge-verify-") as temp:
        try:
            reference = build_reference(args.repo, args.cache, Path(temp))
        except FileNotFoundError as error:
            print(error)
            return 2
        outcomes = {"release": compare_release(reference, deployed)}
        if args.plugin_dir:
            outcomes["plugin"] = compare_folder(
                reference / "addons" / br.PLUGIN_NAME, Path(args.plugin_dir), PLUGIN_SKIPPED_FILES
            )
        if args.extension_dir:
            outcomes["extension"] = compare_folder(reference / "addons" / br.CLIENT_NAME, Path(args.extension_dir))
        link_problem = check_core_link(Path(args.plugin_dir)) if args.plugin_dir else ""
    ok = True
    for label, outcome in outcomes.items():
        print(summary(label, outcome))
        ok = ok and is_clean(outcome)
    if link_problem:
        print(f"plugin: {link_problem}")
        ok = False
    print("OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

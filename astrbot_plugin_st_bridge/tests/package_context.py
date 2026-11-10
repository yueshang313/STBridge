"""Expose the workspace plugin under AstrBot's package-qualified import name."""

import sys
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = "data.plugins.astrbot_plugin_st_bridge"


def _ensure_package(name: str, search_path: Path | None = None):
    module = sys.modules.get(name)
    paths = [] if search_path is None else [str(search_path)]
    if module is None:
        spec = ModuleSpec(name, loader=None, is_package=True)
        spec.submodule_search_locations = paths
        module = module_from_spec(spec)
        sys.modules[name] = module
    else:
        module.__path__ = paths
        if module.__spec__ is not None:
            module.__spec__.submodule_search_locations = paths
    return module


def install_plugin_namespace() -> None:
    data_package = _ensure_package("data")
    plugins_package = _ensure_package("data.plugins", PLUGIN_ROOT.parent)
    plugin_package = _ensure_package(PLUGIN_PACKAGE, PLUGIN_ROOT)
    data_package.plugins = plugins_package
    plugins_package.astrbot_plugin_st_bridge = plugin_package

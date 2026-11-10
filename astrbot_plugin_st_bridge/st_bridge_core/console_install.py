"""The console's "安装与路径" routes (R4b) and the restart button. The work itself is in ``install_service``.

Every route that changes something is a POST; they go through the console's Host / Origin checks like all others.
Nothing here takes a path to delete from the request: the plugin and the extension live in fixed-name folders
inside the folders worked out from the saved paths.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from aiohttp import web

from .console_server import (
    _AFFINITY_KEY,
    _COVER_KEY,
    _INSTALL_KEY,
    _RESTART_KEY,
    _STORE_KEY,
    _TTS_KEY,
    _json,
    _json_body,
)
from .cover_config import cover_settings, validate_cover_settings
from .emotion_config import EmotionConfigError, validate_tts_settings
from .install_config import clean_path_text, install_settings, validate_install_settings
from .install_service import check_paths
from .network_config import network_settings, reserved_ports

# request field -> where the value lives
_FIELDS = ("st_dir", "astrbot_dir", "napcat_path", "gpt_sovits_dir", "irodori_dir", "irodori_server_dir", "rvc_dir", "pip_mirror")


def _current_values(config: dict[str, Any]) -> dict[str, str]:
    """All the paths as the page shows them, from the settings they are stored in."""
    install = install_settings(config)
    tts = config.get("tts") if isinstance(config.get("tts"), dict) else {}
    irodori = tts.get("irodori") if isinstance(tts.get("irodori"), dict) else {}
    sovits = tts.get("gpt_sovits") if isinstance(tts.get("gpt_sovits"), dict) else {}
    cover = config.get("cover") if isinstance(config.get("cover"), dict) else {}
    text = lambda value: value if isinstance(value, str) else ""  # noqa: E731
    return {
        "st_dir": install["st_dir"],
        "astrbot_dir": install["astrbot_dir"],
        "napcat_path": install["napcat_path"],
        "gpt_sovits_dir": text(sovits.get("launch_dir")),
        "irodori_dir": text(irodori.get("launch_dir")),
        "irodori_server_dir": text(irodori.get("server_dir")),
        "rvc_dir": text(cover.get("rvc_root")),
        "pip_mirror": install["pip_mirror"],
    }


def _check_keys(values: dict[str, str]) -> dict[str, str]:
    return {
        "st": values["st_dir"], "astrbot": values["astrbot_dir"], "napcat": values["napcat_path"],
        "gpt_sovits": values["gpt_sovits_dir"], "irodori": values["irodori_dir"],
        "irodori_server": values["irodori_server_dir"], "rvc": values["rvc_dir"],
    }


def _body_values(body: dict[str, Any], base: dict[str, str]) -> dict[str, str]:
    """The fields of a request body (missing ones keep their current value), cleaned."""
    unknown = set(body) - set(_FIELDS)
    if unknown:
        raise EmotionConfigError(400, "unknown field")
    values = dict(base)
    for field in _FIELDS:
        if field not in body:
            continue
        if field == "pip_mirror":
            values[field] = validate_install_settings({"pip_mirror": body[field]})["pip_mirror"]
        else:
            values[field] = clean_path_text(body[field], field)
    return values


def _service(request: web.Request):
    return request.app[_INSTALL_KEY]


async def _overview(request: web.Request, values: dict[str, str] | None = None) -> dict[str, Any]:
    store = request.app[_STORE_KEY]
    service = _service(request)
    values = values or _current_values(store.config)
    checks = await asyncio.to_thread(check_paths, _check_keys(values))
    plugin, extension, source = await asyncio.gather(
        asyncio.to_thread(service.plugin_status, checks["astrbot"]),
        asyncio.to_thread(service.extension_status, checks["st"]),
        asyncio.to_thread(service.default_import_source, checks["astrbot"]),
    )
    return {
        "settings": values,
        "checks": checks,
        "package": service.package_info(),
        "plugin": plugin,
        "extension": extension,
        "import_source": source,
        "napcat_can_start": checks["napcat"]["state"] == "valid",
        "data_dir": str(service.data_dir),
        "job": service.job_status(),
        "restart_available": request.app[_RESTART_KEY] is not None,
    }


async def _get_install(request: web.Request) -> web.Response:
    return _json(await _overview(request))


async def _check(request: web.Request) -> web.Response:
    """Check typed values without saving them."""
    store = request.app[_STORE_KEY]
    values = _body_values(await _json_body(request), _current_values(store.config))
    return _json({"checks": await asyncio.to_thread(check_paths, _check_keys(values))})


async def _save_paths(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    current = _current_values(store.config)
    values = _body_values(await _json_body(request), current)

    install = validate_install_settings({
        "st_dir": values["st_dir"], "astrbot_dir": values["astrbot_dir"],
        "napcat_path": values["napcat_path"], "pip_mirror": values["pip_mirror"],
    })
    tts_old = copy.deepcopy(store.config.get("tts", {}))
    tts_new = copy.deepcopy(tts_old)
    tts_new.setdefault("irodori", {}).update(launch_dir=values["irodori_dir"], server_dir=values["irodori_server_dir"])
    tts_new.setdefault("gpt_sovits", {}).update(launch_dir=values["gpt_sovits_dir"])
    tts_new = validate_tts_settings(tts_new, network=network_settings(store.config))
    cover_old = cover_settings(store.config)
    cover_new = validate_cover_settings(
        {**cover_old, "rvc_root": values["rvc_dir"]}, reserved=reserved_ports(store.config)
    )

    def update(config):
        config["install"] = install
        config["tts"] = tts_new
        stored = config.get("cover") if isinstance(config.get("cover"), dict) else {}
        config["cover"] = {**stored, "rvc_root": cover_new["rvc_root"]}

    await store.update(update)
    tts = request.app[_TTS_KEY]
    if tts is not None and tts_new != tts_old:
        await tts.apply_settings(tts_new)
    cover = request.app[_COVER_KEY]
    if cover is not None and cover_old["rvc_root"] != cover_new["rvc_root"]:
        await cover.apply_settings(cover_old, cover_new)
    return _json(await _overview(request))


def _action(body: dict[str, Any]) -> str:
    action = body.get("action")
    if action not in ("install", "uninstall"):
        raise EmotionConfigError(400, "action must be install or uninstall")
    return action


async def _plugin_action(request: web.Request) -> web.Response:
    service = _service(request)
    action = _action(await _json_body(request))
    checks = await asyncio.to_thread(check_paths, _check_keys(_current_values(request.app[_STORE_KEY].config)))
    run = service.install_plugin if action == "install" else service.uninstall_plugin
    result = await asyncio.to_thread(run, checks["astrbot"])
    return _json({**result, "overview": await _overview(request)})


async def _extension_action(request: web.Request) -> web.Response:
    service = _service(request)
    action = _action(await _json_body(request))
    checks = await asyncio.to_thread(check_paths, _check_keys(_current_values(request.app[_STORE_KEY].config)))
    run = service.install_extension if action == "install" else service.uninstall_extension
    result = await asyncio.to_thread(run, checks["st"])
    return _json({**result, "overview": await _overview(request)})


def _source_of(body: dict[str, Any]) -> str:
    return clean_path_text(body.get("source"), "source")


async def _import_preview(request: web.Request) -> web.Response:
    body = await _json_body(request)
    return _json(await asyncio.to_thread(_service(request).import_preview, _source_of(body)))


async def _import_run(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body.get("confirm") is not True:
        raise EmotionConfigError(400, "confirm required")
    result = await asyncio.to_thread(_service(request).import_data, _source_of(body))
    hook = request.app[_RESTART_KEY]
    outcome: dict[str, Any] = {}
    if hook is not None:
        # The core still holds the old settings in memory: from now on nothing may write over the imported files, and the
        # core restarts by itself (the same path as the restart button) to read them.
        request.app[_STORE_KEY].freeze()
        affinity = request.app[_AFFINITY_KEY]
        if affinity is not None:
            affinity.freeze()
        outcome = hook() or {}
    return _json({**result, "restart_available": hook is not None, "restarting": hook is not None, "auto": bool(outcome.get("auto"))})


async def _get_deps(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    service = _service(request)
    mirror = install_settings(store.config)["pip_mirror"]
    rows = await asyncio.to_thread(service.dependency_rows, store.config, mirror)
    return _json({"rows": rows, "job": service.job_status(), "mirror": mirror})


async def _install_dep(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    mirror = install_settings(store.config)["pip_mirror"]
    job = _service(request).start_dependency(request.match_info["name"], store.config, mirror)
    return _json({"job": job}, 202)


async def _start_napcat(request: web.Request) -> web.Response:
    checks = await asyncio.to_thread(check_paths, _check_keys(_current_values(request.app[_STORE_KEY].config)))
    return _json(await asyncio.to_thread(_service(request).start_napcat, checks["napcat"]))


async def _restart(request: web.Request) -> web.Response:
    hook = request.app[_RESTART_KEY]
    if hook is None:
        raise EmotionConfigError(503, "restart is not available")
    outcome = hook() or {}
    return _json({"ok": True, "auto": bool(outcome.get("auto"))}, 202)


def register_install_routes(router: web.UrlDispatcher) -> None:
    router.add_get("/api/install", _get_install, allow_head=False)
    router.add_post("/api/install/check", _check)
    router.add_post("/api/install/paths", _save_paths)
    router.add_post("/api/install/plugin", _plugin_action)
    router.add_post("/api/install/st-extension", _extension_action)
    router.add_post("/api/install/import/preview", _import_preview)
    router.add_post("/api/install/import", _import_run)
    router.add_get("/api/install/deps", _get_deps, allow_head=False)
    router.add_post("/api/install/deps/{name}/install", _install_dep)
    router.add_post("/api/install/napcat/start", _start_napcat)
    router.add_post("/api/system/restart", _restart)

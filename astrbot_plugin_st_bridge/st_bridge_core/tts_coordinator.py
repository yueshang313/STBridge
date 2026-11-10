"""Pick the TTS engine, own one service manager per engine and synthesize speech.

Settings come from the console config's ``tts`` section. Changing them through
``apply_settings`` takes effect immediately: the service this plugin started for
the old engine (or with old launch parameters) is stopped, an externally started
service is never touched.
"""

from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
from typing import Any, Callable

from .emotion_config import TTS_ENGINES, validate_tts_settings
from .tts_clients import (
    ModelSelectionError,
    build_api_base_url,
    build_api_py_model_payload,
    build_api_py_payload,
    build_irodori_payload,
    build_profile_tts_settings,
    post_api_py_audio,
    post_api_py_model,
    post_irodori_audio,
    profile_model_payload,
    usable_gpt_profile,
)
from .tts_service import ServiceManager

ENGINE_IRODORI = "irodori"
ENGINE_GPT_SOVITS = "gpt_sovits"
ENGINE_LABELS = {ENGINE_IRODORI: "Irodori", ENGINE_GPT_SOVITS: "GPT-SoVITS"}


def _irodori_signature(section: dict[str, Any]) -> tuple:
    return tuple(section[key] for key in ("host", "port", "launch_dir", "server_dir", "hf_checkpoint"))


def _gpt_sovits_signature(section: dict[str, Any]) -> tuple:
    return tuple(section[key] for key in ("host", "port", "launch_dir"))


_SIGNATURES: dict[str, Callable[[dict[str, Any]], tuple]] = {
    ENGINE_IRODORI: _irodori_signature,
    ENGINE_GPT_SOVITS: _gpt_sovits_signature,
}


class TtsCoordinator:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        data_dir: Path,
        logger: Any,
        manager_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.settings = validate_tts_settings(settings)
        self._data_dir = Path(data_dir)
        self._logger = logger
        # Test hook: fake launcher/probe/killer/clock for the service managers.
        self._manager_kwargs = dict(manager_kwargs or {})
        self._managers: dict[str, ServiceManager] = {}
        # Unknown at startup, so the first synthesis with a target always selects it.
        self._current_model: dict[str, str] | None = None
        self._model_selection_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()

    # ---- settings ----------------------------------------------------------

    @property
    def engine(self) -> str:
        return self.settings["engine"]

    @property
    def engine_label(self) -> str:
        return ENGINE_LABELS[self.engine]

    @property
    def common(self) -> dict[str, Any]:
        return self.settings["common"]

    @property
    def temp_dir(self) -> Path:
        return self._data_dir / "temp_audio"

    @property
    def tts_timeout(self) -> int:
        return int(self.common["tts_timeout_minutes"]) * 60

    def public_settings(self) -> dict[str, Any]:
        return copy.deepcopy(self.settings)

    # ---- service managers --------------------------------------------------

    def _build_manager(self, engine: str) -> ServiceManager:
        common = self.common
        shared = dict(
            start_timeout=common["start_timeout"],
            stop_on_exit=common["stop_on_exit"],
            data_dir=self._data_dir,
            logger=self._logger,
            service_name=ENGINE_LABELS[engine],
            **self._manager_kwargs,
        )
        if engine == ENGINE_IRODORI:
            section = self.settings["irodori"]
            return ServiceManager(
                host=section["host"],
                port=section["port"],
                launch_dir=section["launch_dir"],
                python_path=os.path.join(section["launch_dir"], ".venv", "Scripts", "python.exe"),
                args=[
                    "-m", "irodori_openai_tts",
                    "--host", section["host"],
                    "--port", str(section["port"]),
                ],
                extra_env=self._irodori_env(section),
                # Without a server folder the launch check must fail (a relative path would be looked up in the cwd).
                required_files=[os.path.join(
                    section["server_dir"], "src", "irodori_openai_tts", "__main__.py"
                ) if section["server_dir"] else "<未设置 Irodori 服务端目录>"],
                **shared,
            )
        section = self.settings["gpt_sovits"]
        return ServiceManager(
            host=section["host"],
            port=section["port"],
            launch_dir=section["launch_dir"],
            on_restart=self._forget_current_model,
            **shared,
        )

    @staticmethod
    def _irodori_env(section: dict[str, Any]) -> dict[str, str]:
        # The Irodori clone is not installed into its venv: it is imported from the
        # working directory, and the server package comes from PYTHONPATH.
        return {
            "PYTHONPATH": os.path.join(section["server_dir"], "src"),
            "HF_HUB_CACHE": os.path.join(section["launch_dir"], "model_cache", "hub"),
            "IRODORI_HF_CHECKPOINT": section["hf_checkpoint"],
            "IRODORI_PRELOAD": "true",
            "IRODORI_COMPILE_MODEL": "false",
            "IRODORI_VOICES_DIR": os.path.join(section["server_dir"], "voices"),
        }

    def manager(self, engine: str | None = None) -> ServiceManager:
        engine = engine or self.engine
        if engine not in TTS_ENGINES:
            raise ValueError("unknown tts engine")
        if engine not in self._managers:
            self._managers[engine] = self._build_manager(engine)
        return self._managers[engine]

    def _forget_current_model(self) -> None:
        # A (re)started or stopped API no longer holds the model we selected.
        self._current_model = None

    async def apply_settings(self, new_settings: dict[str, Any]) -> dict[str, Any]:
        """Switch to ``new_settings``; return which engines had their own service stopped."""
        new = validate_tts_settings(new_settings)
        async with self._apply_lock:
            old = self.settings
            stopped: list[str] = []
            for engine in TTS_ENGINES:
                existing = self._managers.get(engine)
                if existing is None:
                    continue
                signature = _SIGNATURES[engine]
                launch_changed = signature(old[engine]) != signature(new[engine])
                left_active = engine == old["engine"] and new["engine"] != engine
                if not (launch_changed or left_active):
                    continue
                result = await existing.stop()
                if result["ok"]:
                    stopped.append(engine)
                if launch_changed:
                    # Rebuilt lazily with the new parameters at the next use.
                    del self._managers[engine]
            self.settings = new
            for existing in self._managers.values():
                existing.start_timeout = new["common"]["start_timeout"]
                existing.stop_on_exit = new["common"]["stop_on_exit"]
            if old["engine"] != new["engine"]:
                self._logger.info(
                    f"[STBridge] TTS engine switched to {ENGINE_LABELS[new['engine']]}"
                    + (f", stopped own service: {', '.join(stopped)}" if stopped else "")
                )
            elif stopped:
                self._logger.info(
                    f"[STBridge] TTS launch settings changed, stopped own service: {', '.join(stopped)}"
                )
            return {"stopped": stopped}

    async def status(self) -> dict[str, Any]:
        data = await self.manager().status()
        data["engine"] = self.engine
        return data

    async def start_service(self) -> None:
        await self.manager().ensure_started()

    async def stop_service(self) -> dict[str, Any]:
        return await self.manager().stop()

    async def ensure_service_ready(self) -> None:
        """Start the service on demand when its port is closed (needs auto_start)."""
        service = self.manager()
        if await service.is_up():
            return
        self._forget_current_model()
        if not self.common["auto_start"]:
            return
        # The service forgets the model itself when it launches the process; doing
        # it again here would make a second waiting caller reload the model.
        await service.ensure_started()

    async def terminate(self) -> None:
        for existing in list(self._managers.values()):
            await existing.terminate()

    # ---- synthesis ---------------------------------------------------------

    async def synthesize(self, text: str, profile: dict | None) -> bytes:
        if self.engine == ENGINE_IRODORI:
            return await self._synthesize_irodori(text, profile)
        return await self._synthesize_gpt_sovits(text, profile)

    def _gpt_base_url(self) -> str:
        section = self.settings["gpt_sovits"]
        return build_api_base_url(section["host"], section["port"])

    def _irodori_base_url(self) -> str:
        section = self.settings["irodori"]
        return build_api_base_url(section["host"], section["port"])

    async def _post(self, data: dict) -> bytes:
        import httpx
        return await post_api_py_audio(httpx.AsyncClient, self._gpt_base_url(), data, self.tts_timeout)

    async def _set_model(self, payload: dict[str, str]) -> None:
        import httpx
        await post_api_py_model(httpx.AsyncClient, self._gpt_base_url(), payload, self.tts_timeout)

    async def _post_irodori(self, payload: dict) -> bytes:
        import httpx
        return await post_irodori_audio(
            httpx.AsyncClient, self._irodori_base_url(), payload, self.tts_timeout
        )

    def _target_model(self, profile: dict | None) -> dict[str, str] | None:
        target = profile_model_payload(profile)
        if target is not None:
            return target
        section = self.settings["gpt_sovits"]
        return build_api_py_model_payload(section["gpt_model_path"], section["sovits_model_path"])

    async def _ensure_model_selected(self, profile: dict | None = None) -> bool:
        """Load the target model pair if it differs from the current one; return True on a switch."""
        target = self._target_model(profile)
        if target is None:
            return False

        async with self._model_selection_lock:
            if self._current_model == target:
                return False
            try:
                await self._set_model(target)
            except ModelSelectionError:
                raise
            except Exception as error:
                raise ModelSelectionError("GPT-SoVITS model selection failed") from error
            self._current_model = dict(target)
            return True

    async def _synthesize_gpt_sovits(self, text: str, event_profile: dict | None) -> bytes:
        profile = usable_gpt_profile(event_profile)
        if profile is not None:
            settings = build_profile_tts_settings(profile)
        else:
            section = self.settings["gpt_sovits"]
            settings = {
                "ref_audio_path": section["ref_audio_path"],
                "prompt_text": section["prompt_text"],
                "prompt_text_lang": section["prompt_text_lang"],
                "text_lang": section["text_lang"],
                "top_k": section["top_k"],
                "top_p": section["top_p"],
                "temperature": section["temperature"],
                "speed_facter": section["speed_factor"],
                "split_sentence": section["split_sentence"],
            }
        payload = build_api_py_payload(text, settings)
        await self.ensure_service_ready()
        switched = await self._ensure_model_selected(profile)
        self._logger.info(
            "[STBridge] TTS settings "
            f"source={'profile' if profile is not None else 'config'} "
            f"model_switched={switched}"
        )
        return await self._post(payload)

    async def _synthesize_irodori(self, text: str, profile: dict | None) -> bytes:
        section = self.settings["irodori"]
        payload = build_irodori_payload(
            text,
            profile,
            {
                "lora_adapter": section["lora_adapter"],
                "num_steps": section["num_steps"],
                "cfg_scale_text": section["cfg_scale_text"],
                "cfg_scale_caption": section["cfg_scale_caption"],
                "default_caption": section["default_caption"],
            },
        )
        await self.ensure_service_ready()
        options = payload["irodori"]
        self._logger.info(
            "[STBridge] TTS settings engine=irodori "
            f"source={(profile or {}).get('source') or 'none'} "
            f"emoji={payload['input'] != text} caption={'caption' in options}"
        )
        return await self._post_irodori(payload)

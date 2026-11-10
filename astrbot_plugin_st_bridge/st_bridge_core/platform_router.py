"""The PlatformAPI the core runs on once NapCat is built in: AstrBot's connector or NapCat, by conversation.

A NapCat conversation id / ref ("napcat:...") goes to NapCat, anything else to the AstrBot connector. The wake
prefixes come from the platform that is active now. Core code never knows which platform it talks to.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .chat_platform import NAPCAT, conversation_platform
from .platform_api import TEST_CONVERSATION_PREFIX, PlatformAPI, Segment


def _is_test(key: object) -> bool:
    """The console test chat (A1): a belt-and-braces guard, the core already sends nothing for it."""
    return isinstance(key, str) and key.startswith(TEST_CONVERSATION_PREFIX)


class PlatformRouter(PlatformAPI):
    def __init__(self, connector: PlatformAPI, onebot: PlatformAPI, active: Callable[[], str]) -> None:
        self._connector = connector
        self._onebot = onebot
        self._active = active

    def _for(self, key: str) -> PlatformAPI:
        return self._onebot if conversation_platform(key) == NAPCAT else self._connector

    async def send(self, ref: str, segments: list[Segment]) -> None:
        if _is_test(ref):
            return
        await self._for(ref).send(ref, segments)

    async def send_to(self, conversation_id: str, segments: list[Segment]) -> bool:
        if _is_test(conversation_id):
            return False
        return await self._for(conversation_id).send_to(conversation_id, segments)

    def wake_prefixes(self) -> list[str] | None:
        return (self._onebot if self._active() == NAPCAT else self._connector).wake_prefixes()

    def extra_temp_dir(self) -> Path | None:
        # AstrBot's temp directory, as far as the connector told; NapCat has none.
        return self._connector.extra_temp_dir()

    async def can_preupload_image(self, ref: str) -> bool:
        if _is_test(ref):
            return False
        return await self._for(ref).can_preupload_image(ref)

    async def preupload_image(self, ref: str, path: Path) -> str:
        if _is_test(ref):
            raise RuntimeError("test chat has no platform")
        return await self._for(ref).preupload_image(ref, path)

    async def send_uploaded_image(self, ref: str, handle: str) -> None:
        if _is_test(ref):
            return
        await self._for(ref).send_uploaded_image(ref, handle)

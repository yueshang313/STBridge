import asyncio
import importlib
import sys
import tempfile
import time
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol import BridgeRecoveryGate
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_service import HttpServiceState
from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.request_state import (
    GenerationStatus,
    RequestBusyError,
    RequestStateManager,
    RequestStatus,
)


_ASTRBOT_BOUND_MODULES = (
    "astrbot_api",
    "astrbot_platform",
)


def submodule(module, name):
    """The plugin submodule `name` that `module` (main) was loaded together with."""
    return sys.modules[f"{module.__package__}.st_bridge_core.{name}"]


def _create_embedded_runtime(plugin):
    from embedded import EmbeddedRuntime

    runtime = EmbeddedRuntime(plugin)
    plugin._core = runtime.core
    return runtime


def _load_plugin_module():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.__path__ = []
    star_module = types.ModuleType("astrbot.api.star")
    event_module = types.ModuleType("astrbot.api.event")
    message_components_module = types.ModuleType("astrbot.api.message_components")
    core_module = types.ModuleType("astrbot.core")
    core_module.__path__ = []
    platform_module = types.ModuleType("astrbot.core.platform")
    platform_module.__path__ = []
    message_type_module = types.ModuleType("astrbot.core.platform.message_type")

    class FakeMessageType(Enum):
        FRIEND_MESSAGE = "FriendMessage"
        GROUP_MESSAGE = "GroupMessage"

    class FakeEventMessageType(Enum):
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class FakeGroupMessage:
        def __init__(self, author=None):
            self.author = author or SimpleNamespace(bot=False)

    class FakeChannelMessage:
        pass

    class FakePlain:
        def __init__(self, text=""):
            self.text = text

    class FakeAt:
        def __init__(self, qq=""):
            self.qq = qq

    class FakeImage:
        path = None

        @classmethod
        def fromFileSystem(cls, path, **_):
            image = cls()
            image.path = path
            return image

    class FakeMessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class FakeRecord:
        def __init__(self, *, file):
            self.file = file
            self.text = None

    botpy_module = types.ModuleType("botpy")
    botpy_module.__path__ = []
    botpy_message_module = types.ModuleType("botpy.message")
    botpy_message_module.GroupMessage = FakeGroupMessage
    botpy_message_module.Message = FakeChannelMessage
    botpy_module.message = botpy_message_module

    class FakeStar:
        def __init__(self, context, config=None):
            self.context = context

    class FakeLogger:
        def __init__(self):
            self.warnings = []
            self.errors = []
            self.infos = []

        def info(self, message):
            self.infos.append(message)

        def warning(self, message):
            self.warnings.append(message)

        def error(self, message):
            self.errors.append(message)

    class FakeFilter:
        EventMessageType = FakeEventMessageType

        @staticmethod
        def command(name):
            def decorate(handler):
                handler.command_name = name
                return handler

            return decorate

        @staticmethod
        def event_message_type(message_type, **kwargs):
            def decorate(handler):
                handler.event_message_type = message_type
                handler.filter_options = kwargs
                return handler

            return decorate

    star_module.Star = FakeStar
    star_module.Context = object
    api_module.logger = FakeLogger()
    event_module.AstrMessageEvent = object
    event_module.filter = FakeFilter()
    event_module.MessageChain = FakeMessageChain
    message_components_module.Plain = FakePlain
    message_components_module.At = FakeAt
    message_components_module.Record = FakeRecord
    message_components_module.Image = FakeImage
    message_type_module.MessageType = FakeMessageType
    api_module.star = star_module
    api_module.message_components = message_components_module
    astrbot_module.api = api_module

    modules = {
        "astrbot": astrbot_module,
        "astrbot.api": api_module,
        "astrbot.api.star": star_module,
        "astrbot.api.event": event_module,
        "astrbot.api.message_components": message_components_module,
        "botpy": botpy_module,
        "botpy.message": botpy_message_module,
        "astrbot.core": core_module,
        "astrbot.core.platform": platform_module,
        "astrbot.core.platform.message_type": message_type_module,
    }
    names = tuple(modules)
    previous = {name: sys.modules.get(name) for name in names}
    sys.modules.update(modules)
    try:
        module_name = "data.plugins.astrbot_plugin_st_bridge.main"
        # Every module that imports from the fake astrbot (the shim and the adapter modules that use it)
        # has to be re-imported with main, or it keeps the first load's fake classes. The core
        # (st_bridge_core) never imports astrbot and stays loaded; main points its logger at the fake one.
        for name in ("main",) + _ASTRBOT_BOUND_MODULES:
            sys.modules.pop(f"data.plugins.astrbot_plugin_st_bridge.{name}", None)
        module = importlib.import_module(module_name)
        module.TestMessageType = FakeMessageType
        module.TestPlain = FakePlain
        module.TestAt = FakeAt
        module.TestImage = FakeImage
        module.TestMessageChain = FakeMessageChain
        module.TestRecord = FakeRecord
        module.TestGroupMessage = FakeGroupMessage
        module.TestChannelMessage = FakeChannelMessage
        module.TestBotpyModules = {
            "botpy": botpy_module,
            "botpy.message": botpy_message_module,
        }
        module.test_logger = api_module.logger
        # The plugin tests run the core in this process, connected through an in-memory transport.
        module.STBridgePlugin._create_runtime = _create_embedded_runtime
        return module
    finally:
        for name, previous_module in previous.items():
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module


class FakeMessageResult:
    def __init__(self, text):
        self.text = text


class FakeChainResult:
    """chain_result(...): the chain a voiced reply is sent as (text and voice)."""

    def __init__(self, chain):
        self.chain = list(chain)

    @property
    def text(self):
        """The plain text of the chain (what AstrBot's get_plain_text() gives)."""
        return "".join(item.text for item in self.chain if isinstance(getattr(item, "text", None), str))


_VOICE_DIRECTORY = tempfile.TemporaryDirectory(prefix="st-bridge-voice-")


class RecordingTts:
    """A TTS coordinator stand-in: records what it is asked to synthesize and returns a tiny audio blob.

    The old decorating-stage handler marked the event with the Japanese text, its eligibility and the
    emotion profile (event extras); the core now voices the reply itself, so tests read the same facts
    from the synthesis calls: ``eligible`` (something was voiced), ``last_text`` and ``last_profile``.
    """

    def __init__(self, **common):
        # Irodori "native" output: the audio is sent as it is, so the stand-in bytes need no real WAV.
        self.settings = {"engine": "irodori", "irodori": {"output_mode": "native"}}
        self.common = {"prob": 100, "cooldown": 0, "text_limit": 0, "send_text_with_audio": True, **common}
        self.temp_dir = Path(_VOICE_DIRECTORY.name) / "temp_audio"
        self.calls = []

    async def synthesize(self, text, profile):
        self.calls.append((text, profile))
        return b"audio"

    @property
    def eligible(self):
        return True if self.calls else None

    @property
    def last_text(self):
        return self.calls[-1][0] if self.calls else None

    @property
    def last_profile(self):
        return self.calls[-1][1] if self.calls else None


class FakeRecoveryEvent:
    def __init__(self, message_type, platform_id, sender_id):
        self.message_type = message_type
        self.platform_id = platform_id
        self.sender_id = sender_id
        self.call_llm = False
        self.stopped = False

    def get_message_type(self):
        return self.message_type

    def get_platform_id(self):
        return self.platform_id

    def get_sender_id(self):
        return self.sender_id

    def should_call_llm(self, call_llm):
        self.call_llm = call_llm

    def plain_result(self, text):
        return FakeMessageResult(text)

    def chain_result(self, chain):
        return FakeChainResult(chain)

    def stop_event(self):
        self.stopped = True


class FakeBridgeEvent(FakeRecoveryEvent):
    def __init__(
        self,
        message_type,
        platform_id,
        sender_id,
        text,
        components,
        extras=None,
        platform_name="qq-platform",
        group_id="",
        raw_message=None,
        self_id="bot-id",
        is_at_or_wake_command=False,
        sender_name="",
    ):
        super().__init__(message_type, platform_id, sender_id)
        self.text = text
        self.sender_name = sender_name
        self.components = components
        self.extras = extras or {}
        self.platform_name = platform_name
        self.group_id = group_id
        self.message_obj = SimpleNamespace(raw_message=raw_message)
        self.self_id = self_id
        self.is_at_or_wake_command = is_at_or_wake_command

    def get_message_str(self):
        return self.text

    def get_sender_name(self):
        return self.sender_name

    def get_messages(self):
        return self.components

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.self_id


async def _collect(handler, event):
    return [item async for item in handler(event)]


def _ready_plugin(module, *, timeout=120.0, config=None, recovered=True, voice=False, clock=None):
    settings = config or {
        "bridge_token": "a" * 43,
        "operator_platform_id": "qq-platform",
        "operator_sender_id": "qq-user",
    }
    plugin = module.STBridgePlugin(context=None, config=settings)
    manager_options = {} if clock is None else {"clock": clock}
    plugin._core._request_state_manager = RequestStateManager(timeout_seconds=timeout, **manager_options)
    plugin._core._recovery_gate = BridgeRecoveryGate()
    if recovered:
        plugin._core._recovery_gate.confirm_recovery()
    plugin._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
    if voice:
        plugin._core._tts = RecordingTts()
    return plugin


_DEFAULT_COMPONENTS = object()


def _bridge_event(
    module,
    text,
    *,
    message_type=None,
    platform_id="qq-platform",
    sender_id="qq-user",
    components=_DEFAULT_COMPONENTS,
    extras=None,
    platform_name="qq-platform",
    group_id="",
    raw_message=None,
    self_id="bot-id",
    is_at_or_wake_command=False,
    sender_name="",
):
    if components is _DEFAULT_COMPONENTS:
        components = [module.TestPlain(text)]
    return FakeBridgeEvent(
        message_type or module.TestMessageType.FRIEND_MESSAGE,
        platform_id,
        sender_id,
        text,
        components,
        extras,
        platform_name,
        group_id,
        raw_message,
        self_id,
        is_at_or_wake_command,
        sender_name,
    )


def _group_bridge_event(
    module,
    text,
    *,
    sender_id="member-1",
    sender_name="",
    components=None,
):
    if components is None:
        components = [module.TestPlain(text)]
    return _bridge_event(
        module,
        text,
        message_type=module.TestMessageType.GROUP_MESSAGE,
        sender_id=sender_id,
        sender_name=sender_name,
        components=components,
        platform_name="qq_official",
        group_id="group-id",
        raw_message=module.TestGroupMessage(),
        is_at_or_wake_command=True,
    )


async def _wait_for_request(state_manager, timeout=60.0):
    # Waits by the clock, not by a number of event-loop turns: the handler may need real time (file I/O on a slow share).
    deadline = time.monotonic() + timeout
    turns = 0
    while True:
        request = state_manager.peek_pending_request()
        if request is not None:
            return request
        if time.monotonic() >= deadline:
            raise AssertionError("bridge handler did not create a request")
        turns += 1
        await asyncio.sleep(0 if turns < 5000 else 0.005)


async def _complete_bridge_reply(
    plugin,
    event,
    reply,
    *,
    handler=None,
    module=None,
):
    async def complete():
        processing = asyncio.create_task(
            _collect(handler or plugin.stbridge_private_message, event),
        )
        request = await _wait_for_request(plugin._core._request_state_manager)
        plugin._core._request_state_manager.claim_request(
            request.request_id,
            "client-attempt-1",
        )
        plugin._core._request_state_manager.mark_generation_started(request.request_id)
        plugin._core._request_state_manager.confirm_generation_finished(request.request_id)
        plugin._core._request_state_manager.complete_request(request.request_id, reply)
        return await processing

    if module is None:
        return await complete()
    with patch.dict(sys.modules, module.TestBotpyModules):
        return await complete()


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_request_content(
        self,
        plugin,
        event,
        expected_content,
        *,
        handler=None,
    ):
        processing = asyncio.create_task(
            _collect(handler or plugin.stbridge_private_message, event),
        )
        request = await _wait_for_request(plugin._core._request_state_manager)
        self.assertEqual(request.status, RequestStatus.PENDING)
        self.assertEqual(request.content, expected_content)
        plugin._core._request_state_manager.fail_request(
            request.request_id,
            "test_cleanup",
        )
        await processing
        self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

    async def _assert_group_request_content(
        self,
        module,
        plugin,
        event,
        expected_content,
    ):
        with patch.dict(sys.modules, module.TestBotpyModules):
            await self._assert_request_content(
                plugin,
                event,
                expected_content,
                handler=plugin.stbridge_group_message,
            )

    async def test_ping_command_keeps_fixed_response(self):
        module = _load_plugin_module()
        plugin = module.STBridgePlugin(context=None)

        class Event:
            def plain_result(self, text):
                return text

        async def collect():
            return [item async for item in plugin.stbridge_ping(Event())]

        self.assertEqual(plugin.stbridge_ping.command_name, "stbridge_ping")
        self.assertEqual(await collect(), ["ST Bridge 插件已加载"])

    async def test_legacy_token_settings_are_ignored(self):
        module = _load_plugin_module()
        for legacy in (
            {},
            {"bridge_token": "a" * 43, "console_token": "b" * 43},
            {"bridge_token": "short", "console_token": 5},
        ):
            with self.subTest(legacy=sorted(legacy)):
                plugin = module.STBridgePlugin(
                    context=None,
                    config={
                        "operator_platform_id": "qq-platform",
                        "operator_sender_id": "qq-user",
                        **legacy,
                    },
                )
                for name in (
                    "_token_validator",
                    "_console_token_validator",
                    "_bridge_auth_configured",
                ):
                    self.assertFalse(hasattr(plugin, name))
                plugin._core._recovery_gate = BridgeRecoveryGate()
                plugin._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
                event = FakeRecoveryEvent(
                    module.TestMessageType.FRIEND_MESSAGE,
                    "qq-platform",
                    "qq-user",
                )
                result, = await _collect(plugin.stbridge_recover, event)
                self.assertIn("人工恢复确认已记录", result.text)
                self.assertTrue(plugin._core._recovery_gate.allows_business_requests())

    async def test_only_configured_private_platform_sender_can_recover(self):
        module = _load_plugin_module()
        config = {
            "bridge_token": "a" * 43,
            "operator_platform_id": "qq-platform",
            "operator_sender_id": "qq-user",
        }
        cases = (
            (module.TestMessageType.FRIEND_MESSAGE, "other-platform", "qq-user"),
            (module.TestMessageType.FRIEND_MESSAGE, "qq-platform", "other-user"),
            (module.TestMessageType.GROUP_MESSAGE, "qq-platform", "qq-user"),
        )
        for message_type, platform_id, sender_id in cases:
            plugin = module.STBridgePlugin(context=None, config=config)
            plugin._core._recovery_gate = BridgeRecoveryGate()
            plugin._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
            event = FakeRecoveryEvent(message_type, platform_id, sender_id)

            result, = await _collect(plugin.stbridge_recover, event)
            self.assertTrue(event.call_llm)
            self.assertEqual(result.text, "恢复命令未执行。")
            self.assertFalse(plugin._core._recovery_gate.allows_business_requests())

        authorized = module.STBridgePlugin(context=None, config=config)
        authorized._core._recovery_gate = BridgeRecoveryGate()
        authorized._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
        event = FakeRecoveryEvent(
            module.TestMessageType.FRIEND_MESSAGE,
            "qq-platform",
            "qq-user",
        )
        result, = await _collect(authorized.stbridge_recover, event)
        self.assertTrue(event.call_llm)
        self.assertIn("人工恢复确认已记录", result.text)
        self.assertTrue(authorized._core._recovery_gate.allows_business_requests())

    async def test_recovery_command_is_idempotent_and_does_not_create_work(self):
        module = _load_plugin_module()
        plugin = module.STBridgePlugin(
            context=None,
            config={
                "bridge_token": "a" * 43,
                "operator_platform_id": "qq-platform",
                "operator_sender_id": "qq-user",
            },
        )
        plugin._core._recovery_gate = BridgeRecoveryGate()
        plugin._core._http_service = SimpleNamespace(state=HttpServiceState.RUNNING)
        event = FakeRecoveryEvent(
            module.TestMessageType.FRIEND_MESSAGE,
            "qq-platform",
            "qq-user",
        )

        with patch.object(
            submodule(module, "mixin_lifecycle"),
            "RequestStateManager",
            side_effect=AssertionError("recovery must not create request state"),
        ):
            first, = await _collect(plugin.stbridge_recover, event)
            second, = await _collect(plugin.stbridge_recover, event)

        self.assertTrue(event.call_llm)
        self.assertIn("人工恢复确认已记录", first.text)
        self.assertIn("人工恢复确认已记录", second.text)
        self.assertTrue(plugin._core._recovery_gate.allows_business_requests())

    async def test_plugin_lifecycle_uses_fixed_loopback_and_new_gate_per_instance(self):
        module = _load_plugin_module()
        created = []

        class FakeService:
            def __init__(self, app, config, gate):
                self.app = app
                self.config = config
                self.gate = gate
                self.state = HttpServiceState.NEW
                self.start_calls = 0
                created.append(self)

            async def start(self):
                self.start_calls += 1
                self.state = HttpServiceState.RUNNING

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = HttpServiceState.STOPPED

        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FakeService):
            first = module.STBridgePlugin(context=None)
            await first.initialize()
            first_gate = first._core._recovery_gate
            self.assertEqual(first_gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
            self.assertIs(first_gate.allows_business_requests(), False)
            self.assertEqual(first._core._http_service.config.listen_host, "127.0.0.1")
            self.assertEqual(first._core._http_service.config.listen_port, 11452)
            protocol_module = importlib.import_module(
                "data.plugins.astrbot_plugin_st_bridge.st_bridge_core.http_protocol",
            )
            self.assertIs(
                first._core._http_service.app[protocol_module._MANAGER_KEY],
                first._core._request_state_manager,
            )

            await first.initialize()
            self.assertEqual(len(created), 1)
            self.assertEqual(first._core._http_service.start_calls, 2)

            await first.terminate()
            await first.terminate()
            self.assertEqual(first_gate.state, BridgeRecoveryGate.STOPPED)
            self.assertIsNone(first._core._http_service)
            self.assertIsNone(first._core._request_state_manager)

            second = module.STBridgePlugin(context=None)
            await second.initialize()
            self.assertEqual(second._core._recovery_gate.state, BridgeRecoveryGate.RECOVERY_REQUIRED)
            self.assertIs(second._core._recovery_gate.allows_business_requests(), False)
            await second.terminate()

    async def test_initialize_failure_cleans_service_and_gate(self):
        module = _load_plugin_module()

        class FailingService:
            def __init__(self, app, config, gate):
                self.state = HttpServiceState.NEW
                self.gate = gate

            async def start(self):
                self.state = HttpServiceState.FAILED
                raise OSError("simulated startup failure")

            async def stop(self):
                self.gate.begin_shutdown()
                self.gate.mark_stopped()
                self.state = HttpServiceState.STOPPED

        plugin = module.STBridgePlugin(context=None)
        with patch.object(submodule(module, "mixin_lifecycle"), "LocalHttpService", FailingService):
            with self.assertRaisesRegex(OSError, "simulated startup failure"):
                await plugin.initialize()

        self.assertIsNone(plugin._core._http_service)
        self.assertIsNone(plugin._core._recovery_gate)

    async def test_private_message_handler_only_registers_for_private_events(self):
        module = _load_plugin_module()
        handler = module.STBridgePlugin.stbridge_private_message
        self.assertEqual(
            handler.event_message_type,
            module.filter.EventMessageType.PRIVATE_MESSAGE,
        )
        self.assertEqual(handler.filter_options["priority"], -1)

    async def test_group_message_handler_only_registers_for_group_events(self):
        module = _load_plugin_module()
        handler = module.STBridgePlugin.stbridge_group_message
        self.assertEqual(
            handler.event_message_type,
            module.filter.EventMessageType.GROUP_MESSAGE,
        )
        self.assertEqual(handler.filter_options["priority"], -1)

    async def test_qq_official_group_mention_uses_shared_bilingual_bridge_path(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module, voice=True)
        event = _bridge_event(
            module,
            "hello from group",
            message_type=module.TestMessageType.GROUP_MESSAGE,
            platform_name="qq_official",
            group_id="group-id",
            components=[module.TestAt("bot-id"), module.TestPlain("hello from group")],
            raw_message=module.TestGroupMessage(),
            is_at_or_wake_command=True,
        )

        results = await _complete_bridge_reply(
            plugin,
            event,
            '☆‹「グループの返事」›\n"群聊回复"',
            handler=plugin.stbridge_group_message,
            module=module,
        )

        self.assertEqual([result.text for result in results], ["群聊回复"])
        self.assertTrue(event.call_llm)
        self.assertFalse(event.stopped)
        self.assertEqual(
            plugin._core._tts.last_text,
            "「グループの返事」",
        )
        self.assertIs(
            plugin._core._tts.eligible,
            True,
        )

    async def test_qq_official_group_wake_prefix_uses_astrbot_processed_text(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _bridge_event(
            module,
            "question without wake prefix",
            message_type=module.TestMessageType.GROUP_MESSAGE,
            platform_name="qq_official",
            components=[module.TestPlain("!ask question without wake prefix")],
            raw_message=module.TestGroupMessage(),
            is_at_or_wake_command=True,
        )

        with patch.dict(sys.modules, module.TestBotpyModules):
            await self._assert_request_content(
                plugin,
                event,
                "【群友】用户A：question without wake prefix",
                handler=plugin.stbridge_group_message,
            )

        self.assertTrue(event.call_llm)
        self.assertFalse(event.stopped)

    async def test_group_speaker_name_is_stable_for_repeated_sender_id(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        state_manager = plugin._core._request_state_manager

        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "first message",
                sender_id="member-a",
                sender_name="小明",
            ),
            "【群友】小明（1）：first message",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "second message",
                sender_id="member-a",
                sender_name="小明",
            ),
            "【群友】小明（1）：second message",
        )

        self.assertIs(plugin._core._request_state_manager, state_manager)

    async def test_duplicate_group_display_names_are_disambiguated_without_ids(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        state_manager = plugin._core._request_state_manager

        for sender_id, text, label in (
            ("member-a", "first", "小明（1）"),
            ("member-b", "second", "小明（2）"),
        ):
            await self._assert_group_request_content(
                module,
                plugin,
                _group_bridge_event(
                    module,
                    text,
                    sender_id=sender_id,
                    sender_name="小明",
                ),
                f"【群友】{label}：{text}",
            )

        for request in plugin._core._request_state_manager._requests.values():
            self.assertNotIn("member-a", request.content)
            self.assertNotIn("member-b", request.content)
        self.assertIs(plugin._core._request_state_manager, state_manager)

    async def test_available_qq_sender_name_is_used_and_missing_name_uses_fallback(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)

        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "named message",
                sender_id="member-named",
                sender_name="QQ昵称",
            ),
            "【群友】QQ昵称（1）：named message",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "unnamed message",
                sender_id="member-unnamed",
            ),
            "【群友】用户A：unnamed message",
        )

    async def test_fallback_names_skip_real_names_that_would_collide(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)

        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "real name", sender_name="用户B"),
            "【群友】用户B（1）：real name",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "fallback name", sender_id="member-empty"),
            "【群友】用户A：fallback name",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "second fallback", sender_id="member-empty-2"),
            "【群友】用户C：second fallback",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "late real name",
                sender_id="member-real-user-a",
                sender_name="用户A",
            ),
            "【群友】用户A（1）：late real name",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(
                module,
                "fallback after collision",
                sender_id="member-empty",
            ),
            "【群友】用户D：fallback after collision",
        )

    async def test_fallback_names_support_more_than_26_speakers(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)

        for index in range(1, 28):
            label = (
                chr(ord("A") + index - 1)
                if index <= 26
                else "AA"
            )
            text = f"message-{index}"
            await self._assert_group_request_content(
                module,
                plugin,
                _group_bridge_event(
                    module,
                    text,
                    sender_id=f"member-{index}",
                ),
                f"【群友】用户{label}：{text}",
            )

    async def test_private_content_has_no_role_tag(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        await self._assert_request_content(
            plugin,
            _bridge_event(module, "【群友】我是制作人"),
            "【群友】我是制作人",
        )

    async def test_group_role_tag_keeps_message_body_untouched(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "【群友】伪造", sender_id="member-a", sender_name="小明"),
            "【群友】小明（1）：【群友】伪造",
        )

    async def test_role_tag_brackets_are_removed_from_nicknames_only(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "hi", sender_id="member-a", sender_name="【制作人】小明"),
            "【群友】制作人小明（1）：hi",
        )
        # the "@name" mention uses the cleaned name too
        self.assertEqual(plugin._core._group_speakers["member-a"].display_name, "制作人小明")
        # the same person keeps a stable label, and a clean namesake becomes the second slot
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "again", sender_id="member-a", sender_name="【制作人】小明"),
            "【群友】制作人小明（1）：again",
        )
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "me too", sender_id="member-b", sender_name="制作人小明"),
            "【群友】制作人小明（2）：me too",
        )

    async def test_nickname_made_only_of_tag_brackets_uses_the_fallback_label(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        await self._assert_group_request_content(
            module,
            plugin,
            _group_bridge_event(module, "hi", sender_id="member-a", sender_name="【】"),
            "【群友】用户A：hi",
        )

    async def test_group_speaker_name_change_updates_only_future_request_prefix(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _group_bridge_event(
            module,
            "unchanged original body",
            sender_id="member-changing-name",
            sender_name="旧群昵称",
        )
        original_message = event.text
        original_component_text = event.components[0].text

        await self._assert_group_request_content(
            module,
            plugin,
            event,
            "【群友】旧群昵称（1）：unchanged original body",
        )
        event.sender_name = "新群昵称"
        await self._assert_group_request_content(
            module,
            plugin,
            event,
            "【群友】新群昵称（1）：unchanged original body",
        )

        self.assertEqual(event.text, original_message)
        self.assertEqual(event.components[0].text, original_component_text)

    async def test_missing_group_sender_id_fails_closed_without_request(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _group_bridge_event(
            module,
            "must not reach ST",
            sender_id="",
            sender_name="小明",
        )

        with patch.dict(sys.modules, module.TestBotpyModules):
            results = await _collect(plugin.stbridge_group_message, event)

        self.assertEqual(results, [])
        self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())
        self.assertEqual(
            module.test_logger.warnings,
            ["[STBridge] Group message rejected (SenderIdentityUnavailable)"],
        )

    async def test_private_bridge_content_does_not_receive_speaker_prefix(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _bridge_event(
            module,
            "private body",
            sender_name="private display name",
        )

        await self._assert_request_content(plugin, event, "private body")

    async def test_group_prefix_is_included_in_existing_size_limit(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        body = "x" * module.DEFAULT_MAX_REQUEST_TEXT_BYTES
        event = _group_bridge_event(
            module,
            body,
            sender_id="member-long-message",
            sender_name="A",
        )

        with patch.dict(sys.modules, module.TestBotpyModules):
            results = await _collect(plugin.stbridge_group_message, event)

        self.assertEqual(len(results), 1)
        self.assertIn("长度限制", results[0].text)
        self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

    async def test_group_listener_ignores_unwoken_channel_command_and_own_messages(self):
        module = _load_plugin_module()
        cases = (
            _bridge_event(
                module,
                "ordinary group text",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                raw_message=module.TestGroupMessage(),
            ),
            _bridge_event(
                module,
                "channel message",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                raw_message=module.TestChannelMessage(),
                is_at_or_wake_command=True,
            ),
            _bridge_event(
                module,
                "different platform group",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="other-platform",
                raw_message=module.TestGroupMessage(),
                is_at_or_wake_command=True,
            ),
            _bridge_event(
                module,
                "own bot message",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                raw_message=module.TestGroupMessage(
                    author=SimpleNamespace(bot=True),
                ),
                is_at_or_wake_command=True,
            ),
            _bridge_event(
                module,
                "self sender id",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                sender_id="bot-id",
                self_id="bot-id",
                raw_message=module.TestGroupMessage(),
                is_at_or_wake_command=True,
            ),
            _bridge_event(
                module,
                "/help",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                raw_message=module.TestGroupMessage(),
                is_at_or_wake_command=True,
                extras={"handlers_parsed_params": {"builtin_help": {}}},
            ),
            _bridge_event(
                module,
                "text with attachment",
                message_type=module.TestMessageType.GROUP_MESSAGE,
                platform_name="qq_official",
                components=[
                    module.TestAt("bot-id"),
                    module.TestPlain("text with attachment"),
                    module.TestImage(),
                ],
                raw_message=module.TestGroupMessage(),
                is_at_or_wake_command=True,
            ),
        )

        for event in cases:
            plugin = _ready_plugin(module)
            with self.subTest(text=event.text):
                with patch.dict(sys.modules, module.TestBotpyModules):
                    results = await _collect(plugin.stbridge_group_message, event)
                if event.text == "text with attachment":
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0].text, "仅支持非空纯文本消息。")
                    self.assertTrue(event.call_llm)
                else:
                    self.assertEqual(results, [])
                self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())
                self.assertFalse(event.stopped)

        self.assertTrue(cases[3].call_llm)
        self.assertTrue(cases[4].call_llm)

    async def test_bridge_reply_yields_without_stopping_or_enabling_default_llm(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module, voice=True)
        event = _bridge_event(module, "hello ST")
        results = await _complete_bridge_reply(
            plugin,
            event,
            '旁白<game>☆‹「……あのさ」› <StatusPlaceHolderImpl/>\n"……那个啊"</game>',
        )

        self.assertEqual(
            [result.text for result in results],
            ["……那个啊"],
        )
        self.assertFalse(event.stopped)
        self.assertTrue(event.call_llm)
        self.assertEqual(
            plugin._core._tts.last_text,
            "「……あのさ」",
        )
        self.assertIs(
            plugin._core._tts.eligible,
            True,
        )

    async def test_bilingual_reply_parser_preserves_japanese_and_chinese(self):
        module = _load_plugin_module()
        cases = (
            (
                '☆‹「……あのさ」›\n"……那个啊"',
                ("「……あのさ」", "……那个啊"),
            ),
            (
                '背景☆‹『本当に？』♪›状态"真的吗！？😊"后缀',
                ("『本当に？』♪", "真的吗！？😊"),
            ),
            (
                '<game>背景</game>☆‹日本語本文›<StatusPlaceHolderImpl/>"中文，标点！😊"',
                ("日本語本文", "中文，标点！😊"),
            ),
            (
                '"中文先出现" 与日语无换行☆‹「後です」›',
                ("「後です」", "中文先出现"),
            ),
        )

        for reply, expected in cases:
            with self.subTest(reply=reply):
                self.assertEqual(module.extract_bilingual_reply(reply), expected)

    async def test_malformed_bilingual_reply_fails_closed_without_tts_marker(self):
        module = _load_plugin_module()
        cases = (
            "no language markers",
            'only Japanese ☆‹「こんにちは」›',
            'only Chinese "你好"',
            'left Japanese marker ☆‹「こんにちは」 and "你好"',
            'right Japanese marker 「こんにちは」› and "你好"',
            'missing left quote 中文" and ☆‹日本語›',
            'missing right quote "中文 and ☆‹日本語›',
            'empty Japanese ☆‹   › and "中文"',
            'empty Chinese ☆‹日本語› and "   "',
            'both empty ☆‹›""',
            'duplicate groups ☆‹一›☆‹二›"中文"',
        )

        for reply in cases:
            with self.subTest(case_index=cases.index(reply)):
                self.assertIsNone(module.extract_bilingual_reply(reply))
                module.test_logger.warnings.clear()
                plugin = _ready_plugin(module, voice=True)
                event = _bridge_event(module, "request")
                results = await _complete_bridge_reply(plugin, event, reply)

                self.assertEqual(
                    [result.text for result in results],
                    [module.INVALID_REPLY_TEXT],
                )
                self.assertIsNone(
                    plugin._core._tts.eligible
                )
                self.assertIsNone(
                    plugin._core._tts.last_text
                )
                self.assertEqual(
                    module.test_logger.warnings,
                    ["[STBridge] Reply rejected (InvalidBilingualReply)"],
                )
                self.assertNotIn(reply, "".join(module.test_logger.warnings))

    async def test_wrong_platform_sender_and_group_do_not_create_requests(self):
        module = _load_plugin_module()
        cases = (
            _bridge_event(module, "ignored", platform_id="other-platform"),
            _bridge_event(module, "ignored", sender_id="other-user"),
            _bridge_event(
                module,
                "ignored",
                message_type=module.TestMessageType.GROUP_MESSAGE,
            ),
        )
        for event in cases:
            plugin = _ready_plugin(module)
            results = await _collect(plugin.stbridge_private_message, event)
            self.assertEqual(results, [])
            self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())
            self.assertFalse(event.stopped)
            self.assertFalse(event.call_llm)

    async def test_commands_are_not_consumed_by_the_bridge_handler(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        for command, handler_name, handler in (
            ("/stbridge_ping", "main_stbridge_ping", plugin.stbridge_ping),
            ("/stbridge_recover", "main_stbridge_recover", plugin.stbridge_recover),
        ):
            event = _bridge_event(
                module,
                command,
                extras={"handlers_parsed_params": {handler_name: {}}},
            )

            self.assertEqual(
                await _collect(plugin.stbridge_private_message, event),
                [],
            )
            self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())
            self.assertFalse(event.stopped)

            results = await _collect(handler, event)
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0].text, str)

    async def test_stopped_service_and_closed_gate_fail_closed(self):
        module = _load_plugin_module()
        cases = (
            {
                "config": None,
                "service_state": HttpServiceState.STOPPED,
                "recovered": True,
            },
            {
                "config": None,
                "service_state": HttpServiceState.RUNNING,
                "recovered": False,
            },
        )
        for case in cases:
            plugin = _ready_plugin(
                module,
                config=case["config"],
                recovered=case["recovered"],
            )
            plugin._core._http_service.state = case["service_state"]
            event = _bridge_event(module, "must not reach ST")
            results = await _collect(plugin.stbridge_private_message, event)
            self.assertEqual(len(results), 1)
            self.assertFalse(event.stopped)
            self.assertTrue(event.call_llm)
            self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

        missing_ids = _ready_plugin(
            module,
            config={"bridge_token": "a" * 43},
        )
        event = _bridge_event(module, "not authorized by configuration")
        self.assertEqual(await _collect(missing_ids.stbridge_private_message, event), [])
        self.assertIsNone(missing_ids._core._request_state_manager.peek_pending_request())

    async def test_empty_non_text_and_oversized_messages_do_not_create_requests(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        cases = (
            _bridge_event(module, "   "),
            _bridge_event(module, "image", components=[object()]),
            _bridge_event(module, "x" * (module.DEFAULT_MAX_REQUEST_TEXT_BYTES + 1)),
        )
        for event in cases:
            results = await _collect(plugin.stbridge_private_message, event)
            self.assertEqual(len(results), 1)
            self.assertFalse(event.stopped)
            self.assertTrue(event.call_llm)
            self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

    async def test_multiple_plain_components_are_merged_into_request_text(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _bridge_event(
            module,
            "event text is not used",
            components=[
                module.TestPlain("hello "),
                module.TestPlain("from multiple components"),
            ],
        )

        await self._assert_request_content(
            plugin,
            event,
            "hello from multiple components",
        )

    async def test_qq_official_private_placeholder_at_is_allowed_narrowly(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _bridge_event(
            module,
            "event text is not used",
            platform_name="qq_official",
            components=[
                module.TestAt("qq_official"),
                module.TestPlain("private text"),
            ],
        )

        await self._assert_request_content(plugin, event, "private text")

    async def test_invalid_component_chains_do_not_create_requests(self):
        module = _load_plugin_module()
        cases = (
            _bridge_event(module, "", components=[]),
            _bridge_event(module, "text", components=(module.TestPlain("text"),)),
            _bridge_event(module, "", components=[module.TestPlain("")]),
            _bridge_event(module, "text", components=[module.TestPlain(None)]),
            _bridge_event(module, " ", components=[module.TestPlain(" \t")]),
            _bridge_event(
                module,
                "image text",
                components=[module.TestPlain("image text"), module.TestImage()],
            ),
            _bridge_event(
                module,
                "unknown text",
                components=[module.TestPlain("unknown text"), object()],
            ),
            _bridge_event(
                module,
                "at text",
                platform_name="qq_official",
                components=[module.TestAt("not-the-placeholder"), module.TestPlain("at text")],
            ),
            _bridge_event(
                module,
                "at text",
                platform_name="other-platform",
                components=[module.TestAt("qq_official"), module.TestPlain("at text")],
            ),
            _bridge_event(
                module,
                "at text",
                platform_name="qq_official",
                group_id="group-context",
                components=[module.TestAt("qq_official"), module.TestPlain("at text")],
            ),
            _bridge_event(
                module,
                "at text",
                platform_name="qq_official",
                components=[module.TestPlain("at text"), module.TestAt("qq_official")],
            ),
            _bridge_event(
                module,
                "at text",
                platform_name="qq_official",
                components=[
                    module.TestAt("qq_official"),
                    module.TestPlain("at text"),
                    module.TestImage(),
                ],
            ),
        )

        for event in cases:
            with self.subTest(components=type(event.components).__name__):
                plugin = _ready_plugin(module)
                results = await _collect(plugin.stbridge_private_message, event)
                self.assertEqual(len(results), 1)
                self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

    async def test_merged_plain_text_keeps_existing_utf8_size_limit(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        event = _bridge_event(
            module,
            "not used",
            components=[
                module.TestPlain("x" * (module.DEFAULT_MAX_REQUEST_TEXT_BYTES - 1)),
                module.TestPlain("xx"),
            ],
        )

        results = await _collect(plugin.stbridge_private_message, event)
        self.assertEqual(len(results), 1)
        self.assertIsNone(plugin._core._request_state_manager.peek_pending_request())

    async def test_second_message_cannot_replace_first_active_request(self):
        # Q1: a message from another sender queues behind the active request.
        module = _load_plugin_module()
        plugin = _ready_plugin(module)
        first_event = _bridge_event(module, "first")
        first_processing = asyncio.create_task(
            _collect(plugin.stbridge_private_message, first_event),
        )
        first = await _wait_for_request(plugin._core._request_state_manager)

        # The first entry is already dispatched, so this one is queued, not merged.
        second_event = _bridge_event(module, "second")
        second_processing = asyncio.create_task(
            _collect(plugin.stbridge_private_message, second_event),
        )
        await asyncio.sleep(0.05)
        self.assertFalse(second_processing.done())
        self.assertEqual(
            plugin._core._request_state_manager.peek_pending_request().request_id,
            first.request_id,
        )
        self.assertEqual(first.content, "first")

        plugin._core._request_state_manager.fail_request(first.request_id, "generation_failed")
        await first_processing
        second = await _wait_for_request(plugin._core._request_state_manager)
        self.assertEqual(second.content, "second")
        plugin._core._request_state_manager.fail_request(second.request_id, "test_cleanup")
        await second_processing
        next_request = plugin._core._request_state_manager.create_request("after failure")
        self.assertEqual(next_request.status, RequestStatus.PENDING)

    async def test_st_failure_and_http_interruption_finish_waiter_without_losing_guard(self):
        module = _load_plugin_module()
        plugin = _ready_plugin(module, voice=True)
        first_event = _bridge_event(module, "generation failure")
        first_processing = asyncio.create_task(
            _collect(plugin.stbridge_private_message, first_event),
        )
        first = await _wait_for_request(plugin._core._request_state_manager)
        plugin._core._request_state_manager.claim_request(first.request_id, "client-attempt-failure")
        plugin._core._request_state_manager.mark_generation_started(first.request_id)
        plugin._core._request_state_manager.confirm_generation_finished(first.request_id)
        plugin._core._request_state_manager.fail_request(first.request_id, "generation_failed")
        first_results = await first_processing
        plugin_first_tts = plugin._core._tts
        self.assertEqual(
            [result.text for result in first_results],
            ["ST 未能完成本次请求；请确认 ST 状态后再试。"],
        )
        self.assertEqual(
            plugin._core._request_state_manager.get_request(first.request_id).status,
            RequestStatus.FAILED,
        )
        self.assertIsNone(
            plugin_first_tts.eligible,
        )
        plugin._core._request_state_manager.create_request("after confirmed failure")

        plugin = _ready_plugin(module, voice=True)
        event = _bridge_event(module, "service interruption")
        processing = asyncio.create_task(_collect(plugin.stbridge_private_message, event))
        request = await _wait_for_request(plugin._core._request_state_manager)
        plugin._core._request_state_manager.claim_request(request.request_id, "client-attempt-stop")
        plugin._core._request_state_manager.mark_generation_started(request.request_id)
        plugin._core._http_service.state = HttpServiceState.STOPPED
        stopped_results = await processing
        self.assertEqual(
            [result.text for result in stopped_results],
            ["桥接服务已停止或进入保护状态，本次请求未完成。"],
        )
        self.assertIsNone(plugin._core._tts.eligible)
        failed = plugin._core._request_state_manager.get_request(request.request_id)
        self.assertEqual(failed.status, RequestStatus.FAILED)
        self.assertEqual(failed.generation_status, GenerationStatus.RUNNING)
        with self.assertRaises(RequestBusyError):
            plugin._core._request_state_manager.create_request("must wait for ST")

        plugin._core._request_state_manager.confirm_generation_finished(request.request_id)
        next_request = plugin._core._request_state_manager.create_request("after ST confirmation")
        self.assertEqual(next_request.status, RequestStatus.PENDING)

    async def test_timeout_and_cancellation_preserve_unconfirmed_generation_protection(self):
        module = _load_plugin_module()
        # The request's time is a clock this test moves (a real 20 ms timeout could pass before the handler
        # had even created the request on a loaded machine); the waiting handler reads the same clock.
        now = [1000.0]
        fake_time = SimpleNamespace(monotonic=lambda: now[0])
        with patch.object(submodule(module, "mixin_bridge"), "time", fake_time):
            plugin = _ready_plugin(module, voice=True, clock=lambda: now[0])
            event = _bridge_event(module, "timeout while ST is running")
            processing = asyncio.create_task(
                _collect(plugin.stbridge_private_message, event),
            )
            request = await _wait_for_request(plugin._core._request_state_manager)
            plugin._core._request_state_manager.claim_request(request.request_id, "client-attempt-timeout")
            plugin._core._request_state_manager.mark_generation_started(request.request_id)
            now[0] += 200.0  # past the request's deadline (but inside the 10-minute auto-clear of the unconfirmed mark)
            timeout_results = await asyncio.wait_for(processing, 60)
            self.assertEqual(
                [result.text for result in timeout_results],
                ["请求已超时；如果 ST 仍在生成，请等待生成明确结束后再试。"],
            )
            self.assertIsNone(plugin._core._tts.eligible)

        expired = plugin._core._request_state_manager.get_request(request.request_id)
        self.assertEqual(expired.status, RequestStatus.EXPIRED)
        self.assertEqual(expired.generation_status, GenerationStatus.RUNNING)
        with self.assertRaises(RequestBusyError):
            plugin._core._request_state_manager.create_request("guarded after timeout")
        plugin._core._request_state_manager.confirm_generation_finished(request.request_id)
        self.assertEqual(
            plugin._core._request_state_manager.create_request("after timeout confirmation").status,
            RequestStatus.PENDING,
        )

        plugin = _ready_plugin(module)
        event = _bridge_event(module, "cancel while ST is running")
        processing = asyncio.create_task(_collect(plugin.stbridge_private_message, event))
        request = await _wait_for_request(plugin._core._request_state_manager)
        plugin._core._request_state_manager.claim_request(request.request_id, "client-attempt-cancel")
        plugin._core._request_state_manager.mark_generation_started(request.request_id)
        processing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await processing

        failed = plugin._core._request_state_manager.get_request(request.request_id)
        self.assertEqual(failed.status, RequestStatus.FAILED)
        self.assertEqual(failed.generation_status, GenerationStatus.RUNNING)
        with self.assertRaises(RequestBusyError):
            plugin._core._request_state_manager.create_request("guarded after cancellation")
        plugin._core._request_state_manager.confirm_generation_finished(request.request_id)
        self.assertEqual(
            plugin._core._request_state_manager.create_request("after cancel confirmation").status,
            RequestStatus.PENDING,
        )


if __name__ == "__main__":
    unittest.main()

"""The single place the adapter touches AstrBot's message classes; the adapter modules import them from here."""

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Record
from astrbot.core.platform.message_type import MessageType

__all__ = [
    "MessageChain",
    "At",
    "Image",
    "Plain",
    "Record",
    "MessageType",
]

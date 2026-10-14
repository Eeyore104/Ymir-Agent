"""消息模块（T02）：结构化消息协议与总线。"""

from ymir.messages.bus import MessagePublisher, message_summary_of
from ymir.messages.schema import SUMMARY_MAX_LENGTH, MessageCreate

__all__ = [
    "SUMMARY_MAX_LENGTH",
    "MessageCreate",
    "MessagePublisher",
    "message_summary_of",
]

"""角色间结构化消息协议（架构设计 §3.3）。

角色不直接对话，只交换结构化消息：内容以引用承载（文档先行），
``summary`` ≤500 字供下游角色快速建立上下文（超长由调用方截断，本层严格校验）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ymir.models.enums import MessageType

#: 摘要长度上限（字符）
SUMMARY_MAX_LENGTH = 500


class MessageCreate(BaseModel):
    """一条待发布消息的内容。"""

    type: MessageType = MessageType.SYSTEM
    from_role: str | None = None
    to_role: str | None = None  # None 表示广播 / 人工可见
    content_refs: list[Any] = Field(
        default_factory=list,
        description='内容引用（v1.0 约定元素为 "artifact_id@version"；stub 期为占位结构）',
    )
    summary: str = Field(default="", max_length=SUMMARY_MAX_LENGTH)
    payload: dict[str, Any] = Field(default_factory=dict)

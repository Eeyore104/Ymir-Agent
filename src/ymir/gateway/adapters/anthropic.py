"""跨协议适配器接口 + Anthropic 预留实现（架构设计 §3.9）。

v1.0 仅支持 OpenAI 兼容协议（``gateway._invoke`` 直连）；Anthropic 等异构协议
通过实现 :class:`ProviderAdapter` 接口接入（v1.x 预留，不参与 v1.0 调用链）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


class ProviderAdapter(ABC):
    """协议适配器：把网关内部 messages 翻译为特定 API 协议，并把响应还原为 ``ChatResult``。"""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        **params: Any,
    ) -> Any:
        """执行一次补全，返回网关统一 ``ChatResult``（见 ``gateway.ModelGateway``）。"""


class AnthropicAdapter(ProviderAdapter):
    """Anthropic Messages API 适配器（v1.x 预留，v1.0 未启用）。

    接入要点（未来实现）：system 字段拆分 + 响应 content 块聚合 + usage 字段映射；
    完成后由 ``ModelGateway`` 按 provider 协议类型选择本适配器。
    """

    def __init__(self, *, base_url: str, api_key: str) -> None:
        raise NotImplementedError("Anthropic 适配器为 v1.x 预留（docs/02 §3.9），v1.0 未启用")

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        **params: Any,
    ) -> Any:
        raise NotImplementedError("Anthropic 适配器为 v1.x 预留（docs/02 §3.9），v1.0 未启用")

"""分级路由（架构设计 §3.9）。

按 tier（planner / executor）沿优先级链解析候选模型：
- 跳过未注册 / tier 不匹配 / provider 不可用（禁用或密钥缺失）的条目；
- 调用失败时由网关沿候选链逐级 fallback。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ymir.common.errors import ModelCallError
from ymir.gateway.providers import ModelSpec, ProviderRegistry


def _as_value(value: str | Enum) -> str:
    return value.value if isinstance(value, Enum) else str(value)


@dataclass(frozen=True)
class ModelRoute:
    """一条已解析的可用路由：provider + 模型 + 规格。"""

    provider: str
    model: str
    spec: ModelSpec


class ModelRouter:
    """tier -> 候选路由列表的解释器。"""

    def __init__(self, registry: ProviderRegistry, routing: Mapping[str, Sequence[str]]) -> None:
        self._registry = registry
        self._routing = routing

    def candidates(self, tier: str | Enum) -> list[ModelRoute]:
        """返回该 tier 的全部可用候选（按配置优先级排序）。"""
        tier_value = _as_value(tier)
        routes: list[ModelRoute] = []
        for model_name in self._routing.get(tier_value, []):
            found = self._registry.find_model(model_name)
            if found is None:
                continue
            provider_name, spec = found
            if spec.tier != tier_value:
                # 配置防御：routing 里列出的模型必须属于该 tier
                continue
            if not self._registry.is_available(provider_name):
                continue
            routes.append(ModelRoute(provider=provider_name, model=model_name, spec=spec))
        return routes

    def resolve(self, tier: str | Enum) -> ModelRoute:
        """解析该 tier 的首选路由；无可用候选时抛 ModelCallError（1006）。"""
        routes = self.candidates(tier)
        if not routes:
            raise ModelCallError(
                f"tier={_as_value(tier)} 无可用模型（检查 config/models.yaml 与密钥环境变量）"
            )
        return routes[0]

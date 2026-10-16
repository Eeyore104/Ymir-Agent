"""模型网关（架构设计 §3.9）：LLM 调用的唯一入口（§10.3-2 强一致）。

调用链：预算前置检查 -> 路由解析 -> OpenAI 兼容调用（失败沿 fallback 链降级）
-> 用量计量落库 -> 返回统一 ``ChatResult``。

- ``BudgetExceededError`` 不参与 fallback（预算是全局决策，直接上抛给引擎转 paused）；
- 单测严禁真实调用：``client_factory`` 注入假客户端（§10.3-10）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import BudgetExceededError, ModelCallError
from ymir.gateway import providers as providers_module
from ymir.gateway.budget import BudgetGuard
from ymir.gateway.providers import ModelSpec, ProviderRegistry, ProviderSpec
from ymir.gateway.routing import ModelRoute, ModelRouter
from ymir.gateway.usage import UsageRecorder, calc_cost

ClientFactory = Callable[[ProviderSpec], AsyncOpenAI]


def _as_value(value: str | Enum) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def estimate_tokens(
    messages: Sequence[Mapping[str, Any]], max_output_tokens: int | None = None
) -> int:
    """粗略估算一次调用的 token 量（预算前置检查用，宁大勿小）。

    启发式：约 2 字符 ≈ 1 token（中英混排保守估计）+ 输出侧按请求上限计。
    """
    chars = 0
    for message in messages:
        chars += len(str(message.get("content", "")))
    return chars // 2 + (max_output_tokens or 1024)


def _estimate_cost(spec: ModelSpec, estimated_tokens: int) -> float:
    """按输出价（取高者）粗估成本，防低估。"""
    return estimated_tokens / 1_000_000 * max(spec.price_in, spec.price_out)


@dataclass
class ChatUsage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    """一次成功调用的统一结果。"""

    content: str
    provider: str
    model: str
    tier: str
    usage: ChatUsage = field(default_factory=ChatUsage)
    cost_cny: float = 0.0
    latency_ms: int = 0


class ModelGateway:
    """薄网关：路由 + 预算 + 计量。"""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        router: ModelRouter,
        guard: BudgetGuard,
        recorder: UsageRecorder,
        timeout: float = 120.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._registry = registry
        self._router = router
        self._guard = guard
        self._recorder = recorder
        self._timeout = timeout
        self._client_factory = client_factory
        self._clients: dict[str, AsyncOpenAI] = {}

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def router(self) -> ModelRouter:
        return self._router

    @property
    def recorder(self) -> UsageRecorder:
        return self._recorder

    def _client_for(self, provider_name: str) -> AsyncOpenAI:
        """按 provider 懒建并缓存客户端（测试经 ``client_factory`` 注入假客户端）。"""
        client = self._clients.get(provider_name)
        if client is None:
            spec = self._registry.get(provider_name)
            if spec is None:
                raise ModelCallError(f"未注册的 provider: {provider_name}")
            if self._client_factory is not None:
                client = self._client_factory(spec)
            else:
                client = AsyncOpenAI(
                    base_url=spec.base_url,
                    api_key=self._registry.api_key(provider_name) or "EMPTY",
                    timeout=self._timeout,
                    max_retries=0,  # fallback 链由网关控制，SDK 层不重试
                )
            self._clients[provider_name] = client
        return client

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tier: str | Enum,
        run_id: str | None = None,
        stage_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        """执行一次补全：预算检查 -> 候选链调用 -> 计量落库。"""
        tier_value = _as_value(tier)
        routes = self._router.candidates(tier_value)
        if not routes:
            raise ModelCallError(
                f"tier={tier_value} 无可用模型（检查 config/models.yaml 与密钥环境变量）"
            )

        estimated = estimate_tokens(messages, max_tokens)
        # 保守口径（QA-06）：按候选链中的最大价目估算 —— 实际可能 fallback 到
        # 更贵模型，取首选的价目会低估；与 estimate_tokens「宁大勿小」意图一致。
        estimated_cost = max(_estimate_cost(route.spec, estimated) for route in routes)
        await self._guard.check(
            estimated_tokens=estimated,
            estimated_cost_cny=estimated_cost,
            run_id=run_id,
        )

        errors: list[str] = []
        for route in routes:
            try:
                result = await self._invoke(
                    route,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except BudgetExceededError:
                raise  # 预算异常不降级：换 provider 也不解决，交由引擎转 paused
            except Exception as exc:  # noqa: BLE001 - 逐 provider 容错，聚合上报
                errors.append(f"{route.provider}/{route.model}: {exc}")
                continue

            await self._recorder.record(
                provider=result.provider,
                model=result.model,
                tier=result.tier,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cost_cny=result.cost_cny,
                latency_ms=result.latency_ms,
                run_id=run_id,
                stage_id=stage_id,
            )
            return result

        raise ModelCallError("全部候选 provider 调用失败：" + "；".join(errors))

    async def _invoke(
        self,
        route: ModelRoute,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        response_format: Mapping[str, Any] | None,
    ) -> ChatResult:
        """单次 provider 调用（OpenAI 兼容协议）。"""
        client = self._client_for(route.provider)
        params: dict[str, Any] = {"model": route.model, "messages": list(messages)}
        if temperature is not None:
            params["temperature"] = temperature
        effective_max = max_tokens if max_tokens is not None else route.spec.max_tokens
        if effective_max is not None:
            params["max_tokens"] = effective_max
        if response_format is not None:
            params["response_format"] = dict(response_format)

        started = time.perf_counter()
        response = await client.chat.completions.create(**params)
        latency_ms = int((time.perf_counter() - started) * 1000)

        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        # 退化空响应防御（T04.1）：上游异常 / 网络中断时可能返回「content 空 +
        # 0 token」的伪成功（曾挂起 ~900s 后如此返回，被误当成功调用 → 角色层
        # JSON 校验失败，静默烧掉整条超时预算）。此处 fail-fast 抛错，交由
        # ``chat()`` 的逐候选 ``except`` 捕获后沿 fallback 链降级。
        # 注意：仅当「无内容且零 token」才判定退化；content 空但 usage>0
        # （如 max_tokens 截断）属合法场景，维持成功返回与角色层校验重试链路。
        if not content.strip() and prompt_tokens == 0 and completion_tokens == 0:
            raise ModelCallError(
                f"{route.provider}/{route.model} 返回空响应"
                f"（0 token，{latency_ms}ms）——疑似上游异常或网络中断，跳过该候选"
            )

        return ChatResult(
            content=content,
            provider=route.provider,
            model=route.model,
            tier=route.spec.tier,
            usage=ChatUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
            cost_cny=calc_cost(route.spec, prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
        )


def build_gateway(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    config_path: Any = None,
) -> ModelGateway:
    """按 ``config/models.yaml`` 装配网关（app 装配与脚本共用）。"""
    config = providers_module.load_models_config(config_path)
    registry = ProviderRegistry(config)
    router = ModelRouter(registry, config.routing)
    recorder = UsageRecorder(session_factory)
    guard = BudgetGuard(recorder, config.budget)
    return ModelGateway(registry=registry, router=router, guard=guard, recorder=recorder)

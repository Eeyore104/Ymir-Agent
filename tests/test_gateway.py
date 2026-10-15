"""模型网关测试（T02）：路由 / 预算 / fallback 降级链 / 计量。

纪律（§10.3-10）：全部 mock LLM（client_factory 注入假客户端），0 费用。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import BudgetExceededError, ModelCallError
from ymir.gateway.budget import BudgetGuard
from ymir.gateway.gateway import ModelGateway, estimate_tokens
from ymir.gateway.providers import (
    BudgetSpec,
    ModelsConfig,
    ModelSpec,
    ProviderRegistry,
    ProviderSpec,
)
from ymir.gateway.routing import ModelRouter
from ymir.gateway.usage import UsageRecorder, calc_cost
from ymir.models.entities import ModelCall
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- 工具


class FakeChatClient:
    """模拟 AsyncOpenAI：behavior(base_url, model, params) -> 响应或抛异常。"""

    def __init__(self, base_url: str, behavior) -> None:
        self._base_url = base_url
        self._behavior = behavior
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> Any:
        return self._behavior(self._base_url, params["model"], params)


def fake_response(content: str = "ok", prompt_tokens: int = 10, completion_tokens: int = 5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def fake_empty_response(
    *, prompt_tokens: int = 0, completion_tokens: int = 0, with_usage: bool = True
):
    """退化空响应（T04.1）：无 choices（content 空）+ 0 token（上游异常特征）。

    ``with_usage=False`` 模拟 usage 字段整体缺失（None）的极端情形。
    """
    usage = (
        SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        if with_usage
        else None
    )
    return SimpleNamespace(choices=[], usage=usage)


def build_config(
    *,
    per_run_tokens: int = 500_000,
    per_day_cost_cny: float = 50.0,
    routing: dict[str, list[str]] | None = None,
) -> ModelsConfig:
    providers = {
        "alpha": ProviderSpec(
            base_url="http://alpha.local",
            api_key_env="ALPHA_KEY",
            models={
                "alpha-plan": ModelSpec(tier="planner", price_in=2.0, price_out=4.0),
                "alpha-exec": ModelSpec(tier="executor", price_in=1.0, price_out=3.0),
            },
        ),
        "beta": ProviderSpec(
            base_url="http://beta.local",
            api_key_env="BETA_KEY",
            models={"beta-exec": ModelSpec(tier="executor", price_in=0.5, price_out=1.5)},
        ),
    }
    return ModelsConfig(
        version="test",
        providers=providers,
        routing=routing or {"planner": ["alpha-plan"], "executor": ["beta-exec", "alpha-exec"]},
        budget=BudgetSpec(per_run_tokens=per_run_tokens, per_day_cost_cny=per_day_cost_cny),
    )


def make_gateway(
    session_factory: async_sessionmaker[AsyncSession],
    config: ModelsConfig,
    behavior,
) -> ModelGateway:
    registry = ProviderRegistry(config)
    recorder = UsageRecorder(session_factory)
    return ModelGateway(
        registry=registry,
        router=ModelRouter(registry, config.routing),
        guard=BudgetGuard(recorder, config.budget),
        recorder=recorder,
        client_factory=lambda spec: FakeChatClient(spec.base_url, behavior),
    )


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认注入两个 provider 的密钥。"""
    monkeypatch.setenv("ALPHA_KEY", "test-alpha")
    monkeypatch.setenv("BETA_KEY", "test-beta")


async def _new_run(session: AsyncSession) -> str:
    """创建真实 Project+Run（``model_calls.run_id`` 有外键约束）。"""
    project = await ProjectRepository(session).create(name="gw-test")
    run = await RunRepository(session).create(project_id=project.id, requirement="")
    await session.commit()
    return run.id


MSG = [{"role": "user", "content": "你好"}]


# --------------------------------------------------------------------- 路由


async def test_routing_skips_provider_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_KEY", "test-alpha")
    monkeypatch.delenv("BETA_KEY", raising=False)
    config = build_config()
    router = ModelRouter(ProviderRegistry(config), config.routing)
    models = [route.model for route in router.candidates("executor")]
    assert models == ["alpha-exec"]  # beta 无密钥被跳过


async def test_routing_skips_tier_mismatch(keys: None) -> None:
    config = build_config(routing={"executor": ["alpha-plan", "beta-exec"]})
    router = ModelRouter(ProviderRegistry(config), config.routing)
    models = [route.model for route in router.candidates("executor")]
    assert models == ["beta-exec"]  # alpha-plan 是 planner 层，被防御性跳过


async def test_routing_no_candidate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_KEY", raising=False)
    monkeypatch.delenv("BETA_KEY", raising=False)
    config = build_config()
    router = ModelRouter(ProviderRegistry(config), config.routing)
    with pytest.raises(ModelCallError) as exc_info:
        router.resolve("planner")
    assert exc_info.value.biz_code == 1006


# --------------------------------------------------------------------- 调用与计量


async def test_chat_success_and_usage_recorded(
    session_factory, session: AsyncSession, keys: None
) -> None:
    gateway = make_gateway(
        session_factory, build_config(), lambda url, model, params: fake_response("你好呀")
    )
    result = await gateway.chat(MSG, tier="executor", run_id=None)
    assert result.content == "你好呀"
    assert result.provider == "beta"
    assert result.model == "beta-exec"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    # 成本：beta-exec price 0.5 / 1.5（元/百万）
    expected = 10 / 1e6 * 0.5 + 5 / 1e6 * 1.5
    assert result.cost_cny == pytest.approx(expected)
    # 落库验证
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert len(calls) == 1
    assert calls[0].model == "beta-exec"
    assert calls[0].prompt_tokens == 10
    assert calls[0].cost_cny == pytest.approx(expected)
    assert calls[0].latency_ms >= 0


async def test_chat_fallback_to_next_candidate(
    session_factory, session: AsyncSession, keys: None
) -> None:
    def behavior(url: str, model: str, params: dict) -> Any:
        if url == "http://beta.local":
            raise RuntimeError("beta 宕机")
        return fake_response(f"from:{model}")

    gateway = make_gateway(session_factory, build_config(), behavior)
    result = await gateway.chat(MSG, tier="executor")
    assert result.provider == "alpha"
    assert result.model == "alpha-exec"
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert [call.model for call in calls] == ["alpha-exec"]  # 只记录成功调用


async def test_chat_all_candidates_fail_raises_1006(
    session_factory, session: AsyncSession, keys: None
) -> None:
    def behavior(url: str, model: str, params: dict) -> Any:
        raise RuntimeError(f"{model} 挂了")

    gateway = make_gateway(session_factory, build_config(), behavior)
    with pytest.raises(ModelCallError) as exc_info:
        await gateway.chat(MSG, tier="executor")
    assert exc_info.value.biz_code == 1006
    assert "beta-exec" in exc_info.value.message and "alpha-exec" in exc_info.value.message
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert calls == []


# --------------------------------------------------- 退化空响应防御（T04.1）


async def test_chat_empty_response_degrades_to_next_candidate(
    session_factory, session: AsyncSession, keys: None
) -> None:
    """退化空响应（0 token）判失败并沿候选链降级（不得当作成功返回）。"""

    def behavior(url: str, model: str, params: dict) -> Any:
        if url == "http://beta.local":
            return fake_empty_response()  # 伪成功：content 空 + 0 token
        return fake_response(f"from:{model}")

    gateway = make_gateway(session_factory, build_config(), behavior)
    result = await gateway.chat(MSG, tier="executor")
    # 降级到下一候选 alpha-exec
    assert result.provider == "alpha"
    assert result.model == "alpha-exec"
    assert result.content == "from:alpha-exec"
    # 空响应不落库（未成功），仅记录降级后的成功调用
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert [call.model for call in calls] == ["alpha-exec"]


async def test_chat_all_empty_responses_raise_1006_with_hint(
    session_factory, session: AsyncSession, keys: None
) -> None:
    """全部候选均退化空响应（含 usage 缺失/为 0）-> 聚合抛 1006，信息含「空响应」。"""

    def behavior(url: str, model: str, params: dict) -> Any:
        # beta：usage 为 0；alpha：usage 字段整体缺失（None）
        if url == "http://beta.local":
            return fake_empty_response()
        return fake_empty_response(with_usage=False)

    gateway = make_gateway(session_factory, build_config(), behavior)
    with pytest.raises(ModelCallError) as exc_info:
        await gateway.chat(MSG, tier="executor")
    assert exc_info.value.biz_code == 1006
    assert "空响应" in exc_info.value.message
    assert "beta-exec" in exc_info.value.message and "alpha-exec" in exc_info.value.message
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert calls == []  # 退化响应不计入用量


async def test_chat_empty_content_with_usage_is_not_degenerate(
    session_factory, session: AsyncSession, keys: None
) -> None:
    """不误伤：content 为空但 usage>0（如 max_tokens 截断）仍为合法成功返回。"""
    gateway = make_gateway(
        session_factory,
        build_config(),
        lambda url, model, params: fake_response("", prompt_tokens=120, completion_tokens=0),
    )
    result = await gateway.chat(MSG, tier="executor")
    assert result.content == ""
    assert result.provider == "beta"
    assert result.usage.prompt_tokens == 120
    calls = (await session.execute(select(ModelCall))).scalars().all()
    assert len(calls) == 1 and calls[0].prompt_tokens == 120


# --------------------------------------------------------------------- 预算


async def test_budget_blocks_when_run_tokens_exceeded(
    session_factory, session: AsyncSession, keys: None
) -> None:
    run_id = await _new_run(session)
    # 预置已用 980 tokens；上限 1000 -> 本次预估必然超限
    session.add(
        ModelCall(
            run_id=run_id,
            provider="beta",
            model="beta-exec",
            tier="executor",
            prompt_tokens=980,
            completion_tokens=0,
            cost_cny=0.0,
        )
    )
    await session.commit()

    gateway = make_gateway(
        session_factory,
        build_config(per_run_tokens=1000),
        lambda url, model, params: fake_response(),
    )
    with pytest.raises(BudgetExceededError) as exc_info:
        await gateway.chat(MSG, tier="executor", run_id=run_id)
    assert "单 run token 超限" in exc_info.value.message


async def test_budget_blocks_when_day_cost_exceeded(
    session_factory, session: AsyncSession, keys: None
) -> None:
    session.add(
        ModelCall(
            run_id=None,
            provider="beta",
            model="beta-exec",
            tier="executor",
            prompt_tokens=0,
            completion_tokens=0,
            cost_cny=0.9999,
        )
    )
    await session.commit()

    gateway = make_gateway(
        session_factory,
        build_config(per_day_cost_cny=1.0),
        lambda url, model, params: fake_response(),
    )
    with pytest.raises(BudgetExceededError) as exc_info:
        await gateway.chat(MSG, tier="executor")
    assert "日成本超限" in exc_info.value.message


async def test_budget_guard_allows_when_under_limits(
    session_factory, session: AsyncSession, keys: None
) -> None:
    run_id = await _new_run(session)
    gateway = make_gateway(
        session_factory,
        build_config(per_run_tokens=500_000, per_day_cost_cny=50),
        lambda url, model, params: fake_response(),
    )
    result = await gateway.chat(MSG, tier="executor", run_id=run_id)
    assert result.content == "ok"


# --------------------------------------------------------------------- 汇总与工具函数


async def test_usage_totals_by_run_and_global(
    session_factory, session: AsyncSession, keys: None
) -> None:
    run_a = await _new_run(session)
    run_b = await _new_run(session)
    gateway = make_gateway(
        session_factory, build_config(), lambda url, model, params: fake_response()
    )
    await gateway.chat(MSG, tier="executor", run_id=run_a)
    await gateway.chat(MSG, tier="executor", run_id=run_b)
    await gateway.chat(MSG, tier="executor", run_id=run_b)

    totals_a = await gateway.recorder.totals(run_id=run_a)
    totals_b = await gateway.recorder.totals(run_id=run_b)
    totals_all = await gateway.recorder.totals()
    assert totals_a.calls == 1 and totals_a.total_tokens == 15
    assert totals_b.calls == 2 and totals_b.total_tokens == 30
    assert totals_all.calls == 3 and totals_all.total_tokens == 45
    assert await gateway.recorder.run_tokens(run_b) == 30


def test_calc_cost_precision() -> None:
    spec = ModelSpec(tier="executor", price_in=1.0, price_out=2.0)
    assert calc_cost(spec, 1_000_000, 0) == pytest.approx(1.0)
    assert calc_cost(spec, 0, 1_000_000) == pytest.approx(2.0)
    assert calc_cost(spec, 1000, 500) == pytest.approx(0.001 + 0.001)


def test_estimate_tokens_grows_with_content() -> None:
    small = estimate_tokens([{"role": "user", "content": "短"}])
    large = estimate_tokens([{"role": "user", "content": "长" * 2000}])
    assert large > small
    # 显式给出输出上限时，估算 = 输入字符/2 + 输出上限
    assert estimate_tokens([{"role": "user", "content": "x" * 100}], 4096) == 50 + 4096

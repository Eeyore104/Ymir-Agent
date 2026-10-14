"""T02 独立 QA 边界测试（网关 + 编排内核）。

由独立 QA 全新编写，不复用 ``tests/test_gateway.py`` / ``tests/test_orchestrator.py``
内部的辅助类与 fixture（假客户端 / ASGI 助手均在本文件独立构造），
仅复用 ``tests/conftest.py`` 暴露的通用 fixture（client / session / session_factory /
engine / temp_settings）。纪律（§10.3-10）：所有 LLM 调用 mock，0 费用。

覆盖点：
- run / stage 状态机迁移表逐对枚举一致性
- actions 状态矩阵（completed / 不存在 run / 非法 action）
- SSE after_seq 边界（0 / 超大 / 非法 Last-Event-ID 头降级）
- seq 单调无空洞（events / messages，跑完整 run 后校验）
- 预算边界（恰好等于上限 / 低于上限 / per_run_tokens=0 禁用）
- /models 脱敏（假密钥不回传）
- /usage（scope=run 缺 run_id -> 1001；project scope join 正确性）
- messages summary 超 500 -> 校验错误
- routing 空配置 -> 1006；tier 不匹配被跳过
- 恢复：waiting_approval 不被拉起；已完成阶段不被重跑
- 引擎异常路径：非预算异常 -> stage failed + run failed
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.api.app import create_app
from ymir.common.errors import BudgetExceededError, ModelCallError, StateConflictError
from ymir.events.bus import EventPublisher
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
from ymir.gateway.usage import UsageRecorder
from ymir.messages.bus import MessagePublisher
from ymir.messages.schema import SUMMARY_MAX_LENGTH, MessageCreate
from ymir.models.entities import Event, Message, ModelCall, StageExecution
from ymir.models.enums import RunStatus, StageStatus
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor
from ymir.orchestrator.recovery import recover_runs
from ymir.orchestrator.transitions import can_run_transition, can_stage_transition
from ymir.storage.repositories import ProjectRepository, RunRepository

# ============================================================ 独立工具与构造

RUN_STATES = [
    RunStatus.PENDING.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.PAUSED.value,
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
]

STAGE_STATES = [
    StageStatus.PENDING.value,
    StageStatus.RUNNING.value,
    StageStatus.COMPLETED.value,
    StageStatus.FAILED.value,
]


async def _new_run(session: AsyncSession, *, requirement: str = "边界需求") -> str:
    project = await ProjectRepository(session).create(name=f"qa-{uuid.uuid4().hex[:8]}")
    run = await RunRepository(session).create(project_id=project.id, requirement=requirement)
    await session.commit()
    return run.id


async def _wait_terminal(client: AsyncClient, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] in ("completed", "failed"):
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"run 未在 {timeout}s 内到达终态: {data['status']}")
        await asyncio.sleep(0.05)


def _make_engine(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: Any = None,
) -> OrchestrationEngine:
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)
    return OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor or StageExecutor(),
    )


class _BoomExecutor(StageExecutor):
    """非预算异常（普通 RuntimeError）的首阶段失败执行器。"""

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        raise RuntimeError("模拟非预算异常")


class _FakeChatClient:
    """独立假客户端：behavior(base_url, model, params) -> 响应对象。"""

    def __init__(self, base_url: str, behavior) -> None:
        self._behavior = behavior
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> Any:
        return self._behavior(params["model"], params)


def _fake_response(content: str = "ok", prompt: int = 10, completion: int = 5) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _gateway_config(
    *, per_run_tokens: int = 500_000, per_day_cost_cny: float = 50.0
) -> ModelsConfig:
    providers = {
        "solo": ProviderSpec(
            base_url="http://solo.local",
            api_key_env="SOLO_KEY",
            models={"solo-exec": ModelSpec(tier="executor", price_in=1.0, price_out=2.0)},
        )
    }
    return ModelsConfig(
        version="qa",
        providers=providers,
        routing={"executor": ["solo-exec"]},
        budget=BudgetSpec(per_run_tokens=per_run_tokens, per_day_cost_cny=per_day_cost_cny),
    )


def _make_gateway(
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
        client_factory=lambda spec: _FakeChatClient(spec.base_url, behavior),
    )


MSG = [{"role": "user", "content": "你好"}]


# ============================================================ 1. 状态机迁移表逐对枚举

def test_run_transition_table_exhaustive() -> None:
    """6x6 全枚举：文档 mermaid 边必须齐备；终态吸收；目标不得为 pending。"""
    documented_edges = {
        ("pending", "running"),
        ("running", "waiting_approval"),
        ("running", "paused"),
        ("running", "completed"),
        ("running", "failed"),
        ("waiting_approval", "running"),
        ("paused", "running"),
        ("failed", "running"),
    }
    for current in RUN_STATES:
        for target in RUN_STATES:
            legal = can_run_transition(current, target)
            if (current, target) in documented_edges:
                assert legal, f"文档边缺失: {current}->{target}"
            # completed 为吸收态：不得迁出
            if current == "completed":
                assert not legal, f"completed 不应迁出到 {target}"
            # 任何状态都不得迁回 pending
            if target == "pending":
                assert not legal, f"不应迁入 pending: {current}->pending"
            # 原地迁移非法
            if current == target:
                assert not legal, f"原地迁移应非法: {current}->{target}"


def test_run_cancel_extension_matches_transitions_table() -> None:
    """cancel 语义扩展：任意非终态可 -> failed（终态除外）。"""
    non_terminal = ["pending", "running", "waiting_approval", "paused"]
    for state in non_terminal:
        assert can_run_transition(state, "failed"), f"cancel 应允许 {state}->failed"
    assert not can_run_transition("completed", "failed")


def test_run_unknown_state_has_no_transition() -> None:
    assert not can_run_transition("bogus", "running")
    assert not can_run_transition("running", "bogus")


def test_stage_transition_table_exhaustive() -> None:
    expected = {
        ("pending", "running"),
        ("running", "completed"),
        ("running", "failed"),
    }
    for current in STAGE_STATES:
        for target in STAGE_STATES:
            legal = can_stage_transition(current, target)
            if (current, target) in expected:
                assert legal, f"合法 stage 迁移缺失: {current}->{target}"
            else:
                assert not legal, f"非法 stage 迁移被放行: {current}->{target}"


def test_ensure_transition_raises_state_conflict() -> None:
    from ymir.orchestrator.transitions import ensure_run_transition, ensure_stage_transition

    for bad in [("completed", "running"), ("running", "running"), ("pending", "completed")]:
        with pytest.raises(StateConflictError) as exc:
            ensure_run_transition(*bad)
        assert exc.value.biz_code == 1003
    with pytest.raises(StateConflictError):
        ensure_stage_transition("completed", "failed")


# ============================================================ 2. actions 状态矩阵

async def test_actions_on_completed_all_conflict_1003(client: AsyncClient) -> None:
    pid = (await client.post("/api/v1/projects", json={"name": "act"})).json()["data"]["id"]
    run_id = (
        await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": "x"})
    ).json()["data"]["run_id"]
    assert (await _wait_terminal(client, run_id))["status"] == "completed"

    for action in ("pause", "resume", "cancel", "retry-stage"):
        resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": action})
        assert resp.status_code == 409, action
        assert resp.json()["code"] == 1003, action


async def test_actions_unknown_run_returns_1002(client: AsyncClient) -> None:
    resp = await client.post(f"/api/v1/runs/{uuid.uuid4()}/actions", json={"action": "pause"})
    assert resp.status_code == 404
    assert resp.json()["code"] == 1002


async def test_actions_invalid_name_returns_1001(client: AsyncClient) -> None:
    pid = (await client.post("/api/v1/projects", json={"name": "act-bad"})).json()["data"]["id"]
    run_id = (
        await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": "x"})
    ).json()["data"]["run_id"]
    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "fly"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


# ============================================================ 3. SSE 边界

async def _collect(gen, sink: list[str]) -> None:
    async for frame in gen:
        sink.append(frame)


async def _wait_until(check, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(interval)


def _http_scope(
    path: str, query: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None
) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


def _start_asgi_request(app, scope: dict):
    request_queue: asyncio.Queue[dict] = asyncio.Queue()
    sent: list[dict] = []
    done = asyncio.Event()

    async def receive() -> dict:
        return await request_queue.get()

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        try:
            await app(scope, receive, send)
        finally:
            done.set()

    task = asyncio.create_task(run())
    request_queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    return task, request_queue, sent, done


def _body_text(sent: list[dict]) -> str:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    ).decode("utf-8")


async def test_sse_after_seq_zero_replays_all(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    from ymir.events.sse import stream_run_events

    run_id = await _new_run(session)
    publisher = EventPublisher(session_factory)
    for i in range(3):
        await publisher.emit(run_id, "stage.log", {"i": i})

    frames: list[str] = []
    consumer = asyncio.create_task(
        _collect(stream_run_events(publisher, run_id, after_seq=0, heartbeat=5.0), frames)
    )
    try:
        await _wait_until(lambda: len(frames) >= 3)
        ids = [f.splitlines()[0] for f in frames[:3]]
        assert ids == ["id: 1", "id: 2", "id: 3"]
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


async def test_sse_after_seq_beyond_max_replays_nothing_then_live(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """after_seq 越界（大于当前最大 seq）的边界行为（T02 QA P3 修复后）。

    修复后语义：去重水位 = 本次重放实际发出的最大 seq（无重放则为 0），
    after_seq 仅作重放查询下界。越界时重放为空，但后续实时帧不受抑制、
    正常送达。修复前该场景实时帧会被静默丢弃（首个到达帧为 keepalive）。
    """
    from ymir.events.sse import stream_run_events

    run_id = await _new_run(session)
    publisher = EventPublisher(session_factory)
    for i in range(3):
        await publisher.emit(run_id, "stage.log", {"i": i})

    frames: list[str] = []
    consumer = asyncio.create_task(
        _collect(stream_run_events(publisher, run_id, after_seq=10**9, heartbeat=5.0), frames)
    )
    try:
        await asyncio.sleep(0.15)
        assert frames == []  # 无历史重放
        await publisher.emit(run_id, "usage.updated", {"k": 1})  # seq=4 < after_seq
        await asyncio.sleep(0.2)
        assert any("id: 4" in f for f in frames)  # 修复后：实时帧不被越界基准抑制
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


async def test_sse_invalid_last_event_id_header_degrades_to_zero(
    temp_settings, engine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """端点级：非法 Last-Event-ID（非数字）应降级为 0 -> 全量重放。"""
    app = create_app(settings=temp_settings, engine=engine, session_factory=session_factory)
    async with session_factory() as s:
        project = await ProjectRepository(s).create(name="sse-bad-header")
        run = await RunRepository(s).create(project_id=project.id, requirement="x")
        await s.commit()
    publisher = app.state.events
    for i in range(3):
        await publisher.emit(run.id, "stage.log", {"i": i})

    scope = _http_scope(
        f"/api/v1/runs/{run.id}/events", headers=[(b"last-event-id", b"not-a-number")]
    )
    task, request_queue, sent, done = _start_asgi_request(app, scope)
    try:
        await _wait_until(lambda: "id: 3\n" in _body_text(sent))
        ids = [ln for ln in _body_text(sent).splitlines() if ln.startswith("id: ")]
        assert ids == ["id: 1", "id: 2", "id: 3"]
    finally:
        await request_queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=5)
    assert done.is_set()
    await app.state.orchestrator.shutdown()


async def test_sse_endpoint_unknown_run_returns_1002(
    temp_settings, engine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    app = create_app(settings=temp_settings, engine=engine, session_factory=session_factory)
    task, request_queue, sent, done = _start_asgi_request(
        app, _http_scope(f"/api/v1/runs/{uuid.uuid4()}/events")
    )
    try:
        await _wait_until(lambda: any(m["type"] == "http.response.start" for m in sent))
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 404
        body = json.loads(_body_text(sent))
        assert body["code"] == 1002
    finally:
        with contextlib.suppress(Exception):
            await request_queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=5)
    await app.state.orchestrator.shutdown()


# ============================================================ 4. seq 单调无空洞

async def test_seq_monotonic_no_gaps_after_full_run(
    client: AsyncClient, session: AsyncSession
) -> None:
    pid = (await client.post("/api/v1/projects", json={"name": "seq"})).json()["data"]["id"]
    run_id = (
        await client.post(f"/api/v1/projects/{pid}/runs", json={"requirement": "seq 校验"})
    ).json()["data"]["run_id"]
    assert (await _wait_terminal(client, run_id))["status"] == "completed"

    await session.commit()  # 结束可能的读事务，读最新
    events = (
        (await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.seq)))
        .scalars()
        .all()
    )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert len(events) > 0

    messages = (
        (
            await session.execute(
                select(Message).where(Message.run_id == run_id).order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [m.seq for m in messages] == list(range(1, len(messages) + 1))
    assert len(messages) == 4


async def test_seq_independent_between_runs(session_factory, session: AsyncSession) -> None:
    """不同 run 的 events.seq 各自从 1 起（唯一约束为 run_id+seq）。"""
    run_a = await _new_run(session)
    run_b = await _new_run(session)
    publisher = EventPublisher(session_factory)
    await publisher.emit(run_a, "stage.log", {})
    await publisher.emit(run_a, "stage.log", {})
    await publisher.emit(run_b, "stage.log", {})
    a = await publisher.list_after(run_a, 0)
    b = await publisher.list_after(run_b, 0)
    assert [e.seq for e in a] == [1, 2]
    assert [e.seq for e in b] == [1]


# ============================================================ 5. 预算边界

async def test_budget_boundary_equal_passes_just_over_blocks(
    session_factory, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLO_KEY", "test-solo")
    run_id = await _new_run(session)
    used = 1000
    session.add(
        ModelCall(
            run_id=run_id,
            provider="solo",
            model="solo-exec",
            tier="executor",
            prompt_tokens=used,
            completion_tokens=0,
            cost_cny=0.0,
        )
    )
    await session.commit()

    est = estimate_tokens(MSG)

    # 恰好等于上限（used + est == limit）-> 放行
    gw_ok = _make_gateway(
        session_factory,
        _gateway_config(per_run_tokens=used + est),
        lambda model, params: _fake_response(),
    )
    result = await gw_ok.chat(MSG, tier="executor", run_id=run_id)
    assert result.content == "ok"

    # 低于边界 1（used + est == limit + 1）-> 拦截
    gw_block = _make_gateway(
        session_factory,
        _gateway_config(per_run_tokens=used + est - 1),
        lambda model, params: _fake_response(),
    )
    with pytest.raises(BudgetExceededError):
        await gw_block.chat(MSG, tier="executor", run_id=run_id)


async def test_budget_per_run_tokens_zero_disables_limit(
    session_factory, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLO_KEY", "test-solo")
    run_id = await _new_run(session)
    # 预置超大已用量；per_run_tokens=0 表示禁用单 run 上限
    session.add(
        ModelCall(
            run_id=run_id,
            provider="solo",
            model="solo-exec",
            tier="executor",
            prompt_tokens=10_000_000,
            completion_tokens=0,
            cost_cny=0.0,
        )
    )
    await session.commit()

    gw = _make_gateway(
        session_factory,
        _gateway_config(per_run_tokens=0),
        lambda model, params: _fake_response(),
    )
    result = await gw.chat(MSG, tier="executor", run_id=run_id)
    assert result.content == "ok"


async def test_budget_day_cost_zero_disables_limit(
    session_factory, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLO_KEY", "test-solo")
    session.add(
        ModelCall(
            run_id=None,
            provider="solo",
            model="solo-exec",
            tier="executor",
            prompt_tokens=0,
            completion_tokens=0,
            cost_cny=999.0,
        )
    )
    await session.commit()
    gw = _make_gateway(
        session_factory,
        _gateway_config(per_day_cost_cny=0),
        lambda model, params: _fake_response(),
    )
    assert (await gw.chat(MSG, tier="executor")).content == "ok"


# ============================================================ 6. /models 脱敏

async def test_models_response_never_leaks_key(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-super-secret-qa-1234567890"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    resp = await client.get("/api/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert secret not in resp.text
    assert "DEEPSEEK_API_KEY" not in resp.text
    deepseek = next(p for p in body["data"]["providers"] if p["name"] == "deepseek")
    assert deepseek["key_ready"] is True  # 只暴露就绪布尔，不回传密钥
    assert "api_key" not in deepseek


async def test_models_key_ready_false_without_key(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/models")
    deepseek = next(p for p in resp.json()["data"]["providers"] if p["name"] == "deepseek")
    assert deepseek["key_ready"] is False


# ============================================================ 7. /usage

async def test_usage_scope_run_missing_run_id_1001(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/usage", params={"scope": "run"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_usage_scope_project_missing_project_id_1001(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/usage", params={"scope": "project"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_usage_project_scope_join_correctness(
    client: AsyncClient, session: AsyncSession
) -> None:
    proj_a = await ProjectRepository(session).create(name="usage-a")
    proj_b = await ProjectRepository(session).create(name="usage-b")
    run_a = await RunRepository(session).create(project_id=proj_a.id, requirement="a")
    run_b = await RunRepository(session).create(project_id=proj_b.id, requirement="b")
    await session.commit()

    def _call(run_id: str | None, tokens: int) -> ModelCall:
        return ModelCall(
            run_id=run_id,
            provider="solo",
            model="solo-exec",
            tier="executor",
            prompt_tokens=tokens,
            completion_tokens=0,
            cost_cny=0.0,
        )

    session.add_all([_call(run_a.id, 10), _call(run_a.id, 20), _call(run_b.id, 7)])
    session.add(_call(None, 999))  # 全局调用（无 run）不应计入 project scope
    await session.commit()

    resp_a = await client.get("/api/v1/usage", params={"scope": "project", "project_id": proj_a.id})
    data_a = resp_a.json()["data"]
    assert data_a["calls"] == 2
    assert data_a["total_tokens"] == 30

    resp_b = await client.get("/api/v1/usage", params={"scope": "project", "project_id": proj_b.id})
    assert resp_b.json()["data"]["total_tokens"] == 7

    global_data = (await client.get("/api/v1/usage", params={"scope": "global"})).json()["data"]
    assert global_data["calls"] == 4  # 含全局无 run 调用
    assert global_data["total_tokens"] == 10 + 20 + 7 + 999


async def test_usage_run_scope_with_no_calls_returns_zeros(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    resp = await client.get("/api/v1/usage", params={"scope": "run", "run_id": run_id})
    data = resp.json()["data"]
    assert data["calls"] == 0 and data["total_tokens"] == 0 and data["cost_cny"] == 0


# ============================================================ 8. messages summary 上限

def test_message_summary_over_limit_rejected() -> None:
    # 恰好 500 字通过
    MessageCreate(summary="x" * SUMMARY_MAX_LENGTH)
    # 501 字被拒（校验错误）
    with pytest.raises(ValidationError):
        MessageCreate(summary="x" * (SUMMARY_MAX_LENGTH + 1))


async def test_publisher_rejects_overlong_summary(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    events = EventPublisher(session_factory)
    publisher = MessagePublisher(session_factory, events)
    with pytest.raises(ValidationError):
        # MessageCreate 在进入 publish 前即被校验拦截
        await publisher.publish(run_id, MessageCreate(summary="y" * 600))


# ============================================================ 9. routing 边界

def test_routing_empty_config_resolve_raises_1006() -> None:
    config = _gateway_config()
    router = ModelRouter(ProviderRegistry(config), {})  # 空 routing
    with pytest.raises(ModelCallError) as exc:
        router.resolve("executor")
    assert exc.value.biz_code == 1006


def test_routing_skips_tier_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLO_KEY", "test-solo")
    providers = {
        "solo": ProviderSpec(
            base_url="http://solo.local",
            api_key_env="SOLO_KEY",
            models={
                "solo-plan": ModelSpec(tier="planner", price_in=1, price_out=1),
                "solo-exec": ModelSpec(tier="executor", price_in=1, price_out=1),
            },
        )
    }
    config = ModelsConfig(
        version="qa",
        providers=providers,
        routing={"executor": ["solo-plan", "solo-exec"]},  # solo-plan 属 planner
        budget=BudgetSpec(),
    )
    router = ModelRouter(ProviderRegistry(config), config.routing)
    assert [r.model for r in router.candidates("executor")] == ["solo-exec"]


def test_routing_skips_disabled_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLO_KEY", "test-solo")
    providers = {
        "solo": ProviderSpec(
            base_url="http://solo.local",
            api_key_env="SOLO_KEY",
            enabled=False,
            models={"solo-exec": ModelSpec(tier="executor", price_in=1, price_out=1)},
        )
    }
    config = ModelsConfig(
        version="qa",
        providers=providers,
        routing={"executor": ["solo-exec"]},
        budget=BudgetSpec(),
    )
    router = ModelRouter(ProviderRegistry(config), config.routing)
    assert router.candidates("executor") == []
    with pytest.raises(ModelCallError):
        router.resolve("executor")


# ============================================================ 10. 恢复边界

async def test_recovery_does_not_revive_waiting_approval(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = RunStatus.WAITING_APPROVAL.value
    await session.commit()

    engine = _make_engine(session_factory)
    recovered = await recover_runs(session_factory=session_factory, engine=engine)
    assert run_id not in recovered
    async with session_factory() as s:
        assert (await RunRepository(s).get(run_id)).status == "waiting_approval"
    await engine.shutdown()


async def test_recovery_skips_completed_stage_attempt_stays_one(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = RunStatus.RUNNING.value
    run.current_stage_id = "prd"
    session.add(
        StageExecution(
            run_id=run_id,
            stage_id="prd",
            role_id="pm",
            status=StageStatus.COMPLETED.value,
            attempt=1,
            output_refs=[{"name": "prd", "stub": True}],
        )
    )
    await session.commit()

    engine = _make_engine(session_factory)
    await recover_runs(session_factory=session_factory, engine=engine)

    deadline = time.monotonic() + 10
    while True:
        async with session_factory() as s:
            status = (await RunRepository(s).get(run_id)).status
        if status == "completed":
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"恢复后未完成: {status}")
        await asyncio.sleep(0.05)

    await session.commit()
    execs = (
        (
            await session.execute(
                select(StageExecution).where(
                    StageExecution.run_id == run_id, StageExecution.stage_id == "prd"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(execs) == 1 and execs[0].attempt == 1  # prd 未重跑
    await engine.shutdown()


# ============================================================ 11. 引擎异常路径

async def test_non_budget_exception_fails_stage_and_run(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    engine = _make_engine(session_factory, executor=_BoomExecutor())
    await engine.launch(run_id)

    deadline = time.monotonic() + 10
    while True:
        async with session_factory() as s:
            run = await RunRepository(s).get(run_id)
        if run.status == "failed":
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"非预算异常未致 run failed: {run.status}")
        await asyncio.sleep(0.05)

    assert "模拟非预算异常" in (run.error or "")
    await session.commit()
    execs = (
        (
            await session.execute(
                select(StageExecution).where(StageExecution.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert execs and execs[0].status == "failed"  # 阶段现场保留为 failed
    events = (
        (await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.seq)))
        .scalars()
        .all()
    )
    types = [e.type for e in events]
    assert "stage.failed" in types and "run.failed" in types
    assert "budget.exceeded" not in types  # 非预算异常不应误报预算
    await engine.shutdown()


# ============================================================ 12. 反例证伪

async def test_counterexample_no_key_gateway_is_unusable(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """证伪「无密钥时网关仍可调用」：候选为空 -> chat 抛 1006，不静默直连。"""
    from ymir.gateway.gateway import build_gateway

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    gateway = build_gateway(session_factory)
    assert gateway.router.candidates("executor") == []
    assert gateway.router.candidates("planner") == []
    with pytest.raises(ModelCallError) as exc:
        await gateway.chat(MSG, tier="executor")
    assert exc.value.biz_code == 1006


async def test_counterexample_health_reports_no_key(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """证伪「/health 的 gateway 字段硬编码为 ready」：无密钥时应为 no-key。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["data"]["gateway"] == "no-key"


async def test_counterexample_completed_run_leaves_no_active_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """证伪「run 完成后引擎任务仍在推进」：完成后推进任务应已退出。"""
    run_id = await _new_run(session)
    engine = _make_engine(session_factory)
    await engine.launch(run_id)

    deadline = time.monotonic() + 10
    while True:
        async with session_factory() as s:
            status = (await RunRepository(s).get(run_id)).status
        if status == "completed":
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"run 未完成: {status}")
        await asyncio.sleep(0.05)

    # 给 finally 清理一点时间后确认无活跃任务
    await asyncio.sleep(0.1)
    task = engine._tasks.get(run_id)
    assert task is None or task.done()
    await engine.shutdown()

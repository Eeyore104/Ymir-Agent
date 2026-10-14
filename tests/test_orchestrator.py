"""编排内核测试（T02）：状态机 / SOP 校验 / 引擎控制 / 断点恢复 / SSE / 预算。

纪律（§10.3-10）：stub 执行器不调 LLM（0 费用）；SSE 为端到端流式读取。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.common.errors import BudgetExceededError, StateConflictError
from ymir.events.bus import EventPublisher
from ymir.messages.bus import MessagePublisher
from ymir.models.entities import Event, Message, StageExecution
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor
from ymir.orchestrator.recovery import recover_runs
from ymir.orchestrator.transitions import (
    can_run_transition,
    can_stage_transition,
    ensure_run_transition,
    ensure_stage_transition,
)
from ymir.sop.loader import load_pipeline
from ymir.sop.models import PipelineDefinition
from ymir.sop.validator import SopValidator
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- 工具


async def _new_run(
    session: AsyncSession, *, mode: str = "batch", requirement: str = "测试需求"
) -> str:
    project = await ProjectRepository(session).create(name="orch-test")
    run = await RunRepository(session).create(
        project_id=project.id, requirement=requirement, mode=mode
    )
    await session.commit()
    return run.id


async def _get_run(session_factory: async_sessionmaker[AsyncSession], run_id: str):
    async with session_factory() as s:
        return await RunRepository(s).get(run_id)


async def _wait_status(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    statuses: set[str],
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout
    while True:
        run = await _get_run(session_factory, run_id)
        if run is not None and run.status in statuses:
            return run
        if time.monotonic() > deadline:
            current = run.status if run is not None else "missing"
            raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}（当前 {current}）")
        await asyncio.sleep(0.05)


def make_engine(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: Any = None,
    approval_hook: Any = None,
) -> OrchestrationEngine:
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)
    return OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor or StageExecutor(),
        approval_hook=approval_hook,
    )


class SlowExecutor(StageExecutor):
    """每阶段耗时 delay 秒（用于暂停/取消等控制测试）。"""

    def __init__(self, delay: float = 0.2) -> None:
        self._delay = delay

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        await asyncio.sleep(self._delay)
        return await super().execute(run=run, stage=stage, attempt=attempt)


class FlakyCodingExecutor(StageExecutor):
    """coding 阶段第一次失败、第二次成功（用于 retry-stage）。"""

    def __init__(self) -> None:
        self.coding_calls = 0

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        if stage.id == "coding":
            self.coding_calls += 1
            if self.coding_calls == 1:
                raise RuntimeError("模拟代码生成失败")
        return await super().execute(run=run, stage=stage, attempt=attempt)


class BudgetBoomExecutor(StageExecutor):
    """模拟网关预算拦截：直接抛 BudgetExceededError。"""

    async def execute(self, *, run, stage, attempt):  # type: ignore[override]
        raise BudgetExceededError("模拟预算超限")


# --------------------------------------------------------------------- 状态机


def test_run_transition_matrix() -> None:
    assert can_run_transition("pending", "running")
    assert can_run_transition("pending", "failed")  # cancel
    assert can_run_transition("running", "waiting_approval")
    assert can_run_transition("running", "paused")
    assert can_run_transition("running", "completed")
    assert can_run_transition("running", "failed")
    assert can_run_transition("waiting_approval", "running")
    assert can_run_transition("paused", "running")
    assert can_run_transition("failed", "running")  # retry-stage
    assert not can_run_transition("completed", "running")
    assert not can_run_transition("pending", "completed")
    assert not can_run_transition("completed", "failed")


def test_ensure_run_transition_raises_1003() -> None:
    with pytest.raises(StateConflictError) as exc_info:
        ensure_run_transition("completed", "running")
    assert exc_info.value.biz_code == 1003
    with pytest.raises(StateConflictError):
        ensure_run_transition("running", "running")  # 原地迁移视为冲突


def test_stage_transition_matrix() -> None:
    assert can_stage_transition("pending", "running")
    assert can_stage_transition("running", "completed")
    assert can_stage_transition("running", "failed")
    assert not can_stage_transition("completed", "running")
    with pytest.raises(StateConflictError):
        ensure_stage_transition("completed", "failed")


# --------------------------------------------------------------------- SOP 校验


def test_load_software_dev_pipeline_clean() -> None:
    definition = load_pipeline("software-dev", known_roles={"pm", "architect", "engineer", "qa"})
    assert definition.stage_ids() == ["prd", "architecture", "coding", "evaluation"]
    assert definition.next_stage("prd") == "architecture"
    assert definition.next_stage("evaluation") == "__end__"
    assert SopValidator(definition).errors() == []


def test_validator_catches_structural_errors() -> None:
    bad = PipelineDefinition.model_validate(
        {
            "id": "bad",
            "version": "1",
            "stages": [
                {"id": "a", "role": "pm", "inputs": ["requirement"], "outputs": ["x"]},
                # 重复 ID + 未注册角色
                {"id": "a", "role": "ghost", "outputs": ["y"]},
                # 不可达 + 输入不满足
                {"id": "b", "role": "qa", "inputs": ["missing"], "outputs": ["z"]},
                # 可达（a->c）但没有出边 -> 无法到达终点
                {"id": "c", "role": "qa", "inputs": ["x"], "outputs": ["w"]},
            ],
            "transitions": [
                {"from": "a", "on": "stage_completed", "to": "zzz"},  # 未知目标
                {"from": "a", "on": "stage_completed", "to": "__end__"},
                {"from": "a", "on": "stage_completed", "to": "c"},
            ],
        }
    )
    codes = {issue.code for issue in SopValidator(bad, known_roles={"pm"}).errors()}
    assert "duplicate-stage-id" in codes
    assert "transition-unknown-stage" in codes
    assert "stage-unreachable" in codes
    assert "stage-cannot-reach-end" in codes
    assert "input-unsatisfied" in codes
    assert "role-unknown" in codes


def test_validator_catches_bad_reject_target() -> None:
    bad = PipelineDefinition.model_validate(
        {
            "id": "bad2",
            "version": "1",
            "stages": [
                {
                    "id": "a",
                    "role": "pm",
                    "inputs": ["requirement"],
                    "outputs": ["x"],
                    "gate": {"name": "关卡", "on_reject_target": "nonexistent"},
                }
            ],
            "transitions": [{"from": "a", "on": "stage_completed", "to": "__end__"}],
        }
    )
    codes = {issue.code for issue in SopValidator(bad).errors()}
    assert "reject-target-missing" in codes


def test_validator_multi_root_warns() -> None:
    """多入口：除首个声明阶段外的入口必然不可达（error），同时给出 multi-root 警示。"""
    definition = PipelineDefinition.model_validate(
        {
            "id": "multi",
            "version": "1",
            "stages": [
                {"id": "a", "role": "pm", "inputs": ["requirement"], "outputs": ["x"]},
                {"id": "b", "role": "qa", "outputs": ["y"]},
            ],
            "transitions": [
                {"from": "a", "on": "stage_completed", "to": "__end__"},
                {"from": "b", "on": "stage_completed", "to": "__end__"},
            ],
        }
    )
    validator = SopValidator(definition)
    assert {issue.code for issue in validator.errors()} == {"stage-unreachable"}
    assert {issue.code for issue in validator.warnings()} == {"multi-root"}


def test_load_pipeline_missing_raises_404() -> None:
    from ymir.common.errors import NotFoundError

    with pytest.raises(NotFoundError):
        load_pipeline("no-such-pipeline")


# --------------------------------------------------------------------- 引擎集成（ASGI）


async def test_stub_pipeline_full_flow(client: AsyncClient, session: AsyncSession) -> None:
    """stub 四阶段空流水线：消息链 + 事件 seq 单调 + 阶段执行记录。"""
    created = await client.post("/api/v1/projects", json={"name": "flow"})
    project_id = created.json()["data"]["id"]
    resp = await client.post(
        f"/api/v1/projects/{project_id}/runs", json={"requirement": "做一个 CLI 工具"}
    )
    run_id = resp.json()["data"]["run_id"]

    deadline = time.monotonic() + 10
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] in ("completed", "failed"):
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"run 未完成: {data['status']}")
        await asyncio.sleep(0.05)
    assert data["status"] == "completed"

    # 消息链：pm -> architect -> engineer -> qa -> 广播
    messages = (
        (
            await session.execute(
                select(Message).where(Message.run_id == run_id).order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [m.seq for m in messages] == [1, 2, 3, 4]
    assert [m.from_role for m in messages] == ["pm", "architect", "engineer", "qa"]
    assert [m.to_role for m in messages] == ["architect", "engineer", "qa", None]
    assert all(m.type == "artifact" and m.content_refs for m in messages)

    # 事件：seq 从 1 起严格单调（无空洞），全流程类型齐全
    events = (
        (await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.seq)))
        .scalars()
        .all()
    )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    types = [e.type for e in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert types.count("stage.started") == 4
    assert types.count("stage.completed") == 4
    assert types.count("message.created") == 4
    assert "stage.failed" not in types

    # 阶段执行记录：4 阶段各 1 条 attempt=1 completed
    execs = (
        (
            await session.execute(
                select(StageExecution)
                .where(StageExecution.run_id == run_id)
                .order_by(StageExecution.started_at)
            )
        )
        .scalars()
        .all()
    )
    assert [e.stage_id for e in execs] == ["prd", "architecture", "coding", "evaluation"]
    assert all(e.attempt == 1 and e.status == "completed" for e in execs)


# --------------------------------------------------------------------- SSE 桥与端点
#
# 注意：httpx 0.28 的 ASGITransport 对响应全缓冲（ASGIResponseStream 一次性
# b"".join），无法消费无限 SSE 流 —— 端点级测试改用 ASGI 协议直连，
# 生成器级测试直接消费异步生成器。


async def _wait_until(check, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(interval)


async def _collect_frames(gen, sink: list[str]) -> None:
    async for frame in gen:
        sink.append(frame)


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
    """以 ASGI 协议直连启动一次请求；返回 (task, 请求队列, 已发送消息, 完成事件)。"""
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


async def test_stream_run_events_replay_then_live(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """生成器级：after_seq 重放缺口 + 订阅实时新事件。"""
    from ymir.events.sse import stream_run_events

    run_id = await _new_run(session)
    publisher = EventPublisher(session_factory)
    for i in range(3):
        await publisher.emit(run_id, "stage.log", {"i": i})

    frames: list[str] = []
    gen = stream_run_events(publisher, run_id, after_seq=1, heartbeat=5.0)
    consumer = asyncio.create_task(_collect_frames(gen, frames))
    try:
        await _wait_until(lambda: len(frames) >= 2)
        assert "id: 2" in frames[0]
        assert "id: 3" in frames[1]
        # 新事件实时送达（seq 连续递增）
        await publisher.emit(run_id, "usage.updated", {"k": 1})
        await _wait_until(lambda: len(frames) >= 3)
        assert "id: 4" in frames[2]
        assert '"type": "usage.updated"' in frames[2]
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


async def test_stream_run_events_keepalive(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """生成器级：无事件时按心跳间隔发出 keepalive 注释帧。"""
    from ymir.events.sse import stream_run_events

    run_id = await _new_run(session)
    publisher = EventPublisher(session_factory)
    frames: list[str] = []
    gen = stream_run_events(publisher, run_id, after_seq=0, heartbeat=0.05)
    consumer = asyncio.create_task(_collect_frames(gen, frames))
    try:
        await _wait_until(lambda: any(f.startswith(": keepalive") for f in frames), timeout=2.0)
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


async def test_sse_endpoint_streams_over_asgi(
    temp_settings, engine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """端点级：真实 ASGI 协议直连 —— 响应流式（未结束即有帧）、帧信封正确、断开即清理。"""
    from ymir.api.app import create_app

    app = create_app(settings=temp_settings, engine=engine, session_factory=session_factory)
    async with session_factory() as s:
        project = await ProjectRepository(s).create(name="sse-asgi")
        run = await RunRepository(s).create(project_id=project.id, requirement="x")
        await s.commit()
    publisher = app.state.events
    for i in range(3):
        await publisher.emit(run.id, "stage.log", {"i": i})

    task, request_queue, sent, done = _start_asgi_request(
        app, _http_scope(f"/api/v1/runs/{run.id}/events", b"after_seq=0")
    )
    try:
        await _wait_until(lambda: any(m["type"] == "http.response.start" for m in sent))
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 200
        # 流式证据：已有帧到达且响应尚未结束（全缓冲实现此时不可能有 body）
        await _wait_until(
            lambda: any(m["type"] == "http.response.body" for m in sent)
        )
        assert not done.is_set()
        text = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        ).decode("utf-8")
        ids = [ln for ln in text.splitlines() if ln.startswith("id: ")]
        assert ids == ["id: 1", "id: 2", "id: 3"]
        assert f'"run_id": "{run.id}"' in text
        assert '"type": "stage.log"' in text
    finally:
        # 客户端断开：SSE 生成器应被取消，app 调用返回
        await request_queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=5)
    assert done.is_set()
    await app.state.orchestrator.shutdown()


async def test_sse_endpoint_respects_last_event_id_header(
    temp_settings, engine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """端点级：Last-Event-ID 头等价于 after_seq（断线重连语义）。"""
    from ymir.api.app import create_app

    app = create_app(settings=temp_settings, engine=engine, session_factory=session_factory)
    async with session_factory() as s:
        project = await ProjectRepository(s).create(name="sse-header")
        run = await RunRepository(s).create(project_id=project.id, requirement="x")
        await s.commit()
    publisher = app.state.events
    for i in range(3):
        await publisher.emit(run.id, "stage.log", {"i": i})

    scope = _http_scope(
        f"/api/v1/runs/{run.id}/events",
        headers=[(b"last-event-id", b"2")],
    )
    task, request_queue, sent, done = _start_asgi_request(app, scope)
    try:

        def _body_text() -> str:
            return b"".join(
                m.get("body", b"") for m in sent if m["type"] == "http.response.body"
            ).decode("utf-8")

        await _wait_until(lambda: "id: 3\n" in _body_text())
        ids = [ln for ln in _body_text().splitlines() if ln.startswith("id: ")]
        assert ids == ["id: 3"]  # 只补 Last-Event-ID 之后的缺口
    finally:
        await request_queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=5)
    assert done.is_set()
    await app.state.orchestrator.shutdown()


async def test_run_actions_state_conflicts(client: AsyncClient) -> None:
    """已完成 run 上的控制动作 -> 1003；非法动作名 -> 1001。"""
    created = await client.post("/api/v1/projects", json={"name": "actions"})
    project_id = created.json()["data"]["id"]
    resp = await client.post(f"/api/v1/projects/{project_id}/runs", json={"requirement": "x"})
    run_id = resp.json()["data"]["run_id"]

    deadline = time.monotonic() + 10
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] == "completed":
            break
        if time.monotonic() > deadline:
            raise AssertionError("run 未完成")
        await asyncio.sleep(0.05)

    for action in ("pause", "resume", "cancel", "retry-stage"):
        resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": action})
        assert resp.status_code == 409, action
        assert resp.json()["code"] == 1003, action

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "fly"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


async def test_run_messages_endpoint(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects", json={"name": "msgs"})
    project_id = created.json()["data"]["id"]
    resp = await client.post(f"/api/v1/projects/{project_id}/runs", json={"requirement": "x"})
    run_id = resp.json()["data"]["run_id"]

    deadline = time.monotonic() + 10
    while True:
        data = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
        if data["status"] in ("completed", "failed"):
            break
        if time.monotonic() > deadline:
            raise AssertionError("run 未完成")
        await asyncio.sleep(0.05)

    body = (await client.get(f"/api/v1/runs/{run_id}/messages")).json()
    assert body["code"] == 0
    assert body["data"]["total"] == 4
    assert body["data"]["items"][0]["from_role"] == "pm"


# --------------------------------------------------------------------- 引擎控制（慢执行器）


async def test_pause_then_resume(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    engine = make_engine(session_factory, executor=SlowExecutor(0.15))
    await engine.launch(run_id)
    await asyncio.sleep(0.05)
    paused = await engine.request_pause(run_id)
    assert paused.status == "paused"

    # 等当前阶段执行完（0.15s）后再验证引擎确实停住（0.4s 内不应推进到下一阶段）
    await asyncio.sleep(0.4)
    run = await _get_run(session_factory, run_id)
    assert run.status == "paused"
    assert run.current_stage_id in (None, "prd")  # 未推进

    await engine.request_resume(run_id)
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"
    await engine.shutdown()


async def test_cancel_from_running(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    engine = make_engine(session_factory, executor=SlowExecutor(0.15))
    await engine.launch(run_id)
    await asyncio.sleep(0.05)
    cancelled = await engine.request_cancel(run_id)
    assert cancelled.status == "failed"
    assert cancelled.error == "cancelled by user"

    run = await _wait_status(session_factory, run_id, {"failed"})
    assert run.error == "cancelled by user"
    # 引擎任务应感知终态并退出
    task = engine._tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
    await engine.shutdown()


async def test_retry_stage_reruns_failed_stage_with_attempt_plus_one(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    engine = make_engine(session_factory, executor=FlakyCodingExecutor())
    await engine.launch(run_id)

    run = await _wait_status(session_factory, run_id, {"failed"})
    assert "coding" in (run.error or "")

    await engine.request_retry_stage(run_id)
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    execs = (
        (
            await session.execute(
                select(StageExecution)
                .where(StageExecution.run_id == run_id, StageExecution.stage_id == "coding")
                .order_by(StageExecution.attempt)
            )
        )
        .scalars()
        .all()
    )
    assert [(e.attempt, e.status) for e in execs] == [(1, "failed"), (2, "completed")]
    await engine.shutdown()


async def test_budget_exceeded_pauses_and_emits_events(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    engine = make_engine(session_factory, executor=BudgetBoomExecutor())
    await engine.launch(run_id)

    run = await _wait_status(session_factory, run_id, {"paused"})
    assert run.status == "paused"

    events = (
        (
            await session.execute(
                select(Event).where(Event.run_id == run_id).order_by(Event.seq)
            )
        )
        .scalars()
        .all()
    )
    types = [e.type for e in events]
    assert "budget.exceeded" in types
    assert "run.paused" in types
    assert "stage.failed" in types
    # 阶段执行记录最后一条为 failed
    last_exec = (
        (
            await session.execute(
                select(StageExecution)
                .where(StageExecution.run_id == run_id)
                .order_by(StageExecution.attempt.desc())
            )
        )
        .scalars()
        .first()
    )
    assert last_exec is not None and last_exec.status == "failed"
    await engine.shutdown()


async def test_approval_hook_parks_run_in_waiting(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """T04 挂点骨架：hook 返回 True 时阶段完成后进入 waiting_approval。"""
    run_id = await _new_run(session)

    async def always_require(_run, _stage) -> bool:
        return True

    engine = make_engine(session_factory, approval_hook=always_require)
    await engine.launch(run_id)
    run = await _wait_status(session_factory, run_id, {"waiting_approval"})
    assert run.status == "waiting_approval"
    await engine.shutdown()


# --------------------------------------------------------------------- 断点恢复


async def test_recovery_restarts_running_run_without_records(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = "running"  # 模拟进程被杀在启动瞬间
    await session.commit()

    engine = make_engine(session_factory)
    recovered = await recover_runs(session_factory=session_factory, engine=engine)
    assert run_id in recovered
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"
    await engine.shutdown()


async def test_recovery_reruns_interrupted_stage_with_attempt_plus_one(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = "running"
    run.current_stage_id = "prd"
    session.add(
        StageExecution(
            run_id=run_id, stage_id="prd", role_id="pm", status="running", attempt=1
        )
    )
    await session.commit()

    engine = make_engine(session_factory)
    await recover_runs(session_factory=session_factory, engine=engine)
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    await session.commit()  # 结束既有读事务，读取最新落库
    execs = (
        (
            await session.execute(
                select(StageExecution)
                .where(StageExecution.run_id == run_id, StageExecution.stage_id == "prd")
                .order_by(StageExecution.attempt)
            )
        )
        .scalars()
        .all()
    )
    assert [(e.attempt, e.status) for e in execs] == [(1, "failed"), (2, "completed")]
    assert "中断" in (execs[0].error or "")
    await engine.shutdown()


async def test_recovery_advances_past_completed_stage(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """进程被杀在「阶段已完成、run 未推进」的间隙 -> 补推进，不重跑该阶段。"""
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = "running"
    run.current_stage_id = "prd"
    session.add(
        StageExecution(
            run_id=run_id,
            stage_id="prd",
            role_id="pm",
            status="completed",
            attempt=1,
            output_refs=[{"name": "prd", "stub": True}],
        )
    )
    await session.commit()

    engine = make_engine(session_factory)
    await recover_runs(session_factory=session_factory, engine=engine)
    run = await _wait_status(session_factory, run_id, {"completed"})
    assert run.status == "completed"

    await session.commit()  # 结束既有读事务，读取最新落库
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
    assert len(execs) == 1  # prd 未被重跑
    assert execs[0].attempt == 1
    await engine.shutdown()


async def test_recovery_skips_waiting_approval(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """waiting_approval 不在启动恢复范围（T04 接审批决策），不被重新拉起。"""
    run_id = await _new_run(session)
    run = await RunRepository(session).get(run_id)
    run.status = "waiting_approval"
    await session.commit()

    engine = make_engine(session_factory)
    recovered = await recover_runs(session_factory=session_factory, engine=engine)
    assert run_id not in recovered
    run = await _get_run(session_factory, run_id)
    assert run.status == "waiting_approval"
    await engine.shutdown()

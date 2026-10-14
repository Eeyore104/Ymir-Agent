"""T03 集成测试：真实执行器全链路 / 截断单跑 / 网关预算超限真实链路。

纪律：假网关（0 真实模型调用）；沙箱用 LocalSandbox（不依赖 Docker）；
冒烟子进程为轻量命令（``python -c``）。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.config import PROJECT_ROOT
from ymir.events.bus import EventPublisher
from ymir.gateway.budget import BudgetGuard
from ymir.gateway.gateway import ChatResult, ChatUsage, ModelGateway
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
from ymir.models.entities import Event, Message, StageExecution
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox import LocalSandbox, SandboxService
from ymir.sop.loader import load_pipeline, truncate_pipeline
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- 假响应


PRD_JSON = json.dumps(
    {
        "title": "待办工具 PRD",
        "background": "需求背景",
        "goals": ["可增删改查"],
        "user_stories": [
            {
                "id": "US-1",
                "title": "管理待办",
                "as_a": "用户",
                "i_want": "记录待办",
                "so_that": "不遗忘",
                "acceptance_criteria": ["可以新增"],
            }
        ],
        "requirements": [
            {
                "id": "R-1",
                "title": "新增待办",
                "description": "支持创建",
                "priority": "P0",
                "acceptance_criteria": ["创建返回 id"],
            }
        ],
        "out_of_scope": [],
    },
    ensure_ascii=False,
)

DESIGN_JSON = json.dumps(
    {
        "title": "待办工具设计",
        "overview": "单文件 CLI",
        "modules": [{"name": "cli", "responsibility": "命令解析", "interfaces": ["main()"]}],
        "tech_stack": {"语言": "Python"},
        "directory_structure": "main.py",
        "api_drafts": [],
        "data_models": [],
    },
    ensure_ascii=False,
)

CODE_JSON = json.dumps(
    {
        "files": [
            {
                "path": "main.py",
                "content": "def main():\n    print('ok')\n\n\nif __name__ == '__main__':\n"
                "    main()",
            }
        ],
        "run_commands": ['python -c "print(\'smoke-ok\')"'],
        "notes": "最小示例",
    },
    ensure_ascii=False,
)

QA_JSON = json.dumps(
    {
        "verdict": "pass",
        "summary": "覆盖 P0 需求，冒烟通过",
        "findings": [{"severity": "low", "title": "无异常", "detail": "代码可用"}],
        "suggestions": ["补充边界测试"],
    },
    ensure_ascii=False,
)


class FakeGateway:
    """按调用顺序返回脚本响应的假网关。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        *,
        tier,
        run_id=None,
        stage_id=None,
        temperature=None,
        max_tokens=None,
        response_format=None,
    ) -> ChatResult:
        self.calls.append({"tier": str(tier), "stage_id": stage_id})
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(
            content=content,
            provider="fake",
            model="fake-model",
            tier=str(tier),
            usage=ChatUsage(prompt_tokens=10, completion_tokens=20),
            cost_cny=0.0001,
            latency_ms=3,
        )


# --------------------------------------------------------------------- 装配


def _build_engine(session_factory, tmp_path, gateway, *, definition_loader=None):
    events = EventPublisher(session_factory)
    messages = MessagePublisher(session_factory, events)
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    sandbox_service = SandboxService(
        session_factory=session_factory,
        sandbox=LocalSandbox(base_dir=tmp_path / "sandboxes"),
    )
    roles_dir = PROJECT_ROOT / "config" / "roles"
    runtime = RoleRuntime(
        gateway=gateway,
        registry=RoleRegistry(roles_dir),
        renderer=PromptRenderer(roles_dir / "prompts"),
    )
    executor = RoleStageExecutor(
        session_factory=session_factory,
        store=store,
        runtime=runtime,
        sandbox_service=sandbox_service,
        events=events,
        exec_timeout=60.0,
    )
    engine = OrchestrationEngine(
        session_factory=session_factory,
        events=events,
        messages=messages,
        executor=executor,
        definition_loader=definition_loader,
    )
    return engine, store


async def _new_run(
    session_factory: async_sessionmaker[AsyncSession], requirement: str = "做一个待办工具"
) -> tuple[str, str]:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="t03 集成")
        run = await RunRepository(session).create(project_id=project.id, requirement=requirement)
        await session.commit()
        return project.id, run.id


async def _wait_run(session_factory, run_id: str, statuses: set[str], timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while True:
        async with session_factory() as session:
            run = await RunRepository(session).get(run_id)
        if run is not None and run.status in statuses:
            return run
        if time.monotonic() > deadline:
            current = run.status if run is not None else "missing"
            raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}（当前 {current}）")
        await asyncio.sleep(0.1)


async def _wait_events(session_factory, run_id: str, types_needed: set[str], timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(Event).where(Event.run_id == run_id).order_by(Event.seq)
                    )
                )
                .scalars()
                .all()
            )
        if types_needed <= {row.type for row in rows}:
            return rows
        if time.monotonic() > deadline:
            raise AssertionError(f"事件未齐全：需要 {types_needed}")
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------- 全链路


async def test_full_pipeline_with_fake_llm(session_factory, tmp_path) -> None:
    """四角色真实执行器 + 假 LLM：产物版本 / 沙箱冒烟 / 消息链 / 事件 全落库。"""
    gateway = FakeGateway([PRD_JSON, DESIGN_JSON, CODE_JSON, QA_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway)
    project_id, run_id = await _new_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_run(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    # 1) 四个产物各 v1
    artifacts = await store.list_artifacts(project_id)
    assert {item.name for item in artifacts} == {"prd", "design", "code", "evaluation_report"}
    assert all(item.current_version == 1 for item in artifacts)

    # 2) 代码产物含沙箱冒烟报告（LocalSandbox 真实执行）
    loaded = await store.latest_by_name(project_id, "code")
    assert loaded is not None
    assert "smoke_report.json" in loaded.files
    smoke = json.loads(loaded.files["smoke_report.json"])
    assert smoke["all_ok"] is True
    assert "smoke-ok" in smoke["commands"][0]["stdout"]

    # 3) 阶段记录 / 消息链 / 事件
    async with session_factory() as session:
        stages = list(
            (
                await session.execute(
                    select(StageExecution).where(StageExecution.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        messages = list(
            (
                await session.execute(
                    select(Message).where(Message.run_id == run_id).order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [stage.stage_id for stage in stages] == ["prd", "architecture", "coding", "evaluation"]
    assert all(stage.status == "completed" for stage in stages)
    assert [message.from_role for message in messages] == ["pm", "architect", "engineer", "qa"]
    # 消息 content_refs 为产物引用（artifact_id@version 语义）
    assert messages[0].content_refs[0]["version"] == 1

    events = await _wait_events(session_factory, run_id, {"run.completed"})
    created = [event for event in events if event.type == "artifact.created"]
    assert len(created) == 4

    # 4) 每个角色按声明的 tier 路由（pm/architect=planner；engineer/qa=executor）
    assert [call["tier"] for call in gateway.calls] == [
        "planner",
        "planner",
        "executor",
        "executor",
    ]

    await engine.shutdown()


async def test_truncated_pipeline_runs_only_target(session_factory, tmp_path) -> None:
    """截断流水线（T03 单跑机制）：只执行目标阶段并正常收束为 completed。"""
    gateway = FakeGateway([PRD_JSON])
    definition = load_pipeline("software-dev")
    truncated = truncate_pipeline(definition, "prd")
    engine, store = _build_engine(
        session_factory, tmp_path, gateway, definition_loader=lambda _pid: truncated
    )
    project_id, run_id = await _new_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_run(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    artifacts = await store.list_artifacts(project_id)
    assert [item.name for item in artifacts] == ["prd"]
    assert len(gateway.calls) == 1  # 只调了一次（pm）


# --------------------------------------------------------------------- 预算链路（T02 遗留④）


def _tiny_budget_gateway(session_factory) -> ModelGateway:
    """真实网关 + 假客户端 + 极小预算（per_run_tokens=1，必然超限）。"""
    config = ModelsConfig(
        version="t03-budget-test",
        providers={
            "alpha": ProviderSpec(
                base_url="http://alpha.local",
                api_key_env="ALPHA_KEY",
                models={
                    "alpha-plan": ModelSpec(tier="planner", price_in=1.0, price_out=2.0),
                    "alpha-exec": ModelSpec(tier="executor", price_in=1.0, price_out=2.0),
                },
            )
        },
        routing={"planner": ["alpha-plan"], "executor": ["alpha-exec"]},
        budget=BudgetSpec(per_run_tokens=1, per_day_cost_cny=50.0),
    )
    registry = ProviderRegistry(config)
    recorder = UsageRecorder(session_factory)

    def _fake_client(spec):  # noqa: ANN001
        def _create(**params: Any):  # noqa: ANN202
            raise AssertionError("预算超限时不应触达客户端调用")

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

    return ModelGateway(
        registry=registry,
        router=ModelRouter(registry, config.routing),
        guard=BudgetGuard(recorder, config.budget),
        recorder=recorder,
        client_factory=_fake_client,
    )


async def test_budget_exceeded_real_gateway_chain(
    session_factory, tmp_path, monkeypatch
) -> None:
    """网关 BudgetExceeded -> 真实执行器 -> 引擎转 paused 并广播事件（完整链路）。"""
    monkeypatch.setenv("ALPHA_KEY", "test-key")
    gateway = _tiny_budget_gateway(session_factory)
    engine, _store = _build_engine(session_factory, tmp_path, gateway)
    _project_id, run_id = await _new_run(session_factory)

    await engine.launch(run_id)
    run = await _wait_run(session_factory, run_id, {"paused", "failed"})
    assert run.status == "paused", run.error

    events = await _wait_events(session_factory, run_id, {"budget.exceeded", "run.paused"})
    types = [event.type for event in events]
    assert "stage.failed" in types
    assert "budget.exceeded" in types
    await engine.shutdown()

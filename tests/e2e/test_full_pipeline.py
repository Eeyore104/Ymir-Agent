"""T05 全链路 e2e（假响应）：需求 → PRD → 架构 → 代码 → 评测。

纪律：LLM 全假（0 真实调用）；流水线沙箱用 LocalSandbox（不依赖 Docker）；
评测 harness 用假 judge + 假沙箱会话。验证产物版本 / 事件 / 四维报告入库 / 导出。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from contextlib import asynccontextmanager

from sqlalchemy import select

from ymir.artifacts.store import ArtifactStore
from ymir.config import PROJECT_ROOT
from ymir.evaluation import (
    ARCHITECTURE,
    CONVENTION,
    REQUIREMENT,
    RUNNABILITY,
    EvaluationHarness,
)
from ymir.evaluation.evaluators import JudgeOutcome
from ymir.evaluation.evaluators.architecture import _DesignJudgement
from ymir.evaluation.evaluators.convention import _ConventionJudgement
from ymir.evaluation.evaluators.requirement import _ItemJudgement, _RequirementJudgement
from ymir.events.bus import EventPublisher
from ymir.gateway.gateway import ChatResult, ChatUsage
from ymir.messages.bus import MessagePublisher
from ymir.models.entities import Event
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.role_executor import RoleStageExecutor
from ymir.roles.prompts import PromptRenderer
from ymir.roles.registry import RoleRegistry
from ymir.roles.runtime import RoleRuntime
from ymir.sandbox import LocalSandbox, SandboxService
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.storage.repositories import ProjectRepository, RunRepository

# --------------------------------------------------------------------- 假 LLM 响应

PRD_JSON = json.dumps(
    {
        "title": "待办工具 PRD",
        "background": "背景",
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
            {"path": "main.py", "content": "def main():\n    print('ok')\n"},
            {"path": "tests/test_main.py", "content": "def test_ok():\n    assert True\n"},
        ],
        "run_commands": ["python -c \"print('smoke-ok')\""],
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


class FakeJudge:
    """结构化假 judge（按 schema 返回脚本化结果）。"""

    async def judge(self, *, system: str, user: str, schema: type) -> JudgeOutcome:
        if schema is _RequirementJudgement:
            data = _RequirementJudgement(
                items=[
                    _ItemJudgement(
                        item_id="R-1",
                        score=4,
                        evidence=["main.py: main()"],
                        rationale="ok",
                    )
                ]
            )
        elif schema is _ConventionJudgement:
            data = _ConventionJudgement(score=4, evidence=["命名清晰"], rationale="ok")
        elif schema is _DesignJudgement:
            data = _DesignJudgement(score=4, evidence=["分层对齐"], rationale="ok")
        else:  # pragma: no cover
            data = schema()
        return JudgeOutcome(
            data=data,
            provider="fake",
            model="fake-judge",
            prompt_tokens=8,
            completion_tokens=16,
            cost_cny=0.0002,
        )


class FakeSandboxSession(SandboxSession):
    """假评测沙箱：可运行命令一律成功。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    @property
    def sandbox_id(self) -> str | None:
        return "fake-eval"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.RESTRICTED

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        return ExecResult(stdout="ok", stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class FakeSandboxProvider:
    """假评测沙箱 provider。"""

    def __init__(self) -> None:
        self.session_obj = FakeSandboxSession()

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        yield self.session_obj


# --------------------------------------------------------------------- 装配


def _build_engine(session_factory, tmp_path, gateway):
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
    )
    return engine, store


async def _new_run(session_factory, requirement: str) -> tuple[str, str]:
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="T05 e2e")
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


# --------------------------------------------------------------------- e2e


async def test_full_pipeline_then_evaluation(session_factory, tmp_path) -> None:
    """需求 → PRD → 架构 → 代码（假 LLM）→ 四维评测报告（假 judge/沙箱）全链路。"""
    gateway = FakeGateway([PRD_JSON, DESIGN_JSON, CODE_JSON, QA_JSON])
    engine, store = _build_engine(session_factory, tmp_path, gateway)
    project_id, run_id = await _new_run(session_factory, "做一个命令行待办工具")

    # 1) 流水线跑到完成
    await engine.launch(run_id)
    run = await _wait_run(session_factory, run_id, {"completed", "failed"})
    assert run.status == "completed", run.error

    artifacts = await store.list_artifacts(project_id)
    assert {item.name for item in artifacts} == {"prd", "design", "code", "evaluation_report"}

    async with session_factory() as session:
        rows = list(
            (await session.execute(select(Event).where(Event.run_id == run_id))).scalars().all()
        )
    types = {row.type for row in rows}
    assert "run.started" in types and "run.completed" in types and "artifact.created" in types
    # run.started 携带 mode（T5-5①）
    started = next(row for row in rows if row.type == "run.started")
    assert started.payload.get("mode") == "key_node"

    # 2) 对 code 产物做四维评测（假 judge + 假沙箱）
    harness = EvaluationHarness(
        store=store,
        judge=FakeJudge(),
        sandbox_provider=FakeSandboxProvider(),
        session_factory=session_factory,
    )
    report = await harness.run_suite("default", project_id=project_id)
    dims = {dim.name for dim in report.tasks[0].dimensions}
    assert dims == {REQUIREMENT, RUNNABILITY, CONVENTION, ARCHITECTURE}
    # 每维必须有证据链（验收点③）
    for dim in report.tasks[0].dimensions:
        assert dim.evidence, f"{dim.name} 缺证据链"

    # 3) 报告作为一等产物入库（type=report，带版本；与 qa 阶段同名产物叠版本）
    saved = await report.save(store, project_id=project_id)
    assert saved.artifact_type == "report"
    loaded = await store.latest_by_name(project_id, "evaluation_report")
    assert loaded is not None
    assert "evaluation_report.json" in loaded.files
    payload = json.loads(loaded.files["evaluation_report.json"])
    assert payload["suite_id"] == "default"
    assert {d["name"] for d in payload["tasks"][0]["dimensions"]} == dims

    # 4) 整库导出 zip（含 manifest + 各产物最新版本）
    blob = await store.export_zip(project_id)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert any(name.startswith("code/") for name in names)
    assert any(name.startswith("evaluation_report/") for name in names)

    await engine.shutdown()

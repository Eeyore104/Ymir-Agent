"""单跑与批量同源打分（v1.3 · T4.2，docs/11 §7.4 T4.2）。

**要防的裂缝**：v1.2 里「把 run 装配成 ``EvalTask``」有两份实现 ——
``HarnessEvaluationGate`` 内的完整版（模板 ``checks`` 覆盖 + ``layout`` + ``baseline_ref``）
与 ``BenchmarkRunner._row_from_fallback`` 自己拼的弱化版（只有套件 ``checks``）。
同一产物走"裁决"与走"批量补评"可能拿到**两套分数**，且没人报错。

v1.3 把装配收敛到 :func:`ymir.evaluation.run_task.build_task_for_run`，本文件用
**容差 0** 的逐项比对把它钉死：总分 / 可运行率 / 四维（名称+分数+权重+理由）全部相等。

测试纪律：零真实模型、零 Docker —— 沙箱是脚本化的假实现（全部检查通过），
因此分数完全由**装配结果**（checks / layout）决定，而不是由执行结果噪声决定。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.store import ArtifactStore
from ymir.evaluation.benchmark import BenchmarkOptions, BenchmarkRunner
from ymir.evaluation.evaluators.base import EvalTask
from ymir.evaluation.harness import EvaluationHarness
from ymir.evaluation.report import TaskOut
from ymir.evaluation.run_task import build_task_for_run, evaluate_run
from ymir.evaluation.suite import EvalSuite
from ymir.sandbox.base import ExecResult, NetworkPolicy, SandboxSession
from ymir.storage.repositories import ProjectRepository, RunRepository

SUITE_ID = "default"
TEMPLATE_ID = "fastapi-vue3"

#: 产物刻意「缺 web/src」（模板 required_dirs 之一）⇒ 有无 ``layout`` 会得到不同结构分，
#: 这正是 v1.2 裂缝的可观测点：装配漏掉 layout 就必然分数不一致。
CODE_FILES: dict[str, str] = {
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
    "pyproject.toml": "[project]\nname = 'app'\n",
    "requirements.txt": "fastapi>=0.115\n",
    "tests/test_app.py": "def test_ok() -> None:\n    assert True\n",
    "README.md": "# app\n",
    ".gitignore": "*.pyc\n",
}


class _PassingSession(SandboxSession):
    """脚本化假会话：默认全过；可指定「缺工具链」与「指定命令失败」。

    - ``missing_bins``：``command -v <bin>`` 返回 127 ⇒ 该检查 ``skipped``（T2.4 降级）；
    - ``fail_commands``：命令包含该子串 ⇒ exit 1（模拟构建失败，Q4 阻断项）。
    """

    def __init__(
        self,
        *,
        missing_bins: tuple[str, ...] = (),
        fail_commands: tuple[str, ...] = (),
    ) -> None:
        self.files: dict[str, str] = {}
        self.executed: list[str] = []
        self.missing_bins = missing_bins
        self.fail_commands = fail_commands

    @property
    def sandbox_id(self) -> str | None:
        return "fake-parity-1"

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy.NONE

    async def exec(
        self, cmd: str, *, timeout: float = 60.0, workdir: str | None = None
    ) -> ExecResult:
        self.executed.append(cmd)
        if cmd.startswith("command -v "):
            name = cmd[len("command -v ") :].strip()
            code = 127 if name in self.missing_bins else 0
            return ExecResult(stdout="", stderr="", exit_code=code, duration_ms=1)
        if any(fragment in cmd for fragment in self.fail_commands):
            return ExecResult(
                stdout="", stderr="build failed", exit_code=1, duration_ms=1
            )
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, path: str = ".") -> list[str]:
        return sorted(self.files)

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        return None


class _PassingProvider:
    """每次进入都给新会话（基线与合并树互不污染）。"""

    def __init__(
        self,
        *,
        missing_bins: tuple[str, ...] = (),
        fail_commands: tuple[str, ...] = (),
    ) -> None:
        self.missing_bins = missing_bins
        self.fail_commands = fail_commands
        self.sessions: list[_PassingSession] = []

    @asynccontextmanager
    async def session(self):  # noqa: ANN201
        session = _PassingSession(
            missing_bins=self.missing_bins, fail_commands=self.fail_commands
        )
        self.sessions.append(session)
        yield session


class _RecordingHarness:
    """包一层记录每次 ``evaluate_task`` 的 ``TaskOut``（批量路径内部不可见结果）。"""

    def __init__(self, inner: EvaluationHarness) -> None:
        self._inner = inner
        self.outcomes: list[TaskOut] = []

    async def evaluate_task(
        self,
        task: EvalTask,
        *,
        project_id: str | None,
        version_override: int | None = None,
        suite: EvalSuite | None = None,
    ) -> TaskOut:
        outcome = await self._inner.evaluate_task(
            task,
            project_id=project_id,
            version_override=version_override,
            suite=suite,
        )
        self.outcomes.append(outcome)
        return outcome

    def without_judge(self) -> _RecordingHarness:
        return _RecordingHarness(self._inner.without_judge())


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[ArtifactStore, str, Any, EvalTask]:
    """铺一个「带模板的 run + code 产物」，返回 ``(store, project_id, run, suite_task)``。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="parity")
        await session.commit()
        project_id = project.id
        run = await RunRepository(session).create(
            project_id=project_id,
            requirement="实现一个待办事项 REST API",
            mode="batch",
            context={
                "template": {"id": TEMPLATE_ID, "version": "1.0"},
                "benchmark": {"eval_suite": SUITE_ID, "eval_task": None},
            },
        )
        await session.commit()

    await store.save(
        project_id=project_id,
        name="code",
        artifact_type="code",
        files=CODE_FILES,
        run_id=run.id,
    )
    suite = EvalSuite.load(SUITE_ID)
    return store, project_id, run, suite.tasks[0]


def _details(outcome: TaskOut) -> dict[str, Any]:
    """把 ``TaskOut`` 拆成可逐项比对的结构（失败时便于打印两方明细）。"""
    return {
        "total_score": outcome.total_score,
        "runnability": outcome.runnability,
        "passed": outcome.passed,
        "dimensions": [
            {
                "name": dim.name,
                "score": dim.score,
                "weight": dim.weight,
                "rationale": dim.rationale,
            }
            for dim in outcome.dimensions
        ],
    }


async def test_single_run_matches_batch_fallback(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """同一产物：``evaluate_run``（单跑）== ``_row_from_fallback``（批量），容差 0。"""
    store, project_id, run, suite_task = await _seed(session_factory, tmp_path)
    harness = EvaluationHarness(store=store, sandbox_provider=_PassingProvider())

    single = await evaluate_run(harness, run, default_suite=SUITE_ID)

    recorder = _RecordingHarness(harness)
    runner = BenchmarkRunner(
        session_factory=session_factory,
        orchestrator=object(),  # 本用例只调打分方法，不推进引擎
        harness=recorder,  # type: ignore[arg-type]
        suite_id=SUITE_ID,
    )
    row = await runner._row_from_fallback(  # noqa: SLF001 - 被测私有入口（批量补评）
        suite_task,
        project_id,
        run,
        EvalSuite.load(SUITE_ID),
        BenchmarkOptions(suite_id=SUITE_ID, batch_id="parity"),
    )

    assert recorder.outcomes, "批量补评没有真正调用 harness（用例前置失效）"
    batch = recorder.outcomes[0]

    assert row["score_source"] == "fallback"
    assert row["final_total"] == pytest.approx(batch.total_score, abs=0)
    # 逐项相等（不一致时 pytest 会打印两方明细）
    assert _details(single) == _details(batch)
    assert single.total_score == batch.total_score
    assert single.runnability == batch.runnability
    assert [dim.name for dim in single.dimensions] == [dim.name for dim in batch.dimensions]


async def test_parity_covers_template_checks_and_layout(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """裂缝回归：批量路径**必须**带上模板 ``checks`` 与 ``layout``（v1.2 漏了）。"""
    store, project_id, run, _suite_task = await _seed(session_factory, tmp_path)

    batch_task, batch_suite = build_task_for_run(
        run, artifact_name="code", default_suite=SUITE_ID
    )
    # 模板 checks 覆盖套件 checks（fastapi-vue3 有 4 条后端 + 1 条前端构建，v1.3）
    assert [check.run for check in batch_task.checks] == [
        "python -m compileall -q app",
        "python -m pytest -q",
        "ruff check --ignore B008 .",
        'python -c "from app.main import app"',
        "fe-build web",
    ]
    # v1.3 Q3：scoring 也随装配注入（否则批量与单跑会算出两套 runnability）
    assert batch_task.scoring is not None and batch_task.scoring.enabled
    # layout 已注入（缺 web/src ⇒ 结构分会被模板规则扣分，与"无 layout"必然不同）
    assert batch_task.layout is not None
    assert batch_task.layout.required_dirs == ["app", "tests", "web/src"]
    assert batch_suite.id == SUITE_ID

    harness = EvaluationHarness(store=store, sandbox_provider=_PassingProvider())
    outcome = await evaluate_run(harness, run, default_suite=SUITE_ID)
    convention = outcome.dimension("convention")
    assert convention is not None
    # 模板结构规则命中（证据里出现 required_dirs 的目录名）
    assert any(
        ev.ref in {"app", "tests", "web/src"} for ev in convention.evidence
    ), [ev.ref for ev in convention.evidence]


async def test_no_template_run_keeps_v11_assembly(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """无模板 ⇒ ``layout=None`` + 套件 checks（v1.1 口径不变，两条路径同样相等）。"""
    store = ArtifactStore(session_factory, tmp_path / "artifacts")
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="parity-plain")
        await session.commit()
        project_id = project.id
        run = await RunRepository(session).create(
            project_id=project_id, requirement="写一个脚本", mode="batch"
        )
        await session.commit()
    await store.save(
        project_id=project_id, name="code", artifact_type="code", files=CODE_FILES
    )

    task, suite = build_task_for_run(run, default_suite=SUITE_ID)
    assert task.layout is None
    assert [check.run for check in task.checks] == [
        check.run for check in suite.tasks[0].checks
    ]

    harness = EvaluationHarness(store=store, sandbox_provider=_PassingProvider())
    outcome = await evaluate_run(harness, run, default_suite=SUITE_ID)
    assert outcome.total_score > 0
    assert outcome.baseline_runnability is None  # 非增量：不探测基线


# ==================================================== T2.3 / T2.4 / Q4：构建检查三态


async def test_build_check_passes_with_node(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """有 node 且构建成功：``fe-build web`` 计入 ``frontend_build`` 组。"""
    store, project_id, run, _task = await _seed(session_factory, tmp_path)
    harness = EvaluationHarness(store=store, sandbox_provider=_PassingProvider())
    outcome = await evaluate_run(harness, run, default_suite=SUITE_ID)

    assert outcome.skipped_checks == []
    assert outcome.blocking_failed == []
    build_dim = outcome.dimension("runnability")
    assert build_dim is not None
    assert any("fe-build web" in item.ref for item in build_dim.evidence)


async def test_missing_node_skips_build_without_blocking_or_deduction(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """T2.4 兜底：无 node ⇒ 构建检查 ``skipped``，**不阻断、不扣分**，报告标注环境缺失。"""
    store, project_id, run, _task = await _seed(session_factory, tmp_path)
    harness = EvaluationHarness(
        store=store, sandbox_provider=_PassingProvider(missing_bins=("node",))
    )
    outcome = await evaluate_run(harness, run, default_suite=SUITE_ID)

    skipped = {item.command: item.reason for item in outcome.skipped_checks}
    assert skipped.get("fe-build web") == "requires_missing:node"
    assert outcome.blocking_failed == []  # 环境缺失绝不阻断
    assert any("前端构建未执行" in warning for warning in outcome.warnings)
    # 构建组整组 skipped ⇒ 不进分母。后端 4 条全过 = 100；静态组 5 条规则里
    # 只有两条 "禁止依赖 jquery/bootstrap" 通过（产物没有 web/）= 40
    # ⇒ (10×100 + 10×40) / 20 = 70（若把 skipped 的构建组算进分母则会是 46.7，明显偏低）
    assert outcome.runnability == pytest.approx(70.0)


async def test_build_failure_is_blocking_but_scores_still_reported(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Q4：构建失败 ⇒ 阻断项（``passed=False``），但**分数照算照披露**。"""
    store, project_id, run, _task = await _seed(session_factory, tmp_path)
    harness = EvaluationHarness(
        store=store, sandbox_provider=_PassingProvider(fail_commands=("fe-build",))
    )
    outcome = await evaluate_run(harness, run, default_suite=SUITE_ID)

    assert outcome.blocking_failed == ["fe-build web"]
    assert outcome.passed is False  # 阻断项强制不可交付
    # 分数仍然算出来并披露（不是被清零）
    assert outcome.total_score > 0
    assert outcome.dimension("runnability") is not None


# ==================================================== T2.3 / T2.4 / Q4：构建检查三态

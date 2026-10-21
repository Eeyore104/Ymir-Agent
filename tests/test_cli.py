"""CLI 测试（v1.1 T4.3）：``scripts/run_action.py`` 的装配完整性与退出码语义。

覆盖两条此前完全没有测试守护的点（QA 实测暴露的真实缺陷）：

1. **装配完整性**：CLI 必须与 ``api/app.py`` 同构地装配「评测 harness + 回边栈」，
   否则 ``extend-repair`` 必然抛「回边控制器未装配」（T4.3 验收①）；
2. **退出码语义**：非法动作 -> argparse ``exit 2``；业务错误（run 不存在等）-> ``exit 1``。

纪律：临时 SQLite + stub 执行器，零真实模型、零 Docker；脚本按文件路径动态加载
（``scripts/`` 不是包，不为测试改 CLI 语义）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.config import PROJECT_ROOT, Settings
from ymir.models.base import Base, utcnow
from ymir.models.entities import StageExecution
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor
from ymir.storage import db as db_module
from ymir.storage.repositories import ProjectRepository, RunRepository

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_action.py"

HISTORY: list[dict[str, Any]] = [
    {"round": 0, "verdict": "fail", "total": 62.0, "runnability": 48.0, "artifact_version": 1},
    {"round": 1, "verdict": "fail", "total": 71.0, "runnability": 58.0, "artifact_version": 2},
]


def _load_cli() -> Any:
    """按文件路径加载 CLI 模块（``scripts/`` 非包，避免改 ``sys.path``）。"""
    spec = importlib.util.spec_from_file_location("ymir_run_action_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(tmp_path: Path) -> Settings:
    """隔离配置（临时 DB + 临时产物目录，互不污染）。"""
    return Settings(db_path=tmp_path / "ymir.db", artifacts_dir=tmp_path / "artifacts")


async def _init_db(settings: Settings) -> async_sessionmaker[AsyncSession]:
    eng = db_module.build_engine(settings)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = db_module.build_session_factory(eng)
    await eng.dispose()
    return factory


async def _seed_failed_run(
    settings: Settings, *, history: list[dict[str, Any]] | None = None
) -> str:
    """铺一个 failed + 非空 history 的 run（含 completed 阶段执行供回边作废）。"""
    factory = await _init_db(settings)
    async with factory() as session:
        project = await ProjectRepository(session).create(name="cli")
        run = await RunRepository(session).create(
            project_id=project.id, requirement="做一个 CLI 工具", mode="batch"
        )
        await session.commit()
        run_id = run.id
    async with factory() as session:
        row = await RunRepository(session).get(run_id)
        assert row is not None
        row.status = "failed"
        row.current_stage_id = "evaluation"
        row.error = "评测未达标"
        row.context = {
            **(row.context or {}),
            "repair": {
                "round": 2,
                "max_rounds": 2,
                "extra_rounds": 0,
                "stage": "coding",
                "history": list(HISTORY if history is None else history),
                "stop_reason": "rounds_exhausted",
                "accepted": None,
            },
        }
        for stage_id, role in (("coding", "engineer"), ("evaluation", "qa")):
            session.add(
                StageExecution(
                    run_id=run_id,
                    stage_id=stage_id,
                    role_id=role,
                    status="completed",
                    attempt=2,
                    started_at=utcnow(),
                )
            )
        await session.commit()
    return run_id


async def _read_run(settings: Settings, run_id: str) -> Any:
    factory = db_module.build_session_factory(db_module.build_engine(settings))
    async with factory() as session:
        return await RunRepository(session).get(run_id)


def test_run_action_invalid_action_exits_2() -> None:
    """非法动作 -> argparse ``exit 2``（不是业务错误 1，也不是 1001）。"""
    cli = _load_cli()

    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["some-run-id", "--action", "fly"])

    assert excinfo.value.code == 2


async def test_build_orchestrator_wires_repair_stack(tmp_path: Path) -> None:
    """装配护栏：CLI 的编排引擎必须同时拿到 ``eval_gate`` 与 ``repair``。

    这条直接锁住 QA 发现的缺陷根因（只传 executor 会漏掉回边栈，
    ``extend-repair`` 就 100% 抛「回边控制器未装配」）。
    """
    cli = _load_cli()
    settings = _settings(tmp_path)

    engine, orchestrator = await cli.build_orchestrator(
        settings, executor=StageExecutor()
    )
    try:
        assert orchestrator._eval_gate is not None  # noqa: SLF001 - 装配护栏，直读注入位
        assert orchestrator._repair is not None  # noqa: SLF001
    finally:
        await orchestrator.shutdown()
        await engine.dispose()


async def test_run_action_extend_repair_uses_repair_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端：``run_main`` 执行 extend-repair -> exit 0，且 run 进入 coding 重跑。

    用 ``_spawn`` 记录式替身阻断进程内推进任务（保证零模型调用 + 确定性），
    同时断言**唤醒确实发生**——否则「装配正确但根本不 wake」的回归会让 run 永远
    停在 running、没人推进（这是 extend-repair 语义的一部分）。

    另断言 CLI 的**输出契约**（stdout 含 ``status: running``）：只有 exit code 时，
    「不报错但什么都没做」的实现仍可能蒙混过关。
    """
    cli = _load_cli()
    settings = _settings(tmp_path)
    run_id = await _seed_failed_run(settings)
    spawned: list[str] = []
    monkeypatch.setattr(
        OrchestrationEngine, "_spawn", lambda self, rid: spawned.append(rid)
    )

    code = await cli.run_main(
        cli.parse_args(
            [run_id, "--action", "extend-repair", "--db", str(settings.db_path)]
        )
    )

    assert code == 0
    assert spawned == [run_id]  # 必须唤醒推进任务
    out = capsys.readouterr().out
    assert "action: extend-repair" in out
    assert "status: running" in out  # T4.3 遗留：CLI 输出契约
    run = await _read_run(settings, run_id)
    assert run is not None
    assert run.status == "running"
    assert run.current_stage_id == "coding"  # 必须回 coding，而不是从 evaluation 重跑
    repair = dict(run.context or {})["repair"]
    assert repair["extra_rounds"] == 1
    assert repair["max_rounds"] == 3
    assert repair["stop_reason"] is None


async def test_run_action_accept_best_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：``run_main`` 执行 accept-best -> exit 0 + run completed + accepted 落库。"""
    cli = _load_cli()
    settings = _settings(tmp_path)
    run_id = await _seed_failed_run(settings)
    spawned: list[str] = []
    monkeypatch.setattr(
        OrchestrationEngine, "_spawn", lambda self, rid: spawned.append(rid)
    )

    code = await cli.run_main(
        cli.parse_args(
            [run_id, "--action", "accept-best", "--db", str(settings.db_path), "--by", "qa"]
        )
    )

    assert code == 0
    assert spawned == [run_id]  # 终态也要唤醒（挂起中的任务需醒来退出）
    run = await _read_run(settings, run_id)
    assert run is not None
    assert run.status == "completed"
    accepted = dict(run.context or {})["repair"]["accepted"]
    assert accepted["version"] == 2  # total=71.0
    assert accepted["accepted_by"] == "qa"  # --by 真正生效


async def test_run_action_unknown_run_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """业务错误（run 不存在）-> ``exit 1``（YmirError 被捕获，不抛栈）。"""
    cli = _load_cli()
    settings = _settings(tmp_path)
    await _init_db(settings)  # 有表但无该 run
    monkeypatch.setattr(OrchestrationEngine, "_spawn", lambda self, _rid: None)

    code = await cli.run_main(
        cli.parse_args(
            ["no-such-run", "--action", "accept-best", "--db", str(settings.db_path)]
        )
    )

    assert code == 1

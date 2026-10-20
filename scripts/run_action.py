"""人工介入 CLI（v1.1 T4.3，docs/07 §4.5.3 B-07）。

对**修复耗尽/预算中断**后的 run 执行人工三动作（仅 CLI/API）：

  # 接受最优未达标版本（failed -> completed，落 context.repair.accepted）
  uv run python scripts/run_action.py <run_id> --action accept-best

  # 追加 1 轮修复（failed -> running，先过预算预检）
  uv run python scripts/run_action.py <run_id> --action extend-repair

  # 终止（复用既有 cancel 语义：保留现场）
  uv run python scripts/run_action.py <run_id> --action cancel

纪律：本脚本只做「动作发起 + 结果打印」，不做业务判定；预算预检、状态迁移、
版本作废与事件发射全部在编排引擎内完成（同一个来源，避免 CLI 与 API 行为漂移）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from ymir.bootstrap import build_execution_stack, build_repair_stack
from ymir.common.errors import YmirError
from ymir.common.logging import setup_logging
from ymir.config import Settings, get_settings, load_env_file
from ymir.evaluation import build_eval_harness
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.orchestrator.executor import StageExecutor
from ymir.storage.db import build_engine, build_session_factory

#: 合法动作（与 ``api/routers/runs.py::RUN_ACTIONS`` 保持一致）
ACTIONS: tuple[str, ...] = (
    "pause",
    "resume",
    "cancel",
    "retry-stage",
    "accept-best",
    "extend-repair",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ymir-agent 人工介入（accept-best / extend-repair / cancel 等）"
    )
    parser.add_argument("run_id", help="目标 run ID")
    parser.add_argument(
        "--action",
        required=True,
        choices=list(ACTIONS),
        help="动作（六选一）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "数据库文件路径（缺省取 settings.db_path，通常为 data/ymir.db）；"
            "Windows 上请给盘符绝对路径（如 D:/ymir/data/ymir.db），"
            "POSIX 风格 /tmp/x.db 会被解析成当前盘根目录"
        ),
    )
    parser.add_argument("--by", default="user", help="操作人标识（审计用，默认 user）")
    return parser.parse_args(argv)


async def build_orchestrator(
    settings: Settings, *, executor: StageExecutor | None = None
) -> tuple[AsyncEngine, OrchestrationEngine]:
    """按 ``settings`` 装配「执行栈 + 评测 harness + 回边栈 + 编排引擎」。

    与 ``api/app.py`` 的装配保持**同构**（唯一差别是命令行进程不做 lifespan 恢复）：
    缺少回边栈会让 ``extend-repair`` 必然抛「回边控制器未装配」（T4.3 验收①），
    因此 db / artifacts 路径必须同时作用于 harness 与产物库（都来自同一个 settings）。

    ``executor`` 仅用于测试注入 stub（CLI 语义不变：缺省仍是真实角色执行器）。
    """
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    stack = build_execution_stack(settings, session_factory, executor=executor)
    harness = build_eval_harness(
        settings,
        stack.artifact_store,
        gateway=stack.gateway,
        session_factory=session_factory,
    )
    repair_stack = build_repair_stack(
        session_factory=session_factory,
        events=stack.event_publisher,
        messages=stack.message_publisher,
        harness=harness,
        store=stack.artifact_store,
        per_run_tokens=stack.gateway.registry.config.budget.per_run_tokens,
    )
    orchestrator = OrchestrationEngine(
        session_factory=session_factory,
        events=stack.event_publisher,
        messages=stack.message_publisher,
        executor=stack.executor,
        eval_gate=repair_stack.gate,
        repair=repair_stack.controller,
    )
    return engine, orchestrator


async def run_main(args: argparse.Namespace) -> int:
    load_env_file()
    settings = get_settings()
    if args.db:
        settings = settings.model_copy(update={"db_path": Path(args.db)})
    setup_logging(debug=settings.debug)

    engine, orchestrator = await build_orchestrator(settings)
    try:
        handlers = {
            "pause": orchestrator.request_pause,
            "resume": orchestrator.request_resume,
            "cancel": orchestrator.request_cancel,
            "retry-stage": orchestrator.request_retry_stage,
            "accept-best": lambda rid: orchestrator.request_accept_best(
                rid, accepted_by=args.by
            ),
            "extend-repair": orchestrator.request_extend_repair,
        }
        run = await handlers[args.action](args.run_id)
        print(f"run_id: {run.id}")
        print(f"action: {args.action}")
        print(f"by: {args.by}")
        print(f"status: {run.status}")
        if run.error:
            print(f"error: {run.error}")
        return 0
    except YmirError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    finally:
        await orchestrator.shutdown()
        await engine.dispose()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_main(args))
    except KeyboardInterrupt:
        print("\n[interrupt] 已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

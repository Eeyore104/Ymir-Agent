"""T03 联调脚本：命令行单跑角色（默认 PM 出 PRD v1）。

用法（仓库根目录，发真实 DeepSeek 调用；确保 ``.env`` 已配置 DEEPSEEK_API_KEY）：

  # 单跑 PM：需求 -> PRD 产物 v1 入库 + 消息/事件/用量全落库
  uv run python scripts/run_single_role.py --requirement "做一个命令行待办清单工具"

  # 跑到指定角色为止（自动含上游链，engineer 阶段含沙箱冒烟）
  uv run python scripts/run_single_role.py --requirement "..." --role engineer

  # 演练（不调模型）：打印将执行的阶段链与模型档位
  uv run python scripts/run_single_role.py --requirement "x" --role qa --dry-run

原理：以目标角色阶段为终点对流水线做「截断」（原定义不改），经
``definition_loader`` 注入引擎——走标准编排链路（阶段记录 / 消息 / 事件 /
用量计量全落库），跑到终点 run 置 completed。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from sqlalchemy import select

from ymir.bootstrap import build_execution_stack
from ymir.common.logging import setup_logging
from ymir.config import PROJECT_ROOT, get_settings, load_env_file
from ymir.models.entities import Message, ModelCall, StageExecution
from ymir.models.enums import RunStatus
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.roles.registry import RoleRegistry
from ymir.sop.loader import load_pipeline, truncate_pipeline
from ymir.sop.models import PipelineDefinition, StageSpec
from ymir.storage.db import build_engine, build_session_factory
from ymir.storage.repositories import ProjectRepository, RunRepository

TERMINAL = (RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.PAUSED.value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ymir-agent 单跑角色（T03 联调脚本）")
    parser.add_argument(
        "--role",
        default="pm",
        choices=["pm", "architect", "engineer", "qa"],
        help="目标角色（默认 pm；自动含上游链一起执行）",
    )
    parser.add_argument("--requirement", required=True, help="原始需求文本")
    parser.add_argument("--project-id", default=None, help="复用已有项目 ID")
    parser.add_argument("--project-name", default=None, help="新建项目名（默认时间戳命名）")
    parser.add_argument("--timeout", type=float, default=900.0, help="等待终态超时（秒）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印阶段链与档位，不调模型")
    return parser.parse_args()


def find_stage_by_role(definition: PipelineDefinition, role_id: str) -> StageSpec:
    for stage in definition.stages:
        if stage.role == role_id:
            return stage
    raise SystemExit(f"流水线 {definition.id} 中没有角色 {role_id} 的阶段")


def print_dry_run(truncated: PipelineDefinition) -> None:
    registry = RoleRegistry(PROJECT_ROOT / "config" / "roles")
    print("=" * 72)
    print(f"dry-run：流水线 {truncated.id}（截断）阶段链")
    for stage in truncated.stages:
        role = registry.get(stage.role)
        gate = f" gate={stage.gate.name}" if stage.gate else ""
        sandbox = " sandbox=on" if stage.use_sandbox else ""
        print(
            f"  [{stage.id}] 角色={role.id}（{role.name}）"
            f" tier={role.tier.value} 输出={stage.outputs}{gate}{sandbox}"
        )
    print("注：dry-run 不发起任何模型调用。真实运行将走网关分级路由（见 config/models.yaml）。")
    print("=" * 72)


async def resolve_project(session_factory, args: argparse.Namespace) -> str:
    async with session_factory() as session:
        repo = ProjectRepository(session)
        if args.project_id:
            project = await repo.get(args.project_id)
            if project is None:
                raise SystemExit(f"项目不存在: {args.project_id}")
            return project.id
        name = args.project_name or time.strftime("单跑演示-%m%d-%H%M%S")
        project = await repo.create(name=name, description="T03 run_single_role.py 创建")
        await session.commit()
        print(f"[project] {project.id}  {project.name}")
        return project.id


async def wait_terminal(session_factory, run_id: str, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    last_mark = ""
    while True:
        async with session_factory() as session:
            run = await RunRepository(session).get(run_id)
        if run is None:
            raise SystemExit(f"run 不存在: {run_id}")
        if run.status in TERMINAL:
            return run
        mark = f"{run.status}:{run.current_stage_id}"
        if mark != last_mark:
            print(f"[wait] 状态={run.status} 当前阶段={run.current_stage_id}")
            last_mark = mark
        if time.monotonic() > deadline:
            print(f"[warn] 等待超时（{timeout:.0f}s），当前状态 {run.status}")
            return run
        await asyncio.sleep(1.0)


async def print_report(session_factory, run_id: str, elapsed: float) -> None:
    async with session_factory() as session:
        run = await RunRepository(session).get(run_id)
        stages = (
            (
                await session.execute(
                    select(StageExecution)
                    .where(StageExecution.run_id == run_id)
                    .order_by(StageExecution.attempt.asc(), StageExecution.started_at.asc())
                )
            )
            .scalars()
            .all()
        )
        messages = (
            (
                await session.execute(
                    select(Message).where(Message.run_id == run_id).order_by(Message.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        calls = (
            (
                await session.execute(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id)
                    .order_by(ModelCall.created_at)
                )
            )
            .scalars()
            .all()
        )

    print("=" * 72)
    assert run is not None  # noqa: S101 - 调用方持有 run_id
    print(f"run 状态：{run.status}    用时：{elapsed:.1f}s")
    if run.error:
        print(f"错误：{run.error}")

    print("- 阶段执行")
    for stage in stages:
        refs = ", ".join(
            f"{ref.get('name')} v{ref.get('version')}"
            for ref in (stage.output_refs or [])
            if isinstance(ref, dict)
        )
        print(
            f"  [{stage.stage_id}] 角色={stage.role_id} 第{stage.attempt}次"
            f" {stage.status}  {refs}"
        )

    print("- 消息链")
    for message in messages:
        target = message.to_role or "（广播）"
        print(
            f"  #{message.seq} [{message.type}] {message.from_role} -> {target}："
            f"{message.summary}"
        )

    print("- 模型用量")
    for call in calls:
        tokens = call.prompt_tokens + call.completion_tokens
        print(
            f"  {call.provider}/{call.model} [{call.tier}] {tokens} tokens"
            f"  ¥{call.cost_cny:.6f}  {call.latency_ms}ms"
        )
    total_cost = sum(call.cost_cny for call in calls)
    total_tokens = sum(call.prompt_tokens + call.completion_tokens for call in calls)
    print(f"  合计：{len(calls)} 次调用 / {total_tokens} tokens / ¥{total_cost:.6f}")
    print("=" * 72)


async def run_main(args: argparse.Namespace) -> int:
    load_env_file()  # 注入 .env（DEEPSEEK_API_KEY 等）
    settings = get_settings()
    setup_logging(debug=settings.debug)
    db_engine = build_engine(settings)
    session_factory = build_session_factory(db_engine)
    try:
        definition = load_pipeline(settings.default_pipeline_id)
        target_stage = find_stage_by_role(definition, args.role)
        truncated = truncate_pipeline(definition, target_stage.id)

        if args.dry_run:
            print_dry_run(truncated)
            return 0

        stack = build_execution_stack(settings, session_factory)
        orchestrator = OrchestrationEngine(
            session_factory=session_factory,
            events=stack.event_publisher,
            messages=stack.message_publisher,
            executor=stack.executor,
            definition_loader=lambda _pid: truncated,
        )

        project_id = await resolve_project(session_factory, args)
        async with session_factory() as session:
            run = await RunRepository(session).create(
                project_id=project_id,
                requirement=args.requirement,
                pipeline_id=definition.id,
            )
            await session.commit()
            run_id = run.id

        chain_text = " -> ".join(f"{stage.id}({stage.role})" for stage in truncated.stages)
        print(f"[run] {run_id}  阶段链：{chain_text}")
        started = time.perf_counter()
        await orchestrator.launch(run_id)
        final = await wait_terminal(session_factory, run_id, args.timeout)
        elapsed = time.perf_counter() - started
        await print_report(session_factory, run_id, elapsed)
        await orchestrator.shutdown()
        return 0 if final.status == RunStatus.COMPLETED.value else 1  # type: ignore[attr-defined]
    finally:
        await db_engine.dispose()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_main(args))
    except KeyboardInterrupt:
        print("\n[interrupt] 已中断（run 现场保留，可重启服务由恢复机制续跑）")
        return 130


if __name__ == "__main__":
    sys.exit(main())

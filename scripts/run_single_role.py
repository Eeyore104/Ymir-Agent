"""T03 联调脚本：命令行单跑角色（默认 PM 出 PRD v1）。

用法（仓库根目录，发真实 DeepSeek 调用；确保 ``.env`` 已配置 DEEPSEEK_API_KEY）：

  # 单跑 PM：需求 -> PRD 产物 v1 入库 + 消息/事件/用量全落库
  uv run python scripts/run_single_role.py --requirement "做一个命令行待办清单工具"

  # 跑到指定角色为止（自动含上游链，engineer 阶段含沙箱冒烟）
  uv run python scripts/run_single_role.py --requirement "..." --role engineer

  # 演练（不调模型）：打印将执行的阶段链与模型档位
  uv run python scripts/run_single_role.py --requirement "x" --role qa --dry-run

  # 跑完顺带出一份四维分数（v1.3 T4.1：与 gate / 批量补评同一装配点）
  uv run python scripts/run_single_role.py --requirement "..." --role engineer --evaluate

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

from ymir.artifacts.store import BASELINE_TYPE, ArtifactStore
from ymir.bootstrap import build_execution_stack
from ymir.common.errors import NotFoundError
from ymir.common.logging import setup_logging
from ymir.config import PROJECT_ROOT, get_settings, load_env_file
from ymir.evaluation.harness import build_eval_harness
from ymir.evaluation.report import TaskOut
from ymir.evaluation.run_task import evaluate_run
from ymir.gateway.usage import UsageRecorder
from ymir.models.entities import DEFAULT_TENANT_ID, Message, ModelCall, StageExecution
from ymir.models.enums import RunStatus
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.roles.registry import RoleRegistry
from ymir.sop.loader import load_pipeline, truncate_pipeline
from ymir.sop.models import PipelineDefinition, StageSpec
from ymir.storage.db import build_engine, build_session_factory
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates import load_template

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
    # v1.2（T5.1 真跑场景）：模板与基线都是 run 的**运行参数**，只写引用进 run.context，
    # 绝不进 stage.inputs（sop 校验器会否决未知键）。语义与 ``POST /runs`` 一致。
    parser.add_argument(
        "--template-id",
        default=None,
        help="v1.2 模板 ID（fastapi-vue3 / python-cli）；缺省 = 通用模式（零模板）",
    )
    parser.add_argument(
        "--baseline-artifact-id",
        default=None,
        help="v1.2 基线产物 ID；指定即进入增量（patch）模式",
    )
    parser.add_argument(
        "--baseline-version",
        type=int,
        default=None,
        help="基线产物版本（缺省取最新）",
    )
    # HITL 模式（docs §3.8）。默认 key_node 保持 T03 联调脚本原行为；
    # 无人值守的真跑（T5.1 四场景）必须用 batch，否则会在三道审批 gate 上挂到超时。
    parser.add_argument(
        "--mode",
        default="key_node",
        choices=["key_node", "batch", "debug"],
        help="HITL 模式（key_node=三关审批 / batch=全自动 / debug=全步骤人审）",
    )
    # v1.3（T4.1）：终态后按 harness 口径补一次四维打分（**附加信息，不参与 verdict**）
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="run 终态后补一次四维打分（与 gate / 批量同源；不影响 verdict）",
    )
    parser.add_argument(
        "--suite",
        default="default",
        help="评测套件 id（默认 default；仅 --evaluate 时生效）",
    )
    return parser.parse_args()


async def resolve_baseline_ref(
    store: ArtifactStore,
    *,
    project_id: str | None,
    artifact_id: str,
    version: int | None,
) -> dict[str, object]:
    """校验基线引用并返回**四字段**快照（与 ``POST /runs`` 的 ``_resolve_baseline`` 同形）。

    ★ v1.3.1（T6.3-C）修复的裂缝：旧实现只写 ``{"artifact_id", "version"}``，而
    「另存为新基线」端点要做**三方指纹比对**，缺 ``content_hash`` 直接 1003
    ⇒ 头号能力在 CLI 入口上跑不通（只有走 API 才行）。``file_count`` 一并写入：
    它是 ``BaselineContext`` 的既有字段，缺了提示词里「基线文件（共 N 个）」还得再查一次库。

    只接受 ``type=baseline`` 产物；给了 ``project_id`` 时再校验**属于本项目**
    （跨项目引用会让 ``store.load`` 与项目隔离自相矛盾，docs/09 §9-T2.2 ④）。
    不给 ``project_id``（调试 / 外部校验脚本）时跳过跨项目那条，只打印一句提示 ——
    不为对齐 API 在这里引入 repository 依赖。

    Args:
        store: 产物库（脚本里由 ``stack.artifact_store`` 提供）。
        project_id: 本次 run 所属项目；``None`` = 调用方拿不到，跳过跨项目校验。
        artifact_id: 基线产物 id。
        version: 基线版本；``None`` = 取该产物最新版（与 ``POST /runs`` 同义）。

    Returns:
        ``{"artifact_id", "version", "content_hash", "file_count"}``。

    Raises:
        SystemExit: 产物不存在 / 不是基线 / 不属于本项目 —— 都是**用法错误**，
            脚本以非零退出更直接（不引入 HTTP 业务码）。
    """
    try:
        artifact, row = await store.get_version_meta(
            artifact_id.strip(), version, tenant_id=DEFAULT_TENANT_ID
        )
    except NotFoundError as exc:
        # v1.3.1（T6.3-C / QA-T63-01）：**不**在这里自己拼「基线不存在」——
        # ``store.get_version_meta`` 已区分「产物不存在」与「产物版本不存在」
        # 两种成因，压成一句会把版本号丢掉，逼人只去查 artifact_id。
        # 与 ``api/routers/runs.py::_resolve_baseline`` 同口径（两入口答案必须一致）。
        raise SystemExit(f"基线引用无效: {exc}") from exc
    if artifact.type != BASELINE_TYPE:
        raise SystemExit(
            f"产物 {artifact_id} 不是基线（type={artifact.type}，应为 {BASELINE_TYPE}）"
        )
    if project_id is None:
        print(f"[baseline] 跳过「属于本项目」校验（未提供 project_id）：{artifact_id}")
    elif artifact.project_id != project_id:
        raise SystemExit(f"基线 {artifact_id} 不属于本项目 {project_id}")
    return {
        "artifact_id": artifact.id,
        "version": row.version,
        "content_hash": row.content_hash,
        "file_count": row.file_count,
    }


async def build_run_context(
    args: argparse.Namespace,
    store: ArtifactStore | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    """构造 ``run.context``：模板与基线只放**引用快照**。

    与 ``src/ymir/api/routers/runs.py`` 的 ``POST /runs`` 保持同一形状：
    ``{"context_refs": [...], "template": {"id","version"}, "baseline": {...}}``。

    ★ v1.3.1（T6.3-C）：``baseline`` 是**四字段**
    （``artifact_id`` / ``version`` / ``content_hash`` / ``file_count``），由
    :func:`resolve_baseline_ref` 从产物库取。旧实现只写前两个、却在这段 docstring 里
    声称"与 POST /runs 同一形状" —— 那句话当时**不成立**，代价是提升端点拿不到
    ``content_hash``、CLI 发起的增量 run 无法「另存为新基线」。

    Args:
        args: 命令行参数（用到 ``template_id`` / ``baseline_artifact_id`` /
            ``baseline_version``）。
        store: 产物库；**可以不给**（``None``）—— 不指定基线时根本不需要它。
            指定了基线却拿不到 store ⇒ ``SystemExit``：宁可早失败，也不写半份
            引用（少了 ``content_hash`` 的 baseline 会让产物永远提升不了）。
        project_id: 本次 run 的项目；``None`` = 跳过跨项目校验（见
            :func:`resolve_baseline_ref`）。

    Raises:
        SystemExit: 指定了基线但没有 store（缺 ``content_hash`` 就无从三方向比对）。
    """
    context: dict[str, object] = {"context_refs": []}
    if args.template_id:
        spec = load_template(args.template_id)
        context["template"] = {"id": spec.id, "version": spec.version}
        print(f"[template] 已加载 {spec.id} v{spec.version}")
    if args.baseline_artifact_id:
        if store is None:
            # 不静默少写两个字段：半份引用比报错更难查（提升时只会看到 1003）。
            raise SystemExit(
                "缺少产物库（store）：基线引用必须从版本行取 content_hash / file_count，"
                "不能只手写 artifact_id + version（dry-run 请走 --dry-run，不走本函数）"
            )
        baseline = await resolve_baseline_ref(
            store,
            project_id=project_id,
            artifact_id=args.baseline_artifact_id,
            version=args.baseline_version,
        )
        context["baseline"] = baseline
        print(
            f"[baseline] {baseline['artifact_id']} v{baseline['version']}"
            f"（{baseline['file_count']} 文件，增量模式）"
        )
    return context


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


def print_evaluation(outcome: TaskOut, *, usage_line: str = "") -> None:
    """打印四维分数 + 双门槛（**不是裁决**：verdict 仍由 QA 角色判定，L4 口径不变）。

    Args:
        outcome: 评测产出。
        usage_line: 「合计（含 --evaluate）」那一行（v1.3.1 T4.3）；空 ⇒ 不打印
            —— **未传 ``--evaluate`` 时输出与 v1.3 逐字节一致**。
    """
    print("- 四维分数（确定性口径）")
    for dim in outcome.dimensions:
        print(f"  {dim.label} {dim.score:.2f} / 100（权重 {dim.weight:.2f}）")
    thresholds = outcome.thresholds or {}
    print(
        f"  加权总分 {outcome.total_score:.2f}"
        f"（门槛 {thresholds.get('total', 75):g}）"
        f"  可运行率 {outcome.runnability:.2f}"
        f"（门槛 {thresholds.get('runnability', 60):g}）"
    )
    if outcome.baseline_runnability is not None:
        probe = outcome.baseline_runnability
        print(f"  基线检查：{probe.status}（{probe.reason or '通过'}）")
    if outcome.regression:
        print(f"  回归：{outcome.regression_reason}")
    if outcome.blocking_failed:
        print(f"  阻断项失败：{'、'.join(outcome.blocking_failed)}")
    if outcome.skipped_checks:
        print(f"  已跳过检查：{len(outcome.skipped_checks)} 条（环境缺失，不扣分）")
    print("  ⚠️ 该分数为附加信息，不参与 verdict（verdict 仍由 QA 角色判定）")
    if usage_line:
        # v1.3.1（T4.3）：`--evaluate` 自己也会调模型（judge / 语料分级），
        # 上面「合计」是**执行阶段**口径，两者不能混叠 ⇒ 另起一行给含评测的口径。
        print(usage_line)
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
            if args.baseline_artifact_id:
                # dry-run 不建 run、也不建 stack ⇒ 拿不到 store，取不出 content_hash。
                # 这里只声明"没校验"，不当错误（演练本来就不该碰库）。
                print("[baseline] 基线引用校验跳过（dry-run）")
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
        # v1.3.1（T6.3-C）：传 store + project_id —— 基线引用要写成**四字段**
        # （与 POST /runs 同形），否则提升端点缺 content_hash 无法三方比对。
        context = await build_run_context(args, stack.artifact_store, project_id=project_id)
        async with session_factory() as session:
            run = await RunRepository(session).create(
                project_id=project_id,
                requirement=args.requirement,
                pipeline_id=definition.id,
                mode=args.mode,
                context=context,
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
        if args.evaluate:
            # v1.3（T4.1）：与 gate 裁决 / 批量补评**同一装配点**（run_task.evaluate_run），
            # 不重复实现打分逻辑，也不改写 run 的终态与 verdict。
            async with session_factory() as session:
                fresh_run = await RunRepository(session).get(run_id)
            if fresh_run is None:
                print("[evaluate] 跳过：run 已不存在")
            else:
                harness = build_eval_harness(
                    settings,
                    stack.artifact_store,
                    gateway=stack.gateway,
                    session_factory=session_factory,
                    events=stack.event_publisher,
                )
                print("- 评测（附加信息，不参与 verdict）")
                outcome = await evaluate_run(
                    harness, fresh_run, default_suite=args.suite
                )
                # v1.3.1（T4.3）：既有「合计：N 次调用 / tokens / ¥」是**执行阶段**
                # 口径，刻意不动；这里补一行「含 --evaluate」的口径，
                # 让"单跑 + 评测"的真实花费可核（两行并存，不混叠）。
                totals = await UsageRecorder(session_factory).totals(run_id=run_id)
                print_evaluation(
                    outcome,
                    usage_line=(
                        f"  合计（含 --evaluate）：{totals.calls} 次调用 / "
                        f"{totals.total_tokens} tokens / ¥{totals.cost_cny:.6f}"
                    ),
                )
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

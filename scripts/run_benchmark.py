"""基准批量跑分 CLI（v1.1 T2.3，docs/07 §4.2.1）。

用法（仓库根目录）：

  # 先校准 3 个（零费用演练用 --no-judge）
  uv run python scripts/run_benchmark.py --suite benchmark-12 --limit 3 --no-judge

  # 续跑剩余任务
  uv run python scripts/run_benchmark.py --resume --batch-id 20261019-a

  # 全量 + 批次预算硬上限（达到后剩余任务 skipped{budget}，零模型调用）
  uv run python scripts/run_benchmark.py --budget-cny 20 --batch-id 20261019-a

  # 只重算基线表（不发起任何 run）
  uv run python scripts/run_benchmark.py --batch-id 20261019-a --report-only

**启动预检是强制的**：镜像不存在 / 镜像内 pytest 或 ruff 不可用 → 打印重建命令并
``SystemExit(2)``，**不发起任何 run、也绝不回退到 ``python:3.12-slim``**
（那样会静默跑出一张全红基线，比没有基线更有害）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from ymir.bootstrap import build_execution_stack, build_repair_stack
from ymir.common.errors import InvalidParamError, YmirError
from ymir.common.logging import setup_logging
from ymir.config import Settings, get_settings, load_env_file
from ymir.evaluation import build_eval_harness
from ymir.evaluation.baseline import BaselineTable
from ymir.evaluation.benchmark import BenchmarkOptions, BenchmarkRunner, BenchmarkState
from ymir.evaluation.suite import EvalSuite
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.storage.db import build_engine, build_session_factory

#: 缺省沙箱镜像（可用 ``--sandbox-image`` 或 ``YMIR_SANDBOX_IMAGE`` 覆盖）
DEFAULT_IMAGE = "ymir-sandbox:latest"

#: 预检失败时给用户的重建命令（唯一口径，不要改成 python:3.12-slim）
BUILD_HINT = "docker build -f docker/sandbox.Dockerfile -t ymir-sandbox:latest ."

#: 批量跑分固定网络策略（镜像已预置依赖，评测期不联网）
BATCH_NETWORK = "none"

#: 「镜像内工具缺失」的判定特征（与「docker 本身报错」区分，避免误报成镜像未构建）
TOOL_MISSING_PATTERNS: tuple[str, ...] = ("executable file not found", "No module named")

#: docker CLI 的噪音行（不含诊断信息，打印时剔除）
_DOCKER_NOISE_PREFIXES: tuple[str, ...] = (
    "Run 'docker run --help'",
    "See 'docker run --help'",
)

#: 数据目录（状态文件与基线表；``data/`` 已在 .gitignore）
DATA_DIR = Path("data") / "benchmark"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ymir-agent 基准批量跑分（v1.1 T2.3）")
    parser.add_argument("--suite", default="benchmark-12", help="套件 ID（须 per_task 绑定）")
    parser.add_argument("--batch-id", default="", help="批次标识（缺省按 UTC 日期生成）")
    parser.add_argument("--limit", type=int, default=None, help="本次最多发起 N 个任务")
    parser.add_argument("--resume", action="store_true", help="读状态文件，跳过已完成任务")
    parser.add_argument("--budget-cny", type=float, default=None, help="批次实测成本硬上限")
    parser.add_argument("--no-judge", action="store_true", help="纯确定性口径（judge 成本 0）")
    parser.add_argument(
        "--sandbox-image",
        default=None,
        help=f"沙箱镜像 tag（缺省取 YMIR_SANDBOX_IMAGE，回落 {DEFAULT_IMAGE}）",
    )
    parser.add_argument("--concurrency", type=int, default=1, help="固定 1（单 run 串行）")
    parser.add_argument("--report-only", action="store_true", help="只重算基线表，不发起 run")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印基线表")
    return parser.parse_args(argv)


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """执行外部命令（测试可 monkeypatch 至此函数）。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def preflight(image: str) -> dict[str, str]:
    """启动预检：镜像存在 + pytest 可用 + ruff 可用；失败 ``SystemExit(2)``。

    返回 ``image_probe``（``{"pytest": "8.x", "ruff": "0.x"}``），写入状态文件
    与基线表的 ``meta``，作为「本次基线的执行环境指纹」。
    """
    commands: list[tuple[str, list[str]]] = [
        ("image", ["docker", "image", "inspect", image]),
        (
            "pytest",
            ["docker", "run", "--rm", "--network", BATCH_NETWORK, image,
             "python", "-m", "pytest", "--version"],
        ),
        (
            "ruff",
            ["docker", "run", "--rm", "--network", BATCH_NETWORK, image, "ruff", "--version"],
        ),
    ]
    probe: dict[str, str] = {}
    for name, cmd in commands:
        try:
            result = _run(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            # docker 未安装 / 无法执行：同样按「预检失败」处理（绝不回退基础镜像）
            print(f"[preflight] {name} 无法执行（{image}）：{exc}", file=sys.stderr)
            print(f"[preflight] 请先构建沙箱镜像：{BUILD_HINT}", file=sys.stderr)
            raise SystemExit(2) from exc
        if result.returncode != 0:
            tail = _detail_tail(result, result.returncode)
            if name != "image" and _is_tool_missing(tail):
                # 镜像能跑但工具没装（如旧镜像缺 ruff）：这不是 docker 故障，重建镜像即可
                print(f"[preflight] 镜像缺少 {name}（{image}）：{tail}", file=sys.stderr)
                print(
                    f"[preflight] 请重建镜像以补齐 pytest/ruff：{BUILD_HINT}",
                    file=sys.stderr,
                )
            elif name == "image":
                print(f"[preflight] image 检查未通过（{image}）：{tail}", file=sys.stderr)
                print(f"[preflight] 请先构建沙箱镜像：{BUILD_HINT}", file=sys.stderr)
            else:
                # 其它 docker 错误：原样保留诊断信息，**不**误报成「请先构建镜像」
                print(f"[preflight] {name} 检查未通过（{image}）：{tail}", file=sys.stderr)
            raise SystemExit(2)
        if name != "image":
            probe[name] = _version_of(result.stdout, name)
    print(f"[preflight] image={image} pytest={probe.get('pytest')} ruff={probe.get('ruff')}")
    return probe


def _detail_tail(result: subprocess.CompletedProcess[str], returncode: int) -> str:
    """取一条**有诊断价值**的失败信息（剔除 docker CLI 的 ``--help`` 噪音行）。"""
    text = (result.stderr or result.stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    meaningful = [
        line for line in lines if not line.startswith(_DOCKER_NOISE_PREFIXES)
    ]
    return meaningful[-1] if meaningful else f"exit={returncode}"


def _is_tool_missing(tail: str) -> bool:
    """失败信息是否属于「镜像内工具缺失」（而非 docker/daemon 故障）。"""
    return any(pattern in tail for pattern in TOOL_MISSING_PATTERNS)


def _version_of(stdout: str, name: str) -> str:
    """从 ``pytest 8.3.3`` / ``ruff 0.6.9`` 里取版本号（解析失败回落首行）。"""
    text = (stdout or "").strip()
    if not text:
        return "unknown"
    first = text.splitlines()[0].strip()
    parts = first.split()
    for index, token in enumerate(parts):
        if token == name and index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1]


def default_batch_id() -> str:
    """缺省批次标识（UTC 日期 + ``-1`` 序号后缀，避免同日多次覆盖）。"""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%d-1")


def state_path_for(suite_id: str, batch_id: str, data_dir: Path | None = None) -> Path:
    return Path(data_dir or DATA_DIR) / f"state-{suite_id}-{batch_id}.json"


async def build_runner(
    settings: Settings,
    *,
    suite_id: str,
    batch_id: str,
    limit: int | None,
    budget_cny: float | None,
    image: str,
    probe: dict[str, str],
    data_dir: Path | None = None,
) -> tuple[AsyncEngine, BenchmarkRunner]:
    """装配「执行栈 + harness + 回边栈 + 引擎」并构造 runner（与 api/app.py 同构）。"""
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    stack = build_execution_stack(settings, session_factory)
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
    meta = {
        "model_routing": _routing_text(stack.gateway),
        "sandbox": settings.sandbox_backend,
        "image": image,
        "image_probe": dict(probe),
        "network": BATCH_NETWORK,
        "judge": _judge_text(stack.gateway),
        "pipeline_id": settings.default_pipeline_id,
        "pipeline_version": _pipeline_version(settings),
    }
    runner = BenchmarkRunner(
        session_factory=session_factory,
        orchestrator=orchestrator,
        harness=harness,
        suite_id=suite_id,
        batch_id=batch_id,
        limit=limit,
        budget_cny=budget_cny,
        sandbox=settings.sandbox_backend,
        data_dir=data_dir or DATA_DIR,
        meta=meta,
    )
    return engine, runner


def _routing_text(gateway: Any) -> str:
    """路由指纹：``planner=<model>/executor=<model>``（取不到时回落 —）。"""
    try:
        routing = gateway.registry.config.routing or {}
        parts = [f"{tier}={models[0]}" for tier, models in routing.items() if models]
        return "/".join(parts) or "—"
    except Exception:  # noqa: BLE001 - 指纹缺失不应影响跑分
        return "—"


def _judge_text(gateway: Any) -> str:
    """judge 指纹：``<model>/temp=0.1``（judge 与 executor 同档同温）。"""
    try:
        routing = gateway.registry.config.routing or {}
        executor_models = routing.get("executor") or []
        model = executor_models[0] if executor_models else "—"
        return f"{model}/temp=0.1"
    except Exception:  # noqa: BLE001
        return "—"


def _pipeline_version(settings: Settings) -> str:
    try:
        from ymir.sop.loader import load_pipeline

        return load_pipeline(settings.default_pipeline_id).version
    except Exception:  # noqa: BLE001 - 版本指纹缺失不阻断跑分
        return ""


def render_table(
    state: BenchmarkState, suite: EvalSuite, *, as_json: bool, data_dir: Path | None = None
) -> str:
    """把状态渲染成基线表并落盘，返回待打印文本。"""
    table = BaselineTable.from_state(state, suite)
    table.save(data_dir or DATA_DIR)
    if as_json:
        return json.dumps(table.to_json(), ensure_ascii=False, indent=2)
    return table.to_markdown()


async def run_main(args: argparse.Namespace) -> int:
    load_env_file()
    settings = get_settings()
    setup_logging(debug=settings.debug)
    if args.concurrency != 1:
        raise InvalidParamError("批量跑分并发固定为 1（单 run 串行，v1.x 再放开）")
    if settings.sandbox_network != BATCH_NETWORK:
        raise InvalidParamError(
            f"批量跑分固定 network={BATCH_NETWORK}（当前 {settings.sandbox_network}）"
        )

    suite = EvalSuite.load(args.suite)
    batch_id = args.batch_id or default_batch_id()
    state_path = state_path_for(args.suite, batch_id)

    if args.report_only:
        state = BenchmarkState.load(state_path)
        if not state.tasks:
            raise InvalidParamError(f"状态文件为空或不存在，无法只重算表：{state_path}")
        print(render_table(state, suite, as_json=args.json))
        return 0

    image = args.sandbox_image or DEFAULT_IMAGE
    probe = preflight(image)
    engine, runner = await build_runner(
        settings,
        suite_id=args.suite,
        batch_id=batch_id,
        limit=args.limit,
        budget_cny=args.budget_cny,
        image=image,
        probe=probe,
        data_dir=DATA_DIR,
    )
    try:
        state = await runner.run(
            BenchmarkOptions(
                suite_id=args.suite,
                batch_id=batch_id,
                limit=args.limit,
                resume=args.resume,
                budget_cny=args.budget_cny,
                no_judge=args.no_judge,
                state_path=state_path,
            )
        )
        print(render_table(state, suite, as_json=args.json))
        print(
            f"[summary] batch={batch_id} executed={len(state.tasks)} "
            f"spent=¥{state.spent_cny:.4f} state={state_path}",
            file=sys.stderr,
        )
        return 0
    finally:
        await runner.shutdown()
        await engine.dispose()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_main(args))
    except SystemExit:
        raise
    except YmirError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[interrupt] 已中断（可用 --resume 续跑）")
        return 130


if __name__ == "__main__":
    sys.exit(main())

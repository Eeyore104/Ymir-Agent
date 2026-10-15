"""T05 批量跑分脚本（架构设计 §12.4）。

对同一套件、不同产物版本（或不同模型路由组合）跑分，输出四维报告 + 性价比榜单。

用法（仓库根目录）：

  # 对项目 code 产物 v1、v2 批量跑分（确定性口径，不调模型）
  uv run python scripts/run_eval.py --project-name demo --versions v1,v2 --no-judge

  # 单版本，真实 judge（需 .env 密钥；产生评测成本）
  uv run python scripts/run_eval.py --project-id <id> --suite default --save

  # 输出机读 JSON（供 CI 消费）
  uv run python scripts/run_eval.py --project-id <id> --versions latest --json

口径：runnability 走沙箱 exec（真实执行），convention/architecture 确定性规则为辅，
requirement 与设计一致性由 judge 打分（--no-judge 时全部走确定性降级路径）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ymir.artifacts.store import ArtifactStore
from ymir.common.logging import setup_logging
from ymir.config import get_settings, load_env_file
from ymir.evaluation import BatchEntry, BatchReport, BatchTarget, build_eval_harness
from ymir.evaluation.evaluators import Judge, LLMJudge
from ymir.evaluation.harness import BackendSandboxProvider
from ymir.gateway.gateway import build_gateway
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.factory import build_sandbox
from ymir.storage.db import build_engine, build_session_factory
from ymir.storage.repositories import ProjectRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ymir-agent 批量跑分（T05）")
    parser.add_argument("--suite", default="default", help="评测套件 ID（evals/suites/*.yaml）")
    parser.add_argument("--project-id", default=None, help="项目 ID")
    parser.add_argument("--project-name", default=None, help="项目名精确匹配（与 id 二选一）")
    parser.add_argument("--artifact-name", default="code", help="目标产物名（默认 code）")
    parser.add_argument(
        "--versions",
        default="latest",
        help="逗号分隔版本号（latest 或 v1,v2；留空=latest）",
    )
    parser.add_argument("--route", default="", help="路由组合标签（性价比榜单展示用）")
    parser.add_argument("--cost", type=float, default=0.0, help="该目标的生成成本（元）")
    parser.add_argument("--no-judge", action="store_true", help="纯确定性口径，不调 judge")
    parser.add_argument("--save", action="store_true", help="把报告作为产物入库")
    parser.add_argument("--json", action="store_true", help="额外输出机读 JSON")
    return parser.parse_args()


def _parse_versions(raw: str) -> list[int | None]:
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        return [None]
    versions: list[int | None] = []
    for token in tokens:
        if token.lower() in ("latest", "null", "head"):
            versions.append(None)
        else:
            versions.append(int(token.lstrip("vV")))
    return versions


async def _resolve_project(session_factory, args: argparse.Namespace) -> str:
    async with session_factory() as session:
        repo = ProjectRepository(session)
        if args.project_id:
            project = await repo.get(args.project_id)
            if project is None:
                raise SystemExit(f"项目不存在: {args.project_id}")
            return project.id
        if args.project_name:
            projects = await repo.list(limit=500)
            matches = [p for p in projects if p.name == args.project_name]
            if not matches:
                raise SystemExit(f"未找到项目：{args.project_name}")
            return matches[0].id
        raise SystemExit("必须提供 --project-id 或 --project-name")


def _build_judge(settings, gateway, *, no_judge: bool) -> Judge | None:
    if no_judge:
        return None
    return LLMJudge(gateway)


async def run_main(args: argparse.Namespace) -> int:
    load_env_file()
    settings = get_settings()
    setup_logging(debug=settings.debug)
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        store = ArtifactStore(session_factory, settings.resolved_artifacts_dir)
        gateway = build_gateway(session_factory)
        judge = _build_judge(settings, gateway, no_judge=args.no_judge)
        harness = build_eval_harness(
            settings,
            store,
            gateway=gateway if not args.no_judge else None,
            judge=judge,
            sandbox_provider=BackendSandboxProvider(
                loader=lambda: build_sandbox(settings),
                network=NetworkPolicy(settings.sandbox_network),
            ),
            session_factory=session_factory,
        )

        project_id = await _resolve_project(session_factory, args)
        versions = _parse_versions(args.versions)
        targets = [
            BatchTarget(
                label=(args.route or "default") if version is None else f"v{version}",
                version=version,
                route=args.route,
                cost_cny=args.cost,
            )
            for version in versions
        ]

        entries = []
        payloads: list[dict] = []
        for target in targets:
            report = await harness.run_suite(
                args.suite, project_id=project_id, version_override=target.version
            )
            print("=" * 78)
            print(report.to_markdown())
            if args.save:
                saved = await report.save(store, project_id=project_id)
                print(f"[saved] 报告入库：{saved.artifact_name} v{saved.version}")
            payloads.append(json.loads(report.to_json()))
            cost = target.cost_cny if target.cost_cny > 0 else report.cost_cny
            entries.append(
                BatchEntry(
                    label=target.label,
                    route=target.route,
                    version=target.version,
                    total_score=report.total_score,
                    runnability=report.runnability,
                    passed=report.passed,
                    cost_cny=round(cost, 6),
                    wall_time_ms=report.wall_time_ms,
                    cost_per_score=round(cost / report.total_score, 6)
                    if report.total_score > 0
                    else 0.0,
                )
            )

        batch = BatchReport(suite_id=args.suite, project_id=project_id, entries=entries)
        print("=" * 78)
        print(batch.to_table())
        if args.json:
            print("=" * 78)
            print(json.dumps({"reports": payloads, "batch": [e.to_dict() for e in entries]},
                             ensure_ascii=False, indent=2))
        return 0
    finally:
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

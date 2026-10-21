"""运行控制 API 测试（v1.1 T4.3）：动作白名单 + 人工介入两动作的 API 语义。

范围（docs/07 §4.5.3）：``accept-best`` / ``extend-repair`` 与既有四动作共用同一
``POST /runs/{id}/actions`` 入口；非法动作 -> 1001（消息列出合法动作）、
非 ``failed`` 状态执行 accept-best -> 1003（状态冲突）。

纪律：``client`` 夹具注入 stub 执行器（0 真实模型）；run 状态由 DB 直接铺设，
不经真实流水线，保证确定性。
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.models.base import utcnow
from ymir.models.entities import StageExecution
from ymir.storage.repositories import ProjectRepository, RunRepository

HISTORY: list[dict[str, Any]] = [
    {"round": 0, "verdict": "fail", "total": 62.0, "runnability": 48.0, "artifact_version": 1},
    {"round": 1, "verdict": "fail", "total": 71.0, "runnability": 58.0, "artifact_version": 2},
]


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    history: list[dict[str, Any]] | None = None,
    stop_reason: str | None = "rounds_exhausted",
) -> str:
    """铺一个指定状态的 run（含 context.repair.history）。"""
    async with session_factory() as session:
        project = await ProjectRepository(session).create(name="api-actions")
        run = await RunRepository(session).create(
            project_id=project.id, requirement="做一个 CLI 工具", mode="batch"
        )
        await session.commit()
        run_id = run.id

    async with session_factory() as session:
        row = await RunRepository(session).get(run_id)
        assert row is not None
        row.status = status
        row.current_stage_id = "evaluation"
        row.error = "评测未达标" if status == "failed" else None
        row.context = {
            **(row.context or {}),
            "repair": {
                "round": 2,
                "max_rounds": 2,
                "extra_rounds": 0,
                "stage": "coding",
                "history": list(HISTORY if history is None else history),
                "stop_reason": stop_reason,
                "accepted": None,
            },
        }
        session.add(
            StageExecution(
                run_id=run_id,
                stage_id="coding",
                role_id="engineer",
                status="completed",
                attempt=2,
                started_at=utcnow(),
            )
        )
        await session.commit()
    return run_id


async def test_illegal_action_returns_1001_with_whitelist(client: AsyncClient) -> None:
    """非法动作 -> 400/1001，且报错信息列出全部合法动作（含两个新动作）。"""
    run_id = await _seed_run(client._transport.app.state.session_factory, status="failed")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "fly"})

    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    message = body["message"]
    for action in ("pause", "resume", "cancel", "retry-stage", "accept-best", "extend-repair"):
        assert action in message, action


async def test_accept_best_conflicts_when_not_failed(client: AsyncClient) -> None:
    """非 failed 状态执行 accept-best -> 1003（running 也不能直接接受）。"""
    run_id = await _seed_run(client._transport.app.state.session_factory, status="running")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "accept-best"})

    assert resp.status_code == 409
    assert resp.json()["code"] == 1003


async def test_accept_best_completes_run_via_api(client: AsyncClient) -> None:
    """failed + history -> accept-best 返回 completed，并落 accepted 痕迹。"""
    app = client._transport.app
    run_id = await _seed_run(app.state.session_factory, status="failed")

    resp = await client.post(f"/api/v1/runs/{run_id}/actions", json={"action": "accept-best"})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["action"] == "accept-best"
    assert data["status"] == "completed"

    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()["data"]
    assert detail["status"] == "completed"

    async with app.state.session_factory() as session:
        run = await RunRepository(session).get(run_id)
    accepted = (run.context or {})["repair"]["accepted"]
    assert accepted["version"] == 2  # total=71.0 的最优轮次
    assert accepted["total"] == 71.0


async def test_extend_repair_requires_history(client: AsyncClient) -> None:
    """history 为空 -> 1001（无可追加的修复轮次）。"""
    run_id = await _seed_run(
        client._transport.app.state.session_factory, status="failed", history=[]
    )

    resp = await client.post(
        f"/api/v1/runs/{run_id}/actions", json={"action": "extend-repair"}
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == 1001


# --------------------------------------------------------------------- 双报告类型（T5.2）

import json  # noqa: E402


def _report_payload(
    *, suite_id: str, total: float, runnability: float, passed: bool, **extra: Any
) -> str:
    """构造一份最小的 ``EvaluationReport`` JSON（两种产物共用同一结构）。"""
    payload: dict[str, Any] = {
        "suite_id": suite_id,
        "suite_version": "1.1",
        "total_score": total,
        "runnability": runnability,
        "passed": passed,
        "tasks": [],
        "thresholds": {"total": 75.0, "runnability": 60.0},
        "weights": {"requirement_conformance": 0.3, "runnability": 0.3},
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def test_evaluations_support_both_report_kinds(client: AsyncClient) -> None:
    """T5.2：同项目同时有 ``evaluation_report`` 与 ``eval_verdict`` -> 列表 2 条、kind 正确。"""
    app = client._transport.app
    store = app.state.artifacts
    created = await client.post("/api/v1/projects", json={"name": "dual-report"})
    project_id = created.json()["data"]["id"]

    qa_report = await store.save(
        project_id=project_id,
        name="evaluation_report",
        artifact_type="report",
        files={
            "evaluation_report.json": _report_payload(
                suite_id="default", total=62.4, runnability=48.0, passed=False,
                verdict_source="llm_qa",
            )
        },
    )
    verdict_report = await store.save(
        project_id=project_id,
        name="eval_verdict",
        artifact_type="report",
        files={
            # 生产实况：``EvaluationReport.save`` 固定写 ``evaluation_report.json``，
            # 因此 ``eval_verdict`` 产物内部文件名与 ``evaluation_report`` 相同；
            # 路由按 artifact.name 选名并回退，两种命名都必须能取到。
            "evaluation_report.json": _report_payload(
                suite_id="benchmark-12",
                total=81.3,
                runnability=80.0,
                passed=True,
                run_id="run-1",
                artifact_version=2,
                repair_round=1,
                verdict_source="harness",
            )
        },
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/evaluations")
    items = resp.json()["data"]["items"]
    assert resp.json()["data"]["total"] == 2
    assert {item["kind"] for item in items} == {"evaluation_report", "eval_verdict"}

    verdict_item = next(item for item in items if item["kind"] == "eval_verdict")
    assert verdict_item["artifact_id"] == verdict_report.artifact_id
    assert verdict_item["total_score"] == 81.3
    assert verdict_item["runnability"] == 80.0
    assert verdict_item["passed"] is True

    qa_item = next(item for item in items if item["kind"] == "evaluation_report")
    assert qa_item["artifact_id"] == qa_report.artifact_id
    assert qa_item["passed"] is False

    # 全局列表同样能区分两类
    global_items = (await client.get("/api/v1/evaluations")).json()["data"]["items"]
    assert {item["kind"] for item in global_items} >= {"evaluation_report", "eval_verdict"}

    # 详情：两种产物都能打开，都返回 EvaluationReport
    for item in items:
        detail = await client.get(f"/api/v1/evaluations/{item['artifact_id']}")
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["suite_id"] in {"default", "benchmark-12"}
        assert "tasks" in data

    # 非报告产物仍 1002/404
    prd = await store.save(
        project_id=project_id, name="prd", artifact_type="prd", files={"prd.md": "# x"}
    )
    miss = await client.get(f"/api/v1/evaluations/{prd.artifact_id}")
    assert miss.status_code == 404
    assert miss.json()["code"] == 1002


def test_report_raw_supports_both_file_names() -> None:
    """路由按产物名选报告文件名，且两种命名都能取到（生产实况 vs 理想命名）。"""
    from ymir.api.routers.evaluations import _report_raw

    both = {
        "evaluation_report.json": "QA",
        "eval_verdict.json": "VERDICT",
    }
    assert _report_raw(both, "evaluation_report") == "QA"
    assert _report_raw(both, "eval_verdict") == "VERDICT"
    # 生产实况：eval_verdict 产物内部文件名仍是 evaluation_report.json
    assert _report_raw({"evaluation_report.json": "QA"}, "eval_verdict") == "QA"
    # 未知产物名：两种都试
    assert _report_raw(both, "unknown-name") == "QA"
    # 非报告产物：取不到
    assert _report_raw({"prd.md": "# x"}, "prd") is None

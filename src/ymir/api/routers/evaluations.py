"""评测接口（§3.7、§12、§6.1）。

- ``GET /projects/{id}/evaluations``：项目评测报告列表（摘要）；
- ``GET /evaluations``：全局评测报告列表（跨项目，最新在前）；
- ``POST /projects/{id}/evaluations``：对项目某产物版本跑套件，返回完整报告
  （默认作为一等产物入库，可发 ``evaluation.completed`` 事件）。

响应遵循统一信封（§6.3）：``{"code": 0, "data": ...}``。
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from sqlalchemy import select

from ymir.api.deps import EvaluationsDep, EventsDep, SessionDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    EvaluationRunRequest,
    EvaluationSummaryOut,
    PageData,
)
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.evaluation.report import EvaluationReport
from ymir.models.entities import Artifact
from ymir.models.enums import ArtifactType
from ymir.storage.repositories import ProjectRepository

router = APIRouter(tags=["evaluations"])

_REPORT_NAME = "evaluation_report"


async def _report_artifacts(session, *, project_id: str | None, tenant_id: str) -> list[Artifact]:
    stmt = select(Artifact).where(
        Artifact.tenant_id == tenant_id,
        Artifact.type == ArtifactType.REPORT.value,
        Artifact.name == _REPORT_NAME,
    )
    if project_id is not None:
        stmt = stmt.where(Artifact.project_id == project_id)
    stmt = stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def _summaries(store, artifacts: list[Artifact]) -> list[EvaluationSummaryOut]:
    items: list[EvaluationSummaryOut] = []
    for artifact in artifacts:
        try:
            loaded = await store.load(artifact.id)
        except NotFoundError:
            continue
        raw = loaded.files.get("evaluation_report.json")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # 仅收录四维评测报告（含 suite_id + total_score）；qa 阶段简化版
        # evaluation_report（EvalSummary 格式）不含这些字段，跳过以免出现空摘要。
        if not isinstance(data, dict) or "suite_id" not in data or "total_score" not in data:
            continue
        items.append(
            EvaluationSummaryOut(
                artifact_id=artifact.id,
                version=loaded.version,
                project_id=artifact.project_id,
                suite_id=str(data.get("suite_id", "")),
                total_score=float(data.get("total_score", 0.0)),
                runnability=float(data.get("runnability", 0.0)),
                passed=bool(data.get("passed", False)),
                generated_at=data.get("generated_at"),
                created_at=loaded.version_row.created_at,
            )
        )
    return items


@router.get(
    "/projects/{project_id}/evaluations",
    response_model=ApiResponse[PageData[EvaluationSummaryOut]],
    summary="项目评测报告列表",
)
async def list_project_evaluations(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    harness: EvaluationsDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    artifacts = await _report_artifacts(session, project_id=project_id, tenant_id=tenant_id)
    items = await _summaries(harness.store, artifacts)
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/evaluations",
    response_model=ApiResponse[PageData[EvaluationSummaryOut]],
    summary="全局评测报告列表（跨项目）",
)
async def list_evaluations(
    session: SessionDep,
    tenant_id: TenantDep,
    harness: EvaluationsDep,
) -> dict[str, object]:
    artifacts = await _report_artifacts(session, project_id=None, tenant_id=tenant_id)
    items = await _summaries(harness.store, artifacts)
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.post(
    "/projects/{project_id}/evaluations",
    response_model=ApiResponse[EvaluationReport],
    summary="对项目产物发起评测（四维报告）",
)
async def run_evaluation(
    project_id: str,
    payload: EvaluationRunRequest,
    session: SessionDep,
    tenant_id: TenantDep,
    harness: EvaluationsDep,
    events: EventsDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    if payload.version is None:
        artifacts = await harness.store.list_artifacts(project_id, tenant_id=tenant_id)
        if not any(a.name == payload.artifact_name for a in artifacts):
            raise InvalidParamError(f"项目内不存在产物 `{payload.artifact_name}`，无法评测")

    report = await harness.evaluate_project(
        project_id,
        suite_id=payload.suite_id,
        artifact_name=payload.artifact_name,
        version=payload.version,
        requirement=payload.requirement,
    )

    if payload.save:
        await report.save(harness.store, project_id=project_id, run_id=payload.run_id)

    if payload.run_id:
        await events.emit(
            payload.run_id,
            "evaluation.completed",
            {
                "suite_id": report.suite_id,
                "total_score": report.total_score,
                "runnability": report.runnability,
                "passed": report.passed,
                "verdict": "pass" if report.passed else "fail",
            },
        )
    return {"code": 0, "data": report}

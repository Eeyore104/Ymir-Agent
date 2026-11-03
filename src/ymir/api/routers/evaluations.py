"""评测接口（§3.7、§12、§6.1）。

- ``GET /projects/{id}/evaluations``：项目评测报告列表（摘要）；
- ``GET /evaluations``：全局评测报告列表（跨项目，最新在前）；
- ``POST /projects/{id}/evaluations``：对项目某产物版本跑套件，返回完整报告
  （默认作为一等产物入库，可发 ``evaluation.completed`` 事件）。

响应遵循统一信封（§6.3）：``{"code": 0, "data": ...}``。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError
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

#: 一等评测报告产物名（v1.1 双类型：QA 叙事 ``evaluation_report`` + harness 裁决 ``eval_verdict``）
REPORT_ARTIFACT_NAMES: tuple[str, ...] = ("evaluation_report", "eval_verdict")

#: 产物名 -> 报告 JSON 文件名（两种产物都承载一份 ``EvaluationReport``）
REPORT_FILE_NAMES: dict[str, str] = {
    "evaluation_report": "evaluation_report.json",
    "eval_verdict": "eval_verdict.json",
}


async def _report_artifacts(session, *, project_id: str | None, tenant_id: str) -> list[Artifact]:
    """两类报告产物都取（v1.1）：``evaluation_report``（QA）与 ``eval_verdict``（harness）。"""
    stmt = select(Artifact).where(
        Artifact.tenant_id == tenant_id,
        Artifact.type == ArtifactType.REPORT.value,
        Artifact.name.in_(REPORT_ARTIFACT_NAMES),
    )
    if project_id is not None:
        stmt = stmt.where(Artifact.project_id == project_id)
    stmt = stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc())
    return list((await session.execute(stmt)).scalars().all())


def _report_raw(files: dict[str, str], artifact_name: str) -> str | None:
    """按产物名选报告 JSON 文件名（未知产物名时两种都试一遍，兼容历史数据）。

    注意（历史兼容）：**产物名与内部文件名是解耦的** —— ``EvaluationReport.save()``
    固定写 ``evaluation_report.json``，所以 ``eval_verdict`` 产物内部仍然是这个名字。
    这里坚持「按 artifact.name 选名 + 回退 evaluation_report.json」，不重命名产物
    内部文件（重命名要动 ``evaluation/report.py``，收益低、回归风险高）。
    """
    name = REPORT_FILE_NAMES.get(artifact_name)
    if name is not None:
        return files.get(name) or files.get(REPORT_FILE_NAMES["evaluation_report"])
    for candidate in REPORT_FILE_NAMES.values():
        raw = files.get(candidate)
        if raw:
            return raw
    return None


async def _summaries(store, artifacts: list[Artifact]) -> list[EvaluationSummaryOut]:
    if not artifacts:
        return []
    # 批量加载（ARCH-08）：一次 IN 查询取各报告最新版本，替换逐个 store.load 的 N+1
    loaded_map = {lv.artifact.id: lv for lv in await store.load_many([a.id for a in artifacts])}
    items: list[EvaluationSummaryOut] = []
    for artifact in artifacts:
        loaded = loaded_map.get(artifact.id)
        if loaded is None:
            continue
        raw = _report_raw(loaded.files, artifact.name)
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
                kind=artifact.name,
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


@router.get(
    "/evaluations/{evaluation_id}",
    response_model=ApiResponse[EvaluationReport],
    summary="评测报告详情（分数 + 证据链）",
)
async def get_evaluation(
    evaluation_id: str,
    tenant_id: TenantDep,
    harness: EvaluationsDep,
) -> dict[str, object]:
    """按产物 ID 读取评测报告详情（§6.1、§12.3）。

    报告以一等产物承载，v1.1 支持**两种**：``evaluation_report``（QA 叙事）与
    ``eval_verdict``（harness 确定性裁决）—— 按产物名选对应 JSON 文件名，
    两者都返回同一 ``EvaluationReport`` 结构（字段同源，前端无需分支）。
    不存在或非报告产物 -> ``NotFoundError``（1002 / 404）。
    """
    store = harness.store
    try:
        loaded = await store.load(evaluation_id, tenant_id=tenant_id)
    except NotFoundError as exc:
        raise NotFoundError(f"评测报告不存在: {evaluation_id}") from exc
    raw = _report_raw(loaded.files, getattr(loaded.artifact, "name", ""))
    if not raw:
        raise NotFoundError(f"评测报告不存在: {evaluation_id}")
    try:
        report = EvaluationReport.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise NotFoundError(f"评测报告格式不合法: {evaluation_id}") from exc
    return {"code": 0, "data": report}


async def _infer_report_run_id(
    store: Any, target: Any, version: int | None, *, tenant_id: str
) -> str | None:
    """按「产物版本行」反查产出该版本的 run（缺省 ``run_id`` 时兜底）。

    ★ 为什么需要（QA R5 `Q-R5-01`，P2）：提升闸门按 ``run_id`` 取报告，而
    **前端唯一的评测入口**只传 ``suite_id / artifact_name / version / save``
    （``web/src/views/EvalReport.vue``），``EvaluationRunRequest.run_id`` 一直是空
    ⇒ UI 用户"补评测 → 再提升"永远 409，只能 ``force``，而留痕写的是"没评测过"。

    修法选在服务端而不是加 UI 选择器：产物版本行本来就记着 ``run_id``
    （``artifact_versions.run_id``），反查零歧义。

    **边界（QA R6 ``Q-R6-01`` 如实标注）**：这里补的是**报告归属** —— 报告根部
    ``run_id`` 与报告产物的版本行；**不是** ``model_calls.run_id``。后者的唯一通道是
    ``eval_task.run_id``（``harness._bound_judge``），而该字段还会参与基线解析
    （``harness._baseline_ref_of``），改它属于口径变更，不在本次返修范围。
    因此本端点的 judge 成本仍记在 ``model_calls.run_id = NULL``。

    Returns:
        run id 字符串；产物不存在 / 该版本无归属 run / 读取失败 ⇒ ``None``
        （保持原行为：报告不带 run，闸门按"没有结论"fail-closed）。
    """
    if target is None:
        return None
    try:
        _artifact, row = await store.get_version_meta(
            target.id, version, tenant_id=tenant_id
        )
    except Exception:  # noqa: BLE001 - 反查失败只影响归属，不该让评测本身 500
        return None
    run_id = getattr(row, "run_id", None)
    return str(run_id) if run_id else None


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

    artifacts = await harness.store.list_artifacts(project_id, tenant_id=tenant_id)
    target = next((a for a in artifacts if a.name == payload.artifact_name), None)
    if target is None and payload.version is None:
        raise InvalidParamError(f"项目内不存在产物 `{payload.artifact_name}`，无法评测")

    # ★ v1.4 返修 5（QA R5 Q-R5-01）：UI 发起的评测**不带 run_id**，而提升闸门按
    # ``run_id`` 取报告 ⇒ Web 用户的"补评测"永远解锁不了提升。这里按**产物版本行**
    # 反查产出该版本的 run 兜底：前端零改动，报告获得正确的 **run 归属**。
    # （**不是** ``model_calls`` 的成本归属 —— 见 ``_infer_report_run_id`` 的边界说明。）
    effective_run_id = payload.run_id or await _infer_report_run_id(
        harness.store, target, payload.version, tenant_id=tenant_id
    )

    report = await harness.evaluate_project(
        project_id,
        suite_id=payload.suite_id,
        artifact_name=payload.artifact_name,
        version=payload.version,
        requirement=payload.requirement,
        # ★ 返修 4（Q-R4-02）：报告要自证归属（版本 + run），否则版本绑定读不到。
        run_id=effective_run_id,
    )

    if payload.save:
        await report.save(harness.store, project_id=project_id, run_id=effective_run_id)

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

"""评测接口（§3.7、§12、§6.1）。

- ``GET /projects/{id}/evaluations``：项目评测报告列表（摘要）；
- ``GET /evaluations``：全局评测报告列表（跨项目，最新在前）；
- ``POST /projects/{id}/evaluations``：对项目某产物版本跑套件，返回完整报告
  （默认作为一等产物入库，可发 ``evaluation.completed`` 事件）。

响应遵循统一信封（§6.3）：``{"code": 0, "data": ...}``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from ymir.artifacts.provenance import template_yaml_hash
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.evaluation.report import EvaluationReport
from ymir.models.entities import Artifact
from ymir.models.enums import ArtifactType
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates import DEFAULT_TEMPLATES_DIR

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


@dataclass(frozen=True)
class _RunCriteria:
    """归属 run 的「判据来源 + 需求文本」（一次读库、两处用，v1.5 D-2 修复）。

    Attributes:
        template_id: 模板 id（``""`` = 无模板）。
        template_hash: 该模板的 YAML 指纹（现算；``""`` = 无模板）。
        requirement: run 当时的原始需求（``""`` = run 没记）。
    """

    template_id: str = ""
    template_hash: str = ""
    requirement: str = ""


async def _resolve_template_ref(
    session, run_id: str | None, *, tenant_id: str
) -> _RunCriteria:
    """按归属 run 反查它当时的模板与需求（v1.5 T01.4；v1.5 修复轮 D-2 扩了需求）。

    ★ 为什么必须做（docs/17 §1.1.3 第 1 档）：``evaluate_project`` 没有 ``Run`` 对象
    ⇒ 拿不到 ``run.context["template"]["id"]`` ⇒ 补评测只能用套件 ``checks``，而 gate
    用的是模板 ``checks`` —— 那就是 v1.4 §0.3 那条闸门旁路（弱口径覆盖强口径）。
    UI 的评测请求**不带 ``run_id``**（``web/src/views/EvalReport.vue``），所以这里
    复用 :func:`_infer_report_run_id` 反查出的归属 run，再读它的模板。

    ``template_hash`` 在**这一层现算**（``api`` rank 0 可合法 import ``templates``）：
    ``artifacts.template_yaml_hash`` 收目录入参正是为了这种跨层调用（单一实现）。

    ★★ **D-2（v1.5 修复轮）**：同一个归因 run 的 ``requirement`` 也在这里取。
    gate 的 ``requirement`` 取的是 ``run.context["requirement"]``；补评测若允许调用方
    自带 ``requirement``，**同一棵树会被评出两个分数**（真跑实测 92.65 vs 62.65，
    差值恰好是该维权重）。判据同尺的前提是**输入同尺**。

    Args:
        session: 数据库会话。
        run_id: 归属 run（``None`` ⇒ 无模板、无需求）。
        tenant_id: 租户（**必须透传**，否则非默认租户下永远读不到 run）。

    Returns:
        :class:`_RunCriteria`；无 run / 读不到 / 模板已删除 ⇒ 对应字段为空
        （装配点据此落 ``criteria_source="suite"`` 显式弱口径，**不静默**）。
    """
    if not run_id:
        return _RunCriteria()
    try:
        run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - 反查失败只影响判据来源，不该让评测 500
        return _RunCriteria()
    context = run.context if run is not None and isinstance(run.context, dict) else {}
    requirement = str(context.get("requirement") or "").strip()
    ref = context.get("template")
    template_id = str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""
    if not template_id:
        return _RunCriteria(requirement=requirement)
    return _RunCriteria(
        template_id=template_id,
        template_hash=template_yaml_hash(DEFAULT_TEMPLATES_DIR, template_id),
        requirement=requirement,
    )


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
    # ★ v1.5（T01.4）：判据来源同样按归属 run 反查并**透传**给装配点。
    #   UI 口径（无 ``run_id``）因此也能拿到 ``criteria_source="template"``；
    #   拿不到时装配点落 ``"suite"`` 显式弱口径（A12 要求不返回 null）。
    run_criteria = await _resolve_template_ref(
        session, effective_run_id, tenant_id=tenant_id
    )
    # ★★ v1.5 修复轮 D-2：**有归属 run 就用它的 requirement**，忽略调用方自带的
    #   那份 —— 否则"同一棵树、两次补评测传不同 requirement"会得到两个分数
    #   （真跑实测 92.65 vs 62.65）。``payload.requirement`` 只在**无归属 run**
    #   （手工导入 / 脚本写入的产物）时才生效，并在报告里标 ``requirement_source``。
    requirement = payload.requirement
    requirement_source = "caller" if str(requirement or "").strip() else "none"
    if effective_run_id and run_criteria.requirement:
        requirement = run_criteria.requirement
        requirement_source = "run"

    report = await harness.evaluate_project(
        project_id,
        suite_id=payload.suite_id,
        artifact_name=payload.artifact_name,
        version=payload.version,
        requirement=requirement,
        # ★ D-2：需求来源随任务进报告（报告自证"我用的是谁的需求"）
        requirement_source=requirement_source,
        # ★ 返修 4（Q-R4-02）：报告要自证归属（版本 + run），否则版本绑定读不到。
        run_id=effective_run_id,
        # ★ v1.5（T01.1 / T01.4）：判据来源 —— 与 gate 的装配点同源（A8「同尺」）。
        template_id=run_criteria.template_id or None,
        template_hash=run_criteria.template_hash or None,
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

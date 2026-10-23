"""运行相关接口（§6.1）。

T02：发起运行后由编排引擎接管推进（pending -> running）；
新增 pause/resume/cancel/retry-stage 动作与角色消息流查询。
"""

from __future__ import annotations

from fastapi import APIRouter

from ymir.api.deps import (
    ArtifactsDep,
    MessagesDep,
    OrchestratorDep,
    SessionDep,
    SettingsDep,
    TenantDep,
)
from ymir.api.schemas import (
    ApiResponse,
    MessageOut,
    PageData,
    RunActionOut,
    RunActionPayload,
    RunCreate,
    RunCreated,
    RunOut,
)
from ymir.artifacts.store import BASELINE_TYPE
from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.messages.bus import message_summary_of
from ymir.storage.repositories import ProjectRepository, RunRepository
from ymir.templates import load_template

router = APIRouter(tags=["runs"])

#: 运行控制动作白名单（v1.0 四动作 + v1.1 人工介入两动作，§4.5.3）
RUN_ACTIONS: tuple[str, ...] = (
    "pause",
    "resume",
    "cancel",
    "retry-stage",
    "accept-best",
    "extend-repair",
)


@router.post(
    "/projects/{project_id}/runs",
    response_model=ApiResponse[RunCreated],
    status_code=202,
    summary="发起运行",
)
async def create_run(
    project_id: str,
    payload: RunCreate,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    orchestrator: OrchestratorDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    # v1.2：模板与基线都是 run 的**运行参数**（docs/09 §4.1.4 / §4.2.1）。
    # 两者都只写引用进 run.context，绝不进 stage.inputs（sop 校验器会否决）。
    template_ref = _resolve_template(payload.template_id)
    baseline_ref = await _resolve_baseline(
        store, project_id, payload.baseline_artifact_id, payload.baseline_version,
        tenant_id=tenant_id,
    )

    pipeline_id = payload.pipeline_id or settings.default_pipeline_id
    context: dict[str, object] = {"context_refs": payload.context_refs}
    if template_ref is not None:
        context["template"] = template_ref
    if baseline_ref is not None:
        context["baseline"] = baseline_ref
    run = await RunRepository(session).create(
        project_id=project_id,
        requirement=payload.requirement,
        pipeline_id=pipeline_id,
        mode=payload.mode,
        context=context,
        tenant_id=tenant_id,
    )
    await session.commit()
    # pending -> running，引擎接管推进（launch 失败时 run 保留为 pending 现场）
    await orchestrator.launch(run.id)
    return {"code": 0, "data": RunCreated(run_id=run.id)}


def _resolve_template(template_id: str | None) -> dict[str, str] | None:
    """校验模板存在性并返回**引用快照** ``{"id","version"}``。

    不存在 / 非法一律 1001（**而不是 1002**）：``template_id`` 是请求体里的参数，
    指向一个不存在的平台配置属于入参非法（与 T1.2 DoD ⑥、§13「1001 含模板非法」
    一致）；``GET /templates/{id}`` 那种"路径资源不存在"才用 1002。
    """
    if template_id is None or not template_id.strip():
        return None
    try:
        spec = load_template(template_id.strip())
    except NotFoundError as exc:
        raise InvalidParamError(f"模板不存在: {template_id}") from exc
    return {"id": spec.id, "version": spec.version}


async def _resolve_baseline(
    store: ArtifactsDep,
    project_id: str,
    artifact_id: str | None,
    version: int | None,
    *,
    tenant_id: str,
) -> dict[str, object] | None:
    """校验基线引用并返回快照 ``{"artifact_id","version","content_hash","file_count"}``。

    只接受**本项目**的 ``type=baseline`` 产物（否则 1001）：跨项目引用会让
    ``store.load`` 语义与租户/项目隔离自相矛盾（docs/09 §9-T2.2 ④）。
    """
    if artifact_id is None or not artifact_id.strip():
        return None
    try:
        artifact, row = await store.get_version_meta(
            artifact_id.strip(), version, tenant_id=tenant_id
        )
    except NotFoundError as exc:
        raise InvalidParamError(f"基线不存在: {artifact_id}") from exc
    if artifact.project_id != project_id:
        raise InvalidParamError(f"基线 {artifact_id} 不属于本项目")
    if artifact.type != BASELINE_TYPE:
        raise InvalidParamError(
            f"产物 {artifact_id} 不是基线（type={artifact.type}，应为 baseline）"
        )
    return {
        "artifact_id": artifact.id,
        "version": row.version,
        "content_hash": row.content_hash,
        "file_count": row.file_count,
    }


@router.get("/runs/{run_id}", response_model=ApiResponse[RunOut], summary="运行详情")
async def get_run(
    run_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    return {"code": 0, "data": RunOut.model_validate(run)}


@router.post(
    "/runs/{run_id}/actions",
    response_model=ApiResponse[RunActionOut],
    summary="运行控制动作（pause / resume / cancel / retry-stage / accept-best / extend-repair）",
)
async def run_action(
    run_id: str,
    payload: RunActionPayload,
    orchestrator: OrchestratorDep,
    session: SessionDep,
    tenant_id: TenantDep,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    handlers = {
        "pause": orchestrator.request_pause,
        "resume": orchestrator.request_resume,
        "cancel": orchestrator.request_cancel,
        "retry-stage": orchestrator.request_retry_stage,
        "accept-best": orchestrator.request_accept_best,
        "extend-repair": orchestrator.request_extend_repair,
    }
    handler = handlers.get(payload.action)
    if handler is None:
        raise InvalidParamError(
            f"非法的运行动作 {payload.action!r}，合法动作：{', '.join(RUN_ACTIONS)}"
        )
    updated = await handler(run_id)
    return {
        "code": 0,
        "data": RunActionOut(run_id=run_id, action=payload.action, status=updated.status),
    }


@router.get(
    "/runs/{run_id}/messages",
    response_model=ApiResponse[PageData[MessageOut]],
    summary="角色消息流",
)
async def list_run_messages(
    run_id: str,
    messages: MessagesDep,
    session: SessionDep,
    tenant_id: TenantDep,
    after_seq: int = 0,
    limit: int = 200,
) -> dict[str, object]:
    run = await RunRepository(session).get(run_id, tenant_id=tenant_id)
    if run is None:
        raise NotFoundError("运行不存在")
    page_size = max(1, min(limit, 500))
    records = await messages.list_by_run(
        run_id, after_seq=max(0, after_seq), limit=page_size, tenant_id=tenant_id
    )
    items = [MessageOut.model_validate(message_summary_of(record)) for record in records]
    return {"code": 0, "data": {"items": items, "total": len(items)}}

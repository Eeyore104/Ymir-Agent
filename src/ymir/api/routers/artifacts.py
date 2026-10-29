"""产物库接口（§6.1、§3.6）。

- ``GET /projects/{id}/artifacts``：项目产物列表；
- ``GET /artifacts/{id}``：产物详情（含版本历史与血缘）；
- ``GET /artifacts/{id}/versions/{v}/content``：版本文件清单 / 单文件内容；
- ``GET /artifacts/{id}/diff?v1=&v2=``：版本间 unified diff（按文件分组）；
- ``POST /projects/{id}/baselines``：**上传现有工程快照**（v1.2，docs/09 §4.2.1）；
- ``POST /projects/{id}/baselines/from-merge``：**以合入结果为新基线**
  （v1.3.1 T5.3，docs/13 §7.3-8；L7 闭环的落点）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.api.deps import ArtifactsDep, SessionDep, SettingsDep, TenantDep
from ymir.api.schemas import (
    ApiResponse,
    ArtifactContentOut,
    ArtifactDetailOut,
    ArtifactDiffOut,
    ArtifactOut,
    ArtifactVersionOut,
    BaselineMergeOut,
    BaselineMergeRequest,
    BaselineUploadOut,
    DiffStatsOut,
    FileDiffOut,
    PageData,
)
from ymir.artifacts.baseline import read_zip_files
from ymir.artifacts.diff import diff_file_sets
from ymir.artifacts.provenance import (
    BASELINE_ARTIFACT_NAME,
    promote_merged_to_baseline,
    template_yaml_hash,
)
from ymir.artifacts.store import BASELINE_TYPE
from ymir.common.errors import InvalidParamError, NotFoundError, StateConflictError
from ymir.common.paths import normalize_rel_path
from ymir.config import Settings
from ymir.evaluation.run_task import template_spec_for_run
from ymir.models.entities import PipelineRun, StageExecution
from ymir.storage.repositories import (
    ProjectRepository,
    RunRepository,
    StageExecutionRepository,
)
from ymir.templates import DEFAULT_TEMPLATES_DIR

router = APIRouter(tags=["artifacts"])

#: 基线产物名（同项目内复用同名 artifact，多次上传即 v1 -> v2 -> …）。
#:
#: v1.3.1：字面量唯一定义点移到 ``artifacts.provenance``（写入方，"以合入结果为
#: 新基线"也要用它），本模块改为**复用**，避免两处字面量漂移。

#: ``code`` 产物的名字（"另存为新基线"默认解析目标）。
CODE_ARTIFACT_NAME = "code"


@router.get(
    "/projects/{project_id}/artifacts",
    response_model=ApiResponse[PageData[ArtifactOut]],
    summary="项目产物列表",
)
async def list_project_artifacts(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")
    artifacts = await store.list_artifacts(project_id, tenant_id=tenant_id)
    items = [ArtifactOut.model_validate(artifact) for artifact in artifacts]
    return {"code": 0, "data": {"items": items, "total": len(items)}}


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ApiResponse[ArtifactDetailOut],
    summary="产物详情（含版本列表）",
)
async def get_artifact(
    artifact_id: str,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    artifact, versions = await store.get_artifact_detail(artifact_id, tenant_id=tenant_id)
    detail = ArtifactDetailOut(
        **ArtifactOut.model_validate(artifact).model_dump(),
        versions=[ArtifactVersionOut.model_validate(row) for row in versions],
    )
    return {"code": 0, "data": detail}


@router.get(
    "/artifacts/{artifact_id}/versions/{version}/content",
    response_model=ApiResponse[ArtifactContentOut],
    summary="版本内容（无 path 返回文件清单，有 path 返回单文件）",
)
async def get_version_content(
    artifact_id: str,
    version: int,
    tenant_id: TenantDep,
    store: ArtifactsDep,
    path: str | None = None,
) -> dict[str, object]:
    loaded = await store.load(artifact_id, version, tenant_id=tenant_id)
    if path is None:
        data = ArtifactContentOut(
            artifact_id=artifact_id,
            version=loaded.version,
            files=sorted(loaded.files),
        )
        return {"code": 0, "data": data}
    if path not in loaded.files:
        raise NotFoundError(f"产物文件不存在: {path}")
    data = ArtifactContentOut(
        artifact_id=artifact_id,
        version=loaded.version,
        path=path,
        content=loaded.files[path],
    )
    return {"code": 0, "data": data}


@router.get(
    "/artifacts/{artifact_id}/diff",
    response_model=ApiResponse[ArtifactDiffOut],
    summary="版本间 diff（unified，按文件分组）",
)
async def get_artifact_diff(
    artifact_id: str,
    v1: int,
    v2: int,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    report = await store.diff(artifact_id, v1, v2, tenant_id=tenant_id)
    data = ArtifactDiffOut(
        artifact_id=artifact_id,
        v1=v1,
        v2=v2,
        files=[FileDiffOut(**item.to_dict()) for item in report.files],
        stats=DiffStatsOut(
            files_changed=report.files_changed,
            additions=report.additions,
            deletions=report.deletions,
        ),
    )
    return {"code": 0, "data": data}


@router.get(
    "/projects/{project_id}/diff-across",
    response_model=ApiResponse[ArtifactDiffOut],
    summary="跨产物对比（基线 vs 合并结果，复用 DiffReport 结构）",
)
async def diff_across(
    project_id: str,
    session: SessionDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
    base: str = "baseline",
    target: str = "code",
) -> dict[str, object]:
    """跨产物 diff（v1.2，docs/09 §4.2.4）：``base`` 与「基线 ⊕ 补丁」的对比。

    ``base`` / ``target`` 形如 ``name`` 或 ``name@version``（缺省 ``baseline`` /
    ``code`` 的最新版本）。返回复用 :class:`ArtifactDiffOut` —— 前端 ``DiffView.vue``
    与 ``stores/artifacts.ts`` **零改动**即可渲染（DoD ⑤）。

    Raises:
        NotFoundError: 项目不存在 / 产物或版本不存在（1002）。
        InvalidParamError: 项目**没有基线**（1001，提示改用同产物 diff）。
    """
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    base_name, base_version = _split_ref(base)
    target_name, target_version = _split_ref(target)

    base_latest = await store.latest_by_name(project_id, base_name, tenant_id=tenant_id)
    if base_latest is None:
        # 无基线项目是**入参问题**（1001，提示改用同产物 diff）；
        # 只有"基线存在但版本不存在"才走 1002。
        if base_name == "baseline":
            raise InvalidParamError(
                "项目没有基线，无法做跨产物对比；请先用 "
                "POST /projects/{id}/baselines 上传基线，或改用 GET /artifacts/{id}/diff"
            )
        raise NotFoundError(f"产物不存在: {base_name}")
    base_loaded = (
        base_latest
        if base_version in (None, base_latest.version)
        else await store.load(base_latest.artifact.id, base_version, tenant_id=tenant_id)
    )
    target_loaded = await _load_named(store, project_id, target_name, target_version, tenant_id)

    if base_loaded.artifact.type != BASELINE_TYPE:
        raise InvalidParamError(
            f"{base_name} 不是基线（type={base_loaded.artifact.type}）；"
            "无基线项目请改用 GET /artifacts/{id}/diff"
        )

    merged = await store.merged_tree(
        project_id, name=target_name, version=target_version or None, tenant_id=tenant_id
    )
    # 复用纯函数，不重写 diff（DoD ①）
    report = diff_file_sets(base_loaded.files, merged)
    data = ArtifactDiffOut(
        artifact_id=target_loaded.artifact.id,
        v1=base_loaded.version,
        v2=target_loaded.version,
        files=[FileDiffOut(**item.to_dict()) for item in report.files],
        stats=DiffStatsOut(
            files_changed=report.files_changed,
            additions=report.additions,
            deletions=report.deletions,
        ),
    )
    return {"code": 0, "data": data}


def _split_ref(raw: str) -> tuple[str, int | None]:
    """``name@version`` -> ``(name, version)``；无 ``@`` 时版本为 ``None``（= latest）。"""
    name, _, version_text = raw.partition("@")
    name = name.strip()
    if not name:
        raise InvalidParamError(f"非法的产物引用: {raw!r}（形如 name 或 name@version）")
    if not version_text:
        return name, None
    try:
        version = int(version_text)
    except ValueError as exc:
        raise InvalidParamError(f"非法的产物版本号: {raw!r}") from exc
    if version < 1:
        raise InvalidParamError(f"产物版本号必须 >= 1: {raw!r}")
    return name, version


async def _load_named(
    store: ArtifactsDep, project_id: str, name: str, version: int | None, tenant_id: str
) -> Any:
    """按「项目 + 名字（+ 版本）」加载产物（不存在 → 1002）。"""
    latest = await store.latest_by_name(project_id, name, tenant_id=tenant_id)
    if latest is None:
        raise NotFoundError(f"产物不存在: {name}")
    if version is None or version == latest.version:
        return latest
    return await store.load(latest.artifact.id, version, tenant_id=tenant_id)


@router.post(
    "/projects/{project_id}/baselines",
    response_model=ApiResponse[BaselineUploadOut],
    status_code=201,
    summary="上传现有工程快照为基线（zip 原始字节，或 JSON files 映射）",
)
async def upload_baseline(
    project_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    """把用户的现有工程落成 ``type=baseline`` 的**不可变产物**（docs/09 §4.2.1）。

    两种入参形态（**都不接受服务端路径**，规避 LFI，§4.5.2）：

    - ``Content-Type: application/zip``（前端 / curl）：请求体 = zip 原始字节；
    - ``Content-Type: application/json``（仅测试 / 脚本）：``{"files": {"a.py": "..."}}``。

    为什么不是 ``multipart/form-data``：FastAPI 的 ``UploadFile`` 依赖
    ``python-multipart``，而 v1.2 明确"Python 依赖零新增"（docs/09 §8）。
    原始字节上传与会话级 multipart 在语义上等价，且前端可直接
    ``axios.post(url, file, {headers: {"Content-Type": "application/zip"}})``。

    Raises:
        NotFoundError: 项目不存在（1002）。
        InvalidParamError: 非 zip / zip slip / 超上限 / 非 UTF-8 / 空基线（1001）。
    """
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    body = await request.body()
    if not body:
        raise InvalidParamError("基线内容为空（请上传 zip 或 JSON files 映射）")

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json" or body.lstrip().startswith(b"{"):
        files, size_bytes = _files_from_json(body, settings=settings)
    else:
        files, size_bytes = read_zip_files(
            body,
            max_files=settings.baseline_max_files,
            max_bytes=settings.baseline_max_bytes,
        )

    # 落盘前校验已完成（路径/上限/编码），store.save 再做一次归一化 + 原子占位
    saved = await store.save(
        project_id=project_id,
        name=BASELINE_ARTIFACT_NAME,
        artifact_type=BASELINE_TYPE,
        files=files,
        created_by="user",
        message=f"用户上传基线（{len(files)} 文件 / {size_bytes} 字节）",
        tenant_id=tenant_id,
    )
    return {
        "code": 0,
        "data": BaselineUploadOut(
            artifact_id=saved.artifact_id,
            version=saved.version,
            file_count=saved.file_count,
            content_hash=saved.content_hash,
            size_bytes=saved.size_bytes,
        ),
    }


def _files_from_json(body: bytes, *, settings: Settings) -> tuple[dict[str, str], int]:
    """解析 ``{"files": {path: content}}``（仅测试/脚本通道）。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidParamError(f"基线 JSON 解析失败: {exc}") from exc
    raw_files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw_files, dict) or not raw_files:
        raise InvalidParamError("基线 JSON 必须形如 {\"files\": {\"path\": \"content\"}}")
    if len(raw_files) > settings.baseline_max_files:
        raise InvalidParamError(
            f"基线文件数 {len(raw_files)} 超过上限 {settings.baseline_max_files}"
        )
    files: dict[str, str] = {}
    size_bytes = 0
    for raw_path, raw_content in raw_files.items():
        if not isinstance(raw_content, str):
            raise InvalidParamError(f"基线文件内容必须是字符串: {raw_path}")
        path = normalize_rel_path(str(raw_path))
        if path in files:
            raise InvalidParamError(f"基线包含重复路径: {path}")
        files[path] = raw_content
        size_bytes += len(raw_content.encode("utf-8"))
    if size_bytes > settings.baseline_max_bytes:
        raise InvalidParamError(
            f"基线内容 {size_bytes} 字节超过上限 {settings.baseline_max_bytes}"
        )
    return files, size_bytes


@router.post(
    "/projects/{project_id}/baselines/from-merge",
    response_model=ApiResponse[BaselineMergeOut],
    status_code=201,
    summary="以合入结果为新基线（从增量产物确定性重建合并树）",
)
async def promote_baseline_from_merge(
    project_id: str,
    payload: BaselineMergeRequest,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    store: ArtifactsDep,
) -> dict[str, object]:
    """把「增量 run 的合入结果」落成**新基线版本**（v1.3.1 T5.3，docs/13 §7.3-8）。

    端点名不是新造的：``docs/09`` §4.3 的接口表早已承诺
    ``POST /projects/{id}/baselines/from-merge``，本版把那句承诺落到代码。

    **薄层纪律**：这里只做「取纯数据 → 调服务 → 组装响应」，重建与六道校验全在
    :func:`ymir.artifacts.provenance.promote_merged_to_baseline`（纯数据进、纯数据出）。

    八步与失败语义：

    ==========================================  ==============================
    步骤                                        失败
    ==========================================  ==============================
    1 项目存在                                 1002
    2 ``run_id`` → Run（须属于该项目）          1002
    3 解析阶段：给 ``stage_id`` 则按其取，否则取
      「本 run 中产出 ``code`` 的最后一次成功
      阶段」（不按 ``role_id`` 硬编码）          1002
    4 从 ``output_refs[0]`` 取产物 id/版本      1002
    5 ``run.context["baseline"]`` 引用完整      1003（非增量 run）
    6 模板可读 → 骨架 / 模板 id                 1002
    7 模板 YAML 指纹现算                        1002（文件已不可读）
    8 调服务（校验 / 重建 / 落盘）              1001 / 1003
    ==========================================  ==============================

    Raises:
        NotFoundError: 项目 / 运行 / 阶段 / 产物引用 / 模板缺失（1002）。
        StateConflictError: 非增量 run、无溯源、指纹不符（1003）。
        InvalidParamError: 非增量产物、超基线上限（1001）。
    """
    project = await ProjectRepository(session).get(project_id, tenant_id=tenant_id)
    if project is None:
        raise NotFoundError("项目不存在")

    run = await RunRepository(session).get(payload.run_id, tenant_id=tenant_id)
    if run is None or run.project_id != project_id:
        raise NotFoundError(f"运行不存在: {payload.run_id}")

    stage_row = await _resolve_promote_stage(
        session, run_id=run.id, stage_id=payload.stage_id, tenant_id=tenant_id
    )
    artifact_id, artifact_version = _promote_artifact_ref(stage_row)
    if payload.artifact_version is not None:
        # 显式版本优先：供「重跑后取历史版本」的场景（步骤 4 的覆盖语义）
        artifact_version = payload.artifact_version

    base_ref = _promote_baseline_ref(run)
    skeleton, template_id, template_hash = _promote_template_ref(run)

    result = await promote_merged_to_baseline(
        store,
        project_id=project_id,
        artifact_id=artifact_id,
        version=artifact_version,
        base_ref=base_ref,
        skeleton=skeleton,
        template_id=template_id,
        template_hash=template_hash,
        run_id=run.id,
        max_files=settings.baseline_max_files,
        max_bytes=settings.baseline_max_bytes,
        tenant_id=tenant_id,
    )
    return {
        "code": 0,
        "data": BaselineMergeOut(
            artifact_id=result.artifact_id,
            version=result.version,
            file_count=result.file_count,
            content_hash=result.content_hash,
            size_bytes=result.size_bytes,
            reused=result.reused,
        ),
    }


async def _resolve_promote_stage(
    session: AsyncSession,
    *,
    run_id: str,
    stage_id: str | None,
    tenant_id: str,
) -> StageExecution:
    """解析"要提升哪一次执行"（步骤 3，两种入口）。

    ★ 未给 ``stage_id`` 时**不按 ``role_id`` 硬编码**（如"取 role=engineer 那条"）：
    阶段 id / 角色名是流水线配置，自定义流水线上会直接失效；判据只能是"这条阶段的
    产物就是 ``code``"（且必须是**成功**态，否则产物不完整）。

    Raises:
        NotFoundError: 该 run 没有匹配的成功阶段（1002）。
    """
    repo = StageExecutionRepository(session)
    if stage_id:
        row = await repo.latest(run_id, stage_id, tenant_id=tenant_id)
        if row is None:
            raise NotFoundError(f"阶段执行记录不存在: {stage_id}")
        return row
    found = await repo.latest_success_with_output(run_id, CODE_ARTIFACT_NAME, tenant_id=tenant_id)
    if found is None:
        raise NotFoundError("该运行还没有产出 code 的成功阶段")
    return found


def _promote_artifact_ref(stage_row: StageExecution) -> tuple[str, int]:
    """从阶段记录的 ``output_refs[0]`` 取 ``(artifact_id, version)``（步骤 4）。"""
    refs = stage_row.output_refs if isinstance(stage_row.output_refs, list) else []
    head = refs[0] if refs else None
    if not isinstance(head, dict):
        raise NotFoundError(f"阶段 {stage_row.stage_id} 没有可用的产物引用")
    artifact_id = str(head.get("artifact_id") or "").strip()
    raw_version = head.get("version")
    version = raw_version if isinstance(raw_version, int) else None
    if not artifact_id or version is None:
        raise NotFoundError(f"阶段 {stage_row.stage_id} 的产物引用不完整")
    return artifact_id, version


def _promote_baseline_ref(run: PipelineRun) -> dict[str, object]:
    """取 ``run.context["baseline"]`` 的纯数据形态（步骤 5）。

    ``content_hash`` 与 ``artifact_id@version`` **缺一不可**：前者是"不许拿最新基线
    顶上"的三方比对依据，后者是唯一允许的加载入口（docs/09 §4.3 两条铁律）。
    """
    context = run.context if isinstance(run.context, dict) else {}
    ref = context.get("baseline")
    if not isinstance(ref, dict):
        raise StateConflictError("无法确定基线引用（该 run 不是增量运行）")
    artifact_id = str(ref.get("artifact_id") or "").strip()
    content_hash = str(ref.get("content_hash") or "").strip()
    if not artifact_id:
        raise StateConflictError("无法确定基线引用（该 run 不是增量运行）")
    if not content_hash:
        # v1.3.1（T6.3-C）：这里与 `artifacts.provenance._load_pinned_baseline` 同步
        # 区分两种原因。原来一律说「该 run 不是增量运行」，把"字段没写全"误诊成
        # "没有基线"（T6.2 真跑里 CLI 发起的 R-b 就是这样被误导的）。
        raise StateConflictError(
            "无法确定基线引用：run 的基线引用缺少 `content_hash`"
            "（CLI 发起时可能未写全），无法做三方比对"
        )
    raw_version = ref.get("version")
    return {
        "artifact_id": artifact_id,
        "version": raw_version if isinstance(raw_version, int) else None,
        "content_hash": content_hash,
    }


def _promote_template_ref(run: PipelineRun) -> tuple[dict[str, str], str, str]:
    """取模板的骨架 / id / YAML 指纹（步骤 6、7）。

    ``template_hash`` 在**这里**现算（API 层 rank 0，可合法 import ``templates``）：
    ``artifacts``(3) 不能 import ``templates``(2)（逆层），所以哈希函数收目录入参。

    Returns:
        ``(skeleton, template_id, template_hash)``；无模板 ⇒ ``({}, "", "")``。

    Raises:
        NotFoundError: 模板声明了 id 但已删除 / 改坏 / 文件不可读（1002）。
    """
    context = run.context if isinstance(run.context, dict) else {}
    ref = context.get("template")
    template_id = str(ref.get("id") or "").strip() if isinstance(ref, dict) else ""
    if not template_id:
        return {}, "", ""
    spec = template_spec_for_run(run)
    if spec is None:
        raise NotFoundError(f"模板不存在或不可读: {template_id}")
    template_hash = template_yaml_hash(DEFAULT_TEMPLATES_DIR, template_id)
    if not template_hash:
        raise NotFoundError(f"模板文件不可读: {template_id}.yaml")
    return dict(spec.skeleton_files()), template_id, template_hash

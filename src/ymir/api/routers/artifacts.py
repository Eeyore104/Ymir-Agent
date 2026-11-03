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
import logging
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

logger = logging.getLogger(__name__)

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
    # ---- v1.4 T2.1（docs/15 §3.2）：合并树健康度闸门 ----
    # ★ 只**复用** run 已有的评测结论（``evaluation_report.json`` 里的
    #   ``blocking_failed`` / ``regression``）⇒ 零额外沙箱调用。
    # ★ 这里**不**现跑探针（``health_probe=None``）：HTTP 提升动作没有 run 级沙箱，
    #   为一个低频只读动作临时起一个沙箱会话（最长 120s）不划算，且会在集成测试里
    #   真的在宿主环境跑模板 checks。
    # ★★ 因此 ``strict_health=True``（v1.4 QA 返修 P1-3）：拿不到结论 ⇒ 409
    #   ``baseline_health_unknown``，``force=true`` 才放行并留痕。首版传 ``None``
    #   探针 + "未知放行" ⇒ **不评测即可绕过整个闸门**（比没闸门更糟：给出已校验的错觉）。
    # ★ ``tenant_id`` 必须透传（P2-4）：首版守默认值，非默认租户下闸门静默失效。
    # ★★ ``artifact_version`` 必须透传（返修 3，P1）：报告只有**评过同一版树**
    #   才算数；只按 ``run_id`` 匹配会把"另一版树的结论"当成这棵树的（判据串味）。
    #   这里传的是**已解析的**版本（``payload.artifact_version`` 覆盖之后的值）——
    #   与下面 ``promote_merged_to_baseline(version=artifact_version)`` 完全同一口径。
    # ★ 返修 5（Q-R5-03）：还要绑定**产物身份** —— 同一 run 里"对别的产物（版本号
    #   恰好相等）评测一次"不能给这棵树背书（返修 4 自己打开的口子，返修前是
    #   fail-closed）。名称取自产物元数据；读不到就传 None（退化为"只绑版本"）。
    try:
        artifact_meta, _ = await store.get_artifact_detail(artifact_id, tenant_id=tenant_id)
        artifact_name: str | None = str(getattr(artifact_meta, "name", "") or "") or None
    except Exception:  # noqa: BLE001 - 附加守卫读不到名称时不该让提升 500
        artifact_name = None
    known_health = await _known_merged_health(
        store,
        project_id=project_id,
        run_id=run.id,
        tenant_id=tenant_id,
        artifact_version=artifact_version,
        artifact_name=artifact_name,
    )

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
        force=payload.force,
        known_health=known_health,
        health_probe=None,
        strict_health=True,
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
            warning=result.warning,
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


# ------------------------------------------------- v1.4 T2.1：合并树健康度（★3 闸门）


#: 评测报告产物名（与 ``api.routers.evaluations.REPORT_ARTIFACT_NAMES`` 同值）。
#:
#: 刻意复写而不 import：两个 router 互相 import 会形成环（``evaluations`` 也依赖
#: ``artifacts`` 侧的助手），而这两个名字是**产物名契约**（``EvaluationReport.save``
#: 的 ``name`` 参数），改动频率极低；两侧各有注释互指。
_REPORT_ARTIFACT_NAMES: tuple[str, ...] = ("evaluation_report", "eval_verdict")

#: 评测报告里装「合并树健康结论」的 JSON 正文名。
_REPORT_JSON_NAME: str = "evaluation_report.json"

#: 合并树健康度三态字面量（与 ``artifacts.provenance.HEALTH_*`` **同源**）。
#:
#: ★ 复写而不 import 的理由同上（本模块已 import 了 ``provenance`` 的部分名字，这两
#: 个字面量刻意不 import 以免"看起来像契约"）：``api``(0) -> ``artifacts``(3) 是**顺**
#: 层，技术上可以 import；但这两个值只在本文件的判定里用，复写更不易漂移（一处改动
#: 就会同时被抓：``tests/test_baseline_promote.py::TestMergedHealthFromReport``）。
_HEALTHY: str = "healthy"
_UNHEALTHY: str = "unhealthy"


def _report_health_status(
    raw: str | None,
    expected_version: int | None = None,
    expected_artifact: str | None = None,
) -> str:
    """从 ``evaluation_report.json`` 正文里取**合并树**健康结论（坏数据 ⇒ ``""``）。

    ★★ **v1.4 QA 返修 P0-1：方向反了，必须改口径。**
    首版读的是报告里的 ``baseline_runnability.status``，但那个值探的是**基线**
    （``evaluation/harness.py`` 里 ``_probe(task, base_files)``，``base_files`` 来自
    ``_resolve_baseline(...)``）。等价于拿 ``health(B_N)`` 去判断 ``M_N``：

    - 基线健康、合并树坏 ⇒ 放行坏树（正是 R-c1 的形态）；
    - 基线坏、合并树好 ⇒ 误拦。
    比没有闸门更糟：它给出"已校验"的错觉。

    **合并树的结论就在同一份报告里**（``evaluation.report.TaskOut``）：

    - ``blocking_failed`` 非空 ⇒ 阻断项失败 ⇒ ``unhealthy``；
    - ``regression=True`` ⇒ 合并树比基线更差 ⇒ ``unhealthy``；
    - 报告存在且两者都空 ⇒ ``healthy``。

    ★★ **v1.4 返修 3（P1）：报告必须与**被提升的 artifact 版本**绑定。**
    ``HarnessEvaluationGate.evaluate`` 评的是 artifact 的**当时最新版本**，而
    ``_known_merged_health`` 原先只按 ``run_id`` 匹配 ⇒ 同一 run 的报告可能对应
    **另一版树**（真跑 ``data/v14/INDEX.md`` §4.5：为 run ``29f52b8f`` 补评实际评了
    code v4，而提升的是 v3）。用 v4 的结论去闸 v3 的树就是**判据串味**：v4 修好了
    不代表 v3 能跑，v3 的结论也不该由 v4 的报告给出。

    因此报告根部的 ``artifact_version``（``EvaluationReport.artifact_version``）必须
    等于本次要提升的版本，否则一律按"拿不到结论"处理：

    Args:
        raw: 报告 JSON 正文（可空 / 可损坏）。
        expected_version: 本次要提升的 artifact 版本；``None`` ⇒ 不做版本绑定
            （只有"不知道提升哪一版"的调用方才该传 ``None``，HTTP 主路径恒传具体值）。
        expected_artifact: 本次要提升的**产物名**；报告若声明了别的产物（
            ``tasks[].artifact_ref.name``）⇒ 不采信（见 :func:`_report_artifact_matches`，
            ``None`` ⇒ 不做产物绑定）。

    Returns:
        ``healthy`` / ``unhealthy`` / ``""``（拿不到结论 / 版本或产物不匹配）。

    Note:
        **绝不抛**：报告正文可能被人工改过 / 是旧版本格式。读不出来就返回 ``""``，
        由调用方按 fail-closed（HTTP 主路径）或放行（探针路径）处置。
        报告里**没有任何已评测任务**（空 ``tasks`` / 全是 ``skipped``）也算拿不到结论
        —— "没评测"不等于"跑得好"。
        报告**缺** ``artifact_version``（旧格式）同样按不匹配处理 —— **不猜**：
        猜错的代价是"用别的树"的结论去放行坏树。
    """
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if not _report_version_matches(payload, expected_version):
        return ""
    if not _report_artifact_matches(payload, expected_artifact):
        return ""
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ""
    evaluated = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").strip().lower()
        if status and status != "evaluated":
            continue  # 跳过项没有跑过检查，不能拿它当"健康"的证据
        evaluated += 1
        blocking = task.get("blocking_failed")
        if isinstance(blocking, list) and any(str(item or "").strip() for item in blocking):
            return _UNHEALTHY
        if task.get("regression") is True:
            return _UNHEALTHY
    return _HEALTHY if evaluated else ""


def _report_artifact_matches(payload: dict[str, Any], expected_artifact: str | None) -> bool:
    """报告若**声明了**评的是哪个产物，就必须与本次要提升的产物同名（``None`` ⇒ 不绑定）。

    ★ 为什么需要（QA R5 `Q-R5-03`，P3）：返修 4 之后 API 报告带上了
    ``artifact_version``，而 ``_known_merged_health`` 只按 ``(run_id, 版本号)`` 匹配 ——
    于是"对**同 run 的另一个产物**（版本号恰好相等）评测一次"就能给这棵树背书：
    对 ``prd`` v1 的 healthy 结论会被当成 ``code`` v1 的结论，坏代码树可**静默**提升
    （无 ``force``、无 ``warning``）。返修 4 之前这条路是 fail-closed（版本恒 ``null``），
    所以这是**本轮自己打开的口子**。

    判据来源：真实报告天然带 ``tasks[].artifact_ref``（``harness._load_task`` 写入
    ``{"artifact_id","name","version"}``）。**没声明不否决** —— 合成/手写夹具可能没有这
    一栏，而"没写"不等于"别的产物"；一旦写了别的产物名，结论就不能给这棵树背书。
    这条不作安全边界（本地产物本来就可人工改），只作"判据不串味"的守卫。
    """
    if not expected_artifact:
        return True
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return True
    for task in tasks:
        if not isinstance(task, dict):
            continue
        ref = task.get("artifact_ref")
        if not isinstance(ref, dict):
            continue
        name = str(ref.get("name") or "").strip()
        if name and name != expected_artifact:
            logger.warning(
                "报告评测的产物不是本产物：report=%s promote=%s ⇒ 按未知处置",
                name,
                expected_artifact,
            )
            return False
    return True


def _report_version_matches(payload: dict[str, Any], expected_version: int | None) -> bool:
    """报告的 ``artifact_version`` 是否等于本次要提升的版本（``None`` ⇒ 不绑定）。

    ★ 缺失 / ``null`` / 非整数一律**不匹配**：旧格式报告没有这个字段，而"不猜"的
    代价只是 fail-closed（409 + ``force`` 人工通道），猜错的代价是放行坏树。
    """
    if expected_version is None:
        return True
    raw_version = payload.get("artifact_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        logger.warning(
            "合并树健康报告缺少 artifact_version，无法绑定到 promote v%s ⇒ 按未知处置",
            expected_version,
        )
        return False
    if raw_version != expected_version:
        logger.warning(
            "报告版本不匹配：report=v%s promote=v%s ⇒ 按未知处置（避免用别的树的结论）",
            raw_version,
            expected_version,
        )
        return False
    return True


async def _known_merged_health(
    store: Any,
    *,
    project_id: str,
    run_id: str,
    tenant_id: str,
    artifact_version: int | None,
    artifact_name: str | None,
) -> str:
    """复用**该 run 已有评测报告**里的**合并树**健康结论（零额外沙箱调用）。

    设计口径是"优先复用 run 自身已有的结论"：评测阶段已经真的跑过（并记录了
    ``blocking_failed`` / ``regression``），提升时再跑一遍纯属重复付费。

    ★ **v1.4 QA 返修**三处：

    - P0-1：判据换成 ``blocking_failed`` / ``regression``（见
      :func:`_report_health_status`），不再读 ``baseline_runnability``；
    - P2-4：``tenant_id`` 必须**透传**给 ``list_artifacts`` / ``load``。
      首版守默认值 ⇒ 非默认租户下这里永远返回 ``""``（闸门静默失效）；
    - **P1-A（第二次返修）**：**必须遍历报告产物的全部版本**。``EvaluationReport.save``
      固定用 ``name="evaluation_report"``，``store.save`` 按 ``(project, name)`` 复用
      artifact 递增版本 ⇒ 一个项目只有 **1 个** 报告 artifact、N 次评测 = N 版。
      首版调 ``store.load(artifact.id)`` 不传版本 ⇒ 取 ``artifact.current_version``
      （**最近一次**评测）⇒ 除最近一次外，任何 run 都拿不到自己的报告：
      更早 run 每次提升都被迫 ``force``，且 reason 会说"没有健康结论"（把已经评测过、
      结论是 unhealthy 的树指向"补评测"而不是"修代码"）。

    ★ **返修 3（P1）：报告必须绑定到被提升的 ``artifact_version``。**
    ``artifact_version`` 是**必填**关键字（不给默认 ``None``）：漏传等于闸门静默退回
    "只看 run_id"的串味口径，且没有任何报错 —— 这类"忘传就失效"的参数不该有默认值。
    同一 run 落了多份报告时，**从新到旧**找第一份"版本匹配且能给出结论"的；都不匹配
    就返回 ``""``（fail-closed ⇒ 409 ``baseline_health_unknown``）。

    ★ **返修 5（Q-R5-03）：``artifact_name`` 同样是必填关键字。**
    只绑 ``run_id + 版本号`` 时，"对同 run 的**另一个产物**（版本号恰好相等）评测一次"
    就能给这棵树背书（实测：``prd`` v1 的 healthy 结论被当成 ``code`` v1 的结论，
    坏代码树静默提升）。``None`` 表示调用方确实不知道产物名（退化为只绑版本）。

    Returns:
        ``healthy`` / ``unhealthy`` / ``""``（拿不到 / 报告版本与被提升版本不匹配）。
        **任何异常都降级成 ``""``** —— 这只是省成本的旁路，不能让 I/O 抖动变成 500。
    """
    try:
        artifacts = await store.list_artifacts(project_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - 旁路：读不到就按"没结论"处置
        return ""
    for artifact in artifacts:
        if str(getattr(artifact, "name", "")) not in _REPORT_ARTIFACT_NAMES:
            continue
        # ★ P1-A：拿**全部版本行**（含各自 ``run_id``），而不是只加载 current_version。
        try:
            _artifact, versions = await store.get_artifact_detail(
                artifact.id, tenant_id=tenant_id
            )
        except Exception:  # noqa: BLE001 - 单个产物读不出来不影响其它候选
            continue
        matched = [
            row for row in versions if str(getattr(row, "run_id", "") or "") == run_id
        ]
        if not matched:
            # ★ 只认同一个 run 的报告：别的 run 的健康度与这棵树无关
            #   （P2-6：这条判据必须有反向用例，否则"跨 run 串味"无测试守护；
            #    P1-A 之后反向用例还必须断言"自己的看得到"，否则
            #    "永远返回空"也能让隔离用例变绿）
            continue
        # ``list_versions`` 按版本号升序 ⇒ 同一 run 落了多份报告时**先看最新**那一版；
        # 最新那版若是"别的 artifact 版本"的报告（返修 3 的串味形态），继续往旧版找。
        for version_row in reversed(matched):
            try:
                loaded = await store.load(
                    artifact.id, int(version_row.version), tenant_id=tenant_id
                )
            except Exception:  # noqa: BLE001 - 单个版本读不出来不影响其它候选
                continue
            status = _report_health_status(
                loaded.files.get(_REPORT_JSON_NAME), artifact_version, artifact_name
            )
            if status:
                return status
    return ""

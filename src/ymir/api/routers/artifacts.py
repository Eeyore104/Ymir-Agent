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
from dataclasses import dataclass
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
        # ★ v1.5（T02.2）：判据也要绑定 —— 报告必须自证"用哪把尺子"，否则
        #   别的模板的报告不能给这棵树背书（判据不同源 = 不可比）。
        expected_template_id=template_id,
        expected_template_hash=template_hash,
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
        known_health=known_health.status,
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
            warning=_merge_warning_codes(
                result.warning,
                known_health.divergent,
                # ★ D-1：没做过回归判定 ⇒ 必须披露（否则"没验"与"没回归"长得一样）
                regression_unknown=known_health.regression_checked is False,
            ),
        ),
    }


def _merge_warning_codes(
    business_code: str,
    divergent: bool,
    regression_unknown: bool = False,
) -> str:
    """把「业务留痕码」与「披露码」用 ``,`` 拼成一串（v1.5 T02.3 / 修复轮 D-1）。

    ★ 顺序**固定**（前端按 ``,`` 拆分后逐码匹配，顺序稳定才能写快照断言）：
    ``业务码`` → ``criteria_diverged:1`` → ``baseline_promoted_regression_unknown:1``。
    ★ ``divergent`` 与业务码**正交**：两份报告结论不一致时，无论胜者是
    ``healthy`` / ``unverified`` 还是 ``unhealthy`` 都会披露。因此
    ``force`` + ``unhealthy`` **且存在分歧**时是
    ``baseline_promoted_unhealthy:1,criteria_diverged:1``；**只有一份报告**时仍是
    恰好一个码（既有断言不破 —— 那几条用例本来就只铺了一份报告）。
    ★ ``regression_unknown``（D-1）：采纳的报告没做过回归判定 ⇒ 追加披露码。
    它可能**单独出现**（树 healthy 但回归没验过）—— 那正是最需要解释的一档。

    Args:
        business_code: ``PromoteResult.warning``（空串 = 无业务留痕）。
        divergent: 两个产物名的结论是否不一致。
        regression_unknown: 采纳的报告是否**没做过**回归判定。

    Returns:
        逗号分隔的码串；都无 ⇒ ``""``（``BaselineMergeOut`` 会把空串键摘掉）。
    """
    codes = [code for code in (str(business_code or ""),) if code]
    if divergent:
        codes.append(CRITERIA_DIVERGED_CODE)
    if regression_unknown:
        codes.append(BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE)
    return ",".join(codes)


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

#: 报告产物的**读取优先级**（v1.5 T02.1，docs/17 §1.1.5）。
#:
#: ★★ 这是本轮最关键的一行：优先级**由 tuple 顺序显式表达**，不再依赖
#: ``list_artifacts`` 的 ``created_at asc``。旧实现按创建顺序遍历 + "第一份能给出
#: 结论的就 return" ⇒ ``evaluation_report``（QA 阶段产物，artifact 更早创建）会先于
#: ``eval_verdict`` 被读到；而补评测的 ``evaluation_report`` 一旦给出 ``healthy``，
#: 用户"对坏树点一次补评测"就能让闸门放行（v1.4 §0.3 那条静默旁路）。
#:
#: 产品口径（PRD T2-R3）：``eval_verdict``（gate 当时基于同一棵树的判定）>
#: ``evaluation_report``（事后补评）> 无结论。
_REPORT_PRIORITY: tuple[str, ...] = ("eval_verdict", "evaluation_report")

#: 评测报告里装「合并树健康结论」的 JSON 正文名。
_REPORT_JSON_NAME: str = "evaluation_report.json"

#: 合并树健康度四态字面量（与 ``artifacts.provenance.HEALTH_*`` **同源**）。
#:
#: ★ 复写而不 import 的理由同上（本模块已 import 了 ``provenance`` 的部分名字，这两
#: 个字面量刻意不 import 以免"看起来像契约"）：``api``(0) -> ``artifacts``(3) 是**顺**
#: 层，技术上可以 import；但这两个值只在本文件的判定里用，复写更不易漂移（一处改动
#: 就会同时被抓：``tests/test_baseline_promote.py::TestMergedHealthFromReport``）。
_HEALTHY: str = "healthy"
_UNHEALTHY: str = "unhealthy"
#: ★ v1.5 第四态（T02.2）：某组某档一条检查都没真的执行 ⇒ "我确实不知道"。
#: ``evaluation.report`` 与 ``artifacts.provenance`` 同名同值（跨层不能 import，
#: 三处字面量由注释互指）。
_UNVERIFIED: str = "unverified"
#: 探针整体未执行（``baseline_probe.BASELINE_SKIPPED`` 同值）：回归**无法**判定。
_BASELINE_SKIPPED: str = "skipped"

#: 判据来源枚举（与 ``evaluation.report.CRITERIA_SOURCE_*`` **同源**）。
#: ``artifacts``(3) -> ``evaluation``(1) 是逆层，CI 拦；故复写并互指注释。
_CRITERIA_TEMPLATE: str = "template"
_CRITERIA_SUITE: str = "suite"

#: 「回归未检查」的披露短码（v1.5 修复轮 D-1）。
#:
#: ★ 触发条件：采纳的报告里**没有** ``baseline_runnability``（探针没跑）或它是
#: ``skipped`` —— 此时 ``regression`` 恒 ``False``，"没做回归判定"与"没有回归"
#: 长得一样。那一版的提升若不披露，就是**静默**放行（v1.5 修复轮实测：只有
#: ``evaluation_report``、没有 ``eval_verdict`` 时，仅 D3 回归的树被判 healthy ⇒ 201 无 warning）。
#: 本版**只披露不根治**（根治要把 ``baseline_ref`` 引进项目维度，属 v1.6 范围）。
BASELINE_PROMOTED_REGRESSION_UNKNOWN_CODE: str = "baseline_promoted_regression_unknown:1"

#: 「两份报告的结论不一致」的披露短码（v1.5 T02.3，读侧唯一产生点）。
#:
#: ★ 为什么只能读侧产生：写侧（gate / 补评测）各自只看得到自己那一份，无从比较。
#: 与 ``baseline_promoted_*`` 用 ``,`` 拼装（``,`` 是响应里 `warning` 的分隔符）。
CRITERIA_DIVERGED_CODE: str = "criteria_diverged:1"


@dataclass(frozen=True)
class MergedHealthReading:
    """读取侧对「该 run 这份树」的健康度读取结果（v1.5 T02.1，纯数据）。

    Attributes:
        status: ``healthy`` / ``unhealthy`` / ``unverified`` / ``""``（拿不到结论）。
        source_name: 采纳了哪个产物名的报告（``eval_verdict`` / ``evaluation_report``；
            ``""`` = 没采纳任何一份）。**排障用**：解释"为什么这次是这个结论"。
        divergent: 两个产物名的结论**不一致**（v1.5 §1.1.5 的有界补读：胜者确定后
        **最多再读一次**败者最新匹配版）。披露用，**不改变采纳结果** —— 冲突时永远
        取优先级更高的那份（PRD T2-R3）。
    """

    status: str = ""
    source_name: str = ""
    divergent: bool = False
    #: 采纳的那份报告**做过回归判定**吗（v1.5 修复轮 D-1）。``False`` ⇒ 该提升要在
    #: ``warning`` 里披露 ``baseline_promoted_regression_unknown:1``（不静默）。
    regression_checked: bool = True


def _report_health_status(  # noqa: PLR0913 - 判据维度就是这么多（版本/产物/模板/指纹）
    raw: str | None,
    expected_version: int | None = None,
    expected_artifact: str | None = None,
    expected_template_id: str | None = None,
    expected_template_hash: str | None = None,
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
    - v1.5：``unverified_groups`` 非空（且上面两条都不成立）⇒ ``unverified``；
    - 报告存在且都空 ⇒ ``healthy``。

    ★★ **v1.4 返修 3（P1）：报告必须与**被提升的 artifact 版本**绑定。**
    ``HarnessEvaluationGate.evaluate`` 评的是 artifact 的**当时最新版本**，而
    ``_known_merged_health`` 原先只按 ``run_id`` 匹配 ⇒ 同一 run 的报告可能对应
    **另一版树**（真跑 ``data/v14/INDEX.md`` §4.5：为 run ``29f52b8f`` 补评实际评了
    code v4，而提升的是 v3）。用 v4 的结论去闸 v3 的树就是**判据串味**：v4 修好了
    不代表 v3 能跑，v3 的结论也不该由 v4 的报告给出。

    ★★ **v1.5（T02.2）：报告必须自证**判据来源**（PRD T2-R2 / A11 / A12 反向）。**
    只绑"哪一版树"不够 —— 还要绑"用哪把尺子"。``criteria_source`` 必须是
    ``template`` / ``suite`` 两个显式字面量之一；调用方给了
    ``expected_template_id``（HTTP 主路径恒给，来自被提升 run 的模板）时，报告必须
    声明 ``criteria_source="template"`` **且** ``template_id`` 相等；
    给了 ``expected_template_hash`` 时 ``criteria_version`` 必须相等。
    不满足 ⇒ ``""``（判据不同源 = 不可比，不需要额外机制）。
    **v1.4 及之前生成的报告没有这三个字段 ⇒ 一律 ``""``**（A11：fail-closed，
    用户重评一次即获得强口径）。

    Args:
        raw: 报告 JSON 正文（可空 / 可损坏）。
        expected_version: 本次要提升的 artifact 版本；``None`` ⇒ 不做版本绑定
            （只有"不知道提升哪一版"的调用方才该传 ``None``，HTTP 主路径恒传具体值）。
        expected_artifact: 本次要提升的**产物名**；报告若声明了别的产物（
            ``tasks[].artifact_ref.name``）⇒ 不采信（见 :func:`_report_artifact_matches`，
            ``None`` ⇒ 不做产物绑定）。
        expected_template_id: 本次要提升的树**归属的模板 id**；``None`` / 空串 ⇒
            不做判据绑定（兼容既有纯函数用例与"无模板 run"）。
        expected_template_hash: 该模板**现算**的 YAML 指纹（与报告里的
            ``criteria_version`` 对比）；``None`` / 空串 ⇒ 不做指纹绑定。

    Returns:
        ``healthy`` / ``unhealthy`` / ``unverified`` / ``""``（拿不到结论 / 版本 /
        产物 / 判据不匹配）。

    Note:
        **绝不抛**：报告正文可能被人工改过 / 是旧版本格式。读不出来就返回 ``""``，
        由调用方按 fail-closed（HTTP 主路径）或放行（探针路径）处置。
        报告里**没有任何已评测任务**（空 ``tasks`` / 全是 ``skipped``）也算拿不到结论
        —— "没评测"不等于"跑得好"；而"任务跑了但某组某档一条检查都没执行"是
        ``unverified``（"没验证过"）—— 两者**必须分开**。
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
    if not _criteria_matches(payload, expected_template_id, expected_template_hash):
        return ""
    if not _report_version_matches(payload, expected_version):
        return ""
    if not _report_artifact_matches(payload, expected_artifact):
        return ""
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ""
    evaluated = 0
    unverified = False
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
        groups = task.get("unverified_groups")
        if isinstance(groups, list) and any(str(item or "").strip() for item in groups):
            # v1.5：任务**跑了**，但某组某档的检查一条都没真的执行 ⇒ 未验证
            unverified = True
    if not evaluated:
        return ""
    return _UNVERIFIED if unverified else _HEALTHY


def _report_regression_checked(payload: dict[str, Any]) -> bool:
    """该报告是否**做过回归判定**（v1.5 修复轮 D-1）。

    判据只用一个**既有**字段，不新增报告键（避免动字节契约）：``TaskOut`` 的
    ``baseline_runnability``（探针结果）。三种情形：

    - 键不存在 ⇒ 探针**根本没跑**（项目维度没有 ``baseline_ref``，或基线正文不可用）
      ⇒ 回归未检查；
    - ``status == "skipped"`` ⇒ 探针跑了但一条检查都没执行（环境缺失 / 基线不完整）
      ⇒ 也无法判回归 ⇒ 回归未检查；
    - 其余（``healthy`` / ``unhealthy``）⇒ 检查过了。

    ★ 只要**有一个已评测任务**做过回归判定就算"检查过"（与 ``_report_health_status``
    的"任一任务给坏结论即坏"同向：宽松判定只会在所有任务都没判过时成立）。
    """
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").strip().lower()
        if status and status != "evaluated":
            continue
        probe = task.get("baseline_runnability")
        if isinstance(probe, dict) and str(probe.get("status") or "") != _BASELINE_SKIPPED:
            return True
    return False


def _criteria_matches(
    payload: dict[str, Any],
    expected_template_id: str | None,
    expected_template_hash: str | None,
) -> bool:
    """报告自证的判据来源是否与「本次要提升的树」同源（``""`` 视为不同源）。

    ★ 判据来源必须是**显式字面量**（PRD R5 / A11）：不接受"字段缺失 ⇒ 弱口径"这种
    隐式判定 —— 隐式判定在下一版会重演 v1.4 §0.3 那类静默旁路，而且没有测试抓手。

    ★ 只绑"报告自称的"判据（模板侧现算 vs 报告里记的值）。本地产物本来就可人工改写：
    这条判据防的是**串味**，不是**伪造**（与 ``docs/16`` §1 第 8 条同口径）。
    """
    source = str(payload.get("criteria_source") or "").strip().lower()
    if source not in {_CRITERIA_TEMPLATE, _CRITERIA_SUITE}:
        logger.warning(
            "报告未自证判据来源（criteria_source=%r）⇒ 按未知处置（A11 fail-closed）",
            payload.get("criteria_source"),
        )
        return False
    expected = str(expected_template_id or "").strip()
    if not expected:
        # 调用方不知道这棵树属于哪个模板（无模板 run / 老调用方）⇒ 不绑判据。
        return True
    if source != _CRITERIA_TEMPLATE:
        logger.warning(
            "报告用的是套件口径（criteria_source=suite），而本次提升的树归属模板 %s "
            "⇒ 判据不同源，按未知处置",
            expected,
        )
        return False
    if str(payload.get("template_id") or "").strip() != expected:
        logger.warning(
            "报告的判据模板不是本树的模板：report=%s promote=%s ⇒ 按未知处置",
            payload.get("template_id"),
            expected,
        )
        return False
    expected_hash = str(expected_template_hash or "").strip()
    if expected_hash and str(payload.get("criteria_version") or "").strip() != expected_hash:
        logger.warning(
            "报告的判据版本与当前模板指纹不符（模板已变更）⇒ 按未知处置，请重评一次"
        )
        return False
    return True


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


async def _known_merged_health(  # noqa: PLR0913 - 绑定的维度见 docstring
    store: Any,
    *,
    project_id: str,
    run_id: str,
    tenant_id: str,
    artifact_version: int | None,
    artifact_name: str | None,
    expected_template_id: str | None = None,
    expected_template_hash: str | None = None,
) -> MergedHealthReading:
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

    ★ **返修 3（P1）**：报告必须绑定到被提升的 ``artifact_version``（同一 run 落了
    多份报告时，**从新到旧**找第一份"版本匹配且能给出结论"的）。

    ★ **返修 5（Q-R5-03）**：``artifact_name`` 同样参与绑定（同 run 的别的产物不给
    这棵树背书）。``None`` 表示调用方确实不知道产物名（退化为只绑版本）。

    ★★ **v1.5（T02.1，docs/17 §1.1.5）：遍历结构从"按 artifact 创建顺序"改为
    "按产物名优先级成组"。**

    ```
    for name in _REPORT_PRIORITY:          # ★ 优先级 = 这个循环的顺序
        for artifact in by_name[name]:
            for row in reversed(matched_versions(run_id)):   # 同 run 内版本从新到旧
                if status: 采纳并跳出
    ```

    旧结构按 ``created_at asc`` 遍历 + 首个结论即返回 ⇒ ``evaluation_report``
    （QA 阶段产物，artifact 更早创建）会先于 ``eval_verdict`` 被读到，而补评测的
    报告 ``blocking_failed`` 恒空 ⇒ ``healthy`` ⇒ **闸门被旁路且静默**。
    现在优先级是 tuple 的字面量顺序：谁先创建都不影响结论（A10 的双向保证）。

    Args:
        store: 产物库。
        project_id: 项目 id。
        run_id: 归属 run（只认同一 run 的报告）。
        tenant_id: 租户（**必须透传**，否则非默认租户下闸门静默失效）。
        artifact_version: 本次要提升的版本（``None`` ⇒ 不绑版本，仅调试路径）。
        artifact_name: 本次要提升的产物名（``None`` ⇒ 不绑产物）。
        expected_template_id: 被提升 run 的模板 id（判据绑定，``None``/``""`` ⇒ 不绑）。
        expected_template_hash: 该模板现算 YAML 指纹（判据版本绑定）。

    Returns:
        :class:`MergedHealthReading`。``status=""`` = 拿不到结论（fail-closed 409）。
        **任何异常都降级成 ``""``** —— 这只是省成本的旁路，不能让 I/O 抖动变成 500。

    Note:
        N+1 评估：``get_artifact_detail`` 每个报告产物名最多 1 次（≤2），``store.load``
        每个产物名最多「首个结论」1 次 + 「胜者确定后再判 diverged」1 次 ⇒ **≤4 次
        文件读**（v1.5 复审把 diverged 改成双向披露后 +1），与现状同阶。
        不做批量改：这里最多 6 行元数据，换来的复杂度大于收益。
    """
    try:
        artifacts = await store.list_artifacts(project_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - 旁路：读不到就按"没结论"处置
        return MergedHealthReading()
    by_name: dict[str, list[Any]] = {}
    for artifact in artifacts:
        name = str(getattr(artifact, "name", ""))
        if name in _REPORT_ARTIFACT_NAMES:
            by_name.setdefault(name, []).append(artifact)

    winner = ""
    winner_name = ""
    regression_checked = True
    for name in _REPORT_PRIORITY:
        groups = by_name.get(name)
        if not groups:
            continue
        status, checked = await _first_conclusive_status(
            store,
            groups,
            run_id=run_id,
            tenant_id=tenant_id,
            artifact_version=artifact_version,
            artifact_name=artifact_name,
            expected_template_id=expected_template_id,
            expected_template_hash=expected_template_hash,
        )
        if status:
            winner, winner_name, regression_checked = status, name, checked
            break

    if not winner:
        return MergedHealthReading()
    # ★ 无论胜者严格还是宽松，**都**补读败者一次判 diverged（有界：≤1 个产物名 × 1 次）。
    #
    # 为什么不能"严格结论就短路"（v1.5 复审修正）：``eval_verdict=unhealthy`` +
    # ``evaluation_report=healthy`` 恰恰是**最需要解释**的场景 —— 用户补评测看到
    # healthy、提升却 409，正是最困惑的时候。PRD T2-R3 的字面口径也是"两者并存且
    # 结论不一致 ⇒ 披露"，不区分胜者方向。代价是最坏多 1 次产物读（毫秒级）。
    for name in _REPORT_PRIORITY[_REPORT_PRIORITY.index(winner_name) + 1 :]:
        groups = by_name.get(name)
        if not groups:
            continue
        loser, _loser_checked = await _first_conclusive_status(
            store,
            groups,
            run_id=run_id,
            tenant_id=tenant_id,
            artifact_version=artifact_version,
            artifact_name=artifact_name,
            expected_template_id=expected_template_id,
            expected_template_hash=expected_template_hash,
        )
        return MergedHealthReading(
            status=winner,
            source_name=winner_name,
            divergent=bool(loser) and loser != winner,
            regression_checked=regression_checked,
        )
    return MergedHealthReading(
        status=winner,
        source_name=winner_name,
        regression_checked=regression_checked,
    )


async def _first_conclusive_status(  # noqa: PLR0913 - 绑定维度与 _known_merged_health 一致
    store: Any,
    groups: list[Any],
    *,
    run_id: str,
    tenant_id: str,
    artifact_version: int | None,
    artifact_name: str | None,
    expected_template_id: str | None,
    expected_template_hash: str | None,
) -> tuple[str, bool]:
    """在**同一产物名**的分组里取第一份「版本匹配且能给出结论」的报告。

    组内：先并列 artifact（同项目同名理论上只有 1 个，但手工命名可能有多份），
    再让每个 artifact 的**同 run 版本从新到旧**——最新那版若绑的是别的树
    （返修 3 的串味形态），继续往旧版找。

    Returns:
        ``(status, regression_checked)``：``status`` 为
        ``healthy`` / ``unhealthy`` / ``unverified`` / ``""``（本组给不出结论）；
        第二项 = 该报告的**回归判定是否真的做过**（D-1：探针没跑 ⇒ ``False``）。
    """
    for artifact in groups:
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
            #   （P2-6：这条判据必须有反向用例，否则"跨 run 串味"无测试守护）
            continue
        for version_row in reversed(matched):
            try:
                loaded = await store.load(
                    artifact.id, int(version_row.version), tenant_id=tenant_id
                )
            except Exception:  # noqa: BLE001 - 单个版本读不出来不影响其它候选
                continue
            raw = loaded.files.get(_REPORT_JSON_NAME)
            status = _report_health_status(
                raw,
                artifact_version,
                artifact_name,
                expected_template_id,
                expected_template_hash,
            )
            if status:
                return status, _raw_regression_checked(raw)
    return "", False


def _raw_regression_checked(raw: str | None) -> bool:
    """``_report_regression_checked`` 的**容错包装**（坏正文 ⇒ ``False``＝未检查）。"""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return _report_regression_checked(payload) if isinstance(payload, dict) else False

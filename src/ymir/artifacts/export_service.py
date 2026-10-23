"""导出编排服务（v1.2，docs/09 §4.3.1 / §4.3.4 / §9-T3.3）。

职责边界（**通道无关**）：选树 → 过滤移交通道 → 幂等判定 → 落回执 → 错误归一。
真正的网络请求只在 :class:`~ymir.artifacts.github.GitHubExporter` 里发生。

四件事，顺序不能换：

1. **选树**（``tree="merged"`` 用 :func:`merge_trees` 得到"基线 ⊕ 补丁"的**合并树**；
   ``tree="files"`` 用原产物最新版本）—— 绝不"只推补丁"：那推出去的工程跑不起来；
2. **幂等**：``sha256(artifact_id|version|content_hash|repo|branch|path_prefix)``；
   同键且上次成功且**远端 head 未变** → ``skipped_identical``（**不再调用 Exporter**）；
3. **落回执**：每次尝试（含 ``dry_run`` / 失败）都写一行 ``exports``；
4. **错误归一**：任何异常/失败 → ``error_code=1007``（推送域失败码，§12-D4）。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.exporter import (
    BlockedFile,
    Exporter,
    ExportRequest,
    ExportResult,
    build_result,
    check_protected_branch,
    filter_files,
)
from ymir.artifacts.github import map_exception
from ymir.artifacts.store import ArtifactStore
from ymir.common.errors import BizCode, NotFoundError
from ymir.models.entities import DEFAULT_TENANT_ID, Export
from ymir.storage.repositories import ExportRepository

logger = logging.getLogger(__name__)

#: 推送域失败码（docs/09 §12-D4 定案 A）
EXPORT_FAILED_CODE: int = int(BizCode.EXPORT_FAILED)


def idempotency_key(
    *,
    artifact_id: str,
    version: int,
    content_hash: str,
    repo: str,
    branch: str,
    path_prefix: str,
) -> str:
    """幂等键 = sha256 六元组（docs/09 §4.3.4 定案公式，一字不改）。"""
    payload = f"{artifact_id}|{version}|{content_hash}|{repo}|{branch}|{path_prefix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExportService:
    """推送编排（选树 / 幂等 / 回执 / 错误映射）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        exporters: Mapping[str, Exporter],
        default_artifact: str = "code",
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._exporters = dict(exporters)
        self._default_artifact = default_artifact

    def register_exporter(self, kind: str, exporter: Exporter) -> None:
        """注册 / 替换一个推送通道（装配期与测试注入共用；同 kind 覆盖）。"""
        self._exporters[kind] = exporter

    # ------------------------------------------------------------------ 入口

    async def export(
        self,
        *,
        project_id: str,
        request: ExportRequest,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Export:
        """执行一次推送，返回**已落库**的回执行（含 dry_run 与失败）。

        Raises:
            NotFoundError: 产物不存在（1002）。
            InvalidParamError: 未知通道 / 受保护分支（1001）—— 由通道或目标校验抛出。
        """
        loaded = await self._resolve_artifact(project_id, request, tenant_id)

        files = await self._select_tree(
            project_id,
            loaded.files,
            tree=request.tree,
            name=request.artifact_name,
            version=loaded.version,
            tenant_id=tenant_id,
        )
        branch = request.target.target_branch(request.artifact_name, loaded.version)
        key = idempotency_key(
            artifact_id=loaded.artifact.id,
            version=loaded.version,
            content_hash=loaded.version_row.content_hash,
            repo=request.target.repo,
            branch=branch,
            path_prefix=request.target.path_prefix,
        )
        request = request.model_copy(
            update={
                "project_id": project_id,
                "artifact_version": loaded.version,
                "files": files,
            }
        )

        # 分支保护在**任何通道调用之前**（破坏性默认值挡在网络外，也挡在假通道外）
        check_protected_branch(request.target)

        exporter = self._exporters.get(request.target.kind)
        if exporter is None:
            raise NotFoundError(f"未注册的推送通道: {request.target.kind}")

        # dry-run：服务侧直接算"将推什么"，**不调用通道**（通道实现无关、0 网络）
        if request.dry_run:
            outcome = filter_files(
                files, strict_secret_check=request.effective_strict
            )
            preview = build_result(outcome, request=request)
            return await self._persist(
                project_id=project_id,
                loaded=loaded,
                request=request,
                key=key,
                branch=branch,
                result=preview,
                tenant_id=tenant_id,
            )

        # ---- 幂等：上次成功且远端 head 未变 → 不重复推（Exporter 调用次数保持 1）----
        previous = await self._find_previous(key, tenant_id)
        if previous is not None and previous.status == "ok" and previous.commit_sha:
            head = await self._remote_head(exporter, request)
            if head is not None and head == previous.commit_sha:
                return await self._persist(
                    project_id=project_id,
                    loaded=loaded,
                    request=request,
                    key=key,
                    branch=branch,
                    result=ExportResult(
                        status="skipped_identical",
                        commit_sha=previous.commit_sha,
                        file_count=previous.file_count,
                        branch=branch,
                        dry_run=request.dry_run,
                    ),
                    tenant_id=tenant_id,
                )

        result = await self._run_exporter(exporter, request, branch)
        return await self._persist(
            project_id=project_id,
            loaded=loaded,
            request=request,
            key=key,
            branch=branch,
            result=result,
            tenant_id=tenant_id,
        )

    async def _resolve_artifact(
        self, project_id: str, request: ExportRequest, tenant_id: str
    ) -> Any:
        """定位要推送的产物版本：优先 ``artifact_id``（API 入口），否则按名字取最新。"""
        if request.artifact_id:
            return await self._store.load(
                request.artifact_id, request.artifact_version, tenant_id=tenant_id
            )
        loaded = await self._store.latest_by_name(
            project_id, request.artifact_name or self._default_artifact, tenant_id=tenant_id
        )
        if loaded is None:
            raise NotFoundError(f"产物不存在: {request.artifact_name}")
        return loaded

    # ------------------------------------------------------------------ 选树

    async def _select_tree(
        self,
        project_id: str,
        artifact_files: dict[str, str],
        *,
        tree: str,
        name: str,
        version: int,
        tenant_id: str,
    ) -> dict[str, str]:
        """``merged`` → 合并树（基线 ⊕ 补丁）；``files`` → 原产物文件集。

        合并逻辑**复用** ``ArtifactStore.merged_tree``（内部调 ``merge_trees``），
        本服务不自己拼基线 + 补丁 —— 否则"哪些文件该出现"会有第二份口径。
        """
        if tree == "files":
            return dict(artifact_files)
        if tree != "merged":
            raise ValueError(f"非法树语义: {tree!r}（files | merged）")
        return await self._store.merged_tree(
            project_id, name=name, version=version, tenant_id=tenant_id
        )

    # ------------------------------------------------------------------ 执行 / 回执

    async def _run_exporter(
        self, exporter: Exporter, request: ExportRequest, branch: str
    ) -> ExportResult:
        """调用通道；任何异常都归一为 ``failed/1007``（不让 API 500）。"""
        try:
            return await exporter.export(request)
        except Exception as exc:  # noqa: BLE001 - 推送失败必须落回执，不向上炸
            error_class, message = map_exception(exc)
            logger.warning(
                "推送失败（repo=%s branch=%s class=%s）：%s",
                request.target.repo,
                branch,
                error_class,
                message,
            )
            return ExportResult(
                status="failed",
                error_class=error_class,
                error_code=EXPORT_FAILED_CODE,
                error_message=message,
                branch=branch,
                dry_run=request.dry_run,
            )

    async def _remote_head(self, exporter: Exporter, request: ExportRequest) -> str | None:
        """读远端 head（通道可选能力：没有 ``remote_head`` 就返回 ``None`` = 未知）。"""
        reader = getattr(exporter, "remote_head", None)
        if not callable(reader):
            return None
        try:
            return await reader(request)
        except Exception:  # noqa: BLE001 - 读不到 head 只影响幂等，不影响推送
            logger.warning("读取远端 head 失败（按未知处理）", exc_info=True)
            return None

    async def _find_previous(self, key: str, tenant_id: str) -> Export | None:
        async with self._session_factory() as session:
            return await ExportRepository(session).find_by_key(key, tenant_id=tenant_id)

    async def _persist(
        self,
        *,
        project_id: str,
        loaded: Any,
        request: ExportRequest,
        key: str,
        branch: str,
        result: ExportResult,
        tenant_id: str,
    ) -> Export:
        """落一条回执（每次尝试都写；``detail`` 只含路径与原因，不含正文/密钥）。"""
        row = Export(
            tenant_id=tenant_id,
            project_id=project_id,
            artifact_id=loaded.artifact.id,
            artifact_name=request.artifact_name,
            artifact_version=loaded.version,
            tree_mode=request.tree,
            target_kind=request.target.kind,
            repo=request.target.repo,
            branch=branch,
            path_prefix=request.target.path_prefix,
            status=result.status,
            commit_sha=result.commit_sha,
            file_count=(
                result.file_count if result.file_count else len(result.files_pushed)
            ),
            blocked_count=len(result.blocked_files),
            skipped_count=len(result.skipped_files),
            error_code=result.error_code,
            error_class=result.error_class,
            error_message=result.error_message,
            idempotency_key=key,
            dry_run=request.dry_run,
            detail=_detail_payload(result),
            finished_at=datetime.now(UTC),
        )
        async with self._session_factory() as session:
            saved = await ExportRepository(session).add(row)
            await session.commit()
        return saved

    async def list_receipts(
        self,
        project_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
    ) -> list[Export]:
        """项目推送历史（时间倒序）。"""
        async with self._session_factory() as session:
            return await ExportRepository(session).list_by_project(
                project_id, tenant_id=tenant_id, limit=limit
            )


def _detail_payload(result: ExportResult) -> dict[str, Any]:
    """回执明细（被拦/跳过文件清单；**绝不含文件正文或 token**）。"""
    return {
        "blocked_files": [
            {"path": item.path, "reason": item.reason} for item in result.blocked_files
        ],
        "skipped_files": [
            {"path": item.path, "reason": item.reason} for item in result.skipped_files
        ],
        "bytes_total": result.bytes_total,
        "pushed_files": list(result.files_pushed),
    }


__all__ = [
    "EXPORT_FAILED_CODE",
    "BlockedFile",
    "ExportService",
    "build_result",
    "filter_files",
    "idempotency_key",
]

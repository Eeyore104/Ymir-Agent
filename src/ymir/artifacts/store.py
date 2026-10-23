"""产物库（架构设计 §3.6）：版本不可变 + 血缘 + diff + zip 导出。

- 内容只进文件系统（``<artifacts_dir>/<project_id>/<artifact_id>/v<version>/``），
  DB 只存元数据与 hash（§3.6 规则，避免 SQLite 存大文本）；
- 同一 artifact 名在项目内复用，重复保存即产出新版本（v1 → v2 → …），
  ``parent_version_id`` 自动串联血缘；版本目录一经写入永不修改；
- ``diff`` 逐文件 unified diff；``export_zip`` 导出各产物最新版本 +
  ``manifest.json``（含版本历史元数据）。

文件读写为同步小文件操作（KB 级），目录操作已在路径层做穿越防护
（:mod:`ymir.artifacts.versioning`）。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.artifacts.diff import DiffReport, diff_file_sets
from ymir.artifacts.merge import PATCH_MANIFEST_NAME, merge_trees, parse_patch_manifest
from ymir.artifacts.versioning import compute_content_hash, normalize_files
from ymir.common.errors import NotFoundError, StateConflictError
from ymir.models.base import utcnow
from ymir.models.entities import DEFAULT_TENANT_ID, Artifact, ArtifactVersion
from ymir.models.enums import ArtifactType
from ymir.storage.artifact_repo import ArtifactRepository, ArtifactVersionRepository

logger = logging.getLogger(__name__)

#: 基线产物类型值（``Artifact.type``，与 :class:`ymir.models.enums.ArtifactType` 同源）
BASELINE_TYPE: str = ArtifactType.BASELINE.value


@dataclass
class SavedVersion:
    """一次保存的结果（新版本引用）。"""

    artifact_id: str
    artifact_name: str
    artifact_type: str
    version: int
    version_id: str
    content_hash: str
    file_count: int
    size_bytes: int
    storage_path: str

    def ref(self) -> dict[str, Any]:
        """消息 / 事件用的产物引用（``artifact_id@version`` 语义）。"""
        return {
            "artifact_id": self.artifact_id,
            "name": self.artifact_name,
            "type": self.artifact_type,
            "version": self.version,
        }


@dataclass
class LoadedVersion:
    """一次加载的结果（元数据 + 文件内容）。"""

    artifact: Artifact
    version_row: ArtifactVersion
    files: dict[str, str]

    @property
    def version(self) -> int:
        return self.version_row.version


class ArtifactStore:
    """产物库：文件系统内容 + DB 元数据的协调层。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        artifacts_dir: Path | str,
    ) -> None:
        self._session_factory = session_factory
        self._root = Path(artifacts_dir)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ 写入

    async def save(
        self,
        *,
        project_id: str,
        name: str,
        artifact_type: str,
        files: Mapping[str, str],
        run_id: str | None = None,
        stage_id: str | None = None,
        created_by: str | None = None,
        message: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> SavedVersion:
        """保存产物新版本（版本不可变：同 artifact 每次保存递增版本号）。

        流程：路径归一化 -> 复用/新建 artifact -> 预计算版本号 -> 写文件树
        -> 落元数据（含血缘）-> 提交；任一环节失败回滚并清理磁盘残留。
        """
        normalized = normalize_files(files)
        content_hash = compute_content_hash(normalized)
        size_bytes = sum(len(content.encode("utf-8")) for content in normalized.values())

        async with self._session_factory() as session:
            artifacts = ArtifactRepository(session)
            artifact = await artifacts.get_by_name(project_id, name, tenant_id=tenant_id)
            if artifact is None:
                artifact = Artifact(
                    project_id=project_id,
                    name=name,
                    type=artifact_type,
                    tenant_id=tenant_id,
                )
                await artifacts.add(artifact)

            versions = ArtifactVersionRepository(session)
            next_version = await artifacts.next_version_number(artifact.id)
            parent = await versions.latest(artifact.id, tenant_id=tenant_id)

            target_dir = self._version_dir(project_id, artifact.id, next_version)
            # 原子占位（QA-04）：mkdir(exist_ok=False) 以文件系统为并发裁决 ——
            # 并行双写同一版本时仅一方能建目录，另一方 FileExistsError 即冲突，
            # 绝不覆盖已写版本；DB 唯一约束（uq_artifact_version）仍为最终裁决。
            try:
                target_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise StateConflictError(
                    f"产物版本目录已存在（版本不可变，禁止覆盖）: {target_dir}"
                ) from exc
            self._write_tree(target_dir, normalized)

            row = ArtifactVersion(
                artifact_id=artifact.id,
                version=next_version,
                run_id=run_id,
                stage_id=stage_id,
                storage_path=target_dir.relative_to(self._root).as_posix(),
                content_hash=content_hash,
                file_count=len(normalized),
                size_bytes=size_bytes,
                message=message,
                parent_version_id=parent.id if parent is not None else None,
                created_by=created_by,
                tenant_id=tenant_id,
            )
            session.add(row)
            artifact.current_version = next_version
            try:
                await session.commit()
            except Exception:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise

            return SavedVersion(
                artifact_id=artifact.id,
                artifact_name=artifact.name,
                artifact_type=artifact.type,
                version=next_version,
                version_id=row.id,
                content_hash=content_hash,
                file_count=len(normalized),
                size_bytes=size_bytes,
                storage_path=row.storage_path,
            )

    def _version_dir(self, project_id: str, artifact_id: str, version: int) -> Path:
        return self._root / project_id / artifact_id / f"v{version}"

    @staticmethod
    def _write_tree(target: Path, files: Mapping[str, str]) -> None:
        """写入文件树（失败时清理残留目录，保持版本不可变语义）。"""
        try:
            for rel_path, content in files.items():
                file_path = target / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                # newline=""：禁用平台换行转换，保证内容字节与 hash 一致（Windows 关键）
                with file_path.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    # ------------------------------------------------------------------ 读取

    async def load(
        self,
        artifact_id: str,
        version: int | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> LoadedVersion:
        """加载产物指定版本（缺省为 current_version）的元数据 + 文件内容。"""
        async with self._session_factory() as session:
            artifact = await ArtifactRepository(session).get(artifact_id, tenant_id=tenant_id)
            if artifact is None:
                raise NotFoundError(f"产物不存在: {artifact_id}")
            effective = artifact.current_version if version is None else version
            row = await ArtifactVersionRepository(session).get_version(
                artifact_id, effective, tenant_id=tenant_id
            )
            if row is None:
                raise NotFoundError(f"产物版本不存在: {artifact_id} v{effective}")
            files = self._read_tree(self._root / row.storage_path)
        return LoadedVersion(artifact=artifact, version_row=row, files=files)

    async def latest_by_name(
        self, project_id: str, name: str, *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> LoadedVersion | None:
        """按「项目 + 名字」加载最新版本（上游产物引用解析用），不存在返回 None。"""
        async with self._session_factory() as session:
            artifact = await ArtifactRepository(session).get_by_name(
                project_id, name, tenant_id=tenant_id
            )
        if artifact is None or artifact.current_version < 1:
            return None
        return await self.load(artifact.id, tenant_id=tenant_id)

    async def load_many(
        self, artifact_ids: list[str], *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> list[LoadedVersion]:
        """批量加载多个产物各自的最新版本（ARCH-08：避免逐条 load 的 N+1）。

        一次 IN 查询取回全部产物与版本行，再逐条读文件树（KB 级小文件）。
        """
        async with self._session_factory() as session:
            artifacts = await ArtifactRepository(session).get_many(
                artifact_ids, tenant_id=tenant_id
            )
            latest = await ArtifactVersionRepository(session).latest_many(
                [art.id for art in artifacts], tenant_id=tenant_id
            )
        loaded: list[LoadedVersion] = []
        for artifact in artifacts:
            row = latest.get(artifact.id)
            if row is None:
                continue
            files = self._read_tree(self._root / row.storage_path)
            loaded.append(LoadedVersion(artifact=artifact, version_row=row, files=files))
        return loaded

    @staticmethod
    def _read_tree(base: Path) -> dict[str, str]:
        if not base.is_dir():
            # 版本行存在但目录缺失（外部清理 / 迁移丢失）：显式告警（QA-09），
            # 但保持既有返回语义（空文件集）以免破坏 API / 评测路径。
            logger.warning("产物版本目录缺失（可能被外部清理）: %s", base)
            return {}
        files: dict[str, str] = {}
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(base).as_posix()
                with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    files[rel] = handle.read()
        return files

    # ------------------------------------------------------------------ 查询 / diff / 导出

    async def list_artifacts(
        self,
        project_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Artifact]:
        async with self._session_factory() as session:
            return await ArtifactRepository(session).list_by_project(
                project_id, tenant_id=tenant_id, limit=limit, offset=offset
            )

    async def get_artifact_detail(
        self, artifact_id: str, *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> tuple[Artifact, list[ArtifactVersion]]:
        """产物详情：元数据 + 版本列表（升序）。"""
        async with self._session_factory() as session:
            artifact = await ArtifactRepository(session).get(artifact_id, tenant_id=tenant_id)
            if artifact is None:
                raise NotFoundError(f"产物不存在: {artifact_id}")
            versions = await ArtifactVersionRepository(session).list_versions(
                artifact_id, tenant_id=tenant_id
            )
        return artifact, versions

    async def get_version_meta(
        self,
        artifact_id: str,
        version: int | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[Artifact, ArtifactVersion]:
        """只取版本元数据（**不读文件树**），用于校验引用合法性（v1.2 基线引用）。

        与 :meth:`load` 的区别：基线可能有几百个文件，仅仅为了"这个 id@version
        是否存在、属于哪个项目"而读全树是纯浪费。
        """
        async with self._session_factory() as session:
            artifact = await ArtifactRepository(session).get(artifact_id, tenant_id=tenant_id)
            if artifact is None:
                raise NotFoundError(f"产物不存在: {artifact_id}")
            effective = artifact.current_version if version is None else version
            row = await ArtifactVersionRepository(session).get_version(
                artifact_id, effective, tenant_id=tenant_id
            )
            if row is None:
                raise NotFoundError(f"产物版本不存在: {artifact_id} v{effective}")
        return artifact, row

    async def diff(
        self,
        artifact_id: str,
        v1: int,
        v2: int,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> DiffReport:
        """两个版本间的逐文件 unified diff。"""
        base = await self.load(artifact_id, v1, tenant_id=tenant_id)
        target = await self.load(artifact_id, v2, tenant_id=tenant_id)
        return diff_file_sets(base.files, target.files)

    async def find_baseline_files(
        self,
        project_id: str,
        expected_hash: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> dict[str, str]:
        """按内容指纹在项目基线产物的**全部版本**里找正文（R9：不看 latest）。

        v1.2 唯一的"按指纹回溯基线"实现：``evaluation.harness`` 与
        ``export_zip(mode="merged")`` 共用它，避免两处各写一遍搜索逻辑。
        找不到返回 ``{}``（调用方据此降级并标注，绝不猜"当前基线"）。
        """
        if not expected_hash:
            return {}
        artifacts = await self.list_artifacts(
            project_id, tenant_id=tenant_id, limit=1000
        )
        for artifact in artifacts:
            if artifact.type != BASELINE_TYPE:
                continue
            _artifact, versions = await self.get_artifact_detail(
                artifact.id, tenant_id=tenant_id
            )
            for row in versions:
                if row.content_hash != expected_hash:
                    continue
                loaded = await self.load(artifact.id, row.version, tenant_id=tenant_id)
                return loaded.files
        return {}

    async def merged_tree(
        self,
        project_id: str,
        *,
        name: str = "code",
        version: int | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> dict[str, str]:
        """取"合入后的工程树"（基线 ⊕ 补丁），供合并导出 / 跨产物 diff 使用。

        - 产物**没有** ``patch_manifest.json`` → 直接返回其文件集（v1.1 语义）；
        - 有 manifest 且有基线 → ``merge_trees(mode="merge")`` 的整树；
        - 有 manifest 但基线不可用（R9）→ **只返回补丁自身**，不拿"当前基线"凑。

        簿记文件 ``patch_manifest.json`` 永远不进导出树（它不是工程源码）。
        """
        latest = await self.latest_by_name(project_id, name, tenant_id=tenant_id)
        if latest is None:
            raise NotFoundError(f"产物不存在: {name}")
        loaded = (
            latest
            if version is None or version == latest.version
            else await self.load(latest.artifact.id, version, tenant_id=tenant_id)
        )
        patch = {
            path: content
            for path, content in loaded.files.items()
            if path != PATCH_MANIFEST_NAME
        }
        manifest = parse_patch_manifest(loaded.files.get(PATCH_MANIFEST_NAME))
        if manifest is None or not manifest.base_content_hash:
            return patch
        base_files = await self.find_baseline_files(
            project_id, manifest.base_content_hash, tenant_id=tenant_id
        )
        if not base_files:
            logger.warning(
                "合并导出：基线不可用，只导出补丁自身（project=%s name=%s hash=%s）",
                project_id,
                name,
                manifest.base_content_hash[:12],
            )
            return patch
        return merge_trees(
            base_files, patch, mode="merge", deleted=manifest.deleted
        ).files

    async def export_zip(
        self,
        project_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        include_baseline: bool = False,
        mode: str = "latest",
    ) -> bytes:
        """整库导出（``mode="latest"``）或**扁平工程布局**导出（``mode="merged"``）。

        ``mode="latest"``（缺省，v1.1 行为）：各产物最新版本内容 + ``manifest.json``，
        zip 内路径 ``{name}/v{n}/{rel_path}``；``include_baseline=False`` 时排除
        ``type=baseline`` 产物（v1.2，docs/09 §9-T2.2 ⑤）。

        ``mode="merged"``（v1.2，docs/09 §9-T2.5 ③）：把「基线 ⊕ 补丁」的**合并树**
        以**可运行工程布局**打出 —— 顶层直接 ``app/main.py``，**没有** ``code/v1/``
        前缀；``manifest.json`` 里额外标注 ``mode`` 与来源产物，供合入前追溯。
        """
        if mode == "merged":
            return await self.export_merged_zip(project_id, tenant_id=tenant_id)
        if mode != "latest":
            raise StateConflictError(f"非法导出模式: {mode!r}（latest | merged）")
        async with self._session_factory() as session:
            artifacts = await ArtifactRepository(session).list_by_project(
                project_id, tenant_id=tenant_id, limit=1000
            )
            # 批量 IN 查询版本行（ARCH-08），替换逐 artifact 的 list_versions（N+1）
            version_rows = await ArtifactVersionRepository(session).list_for_artifacts(
                [art.id for art in artifacts], tenant_id=tenant_id
            )
        if not include_baseline:
            artifacts = [art for art in artifacts if art.type != BASELINE_TYPE]
        # 整库打包为同步阻塞 IO（逐文件读取 + zip 压缩），转线程避免阻塞事件循环（ARCH-07）
        return await asyncio.to_thread(
            self._build_export_zip, project_id, artifacts, version_rows
        )

    async def export_merged_zip(
        self,
        project_id: str,
        *,
        name: str = "code",
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> bytes:
        """合并树导出（扁平布局：顶层直接是工程文件）。"""
        files = await self.merged_tree(project_id, name=name, tenant_id=tenant_id)
        latest = await self.latest_by_name(project_id, name, tenant_id=tenant_id)
        manifest = {
            "project_id": project_id,
            "exported_at": utcnow().isoformat(),
            "mode": "merged",
            "tree": "merged",
            "artifacts": [
                {
                    "id": latest.artifact.id if latest is not None else None,
                    "name": name,
                    "version": latest.version if latest is not None else None,
                    "file_count": len(files),
                }
            ],
        }
        return await asyncio.to_thread(self._build_flat_zip, files, manifest)

    @staticmethod
    def _build_flat_zip(files: Mapping[str, str], manifest: Mapping[str, Any]) -> bytes:
        """扁平工程布局 zip（无 ``{name}/v{n}/`` 前缀）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, content in sorted(files.items()):
                zf.writestr(rel_path, content)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return buffer.getvalue()

    def _build_export_zip(
        self,
        project_id: str,
        artifacts: list[Artifact],
        version_rows: dict[str, list[ArtifactVersion]],
    ) -> bytes:
        """同步构建导出包（供 :meth:`export_zip` 经 ``asyncio.to_thread`` 调度）。"""
        manifest: dict[str, Any] = {
            "project_id": project_id,
            "exported_at": utcnow().isoformat(),
            "artifacts": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for art in artifacts:
                versions = version_rows.get(art.id, [])
                current = versions[-1] if versions else None
                manifest["artifacts"].append(
                    {
                        "id": art.id,
                        "name": art.name,
                        "type": art.type,
                        "current_version": art.current_version,
                        "versions": [
                            {
                                "id": row.id,
                                "version": row.version,
                                "run_id": row.run_id,
                                "stage_id": row.stage_id,
                                "content_hash": row.content_hash,
                                "file_count": row.file_count,
                                "size_bytes": row.size_bytes,
                                "message": row.message,
                                "parent_version_id": row.parent_version_id,
                                "created_by": row.created_by,
                                "created_at": row.created_at.isoformat(),
                            }
                            for row in versions
                        ],
                    }
                )
                if current is None:
                    continue
                files = self._read_tree(self._root / current.storage_path)
                for rel_path, content in files.items():
                    zf.writestr(f"{art.name}/v{current.version}/{rel_path}", content)
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        return buffer.getvalue()

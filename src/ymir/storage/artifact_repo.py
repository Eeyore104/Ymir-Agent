"""产物仓储（架构设计 §3.6、§3.10）：``artifacts`` / ``artifact_versions`` 表操作。

内容不进 DB（只存元数据 + hash）；本模块只负责元数据查询与写入，文件系统
读写由 :class:`ymir.artifacts.store.ArtifactStore` 协调。
"""

from __future__ import annotations

from sqlalchemy import func, select

from ymir.models.entities import Artifact, ArtifactVersion
from ymir.storage.repositories import DEFAULT_TENANT_ID, BaseRepository


class ArtifactRepository(BaseRepository[Artifact]):
    """产物仓储。"""

    model = Artifact

    async def get_by_name(
        self, project_id: str, name: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> Artifact | None:
        """按「项目 + 名称」取产物（同名产物在项目内复用）。"""
        stmt = select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.name == name,
            Artifact.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_by_project(
        self,
        project_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Artifact]:
        stmt = (
            select(Artifact)
            .where(Artifact.project_id == project_id, Artifact.tenant_id == tenant_id)
            .order_by(Artifact.created_at.asc(), Artifact.id.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_many(
        self, artifact_ids: list[str], tenant_id: str = DEFAULT_TENANT_ID
    ) -> list[Artifact]:
        """批量按主键读取（ARCH-08：避免逐条 load 的 N+1）。"""
        ids = list(dict.fromkeys(artifact_ids))
        if not ids:
            return []
        stmt = select(Artifact).where(
            Artifact.id.in_(ids), Artifact.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_project(
        self, project_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Artifact)
            .where(Artifact.project_id == project_id, Artifact.tenant_id == tenant_id)
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def next_version_number(self, artifact_id: str) -> int:
        """下一版本号（当前最大版本 + 1；供保存路径预计算）。"""
        stmt = select(func.coalesce(func.max(ArtifactVersion.version), 0)).where(
            ArtifactVersion.artifact_id == artifact_id
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one()) + 1


class ArtifactVersionRepository(BaseRepository[ArtifactVersion]):
    """产物版本仓储。"""

    model = ArtifactVersion

    async def get_version(
        self, artifact_id: str, version: int, tenant_id: str = DEFAULT_TENANT_ID
    ) -> ArtifactVersion | None:
        stmt = select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version == version,
            ArtifactVersion.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def latest(
        self, artifact_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> ArtifactVersion | None:
        """最新（最大版本号）版本记录。"""
        stmt = (
            select(ArtifactVersion)
            .where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.tenant_id == tenant_id,
            )
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_versions(
        self, artifact_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> list[ArtifactVersion]:
        """全部版本（按版本号升序）。"""
        stmt = (
            select(ArtifactVersion)
            .where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.tenant_id == tenant_id,
            )
            .order_by(ArtifactVersion.version.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_artifacts(
        self, artifact_ids: list[str], tenant_id: str = DEFAULT_TENANT_ID
    ) -> dict[str, list[ArtifactVersion]]:
        """批量拉取多个产物的全部版本（一次 IN 查询，ARCH-08）。

        返回 ``artifact_id -> [版本升序]``；无版本的产物不出现在结果中。
        """
        ids = list(dict.fromkeys(artifact_ids))
        if not ids:
            return {}
        stmt = (
            select(ArtifactVersion)
            .where(
                ArtifactVersion.artifact_id.in_(ids),
                ArtifactVersion.tenant_id == tenant_id,
            )
            .order_by(ArtifactVersion.artifact_id.asc(), ArtifactVersion.version.asc())
        )
        result = await self.session.execute(stmt)
        grouped: dict[str, list[ArtifactVersion]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.artifact_id, []).append(row)
        return grouped

    async def latest_many(
        self, artifact_ids: list[str], tenant_id: str = DEFAULT_TENANT_ID
    ) -> dict[str, ArtifactVersion]:
        """批量拉取多个产物的最新版本（一次 IN 查询，ARCH-08）。"""
        grouped = await self.list_for_artifacts(artifact_ids, tenant_id=tenant_id)
        return {artifact_id: rows[-1] for artifact_id, rows in grouped.items() if rows}

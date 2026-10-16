"""实体 Repository（架构设计 §2.4 扩展点、§5）。

所有查询预留 ``tenant_id`` scope 参数（默认 ``default``），为 v2.x 多租户演进留口子。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.models.entities import DEFAULT_TENANT_ID, Approval, PipelineRun, Project, StageExecution
from ymir.models.enums import ApprovalStatus, RunMode, RunStatus


def _as_value(value: str | Enum) -> str:
    """将枚举或字符串统一转为落库字符串值。"""
    return value.value if isinstance(value, Enum) else str(value)


class BaseRepository[ModelT]:
    """通用仓储：按主键读取、新增、计数。"""

    model: type[ModelT]
    id_column: Any = None

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @property
    def _id_attr(self) -> Any:
        return self.id_column if self.id_column is not None else self.model.id  # type: ignore[attr-defined]

    async def get(self, entity_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> ModelT | None:
        """按主键读取实体（带 tenant scope）。"""
        stmt = select(self.model).where(
            self._id_attr == entity_id,
            self.model.tenant_id == tenant_id,  # type: ignore[attr-defined]
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, entity: ModelT) -> ModelT:
        """新增实体并 flush（不提交，由调用方控制事务边界）。"""
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def count(self, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        """统计实体数量（带 tenant scope）。"""
        stmt = (
            select(func.count()).select_from(self.model).where(self.model.tenant_id == tenant_id)  # type: ignore[attr-defined]
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())


class ProjectRepository(BaseRepository[Project]):
    """项目仓储。"""

    model = Project

    async def create(
        self,
        name: str,
        description: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Project:
        project = Project(name=name, description=description, tenant_id=tenant_id)
        return await self.add(project)

    async def list(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Project]:
        stmt = (
            select(Project)
            .where(Project.tenant_id == tenant_id)
            .order_by(Project.created_at.desc(), Project.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class RunRepository(BaseRepository[PipelineRun]):
    """运行仓储。"""

    model = PipelineRun

    async def create(
        self,
        project_id: str,
        requirement: str = "",
        pipeline_id: str = "software-dev",
        mode: str | RunMode = RunMode.KEY_NODE,
        context: dict[str, Any] | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> PipelineRun:
        merged_context: dict[str, Any] = {"requirement": requirement}
        if context:
            merged_context.update(context)
        run = PipelineRun(
            project_id=project_id,
            pipeline_id=pipeline_id,
            mode=_as_value(mode),
            status=RunStatus.PENDING.value,
            current_stage_id=None,
            context=merged_context,
            tenant_id=tenant_id,
        )
        return await self.add(run)

    async def list_by_project(
        self,
        project_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineRun]:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.project_id == project_id, PipelineRun.tenant_id == tenant_id)
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_project(self, project_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        stmt = (
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.project_id == project_id, PipelineRun.tenant_id == tenant_id)
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def update_status(
        self,
        run_id: str,
        status: str | RunStatus,
        *,
        error: str | None = None,
        current_stage_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> PipelineRun | None:
        run = await self.get(run_id, tenant_id=tenant_id)
        if run is None:
            return None
        run.status = _as_value(status)
        if error is not None:
            run.error = error
        if current_stage_id is not None:
            run.current_stage_id = current_stage_id
        await self.session.flush()
        return run

    async def list_unfinished(self, tenant_id: str = DEFAULT_TENANT_ID) -> list[PipelineRun]:
        """未完成运行（供启动恢复扫描，§3.4）：running / waiting_approval。"""
        unfinished = (RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value)
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.tenant_id == tenant_id, PipelineRun.status.in_(unfinished))
            .order_by(PipelineRun.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class ApprovalRepository(BaseRepository[Approval]):
    """审批仓储（T04，§3.8）。"""

    model = Approval

    async def create(
        self,
        *,
        run_id: str,
        stage_id: str,
        gate_name: str,
        artifact_refs: list[Any] | None = None,
        status: str | ApprovalStatus = ApprovalStatus.PENDING,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Approval:
        """新增审批记录（flush 不提交，事务边界由调用方控制）。"""
        approval = Approval(
            run_id=run_id,
            stage_id=stage_id,
            gate_name=gate_name,
            artifact_refs=list(artifact_refs or []),
            status=_as_value(status),
            tenant_id=tenant_id,
        )
        return await self.add(approval)

    async def list_filtered(
        self,
        *,
        status: str | ApprovalStatus | None = None,
        run_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Approval]:
        """按 status / run_id 过滤（供审批中心列表；新的在前）。"""
        stmt = select(Approval).where(Approval.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(Approval.status == _as_value(status))
        if run_id is not None:
            stmt = stmt.where(Approval.run_id == run_id)
        stmt = (
            stmt.order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class StageExecutionRepository(BaseRepository[StageExecution]):
    """阶段执行记录仓储（ARCH-09：收口「取最新执行记录」重复查询）。"""

    model = StageExecution

    async def latest(
        self, run_id: str, stage_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> StageExecution | None:
        """某 run 某阶段的「最新一条」执行记录（按 attempt 降序）。"""
        stmt = (
            select(StageExecution)
            .where(
                StageExecution.run_id == run_id,
                StageExecution.stage_id == stage_id,
                StageExecution.tenant_id == tenant_id,
            )
            .order_by(StageExecution.attempt.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

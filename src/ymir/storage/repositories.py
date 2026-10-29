"""实体 Repository（架构设计 §2.4 扩展点、§5）。

所有查询预留 ``tenant_id`` scope 参数（默认 ``default``），为 v2.x 多租户演进留口子。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.models.entities import (
    DEFAULT_TENANT_ID,
    Approval,
    Export,
    PipelineRun,
    Project,
    StageExecution,
)
from ymir.models.enums import ApprovalStatus, RunMode, RunStatus, StageStatus


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

    async def get_pending(
        self,
        run_id: str,
        stage_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Approval | None:
        """取某 run 某阶段当前 pending 的审批（防刷屏复用，§4.5.1）。

        「同 run + 同 stage 已有 pending」时不得新建卡片；人工驳回后该记录已变
        ``rejected``，因此下次终局会正常新建一次（R8③ 语义）。
        """
        stmt = (
            select(Approval)
            .where(
                Approval.run_id == run_id,
                Approval.stage_id == stage_id,
                Approval.status == ApprovalStatus.PENDING.value,
                Approval.tenant_id == tenant_id,
            )
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

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

    async def latest_success_with_output(
        self,
        run_id: str,
        output_name: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> StageExecution | None:
        """该 run 中「首个产出为 ``output_name``」的**最后一次成功**阶段（v1.3.1 T5.3）。

        为什么**不按 ``role_id`` 硬编码**（如"取 role=engineer 那条"）：阶段 id / 角色名
        是流水线配置（可被 ``sop`` 改写、可有多条产出 ``code`` 的阶段），硬编码会在
        自定义流水线上直接失效；判据只能是"这条阶段的产物就是那个东西"。

        为什么按 ``attempt desc, started_at desc`` 排：重跑/回修会让同一 stage 有多条
        记录，用户点"另存为新基线"指的是**屏幕上那一版**，即最近一次成功的产出；
        ``started_at`` 兜底 ``attempt`` 相同的情况（不同 stage 之间没有 attempt 可比性）。

        Args:
            run_id: 运行 id。
            output_name: 产物名（``"code"``）。
            tenant_id: 租户。

        Returns:
            命中的阶段执行记录；没有成功产出该产物的阶段 ⇒ ``None``。
        """
        stmt = (
            select(StageExecution)
            .where(
                StageExecution.run_id == run_id,
                StageExecution.tenant_id == tenant_id,
                StageExecution.status == StageStatus.COMPLETED.value,
            )
            .order_by(StageExecution.attempt.desc(), StageExecution.started_at.desc())
        )
        result = await self.session.execute(stmt)
        for row in result.scalars().all():
            if _first_output_name(row.output_refs) == output_name:
                return row
        return None


def _first_output_name(refs: list[Any] | None) -> str:
    """取 ``output_refs[0].name``（脏数据返回空串，绝不抛）。

    与 ``role_executor`` 里写 ``output_refs=[saved.ref()]`` 的形状一一对应；
    历史数据可能是空列表 / 非 dict ⇒ 视为"没有产出"，让调用方走 1002 分支。
    """
    if not isinstance(refs, list) or not refs:
        return ""
    head = refs[0]
    if not isinstance(head, dict):
        return ""
    return str(head.get("name") or "")


class ExportRepository(BaseRepository[Export]):
    """推送回执仓储（v1.2，docs/09 §9-T3.3）。

    三个方法对应三类查询：写入（``add``）/ 幂等判定（``find_by_key``）/ 列表页
    （``list_by_project``）。幂等判定取**最近一条**回执 —— 因为每次尝试都写一行，
    "上次是否已推成功"必须看最新状态，而不是"有没有推过"。
    """

    model = Export

    async def add(self, row: Export) -> Export:
        """写入一条回执（不 commit，调用方控制事务边界）。"""
        self.session.add(row)
        await self.session.flush()
        return row

    async def find_by_key(
        self, idempotency_key: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> Export | None:
        """按幂等键取**最近一条**回执（含失败与 dry_run）。"""
        stmt = (
            select(Export)
            .where(
                Export.idempotency_key == idempotency_key,
                Export.tenant_id == tenant_id,
            )
            .order_by(Export.created_at.desc(), Export.id.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_project(
        self,
        project_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Export]:
        """项目推送历史（时间倒序）。"""
        stmt = (
            select(Export)
            .where(Export.project_id == project_id, Export.tenant_id == tenant_id)
            .order_by(Export.created_at.desc(), Export.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

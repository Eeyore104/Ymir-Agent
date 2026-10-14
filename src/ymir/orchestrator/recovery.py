"""断点恢复（架构设计 §3.4）：进程启动扫描未完成 run。

- ``running``：进程中断 —— 重新拉起推进任务；引擎按「当前阶段最新执行记录」
  自动决定补推进（记录已 completed）或 attempt+1 重跑（记录 running/failed），
  已产产物与历史记录保留；
- ``waiting_approval``：审批不丢，保持等待（T02 无审批链路；T04 接入审批决策恢复）。
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.models.base import utcnow
from ymir.models.entities import DEFAULT_TENANT_ID, StageExecution
from ymir.models.enums import RunStatus, StageStatus
from ymir.orchestrator.engine import OrchestrationEngine
from ymir.storage.repositories import RunRepository


async def recover_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    engine: OrchestrationEngine,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[str]:
    """扫描并恢复未完成 run；返回被重新拉起的 run_id 列表。"""
    async with session_factory() as session:
        runs = await RunRepository(session).list_unfinished(tenant_id=tenant_id)

    recovered: list[str] = []
    for run in runs:
        if run.status == RunStatus.RUNNING.value:
            # 中断现场清理：残留的 running 阶段执行标记为 failed（现场保留为历史），
            # 引擎随后按最新记录决定 attempt+1 重跑
            async with session_factory() as session:
                await session.execute(
                    update(StageExecution)
                    .where(
                        StageExecution.run_id == run.id,
                        StageExecution.status == StageStatus.RUNNING.value,
                    )
                    .values(
                        status=StageStatus.FAILED.value,
                        error="进程中断（启动恢复）",
                        finished_at=utcnow(),
                    )
                )
                await session.commit()
            await engine.resume_interrupted(run.id)
            recovered.append(run.id)
        elif run.status == RunStatus.WAITING_APPROVAL.value:
            continue  # T04：审批决策恢复（保持等待态，等人工处理）
    return recovered

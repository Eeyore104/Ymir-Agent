"""ORM 实体与 Repository 单元测试。"""

from __future__ import annotations

from datetime import UTC

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ymir.models.entities import (
    Approval,
    Artifact,
    ArtifactVersion,
    Event,
    Message,
    ModelCall,
    Pipeline,
    PipelineRun,
    SandboxSession,
    StageExecution,
)
from ymir.models.enums import (
    ApprovalStatus,
    ArtifactType,
    MessageType,
    ModelTier,
    RunMode,
    RunStatus,
    SandboxStatus,
    StageStatus,
)
from ymir.storage.repositories import ProjectRepository, RunRepository


async def _make_project_run(session: AsyncSession) -> tuple[str, str]:
    project = await ProjectRepository(session).create(name="demo", description="示例")
    await session.flush()
    run = await RunRepository(session).create(
        project_id=project.id, requirement="做一个待办 API"
    )
    await session.flush()
    return project.id, run.id


async def _load_run_with_children(session: AsyncSession, run_id: str) -> PipelineRun:
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.id == run_id)
        .options(
            selectinload(PipelineRun.stages),
            selectinload(PipelineRun.messages),
            selectinload(PipelineRun.approvals),
            selectinload(PipelineRun.events),
            selectinload(PipelineRun.model_calls),
            selectinload(PipelineRun.sandbox_sessions),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def test_project_defaults_and_timestamps(session: AsyncSession) -> None:
    repo = ProjectRepository(session)
    project = await repo.create(name="demo", description="示例项目")
    await session.flush()

    assert project.id
    assert len(project.id) == 36
    assert project.tenant_id == "default"
    assert project.created_at.tzinfo is not None
    assert project.created_at.utcoffset() == UTC.utcoffset(None)


async def test_run_defaults(session: AsyncSession) -> None:
    project = await ProjectRepository(session).create(name="p")
    await session.flush()
    run = await RunRepository(session).create(project_id=project.id, requirement="需求")
    await session.flush()

    assert run.status == RunStatus.PENDING.value
    assert run.mode == RunMode.KEY_NODE.value
    assert run.pipeline_id == "software-dev"
    assert run.context["requirement"] == "需求"
    assert run.current_stage_id is None
    assert run.started_at is None and run.finished_at is None


async def test_relationship_cascade(session: AsyncSession) -> None:
    project_id, run_id = await _make_project_run(session)

    session.add(StageExecution(run_id=run_id, stage_id="prd", role_id="pm"))
    session.add(
        Message(run_id=run_id, seq=1, type=MessageType.ARTIFACT.value, summary="PRD v1")
    )
    session.add(Approval(run_id=run_id, stage_id="prd", gate_name="PRD 确认"))
    session.add(Event(run_id=run_id, seq=1, type="run.started", payload={}))
    session.add(
        ModelCall(
            run_id=run_id,
            stage_id="prd",
            provider="deepseek",
            model="deepseek-chat",
            tier=ModelTier.EXECUTOR.value,
        )
    )
    session.add(SandboxSession(run_id=run_id))
    artifact = Artifact(project_id=project_id, name="prd.md", type=ArtifactType.PRD.value)
    session.add(artifact)
    await session.flush()

    session.add(
        ArtifactVersion(
            artifact_id=artifact.id, version=1, run_id=run_id, storage_path="data/artifacts/x"
        )
    )
    await session.flush()

    run = await _load_run_with_children(session, run_id)
    assert len(run.stages) == 1
    assert len(run.messages) == 1
    assert len(run.approvals) == 1
    assert len(run.events) == 1
    assert len(run.model_calls) == 1
    assert len(run.sandbox_sessions) == 1
    assert run.stages[0].status == StageStatus.PENDING.value
    assert run.stages[0].attempt == 1
    assert run.approvals[0].status == ApprovalStatus.PENDING.value
    assert run.sandbox_sessions[0].status == SandboxStatus.CREATED.value


async def test_event_seq_unique_constraint(session: AsyncSession) -> None:
    _, run_id = await _make_project_run(session)
    session.add(Event(run_id=run_id, seq=1, type="run.started", payload={}))
    await session.flush()

    session.add(Event(run_id=run_id, seq=1, type="run.started", payload={}))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_message_seq_unique_constraint(session: AsyncSession) -> None:
    _, run_id = await _make_project_run(session)
    session.add(Message(run_id=run_id, seq=1, type=MessageType.STATUS.value))
    await session.flush()

    session.add(Message(run_id=run_id, seq=1, type=MessageType.STATUS.value))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_artifact_version_unique_constraint(session: AsyncSession) -> None:
    project_id, run_id = await _make_project_run(session)
    artifact = Artifact(project_id=project_id, name="code", type=ArtifactType.CODE.value)
    session.add(artifact)
    await session.flush()
    session.add(
        ArtifactVersion(artifact_id=artifact.id, version=1, storage_path="p1", run_id=run_id)
    )
    await session.flush()

    session.add(
        ArtifactVersion(artifact_id=artifact.id, version=1, storage_path="p2", run_id=run_id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_pipeline_composite_pk(session: AsyncSession) -> None:
    session.add(Pipeline(id="software-dev", version="1.0", definition={"stages": []}))
    await session.flush()

    result = await session.execute(select(Pipeline))
    pipelines = result.scalars().all()
    assert len(pipelines) == 1
    assert pipelines[0].definition == {"stages": []}


async def test_repository_crud_and_tenant_scope(session: AsyncSession) -> None:
    repo = ProjectRepository(session)
    created = await repo.create(name="A", description="da")
    await session.commit()

    assert await repo.count() == 1
    fetched = await repo.get(created.id)
    assert fetched is not None and fetched.name == "A"
    assert await repo.get("does-not-exist") is None

    listed = await repo.list()
    assert len(listed) == 1

    # tenant scope 隔离
    assert await repo.get(created.id, tenant_id="other") is None
    assert await repo.count(tenant_id="other") == 0


async def test_run_repository_list_and_update(session: AsyncSession) -> None:
    project = await ProjectRepository(session).create(name="p")
    await session.flush()
    run_repo = RunRepository(session)
    run = await run_repo.create(project_id=project.id, requirement="r")
    await session.flush()

    assert await run_repo.count_by_project(project.id) == 1
    assert len(await run_repo.list_by_project(project.id)) == 1

    # pending 不算未完成
    assert await run_repo.list_unfinished() == []

    updated = await run_repo.update_status(run.id, RunStatus.RUNNING, current_stage_id="prd")
    assert updated is not None
    assert updated.status == RunStatus.RUNNING.value
    assert updated.current_stage_id == "prd"

    unfinished = await run_repo.list_unfinished()
    assert len(unfinished) == 1

    missing = await run_repo.update_status("nope", RunStatus.COMPLETED)
    assert missing is None

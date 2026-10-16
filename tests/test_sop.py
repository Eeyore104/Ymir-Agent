"""SOP 定义加载与快照入库测试（B2 补测：``sync_pipeline`` 幂等 upsert）。

覆盖巡检盲区 B2：定义快照的 upsert 幂等性（重复同步不产生重复行 / 版本）、
同 id+version 定义变更时的更新路径、快照内容与定义一致。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ymir.models.entities import Pipeline
from ymir.sop.loader import load_pipeline, sync_pipeline
from ymir.sop.models import END_TOKEN, PipelineDefinition, StageSpec, TransitionSpec

PIPELINE_ID = "b2-test-pipeline"


def _definition(version: str = "1.0", *, extra_stage: bool = False) -> PipelineDefinition:
    """构造测试流水线定义（含 code 阶段与否，用于构造「定义变更」）。"""
    stages = [
        StageSpec(id="prd", role="pm", inputs=["requirement"], outputs=["prd"]),
        StageSpec(id="design", role="architect", inputs=["prd"], outputs=["design"]),
    ]
    if extra_stage:
        stages.append(StageSpec(id="code", role="engineer", inputs=["design"], outputs=["code"]))
    transitions = [
        TransitionSpec(**{"from": "prd", "on": "stage_completed", "to": "design"}),
    ]
    if extra_stage:
        transitions.append(
            TransitionSpec(**{"from": "design", "on": "stage_completed", "to": "code"})
        )
    transitions.append(
        TransitionSpec(**{"from": stages[-1].id, "on": "stage_completed", "to": END_TOKEN})
    )
    return PipelineDefinition(
        id=PIPELINE_ID,
        version=version,
        description="B2 测试流水线",
        stages=stages,
        transitions=transitions,
    )


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[Pipeline]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Pipeline).where(Pipeline.id == PIPELINE_ID).order_by(Pipeline.version)
                )
            )
            .scalars()
            .all()
        )


async def test_sync_pipeline_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """重复同步同 id+version：不产生重复行，快照内容不变。"""
    definition = _definition()
    first = await sync_pipeline(session, definition)
    await session.commit()

    rows = await _rows(session_factory)
    assert len(rows) == 1
    assert (first.id, first.version) == (PIPELINE_ID, "1.0")
    assert first.definition == definition.snapshot()

    second = await sync_pipeline(session, definition)
    await session.commit()

    rows_after = await _rows(session_factory)
    assert len(rows_after) == 1  # 幂等：不产生重复行
    assert (second.id, second.version) == (first.id, first.version)
    assert second.definition == first.definition


async def test_sync_pipeline_updates_snapshot_for_same_version(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """同 id+version 但定义变更：快照被正确更新（update 路径）。"""
    await sync_pipeline(session, _definition("1.0"))
    await session.commit()

    changed = _definition("1.0", extra_stage=True)
    row = await sync_pipeline(session, changed)
    await session.commit()

    rows = await _rows(session_factory)
    assert len(rows) == 1
    assert row.definition == changed.snapshot()
    assert len(row.definition["stages"]) == 3  # 新增 code 阶段已生效


async def test_sync_pipeline_new_version_creates_new_row(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """version 变化：产生新行（版本化快照），旧行保留。"""
    v1 = await sync_pipeline(session, _definition("1.0"))
    await session.commit()

    changed = _definition("2.0", extra_stage=True)
    v2 = await sync_pipeline(session, changed)
    await session.commit()

    rows = await _rows(session_factory)
    assert [row.version for row in rows] == ["1.0", "2.0"]
    assert v2.version == "2.0"
    assert v2.definition != v1.definition
    assert v2.definition == changed.snapshot()


async def test_sync_pipeline_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """租户隔离语义（QA-B3-01 已闭环）：同 (id, version) 跨租户各占一行、互不覆盖。

    ``pipelines`` 复合主键为 ``(id, version, tenant_id)``，``sync_pipeline`` 的 upsert
    定位键同为三者：不同租户的同版本定义可以并存，单租户下 tenant 恒定、行为与此前一致。
    """
    definition = _definition()
    first = await sync_pipeline(session, definition, tenant_id="t-a")
    await session.commit()
    assert first.tenant_id == "t-a"

    # 同 (id, version) 换 tenant：应另起一行，且不覆盖 t-a 的定义
    changed = _definition("1.0", extra_stage=True)
    second = await sync_pipeline(session, changed, tenant_id="t-b")
    await session.commit()
    assert second.tenant_id == "t-b"

    rows = await _rows(session_factory)
    assert len(rows) == 2
    by_tenant = {row.tenant_id: row for row in rows}
    assert set(by_tenant) == {"t-a", "t-b"}
    assert by_tenant["t-a"].definition == definition.snapshot()  # 未被 t-b 覆盖
    assert by_tenant["t-b"].definition == changed.snapshot()

    # 同租户重复 sync 仍幂等：不新增行、快照内容不变
    again = await sync_pipeline(session, definition, tenant_id="t-a")
    await session.commit()
    rows_after = await _rows(session_factory)
    assert len(rows_after) == 2
    assert again.definition == definition.snapshot()
    assert by_tenant["t-a"].definition == again.definition


def test_load_pipeline_reads_repo_config() -> None:
    """加载仓库真实流水线：结构校验通过且含三关人工审批。"""
    definition = load_pipeline("software-dev")
    assert definition.id == "software-dev"
    gates = {stage.id: stage.gate for stage in definition.stages if stage.gate is not None}
    assert set(gates) == {"prd", "architecture", "coding"}
    assert all(gate.on_reject_target == stage_id for stage_id, gate in gates.items())

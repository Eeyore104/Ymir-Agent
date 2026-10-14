"""流水线加载与快照入库（架构设计 §3.2）。

- ``load_pipeline``：YAML -> Pydantic 结构校验 -> SopValidator 静态校验；
- ``truncate_pipeline``：截断至目标阶段（单角色调试 / 分步演示）；
- ``sync_pipeline``：定义快照版本化入 ``pipelines`` 表（id+version 幂等 upsert），
  为 v1.x 可视化编排留数据基础。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.common.errors import InvalidParamError, NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.models.entities import DEFAULT_TENANT_ID, Pipeline
from ymir.sop.models import END_TOKEN, PipelineDefinition, TransitionSpec
from ymir.sop.validator import SopDefinitionError, SopValidator

DEFAULT_PIPELINES_DIR = PROJECT_ROOT / "config" / "pipelines"


def load_pipeline(
    pipeline_id: str,
    *,
    pipelines_dir: Path | None = None,
    known_roles: set[str] | None = None,
) -> PipelineDefinition:
    """加载并校验流水线定义。

    异常：找不到文件 -> ``NotFoundError``（1002）；
    结构非法 / 静态校验失败 -> ``SopDefinitionError``（1001）。
    """
    directory = pipelines_dir or DEFAULT_PIPELINES_DIR
    path = directory / f"{pipeline_id}.yaml"
    if not path.exists():
        raise NotFoundError(f"流水线定义不存在: {pipeline_id}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        definition = PipelineDefinition.model_validate(raw)
    except ValidationError as exc:
        raise SopDefinitionError(f"流水线定义结构非法（{pipeline_id}）: {exc}") from exc
    SopValidator(definition, known_roles=known_roles).raise_for_errors()
    return definition


def truncate_pipeline(
    definition: PipelineDefinition, upto_stage_id: str
) -> PipelineDefinition:
    """截断流水线至目标阶段（重建 ``stage_completed`` 主链；原定义不改）。

    用于单角色调试 / 分步演示（``scripts/run_single_role.py``）：
    从初始阶段沿 ``stage_completed`` 边走到目标阶段，只保留链上阶段，
    末阶段重接到 ``__end__``。
    """
    chain: list[str] = []
    current = definition.initial_stage().id
    while True:
        chain.append(current)
        if current == upto_stage_id:
            break
        nxt = definition.next_stage(current)
        if nxt is None or nxt == END_TOKEN:
            raise InvalidParamError(f"阶段链中不存在 {upto_stage_id}")
        current = nxt
    stages = [definition.stage(sid) for sid in chain]
    transitions = [
        TransitionSpec(
            **{
                "from": sid,
                "on": "stage_completed",
                "to": chain[i + 1] if i + 1 < len(chain) else END_TOKEN,
            }
        )
        for i, sid in enumerate(chain)
    ]
    return PipelineDefinition(
        id=definition.id,
        version=definition.version,
        description=f"{definition.description}（截断至 {upto_stage_id}）",
        stages=stages,  # type: ignore[list-item]
        transitions=transitions,
    )


async def sync_pipeline(
    session: AsyncSession,
    definition: PipelineDefinition,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Pipeline:
    """定义快照入库（同 id+version 幂等更新，不产生重复行）。"""
    stmt = select(Pipeline).where(
        Pipeline.id == definition.id, Pipeline.version == definition.version
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    snapshot = definition.snapshot()
    if existing is None:
        record = Pipeline(
            id=definition.id,
            version=definition.version,
            definition=snapshot,
            tenant_id=tenant_id,
        )
        session.add(record)
        await session.flush()
        return record
    existing.definition = snapshot
    await session.flush()
    return existing

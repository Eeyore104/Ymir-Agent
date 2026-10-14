"""流水线加载与快照入库（架构设计 §3.2）。

- ``load_pipeline``：YAML -> Pydantic 结构校验 -> SopValidator 静态校验；
- ``sync_pipeline``：定义快照版本化入 ``pipelines`` 表（id+version 幂等 upsert），
  为 v1.x 可视化编排留数据基础。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymir.common.errors import NotFoundError
from ymir.config import PROJECT_ROOT
from ymir.models.entities import DEFAULT_TENANT_ID, Pipeline
from ymir.sop.models import PipelineDefinition
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

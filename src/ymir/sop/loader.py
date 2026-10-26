"""流水线加载与快照入库（架构设计 §3.2）。

- ``load_pipeline``：YAML -> Pydantic 结构校验 -> SopValidator 静态校验；
- ``truncate_pipeline``：截断至目标阶段（单角色调试 / 分步演示）；
- ``sync_pipeline``：定义快照版本化入 ``pipelines`` 表（id+version+tenant 幂等 upsert），
  为 v1.x 可视化编排留数据基础。

v1.1 护栏（任务 #7）：YAML 1.1 会把**裸键** ``on`` 解析成布尔键 ``True``，导致流转边
的事件名静默回退成缺省 ``stage_completed``（v1.0 因全部边都是主链事件而从未暴露）。
加载期先做「非字符串键」检查并给出加引号提示，再交给 Pydantic（``extra="forbid"``
兜住纯多余键），双层拒绝而不是静默修正 —— 配置错误必须显式报出来。
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

#: 非字符串键的提示（YAML 1.1 布尔字面量：on/off/yes/no 作为键会被解析成 bool）
_QUOTE_HINT = "事件键 on 需加引号（YAML 1.1 会将裸 on 解析为布尔键）"


def _reject_non_string_transition_keys(raw: object, pipeline_id: str) -> None:
    """拒绝 ``transitions`` 里的非字符串键（YAML 1.1 布尔键护栏，任务 #7 第一层）。

    典型触发：``- { from: evaluation, on: evaluation_failed, to: coding }`` ——
    ``on`` 被 PyYAML 解析为 ``True``，``TransitionSpec.on`` 静默回退缺省值。
    """
    if not isinstance(raw, dict):
        return
    transitions = raw.get("transitions")
    if not isinstance(transitions, list):
        return
    for index, item in enumerate(transitions):
        if not isinstance(item, dict):
            continue
        for key in item:
            if not isinstance(key, str):
                raise SopDefinitionError(
                    f"流水线定义结构非法（{pipeline_id}）: "
                    f"transitions[{index}] 出现非字符串键 {key!r}；{_QUOTE_HINT}"
                )


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
    _reject_non_string_transition_keys(raw, pipeline_id)
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
    # ★保留「两端都在链上」的非主链边（v1.1 repair 回边：
    # ``evaluation --evaluation_failed--> coding``）。截断的语义是「裁剪阶段」，
    # 不是「只留线性主链」；否则单跑脚本永远走不到自愈回环 —— T5.1 这类
    # 「停在某阶段观察完整行为」的验证会丢掉 v1.1 的核心能力。
    # 目标阶段之后被裁掉的边仍丢弃（避免指向不存在的阶段）。
    chain_set = set(chain)
    for transition in definition.transitions:
        if transition.on == "stage_completed":
            continue  # 主链边已按截断规则重建，避免重复
        if transition.from_ in chain_set and transition.to in chain_set:
            transitions.append(transition)
    return PipelineDefinition(
        id=definition.id,
        version=definition.version,
        description=f"{definition.description}（截断至 {upto_stage_id}）",
        # ★repair 配置必须一起带过去：引擎靠它判定「是否启用回边 / 允许几轮」，
        # 漏传等于回边形同虚设（2026-10-26 T5.1 实测：评测 fail 后 run 直接
        # completed，从未回到 coding 修复）。
        repair=definition.repair,
        stages=stages,  # type: ignore[list-item]
        transitions=transitions,
    )


async def sync_pipeline(
    session: AsyncSession,
    definition: PipelineDefinition,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Pipeline:
    """定义快照入库（同 id+version+tenant 幂等更新，不产生重复行）。

    ``tenant_id`` 参与 upsert 定位（QA-B3-01）：同 ``(id, version)`` 可依租户
    各存一行（``pipelines`` 复合主键为 ``(id, version, tenant_id)``），跨租户
    互不覆盖；单租户下 tenant 恒定，行为与此前完全一致。
    """
    stmt = select(Pipeline).where(
        Pipeline.id == definition.id,
        Pipeline.version == definition.version,
        Pipeline.tenant_id == tenant_id,
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

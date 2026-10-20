"""审批挂点适配层（架构设计 §3.4 推进钩子、§3.8 HITL 三模式）。

编排内核（``OrchestrationEngine``）只认 ``ApprovalHook`` 契约
（``(run, stage, ctx=None) -> Awaitable[bool]``，返回 True 则阶段完成后转入
waiting_approval）；本模块把 :class:`~ymir.hitl.engine.ApprovalEngine`
包装成该契约，是「引擎挂点」与「HITL 策略 / 生命周期」之间的唯一接缝：

- :func:`build_approval_hook`：bootstrap 装配时注入引擎的挂点工厂；
  阶段完成 -> 策略判断（key_node 三关 / debug 全步骤 / batch 自动）->
  阻断或放行（batch 的自动通过轨迹由引擎落库）；
- v1.1 第三参 ``ctx``（:class:`~ymir.common.contracts.ApprovalContext`）：
  区分「阶段完成」与「终局候选」，并透传 ``repair_active`` / ``repair_round``
  供 HITL 引擎判断是否 ``defer`` 中间候选（§4.5.1）。

关卡语义纯函数（``is_human_gate`` / ``gate_name_of`` / ``reject_target_of``）
收口在 :mod:`ymir.hitl.policy`，避免 YAML ``gate`` 字段判空逻辑散落多处。
"""

from __future__ import annotations

from ymir.common.contracts import ApprovalContext
from ymir.hitl.engine import ApprovalEngine
from ymir.models.entities import PipelineRun
from ymir.orchestrator.engine import ApprovalHook
from ymir.sop.models import StageSpec


def build_approval_hook(approval_engine: ApprovalEngine) -> ApprovalHook:
    """把审批引擎包装为编排引擎的审批挂点（阶段完成 / 终局候选回调）。"""

    async def hook(
        run: PipelineRun,
        stage: StageSpec,
        ctx: ApprovalContext | None = None,
    ) -> bool:
        return await approval_engine.on_stage_completed(run, stage, ctx)

    return hook

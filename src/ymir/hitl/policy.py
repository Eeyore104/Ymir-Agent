"""HITL 审批策略（架构设计 §3.8）：三模式同一套机制，只是阻断策略不同。

- ``key_node``（默认）：仅流水线声明了 ``human_approval`` 关卡的阶段阻断；
- ``batch``：全自动 —— 所有关卡自动通过（不建阻断审批，但仍落审批轨迹供审计）；
- ``debug``：全步骤人审 —— 每个阶段完成都阻断，无论有无关卡。

模式来源为 ``PipelineRun.mode``（:class:`~ymir.models.enums.RunMode`），
项目级默认值由配置（``Settings.default_run_mode``）在创建 run 时兜底。

本模块同时收口「关卡语义」纯函数（gate 元数据读取口径唯一来源），
供审批引擎与挂点适配层共用，避免散落的 ``stage.gate`` 判空。
"""

from __future__ import annotations

from ymir.models.entities import PipelineRun
from ymir.models.enums import RunMode
from ymir.sop.models import StageSpec

#: 自动审批（batch 模式审计轨迹）的决策人标识
BATCH_DECIDER = "batch"

#: 人工审批默认决策人（单租户 MVP 无登录体系，§2.4）
DEFAULT_DECIDER = "human"

#: 决策取值（与 Approval.decision 落库值一致）
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
VALID_DECISIONS: frozenset[str] = frozenset({DECISION_APPROVE, DECISION_REJECT})

#: 人工关卡类型（YAML ``gate.type``）
HUMAN_GATE_TYPE = "human_approval"


def coerce_mode(value: str | RunMode | None, *, default: RunMode = RunMode.KEY_NODE) -> RunMode:
    """把模式值收敛为 RunMode（缺省 / 非法值回退默认模式）。"""
    if isinstance(value, RunMode):
        return value
    try:
        return RunMode(str(value))
    except ValueError:
        return default


def is_human_gate(stage: StageSpec) -> bool:
    """阶段是否声明了人工审批关卡。"""
    return stage.gate is not None and stage.gate.type == HUMAN_GATE_TYPE


def gate_name_of(stage: StageSpec) -> str:
    """关卡名：优先 ``gate.name``，缺省为阶段 ID（debug 模式无关卡元数据）。"""
    if stage.gate is not None and stage.gate.name:
        return stage.gate.name
    return stage.id


def reject_target_of(stage: StageSpec) -> str:
    """驳回回退目标：``gate.on_reject_target``，缺省为阶段自身（原地重跑）。"""
    if stage.gate is not None and stage.gate.on_reject_target:
        return stage.gate.on_reject_target
    return stage.id


class ApprovalPolicy:
    """审批策略：按运行模式判断阶段是否需要人工审批（阻断语义）。"""

    def __init__(self, mode: str | RunMode = RunMode.KEY_NODE) -> None:
        self._mode = coerce_mode(mode)

    @property
    def mode(self) -> RunMode:
        """当前策略模式。"""
        return self._mode

    @classmethod
    def for_run(
        cls,
        run: PipelineRun,
        *,
        default_mode: str | RunMode = RunMode.KEY_NODE,
    ) -> ApprovalPolicy:
        """按 ``run.mode`` 构造策略（缺省 / 非法值回落默认模式）。"""
        return cls(coerce_mode(run.mode, default=coerce_mode(default_mode)))

    def requires_approval(self, stage: StageSpec) -> bool:
        """该阶段是否需要人工审批（返回 True 时阶段完成后阻断）。

        - ``key_node``：仅 ``human_approval`` 关卡的阶段；
        - ``debug``：所有阶段；
        - ``batch``：恒为 False（调用方另行落自动审批轨迹供审计）。
        """
        if self._mode is RunMode.DEBUG:
            return True
        if self._mode is RunMode.BATCH:
            return False
        return is_human_gate(stage)

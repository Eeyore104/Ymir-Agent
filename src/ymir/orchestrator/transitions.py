"""状态机迁移表（架构设计 §3.4）。

集中定义 run / stage 的合法迁移，非法迁移抛 ``StateConflictError``（1003）。
引擎与 API 层共用本表，保证「状态冲突」判定只有一个来源。

关于 cancel：架构设计未定义 ``cancelled`` 状态，取消统一映射为
``failed``（error="cancelled by user"），语义为「保留现场、可 retry-stage」。
"""

from __future__ import annotations

from enum import Enum

from ymir.common.errors import StateConflictError
from ymir.models.enums import RunStatus, StageStatus


def _as_value(value: str | Enum) -> str:
    return value.value if isinstance(value, Enum) else str(value)


#: run 合法迁移（键为当前态，值为可到的目标态）
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RunStatus.PENDING.value: frozenset({RunStatus.RUNNING.value, RunStatus.FAILED.value}),
    RunStatus.RUNNING.value: frozenset(
        {
            RunStatus.WAITING_APPROVAL.value,
            RunStatus.PAUSED.value,
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
        }
    ),
    RunStatus.WAITING_APPROVAL.value: frozenset(
        {RunStatus.RUNNING.value, RunStatus.FAILED.value}
    ),
    RunStatus.PAUSED.value: frozenset({RunStatus.RUNNING.value, RunStatus.FAILED.value}),
    RunStatus.COMPLETED.value: frozenset(),
    # failed -> running：retry-stage / extend-repair；
    # failed -> completed：**accept-best 专用**（人工接受未达标最优版，§4.5.3）。
    # 端点白名单保持不变：其余入口（pause/resume/cancel/retry-stage）仍按各自语义
    # 走 ensure_run_transition，不会因本表放宽而误放行到 completed。
    RunStatus.FAILED.value: frozenset(
        {RunStatus.RUNNING.value, RunStatus.COMPLETED.value}
    ),
}

#: stage 合法迁移
STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    StageStatus.PENDING.value: frozenset({StageStatus.RUNNING.value}),
    StageStatus.RUNNING.value: frozenset(
        {StageStatus.COMPLETED.value, StageStatus.FAILED.value}
    ),
    StageStatus.COMPLETED.value: frozenset(),
    StageStatus.FAILED.value: frozenset(),
}

#: run 终态（不可再迁移，retry-stage 除外）
RUN_TERMINAL: frozenset[str] = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value}
)


def can_run_transition(current: str | Enum, target: str | Enum) -> bool:
    """判断 run 迁移是否合法。"""
    return _as_value(target) in RUN_TRANSITIONS.get(_as_value(current), frozenset())


def ensure_run_transition(current: str | Enum, target: str | Enum) -> None:
    """校验 run 迁移；非法抛 ``StateConflictError``（1003）。"""
    if not can_run_transition(current, target):
        raise StateConflictError(f"非法 run 状态迁移: {_as_value(current)} -> {_as_value(target)}")


def can_stage_transition(current: str | Enum, target: str | Enum) -> bool:
    """判断 stage 迁移是否合法。"""
    return _as_value(target) in STAGE_TRANSITIONS.get(_as_value(current), frozenset())


def ensure_stage_transition(current: str | Enum, target: str | Enum) -> None:
    """校验 stage 迁移；非法抛 ``StateConflictError``（1003）。"""
    if not can_stage_transition(current, target):
        raise StateConflictError(
            f"非法 stage 状态迁移: {_as_value(current)} -> {_as_value(target)}"
        )

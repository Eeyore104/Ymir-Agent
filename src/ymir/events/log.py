"""事件类型定义（架构设计 §6.2，八类 SSE 事件统一信封）。

信封：``{seq, type, run_id, timestamp, payload}``；seq 按 run 单调递增。
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """SSE 事件类型全集（docs/02 §6.2）。"""

    # 运行生命周期
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_PAUSED = "run.paused"
    # 阶段生命周期
    STAGE_STARTED = "stage.started"
    STAGE_COMPLETED = "stage.completed"
    STAGE_FAILED = "stage.failed"
    STAGE_LOG = "stage.log"
    # 审批生命周期（T04）
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    # 产物 / 消息 / 用量
    ARTIFACT_CREATED = "artifact.created"
    MESSAGE_CREATED = "message.created"
    USAGE_UPDATED = "usage.updated"
    BUDGET_EXCEEDED = "budget.exceeded"
    # 评测（T05）
    EVALUATION_COMPLETED = "evaluation.completed"

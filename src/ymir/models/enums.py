"""状态与类型枚举（架构设计 §3.4、§3.6、§3.8、§3.9）。

均以 ``str`` 值落库，保持列类型简单、跨库安全。
"""

from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    """HITL 三模式（§3.8）。"""

    KEY_NODE = "key_node"
    BATCH = "batch"
    DEBUG = "debug"


class RunStatus(StrEnum):
    """PipelineRun 状态机（§3.4）。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class StageStatus(StrEnum):
    """StageExecution 状态机（§3.4）。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactType(StrEnum):
    """产物类型（§3.6）。

    ``BASELINE``（v1.2，docs/09 §4.4.1）：用户上传的**现有项目快照**，是独立不可变
    产物（``name="baseline"``）。``Artifact.type`` 是 ``String(32)``，加枚举值**零迁移**。
    """

    PRD = "prd"
    DESIGN = "design"
    CODE = "code"
    TEST = "test"
    REPORT = "report"
    DOC = "doc"
    BASELINE = "baseline"


class MessageType(StrEnum):
    """角色间消息类型（§3.3）。"""

    ARTIFACT = "artifact"
    FEEDBACK = "feedback"
    QUERY = "query"
    STATUS = "status"
    SYSTEM = "system"


class ApprovalStatus(StrEnum):
    """审批状态（§3.8）。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SandboxStatus(StrEnum):
    """沙箱会话状态（§3.5）。"""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    DESTROYED = "destroyed"


class ModelTier(StrEnum):
    """模型分级（§3.9）。"""

    PLANNER = "planner"
    EXECUTOR = "executor"

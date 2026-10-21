"""ORM 实体（架构设计 §3.10，共 11 张表）。

约定（§10.3）：
- ID 为 UUID v4 字符串；时间以 ISO 8601 UTC 存储；
- 所有实体携带 ``tenant_id``（MVP 恒为 ``default``，§2.4 扩展点）；
- ``events.seq`` / ``messages.seq`` 按 run 单调递增，唯一约束支撑断线重放。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ymir.models.base import Base, TZDateTime, new_uuid, utcnow
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

DEFAULT_TENANT_ID = "default"


class Project(Base):
    """项目。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    runs: Mapped[list[PipelineRun]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class PipelineRun(Base):
    """一次流水线运行（checkpoint 主体，§3.4）。"""

    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pipeline_id: Mapped[str] = mapped_column(String(128), nullable=False, default="software-dev")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default=RunMode.KEY_NODE.value)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RunStatus.PENDING.value, index=True
    )
    current_stage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 失败/暂停对应的业务错误码（§6.3，如 1004 预算 / 1005 沙箱 / 1006 模型；PM-10）
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="runs")
    stages: Mapped[list[StageExecution]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list[Event]] = relationship(back_populates="run", cascade="all, delete-orphan")
    model_calls: Mapped[list[ModelCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    sandbox_sessions: Mapped[list[SandboxSession]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class StageExecution(Base):
    """阶段执行记录（checkpoint 载体，§3.4）。"""

    __tablename__ = "stage_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StageStatus.PENDING.value, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    output_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    run: Mapped[PipelineRun] = relationship(back_populates="stages")


class Artifact(Base):
    """产物（代码 / PRD / 设计 / 评测报告统一管理，§3.6）。"""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ArtifactType.DOC.value, index=True
    )
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="artifacts")
    versions: Mapped[list[ArtifactVersion]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )


class ArtifactVersion(Base):
    """产物版本快照元数据（内容只进文件系统，§3.6）。"""

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    artifact: Mapped[Artifact] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),)


class Message(Base):
    """角色间结构化消息（文档先行，§3.3）。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MessageType.SYSTEM.value, index=True
    )
    from_role: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_role: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    run: Mapped[PipelineRun] = relationship(back_populates="messages")

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_message_run_seq"),)


class Approval(Base):
    """人工审批请求（§3.8）。"""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gate_name: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApprovalStatus.PENDING.value, index=True
    )
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    run: Mapped[PipelineRun] = relationship(back_populates="approvals")


class ModelCall(Base):
    """模型调用用量计量（§3.9）。"""

    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False, default=ModelTier.EXECUTOR.value)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    run: Mapped[PipelineRun | None] = relationship(back_populates="model_calls")


class Event(Base):
    """事件日志（SSE 重放源，§3.4、§6.2）。"""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

    run: Mapped[PipelineRun] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),)


class SandboxSession(Base):
    """沙箱会话记录（§3.5）。"""

    __tablename__ = "sandbox_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    image: Mapped[str] = mapped_column(String(255), nullable=False, default="ymir-sandbox:latest")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SandboxStatus.CREATED.value, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)
    destroyed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="sandbox_sessions")


class Pipeline(Base):
    """流水线定义快照（可版本化，§3.2、§3.10）。"""

    __tablename__ = "pipelines"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: tenant_id 纳入复合主键（QA-B3-01）：同 (id, version) 可依租户各存一行，
    #: 与 sync_pipeline 的 upsert 定位键保持一致，杜绝跨租户互相覆盖。
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TENANT_ID, primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)

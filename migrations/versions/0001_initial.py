"""initial schema: 11 tables

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TZDateTime 以 String(32) 落库（ISO 8601 UTC）
TS = sa.String(length=32)


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pipeline_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_stage_id", sa.String(length=128), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_pipeline_runs_project_id", "pipeline_runs", ["project_id"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_pipeline_runs_tenant_id", "pipeline_runs", ["tenant_id"])

    op.create_table(
        "stage_executions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_id", sa.String(length=128), nullable=False),
        sa.Column("role_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_refs", sa.JSON(), nullable=False),
        sa.Column("output_refs", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_stage_executions_run_id", "stage_executions", ["run_id"])
    op.create_index("ix_stage_executions_status", "stage_executions", ["status"])
    op.create_index("ix_stage_executions_tenant_id", "stage_executions", ["tenant_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_type", "artifacts", ["type"])
    op.create_index("ix_artifacts_tenant_id", "artifacts", ["tenant_id"])

    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(length=36),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stage_id", sa.String(length=128), nullable=True),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "parent_version_id",
            sa.String(length=36),
            sa.ForeignKey("artifact_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
    )
    op.create_index("ix_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"])
    op.create_index("ix_artifact_versions_run_id", "artifact_versions", ["run_id"])
    op.create_index("ix_artifact_versions_tenant_id", "artifact_versions", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("from_role", sa.String(length=128), nullable=True),
        sa.Column("to_role", sa.String(length=128), nullable=True),
        sa.Column("content_refs", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_message_run_seq"),
    )
    op.create_index("ix_messages_run_id", "messages", ["run_id"])
    op.create_index("ix_messages_type", "messages", ["type"])
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_id", sa.String(length=128), nullable=False),
        sa.Column("gate_name", sa.String(length=255), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_tenant_id", "approvals", ["tenant_id"])

    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stage_id", sa.String(length=128), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_model_calls_run_id", "model_calls", ["run_id"])
    op.create_index("ix_model_calls_tenant_id", "model_calls", ["tenant_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
    )
    op.create_index("ix_events_run_id", "events", ["run_id"])
    op.create_index("ix_events_type", "events", ["type"])
    op.create_index("ix_events_tenant_id", "events", ["tenant_id"])

    op.create_table(
        "sandbox_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("image", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("destroyed_at", TS, nullable=True),
    )
    op.create_index("ix_sandbox_sessions_run_id", "sandbox_sessions", ["run_id"])
    op.create_index("ix_sandbox_sessions_status", "sandbox_sessions", ["status"])
    op.create_index("ix_sandbox_sessions_tenant_id", "sandbox_sessions", ["tenant_id"])

    op.create_table(
        "pipelines",
        sa.Column("id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("version", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_pipelines_tenant_id", "pipelines", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("pipelines")
    op.drop_table("sandbox_sessions")
    op.drop_table("events")
    op.drop_table("model_calls")
    op.drop_table("approvals")
    op.drop_table("messages")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
    op.drop_table("stage_executions")
    op.drop_table("pipeline_runs")
    op.drop_table("projects")

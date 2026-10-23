"""推送回执表 ``exports``（v1.2，docs/09 §4.4.1 / §12-Q4 定案 A）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23

设计要点（与 ``models/entities.py::Export`` 严格一致，否则 ``alembic check`` 会报 drift）：

- **双向可逆**：``downgrade`` 直接 drop 表与两个索引；
- ``idempotency_key`` **不设唯一约束**（与 docs/09 §4.4.1 的 partial index 不同）：
  每次尝试都写一条回执，远端 head 变化后"同键再推"是合法的新回执，唯一索引会把
  这条正常路径堵死；并发裁决交给 GitHub 的 ref 更新（原子）。只建普通索引供
  "按幂等键查最近回执"用；
- 时间列沿用既有口径 ``String(32)``（``TZDateTime`` 的落库形态）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TZDateTime 以 String(32) 落库（ISO 8601 UTC）
TS = sa.String(length=32)


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(length=36),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_name", sa.String(length=128), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("tree_mode", sa.String(length=16), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("path_prefix", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
    )
    op.create_index("ix_exports_tenant_id", "exports", ["tenant_id"])
    op.create_index("ix_exports_status", "exports", ["status"])
    op.create_index("ix_exports_idempotency_key", "exports", ["idempotency_key"])
    op.create_index("ix_exports_project", "exports", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_exports_project", table_name="exports")
    op.drop_index("ix_exports_idempotency_key", table_name="exports")
    op.drop_index("ix_exports_status", table_name="exports")
    op.drop_index("ix_exports_tenant_id", table_name="exports")
    op.drop_table("exports")

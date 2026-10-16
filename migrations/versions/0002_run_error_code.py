"""add pipeline_runs.error_code（PM-10：run 失败码透传）

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 需 batch 模式重建表（env.py 已设 render_as_batch=True）
    with op.batch_alter_table("pipeline_runs") as batch_op:
        batch_op.add_column(sa.Column("error_code", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pipeline_runs") as batch_op:
        batch_op.drop_column("error_code")

"""pipelines: tenant_id 纳入复合主键（QA-B3-01 多租户 upsert 定位）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TZDateTime 以 String(32) 落库（ISO 8601 UTC）
TS = sa.String(length=32)


def upgrade() -> None:
    # SQLite 不支持 ALTER 主键约束：显式建新表 -> 拷数据 -> 换名。
    # tenant_id 纳入复合主键后，同 (id, version) 可依租户各存一行，
    # 与 sync_pipeline 的 upsert 定位键（id + version + tenant_id）一致（QA-B3-01）。
    op.create_table(
        "pipelines_new",
        sa.Column("id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("version", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.execute(
        "INSERT INTO pipelines_new (id, version, definition, tenant_id, created_at) "
        "SELECT id, version, definition, tenant_id, created_at FROM pipelines"
    )
    op.drop_table("pipelines")
    op.rename_table("pipelines_new", "pipelines")
    op.create_index("ix_pipelines_tenant_id", "pipelines", ["tenant_id"])


def downgrade() -> None:
    # 逆向回到 (id, version) 主键：若已存在跨租户同 (id, version) 行，
    # 每个 (id, version) 仅保留 rowid 最小（最早写入）的一行，避免主键冲突。
    op.create_table(
        "pipelines_old",
        sa.Column("id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("version", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.execute(
        "INSERT INTO pipelines_old (id, version, definition, tenant_id, created_at) "
        "SELECT id, version, definition, tenant_id, created_at FROM pipelines "
        "WHERE rowid IN (SELECT MIN(rowid) FROM pipelines GROUP BY id, version)"
    )
    op.drop_table("pipelines")
    op.rename_table("pipelines_old", "pipelines")
    op.create_index("ix_pipelines_tenant_id", "pipelines", ["tenant_id"])

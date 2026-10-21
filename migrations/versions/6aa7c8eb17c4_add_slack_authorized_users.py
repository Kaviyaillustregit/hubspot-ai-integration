"""add slack authorized users

Revision ID: 6aa7c8eb17c4
Revises: e475126bdbc0
Create Date: 2026-10-16 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6aa7c8eb17c4"
down_revision: str | None = "3f812219a0d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_authorized_users",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("slack_team_id", sa.String(length=64), nullable=False),
        sa.Column("slack_user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="sales_agent"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "slack_team_id", "slack_user_id"),
    )
    op.create_index(
        op.f("ix_slack_authorized_users_active"),
        "slack_authorized_users",
        ["active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_slack_authorized_users_role"),
        "slack_authorized_users",
        ["role"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_slack_authorized_users_role"), table_name="slack_authorized_users")
    op.drop_index(op.f("ix_slack_authorized_users_active"), table_name="slack_authorized_users")
    op.drop_table("slack_authorized_users")

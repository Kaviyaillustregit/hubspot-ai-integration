"""add hubspot oauth state and token storage

Revision ID: 0001_add_hubspot_oauth
Revises:
Create Date: 2026-09-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_add_hubspot_oauth"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hubspot_oauth_states",
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nonce"),
    )
    op.create_index(
        "ix_hubspot_oauth_states_tenant_id",
        "hubspot_oauth_states",
        ["tenant_id"],
    )
    op.create_index(
        "ix_hubspot_oauth_states_expires_at",
        "hubspot_oauth_states",
        ["expires_at"],
    )
    op.create_table(
        "hubspot_oauth_tokens",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("hubspot_account_id", sa.String(length=64), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("hubspot_account_id"),
    )
    op.create_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        "hubspot_oauth_tokens",
        ["hubspot_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        table_name="hubspot_oauth_tokens",
    )
    op.drop_table("hubspot_oauth_tokens")
    op.drop_index("ix_hubspot_oauth_states_expires_at", table_name="hubspot_oauth_states")
    op.drop_index("ix_hubspot_oauth_states_tenant_id", table_name="hubspot_oauth_states")
    op.drop_table("hubspot_oauth_states")

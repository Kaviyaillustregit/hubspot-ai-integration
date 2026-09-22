"""align hubspot account id unique index

Revision ID: 3f812219a0d6
Revises: e475126bdbc0
Create Date: 2026-09-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3f812219a0d6"
down_revision: str | None = "e475126bdbc0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "hubspot_oauth_tokens_hubspot_account_id_key",
        "hubspot_oauth_tokens",
        type_="unique",
    )

    op.drop_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        table_name="hubspot_oauth_tokens",
    )

    op.create_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        "hubspot_oauth_tokens",
        ["hubspot_account_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        table_name="hubspot_oauth_tokens",
    )

    op.create_index(
        "ix_hubspot_oauth_tokens_hubspot_account_id",
        "hubspot_oauth_tokens",
        ["hubspot_account_id"],
        unique=False,
    )

    op.create_unique_constraint(
        "hubspot_oauth_tokens_hubspot_account_id_key",
        "hubspot_oauth_tokens",
        ["hubspot_account_id"],
    )
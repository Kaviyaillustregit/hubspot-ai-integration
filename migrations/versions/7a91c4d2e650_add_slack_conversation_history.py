"""add Slack Home conversation history"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a91c4d2e650"
down_revision: str | None = "6aa7c8eb17c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_conversation_exchanges",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("slack_team_id", sa.String(length=128), nullable=False),
        sa.Column("slack_user_id", sa.String(length=128), nullable=False),
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("response_status", sa.String(length=64), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("render_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "slack_team_id", "slack_user_id", "delivery_id",
            name="uq_slack_conversation_delivery",
        ),
    )
    op.create_index(
        "ix_slack_conversation_owner_created",
        "slack_conversation_exchanges",
        ["tenant_id", "slack_team_id", "slack_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_slack_conversation_owner_created", table_name="slack_conversation_exchanges"
    )
    op.drop_table("slack_conversation_exchanges")

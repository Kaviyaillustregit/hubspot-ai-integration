from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SlackConversationExchange(Base):
    __tablename__ = "slack_conversation_exchanges"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "slack_team_id", "slack_user_id", "delivery_id",
            name="uq_slack_conversation_delivery",
        ),
        Index(
            "ix_slack_conversation_owner_created",
            "tenant_id", "slack_team_id", "slack_user_id", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    slack_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[str] = mapped_column(String(64), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    render_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

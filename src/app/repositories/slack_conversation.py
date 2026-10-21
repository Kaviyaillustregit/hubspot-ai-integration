from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.conversation_models import SlackConversationExchange


class SlackConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_once(
        self,
        *,
        tenant_id: str,
        team_id: str,
        user_id: str,
        delivery_id: str,
        question: str,
        response_status: str,
        response_text: str,
        render_data: dict[str, Any],
    ) -> bool:
        statement = (
            insert(SlackConversationExchange)
            .values(
                id=uuid4().hex,
                tenant_id=tenant_id,
                slack_team_id=team_id,
                slack_user_id=user_id,
                delivery_id=delivery_id,
                question=question,
                response_status=response_status,
                response_text=response_text,
                render_data=render_data,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_slack_conversation_delivery")
        )
        result = await self._session.execute(statement)
        return result.rowcount == 1

    async def latest(
        self, *, tenant_id: str, team_id: str, user_id: str, limit: int = 5
    ) -> list[SlackConversationExchange]:
        result = await self._session.execute(
            select(SlackConversationExchange)
            .where(
                SlackConversationExchange.tenant_id == tenant_id,
                SlackConversationExchange.slack_team_id == team_id,
                SlackConversationExchange.slack_user_id == user_id,
            )
            .order_by(
                SlackConversationExchange.created_at.desc(),
                SlackConversationExchange.id.desc(),
            )
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def get_owned(
        self,
        *,
        tenant_id: str,
        team_id: str,
        user_id: str,
        delivery_id: str,
    ) -> SlackConversationExchange | None:
        result = await self._session.execute(
            select(SlackConversationExchange).where(
                SlackConversationExchange.tenant_id == tenant_id,
                SlackConversationExchange.slack_team_id == team_id,
                SlackConversationExchange.slack_user_id == user_id,
                SlackConversationExchange.delivery_id == delivery_id,
            )
        )
        return result.scalar_one_or_none()

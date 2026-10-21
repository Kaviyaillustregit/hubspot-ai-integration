from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import AgentResponse
from app.db.session import UnitOfWork
from app.repositories.slack_conversation import SlackConversationRepository

_SENSITIVE_KEYS = {
    "token", "secret", "authorization", "headers", "api_key", "password",
    "action_id",
}
_RENDER_RESULT_KEYS = {
    "kind", "title", "table", "chart_url", "chart_title", "chart_summary",
    "hubspot_url", "name", "company_name", "email", "contact_id", "action_type",
    "record_label",
}


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEYS)
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def response_render_data(response: AgentResponse) -> dict[str, Any]:
    """Keep only renderer inputs; omit request IDs and internal tool metadata."""
    return {
        "result": {
            key: _safe_json(value)
            for key, value in (response.result or {}).items()
            if key in _RENDER_RESULT_KEYS
        },
        "cards": [
            {"hubspot_url": card["hubspot_url"]}
            for card in response.cards
            if isinstance(card.get("hubspot_url"), str)
        ],
    }


@dataclass(frozen=True)
class ConversationExchange:
    delivery_id: str
    question: str
    response_status: str
    response_text: str
    render_data: dict[str, Any]


class SlackConversationService:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_completed(
        self,
        *,
        tenant_id: str,
        team_id: str,
        user_id: str,
        delivery_id: str,
        question: str,
        response: AgentResponse,
    ) -> bool:
        if (
            not delivery_id
            or not question.strip()
            or response.status in {"duplicate_request", "unavailable"}
        ):
            return False
        async with UnitOfWork(self._session_factory) as unit:
            if unit.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")
            inserted = await SlackConversationRepository(unit.session).create_once(
                tenant_id=tenant_id,
                team_id=team_id,
                user_id=user_id,
                delivery_id=delivery_id,
                question=question,
                response_status=response.status,
                response_text=response.text,
                render_data=response_render_data(response),
            )
            await unit.commit()
            return inserted

    async def latest(
        self, *, tenant_id: str, team_id: str, user_id: str, limit: int = 5
    ) -> list[ConversationExchange]:
        async with UnitOfWork(self._session_factory) as unit:
            if unit.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")
            rows = await SlackConversationRepository(unit.session).latest(
                tenant_id=tenant_id, team_id=team_id, user_id=user_id, limit=limit
            )
            return [
                ConversationExchange(
                    delivery_id=row.delivery_id,
                    question=row.question,
                    response_status=row.response_status,
                    response_text=row.response_text,
                    render_data=dict(row.render_data),
                )
                for row in rows
            ]

    async def get_owned(
        self,
        *,
        tenant_id: str,
        team_id: str,
        user_id: str,
        delivery_id: str,
    ) -> ConversationExchange | None:
        """Fetch one saved answer only within its tenant, workspace, and user scope."""
        async with UnitOfWork(self._session_factory) as unit:
            if unit.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")
            row = await SlackConversationRepository(unit.session).get_owned(
                tenant_id=tenant_id,
                team_id=team_id,
                user_id=user_id,
                delivery_id=delivery_id,
            )
            if row is None:
                return None
            return ConversationExchange(
                delivery_id=row.delivery_id,
                question=row.question,
                response_status=row.response_status,
                response_text=row.response_text,
                render_data=dict(row.render_data),
            )

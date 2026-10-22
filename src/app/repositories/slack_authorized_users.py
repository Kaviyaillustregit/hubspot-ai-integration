from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.authorized_users import SlackAuthorizedUserRecord


@dataclass(frozen=True)
class SlackAuthorizedUser:
    tenant_id: str
    slack_team_id: str
    slack_user_id: str
    role: str
    active: bool


class SlackAuthorizedUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_authorized(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str) -> bool:
        result = await self._session.execute(
            select(SlackAuthorizedUserRecord.active)
            .where(
                SlackAuthorizedUserRecord.tenant_id == tenant_id,
                SlackAuthorizedUserRecord.slack_team_id == slack_team_id,
                SlackAuthorizedUserRecord.slack_user_id == slack_user_id,
                SlackAuthorizedUserRecord.active.is_(True),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is True

    async def is_admin(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str) -> bool:
        result = await self._session.execute(
            select(SlackAuthorizedUserRecord.role)
            .where(
                SlackAuthorizedUserRecord.tenant_id == tenant_id,
                SlackAuthorizedUserRecord.slack_team_id == slack_team_id,
                SlackAuthorizedUserRecord.slack_user_id == slack_user_id,
                SlackAuthorizedUserRecord.active.is_(True),
                SlackAuthorizedUserRecord.role == "admin",
            )
            .limit(1)
        )
        return result.scalar_one_or_none() == "admin"

    async def list_for_tenant(self, *, tenant_id: str, slack_team_id: str | None = None) -> list[SlackAuthorizedUser]:
        stmt = select(SlackAuthorizedUserRecord).where(
            SlackAuthorizedUserRecord.tenant_id == tenant_id,
            SlackAuthorizedUserRecord.active.is_(True),
        )
        if slack_team_id is not None:
            stmt = stmt.where(SlackAuthorizedUserRecord.slack_team_id == slack_team_id)
        result = await self._session.execute(stmt.order_by(SlackAuthorizedUserRecord.slack_user_id))
        return [
            SlackAuthorizedUser(
                tenant_id=record.tenant_id,
                slack_team_id=record.slack_team_id,
                slack_user_id=record.slack_user_id,
                role=record.role,
                active=record.active,
            )
            for record in result.scalars().all()
        ]

    async def upsert(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str, role: str, active: bool = True) -> None:
        record = await self._session.get(
            SlackAuthorizedUserRecord,
            (tenant_id, slack_team_id, slack_user_id),
        )
        if record is None:
            self._session.add(
                SlackAuthorizedUserRecord(
                    tenant_id=tenant_id,
                    slack_team_id=slack_team_id,
                    slack_user_id=slack_user_id,
                    role=role,
                    active=active,
                )
            )
            return
        record.role = role
        record.active = active
        record.updated_at = __import__("datetime").datetime.now(__import__("datetime").UTC)

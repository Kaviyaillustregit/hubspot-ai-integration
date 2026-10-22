from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.slack_authorized_users import SlackAuthorizedUserRepository


class SlackAuthorizationService:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def is_authorized(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str) -> bool:
        async with self._session_factory() as session:
            return await SlackAuthorizedUserRepository(session).is_authorized(
                tenant_id=tenant_id,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
            )

    async def is_admin(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str) -> bool:
        async with self._session_factory() as session:
            return await SlackAuthorizedUserRepository(session).is_admin(
                tenant_id=tenant_id,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
            )

    async def list_for_tenant(self, *, tenant_id: str, slack_team_id: str | None = None) -> list[object]:
        async with self._session_factory() as session:
            return await SlackAuthorizedUserRepository(session).list_for_tenant(
                tenant_id=tenant_id,
                slack_team_id=slack_team_id,
            )

    async def bootstrap_admin(self, *, tenant_id: str, slack_team_id: str, slack_user_id: str) -> bool:
        if not slack_team_id or not slack_user_id:
            return False
        async with self._session_factory() as session:
            repository = SlackAuthorizedUserRepository(session)
            existing = await repository.is_authorized(
                tenant_id=tenant_id,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
            )
            if existing:
                return False
            await repository.upsert(
                tenant_id=tenant_id,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                role="admin",
                active=True,
            )
            await session.commit()
            return True

"""Explicit operator actions for provisioning Slack agent access."""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.authorized_users import SlackAuthorizedUserRecord


class ProvisioningResult(StrEnum):
    GRANTED = "granted"
    ALREADY_ACTIVE = "already_active"
    REVOKED = "revoked"
    ALREADY_INACTIVE = "already_inactive"
    NOT_FOUND = "not_found"


class SlackAuthorizationProvisioner:
    """Grant/revoke one exact tenant/workspace/user authorization tuple."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def grant_access(
        self, *, tenant_id: str, slack_team_id: str, slack_user_id: str
    ) -> ProvisioningResult:
        tenant_id, slack_team_id, slack_user_id = _validate_identity(
            tenant_id, slack_team_id, slack_user_id
        )
        async with self._session_factory() as session:
            async with session.begin():
                record = await session.get(
                    SlackAuthorizedUserRecord,
                    (tenant_id, slack_team_id, slack_user_id),
                )
                if record is None:
                    session.add(
                        SlackAuthorizedUserRecord(
                            tenant_id=tenant_id,
                            slack_team_id=slack_team_id,
                            slack_user_id=slack_user_id,
                            role="sales_agent",
                            active=True,
                        )
                    )
                    return ProvisioningResult.GRANTED

                if record.role != "sales_agent":
                    raise ValueError("Grant operation is limited to sales_agent records")
                if record.active:
                    return ProvisioningResult.ALREADY_ACTIVE

                record.active = True
                record.updated_at = datetime.now(UTC)
                return ProvisioningResult.GRANTED

    async def revoke_access(
        self, *, tenant_id: str, slack_team_id: str, slack_user_id: str
    ) -> ProvisioningResult:
        tenant_id, slack_team_id, slack_user_id = _validate_identity(
            tenant_id, slack_team_id, slack_user_id
        )
        async with self._session_factory() as session:
            async with session.begin():
                record = await session.get(
                    SlackAuthorizedUserRecord,
                    (tenant_id, slack_team_id, slack_user_id),
                )
                if record is None:
                    return ProvisioningResult.NOT_FOUND
                if not record.active:
                    return ProvisioningResult.ALREADY_INACTIVE

                record.active = False
                record.updated_at = datetime.now(UTC)
                return ProvisioningResult.REVOKED


def _validate_identity(tenant_id: str, team_id: str, user_id: str) -> tuple[str, str, str]:
    identities = (
        ("tenant ID", tenant_id, 128),
        ("Slack workspace ID", team_id, 64),
        ("Slack user ID", user_id, 64),
    )
    normalized: list[str] = []
    for label, value, max_length in identities:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must be non-empty and contain no surrounding whitespace")
        if len(value) > max_length:
            raise ValueError(f"{label} exceeds the supported length")
        normalized.append(value)
    return normalized[0], normalized[1], normalized[2]

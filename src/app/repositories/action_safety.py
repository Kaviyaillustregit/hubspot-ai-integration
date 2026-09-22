from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_models import (
    AuditLogRecord,
    IdempotencyRecord,
    PendingActionRecord,
)


class PendingActionRepository:
    """Persistence boundary for tenant-scoped pending CRM actions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        action_type: str,
        resource_type: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> PendingActionRecord:
        record = PendingActionRecord(
            id=action_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action_type=action_type,
            resource_type=resource_type,
            payload=payload,
            status="pending",
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        self._session.add(record)
        return record

    async def confirm(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
    ) -> PendingActionRecord | None:
        now = datetime.now(UTC)

        result = await self._session.execute(
            select(PendingActionRecord)
            .where(
                PendingActionRecord.id == action_id,
                PendingActionRecord.tenant_id == tenant_id,
                PendingActionRecord.actor_id == actor_id,
                PendingActionRecord.status == "pending",
                PendingActionRecord.expires_at > now,
            )
            .with_for_update()
        )
        record = result.scalar_one_or_none()

        if record is None:
            return None

        record.status = "confirmed"
        record.confirmed_at = now
        return record

    async def set_status(
        self,
        *,
        action_id: str,
        tenant_id: str,
        status: str,
    ) -> bool:
        if status not in {
            "completed",
            "failed",
            "reconciliation_required",
        }:
            return False

        result = await self._session.execute(
            update(PendingActionRecord)
            .where(
                PendingActionRecord.id == action_id,
                PendingActionRecord.tenant_id == tenant_id,
                PendingActionRecord.status == "confirmed",
            )
            .values(status=status)
        )

        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount) == 1


class AuditLogRepository:
    """Append-only application audit record persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        audit_id: str,
        tenant_id: str,
        actor_id: str,
        request_id: str,
        action_type: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str,
        event_data: dict[str, Any],
    ) -> AuditLogRecord:
        record = AuditLogRecord(
            id=audit_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            request_id=request_id,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            event_data=event_data,
            created_at=datetime.now(UTC),
        )
        self._session.add(record)
        return record


class IdempotencyRepository:
    """Persistence boundary preventing duplicate mutation execution."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        key: str,
        tenant_id: str,
        action_type: str,
        request_fingerprint: str,
    ) -> bool:
        now = datetime.now(UTC)

        statement = (
            insert(IdempotencyRecord)
            .values(
                key=key,
                tenant_id=tenant_id,
                action_type=action_type,
                request_fingerprint=request_fingerprint,
                status="in_progress",
                result=None,
                created_at=now,
                claimed_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[IdempotencyRecord.key]
            )
        )

        result = await self._session.execute(statement)
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount) == 1

    async def get(self, key: str) -> IdempotencyRecord | None:
        return await self._session.get(IdempotencyRecord, key)

    async def complete(
        self,
        *,
        key: str,
        status: str,
        result: dict[str, Any] | None,
    ) -> bool:
        statement = (
            update(IdempotencyRecord)
            .where(IdempotencyRecord.key == key)
            .values(
                status=status,
                result=result,
            )
        )
        result_proxy = await self._session.execute(statement)
        rowcount = getattr(result_proxy, "rowcount", 0)
        return int(rowcount) == 1
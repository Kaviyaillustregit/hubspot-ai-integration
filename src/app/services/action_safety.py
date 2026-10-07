import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import UnitOfWork
from app.repositories.action_safety import (
    AuditLogRepository,
    IdempotencyRepository,
    PendingActionRepository,
)


@dataclass(frozen=True)
class ConfirmedAction:
    id: str
    tenant_id: str
    actor_id: str
    action_type: str
    resource_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DirectActionClaim:
    action_id: str
    claimed: bool
    previous_status: str | None = None


@dataclass(frozen=True)
class RecentAction:
    action_type: str
    status: str
    payload: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class ActionSafetyService:
    """Coordinates pending actions, confirmation, idempotency, and audit records."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_pending_action(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        action_type: str,
        resource_type: str,
        payload: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> str:
        action_id = uuid4().hex

        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            repository = PendingActionRepository(unit_of_work.session)

            await repository.create(
                action_id=action_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action_type=action_type,
                resource_type=resource_type,
                payload=payload,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )

            await unit_of_work.commit()

        return action_id

    async def recent_actions(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        limit: int = 5,
    ) -> list[RecentAction]:
        """Read-only view of an actor's latest CRM actions within their tenant."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            records = await PendingActionRepository(unit_of_work.session).list_recent(
                tenant_id=tenant_id,
                actor_id=actor_id,
                limit=limit,
            )
            return [
                RecentAction(
                    action_type=record.action_type,
                    status=record.status,
                    payload=dict(record.payload),
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
                for record in records
            ]

    @staticmethod
    def direct_action_id(*, tenant_id: str, action_type: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            f"{tenant_id}\x1f{action_type}\x1f{idempotency_key}".encode()
        ).hexdigest()
        return f"direct_{digest}"

    async def start_direct_action(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        actor_id: str,
        action_type: str,
        resource_type: str,
        payload: dict[str, Any],
        request_fingerprint: str,
        ttl_seconds: int = 300,
    ) -> DirectActionClaim:
        """Claim an action that runs without confirmation.

        The action id is derived from the idempotency key, so a redelivered request
        maps to the same id and loses the claim instead of executing twice. The claimed
        action is recorded as already confirmed, so complete_action/fail_action apply.
        """
        action_id = self.direct_action_id(
            tenant_id=tenant_id,
            action_type=action_type,
            idempotency_key=idempotency_key,
        )

        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(unit_of_work.session)
            idempotency_repository = IdempotencyRepository(unit_of_work.session)

            claimed = await idempotency_repository.claim(
                key=action_id,
                tenant_id=tenant_id,
                action_type=action_type,
                request_fingerprint=request_fingerprint,
            )

            if not claimed:
                existing = await idempotency_repository.get(action_id)
                # Read before rollback: rollback expires ORM instances, and reloading an
                # expired attribute is synchronous IO, which fails under asyncio.
                previous_status = (
                    existing.status
                    if existing is not None and existing.tenant_id == tenant_id
                    else None
                )
                await unit_of_work.rollback()
                return DirectActionClaim(action_id, False, previous_status)

            await pending_repository.create(
                action_id=action_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action_type=action_type,
                resource_type=resource_type,
                payload=payload,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                status="confirmed",
            )

            await unit_of_work.commit()

        return DirectActionClaim(action_id, True)

    async def confirm_and_claim_action(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        request_fingerprint: str,
    ) -> ConfirmedAction | None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(
                unit_of_work.session
            )
            idempotency_repository = IdempotencyRepository(
                unit_of_work.session
            )

            action = await pending_repository.confirm(
                action_id=action_id,
                tenant_id=tenant_id,
                actor_id=actor_id,

            )

            if action is None:
                await unit_of_work.rollback()
                return None

            claimed = await idempotency_repository.claim(
                key=action_id,
                tenant_id=tenant_id,
                action_type=action.action_type,
                request_fingerprint=request_fingerprint,
            )

            if not claimed:
                await unit_of_work.rollback()
                return None

            confirmed_action = ConfirmedAction(
                id=action.id,
                tenant_id=action.tenant_id,
                actor_id=action.actor_id,
                action_type=action.action_type,
                resource_type=action.resource_type,
                payload=dict(action.payload),
            )

            await unit_of_work.commit()

            return confirmed_action

    async def confirm_and_claim(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        request_fingerprint: str,
    ) -> bool:
        action = await self.confirm_and_claim_action(
            action_id=action_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            request_fingerprint=request_fingerprint,
        )

        return action is not None

    async def complete_action(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        request_id: str,
        resource_type: str,
        resource_id: str | None,
        result: dict[str, Any],
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(unit_of_work.session)
            idempotency_repository = IdempotencyRepository(unit_of_work.session)
            audit_repository = AuditLogRepository(unit_of_work.session)

            updated = await pending_repository.set_status(
                action_id=action_id,
                tenant_id=tenant_id,
                status="completed",
            )
            if not updated:
                raise ValueError("Pending action was not found")

            await idempotency_repository.complete(
                key=action_id,
                status="succeeded",
                result=result,
            )

            await audit_repository.create(
                audit_id=uuid4().hex,
                tenant_id=tenant_id,
                actor_id=actor_id,
                request_id=request_id,
                action_type="crm_mutation",
                resource_type=resource_type,
                resource_id=resource_id,
                outcome="success",
                event_data={
                    "action_id": action_id,
                    "result": result,
                },
            )

            await unit_of_work.commit()

    async def fail_action(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        request_id: str,
        resource_type: str,
        error_code: str,
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(unit_of_work.session)
            idempotency_repository = IdempotencyRepository(unit_of_work.session)
            audit_repository = AuditLogRepository(unit_of_work.session)

            await pending_repository.set_status(
                action_id=action_id,
                tenant_id=tenant_id,
                status="failed",
            )

            await idempotency_repository.complete(
                key=action_id,
                status="failed",
                result={"error_code": error_code},
            )

            await audit_repository.create(
                audit_id=uuid4().hex,
                tenant_id=tenant_id,
                actor_id=actor_id,
                request_id=request_id,
                action_type="crm_mutation",
                resource_type=resource_type,
                resource_id=None,
                outcome="failure",
                event_data={
                    "action_id": action_id,
                    "error_code": error_code,
                },
            )

            await unit_of_work.commit()
    async def record_partial_action(
        self,
        *,
        action_id: str,
        tenant_id: str,
        actor_id: str,
        request_id: str,
        resource_type: str,
        result: dict[str, Any],
    ) -> None:
        """Record a multi-step action where some CRM writes succeeded before a failure."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(unit_of_work.session)
            idempotency_repository = IdempotencyRepository(unit_of_work.session)
            audit_repository = AuditLogRepository(unit_of_work.session)

            await pending_repository.set_status(
                action_id=action_id,
                tenant_id=tenant_id,
                status="reconciliation_required",
            )

            await idempotency_repository.complete(
                key=action_id,
                status="partial",
                result=result,
            )

            await audit_repository.create(
                audit_id=uuid4().hex,
                tenant_id=tenant_id,
                actor_id=actor_id,
                request_id=request_id,
                action_type="crm_mutation",
                resource_type=resource_type,
                resource_id=None,
                outcome="partial",
                event_data={
                    "action_id": action_id,
                    "result": result,
                },
            )

            await unit_of_work.commit()

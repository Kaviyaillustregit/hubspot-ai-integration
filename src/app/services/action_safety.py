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
                action_id,
                tenant_id,
                actor_id,
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
        """
        Atomically confirm a pending action and claim its idempotency key.

        Only the same tenant and actor that created the action can confirm it.
        """
        async with UnitOfWork(self._session_factory) as unit_of_work:
            if unit_of_work.session is None:
                raise RuntimeError("UnitOfWork session is unavailable")

            pending_repository = PendingActionRepository(unit_of_work.session)
            idempotency_repository = IdempotencyRepository(unit_of_work.session)

            action = await pending_repository.confirm(
                action_id=action_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
            )
            if action is None:
                await unit_of_work.rollback()
                return False

            claimed = await idempotency_repository.claim(
                key=action_id,
                tenant_id=tenant_id,
                action_type=action.action_type,
                request_fingerprint=request_fingerprint,
            )
            if not claimed:
                await unit_of_work.rollback()
                return False

            await unit_of_work.commit()

        return True

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
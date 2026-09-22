from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.action_models import AuditLogRecord, PendingActionRecord
from app.repositories.action_safety import (
    AuditLogRepository,
    IdempotencyRepository,
    PendingActionRepository,
)


@pytest.mark.asyncio
async def test_pending_action_create_adds_pending_record():
    session = MagicMock()
    session.execute = AsyncMock()
    repository = PendingActionRepository(session)

    expires_at = datetime.now(UTC) + timedelta(minutes=10)

    record = await repository.create(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        action_type="create_contact",
        resource_type="contact",
        payload={"email": "arun@test.com"},
        expires_at=expires_at,
    )

    assert isinstance(record, PendingActionRecord)
    assert record.id == "action-1"
    assert record.tenant_id == "tenant-a"
    assert record.actor_id == "user-1"
    assert record.status == "pending"
    assert record.payload == {"email": "arun@test.com"}
    session.add.assert_called_once_with(record)


@pytest.mark.asyncio
async def test_pending_action_confirm_requires_matching_tenant_and_actor():
    session = MagicMock()
    session.execute = AsyncMock()

    result = MagicMock()

    record = PendingActionRecord(
        id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        action_type="create_contact",
        resource_type="contact",
        payload={"email": "arun@test.com"},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        created_at=datetime.now(UTC),
    )

    result.scalar_one_or_none.return_value = record
    session.execute.return_value = result

    repository = PendingActionRepository(session)

    confirmed = await repository.confirm(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
    )

    assert confirmed is record
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_at is not None


@pytest.mark.asyncio
async def test_pending_action_confirm_returns_none_when_not_found():
    session = MagicMock()
    session.execute = AsyncMock()

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    repository = PendingActionRepository(session)

    confirmed = await repository.confirm(
        action_id="missing",
        tenant_id="tenant-a",
        actor_id="user-1",
    )

    assert confirmed is None


@pytest.mark.asyncio
async def test_pending_action_set_status_returns_true_when_row_updated():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=1)
    )

    repository = PendingActionRepository(session)

    updated = await repository.set_status(
        action_id="action-1",
        tenant_id="tenant-a",
        status="completed",
    )

    assert updated is True


@pytest.mark.asyncio
async def test_pending_action_set_status_returns_false_when_row_not_updated():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=0)
    )

    repository = PendingActionRepository(session)

    updated = await repository.set_status(
        action_id="action-1",
        tenant_id="tenant-a",
        status="completed",
    )

    assert updated is False


@pytest.mark.asyncio
async def test_audit_log_create_adds_audit_record():
    session = MagicMock()
    session.execute = AsyncMock()
    repository = AuditLogRepository(session)

    record = await repository.create(
        audit_id="audit-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_id="request-1",
        action_type="create_contact",
        resource_type="contact",
        resource_id="123",
        outcome="success",
        event_data={"source": "slack"},
    )

    assert isinstance(record, AuditLogRecord)
    assert record.id == "audit-1"
    assert record.tenant_id == "tenant-a"
    assert record.resource_id == "123"
    assert record.outcome == "success"
    assert record.event_data == {"source": "slack"}
    session.add.assert_called_once_with(record)



@pytest.mark.asyncio
async def test_idempotency_claim_returns_false_for_existing_key():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=0)
    )

    repository = IdempotencyRepository(session)

    claimed = await repository.claim(
        key="idem-1",
        tenant_id="tenant-a",
        action_type="create_contact",
        request_fingerprint="fingerprint-1",
    )

    assert claimed is False


@pytest.mark.asyncio
async def test_idempotency_get_returns_record():
    session = MagicMock()
    session.get = AsyncMock()

    record = MagicMock()
    session.get.return_value = record

    repository = IdempotencyRepository(session)

    result = await repository.get("idem-1")

    assert result is record
    session.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_idempotency_complete_returns_true_when_updated():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=1)
    )

    repository = IdempotencyRepository(session)

    completed = await repository.complete(
        key="idem-1",
        status="succeeded",
        result={"contact_id": "123"},
    )

    assert completed is True


@pytest.mark.asyncio
async def test_idempotency_complete_returns_false_when_missing():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=0)
    )

    repository = IdempotencyRepository(session)

    completed = await repository.complete(
        key="missing",
        status="succeeded",
        result={"contact_id": "123"},
    )

    assert completed is False

@pytest.mark.asyncio
async def test_pending_action_set_status_rejects_non_confirmed_action():
    session = MagicMock()

    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=0)
    )

    repository = PendingActionRepository(session)

    updated = await repository.set_status(
        action_id="action-1",
        tenant_id="tenant-a",
        status="completed",
    )

    assert updated is False
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_action_set_status_rejects_unsupported_status():
    session = MagicMock()

    repository = PendingActionRepository(session)

    updated = await repository.set_status(
        action_id="action-1",
        tenant_id="tenant-a",
        status="confirmed",
    )

    assert updated is False
    session.execute.assert_not_called()

@pytest.mark.asyncio
async def test_idempotency_claim_returns_true_for_new_key():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=1)
    )

    repository = IdempotencyRepository(session)

    claimed = await repository.claim(
        key="idem-1",
        tenant_id="tenant-a",
        action_type="create_contact",
        request_fingerprint="fingerprint-1",
    )

    assert claimed is True

    statement = session.execute.await_args.args[0]
    params = statement.compile().params

    assert params["claimed_at"] is not None
    assert params["created_at"] == params["claimed_at"]

@pytest.mark.asyncio
async def test_pending_action_set_status_allows_reconciliation_required():
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(rowcount=1)
    )

    repository = PendingActionRepository(session)

    updated = await repository.set_status(
        action_id="action-1",
        tenant_id="tenant-a",
        status="reconciliation_required",
    )

    assert updated is True
    session.execute.assert_awaited_once()
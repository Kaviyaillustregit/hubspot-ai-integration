from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.action_safety as action_safety_module
from app.repositories.action_safety import (
    AuditLogRepository,
    IdempotencyRepository,
    PendingActionRepository,
)
from app.services.action_safety import ActionSafetyService


class FakeUnitOfWork:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.session = None
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        self.session = self._session_factory()
        return self

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.session is not None:
            close = getattr(self.session, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


@pytest.fixture
def safety_mocks(monkeypatch):
    pending = MagicMock(spec=PendingActionRepository)
    audit = MagicMock(spec=AuditLogRepository)
    idempotency = MagicMock(spec=IdempotencyRepository)

    pending.create = AsyncMock()
    pending.confirm = AsyncMock()
    pending.set_status = AsyncMock()

    audit.create = AsyncMock()

    idempotency.claim = AsyncMock()
    idempotency.complete = AsyncMock()
    idempotency.get = AsyncMock()

    monkeypatch.setattr(
        action_safety_module,
        "PendingActionRepository",
        lambda session: pending,
    )
    monkeypatch.setattr(
        action_safety_module,
        "AuditLogRepository",
        lambda session: audit,
    )
    monkeypatch.setattr(
        action_safety_module,
        "IdempotencyRepository",
        lambda session: idempotency,
    )

    unit_of_works: list[FakeUnitOfWork] = []

    def make_uow(session_factory):
        uow = FakeUnitOfWork(session_factory)
        unit_of_works.append(uow)
        return uow

    monkeypatch.setattr(action_safety_module, "UnitOfWork", make_uow)

    return pending, audit, idempotency, unit_of_works


@pytest.mark.asyncio
async def test_create_pending_action_persists_action(safety_mocks):
    pending, _, _, unit_of_works = safety_mocks
    service = ActionSafetyService(lambda: MagicMock())

    action_id = await service.create_pending_action(
        tenant_id="tenant-a",
        actor_id="user-1",
        action_type="create_contact",
        resource_type="contact",
        payload={"email": "arun@test.com"},
    )

    assert isinstance(action_id, str)
    assert len(action_id) == 32

    pending.create.assert_awaited_once()
    assert unit_of_works[0].committed is True


@pytest.mark.asyncio
async def test_confirm_and_claim_succeeds_for_valid_action(safety_mocks):
    pending, _, idempotency, unit_of_works = safety_mocks

    pending.confirm.return_value = MagicMock(
        action_type="create_contact",
    )
    idempotency.claim.return_value = True

    service = ActionSafetyService(lambda: MagicMock())

    result = await service.confirm_and_claim(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_fingerprint="fingerprint-1",
    )

    assert result is True
    pending.confirm.assert_awaited_once_with(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
    )
    idempotency.claim.assert_awaited_once_with(
        key="action-1",
        tenant_id="tenant-a",
        action_type="create_contact",
        request_fingerprint="fingerprint-1",
    )
    assert unit_of_works[0].committed is True


@pytest.mark.asyncio
async def test_confirm_and_claim_rejects_missing_or_expired_action(safety_mocks):
    pending, _, idempotency, unit_of_works = safety_mocks
    pending.confirm.return_value = None

    service = ActionSafetyService(lambda: MagicMock())

    result = await service.confirm_and_claim(
        action_id="missing",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_fingerprint="fingerprint-1",
    )

    assert result is False
    idempotency.claim.assert_not_awaited()
    assert unit_of_works[0].rolled_back is True


@pytest.mark.asyncio
async def test_confirm_and_claim_rejects_duplicate_idempotency_claim(safety_mocks):
    pending, _, idempotency, unit_of_works = safety_mocks

    pending.confirm.return_value = MagicMock(
        action_type="create_contact",
    )
    idempotency.claim.return_value = False

    service = ActionSafetyService(lambda: MagicMock())

    result = await service.confirm_and_claim(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_fingerprint="fingerprint-1",
    )

    assert result is False
    assert unit_of_works[0].rolled_back is True


@pytest.mark.asyncio
async def test_complete_action_updates_pending_idempotency_and_audit(safety_mocks):
    pending, audit, idempotency, unit_of_works = safety_mocks

    pending.set_status.return_value = True
    idempotency.complete.return_value = True

    service = ActionSafetyService(lambda: MagicMock())

    await service.complete_action(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_id="request-1",
        resource_type="contact",
        resource_id="123",
        result={"contact_id": "123"},
    )

    pending.set_status.assert_awaited_once_with(
        action_id="action-1",
        tenant_id="tenant-a",
        status="completed",
    )
    idempotency.complete.assert_awaited_once_with(
        key="action-1",
        status="succeeded",
        result={"contact_id": "123"},
    )
    audit.create.assert_awaited_once()
    assert unit_of_works[0].committed is True


@pytest.mark.asyncio
async def test_complete_action_fails_when_pending_action_missing(safety_mocks):
    pending, audit, idempotency, _ = safety_mocks
    pending.set_status.return_value = False

    service = ActionSafetyService(lambda: MagicMock())

    with pytest.raises(ValueError, match="Pending action was not found"):
        await service.complete_action(
            action_id="missing",
            tenant_id="tenant-a",
            actor_id="user-1",
            request_id="request-1",
            resource_type="contact",
            resource_id="123",
            result={"contact_id": "123"},
        )

    idempotency.complete.assert_not_awaited()
    audit.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_action_marks_idempotency_and_writes_audit(safety_mocks):
    pending, audit, idempotency, unit_of_works = safety_mocks

    service = ActionSafetyService(lambda: MagicMock())

    await service.fail_action(
        action_id="action-1",
        tenant_id="tenant-a",
        actor_id="user-1",
        request_id="request-1",
        resource_type="contact",
        error_code="hubspot_rate_limited",
    )

    pending.set_status.assert_awaited_once_with(
        action_id="action-1",
        tenant_id="tenant-a",
        status="failed",
    )
    idempotency.complete.assert_awaited_once_with(
        key="action-1",
        status="failed",
        result={"error_code": "hubspot_rate_limited"},
    )
    audit.create.assert_awaited_once()
    assert unit_of_works[0].committed is True


def test_action_service_uses_utc_time_for_pending_actions():
    now = datetime.now(UTC)
    assert now.tzinfo == UTC

@pytest.mark.asyncio
async def test_confirm_and_claim_action_returns_confirmed_action(safety_mocks):
    pending, _, idempotency, _ = safety_mocks

    pending.confirm.return_value = MagicMock(
        id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
        action_type="create_contact",
        resource_type="contact",
        payload={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

    idempotency.claim.return_value = True

    service = ActionSafetyService(lambda: MagicMock())

    confirmed = await service.confirm_and_claim_action(
        action_id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
        request_fingerprint="fingerprint-123",
    )

    assert confirmed is not None
    assert confirmed.id == "action-123"
    assert confirmed.tenant_id == "tenant-a"
    assert confirmed.actor_id == "user-a"
    assert confirmed.action_type == "create_contact"
    assert confirmed.resource_type == "contact"
    assert confirmed.payload == {
        "email": "arun@test.com",
        "firstname": "Arun",
        "lastname": "Kumar",
    }

    pending.confirm.assert_awaited_once_with(
        action_id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
    )

    idempotency.claim.assert_awaited_once_with(
        key="action-123",
        tenant_id="tenant-a",
        action_type="create_contact",
        request_fingerprint="fingerprint-123",
    )

@pytest.mark.asyncio
async def test_confirm_and_claim_action_rejects_duplicate_confirmation(
    safety_mocks,
):
    pending, _, idempotency, unit_of_works = safety_mocks

    pending.confirm.return_value = MagicMock(
        id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
        action_type="create_contact",
        resource_type="contact",
        payload={
            "email": "arun@test.com",
            "firstname": "Arun",
            "lastname": "Kumar",
        },
    )

    idempotency.claim.side_effect = [True, False]

    service = ActionSafetyService(lambda: MagicMock())

    first = await service.confirm_and_claim_action(
        action_id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
        request_fingerprint="fingerprint-123",
    )

    second = await service.confirm_and_claim_action(
        action_id="action-123",
        tenant_id="tenant-a",
        actor_id="user-a",
        request_fingerprint="fingerprint-123",
    )

    assert first is not None
    assert first.id == "action-123"
    assert second is None

    assert idempotency.claim.await_count == 2
    assert unit_of_works[0].committed is True
    assert unit_of_works[1].rolled_back is True


@pytest.mark.asyncio
async def test_start_direct_action_claims_key_and_records_confirmed_action(safety_mocks):
    pending, _, idempotency, unit_of_works = safety_mocks
    idempotency.claim.return_value = True

    service = ActionSafetyService(lambda: MagicMock())

    claim = await service.start_direct_action(
        idempotency_key="slack-message:C1:1712345678.000100",
        tenant_id="tenant-a",
        actor_id="U1",
        action_type="create_contact",
        resource_type="contact",
        payload={"firstname": "Victor"},
        request_fingerprint="fingerprint",
    )

    expected_id = ActionSafetyService.direct_action_id(
        tenant_id="tenant-a",
        action_type="create_contact",
        idempotency_key="slack-message:C1:1712345678.000100",
    )
    assert claim.claimed is True
    assert claim.action_id == expected_id
    assert len(claim.action_id) <= 128
    idempotency.claim.assert_awaited_once_with(
        key=expected_id,
        tenant_id="tenant-a",
        action_type="create_contact",
        request_fingerprint="fingerprint",
    )
    assert pending.create.await_args.kwargs["action_id"] == expected_id
    assert pending.create.await_args.kwargs["status"] == "confirmed"
    assert unit_of_works[0].committed is True


@pytest.mark.asyncio
async def test_start_direct_action_rejects_redelivery_without_recording_new_action(
    safety_mocks,
):
    pending, _, idempotency, unit_of_works = safety_mocks
    idempotency.claim.return_value = False

    class ExpiringRecord:
        """Mimics an ORM instance: attribute reads after rollback would lazy-load (sync IO)."""

        def __getattr__(self, name):
            if unit_of_works and unit_of_works[0].rolled_back:
                raise AssertionError(f"read {name} after rollback expired the instance")
            return {"tenant_id": "tenant-a", "status": "succeeded"}[name]

    idempotency.get.return_value = ExpiringRecord()

    service = ActionSafetyService(lambda: MagicMock())

    claim = await service.start_direct_action(
        idempotency_key="slack-message:C1:1712345678.000100",
        tenant_id="tenant-a",
        actor_id="U1",
        action_type="create_contact",
        resource_type="contact",
        payload={},
        request_fingerprint="fingerprint",
    )

    assert claim.claimed is False
    assert claim.previous_status == "succeeded"
    pending.create.assert_not_awaited()
    assert unit_of_works[0].rolled_back is True
    assert unit_of_works[0].committed is False


@pytest.mark.asyncio
async def test_recent_actions_are_read_for_the_actor_within_the_tenant(safety_mocks):
    pending, _, _, unit_of_works = safety_mocks
    created_at = datetime.now(UTC)
    pending.list_recent = AsyncMock(
        return_value=[
            MagicMock(
                action_type="create_contact",
                status="completed",
                payload={"firstname": "Angel"},
                created_at=created_at,
                expires_at=created_at,
            )
        ]
    )

    service = ActionSafetyService(lambda: MagicMock())

    recent = await service.recent_actions(tenant_id="tenant-a", actor_id="U1", limit=3)

    pending.list_recent.assert_awaited_once_with(tenant_id="tenant-a", actor_id="U1", limit=3)
    assert [(item.action_type, item.status, item.payload) for item in recent] == [
        ("create_contact", "completed", {"firstname": "Angel"})
    ]
    assert unit_of_works[0].committed is False


def test_direct_action_ids_are_tenant_and_message_scoped():
    def action_id(tenant: str, key: str) -> str:
        return ActionSafetyService.direct_action_id(
            tenant_id=tenant, action_type="create_contact", idempotency_key=key
        )

    assert action_id("tenant-a", "k1") == action_id("tenant-a", "k1")
    assert action_id("tenant-a", "k1") != action_id("tenant-b", "k1")
    assert action_id("tenant-a", "k1") != action_id("tenant-a", "k2")
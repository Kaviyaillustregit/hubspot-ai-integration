import inspect
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.errors import AppError
from app.core.config import DEFAULT_DATABASE_URL, Settings
from app.db.session import UnitOfWork
from app.integrations.hubspot.client import HubSpotClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.webhooks.contracts import EventIdempotencyKey, InboundWebhook
from app.services.mutations import (
    ApprovalRequirement,
    AuthorizationDecision,
    MutationCoordinator,
    MutationRecommendation,
    MutationResult,
)


def test_production_settings_reject_local_database_default():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(app_env="production", database_url=DEFAULT_DATABASE_URL)


def test_invalid_request_id_is_replaced():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad value"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad value"
    assert len(response.headers["X-Request-ID"]) == 36


def test_application_errors_have_safe_shape_and_request_id():
    application = create_app(Settings())

    async def fail() -> None:
        raise AppError("demo_error", "A safe message", status_code=409)

    application.add_api_route("/test-error", fail)
    with TestClient(application) as client:
        response = client.get("/test-error", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 409
    assert response.json() == {
        "code": "demo_error",
        "message": "A safe message",
        "request_id": "request-123",
    }


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_and_closes_on_error():
    session = FakeSession()

    with pytest.raises(RuntimeError):
        async with UnitOfWork(lambda: session):
            raise RuntimeError("service failure")

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closed is True


def test_tenant_contexts_do_not_share_identity_or_credentials():
    first = TenantContext("tenant-a", "account-a", "credential-a")
    second = TenantContext("tenant-b", "account-b", "credential-b")

    assert first != second
    assert first.credential_reference != second.credential_reference


@pytest.mark.asyncio
async def test_mutation_requires_authorization_and_human_approval():
    recommendation = MutationRecommendation("create_task", {"subject": "Follow up"}, "stale deal")
    executed = False
    audited = False

    class Authorizer:
        async def authorize(self, recommendation, actor):
            return AuthorizationDecision(True, "allowed")

    class Policy:
        def requirement_for(self, recommendation):
            return ApprovalRequirement.REQUIRED

    class Executor:
        async def execute(self, recommendation):
            nonlocal executed
            executed = True
            return MutationResult("create_task", "executed", "audit-1")

    class Audit:
        async def record(self, recommendation, result):
            nonlocal audited
            audited = True

    coordinator = MutationCoordinator(Authorizer(), Policy(), Executor(), Audit())

    with pytest.raises(PermissionError, match="approval"):
        await coordinator.execute(recommendation, actor="user-1")

    assert executed is False
    assert audited is False
    result = await coordinator.execute(
        recommendation, actor="user-1", approval_granted=True
    )
    assert result.audit_reference == "audit-1"
    assert executed is True
    assert audited is True


def test_webhook_verification_is_not_an_outbound_hubspot_client_method():
    assert "verify_webhook_signature" not in dir(HubSpotClient)
    event = InboundWebhook(
        provider="hubspot",
        event_id="event-1",
        received_at=datetime.now(UTC),
        payload={"objectId": "123"},
    )

    assert EventIdempotencyKey(event_id=event.event_id).event_id == "event-1"
    assert inspect.isabstract(HubSpotClient)
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.cli import slack_authorization
from app.core.config import Settings
from app.db.authorized_users import SlackAuthorizedUserRecord
from app.services.slack_authorization_provisioning import (
    ProvisioningResult,
    SlackAuthorizationProvisioner,
)


class FakeTransaction:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self):
        self.session.snapshot = deepcopy(self.session.records)
        self.session.transaction_count += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.session.records = self.session.snapshot
            self.session.rollback_count += 1
        else:
            for key, record in self.session.pending.items():
                self.session.records[key] = record
            self.session.pending.clear()
            self.session.commit_count += 1


class FakeSession:
    def __init__(self, records=None) -> None:
        self.records = records or {}
        self.pending = {}
        self.snapshot = {}
        self.transaction_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def begin(self):
        return FakeTransaction(self)

    async def get(self, model, key):
        self.get_calls.append((model, key))
        return self.pending.get(key) or self.records.get(key)

    def add(self, record):
        self.pending[(record.tenant_id, record.slack_team_id, record.slack_user_id)] = record


def make_record(tenant="tenant-a", workspace="T1", user="U1", *, role="sales_agent", active=True):
    return SlackAuthorizedUserRecord(
        tenant_id=tenant,
        slack_team_id=workspace,
        slack_user_id=user,
        role=role,
        active=active,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def session_factory():
    sessions = []
    queued = []

    def factory():
        session = queued.pop(0) if queued else FakeSession()
        sessions.append(session)
        return session

    factory.sessions = sessions
    factory.queue = queued
    return factory


@pytest.mark.asyncio
async def test_grant_creates_sales_agent_in_explicit_transaction(session_factory):
    service = SlackAuthorizationProvisioner(session_factory)

    result = await service.grant_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
    )

    session = session_factory.sessions[0]
    row = session.records[("tenant-a", "T1", "U1")]
    assert result is ProvisioningResult.GRANTED
    assert (row.tenant_id, row.slack_team_id, row.slack_user_id) == ("tenant-a", "T1", "U1")
    assert row.role == "sales_agent" and row.active is True
    assert session.transaction_count == session.commit_count == 1
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_grant_is_idempotent_for_active_sales_agent(session_factory):
    session = FakeSession({("tenant-a", "T1", "U1"): make_record()})
    session_factory.queue.append(session)

    result = await SlackAuthorizationProvisioner(session_factory).grant_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
    )

    assert result is ProvisioningResult.ALREADY_ACTIVE
    assert session.records[("tenant-a", "T1", "U1")].active is True
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_explicit_grant_reactivates_revoked_sales_agent(session_factory):
    session = FakeSession({("tenant-a", "T1", "U1"): make_record(active=False)})
    session_factory.queue.append(session)

    result = await SlackAuthorizationProvisioner(session_factory).grant_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
    )

    assert result is ProvisioningResult.GRANTED
    assert session.records[("tenant-a", "T1", "U1")].active is True
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_grant_does_not_create_or_reactivate_administrator(session_factory):
    session = FakeSession({("tenant-a", "T1", "U1"): make_record(role="admin", active=False)})
    session_factory.queue.append(session)

    with pytest.raises(ValueError, match="sales_agent"):
        await SlackAuthorizationProvisioner(session_factory).grant_access(
            tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
        )

    assert session.records[("tenant-a", "T1", "U1")].active is False
    assert session.commit_count == 0 and session.rollback_count == 1


@pytest.mark.asyncio
async def test_revoke_deactivates_only_exact_tenant_workspace_user(session_factory):
    rows = {
        ("tenant-a", "T1", "U1"): make_record(),
        ("tenant-a", "T2", "U1"): make_record(workspace="T2"),
        ("tenant-b", "T1", "U1"): make_record(tenant="tenant-b"),
    }
    session = FakeSession(rows)
    session_factory.queue.append(session)

    result = await SlackAuthorizationProvisioner(session_factory).revoke_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
    )

    assert result is ProvisioningResult.REVOKED
    assert session.records[("tenant-a", "T1", "U1")].active is False
    assert session.records[("tenant-a", "T2", "U1")].active is True
    assert session.records[("tenant-b", "T1", "U1")].active is True
    assert session.get_calls == [(SlackAuthorizedUserRecord, ("tenant-a", "T1", "U1"))]


@pytest.mark.asyncio
async def test_revoke_does_not_insert_unknown_user_or_reactivate(session_factory):
    service = SlackAuthorizationProvisioner(session_factory)
    result = await service.revoke_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U404"
    )
    assert result is ProvisioningResult.NOT_FOUND
    session = session_factory.sessions[0]
    assert session.records == {} and session.pending == {}
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_revoke_already_inactive_is_noop(session_factory):
    session = FakeSession({("tenant-a", "T1", "U1"): make_record(active=False)})
    session_factory.queue.append(session)

    result = await SlackAuthorizationProvisioner(session_factory).revoke_access(
        tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
    )
    assert result is ProvisioningResult.ALREADY_INACTIVE
    assert session.records[("tenant-a", "T1", "U1")].active is False


@pytest.mark.asyncio
async def test_database_failure_propagates_and_rolls_back(session_factory):
    class BrokenSession(FakeSession):
        async def get(self, model, key):
            raise RuntimeError("database unavailable")

    session = BrokenSession()
    session_factory.queue.append(session)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await SlackAuthorizationProvisioner(session_factory).grant_access(
            tenant_id="tenant-a", slack_team_id="T1", slack_user_id="U1"
        )
    assert session.rollback_count == 1 and session.commit_count == 0


@pytest.mark.parametrize(
    "identity",
    [
        ("", "T1", "U1"),
        ("tenant-a", " ", "U1"),
        ("tenant-a", "T1", " U1"),
        ("t" * 129, "T1", "U1"),
        ("tenant-a", "T" * 65, "U1"),
        ("tenant-a", "T1", "U" * 65),
    ],
)
@pytest.mark.asyncio
async def test_invalid_identity_fails_before_database_access(session_factory, identity):
    with pytest.raises(ValueError):
        await SlackAuthorizationProvisioner(session_factory).grant_access(
            tenant_id=identity[0], slack_team_id=identity[1], slack_user_id=identity[2]
        )
    assert session_factory.sessions == []


def test_cli_rejects_workspace_tenant_mismatch_before_database(monkeypatch):
    def fail_if_database_opens(_settings):
        pytest.fail("DB opened")

    monkeypatch.setattr(slack_authorization, "create_database", fail_if_database_opens)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://safe:secret@localhost:5432/disposable",
        slack_team_tenant_map='{"T1":"tenant-a"}',
    )
    with pytest.raises(ValueError, match="not mapped"):
        slack_authorization._validated_args(
            ["grant", "--tenant-id", "tenant-b", "--workspace-id", "T1", "--user-id", "U1"],
            settings,
        )


def test_cli_rejects_ambiguous_workspace_mapping_before_database(monkeypatch):
    def fail_if_database_opens(_settings):
        pytest.fail("DB opened")

    monkeypatch.setattr(slack_authorization, "create_database", fail_if_database_opens)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://safe:secret@localhost:5432/disposable",
        slack_team_tenant_map='{"T1":"tenant-a","T1":"tenant-b"}',
    )
    with pytest.raises(ValueError, match="Invalid identity or server-owned workspace mapping"):
        slack_authorization._validated_args(
            ["grant", "--tenant-id", "tenant-b", "--workspace-id", "T1", "--user-id", "U1"],
            settings,
        )


def test_cli_database_target_never_includes_password():
    rendered = slack_authorization._safe_database_target(
        "postgresql+asyncpg://operator:do-not-print@127.0.0.1:54321/hubspot_ai_history_test"
    )
    assert rendered == "operator@127.0.0.1:54321/hubspot_ai_history_test"
    assert "do-not-print" not in rendered

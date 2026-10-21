"""Fail-closed guards for isolated conversation-history database tests."""

import os

from sqlalchemy.engine import make_url

HISTORY_TEST_DATABASE = "hubspot_ai_history_test"
HISTORY_TEST_USER = "hubspot_history_test"
PRODUCTION_MIGRATION_DATABASE = "hubspot_ai"
PRODUCTION_MIGRATION_USER = "app"
PRODUCTION_HISTORY_MIGRATION_REVISION = "7a91c4d2e650"


def validate_history_test_database(database_url: str) -> None:
    """Accept only the dedicated local history test database and role."""
    try:
        url = make_url(database_url)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("A valid isolated history test DATABASE_URL is required") from exc

    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.database != HISTORY_TEST_DATABASE
        or url.username != HISTORY_TEST_USER
        or not url.password
    ):
        raise RuntimeError(
            "History persistence tests require the dedicated local "
            f"{HISTORY_TEST_DATABASE} database and {HISTORY_TEST_USER} role"
        )


def reject_shared_migration_database(database_url: str) -> None:
    """Never allow Alembic to target the known shared application database."""
    try:
        url = make_url(database_url)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("A valid DATABASE_URL is required for migrations") from exc
    if url.database == PRODUCTION_MIGRATION_DATABASE:
        raise RuntimeError("Refusing to run migrations against shared database hubspot_ai")


def validate_production_history_migration_target(
    database_url: str, *, authorized: bool
) -> None:
    """Allow an explicitly authorized migration only to the expected Compose DB identity."""
    if not authorized:
        raise RuntimeError("Production history migration requires explicit authorization")
    try:
        url = make_url(database_url)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("A valid production migration DATABASE_URL is required") from exc

    if (
        url.get_backend_name() != "postgresql"
        or url.host != "postgres"
        or url.port != 5432
        or url.database != PRODUCTION_MIGRATION_DATABASE
        or url.username != PRODUCTION_MIGRATION_USER
        or not url.password
    ):
        raise RuntimeError(
            "Production history migrations require the exact postgres:5432 "
            "hubspot_ai database and app role"
        )


def validate_production_migration_revision(
    requested_revision: str | None, authorized_revision: str | None
) -> None:
    """Limit an authorized production Alembic invocation to the additive history revision."""
    if (
        requested_revision != PRODUCTION_HISTORY_MIGRATION_REVISION
        or authorized_revision != PRODUCTION_HISTORY_MIGRATION_REVISION
    ):
        raise RuntimeError(
            "Production migration authorization is limited to revision "
            f"{PRODUCTION_HISTORY_MIGRATION_REVISION}"
        )


def migration_database_url(default_database_url: str) -> str:
    """Resolve migration target, enforcing strict identity in the test workflow."""
    test_mode = os.environ.get("HISTORY_TEST_MODE") == "1"
    production_mode = os.environ.get("HUBSPOT_PRODUCTION_MIGRATION_MODE") == "1"
    if test_mode and production_mode:
        raise RuntimeError("Test and production migration modes cannot be enabled together")
    if test_mode:
        test_url = os.environ.get("HISTORY_TEST_DATABASE_URL", "")
        validate_history_test_database(test_url)
        return test_url
    if production_mode:
        validate_production_history_migration_target(default_database_url, authorized=True)
        return default_database_url
    reject_shared_migration_database(default_database_url)
    return default_database_url

"""Create the disposable local PostgreSQL database used by history tests."""

import asyncio
import os
from urllib.parse import quote, urlsplit

import asyncpg

DATABASE = "hubspot_ai_history_test"
ROLE = "hubspot_history_test"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def provision() -> None:
    admin_url = os.environ.get("HISTORY_TEST_ADMIN_DATABASE_URL", "")
    password = os.environ.get("HISTORY_TEST_USER_PASSWORD", "")
    parsed = urlsplit(admin_url)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.path.lstrip("/") != "postgres"
        or not parsed.username
        or not password
    ):
        raise RuntimeError(
            "Set HISTORY_TEST_ADMIN_DATABASE_URL to the local postgres maintenance database "
            "and HISTORY_TEST_USER_PASSWORD; shared hubspot_ai is never a valid admin target."
        )

    admin = await asyncpg.connect(admin_url)
    try:
        db_owner = await admin.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1", DATABASE
        )
        if db_owner is not None and db_owner != ROLE:
            raise RuntimeError(f"Refusing to reuse {DATABASE}; owner is not {ROLE}")
        role_exists = await admin.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE
        )
        if db_owner is None:
            if role_exists:
                await admin.execute(
                    f"ALTER ROLE {ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
                    f"NOCREATEROLE NOREPLICATION PASSWORD {_quote_literal(password)}"
                )
            else:
                await admin.execute(
                    f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    f"NOREPLICATION PASSWORD {_quote_literal(password)}"
                )
            await admin.execute(f"CREATE DATABASE {DATABASE} OWNER {ROLE}")
        else:
            await admin.execute(
                f"ALTER ROLE {ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOREPLICATION PASSWORD {_quote_literal(password)}"
            )
    finally:
        await admin.close()

    test_url = (
        f"postgresql+asyncpg://{ROLE}:{quote(password, safe='')}@{parsed.hostname}:"
        f"{parsed.port or 5432}/{DATABASE}"
    )
    connection = await asyncpg.connect(
        test_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        identity = await connection.fetchrow("SELECT current_database(), current_user")
        if identity[0] != DATABASE or identity[1] != ROLE:
            raise RuntimeError(
                "Created history test database identity did not match expected target"
            )
    finally:
        await connection.close()
    print(f"Created/verified isolated database {DATABASE} owned by dedicated role {ROLE}.")
    print("Set HISTORY_TEST_DATABASE_URL for migrations and tests; credentials were not printed.")


if __name__ == "__main__":
    asyncio.run(provision())

"""Fail-closed preflight and narrowly-scoped production history migration."""

import argparse
import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.history_safety import validate_production_history_migration_target

EXPECTED_DATABASE = "hubspot_ai"
EXPECTED_USER = "app"
EXPECTED_REVISION = "6aa7c8eb17c4"
TARGET_REVISION = "7a91c4d2e650"


async def verify_database_target(database_url: str) -> None:
    validate_production_history_migration_target(
        database_url,
        authorized=os.environ.get("HUBSPOT_PRODUCTION_MIGRATION_MODE") == "1",
    )
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text("SELECT current_database(), current_user")
                )
            ).one()
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            auth_table = await connection.scalar(
                text("SELECT to_regclass('public.slack_authorized_users')")
            )
            history_table = await connection.scalar(
                text("SELECT to_regclass('public.slack_conversation_history')")
            )
            if identity != (EXPECTED_DATABASE, EXPECTED_USER):
                raise RuntimeError("Live database identity does not match expected target")
            if revision != EXPECTED_REVISION:
                raise RuntimeError("Live Alembic revision is not the expected starting revision")
            if auth_table is None or history_table is not None:
                raise RuntimeError(
                    "Live schema does not match the expected migration preconditions"
                )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help=f"apply only Alembic target {TARGET_REVISION}; otherwise perform read-only checks",
    )
    args = parser.parse_args()
    database_url = get_settings().database_url
    asyncio.run(verify_database_target(database_url))
    if args.apply:
        os.environ["HUBSPOT_PRODUCTION_MIGRATION_TARGET"] = TARGET_REVISION
        config = Config("alembic.ini")
        command.upgrade(config, TARGET_REVISION)
        print(f"Applied additive migration target {TARGET_REVISION}.")
    else:
        print(
            f"Preflight passed: database={EXPECTED_DATABASE}, user={EXPECTED_USER}, "
            f"revision={EXPECTED_REVISION}; target={TARGET_REVISION}. No migration applied."
        )


if __name__ == "__main__":
    main()

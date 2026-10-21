import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.conversation_models import SlackConversationExchange
from app.db.history_safety import validate_history_test_database
from app.repositories.slack_conversation import SlackConversationRepository

DATABASE_URL = os.environ.get("HISTORY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Run through scripts/test_history.ps1 with an isolated PostgreSQL target",
)


@pytest.fixture
async def history_session_factory():
    assert DATABASE_URL is not None
    validate_history_test_database(DATABASE_URL)
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        database, user = (await connection.execute(
            text("SELECT current_database(), current_user")
        )).one()
        assert database == "hubspot_ai_history_test"
        assert user == "hubspot_history_test"
        exists = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).has_table(
                "slack_conversation_exchanges"
            )
        )
        assert exists, "Run isolated Alembic migrations before persistence tests"
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_history_deduplicates_delivery_and_isolates_owner(
    history_session_factory,
):
    delivery = f"test-{uuid4().hex}"
    async with history_session_factory() as session:
        repository = SlackConversationRepository(session)
        async with session.begin():
            first = await repository.create_once(
                tenant_id="tenant-a", team_id="T-A", user_id="U-A",
                delivery_id=delivery, question="Question", response_status="ok",
                response_text="Answer", render_data={"result": {}, "cards": []},
            )
            duplicate = await repository.create_once(
                tenant_id="tenant-a", team_id="T-A", user_id="U-A",
                delivery_id=delivery, question="Question", response_status="ok",
                response_text="Answer", render_data={"result": {}, "cards": []},
            )
            other_workspace = await repository.create_once(
                tenant_id="tenant-a", team_id="T-B", user_id="U-A",
                delivery_id=delivery, question="Separate", response_status="ok",
                response_text="Separate answer", render_data={"result": {}, "cards": []},
            )
        assert first is True and duplicate is False and other_workspace is True

        rows = await repository.latest(
            tenant_id="tenant-a", team_id="T-A", user_id="U-A", limit=5
        )
        assert len(rows) == 1
        assert rows[0].delivery_id == delivery
        assert rows[0].question == "Question"

    async with history_session_factory() as session, session.begin():
        await session.execute(
            delete(SlackConversationExchange).where(
                SlackConversationExchange.delivery_id == delivery
            )
        )

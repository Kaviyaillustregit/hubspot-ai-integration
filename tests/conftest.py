import pytest

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="postgresql+asyncpg://app:app@localhost:5432/hubspot_ai")

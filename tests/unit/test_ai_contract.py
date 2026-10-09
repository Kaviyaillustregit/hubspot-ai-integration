from pydantic import BaseModel

from app.ai.provider import AIProvider
from app.ai.service import AIService


class Result(BaseModel):
    answer: str


class FakeProvider(AIProvider):
    async def generate_structured(self, *, prompt_name, variables, output_schema):
        return output_schema(answer=f"generated for {variables['name']}")


async def test_ai_service_uses_provider_contract():
    result = await AIService(FakeProvider()).generate(
        prompt_name="example/v1",
        variables={"name": "deal"},
        output_schema=Result,
    )

    assert result.answer == "generated for deal"


async def test_ai_service_logs_intent_extraction_timing(caplog):
    caplog.set_level("INFO", logger="app.ai.service")
    result = await AIService(FakeProvider()).generate(
        prompt_name="crm-intent/v2",
        variables={"name": "deal"},
        output_schema=Result,
    )

    assert result.answer == "generated for deal"
    assert [record.message for record in caplog.records] == [
        "Intent extraction started",
        "Intent extraction completed",
    ]
    assert isinstance(caplog.records[-1].elapsed_ms, float)

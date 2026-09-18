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

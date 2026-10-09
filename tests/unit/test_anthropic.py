from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.ai.anthropic import AnthropicProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError


class Result(BaseModel):
    answer: str


class FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text

    async def create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = FakeMessages(text)


@pytest.mark.parametrize(
    "response",
    [
        '{"answer":"ok"}',
        '```json\n{"answer":"ok"}\n```',
        '\ufeff```json\r\n{"answer":"ok"}\r\n```\r\n',
    ],
)
async def test_anthropic_provider_parses_raw_and_fenced_json(response):
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=FakeClient(response),  # type: ignore[arg-type]
    )

    result = await provider.generate_structured(
        prompt_name="account-intelligence/v1",
        variables={},
        output_schema=Result,
    )

    assert result == Result(answer="ok")


async def test_anthropic_provider_rejects_malformed_json():
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=FakeClient('```json\n{"answer": }\n```'),  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrationError, match="LLM returned an invalid response"):
        await provider.generate_structured(
            prompt_name="account-intelligence/v1",
            variables={},
            output_schema=Result,
        )

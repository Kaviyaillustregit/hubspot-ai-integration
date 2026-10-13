from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agent.schemas import DealAnalysis, DealFollowUpAssessment
from app.ai.anthropic import AnthropicProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError


class Result(BaseModel):
    answer: str


class FakeMessages:
    def __init__(
        self,
        text: str | None = None,
        *,
        responses: list[tuple[str, str]] | None = None,
    ) -> None:
        self.responses = responses or [(text or "", "end_turn")]
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        text, stop_reason = self.responses.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
        )


class FakeClient:
    def __init__(
        self,
        text: str | None = None,
        *,
        responses: list[tuple[str, str]] | None = None,
    ) -> None:
        self.messages = FakeMessages(text, responses=responses)


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


async def test_deal_analysis_parses_valid_json_with_feature_specific_budget():
    client = FakeClient(
        '{"summary":"On track.","identified_issues":[],"recommendations":'
        '["Confirm timeline.","Schedule review."]}'
    )
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    result = await provider.generate_structured(
        prompt_name="deal-analysis/v1",
        variables={},
        output_schema=DealAnalysis,
    )

    assert result.summary == "On track."
    assert client.messages.calls[0]["max_tokens"] == 1_400
    assert len(client.messages.calls) == 1


async def test_other_anthropic_requests_keep_the_existing_token_budget():
    client = FakeClient('{"answer":"ok"}')
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    result = await provider.generate_structured(
        prompt_name="account-intelligence/v1",
        variables={},
        output_schema=Result,
    )

    assert result == Result(answer="ok")
    assert client.messages.calls[0]["max_tokens"] == 700


async def test_deal_analysis_rejects_truncated_json_without_token_limit_stop_reason():
    client = FakeClient('{"summary":"Incomplete',)
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrationError, match="LLM returned an invalid response"):
        await provider.generate_structured(
            prompt_name="deal-analysis/v1",
            variables={},
            output_schema=DealAnalysis,
        )

    assert len(client.messages.calls) == 1


async def test_deal_analysis_retries_once_with_higher_budget_on_max_tokens():
    valid_json = (
        '{"summary":"Follow up promptly.","identified_issues":["No next meeting."],'
        '"recommendations":["Confirm stakeholders.","Book a review."]}'
    )
    client = FakeClient(
        responses=[
            ('{"summary":"Follow up', "max_tokens"),
            (valid_json, "end_turn"),
        ]
    )
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    result = await provider.generate_structured(
        prompt_name="deal-analysis/v1",
        variables={},
        output_schema=DealAnalysis,
    )

    assert result.summary == "Follow up promptly."
    assert [call["max_tokens"] for call in client.messages.calls] == [1_400, 2_800]


async def test_deal_analysis_failed_retry_preserves_invalid_response_error():
    client = FakeClient(
        responses=[
            ('{"summary":"First attempt', "max_tokens"),
            ('{"summary":"Second attempt', "max_tokens"),
        ]
    )
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrationError, match="LLM returned an invalid response"):
        await provider.generate_structured(
            prompt_name="deal-analysis/v1",
            variables={},
            output_schema=DealAnalysis,
        )

    assert [call["max_tokens"] for call in client.messages.calls] == [1_400, 2_800]


async def test_deal_follow_up_retries_truncated_json_with_feature_specific_budget():
    valid_json = (
        '{"summary":"Inactive deal.","reason":"No activity for 14 days.",'
        '"recommendations":["Confirm next step.","Offer a review."],'
        '"follow_up_email_draft":"Hello, can we discuss next steps?"}'
    )
    client = FakeClient(
        responses=[
            ('{"summary":"Inactive', "max_tokens"),
            (valid_json, "end_turn"),
        ]
    )
    provider = AnthropicProvider(
        Settings(anthropic_api_key="test-key"),
        client=client,  # type: ignore[arg-type]
    )

    result = await provider.generate_structured(
        prompt_name="deal-follow-up/v1",
        variables={},
        output_schema=DealFollowUpAssessment,
    )

    assert result.summary == "Inactive deal."
    assert [call["max_tokens"] for call in client.messages.calls] == [1_400, 2_800]

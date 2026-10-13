from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from app.ai.prompts import build_prompt, strip_json_fences
from app.ai.provider import AIProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError, IntegrationTimeoutError

OutputT = TypeVar("OutputT", bound=BaseModel)

_DEFAULT_MAX_TOKENS = 700
_DEAL_ANALYSIS_MAX_TOKENS = 1_400
_DEAL_ANALYSIS_RETRY_MAX_TOKENS = 2_800
_DEAL_PROMPTS = frozenset({"deal-analysis/v1", "deal-follow-up/v1"})


class AnthropicProvider(AIProvider):
    """Anthropic adapter. The application only depends on ``AIProvider``."""

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        self._settings = settings
        self._client = client

    async def generate_structured(
        self,
        *,
        prompt_name: str,
        variables: dict[str, Any],
        output_schema: type[OutputT],
    ) -> OutputT:
        if not self._settings.anthropic_api_key:
            raise IntegrationError("LLM provider is not configured")
        prompt = build_prompt(prompt_name, variables, output_schema)
        client = self._client or AsyncAnthropic(
            api_key=self._settings.anthropic_api_key,
            timeout=self._settings.request_timeout_seconds,
            max_retries=self._settings.max_retries,
        )
        try:
            max_tokens = (
                _DEAL_ANALYSIS_MAX_TOKENS
                if prompt_name in _DEAL_PROMPTS
                else _DEFAULT_MAX_TOKENS
            )
            response = await client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            if (
                prompt_name in _DEAL_PROMPTS
                and response.stop_reason == "max_tokens"
            ):
                response = await client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=_DEAL_ANALYSIS_RETRY_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
            text = "".join(block.text for block in response.content if block.type == "text")
            return output_schema.model_validate_json(strip_json_fences(text))
        except TimeoutError as exc:
            raise IntegrationTimeoutError("LLM request timed out") from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise IntegrationError("LLM returned an invalid response") from exc
        except Exception as exc:
            raise IntegrationError("LLM request failed") from exc

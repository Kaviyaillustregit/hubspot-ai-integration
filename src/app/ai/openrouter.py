import json
from typing import Any, TypeVar

from openai import APIError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.ai.provider import AIProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError, IntegrationTimeoutError

OutputT = TypeVar("OutputT", bound=BaseModel)


class OpenRouterProvider(AIProvider):
    """OpenRouter adapter behind the provider-neutral AIProvider contract."""

    def __init__(
        self,
        settings: Settings,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def generate_structured(
        self,
        *,
        prompt_name: str,
        variables: dict[str, Any],
        output_schema: type[OutputT],
    ) -> OutputT:
        if not self._settings.openrouter_api_key:
            raise IntegrationError("LLM provider is not configured")

        prompt = (
            f"Prompt: {prompt_name}\n"
            "Return JSON only, matching this schema exactly.\n"
            f"{json.dumps(output_schema.model_json_schema())}\n"
            "Use only these CRM facts; do not infer missing facts:\n"
            f"{json.dumps(variables, default=str)}"
        )

        client = self._client or AsyncOpenAI(
            api_key=self._settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=self._settings.request_timeout_seconds,
            max_retries=self._settings.max_retries,
        )

        try:
            response = await client.chat.completions.create(
                model=self._settings.openrouter_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700,
                response_format={"type": "json_object"},
            )

            text = response.choices[0].message.content
            if not text:
                raise IntegrationError("LLM returned an empty response")

            return output_schema.model_validate_json(text)

        except APITimeoutError as exc:
            raise IntegrationTimeoutError("LLM request timed out") from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise IntegrationError("LLM returned an invalid response") from exc
        except APIError as exc:
            raise IntegrationError("LLM request failed") from exc
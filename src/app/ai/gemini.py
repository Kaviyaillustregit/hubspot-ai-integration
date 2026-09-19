import json
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.ai.provider import AIProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError, IntegrationTimeoutError

OutputT = TypeVar("OutputT", bound=BaseModel)


class GeminiProvider(AIProvider):
    """Google Gemini adapter behind the provider-neutral AIProvider contract."""

    def __init__(
        self,
        settings: Settings,
        client: genai.Client | None = None,
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
        if not self._settings.gemini_api_key:
            raise IntegrationError("LLM provider is not configured")

        prompt = (
            f"Prompt: {prompt_name}\n"
            "Return JSON only matching the supplied schema.\n"
            f"Schema: {json.dumps(output_schema.model_json_schema())}\n"
            "Use only these CRM facts; do not infer missing facts.\n"
            f"{json.dumps(variables, default=str)}"
        )

        client = self._client or genai.Client(
            api_key=self._settings.gemini_api_key,
        )

        try:
            response = await client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=output_schema.model_json_schema(),
                    temperature=0,
                    max_output_tokens=700,
                    ),
            )

            text = response.text
            if not text:
                raise IntegrationError("LLM returned an empty response")

            return output_schema.model_validate_json(text)

        except TimeoutError as exc:
            raise IntegrationTimeoutError("LLM request timed out") from exc
        except (ValidationError, ValueError, TypeError) as exc:
            raise IntegrationError("LLM returned an invalid response") from exc
        except IntegrationError:
            raise
        except Exception as exc:
            raise IntegrationError("LLM request failed") from exc
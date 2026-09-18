from typing import Any, TypeVar

from pydantic import BaseModel

from app.ai.provider import AIProvider

OutputT = TypeVar("OutputT", bound=BaseModel)


class AIService:
    def __init__(self, provider: AIProvider) -> None:
        self._provider = provider

    async def generate(
        self,
        *,
        prompt_name: str,
        variables: dict[str, Any],
        output_schema: type[OutputT],
    ) -> OutputT:
        return await self._provider.generate_structured(
            prompt_name=prompt_name,
            variables=variables,
            output_schema=output_schema,
        )

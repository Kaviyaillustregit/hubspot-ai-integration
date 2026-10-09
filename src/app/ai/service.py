import logging
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from app.ai.provider import AIProvider

logger = logging.getLogger(__name__)

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
        measure_intent_extraction = prompt_name == "crm-intent/v2"
        started = perf_counter() if measure_intent_extraction else None
        if measure_intent_extraction:
            logger.info("Intent extraction started")

        try:
            return await self._provider.generate_structured(
                prompt_name=prompt_name,
                variables=variables,
                output_schema=output_schema,
            )
        finally:
            if started is not None:
                logger.info(
                    "Intent extraction completed",
                    extra={"elapsed_ms": round((perf_counter() - started) * 1000, 2)},
                )

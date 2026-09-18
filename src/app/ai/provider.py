from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


class AIProvider(ABC):
    """Provider-neutral contract for validated structured model responses."""

    @abstractmethod
    async def generate_structured(
        self,
        *,
        prompt_name: str,
        variables: dict[str, Any],
        output_schema: type[OutputT],
    ) -> OutputT: ...

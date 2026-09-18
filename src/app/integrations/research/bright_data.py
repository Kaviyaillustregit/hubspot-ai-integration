from abc import ABC, abstractmethod
from typing import Any


class BrightDataClient(ABC):
    @abstractmethod
    async def collect(
        self, target: str, *, options: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

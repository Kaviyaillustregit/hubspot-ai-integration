from abc import ABC, abstractmethod
from typing import Any


class TavilyClient(ABC):
    @abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> list[dict[str, Any]]: ...

from abc import ABC, abstractmethod
from typing import Any


class AuditService(ABC):
    """Future audit trail boundary for AI-initiated and human-approved actions."""

    @abstractmethod
    async def record(self, *, action: str, actor: str, payload: dict[str, Any]) -> None: ...

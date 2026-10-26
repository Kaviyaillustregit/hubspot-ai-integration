from abc import ABC, abstractmethod
from typing import Any


class SlackClient(ABC):
    """Business-facing Slack contract; signing and transport remain adapter concerns."""

    @abstractmethod
    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str | None: ...

    @abstractmethod
    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None: ...

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update an existing Slack message if supported by the client implementation."""
        return None

    async def delete_message(
        self,
        channel: str,
        ts: str,
    ) -> None:
        """Delete an existing Slack message if supported by the client implementation."""
        return None

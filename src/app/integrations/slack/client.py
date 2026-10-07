from abc import ABC, abstractmethod
from typing import Any


class SlackClient(ABC):
    """Business-facing Slack contract; signing and transport remain adapter concerns."""

    @abstractmethod
    async def post_message(self, channel: str, text: str) -> None: ...

    @abstractmethod
    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None: ...

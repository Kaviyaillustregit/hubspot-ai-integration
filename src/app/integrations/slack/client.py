from abc import ABC, abstractmethod


class SlackClient(ABC):
    """Business-facing Slack contract; signing and transport remain adapter concerns."""

    @abstractmethod
    async def post_message(self, channel: str, text: str) -> None: ...

import httpx

from app.core.config import Settings
from app.integrations.errors import IntegrationError, IntegrationTimeoutError
from app.integrations.slack.client import SlackClient


class SlackWebApiClient(SlackClient):
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def post_message(self, channel: str, text: str) -> None:
        if not self._settings.slack_bot_token:
            raise IntegrationError("Slack bot token is not configured")
        try:
            if self._client is not None:
                response = await self._client.post(
                    "https://slack.com/api/chat.postMessage",
                    json={"channel": channel, "text": text},
                    headers={"Authorization": f"Bearer {self._settings.slack_bot_token}"},
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        json={"channel": channel, "text": text},
                        headers={"Authorization": f"Bearer {self._settings.slack_bot_token}"},
                    )
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError("Slack message request timed out") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("Slack message request failed") from exc
        if response.is_error or not response.json().get("ok", False):
            raise IntegrationError("Slack message request failed")

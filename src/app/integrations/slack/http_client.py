from typing import Any

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

    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None:
        if not self._settings.slack_bot_token:
            raise IntegrationError("Slack bot token is not configured")
        url = "https://slack.com/api/views.publish"
        body = {"user_id": user_id, "view": view}
        headers = {"Authorization": f"Bearer {self._settings.slack_bot_token}"}
        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise IntegrationTimeoutError("Slack views.publish timed out") from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("Slack views.publish request failed") from exc
        if response.is_error:
            raise IntegrationError("Slack views.publish request failed")
        payload = response.json()
        if not payload.get("ok", False):
            # Slack's error code (e.g. invalid_blocks) carries no secrets and is needed to debug.
            raise IntegrationError(f"Slack views.publish failed: {payload.get('error')}")

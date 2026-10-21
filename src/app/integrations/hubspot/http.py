import asyncio
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.errors import (
    IntegrationAuthenticationError,
    IntegrationError,
    IntegrationNotFoundError,
    IntegrationPermissionError,
    IntegrationRateLimitError,
    IntegrationTimeoutError,
)

HUBSPOT_API = "https://api.hubapi.com"


class HubSpotApiClient:
    """Shared HubSpot HTTP transport with the adapters' normalized error mapping."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def _call(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        label: str,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{HUBSPOT_API}{path}"
        headers = {"Authorization": f"Bearer {access_token}"}
        retry_safe = method.upper() == "GET" or (
            method.upper() == "POST"
            and (path.endswith("/search") or path.endswith("/batch/read"))
        )
        retries = self._settings.hubspot_read_retry_attempts if retry_safe else 0
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                if self._client is not None:
                    response = await self._client.request(
                        method, url, params=params, headers=headers, json=json_body
                    )
                else:
                    async with httpx.AsyncClient(
                        timeout=self._settings.request_timeout_seconds
                    ) as client:
                        response = await client.request(
                            method, url, params=params, headers=headers, json=json_body
                        )
            except httpx.TimeoutException as exc:
                if attempt < retries:
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                raise IntegrationTimeoutError(f"{label} request timed out") from exc
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                raise IntegrationError(f"{label} request failed") from exc

            if response.status_code not in {429, 500, 502, 503, 504} or attempt >= retries:
                break
            await response.aread()
            await asyncio.sleep(_retry_delay(attempt, response.headers.get("Retry-After")))

        assert response is not None

        if response.status_code == 401:
            raise IntegrationAuthenticationError(f"{label} request was rejected")
        if response.status_code == 403:
            raise IntegrationPermissionError(f"{label} request was not permitted")
        if response.status_code == 404:
            raise IntegrationNotFoundError(f"{label} record was not found")
        if response.status_code == 429:
            raise IntegrationRateLimitError(f"{label} rate limit reached")
        if response.is_error:
            raise IntegrationError(f"{label} request failed")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(f"{label} response was invalid") from exc


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return min(30.0, max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2.0, 0.2 * (2**attempt)) + random.uniform(0.0, 0.1)

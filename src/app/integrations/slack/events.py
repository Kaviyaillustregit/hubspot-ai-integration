import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass


class SlackSignatureError(ValueError):
    pass


class SlackTenantResolver:
    """Resolves a verified Slack workspace to an application tenant, never from message input."""

    def __init__(self, mapping_json: str | None) -> None:
        try:
            mapping = json.loads(mapping_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("SLACK_TEAM_TENANT_MAP must be a JSON object") from exc
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise ValueError("SLACK_TEAM_TENANT_MAP must map workspace IDs to tenant IDs")
        self._mapping: Mapping[str, str] = mapping

    def resolve(self, team_id: str | None) -> str | None:
        return self._mapping.get(team_id or "")


class SlackRequestVerifier:
    def __init__(self, signing_secret: str, *, max_age_seconds: int = 300) -> None:
        self._signing_secret = signing_secret.encode()
        self._max_age_seconds = max_age_seconds

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        timestamp = headers.get("x-slack-request-timestamp")
        signature = headers.get("x-slack-signature")
        if not timestamp or not signature:
            raise SlackSignatureError("Slack signature headers are required")
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise SlackSignatureError("Slack timestamp is invalid") from exc
        if abs(time.time() - timestamp_value) > self._max_age_seconds:
            raise SlackSignatureError("Slack request is too old")
        base = b"v0:" + timestamp.encode() + b":" + raw_body
        expected = "v0=" + hmac.new(self._signing_secret, base, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise SlackSignatureError("Slack signature is invalid")


@dataclass(frozen=True)
class SlackMessage:
    team_id: str
    user_id: str
    channel_id: str
    text: str
    event_id: str
    # Slack's message timestamp; with channel_id it identifies the message across
    # retries and across message/app_mention deliveries of the same post.
    ts: str | None = None


def parse_message(payload: object) -> SlackMessage | None:
    """Parse employee messages and app mentions from a verified Events API payload."""
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return None
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") not in {"message", "app_mention"}:
        return None
    if event.get("type") == "message" and event.get("bot_id"):
        return None
    values = (
        payload.get("team_id"),
        event.get("user"),
        event.get("channel"),
        event.get("text"),
        payload.get("event_id"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    team_id, user_id, channel_id, text, event_id = values
    ts = event.get("ts")
    return SlackMessage(
        team_id=str(team_id),
        user_id=str(user_id),
        channel_id=str(channel_id),
        text=str(text),
        event_id=str(event_id),
        ts=ts if isinstance(ts, str) and ts else None,
    )


@dataclass(frozen=True)
class SlackHomeOpened:
    team_id: str
    user_id: str


def parse_app_home_opened(payload: object) -> SlackHomeOpened | None:
    """Parse a verified app_home_opened event for the Home tab (not the Messages tab)."""
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return None
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "app_home_opened":
        return None
    if event.get("tab") != "home":
        return None
    team_id = payload.get("team_id")
    user_id = event.get("user")
    if not isinstance(team_id, str) or not team_id or not isinstance(user_id, str) or not user_id:
        return None
    return SlackHomeOpened(team_id=team_id, user_id=user_id)

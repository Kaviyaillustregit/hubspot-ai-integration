"""Signed, expiring tokens that carry a Slack-verified identity into the web assistant.

Slack is the identity provider: the App Home publishes a short-lived *link* token for the
Slack user, and the web app exchanges it for a longer *session* token in an HttpOnly cookie.
The tenant always comes from the verified Slack workspace mapping, never from the browser.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal

Purpose = Literal["link", "session"]

LINK_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "hubspot_ai_session"
# Domain separation: this key is derived from, but never equal to, the Slack signing secret.
_KEY_LABEL = b"hubspot-ai-web-session-v1"


@dataclass(frozen=True)
class WebIdentity:
    tenant_id: str
    user_id: str


class WebSessionSigner:
    def __init__(self, slack_signing_secret: str) -> None:
        self._key = hmac.new(slack_signing_secret.encode(), _KEY_LABEL, hashlib.sha256).digest()

    def issue(
        self,
        identity: WebIdentity,
        *,
        purpose: Purpose,
        ttl_seconds: int,
        now: float | None = None,
    ) -> str:
        expires = int((now if now is not None else time.time()) + ttl_seconds)
        payload = json.dumps(
            {"t": identity.tenant_id, "u": identity.user_id, "p": purpose, "e": expires},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"{_encode(payload)}.{_encode(self._sign(payload))}"

    def verify(
        self, token: str | None, *, purpose: Purpose, now: float | None = None
    ) -> WebIdentity | None:
        if not token or token.count(".") != 1 or len(token) > 2048:
            return None
        encoded_payload, encoded_signature = token.split(".")
        try:
            payload = _decode(encoded_payload)
            signature = _decode(encoded_signature)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            claims = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(claims, dict) or claims.get("p") != purpose:
            return None
        expires = claims.get("e")
        if not isinstance(expires, int) or expires <= (now if now is not None else time.time()):
            return None
        tenant_id, user_id = claims.get("t"), claims.get("u")
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(user_id, str):
            return None
        if not user_id:
            return None
        return WebIdentity(tenant_id=tenant_id, user_id=user_id)

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid token encoding") from exc

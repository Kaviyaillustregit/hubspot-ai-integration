"""Slack App Home: a launch screen for the HubSpot AI web assistant.

The conversational experience lives in the web assistant (/assistant). Slack only
provides a professional, per-user signed entry point to it.
"""

from dataclasses import dataclass
from typing import Any

OPEN_WEB_ACTION = "open_web_assistant"

Block = dict[str, Any]


@dataclass(frozen=True)
class HomeInteraction:
    team_id: str
    user_id: str


def parse_home_interaction(payload: object) -> HomeInteraction | None:
    """Identify who interacted with our Home tab (e.g. a control on an older Home view)."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    view = payload.get("view")
    user = payload.get("user")
    team = payload.get("team")
    if not isinstance(view, dict) or view.get("type") != "home" or not isinstance(user, dict):
        return None
    user_id = user.get("id")
    team_id = team.get("id") if isinstance(team, dict) else None
    team_id = team_id or user.get("team_id")
    if not isinstance(user_id, str) or not user_id or not isinstance(team_id, str) or not team_id:
        return None
    return HomeInteraction(team_id=team_id, user_id=user_id)


def build_home_view(*, web_url: str | None) -> dict[str, Any]:
    blocks: list[Block] = [
        {"type": "header", "text": _plain("✦ HubSpot AI Agent")},
        _context("Your AI assistant for HubSpot CRM"),
        _section(
            "Create and manage contacts, companies, deals and CRM relationships "
            "using natural language."
        ),
    ]
    if web_url:
        # Signed for this Slack user; opens the web assistant already signed in.
        blocks.append(
            {
                "type": "actions",
                "block_id": "home_web_assistant",
                "elements": [
                    {
                        "type": "button",
                        "action_id": OPEN_WEB_ACTION,
                        "text": _plain("Open HubSpot AI ↗"),
                        "style": "primary",
                        "url": web_url,
                    }
                ],
            }
        )
    else:
        blocks.append(_context("The HubSpot AI web assistant isn't available yet."))
    blocks.append(_context("Powered by HubSpot AI"))
    return {"type": "home", "blocks": blocks}


def _plain(text: str) -> Block:
    return {"type": "plain_text", "text": text, "emoji": True}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}

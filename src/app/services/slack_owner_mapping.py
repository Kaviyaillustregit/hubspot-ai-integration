"""Slack user to HubSpot owner resolution and mapping service."""

import os
from typing import Any

from app.agent.schemas import AgentRequest
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.owners import HubSpotOwner

# In-memory registry for user email mappings (useful for testing and deterministic setups)
# Keyed by (tenant_id, actor_id) or ("", actor_id)
_SLACK_USER_EMAIL_REGISTRY: dict[tuple[str, str], str] = {}


def register_slack_user_email(tenant_id: str | None, actor_id: str, email: str) -> None:
    """Register a Slack user's email address for owner matching."""
    t_id = (tenant_id or "").strip()
    _SLACK_USER_EMAIL_REGISTRY[(t_id, actor_id.strip())] = email.strip().lower()


def clear_slack_user_email_registry() -> None:
    """Clear all registered Slack user email mappings."""
    _SLACK_USER_EMAIL_REGISTRY.clear()


async def get_slack_user_email(
    request: AgentRequest,
    tools: Any = None,
    slack_client: Any = None,
) -> str | None:
    """
    Retrieve the Slack email address for the user initiating the request.
    Checks:
    1. request.actor_email
    2. in-memory registry
    3. environment variables (SLACK_USER_EMAIL_{tenant_id}_{actor_id}, SLACK_USER_EMAIL_{actor_id})
    4. slack_client or tools user email lookup
    """
    if getattr(request, "actor_email", None):
        return request.actor_email.strip().lower()

    actor_id = request.actor_id.strip() if request.actor_id else ""
    tenant_id = request.tenant_id.strip() if request.tenant_id else ""

    # Check in-memory registry (tenant-scoped first, then unscoped)
    if (tenant_id, actor_id) in _SLACK_USER_EMAIL_REGISTRY:
        return _SLACK_USER_EMAIL_REGISTRY[(tenant_id, actor_id)]
    if ("", actor_id) in _SLACK_USER_EMAIL_REGISTRY:
        return _SLACK_USER_EMAIL_REGISTRY[("", actor_id)]

    # Check environment variables
    env_keys = [
        f"SLACK_USER_EMAIL_{tenant_id}_{actor_id}".upper(),
        f"SLACK_USER_EMAIL_{actor_id}".upper(),
    ]
    for k in env_keys:
        v = os.getenv(k)
        if v and v.strip():
            return v.strip().lower()

    # Check slack_client if provided
    if slack_client and hasattr(slack_client, "get_user_email"):
        try:
            email = await slack_client.get_user_email(actor_id)
            if email and email.strip():
                return email.strip().lower()
        except Exception:
            pass

    # Check tools if it has slack user email lookup
    if tools and hasattr(tools, "get_slack_user_email"):
        try:
            email = await tools.get_slack_user_email(actor_id)
            if email and email.strip():
                return email.strip().lower()
        except Exception:
            pass

    return None


async def resolve_slack_user_hubspot_owner(
    request: AgentRequest,
    tools: Any,
    slack_client: Any = None,
) -> tuple[HubSpotOwner | None, str | None]:
    """
    Match the Slack user to an authenticated HubSpot owner for the current tenant.

    Returns:
        (HubSpotOwner, None) on success
        (None, error_message) when mapping is unavailable

    Strict safety constraints:
    - Never assign another user's ownership
    - Never select the first owner arbitrarily
    - Never fallback to a common default owner
    - Tenant-isolated
    """
    actor_id = request.actor_id if request.actor_id else "unknown"
    tenant_id = request.tenant_id

    slack_email = await get_slack_user_email(request, tools, slack_client)
    if not slack_email:
        return (
            None,
            f"Could not determine the Slack email address for user '{actor_id}'. "
            "Please ensure your Slack profile includes a valid email address.",
        )

    if not hasattr(tools, "list_owners"):
        return (
            None,
            "HubSpot owner lookup is not configured for this workspace.",
        )

    try:
        owners = await tools.list_owners(tenant_id)
    except IntegrationError as exc:
        return (
            None,
            f"Failed to retrieve HubSpot owners for workspace '{tenant_id}': {exc}",
        )
    except Exception as exc:
        return (
            None,
            f"Unexpected error retrieving HubSpot owners: {exc}",
        )

    if not owners:
        return (
            None,
            f"No HubSpot owners found for workspace '{tenant_id}'.",
        )

    # Match Slack user email with HubSpot owner email (case-insensitive, normalized)
    normalized_slack_email = slack_email.strip().lower()
    matched_owner = None
    for owner in owners:
        if owner.email and owner.email.strip().lower() == normalized_slack_email:
            matched_owner = owner
            break

    if not matched_owner:
        return (
            None,
            f"No HubSpot owner found with email '{normalized_slack_email}' in workspace '{tenant_id}'. "
            "Your Slack email must match your HubSpot user email.",
        )

    return (matched_owner, None)

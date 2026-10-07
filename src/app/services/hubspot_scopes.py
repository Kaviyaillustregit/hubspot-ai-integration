from app.integrations.errors import IntegrationPermissionError
from app.integrations.hubspot.oauth import StoredOAuthToken


def require_scope(token: StoredOAuthToken, scope: str) -> None:
    """Fail fast, with a precise reason, when the tenant's grant lacks a CRM object scope."""
    if token.scopes and scope not in token.scopes:
        raise IntegrationPermissionError(
            f"HubSpot connection is missing the {scope} scope",
            scope=scope,
        )

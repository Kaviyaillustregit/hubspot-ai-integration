from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.errors import AppError
from app.core.secrets import SecretCipher
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.contacts import HubSpotContactsClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotContactsPage
from app.integrations.hubspot.oauth import HubSpotOAuthTokenClient
from app.repositories.hubspot_oauth import HubSpotOAuthRepository
from app.services.hubspot_contacts import (
    HubSpotAccessTokenProvider,
    HubSpotContactsService,
)

router = APIRouter(prefix="/hubspot", tags=["hubspot-contacts"])


def get_contacts_service(request: Request) -> HubSpotContactsService:
    settings = request.app.state.settings
    if not settings.hubspot_token_encryption_key:
        raise AppError("oauth_not_configured", "HubSpot OAuth is not configured", 503)
    token_client = HubSpotOAuthTokenClient(settings)
    provider = HubSpotAccessTokenProvider(
        request.app.state.session_factory,
        token_client,
        SecretCipher(settings.hubspot_token_encryption_key),
        HubSpotOAuthRepository,
    )
    return HubSpotContactsService(HubSpotContactsClient(settings), provider)


@router.get("/contacts", response_model=HubSpotContactsPage)
async def list_contacts(
    service: Annotated[HubSpotContactsService, Depends(get_contacts_service)],
    tenant_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    limit: int = Query(default=100, ge=1, le=100),
    after: str | None = Query(default=None, min_length=1),
    properties: str | None = Query(default=None),
) -> HubSpotContactsPage:
    try:
        return await service.list_contacts(
            context=TenantContext(
                tenant_id=tenant_id,
                hubspot_account_id="",
                credential_reference="hubspot-oauth-token",
            ),
            limit=limit,
            after=after,
            properties=tuple(
                property_name.strip()
                for property_name in (properties or "").split(",")
                if property_name.strip()
            ),
        )
    except ValueError as exc:
        raise AppError(
            "hubspot_oauth_required", "HubSpot OAuth connection is required", 401
        ) from exc
    except IntegrationError as exc:
        raise AppError("hubspot_contacts_failed", "HubSpot Contacts request failed", 502) from exc
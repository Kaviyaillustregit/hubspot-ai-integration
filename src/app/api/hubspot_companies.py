from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.errors import AppError
from app.core.secrets import SecretCipher
from app.integrations.errors import IntegrationError
from app.integrations.hubspot.companies import HubSpotCompaniesClient
from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import (
    HubSpotCompaniesPage,
    HubSpotContactCompanyAssociations,
)
from app.integrations.hubspot.oauth import HubSpotOAuthTokenClient
from app.repositories.hubspot_oauth import HubSpotOAuthRepository
from app.services.hubspot_companies import HubSpotCompaniesService
from app.services.hubspot_contacts import HubSpotAccessTokenProvider

router = APIRouter(prefix="/hubspot", tags=["hubspot-companies"])


def get_companies_service(request: Request) -> HubSpotCompaniesService:
    settings = request.app.state.settings
    if not settings.hubspot_token_encryption_key:
        raise AppError("oauth_not_configured", "HubSpot OAuth is not configured", 503)
    provider = HubSpotAccessTokenProvider(
        request.app.state.session_factory,
        HubSpotOAuthTokenClient(settings),
        SecretCipher(settings.hubspot_token_encryption_key),
        HubSpotOAuthRepository,
    )
    return HubSpotCompaniesService(HubSpotCompaniesClient(settings), provider)


def _properties(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


@router.get("/companies", response_model=HubSpotCompaniesPage)
async def list_companies(
    service: Annotated[HubSpotCompaniesService, Depends(get_companies_service)],
    tenant_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    limit: int = Query(default=100, ge=1, le=100),
    after: str | None = Query(default=None, min_length=1),
    properties: str | None = Query(default=None),
) -> HubSpotCompaniesPage:
    try:
        return await service.list_companies(
            TenantContext(tenant_id, "", "hubspot-oauth-token"),
            limit=limit,
            after=after,
            properties=_properties(properties),
        )
    except ValueError as exc:
        raise AppError(
            "hubspot_oauth_required", "HubSpot OAuth connection is required", 401
        ) from exc
    except IntegrationError as exc:
        raise AppError("hubspot_companies_failed", "HubSpot Companies request failed", 502) from exc


@router.get(
    "/contacts/{contact_id}/associations/companies",
    response_model=HubSpotContactCompanyAssociations,
)
async def get_contact_company_associations(
    service: Annotated[HubSpotCompaniesService, Depends(get_companies_service)],
    tenant_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    contact_id: str = Path(min_length=1, max_length=128),
) -> HubSpotContactCompanyAssociations:
    try:
        return await service.get_contact_company_associations(
            TenantContext(tenant_id, "", "hubspot-oauth-token"), contact_id
        )
    except ValueError as exc:
        raise AppError(
            "hubspot_oauth_required", "HubSpot OAuth connection is required", 401
        ) from exc
    except IntegrationError as exc:
        raise AppError(
            "hubspot_associations_failed", "HubSpot association request failed", 502
        ) from exc
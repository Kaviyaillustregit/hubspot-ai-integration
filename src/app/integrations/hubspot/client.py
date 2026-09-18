from abc import ABC, abstractmethod

from app.integrations.hubspot.context import TenantContext
from app.integrations.hubspot.models import HubSpotRecord, HubSpotTaskCreate


class HubSpotClient(ABC):
    """Business-facing HubSpot contract; SDK and HTTP details stay in its adapter."""

    @abstractmethod
    async def get_contact(self, context: TenantContext, contact_id: str) -> HubSpotRecord: ...

    @abstractmethod
    async def get_company(self, context: TenantContext, company_id: str) -> HubSpotRecord: ...

    @abstractmethod
    async def get_deal(self, context: TenantContext, deal_id: str) -> HubSpotRecord: ...

    @abstractmethod
    async def get_ticket(self, context: TenantContext, ticket_id: str) -> HubSpotRecord: ...

    @abstractmethod
    async def create_task(
        self, context: TenantContext, payload: HubSpotTaskCreate
    ) -> HubSpotRecord: ...

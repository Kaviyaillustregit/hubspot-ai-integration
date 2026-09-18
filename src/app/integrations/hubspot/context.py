from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Non-secret identity used to resolve a tenant's integration credentials."""

    tenant_id: str
    hubspot_account_id: str
    credential_reference: str
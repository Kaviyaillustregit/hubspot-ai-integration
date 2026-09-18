from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OAuthStateRecord(Base):
    __tablename__ = "hubspot_oauth_states"

    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class HubSpotOAuthTokenRecord(Base):
    __tablename__ = "hubspot_oauth_tokens"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hubspot_account_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
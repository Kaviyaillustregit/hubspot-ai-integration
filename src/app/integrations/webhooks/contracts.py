from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class InboundWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str
    event_id: str
    received_at: datetime
    payload: dict[str, object]


class WebhookVerifier(Protocol):
    async def verify(self, *, body: bytes, signature: str, timestamp: str) -> bool: ...


class EventStore(Protocol):
    async def claim(self, event: InboundWebhook) -> bool: ...

    async def mark_processed(self, event_id: str) -> None: ...


class EventProcessor(Protocol):
    async def process(self, event: InboundWebhook) -> None: ...


class EventIdempotencyKey(BaseModel):
    event_id: str
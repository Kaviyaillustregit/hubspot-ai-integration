# HubSpot AI Revenue Platform

Production-oriented foundation for an AI Revenue and CRM Operations platform built around HubSpot. This repository intentionally contains architecture and runtime foundations only; business workflows and live integrations are not implemented yet.

## Architecture

```text
API / Slack adapters
        |
FastAPI API layer
        |
Service / business layer
        |
AI service + validated schemas
        |
Provider-neutral integration clients and tools
        |
HubSpot / Slack / Claude / Tavily / Bright Data adapters
        |
Repositories + PostgreSQL transaction boundary
```

### Module boundaries

- `src/app/api`: FastAPI application factory, versioned routes, request IDs, health checks.
- `src/app/core`: environment-backed settings and structured logging context.
- `src/app/db`: async SQLAlchemy engine/session setup, declarative base, and Alembic integration.
- `src/app/services`: business workflows, audit authorization boundaries, and HubSpot OAuth orchestration.
- `src/app/ai`: provider-neutral structured generation contracts and schema validation entry points.
- `src/app/integrations`: isolated contracts for HubSpot, Slack, Tavily, Bright Data, and inbound webhooks.
- `src/app/repositories`: application-owned persistence abstractions.
- `prompts`: versioned prompt assets, separate from application code.
- `tests/unit`, `tests/integration`: isolated tests for contracts and future external/database boundaries.

Business services must depend on the contracts in `ai`, `integrations`, and `repositories`, never on SDK-specific implementation details. Write tools and sensitive mutations should be introduced later with explicit authorization, human confirmation, idempotency, and audit records.

Tenant-aware integration calls receive an explicit `TenantContext`; credentials are represented by a reference, not passed as global process configuration. Future mutations must pass through recommendation, authorization, approval, execution, and audit boundaries. Services own `UnitOfWork` transaction boundaries; repositories must not commit independently. Inbound webhook verification and idempotency are separate from outbound integration clients.

The HubSpot OAuth slice uses HubSpot's current developer-platform `POST /oauth/v3/token` endpoint for authorization-code exchange and refresh. Access and refresh tokens are encrypted with the configured Fernet key before persistence, and OAuth state is stored and consumed once per tenant. Token exchange and refresh results never include token values in API responses.

## Local setup

1. Create a Python 3.11+ virtual environment.
2. Install development dependencies:

   ```powershell
   python -m pip install -e ".[dev]"
   ```

3. Copy `.env.example` to `.env` and set only local development values. Never commit `.env`.
4. Start PostgreSQL locally, or use `docker compose up --build`.
5. Run the API:

   ```powershell
   python -m uvicorn app.main:app --app-dir src --reload
   ```

6. Check `GET /api/v1/health/live`. Readiness checks the configured PostgreSQL connection at `GET /api/v1/health/ready`.

## Quality checks

```powershell
python -m ruff check src tests
python -m pytest
python -m compileall -q src tests migrations
```

## Account Intelligence Slack slice

Employees can ask the Slack app for a named account, for example: `Give me the latest
information about Test AI Company.` The signed Slack Events request is acknowledged
immediately, then the independent account-intelligence agent retrieves only the HubSpot
company data it needs and posts a response. Responses always label CRM facts separately
from AI observations/suggestions.

Configure these server-side environment values (never commit them):

```text
SLACK_SIGNING_SECRET=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_TEAM_TENANT_MAP={"T01234567":"your-internal-tenant-id"}
ANTHROPIC_API_KEY=...
HUBSPOT_TOKEN_ENCRYPTION_KEY=...
```

`SLACK_TEAM_TENANT_MAP` is an application-owned workspace-to-tenant authorization map;
the endpoint never accepts a tenant ID from a Slack message. The relevant tenant must
already have completed the existing HubSpot OAuth connection flow.

To run locally, expose the FastAPI server through an HTTPS tunnel, configure that public
URL plus `/api/v1/slack/events` as the Slack app's Event Subscriptions Request URL, and
subscribe to the bot message event appropriate for the channels where the app is used.
Install the app with permission to post messages. Slack's URL verification challenge is
handled by the same endpoint. Unit tests mock Slack, HubSpot, OAuth, and LLM boundaries;
they do not prove a live Slack installation or provider credentials.

## Not implemented yet

There is no client connection, OAuth flow, webhook receiver, Slack authentication, Claude request, research API call, CRM mutation, AI agent, queue, or feature workflow in this foundation. Before implementing those modules, decide the deployment target, tenant model, OAuth token storage/encryption strategy, queue choice, authorization model, retention policy, and approval UX for sensitive actions.

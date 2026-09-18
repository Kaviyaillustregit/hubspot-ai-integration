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
- `src/app/services`: business workflows and audit authorization boundaries.
- `src/app/ai`: provider-neutral structured generation contracts and schema validation entry points.
- `src/app/integrations`: isolated contracts for HubSpot, Slack, Tavily, Bright Data, and inbound webhooks.
- `src/app/repositories`: application-owned persistence abstractions.
- `prompts`: versioned prompt assets, separate from application code.
- `tests/unit`, `tests/integration`: isolated tests for contracts and future external/database boundaries.

Business services must depend on the contracts in `ai`, `integrations`, and `repositories`, never on SDK-specific implementation details. Write tools and sensitive mutations should be introduced later with explicit authorization, human confirmation, idempotency, and audit records.

Tenant-aware integration calls receive an explicit `TenantContext`; credentials are represented by a reference, not passed as global process configuration. Future mutations must pass through recommendation, authorization, approval, execution, and audit boundaries. Services own `UnitOfWork` transaction boundaries; repositories must not commit independently. Inbound webhook verification and idempotency are separate from outbound integration clients.

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

## Not implemented yet

There is no client connection, OAuth flow, webhook receiver, Slack authentication, Claude request, research API call, CRM mutation, AI agent, queue, or feature workflow in this foundation. Before implementing those modules, decide the deployment target, tenant model, OAuth token storage/encryption strategy, queue choice, authorization model, retention policy, and approval UX for sensitive actions.

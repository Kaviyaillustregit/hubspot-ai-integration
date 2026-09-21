# HubSpot AI Revenue Platform

Production-oriented AI Revenue and CRM Operations platform built around HubSpot.

The repository currently contains a **working first vertical slice**:

```text
Slack
  ↓
FastAPI
  ↓
Slack Workspace → Tenant
  ↓
HubSpot OAuth / CRM
  ↓
AI Account Intelligence Agent
  ↓
Configured LLM Provider
  ↓
Slack Response

The current implementation is validated in a development/test environment. Production deployment, production credentials, CRM write actions, proactive workflows, and additional business features are still pending.

Architecture
                    Slack / User
                         |
                         v
                  +-------------+
                  |   FastAPI   |
                  +------+------+
                         |
                         v
                 +---------------+
                 |   AI Agent    |
                 | Orchestrator  |
                 +------+--------+
                        |
                 +------+------+
                 |             |
                 v             v
            HubSpot CRM    LLM Provider
                           |     |     |
                           v     v     v
                        Gemini Anthropic OpenRouter
Module boundaries
src/app/api: FastAPI application factory, versioned routes, request IDs, health checks, Slack and HubSpot API routes.
src/app/core: environment-backed settings and structured logging.
src/app/db: async SQLAlchemy engine/session setup and database integration.
src/app/agent: Account Intelligence agent, schemas, and HubSpot tool registry.
src/app/ai: provider-neutral AI contracts, AI service, and LLM provider adapters.
src/app/integrations: isolated integration contracts and clients.
src/app/repositories: application-owned persistence abstractions.
prompts: versioned prompt assets.
tests/unit, tests/integration: application and integration tests.

Business services should depend on provider/integration contracts rather than SDK-specific implementations.

Sensitive CRM mutations are intentionally not enabled yet. Future write actions must use explicit authorization, confirmation, idempotency, execution, and audit boundaries.

Current Implemented Capabilities
HubSpot
HubSpot OAuth authorization-code flow
OAuth token refresh
Encrypted token storage
Tenant-aware HubSpot connections
Contacts retrieval
Companies retrieval
Contact → Company association retrieval
AI Agent
Account Intelligence Agent
Tenant-aware agent requests
HubSpot tool registry
Structured AI output
Pydantic response validation
CRM facts separated from AI observations/suggestions
LLM Providers

Supported providers:

Gemini
Anthropic
OpenRouter

Provider selection is controlled through:

AI_PROVIDER=gemini

The AI Agent depends on the provider-neutral AIProvider interface, so the LLM provider can be changed without changing the agent architecture.

Slack
Slack Events API
Slack request signature verification
Replay protection
URL verification
Workspace → tenant mapping
app_mention handling
Slack response publishing

Endpoint:

POST /api/v1/slack/events
Current End-to-End Demo

Example Slack request:

@HubSpot AI Agent give me the latest information about Test AI Company

Current flow:

Slack
  ↓
Slack Events API
  ↓
FastAPI
  ↓
Workspace → Tenant
  ↓
HubSpot CRM
  ↓
Account Intelligence Agent
  ↓
Gemini
  ↓
Structured Response
  ↓
Slack

The response separates:

CRM facts
AI observations/suggestions

The current demo uses a HubSpot test tenant and test data.

Local Setup:

Requirements
Git
Python 3.11+
Docker Desktop
VS Code
HubSpot developer/test account
Slack test workspace
LLM API key for the selected provider

Optional for Slack local testing:

ngrok
Node.js/npm for HubSpot CLI-related work

Install:
python -m pip install -e ".[dev]"

Create the local environment file:

Copy-Item .env.example .env

Never commit .env.

Start the Application:

docker compose up -d --build

Check services:

docker compose ps

API:

http://127.0.0.1:8000

Health:

http://127.0.0.1:8000/api/v1/health/live

Readiness:

http://127.0.0.1:8000/api/v1/health/ready
Environment Configuration
Gemini

Current test configuration:

AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite
Anthropic

For company-approved production/testing use:

AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=...
OpenRouter

Optional testing provider:

AI_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=openrouter/free

Use only test/synthetic CRM data with free/public model providers.

HubSpot
HUBSPOT_CLIENT_ID=...
HUBSPOT_CLIENT_SECRET=...
HUBSPOT_REDIRECT_URI=http://localhost:8000/api/v1/auth/hubspot/callback
HUBSPOT_TOKEN_ENCRYPTION_KEY=...
Slack
SLACK_SIGNING_SECRET=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_TEAM_TENANT_MAP={"YOUR_SLACK_TEAM_ID":"test-tenant"}

The Slack workspace-to-tenant mapping is server-controlled. A Slack message cannot choose its own tenant.

HubSpot OAuth Setup

Start the OAuth flow:

http://localhost:8000/api/v1/auth/hubspot/start

The test tenant must complete HubSpot OAuth before the AI Agent can retrieve CRM data.

Slack Local Testing

Expose the FastAPI server through an HTTPS tunnel:

ngrok http 8000

Configure the Slack Event Subscription Request URL:

https://<ngrok-domain>/api/v1/slack/events

Enable the appropriate Slack bot event and install the app in the test channel.

Example:

@HubSpot AI Agent give me the latest information about Test AI Company
Quality Checks

Run:

python -m ruff check src tests
python -m pytest
python -m mypy src
python -m compileall -q src tests migrations
git diff --check

Current validated test suite:

33 tests passed
Useful Logs

Watch API logs:

docker compose logs -f api

Or:

docker compose logs api --tail=100
Security

Never commit or expose:

.env
API keys
HubSpot client secrets
HubSpot tokens
Slack bot tokens
Slack signing secrets
Encryption keys
Cloud credentials

Production environments should use company-approved secret management.

Current Project Status
Completed
FastAPI foundation
PostgreSQL integration
HubSpot OAuth
Encrypted HubSpot token storage
Tenant-aware HubSpot access
Contacts retrieval
Companies retrieval
Associations retrieval
Account Intelligence Agent
AIProvider abstraction
Gemini provider
Anthropic provider
OpenRouter provider
Slack Events API
Slack signature verification
Slack workspace → tenant mapping
Slack app mentions
Slack response publishing
Docker setup
Unit tests
Ruff
mypy
Live Slack → HubSpot → Gemini → Slack test
Pending for Production
Production cloud deployment
Production HubSpot client account
Production Slack installation/admin approval
Company-approved production LLM/API
Production PostgreSQL
Secret management
CI/CD
Monitoring and alerting
Advanced RBAC
CRM write actions
Deals / Tickets / Tasks workflows
Proactive AI alerts
AI enrichment
Production UAT and acceptance testing
Production hardening and operational monitoring
Current Milestone

Working Slack + HubSpot Account Intelligence AI Agent vertical slice.

The current implementation demonstrates the AI Agent acting as an intelligence/orchestration layer on top of HubSpot CRM.

The next phase is to expand this into a production-ready CRM AI system with controlled CRM actions, additional workflows, security/authorization, and production infrastructure.


This version is much more accurate for your senior because it tells him **what is already working, how to run it, and what is still pending**, instead of making him think the repository is still only a foundation.
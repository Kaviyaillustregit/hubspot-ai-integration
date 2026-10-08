# HubSpot AI Revenue Platform

AI-powered CRM intelligence platform built around HubSpot, Slack, FastAPI, and LLM providers.

## Architecture

```text
Slack
  ↓
FastAPI
  ↓
Workspace → Tenant
  ↓
HubSpot CRM
  ↓
AI Agent
  ↓
Gemini / Anthropic / OpenRouter
  ↓
Slack Response
Current Capabilities
HubSpot OAuth and tenant-aware CRM access
Companies, Contacts, Associations, and Deals
Account Intelligence AI Agent
Gemini, Anthropic, and OpenRouter support
Slack Events API integration
Slack workspace → tenant mapping
Slack → HubSpot → AI → Slack workflow
Docker Compose
Unit and integration tests
Setup
Requirements
Python 3.11+
Docker Desktop
HubSpot developer/test account
Slack test workspace
LLM API key
Install
git clone <repository-url>
cd hubspot-ai-integration
python -m pip install -e ".[dev]"
Copy-Item .env.example .env

Configure the required values in .env.

Start
docker compose up -d --build
docker compose ps

API:

http://127.0.0.1:8000

Health:

http://127.0.0.1:8000/api/v1/health/live
HubSpot OAuth

Open:

http://localhost:8000/api/v1/auth/hubspot/start

Complete the OAuth flow using the test HubSpot account.

Slack Local Testing

Start an HTTPS tunnel:

cloudflared tunnel --url http://localhost:8000

Set the generated URL in:

WEB_APP_BASE_URL=https://<your-tunnel>.trycloudflare.com

Restart the API:

docker compose restart api

Configure Slack Event Subscription:

https://<your-tunnel>.trycloudflare.com/api/v1/slack/events

Keep the Cloudflare tunnel running while testing.

Test
python -m pytest
python -m ruff check src tests
python -m mypy src
Security

Never commit:

.env
API keys
HubSpot secrets/tokens
Slack tokens/secrets
Encryption keys

Use company-approved secret management for production.

Status
Working

Slack + HubSpot + Account Intelligence AI Agent vertical slice.

Pending
Production deployment
Production credentials
Production database and secrets
Advanced RBAC
CRM write workflows
Proactive AI workflows
Monitoring and CI/CD
Production UAT
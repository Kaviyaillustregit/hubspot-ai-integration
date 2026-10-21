# Stage 1 CRM intelligence regression coverage

Run the Stage 1 suite from the workspace root with `./scripts/test_stage1.ps1`.
The suite uses deterministic providers and synthetic HubSpot records; it makes no
paid model calls, live HubSpot calls, Slack posts, or CRM writes.

| Capability | Coverage | Status |
| --- | --- | --- |
| Overall/by-stage counts, empty results, all-page aggregation | `test_stage1_deal_intelligence.py` | Tested |
| Sum, average, minimum, maximum; missing amounts and currency handling | `test_stage1_deal_intelligence.py` | Tested |
| Explicit and relative dates, before/after/since, local timezone bounds | `test_stage1_deal_intelligence.py` | Tested |
| Grouping by stage, pipeline, owner ID, month, quarter, year | `test_stage1_deal_intelligence.py` | Tested |
| Deal name/stage/pipeline/owner ID/amount/date filter combinations | `test_stage1_deal_intelligence.py` | Tested |
| Latest, oldest, highest, lowest ordering; top N and offset pagination | `test_stage1_deal_intelligence.py` | Tested |
| Latest deal properties, Stage 1 links/table rendering, charts | `test_stage1_deal_intelligence.py`, `test_agent_crm_intelligence_integration.py` | Tested |
| Structured extraction and `all_deals` routing, invalid metadata, extraction fallback | `test_stage1_deal_intelligence.py`, `test_agent_crm_intelligence_integration.py` | Tested |
| Slack tenant mapping/signature, read-only Stage 1 route | `test_agent_crm_intelligence_integration.py` | Tested |
| Follow-up date filter using prior conversation context | `test_stage1_deal_intelligence.py` | Tested |

Company/contact analytics are outside Stage 1. Ambiguous date phrasing is not
converted into a guessed filter. Tests use mock providers and synthetic deals; passing
tests do not verify live HubSpot or Slack behavior.

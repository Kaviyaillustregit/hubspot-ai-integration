# Stage 1 Deal Intelligence Acceptance Report

## Results

- **Natural-language acceptance prompts:** 100 supported prompts, 100 passed, 0 failed, 0 unsupported. The runner also has one fixture/pagination integrity test (101 total passed).
- **Acceptance path:** The suite posts Slack-signed App Home interactions through FastAPI and the real agent and Block Kit renderer. Slack publishing is captured by a fake client. HubSpot deals and pipeline HTTP responses come from a deterministic 130-deal `httpx.MockTransport` fixture, with two deal pages.
- **Stage 1 regression script:** `scripts/test_stage1.ps1` — 73 passed.
- **Slack App Home tests:** 71 passed.
- **Additional CRM query, extraction, list, and resolution tests:** 88 passed.
- **Ruff:** Passed for the changed Python implementation and test files.
- **Live HubSpot and Slack:** Not contacted. CRM data was read-only fixture data; no messages were sent.

## Coverage in the acceptance runner

The 100 prompts cover all-deal retrieval, latest-five sorting and requested fields, stage counts and filters, Q3 2026 and exact-date boundaries, amount filters, pipeline and owner filters, and month grouping. Independent fixture predicates check matching IDs/counts. List cases check record links, readable dates and amounts, compact Home output, and that each displayed deal appears once. The fixture includes 130 records to exercise HubSpot pagination, multiple stages/pipelines/owners/currencies, null amounts and close dates, and timestamp dates at the quarter boundary.

The existing Stage 1 tests additionally cover analytics, currency handling, timezone conversion, chart payloads, missing fields, ambiguous dates, and follow-up behavior.

## Findings corrected

- Canonicalized planner bounds for an explicitly requested quarter when the planner returns an end date that would exclude the quarter's final day. Non-quarter explicit ranges are preserved.
- Corrected “this quarter” headings to name the resolved quarter and year.
- Kept per-deal text in the canonical agent response for existing Slack message consumers, while the App Home table renderer omits those repeated detail lines.
- Displayed dates in readable month/day/year form and included available deal currency codes in list amounts.
- Avoided combined amount calculations when filtered deals have incompatible or missing currency metadata.
- Expanded deterministic handling for reversed latest-N wording, owner and pipeline filters, stage breakdown phrasing, monthly grouping, and “what deals” listing requests.

## Remaining verification issues and limitations

- The broader `tests/unit/test_crm_operations.py` run reports **16 failed, 71 passed**. Failures include older exact-result-shape and App Home emoji expectations, legacy list-routing expectations, and one provider-failure expectation. These tests were not changed; because no clean baseline run was available, their regression status is **undetermined**. They need review before claiming the entire CRM suite is green.
- `tests/unit/test_slack_events.py` reports **3 failed, 6 passed** (two requests return 503 instead of the expected signature/challenge responses, and one contact-create case errors). Slack Events code was not changed in this work; baseline status is **undetermined**.
- Live HubSpot behavior, real Slack delivery, model-specific extraction quality, and production timezone/account data remain unverified.
- Large result sets are safely capped in the Home view and report the displayed count. There is no interactive Next Page button; users can request a numbered page in natural language.

## Deployment assessment

Stage 1 acceptance, focused Stage 1 regressions, Slack App Home tests, and Ruff pass. **Do not describe the workspace as fully deployment-ready yet:** resolve or formally disposition the 16 CRM Operations and 3 Slack Events failures first. No database, Docker, deployment, or live-service operation was performed.

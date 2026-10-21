# Slack AI Agent Regression Audit

**Date:** 2026-10-20  
**Scope:** Read-only inspection of the workspace and automated tests. No source files, Docker containers, CRM records, Slack messages, ports, or database settings were changed.  
**Test command:** `pytest -q` — **844 passed, 31 failed, 1 skipped** (30.14s). The tests use local fakes/fixtures; this run does not verify live Slack or HubSpot.

## Functionality inventory

| Area | Supported behavior found in source/tests | Main path |
|---|---|---|
| Contacts | List/search, named/ID lookup, selected properties, lifecycle filters, contact-by-company association query, paged retrieval; create/update/delete flows | `crm_intelligence.py`, `service.py`, `operations.py`, `tools.py` |
| Companies | List/search, named/ID lookup, selected properties, company-associated contacts/deals, paged retrieval; create/update/delete flows | `operations.py`, `service.py`, `tools.py`, HubSpot services |
| Deals | List/search, ID/name lookup, selected fields, stage/pipeline/owner/amount/date filters, latest/oldest/top/bottom ordering, overdue/open filters | `crm_intelligence.py`, `service.py`, `operations.py` |
| Associations | Read contact/company and company/deal associations; create associations as part of supported workflows | `tools.py`, `operations.py`, HubSpot association services |
| Owners | Owner lookup/display when a resolvable owner ID is returned; unresolved IDs may remain IDs | `tools.py`, `service.py`, owner service |
| CRM search/listing | Contacts, companies, and deals have search/list paths; cursor paging and configured read-page caps exist | `tools.py`, `crm_query_intelligence.py` |
| Grouped analytics | Deal counts and amount metrics grouped by stage/status, pipeline, owner, month, quarter, year; contact/company grouping supported when property discovery/catalog resolves the field | `crm_intelligence.py`, `service.py` |
| Charts | Grouped and top-deal charts use QuickChart config/URL generation; chart output is rendered in Slack Home and channel formatting | `service.py`, `crm_intelligence.py`, `quickchart.py`, `home.py` |
| Mutations | Contacts: create/update/delete. Companies: create/update/delete. Deals: create/update; no deal delete tool or agent path was found. Create flows may execute directly; update/delete safety confirmation behavior varies by operation. This audit did not invoke mutations. | `operations.py`, `service.py`, `tools.py`, action-safety service, `slack.py` |
| Conversation history | Persists owner-scoped exchanges, deduplicates deliveries, provides previews, a View full response button, and Previous/Next pages | conversation service/repository, `home.py`, `slack.py` |
| Follow-ups | Deal follow-up/date questions can reuse prior conversation context; broader conversational follow-up behavior is not guaranteed by the inspected tests | `service.py`, CRM planner |
| Error handling | Safe CRM/AI error responses, permission/timeout/rate-limit paths, Slack signature checks, and invalid/missing-input handling exist | `service.py`, `slack.py`, API error and formatting modules |

“Supported” here means implemented and/or represented in local tests. It does not certify live account data, model extraction quality, tenant-specific property availability, or Slack delivery.

## Practical query test matrix

Status legend: **PASS (automated)** means the capability has relevant local unit/acceptance coverage; the whole suite still has failures noted below. **NOT TESTED (live)** means no real Slack query was sent. **UNSUPPORTED/conditional** means code requires a capability/catalog/field or does not provide the requested pagination control.

| ID | Slack test query / action | Expected result | Status |
|---|---|---|---|
| C-01 | “Show all contacts” | Paged contacts with count, readable requested/default fields and record links | PASS (automated); NOT TESTED (live) |
| C-02 | “Show contact [name or HubSpot ID]” | Exact/verified contact details; ambiguity or not-found handled safely | PASS (automated); NOT TESTED (live) |
| C-03 | “Show [email, job title, lifecycle stage] for contact [name]” | Only requested fields, correct labels and missing-value handling | PASS (formatter/query tests); NOT TESTED (live) |
| C-04 | “Show contacts at [company]” | Contacts from CRM association/search resolution; no guessed company match | PASS (association unit coverage); NOT TESTED (live) |
| C-05 | “Group contacts by lifecycle stage” | Correct group counts/labels and chart when supported by property catalog | PASS (fixture regression); NOT TESTED (live) |
| C-06 | Create a disposable test contact with unique email | No action unless approved/confirmed per selected flow; result/link on success | NOT TESTED — mutation prohibited for this audit |
| C-07 | Update the disposable contact’s title | Correct target and requested property; confirmation behavior where required | NOT TESTED — mutation prohibited |
| C-08 | Delete the disposable contact | Exact target; explicit safety/confirmation; no unintended deletion | NOT TESTED — mutation prohibited |
| CO-01 | “Show all companies” | Paged company records, counts, labels, links | PASS (automated); NOT TESTED (live) |
| CO-02 | “Show company [name or HubSpot ID]” | Exact/verified company detail or safe ambiguity/not-found response | PASS (automated); NOT TESTED (live) |
| CO-03 | “Show [industry, city, owner] for [company]” | Requested fields and labels; owner name if resolvable | PASS (formatter/query tests); NOT TESTED (live) |
| CO-04 | “Show contacts associated with [company]” | Associated contacts, not a broad unassociated search | PASS (association unit coverage); NOT TESTED (live) |
| CO-05 | “Group companies by industry” | Catalog-resolved industry counts and appropriate chart | PASS (fixture regression); NOT TESTED (live) |
| CO-06 | Create a uniquely named disposable test company | Safe create result and HubSpot link | NOT TESTED — mutation prohibited |
| CO-07 | Update the disposable company’s city | Correct target/property and confirmation behavior | NOT TESTED — mutation prohibited |
| CO-08 | Delete the disposable company | Exact target and explicit safety/confirmation | NOT TESTED — mutation prohibited |
| D-01 | “Show all deals” | Correct matched/returned counts, page indication, fields and links | PASS (automated fixtures); NOT TESTED (live) |
| D-02 | “Show deal [name or HubSpot ID]” | Verified deal details with stage/pipeline labels where resolvable | PASS (automated); NOT TESTED (live) |
| D-03 | “Show amount, stage, pipeline, owner, and close date for [deal]” | Requested fields, labels, correct owner display or clearly identified owner ID | PASS (formatter/query coverage); NOT TESTED (live) |
| D-04 | “Show deals grouped by stage” | All matching stages and correct counts/totals | FAIL (automated Slack Home acceptance cases; see IR-01) |
| D-05 | “Show deals grouped by pipeline” | Correct pipeline labels/counts; for the known case, 26 in Sales Pipeline | FAIL (observed by requester; not retested live) |
| D-06 | “Show deals grouped by owner” | Correct owner buckets and Unassigned bucket; owner labels resolved when possible | PASS (fixture regression); NOT TESTED (live) |
| D-07 | “What is the total deal value?” | Correct all-page sum with currency comparability safeguards | PASS (unit coverage); NOT TESTED (live) |
| D-08 | “Show overdue deals” | Open deals with close date before today; no closed deals | PASS (planner/unit coverage); NOT TESTED (live) |
| D-09 | Create a uniquely named disposable test deal | Safe create result; no live mutation during this audit | NOT TESTED — mutation prohibited |
| D-10 | Update the disposable deal amount/stage | Correct target/property, pipeline-valid stage, safety behavior | NOT TESTED — mutation prohibited |
| D-11 | Delete a deal | No deal delete tool/agent operation was found in inspected implementation | UNSUPPORTED |
| A-01 | “How many deals are in each stage?” | Counts total all matching deals and each stage | FAIL (automated acceptance; IR-01) |
| A-02 | “Show deal amounts grouped by stage” | Correct per-stage sum/average as requested; currency safeguards | PASS (unit coverage); NOT TESTED (live) |
| A-03 | “Show deals grouped by pipeline” | One total metric per group; no duplicate aliases | FAIL (requester-observed duplicate; IR-02) |
| A-04 | “Show a chart of deal counts by stage” | Count chart, suitable type, no misleading amount axis | PASS (chart unit coverage); NOT TESTED (live) |
| A-05 | “Show a chart of deal value by stage” | Amount chart with currency/metric labeling | PASS (chart unit coverage); NOT TESTED (live) |
| A-06 | “Show a chart of deals by pipeline” | Readable group labels; only necessary labels/legend | FAIL (redundancy observed; chart defect not yet isolated) |
| A-07 | Query with no matching records | Clear empty result; no fabricated values/chart | PASS (unit coverage); NOT TESTED (live) |
| A-08 | Query across >100 deals / next page | Accurate aggregate across pages; listing indicates offset/page | PASS (fixture coverage). Interactive Next Page for CRM listings: UNSUPPORTED; ask for a numbered page |
| S-01 | Submit latest natural-language response in App Home | One readable answer; valid Block Kit limits and record links | FAIL (automated stage-count Home acceptance; other formatter cases pass) |
| S-02 | Open prior conversation preview | Short preview and View full response control for long output | PASS (history unit coverage); NOT TESTED (live) |
| S-03 | View full response, then Previous/Next | Correct saved response and page navigation | PASS (history unit coverage); NOT TESTED (live) |
| S-04 | Follow a HubSpot record link | Valid record URL and Slack link syntax; no duplicated link | PASS (link formatter tests); NOT TESTED (live portal) |
| S-05 | Use App Home controls (submit, confirm, history) | Correct action routing, owner/tenant scoping, proper control visibility | PASS (unit coverage); NOT TESTED (live Slack) |
| S-06 | Cause a safe read timeout/permission/invalid query | User-safe response; no secret/internal exception leak | PASS (unit coverage); NOT TESTED (live integrations) |

## Live manual test list

Run these in the existing Slack App Home only after confirming the test workspace has suitable records. Do not run C-06–C-08, CO-06–CO-08, or D-09–D-10 without separately approved disposable records. Deal deletion (D-11) was not found as a supported operation. Do not treat fixture results as live success.

1. Submit C-01 through C-05, then compare each row/count/field with HubSpot.
2. Submit CO-01 through CO-05 and verify company/contact associations against HubSpot.
3. Submit D-01 through D-08. For listings, compare pagination and totals with HubSpot; specifically record pipeline counts and totals.
4. Submit A-01 through A-08 and check exact counts, sums, labels, chart metric/type and empty/large-result behavior.
5. In the App Home, inspect the latest response, history preview, View full response, Previous/Next controls, links, controls, and safe error output.
6. For every answer, check: no duplicate AI answer; correct facts/counts/totals/labels; no duplicate metrics; no escaped Markdown, stray backslashes or HTML entities; valid HubSpot links; requested fields present; correct owner display; pagination; chart readability; and Slack Block Kit limits.

## Automated test results and issue register

| Test ID | Query / test | Expected | Actual | Status | Issue / severity | Suspected source | Proposed minimal fix |
|---|---|---|---|---|---|---|---|
| IR-01 | Ten signed Slack acceptance variants for deal counts by stage | App Home displays every expected stage count from all 130 fixture deals | Test reaches `crm_intelligence` result but expected stage labels/counts are absent from the published Home blocks | FAIL / High | Grouped stage result is lost or rendered incorrectly in App Home acceptance path | `tests/acceptance/test_stage1_acceptance.py`, `src/app/api/slack.py`, `src/app/integrations/slack/home.py`; isolate exact projection before patch | Capture the fixture response and rendered blocks at failure; fix only the faulty handoff/rendering branch; rerun acceptance suite |
| IR-02 | “Show me all deals grouped by pipeline” | One “Total amount” metric per pipeline | Requester observed total amount twice | FAIL / Medium | `_build_row` emits both `sum_amount` and `total_amount`; `_grouped_result_table` maps both to display columns; `_render_grouped_row` recognizes `sum_amount` aliases but not `total_amount` | `crm_intelligence.py` `_build_row`; `home.py` `_grouped_result_table` / `_render_grouped_row` | Omit the redundant alias from the grouped display table or include it in canonical metric deduplication. Preserve the response data contract unless consumers are audited. |
| IR-03 | “Show me all deals grouped by pipeline” | Clean pipeline label and no stray slash | Requester observed a backslash immediately after “Sales Pipeline”; no raw live response available | FAIL / Medium (reported) | Not attributable conclusively from local tests. Grouping copies `pipeline.label` directly; ordinary backslashes are not removed by prose normalization. Could be source label or text/rendering input. | `crm_intelligence.py` `_group_records`; `response_formatting.py` normalization; live HubSpot pipeline metadata | Compare the exact pipeline label, raw agent response text, and final Slack block text. Normalize only after identifying which layer introduces it; do not strip arbitrary user data. |
| IR-04 | Grouped pipeline chart/summary | Clear chart plus one concise data presentation | Requester observed redundant chart labels | FAIL / Low-Medium (reported) | Home chart path emits heading, first response-text line, image, optional chart summary, and grouped table. Redundancy likely comes from stacked representations; no confirmed defect in chart config itself. | `home.py` `_build_response_blocks` chart branch; `crm_intelligence.py` response summary; `quickchart.py` chart config | Inspect final block payload and image labels for the live query. If redundancy is in Home composition, remove only duplicate presentation there; modify chart generation only if payload inspection proves the chart config itself is wrong. |
| IR-05 | Full `pytest -q` | All automated tests pass | 844 passed, 31 failed, 1 skipped | FAIL / High | Broad regression baseline is not green; failures cover acceptance rendering, operation contract expectations, fixture interfaces, date text, OAuth scopes, and case normalization. | Failures listed below across `tests/acceptance` and `tests/unit`; changes already exist in the workspace | Triage each against the intended current contract and a clean known baseline before a consolidated patch; do not infer all failures are caused by this task. |
| IR-06 | Existing automated coverage for response formatting, history, associations, analytics and charts | Formatting limits, history navigation, CRM association/query behavior and chart payloads pass fixture checks | Most relevant formatter/history/analytics tests passed within 844 passing tests; live Slack and HubSpot remain untested | PASS (automated only) | Does not validate current deployed image or actual Slack rendering | Unit/acceptance fixtures | Execute numbered live list in the isolated Slack test workspace after issue fixes are reviewed. |

### Failed test breakdown (31)

- **10** parameterized signed Slack App Home stage-count acceptance cases in `tests/acceptance/test_stage1_acceptance.py`.
- **4** `tests/unit/test_account_intelligence.py` failures in safe failure handling and contact create/update/delete confirmation flows.
- **12** `tests/unit/test_crm_operations.py` failures around company/deal detail, association queries, old exact result/card shapes, multi-step output and failure behavior.
- **1** `tests/unit/test_crm_resolution.py` failure: fake `Contacts` lacks `list_contacts` used by resolution pagination.
- **1** `tests/unit/test_deal_analysis.py` failure: expected “Inactive days: 0” is missing from rendered analysis text.
- **2** `tests/unit/test_hubspot_oauth.py` failures: configured default scopes differ from the test’s expected list and HubSpot app manifest.
- **1** `tests/unit/test_natural_language_agent.py` failure: expected original company query casing (`ABC`) but resolver passes normalized lowercase (`abc`).

The full pytest output also reported pytest cache write-permission warnings under `.pytest_cache`; they did not prevent execution. One test was skipped (the isolated PostgreSQL history integration test requires its dedicated safe test database).

## Known pipeline issue: source assessment

- The requested pipeline grouping is implemented: `_extract_group_field` recognizes `grouped by pipeline`, `_group_records` resolves the pipeline ID to the HubSpot pipeline label, and `_build_row` creates count and amount values.
- **Duplicate amount is confirmed in source:** the aggregation row stores `sum_amount` and a compatibility alias `total_amount`. The Home table builder turns both into columns with separate labels, and the grouped row renderer only de-duplicates the `sum_amount`/`sum_amount_value` family. This matches the observed repeated total.
- **Backslash is not yet attributed:** the group label is copied directly from the pipeline metadata; the final text cleaner strips a backslash only at the very end of the whole answer. A slash after a label inside the answer would therefore point to the source label or an earlier response layer. The real pipeline label and raw response were not read in this read-only audit.
- **Redundant chart labels are not proven to be a QuickChart defect:** App Home can show a response heading/summary, the image, an optional chart summary and a grouped table. Verify the actual block payload and chart config before changing chart generation.

## Non-regression and limits

No live Slack query was submitted, so no successful live result is claimed. No CRM mutation was attempted. No Docker command, restart, database access, source edit, OAuth/session change, Slack authentication change, deployment, or network-port access was performed during this audit. Existing uncommitted workspace changes were preserved. The test command may have attempted pytest cache writes; those were denied by filesystem permissions and no tracked source was changed.

**Stop point:** This report contains the functionality inventory, practical/manual test matrix, automated test outcome, known-issue source assessment and consolidated issue register. No fixes have been implemented.

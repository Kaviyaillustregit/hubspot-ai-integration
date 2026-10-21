$ErrorActionPreference = "Stop"

python -m pytest -p no:cacheprovider `
    tests/unit/test_crm_intelligence.py `
    tests/unit/test_stage1_deal_intelligence.py `
    tests/unit/test_agent_crm_intelligence_integration.py

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

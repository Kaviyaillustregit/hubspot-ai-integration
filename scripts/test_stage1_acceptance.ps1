$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$env:PYTHONPATH = Join-Path $workspace "src"

python -m pytest -q -p no:cacheprovider `
    tests/acceptance/test_stage1_acceptance.py `
    --tb=short `
    --show-capture=no

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

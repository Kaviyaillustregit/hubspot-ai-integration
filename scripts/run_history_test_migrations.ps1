$ErrorActionPreference = "Stop"
if (-not $env:HISTORY_TEST_DATABASE_URL) {
    throw "Set HISTORY_TEST_DATABASE_URL for the isolated history test database."
}
$env:PYTHONPATH = Join-Path $PWD "src"
$env:HISTORY_TEST_MODE = "1"
python -c "import os; from app.db.history_safety import validate_history_test_database; validate_history_test_database(os.environ['HISTORY_TEST_DATABASE_URL'])"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

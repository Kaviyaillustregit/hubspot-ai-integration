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
python -m pytest tests/unit/test_slack_conversation_history.py tests/integration/test_slack_conversation_postgres.py tests/unit/test_slack_app_home.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m ruff check src/app/db/history_safety.py src/app/db/conversation_models.py src/app/db/models.py src/app/repositories/slack_conversation.py src/app/services/slack_conversation.py src/app/api/slack.py src/app/integrations/slack/home.py tests/unit/test_slack_conversation_history.py tests/integration/test_slack_conversation_postgres.py
exit $LASTEXITCODE

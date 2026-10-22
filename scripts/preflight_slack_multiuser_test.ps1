<#
.SYNOPSIS
    Read-only safety preflight for a future local Slack multi-user test run.
.DESCRIPTION
    Verifies the local application configuration and the existing disposable
    PostgreSQL container. It never starts the API, runs migrations, or changes
    Docker, Slack, HubSpot, or database state.
#>

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$requiredBranch = 'integration/session-crm'
$protectedBaseline = '34e6b11'
$databaseContainer = 'hubspot-auth-isolated-20261021'
$databaseName = 'hubspot_ai_history_test'
$databaseRole = 'hubspot_history_test'
$databaseHost = '127.0.0.1'
$databasePort = '52304'
$databaseRevision = '7a91c4d2e650'
$expectedTmpfsPath = '/var/lib/postgresql/data'
$expectedTmpfsOptions = 'rw,noexec,nosuid'

function Stop-Preflight([string] $Message) {
    throw "STOP: $Message"
}

function Assert-DatabaseTarget([string] $DatabaseUrl, [string] $HistoryDatabaseUrl) {
    $pattern = '^postgresql\+asyncpg://(?<user>[^:/@]+):(?<password>[^@]+)@(?<host>[^:/?#]+):(?<port>[0-9]+)/(?<database>[^/?#]+)$'
    $match = [regex]::Match($DatabaseUrl, $pattern)
    if (-not $match.Success -or $DatabaseUrl -cne $HistoryDatabaseUrl -or
        $match.Groups['user'].Value -cne $databaseRole -or
        $match.Groups['host'].Value -cne $databaseHost -or
        $match.Groups['port'].Value -cne $databasePort -or
        $match.Groups['database'].Value -cne $databaseName) {
        Stop-Preflight 'DATABASE_URL must target only the exact disposable PostgreSQL database.'
    }
}

function Assert-WorkspaceMapping([string] $MappingJson) {
    try {
        $mapping = ConvertFrom-Json -InputObject $MappingJson -ErrorAction Stop
    }
    catch {
        Stop-Preflight 'SLACK_TEAM_TENANT_MAP must be valid JSON.'
    }
    $entries = @($mapping.PSObject.Properties | Where-Object { $_.MemberType -eq 'NoteProperty' })
    if ($entries.Count -eq 0) {
        Stop-Preflight 'SLACK_TEAM_TENANT_MAP must contain at least one test workspace mapping.'
    }
    foreach ($entry in $entries) {
        if ([string]::IsNullOrWhiteSpace($entry.Name) -or
            $entry.Value -isnot [string] -or
            [string]::IsNullOrWhiteSpace($entry.Value)) {
            Stop-Preflight 'SLACK_TEAM_TENANT_MAP must map non-empty workspace IDs to tenant IDs.'
        }
    }
    return $entries.Count
}

function Assert-DatabaseContainer([object] $Container) {
    if (-not $Container.State.Running -or
        $Container.Config.Image -notmatch '(^|/)postgres:16-alpine$') {
        Stop-Preflight 'The named disposable database container is not running the expected PostgreSQL image.'
    }

    $containerEnv = @{}
    foreach ($entry in $Container.Config.Env) {
        $separator = $entry.IndexOf('=')
        if ($separator -gt 0) {
            $containerEnv[$entry.Substring(0, $separator)] = $entry.Substring($separator + 1)
        }
    }
    if ($containerEnv['POSTGRES_DB'] -cne $databaseName -or
        $containerEnv['POSTGRES_USER'] -cne $databaseRole) {
        Stop-Preflight 'The named container database identity is not the required disposable target.'
    }

    $tmpfs = $Container.HostConfig.Tmpfs
    $tmpfsPaths = @($tmpfs.PSObject.Properties.Name)
    $tmpfsPath = $tmpfs.PSObject.Properties[$expectedTmpfsPath]
    if ($tmpfsPaths.Count -ne 1 -or
        $tmpfsPaths[0] -cne $expectedTmpfsPath -or
        $null -eq $tmpfsPath -or
        [string]$tmpfsPath.Value -cne $expectedTmpfsOptions) {
        Stop-Preflight 'Database storage must be exactly the approved tmpfs path and options.'
    }
    if (@($Container.Mounts).Count -ne 0) {
        Stop-Preflight 'Persistent mounts and Docker volumes are not allowed for the test database.'
    }

    $networks = @($Container.NetworkSettings.Networks.PSObject.Properties.Name)
    if ($networks.Count -ne 1 -or $networks[0] -cne 'bridge') {
        Stop-Preflight 'The disposable database must remain on the default bridge network only.'
    }
}

Push-Location $repoRoot
try {
    $branch = (& git branch --show-current 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -cne $requiredBranch) {
        Stop-Preflight 'Required Git branch is not active.'
    }
    & git merge-base --is-ancestor $protectedBaseline HEAD 2>$null
    if ($LASTEXITCODE -ne 0) {
        Stop-Preflight 'The protected baseline is not an ancestor of the current HEAD.'
    }
    if (Test-Path -LiteralPath (Join-Path $repoRoot '.env')) {
        Stop-Preflight 'A local .env exists; remove it from this test workspace or use a clean isolated checkout.'
    }

    if ($env:APP_ENV -cne 'development' -or
        $env:HISTORY_TEST_MODE -cne '1' -or
        $env:HUBSPOT_PRODUCTION_MIGRATION_MODE -eq '1') {
        Stop-Preflight 'Development and isolated-test modes must be selected; production migration mode must be off.'
    }
    if ($env:SLACK_MULTIUSER_TEST_CONFIRMATION -cne 'separate-test-app-and-sandbox-workspace' -or
        $env:HUBSPOT_TEST_ACCOUNT_CONFIRMATION -cne 'developer-test-account-only') {
        Stop-Preflight 'Explicit confirmation is required that only a separate Slack test app and HubSpot developer test account are configured.'
    }
    if (-not $env:HISTORY_TEST_DATABASE_URL -or -not $env:DATABASE_URL) {
        Stop-Preflight 'The process must have both isolated database URL variables configured.'
    }
    Assert-DatabaseTarget $env:DATABASE_URL $env:HISTORY_TEST_DATABASE_URL

    $requiredSecrets = @(
        'SLACK_SIGNING_SECRET', 'SLACK_BOT_TOKEN',
        'HUBSPOT_CLIENT_ID', 'HUBSPOT_CLIENT_SECRET', 'HUBSPOT_TOKEN_ENCRYPTION_KEY'
    )
    foreach ($name in $requiredSecrets) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
            Stop-Preflight "Required process-scoped configuration is missing: $name."
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:HUBSPOT_REDIRECT_URI) -or
        $env:HUBSPOT_REDIRECT_URI -notmatch '^https?://(localhost|127\.0\.0\.1):8003/api/v1/auth/hubspot/callback$') {
        Stop-Preflight 'HUBSPOT_REDIRECT_URI must use the local port 8003 OAuth callback.'
    }
    $workspaceCount = Assert-WorkspaceMapping $env:SLACK_TEAM_TENANT_MAP

    $provider = if ([string]::IsNullOrWhiteSpace($env:AI_PROVIDER)) { 'anthropic' } else { $env:AI_PROVIDER.Trim().ToLowerInvariant() }
    $providerVariable = switch ($provider) {
        'anthropic' { 'ANTHROPIC_API_KEY'; break }
        'openrouter' { 'OPENROUTER_API_KEY'; break }
        'gemini' { 'GEMINI_API_KEY'; break }
        default { Stop-Preflight 'AI_PROVIDER must be anthropic, openrouter, or gemini.' }
    }
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($providerVariable, 'Process'))) {
        Stop-Preflight "The selected AI provider is missing required configuration: $providerVariable."
    }
    if (-not [string]::IsNullOrWhiteSpace($env:WEB_APP_BASE_URL)) {
        $baseUri = $null
        if (-not [uri]::TryCreate($env:WEB_APP_BASE_URL, [System.UriKind]::Absolute, [ref]$baseUri) -or
            $baseUri.Scheme -cne 'https' -or $baseUri.UserInfo -or $baseUri.Query -or $baseUri.Fragment) {
            Stop-Preflight 'WEB_APP_BASE_URL, when set, must be a credential-free HTTPS URL.'
        }
    }

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8003)
    try {
        $listener.Start()
    }
    catch {
        Stop-Preflight '127.0.0.1:8003 is not available.'
    }
    finally {
        if ($listener) { $listener.Stop() }
    }

    $dockerJson = & docker inspect $databaseContainer 2>$null
    $inspectExit = $LASTEXITCODE
    if ($inspectExit -ne 0 -or -not $dockerJson) {
        Stop-Preflight 'Docker cannot inspect the expected disposable database container.'
    }
    try {
        $container = (($dockerJson -join "`n") | ConvertFrom-Json -ErrorAction Stop)[0]
    }
    catch {
        Stop-Preflight 'Docker returned invalid metadata for the disposable database container.'
    }
    Assert-DatabaseContainer $container

    $publishedPorts = @(& docker port $databaseContainer '5432/tcp' 2>$null | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0 -or $publishedPorts.Count -ne 1 -or
        $publishedPorts[0] -cne ($databaseHost + ':' + $databasePort)) {
        Stop-Preflight 'The database must publish exactly 127.0.0.1:52304.'
    }

    $pythonCheck = @'
import asyncio
import json
import os
import sys
from sqlalchemy.engine import make_url

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from app.api.app import create_app
from app.api.slack import router as slack_router
from app.core.config import Settings
from app.core.secrets import SecretCipher
from app.db.history_safety import migration_database_url, validate_history_test_database
import asyncpg

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

target = os.environ.get("DATABASE_URL", "")
require(target == os.environ.get("HISTORY_TEST_DATABASE_URL"), "database variables differ")
require(os.environ.get("HISTORY_TEST_MODE") == "1", "test mode missing")
require(os.environ.get("HUBSPOT_PRODUCTION_MIGRATION_MODE") != "1", "production mode enabled")
validate_history_test_database(target)
require(migration_database_url(target) == target, "migration guard changed target")
url = make_url(target)
require((url.host, url.port, url.database, url.username) ==
        ("127.0.0.1", 52304, "hubspot_ai_history_test", "hubspot_history_test"),
        "database identity mismatch")
settings = Settings()
require(settings.app_env == "development" and settings.database_url == target,
        "application settings are not isolated")
require(all((settings.slack_signing_secret, settings.slack_bot_token,
             settings.hubspot_client_id, settings.hubspot_client_secret,
             settings.hubspot_token_encryption_key)), "required integration configuration missing")
SecretCipher(settings.hubspot_token_encryption_key)
mapping = json.loads(settings.slack_team_tenant_map or "{}")
require(isinstance(mapping, dict) and mapping and
        all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in mapping.items()),
        "workspace mapping invalid")
from app.integrations.slack.events import SlackTenantResolver
SlackTenantResolver(settings.slack_team_tenant_map)
route_paths = {route.path for route in slack_router.routes}
require({"/slack/events", "/slack/interactions"}.issubset(route_paths), "Slack routes missing")

required_tables = {
    "alembic_version", "hubspot_oauth_states", "hubspot_oauth_tokens",
    "pending_actions", "audit_logs", "idempotency_records",
    "slack_authorized_users", "slack_conversation_exchanges",
}

async def main():
    connection = None
    try:
        connection = await asyncpg.connect(
            host=url.host, port=url.port, database=url.database,
            user=url.username, password=url.password, timeout=5,
            server_settings={"default_transaction_read_only": "on"},
        )
        async with connection.transaction(readonly=True):
            identity = await connection.fetchrow("SELECT current_database(), current_user")
            require(tuple(identity) == ("hubspot_ai_history_test", "hubspot_history_test"),
                    "connected database identity mismatch")
            table_rows = await connection.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = {row["table_name"] for row in table_rows}
            missing = required_tables - tables
            require(not missing, "required application tables missing")
            revisions = await connection.fetch("SELECT version_num FROM alembic_version")
            require(len(revisions) == 1 and revisions[0]["version_num"] == "7a91c4d2e650",
                    "database migration head mismatch")
            tenant_ids = sorted(set(mapping.values()))
            oauth_rows = await connection.fetch(
                "SELECT DISTINCT tenant_id FROM hubspot_oauth_tokens "
                "WHERE tenant_id = ANY($1::text[])", tenant_ids
            )
            oauth_tenants = {row["tenant_id"] for row in oauth_rows}
            active_users = 0
            for workspace_id, tenant_id in mapping.items():
                active_users += await connection.fetchval(
                    "SELECT count(*) FROM slack_authorized_users "
                    "WHERE tenant_id = $1 AND slack_team_id = $2 AND active = true",
                    tenant_id, workspace_id,
                )
            print("Application imports/settings/routes: PASS")
            print("Database identity, read-only access, schema, and migration head: PASS")
            print("Mapped test workspaces: " + str(len(mapping)))
            print("HubSpot OAuth connections: " + str(len(oauth_tenants)) + "/" + str(len(tenant_ids)) + " mapped tenant(s)")
            print("Active Slack authorization records: " + str(active_users))
            if len(oauth_tenants) != len(tenant_ids):
                print("CRM query testing blocker: connect each mapped test tenant through the HubSpot OAuth flow.")
            if active_users < 2:
                print("Slack multi-user testing blocker: grant access to Users A and B in the disposable database.")
            if not settings.web_app_base_url:
                print("App Home banner image blocker: set WEB_APP_BASE_URL to a public HTTPS URL for the isolated app.")
    except Exception:
        raise RuntimeError("Read-only database/application preflight failed; diagnostic details suppressed") from None
    finally:
        if connection is not None:
            await connection.close()

asyncio.run(main())
'@
    $pythonOutput = $pythonCheck | & python - 2>&1 | Out-String
    $pythonExit = $LASTEXITCODE
    if ($pythonExit -ne 0) {
        Stop-Preflight 'Python import or read-only database preflight failed; diagnostic details were suppressed.'
    }
    Write-Output $pythonOutput.Trim()
    Write-Output "Required process configuration: PASS (values hidden; $workspaceCount workspace mapping(s))"
    Write-Output 'Port 8003 availability: PASS'
    Write-Output 'Disposable PostgreSQL container identity and tmpfs storage: PASS'
    Write-Output 'No migrations, server startup, Slack requests, HubSpot requests, or container changes were performed.'
}
finally {
    Pop-Location
}

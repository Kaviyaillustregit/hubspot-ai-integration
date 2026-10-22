# Run from the PowerShell session that owns $testPassword and the HISTORY_TEST_* variables:
#   Set-Location C:\hubspot-ai-baseline-test
#   . .\scripts\test_isolated_slack_authorization_postgres.ps1
# This script never stops, removes, restarts, or modifies the pre-existing DB container.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$containerName = "hubspot-auth-isolated-20261021"
$expectedDatabase = "hubspot_ai_history_test"
$expectedRole = "hubspot_history_test"
$expectedHost = "127.0.0.1"
$expectedPort = "52304"
$expectedHead = "7a91c4d2e650"
$expectedBranch = "integration/session-crm"
$expectedCommit = "34e6b117a6a0b1391a7bb64be21caff8aba49c67"

function Stop-Test([string] $Message) {
    throw "STOP: $Message"
}

Push-Location $repoRoot
try {
    $branch = (& git branch --show-current 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -cne $expectedBranch) {
        Stop-Test "Required Git branch is not active."
    }
    $commit = (& git rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -cne $expectedCommit) {
        Stop-Test "Protected baseline commit does not match."
    }

    if ($env:HISTORY_TEST_MODE -cne "1" -or $env:HUBSPOT_PRODUCTION_MIGRATION_MODE -eq "1") {
        Stop-Test "Isolated test mode is missing or production migration mode is enabled."
    }
    if (-not $env:HISTORY_TEST_DATABASE_URL -or
        $env:DATABASE_URL -cne $env:HISTORY_TEST_DATABASE_URL) {
        Stop-Test "DATABASE_URL and HISTORY_TEST_DATABASE_URL must be the same isolated URL."
    }
    $passwordVariable = Get-Variable -Name testPassword -ErrorAction SilentlyContinue
    if ($null -eq $passwordVariable -or [string]::IsNullOrEmpty([string]$passwordVariable.Value)) {
        Stop-Test 'The current PowerShell session does not contain `$testPassword.'
    }
    $testPassword = [string]$passwordVariable.Value
    $urlPattern = '^postgresql\+asyncpg://(?<user>[^:/@]+):(?<password>[^@]+)@(?<host>[^:/?#]+):(?<port>[0-9]+)/(?<database>[^/?#]+)$'
    $urlMatch = [regex]::Match($env:HISTORY_TEST_DATABASE_URL, $urlPattern)
    if (-not $urlMatch.Success -or
        $urlMatch.Groups['user'].Value -cne $expectedRole -or
        $urlMatch.Groups['host'].Value -cne $expectedHost -or
        $urlMatch.Groups['port'].Value -cne $expectedPort -or
        $urlMatch.Groups['database'].Value -cne $expectedDatabase -or
        [uri]::UnescapeDataString($urlMatch.Groups['password'].Value) -cne $testPassword) {
        Stop-Test "Database URL identity does not match the required disposable target."
    }

    $dockerJson = & docker inspect $containerName 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $dockerJson) {
        Stop-Test "Docker cannot inspect the expected temporary database container."
    }
    $container = (($dockerJson -join "`n") | ConvertFrom-Json)[0]
    if (-not $container.State.Running -or
        $container.Config.Image -notmatch '(^|/)postgres:16-alpine$') {
        Stop-Test "The named container is not running the expected PostgreSQL 16 image."
    }

    $containerEnv = @{}
    foreach ($entry in $container.Config.Env) {
        $separator = $entry.IndexOf('=')
        if ($separator -gt 0) {
            $containerEnv[$entry.Substring(0, $separator)] = $entry.Substring($separator + 1)
        }
    }
    if ($containerEnv['POSTGRES_DB'] -cne $expectedDatabase -or
        $containerEnv['POSTGRES_USER'] -cne $expectedRole -or
        $containerEnv['POSTGRES_PASSWORD'] -cne $testPassword) {
        Stop-Test "Container database identity or temporary credential does not match."
    }

$publishedPorts = @(
    & docker port $containerName '5432/tcp' 2>$null |
        ForEach-Object { ([string]$_).Trim() } |
        Where-Object { $_ }
)
$expectedBinding = $expectedHost + ':' + $expectedPort
if ($LASTEXITCODE -ne 0 -or
    $publishedPorts.Count -ne 1 -or
    $publishedPorts[0] -cne $expectedBinding) {
        Stop-Test "The PostgreSQL host binding is not exactly the expected loopback port."
    }

    $expectedTmpfsPath = '/var/lib/postgresql/data'
    $expectedTmpfsOptions = 'rw,noexec,nosuid'
    $tmpfsEntries = $container.HostConfig.Tmpfs
    $tmpfsPaths = @($tmpfsEntries.PSObject.Properties.Name)
    if ($tmpfsPaths.Count -ne 1 -or
        $tmpfsPaths[0] -cne $expectedTmpfsPath -or
        [string]$tmpfsEntries.PSObject.Properties[$expectedTmpfsPath].Value -cne $expectedTmpfsOptions) {
        Stop-Test "Database tmpfs configuration is not exactly the expected path and safe options."
    }
    $mounts = @($container.Mounts)
    if ($mounts.Count -ne 0) {
        Stop-Test "Unexpected persistent mounts or Docker volumes are attached to the database container."
    }
    $networkNames = @($container.NetworkSettings.Networks.PSObject.Properties.Name)
    if ($networkNames.Count -ne 1 -or $networkNames[0] -cne 'bridge') {
        Stop-Test "The database container is attached to an unexpected Docker network."
    }

    $readyOutput = & docker exec $containerName pg_isready -h 127.0.0.1 -p 5432 `
        -U $expectedRole -d $expectedDatabase 2>$null
    if ($LASTEXITCODE -ne 0) {
        Stop-Test "PostgreSQL readiness check failed."
    }
    Write-Output "Container identity, loopback binding, tmpfs storage, network, and pg_isready: PASS"

    $preflight = @'
import ast
import asyncio
import os
import sys
from pathlib import Path
from sqlalchemy.engine import make_url

root = Path.cwd()
sys.path.insert(0, str(root / "src"))
from app.db.history_safety import migration_database_url, validate_history_test_database

if sys.flags.optimize != 0:
    raise RuntimeError("Refusing to run with Python optimization enabled")

expected = {
    "0001_add_hubspot_oauth": None,
    "4dbdd102592b": "0001_add_hubspot_oauth",
    "e475126bdbc0": "4dbdd102592b",
    "3f812219a0d6": "e475126bdbc0",
    "6aa7c8eb17c4": "3f812219a0d6",
    "7a91c4d2e650": "6aa7c8eb17c4",
}
actual = {}
for path in (root / "migrations" / "versions").glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in {"revision", "down_revision"}:
                values[node.target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"revision", "down_revision"}:
                values[node.targets[0].id] = ast.literal_eval(node.value)
    if "revision" in values:
        actual[values["revision"]] = values.get("down_revision")
if actual != expected:
    raise RuntimeError("Alembic dependency graph differs from the reviewed linear chain")

if os.environ.get("HISTORY_TEST_MODE") != "1" or os.environ.get("HUBSPOT_PRODUCTION_MIGRATION_MODE") == "1":
    raise RuntimeError("Unsafe migration mode")
target = os.environ["HISTORY_TEST_DATABASE_URL"]
if os.environ.get("DATABASE_URL") != target:
    raise RuntimeError("Database URLs differ")
validate_history_test_database(target)
resolved = migration_database_url(os.environ["DATABASE_URL"])
url = make_url(resolved)
if resolved != target or url.host != "127.0.0.1" or url.port != 52304:
    raise RuntimeError("Migration guard resolved an unexpected target")

async def verify_database_identity():
    import asyncpg
    connection = await asyncpg.connect(
        host=url.host, port=url.port, database=url.database,
        user=url.username, password=url.password, timeout=5,
    )
    try:
        row = await connection.fetchrow("SELECT current_database(), current_user")
        if tuple(row) != ("hubspot_ai_history_test", "hubspot_history_test"):
            raise RuntimeError("Connected database identity mismatch")
    finally:
        await connection.close()

asyncio.run(verify_database_identity())
print("Migration graph, safety guard, connection target, database, and role: PASS")
'@
    $preflightOutput = $preflight | & python - 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Stop-Test "Database or migration preflight failed; migration was not attempted."
    }
    Write-Output $preflightOutput.Trim()

    $migrationOutput = ""
    $migrationExit = 1
    $previousPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = "Continue"
        $migrationOutput = & python -m alembic upgrade head 2>&1 | Out-String
        $migrationExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $migrationExit = $LASTEXITCODE
    if ($migrationExit -ne 0) {
        # Never emit a driver URL or any environment value in failure output.
        Write-Output "Alembic upgrade: FAIL (exit code $migrationExit; output suppressed to protect credentials)"
        Stop-Test "Alembic migration failed. No authorization test was run."
    }
    Write-Output "Alembic upgrade to head: PASS"

    $integrationTests = @'
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

root = Path.cwd()
sys.path.insert(0, str(root / "src"))
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.db.authorized_users import SlackAuthorizedUserRecord
from app.db.history_safety import validate_history_test_database
from app.services.slack_authorization_provisioning import ProvisioningResult, SlackAuthorizationProvisioner
from app.services.slack_authorized_users import SlackAuthorizationService

import os
database_url = os.environ["HISTORY_TEST_DATABASE_URL"]
validate_history_test_database(database_url)
run_id = uuid4().hex[:16]
tenant_a = "codex_authz_test_a_" + run_id
tenant_b = "codex_authz_test_b_" + run_id
workspace_a = "TTESTA" + run_id[:10]
workspace_b = "TTESTB" + run_id[:10]
user_a = "UTESTA" + run_id[:10]
user_b = "UTESTB" + run_id[:10]
engine = create_async_engine(database_url, pool_pre_ping=True)
factory = async_sessionmaker(engine, expire_on_commit=False)
provisioner = SlackAuthorizationProvisioner(factory)
authorization = SlackAuthorizationService(factory)
synthetic_keys = [
    (tenant_a, workspace_a, user_a),
    (tenant_a, workspace_a, user_b),
    (tenant_b, workspace_b, user_a),
]
created_keys = []

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def track_created(key, result):
    require(result is ProvisioningResult.GRANTED, "Expected a new grant for a unique synthetic key")
    if key not in created_keys:
        created_keys.append(key)

async def run():
    try:
        async with engine.connect() as connection:
            db, role = (await connection.exec_driver_sql("SELECT current_database(), current_user")).one()
            if (db, role) != ("hubspot_ai_history_test", "hubspot_history_test"):
                raise RuntimeError("Database identity changed after migration")
            table_exists = await connection.run_sync(
                lambda sync: inspect(sync).has_table("slack_authorized_users")
            )
            if not table_exists:
                raise RuntimeError("Authorization table was not created")
        print("Authorization table creation: PASS")

        async with factory() as session:
            for key in synthetic_keys:
                existing = await session.get(SlackAuthorizedUserRecord, key)
                if existing is not None:
                    raise RuntimeError("A synthetic authorization key already exists; refusing to overwrite it")
        print("Synthetic key collision check: PASS")

        result = await provisioner.grant_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a)
        track_created((tenant_a, workspace_a, user_a), result)
        result = await provisioner.grant_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b)
        track_created((tenant_a, workspace_a, user_b), result)
        require(await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a), "User A was not authorized after grant")
        require(await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b), "User B was not authorized after grant")
        print("Grant User A and User B in one tenant/workspace: PASS")

        require(await provisioner.grant_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a) is ProvisioningResult.ALREADY_ACTIVE, "Duplicate grant was not idempotent")
        require(await provisioner.revoke_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b) is ProvisioningResult.REVOKED, "User B revocation failed")
        require(await provisioner.revoke_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b) is ProvisioningResult.ALREADY_INACTIVE, "Duplicate revocation was not idempotent")
        require(await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a), "Revoking User B affected User A")
        require(not await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b), "Revoked User B remains authorized")
        print("Duplicate grant/revoke, revoke isolation, and remaining User A access: PASS")

        require(await provisioner.grant_access(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b) is ProvisioningResult.GRANTED, "Explicit re-grant of User B failed")
        require(await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b), "User B is not authorized after explicit re-grant")
        print("Explicit re-grant User B: PASS")

        require(not await authorization.is_authorized(tenant_id=tenant_b, slack_team_id=workspace_a, slack_user_id=user_a), "Tenant mismatch was authorized")
        require(not await authorization.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_b, slack_user_id=user_a), "Workspace mismatch was authorized")
        require(not await authorization.is_authorized(tenant_id=tenant_b, slack_team_id=workspace_b, slack_user_id=user_b), "User B leaked into another tenant/workspace")
        result = await provisioner.grant_access(tenant_id=tenant_b, slack_team_id=workspace_b, slack_user_id=user_a)
        track_created((tenant_b, workspace_b, user_a), result)
        require(await authorization.is_authorized(tenant_id=tenant_b, slack_team_id=workspace_b, slack_user_id=user_a), "Independent tenant/workspace grant failed")
        require(not await authorization.is_authorized(tenant_id=tenant_b, slack_team_id=workspace_a, slack_user_id=user_a), "Workspace mismatch was authorized after independent grant")
        print("Tenant/workspace identity isolation: PASS")

        try:
            async with factory() as session:
                async with session.begin():
                    session.add(SlackAuthorizedUserRecord(
                        tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a,
                        role="sales_agent", active=True,
                    ))
                    await session.flush()
        except IntegrityError:
            pass
        else:
            raise RuntimeError("Duplicate-key failure did not occur")
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(SlackAuthorizedUserRecord).where(
                SlackAuthorizedUserRecord.tenant_id == tenant_a,
                SlackAuthorizedUserRecord.slack_team_id == workspace_a,
                SlackAuthorizedUserRecord.slack_user_id == user_a,
            ))
            require(count == 1, "Failed transaction did not roll back cleanly")
        print("Transaction rollback after uniqueness failure: PASS")

        await engine.dispose()
        fresh_engine = create_async_engine(database_url, pool_pre_ping=True)
        fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
        fresh_auth = SlackAuthorizationService(fresh_factory)
        require(await fresh_auth.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_a), "User A did not persist across reconnect")
        require(await fresh_auth.is_authorized(tenant_id=tenant_a, slack_team_id=workspace_a, slack_user_id=user_b), "User B did not persist across reconnect")
        require(await fresh_auth.is_authorized(tenant_id=tenant_b, slack_team_id=workspace_b, slack_user_id=user_a), "Independent grant did not persist across reconnect")
        print("Authorization state persisted across engine reconnect: PASS")
        return fresh_engine
    except Exception:
        raise

async def main():
    fresh_engine = None
    try:
        fresh_engine = await run()
    finally:
        # Delete only exact keys that this run confirmed it created.
        cleanup_engine = fresh_engine or engine
        try:
            async with cleanup_engine.begin() as connection:
                for tenant_id, workspace_id, user_id in created_keys:
                    await connection.execute(delete(SlackAuthorizedUserRecord).where(
                        SlackAuthorizedUserRecord.tenant_id == tenant_id,
                        SlackAuthorizedUserRecord.slack_team_id == workspace_id,
                        SlackAuthorizedUserRecord.slack_user_id == user_id,
                    ))
        finally:
            await cleanup_engine.dispose()

asyncio.run(main())
print("Database authorization integration tests: PASS")
'@
    $integrationOutput = $integrationTests | & python - 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Stop-Test "PostgreSQL authorization integration testing failed; diagnostics were suppressed."
    }
    Write-Output $integrationOutput.Trim()

    Write-Output "All PostgreSQL authorization checks passed. Existing containers were not modified or removed."
}
finally {
    Pop-Location
}

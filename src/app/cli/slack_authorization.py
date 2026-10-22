"""Operator-controlled Slack authorization provisioning command."""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Sequence

from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.db.session import create_database
from app.integrations.slack.events import SlackTenantResolver
from app.services.slack_authorization_provisioning import (
    ProvisioningResult,
    SlackAuthorizationProvisioner,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grant or revoke one Slack user's access to a configured tenant."
    )
    parser.add_argument("operation", choices=("grant", "revoke"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--user-id", required=True)
    return parser


def _safe_database_target(database_url: str) -> str:
    """Render DB identity without password, query parameters, or full URL."""
    url = make_url(database_url)
    host = url.host or "(local/default)"
    port = f":{url.port}" if url.port is not None else ""
    user = url.username or "(default user)"
    database = url.database or "(default database)"
    return f"{user}@{host}{port}/{database}"


def _validated_args(argv: Sequence[str] | None, settings: Settings) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    try:
        tenant_id, workspace_id, user_id = (
            args.tenant_id,
            args.workspace_id,
            args.user_id,
        )
        # Reuse the same identity validation before any DB engine is created.
        from app.services.slack_authorization_provisioning import _validate_identity

        _validate_identity(tenant_id, workspace_id, user_id)
        _reject_duplicate_mapping_keys(settings.slack_team_tenant_map)
        mapped_tenant = SlackTenantResolver(settings.slack_team_tenant_map).resolve(workspace_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid identity or server-owned workspace mapping") from exc
    if mapped_tenant != tenant_id:
        raise ValueError("Workspace is not mapped to the specified tenant")
    return args


def _reject_duplicate_mapping_keys(mapping_json: str | None) -> None:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate workspace in server-owned tenant mapping")
            result[key] = value
        return result

    json.loads(mapping_json or "{}", object_pairs_hook=unique_pairs)


async def _execute(args: argparse.Namespace, settings: Settings) -> ProvisioningResult:
    engine, session_factory = create_database(settings)
    try:
        provisioner = SlackAuthorizationProvisioner(session_factory)
        operation = (
            provisioner.grant_access
            if args.operation == "grant"
            else provisioner.revoke_access
        )
        return await operation(
            tenant_id=args.tenant_id,
            slack_team_id=args.workspace_id,
            slack_user_id=args.user_id,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        settings = get_settings()
        args = _validated_args(argv, settings)
    except (ValueError, OSError) as exc:
        print(f"Denied: {exc}", file=sys.stderr)
        return 2

    identity = (
        f"{args.operation.upper()} tenant={args.tenant_id} "
        f"workspace={args.workspace_id} user={args.user_id}"
    )
    phrase = f"{args.operation.upper()} {args.tenant_id} {args.workspace_id} {args.user_id}"
    try:
        database_target = _safe_database_target(settings.database_url)
    except Exception:
        print("Denied: configured database target is invalid", file=sys.stderr)
        return 2
    print(f"Database target: {database_target}")
    print(f"Requested operation: {identity}")
    if not sys.stdin.isatty():
        print("Denied: interactive confirmation is required", file=sys.stderr)
        return 2
    try:
        confirmation = getpass.getpass(f"Type exactly '{phrase}' to continue: ")
    except (EOFError, OSError):
        print("Denied: interactive confirmation is required", file=sys.stderr)
        return 2
    if confirmation != phrase:
        print("Denied: confirmation did not match", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(_execute(args, settings))
    except Exception:
        # Do not print exception text; drivers may include connection details.
        print("Operation failed; transaction was rolled back where applicable", file=sys.stderr)
        return 1
    print(result.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

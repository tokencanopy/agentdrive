"""`python -m agentdrive.keys` — mint and manage a self-hosted install's API
keys (`AUTH_MODE=local`).

    init   [--workspace W] [--name N]   mint the workspace's owner subject (once)
    create --subject-type agent|user --name NAME [--workspace W]
           [--scopes all|a,b] [--expires 90d] [--role owner|admin|member]
           [--subject tcagt_…|tcusr_…]
    list   [--workspace W]              the keys this install issued, by id
    revoke ID                           refuse a key from the next request

A key is printed exactly once, on `create`; it is a password. It is one
credential for every surface — the `/v0` REST API, the SDKs and the MCP
transport all take the same `Authorization: Bearer adk_…`, because an
operator who minted a key on their own box IS the principal and there is no
delegated credential to keep apart (§4.2). Only its sha256 is stored, so a
lost key is reissued, never recovered.

**A key's scopes are fixed when it is created** (Josh, 2026-09-21; §8
decision 11). There is no command to widen or narrow one: changing what a
client may do is `revoke` plus `create`, which is also why a leaked key
cannot be escalated by whoever leaked it.

**Rotation keeps the identity.** `create` mints a NEW principal by default —
a new agent gets a new subject. To replace a leaked key, or to change a
client's scopes, pass `--subject` with the id `list` shows: the key is new,
the subject is the same, and every per-drive grant naming it survives. Minting
a fresh subject instead is what would quietly orphan the grants, and there
would be nothing to see afterwards but an agent that had lost its drives.

Subjects are recorded in `local_principals`; an agent key's sponsor is the
workspace owner minted by `init`, which is what lets an agent-created drive
grant its sponsor and gives an agent-only install a human principal that can
administer every drive.

Design: docs/superpowers/specs/2026-09-19-agentdrive-open-source-design.md §4.2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .identity.api_keys import (
    ALL_SCOPES,
    SUBJECT_PREFIX,
    display_id,
    generate_key,
    key_hash,
    new_subject,
)

DEFAULT_WORKSPACE = "default"
_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Ten years. Not a policy — keys do not expire by default — only a ceiling
# that keeps `--expires` a real timestamp rather than an overflow.
MAX_EXPIRES_SECONDS = 10 * 365 * 24 * 3600
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: Display ids are 48 bits of the key's own randomness, so a collision is a
#: curiosity rather than a risk; regenerating on one keeps `create` total.
_CREATE_ATTEMPTS = 5


def parse_expires(raw: str) -> int:
    m = _DURATION.match(raw.strip())
    if not m:
        raise ValueError("expiry must look like 30m, 12h or 90d")
    seconds = int(m.group(1)) * _UNITS[m.group(2)]
    if seconds <= 0:
        raise ValueError("expiry must be positive")
    if seconds > MAX_EXPIRES_SECONDS:
        raise ValueError("expiry must be at most 3650d")
    return seconds


def parse_workspace_id(raw: str) -> str:
    """A workspace id is a label the operator chose; blank or whitespace is
    not a workspace, and the key it would mint is unusable."""
    value = raw.strip()
    if not _WORKSPACE_ID.match(value):
        raise argparse.ArgumentTypeError(
            "workspace must be 1-128 letters, digits, '.', '_' or '-', starting "
            "with a letter or digit"
        )
    return value


def parse_scopes(raw: str) -> list[str]:
    """`all`, or a comma/space-separated subset of the vocabulary."""
    if raw.strip() == "all":
        return list(ALL_SCOPES)
    scopes = [s for s in raw.replace(",", " ").split() if s]
    unknown = sorted(set(scopes) - set(ALL_SCOPES))
    if unknown:
        raise ValueError(f"unknown scopes: {', '.join(unknown)}; known: {', '.join(ALL_SCOPES)}")
    if not scopes:
        raise ValueError("at least one scope is required")
    return list(dict.fromkeys(scopes))


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _settings():
    """Settings, constructed lazily, so the module imports without one."""
    from .config import settings

    return settings


def _require_local_mode() -> None:
    settings = _settings()
    if settings.auth_mode != "local":
        raise SystemExit(
            "this command mints local API keys and needs AUTH_MODE=local; under "
            f"AUTH_MODE={settings.auth_mode!r} Hub issues every credential"
        )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(_settings().database_url)


# ---- commands ---------------------------------------------------------------


async def _owner(conn: asyncpg.Connection, workspace_id: str) -> str | None:
    return await conn.fetchval(
        "SELECT subject FROM local_principals WHERE workspace_id = $1 "
        "AND principal_type = 'user' AND workspace_role = 'owner' ORDER BY created_at LIMIT 1",
        workspace_id,
    )


async def _init(workspace_id: str, name: str) -> dict:
    conn = await _connect()
    try:
        existing = await _owner(conn, workspace_id)
        if existing:
            return {"workspace_id": workspace_id, "owner": existing, "created": False}
        subject = new_subject("user")
        try:
            await conn.execute(
                "INSERT INTO local_principals "
                "(subject, principal_type, name, workspace_id, workspace_role) "
                "VALUES ($1, 'user', $2, $3, 'owner')",
                subject, name, workspace_id,
            )
        except asyncpg.UniqueViolationError:
            # A concurrent `init` won the one-owner index; report its owner.
            existing = await _owner(conn, workspace_id)
            return {"workspace_id": workspace_id, "owner": existing, "created": False}
        return {"workspace_id": workspace_id, "owner": subject, "created": True}
    finally:
        await conn.close()


def cmd_init(args: argparse.Namespace) -> int:
    _require_local_mode()
    print(json_dumps(asyncio.run(_init(args.workspace, args.name))))
    return 0


async def _existing_principal(
    conn: asyncpg.Connection, subject: str, workspace_id: str
) -> asyncpg.Record:
    """The principal `--subject` names, or a refusal.

    Checked against the workspace AND the requested type: the resolver joins
    a key to its principal on both columns and takes the role from the
    principal, so a key minted against a principal in another workspace or of
    another type would simply never resolve.
    """
    row = await conn.fetchrow(
        "SELECT subject, principal_type, workspace_id, workspace_role "
        "FROM local_principals WHERE subject = $1",
        subject,
    )
    if row is None:
        raise SystemExit(f"no principal {subject!r}; `list` shows the subjects this install has")
    if row["workspace_id"] != workspace_id:
        raise SystemExit(
            f"principal {subject!r} belongs to workspace {row['workspace_id']!r}, "
            f"not {workspace_id!r}"
        )
    return row


async def _create(args: argparse.Namespace) -> dict:
    scopes = parse_scopes(args.scopes)
    expires_in = parse_expires(args.expires) if args.expires else None
    role = getattr(args, "role", None)
    reuse_subject = (getattr(args, "subject", None) or "").strip()
    if args.subject_type == "agent" and role is not None:
        raise ValueError("--role applies to user keys; an agent has no workspace role")
    if reuse_subject and role is not None:
        raise ValueError(
            "--role and --subject are exclusive: an existing principal keeps the role "
            "it was created with, and this command never changes one"
        )
    if reuse_subject and not reuse_subject.startswith(SUBJECT_PREFIX[args.subject_type]):
        raise ValueError(
            f"--subject {reuse_subject!r} is not a {args.subject_type} subject "
            f"(expected {SUBJECT_PREFIX[args.subject_type]}…)"
        )
    if args.subject_type == "user" and role is None and not reuse_subject:
        role = "member"
    conn = await _connect()
    try:
        owner = await _owner(conn, args.workspace)
        if owner is None:
            raise SystemExit(
                f"workspace {args.workspace!r} has no owner yet; run "
                f"`python -m agentdrive.keys init --workspace {args.workspace}` first"
            )
        existing = None
        if reuse_subject:
            existing = await _existing_principal(conn, reuse_subject, args.workspace)
            role = existing["workspace_role"]
        reuse_owner = not reuse_subject and args.subject_type == "user" and role == "owner"
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
        )
        # Principal and key in ONE transaction, and the key is returned only
        # after it commits: a failure anywhere leaves no orphan principal and
        # no row for a key the caller never saw.
        mint_principal = not reuse_owner and existing is None
        for attempt in range(_CREATE_ATTEMPTS):
            if existing is not None:
                subject = existing["subject"]
            else:
                subject = owner if reuse_owner else new_subject(args.subject_type)
            key = generate_key()
            try:
                async with conn.transaction():
                    if mint_principal:
                        await conn.execute(
                            "INSERT INTO local_principals "
                            "(subject, principal_type, name, workspace_id, workspace_role) "
                            "VALUES ($1, $2, $3, $4, $5)",
                            subject, args.subject_type, args.name, args.workspace, role,
                        )
                    await conn.execute(
                        "INSERT INTO local_api_keys "
                        "(id, key_hash, subject, name, scopes, workspace_id, expires_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        display_id(key), key_hash(key), subject, args.name,
                        " ".join(scopes), args.workspace, expires_at,
                    )
                break
            except asyncpg.UniqueViolationError:
                if attempt == _CREATE_ATTEMPTS - 1:
                    raise
    finally:
        await conn.close()
    return {
        "key": key,
        "id": display_id(key),
        "subject": subject,
        "principal_type": args.subject_type,
        "workspace_id": args.workspace,
        "scopes": scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "reused_subject": existing is not None or reuse_owner,
        "note": "shown once; treat it as a password",
    }


def cmd_create(args: argparse.Namespace) -> int:
    _require_local_mode()
    try:
        print(json_dumps(asyncio.run(_create(args))))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


async def _list(workspace_id: str | None) -> list[dict]:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            # `key_hash` is deliberately not selected: `list` names keys, it
            # does not hand back material an operator could present.
            "SELECT k.id, k.subject, p.principal_type, k.name, k.scopes, "
            "k.workspace_id, k.expires_at, k.created_at, k.revoked_at FROM local_api_keys k "
            "LEFT JOIN local_principals p ON p.subject = k.subject "
            "WHERE $1::text IS NULL OR k.workspace_id = $1 ORDER BY k.created_at",
            workspace_id,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def cmd_list(args: argparse.Namespace) -> int:
    _require_local_mode()
    print(json_dumps(asyncio.run(_list(args.workspace))))
    return 0


async def _revoke(key_id: str) -> dict:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "UPDATE local_api_keys SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE id = $1 RETURNING id, revoked_at",
            key_id,
        )
        if row is None:
            # Truncated to the display id's own length. An operator who pastes
            # a whole `adk_…` key here would otherwise put the live secret in
            # their terminal and their shell history.
            shown = key_id[:12] + ("…" if len(key_id) > 12 else "")
            raise SystemExit(f"no API key with id {shown!r}")
        return dict(row)
    finally:
        await conn.close()


def cmd_revoke(args: argparse.Namespace) -> int:
    _require_local_mode()
    print(json_dumps(asyncio.run(_revoke(args.id))))
    return 0


# ---- entry ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agentdrive.keys", description=__doc__.split("\n\n")[0]
    )
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init", help="mint the workspace's owner subject")
    i.add_argument("--workspace", default=DEFAULT_WORKSPACE, type=parse_workspace_id)
    i.add_argument("--name", default="owner")
    i.set_defaults(func=cmd_init)
    c = sub.add_parser("create", help="mint an API key; printed once")
    c.add_argument("--subject-type", choices=("agent", "user"), required=True)
    c.add_argument("--name", required=True, help="a label, e.g. claude-code")
    c.add_argument("--workspace", default=DEFAULT_WORKSPACE, type=parse_workspace_id)
    c.add_argument("--scopes", default="all", help=f"'all' or a subset of: {', '.join(ALL_SCOPES)}")
    c.add_argument(
        "--expires", default=None,
        help="30m, 12h, 90d; at most 3650d. Omitted, the key does not expire.",
    )
    c.add_argument("--role", choices=("owner", "admin", "member"), default=None,
                   help="user keys only (default member); 'owner' reuses the init subject")
    c.add_argument(
        "--subject", default=None,
        help="mint a second key for an EXISTING principal (rotation): its grants survive",
    )
    c.set_defaults(func=cmd_create)
    ls = sub.add_parser("list", help="the keys this install issued")
    ls.add_argument("--workspace", default=None, type=parse_workspace_id)
    ls.set_defaults(func=cmd_list)
    r = sub.add_parser("revoke", help="refuse a key from the next request")
    r.add_argument("id", help="the display id `create`/`list` shows, e.g. adk_k7Qm2xZp")
    r.set_defaults(func=cmd_revoke)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

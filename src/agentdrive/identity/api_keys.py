"""Opaque API keys: the self-hosted install's one credential.

`AUTH_MODE=local` (2026-09-19 open-source design §4.2, as amended
2026-09-21) gives an installation with no Hub a credential of its own. It is
not a token: there is nothing to decode, nothing to rotate and no key
material anywhere. `adk_` plus 240 bits of randomness is presented as a
bearer, hashed, and looked up; the row it finds — joined to its
`local_principals` principal — builds exactly the `V0ActorContext` a
Hub-issued JWT yields, so authorization, sponsor grants and the schema
CHECKs below the boundary run unchanged.

WHY NOT A JWT. #723 shipped a local issuer. Binding every request to the
issued-token row (so a leaked signing key could not mint an escalated token)
made local verification stateful anyway, which left the JWT with its cost —
key files, `/jwks`, rotation, an audience split — and none of its benefit.
A row lookup either way, so the credential became the row's key.

ONE KEY, EVERY SURFACE. The hosted 2026-08-28 audience split exists because
an MCP session token is obtained through an OAuth consent flow with its own
resource; an operator who mints a key on their own box IS the principal, so
there is no delegated credential to keep apart. A key therefore works for
`/v0`, the SDKs and the MCP transport alike, and local mode has no `api`
versus `mcp` distinction. The hosted split is untouched: nothing here runs
under `AUTH_MODE=hub`.

WHAT THE DATABASE HOLDS. The key's sha256, and its DISPLAY ID — which is
`adk_` plus the key's first 8 secret characters (§4.2). So the stored row is
not entirely free of the secret: 48 of its 240 bits are the operator-visible
name of the key, by design, because an operator has to be able to say WHICH
key to revoke. The other 192 bits exist only inside the hash, and they are
what a bearer has to present.

SCOPES ARE IMMUTABLE. There is deliberately no `UPDATE` of `scopes`
anywhere in this module or the CLI (Josh, 2026-09-21; §8 decision 11), so a
leaked key cannot be widened by anyone, including its operator. Changing
what a client may do is `revoke` plus `create`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from .actor import V0ActorContext
from .product_token import V0_SCOPES

#: The whole credential is `PREFIX` + `SECRET_CHARS` base64url characters.
#: 30 random bytes encode to exactly 40 characters with no padding, so every
#: key is the same length and the shape is checkable without a lookup.
KEY_PREFIX = "adk_"
KEY_SECRET_BYTES = 30
KEY_SECRET_CHARS = 40
#: The display id is the prefix plus the key's first 8 secret characters:
#: enough to name one key among an operator's handful, far too little to
#: guess the other 32. `list` shows it; `revoke` takes it.
DISPLAY_ID_CHARS = 8

#: The namespaces the schema CHECKs pin (§8 decision 3: kept on self-hosted
#: installs, so no schema change is needed for a standalone tree).
SUBJECT_PREFIX = {"agent": "tcagt_", "user": "tcusr_"}

#: Every `/v0` scope this deployment enforces; `--scopes all` expands to it.
#: The verifier's vocabulary is the one source; this is only an ordering.
ALL_SCOPES = tuple(sorted(V0_SCOPES))


def generate_key() -> str:
    """A fresh key. Printed once, by `create`, and never stored."""
    raw = secrets.token_bytes(KEY_SECRET_BYTES)
    secret = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return KEY_PREFIX + secret


def key_hash(key: str) -> str:
    """Hex sha256 of the WHOLE key, which is what the database matches on.

    A plain digest, not a slow KDF: the secret is 240 bits of `secrets`
    randomness with no structure to guess, so there is no dictionary for a
    work factor to defend against — and a per-request KDF would be a
    self-inflicted denial of service on the auth path.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_id(key: str) -> str:
    """The key's id: prefix plus its first 8 secret characters."""
    if not looks_like_key(key):
        raise ValueError("not an AgentDrive API key")
    return KEY_PREFIX + key[len(KEY_PREFIX) : len(KEY_PREFIX) + DISPLAY_ID_CHARS]


def looks_like_key(value: str) -> bool:
    """Whether a presented bearer is shaped like one of our keys.

    Cheap and non-secret: it decides which resolution PATH a bearer takes,
    never whether it is valid. A wrong answer here costs a 401 either way.
    """
    return value.startswith(KEY_PREFIX) and len(value) == len(KEY_PREFIX) + KEY_SECRET_CHARS


#: The row and its principal in one statement. The owner subject is looked up
#: in the same round trip because an agent's actor context needs it as
#: `sponsor_id` — that is what lets an agent-created drive mint its sponsor
#: grant (`core/v0_drives.py`) and gives an agent-only install a human
#: principal that can administer every drive.
#:
#: The join carries `p.workspace_id = k.workspace_id` as well as the subject.
#: The KEY row's workspace is what the actor acts in, while the PRINCIPAL row
#: supplies `principal_type` and `workspace_role`, and nothing in the schema
#: holds the two columns equal. Without this predicate a row whose workspaces
#: disagree would build an actor acting in workspace B with a principal that
#: belongs to A, sponsored by B's owner. The CLI cannot write that row; a
#: later writer (the §4.11 OIDC console, a restore, a support script) could,
#: and with it a mismatch is a 401 instead of a confused actor.
_RESOLVE_SQL = """
SELECT k.id,
       k.key_hash,
       k.subject,
       k.scopes,
       k.workspace_id,
       k.expires_at,
       k.revoked_at,
       p.principal_type,
       p.workspace_role,
       (SELECT o.subject
          FROM local_principals o
         WHERE o.workspace_id = k.workspace_id
           AND o.principal_type = 'user'
           AND o.workspace_role = 'owner'
         ORDER BY o.created_at
         LIMIT 1) AS owner_subject
  FROM local_api_keys k
  JOIN local_principals p
    ON p.subject = k.subject
   AND p.workspace_id = k.workspace_id
 WHERE k.key_hash = $1
"""


async def resolve(connection: Any, key: str) -> V0ActorContext | None:
    """The actor a presented key authenticates, or None.

    None means "refuse this bearer" — malformed, unknown, revoked, expired,
    or an agent key in a workspace with no owner to sponsor it. A database
    failure is NOT swallowed: it propagates, and the boundary answers 503
    AUTH_UNAVAILABLE rather than blaming the caller's key (§6).
    """
    if not looks_like_key(key):
        return None
    presented = key_hash(key)
    row = await connection.fetchrow(_RESOLVE_SQL, presented)
    if row is None:
        return None
    # Postgres already matched on equality, so this cannot fail. It is here so
    # the byte comparison a credential check rests on is spelled once, in this
    # module, in constant time — not delegated wholly to a column's collation.
    if not hmac.compare_digest(row["key_hash"], presented):
        return None
    if row["revoked_at"] is not None:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(UTC):
        return None
    principal_type = row["principal_type"]
    if principal_type == "agent" and not row["owner_subject"]:
        # A Hub-issued agent token always carries a sponsor. Handing the
        # routes an agent actor without one would silently drop the sponsor
        # grant on every drive it creates, so refuse instead: `init` mints
        # the owner, and this state means it was deleted afterwards.
        return None
    return V0ActorContext.from_local_key(
        subject=row["subject"],
        principal_type=principal_type,
        workspace_id=row["workspace_id"],
        # Intersected with the vocabulary, exactly as `from_claims` does with a
        # Hub token's `scope` claim: a scope string this deployment does not
        # enforce authorizes nothing, and carrying it into the actor would hand
        # a future scope consumer something `from_claims` never could.
        scopes=frozenset(row["scopes"].split()) & V0_SCOPES,
        workspace_role=row["workspace_role"],
        sponsor_id=row["owner_subject"] if principal_type == "agent" else None,
        key_id=row["id"],
    )


def new_subject(principal_type: str) -> str:
    """A fresh `tcagt_…` / `tcusr_…` subject for a local principal."""
    return SUBJECT_PREFIX[principal_type] + secrets.token_hex(8)

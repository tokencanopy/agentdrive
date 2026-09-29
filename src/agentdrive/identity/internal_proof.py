"""The per-boot proof that gates the private same-container MCP ingress.

WHAT THIS IS NOT. It is not authorization. It authenticates nothing about a
caller, carries no identity, and grants no scope. The internal ingress still
verifies the caller's Hub-issued MCP JWT and still intersects its scopes with
live local grants — exactly as the public `/v0` boundary does. Removing the
proof would not by itself let anyone read a byte.

WHAT IT IS FOR. The ingress binds loopback inside one container so the MCP
sidecar can reach AgentDrive without presenting an MCP-audience token to the
public `/v0` API, which rejects that audience by design. The proof makes the
port indistinguishable from a closed route to anything in the container that
was not handed it at boot: the process supervisor generates it fresh per boot
and puts it in the environment of the MCP and ingress children only.

The comparison is constant-time. A timing oracle on a per-boot value is a
narrow attack, but the correct comparison costs nothing and the wrong one is
the kind of detail that survives into a place where it matters.
"""

from __future__ import annotations

import hmac

#: Header the MCP sidecar sends. Lowercase because ASGI normalizes headers,
#: and matched case-insensitively by Starlette's header mapping regardless.
INTERNAL_PROOF_HEADER = "x-agentdrive-internal-proof"

#: `secrets.token_urlsafe(32)` — 256 bits — base64url-encodes to exactly 43
#: characters. The supervisor generates that; anything shorter reaching this
#: module is a placeholder, a truncation, or a hand-set value, none of which
#: may configure a boundary.
MIN_PROOF_LENGTH = 43


class InvalidInternalProof(ValueError):
    """The configured proof is unusable, so the ingress must not start."""


def require_configured_proof(value: str | None) -> str:
    """Return a usable proof or refuse to start.

    Fails closed on absence rather than defaulting to an empty string that
    would then match an empty header.
    """
    proof = (value or "").strip()
    if len(proof) < MIN_PROOF_LENGTH:
        raise InvalidInternalProof(
            f"the internal ingress proof must be at least {MIN_PROOF_LENGTH} characters"
        )
    return proof


def proof_matches(presented: str | None, expected: str) -> bool:
    """Constant-time compare of a presented header against the boot proof.

    The ASCII guard is not cosmetic: `hmac.compare_digest` RAISES TypeError on
    a str holding a code point above U+00FF, and a header is caller-controlled
    bytes decoded as latin-1. Without it a request carrying a non-ASCII proof
    header turns a 404 — "there is nothing here" — into a 500, which is both
    an unhandled error on an auth path and a signal that something IS here.
    A proof is 43 base64url characters, so nothing legitimate is refused.
    """
    if not presented or not expected:
        return False
    if not presented.isascii() or not expected.isascii():
        return False
    return hmac.compare_digest(presented, expected)

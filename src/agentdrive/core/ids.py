"""Public identifier minting and shape validation.

Public identifiers are opaque, stable, non-enumerable strings (§4.1). Every
one carries its prefix, and the prefix is load-bearing: the route convertors
discriminate on it, so an id whose shape does not match its resource is a bug
that must not reach a row. The same regexes are CHECK constraints in
`schema.sql` — if a shape ever changes, both move together or writes start
failing at the database instead of at the boundary.

Sixteen hex characters is 64 bits from `secrets.token_bytes`, which is the
non-enumerable part: ids are never sequential and never derived from a
timestamp, a row count, or anything else a caller could walk.

`rev_*` is deliberately in the same family. §4.1 says a revision is not a
transaction id, timestamp, GCS generation, or encoded PostgreSQL state —
making it the same opaque shape as everything else is what stops one of those
leaking into an `ETag` later.
"""

from __future__ import annotations

import hashlib
import re
import secrets

# Prefixes claimed but not yet issued in v0. Upload sessions and jobs are
# minted today (`upld_*`, `job_*`), so the container is empty; keep it so a
# later layer claims a prefix here rather than picking a name that could
# collide with an id already in the wild.
_RESERVED_UNISSUED: tuple[str, ...] = ()

# (prefix, what it identifies). Kept as one table so `new_id`, the validator,
# and the schema CHECKs cannot drift apart silently.
PREFIXES: dict[str, str] = {
    "drv": "drive",
    "fld": "folder",
    "art": "artifact",
    "ver": "artifact version",
    "grn": "grant",
    "shr": "share link",
    "idem": "idempotency record",
    "chg": "change",
    "cset": "change set",
    "rev": "revision",
    "upld": "upload session",
    "rsv": "storage reservation",
    "job": "async job",
    "vwr": "viewer session",
    "shs": "sheet edit session",
}

# `cur_*` is deliberately NOT here. Every prefix above names a row, minted at
# random and stored. A cursor stores nothing: D14 puts the client's position
# inside the token itself, so there is no per-client subscription state and
# no table to point at. It is a sealed, self-describing token minted by
# `core/cursors.py`, not a random handle -- and `new_id("cur")` would be a
# 16-hex string carrying no position at all.

_HEX_LEN = 16

_PATTERNS: dict[str, re.Pattern[str]] = {
    prefix: re.compile(rf"^{prefix}_[a-f0-9]{{{_HEX_LEN}}}$") for prefix in PREFIXES
}


class InvalidId(ValueError):
    """An id whose shape does not match the resource it claims to name."""


def new_id(prefix: str) -> str:
    """Mint an opaque id for `prefix`.

    Raises on an unknown prefix rather than minting something the schema will
    reject at INSERT time -- a typo here should fail where it is written, not
    three layers down in a constraint violation.
    """
    if prefix not in PREFIXES:
        raise InvalidId(
            f"unknown id prefix {prefix!r}; "
            f"known prefixes: {', '.join(sorted(PREFIXES))}"
        )
    return f"{prefix}_{secrets.token_bytes(_HEX_LEN // 2).hex()}"


def is_valid(value: object, prefix: str) -> bool:
    """True when `value` is a well-formed id of `prefix`'s resource."""
    if prefix not in _PATTERNS:
        raise InvalidId(f"unknown id prefix {prefix!r}")
    return isinstance(value, str) and bool(_PATTERNS[prefix].match(value))


def require(value: object, prefix: str) -> str:
    """Return `value` if it is a well-formed `prefix` id, else raise.

    The boundary helper: use it where a caller-supplied id first enters, so
    every layer below can assume shape without re-checking.
    """
    if not is_valid(value, prefix):
        raise InvalidId(
            f"expected a {PREFIXES[prefix]} id of the form "
            f"{prefix}_<{_HEX_LEN} hex>, got {value!r}"
        )
    return str(value)


def prefix_of(value: str) -> str | None:
    """The prefix `value` is a well-formed id for, or None.

    Used where a reference is discriminated by its own shape rather than by a
    sibling `type` field -- a share or grant target, for instance.
    """
    for prefix in PREFIXES:
        if is_valid(value, prefix):
            return prefix
    return None


def hash_key(key: str) -> str:
    """SHA-256 of a secret, hex-encoded.

    Two callers, both storing a fingerprint rather than the secret:
    `shares.secret_hash` (§6.9 -- the link secret is shown once at creation
    or rotation and never again) and the rate limiter's bucket key, which
    hashes so slowapi's in-memory dict never holds a raw bearer.
    """
    return hashlib.sha256(key.encode()).hexdigest()

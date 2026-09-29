"""The closed-set rule for reading a version's bytes.

A version row records WHERE its bytes live: ``storage_bucket`` is NULL for
pre-B3 inline CAS content in the artifact bucket, the artifact bucket
itself once the coordinates have been backfilled, or the DEDICATED
direct-transfer bucket for a B3 direct upload (with ``storage_generation``
pinning the exact immutable object). Every read surface — the machine API,
the private viewer, the public renderer, the share resolver, and the copy
path's preview read — resolves coordinates through this one helper so the
boundary cannot drift per caller:

  * the artifact bucket and the CONFIGURED transfer bucket are the complete
    set of places a committed version may point (mirrors the download
    mint's namespace rule, packet 4);
  * each bucket has ONE legitimate namespace — ``cas/`` in the artifact
    bucket, the configured immutable prefix in the transfer bucket. A
    scratch object is browser-written and is never readable, and an unset
    immutable prefix means no transfer-bucket read is legitimate at all,
    rather than every one of them;
  * the persisted object name must be a real member of that namespace, by
    the same rule the packet-4 signer applies (no bare prefix, no empty or
    dot segments, no traversal, no percent/backslash ambiguity, no control
    characters) — a row that violates it is corruption, and this is the
    last fail-closed boundary over persisted coordinates before a fetch;
  * anything else is unreadable, full stop: the caller answers its uniform
    not-found, never a fetch to an arbitrary bucket.

Deliberately independent of ``direct_transfer_enabled``: disabling the
transfer surface (rollback) must not strand already-published versions.
That independence is exactly why the rules above cannot lean on the
config-completeness check, which returns early while the flag is false.

**Generations are pinned only where they carry meaning.** A transfer-bucket
object lives at a server-chosen but mutable NAME, so the generation is what
makes the read immutable, and it is required. Artifact-bucket content is
content-addressed — the digest IS the identity — so pinning there would buy
nothing and would turn a benign CAS re-write (the documented GC-race
refresh in ``storage.put``) into a hard read failure for older rows.
"""

from __future__ import annotations

import re
from typing import Any

from .. import storage
from ..config import settings
from ..storage import CAS_PREFIX

# Control characters and the separators that make one persisted name mean
# two different objects. Mirrors `storage_transfers._OBJECT_NAME_FORBIDDEN`.
_OBJECT_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")


def _is_member_of(object_name: Any, prefix: str) -> bool:
    """Whether ``object_name`` is a real member of ``prefix``'s namespace."""
    if not isinstance(object_name, str) or not prefix:
        return False
    segments = object_name.split("/")
    return not (
        not object_name.startswith(prefix)
        or len(object_name) <= len(prefix)
        or "" in segments
        or "." in segments
        or ".." in segments
        or "%" in object_name
        or "\\" in object_name
        or _OBJECT_NAME_FORBIDDEN.search(object_name)
    )


def read_coordinates(row: Any) -> tuple[str | None, int | None] | None:
    """``(bucket_override, generation)`` for a version row's byte read, or
    ``None`` when the recorded coordinates are outside the closed set. A
    ``None`` bucket_override means the default artifact bucket."""
    bucket = row["storage_bucket"]

    # Pre-B3 rows carry no coordinates at all. Their reads are unchanged:
    # default bucket, latest generation — exactly the behavior every one of
    # these surfaces had before coordinates existed.
    if bucket is None:
        return (None, None)

    if bucket == storage.store_id():
        if not _is_member_of(row["storage_object"], CAS_PREFIX):
            return None
        return (None, None)

    if (
        settings.direct_transfer_bucket
        and bucket == settings.direct_transfer_bucket
        and _is_member_of(
            row["storage_object"], settings.direct_transfer_immutable_prefix
        )
    ):
        generation = row["storage_generation"]
        if not isinstance(generation, int) or isinstance(generation, bool):
            return None
        if generation <= 0:
            return None
        return (bucket, generation)

    return None

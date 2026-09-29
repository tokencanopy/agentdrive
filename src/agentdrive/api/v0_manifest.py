"""The v0 operation catalog and environment-active manifest.

The original 46-op inventory was cut to 39 for the v0 launch: the uploads
(4) and jobs (3) verticals were removed — large/resumable file uploads and
the cross-drive copy job path were out of scope for the launch. The
viewer-session mint made 40, and the B3 direct-transfer amendment
(2026-08-14 design §1) restored the four upload-session controls as packet
3 (44) and added the download-capability mint as packet 4 (45). D13 adds two
unified navigation reads while retaining the typed collection reads for
compatibility, bringing the count to 47. The wire
inventory in ``v0-operations.json`` — package data, shipped alongside this
module by every distribution mechanism (wheel, editable install, ``COPY
src``) — is the source of truth. The catalog has 59 operations; production's
fail-closed default removes the eight ``sheet_sessions``-gated operations,
leaving 51 active operations. Staging enables all 59.

Schema version 3 adds the explicit, closed ``idempotency_class`` field
(B3 §1): ``required`` | ``not_required`` | ``forbidden``, never inferred
from the HTTP method or ``precondition_class``. The loader fails closed on
a missing or unrecognized value.

Schema version 4 does the same for ``if_match_class``: ``required`` |
``optional`` | ``conditional`` | ``forbidden``. This is separate from the
broader precondition family because sheet-session creation is creation-shaped
yet requires If-Match, while completion enforces a captured revision without
accepting the header.

Schema version 5 adds the optional, closed ``feature`` field. The only current
value is ``sheet_sessions``; unknown gates fail catalog loading instead of
silently publishing an operation.

Package data on purpose: the manifest used to live at the repo root
(``api/v0-target-operations.json``), outside the package, and the container
image shipped without it — every deployed instance 500'd its first
/openapi.json. A data file a package reads belongs INSIDE the package,
loaded via ``importlib.resources``, so forgetting to ship it is
unrepresentable rather than merely tested for.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from agentdrive.feature_settings import feature_settings

_MANIFEST = files("agentdrive.api").joinpath("v0-operations.json")

IDEMPOTENCY_CLASSES = frozenset({"required", "not_required", "forbidden"})
IF_MATCH_CLASSES = frozenset({"required", "optional", "conditional", "forbidden"})
FEATURES = frozenset({"sheet_sessions"})


def _load() -> list[dict[str, Any]]:
    data = json.loads(_MANIFEST.read_text())
    ops = data["operations"]
    # 39 launch operations + viewer_sessions_create (2026-08-09
    # private-viewer design) + the four B3 upload-session controls
    # (packet 3) + the B3 download-capability mint (2026-08-14
    # direct-transfer design, packet 4). D13 adds entries_list and lookup;
    # the sheet edit-session design (2026-08-22 §1) adds twelve more. The
    # private-beta policy classifies all 59 operations as beta.
    assert len(ops) == 59, f"manifest is not 59 operations: {len(ops)}"
    assert len({(o["method"], o["path"]) for o in ops}) == 59, "duplicate method/path"
    for o in ops:
        # Fail closed: an operation without an explicit, recognized
        # idempotency class must never load (B3 §1).
        assert o.get("idempotency_class") in IDEMPOTENCY_CLASSES, (
            f"{o.get('operation_id')} has no closed idempotency_class"
        )
        assert o.get("if_match_class") in IF_MATCH_CLASSES, (
            f"{o.get('operation_id')} has no closed if_match_class"
        )
        assert o.get("feature") is None or o["feature"] in FEATURES, (
            f"{o.get('operation_id')} has an unknown feature gate"
        )
    return sorted(ops, key=lambda o: (o["domain"], o["method"], o["path"]))


catalog_operations: list[dict[str, Any]] = _load()
CATALOG_OPERATION_COUNT = len(catalog_operations)


def operations_for(*, sheet_sessions_enabled: bool) -> list[dict[str, Any]]:
    enabled = {"sheet_sessions"} if sheet_sessions_enabled else set()
    return [
        operation
        for operation in catalog_operations
        if operation.get("feature") is None or operation["feature"] in enabled
    ]


# Canonical public names describe the contract active in this process. Tools
# that validate the closed superset import ``catalog_operations`` explicitly.
operations: list[dict[str, Any]] = operations_for(
    sheet_sessions_enabled=feature_settings.sheet_sessions_enabled
)
OPERATION_COUNT = len(operations)


def op_ids() -> set[str]:
    return {o["operation_id"] for o in operations}

"""Shared harness for the conformance suites that drive the real surface.

Two suites need the same three things: an actor with an arbitrary scope set, a
drive containing one real resource of every addressable kind, and a request
per operation that reaches the handler rather than dying in validation. That
last part is most of the work — real ids so the route dependency resolves, a
real `If-Match` from the seeded revisions, no `Idempotency-Key` on the one
operation that forbids one — and it is not worth building twice.

Kept deliberately free of assertions: what to assert about a response belongs
to the suite asking the question, not here.
"""

from __future__ import annotations

import inspect
from typing import Any

from agentdrive.api.v0_manifest import operations as MANIFEST_OPERATIONS
from agentdrive.app import app as _app
from agentdrive.identity.actor import V0ActorContext

ALL_SCOPES = frozenset(
    {
        "drives:read",
        "drives:write",
        "content:read",
        "content:write",
        "changes:read",
        "sharing:read",
        "sharing:write",
        "usage:read",
    }
)

AGENT = "tcagt_0000000000000001"
HUMAN = "tcusr_0000000000000003"
SPONSOR = "tcusr_0000000000000009"
WS = "tcws_0000000000000001"

SUBJECT_TYPES = ("agent", "user")

OPERATIONS = {op["operation_id"]: op for op in MANIFEST_OPERATIONS}


def _state_filter_operations() -> frozenset[str]:
    """Every operation whose handler takes a `state` parameter.

    DERIVED, not listed. An earlier revision hand-listed three of the five and
    left `grants_list` and `shares_list` uncovered — where the disclosure is
    worst, since a scopeless token could read the drive's whole access roster
    through `?state=all`. Enumerating instances of a bug class is how the
    class survives; asking the app which handlers have the branch is how the
    next one gets covered for free.

    The parameter was called `lifecycle` on five of these until the v0 filter
    rename; deriving on the name means the set follows the app, so the rename
    also pulled in `entries_list` and `sheet_sessions_list`, which had the
    branch all along and no variant coverage.
    """
    found = set()
    for route in _app.routes:
        op_id = getattr(route, "operation_id", None)
        endpoint = getattr(route, "endpoint", None)
        if not op_id or endpoint is None or not getattr(route, "include_in_schema", True):
            continue
        if "state" in inspect.signature(endpoint).parameters:
            found.add(op_id)
    return frozenset(found)


STATE_FILTER_OPS = _state_filter_operations()

# `all` is the wildcard every v0 COLLECTION filter takes. Sheet sessions spell
# their own states (`open`/`completed`/`discarded`/`expired`) and have no
# wildcard, so that one op gets a value its handler accepts. The SET stays
# derived — only the value is per-operation.
_VARIANT_VALUE = {"sheet_sessions_list": "open"}
REQUEST_VARIANTS: dict[str, dict[str, str]] = {
    op_id: {"state": _VARIANT_VALUE.get(op_id, "all")} for op_id in STATE_FILTER_OPS
}

# `If-Match` sources per operation family. Without these, eleven mutations stop
# at `428 PRECONDITION_REQUIRED` a few statements past the scope check, so the
# allow-side assertion proves far less than it appears to.
REVISION_FOR: dict[str, str] = {
    "drives": "drive_revision",
    "folders": "folder_revision",
    "artifacts": "artifact_revision",
    # A version's precondition is carried on its ARTIFACT.
    "versions": "artifact_revision",
    "grants": "grant_revision",
    "shares": "share_revision",
    # A sheet session's own revision does not exist in the seed (the id is
    # deliberately absent), and the lookup 404s before the precondition is
    # evaluated — any well-formed value gets past the scope gate.
    "sheet": "artifact_revision",
}



def make_actor(scopes: frozenset[str], subject_type: str = "agent") -> V0ActorContext:
    is_agent = subject_type == "agent"
    return V0ActorContext(
        subject=AGENT if is_agent else HUMAN,
        subject_type=subject_type,
        workspace_id=WS,
        membership_id="tcagm_0000000000000001",
        token_id="tctok_0000000000000001",
        scopes=frozenset(scopes),
        credential_id="tccred_0000000000000001" if is_agent else None,
        runtime_id="tcrun_0000000000000001" if is_agent else None,
        sponsor_id=SPONSOR if is_agent else None,
        workspace_role=None if is_agent else "member",
    )



async def seed_resources(http, set_actor, subject_type: str) -> dict[str, str]:
    """One real resource of every addressable kind, created with full scopes.

    Real ids matter: most routes resolve local capability in a route dependency
    that runs BEFORE the handler body, so a fictional id would render 404
    as-if-absent and never reach what the caller wants to test. The creator
    holds manager on what it creates, which keeps that dependency satisfied
    while the token's scopes are varied underneath it.
    """
    set_actor(ALL_SCOPES, subject_type)
    made: dict[str, str] = {}

    r = await http.post(
        "/v0/drives",
        json={"name": "scope-matrix"},
        headers={"Idempotency-Key": "k-drive"},
    )
    assert r.status_code == 201, r.text
    made["drive_id"] = r.json()["id"]
    made["drive_revision"] = r.json()["revision"]
    made["root_folder_id"] = r.json()["root_folder_id"]
    d = made["drive_id"]

    r = await http.post(
        f"/v0/drives/{d}/folders",
        json={"parent_id": made["root_folder_id"], "name": "folder"},
        headers={"Idempotency-Key": "k-folder"},
    )
    assert r.status_code == 201, r.text
    made["folder_id"] = r.json()["id"]
    made["folder_revision"] = r.json()["revision"]

    r = await http.post(
        f"/v0/drives/{d}/artifacts",
        files={"content": ("note.md", b"# hello\n", "text/markdown")},
        data={"name": "note.md", "parent_id": made["root_folder_id"]},
        headers={"Idempotency-Key": "k-artifact"},
    )
    assert r.status_code == 201, r.text
    made["artifact_id"] = r.json()["id"]
    made["artifact_revision"] = r.json()["revision"]
    a = made["artifact_id"]

    r = await http.get(f"/v0/drives/{d}/artifacts/{a}/versions")
    assert r.status_code == 200, r.text
    made["version_id"] = r.json()["items"][0]["id"]

    r = await http.post(
        f"/v0/drives/{d}/grants",
        json={
            "principal_type": "user",
            "principal_id": "tcusr_0000000000000077",
            "resource_type": "drive",
            "resource_id": d,
            "role": "viewer",
        },
        headers={"Idempotency-Key": "k-grant"},
    )
    assert r.status_code == 201, r.text
    made["grant_id"] = r.json()["id"]
    made["grant_revision"] = r.json()["revision"]

    r = await http.post(
        f"/v0/drives/{d}/shares",
        json={"resource_type": "artifact", "resource_id": a},
        headers={"Idempotency-Key": "k-share"},
    )
    assert r.status_code == 201, r.text
    made["share_id"] = r.json()["id"]
    made["share_revision"] = r.json()["revision"]

    # Well-formed but absent: the upload controls validate the `upld` prefix
    # before looking the session up, so a wrong prefix stops at 400 and never
    # proves the gate opened.
    made["upload_id"] = "upld_00000000000000ff"
    # Same idiom for sheet sessions: the scope gate runs before the session
    # lookup, so a well-formed-but-absent id still proves the gate opened.
    made["session_id"] = "shs_00000000000000ff"
    return made




def build_request(
    op_id: str, r: dict[str, str], *, variant: bool = False
) -> tuple[str, str, dict[str, Any]]:
    """Build a request that reaches the operation's scope check.

    It need not SUCCEED: `Idempotency-Key` and `If-Match` are validated inside
    the handler, after the scope check, so a missing precondition surfaces as
    400/428 — which still proves the gate opened. What it must do is satisfy
    FastAPI's own body and query validation, since that runs first; a 422 means
    the request never reached the handler and the case proved nothing, which is
    why every allow-side assertion rejects 422 explicitly.
    """
    op = OPERATIONS[op_id]
    path = (
        op["path"]
        .replace("{drive_id}", r["drive_id"])
        .replace("{folder_id}", r["folder_id"])
        .replace("{artifact_id}", r["artifact_id"])
        .replace("{version_id}", r["version_id"])
        .replace("{grant_id}", r["grant_id"])
        .replace("{share_id}", r["share_id"])
        .replace("{upload_id}", r["upload_id"])
        .replace("{session_id}", r["session_id"])
    )
    headers: dict[str, str] = {}
    # `download_capabilities_create` is the manifest's only `forbidden` class:
    # sending a key there is a 400 six statements into the handler, so an
    # undeclared scope demanded after that point would be invisible.
    if op["idempotency_class"] == "required":
        headers["Idempotency-Key"] = f"k-{op_id}"
    if op["if_match_class"] == "required":
        family = op_id.split("_", 1)[0]
        revision = r.get(REVISION_FOR.get(family, ""))
        if revision:
            headers["If-Match"] = f'"{revision}"'
    kwargs: dict[str, Any] = {"headers": headers}
    params: dict[str, str] = {}
    if op_id == "drive_search":
        params["q"] = "hello"
    if op_id == "changes_list":
        # Without this the feed answers 400 "pass exactly one of start or cursor".
        params["start"] = "beginning"
    if op_id == "entries_list":
        params["parent_id"] = r["root_folder_id"]
    if op_id == "lookup":
        params["path"] = "note.md"
    if variant:
        params.update(REQUEST_VARIANTS.get(op_id, {}))
    if params:
        kwargs["params"] = params

    bodies: dict[str, dict[str, Any]] = {
        "drives_create": {"name": "another"},
        "drives_update": {"name": "renamed"},
        "folders_create": {"parent_id": r["root_folder_id"], "name": "child"},
        "folders_update": {"name": "renamed"},
        "folders_copy": {
            "destination_parent_id": r["root_folder_id"],
            "destination_name": "folder-copy",
        },
        "artifacts_update": {"name": "renamed.md"},
        "artifacts_copy": {
            "destination_parent_id": r["root_folder_id"],
            "destination_name": "note-copy.md",
        },
        "grants_create": {
            "principal_type": "user",
            "principal_id": "tcusr_0000000000000078",
            "resource_type": "drive",
            "resource_id": r["drive_id"],
            "role": "viewer",
        },
        "grants_update": {"role": "editor"},
        "shares_create": {"resource_type": "artifact", "resource_id": r["artifact_id"]},
        # Route-dependency idiom: its scope is enforced before body validation,
        # but an absent body would still make the allow side a 422 and prove
        # nothing — and would misreport the operation as unenforced the moment
        # it is refactored onto the in-handler idiom.
        "viewer_sessions_create": {},
        "download_capabilities_create": {"artifact_id": r["artifact_id"]},
        "sheet_sessions_create": {},
        "sheet_sessions_complete": {},
        "sheet_sessions_write_cells": {
            "writes": [{"sheet": "Q3", "range": "A1", "values": [["x"]]}]
        },
        "uploads_create": {
            "target": {
                "kind": "artifact",
                "parent_folder_id": r["root_folder_id"],
                "name": "big.bin",
            },
            "content": {
                "size_bytes": 1024,
                "media_type": "application/octet-stream",
                "checksum": {"algorithm": "crc32c", "value": "AAAAAA=="},
            },
        },
    }
    if op_id in bodies:
        kwargs["json"] = bodies[op_id]
    if op_id in {"artifacts_create", "versions_append"}:
        kwargs["files"] = {"content": ("v.md", b"# next\n", "text/markdown")}
        kwargs["data"] = {"name": "v.md"}
        if op_id == "artifacts_create":
            kwargs["data"]["parent_id"] = r["root_folder_id"]
    return op["method"].lower(), path, kwargs

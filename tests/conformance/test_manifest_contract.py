"""The manifest is exactly the accepted surface (§5, §11.2; B3 §1)."""

from agentdrive.api.v0_manifest import (
    CATALOG_OPERATION_COUNT,
    IDEMPOTENCY_CLASSES,
    IF_MATCH_CLASSES,
    OPERATION_COUNT,
    catalog_operations,
    operations,
    operations_for,
)


def test_manifest_has_exactly_59_operations():
    # The 39 launch operations plus viewer_sessions_create plus the
    # four B3 direct-upload session controls (packet 3) plus the packet 4
    # download-capability mint plus the two D13 navigation reads, plus the
    # twelve sheet operations (2026-08-22 sheet edit-session design). The typed
    # collection reads remain public for compatibility. All 59 are beta during
    # private beta.
    assert OPERATION_COUNT == 59
    assert CATALOG_OPERATION_COUNT == 59


def test_sheet_sessions_are_a_closed_fail_off_capability():
    production = operations_for(sheet_sessions_enabled=False)
    staging = operations_for(sheet_sessions_enabled=True)

    assert len(production) == 51
    assert len(staging) == 59
    assert {o["operation_id"] for o in catalog_operations} == {
        o["operation_id"] for o in staging
    }
    assert not any(o["domain"] == "sheet-sessions" for o in production)
    assert {
        o["operation_id"]
        for o in catalog_operations
        if o.get("feature") == "sheet_sessions"
    } == {
        o["operation_id"]
        for o in catalog_operations
        if o["domain"] == "sheet-sessions"
    }
    assert {o["operation_id"] for o in production if o["domain"] == "sheets"} == {
        "sheets_list",
        "sheet_cells_read",
        "version_sheets_list",
        "version_cells_read",
    }


def test_manifest_has_no_duplicate_method_path():
    pairs = [(o["method"], o["path"]) for o in operations]
    assert len(pairs) == len(set(pairs))


def test_manifest_is_drive_scoped():
    for o in operations:
        assert "/v0/drives" in o["path"], o


def test_manifest_domain_counts():
    from collections import Counter

    counts = Counter(o["domain"] for o in operations)
    assert counts == {
        "drives": 7, "folders": 7, "artifacts": 8, "versions": 5,
        "search": 1, "changes": 1, "grants": 5, "shares": 5,
        "viewer-sessions": 1, "uploads": 4, "downloads": 1,
        "sheets": 4, "sheet-sessions": 8,
        "navigation": 2,
    }


def test_every_operation_has_a_closed_idempotency_class():
    """B3 §1: the manifest declares idempotency explicitly, never inferred
    from HTTP method or precondition_class. Closed values only."""
    assert frozenset({"required", "not_required", "forbidden"}) == IDEMPOTENCY_CLASSES
    for o in operations:
        assert o["idempotency_class"] in IDEMPOTENCY_CLASSES, o["operation_id"]


def test_every_operation_has_a_closed_if_match_class():
    assert frozenset({"required", "optional", "conditional", "forbidden"}) == IF_MATCH_CLASSES
    for o in operations:
        assert o["if_match_class"] in IF_MATCH_CLASSES, o["operation_id"]

    by_id = {o["operation_id"]: o for o in operations}
    assert by_id["sheet_sessions_create"]["if_match_class"] == "required"
    assert by_id["sheet_sessions_create"]["expected_statuses"] == [200, 201]
    assert by_id["sheet_sessions_complete"]["if_match_class"] == "forbidden"
    assert by_id["uploads_create"]["if_match_class"] == "conditional"
    assert by_id["folders_copy"]["if_match_class"] == "optional"


def test_baseline_idempotency_classes():
    """Every mutation requires a key; every read is not_required. The one
    `forbidden` operation is packet 4's download mint (B3 §5.7): a supplied
    key is 400 INVALID_REQUEST and no idempotency record may hold a signed
    target."""
    for o in operations:
        if o["operation_id"] == "download_capabilities_create":
            assert o["idempotency_class"] == "forbidden"
        elif o["precondition_class"] == "read":
            assert o["idempotency_class"] == "not_required", o["operation_id"]
        else:
            assert o["idempotency_class"] == "required", o["operation_id"]


def test_transfer_operations_are_exactly_the_b3_five():
    uploads = {o["operation_id"] for o in operations if o["domain"] == "uploads"}
    assert uploads == {
        "uploads_create", "uploads_read", "uploads_delete", "uploads_complete",
    }
    downloads = {
        o["operation_id"] for o in operations if o["domain"] == "downloads"
    }
    assert downloads == {"download_capabilities_create"}


def test_manifest_prose_matches_the_shipped_facts():
    """Should-fix: the wrapper prose must state the CURRENT schema version
    and operation count — stale numbers in the served inventory are contract
    drift."""
    import json
    from importlib.resources import files

    data = json.loads(
        files("agentdrive.api").joinpath("v0-operations.json").read_text()
    )
    assert data["schema_version"] == 5
    description = data["description"]
    assert "59" in description
    assert "schema_version 5" in description
    assert "SHEET_SESSIONS_ENABLED" in description
    assert "idempotency_class" in description
    assert "if_match_class" in description
    assert "schema_version 2" not in description
    assert "44 operations total" not in description


def test_dockerfile_manifest_assertion_matches_the_loader():
    """Round-4 blocker: the Dockerfile's build-time manifest tripwire must
    assert the SAME count the loader enforces — a stale literal breaks every
    image build after the surface grows (40 → 44 → 45)."""
    import re
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent.parent / "Dockerfile").read_text()
    matched = re.search(r"assert len\(catalog_operations\) == (\d+)", dockerfile)
    assert matched, "the Dockerfile manifest assertion is missing"
    assert int(matched.group(1)) == CATALOG_OPERATION_COUNT, (
        f"Dockerfile asserts {matched.group(1)} operations but the manifest "
        f"catalog loads {CATALOG_OPERATION_COUNT}"
    )

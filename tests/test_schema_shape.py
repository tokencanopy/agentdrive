"""The day-0 schema is exactly what the 39 operations reach.

These are static assertions over `schema.sql` itself, deliberately separate
from `test_schema_applies.py` (which proves the file executes) and from the
invariant tests (which prove the constraints bite at runtime). A regex over
the text catches the class of mistake those cannot: a column that gets added
back because it was convenient, with no operation behind it.

Every assertion cites the contract clause it holds, so a future reader can
tell a ratified decision from an implementation habit. Contract:
docs/superpowers/specs/2026-07-30-agentdrive-v0-api-contract-design.md.
"""

from __future__ import annotations

import pathlib
import re

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

# §3.1 splits ownership: Hub holds principals, workspaces, memberships,
# product entitlement and credentials; AgentDrive holds drives, namespaces,
# versions/bytes, local grants/shares, search/usage, and the change feed.
EXPECTED_TABLES = {
    "drives",
    "folders",
    "artifacts",
    "artifact_versions",
    "grants",
    "shares",
    "idempotency_records",
    "drive_changes",
    "drive_change_heads",
    "v0_uploads",
    "v0_jobs",
    "v0_job_object_refs",
    # 0046 — the private viewer's hashed short-lived credentials
    # (2026-08-09 private-viewer design).
    "viewer_sessions",
    # 0049 — B3 direct-transfer sessions, the promised-byte reservation
    # ledger, and the workspace accounting row (2026-08-14 direct-transfer
    # design §6/§9). Additive; the dormant v0_uploads table is not promoted.
    "upload_sessions",
    "storage_reservations",
    "workspace_storage",
    # 0052 — sheet edit sessions: one row per session plus an append-only
    # edit log (2026-08-22 sheet edit-session design §6). The base grid is
    # deliberately NOT a table — it is derivable from an immutable version.
    "sheet_sessions",
    "sheet_session_edits",
    # 0058 — the direct-transfer rate windows, moved out of process memory
    # so the configured limits hold across instances. In-memory counters
    # were exact only at `api_max_instances = 1`, which is the pin that
    # left the API tier with no redundancy.
    "transfer_rate_windows",
    "usage_windows",
    "usage_operations",
    # 0063/0064 -- the self-hosted install's credentials (2026-09-19
    # open-source design §4.2, amended 2026-09-21 to opaque API keys). Under
    # AUTH_MODE=local there is no Hub, so the install records the principals
    # it minted and the keys it issued: `list` shows the operator every
    # credential and the key itself is stored only as a hash. 0064 dropped
    # `local_tokens`, the retired issuer's ledger. Hub mode never reads either
    # table -- §3.1's ownership split holds for every hosted deployment.
    "local_principals",
    "local_api_keys",
}


def _text() -> str:
    return SCHEMA.read_text()


def _tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)", _text()))


def _table_block(table: str) -> str:
    """The column list of one CREATE TABLE, for per-table assertions."""
    m = re.search(
        rf"CREATE TABLE(?: IF NOT EXISTS)? {table} \((.*?)\n\);",
        _text(),
        re.S,
    )
    assert m, f"{table} not found in schema.sql"
    return m.group(1)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_exactly_the_expected_tables():
    assert _tables() == EXPECTED_TABLES


def test_usage_metering_has_closed_atomic_shape():
    windows = _table_block("usage_windows")
    operations = _table_block("usage_operations")
    assert "PRIMARY KEY (metric, scope_type, scope_id, period, window_start)" in windows
    assert "CHECK (used >= 0)" in windows
    assert "CHECK (reserved >= 0)" in windows
    assert "UNIQUE (operation_key, metric)" in operations
    assert "'commit_reserved'" in operations
    assert "(state = 'reserved') = (finalized_at IS NULL)" in operations


def test_v0_upload_job_substrate_tables_exist():
    text = _text()
    for table in ("v0_uploads", "v0_jobs", "v0_job_object_refs"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(", text), table


def test_local_api_keys_stores_a_hash_and_an_optional_expiry():
    """The opaque-key row (§4.2 as amended). The key never lands in the
    database: only its sha256 does, uniquely. `expires_at` is nullable —
    keys do not expire unless `--expires` was given — and `scopes` cannot be
    empty, because a key with no scopes authorizes nothing and would be a
    silently useless credential."""
    block = _table_block("local_api_keys")
    assert re.search(r"key_hash\s+text\s+NOT NULL UNIQUE", block)
    assert "REFERENCES local_principals (subject)" in block
    assert re.search(r"expires_at\s+timestamptz,", block), "an expiry must be optional"
    assert "CHECK (length(btrim(scopes)) > 0)" in block
    # The id and the hash carry their wire shapes, as every other id namespace
    # in this schema does.
    assert "id ~ '^adk_[A-Za-z0-9_-]{8}$'" in block
    assert "key_hash ~ '^[0-9a-f]{64}$'" in block
    # The retired issuer's ledger is gone from the fresh baseline (0064);
    # only the comment recording why it went remains.
    assert "local_tokens" not in _tables()


def test_v0_job_state_is_check_constrained():
    block = _table_block("v0_jobs")
    assert re.search(r"state\s+TEXT NOT NULL DEFAULT 'queued'.*CHECK \(state IN", block, re.S), (
        "v0_jobs.state must be a closed CHECK set"
    )


def test_no_control_plane_state():
    """Hub owns principals, workspaces, memberships and entitlement (§3.1).

    A tier, quota, or billing column in the data plane is a contract
    violation, not a convenience -- it is how the two planes grow a second,
    disagreeing copy of the same fact.
    """
    text = _text().lower()
    for banned in (
        "tier_id",
        "quota_override",
        "billing_status",
        "stripe_",
        "seat_count",
        "workos_",
        "hub_workspace_id",
    ):
        assert banned not in text, f"control-plane column in data plane: {banned}"


def test_no_materialized_path_column():
    """Path is derived from the parent_id chain, never stored (§4.2).

    A stored path is a denormalization that must be rewritten for every
    descendant on a rename or move -- the operation that made the legacy
    surface's rename O(subtree) and its concurrency untestable.

    Scoped to the tree tables on purpose. `idempotency_records.path` is an
    HTTP request path, which §7.2 requires storing to tell a replay from an
    IDEMPOTENCY_CONFLICT; banning the column name everywhere would be
    string-matching rather than asserting the invariant.
    """
    for table in ("folders", "artifacts"):
        assert not re.search(r"^\s+path\s+", _table_block(table), re.M), (
            f"{table} has a stored path column"
        )


def test_no_per_client_cursor_table():
    """D14: the reader carries its position; nothing server-side stores it.

    A per-client cursor table is what makes a change feed at-most-once --
    the server can record a position it never successfully delivered.
    """
    assert "change_cursors" not in _text()


def test_parent_id_is_not_null():
    """The tree is authoritative, so its edges are mandatory (§4.2).

    Today's schema has this inverted -- `path` is NOT NULL and `parent_id`
    is nullable -- because the v0 tree was added beside the legacy path
    router. Folders are the one exception: the drive's structural root has
    no parent, which `folders_one_root` pins to exactly one row per drive.
    """
    assert re.search(r"parent_id\s+TEXT\s+NOT NULL", _table_block("artifacts")), (
        "artifacts.parent_id must be NOT NULL"
    )


# ---------------------------------------------------------------------------
# §12A invariants this layer owns. Each row of the contract's table names an
# enforcement layer; these are the ones whose layer is "DB constraint" or
# "DB trigger", i.e. the ones that must exist in this file rather than in a
# handler. The two rows owned by "App transaction" -- one manager per drive
# (Layer 3) and at-least-once replay (Layer 7) -- are deliberately absent.
# ---------------------------------------------------------------------------


def test_shared_namespace_is_a_unique_index():
    """One name per (parent, kind-agnostic) sibling set (§6.2).

    Postgres has no cross-table unique index, so this is half the guarantee:
    each table is pinned here, and the folder-vs-artifact exclusion is held
    by the Layer 5 mutation transaction plus an invariant test.
    """
    text = _text()
    for table in ("folders", "artifacts"):
        assert re.search(
            rf"CREATE UNIQUE INDEX (?:IF NOT EXISTS )?\w+ ON {table} \(parent_id, name\)"
            rf"\s*WHERE deleted_at IS NULL",
            text,
        ), f"{table} is missing its (parent_id, name) namespace index"


def test_one_root_folder_per_drive():
    """The structural root is the only parent-less folder in a drive (§6.2)."""
    assert re.search(
        r"CREATE UNIQUE INDEX (?:IF NOT EXISTS )?\w+ ON folders \(drive_id\)"
        r"\s*WHERE parent_id IS NULL",
        _text(),
    ), "folders is missing its one-root-per-drive index"


def test_search_tsv_is_generated():
    """Search reflects current state (§6.6, blocker B3).

    A trigger-maintained or handler-maintained tsvector goes stale the first
    time a writer forgets. GENERATED ... STORED cannot: Postgres recomputes
    it on every write, including writes from a future code path nobody has
    written yet.
    """
    block = _table_block("artifacts")
    assert re.search(
        r"search_tsv\s+tsvector\s+GENERATED ALWAYS AS", block
    ), "artifacts.search_tsv must be a GENERATED column"
    assert "STORED" in block, "search_tsv must be STORED"


def test_search_tsv_arms_are_input_bounded():
    """Every search_tsv arm truncates its input (blocker B3).

    The tsvector is capped at 1 MiB but `metadata` is unbounded JSONB and
    the v0 inline-body ceiling is 20 MiB, so an arm fed raw values turns a
    legal-sized write into "string is too long for tsvector" at the storage
    layer. Each arm must `left(...)` its input; a future edit that drops a
    bound silently re-opens the write-blocker.
    """
    block = _table_block("artifacts")
    expr = block.split("GENERATED ALWAYS", 1)[1].split("STORED", 1)[0]
    assert "left(regexp_replace(name" in expr, "name arm is not bounded"
    assert "left(coalesce(content_preview" in expr, (
        "content_preview arm is not bounded"
    )
    assert "left(coalesce(metadata::text" in expr, "metadata arm is not bounded"
    assert "left(labels_text(labels)" in expr, "labels arm is not bounded"


def test_versions_are_immutable():
    """Versions are immutable (§6.4) -- held by a trigger, not convention.

    A CHECK cannot express "no UPDATE ever"; a trigger binds the table owner
    too, which is the point. Without it, immutability is a property of the
    handlers that happen to exist today.
    """
    text = _text()
    assert re.search(r"CREATE (?:OR REPLACE )?FUNCTION \w*version\w*", text, re.I), (
        "no version-immutability trigger function"
    )
    assert re.search(
        r"CREATE TRIGGER \w+\s+BEFORE UPDATE ON artifact_versions", text, re.I
    ), "artifact_versions is missing its reject-update trigger"


def test_byte_counters_are_non_negative():
    """Byte counters never go negative (§6.1).

    A negative counter is unreachable by correct code, which is exactly why
    it needs a constraint -- it is the signature of a double-decrement, and
    without the CHECK it surfaces as a wrong number rather than an error.
    """
    block = _table_block("drives")
    counters = re.findall(r"(\w*bytes\w*)\s+BIGINT", block)
    assert counters, "drives has no byte counters at all"
    for counter in counters:
        assert re.search(rf"CHECK \({counter} >= 0\)", block), (
            f"drives.{counter} is missing its non-negative CHECK"
        )


def test_grant_role_and_principal_domains_are_constrained():
    """Role and principal domains match the wire contract (§6.8).

    Including `public ⇒ viewer`: a public grant at any role above viewer is
    a privilege-escalation surface, so the schema refuses to represent one.
    """
    block = _table_block("grants")
    assert re.search(r"role\s+TEXT[^,]*CHECK", block, re.S) or re.search(
        r"CHECK \(role IN \(", block
    ), "grants.role has no domain CHECK"
    assert "viewer" in block, "grants.role CHECK does not name the viewer role"
    assert re.search(r"public", block), (
        "grants has no public-principal constraint (public ⇒ viewer)"
    )


def test_ids_carry_their_prefix():
    """Every id carries its prefix; a malformed id cannot be stored (§4.1).

    The prefix is load-bearing on the wire -- the route convertors
    discriminate on it -- so an id that does not match its table's shape is
    a bug that must not reach a row.
    """
    text = _text()
    for table, prefix in (
        ("drives", "drv"),
        ("folders", "fld"),
        ("artifacts", "art"),
        ("artifact_versions", "ver"),
        ("grants", "grn"),
        ("shares", "shr"),
        ("v0_uploads", "upld"),
        ("v0_jobs", "job"),
    ):
        assert re.search(rf"\^{prefix}_\[a-f0-9\]", text), (
            f"{table}: no id-shape CHECK pinning the {prefix}_ prefix"
        )


def test_v0_substrate_ids_are_sixteen_hex():
    """Ruling 2: upload/job ids reuse the 16-hex family, not the reference's
    32-hex rev_/upl_ shapes. The schema CHECKs must match `ids.py` PREFIXES."""
    for table, prefix in (("v0_uploads", "upld"), ("v0_jobs", "job")):
        assert re.search(
            rf"\^{prefix}_\[a-f0-9\]" + r"\{16}\$", _table_block(table)
        ), (
            f"{table}.id must be ^{prefix}_[a-f0-9]{{16}}$"
        )
    # v0_job_object_refs carries no id of its own, so it pins its drive
    # reference to this branch's 16-hex drive shape (ruling 2).
    assert re.search(
        r"drive_id\s+TEXT NOT NULL CHECK \(drive_id ~ '\^drv_\[a-f0-9\]\{16\}\$'\)",
        _table_block("v0_job_object_refs"),
    ), "v0_job_object_refs.drive_id must be ^drv_[a-f0-9]{16}$"


def test_cross_drive_references_are_impossible():
    """Parent AND ROOT references stay inside one drive (§4).

    A composite (drive_id, id) foreign key is what makes a cross-drive
    reference unrepresentable. A plain FK on the id alone would let a folder
    in drive A parent an artifact in drive B, which no operation can undo --
    every repair path is itself drive-scoped.

    This test used to assert only the parent half while quoting the §12A row
    that says "parent and root". The root half was genuinely missing, and the
    test's name claimed otherwise.
    """
    text = _text()
    assert re.search(r"FOREIGN KEY \(drive_id,\s*parent_id\)", text), (
        "no composite (drive_id, parent_id) FK -- cross-drive parents are "
        "representable"
    )
    assert re.search(r"FOREIGN KEY \(id,\s*root_folder_id\)", text), (
        "drives.root_folder_id is not a composite FK -- a drive can root "
        "itself at another drive's folder"
    )


def test_shared_namespace_is_enforced_across_both_tables():
    """§6.2's collision domain spans folders AND artifacts.

    The per-table unique indexes are only half of it; Postgres has no
    cross-table unique index. The other half is a trigger, and its absence is
    what made `folder a.txt` + `artifact a.txt` under one parent commit
    cleanly -- which also makes §6.11's "at most one item" false.
    """
    text = _text()
    assert re.search(
        r"CREATE (?:OR REPLACE )?FUNCTION reject_cross_kind_name_collision", text
    ), "no cross-kind namespace trigger function"
    for table in ("folders", "artifacts"):
        assert re.search(
            rf"CREATE TRIGGER \w+\s+BEFORE INSERT OR UPDATE[^;]*ON {table}", text
        ), f"{table} has no cross-kind namespace trigger"


def test_grant_and_share_resources_are_drive_scoped():
    """§6.8/§6.9 via §12A: a grant/share is drive-scoped, and so is the
    resource it names.

    `resource_id` is polymorphic -- `resource_type` picks the table -- so no
    composite FK can pin it to the row's drive. The trigger is the layer-2
    mechanism that makes a cross-drive reference unrepresentable, exactly as
    `reject_cross_kind_name_collision` does for the §6.2 namespace. Without
    it, drive A's grant could name drive B's folder, and no drive-scoped
    operation could repair it.
    """
    text = _text()
    assert re.search(
        r"CREATE (?:OR REPLACE )?FUNCTION reject_out_of_drive_resource", text
    ), "no drive-scoping trigger function for grants/shares"
    for table in ("grants", "shares"):
        assert re.search(
            rf"CREATE TRIGGER \w+\s+BEFORE INSERT OR UPDATE OF "
            rf"drive_id,\s*resource_type,\s*resource_id[^;]*ON {table}",
            text,
        ), f"{table} has no resource drive-scoping trigger"


def test_head_version_belongs_to_its_artifact():
    """head_version_id belongs to the same artifact (§6.4).

    Composite FK, not a bare reference: otherwise an artifact can point its
    head at another artifact's version, and every read of it is wrong.
    """
    assert re.search(r"FOREIGN KEY \(id,\s*head_version_id\)", _text()) or re.search(
        r"FOREIGN KEY \(head_version_id,\s*id\)", _text()
    ), "head_version_id is not pinned to its own artifact by a composite FK"

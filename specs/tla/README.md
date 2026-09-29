# TLA+ specs

Design-level models of AgentDrive's concurrency-critical protocols, checked
with TLC. These verify *protocols*, not the Python code — when the protocol
changes, update the spec in the same PR, like any design doc.

## Running

```bash
make tla        # drift guard + bug configs (must violate) + fix configs at CI bounds (~5 min)
make tla-full   # adds the full exhaustive fix configs (adds ~10-30 min)
```

`run.sh` fetches `tla2tools.jar` (pinned by release tag + sha256) into
`specs/tla/.cache/`, verifies the checked-in TRANSLATION sections are in sync
with their PlusCal source (the `make css`-style drift guard), and runs every
config against its EXPECTED outcome: the `*_bug`/`*_nodel` configs must
reproduce their invariant violation — they are regression artifacts, and a bug
config coming up clean means the model drifted — while the `*_fix` configs
must check clean. CI runs `make tla` on `specs/tla/**` changes
(`.github/workflows/tla.yml`). Needs Java
(`brew install openjdk`).

After editing a PlusCal block, re-translate in place before committing:

```bash
java -cp specs/tla/.cache/tla2tools-*.jar pcal.trans UploadGC.tla
```

`-deadlock` (which the harness passes) disables deadlock reporting — the GC
process loops forever by design. `pcal.trans` also emits a default
`UploadGC.cfg`; delete it — the checked-in `*_bug`/`*_fix` configs are the
models.

## UploadGC — large-upload commit vs. GC sweep

Models `core/uploads.py` (`commit_upload`, `release_expired`) against
`core/gc.py` (`_mark_sweep`). Invariant `NoDangling`: no
`artifacts`/`artifact_versions` row ever references a GCS object that does not
exist.

**Finding (2026-07-23, confirmed by TLC on the as-shipped model,
`UploadGC_bug.cfg`):** a commit that straddles its session's `expires_at`
instant can produce a permanently dangling artifact:

1. `commit_upload` passes its expiry check (`uploads.py:393`) moments before
   `expires_at`, then proceeds to the GCS stat and artifact upsert — each in
   its own transaction.
2. The GC sweep starts moments after `expires_at`. Its `release_expired`
   pre-phase bulk-DELETEs the now-expired `open` row **without taking the
   per-upload `upload-commit:{id}` advisory lock** the commit is holding.
3. The mark phase then sees neither an artifact row (not yet written) nor an
   open pending row (just deleted) — the blob is unprotected.
4. The 24h age gate does not save it: the blob's `time_created` is when the
   client PUT the bytes, up to ~6 days (SESSION_TTL) before the commit.
   The sweep deletes the blob.
5. The commit's upsert then lands, writing an artifact version that references
   the deleted object.

The window is narrow (the sweep's release→mark→delete must land inside the
seconds between the commit's expiry check and its upsert, straddling
`expires_at`) but the consequence is permanent artifact corruption.

**Status: fixed.** `release_expired` now skips rows whose commit lock is held
(see below); the deterministic reproduction lives at
`tests/test_uploads_protocol.py::test_commit_straddling_expiry_is_not_gc_dangled`.

**Fix, verified by `UploadGC_fix.cfg` (`GcLocksRow = TRUE`):** make
`release_expired` honor the commit lock — take
`pg_try_advisory_lock('upload-commit:' || id)` per expired row and skip rows
whose lock is held (an in-flight commit or abort owns them; the next sweep
reaps them). An equivalent alternative, not separately modeled: have the
commit's artifact-write transaction re-lock the pending row
(`SELECT 1 FROM pending_uploads WHERE id = $1 AND state = 'open' FOR UPDATE`)
and fail as expired if it's gone — that serializes the upsert against
`release_expired`'s DELETE on the row lock, so the mark phase (which runs
after `release_expired` on the same connection) always sees either the
artifact row or the open pending row.

## ArtifactGC — normal file operations vs. GC mark-sweep

Companion spec covering the normal (buffered) operation surface against the
`cas/` mark-sweep: `upsert_artifact` (create / overwrite / CAS re-upload,
including the same-tx retention prune and the post-tx immediate orphan
delete), `copy_artifact` (CAS blob sharing), and purge. Rename, metadata
patch, soft delete, and restore are argued out of scope in the module header
(none of them change blob references; the mark query does not filter
`deleted_at`). Same invariant: `NoDangling`.

**Findings (2026-07-23, both confirmed by TLC — BOTH FIXED, same PR as the
fixes; regression tests
`tests/test_version_retention.py::test_blob_unreferenced_by_concurrent_prune_survives_overwrite`
and `tests/test_gc_mark_sweep.py::test_sweep_spares_blob_reput_after_listing`):**

1. **Immediate-orphan-delete TOCTOU** (`ArtifactGC_bug.cfg`;
   `artifacts.py:1261-1277`). The overwrite path's post-tx cleanup — check
   "is the previous head blob still referenced anywhere?", then
   `storage.delete` — is not atomic with anything. Between the writer's tx,
   the check, and the delete, a concurrent writer can prune/purge the last
   reference AND a third party can re-upload the same content (CAS: same
   key, re-put) and commit a new reference; the unconditional delete then
   destroys a referenced blob. The F2 skip-guard (`pruned_gcs_objects`) is
   process-local, so another writer's prune bypasses it. Note the check can
   only observe the blob unreferenced under such concurrent interference in
   the first place — the previous head otherwise always retains its own
   version row — so the path is dead code in healthy flows and harmful
   exactly in the racy ones. Recommended fix: remove the immediate delete;
   the mark-sweep already owns orphan reclaim (modeled by
   `ImmediateOrphanDelete = FALSE`).

2. **Stale-listing sweep delete** (`ArtifactGC_nodel.cfg`, which disables
   the immediate delete to isolate this one). The sweep decides ORPHAN from
   the listing's metadata snapshot (frozen live-set + listed
   `time_created`), but `storage.delete` is by name with no generation
   precondition. CAS keys are content-addressed, so re-uploading
   previously-seen bytes re-puts an *existing* key: list → (writer re-puts
   and references the key) → unconditional delete kills the new
   generation → dangling artifact. Fix (shipped): pin sweep deletes with
   `if_generation_match` from the listed blob's generation (standard GCS GC
   practice; a re-put since the listing fails the precondition and the blob
   survives to the next sweep). Modeled by `SweepPinsGeneration = TRUE`.

`ArtifactGC_fix.cfg` (immediate delete removed + generation-pinned sweep)
model-checks clean: exhaustive at 2 writers × 2 ops, 2 artifacts, 2 content
hashes, clock bound 4 — ~109M distinct states under symmetry reduction
(`SYMMETRY Symm`; writers/artifacts/hashes are interchangeable model
values, sound for this invariant).

## ReservationAccounting — the reservation ledger

Pure verification (no bug config): `drives.reserved_bytes` must equal the sum
of open sessions' sizes at every instant, with exactly-once release across
commit settle / abort / lock-honoring `release_expired` — including
crash-retried commits (the process may die between the artifact write and the
settle, dropping its advisory lock, then retry). The model keeps `reserved`
unclamped, because the code's `GREATEST(.., 0)` clamp would mask a
double-release as silent under-counting of other sessions. Checks clean,
exhaustive (~256k distinct states). This spec pins the accounting protocol
against future refactors; if it ever reports a violation, the protocol
changed, not the spec.

## FolderNamespace — folder ops vs. concurrent artifact writes

Models the file-vs-folder name-exclusivity invariant the folders design
declares ("avoid file-vs-folder ambiguity at the same name"): no live folder
`/X/` while a live artifact `X` exists. Cascades (move/delete) and writes
into soft-deleted prefixes are deliberately out of scope — the design's §5
edge table declares those legal ("folders are sparse metadata").

**Finding (2026-07-24 — FIXED in the same PR):** the invariant
is enforced only from the mkdir side (`folders.py` file-form probe), and that
probe is an unlocked SELECT. Two violations, one per config:

1. `FolderNamespace_bug.cfg` — as shipped, violated **serially**: with live
   folder `/X/`, `upsert_artifact("X")` (and rename/copy/restore
   destinations) succeeds — no artifact-side check exists. Empirically
   confirmed; regression test
   `tests/test_folders_service.py::test_artifact_write_rejected_at_live_folder_file_form`.
2. `FolderNamespace_probe.cfg` — the naive fix (mirror probe on the artifact
   side, no shared lock) is **still violated by the race**: both unlocked
   probes pass before either insert commits, and the per-table partial
   unique indexes cannot stop a cross-table race.

`FolderNamespace_fix.cfg` (clean) verifies the shipped protocol:
`artifacts._assert_no_folder_shadow` runs inside every artifact
destination-path mutator's tx (upsert / rename / copy / restore) under the
drives-row `FOR UPDATE` those txs already hold, and the folder side
(`folders._assert_no_artifact_at_file_forms`) covers mkdir, move,
copy_subtree, and restore_cascade — each now also serialized on the drives
row (global lock order: cascade advisory → drives row → folder rows →
artifact rows). A centralized app-level handler maps `FolderConflict` to the
standard 409 on artifact routes. API note: artifact writes at a live
folder's file form now 409 with `FOLDER_PATH_CONFLICT` (kind="folder").

### Modeling notes

- One clock tick ≈ `MARK_SWEEP_AGE` (24h). A tick crossing during a
  seconds-long operation represents the `expires_at` instant falling inside
  that operation — ticks are boundary crossings, not durations.
- The sweep's age cutoff and live-set are **frozen at mark time** (`markT`),
  matching the code (`gc.py:898`, `gc.py:937`). Modeling the age gate against
  the live clock instead lets the mark snapshot go stale across ticks (~days)
  — impossible under the sweeper's 50-minute hard timeout — and produces
  spurious counterexamples. (TLC found exactly that artifact in the first
  draft of this spec.)
- Simplifications (all safe for this invariant) are listed in the module
  header comment.

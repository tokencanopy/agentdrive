"""The scopes the manifest DECLARES are the scopes the API ENFORCES (§7.1).

`v0-operations.json` carries a `scopes` array per operation, and #465 publishes
it in the served spec as `x-required-scopes` so a generated client can answer a
403 by naming the scope the token lacks. That makes the field part of the public
contract — and until this module existed nothing checked it against the code.

Proven behaviorally, from outside the app, because the surface enforces scope
through three idioms: ``_require_scope(actor, _SCOPE_X)`` in the handler body
(42 operations), an inline ``if not actor.can(...)`` (``drive_search``,
``changes_list``), and ``require_local(..., scope=...)`` composed into the route
dependency (``viewer_sessions_create``). Removing a scope from the token and
watching the wire does not care which idiom is used.

**The equality is two-directional, and both directions are load-bearing:**

* ``test_missing_declared_scope_is_denied`` — a token holding every scope EXCEPT
  the declared one gets 403, and the message names that exact scope. This is
  ``declared ⊆ enforced``: it catches an operation that stopped enforcing, or
  one that enforces some other scope.
* ``test_exactly_the_declared_scopes_is_allowed`` — a token holding ONLY the
  declared scopes is not refused. This is ``enforced ⊆ declared``, and it is the
  direction a denial test cannot see: a handler that quietly demands an *extra*
  undeclared scope, and a manifest that under-declares what the handler already
  requires, both pass every denial case while publishing a wrong
  ``x-required-scopes`` to every generated client.

Three further axes each close a bypass a single happy-path parametrization
misses:

* **subject type** — every case runs for an agent token and a human token,
  because enforcement conditioned on ``actor.is_agent`` would otherwise pass.
* **idempotent replay** — ``require_local`` promises it re-authorizes on every
  request "idempotent replays included"; the scope half is checked the same way,
  by replaying a committed key under a reduced token.
* **request variant** — list operations are exercised on their state-filter branch
  as well as their default, because a scope check placed on one code path
  survives a single-shape test.

Known limitations, deliberately not papered over:

* ``enforced ⊆ declared`` holds only for **the code path the request actually
  reaches**. Where a request stops early by nature — a restore against a live
  resource answers 409, an upload id backed by no session answers 404 — an
  extra undeclared scope demanded further down would be invisible. The request
  builder pushes as deep as it can (real ``If-Match`` from the seeded
  revisions, no ``Idempotency-Key`` on the one operation that forbids it) to
  keep that shallow region small, but it is not empty.
* The variant axis varies ``state`` only. ``drive_search``'s ``mode`` is
  another conditional branch of the same shape, uncovered.
* Every actor holds ``manager`` on what it touches, so this module cannot see
  the ORDER of the scope check against local authorization. A handler that
  resolved local capability first and answered 404 to a scopeless caller would
  pass every case here.
* The human actor uses ``workspace_role="member"`` deliberately. ``admin``
  would reach the same assertions — the one break-glass path
  (``core/v0_grants.py``'s ``_try_break_glass``) requires a drive with zero
  live managers, which ``seeded`` never produces — so ``member`` is the
  weaker, more representative credential.
"""

from __future__ import annotations

import pytest

from tests.conformance.harness import (
    ALL_SCOPES,
    OPERATIONS,
    REQUEST_VARIANTS,
    STATE_FILTER_OPS,
    build_request,
)

pytestmark = pytest.mark.asyncio

OP_IDS = sorted(OPERATIONS)


async def _call(http, op_id: str, r: dict[str, str], *, variant: bool = False):
    method, path, kwargs = build_request(op_id, r, variant=variant)
    return await getattr(http, method)(path, **kwargs)


SCOPE_CASES = [
    (op_id, scope)
    for op_id, op in sorted(OPERATIONS.items())
    for scope in (op.get("scopes") or [])
]
SCOPE_IDS = [f"{o}-{s}" for o, s in SCOPE_CASES]
VARIANT_CASES = [(o, s) for o, s in SCOPE_CASES if o in REQUEST_VARIANTS]
VARIANT_IDS = [f"{o}-{s}" for o, s in VARIANT_CASES]


def test_every_operation_declares_a_scope() -> None:
    """An unscoped /v0 operation would be reachable by any workspace token.

    The matrix itself is derived from the manifest, so "every operation is
    covered" is true by construction and worth nothing as an assertion. The
    operation count is likewise already asserted by `v0_manifest._load()` at
    import — a wrong count is a collection error, not a test failure. This is
    the one property neither of those gives for free.
    """
    assert [op_id for op_id, op in OPERATIONS.items() if not op.get("scopes")] == []


def test_the_variant_axis_is_derived_and_non_empty() -> None:
    """Guard the derivation itself.

    `REQUEST_VARIANTS` is computed by introspecting handler signatures. If that
    introspection silently returned nothing — a FastAPI internals change, a
    decorator that hides the signature — the second-code-path coverage would
    vanish with every test still green.
    """
    assert STATE_FILTER_OPS, "no state-filter operations found; derivation broke"
    assert set(OPERATIONS) >= STATE_FILTER_OPS, STATE_FILTER_OPS - set(OPERATIONS)


@pytest.mark.parametrize(("op_id", "scope"), SCOPE_CASES, ids=SCOPE_IDS)
@pytest.mark.usefixtures("enabled_transfer", "fake_storage", "bound_viewer")
async def test_missing_declared_scope_is_denied(
    http, set_actor, seeded, op_id: str, scope: str
) -> None:
    """declared ⊆ enforced, and the refusal names the scope it is about."""
    subject_type, r = seeded
    set_actor(ALL_SCOPES - {scope}, subject_type)
    resp = await _call(http, op_id, r)

    assert resp.status_code == 403, (
        f"{op_id} declares {scope} but did not enforce it: "
        f"{resp.status_code} {resp.text[:200]}"
    )
    body = resp.json()["error"]
    assert body["code"] == "PERMISSION_DENIED"
    # Without this the 403 could be any other refusal; naming the scope is what
    # proves the scope gate produced it rather than object authorization.
    assert scope in body["message"], body["message"]


@pytest.mark.parametrize("op_id", OP_IDS)
@pytest.mark.usefixtures("enabled_transfer", "fake_storage", "bound_viewer")
async def test_exactly_the_declared_scopes_is_allowed(
    http, set_actor, seeded, op_id: str
) -> None:
    """enforced ⊆ declared — the direction a denial test cannot see."""
    subject_type, r = seeded
    declared = frozenset(OPERATIONS[op_id]["scopes"])
    set_actor(declared, subject_type)
    resp = await _call(http, op_id, r)

    assert resp.status_code != 403, (
        f"{op_id} refused a token holding exactly its declared scopes "
        f"{sorted(declared)}: {resp.text[:200]}"
    )
    assert resp.status_code != 422, (
        f"{op_id} never reached its handler (422), so this case proves nothing: "
        f"{resp.text[:200]}"
    )


@pytest.mark.parametrize(("op_id", "scope"), VARIANT_CASES, ids=VARIANT_IDS)
@pytest.mark.usefixtures("enabled_transfer", "fake_storage", "bound_viewer")
async def test_variant_request_shape_is_also_enforced(
    http, set_actor, seeded, op_id: str, scope: str
) -> None:
    """Enforcement on only the default code path is a real bypass.

    `?state=all` is a second branch through the same handler; a scope check
    inside the default branch alone would serve deleted resources to a token
    holding no read scope.
    """
    subject_type, r = seeded
    set_actor(ALL_SCOPES - {scope}, subject_type)
    resp = await _call(http, op_id, r, variant=True)
    assert resp.status_code == 403, (
        f"{op_id} enforced {scope} on its default shape but not on "
        f"{REQUEST_VARIANTS[op_id]}: {resp.status_code} {resp.text[:200]}"
    )


REPLAY_CASES = [op_id for op_id in OP_IDS if OPERATIONS[op_id]["idempotency_class"] == "required"]


@pytest.mark.parametrize("op_id", REPLAY_CASES)
@pytest.mark.usefixtures("enabled_transfer", "fake_storage", "bound_viewer")
async def test_idempotent_replay_reauthorizes_the_scope(
    http, set_actor, seeded, op_id: str
) -> None:
    """A committed key replayed under a reduced token is refused, not served.

    `require_local` states it runs on every request "idempotent replays
    included" so a revoked grant cannot be replayed around. The token half must
    hold the same way: were the scope check inside the mutation body that
    `_run_mutation` skips on replay, a caller could keep a successful key and
    reuse it after losing the scope.

    Parametrized over every `idempotency_class == "required"` operation rather
    than one hand-picked mutation — proving the property for `drives_create`
    alone left the same bypass alive on the other twenty-five.
    """
    subject_type, r = seeded
    declared = OPERATIONS[op_id]["scopes"]

    set_actor(ALL_SCOPES, subject_type)
    first = await _call(http, op_id, r)
    if first.status_code not in (200, 201):
        # §6.2: only an executed mutation writes an idempotency record, so a
        # request that never committed has no replay to re-authorize. Skipped
        # loudly rather than passed silently.
        pytest.skip(f"{op_id} did not commit ({first.status_code}); no record to replay")

    set_actor(ALL_SCOPES - {declared[0]}, subject_type)
    replay = await _call(http, op_id, r)

    # The property under test is that the stored result is NOT served. That is
    # what a scope check inside `execute()` would break: `_run_mutation` skips
    # the body on replay and hands back the committed 2xx.
    assert replay.status_code not in (200, 201), (
        f"{op_id} replayed a committed key without {declared[0]} and was SERVED "
        f"{replay.status_code}: {replay.text[:200]}"
    )
    if replay.status_code == 404:
        # A delete makes its own target absent, and object authorization
        # resolves as-if-absent ahead of the token check — so the refusal is a
        # 404 rather than a 403. Still a refusal, and still not the stored
        # result, which is the invariant that matters.
        return
    assert replay.status_code == 403, replay.text[:200]
    assert replay.json()["error"]["code"] == "PERMISSION_DENIED"

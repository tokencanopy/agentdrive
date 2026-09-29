"""Real responses validate against the schema the spec publishes for them.

`response_model` guards what a handler returns against its Pydantic model. It
says nothing about the document AgentDrive actually serves: `api/openapi.py` is
a post-processor that hand-writes request unions, prunes unreachable statuses,
and attaches the error envelope to every operation. Anything it declares that
the handler does not produce — or produces differently — is a lie told to every
generated SDK, and nothing was checking it.

`test_schemathesis.py` covers this for three read-only operations, narrowly and
deliberately: unconstrained generation against a surface this constrained
(mandatory `Idempotency-Key`, mandatory `If-Match`, multipart bodies,
cross-resource ids) produces mostly 4xx with no schema-validatable body. The
request-building problem is what limits it.

That problem is already solved next door. `harness.build_request` reaches the
handler for all 47 operations, so this suite drives each one and validates the
body it gets back against the schema declared for that exact status code.

Two cases per operation, because the interesting half is the one nobody
generates:

* **success** — the shape a caller consumes.
* **denied** — the error envelope, which until now was never validated against
  its own published schema on a real response. Since #465 that schema carries
  an `enum` of the registry's error codes, so a code emitted from outside the
  registry fails here.

It asserts two things per response: the status is one the spec DECLARES for
that operation (an undeclared status is drift even when the body is fine), and
the body validates against the declared schema — including `format`, which
needs a checker registered or all 32 `format: date-time` declarations are
decoration.

**What this does NOT cover, stated in proportion.** The spec declares 459
(operation, status) pairs. Two cases per operation reach a small minority of
them, so "47 operations are exercised" must not be read as "the surface is
covered":

* Only the statuses these two cases happen to produce are checked. 429, 503,
  401, and most 400/422/404 declarations are never reached from here.
* Several operations never reach their declared 2xx at all: the three
  `*_restore` operations answer 409 against a live resource, `uploads_read`
  and `uploads_complete` answer 404 against a session id no upload backs,
  `uploads_delete` answers 428, and the download mint answers 400.
* Those last four, plus the content operations, are precisely where
  `api/openapi.py` HAND-WRITES declarations FastAPI never generated — the
  drift class named at the top of this docstring as the motivation. They are
  the least covered part of the surface, not the most.
* Only a handful of the enum's error codes are observed, `PERMISSION_DENIED`
  most of all. The enum is proven to be *enforced*, not to be *exhausted*.

Closing those needs request variants that drive an operation to a specific
declared status — an oversized body for 413, a wrong media type for 415, a
stale `If-Match` for 412 — which is worth doing and is not what this suite
does today.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from agentdrive.app import app as _app
from tests.conformance.harness import ALL_SCOPES, OPERATIONS, build_request

OP_IDS = sorted(OPERATIONS)
CASES = [(op_id, case) for op_id in OP_IDS for case in ("success", "denied")]
CASE_IDS = [f"{o}-{c}" for o, c in CASES]


def _served_spec() -> dict[str, Any]:
    return _app.openapi()


def _operations_by_id(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = {}
    for item in spec["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict) and operation.get("operationId"):
                found[operation["operationId"]] = operation
    return found


SPEC = _served_spec()
SPEC_OPERATIONS = _operations_by_id(SPEC)


def _declared_schema(op_id: str, status: int) -> dict[str, Any] | None:
    responses = SPEC_OPERATIONS[op_id].get("responses", {})
    declared = responses.get(str(status))
    if declared is None:
        return None
    content = declared.get("content") or {}
    return (content.get("application/json") or {}).get("schema")


def _validate(schema: dict[str, Any], body: Any) -> list[str]:
    # 2020-12 permits siblings of `$ref`, so carrying `components` onto the
    # schema root is what makes `#/components/schemas/...` resolve locally
    # without standing up a registry.
    rooted = dict(schema)
    rooted["components"] = SPEC["components"]
    # `format` is an annotation unless a checker is registered. Without this,
    # all 32 `format: date-time` declarations in the spec are unasserted and a
    # handler emitting `19/08/2026 06:20:11` for an RFC 3339 field validates
    # clean — `response_model` cannot catch it either, because the annotated
    # type is a bare `str`.
    return [
        f"{'/'.join(str(p) for p in error.path)}: {error.message}"
        for error in sorted(
            Draft202012Validator(
                rooted, format_checker=Draft202012Validator.FORMAT_CHECKER
            ).iter_errors(body),
            key=lambda e: list(e.path),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "case"), CASES, ids=CASE_IDS)
@pytest.mark.usefixtures("enabled_transfer", "fake_storage", "bound_viewer")
async def test_response_matches_its_declared_schema(
    http, set_actor, seeded, op_id: str, case: str
) -> None:
    subject_type, resources = seeded
    scopes = ALL_SCOPES
    if case == "denied":
        scopes = ALL_SCOPES - {OPERATIONS[op_id]["scopes"][0]}
    set_actor(scopes, subject_type)

    method, path, kwargs = build_request(op_id, resources)
    response = await getattr(http, method)(path, **kwargs)

    responses = SPEC_OPERATIONS[op_id].get("responses", {})
    assert str(response.status_code) in responses, (
        f"{op_id} returned {response.status_code}, which its spec does not declare "
        f"(declared: {sorted(responses)}); body={response.text[:160]}"
    )

    schema = _declared_schema(op_id, response.status_code)
    if schema is None or not response.content:
        # Content endpoints stream octet-stream, and some refusals are bodyless.
        return
    try:
        body = response.json()
    except ValueError:
        pytest.fail(f"{op_id} [{response.status_code}] declared JSON but did not return it")

    errors = _validate(schema, body)
    assert not errors, (
        f"{op_id} [{case} {response.status_code}] body violates its declared schema: "
        f"{errors[:3]}"
    )


def test_every_error_response_points_at_the_shared_envelope() -> None:
    """A published enum is only useful where the operation actually points at it.

    #465 replaced 357 inlined copies of the error envelope with a `$ref`. An
    earlier version of this test read only `responses["403"]`, so it guarded 45
    of those 357 — an edit that re-inlined the envelope on the other 312 passed
    it untouched. It now walks every declared status whose content is the
    envelope's media type, and requires 403 to be present rather than skipping
    an operation that stopped declaring it.
    """
    ref = {"$ref": "#/components/schemas/V0ErrorEnvelope"}
    inlined: list[str] = []
    missing_forbidden: list[str] = []

    for op_id in OP_IDS:
        responses = SPEC_OPERATIONS[op_id].get("responses", {})
        if "403" not in responses:
            missing_forbidden.append(op_id)
        for status, declared in responses.items():
            if int(status) < 400:
                continue
            content = (declared or {}).get("content") or {}
            schema = (content.get("application/json") or {}).get("schema")
            if schema is None:
                continue
            # The 422 validation envelope is its own component by design.
            if schema == {"$ref": "#/components/schemas/ValidationErrorResponse"}:
                continue
            if schema != ref:
                inlined.append(f"{op_id}[{status}]")

    assert missing_forbidden == [], f"operations no longer declaring 403: {missing_forbidden}"
    assert inlined == [], f"error responses not pointing at the shared envelope: {inlined[:10]}"

"""The /v0 error envelope is uniform and top-level: {"error": {code, message, details}}."""

import json

import pytest

from agentdrive.api.v0_errors import V0ApiError


def test_v0_api_error_has_status_code_code_message():
    err = V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive")
    assert err.status_code == 404
    assert err.code == "DRIVE_NOT_FOUND"
    assert err.message == "no such drive"
    assert err.details is None
    assert err.headers == {}


def test_v0_api_error_accepts_details_and_headers():
    err = V0ApiError(
        409,
        "IDEMPOTENCY_IN_PROGRESS",
        "retry",
        details={"current_revision": "rev_x"},
        headers={"Retry-After": "5"},
    )
    assert err.status_code == 409
    assert err.details == {"current_revision": "rev_x"}
    assert err.headers == {"Retry-After": "5"}


def test_v0_api_error_rejects_unregistered_code():
    with pytest.raises(ValueError):
        V0ApiError(400, "NO_SUCH_CODE", "boom")


async def test_api_error_handler_renders_top_level_envelope():
    from agentdrive.api.v0_errors import v0_api_error_handler

    exc = V0ApiError(404, "DRIVE_NOT_FOUND", "no such drive")
    resp = await v0_api_error_handler(None, exc)
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"] == "application/json; charset=utf-8"
    body = json.loads(resp.body)
    assert body == {"error": {"code": "DRIVE_NOT_FOUND", "message": "no such drive"}}
    assert "detail" not in body


async def test_api_error_handler_renders_details():
    from agentdrive.api.v0_errors import v0_api_error_handler

    exc = V0ApiError(412, "PRECONDITION_FAILED", "stale", details={"current_revision": "rev_x"})
    resp = await v0_api_error_handler(None, exc)
    assert json.loads(resp.body) == {
        "error": {
            "code": "PRECONDITION_FAILED",
            "message": "stale",
            "details": {"current_revision": "rev_x"},
        }
    }


async def test_validation_error_maps_to_top_level_envelope():
    from fastapi.exceptions import RequestValidationError

    from agentdrive.api.v0_errors import v0_validation_error_handler

    exc = RequestValidationError(
        [{"loc": ("body", "name"), "msg": "field required", "type": "missing"}]
    )
    resp = await v0_validation_error_handler(None, exc)
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {
        "fields": [{"location": "body.name", "reason": "required"}]
    }
    assert "detail" not in body


async def test_validation_error_collapses_extra_field_reason():
    from fastapi.exceptions import RequestValidationError

    from agentdrive.api.v0_errors import v0_validation_error_handler

    exc = RequestValidationError(
        [{"loc": ("body", "bogus"), "msg": "extra forbidden", "type": "extra_forbidden"}]
    )
    resp = await v0_validation_error_handler(None, exc)
    assert json.loads(resp.body)["error"]["details"] == {
        "fields": [{"location": "body.bogus", "reason": "unknown_field"}]
    }

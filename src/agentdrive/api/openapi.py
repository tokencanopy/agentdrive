"""Reusable OpenAPI contract fragments for the public `/v0` routers.

The post-processor here makes the served spec TRUTHFUL about the v0 wire
contract (§11.2/§11.3):

  * registers the top-level ``{"error": {code, message, details}}`` envelope
    (and its validation sibling) as real components, so every error ``$ref``
    resolves — the spec is a valid OpenAPI document a generator can consume;
  * models bearer auth (``securitySchemes.bearerAuth`` + a global ``security``
    on the v0 surface) so clients and generators see how to authenticate;
  * documents the per-operation responses the runtime actually produces —
    statuses, ETag/Location/Retry-After/304/307, and the stream-or-307 content
    surface — driven by ``src/agentdrive/api/v0-operations.json`` (the manifest) rather
    than hand-maintained allowlists that drift;
  * adds the generic failure set (401/403/429/400/503 with their headers) to
    every /v0 operation and flips the contractually-mandatory Idempotency-Key /
    If-Match request headers to ``required: true`` from the manifest's
    ``precondition_class`` metadata.

The manifest is the source of truth for the operation surface (47 ops) and its
``precondition_class``/``expected_statuses`` describe each op's wire behavior.
"""

from __future__ import annotations

from typing import Any

from .error_codes import ERROR_CODES

X_REQUEST_ID_HEADER = {
    "description": "Request correlation identifier.",
    "schema": {"type": "string"},
}
WWW_AUTHENTICATE_HEADER = {
    "description": "RFC 6750 bearer authentication challenge.",
    "schema": {"type": "string"},
}
RETRY_AFTER_HEADER = {
    "description": "Seconds until the caller should retry.",
    "schema": {"type": "integer", "minimum": 0},
}
ETAG_HEADER = {
    "description": "Current strong entity tag.",
    "schema": {"type": "string"},
}
LOCATION_HEADER = {
    "description": "Canonical URL of the created resource.",
    "schema": {"type": "string", "format": "uri-reference"},
}
REDIRECT_LOCATION_HEADER = {
    "description": "Redirect target.",
    "schema": {"type": "string", "format": "uri-reference"},
}


# The v0 error envelope (§6.3): top-level {"error": {code, message, details}}.
# The schemas.py ErrorResponse/ValidationErrorResponse describe the legacy
# {"detail": ...} shape (kept for the legacy /health surface); the v0 routers
# emit the top-level shape, so the spec must model THAT. We declare it inline
# here and register it as components so refs resolve.
V0_ERROR_BODY_SCHEMA = {
    "type": "object",
    "required": ["code", "message"],
    "properties": {
        "code": {
            "type": "string",
            "description": "Stable machine-readable error code (see the error-catalog).",
            # The registry is enforced bidirectionally by
            # tests/test_error_code_registry.py — EMITTED subset of REGISTRY
            # and REGISTRY subset of EMITTED — so this enum is exactly the
            # set of codes that can appear on the wire, not a guess. It is
            # what lets a generated SDK expose an exhaustive union in every
            # language instead of re-deriving the catalog per client.
            "enum": sorted(ERROR_CODES),
        },
        "message": {"type": "string"},
        "details": {
            "type": "object",
            "description": "Error-code-specific context (optional).",
        },
    },
    "additionalProperties": True,
}

V0_ERROR_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {"error": V0_ERROR_BODY_SCHEMA},
}

# FastAPI's RequestValidationError handler (app.py) renders the top-level
# envelope with a `fields` list under details.
V0_VALIDATION_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "location": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            },
            "additionalProperties": True,
        }
    },
}

BEARER_AUTH_SCHEME = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": (
        "Hub-issued bearer token. Scopes: drives:*, content:*, sharing:*, "
        "changes:read, usage:read. See the OAuth-protected-resource discovery "
        "document (RFC 9728)."
    ),
}

# Per-op error responses beyond the generic set, keyed by operation id.
# The manifest's precondition_class drives the mutation-specific ones.
_PRECONDITION_CLASS_ERRORS = {
    "read": {404: "The resource was not found or is not visible to the caller."},
    "mutation-of-existing": {
        404: "The resource was not found or is not visible to the caller.",
        409: "The mutation conflicts with current state (name/path, lifecycle).",
        412: "If-Match did not match the resource's current revision.",
        428: "If-Match is required for this mutation.",
    },
    "creation-flavored": {
        404: "The parent or target resource was not found or is not visible.",
        409: (
            "A sibling already occupies the name/path, or the idempotency key "
            "was reused for a different request."
        ),
        412: "If-Match did not match (copy/restore preconditions).",
        428: "If-Match is required for this mutation.",
    },
}

# Creation-flavored operations do not share one If-Match contract. Most take
# no precondition at all; copy accepts an optional source revision (412 only),
# upload begin conditionally requires one for a version target, and complete
# rechecks the revision captured by begin (412 without accepting If-Match).
_CREATION_PRECONDITION_ERRORS = {
    "drives_create": {},
    "folders_create": {},
    "folders_copy": {412: "If-Match did not match the source folder revision."},
    "artifacts_create": {},
    "artifacts_copy": {412: "If-Match did not match the source artifact revision."},
    "grants_create": {},
    "shares_create": {},
    "viewer_sessions_create": {},
    "uploads_create": {
        412: "If-Match did not match the target artifact revision.",
        428: "A version upload target requires If-Match.",
    },
    "uploads_complete": {
        412: "The target artifact changed after the upload session began.",
    },
    "download_capabilities_create": {},
    "sheet_sessions_create": {
        412: "If-Match did not match the artifact revision.",
        428: "If-Match is required to open a sheet session.",
    },
    "sheet_sessions_write_cells": {},
    "sheet_sessions_complete": {
        412: "The artifact head changed after the sheet session began.",
    },
}

# Operation ids whose responses carry specific headers.
_ETAG_OPERATION_IDS = {
    # reads carry ETag
    "drives_read",
    "folders_read",
    "artifacts_read",
    "versions_read",
    "grants_read",
    "shares_read",
    # mutations return the new ETag
    "drives_create",
    "drives_update",
    "drives_delete",
    "drives_restore",
    "folders_create",
    "folders_update",
    "folders_delete",
    "folders_restore",
    "folders_copy",
    "artifacts_create",
    "artifacts_update",
    "artifacts_delete",
    "artifacts_restore",
    "artifacts_copy",
    "versions_append",
    "versions_restore",
    "grants_create",
    "grants_update",
    "grants_revoke",
    "shares_create",
    "shares_revoke",
    "shares_rotate",
    "uploads_create",
    "uploads_read",
    "uploads_delete",
    "uploads_complete",
    "sheet_sessions_create",
    "sheet_sessions_read",
    "sheet_sessions_delete",
    "sheet_sessions_write_cells",
    "sheet_sessions_complete",
}

# 201-create operations that emit a Location header.
_LOCATION_OPERATION_IDS = {
    "sheet_sessions_create",
    "sheet_sessions_complete",
    "drives_create",
    "folders_create",
    "folders_copy",
    "artifacts_create",
    "artifacts_copy",
    "grants_create",
    "shares_create",
    "versions_append",
    "versions_restore",
    "uploads_create",
    "uploads_complete",
}

# Operations that honor If-None-Match and can return 304.
_CONDITIONAL_READ_OPERATION_IDS = {
    "drives_read",
    "folders_read",
    "artifacts_read",
    "artifacts_content",
    "versions_read",
    "versions_content",
    "grants_read",
    "shares_read",
    "uploads_read",
    "sheet_sessions_read",
}

# The B3 direct-upload session controls: idempotent-replay 200s on the
# creation-flavored POSTs, plus the disabled-first 503 every control keeps
# until the complete B8 configuration is present.
_UPLOAD_OPERATION_IDS = {
    "uploads_create",
    "uploads_read",
    "uploads_delete",
    "uploads_complete",
}
_UPLOAD_REPLAY_OPERATION_IDS = {"uploads_create", "uploads_complete"}

# The CLOSED begin request union (B3 §5.2), served explicitly because the
# route parses the raw body (duplicate-key rejection) and FastAPI therefore
# cannot infer it. Mirrors the strict Pydantic models in api/v0_uploads.py;
# the conformance suite pins the two against each other.
_UPLOAD_BEGIN_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target", "content"],
    "properties": {
        "target": {
            "description": "Exactly one destination union member.",
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "parent_folder_id", "name"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["artifact"]},
                        "parent_folder_id": {
                            "type": "string",
                            "pattern": "^fld_[a-f0-9]{16}$",
                        },
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "artifact_id"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["version"]},
                        "artifact_id": {
                            "type": "string",
                            "pattern": "^art_[a-f0-9]{16}$",
                        },
                    },
                },
            ],
        },
        "content": {
            "type": "object",
            "additionalProperties": False,
            "required": ["size_bytes", "media_type", "checksum"],
            "properties": {
                "size_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Declared object size in bytes, within the enabled B8-configured window."
                    ),
                },
                "media_type": {
                    "type": "string",
                    "description": "Bare IANA type/subtype, no parameters.",
                },
                "checksum": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["algorithm", "value"],
                    "properties": {
                        "algorithm": {"type": "string", "enum": ["crc32c"]},
                        "value": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9+/]{6}==$",
                            "description": (
                                "Canonical padded standard-base64 CRC32C of "
                                "exactly four bytes (GCS metadata form)."
                            ),
                        },
                    },
                },
            },
        },
    },
}

# The packet-4 download-capability mint (B3 §5.7): a strict discriminated
# request union, forbidden Idempotency-Key, 200-only success with no-store/
# no-referrer headers, and a fail-closed 503 — served explicitly because the
# route parses the raw body (duplicate-key rejection) and FastAPI therefore
# cannot infer it.
_DOWNLOAD_MINT_OPERATION_ID = "download_capabilities_create"

_DOWNLOAD_MINT_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target"],
    "properties": {
        "target": {
            "description": "Exactly one download-target union member.",
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "artifact_id"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["artifact"]},
                        "artifact_id": {
                            "type": "string",
                            "pattern": "^art_[a-f0-9]{16}$",
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "artifact_id", "version_id"],
                    "properties": {
                        "kind": {"type": "string", "enum": ["version"]},
                        "artifact_id": {
                            "type": "string",
                            "pattern": "^art_[a-f0-9]{16}$",
                        },
                        "version_id": {
                            "type": "string",
                            "pattern": "^ver_[a-f0-9]{16}$",
                        },
                    },
                },
            ],
        },
    },
}

# Content endpoints stream bytes or 307 to a signed URL.
# Sheet reads answer two statuses no other read does: 409 when the artifact
# is not a workbook at all (deliberately NOT 404 — it exists and the caller
# can see it), and 413 when the requested rectangle exceeds the read cap.
_SHEET_READ_OPERATION_IDS = {
    "sheets_list",
    "sheet_cells_read",
    # The version-scoped twins answer the same two statuses for the same
    # reasons — a version of a markdown file is still not a workbook.
    "version_sheets_list",
    "version_cells_read",
}
_SHEET_READ_ERRORS = {
    409: (
        "The artifact is not a spreadsheet, or the workbook cannot be edited "
        "(WORKBOOK_NOT_EDITABLE). Not 404: the artifact exists and is visible."
    ),
    413: "The requested range exceeds the per-request cell cap.",
}

_CONTENT_OPERATION_IDS = {"artifacts_content", "versions_content"}

# Multipart body endpoints (the manifest describes the parts in prose).
_MULTIPART_OPERATION_IDS = {"artifacts_create", "versions_append"}

# The change feed can return 410 when a cursor predates retained history.
_CHANGES_OPERATION_IDS = {"changes_list"}


# The generic failure set every /v0 operation can produce (§6.3): the
# authenticated surface's auth/rate-limit envelope (401/403/429 + the 503 when
# token verification itself is unavailable) plus malformed-request 400s.
# Descriptions are per-response; operation-specific 400s (search mode,
# change-feed cursor rules) extend them in `_apply_operation_responses`.
_GENERIC_FAILURE_RESPONSES = {
    "401": {
        "description": "Missing or invalid bearer token.",
        "headers": {"WWW-Authenticate": WWW_AUTHENTICATE_HEADER},
    },
    "403": {"description": "Token lacks a required scope."},
    "429": {
        "description": "Rate limited.",
        "headers": {"Retry-After": RETRY_AFTER_HEADER},
    },
    "503": {
        "description": (
            "Token verification is temporarily unavailable (the Hub JWKS "
            "could not be fetched). This is the API's unavailability, not "
            "a problem with the presented credential."
        ),
        "headers": {"Retry-After": RETRY_AFTER_HEADER},
    },
    "400": {
        "description": ("Malformed request (invalid query parameter, cursor, or argument)."),
    },
}

# Operation-specific 400 clarifications, keyed by operation id.
_GENERIC_400_CLARIFICATIONS = {
    "drive_search": (
        "Malformed request (invalid query parameter, cursor, or argument). "
        "Requesting a disabled search mode fails with SEARCH_MODE_UNAVAILABLE."
    ),
    "changes_list": (
        "Malformed request (invalid query parameter, cursor, or argument). "
        "Pass exactly one of start or cursor (INVALID_REQUEST); a cursor not "
        "issued for this drive fails with INVALID_CURSOR."
    ),
}


def _manifest_required_header_sets() -> tuple[set[str], set[str]]:
    """Derive which operations require Idempotency-Key / If-Match from the
    manifest metadata.

    Idempotency-Key requirement comes from the EXPLICIT ``idempotency_class``
    (B3 §1: ``required`` | ``not_required`` | ``forbidden`` — never inferred
    from the HTTP method). Every ``mutation-of-existing`` operation
    additionally requires If-Match (428 when absent); the creation-flavored
    POSTs (``/copy``, upload begin/complete) accept it optionally or not at
    all, so they stay out of the required set automatically.
    """
    manifest = _manifest_operations()
    idempotency = {
        op_id for op_id, op in manifest.items() if op.get("idempotency_class") == "required"
    }
    if_match = {
        op_id for op_id, op in manifest.items() if op.get("if_match_class") == "required"
    }
    return idempotency, if_match


def _manifest_operations() -> dict[str, dict[str, Any]]:
    # OpenAPI generation validates whichever subset is mounted, but it needs
    # the closed catalog so both the production and staged route tables can be
    # decorated from the same metadata.
    from ..api.v0_manifest import catalog_operations

    return {o["operation_id"]: o for o in catalog_operations}


def _error_response(description: str, *, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    response_headers = {"X-Request-Id": X_REQUEST_ID_HEADER}
    if headers:
        response_headers.update(headers)
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/V0ErrorEnvelope"}}
        },
        "headers": response_headers,
    }


def add_documented_response_headers(spec: dict[str, Any]) -> dict[str, Any]:
    """Make the served spec truthful about the v0 wire contract.

    Runs after FastAPI's generation: registers the v0 error components and
    bearer auth, then fills in the per-operation error/header/status responses
    from the manifest and the operation-specific sets above.
    """
    methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
    manifest = _manifest_operations()
    idempotency_required, if_match_required = _manifest_required_header_sets()

    # Register the v0 error envelope + bearer auth so every $ref resolves and
    # generators see the auth model.
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas["ErrorResponse"] = V0_ERROR_RESPONSE_SCHEMA
    schemas["ValidationErrorResponse"] = V0_VALIDATION_RESPONSE_SCHEMA
    schemas.setdefault("V0ErrorEnvelope", V0_ERROR_RESPONSE_SCHEMA)

    security_schemes = spec.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes.setdefault("bearerAuth", BEARER_AUTH_SCHEME)

    for path, path_item in spec.get("paths", {}).items():
        is_v0 = path.startswith("/v0")
        for method, operation in path_item.items():
            if method not in methods:
                continue
            if is_v0:
                # The v0 surface is bearer-authenticated (except /s/, which is
                # not under /v0). Set per-operation so strict OpenAPI 3.1
                # validators accept it.
                operation.setdefault("security", [{"bearerAuth": []}])
            op_id = operation.get("operationId", "")
            if is_v0:
                man = manifest.get(op_id)
                _apply_operation_responses(operation, op_id, man)
                _apply_generic_failures(operation, op_id)
                _apply_required_headers(
                    operation,
                    op_id,
                    idempotency_required=idempotency_required,
                    if_match_required=if_match_required,
                )
                _drop_redundant_authorization_param(operation)
                _apply_required_scopes(operation, man)
            # Any route FastAPI gave a 422 against the legacy HTTPValidationError
            # shape is restated against the registered v0 validation envelope so
            # the spec stays a resolvable document.
            _rewrite_422(operation)

    # Remove legacy FastAPI schemas that shadow our registered shapes.
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    return spec


def _drop_redundant_authorization_param(operation: dict[str, Any]) -> None:
    """Deprecate the hand-declared `authorization` header parameter.

    FastAPI emits one because the auth dependency reads the header
    explicitly, but the operation already carries `security: [{bearerAuth:
    []}]`, which is how a generator is supposed to learn about auth.
    Declaring both makes every generated client sprout an `authorization`
    argument on every method — noise at best, and an invitation to pass a
    token by hand instead of through the client's own auth at worst.

    Deprecated rather than removed, on the compatibility gate's own
    advice: oasdiff reports `request-parameter-removed` as a warning
    ("some clients may return an error when receiving an unexpected
    parameter; it is recommended to deprecate the parameter first"), and
    `scripts/check-openapi-compat.sh` fails on warnings. Marking it is
    non-breaking, records the intent in the document, and lets a later
    release drop it. Generators still emit the argument meanwhile; the SDK
    simply never passes it.

    Description-only either way: the wire contract is unchanged, and the
    header is still required — `security` is what says so.
    """
    params = operation.get("parameters")
    if not params:
        return
    for p in params:
        if p.get("in") == "header" and str(p.get("name", "")).lower() == "authorization":
            p["deprecated"] = True
            p["description"] = (
                "Deprecated: redundant with the operation's `bearerAuth` "
                "security requirement, which is how a generated client "
                "should learn to authenticate. Scheduled for removal."
            )


def _apply_required_scopes(operation: dict[str, Any], man: dict[str, Any] | None) -> None:
    """Publish each operation's product scopes as `x-required-scopes`.

    `bearerAuth` is an http/bearer scheme, which has nowhere to express
    scopes — only OAuth2 flows do, and restructuring the scheme to gain a
    field would be a far larger change than the field is worth. The
    manifest already knows, so the extension carries it: a client can then
    answer a 403 by naming the scope the token is missing instead of
    saying "forbidden".
    """
    if not man:
        return
    scopes = man.get("scopes")
    if scopes:
        operation["x-required-scopes"] = list(scopes)


def _rewrite_422(operation: dict[str, Any]) -> None:
    responses = operation.get("responses", {})
    if "422" not in responses:
        return
    responses["422"] = {
        "description": "Request validation failed.",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ValidationErrorResponse"}}
        },
        "headers": {"X-Request-Id": X_REQUEST_ID_HEADER},
    }


def _apply_generic_failures(operation: dict[str, Any], op_id: str) -> None:
    """Document the auth/rate-limit/malformed-request failure set on /v0 ops.

    Every /v0 operation sits behind bearer auth, the v0 rate limiter, and the
    §6.3 envelope — so 401/403/429/400/503 are all real wire outcomes a
    generated SDK must model. ``setdefault`` keeps any hand-declared status
    intact; the operation-specific 400 clarifications (search mode,
    change-feed cursor) override the generic wording.
    """
    responses = operation.setdefault("responses", {})
    for status, response in _GENERIC_FAILURE_RESPONSES.items():
        responses.setdefault(status, _error_response(response["description"]))
        if response.get("headers"):
            responses[status].setdefault("headers", {}).update(response["headers"])
    clarification = _GENERIC_400_CLARIFICATIONS.get(op_id)
    if clarification is not None:
        responses["400"]["description"] = clarification


def _apply_required_headers(
    operation: dict[str, Any],
    op_id: str,
    *,
    idempotency_required: set[str],
    if_match_required: set[str],
) -> None:
    """Flip the contractually-mandatory request headers to ``required: true``.

    FastAPI models the ``Idempotency-Key`` / ``If-Match`` header parameters as
    optional (they are typed ``str | None``); the manifest's explicit
    ``if_match_class`` is the source of truth for when If-Match is mandatory.
    """
    for parameter in operation.get("parameters", []):
        if parameter.get("in") != "header":
            continue
        name = parameter.get("name")
        if name == "Idempotency-Key":
            parameter["required"] = op_id in idempotency_required
        elif name == "If-Match":
            parameter["required"] = op_id in if_match_required


def _apply_operation_responses(
    operation: dict[str, Any],
    op_id: str,
    man: dict[str, Any] | None,
) -> None:
    responses = operation.setdefault("responses", {})

    # The manifest declares the op's success status(es). FastAPI emits the
    # decorator's status_code (default 200); when the runtime actually returns
    # 201 (copy/restore ops that share a 200-flavored decorator), promote the
    # success response so the spec matches the wire.
    expected = (man or {}).get("expected_statuses") or []
    expected_strs = {str(s) for s in expected}
    if "201" in expected_strs and "201" not in responses and "200" in responses:
        responses["201"] = responses.pop("200")

    precondition_class = (man or {}).get("precondition_class", "read")
    class_errors = dict(_PRECONDITION_CLASS_ERRORS.get(precondition_class, {}))
    if precondition_class == "creation-flavored":
        class_errors.pop(412, None)
        class_errors.pop(428, None)
        class_errors.update(_CREATION_PRECONDITION_ERRORS.get(op_id, {}))
    for status, description in class_errors.items():
        responses.setdefault(str(status), _error_response(description))

    if op_id in _SHEET_READ_OPERATION_IDS:
        for status, description in _SHEET_READ_ERRORS.items():
            responses.setdefault(str(status), _error_response(description))

    if op_id in _UPLOAD_OPERATION_IDS:
        responses.setdefault(
            "406",
            _error_response(
                "Accept does not admit application/json (NOT_ACCEPTABLE) — "
                "every upload-control response is JSON."
            ),
        )
        responses.setdefault(
            "503",
            _error_response(
                "Direct transfer is not fully configured/enabled "
                "(TRANSFER_DISABLED, no Retry-After), or the provider is "
                "transiently unavailable (TRANSFER_UNAVAILABLE, with "
                "Retry-After).",
                headers={"Retry-After": RETRY_AFTER_HEADER},
            ),
        )
        if op_id in {"uploads_create", "uploads_complete"}:
            responses.setdefault(
                "422",
                _error_response(
                    "A hard transfer bound cannot be acquired, or the finalized "
                    "object failed a deterministic publication check "
                    "(TRANSFER_LIMIT_EXCEEDED, CHECKSUM_MISMATCH, "
                    "OBJECT_SIZE_MISMATCH, OBJECT_METADATA_MISMATCH, "
                    "UPLOAD_EXPIRED)."
                ),
            )
    if op_id == "uploads_create":
        responses.setdefault(
            "413",
            _error_response(
                "The declared size exceeds the enabled direct-transfer "
                "ceiling (PAYLOAD_TOO_LARGE), or the control body exceeds "
                "its bound."
            ),
        )
        responses.setdefault(
            "415",
            _error_response("The begin body must be application/json (UNSUPPORTED_MEDIA_TYPE)."),
        )
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _UPLOAD_BEGIN_REQUEST_SCHEMA}},
        }
        operation["description"] = (
            (operation.get("description") or "")
            + "\n\nStrict JSON body (charset utf-8): unknown/duplicate "
            "fields, unknown discriminators, non-canonical CRC32C, and "
            "malformed ids are 400 INVALID_REQUEST. An artifact target "
            "takes NO If-Match (400 if sent); a version target REQUIRES "
            "If-Match carrying the artifact head ETag (428 absent, 412 "
            "stale) — the revision is captured for completion-time "
            "enforcement."
        )
    if op_id == "uploads_delete":
        operation["description"] = (
            (operation.get("description") or "")
            + "\n\nIf-Match must carry THE session's current strong ETag: "
            "'*' and multi-member lists cannot pin a revision and are 400 "
            "INVALID_REQUEST; a weak or foreign tag is 412. The exact "
            "same-key idempotent replay is exempt from the If-Match "
            "requirement (it reauthorizes and returns the stored 200)."
        )
    if op_id == "uploads_read":
        operation["description"] = (
            (operation.get("description") or "")
            + "\n\nIdempotency-Key is not part of this read's contract "
            "(manifest idempotency_class: not_required): a supplied key "
            "plays no role and creates no idempotency record."
        )
    if op_id in _UPLOAD_REPLAY_OPERATION_IDS:
        replay = responses.setdefault(
            "200",
            {
                "description": (
                    "Idempotent replay (Idempotent-Replay: true): the retained "
                    "non-secret session state or durable completion result. "
                    "The transfer target is never reissued."
                ),
                "headers": {"X-Request-Id": X_REQUEST_ID_HEADER},
            },
        )
        # FastAPI can emit a bare 200 response for a route whose runtime
        # replay returns UploadSessionOut. Keep the established schema binding
        # explicit so generated clients do not lose the replay body.
        replay["content"] = {
            "application/json": {"schema": {"$ref": "#/components/schemas/UploadSessionOut"}}
        }
    if op_id == "viewer_sessions_create":
        # The mint fails closed while the deployment has no bound viewer
        # host: a credential minted then would be redeemable nowhere.
        responses["503"] = _error_response(
            "The private viewer is not enabled on this deployment "
            "(VIEWER_DISABLED — fail closed, no fallback, and no Retry-After: "
            "operator enablement has no honest client retry time), or token "
            "verification is temporarily unavailable (the generic "
            "auth-unavailability 503, which does carry Retry-After).",
            headers={"Retry-After": RETRY_AFTER_HEADER},
        )
    if op_id == _DOWNLOAD_MINT_OPERATION_ID:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _DOWNLOAD_MINT_REQUEST_SCHEMA}},
        }
        operation["description"] = (
            (operation.get("description") or "")
            + "\n\nStrict JSON body (charset utf-8): unknown/duplicate "
            "fields, unknown discriminators, and malformed ids are 400 "
            "INVALID_REQUEST. Idempotency-Key is FORBIDDEN on this "
            "operation (manifest idempotency_class: forbidden): a supplied "
            "key is rejected with 400 INVALID_REQUEST and no idempotency "
            "record is created — every request reauthorizes and mints a "
            "fresh signed target. The signed URL is a bearer capability "
            "after disclosure: it is bucket/object-, generation-, method-, "
            "semantic-query-, and expiry-bound only (no one-time-use or "
            "audience enforcement)."
        )
        success = operation["responses"].get("200")
        if success is not None:
            success.setdefault("headers", {}).update(
                {
                    "Cache-Control": {
                        "description": "Always no-store.",
                        "schema": {"type": "string"},
                    },
                    "Referrer-Policy": {
                        "description": "Always no-referrer.",
                        "schema": {"type": "string"},
                    },
                    "X-Content-Type-Options": {
                        "description": (
                            "Always nosniff — governs THIS JSON response only, "
                            "never the later GCS response."
                        ),
                        "schema": {"type": "string"},
                    },
                }
            )
        operation["responses"].setdefault(
            "406",
            _error_response("Accept does not admit application/json (NOT_ACCEPTABLE)."),
        )
        operation["responses"].setdefault(
            "415",
            _error_response("The mint body must be application/json (UNSUPPORTED_MEDIA_TYPE)."),
        )
        operation["responses"]["503"] = _error_response(
            "Direct transfer is not fully configured/enabled "
            "(TRANSFER_DISABLED) or the direct download signer/"
            "configuration is unavailable (DOWNLOAD_SIGNING_UNAVAILABLE) — "
            "fail closed, no redirect/stream/viewer fallback, and no "
            "retry hint of their own (B8 owns retry policy). Retry-After "
            "appears only on the generic auth-unavailability 503.",
            headers={"Retry-After": RETRY_AFTER_HEADER},
        )
        # The mint's response map is a CLOSED set (review blocker 4): it
        # has no If-Match, no idempotency claim, no publication semantics,
        # and no typed FastAPI body, so the generic creation-flavored
        # 409/412/428 and FastAPI's auto-422 are unreachable and must not
        # be advertised to SDK generators.
        for status in ("409", "412", "422", "428"):
            operation["responses"].pop(status, None)
    if op_id in _CHANGES_OPERATION_IDS:
        responses.setdefault(
            "410",
            _error_response(
                "The change cursor is older than retained history. Recover with "
                "a full sync: capture start=now, enumerate current resources, "
                "then replay from the captured cursor."
            ),
        )
    if op_id in _CONTENT_OPERATION_IDS:
        # Content endpoints stream octet-stream on 200, 307 to a signed URL
        # above the signed-download threshold, and 304 on If-None-Match.
        responses["200"] = {
            "description": "Raw artifact bytes (streamed).",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
            "headers": {"X-Request-Id": X_REQUEST_ID_HEADER},
        }
        responses.setdefault(
            "307",
            {
                "description": "Redirect to a short-lived signed URL.",
                "headers": {
                    "Location": REDIRECT_LOCATION_HEADER,
                    "X-Request-Id": X_REQUEST_ID_HEADER,
                },
            },
        )
        responses["307"].setdefault("headers", {}).setdefault(
            "Location", REDIRECT_LOCATION_HEADER
        )
    if op_id in _CONDITIONAL_READ_OPERATION_IDS:
        responses.setdefault(
            "304",
            {
                "description": "If-None-Match matched the current ETag.",
                "headers": {
                    "ETag": ETAG_HEADER,
                    "X-Request-Id": X_REQUEST_ID_HEADER,
                },
            },
        )
        responses["304"].setdefault("headers", {}).setdefault("ETag", ETAG_HEADER)

    for status in list(responses):
        headers = responses[status].setdefault("headers", {})
        headers.setdefault("X-Request-Id", X_REQUEST_ID_HEADER)
        if op_id in _ETAG_OPERATION_IDS and status in ("200", "201", "204", "304", "412"):
            headers.setdefault("ETag", ETAG_HEADER)
        if op_id in _LOCATION_OPERATION_IDS and status in ("200", "201"):
            headers.setdefault("Location", LOCATION_HEADER)

    if op_id in _MULTIPART_OPERATION_IDS:
        # Both raise these and neither declared them, so an SDK generated from
        # this document could not model an over-size or wrong-media-type
        # failure on the only content-write path that works today.
        responses.setdefault(
            "413",
            _error_response(
                "The content part exceeds the inline ceiling "
                "(ARTIFACT_TOO_LARGE). Above it, use a direct upload session."
            ),
        )
        responses.setdefault(
            "415",
            _error_response(
                "This operation requires multipart/form-data "
                "(UNSUPPORTED_MEDIA_TYPE)."
            ),
        )
        # Multipart request schemas, per operation: create carries the full
        # set (parent_id/name/metadata/content/content_type/sha256), append
        # accepts only the byte parts (content/content_type/sha256) — the
        # shared schema was wrong for both.
        shared_parts = {
            "content": {
                "type": "string",
                "format": "binary",
                "description": "The artifact bytes.",
            },
            "content_type": {
                "type": "string",
                "description": "Declared media type.",
            },
            "sha256": {
                "type": "string",
                "description": "Optional content sha256 for verification.",
            },
        }
        if op_id == "artifacts_create":
            schema = {
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "Destination folder id (fld_*).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Artifact name.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Free-form JSON metadata.",
                    },
                    **shared_parts,
                },
                "required": ["parent_id", "name", "content"],
            }
            parts_line = (
                "Parts: parent_id, name, metadata, content (bytes), "
                "content_type, sha256. parent_id, name, and content are "
                "required."
            )
        else:  # versions_append
            schema = {
                "type": "object",
                "properties": dict(shared_parts),
                "required": ["content"],
            }
            parts_line = (
                "Parts: content (bytes), content_type, sha256. content is "
                "required; name, parent_id, and metadata are not accepted here."
            )
        operation["requestBody"] = {
            "required": True,
            "content": {"multipart/form-data": {"schema": schema}},
        }
        operation["description"] = (
            operation.get("description") or ""
        ) + f"\n\nMultipart only (415 for a JSON body). {parts_line}"

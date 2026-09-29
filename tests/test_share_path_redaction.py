"""Share credentials are redacted from both application and access logs."""

import io
import logging

import pytest

from agentdrive.config import settings
from agentdrive.observability.logging_setup import (
    BracketFormatter,
    ContextFilter,
    RedactSharePathFilter,
)
from agentdrive.observability.middleware import RequestContextMiddleware, _log_safe_path

SECRET = "share_synthetic_secret"


@pytest.mark.parametrize(
    "mount_prefix,path,expected",
    (
        ("", f"/s/{SECRET}", "/s/<redacted>"),
        ("", f"/s/{SECRET}/content", "/s/<redacted>/content"),
        ("/drive", f"/s/{SECRET}", "/s/<redacted>"),
        ("/drive", f"/drive/s/{SECRET}", "/drive/s/<redacted>"),
        (
            "/drive",
            f"/drive/s/{SECRET}/content",
            "/drive/s/<redacted>/content",
        ),
        ("", f"/drive/s/{SECRET}", f"/drive/s/{SECRET}"),
        ("/drive", "/drive/something", "/drive/something"),
        ("/drive", "/drive/s/", "/drive/s/"),
        ("/drive", "/v/art_0000000000000000/1", "/v/art_0000000000000000/1"),
    ),
)
def test_request_context_path_redaction_is_mount_aware(monkeypatch, mount_prefix, path, expected):
    monkeypatch.setattr(settings, "mount_prefix", mount_prefix)

    assert _log_safe_path(path) == expected


@pytest.mark.parametrize("status", (200, 500))
@pytest.mark.parametrize(
    "mount_prefix,path,expected",
    (
        ("", f"/s/{SECRET}/content", "/s/<redacted>/content"),
        ("/drive", f"/s/{SECRET}/content", "/s/<redacted>/content"),
        (
            "/drive",
            f"/drive/s/{SECRET}/content",
            "/drive/s/<redacted>/content",
        ),
        ("", f"/drive/s/{SECRET}/content", f"/drive/s/{SECRET}/content"),
        ("/drive", "/drive/something", "/drive/something"),
    ),
)
def test_uvicorn_access_redaction_is_mount_aware(monkeypatch, mount_prefix, path, expected, status):
    monkeypatch.setattr(settings, "mount_prefix", mount_prefix)
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path, "1.1", status),
        exc_info=None,
    )

    assert RedactSharePathFilter().filter(record) is True
    assert record.args[2] == expected
    if expected != path:
        assert SECRET not in record.getMessage()


@pytest.mark.parametrize("raises", (False, True), ids=("success", "error"))
@pytest.mark.parametrize(
    "mount_prefix,path,expected_operation",
    (
        ("", f"/s/{SECRET}/content", "GET /s/<redacted>/content"),
        (
            "/drive",
            f"/drive/s/{SECRET}/content",
            "GET /drive/s/<redacted>/content",
        ),
    ),
)
async def test_request_logs_never_contain_share_secret_on_success_or_error(
    monkeypatch, mount_prefix, path, expected_operation, raises
):
    monkeypatch.setattr(settings, "mount_prefix", mount_prefix)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(ContextFilter())
    handler.setFormatter(BracketFormatter(base_fmt="%(name)s:"))
    request_logger = logging.getLogger("agentdrive.request")
    previous_handlers = request_logger.handlers[:]
    previous_level = request_logger.level
    previous_propagate = request_logger.propagate
    request_logger.handlers = [handler]
    request_logger.setLevel(logging.INFO)
    request_logger.propagate = False

    async def inner_app(_scope, _receive, send):
        if raises:
            raise RuntimeError("synthetic failure")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = RequestContextMiddleware(inner_app)
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    try:
        if raises:
            with pytest.raises(RuntimeError, match="synthetic failure"):
                await middleware(scope, receive, send)
        else:
            await middleware(scope, receive, send)
    finally:
        request_logger.handlers = previous_handlers
        request_logger.setLevel(previous_level)
        request_logger.propagate = previous_propagate
        handler.close()

    rendered = output.getvalue()
    assert expected_operation in rendered
    assert SECRET not in rendered

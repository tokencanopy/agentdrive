"""Explicit prerequisites for the opt-in private-viewer browser suite."""

from __future__ import annotations

import socket
from urllib.parse import urlsplit


def service_endpoint(value: str) -> tuple[str, int]:
    """Return a TCP endpoint from a database or HTTP service URL."""
    parsed = urlsplit(value)
    if not parsed.hostname:
        raise RuntimeError("browser suite prerequisite must name a host")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def assert_service_ready(variable: str, value: str) -> None:
    """Fail closed when an explicit browser-suite service is unavailable."""
    host, port = service_endpoint(value)
    try:
        with socket.create_connection((host, port), timeout=1):
            return
    except OSError as exc:
        raise RuntimeError(f"browser suite prerequisite {variable} is unreachable") from exc

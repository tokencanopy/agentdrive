"""Jinja environment for the narrow live product-information surface."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from ..config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def u(path: str) -> str:
    """Return an app URL that preserves the configured browser mount prefix."""
    if not path.startswith("/"):
        raise ValueError(f"u() expects a root-absolute path, got {path!r}")
    prefix = settings.mount_prefix.rstrip("/")
    return f"{prefix}{path}" if prefix else path


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["u"] = u

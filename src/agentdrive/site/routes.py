"""Public product-information routes that do not touch application state."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..rendering.render import render_body
from .templates import templates

router = APIRouter()

# The Markdown file is the sole editable SDK-design source. The production
# image copies this one file to the same repository-relative location, and the
# artifact renderer escapes raw HTML before this trusted-shape result reaches
# Jinja's explicit `|safe` boundary in python_sdk.html.
SDK_DESIGN_PATH = Path(__file__).resolve().parents[3] / "docs" / "agentdriveSDK.md"


@lru_cache(maxsize=1)
def sdk_design_html() -> str:
    """Render the canonical Markdown once, on first request rather than import.

    Import-time loading would make a missing docs/ file (e.g. a wheel install
    that ships only src/) crash every `agentdrive` import, not just this page.
    """
    rendered = render_body(
        SDK_DESIGN_PATH.read_bytes(),
        "text/markdown",
        SDK_DESIGN_PATH.name,
    )
    if rendered.mode != "markdown":  # pragma: no cover - fixed local input
        raise RuntimeError(f"SDK design did not render as Markdown: {rendered.mode}")
    return rendered.html


@router.get(
    "/sdk/python",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def python_sdk_page(request: Request) -> HTMLResponse:
    """Render the canonical Markdown SDK design without application I/O."""
    # The SUPPORTED origin, not `public_base_url`.
    #
    # `public_base_url` is compatibility configuration for archived helpers and
    # the mount-prefix startup invariant; production deliberately keeps it
    # pointed at a retired brand domain so nobody mistakes it for a supported
    # origin (tests/test_deploy_contract.py pins that). Building a `<link
    # rel="canonical">` from it therefore published a legacy label as this
    # page's authoritative URL — one that now redirects away and drops the
    # path. `api_base_url` is the origin this deployment actually answers on,
    # so the canonical is self-referential, and this matches how every other
    # absolute URL in the app is built (v0_shares, v0_grants, v0_folders,
    # v0_uploads, v0_deps, app.py).
    public_origin = (settings.api_base_url or settings.public_base_url).rstrip("/")
    response = templates.TemplateResponse(
        request,
        "python_sdk.html",
        {
            "design_html": sdk_design_html(),
            "canonical_url": f"{public_origin}/sdk/python",
        },
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response

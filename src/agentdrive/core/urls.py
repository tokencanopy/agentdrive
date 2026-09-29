from urllib.parse import quote

from ..config import settings


def _public_origin() -> str:
    """Where the public read surface actually answers.

    `HostSurfaceMiddleware` binds `/s/`, `/a/`, `/f/` and `/v/` to the share
    host and 404s them on every other host. So once `share_base_url` is set,
    minting these URLs from `public_base_url` yields links this very
    deployment refuses: a share link dead on delivery, and an `og:url` that
    sends every unfurl to an address that answers 404.

    Empty in dev and in tests, where one origin serves everything — the
    fallback keeps that working without an extra env var.
    """
    return (settings.share_base_url or settings.public_base_url).rstrip("/")


def _renderer_origin() -> str:
    """Origin for anonymous artifact rendering.

    A configured content origin always wins. The share origin is the bounded
    rollback/local single-origin renderer; only a deployment with neither
    content setting falls back to the legacy public base. This ordering keeps
    renderer redirects off the machine API whenever a public surface has
    been configured.
    """
    return (
        settings.public_content_base_url or settings.share_base_url or settings.public_base_url
    ).rstrip("/")


def _encoded_path(*segments: str, trailing_slash: bool) -> str:
    encoded = "/".join(quote(str(segment), safe="") for segment in segments)
    return f"/{encoded}{'/' if trailing_slash else ''}"


def renderer_url(*segments: str, trailing_slash: bool = False) -> str:
    """Exact anonymous-renderer URL for already-selected route segments.

    Segments are encoded independently, so a decoded request parameter cannot
    introduce path structure or response-header bytes. The origin comes only
    from validated process configuration, never from the request URL or Host.
    """
    if not segments:
        raise ValueError("renderer URL requires at least one route segment")
    return f"{_renderer_origin()}{_encoded_path(*segments, trailing_slash=trailing_slash)}"


def public_url(drive_id: str, path: str) -> str:
    """Canonical viewer URL for an artifact.

    Every artifact shares the same URL shape regardless of who can read it
    — the viewer route resolves access via grants. Anonymous requests for
    an artifact with no `anyone:viewer` grant get a 404 (matching the
    response for artifacts that don't exist), so the URL doesn't leak
    existence."""
    return f"{settings.public_base_url}/{drive_id}/{path}"


def share_url(share_key: str) -> str:
    """The redemption URL for a share link (`/s/{share_key}/`). Returned only
    at mint/rotate — the `share_key` is the credential, so this URL must not
    be logged or echoed elsewhere (permission-sharing-design §4.5).

    The trailing slash is load-bearing, not cosmetic. The rendered page links
    its own bytes relatively, and relative resolution replaces the last path
    segment: without the slash an image on a share page resolves to
    `/s/content` and 404s. The un-slashed form still works — it 308s here."""
    return f"{_public_origin()}{_encoded_path('s', share_key, trailing_slash=True)}"


def artifact_permalink_url(art_id: str) -> str:
    """Stable, rename-surviving public permalink for an artifact
    (`/a/{art_id}/`). Used by `publish()` — the artifact is made public via
    a `public:viewer` grant, so the permalink resolves for anyone and
    survives any later rename/move (permission-sharing-design §8.1).

    The trailing slash is load-bearing for the same reason it is on
    `share_url`: the rendered page links its own bytes relatively, and
    relative resolution replaces the last path segment, so without the slash
    an image resolves to `/a/content` and 404s. The un-slashed form still
    works — it 308s here."""
    return f"{_public_origin()}{_encoded_path('a', art_id, trailing_slash=True)}"


def console_artifact_url(drive_id: str, art_id: str) -> str:
    """Authenticated console route for an artifact owner or member.

    This is only a navigation convenience. The console performs its own
    session and workspace authorization after the browser follows the link.
    Empty when no console is configured (a self-hosted install), which the
    templates read as "no button".
    """
    if not settings.console_base_url:
        return ""
    return (
        f"{settings.console_base_url.rstrip('/')}/drive/"
        f"{quote(drive_id, safe='')}/a/{quote(art_id, safe='')}/"
    )


def version_permalink_url(art_id: str, version_id: str) -> str:
    """Permalink for ONE immutable version (`/v/{art_id}/{ver_id}/`).

    Distinct from `artifact_permalink_url` in what it promises, not merely in
    shape: `/a/` follows the head wherever it moves, `/v/` is frozen — which
    is what lets the route behind it be cached for a year. Same trailing-slash
    rule, same 308 from the un-slashed form."""
    return f"{_public_origin()}{_encoded_path('v', art_id, version_id, trailing_slash=True)}"


def folder_permalink_url(fld_id: str) -> str:
    """Stable, rename-surviving permalink for a folder (`/f/{fld_id}/`) — the
    public listing of the children a reader is entitled to, live only while a
    `public` grant covers the folder. The id-based form survives any later
    rename/move, mirroring `artifact_permalink_url`.

    Same trailing-slash rule as `/a/` and `/v/`, and for the same reason:
    relative resolution replaces the last path segment, so anything the page
    links relative to `/f/ID` would resolve against `/f/` instead. The
    un-slashed form still works — it 308s here."""
    return f"{_public_origin()}{_encoded_path('f', fld_id, trailing_slash=True)}"

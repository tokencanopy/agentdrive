"""Client-IP trust under the reverse proxy
(read-dethrottle-and-view-metrics-design.md §11, finding 5a).

Every per-IP control — the public-viewer DoS ceiling, the auth/identity
rate limits, and the view-counter's visitor hash — keys off the client IP
that uvicorn's proxy-headers middleware resolves from `X-Forwarded-For`.
With `--forwarded-allow-ips='*'` uvicorn returns the LEFT-MOST XFF entry,
which the external caller fully controls (just prepend a header) — so all
those controls become spoofable. The fix is to trust only the private /
link-local ranges Cloud Run's infra hops use, so uvicorn walks XFF
right-to-left and returns the real (public) client, skipping internal hops
and ignoring client-supplied values to their left.

This test pins that property against uvicorn's ACTUAL resolution logic
(`_TrustedHosts.get_trusted_client_address`), so a regression to '*' or a
mis-scoped range is caught in CI rather than in prod.
"""

from uvicorn.middleware.proxy_headers import _TrustedHosts

# KEEP IN LOCKSTEP with the Dockerfile CMD's --forwarded-allow-ips default.
TRUSTED_RANGES = (
    "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "169.254.0.0/16,::1,fc00::/7,fe80::/10"
)

_REAL_CLIENT = "203.0.113.7"      # a public client IP
_SPOOF_PUBLIC = "9.9.9.9"         # attacker-supplied public IP
_SPOOF_PRIVATE = "10.9.9.9"       # attacker-supplied private IP


def _resolve(xff: str, ranges: str) -> str:
    return _TrustedHosts(ranges).get_trusted_client_address(xff)[0]


def test_star_config_is_spoofable_leftmost():
    """Documents the vulnerability we're fixing: '*' returns the left-most
    (caller-controlled) entry, so a prepended XFF wins."""
    assert _resolve(f"{_SPOOF_PUBLIC}, {_REAL_CLIENT}", "*") == _SPOOF_PUBLIC


def test_trusted_ranges_return_real_client_only():
    assert _resolve(_REAL_CLIENT, TRUSTED_RANGES) == _REAL_CLIENT


def test_trusted_ranges_ignore_prepended_public_spoof():
    # Cloud Run appends the real client AFTER whatever the caller sent.
    assert _resolve(f"{_SPOOF_PUBLIC}, {_REAL_CLIENT}", TRUSTED_RANGES) == _REAL_CLIENT


def test_trusted_ranges_ignore_prepended_private_spoof():
    assert _resolve(f"{_SPOOF_PRIVATE}, {_REAL_CLIENT}", TRUSTED_RANGES) == _REAL_CLIENT


def test_trusted_ranges_skip_internal_hops_hop_count_agnostic():
    # However many private/link-local hops the infra appends, the resolver
    # lands on the real client — no fixed hop count baked in.
    one = f"{_REAL_CLIENT}, 169.254.8.1"
    two = f"{_REAL_CLIENT}, 10.1.2.3, 169.254.8.1"
    three = f"{_SPOOF_PUBLIC}, {_REAL_CLIENT}, 10.1.2.3, 169.254.8.1"
    assert _resolve(one, TRUSTED_RANGES) == _REAL_CLIENT
    assert _resolve(two, TRUSTED_RANGES) == _REAL_CLIENT
    assert _resolve(three, TRUSTED_RANGES) == _REAL_CLIENT


def test_trusted_ranges_never_returns_star_default():
    """Guard against silently reverting the Dockerfile default to '*'."""
    assert "*" not in TRUSTED_RANGES

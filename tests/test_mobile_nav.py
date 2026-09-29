"""Mobile-navigation regressions.

The sidebar is off-canvas at ≤760px (CSS translates it off-screen). That
only works if the app shell also ships the .mobile-bar hamburger + the
drawer-toggle JS to slide it back in — otherwise phones get a page with no
reachable navigation at all. These guards pin both halves (markup + the
stylesheet rules that make the drawer and the table-restack work) so a
future template/CSS edit can't silently strip mobile access again.
"""

from __future__ import annotations

from pathlib import Path

import agentdrive

_CSS = Path(agentdrive.__file__).parent / "static" / "agentdrive.css"


def test_css_defines_mobile_drawer_and_table_restack():
    """The stylesheet must carry the off-canvas drawer + scrim rules and the
    ≤760px table→card restack. Pure file read — no DB, always runs."""
    css = _CSS.read_text()
    assert "@media (max-width:760px)" in css
    # Off-canvas drawer + its open state + the backdrop that dims the page.
    assert ".side.open" in css
    assert ".side-scrim" in css
    # The mobile bar reveal is what gives the user a way to open the drawer.
    assert ".mobile-bar{display:flex !important}" in css
    # Dense tables must restack rather than overflow horizontally.
    assert ".tbl thead{display:none}" in css


def test_version_history_css_has_desktop_and_mobile_states():
    css = _CSS.read_text()
    assert ".version-popover" in css
    assert '.version-row[aria-current="page"]' in css
    assert ".version-status" in css
    mobile = css.split("@media (max-width:760px)", 1)[1]
    assert ".version-popover{position:fixed" in mobile
    assert ".viewer-bar .path-text{display:none}" in mobile
    assert ".viewer-bar .bar-theme" in mobile
    assert ".bar-permalink" in mobile
    assert ".bar-raw" in mobile

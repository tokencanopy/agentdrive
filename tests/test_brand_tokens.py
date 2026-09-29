"""Regression guard for the Token Canopy token convergence.

Reads the GENERATED app stylesheet (what the app actually serves) and
asserts the canopy/gold palette is in place and ember is gone. Pure
file reads — no fixtures, no DB."""

from pathlib import Path

CSS = (
    Path(__file__).resolve().parent.parent
    / "src" / "agentdrive" / "static" / "agentdrive.css"
).read_text()


def test_generated_banner_present():
    assert CSS.startswith("/* GENERATED from design-system/src/styles/agentdrive.css")


def test_canopy_tokens_present():
    assert "--canopy:#2B4033;" in CSS
    assert "--moss:#5C6F51;" in CSS


def test_accent_remapped_to_gold_with_canopy_fills():
    assert "--accent:#C17D2B;" in CSS          # gold · decorative
    assert "--accent-strong:#8A5214;" in CSS   # gold · links on light
    assert "--accent-fill:#2B4033;" in CSS     # canopy · button fill


def test_dark_block_converged():
    assert "--accent:#D9A05B;" in CSS
    assert "--accent-fill:#35543F;" in CSS


def test_ember_retired():
    assert "#E26534" not in CSS   # old --accent
    assert "#B84A20" not in CSS   # old --accent-fill
    assert "#A84218" not in CSS   # old --accent-strong

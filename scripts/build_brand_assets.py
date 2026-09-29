"""Render the AgentDrive brand assets (raster PNGs) from PIL primitives.

The SVG sources of truth are `static/logo.svg` (currentColor) and
`static/favicon.svg` (explicit colors). This script generates the raster
outputs that browsers and link-preview crawlers need:

  - favicon-32.png         (32x32, dark mark on transparent)
  - apple-touch-icon.png   (180x180, dark mark on brand bg)
  - og-image.png           (1200x630, full lockup + tagline on brand bg)

Run once when the mark or brand colors change; commit outputs.

Usage:
  uv run python scripts/build_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).resolve().parent.parent / "src" / "agentdrive" / "static"

# Brand palette (mirrors agentdrive.css `--ink`, `--ink-fg`, `--accent`).
INK = (26, 23, 20)  # #1A1714
INK_FG = (236, 230, 217)  # #ECE6D9 — wordmark / mark on dark
INK_FG_MUTED = (140, 133, 122)  # #8C857A — tagline / supporting copy
ACCENT = (226, 101, 52)  # #E26534 — accent dot

SF_MONO = "/System/Library/Fonts/SFNSMono.ttf"
MENLO = "/System/Library/Fonts/Menlo.ttc"
SF_PRO = "/System/Library/Fonts/SFNS.ttf"
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"


def _font(path_choices: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Pick the first available font from a fallback list."""
    for path in path_choices:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_mark(img: Image.Image, cx: int, cy: int, size: int, stroke: tuple[int, int, int]) -> None:
    """Draw the chevron-in-rounded-square mark centered at (cx, cy).

    Scales the canonical 28-unit SVG geometry to `size` pixels. Uses
    supersampling (4x) for crisp edges at small target sizes.
    """
    s = 4  # supersample factor
    big_size = size * s
    mark = Image.new("RGBA", (big_size, big_size), (0, 0, 0, 0))
    d = ImageDraw.Draw(mark)
    k = big_size / 28.0

    # Rounded rect outline: SVG `<rect x=1.5 y=1.5 w=25 h=25 rx=6 stroke=1.5>`
    rect_x0 = 1.5 * k
    rect_y0 = 1.5 * k
    rect_x1 = (1.5 + 25) * k
    rect_y1 = (1.5 + 25) * k
    radius = 6 * k
    rect_stroke = max(1, int(round(1.5 * k)))
    d.rounded_rectangle(
        [rect_x0, rect_y0, rect_x1, rect_y1],
        radius=radius,
        outline=stroke,
        width=rect_stroke,
    )

    # Chevron: SVG `<path M7,18 L14,9 L21,18 stroke=2 round-caps round-joins>`
    chevron_stroke = max(2, int(round(2 * k)))
    pts = [(7 * k, 18 * k), (14 * k, 9 * k), (21 * k, 18 * k)]
    d.line(pts, fill=stroke, width=chevron_stroke, joint="curve")
    # Round caps — overlay circles at the endpoints to match SVG round-cap.
    half = chevron_stroke / 2
    for px, py in (pts[0], pts[-1]):
        d.ellipse([px - half, py - half, px + half, py + half], fill=stroke)

    mark = mark.resize((size, size), Image.LANCZOS)
    img.alpha_composite(mark, (cx - size // 2, cy - size // 2))


def build_favicon_32() -> Path:
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    _draw_mark(img, 16, 16, 28, INK)
    out = STATIC / "favicon-32.png"
    img.save(out, "PNG", optimize=True)
    return out


def build_apple_touch_icon() -> Path:
    """180x180, dark brand bg, mark centered. iOS 'Add to Home Screen' icon."""
    img = Image.new("RGBA", (180, 180), INK + (255,))
    _draw_mark(img, 90, 90, 120, INK_FG)
    out = STATIC / "apple-touch-icon.png"
    img.save(out, "PNG", optimize=True)
    return out


def build_og_image() -> Path:
    """1200x630 social card. Centered lockup + tagline on the brand bg."""
    W, H = 1200, 630
    img = Image.new("RGBA", (W, H), INK + (255,))
    d = ImageDraw.Draw(img)

    # Eyebrow ("· now in beta") — small monospace, ember accent dot + muted text.
    eyebrow_font = _font([SF_MONO, MENLO], 22)
    eyebrow_text = "now in beta"
    eyebrow_w = d.textlength(eyebrow_text, font=eyebrow_font)
    eyebrow_y = 180
    dot_r = 6
    eyebrow_total = dot_r * 2 + 12 + eyebrow_w
    eyebrow_x0 = (W - eyebrow_total) / 2
    d.ellipse(
        [eyebrow_x0, eyebrow_y + 6, eyebrow_x0 + dot_r * 2, eyebrow_y + 6 + dot_r * 2],
        fill=ACCENT,
    )
    d.text(
        (eyebrow_x0 + dot_r * 2 + 12, eyebrow_y),
        eyebrow_text,
        font=eyebrow_font,
        fill=INK_FG_MUTED,
    )

    # Lockup: mark + wordmark, centered horizontally.
    wordmark_font = _font([SF_MONO, MENLO], 96)
    wordmark = "agentdrive"
    wordmark_w = d.textlength(wordmark, font=wordmark_font)
    mark_size = 96
    gap = 28
    lockup_w = mark_size + gap + wordmark_w
    lockup_x0 = (W - lockup_w) / 2
    lockup_cy = 300
    _draw_mark(img, int(lockup_x0 + mark_size / 2), lockup_cy, mark_size, INK_FG)
    # Align wordmark baseline with mark center — adjust for font ascent.
    ascent, _descent = wordmark_font.getmetrics()
    word_y = lockup_cy - ascent / 2 + 4
    d.text(
        (lockup_x0 + mark_size + gap, word_y),
        wordmark,
        font=wordmark_font,
        fill=INK_FG,
    )

    # Tagline.
    tagline_font = _font([SF_PRO, HELVETICA], 36)
    tagline = "A drive for the agents you build."
    tagline_w = d.textlength(tagline, font=tagline_font)
    d.text(
        ((W - tagline_w) / 2, 420),
        tagline,
        font=tagline_font,
        fill=INK_FG,
    )

    # Footer URL — small, muted.
    foot_font = _font([SF_MONO, MENLO], 22)
    foot_text = "share.tokencanopy.com"
    foot_w = d.textlength(foot_text, font=foot_font)
    d.text(
        ((W - foot_w) / 2, 530),
        foot_text,
        font=foot_font,
        fill=INK_FG_MUTED,
    )

    out = STATIC / "og-image.png"
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def main() -> None:
    for builder in (build_favicon_32, build_apple_touch_icon, build_og_image):
        path = builder()
        kb = path.stat().st_size / 1024
        print(f"  wrote {path.relative_to(STATIC.parent.parent.parent)}  ({kb:.1f} KB)")


if __name__ == "__main__":
    main()

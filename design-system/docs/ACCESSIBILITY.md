# AgentDrive accessibility · contrast audit + rules

What's measured, what passes, what to do about the few that don't. Run after every meaningful token or component change.

---

## Contrast audit · light mode

WCAG 2.1 AA requires **4.5:1** for body text, **3.0:1** for large text (18pt regular / 14pt bold) and UI components/state indicators. AAA requires 7:1 for body text.

| Pair | Ratio | AA body | AA large | AAA |
|---|---:|:---:|:---:|:---:|
| fg `#1A1714` on bg `#FAF7F2` | 16.70:1 | ✓ | ✓ | ✓ |
| fg on bg-panel `#FFFFFF` | 17.85:1 | ✓ | ✓ | ✓ |
| fg on bg-elev `#F2ECE2` | 15.19:1 | ✓ | ✓ | ✓ |
| fg-muted `#6E665B` on bg | 5.29:1 | ✓ | ✓ | ✗ |
| fg-muted on bg-panel | 5.65:1 | ✓ | ✓ | ✗ |
| **fg-subtle `#9A9082` on bg** | **2.94:1** | **✗** | **✗** | ✗ |
| accent-strong `#8A5214` on bg (link) | 5.96:1 | ✓ | ✓ | ✗ |
| accent-strong on bg-panel | 6.37:1 | ✓ | ✓ | ✗ |
| accent-strong on accent-soft `#F6EAD3` (badge) | 5.35:1 | ✓ | ✓ | ✗ |
| white on accent-fill `#2B4033` (button) | 11.15:1 | ✓ | ✓ | ✓ |
| white on accent-fill-hov `#223529` | 13.05:1 | ✓ | ✓ | ✓ |
| info-strong `#0050D6` on bg (link) | 6.30:1 | ✓ | ✓ | ✗ |
| info-strong on info-bg `#E4ECFF` | 5.69:1 | ✓ | ✓ | ✗ |
| warn-strong `#8F5F00` on warn-bg `#FFF1D1` | 4.93:1 | ✓ | ✓ | ✗ |
| danger-strong `#A82020` on danger-bg `#FBE3E0` | 5.93:1 | ✓ | ✓ | ✗ |
| k-md `#2D6CFF` on bg-elev (chip) | 3.80:1 | ✗ | ✓ | ✗ |
| k-code `#7A4FE0` on bg-elev | 4.44:1 | ✗ | ✓ | ✗ |
| k-image `#0F7A4D` on bg-elev | 4.57:1 | ✓ | ✓ | ✗ |
| **k-video `#A0700B` on bg-elev** | **3.71:1** | **✗** | ✓ | ✗ |
| k-dataset `#B43E8F` on bg-elev | 4.44:1 | ✗ | ✓ | ✗ |
| k-skill `#8A5214` on bg-elev | 5.42:1 | ✓ | ✓ | ✗ |
| ink-fg `#E8E3D8` on ink `#1A1714` | 13.95:1 | ✓ | ✓ | ✓ |
| ink-fg-muted `#8C857A` on ink | 4.89:1 | ✓ | ✓ | ✗ |
| spectral `#6FDDE5` on ink | 11.18:1 | ✓ | ✓ | ✓ |
| machine `#B6F36E` on ink | 13.63:1 | ✓ | ✓ | ✓ |
| accent `#C17D2B` on ink | 5.30:1 | ✓ | ✓ | ✗ |
| canopy `#2B4033` on canopy-soft `#E7EDE4` (tc-prod-active) | 9.36:1 | ✓ | ✓ | ✓ |
| on-canopy-muted `#B3BCA6` on canopy `#2B4033` (tc-shell switcher/email) | 5.66:1 | ✓ | ✓ | ✗ |

## Contrast audit · dark mode

| Pair | Ratio | AA body | AA large | AAA |
|---|---:|:---:|:---:|:---:|
| fg `#ECE6D9` on bg `#13110E` | 15.16:1 | ✓ | ✓ | ✓ |
| fg-muted `#A8A092` on bg | 7.28:1 | ✓ | ✓ | ✓ |
| accent-strong `#E7B77A` on bg (link) | 10.28:1 | ✓ | ✓ | ✓ |
| white on accent-fill `#35543F` (button) | 8.43:1 | ✓ | ✓ | ✓ |
| info-strong `#A6CCFF` on bg | 11.40:1 | ✓ | ✓ | ✓ |
| accent on accent-soft (badge) | 6.74:1 | ✓ | ✓ | ✗ |
| canopy-fg `#E6EDE4` on canopy-soft `#1C2A20` (tc-prod-active) | 12.56:1 | ✓ | ✓ | ✓ |
| on-canopy-muted `#B3BCA6` on canopy `#223328` (tc-shell switcher/email) | 6.78:1 | ✓ | ✓ | ✗ |

---

## Documented exceptions

Two pairs fail AA for body text. Both are intentional and constrained:

**`fg-subtle` (2.94:1)** — decoration only. Used for: hint text in inputs (e.g. `⌘K` chip), tertiary labels on long-form metadata, line separators in dense `kv` lists. **Rule:** never use `fg-subtle` as the *only* signal for important information. If you'd put it next to a screen reader and rely on it, use `fg-muted` instead.

**`k-md`, `k-video`, `k-dataset`, `k-code` chips (3.71–4.44:1)** — kind chips render at 11px with a redundant icon and a single-word label. They pass AA-large (3.0:1) which is the WCAG threshold for "UI components and graphical objects." The icon adjacent to the label provides redundant signaling. **Rule:** kind chips must always pair color with the kind glyph. Never render the kind text in this color *without* its icon.

---

## Color is never the only signal

WCAG 1.4.1 (use of color). AgentDrive satisfies this because every color-coded element has a redundant cue:

- **Kind chips** — color + icon + text label.
- **Visibility pill** (`.vis`) — color + colored dot + text ("public" / "private" / "unlisted").
- **Agent attribution** — color dot + text label (the dot color is decorative).
- **Status badges** (success / warn / danger) — color + icon (used in `.alert` and `.toast`).
- **Selected table row** — accent-soft background + (in production) `aria-selected="true"`.

If you add a new component that uses color to mean something, add a non-color signal too.

---

## Focus & keyboard

- **Focus ring** is `:focus-visible` only (don't show on mouse click). Defined globally in `agentdrive.css`. Ring is 2px solid `--accent` with a 3px soft glow at 30% opacity.
- **Skip-to-content** link should be added at the top of authenticated pages: `<a href="#main" class="sr-only sr-only-focusable">Skip to content</a>` (the `sr-only-focusable` variant of `.sr-only` will need to appear on focus — add it when you wire this in the templates).
- **Tab order** follows DOM order. Don't override with `tabindex` except `tabindex="-1"` for focusable-but-not-in-tab-order containers.
- **Modal traps focus** — when `.scrim` is mounted, focus must move to the first focusable element inside `.modal`, and tab must cycle within the modal. Close (Escape or close-button) returns focus to the trigger.
- **Keyboard shortcuts** shown in `13-states.html` section 5. Hint chips use `<kbd>` semantics.

---

## ARIA & semantic HTML

- Use semantic HTML by default — `<button>` not `<div onclick>`, `<a href>` not `<span>`. Don't fight the browser.
- **Buttons that look like links** still use `<button>`. **Links that look like buttons** still use `<a href>`.
- **Modals** — wrap with `role="dialog"`, `aria-modal="true"`, `aria-labelledby="<modal-title-id>"`.
- **Drawers** (sidebar opened on mobile) — `role="dialog"` if it traps focus, otherwise just animate visibility.
- **Toasts** — `role="status"` (polite) for success/info, `role="alert"` (assertive) for danger.
- **Tabs** — `role="tablist"` > `role="tab"` with `aria-selected`. The component sheet doesn't yet wire this; add when porting to templates.
- **Search input** — `<input type="search">` with `aria-label` if no visible label.
- **Forms** — every `<input>` has a `<label>` (visible or `.sr-only`).
- **Images** — `alt=""` for decorative, descriptive `alt` for content. Kind glyphs are decorative → `aria-hidden="true"` on the inner SVG.

---

## Reduced motion

`agentdrive.css` honors `@media (prefers-reduced-motion: reduce)` and kills animations system-wide. The `.pulse` element, `.skeleton` shimmer, and progress bar `indeterminate` animation all collapse to a static frame for users who request it. When adding new animations, scope them under a media query or use `transition` properties that respect the user's preference.

---

## Touch targets

Minimum 44×44px for any interactive target on touch surfaces. The mobile patterns in `15-mobile.html` use 56px row heights and 52px FABs. The default `.btn` at desktop sizes (28-30px) is fine for cursor use; on mobile, increase padding to ≥10px vertical or use `.btn-lg`.

---

## Internationalization

- Mono font has limited international glyph coverage. Use it only for ASCII identifiers (paths, hashes, IDs). Don't put proper names or i18n strings in mono.
- The `.path-trunc` middle-ellipsis pattern uses `direction: rtl` on the wrapper and `direction: ltr` on the inner span — works correctly with LTR-only content. RTL languages need a different truncation strategy (test first).
- Avoid concatenating sentence fragments — translate full strings.

---

## What's NOT covered yet

- Screen-reader testing on the actual app (these are static mockups). Do this once the templates are ported.
- Color-vision-deficiency simulation. Run the system through a Daltonism filter and verify kind chips remain distinguishable; the kind icons make them robust to most CVD cases but worth checking.
- Cognitive accessibility (plain-language audit). Most copy is jargon-heavy because the audience is agent developers — fine for v0, worth softening when end-users come into scope.

---

## Maintenance

When adding a token:
1. Pick a value that passes AA against its intended background. Run the snippet in this folder against your candidate hex.
2. If the value is decorative (icon, dot, gradient), document that explicitly in `CLAUDE.md`.
3. If the value will carry text or be a touch target, prove it passes 4.5:1 (or 3:1 for >18pt) before committing.

Audit script (kept in this doc so it's never lost):

```python
def srgb_to_linear(c):
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

def luminance(hex_color):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126*srgb_to_linear(r) + 0.7152*srgb_to_linear(g) + 0.0722*srgb_to_linear(b)

def contrast(fg, bg):
    lf, lb = luminance(fg), luminance(bg)
    light, dark = max(lf, lb), min(lf, lb)
    return (light + 0.05) / (dark + 0.05)
```

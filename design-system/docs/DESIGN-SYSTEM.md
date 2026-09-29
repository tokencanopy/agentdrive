# AgentDrive design system · agent handoff

This file tells any agent (Claude Code, Cursor, etc.) how to apply the AgentDrive design system to the AgentDrive codebase. **Read this before touching any HTML, CSS, or template file.**

> **Note (2026-06-27):** The design system is now a real React component library
> under `design-system/`. The token/component/two-surface/accessibility guidance
> below remains the source of truth. Two things have moved since this was written:
> - The **canonical stylesheet** is `design-system/src/styles/agentdrive.css`
>   (the app serves a generated copy at `src/agentdrive/static/agentdrive.css` via
>   `make css` — never edit that copy). The old `design/agentdrive.css` is gone.
> - The **15 `design/NN-*.html` mockups are retired** (preserved in git history).
>   The component library + its Storybook (`design-system/`) are now the visual
>   reference, and the system is synced to Claude Design. The agent-facing usage
>   reference for that sync is `.design-sync/conventions.md`.
>
> The "Where things live" and "Porting the existing Jinja templates" sections
> below describe the original port and are kept as historical context.

---

## Where things live

```
design/
├── CLAUDE.md             ← you are here
├── BRIEF.md              ← why decisions were made
├── ACCESSIBILITY.md      ← WCAG audit · contrast values · ARIA rules
├── agentdrive.css        ← THE stylesheet · tokens + primitives · v0.2
├── index.html            ← mockup directory (start here visually)
├── 01-foundations.html   ← color/type/spacing/glyph reference (self-contained)
├── 02-dashboard.html     ← Save anchor
├── 03-marketplace.html   ← Marketplace anchor
├── 04-artifact-detail.html
├── 05-empty-state.html
├── 06-share-sheet.html
├── 07-viewer-owner.html
├── 08-viewer-visitor.html
├── 09-marketplace-detail.html
├── 10-publisher.html
├── 11-collection.html
├── 12-components.html    ← every primitive in every variant
├── 13-states.html        ← loading · empty · error · long-data · focus
├── 14-marketing.html     ← historical AgentDrive-domain landing-page mock
└── 15-mobile.html        ← 380px patterns for the three anchors
```

Files 02–12 reference `agentdrive.css` via `<link>`. File 01 inlines tokens for self-contained reference.

The running app lives in `src/agentdrive/`:

```
src/agentdrive/
├── templates/            ← Jinja templates (this is what to port)
│   ├── base.html         ← global CSS lives here today, inlined GitHub-style
│   ├── app.html          ← sidebar layout
│   ├── dashboard.html    ← maps to design/02-dashboard.html
│   ├── login.html        ← no mockup yet; apply tokens directly
│   ├── settings.html     ← no mockup yet
│   └── danger.html       ← no mockup yet
├── web/                  ← FastAPI routes that render templates
└── ...
```

---

## The two-surface principle

AgentDrive has **two visual surfaces**, on purpose. Every screen uses both.

| Surface | When | Tokens |
|---|---|---|
| **Drive shell** (warm cream) | The user-facing UI. Dashboards, marketplace, viewers, modals, navigation, content. | `--bg`, `--bg-panel`, `--bg-elev`, `--fg`, `--accent` (gold `#C17D2B`) |
| **Ink** (warm dark) | Agent context surfaces. Code/MCP snippets, API keys, machine-readable provenance, "this is talking to the machine" blocks. | `--ink`, `--ink-elev`, `--ink-fg`, `--spectral` (cyan accent on ink), `--machine` (chartreuse accent on ink) |

**Rule:** if a block shows path / hash / API key / curl / MCP call / agent attribution, it goes on ink. If it's the regular product UI, it stays on cream. Mixing inside one block is allowed (a card with a kv table on cream that has one ink-surface code block inside it is correct).

---

## Tokens reference

Full source: `agentdrive.css`. Most-used:

**Color** — each semantic role has both a decorative value and an AA-passing `-strong` variant for text.
- `--accent: #C17D2B` gold · brand · dots, gradients, decorative icons. **Don't use as text color** — use `--accent-strong` instead.
- `--accent-strong: #8A5214` · text-on-light: links, badge labels (passes AA on bg-panel and on accent-soft)
- `--accent-fill: #2B4033` · primary button fill (AA-passing with white text; canopy green)
- `--accent-fill-hov: #223529` · button hover
- `--accent-soft: #F6EAD3` · tinted backgrounds, public-visibility chip
- `--ink: #1A1714` · code/agent surfaces. Never use as page background.
- `--info: #2D6CFF` / `--info-strong: #0050D6` · decorative / text
- `--warn: #C78400` / `--warn-strong: #8F5F00`
- `--danger: #CC2E2E` / `--danger-strong: #A82020`
- `--success: #0F7A4D` / `--success-bg: #DFF3E8`
- Kind hues (decorative; paired with the kind icon to be CVD-robust): `--k-md` (blue), `--k-code` (purple), `--k-image` (green), `--k-video` (amber), `--k-dataset` (magenta), `--k-skill` (gold), `--k-bundle` (gray), `--k-folder` (warn)
- Avatar palette: `--av-1` … `--av-8` for deterministic assignment by hash. Use `<span class="av" data-h="0..7">`.

**Rule:** if a token is going to render as text, use the `-strong` variant. The decorative variant is for icons, dots, gradients. See `ACCESSIBILITY.md` for the full contrast table.

**Type**
- `--f-ui` (Inter) — everything readable
- `--f-mono` (JetBrains Mono) — **all identifiers**: paths, hashes, IDs, content-types, timestamps when shown next to identifiers, agent handles (`agent:name`), API keys, file sizes. If a string is machine-meaningful, it's mono.

**Radii**: `--r-sm 4`, `--r-md 6` (default), `--r-lg 10` (cards, modals), `--r-xl 16` (heroes)

**Spacing** is a 4px grid. Most padding is 12/14/16/18/24/32/48.

**Don't introduce new color values.** If you need a color that's not in `agentdrive.css`, add it to `agentdrive.css` first, then use the variable.

---

## Component catalog

Every primitive in `12-components.html`. Pick from these before inventing.

| Component | CSS class | Use |
|---|---|---|
| Button | `.btn` + `.btn-primary` `.btn-secondary` `.btn-ghost` `.btn-danger` `.btn-ink` | Sizes: `.btn-sm` `.btn-lg`. Mono: `.btn-mono`. Only **one** primary per visible region. |
| Input | `.input` (optional `.mono`) | Mono variant for paths/IDs. |
| Checkbox / radio / toggle | `.check` `.radio` `.toggle` | Toggle for on/off settings, radio for visibility, check for filters. |
| Badge | `.badge` + `.solid-accent` `.solid-info` `.solid-warn` `.solid-danger` | Add `.dot` for live indicator. |
| Kind chip | `.kind[data-k="md"]` etc. | Use on every artifact reference. The `data-k` attribute drives the color. |
| Visibility pill | `.vis.public` `.vis.private` `.vis.unlisted` | Renders the colored dot automatically. |
| Agent tag | `.agent-tag` + optional `.user` `.gpt` `.claude` | For anything written by an agent. Dot color signals source. |
| Card | `.card` + `.card-pad` | Marketplace tiles, panels, dashboard cards. |
| Table | `.tbl` | Dense by default. Apply `.mono` to td cells holding identifiers. |
| Console | `.console` with `.c-prompt` `.c-comment` `.c-string` `.c-key` spans | Ink surface for code. |
| Modal | `.scrim` > `.modal` with `.modal-head` `.modal-body` `.modal-foot` | Shared share-sheet pattern. |
| Pill filter | `.pill` + optional `.active` | Kind/category filters, sort options. |
| Tabs | `.tabs > button.active` | Page-tab pattern in mockups uses bottom-border accent (no shared class yet — copy from 09/10). |
| Drawer | Use `.card` + `.kv` definition list | Agent-native context. See 04, 07, 12. |
| Toast | `.toast` + `.success` `.danger` | One-line confirmations. |
| Alert | `.alert` + `.alert-info` `.alert-warn` `.alert-danger` `.alert-success` | Inline banners at the top of a form/section. |
| Progress | `.progress` + `.bar` + optional `.indeterminate` | Storage, live agent activity. |
| Pulse | `.pulse` | Inline "live now" dot. |
| Spinner | `.spinner` | Inline loading inside buttons or sentences. |
| Skeleton | `.skeleton` + `.line` `.title` `.thumb` `.btn` `.avatar` | Show while data is in flight (~250ms+). |
| Empty | `.empty` (inside any container) | Zero-state inside a table, card, or modal. |
| Avatar | `.av` + `.sq` `.sm` `.lg` `.xl` + `data-h="0..7"` | Deterministic publisher color from hash. |
| Truncation | `.trunc` `.clamp-2` `.clamp-3` `.path-trunc` | Long paths, titles, table cells. |
| Mobile bar | `.mobile-bar` + `.ham` | Auto-shown below 760px when sidebar is hidden. |

---

## Layout patterns

**Authenticated app shell** (Save, Marketplace, Settings):

```html
<div class="app">
  <aside class="side"> <!-- 240px sidebar --> </aside>
  <main class="main"> <!-- content --> </main>
</div>
```

With right rail (dashboard live activity):

```html
<div class="app with-rail">
  <aside class="side"></aside>
  <main class="main"></main>
  <aside class="rail"></aside> <!-- 320px rail; collapses below 1180px -->
</div>
```

**Viewer chrome** (public artifact pages): no sidebar. Sticky top bar with lockup + breadcrumb + share/save action. See 07 and 08.

**Hero pattern** (marketplace, collection): full-width gradient or ink hero, then sticky kind-nav, then content sections. See 03 and 11.

---

## Rules · do / don't

**DO**
- Pull tokens from `agentdrive.css`. If a value isn't a variable, you're probably wrong.
- Use mono for every identifier. Path → mono. Hash → mono. Agent handle → mono.
- Show provenance. If an artifact was written by an agent, surface which agent + which MCP tool, *unless* the screen has good reason not to (visitor viewer is the only exception, and even there it lives in the machine-strip footer).
- Use kind chips on every artifact reference, not just first mention.
- Keep the accent for one signature thing per region. Don't paint half the screen in `--accent`.
- Match the breadcrumb style across screens: `mono` font, `--fg-muted` for crumbs, `--fg` for current.

**DON'T**
- Don't put body text on `--ink`. Ink is for snippets and agent context, not paragraphs.
- Don't use `--accent` for destructive actions. Destructive is `.btn-danger` (cream surface, red border).
- Don't use Tailwind, Bootstrap, or any other CSS framework. The system is hand-rolled with CSS variables — adding a framework defeats the point.
- Don't introduce shadows that aren't `--sh-1`, `--sh-2`, or `--sh-pop`.
- Don't add icons that aren't either (a) inline SVG matching the kind glyph style or (b) from a single source you commit to. Don't mix icon libraries.
- Don't break the kind/color mapping. `--k-skill` is gold and only gold. If a designer asks to recolor, push back — that color binding is load-bearing.

---

## Porting the existing Jinja templates

The current `src/agentdrive/templates/base.html` has GitHub-style tokens inlined (green `#2da44e` accent, blue `#0969da` link). Port plan:

1. **`base.html`** — strip the existing `<style>` block. Add `<link rel="stylesheet" href="/static/agentdrive.css">`. Copy `design/agentdrive.css` to `src/agentdrive/static/agentdrive.css` and register a static route if there isn't one. Keep the existing theme toggle script — it works fine. The localStorage key is `agentdrive-theme`.
2. **`app.html`** — the existing sidebar markup is close. Match the class names in `agentdrive.css` (`.app`, `.side`, `.lockup`, `.nav`, `.nav-group`, `.userblock`). The mockup at `design/02-dashboard.html` is the source of truth.
3. **`dashboard.html`** — biggest port. The current template is the table-only version. The new one adds (a) kind-filter pills, (b) view toggle, (c) right rail showing agent activity. The activity feed needs a backend hook; for v0 you can ship without it (just hide the `with-rail` class), or stub it with the user's actual recent file events.
4. **`login.html` / `check_email.html` / `settings.html` / `danger.html`** — no mockup yet. Apply the tokens directly: wrap content in `<div class="wrap">`, use `.card-pad` panels, `.btn` for actions. Use the empty-state pattern (`design/05-empty-state.html`) as your reference for the hero-style auth screens.
5. **Public viewer (`render/markdown.py` etc.)** — replace the Pygments-themed wrapper with the viewer chrome from `design/08-viewer-visitor.html` (for non-authenticated visitors) and `design/07-viewer-owner.html` (when the request is from the drive owner — you'll need to check session).

**Naming note:** The product and its mechanical identifiers retain the
AgentDrive name: `agentdrive.css`, the theme storage key, `.agentdrive-*`
classes, package/CLI names, client env vars, `/v0` paths, and opaque id
prefixes. A deployment names three origins: the machine API, the trusted
share shell, and an isolated public renderer on a separate registrable domain
(see `.env.example`). Legacy alias hosts are compatibility-only, not a naming
reference for new UI or docs.

---

## States · what to show when

The happy path is rarely the only path. The reference for everything below is `13-states.html`.

- **Loading** — use `.skeleton` for content that has known shape (cards, table rows, headings). Use `.spinner` for inline operations inside a button or sentence. Use `.progress.indeterminate` for ambient activity ("agent writing…").
- **Empty** — every list, table, and grid needs an empty state. Use the `.empty` component for in-container blanks. Use a `.pagebox` (full-card empty hero) for first-time drives, no-permissions, and drive-full states.
- **Error** — three tiers: `.alert` for inline (top of a form/section), `.toast` for transient operational errors, `.pagebox` for page-level (404/500). Always show: what went wrong, a code or trace ID, and one recovery action.
- **Long data** — every table is `table-layout: fixed` with `.tbl-trunc` and per-column widths. Use `.path-trunc` for paths where the filename matters (middle-ellipsis). Use `.clamp-2` for titles in cards. Pagination is cursor-style; show `1–50 of 1,284` with prev/next.
- **Focus & keyboard** — focus rings are `:focus-visible` only, gold at 30% opacity. All interactive elements have visible focus by default. See ACCESSIBILITY.md for the full keyboard contract.

## Mobile (≤760px)

Reference: `15-mobile.html`. Already wired in `agentdrive.css`:

- Sidebar collapses to a slide-in drawer (`.side.open` toggles `transform`).
- `.mobile-bar` appears at the top with hamburger + lockup + search-icon.
- Right rails (`.with-rail .rail`) are hidden; their content moves to an inline section or its own screen.
- Tables become row-cards with a 40px thumbnail. Don't horizontal-scroll wide tables on mobile.
- Primary actions become a FAB (drive) or a sticky bottom bar (viewer).
- Tap targets minimum 44×44px.

## Accessibility

Reference: `ACCESSIBILITY.md`. Hard rules:

- Every text use of a semantic color uses the `-strong` variant. Decorative use is the plain variant.
- Color is never the only signal — kind chips pair color with icon and label; visibility pills pair color with dot and text.
- Focus rings are not removed. If `:focus-visible` looks bad somewhere, fix the styling, don't hide the ring.
- Reduced-motion is honored globally; don't bypass it.
- Use semantic HTML — `<button>`, `<a href>`, `<input type="search">`. Buttons that *look* like links still use `<button>`.

---

## When in doubt

1. Find the closest mockup in `design/` and match its structure.
2. If no mockup covers it, find the closest component in `design/12-components.html` and build from primitives.
3. If you need a token that doesn't exist, add it to `agentdrive.css` with a name that fits the existing pattern (e.g. `--ink-fg-strong`, not `--code-text-bright`), then use the variable.
4. If you're unsure whether something belongs on cream or ink, ask: "is this surface talking to a human or to a machine?" Human → cream. Machine → ink. Both → cream with an ink-block inside.

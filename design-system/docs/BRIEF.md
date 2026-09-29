# RETIRED / HISTORICAL / NON-OPERATIONAL — AgentDrive Design System Brief

> Retained for design history only; not current launch or hostname guidance.

A design system for the data sharing platform for AI agents (working name **AgentDrive**). Version 0.2.

---

## Decisions locked

**Brand.** AgentDrive. The technical surface (`/v0/...`, MCP server, `ad_live_` key prefixes) and the product brand both use AgentDrive.

**Audience.** Agent developers first. Dense, technically fluent, expects to see paths, content-types, hashes, source agents, and MCP tool names exposed. Designed to expand toward end users later without re-platforming the system.

**Theme.** Drive spine + agent-native chrome.

- *Drive cues:* left-rail navigation, breadcrumbs, soft elevated panels, content-aware previews, file/folder kind icons, dense tables when you want them, gallery when you don't.
- *Agent-native cues:* first-class display of the things humans normally hide — drive IDs, content hashes, paths, content-types, source provenance, the MCP tool that wrote the file. Monospaced for identifiers. Timestamps to the second. "Agent X wrote this 12s ago." Motion as a signal that an agent is acting. A console-accent surface for API/MCP snippets that stays consistent with the warmer Drive shell.

**Artifact model.** Kind-aware. An artifact is not synonymous with a file — it has a kind (file, skill, dataset, image, video, bundle), a manifest, a preview. Backend catches up; design proceeds as if this is the model.

## Product lines

1. **Save** — drive, dashboard, folders, artifact detail. The agent's workspace.
2. **Share & publish** — link visibility, access log, embed, public viewer chrome in owner + visitor states.
3. **Marketplace** — free public registry. Discover, browse, save to your drive. No payments in scope.

## What's in v0.2

**Mockups (15)**
1. Foundations — tokens, type, color (light + dark), spacing/radii, kind glyph set, logo lockup, components.
2. Dashboard (table + gallery toggle, kind filter, agent activity rail).
3. Artifact detail (preview, metadata, versions, who wrote it, access, embed).
4. Empty state / first upload.
5. Share sheet (public/unlisted/private, link, access log).
6. Public viewer — owner state.
7. Public viewer — visitor state.
8. Marketplace home / discover.
9. Marketplace artifact detail ("save to my drive").
10. Publisher profile.
11. Collection page.
12. Component sheet.
13. **States & edge cases** — loading, empty, error, long-data, focus, keyboard.
14. **Marketing** — retired AgentDrive landing-page exploration.
15. **Mobile** — three anchors at 380px + drawer pattern.

**Docs**
- `CLAUDE.md` — agent handoff guide (how to apply the system in the codebase).
- `BRIEF.md` — this file.
- `ACCESSIBILITY.md` — full WCAG contrast audit, focus/keyboard rules, ARIA guidance.
- `agentdrive.css` — single stylesheet, both themes, full responsive coverage.
- `index.html` — directory of all 15 mockups.

## What changed in v0.2

- **AA-passing color tokens.** Added `-strong` variants (`accent-strong`, `info-strong`, `warn-strong`, `danger-strong`) for text on tinted backgrounds. Added `--accent-fill` and `--accent-fill-hov` for button surfaces; the brand `--accent` stays for decorative use.
- **Dark mode** — refined dark tokens, system-preference fallback, every component checked.
- **Focus rings** — `:focus-visible` only, 3px gold glow, defined globally.
- **Skeleton loaders** — `.skeleton` with `.line`, `.title`, `.thumb`, `.btn`, `.avatar` variants.
- **Spinner**, **alert** (info/warn/danger/success), **deterministic avatar palette** (`.av[data-h="0..7"]`).
- **Truncation utilities** — `.trunc`, `.clamp-2`, `.clamp-3`, `.path-trunc` (middle-ellipsis), `.tbl-trunc` (fixed-layout table truncation).
- **Mobile** — sidebar collapses to drawer at `max-width:760px`. Mobile patterns documented in `15-mobile.html`.
- **Reduced-motion** support — `@media (prefers-reduced-motion: reduce)` honored.

## What's still future work

- Screen-reader walkthrough on the actual templates (mockups are static).
- Color-vision-deficiency simulation pass.
- Cognitive accessibility (plain-language) pass when end-users come into scope.
- Real-time data hooks for the live agent activity rail (engineering, not design).

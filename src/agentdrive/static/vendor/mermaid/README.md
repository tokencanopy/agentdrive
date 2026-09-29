# Vendored Mermaid

Draws the ` ```mermaid ` fences in markdown artifacts, on both the public
renderer and the private viewer, through `public/static/diagrams.js`.

- **Version**: mermaid **11.17.2** (pinned). The single-file IIFE build,
  `dist/mermaid.min.js` from the npm package, which defines `window.mermaid`
  and bundles every diagram type — no chunk loading, so the allowlist names
  exactly one file.
- **Upstream**: https://github.com/mermaid-js/mermaid, published as
  `mermaid` on npm (`https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz`).
- **License**: MIT — full text in `LICENSE`.

Vendored (not CDN-loaded) on purpose, like pdf.js beside it: both surfaces run
under `script-src 'self'` and pinning keeps rebuilds reproducible. It is
loaded **only** when a document contains a diagram — 3.5 MB of engine has no
business loading for a plain report — and it never runs in the page at all:
it runs inside `public/static/diagram-frame.html`, a same-origin frame served
with its own policy (`style-src 'unsafe-inline'`, which mermaid needs to
measure text, and `script-src 'self'`), and `diagrams.js` talks to that frame
over postMessage — diagram source text in, an SVG string out, shown as an
`<img>`. So the page keeps `style-src 'self'` exactly as it is, the engine
never shares a realm with the page or the private shell, and the drawn
diagram can neither run script nor reach the document. To upgrade: bump the
version, re-download `dist/mermaid.min.js` and `LICENSE` from the package,
and re-run the browser suite on both surfaces.

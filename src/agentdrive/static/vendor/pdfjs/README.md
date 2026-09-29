# Vendored PDF.js

Powers every PDF surface in AgentDrive through the shared
`static/pdfview.js` wrapper around Mozilla's `pdf_viewer` component layer:
the LaTeX **live PDF preview** (`/f/{fld_id}/preview`, scroll-preserving
in-place recompiles — see `docs/latex-live-preview-design.md`) and the
**artifact PDF file viewer** (`render/media.py`). Both get selectable text,
find-in-page, and clickable links from these files.

- **Version**: pdfjs-dist **5.4.149** (pinned) — bumped from 4.6.82 for the
  Signature annotation editor (`SignatureExtractor`) + annotation/serialization
  polish; the annotation tools are wired in `static/pdfview.js` (`attachAnnotation`).
- **Upstream**: https://github.com/mozilla/pdf.js (release `v5.4.149`), published
  as `pdfjs-dist` on npm. The committed bundles were fetched from the cdnjs
  mirror: https://cdnjs.cloudflare.com/ajax/libs/pdf.js/5.4.149/
- **Files**:
  - `pdf.min.mjs` (main API, ESM) + `pdf.worker.min.mjs` (worker)
  - `pdf_viewer.mjs` (the component layer: `PDFViewer`, `EventBus`,
    `PDFLinkService`, `PDFFindController`) + `pdf_viewer.css` (its required
    geometry stylesheet, loaded per-page via the `head_extra` block). The
    components operate on a `PDFDocumentProxy` loaded by `pdf.min.mjs`, so
    there is a single engine + worker — `pdf_viewer.mjs` never loads its own
    document.
  - `images/loading-icon.gif` (the page-loading spinner `pdf_viewer.css`
    references — the only image it pulls in our configuration; the rest of
    upstream's `images/` are annotation-editor chrome our viewers never
    enable, so they are deliberately not vendored). Fetched from the same
    cdnjs mirror path (`.../5.4.149/images/loading-icon.gif`).
- **License**: Apache-2.0 (Mozilla) — full text in `LICENSE`; each file also
  carries the `@license` header.

Vendored (not CDN-loaded) on purpose: the design system forbids external
script sources, the worker must be same-origin, and pinning keeps rebuilds
reproducible. To upgrade: bump the version, re-download all four files from the
authoritative upstream (or the cdnjs mirror at the same path), refresh
`LICENSE` if it changed, and re-test both the live preview and the artifact
PDF viewer.

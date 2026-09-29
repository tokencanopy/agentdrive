// PDF viewer for the PUBLIC surface — the visitor half of the legacy
// artifact-pdf.js, reusing `pdfview.js` unchanged.
//
// Legacy rejected the browser's native <embed> deliberately: pdf.js gives
// selectable text, find-in-page and clickable links, which <embed> does not.
// That reasoning still holds, so this reuses the same engine the LaTeX live
// preview uses rather than reimplementing anything.
//
// What is dropped relative to artifact-pdf.js is exactly the owner half:
// `attachAnnotation`, the annotation toolbar, and the authenticated PUT that
// saved an annotated copy back as a new version. A share-link recipient is
// not the owner and this surface carries no session, so annotation is not a
// feature we are withholding — it is one that cannot exist here.
import {
  attachFindBar,
  attachToolbar,
  attachZoom,
  createPdfView,
} from "./pdfview.js"; // module-relative — see pdfview.js header

const root = document.getElementById("pdf-doc");
if (root) {
  const zoomPct = document.getElementById("pv-zoom-pct");

  const view = createPdfView({
    container: document.getElementById("pv-container"),
    viewer: document.getElementById("pv-viewer"),
    annotation: false, // no editor layers for a visitor
    // "auto" leaves a page under-scaled in a wide container; a reader opening
    // a document wants it to fill the width they have.
    defaultScale: "page-width",
    onScale: (s) => {
      if (zoomPct && document.activeElement !== zoomPct) {
        zoomPct.value = `${Math.round(s * 100)}%`;
      }
    },
  });
  view.printUrl = root.dataset.pdfUrl;

  attachZoom(view, {
    zoomIn: document.getElementById("pv-zoom-in"),
    zoomOut: document.getElementById("pv-zoom-out"),
    pct: zoomPct,
  });
  attachToolbar(view, {
    prevPage: document.getElementById("pv-page-prev"),
    nextPage: document.getElementById("pv-page-next"),
    pageInput: document.getElementById("pv-page-input"),
    pageTotal: document.getElementById("pv-page-total"),
    fit: document.getElementById("pv-fit"),
    rotate: document.getElementById("pv-rotate"),
    print: document.getElementById("pv-print"),
  });
  attachFindBar(view, {
    toggle: document.getElementById("pv-find-toggle"),
    bar: document.getElementById("pv-find"),
    input: document.getElementById("pv-find-input"),
    count: document.getElementById("pv-find-count"),
    prev: document.getElementById("pv-find-prev"),
    next: document.getElementById("pv-find-next"),
    close: document.getElementById("pv-find-close"),
  });

  view.load(root.dataset.pdfUrl).catch(() => {
    // A corrupt or unsupported PDF must never strand the reader behind a
    // blank viewer — the bytes are still theirs to take.
    const host = document.getElementById("pv-container");
    if (host) {
      host.innerHTML =
        '<div class="pdf-doc-fallback">Couldn’t render this PDF in the ' +
        'browser. <a href="content" download>Download it instead.</a></div>';
    }
  });
}

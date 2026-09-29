// Artifact PDF file viewer (render/media.py).
//
// Renders a single static PDF through the shared pdf_viewer wrapper — the same
// engine the LaTeX live preview uses — so the artifact page gets selectable
// text, find-in-page, and clickable links instead of the browser's opaque
// native <embed>. No polling/live-update here; the document is loaded once.

import {
  createPdfView,
  attachFindBar,
  attachZoom,
  attachToolbar,
  attachAnnotation,
} from "./pdfview.js"; // module-relative — see pdfview.js header

const root = document.getElementById("pdf-doc");
if (root) {
  const container = document.getElementById("pv-container");
  const viewer = document.getElementById("pv-viewer");
  const zoomPct = document.getElementById("pv-zoom-pct");
  const isOwner = root.dataset.owner === "1";

  const view = createPdfView({
    container,
    viewer,
    annotation: isOwner, // editor layers + tools only for the owner
    onScale: (s) => {
      if (zoomPct && document.activeElement !== zoomPct) {
        zoomPct.value = `${Math.round(s * 100)}%`;
      }
    },
  });
  view.printUrl = root.dataset.pdfUrl; // print opens the raw PDF

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

  if (isOwner) {
    attachAnnotation(view, {
      buttons: document.querySelectorAll(".pdf-anno-bar [data-anno]"),
      save: document.getElementById("pv-anno-save"),
      status: document.getElementById("pv-anno-status"),
      // Persist the annotated PDF as a NEW version of this artifact. PUT the
      // saveDocument() bytes to the same drive-relative path; web_put_artifact
      // (owner + CSRF + quota + size-capped) creates the version.
      onSave: async (bytes) => {
        const res = await fetch(mountUrl(`/web/artifacts/${root.dataset.savePath}`), {
          method: "PUT",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/pdf",
            "X-CSRF-Token": root.dataset.csrf || "",
            // Target THIS artifact's own drive, not the active workspace (§4.3).
            "X-Drive-Id": root.dataset.driveId || "",
          },
          body: bytes,
        });
        if (!res.ok) throw new Error(`save ${res.status}`);
      },
    });
  }

  view
    .load(root.dataset.pdfUrl)
    .catch(() => {
      // Render failed (corrupt/unsupported PDF): fall back to a download link
      // so the artifact is never stranded behind a blank viewer.
      const host = document.getElementById("pv-container");
      if (host) {
        host.innerHTML =
          '<div class="pdf-doc-fallback">Couldn’t render this PDF in the browser. ' +
          '<a href="?raw=1" download>Download it instead.</a></div>';
      }
    });
}

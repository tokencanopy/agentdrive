const { wireReadingControls } = await import(
  `./reading.js${new URL(import.meta.url).search}`
);
// The private viewer shell — the document side of the console handshake.
//
// Security invariants, in order of importance:
//
//   1. The viewer credential exists ONLY in this module's closure. It is
//      never written to a URL, cookie, localStorage/sessionStorage, or the
//      DOM, and it is nulled the moment the last credentialed fetch
//      completes.
//   2. Every inbound message is validated on BOTH `event.source` (must be
//      our embedding parent) and `event.origin` (must be one of the
//      server-configured embed origins). Every outbound message names the
//      locked parent origin exactly — never `*`.
//   3. Rendered HTML arrives pre-escaped from the shared server renderer.
//      It is staged in a <template> (inert — nothing loads or runs) so
//      credential-protected subresources can be swapped to object URLs
//      before the content touches the live DOM.
//   4. Link clicks never navigate this frame. External links are forwarded
//      to the console, which decides whether and how to open them.
//
// Presentation, by contrast, is the CONSOLE's to decide. This frame is a
// document surface inside someone else's chrome, so it takes two instructions
// from the parent and reports one measurement back:
//
//   theme   the console's resolved light/dark. Following the OS instead
//           renders a dark document inside a light console, which reads as a
//           bug in the console — the reader already chose.
//   chrome  whether to draw the document header at all. Standalone it is the
//           only title on the page; embedded, the console already shows the
//           name, the path and the size directly above the frame, so drawing
//           it again states the same fact three times before any content.
//   height  reported outward — the rendered content height, so the console can
//           size the frame to the document instead of scrolling it inside a
//           fixed box.
//
// None of the three carries or gates a capability, so each is validated for
// shape and otherwise trusted. Only the credential is single-shot.

const configEl = document.getElementById("viewer-config");
const CONFIG = JSON.parse(configEl ? configEl.textContent : "{}");
const EMBED_ORIGINS = Array.isArray(CONFIG.embedOrigins) ? CONFIG.embedOrigins : [];
const PROTOCOL = 1;

const head = document.getElementById("shell-head");
const titleEl = document.getElementById("shell-title");
const pathEl = document.getElementById("shell-path");
const metaEl = document.getElementById("shell-meta");
const main = document.getElementById("shell-doc");

let credential = null;
let parentOrigin = null;
let started = false;
let showChrome = true;
let lastHeight = 0;
/* Whether the CONSOLE declared it can print. Default off, and deliberately
 * not inferred from being embedded: an older console that does not handle
 * the print messages would otherwise get a button whose click goes nowhere,
 * which is the one failure this surface has to avoid. */
let canPrint = false;
/* Whether the bytes for THIS document actually reached the console. Distinct
 * from `canPrint`, which is only the console's declaration: an oversized PDF
 * is never pushed, and a button whose click finds no bytes on the other side
 * is the same dead control the declaration exists to prevent. */
let printOffered = false;

/* A ceiling on what one tab is asked to keep alive for printing. Structured
 * cloning a Blob passes a reference rather than copying the bytes, so the
 * cost here is a second holder keeping the blob resident — not a second
 * copy. Matches the inline-video ceiling for the same reason it exists. */
const MAX_PRINTABLE_BYTES = 64 * 1024 * 1024;

// Stamp a theme immediately so `[data-theme]` is never absent: viewer.css
// hangs the dark palette (and all 79 syntax-highlight rules) off the
// attribute, treating the media query as a no-script fallback only. The
// console overrides this the moment it speaks; until then the frame is still
// hidden behind its pending state, so this value is only ever a floor.
applyTheme(window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

function applyTheme(theme) {
  if (theme !== "light" && theme !== "dark") return;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

function post(type, extra) {
  // Outbound only to the locked parent origin — never "*". Before the
  // handshake locks one, nothing is sent at all (the ready beacon below is
  // the one exception, and it carries no data).
  if (!parentOrigin) return;
  window.parent.postMessage(
    Object.assign({ type, protocol: PROTOCOL }, extra || {}),
    parentOrigin,
  );
}

/**
 * Tell the console how tall the document actually is.
 *
 * Only for modes whose content FLOWS. `pdf` is deliberately excluded: pdf.js
 * owns a scroll container sized to its host, so its scrollHeight is a function
 * of the frame it was given — reporting it would feed the console's own height
 * back to itself and ratchet the frame taller on every page render.
 *
 * Debounced against the last value because a ResizeObserver fires on
 * sub-pixel changes, and every report crosses an origin boundary.
 */
function contentHeight() {
  /* The BODY, not the documentElement. `documentElement.scrollHeight` never
   * reports less than the viewport, so a short document measured that way
   * returns the height of the frame it is already in — the console applies it,
   * nothing changes, and the frame never shrinks to fit a two-line note. The
   * body's own box is content-driven, which is the number the console needs. */
  return Math.ceil(
    Math.max(document.body.scrollHeight, document.body.getBoundingClientRect().height),
  );
}

function reportHeight(mode) {
  if (mode === "pdf") return;
  const height = contentHeight();
  if (!Number.isFinite(height) || height <= 0) return;
  if (Math.abs(height - lastHeight) < 2) return;
  lastHeight = height;
  post("agentdrive.viewer.resize", { height });
}

/** Watch for the late growth a first measurement cannot see: an image
 *  decoding, a font swapping, the reader widening the console. */
function watchHeight(mode) {
  if (mode === "pdf" || typeof ResizeObserver === "undefined") return;
  new ResizeObserver(() => reportHeight(mode)).observe(document.body);
}

function status(text) {
  main.className = "doc";
  main.replaceChildren();
  const card = document.createElement("div");
  card.className = "download-card";
  const p = document.createElement("p");
  p.textContent = text;
  card.appendChild(p);
  main.appendChild(card);
}

function fail(code, text) {
  credential = null;
  status(text);
  post("agentdrive.viewer.error", { code });
}

async function authFetch(path) {
  // The credential rides ONLY here — an Authorization header on a
  // same-origin fetch. Never a query parameter: request logs keep paths.
  return fetch(path, {
    headers: { authorization: "Bearer " + credential },
    credentials: "omit",
  });
}

async function contentBlob() {
  const resp = await authFetch("/view/content");
  if (!resp.ok) throw new Error("content " + resp.status);
  return resp.blob();
}

async function contentObjectUrl() {
  return URL.createObjectURL(await contentBlob());
}

/**
 * Hand the console the PDF's own bytes so its Print button can open them.
 *
 * An object URL is origin-scoped — `blob:<viewer-origin>/…` resolves only
 * here — so the console cannot be handed the URL. It has to be handed the
 * bytes and mint its own on its own origin, where a top-level tab can
 * reach them and the browser's native viewer prints them as vector.
 *
 * The Blob crosses by structured clone, which passes a reference to the
 * same underlying data rather than copying it and, unlike a transferred
 * ArrayBuffer, leaves this frame's copy intact for pdf.js to keep
 * rendering from.
 *
 * The credential does not cross and never has. What crosses is bytes the
 * reader is already looking at, to the origin they are looking at them
 * from.
 */
function offerPrintable(blob) {
  if (!canPrint || !blob) return;
  if (blob.size > MAX_PRINTABLE_BYTES) return;
  post("agentdrive.viewer.printable", { blob });
  printOffered = true;
}

function interceptLinks(root) {
  root.addEventListener("click", (event) => {
    // Only a REAL user click is forwarded. The parent turns an `open`
    // message into window.open — a capability this frame's sandbox
    // deliberately withholds — so a synthetic click dispatched by
    // document script must not be able to reach it.
    if (!event.isTrusted) return;
    const anchor = event.target && event.target.closest ? event.target.closest("a") : null;
    if (!anchor) return;
    const href = anchor.getAttribute("href") || "";
    if (href.startsWith("#")) return; // in-document navigation stays local
    event.preventDefault();
    // The console decides whether to open it — this frame never navigates
    // away from the document it was born to show, and a rendered link can
    // never turn the embedded frame into someone else's page.
    if (/^https?:\/\//i.test(href)) {
      post("agentdrive.viewer.open", { href });
    }
  });
}

function stripDownloadCards(fragment) {
  // Sandbox omits allow-downloads, so an in-frame download link is a dead
  // control. The console owns the download affordance (it has the product
  // token and no sandbox); tell it the document needs one.
  let stripped = false;
  for (const anchor of fragment.querySelectorAll(".download-card a[download]")) {
    // NOT the PDF shell's `<noscript>` fallback. Inside a `<template>` the
    // parser runs with scripting disabled, so noscript content is real
    // elements rather than inert text and this selector matched it — which
    // made every PDF report `needsDownload`, and the console tell the reader
    // that a document rendering perfectly in front of them "can't be shown
    // inline". The fallback is for a reader with no script at all; it is not
    // a signal about this render.
    if (anchor.closest("noscript")) continue;
    const note = document.createElement("p");
    note.textContent = "Use the console's download button to save this file.";
    anchor.replaceWith(note);
    stripped = true;
  }
  return stripped;
}

async function render(expected) {
  status("Loading document…");
  let resp;
  try {
    resp = await authFetch("/view/doc");
  } catch {
    return fail("network", "The viewer could not reach its server.");
  }
  if (resp.status === 401 || resp.status === 404) {
    return fail("expired", "This view has expired. Reopen it from the console.");
  }
  if (!resp.ok) {
    return fail("unavailable", "The document could not be loaded.");
  }
  const doc = await resp.json();

  // The credential is bound server-side to one drive/artifact/version; this
  // check catches the parent-side mixup (a stale credential handed to a
  // viewer showing a different artifact) before anything renders.
  if (
    expected &&
    (doc.binding.drive_id !== expected.drive_id ||
      doc.binding.artifact_id !== expected.artifact_id ||
      (expected.version_id && doc.binding.version_id !== expected.version_id))
  ) {
    return fail("binding-mismatch", "This view does not match the requested document.");
  }

  // The tab title is set either way — it is what a "reopen in a tab" flow and
  // the accessibility tree read, and it costs nothing when the frame is
  // embedded. The visible header is the part the console suppresses.
  document.title = doc.title;
  if (showChrome) {
    titleEl.textContent = doc.title;
    if (doc.path && doc.path !== doc.name) {
      pathEl.textContent = doc.path;
      pathEl.hidden = false;
    }
    metaEl.textContent =
      doc.size_human + (doc.updated_at ? " · updated " + doc.updated_at : "");
    head.hidden = false;
  }

  // Stage in an inert <template>: nothing fetches or runs until the
  // fragment is adopted, which is what lets credential-protected
  // subresources be rewritten first.
  const tpl = document.createElement("template");
  tpl.innerHTML = doc.html;
  const fragment = tpl.content;

  let needsDownload = stripDownloadCards(fragment);

  try {
    if (doc.mode === "image") {
      const img = fragment.querySelector("img.artifact-image");
      if (img) img.src = await contentObjectUrl();
    }
    if (doc.mode === "video" || doc.mode === "audio") {
      // Same swap as an image, and the same reason: `src="content"` is a
      // credentialed path the element cannot authenticate for itself. The
      // renderer caps inline video precisely because this is a whole-blob
      // fetch — the element gets bytes already in memory, not a stream.
      const video = fragment.querySelector("video.artifact-video, audio.artifact-audio");
      if (video) video.src = await contentObjectUrl();
    }
    let pdfUrl = null;
    if (doc.mode === "pdf") {
      // pdf.js loads from an object URL of the credential-fetched bytes —
      // the engine itself never sees the credential.
      const blob = await contentBlob();
      pdfUrl = URL.createObjectURL(blob);
      const root = fragment.querySelector("#pdf-doc");
      if (root) root.dataset.pdfUrl = pdfUrl;
      offerPrintable(blob);
    }

    main.className = "doc " + doc.mode;
    main.replaceChildren(fragment);
    interceptLinks(main);
    wireReadingControls(main, doc.mode);

    // Diagrams draw AFTER the document is in the tree (mermaid measures text
    // with getBBox, which needs a live element) and BEFORE the height is
    // reported, because a drawn diagram is taller than its source. The module
    // and the engine load only for a document that carries one; every other
    // document never pays for them. Neither fetch is credentialed.
    if (doc.diagrams && main.querySelector("[data-diagram]")) {
      const { renderDiagrams, followTheme } = await import(
        `./diagrams.js${new URL(import.meta.url).search}`
      );
      await renderDiagrams(main);
      followTheme(main);
    }

    /* The mode classes go on BEFORE pdf.js boots, and the order is load-bearing.
     *
     * `page-fit` is computed from the scroll container's clientHeight at
     * `pagesinit`. Setting these afterwards meant pdf.js measured a box that
     * had not yet been given its height, computed a NEGATIVE scale from
     * `(0 - padding) / pageHeight`, and rendered nothing behind a toolbar
     * reading "-5%". Whatever pdf.js measures has to be in its final box first.
     *
     * `embedded` also lets the stylesheet drop the standalone page's top margin:
     * the console's card already supplies that breathing room, and doubling it
     * spends the frame's first 40px on nothing. Mirrored onto <html> because a
     * full-height chain needs the ROOT sized and a body class cannot reach the
     * element above it. */
    document.body.className = "mode-" + doc.mode + (showChrome ? "" : " embedded");
    document.documentElement.dataset.mode = doc.mode;
    if (!showChrome) document.documentElement.dataset.reading = "contained";
    if (!showChrome) document.documentElement.dataset.embedded = "";

    if (doc.mode === "pdf") {
      await bootPdf();
    }
  } catch {
    return fail("unavailable", "The document could not be loaded.");
  }

  // Everything credentialed has been fetched; drop the credential now so a
  // later compromise of this frame has nothing left to steal.
  credential = null;
  post("agentdrive.viewer.rendered", {
    mode: doc.mode,
    needsDownload: needsDownload || doc.mode === "download",
    // Measured after the fragment is live, so it reflects the real document
    // rather than the placeholder it replaced.
    height: doc.mode === "pdf" ? null : contentHeight(),
  });
  watchHeight(doc.mode);
}

async function bootPdf() {
  // The public pdf-visitor wiring, inline: same pdfview.js engine, same
  // element ids, minus nothing — the only difference is where the document
  // bytes came from (an object URL instead of a relative fetch).
  const { attachFindBar, attachToolbar, attachZoom, createPdfView } =
    await import("./pdfview.js");
  const root = document.getElementById("pdf-doc");
  if (!root) return;
  const zoomPct = document.getElementById("pv-zoom-pct");
  const view = createPdfView({
    container: document.getElementById("pv-container"),
    viewer: document.getElementById("pv-viewer"),
    annotation: false,
    /* A whole page, not a page-width crop. In a frame this wide, page-width
     * resolves to ~147% — a US Letter page 1160px tall inside a 500px box, so
     * the reader saw a third of page one and two scrollbars. `page-fit` shows
     * the page the artifact actually is; zoom is right there for detail. */
    defaultScale: "page-fit",
    onScale: (s) => {
      if (zoomPct && document.activeElement !== zoomPct) {
        zoomPct.value = `${Math.round(s * 100)}%`;
      }
    },
  });
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
    // `print` is deliberately NOT passed: `attachToolbar` wires it to
    // `view.print()`, which is `window.open` — a capability this frame's
    // sandbox withholds. Printing is the console's to perform, exactly as
    // link opening already is.
  });
  const printBtn = document.getElementById("pv-print");
  if (printBtn) {
    // BOTH conditions: the console said it can print, AND this document's
    // bytes actually got there. Either alone leaves a button that does
    // nothing when clicked.
    if (canPrint && printOffered) {
      printBtn.addEventListener("click", (event) => {
        // Same trust gate as a forwarded link click: only a real user
        // gesture reaches the parent, so document script cannot make the
        // console open a tab on its behalf.
        if (!event.isTrusted) return;
        post("agentdrive.viewer.print");
      });
    } else {
      // Fails closed, for either reason: a console that did not declare
      // print support, or a document too large to hand over. The console's
      // own Download control remains the answer in both cases.
      printBtn.remove();
    }
  }
  attachFindBar(view, {
    toggle: document.getElementById("pv-find-toggle"),
    bar: document.getElementById("pv-find"),
    input: document.getElementById("pv-find-input"),
    count: document.getElementById("pv-find-count"),
    prev: document.getElementById("pv-find-prev"),
    next: document.getElementById("pv-find-next"),
    close: document.getElementById("pv-find-close"),
  });
  await view.load(root.dataset.pdfUrl).catch(() => {
    const host = document.getElementById("pv-container");
    if (host) {
      const fallback = document.createElement("div");
      fallback.className = "pdf-doc-fallback";
      fallback.textContent =
        "Couldn’t render this PDF here. Download required. Private download is not yet available in this view.";
      host.replaceChildren(fallback);
    }
    post("agentdrive.viewer.rendered", { mode: "pdf", needsDownload: true });
  });
}

// Keyboard events do not bubble out of an isolated iframe. The parent
// validates this source/origin before treating it as a dialog close request.
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") post("agentdrive.viewer.escape");
});

window.addEventListener("message", (event) => {
  // Sender checks first and unconditionally: both `event.source` (our
  // embedding parent) and `event.origin` (a configured embed origin) gate
  // EVERY message type, presentation ones included.
  if (event.source !== window.parent) return;
  if (!EMBED_ORIGINS.includes(event.origin)) return;
  const data = event.data;
  if (!data || data.protocol !== PROTOCOL) return;

  // Theme is repeatable, because the reader can toggle the console's theme
  // while a document is open. Safe to leave ungated by `started`: it sets one
  // attribute drawn from a two-value allowlist, reads nothing, and can neither
  // reach the credential nor cause a fetch.
  if (data.type === "agentdrive.viewer.theme") {
    applyTheme(data.theme);
    return;
  }

  if (data.type !== "agentdrive.viewer.credential") return;
  // Single-shot: exactly one credential per iframe lifetime. The console
  // remounts the iframe to show a different document, so a second message —
  // legitimate or not — has nothing it should be allowed to change.
  if (started) return;
  if (typeof data.credential !== "string" || data.credential.length === 0) return;
  started = true;
  parentOrigin = event.origin;
  credential = data.credential;
  // Both ride the credential message rather than a round trip of their own:
  // it is already the first thing the console says, and the header must be
  // resolved BEFORE the document paints or it flashes in and back out.
  applyTheme(data.theme);
  showChrome = data.chrome !== "none";
  // Strict `=== true`: an absent, truthy-but-not-true, or malformed value
  // leaves print off, so the button is drawn only for a console that has
  // actually said it handles the messages behind it.
  canPrint = data.print === true;
  render(data.expected || null).catch(() => {
    fail("unavailable", "The document could not be loaded.");
  });
});

// Announce readiness to every allowed embed origin. The message carries no
// data, and postMessage's targetOrigin filter means only the real parent —
// if it is one of these origins — ever receives it. With no origins
// configured, nothing is announced and the shell stays inert: fail closed.
for (const origin of EMBED_ORIGINS) {
  try {
    window.parent.postMessage(
      { type: "agentdrive.viewer.ready", protocol: PROTOCOL },
      origin,
    );
  } catch {
    // A malformed configured origin must not break the loop for the rest.
  }
}

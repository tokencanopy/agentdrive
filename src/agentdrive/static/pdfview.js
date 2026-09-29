// Shared PDF.js component-layer view.
//
// Wraps Mozilla's `pdf_viewer` components (PDFViewer + EventBus +
// PDFLinkService + PDFFindController) so every PDF surface in AgentDrive — the
// LaTeX live preview (preview.js) and the artifact file viewer — renders
// through ONE engine with selectable text, find-in-page, and clickable
// internal/external links. The document is loaded by our vendored pdf.min.mjs
// and handed to the components as a PDFDocumentProxy, so there is a single
// engine + worker (the components are bundled but operate on the proxy we
// pass; they never load their own document).
//
// Surfaces own their own chrome (toolbar, find bar, status); this module owns
// the engine wiring only.

// Module-relative imports (not "/static/…") — a root-absolute specifier
// resolves against the ORIGIN and bypasses mountUrl, 404ing when the app
// serves under MOUNT_PREFIX (staging retains the /drive compatibility mount). Relative
// specifiers resolve off this module's own URL, correct on both hosts.
import * as pdfjsLib from "./vendor/pdfjs/pdf.min.mjs";
import {
  PDFViewer,
  EventBus,
  PDFLinkService,
  PDFFindController,
} from "./vendor/pdfjs/pdf_viewer.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  "./vendor/pdfjs/pdf.worker.min.mjs",
  import.meta.url,
).href;

const MIN_SCALE = 0.25;
const MAX_SCALE = 4;

// container: the scroll host — position:absolute|relative, overflow:auto.
// viewer:    the inner `.pdfViewer` element pages mount into.
// onScale:   optional callback(scale) on every scale change (for a % label).
// defaultScale: pdf.js scale value applied on first load ("auto" | "page-width"
//   | "page-fit" | a number). "auto" fits width but caps very wide pages.
export function createPdfView({
  container,
  viewer,
  onScale,
  defaultScale = "auto",
  annotation = false,
}) {
  const eventBus = new EventBus();
  const linkService = new PDFLinkService({ eventBus });
  const findController = new PDFFindController({ eventBus, linkService });
  const pdfViewer = new PDFViewer({
    container,
    viewer,
    eventBus,
    linkService,
    findController,
    // NONE keeps the AnnotationEditorUIManager + per-page editor layers built
    // (so tools can be switched on) but no tool active; surfaces that don't opt
    // in omit it entirely (DISABLE-equivalent — no editor machinery).
    ...(annotation
      ? { annotationEditorMode: pdfjsLib.AnnotationEditorType.NONE }
      : {}),
  });
  linkService.setViewer(pdfViewer);

  let lastQuery = "";
  let currentDoc = null; // for saveDocument() (annotation persistence)
  /* A scale that could not be applied yet, because the scroll container had no
   * usable box when we tried. See `applyScale`. */
  let pendingScale = null;

  /**
   * Set the scale, but only against a container that has been laid out.
   *
   * `page-fit` and `page-width` are ratios of the container's box, so applying
   * one to a collapsed container produces nonsense — `(0 - padding) / 792` is
   * NEGATIVE, and pdf.js renders nothing behind a toolbar reading "-5%". That
   * is not hypothetical: an embedded viewer measured 0x0 at `pagesinit`,
   * because the frame it lives in had not been sized yet.
   *
   * Returns false when it deferred, and the caller parks the value for the
   * ResizeObserver below to retry the moment the box is real.
   */
  function applyScale(value) {
    if (container.clientHeight <= 0 || container.clientWidth <= 0) return false;
    pdfViewer.currentScaleValue = value;
    onScale?.(pdfViewer.currentScale);
    return true;
  }

  // Reflect every scale change to the consumer's % label.
  eventBus.on("scalechanging", (e) => onScale?.(e.scale));

  /* Re-fit when the container resizes.
   *
   * pdf.js resolves a NAMED scale ("page-fit", "page-width", "auto") once, at
   * `pagesinit`, against the container as it was then. Nothing re-resolved it
   * afterwards, so the page kept a scale computed for a box that no longer
   * existed: resizing the window left a "Page"-fitted document not fitting the
   * page, and in the console — which grows the frame once it learns the
   * document is a PDF — the first fit was computed against the smaller frame.
   *
   * Only named values are re-applied. A reader who typed 150% chose 150%, and
   * having it silently change on a window drag would be worse than not
   * refitting at all.
   *
   * The `clientHeight <= 0` guard is not defensive noise: `page-fit` divides by
   * the container height, so measuring a collapsed box yields a negative scale
   * and pdf.js renders nothing behind a toolbar reading "-5%". */
  if (typeof ResizeObserver !== "undefined") {
    let lastW = 0;
    let lastH = 0;
    new ResizeObserver(() => {
      const w = container.clientWidth;
      const h = container.clientHeight;
      if (w === lastW && h === lastH) return;
      lastW = w;
      lastH = h;
      if (!currentDoc || w <= 0 || h <= 0) return;
      // First duty: land a scale that was deferred because the box was not
      // laid out yet. This is the path that rescues the embedded viewer.
      if (pendingScale !== null) {
        if (applyScale(pendingScale)) pendingScale = null;
        return;
      }
      const value = pdfViewer.currentScaleValue;
      // A numeric value stringifies as "1.47"; a named one starts with a letter.
      if (typeof value !== "string" || /^[\d.]/.test(value)) return;
      applyScale(value); // re-resolves against the new box
    }).observe(container);
  }

  return {
    eventBus,
    pdfViewer,
    findController,
    linkService,

    // Load from an ArrayBuffer (live preview hands us bytes already fetched
    // with no-store) or a URL string (artifact viewer points at ?raw=1).
    // preserveScroll keeps the reader's scroll + scale across a recompile swap.
    async load(source, { preserveScroll = false } = {}) {
      // Capture the reader's place BEFORE the swap; restore it once the new
      // doc's pages initialise. A fresh load (no preserve) applies defaultScale.
      const keepScale = preserveScroll ? pdfViewer.currentScaleValue : null;
      const keepScroll = preserveScroll ? container.scrollTop : null;
      const params =
        source instanceof ArrayBuffer ? { data: source } : { url: source };
      const doc = await pdfjsLib.getDocument(params).promise;
      eventBus.on(
        "pagesinit",
        () => {
          const wanted = keepScale != null ? keepScale : defaultScale;
          if (!applyScale(wanted)) pendingScale = wanted;
          if (keepScroll != null) container.scrollTop = keepScroll;
        },
        { once: true },
      );
      pdfViewer.setDocument(doc);
      linkService.setDocument(doc, null);
      currentDoc = doc;
      return doc;
    },

    // Annotation editor (only meaningful when created with annotation:true).
    // mode is a pdfjsLib.AnnotationEditorType value; NONE exits to selection.
    // NB: the component-layer PDFViewer does NOT listen to the app's
    // "switchannotationeditormode" event — we set the property directly.
    setAnnotationMode(mode) {
      pdfViewer.annotationEditorMode = { mode };
    },
    get annotationMode() {
      return pdfViewer.annotationEditorMode;
    },
    // Serialize the document WITH its editor annotations to a new PDF byte
    // array (Uint8Array) — the surface uploads it as a new artifact version.
    async save() {
      if (!currentDoc) throw new Error("no document loaded");
      return currentDoc.saveDocument();
    },

    // Build a clickable section outline. Prefers the PDF's own bookmark tree
    // (hyperref papers have one) — clicking jumps via the link service. Falls
    // back to scanning rendered text for numbered headings (e.g. "2 Methods"),
    // where clicking scrolls to that page. Returns [{title, level, go}].
    async buildOutline() {
      if (!currentDoc) return [];
      const tree = await currentDoc.getOutline().catch(() => null);
      if (tree && tree.length) {
        const flat = [];
        const walk = (items, level) => {
          for (const it of items) {
            const dest = it.dest;
            flat.push({
              title: it.title,
              level,
              go: () => linkService.goToDestination(dest),
            });
            if (it.items?.length) walk(it.items, level + 1);
          }
        };
        walk(tree, 0);
        return flat;
      }
      // Fallback: numbered headings in the page text. Engines split the number
      // and title differently — pdflatex glues them into one item ("2Methods")
      // while XeTeX/Tectonic emit them as separate items ("2", then "Methods").
      // Handle both: a glued "N Title" item, OR a bare "N" item immediately
      // followed by a capitalised title item (the capital filters out body runs
      // like "40 steps").
      const gluedRe = /^(\d+(?:\.\d+)*)\s*([A-Z][A-Za-z][^\n]{0,60})$/;
      const numRe = /^(\d+(?:\.\d+)*)$/;
      const titleRe = /^([A-Z][A-Za-z][^\n]{0,60})$/;
      const seen = new Set();
      const headings = [];
      for (let n = 1; n <= currentDoc.numPages; n++) {
        const page = await currentDoc.getPage(n);
        const items = (await page.getTextContent()).items;
        for (let k = 0; k < items.length; k++) {
          const str = (items[k].str || "").trim();
          let num, title;
          const glued = gluedRe.exec(str);
          if (glued) {
            num = glued[1];
            title = glued[2];
          } else if (numRe.test(str)) {
            // bare section number — peek at the next non-empty item for the title
            let j = k + 1;
            while (j < items.length && !(items[j].str || "").trim()) j++;
            const tm = j < items.length ? titleRe.exec((items[j].str || "").trim()) : null;
            if (tm) {
              num = str;
              title = tm[1];
            }
          }
          if (!num) continue;
          const key = `${num} ${title}`;
          if (seen.has(key)) continue;
          seen.add(key);
          // transform[5] is the heading's y baseline in PDF user space (from the
          // page bottom); land the heading near the top of the viewport.
          const top = (items[k].transform?.[5] ?? 0) + (items[k].height || 16);
          headings.push({
            title: `${num}  ${title}`,
            level: (num.match(/\./g) || []).length,
            go: () =>
              pdfViewer.scrollPageIntoView({
                pageNumber: n,
                destArray: [null, { name: "XYZ" }, null, top, null],
              }),
          });
        }
      }
      return headings;
    },

    get pagesCount() {
      return pdfViewer.pagesCount;
    },
    get page() {
      return pdfViewer.currentPageNumber;
    },
    get pages() {
      return pdfViewer.pagesCount;
    },
    setPage(n) {
      const clamped = Math.min(Math.max(1, n | 0), pdfViewer.pagesCount || 1);
      if (clamped) pdfViewer.currentPageNumber = clamped;
    },
    rotate(delta) {
      // pdf.js normalises rotation to [0,360); keep it positive.
      pdfViewer.pagesRotation = (((pdfViewer.pagesRotation + delta) % 360) + 360) % 360;
    },
    get scale() {
      return pdfViewer.currentScale;
    },
    zoomBy(factor) {
      const next = Math.min(
        MAX_SCALE,
        Math.max(MIN_SCALE, pdfViewer.currentScale * factor),
      );
      pdfViewer.currentScale = next;
    },
    setScalePct(pct) {
      const n = pct / 100;
      if (!Number.isFinite(n) || n <= 0) return;
      pdfViewer.currentScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, n));
    },
    fit(mode = "auto") {
      pdfViewer.currentScaleValue = mode;
    },
    // The surface sets this to the raw PDF URL; print() opens it so the browser
    // prints the PDF itself (not the surrounding chrome). Re-set per recompile.
    printUrl: null,
    print() {
      if (this.printUrl) window.open(this.printUrl, "_blank", "noopener");
    },

    // Find-in-page. find() runs a fresh query; findAgain() steps next/prev over
    // the same query. Consumers listen on `eventBus` for "updatefindmatchescount"
    // and "updatefindcontrolstate" to drive a match counter / not-found state.
    find(query) {
      lastQuery = query;
      eventBus.dispatch("find", {
        source: this,
        type: "",
        query,
        caseSensitive: false,
        entireWord: false,
        highlightAll: true,
        findPrevious: false,
        matchDiacritics: false,
      });
    },
    findAgain(findPrevious) {
      if (!lastQuery) return;
      eventBus.dispatch("find", {
        source: this,
        type: "again",
        query: lastQuery,
        caseSensitive: false,
        entireWord: false,
        highlightAll: true,
        findPrevious,
        matchDiacritics: false,
      });
    },
    clearFind() {
      lastQuery = "";
      eventBus.dispatch("find", {
        source: this,
        type: "",
        query: "",
        caseSensitive: false,
        entireWord: false,
        highlightAll: false,
        findPrevious: false,
        matchDiacritics: false,
      });
    },
  };
}

// Wire a find bar's controls to a view. Shared by every PDF surface so the
// find UX (debounced query, Enter / Shift-Enter, prev/next, ⌘/Ctrl-F, the
// match counter that tracks navigation) stays identical everywhere.
// els: { toggle, bar, input, count, prev, next, close }.
export function attachFindBar(view, els) {
  const { toggle, bar, input, count, prev, next, close } = els;

  // Render from BOTH count + control-state events: the latter is what fires
  // on next/prev navigation, so the counter follows the selected match.
  const renderCount = (mc) => {
    if (mc && mc.total) {
      count.textContent = `${mc.current}/${mc.total}`;
      count.dataset.state = "ok";
    } else {
      count.textContent = input.value ? "0/0" : "";
      count.dataset.state = input.value ? "none" : "";
    }
  };
  view.eventBus.on("updatefindmatchescount", (e) => renderCount(e.matchesCount));
  view.eventBus.on("updatefindcontrolstate", (e) => renderCount(e.matchesCount));

  const open = () => {
    bar.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    input.focus();
    input.select();
  };
  const closeBar = () => {
    bar.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    count.textContent = "";
    count.dataset.state = "";
    view.clearFind();
  };

  toggle.addEventListener("click", () => (bar.hidden ? open() : closeBar()));
  close.addEventListener("click", closeBar);

  let debounce = null;
  input.addEventListener("input", () => {
    clearTimeout(debounce);
    const q = input.value;
    debounce = setTimeout(() => (q ? view.find(q) : view.clearFind()), 180);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      view.findAgain(e.shiftKey);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeBar();
    }
  });
  prev.addEventListener("click", () => view.findAgain(true));
  next.addEventListener("click", () => view.findAgain(false));

  // ⌘/Ctrl-F opens the in-document find bar instead of the browser's.
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "f" || e.key === "F")) {
      e.preventDefault();
      open();
    }
  });

  return { open, close: closeBar };
}

// Wire the zoom control: +/- buttons and (if els.pct is an <input>) an editable
// percentage the reader can type into. The % display updates via createPdfView's
// onScale callback. els: { zoomIn, zoomOut, pct? }.
export function attachZoom(view, els) {
  els.zoomOut.addEventListener("click", () => view.zoomBy(1 / 1.1));
  els.zoomIn.addEventListener("click", () => view.zoomBy(1.1));
  const pct = els.pct;
  if (pct && pct.tagName === "INPUT") {
    const commit = () => {
      const n = parseFloat(pct.value); // tolerant of a trailing "%"
      if (Number.isFinite(n) && n > 0) view.setScalePct(n);
      // reflect the actual (possibly clamped) scale back into the field
      pct.value = `${Math.round(view.scale * 100)}%`;
    };
    pct.addEventListener("focus", () => pct.select());
    pct.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commit();
        pct.blur();
      } else if (e.key === "Escape") {
        pct.blur();
      }
    });
    pct.addEventListener("blur", commit);
  }
}

// Wire the rest of the toolbar — page navigation, fit-mode cycle, rotate, and
// print. Every element is optional so a surface can wire only what it shows.
// els: { prevPage, nextPage, pageInput, pageTotal, fit, rotate, print }.
export function attachToolbar(view, els) {
  const { prevPage, nextPage, pageInput, pageTotal, fit, rotate, print } = els;

  const syncPage = () => {
    if (pageInput) pageInput.value = String(view.page || 1);
    if (pageTotal) pageTotal.textContent = String(view.pages || 0);
    if (prevPage) prevPage.disabled = view.page <= 1;
    if (nextPage) nextPage.disabled = view.page >= view.pages;
  };
  view.eventBus.on("pagechanging", syncPage);
  view.eventBus.on("pagesinit", syncPage);
  view.eventBus.on("rotationchanging", syncPage);

  prevPage?.addEventListener("click", () => view.setPage(view.page - 1));
  nextPage?.addEventListener("click", () => view.setPage(view.page + 1));
  pageInput?.addEventListener("change", () => {
    const n = parseInt(pageInput.value, 10);
    if (n) view.setPage(n);
    else syncPage();
  });

  // Cycle auto → fit-width → fit-page; the label/title reflects the next mode.
  const FITS = [
    { value: "auto", label: "Fit" },
    { value: "page-width", label: "Width" },
    { value: "page-fit", label: "Page" },
  ];
  let fitIdx = 0;
  fit?.addEventListener("click", () => {
    fitIdx = (fitIdx + 1) % FITS.length;
    view.fit(FITS[fitIdx].value);
    fit.dataset.fit = FITS[fitIdx].value;
    fit.title = `Fit: ${FITS[fitIdx].label}`;
  });

  rotate?.addEventListener("click", () => view.rotate(90));
  print?.addEventListener("click", () => view.print());
}

// Wire the annotation toolbar (owner-only surfaces). Each tool button carries
// data-anno="highlight|ink|freetext|signature"; clicking activates that editor,
// re-clicking returns to selection. The Save button serializes the annotated
// PDF and hands the bytes to onSave(bytes) (the surface owns the upload).
// els: { buttons: NodeList, save, status, onSave }.
export function attachAnnotation(view, { buttons, save, status, onSave }) {
  const T = pdfjsLib.AnnotationEditorType;
  const NAME_TO_TYPE = {
    highlight: T.HIGHLIGHT,
    ink: T.INK,
    freetext: T.FREETEXT,
    signature: T.SIGNATURE,
  };
  const list = Array.from(buttons || []);

  const reflect = (mode) => {
    for (const b of list) {
      const t = NAME_TO_TYPE[b.dataset.anno];
      b.classList.toggle("is-active", mode !== T.NONE && t === mode);
    }
  };

  let active = T.NONE;
  const setMode = (mode) => {
    active = mode;
    view.setAnnotationMode(mode);
    reflect(mode);
  };

  for (const b of list) {
    b.addEventListener("click", () => {
      const t = NAME_TO_TYPE[b.dataset.anno];
      if (t === undefined) return;
      setMode(active === t ? T.NONE : t); // toggle off if already active
    });
  }
  // The engine can change mode on its own (Esc, or auto-exit after placing an
  // element); keep the buttons in sync.
  view.eventBus.on("annotationeditormodechanged", ({ mode }) => {
    active = mode;
    reflect(mode);
  });

  if (save && onSave) {
    save.addEventListener("click", async () => {
      save.disabled = true;
      if (status) {
        status.textContent = "Saving…";
        status.dataset.state = "busy";
      }
      try {
        const bytes = await view.save();
        await onSave(bytes);
        if (status) {
          status.textContent = "Saved";
          status.dataset.state = "ok";
        }
      } catch (e) {
        if (status) {
          status.textContent = "Save failed";
          status.dataset.state = "err";
        }
      } finally {
        save.disabled = false;
      }
    });
  }
}

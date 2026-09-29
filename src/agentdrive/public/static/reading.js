/* Shared document controls. Both public pages and the private shell call this
   after adopting the safe renderer output. Never reads credentials or fetches
   artifact bytes. Artifact HTML cannot forge data-view controls (sanitizer). */
export function wireReadingControls(root, mode) {
  if (!root || root.dataset.readingControls) return;
  root.dataset.readingControls = "ready";
  if (mode === "pdf") return; // PDF.js owns find, zoom and its scroll container.

  const toolbar = root.querySelector(".doc-modes") || document.createElement("nav");
  toolbar.className = "doc-modes";
  toolbar.setAttribute("aria-label", "Document controls");
  const viewport = document.createElement("div");
  viewport.className = "reading-content";
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "region");
  viewport.setAttribute("aria-label", "Document content");
  for (const child of Array.from(root.childNodes)) {
    if (child !== toolbar) viewport.append(child);
  }
  root.append(toolbar, viewport);

  const button = (label, action, text = label) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "reading-button";
    el.textContent = text;
    if (text !== label) {
      el.setAttribute("aria-label", label);
      el.title = label;
    }
    el.addEventListener("click", action);
    toolbar.append(el);
    return el;
  };

  const previewNote = mode === "table" ? viewport.querySelector(".table-note") : null;
  let limit;
  if (previewNote) {
    limit = document.createElement("span");
    limit.className = "reading-limit";
    limit.textContent = previewNote.textContent;
    toolbar.append(limit);
  }

  let clearSearch = () => {};
  for (const control of toolbar.querySelectorAll("button[data-view]")) {
    control.addEventListener("click", () => {
      clearSearch();
      for (const pane of viewport.querySelectorAll(".doc-view[data-view]")) {
        pane.hidden = pane.dataset.view !== control.dataset.view;
      }
      for (const peer of toolbar.querySelectorAll("button[data-view]")) {
        const current = peer === control;
        peer.classList.toggle("is-current", current);
        peer.setAttribute("aria-pressed", String(current));
      }
      root.dataset.view = control.dataset.view;
      if (limit) limit.hidden = control.dataset.view === "source";
      viewport.scrollTop = 0;
    });
  }

  // A JSON tree: fold or unfold every node at once. Each node is a native
  // <details>, so the per-node toggle needs no script; these two only save
  // a reader four hundred clicks.
  const tree = viewport.querySelector(".json-tree");
  if (mode === "data" && tree) {
    const setAll = (open) => {
      for (const node of tree.querySelectorAll("details.json-node")) node.open = open;
    };
    button("Expand all", () => setAll(true));
    button("Collapse all", () => setAll(false));
  }

  if (["code", "markdown", "page", "document", "table", "sheet", "data"].includes(mode)) {
    if (mode !== "sheet") {
      const wrap = button("Wrap lines", () => {
        const enabled = viewport.classList.toggle("reading-wrap");
        wrap.setAttribute("aria-pressed", String(enabled));
      });
      wrap.setAttribute("aria-pressed", "false");
    }
    const input = document.createElement("input");
    input.type = "search";
    input.placeholder = "Find in document";
    input.setAttribute("aria-label", "Find in document");
    input.maxLength = 256;
    toolbar.append(input);
    const count = document.createElement("span");
    count.className = "reading-count";
    count.setAttribute("role", "status");
    let matches = [];
    let index = -1;
    let previousQuery = null;
    let caret = null; // the field's own selection, saved while a match holds the document's
    const selection = window.getSelection();
    clearSearch = () => {
      matches = [];
      index = -1;
      previousQuery = null;
      caret = null;
      count.textContent = "";
      selection?.removeAllRanges();
    };
    const find = (direction) => {
      const query = input.value;
      // Read the field's caret FIRST: clearSearch() drops the document
      // selection, and a focused field forgets its own selection with it. While
      // a match already holds the selection the field has forgotten it too, so
      // the saved caret is the true one.
      const fieldCaret = caret ?? [input.selectionStart, input.selectionEnd];
      if (!query) { clearSearch(); return; }
      if (query !== previousQuery) {
        clearSearch();
        previousQuery = query;
        const nodes = [];
        let text = "";
        const walker = document.createTreeWalker(viewport, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          if (!node.parentElement?.getClientRects().length ||
              node.parentElement.closest("[hidden], .workbook-tabs, .linenos")) continue;
          nodes.push({ node, start: text.length });
          text += node.textContent;
        }
        // Search the escaped/rendered text across syntax-highlight spans.
        // Bound stored ranges even for a source file full of one character.
        const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
        const position = (offset) => {
          let low = 0, high = nodes.length - 1;
          while (low < high) {
            const mid = Math.ceil((low + high) / 2);
            if (nodes[mid].start <= offset) low = mid;
            else high = mid - 1;
          }
          return [nodes[low].node, offset - nodes[low].start];
        };
        let match;
        while ((match = pattern.exec(text)) && matches.length < 1000) {
          const offset = match.index;
          const range = document.createRange();
          range.setStart(...position(offset));
          range.setEnd(...position(offset + match[0].length));
          matches.push(range);
        }
      }
      if (!matches.length) { count.textContent = "No matches"; return; }
      index = index === -1 ? (direction > 0 ? 0 : matches.length - 1)
        : (index + direction + matches.length) % matches.length;
      const range = matches[index];
      caret = fieldCaret;
      selection?.removeAllRanges();
      selection?.addRange(range);
      // Move the reading viewport only; scrollIntoView could move the console
      // and hide its toolbar when the match is inside an embedded document.
      let scroller = range.startContainer.parentElement.closest(".table-scroll") || viewport;
      if (scroller.scrollHeight <= scroller.clientHeight &&
          scroller.scrollWidth <= scroller.clientWidth) scroller = document.scrollingElement;
      const rect = range.getBoundingClientRect();
      const box = scroller === document.scrollingElement
        ? { top: 0, left: 0 } : scroller.getBoundingClientRect();
      scroller.scrollTop += rect.top - box.top - scroller.clientHeight / 2;
      scroller.scrollLeft += rect.left - box.left - scroller.clientWidth / 2;
      count.textContent = `${index + 1} of ${matches.length}${matches.length === 1000 ? "+" : ""}`;
    };
    // The step buttons leave focus in the field, as a browser find bar does:
    // step, then keep typing. Focus would otherwise land on the button and
    // the next keystroke would go nowhere.
    for (const step of [button("Previous match", () => find(-1), "↑"),
                        button("Next match", () => find(1), "↓")]) {
      step.addEventListener("mousedown", (event) => event.preventDefault());
    }
    toolbar.append(count);
    input.addEventListener("input", clearSearch);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); find(event.shiftKey ? -1 : 1); return; }
      if (event.key === "Escape") { input.value = ""; clearSearch(); return; }
      // A highlighted match IS the document selection, and editing follows
      // the selection rather than focus: with it sitting in the viewport, a
      // keystroke in the still-focused field inserted nothing, and the field
      // had also forgotten its caret. Hand the saved caret back before the key
      // is processed; the highlight goes with it, which is right — the next
      // `input` event clears the stale matches anyway.
      if (caret) { input.setSelectionRange(caret[0], caret[1]); caret = null; }
    });
    // Workbook fragment navigation changes the visible text without changing
    // the query. Rebuild the matches for the newly selected sheet.
    viewport.addEventListener("click", (event) => {
      if (event.target.closest?.(".workbook-tab")) clearSearch();
    });
  }

  if (mode === "image") {
    const img = viewport.querySelector("img.artifact-image");
    if (img) {
      button("Fit image", () => {
        viewport.dataset.imageZoom = "fit";
        img.removeAttribute("width");
      });
      button("Actual size", () => {
        viewport.dataset.imageZoom = "actual";
        if (img.naturalWidth) img.width = img.naturalWidth;
      });
      viewport.dataset.imageZoom = "fit";
    }
  }
  if (!toolbar.childNodes.length) toolbar.hidden = true;
}

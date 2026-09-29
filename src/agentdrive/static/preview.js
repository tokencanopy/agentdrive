// LaTeX workspace client (design_handoff_latex_workspace).
//
// One cookie-authed poll (/web/projects/{fld}/preview, every 2s, paused on
// document.hidden) is the single source of truth for the whole three-pane
// workspace: the live PDF (PDF.js, scroll-preserving), the status strip +
// diagnostics drawer, the read-only file tree, the source pane, and the ink
// agent-activity feed. The only mutation is the human Recompile button
// (POST .../compile) — after it returns `queued` the same poll renders the
// queued → running → success/error motion. Agents never stream characters;
// they re-upload whole files, so the "watch it rebuild" happens at
// version/compile granularity (a tree row flashes, a new PDF swaps in).

// Module-relative import (not "/static/pdfview.js") — see pdfview.js header.
import { createPdfView, attachFindBar, attachZoom, attachToolbar } from "./pdfview.js";

const POLL_MS = 2000;
const ENGINES = ["pdflatex", "xelatex", "lualatex"];

const root = document.querySelector(".preview");
const pollUrl = root.dataset.pollUrl;
const compileUrl = root.dataset.compileUrl;
const folderName = root.dataset.folder || "";
const csrf = document.getElementById("ws-csrf").value;

const viewerContainerEl = document.getElementById("ws-viewer-container");
const viewerEl = document.getElementById("ws-viewer");
const emptyEl = document.getElementById("preview-empty");
const statusEl = document.getElementById("preview-status");
const diagEl = document.getElementById("preview-diag");
const spinnerEl = document.getElementById("ws-spinner");
const statusDotEl = document.getElementById("ws-status-dot");
const progressEl = document.getElementById("ws-progress");

const treeBody = document.getElementById("ws-tree-body");
const fileCountEl = document.getElementById("ws-file-count");
const feedBody = document.getElementById("ws-feed-body");
const feedCountEl = document.getElementById("ws-feed-count");
const outlineEl = document.getElementById("ws-outline");
const outlineBodyEl = document.getElementById("ws-outline-body");

const codeInner = document.querySelector("#ws-code .inner");
const srcKindEl = document.getElementById("ws-src-kind");
const srcPathEl = document.getElementById("ws-src-path");
const srcVerEl = document.getElementById("ws-src-ver");
const srcByEl = document.getElementById("ws-src-by");
const srcAgoEl = document.getElementById("ws-src-ago");

const agentEl = document.getElementById("ws-agent");
const agentDotEl = document.getElementById("ws-agent-dot");
const agentWhoEl = document.getElementById("ws-agent-who");
const agentStateEl = document.getElementById("ws-agent-state");

const recompileEl = document.getElementById("ws-recompile");
const recompileBtn = document.getElementById("ws-recompile-btn");
const recompileCaret = document.getElementById("ws-recompile-caret");
const engineMenu = document.getElementById("ws-engine-menu");
const entryEl = document.getElementById("ws-entry");

const zoomOutBtn = document.getElementById("ws-zoom-out");
const zoomInBtn = document.getElementById("ws-zoom-in");
const zoomPctEl = document.getElementById("ws-zoom-pct");

const findToggle = document.getElementById("ws-find-toggle");
const findBar = document.getElementById("ws-find");
const findInput = document.getElementById("ws-find-input");
const findCount = document.getElementById("ws-find-count");
const findPrevBtn = document.getElementById("ws-find-prev");
const findNextBtn = document.getElementById("ws-find-next");
const findCloseBtn = document.getElementById("ws-find-close");

// Shared pdf.js component-layer view (renders into the #ws-viewer host). The
// zoom % label tracks the live scale; pages, text selection, find + links are
// owned by the view.
const pdfView = createPdfView({
  container: viewerContainerEl,
  viewer: viewerEl,
  defaultScale: "page-width", // fill the pane width (less side margin)
  onScale: (scale) => {
    // don't clobber the field while the reader is typing a custom %
    if (document.activeElement !== zoomPctEl) {
      zoomPctEl.value = `${Math.round(scale * 100)}%`;
    }
  },
});

let lastKey = null; // identity of the PDF on screen (change-detection)
let rendering = false; // guard against overlapping renders
let stopped = false; // set when the project is gone (404) — polling halts

let prevVersions = new Map(); // rel → version, for re-upload flash detection
let files = []; // latest file rows from the poll
let selectedRel = null; // source pane: which file is shown
let selectedVersion = null;
let userPickedFile = false; // once the user clicks a file, stop auto-following the entrypoint
let lastDiagnostics = [];
let lastFeedSig = null; // skip re-rendering (and re-animating) an unchanged feed
let lastTreeSig = null; // … and an unchanged file tree
let polling = false; // re-entrancy guard: one poll body at a time
let srcReqSeq = 0; // monotonic token so a slow source fetch can't clobber a newer pick

let selectedEngine = "pdflatex";
let engineUserSet = false;
let entrypoint = "";
let recompileBusy = false;
let compileSince = null; // when the current in-flight compile started (escape hatch)
const STUCK_MS = 25000; // re-enable Recompile if a compile stays in flight this long

// ─── helpers ─────────────────────────────────────────────────────────────

function ago(iso) {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 5) return "now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Drop the `agent:` prefix for display; the dot color carries the provider.
function shortActor(name) {
  if (!name) return "system";
  return name.startsWith("agent:") ? name.slice(6) : name;
}

const _CODE_EXT = new Set(["tex", "bib", "cls", "sty", "ltx"]);
const _IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp"]);
const GLYPH = { folder: "▢", code: "⟨ ⟩", image: "◈", bundle: "▤", text: "•" };

function kindOf(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (ext === "pdf") return "bundle";
  if (_CODE_EXT.has(ext)) return "code";
  if (_IMG_EXT.has(ext)) return "image";
  return "text";
}

// ─── LaTeX tokenizer (read-only highlight; ported from the prototype) ─────
// One pass per line: a leading `%` splits off a comment; the regex picks out
// commands (\foo), inline math ($…$), and braces. Everything else is text.

function tokenize(line) {
  const out = [];
  const ci = line.indexOf("%");
  let code = line;
  let comment = "";
  if (ci >= 0) {
    code = line.slice(0, ci);
    comment = line.slice(ci);
  }
  const re = /(\\[a-zA-Z@]+\*?|\$[^$]*\$|[{}[\]])/g;
  let last = 0;
  let m;
  while ((m = re.exec(code))) {
    if (m.index > last) out.push({ text: code.slice(last, m.index), cls: "tok-text" });
    const t = m[0];
    let cls = "tok-brace";
    if (t[0] === "\\") cls = "tok-cmd";
    else if (t[0] === "$") cls = "tok-math";
    out.push({ text: t, cls });
    last = m.index + t.length;
  }
  if (last < code.length) out.push({ text: code.slice(last), cls: "tok-text" });
  if (comment) out.push({ text: comment, cls: "tok-comment" });
  if (out.length === 0) out.push({ text: " ", cls: "tok-text" });
  return out;
}

// ─── file tree ───────────────────────────────────────────────────────────
// Build a nested folder/file structure from the flat rel paths, folders
// first (alpha), then files — the read-only mirror of what compiles.

function buildTreeRows(rows) {
  const root = { dirs: new Map(), files: [] };
  for (const f of rows) {
    const parts = f.rel.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const d = parts[i];
      if (!node.dirs.has(d)) node.dirs.set(d, { dirs: new Map(), files: [] });
      node = node.dirs.get(d);
    }
    node.files.push({ name: parts[parts.length - 1], file: f });
  }
  const out = [];
  (function walk(node, depth) {
    for (const [name, child] of [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      out.push({ type: "folder", name, depth });
      walk(child, depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
      out.push({ type: "file", name: f.name, depth, file: f.file });
    }
  })(root, 0);
  return out;
}

function renderTree() {
  fileCountEl.textContent = `${files.length} file${files.length === 1 ? "" : "s"}`;
  const rows = buildTreeRows(files);
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const el = document.createElement(r.type === "file" ? "button" : "div");
    el.className = "ft-row" + (r.type === "folder" ? " is-folder" : "");
    el.style.paddingLeft = `${8 + r.depth * 16}px`;
    if (r.type === "file") el.type = "button";

    const kind = r.type === "folder" ? "folder" : kindOf(r.name);
    const g = document.createElement("span");
    g.className = "glyph";
    g.dataset.k = kind; // CSS colors the glyph by kind (matches .kind[data-k])
    g.textContent = GLYPH[kind] || GLYPH.text;
    el.appendChild(g);

    const nm = document.createElement("span");
    nm.className = "ft-name";
    nm.textContent = r.name;
    el.appendChild(nm);

    if (r.type === "file") {
      const f = r.file;
      if (f.is_entrypoint) {
        el.classList.add("is-entry");
        const b = document.createElement("span");
        b.className = "badge solid-accent ft-entry";
        b.textContent = "entry";
        el.appendChild(b);
      }
      const v = document.createElement("span");
      v.className = "ft-ver";
      v.textContent = `v${f.version}`;
      el.appendChild(v);

      if (selectedRel === f.rel) el.classList.add("is-active");
      const prev = prevVersions.get(f.rel);
      if (prev != null && prev !== f.version) el.classList.add("is-flash");
      el.addEventListener("click", () => selectFile(f, { manual: true }));
    }
    frag.appendChild(el);
  }
  treeBody.replaceChildren(frag);
}

// ─── source pane ─────────────────────────────────────────────────────────

function renderSourceHeader(f) {
  const slash = f.rel.lastIndexOf("/");
  const dir = slash >= 0 ? f.rel.slice(0, slash + 1) : "";
  const name = slash >= 0 ? f.rel.slice(slash + 1) : f.rel;
  // Kind chip reflects the actual selected file (was hardcoded "tex").
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "";
  srcKindEl.dataset.k = kindOf(name);
  srcKindEl.textContent = `</> ${ext || name}`;
  srcPathEl.replaceChildren();
  if (dir) {
    const d = document.createElement("span");
    d.className = "dir";
    d.textContent = dir;
    srcPathEl.appendChild(d);
  }
  const n = document.createElement("span");
  n.className = "name";
  n.textContent = name;
  srcPathEl.appendChild(n);

  srcVerEl.textContent = `v${f.version}`;
  if (f.actor_name) {
    srcByEl.hidden = false;
    srcByEl.textContent = shortActor(f.actor_name);
    srcByEl.className = "agent-tag " + (f.actor_name.startsWith("agent:") ? "claude" : "user");
  } else {
    srcByEl.hidden = true;
  }
  srcAgoEl.dataset.ts = f.updated_at || ""; // ticked by updateRelativeTimes
  srcAgoEl.textContent = ago(f.updated_at);
}

// Diagnostics carry (file, line); underline the offending line red when the
// source on screen is the file that failed.
function errorLinesFor(rel) {
  const set = new Set();
  const base = rel.split("/").pop();
  for (const d of lastDiagnostics) {
    if (!d.line) continue;
    const df = d.file || "";
    if (df === rel || df.endsWith("/" + rel) || df === base || df.endsWith("/" + base)) {
      set.add(d.line);
    }
  }
  return set;
}

function renderSourceBody(text, rel) {
  const errs = errorLinesFor(rel);
  const lines = text.replace(/\n$/, "").split("\n");
  const frag = document.createDocumentFragment();
  lines.forEach((line, i) => {
    const row = document.createElement("div");
    row.className = "code-line";
    if (errs.has(i + 1)) row.classList.add("ln-err");

    const gutter = document.createElement("span");
    gutter.className = "ln-gutter";
    gutter.textContent = String(i + 1);
    row.appendChild(gutter);

    const body = document.createElement("span");
    body.className = "ln-text";
    // Untrusted file bytes — build tokens via textContent, never innerHTML.
    for (const tok of tokenize(line)) {
      const s = document.createElement("span");
      s.className = tok.cls;
      s.textContent = tok.text;
      body.appendChild(s);
    }
    row.appendChild(body);
    frag.appendChild(row);
  });
  codeInner.replaceChildren(frag);
}

async function selectFile(f, { manual = false } = {}) {
  if (manual) userPickedFile = true;
  selectedRel = f.rel;
  selectedVersion = f.version;
  const seq = ++srcReqSeq; // newest pick wins, even for two fetches of the same file
  renderSourceHeader(f);
  lastTreeSig = null; // force the next poll to re-mark the active row
  renderTree(); // re-mark the active row now

  // The source pane is text-only. Don't fetch + tokenize a binary file
  // (image / PDF) — that would render mojibake as "LaTeX source".
  const base = f.rel.split("/").pop();
  if (kindOf(base) === "image" || kindOf(base) === "bundle") {
    const note = document.createElement("div");
    note.className = "notice";
    note.textContent = "Binary file — open it from the drive to view.";
    codeInner.replaceChildren(note);
    return;
  }
  try {
    const text = await fetch(f.raw_url, { cache: "no-store", credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error(`src ${r.status}`);
        return r.text();
      });
    if (seq === srcReqSeq) renderSourceBody(text, f.rel);
  } catch (_e) {
    /* leave the previous source on screen on a transient fetch error */
  }
}

// ─── activity feed (ink) ─────────────────────────────────────────────────

function feedText(e) {
  switch (e.kind) {
    case "upload":
      return `uploaded ${e.path || "a file"}${e.version ? ` → v${e.version}` : ""}`;
    case "compiled":
      return `compiled main.pdf${e.pages != null ? ` · ${e.pages}p` : ""}${e.engine ? ` · ${e.engine}` : ""}`;
    case "failed":
      return `compile failed${e.engine ? ` · ${e.engine}` : ""}`;
    case "queued":
      return `${e.actor_name === "you" ? "recompile" : "queued"}${e.engine ? ` · ${e.engine}` : ""}`;
    default:
      return e.kind;
  }
}

function renderFeed(events) {
  const frag = document.createDocumentFragment();
  for (const e of events || []) {
    const row = document.createElement("div");
    row.className = "ev" + (e.actor_name === "you" ? " is-you" : "");
    row.dataset.kind = e.kind;

    const mk = document.createElement("span");
    mk.className = "marker";
    row.appendChild(mk);

    const body = document.createElement("div");
    body.className = "body";
    const what = document.createElement("div");
    what.className = "what";
    what.textContent = feedText(e);
    body.appendChild(what);
    const who = document.createElement("div");
    who.className = "who";
    // data-ts + data-actor let the relative-time ticker refresh "Ns ago"
    // in place without rebuilding the feed (which would re-flash it).
    who.dataset.actor = shortActor(e.actor_name);
    who.dataset.ts = e.created_at || "";
    who.textContent = `${who.dataset.actor} · ${ago(e.created_at)}`;
    body.appendChild(who);
    row.appendChild(body);
    frag.appendChild(row);
  }
  feedBody.replaceChildren(frag);
}

// Refresh every relative timestamp in place (feed rows + source byline) so
// "Ns ago" keeps ticking even though the feed only re-renders on real change.
function updateRelativeTimes() {
  for (const el of document.querySelectorAll("[data-ts]")) {
    if (!el.dataset.ts) continue;
    el.textContent = el.dataset.actor != null
      ? `${el.dataset.actor} · ${ago(el.dataset.ts)}`
      : ago(el.dataset.ts);
  }
}

// ─── last-agent-activity pill ────────────────────────────────────────────
// Shows the agent that last touched the project + how long ago (real, from
// the events feed). Goes live ("compiling…" + gold pulse) only while an
// agent-triggered compile is actually in flight. NOT presence — there's no
// heartbeat backend, so we never claim the agent is "idle"/"online".

function updateAgent(d) {
  const events = d.recent_events || [];
  let handle = null;
  let lastTs = null;
  for (const e of events) {
    if (e.actor_name && e.actor_name !== "you" && e.actor_name !== "system") {
      handle = e.actor_name;
      lastTs = e.created_at;
      break;
    }
  }
  if (!handle) {
    agentEl.hidden = true; // no agent has touched this project yet
    return;
  }
  agentEl.hidden = false;
  agentWhoEl.textContent = handle;

  // Live only if an AGENT (not "you") triggered the in-flight compile.
  const inFlight = d.status === "queued" || d.status === "running";
  let humanTriggered = false;
  for (const e of events) {
    if (e.kind === "queued" || e.kind === "compiled" || e.kind === "failed") {
      humanTriggered = e.actor_name === "you";
      break;
    }
  }
  const compiling = inFlight && !humanTriggered;
  agentEl.classList.toggle("is-editing", compiling);
  // `.pulse` is gold, `.idle-dot` grey — no inline color needed.
  agentDotEl.className = compiling ? "pulse" : "idle-dot";
  if (compiling) {
    delete agentStateEl.dataset.ts; // stop the relative-time ticker
    agentStateEl.textContent = "compiling…";
  } else {
    agentStateEl.dataset.ts = lastTs || ""; // ticked live; historical, not presence
    agentStateEl.textContent = lastTs ? ago(lastTs) : "";
  }
}

// ─── diagnostics (ink; bottom drawer) ────────────────────────────────────
// TeX logs are untrusted text — build via textContent, never innerHTML.
//
// The drawer is height-resizable (drag its top edge) and collapsible (click
// its header). Both persist to localStorage — kept separate from the pane-width
// layout so a tall/short log preference sticks independently across visits.

const DIAG_STORE = "agentdrive-latex-diag";
const DIAG_MIN_H = 90;
let lastDiagSig = null; // skip rebuilding an unchanged drawer on every poll
let diagDragging = false; // true mid-resize — suppresses the poll rebuild
let diagLayout;
try {
  // tolerate a corrupt/hostile value: only a real object is usable. A truthy
  // JSON primitive (5/true/"x") would otherwise survive `|| {}` and then throw
  // on the first `diagLayout.h = …` under module strict mode.
  const parsed = JSON.parse(localStorage.getItem(DIAG_STORE));
  diagLayout = parsed && typeof parsed === "object" ? parsed : {};
} catch {
  diagLayout = {};
}
function saveDiagLayout() {
  try {
    localStorage.setItem(DIAG_STORE, JSON.stringify(diagLayout));
  } catch {
    /* private mode / quota — layout just won't persist */
  }
}
function applyDiagLayout() {
  const h = Number(diagLayout.h);
  if (Number.isFinite(h) && h > 0) diagEl.style.setProperty("--h-diag", `${h}px`);
  diagEl.classList.toggle("is-collapsed", !!diagLayout.collapsed);
}
const diagMaxH = () => Math.round(window.innerHeight * 0.72);
function setDiagHeight(h) {
  if (!Number.isFinite(h)) return; // guard against a corrupt persisted height
  diagLayout.h = Math.round(Math.max(DIAG_MIN_H, Math.min(diagMaxH(), h)));
  diagEl.style.setProperty("--h-diag", `${diagLayout.h}px`);
}

function renderDiagnostics(list) {
  if (!list || !list.length) {
    diagEl.hidden = true;
    diagEl.replaceChildren();
    lastDiagSig = null;
    return;
  }
  // renderDiagnostics runs every poll (~2s). A blind rebuild would (a) interrupt
  // an in-progress resize drag — replaceChildren detaches the pointer-capturing
  // handle, which silently releases capture and kills the gesture — and (b) reset
  // the reader's scroll position in the rows. So never rebuild mid-drag, and
  // otherwise only when the diagnostics actually changed. Height/collapse live on
  // diagEl itself, so a skipped render keeps them; re-assert via applyDiagLayout.
  if (diagDragging) return;
  const sig = JSON.stringify(
    list.map((d) => [d.severity, d.file, d.line, d.message, d.suggestion]),
  );
  if (sig === lastDiagSig && !diagEl.hidden) {
    applyDiagLayout();
    return;
  }
  lastDiagSig = sig;

  // top-edge drag handle — resize the drawer height (it grows upward, eating
  // into the PDF pane rather than overflowing the viewport).
  const resize = document.createElement("div");
  resize.className = "diag-resize";
  resize.setAttribute("role", "separator");
  resize.setAttribute("aria-orientation", "horizontal");
  resize.setAttribute("aria-label", "Resize diagnostics");
  resize.tabIndex = 0;
  resize.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = diagEl.getBoundingClientRect().height;
    resize.classList.add("is-dragging");
    diagDragging = true;
    resize.setPointerCapture(e.pointerId);
    const onMove = (ev) => setDiagHeight(startH + (startY - ev.clientY));
    const onUp = () => {
      resize.classList.remove("is-dragging");
      diagDragging = false;
      resize.removeEventListener("pointermove", onMove);
      resize.removeEventListener("pointerup", onUp);
      resize.removeEventListener("pointercancel", onUp);
      saveDiagLayout();
    };
    resize.addEventListener("pointermove", onMove);
    resize.addEventListener("pointerup", onUp);
    // pointercancel/implicit capture loss (e.g. the rare mid-drag rebuild) still
    // clears the flag and persists, so a gesture can never strand diagDragging.
    resize.addEventListener("pointercancel", onUp);
  });
  resize.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 40 : 12;
    let delta = 0;
    if (e.key === "ArrowUp") delta = step; // taller
    else if (e.key === "ArrowDown") delta = -step;
    else return;
    e.preventDefault();
    setDiagHeight((Number(diagLayout.h) || diagEl.getBoundingClientRect().height) + delta);
    saveDiagLayout();
  });

  const head = document.createElement("div");
  head.className = "diag-head";
  const eb = document.createElement("span");
  eb.className = "diag-eyebrow";
  eb.textContent = "Diagnostics";
  head.appendChild(eb);
  const errs = list.filter((x) => (x.severity || "error") === "error").length;
  const cnt = document.createElement("span");
  cnt.className = "diag-count";
  cnt.textContent = `${errs || list.length} ${(errs || list.length) === 1 ? "issue" : "issues"}`;
  head.appendChild(cnt);

  // collapse toggle — clicking the chevron (or anywhere on the header) folds
  // the drawer down to just this bar; aria-expanded tracks the rows.
  const collapseBtn = document.createElement("button");
  collapseBtn.type = "button";
  collapseBtn.className = "diag-collapse";
  collapseBtn.textContent = "▾";
  head.appendChild(collapseBtn);
  const syncCollapseAria = () => {
    const expanded = !diagLayout.collapsed;
    collapseBtn.setAttribute("aria-expanded", String(expanded));
    // label tracks state so AT announces the right action (collapse vs expand)
    collapseBtn.setAttribute("aria-label", expanded ? "Collapse diagnostics" : "Expand diagnostics");
  };
  const toggleCollapsed = () => {
    diagLayout.collapsed = !diagLayout.collapsed;
    applyDiagLayout();
    syncCollapseAria();
    saveDiagLayout();
  };
  collapseBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleCollapsed();
  });
  head.addEventListener("click", toggleCollapsed);
  syncCollapseAria();

  const rowsWrap = document.createElement("div");
  rowsWrap.className = "diag-rows";
  for (const d of list) {
    const row = document.createElement("div");
    row.className = "diag-row";
    row.dataset.sev = d.severity || "error";

    const sev = document.createElement("span");
    sev.className = "diag-sev";
    sev.textContent = d.severity || "error";
    row.appendChild(sev);

    if (d.file) {
      const loc = document.createElement("span");
      loc.className = "diag-loc";
      loc.textContent = d.line ? `${d.file}:${d.line}` : d.file;
      row.appendChild(loc);
    }
    const msg = document.createElement("span");
    msg.className = "diag-msg";
    msg.textContent = d.message || "";
    row.appendChild(msg);

    if (d.suggestion) {
      const fix = document.createElement("span");
      fix.className = "diag-fix";
      fix.textContent = d.suggestion;
      row.appendChild(fix);
    }
    rowsWrap.appendChild(row);
  }

  diagEl.replaceChildren(resize, head, rowsWrap);
  diagEl.hidden = false;
  applyDiagLayout();
}

// ─── live PDF (pdf.js component layer; scroll-preserving) ────────────────
//
// Rendering goes through the shared pdfview.js (Mozilla's PDFViewer), which
// gives selectable text, find-in-page, and clickable links out of the box. We
// fetch the bytes ourselves (no-store, cookie-authed) and hand them to the
// view; on a recompile the same call swaps the document in place and the view
// restores the reader's scroll + zoom.

let pdfLoaded = false; // first successful load flips this (preserve from #2 on)

async function renderPdf(rawUrl) {
  if (rendering) return;
  rendering = true;
  try {
    const buf = await fetch(rawUrl, { cache: "no-store", credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error(`pdf fetch ${r.status}`);
        return r.arrayBuffer();
      });
    showViewer();
    pdfView.printUrl = rawUrl; // print opens the raw PDF (updated each recompile)
    await pdfView.load(buf, { preserveScroll: pdfLoaded });
    pdfLoaded = true;
    buildOutlineUI(); // refresh the section outline for this version
  } finally {
    rendering = false;
  }
}

// Toggle the empty/no-preview overlay vs. the live pdf_viewer host.
function showViewer() {
  emptyEl.hidden = true;
  viewerContainerEl.hidden = false;
}

// Section outline (PDF bookmarks → numbered-heading fallback). Clicking jumps
// the PDF; if the PDF pane is hidden, reveal it first via the chip.
async function buildOutlineUI() {
  let items = [];
  try {
    items = await pdfView.buildOutline();
  } catch {
    items = [];
  }
  if (!items.length) {
    outlineEl.hidden = true;
    outlineBodyEl.replaceChildren();
    return;
  }
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "ws-ol-row";
    row.style.setProperty("--lvl", String(Math.min(it.level || 0, 3)));
    row.textContent = it.title;
    row.addEventListener("click", () => {
      const pdfChip = document.querySelector('.ws-panes [data-pane="pdf"]');
      if (pdfChip && !pdfChip.classList.contains("is-on")) pdfChip.click();
      it.go();
    });
    frag.appendChild(row);
  }
  outlineBodyEl.replaceChildren(frag);
  outlineEl.hidden = false;
}

function setStatus(state, text, aside) {
  statusEl.dataset.state = state;
  statusEl.textContent = text;
  // `aside` (e.g. "3m ago") rides along inside the colored status so it reads
  // as one line, but is aria-hidden so the relative-time tick doesn't spam the
  // live region — only the state text ("Updated · 2p · …") is announced.
  if (aside) {
    const span = document.createElement("span");
    span.className = "preview-status-ago";
    span.setAttribute("aria-hidden", "true");
    span.textContent = ` · ${aside}`;
    statusEl.appendChild(span);
  }
}

function setEmpty() {
  // Hide the pdf_viewer host and show the rich "no preview yet" overlay.
  viewerContainerEl.hidden = true;
  emptyEl.hidden = false;
  const wrap = document.createElement("div");
  wrap.className = "ws-empty";
  wrap.innerHTML =
    '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--fg-subtle)" stroke-width="1.4" aria-hidden="true"><path d="M6 2h9l5 5v15H6Z"/><path d="M14 2v6h6"/><path d="M9 14h6M9 18h6M9 10h2"/></svg>';
  const h = document.createElement("div");
  const t = document.createElement("h3");
  t.textContent = "No preview yet";
  const p = document.createElement("p");
  p.textContent =
    "This project hasn't been compiled. Ask your agent to compile the folder — the PDF appears here and refreshes on every recompile.";
  h.append(t, p);
  // Real, copy-pasteable MCP call for THIS folder (was a `{fld}` placeholder).
  const snip = document.createElement("div");
  snip.className = "snippet";
  const span = (cls, text) => {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  };
  snip.append(
    span("c", "# via MCP"),
    document.createElement("br"),
    span("m", "compile"),
    span("t", " {folder: "),
    span("s", `"${folderName}"`),
    span("t", "}"),
  );
  wrap.append(h, snip);
  emptyEl.replaceChildren(wrap);
}

// ─── status strip + the per-tick UI from one poll envelope ───────────────

function renderStrip(d) {
  const diags = d.diagnostics || [];
  const errs = diags.filter((x) => x.severity === "error").length;
  const warns = diags.filter((x) => x.severity === "warning").length;
  const compiling = d.status === "queued" || d.status === "running";

  switch (d.status) {
    case "none":
      setStatus("muted", "No preview yet");
      break;
    case "queued":
    case "running":
      setStatus("live", "Compiling…");
      break;
    case "success":
      setStatus("ok", `Updated · ${d.pdf ? d.pdf.pages : "?"}p${d.engine ? ` · ${d.engine}` : ""}${warns ? ` · ${warns} warning${warns === 1 ? "" : "s"}` : ""}`, d.updated_at ? ago(d.updated_at) : "");
      break;
    case "timeout":
      setStatus("err", "Compile timed out");
      break;
    case "error":
      if (errs) setStatus("err", `Compile failed — ${errs} issue${errs === 1 ? "" : "s"}`);
      else if (diags.length) setStatus("err", `Compile failed — ${diags.length} issue${diags.length === 1 ? "" : "s"}`);
      else setStatus("err", "Compile failed");
      break;
    default:
      setStatus("err", d.status);
  }

  spinnerEl.hidden = !compiling;
  if (compiling || d.status === "none") {
    statusDotEl.hidden = true;
  } else {
    statusDotEl.hidden = false;
    statusDotEl.dataset.state = d.status === "success" ? "ok" : "err"; // CSS colors it
  }

  progressEl.classList.toggle("is-on", compiling);
  if (compiling && !progressEl.firstChild) {
    const bar = document.createElement("span");
    bar.className = "bar";
    progressEl.appendChild(bar);
  } else if (!compiling) {
    progressEl.replaceChildren();
  }
  // (page count now lives in the toolbar's page-nav, driven by the viewer.)

  lastDiagnostics = diags;
  renderDiagnostics(diags);
  // Disable Recompile while a compile is in flight — but never trap the user:
  // if a job wedges (worker down/slow) and stays in flight past STUCK_MS,
  // re-enable so they can re-trigger (enqueue coalesces identical input).
  if (compiling) {
    if (compileSince == null) compileSince = Date.now();
    setRecompileDisabled(Date.now() - compileSince < STUCK_MS);
  } else {
    compileSince = null;
    setRecompileDisabled(false);
  }
}

// ─── recompile control ───────────────────────────────────────────────────

function markEngine() {
  for (const b of engineMenu.querySelectorAll(".rc-engine")) {
    const active = b.dataset.engine === selectedEngine;
    b.classList.toggle("is-active", active);
    b.querySelector(".mark").textContent = active ? "●" : "○";
  }
}

function closeMenu() {
  engineMenu.hidden = true;
  recompileCaret.setAttribute("aria-expanded", "false");
}

function setRecompileDisabled(disabled) {
  recompileBusy = disabled;
  recompileEl.setAttribute("aria-disabled", disabled ? "true" : "false");
  if (disabled) closeMenu();
}

recompileCaret.addEventListener("click", (e) => {
  e.stopPropagation();
  if (recompileBusy) return;
  const open = engineMenu.hidden;
  engineMenu.hidden = !open;
  recompileCaret.setAttribute("aria-expanded", open ? "true" : "false");
});

engineMenu.addEventListener("click", (e) => {
  const btn = e.target.closest(".rc-engine");
  if (!btn) return;
  e.stopPropagation();
  selectedEngine = btn.dataset.engine;
  engineUserSet = true;
  markEngine();
  closeMenu();
});

document.addEventListener("click", closeMenu);

// ─── zoom + find ─────────────────────────────────────────────────────────
// Shared wiring (pdfview.js) so the LaTeX preview and the artifact PDF viewer
// behave identically. The zoom % label updates via createPdfView's onScale.
attachZoom(pdfView, { zoomIn: zoomInBtn, zoomOut: zoomOutBtn, pct: zoomPctEl });
attachToolbar(pdfView, {
  prevPage: document.getElementById("ws-page-prev"),
  nextPage: document.getElementById("ws-page-next"),
  pageInput: document.getElementById("ws-page-input"),
  pageTotal: document.getElementById("ws-page-total"),
  fit: document.getElementById("ws-fit"),
  rotate: document.getElementById("ws-rotate"),
  print: document.getElementById("ws-print"),
});
attachFindBar(pdfView, {
  toggle: findToggle,
  bar: findBar,
  input: findInput,
  count: findCount,
  prev: findPrevBtn,
  next: findNextBtn,
  close: findCloseBtn,
});

// ─── resizable / collapsible panes ────────────────────────────────────────
// Drag the splitters to resize files/source (the PDF pane absorbs the rest);
// the top-bar chips + per-pane chevrons hide/show panes. Widths + hidden state
// persist to localStorage so the reader's layout sticks across visits.
(function panes() {
  const split = document.getElementById("ws-split");
  if (!split) return;
  const STORAGE = "agentdrive-latex-layout";
  const MIN = { files: 150, source: 240 };
  const MAX = { files: 480, source: 920 };
  const DEFAULT = { files: 236, source: 440 };

  let state;
  try {
    // only a real object is usable — a truthy JSON primitive would survive
    // `|| {}` and then throw on the first `state.hidden = …` (strict mode).
    const parsed = JSON.parse(localStorage.getItem(STORAGE));
    state = parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    state = {};
  }
  state.hidden = state.hidden || {};
  const save = () => {
    try {
      localStorage.setItem(STORAGE, JSON.stringify(state));
    } catch {
      /* private mode / quota — layout just won't persist */
    }
  };

  const feedEl = () => split.querySelector(".ws-feed");

  function apply() {
    if (state.files) split.style.setProperty("--w-files", `${state.files}px`);
    if (state.source) split.style.setProperty("--w-source", `${state.source}px`);
    const feed = feedEl();
    if (feed) {
      if (state.feed) {
        feed.style.setProperty("--h-feed", `${state.feed}px`);
        feed.classList.add("is-sized");
      } else {
        feed.classList.remove("is-sized");
      }
    }
    for (const pane of ["files", "source", "pdf"]) {
      const hidden = !!state.hidden[pane];
      split.classList.toggle(`${pane}-hidden`, hidden);
      const chip = document.querySelector(`.ws-panes [data-pane="${pane}"]`);
      if (chip) {
        chip.classList.toggle("is-on", !hidden);
        chip.setAttribute("aria-pressed", String(!hidden));
      }
    }
  }
  apply();

  function togglePane(pane) {
    const next = !state.hidden[pane];
    // never hide the last visible pane
    if (next && ["files", "source", "pdf"].filter((p) => !state.hidden[p]).length <= 1) return;
    state.hidden[pane] = next;
    apply();
    save();
  }
  for (const chip of document.querySelectorAll(".ws-panes [data-pane]")) {
    chip.addEventListener("click", () => togglePane(chip.dataset.pane));
  }
  for (const btn of document.querySelectorAll(".ws-collapse[data-collapse]")) {
    btn.addEventListener("click", () => togglePane(btn.dataset.collapse));
  }

  const paneEl = (which) =>
    which === "files" ? split.querySelector(".ws-tree") : split.querySelector(".ws-source");

  for (const handle of split.querySelectorAll(".ws-splitter")) {
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const which = handle.dataset.splitter;
      const startX = e.clientX;
      const startW = paneEl(which).getBoundingClientRect().width;
      handle.classList.add("is-dragging");
      handle.setPointerCapture(e.pointerId);
      const onMove = (ev) => {
        const w = Math.max(MIN[which], Math.min(MAX[which], startW + (ev.clientX - startX)));
        state[which] = Math.round(w);
        split.style.setProperty(`--w-${which}`, `${state[which]}px`);
      };
      const onUp = () => {
        handle.classList.remove("is-dragging");
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        save();
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    });
    // keyboard: arrows nudge the boundary (a11y for the separator role)
    handle.addEventListener("keydown", (e) => {
      const which = handle.dataset.splitter;
      const step = e.shiftKey ? 40 : 12;
      let delta = 0;
      if (e.key === "ArrowLeft") delta = -step;
      else if (e.key === "ArrowRight") delta = step;
      else return;
      e.preventDefault();
      const cur = state[which] || DEFAULT[which];
      state[which] = Math.max(MIN[which], Math.min(MAX[which], cur + delta));
      split.style.setProperty(`--w-${which}`, `${state[which]}px`);
      save();
    });
  }

  // horizontal splitter: resize the activity feed's height. It grows upward,
  // shrinking the file tree / outline above it; clamped so those keep room.
  const FEED_MIN = 80;
  const feedMaxH = () => {
    const tree = split.querySelector(".ws-tree");
    return tree ? Math.max(120, tree.getBoundingClientRect().height - 140) : 600;
  };
  function setFeedHeight(h) {
    if (!Number.isFinite(h)) return; // guard against a corrupt persisted height
    const feed = feedEl();
    if (!feed) return;
    state.feed = Math.round(Math.max(FEED_MIN, Math.min(feedMaxH(), h)));
    feed.style.setProperty("--h-feed", `${state.feed}px`);
    feed.classList.add("is-sized");
  }
  const feedHandle = split.querySelector(".ws-feed-resize");
  if (feedHandle) {
    feedHandle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const feed = feedEl();
      if (!feed) return;
      const startY = e.clientY;
      const startH = feed.getBoundingClientRect().height;
      feedHandle.classList.add("is-dragging");
      feedHandle.setPointerCapture(e.pointerId);
      const onMove = (ev) => setFeedHeight(startH + (startY - ev.clientY));
      const onUp = () => {
        feedHandle.classList.remove("is-dragging");
        feedHandle.removeEventListener("pointermove", onMove);
        feedHandle.removeEventListener("pointerup", onUp);
        save();
      };
      feedHandle.addEventListener("pointermove", onMove);
      feedHandle.addEventListener("pointerup", onUp);
    });
    feedHandle.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 40 : 12;
      let delta = 0;
      if (e.key === "ArrowUp") delta = step; // taller
      else if (e.key === "ArrowDown") delta = -step;
      else return;
      e.preventDefault();
      const feed = feedEl();
      setFeedHeight((Number(state.feed) || (feed ? feed.getBoundingClientRect().height : 0)) + delta);
      save();
    });
  }
})();

recompileBtn.addEventListener("click", async () => {
  if (recompileBusy) return;
  closeMenu();
  setRecompileDisabled(true); // optimistic; the poll confirms via status
  try {
    const body = new URLSearchParams();
    body.set("csrf", csrf);
    body.set("engine", selectedEngine);
    if (entrypoint) body.set("entrypoint", entrypoint);
    const r = await fetch(compileUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    if (r.ok) {
      poll(); // pick up queued immediately, don't wait for the next tick
    } else {
      const j = await r.json().catch(() => null);
      const msg = j && j.detail && j.detail.error ? j.detail.error.message : "Recompile failed";
      setStatus("err", msg);
      setRecompileDisabled(false);
    }
  } catch (_e) {
    setRecompileDisabled(false);
  }
});

// ─── poll ────────────────────────────────────────────────────────────────

async function poll() {
  if (stopped || document.hidden || polling) return; // one poll body at a time
  polling = true;
  try {
    await pollOnce();
  } finally {
    polling = false;
  }
}

async function pollOnce() {
  let d;
  try {
    const r = await fetch(pollUrl, { credentials: "same-origin" });
    if (r.status === 404) {
      stopped = true;
      setStatus("err", "Project gone");
      setEmpty();
      return;
    }
    if (!r.ok) throw new Error(`poll ${r.status}`);
    d = await r.json();
  } catch (_e) {
    return; // keep last good state on a transient blip
  }

  renderStrip(d);
  updateAgent(d);

  // file tree (+ re-upload flash) and feed
  files = d.files || [];
  entrypoint = d.entrypoint || "";
  entryEl.textContent = entrypoint || "—";
  if (!engineUserSet && d.engine_selected) {
    selectedEngine = d.engine_selected;
    markEngine();
  }
  // Signature-gate the tree + feed re-render: rebuilding the DOM every tick
  // re-fires the CSS entrance animations (the feed "flashing"). Only re-render
  // when the data — or, for the tree, the active selection — actually changes.
  const treeSig = JSON.stringify([
    selectedRel,
    files.map((f) => [f.rel, f.version, f.is_entrypoint]),
  ]);
  if (treeSig !== lastTreeSig) {
    lastTreeSig = treeSig;
    renderTree();
  }
  const feedSig = JSON.stringify(
    (d.recent_events || []).map((e) => [e.kind, e.actor_name, e.created_at, e.path, e.version, e.engine, e.pages]),
  );
  if (feedSig !== lastFeedSig) {
    lastFeedSig = feedSig;
    renderFeed(d.recent_events);
    if (feedCountEl) {
      const n = (d.recent_events || []).length;
      feedCountEl.textContent = n ? String(n) : "";
    }
  }

  // source pane: follow the entrypoint until the user picks a file; then keep
  // their choice but reload it if its version changed (re-upload). If a picked
  // file disappears (deleted/renamed), drop back to following the entrypoint
  // instead of stranding stale bytes on screen.
  const selected = files.find((f) => f.rel === selectedRel);
  if (userPickedFile && !selected) userPickedFile = false;
  if (!userPickedFile && files.length) {
    const target = files.find((f) => f.is_entrypoint) || files.find((f) => f.rel.endsWith(".tex")) || files[0];
    if (target && target.rel !== selectedRel) selectFile(target);
    else if (target && target.version !== selectedVersion) selectFile(target);
  } else if (selected && selected.version !== selectedVersion) {
    selectFile(selected);
  }

  // Advance the flash baseline AFTER the source-pane re-render above, so a
  // re-upload of the *currently-open* file still flashes its tree row (its
  // selectFile() re-render runs against the old versions first).
  prevVersions = new Map(files.map((f) => [f.rel, f.version]));

  // PDF swap — preserve scroll, animate new pages (renderPdf owns both)
  if (d.pdf && pdfKey(d.pdf) !== lastKey) {
    try {
      await renderPdf(d.pdf.raw_url);
      lastKey = pdfKey(d.pdf);
    } catch (_e) {
      /* keep the last good PDF on a render error */
    }
  } else if (!d.pdf && d.status === "none") {
    setEmpty();
  }
}

// Change-detection key — version is primary; fall back to art_id for legacy
// success rows that lack a version.
function pdfKey(pdf) {
  return pdf.version != null ? `v${pdf.version}` : `a${pdf.art_id}`;
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});

markEngine();
poll();
setInterval(poll, POLL_MS);
setInterval(updateRelativeTimes, 30000); // keep "Ns ago" live without re-rendering

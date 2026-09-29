// Draws ```mermaid fences. Shared by the public page (via diagram-visitor.js)
// and the private shell (imported directly), so the two surfaces cannot drift.
//
// The engine does not run in this document. It runs in `diagram-frame.html`,
// a same-origin frame with its own policy, and this module talks to it over
// postMessage: diagram source text goes in, an SVG string comes back, and the
// string is shown as an <img>. Never inline SVG, never the engine in the
// page. Four things follow, all of them the point:
//
//   * This page keeps `style-src 'self'` with nothing inline. Mermaid needs
//     inline styles to measure text; it gets them in ITS document, not ours.
//   * The engine — 3.5 MB of third-party code — never shares a realm with the
//     page, or with the private shell and the credential in its closure.
//   * The drawing can neither run script nor reach the document. An SVG
//     loaded as an image runs no script and loads no external resource,
//     whatever the engine or a hostile diagram put in it.
//   * Both surfaces already allow `data:` in img-src; the only policy change
//     is `frame-src 'self'`, and only our own scripts can create a frame.
//
// Failure is a code block. The source <pre> the renderer emitted IS the
// fallback: with script off, the engine unreachable, or a diagram mermaid
// cannot parse, the reader sees the diagram as its author wrote it, plus a
// one-line note for the last case so "not drawn" is distinguishable from
// "not a diagram".

const SOURCE = 'pre[data-diagram="mermaid"]';
const FRAME_URL = new URL("./diagram-frame.html", import.meta.url);
const RENDER_TIMEOUT_MS = 20_000;

let framePromise = null;
const pending = new Map();
let sequence = 0;

function engineFrame() {
  framePromise ??= new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.className = "diagram-scratch";
    frame.setAttribute("aria-hidden", "true");
    frame.tabIndex = -1;
    frame.title = "Diagram engine";
    const timer = setTimeout(() => {
      frame.remove();
      framePromise = null;
      reject(new Error("engine did not start"));
    }, RENDER_TIMEOUT_MS);
    const onReady = (event) => {
      if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
      if (event.data?.type !== "agentdrive.diagram.ready") return;
      window.removeEventListener("message", onReady);
      clearTimeout(timer);
      resolve(frame);
    };
    window.addEventListener("message", onReady);
    frame.src = FRAME_URL.href;
    document.body.append(frame);
  });
  return framePromise;
}

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin) return;
  const data = event.data;
  if (!data || data.type !== "agentdrive.diagram.rendered") return;
  const entry = pending.get(data.id);
  if (!entry || event.source !== entry.frame.contentWindow) return;
  pending.delete(data.id);
  clearTimeout(entry.timer);
  if (typeof data.svg === "string") {
    entry.resolve({
      svg: data.svg,
      width: Number.isFinite(data.width) ? data.width : null,
      height: Number.isFinite(data.height) ? data.height : null,
    });
  } else {
    entry.reject(new Error(String(data.error || "render failed")));
  }
});

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "default";
}

async function renderSvg(text) {
  const frame = await engineFrame();
  const id = `d${++sequence}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("render timed out"));
    }, RENDER_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer, frame });
    frame.contentWindow.postMessage(
      { type: "agentdrive.diagram.render", id, text, theme: currentTheme() },
      location.origin,
    );
  });
}

async function draw(source) {
  // The frame returns the SVG already sized from its viewBox, and this side
  // never parses it: a DOMParser document would inherit this page's CSP and
  // report mermaid's style attributes as violations. The string goes straight
  // into a data: URL, where the browser decodes it as an image and nothing
  // in it can run or style anything here.
  const { svg, width, height } = await renderSvg(source);
  if (!svg.trimStart().startsWith("<svg")) throw new Error("not an svg");
  return {
    src: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg),
    width,
    height,
  };
}

function firstLine(source) {
  const line = source.trim().split("\n")[0] || "diagram";
  return line.length > 80 ? line.slice(0, 77) + "…" : line;
}

function noteFailure(pre, text) {
  if (pre.previousElementSibling?.classList.contains("diagram-note")) return;
  pre.classList.add("diagram-failed");
  const note = document.createElement("p");
  note.className = "diagram-note";
  note.textContent = text;
  pre.before(note);
}

async function replace(pre) {
  const source = pre.textContent;
  let drawn;
  try {
    drawn = await draw(source);
  } catch {
    noteFailure(pre, "This diagram could not be drawn; its source is shown instead.");
    return;
  }
  const figure = document.createElement("figure");
  figure.className = "diagram-figure";
  figure.dataset.diagram = "mermaid";
  const img = document.createElement("img");
  img.className = "diagram";
  img.alt = "Diagram: " + firstLine(source);
  img.decoding = "async";
  if (drawn.width) img.width = drawn.width;
  if (drawn.height) img.height = drawn.height;
  img.src = drawn.src;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "Diagram source";
  details.append(summary, pre.cloneNode(true));
  figure.append(img, details);
  pre.replaceWith(figure);
}

export async function renderDiagrams(root) {
  // Only sources not yet drawn: a drawn figure keeps its source inside its
  // own <details>, and a second pass must not draw it twice.
  const sources = Array.from(root.querySelectorAll(SOURCE)).filter(
    (pre) => !pre.closest("figure.diagram-figure"),
  );
  if (sources.length === 0) return;
  try {
    await engineFrame();
  } catch {
    // Script on, engine gone: the code blocks stand, and nothing is said —
    // the page is not broken, it merely could not do better.
    return;
  }
  for (const pre of sources) {
    await replace(pre);
  }
}

// Redraw when the theme changes: the image was painted for one palette and
// cannot be restyled from outside. Reads the source back out of the figure's
// own <details>, so the redraw is the same path as the first draw.
export function followTheme(root) {
  if (typeof MutationObserver === "undefined") return;
  let last = currentTheme();
  const observer = new MutationObserver(async () => {
    const now = currentTheme();
    if (now === last) return;
    last = now;
    for (const figure of root.querySelectorAll("figure.diagram-figure")) {
      const pre = figure.querySelector(SOURCE);
      const img = figure.querySelector("img.diagram");
      if (!pre || !img) continue;
      try {
        img.src = (await draw(pre.textContent)).src;
      } catch {
        // Keep the previous palette's drawing rather than drop the diagram.
      }
    }
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}

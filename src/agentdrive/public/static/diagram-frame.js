// Runs INSIDE diagram-frame.html — the engine's realm, not the page's.
//
// One message in, one message out. The parent (diagrams.js, same origin)
// posts `{type: "agentdrive.diagram.render", id, text, theme}`; this replies
// `{type: "agentdrive.diagram.rendered", id, svg}` or `{..., id, error}`.
// Both `event.origin` and `event.source` are checked, so a message from any
// other document — another frame, a devtools paste, an extension — is
// ignored. The reply goes to `event.source` at `event.origin`, never `"*"`.
//
// The engine never sees anything but diagram source text, and this document
// never holds anything else: no credential, no artifact markup, no styles of
// ours. Its CSP (set by the asset route) allows exactly what mermaid needs
// for getBBox measurement — inline styles — and nothing more.
(() => {
  "use strict";
  const engine = globalThis.mermaid;
  let counter = 0;
  let configuredTheme = null;

  function configure(theme) {
    if (configuredTheme === theme) return;
    configuredTheme = theme;
    engine.initialize({
      startOnLoad: false,
      // mermaid's own label sanitiser. Belt to the parent's <img> braces.
      securityLevel: "strict",
      // HTML labels render through <foreignObject>, which an SVG-as-image
      // draws poorly or not at all. Plain SVG text everywhere.
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      theme: theme === "dark" ? "dark" : "default",
      // The final image cannot load a web font, so measure with the generic
      // family it will fall back to.
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
    });
  }

  // Mermaid sizes its root to 100% width with a max-width in a style
  // attribute — right for inline SVG, wrong for an image, which needs an
  // intrinsic size. The viewBox already holds it. Done HERE rather than in
  // the parent because a DOMParser document inherits its creator's CSP, and
  // parsing mermaid's style attributes under `style-src 'self'` is reported
  // as a violation even though nothing is ever applied.
  function intrinsicallySized(svg) {
    const parsed = new DOMParser().parseFromString(svg, "image/svg+xml");
    const root = parsed.documentElement;
    if (root.nodeName !== "svg") throw new Error("not an svg");
    const viewBox = (root.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    if (
      viewBox.length === 4 && viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0
    ) {
      root.setAttribute("width", String(Math.ceil(viewBox[2])));
      root.setAttribute("height", String(Math.ceil(viewBox[3])));
      root.removeAttribute("style");
    }
    return {
      svg: new XMLSerializer().serializeToString(root),
      width: Number(root.getAttribute("width")) || null,
      height: Number(root.getAttribute("height")) || null,
    };
  }

  window.addEventListener("message", async (event) => {
    if (event.origin !== location.origin || event.source !== window.parent) return;
    const data = event.data;
    if (!data || data.type !== "agentdrive.diagram.render" || typeof data.id !== "string") return;
    const reply = (payload) => event.source.postMessage(
      { type: "agentdrive.diagram.rendered", id: data.id, ...payload },
      event.origin,
    );
    if (!engine) return reply({ error: "no engine" });
    try {
      configure(data.theme);
      const { svg } = await engine.render(
        `agentdrive-diagram-${++counter}`,
        String(data.text ?? ""),
      );
      reply(intrinsicallySized(svg));
    } catch (error) {
      reply({ error: String(error && error.message ? error.message : error).slice(0, 200) });
    } finally {
      // The engine's scratch element is removed by mermaid on success; on a
      // parse failure it can leave one behind. Nothing here is worth keeping.
      document.body.replaceChildren();
    }
  });

  // Tell the parent this document is listening. `parent` is the only
  // legitimate recipient, and it is same-origin by construction.
  if (window.parent !== window) {
    window.parent.postMessage({ type: "agentdrive.diagram.ready" }, location.origin);
  }
})();

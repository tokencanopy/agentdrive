// Region-select overlay — injected into the active tab on demand.
//
// Renders a fixed-position semi-transparent overlay covering the
// viewport with a crosshair cursor. The user click-drags a rect;
// on mouseup the overlay posts the rect (in CSS pixels) + DPR back
// to the SW. Esc cancels.

(function () {
  // Idempotent install: if a previous overlay is still alive, remove it.
  const PREV_ID = "__snipit_overlay__";
  const existing = document.getElementById(PREV_ID);
  if (existing) existing.remove();

  // Self-destruct if the extension context is no longer valid. This can
  // happen when the user reloads the extension while old content scripts
  // are still injected in tabs. Any chrome.runtime.* call would throw
  // "Extension context invalidated"; bail quietly instead.
  function isOrphaned() {
    try {
      return !chrome.runtime?.id;
    } catch (_e) {
      return true;
    }
  }
  function safeSend(msg) {
    if (isOrphaned()) {
      cleanup();
      return;
    }
    try {
      const p = chrome.runtime.sendMessage(msg);
      // sendMessage in MV3 returns a promise; absorb any
      // "message channel closed" rejections so they don't bubble.
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch (_e) {
      cleanup();
    }
  }
  if (isOrphaned()) {
    // Extension was reloaded between injection and execution — don't
    // attach handlers, don't wire the overlay, don't make a mess.
    return;
  }

  const overlay = document.createElement("div");
  overlay.id = PREV_ID;
  Object.assign(overlay.style, {
    position: "fixed",
    inset: "0",
    zIndex: "2147483647",
    cursor: "crosshair",
    background: "rgba(26, 23, 20, 0.18)",
    userSelect: "none",
  });

  const rectEl = document.createElement("div");
  Object.assign(rectEl.style, {
    position: "absolute",
    border: "2px solid #E26534",
    background: "rgba(226, 101, 52, 0.08)",
    display: "none",
    pointerEvents: "none",
  });
  overlay.appendChild(rectEl);

  document.documentElement.appendChild(overlay);

  let start = null;

  function setRectFromPoints(x1, y1, x2, y2) {
    const x = Math.min(x1, x2);
    const y = Math.min(y1, y2);
    const w = Math.abs(x2 - x1);
    const h = Math.abs(y2 - y1);
    rectEl.style.left = `${x}px`;
    rectEl.style.top = `${y}px`;
    rectEl.style.width = `${w}px`;
    rectEl.style.height = `${h}px`;
    rectEl.style.display = "block";
    return { x, y, w, h };
  }

  function cleanup() {
    overlay.removeEventListener("mousedown", onDown);
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("pagehide", onPageHide);
    overlay.remove();
  }

  function onPageHide() {
    // The page navigated away (link click, history.back, JS nav).
    // The overlay element is gone with the DOM but the SW is still
    // blocked waiting for `region-selected` or `region-cancelled`.
    // Fire cancel so the SW unblocks fast instead of timing out.
    cleanup();
    try {
      safeSend({ type: "region-cancelled" });
    } catch (_e) { /* extension may be reloading; ignore */ }
  }

  function onDown(e) {
    if (e.button !== 0) return;
    start = { x: e.clientX, y: e.clientY };
    setRectFromPoints(start.x, start.y, start.x, start.y);
    e.preventDefault();
  }

  function onMove(e) {
    if (!start) return;
    setRectFromPoints(start.x, start.y, e.clientX, e.clientY);
    e.preventDefault();
  }

  function onUp(e) {
    if (!start) return;
    const rect = setRectFromPoints(start.x, start.y, e.clientX, e.clientY);
    const dpr = window.devicePixelRatio || 1;
    const title = document.title;
    const pageUrl = location.href;
    if (rect.w < 8 || rect.h < 8) {
      // Too-small drag — silently reset selection and let the user
      // retry. The crosshair cursor stays, so the affordance is intact.
      // No on-page text because that would get captured (the bug
      // this design eliminates).
      rectEl.style.display = "none";
      start = null;
      // Brief background flash on the dimmer to acknowledge the
      // dropped selection — fades within ~150ms, no chance of being
      // captured in the next attempt's screenshot.
      overlay.style.background = "rgba(226, 101, 52, 0.32)";  // accent tint
      setTimeout(() => {
        overlay.style.background = "rgba(26, 23, 20, 0.18)";   // back to ink dim
      }, 150);
      return;
    }
    cleanup();
    // Wait two animation frames so the browser actually paints the
    // overlay-removed state before the SW calls captureVisibleTab.
    // One rAF only schedules the next paint; the second fires AFTER
    // it's flushed. Without this, Chrome captures the frame that
    // still has the "Drag to select" hint banner in it.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        safeSend({
          type: "region-selected",
          rect, dpr, title, pageUrl,
        });
      });
    });
  }

  function onKey(e) {
    if (e.key === "Escape") {
      cleanup();
      safeSend({ type: "region-cancelled" });
    }
  }

  overlay.addEventListener("mousedown", onDown);
  window.addEventListener("mousemove", onMove, { passive: false });
  window.addEventListener("mouseup", onUp, { passive: false });
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("pagehide", onPageHide);
})();

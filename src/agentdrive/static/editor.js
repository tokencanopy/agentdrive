// AgentDrive web editor — annotation surface for an image artifact.
// Cookie-authed; reads pixels from `data-image-src` (same-origin) and
// auto-saves to `PUT /web/artifacts/{path}` with the session cookie.
//
// Ported from extensions/snipit/src/editor/editor.js. Differences:
//   * No chrome.runtime / chrome.storage — uses fetch + same-origin.
//   * No tempId / SW lifecycle.
//   * Discard hits DELETE /web/artifacts/{path} then redirects to /dashboard.
//   * Source PNG comes from `image_src` (the viewer URL with `?raw=1`).

const root        = document.querySelector(".m-editor");
const artId       = root.dataset.artId;
const artPath     = root.dataset.path;
// The artifact's OWN drive (may differ from the session's active workspace).
// Sent as X-Drive-Id so autosave/discard targets the resource's drive
// (cross-workspace; design §4.3), not whatever workspace is active.
const driveId     = root.dataset.driveId || "";
const imageSrc    = root.dataset.imageSrc;
const shareUrl    = root.dataset.shareUrl;
const canvas      = document.getElementById("canvas");
const ctx         = canvas.getContext("2d");
const hint        = document.getElementById("hint");
const csrfToken   = document.querySelector('meta[name="csrf-token"]')?.content || "";

const COLORS = { ink: "#1A1714", accent: "#C17D2B" };
const ACCENT = COLORS.accent;
const ALL_TOOLS = ["select", "arrow", "rect", "redact", "text", "crop"];

let currentTool = "select";
let currentColor = "ink";
let baseImage = null;
let baseW = 0, baseH = 0;
let viewFitScale = 1;
let marks = [];
let drawing = null;
let drawingStart = null;
let selectedIndex = -1;
let dragState = null;
let activeTextEditor = null;

// Crop tool state. `cropRect` is the in-flight crop selection (canvas
// coords); user can resize or just press Enter to commit. Esc cancels.
// Commit replaces baseImage with the cropped portion and translates
// any marks within the crop bounds.
let cropRect = null;
let cropDragMode = null;     // "draw" | "move" | "resize-<handle>" | null
let cropDragOrigin = null;
let cropSnapshot = null;

const HANDLE_PX = 9;
const HANDLE_HIT_PX = 12;

// -- bootstrap --------------------------------------------------------------

async function init() {
  attachUrlBarHandlers();
  attachDiscardHandler();
  // Fetch the artifact bytes via the same-origin viewer URL. `credentials:
  // 'include'` carries the session cookie so private artifacts work too.
  let blob;
  try {
    const r = await fetch(imageSrc, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    blob = await r.blob();
  } catch (e) {
    hint.textContent = `Couldn't load capture: ${e.message || e}`;
    return;
  }
  const img = new Image();
  img.onload = () => buildCanvas(img);
  img.onerror = () => { hint.textContent = "Capture image failed to load."; };
  img.src = URL.createObjectURL(blob);
}

function buildCanvas(img) {
  baseImage = img;
  baseW = img.naturalWidth;
  baseH = img.naturalHeight;
  canvas.width = baseW;
  canvas.height = baseH;
  // Let CSS handle fit-to-viewport (the canvas has `max-width: 100%;
  // height: auto`). We re-derive viewFitScale on demand from the
  // current rendered rect so event coordinates always map correctly,
  // even if the user resizes the window mid-edit.
  recomputeViewFitScale();
  window.addEventListener("resize", recomputeViewFitScale);
  attachHandlers();
  setTool("arrow");
  setColor("ink");
  hint.textContent = `${baseW}×${baseH} · 1/2/3/4 tools · 5 select · c color · Del removes · Esc cancels`;
  redraw();
}

// -- rendering --------------------------------------------------------------

function redraw() {
  if (!baseImage) return;
  ctx.clearRect(0, 0, baseW, baseH);
  ctx.drawImage(baseImage, 0, 0);
  for (const m of marks) {
    if (m.type === "redact") {
      ctx.fillStyle = COLORS.ink;
      ctx.fillRect(m.x, m.y, m.w, m.h);
    }
  }
  for (const m of marks) {
    if (m.type === "arrow") drawArrow(ctx, m);
    else if (m.type === "rect") drawRect(ctx, m);
    else if (m.type === "text") drawText(ctx, m);
  }
  if (drawing) {
    if (drawing.type === "arrow") drawArrow(ctx, drawing);
    else if (drawing.type === "rect") drawRect(ctx, drawing);
    else if (drawing.type === "redact") {
      ctx.fillStyle = COLORS.ink;
      ctx.fillRect(drawing.x, drawing.y, drawing.w, drawing.h);
    }
  }
  if (selectedIndex >= 0 && marks[selectedIndex]) {
    drawSelectionOutline(ctx, marks[selectedIndex]);
  }
  if (cropRect) drawCropOverlay(ctx);
}

// Dims everything outside `cropRect`, then draws an accent border +
// resize handles on the rect. The dim layer is drawn LAST so it's
// what the user sees over any prior marks too.
function drawCropOverlay(ctx) {
  const { x, y, w, h } = cropRect;
  ctx.save();
  ctx.fillStyle = "rgba(20, 16, 12, 0.55)";
  // Four bands around the crop rect — keeps the inside fully visible.
  ctx.fillRect(0, 0, baseW, y);
  ctx.fillRect(0, y + h, baseW, baseH - y - h);
  ctx.fillRect(0, y, x, h);
  ctx.fillRect(x + w, y, baseW - x - w, h);
  // Rule-of-thirds grid — composition cue inside the crop rect.
  ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  for (let i = 1; i < 3; i++) {
    const gx = x + (w * i / 3);
    const gy = y + (h * i / 3);
    ctx.beginPath();
    ctx.moveTo(gx, y);     ctx.lineTo(gx, y + h); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(x, gy);     ctx.lineTo(x + w, gy); ctx.stroke();
  }
  // Crop frame border (drawn over the grid so it pops).
  ctx.strokeStyle = ACCENT;
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);
  // 8 handles for resize.
  for (const c of cropHandles()) drawHandle(ctx, c.x, c.y);
  ctx.restore();
}

function cropHandles() {
  if (!cropRect) return [];
  const { x, y, w, h } = cropRect;
  const cx = x + w / 2, cy = y + h / 2;
  return [
    // 4 corners
    { x: x,     y: y,     handle: "nw", cursor: "nwse-resize" },
    { x: x + w, y: y,     handle: "ne", cursor: "nesw-resize" },
    { x: x,     y: y + h, handle: "sw", cursor: "nesw-resize" },
    { x: x + w, y: y + h, handle: "se", cursor: "nwse-resize" },
    // 4 edge midpoints
    { x: cx,    y: y,     handle: "n",  cursor: "ns-resize" },
    { x: cx,    y: y + h, handle: "s",  cursor: "ns-resize" },
    { x: x,     y: cy,    handle: "w",  cursor: "ew-resize" },
    { x: x + w, y: cy,    handle: "e",  cursor: "ew-resize" },
  ];
}

function hitCropHandle(p) {
  for (const c of cropHandles()) {
    if (Math.abs(p.x - c.x) <= HANDLE_HIT_PX && Math.abs(p.y - c.y) <= HANDLE_HIT_PX) return c;
  }
  return null;
}

function pointInCrop(p) {
  if (!cropRect) return false;
  return pointInBox(p, cropRect);
}

function drawArrow(ctx, m) {
  const { x1, y1, x2, y2, color } = m;
  ctx.strokeStyle = COLORS[color] || color;
  ctx.fillStyle = COLORS[color] || color;
  ctx.lineWidth = 4;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  const head = 16;
  const angle = Math.atan2(y2 - y1, x2 - x1);
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - head * Math.cos(angle - Math.PI / 6),
             y2 - head * Math.sin(angle - Math.PI / 6));
  ctx.lineTo(x2 - head * Math.cos(angle + Math.PI / 6),
             y2 - head * Math.sin(angle + Math.PI / 6));
  ctx.closePath();
  ctx.fill();
}

function drawRect(ctx, m) {
  ctx.strokeStyle = COLORS[m.color] || m.color;
  ctx.lineWidth = 3;
  ctx.strokeRect(m.x, m.y, m.w, m.h);
}

function drawText(ctx, m) {
  ctx.fillStyle = COLORS[m.color] || m.color;
  const fs = m.fontSize || 18;
  ctx.font = `${fs}px -apple-system, BlinkMacSystemFont, sans-serif`;
  ctx.textBaseline = "top";
  const lines = String(m.text).split("\n");
  const lh = Math.round(fs * 1.22);
  let y = m.y;
  for (const line of lines) { ctx.fillText(line, m.x, y); y += lh; }
}

function drawSelectionOutline(ctx, m) {
  const b = boundingBox(m);
  if (!b) return;
  ctx.save();
  ctx.strokeStyle = ACCENT;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 4]);
  ctx.strokeRect(b.x - 4, b.y - 4, b.w + 8, b.h + 8);
  ctx.restore();
  for (const h of getHandles(m)) drawHandle(ctx, h.x, h.y);
}

function drawHandle(ctx, x, y) {
  const r = HANDLE_PX / 2;
  ctx.save();
  ctx.fillStyle = "#FAF8F2"; ctx.strokeStyle = ACCENT;
  ctx.lineWidth = 1.5; ctx.setLineDash([]);
  ctx.beginPath(); ctx.rect(x - r, y - r, HANDLE_PX, HANDLE_PX);
  ctx.fill(); ctx.stroke();
  ctx.restore();
}

function getHandles(m) {
  if (m.type === "arrow") return [
    { x: m.x1, y: m.y1, handle: "start", cursor: "move" },
    { x: m.x2, y: m.y2, handle: "end",   cursor: "move" },
  ];
  if (m.type === "rect" || m.type === "redact") return [
    { x: m.x,       y: m.y,       handle: "nw", cursor: "nwse-resize" },
    { x: m.x + m.w, y: m.y,       handle: "ne", cursor: "nesw-resize" },
    { x: m.x,       y: m.y + m.h, handle: "sw", cursor: "nesw-resize" },
    { x: m.x + m.w, y: m.y + m.h, handle: "se", cursor: "nwse-resize" },
  ];
  if (m.type === "text") {
    const b = boundingBox(m);
    if (!b) return [];
    return [{ x: b.x + b.w, y: b.y + b.h, handle: "fontsize", cursor: "nwse-resize" }];
  }
  return [];
}

function hitHandle(p, m) {
  for (const h of getHandles(m)) {
    if (Math.abs(p.x - h.x) <= HANDLE_HIT_PX && Math.abs(p.y - h.y) <= HANDLE_HIT_PX) return h;
  }
  return null;
}

// -- bounding boxes + hit testing ------------------------------------------

function boundingBox(m) {
  if (m.type === "arrow") {
    const x = Math.min(m.x1, m.x2), y = Math.min(m.y1, m.y2);
    return { x: x - 8, y: y - 8, w: Math.abs(m.x2 - m.x1) + 16, h: Math.abs(m.y2 - m.y1) + 16 };
  }
  if (m.type === "rect" || m.type === "redact") {
    return { x: m.x - 2, y: m.y - 2, w: m.w + 4, h: m.h + 4 };
  }
  if (m.type === "text") {
    const fs = m.fontSize || 18;
    ctx.font = `${fs}px -apple-system, BlinkMacSystemFont, sans-serif`;
    const lines = String(m.text).split("\n");
    let maxW = 0;
    for (const line of lines) {
      const w = ctx.measureText(line).width;
      if (w > maxW) maxW = w;
    }
    return { x: m.x - 2, y: m.y - 2, w: maxW + 4, h: lines.length * Math.round(fs * 1.22) + 4 };
  }
  return null;
}

function pointInBox(p, b) { return p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h; }

function pointNearSegment(p, x1, y1, x2, y2, tol) {
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(p.x - x1, p.y - y1) <= tol;
  let t = ((p.x - x1) * dx + (p.y - y1) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const cx = x1 + t * dx, cy = y1 + t * dy;
  return Math.hypot(p.x - cx, p.y - cy) <= tol;
}

function hitTest(p) {
  for (let i = marks.length - 1; i >= 0; i--) {
    const m = marks[i];
    if (m.type === "arrow") {
      if (pointNearSegment(p, m.x1, m.y1, m.x2, m.y2, 10)) return i;
    } else {
      const b = boundingBox(m);
      if (b && pointInBox(p, b)) return i;
    }
  }
  return -1;
}

// -- coordinate transform --------------------------------------------------

function recomputeViewFitScale() {
  const rect = canvas.getBoundingClientRect();
  viewFitScale = rect.width > 0 ? rect.width / baseW : 1;
}

function eventToCanvas(evt) {
  // Re-derive on each event so we're immune to layout shifts
  // (sidebar opens, devtools dock, etc.) since the last buildCanvas.
  const rect = canvas.getBoundingClientRect();
  const scale = rect.width > 0 ? rect.width / baseW : viewFitScale;
  return {
    x: (evt.clientX - rect.left) / scale,
    y: (evt.clientY - rect.top) / scale,
  };
}

// -- draw modes ------------------------------------------------------------

function startDraw(p) {
  drawingStart = p;
  if (currentTool === "arrow") drawing = { type: "arrow", x1: p.x, y1: p.y, x2: p.x, y2: p.y, color: currentColor };
  else if (currentTool === "rect") drawing = { type: "rect", x: p.x, y: p.y, w: 0, h: 0, color: currentColor };
  else if (currentTool === "redact") drawing = { type: "redact", x: p.x, y: p.y, w: 0, h: 0 };
  else if (currentTool === "text") { spawnTextEditor(p); drawing = null; drawingStart = null; }
  else if (currentTool === "crop") {
    // Hit priority: handle (resize) > inside-frame (move) > outside (no-op).
    // The frame is always present in Crop mode (seeded by setTool), so we
    // never need to "draw a new rect" — adjust the existing one.
    const h = hitCropHandle(p);
    if (h) {
      cropDragMode = `resize-${h.handle}`;
      cropSnapshot = { ...cropRect };
      cropDragOrigin = p;
      canvas.style.cursor = h.cursor;
    } else if (pointInCrop(p)) {
      cropDragMode = "move";
      cropDragOrigin = { dx: p.x - cropRect.x, dy: p.y - cropRect.y };
      canvas.style.cursor = "grabbing";
    }
    drawing = null; drawingStart = null;
  }
}

function moveDraw(p) {
  if (currentTool === "crop") {
    if (cropDragMode) {
      moveCropDrag(p);
      updateCropHint();
      redraw();
    } else {
      // Hover cursor: handle > inside frame > arrow.
      const h = hitCropHandle(p);
      if (h) canvas.style.cursor = h.cursor;
      else if (pointInCrop(p)) canvas.style.cursor = "grab";
      else canvas.style.cursor = "default";
    }
    return;
  }
  if (!drawing || !drawingStart) return;
  if (drawing.type === "arrow") { drawing.x2 = p.x; drawing.y2 = p.y; }
  else {
    drawing.x = Math.min(drawingStart.x, p.x);
    drawing.y = Math.min(drawingStart.y, p.y);
    drawing.w = Math.abs(p.x - drawingStart.x);
    drawing.h = Math.abs(p.y - drawingStart.y);
  }
  redraw();
}

function moveCropDrag(p) {
  if (!cropDragMode || !cropRect) return;
  if (cropDragMode === "move") {
    const o = cropDragOrigin;
    cropRect = {
      x: Math.max(0, Math.min(baseW - cropRect.w, p.x - o.dx)),
      y: Math.max(0, Math.min(baseH - cropRect.h, p.y - o.dy)),
      w: cropRect.w, h: cropRect.h,
    };
  } else if (cropDragMode.startsWith("resize-")) {
    const handle = cropDragMode.slice("resize-".length);
    const snap = cropSnapshot;
    let x1 = snap.x, y1 = snap.y, x2 = snap.x + snap.w, y2 = snap.y + snap.h;
    // Corners move two edges; edge handles move one. Map each handle
    // to which of {x1,y1,x2,y2} follow the cursor.
    if (handle === "nw") { x1 = p.x; y1 = p.y; }
    else if (handle === "ne") { x2 = p.x; y1 = p.y; }
    else if (handle === "sw") { x1 = p.x; y2 = p.y; }
    else if (handle === "se") { x2 = p.x; y2 = p.y; }
    else if (handle === "n")  { y1 = p.y; }
    else if (handle === "s")  { y2 = p.y; }
    else if (handle === "w")  { x1 = p.x; }
    else if (handle === "e")  { x2 = p.x; }
    cropRect = {
      x: Math.max(0, Math.min(x1, x2)),
      y: Math.max(0, Math.min(y1, y2)),
      w: Math.max(10, Math.abs(x2 - x1)),
      h: Math.max(10, Math.abs(y2 - y1)),
    };
    cropRect.w = Math.min(cropRect.w, baseW - cropRect.x);
    cropRect.h = Math.min(cropRect.h, baseH - cropRect.y);
  }
}

function endDraw() {
  if (currentTool === "crop") {
    cropDragMode = null;
    cropDragOrigin = null;
    cropSnapshot = null;
    if (cropRect && (cropRect.w < 10 || cropRect.h < 10)) {
      // Drag was a click or too-small — clear the selection.
      cropRect = null;
    }
    updateCropHint();
    redraw();
    return;
  }
  if (!drawing) return;
  let kept = false;
  if (drawing.type === "arrow") {
    const d = Math.hypot(drawing.x2 - drawing.x1, drawing.y2 - drawing.y1);
    if (d >= 8) { marks.push(drawing); kept = true; }
  } else if (drawing.w >= 4 && drawing.h >= 4) {
    marks.push(drawing); kept = true;
  }
  drawing = null; drawingStart = null;
  if (kept) {
    selectedIndex = marks.length - 1;
    setTool("select");
    scheduleAutoSave();
  }
  redraw();
}

// Apply / cancel the in-flight crop. `applyCrop` is destructive: it
// rebuilds baseImage at the new dimensions, translates marks, and
// drops marks that fall fully outside the new bounds.
async function applyCrop() {
  if (!cropRect || cropRect.w < 10 || cropRect.h < 10) {
    cancelCrop();
    return;
  }
  const { x: cx, y: cy, w: cw, h: ch } = cropRect;
  // Render just the cropped portion of the original baseImage onto a
  // temporary canvas (no marks, no overlay — those re-render fresh).
  const tmp = document.createElement("canvas");
  tmp.width = cw;
  tmp.height = ch;
  const tctx = tmp.getContext("2d");
  tctx.drawImage(baseImage, cx, cy, cw, ch, 0, 0, cw, ch);
  const blob = await new Promise((r) => tmp.toBlob(r, "image/png"));
  const newImg = await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = URL.createObjectURL(blob);
  });
  // Translate marks; drop marks that don't intersect the new bounds.
  marks = marks
    .map((m) => translateMark(m, -cx, -cy))
    .filter((m) => markIntersectsCanvas(m, cw, ch));
  baseImage = newImg;
  baseW = cw;
  baseH = ch;
  canvas.width = baseW;
  canvas.height = baseH;
  selectedIndex = -1;
  cropRect = null;
  cropDragMode = null;
  updateCropHint();
  recomputeViewFitScale();
  redraw();
  // Crops are destructive; trigger the autosave immediately rather
  // than waiting for the debounce so the artifact's stored dimensions
  // catch up fast.
  scheduleAutoSave();
}

function cancelCrop() {
  cropRect = null;
  cropDragMode = null;
  cropDragOrigin = null;
  cropSnapshot = null;
  updateCropHint();
  redraw();
}

function translateMark(m, dx, dy) {
  if (m.type === "arrow") {
    return { ...m, x1: m.x1 + dx, y1: m.y1 + dy, x2: m.x2 + dx, y2: m.y2 + dy };
  }
  return { ...m, x: m.x + dx, y: m.y + dy };
}

function markIntersectsCanvas(m, w, h) {
  const b = boundingBox(m);
  if (!b) return false;
  return b.x < w && b.x + b.w > 0 && b.y < h && b.y + b.h > 0;
}

function updateCropHint() {
  if (currentTool !== "crop") return;
  if (cropRect) {
    hint.textContent =
      `Crop ${Math.round(cropRect.w)} × ${Math.round(cropRect.h)} — ` +
      `drag handles to resize, inside to move · Enter applies · Esc cancels.`;
  }
}

// -- select mode -----------------------------------------------------------

function startSelect(p) {
  if (selectedIndex >= 0 && marks[selectedIndex]) {
    const h = hitHandle(p, marks[selectedIndex]);
    if (h) {
      dragState = { mode: "resize", handle: h.handle, snapshot: JSON.parse(JSON.stringify(marks[selectedIndex])), origin: p };
      canvas.style.cursor = h.cursor;
      return;
    }
  }
  const i = hitTest(p);
  selectedIndex = i;
  if (i >= 0) {
    dragState = { mode: "move", anchor: anchorOffsetFor(marks[i], p) };
    canvas.style.cursor = "grabbing";
  } else dragState = null;
  redraw();
}

function moveSelect(p) {
  if (!dragState) {
    if (selectedIndex >= 0) {
      const h = hitHandle(p, marks[selectedIndex]);
      if (h) { canvas.style.cursor = h.cursor; return; }
    }
    const i = hitTest(p);
    canvas.style.cursor = i >= 0 ? "grab" : "default";
    return;
  }
  if (dragState.mode === "move") applyDrag(marks[selectedIndex], p, dragState.anchor);
  else if (dragState.mode === "resize") applyResize(marks[selectedIndex], p, dragState);
  redraw();
}

function endSelect() {
  if (dragState) {
    canvas.style.cursor = "grab";
    scheduleAutoSave();
  }
  dragState = null;
}

function anchorOffsetFor(m, p) {
  if (m.type === "arrow") {
    const mx = (m.x1 + m.x2) / 2, my = (m.y1 + m.y2) / 2;
    return { dx: p.x - mx, dy: p.y - my };
  }
  return { dx: p.x - m.x, dy: p.y - m.y };
}

function applyDrag(m, p, offset) {
  if (m.type === "arrow") {
    const halfW = (m.x2 - m.x1) / 2, halfH = (m.y2 - m.y1) / 2;
    const mx = p.x - offset.dx, my = p.y - offset.dy;
    m.x1 = mx - halfW; m.y1 = my - halfH;
    m.x2 = mx + halfW; m.y2 = my + halfH;
  } else {
    m.x = p.x - offset.dx; m.y = p.y - offset.dy;
  }
}

function applyResize(m, p, state) {
  const snap = state.snapshot;
  if (m.type === "arrow") {
    if (state.handle === "start") { m.x1 = p.x; m.y1 = p.y; }
    else if (state.handle === "end") { m.x2 = p.x; m.y2 = p.y; }
    return;
  }
  if (m.type === "rect" || m.type === "redact") {
    let x1 = snap.x, y1 = snap.y, x2 = snap.x + snap.w, y2 = snap.y + snap.h;
    if (state.handle === "nw") { x1 = p.x; y1 = p.y; }
    else if (state.handle === "ne") { x2 = p.x; y1 = p.y; }
    else if (state.handle === "sw") { x1 = p.x; y2 = p.y; }
    else if (state.handle === "se") { x2 = p.x; y2 = p.y; }
    m.x = Math.min(x1, x2); m.y = Math.min(y1, y2);
    m.w = Math.max(4, Math.abs(x2 - x1));
    m.h = Math.max(4, Math.abs(y2 - y1));
    return;
  }
  if (m.type === "text") {
    const origDiag = Math.hypot(p.x - snap.x, p.y - snap.y);
    const fs = snap.fontSize || 18;
    ctx.font = `${fs}px -apple-system, BlinkMacSystemFont, sans-serif`;
    const lines = String(snap.text).split("\n");
    let maxW = 0;
    for (const line of lines) { const w = ctx.measureText(line).width; if (w > maxW) maxW = w; }
    const origDiagRef = Math.hypot(maxW, lines.length * Math.round(fs * 1.22));
    if (origDiagRef <= 0) return;
    const ratio = origDiag / origDiagRef;
    m.fontSize = Math.max(10, Math.min(120, Math.round(fs * ratio)));
  }
}

function deleteSelected() {
  if (selectedIndex < 0) return;
  marks.splice(selectedIndex, 1);
  selectedIndex = -1; dragState = null;
  redraw(); scheduleAutoSave();
}

// -- inline text editor ----------------------------------------------------

function spawnTextEditor(p, opts = {}) {
  closeTextEditor(false);
  const { editingIndex = -1, initialText = "", fontSize = 18, color = currentColor } = opts;
  const editor = document.createElement("div");
  editor.contentEditable = "true";
  editor.spellcheck = false;
  editor.className = "text-edit";
  editor.textContent = initialText;
  editor.dataset.x = String(p.x); editor.dataset.y = String(p.y);
  editor.dataset.color = color; editor.dataset.fontSize = String(fontSize);
  editor.dataset.editingIndex = String(editingIndex);
  const rect = canvas.getBoundingClientRect();
  const screenX = rect.left + p.x * viewFitScale;
  const screenY = rect.top + p.y * viewFitScale;
  Object.assign(editor.style, {
    position: "fixed", left: `${screenX}px`, top: `${screenY}px`,
    minWidth: "60px", maxWidth: `${rect.right - screenX}px`,
    color: COLORS[color],
    fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
    fontSize: `${fontSize * viewFitScale}px`,
    lineHeight: "1.22", padding: "2px 4px",
    border: `1px dashed ${ACCENT}`,
    background: "rgba(250, 248, 242, 0.92)",
    outline: "none", whiteSpace: "pre-wrap",
    zIndex: "100", pointerEvents: "auto",
    minHeight: `${fontSize * viewFitScale * 1.22}px`,
    transformOrigin: "top left",
  });
  document.body.appendChild(editor);
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeTextEditor(false); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); closeTextEditor(true); }
  });
  editor.addEventListener("blur", () => closeTextEditor(true));
  setTimeout(() => {
    editor.focus();
    const range = document.createRange();
    range.selectNodeContents(editor); range.collapse(false);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  }, 0);
  activeTextEditor = editor;
}

function closeTextEditor(commit) {
  const editor = activeTextEditor;
  if (!editor) return;
  activeTextEditor = null;
  const text = editor.innerText.trim();
  const x = parseFloat(editor.dataset.x), y = parseFloat(editor.dataset.y);
  const color = editor.dataset.color;
  const fontSize = parseFloat(editor.dataset.fontSize);
  const editingIndex = parseInt(editor.dataset.editingIndex, 10);
  editor.remove();
  if (commit && text.length > 0) {
    if (editingIndex >= 0 && marks[editingIndex] && marks[editingIndex].type === "text") {
      marks[editingIndex] = { type: "text", x, y, text, color, fontSize };
      selectedIndex = editingIndex;
    } else {
      marks.push({ type: "text", x, y, text, color, fontSize });
      selectedIndex = marks.length - 1;
    }
    setTool("select");
    scheduleAutoSave();
  } else if (editingIndex >= 0 && commit === false) {
    selectedIndex = editingIndex;
  }
  redraw();
}

// -- toolbar wiring --------------------------------------------------------

function setTool(t) {
  if (!ALL_TOOLS.includes(t)) return;
  if (activeTextEditor && t !== "text") closeTextEditor(true);
  currentTool = t;
  document.querySelectorAll("#tools button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tool === t);
  });
  if (t === "select") canvas.style.cursor = "default";
  else if (t === "text") canvas.style.cursor = "text";
  else if (t === "crop") {
    canvas.style.cursor = "default";
    selectedIndex = -1;
    // Photoshop pattern: entering Crop seeds a visible frame ready
    // to adjust. We inset 6% from each edge so all 8 handles sit
    // INSIDE the canvas bounds — at the literal corners they'd be
    // half-clipped and effectively unclickable.
    if (!cropRect) {
      const insetX = Math.round(baseW * 0.06);
      const insetY = Math.round(baseH * 0.06);
      cropRect = {
        x: insetX,
        y: insetY,
        w: baseW - insetX * 2,
        h: baseH - insetY * 2,
      };
    }
    updateCropHint();
    redraw();
    return;
  }
  else { canvas.style.cursor = "crosshair"; selectedIndex = -1; redraw(); }
  // Leaving crop mode clears any pending crop selection.
  if (cropRect) { cropRect = null; cropDragMode = null; redraw(); }
}

function setColor(c) {
  currentColor = c;
  document.querySelectorAll(".swatch").forEach((b) => {
    b.classList.toggle("active", b.dataset.color === c);
  });
  if (selectedIndex >= 0 && marks[selectedIndex] && marks[selectedIndex].type !== "redact") {
    marks[selectedIndex].color = c;
    redraw();
    scheduleAutoSave();
  }
}

function attachHandlers() {
  document.querySelectorAll("#tools button").forEach((b) => {
    b.addEventListener("click", () => setTool(b.dataset.tool));
  });
  document.querySelectorAll(".swatch").forEach((b) => {
    b.addEventListener("click", () => setColor(b.dataset.color));
  });
  canvas.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || activeTextEditor) return;
    e.preventDefault();
    const p = eventToCanvas(e);
    if (currentTool === "select") startSelect(p);
    else startDraw(p);
  });
  canvas.addEventListener("dblclick", (e) => {
    if (e.button !== 0 || activeTextEditor) return;
    const p = eventToCanvas(e);
    const i = hitTest(p);
    if (i >= 0 && marks[i].type === "text") {
      e.preventDefault();
      const m = marks[i];
      selectedIndex = i;
      setTool("text");
      spawnTextEditor({ x: m.x, y: m.y }, {
        editingIndex: i, initialText: m.text,
        fontSize: m.fontSize || 18, color: m.color,
      });
    }
  });
  window.addEventListener("mousemove", (e) => {
    const p = eventToCanvas(e);
    if (currentTool === "select") moveSelect(p);
    else if (currentTool === "crop") moveDraw(p);   // handles both drag and hover-cursor
    else if (drawing) moveDraw(p);
  });
  window.addEventListener("mouseup", () => {
    if (currentTool === "select") endSelect();
    else endDraw();
  });
  window.addEventListener("keydown", (e) => {
    if (activeTextEditor) return;
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "1") setTool("arrow");
    else if (e.key === "2") setTool("rect");
    else if (e.key === "3") setTool("redact");
    else if (e.key === "4") setTool("text");
    else if (e.key === "5") setTool("select");
    else if (e.key === "6") setTool("crop");
    else if (e.key === "c" && currentTool !== "crop") {
      setColor(currentColor === "ink" ? "accent" : "ink");
    }
    else if (e.key === "Enter" && currentTool === "crop" && cropRect) {
      e.preventDefault();
      applyCrop();
    }
    else if (e.key === "Escape") {
      if (currentTool === "crop" && cropRect) { e.preventDefault(); cancelCrop(); }
      else if (drawing) { drawing = null; drawingStart = null; redraw(); }
      else if (selectedIndex >= 0) { selectedIndex = -1; redraw(); }
    } else if (e.key === "Delete" || e.key === "Backspace") {
      if (selectedIndex >= 0) { e.preventDefault(); deleteSelected(); }
    } else if (e.key === "z" && (e.metaKey || e.ctrlKey)) {
      if (marks.length) {
        marks.pop();
        if (selectedIndex >= marks.length) selectedIndex = -1;
        redraw(); scheduleAutoSave();
      }
    }
  });
}

// -- share-URL bar ---------------------------------------------------------

async function copyPermalink(button, copiedLabel = "Copied") {
  if (!shareUrl) return;
  try {
    await navigator.clipboard.writeText(shareUrl);
    if (!button) return;
    const original = button.textContent;
    button.textContent = copiedLabel;
    button.classList.add("url-copied");
    setTimeout(() => {
      button.textContent = original;
      button.classList.remove("url-copied");
    }, 1200);
  } catch (e) {
    console.error("[editor] copy failed", e);
  }
}

function attachUrlBarHandlers() {
  const tbCopy = document.getElementById("copy-permalink");
  if (tbCopy) tbCopy.addEventListener("click", () => copyPermalink(tbCopy));
}

// -- auto-save + discard ---------------------------------------------------

const AUTOSAVE_DEBOUNCE_MS = 1500;
const AUTOSAVE_RETRY_MS = 5000;
let saveState = "saved";
let saveTimer = null;
let queuedWhileSaving = false;
let discarded = false;   // once set, no further writes may fire
let inflightSave = null; // the current autosave fetch, so discard can await it

function setSaveStatus(state, label) {
  saveState = state;
  const el = document.getElementById("save-status");
  if (!el) return;
  el.className = `save-status save-${state}`;
  el.textContent = label || ({
    saved: "Saved", dirty: "Editing…", saving: "Saving…", failed: "Save failed — retrying",
  }[state] || state);
}

function scheduleAutoSave() {
  if (discarded) return;
  if (saveState === "saving") { queuedWhileSaving = true; return; }
  setSaveStatus("dirty");
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(performAutoSave, AUTOSAVE_DEBOUNCE_MS);
}

async function performAutoSave() {
  saveTimer = null;
  if (discarded || saveState === "saving" || !baseImage) return;
  setSaveStatus("saving");
  const prevSelected = selectedIndex;
  selectedIndex = -1;
  redraw();
  const blob = await new Promise((res) => canvas.toBlob(res, "image/png"));
  selectedIndex = prevSelected;
  redraw();
  if (!blob) {
    setSaveStatus("failed");
    saveTimer = setTimeout(performAutoSave, AUTOSAVE_RETRY_MS);
    return;
  }
  // Re-check after the async blob encode — a discard click can land
  // while toBlob is in flight, and a PUT fired past this point would
  // race the DELETE.
  if (discarded) return;
  // Encode path segments — same trick as the extension.
  const encodedPath = artPath.split("/").map(encodeURIComponent).join("/");
  try {
    inflightSave = fetch(mountUrl(`/web/artifacts/${encodedPath}`), {
      method: "PUT",
      credentials: "include",
      headers: {
        "Content-Type": "image/png",
        // No visibility header — annotation autosave preserves the clip's
        // existing visibility (the initial capture upload sets it).
        "X-CSRF-Token": csrfToken,
        "X-Drive-Id": driveId,
      },
      body: blob,
    });
    const r = await inflightSave;
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (e) {
    console.error("[editor] autosave failed", e);
    if (discarded) return;  // don't schedule a retry into a discarded session
    setSaveStatus("failed");
    saveTimer = setTimeout(performAutoSave, AUTOSAVE_RETRY_MS);
    return;
  } finally {
    inflightSave = null;
  }
  if (discarded) return;
  setSaveStatus("saved");
  if (queuedWhileSaving) { queuedWhileSaving = false; scheduleAutoSave(); }
}

// -- discard ----------------------------------------------------------------

// The clip is auto-uploaded before the user has even seen it, so the
// editor needs an escape hatch: Discard soft-deletes the artifact
// (recoverable from trash until purge_at) and returns to the dashboard.
async function performDiscard(btn) {
  if (!confirm("Delete this clip? The shared link will stop working.")) return;
  discarded = true;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  queuedWhileSaving = false;
  btn.disabled = true;
  setSaveStatus("saving", "Deleting…");
  // If an autosave PUT is mid-flight, wait for it to settle — no time
  // cap, because a PUT landing after the DELETE re-creates the artifact
  // as a fresh live row (upsert is `ON CONFLICT … WHERE deleted_at IS
  // NULL`) and the user was just told the link is dead. Large PNGs on
  // slow uplinks can outlive any guessed cap. A save that hasn't
  // reached its fetch yet bails on the `discarded` re-checks instead.
  if (inflightSave) {
    try { await inflightSave; } catch (_e) { /* settled either way */ }
  }
  const encodedPath = artPath.split("/").map(encodeURIComponent).join("/");
  try {
    const r = await fetch(mountUrl(`/web/artifacts/${encodedPath}`), {
      method: "DELETE",
      credentials: "include",
      headers: { "X-CSRF-Token": csrfToken, "X-Drive-Id": driveId },
    });
    // 404 = already gone (double-click, deleted elsewhere) — the
    // user's intent is satisfied either way.
    if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
    location.href = mountUrl("/dashboard");
  } catch (e) {
    console.error("[editor] discard failed", e);
    discarded = false;
    btn.disabled = false;
    setSaveStatus("failed", "Delete failed — retry");
  }
}

function attachDiscardHandler() {
  const btn = document.getElementById("discard-clip");
  if (btn) btn.addEventListener("click", () => performDiscard(btn));
}

init();

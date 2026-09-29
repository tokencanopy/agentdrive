// Offscreen document: a tiny DOM context kept open by the SW for
// (a) keeping the SW alive past 30s during uploads, and (b) running
// `document.execCommand('copy')` against a hidden textarea (the
// MV3-compatible clipboard pattern — `navigator.clipboard.writeText`
// has known focus issues from a SW caller).

const ta = document.getElementById("clip");

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "offscreen") return;
  if (msg.type === "copy") {
    try {
      ta.value = msg.text;
      ta.focus();
      ta.select();
      const ok = document.execCommand("copy");
      sendResponse({ ok });
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
    return true;  // keep channel open for sendResponse
  }
  if (msg.type === "ping") {
    sendResponse({ ok: true });
    return true;
  }
});

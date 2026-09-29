/* Trusted-shell behaviour. The shell is server-rendered and complete without
   this file: the header, the metadata, the download link and the framed
   document are all in the first response. What runs here is the part HTML
   cannot express — the clipboard and a theme shared with the frame.
   Every control it owns ships `hidden` in the markup
   and is revealed by the code that makes it work. A button that does nothing
   with script off is worse than no button.

   NOT deferred, and that is the same call `viewer.js` makes: the theme has to
   resolve before the first paint or a dark reader watches the bar flash white.

   Two hard rules on the frame boundary, both mirroring the private viewer:
   every outbound message names the frame's exact origin (never `*`), and every
   inbound one is validated on BOTH `event.source` and `event.origin` before a
   single field is read. Nothing crossing this boundary carries or gates a
   capability — only the resolved theme crosses this boundary. */
(function () {
  "use strict";

  var KEY = "agentdrive-theme";
  var ORDER = ["auto", "light", "dark"];
  var PROTOCOL = 1;

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return ORDER.indexOf(v) === -1 ? "auto" : v;
    } catch (e) {
      // Storage throws outright in private mode or with cookies blocked. A
      // share link must never fail to render over a preference.
      return "auto";
    }
  }

  function resolve(pref) {
    if (pref !== "auto") return pref;
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function paint(pref) {
    var theme = resolve(pref);
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }

  // Pre-paint. Runs at parse time, before <body> exists.
  paint(stored());

  // Resolve lazily: the listener starts before the iframe is parsed.
  function theFrame() {
    return document.querySelector(".shell-content iframe");
  }

  function frameOrigin() {
    /* The origin comes from the src the SERVER wrote — `render_shell` has
       already proved it is the configured renderer origin — so this is a
       server-validated value read back, not a value from the frame. */
    var frame = theFrame();
    if (!frame) return null;
    try {
      return new URL(frame.src, window.location.href).origin;
    } catch (e) {
      return null;
    }
  }

  function tellFrame(theme) {
    var frame = theFrame();
    var origin = frameOrigin();
    if (!frame || !origin || !frame.contentWindow) return;
    frame.contentWindow.postMessage(
      { type: "agentdrive.viewer.theme", protocol: PROTOCOL, theme: theme },
      origin
    );
  }

  window.addEventListener("message", function (event) {
    var frame = theFrame();
    var origin = frameOrigin();
    if (!frame || !origin) return;
    if (event.source !== frame.contentWindow) return;
    if (event.origin !== origin) return;
    var data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.protocol !== PROTOCOL) return;
    if (data.type === "agentdrive.viewer.ready") {
      tellFrame(resolve(stored()));
      return;
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    /* Belt and braces against a frame that had already finished parsing, and
       announced itself, before this document's listener could be attached at
       all — a cached frame on a slow shell. */
    var frame = theFrame();
    if (frame) {
      frame.addEventListener("load", function () {
        tellFrame(resolve(stored()));
      });
    }

    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) {
      var label = function () {
        var pref = stored();
        toggle.textContent =
          pref === "auto" ? "Auto" : pref === "dark" ? "Dark" : "Light";
        toggle.setAttribute(
          "aria-label",
          "Colour theme: " + toggle.textContent + ". Click to change."
        );
      };
      label();
      toggle.hidden = false;
      toggle.addEventListener("click", function () {
        var next = ORDER[(ORDER.indexOf(stored()) + 1) % ORDER.length];
        try {
          localStorage.setItem(KEY, next);
        } catch (e) {
          /* Preference will not persist; the page still switches. */
        }
        paint(next);
        label();
        tellFrame(resolve(next));
      });
    }

    // Follow the OS while the reader is on "auto", so a system theme change
    // takes effect in both documents without a reload.
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function () {
      if (stored() === "auto") {
        paint("auto");
        tellFrame(resolve("auto"));
      }
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);

    var copy = document.querySelector("[data-copy-url]");
    if (copy && navigator.clipboard && navigator.clipboard.writeText) {
      copy.hidden = false;
      copy.addEventListener("click", function () {
        /* Empty for a `/s/` capability link, which has no non-secret URL to
           advertise — there the address bar the reader is already looking at
           is the thing to copy, and it never enters the response. */
        var url = copy.getAttribute("data-copy-url") || window.location.href;
        var done = function (ok) {
          var prev = copy.textContent;
          copy.textContent = ok ? "Copied" : "Copy failed";
          setTimeout(function () {
            copy.textContent = prev;
          }, 1600);
        };
        navigator.clipboard.writeText(url).then(
          function () { done(true); },
          function () { done(false); }
        );
      });
    }
  });
})();

/* Public viewer behaviour. Loaded from <head> WITHOUT defer/async, so it runs
   before first paint — the theme has to be resolved before the browser paints
   or every visit flashes light before switching to dark.

   Deliberately an external file with no inline handlers anywhere: the page's
   CSP is `script-src 'self'` and must never need `'unsafe-inline'`, because
   that is precisely the directive that would let an injected inline script
   run and undo the backstop behind the renderer's escaping.

   `agentdrive-theme` is the same localStorage key the rest of the product
   uses, so a reader who set a preference in the dashboard keeps it here. */
(function () {
  "use strict";

  var assetQuery = new URL(document.currentScript.src).search;
  var KEY = "agentdrive-theme";
  var ORDER = ["auto", "light", "dark"];
  var PROTOCOL = 1;

  /* Both flags are on <html> rather than <body> because this script runs at
     parse time, before <body> exists — and because the stylesheet's
     full-height PDF chain has to start at the root element. The server sets
     them; being inside a frame is never INFERRED from `window !== top`, which
     any page could arrange for itself. */
  var EMBEDDED = document.documentElement.hasAttribute("data-embedded");
  var EMBED_ORIGIN =
    document.documentElement.getAttribute("data-embed-origin") || "";

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return ORDER.indexOf(v) === -1 ? "auto" : v;
    } catch (e) {
      // Storage can throw outright in private mode or with cookies blocked.
      // A viewer must never fail to render over a preference.
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

  /* Pre-paint. Runs at parse time, before <body> exists.

     Embedded, the parent owns the preference and this origin's own
     localStorage is deliberately NOT consulted: a reader who once opened the
     renderer origin directly and chose dark there would otherwise see that
     stale choice flash before the shell's real one arrived. The OS is the
     right floor until the first message lands. */
  paint(EMBEDDED ? "auto" : stored());

  /* Embedded pages receive only the shell's theme. The viewport owns its
     scrolling, so document height is never sent back to resize the iframe.
     Validate both source and origin before accepting a theme. */
  function embed() {
    if (!EMBED_ORIGIN) return; // misconfigured: say nothing at all
    var post = function (type, extra) {
      var message = { type: type, protocol: PROTOCOL };
      if (extra) {
        for (var k in extra) {
          if (Object.prototype.hasOwnProperty.call(extra, k)) message[k] = extra[k];
        }
      }
      window.parent.postMessage(message, EMBED_ORIGIN);
    };

    window.addEventListener("message", function (event) {
      if (event.source !== window.parent) return;
      if (event.origin !== EMBED_ORIGIN) return;
      var data = event.data;
      if (!data || typeof data !== "object") return;
      if (data.protocol !== PROTOCOL) return;
      if (data.type !== "agentdrive.viewer.theme") return;
      if (data.theme !== "light" && data.theme !== "dark") return;
      paint(data.theme);
    });

    post("agentdrive.viewer.ready");

  }

  document.addEventListener("DOMContentLoaded", function () {
    import("./reading.js" + assetQuery).then(function (reading) {
      var root = document.querySelector("main.doc");
      reading.wireReadingControls(root,
        document.documentElement.getAttribute("data-mode") || "");
      if (EMBEDDED && root) document.documentElement.dataset.reading = "contained";
    });

    if (EMBEDDED) {
      embed();
      return;
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
      toggle.addEventListener("click", function () {
        var next = ORDER[(ORDER.indexOf(stored()) + 1) % ORDER.length];
        try {
          localStorage.setItem(KEY, next);
        } catch (e) {
          /* Preference will not persist; the page still switches. */
        }
        paint(next);
        label();
      });
    }

    // Follow the OS while the reader is on "auto", so a system theme change
    // takes effect without a reload.
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function () {
      if (stored() === "auto") paint("auto");
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);

    var copy = document.querySelector("[data-copy-url]");
    if (copy) {
      copy.addEventListener("click", function () {
        var url = copy.getAttribute("data-copy-url") || window.location.href;
        var done = function (ok) {
          var prev = copy.textContent;
          copy.textContent = ok ? "Copied" : "Copy failed";
          setTimeout(function () {
            copy.textContent = prev;
          }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(
            function () { done(true); },
            function () { done(false); }
          );
        } else {
          done(false);
        }
      });
    }
  });
})();

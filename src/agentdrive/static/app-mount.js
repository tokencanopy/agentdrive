/* Same-origin URL prefixer for JS. Mirrors the Jinja u() helper:
   window.__APP_MOUNT__ is injected by base.html ("" on agentdrive.run,
   "/drive" on the TokenCanopy host). */
window.mountUrl = function (path) {
  var prefix = window.__APP_MOUNT__ || "";
  return prefix + path;
};

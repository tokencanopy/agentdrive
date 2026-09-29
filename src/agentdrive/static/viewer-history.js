(function () {
  "use strict";

  var states = new WeakMap();
  var mobile = window.matchMedia("(max-width: 760px)");

  function stateFor(control) {
    var state = states.get(control);
    if (!state) {
      state = { loaded: false, loading: false, nextCursor: null, seen: new Set() };
      states.set(control, state);
    }
    return state;
  }

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = value;
    return node;
  }

  function versionHref(control, item) {
    var artId = encodeURIComponent(control.dataset.artId);
    return item.is_head
      ? "/a/" + artId
      : "/v/" + artId + "/" + item.version_number;
  }

  function formatTime(value) {
    var date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Unknown time"
      : new Intl.DateTimeFormat(undefined, {
          dateStyle: "medium",
          timeStyle: "short"
        }).format(date);
  }

  function makeRow(control, item) {
    var selected = Number(control.dataset.selectedVersion) === item.version_number;
    var link = element("a", "version-row" + (selected ? " is-selected" : ""));
    link.href = versionHref(control, item);
    if (selected) link.setAttribute("aria-current", "page");
    link.appendChild(element("span", "version-number", "v" + item.version_number));

    var meta = element("span", "version-meta");
    var label = item.is_head
      ? "Latest · " + formatTime(item.created_at)
      : formatTime(item.created_at);
    var time = element("time", "version-time", label);
    time.dateTime = item.created_at;
    meta.appendChild(time);
    meta.appendChild(element(
      "span",
      "version-actor",
      item.actor_name || "No actor recorded"
    ));
    if (item.change_summary) {
      var summary = element("span", "version-summary", item.change_summary);
      summary.title = item.change_summary;
      meta.appendChild(summary);
    }
    link.appendChild(meta);
    link.appendChild(element("span", "version-check", selected ? "✓" : ""));
    return link;
  }

  function showMessage(list, message, action) {
    list.replaceChildren();
    var box = element("div", "version-message", message);
    if (action) box.appendChild(action);
    list.appendChild(box);
  }

  function renderPinned(control, list, state, pageItems) {
    var selected = Number(control.dataset.selectedVersion);
    if (pageItems.some(function (item) {
      return item.version_number === selected;
    })) return;
    if (state.seen.has(selected) || !control.dataset.selectedCreatedAt) return;
    list.appendChild(makeRow(control, {
      version_number: selected,
      created_at: control.dataset.selectedCreatedAt,
      actor_name: control.dataset.selectedActor || null,
      change_summary: null,
      is_head: selected === Number(control.dataset.headVersion)
    }));
    state.seen.add(selected);
    list.appendChild(element("div", "version-divider", "Recent versions"));
  }

  function appendPage(control, payload, reset) {
    var list = control.querySelector("[data-version-list]");
    var state = stateFor(control);
    if (reset) {
      list.replaceChildren();
      renderPinned(control, list, state, payload.items);
    } else {
      var oldMore = list.querySelector("[data-version-more]");
      if (oldMore) oldMore.remove();
    }
    payload.items.forEach(function (item) {
      if (state.seen.has(item.version_number)) return;
      state.seen.add(item.version_number);
      list.appendChild(makeRow(control, item));
    });
    state.nextCursor = payload.next_cursor;
    if (state.nextCursor) {
      var more = element("button", "version-more", "Load older versions");
      more.type = "button";
      more.dataset.versionMore = "";
      list.appendChild(more);
    } else if (payload.pruned_before) {
      list.appendChild(element(
        "p",
        "version-retention",
        "Versions before v" + payload.pruned_before
          + " were removed by the retention policy."
      ));
    }
  }

  async function load(control, reset) {
    var state = stateFor(control);
    if (state.loading || (!reset && !state.nextCursor)) return;
    if (reset) {
      state.seen.clear();
      state.nextCursor = null;
    }
    state.loading = true;
    var list = control.querySelector("[data-version-list]");
    var timer = window.setTimeout(function () {
      if (!state.loading) return;
      list.replaceChildren();
      for (var i = 0; i < 3; i += 1) {
        list.appendChild(element("div", "version-skeleton"));
      }
    }, 250);
    var params = new URLSearchParams({ limit: "20" });
    if (!reset && state.nextCursor) params.set("cursor", state.nextCursor);
    try {
      var response = await fetch(
        mountUrl("/a/" + encodeURIComponent(control.dataset.artId) + "/versions?" + params),
        { credentials: "same-origin", cache: "no-store" }
      );
      if (!response.ok) throw new Error("history request failed");
      var payload = await response.json();
      appendPage(control, payload, reset);
      state.loaded = true;
    } catch (_) {
      var retry = element("button", "version-retry", "Retry");
      retry.type = "button";
      retry.dataset.versionRetry = "";
      showMessage(list, "Version history couldn't be loaded.", retry);
    } finally {
      window.clearTimeout(timer);
      state.loading = false;
    }
  }

  function close(control) {
    var trigger = control.querySelector("[data-version-trigger]");
    control.querySelector("[data-version-popover]").hidden = true;
    control.querySelector("[data-version-scrim]").hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    trigger.focus();
  }

  function open(control) {
    var trigger = control.querySelector("[data-version-trigger]");
    var popover = control.querySelector("[data-version-popover]");
    popover.hidden = false;
    control.querySelector("[data-version-scrim]").hidden = !mobile.matches;
    trigger.setAttribute("aria-expanded", "true");
    var state = stateFor(control);
    if (!state.loaded) load(control, true);
    window.requestAnimationFrame(function () {
      var selected = popover.querySelector('[aria-current="page"]');
      (selected || popover).focus();
    });
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-version-trigger]");
    if (trigger) {
      var control = trigger.closest("[data-version-control]");
      if (trigger.getAttribute("aria-expanded") === "true") close(control);
      else open(control);
      return;
    }
    var more = event.target.closest("[data-version-more]");
    if (more) return void load(more.closest("[data-version-control]"), false);
    var retry = event.target.closest("[data-version-retry]");
    if (retry) return void load(retry.closest("[data-version-control]"), true);
    var scrim = event.target.closest("[data-version-scrim]");
    if (scrim) return close(scrim.closest("[data-version-control]"));
    document.querySelectorAll(
      '[data-version-trigger][aria-expanded="true"]'
    ).forEach(function (openTrigger) {
      var openControl = openTrigger.closest("[data-version-control]");
      if (!openControl.contains(event.target)) close(openControl);
    });
  });

  document.addEventListener("keydown", function (event) {
    var control = event.target.closest
      && event.target.closest("[data-version-control]");
    if (!control) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close(control);
      return;
    }
    if (event.key !== "Tab" || !mobile.matches) return;
    var focusable = Array.from(control.querySelectorAll(
      "[data-version-popover] a, [data-version-popover] button"
    )).filter(function (node) {
      return !node.hidden;
    });
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  document.addEventListener("agentdrive:head-version-changed", function () {
    document.querySelectorAll("[data-version-control]").forEach(function (control) {
      states.delete(control);
    });
  });
})();

// The assistant lane, and keeping the page still while the server redraws it.
//
// Everything the lane shows is server-rendered. This file only remembers which
// block and which mode the author is looking at, reads what they select, and
// puts the page back where it was after a swap. Delegated from the document, so
// an HTMX swap needs no rebinding.
(function () {
  "use strict";

  var MINIMUM = 4; // a shorter selection is not unique enough to pin

  // --- which block, which mode ---------------------------------------------

  function lane() { return document.querySelector("[data-assistant]"); }

  function storeKey(el) { return "lcf.lane." + el.dataset.scope; }

  function saved(el) {
    try { return JSON.parse(sessionStorage.getItem(storeKey(el)) || "{}"); } catch (e) { return {}; }
  }

  function save(el, state) {
    try { sessionStorage.setItem(storeKey(el), JSON.stringify(state)); } catch (e) { /* private mode */ }
  }

  // Closed until the author asks for it with ✦ Assistant, and only for this
  // page: a redraw keeps it open, a new page starts closed again.
  var open = false;

  function apply(state) {
    var el = lane();
    if (!el) return;
    var shut = !open;
    var workspace = el.closest(".layout");
    if (workspace) workspace.classList.toggle("lane-closed", shut);
    state = state || {};
    var blocks = Array.prototype.map.call(el.querySelectorAll("[data-conv]"), function (c) {
      return c.dataset.block;
    });
    var focus = blocks.indexOf(state.focus) >= 0 ? state.focus : el.dataset.focus || blocks[0];
    var mode = state.mode === "discuss" ? "discuss" : "rewrite";
    el.dataset.focus = focus;
    el.dataset.mode = mode;
    el.querySelectorAll("[data-conv]").forEach(function (c) {
      c.hidden = c.dataset.block !== focus;
    });
    el.querySelectorAll("[data-mode-to]").forEach(function (b) {
      b.classList.toggle("on", b.dataset.modeTo === mode);
      b.setAttribute("aria-selected", b.dataset.modeTo === mode);
    });
    el.querySelectorAll(".block-tab").forEach(function (b) {
      b.classList.toggle("on", b.dataset.focusTo === focus);
    });
    document.querySelectorAll(".block[data-block]").forEach(function (b) {
      b.classList.toggle("focused", !shut && b.dataset.block === focus);
    });
    save(el, { focus: focus, mode: mode });
  }

  function current() {
    var el = lane();
    return el ? { focus: el.dataset.focus, mode: el.dataset.mode } : {};
  }

  function visibleThread() {
    var el = lane();
    if (!el) return null;
    return el.querySelector('[data-conv]:not([hidden]) [data-pane="' + el.dataset.mode + '"] [data-thread]');
  }

  function toBottom() {
    var t = visibleThread();
    if (t) t.scrollTop = t.scrollHeight;
  }

  // Follow a reply as it grows, unless the author has scrolled up to read.
  var following = true;

  function activeComposer() {
    var el = lane();
    if (!el) return null;
    var conv = el.querySelector("[data-conv]:not([hidden])");
    return conv && conv.querySelector('[data-pane="' + el.dataset.mode + '"] form[data-compose]');
  }

  document.addEventListener("click", function (event) {
    var t = event.target.closest("[data-mode-to], [data-focus-to], [data-fill], [data-view], " +
      "[data-focus-selection], [data-focus-clear], [data-quote-selection], [data-copy], " +
      "[data-copy-passage], [data-lane-close]");
    if (!t) return;
    var state = current();

    if (t.hasAttribute("data-lane-close")) {
      open = false;
      apply(state);
    } else if (t.hasAttribute("data-copy-passage")) {
      var passage = t.parentElement.cloneNode(true);
      passage.querySelectorAll("[data-copy-passage]").forEach(function (b) { b.remove(); });
      copy(passage.innerText.trim(), t);
    } else if (t.dataset.modeTo) {
      state.mode = t.dataset.modeTo;
      apply(state);
      toBottom();
    } else if (t.dataset.focusTo) {
      state.focus = t.dataset.focusTo;
      open = true;
      apply(state);
      toBottom();
      var el = lane();
      // Stacked under the text on a narrow screen: go to it.
      if (el && !t.closest("[data-assistant]") && el.getBoundingClientRect().top > window.innerHeight) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } else if (t.dataset.fill !== undefined) {
      var area = t.closest("form").querySelector("textarea");
      area.value = t.dataset.fill;
      area.focus();
    } else if (t.dataset.view) {
      var msg = t.closest(".msg");
      msg.querySelectorAll("[data-view]").forEach(function (b) {
        b.classList.toggle("on", b === t);
      });
      msg.querySelectorAll("[data-view-pane]").forEach(function (p) {
        p.hidden = p.dataset.viewPane !== t.dataset.view;
      });
    } else if (t.hasAttribute("data-focus-selection")) {
      var pane = t.closest(".pane");
      setFocus(pane, pane.querySelector("[data-pin-quote]").value);
    } else if (t.hasAttribute("data-focus-clear")) {
      setFocus(t.closest(".pane"), "");
    } else if (t.hasAttribute("data-quote-selection")) {
      var p = t.closest(".pane");
      var box = p.querySelector("form[data-compose] textarea");
      var quote = p.querySelector("[data-sel-quote]").dataset.full || "";
      box.value = "> " + quote + "\n\n" + box.value;
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    } else if (t.hasAttribute("data-copy")) {
      copy(t.closest(".msg").querySelector(".md").innerText, t);
    }
  });

  function copy(text, button) {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      button.classList.add("copied");
      if (button.hasAttribute("data-copy")) button.textContent = "Copied";
      setTimeout(function () {
        button.classList.remove("copied");
        if (button.hasAttribute("data-copy")) button.textContent = "Copy";
      }, 1400);
    });
  }

  function setFocus(pane, quote) {
    var chip = pane.querySelector("[data-focus-chip]");
    var field = pane.querySelector("[data-focus-field]");
    if (!chip || !field) return;
    field.value = quote;
    chip.hidden = !quote;
    chip.querySelector("[data-focus-text]").textContent =
      quote.length > 60 ? quote.slice(0, 60) + "…" : quote;
    if (quote) pane.querySelector("form[data-compose] textarea").focus();
  }

  // Ctrl/Cmd+Enter sends. Plain Enter is a new line: remarks are often several.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    var form = event.target.closest && event.target.closest("form[data-compose]");
    if (form && !form.querySelector("button[type=submit]").disabled) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  // --- what the author selected ---------------------------------------------

  function selectionIn(element) {
    if (element.tagName === "TEXTAREA") {
      return element.value.slice(element.selectionStart, element.selectionEnd);
    }
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !element.contains(selection.anchorNode)) return "";
    return selection.toString();
  }

  function pinnable() {
    var active = document.activeElement;
    if (active && active.tagName === "TEXTAREA" && active.hasAttribute("data-pinnable")) return active;
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.anchorNode) return null;
    var node = selection.anchorNode;
    var element = node.nodeType === 1 ? node : node.parentElement;
    return element ? element.closest("[data-pinnable]") : null;
  }

  function showSelection(block, quote) {
    document.querySelectorAll("[data-assistant] [data-conv]").forEach(function (conv) {
      var mine = conv.dataset.block === block && quote.length >= MINIMUM;
      conv.querySelectorAll("[data-selection]").forEach(function (bar) {
        bar.hidden = !mine;
        if (!mine) return;
        var label = bar.querySelector("[data-sel-quote]");
        label.textContent = "“" + (quote.length > 80 ? quote.slice(0, 80) + "…" : quote) + "”";
        label.dataset.full = quote;
        var field = bar.querySelector("[data-pin-quote]");
        if (field) field.value = quote;
      });
    });
  }

  function onSelect(event) {
    // A click in the lane's own controls must not clear what it is about to use.
    if (event && event.target && event.target.closest &&
        event.target.closest("[data-selection], form[data-compose]")) return;
    var source = pinnable();
    var holder = source && source.closest("[data-block]");
    var quote = source ? selectionIn(source).trim() : "";
    if (holder && quote.length >= MINIMUM) {
      if (current().focus !== holder.dataset.block && source.tagName === "TEXTAREA") {
        var s = current();
        s.focus = holder.dataset.block;
        apply(s);
      }
      showSelection(holder.dataset.block, quote);
    } else if (event && event.type !== "selectionchange") {
      showSelection(null, "");
    }
  }

  document.addEventListener("mouseup", onSelect);
  document.addEventListener("keyup", onSelect);

  // Working in a prose block brings its conversation up beside it.
  document.addEventListener("focusin", function (event) {
    var area = event.target;
    if (area.tagName !== "TEXTAREA" || !area.hasAttribute("data-pinnable")) return;
    var holder = area.closest(".block[data-block]");
    if (holder && current().focus !== holder.dataset.block) {
      var s = current();
      s.focus = holder.dataset.block;
      apply(s);
    }
  });

  // --- unsent text survives a redraw ----------------------------------------

  var drafts = {};

  function draftKey(area) {
    var conv = area.closest("[data-conv]");
    var pane = area.closest("[data-pane]");
    return conv && pane ? conv.dataset.block + "/" + pane.dataset.pane : null;
  }

  function keepDrafts() {
    document.querySelectorAll("[data-assistant] form[data-compose] textarea").forEach(function (a) {
      var k = draftKey(a);
      if (k) drafts[k] = a.value;
    });
  }

  function restoreDrafts() {
    document.querySelectorAll("[data-assistant] form[data-compose] textarea").forEach(function (a) {
      var k = draftKey(a);
      if (k && drafts[k] && !a.value && !a.disabled) a.value = drafts[k];
    });
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var form = event.detail.elt.closest && event.detail.elt.closest("form[data-compose]");
    if (!form) return;
    var area = form.querySelector("textarea");
    var k = area && draftKey(area);
    if (k) delete drafts[k];
    if (area) area.value = "";
    var field = form.querySelector("[data-focus-field]");
    if (field && field.value) setFocus(form.closest(".pane"), "");
  });

  document.addEventListener("htmx:beforeSwap", function () {
    keepDrafts();
    var thread = visibleThread();
    following = !thread || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
  });

  document.addEventListener("htmx:afterSwap", function () {
    apply(saved(lane() || { dataset: {} }));
    restoreDrafts();
    if (following) toBottom();
  });

  function boot() {
    var el = lane();
    if (el) { apply(saved(el)); toBottom(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

// The page stays where it was when the server redraws part of it.
//
// A swap replaces a region, and anything above the reader that changed height
// moves what they were looking at. So before every swap one element is chosen —
// the one the action came from, or failing that the first thing on screen — and
// after the swap the same element is put back at the same height.
(function () {
  "use strict";

  var LANDMARK = "[id], .block[data-block], details, .card, .field, li, tr, form";
  // Lanes that stay put while the page scrolls: measuring one tells nothing.
  var STICKY = ".lane-sticky, nav.sections";
  var anchor = null;

  function onScreen(el) {
    var r = el.getBoundingClientRect();
    return r.bottom > 64 && r.top < window.innerHeight;
  }

  // A way back to an element after its region has been replaced. Ids and block
  // keys survive a redraw; otherwise the element is found again by what it is —
  // its tag, what it posts to, its name, its heading — counted among the
  // elements that look the same, which survives a row being added before it.
  function keyOf(el, target) {
    if (el.id && el !== target) return "#" + CSS.escape(el.id);
    if (el.matches(".block[data-block]")) {
      return '.block[data-block="' + CSS.escape(el.dataset.block) + '"]';
    }
    return null;
  }

  function signature(el) {
    var parts = [el.tagName];
    ["hx-post", "hx-get", "action", "name", "data-dirty"].forEach(function (a) {
      if (el.hasAttribute(a)) parts.push(a + "=" + el.getAttribute(a));
    });
    if (el.tagName === "FORM") {
      var op = el.querySelector("input[name=op]");
      var first = el.querySelector("input[type=text], textarea");
      if (op) parts.push("op=" + op.value);
      if (first && first.placeholder) parts.push("ph=" + first.placeholder);
    }
    if (parts.length === 1) {
      var head = el.tagName === "DETAILS" ? el.querySelector("summary") : el;
      parts.push((head ? head.textContent : "").replace(/\s+/g, " ").trim().slice(0, 60));
    }
    return parts.join("|");
  }

  function alike(base, el) {
    var sig = signature(el);
    return Array.prototype.filter.call(base.querySelectorAll(el.tagName), function (other) {
      return signature(other) === sig;
    });
  }

  function locator(el, target) {
    var base = target;
    for (var node = el; node && node !== target; node = node.parentElement) {
      var key = keyOf(node, target);
      if (key && node === el) return { sel: key };
      if (key) { base = node; break; }
    }
    var baseSel = base === target ? null : keyOf(base, target);
    return {
      base: baseSel,
      tag: el.tagName,
      sig: signature(el),
      nth: alike(base, el).indexOf(el),
    };
  }

  function resolve(loc, targetId) {
    if (loc.sel) return document.querySelector(loc.sel);
    var base = loc.base ? document.querySelector(loc.base) : document.getElementById(targetId);
    if (!base) return null;
    var matches = Array.prototype.filter.call(base.querySelectorAll(loc.tag), function (other) {
      return signature(other) === loc.sig;
    });
    return matches[Math.min(Math.max(loc.nth, 0), matches.length - 1)] || null;
  }

  function firstVisible(target) {
    var all = target.querySelectorAll(LANDMARK);
    for (var i = 0; i < all.length; i++) {
      if (onScreen(all[i]) && !all[i].closest(STICKY)) return all[i];
    }
    return null;
  }

  function choose(target, elt) {
    var el = null;
    var fromLane = elt && elt.closest && elt.closest(STICKY);
    if (elt && elt.isConnected && target.contains(elt) && elt !== target && !fromLane) {
      el = elt.closest(LANDMARK);
      if (el && !target.contains(el)) el = null;
    } else if (elt && elt.isConnected) {
      // Asked from outside the region, the assistant lane for one: hold on to
      // the block it is about.
      el = target.querySelector(".block.focused");
    }
    if (!el || el === target || !onScreen(el)) el = firstVisible(target);
    return el;
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    anchor = null;
    var target = event.detail.target;
    if (!target || !target.isConnected || target.closest(STICKY)) return;
    var elt = event.detail.requestConfig && event.detail.requestConfig.elt;
    var el = choose(target, elt);
    if (!el) return;
    anchor = {
      loc: locator(el, target),
      targetId: target.id,
      top: el.getBoundingClientRect().top,
    };
  });

  document.addEventListener("htmx:afterSwap", function () {
    if (!anchor) return;
    var el = resolve(anchor.loc, anchor.targetId);
    if (el) window.scrollBy(0, el.getBoundingClientRect().top - anchor.top);
    anchor = null;
  });
})();

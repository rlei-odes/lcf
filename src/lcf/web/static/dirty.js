// Does this form hold anything that is not saved yet?
//
// Two problems, one answer. A page of Save buttons that all look pressable says
// nothing about which of them still need pressing — so a form that matches what
// was rendered disables its Save and says "Saved". And "Mark complete" sits
// outside the block forms, so clicking it with an edited-but-unsaved table threw
// the edit away silently — so anything that would navigate away from unsaved work
// refuses while there is any, and says which.
//
// The comparison is against what the server rendered, not a flag set on first
// keystroke: typing a character and deleting it again leaves a form clean, which
// is what a person would say about it.
(function () {
  "use strict";

  function snapshot(form) {
    try {
      return new URLSearchParams(new FormData(form)).toString();
    } catch (e) {
      return null; // a form we cannot serialise is one we do not claim to track
    }
  }

  function label(form) {
    return form.getAttribute("data-dirty") || "unsaved changes";
  }

  function refresh(form) {
    if (form.__clean === null) return;
    var dirty = snapshot(form) !== form.__clean;
    form.classList.toggle("is-dirty", dirty);
    // A form marked data-unconfirmed holds values nobody has agreed to yet —
    // answers intake proposed. Pressing Save there is the confirmation, not an
    // edit, so it stays pressable with nothing typed. Without this the author
    // who wants to change none of them has no way past the gate at all.
    var savable = dirty || form.hasAttribute("data-unconfirmed");
    form.querySelectorAll("[data-save]").forEach(function (button) {
      button.disabled = !savable;
    });
    guards();
  }

  // Anything marked data-needs-saved is blocked while work is outstanding, and
  // told why. Refusing is better than a confirm dialog here: the answer to "you
  // have unsaved work" is to save it, not to choose whether to lose it.
  function guards() {
    var dirty = Array.prototype.map.call(
      document.querySelectorAll("form.is-dirty[data-dirty]"),
      label
    );
    document.querySelectorAll("[data-needs-saved]").forEach(function (el) {
      el.disabled = dirty.length > 0 || el.hasAttribute("data-blocked");
    });
    document.querySelectorAll("[data-unsaved-note]").forEach(function (note) {
      note.hidden = dirty.length === 0;
      if (dirty.length) {
        note.textContent =
          "Save your changes to " + dirty.join(" and ") + " first.";
      }
    });
  }

  function track(root) {
    (root || document).querySelectorAll("form[data-dirty]").forEach(function (form) {
      if (form.__clean !== undefined) return;
      form.__clean = snapshot(form);
      form.__cleanEntries = entries(form);
      form.addEventListener("input", function () {
        refresh(form);
      });
      form.addEventListener("change", function () {
        refresh(form);
      });
      refresh(form);
    });
    guards();
  }

  document.addEventListener("DOMContentLoaded", function () {
    track(document);
  });
  // HTMX replaces whole regions, and the replacements arrive already saved.
  document.body && document.body.addEventListener("htmx:load", function (e) {
    track(e.target);
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    track(e.target);
  });

  // --- unsaved work survives a redraw -----------------------------------------
  //
  // Any action redraws a whole region, and every form in it comes back as the
  // server has it. So what was typed and not saved is read out before the swap
  // and put back after it — unless the server's copy changed meanwhile (an
  // accepted proposal, say), in which case the author is asked rather than
  // either version being dropped silently.

  // Hidden fields that change on every save without changing what a form is.
  var VOLATILE = { token: 1, focus: 1, opened: 1 };

  function identity(form) {
    var parts = [
      form.getAttribute("hx-post") || form.getAttribute("action") || "",
      form.getAttribute("data-dirty") || "",
      form.id || "",
    ];
    form.querySelectorAll("input[type=hidden]").forEach(function (h) {
      if (!VOLATILE[h.name]) parts.push(h.name + "=" + h.value);
    });
    return parts.join("|");
  }

  function editable(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name && ["hidden", "submit", "button", "file", "reset"].indexOf(el.type) < 0;
    });
  }

  function keyOf(el, seen) {
    if (el.type === "checkbox" || el.type === "radio") return el.name + "=" + el.value;
    seen[el.name] = (seen[el.name] || 0) + 1;
    return el.name + "#" + seen[el.name];
  }

  function entries(form) {
    var out = {};
    var seen = {};
    editable(form).forEach(function (el) {
      var checkable = el.type === "checkbox" || el.type === "radio";
      out[keyOf(el, seen)] = checkable ? el.checked : el.value;
    });
    return out;
  }

  function putBack(form, values) {
    var seen = {};
    editable(form).forEach(function (el) {
      var key = keyOf(el, seen);
      if (!(key in values)) return;
      if (el.type === "checkbox" || el.type === "radio") el.checked = values[key];
      else el.value = values[key];
    });
    refresh(form);
  }

  function same(a, b) {
    var ka = Object.keys(a).sort();
    var kb = Object.keys(b).sort();
    if (ka.join("\u0000") !== kb.join("\u0000")) return false;
    return ka.every(function (k) { return a[k] === b[k]; });
  }

  function find(id) {
    var all = document.querySelectorAll("form[data-dirty]");
    for (var i = 0; i < all.length; i++) if (identity(all[i]) === id) return all[i];
    return null;
  }

  function readable(values) {
    return Object.keys(values)
      .map(function (k) { return values[k]; })
      .filter(function (v) { return typeof v === "string" && v.trim(); })
      .join("\n\n");
  }

  function notice(html, where) {
    var box = document.createElement("div");
    box.className = "unsaved-kept";
    box.setAttribute("role", "status");
    box.innerHTML = html;
    if (where) where.insertBefore(box, where.querySelector(".actions") || null);
    else document.body.appendChild(box).classList.add("floating");
    box.addEventListener("click", function (e) {
      if (e.target.closest("[data-dismiss]")) box.remove();
    });
    return box;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  var held = [];
  var justSaved = null;

  document.addEventListener("htmx:beforeSwap", function (e) {
    held = [];
    justSaved = null;
    var target = e.detail.target;
    if (!target) return;
    var elt = e.detail.requestConfig && e.detail.requestConfig.elt;
    var sent = elt && elt.closest && elt.closest("form");
    if (sent && sent.hasAttribute("data-dirty")) justSaved = identity(sent);
    var forms = Array.prototype.slice.call(target.querySelectorAll("form.is-dirty[data-dirty]"));
    if (target.matches && target.matches("form.is-dirty[data-dirty]")) forms.push(target);
    forms.forEach(function (form) {
      if (form === sent) return; // what it held is what was just sent
      held.push({
        id: identity(form),
        label: label(form),
        mine: entries(form),
        clean: form.__cleanEntries,
      });
    });
  });

  document.addEventListener("htmx:afterSwap", function () {
    track(document);
    held.forEach(function (h) {
      var form = find(h.id);
      if (!form) {
        var text = readable(h.mine);
        if (!text) return;
        var box = notice(
          "<strong>Unsaved changes to " + escapeHtml(h.label) + " could not be put back.</strong>" +
          "<pre></pre><div class=\"unsaved-actions\">" +
          "<button type=\"button\" class=\"tool\" data-copy-kept>Copy them</button>" +
          "<button type=\"button\" class=\"tool\" data-dismiss>Dismiss</button></div>"
        );
        box.querySelector("pre").textContent = text;
        box.querySelector("[data-copy-kept]").addEventListener("click", function () {
          if (navigator.clipboard) navigator.clipboard.writeText(text);
        });
        return;
      }
      if (same(entries(form), h.clean)) {
        putBack(form, h.mine);
        return;
      }
      var box2 = notice(
        "<strong>Your unsaved changes were not put back</strong>, because " +
        escapeHtml(h.label) + " changed while you were editing it." +
        "<div class=\"unsaved-actions\">" +
        "<button type=\"button\" class=\"tool\" data-put-back>Put mine back</button>" +
        "<button type=\"button\" class=\"tool\" data-dismiss>Keep the new version</button></div>",
        form
      );
      box2.querySelector("[data-put-back]").addEventListener("click", function () {
        putBack(form, h.mine);
        box2.remove();
      });
    });
    held = [];

    // "Saved" is said once, by the form that was saved, rather than standing
    // next to every form that has nothing to save.
    if (justSaved) {
      var saved = find(justSaved);
      if (saved && !saved.classList.contains("is-dirty")) {
        saved.classList.add("just-saved");
        setTimeout(function () { saved.classList.remove("just-saved"); }, 2600);
      }
      justSaved = null;
    }
  });

  // Leaving the page is the one way to lose typing that no swap can catch.
  window.addEventListener("beforeunload", function (e) {
    if (document.querySelector("form.is-dirty[data-dirty]")) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();

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
})();

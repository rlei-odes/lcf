// The whole editor island: what did the author just select?
//
// Pinning a passage needs one fact the server cannot know, and nothing else. The
// marks on pinned text are rendered server-side, the pins are rows, and the
// rewrite is a job — so this file reads a selection, puts it in a hidden field,
// and stops. That is why there is no bundler in this repo.
//
// Two selection models, because the text lives in two kinds of element. A
// textarea has selectionStart/selectionEnd and is invisible to
// window.getSelection(); a rendered proposal is ordinary markup and has no
// selectionStart. Both are marked `data-pinnable`.
//
// Everything is delegated from the document, so an HTMX swap of the whole
// workspace needs no rebinding.
(function () {
  "use strict";

  // A selection too short to be unique is a pin that would match text the author
  // never meant, so it is refused here rather than stored and misread.
  var MINIMUM = 4;

  function selectionIn(element) {
    if (element.tagName === "TEXTAREA") {
      if (element.selectionStart === element.selectionEnd) return "";
      return element.value.slice(element.selectionStart, element.selectionEnd);
    }
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed) return "";
    if (!element.contains(selection.anchorNode)) return "";
    return selection.toString();
  }

  // The pinnable element the author is working in, or nothing. A selection in a
  // proposal and a selection in the text box both belong to the same block, so
  // the panel to fill is found by walking up to it.
  function pinnable() {
    var active = document.activeElement;
    if (active && active.tagName === "TEXTAREA" && active.hasAttribute("data-pinnable")) {
      return active;
    }
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.anchorNode) return null;
    var node = selection.anchorNode;
    var element = node.nodeType === 1 ? node : node.parentElement;
    return element ? element.closest("[data-pinnable]") : null;
  }

  function refresh() {
    var source = pinnable();
    var block = source && source.closest(".block");
    var quote = source ? selectionIn(source).trim() : "";

    document.querySelectorAll("[data-revise]").forEach(function (panel) {
      var field = panel.querySelector("[data-pin-quote]");
      var button = panel.querySelector("[data-pin-button]");
      var note = panel.querySelector("[data-pin-note]");
      if (!field || !button) return;

      var mine = block && panel.closest(".block") === block && quote.length >= MINIMUM;
      field.value = mine ? quote : "";
      button.disabled = !mine;
      if (!note) return;
      note.textContent = mine
        ? "“" + (quote.length > 70 ? quote.slice(0, 70) + "…" : quote) + "”"
        : note.dataset.idle;
    });
  }

  // Keep the idle wording the template shipped, so it can be changed there.
  function remember() {
    document.querySelectorAll("[data-pin-note]").forEach(function (note) {
      if (note.dataset.idle === undefined) {
        note.dataset.idle = note.textContent.trim();
      }
    });
  }

  document.addEventListener("selectionchange", refresh);
  // selectionchange does not fire for every textarea change in every browser,
  // and a click outside the selection has to clear the button.
  document.addEventListener("mouseup", refresh);
  document.addEventListener("keyup", refresh);
  document.addEventListener("DOMContentLoaded", remember);
  document.body && remember();
  document.addEventListener("htmx:afterSwap", function () {
    remember();
    refresh();
  });
})();

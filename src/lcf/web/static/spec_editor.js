// Tab inside the spec editor.
//
// YAML is indentation, and in a plain textarea Tab moves focus to the next
// control — so the one key you need most takes you out of the field. This inserts
// two spaces instead, and Shift+Tab removes them. Escape restores the browser's
// behaviour for anyone navigating by keyboard, so the field is not a trap.
(function () {
  "use strict";
  var INDENT = "  ";

  function lineStart(value, position) {
    return value.lastIndexOf("\n", position - 1) + 1;
  }

  document.addEventListener("keydown", function (event) {
    var field = event.target;
    if (!field.classList || !field.classList.contains("code")) return;

    if (event.key === "Escape") {
      field.blur();
      return;
    }
    if (event.key !== "Tab" || event.ctrlKey || event.metaKey || event.altKey) return;
    event.preventDefault();

    var value = field.value;
    var start = field.selectionStart;
    var from = lineStart(value, start);

    if (event.shiftKey) {
      if (value.slice(from, from + INDENT.length) !== INDENT) return;
      field.value = value.slice(0, from) + value.slice(from + INDENT.length);
      field.selectionStart = field.selectionEnd = Math.max(from, start - INDENT.length);
      return;
    }
    field.value = value.slice(0, start) + INDENT + value.slice(field.selectionEnd);
    field.selectionStart = field.selectionEnd = start + INDENT.length;
  });
})();

// Moving on saves first.
//
// In the document flow the way on is the step's own submit button, so there is
// nothing to lose. In the builder a step holds several forms and the way on is a
// link, so a link marked data-save-first saves every form dirty.js says is unsaved,
// one at a time, and then follows itself. It stops, and stays on the page, if a
// save fails or the server refuses an edit — the notice says why, and the typing
// is still there.
(function () {
  "use strict";

  function dirtyForm() {
    return document.querySelector("form.is-dirty[data-dirty]");
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-save-first]");
    if (!link || !dirtyForm() || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    var href = link.href;
    var tries = document.querySelectorAll("form.is-dirty[data-dirty]").length + 1;

    function step() {
      var form = dirtyForm();
      if (!form) {
        window.location.href = href;
        return;
      }
      if (tries-- <= 0) return;
      document.addEventListener("htmx:afterSettle", function done(e) {
        document.removeEventListener("htmx:afterSettle", done);
        if (document.getElementById("edit-refused")) return;
        step();
      });
      document.addEventListener("htmx:afterRequest", function failed(e) {
        document.removeEventListener("htmx:afterRequest", failed);
        if (!e.detail.successful) tries = 0;
      });
      htmx.trigger(form, "submit");
    }
    step();
  });
})();

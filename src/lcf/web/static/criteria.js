// A quality criterion's form answers its own controls before it is saved.
//
// Changing the kind shows that kind's field. Ticking a section means "only these
// sections", and choosing the whole document clears the ticks, so the form can
// never say both. Delegated from the document because the editor is swapped
// whole on every edit.
(function () {
  "use strict";

  document.addEventListener("change", function (event) {
    var target = event.target;
    var form = target.form;
    if (!form) return;

    if (target.matches("[data-criterion-kind]")) {
      form.querySelectorAll("[data-for-kind]").forEach(function (field) {
        field.hidden = field.dataset.forKind.split(" ").indexOf(target.value) < 0;
      });
      return;
    }

    if (!target.closest("[data-criterion-scope]")) return;
    var only = form.querySelector('input[name="whole_document"][value="no"]');
    var whole = form.querySelector('input[name="whole_document"][value="yes"]');
    var ticks = form.querySelectorAll('input[name="scope"]');

    if (target.name === "scope") {
      var any = Array.prototype.some.call(ticks, function (t) { return t.checked; });
      if (any) only.checked = true;
      else whole.checked = true;
    } else if (target === whole && whole.checked) {
      ticks.forEach(function (t) { t.checked = false; });
    }
    // dirty.js read the form before these changes were made.
    form.dispatchEvent(new Event("change"));
  });
})();

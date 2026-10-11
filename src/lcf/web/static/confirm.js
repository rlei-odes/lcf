// "Are you sure?" in the application's own dialog rather than the browser's.
//
// Every `hx-confirm` goes through here, and so does a plain form carrying
// `data-confirm`. The confirming button repeats the label of the one that asked,
// so the dialog says "Delete" or "Start over" instead of a generic OK.
(function () {
  "use strict";

  function ask(question, action) {
    return new Promise(function (resolve) {
      var returnTo = document.activeElement;
      var modal = document.createElement("div");
      modal.className = "modal confirm-modal";
      modal.innerHTML =
        '<div class="modal-body" role="alertdialog" aria-modal="true" aria-labelledby="confirm-q">' +
        '<p id="confirm-q"></p>' +
        '<div class="actions">' +
        '<button type="button" class="danger" data-ok></button>' +
        '<button type="button" data-cancel>Cancel</button>' +
        "</div></div>";
      modal.querySelector("#confirm-q").textContent = question;
      modal.querySelector("[data-ok]").textContent = action || "Continue";

      function close(answer) {
        document.removeEventListener("keydown", keys, true);
        modal.remove();
        if (returnTo && returnTo.focus && returnTo.isConnected) returnTo.focus();
        resolve(answer);
      }

      function keys(event) {
        if (event.key === "Escape") {
          event.preventDefault();
          close(false);
        } else if (event.key === "Tab") {
          // Two buttons: keep focus between them.
          var buttons = modal.querySelectorAll("button");
          var i = Array.prototype.indexOf.call(buttons, document.activeElement);
          event.preventDefault();
          buttons[(i + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
        }
      }

      modal.addEventListener("click", function (event) {
        if (event.target === modal || event.target.closest("[data-cancel]")) close(false);
        else if (event.target.closest("[data-ok]")) close(true);
      });
      document.addEventListener("keydown", keys, true);
      document.body.appendChild(modal);
      modal.querySelector("[data-cancel]").focus();
    });
  }

  function actionOf(el) {
    var text = el && (el.getAttribute("data-confirm-action") || el.textContent || "").trim();
    return text && text.length <= 28 ? text : "";
  }

  document.addEventListener("htmx:confirm", function (event) {
    if (!event.detail.question) return;
    event.preventDefault();
    var elt = event.detail.elt;
    var button = elt.tagName === "FORM" ? elt.querySelector("[type=submit]") : elt;
    ask(event.detail.question, actionOf(button)).then(function (ok) {
      if (ok) event.detail.issueRequest(true);
    });
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute("data-confirm") || form.__confirmed) return;
    event.preventDefault();
    var button = event.submitter || form.querySelector("[type=submit]");
    ask(form.getAttribute("data-confirm"), actionOf(button)).then(function (ok) {
      if (!ok) return;
      form.__confirmed = true;
      form.requestSubmit(event.submitter || undefined);
    });
  });

  window.lcfConfirm = ask;
})();

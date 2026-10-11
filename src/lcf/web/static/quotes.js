// The magnifier on a filed passage: the first click loads where it sits in the
// paste, the second folds it away again without asking the server.
(function () {
  "use strict";

  document.addEventListener("htmx:beforeRequest", function (event) {
    var button = event.detail.elt;
    if (!button.hasAttribute || !button.hasAttribute("data-context-toggle")) return;
    var slot = button.parentElement.querySelector(".quote-context");
    if (slot && slot.childElementCount) {
      event.preventDefault();
      slot.innerHTML = "";
      button.classList.remove("on");
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var slot = event.detail.target;
    if (slot && slot.classList && slot.classList.contains("quote-context")) {
      var button = slot.parentElement.querySelector("[data-context-toggle]");
      if (button) button.classList.add("on");
    }
  });
})();

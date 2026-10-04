// Looking at an image full size, without leaving the card it belongs to.
//
// Delegated from the document because the tray is swapped by HTMX, and built on
// <dialog> because Escape, the backdrop and focus trapping are then the
// browser's job rather than ours. The links keep their href and target, so with
// this script absent — or before it loads — clicking one still opens the image.
(function () {
  let dialog = null;

  function ensure() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "lightbox";
    dialog.innerHTML =
      '<button class="lightbox-close" type="button" aria-label="Close">✕</button>' +
      '<img alt="">' +
      '<p class="lightbox-caption"></p>';
    dialog.addEventListener("click", function (event) {
      // The backdrop is the dialog itself: a click that did not land on the
      // picture or the caption is a click outside them.
      if (event.target === dialog || event.target.closest(".lightbox-close")) {
        dialog.close();
      }
    });
    document.body.appendChild(dialog);
    return dialog;
  }

  document.addEventListener("click", function (event) {
    const link = event.target.closest("a[data-lightbox]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    if (typeof HTMLDialogElement === "undefined") return; // let the href do its job
    event.preventDefault();

    const box = ensure();
    const caption = link.getAttribute("data-lightbox") || "";
    box.querySelector("img").src = link.getAttribute("href");
    box.querySelector("img").alt = caption;
    box.querySelector(".lightbox-caption").textContent = caption;
    box.showModal();
  });
})();

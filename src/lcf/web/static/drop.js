/* Dropping files on the evidence desk, and pasting a screenshot into it.
 *
 * A few lines beside the other page scripts, deliberately not the start of a
 * front end: the form it decorates already works without any of this. Choosing
 * files with the input submits, and the server renders the result — everything
 * here is about the gesture, not about the state.
 *
 * Delegated from `document` rather than bound to the element, because the panel
 * is replaced by an htmx swap after every parse and a listener bound to the old
 * node would stop working the first time somebody added a file.
 */
(function () {
  "use strict";

  const ZONE = "#dropzone";

  function zone(target) {
    return target && target.closest ? target.closest(ZONE) : null;
  }

  function current() {
    return document.querySelector(ZONE);
  }

  /* Submit through htmx so the response is swapped into the panel, exactly as a
   * click on the file input does. Assigning to `input.files` needs a DataTransfer
   * list — a FileList cannot be built any other way, and the input has to hold
   * the files for the multipart encoding to pick them up. */
  function send(files) {
    const form = current();
    if (!form || !files || !files.length) return;
    const input = form.querySelector('input[type="file"]');
    if (!input) return;

    const bag = new DataTransfer();
    for (const file of files) bag.items.add(file);
    input.files = bag.files;

    if (window.htmx) {
      window.htmx.trigger(form, "submit");
    } else {
      form.requestSubmit();
    }
  }

  /* dragover has to be cancelled on every event, not just the first: the browser
   * treats an uncancelled dragover as "not a drop target" and shows the no-entry
   * cursor even though the drop handler exists. */
  document.addEventListener("dragover", function (event) {
    const form = zone(event.target);
    if (!form) return;
    event.preventDefault();
    form.classList.add("dragging");
  });

  document.addEventListener("dragleave", function (event) {
    const form = zone(event.target);
    if (form) form.classList.remove("dragging");
  });

  document.addEventListener("drop", function (event) {
    const form = zone(event.target);
    if (!form) return;
    event.preventDefault();
    form.classList.remove("dragging");
    send(event.dataTransfer && event.dataTransfer.files);
  });

  /* Choosing files with the input submits on its own, so nobody has to find a
   * second button after the file dialog has closed. */
  document.addEventListener("change", function (event) {
    const form = zone(event.target);
    if (form && event.target.type === "file") send(event.target.files);
  });

  /* A screenshot of a measurement is pasted, not saved and uploaded — which is
   * how that evidence actually arrives. Ignored while the caret is in a field,
   * because pasting text into the paste box must keep working. */
  document.addEventListener("paste", function (event) {
    if (!current()) return;
    const inField = document.activeElement;
    if (inField && /^(INPUT|TEXTAREA|SELECT)$/.test(inField.tagName)) return;

    const items = (event.clipboardData && event.clipboardData.files) || [];
    const images = Array.from(items).filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    event.preventDefault();
    send(images);
  });
})();

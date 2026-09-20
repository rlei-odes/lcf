// Exporting, without losing the page underneath.
//
// A download cannot come back through an htmx swap — a .docx is not HTML — so the
// export buttons were ordinary form posts. That works, but the browser stays on a
// page the server has just made out of date: an export has been recorded, and the
// "previous exports" list still shows the state from before the click.
//
// So the form is posted with fetch instead. We hand the bytes to the browser
// ourselves, and only once the response is complete do we ask for the export card
// again — which is the one moment we know the new record exists. The server
// contract is unchanged: the same POST from curl still answers with the file.
(function () {
  "use strict";
  if (!window.fetch || !window.htmx) return;

  function filenameFrom(response, fallback) {
    var header = response.headers.get("content-disposition") || "";
    var match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
    return match ? decodeURIComponent(match[1]) : fallback;
  }

  function save(blob, filename) {
    var href = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () {
      URL.revokeObjectURL(href);
    }, 30000);
  }

  function busy(form, on) {
    form.querySelectorAll("button").forEach(function (button) {
      button.disabled = on;
    });
    form.classList.toggle("busy", on);
  }

  async function run(form, url) {
    busy(form, true);
    try {
      var response = await fetch(url, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-LCF-Fetch": "1" },
      });
      var type = response.headers.get("content-type") || "";
      if (type.indexOf("text/html") !== -1) {
        // Refused. The server answered with the card itself, notice and all.
        htmx.swap("#exports", await response.text(), { swapStyle: "outerHTML" });
        return;
      }
      save(await response.blob(), filenameFrom(response, "export"));
      htmx.ajax("GET", form.dataset.exportCard, {
        target: "#exports",
        swap: "outerHTML",
      });
    } catch (error) {
      busy(form, false);
      // Falling back to a plain submit is better than a button that did nothing.
      form.submit();
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-export-card]");
    if (!form) return;
    event.preventDefault();
    run(form, (event.submitter && event.submitter.formAction) || form.action);
  });
})();

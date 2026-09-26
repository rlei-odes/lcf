// Give a dropdown room to open downward, and let it flip only when there is
// genuinely none.
//
// Chrome's customizable select flips its list above the field far too eagerly:
// measured flipping with 365px of clear space beneath a list only 184px tall,
// which puts the jump in the middle of the screen and hides whatever you were
// reading. Nothing declarable moves that threshold — max-height, min-height,
// position-area, position-try-order and an explicit constrained @position-try
// were all tried, and the trigger does not shift.
//
// So the fallback is off by default in CSS, and the decision is made here, per
// opening, in two steps:
//
//   1. If the field is too near the bottom, scroll the page down so it is not.
//      This handles every case in the body of a long page.
//   2. If the page cannot scroll any further — the field really is at the end
//      of the document — turn the flip back on for that one select, so the list
//      goes upward rather than being squashed into thirty pixels.
//
// The net effect is what a dropdown should do: open downward, at full size,
// and flip only at the very bottom.
(function () {
  "use strict";

  // Room a comfortable list wants, and the least it can live with.
  var WANTED = 260;
  var MINIMUM = 150;
  // Keeps a field from scrolling up underneath the sticky app bar.
  var APP_BAR = 72;

  function prepare(select) {
    select.classList.remove("flip-up");

    var rect = select.getBoundingClientRect();
    var below = window.innerHeight - rect.bottom;
    if (below >= WANTED) return;

    var scrollable =
      document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
    var headroom = Math.max(0, rect.top - APP_BAR);
    var by = Math.min(WANTED - below, Math.max(0, scrollable), headroom);

    if (by > 0) {
      window.scrollBy({ top: by, behavior: "instant" });
      below += by;
    }
    if (below < MINIMUM) select.classList.add("flip-up");
  }

  // pointerdown, not click: the browser opens the list on mouse *down*, so by
  // the time a click event arrives the position has already been decided.
  document.addEventListener(
    "pointerdown",
    function (e) {
      var select = e.target && e.target.closest && e.target.closest("select");
      if (select) prepare(select);
    },
    true
  );

  // Space, Enter and the arrow keys all drop the list open.
  document.addEventListener(
    "keydown",
    function (e) {
      if (!e.target || e.target.tagName !== "SELECT") return;
      if ([" ", "Enter", "ArrowDown", "ArrowUp"].indexOf(e.key) === -1) return;
      prepare(e.target);
    },
    true
  );
})();

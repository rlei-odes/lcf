// Which section of a long page you are actually looking at.
//
// A rail of anchors that always highlights the first one is worse than no
// highlight: it claims to know where you are and is wrong every time but once.
// This watches the targets and moves the highlight to whichever is nearest the
// top of the viewport, so the claim becomes true.
//
// The scroll offset itself is CSS — `scroll-margin-top` on the targets, so an
// anchor jump does not land underneath the sticky app bar.
(function () {
  "use strict";

  function spy(nav) {
    var links = Array.prototype.slice.call(nav.querySelectorAll('a[href^="#"]'));
    var targets = links
      .map(function (a) {
        var el = document.getElementById(a.getAttribute("href").slice(1));
        return el ? { link: a, el: el } : null;
      })
      .filter(Boolean);
    if (!targets.length) return;

    function mark(active) {
      targets.forEach(function (t) {
        t.link.classList.toggle("active", t.link === active);
      });
    }

    function nearest() {
      // The one whose top is closest to just below the app bar, preferring
      // anything already scrolled past so the last section still lights up at
      // the bottom of the page.
      var line = 80;
      var best = targets[0];
      targets.forEach(function (t) {
        var top = t.el.getBoundingClientRect().top;
        if (top - line <= 0) best = t;
      });
      // At the very bottom nothing further can scroll, so the final section wins.
      if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
        best = targets[targets.length - 1];
      }
      return best.link;
    }

    var queued = false;
    function update() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        mark(nearest());
      });
    }

    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update, { passive: true });
    links.forEach(function (a) {
      a.addEventListener("click", function () {
        // The scroll has not happened yet; let it, then recompute.
        window.setTimeout(update, 50);
      });
    });
    update();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("nav[data-spy]").forEach(spy);
  });
})();

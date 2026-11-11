// While any .modal is open the page behind it must not scroll. iOS Safari ignores overflow:hidden on html, so the page
// is frozen in place instead: body goes position:fixed at the current offset, and the scroll position is put back on close.
(function () {
  var locked = false, y = 0;

  function anyOpen() { return !!document.querySelector(".modal:not([hidden])"); }

  function sync() {
    var open = anyOpen();
    if (open === locked) return;
    var b = document.body, s = b.style;
    if (open) {
      y = window.pageYOffset || document.documentElement.scrollTop || 0;
      s.position = "fixed"; s.top = -y + "px"; s.left = "0"; s.right = "0"; s.width = "100%";
    } else {
      s.position = s.top = s.left = s.right = s.width = "";
      window.scrollTo(0, y);
    }
    locked = open;
  }

  // Modals are shown and hidden by toggling [hidden] (and the player modal is created on demand), so watch for both.
  new MutationObserver(sync).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden"] });
  sync();
})();

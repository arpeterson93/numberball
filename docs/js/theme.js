/* Light/dark theme.
 *
 * Loaded from <head>, before the stylesheet, and applies data-theme
 * synchronously at parse time - the rest of the JS loads at the end of <body>,
 * which would be late enough to show a light flash before every dark paint.
 *
 * Stored value is the *preference*, not the resolved theme: "light"/"dark"
 * means the visitor chose explicitly, and absent means follow the OS. Keeping
 * those distinct is what lets an unset visitor track their system switching to
 * night mode while a visitor who picked one stays on it.
 *
 * localStorage only, same as favorites' km_* keys - deliberately not synced to
 * the Apps Script backend; see dark-mode-plan.md.
 */
(function () {
  "use strict";

  var KEY = "km_theme";

  function systemPrefersDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function stored() {
    try {
      var v = window.localStorage.getItem(KEY);
      return (v === "light" || v === "dark") ? v : null;
    } catch (e) {
      return null;   // private browsing / storage disabled
    }
  }

  function resolve() {
    return stored() || (systemPrefersDark() ? "dark" : "light");
  }

  function syncButton(theme) {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    syncButton(theme);
  }

  // theme: "light" | "dark" | null (null clears the override, back to the OS)
  function set(theme) {
    try {
      if (theme) window.localStorage.setItem(KEY, theme);
      else window.localStorage.removeItem(KEY);
    } catch (e) { /* in-memory for this page load only */ }
    apply(theme || (systemPrefersDark() ? "dark" : "light"));
  }

  function toggle() {
    set(resolve() === "dark" ? "light" : "dark");
  }

  apply(resolve());

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", toggle);
    syncButton(resolve());
  });

  // Follow the OS live, but only while nothing explicit is stored.
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function () { if (!stored()) apply(resolve()); };
    if (mq.addEventListener) mq.addEventListener("change", onSystemChange);
    else if (mq.addListener) mq.addListener(onSystemChange);   // Safari < 14
  }

  window.KMTheme = { toggle: toggle, current: resolve, set: set };
})();

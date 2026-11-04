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
    applyColors();
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

  // ---- Accent / secondary (navy) color overrides, set from Settings ----
  // Picks are stored as plain 6-digit hex (no '#'). They are never used raw:
  // a pale team color as a tab underline, or a dark one on the dark theme,
  // would vanish, so fit() nudges the stored color toward black (light theme)
  // or white (dark theme) only as far as it takes to reach 3:1 against the
  // page background, and onColor() picks black or white text for the result.
  // Set as inline custom properties on <html>, which beat both the :root and
  // :root[data-theme="dark"] blocks; clearing them falls back to the
  // stylesheet. Re-run on every theme switch since the nudge differs per theme.
  var ACCENT_KEY = "km_accent", NAVY_KEY = "km_navy", TEAM_KEY = "km_color_team";
  var DEFAULTS = { accent: "c8102e", navy: "0f2238" };
  var PAGE_BG = { light: [238, 241, 244], dark: [18, 22, 28] };

  function normHex(v) {
    var m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(v || "").trim());
    if (!m) return null;
    var h = m[1].toLowerCase();
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    return h;
  }
  function toRgb(h) { return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]; }
  function toHex(c) {
    return c.map(function (x) { var s = Math.round(x).toString(16); return s.length < 2 ? "0" + s : s; }).join("");
  }
  function lum(c) {
    var l = c.map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
  }
  function contrast(a, b) {
    var x = lum(a), y = lum(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }
  function mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  function fit(c, theme) {
    var bg = PAGE_BG[theme], toward = theme === "dark" ? [255, 255, 255] : [0, 0, 0];
    for (var i = 0; i < 25 && contrast(c, bg) < 3; i++) c = mix(c, toward, 0.06);
    return c;
  }
  function onColor(c) { return contrast(c, [255, 255, 255]) >= contrast(c, [0, 0, 0]) ? "#fff" : "#000"; }

  function read(key) {
    try { return normHex(window.localStorage.getItem(key)); } catch (e) { return null; }
  }
  function write(key, val) {
    try {
      if (val) window.localStorage.setItem(key, val);
      else window.localStorage.removeItem(key);
    } catch (e) { /* in-memory for this page load only */ }
  }

  var overrides = { accent: read(ACCENT_KEY), navy: read(NAVY_KEY) };

  function applyColors() {
    var st = document.documentElement.style;
    var theme = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    var c;
    if (overrides.accent) {
      c = fit(toRgb(overrides.accent), theme);
      st.setProperty("--accent", "#" + toHex(c));
      st.setProperty("--on-accent", onColor(c));
    } else {
      st.removeProperty("--accent");
      st.removeProperty("--on-accent");
    }
    if (overrides.navy) {
      c = fit(toRgb(overrides.navy), theme);
      st.setProperty("--navy", "#" + toHex(c));
      st.setProperty("--navy-2", "#" + toHex(theme === "dark" ? mix(c, [0, 0, 0], 0.2) : mix(c, [255, 255, 255], 0.12)));
      st.setProperty("--on-navy", onColor(c));
    } else {
      st.removeProperty("--navy");
      st.removeProperty("--navy-2");
      st.removeProperty("--on-navy");
    }
  }

  // which: "accent" | "navy"; hex: anything normHex accepts, or null for the default
  function setColor(which, hex) {
    var h = hex ? normHex(hex) : null;
    if (hex && !h) return false;
    overrides[which] = h;
    write(which === "accent" ? ACCENT_KEY : NAVY_KEY, h);
    applyColors();
    return true;
  }

  apply(resolve());

  // ---- Settings panel controls ----
  var teams = {};

  function teamPick() {
    var sel = document.getElementById("color-team");
    return sel && sel.value && teams[sel.value] ? teams[sel.value] : null;
  }

  function renderColorControls() {
    var team = teamPick();
    ["accent", "navy"].forEach(function (which) {
      var box = document.getElementById("color-" + which);
      if (!box) return;
      var cur = overrides[which];
      // The default circle only shows with no team picked; with a team, the
      // dropdown's own "Default" entry (or Reset) is the way back.
      var swatches = team ? [] : [{ hex: null, title: "Default" }];
      if (team) {
        var p = normHex(team.primary_hex), s = normHex(team.secondary_hex);
        if (p) swatches.push({ hex: p, title: "Team primary #" + p });
        if (s && s !== p) swatches.push({ hex: s, title: "Team secondary #" + s });
      }
      var wrap = box.querySelector(".color-swatches");
      wrap.innerHTML = "";
      swatches.forEach(function (sw) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "color-swatch" + (sw.hex === cur ? " active" : "");
        b.style.background = "#" + (sw.hex || DEFAULTS[which]);
        b.title = sw.title;
        b.setAttribute("aria-label", sw.title);
        b.addEventListener("click", function () { setColor(which, sw.hex); renderColorControls(); });
        wrap.appendChild(b);
      });
      var txt = box.querySelector("input[type=text]");
      var pick = box.querySelector("input[type=color]");
      if (document.activeElement !== txt) txt.value = cur ? "#" + cur : "";
      txt.classList.remove("invalid");
      pick.value = "#" + (cur || DEFAULTS[which]);
    });
  }

  function populateTeams(t) {
    teams = t || {};
    var sel = document.getElementById("color-team");
    if (!sel) return;
    var keep = sel.value;
    if (!keep) { try { keep = window.localStorage.getItem(TEAM_KEY) || ""; } catch (e) { keep = ""; } }
    sel.innerHTML = '<option value="">Default</option>';
    Object.keys(teams).sort(function (a, b) {
      return String(teams[a].name || a).localeCompare(String(teams[b].name || b));
    }).forEach(function (abbr) {
      var o = document.createElement("option");
      o.value = abbr;
      o.textContent = teams[abbr].name || abbr;
      sel.appendChild(o);
    });
    sel.value = teams[keep] ? keep : "";
    renderColorControls();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", toggle);
      syncButton(resolve());
    }
    var sel = document.getElementById("color-team");
    if (!sel) return;
    sel.addEventListener("change", function () {
      try {
        if (sel.value) window.localStorage.setItem(TEAM_KEY, sel.value);
        else window.localStorage.removeItem(TEAM_KEY);
      } catch (e) { /* per-load only */ }
      var t = teamPick();
      // Picking a team applies its primary as the accent and its secondary as
      // the navy; the swatches and hex boxes then fine-tune either one.
      setColor("accent", t ? t.primary_hex : null);
      setColor("navy", t ? t.secondary_hex : null);
      renderColorControls();
    });
    ["accent", "navy"].forEach(function (which) {
      var box = document.getElementById("color-" + which);
      if (!box) return;
      var txt = box.querySelector("input[type=text]");
      var pick = box.querySelector("input[type=color]");
      txt.addEventListener("input", function () {
        var v = txt.value.trim();
        if (!v) { txt.classList.remove("invalid"); return; }
        if (setColor(which, v)) { txt.classList.remove("invalid"); renderColorControls(); }
        else txt.classList.add("invalid");
      });
      txt.addEventListener("change", function () {
        if (!txt.value.trim()) setColor(which, null);
        renderColorControls();
      });
      pick.addEventListener("input", function () { setColor(which, pick.value); renderColorControls(); });
    });
    var reset = document.getElementById("color-reset");
    if (reset) reset.addEventListener("click", function () {
      setColor("accent", null);
      setColor("navy", null);
      sel.value = "";
      try { window.localStorage.removeItem(TEAM_KEY); } catch (e) { /* per-load only */ }
      renderColorControls();
    });
    populateTeams(teams);
  });

  // Follow the OS live, but only while nothing explicit is stored.
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function () { if (!stored()) apply(resolve()); };
    if (mq.addEventListener) mq.addEventListener("change", onSystemChange);
    else if (mq.addListener) mq.addListener(onSystemChange);   // Safari < 14
  }

  window.KMTheme = { toggle: toggle, current: resolve, set: set, setColor: setColor, setTeams: populateTeams };
})();

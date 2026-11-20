/* Push notifications (Settings > Notifications).
 *
 * No accounts: the browser's own push subscription is the identity. It is stored in the Apps Script sheet's `push` tab
 * next to the Catch Me Up name slug (so the sender can look up that name's favorites), and the preferences ride along:
 *   { teams: { ABC: { m: 1, e: 0 } }, favs: 1, tags: ["steal", ...] }   m = every play / key moment for the team, e = game ends,
 *   fmt = own layout for plays (absent = Classic), fmtk = own layout for key moments (absent = Spotlight); see push_format.js, favs = favorites plays (0 off, 1 key moments, 2 all plays), feats = new feat alerts (0 off, 1 my teams and favorites, 2 everyone), tags = own key moment definition (optional; absent = the site default)
 * Needs KM_CONFIG.VAPID_PUBLIC_KEY and the Apps Script FAVORITES_ENDPOINT; with either missing the Settings row stays hidden.
 * iOS only allows web push from an installed (Home Screen) PWA, so there it explains that instead of asking for permission.
 */
(function () {
  "use strict";
  var cfg = window.KM_CONFIG || {};
  var PREFS_KEY = "km_push_prefs";
  var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  var state = { teams: null, prefs: { teams: {}, favs: 0 }, sub: null, saveTimer: 0, modal: null };

  // A tapped notification while the app is already open: the service worker (sw.js) sends the link, we go there.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", function (e) {
      var d = e.data;
      if (!d || d.type !== "mln-open" || typeof d.url !== "string") return;
      report("OPEN: message", d.url);
      go(d.url, true);
    });
    // ...and the same link is parked in the cache by the service worker: pick it up on load and whenever the app comes back
    document.addEventListener("visibilitychange", function () { if (!document.hidden) checkPending(); });
    window.addEventListener("pageshow", checkPending);
    window.addEventListener("focus", checkPending);
    checkPending();
    // a cold start from a tap can load the page a moment before the service worker has parked the link, so look again a few times
    [1000, 2500, 5000, 10000].forEach(function (ms) { window.setTimeout(checkPending, ms); });
    // every launch of the app, tapped alert or not (at most one per 3 minutes), so a tap whose click never reached the service worker still
    // leaves a row: standalone = opened from the home screen, ctrl = a service worker is in charge, link = what the address carried
    // Diagnostics for the tap-from-lock-screen investigation: nav = how the page loaded (navigate/reload/back_forward), hid = page was hidden at
    // load, ref = referrer host, hl = history length, tray = notifications still in the tray as count/age in seconds of the newest.
    function bootInfo(extra) {
      var nav = "?";
      try { nav = (performance.getEntriesByType("navigation")[0] || {}).type || "?"; } catch (e) { /* skip */ }
      var ref = "none";
      try { ref = document.referrer ? new URL(document.referrer).host : "none"; } catch (e) { /* skip */ }
      var base = "standalone=" + ((window.navigator.standalone || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)) ? 1 : 0) +
        " ctrl=" + (navigator.serviceWorker.controller ? 1 : 0) + " link=" + (location.search.slice(0, 40) || "none") +
        " nav=" + nav + " hid=" + (document.hidden ? 1 : 0) + " ref=" + ref + " hl=" + history.length + (extra || "");
      return navigator.serviceWorker.ready.then(function (reg) { return reg.getNotifications(); }).then(function (list) {
        var newest = 0;
        list.forEach(function (n) { if (n.timestamp > newest) newest = n.timestamp; });
        return base + " tray=" + list.length + (newest ? "/" + Math.round((Date.now() - newest) / 1000) + "s" : "");
      }).catch(function () { return base + " tray=?"; });
    }
    try {
      var lastBoot = Number(window.localStorage.getItem("mln-boot-report") || 0);
      if (Date.now() - lastBoot > 60000) {
        window.localStorage.setItem("mln-boot-report", String(Date.now()));
        bootInfo().then(function (s) { report("OPEN: launch", s); });
      }
    } catch (e) { /* storage blocked: skip */ }
    // the app already running (so no page load, no launch row) and brought to the front after at least 20 s away: a tap that only resumed it
    var hiddenAt = 0, lastResume = 0;
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { hiddenAt = Date.now(); return; }
      if (!hiddenAt || Date.now() - hiddenAt < 20000 || Date.now() - lastResume < 60000) return;
      lastResume = Date.now();
      bootInfo(" away=" + Math.round((Date.now() - hiddenAt) / 1000) + "s").then(function (s) { report("OPEN: resume", s); });
    });
    // arriving on a game link (a tapped alert or a shared link): say whether the replay actually opened
    if (/[?&]game=/.test(location.search)) {
      window.setTimeout(function () {
        var m = document.getElementById("replay-modal");
        report("OPEN: landed", (m && !m.hidden ? "replay open " : "replay NOT open ") + location.search.slice(0, 60));
      }, 6000);
    }
  }

  // Tiny anonymous trace lines into the push_receipts tab (same device id as the sender log), only from a subscribed browser.
  var deviceIdP = null;
  function deviceId() {
    if (!deviceIdP) {
      deviceIdP = navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
        if (!sub) return "";
        return crypto.subtle.digest("SHA-1", new TextEncoder().encode(sub.endpoint)).then(function (buf) {
          return Array.prototype.map.call(new Uint8Array(buf), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("").slice(0, 6);
        });
      }).catch(function () { return ""; });
    }
    return deviceIdP;
  }
  function report(label, detail) {
    if (!cfg.FAVORITES_ENDPOINT || !window.crypto || !crypto.subtle) return;
    deviceId().then(function (id) {
      if (!id) return;
      return fetch(cfg.FAVORITES_ENDPOINT, { method: "POST", mode: "no-cors", headers: { "Content-Type": "text/plain;charset=utf-8" },
        body: JSON.stringify({ action: "push_received", id: id, title: label, body: String(detail || "").slice(0, 150), shown: true }) });
    }).catch(function () {});
  }

  var going = "";
  // Navigate to a notification's link (same origin only). `force`: a tap while on that very link reloads it so the replay reruns.
  function go(url, force) {
    try { if (new URL(url).origin !== location.origin) return; } catch (err) { return; }
    forgetPending();
    if (going === url) return;
    if (url === location.href) { if (force) { going = url; location.reload(); } return; }
    going = url;
    location.href = url;
  }

  function pendingKey() { return navigator.serviceWorker.ready.then(function (reg) { return reg.scope + "__pending"; }); }
  function forgetPending() {
    if (!window.caches) return;
    pendingKey().then(function (k) { return caches.open("mln-push").then(function (c) { return c.delete(k); }); }).catch(function () {});
  }
  function checkPending() {
    if (!window.caches) return;
    pendingKey().then(function (k) {
      return caches.open("mln-push").then(function (c) {
        return c.match(k).then(function (r) {
          if (!r) return;
          return r.json().then(function (p) {
            c.delete(k);
            if (p && p.url && Date.now() - p.t < 120000) { report("OPEN: cache", p.url); go(p.url, false); }   // an old, unclaimed tap is dropped
          });
        });
      });
    }).catch(function () {});
  }

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function endpoint() { return cfg.FAVORITES_ENDPOINT || ""; }

  function post(body, keepalive) {
    // text/plain keeps this a "simple" request, so the browser skips the CORS preflight Apps Script cannot answer.
    return fetch(endpoint(), { method: "POST", headers: { "Content-Type": "text/plain;charset=utf-8" }, body: JSON.stringify(body), keepalive: !!keepalive })
      .then(function (r) { return r.json(); });
  }
  function b64ToBytes(b64) {
    var pad = "=".repeat((4 - (b64.length % 4)) % 4);
    var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function loadPrefs() {
    try { var p = JSON.parse(localStorage.getItem(PREFS_KEY) || "null"); if (p && p.teams) state.prefs = p; } catch (e) { /* ignore */ }
  }
  function storePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs)); } catch (e) { /* ignore */ } }

  function syncButton() {
    var btn = $("push-btn");
    if (btn) btn.textContent = state.sub ? "Preferences" : "Set up alerts";
  }

  function register() {
    return navigator.serviceWorker.register("sw.js").then(function () { return navigator.serviceWorker.ready; });
  }
  function currentSub() {
    return register().then(function (reg) { return reg.pushManager.getSubscription(); });
  }

  // ── actions ──────────────────────────────────────────────────────────────────
  function subscribe() {
    var fav = window.KMFavorites;
    if (!fav || !fav.hasName()) return Promise.reject(new Error("name"));
    return Notification.requestPermission().then(function (perm) {
      if (perm !== "granted") throw new Error("denied");
      return register();
    }).then(function (reg) {
      return reg.pushManager.getSubscription().then(function (existing) {
        return existing || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(cfg.VAPID_PUBLIC_KEY) });
      });
    }).then(function (sub) {
      state.sub = sub;
      return post({ action: "push_subscribe", key: fav.key(), subscription: sub.toJSON(), prefs: state.prefs });
    }).then(function (res) {
      if (res && res.error) throw new Error(res.error);
    });
  }
  function unsubscribe() {
    var sub = state.sub;
    state.sub = null;
    if (!sub) return Promise.resolve();
    var ep = sub.endpoint;
    return sub.unsubscribe().then(function () { return post({ action: "push_unsubscribe", endpoint: ep }); });
  }
  // The sheet's copy wins when the app next opens, so a save that never arrives looks like settings resetting. Two ways one went missing:
  // the app was closed inside the 600 ms wait (flushSave sends it the moment the page is hidden), or the script answered with an error
  // (not subscribed, busy) that used to be ignored.
  function flushSave() {
    if (!state.saveTimer) return;
    clearTimeout(state.saveTimer);
    state.saveTimer = 0;
    if (!state.sub) return;
    post({ action: "push_prefs", endpoint: state.sub.endpoint, prefs: state.prefs }, true).then(function (r) {
      if (r && r.error) setStatus(r.error === "not subscribed" ? "Could not save: this device's alert subscription is missing. Turn alerts off and on again." : "Could not save: " + r.error);
      else setStatus("");
    }).catch(function () { setStatus("Could not save - check your connection."); });
  }
  function queueSave() {
    storePrefs();
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(flushSave, 600);
  }
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flushSave(); });
  window.addEventListener("pagehide", flushSave);

  // ── UI ───────────────────────────────────────────────────────────────────────
  function setStatus(t) { var el = $("push-status"); if (el) el.textContent = t || ""; }

  function ensureModal() {
    if (state.modal) return state.modal;
    var m = document.createElement("div");
    m.className = "modal";
    m.id = "push-modal";
    m.hidden = true;
    m.innerHTML =
      '<div class="modal-card"><div class="modal-head"><strong>Notifications</strong>' +
      '<button type="button" class="link-btn" id="push-close">Close</button></div>' +
      '<div class="modal-body" id="push-body"></div></div>';
    document.body.appendChild(m);
    m.addEventListener("click", function (e) { if (e.target === m) m.hidden = true; });
    $("push-close").addEventListener("click", function () { m.hidden = true; });
    m.addEventListener("click", onClick);
    m.addEventListener("change", onChange);
    m.addEventListener("pointerdown", dragStart);
    state.modal = m;
    return m;
  }

  var LEVELS = [["0", "Off"], ["1", "Key moments"], ["2", "All plays"]];
  function levelSelect(attrs, value, label) {
    return '<select class="push-level" ' + attrs + ' aria-label="' + esc(label) + '">' +
      (value === "" ? '<option value="" selected disabled>Set all...</option>' : "") +
      LEVELS.map(function (l) { return '<option value="' + l[0] + '"' + (l[0] === value ? " selected" : "") + ">" + l[1] + "</option>"; }).join("") + "</select>";
  }
  var FEAT_LEVELS = [["0", "Off"], ["1", "My teams and favorites"], ["2", "Everyone"]];
  function featSelect(value) {
    return '<select class="push-level" data-pfeats aria-label="Feat alerts">' +
      FEAT_LEVELS.map(function (l) { return '<option value="' + l[0] + '"' + (l[0] === value ? " selected" : "") + ">" + l[1] + "</option>"; }).join("") + "</select>";
  }
  function teamRows() {
    var t = state.teams || {};
    return Object.keys(t).sort().map(function (abbr) {
      var p = state.prefs.teams[abbr] || {}, name = t[abbr].name || abbr;
      var logo = t[abbr].logo_url ? '<img class="push-logo" src="' + esc(t[abbr].logo_url) + '" alt="' + esc(abbr) + '" title="' + esc(name) + '" loading="lazy">' : "<strong>" + esc(abbr) + "</strong>";
      return '<tr><td class="push-team"><div class="push-team-in">' + logo + '<span class="push-name">' + esc(t[abbr].nickname || name) + "</span></div></td>" +
        "<td>" + levelSelect('data-pteam="' + abbr + '" data-pkind="m"', String(p.m || 0), name + " plays") + "</td>" +
        '<td><input type="checkbox" data-pteam="' + abbr + '" data-pkind="e"' + (p.e ? " checked" : "") + ' aria-label="' + esc(name) + ' game ends"></td></tr>';
    }).join("");
  }
  function allValue(kind) {   // the shared setting if every team has the same one, else "" (mixed)
    var abbrs = Object.keys(state.teams || {}), first = null, i, v;
    for (i = 0; i < abbrs.length; i++) {
      v = String((state.prefs.teams[abbrs[i]] || {})[kind] || 0);
      if (first === null) first = v; else if (v !== first) return "";
    }
    return first === null ? "0" : first;
  }

  // Own definition of a key moment: prefs.tags, a list of play tag slugs. Absent = the site's default, which is every tag except the
  // filter-only ones (FILTER_ONLY_TAGS in key_moments_build.py). Saved only when the choice differs from that default.
  var DEFAULT_OFF = { risp: 1 };
  function tagSlugs() { return Object.keys(state.tagLabels || {}); }
  function tagOn(slug) { return Array.isArray(state.prefs.tags) ? state.prefs.tags.indexOf(slug) !== -1 : !DEFAULT_OFF[slug]; }
  function tagRows() {
    var labels = state.tagLabels || {};
    return tagSlugs().map(function (s) {
      return '<label class="push-tag"><input type="checkbox" data-ptag="' + esc(s) + '"' + (tagOn(s) ? " checked" : "") + "> " + esc(labels[s]) + "</label>";
    }).join("");
  }
  function setTags() {
    var picked = tagSlugs().filter(function (s) { return state.modal.querySelector('input[data-ptag="' + s + '"]').checked; });
    var isDefault = tagSlugs().every(function (s) { return (picked.indexOf(s) !== -1) === !DEFAULT_OFF[s]; });
    if (isDefault) delete state.prefs.tags; else state.prefs.tags = picked;
  }

  // ── own notification layout (prefs.fmt, rendered by tools/push_format.py for the real alerts, docs/js/push_format.js for the preview) ──
  var F = window.MLNPushFmt, compLabel = {}, MAX_LINES = 5;
  var PAL_GROUPS = [["Game", ["score", "inning", "bases", "bases_words", "outs"]], ["Matchup", ["batter", "batter_last", "pitcher", "pitcher_last"]],
    ["Result", ["result", "result_full", "scoring", "diff", "tags"]], ["Analytics", ["lev", "wpa", "wp", "wp_change"]]];
  if (F) F.COMPONENTS.forEach(function (c) { compLabel[c[0]] = c[1]; });
  // two layouts are designed here: "fmt" (regular plays, Classic until changed) and "fmtk" (key moments, Spotlight until changed); feat alerts are fixed
  function fmtSlot() { return state.fmtKey === "fmtk" ? "fmtk" : "fmt"; }
  function curFmt() {
    var raw = fmtSlot() === "fmtk" ? (state.prefs.fmtk || F.SPOTLIGHT) : state.prefs.fmt;
    return F.normalize(raw ? JSON.parse(JSON.stringify(raw)) : F.CLASSIC);
  }
  // The layout a user built that matches no preset is kept as a copy (prefs.fmtc for plays, prefs.fmtkc for key moments) so choosing a
  // preset does not lose it: the Custom chip then restores it. The sender ignores the copies.
  function copyKey() { return fmtSlot() + "c"; }
  function isPreset(f) { var s = JSON.stringify(F.normalize(f)); return F.PRESETS.some(function (p) { return JSON.stringify(F.normalize(p[1])) === s; }); }
  function stashCustom(f) { if (!isPreset(f)) state.prefs[copyKey()] = F.normalize(JSON.parse(JSON.stringify(f))); }
  function editFmt(fn) {
    var f = curFmt();
    stashCustom(f);   // about to change it: if it is a custom layout, the copy keeps it
    fn(f);
    f = F.normalize(f);
    stashCustom(f);   // and if the result is a custom layout, it is the new copy
    if (fmtSlot() === "fmtk") {
      if (JSON.stringify(f) === JSON.stringify(F.normalize(F.SPOTLIGHT))) delete state.prefs.fmtk; else state.prefs.fmtk = f;   // the standard layout is just "no custom layout"
    } else if (JSON.stringify(f) === JSON.stringify(F.normalize(F.CLASSIC))) delete state.prefs.fmt; else state.prefs.fmt = f;   // the standard layout is just "no custom layout"
    queueSave();
    renderComposer();
  }
  function chipHtml(id, from) {
    return '<span class="push-chip" data-comp="' + esc(id) + '" data-from="' + from + '"><span>' + esc(compLabel[id] || id) + "</span>" +
      (from === "pal" ? "" : '<button type="button" class="push-chip-x" data-rm="' + esc(id) + '" aria-label="Remove ' + esc(compLabel[id] || id) + '">&times;</button>') + "</span>";
  }
  function previewHtml(f) {
    var s = F.SAMPLES[state.sample || 0], r = F.render(s.m, f, state.resultLabels, state.resultShort) || { title: "(nothing to show)", body: [] };
    var t = (state.teams || {})[s.m.off_team_abbr] || {};
    return '<div class="push-card">' + (t.logo_url ? '<img class="push-card-logo" src="' + esc(t.logo_url) + '" alt="">' : "") +
      '<div class="push-card-text"><div class="push-card-title">' + esc(r.title) + "</div>" +
      r.body.map(function (l) { return '<div class="push-card-line">' + esc(l) + "</div>"; }).join("") + "</div></div>";
  }
  function composerHtml() {
    if (!F) return "";
    var f = curFmt(), used = {}, cur = JSON.stringify(f);
    f.l.forEach(function (l) { l.forEach(function (id) { used[id] = 1; }); });
    var matched = false;
    var presets = F.PRESETS.map(function (p, i) {
      var on = JSON.stringify(F.normalize(p[1])) === cur;
      if (on) matched = true;
      return '<button type="button" class="chip' + (on ? " active" : "") + '" data-pfmtpreset="' + i + '">' + esc(p[0]) + "</button>";
    }).join("") + (!matched ? '<span class="chip active" aria-current="true">Custom</span>'   // a layout that is none of the presets
      : state.prefs[copyKey()] ? '<button type="button" class="chip" data-pfmtcustom="1">Custom</button>' : "");   // not in use, but the saved copy can be brought back
    var samples = F.SAMPLES.map(function (s, i) { return '<option value="' + i + '"' + (i === (state.sample || 0) ? " selected" : "") + ">" + esc(s.name) + "</option>"; }).join("");
    var lines = f.l.map(function (l, i) {
      return '<div class="push-line-label">' + (i === 0 ? "Title" : "Line " + (i + 1)) +
        (i > 0 && !l.length ? ' <button type="button" class="link-btn" data-rmline="' + i + '">remove line</button>' : "") + "</div>" +
        '<div class="push-line" data-line="' + i + '">' + l.map(function (id) { return chipHtml(id, i); }).join("") +
        (l.length ? "" : '<span class="push-drop-hint">Drop components here</span>') + "</div>";
    }).join("");
    // "Not used", in labelled segments (a segment with nothing left in it is hidden); a component missing from every segment goes in the last
    var listed = {};
    PAL_GROUPS.forEach(function (g) { g[1].forEach(function (id) { listed[id] = 1; }); });
    var pal = PAL_GROUPS.map(function (g, gi) {
      var ids = g[1].concat(gi === PAL_GROUPS.length - 1 ? F.COMPONENTS.map(function (c) { return c[0]; }).filter(function (id) { return !listed[id]; }) : []).filter(function (id) { return !used[id]; });
      return ids.length ? '<div class="push-pal-group"><span class="push-pal-label">' + g[0] + "</span>" + ids.map(function (id) { return chipHtml(id, "pal"); }).join("") + "</div>" : "";
    }).join("");
    function sel(opt, val, items) {
      return '<select class="push-level" data-pfmtopt="' + opt + '">' + items.map(function (it) { return '<option value="' + it[0] + '"' + (String(it[0]) === String(val) ? " selected" : "") + ">" + it[1] + "</option>"; }).join("") + "</select>";
    }
    var kinds = '<div class="push-preset-row">' + [["fmt", "Plays"], ["fmtk", "Key Moments"]].map(function (k) {
      return '<button type="button" class="chip' + (fmtSlot() === k[0] ? " active" : "") + '" data-pfmtkind="' + k[0] + '">' + k[1] + "</button>";
    }).join("") + "</div>" +
      '<p class="modal-note">' + (fmtSlot() === "fmtk" ? "Used for plays that count as key moments." : "Used for every other play.") + "</p>";
    return kinds + '<div class="push-preset-row">' + presets + "</div>" +
      '<div class="push-preview-row"><span>Preview</span><select class="push-level" data-psample>' + samples + "</select></div>" +
      '<div id="push-preview">' + previewHtml(f) + "</div>" +
      '<div class="push-line-label">Drag components between lines</div>' + lines +
      '<div class="push-line-label">Not used</div><div class="push-line push-palette" data-line="pal">' + pal + (pal ? "" : '<span class="push-drop-hint">Everything is in use</span>') + "</div>" +
      '<div class="push-line-label">Options</div>' +
      '<div class="push-opts">' + [["Win Prob Side", "pv", [["hit", "Batting team"], ["feat", "Featured player's team"]]]
      ].map(function (o) { return '<div class="settings-row"><span>' + o[0] + "</span>" + sel(o[1], f[o[1]], o[2]) + "</div>"; }).join("") + "</div>" +
      '<div class="push-fmt-actions">' + (f.l.length < MAX_LINES ? '<button type="button" class="chip" data-pfmt="addline">Add line</button>' : "") +
      '<button type="button" class="chip" data-pfmt="reset">' + "Reset to standard" + "</button></div>";
  }
  function renderComposer() { var el = $("push-composer"); if (el) el.innerHTML = composerHtml(); }

  // pointer-based drag (mouse and touch): a chip dragged onto a line goes there at the pointer's position, onto "Not used" it is removed
  var drag = null;
  function dragStart(e) {
    var chip = e.target.closest && e.target.closest(".push-chip");
    if (!chip || e.target.closest("[data-rm]") || (e.button !== undefined && e.button > 0)) return;
    drag = { chip: chip, id: chip.getAttribute("data-comp"), from: chip.getAttribute("data-from"), x: e.clientX, y: e.clientY, moving: false, ghost: null, over: null };
    document.addEventListener("pointermove", dragMove, { passive: false });
    document.addEventListener("pointerup", dragEnd);
    document.addEventListener("pointercancel", dragEnd);
  }
  function dragMove(e) {
    if (!drag) return;
    if (!drag.moving) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 8) return;
      drag.moving = true;
      drag.ghost = drag.chip.cloneNode(true);
      drag.ghost.className = "push-chip push-ghost";
      document.body.appendChild(drag.ghost);
      drag.chip.classList.add("dragging");
    }
    e.preventDefault();
    drag.ghost.style.left = e.clientX + "px";
    drag.ghost.style.top = e.clientY + "px";
    var el = document.elementFromPoint(e.clientX, e.clientY), line = el && el.closest ? el.closest(".push-line") : null;
    if (drag.over && drag.over !== line) drag.over.classList.remove("over");
    drag.over = line;
    if (line) line.classList.add("over");
  }
  function dragEnd(e) {
    document.removeEventListener("pointermove", dragMove);
    document.removeEventListener("pointerup", dragEnd);
    document.removeEventListener("pointercancel", dragEnd);
    var d = drag;
    drag = null;
    if (!d) return;
    if (!d.moving) {   // a tap: a chip from "Not used" is added to the last line
      if (d.from === "pal" && e.type === "pointerup") editFmt(function (f) { f.l[f.l.length - 1].push(d.id); });
      return;
    }
    d.ghost.remove();
    d.chip.classList.remove("dragging");
    if (d.over) d.over.classList.remove("over");
    if (!d.over || e.type === "pointercancel") return;
    var target = d.over.getAttribute("data-line"), idx = 0;
    if (target !== "pal") {
      Array.prototype.forEach.call(d.over.querySelectorAll(".push-chip:not(.dragging)"), function (c) {
        var r = c.getBoundingClientRect();
        if (r.bottom < e.clientY || (r.top <= e.clientY && e.clientY <= r.bottom && r.left + r.width / 2 < e.clientX)) idx++;
      });
    }
    editFmt(function (f) {
      f.l.forEach(function (l) { var k = l.indexOf(d.id); if (k !== -1) l.splice(k, 1); });
      if (target !== "pal") f.l[parseInt(target, 10)].splice(idx, 0, d.id);
    });
  }

  function render() {
    var body = $("push-body");
    if (!body) return;
    if (!supported || (isIOS && !standalone)) {
      body.innerHTML = '<p class="modal-note">' + (isIOS && !standalone
        ? "On iPhone and iPad, alerts only work from the installed app: tap Share, choose Add to Home Screen, then open MLN Gameday from your Home Screen and come back here."
        : "This browser does not support push notifications.") + "</p>";
      return;
    }
    if (!window.KMFavorites || !window.KMFavorites.hasName()) {
      body.innerHTML = '<p class="modal-note">Alerts are tied to your display name (no account needed). Pick a name in Favorites first, then come back.</p>' +
        '<button type="button" class="chip active" data-push="fav">Open Favorites</button>';
      return;
    }
    var denied = Notification.permission === "denied";
    var on = !!state.sub;
    var design = state.tab === "design" && !!F;
    var tabs = F ? '<div class="push-tabs" role="tablist">' +
      [["alerts", "Alerts"], ["design", "Designer"]].map(function (t) {
        var act = (t[0] === "design") === design;
        return '<button type="button" role="tab" aria-selected="' + act + '" class="chip' + (act ? " active" : "") + '" data-ptab="' + t[0] + '">' + t[1] + "</button>";
      }).join("") + "</div>" : "";
    var alertsTab =
      (denied ? '<p class="modal-note">Notifications are blocked for this site in your browser settings.</p>' : "") +
      '<div class="settings-row"><span>' + (on ? "Alerts are on" : "Alerts are off") + '</span>' +
      '<button type="button" class="chip' + (on ? "" : " active") + '" data-push="toggle"' + (denied ? " disabled" : "") + ">" + (on ? "Turn off" : "Turn on") + "</button></div>" +
      (on
        ? '<div class="settings-row"><span>What counts as a key moment (for your alerts only)</span>' +
          '<button type="button" class="link-btn" data-push="tagsreset"' + (Array.isArray(state.prefs.tags) ? "" : " hidden") + ">Reset</button></div>" +
          '<div class="push-tags">' + tagRows() + "</div>" +
          '<div class="settings-row"><span>Favorites</span>' + levelSelect("data-pfavs", String(state.prefs.favs || 0), "Favorites plays") + "</div>" +
          '<div class="settings-row"><span>Feats</span>' + featSelect(String(state.prefs.feats || 0)) + "</div>" +
          '<table class="stats-table push-grid"><thead><tr><th>Team</th><th>Plays</th><th>Game end</th></tr></thead><tbody>' +
          '<tr><td class="push-team"><div class="push-team-in"><em>All teams</em></div></td><td>' + levelSelect('data-pall="m"', allValue("m"), "All teams plays") + "</td>" +
          '<td><input type="checkbox" data-pall="e"' + (allValue("e") === "1" ? " checked" : "") + ' aria-label="All teams game ends"></td></tr>' + teamRows() + "</tbody></table>"
        : "");
    var designTab = F ? (on ? "" : '<p class="modal-note">You can set this up now; it applies once alerts are on.</p>') + '<div id="push-composer">' + composerHtml() + "</div>" : "";
    body.innerHTML =
      '<p class="modal-note">Alerts for <strong>' + esc(window.KMFavorites.name()) + "</strong> on this device.</p>" +
      tabs + (design ? designTab : alertsTab) + '<div class="fav-status" id="push-status"></div>';
  }

  function onClick(e) {
    var t = e.target;
    if (!t.getAttribute) return;
    if (t.getAttribute("data-push") === "fav") { state.modal.hidden = true; window.KMFavorites.openPanel(); return; }
    if (t.hasAttribute("data-ptab")) { state.tab = t.getAttribute("data-ptab"); render(); return; }
    if (t.hasAttribute("data-pfmtpreset")) { var pr = F.PRESETS[parseInt(t.getAttribute("data-pfmtpreset"), 10)]; if (pr) { var pf = JSON.parse(JSON.stringify(pr[1])); editFmt(function (f) { Object.keys(pf).forEach(function (k) { f[k] = pf[k]; }); }); } return; }
    if (t.hasAttribute("data-rm")) { var rid = t.getAttribute("data-rm"); editFmt(function (f) { f.l.forEach(function (l) { var k = l.indexOf(rid); if (k !== -1) l.splice(k, 1); }); }); return; }
    if (t.hasAttribute("data-rmline")) { var ri = parseInt(t.getAttribute("data-rmline"), 10); editFmt(function (f) { if (f.l.length > 1) f.l.splice(ri, 1); }); return; }
    if (t.getAttribute("data-pfmt") === "addline") { editFmt(function (f) { if (f.l.length < MAX_LINES) f.l.push([]); }); return; }
    if (t.hasAttribute("data-pfmtkind")) { state.fmtKey = t.getAttribute("data-pfmtkind"); renderComposer(); return; }
    if (t.hasAttribute("data-pfmtcustom")) { var cp = JSON.parse(JSON.stringify(state.prefs[copyKey()] || {})); editFmt(function (f) { Object.keys(cp).forEach(function (k) { f[k] = cp[k]; }); }); return; }
    if (t.getAttribute("data-pfmt") === "reset") { stashCustom(curFmt()); delete state.prefs[fmtSlot()]; queueSave(); renderComposer(); return; }
    if (t.getAttribute("data-push") === "tagsreset") { delete state.prefs.tags; queueSave(); render(); return; }
    if (t.getAttribute("data-push") === "toggle") {
      t.disabled = true;
      (state.sub ? unsubscribe() : subscribe()).then(function () { syncButton(); render(); }).catch(function (err) {
        syncButton();
        render();
        setStatus(err.message === "denied" ? "Permission was not granted." : "Could not turn alerts on (" + err.message + ").");
      });
      return;
    }
  }

  // the inputs: favorites toggle, each team's plays level and game-end box, and the All teams row
  function onChange(e) {
    var t = e.target;
    if (!t.hasAttribute) return;
    if (t.hasAttribute("data-pfeats")) { state.prefs.feats = parseInt(t.value, 10) || 0; queueSave(); return; }
    if (t.hasAttribute("data-pfavs")) { state.prefs.favs = parseInt(t.value, 10) || 0; queueSave(); return; }
    if (t.hasAttribute("data-psample")) { state.sample = parseInt(t.value, 10) || 0; var pv = $("push-preview"); if (pv) pv.innerHTML = previewHtml(curFmt()); return; }
    if (t.hasAttribute("data-pfmtopt")) { var ok = t.getAttribute("data-pfmtopt"), ov = t.value; editFmt(function (f) { f[ok] = ov; }); return; }
    if (t.hasAttribute("data-ptag")) {
      setTags();
      queueSave();
      var rb = state.modal.querySelector('[data-push="tagsreset"]');
      if (rb) rb.hidden = !Array.isArray(state.prefs.tags);
      return;
    }
    var val = t.type === "checkbox" ? (t.checked ? 1 : 0) : parseInt(t.value, 10) || 0;
    if (t.hasAttribute("data-pteam")) {
      setTeam(t.getAttribute("data-pteam"), t.getAttribute("data-pkind"), val);
      queueSave();
      refreshAll();
    } else if (t.hasAttribute("data-pall")) {
      var kind = t.getAttribute("data-pall");
      Object.keys(state.teams || {}).forEach(function (abbr) { setTeam(abbr, kind, val); });
      queueSave();
      Array.prototype.forEach.call(state.modal.querySelectorAll('[data-pteam][data-pkind="' + kind + '"]'), function (c) {
        if (c.type === "checkbox") c.checked = !!val; else c.value = String(val);
      });
    }
  }
  function setTeam(abbr, kind, val) {
    var p = state.prefs.teams[abbr] || (state.prefs.teams[abbr] = { m: 0, e: 0 });
    p[kind] = val;
    if (!p.m && !p.e) delete state.prefs.teams[abbr];
  }
  function refreshAll() {   // keep the All teams row in step with the rows below it
    var m = state.modal.querySelector('select[data-pall="m"]'), g = state.modal.querySelector('input[data-pall="e"]');
    if (m) m.outerHTML = levelSelect('data-pall="m"', allValue("m"), "All teams plays");
    if (g) g.checked = allValue("e") === "1";
  }

  function open() {
    ensureModal().hidden = false;
    $("push-body").innerHTML = '<p class="modal-note">Loading...</p>';
    var teams = state.teams ? Promise.resolve() : fetch("data/meta.json").then(function (r) { return r.json(); }).then(function (m) { state.teams = m.teams || {}; state.tagLabels = m.tag_labels || {}; state.resultLabels = m.result_labels || {}; state.resultShort = m.result_short || {}; });
    var sub = supported ? currentSub().then(function (s) { state.sub = s; }).catch(function () { state.sub = null; }) : Promise.resolve();
    Promise.all([teams, sub]).then(function () {
      syncButton();
      if (!state.sub) return;
      // the sheet's copy wins on a fresh device that already has a subscription
      return post({ action: "push_get", endpoint: state.sub.endpoint }).then(function (r) {
        if (r && r.subscribed && r.prefs) { state.prefs = { teams: r.prefs.teams || {}, favs: parseInt(r.prefs.favs, 10) || 0 };
          if (Array.isArray(r.prefs.tags)) state.prefs.tags = r.prefs.tags;
          if (r.prefs.feats) state.prefs.feats = parseInt(r.prefs.feats, 10) || 0;
          if (r.prefs.fmt && Array.isArray(r.prefs.fmt.l)) state.prefs.fmt = r.prefs.fmt;
          if (r.prefs.fmtk && Array.isArray(r.prefs.fmtk.l)) state.prefs.fmtk = r.prefs.fmtk;
          if (r.prefs.fmtc && Array.isArray(r.prefs.fmtc.l)) state.prefs.fmtc = r.prefs.fmtc;
          if (r.prefs.fmtkc && Array.isArray(r.prefs.fmtkc.l)) state.prefs.fmtkc = r.prefs.fmtkc;
          storePrefs(); }
      }).catch(function () { /* keep the local copy */ });
    }).then(render, render);
  }

  function init() {
    if (!cfg.VAPID_PUBLIC_KEY || !endpoint()) return;
    var row = $("push-row"), btn = $("push-btn");
    if (!row || !btn) return;
    loadPrefs();
    row.hidden = false;
    btn.addEventListener("click", open);
    // already subscribed on this device: say Preferences from the start
    if (supported && Notification.permission === "granted") currentSub().then(function (sub) { state.sub = sub; syncButton(); }).catch(function () { /* ignore */ });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();

/* Push notifications (Settings > Notifications).
 *
 * No accounts: the browser's own push subscription is the identity. It is stored in the Apps Script sheet's `push` tab
 * next to the Catch Me Up name slug (so the sender can look up that name's favorites), and the preferences ride along:
 *   { teams: { ABC: { m: 1, e: 0 } }, favs: 1 }   m = every play / key moment for the team, e = game ends, favs = plays from favorites
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
    try {
      var lastBoot = Number(window.localStorage.getItem("mln-boot-report") || 0);
      if (Date.now() - lastBoot > 180000) {
        window.localStorage.setItem("mln-boot-report", String(Date.now()));
        report("OPEN: launch", "standalone=" + ((window.navigator.standalone || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)) ? 1 : 0) +
          " ctrl=" + (navigator.serviceWorker.controller ? 1 : 0) + " link=" + (location.search.slice(0, 50) || "none"));
      }
    } catch (e) { /* storage blocked: skip */ }
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

  function post(body) {
    // text/plain keeps this a "simple" request, so the browser skips the CORS preflight Apps Script cannot answer.
    return fetch(endpoint(), { method: "POST", headers: { "Content-Type": "text/plain;charset=utf-8" }, body: JSON.stringify(body) })
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
  function queueSave() {
    storePrefs();
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(function () {
      if (state.sub) post({ action: "push_prefs", endpoint: state.sub.endpoint, prefs: state.prefs }).catch(function () { setStatus("Could not save - check your connection."); });
    }, 600);
  }

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
    state.modal = m;
    return m;
  }

  var LEVELS = [["0", "Off"], ["1", "Key moments"], ["2", "All plays"]];
  function levelSelect(attrs, value, label) {
    return '<select class="push-level" ' + attrs + ' aria-label="' + esc(label) + '">' +
      (value === "" ? '<option value="" selected disabled>Set all...</option>' : "") +
      LEVELS.map(function (l) { return '<option value="' + l[0] + '"' + (l[0] === value ? " selected" : "") + ">" + l[1] + "</option>"; }).join("") + "</select>";
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
    body.innerHTML =
      '<p class="modal-note">Alerts for <strong>' + esc(window.KMFavorites.name()) + "</strong> on this device. They arrive a few minutes after the play, not live.</p>" +
      (denied ? '<p class="modal-note">Notifications are blocked for this site in your browser settings.</p>' : "") +
      '<div class="settings-row"><span>' + (on ? "Alerts are on" : "Alerts are off") + '</span>' +
      '<button type="button" class="chip' + (on ? "" : " active") + '" data-push="toggle"' + (denied ? " disabled" : "") + ">" + (on ? "Turn off" : "Turn on") + "</button></div>" +
      (on
        ? '<label class="settings-row"><span>Plays from my favorites</span><input type="checkbox" data-pfavs' + (state.prefs.favs ? " checked" : "") + "></label>" +
          '<table class="stats-table push-grid"><thead><tr><th>Team</th><th>Plays</th><th>Game end</th></tr></thead><tbody>' +
          '<tr><td class="push-team"><div class="push-team-in"><em>All teams</em></div></td><td>' + levelSelect('data-pall="m"', allValue("m"), "All teams plays") + "</td>" +
          '<td><input type="checkbox" data-pall="e"' + (allValue("e") === "1" ? " checked" : "") + ' aria-label="All teams game ends"></td></tr>' + teamRows() + "</tbody></table>"
        : "") +
      '<div class="fav-status" id="push-status"></div>';
  }

  function onClick(e) {
    var t = e.target;
    if (!t.getAttribute) return;
    if (t.getAttribute("data-push") === "fav") { state.modal.hidden = true; window.KMFavorites.openPanel(); return; }
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
    if (t.hasAttribute("data-pfavs")) { state.prefs.favs = t.checked ? 1 : 0; queueSave(); return; }
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
    var teams = state.teams ? Promise.resolve() : fetch("data/meta.json").then(function (r) { return r.json(); }).then(function (m) { state.teams = m.teams || {}; });
    var sub = supported ? currentSub().then(function (s) { state.sub = s; }).catch(function () { state.sub = null; }) : Promise.resolve();
    Promise.all([teams, sub]).then(function () {
      syncButton();
      if (!state.sub) return;
      // the sheet's copy wins on a fresh device that already has a subscription
      return post({ action: "push_get", endpoint: state.sub.endpoint }).then(function (r) {
        if (r && r.subscribed && r.prefs) { state.prefs = { teams: r.prefs.teams || {}, favs: r.prefs.favs ? 1 : 0 }; storePrefs(); }
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

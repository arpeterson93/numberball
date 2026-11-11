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
    state.modal = m;
    return m;
  }

  function teamRows() {
    var t = state.teams || {};
    return Object.keys(t).sort().map(function (abbr) {
      var p = state.prefs.teams[abbr] || {};
      return '<tr><td class="push-team"><strong>' + esc(abbr) + "</strong> " + esc(t[abbr].name || "") + "</td>" +
        '<td><input type="checkbox" data-pteam="' + abbr + '" data-pkind="m"' + (p.m ? " checked" : "") + ' aria-label="' + esc(abbr) + ' plays"></td>' +
        '<td><input type="checkbox" data-pteam="' + abbr + '" data-pkind="e"' + (p.e ? " checked" : "") + ' aria-label="' + esc(abbr) + ' game ends"></td></tr>';
    }).join("");
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
          '<tr><td class="push-team"><em>All teams</em></td><td><input type="checkbox" data-pall="m" aria-label="All teams plays"></td>' +
          '<td><input type="checkbox" data-pall="e" aria-label="All teams game ends"></td></tr>' + teamRows() + "</tbody></table>"
        : "") +
      '<div class="fav-status" id="push-status"></div>';
  }

  function onClick(e) {
    var t = e.target;
    if (!t.getAttribute) return;
    if (t.getAttribute("data-push") === "fav") { state.modal.hidden = true; window.KMFavorites.openPanel(); return; }
    if (t.getAttribute("data-push") === "toggle") {
      t.disabled = true;
      (state.sub ? unsubscribe() : subscribe()).then(render).catch(function (err) {
        render();
        setStatus(err.message === "denied" ? "Permission was not granted." : "Could not turn alerts on (" + err.message + ").");
      });
      return;
    }
    if (t.hasAttribute("data-pfavs")) { state.prefs.favs = t.checked ? 1 : 0; queueSave(); return; }
    if (t.hasAttribute("data-pteam")) {
      var a = t.getAttribute("data-pteam"), k = t.getAttribute("data-pkind");
      var p = state.prefs.teams[a] || (state.prefs.teams[a] = { m: 0, e: 0 });
      p[k] = t.checked ? 1 : 0;
      if (!p.m && !p.e) delete state.prefs.teams[a];
      queueSave();
      return;
    }
    if (t.hasAttribute("data-pall")) {
      var kind = t.getAttribute("data-pall"), on = t.checked;
      Object.keys(state.teams || {}).forEach(function (abbr) {
        var q = state.prefs.teams[abbr] || (state.prefs.teams[abbr] = { m: 0, e: 0 });
        q[kind] = on ? 1 : 0;
        if (!q.m && !q.e) delete state.prefs.teams[abbr];
      });
      queueSave();
      Array.prototype.forEach.call(state.modal.querySelectorAll('[data-pteam][data-pkind="' + kind + '"]'), function (c) { c.checked = on; });
    }
  }

  function open() {
    ensureModal().hidden = false;
    $("push-body").innerHTML = '<p class="modal-note">Loading...</p>';
    var teams = state.teams ? Promise.resolve() : fetch("data/meta.json").then(function (r) { return r.json(); }).then(function (m) { state.teams = m.teams || {}; });
    var sub = supported ? currentSub().then(function (s) { state.sub = s; }).catch(function () { state.sub = null; }) : Promise.resolve();
    Promise.all([teams, sub]).then(function () {
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
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();

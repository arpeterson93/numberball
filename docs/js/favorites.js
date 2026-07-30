/* Favorites layer.
 *
 * Deliberately not authentication: a display name typed once is remembered in
 * localStorage and used as the key into a small Google Sheet via an Apps Script
 * web app. Two people who type the same name share a list. That tradeoff is
 * accepted - see apps-script/DEPLOY.md.
 */
(function () {
  "use strict";

  var NAME_KEY = "km_display_name";
  var LOCAL_IDS_KEY = "km_favorite_ids";

  var state = {
    name: null,
    key: null,
    ids: new Set(),
    players: [],
    loaded: false,
    onChange: null,
    saveTimer: null,
  };

  function endpoint() {
    return (window.KM_CONFIG && window.KM_CONFIG.FAVORITES_ENDPOINT) || "";
  }

  function slugify(name) {
    return String(name || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60);
  }

  function readLocalIds() {
    try {
      var raw = window.localStorage.getItem(LOCAL_IDS_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function writeLocalIds() {
    try {
      window.localStorage.setItem(LOCAL_IDS_KEY, JSON.stringify(Array.from(state.ids)));
    } catch (e) {
      /* private browsing - in-memory only */
    }
  }

  function setStatus(msg) {
    var el = document.getElementById("fav-status");
    if (el) el.textContent = msg || "";
  }

  function notify() {
    writeLocalIds();
    if (state.onChange) state.onChange();
  }

  function load() {
    if (!state.key || !endpoint()) {
      state.loaded = true;
      return Promise.resolve();
    }
    setStatus("Loading your list...");
    var url = endpoint() + "?key=" + encodeURIComponent(state.key);
    return fetch(url, { method: "GET" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var ids = (data && data.player_ids) || [];
        state.ids = new Set(ids.map(Number));
        state.loaded = true;
        setStatus(ids.length ? ids.length + " favorite(s) loaded." : "No favorites yet - star some players.");
        notify();
      })
      .catch(function () {
        state.loaded = true;
        setStatus("Could not reach the favorites service - using this device's copy.");
        notify();
      });
  }

  function save() {
    if (!state.key || !endpoint()) return;
    window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(function () {
      setStatus("Saving...");
      fetch(endpoint(), {
        method: "POST",
        // text/plain dodges the CORS preflight Apps Script cannot answer.
        headers: { "Content-Type": "text/plain;charset=utf-8" },
        body: JSON.stringify({ key: state.key, player_ids: Array.from(state.ids), note: state.name }),
      })
        .then(function (r) { return r.json(); })
        .then(function () { setStatus("Saved."); })
        .catch(function () { setStatus("Save failed - your list is still stored on this device."); });
    }, 400);
  }

  function setName(name) {
    var slug = slugify(name);
    if (!slug) return Promise.resolve();
    state.name = String(name).trim();
    state.key = slug;
    try {
      window.localStorage.setItem(NAME_KEY, state.name);
    } catch (e) { /* ignore */ }
    return load();
  }

  function renderList(filter) {
    var listEl = document.getElementById("fav-list");
    if (!listEl) return;
    var needle = String(filter || "").trim().toLowerCase();
    var rows = state.players.filter(function (p) {
      if (!needle) return state.ids.has(p.id);
      return p.name.toLowerCase().indexOf(needle) !== -1;
    });
    if (!rows.length) {
      listEl.innerHTML = '<div class="fav-row">' +
        (needle ? "No players match that search." : "No favorites yet - search above to add some.") +
        "</div>";
      return;
    }
    listEl.innerHTML = rows.slice(0, 60).map(function (p) {
      return '<div class="fav-row">' +
        '<button type="button" class="star-btn ' + (state.ids.has(p.id) ? "on" : "") +
        '" data-fav-id="' + p.id + '">' + (state.ids.has(p.id) ? "★" : "☆") + "</button>" +
        "<span>" + escapeHtml(p.name) + "</span>" +
        '<span class="fav-team">' + escapeHtml(p.team || "") + "</span>" +
        "</div>";
    }).join("");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toggle(id) {
    id = Number(id);
    if (!id) return;
    if (state.ids.has(id)) state.ids.delete(id);
    else state.ids.add(id);
    notify();
    save();
  }

  function wirePanel() {
    var modal = document.getElementById("fav-modal");
    var openBtn = document.getElementById("favorites-btn");
    var closeBtn = document.getElementById("fav-close");
    var nameInput = document.getElementById("fav-name");
    var saveNameBtn = document.getElementById("fav-save-name");
    var search = document.getElementById("fav-search");
    var listEl = document.getElementById("fav-list");
    var note = document.getElementById("fav-note");

    if (!endpoint() && note) {
      note.textContent = "The favorites service is not configured yet, so stars are kept " +
        "on this device only. See apps-script/DEPLOY.md to connect it.";
    }

    function openPanel() {
      modal.hidden = false;
      nameInput.value = state.name || "";
      search.disabled = !state.key && !!endpoint();
      renderList(search.value);
    }
    function closePanel() { modal.hidden = true; }

    openBtn.addEventListener("click", openPanel);
    closeBtn.addEventListener("click", closePanel);
    modal.addEventListener("click", function (e) { if (e.target === modal) closePanel(); });

    saveNameBtn.addEventListener("click", function () {
      setName(nameInput.value).then(function () {
        search.disabled = false;
        renderList(search.value);
      });
    });
    nameInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") saveNameBtn.click();
    });
    search.addEventListener("input", function () { renderList(search.value); });
    listEl.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-fav-id]");
      if (!btn) return;
      toggle(btn.getAttribute("data-fav-id"));
      renderList(search.value);
    });
  }

  window.KMFavorites = {
    init: function (players, onChange) {
      state.players = players || [];
      state.onChange = onChange || null;
      state.ids = new Set(readLocalIds().map(Number));
      wirePanel();
      var stored = null;
      try { stored = window.localStorage.getItem(NAME_KEY); } catch (e) { /* ignore */ }
      if (stored) return setName(stored);
      state.loaded = true;
      return Promise.resolve();
    },
    has: function (id) { return state.ids.has(Number(id)); },
    toggle: toggle,
    count: function () { return state.ids.size; },
    name: function () { return state.name; },
    refreshList: function () { renderList((document.getElementById("fav-search") || {}).value); },
  };
})();

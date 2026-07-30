/* MLN Key Moments - static feed.
 *
 * Loads the JSON written by key_moments_build.py, then does every filter and
 * sort client-side. No server round-trip per interaction.
 *
 * Two pools, chosen by the "Key Moments" toggle (default on), independent of
 * every other filter including Favorites:
 *   Key Moments on  - key moments only (key_moments.json, loaded on boot)
 *   Key Moments off - every play of the active session(s), key moment or not
 *                     (plays_NN.json, fetched lazily the first time it's needed)
 * Favorites, Rookies, Result, League, Team, Player and the tag chips are all
 * plain AND filters on top of whichever pool is active - so "Favorites only"
 * with "Key Moments" switched off is how you browse one player's full game,
 * not just their key moments.
 */
(function () {
  "use strict";

  var GAME_LINK_BASE = "https://www.mln-reference.com/live/";
  var PLAYER_LINK_BASE = "https://www.mln-reference.com/player/";

  var data = {
    moments: [],        // key moments, whole season
    players: [],
    meta: {},
    playsBySession: {}, // session number -> every play of that session
  };

  var filters = {
    session: null,      // number, or null for the whole season
    result: "",         // "" | "hitting" | "pitching" | "hr"  (radio, "" = all)
    league: "",         // "" (all MLN) | "GL" | "LL"
    keyMomentsOnly: true,  // pool switch - on = curated feed, off = every play
    rookiesOnly: false,
    favoritesOnly: false,
    team: "",
    player: "",         // free-text substring, used only when playerId is null
    playerId: null,     // set by picking a row from the suggestion dropdown
    tags: new Set(),    // multi-select, OR'd together
    sort: "chrono",
  };

  var loadingPlays = false;

  // ── helpers ─────────────────────────────────────────────────────────────────

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function bust(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "t=" + Date.now();
  }

  function getJSON(url) {
    return fetch(bust(url), { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": " + r.status);
      return r.json();
    });
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function formatMomentTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var h = d.getHours();
    var ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + h + ":" + pad2(d.getMinutes()) + " " + ampm;
  }

  function formatBuiltAt(iso) {
    if (!iso) return "Data as of -";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "Data as of " + iso;
    var mins = Math.round((Date.now() - d.getTime()) / 60000);
    var ago;
    if (mins < 1) ago = "just now";
    else if (mins < 60) ago = mins + " min ago";
    else if (mins < 1440) ago = Math.round(mins / 60) + " hr ago";
    else ago = Math.round(mins / 1440) + " day(s) ago";
    return "Updated " + ago;
  }

  function toast(msg) {
    var el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    window.clearTimeout(el._timer);
    el._timer = window.setTimeout(function () { el.hidden = true; }, 3500);
  }

  function activeSessions() {
    if (filters.session !== null) return [filters.session];
    return (data.meta.sessions || []).slice();
  }

  // ── pools and filtering ─────────────────────────────────────────────────────

  function isFavorited(m) {
    var fav = window.KMFavorites;
    if (!fav) return false;
    // Only featured_id and counterpart_id are ever actually shown/starred
    // on a card. batter_id/pitcher_id are NOT the same thing on a steal or
    // caught stealing (they're just whoever happened to be at the plate/on
    // the mound during that steal attempt, not the runner or catcher the
    // card displays) - matching on them made Favorites+Steal surface plays
    // where neither visible name was actually a favorite.
    return fav.has(m.featured_id) || fav.has(m.counterpart_id);
  }

  function isFavoritedId(id) {
    var fav = window.KMFavorites;
    return !!(fav && id && fav.has(id));
  }

  var rookieIds = null;

  function isRookie(m) {
    if (!rookieIds) {
      rookieIds = new Set();
      (data.players || []).forEach(function (p) {
        if (p.rookie) rookieIds.add(p.id);
      });
    }
    // Same reasoning as isFavorited above - only featured_id/counterpart_id
    // are ever shown on a card, so those are the only two who can make a
    // play a "rookie" moment.
    return rookieIds.has(m.featured_id) || rookieIds.has(m.counterpart_id);
  }

  /* The "Key Moments" toggle swaps the pool rather than narrowing it - off
     means every play of the active session(s), not just the curated feed.
     Every other filter (including Favorites) then narrows whichever pool is
     active, in matches() below. */
  function pool() {
    if (filters.keyMomentsOnly) {
      return data.moments.filter(function (m) {
        return filters.session === null || m.session_number === filters.session;
      });
    }
    var rows = [];
    activeSessions().forEach(function (s) {
      var loaded = data.playsBySession[s];
      if (loaded) rows = rows.concat(loaded);
    });
    return rows;
  }

  function matches(m) {
    if (filters.favoritesOnly && !isFavorited(m)) return false;
    if (filters.result === "hr") {
      if (m.result !== "HR") return false;
    } else if (filters.result && m.result_category !== filters.result) {
      return false;
    }
    if (filters.league) {
      var teams = data.meta.teams || {};
      var off = (teams[m.off_team_abbr] || {}).sub_league;
      var def = (teams[m.def_team_abbr] || {}).sub_league;
      if (off !== filters.league && def !== filters.league) return false;
    }
    if (filters.team && m.off_team_abbr !== filters.team && m.def_team_abbr !== filters.team) return false;
    if (filters.playerId) {
      // Picked from the suggestion dropdown - match only the two people a
      // card can actually show (featured + counterpart). batter_id/
      // pitcher_id/runner_id are NOT the same thing on a steal or caught
      // stealing (just whoever was at the plate/on the mound during that
      // attempt, not the runner or catcher displayed) - matching on those
      // would surface plays where the picked player is nowhere on the card.
      var pid = filters.playerId;
      if (m.featured_id !== pid && m.counterpart_id !== pid) return false;
    } else if (filters.player) {
      var needle = filters.player.toLowerCase();
      var hay = [m.featured_name, m.counterpart_name].join(" ").toLowerCase();
      if (hay.indexOf(needle) === -1) return false;
    }
    if (filters.rookiesOnly && !isRookie(m)) return false;
    if (filters.tags.size) {
      // OR within the tag group, AND against everything else.
      var hit = (m.tags || []).some(function (t) { return filters.tags.has(t); });
      if (!hit) return false;
    }
    return true;
  }

  function sorted(rows) {
    var out = rows.slice();
    if (filters.sort === "wpa") {
      out.sort(function (a, b) { return Math.abs(b.wpa || 0) - Math.abs(a.wpa || 0); });
    } else if (filters.sort === "leverage") {
      out.sort(function (a, b) { return (b.leverage || 0) - (a.leverage || 0); });
    } else {
      out.sort(function (a, b) {
        var ta = a.timestamp || "", tb = b.timestamp || "";
        if (ta !== tb) return ta < tb ? 1 : -1;
        return b.play_num - a.play_num;
      });
    }
    return out;
  }

  // ── rendering ───────────────────────────────────────────────────────────────

  function teamColor(abbr) {
    var hex = ((data.meta.teams || {})[abbr] || {}).primary_hex || "";
    if (hex && hex.charAt(0) !== "#") hex = "#" + hex;
    return hex;
  }

  function wpFragment(m) {
    if (m.featured_wp_after == null || m.featured_wpa == null) return "";
    var pct = Math.round(m.featured_wp_after * 100);
    var delta = m.featured_wpa * 100;
    var cls = delta >= 0 ? "wpa-pos" : "wpa-neg";
    var sign = delta >= 0 ? "+" : "";
    return "<span>" + escapeHtml(m.featured_team_abbr) + " win probability " +
      '<span class="' + cls + '">' + pct + "% (" + sign + delta.toFixed(1) + ")</span></span>";
  }

  function scoreBlock(m) {
    var awayBatting = !m.batting_is_home;
    return '<div class="score-block">' +
      '<div class="row' + (awayBatting ? " batting" : "") + '">' +
        '<span class="abbr">' + escapeHtml(m.away_team_abbr) + "</span>" +
        '<span class="val">' + m.away_score + "</span></div>" +
      '<div class="row' + (awayBatting ? "" : " batting") + '">' +
        '<span class="abbr">' + escapeHtml(m.home_team_abbr) + "</span>" +
        '<span class="val">' + m.home_score + "</span></div>" +
      "</div>";
  }

  /* Game-final wins over half-inning-final: the last out of a game is both,
     and FINAL is the more informative badge. */
  function stateStack(m) {
    if (m.is_game_final) {
      return '<div class="state-stack"><div class="state-badge final">FINAL</div></div>';
    }
    if (m.is_half_inning_final) {
      return '<div class="state-stack"><div class="state-badge">END INNING</div></div>';
    }
    var svg = (data.meta.bases_svg || {})[m.obc_after] || "";
    // outs_after is 0, 1 or 2 here - a 3rd out always routes to a badge above.
    var dots = [0, 1].map(function (i) {
      return '<span class="dot' + (i < m.outs_after ? " on" : "") + '"></span>';
    }).join("");
    return '<div class="state-stack">' + svg + '<div class="outs-dots">' + dots + "</div></div>";
  }

  function scoringLine(m) {
    var names = m.scoring_names || [];
    if (!names.length) return "";
    var text = names.length === 1
      ? names[0] + " scores"
      : names.join(", ") + " score";
    return '<div class="scoring-line">' + escapeHtml(text) + "</div>";
  }

  function diffPill(m) {
    if (m.diff === 0) return '<span class="diff-pill zero">0 Diff</span>';
    if (m.diff === 500) return '<span class="diff-pill five">500 Diff</span>';
    return "";
  }

  function card(m) {
    var labels = data.meta.tag_labels || {};
    var isFav = window.KMFavorites && m.featured_id && window.KMFavorites.has(m.featured_id);
    var star = m.featured_id
      ? '<button type="button" class="star-btn ' + (isFav ? "on" : "") +
        '" data-fav-id="' + m.featured_id + '" title="Favorite this player">' +
        (isFav ? "★" : "☆") + "</button>"
      : "";
    var levText = m.leverage != null ? "<span>Leverage " + m.leverage.toFixed(1) + "</span>" : "";
    var why = (m.tags || []).map(function (t) {
      return '<span class="why-tag">' + escapeHtml(labels[t] || t) + "</span>";
    }).join("");
    var resultLabel = (data.meta.result_labels || {})[m.result] || m.result;
    var counterpart = (m.counterpart_id && m.counterpart_id !== m.featured_id)
      ? '<span class="counterpart">vs ' +
        (isFavoritedId(m.counterpart_id)
          ? '<span class="counterpart-fav" title="On your favorites list">★</span> '
          : "") +
        '<a class="counterpart-name" href="' + PLAYER_LINK_BASE + encodeURIComponent(m.counterpart_id) +
        '" target="_blank" rel="noopener noreferrer">' + escapeHtml(m.counterpart_name) + "</a></span>"
      : "";
    var gameLink = m.game_code
      ? '<a class="game-link" href="' + GAME_LINK_BASE + encodeURIComponent(m.game_code) +
        '" target="_blank" rel="noopener noreferrer" title="View this game on MLN Reference" ' +
        'aria-label="View this game on MLN Reference">↗︎</a>'
      : "";

    var levBarHex = teamColor(m.featured_team_abbr);

    return '<div class="moment">' +
      gameLink +
      '<div class="lev-bar' + (levBarHex ? "" : " neutral") + '"' +
        (levBarHex ? ' style="background:' + escapeHtml(levBarHex) + '"' : "") + "></div>" +
      '<div class="moment-left">' +
        '<div class="timestamp">' + escapeHtml(formatMomentTime(m.timestamp)) + "</div>" +
        '<div class="play-line">' + star +
          (m.featured_id
            ? '<a class="player-name" href="' + PLAYER_LINK_BASE + encodeURIComponent(m.featured_id) +
              '" target="_blank" rel="noopener noreferrer">' + escapeHtml(m.featured_name) + "</a>"
            : '<span class="player-name">' + escapeHtml(m.featured_name) + "</span>") +
          '<span class="result-pill ' + (m.result_category === "hitting" ? "offense" : "defense") + '">' +
            escapeHtml(resultLabel) + "</span>" +
          diffPill(m) +
          counterpart +
        "</div>" +
        scoringLine(m) +
        '<div class="meta-line">' + wpFragment(m) + levText + "</div>" +
        '<div class="why-line">' + why + "</div>" +
      "</div>" +
      '<div class="moment-right">' +
        '<div class="inning-indicator">' +
          '<div class="tri ' + (m.half === "top" ? "up" : "down") + '"></div>' +
          '<div class="inning-num">' + m.inning + "</div>" +
        "</div>" +
        scoreBlock(m) +
        stateStack(m) +
      "</div>" +
      /* Phone-only: the tags/why-line repeat here so they can sit below the
         scorebug instead of under meta-line - CSS shows only one copy at a
         time depending on breakpoint (see style.css). */
      '<div class="why-line why-line-bottom">' + why + "</div>" +
    "</div>";
  }

  /* Drives the phone-only "Filters (N active)" bar. Every chip and field
     counts on its own, tag chips included, so the number matches what a user
     would count if they expanded the panel. */
  function activeFilterCount() {
    var n = 0;
    if (!filters.keyMomentsOnly) n += 1;   // off is the non-default state
    if (filters.result) n += 1;
    if (filters.league) n += 1;
    if (filters.rookiesOnly) n += 1;
    if (filters.favoritesOnly) n += 1;
    if (filters.team) n += 1;
    if (filters.player) n += 1;
    return n + filters.tags.size;
  }

  function updateFilterSummary() {
    var n = activeFilterCount();
    $("filters-toggle-label").textContent = n ? "Filters (" + n + " active)" : "Filters";
  }

  function render() {
    updateFilterSummary();
    $("page-title").textContent = filters.keyMomentsOnly ? "KEY MOMENTS" : "ALL PLAYS";
    if (loadingPlays) {
      $("moments").innerHTML = "";
      $("empty-state").hidden = false;
      $("empty-state").textContent = "Loading plays...";
      return;
    }
    var rows = sorted(pool().filter(matches));
    $("moments").innerHTML = rows.map(card).join("");
    $("empty-state").hidden = rows.length > 0;
    $("empty-state").textContent = filters.keyMomentsOnly
      ? (filters.favoritesOnly
          ? "No key moments from your favorites match these filters."
          : "No key moments match these filters.")
      : (filters.favoritesOnly
          ? "No plays from your favorites match these filters."
          : "No plays match these filters.");
    var noun = filters.keyMomentsOnly
      ? (rows.length === 1 ? " key moment" : " key moments")
      : (rows.length === 1 ? " play" : " plays");
    $("count-text").textContent = rows.length + noun;
  }

  // ── lazy play loading ───────────────────────────────────────────────────────

  function ensurePlaysLoaded() {
    var needed = activeSessions().filter(function (s) { return !data.playsBySession[s]; });
    if (!needed.length) return Promise.resolve();
    loadingPlays = true;
    render();
    return Promise.all(needed.map(function (s) {
      return getJSON("data/plays_" + pad2(s) + ".json").then(function (rows) {
        data.playsBySession[s] = rows;
      });
    })).then(function () {
      loadingPlays = false;
    }).catch(function () {
      loadingPlays = false;
      toast("Could not load the full play list.");
    });
  }

  function renderMaybeLoading() {
    if (!filters.keyMomentsOnly) {
      ensurePlaysLoaded().then(render);
    } else {
      render();
    }
  }

  // ── controls ────────────────────────────────────────────────────────────────

  function populateSessionSelect(keepSelection) {
    var sel = $("session-select");
    var sessions = data.meta.sessions || [];
    // Plain integer - never "Session 03".
    sel.innerHTML = '<option value="">Full season</option>' +
      sessions.map(function (s) {
        return '<option value="' + s + '">Session ' + parseInt(s, 10) + "</option>";
      }).join("");
    if (keepSelection && (filters.session === null || sessions.indexOf(filters.session) !== -1)) {
      sel.value = filters.session === null ? "" : String(filters.session);
    } else if (sessions.length) {
      filters.session = sessions[0];
      sel.value = String(sessions[0]);
    }
  }

  function populateTeamSelect() {
    var teams = data.meta.teams || {};
    var abbrs = Object.keys(teams).sort(function (a, b) {
      return (teams[a].name || a).localeCompare(teams[b].name || b);
    });
    $("team-select").innerHTML = '<option value="">All teams</option>' +
      abbrs.map(function (a) {
        return '<option value="' + escapeHtml(a) + '">' + escapeHtml(teams[a].name || a) + "</option>";
      }).join("");
    if (filters.team && abbrs.indexOf(filters.team) !== -1) {
      $("team-select").value = filters.team;
    } else {
      filters.team = "";
    }
  }

  var PLAYER_SUGGEST_LIMIT = 8;

  function renderPlayerSuggest(query) {
    var box = $("player-suggest");
    var needle = query.trim().toLowerCase();
    if (!needle) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    var matches = (data.players || []).filter(function (p) {
      return p.name && p.name.toLowerCase().indexOf(needle) !== -1;
    }).slice(0, PLAYER_SUGGEST_LIMIT);
    if (!matches.length) {
      box.innerHTML = '<div class="player-suggest-empty">No players match.</div>';
      box.hidden = false;
      return;
    }
    box.innerHTML = matches.map(function (p) {
      return '<div class="player-suggest-row" data-player-id="' + p.id +
        '" data-player-name="' + escapeHtml(p.name) + '">' + escapeHtml(p.name) +
        '<span class="team">' + escapeHtml(p.team || "") + "</span></div>";
    }).join("");
    box.hidden = false;
  }

  function populateTagChips() {
    var labels = data.meta.tag_labels || {};
    $("tag-chips").innerHTML = Object.keys(labels).map(function (slug) {
      return '<button type="button" class="chip multi" data-tag="' + escapeHtml(slug) + '">' +
        escapeHtml(labels[slug]) + "</button>";
    }).join("");
  }

  function wireControls() {
    // Phone-only disclosure. CSS force-shows the panel above 600px, so the
    // class this leaves behind cannot strand a desktop user with it shut.
    $("filters-toggle").addEventListener("click", function () {
      var collapsed = $("filters-card").classList.toggle("collapsed");
      this.setAttribute("aria-expanded", String(!collapsed));
      this.querySelector(".caret").textContent = collapsed ? "▾" : "▴";
    });

    $("session-select").addEventListener("change", function (e) {
      filters.session = e.target.value === "" ? null : Number(e.target.value);
      renderMaybeLoading();
    });

    // Result is a radio group that can also be fully off: clicking the active
    // chip clears it back to "all categories".
    $("result-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var kind = chip.getAttribute("data-result");
      filters.result = (filters.result === kind) ? "" : kind;
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-result") === filters.result);
      });
      render();
    });

    // League always has exactly one chip active; MLN is the neutral default.
    $("league-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      filters.league = chip.getAttribute("data-league");
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", c === chip);
      });
      render();
    });

    // Key Moments is the pool switch (default on); Rookies and Favorites are
    // independent booleans that AND with everything, including each other.
    $("toggle-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var which = chip.getAttribute("data-toggle");
      if (which === "keymoments") {
        filters.keyMomentsOnly = !filters.keyMomentsOnly;
        chip.classList.toggle("active", filters.keyMomentsOnly);
        renderMaybeLoading();
      } else if (which === "rookies") {
        filters.rookiesOnly = !filters.rookiesOnly;
        chip.classList.toggle("active", filters.rookiesOnly);
        render();
      } else {
        filters.favoritesOnly = !filters.favoritesOnly;
        chip.classList.toggle("active", filters.favoritesOnly);
        render();
      }
    });

    // Tag chips multi-select and OR together.
    $("tag-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var slug = chip.getAttribute("data-tag");
      if (filters.tags.has(slug)) filters.tags.delete(slug);
      else filters.tags.add(slug);
      chip.classList.toggle("active", filters.tags.has(slug));
      render();
    });

    $("sort-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      filters.sort = chip.getAttribute("data-sort");
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", c === chip);
      });
      render();
    });

    $("team-select").addEventListener("change", function (e) {
      filters.team = e.target.value;
      render();
    });

    var playerTimer;
    $("player-input").addEventListener("input", function (e) {
      var v = e.target.value;
      filters.playerId = null;   // typing again invalidates a previous exact pick
      renderPlayerSuggest(v);
      window.clearTimeout(playerTimer);
      playerTimer = window.setTimeout(function () {
        filters.player = v.trim();
        render();
      }, 150);
    });

    $("player-input").addEventListener("focus", function (e) {
      if (e.target.value.trim()) renderPlayerSuggest(e.target.value);
    });

    $("player-input").addEventListener("blur", function () {
      // Delayed so a click on a suggestion row (below) still lands first.
      window.setTimeout(function () { $("player-suggest").hidden = true; }, 120);
    });

    // mousedown, not click - fires before the input's blur, so the pick
    // registers instead of the dropdown closing first.
    $("player-suggest").addEventListener("mousedown", function (e) {
      var row = e.target.closest(".player-suggest-row");
      if (!row) return;
      e.preventDefault();
      var id = Number(row.getAttribute("data-player-id"));
      var name = row.getAttribute("data-player-name");
      filters.playerId = id;
      filters.player = name;
      $("player-input").value = name;
      $("player-suggest").hidden = true;
      window.clearTimeout(playerTimer);
      render();
    });

    $("reset-btn").addEventListener("click", function () {
      filters.result = "";
      filters.league = "";
      filters.keyMomentsOnly = true;
      filters.rookiesOnly = false;
      filters.favoritesOnly = false;
      filters.team = "";
      filters.player = "";
      filters.playerId = null;
      filters.tags.clear();
      $("player-suggest").hidden = true;
      Array.prototype.forEach.call(
        document.querySelectorAll("#result-chips .chip, #tag-chips .chip"),
        function (c) { c.classList.remove("active"); }
      );
      // Key Moments defaults active, unlike Rookies/Favorites - not a blanket clear.
      Array.prototype.forEach.call(document.querySelectorAll("#toggle-chips .chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-toggle") === "keymoments");
      });
      Array.prototype.forEach.call(document.querySelectorAll("#league-chips .chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-league") === "");
      });
      $("team-select").value = "";
      $("player-input").value = "";
      renderMaybeLoading();
    });

    $("moments").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-fav-id]");
      if (!btn || !window.KMFavorites) return;
      window.KMFavorites.toggle(btn.getAttribute("data-fav-id"));
    });

    $("refresh-btn").addEventListener("click", requestRefresh);
  }

  // ── refresh ─────────────────────────────────────────────────────────────────

  function reloadData() {
    return Promise.all([
      getJSON("data/key_moments.json"),
      getJSON("data/meta.json"),
    ]).then(function (res) {
      data.moments = res[0];
      data.meta = res[1];
      data.playsBySession = {};   // stale once the feed moves
      $("built-at").textContent = formatBuiltAt(data.meta.built_at);
      populateSessionSelect(true);
      populateTeamSelect();
      populateTagChips();
      Array.prototype.forEach.call(document.querySelectorAll("#tag-chips .chip"), function (c) {
        c.classList.toggle("active", filters.tags.has(c.getAttribute("data-tag")));
      });
      renderMaybeLoading();
    });
  }

  function requestRefresh() {
    var url = (window.KM_CONFIG && window.KM_CONFIG.REFRESH_ENDPOINT) || "";
    var btn = $("refresh-btn");
    var status = $("refresh-status");
    if (!url) {
      status.textContent = "Refresh endpoint not configured - see apps-script/DEPLOY.md.";
      return;
    }
    var startedAt = data.meta.built_at || "";
    btn.disabled = true;
    status.textContent = "Refreshing...";

    fetch(url + "?action=trigger_refresh")
      .then(function (r) { return r.json(); })
      .catch(function () { return null; })
      .then(function (res) {
        if (res && res.error) {
          status.textContent = "Refresh rejected: " + res.error;
          btn.disabled = false;
          return;
        }
        status.textContent = "Fetching the latest plays - usually done within a couple minutes.";
        pollForRebuild(startedAt, Date.now() + 180000);
      });
  }

  function pollForRebuild(startedAt, deadline) {
    var btn = $("refresh-btn");
    var status = $("refresh-status");
    window.setTimeout(function () {
      getJSON("data/meta.json")
        .then(function (meta) {
          if (meta.built_at && meta.built_at !== startedAt) {
            return reloadData().then(function () {
              status.textContent = "";
              btn.disabled = false;
              toast("Key moments updated.");
            });
          }
          if (Date.now() > deadline) {
            status.textContent = "Still refreshing - check back shortly.";
            btn.disabled = false;
            return;
          }
          pollForRebuild(startedAt, deadline);
        })
        .catch(function () {
          if (Date.now() > deadline) {
            status.textContent = "Still refreshing - check back shortly.";
            btn.disabled = false;
          } else {
            pollForRebuild(startedAt, deadline);
          }
        });
    }, 6000);
  }

  // ── boot ────────────────────────────────────────────────────────────────────

  function boot() {
    wireControls();
    Promise.all([
      getJSON("data/key_moments.json"),
      getJSON("data/players.json"),
      getJSON("data/meta.json"),
    ]).then(function (res) {
      data.moments = res[0];
      data.players = res[1];
      data.meta = res[2];
      $("built-at").textContent = formatBuiltAt(data.meta.built_at);
      populateSessionSelect(false);
      populateTeamSelect();
      populateTagChips();
      render();
      window.KMFavorites.init(data.players, function () {
        renderMaybeLoading();
        window.KMFavorites.refreshList();
      });
    }).catch(function (err) {
      $("built-at").textContent = "";
      $("empty-state").hidden = false;
      $("empty-state").textContent = "Could not load the key moments feed (" + err.message + ").";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

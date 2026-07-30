/* MLN Key Moments - static feed.
 *
 * Loads the JSON written by key_moments_build.py, then does every filter and
 * sort client-side. No server round-trip per interaction.
 */
(function () {
  "use strict";

  var LOW_LEVERAGE = 0.5;

  var data = { moments: [], players: [], meta: {} };
  var filters = {
    session: null,        // number, or null for the whole season
    results: new Set(),   // "hitting" / "pitching"
    hrOnly: false,
    league: "",           // "" | "GL" | "LL"
    team: "",
    player: "",
    rookiesOnly: false,
    favoritesOnly: false,
    sort: "chrono",
  };

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

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function formatMomentTime(iso, raw) {
    if (!iso) return raw || "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return raw || "";
    var h = d.getHours();
    var ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    var mins = String(d.getMinutes()).padStart(2, "0");
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + h + ":" + mins + " " + ampm;
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
    return "Data as of " + formatMomentTime(iso, iso) + " (" + ago + ")";
  }

  function toast(msg) {
    var el = $("toast");
    el.textContent = msg;
    el.hidden = false;
    window.clearTimeout(el._timer);
    el._timer = window.setTimeout(function () { el.hidden = true; }, 3500);
  }

  // ── filtering / sorting ─────────────────────────────────────────────────────

  function matches(m) {
    if (filters.session !== null && m.session_number !== filters.session) return false;
    if (filters.hrOnly && m.result !== "HR") return false;
    if (filters.results.size && !filters.results.has(m.result_category)) return false;
    if (filters.league && (m.sub_leagues || []).indexOf(filters.league) === -1) return false;
    if (filters.team && m.off_team_abbr !== filters.team && m.def_team_abbr !== filters.team) return false;
    if (filters.player) {
      var needle = filters.player.toLowerCase();
      var hay = [m.batter_name, m.pitcher_name, m.runner_name, m.featured_name]
        .join(" ").toLowerCase();
      if (hay.indexOf(needle) === -1) return false;
    }
    if (filters.rookiesOnly && !m.rookie) return false;
    if (filters.favoritesOnly) {
      var fav = window.KMFavorites;
      if (!fav) return false;
      if (!fav.has(m.batter_id) && !fav.has(m.pitcher_id) && !fav.has(m.runner_id)) return false;
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

  function levClass(leverage) {
    if (leverage == null) return "neutral";
    var high = data.meta.leverage_threshold || 2.0;
    if (leverage >= high) return "high";
    if (leverage <= LOW_LEVERAGE) return "low";
    return "neutral";
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

  function diffPill(m) {
    if (m.diff === 0) return '<span class="diff-pill zero">0 Diff</span>';
    if (m.diff === 500) return '<span class="diff-pill five">500 Diff</span>';
    return "";
  }

  function card(m) {
    var isFav = window.KMFavorites && m.featured_id && window.KMFavorites.has(m.featured_id);
    var star = m.featured_id
      ? '<button type="button" class="star-btn ' + (isFav ? "on" : "") +
        '" data-fav-id="' + m.featured_id + '" title="Favorite this player">' +
        (isFav ? "★" : "☆") + "</button>"
      : "";
    var levText = m.leverage != null
      ? "<span>Leverage " + m.leverage.toFixed(1) + "</span>"
      : "<span>" + m.outs_after + " out" + (m.outs_after === 1 ? "" : "s") + "</span>";
    var why = (m.tag_labels || []).map(function (t) {
      return '<span class="why-tag">' + escapeHtml(t) + "</span>";
    }).join("");

    return '<div class="moment">' +
      '<div class="lev-bar ' + levClass(m.leverage) + '"></div>' +
      '<div class="moment-left">' +
        '<div class="timestamp">' + escapeHtml(formatMomentTime(m.timestamp, m.timestamp_raw)) + "</div>" +
        '<div class="play-line">' + star +
          '<span class="player-name">' + escapeHtml(m.featured_name) + "</span>" +
          '<span class="result-pill ' + (m.result_category === "hitting" ? "offense" : "defense") + '">' +
            escapeHtml(m.result_label) + "</span>" +
          diffPill(m) +
        "</div>" +
        '<div class="meta-line">' + wpFragment(m) + levText + "</div>" +
        '<div class="why-line">' + why + "</div>" +
      "</div>" +
      '<div class="moment-right">' +
        '<div class="inning-indicator">' +
          '<div class="tri ' + (m.half === "top" ? "up" : "down") + '"></div>' +
          '<div class="inning-num">' + m.inning + "</div>" +
        "</div>" +
        scoreBlock(m) +
        m.bases_svg +
      "</div>" +
    "</div>";
  }

  function render() {
    var rows = sorted(data.moments.filter(matches));
    $("moments").innerHTML = rows.map(card).join("");
    $("empty-state").hidden = rows.length > 0;
    $("count-text").textContent = rows.length + (rows.length === 1 ? " key moment" : " key moments");
    $("scope-label").textContent = filters.session === null
      ? "Season " + (data.meta.season || "")
      : "Session " + String(filters.session).padStart(2, "0");
  }

  // ── controls ────────────────────────────────────────────────────────────────

  function populateSessionSelect(keepSelection) {
    var sel = $("session-select");
    var sessions = data.meta.sessions || [];
    sel.innerHTML = '<option value="">Full season</option>' +
      sessions.map(function (s) {
        return '<option value="' + s + '">Session ' + String(s).padStart(2, "0") + "</option>";
      }).join("");
    if (keepSelection && sessions.indexOf(filters.session) !== -1) {
      sel.value = String(filters.session);
    } else if (keepSelection && filters.session === null) {
      sel.value = "";
    } else if (sessions.length) {
      filters.session = sessions[0];
      sel.value = String(sessions[0]);
    }
  }

  function populateTeamSelect() {
    var previous = $("team-select").value;
    var byAbbr = {};
    data.moments.forEach(function (m) {
      if (m.off_team_abbr) byAbbr[m.off_team_abbr] = m.off_team || m.off_team_abbr;
      if (m.def_team_abbr) byAbbr[m.def_team_abbr] = m.def_team || m.def_team_abbr;
    });
    var abbrs = Object.keys(byAbbr).sort(function (a, b) {
      return byAbbr[a].localeCompare(byAbbr[b]);
    });
    $("team-select").innerHTML = '<option value="">All teams</option>' +
      abbrs.map(function (a) {
        return '<option value="' + escapeHtml(a) + '">' + escapeHtml(byAbbr[a]) + "</option>";
      }).join("");
    if (previous && abbrs.indexOf(previous) !== -1) {
      $("team-select").value = previous;
    } else {
      filters.team = "";
    }
  }

  function wireControls() {
    $("session-select").addEventListener("change", function (e) {
      filters.session = e.target.value === "" ? null : Number(e.target.value);
      render();
    });

    $("result-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var kind = chip.getAttribute("data-result");
      if (kind === "hr") {
        filters.hrOnly = !filters.hrOnly;
      } else if (filters.results.has(kind)) {
        filters.results.delete(kind);
      } else {
        filters.results.add(kind);
      }
      chip.classList.toggle("active");
      render();
    });

    $("league-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      filters.league = chip.getAttribute("data-league");
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", c === chip);
      });
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
      window.clearTimeout(playerTimer);
      playerTimer = window.setTimeout(function () {
        filters.player = v.trim();
        render();
      }, 150);
    });

    $("rookies-only").addEventListener("change", function (e) {
      filters.rookiesOnly = e.target.checked;
      render();
    });

    $("favorites-only").addEventListener("change", function (e) {
      filters.favoritesOnly = e.target.checked;
      render();
    });

    $("reset-btn").addEventListener("click", function () {
      filters.results.clear();
      filters.hrOnly = false;
      filters.league = "";
      filters.team = "";
      filters.player = "";
      filters.rookiesOnly = false;
      filters.favoritesOnly = false;
      Array.prototype.forEach.call(document.querySelectorAll("#result-chips .chip"), function (c) {
        c.classList.remove("active");
      });
      Array.prototype.forEach.call(document.querySelectorAll("#league-chips .chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-league") === "");
      });
      $("team-select").value = "";
      $("player-input").value = "";
      $("rookies-only").checked = false;
      $("favorites-only").checked = false;
      render();
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
      $("built-at").textContent = formatBuiltAt(data.meta.built_at);
      populateSessionSelect(true);
      populateTeamSelect();
      render();
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
    status.textContent = "Requesting a rebuild...";

    fetch(url + "?action=trigger_refresh")
      .then(function (r) { return r.json(); })
      .catch(function () { return null; })
      .then(function (res) {
        if (res && res.error) {
          status.textContent = "Refresh rejected: " + res.error;
          btn.disabled = false;
          return;
        }
        status.textContent = "Rebuilding - the Action run plus the Pages deploy takes a minute or two.";
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
      populateSessionSelect();
      populateTeamSelect();
      render();
      window.KMFavorites.init(data.players, function () {
        render();
        window.KMFavorites.refreshList();
      });
    }).catch(function (err) {
      $("scope-label").textContent = "";
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

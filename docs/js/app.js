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
    catchUpGroups: null, // Catch Me Up: plays new since last visit, grouped by game
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
    selectedGame: null, // game_code, set by clicking a scoreboard tile - display only, not a real filter
    side: "all",        // "all" | "for" | "against" - see sideVerdicts() below
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

  // key_moments_build.py's timestamps are naive Central-time wall-clock
  // strings (no zone in the ISO string it writes) - confirmed source zone
  // is always Central. `new Date(iso)` on a zone-less "T"-separated string
  // would otherwise have the browser silently assume ITS OWN local zone,
  // which is wrong here - reinterpret the digits as America/Chicago first,
  // then the normal Date getters below report back in the viewer's zone.
  var CHICAGO_TZ = "America/Chicago";

  function chicagoOffsetMinutesAt(utcMs) {
    var parts = new Intl.DateTimeFormat("en-US", {
      timeZone: CHICAGO_TZ, timeZoneName: "shortOffset",
    }).formatToParts(new Date(utcMs));
    var tz = parts.find(function (p) { return p.type === "timeZoneName"; });
    var m = tz && /GMT([+-]\d+)/.exec(tz.value);
    return m ? parseInt(m[1], 10) * 60 : -300; // fallback: CDT
  }

  function parseChicagoNaive(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(iso || "");
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3], h = +m[4], mi = +m[5], s = +m[6];
    var guessUTC = Date.UTC(y, mo - 1, d, h, mi, s);
    var offset = chicagoOffsetMinutesAt(guessUTC);
    var correctedUTC = guessUTC - offset * 60000;
    // Re-check from the corrected instant in case the first guess (off by
    // Chicago's ~5-6hr offset) landed on the wrong side of a DST transition -
    // only possible for events within a few hours of the transition itself.
    var offset2 = chicagoOffsetMinutesAt(correctedUTC);
    if (offset2 !== offset) correctedUTC = guessUTC - offset2 * 60000;
    return new Date(correctedUTC);
  }

  function viewerZoneAbbr(d) {
    var parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d);
    var tz = parts.find(function (p) { return p.type === "timeZoneName"; });
    return tz ? tz.value : "";
  }

  function formatMomentTime(iso) {
    if (!iso) return "";
    var d = parseChicagoNaive(iso);
    if (!d || isNaN(d.getTime())) return iso;
    var h = d.getHours();
    var ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    var zone = viewerZoneAbbr(d);
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + h + ":" + pad2(d.getMinutes()) + " " + ampm +
      (zone ? " " + zone : "");
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

  function isRookieId(id) {
    if (!rookieIds) {
      rookieIds = new Set();
      (data.players || []).forEach(function (p) {
        if (p.rookie) rookieIds.add(p.id);
      });
    }
    return rookieIds.has(id);
  }

  function isRookie(m) {
    // Same reasoning as isFavorited above - only featured_id/counterpart_id
    // are ever shown on a card, so those are the only two who can make a
    // play a "rookie" moment.
    return isRookieId(m.featured_id) || isRookieId(m.counterpart_id);
  }

  /* Whichever team isn't the featured player's team - always the other one
     of off/def, since a play's two teams are never the same team. */
  function counterpartTeamAbbr(m) {
    return m.off_team_abbr === m.featured_team_abbr ? m.def_team_abbr : m.off_team_abbr;
  }

  function sideVerdict(featuredMatch, counterpartMatch) {
    if (featuredMatch && counterpartMatch) return "both";
    if (featuredMatch) return "featured";
    if (counterpartMatch) return "counterpart";
    return null;
  }

  /* One verdict per currently-active side-sensitive filter (Team, Player,
     League, Rookies, Favorites) for this play: "featured", "counterpart",
     "both" (the criterion is independently true on both sides - e.g. a
     rookie-vs-rookie matchup), or null (shouldn't happen - by the time this
     runs, m has already passed every one of these as a plain AND filter in
     matches(), so each active one here is already known to match somewhere).
     Empty array means no side-sensitive filter is active, which is exactly
     when the For/Against chips are disabled in the UI. */
  function sideVerdicts(m) {
    var out = [];
    if (filters.team) {
      out.push(sideVerdict(m.featured_team_abbr === filters.team, counterpartTeamAbbr(m) === filters.team));
    }
    if (filters.playerId) {
      out.push(sideVerdict(m.featured_id === filters.playerId, m.counterpart_id === filters.playerId));
    } else if (filters.player) {
      var needle = filters.player.toLowerCase();
      out.push(sideVerdict(
        (m.featured_name || "").toLowerCase().indexOf(needle) !== -1,
        (m.counterpart_name || "").toLowerCase().indexOf(needle) !== -1
      ));
    }
    if (filters.league) {
      var teams = data.meta.teams || {};
      var featLeague = (teams[m.featured_team_abbr] || {}).sub_league;
      var cpLeague = (teams[counterpartTeamAbbr(m)] || {}).sub_league;
      out.push(sideVerdict(featLeague === filters.league, cpLeague === filters.league));
    }
    if (filters.rookiesOnly) {
      out.push(sideVerdict(isRookieId(m.featured_id), isRookieId(m.counterpart_id)));
    }
    if (filters.favoritesOnly) {
      out.push(sideVerdict(isFavoritedId(m.featured_id), isFavoritedId(m.counterpart_id)));
    }
    return out;
  }

  /* For/Against requires every currently-active side-sensitive filter to
     agree on the same side (a "both" verdict always counts as agreeing,
     since a two-sided play - e.g. two favorited players in the same play -
     is relevant no matter which way you're looking at it). A play where
     active filters point at different sides shows under neither For nor
     Against, only under All - see the conversation that settled this. */
  function sideMatches(m) {
    if (filters.side === "all") return true;
    var verdicts = sideVerdicts(m);
    if (!verdicts.length) return true;   // no side-sensitive filter active
    var wanted = filters.side === "for" ? "featured" : "counterpart";
    return verdicts.every(function (v) { return v === wanted || v === "both"; });
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
    if (!sideMatches(m)) return false;
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

  function teamLogoUrl(abbr) {
    return ((data.meta.teams || {})[abbr] || {}).logo_url || "";
  }

  function teamLogoImg(abbr, cls) {
    var url = teamLogoUrl(abbr);
    if (!url) return "";
    return '<img class="' + cls + '" src="' + escapeHtml(url) + '" alt="" loading="lazy">';
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
     and FINAL is the more informative badge. finalLabel lets the scoreboard
     (which drops the inning-indicator once a game is final - see
     scoreboardCard) fold the inning count into the badge itself when the
     game didn't end after the expected number of innings; play cards keep
     showing the inning-indicator alongside FINAL, so they never pass one. */
  function stateStack(m, finalLabel) {
    if (m.is_game_final) {
      return '<div class="state-stack"><div class="state-badge final">' +
        escapeHtml(finalLabel || "FINAL") + "</div></div>";
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
    var inlineLogo = teamLogoImg(m.featured_team_abbr, "team-logo-inline-img");

    var levBarHex = teamColor(m.featured_team_abbr);

    return '<div class="moment">' +
      '<div class="corner-actions">' + gameLink + "</div>" +
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
        (inlineLogo ? '<span class="team-logo-inline">' + inlineLogo + '</span>' : "") +
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

  // ── scoreboard ──────────────────────────────────────────────────────────────

  var SCOREBOARD_HOT_LEVERAGE = 1.5;

  function leverageClass(lev) {
    return lev >= SCOREBOARD_HOT_LEVERAGE ? " hot" : "";
  }

  /* One tile per game in the selected session, sorted server-side by
     leverage (finished games forced to the back - see key_moments_build.py).
     Reuses stateStack()'s diamond/outs/FINAL badge since a scoreboard game
     object carries the same half/inning/outs_after/obc_after/is_game_final
     shape as a moment. Independent of every play filter below it - clicking
     a tile only ever touches the Team filter (see wireControls).

     The leverage badge sits in its own reserved header row rather than
     floating absolutely over the score column - the old absolute layout
     only cleared the score at the narrow tile width it was tuned against and
     started overlapping once tiles got wider from the column-balancing pass
     below. */
  function scoreboardCard(g) {
    var awayBatting = g.half === "top";
    var awayPct = g.away_win_prob != null ? Math.round(g.away_win_prob * 100) : 50;
    var homePct = 100 - awayPct;
    var awayHex = teamColor(g.away_team_abbr) || "#9aa4b2";
    var homeHex = teamColor(g.home_team_abbr) || "#c7ccd3";
    var levBadge = g.is_game_final ? "" :
      '<span class="sb-lev' + leverageClass(g.leverage) + '">LI ' + g.leverage.toFixed(1) + "</span>";
    var replayBtn = '<button type="button" class="tile-replay-btn" data-replay="' +
      escapeHtml(g.game_code) + '" title="Replay this game" aria-label="Replay ' +
      escapeHtml(g.away_team_abbr) + " at " + escapeHtml(g.home_team_abbr) + '">' +
      '<svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="8 5 19 12 8 19 8 5"></polygon></svg>' +
      "</button>";
    var selected = filters.selectedGame === g.game_code ? " selected" : "";

    // Once final, the inning-indicator is redundant with the FINAL badge -
    // drop it, and fold the inning count into the badge itself (only) when
    // the game didn't end after the expected number of innings.
    var expectedInnings = data.meta.innings || 6;
    var inningIndicator = g.is_game_final ? "" :
      '<div class="inning-indicator">' +
        '<div class="tri ' + (g.half === "top" ? "up" : "down") + '"></div>' +
        '<div class="inning-num">' + g.inning + "</div>" +
      "</div>";
    var finalLabel = g.is_game_final && g.inning !== expectedInnings ? "FINAL/" + g.inning : "FINAL";

    // A div with role=button, not a <button>: the replay control below is a
    // real nested <button>, and the HTML content model forbids a button inside
    // a button (browsers auto-close the outer one and the markup falls apart).
    // Keyboard activation is wired explicitly in the #scoreboard keydown
    // handler to replace what the native button gave us for free.
    return '<div role="button" tabindex="0" class="scoreboard-tile' + selected +
      '" data-game="' + escapeHtml(g.game_code) +
      '" data-away="' + escapeHtml(g.away_team_abbr) +
      '" data-home="' + escapeHtml(g.home_team_abbr) +
      '" data-session="' + escapeHtml(String(filters.session == null ? "" : filters.session)) +
      '" aria-pressed="' + (selected ? "true" : "false") + '">' +
      '<div class="sb-body">' +
        '<div class="sb-teams">' +
          '<div class="sb-row' + (awayBatting ? " batting" : "") + '">' +
            teamLogoImg(g.away_team_abbr, "sb-logo") +
            '<span class="sb-abbr">' + escapeHtml(g.away_team_abbr) + "</span>" +
            '<span class="sb-score">' + g.away_score + "</span>" +
          "</div>" +
          '<div class="sb-row' + (!awayBatting ? " batting" : "") + '">' +
            teamLogoImg(g.home_team_abbr, "sb-logo") +
            '<span class="sb-abbr">' + escapeHtml(g.home_team_abbr) + "</span>" +
            '<span class="sb-score">' + g.home_score + "</span>" +
          "</div>" +
        "</div>" +
        '<div class="sb-state">' +
          inningIndicator +
          stateStack(g, finalLabel) +
        "</div>" +
      "</div>" +
      '<div class="sb-foot">' +
        '<div class="wp-bar">' +
          '<div class="wp-seg" style="width:' + awayPct + '%;background:' + awayHex + '"></div>' +
          '<div class="wp-seg" style="width:' + homePct + '%;background:' + homeHex + '"></div>' +
        "</div>" +
        // Leverage pill and replay control ride together above the bar, so the
        // replay button stops colliding with the scorebug in the tile's
        // top-right. A finished game drops the pill and the row centres on the
        // button alone.
        '<div class="sb-actions">' + levBadge + replayBtn + "</div>" +
      "</div>" +
    "</div>";
  }

  function deselectScoreboardTile() {
    filters.selectedGame = null;
    Array.prototype.forEach.call(document.querySelectorAll(".scoreboard-tile"), function (t) {
      t.classList.remove("selected");
      t.setAttribute("aria-pressed", "false");
    });
  }

  /* Select (or re-click to clear) a game tile. Extracted from the click
     handler so the keyboard path activates exactly the same behaviour rather
     than a second copy of it. */
  function selectScoreboardTile(tile) {
    var game = tile.getAttribute("data-game");
    if (filters.selectedGame === game) {
      filters.team = "";
      filters.selectedGame = null;
      $("team-select").value = "";
      tile.classList.remove("selected");
      tile.setAttribute("aria-pressed", "false");
      render();
      return;
    }
    var away = tile.getAttribute("data-away");
    // Either team in the matchup would do - Team is a season-long filter, but
    // the scoreboard only ever shows the selected session's games, so this
    // reads as "just this game" in practice.
    filters.team = away;
    filters.selectedGame = game;
    $("team-select").value = away;
    Array.prototype.forEach.call(document.querySelectorAll(".scoreboard-tile"), function (t) {
      var on = t === tile;
      t.classList.toggle("selected", on);
      t.setAttribute("aria-pressed", String(on));
    });
    render();
  }

  var SCOREBOARD_TILE_MIN = 176;
  var SCOREBOARD_GAP = 10;
  var SCOREBOARD_MOBILE_MAX_COLS = 2;
  var SCOREBOARD_MOBILE_BREAKPOINT = 600;

  /* Balances the grid so a row never has noticeably more tiles than the
     next - e.g. 8 tiles at a natural fit of 6-per-row becomes two rows of 4
     instead of 6-then-2. maxCols is however many tiles the row can fit at
     the tile's minimum width (or a hard cap of 2 on phones); rows is the
     fewest rows that fit within that cap, and cols redistributes the tiles
     evenly across exactly that many rows. */
  function applyScoreboardColumns() {
    var row = document.querySelector("#scoreboard .scoreboard-row");
    if (!row) return;
    var n = row.children.length;
    if (!n) return;
    var maxCols;
    if (window.innerWidth <= SCOREBOARD_MOBILE_BREAKPOINT) {
      maxCols = SCOREBOARD_MOBILE_MAX_COLS;
    } else {
      maxCols = Math.max(1, Math.floor((row.clientWidth + SCOREBOARD_GAP) / (SCOREBOARD_TILE_MIN + SCOREBOARD_GAP)));
    }
    maxCols = Math.min(maxCols, n);
    var rows = Math.ceil(n / maxCols);
    var cols = Math.ceil(n / rows);
    row.style.gridTemplateColumns = "repeat(" + cols + ", 1fr)";
  }

  var scoreboardResizeTimer;
  function scheduleScoreboardResize() {
    window.clearTimeout(scoreboardResizeTimer);
    scoreboardResizeTimer = window.setTimeout(applyScoreboardColumns, 150);
  }

  /* Scoreboard tracks the session selector rather than "whatever session
     the build considered current" - it goes with whichever slate of games
     the user is actually looking at, and hides entirely for "Full season"
     (filters.session === null) since there's no single slate to show. */
  function renderScoreboard() {
    var el = $("scoreboard");
    var label = $("scoreboard-label");
    var games = filters.session === null
      ? []
      : ((data.meta.games || {})[String(filters.session)] || []);
    if (!games.length) {
      el.hidden = true;
      el.innerHTML = "";
      label.textContent = "";
      return;
    }
    el.hidden = false;
    label.textContent = "Scoreboard";
    el.innerHTML = '<div class="scoreboard-row">' + games.map(scoreboardCard).join("") + "</div>";
    applyScoreboardColumns();
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
    if (filters.side !== "all") n += 1;
    return n + filters.tags.size;
  }

  function updateFilterSummary() {
    var n = activeFilterCount();
    $("filters-toggle-label").textContent = n ? "Filters (" + n + " active)" : "Filters";
  }

  /* For/Against only means anything once one of the five side-sensitive
     filters is active (see sideVerdicts) - otherwise there's no "side" to
     be for or against. Disabled rather than hidden, so its presence hints
     at the feature even when it's currently a no-op, and forced back to
     "all" so a play never silently vanishes because a stale for/against
     pick outlived the filter that made it meaningful. */
  function sideFilterActive() {
    return !!(filters.team || filters.playerId || filters.player ||
      filters.league || filters.rookiesOnly || filters.favoritesOnly);
  }

  function updateSideAvailability() {
    var active = sideFilterActive();
    if (!active) filters.side = "all";
    var chips = document.querySelectorAll("#side-chips .chip");
    Array.prototype.forEach.call(chips, function (c) {
      c.disabled = !active;
      c.classList.toggle("active", c.getAttribute("data-side") === filters.side);
    });
  }

  function render() {
    updateFilterSummary();
    updateSideAvailability();
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

  // ── Catch Me Up: what's new since this name last had the page open ──────────

  /* Load every session's plays, not just the active filter's. "No cap" means
     a returning visitor's backlog can span any number of sessions, so guessing
     which ones are relevant would be a correctness risk for a handful of small
     JSON fetches. If a many-season backlog ever makes this slow, the fix is to
     skip sessions older than the cursor's date - not needed yet. */
  function loadAllSessions() {
    var sessions = (data.meta.sessions || []).slice();
    return Promise.all(sessions.map(function (s) {
      if (data.playsBySession[s]) return Promise.resolve(data.playsBySession[s]);
      return getJSON("data/plays_" + pad2(s) + ".json").then(function (rows) {
        data.playsBySession[s] = rows;
        return rows;
      });
    }));
  }

  /* Group plays into games: groups ordered by each game's earliest play,
     plays within a group by play_num. Team abbreviations ride on every play
     row already, so a title slide needs no extra lookup. */
  function groupByGame(plays) {
    var byGame = {};
    plays.forEach(function (p) {
      var g = byGame[p.game_code];
      if (!g) {
        g = byGame[p.game_code] = {
          game_code: p.game_code,
          away_team_abbr: p.away_team_abbr,
          home_team_abbr: p.home_team_abbr,
          session_number: p.session_number,
          first_ts: p.timestamp || "",
          plays: [],
        };
      }
      g.plays.push(p);
      if (p.timestamp && (!g.first_ts || p.timestamp < g.first_ts)) g.first_ts = p.timestamp;
    });
    return Object.keys(byGame).map(function (k) { return byGame[k]; })
      .sort(function (a, b) { return a.first_ts < b.first_ts ? -1 : (a.first_ts > b.first_ts ? 1 : 0); })
      .map(function (g) {
        g.plays.sort(function (a, b) { return a.play_num - b.play_num; });
        return g;
      });
  }

  /* Read the OLD cursor, build the new-since set from it, THEN write the new
     cursor. That order is the whole contract: the value used to decide what is
     new must never be the value just written. The cursor advances on page load
     regardless of whether the slideshow is ever opened, so closing it early -
     or never opening it - loses nothing that browsing the page normally
     wouldn't have. */
  function computeCatchUp() {
    var fav = window.KMFavorites;
    if (!fav || !fav.hasName()) return Promise.resolve([]);
    var cursor = fav.lastSeen();
    if (!cursor) {
      /* First time this name has been seen: start tracking from now rather
         than replaying the whole season at someone who just typed a name.

         built_at is passed here too, not just in the branch below: a
         first-time visitor arriving during a stale-data window has exactly the
         same gap risk as a returning one, and capping in only one of the two
         places would leave that hole open. */
      fav.markSeenNow(data.meta.built_at);
      return Promise.resolve([]);
    }
    return loadAllSessions().then(function (bySession) {
      // Both sides are naive Central "YYYY-MM-DDTHH:mm:ss" strings in the same
      // source zone (see favorites.js's centralNowIso), so lexicographic order
      // is chronological order and no timezone math is needed here.
      var newPlays = [].concat.apply([], bySession).filter(function (p) {
        return p.timestamp && p.timestamp > cursor;
      });
      // Capped at the build's freshness, not wall-clock now - these plays came
      // out of THIS build, so that is as far as the visitor has actually seen.
      fav.markSeenNow(data.meta.built_at);
      return groupByGame(newPlays);
    }).catch(function () {
      return [];
    });
  }

  /* Every play of one game, in order. Reads the same per-session cache
     loadAllSessions()/ensurePlaysLoaded() fill, so by the time a slideshow is
     open this is already in memory. */
  function gamePlaysFor(session, gameCode) {
    var rows = data.playsBySession[session] || [];
    return rows.filter(function (p) { return p.game_code === gameCode; })
      .sort(function (a, b) { return a.play_num - b.play_num; });
  }

  function catchUpPlayCount(groups) {
    return (groups || []).reduce(function (n, g) { return n + g.plays.length; }, 0);
  }

  /* Banner states, in the order they are checked:
       no name        - quiet prompt, opens the Favorites modal (that modal
                        already leads with the name input, so there is no
                        second name-prompt UI to build)
       nothing new    - hidden entirely, rather than a standing "all caught up"
                        line that would just be noise on every visit
       something new  - prominent, with the count */
  function renderCatchUpBanner() {
    var el = $("catchup-banner");
    if (!el) return;
    var fav = window.KMFavorites;
    if (fav && !fav.hasName()) {
      el.hidden = false;
      el.classList.add("quiet");
      el.textContent = "Catch Me Up - add your name to track what's new";
      return;
    }
    var count = catchUpPlayCount(data.catchUpGroups);
    if (!count) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.classList.remove("quiet");
    el.textContent = "▶ Catch Me Up (" + count + (count === 1 ? " new play)" : " new plays)");
  }

  // ── Catch Me Up: the slideshow ──────────────────────────────────────────────

  var TITLE_DWELL_MS = 1800;

  var catchUp = {
    slides: [],
    index: 0,
    timer: null,
    paused: false,
    startedAt: 0,
    remaining: 0,
  };

  function buildCatchUpSlides(groups) {
    var slides = [];
    var playNo = 0;
    var total = catchUpPlayCount(groups);
    var gameTotal = (groups || []).length;
    (groups || []).forEach(function (g, gi) {
      slides.push({ kind: "title", group: g, gameNo: gi + 1, gameTotal: gameTotal });
      // The ribbon plots the WHOLE game, not just the new plays, so the shape
      // of what came before is visible behind what's new.
      var gamePlays = gamePlaysFor(g.session_number, g.game_code);
      var byNum = {};
      gamePlays.forEach(function (p, i) { byNum[p.play_num] = i; });
      var ribbonFrom = g.plays.length ? (byNum[g.plays[0].play_num] || 0) : 0;
      g.plays.forEach(function (p) {
        playNo += 1;
        slides.push({
          kind: "play", play: p, group: g, playNo: playNo, total: total,
          gamePlays: gamePlays,
          gameIdx: byNum[p.play_num] == null ? -1 : byNum[p.play_num],
          ribbonFrom: ribbonFrom,
          homeAbbr: g.home_team_abbr,
          awayAbbr: g.away_team_abbr,
        });
      });
    });
    slides.push({ kind: "done", total: total });
    return slides;
  }

  function teamName(abbr) {
    var t = (data.meta.teams || {})[abbr];
    return (t && t.name) || abbr || "";
  }

  // ── Play Scene ──────────────────────────────────────────────────────────────
  // The slideshow's play slide. Composes an animated diamond, a leverage
  // meter, a win-probability ribbon and a detail strip around the same data
  // card(m) already uses. card(m) itself is untouched - the main feed keeps
  // rendering exactly as it does today; this is Catch Me Up / Game Replay only.
  //
  // Every animation here is a pure CSS @keyframes driven by custom properties
  // set inline, deliberately: the slide's innerHTML is replaced wholesale on
  // every advance, so fresh elements restart their own animations with no
  // rAF dance, no timers to leak, and one place (the reduced-motion block in
  // style.css) that can switch all of them off at once.

  var SCENE_BASES = {
    HOME: { x: 100, y: 170 },
    "1B": { x: 158, y: 110 },
    "2B": { x: 100, y: 50 },
    "3B": { x: 42, y: 110 },
  };

  // Running order around the diamond. Index doubles as "how far around" a
  // runner is, with 4 meaning they came all the way back to score.
  var BASE_PATH = ["HOME", "1B", "2B", "3B"];
  var BASE_ORDINAL = { HOME: 0, "1B": 1, "2B": 2, "3B": 3 };

  /* Every base a runner actually touches getting from one place to another.
     Runners follow the basepaths - a triple is home to 1st to 2nd to 3rd, not
     a diagonal across the infield - so a move is a sequence of legs, one per
     base passed, rather than a single straight hop.

     All four legs of this diamond are the same length, so evenly spaced
     keyframe stops give constant speed the whole way round. */
  function basepathWaypoints(fromKey, toKey, scored) {
    var start = fromKey === "BATTER" ? 0 : BASE_ORDINAL[fromKey];
    var end = scored ? 4 : BASE_ORDINAL[toKey];
    if (start == null || end == null || end <= start) return [];
    var pts = [];
    for (var o = start + 1; o <= end; o++) {
      pts.push(o === 4 ? SCENE_BASES.HOME : SCENE_BASES[BASE_PATH[o]]);
    }
    return pts;
  }

  // Longer trips run a little faster rather than strictly proportionally, so
  // even a home run's four legs finish inside the shortest play dwell (2000ms).
  var RUN_LEG_MS = [0, 800, 1150, 1450, 1700];

  /* Pair up who was on base before a play with where runners ended after it.
     Runners cannot pass each other, so listing both sides most-advanced-first
     and zipping them is physically valid for hits, walks, home runs, sac
     flies and ordinary outs.

     Known limitation, deliberate: on tangled force plays and fielder's
     choices the runner actually removed is not always the furthest back, and
     obc_before/obc_after/runs alone do not uniquely determine which runner
     did what. The token that animates can therefore be plausible-but-unverified
     on those plays. Shipping the heuristic is the explicit v1 choice; the
     exact assignment would have to come from key_moments_build.py, which
     already knows it, if this ever reads wrong often enough to matter. */
  function deriveRunnerMoves(obcBefore, obcAfter, runs) {
    var before = [];
    if (obcBefore[0] === "1") before.push("3B");
    if (obcBefore[1] === "1") before.push("2B");
    if (obcBefore[2] === "1") before.push("1B");

    var after = [];
    for (var i = 0; i < runs; i++) after.push("HOME");
    if (obcAfter[0] === "1") after.push("3B");
    if (obcAfter[1] === "1") after.push("2B");
    if (obcAfter[2] === "1") after.push("1B");

    var moves = [];
    var n = Math.min(before.length, after.length);
    for (var j = 0; j < n; j++) {
      moves.push({ from: before[j], to: after[j], scored: after[j] === "HOME" });
    }
    for (var k = n; k < before.length; k++) {
      moves.push({ from: before[k], to: "OUT", scored: false });
    }
    if (after.length > n) {
      moves.push({ from: "BATTER", to: after[n], scored: after[n] === "HOME" });
    }
    return moves;
  }

  function sceneDiamondHtml(m) {
    var before = String(m.obc_before || "000");
    var after = String(m.obc_after || "000");
    var moves = deriveRunnerMoves(before, after, m.runs || 0);

    /* Two nested groups per token, deliberately: the outer one owns position
       (the multi-leg basepath run) and the inner one owns opacity and scale
       (fading out, the batter appearing, the flash on scoring). Both would
       otherwise be competing to animate `transform` on one element, and only
       one of them could win. */
    var tokens = moves.map(function (mv) {
      var from = mv.from === "BATTER" ? SCENE_BASES.HOME : SCENE_BASES[mv.from];
      var isOut = mv.to === "OUT";
      if (!from || (!isOut && !SCENE_BASES[mv.to] && !mv.scored)) return "";
      var path = isOut ? [] : basepathWaypoints(mv.from, mv.to, mv.scored);
      var end = path.length ? path[path.length - 1] : from;
      var legs = Math.min(path.length, RUN_LEG_MS.length - 1);
      var vars = "--fx:" + from.x + "px;--fy:" + from.y + "px;" +
                 "--tx:" + end.x + "px;--ty:" + end.y + "px;";
      path.forEach(function (p, i) {
        vars += "--p" + (i + 1) + "x:" + p.x + "px;--p" + (i + 1) + "y:" + p.y + "px;";
      });
      vars += "--dur:" + (RUN_LEG_MS[legs] || 0) + "ms";
      var cls = "rn" + (legs ? " legs" + legs : "") + (isOut ? " out" : "") +
                (mv.scored ? " score" : "") + (mv.from === "BATTER" ? " batter" : "");
      return '<g class="' + cls + '" style="' + vars + '">' +
        '<g class="rn-inner"><circle r="9"></circle></g></g>';
    }).join("");

    // deriveRunnerMoves only tracks RUNNERS, so a play where the batter never
    // reached base (a strikeout with nobody on, most obviously) yields no
    // tokens at all and the diamond sits empty with nothing having happened.
    // Give the batter their own token in that case, fading at the plate.
    var batterReached = moves.some(function (mv) { return mv.from === "BATTER"; });
    if (!batterReached) {
      var h = SCENE_BASES.HOME;
      tokens += '<g class="rn batter-out" style="' +
        "--fx:" + h.x + "px;--fy:" + h.y + "px;--tx:" + h.x + "px;--ty:" + h.y + "px" + '">' +
        '<g class="rn-inner"><circle r="9"></circle></g></g>';
    }

    // Base plates show post-play occupancy so the diamond still reads
    // correctly once the tokens have settled.
    var plates = ["3B", "2B", "1B"].map(function (b, i) {
      var occupied = after[i] === "1";
      var p = SCENE_BASES[b];
      return '<rect class="dm-base' + (occupied ? " on" : "") +
        '" x="-7" y="-7" width="14" height="14" rx="2" transform="translate(' +
        p.x + "," + p.y + ') rotate(45)"></rect>';
    }).join("");

    /* The batting team's mark is painted ON the infield, inside the SVG and
       above the field fill - as an HTML layer underneath it, the opaque
       .dm-field simply covered it. It sits below the bases and tokens so the
       runners always stay the thing you look at. */
    var batAbbr = m.batting_is_home ? m.home_team_abbr : m.away_team_abbr;
    var markUrl = teamLogoUrl(batAbbr);
    var watermark = markUrl
      ? '<image class="dm-mark" href="' + escapeHtml(markUrl) + '" x="52" y="62" ' +
        'width="96" height="96" preserveAspectRatio="xMidYMid meet"></image>'
      : "";
    // Runners wear the batting team's colour - they are that team's runners.
    // Scoring and out tokens override it, since those states matter more than
    // whose they are.
    var runHex = teamColor(batAbbr);
    return '<div class="scene-diamond-wrap">' +
      '<svg class="scene-diamond" viewBox="0 0 200 200" aria-hidden="true"' +
        (runHex ? ' style="--rn-fill:' + escapeHtml(runHex) + '"' : "") + ">" +
        '<path class="dm-field" d="M100,170 L158,110 L100,50 L42,110 Z"></path>' +
        watermark +
        plates +
        // Point down, toward the backstop - a home plate seen from above has
        // its flat edge facing the pitcher, not its apex.
        '<path class="dm-plate" d="M94,162 L106,162 L106,170 L100,176 L94,170 Z"></path>' +
        tokens +
      "</svg>" +
    "</div>";
  }

  /* LI 1.0 sits at the apex of the gauge. That is average leverage by
     definition - the index divides by the average WP swing - so the top of the
     arc reading "ordinary" makes the needle's side of centre meaningful on its
     own. The scale is piecewise: 0 to 1.0 fills the left half, 1.0 up to the
     ceiling fills the right. In the current data that puts about 80% of plays
     on the left half and gives the remaining 20% the whole right half to
     spread across (observed max is 3.8, median 0.56). */
  var SCENE_LEV_ANCHOR = 1.0;
  var SCENE_LEV_CEILING = 4;
  var SCENE_ARC_LEN = Math.PI * 46;   // matches the r=46 arc path below

  function meterFraction(lev) {
    if (lev <= 0) return 0;
    if (lev <= SCENE_LEV_ANCHOR) return 0.5 * (lev / SCENE_LEV_ANCHOR);
    var over = (lev - SCENE_LEV_ANCHOR) / (SCENE_LEV_CEILING - SCENE_LEV_ANCHOR);
    return 0.5 + 0.5 * Math.min(1, over);
  }

  function sceneMeterHtml(m) {
    if (m.leverage == null) return "";
    var lev = m.leverage;
    var frac = meterFraction(lev);
    // Same threshold and the same hot/cold rule the scoreboard tile uses, so a
    // play that redlines here is exactly one that shows hot there.
    var threshold = data.meta.leverage_threshold || SCOREBOARD_HOT_LEVERAGE;
    var hot = leverageClass(lev) === " hot";
    var tFrac = meterFraction(threshold);
    var tickAt = function (f, r1, r2) {
      var a = Math.PI * (1 - f);
      return { x1: 60 + Math.cos(a) * r1, y1: 60 - Math.sin(a) * r1,
               x2: 60 + Math.cos(a) * r2, y2: 60 - Math.sin(a) * r2 };
    };
    var tick = tickAt(tFrac, 36, 56);
    var anchor = tickAt(0.5, 38, 54);   // the apex: LI 1.0, average leverage
    return '<div class="scene-meter' + (hot ? " hot" : "") + '">' +
      '<svg viewBox="0 0 120 72" aria-hidden="true">' +
        '<path class="mt-track" d="M14,60 A46,46 0 0 1 106,60"></path>' +
        '<path class="mt-redline" d="M14,60 A46,46 0 0 1 106,60" style="' +
          "--len:" + SCENE_ARC_LEN.toFixed(2) + "px;--off:" +
          (SCENE_ARC_LEN * tFrac).toFixed(2) + 'px"></path>' +
        '<line class="mt-anchor" x1="' + anchor.x1.toFixed(1) + '" y1="' + anchor.y1.toFixed(1) +
          '" x2="' + anchor.x2.toFixed(1) + '" y2="' + anchor.y2.toFixed(1) + '"></line>' +
        '<line class="mt-tick" x1="' + tick.x1.toFixed(1) + '" y1="' + tick.y1.toFixed(1) +
          '" x2="' + tick.x2.toFixed(1) + '" y2="' + tick.y2.toFixed(1) + '"></line>' +
        '<path class="mt-fill" d="M14,60 A46,46 0 0 1 106,60" style="' +
          "--len:" + SCENE_ARC_LEN.toFixed(2) + "px;--off:" +
          (SCENE_ARC_LEN * (1 - frac)).toFixed(2) + 'px"></path>' +
      "</svg>" +
      '<div class="scene-meter-val">' + lev.toFixed(1) + "</div>" +
      '<div class="scene-meter-lbl">LEVERAGE</div>' +
    "</div>";
  }

  /* Win probability from the home team's perspective. The stored value is
     from the batting team's, which flips every half-inning, so it has to be
     normalized before it can be plotted as one continuous line - the same
     conversion utils.compute_game_wp_series does server-side. */
  function homeWpOf(p) {
    if (p.win_prob_after == null) return null;
    return p.batting_is_home ? p.win_prob_after : 1 - p.win_prob_after;
  }

  var RIBBON_W = 300, RIBBON_H = 64, RIBBON_PAD = 5;

  /* Half-inning-pair index: 0 = top 1st, 1 = bottom 1st, 2 = top 2nd... Used
     both by the ribbon's segment grouping and by the dwell/beat logic that
     marks where one half-inning ends and the next begins. */
  function hipOf(p) {
    return (p.inning - 1) * 2 + (p.half === "bottom" ? 1 : 0);
  }

  /* True when this play opens a half-inning - i.e. the play before it in the
     same game was in a different half. The comparison is against the game's
     own play list, not the previous slide, so it stays correct for Catch Me Up
     runs that only show part of a game. */
  function startsHalfInning(slide) {
    if (!slide || slide.kind !== "play") return false;
    var plays = slide.gamePlays || [];
    var i = slide.gameIdx;
    if (i == null || i <= 0 || !plays[i] || !plays[i - 1]) return false;
    return hipOf(plays[i]) !== hipOf(plays[i - 1]);
  }

  function sceneRibbonHtml(slide) {
    var plays = slide.gamePlays || [];
    var upto = slide.gameIdx;
    if (plays.length < 2 || upto == null || upto < 0) return "";

    /* The x axis is a half-inning timeline, not a play index, and it always
       runs the full length of regulation. A live game in the 2nd therefore
       shows the line reaching a fifth of the way across with the rest of the
       game still ahead of it, instead of stretching two innings over the whole
       width and looking complete. Extras push the axis out past regulation.
       Plays are spaced evenly inside whichever half-inning they belong to. */
    var segs = [];
    plays.forEach(function (p, i) {
      var hip = hipOf(p);
      var s = segs[segs.length - 1];
      if (!s || s.hip !== hip) segs.push({ hip: hip, inning: p.inning, half: p.half, a: i, b: i });
      else s.b = i;
    });
    var lastHip = segs.length ? segs[segs.length - 1].hip : 0;
    var totalHalves = Math.max((data.meta.innings || 6) * 2, lastHip + 1);
    var xAt = function (frac) { return (frac / totalHalves) * RIBBON_W; };
    var xByIdx = new Array(plays.length);
    segs.forEach(function (s) {
      var k = s.b - s.a + 1;
      for (var i = s.a; i <= s.b; i++) {
        xByIdx[i] = xAt(s.hip + (i - s.a + 0.5) / k);
      }
    });

    var pts = [];
    for (var i = 0; i <= upto && i < plays.length; i++) {
      var hw = homeWpOf(plays[i]);
      if (hw == null) continue;
      pts.push({
        i: i,
        x: xByIdx[i],
        y: RIBBON_PAD + (1 - hw) * (RIBBON_H - RIBBON_PAD * 2),
      });
    }
    if (!pts.length) return "";

    // Everything before this run started is context the viewer already knows -
    // muted. From there on is what they came to see.
    var from = slide.ribbonFrom || 0;

    /* The line and the shading under it are the same story told twice: whoever
       is ahead owns that stretch, in their colour. Each segment is split at the
       exact point it crosses the 50/50 axis, so the lead changing hands shows
       as the colour changing mid-segment rather than a whole segment being
       coloured for whichever end happens to win a vote. */
    var midY = RIBBON_PAD + 0.5 * (RIBBON_H - RIBBON_PAD * 2);
    var homeHex = teamColor(slide.homeAbbr) || "#4a6fa5";
    var awayHex = teamColor(slide.awayAbbr) || "#9aa4b2";
    var subs = [];
    for (var q = 1; q < pts.length; q++) {
      var pa = pts[q - 1], pb = pts[q];
      var isLast = q === pts.length - 1;
      var seen = pts[q].i <= from;
      var da = pa.y - midY, db = pb.y - midY;
      if ((da < 0 && db > 0) || (da > 0 && db < 0)) {
        var t = da / (da - db);
        var cross = { x: pa.x + (pb.x - pa.x) * t, y: midY };
        subs.push({ a: pa, b: cross, seen: seen, last: isLast });
        subs.push({ a: cross, b: pb, seen: seen, last: isLast });
      } else {
        subs.push({ a: pa, b: pb, seen: seen, last: isLast });
      }
    }
    subs.forEach(function (s) {
      s.hex = ((s.a.y + s.b.y) / 2 <= midY) ? homeHex : awayHex;
      s.len = Math.sqrt(Math.pow(s.b.x - s.a.x, 2) + Math.pow(s.b.y - s.a.y, 2)) || 1;
    });
    var fills = subs.map(function (s) {
      if (Math.abs(s.a.y - midY) < 0.05 && Math.abs(s.b.y - midY) < 0.05) return "";
      return '<path class="rb-fill" fill="' + escapeHtml(s.hex) + '" d="M' +
        s.a.x.toFixed(1) + "," + midY.toFixed(1) + " L" + s.a.x.toFixed(1) + "," + s.a.y.toFixed(1) +
        " L" + s.b.x.toFixed(1) + "," + s.b.y.toFixed(1) +
        " L" + s.b.x.toFixed(1) + "," + midY.toFixed(1) + ' Z"></path>';
    }).join("");
    var strokes = subs.map(function (s) {
      return '<line class="rb-seg' + (s.seen ? " seen" : "") + (s.last ? " rb-new" : "") +
        '" stroke="' + escapeHtml(s.hex) + '" x1="' + s.a.x.toFixed(1) + '" y1="' + s.a.y.toFixed(1) +
        '" x2="' + s.b.x.toFixed(1) + '" y2="' + s.b.y.toFixed(1) +
        '" style="--len:' + s.len.toFixed(2) + 'px"></line>';
    }).join("");
    var lastHex = subs.length ? subs[subs.length - 1].hex : homeHex;

    /* On the very first plotted play there are no segments yet. The frame and
       the marker still render, so the slide's layout does not jump when the
       line appears on the play after it. */
    var last = pts[pts.length - 1];
    var mid = midY;

    /* One divider and one label per half-inning across the whole axis,
       including halves the game has not reached yet - those render dimmed, so
       the amount of game still to come is visible rather than implied. */
    var dividers = "";
    var axis = "";
    for (var h = 0; h < totalHalves; h++) {
      var future = h > lastHip ? " future" : "";
      if (h > 0) {
        var dx = xAt(h).toFixed(1);
        dividers += '<line class="rb-div' + future + '" x1="' + dx +
          '" y1="0" x2="' + dx + '" y2="' + RIBBON_H + '"></line>';
      }
      axis += '<span class="rb-axis-tick' + future + '" style="left:' +
        ((xAt(h + 0.5) / RIBBON_W) * 100).toFixed(2) + '%">' +
        (h % 2 === 0 ? "▲" : "▼") + (Math.floor(h / 2) + 1) + "</span>";
    }

    /* The marker carries the readout instead of a separate caption: the home
       team's badge sits on the newest point, with their current odds and what
       this play just did to them. It rides on an HTML overlay rather than
       inside the SVG because the SVG is stretched with
       preserveAspectRatio="none" and would squash an embedded image. */
    var cur = plays[upto];
    var wpAfter = homeWpOf(cur);
    var wpBefore = cur.win_prob_before == null ? null
      : (cur.batting_is_home ? cur.win_prob_before : 1 - cur.win_prob_before);
    var homeDelta = (wpAfter != null && wpBefore != null) ? (wpAfter - wpBefore) : null;
    var xPct = (last.x / RIBBON_W) * 100;
    var marker = "";
    if (wpAfter != null) {
      /* The badge belongs to whichever team the play just helped, with their
         own odds and their gain - "who just got better off" is the thing worth
         reading, and it makes the number always a positive move.

         The line itself stays home-perspective, because that is the only way
         it can be one continuous curve. So the badge is a callout about the
         moment rather than a label for the dot's height: on an away-team gain
         the dot still sits at the home team's win probability. */
      var homeGained = homeDelta == null ? true : homeDelta >= 0;
      var gainAbbr = homeGained ? slide.homeAbbr : slide.awayAbbr;
      var gainPct = homeGained ? wpAfter : 1 - wpAfter;
      var gain = homeDelta == null ? null : Math.abs(homeDelta) * 100;
      // Only the badge itself needs to stay inside the plot; the readout flips
      // to the other side once the point is far enough right.
      var markLeft = Math.max(1.7, Math.min(98.3, xPct));
      // The ring around the badge is the colour of the team inside it, so the
      // marker reads as belonging to them rather than to the chart.
      var gainHex = teamColor(gainAbbr) || lastHex;
      var gainUrl = teamLogoUrl(gainAbbr);
      var badge = gainUrl
        ? '<img class="rb-marker-logo" src="' + escapeHtml(gainUrl) + '" alt="" loading="lazy" ' +
          'style="box-shadow:0 0 0 2px ' + escapeHtml(gainHex) + '">'
        : '<span class="rb-marker-dot" style="background:' + escapeHtml(gainHex) + '"></span>';
      marker = '<div class="rb-marker' + (xPct > 68 ? " flip" : "") + '" style="left:' +
        markLeft.toFixed(2) + "%;top:" + ((last.y / RIBBON_H) * 100).toFixed(2) + '%">' +
        badge +
        '<div class="rb-readout">' +
          '<span class="rb-pct">' + Math.round(gainPct * 100) + "%</span>" +
          (gain == null ? "" :
            '<span class="wpa-pos">+' + gain.toFixed(1) + "</span>") +
        "</div>" +
      "</div>";
    }

    /* The marker is positioned inside .rb-plot, which wraps the SVG and
       nothing else. Percentages have to resolve against exactly the plot box:
       hung on .scene-ribbon they would resolve against its full height, axis
       row included, and the badge would sit below the point it marks. */
    return '<div class="scene-ribbon"><div class="rb-plot">' +
      '<svg viewBox="0 0 ' + RIBBON_W + " " + RIBBON_H + '" preserveAspectRatio="none" aria-hidden="true">' +
        fills +
        dividers +
        '<line class="rb-mid" x1="0" y1="' + mid + '" x2="' + RIBBON_W + '" y2="' + mid + '"></line>' +
        strokes +
      "</svg>" +
      // Which end of the y-axis belongs to whom. Without this the line's
      // direction is ambiguous - up is good for someone, but you cannot tell
      // who from the curve alone.
      '<div class="rb-y rb-y-top"><span>' + escapeHtml(slide.homeAbbr || "") + " 100%</span></div>" +
      '<div class="rb-y rb-y-bot"><span>' + escapeHtml(slide.awayAbbr || "") + " 100%</span></div>" +
      marker + "</div>" +
      '<div class="rb-axis">' + axis + "</div>" +
    "</div>";
  }

  /* Batter and pitcher always sit in the same two slots, unlike card(m)'s
     featured/counterpart pair - which one is "featured" flips with the result
     category, so in a slideshow the same name would jump sides play to play.
     A fixed AT BAT / PITCHING pairing is what makes a run of slides readable. */
  function sceneRoleHtml(role, id, name, teamAbbr) {
    var isFav = window.KMFavorites && id && window.KMFavorites.has(id);
    var star = id
      ? '<button type="button" class="star-btn ' + (isFav ? "on" : "") +
        '" data-fav-id="' + id + '" title="Favorite this player">' +
        (isFav ? "★" : "☆") + "</button>"
      : "";
    return '<div class="mu-side">' +
      '<div class="mu-role">' + teamLogoImg(teamAbbr, "mu-logo") +
        "<span>" + role + "</span></div>" +
      '<div class="mu-name">' + star +
        "<span>" + escapeHtml(name || "-") + "</span></div>" +
    "</div>";
  }

  /* No tag pills and no win-probability text here: the tags are a filtering
     device for the main feed rather than something to read mid-slideshow, and
     the win probability is already on the ribbon's marker, attached to the
     point it belongs to. */
  function sceneDetailHtml(m) {
    var resultLabel = (data.meta.result_labels || {})[m.result] || m.result;
    /* Result first, then who did it - the same order at every width, so the
       eye lands in the same place whether the scene is stacked on a phone or
       split into two columns on a wide screen. */
    return '<div class="scene-detail">' +
      '<div class="scene-play-line">' +
        '<span class="result-pill ' + (m.result_category === "hitting" ? "offense" : "defense") + '">' +
          escapeHtml(resultLabel) + "</span>" +
        diffPill(m) +
      "</div>" +
      '<div class="scene-matchup">' +
        sceneRoleHtml("AT BAT", m.batter_id, m.batter_name, m.off_team_abbr) +
        '<span class="mu-vs">vs</span>' +
        sceneRoleHtml("PITCHING", m.pitcher_id, m.pitcher_name, m.def_team_abbr) +
      "</div>" +
      scoringLine(m) +
    "</div>";
  }

  /* Score ticker: the run lands as the scoring token reaches home, so the
     digit's animation-delay is tuned to the token travel time in style.css
     rather than being driven by a timer that could outlive the slide. */
  function sceneScoreHtml(m) {
    var awayBatting = !m.batting_is_home;
    var scored = (m.runs || 0) > 0;
    var tick = function (isThisRow) {
      return scored && isThisRow ? " tick" : "";
    };
    return '<div class="score-block scene-score">' +
      '<div class="row' + (awayBatting ? " batting" : "") + '">' +
        teamLogoImg(m.away_team_abbr, "scene-score-logo") +
        '<span class="abbr">' + escapeHtml(m.away_team_abbr) + "</span>" +
        '<span class="val' + tick(awayBatting) + '">' + m.away_score + "</span></div>" +
      '<div class="row' + (awayBatting ? "" : " batting") + '">' +
        teamLogoImg(m.home_team_abbr, "scene-score-logo") +
        '<span class="abbr">' + escapeHtml(m.home_team_abbr) + "</span>" +
        '<span class="val' + tick(!awayBatting) + '">' + m.home_score + "</span></div>" +
    "</div>";
  }

  /* Carries what the removed replay-done card used to say, pinned to the play
     the replay now rests on. */
  function sceneRecapHtml(r) {
    if (!r) return "";
    if (!r.isFinal) {
      return '<div class="scene-recap live">That’s everything so far · the game’s still going</div>';
    }
    var head = "FINAL · " + escapeHtml(r.away) + " " + r.awayScore + ", " +
               escapeHtml(r.home) + " " + r.homeScore;
    var top = r.topPlay
      ? '<span class="scene-recap-top">Biggest play: ' + escapeHtml(r.topPlay.featured_name) +
        " · " + escapeHtml((data.meta.result_labels || {})[r.topPlay.result] || r.topPlay.result) +
        " (LI " + r.topPlay.leverage.toFixed(1) + ")</span>"
      : "";
    return '<div class="scene-recap"><span class="scene-recap-head">' + head + "</span>" + top + "</div>";
  }

  function playSceneHtml(slide) {
    var m = slide.play;
    // A lead change gets a one-shot wash of the new leader's colour - cheap,
    // and it makes the one tag that changes the game's story feel different.
    var flash = "";
    if ((m.tags || []).indexOf("lead_change") !== -1) {
      var leader = m.home_score > m.away_score ? m.home_team_abbr : m.away_team_abbr;
      var hex = teamColor(leader);
      if (hex) flash = '<div class="scene-flash" style="background:' + escapeHtml(hex) + '"></div>';
    }
    // The key-moment flag drives the longer dwell (slideDwell). Recorded here
    // as data only - the scene shows no badge for it; the extra time the slide
    // stays up is the whole tell.
    var isKey = !!m.is_key_moment;
    // A new half-inning gets both a dwell bonus (slideDwell) and a beat on the
    // inning indicator, so the break between halves is felt rather than just
    // waited through.
    var newHalf = startsHalfInning(slide);
    return '<div class="play-scene" data-key="' +
      (isKey ? "1" : "0") + '" data-new-half="' + (newHalf ? "1" : "0") +
      '" data-game="' + escapeHtml(m.game_code || "") + '">' + flash +
      sceneRecapHtml(slide.recap) +
      '<div class="scene-top">' +
        sceneDiamondHtml(m) +
        '<div class="scene-side">' +
          '<div class="scene-inning' + (newHalf ? " new-half" : "") + '">' +
            '<div class="tri ' + (m.half === "top" ? "up" : "down") + '"></div>' +
            '<div class="inning-num">' + m.inning + "</div>" +
          "</div>" +
          sceneScoreHtml(m) +
          sceneMeterHtml(m) +
        "</div>" +
      "</div>" +
      sceneDetailHtml(m) +
      sceneRibbonHtml(slide) +
    "</div>";
  }

  function catchUpSlideHtml(slide) {
    if (slide.kind === "title") {
      var g = slide.group;
      return '<div class="catchup-title">' +
        '<div class="catchup-title-teams">' +
          teamLogoImg(g.away_team_abbr, "catchup-title-logo") +
          '<span>' + escapeHtml(teamName(g.away_team_abbr)) + "</span>" +
          '<span class="catchup-at">@</span>' +
          teamLogoImg(g.home_team_abbr, "catchup-title-logo") +
          '<span>' + escapeHtml(teamName(g.home_team_abbr)) + "</span>" +
        "</div>" +
        '<div class="catchup-title-sub">Session ' + escapeHtml(String(g.session_number)) +
          " · " + g.plays.length + (g.plays.length === 1 ? " new play" : " new plays") + "</div>" +
      "</div>";
    }
    if (slide.kind === "done") {
      return '<div class="catchup-title">' +
        '<div class="catchup-title-teams"><span>You’re all caught up</span></div>' +
        '<div class="catchup-title-sub">' + slide.total +
          (slide.total === 1 ? " play" : " plays") + " since your last visit</div>" +
      "</div>";
    }
    if (slide.kind === "replay-title") {
      return '<div class="catchup-title">' +
        '<div class="catchup-title-teams">' +
          teamLogoImg(slide.away, "catchup-title-logo") +
          '<span>' + escapeHtml(teamName(slide.away)) + "</span>" +
          '<span class="catchup-at">@</span>' +
          teamLogoImg(slide.home, "catchup-title-logo") +
          '<span>' + escapeHtml(teamName(slide.home)) + "</span>" +
        "</div>" +
        '<div class="catchup-title-sub">' +
          (slide.isFinal ? "Final · " : "In progress · ") + slide.count +
          (slide.count === 1 ? " play" : " plays") + "</div>" +
      "</div>";
    }
    return playSceneHtml(slide);
  }

  /* Dwell is per-play, not a metronome: a routine groundout goes by quickly,
     a play that earned its way into the feed lingers. Shared by Catch Me Up
     and Game Replay so pacing is tuned in one place.

     The key-moment floor also has to clear the Play Scene's own animations
     (diamond travel 900ms, score ticker landing at ~1.3s) - a slide that
     auto-advances mid-animation reads as broken. */
  var PLAY_DWELL_MS_ROUTINE = 2000;
  var PLAY_DWELL_MS_KEY = 4500;
  // Extra beat on the play that opens a half-inning, so the break between
  // halves registers instead of the reel running straight through it.
  var HALF_INNING_BONUS_MS = 700;

  function slideDwell(slide) {
    if (slide.kind !== "play") return TITLE_DWELL_MS;
    var base = slide.play.is_key_moment ? PLAY_DWELL_MS_KEY : PLAY_DWELL_MS_ROUTINE;
    return base + (startsHalfInning(slide) ? HALF_INNING_BONUS_MS : 0);
  }

  /* Whole-card fades are for structural changes only - title to play, one game
     to the next, play to the closing card. Running that fade between two plays
     of the same game blinked every logo, the diamond field and the ribbon
     baseline back through transparent even though none of it had changed,
     which is what read as choppy. On a same-game advance the card stays at
     full opacity and each inner component animates itself instead; they are
     freshly built elements, so their own CSS animations restart on their own. */
  function isSameGameAdvance(prev, next) {
    if (!prev || !next || prev.kind !== "play" || next.kind !== "play") return false;
    return prev.play.game_code === next.play.game_code;
  }

  function mountSlide(slideEl, slide, prev) {
    slideEl.innerHTML = catchUpSlideHtml(slide);
    if (isSameGameAdvance(prev, slide)) {
      slideEl.classList.add("in");
      return;
    }
    // Restart the fade: removing and re-adding after a reflow is what makes
    // the transition replay rather than running only on the first slide.
    slideEl.classList.remove("in");
    void slideEl.offsetWidth;
    slideEl.classList.add("in");
  }

  function clearCatchUpTimer() {
    if (catchUp.timer) {
      window.clearTimeout(catchUp.timer);
      catchUp.timer = null;
    }
  }

  function scheduleCatchUp(ms) {
    clearCatchUpTimer();
    catchUp.startedAt = Date.now();
    catchUp.remaining = ms;
    catchUp.timer = window.setTimeout(function () {
      catchUp.timer = null;
      showCatchUpSlide(catchUp.index + 1);
    }, ms);
  }

  function showCatchUpSlide(i) {
    if (i >= catchUp.slides.length) { closeCatchUp(); return; }
    var prev = catchUp.index >= 0 ? catchUp.slides[catchUp.index] : null;
    catchUp.index = i;
    var slide = catchUp.slides[i];
    mountSlide($("catchup-slide"), slide, prev);

    var progress = $("catchup-progress");
    // Unitless on purpose - the CSS divides it by 100 (see .catchup-progress::after).
    if (slide.kind === "play") {
      progress.textContent = "Play " + slide.playNo + " of " + slide.total;
      progress.style.setProperty("--catchup-pct", String(100 * slide.playNo / slide.total));
    } else if (slide.kind === "done") {
      progress.textContent = "";
      progress.style.setProperty("--catchup-pct", "100");
    } else {
      progress.textContent = "Game " + slide.gameNo + " of " + slide.gameTotal;
    }

    // The closing slide is the one place the show stops on its own - it waits
    // for the user rather than blinking out from under them.
    if (slide.kind === "done") {
      clearCatchUpTimer();
      return;
    }
    if (!catchUp.paused) scheduleCatchUp(slideDwell(slide));
  }

  function setCatchUpPaused(paused) {
    if (catchUp.paused === paused) return;
    catchUp.paused = paused;
    $("catchup-pause-hint").hidden = !paused;
    $("catchup-card").classList.toggle("paused", paused);
    if (paused) {
      // Resume from what was actually left, not a fresh full dwell.
      var elapsed = Date.now() - catchUp.startedAt;
      catchUp.remaining = Math.max(0, catchUp.remaining - elapsed);
      clearCatchUpTimer();
    } else if (catchUp.slides[catchUp.index] &&
               catchUp.slides[catchUp.index].kind !== "done") {
      scheduleCatchUp(catchUp.remaining || slideDwell(catchUp.slides[catchUp.index]));
    }
  }

  function openCatchUp() {
    var fav = window.KMFavorites;
    if (fav && !fav.hasName()) { fav.openPanel(); return; }
    if (!catchUpPlayCount(data.catchUpGroups)) return;
    catchUp.slides = buildCatchUpSlides(data.catchUpGroups);
    catchUp.index = -1;   // no previous slide, so slide 0 gets the full fade in
    catchUp.paused = false;
    $("catchup-pause-hint").hidden = true;
    $("catchup-card").classList.remove("paused");
    $("catchup-modal").hidden = false;
    showCatchUpSlide(0);
  }

  function closeCatchUp() {
    // Clearing the timer here is what stops a slide advancing behind an
    // already-hidden modal.
    clearCatchUpTimer();
    catchUp.paused = false;
    $("catchup-modal").hidden = true;
    $("catchup-slide").innerHTML = "";
  }

  function wireCatchUp() {
    var modal = $("catchup-modal");
    if (!modal) return;
    $("catchup-banner").addEventListener("click", openCatchUp);
    $("catchup-close").addEventListener("click", closeCatchUp);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeCatchUp(); });
    $("catchup-slide").addEventListener("click", function (e) {
      // A star or a link inside a card is a real action - it should not also
      // pause the show.
      var star = e.target.closest("[data-fav-id]");
      if (star) {
        if (window.KMFavorites) window.KMFavorites.toggle(star.getAttribute("data-fav-id"));
        star.classList.toggle("on");
        star.textContent = star.classList.contains("on") ? "★" : "☆";
        return;
      }
      if (e.target.closest("a")) return;
      setCatchUpPaused(!catchUp.paused);
    });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") closeCatchUp();
      else if (e.key === " ") { e.preventDefault(); setCatchUpPaused(!catchUp.paused); }
    });
  }

  // ── Game Replay ─────────────────────────────────────────────────────────────
  // Watch one whole game from a scoreboard tile. No identity, no cursor, no
  // per-user progress - it works for any anonymous visitor.
  //
  // The timer/pause/close machinery below is a deliberate parallel copy of
  // Catch Me Up's rather than a shared abstraction. This codebase's convention
  // has been to wait for two proven usages before generalizing; this is the
  // second one, so de-duplicating the two engines is now a reasonable cleanup -
  // but it is a refactor of shipped, working code, and worth doing only once
  // both features have been live long enough to be sure the shapes really are
  // the same. The slide RENDERER (playSceneHtml/catchUpSlideHtml) and the dwell
  // function are already shared; it is only the stateful parts that are twinned.

  var replay = {
    slides: [],
    index: 0,
    timer: null,
    paused: false,
    startedAt: 0,
    remaining: 0,
  };

  /* Same lazy-fetch-and-cache shape as loadAllSessions()/ensurePlaysLoaded(),
     scoped to the one session the game belongs to. */
  function loadGameReplay(gameCode, session) {
    var fetchPromise = data.playsBySession[session]
      ? Promise.resolve(data.playsBySession[session])
      : getJSON("data/plays_" + pad2(session) + ".json").then(function (rows) {
          data.playsBySession[session] = rows;
          return rows;
        });
    return fetchPromise.then(function () {
      return gamePlaysFor(session, gameCode);
    });
  }

  /* No trailing summary card: a single-game replay ends by holding on the
     game's actual last play. The recap that card used to carry (final score,
     and the game's highest-leverage play) rides along on that last play slide
     as a banner instead, so the information survives the change. */
  function buildGameReplaySlides(plays) {
    if (!plays.length) return [];
    var last = plays[plays.length - 1];
    var isFinal = !!last.is_game_final;
    var away = plays[0].away_team_abbr, home = plays[0].home_team_abbr;
    // Highest-leverage play of the game, straight off the list already loaded.
    var top = null;
    plays.forEach(function (p) {
      if (p.leverage != null && (!top || p.leverage > top.leverage)) top = p;
    });
    var slides = [{
      kind: "replay-title", away: away, home: home,
      isFinal: isFinal, count: plays.length,
    }];
    plays.forEach(function (p, i) {
      var slide = {
        kind: "play", play: p, playNo: i + 1, total: plays.length,
        gamePlays: plays, gameIdx: i,
        ribbonFrom: 0,          // a replay has no already-known prefix to mute
        homeAbbr: home, awayAbbr: away,
      };
      if (i === plays.length - 1) {
        slide.recap = {
          isFinal: isFinal, away: away, home: home,
          awayScore: last.away_score, homeScore: last.home_score,
          topPlay: isFinal ? top : null,
        };
      }
      slides.push(slide);
    });
    return slides;
  }

  function clearReplayTimer() {
    if (replay.timer) {
      window.clearTimeout(replay.timer);
      replay.timer = null;
    }
  }

  function scheduleReplay(ms) {
    clearReplayTimer();
    replay.startedAt = Date.now();
    replay.remaining = ms;
    replay.timer = window.setTimeout(function () {
      replay.timer = null;
      showReplaySlide(replay.index + 1);
    }, ms);
  }

  function showReplaySlide(i) {
    if (i >= replay.slides.length) { closeReplay(); return; }
    var prev = replay.index >= 0 ? replay.slides[replay.index] : null;
    replay.index = i;
    var slide = replay.slides[i];
    mountSlide($("replay-slide"), slide, prev);

    var progress = $("replay-progress");
    if (slide.kind === "play") {
      progress.textContent = "Play " + slide.playNo + " of " + slide.total;
      progress.style.setProperty("--catchup-pct", String(100 * slide.playNo / slide.total));
    } else {
      progress.textContent = "Replay";
      progress.style.setProperty("--catchup-pct", "0");
    }

    /* A replay ends by holding on the game's actual last play, not by cutting
       to a summary card. Without a trailing slide to stop on, the final play
       has to stop the timer itself - otherwise its dwell would expire, the
       index would run past the end and the overlay would auto-close. */
    if (i === replay.slides.length - 1) { clearReplayTimer(); return; }
    if (!replay.paused) scheduleReplay(slideDwell(slide));
  }

  function setReplayPaused(paused) {
    if (replay.paused === paused) return;
    replay.paused = paused;
    $("replay-pause-hint").hidden = !paused;
    $("replay-card").classList.toggle("paused", paused);
    if (paused) {
      var elapsed = Date.now() - replay.startedAt;
      replay.remaining = Math.max(0, replay.remaining - elapsed);
      clearReplayTimer();
    } else if (replay.slides[replay.index] &&
               replay.index !== replay.slides.length - 1) {
      // The last play is the resting point - resuming there has nothing to
      // advance to, same as the closing card it replaced.
      scheduleReplay(replay.remaining || slideDwell(replay.slides[replay.index]));
    }
  }

  /* A live game's replay is a snapshot taken when it opens - it plays to the
     last recorded play and stops, rather than chasing a game still in progress.
     Same "don't chase a moving target" rule as Catch Me Up's cursor read. */
  function openGameReplayFor(btn) {
    var gameCode = btn.getAttribute("data-replay");
    var tile = btn.closest(".scoreboard-tile");
    var raw = tile && tile.getAttribute("data-session");
    var session = raw ? Number(raw) : filters.session;
    if (session == null || isNaN(session)) {
      toast("Pick a session to replay a game.");
      return;
    }
    btn.classList.add("loading");
    loadGameReplay(gameCode, session).then(function (plays) {
      btn.classList.remove("loading");
      if (!plays.length) { toast("No plays recorded for that game yet."); return; }
      replay.slides = buildGameReplaySlides(plays);
      replay.index = -1;   // no previous slide, so slide 0 gets the full fade in
      replay.paused = false;
      $("replay-pause-hint").hidden = true;
      $("replay-card").classList.remove("paused");
      $("replay-modal").hidden = false;
      showReplaySlide(0);
    }).catch(function () {
      btn.classList.remove("loading");
      toast("Could not load that game's plays.");
    });
  }

  function closeReplay() {
    clearReplayTimer();
    replay.paused = false;
    $("replay-modal").hidden = true;
    $("replay-slide").innerHTML = "";
  }

  function wireReplay() {
    var modal = $("replay-modal");
    if (!modal) return;
    $("replay-close").addEventListener("click", closeReplay);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeReplay(); });
    $("replay-slide").addEventListener("click", function (e) {
      var star = e.target.closest("[data-fav-id]");
      if (star) {
        if (window.KMFavorites) window.KMFavorites.toggle(star.getAttribute("data-fav-id"));
        star.classList.toggle("on");
        star.textContent = star.classList.contains("on") ? "★" : "☆";
        return;
      }
      if (e.target.closest("a")) return;
      setReplayPaused(!replay.paused);
    });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") closeReplay();
      else if (e.key === " ") { e.preventDefault(); setReplayPaused(!replay.paused); }
    });
  }

  function renderMaybeLoading() {
    if (!filters.keyMomentsOnly) {
      ensurePlaysLoaded().then(render);
    } else {
      render();
    }
  }

  // ── Key Moments / Favorites toggle - shared between #toggle-chips and the
  //    phone-only header shortcuts (index.html), kept in sync both ways ──────

  function syncToggleChips(slug, on) {
    Array.prototype.forEach.call(
      document.querySelectorAll('#toggle-chips [data-toggle="' + slug + '"]'),
      function (c) { c.classList.toggle("active", on); }
    );
  }

  function syncHeaderToggle(id, on) {
    var btn = $(id);
    if (!btn) return;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  }

  function toggleKeyMoments() {
    filters.keyMomentsOnly = !filters.keyMomentsOnly;
    syncToggleChips("keymoments", filters.keyMomentsOnly);
    syncHeaderToggle("header-key-toggle", filters.keyMomentsOnly);
    renderMaybeLoading();
  }

  function toggleFavorites() {
    filters.favoritesOnly = !filters.favoritesOnly;
    syncToggleChips("favorites", filters.favoritesOnly);
    syncHeaderToggle("header-fav-toggle", filters.favoritesOnly);
    render();
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
      deselectScoreboardTile();
      renderScoreboard();
      renderMaybeLoading();
    });

    window.addEventListener("resize", scheduleScoreboardResize);

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

    // For/Against/All - render() re-derives disabled state and active chip
    // every time (see updateSideAvailability), so a disabled chip here is
    // just belt-and-suspenders against a stray click event.
    $("side-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip || chip.disabled) return;
      filters.side = chip.getAttribute("data-side");
      render();
    });

    // Key Moments is the pool switch (default on); Rookies and Favorites are
    // independent booleans that AND with everything, including each other.
    $("toggle-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var which = chip.getAttribute("data-toggle");
      if (which === "keymoments") {
        toggleKeyMoments();
      } else if (which === "rookies") {
        filters.rookiesOnly = !filters.rookiesOnly;
        chip.classList.toggle("active", filters.rookiesOnly);
        render();
      } else {
        toggleFavorites();
      }
    });

    // Phone-only header shortcuts for the same two toggles - see the
    // markup comment in index.html. syncToggleChips() inside each function
    // keeps #toggle-chips's keymoments/favorites buttons in sync so the two
    // controls never disagree, whichever one was clicked.
    $("header-key-toggle").addEventListener("click", toggleKeyMoments);
    $("header-fav-toggle").addEventListener("click", toggleFavorites);

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
      deselectScoreboardTile();
      render();
    });

    $("scoreboard").addEventListener("click", function (e) {
      // Checked before the tile lookup: the replay control sits inside the
      // tile, so without stopping here a click on it would also toggle the
      // tile's own game filter.
      var replayBtn = e.target.closest("[data-replay]");
      if (replayBtn) {
        e.stopPropagation();
        openGameReplayFor(replayBtn);
        return;
      }
      var tile = e.target.closest(".scoreboard-tile");
      if (!tile) return;
      selectScoreboardTile(tile);
    });

    // role=button does not bring the native Enter/Space activation a real
    // <button> had, so the tile's keyboard behaviour is restored by hand.
    $("scoreboard").addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var tile = e.target.closest(".scoreboard-tile");
      if (!tile || e.target.closest("[data-replay]")) return;   // the button handles itself
      e.preventDefault();
      selectScoreboardTile(tile);
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
      filters.side = "all";
      deselectScoreboardTile();
      $("player-suggest").hidden = true;
      Array.prototype.forEach.call(
        document.querySelectorAll("#result-chips .chip, #tag-chips .chip"),
        function (c) { c.classList.remove("active"); }
      );
      // Key Moments defaults active, unlike Rookies/Favorites - not a blanket clear.
      Array.prototype.forEach.call(document.querySelectorAll("#toggle-chips .chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-toggle") === "keymoments");
      });
      syncHeaderToggle("header-key-toggle", true);
      syncHeaderToggle("header-fav-toggle", false);
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

    wireCatchUp();
    wireReplay();
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
      renderScoreboard();
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
      renderScoreboard();
      populateTeamSelect();
      populateTagChips();
      render();
      window.KMFavorites.init(data.players, function () {
        renderMaybeLoading();
        window.KMFavorites.refreshList();
      }).then(function () {
        // After init resolves, so the cursor from the favorites GET is in hand.
        renderCatchUpBanner();
        return computeCatchUp();
      }).then(function (groups) {
        data.catchUpGroups = groups;
        renderCatchUpBanner();
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

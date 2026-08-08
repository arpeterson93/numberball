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
    resultCode: "",     // exact result code (e.g. "GO", "1BWH") - debugging/search, distinct from the broad `result` category above
    outs: null,         // 0 | 1 | 2 | null for all - outs_before
    obc: "",            // "" | "000".."111" - obc_before
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

  function ordinal(n) {
    var v = n % 100;
    if (v >= 11 && v <= 13) return n + "th";
    return n + ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
  }

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

  function formatMomentTime(iso) {
    if (!iso) return "";
    var d = parseChicagoNaive(iso);
    if (!d || isNaN(d.getTime())) return iso;
    var h = d.getHours();
    var ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + h + ":" + pad2(d.getMinutes()) + " " + ampm;
  }

  function formatBuiltAt(iso) {
    if (!iso) return "Data as of -";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "Data as of " + iso;
    // built_at is a real UTC "...Z" string (key_moments_build.py), so these
    // getters already report back in the viewer's own local timezone with no
    // extra reinterpretation needed - unlike formatMomentTime's naive Central
    // strings above, this one is properly zoned already.
    var h = d.getHours();
    var ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return "Updated " + MONTHS[d.getMonth()] + " " + d.getDate() + ", " + h + ":" + pad2(d.getMinutes()) + " " + ampm;
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
    if (filters.resultCode && m.result !== filters.resultCode) return false;
    if (filters.outs !== null && (m.outs_before || 0) !== filters.outs) return false;
    if (filters.obc && String(m.obc_before || "000") !== filters.obc) return false;
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
      '<span class="' + cls + '">' + pct + "% " + sign + delta.toFixed(1) + "</span></span>";
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
      // half === "bottom" means the whole inning just closed; "top" means
      // only the first half is done and the same inning continues.
      var halfLbl = (m.half === "bottom" ? "END " : "MID ") + ordinal(m.inning);
      return '<div class="state-stack"><div class="state-badge">' + escapeHtml(halfLbl) + "</div></div>";
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
    // Jumps straight into Game Replay at this exact play, left of the
    // MLN-reference arrow. Needs the same three fields Game Replay's own
    // scoreboard-tile replay button uses (loadGameReplay's game/session,
    // play_num to seek within it) - all already on every card.
    var jumpBtn = (m.game_code && m.session_number != null && m.play_num != null)
      ? '<button type="button" class="play-jump-btn" data-jump-game="' + escapeHtml(m.game_code) +
        '" data-jump-session="' + m.session_number + '" data-jump-num="' + m.play_num +
        '" title="Watch this play" aria-label="Watch this play in the game replay">' +
        '<svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="8 5 19 12 8 19 8 5"></polygon></svg>' +
        "</button>"
      : "";
    var inlineLogo = teamLogoImg(m.featured_team_abbr, "team-logo-inline-img");

    var levBarHex = teamColor(m.featured_team_abbr);

    return '<div class="moment">' +
      '<div class="corner-actions">' + jumpBtn + gameLink + "</div>" +
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
    if (filters.resultCode) n += 1;
    if (filters.outs !== null) n += 1;
    if (filters.obc) n += 1;
    return n + filters.tags.size;
  }

  function updateFilterSummary() {
    var n = activeFilterCount();
    $("filters-toggle-label").textContent = n ? "Filters · " + n + " active" : "Filters";
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
    $("page-title").textContent = "MLN GAMEDAY";
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
    // An SVG triangle, not a "▶" text glyph - iOS renders that glyph with its
    // own colored emoji presentation, which reads differently there than the
    // plain monochrome arrow desktop browsers show for the same character.
    el.innerHTML = '<svg class="catchup-play-icon" viewBox="0 0 24 24" aria-hidden="true">' +
      '<polygon points="8 5 19 12 8 19 8 5"></polygon></svg> Catch Me Up · ' +
      count + (count === 1 ? " new play" : " new plays");
  }

  // ── Playback speed: a per-browser preference (same "device identity" as the
  //    favorites name), not synced anywhere - it only ever changes what
  //    slideDwell() hands back, so every slideshow (Catch Me Up, Game Replay,
  //    the filtered-plays reel) picks it up for free. ──────────────────────────
  var PLAYBACK_SPEED_KEY = "km_playback_speed";
  var PLAYBACK_SPEED_MIN = 0.25, PLAYBACK_SPEED_MAX = 2;

  function clampSpeed(v) {
    v = Number(v);
    if (!isFinite(v)) return 1;
    return Math.min(PLAYBACK_SPEED_MAX, Math.max(PLAYBACK_SPEED_MIN, v));
  }

  function getPlaybackSpeed() {
    try {
      var raw = window.localStorage.getItem(PLAYBACK_SPEED_KEY);
      return raw == null ? 1 : clampSpeed(raw);
    } catch (e) {
      return 1;
    }
  }

  function setPlaybackSpeed(v) {
    var speed = clampSpeed(v);
    try { window.localStorage.setItem(PLAYBACK_SPEED_KEY, speed.toFixed(2)); } catch (e) { /* private browsing */ }
    return speed;
  }

  function wirePlaybackSpeed() {
    var input = $("playback-speed");
    if (!input) return;
    input.value = getPlaybackSpeed().toFixed(2);
    function commit() { input.value = setPlaybackSpeed(input.value).toFixed(2); }
    input.addEventListener("change", commit);
    input.addEventListener("blur", commit);
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

  // Field canvas: 460x370, home at (230,330).
  var FIELD_W = 460, FIELD_H = 370;
  var HOME_SVG = { x: 230, y: 330 };
  // Feet per SVG unit for the WHOLE field - infield included. A real infield
  // (90ft basepaths) and the outfield/fence/ball-flight geometry all share
  // this one scale; the diamond used to be hand-placed at a different,
  // stylized size, which made batted-ball distances look wrong relative to
  // it (a 380ft flyout reading as barely past the infield). Chosen so a
  // 420ft home run and a 375ft uniform fence both fit the canvas with margin
  // (see fenceAt/FENCE_DEPTH_FT below) - a proposal, not a relayed decision
  // (ball-flight-plan.md Open Question 1).
  var FT_PER_UNIT = 1.4;

  // The single conversion point from field-plane feet (home at the origin,
  // +y = depth into the outfield, +x toward 1B) to SVG coordinates. Every
  // field element - infield, outfield, fielders, fence, ball flight - renders
  // through this, on this one scale (ball-flight-plan.md Stage 4a: this
  // codebase's bug history is mostly coordinate-space confusion).
  function ftToSvg(xFt, yFt) {
    return { x: HOME_SVG.x + xFt / FT_PER_UNIT, y: HOME_SVG.y - yFt / FT_PER_UNIT };
  }

  // Real MLB basepaths: 90ft square, so home-to-1B/3B is 90ft along the foul
  // lines (angle 90/0, i.e. offset +-45deg from dead centre) and home-to-2B
  // is the 90ft square's diagonal.
  var BASE_DIST_FT = 90;
  var BASE_DIAG_FT = BASE_DIST_FT * Math.SQRT2;
  // Token/marker sizes, scaled down from the old hand-placed diamond to match
  // the now-correctly-scaled (and visually smaller) real-90ft infield.
  var RUNNER_R = 6, BASE_R = 4.5, BALL_R = 3, FIELDER_R = 4;

  // A real bag sits entirely in fair territory - its outer corner touches the
  // foul line, not its centre (Alex's correction). These are the exact 90ft
  // marks ON the foul lines; 1B/3B get nudged inward toward 2B below so the
  // drawn square's outer corner lands here instead of straddling the line
  // with its centre.
  var SCENE_BASES_ON_LINE = {
    "1B": ftToSvg(BASE_DIST_FT * Math.SQRT1_2, BASE_DIST_FT * Math.SQRT1_2),
    "2B": ftToSvg(0, BASE_DIAG_FT),
    "3B": ftToSvg(-BASE_DIST_FT * Math.SQRT1_2, BASE_DIST_FT * Math.SQRT1_2),
  };
  function insetTowardSecond(pt) {
    var second = SCENE_BASES_ON_LINE["2B"];
    var dx = second.x - pt.x, dy = second.y - pt.y;
    var len = Math.hypot(dx, dy) || 1;
    return { x: pt.x + (dx / len) * BASE_R, y: pt.y + (dy / len) * BASE_R };
  }
  var SCENE_BASES = {
    HOME: HOME_SVG,
    "1B": insetTowardSecond(SCENE_BASES_ON_LINE["1B"]),
    "2B": SCENE_BASES_ON_LINE["2B"],
    "3B": insetTowardSecond(SCENE_BASES_ON_LINE["3B"]),
  };

  // Home plate's own two outer corners - where the foul lines actually start
  // (Alex's correction), not the plate's centre. side: +1 = the 1B-side
  // corner, -1 = the 3B-side corner. HOME_PLATE_R matches platePath's own
  // plateR (sceneFieldHtml) - kept here too since foulLineD/infieldDirtHtml
  // need it and live outside that function.
  var HOME_PLATE_R = BASE_R * 0.9;
  function homePlateCorner(side) {
    return { x: SCENE_BASES.HOME.x + side * HOME_PLATE_R, y: SCENE_BASES.HOME.y };
  }

  // Nine generic fielder anchors, field-plane feet. No names, no per-play
  // defensive alignment - that data doesn't exist (ball-flight-plan.md
  // Decision 5).
  var FIELDER_ANCHORS_FT = {
    P: { x: 0, y: 60 }, C: { x: 0, y: -5 },
    "1B": { x: 75, y: 85 }, "2B": { x: 40, y: 145 }, SS: { x: -40, y: 145 }, "3B": { x: -75, y: 85 },
    LF: { x: -200, y: 260 }, CF: { x: 0, y: 320 }, RF: { x: 200, y: 260 },
  };

  // Ground ball outs, which infielder and how deep (Alex's HZ/depth spec).
  // The horizontal angle is only ever one of exactly 11 values - bucket
  // (signedCirc's ones-digit result) is an integer -5..5, and angle is a
  // linear function of it, so flight.angle always lands on 5, 13, 21, ...,
  // 85 exactly, for either hand (the hand-mirror just reverses which bucket
  // maps to which end - the final angle set is identical). That makes this a
  // plain lookup, not a range/interpolation.
  var HZ_FIELDER_BY_ANGLE = {
    5: "3B", 13: "3B",
    21: "SS", 29: "SS", 37: "SS",
    45: "P",
    53: "2B", 61: "2B", 69: "2B",
    77: "1B", 85: "1B",
  };
  var INFIELDER_DEPTH_FT = { "3B": 119, SS: 147, P: 60, "2B": 147, "1B": 111 };
  // The archetype (from the result's own band row) is the true "is this a
  // ground ball" signal, not flight.isGrounder - that flag is just LA<4 on a
  // value computed independently off the pitch/swing wheel, and it can
  // disagree with the actual play: a real "GO" can compute an LA a hair
  // above 4 (isGrounder false on a genuine grounder), and a caught line
  // drive can compute one a hair below it (isGrounder true on a ball that
  // was never on the ground at all). Anything gating ground-ball-only
  // behaviour (infielder depth, rollout) keys off archetype instead.
  var GROUND_ARCHETYPES = { grounder: 1, bunt: 1, infield_single: 1 };

  // Uniform fence depth in every direction (Alex's addition to the plan: one
  // constant distance all the way around, not a per-angle profile, for
  // simplicity in the visual). This trades some realism at the foul lines
  // (a real fence is usually shallower down the lines than to center) for a
  // simple circular arc and a single tunable number. Kept as a function
  // rather than a bare constant so callers read as "the fence distance at
  // this angle" and nothing has to change if a per-angle profile ever comes
  // back.
  //
  // Tuning note: the plan's original variable fence kept the overall
  // inside-the-park rate low (~0.9%) because most contact isn't dead center,
  // where the only deep (375ft) stretch of wall lived - everywhere else was
  // an easier 330-374ft to clear. A uniform fence removes that escape valve
  // at every angle, so more of the home_run archetype's low-q tail (soft
  // contact still classified HR, landing near depth_min=370) now lands
  // inside the park regardless of direction. Expect a meaningfully higher
  // inside-the-park rate than the plan's 0.9% estimate; watch real plays and
  // raise this or HR's own depth_min in result_diff_bands.csv if it happens
  // too often (ball-flight-plan.md Open Question 1b).
  var FENCE_DEPTH_FT = 375;
  function fenceAt(angleDeg) { return FENCE_DEPTH_FT; }

  // Real-park constants for the infield dirt circle: 60.5ft from home to the
  // pitcher's plate, and (per usual groundskeeping practice) a 95ft radius
  // for the dirt circle centred on the plate.
  var PITCHER_MOUND_FT = 60.5;
  var INFIELD_DIRT_RADIUS_FT = 95;

  // Batting team's dugout, field-plane feet - just foul of each line, behind
  // the bases. Home team uses the 1B-side dugout, away team 3B-side. A
  // convention, not a fact about any real park (ball-flight-plan.md Stage 6).
  var DUGOUT_FT = { home: { x: 95, y: -25 }, away: { x: -95, y: -25 } };
  function dugoutFor(m) { return m.batting_is_home ? DUGOUT_FT.home : DUGOUT_FT.away; }

  // Running order around the diamond. Index doubles as "how far around" a
  // runner is, with 4 meaning they came all the way back to score.
  var BASE_PATH = ["HOME", "1B", "2B", "3B"];
  var BASE_ORDINAL = { HOME: 0, "1B": 1, "2B": 2, "3B": 3 };

  // The next base a runner reaches past the one they're leaving - NOT
  // BASE_PATH[Math.min(3, BASE_ORDINAL[x] + 1)], which capped at index 3
  // ("3B") for a runner already ON 3B, wrongly implying "forced to stay at
  // 3rd" instead of "forced home." That formula was fine for 1B->2B and
  // 2B->3B (ordinals 1/2, one past never exceeds 3), but wrong the moment a
  // runner starts at 3B itself - and a runner stranded on 3B when a
  // half-inning-ending out resets obc_after to "000" looks, to
  // deriveRunnerMoves, exactly like one who was genuinely forced there, so
  // this was live on any final out with a runner on 3rd (not just a rare
  // edge case). Explicit map instead of that arithmetic.
  var NEXT_BASE = { "1B": "2B", "2B": "3B", "3B": "HOME" };

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

  // ── Ball flight (ball-flight-plan.md Stage 3) ─────────────────────────────
  // Pure function group: no DOM, no state. Everything here is deterministic
  // given (pitch, swing, diff, result, hand) plus the two tables shipped in
  // data.meta.flight. Ground-truth invariant: result/obc_before/obc_after/
  // runs are inputs - nothing here may change, override or contradict them,
  // it only decides how to stage an outcome that already happened.

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  // Generalises utils.circular_signed_delta to an arbitrary modulus. At the
  // wheel's exact halfway point (|d| === mod/2, e.g. a ones-digit delta of
  // exactly 5, or a two-digit delta of exactly 50) the sign is genuinely
  // ambiguous - both directions around the wheel are the same distance. This
  // is resolved by anchoring on `a` and taking whichever direction does not
  // cross the wheel's own wrap boundary: `b > a` is exactly "the forward path
  // from a to b does not wrap," `b < a` is exactly "the backward path does
  // not wrap" - so the plain `b - a`, left un-wrapped at the tie, already is
  // that rule. Verified exhaustively over every tie pair on both wheels this
  // function is used on (mod 10 and mod 100) - see ball_flight_test.py.
  function signedCirc(a, b, mod) {
    var d = b - a;
    if (d > mod / 2) d -= mod;
    else if (d < -mod / 2) d += mod;
    return d;
  }

  function firstTwo(v) { return Math.floor((v - 1) / 10); }   // 0..99
  function onesDigit(v) { return (v - 1) % 10; }               // 0..9

  // The HZ bucket's own digit extraction (Alex's call): literally v's last
  // decimal digit, with 1000 landing on 0 rather than 9 - unlike onesDigit's
  // -1 shift (needed to decompose the 1-1000 wheel into consistent
  // decade/ones components for LA), this is deliberately NOT shifted, so a
  // raw pitch/swing pair can be read by eye as a plain digit subtraction:
  // 220 vs 225 is "swing is 5 to the right of pitch," not off by one from
  // what the numbers themselves say. Only differs from onesDigit at the exact
  // wheel-halfway tie (a bucket delta of precisely +-5) - away from that tie
  // the two conventions agree, since the wraparound in signedCirc absorbs a
  // uniform shift everywhere except right at that boundary.
  function lastDigit(v) { return v % 10; }                    // 0..9, 1000->0

  // Pitcher hand is not threaded into the JSON (real roster data has zero
  // switch hitters today - ball-flight-plan.md Finding 4 - so this path is
  // dead on real data). A blank/unexpected batter hand, including "S" with
  // no pitcher hand to resolve against, falls back to "R" like any other
  // unresolved value.
  function effectiveHand(batterHand) {
    return batterHand === "L" || batterHand === "R" ? batterHand : "R";
  }

  function landingPoint(D, angleDeg) {
    var offset = (angleDeg - 45) * Math.PI / 180;
    return { x: D * Math.sin(offset), y: D * Math.cos(offset) };
  }

  // One-directional clamp (ball-flight-plan.md Stage 4d): a non-home-run may
  // never clear the fence, since a ball leaving the park in the air is a home
  // run by definition. A home run that lands short of the (uniform) fence is
  // left alone - that's a real inside-the-park home run, not an error.
  function clampToFence(D, angleDeg, isHomeRun) {
    if (isHomeRun) return D;
    return Math.min(D, fenceAt(angleDeg) - 12);
  }

  // The fence as one SVG arc, drawn once per field render but built from the
  // same landingPoint/ftToSvg functions everything else uses - no separate
  // hand-derived geometry to get out of sync with the actual math. Since the
  // fence is uniform, this is exactly a circular arc of radius FENCE_DEPTH_FT
  // from the 3B foul line (angle 0) to the 1B foul line (angle 90).
  function fencePathD() {
    var aFt = landingPoint(FENCE_DEPTH_FT, 0);
    var bFt = landingPoint(FENCE_DEPTH_FT, 90);
    var a = ftToSvg(aFt.x, aFt.y);
    var b = ftToSvg(bFt.x, bFt.y);
    var r = FENCE_DEPTH_FT / FT_PER_UNIT;
    return "M" + a.x.toFixed(1) + "," + a.y.toFixed(1) +
      " A" + r.toFixed(1) + "," + r.toFixed(1) + " 0 0 1 " + b.x.toFixed(1) + "," + b.y.toFixed(1);
  }

  // Foul lines: home plate's own outer corner (Alex's correction - not the
  // plate's centre) straight out to where the fence starts - the same
  // landingPoint(FENCE_DEPTH_FT, angle) endpoint fencePathD itself uses, so
  // the lines visually meet the fence with no separate geometry to drift out
  // of sync. 1B/3B now sit just inside this line (SCENE_BASES' inset above),
  // matching how a real bag is entirely in fair territory.
  function foulLineD(angleDeg) {
    var pt = landingPoint(FENCE_DEPTH_FT, angleDeg);
    var end = ftToSvg(pt.x, pt.y);
    var start = homePlateCorner(angleDeg >= 45 ? 1 : -1);
    return "M" + start.x.toFixed(1) + "," + start.y.toFixed(1) + " L" + end.x.toFixed(1) + "," + end.y.toFixed(1);
  }

  // Infield dirt: a circle of radius INFIELD_DIRT_RADIUS_FT centred on the
  // pitcher's plate (PITCHER_MOUND_FT in front of home), clipped to fair
  // territory only (Alex's correction) - a full circle that size reaches
  // well past both foul lines. Just the ARC that stays in fair territory -
  // no straight edges down to home, which double-drew right on top of the
  // foul lines themselves (Alex's follow-up correction) and read as one
  // thick line rather than two separate features. The arc's own two
  // endpoints already sit exactly on the foul lines (where the circle
  // intersects them), so it meets them cleanly with no gap and no overlap.
  function infieldDirtHtml() {
    var m = PITCHER_MOUND_FT, r = INFIELD_DIRT_RADIUS_FT;
    // Solving x^2 + (x-m)^2 = r^2 for the intersection of the circle with the
    // line y=x (the 1B foul line) - the 3B line (y=-x) is the mirror image.
    var edge = (m + Math.sqrt(2 * r * r - m * m)) / 2;
    var right = ftToSvg(edge, edge), left = ftToSvg(-edge, edge);
    var rSvg = r / FT_PER_UNIT;
    var d = "M" + left.x.toFixed(1) + "," + left.y.toFixed(1) +
      " A" + rSvg.toFixed(1) + "," + rSvg.toFixed(1) + " 0 0 1 " + right.x.toFixed(1) + "," + right.y.toFixed(1);
    return '<path class="dm-dirt" d="' + d + '"></path>';
  }

  function nearestFielder(x, y) {
    var best = null, bestD = Infinity;
    for (var key in FIELDER_ANCHORS_FT) {
      var a = FIELDER_ANCHORS_FT[key];
      var d = (a.x - x) * (a.x - x) + (a.y - y) * (a.y - y);
      if (d < bestD) { bestD = d; best = key; }
    }
    return best;
  }

  var GROUND_LA_THRESHOLD = 4;   // degrees; below this a batted ball is a grounder, not airborne

  // Launch angle anchors on this specific MLN result's own real, Statcast-
  // derived "ideal" LA (band.laIdeal - the median LA of real MLB plays
  // matching this result's own filter, e.g. events='home_run', or bb_type=
  // 'ground_ball' & events IN ('field_out','force_out') for GO - see
  // result_diff_bands.csv) rather than a flat laMin-to-laMax sweep or a
  // single borrowed physics constant. Reuses the SAME diff/band q that
  // already drives EV/depth below - q=1 at the band's low end (the
  // best-timed, hardest contact) is already "closest to ideal" for EV and
  // depth, so LA rides the same curve instead of a second, disconnected
  // timing signal: at q=1 this collapses to exactly laIdeal regardless of
  // direction; at q=0 it bottoms/tops out at this result's own laMin/laMax.
  // laIdeal is itself a real percentile (the 50th) of the same distribution
  // laMin/laMax come from (the 10th/90th), so it always sits meaningfully
  // inside the range with room on both sides - the old approach (an
  // externally-sourced physics ideal clamped into an archetype-wide range)
  // could put ideal exactly AT laMin or laMax for a result whose real range
  // never reaches the physics optimum, collapsing one whole direction's
  // adjustment to zero (Alex's catch - a mistimed swing that's still always
  // reported as "ideal" makes no sense).
  function launchAngleFor(band, q, onTop) {
    var ideal = band.laIdeal;
    return onTop
      ? ideal - (1 - q) * (ideal - band.laMin)
      : ideal + (1 - q) * (band.laMax - ideal);
  }

  // tables is data.meta.flight: { bands, excluded }. Each band now carries
  // its own laMin/laIdeal/laMax/evMin/evMax/depthMin/depthMax directly (see
  // result_diff_bands.csv) - no separate archetype-keyed range table to look
  // up; `band.archetype` is still a plain category label for
  // CAUGHT_IN_AIR/GROUND_ARCHETYPES/TAG_THROW_ARCHETYPES below.
  function flightParams(play, tables) {
    var result = play.result;
    if (!tables || (tables.excluded || []).indexOf(result) !== -1) return null;
    var pitch = play.pitch, swing = play.swing;
    if (pitch == null || swing == null) return null;
    var band = (tables.bands || {})[result];
    if (!band) return null;

    var bucket = signedCirc(lastDigit(pitch), lastDigit(swing), 10);

    var diff = play.diff;
    var q = diff == null ? 0 : 1 - clamp((diff - band.lo) / (band.hi - band.lo), 0, 1);

    // "On top" of the pitch (bat path above the ball - a topped, lower-LA
    // swing) vs "below" it (an uppercut, higher LA) - the sign of the FULL
    // pitch/swing circular delta over all three digits, not just the first
    // two the old dLA used - signedCirc already generalises to any modulus.
    var onTop = signedCirc(pitch, swing, 1000) > 0;
    var LA = launchAngleFor(band, q, onTop);

    var hand = effectiveHand(play.batter_hand);
    var frac = bucket / 5;
    var angle = hand === "L" ? 45 - frac * 40 : 45 + frac * 40;

    var EV = band.evMin + q * (band.evMax - band.evMin);
    var D = band.depthMin + q * (band.depthMax - band.depthMin);

    var isHomeRun = result === "HR";
    D = clampToFence(D, angle, isHomeRun);

    var pt = landingPoint(D, angle);
    var isGrounder = LA < GROUND_LA_THRESHOLD;
    var hangMs = isGrounder ? null : 1000 * 2 * (EV * 1.4667) * Math.sin(LA * Math.PI / 180) / 32.2;

    return {
      la: LA, ev: EV, distance: D, angle: angle, x: pt.x, y: pt.y, hangMs: hangMs,
      isGrounder: isGrounder, fielder: nearestFielder(pt.x, pt.y), archetype: band.archetype,
      // Over-the-fence vs. inside-the-park - both legal outcomes for a home
      // run (ball-flight-plan.md ground-truth invariant note). Never true for
      // anything else: clampToFence already prevented that.
      clearedFence: isHomeRun && D > fenceAt(angle),
    };
  }

  /* Ground ball outs: the archetype's depth range (a single 60-150ft band for
     every grounder) doesn't know a 3B/1B plays shallower than a SS/2B, so
     the same play could land at a distance that makes sense for one
     position and not another. This corrects it using the HZ angle, which
     already tells us exactly which infielder is fielding it: if the
     archetype distance would carry the ball past that fielder's normal
     depth, it's capped there (fielded on the way through, same as a real
     infielder never lets a ball get all the way to the track); if it's
     short, left alone - the fielder charges in and fields a weak roller
     closer to home, which is exactly Alex's "still have ground ball outs
     that aren't hit very far." Hits are untouched: a grounder that gets past
     the fielder's depth is precisely what makes it a hit, not an out. Called
     once, right after flightParams, so every consumer of `flight` (the ball
     trail, the throw origin, the rollout) sees the corrected distance/x/y
     with no separate plumbing. */
  var OF_POSITIONS = { LF: 1, CF: 1, RF: 1 };

  /* import_BRC.csv's optional ExcludedPositions/DefaultPosition columns -
     "the physics-computed fielder for this situation doesn't make sense,
     use this one instead." Positions occupy genuinely different places on
     the field, not just different depths along the same line out from home
     (a shortstop and a pitcher aren't on the same line at different
     depths) - so this snaps the landing point to the default position's own
     real anchor (FIELDER_ANCHORS_FT), both direction and depth, rather than
     nudging the physics-computed point along its original angle. Returns
     true if it fired, so the caller can skip the physics-angle-based ground
     ball depth cap below - the two are mutually exclusive corrections for
     the same kind of problem, not meant to stack. */
  function applyPositionOverride(m, flight) {
    var excluded = m.excluded_positions;
    var def = m.default_position;
    if (!excluded || !excluded.length || !def) return false;
    var isOF = !!OF_POSITIONS[flight.fielder];
    var isExcluded = excluded.indexOf(flight.fielder) !== -1 ||
      (isOF && excluded.indexOf("OF") !== -1);
    if (!isExcluded) return false;
    var anchor = FIELDER_ANCHORS_FT[def];
    if (!anchor) return false;
    flight.fielder = def;
    flight.x = anchor.x;
    flight.y = anchor.y;
    flight.distance = Math.hypot(anchor.x, anchor.y);
    flight.angle = Math.atan2(anchor.x, anchor.y) * 180 / Math.PI + 45;
    return true;
  }

  function applyGroundBallFielderDepth(m, flight) {
    if (!GROUND_ARCHETYPES[flight.archetype]) return;
    if (!((m.outs_after || 0) > (m.outs_before || 0))) return;
    var pos = HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];
    var depth = pos ? INFIELDER_DEPTH_FT[pos] : null;
    if (depth == null || flight.distance <= depth) return;
    flight.distance = depth;
    var pt = landingPoint(depth, flight.angle);
    flight.x = pt.x;
    flight.y = pt.y;
    flight.fielder = pos;
  }

  // ── Ball flight rendering (ball-flight-plan.md Stage 4) ───────────────────
  // Timing constants below are animation-feel judgment calls, not derived
  // from anything physical - flagged as tune-after-watching in the plan
  // (Open Questions 2 and 6).
  // A grounder to an infielder is quick - was 600, a groundout throw was
  // arriving 100ms after the runner (refinements plan Finding F10).
  var GROUNDER_ROLL_MS = 450;                          // Open Question 2
  var HANG_MS_SCALE = 0.35, HANG_MS_MIN = 450, HANG_MS_MAX = 1400;  // Open Question 6
  var RUNNER_LEAD_MS = 150;         // runners begin this long after slide mount, behind the ball
  var OUT_BEAT_MS = 400;            // "outs choreography begins" beat, on top of ball travel if any
  // Throw leaves almost as the ball is fielded (was 150) so a grounder's
  // throw beats the runner to the bag (refinements plan A4/F10).
  var THROW_DELAY_MS = 60;          // throw draws in this long after the ball is fielded
  var THROW_DRAW_MS = 180;          // how long one throw takes to draw in
  // Gap between successive throws on a multi-throw play (a DP's relay).
  // Tightened from 150 when THROW_LEAD_MS went 100->200: a 2-throw relay's
  // schedule (THROW_DELAY_MS + this + THROW_DRAW_MS, stacked on top of the
  // ball's own travel time) only had ~10ms of slack against the batter's
  // fixed first-to-base arrival time at the old margin, so doubling that
  // margin without also tightening this pushed the relay's second throw
  // past the runner outright. This restores the same ~10ms slack.
  var THROW_STAGGER_MS = 50;
  var THROW_LEAD_MS = 200;          // required margin: every throw must land at least this early
  var TAG_UP_MS = 80;               // a tagging runner leaves this long after the catch (B5)
  // A caught-ball throw that isn't chasing a real out (SacF/DSacF/FO's "the
  // drama of a sac fly" throw - see throwSchedule) is chasing a runner who's
  // already safe, same convention as STEAL_THROW_MARGIN_MS: the runner beats
  // the throw home by at least this much, instead of racing it.
  var TAG_THROW_MARGIN_MS = 200;
  // Off for now (Item 15) - Alex found the converging dot in the outfield an
  // unnecessary touch. The function, its CSS and its reduced-motion entry are
  // all still correct; this is a one-word revert if it comes back, e.g. as a
  // visible throw origin.
  var SHOW_FIELDER_TOKENS = false;

  function ballTravelMs(flight) {
    if (!flight) return 0;
    if (flight.isGrounder) return GROUNDER_ROLL_MS;
    return clamp((flight.hangMs || 0) * HANG_MS_SCALE, HANG_MS_MIN, HANG_MS_MAX);
  }

  // How far past the landing point a hit that stayed in play carries before
  // being fielded (C4/Item 16) - Alex's ask, read as "where it landed" and
  // "where it was fielded" should be two different things on screen, not
  // just a formula. A function of both inputs, not distance alone (Alex's
  // second-round correction): harder contact (higher EV) rolls out further,
  // and a flatter trajectory (lower LA) rolls out further too - a scorched
  // liner skids and keeps going, a towering fly drops closer to dead. Low LA
  // doesn't just add to the effect, it amplifies EV's contribution (evFrac
  // alone still contributes a floor via the 0.4 term, so a hard-hit ball at
  // a high launch angle still rolls some, just not as much as the same exit
  // velo at a grounder-level angle).
  var ROLLOUT_FT = 34;
  var ROLLOUT_EV_LOW = 40, ROLLOUT_EV_HIGH = 115;     // mph, weak contact to max
  var ROLLOUT_LA_LOW = -15, ROLLOUT_LA_HIGH = 50;     // degrees, steepest grounder to a high fly
  var ROLLOUT_MS = 320;

  function rolloutFraction(ev, la) {
    var evFrac = clamp((ev - ROLLOUT_EV_LOW) / (ROLLOUT_EV_HIGH - ROLLOUT_EV_LOW), 0, 1);
    var laFrac = clamp(1 - (la - ROLLOUT_LA_LOW) / (ROLLOUT_LA_HIGH - ROLLOUT_LA_LOW), 0, 1);
    return evFrac * (0.4 + 0.6 * laFrac);
  }

  // The infield dirt's edge, in feet from home, along a given HZ angle - the
  // far intersection of the ray from home with the dirt circle (centred
  // PITCHER_MOUND_FT out, radius INFIELD_DIRT_RADIUS_FT). Same law-of-cosines
  // form infieldDirtHtml uses for the foul-line intersections, generalised to
  // an arbitrary angle instead of just the two 45 degrees-off-center foul lines.
  var DIRT_CLEAR_MARGIN_FT = 3;   // a safe grounder rolls at least this far past the dirt's edge
  // These two archetypes are deliberately short (every bunt/infield_single
  // result's own depthMin/depthMax in result_diff_bands.csv stays well under
  // INFIELD_DIRT_RADIUS_FT) - a legged-out infield hit that stays on the
  // dirt is the realistic outcome there, not a bug to floor away.
  var STAYS_IN_INFIELD_ARCHETYPES = { bunt: 1, infield_single: 1 };
  function dirtEdgeFt(angleDeg) {
    var offset = (angleDeg - 45) * Math.PI / 180;
    var m = PITCHER_MOUND_FT, r = INFIELD_DIRT_RADIUS_FT;
    var s = Math.sin(offset);
    return m * Math.cos(offset) + Math.sqrt(Math.max(0, r * r - m * m * s * s));
  }

  // How far, in feet, a batted ball rolls past its bounce point before
  // anyone picks it up - shared by rolloutHtml (draws it) and throwHtml (an
  // infielder throws from where they fielded the ball, not from the bounce
  // point the ball trail happens to end its "land" keyframe at). Applies to
  // any ball still in play, not just grounders - a line-drive single that
  // lands well short of the fence still skids/bounces on before an
  // outfielder has it, same idea as a grounder's roll, just a smaller EV/LA
  // contribution at a higher launch angle.
  function groundBallRolloutFt(m, flight) {
    if (!flight) return 0;
    var isOut = (m.outs_after || 0) > (m.outs_before || 0);
    // Caught in the air, full stop - the ball was never on the ground to
    // begin with, so there's nothing to roll (isGrounder is a raw LA<4
    // threshold and unreliable here; see GROUND_ARCHETYPES above).
    if (isOut && CAUGHT_IN_AIR[flight.archetype]) return 0;
    var rollFt = rolloutFraction(flight.ev, flight.la) * ROLLOUT_FT;
    // Never let the rollout carry the ball past the fence - an inside-the-
    // park home run can land within ROLLOUT_FT of the wall, and the roll
    // distance alone doesn't know that.
    var maxReachFt = fenceAt(flight.angle) - 2;
    if (GROUND_ARCHETYPES[flight.archetype] && isOut) {
      // Nor past the fielder assigned to this HZ angle, on a ground ball out -
      // applyGroundBallFielderDepth already capped flight.distance itself if
      // the archetype distance alone overshot; this catches the case where
      // the base distance was short but distance+rollout would still
      // overshoot.
      var pos = HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];
      var depth = pos ? INFIELDER_DEPTH_FT[pos] : null;
      if (depth != null) {
        maxReachFt = Math.min(maxReachFt, depth);
        // A grounder out's roll bounds toward the REAL fielder standing at
        // `depth` (60ft for the pitcher up to 147ft for SS/2B), not a flat
        // ROLLOUT_FT - real infield depths span too wide a range for one
        // constant max to ever bridge the gap for a deep position (Alex's
        // catch: a modest shortstop-side grounder landed at ~87ft, could
        // only add another 34ft at best, stopping 44ft short of a real
        // 147ft-deep shortstop no matter how well it was hit). Scaling the
        // same contact-quality fraction against the gap-to-the-fielder
        // instead of a flat constant keeps weak contact fielded well in
        // front (charged in, still "not hit very far") while well-hit
        // contact closes most of the way to where the fielder actually is.
        rollFt = rolloutFraction(flight.ev, flight.la) * Math.max(0, depth - flight.distance);
      }
    } else if (!isOut && !STAYS_IN_INFIELD_ARCHETYPES[flight.archetype]) {
      // Any hit that reaches base safely, and isn't meant to be a short
      // infield hit, has to visibly clear the infield dirt, even when its
      // bounce point (the archetype distance alone) landed short of the
      // dirt's edge or right on it. Deliberately NOT gated on
      // GROUND_ARCHETYPES here - "grounder" itself only ever appears on OUT
      // results (every result mapped to it records at least one out), so
      // that gate meant this branch could never fire for any hit at all: a
      // "single" with a low, grounder-ish LA (like the one that exposed
      // this) was falling through with no floor whatsoever. The bounce
      // point itself is left alone (a squibber landing on the dirt is
      // realistic); only the roll-out is floored.
      var need = dirtEdgeFt(flight.angle) + DIRT_CLEAR_MARGIN_FT - flight.distance;
      if (need > rollFt) rollFt = need;
    }
    return Math.max(0, Math.min(rollFt, maxReachFt - flight.distance));
  }

  function rolloutHtml(flight, landEnd, dur, rollFt) {
    var rollPt = landingPoint(flight.distance + rollFt, flight.angle);
    var end = ftToSvg(rollPt.x, rollPt.y);
    var len = Math.hypot(end.x - landEnd.x, end.y - landEnd.y) || 1;
    var vars = "--fx:" + landEnd.x.toFixed(1) + "px;--fy:" + landEnd.y.toFixed(1) + "px;" +
               "--tx:" + end.x.toFixed(1) + "px;--ty:" + end.y.toFixed(1) + "px;" +
               "--rdelay:" + dur + "ms;--rdur:" + ROLLOUT_MS + "ms";
    var trailVars = "--len:" + len.toFixed(1) + "px;--rdelay:" + dur + "ms;--rdur:" + ROLLOUT_MS + "ms";
    return '<line class="ball-rollout-trail" x1="' + landEnd.x.toFixed(1) + '" y1="' + landEnd.y.toFixed(1) +
        '" x2="' + end.x.toFixed(1) + '" y2="' + end.y.toFixed(1) +
        '" style="' + trailVars + '"></line>' +
      '<circle class="ball-rollout" r="' + BALL_R + '" style="' + vars + '"></circle>';
  }

  function ballFlightHtml(m, flight) {
    if (!flight) return "";
    var home = SCENE_BASES.HOME;
    var cleared = flight.clearedFence;
    // An over-the-fence home run clears and fades near the wall rather than
    // continuing on to its full (often far off-canvas) computed distance.
    var targetFt = cleared ? landingPoint(FENCE_DEPTH_FT + 15, flight.angle) : { x: flight.x, y: flight.y };
    var end = ftToSvg(targetFt.x, targetFt.y);
    var dur = ballTravelMs(flight);
    var len = Math.hypot(end.x - home.x, end.y - home.y) || 1;
    var moveVars = "--fx:" + home.x + "px;--fy:" + home.y + "px;" +
                   "--tx:" + end.x.toFixed(1) + "px;--ty:" + end.y.toFixed(1) + "px;" +
                   "--dur:" + dur + "ms";
    var trailVars = "--len:" + len.toFixed(1) + "px;--dur:" + dur + "ms";
    // C1: red for an out, green for a hit - a play can be both (a sac fly),
    // and the ball itself having been caught wins that tie.
    var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
    var cls = (cleared ? " clear" : " land") + (flight.isGrounder ? " ground" : " air") +
              (wasOut ? " out" : " hit");
    // C4: any hit that stayed in the park carries a bit further than where
    // it landed before the fielder gets to it - a home run's a dead ball at
    // the wall, so it never rolls, like everything else that cleared the
    // fence. A ground ball out also gets a rollout (a fielded grounder still
    // bounces/skids up to the fielder), capped by
    // applyGroundBallFielderDepth/groundBallRolloutFt's own fielder-depth
    // logic so it never rolls the ball past whoever's actually fielding it.
    // A caught fly, line drive or pop out never rolls - it was caught in the
    // air, nothing to bounce - checked by archetype (CAUGHT_IN_AIR), not
    // flight.isGrounder, which is an independently-computed LA<4 flag that
    // can disagree with what the play actually was.
    var caughtInAir = wasOut && CAUGHT_IN_AIR[flight.archetype];
    var rollFt = (!cleared && !caughtInAir) ? groundBallRolloutFt(m, flight) : 0;
    var rollout = rollFt > 0 ? rolloutHtml(flight, end, dur, rollFt) : "";

    // Labels sit next to wherever the ball actually ends up: the rollout's
    // endpoint when there is one, otherwise the landing/catch point itself -
    // and only pop in once that rollout has actually finished playing, not
    // mid-roll. A cleared HR's true distance is often well off-canvas, so its
    // anchor is capped at the same fence+15 fade point the ball itself stops
    // at (targetFt/end above), not the real number.
    var short = (data.meta.result_short || {})[m.result] || m.result;
    var labelBaseDist = cleared ? FENCE_DEPTH_FT + 15 : flight.distance;
    var labelPt = landingPoint(labelBaseDist + rollFt, flight.angle);
    var labelSvg = ftToSvg(labelPt.x, labelPt.y);
    var labelDelay = dur + (rollFt > 0 ? ROLLOUT_MS : 0);
    // C2: a short abbreviation next to wherever the ball ended up.
    var label = '<text class="ball-label" x="' + labelSvg.x.toFixed(1) + '" y="' + labelSvg.y.toFixed(1) +
        '" dx="7" dy="-4" style="--delay:' + labelDelay + 'ms">' + escapeHtml(short) + "</text>";
    // C3: distance next to every home run's landing point, cleared-the-fence
    // ones included (the true number, even though the marker itself stops
    // at the wall for those) - stacked below the result label at the same
    // anchor, not on top of it.
    var distLabel = m.result === "HR" ?
      '<text class="ball-dist" x="' + labelSvg.x.toFixed(1) + '" y="' + labelSvg.y.toFixed(1) +
        '" dx="7" dy="11" style="--delay:' + labelDelay + 'ms">' + Math.round(flight.distance) + " ft</text>" : "";
    return '<line class="ball-trail' + cls + '" x1="' + home.x + '" y1="' + home.y +
        '" x2="' + end.x.toFixed(1) + '" y2="' + end.y.toFixed(1) +
        '" style="' + trailVars + '"></line>' +
      rollout +
      '<circle class="ball' + cls + '" r="' + BALL_R + '" style="' + moveVars + '"></circle>' +
      label + distLabel;
  }

  function fielderTokensHtml(flight) {
    // No fielder converge on a ball that left the park - that's the visible
    // difference between an over-the-fence and an inside-the-park home run.
    if (!flight || flight.clearedFence) return "";
    var anchor = FIELDER_ANCHORS_FT[flight.fielder];
    if (!anchor) return "";
    var from = ftToSvg(anchor.x, anchor.y);
    var to = ftToSvg(flight.x, flight.y);
    var vars = "--fx:" + from.x.toFixed(1) + "px;--fy:" + from.y.toFixed(1) + "px;" +
               "--tx:" + to.x.toFixed(1) + "px;--ty:" + to.y.toFixed(1) + "px;" +
               "--delay:" + ballTravelMs(flight) + "ms";
    return '<g class="fielder" style="' + vars + '"><circle r="' + FIELDER_R + '"></circle></g>';
  }

  // Archetypes caught in the air - no throw on a routine catch with nobody
  // on base to play behind (A1). Branch on archetype rather than
  // flight.isGrounder: isGrounder is an LA threshold and would misclassify a
  // low line drive as a grounder for this purpose.
  var CAUGHT_IN_AIR = { fly_ball: 1, pop_up: 1, line_drive: 1 };

  // Fly balls and pop-ups whose throw is scheduled anyway (SacF/DSacF/FO's
  // decorative "throw home" - see throwSchedule) never beat anyone: there is
  // no outfield-assist scenario in the situation list, so any runner shown
  // advancing on one of these already beat the throw, full stop - not an
  // inference, a fact about the current finite scenario set. Line drives are
  // excluded: LODP/LOTP are real outs off the caught ball (doubled off) and
  // must keep racing the runner like any other out.
  var TAG_THROW_ARCHETYPES = { fly_ball: 1, pop_up: 1 };

  // Lead-runner-first: real defensive priority always tries for the runner
  // furthest around the bases first (A2).
  var THROW_ORDER = ["HOME", "3B", "2B", "1B"];

  // Result codes whose name names the out's base outright - the generic
  // "next base past the runner's from" logic gets every one of these wrong,
  // because the FC family and LODP/LOTP produce zero OUT-bound moves or an
  // OUT move whose target is backwards (A3, B6). "OWN" means the base the
  // runner left, not the next one - used for a doubled-off runner on a
  // caught line drive, who is out for leaving too early, not for failing to
  // reach the next bag. FCLead is unreachable on current data (no diff-band
  // row); "2B" is an unverified default for "lead runner forced."
  var FORCED_OUT_BASE = {
    FC: "2B", FC3rd: "3B", FCH: "HOME", FCLead: "2B",
    DPH1: "HOME",
    LODP: "OWN", LOTP: "OWN",
  };

  // Ground-ball-family results where a genuinely forced runner leaves on
  // contact, same beat as a safe runner on a hit - not on the fielding beat
  // every other out-bound runner waits for. Fly-ball/tag-up results (SacF,
  // DFO, ...) are deliberately excluded: those runners wait for the catch
  // (catchMs below), the opposite of a force runner who has no choice but
  // to go immediately.
  var FORCE_TIMING_RESULTS = {
    BDP: 1, BFC: 1, BGO: 1, DP: 1, DP21: 1, DP31: 1, DPH1: 1, DPRun: 1,
    FC: 1, FC3rd: 1, FCH: 1, GO: 1, GORA: 1, SacB: 1,
  };

  // Whether a runner leaving fromBase is genuinely forced to the next base,
  // from the base-occupancy rule itself (obcBefore), not from the play's
  // result code: 1B is always forced (the batter always fills it behind
  // them); 2B only if 1B was occupied too; 3B only if both 1B and 2B were -
  // the force has to chain unbroken all the way back to the batter.
  function isForcedRunner(fromBase, obcBefore) {
    if (fromBase === "1B") return true;
    if (fromBase === "2B") return obcBefore.charAt(2) === "1";
    if (fromBase === "3B") return obcBefore.charAt(1) === "1" && obcBefore.charAt(2) === "1";
    return false;
  }

  // The FC family reaches first but deriveRunnerMoves pairs before/after
  // like-for-like for these codes (obc_before === obc_after), so it never
  // emits a BATTER move - the batter token would otherwise be missing
  // entirely and the play would render completely static (A3).
  var BATTER_REACHES_FIRST = { FC: 1, FC3rd: 1, FCH: 1, FCLead: 1 };
  // A strikeout has no ball flight to hang a result label off (ballFlightHtml
  // never runs), so its "K" gets drawn directly next to the batter token
  // instead, at the same beat the out itself resolves.
  // KCS is a strikeout that also caught a runner stealing on the same pitch -
  // the batter half of that combo gets the same "K" treatment as a plain K.
  var STRIKEOUT_RESULTS = { K: 1, AutoK: 1, KCS: 1 };

  // import_BRC.csv's optional ThrowOrder column (e.g. "1,2,3,4" or bare
  // "1234") - a base-by-base fielding sequence for one (result, obc_before,
  // outs_before) situation, straight from the data instead of guessed from
  // before/after diffing. Digits only: 1=1B, 2=2B, 3=3B, 4=home. Anything
  // that isn't one of those four characters (a comma, a space, a dash) is
  // just a separator and gets stripped, so "1,2,3,4" and "1234" parse
  // identically - no format the sheet ends up using is wrong.
  var THROW_ORDER_DIGIT_TO_BASE = { "1": "1B", "2": "2B", "3": "3B", "4": "HOME" };
  function parseThrowOrder(raw) {
    if (raw == null) return null;
    var digits = String(raw).replace(/[^1234]/g, "");
    if (!digits) return null;
    var bases = [];
    for (var i = 0; i < digits.length; i++) bases.push(THROW_ORDER_DIGIT_TO_BASE[digits.charAt(i)]);
    return bases;
  }

  // import_BRC.csv's optional per-position ThrowOrder_* columns key on "OF"
  // for any outfielder rather than LF/CF/RF individually - a play's throw
  // sequence rarely needs to distinguish which outfield third fielded it,
  // just infield vs outfield vs battery. flight.fielder is always a specific
  // LF/CF/RF, so this collapses those three down to the one column to check.
  function throwOrderKeyForPosition(pos) {
    return OF_POSITIONS[pos] ? "OF" : pos;
  }

  /* Every base a throw goes to, in real fielding order (A2/A3/B6). Which
     runner is out still comes entirely from obc_before/obc_after/outs_* via
     deriveRunnerMoves - this only decides the throw count and sequencing on
     top of that ground truth, never who is actually out. An explicit
     sequence from import_BRC.csv is authoritative and skips all the
     before/after-diff guessing below entirely - the heuristic is a fallback
     for situations that haven't been given an explicit sequence yet, not a
     second opinion to reconcile against one that has. Checked most-specific
     first: a per-position ThrowOrder_* (which fielder ends up credited,
     after any ExcludedPositions/DefaultPosition override, decides which
     column) beats the generic ThrowOrder column, which beats the heuristic. */
  function outThrowTargets(m, moves, flight) {
    if (!flight || flight.clearedFence) return [];
    var byPosition = m.throw_order_by_position;
    var posKey = throwOrderKeyForPosition(flight.fielder);
    var explicit = parseThrowOrder(byPosition && byPosition[posKey]) || parseThrowOrder(m.throw_order);
    if (explicit) return explicit;
    var caught = !!CAUGHT_IN_AIR[flight.archetype];
    var forced = FORCED_OUT_BASE[m.result];
    var targets = [];

    moves.forEach(function (mv) {
      if (mv.to !== "OUT") return;
      if (forced === "OWN") { targets.push(mv.from); return; }
      if (forced) { targets.push(forced); return; }
      targets.push(NEXT_BASE[mv.from] || mv.from);
    });

    // Outs the data records that no move accounts for are the batter's -
    // deriveRunnerMoves only tracks runners already on base (F3) - EXCEPT on
    // the FC family, where the batter is known to have reached first safely
    // (BATTER_REACHES_FIRST) and the unaccounted out is really some other
    // runner deriveRunnerMoves has no move object for at all, redirected via
    // the same forced-base override the (nonexistent) OUT move would have
    // used above.
    var recorded = Math.max(0, (m.outs_after || 0) - (m.outs_before || 0));
    var unaccounted = recorded - targets.length;
    var batterReached = moves.some(function (mv) { return mv.from === "BATTER"; });
    if (unaccounted > 0 && !batterReached && !caught) {
      targets.push(BATTER_REACHES_FIRST[m.result] ? (forced || "1B") : "1B");
    }

    // Ground-truth invariant, restated for throws: never show more OUT-bound
    // throws than outs actually recorded, less the batter's own out on a
    // caught ball. deriveRunnerMoves never models that catch as a move at
    // all (a caught batter never "reaches", so it tracks nothing for them) -
    // without subtracting it here, a routine fly/pop out that happens to end
    // the half-inning "spent" its one real out on a phantom stranded-runner
    // OUT move instead (the same half-inning-ending obc-reset artifact noted
    // above), throwing to a base nobody was actually forced at. deriveRunnerMoves'
    // own documented limitation (its docstring - tangled force plays, not
    // always the furthest-back runner) is a separate, pre-existing thing;
    // capping here is the fix that belongs in throw logic, not there.
    var battersOwnOut = caught ? 1 : 0;
    var seen = {};
    var sorted = targets.filter(function (b) {
      if (seen[b]) return false;
      seen[b] = 1;
      return true;
    }).sort(function (a, b) { return THROW_ORDER.indexOf(a) - THROW_ORDER.indexOf(b); })
      .slice(0, Math.max(0, recorded - battersOwnOut));

    // A caught-in-air play with a runner tagging up gets the tag-up throw -
    // the whole drama of a sac fly (A1/F2). Added after the cap above: the
    // run scores safely, so this throw is never one of "the outs recorded"
    // and must not be capped away by it.
    if (!sorted.length && caught && moves.some(function (mv) { return mv.scored; })) {
      sorted.push("HOME");
    }

    return sorted;
  }

  /* Pure schedule (A4/A5): throw i originates at the ball's landing point;
     throw i+1 relays from throw i's target base. Kept separate from the
     rendering so the timing race against the runner can be asserted rather
     than eyeballed - see ball_flight_test.py. */
  function throwSchedule(m, moves, flight) {
    var targets = outThrowTargets(m, moves, flight);
    if (!targets.length) return [];

    // A fly ball/pop-up's throw (SacF/DSacF/FO's decorative "throw home
    // anyway" - outThrowTargets appends it, or an explicit ThrowOrder
    // describes the same throw) never beats anyone - see TAG_THROW_ARCHETYPES.
    // The ball-fielding-only schedule below has no idea the runner had to
    // wait out the catch/tag-up before moving at all, so it drew the throw
    // in a good 600ms+ before the runner had actually crossed the plate.
    // Anchor this one on the slowest safe runner's own arrival instead.
    if (flight && TAG_THROW_ARCHETYPES[flight.archetype]) {
      var catchMs = ballTravelMs(flight);
      var runnerArrival = 0;
      moves.forEach(function (mv) {
        if (mv.to === "OUT") return;
        var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
        var endOrd = mv.scored ? 4 : BASE_ORDINAL[mv.to];
        if (startOrd == null || endOrd == null || endOrd <= startOrd) return;
        var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
        runnerArrival = Math.max(runnerArrival, catchMs + TAG_UP_MS + (RUN_LEG_MS[legs] || 0));
      });
      var tagStart = Math.max(0, runnerArrival + TAG_THROW_MARGIN_MS - THROW_DRAW_MS);
      return targets.map(function (b, i) {
        var start = tagStart + i * THROW_STAGGER_MS;
        return { base: b, startMs: start, endMs: start + THROW_DRAW_MS };
      });
    }

    // A rolling grounder isn't fielded until it stops rolling - the throw
    // has to wait out that extra beat too, or it'd draw from a spot the ball
    // hasn't visibly reached yet (see throwHtml's fieldPt).
    var rollMs = groundBallRolloutFt(m, flight) > 0 ? ROLLOUT_MS : 0;
    var base = ballTravelMs(flight) + rollMs + THROW_DELAY_MS;
    return targets.map(function (b, i) {
      var start = base + i * THROW_STAGGER_MS;
      return { base: b, startMs: start, endMs: start + THROW_DRAW_MS };
    });
  }

  // The .out-to-first keyframe's 47.06% stop is where the batter reaches
  // first (A4) - kept as one named function so the throw-beats-runner
  // assertion has a single source of truth to check against.
  function batterFirstArrivalMs() {
    return RUNNER_LEAD_MS + 0.4706 * 1700;
  }

  function throwHtml(m, flight, moves) {
    var schedule = throwSchedule(m, moves, flight);
    if (!schedule.length) return "";
    // A grounder is fielded wherever it stops rolling, not at its bounce
    // point - the throw has to originate there, or it visibly starts from
    // empty grass short of the fielder.
    var rollFt = groundBallRolloutFt(m, flight);
    var fieldPt = rollFt > 0 ? landingPoint(flight.distance + rollFt, flight.angle) : flight;
    var origin = ftToSvg(fieldPt.x, fieldPt.y);
    return schedule.map(function (t) {
      var to = t.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[t.base];
      if (!to) return "";
      var len = Math.hypot(to.x - origin.x, to.y - origin.y) || 1;
      var vars = "--len:" + len.toFixed(1) + "px;--delay:" + t.startMs + "ms;--draw:" + THROW_DRAW_MS + "ms";
      var line = '<line class="throw-line" x1="' + origin.x.toFixed(1) + '" y1="' + origin.y.toFixed(1) +
        '" x2="' + to.x + '" y2="' + to.y + '" style="' + vars + '"></line>';
      origin = to;   // next throw relays from here (A5)
      return line;
    }).join("");
  }

  // ── Stolen base / caught stealing throw (B3-c+a) ──────────────────────────
  // These plays never get a ball flight (they're in FLIGHT_EXCLUDED), so the
  // batted-ball throw pipeline above never runs for them - this is a small,
  // parallel one: a catcher-to-base throw, timed against the SAME runner
  // token the diamond already animates for these plays (a plain legs1 safe
  // advance for a steal, an out-to-base token for a caught stealing - both
  // pre-existing, untouched by this addition).
  // SB32/SB42/SB43/SB432: multi-runner steals, safe (e.g. SB42 = steal of
  // home and 2nd on the same play). KCS: a strikeout that also caught a
  // runner stealing on the same pitch - caught, not safe.
  var STEAL_SAFE_CODES = { SB: 1, SB2: 1, SB3: 1, SB4: 1, SB32: 1, SB42: 1, SB43: 1, SB432: 1 };
  var STEAL_CAUGHT_CODES = { CS: 1, CS2: 1, CS3: 1, CS4: 1, KCS: 1 };
  var STEAL_THROW_MARGIN_MS = 200;  // CS: throw arrives this early; SB: this late

  // The runner token's own "reaches the base" moment - RUN_LEG_MS[1] (800ms)
  // is both a plain legs1 advance's full duration AND (per batterFirstArrivalMs's
  // 47.06%-of-1700ms note above) the out-to-base keyframe's first-leg
  // checkpoint, so one formula covers both a safe steal and a caught one.
  function stealRunnerArrivalMs(isCaught, runDelay, outDelay) {
    return (isCaught ? outDelay : runDelay) + RUN_LEG_MS[1];
  }

  function stealThrowTarget(m, moves) {
    var caught = !!STEAL_CAUGHT_CODES[m.result];
    var safe = !!STEAL_SAFE_CODES[m.result];
    if (!caught && !safe) return null;
    var mv = caught
      ? moves.filter(function (x) { return x.to === "OUT"; })[0]
      : moves.filter(function (x) { return x.to !== "OUT" && x.to !== x.from; })[0];
    if (!mv) return null;
    // import_BRC.csv's aN column names the base this runner was actually
    // making for, straight from the data - preferred over guessing "the next
    // base past where they started" (NEXT_BASE), which has no way to know
    // about a runner thrown out attempting something other than the single
    // next bag. Falls back to the old guess only when a row hasn't been
    // given an explicit assist yet.
    var base = caught ? (mv.assist || NEXT_BASE[mv.from] || mv.from) : mv.to;
    return { base: base, caught: caught, delay: !!mv.delay };
  }

  // The catcher, unless import_BRC.csv's ExcludedPositions/DefaultPosition
  // say otherwise for this situation - e.g. excluding "C" with a default of
  // "P" for a steal of home, where the pitcher (not the catcher) makes the
  // throw. Same two columns the batted-ball position override reads
  // (applyPositionOverride above), just checked against steals' fixed
  // "the catcher starts with the ball" baseline instead of a physics-
  // computed fielder - there's no ball flight on a steal to compute one from.
  function stealThrowOrigin(m) {
    var basePos = "C";
    var excluded = m.excluded_positions;
    var def = m.default_position;
    if (excluded && excluded.length && def && excluded.indexOf(basePos) !== -1) {
      return def;
    }
    return basePos;
  }

  function stealThrowHtml(m, moves, runDelay, outDelay) {
    var target = stealThrowTarget(m, moves);
    if (!target) return "";
    var to = target.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[target.base];
    if (!to) return "";
    var originAnchor = FIELDER_ANCHORS_FT[stealThrowOrigin(m)] || FIELDER_ANCHORS_FT.C;
    var from = ftToSvg(originAnchor.x, originAnchor.y);
    // Explicit "delay" flag (e.g. KCS - a strikeout that also catches a
    // runner stealing): the steal doesn't start on the pitch like a normal
    // attempt, it starts once the K itself resolves (outDelay already is
    // that moment - there's no ball flight on any steal play to hang a
    // separate "catch" instant off), plus the same beat any other tag-up
    // runner already waits after a catch (TAG_UP_MS) - see delayedStartMs's
    // sibling logic in sceneFieldHtml.
    var effOutDelay = target.delay ? outDelay + TAG_UP_MS : outDelay;
    var arrival = stealRunnerArrivalMs(target.caught, runDelay, effOutDelay);
    var arrive = target.caught ? arrival - STEAL_THROW_MARGIN_MS : arrival + STEAL_THROW_MARGIN_MS;
    var start = Math.max(0, arrive - THROW_DRAW_MS);
    var len = Math.hypot(to.x - from.x, to.y - from.y) || 1;
    var vars = "--len:" + len.toFixed(1) + "px;--delay:" + start + "ms;--draw:" + THROW_DRAW_MS + "ms";
    return '<line class="throw-line steal-throw" x1="' + from.x.toFixed(1) + '" y1="' + from.y.toFixed(1) +
      '" x2="' + to.x.toFixed(1) + '" y2="' + to.y.toFixed(1) + '" style="' + vars + '"></line>';
  }

  // Exposed for the Playwright pure-function test harness only
  // (ball-flight-plan.md Stage 3b, extended by the refinements round's Stage
  // T) - never read by the page itself. Placed here, after every function and
  // constant it references, since this is plain top-to-bottom script
  // execution - var bindings (unlike function declarations) are not
  // initialised until their own assignment runs.
  window.KMFlight = {
    signedCirc: signedCirc, firstTwo: firstTwo, onesDigit: onesDigit, lastDigit: lastDigit,
    effectiveHand: effectiveHand, landingPoint: landingPoint, clampToFence: clampToFence,
    nearestFielder: nearestFielder, flightParams: flightParams, fenceAt: fenceAt,
    launchAngleFor: launchAngleFor,
    FENCE_DEPTH_FT: FENCE_DEPTH_FT,
    ordinal: ordinal, deriveRunnerMoves: deriveRunnerMoves,
    outThrowTargets: outThrowTargets, throwSchedule: throwSchedule,
    batterFirstArrivalMs: batterFirstArrivalMs,
    stealThrowTarget: stealThrowTarget, stealRunnerArrivalMs: stealRunnerArrivalMs,
    stealThrowOrigin: stealThrowOrigin,
    THROW_LEAD_MS: THROW_LEAD_MS, THROW_DELAY_MS: THROW_DELAY_MS,
    THROW_DRAW_MS: THROW_DRAW_MS, THROW_STAGGER_MS: THROW_STAGGER_MS,
    GROUNDER_ROLL_MS: GROUNDER_ROLL_MS, RUNNER_LEAD_MS: RUNNER_LEAD_MS,
    groundBallRolloutFt: groundBallRolloutFt,
    applyGroundBallFielderDepth: applyGroundBallFielderDepth,
    dirtEdgeFt: dirtEdgeFt, CAUGHT_IN_AIR: CAUGHT_IN_AIR,
    TAG_THROW_ARCHETYPES: TAG_THROW_ARCHETYPES,
    GROUND_ARCHETYPES: GROUND_ARCHETYPES,
    parseThrowOrder: parseThrowOrder,
    applyPositionOverride: applyPositionOverride,
    throwOrderKeyForPosition: throwOrderKeyForPosition,
    OF_POSITIONS: OF_POSITIONS, FIELDER_ANCHORS_FT: FIELDER_ANCHORS_FT,
    TAG_UP_MS: TAG_UP_MS, TAG_THROW_MARGIN_MS: TAG_THROW_MARGIN_MS,
    RUN_LEG_MS: RUN_LEG_MS, BASE_ORDINAL: BASE_ORDINAL,
    ballTravelMs: ballTravelMs,
  };

  /* Replaces the old tightly-cropped infield-only diamond. Same runner-token
     architecture (deriveRunnerMoves/basepathWaypoints/RUN_LEG_MS, untouched),
     staged on a bigger field canvas with the ball flight, a converging
     fielder and an out-choreography walk to the dugout layered underneath
     the runners (ball-flight-plan.md Stage 4/6). `flight` is
     flightParams(m, data.meta.flight) or null for an out-of-scope result. */
  function sceneFieldHtml(m, flight) {
    var before = String(m.obc_before || "000");
    var after = String(m.obc_after || "000");
    // import_BRC.csv's B/r1/r2/r3 columns, decoded server-side into the
    // exact per-runner outcome for this situation (key_moments_build.py's
    // runner_moves) - trusted completely over the diff-based guess below
    // whenever it's present. deriveRunnerMoves only runs at all for
    // situations that haven't been given explicit data yet.
    var moves = m.runner_moves || deriveRunnerMoves(before, after, m.runs || 0);
    // Fallback for the one still-common gap: DPH1 always starts from bases
    // loaded and always removes the MOST advanced runner (3B, out at home),
    // not the least advanced one - the opposite of deriveRunnerMoves' most-
    // advanced-pairs-with-most-advanced assumption, which instead pairs
    // 3B->3B and 2B->2B as if neither runner moved and blames the 1B runner
    // for an out that never involved them ("DPH1's heuristic mismatch",
    // already flagged where forcedBase is computed below). Only applies
    // when m.runner_moves is missing (the inning-ending DPH1 variant's
    // r1/r2/r3 aren't filled in yet) - once that row is completed too, this
    // becomes dead code on its own, no flag to flip.
    if (!m.runner_moves && m.result === "DPH1" && before === "111") {
      moves = [
        { from: "3B", to: "OUT", scored: false },
        { from: "2B", to: "3B", scored: false },
        { from: "1B", to: "2B", scored: false },
      ];
    }
    var dugoutFt = dugoutFor(m);
    var dugoutSvg = ftToSvg(dugoutFt.x, dugoutFt.y);

    // Which OUT-bound moves are corroborated by an actual throw, batted-ball
    // (outThrowTargets, already capped to real outs) or steal (stealThrowTarget) -
    // used below to tell a real force/tag-out from a runner simply stranded
    // when the half-inning ended (deriveRunnerMoves' obc-reset artifact,
    // already noted above outThrowTargets - on a strikeout or walk this was
    // showing stranded runners advancing toward a base before "being out",
    // when nothing actually happened to them at all).
    var realOutTargets = outThrowTargets(m, moves, flight);
    var stealOut = stealThrowTarget(m, moves);

    // Safe/scoring runners lead off 150ms after slide mount, behind the ball
    // (Stage 4e) - but only when there was a ball to lead them. An out's walk
    // to the dugout begins on its own later beat, plus however long the ball
    // stayed airborne/rolling first. A runner tagging up on a caught fly is a
    // third case (B5): they cannot leave before the catch, so their delay is
    // the ball's hang time plus a beat, not the shared lead-off delay.
    var runDelay = flight ? RUNNER_LEAD_MS : 0;
    var outDelay = (flight ? ballTravelMs(flight) : 0) + OUT_BEAT_MS;
    var catchMs = flight && CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
    // Explicit "delay" flag (import_BRC.csv, e.g. KCS): this move doesn't
    // start until the ball is caught by the fielder - literally
    // ballTravelMs(flight) for a play with a flight, or outDelay itself for a
    // no-flight play like a strikeout (the catcher receiving strike three IS
    // the "catch" there) - then the same beat any other tag-up runner already
    // waits after a catch (TAG_UP_MS), so it reads as a beat AFTER the play's
    // own resolution, not simultaneous with it.
    var delayedStartMs = (flight ? ballTravelMs(flight) : outDelay) + TAG_UP_MS;
    // A caught stealing that ends the half inning strands whoever else was on
    // base at that moment - deriveRunnerMoves' obc-reset artifact turns their
    // move into an uncorroborated "OUT" too (falls back to the plain
    // out-walk below, forcedBase stays null), but they weren't actually
    // caught on this play and must not start for the dugout until the
    // runner who WAS caught is actually tagged, not on the same beat.
    var stealOutDelay = stealOut && stealOut.delay ? delayedStartMs : outDelay;
    var stealOutResolveMs = stealOut && stealOut.caught
      ? stealRunnerArrivalMs(true, runDelay, stealOutDelay) + TAG_UP_MS
      : 0;
    // I7: the slowest arriving safe/scoring runner - the base plates' gold
    // "post-play occupancy" fill is delayed until this moment, so a base
    // does not light up before any runner has actually reached it.
    var maxArrival = 0;

    /* Two nested groups per token, deliberately: the outer one owns position
       (the multi-leg basepath run) and the inner one owns opacity and scale
       (fading out, the batter appearing, the flash on scoring). Both would
       otherwise be competing to animate `transform` on one element, and only
       one of them could win. */
    var tokens = moves.map(function (mv) {
      var from = mv.from === "BATTER" ? SCENE_BASES.HOME : SCENE_BASES[mv.from];
      var isOut = mv.to === "OUT";
      if (!from || (!isOut && !SCENE_BASES[mv.to] && !mv.scored)) return "";
      // I8: an out-bound runner travels toward the base the throw actually
      // targeted before turning red, same override table outThrowTargets
      // uses - but only when a real throw actually corroborates it (realOutTargets/
      // stealOut above). A move that isn't corroborated is a runner simply
      // stranded when the half-inning ended, not one who was forced or
      // tagged out, and falls back to the plain straight-to-dugout walk.
      // basepathWaypoints' own end<=start guard separately suppresses the
      // partial advance for a backward-relative target (a doubled-off runner
      // on a caught line drive, B6 - deferred) or one already at that base -
      // those are real outs, just ones with no forward leg to show.
      var forcedBase = null;
      if (isOut) {
        var forced = FORCED_OUT_BASE[m.result];
        var candidate = forced === "OWN" ? mv.from
          : (forced || NEXT_BASE[mv.from] || mv.from);
        var corroborated = realOutTargets.indexOf(candidate) !== -1 ||
          (stealOut && stealOut.caught && candidate === stealOut.base);
        forcedBase = corroborated ? candidate : null;
      }
      // Explicit "retreat" flag (import_BRC.csv, e.g. LODP - a runner
      // doubled off a caught line drive): this runner didn't just vanish
      // from their base or get forced ahead - they broke for aN (the assist
      // column, same base-token format as rN), then had to scramble back once
      // the ball was actually caught. The path shows them getting about
      // halfway there and returning, instead of the plain forced-advance
      // shape below. Applied verbatim per the row's own flag only - not
      // inferred onto any other out, and never onto a safe move (Alex's
      // call: generalising retreat to a safe "near miss" scramble is a
      // separate, not-yet-designed animation).
      var assistBase = mv.assist === "HOME" ? SCENE_BASES.HOME : (mv.assist ? SCENE_BASES[mv.assist] : null);
      var useRetreat = isOut && mv.retreat && !!assistBase;
      // basepathWaypoints' own ordinal math treats HOME as 0 - "before" every
      // other base - so an out-bound path whose forcedBase is HOME needs the
      // same end=4 wraparound a genuine score gets, or it reads as
      // end(0)<=start and returns no path at all. Passing forcedBase==="HOME"
      // here only feeds that ordinal - mv.scored (used for the CSS "score"
      // flash below) is untouched, since this runner didn't actually score.
      var path = useRetreat
        ? [{ x: (from.x + assistBase.x) / 2, y: (from.y + assistBase.y) / 2 }]
        : isOut
          ? (forcedBase ? basepathWaypoints(mv.from, forcedBase, forcedBase === "HOME") : [])
          : basepathWaypoints(mv.from, mv.to, mv.scored);
      // A safe runner (reached or held their forced base, never put out)
      // whose own half-inning ends on this very play has nobody left on the
      // bases between innings either - after reaching/holding, they walk
      // off to the dugout too, same as an out would, just without ever
      // turning red. A true "hold" (no real advance, path empty) still
      // needs a --p1 waypoint for the shared keyframe - their own current
      // position is a harmless no-op "leg".
      var strandedSafe = !isOut && !mv.scored && !!m.is_half_inning_final;
      if (strandedSafe && !path.length) path = [from];
      var end = isOut ? dugoutSvg : (strandedSafe ? dugoutSvg : (path.length ? path[path.length - 1] : from));
      var legs = Math.min(path.length, RUN_LEG_MS.length - 1);
      // A genuinely forced runner (isForcedRunner, on one of the ground-ball
      // results where that applies) leaves on contact - same beat as a safe
      // runner on a hit - because they have no choice but to vacate the
      // base immediately, regardless of how the play develops. Every other
      // out-bound runner waits for the ball to actually be fielded first.
      // Deliberately independent of forcedBase (corroboration/path): a
      // forced runner starts running the instant the ball is hit whether or
      // not the data tells us exactly where they ended up - not knowing
      // their fate for certain doesn't mean they didn't react to the force.
      var forcedOnContact = FORCE_TIMING_RESULTS[m.result] && isForcedRunner(mv.from, before);
      // B5: a runner tagging up on a caught fly cannot leave before the
      // catch, whether they end up scoring (a sac fly) or just advancing a
      // base (e.g. 2nd to 3rd on a deep flyout with nobody home) - catchMs
      // is already 0/falsy for anything not caught in the air, so this
      // applies to every safe move on those plays, not scoring ones only.
      var mvDelay = mv.delay
        ? delayedStartMs
        : isOut
          ? (forcedOnContact ? runDelay : (forcedBase ? outDelay : Math.max(outDelay, stealOutResolveMs)))
          : (catchMs ? catchMs + TAG_UP_MS : runDelay);
      // stranded-to-dugout's own keyframe ignores --dur (a fixed 1700ms,
      // matching the out choreography's own run-then-leave timing exactly)
      // - RUN_LEG_MS[legs] here would understate how long the token is
      // actually on screen for maxArrival's "don't light a base up before
      // everyone's actually arrived" purpose.
      if (!isOut) maxArrival = Math.max(maxArrival, mvDelay + (strandedSafe ? 1700 : (RUN_LEG_MS[legs] || 0)));
      var vars = "--fx:" + from.x + "px;--fy:" + from.y + "px;" +
                 "--tx:" + end.x + "px;--ty:" + end.y + "px;" +
                 "--rdelay:" + mvDelay + "ms;";
      path.forEach(function (p, i) {
        vars += "--p" + (i + 1) + "x:" + p.x + "px;--p" + (i + 1) + "y:" + p.y + "px;";
      });
      vars += "--dur:" + (RUN_LEG_MS[legs] || 0) + "ms";
      // A put-out runner with somewhere to be forced travels there first,
      // THEN turns red, THEN walks a straight line to the dugout (Stage
      // 6a/6b, generalised by I8). legsN is a safe-runner-only class - the
      // out choreography's own keyframe owns --p1 instead, so isOut never
      // gets a legsN class alongside it.
      var outCls = isOut ? (useRetreat ? " out-retreat" : (path.length ? " out-to-base" : " out-walk")) : "";
      var cls = "rn" + (legs && !isOut && !strandedSafe ? " legs" + legs : "") + outCls +
                (strandedSafe ? " stranded-to-dugout" : "") +
                (mv.scored ? " score" : "") + (mv.from === "BATTER" ? " batter" : "");
      return '<g class="' + cls + '" style="' + vars + '">' +
        '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
    }).join("");

    // deriveRunnerMoves only tracks RUNNERS, so a play where the batter never
    // reached base yields no token for them at all. Three shapes: the FC
    // family (BATTER_REACHES_FIRST - safe at first, A3), a batted-ball out
    // (Stage 6b) or no batted ball at all (Stage 6c) - except on a
    // no-plate-appearance play (a steal, a caught stealing, a balk), where the
    // batter did nothing at all and gets no token, no walk to the dugout.
    var noPa = (data.meta.flight && data.meta.flight.no_pa) || [];
    var batterReached = moves.some(function (mv) { return mv.from === "BATTER"; });
    if (!batterReached && noPa.indexOf(m.result) === -1) {
      var h = SCENE_BASES.HOME;
      if (BATTER_REACHES_FIRST[m.result]) {
        // A3/F5: the FC family reaches first safely - someone else was
        // forced out. deriveRunnerMoves pairs obc_before/after like-for-like
        // for these codes and never emits a BATTER move, so without this the
        // batter is invisible and the whole play renders static. Plain safe
        // token, not the out choreography - the batter isn't out here.
        var fc1 = SCENE_BASES["1B"];
        var fcVars = "--fx:" + h.x + "px;--fy:" + h.y + "px;" +
                     "--tx:" + fc1.x + "px;--ty:" + fc1.y + "px;" +
                     "--p1x:" + fc1.x + "px;--p1y:" + fc1.y + "px;" +
                     "--rdelay:" + runDelay + "ms;--dur:" + RUN_LEG_MS[1] + "ms";
        tokens += '<g class="rn legs1 batter" style="' + fcVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
        maxArrival = Math.max(maxArrival, runDelay + RUN_LEG_MS[1]);
      } else if (flight) {
        // 6b: runs to first on the normal basepath, THEN turns red, THEN
        // returns to the dugout - what makes a groundout read differently
        // from a strikeout.
        var p1 = SCENE_BASES["1B"];
        var voVars = "--fx:" + h.x + "px;--fy:" + h.y + "px;" +
                     "--p1x:" + p1.x + "px;--p1y:" + p1.y + "px;" +
                     "--tx:" + dugoutSvg.x + "px;--ty:" + dugoutSvg.y + "px;" +
                     "--rdelay:" + runDelay + "ms";
        tokens += '<g class="rn out-to-first batter" style="' + voVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
      } else {
        // 6c: no batted ball (strikeout and friends) - straight from home to
        // the dugout, no trip to first.
        var owVars = "--fx:" + h.x + "px;--fy:" + h.y + "px;" +
                     "--tx:" + dugoutSvg.x + "px;--ty:" + dugoutSvg.y + "px;" +
                     "--rdelay:" + outDelay + "ms";
        tokens += '<g class="rn out-walk batter" style="' + owVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
        if (STRIKEOUT_RESULTS[m.result]) {
          var kShort = (data.meta.result_short || {})[m.result] || m.result;
          tokens += '<text class="ball-label" x="' + h.x + '" y="' + h.y +
            '" dx="10" dy="-6" style="--delay:' + outDelay + 'ms">' + escapeHtml(kShort) + "</text>";
        }
      }
    }

    // B3: a caught-stealing or stolen-base attempt gets a catcher throw and a
    // tag-at-the-bag flash on whichever base was in play - it's the one play
    // type where "the ball beat (or didn't beat) the runner" is the whole
    // story, and it never gets a ball flight to hang a throw off otherwise.
    var stealTarget = stealThrowTarget(m, moves);
    var stealFlashDelay = stealTarget
      ? stealRunnerArrivalMs(stealTarget.caught, runDelay, stealTarget.delay ? delayedStartMs : outDelay)
      : 0;

    // Base plates show post-play occupancy so the field still reads
    // correctly once the tokens have settled. I7: the gold fill is delayed
    // until maxArrival, the slowest arriving runner - otherwise a base lights
    // up before anyone has visibly reached it.
    var plates = ["3B", "2B", "1B"].map(function (b, i) {
      var occupied = after[i] === "1";
      var p = SCENE_BASES[b];
      var flashCls = stealTarget && stealTarget.base === b
        ? (stealTarget.caught ? " steal-out" : " steal-safe") : "";
      return '<rect class="dm-base' + (occupied ? " on" : "") + flashCls +
        '" style="--blight:' + maxArrival + 'ms;--sflash:' + stealFlashDelay + 'ms' +
        '" x="-' + BASE_R + '" y="-' + BASE_R + '" width="' + (BASE_R * 2) + '" height="' + (BASE_R * 2) +
        '" rx="1.5" transform="translate(' +
        p.x.toFixed(1) + "," + p.y.toFixed(1) + ') rotate(45)"></rect>';
    }).join("");

    /* The batting team's mark is painted ON the infield, inside the SVG and
       above the field fill - as an HTML layer underneath it, the opaque
       .dm-field simply covered it. It sits below the bases and tokens so the
       runners always stay the thing you look at. Centred on the diamond's
       own centroid so it scales with the (real 90ft-basepath-scaled)
       infield rather than a hardcoded position. */
    var batAbbr = m.batting_is_home ? m.home_team_abbr : m.away_team_abbr;
    var markUrl = teamLogoUrl(batAbbr);
    var centroid = {
      x: (SCENE_BASES.HOME.x + SCENE_BASES["1B"].x + SCENE_BASES["2B"].x + SCENE_BASES["3B"].x) / 4,
      y: (SCENE_BASES.HOME.y + SCENE_BASES["1B"].y + SCENE_BASES["2B"].y + SCENE_BASES["3B"].y) / 4,
    };
    var markSize = BASE_DIST_FT / FT_PER_UNIT;   // roughly one basepath-length square
    var watermark = markUrl
      ? '<image class="dm-mark" href="' + escapeHtml(markUrl) +
        '" x="' + (centroid.x - markSize / 2).toFixed(1) + '" y="' + (centroid.y - markSize / 2).toFixed(1) +
        '" width="' + markSize.toFixed(1) + '" height="' + markSize.toFixed(1) +
        '" preserveAspectRatio="xMidYMid meet"></image>'
      : "";
    // Runners wear the batting team's colour - they are that team's runners.
    // Scoring and out tokens override it, since those states matter more than
    // whose they are.
    var runHex = teamColor(batAbbr);
    var h = SCENE_BASES.HOME;
    var plateR = HOME_PLATE_R;
    // Point down, toward the backstop - a home plate seen from above has its
    // flat edge facing the pitcher, not its apex. Sized off BASE_R so it
    // scales with everything else on the infield.
    var platePath = "M" + (h.x - plateR).toFixed(1) + "," + (h.y - plateR * 1.15).toFixed(1) +
      " L" + (h.x + plateR).toFixed(1) + "," + (h.y - plateR * 1.15).toFixed(1) +
      " L" + (h.x + plateR).toFixed(1) + "," + h.y.toFixed(1) +
      " L" + h.x.toFixed(1) + "," + (h.y + plateR * 1.15).toFixed(1) +
      " L" + (h.x - plateR).toFixed(1) + "," + h.y.toFixed(1) + " Z";
    // Layering bottom to top, per Stage 4c: grass, fence, infield dirt circle,
    // infield fill, foul lines, base plates, watermark, fielder, ball trail +
    // ball, throw, runner tokens. Runner tokens stay on top - they're what
    // the viewer follows.
    return '<div class="scene-diamond-wrap">' +
      '<svg class="scene-diamond" viewBox="0 0 ' + FIELD_W + " " + FIELD_H + '" aria-hidden="true"' +
        (runHex ? ' style="--rn-fill:' + escapeHtml(runHex) + '"' : "") + ">" +
        '<rect class="dm-grass" x="0" y="0" width="' + FIELD_W + '" height="' + FIELD_H + '"></rect>' +
        '<path class="dm-fence" d="' + fencePathD() + '"></path>' +
        infieldDirtHtml() +
        '<path class="dm-field" d="M' + h.x.toFixed(1) + "," + h.y.toFixed(1) +
          " L" + SCENE_BASES["1B"].x.toFixed(1) + "," + SCENE_BASES["1B"].y.toFixed(1) +
          " L" + SCENE_BASES["2B"].x.toFixed(1) + "," + SCENE_BASES["2B"].y.toFixed(1) +
          " L" + SCENE_BASES["3B"].x.toFixed(1) + "," + SCENE_BASES["3B"].y.toFixed(1) + ' Z"></path>' +
        '<path class="dm-foul-line" d="' + foulLineD(0) + '"></path>' +
        '<path class="dm-foul-line" d="' + foulLineD(90) + '"></path>' +
        plates +
        watermark +
        '<path class="dm-plate" d="' + platePath + '"></path>' +
        (SHOW_FIELDER_TOKENS ? fielderTokensHtml(flight) : "") +
        ballFlightHtml(m, flight) +
        throwHtml(m, flight, moves) +
        stealThrowHtml(m, moves, runDelay, outDelay) +
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

  // Only the 1.5 (hot-threshold) tick survives as a marking (Alex's call -
  // the gauge previously also carried a gray dash at the LI 1.0 apex and a
  // soft red background band from the threshold onward; both removed as
  // clutter). The fill itself now carries the granularity instead, as a
  // 4-stop gradient - deep blue (calm) through light blue, light red, to a
  // vivid red (hot) - so intensity reads continuously along the bar rather
  // than a single flat colour that only ever flips at the hot threshold.
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
    return '<div class="scene-meter' + (hot ? " hot" : "") + '">' +
      '<svg viewBox="0 0 120 72" aria-hidden="true">' +
        "<defs><linearGradient id=\"levGradient\" x1=\"14\" y1=\"60\" x2=\"106\" y2=\"60\" gradientUnits=\"userSpaceOnUse\">" +
          '<stop offset="0%" class="mt-stop-cold"></stop>' +
          '<stop offset="50%" class="mt-stop-cool"></stop>' +
          '<stop offset="' + (tFrac * 100).toFixed(1) + '%" class="mt-stop-warm"></stop>' +
          '<stop offset="100%" class="mt-stop-hot"></stop>' +
        "</linearGradient></defs>" +
        '<path class="mt-track" d="M14,60 A46,46 0 0 1 106,60"></path>' +
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
        // Home team on the bottom (Alex's ask): higher home win% -> larger y
        // -> lower on screen, since SVG y grows downward. Was (1 - hw),
        // putting a home-favored line near the top instead.
        y: RIBBON_PAD + hw * (RIBBON_H - RIBBON_PAD * 2),
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
      // Below the midline (larger y) is now home's half, matching the y-flip above.
      s.hex = ((s.a.y + s.b.y) / 2 <= midY) ? awayHex : homeHex;
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
      '<div class="rb-y rb-y-top"><span>' + escapeHtml(slide.awayAbbr || "") + " 100%</span></div>" +
      '<div class="rb-y rb-y-bot"><span>' + escapeHtml(slide.homeAbbr || "") + " 100%</span></div>" +
      marker + "</div>" +
      '<div class="rb-axis">' + axis + "</div>" +
    "</div>";
  }

  /* Live outs, in the same three-circle format the scorebug uses, filling
     left to right. Circles the play itself recorded animate in; ones already
     on the board when the batter stepped up are just there. This is a replay
     of a completed play, not the scorebug's live state, so the third out gets
     its own dot rather than a separate "ends the half-inning" badge - the
     .inning-over class still marks that outcome on the container. */
  function sceneOutsHtml(m) {
    var before = Math.max(0, Math.min(2, m.outs_before == null ? 0 : m.outs_before));
    var after = Math.max(0, Math.min(3, m.outs_after == null ? before : m.outs_after));
    var dots = [0, 1, 2].map(function (i) {
      var on = i < after;
      return '<span class="out-dot' + (on ? " on" : "") +
        (on && i >= before ? " fresh" : "") + '"></span>';
    }).join("");
    return '<div class="scene-outs' + (after >= 3 ? " inning-over" : "") + '">' +
      '<span class="scene-outs-lbl">OUTS</span>' +
      '<span class="out-dots">' + dots + "</span>" +
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

  // ── Pitch/swing wheel (debugging readout, refinements round F1) ──────────
  // Pitch and swing are both marked ON the same arc (Alex's correction - the
  // earlier build staggered the DOTS, which was wrong; what gets staggered is
  // the two value LABELS, outside the ring, the first value's label closer in
  // and the second's further out, so they don't collide when the two values
  // are angularly close) with an arc connecting them (the shorter way around,
  // exactly the wheel signedCirc already resolves ties on), and the resulting
  // number in the middle. Left to right, roughly in order of importance:
  //   DIFF wheel - raw pitch/swing (or, on a steal attempt, steal_num/
  //                throw_num - utils.py's steal_color_bar: Safe if
  //                circular_diff(throw_num, steal_num) <= a per-runner
  //                safe_range, not threaded through here, so no safe-zone
  //                shown, just the two values and their diff), mod 1000 - the
  //                wheel that actually decides the result. Shown on every
  //                play with either pair available - a walk or strikeout
  //                still had a real pitch/swing duel, even with no ball in
  //                play to show a flight for. Shows the result's own diff
  //                band as a reference arc when one exists (batted balls
  //                only), anchored at the first dot, swept the same direction
  //                the actual diff went, so "where does this diff fall on
  //                the continuum we set up for this result" is a direct
  //                visual comparison, not a lookup.
  //   HZ wheel   - lastDigit(pitch)/lastDigit(swing), mod 10. Batted balls only.
  // There is no separate LA wheel: launch angle is now driven by the same
  // diff/band the DIFF wheel already shows (its on-top/below direction is
  // the DIFF arc's own sign, its "how close to ideal" is the DIFF band
  // overlay) - a firstTwo-only wheel would just be a second, disconnected
  // number with no bearing on the actual result (see launchAngleFor).
  // HZ disappears on anything without a real flight (a walk, strikeout, a
  // steal attempt) - DIFF alone then centres itself (scene-wheels is already
  // a centered flex row; one child centers for free).
  var WHEEL_CX = 50, WHEEL_CY = 50, WHEEL_VB = 100;
  var WHEEL_RING_R = 26, WHEEL_BAND_R = 20;
  var WHEEL_LABEL1_R = 34, WHEEL_LABEL2_R = 43;
  var WHEEL_DOT_R = 3.5;

  function wheelPt(r, angleDeg) {
    var rad = (angleDeg - 90) * Math.PI / 180;
    return { x: WHEEL_CX + r * Math.cos(rad), y: WHEEL_CY + r * Math.sin(rad) };
  }

  // Arc from `startDeg` sweeping `deltaDeg` (signed - positive is clockwise).
  // Correct for any magnitude, though every caller except the band overlay
  // passes a signedCirc result, which is never more than half the wheel.
  function wheelArcD(r, startDeg, deltaDeg) {
    var p0 = wheelPt(r, startDeg);
    var p1 = wheelPt(r, startDeg + deltaDeg);
    var sweep = deltaDeg >= 0 ? 1 : 0;
    var largeArc = Math.abs(deltaDeg) > 180 ? 1 : 0;
    return "M" + p0.x.toFixed(2) + "," + p0.y.toFixed(2) +
      " A" + r.toFixed(2) + "," + r.toFixed(2) + " 0 " + largeArc + " " + sweep + " " +
      p1.x.toFixed(2) + "," + p1.y.toFixed(2);
  }

  function wheelAngleOf(v, mod) { return (v % mod) / mod * 360; }

  // Role markers on the ring: a baseball for defense (pitcher/catcher) and a
  // bat for offense (batter/runner), shaped/coloured like the real things
  // rather than a plain dot. Positioning/rotation goes on an un-animated
  // outer <g> via the "transform" attribute; the CSS scale-in animation
  // lands on the inner <g class="wheel-dot ..."> instead, since a CSS
  // `transform` animation replaces (rather than composes with) an element's
  // own transform attribute - nesting keeps the two from fighting.
  function wheelBallIconSvg() {
    return '<circle r="' + WHEEL_DOT_R + '"></circle>' +
      '<path class="wheel-dot-seam" d="M -2.4,-1.7 Q -0.8,0 -2.4,1.7"></path>' +
      '<path class="wheel-dot-seam" d="M 2.4,-1.7 Q 0.8,0 2.4,1.7"></path>';
  }

  // Drawn barrel-down (positive local y) so the caller's rotation can point
  // that barrel straight out along the ring's radius - see wheelMarkerHtml.
  // A single tapered silhouette (round knob, thin straight handle, angled
  // shoulder, straight-sided barrel, rounded tip) rather than stacked
  // primitives - those read as a lollipop/bowling pin at this size, this
  // reads as a bat.
  var WHEEL_BAT_PATH = "M0,-4.6C0.55,-4.6 0.55,-4.15 0.3,-3.9L0.35,-1.3L1.25,0.9L1.25,3.6" +
    "C1.25,4.05 0.9,4.4 0.4,4.55C0.25,4.6 0.1,4.6 0,4.6C-0.1,4.6 -0.25,4.6 -0.4,4.55" +
    "C-0.9,4.4 -1.25,4.05 -1.25,3.6L-1.25,0.9L-0.35,-1.3L-0.3,-3.9C-0.55,-4.15 -0.55,-4.6 0,-4.6Z";
  function wheelBatIconSvg() {
    return '<path d="' + WHEEL_BAT_PATH + '"></path>';
  }

  // angleDeg is the marker's own position on the ring, in wheelPt's
  // convention (0 = straight up from centre, clockwise). The bat is drawn
  // barrel-down/handle-up in its own local coordinates, so rotating it by
  // angleDeg+180 swings the handle to point straight in at the wheel's
  // centre and the barrel straight out - perpendicular to the ring at that
  // point, every time, regardless of where the marker lands.
  function wheelMarkerHtml(pt, angleDeg, cls, dotIdxCls) {
    var isOff = cls === "off";
    var xf = "translate(" + pt.x.toFixed(2) + "," + pt.y.toFixed(2) + ")" +
      (isOff ? " rotate(" + (angleDeg + 180).toFixed(1) + ")" : "");
    return '<g transform="' + xf + '"><g class="wheel-dot ' + dotIdxCls + ' wheel-dot-' + cls + '">' +
      (isOff ? wheelBatIconSvg() : wheelBallIconSvg()) +
      "</g></g>";
  }

  // The archetype band, anchored at the first dot and swept in the same
  // direction the actual diff went - so the real arc and the "expected
  // range for this result" reference both start from the same point and are
  // directly comparable, not just two numbers to cross-reference by hand.
  function wheelBandArcHtml(startDeg, dirSign, lo, hi, mod) {
    var loDeg = dirSign * (lo / mod) * 360;
    var hiDeg = dirSign * (hi / mod) * 360;
    return '<path class="wheel-band" d="' + wheelArcD(WHEEL_BAND_R, startDeg + loDeg, hiDeg - loDeg) + '"></path>';
  }

  /* v1/v2 are the two rolled values in narrative order (pitch-then-swing for
     a batted ball, runner-then-catcher for a steal) - v1's dot/label always
     appears first, v2's 650ms later. cls1/cls2 are each "def" or "off"
     (defense = baseball, offense = bat) - NOT tied to v1/v2 position, since
     the defense role goes first for a batted ball (pitcher) but second for a
     steal (catcher), so shape has to travel with the role, not the slot.
     arcCls picks the arc's colour: "neutral" (LA/HZ - theme-flipped
     black/white) or "hit"/"out" (DIFF - green/red, matching the ball
     marker's own C1 convention). */
  function wheelHtml(label, v1, v2, mod, cls1, cls2, centerBig, centerSmall, band, arcCls) {
    var deg1 = wheelAngleOf(v1, mod);
    var delta = signedCirc(v1, v2, mod);
    var deltaDeg = delta / mod * 360;
    var arcLen = WHEEL_RING_R * Math.abs(deltaDeg) * Math.PI / 180;
    var dot1Pt = wheelPt(WHEEL_RING_R, deg1);
    var dot2Pt = wheelPt(WHEEL_RING_R, deg1 + deltaDeg);
    var label1Pt = wheelPt(WHEEL_LABEL1_R, deg1);
    var label2Pt = wheelPt(WHEEL_LABEL2_R, deg1 + deltaDeg);
    var bandHtml = band ? wheelBandArcHtml(deg1, deltaDeg >= 0 ? 1 : -1, band.lo, band.hi, mod) : "";
    return '<div class="wheel">' +
      '<div class="wheel-label">' + escapeHtml(label) + "</div>" +
      '<svg class="wheel-svg" viewBox="0 0 ' + WHEEL_VB + " " + WHEEL_VB + '" aria-hidden="true">' +
        '<circle class="wheel-ring" cx="' + WHEEL_CX + '" cy="' + WHEEL_CY + '" r="' + WHEEL_RING_R + '"></circle>' +
        bandHtml +
        '<path class="wheel-arc wheel-arc-' + arcCls + '" d="' + wheelArcD(WHEEL_RING_R, deg1, deltaDeg) +
          '" style="--alen:' + arcLen.toFixed(2) + 'px"></path>' +
        wheelMarkerHtml(dot1Pt, deg1, cls1, "wheel-dot-1") +
        wheelMarkerHtml(dot2Pt, deg1 + deltaDeg, cls2, "wheel-dot-2") +
        '<text class="wheel-val wheel-val-1" x="' + label1Pt.x.toFixed(2) + '" y="' + label1Pt.y.toFixed(2) +
          '">' + escapeHtml(String(v1)) + "</text>" +
        '<text class="wheel-val wheel-val-2" x="' + label2Pt.x.toFixed(2) + '" y="' + label2Pt.y.toFixed(2) +
          '">' + escapeHtml(String(v2)) + "</text>" +
        '<text class="wheel-center-big" x="' + WHEEL_CX + '" y="' + (WHEEL_CY - (centerSmall ? 1 : -4)) +
          '">' + escapeHtml(centerBig) + "</text>" +
        (centerSmall
          ? '<text class="wheel-center-small" x="' + WHEEL_CX + '" y="' + (WHEEL_CY + 10) + '">' +
            escapeHtml(centerSmall) + "</text>"
          : "") +
      "</svg>" +
    "</div>";
  }

  function sceneWheelsHtml(m, flight) {
    var wheels = "";

    // DIFF: every play with a real pitch/swing or steal_num/throw_num pair -
    // a walk or strikeout still had a real pitch/swing duel, and a steal
    // attempt has its own equivalent pair, even with no batted-ball flight.
    var isSteal = m.pitch == null && m.steal_num != null && m.throw_num != null;
    if (m.pitch != null && m.swing != null || isSteal) {
      var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
      if (isSteal) {
        // Runner (offense, bat marker) breaks first; catcher (defense,
        // baseball marker) throws second - per Alex's spec, "runner # then
        // progress to catcher #". No archetype band - steals don't have one.
        wheels += wheelHtml("DIFF", m.steal_num, m.throw_num, 1000, "off", "def",
          String(Math.abs(signedCirc(m.steal_num, m.throw_num, 1000))), null, null,
          wasOut ? "out" : "hit");
      } else {
        var bandRow = (data.meta.flight && data.meta.flight.bands || {})[m.result];
        wheels += wheelHtml("DIFF", m.pitch, m.swing, 1000, "def", "off", String(m.diff),
          flight ? Math.round(flight.ev) + " mph" : null,
          bandRow ? { lo: bandRow.lo, hi: bandRow.hi } : null,
          wasOut ? "out" : "hit");
      }
    }

    // HZ: batted balls only (Alex's call) - `flight` truthy is exactly that
    // gate (flightParams returns null for everything else, the same set
    // sceneFlightReadoutHtml below already checks against).
    if (flight) {
      var d1p = lastDigit(m.pitch), d1s = lastDigit(m.swing);
      wheels += wheelHtml("HZ", d1p, d1s, 10, "def", "off", flight.angle.toFixed(0) + "°", null, null, "neutral");
    }

    return wheels ? '<div class="scene-wheels">' + wheels + "</div>" : "";
  }

  /* Compact "telemetry" readout beside the result pill (ball-flight-plan.md
     Stage 5): launch angle to 1dp, exit velocity to the nearest mph, matching
     how Statcast broadcasts read. Only rendered when there's a batted ball to
     report - a strikeout still shows just the result pill. A grounder's
     negative launch angle is informative, not hidden. */
  function sceneFlightReadoutHtml(flight) {
    if (!flight) return "";
    return '<span class="scene-flight-readout">' +
      '<span class="sf-la">' + flight.la.toFixed(1) + "°</span>" +
      '<span class="sf-ev">' + Math.round(flight.ev) + " mph</span>" +
    "</span>";
  }

  /* No tag pills and no win-probability text here: the tags are a filtering
     device for the main feed rather than something to read mid-slideshow, and
     the win probability is already on the ribbon's marker, attached to the
     point it belongs to. */
  function sceneDetailHtml(m, flight) {
    var resultLabel = (data.meta.result_labels || {})[m.result] || m.result;
    /* Result first, then who did it - the same order at every width, so the
       eye lands in the same place whether the scene is stacked on a phone or
       split into two columns on a wide screen. */
    return '<div class="scene-detail">' +
      sceneOutsHtml(m) +
      '<div class="scene-play-line">' +
        '<span class="result-pill ' + (m.result_category === "hitting" ? "offense" : "defense") + '">' +
          escapeHtml(resultLabel) + "</span>" +
        // Cheap and worth doing (Stage 4d): a home run that stayed inside the
        // park is rare enough that without a callout it reads as a glitch.
        (flight && m.result === "HR" && !flight.clearedFence
          ? '<span class="itp-pill">Inside the Park</span>' : "") +
        diffPill(m) +
        sceneFlightReadoutHtml(flight) +
      "</div>" +
      // Pitcher first, batter second - the order holds at every width, so when
      // the row wraps on a phone the pitcher stacks on top rather than the
      // pairing reversing between breakpoints.
      '<div class="scene-matchup">' +
        sceneRoleHtml("PITCHING", m.pitcher_id, m.pitcher_name, m.def_team_abbr) +
        '<span class="mu-vs">vs</span>' +
        sceneRoleHtml("AT BAT", m.batter_id, m.batter_name, m.off_team_abbr) +
      "</div>" +
      scoringLine(m) +
    "</div>";
  }

  /* When each run actually crosses the plate, derived from the same basepath
     legs the diamond animates: a runner coming home from third arrives well
     before one coming all the way from first, and the scoreboard should tick
     at those moments rather than all at once on a guessed delay.

     B5: a runner tagging up on a caught fly does not leave until the catch
     (see sceneFieldHtml's matching mvDelay logic) - this must add the same
     offset or the scoreboard ticks before the runner visibly arrives. */
  function scoreArrivals(m, flight) {
    var moves = m.runner_moves || deriveRunnerMoves(String(m.obc_before || "000"),
                                  String(m.obc_after || "000"), m.runs || 0);
    var catchMs = flight && CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
    var times = [];
    moves.forEach(function (mv) {
      if (!mv.scored) return;
      var legs = basepathWaypoints(mv.from, mv.to, true).length;
      var dur = RUN_LEG_MS[Math.min(legs, RUN_LEG_MS.length - 1)] || 0;
      var lead = catchMs ? catchMs + TAG_UP_MS : 0;
      times.push(lead + dur);
    });
    return times.sort(function (a, b) { return a - b; });
  }

  /* The batting team's score counts up rather than just flashing its final
     value: each intermediate number is stacked in the same grid cell and shown
     over the window between one runner touching home and the next. Pure CSS
     delays, so nothing here can outlive the slide.

     `finalScore` is the score AFTER this play - key_moments_build.py emits
     `away_score_before + runs` (key_moments_build.py:577-578), verified against
     cumulative runs across every play in the feed. So the count starts at
     finalScore - runs, which is the score the play began with, and ends on the
     value already in the data. Starting from finalScore instead would count
     this play's runs twice. */
  function scoreCellHtml(finalScore, runs, arrivals) {
    if (!runs) return '<span class="val">' + finalScore + "</span>";
    var steps = "";
    for (var i = 0; i <= runs; i++) {
      var at = i === 0 ? 0 : (arrivals[i - 1] != null
        ? arrivals[i - 1]
        : (arrivals[arrivals.length - 1] || 0));
      var until = i === runs ? null : (arrivals[i] != null
        ? arrivals[i]
        : (arrivals[arrivals.length - 1] || 0));
      steps += '<span class="tick-step' + (i === 0 ? " first" : "") + '" style="--at:' +
        at + "ms" + (until == null ? "" : ";--until:" + until + "ms") + '">' +
        (finalScore - runs + i) + "</span>";
    }
    return '<span class="val counter">' + steps + "</span>";
  }

  function sceneScoreHtml(m, flight) {
    var awayBatting = !m.batting_is_home;
    var runs = m.runs || 0;
    var arrivals = runs ? scoreArrivals(m, flight) : [];
    return '<div class="score-block scene-score">' +
      '<div class="row' + (awayBatting ? " batting" : "") + '">' +
        teamLogoImg(m.away_team_abbr, "scene-score-logo") +
        '<span class="abbr">' + escapeHtml(m.away_team_abbr) + "</span>" +
        scoreCellHtml(m.away_score, awayBatting ? runs : 0, arrivals) + "</div>" +
      '<div class="row' + (awayBatting ? "" : " batting") + '">' +
        teamLogoImg(m.home_team_abbr, "scene-score-logo") +
        '<span class="abbr">' + escapeHtml(m.home_team_abbr) + "</span>" +
        scoreCellHtml(m.home_score, awayBatting ? 0 : runs, arrivals) + "</div>" +
    "</div>";
  }

  /* Carries what the removed replay-done card used to say, pinned to the play
     the replay now rests on. */
  function sceneRecapHtml(r) {
    if (!r) return "";
    if (!r.isFinal) {
      return '<div class="scene-recap live">Caught up on all the plays in this game</div>';
    }
    var head = "FINAL · " + escapeHtml(r.away) + " " + r.awayScore + ", " +
               escapeHtml(r.home) + " " + r.homeScore;
    var top = r.topPlay
      ? '<span class="scene-recap-top">Biggest play: ' + escapeHtml(r.topPlay.featured_name) +
        " · " + escapeHtml((data.meta.result_labels || {})[r.topPlay.result] || r.topPlay.result) +
        " · LI " + r.topPlay.leverage.toFixed(1) + "</span>"
      : "";
    return '<div class="scene-recap"><span class="scene-recap-head">' + head + "</span>" + top + "</div>";
  }

  function playSceneHtml(slide) {
    var m = slide.play;
    var flight = flightParams(m, data.meta.flight);
    if (flight && !applyPositionOverride(m, flight)) applyGroundBallFielderDepth(m, flight);
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
        sceneFieldHtml(m, flight) +
        '<div class="scene-side">' +
          '<div class="scene-inning' + (newHalf ? " new-half" : "") + '">' +
            '<div class="tri ' + (m.half === "top" ? "up" : "down") + '"></div>' +
            '<div class="inning-num">' + m.inning + "</div>" +
          "</div>" +
          sceneScoreHtml(m, flight) +
          sceneMeterHtml(m) +
        "</div>" +
      "</div>" +
      sceneDetailHtml(m, flight) +
      sceneWheelsHtml(m, flight) +
      sceneRibbonHtml(slide) +
    "</div>";
  }

  function catchUpSlideHtml(slide) {
    if (slide.kind === "title") {
      var g = slide.group;
      return '<div class="catchup-title">' +
        '<div class="catchup-title-teams">' +
          '<span class="catchup-title-team">' +
            teamLogoImg(g.away_team_abbr, "catchup-title-logo") +
            '<span>' + escapeHtml(teamName(g.away_team_abbr)) + "</span>" +
          "</span>" +
          '<span class="catchup-at">@</span>' +
          '<span class="catchup-title-team">' +
            teamLogoImg(g.home_team_abbr, "catchup-title-logo") +
            '<span>' + escapeHtml(teamName(g.home_team_abbr)) + "</span>" +
          "</span>" +
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
          '<span class="catchup-title-team">' +
            teamLogoImg(slide.away, "catchup-title-logo") +
            '<span>' + escapeHtml(teamName(slide.away)) + "</span>" +
          "</span>" +
          '<span class="catchup-at">@</span>' +
          '<span class="catchup-title-team">' +
            teamLogoImg(slide.home, "catchup-title-logo") +
            '<span>' + escapeHtml(teamName(slide.home)) + "</span>" +
          "</span>" +
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
  /* Both numbers cover the scene's own animations first and then leave real
     reading time on top. The longest run around the bases takes 1700ms and the
     outs circle lands at ~900ms, so a 2000ms routine dwell was spending most of
     itself on motion and leaving almost nothing to actually read the matchup,
     the result and the ribbon.
     Retuned again for ball flight (ball-flight-plan.md Stage 7a): the slowest
     single piece is now a thrown-out runner's out-walk on a max-hangtime fly
     ball - ball travel capped at 1400ms, +400ms outs-choreography beat,
     +900ms turn-red/walk/fade - about 2700ms before anything is left to read.
     3600/6000 clear that with real reading time on top, same starting-guess
     discipline every dwell constant here has gone through; watch a real run
     and adjust.
     Flag for Alex, not to silently resolve: the Catch Me Up backlog scenario
     already ran ~39 minutes for 656 plays at 2800/5200. At 3600/6000 that
     grows to roughly 48-50 minutes - a real, sharper tradeoff worth surfacing
     rather than burying in a constant change. */
  var PLAY_DWELL_MS_ROUTINE = 3600;
  var PLAY_DWELL_MS_KEY = 6000;
  // Extra beat on the play that opens a half-inning, so the break between
  // halves registers instead of the reel running straight through it.
  var HALF_INNING_BONUS_MS = 800;

  function slideDwell(slide) {
    var speed = getPlaybackSpeed();
    if (slide.kind !== "play") return TITLE_DWELL_MS / speed;
    var base = slide.play.is_key_moment ? PLAY_DWELL_MS_KEY : PLAY_DWELL_MS_ROUTINE;
    return (base + (startsHalfInning(slide) ? HALF_INNING_BONUS_MS : 0)) / speed;
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

  /* Tap to pause and swipe to step both live on the slide, so they have to be
     told apart: a tap barely moves, a swipe travels horizontally. Drags that
     are mostly vertical are ignored entirely so the slide can still scroll,
     and the click the browser synthesises after a swipe is swallowed rather
     than pausing the show on every gesture.

     Shared by both slideshows - it is pure input plumbing with no state of its
     own beyond the gesture in flight, unlike the timers each feature keeps
     separately. */
  var SWIPE_MIN_PX = 45;

  function wireSlideGestures(el, onTap, onStep) {
    var x0 = null, y0 = 0, swiped = false;
    el.addEventListener("touchstart", function (e) {
      swiped = false;
      if (e.touches.length !== 1) { x0 = null; return; }
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    }, { passive: true });
    el.addEventListener("touchend", function (e) {
      if (x0 == null) return;
      var t = e.changedTouches[0];
      var dx = t.clientX - x0, dy = t.clientY - y0;
      x0 = null;
      if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) <= Math.abs(dy)) return;
      swiped = true;
      // swipe left goes forward, as a carousel does - wraps at either end
      // (stepCatchUp/stepReplay) rather than a long swipe jumping there.
      onStep(dx < 0 ? 1 : -1);
    }, { passive: true });
    el.addEventListener("click", function (e) {
      if (swiped) { swiped = false; return; }
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
      onTap();
    });
  }

  /* Grey out the step control that has nowhere to go, so the ends of the run
     are felt rather than met with a dead click. */
  function syncNav(prefix, state) {
    var prev = $(prefix + "-prev"), next = $(prefix + "-next");
    if (!prev || !next) return;
    prev.disabled = state.index <= 0;
    next.disabled = state.index >= state.slides.length - 1;
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

    syncNav("catchup", catchUp);

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

  /* Manual step. The new slide gets a full dwell rather than whatever was left
     of the old one, and stepping does not change whether the show is paused -
     someone paging through a paused run stays paused.

     G1: wraps at either end rather than a dead stop - an ordinary swipe past
     the last play goes to the first, and back again from the first. */
  function stepCatchUp(delta) {
    var n = catchUp.slides.length;
    var i = (catchUp.index + delta + n) % n;
    clearCatchUpTimer();
    catchUp.remaining = 0;
    showCatchUpSlide(i);
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

  // Closing the modal while the browser is actually in fullscreen must not
  // leave the tab stuck there behind a hidden, empty modal.
  function exitFullscreenIfActive() {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
  }

  function closeCatchUp() {
    // Clearing the timer here is what stops a slide advancing behind an
    // already-hidden modal.
    clearCatchUpTimer();
    catchUp.paused = false;
    exitFullscreenIfActive();
    $("catchup-modal").hidden = true;
    $("catchup-slide").innerHTML = "";
  }

  /* Expand button in the slideshow's corner - real browser fullscreen on the
     modal element (not just a bigger CSS layout), so the browser chrome
     itself goes away too. A fullscreenchange listener keeps the icon in sync
     even when fullscreen is left via Esc or the browser's own UI, not just
     this button. */
  function wireFullscreenToggle(modalId, btnId) {
    var modal = $(modalId), btn = $(btnId);
    if (!modal || !btn) return;
    btn.addEventListener("click", function () {
      var active = document.fullscreenElement || document.webkitFullscreenElement;
      if (active) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else if (modal.requestFullscreen) {
        modal.requestFullscreen();
      } else if (modal.webkitRequestFullscreen) {
        modal.webkitRequestFullscreen();
      }
    });
    document.addEventListener("fullscreenchange", function () {
      btn.classList.toggle("is-fullscreen", document.fullscreenElement === modal);
    });
    document.addEventListener("webkitfullscreenchange", function () {
      btn.classList.toggle("is-fullscreen", document.webkitFullscreenElement === modal);
    });
  }

  function wireCatchUp() {
    var modal = $("catchup-modal");
    if (!modal) return;
    wireFullscreenToggle("catchup-modal", "catchup-fullscreen");
    $("catchup-banner").addEventListener("click", openCatchUp);
    $("catchup-close").addEventListener("click", closeCatchUp);
    $("catchup-prev").addEventListener("click", function () { stepCatchUp(-1); });
    $("catchup-next").addEventListener("click", function () { stepCatchUp(1); });
    modal.addEventListener("click", function (e) { if (e.target === modal) closeCatchUp(); });
    wireSlideGestures($("catchup-slide"),
      function () { setCatchUpPaused(!catchUp.paused); },
      function (d) { stepCatchUp(d); });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") closeCatchUp();
      else if (e.key === " ") { e.preventDefault(); setCatchUpPaused(!catchUp.paused); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); stepCatchUp(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); stepCatchUp(1); }
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

  /* A slideshow of exactly what's currently filtered/visible in the feed -
     reuses the same replay modal/state machine as Game Replay (no separate
     third slideshow to maintain), just fed a different slide list: plain
     "play" slides only, no title card, since a filtered set can span any
     number of different games and there is no one game to title it after.
     Each play still gets its own game's ribbon context (gamePlaysFor), same
     as every other slideshow. */
  function buildFilteredPlaysSlides(plays) {
    var total = plays.length;
    return plays.map(function (p, i) {
      var gamePlays = gamePlaysFor(p.session_number, p.game_code);
      var byNum = {};
      gamePlays.forEach(function (gp, gi) { byNum[gp.play_num] = gi; });
      return {
        kind: "play", play: p, playNo: i + 1, total: total,
        gamePlays: gamePlays,
        gameIdx: byNum[p.play_num] == null ? -1 : byNum[p.play_num],
        ribbonFrom: 0,
        homeAbbr: p.home_team_abbr, awayAbbr: p.away_team_abbr,
      };
    });
  }

  /* Ascending on purpose, the reverse of the feed's own "most important at
     the top" sort (sorted() above) - a slideshow plays forward in time and
     builds toward a finale, so the oldest/least dramatic play leads and the
     most recent/most dramatic one closes the show. */
  function filteredPlaysOrdered(orderBy) {
    var rows = pool().filter(matches).slice();
    if (orderBy === "wpa") {
      rows.sort(function (a, b) { return Math.abs(a.wpa || 0) - Math.abs(b.wpa || 0); });
    } else if (orderBy === "leverage") {
      rows.sort(function (a, b) { return (a.leverage || 0) - (b.leverage || 0); });
    } else {
      rows.sort(function (a, b) {
        var ta = a.timestamp || "", tb = b.timestamp || "";
        if (ta !== tb) return ta < tb ? -1 : 1;
        return a.play_num - b.play_num;
      });
    }
    return rows;
  }

  // Exposed for the Playwright test harness only, same convention as the
  // ball-flight KMFlight object above - window.KMFlight already exists by
  // the time this line runs (assigned earlier in execution order).
  window.KMFlight.filteredPlaysOrdered = filteredPlaysOrdered;

  function openFilteredPlaysSlideshow(orderBy) {
    var rows = filteredPlaysOrdered(orderBy);
    if (!rows.length) { toast("No plays match these filters."); return; }
    // Ribbon context can reach into any game a filtered play belongs to, not
    // just whichever sessions happen to be loaded for the current filter -
    // same "load everything" guarantee Catch Me Up's own reel relies on.
    loadAllSessions().then(function () {
      replay.slides = buildFilteredPlaysSlides(rows);
      replay.index = -1;   // no previous slide, so slide 0 gets the full fade in
      replay.paused = false;
      $("replay-pause-hint").hidden = true;
      $("replay-card").classList.remove("paused");
      $("replay-modal").hidden = false;
      showReplaySlide(0);
    });
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

    syncNav("replay", replay);

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

  // G1: wraps at either end, same as stepCatchUp.
  function stepReplay(delta) {
    var n = replay.slides.length;
    var i = (replay.index + delta + n) % n;
    clearReplayTimer();
    replay.remaining = 0;
    showReplaySlide(i);
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

  // Tune-after-watching, like every other timing constant in this scene.
  var REPLAY_JUMP_OPEN_DELAY_MS = 120;

  /* Feed card's play button: same Game Replay overlay, but seeks straight to
     the play that was clicked instead of starting from the top - and opens
     paused, since the point is to look at this one play, not watch the rest
     of the game auto-advance past it. */
  function openReplayAtPlay(gameCode, session, playNum, btn) {
    if (session == null || isNaN(session)) {
      toast("Could not determine which session this play belongs to.");
      return;
    }
    btn.classList.add("loading");
    loadGameReplay(gameCode, session).then(function (plays) {
      btn.classList.remove("loading");
      if (!plays.length) { toast("No plays recorded for that game yet."); return; }
      replay.slides = buildGameReplaySlides(plays);
      var target = -1;
      replay.slides.forEach(function (s, i) {
        if (target === -1 && s.kind === "play" && s.play.play_num === playNum) target = i;
      });
      replay.index = -1;
      replay.paused = true;
      // A stale leftover from a previous paused session must not leak into
      // this one - setReplayPaused's resume path falls back to it directly.
      replay.remaining = 0;
      $("replay-pause-hint").hidden = false;
      $("replay-card").classList.add("paused");
      // The modal itself opens right away (immediate feedback that the click
      // registered), but the play mounts a beat later - mounting it in the
      // same tick the modal becomes visible let the scene's animation clock
      // start before the browser had actually painted the modal open, so the
      // sequence (pitch dot, arc draw, swing dot...) was already partway
      // through by the first frame anyone could see it.
      $("replay-modal").hidden = false;
      window.setTimeout(function () {
        showReplaySlide(target >= 0 ? target : 0);
      }, REPLAY_JUMP_OPEN_DELAY_MS);
    }).catch(function () {
      btn.classList.remove("loading");
      toast("Could not load that game's plays.");
    });
  }

  function closeReplay() {
    clearReplayTimer();
    replay.paused = false;
    exitFullscreenIfActive();
    $("replay-modal").hidden = true;
    $("replay-slide").innerHTML = "";
  }

  function wireReplay() {
    var modal = $("replay-modal");
    if (!modal) return;
    wireFullscreenToggle("replay-modal", "replay-fullscreen");
    $("replay-close").addEventListener("click", closeReplay);
    $("replay-prev").addEventListener("click", function () { stepReplay(-1); });
    $("replay-next").addEventListener("click", function () { stepReplay(1); });
    modal.addEventListener("click", function (e) { if (e.target === modal) closeReplay(); });
    wireSlideGestures($("replay-slide"),
      function () { setReplayPaused(!replay.paused); },
      function (d) { stepReplay(d); });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") closeReplay();
      else if (e.key === " ") { e.preventDefault(); setReplayPaused(!replay.paused); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); stepReplay(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); stepReplay(1); }
    });
    var filteredBtn = $("play-filtered-btn");
    if (filteredBtn) {
      filteredBtn.addEventListener("click", function () {
        openFilteredPlaysSlideshow(filters.sort);
      });
    }
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

  var RESULT_CODE_SUGGEST_LIMIT = 8;

  // Every distinct raw result code (not the collapsed "Hitting"/"Pitching"
  // category, and not result_short's grouping - e.g. 1B/1BWH/1BWH2 are three
  // separate options here) - result_labels' own keys are already the
  // complete set the build ships, so no separate list to keep in sync.
  function renderResultCodeSuggest(query) {
    var box = $("result-code-suggest");
    var needle = query.trim().toLowerCase();
    if (!needle) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    var labels = data.meta.result_labels || {};
    var matches = Object.keys(labels).filter(function (code) {
      return code.toLowerCase().indexOf(needle) !== -1 ||
        (labels[code] || "").toLowerCase().indexOf(needle) !== -1;
    }).sort().slice(0, RESULT_CODE_SUGGEST_LIMIT);
    if (!matches.length) {
      box.innerHTML = '<div class="player-suggest-empty">No results match.</div>';
      box.hidden = false;
      return;
    }
    box.innerHTML = matches.map(function (code) {
      return '<div class="player-suggest-row" data-result-code="' + escapeHtml(code) + '">' +
        "<strong>" + escapeHtml(code) + "</strong>" +
        '<span class="team">' + escapeHtml(labels[code] || "") + "</span></div>";
    }).join("");
    box.hidden = false;
  }

  // On-base filter chips: one per real OBC code, drawn from the exact same
  // mini-diamond markup the play cards already use (data.meta.bases_svg) -
  // reusing that rather than a second hand-drawn icon set. Ordered 0-7 per
  // Alex's explicit spec, matching the project's own _BRC_TO_OBC numbering
  // used elsewhere (000,001,010,100,011,101,110,111) - not plain ascending
  // binary value of the string.
  //
  // An explicit ORDER array, not Object.keys() on a lookup object - a plain
  // object silently reorders any own key that happens to be a canonical
  // integer string (no leading zero) to the front, sorted numerically, ahead
  // of every other string key's insertion order. "100"/"101"/"110"/"111" all
  // qualify (ToString(ToUint32(k)) === k) but "000"/"001"/"010"/"011" don't
  // (their canonical form drops the leading zeros), so Object.keys() was
  // silently splitting this into two groups - the four without a leading
  // zero first, numerically, then the four with one, in insertion order -
  // instead of the single 0-7 sequence actually written below.
  var OBC_ORDER = ["000", "001", "010", "100", "011", "101", "110", "111"];
  var OBC_LABELS = {
    "000": "Bases empty", "001": "Runner on 1st", "010": "Runner on 2nd", "100": "Runner on 3rd",
    "011": "1st & 2nd", "101": "1st & 3rd", "110": "2nd & 3rd", "111": "Bases loaded",
  };
  function populateObcChips() {
    var svgs = data.meta.bases_svg || {};
    $("obc-chips").innerHTML = OBC_ORDER.map(function (obc) {
      return '<button type="button" class="chip obc-chip" data-obc="' + obc + '" title="' +
        escapeHtml(OBC_LABELS[obc]) + '">' + (svgs[obc] || "") + "</button>";
    }).join("");
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

    // Exact result code search - same interaction shape as the player search
    // above (type to filter, click a row to pick), but resultCode is a single
    // exact match rather than a substring, so there's no separate "typed but
    // unconfirmed" text state to debounce-apply the way player search has.
    $("result-code-input").addEventListener("input", function (e) {
      filters.resultCode = "";
      renderResultCodeSuggest(e.target.value);
      render();
    });

    $("result-code-input").addEventListener("focus", function (e) {
      if (e.target.value.trim()) renderResultCodeSuggest(e.target.value);
    });

    $("result-code-input").addEventListener("blur", function () {
      window.setTimeout(function () { $("result-code-suggest").hidden = true; }, 120);
    });

    $("result-code-suggest").addEventListener("mousedown", function (e) {
      var row = e.target.closest(".player-suggest-row");
      if (!row) return;
      e.preventDefault();
      var code = row.getAttribute("data-result-code");
      filters.resultCode = code;
      $("result-code-input").value = code;
      $("result-code-suggest").hidden = true;
      render();
    });

    // Outs/OBC: radio-with-off, same click-the-active-one-to-clear pattern
    // result-chips already uses above.
    $("outs-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var outs = Number(chip.getAttribute("data-outs"));
      filters.outs = (filters.outs === outs) ? null : outs;
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", Number(c.getAttribute("data-outs")) === filters.outs);
      });
      render();
    });

    $("obc-chips").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var obc = chip.getAttribute("data-obc");
      filters.obc = (filters.obc === obc) ? "" : obc;
      Array.prototype.forEach.call(this.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-obc") === filters.obc);
      });
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
      filters.resultCode = "";
      filters.outs = null;
      filters.obc = "";
      deselectScoreboardTile();
      $("player-suggest").hidden = true;
      $("result-code-suggest").hidden = true;
      Array.prototype.forEach.call(
        document.querySelectorAll("#result-chips .chip, #tag-chips .chip, #outs-chips .chip, #obc-chips .chip"),
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
      $("result-code-input").value = "";
      renderMaybeLoading();
    });

    $("moments").addEventListener("click", function (e) {
      var jumpBtn = e.target.closest("[data-jump-game]");
      if (jumpBtn) {
        openReplayAtPlay(
          jumpBtn.getAttribute("data-jump-game"),
          Number(jumpBtn.getAttribute("data-jump-session")),
          Number(jumpBtn.getAttribute("data-jump-num")),
          jumpBtn
        );
        return;
      }
      var btn = e.target.closest("[data-fav-id]");
      if (!btn || !window.KMFavorites) return;
      window.KMFavorites.toggle(btn.getAttribute("data-fav-id"));
    });

    $("refresh-btn").addEventListener("click", requestRefresh);

    wireCatchUp();
    wireReplay();
    wireSettings();
    wirePlaybackSpeed();
  }

  // Gear icon: Manage Favorites, Dark/Light Mode, Slideshow speed - same
  // simple open/close pattern as the Favorites panel itself.
  function wireSettings() {
    var modal = $("settings-modal");
    var openBtn = $("settings-btn");
    if (!modal || !openBtn) return;
    function closeSettings() { modal.hidden = true; }
    openBtn.addEventListener("click", function () { modal.hidden = false; });
    $("settings-close").addEventListener("click", closeSettings);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeSettings(); });
    // Favorites' own click handler (favorites.js) opens #fav-modal on this
    // same button - this just closes Settings out of the way first so the
    // two panels don't end up stacked on top of each other.
    var favBtn = $("favorites-btn");
    if (favBtn) favBtn.addEventListener("click", closeSettings);
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
      populateObcChips();
      Array.prototype.forEach.call(document.querySelectorAll("#obc-chips .chip"), function (c) {
        c.classList.toggle("active", c.getAttribute("data-obc") === filters.obc);
      });
      renderMaybeLoading();
      // Mirrors boot()'s own post-load step - without this, Catch Me Up's
      // banner and cursor only ever refresh on a full page load, not after a
      // manual "Refresh now" that pulls in new plays mid-session.
      return computeCatchUp().then(function (groups) {
        data.catchUpGroups = groups;
        renderCatchUpBanner();
      });
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
      populateObcChips();
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

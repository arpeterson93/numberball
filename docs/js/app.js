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
    // Exact match against either the raw result code (GO, DP21, ...) or the
    // play's own computed scorecard notation (4-6-3, F8, ...) - a fielding
    // sequence isn't a fixed enum the way result codes are (see
    // playFieldingNotation), so this can't be a lookup, only a per-play check.
    if (filters.resultCode && m.result !== filters.resultCode &&
      playFieldingNotation(m) !== filters.resultCode) return false;
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

  // Field canvas, widened toward the reference image's ~460x300 aspect
  // (physics-redesign plan Part 6.1/OQ-4). Height grown from the original
  // 300 to 335 (Alex's report): .scene-diamond renders with
  // overflow:visible, and the warning track/grass now wrap all the way
  // behind home (BEHIND_HOME_R_FT, field-geometry refinement round) - at
  // HOME_SCREEN_Y=282 the deepest behind-home point already projects to
  // y~=326, past the old 300 viewBox, so the diamond's own bottom edge was
  // bleeding into the play-result/launch-angle/exit-velo text below it in
  // the slideshow instead of being visibly contained. 335 keeps a ~9px
  // margin past that deepest point - was 350 (~24px margin) until a second
  // report that the taller canvas pushed the whole slide into needing a
  // vertical scroll on short viewports, chopping the scorebug off the top
  // on widescreen; trimmed back toward the minimum that still clears the
  // overlap. FIELD_W/HOME_SCREEN_Y untouched - this only adds clear canvas
  // below home, it doesn't move or rescale anything already drawn.
  var FIELD_W = 460, FIELD_H = 335;

  // ── Perspective projection (physics-redesign plan Part 6.1) ───────────────
  // A single analytic pinhole projection, JS-side, replacing the old flat
  // ft/unit scale - the ball needs true z rendering (a point off the field
  // plane), which a 2D scale factor can never produce. World: x ft toward
  // 1B, y ft toward CF, z ft up, origin = home plate. Camera sits behind and
  // above home at (0, -CAM_BACK, CAM_UP), looking at (0, LOOK_Y, 0), no
  // roll/yaw - so screen X is always a pure function of world x (the
  // 2B-to-home line is guaranteed vertical on screen by construction) and
  // the camera-space "up" axis is a fixed rotation of world (y,z) by the
  // camera's pitch. Feel knobs, tuned against baseballflightsim.jpg per the
  // plan's acceptance criteria (whole fence arc in view with margin for a
  // 428ft HR's fade point; home plate bottom-center; dirt circle visibly
  // elliptical; a 100ft-apex fly ball's arc reads above the fence) - revisit
  // in Stage E.
  // Steeper, wider-angle than the first pass (Alex's reference photo: the
  // fence spans nearly the full frame width and sits close to the top, home
  // plate close to the bottom - a higher, closer camera with a wider FOCAL
  // than a "normal" broadcast lens).
  var CAM_BACK = 145, CAM_UP = 275, LOOK_Y = 145, FOCAL = 360;
  var SCREEN_CX = 230;
  // Desired screen y for home plate (bottom-center, with room below for the
  // plate marker/dugouts) - SCREEN_CY itself is solved from this below so
  // the camera constants above stay the only real tunables.
  var HOME_SCREEN_Y = 282;

  var CAM_L = Math.hypot(LOOK_Y + CAM_BACK, CAM_UP);
  var FWD_Y = (LOOK_Y + CAM_BACK) / CAM_L, FWD_Z = -CAM_UP / CAM_L;
  var UP_Y = -FWD_Z, UP_Z = FWD_Y;   // camera "up" = right(1,0,0) x forward, no roll
  var SCREEN_CY = (function () {
    var dy0 = 0 + CAM_BACK, dz0 = 0 - CAM_UP;
    var depth0 = FWD_Y * dy0 + FWD_Z * dz0;
    var up0 = UP_Y * dy0 + UP_Z * dz0;
    return HOME_SCREEN_Y + FOCAL * up0 / depth0;
  })();

  // The single conversion point from field-plane feet (home at the origin,
  // +y = depth into the outfield, +x toward 1B, +z up) to SVG pixels. Every
  // field element - infield, outfield, fielders, fence, ball flight - renders
  // through this (ball-flight-plan.md Stage 4a / physics-redesign Part 6:
  // this codebase's bug history is mostly coordinate-space confusion, so
  // there is exactly one ft->px mapping, full stop).
  function projectFt(x, y, z) {
    var dy = y + CAM_BACK, dz = z - CAM_UP;
    var depth = FWD_Y * dy + FWD_Z * dz;
    var upComp = UP_Y * dy + UP_Z * dz;
    return {
      x: SCREEN_CX + FOCAL * x / depth,
      y: SCREEN_CY - FOCAL * upComp / depth,
      depth: depth,
    };
  }
  function ftToSvg(xFt, yFt) { return projectFt(xFt, yFt, 0); }
  var HOME_SVG = ftToSvg(0, 0);

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
  // plateR (sceneFieldHtml) - kept here too since platePath needs it and
  // lives outside that function.
  var HOME_PLATE_R = BASE_R * 0.9;
  // A true world point on the foul-line bearing itself (landingPoint, not a
  // flat screen-pixel offset off SCENE_BASES.HOME) - foulLineD and
  // infieldSkinHtml's outer dirt edge both start here and both aim at other
  // true-bearing points further out (the fence, the dirt skin's own edge),
  // so sharing one true-bearing start point makes those two strokes
  // perfectly collinear in projection instead of visibly forking apart
  // near home (was a flat pixel offset - two straight segments from an
  // off-bearing point to two different on-bearing far points don't project
  // to the same line, which read as a grass strip wedged between the
  // infield dirt's edge and the foul line - point 7 of Alex's field-
  // geometry refinement request). Magnitude is cosmetic (roughly a real
  // plate's half-width); only being exactly on the bearing matters.
  var HOME_PLATE_CORNER_FT = 1.5;
  function homePlateCorner(side) {
    var pt = landingPoint(HOME_PLATE_CORNER_FT, side >= 0 ? 90 : 0);
    return ftToSvg(pt.x, pt.y);
  }

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
  // The researched, documented depths - one source of truth for infield
  // geometry (physics-redesign plan Part 4.1, resolves F5: this used to be
  // duplicated as a second, independently hand-placed set of anchors below,
  // which could and did drift out of sync with these numbers).
  var INFIELDER_DEPTH_FT = { "3B": 119, SS: 147, P: 60, "2B": 147, "1B": 111 };
  // Midpoint HZ angle of each position's own bucket set (see
  // HZ_FIELDER_BY_ANGLE): {5,13}->9, {21,29,37}->29, {45}->45, {53,61,69}->61,
  // {77,85}->81 - the "straight out from home at this depth" direction used
  // to derive that position's anchor point below.
  var CANONICAL_ANGLE = { "3B": 9, SS: 29, P: 45, "2B": 61, "1B": 81 };
  // The minimum lattice angle mapping to each position - the direction a
  // BRC-excluded ground ball gets redirected to on override (Part 4.2/4.3).
  var MIN_ANGLE_FOR_POS = { "3B": 5, SS: 21, P: 45, "2B": 53, "1B": 77 };
  // Traditional scorecard numbering (fieldingNotation, below outThrowTargets) -
  // the app's own position strings already match the real 9 defensive spots
  // 1-for-1, they've just never had scorecard numbers attached before.
  var POSITION_NUMBER = { P: 1, C: 2, "1B": 3, "2B": 4, "3B": 5, SS: 6, LF: 7, CF: 8, RF: 9 };

  // Nine generic fielder anchors, field-plane feet. No names, no per-play
  // defensive alignment - that data doesn't exist (ball-flight-plan.md
  // Decision 5). Infield anchors are DERIVED from INFIELDER_DEPTH_FT/
  // CANONICAL_ANGLE rather than hand-placed (physics-redesign plan Part 4.1) -
  // one real-world-unit source instead of two that can disagree (a 3B anchor
  // used to sit at the hand-placed (-75,85), about 13ft from where its own
  // documented 119ft depth actually points; that's now impossible by
  // construction). Outfield + C anchors stay hand-placed - no depth table
  // exists for them.
  var FIELDER_ANCHORS_FT = {
    C: { x: 0, y: -5 },
    LF: { x: -200, y: 260 }, CF: { x: 0, y: 320 }, RF: { x: 200, y: 260 },
  };
  // Deep enough to read as center field grass, clear of the infield dirt
  // skin and mound, but shallow enough that the mark's own box doesn't
  // reach the fence, where the batting team's watermark is painted
  // (sceneFieldHtml).
  var CF_MARK_DEPTH_FT = 245;
  ["3B", "SS", "P", "2B", "1B"].forEach(function (pos) {
    var pt = landingPoint(INFIELDER_DEPTH_FT[pos], CANONICAL_ANGLE[pos]);
    FIELDER_ANCHORS_FT[pos] = { x: pt.x, y: pt.y };
  });

  // The archetype (from the result's own band row) is the true "is this a
  // ground ball" signal - LA alone is too noisy near the boundary (a real
  // "GO" can compute an LA a hair above the ground/air threshold, and a
  // caught line drive can compute one a hair below it). Anything gating
  // ground-ball-only behaviour (infielder depth, interception) keys off
  // archetype instead.
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

  // 60.5ft from home to the pitcher's plate - the dirt circle itself is
  // INFIELD_SKIN_DIRT_R_FT, defined with infieldSkinHtml below (also this
  // circle's own centre); dirtEdgeFt reads that one directly rather than
  // keeping a second radius here in sync with it by hand.
  var PITCHER_MOUND_FT = 60.5;

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

  // The fence as one SVG polyline, drawn once per field render but built
  // from the same landingPoint/ftToSvg (now projectFt) functions everything
  // else uses - no separate hand-derived geometry to get out of sync with
  // the actual math. Since the fence is uniform, it's a circular arc of
  // radius FENCE_DEPTH_FT from the 3B foul line (angle 0) to the 1B foul
  // line (angle 90) in world feet - but under perspective a world circle no
  // longer projects to a circular SVG arc, so it's sampled instead
  // (physics-redesign plan Part 6.2).
  var FENCE_SAMPLE_STEP_DEG = 3;
  // A small rounding pad past each foul line (Alex's reference photo: the
  // fence/warning-track corner reads as a smooth cap, not a pinched point) -
  // purely cosmetic, the same uniform-radius circle just sampled a few
  // degrees further each way. Never touches gameplay: fenceAt/clampToFence
  // are only ever called with real HZ angles in [5,85], well inside this
  // padded range. 0 (Alex's field-geometry editor export): the fence/wall
  // stops exactly at the foul line, no rounded cap; the much wider
  // foul-ground sweep beyond it is boundaryRFt's job, not this pad's.
  var FENCE_VISUAL_PAD_DEG = 0;
  // Every angle the fence/wall/warning-track arcs sample, shared so all
  // three stay corner-for-corner consistent with each other.
  function fenceSampleAngles() {
    var angles = [];
    for (var a = -FENCE_VISUAL_PAD_DEG; a <= 90 + FENCE_VISUAL_PAD_DEG; a += FENCE_SAMPLE_STEP_DEG) {
      angles.push(a);
    }
    return angles;
  }
  function fenceArcPoints() {
    return fenceSampleAngles().map(function (a) {
      var ft = landingPoint(FENCE_DEPTH_FT, a);
      return ftToSvg(ft.x, ft.y);
    });
  }
  function polylineD(pts) {
    return pts.map(function (p, i) { return (i === 0 ? "M" : "L") + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ");
  }
  function fencePathD() { return polylineD(fenceArcPoints()); }

  // Grass/foul-ground outer boundary, radius from home as a function of
  // bearing offset (landingPoint's own convention: offset 0 = dead centre,
  // +-45 = the foul lines) - purely a visual shape. Hand-tuned in the Field
  // Geometry Editor artifact (drag-to-match against a reference photo) and
  // exported as this control-point table rather than a formula: linear
  // interpolation between points, held at FENCE_DEPTH_FT from 0-45 (fair
  // territory, matching the fence exactly) and at the last point's radius
  // beyond 180. offsetDeg 45 (the foul line itself) is the implicit first
  // boundary condition, not listed - every table starts its own taper from
  // there.
  //
  // A real, uncapped sidespin drift can still carry a caught fly/pop foul
  // (CAUGHT_IN_AIR archetypes are exempt from clampFairTerritory, by design
  // - "a neat artifact") out past this narrower shape before landing -
  // swept every FO/DFO/SacF/DSacF/PO band's extreme EV/LA/phi/hand
  // combinations and confirmed results can stay near full fence depth out
  // to ~62-64 degrees of offset (a deep fly ball that hooks foul late is
  // still a deep fly ball) before collapsing under 40ft beyond ~70 degrees.
  // Rather than flaring this whole boundary out to match that rare extreme
  // (which read as a wide, unnatural "wing" past each foul pole), the ball
  // itself is radially capped to this same boundary at render time -
  // distanceCap, below, alongside clampToFence - so the diagram stays the
  // narrow shape Alex wants while every caught ball still lands on it.
  var BOUNDARY_SAMPLE_STEP_DEG = 3;
  function boundarySampleOffsets() {
    var offsets = [];
    for (var o = -180; o <= 180; o += BOUNDARY_SAMPLE_STEP_DEG) offsets.push(o);
    return offsets;
  }

  var BOUNDARY_TABLE = [
    { angle: 46, r: 355 }, { angle: 48, r: 315 }, { angle: 50, r: 270 },
    { angle: 52, r: 235 }, { angle: 55, r: 180 }, { angle: 58, r: 130 },
    { angle: 66, r: 80 }, { angle: 74, r: 60 }, { angle: 80, r: 50 },
    { angle: 95, r: 40 }, { angle: 115, r: 37 }, { angle: 140, r: 35 },
    { angle: 165, r: 35 }, { angle: 180, r: 35 },
  ];
  // BOUNDARY_TABLE entries carry their value as .r, TRACK_WIDTH_TABLE's as
  // .w (matching the Field Geometry Editor's own export field names) - read
  // whichever is present rather than hardcoding one, so this one lookup
  // serves both tables. (Previously hardcoded .r for both, which left
  // every track-width lookup reading undefined -> NaN warning-track
  // coordinates - the field rendered as one solid dirt-coloured blob with
  // no visible ring, since a "NaN" point breaks a closed SVG subpath.)
  function tableVal(entry) { return entry.r !== undefined ? entry.r : entry.w; }
  function tableLookup(table, offsetDegAbs, fenceFloorDeg) {
    if (fenceFloorDeg != null && offsetDegAbs <= fenceFloorDeg) return FENCE_DEPTH_FT;
    if (offsetDegAbs <= table[0].angle) {
      if (fenceFloorDeg == null) return tableVal(table[0]);
      var t0 = (offsetDegAbs - fenceFloorDeg) / (table[0].angle - fenceFloorDeg);
      return FENCE_DEPTH_FT + (tableVal(table[0]) - FENCE_DEPTH_FT) * t0;
    }
    for (var i = 0; i < table.length - 1; i++) {
      if (offsetDegAbs >= table[i].angle && offsetDegAbs <= table[i + 1].angle) {
        var t = (offsetDegAbs - table[i].angle) / (table[i + 1].angle - table[i].angle);
        return tableVal(table[i]) + (tableVal(table[i + 1]) - tableVal(table[i])) * t;
      }
    }
    return tableVal(table[table.length - 1]);
  }

  /* Smoothing layer, sitting above the raw control-point tables rather than
     touching them (Alex's ask: the Field Geometry Editor's exported JSON is
     the authored input, left exact - this only shapes what gets read out of
     it). tableLookup is piecewise-LINEAR between control points, so wherever
     two adjacent points swing sharply - the 270->130->80->60->50ft run from
     angle 50 to 80, i.e. home plate down each foul line, is by far the
     steepest in the table - the boundary reads as a faceted polygon instead
     of a curve. Fix: sample the raw lookup, run a binomial (approximately
     Gaussian) moving-average across it, and have boundaryRFt/trackWidthFt
     read from the smoothed samples instead of the raw table directly.

     Exactly one sample is deliberately EXCLUDED from that average: the pin
     point (see buildSmoothedSamples) - fenceFloorDeg (45) for BOUNDARY_TABLE,
     where the curve has to mesh with the fence exactly, or the table's own
     first control point for TRACK_WIDTH_TABLE. A first version protected
     the whole flat-at-FENCE_DEPTH_FT plateau below the pin AND left the
     45-50 ramp down to the table's first real point unsmoothed too - the
     ramp's straight edge butted up against the smoothed curve past 50 in
     its own visible kink, right where the warning track meets the wall
     (Alex's report, after an earlier version smoothed across the fence
     transition itself and dipped inside the fence at the pole - also
     Alex's report). edgeSmooth's protectBelowIndex now pins only the ONE
     sample AT the fence/pole itself; every sample past it, ramp included,
     smooths with a window clamped so it can reach back to that pin but
     never past it - the corner stays flush with the fence and the curve
     leading away from it is continuous, no separate unsmoothed ramp
     segment.

     boundaryRFt/trackWidthFt are the ONE place both physics (distanceCap,
     capRollToBoundary, resolveGrounderInterception's depth checks) and
     rendering (grassPathD, warningTrackPathD) read the boundary from -
     smoothing lives here, at the source, so a caught/rolled ball is always
     capped against the exact same curve that gets drawn, never a jagged
     physics boundary under a smooth visual one (or vice versa). */
  var BOUNDARY_SMOOTH_KERNEL = [1, 6, 15, 20, 15, 6, 1];  // binomial, ~9deg half-width at the 3deg sample step
  function edgeSmooth(values, weights, protectBelowIndex) {
    // values is a plain 0..180deg sweep (offsetDegAbs), not a closed ring -
    // no wraparound, since BOUNDARY_TABLE is itself only ever read via
    // Math.abs(offsetDeg). Indices below protectBelowIndex are copied
    // through untouched; the window for indices at/above it clamps at that
    // same edge instead of reaching below it.
    var half = (weights.length - 1) / 2;
    var wsum = 0;
    for (var w = 0; w < weights.length; w++) wsum += weights[w];
    var n = values.length;
    var out = values.slice();
    for (var i = protectBelowIndex; i < n; i++) {
      var acc = 0;
      for (var k = -half; k <= half; k++) {
        var idx = i + k;
        if (idx < protectBelowIndex) idx = protectBelowIndex;
        if (idx > n - 1) idx = n - 1;
        acc += values[idx] * weights[k + half];
      }
      out[i] = acc / wsum;
    }
    return out;
  }
  function buildSmoothedSamples(table, fenceFloorDeg) {
    var steps = Math.round(180 / BOUNDARY_SAMPLE_STEP_DEG);
    var raw = [];
    for (var i = 0; i <= steps; i++) raw.push(tableLookup(table, i * BOUNDARY_SAMPLE_STEP_DEG, fenceFloorDeg));
    // The pin point is exactly where this curve has to mesh with something
    // outside the table - the fence itself, for BOUNDARY_TABLE, at
    // fenceFloorDeg (45); just the table's own first control point for
    // TRACK_WIDTH_TABLE (no fence to match, fenceFloorDeg is null). Only
    // the ONE sample AT that point is left untouched; everything past it -
    // including the 45-50 ramp down to the table's first real point, which
    // read as its own unsmoothed straight-line kink butted up against the
    // smoothed curve beyond 50 (Alex's report: the warning track's last
    // segment into the wall corner didn't match the rest) - now smooths
    // too, clamped so its window never reaches back past the pin and
    // never pulls the corner itself off the fence.
    var pinDeg = fenceFloorDeg != null ? fenceFloorDeg : table[0].angle;
    var protectBelowIndex = Math.floor(pinDeg / BOUNDARY_SAMPLE_STEP_DEG) + 1;
    return edgeSmooth(raw, BOUNDARY_SMOOTH_KERNEL, protectBelowIndex);
  }
  // offsetDeg -> linear interpolation between the two nearest smoothed
  // samples (3deg apart, over offsetDegAbs 0..180) - a continuous curve for
  // any angle, not just the sampled grid, built once at load and shared by
  // every caller below.
  function smoothedLookup(samples, offsetDeg) {
    var abs = Math.min(180, Math.abs(offsetDeg));
    var pos = abs / BOUNDARY_SAMPLE_STEP_DEG;
    var i0 = Math.floor(pos);
    var i1 = Math.min(i0 + 1, samples.length - 1);
    var t = pos - i0;
    return samples[i0] + (samples[i1] - samples[i0]) * t;
  }

  var SMOOTHED_BOUNDARY_R = buildSmoothedSamples(BOUNDARY_TABLE, 45);
  function boundaryRFt(offsetDeg) {
    return smoothedLookup(SMOOTHED_BOUNDARY_R, offsetDeg);
  }
  function boundaryFt() {
    return boundarySampleOffsets().map(function (o) { return landingPoint(boundaryRFt(o), o + 45); });
  }
  function grassPathD() {
    return polylineD(boundaryFt().map(function (p) { return ftToSvg(p.x, p.y); })) + " Z";
  }

  // The fence drawn as a short wall rather than a flat line (Part 6.1/OQ-8) -
  // the same ground-level arc duplicated at FENCE_WALL_HEIGHT_FT and filled
  // between, so an over-the-fence home run visibly crosses above it instead
  // of just touching a line.
  var FENCE_WALL_HEIGHT_FT = 8;
  function fenceWallPathD() {
    var ground = fenceArcPoints();
    var top = fenceSampleAngles().map(function (a) {
      var ft = landingPoint(FENCE_DEPTH_FT, a);
      return projectFt(ft.x, ft.y, FENCE_WALL_HEIGHT_FT);
    });
    var d = polylineD(ground);
    for (var i = top.length - 1; i >= 0; i--) {
      d += " L" + top[i].x.toFixed(1) + "," + top[i].y.toFixed(1);
    }
    return d + " Z";
  }

  // Warning-track width (ft) by bearing, same control-point/interpolation
  // scheme and same editor export as BOUNDARY_TABLE above - editable
  // independently per angle rather than one flat width, so e.g. the
  // corners can read wider than the deep alleys.
  var TRACK_WIDTH_TABLE = [
    { angle: 46, w: 25 }, { angle: 48, w: 40 }, { angle: 50, w: 40 },
    { angle: 52, w: 40 }, { angle: 55, w: 40 }, { angle: 58, w: 40 },
    { angle: 66, w: 15 }, { angle: 74, w: 14 }, { angle: 80, w: 8 },
    { angle: 95, w: 8 }, { angle: 115, w: 8 }, { angle: 140, w: 8 },
    { angle: 165, w: 8 }, { angle: 180, w: 8 },
  ];
  var SMOOTHED_TRACK_W = buildSmoothedSamples(TRACK_WIDTH_TABLE, null);
  function trackWidthFt(offsetDeg) {
    return smoothedLookup(SMOOTHED_TRACK_W, offsetDeg);
  }

  // The warning track as ONE continuous band around the WHOLE field
  // boundary (Alex's reference photo) - behind home plate, down both foul
  // territories, around the outfield curve, and back - not just the curved
  // fence arc. Outer edge is exactly grassPathD's own boundary (so the
  // track never floats free of the grass edge); inner edge is the same
  // bearing at boundaryRFt(angle) - trackWidthFt(angle), i.e. each sampled
  // point pulled straight in along its own ray by that angle's own width,
  // not a uniform inset.
  //
  // Two SEPARATE closed subpaths (M..Z M..Z), rendered evenodd - NOT one
  // path that walks the outer loop then jumps to the inner loop and walks
  // it in reverse before a single closing Z. That single-path "seam"
  // construction looks like a textbook annulus and normally works, but
  // with a boundary/width table producing a highly non-circular, sharply
  // varying radius, the seam segment (outer's last sample back to inner's
  // last sample, both essentially the same bearing) turned out to fill the
  // ENTIRE outer region solid under both nonzero and evenodd - rendered it
  // in total isolation (bare SVG, no app CSS) to confirm it wasn't a
  // layering/CSS issue, then confirmed the two-independent-loops version
  // renders a correct ring in the same isolated test before porting the
  // fix back here.
  function warningTrackPathD() {
    var offsets = boundarySampleOffsets();
    var outer = [], inner = [];
    for (var i = 0; i < offsets.length; i++) {
      var o = offsets[i];
      var R = boundaryRFt(o), w = trackWidthFt(o);
      var op = landingPoint(R, o + 45), ip = landingPoint(Math.max(0, R - w), o + 45);
      outer.push(ftToSvg(op.x, op.y));
      inner.push(ftToSvg(ip.x, ip.y));
    }
    return polylineD(outer) + " Z " + polylineD(inner) + " Z";
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

  // A world-space circle (any centre/radius), sampled into a polyline the
  // same way the fence/dirt arcs are (Part 6.2 - a circle doesn't project to
  // a circular SVG arc under perspective).
  function circlePathD(cxFt, cyFt, rFt, steps) {
    var pts = [];
    for (var i = 0; i <= steps; i++) {
      var a = 2 * Math.PI * i / steps;
      pts.push(ftToSvg(cxFt + rFt * Math.sin(a), cyFt + rFt * Math.cos(a)));
    }
    return polylineD(pts) + " Z";
  }

  // "Modified" dirt cutout (Alex's second reference photo) - a wide dirt
  // arc over the mound down to each foul line, with the actual basepath
  // square left as a grass island in the middle, rather than the thinner
  // basepath-only strips the first pass drew. One <path> with two subpaths
  // (the outer cutout, then the inner grass diamond) and fill-rule=evenodd
  // (CSS) does the hole - simpler and more robust than boolean path ops.
  // INFIELD_SKIN_DIRT_R_FT also doubles as the physics dirt-clearance
  // radius (dirtEdgeFt, Part 4.6's rollout floor) - it used to be purely
  // decorative, deliberately kept apart from a separate "real-park" 95ft
  // gameplay radius, but that let a hit rest visually on the drawn dirt
  // (which had grown to 115ft) while the physics floor still only demanded
  // 95+3ft of clearance - a "Single" resting right at the dirt's edge
  // instead of past it (Alex's report). One radius now, read by both.
  //
  // 115 reaches beyond the new BOUNDARY_TABLE's foul territory at most
  // angles (e.g. ~125ft reach at the 66-degree mark vs an 80ft boundary
  // there) - tried shrinking this to 70 to fit inside it everywhere, but
  // Alex asked for 115 back regardless, so the dirt patch legitimately
  // does extend past the grass/warning-track edge in foul ground now; the
  // patch itself doesn't get clipped to the boundary shape, it's drawn as
  // its own circle on top.
  var INFIELD_SKIN_DIRT_R_FT = 115;
  var HOME_DIRT_R_FT = 16;
  // The mound reads as its own small dirt patch surrounded by grass (Alex's
  // reference photo), not merged into the big cutout - true regardless of
  // INFIELD_SKIN_DIRT_R_FT, since the mound point always sits well inside
  // the grass-diamond hole below.
  var MOUND_DIRT_R_FT = 11;
  // Interior dirt geometry, hand-tuned in the Field Geometry Editor artifact
  // and exported as this constant set. Perpendicular dirt-band thickness
  // (ft) off the true home-1B/home-3B baselines and 1B-2B/2B-3B baselines
  // respectively - NOT a point on the centerline. An earlier version used a
  // single point per corner, which tapered the band like a triangle instead
  // of keeping it parallel to the line (Alex's correction) - see
  // basepathOffset/infieldSkinHtml below for the fix.
  var HOME_THICKNESS_FT = 5;
  var SECOND_THICKNESS_FT = 5;
  // Circular dirt wedge radius at 1B/3B, centred exactly on the true base
  // and facing the mound, replacing a rounded bezier bulge at the diamond's
  // corner there (home/2B stay plain vertices).
  var WEDGE_R_FT = 20;
  // How far past each foul line (perpendicular) the infield dirt patch
  // extends into foul ground - the patch is otherwise a circle centred on
  // the fair-territory mound, so without this grass would touch the line
  // immediately in foul ground (Alex's report). The far end hands off to an
  // EXACT point on the patch's own INFIELD_SKIN_DIRT_R_FT circle (via
  // lineCircleNear), not a straight cutoff, so the foul-side edge reads as
  // a continuation of that same arc.
  var FOUL_MARGIN_FT = 3;

  // Where a ray (from P, unit direction d) FIRST crosses a circle (centre
  // C, radius R) going forward - the smallest non-negative t, not just the
  // smaller of the two roots. Those are the same thing when P starts
  // outside the circle (e.g. the 1B/3B wedges: the home/2B band points are
  // far outside their own ~20ft wedge circle), but NOT when P starts inside
  // a much bigger circle (e.g. the foul-side dirt band: a point a few feet
  // from home sits deep inside the 115ft mound circle) - there the smaller
  // root is negative (behind P, not a real forward crossing) and taking it
  // anyway collapses the polygon.
  function lineCircleNear(P, d, C, R) {
    var fx = P.x - C.x, fy = P.y - C.y;
    var b = 2 * (fx * d.x + fy * d.y);
    var c = fx * fx + fy * fy - R * R;
    var disc = Math.max(0, b * b - 4 * c);
    var sq = Math.sqrt(disc);
    var t1 = (-b - sq) / 2, t2 = (-b + sq) / 2; // t1 <= t2 always
    return { x: P.x + d.x * (t1 >= 0 ? t1 : t2), y: P.y + d.y * (t1 >= 0 ? t1 : t2) };
  }
  // The true baseline A->B's own direction (unit vector) - used to aim the
  // constant-thickness band edges and wedge-facing rays at the correct true
  // bearing (the "toward `interior`" perpendicular half of this used to
  // also return an offset point, but every caller here only ever wants the
  // direction).
  function basepathOffset(A, B) {
    var dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1;
    return { dir: { x: dx / len, y: dy / len } };
  }
  // The angle (in this function's own arc convention: x=r*sin(phi),
  // y=cy-r*cos(phi), i.e. phi=atan2(x-cx,cy-y)) of a point on a circle
  // centred at c.
  function phiOnCircle(pt, c) { return Math.atan2(pt.x - c.x, c.y - pt.y); }
  // A circular arc's points (world ft) sweeping the short way from angle a0
  // to a1, centred at `base` radius R - used for the 1B/3B wedge cutouts.
  function wedgeArcFt(base, R, a0, a1, steps) {
    var d = a1 - a0;
    while (d > Math.PI) d -= 2 * Math.PI;
    while (d < -Math.PI) d += 2 * Math.PI;
    var pts = [];
    for (var i = 0; i <= steps; i++) {
      var a = a0 + d * i / steps;
      pts.push(ftToSvg(base.x + R * Math.sin(a), base.y - R * Math.cos(a)));
    }
    return pts;
  }

  function infieldSkinHtml() {
    var m = PITCHER_MOUND_FT, r = INFIELD_SKIN_DIRT_R_FT;
    var mound = { x: 0, y: m };
    var homeTrue = { x: 0, y: 0 };
    var b1 = { x: BASE_DIST_FT * Math.SQRT1_2, y: BASE_DIST_FT * Math.SQRT1_2 };
    var b3 = { x: -BASE_DIST_FT * Math.SQRT1_2, y: BASE_DIST_FT * Math.SQRT1_2 };

    // Foul-side dirt: a true constant-width band off the real baseline
    // (like the home/2B interior bands below), handed off to an exact
    // point on the patch's own r-radius circle rather than a straight cut.
    var M = FOUL_MARGIN_FT;
    var foulNormal1 = { x: Math.SQRT1_2, y: -Math.SQRT1_2 }, dir1 = { x: Math.SQRT1_2, y: Math.SQRT1_2 };
    var foulNormal3 = { x: -Math.SQRT1_2, y: -Math.SQRT1_2 }, dir3 = { x: -Math.SQRT1_2, y: Math.SQRT1_2 };
    var homeFoulOut1 = { x: homeTrue.x + foulNormal1.x * M, y: homeTrue.y + foulNormal1.y * M };
    var homeFoulOut3 = { x: homeTrue.x + foulNormal3.x * M, y: homeTrue.y + foulNormal3.y * M };
    var foulArcStart1 = lineCircleNear(homeFoulOut1, dir1, mound, r);
    var foulArcStart3 = lineCircleNear(homeFoulOut3, dir3, mound, r);
    var phi1 = phiOnCircle(foulArcStart1, mound);
    var phi3 = phiOnCircle(foulArcStart3, mound);
    while (phi3 < phi1) phi3 += 2 * Math.PI; // long way round (through CF), same convention infieldSkinHtml always used

    var steps = 18, arcPts = [];
    for (var i = 0; i <= steps; i++) {
      var phi = phi1 + (phi3 - phi1) * i / steps;
      arcPts.push(ftToSvg(mound.x + r * Math.sin(phi), mound.y - r * Math.cos(phi)));
    }
    arcPts.reverse(); // sweep computed 1B->3B; the path below runs home-out-3 -> arc -> home-out-1, so flip to 3B->1B
    var homeL = homePlateCorner(-1), homeR = homePlateCorner(1);
    var outer = polylineD(
      [ftToSvg(homeFoulOut3.x, homeFoulOut3.y)].concat(arcPts).concat([
        ftToSvg(homeFoulOut1.x, homeFoulOut1.y), homeR, homeL,
      ])
    ) + " Z";

    // Constant-thickness dirt band along each true basepath (home-1B,
    // 1B-2B, 2B-3B, 3B-home), independently adjustable near home vs near
    // 2nd, with a circular dirt wedge cut around each base (centred exactly
    // on the true base) instead of a sharp corner - home stays a plain
    // vertex. The home point is where the two home-adjacent offset lines
    // meet on the centreline (a clean closed form here since every basepath
    // meets the centreline at 45 degrees: offset by thickness T
    // perpendicular puts that intersection T*sqrt(2) along the centreline);
    // b2 is that same construction for 2nd, used only to aim the 1B/3B-side
    // rays at the correct offset line (2nd's own corner is now a wedge too,
    // below - see arr2/dep2).
    var b2True = { x: 0, y: BASE_DIAG_FT };
    var homeIn = { x: 0, y: HOME_THICKNESS_FT * Math.SQRT2 };
    var b2 = { x: 0, y: BASE_DIAG_FT - SECOND_THICKNESS_FT * Math.SQRT2 };

    var R = WEDGE_R_FT;
    var dH1 = basepathOffset(homeTrue, b1).dir;
    var d12 = basepathOffset(b2True, b1).dir;
    var app1 = lineCircleNear(homeIn, dH1, b1, R);
    var exit1 = lineCircleNear(b2, d12, b1, R);
    var wedge1 = wedgeArcFt(b1, R, phiOnCircle(app1, b1), phiOnCircle(exit1, b1), 10);

    var d23 = basepathOffset(b2True, b3).dir;
    var dH3 = basepathOffset(homeTrue, b3).dir;
    var app3 = lineCircleNear(b2, d23, b3, R);
    var exit3 = lineCircleNear(homeIn, dH3, b3, R);
    var wedge3 = wedgeArcFt(b3, R, phiOnCircle(app3, b3), phiOnCircle(exit3, b3), 10);

    // 2nd base gets the same circular wedge treatment 1B/3B already have
    // (Alex's ask), just built off the 1B/3B-side offset lines instead of
    // home's: exit1 and app3 are already exact points on the 1B-2B and
    // 2B-3B interior edges (where those edges cross the OTHER base's own
    // wedge circle), so a ray from each, aimed back along its own edge
    // (i.e. the reverse of d12/d23) toward 2nd, lands exactly on that same
    // edge where it meets 2nd's wedge circle - no new line construction
    // needed. Sweeping the short way between those two points bulges the
    // arc toward home (the same "faces the interior neighbour" pattern
    // 1B/3B's wedges already have, just pointed at home instead of the
    // mound - verified by construction, not hand-picked: the short arc
    // between a 1B-side point and a 3B-side point on 2nd's own circle can
    // only pass through 2nd's home-facing side).
    var arr2 = lineCircleNear(exit1, { x: -d12.x, y: -d12.y }, b2True, R);
    var dep2 = lineCircleNear(app3, { x: -d23.x, y: -d23.y }, b2True, R);
    var wedge2 = wedgeArcFt(b2True, R, phiOnCircle(arr2, b2True), phiOnCircle(dep2, b2True), 10);

    var homeInPx = ftToSvg(homeIn.x, homeIn.y);
    var app1Px = ftToSvg(app1.x, app1.y), app3Px = ftToSvg(app3.x, app3.y);
    var arr2Px = ftToSvg(arr2.x, arr2.y);
    var hole = "M" + homeInPx.x.toFixed(1) + "," + homeInPx.y.toFixed(1) +
      " L" + app1Px.x.toFixed(1) + "," + app1Px.y.toFixed(1) +
      " " + wedge1.map(function (p) { return "L" + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ") +
      " L" + arr2Px.x.toFixed(1) + "," + arr2Px.y.toFixed(1) +
      " " + wedge2.map(function (p) { return "L" + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ") +
      " L" + app3Px.x.toFixed(1) + "," + app3Px.y.toFixed(1) +
      " " + wedge3.map(function (p) { return "L" + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ") +
      " L" + homeInPx.x.toFixed(1) + "," + homeInPx.y.toFixed(1) + " Z";

    return '<path class="dm-infield-dirt" fill-rule="evenodd" d="' + outer + " " + hole + '"></path>' +
      '<path class="dm-home-dirt" d="' + circlePathD(0, 0, HOME_DIRT_R_FT, 20) + '"></path>' +
      '<path class="dm-mound-dirt" d="' + circlePathD(0, PITCHER_MOUND_FT, MOUND_DIRT_R_FT, 20) + '"></path>';
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

  // Radially rescale a KMTraj sample list (x,y only - z/t untouched) - used
  // whenever clampToFence engages, so the drawn arc still ends exactly at
  // the clamped landing point instead of the integrator's raw (uncapped)
  // one. A cheap squash rather than a re-integration at the clamped
  // distance: exact physics at the wall isn't worth it for a visual
  // (physics-redesign plan 2.3).
  function scaleSamples(samples, scale) {
    if (scale === 1) return samples;
    return samples.map(function (s) {
      return { t: s.t, x: s.x * scale, y: s.y * scale, z: s.z };
    });
  }

  // Sidespin drift (Part 1's "tail" - real, workbook-original physics) can
  // push a batted ball's true landing bearing past a foul line even though
  // its HZ bucket sits safely in [5,85]. Fine for a ball caught in the air -
  // a foul fly/pop out is a real, common outcome, and a neat side effect of
  // modeling the drift at all - never fine for anything that stays in play:
  // a "single" that lands foul is a foul ball, not a hit. Rotates every
  // sample (and contactVel) rigidly about home by whatever's needed to bring
  // the LANDING point's bearing back to the nearest foul line - preserves
  // distance/hang time/apex exactly, and since sidespin drift accumulates
  // monotonically over the flight, the landing point is always the
  // most-drifted point on the path, so clamping just that point guarantees
  // every earlier sample is already inside the lines too.
  var FAIR_OFFSET_MAX_DEG = 45;   // landingPoint's own convention: angle = 45 +- this
  function clampFairTerritory(sim, archetype) {
    if (CAUGHT_IN_AIR[archetype]) return sim;
    var offsetDeg = Math.atan2(sim.landing.x, sim.landing.y) * 180 / Math.PI;
    var over = offsetDeg > FAIR_OFFSET_MAX_DEG ? offsetDeg - FAIR_OFFSET_MAX_DEG
      : offsetDeg < -FAIR_OFFSET_MAX_DEG ? offsetDeg + FAIR_OFFSET_MAX_DEG : 0;
    if (!over) return sim;
    var deltaRad = -over * Math.PI / 180;
    var cosD = Math.cos(deltaRad), sinD = Math.sin(deltaRad);
    function rot(x, y) { return { x: x * cosD + y * sinD, y: y * cosD - x * sinD }; }
    var landing = rot(sim.landing.x, sim.landing.y);
    var samples = sim.samples.map(function (s) {
      var r = rot(s.x, s.y);
      return { t: s.t, x: r.x, y: r.y, z: s.z };
    });
    var cv = rot(sim.contactVel.vx, sim.contactVel.vy);
    return {
      distance: sim.distance, hangS: sim.hangS, apexFt: sim.apexFt,
      landing: landing, samples: samples,
      contactVel: { vx: cv.x, vy: cv.y, vz: sim.contactVel.vz },
    };
  }

  // The field-boundary counterpart to clampToFence (Part 4d): a home run is
  // still never capped (it's meant to clear/reach beyond the drawn fence),
  // but everything else - including a CAUGHT_IN_AIR foul exempted from
  // clampFairTerritory - gets radially capped to boundaryRFt's own shape at
  // its landing bearing, so the visual field never has to flare out to
  // match a rare extreme (see boundaryRFt above). FIELD_BOUNDARY_MARGIN_FT
  // keeps the point visibly inside the grass/track, not painted on its
  // outer edge.
  var FIELD_BOUNDARY_MARGIN_FT = 8;
  function distanceCap(sim, angleDeg, isHomeRun) {
    var D = clampToFence(sim.distance, angleDeg, isHomeRun);
    if (isHomeRun) return D;
    var offsetDeg = Math.atan2(sim.landing.x, sim.landing.y) * 180 / Math.PI;
    var maxR = boundaryRFt(offsetDeg) - FIELD_BOUNDARY_MARGIN_FT;
    return Math.min(D, maxR);
  }

  // Re-run the integrator at a new HZ angle, same LA/EV/hand - the direction
  // override mechanism shared by the grounder resolver (Part 4.2) and the
  // caught-in-air BRC override (Part 4.3). Mutates `flight` in place so
  // every already-set field (angle/distance/x/y/contactVel/samples/apexFt/
  // hangS/hangMs/clamped) reflects the new direction; la/ev/archetype never
  // change - only where the ball goes.
  function applyAngleOverride(flight, newAngleDeg, hand, isHomeRun) {
    var sim = clampFairTerritory(KMTraj.simulateFlight(flight.ev, flight.la, newAngleDeg - 45, hand), flight.archetype);
    var D = distanceCap(sim, newAngleDeg, isHomeRun);
    var scale = D / sim.distance;
    flight.angle = newAngleDeg;
    flight.distance = D;
    flight.x = sim.landing.x * scale;
    flight.y = sim.landing.y * scale;
    flight.contactVel = sim.contactVel;
    flight.apexFt = sim.apexFt;
    flight.hangS = sim.hangS;
    flight.hangMs = 1000 * sim.hangS;
    flight.samples = scaleSamples(sim.samples, scale);
    flight.clamped = D !== sim.distance;
  }

  // tables is data.meta.flight: { bands, excluded }. Each band now carries
  // its own laMin/laIdeal/laMax/evMin/evMax/depthMin/depthMax directly (see
  // result_diff_bands.csv) - no separate archetype-keyed range table to look
  // up; `band.archetype` is still a plain category label for
  // CAUGHT_IN_AIR/GROUND_ARCHETYPES/TAG_THROW_ARCHETYPES below. depthMin/
  // depthMax are audit-only now (2.4) - distance comes from the physics
  // integrator, la/ev straight into KMTraj.simulateFlight, for every
  // archetype including grounders (physics-redesign plan Part 2.3).
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
    var isHomeRun = result === "HR";

    // phi = HZ - 45 (physics-redesign plan Part 1, verified); the resolved
    // hand goes into the spin formulas unchanged, not folded into phi a
    // second time - the HZ angle above already carries the hand mirror.
    var sim = clampFairTerritory(KMTraj.simulateFlight(EV, LA, angle - 45, hand), band.archetype);
    var D = distanceCap(sim, angle, isHomeRun);
    var scale = D / sim.distance;

    return {
      la: LA, ev: EV, distance: D, angle: angle,
      x: sim.landing.x * scale, y: sim.landing.y * scale,
      hangMs: 1000 * sim.hangS, hangS: sim.hangS, apexFt: sim.apexFt,
      contactVel: sim.contactVel, samples: scaleSamples(sim.samples, scale),
      clamped: D !== sim.distance,
      fielder: nearestFielder(sim.landing.x * scale, sim.landing.y * scale), archetype: band.archetype,
      // Over-the-fence vs. inside-the-park - both legal outcomes for a home
      // run (ball-flight-plan.md ground-truth invariant note). Never true for
      // anything else: clampToFence already prevented that.
      clearedFence: isHomeRun && D > fenceAt(angle),
    };
  }

  var OF_POSITIONS = { LF: 1, CF: 1, RF: 1 };

  /* import_BRC.csv's optional ExcludedPositions/DefaultPosition columns -
     "the physics-computed fielder for this situation doesn't make sense, use
     this one instead," tested against the HZ answer (or, post-override, the
     resolved position) - never a proximity answer (physics-redesign plan
     Part 4.2, resolves F3: flight.fielder vs the HZ fielder used to disagree
     39% of the time because the exclusion check and the actual assignment
     read two different sources). */
  function brcExcludes(m, pos) {
    var excluded = m.excluded_positions;
    if (!excluded || !excluded.length || !pos) return false;
    var isOF = !!OF_POSITIONS[pos];
    return excluded.indexOf(pos) !== -1 || (isOF && excluded.indexOf("OF") !== -1);
  }

  /* Point along the ball's actual ground-contact direction, `alongFt` beyond
     the landing point - NOT the HZ launch bearing (physics-redesign plan
     Part 1: sidespin drift means those differ, by well under a degree for a
     grounder but more for a harder-hit ball). This is what "fielded" and
     "rolled to" points actually mean physically. */
  function groundDirPoint(flight, alongFt) {
    var vx = flight.contactVel.vx, vy = flight.contactVel.vy;
    var sh = Math.hypot(vx, vy) || 1;
    return { x: flight.x + (vx / sh) * alongFt, y: flight.y + (vy / sh) * alongFt };
  }

  /* One function replaces five disagreeing mechanisms (physics-redesign plan
     Part 4): who fields a ground-ball out, at what corrected direction, at
     what depth, and when. Called once, right after flightParams, for every
     ground-archetype out - every consumer of `flight` (the ball trail, the
     throw origin, labels) sees flight.fielder/fieldedDistFt/groundTimeS/
     rollSamples with no separate plumbing.

     (1) Position, before any physics: the HZ answer, then the BRC check. A
     BRC exclusion overrides DIRECTION only (a lattice angle, so every
     downstream lattice lookup still hits) - LA/EV are untouched, and the
     integrator re-runs at the new angle for a physically consistent new
     landing/contactVel (F1: no post-hoc angle rewrite exists anywhere in
     this path).
     (2) Ground path from the real contact velocity (Part 3).
     (3) Interception: the fielder's depth crossing along that path, or the
     ball dying first (charge-in). fieldedFt <= alongFt always, by
     construction in every branch. */
  function resolveGrounderInterception(m, flight, hand) {
    var hzPos = HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];
    var pos = hzPos;
    if (brcExcludes(m, hzPos) && m.default_position) {
      pos = m.default_position;
      applyAngleOverride(flight, MIN_ANGLE_FOR_POS[pos], hand, false);
    }
    var gp = KMTraj.groundPath(Math.hypot(flight.contactVel.vx, flight.contactVel.vy), flight.contactVel.vz);
    var depth = INFIELDER_DEPTH_FT[pos];
    var alongFt = depth - flight.distance;
    var fieldedFt, groundTimeS;
    if (alongFt <= 0) {
      fieldedFt = 0; groundTimeS = 0;
    } else if (alongFt <= gp.restFt) {
      fieldedFt = alongFt; groundTimeS = gp.timeAt(alongFt);
    } else {
      fieldedFt = gp.restFt; groundTimeS = gp.totalS;
    }
    flight.fielder = pos;
    flight.fieldedDistFt = flight.distance + fieldedFt;
    flight.groundTimeS = groundTimeS;
    flight.groundPath = gp;
  }

  /* The caught-in-air sibling of the resolver above (physics-redesign plan
     Part 4.3): a BRC exclusion on a caught fly ball also overrides direction
     only, clamped into the default outfielder's own angular third rather
     than snapped to that outfielder's anchor point regardless of this ball's
     own EV/LA (today's applyPositionOverride teleported a SacF excluded from
     LF straight to CF's exact (0,320) anchor no matter how it was hit) - the
     catch point stays a physically consistent landing for this ball's actual
     contact quality. */
  var OF_ANGLE_THIRDS = { LF: [5, 33], CF: [33, 57], RF: [57, 85] };
  function applyAirPositionOverride(m, flight, hand) {
    if (!brcExcludes(m, flight.fielder) || !m.default_position) return false;
    var def = m.default_position;
    var third = OF_ANGLE_THIRDS[def];
    if (!third) return false;
    var clamped = clamp(flight.angle, third[0], third[1]);
    applyAngleOverride(flight, clamped, hand, flight.archetype === "home_run");
    flight.fielder = def;
    return true;
  }

  // ── Ball flight rendering (ball-flight-plan.md Stage 4) ───────────────────
  // Timing constants below are animation-feel judgment calls, not derived
  // from anything physical - flagged as tune-after-watching in the plan
  // (Open Questions 2 and 6; physics-redesign plan OQ-3).
  // Physics seconds -> animation ms, one knob (physics-redesign plan Part 7):
  // flight/ground times are now real (fly balls hang 4-6.6s, grounders reach
  // the fielder in ~0.8-1.9s), but the rest of the choreography (runner leg
  // times, throw beats) still runs in stylized animation time - this is the
  // one place physics time gets compressed into it. 5.35s HR hang * 0.22 =
  // 1177ms (was pinned at 1400); a GO (0.11s flight + ~1.3s ground) * 0.22 =
  // ~310ms (was a flat 450ms) - both land in the old feel range.
  var ANIM_TIME_SCALE = 0.22;                          // Open Question 3
  var HANG_MS_MIN = 450, HANG_MS_MAX = 1400;           // Open Question 6
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
  // One id per rendered throw line's reveal clip-path (throwLineHtml) - just
  // needs to be unique within the DOM at any moment, not stable/meaningful.
  var THROW_CLIP_SEQ = 0;
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

  // How long the ball is airborne, in animation ms - physics hang time
  // scaled by ANIM_TIME_SCALE and clamped to the old feel range. No more
  // isGrounder branch (F7 fixed): a grounder's own hangS is tiny (~0.1-0.8s),
  // so this clamps to HANG_MS_MIN for essentially every grounder anyway -
  // the same 450ms floor the old flat GROUNDER_ROLL_MS constant gave, just
  // arrived at from the real physics instead of a second hardcoded number
  // that happened to agree with it.
  function ballTravelMs(flight) {
    if (!flight) return 0;
    return clamp((flight.hangMs || 0) * ANIM_TIME_SCALE, HANG_MS_MIN, HANG_MS_MAX);
  }

  // A much smaller scale than ANIM_TIME_SCALE, deliberately (Open Question
  // 3): ballTravelMs already floors every grounder at HANG_MS_MIN (450ms) -
  // real grounder hang time is under a second, so ANIM_TIME_SCALE never
  // lifts it off that floor - which leaves only ~60ms of the stylized
  // runner-to-first budget (RUN_LEG_MS/THROW_LEAD_MS, both preserved exactly
  // per Part 5) for the ground-phase delay before a deep SS/2B grounder's
  // throw would stop beating the runner, and a two-throw DP relay
  // (THROW_STAGGER_MS on top of that) leaves less than 10ms of that budget
  // once THROW_DELAY/DRAW/LEAD are accounted for. Applying ANIM_TIME_SCALE
  // itself to ground time (as a single shared knob) can't satisfy both this
  // and a readable fly-ball hang time at once - tuned here to the small
  // starting value that keeps the worst realistic case (a full-depth SS/2B
  // grounder feeding a two-throw relay, ~1.4s of ground time) inside that
  // budget with a few ms to spare. Genuinely tight by construction, not a
  // margin of comfort - Stage E's real task here is deciding whether the
  // ground-phase visual (currently near-instant at this scale) needs a
  // bigger budget carved out elsewhere (THROW_STAGGER_MS, THROW_LEAD_MS) or
  // stays this subtle; re-tune together with ANIM_TIME_SCALE against the
  // full timing-race sweep, not in isolation.
  var GROUND_TIME_SCALE = 0.005;

  // Ball-in-flight time plus the additional ground time until the fielder
  // actually has it (physics-redesign plan Part 7) - the throw can't be
  // drawn until this moment, not just when the ball first touches down.
  // flight.groundTimeS is set by resolveGrounderInterception/resolveHitPickup;
  // 0/absent for a ball caught in the air or one with no ground phase at all.
  function fieldedMs(flight) {
    return ballTravelMs(flight) + (flight && flight.groundTimeS ? flight.groundTimeS * 1000 * GROUND_TIME_SCALE : 0);
  }

  // The infield dirt's edge, in feet from home, along a given HZ angle - the
  // far intersection of the ray from home with the dirt circle (centred
  // PITCHER_MOUND_FT out, radius INFIELD_SKIN_DIRT_R_FT - the same circle
  // infieldSkinHtml actually draws). Same law-of-cosines form infieldSkinHtml
  // uses for the foul-line intersections, generalised to an arbitrary angle
  // instead of just the two 45 degrees-off-center foul lines.
  //
  // Used to read INFIELD_DIRT_RADIUS_FT, a separate "real-park" 95ft
  // constant kept deliberately apart from the drawn dirt's own (hand-tuned,
  // eventually 115ft) radius - Alex's report: a "Single" (not an infield
  // single) resting right at the visible edge of the dirt patch instead of
  // clearly past it. The two radii had drifted apart - this floor was still
  // only guaranteeing 95+3ft of clearance while the actual drawn patch had
  // grown to 115ft, so anything landing in that 95-115ft gap read as still
  // sitting on the dirt. Reading the same radius the dirt is drawn with
  // closes that gap; there's no longer a second "real" radius to keep in
  // sync with it by hand.
  var DIRT_CLEAR_MARGIN_FT = 3;   // a safe grounder rolls at least this far past the dirt's edge
  // These two archetypes are deliberately short (every bunt/infield_single
  // result's own depthMin/depthMax in result_diff_bands.csv stays well under
  // INFIELD_SKIN_DIRT_R_FT) at LANDING - but nothing capped their post-
  // bounce ROLL the same way, so a harder-EV roll could still carry one out
  // past the dirt (see resolveHitPickup's else branch below).
  var STAYS_IN_INFIELD_ARCHETYPES = { bunt: 1, infield_single: 1 };
  function dirtEdgeFt(angleDeg) {
    var offset = (angleDeg - 45) * Math.PI / 180;
    var m = PITCHER_MOUND_FT, r = INFIELD_SKIN_DIRT_R_FT;
    var s = Math.sin(offset);
    return m * Math.cos(offset) + Math.sqrt(Math.max(0, r * r - m * m * s * s));
  }

  /* Hits that stay in the park (physics-redesign plan Part 4.6): the ball
     needs a visible end point (labels, rollout, plausibility) but no out
     choreography. Runs groundPath from the real landing contact velocity;
     picked up at the first crossing of the assigned outfielder's radial
     depth (outfielder by angle-third of the HZ angle), if the roll segment
     reaches that far; otherwise at rest. Infield-archetype hits (bunt,
     infield_single) skip the OF rule and just use rest - they die on the
     dirt by construction of their EV range, now physically instead of by a
     hand-tuned rollout constant. The dirt-clearance floor is kept as a
     runtime floor on the pickup point for non-infield hits (still cheap
     insurance against a squibber that technically never left the dirt
     circle). Always capped at fenceAt(angle)-2. Sets flight.fieldedDistFt/
     groundTimeS the same way the grounder-out resolver does, so every
     consumer (labels, throwHtml on the rare hit-then-throw case) reads one
     shape regardless of out vs. hit. */
  // How far along the ball's real ground-contact direction (not necessarily
  // the nominal HZ angle - sidespin drift can point it elsewhere) a point
  // stays within boundaryRFt's own shape, home-centred - the roll-phase
  // counterpart to distanceCap's landing-point clamp. maxReachFt below is a
  // flat fenceAt(angle)-2 ceiling, fine while the field was a uniform
  // 375ft circle, but the boundary now tapers down to 35-80ft in foul
  // ground, and the ROLL direction can carry a ball fielded near the line
  // (or one that drifted foul before a caught-in-air exemption stopped
  // applying) out past that tighter edge with room to spare under the old
  // flat cap - Alex's report: a single's rollout crossing the warning
  // track into what should be out-of-bounds. Binary search rather than a
  // closed form since boundaryRFt is a general piecewise table, and the
  // roll direction generally isn't radial from home so the bearing at the
  // capped point isn't known in advance.
  function capRollToBoundary(flight, pickupFt) {
    function withinBounds(t) {
      var p = groundDirPoint(flight, t);
      var offsetDeg = Math.atan2(p.x, p.y) * 180 / Math.PI;
      var maxR = boundaryRFt(offsetDeg) - FIELD_BOUNDARY_MARGIN_FT;
      return Math.hypot(p.x, p.y) <= maxR;
    }
    if (pickupFt <= 0 || withinBounds(pickupFt)) return pickupFt;
    var lo = 0, hi = pickupFt; // lo=0 (the landing point itself) is always in bounds - distanceCap already saw to that
    for (var i = 0; i < 30; i++) {
      var mid = (lo + hi) / 2;
      if (withinBounds(mid)) lo = mid; else hi = mid;
    }
    return lo;
  }

  function resolveHitPickup(flight) {
    var gp = KMTraj.groundPath(Math.hypot(flight.contactVel.vx, flight.contactVel.vy), flight.contactVel.vz);
    var maxReachFt = fenceAt(flight.angle) - 2;
    var pickupFt = gp.restFt, groundTimeS = gp.totalS;

    if (!STAYS_IN_INFIELD_ARCHETYPES[flight.archetype]) {
      var ofPos = flight.angle < 33 ? "LF" : (flight.angle <= 57 ? "CF" : "RF");
      var ofAnchor = FIELDER_ANCHORS_FT[ofPos];
      var ofDepth = Math.hypot(ofAnchor.x, ofAnchor.y);
      var alongFt = ofDepth - flight.distance;
      if (alongFt <= 0) {
        pickupFt = 0; groundTimeS = 0;
      } else if (alongFt <= gp.restFt) {
        pickupFt = alongFt; groundTimeS = gp.timeAt(alongFt);
      }
      // else: dies before reaching the outfielder's depth - stays at rest
      // (gp.restFt/gp.totalS, already the default above).
      var need = dirtEdgeFt(flight.angle) + DIRT_CLEAR_MARGIN_FT - flight.distance;
      if (need > pickupFt) { pickupFt = need; groundTimeS = gp.timeAt(need) != null ? gp.timeAt(need) : gp.totalS; }
    } else {
      // The opposite floor: a bunt/infield_single is supposed to die ON the
      // dirt, but nothing was actually stopping its natural friction-based
      // roll (gp.restFt) from carrying it well past the dirt's edge if the
      // contact velocity happened to be on the harder end of that
      // archetype's real EV range - Alex's report: an "Infield Single"
      // rolling out into the grass. Ceiling, not floor: the ONLY constraint
      // is that it doesn't roll past dirtEdgeFt - the same mound-centred
      // circle the floor case above clears, just enforced the other
      // direction, and with no added margin/buffer past that one rule
      // (Alex's correction, twice over: neither the grass notch cut around
      // each base for the wedge rendering, nor an arbitrary "stop a bit
      // early" cushion, are part of what "stays in the infield" means. A
      // roll that falls short of the edge on its own - a soft one-hopper -
      // is left exactly where its own physics put it).
      var ceiling = dirtEdgeFt(flight.angle) - flight.distance;
      if (ceiling < pickupFt) {
        pickupFt = Math.max(0, ceiling);
        groundTimeS = gp.timeAt(pickupFt) != null ? gp.timeAt(pickupFt) : groundTimeS;
      }
    }

    pickupFt = Math.max(0, Math.min(pickupFt, maxReachFt - flight.distance));
    pickupFt = capRollToBoundary(flight, pickupFt);
    if (gp.timeAt(pickupFt) != null) groundTimeS = gp.timeAt(pickupFt);
    flight.fieldedDistFt = flight.distance + pickupFt;
    flight.groundTimeS = groundTimeS;
    flight.groundPath = gp;
    // Alex's report: whoever's credited with fielding a HIT should be
    // whoever's actually closest to where the ball comes to REST after its
    // rollout, not the flightParams-time guess off the raw landing point
    // (before any roll ever happened) - the two can name a completely
    // different fielder once a roll carries the ball well past its landing
    // spot (a single that lands in front of an OF but rolls into the
    // corner, say). Every consumer of flight.fielder for a non-out ball
    // (involvedPositions/"Fielded by", fielderTokensHtml's convergence
    // point) reads this same corrected value.
    var restPt = groundDirPoint(flight, pickupFt);
    flight.fielder = nearestFielder(restPt.x, restPt.y);
  }

  // A physically low trajectory reads as "ground" for CSS purposes - the old
  // isGrounder flag (a raw LA<4 threshold, independently computed off the
  // pitch/swing wheel) is gone (F7); apexFt is a real output of the
  // integrator, so this can never disagree with the ball's own flight.
  var GROUND_APEX_THRESHOLD_FT = 8;

  // Point along the ground-contact direction, `fieldedDistFt` beyond the
  // landing point (flight.distance) - or the landing point itself when no
  // ground phase was resolved (caught in the air).
  function fieldedPoint(flight) {
    if (flight.fieldedDistFt == null) return { x: flight.x, y: flight.y };
    return groundDirPoint(flight, flight.fieldedDistFt - flight.distance);
  }

  // An over-the-fence home run clears and fades near the wall (Part 6.3 item
  // 6) rather than animating on to its full, often far-off-canvas true
  // landing point - cut the sample list at the first point that crosses
  // fence+15ft.
  function fenceTruncatedSamples(samples) {
    var maxD = FENCE_DEPTH_FT + 15;
    for (var i = 0; i < samples.length; i++) {
      if (Math.hypot(samples[i].x, samples[i].y) >= maxD) return samples.slice(0, i + 1);
    }
    return samples;
  }

  // Ground-phase samples (Part 6.3 item 5: hops flatten into decaying bounces
  // then a straight roll, joining the same keyframe timeline as the flight)
  // - sampled at even time steps from flight.groundPath (Part 3), along the
  // ball's real ground-contact direction (Part 1), not the HZ launch bearing.
  var GROUND_SAMPLE_STEPS = 10;
  function groundPhaseSamples(flight) {
    var gp = flight.groundPath;
    if (!gp || flight.groundTimeS == null) return [];
    var vx = flight.contactVel.vx, vy = flight.contactVel.vy;
    var sh = Math.hypot(vx, vy) || 1;
    var ux = vx / sh, uy = vy / sh;
    // The last sample is the ball's rendered rest point, and throwHtml's
    // throw origin is fieldedPoint(flight) projected through ftToSvg, which
    // always assumes z=0 - a thrown ball is picked up off the ground, not
    // mid-bounce. Routing this last sample through gp.distAt(gp.timeAt(
    // fieldedFt))/gp.heightAt(t) instead of using fieldedFt and z=0 directly
    // let the two drift apart (both in ground position AND in the z that
    // projectFt folds into screen x/y) whenever that time/distance round
    // trip didn't land back exactly on its own input, or landed a hair into
    // a residual bounce (Alex's report: the throw visibly starting short of/
    // beyond where the ball actually stopped). Snapping this one sample to
    // the same fieldedFt + z=0 fieldedPoint/ftToSvg use keeps them pinned
    // together exactly.
    var fieldedFt = flight.fieldedDistFt != null ? flight.fieldedDistFt - flight.distance : null;
    var pts = [];
    for (var i = 1; i <= GROUND_SAMPLE_STEPS; i++) {
      var isLast = i === GROUND_SAMPLE_STEPS && fieldedFt != null;
      var t = flight.groundTimeS * i / GROUND_SAMPLE_STEPS;
      var d = isLast ? fieldedFt : gp.distAt(t);
      var z = isLast ? 0 : gp.heightAt(t);
      pts.push({ t: t, x: flight.x + ux * d, y: flight.y + uy * d, z: z });
    }
    return pts;
  }

  // Every sample from contact to the ball's final resting/fielded/fade
  // point, field-plane feet and real seconds since contact - one continuous
  // timeline, not a separate flight + rollout (Part 6.3 item 5). The last
  // air sample is always the exact interpolated landing point (KMTraj's own
  // contract); ground samples continue from there.
  function flightSampleSeries(flight) {
    var air = flight.clearedFence ? fenceTruncatedSamples(flight.samples) : flight.samples;
    var hangS = flight.hangS;
    if (flight.clearedFence || flight.fieldedDistFt == null) return { samples: air, totalS: hangS };
    var ground = groundPhaseSamples(flight).map(function (s) {
      return { t: hangS + s.t, x: s.x, y: s.y, z: s.z };
    });
    return { samples: air.concat(ground), totalS: hangS + (flight.groundTimeS || 0) };
  }

  var kmArcCounter = 0;
  function kmArcId(m) {
    var raw = m.moment_id != null ? m.moment_id : "n" + (kmArcCounter++);
    return "kmArc-" + String(raw).replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  /* Per-play generated CSS keyframes (physics-redesign plan Part 6.3): one
     stop per real sample, offset = t/totalS, so the ball follows its true
     speed profile (fast off the bat, hanging at apex, decelerating through
     the roll) under plain linear interpolation between stops - no easing
     curve to keep in sync with the physics. Returns the <style> block, the
     trail's polyline `d` + pixel length, and the final projected point
     (the base .ball rule's --tx/--ty fallback for when animations are off).

     The trail gets its OWN time-correct keyframes too (a second @keyframes
     stepping stroke-dashoffset down by each sample's real cumulative arc
     length, at the same t/totalS offsets as the ball) - style.css's generic
     trailDraw (a flat ease-out over the whole path length) doesn't know
     about the physical hang time near a fly ball's apex, so the dashed line
     used to visibly outrun the ball there, reaching the ground before the
     ball token did (most obvious on a high PO/FO arc). Same offsets, same
     linear timing-function as the ball's own keyframes, so the two can never
     drift apart. */
  function ballArcHtml(m, flight) {
    var series = flightSampleSeries(flight);
    var totalS = series.totalS > 0 ? series.totalS : 1e-6;
    var projected = series.samples.map(function (s) { return projectFt(s.x, s.y, s.z); });
    var cumLen = [0];
    for (var i = 1; i < projected.length; i++) {
      cumLen.push(cumLen[i - 1] + Math.hypot(projected[i].x - projected[i - 1].x, projected[i].y - projected[i - 1].y));
    }
    var len = cumLen[cumLen.length - 1] || 1;
    var stops = "", trailStops = "";
    var lastOff = 0;
    series.samples.forEach(function (s, i) {
      lastOff = clamp(s.t / totalS, 0, 1) * 100;
      stops += lastOff.toFixed(3) + "% { transform: translate(" + projected[i].x.toFixed(1) + "px," + projected[i].y.toFixed(1) + "px); } ";
      trailStops += lastOff.toFixed(3) + "% { stroke-dashoffset: " + (len - cumLen[i]).toFixed(1) + "px; } ";
    });
    // A cleared-fence HR's samples are cut short at the wall (fenceTruncated
    // Samples), well before totalS (still the real, un-truncated hang time) -
    // so the last real stop lands under 100%, leaving the 100% keyframe to
    // an implicit hold of that last value. Chromium was observed dropping
    // that implicit hold partway through (both this trail and the ball's own
    // position animation snapping back to their 0% start and sticking there)
    // - an explicit 100% stop matching the last real value sidesteps whatever
    // that implicit-extension edge case is, rather than relying on it.
    if (lastOff < 100 && projected.length) {
      var lastP = projected[projected.length - 1];
      stops += "100.000% { transform: translate(" + lastP.x.toFixed(1) + "px," + lastP.y.toFixed(1) + "px); } ";
      trailStops += "100.000% { stroke-dashoffset: 0px; } ";
    }
    var name = kmArcId(m);
    // Mirrors style.css's own .ball.air/.ball.clear composite rule exactly
    // (ballSettle untouched there), just swapping the primary movement
    // animation for this play's own generated keyframes - equal
    // specificity, later in the cascade (this <style> block is appended
    // fresh into the DOM after style.css loads), so it wins outright. The
    // reduced-motion block adds `animation:none!important` to outrank this.
    // A cleared-fence HR used to fade the ball to fully invisible here
    // (ballClearFade) instead of settling like every other result - Alex's
    // report: the arc should stay on screen after the play resolves same
    // as any other result, not vanish just because it left the park.
    var settleRule = "animation: " + name + " var(--dur) linear forwards, ballSettle 350ms ease var(--dur) forwards;";
    var style = "<style>" +
      "@keyframes " + name + " { " + stops + "} .ball." + name + " { " + settleRule + " }" +
      "@keyframes " + name + "-trail { " + trailStops + "} " +
      ".ball-trail." + name + " { animation: " + name + "-trail var(--dur) linear forwards; }" +
      "</style>";
    var pathD = projected.map(function (p, i) { return (i === 0 ? "M" : "L") + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ");
    return { style: style, name: name, pathD: pathD, len: len, endPt: projected[projected.length - 1] };
  }

  // True when fielderNameLabelsHtml (below) will stack the same short/
  // notation text over the original fielder's name - i.e. a name actually
  // resolved for them (m.defense). ballResultLabelHtml's own landing-point
  // label checks this so the identical string doesn't render twice on screen
  // (once at the ball's own landing/fielded point, once stacked over the
  // name) - when it's true, the fielder label already carries that text and
  // this one is redundant. False whenever the fielder label can't show a
  // name (Decision 4's partial-data case), so the notation isn't lost
  // entirely. Only ever true on an out now (Alex's call, above) -
  // fieldingChainDetail is null on every hit, so a hit's own result label
  // always renders instead of getting suppressed here.
  function fielderLabelHasResult(m, flight) {
    if (!flight || flight.clearedFence || !flight.fielder) return false;
    var detail = fieldingChainDetail(m, flight);
    if (!detail) return false;
    return !!(m.defense && m.defense[detail[0].pos]);
  }

  // Same anchor-offset bump as the fielder-name labels above (Alex's
  // report: these were landing right on the throw/fielding convergence
  // point too) - a bit smaller since this one only ever stacks up to two
  // short lines, not a name plus a result.
  var BALL_LABEL_DX = 9;
  var BALL_LABEL_DY = -11;
  var BALL_DIST_DY = 17;

  function ballFlightHtml(m, flight) {
    if (!flight) return "";
    var cleared = flight.clearedFence;
    var dur = ballTravelMs(flight);
    var arc = ballArcHtml(m, flight);
    var moveVars = "--tx:" + arc.endPt.x.toFixed(1) + "px;--ty:" + arc.endPt.y.toFixed(1) + "px;--dur:" + dur + "ms";
    var trailVars = "--len:" + arc.len.toFixed(1) + "px;--dur:" + dur + "ms";
    // C1: red for an out, green for a hit - a play can be both (a sac fly),
    // and the ball itself having been caught wins that tie. Except a ground
    // ball out specifically (Alex's call): that verdict now belongs to the
    // throw itself (throw-out/throw-safe, throwHtml), which carries the
    // real "who's out" answer per leg - the grounder's own trail just goes
    // neutral grey so it doesn't pre-empt or clash with that. Keyed off the
    // real archetype (GROUND_ARCHETYPES), not the apex-based " ground"/" air"
    // class below (a purely visual low-arc-vs-high-arc split that a low
    // line drive can also land in).
    var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
    var groundedOut = wasOut && !!GROUND_ARCHETYPES[flight.archetype];
    var cls = (cleared ? " clear" : " land") + (flight.apexFt < GROUND_APEX_THRESHOLD_FT ? " ground" : " air") +
              (wasOut ? " out" : " hit") + (groundedOut ? " grounded-out" : "");
    return arc.style +
      '<path class="ball-trail' + cls + " " + arc.name + '" d="' + arc.pathD + '" style="' + trailVars + '"></path>' +
      '<circle class="ball' + cls + " " + arc.name + '" r="' + BALL_R + '" style="' + moveVars + '"></circle>';
  }

  // Split out of ballFlightHtml (Alex's report) so the render order in
  // sceneFieldHtml can layer this - and fielderNameLabelsHtml - ON TOP of
  // the throw lines instead of underneath them; both label sets converge
  // on the same points the throw lines draw to/from, so drawn first they
  // used to sit under the throw's dashed stroke.
  function ballResultLabelHtml(m, flight) {
    if (!flight) return "";
    var cleared = flight.clearedFence;
    var dur = ballTravelMs(flight);
    // Labels sit next to wherever the ball actually ends up - the fielded/
    // rest point when there is a ground phase, otherwise the landing/catch
    // point itself - and only pop in once the ball has actually arrived
    // there, not mid-flight. A cleared HR's true distance is often well
    // off-canvas, so its anchor is capped at the same fence+15 fade point
    // the ball itself stops at, not the real number.
    // The scorecard fielding sequence (4-6-3, F8, 3U, ...) when there is
    // one - a specific description of how THIS out was made, not just the
    // general "Groundout"/"Double Play" category the result pill already
    // says - falling back to the plain short code for anything fieldingNotation
    // doesn't cover (clean hits, K/BB, an error-free non-putout play).
    var short = fieldingNotation(m, flight) || (data.meta.result_short || {})[m.result] || m.result;
    var hasGroundPhase = !cleared && flight.fieldedDistFt != null && flight.fieldedDistFt > flight.distance;
    // A cleared HR's label used to anchor at landingPoint(FENCE_DEPTH_FT+15,
    // flight.angle) - a geometrically "clean" point at exactly 390ft along
    // the play's nominal HZ angle - but that's not actually where the ball
    // stops on screen (Alex's report). Two things pull them apart: (1) the
    // ball's own trail is cut by fenceTruncatedSamples at the first REAL
    // sample crossing 390ft, which can land noticeably past 390 depending on
    // sample spacing, and off the nominal-angle bearing entirely once
    // sidespin drift (clampFairTerritory) has bent the true flight path
    // away from it; (2) that trimmed sample is still airborne (nonzero z) at
    // fence depth, and ftToSvg flattens z=0, while the ball marker itself
    // projects through the real height (ballArcHtml's projectFt) - so even
    // an identical (x,y) would land in a different screen spot. Reusing the
    // exact same fenceTruncatedSamples(...) endpoint, projected the same way
    // (projectFt with its real z), pins the label to wherever the ball
    // marker itself actually stops - not a separate idealized guess. The
    // distance label below inherits this same anchor, so fixing this one
    // point fixes both.
    var labelSvg;
    if (cleared) {
      var truncated = fenceTruncatedSamples(flight.samples);
      var endSample = truncated[truncated.length - 1];
      labelSvg = projectFt(endSample.x, endSample.y, endSample.z);
    } else {
      var labelPtFt = hasGroundPhase ? fieldedPoint(flight) : { x: flight.x, y: flight.y };
      labelSvg = ftToSvg(labelPtFt.x, labelPtFt.y);
    }
    var labelDelay = hasGroundPhase ? fieldedMs(flight) : dur;
    // C2: a short abbreviation next to wherever the ball ended up - skipped
    // when the fielder-name label is about to show this exact same text
    // stacked over the fielder's own name (fielderLabelHasResult) - one
    // label, not two saying the same thing. On a HIT this is always the
    // rollout rest point (fieldedPoint, via resolveHitPickup) - never a
    // fielder's own fixed anchor - since fielderNameLabelsHtml no longer
    // labels hits at all (Alex's call, above).
    var label = fielderLabelHasResult(m, flight) ? "" :
      '<text class="ball-label" x="' + labelSvg.x.toFixed(1) + '" y="' + labelSvg.y.toFixed(1) +
        '" dx="' + BALL_LABEL_DX + '" dy="' + BALL_LABEL_DY + '" style="--delay:' + labelDelay + 'ms">' + escapeHtml(short) + "</text>";
    // C3: distance next to every home run's landing point, cleared-the-fence
    // ones included (the true number, even though the marker itself stops
    // at the wall for those) - stacked below the result label at the same
    // anchor, not on top of it.
    var distLabel = m.result === "HR" ?
      '<text class="ball-dist" x="' + labelSvg.x.toFixed(1) + '" y="' + labelSvg.y.toFixed(1) +
        '" dx="' + BALL_LABEL_DX + '" dy="' + BALL_DIST_DY + '" style="--delay:' + labelDelay + 'ms">' + Math.round(flight.distance) + " ft</text>" : "";
    return label + distLabel;
  }

  function fielderTokensHtml(flight) {
    // No fielder converge on a ball that left the park - that's the visible
    // difference between an over-the-fence and an inside-the-park home run.
    if (!flight || flight.clearedFence) return "";
    var anchor = FIELDER_ANCHORS_FT[flight.fielder];
    if (!anchor) return "";
    var from = ftToSvg(anchor.x, anchor.y);
    var fieldedFt = fieldedPoint(flight);
    var to = ftToSvg(fieldedFt.x, fieldedFt.y);
    var vars = "--fx:" + from.x.toFixed(1) + "px;--fy:" + from.y.toFixed(1) + "px;" +
               "--tx:" + to.x.toFixed(1) + "px;--ty:" + to.y.toFixed(1) + "px;" +
               "--delay:" + fieldedMs(flight) + "ms";
    return '<g class="fielder" style="' + vars + '"><circle r="' + FIELDER_R + '"></circle></g>';
  }

  // Field name labels (Decisions 5-6): only the fielder(s) involved in THIS
  // play get a name label - not all 9 FIELDER_ANCHORS_FT positions every
  // slide, which would risk real overlap among the close infield anchors.
  // Pop in on the same timing as the ball label.
  //
  // Outs only (Alex's call): a name label anchored at a fielder's fixed
  // depth spot reads fine for an out (there's really only one meaningful
  // point - where they made the play), but a HIT already gets its own
  // result label planted at the true rollout rest point (ballResultLabelHtml)
  // - a second, fielder-anchored label for the same ball just duplicates
  // that near a fixed anchor that has nothing to do with where the ball
  // actually ended up, which is what produced results like "3B" floating on
  // the warning track for a ball that really rolled out to left-center.
  // fieldingChainDetail itself is already out-only (returns null whenever
  // outs_after<=outs_before), so gating on `detail` alone is sufficient.
  //
  // Two label shapes for that out case, by role (Alex's refinement on
  // Decision 5):
  //   - The ORIGINAL fielder (the one who actually touched the ball where
  //     they were standing - fieldingChainDetail's base===null entry) labels
  //     stacked two lines: the fielding result on top, their name below -
  //     the same short/notation text the ball's own landing-point label
  //     already shows, so a name near the fielded point (very common on a
  //     routine grounder or comebacker) reads as one unit instead of two
  //     overlapping labels. Anchor point depends on how they got the out:
  //     a caught fly/pop/line drive labels at the actual catch point (where
  //     the ball really was, same point ballFlightHtml's own label uses for
  //     an air catch) - a ground ball out still labels at the fielder's own
  //     fixed depth anchor, since "where it was fielded" is much less
  //     precise for a rolling grounder.
  //   - A RECEIVING fielder (every later chain entry - someone taking a
  //     relay throw) labels at the BASE they're covering, not their nominal
  //     fielding position - a 2B fielder taking a throw at the bag is
  //     standing on second, not out at their normal depth.
  var MIN_LABEL_GAP_PX = 34;  // tune-by-eye, Stage 5d
  // How far labels get pushed off their anchor point (Alex's report: names
  // and result labels were landing right on top of the throw lines that
  // converge on the very same fielding/base/receiving point) - bumped up
  // from the original flat -6px single-line offset; the stacked two-line
  // label's own first tspan gets a bigger nudge since the name tspan below
  // it sits even further from the anchor.
  var LABEL_ANCHOR_OFFSET_PX = 14;
  var LABEL_ANCHOR_OFFSET_STACKED_PX = 19;

  function fielderNameLabelsHtml(m, flight) {
    var defense = m.defense || {};
    var detail = fieldingChainDetail(m, flight);
    var entries;
    if (detail) {
      // A chain can revisit a position non-adjacently (3-6-3) - one label
      // per person, keeping their FIRST touch (their real role: original
      // fielder beats a later return throw to the same guy).
      var seen = {};
      entries = [];
      detail.forEach(function (e) {
        if (seen[e.pos]) return;
        seen[e.pos] = 1;
        entries.push(e);
      });
    } else {
      entries = [];  // over-the-fence HR; K/BB/steals and everything else with no flight
    }
    if (!entries.length) return "";

    var hasGroundPhase = !!flight && !flight.clearedFence &&
      flight.fieldedDistFt != null && flight.fieldedDistFt > flight.distance;
    var delay = hasGroundPhase ? fieldedMs(flight) : ballTravelMs(flight);
    // Same short/notation text ballFlightHtml's own landing-point label
    // computes - deliberately identical value, not just similar wording.
    var resultShort = fieldingNotation(m, flight) || (data.meta.result_short || {})[m.result] || m.result;
    // A caught fly/pop/line out was resolved by a real chain (detail !=
    // null) - only there does flight.archetype describe a completed out,
    // never the non-out single-fielder fallback below.
    var isCaughtOut = !!(detail && CAUGHT_IN_AIR[flight.archetype]);

    var labels = [];
    entries.forEach(function (e) {
      var nameEntry = defense[e.pos];
      // Partial live-game data (Decision 4) - nothing renders for this
      // position, same as today's generic-anchor-only behaviour.
      if (!nameEntry) return;
      var pt;
      if (e.base === null) {
        if (isCaughtOut) {
          pt = ftToSvg(flight.x, flight.y);
        } else {
          var anchor = FIELDER_ANCHORS_FT[e.pos];
          if (!anchor) return;
          pt = ftToSvg(anchor.x, anchor.y);
        }
        labels.push({ x: pt.x, y: pt.y, lines: [resultShort, nameEntry[1]] });
      } else {
        pt = e.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[e.base];
        if (!pt) return;
        labels.push({ x: pt.x, y: pt.y, lines: [nameEntry[1]] });
      }
    });
    if (!labels.length) return "";

    // Proximity offset (Decision 6): a simple pairwise sweep, not a general
    // layout solver - the chain caps this at 1-3 labels, usually 1-2.
    // Distance is checked against each sweep's current (possibly
    // already-nudged) positions, but the push direction always comes from
    // the pair's ORIGINAL anchors so it can't flip mid-sweep if two labels
    // have already crossed.
    var origins = labels.map(function (l) { return { x: l.x, y: l.y }; });
    for (var sweep = 0; sweep < 2; sweep++) {
      for (var i = 0; i < labels.length; i++) {
        for (var j = i + 1; j < labels.length; j++) {
          var a = labels[i], b = labels[j];
          var dx = b.x - a.x, dy = b.y - a.y;
          var d = Math.sqrt(dx * dx + dy * dy);
          if (d > 0 && d < MIN_LABEL_GAP_PX) {
            var oa = origins[i], ob = origins[j];
            var odx = ob.x - oa.x, ody = ob.y - oa.y;
            var od = Math.sqrt(odx * odx + ody * ody) || 1;
            var ux = odx / od, uy = ody / od;
            var push = (MIN_LABEL_GAP_PX - d) / 2;
            a.x -= ux * push; a.y -= uy * push;
            b.x += ux * push; b.y += uy * push;
          }
        }
      }
    }

    return labels.map(function (l) {
      var x = l.x.toFixed(1), y = l.y.toFixed(1);
      if (l.lines.length === 1) {
        return '<text class="fielder-name" x="' + x + '" y="' + y +
          '" dy="-' + LABEL_ANCHOR_OFFSET_PX + '" style="--delay:' + delay + 'ms">' + escapeHtml(l.lines[0]) + "</text>";
      }
      // Stacked via explicit tspans - SVG <text> has no native multi-line
      // wrap. The result line is the lighter/smaller of the two (.fielder-result).
      return '<text class="fielder-name" x="' + x + '" y="' + y + '" style="--delay:' + delay + 'ms">' +
        '<tspan class="fielder-result" x="' + x + '" dy="-' + LABEL_ANCHOR_OFFSET_STACKED_PX + '">' + escapeHtml(l.lines[0]) + '</tspan>' +
        '<tspan x="' + x + '" dy="10">' + escapeHtml(l.lines[1]) + '</tspan>' +
      "</text>";
    }).join("");
  }

  // Archetypes caught in the air - no throw on a routine catch with nobody
  // on base to play behind (A1). Branch on archetype, not a physical
  // threshold - a low line drive can have the same apex as a grounder but
  // was never on the ground.
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
  // Same idea as STRIKEOUT_RESULTS' "K" (no ball flight to hang a label off,
  // so it labels the batter directly) - a walk still moves the batter to
  // first, but the "BB"/"IBB" itself happened at the plate, so the label
  // anchors there rather than following the batter down the basepath.
  var WALK_RESULTS = { BB: 1, IBB: 1 };
  // A balk has no batter involvement at all (it's in data.meta.flight.no_pa -
  // see the batterReached block below) - only the pitcher and baserunners.
  // Its "Balk" label anchors at the mound instead of home plate or a batter
  // token, since that's the one fixed point every balk is actually about.
  var BALK_RESULTS = { Balk: 1 };

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

  /* Traditional scorecard notation (fieldingNotation, below) needs to know
     which position covers a given base for a relay throw - not modeled
     anywhere else in the app, since throwHtml only ever needs a BASE to draw
     a line to, never who's standing there. This is a best-effort standard-
     alignment convention (real coverage depends on the actual defensive
     alignment that day, which this data model doesn't record), worked out
     with Alex directly:
       - HOME is always the catcher.
       - 3B is the third baseman, UNLESS he's the one who fielded a BUNT
         (charging in pulls him off the bag) - then SS covers.
       - 1B is the first baseman, UNLESS he's the one who fielded a BUNT (2B
         covers instead), or he fielded a grounder at the 77deg lattice angle
         specifically (the PFP play - hit deep enough into the 1B/2B hole
         that he can't get back to the bag himself, so the pitcher breaks
         over to cover it; the other 1B angle, 85deg, sits close enough to
         the bag/line that he beats the runner back there himself).
       - 2B is decided by which side of the infield the ball was hit to, NOT
         by who actually fielded it (so a slow roller fielded by the pitcher
         or a charging outfielder still gets a sensible coverer): angle<45
         (the 3B/SS side) -> 2B covers; angle>=45 (dead centre through the
         1B side) -> SS covers. The >=45 half deliberately folds in the
         45deg comebacker-to-the-pitcher tie case, not just the literal
         right side, per Alex's call.
     fieldingNotation collapses a fielder covering their own next base right
     back down to a single (unassisted) touch - see there for why that's the
     general unassisted rule rather than a separate angle check. */
  function coveringPosition(base, archetype, angle, fielderPos) {
    if (base === "HOME") return "C";
    if (base === "3B") return (archetype === "bunt" && fielderPos === "3B") ? "SS" : "3B";
    if (base === "1B") {
      if (archetype === "bunt" && fielderPos === "1B") return "2B";
      if (fielderPos === "1B" && angle === 77) return "P";
      return "1B";
    }
    if (base === "2B") return angle < 45 ? "2B" : "SS";
    return fielderPos;
  }

  // Standard scorecard shorthand for a batted-ball out - "6-4-3", "F8",
  // "3U" - built entirely from data the app already resolves per play
  // (flight.fielder, outThrowTargets' relay bases) plus the coverage
  // convention above. Scoped to batted-ball outs only (Alex's own framing:
  // "based on which defensive players touch the ball") - a strikeout has no
  // batted ball, a walk/steal/pickoff has no fielder touching a batted ball,
  // and this data model has no error/E-code concept to draw an "E6" from.
  // How many of outThrowTargets' bases represent a REAL out, vs a decorative,
  // non-competitive tag-up throw on a routine sac fly where the runner
  // scores safely (both outThrowTargets' own heuristic, and real
  // import_BRC.csv ThrowOrder data, carry that one for the animation - see
  // outThrowTargets' own comment on it). The catch itself already accounts
  // for one out on any caught-in-air play; everything else outThrowTargets
  // returns is a genuine extra out (a real assist chain, or LODP/LOTP's
  // double-off). Shared by fieldingNotation (so a plain sac fly reads "F9",
  // not "F9-2" - a putout at home that never happened) and throwHtml/
  // stealThrowHtml (so that same decorative throw draws safe/green instead
  // of out/red) - both need the identical answer to the same question:
  // which of these throws actually put someone out.
  function realOutThrowCount(m, flight) {
    var recorded = Math.max(0, (m.outs_after || 0) - (m.outs_before || 0));
    var battersOwnOut = CAUGHT_IN_AIR[flight.archetype] ? 1 : 0;
    return Math.max(0, recorded - battersOwnOut);
  }

  // The ordered, adjacent-duplicate-collapsed chain of { pos, base } touches
  // on this ball, or null when there's nothing to describe - no flight, no
  // assigned fielder, a cleared fence, an archetype that isn't
  // ground-or-air, or no new out on the play. `base` is null on the first
  // entry (the fielder who actually touched the ball where they were
  // standing - no throw involved) and the base string ("1B"/"2B"/"3B"/
  // "HOME") every later entry is COVERING to receive a relay throw - the
  // field-label placement (fielderNameLabelsHtml) needs that distinction to
  // anchor a receiving fielder at the bag rather than their nominal fielding
  // spot. fieldingChain (below) strips this down to bare position strings
  // for fieldingNotation/involvedPositions, which don't care.
  function fieldingChainDetail(m, flight) {
    if (!flight || !flight.fielder || flight.clearedFence) return null;
    var archetype = flight.archetype;
    var isAir = !!CAUGHT_IN_AIR[archetype];
    if (!GROUND_ARCHETYPES[archetype] && !isAir) return null;
    if ((m.outs_after || 0) <= (m.outs_before || 0)) return null;

    var moves = resolveRunnerMoves(m);
    var relayBases = outThrowTargets(m, moves, flight).slice(0, realOutThrowCount(m, flight));

    var chain = [{ pos: flight.fielder, base: null }];
    relayBases.forEach(function (base) {
      chain.push({ pos: coveringPosition(base, archetype, flight.angle, flight.fielder), base: base });
    });

    // Collapse adjacent duplicates: the same fielder touching the ball and
    // then covering the very next base themselves is one unassisted touch,
    // not a throw to himself. This is also where an unassisted play falls
    // out on its own - a routine 1B/3B putout, or an unassisted lineout
    // double play - purely from the coverage rules above, with no separate
    // "is this unassisted" check needed.
    var collapsed = [chain[0]];
    for (var i = 1; i < chain.length; i++) {
      if (chain[i].pos !== collapsed[collapsed.length - 1].pos) collapsed.push(chain[i]);
    }
    return collapsed;
  }

  // Plain position-string view of fieldingChainDetail (e.g. ["SS","2B","1B"]
  // for a 6-4-3) - what fieldingNotation/involvedPositions actually need,
  // neither of which cares which base a relay entry covers.
  function fieldingChain(m, flight) {
    var detail = fieldingChainDetail(m, flight);
    return detail && detail.map(function (e) { return e.pos; });
  }

  function fieldingNotation(m, flight) {
    var collapsed = fieldingChain(m, flight);
    if (!collapsed) return null;
    var archetype = flight.archetype;
    var isAir = !!CAUGHT_IN_AIR[archetype];

    var nums = collapsed.map(function (p) { return POSITION_NUMBER[p]; });
    if (nums.length === 1) {
      // A caught ball's own letter already says how the (one) out was made -
      // "L6" for an unassisted lineout double play reads the same as a
      // routine lineout catch, real scorecards have the same ambiguity here,
      // the "U" suffix is a ground-ball-only convention (nobody writes F8U).
      if (isAir) {
        var prefix = archetype === "fly_ball" ? "F" : archetype === "line_drive" ? "L" : "P";
        if (m.result === "SacF" || m.result === "DSacF") prefix = "S" + prefix;
        return prefix + nums[0];
      }
      return nums[0] + "U";
    }
    return nums.join("-");
  }

  // The one shared "who touched this ball" answer - Decisions 5 and 7 (field
  // name labels and the defense text line) both consume this so they always
  // agree with each other and with the fielder token's own convergence point.
  function involvedPositions(m, flight) {
    var chain = fieldingChain(m, flight);
    if (chain) {
      // A chain can revisit a position non-adjacently (3-6-3) - one label
      // per position, not one per touch.
      var seen = {};
      var unique = [];
      chain.forEach(function (p) {
        if (!seen[p]) { seen[p] = 1; unique.push(p); }
      });
      return unique;
    }
    // A ball in play that isn't an out (or any other single-fielder touch
    // fieldingChain doesn't cover) still has one assigned fielder - the same
    // one fielderTokensHtml's own token converges on, so the label and the
    // animation never disagree.
    if (flight && !flight.clearedFence && flight.fielder) return [flight.fielder];
    return [];  // over-the-fence HR; K/BB/steals and everything else with no flight
  }

  // fieldingNotation needs a resolved flight (fielder assigned, the same
  // GROUND_ARCHETYPES/CAUGHT_IN_AIR resolver dispatch playSceneHtml runs
  // before ever calling it) - but the Result search box (matches()/
  // renderResultCodeSuggest) needs an answer for every play in the current
  // pool, not just whichever one is on screen. Duplicates playSceneHtml's
  // small dispatch block rather than threading a "give me the resolved
  // flight" call through it, and memoises on the play object itself (data.
  // moments/playsBySession rows are stable, reused across renders) so
  // typing in the search box only pays this once per play ever touched, not
  // once per keystroke.
  function playFieldingNotation(m) {
    if (Object.prototype.hasOwnProperty.call(m, "_fieldingNotation")) return m._fieldingNotation;
    var flight = flightParams(m, data.meta.flight);
    if (flight) {
      var hand = effectiveHand(m.batter_hand);
      var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
      if (GROUND_ARCHETYPES[flight.archetype] && wasOut) {
        resolveGrounderInterception(m, flight, hand);
      } else if (wasOut && CAUGHT_IN_AIR[flight.archetype]) {
        applyAirPositionOverride(m, flight, hand);
      } else if (!flight.clearedFence) {
        resolveHitPickup(flight);
      }
    }
    var notation = fieldingNotation(m, flight);
    m._fieldingNotation = notation;
    return notation;
  }

  /* Pure schedule (A4/A5): throw i originates at the ball's landing point;
     throw i+1 relays from throw i's target base. Kept separate from the
     rendering so the timing race against the runner can be asserted rather
     than eyeballed - see ball_flight_test.py. */
  function throwSchedule(m, moves, flight) {
    var targets = outThrowTargets(m, moves, flight);
    if (!targets.length) return [];
    // Which legs are a real out vs a decorative tag-up throw nobody's out on
    // (realOutThrowCount) - drives throwHtml's out/safe (red/green) colour,
    // same "which of these throws actually put someone out" question
    // fieldingNotation asks of the identical target list.
    var realCount = realOutThrowCount(m, flight);

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
        return { base: b, startMs: start, endMs: start + THROW_DRAW_MS, out: i < realCount };
      });
    }

    // A rolling grounder isn't fielded until the resolver's own ground time
    // has actually elapsed - the throw has to wait out that extra beat too,
    // or it'd draw from a spot the ball hasn't visibly reached yet (see
    // throwHtml's origin). fieldedMs (Part 7) replaces the old
    // ballTravelMs+rollMs sum with one physically-timed number.
    var base = fieldedMs(flight) + THROW_DELAY_MS;
    return targets.map(function (b, i) {
      var start = base + i * THROW_STAGGER_MS;
      return { base: b, startMs: start, endMs: start + THROW_DRAW_MS, out: i < realCount };
    });
  }

  // The .out-to-first keyframe's 47.06% stop is where the batter reaches
  // first (A4) - kept as one named function so the throw-beats-runner
  // assertion has a single source of truth to check against.
  function batterFirstArrivalMs() {
    return RUNNER_LEAD_MS + 0.4706 * 1700;
  }

  // A dashed <line>'s own geometry attributes (x1/y1/x2/y2) aren't CSS-
  // animatable - unlike <rect>'s x/y/width/height, they never made it into
  // the CSS Masking/Geometry properties that got promoted off SVG's
  // presentation-attribute-only list. So a real "grows from the thrower to
  // the target" reveal (Alex's ask, replacing the old dashoffset shimmer -
  // see throwDraw in style.css) needs a second element to animate: a <rect>,
  // full stroke-covering height but zero width, sitting in its own
  // <clipPath> and rotated (a static, non-animated SVG `transform` -
  // computed once here, not fought over with CSS) to lie along the line's
  // own bearing. Animating just its width via CSS is what actually grows
  // the visible portion of the (separately, statically dashed) line
  // underneath.
  function throwLineHtml(x1, y1, x2, y2, cls, startMs) {
    var len = Math.hypot(x2 - x1, y2 - y1) || 1;
    var angleDeg = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
    var id = "throwClip" + (THROW_CLIP_SEQ++);
    var clipVars = "--len:" + len.toFixed(1) + "px;--delay:" + startMs + "ms;--draw:" + THROW_DRAW_MS + "ms";
    var clip = '<clipPath id="' + id + '" clipPathUnits="userSpaceOnUse">' +
      '<rect class="throw-clip-rect" x="' + x1.toFixed(1) + '" y="' + (y1 - 4).toFixed(1) +
      '" width="0" height="8" transform="rotate(' + angleDeg.toFixed(2) + ' ' + x1.toFixed(1) + ' ' + y1.toFixed(1) +
      ')" style="' + clipVars + '"></rect></clipPath>';
    var line = '<line class="' + cls + '" x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) +
      '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '" clip-path="url(#' + id + ')"></line>';
    return clip + line;
  }

  function throwHtml(m, flight, moves) {
    var schedule = throwSchedule(m, moves, flight);
    if (!schedule.length) return "";
    // A grounder is fielded wherever it stops rolling, not at its bounce
    // point - the throw has to originate there, or it visibly starts from
    // empty grass short of the fielder. fieldedPoint follows the ball's real
    // ground-contact direction (Part 1), not the HZ launch bearing.
    var origin = ftToSvg(fieldedPoint(flight).x, fieldedPoint(flight).y);
    return schedule.map(function (t) {
      var to = t.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[t.base];
      if (!to) return "";
      // Red for a throw that puts someone out, green for the rare safe/
      // decorative one (a tag-up run that scores anyway) - Alex's call, same
      // verdict-colour convention the ball itself and a steal attempt use.
      var cls = "throw-line " + (t.out ? "throw-out" : "throw-safe");
      var html = throwLineHtml(origin.x, origin.y, to.x, to.y, cls, t.startMs);
      origin = to;   // next throw relays from here (A5)
      return html;
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
  // throw. Same two columns the batted-ball resolver reads (brcExcludes
  // above), just checked against steals' fixed "the catcher starts with the
  // ball" baseline instead of a physics-computed fielder - there's no ball
  // flight on a steal to compute one from.
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
    var cls = "throw-line steal-throw " + (target.caught ? "throw-out" : "throw-safe");
    return throwLineHtml(from.x, from.y, to.x, to.y, cls, start);
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
    distanceCap: distanceCap, boundaryRFt: boundaryRFt,
    nearestFielder: nearestFielder, flightParams: flightParams, fenceAt: fenceAt,
    launchAngleFor: launchAngleFor,
    FENCE_DEPTH_FT: FENCE_DEPTH_FT,
    ordinal: ordinal, deriveRunnerMoves: deriveRunnerMoves,
    outThrowTargets: outThrowTargets, throwSchedule: throwSchedule,
    throwLineHtml: throwLineHtml,
    batterFirstArrivalMs: batterFirstArrivalMs,
    stealThrowTarget: stealThrowTarget, stealRunnerArrivalMs: stealRunnerArrivalMs,
    stealThrowOrigin: stealThrowOrigin,
    THROW_LEAD_MS: THROW_LEAD_MS, THROW_DELAY_MS: THROW_DELAY_MS,
    THROW_DRAW_MS: THROW_DRAW_MS, THROW_STAGGER_MS: THROW_STAGGER_MS,
    RUNNER_LEAD_MS: RUNNER_LEAD_MS,
    dirtEdgeFt: dirtEdgeFt, CAUGHT_IN_AIR: CAUGHT_IN_AIR,
    TAG_THROW_ARCHETYPES: TAG_THROW_ARCHETYPES,
    GROUND_ARCHETYPES: GROUND_ARCHETYPES,
    parseThrowOrder: parseThrowOrder,
    brcExcludes: brcExcludes,
    resolveGrounderInterception: resolveGrounderInterception,
    applyAirPositionOverride: applyAirPositionOverride,
    resolveHitPickup: resolveHitPickup,
    applyAngleOverride: applyAngleOverride, clampFairTerritory: clampFairTerritory,
    groundDirPoint: groundDirPoint, fieldedPoint: fieldedPoint,
    throwOrderKeyForPosition: throwOrderKeyForPosition,
    fieldingChain: fieldingChain, fieldingChainDetail: fieldingChainDetail, involvedPositions: involvedPositions,
    fielderLabelHasResult: fielderLabelHasResult, ballFlightHtml: ballFlightHtml,
    ballResultLabelHtml: ballResultLabelHtml,
    fielderNameLabelsHtml: fielderNameLabelsHtml, fieldingNotation: fieldingNotation,
    sceneDefenseLineHtml: sceneDefenseLineHtml, playSceneHtml: playSceneHtml,
    OF_POSITIONS: OF_POSITIONS, FIELDER_ANCHORS_FT: FIELDER_ANCHORS_FT,
    INFIELDER_DEPTH_FT: INFIELDER_DEPTH_FT, MIN_ANGLE_FOR_POS: MIN_ANGLE_FOR_POS,
    HZ_FIELDER_BY_ANGLE: HZ_FIELDER_BY_ANGLE,
    TAG_UP_MS: TAG_UP_MS, TAG_THROW_MARGIN_MS: TAG_THROW_MARGIN_MS,
    RUN_LEG_MS: RUN_LEG_MS, BASE_ORDINAL: BASE_ORDINAL,
    ballTravelMs: ballTravelMs, fieldedMs: fieldedMs,
    ANIM_TIME_SCALE: ANIM_TIME_SCALE, GROUND_TIME_SCALE: GROUND_TIME_SCALE,
    projectFt: projectFt, ftToSvg: ftToSvg, FIELD_W: FIELD_W, FIELD_H: FIELD_H,
    fencePathD: fencePathD, fenceWallPathD: fenceWallPathD, grassPathD: grassPathD,
    HOME_SVG: HOME_SVG,
    POSITION_NUMBER: POSITION_NUMBER, coveringPosition: coveringPosition,
    fieldingNotation: fieldingNotation, resolveRunnerMoves: resolveRunnerMoves,
    realOutThrowCount: realOutThrowCount,
  };

  /* Replaces the old tightly-cropped infield-only diamond. Same runner-token
     architecture (deriveRunnerMoves/basepathWaypoints/RUN_LEG_MS, untouched),
     staged on a bigger field canvas with the ball flight, a converging
     fielder and an out-choreography walk to the dugout layered underneath
     the runners (ball-flight-plan.md Stage 4/6). `flight` is
     flightParams(m, data.meta.flight) or null for an out-of-scope result. */
  // import_BRC.csv's B/r1/r2/r3 columns, decoded server-side into the exact
  // per-runner outcome for this situation (key_moments_build.py's
  // runner_moves) - trusted completely over the diff-based guess below
  // whenever it's present. deriveRunnerMoves only runs at all for situations
  // that haven't been given explicit data yet.
  //
  // Fallback for the one still-common gap: DPH1 always starts from bases
  // loaded and always removes the MOST advanced runner (3B, out at home),
  // not the least advanced one - the opposite of deriveRunnerMoves' most-
  // advanced-pairs-with-most-advanced assumption, which instead pairs
  // 3B->3B and 2B->2B as if neither runner moved and blames the 1B runner
  // for an out that never involved them ("DPH1's heuristic mismatch", see
  // outThrowTargets' forcedBase handling). Only applies when m.runner_moves
  // is missing (the inning-ending DPH1 variant's r1/r2/r3 aren't filled in
  // yet) - once that row is completed too, this becomes dead code on its
  // own, no flag to flip.
  //
  // Shared by every consumer that needs the real per-runner outcome
  // (sceneFieldHtml's tokens/throws, fieldingNotation's putout chain) so the
  // DPH1 special case can't drift out of sync between them.
  function resolveRunnerMoves(m) {
    var before = String(m.obc_before || "000");
    var after = String(m.obc_after || "000");
    if (m.runner_moves) return m.runner_moves;
    if (m.result === "DPH1" && before === "111") {
      return [
        { from: "3B", to: "OUT", scored: false },
        { from: "2B", to: "3B", scored: false },
        { from: "1B", to: "2B", scored: false },
      ];
    }
    return deriveRunnerMoves(before, after, m.runs || 0);
  }

  function sceneFieldHtml(m, flight) {
    var before = String(m.obc_before || "000");
    var after = String(m.obc_after || "000");
    var moves = resolveRunnerMoves(m);
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
      // Same HOME-wraparound fix as the isOut branch above, for the safe
      // side: a runner who's animated running for home without it counting
      // (import_BRC.csv's aN fallback on an inning-ending force play - see
      // utils._build_runner_moves_for_row) still needs the end=4 wraparound
      // or basepathWaypoints reads HOME's ordinal(0) <= their own and
      // renders no path at all. mv.scored (the CSS "score" flash below)
      // stays untouched - this runner didn't actually score.
      var path = useRetreat
        ? [{ x: (from.x + assistBase.x) / 2, y: (from.y + assistBase.y) / 2 }]
        : isOut
          ? (forcedBase ? basepathWaypoints(mv.from, forcedBase, forcedBase === "HOME") : [])
          : basepathWaypoints(mv.from, mv.to, mv.scored || mv.to === "HOME");
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
      // Except a strandedSafe move: import_BRC.csv's aN fallback on an
      // inning-ending row (utils._build_runner_moves_for_row) is explicitly
      // non-credited - "the final base doesn't matter, the frame resets" -
      // so when that row's own delay flag is false, there's no real tag-up
      // being described here, just a decorative advance shown for
      // continuity. That one starts on contact like any other safe runner,
      // not after the catch.
      //
      // A caught-stealing row (CS family, KCS included) is its own third
      // case: there's no ball flight at all here (runDelay would be 0, "the
      // instant the slide mounts"), but every runner on base still reacts to
      // the SAME throw down at the SAME moment - the runner who's thrown out
      // and any other runner just advancing/faux-advancing belong on one
      // shared beat, not one starting before the play has even happened and
      // the other waiting for it. stealOutDelay is exactly what the caught
      // runner's own token uses below (forcedBase's outDelay, or
      // delayedStartMs when the row's delay flag is true - already shared
      // via mv.delay, since delay/retreat are per-row, not per-runner) -
      // reusing it here is what actually keeps them in sync, not just close.
      var mvDelay = mv.delay
        ? delayedStartMs
        : isOut
          ? (forcedOnContact ? runDelay : (forcedBase ? outDelay : Math.max(outDelay, stealOutResolveMs)))
          : ((catchMs && !strandedSafe) ? catchMs + TAG_UP_MS
              : ((stealOut && stealOut.caught) ? stealOutDelay : runDelay));
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

    // BB/IBB (Alex's ask, same treatment as STRIKEOUT_RESULTS' "K" above):
    // the batter DOES reach first here, so this sits outside the
    // !batterReached block above - the label just anchors at home plate
    // (where the walk was actually drawn) rather than following the batter
    // token down the basepath. Same beat the batter token itself leaves on
    // (runDelay - 0 for any no-flight play, walks included).
    if (WALK_RESULTS[m.result]) {
      var bbHome = SCENE_BASES.HOME;
      var bbShort = (data.meta.result_short || {})[m.result] || m.result;
      tokens += '<text class="ball-label" x="' + bbHome.x + '" y="' + bbHome.y +
        '" dx="10" dy="-6" style="--delay:' + runDelay + 'ms">' + escapeHtml(bbShort) + "</text>";
    }

    // Balk (Alex's ask): no batter token at all here (Balk is in
    // data.meta.flight.no_pa, so the block above never runs) - labels the
    // mound instead, the one fixed point every balk is actually about. Same
    // beat every runner on base starts advancing (runDelay - 0, no flight).
    if (BALK_RESULTS[m.result]) {
      var bkMound = ftToSvg(0, PITCHER_MOUND_FT);
      var bkShort = (data.meta.result_short || {})[m.result] || m.result;
      tokens += '<text class="ball-label" x="' + bkMound.x + '" y="' + bkMound.y +
        '" dx="10" dy="-6" style="--delay:' + runDelay + 'ms">' + escapeHtml(bkShort) + "</text>";
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

    /* The batting team's mark is painted in center field (Alex's call - was
       the infield centroid, moved out to sit on open grass where it doesn't
       compete with the basepath dirt skin or the bases). Sized off the same
       projected-basepath-length as before so it stays perspective-correct
       (bigger when the camera reads the field bigger) without a flat
       ft/unit scale. */
    var batAbbr = m.batting_is_home ? m.home_team_abbr : m.away_team_abbr;
    var markUrl = teamLogoUrl(batAbbr);
    var centroid = ftToSvg(0, CF_MARK_DEPTH_FT);
    var markSize = Math.hypot(SCENE_BASES["1B"].x - SCENE_BASES.HOME.x, SCENE_BASES["1B"].y - SCENE_BASES.HOME.y) * 0.68;
    // The last out of a half-inning: rather than cutting straight from this
    // team's CF mark to the next team's on the following slide, the mark
    // itself fades to a "Mid Nth"/"End Nth" pill in place, on THIS slide -
    // everything else about the play (result, matchup, wheels) stays exactly
    // as it already renders (Alex's call - a separate, stripped-down slide
    // here lost all of that). The next slide's own mark then appears exactly
    // as it already did before this existed. Skipped on the game's actual
    // last play - that one's already carrying the FINAL recap banner, and
    // there's no next half to bridge to.
    var isHalfEnd = !!m.is_half_inning_final && !m.is_game_final;
    var watermark = markUrl
      ? '<image class="dm-mark' + (isHalfEnd ? " dm-mark-fading" : "") + '" href="' + escapeHtml(markUrl) +
        '" x="' + (centroid.x - markSize / 2).toFixed(1) + '" y="' + (centroid.y - markSize / 2).toFixed(1) +
        '" width="' + markSize.toFixed(1) + '" height="' + markSize.toFixed(1) +
        '" preserveAspectRatio="xMidYMid meet"></image>'
      : "";
    if (isHalfEnd) {
      watermark += cfBreakPillHtml((m.half === "top" ? "Mid " : "End ") + ordinal(m.inning));
    }
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
    // The pitcher's rubber (both reference photos show it) - a small flat
    // marker, screen-px sized same as the bases/plate rather than a
    // ft-scaled rectangle (Part 6.2: token sizes stay constant-px).
    var moundPt = ftToSvg(0, PITCHER_MOUND_FT);
    var rubberW = BASE_R * 2.1, rubberH = BASE_R * 0.65;
    var rubberMarker = '<rect class="dm-rubber" x="' + (moundPt.x - rubberW / 2).toFixed(1) +
      '" y="' + (moundPt.y - rubberH / 2).toFixed(1) + '" width="' + rubberW.toFixed(1) +
      '" height="' + rubberH.toFixed(1) + '" rx="0.8"></rect>';
    // Layering bottom to top, per Stage 4c: grass, fence, CF watermark,
    // basepath/mound/home dirt skin, foul lines, base plates, plate marker,
    // fielder, ball trail + ball, throw, fielder/result labels, runner
    // tokens. Labels moved above the throw lines (Alex's report: a
    // fielding/receiving label sitting UNDER a dashed throw line right at
    // the point the throw converges on) - drawn last of the non-runner
    // layers so they're always legible over any line passing through their
    // anchor point. Runner tokens still stay on top of everything - they're
    // what the viewer follows.
    var svgVars = (runHex ? "--rn-fill:" + escapeHtml(runHex) + ";" : "") +
      (isHalfEnd ? "--break-delay:" + CF_BREAK_CROSSFADE_MS + "ms;" : "");
    return '<div class="scene-diamond-wrap">' +
      '<svg class="scene-diamond" viewBox="0 0 ' + FIELD_W + " " + FIELD_H + '" aria-hidden="true"' +
        (svgVars ? ' style="' + svgVars + '"' : "") + ">" +
        '<path class="dm-grass" d="' + grassPathD() + '"></path>' +
        '<path class="dm-warning-track" fill-rule="evenodd" d="' + warningTrackPathD() + '"></path>' +
        '<path class="dm-fence-wall" d="' + fenceWallPathD() + '"></path>' +
        '<path class="dm-fence" d="' + fencePathD() + '"></path>' +
        watermark +
        infieldSkinHtml() +
        rubberMarker +
        '<path class="dm-foul-line" d="' + foulLineD(0) + '"></path>' +
        '<path class="dm-foul-line" d="' + foulLineD(90) + '"></path>' +
        plates +
        '<path class="dm-plate" d="' + platePath + '"></path>' +
        (SHOW_FIELDER_TOKENS ? fielderTokensHtml(flight) : "") +
        ballFlightHtml(m, flight) +
        throwHtml(m, flight, moves) +
        stealThrowHtml(m, moves, runDelay, outDelay) +
        fielderNameLabelsHtml(m, flight) +
        ballResultLabelHtml(m, flight) +
        tokens +
      "</svg>" +
      sceneWheelDiffHtml(m, flight) +
      sceneWheelHzHtml(m, flight) +
    "</div>";
  }

  // "Mid Nth"/"End Nth" pill markup, sized off its own label length (no real
  // text-measurement pass - just enough padding either side that even the
  // longest realistic label, "End 12th" on a rare deep-extras game, still
  // clears the text with room to spare). Positioned at the same CF centroid
  // sceneFieldHtml's own watermark uses - see dm-mark-fading there.
  function cfBreakPillHtml(label) {
    var centroid = ftToSvg(0, CF_MARK_DEPTH_FT);
    var pillW = Math.max(74, label.length * 9 + 26), pillH = 27;
    return '<g class="dm-break-pill">' +
      '<rect x="' + (centroid.x - pillW / 2).toFixed(1) + '" y="' + (centroid.y - pillH / 2).toFixed(1) +
        '" width="' + pillW.toFixed(1) + '" height="' + pillH + '" rx="' + (pillH / 2) + '"></rect>' +
      '<text x="' + centroid.x.toFixed(1) + '" y="' + (centroid.y + 1).toFixed(1) + '">' +
        escapeHtml(label) + "</text>" +
    "</g>";
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
  // steal attempt) - DIFF still sits in its usual bottom-left corner of the
  // diamond wrap when that happens (see .scene-diamond-wrap .wheel).
  var WHEEL_CX = 75, WHEEL_CY = 75, WHEEL_VB = 150;
  var WHEEL_RING_R = 30, WHEEL_BAND_R = 23;
  var WHEEL_DOT_R = 4;
  // How far above centre the title ("DIFF"/"HZ") sits - pulled in from the
  // ring's own inner edge (Alex's report: a marker landing near 12 o'clock,
  // its bat/ball icon and (for the offense role) the handle poking inward
  // past the ring, could crowd right up against the title text sitting just
  // inside the ring there). A marker's own centre never gets closer than
  // WHEEL_RING_R-WHEEL_DOT_R to the wheel's centre; this keeps the title's
  // own glyphs comfortably inside that, never fighting a marker for the
  // same few pixels regardless of which angle it lands at.
  var WHEEL_TITLE_OFFSET_Y = 9;
  // The two value labels collide when their dots land angularly close
  // together (a small DIFF, i.e. good contact - exactly the common,
  // interesting case) even at different radii. Below this separation, push
  // each label's ANGLE (not its dot, which stays exactly on the wheel) apart
  // symmetrically so they never sit on top of each other or the ring.
  var WHEEL_LABEL_MIN_SEP_DEG = 26;
  // Radial placement, computed rather than a flat radius (Alex's catch: a
  // fixed radius overlapped the ring/dot whenever the label landed near the
  // wheel's left or right, where SVG text - always horizontal - runs
  // straight along the radius instead of across it, so its own half-width
  // eats into the "gap" a flat offset assumed). WHEEL_LABEL_GAP is the
  // clearance at the safest angles (top/bottom, where text runs tangential
  // to the ring); horizontalFactor scales in the extra padding a label
  // actually needs as it approaches the 3/9 o'clock positions, where the
  // danger is greatest.
  var WHEEL_LABEL_GAP = 5;
  var WHEEL_LABEL_CHAR_W = 2.4;   // rough half-glyph-width at this font size, viewBox units
  var WHEEL_LABEL_STAGGER = 9;   // label2 always sits at least this far past label1's own safe radius
  function wheelLabelRadius(angleDeg, text) {
    var horizontalFactor = Math.abs(Math.sin(angleDeg * Math.PI / 180));
    var halfWidth = String(text).length * WHEEL_LABEL_CHAR_W;
    return WHEEL_RING_R + WHEEL_DOT_R + WHEEL_LABEL_GAP + horizontalFactor * halfWidth;
  }

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
     marker's own C1 convention). pinTop/mirrored are HZ-only (Alex's ask):
     pinTop forces v1 (pitch) to sit at 12 o'clock instead of its own raw
     modular position, so every play reads from the same fixed reference
     point instead of the whole dial rotating play to play; mirrored flips
     the sweep direction (v2 placed the opposite way round) for a
     left-handed hitter, matching flightParams' own hand mirror on the
     HZ->spray-angle mapping (see angle = hand==="L" ? 45-frac*40 :
     45+frac*40) so the wheel's left/right reads the same way the resulting
     spray direction does, for either hand. */
  function wheelHtml(label, v1, v2, mod, cls1, cls2, centerBig, centerSmall, band, arcCls, pinTop, mirrored) {
    var deg1 = pinTop ? 0 : wheelAngleOf(v1, mod);
    var delta = signedCirc(v1, v2, mod);
    var deltaDeg = delta / mod * 360;
    if (mirrored) deltaDeg = -deltaDeg;
    var arcLen = WHEEL_RING_R * Math.abs(deltaDeg) * Math.PI / 180;
    var dot1Pt = wheelPt(WHEEL_RING_R, deg1);
    var dot2Pt = wheelPt(WHEEL_RING_R, deg1 + deltaDeg);
    // Labels stay anchored to their own dot's angle when there's room; below
    // WHEEL_LABEL_MIN_SEP_DEG of separation, each is nudged apart along the
    // ring by half the shortfall - away from the other dot's direction for
    // label1, further past it for label2 - so the two texts (and the ring
    // itself) never collide even when v1 and v2 are nearly equal.
    var pushSign = deltaDeg >= 0 ? 1 : -1;
    var extraPush = Math.max(0, (WHEEL_LABEL_MIN_SEP_DEG - Math.abs(deltaDeg)) / 2);
    var label1Deg = deg1 - extraPush * pushSign;
    var label2Deg = deg1 + deltaDeg + extraPush * pushSign;
    var label1R = wheelLabelRadius(label1Deg, v1);
    var label2R = Math.max(wheelLabelRadius(label2Deg, v2), label1R + WHEEL_LABEL_STAGGER);
    var label1Pt = wheelPt(label1R, label1Deg);
    var label2Pt = wheelPt(label2R, label2Deg);
    var bandHtml = band ? wheelBandArcHtml(deg1, deltaDeg >= 0 ? 1 : -1, band.lo, band.hi, mod) : "";
    return '<div class="wheel">' +
      '<svg class="wheel-svg" viewBox="0 0 ' + WHEEL_VB + " " + WHEEL_VB + '" aria-hidden="true">' +
        '<circle class="wheel-ring" cx="' + WHEEL_CX + '" cy="' + WHEEL_CY + '" r="' + WHEEL_RING_R + '"></circle>' +
        // Was an external label above the SVG - moved inside the ring
        // (Alex's call, to save space once the wheels sit in foul
        // territory on the diamond itself rather than their own row).
        '<text class="wheel-title" x="' + WHEEL_CX + '" y="' + (WHEEL_CY - WHEEL_TITLE_OFFSET_Y) +
          '">' + escapeHtml(label) + "</text>" +
        bandHtml +
        '<path class="wheel-arc wheel-arc-' + arcCls + '" d="' + wheelArcD(WHEEL_RING_R, deg1, deltaDeg) +
          '" style="--alen:' + arcLen.toFixed(2) + 'px"></path>' +
        wheelMarkerHtml(dot1Pt, deg1, cls1, "wheel-dot-1") +
        wheelMarkerHtml(dot2Pt, deg1 + deltaDeg, cls2, "wheel-dot-2") +
        '<text class="wheel-val wheel-val-1" x="' + label1Pt.x.toFixed(2) + '" y="' + label1Pt.y.toFixed(2) +
          '">' + escapeHtml(String(v1)) + "</text>" +
        '<text class="wheel-val wheel-val-2" x="' + label2Pt.x.toFixed(2) + '" y="' + label2Pt.y.toFixed(2) +
          '">' + escapeHtml(String(v2)) + "</text>" +
        '<text class="wheel-center-big" x="' + WHEEL_CX + '" y="' + (WHEEL_CY - (centerSmall ? 1 : -5)) +
          '">' + escapeHtml(centerBig) + "</text>" +
        (centerSmall
          ? '<text class="wheel-center-small" x="' + WHEEL_CX + '" y="' + (WHEEL_CY + 12) + '">' +
            escapeHtml(centerSmall) + "</text>"
          : "") +
      "</svg>" +
    "</div>";
  }

  // DIFF/HZ sit beside the diamond in .scene-top (Alex's call: "contained
  // in the rectangle around the field SVG", above the play-result pill,
  // not their own row underneath it). Split into two so playSceneHtml can
  // place DIFF on the left (3B side) and HZ on the right (1B side) of the
  // diamond; each still renders itself only when it has something real to
  // show (a walk/strikeout still has a real DIFF pair, but no HZ - no
  // batted ball to swing at).
  function sceneWheelDiffHtml(m, flight) {
    var isSteal = m.pitch == null && m.steal_num != null && m.throw_num != null;
    if (!(m.pitch != null && m.swing != null || isSteal)) return "";
    var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
    if (isSteal) {
      // Runner (offense, bat marker) breaks first; catcher (defense,
      // baseball marker) throws second - per Alex's spec, "runner # then
      // progress to catcher #". No archetype band - steals don't have one.
      return wheelHtml("DIFF", m.steal_num, m.throw_num, 1000, "off", "def",
        String(Math.abs(signedCirc(m.steal_num, m.throw_num, 1000))), null, null,
        wasOut ? "out" : "hit");
    }
    var bandRow = (data.meta.flight && data.meta.flight.bands || {})[m.result];
    // Exit velo used to also show here (centerSmall) - dropped (Alex's
    // call): it's already on screen via sceneFlightReadoutHtml, and the
    // wheel is small enough now (foul-territory overlay) that the room
    // is better spent on the interior DIFF title.
    return wheelHtml("DIFF", m.pitch, m.swing, 1000, "def", "off", String(m.diff),
      null,
      bandRow ? { lo: bandRow.lo, hi: bandRow.hi } : null,
      wasOut ? "out" : "hit");
  }
  // HZ: batted balls only (Alex's call) - `flight` truthy is exactly that
  // gate (flightParams returns null for everything else, the same set
  // sceneFlightReadoutHtml below already checks against).
  function sceneWheelHzHtml(m, flight) {
    if (!flight) return "";
    var d1p = lastDigit(m.pitch), d1s = lastDigit(m.swing);
    var mirrored = effectiveHand(m.batter_hand) === "L";
    return wheelHtml("HZ", d1p, d1s, 10, "def", "off", flight.angle.toFixed(0) + "°", null, null, "neutral",
      true, mirrored);
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
  /* The result pill (+ its Inside-the-Park callout, which annotates that
     same result) - pulled out of sceneDetailHtml so playSceneHtml can seat
     it right under the leverage meter, above the defense text line, while
     everything else that used to share its row (diff pill, launch angle/
     exit velo) stays below the field with the rest of sceneDetailHtml
     (Alex's spec). */
  function sceneResultPillHtml(m, flight) {
    var resultLabel = (data.meta.result_labels || {})[m.result] || m.result;
    return '<div class="scene-result-line">' +
      '<span class="result-pill ' + (m.result_category === "hitting" ? "offense" : "defense") + '">' +
        escapeHtml(resultLabel) + "</span>" +
      // Cheap and worth doing (Stage 4d): a home run that stayed inside the
      // park is rare enough that without a callout it reads as a glitch.
      (flight && m.result === "HR" && !flight.clearedFence
        ? '<span class="itp-pill">Inside the Park</span>' : "") +
    "</div>";
  }

  function sceneDetailHtml(m, flight) {
    /* Diff pill and the launch angle/exit velo readout stay beneath the
       field (Alex's spec) - the result pill itself now renders separately,
       above the field, via sceneResultPillHtml. "Player scores" moved out of
       here too (Alex's call) - it now renders between the defense line and
       the field itself, in playSceneHtml, so the top-down read is result
       pill -> defense-line description -> who scored -> the field. */
    return '<div class="scene-detail">' +
      '<div class="scene-play-line">' +
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

  // Outs as plain dots, no "OUTS" label (the header scorebug's own inning
  // row already reads as game state at a glance). Three dots, not the live
  // feed's up-to-two-then-a-badge-swap convention (stateStack) - this is a
  // replay of a completed play, not a live scoreboard, so the third out
  // gets its own dot same as the old sceneOutsHtml did.
  function scorebugOutsHtml(m) {
    var after = Math.max(0, Math.min(3, m.outs_after == null ? (m.outs_before || 0) : m.outs_after));
    var dots = [0, 1, 2].map(function (i) {
      return '<span class="dot' + (i < after ? " on" : "") + '"></span>';
    }).join("");
    return '<div class="outs-dots">' + dots + "</div>";
  }

  /* Horizontal MLB-style scorebug (physics-redesign-adjacent UI pass, not
     part of the ball-flight plan): away team on the left, home on the right,
     each logo+abbr+score - replaces the old vertical away/home stack that
     used to sit in a side column next to the diamond. The middle column
     carries the inning indicator and outs (no OBC mini-diamond - the
     animated diamond right below already shows base runners) on top, with
     the leverage meter underneath. Sits above `.scene-top` as the slide's
     own header, beneath the modal's progress bar. */
  function sceneScorebugHtml(m, flight, newHalf) {
    var awayBatting = !m.batting_is_home;
    var runs = m.runs || 0;
    var arrivals = runs ? scoreArrivals(m, flight) : [];
    return '<div class="scene-scorebug">' +
      '<div class="scene-scorebug-team' + (awayBatting ? " batting" : "") + '">' +
        teamLogoImg(m.away_team_abbr, "scene-scorebug-logo") +
        '<span class="abbr">' + escapeHtml(m.away_team_abbr) + "</span>" +
        scoreCellHtml(m.away_score, awayBatting ? runs : 0, arrivals) +
      "</div>" +
      '<div class="scene-scorebug-mid">' +
        '<div class="scene-scorebug-state' + (newHalf ? " new-half" : "") + '">' +
          '<div class="tri ' + (m.half === "top" ? "up" : "down") + '"></div>' +
          '<div class="inning-num">' + m.inning + "</div>" +
          scorebugOutsHtml(m) +
        "</div>" +
        sceneMeterHtml(m) +
      "</div>" +
      '<div class="scene-scorebug-team home' + (!awayBatting ? " batting" : "") + '">' +
        teamLogoImg(m.home_team_abbr, "scene-scorebug-logo") +
        '<span class="abbr">' + escapeHtml(m.home_team_abbr) + "</span>" +
        scoreCellHtml(m.home_score, awayBatting ? 0 : runs, arrivals) +
      "</div>" +
    "</div>";
  }

  /* Text line above the field, below the leverage meter (Decision 7) -
     shares fieldingChain/involvedPositions/m.defense exactly with the field
     name labels (Decision 5), so the two always agree about which fielder(s)
     this play is about. Always renders the container, even empty, so the
     slide layout doesn't jump between plays that do and don't have a line.
     Wording settled against real MLB.com play-by-play (Alex's review of
     scraped examples from 10 real games, covering every result code except
     Balk, which never came up in that sample) - MLB's own fielding clause is
     "{position spelled out} {name} to {position spelled out} {name}...", but
     Alex's call was to keep the existing scorecard-notation prefix and use
     the position ABBREVIATION (2B/SS/CF/...) ahead of each name instead of
     the full word - denser, and this app's audience already reads scorecard
     digits. Batted-ball-type wording for hits ("on a line drive to...") was
     considered and explicitly dropped - our archetype tag for a hit is just
     its own result family (e.g. "double"), not a real ground_ball/line_drive/
     fly_ball classification, so that word would have to be guessed from
     launch angle rather than read off real data. */
  function sceneDefenseLineHtml(m, flight) {
    var defense = m.defense || {};
    var chain = fieldingChain(m, flight);
    var text = "";
    if (chain) {
      // A chain position with no resolved name renders its bare position
      // code alone, no name to pair it with (e.g. "6-4-3: SS Uraz to 2B to
      // 1B Sexton") - but if NOTHING in the chain resolved, the line would
      // just repeat the position numbers the result pill's own notation
      // already shows, so it's suppressed entirely rather than rendered as
      // pure noise.
      var anyResolved = chain.some(function (pos) { return !!defense[pos]; });
      if (anyResolved) {
        var parts = chain.map(function (pos) {
          var entry = defense[pos];
          return entry ? (pos + " " + entry[1]) : pos;
        });
        text = fieldingNotation(m, flight) + ": " + parts.join(" to ");
      }
    } else {
      var involved = involvedPositions(m, flight);
      var entry = involved.length === 1 ? defense[involved[0]] : null;
      if (entry) {
        text = "Fielded by " + involved[0] + " " + entry[1];
      }
    }
    // Walk, strikeout, homer, unresolved: nothing to add beyond what the
    // scorebug already shows - the container stays, just empty.
    return '<div class="scene-defense">' + (text ? escapeHtml(text) : "") + "</div>";
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
    if (flight) {
      var hand = effectiveHand(m.batter_hand);
      var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
      if (GROUND_ARCHETYPES[flight.archetype] && wasOut) {
        resolveGrounderInterception(m, flight, hand);
      } else if (wasOut && CAUGHT_IN_AIR[flight.archetype]) {
        applyAirPositionOverride(m, flight, hand);
      } else if (!flight.clearedFence) {
        resolveHitPickup(flight);
      }
    }
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
      sceneScorebugHtml(m, flight, newHalf) +
      sceneResultPillHtml(m, flight) +
      sceneDefenseLineHtml(m, flight) +
      scoringLine(m) +
      '<div class="scene-top">' +
        sceneFieldHtml(m, flight) +
      "</div>" +
      sceneDetailHtml(m, flight) +
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
  // The last play of a half-inning: how long its own CF mark shows before
  // fading to the "Mid Nth"/"End Nth" pill (sceneFieldHtml's isHalfEnd,
  // --break-delay), and how much extra dwell that slide gets on top of its
  // normal reading time so the pill isn't just barely on screen before
  // auto-advance cuts it off - same reasoning as HALF_INNING_BONUS_MS's own
  // beat, just sized like a title slide's dwell (TITLE_DWELL_MS) since the
  // pill is effectively a title card appearing in place.
  var CF_BREAK_CROSSFADE_MS = 2400;
  var CF_BREAK_BONUS_MS = 1800;

  function slideDwell(slide) {
    var speed = getPlaybackSpeed();
    if (slide.kind !== "play") return TITLE_DWELL_MS / speed;
    var base = slide.play.is_key_moment ? PLAY_DWELL_MS_KEY : PLAY_DWELL_MS_ROUTINE;
    var isHalfEnd = !!slide.play.is_half_inning_final && !slide.play.is_game_final;
    return (base + (startsHalfInning(slide) ? HALF_INNING_BONUS_MS : 0) +
      (isHalfEnd ? CF_BREAK_BONUS_MS : 0)) / speed;
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
  // complete set the build ships, so no separate list to keep in sync. Also
  // offers scorecard fielding notations (4-6-3, F8, ...) matching the typed
  // text - unlike result codes these aren't a fixed enum, so they're pulled
  // live from whichever plays are in the current pool (playFieldingNotation
  // memoises per play, so this stays cheap after the first pass touches a
  // given play). Raw-code matches fill first, notation matches top up
  // whatever's left of the limit.
  function renderResultCodeSuggest(query) {
    var box = $("result-code-suggest");
    var needle = query.trim().toLowerCase();
    if (!needle) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    var labels = data.meta.result_labels || {};
    var codeMatches = Object.keys(labels).filter(function (code) {
      return code.toLowerCase().indexOf(needle) !== -1 ||
        (labels[code] || "").toLowerCase().indexOf(needle) !== -1;
    }).sort().slice(0, RESULT_CODE_SUGGEST_LIMIT);

    var notationMatches = [];
    if (codeMatches.length < RESULT_CODE_SUGGEST_LIMIT) {
      var seen = {};
      pool().forEach(function (m) {
        var fn = playFieldingNotation(m);
        if (!fn || seen[fn] || labels[fn]) return;
        seen[fn] = true;
        if (fn.toLowerCase().indexOf(needle) !== -1) notationMatches.push(fn);
      });
      notationMatches.sort();
    }
    var matches = codeMatches.map(function (code) {
      return { code: code, label: labels[code] || "" };
    }).concat(notationMatches.slice(0, RESULT_CODE_SUGGEST_LIMIT - codeMatches.length).map(function (fn) {
      return { code: fn, label: "Fielding sequence" };
    }));
    if (!matches.length) {
      box.innerHTML = '<div class="player-suggest-empty">No results match.</div>';
      box.hidden = false;
      return;
    }
    box.innerHTML = matches.map(function (row) {
      return '<div class="player-suggest-row" data-result-code="' + escapeHtml(row.code) + '">' +
        "<strong>" + escapeHtml(row.code) + "</strong>" +
        '<span class="team">' + escapeHtml(row.label) + "</span></div>";
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

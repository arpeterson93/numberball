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

  // /live/ is only for the current season's in-progress/just-finished games -
  // mln-reference moves a game to /game/ once its season is no longer live.
  var GAME_LINK_BASE = "https://www.mln-reference.com/live/";
  var GAME_LINK_BASE_ARCHIVE = "https://www.mln-reference.com/game/";
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

  // Which season's data is loaded into `data` above. `current` is the live
  // season (data.meta.season at boot, never changes after that); `active` is
  // whichever season the visitor is currently browsing - equal to `current`
  // except while browsing history. `cache` holds every season's data object
  // once loaded, keyed by season number, so switching back is instant and
  // costs no re-fetch. Historical season dirs are immutable once committed,
  // so nothing here needs invalidating.
  var season = { current: null, active: null, cache: {} };
  // [{season, sessions}, ...] for every committed archive season - from the
  // live season's meta.archive_seasons, captured once at boot (immutable
  // once committed, so no need to refresh except when reloadData() re-reads
  // the live meta in case a new archive season landed while the tab's open).
  var archiveSeasonsMeta = [];
  // The LIVE season's own session list, cached separately from data.meta -
  // once a historical season is active, data.meta.sessions belongs to THAT
  // season, but the merged season+session picker still needs to list the
  // live season's own sessions as one of its groups.
  var liveSeasonSessions = [];

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

  // Historical season files are immutable once committed (Part 2) - no
  // cache-buster, no no-store, so the browser/CDN can actually cache them.
  function getJSONCached(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(url + ": " + r.status);
      return r.json();
    });
  }

  // Path to a file in the ACTIVE season's data dir - current season's own
  // files live at the top level, any other (historical) season's live under
  // data/sNN/.
  function dataPath(file) {
    return season.active === season.current ? "data/" + file : "data/s" + pad2(season.active) + "/" + file;
  }

  // Fetch a file from the active season, live-vs-cached per the same rule.
  function fetchSeasonJSON(file) {
    var url = dataPath(file);
    return season.active === season.current ? getJSON(url) : getJSONCached(url);
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

  // A team's secondary color (key_moments_build.py's SECONDARY_HEX, a hand-
  // maintained constant - not on the Teams sheet) - "" when that team has
  // none on file. Same shape as teamColor so gameTeamColors below can treat
  // the two symmetrically.
  function teamSecondaryColor(abbr) {
    var hex = ((data.meta.teams || {})[abbr] || {}).secondary_hex || "";
    if (hex && hex.charAt(0) !== "#") hex = "#" + hex;
    return hex;
  }

  function hexToRgb(hex) {
    var h = (hex || "").replace("#", "");
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    if (h.length !== 6) return null;
    var n = parseInt(h, 16);
    if (isNaN(n)) return null;
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  /* "redmean" - a cheap, widely-used perceptually-weighted RGB distance (eyes
     are far more sensitive to green than to red/blue, so naive Euclidean
     distance under- and over-states clashes depending on which channel two
     colors differ in) - plenty for "are these two team colors too close to
     tell apart at a glance", without a full sRGB->LAB conversion. Range is
     0 (identical) to ~765 (black vs white); real team-color pairs live well
     inside that. Missing/invalid hex reads as maximally different (Infinity)
     rather than blocking a fallback tier over a data gap. */
  function colorDistance(hexA, hexB) {
    var a = hexToRgb(hexA), b = hexToRgb(hexB);
    if (!a || !b) return Infinity;
    var rmean = (a.r + b.r) / 2;
    var dr = a.r - b.r, dg = a.g - b.g, db = a.b - b.b;
    return Math.sqrt((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db);
  }

  // Below this redmean distance, two team colors read as too similar side by
  // side (Alex's ask) - a starting guess, worth re-tuning once real
  // secondary hexes are in and a few actual clashing matchups can be
  // eyeballed against it.
  var TEAM_COLOR_MIN_DISTANCE = 100;

  /* Home/away color pair for a single game, substituting toward each team's
     secondary color when their primaries clash (Alex's ask) - checked in a
     fixed fallback order: 1) primary/primary, 2) home primary + away
     secondary, 3) home secondary + away primary, 4) both secondary -
     stopping at the first tier that clears TEAM_COLOR_MIN_DISTANCE, or
     falling through to tier 4 regardless if none do (nothing further left to
     try). A team with no secondary_hex on file just keeps its primary
     through every tier - teamSecondaryColor(...) || primary makes that slot
     a no-op fallback instead of clearing to nothing. */
  function gameTeamColors(homeAbbr, awayAbbr) {
    var homePrimary = teamColor(homeAbbr), awayPrimary = teamColor(awayAbbr);
    var homeSecondary = teamSecondaryColor(homeAbbr) || homePrimary;
    var awaySecondary = teamSecondaryColor(awayAbbr) || awayPrimary;
    var tiers = [
      { home: homePrimary, away: awayPrimary },
      { home: homePrimary, away: awaySecondary },
      { home: homeSecondary, away: awayPrimary },
      { home: homeSecondary, away: awaySecondary },
    ];
    for (var i = 0; i < tiers.length - 1; i++) {
      if (tiers[i].home && tiers[i].away && colorDistance(tiers[i].home, tiers[i].away) >= TEAM_COLOR_MIN_DISTANCE) {
        return tiers[i];
      }
    }
    return tiers[tiers.length - 1];
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
    // On-deck: no featured_wp_after/wpa (nothing has happened on this play
    // yet), but _next_batter_moment still carries win_prob_after forward -
    // shows the team actually leading right now, no delta to report since
    // there's no "before" for a play that hasn't happened (same reasoning
    // the ribbon marker in the slideshow uses).
    if (m.is_on_deck) {
      var hw = homeWpOf(m);
      if (hw == null) return "";
      var leadTeam = hw >= 0.5 ? m.home_team_abbr : m.away_team_abbr;
      var leadPct = Math.round((hw >= 0.5 ? hw : 1 - hw) * 100);
      return "<span>" + escapeHtml(leadTeam) + " win probability " + leadPct + "%</span>";
    }
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
    // On-deck placeholder: no separate tag here (Alex's later call) - the
    // "Now Batting" pill on the play-line already says what's coming, so the
    // diamond/outs render exactly as they would for any other in-progress
    // situation.
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
    // On-deck has no result yet - "Now Batting" in the same blue pill a real
    // hitting result gets, rather than a broken lookup on a null result.
    var resultLabel = m.is_on_deck ? "Now Batting" : ((data.meta.result_labels || {})[m.result] || m.result);
    var resultCat = m.is_on_deck ? "hitting" : m.result_category;
    var counterpart = (m.counterpart_id && m.counterpart_id !== m.featured_id)
      ? '<span class="counterpart">vs ' +
        (isFavoritedId(m.counterpart_id)
          ? '<span class="counterpart-fav" title="On your favorites list">★</span> '
          : "") +
        '<a class="counterpart-name" href="' + PLAYER_LINK_BASE + encodeURIComponent(m.counterpart_id) +
        '" target="_blank" rel="noopener noreferrer">' + escapeHtml(m.counterpart_name) + "</a></span>"
      : "";
    // Historical (not the live season currently being played) games moved to
    // mln-reference's /game/ path - every card currently on screen belongs
    // to whichever season is active, so this one check covers all of them.
    var gameLinkBase = season.active !== season.current ? GAME_LINK_BASE_ARCHIVE : GAME_LINK_BASE;
    // mln-reference's own game ids drop the season digit's leading zero
    // (season 1-9) - game_code stays zero-padded everywhere else in this
    // file (session/game lookups depend on the fixed 6-digit shape), so
    // this strip is local to the outbound link only.
    var gameLink = m.game_code
      ? '<a class="game-link" href="' + gameLinkBase + encodeURIComponent(String(Number(m.game_code))) +
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
          '<span class="result-pill ' + (resultCat === "hitting" ? "offense" : "defense") + '">' +
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
    var gameColors = gameTeamColors(g.home_team_abbr, g.away_team_abbr);
    var awayHex = gameColors.away || "#9aa4b2";
    var homeHex = gameColors.home || "#c7ccd3";
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
  // A full slate's game count - not a hard cap on how many games can ever
  // show, just the reference size whose balanced column count every row
  // borrows (see below).
  var SCOREBOARD_FULL_SESSION_GAMES = 8;

  /* Caps every row at however many columns a FULL slate
     (SCOREBOARD_FULL_SESSION_GAMES) would balance into at the current width
     - e.g. a natural fit of 6-per-row balances 8 tiles into two rows of 4 -
     rather than balancing against however many games THIS session actually
     has. That way a light session's cards render at the same size as a full
     session's instead of stretching to fill the row (Alex's ask): 2 games
     still render at the full session's per-tile width, just with 2 empty
     grid cells trailing rather than 2 tiles stretched to fill the row. Still
     adapts to viewport width - narrower windows still get fewer, larger
     columns, down to the phone cap - just never against the session's own
     game count. */
  function applyScoreboardColumns() {
    var row = document.querySelector("#scoreboard .scoreboard-row");
    if (!row) return;
    if (!row.children.length) return;
    var cols;
    if (window.innerWidth <= SCOREBOARD_MOBILE_BREAKPOINT) {
      cols = SCOREBOARD_MOBILE_MAX_COLS;
    } else {
      var fitsByWidth = Math.max(1, Math.floor((row.clientWidth + SCOREBOARD_GAP) / (SCOREBOARD_TILE_MIN + SCOREBOARD_GAP)));
      var refCols = Math.min(fitsByWidth, SCOREBOARD_FULL_SESSION_GAMES);
      var refRows = Math.ceil(SCOREBOARD_FULL_SESSION_GAMES / refCols);
      cols = Math.ceil(SCOREBOARD_FULL_SESSION_GAMES / refRows);
    }
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
      $("live-grid-btn").hidden = true;
      return;
    }
    el.hidden = false;
    label.textContent = "Scoreboard";
    el.innerHTML = '<div class="scoreboard-row">' + games.map(scoreboardCard).join("") + "</div>";
    applyScoreboardColumns();
    // Widescreen-only (style.css hides it below 900px regardless) - shown
    // here whenever there's at least one game to look in on, same gate the
    // scoreboard row itself uses. "LIVE" overpromised for a historical
    // session with nothing actually live (Alex's report) - the label now
    // names the shape of what opens instead: the exact-count grids get
    // their own name, anything else in between is a Multiview, and a
    // single game (which doesn't even open a grid - openLiveGrid falls
    // back to the plain single-game replay) gets Spotlight.
    // Only the label span's text changes, not the button's own innerHTML -
    // the icon svg is a permanent sibling, not something to recreate (or
    // accidentally wipe with a textContent write) on every render.
    $("live-grid-btn").hidden = false;
    $("live-grid-btn-label").textContent = games.length === 1 ? "Spotlight"
      : games.length === 4 ? "Quad-Box"
      : games.length === 8 ? "Octo-Box"
      : "Multiview";
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
      return fetchSeasonJSON("plays_" + pad2(s) + ".json").then(function (rows) {
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
      return fetchSeasonJSON("plays_" + pad2(s) + ".json").then(function (rows) {
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

  // The on-deck "Now Batting" placeholder isn't a real new play - it just
  // inherits its game's last real play's own timestamp (key_moments_build.
  // py's _next_batter_moment never overrides that field), so it rides along
  // into `groups` whenever that real play itself is new. Excluded from the
  // headline count (Alex's ask) but left in buildCatchUpSlides's own reel -
  // still worth showing as "who's up next" context to close out a game.
  function catchUpPlayCount(groups) {
    return (groups || []).reduce(function (n, g) {
      return n + g.plays.filter(function (p) { return !p.is_on_deck; }).length;
    }, 0);
  }

  // Auto-fade timer for the "You're All Caught Up" state below - tracked so
  // a fresh render (another computeCatchUp run, say) always cancels
  // whatever fade was previously scheduled rather than stacking timers that
  // could hide a LATER, different banner state out from under the viewer.
  var catchUpCaughtUpTimer = null;
  var CATCH_UP_CAUGHT_UP_MS = 30000;

  /* Banner states, in the order they are checked:
       no name        - quiet prompt, opens the Favorites modal (that modal
                        already leads with the name input, so there is no
                        second name-prompt UI to build)
       computing      - a name is known but computeCatchUp hasn't resolved
                        yet (data.catchUpGroups still null - loadAllSessions
                        takes a real moment) - a spinner placeholder instead
                        of the banner just staying absent for however long
                        that takes (Alex's ask).
       nothing new    - same prominent format as "something new" below, just
                        saying "You're All Caught Up" - fades out on its own
                        after CATCH_UP_CAUGHT_UP_MS rather than standing
                        forever (Alex's ask: confirm the check actually ran,
                        without becoming permanent noise on every visit).
       something new  - prominent, with the count */
  function renderCatchUpBanner() {
    var el = $("catchup-banner");
    if (!el) return;
    window.clearTimeout(catchUpCaughtUpTimer);
    el.classList.remove("fading");
    var fav = window.KMFavorites;
    if (fav && !fav.hasName()) {
      el.hidden = false;
      el.classList.add("quiet");
      el.classList.remove("loading", "caught-up");
      el.textContent = "Catch Me Up - add your name to track what's new";
      return;
    }
    if (data.catchUpGroups === null) {
      el.hidden = false;
      el.classList.remove("quiet", "caught-up");
      el.classList.add("loading");
      el.innerHTML = '<span class="catchup-spinner" aria-hidden="true"></span>Checking for new plays...';
      return;
    }
    el.classList.remove("loading");
    var count = catchUpPlayCount(data.catchUpGroups);
    if (!count) {
      el.hidden = false;
      el.classList.remove("quiet");
      el.classList.add("caught-up");
      el.textContent = "You're All Caught Up";
      catchUpCaughtUpTimer = window.setTimeout(function () {
        el.classList.add("fading");
        // Matches the CSS transition length (see .catchup-banner.fading) -
        // hidden only once the fade has actually finished playing, so it
        // doesn't just vanish mid-transition on a slow frame.
        window.setTimeout(function () { el.hidden = true; }, 650);
      }, CATCH_UP_CAUGHT_UP_MS);
      return;
    }
    el.hidden = false;
    el.classList.remove("quiet", "caught-up");
    // An SVG triangle, not a "▶" text glyph - iOS renders that glyph with its
    // own colored emoji presentation, which reads differently there than the
    // plain monochrome arrow desktop browsers show for the same character.
    el.innerHTML = '<svg class="catchup-play-icon" viewBox="0 0 24 24" aria-hidden="true">' +
      '<polygon points="8 5 19 12 8 19 8 5"></polygon></svg> Catch Me Up · ' +
      count + (count === 1 ? " new play" : " new plays");
  }

  // ── Playback speed: a per-browser preference (same "device identity" as the
  //    favorites name), not synced anywhere - it changes both what
  //    slideDwell() hands back (how long a slide stays up) AND, via the
  //    --play-speed CSS custom property applyPlaybackSpeedVar keeps in sync,
  //    how fast every animation-duration/animation-delay in the slideshow
  //    itself plays (see style.css's :root - every animated rule under
  //    .play-scene divides its timing by var(--play-speed,1)), so 2x actually
  //    looks twice as fast rather than just waiting half as long between
  //    plays. Lives as a toggle button in each slideshow's own control bar
  //    (Alex's ask) rather than a Settings-panel field - cycles 0.5x/1x/1.5x/
  //    2x, wrapping, always showing the current value as its own label. ──────
  var PLAYBACK_SPEED_KEY = "km_playback_speed";
  var PLAYBACK_SPEED_MIN = 0.25, PLAYBACK_SPEED_MAX = 2;
  var PLAYBACK_SPEED_STEPS = [0.5, 1, 1.5, 2];

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

  // Global, not scoped to either modal: the two slideshows never run at the
  // same time, and setting it on the root means a slide mounted anywhere
  // (Catch Me Up, Game Replay, a future third surface) picks it up with no
  // extra wiring at the mount site.
  function applyPlaybackSpeedVar(speed) {
    try { document.documentElement.style.setProperty("--play-speed", String(speed)); } catch (e) { /* no-op */ }
  }

  function setPlaybackSpeed(v) {
    var speed = clampSpeed(v);
    try { window.localStorage.setItem(PLAYBACK_SPEED_KEY, speed.toFixed(2)); } catch (e) { /* private browsing */ }
    applyPlaybackSpeedVar(speed);
    return speed;
  }

  function speedLabel(v) { return v.toFixed(1) + "x"; }

  // Steps to the next value in PLAYBACK_SPEED_STEPS, wrapping past 2x back to
  // 0.5x - a stored value that doesn't land exactly on a step (an old 0.25x-
  // range preference from before this became a fixed cycle) rounds up to the
  // nearest one first rather than getting stuck between two steps forever.
  function cyclePlaybackSpeed() {
    var cur = getPlaybackSpeed();
    var i = PLAYBACK_SPEED_STEPS.findIndex(function (s) { return s >= cur - 0.001; });
    var next = PLAYBACK_SPEED_STEPS[(i + 1 + PLAYBACK_SPEED_STEPS.length) % PLAYBACK_SPEED_STEPS.length];
    return setPlaybackSpeed(next);
  }

  // Both slideshows' speed buttons show the same global preference - synced
  // together on every change so whichever one opens next is never stale.
  function syncSpeedButtons() {
    var speed = getPlaybackSpeed();
    var label = speedLabel(speed);
    ["catchup-speed", "replay-speed"].forEach(function (id) {
      var btn = $(id);
      if (!btn) return;
      btn.textContent = label;
      btn.title = "Playback speed: " + label;
    });
    // cyclePlaybackSpeed already routes through setPlaybackSpeed, which keeps
    // --play-speed current on its own - this call only matters at startup,
    // before setPlaybackSpeed has ever run this session.
    applyPlaybackSpeedVar(speed);
  }

  function wireSpeedToggle(btnId) {
    var btn = $(btnId);
    if (!btn) return;
    btn.addEventListener("click", function () {
      cyclePlaybackSpeed();
      syncSpeedButtons();
    });
  }

  // ── Loop mode: same per-browser, unsynced preference as playback speed,
  //    Apple-Music-style three-state cycle (Alex's ask) -
  //      "all"  - play through the whole run once, then loop back to the start
  //               (the default)
  //      "none" - play through the whole run once and stop/hold at the end
  //      "one"  - repeat only the play currently on screen
  //    Read fresh off localStorage by the two slideshow engines' own timeout
  //    callbacks each time one fires, so a change mid-dwell takes effect on
  //    the very next tick with no extra wiring. ──────────────────────────
  var LOOP_MODE_KEY = "km_loop_mode";
  var LOOP_MODES = ["all", "none", "one"];
  var LOOP_MODE_LABELS = { all: "Repeat: All", none: "Repeat: Off", one: "Repeat: One" };

  function getLoopMode() {
    try {
      var raw = window.localStorage.getItem(LOOP_MODE_KEY);
      return LOOP_MODES.indexOf(raw) !== -1 ? raw : "all";
    } catch (e) {
      return "all";
    }
  }

  function setLoopMode(mode) {
    var m = LOOP_MODES.indexOf(mode) !== -1 ? mode : "all";
    try { window.localStorage.setItem(LOOP_MODE_KEY, m); } catch (e) { /* private browsing */ }
    return m;
  }

  function cycleLoopMode() {
    var i = LOOP_MODES.indexOf(getLoopMode());
    return setLoopMode(LOOP_MODES[(i + 1) % LOOP_MODES.length]);
  }

  function syncLoopButtons() {
    var mode = getLoopMode();
    var label = LOOP_MODE_LABELS[mode];
    ["catchup-loop", "replay-loop"].forEach(function (id) {
      var btn = $(id);
      if (!btn) return;
      btn.setAttribute("data-mode", mode);
      btn.title = label;
      btn.setAttribute("aria-label", "Cycle repeat mode (currently " + label + ")");
    });
  }

  function wireLoopToggle(btnId) {
    var btn = $(btnId);
    if (!btn) return;
    btn.addEventListener("click", function () {
      cycleLoopMode();
      syncLoopButtons();
    });
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
        // Doesn't advance the counter for the on-deck placeholder (matches
        // catchUpPlayCount's own exclusion) - it just repeats the last real
        // play's number rather than pushing playNo past total.
        if (!p.is_on_deck) playNo += 1;
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
  // Real batter's-box starting point (Alex's ask): a right-handed batter
  // stands in the box nearer 3B (negative x here), a lefty nearer 1B
  // (positive x) - purely the batter's OWN runner token's start; the ball's
  // contact point and every other piece of physics still comes straight out
  // of flightParams, untouched. Deliberately subtle (Alex's words: "just a
  // subtle adjustment... as they come out of the batter's box") - the run
  // to first still heads for the same real basepath, it just leaves from a
  // couple feet to one side of dead-center on the plate instead of the
  // exact center every other token (and every label anchored at "home")
  // still uses.
  var BATTER_BOX_OFFSET_FT = 5;
  function batterBoxStartPt(hand) {
    return ftToSvg(hand === "L" ? BATTER_BOX_OFFSET_FT : -BATTER_BOX_OFFSET_FT, 0);
  }
  // Alex's ask: a walk's pitch should visibly miss the zone - away from
  // whichever side the batter stands on (an "outside" pitch), not down the
  // middle like every other result's pitch. Literal mirror of
  // batterBoxStartPt's own sign: that function offsets the batter TOWARD
  // their own box side, this one offsets the pitch to the FAR side of the
  // plate from wherever that box is - a lefty (box toward 1B/+x) sees the
  // ball miss toward 3B/-x, and vice versa. A bigger offset than the box's
  // own 5ft so it doesn't read as landing on the batter themselves.
  var WALK_PITCH_OFFSET_FT = 8;
  function walkPitchTargetSvg(hand) {
    return ftToSvg(hand === "L" ? -WALK_PITCH_OFFSET_FT : WALK_PITCH_OFFSET_FT, 0);
  }

  // Real MLB basepaths: 90ft square, so home-to-1B/3B is 90ft along the foul
  // lines (angle 90/0, i.e. offset +-45deg from dead centre) and home-to-2B
  // is the 90ft square's diagonal.
  var BASE_DIST_FT = 90;
  var BASE_DIAG_FT = BASE_DIST_FT * Math.SQRT2;
  // Real feet, not projected screen pixels (unlike SCENE_BASES below) -
  // throwSchedule's own real-distance-per-throw timing (Alex's ask: a relay
  // throw must not draw for as long as a full corner-to-first throw) needs
  // straight-line feet between two bases, computed before any perspective
  // projection touches it.
  var BASE_POS_FT = {
    HOME: { x: 0, y: 0 },
    "1B": { x: BASE_DIST_FT * Math.SQRT1_2, y: BASE_DIST_FT * Math.SQRT1_2 },
    "2B": { x: 0, y: BASE_DIAG_FT },
    "3B": { x: -BASE_DIST_FT * Math.SQRT1_2, y: BASE_DIST_FT * Math.SQRT1_2 },
  };
  // Token/marker sizes, scaled down from the old hand-placed diamond to match
  // the now-correctly-scaled (and visually smaller) real-90ft infield.
  var RUNNER_R = 6, BASE_R = 4.5, BALL_R = 3, FIELDER_R = 4;
  // The glove icon (#fielder-glove symbol, docs/index.html) reads smaller than
  // a filled circle at the same bounding-box size - its silhouette is mostly
  // negative space around the fingers/webbing - so its box is sized up from
  // the old dot radius rather than matched to it 1:1. Tune by eye.
  var FIELDER_ICON_SIZE = FIELDER_R * 2.6;
  // Ground-projected shadow under a flight ball (Alex's ask): a first pass,
  // not perspective-correct per screen position - just a flattened ellipse
  // (foreshortened like a real ground shadow under this field's angled
  // camera) uniformly scaled by how close to the ground the ball currently
  // is. Scale 1 at ground level (SHADOW_SCALE_MAX), shrinking toward apex
  // (SHADOW_SCALE_MIN) - tunable, no real-world unit behind these two.
  var SHADOW_RX = 2, SHADOW_RY = 1.1;
  var SHADOW_SCALE_MIN = 0.55, SHADOW_SCALE_MAX = 1.6;

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
  // The "straight out from home at this depth" direction used to derive
  // each position's starting anchor point below - Alex's own explicit
  // -30/-12/12/35deg (3B/SS/2B/1B) spec, in the same landingPoint
  // offset-from-45 convention OF_CANONICAL_ANGLE uses (so angleDeg here is
  // just 45+that offset). No longer tied to HZ_FIELDER_BY_ANGLE's own
  // bucket midpoints (the previous 9/29/61/81 values, which were) - that
  // bucket system still independently decides which infielder fields a
  // given grounder angle, this table only ever feeds the anchor a fielder
  // starts and animates from, so the two can differ without conflict. P
  // keeps 45 (dead centre, offset 0) - not part of Alex's ask.
  var CANONICAL_ANGLE = { "3B": 15, SS: 33, P: 45, "2B": 57, "1B": 80 };
  // The minimum lattice angle mapping to each position - the direction a
  // BRC-excluded ground ball gets redirected to on override (Part 4.2/4.3).
  var MIN_ANGLE_FOR_POS = { "3B": 5, SS: 21, P: 45, "2B": 53, "1B": 77 };
  // Traditional scorecard numbering (fieldingNotation, below outThrowTargets) -
  // the app's own position strings already match the real 9 defensive spots
  // 1-for-1, they've just never had scorecard numbers attached before.
  var POSITION_NUMBER = { P: 1, C: 2, "1B": 3, "2B": 4, "3B": 5, SS: 6, LF: 7, CF: 8, RF: 9 };

  // Starting (pre-pitch) outfield depth (Alex's ask) - same 295ft radial
  // distance from home for all three, since no real per-position starting-
  // depth data exists to justify unequal ones (the previous hand-placed
  // anchors had LF/RF sitting deeper than CF, backwards from real MLB
  // positioning, purely as an artifact of nobody having derived them from a
  // radial distance in the first place). angle uses the same landingPoint
  // offset-from-45 convention CANONICAL_ANGLE already relies on, so
  // OF_CANONICAL_ANGLE's 18/45/72 land exactly on Alex's own -27/0/27deg
  // spec (LF/CF/RF) once landingPoint subtracts the 45 back out.
  var OUTFIELDER_DEPTH_FT = { LF: 295, CF: 295, RF: 295 };
  var OF_CANONICAL_ANGLE = { LF: 18, CF: 45, RF: 72 };

  // Nine generic fielder anchors, field-plane feet. No names, no per-play
  // defensive alignment - that data doesn't exist (ball-flight-plan.md
  // Decision 5). Infield AND outfield anchors are both DERIVED from their
  // own depth/angle tables above (physics-redesign plan Part 4.1 did this
  // for infield first; outfield followed the same pattern once Alex had a
  // real depth/angle spec for it) - one real-world-unit source instead of
  // hand-placed (x,y) pairs that can silently drift out of sync with the
  // depth numbers those positions are documented at (a 3B anchor once sat at
  // the hand-placed (-75,85), about 13ft from where its own documented
  // 119ft depth actually points; that's now impossible by construction, for
  // either infield or outfield). C alone stays hand-placed - no depth
  // concept applies to a position that starts at the plate.
  var FIELDER_ANCHORS_FT = {
    C: { x: 0, y: -5 },
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
  ["LF", "CF", "RF"].forEach(function (pos) {
    var pt = landingPoint(OUTFIELDER_DEPTH_FT[pos], OF_CANONICAL_ANGLE[pos]);
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

  // Real average MLB Statcast "sprint speed" (Alex's call, ideas-and-opinions
  // conversation), applied to the real 90ft (BASE_DIST_FT) each leg actually
  // covers - unlike the old sub-linear stylized table this replaces (tuned
  // only to fit a fixed slide-dwell budget, not to real speed - see
  // slideDwell's own comment for why that budget itself had to become
  // dynamic instead), every leg here genuinely does take the same real time,
  // since speed and per-leg distance are both constant.
  var RUNNER_SPRINT_FT_PER_S = 27;
  // Task 11: +1ft/s per SPD point off the league-average runner, additive
  // rather than spdPaceScale's multiplicative percentage - runnerProfile
  // (below) is the only consumer; RUN_LEG_MS/STEAL_LEG_DUR_MS stay flat
  // league-average fallback constants built off RUNNER_SPRINT_FT_PER_S
  // alone, untouched.
  var RUNNER_SPD_FT_PER_S_PER_POINT = 1.0;
  var RUN_LEG_MS = [0, 1, 2, 3, 4].map(function (legs) {
    return Math.round(legs * BASE_DIST_FT / RUNNER_SPRINT_FT_PER_S * 1000);
  });
  // A put-out token's own animation total (style.css: rnOutToBase/
  // rnOutRetreat) - run to the tag point (RUN_LEG_MS[1]) + turn red (250ms) +
  // walk off (650ms). Kept in sync with that CSS rule's own hardcoded
  // 4233ms/percentages by hand (see its comment) - defined here too so
  // slideDwell's own per-play animation estimate below has a single real
  // number to reference instead of a second hand-copied 4233 literal.
  var OUT_CHOREOGRAPHY_MS = RUN_LEG_MS[1] + 250 + 650;

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

  // Alex's ask, steals only: the DIFF wheel's own pace becomes a function of
  // how close the underlying steal_num/throw_num roll actually was
  // (500-diff - the biggest a circular diff on this mod-1000 scale can ever
  // be, so 500-diff ranges 0..500 same as diff itself, just inverted) - the
  // closer the two numbers, the slower the wheel spins itself out. 1 (CSS's
  // own var(--wheel-pace,1) default, i.e. today's fixed baseline) for a
  // blowout diff (500) or anything that isn't a steal at all; WHEEL_PACE_MIN
  // at a dead-even diff (0). Linear in (500-diff), per Alex's own framing.
  // Consumed two places: wheelHtml (steal DIFF instance only - sets
  // --wheel-pace inline, everything else leaves it unset/1) and
  // sceneFieldHtml (pitchBallHtml's own arrival, already synced to "whenever
  // the wheel finishes" - a slower wheel here means a later pitch arrival,
  // which is what actually gives the runner - who still breaks on the pitch
  // itself, runnerSeqDelay, entirely unaffected by this - Alex's own "a
  // bigger jump on the catcher," for free, no separate mechanism needed).
  var WHEEL_PACE_MIN = 0.5;
  function stealWheelPace(m) {
    var isSteal = m.pitch == null && m.steal_num != null && m.throw_num != null;
    if (!isSteal) return 1;
    var diff = Math.abs(signedCirc(m.steal_num, m.throw_num, 1000));
    return WHEEL_PACE_MIN + (1 - WHEEL_PACE_MIN) * (diff / 500);
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
    // No margin (Alex's call, ideas-and-opinions conversation): the old -12ft
    // buffer hedged against the previous design's forecasting uncertainty (a
    // solved/recomputed distance that could land anywhere near the fence
    // unpredictably). The render target is now a real Statcast distance,
    // already filtered at data-build time to stay under the fence
    // (compute_flight_ranges.py's NON_HR_CLAMP_FT, kept in sync with this
    // value) - so this is now a defensive cap for the rare no-station
    // fallback path, not a routine one.
    return Math.min(D, fenceAt(angleDeg));
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
     physics boundary under a smooth visual one (or vice versa).

     The pin alone wasn't enough (Alex's report): the table's steepest run
     is right there at the wall (270->355->400ft over just a few degrees at
     the 45-50 stretch), so the full 9deg window - built for the gentle
     curves everywhere else, which read fine - was reaching out into that
     taper and pulling near-wall samples noticeably off the raw input. Fix:
     ramp the window's half-width up from 0 at the pin to the full half-
     width a few samples out (binomialKernel), instead of applying one
     fixed-size window to every protected sample. A 0-width window is just
     the raw sample, so the curve hugs the authored JSON right at the wall
     and eases into the same smoothing everyone else already had by the
     time it's a few samples away - no hard edge where the taper ends,
     since window size grows one sample at a time rather than snapping. */
  var BOUNDARY_SMOOTH_HALF = 3;  // ~9deg half-width at the 3deg sample step, once fully tapered in
  function binomialKernel(half) {
    var n = 2 * half;
    var row = [1];
    for (var k = 1; k <= n; k++) row.push(row[k - 1] * (n - k + 1) / k);
    return row;
  }
  var BOUNDARY_SMOOTH_KERNELS = [];
  for (var h = 0; h <= BOUNDARY_SMOOTH_HALF; h++) BOUNDARY_SMOOTH_KERNELS.push(binomialKernel(h));
  function edgeSmooth(values, protectBelowIndex) {
    // values is a plain 0..180deg sweep (offsetDegAbs), not a closed ring -
    // no wraparound, since BOUNDARY_TABLE is itself only ever read via
    // Math.abs(offsetDeg). Indices below protectBelowIndex are copied
    // through untouched; the window for indices at/above it tapers up from
    // 0-width at protectBelowIndex, so it never reaches below that edge
    // (no replication padding needed, unlike the old fixed-window clamp).
    var n = values.length;
    var out = values.slice();
    for (var i = protectBelowIndex; i < n; i++) {
      var half = Math.min(BOUNDARY_SMOOTH_HALF, i - protectBelowIndex);
      var weights = BOUNDARY_SMOOTH_KERNELS[half];
      var wsum = 0;
      for (var w = 0; w < weights.length; w++) wsum += weights[w];
      var acc = 0;
      for (var k = -half; k <= half; k++) {
        var idx = i + k;
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
    return edgeSmooth(raw, protectBelowIndex);
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

  // Real fielders converge on a ball asymmetrically: coming IN (running
  // toward home) is easier/faster than retreating (running away from home,
  // tracking a fly ball over one's shoulder, or chasing a rolling ball
  // that's still moving away) - this used to compare every fielder as if
  // all approaches were equally hard, so a shallow fly or rollout between
  // an infielder and outfielder always split exactly down the middle
  // regardless of which direction each was actually running (Alex's
  // report). `retreatPenalty` scales a retreating fielder's effective
  // distance up before comparing - CATCH_RETREAT_PENALTY/
  // PICKUP_RETREAT_PENALTY below are each anchored to an explicit ground-
  // coverage-share anecdote (Alex's calls: 70/30 RF/2B on a fly ball, 80/20
  // chasing a rolling one), not an arbitrary tuned number: penalty =
  // deeperShare/shallowerShare is exactly the multiplier that makes the two
  // fielders score equal at the real boundary point that share split
  // describes.
  var CATCH_RETREAT_PENALTY = 0.7 / 0.3;    // 70/30 RF/2B ground share on a fly ball
  var PICKUP_RETREAT_PENALTY = 0.8 / 0.2;   // 80/20 RF/2B ground share chasing a rolling ball
  function nearestFielder(x, y, retreatPenalty) {
    var penalty = retreatPenalty || CATCH_RETREAT_PENALTY;
    var ballDist = Math.hypot(x, y);
    var best = null, bestScore = Infinity;
    for (var key in FIELDER_ANCHORS_FT) {
      var a = FIELDER_ANCHORS_FT[key];
      var d = Math.hypot(a.x - x, a.y - y);
      var anchorDist = Math.hypot(a.x, a.y);
      var score = anchorDist >= ballDist ? d : d * penalty;
      if (score < bestScore) { bestScore = score; best = key; }
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

  // Joint EV/LA/distance selection (ideas-and-opinions conversation,
  // physics-redesign successor to launchAngleFor above): band.stations is a
  // table of this result's real (EV, LA, distance) triples ranked by their
  // own real Statcast distance (hit_distance_sc, pre-trimmed to the 5th-95th
  // percentile by compute_flight_ranges.py) - not independently-interpolated
  // marginal ranges, and not an EV solved backward through our own physics
  // to hit a target (both, in turn, could produce - or in the solved-EV
  // case, report - an (EV, LA) pair no real batted ball ever was: verified
  // root cause of sign-flipped diff-vs-distance relationships, clamp-
  // clumping on deep hits, and physically nonsensical readouts like a
  // 130mph EV-solve-ceiling clamp on a real play). q picks a real point's LA
  // *and its own real paired EV and distance* directly off this real data;
  // onTop picks which end of the real LA spread at that distance to land
  // on. distTopped/distUppercut are each that SAME real play's own
  // hit_distance_sc - never a value borrowed from a different real play at
  // the same station rank (an earlier design's bug: la_topped/ev_topped
  // were already a real pair, but the distance they got radially rescaled
  // to came from a different real play that merely shared the station's
  // rank). flightParams below runs the picked real pair through the real
  // drag+lift physics and radially rescales the landing point to that same
  // pair's own real distance, rather than trusting the physics model's own
  // recomputed distance for it (see this function's docstring companion in
  // compute_flight_ranges.py for why those two can legitimately differ).
  //
  // laMin/laIdeal/laMax/evMin/evMax on the band are kept as reference/audit
  // only now (still read by the worked-example tests and human eyeballing)
  // - launchAngleFor above is the fallback for a band with no stations
  // (e.g. a stale cached meta.json), not the primary path.
  function stationsLookup(band, q) {
    var st = band.stations;
    if (!st || !st.length) return null;
    function pick(s) {
      return {
        laTopped: s.laTopped, evTopped: s.evTopped, distTopped: s.distTopped,
        laUppercut: s.laUppercut, evUppercut: s.evUppercut, distUppercut: s.distUppercut,
      };
    }
    var first = st[0], last = st[st.length - 1];
    if (q <= first.q) return pick(first);
    if (q >= last.q) return pick(last);
    for (var i = 0; i < st.length - 1; i++) {
      var a = st[i], b = st[i + 1];
      if (q >= a.q && q <= b.q) {
        var t = (b.q - a.q) > 0 ? (q - a.q) / (b.q - a.q) : 0;
        return {
          laTopped: a.laTopped + (b.laTopped - a.laTopped) * t,
          evTopped: a.evTopped + (b.evTopped - a.evTopped) * t,
          distTopped: a.distTopped + (b.distTopped - a.distTopped) * t,
          laUppercut: a.laUppercut + (b.laUppercut - a.laUppercut) * t,
          evUppercut: a.evUppercut + (b.evUppercut - a.evUppercut) * t,
          distUppercut: a.distUppercut + (b.distUppercut - a.distUppercut) * t,
        };
      }
    }
    return pick(last);
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
    // Re-targets the same real distance flightParams originally picked
    // (flight.targetDist), not just whatever this new angle's raw sim
    // happens to produce - a direction override should still land the ball
    // at the same real Statcast distance for this play, just thrown to a
    // new bearing. Falls back to the raw sim distance (no-op) for a
    // pre-stations flight object that never set targetDist.
    var target = flight.targetDist != null ? flight.targetDist : sim.distance;
    var D = distanceCap({ distance: target, landing: sim.landing }, newAngleDeg, isHomeRun);
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
  // Horizontal-spray bucket classification (gameday reconciliation
  // spray-bucket stage) - mirrors compute_flight_ranges.py's own
  // SPRAY_BUCKETS table exactly (there is no shared source of truth across
  // Python/JS for a table this small; keep both in sync by hand). offsetDeg
  // is degrees off dead center in the SAME convention `angle` below already
  // uses (0-90, 45=center, higher=1B/right side, lower=3B/left side - the
  // same convention OF_CANONICAL_ANGLE/ofShadeDirection use elsewhere in
  // this file), so callers just pass `angle - 45`.
  var SPRAY_BUCKETS = [
    ["3B_LINE", -45.0, -34.5],
    ["LF", -34.5, -19.5],
    ["LF_GAP", -19.5, -7.5],
    ["CF", -7.5, 7.5],
    ["RF_GAP", 7.5, 19.5],
    ["RF", 19.5, 34.5],
    ["1B_LINE", 34.5, 45.0],
  ];
  function classifySprayBucket(offsetDeg) {
    for (var i = 0; i < SPRAY_BUCKETS.length; i++) {
      var b = SPRAY_BUCKETS[i];
      if (offsetDeg >= b[1] && offsetDeg <= b[2]) return b[0];
    }
    return null;
  }

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

    var hand = effectiveHand(play.batter_hand);
    var frac = bucket / 5;
    var angle = hand === "L" ? 45 - frac * 40 : 45 + frac * 40;

    // Joint EV/LA selection (see stationsLookup above): q picks a real,
    // jointly-observed (EV, LA) pair off real Statcast data - not a solved
    // EV, so no risk of a physically implausible readout (e.g. an EV-solve-
    // bracket-ceiling clamp on a real play). onTop picks which end of the
    // real LA spread at that distance to land on. Falls back to the old
    // independent-marginal formula only if a band has no stations (e.g. a
    // stale cached meta.json predating this change).
    //
    // Spray-bucket stations (gameday reconciliation spray-bucket stage):
    // for the results that carry one (2B/3B/1BWH/1BWH2 - band.stationsBySpray,
    // see key_moments_build.py's _flight_meta), read this play's own real
    // spray-conditioned station set instead of the pooled-every-direction
    // one - a double hit right at an outfielder's own position genuinely
    // needs a harder/farther real sample than one down the line. Falls back
    // to the unconditioned band.stations whenever this result has no
    // stationsBySpray at all, or this specific bucket's own real sample was
    // too thin to trust (compute_flight_ranges.py just never wrote that
    // bucket's rows in that case).
    var sprayBucket = classifySprayBucket(angle - 45);
    var stationRows = (band.stationsBySpray && band.stationsBySpray[sprayBucket]) || band.stations;
    var station = stationsLookup({ stations: stationRows }, q);
    var LA, EV;
    if (station) {
      LA = onTop ? station.laTopped : station.laUppercut;
      EV = onTop ? station.evTopped : station.evUppercut;
    } else {
      LA = launchAngleFor(band, q, onTop);
      EV = band.evMin + q * (band.evMax - band.evMin);
    }
    var isHomeRun = result === "HR";

    // phi = HZ - 45 (physics-redesign plan Part 1, verified); the resolved
    // hand goes into the spin formulas unchanged, not folded into phi a
    // second time - the HZ angle above already carries the hand mirror.
    var sim = clampFairTerritory(KMTraj.simulateFlight(EV, LA, angle - 45, hand), band.archetype);

    // Radial scale to the real Statcast distance for this station (see the
    // geometric-radial-scale explanation from the ideas-and-opinions
    // conversation): running this real EV/LA pair through the real physics
    // can legitimately land at a different distance than that same pair's
    // own real hit_distance_sc (ball-to-ball Cd/Cl variance the workbook
    // itself admits, and for anything a fielder stops early, hit_distance_sc
    // was never an uninterrupted-flight distance to begin with) - so the
    // real distance, not the model's own recompute, is what gets rendered.
    // Falls back to the raw simulated distance (no-op scale) when there's no
    // station. distanceCap is handed this real target (not the raw simulated
    // one) so the "scale to the real distance" and "clamp to the fence/field
    // boundary, if needed" steps collapse into the single `scale` factor
    // already applied below - the same pattern the fence-clamp-only path
    // used before this change. scaleSamples only ever touches x/y -
    // contactVel stays the real, unscaled physics velocity, so downstream
    // rollout/pickup (resolveHitPickup) reads the real exit speed regardless
    // of either scale step.
    var targetDist = station ? (onTop ? station.distTopped : station.distUppercut) : sim.distance;
    var D = distanceCap({ distance: targetDist, landing: sim.landing }, angle, isHomeRun);
    var scale = D / sim.distance;

    return {
      la: LA, ev: EV, distance: D, angle: angle, targetDist: targetDist,
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
  // Alex's ask, after the Avant play (session 4, POR@RLY, top 3 - a
  // comebacker landing 2ft out that the old model let roll a full 3.9 real
  // seconds before "fielding" it, well past the batter's own sprint to
  // first): a ball that dies before reaching its assigned fielder's own
  // positioned depth doesn't just sit there waiting to be picked up at rest -
  // a real infielder charges in and grabs it while it's still moving. Real
  // MLB "arm strength" scouting speeds run faster than this, but that's a
  // full-extension crow-hop throw, not a barehand-charge speed - closing on
  // a moving ball to field it cleanly (sometimes bare-handed) is a
  // deliberately controlled sprint, a notch under a runner's own
  // RUNNER_SPRINT_FT_PER_S(27) top-speed line-drive-to-the-gap sprint.
  // Tuned down from an original 22 (Alex's ask, after checking a ~100mph
  // comebacker still nibbled ~16ft/~0.1s off a fielder camped at depth - not
  // wrong, exactly, since the ball IS still decelerating right up to the
  // fielder's spot, but 22 read as too close to a flat-out sprint for what's
  // supposed to be a controlled, glove-down-ready approach). At 16, a hard
  // ~100mph shot's own nibble shrinks to ~12ft/~0.08s while a genuine slow
  // roller (session 2, BBEG@POR, Trotter's comebacker) keeps the bulk of its
  // own real fix (1.11s of the original 1.28s) - see chargeInIntercept's own
  // comment for why the fast-ball tail can never reach exactly zero.
  // Per-player speed (Alex's ask): a 1-5 scouted SPD rating (MLN Calculator
  // convention, 3 = average), straight off the Players/Rosters sheet via
  // key_moments_build.py's defense/batter_spd/runners_on_base fields.
  // Scales a flat speed constant by a percentage per point above/below
  // average - SPD_PCT_PER_POINT is a placeholder (Alex is grounding real
  // ranges in Statcast data separately); a SPD-3 (or unresolved) player
  // reproduces today's existing flat constant exactly, so nothing changes
  // until that real value replaces the placeholder.
  var SPD_AVERAGE = 3;
  var SPD_PCT_PER_POINT = 0.12;
  function spdPaceScale(spd) {
    var v = (spd == null) ? SPD_AVERAGE : spd;
    return 1 + (v - SPD_AVERAGE) * SPD_PCT_PER_POINT;
  }
  // The specific fielder standing at `pos` for this play, from m.defense
  // (key_moments_build.py's per-position [name, last, spd]). Pitcher is a
  // deliberate exception (Alex's ask): fielding pace always uses the
  // average rating regardless of their own real SPD, since that attribute
  // is scouted for baserunning, not pitcher fielding quickness.
  function fielderSpd(m, pos) {
    if (pos === "P") return SPD_AVERAGE;
    var entry = m && m.defense && m.defense[pos];
    var spd = entry && entry[2];
    return (spd == null) ? SPD_AVERAGE : spd;
  }
  // The specific runner's own SPD rating (gameday reconciliation plan,
  // Task 3.2) - same [name, last, spd] shape/fallback convention as
  // fielderSpd, just off m.batter_spd (a bare number, not an entry) for the
  // batter-runner and m.runners_on_base[who][2] for anyone already on base.
  // Historical archives built before the spd fields existed (or a runner
  // key key_moments_build.py never resolved) fall back to SPD_AVERAGE,
  // reproducing today's flat league-average timing exactly.
  function runnerSpd(m, who) {
    if (who === "BATTER") {
      var bs = m && m.batter_spd;
      return (bs == null) ? SPD_AVERAGE : bs;
    }
    var entry = m && m.runners_on_base && m.runners_on_base[who];
    var spd = entry && entry[2];
    return (spd == null) ? SPD_AVERAGE : spd;
  }

  var FIELDER_CHARGE_FT_PER_S = 16;
  // Split out from FIELDER_CHARGE_FT_PER_S (Alex's ask, following through on
  // OF_CHARGE_CANDIDATE_POSITIONS's own long-standing "split into its own
  // constant later if wrong" flag below): closing on a base hit from a real
  // outfield distance is a genuine run, not the same delicate short-range
  // "barehand a comebacker" scoop the 16ft/s infield number was tuned for.
  // Real report that surfaced it: a routine single (76mph EV, 18deg LA,
  // landing 207ft out) took 5.6 real seconds of ground time after landing
  // before the assigned RF ever caught up to it at 16ft/s - the ball's own
  // roll was simply outrunning that pace for almost its entire remaining
  // distance, so the race fell through to "wait for it to nearly stop"
  // instead of a real fielder-closing chase. Set close to a real sprint
  // (RUNNER_SPRINT_FT_PER_S) rather than reusing the infield number.
  var OF_CHARGE_FT_PER_S = 24;
  // Recognize-it's-a-roller-and-break beat, same flavor as every other
  // "notice, then react" constant here (OUT_BEAT_MS, THROW_DELAY_MS) - just
  // its own value since a charge decision is a different kind of read than
  // "the ball's already in my glove, throw now."
  var CHARGE_REACTION_S = 0.15;
  // Every infielder who could plausibly be the one who actually gets to a
  // short roller - LF/CF/RF never win this (they're hundreds of feet away,
  // the race below prices that in for free without needing an explicit
  // exclusion), so there's no point racing them.
  var CHARGE_CANDIDATE_POSITIONS = ["P", "C", "1B", "2B", "3B", "SS"];
  // Outfielders racing a single (resolveSinglePickup, below) - the same
  // charge-in race, just the other three positions. Reuses
  // FIELDER_CHARGE_FT_PER_S/CHARGE_REACTION_S unchanged (a fielder closing
  // on a single to hold the batter is the same "controlled charge, glove
  // down, come up ready to throw" action as an infielder's, not a flat-out
  // sprint) - split into its own constant later if that assumption turns
  // out wrong for the longer outfield closing distances.
  var OF_CHARGE_CANDIDATE_POSITIONS = ["LF", "CF", "RF"];
  // CHARGE_REACTION_S only applies once a fielder actually has to move
  // (distFt>0) - a fielder whose own position sits exactly on the ball's
  // path (the camped/naive candidate's own canonical depth, most commonly)
  // doesn't need to "recognize and break" toward something arriving right
  // at him; he's already there, glove down (Alex's ask). Without this, a
  // hard-hit ball could scream past a fielder's own standing spot before
  // his 0.15s reaction alone was up, sending the race chasing the ball's
  // full, un-intercepted natural roll instead - real report: a synthetic
  // 65mph "grounder" the camped 2B was standing right on top of still
  // ended up "fielded" 130ft past his own depth, because the race never
  // credited him with blocking it as it arrived.
  function chargeFielderArriveS(anchor, flight, alongFt, ftPerS) {
    var p = groundDirPoint(flight, alongFt);
    var distFt = Math.hypot(p.x - anchor.x, p.y - anchor.y);
    // Re-based on the shared primitive (Task 3.1): the charge race now
    // accelerates instead of running at a flat top speed - a deliberate
    // behavior change (slightly lengthens short charges, more honest to
    // real fielder motion). accelFtPerS2 defaults to FIELDER_ACCEL_FT_S2
    // via arrivalTimeS/accelTimeS.
    return arrivalTimeS(distFt, {
      topSpeedFtPerS: ftPerS || FIELDER_CHARGE_FT_PER_S,
      accelFtPerS2: FIELDER_ACCEL_FT_S2,
      reactionS: CHARGE_REACTION_S
    });
  }
  // Earliest point along the ball's own roll (0..gp.restFt) where this one
  // fielder's own charge-in catches up to it, real seconds since contact for
  // both. h(alongFt) = this fielder's own arrival time there minus the
  // ball's own arrival time there - starts positive (nobody's already
  // standing on the landing spot) and, for a fielder charging toward where
  // the ball is actually headed, eventually goes non-positive as the gap
  // closes faster than the ball can open it back up. A coarse forward scan
  // finds the first interval where that happens (charge distance-to-ball
  // isn't guaranteed monotonic - a fielder can be closing, pass their own
  // closest approach, then start losing ground again as the ball keeps
  // rolling past them - so this deliberately isn't a plain bisection
  // assuming one clean crossing), then a short bisection refines within
  // that interval. If the fielder never gets there while the ball's still
  // moving (a comebacker rolling AWAY from a catcher's own behind-the-plate
  // anchor, say - see the Avant play), the ball just sits at rest waiting
  // for them - the fielder's own travel time to that dead-ball spot is what
  // actually gates the play at that point, not the ball's (already-elapsed)
  // arrival there.
  // maxAlongFt (optional): caps how far along the roll this race even looks,
  // below the ball's own natural friction-decay rest point (gp.restFt) -
  // used for bunt/infield_single (resolveGrounderInterception's own
  // STAYS_IN_INFIELD_ARCHETYPES check), whose ball is never allowed to
  // reach the outfield grass regardless of whether any fielder actually
  // gets to it in time. Every other archetype passes null and races the
  // ball's own real rest point unchanged.
  // knownAlongFt (optional): the one along-distance we have an ANALYTIC
  // reason to check directly, rather than trust the coarse grid below to
  // land near - chargeInIntercept passes the camped candidate's own
  // campedAlongFt here, since that's exactly where distFt (in
  // chargeFielderArriveS) hits zero and h() can dip into a window narrower
  // than the grid's own step size. Real bug report: a comebacker's camped
  // pitcher crossed at 35.6ft (h(35.6)=-0.12) but the 40-step grid over a
  // restFt of 225ft steps in ~5.6ft jumps - both samples straddling 35.6ft
  // (33.7ft, 39.3ft) came back barely POSITIVE, so the scan never saw a
  // crossing at all and fell through to "nobody intercepted," comparing
  // every fielder's distance to the ball's full, un-intercepted ~225ft
  // natural roll instead - a wildly wrong "who fields it" answer for what
  // should have been an instant comebacker out.
  function fielderInterceptS(anchor, flight, gp, maxAlongFt, knownAlongFt, ftPerS) {
    var restFt = maxAlongFt != null ? Math.min(maxAlongFt, gp.restFt) : gp.restFt;
    function h(alongFt) { return chargeFielderArriveS(anchor, flight, alongFt, ftPerS) - gp.timeAt(alongFt); }
    if (h(0) <= 0) return { alongFt: 0, atS: gp.timeAt(0) };
    if (knownAlongFt != null && knownAlongFt > 0 && knownAlongFt <= restFt && h(knownAlongFt) <= 0) {
      var lo0 = 0, hi0 = knownAlongFt;
      for (var k = 0; k < 20; k++) {
        var mid0 = (lo0 + hi0) / 2;
        if (h(mid0) > 0) lo0 = mid0; else hi0 = mid0;
      }
      var found0 = (lo0 + hi0) / 2;
      return { alongFt: found0, atS: gp.timeAt(found0) };
    }
    var STEPS = 40;
    var prevAlong = 0;
    for (var i = 1; i <= STEPS; i++) {
      var along = restFt * i / STEPS;
      if (h(along) <= 0) {
        var lo = prevAlong, hi = along;
        for (var j = 0; j < 20; j++) {
          var mid = (lo + hi) / 2;
          if (h(mid) > 0) lo = mid; else hi = mid;
        }
        var alongFt = (lo + hi) / 2;
        return { alongFt: alongFt, atS: gp.timeAt(alongFt) };
      }
      prevAlong = along;
    }
    return { alongFt: restFt, atS: chargeFielderArriveS(anchor, flight, restFt, ftPerS) };
  }
  // Races every plausible infielder's own charge-in (above) and returns
  // whichever actually gets there first - "who fields it" and "how long does
  // it take" are the same question for a short roller like this, not two
  // separate lookups (Alex's ask). A ball hit right at one fielder's own
  // anchor still correctly comes back as that same fielder; this only
  // changes the answer when someone else genuinely gets there sooner (a
  // comebacker up the middle, fielded by the pitcher rather than the
  // catcher chasing it in from behind the plate, say).
  //
  // campedPos/campedAlongFt (Alex's ask, session 5, after the Trotter play -
  // session 2, BBEG@POR - showed a ball that DOES reach its assigned
  // fielder's own depth while still rolling, just barely and just slowly,
  // 2.8 real seconds to cover the last 124ft, with the old model having that
  // fielder plant and wait the whole time): resolveGrounderInterception's
  // own assigned position - the one HZ_FIELDER_BY_ANGLE already re-projected
  // onto the ball's real line at their canonical depth, same "gets in front
  // of it" assumption that position's ordinary at-rat depth already carries
  // - races from that projected depth point instead of its true
  // FIELDER_ANCHORS_FT spot, same as every other candidate here. That's the
  // one exception: every other candidate still races from where they
  // actually stand. A fast, hard-hit ball still resolves to "wait at depth"
  // in practice (nobody, including the camped fielder's own charge-in, can
  // out-race a fielder already standing on the line while the ball's still
  // carrying real pace) - h(campedAlongFt) is where that comparison starts,
  // and it's only very close to campedAlongFt itself that the ball's own
  // instantaneous speed has decayed down near FIELDER_CHARGE_FT_PER_S, so
  // the earliest a crossing can appear is right near the end of the roll
  // regardless of the ball's own opening speed. That's also why this can
  // never land on EXACTLY zero savings for a hard-hit ball reaching its
  // fielder while still moving at all - the camped anchor's own "distance 0,
  // reaction beat only" comparison at campedAlongFt itself is always
  // slightly favorable, just by a shrinking margin as the ball's own pace at
  // that depth climbs (FIELDER_CHARGE_FT_PER_S's own tuning already prices
  // this in - Alex's call, after checking real numbers, that this shrinking
  // margin is fine as-is rather than adding a separate minimum-gain gate).
  // candidates (optional): defaults to the infield roster
  // (CHARGE_CANDIDATE_POSITIONS) - resolveSinglePickup passes
  // OF_CHARGE_CANDIDATE_POSITIONS instead to race outfielders on a single.
  function chargeInIntercept(flight, gp, campedPos, campedAlongFt, maxAlongFt, candidates, m) {
    var best = null;
    (candidates || CHARGE_CANDIDATE_POSITIONS).forEach(function (pos) {
      var isCamped = pos === campedPos && campedAlongFt != null;
      var anchor = isCamped ? groundDirPoint(flight, campedAlongFt) : FIELDER_ANCHORS_FT[pos];
      if (!anchor) return;
      // Real per-player charge speed (Alex's ask) - each candidate races at
      // their own scouted pace, not one flat number for the whole infield -
      // off the position-appropriate base pace (OF_CHARGE_FT_PER_S for the
      // three outfield candidates racing a single, FIELDER_CHARGE_FT_PER_S
      // for everyone else).
      var basePace = OUTFIELD_POSITIONS[pos] ? OF_CHARGE_FT_PER_S : FIELDER_CHARGE_FT_PER_S;
      var ftPerS = basePace * spdPaceScale(fielderSpd(m, pos));
      // fielderInterceptS's own knownAlongFt: only the camped candidate has
      // one exact along-distance we already know to check directly
      // (distFt=0 there) - every other candidate's real static anchor has
      // no such analytically-known point, so it just races the coarse grid.
      var result = fielderInterceptS(anchor, flight, gp, maxAlongFt, isCamped ? campedAlongFt : null, ftPerS);
      if (!best || result.atS < best.atS) best = { pos: pos, alongFt: result.alongFt, atS: result.atS };
    });
    return best;
  }

  // Pitcher only fields the dead-middle bucket (angle 45) up through a
  // controlled-pace comebacker - a genuinely hard-hit ball up the middle is
  // out of a pitcher's real reaction window (Alex's ask, 80mph EV cutoff),
  // so above it the ball is divvied up between the middle infielders
  // instead. Handedness decides which one: a RHH's natural tail carries a
  // hard grounder up the middle toward the 2B side, a LHH's toward the SS
  // side. Checked before the BRC-exclusion override below so an explicit
  // import_BRC.csv default_position still wins either way.
  var PITCHER_MIDDLE_EV_MAX_MPH = 80;
  // Every other infield candidate (including P) still races from their own
  // real anchor below (chargeInIntercept/CHARGE_CANDIDATE_POSITIONS) - the
  // pitcher's real anchor sits closest to home of anyone, so on a short,
  // shallow ball P can still win that race on pure geometry even after
  // being reassigned away from the nominal HZ answer above (a real bug
  // report: a 99mph comebacker with a 17ft first-bounce distance still
  // resolved to P, because nothing had actually removed them from the
  // race). This isn't a "who's standing closest" question in real baseball
  // once a ball's moving this hard up the middle - it's genuinely too fast
  // for the pitcher to get a glove up safely, full stop - so the EV
  // reassignment has to also pull P out of the candidate pool entirely,
  // not just out of the nominal/camped slot.
  var CHARGE_CANDIDATES_NO_PITCHER = CHARGE_CANDIDATE_POSITIONS.filter(function (p) { return p !== "P"; });
  function resolveGrounderInterception(m, flight, hand) {
    var hzPos = HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];
    var pitcherExcludedByEv = false;
    if (hzPos === "P" && flight.ev > PITCHER_MIDDLE_EV_MAX_MPH) {
      hzPos = hand === "L" ? "SS" : "2B";
      pitcherExcludedByEv = true;
    }
    var pos = hzPos;
    if (brcExcludes(m, hzPos) && m.default_position) {
      pos = m.default_position;
      applyAngleOverride(flight, MIN_ANGLE_FOR_POS[pos], hand, false);
    }
    var gp = KMTraj.groundPath(Math.hypot(flight.contactVel.vx, flight.contactVel.vy), flight.contactVel.vz);
    // INFIELDER_DEPTH_FT has no "C" entry (a catcher has no real "standard
    // depth" the way an infielder does - they start at the plate) - explicit
    // null check rather than leaning on `undefined - flight.distance` being
    // NaN and failing both comparisons below by accident, same outcome
    // (straight to the charge-in race) but readable as a deliberate case.
    var depth = INFIELDER_DEPTH_FT[pos];
    var alongFt = depth != null ? depth - flight.distance : null;
    // A bunt/infield_single is never allowed to reach the outfield grass
    // (Alex's ask: don't cluster IF1B at the dirt edge - let the fielder
    // genuinely race it instead, same charge-in system as a grounder out,
    // just capped so a ball nobody reaches in time still dies on the dirt
    // rather than continuing to roll). Every other archetype races the
    // ball's own real rest point uncapped, same as before this change.
    var maxAlongFt = STAYS_IN_INFIELD_ARCHETYPES[flight.archetype]
      ? Math.max(0, dirtEdgeFt(flight.angle) - flight.distance)
      : null;
    var fieldedFt, groundTimeS;
    if (alongFt != null && alongFt <= 0) {
      fieldedFt = 0; groundTimeS = 0;
    } else {
      // Alex's ask (session 5, after the Trotter play showed the old
      // "reaches depth while still rolling -> just wait" branch letting a
      // fielder plant and watch a slow chopper take 2.8 real seconds to
      // arrive): there's no meaningful line anymore between "doesn't reach
      // the fielder's own depth" and "reaches it, but slowly" - both are the
      // same question, does charging in beat standing still, so both go
      // through the one race. chargeInIntercept may still hand the play to
      // a different, faster-converging fielder than `pos` - correct, not
      // just for timing but for fieldingNotation's own scorecard credit
      // too, which reads flight.fielder same as everything else here. This
      // is also now the ONLY path for a bunt/infield_single HIT (not just
      // an out) - dispatch (playFieldingNotation/playSceneHtml) no longer
      // gates on wasOut for a ground archetype, so an infield single races
      // the same way a comparable groundout does; nobody winning the race
      // in time before maxAlongFt is exactly what "safe" already means.
      var candidates = pitcherExcludedByEv ? CHARGE_CANDIDATES_NO_PITCHER : null;
      var intercept = chargeInIntercept(flight, gp, pos, alongFt, maxAlongFt, candidates, m);
      pos = intercept.pos;
      fieldedFt = intercept.alongFt;
      groundTimeS = intercept.atS;
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

  /* Outfielders charging a single (Alex's ask): the same charge-in race
     resolveGrounderInterception runs for infielders, just raced against
     OF_CHARGE_CANDIDATE_POSITIONS instead - so a single is fielded wherever
     a real outfielder's own starting depth/speed actually gets them, not
     left to roll toward the fence on pure physics the way resolveHitPickup
     (still used for double/triple/HR) does. Deliberately scoped to archetype
     "single" only, not every hit archetype: a double/triple/HR needs the
     ball to genuinely get past the nearest fielder to make sense against
     its own already-locked MLN result - racing those the same way would
     read as "the outfielder cut it off" on a play that's supposed to go for
     extra bases, which doesn't track with what actually happened. No
     ceiling on the race the way resolveGrounderInterception's maxAlongFt
     is for bunt/infield_single - nothing here needs to force the ball to
     stay in any particular zone, so a fielder who's genuinely slow to it
     just fields it wherever gp.restFt (or fielderInterceptS's own fallback
     to that point) naturally is, same as an unraced hit always has.
     BRC-exclusion override mirrors applyAirPositionOverride exactly (same
     OF_ANGLE_THIRDS, same clamp) - checked against flight.fielder as
     flightParams originally set it (nearest-anchor to the raw landing
     point), not a separately recomputed nominal position. */
  function resolveSinglePickup(m, flight, hand) {
    if (brcExcludes(m, flight.fielder) && m.default_position) {
      var third = OF_ANGLE_THIRDS[m.default_position];
      if (third) applyAngleOverride(flight, clamp(flight.angle, third[0], third[1]), hand, false);
    }
    var gp = KMTraj.groundPath(Math.hypot(flight.contactVel.vx, flight.contactVel.vy), flight.contactVel.vz);
    var intercept = chargeInIntercept(flight, gp, null, null, null, OF_CHARGE_CANDIDATE_POSITIONS, m);
    // intercept.atS already correctly times rawPickupFt in both of
    // fielderInterceptS's own cases - either the ball's own gp.timeAt at the
    // crossing point (the ball and the charging fielder arrive together by
    // construction there), or the fielder's own later charge time to the
    // ball's natural rest point when nobody catches up to it in time. Only
    // recompute (via gp.timeAt, the ball's own natural time to the shorter
    // point) when one of the two safety ceilings below actually shortens
    // rawPickupFt - reusing intercept.atS for a capped, shorter point would
    // understate how long fielding it actually took.
    var rawPickupFt = intercept ? intercept.alongFt : 0;
    var maxReachFt = fenceAt(flight.angle) - 2;
    var pickupFt = Math.max(0, Math.min(rawPickupFt, maxReachFt - flight.distance));
    pickupFt = capRollToBoundary(flight, pickupFt);
    var groundTimeS = intercept ? intercept.atS : 0;
    if (pickupFt !== rawPickupFt) {
      groundTimeS = gp.timeAt(pickupFt) != null ? gp.timeAt(pickupFt) : groundTimeS;
    }
    flight.fielder = intercept ? intercept.pos : flight.fielder;
    flight.fieldedDistFt = flight.distance + pickupFt;
    flight.groundTimeS = groundTimeS;
    flight.groundPath = gp;
  }

  // ── Ball flight rendering (ball-flight-plan.md Stage 4) ───────────────────
  // Real-time throughout (Alex's call, ideas-and-opinions conversation):
  // every distance/speed-derived timing below - ball flight, ground time,
  // base-running, throws - now plays at its own true real-world duration
  // rather than a stylized "feel" compression. Pure reaction-time/margin
  // beats (RUNNER_LEAD_MS, TAG_UP_MS, OUT_BEAT_MS, THROW_DELAY_MS,
  // THROW_STAGGER_MS, MARGIN_POLICY) are deliberately left
  // alone - there's no real-world "speed" to derive a tag-up reaction or a
  // safety margin from, only a real distance/speed pair. slideDwell is now
  // computed per-play from the real result (see its own comment) rather than
  // assuming every play fits inside one fixed budget, which no longer holds
  // once a home run trot alone can run 13+ real seconds.
  var ANIM_TIME_SCALE = 1.0;
  var RUNNER_LEAD_MS = 150;         // runners begin this long after slide mount, behind the ball
  var OUT_BEAT_MS = 400;            // "outs choreography begins" beat, on top of ball travel if any
  // Alex's ask: the DIFF/HZ wheels should finish their own animation before
  // the field's own choreography (ball flight, throws, runner tokens) even
  // starts moving - the two used to run in parallel, competing for
  // attention on the very first beat of the slide. Matches the wheels'
  // OWN worst-case timeline exactly (style.css: .wheel-dot-2/.wheel-val-2
  // fire at a 650ms delay, wheelDotIn runs another 160ms - 650+160=810;
  // both wheels share this timeline, so one number covers whichever/both
  // are showing). Applied uniformly to every play (not just ones with a
  // wheel) - a play that has no wheel at all (Balk is the one real case)
  // still gets the same short beat before its field starts, which is far
  // simpler and more predictable than threading a per-play "was there
  // actually a wheel to wait for" flag through every render call below,
  // and reads as a deliberate pause either way. Applied at the exact point
  // each animation's OWN delay is finally written into a --delay/--rdelay/
  // --fdelay/--blight/--sflash CSS value - never baked into the shared
  // runDelay/outDelay/catchMs/etc. locals those writes are computed from,
  // so every existing race (throw-vs-runner, catch-vs-tag-up, ...) keeps
  // exactly the same relative margin it already had; this only pushes the
  // whole picture later, uniformly. slideDwell adds this same amount back
  // to its own budget below, so auto-advance still leaves the same real
  // reading time on top it always has.
  var FIELD_SEQUENCE_DELAY_MS = 810;
  // Throw leaves almost as the ball is fielded (was 150) so a grounder's
  // throw beats the runner to the bag (refinements plan A4/F10).
  var THROW_DELAY_MS = 60;          // throw draws in this long after the ball is fielded
  // Real average MLB infield throw speed (Alex's call, ideas-and-opinions
  // conversation). THROW_DRAW_MS (over BASE_DIAG_FT, 127.3ft, the real
  // cross-diamond distance e.g. SS/3B to 1B) is the FALLBACK duration for
  // when a throw's real endpoints aren't both known - every throw with a
  // resolvable real distance (throwDrawMsForFt/throwDistFt, below) draws at
  // this same mph but over its OWN actual feet, so a short relay flip draws
  // faster than a full corner-to-first throw instead of taking the same
  // time as one (Alex's ask - this was the deliberate scope boundary the
  // old comment here flagged as "a bigger follow-up if it's worth the
  // reach"; making sequential relay throws fit their own time budget is
  // what made it worth the reach).
  var THROW_SPEED_MPH = 90;
  var THROW_DRAW_MS = Math.round(BASE_DIAG_FT / (THROW_SPEED_MPH * 1.46667) * 1000);
  // Task 9.3 (fact 4): per-position throw speed - Alex's supplied numbers,
  // verbatim. min/max are the realistic floor/ceiling the reconciler's
  // slowThrow knob (Task 9.4) solves within; mph is the natural/default
  // speed used everywhere else. No per-player arm data exists, so this is
  // position-only, same granularity fielderProfile's own top speeds use.
  var THROW_SPEED_BY_POS = {
    P: { mph: 85, min: 80, max: 90 }, C: { mph: 80, min: 75, max: 90 },
    "1B": { mph: 80, min: 70, max: 85 }, "2B": { mph: 80, min: 70, max: 85 },
    "3B": { mph: 85, min: 80, max: 90 }, SS: { mph: 85, min: 80, max: 90 },
    LF: { mph: 87, min: 75, max: 95 }, CF: { mph: 90, min: 75, max: 95 },
    RF: { mph: 90, min: 75, max: 95 },
  };
  // mph (optional): defaults to THROW_SPEED_MPH, the unknown-thrower
  // fallback and THROW_DRAW_MS's own flat-fallback basis - unchanged for
  // every call site that doesn't know (or care) who's throwing.
  function throwDrawMsForFt(distFt, mph) {
    return Math.max(1, Math.round(distFt / ((mph || THROW_SPEED_MPH) * 1.46667) * 1000));
  }
  // Alex's ask: a pitch animation ahead of every play (Balk excepted - there
  // was never an actual pitch on one). Real representative MLB pitch speed,
  // same "real distance over a real mph" model as THROW_DRAW_MS above, just
  // over the fixed mound-to-plate distance (always PITCHER_MOUND_FT - unlike
  // a throw this one never varies by fielder position).
  var PITCH_SPEED_MPH = 88;
  var PITCH_TRAVEL_MS = Math.round(PITCHER_MOUND_FT / (PITCH_SPEED_MPH * 1.46667) * 1000);
  // Straight-line real feet from a real {x,y} origin to a named base, or null
  // when the origin isn't known (e.g. ball_flight_test.py's minimal A4
  // timing-race flight objects, built without a resolved fielder position) -
  // callers fall back to the flat THROW_DRAW_MS/BASE_DIAG_FT model in that
  // case, exactly the old always-BASE_DIAG_FT behavior.
  function throwDistFt(fromPt, toBase) {
    var to = BASE_POS_FT[toBase];
    if (!to || !fromPt || !isFinite(fromPt.x) || !isFinite(fromPt.y)) return null;
    return Math.hypot(to.x - fromPt.x, to.y - fromPt.y);
  }
  // A relay "leg" the same fielder covers themselves (an unassisted putout -
  // e.g. a 3B fielding a bunt with the force at third, stepping on the bag
  // himself before throwing on to first for a 5-3 DP) is a jog, not a
  // throw - Alex's ask: it should draw at a runner's pace, not 90mph.
  // Mirrors throwDrawMsForFt exactly, just off RUNNER_SPRINT_FT_PER_S.
  function runnerDrawMsForFt(distFt) {
    return Math.max(1, Math.round(arrivalTimeS(distFt,
      { topSpeedFtPerS: RUNNER_SPRINT_FT_PER_S, accelFtPerS2: Infinity, reactionS: 0 }) * 1000));
  }
  // Per-target whether outThrowTargets' leg i is that same "fielder covers
  // it himself" case, in chain order - the exact coverage rule
  // fieldingChainDetail already uses to collapse the notation (a repeated
  // position touching the ball twice in a row is one unassisted play, not a
  // throw to himself), just kept per-leg here instead of collapsed, since
  // throwSchedule still needs real geometry/timing for every leg whether
  // it's a throw or a jog. Defaults every leg to "assisted" (a real throw)
  // if the fielder itself somehow isn't resolved - the same conservative
  // fallback throwDistFt's own null case already uses elsewhere.
  function relayLegIsUnassisted(m, targets, flight) {
    if (!flight.fielder) return targets.map(function () { return false; });
    var relayBaseCount = baseLegs(targets).length;
    var prevPos = flight.fielder;
    return targets.map(function (leg) {
      // Task 8.2: compare against the leg's own resolved position for
      // both kinds - a position/cutoff leg names its receiver directly
      // (coveringPosition bypassed), a base leg still resolves through the
      // coverage convention.
      var pos = leg.kind === "pos" ? leg.pos
        : coveringPosition(leg.base, flight.archetype, flight.angle, flight.fielder, relayBaseCount, m, flight);
      var unassisted = pos === prevPos;
      prevPos = pos;
      return unassisted;
    });
  }
  // Catch-and-transfer beat before a relay throw leaves, on a multi-throw
  // play (a DP's relay) - Alex's ask: the second throw must not start
  // drawing until the first one has actually landed (was a flat i*this
  // offset from every throw's own start, so a relay's throws overlapped
  // and read as simultaneous instead of thrown/caught/thrown - see
  // throwSchedule, which now chains start[i] off end[i-1] instead).
  var THROW_STAGGER_MS = 50;
  // One id per rendered throw line's reveal clip-path (throwLineHtml) - just
  // needs to be unique within the DOM at any moment, not stable/meaningful.
  var THROW_CLIP_SEQ = 0;
  var TAG_UP_MS = 80;               // a tagging runner leaves this long after the catch (B5)
  // Stylized walk off the field after being put out, same "no real-world
  // distance to derive from" duration out-walk's own choreography already
  // used (its old fixed 650ms shape) - now shared by every out choreography
  // via runnerOutMotionHtml, so a token's walk to the dugout always starts
  // the instant they're actually out (Alex's ask) and takes this long to
  // get there, regardless of which shape put them out.
  var RN_OUT_WALK_MS = 650;

  // ── Verdict + margin policy (gameday reconciliation plan, Task 4.3) ───────
  // "How close should this look" - one table replacing every scattered
  // margin constant (the old flat THROW_LEAD_MS/TAG_THROW_MARGIN_MS/
  // IF1B_THROW_MARGIN_MS/STEAL_THROW_MARGIN_MIN/MAX_MS). Four closeness
  // classes, one per kind of contested event:
  //   forceOut      - throw must beat runner        (GO, DP legs, FC family)
  //   tagOut        - throw must beat runner        (CS family - caught
  //                   stealing; a tag, not a force, but the same timing
  //                   contract)
  //   contestedSafe - throw must LOSE to runner      (infield single, safe
  //                   steal)
  //   uncontested   - throw is decorative, runner comfortably safe
  //                   (SacF/DSacF/FO's "throw home anyway")
  // minMs/maxMs values are the old constants' own values, carried straight
  // through (TAG_THROW_MARGIN_MS's 200->400 history, STEAL_THROW_MARGIN_*) -
  // only their scatter across five different constants dies, not the
  // numbers themselves.
  var MARGIN_POLICY = {
    forceOut:      { minMs: 150, maxMs: 450 },
    tagOut:        { minMs: 150, maxMs: 450 },
    contestedSafe: { minMs: 150, maxMs: 450 },
    uncontested:   { minMs: 400, maxMs: 600 },
  };
  // diff is the league's own decisiveness number for this play - m.diff
  // (pitch/swing) for a batted play, the steal_num/throw_num diff for a
  // steal - scaled linearly across the class's [minMs,maxMs] range exactly
  // as stealThrowMarginMs already did (that function is this one,
  // generalized by class). A decisive roll reads as a decisive play; a
  // near-tie reads bang-bang. Acknowledged caveat (Alex, plan 4.3's own
  // note): m.diff also drives EV/LA upstream, so it's a proxy for play
  // closeness, not a measured bag margin - but it's the only league-native
  // number with the right shape, and it beats a hand-authored per-result
  // table on both consistency and provenance. Missing diff (a test/synthetic
  // moment) falls back to the range's own midpoint diff (250) rather than
  // either extreme.
  function targetMarginMs(cls, diff) {
    var p = MARGIN_POLICY[cls];
    var d = diff == null ? 250 : diff;
    return p.minMs + (p.maxMs - p.minMs) * (d / 500);
  }
  // Bounds for the reconciler's own "throw must land earlier" knob chain
  // (Task 4.4) - how far each named adjustment may go before the next one
  // in line has to engage.
  // A held/double-clutched release is always plausible regardless of
  // duration (unlike a runner's or fielder's own speed, holding a ball has
  // no real physical bound) - a generous sanity ceiling only, not a real
  // constraint. Must comfortably exceed the slowest legitimate uncontested
  // wait (a runner circling the bases on a deep sac fly - up to ~13s real
  // time elsewhere in this file's own worst-case estimate), which the old
  // tagStart backward-solve this replaces never bounded at all.
  var HOLD_RELEASE_MAX_MS = 20000;
  var RUNNER_LATE_JUMP_MAX_MS = 400;   // a genuinely late read off contact is real baseball
  var STRETCH_RUNNER_MAX_FRAC = 0.15;  // last resort - crosses goal (b), bounded to 15% slower

  // holdRelease's own mechanism, standalone: shift a whole throw chain later
  // so its final leg lands at/after requiredEndMs - every later leg already
  // chains its own start off the one before it (sequentialThrowSchedule), so
  // raising leg 0's start carries every leg with it. Never pulls a schedule
  // earlier (returns null, no-op) - a pure floor. Shared by the reconciler's
  // own "throw too early" direction and by throwSchedule's separate
  // pitcher-cover-1B floor (never let the ball visibly beat the covering
  // fielder's own token there) - two different REASONS to hold a release,
  // one mechanism, both bounded/named the same way.
  function holdChainTo(schedule, requiredEndMs, knob, reason) {
    if (!schedule.length || requiredEndMs == null) return null;
    var last = schedule[schedule.length - 1];
    if (requiredEndMs <= last.endMs) return null;
    var hold = Math.min(requiredEndMs - last.endMs, HOLD_RELEASE_MAX_MS);
    if (hold < 1) return null;
    schedule.forEach(function (t) { t.startMs += hold; t.endMs += hold; });
    return { knob: knob, who: last.base, ms: Math.round(hold), reason: reason };
  }

  /* The reconciler (Task 4.4) - closes the gap between an honestly forward-
     computed throw schedule and what the verdict requires, through a fixed,
     ordered, bounded set of named knobs, never by changing WHO is out (that
     is already decided upstream by outThrowTargets/deriveRunnerMoves). Reads
     the FINAL leg's own landing (a relay's earlier legs are real, chained
     transfers - only the last leg actually races the runner) against
     requiredMs = runnerArrivalMs -/+ the class's own targetMarginMs. Mutates
     and returns the same schedule array (every entry already owns real
     start/end times from sequentialThrowSchedule; shifting them in place
     keeps every leg's own real relative draw time intact).
       schedule: sequentialThrowSchedule's own [{base,startMs,endMs,drawMs,out}]
       isOut: true - runner must NOT beat the throw (forceOut/tagOut);
              false - runner MUST beat the throw (contestedSafe/uncontested)
       runnerWho (optional): the specific runner (mv.from - "BATTER"/a base
         key) the runnerLateJump/stretchRunner knobs apply to, so a caller
         (sceneFieldHtml, Stage 4) can look adjustments up per-move; falls
         back to the generic "runner" label when the caller doesn't know
         (or care) which one, e.g. a steal's own single-runner plays.
     Every application is recorded in the returned adjustments[] - debug
     ability is a feature, not an afterthought (plan 4.1's own framing). */
  function reconcileThrowSchedule(schedule, runnerArrivalMs, cls, diff, isOut, runnerWho) {
    var adjustments = [];
    if (!schedule || !schedule.length || runnerArrivalMs == null) {
      return { schedule: schedule, adjustments: adjustments };
    }
    var margin = targetMarginMs(cls, diff);
    var required = isOut ? (runnerArrivalMs - margin) : (runnerArrivalMs + margin);
    var last = schedule[schedule.length - 1];
    var delta = required - last.endMs;
    if (Math.abs(delta) < 1) return { schedule: schedule, adjustments: adjustments };

    if (delta > 0) {
      // Task 9.4 (fact 20): "taking something off the throw" is the
      // physically honest way to land later - solve for the single constant
      // mph (floor-bounded at this thrower's own realistic minimum,
      // THROW_SPEED_BY_POS) that lands the FINAL leg exactly on the
      // required time, before falling back to holding the release at all.
      // Applies to every delta>0 class this function sees (contestedSafe,
      // uncontested, and the rare too-early forceOut, where it reads as
      // "didn't rush a throw that was never in doubt" instead of an
      // arbitrarily early arrival) - chosen once at scheduling time and
      // constant for the leg's own whole flight, so with 9.1's linear CSS
      // timing the rendered ball actually moves at it, not just its total
      // duration.
      if (last.distFt != null && last.throwerPos && THROW_SPEED_BY_POS[last.throwerPos]) {
        var neededDrawMs = required - last.startMs;
        if (neededDrawMs > 0) {
          var speedRange = THROW_SPEED_BY_POS[last.throwerPos];
          var mphFrom = last.distFt / (last.drawMs / 1000) / 1.46667;
          var neededMph = last.distFt / (neededDrawMs / 1000) / 1.46667;
          var mphTo = Math.max(neededMph, speedRange.min);
          var oldDrawMs = last.drawMs;
          last.drawMs = Math.round(last.distFt / (mphTo * 1.46667) * 1000);
          last.endMs = last.startMs + last.drawMs;
          adjustments.push({
            knob: "slowThrow", who: last.base, ms: Math.round(last.drawMs - oldDrawMs),
            mphFrom: Math.round(mphFrom), mphTo: Math.round(mphTo),
            reason: cls + " throw eased off to land later - " +
              (mphTo === speedRange.min && neededMph < speedRange.min
                ? "floor speed reached, closing the remainder with holdRelease"
                : "lands exactly on the required margin, no hold needed"),
          });
          delta = required - last.endMs;
        }
      }
      // Throw must land LATER - hold the release (generalizes today's ad-hoc
      // pitcher-cover-1B clamp and the sac-fly tagStart backward-solve into
      // the one shared knob). slowThrow above already closed as much of the
      // gap as an honestly slower throw can; this closes only the remainder,
      // if any.
      if (delta > 0) {
        var adj = holdChainTo(schedule, required, "holdRelease",
          cls + " throw landed earlier than the required margin");
        if (adj) adjustments.push(adj);
      }
      return { schedule: schedule, adjustments: adjustments };
    }

    // Throw must land EARLIER - the honest throw loses a race the verdict
    // says it won (today's unenforced-at-runtime gap - fact 0.3). Walk the
    // knob order; each closes as much of the remaining deficit as it
    // honestly can before the next one engages.
    var remaining = -delta;

    // 1. quickRelease - shrink the transfer/stagger gaps already baked into
    //    the schedule's own start times toward a floor of 0 (a clean
    //    transfer, no double-clutch) - every leg after the first can give
    //    back up to its own gap to the leg before it (THROW_STAGGER_MS);
    //    the whole chain from that point on shifts earlier with it. Leg 0's
    //    own start is NOT a discretionary gap the same way - most of it is
    //    fieldedMs/catchMs, a hard physical floor (the ball genuinely
    //    hasn't been fielded/caught yet) - only its own THROW_DELAY_MS
    //    transfer sliver is real "quick release" room.
    var quick = 0;
    for (var i = 1; i < schedule.length && remaining > 0; i++) {
      var gap = schedule[i].startMs - schedule[i - 1].endMs;
      var give = Math.min(gap, remaining);
      if (give > 0) {
        for (var j = i; j < schedule.length; j++) { schedule[j].startMs -= give; schedule[j].endMs -= give; }
        quick += give; remaining -= give;
      }
    }
    if (remaining > 0 && THROW_DELAY_MS > 0) {
      var give0 = Math.min(THROW_DELAY_MS, remaining);
      schedule.forEach(function (t) { t.startMs -= give0; t.endMs -= give0; });
      quick += give0; remaining -= give0;
    }
    if (quick > 0) {
      adjustments.push({ knob: "quickRelease", who: last.base, ms: Math.round(quick),
        reason: cls + " throw needed to land earlier to honestly beat the runner" });
    }

    // 2. runnerLateJump / 3. stretchRunner - the runner-side knobs (a late
    // break; failing that, a bounded slower pace). Neither touches the
    // throw's own schedule - a render-layer concern (the runner token's own
    // start delay/pace), wired up when sceneFieldHtml becomes a reader of
    // this plan (Stage 4). Recorded here so the deficit is never silently
    // absorbed: if the throw alone (quickRelease) can't honestly close the
    // gap, that residual is exactly what runnerLateJump/stretchRunner are
    // for, bounded exactly as spec'd - and if even their combined bound
    // can't cover it, the play's own inputs are wrong upstream, by
    // definition, and this says so instead of hiding it.
    if (remaining > 0) {
      var jump = Math.min(remaining, RUNNER_LATE_JUMP_MAX_MS);
      remaining -= jump;
      adjustments.push({ knob: "runnerLateJump", who: runnerWho || "runner", ms: Math.round(jump),
        reason: cls + " runner needed a later break to keep the throw honest" });
    }
    if (remaining > 0) {
      var cap = runnerArrivalMs * STRETCH_RUNNER_MAX_FRAC;
      var stretch = Math.min(remaining, cap);
      remaining -= stretch;
      adjustments.push({ knob: "stretchRunner", who: runnerWho || "runner", ms: Math.round(stretch),
        reason: cls + " runner needed to slow down to keep the throw honest" });
    }
    if (remaining > 0) {
      adjustments.push({ knob: "unresolved", who: last.base, ms: Math.round(remaining),
        reason: "stretchRunner bound exceeded - upstream fielder/runner inputs are honestly too slow for this verdict" });
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[gameday reconciler] could not honestly close a " + cls +
          " margin (" + Math.round(remaining) + "ms short) - upstream inputs need a look, not a bigger fudge");
      }
    }
    return { schedule: schedule, adjustments: adjustments };
  }
  // Was off (Item 15) - a single anonymous dot converging in the outfield
  // read as an unnecessary touch. Back on now that fielderTokensHtml lays
  // out all 9 defenders (dots today, glove SVGs to follow) instead of just
  // the one who touches the ball - a full defensive alignment is a
  // different, more legible thing than the old lone converging dot.
  var SHOW_FIELDER_TOKENS = true;

  // How long the ball is airborne, in animation ms - the real physics hang
  // time, verbatim (ANIM_TIME_SCALE=1.0). No clamp: a real HR hang time (up
  // to ~6.6s) and a real grounder's near-zero hang time both play at their
  // own true duration now, not squeezed into a shared "feel" range.
  function ballTravelMs(flight) {
    return flight ? (flight.hangMs || 0) : 0;
  }

  // Ground-phase time (bounce/roll to the fielder) at its own true real
  // duration too, for the same reason - no longer artificially compressed
  // to fit inside a fixed stylized runner-to-first budget, since RUN_LEG_MS
  // itself is real-time now and has ample room for it.
  var GROUND_TIME_SCALE = 1.0;

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
  // past the dirt. Read by resolveGrounderInterception (maxAlongFt) - both
  // archetypes go through the charge-in race now (Alex's ask: don't cluster
  // IF1B at the dirt edge, let the fielder genuinely race it), this is just
  // the ceiling on how far that race is even allowed to look, so a ball
  // nobody reaches in time still dies on the dirt rather than the outfield
  // grass. Never reaches resolveHitPickup below at all any more - both are
  // GROUND_ARCHETYPES, which now always dispatches to the race regardless
  // of wasOut.
  var STAYS_IN_INFIELD_ARCHETYPES = { bunt: 1, infield_single: 1 };
  function dirtEdgeFt(angleDeg) {
    var offset = (angleDeg - 45) * Math.PI / 180;
    var m = PITCHER_MOUND_FT, r = INFIELD_SKIN_DIRT_R_FT;
    var s = Math.sin(offset);
    return m * Math.cos(offset) + Math.sqrt(Math.max(0, r * r - m * m * s * s));
  }

  /* Hits that stay in the park (physics-redesign plan Part 4.6): the ball
     needs a visible end point (labels, rollout, plausibility) but no out
     choreography. Picked up at its own real friction-decay rest point
     (gp.restFt - already pickupFt's default below), no separate "outfielder
     depth" cutoff (Alex's call: that was always an artificial stand-in for
     an outfielder actually closing on the ball, not a real quantity, and it
     had already twice needed hand-tuning to chase FIELDER_ANCHORS_FT's own
     starting-position depth around - simplest fix is not having a second
     number to keep in sync with anything at all). Never called for a
     bunt/infield_single hit any more (STAYS_IN_INFIELD_ARCHETYPES, above) -
     those race a real fielder via resolveGrounderInterception instead, same
     as a comparable groundout, so there's only ever the one "stays on the
     dirt" ceiling to maintain (maxAlongFt there) rather than two different
     mechanisms for the same rule. The dirt-clearance floor here is a runtime
     floor on the pickup point for ordinary (non-infield-archetype) hits
     only (still cheap insurance against a squibber that technically never
     left the dirt circle). Always capped at fenceAt(angle)-2. Sets flight.
     fieldedDistFt/groundTimeS the same shape the grounder-out resolver
     does, so every consumer (labels, throwHtml on the rare hit-then-throw
     case) reads one shape regardless of out vs. hit. */
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

    // pickupFt/groundTimeS already default to gp.restFt/gp.totalS above -
    // the ball's own real roll, full stop. Only remaining job here is the
    // dirt-clearance floor: a shallow humpback that lands right at the
    // infield dirt's edge with barely any roll of its own still needs to
    // visibly clear it. (STAYS_IN_INFIELD_ARCHETYPES's own ceiling case
    // used to live here too - moved to resolveGrounderInterception's
    // maxAlongFt, since bunt/infield_single now always resolve through the
    // charge-in race instead of ever reaching this function.)
    var need = dirtEdgeFt(flight.angle) + DIRT_CLEAR_MARGIN_FT - flight.distance;
    if (need > pickupFt) { pickupFt = need; groundTimeS = gp.timeAt(need) != null ? gp.timeAt(need) : gp.totalS; }

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
    flight.fielder = nearestFielder(restPt.x, restPt.y, PICKUP_RETREAT_PENALTY);
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
  //
  // Alex's report: a HR that only barely clears (say 378ft against a 375ft
  // wall) never trips that cut at all - its own true landing point sits only
  // a few real feet past the wall's own GROUND position, which (the wall
  // also has real drawn height, fenceWallPathD) reads as landing at or on
  // the wall instead of past it, even though it legitimately cleared. Same
  // fix in the other direction: nudge the last sample out to this same
  // fence+15 floor, along its own real bearing, when the natural flight
  // never reaches it. The ball's real distance (flight.distance, and every
  // label reading it) stays the true number either way - only the marker's
  // own visual stopping point moves, exactly the same "cap the display, not
  // the data" precedent the long-ball truncation above already set.
  function fenceTruncatedSamples(samples) {
    var maxD = FENCE_DEPTH_FT + 15;
    for (var i = 0; i < samples.length; i++) {
      if (Math.hypot(samples[i].x, samples[i].y) >= maxD) return samples.slice(0, i + 1);
    }
    var last = samples[samples.length - 1];
    var d = Math.hypot(last.x, last.y) || 1;
    if (d >= maxD) return samples;
    var scale = maxD / d;
    return samples.slice(0, samples.length - 1).concat([
      { t: last.t, x: last.x * scale, y: last.y * scale, z: last.z }
    ]);
  }

  // Ground-phase samples (Part 6.3 item 5: hops flatten into decaying bounces
  // then a straight roll, joining the same keyframe timeline as the flight)
  // - sampled from flight.groundPath (Part 3), along the ball's real
  // ground-contact direction (Part 1), not the HZ launch bearing.
  //
  // Densely per hop, sparsely across the roll (Alex's report: grounders were
  // reading as a near-straight line). The old version spread a flat
  // GROUND_SAMPLE_STEPS=10 evenly across the WHOLE ground time - for a
  // typical ball, 1-3 short hops (each well under a second) are followed by
  // a much longer roll, so evenly-spaced samples spent most of their budget
  // on the flat roll (where height is genuinely always 0 - correct) and left
  // only one or two samples to trace each hop's actual parabola, often
  // missing the apex entirely. trajectory.js's groundPath already computes
  // each hop's real apexFt/duration (gp.hops) - sampling densely within each
  // hop's own [t0, t0+tS] window traces the real bounce; the roll phase only
  // needs a few points since it has no height to capture, just the
  // deceleration curve.
  var GROUND_HOP_SAMPLE_STEPS = 5;
  var GROUND_ROLL_SAMPLE_STEPS = 5;
  function groundPhaseSamples(flight) {
    var gp = flight.groundPath;
    if (!gp || flight.groundTimeS == null) return [];
    var vx = flight.contactVel.vx, vy = flight.contactVel.vy;
    var sh = Math.hypot(vx, vy) || 1;
    var ux = vx / sh, uy = vy / sh;
    // flight.groundTimeS is the REAL cutoff - when the ball was actually
    // fielded/picked up, almost always earlier than gp.totalS (when it
    // would naturally roll to a dead stop on its own). Bug (Alex's report:
    // trail lines running out past the fielder, sometimes past the field
    // itself): the previous version bounded the roll-phase samples by
    // gp.totalS instead, so on any play fielded before its natural rest it
    // sampled - and drew a trail line through - real ground past the actual
    // play. Every sample here, hops included, must stay <= groundTimeS.
    var groundTimeS = flight.groundTimeS;

    var times = [];
    var tCursor = 0;
    (gp.hops || []).forEach(function (hop) {
      if (tCursor >= groundTimeS) return;
      var hopEnd = Math.min(tCursor + hop.tS, groundTimeS);
      for (var i = 1; i <= GROUND_HOP_SAMPLE_STEPS; i++) {
        times.push(tCursor + (hopEnd - tCursor) * i / GROUND_HOP_SAMPLE_STEPS);
      }
      tCursor += hop.tS;
    });
    if (tCursor < groundTimeS) {
      for (var j = 1; j <= GROUND_ROLL_SAMPLE_STEPS; j++) {
        times.push(tCursor + (groundTimeS - tCursor) * j / GROUND_ROLL_SAMPLE_STEPS);
      }
    }
    if (!times.length) times.push(groundTimeS);

    // The last sample is the ball's rendered rest point, and throwHtml's
    // throw origin is fieldedPoint(flight) projected through ftToSvg, which
    // always assumes z=0 - a thrown ball is picked up off the ground, not
    // mid-bounce. Routing this last sample through gp.distAt/gp.heightAt
    // instead of using fieldedFt and z=0 directly let the two drift apart
    // (both in ground position AND in the z that projectFt folds into screen
    // x/y) whenever that time/distance round trip didn't land back exactly
    // on its own input, or landed a hair into a residual bounce (Alex's
    // report: the throw visibly starting short of/beyond where the ball
    // actually stopped). Snapping this one sample to the same fieldedFt +
    // z=0 fieldedPoint/ftToSvg use keeps them pinned together exactly.
    var fieldedFt = flight.fieldedDistFt != null ? flight.fieldedDistFt - flight.distance : null;
    return times.map(function (t, idx) {
      var isLast = idx === times.length - 1 && fieldedFt != null;
      var d = isLast ? fieldedFt : gp.distAt(t);
      var z = isLast ? 0 : gp.heightAt(t);
      return { t: t, x: flight.x + ux * d, y: flight.y + uy * d, z: z };
    });
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
  // handoffMs (Alex's report - "only one baseball on the field at a time"):
  // raw/unanchored moment (same units as --dur/--fdelay before the final
  // +seqDelay write) a following throw's own first leg starts, when there is
  // one - null for a play with no throw at all (a clean hit, an HR, a fly
  // ball caught with nobody trying anyone). When present, this ball fades
  // OUT instead of merely dimming (ballSettle), so the very next thing the
  // throw's own ball appears doing isn't overlapping a second, still-visible
  // ball sitting at the same spot.
  // Ground-level height for a sample - flightSampleSeries samples carry a
  // real z (feet above the field); interpolated linearly from
  // SHADOW_SCALE_MAX at z=0 down to SHADOW_SCALE_MIN at the flight's own
  // apex, so a grounder (near-zero apexFt throughout) stays large and
  // constant while a fly ball's shadow visibly shrinks toward its peak.
  function shadowScaleAt(zFt, apexFt) {
    if (!apexFt || apexFt <= 0) return SHADOW_SCALE_MAX;
    var frac = clamp(zFt / apexFt, 0, 1);
    return SHADOW_SCALE_MAX - frac * (SHADOW_SCALE_MAX - SHADOW_SCALE_MIN);
  }

  function ballArcHtml(m, flight, handoffMs) {
    var series = flightSampleSeries(flight);
    var totalS = series.totalS > 0 ? series.totalS : 1e-6;
    var projected = series.samples.map(function (s) { return projectFt(s.x, s.y, s.z); });
    // Same samples' real (x,y), ground-projected (z forced to 0, ftToSvg's
    // own definition) - the shadow's own path along the ground, separate
    // from the ball's own arced-through-the-air one above.
    var shadowPts = series.samples.map(function (s) { return ftToSvg(s.x, s.y); });
    var apexFt = flight.apexFt || 0;
    var cumLen = [0];
    for (var i = 1; i < projected.length; i++) {
      cumLen.push(cumLen[i - 1] + Math.hypot(projected[i].x - projected[i - 1].x, projected[i].y - projected[i - 1].y));
    }
    var len = cumLen[cumLen.length - 1] || 1;
    var stops = "", trailStops = "", shadowStops = "";
    var lastOff = 0;
    series.samples.forEach(function (s, i) {
      lastOff = clamp(s.t / totalS, 0, 1) * 100;
      stops += lastOff.toFixed(3) + "% { transform: translate(" + projected[i].x.toFixed(1) + "px," + projected[i].y.toFixed(1) + "px); } ";
      trailStops += lastOff.toFixed(3) + "% { stroke-dashoffset: " + (len - cumLen[i]).toFixed(1) + "px; } ";
      var scale = shadowScaleAt(s.z, apexFt);
      // opacity:1 here only when clearedFence (below explains why) - holds
      // the shadow visible through every real sample so the fade-to-0 added
      // at the truncation stop below is the only opacity change, not a
      // fade spread across the whole flight.
      shadowStops += lastOff.toFixed(3) + "% { transform: translate(" + shadowPts[i].x.toFixed(1) + "px," + shadowPts[i].y.toFixed(1) + "px) scale(" + scale.toFixed(2) + ");" + (flight.clearedFence ? " opacity: 1;" : "") + " } ";
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
      var lastShadowP = shadowPts[shadowPts.length - 1];
      var lastScale = shadowScaleAt(series.samples[series.samples.length - 1].z, apexFt);
      // clearedFence only (Task 12, probe 0.6): the shadow's ground point at
      // the truncation stop is fence+15ft, whose z=0 projection paints
      // inside the drawn wall band (fenceWallPathD) - a resting shadow on
      // the wall's face. The ball itself keeps settling here (Alex's
      // earlier call, untouched) - only the ground shadow, physically
      // hidden behind the wall once the ball clears it, fades out. The
      // movement animation's own `both` fill (movementRule) holds this 0
      // for the rest of the play once the keyframe animation ends.
      shadowStops += "100.000% { transform: translate(" + lastShadowP.x.toFixed(1) + "px," + lastShadowP.y.toFixed(1) + "px) scale(" + lastScale.toFixed(2) + ");" + (flight.clearedFence ? " opacity: 0;" : "") + " } ";
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
    // var(--fdelay,0s) (FIELD_SEQUENCE_DELAY_MS, ballFlightHtml) holds the
    // ball's own movement/trail off until the DIFF/HZ wheels have had their
    // turn - ballSettle's own delay shifts by the same amount via calc() so
    // it still lands exactly when the (now-later) main animation ends.
    // `both`, not `forwards`, on the movement animation itself (Alex's
    // report): with only `forwards`, the animation has no effect at all
    // during its own --fdelay - so for that whole window the element fell
    // back to plain .ball's own base rule, `transform:translate(var(--tx),
    // var(--ty))`, which is the ball's FINAL landing spot (moveVars sets
    // --tx/--ty to arc.endPt) - the marker sat at where the ball ends up
    // before the play had even started. `both` makes the animation's own
    // first keyframe (0% - the real contact point) apply during the delay
    // instead, same fix already in place for .fielder/.rn/.dm-base.on
    // elsewhere in this file.
    // Every duration/delay here divides by --play-speed (Alex's ask: the
    // slideshow speed toggle should actually speed the animation up, not
    // just shorten the wait between plays) - same live CSS custom property
    // style.css's own rules key off, so this generated block stays in sync
    // with the toggle without needing to know the current speed itself.
    // Alex's report: the ball used to sit fully visible (and already
    // green/red) at the contact point for the whole --fdelay wait, thanks to
    // the `both` fill-mode this needs anyway - ballAppear (style.css) holds
    // it invisible until that same moment instead, cross-fading in exactly
    // as pitchBallHtml's own small ball fades out.
    //
    // handoffMs != null (Alex's report, above): swaps the settle-and-stay
    // ballSettle for a real fade-to-0 (ballHandoffFade, style.css), timed to
    // --handoff - the moment a following throw's own ball is about to fade
    // in and take over. Skips ballSettle entirely rather than stacking both.
    //
    // ballHandoffFade MUST be the last animation listed here, after
    // ballAppear, not before - when two animations both hold a `both`/
    // forwards fill on the same property, the one listed LAST wins for
    // their entire overlap (which for two `forwards`-filled animations is
    // "forever after whichever started first"), regardless of which one's
    // own active window comes later in real time. Listed before ballAppear,
    // ballAppear's own permanent post-930ms hold at opacity:1 silently beat
    // this fade every time, no matter what --handoff said (caught by
    // testing, not by reasoning about it up front).
    var hasHandoff = handoffMs != null;
    // Shared by the ball and its shadow - same appear/settle/handoff-fade
    // lifecycle for both (the shadow should never be visible a beat before
    // or after the ball itself is), only the movement keyframes differ.
    function movementRule(kfName) {
      return "animation: " + kfName + " calc(var(--dur) / var(--play-speed,1)) linear calc(var(--fdelay,0s) / var(--play-speed,1)) both, " +
        (hasHandoff ? "" : "ballSettle calc(350ms / var(--play-speed,1)) ease calc((var(--fdelay,0s) + var(--dur)) / var(--play-speed,1)) forwards, ") +
        "ballAppear calc(120ms / var(--play-speed,1)) linear calc(var(--fdelay,0s) / var(--play-speed,1)) both" +
        (hasHandoff
          ? ", ballHandoffFade calc(120ms / var(--play-speed,1)) linear calc(var(--handoff,0s) / var(--play-speed,1)) both;"
          : ";");
    }
    var style = "<style>" +
      "@keyframes " + name + " { " + stops + "} .ball." + name + " { " + movementRule(name) + " }" +
      "@keyframes " + name + "-shadow { " + shadowStops + "} .ball-shadow." + name + " { " + movementRule(name + "-shadow") + " }" +
      "@keyframes " + name + "-trail { " + trailStops + "} " +
      ".ball-trail." + name + " { animation: " + name + "-trail calc(var(--dur) / var(--play-speed,1)) linear calc(var(--fdelay,0s) / var(--play-speed,1)) forwards; }" +
      "</style>";
    var pathD = projected.map(function (p, i) { return (i === 0 ? "M" : "L") + p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ");
    // startPt (Alex's ask, separate report): the ball's real contact point,
    // for pitchBallHtml to hand off to pixel-exactly instead of guessing
    // flat home plate - a batted ball's contact height projects a little
    // differently than ground-level home does.
    return {
      style: style, name: name, pathD: pathD, len: len,
      endPt: projected[projected.length - 1], startPt: projected[0],
      shadowEndPt: shadowPts[shadowPts.length - 1],
      shadowEndScale: shadowScaleAt(series.samples[series.samples.length - 1].z, apexFt),
    };
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

  function ballFlightHtml(m, flight, moves, seqDelay) {
    if (!flight) return "";
    var cleared = flight.clearedFence;
    // fieldedMs, not ballTravelMs (Alex's report, real-time conversation):
    // flightSampleSeries/ballArcHtml already build ONE continuous keyframe
    // timeline from contact all the way to the ball's final resting/fielded
    // point (air samples concatenated with ground-roll samples, "Part 6.3
    // item 5" - a real, deliberate existing feature). But the CSS animation-
    // duration here was still only the AIRBORNE time - so the ball visually
    // covered its full journey, air AND roll, compressed into just the
    // hang time. Invisible while GROUND_TIME_SCALE kept ground time near
    // zero; very visible now that it's real (up to ~1-2s for a routine
    // grounder) - confirmed directly: a real grounder's arc.endPt (the
    // keyframe's own 100% stop) already projects to the true fielded point,
    // not the landing point, so the fix is purely the duration, not a
    // missing visual.
    var dur = fieldedMs(flight);
    // Alex's report: "only one baseball on the field at a time" - when a real
    // throw follows (throwSchedule, the same pure function throwHtml itself
    // calls - TAG_THROW_ARCHETYPES' decorative sac-fly throw counts too, it's
    // still a real ball leaving a fielder's hand), this ball needs to fade
    // out right as that throw's own ball fades in, not just dim and linger
    // forever (ballArcHtml's own handoffMs comment).
    var throwSched = throwSchedule(m, moves, flight);
    var handoffMs = throwSched.length ? Math.min.apply(null, throwSched.map(function (t) { return t.startMs; })) : null;
    var arc = ballArcHtml(m, flight, handoffMs);
    var fdelay = seqDelay || 0;
    var handoffVar = handoffMs != null ? ";--handoff:" + (handoffMs + fdelay) + "ms" : "";
    var moveVars = "--tx:" + arc.endPt.x.toFixed(1) + "px;--ty:" + arc.endPt.y.toFixed(1) + "px;--dur:" + dur + "ms;--fdelay:" + fdelay + "ms" + handoffVar;
    var shadowVars = "--stx:" + arc.shadowEndPt.x.toFixed(1) + "px;--sty:" + arc.shadowEndPt.y.toFixed(1) +
      "px;--sscale:" + arc.shadowEndScale.toFixed(2) + ";--dur:" + dur + "ms;--fdelay:" + fdelay + "ms" + handoffVar;
    var trailVars = "--len:" + arc.len.toFixed(1) + "px;--dur:" + dur + "ms;--fdelay:" + fdelay + "ms";
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
              (wasOut ? " out" : " hit") + (groundedOut ? " grounded-out" : "") +
              (handoffMs != null ? " handoff" : "");
    // Alex's ask: the flight ball is the same baseball (size + seams) the
    // pitch/throw markers now use, not a plain circle - wheelBallIconSvg's
    // own "ball-body" class is what .ball.hit/.ball.out/.ball.out.grounded-out
    // (style.css) key their coloured-border verdict off, now one level
    // deeper than before (the outer .ball<name> element still owns every
    // existing position/opacity animation untouched - a <g> takes CSS
    // transform/opacity animations exactly like the <circle> it replaces).
    // Shadow first (Alex's ask: a ground-projected marker under the ball,
    // sized proportional to height - lower means bigger), so it paints
    // beneath the trail and the ball itself in document order.
    return arc.style +
      '<ellipse class="ball-shadow' + cls + " " + arc.name + '" rx="' + SHADOW_RX + '" ry="' + SHADOW_RY +
        '" style="' + shadowVars + '"></ellipse>' +
      '<path class="ball-trail' + cls + " " + arc.name + '" d="' + arc.pathD + '" style="' + trailVars + '"></path>' +
      '<g class="ball' + cls + " " + arc.name + '" style="' + moveVars + '">' +
        wheelBallIconSvg(BALL_R, "ball-body") +
      "</g>";
  }

  // Alex's ask: every play (a Balk excepted - it's the one result with no
  // actual pitch) opens with the ball travelling mound-to-plate before
  // anything else happens, reusing the wheel's own small-ball icon. Position
  // is a fixed mound->home line every time (PITCH_TRAVEL_MS - the ball's own
  // real mound-to-plate travel time - never varies by play), so unlike
  // ballArcHtml this needs no per-play generated keyframes - one shared
  // @keyframes in style.css covers every play, only the delay varies.
  //
  // Alex's ask: the ball should physically ARRIVE right as the wheel
  // finishes instead of arriving early and then just sitting at the plate
  // for the remainder of that wait - "contact" (or, on a steal, "the catcher
  // has it") and the wheel's own reveal should read as the same instant, not
  // two separate beats. The seqDelay param here is sceneFieldHtml's
  // wheelFinishMs - deliberately NOT runnerSeqDelay even on a steal (Alex's
  // follow-up ask was specifically to keep the pitch itself landing on the
  // wheel's own finish there too, same as every other play, even though the
  // runner's own break/throw/tag timing stays on runnerSeqDelay's earlier,
  // wheel-independent anchor) - and, on a steal, already stealWheelPace-
  // adjusted (Alex's ask: the closer the underlying steal_num/throw_num
  // roll, the slower the wheel plays out for real, so the pitch's own
  // arrival - and the runner's real head start before the catcher/throw,
  // since that runner is already moving well before this - drift later right
  // along with it). Solved by working backwards from that arrival instead of
  // forwards from mount: the ball still takes exactly PITCH_TRAVEL_MS to
  // cross the real 60.5ft, it just leaves the pitcher's hand
  // seqDelay-PITCH_TRAVEL_MS after slide mount instead of immediately, so
  // pStart+PITCH_TRAVEL_MS lands exactly on seqDelay. Math.max(0, ...) is a
  // defensive floor only - never actually hit today, since PITCH_TRAVEL_MS
  // is comfortably under even the slowest wheel pace's finish time.
  // fadeAt is simply wherever the ball actually lands - handing off into
  // ballFlightHtml's hit-ball (which now itself stays invisible until that
  // same fdelay moment, Alex's report - see ballAppear in style.css) or just
  // vanishing for a walk/K/steal.
  //
  // Alex's ask: a walk's pitch visibly misses the zone (walkPitchTargetSvg,
  // away from whichever side the batter's box is on) - every other result
  // (contact, a strikeout, a steal) is a pitch that was actually in the
  // zone, so those all still go dead down the middle.
  function pitchBallHtml(m, flight, seqDelay) {
    // m.result is null only on the on-deck "Now Batting" placeholder (see
    // the moves.map batter-token guard above/key_moments_build.py's
    // _next_batter_moment) - nothing has actually been pitched yet there.
    if (m.result == null || BALK_RESULTS[m.result]) return "";
    var from = ftToSvg(0, PITCHER_MOUND_FT);
    // Alex's report: a batted ball's real contact point (flight.samples[0] -
    // the same first sample ballArcHtml's own arc.startPt projects, read
    // directly here rather than re-running flightSampleSeries/groundPhase
    // Samples' full ground-roll computation just for one point that never
    // differs from this raw first sample either way) isn't flat ground-level
    // home plate - contact happens with some real height on the bat, which
    // projects a few pixels off from HOME_SVG. Matching it exactly is what
    // makes the hand-off into ballFlightHtml's own hit-ball (starting at that
    // identical point) read as one ball, not two overlapping-but-not-quite
    // markers during the crossfade.
    var contactPt = flight ? projectFt(flight.samples[0].x, flight.samples[0].y, flight.samples[0].z) : null;
    var to = WALK_RESULTS[m.result] ? walkPitchTargetSvg(effectiveHand(m.batter_hand)) : (contactPt || HOME_SVG);
    var pStart = Math.max(0, (seqDelay || 0) - PITCH_TRAVEL_MS);
    var fadeAt = pStart + PITCH_TRAVEL_MS;
    var vars = "--fx:" + from.x + "px;--fy:" + from.y + "px;" +
               "--tx:" + to.x + "px;--ty:" + to.y + "px;" +
               "--pdur:" + PITCH_TRAVEL_MS + "ms;--pstart:" + pStart + "ms;--pfade:" + fadeAt + "ms";
    // Alex's ask: sized/bordered exactly like the flight ball (BALL_R,
    // "ball-body" - style.css's shared .ball-body rule), not the wheel's own
    // smaller WHEEL_DOT_R dot - the same baseball the whole diamond now uses.
    return '<g class="pitch-ball" style="' + vars + '">' +
      '<g class="pitch-ball-inner">' + wheelBallIconSvg(BALL_R, "ball-body") + '</g></g>';
  }

  // Split out of ballFlightHtml (Alex's report) so the render order in
  // sceneFieldHtml can layer this - and fielderNameLabelsHtml - ON TOP of
  // the throw lines instead of underneath them; both label sets converge
  // on the same points the throw lines draw to/from, so drawn first they
  // used to sit under the throw's dashed stroke.
  function ballResultLabelHtml(m, flight, seqDelay) {
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
    var labelDelay = (hasGroundPhase ? fieldedMs(flight) : dur) + (seqDelay || 0);
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

  // Every defender the whole time (Alex's ask), not just the one who
  // touches the ball: the 8 not involved this play sit idle at their
  // FIELDER_ANCHORS_FT spot, dimmed via .fielder-idle so they read as
  // background alignment rather than something the ball should visibly
  // collide with on a hit that passes near one - idle tokens also paint
  // UNDER the ball/trail in document order, so a flyover already reads as
  // "over," not "through." Whoever IS involved (today: on a hit that isn't
  // converted to an out, just flight.fielder; on an out, every touch in
  // fieldingChainDetail) is full-opacity and visible from frame one - no
  // fade-in, only their position animates (movingFielderTokenHtml).
  // Glove icon markup shared by both idle and moving tokens - x/y/width/
  // height center the #fielder-glove symbol (docs/index.html) on the <g>'s
  // own local origin, the same point a bare <circle r="..."> used to sit at,
  // so the surrounding --fx/--fy/--tx/--ty transform keeps working unchanged.
  var FIELDER_GLOVE_USE = '<use href="#fielder-glove" x="' + (-FIELDER_ICON_SIZE / 2).toFixed(1) +
    '" y="' + (-FIELDER_ICON_SIZE / 2).toFixed(1) + '" width="' + FIELDER_ICON_SIZE.toFixed(1) +
    '" height="' + FIELDER_ICON_SIZE.toFixed(1) + '"></use>';

  function idleFielderTokenHtml(pos, anchorOverride) {
    var anchor = anchorOverride || FIELDER_ANCHORS_FT[pos];
    var pt = ftToSvg(anchor.x, anchor.y);
    return '<g class="fielder-idle" style="--tx:' + pt.x.toFixed(1) + 'px;--ty:' + pt.y.toFixed(1) +
      'px;">' + FIELDER_GLOVE_USE + '</g>';
  }

  // Fielders not involved in the play still lean toward a batted ball
  // (Alex's ask) - a small creep, not a real break toward the action.
  // Catcher and pitcher excluded: neither is part of any batted-ball race
  // in this model - creeping off the plate reads wrong for a catcher turned
  // around behind it, and a pitcher still settling out of their own
  // delivery isn't in a ready fielding stance to lean anywhere (Alex's
  // question, answered: no). If either one IS the one who fields the ball,
  // that's the unrelated "moving" path above, untouched by this.
  //
  // Two refinements on the original flat-10ft version (Alex's report: it
  // read as too uniform/sudden):
  // (1) Proximity decay - a fielder genuinely near the eventual play leans
  //     close to the full cap; anyone farther falls off fast (exponential
  //     in distance, IDLE_DRIFT_DECAY_FT), not a flat distance regardless
  //     of how far away they actually are.
  // (2) Real accelerating pace, capped by the play's own clock - instead of
  //     a fixed distance, they get however far a real accelerating run
  //     (accelDistForTimeS, the same physics fielderLegDurationsMs's own
  //     accelTimeS uses, just inverted: time -> distance) would cover in
  //     the time actually available before the ball's real fielder gets to
  //     it (fieldedMs) - so they stop leaning once the play's already
  //     decided, rather than continuing to visibly creep after the real
  //     fielder has already touched the ball.
  var IDLE_DRIFT_MAX_FT = 10;
  var IDLE_DRIFT_DECAY_FT = 40;
  var IDLE_DRIFT_MIN_FT = 0.5;
  var IDLE_DRIFT_EXCLUDED_POSITIONS = { C: 1, P: 1 };
  function accelDistForTimeS(timeS, topSpeedFtPerS, accelFtPerS2) {
    var accel = accelFtPerS2 || FIELDER_ACCEL_FT_S2;
    if (timeS <= 0) return 0;
    var accelTimeToTopS = topSpeedFtPerS / accel;
    if (timeS <= accelTimeToTopS) return 0.5 * accel * timeS * timeS;
    var accelDistFt = (topSpeedFtPerS * topSpeedFtPerS) / (2 * accel);
    return accelDistFt + topSpeedFtPerS * (timeS - accelTimeToTopS);
  }
  function idleDriftLeg(pos, targetFt, anchorOverride, m, flight) {
    var anchor = anchorOverride || FIELDER_ANCHORS_FT[pos];
    if (!anchor) return null;
    var dx = targetFt.x - anchor.x, dy = targetFt.y - anchor.y;
    var dist = Math.hypot(dx, dy);
    if (dist <= 0) return null;
    var cap = IDLE_DRIFT_MAX_FT * Math.exp(-dist / IDLE_DRIFT_DECAY_FT);
    if (cap < IDLE_DRIFT_MIN_FT) return null;
    var topSpeed = RUNNER_SPRINT_FT_PER_S * spdPaceScale(fielderSpd(m, pos));
    var availableS = flight ? fieldedMs(flight) / 1000 : Infinity;
    var timeCapFt = accelDistForTimeS(availableS, topSpeed);
    var step = Math.min(cap, dist, timeCapFt);
    if (step < IDLE_DRIFT_MIN_FT) return null;
    var pt = { x: anchor.x + (dx / dist) * step, y: anchor.y + (dy / dist) * step };
    return { toSvg: ftToSvg(pt.x, pt.y), distFt: step };
  }

  // Real outfielders read the ball off the bat before they break, and (per
  // Alex's report) don't cover the ground at a flat-out baserunner sprint
  // either - both of those are simplified here into one flat reaction beat
  // plus one flat pace slowdown, rather than a fully separate model, since
  // the ask was to keep this simple for now. Infielders get neither - the
  // ball's on them too fast for either to read as anything but late.
  var OUTFIELD_POSITIONS = { LF: 1, CF: 1, RF: 1 };

  // Honest outfield pursuit + a reaction-time sink (gameday reconciliation
  // plan 5b), replacing the flat OF_SHIFT_DEG pre-shift prototype. A double/
  // triple, or a well-hit single genuinely past the infield (1BWH/1BWH2 -
  // archetype alone can't tell those from a routine infield-adjacent bloop,
  // so this checks the real result code the same way the old shift did),
  // needs the ball to visibly get past the credited outfielder - in real
  // baseball because the defense wasn't standing right on top of the
  // eventual landing spot. The honest question is a race, not a fixed
  // degree: does this fielder's own real pursuit (true anchor, real
  // accel+SPD, OUTFIELDER_REACT_MS) actually beat the ball to where it
  // ends up? Measured against the real physics before landing this (per
  // the plan's own "verify how often this already suffices" ask): out of
  // 100 sampled double-shaped hits, only 3 - all shallow, borderline
  // ~150ft flies - ever had the honest pursuit winning at all, and by at
  // most ~1.1s; a deep double or triple passes through this completely
  // untouched, no correction needed. applies()/qualifies below is the same
  // archetype/result membership the old OF_SHIFT_ARCHETYPES/
  // OF_SHIFT_WH_SINGLE_RESULTS tables named, just not a separate constant
  // anymore.
  function ofPursuitApplies(m, flight) {
    return !!(flight && OUTFIELD_POSITIONS[flight.fielder] &&
      ((flight.archetype === "double" || flight.archetype === "triple") ||
       (flight.archetype === "single" && m && (m.result === "1BWH" || m.result === "1BWH2"))));
  }
  // Bounded "late break" (Task 5b point 3) - a real, if unlucky, defensive
  // read; visibly hesitates, breaks late, arrives just after the ball. 0
  // when the honest pursuit already loses the race on its own (the common
  // case, confirmed above).
  var OF_READ_DELAY_MAX_MS = 1500;
  // Positive: the fielder's own honest pursuit would beat the ball to the
  // fielded point by this many ms (visually wrong - needs correction, the
  // exact ms a knob below must close). Zero or negative: the ball already
  // beats (or ties) him there on its own - no correction needed.
  function ofPursuitDeficitMs(m, flight, anchorFt) {
    var fieldedFt = fieldedPoint(flight);
    var distFt = Math.hypot(fieldedFt.x - anchorFt.x, fieldedFt.y - anchorFt.y);
    // "pursuit" kind's own profile already carries OUTFIELDER_REACT_MS as
    // its reactionS (fielderProfile, below) plus the Statcast-fit OF
    // pursuit top speed/accel - no longer the generic "run" kind's
    // fielderLegDurationsMs with OUTFIELDER_REACT_MS bolted on separately.
    var honestArrivalMs = Math.round(arrivalTimeS(distFt, fielderProfile(m, flight.fielder, "pursuit")) * 1000);
    return fieldedMs(flight) - honestArrivalMs;
  }
  // anchorFt: whichever anchor the fielder is actually being rendered from
  // (their real one, or the rare ofDerivedShadeAnchorFt fallback below) -
  // the read delay is always sized against the SAME anchor the token
  // actually starts at, not recomputed against a different one.
  function ofReadDelayMs(m, flight, anchorFt) {
    if (!ofPursuitApplies(m, flight)) return 0;
    var deficit = ofPursuitDeficitMs(m, flight, anchorFt);
    return deficit > 0 ? Math.min(deficit, OF_READ_DELAY_MAX_MS) : 0;
  }
  // Fallback (Task 5b point 4) - only reached when even the full read-delay
  // bound can't honestly close the gap: a derived positional shade, same
  // direction the old flat shift used (away from the ball's own TRUE
  // simulated bearing, relative to the credited fielder's own canonical
  // angle - not flight.angle's coarse lattice, which can land exactly on a
  // tie a real bearing never does), sized to the SMALLEST offset that
  // actually closes it for THIS play's own real geometry/pace, not a fixed
  // 5deg for every play regardless of need. Bisects out to a 45deg cap - a
  // pre-pitch defensive shade, which is what this actually represents once
  // physics forces it, not a magic per-play number.
  // Direction relative to the fielder who actually ends up credited
  // (flight.fielder), not the whole field's own dead-center (45) - a real
  // bug report caught this originally: a double left of RF (e.g. angle 60,
  // left of RF's own 72) needs RF shaded further right/deeper into the
  // corner (away from 60, toward 77+) so RF has farther to close, not
  // shaded left toward the hit. Compares against the ball's own TRUE
  // simulated bearing (atan2 off its real x/y), not flight.angle - a
  // second real bug report caught this: flight.angle is the coarse
  // 11-point HZ lattice (8deg apart), which a real play can land exactly
  // ON a tie (a Calvin Huff double: pitch/swing's last digits both landed
  // on the same bucket, so flight.angle read exactly 45) even though the
  // real simulated landing point clearly wasn't a tie at all. The true
  // bearing has no such lattice-quantization gap.
  function ofShadeDirection(flight) {
    var refAngle = OF_CANONICAL_ANGLE[flight.fielder];
    var trueAngle = 45 + Math.atan2(flight.x, flight.y) * 180 / Math.PI;
    return trueAngle <= refAngle ? 1 : -1;
  }
  function ofDerivedShadeAnchorFt(m, flight, pos) {
    var direction = ofShadeDirection(flight);
    function anchorAtDeg(deg) {
      return landingPoint(OUTFIELDER_DEPTH_FT[pos], clamp(OF_CANONICAL_ANGLE[pos] + direction * deg, 3, 87));
    }
    // "Closes" once the remaining deficit at this shade no longer exceeds
    // what the full read-delay bound can still cover on top of it.
    function closesAtDeg(deg) { return ofPursuitDeficitMs(m, flight, anchorAtDeg(deg)) <= OF_READ_DELAY_MAX_MS; }
    if (!closesAtDeg(45)) return anchorAtDeg(45); // even the full cap can't close it - shade as far as this model allows
    var lo = 0, hi = 45; // lo: not yet confirmed to close; hi: confirmed to close (45, checked above)
    for (var i = 0; i < 20; i++) {
      var mid = (lo + hi) / 2;
      if (closesAtDeg(mid)) hi = mid; else lo = mid;
    }
    return anchorAtDeg(hi);
  }
  // The anchor a fielder is actually DRAWN starting from - real anchor
  // unconditionally now (1B's own PFP depth fudge is gone too, per 5a:
  // firstBaseCoverage decides that off a real race, not a render trick),
  // except the rare ofDerivedShadeAnchorFt fallback above.
  function fielderStartAnchorFt(pos, flight, m) {
    if (!ofPursuitApplies(m, flight) || pos !== flight.fielder) return FIELDER_ANCHORS_FT[pos];
    if (ofPursuitDeficitMs(m, flight, FIELDER_ANCHORS_FT[pos]) <= OF_READ_DELAY_MAX_MS) {
      return FIELDER_ANCHORS_FT[pos]; // the read delay alone already covers it - no shade needed
    }
    return ofDerivedShadeAnchorFt(m, flight, pos);
  }

  var OUTFIELDER_REACT_MS = 400;
  // Retired (Alex's report): this used to be a flat 1.6x slowdown standing
  // in for "outfielders don't cover ground at a flat-out sprint," back when
  // the duration model was pure flat distance/speed with no ramp-up at all.
  // Real acceleration (FIELDER_ACCEL_FT_S2 below) now models exactly that
  // trait organically - stacking this flat multiplier ON TOP of a real
  // accelerating run double-counted it, and for a genuinely long run (a
  // fly ball down the line) compounded into an absurd ~1.6x-longer total
  // that was arriving well after a real outfielder ever would (measured:
  // a 232ft corner fly took ~15s to cover with both effects stacked, vs.
  // ~9s with acceleration alone - a real sprinter's actual ballpark for
  // that distance). OUTFIELDER_REACT_MS (the pre-break read) is the only
  // OF-specific trait left, and it's a genuinely different thing - a delay
  // before moving, not a slower pace once moving.
  function fielderStartDelay(pos, baseDelay) {
    return baseDelay + (OUTFIELD_POSITIONS[pos] ? OUTFIELDER_REACT_MS : 0);
  }

  // Task 3 (facts 14/26): the ONE source of "when does the ball-touching
  // fielder actually arrive at fieldedPoint, as rendered" - both
  // fielderTokensHtml (the picture) and throwSchedule (the race) read this
  // so they can never disagree about the same run. Raw units, same
  // pre-seqDelay convention fieldedMs/ballTravelMs already use - callers add
  // their own seqDelay/startDelay on top.
  //   reaction beat: "run" kind gets the flat OUTFIELDER_REACT_MS add
  //   (fielderStartDelay's own logic, reproduced here without baseDelay);
  //   "pursuit" kind already carries that same reaction inside its own
  //   profile (fielderProfile's reactionS) - added once, not twice, exactly
  //   the double-count movingFielderTokenHtml's profileKind param (below)
  //   also avoids.
  function fielderBallArrivalMs(m, flight) {
    if (!flight || !flight.fielder) return null;
    var anchor = fielderStartAnchorFt(flight.fielder, flight, m);
    if (!anchor) return null;
    var fieldedFt = fieldedPoint(flight);
    var distFt = Math.hypot(fieldedFt.x - anchor.x, fieldedFt.y - anchor.y);
    var kind = ofPursuitApplies(m, flight) ? "pursuit" : "run";
    var readDelayMs = ofReadDelayMs(m, flight, anchor);
    var reactionMs = (kind === "run" && OUTFIELD_POSITIONS[flight.fielder]) ? OUTFIELDER_REACT_MS : 0;
    var travelMs = Math.round(arrivalTimeS(distFt, fielderProfile(m, flight.fielder, kind)) * 1000);
    return readDelayMs + reactionMs + travelMs;
  }
  // Real fielders build up to their top pace rather than moving at a flat
  // rate from frame one (Alex's ask, two related reports pointing at the
  // same missing piece: the idle-drift lean read as an instant snap over
  // its own short ~10ft creep, and outfielders were visibly beating a
  // double/triple's own ball flight to the fence - a dead sprint chasing
  // something already past them shouldn't win that race early). Constant-
  // acceleration kinematics up to the existing top-speed constant: a short
  // hop (well under the distance needed to reach top speed) now genuinely
  // takes longer per foot than the old flat distance/speed model ever
  // charged it, and a long run spends real time still ramping up too -
  // both read as more natural without retuning the top-speed constants
  // (RUNNER_SPRINT_FT_PER_S etc.) other systems already lean on. Ballpark
  // value (Alex: tune to taste) - reaches RUNNER_SPRINT_FT_PER_S (27ft/s)
  // in a bit over a second, same rough feel as a real infielder's controlled
  // first few steps.
  var FIELDER_ACCEL_FT_S2 = 25;
  // Runners and a fielder's own "run" kind (below) used to share
  // FIELDER_ACCEL_FT_S2 wholesale. Split out (Alex's ask) after fitting a
  // constant-acceleration curve to the 2026 Statcast home-to-first splits
  // (n=507): a pure-acceleration fit over the 0-40ft window - short enough
  // that real runners haven't already leveled off toward top speed, which
  // pulls a wider-window fit down further than the true early ramp - lands
  // at ~19.2ft/s2, well under FIELDER_ACCEL_FT_S2's 25. Infield charge/
  // barehand-scoop kind keeps the old 25 unchanged; that's a different,
  // shorter-range action this dataset doesn't speak to.
  var RUNNER_ACCEL_FT_S2 = 19.2;
  // Outfield fly-ball pursuit ("pursuit" kind, below) gets its own top speed
  // and acceleration, fit against the 2026 Statcast outfielder jump data
  // (n=90, f_bootup_distance: real feet gained toward the ball's eventual
  // spot in the first 3s) rather than reusing a runner/infield number.
  // Solved against the pursuit kind's OWN already-live reaction constant
  // (OUTFIELDER_REACT_MS/1000 = 0.4s, not CHARGE_REACTION_S) for the
  // constant acceleration that reproduces the real league-average 34.4ft in
  // 3s: ~10.2ft/s2 - a much gentler ramp than FIELDER_ACCEL_FT_S2, and
  // consistent with a fielder still being well short of top speed 3 seconds
  // after reading a fly ball. OF_PURSUIT_TOP_SPEED_FT_PER_S is set to the
  // speed that constant acceleration reaches AT that same 3s mark (rather
  // than assumed independently), since the data doesn't cover what happens
  // after - so the two constants are read together, not tuned separately.
  var OF_PURSUIT_ACCEL_FT_S2 = 10.2;
  var OF_PURSUIT_TOP_SPEED_FT_PER_S = 26.4;
  function accelTimeS(distFt, topSpeedFtPerS, accelFtPerS2) {
    var accel = accelFtPerS2 || FIELDER_ACCEL_FT_S2;
    if (distFt <= 0) return 0;
    var accelDistFt = (topSpeedFtPerS * topSpeedFtPerS) / (2 * accel);
    if (distFt <= accelDistFt) return Math.sqrt(2 * distFt / accel);
    var accelTimeToTopS = topSpeedFtPerS / accel;
    return accelTimeToTopS + (distFt - accelDistFt) / topSpeedFtPerS;
  }

  // ── Shared race primitive (gameday reconciliation plan, Task 3.1) ─────────
  // One motion model for everything that moves or flies. A "profile" is
  // { topSpeedFtPerS, accelFtPerS2, reactionS }: an accelerating fielder or
  // runner passes a finite accelFtPerS2 (accelTimeS above); a thrown/pitched
  // ball is the degenerate case (accelFtPerS2: Infinity, i.e. constant
  // velocity from release) - same function, no special casing at call sites.
  // reactionS is a one-time delay before movement starts (recognize-and-break),
  // charged once per race, not per leg of a multi-leg run.
  function arrivalTimeS(distFt, profile) {
    var dist = Math.max(0, distFt || 0);
    var reactionS = (profile && profile.reactionS) || 0;
    if (dist <= 0) return 0;
    var accel = profile.accelFtPerS2;
    var moveTimeS = (accel == null || !isFinite(accel))
      ? dist / profile.topSpeedFtPerS
      : accelTimeS(dist, profile.topSpeedFtPerS, accel);
    return reactionS + moveTimeS;
  }
  // Per-leg duration array over an ordered [{distFt}, ...] path - momentum
  // (and the one-time reaction) carries across legs rather than each leg
  // re-accelerating from a dead stop; same contract fielderLegDurationsMs
  // already had. A single leg is the degenerate case and reduces to
  // arrivalTimeS's own single-value result.
  function legDurationsMs(legs, profile) {
    var reactionS = (profile && profile.reactionS) || 0;
    var accel = profile && profile.accelFtPerS2;
    var topSpeed = profile && profile.topSpeedFtPerS;
    var cumDistFt = 0, prevTimeS = 0, reactionApplied = false;
    return legs.map(function (leg) {
      var distFt = (leg && leg.distFt != null) ? leg.distFt : leg;
      cumDistFt += distFt;
      var moveTimeS = (accel == null || !isFinite(accel))
        ? cumDistFt / topSpeed
        : accelTimeS(cumDistFt, topSpeed, accel);
      var timeS = moveTimeS + (!reactionApplied && cumDistFt > 0 ? reactionS : 0);
      if (cumDistFt > 0) reactionApplied = true;
      var ms = (timeS - prevTimeS) * 1000;
      prevTimeS = timeS;
      return Math.round(ms);
    });
  }
  // Fielder motion profiles, built off the existing per-position/per-kind
  // constants so each keeps its exact current meaning as a profile
  // parameter, not a formula fork. kind: "charge" (infield charge-in, 16
  // base, FIELDER_ACCEL_FT_S2) | "pursuit" (OF fly-ball routing, its own
  // Statcast-fit top speed/accel above, reaction OUTFIELDER_REACT_MS) |
  // "run" (fielder token pace, 27 base, RUNNER_ACCEL_FT_S2 - same rate and
  // ramp a runner sprints at). All scaled by this fielder's own
  // spdPaceScale(fielderSpd(m,pos)).
  function fielderProfile(m, pos, kind) {
    var base = kind === "pursuit" ? OF_PURSUIT_TOP_SPEED_FT_PER_S
      : kind === "charge" ? FIELDER_CHARGE_FT_PER_S
      : RUNNER_SPRINT_FT_PER_S;
    var accel = kind === "pursuit" ? OF_PURSUIT_ACCEL_FT_S2
      : kind === "charge" ? FIELDER_ACCEL_FT_S2
      : RUNNER_ACCEL_FT_S2;
    var reactionS = kind === "run" ? 0
      : kind === "pursuit" ? OUTFIELDER_REACT_MS / 1000
      : CHARGE_REACTION_S;
    return {
      topSpeedFtPerS: base * spdPaceScale(fielderSpd(m, pos)),
      accelFtPerS2: accel,
      reactionS: reactionS
    };
  }
  // Runner motion profile (Task 3.2) - real per-runner pace, same
  // accelerating kinematics a fielder's own "run" profile uses. who:
  // "BATTER" | "1B" | "2B" | "3B" -> runnerSpd, above.
  function runnerProfile(m, who) {
    return {
      // Task 11: additive, not spdPaceScale's multiplicative percentage -
      // spd 1..5 -> 25/26/27/28/29 ft/s (a 16% spread top to bottom,
      // vs. spdPaceScale's wider 63.2%). Fielders keep spdPaceScale
      // untouched (fielderProfile, idleDriftLeg) - this is runners only.
      topSpeedFtPerS: RUNNER_SPRINT_FT_PER_S + (runnerSpd(m, who) - SPD_AVERAGE) * RUNNER_SPD_FT_PER_S_PER_POINT,
      accelFtPerS2: RUNNER_ACCEL_FT_S2,
      reactionS: 0
    };
  }
  // Per-runner accelerating leg time, folding RUN_LEG_MS (Task 3.2). legs is
  // RUN_LEG_MS's own index convention - an integer count of 90ft bases, not
  // a per-waypoint array; a runner's basepath is always a straight multiple
  // of BASE_DIST_FT for this purpose. Deliberate behavior change alongside
  // the per-player speed wiring (Alex's call, plan 3.2, taken now rather
  // than deferred): runners accelerate from a standing start instead of
  // covering every leg at flat top speed - a spd-3 runner's 90ft leg goes
  // from RUN_LEG_MS[1]'s old flat ~3333ms to ~3873ms. RUN_LEG_MS itself
  // survives unconverted as the explicit league-average fallback table
  // (spd null, or historical archives built before the spd fields existed).
  function runnerLegMs(m, who, legs) {
    return Math.round(arrivalTimeS(legs * BASE_DIST_FT, runnerProfile(m, who)) * 1000);
  }
  // Throw motion profile. throwClass is an extensibility hook (Task 3.3) -
  // "full" is the only implemented class this pass (THROW_SPEED_MPH, no
  // data-grounded arm-strength rating exists to vary it by fielder); "toss"
  // is reserved for a future short-distance/underhand flip and currently
  // falls back to "full" unchanged.
  var THROW_CLASS_MPH = { full: THROW_SPEED_MPH };
  // pos (optional, Task 9.3): THROW_SPEED_BY_POS's own mph wins when known,
  // ahead of throwClass's flat fallback - same precedence throwSchedule's
  // own per-leg thrower resolution uses.
  function throwProfile(throwClass, pos) {
    var mph = (pos && THROW_SPEED_BY_POS[pos] && THROW_SPEED_BY_POS[pos].mph) ||
      (throwClass && THROW_CLASS_MPH[throwClass]) || THROW_SPEED_MPH;
    return { topSpeedFtPerS: mph * 1.46667, accelFtPerS2: Infinity, reactionS: 0 };
  }
  // Real per-player speed (Alex's ask, spdPaceScale/fielderSpd above) scales
  // every fielder's own top speed, not just the flat outfielder slowdown
  // layered on top of it (OUTFIELD_POSITIONS-only, unchanged - a separate
  // "reads it off the bat, doesn't sprint flat-out" behavioral trait, not a
  // raw speed difference).
  //
  // legs: ordered [{distFt}, ...] - momentum carries through every waypoint
  // rather than each leg re-accelerating from a dead stop (a real bug this
  // surfaced: pitcherCover1BLegs's 4-leg curve was computing accelTimeS from
  // zero speed at the start of EACH leg, inflating a real ~68ft path into
  // four independent short sprints worth of ramp-up). Each leg's own
  // duration is the DELTA between accelTimeS at its own cumulative distance
  // and the previous waypoint's - one continuous accelerating run over the
  // summed distance, split back into per-leg deltas only because the
  // multi-leg keyframe renderer (movingFielderTokenHtml) needs per-leg
  // stops. A single leg is the degenerate case (cumDistFt === its own
  // distFt, prevTimeS starts at 0) and returns exactly the old single-value
  // formula unchanged.
  function fielderLegDurationsMs(m, pos, legs, kind) {
    return legDurationsMs(legs, fielderProfile(m, pos, kind || "run"));
  }

  var fielderArcCounter = 0;
  function fielderArcId(m, pos) {
    var raw = (m && m.moment_id != null ? m.moment_id : "n" + (fielderArcCounter++)) + "-" + pos;
    return "fArc-" + String(raw).replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  // legs: ordered [{toSvg, distFt}, ...] waypoints from the fielder's own
  // anchor - distFt is real feet, used only to pace each leg (fielderLegMs).
  // One leg is the common case (a plain fielding run, a steal cover, a relay
  // receiver) and reuses the cheap shared .fielder/fielderIn CSS animation.
  // Two+ legs (Alex's report: an unassisted fielder was running straight to
  // the bag instead of fielding the ball first) need a real waypoint stop
  // mid-animation - CSS keyframe percentages can't be driven by custom
  // properties, so this generates a small per-token <style> block with its
  // own named keyframe, the same "per-play custom @keyframes" pattern
  // ballArcHtml already uses for the ball's own multi-point path (stacking
  // two separately-delayed animations on one element was tried first and
  // doesn't give a clean handoff - each one's own fill-mode:both `from`
  // state bleeds into the other's active window). Visible the whole time in
  // both cases - no fade-in, only position animates; a fielder who reaches
  // a spot before the ball does just stands there already, which is exactly
  // right for a fly ball someone camps under.
  // deadlineMs (optional, absolute - same startDelay-inclusive space as
  // delayMs below): a caught-in-air out's own real catch moment (Alex's
  // ask, no exceptions - a fielder must never visibly arrive after a ball
  // the recorded result already says they caught). The ball's hang time is
  // fixed physics, independent of the fielder's own accelerating travel
  // time, so when the natural pace doesn't get them there in time (real
  // report: a deep fly down the line, genuine ground to cover) this
  // compresses the run to land exactly on the deadline instead. Only ever
  // shortens, never extends - a fielder who's naturally early keeps their
  // own natural pace, no artificial slow-down to "wait" for the ball.
  function movingFielderTokenHtml(m, pos, legs, startDelay, anchorOverride, deadlineMs, profileKind) {
    var anchor = anchorOverride || FIELDER_ANCHORS_FT[pos];
    if (!anchor || !legs.length) return "";
    var from = ftToSvg(anchor.x, anchor.y);
    // "pursuit" already carries its own reaction beat inside its profile
    // (fielderBallArrivalMs's own comment) - skip fielderStartDelay's flat
    // OUTFIELDER_REACT_MS add here to avoid double-counting it; every other
    // kind (the default "run") keeps that add exactly as before.
    var delayMs = profileKind === "pursuit" ? startDelay : fielderStartDelay(pos, startDelay);
    var durs = fielderLegDurationsMs(m, pos, legs, profileKind);
    var totalMs = durs.reduce(function (a, b) { return a + b; }, 0) || 1;
    if (deadlineMs != null) {
      var budgetMs = deadlineMs - delayMs;
      if (budgetMs < totalMs) {
        var newTotalMs = Math.max(durs.length, Math.min(totalMs, budgetMs));
        var scale = newTotalMs / totalMs;
        durs = durs.map(function (d) { return Math.max(1, Math.round(d * scale)); });
        totalMs = durs.reduce(function (a, b) { return a + b; }, 0) || 1;
        delayMs = Math.max(0, deadlineMs - totalMs);
      }
    }

    if (legs.length === 1) {
      var vars = "--fx:" + from.x.toFixed(1) + "px;--fy:" + from.y.toFixed(1) + "px;" +
                 "--tx:" + legs[0].toSvg.x.toFixed(1) + "px;--ty:" + legs[0].toSvg.y.toFixed(1) + "px;" +
                 "--delay:" + delayMs + "ms;--dur:" + totalMs + "ms";
      return '<g class="fielder" style="' + vars + '">' + FIELDER_GLOVE_USE + '</g>';
    }

    var pts = [from].concat(legs.map(function (l) { return l.toSvg; }));
    var name = fielderArcId(m, pos);
    var cum = 0, stops = "";
    pts.forEach(function (p, i) {
      var pct = 0;
      if (i > 0) { cum += durs[i - 1]; pct = clamp(cum / totalMs, 0, 1) * 100; }
      stops += pct.toFixed(3) + "% { transform: translate(" + p.x.toFixed(1) + "px," + p.y.toFixed(1) + "px); } ";
    });
    var lastPt = pts[pts.length - 1];
    var style = "<style>@keyframes " + name + " { " + stops + "} .fielder." + name +
      " { animation: " + name + " calc(" + totalMs + "ms / var(--play-speed,1)) linear calc(" + delayMs + "ms / var(--play-speed,1)) both; }</style>";
    return style + '<g class="fielder ' + name + '" style="--tx:' + lastPt.x.toFixed(1) + 'px;--ty:' + lastPt.y.toFixed(1) + 'px;">' + FIELDER_GLOVE_USE + '</g>';
  }

  // Pitcher covering first doesn't run a straight diagonal from the mound
  // (Alex's ask) - real footwork angles out toward the foul line first,
  // then flattens out to approach the bag on a path roughly parallel to it.
  // Modeled as a handful of straight waypoints (movingFielderTokenHtml's
  // existing multi-leg mechanism, same one the fielder-then-throw case
  // above already uses) whose perpendicular distance off the home-to-1B
  // line decays exponentially with progress along it - an "asymptotic"
  // converge onto the line, then up it - with the final waypoint always
  // forced exactly onto the bag regardless of how close the decay curve
  // alone would land, so the token still lands precisely on base.
  var PITCHER_COVER_1B_LEGS = 4;
  var PITCHER_COVER_1B_CONVERGE_RATE = 3.5;
  function pitcherCover1BLegs(anchorFt, baseFt, baseSvg) {
    var lineLen = Math.hypot(baseFt.x, baseFt.y) || 1;
    var dirX = baseFt.x / lineLen, dirY = baseFt.y / lineLen;
    var perpX = -dirY, perpY = dirX;
    var forward0 = anchorFt.x * dirX + anchorFt.y * dirY;
    var perp0 = anchorFt.x * perpX + anchorFt.y * perpY;
    var legs = [];
    var prevFt = anchorFt;
    for (var i = 1; i <= PITCHER_COVER_1B_LEGS; i++) {
      var isLast = i === PITCHER_COVER_1B_LEGS;
      var ptFt, ptSvg;
      if (isLast) {
        ptFt = baseFt; ptSvg = baseSvg;
      } else {
        var t = i / PITCHER_COVER_1B_LEGS;
        var forward = forward0 + t * (lineLen - forward0);
        var perp = perp0 * Math.exp(-PITCHER_COVER_1B_CONVERGE_RATE * t);
        ptFt = { x: dirX * forward + perpX * perp, y: dirY * forward + perpY * perp };
        ptSvg = ftToSvg(ptFt.x, ptFt.y);
      }
      legs.push({ toSvg: ptSvg, distFt: Math.hypot(ptFt.x - prevFt.x, ptFt.y - prevFt.y) });
      prevFt = ptFt;
    }
    return legs;
  }

  // Real total travel time (ms since contact) for the pitcher's own curved
  // run to cover first (pitcherCover1BLegs) - throwSchedule's own clamp
  // needs this to keep a 3-1 putout's throw from visibly beating the
  // pitcher there (see there for the full rationale). Same anchor/base/leg
  // math the token render itself uses, same per-leg pacing (fielderLegMs,
  // acceleration included) - not a separate estimate that could drift out
  // of sync with what's actually drawn.
  function pitcherCover1BArrivalMs(m) {
    var legs = pitcherCover1BLegs(FIELDER_ANCHORS_FT.P, BASE_POS_FT["1B"], SCENE_BASES["1B"]);
    return fielderLegDurationsMs(m, "P", legs).reduce(function (a, b) { return a + b; }, 0);
  }

  // Real race-based PFP coverage decision (gameday reconciliation plan
  // 5a), replacing the old angle===77 magic-number gate. 1B's own honest
  // jog back from where he actually fielded the ball (fieldedPoint - his
  // real charge endpoint, not his static anchor; by the time this question
  // matters he's already off the bag) to the 1B bag, raced against the
  // batter's own real arrival at first, same forceOut margin every other
  // out-throw race targets.
  //
  // Measured against the real physics before landing this (Alex's own
  // 77deg case, per the plan's own flagged risk): the 77/85 lattice split
  // turns out NOT to be the real signal - both buckets sit close enough to
  // 1B's own anchor that a routine play always has him back with seconds
  // to spare (~1.3-2.9s return vs. a ~4.0s batter). What DOES genuinely
  // strand him is a hard-hit ball that keeps rolling (high EV, positive
  // LA) well past his own depth before the charge race catches it - a
  // real return of 60-157ft, 3-6+ real seconds, comfortably beyond the
  // batter's own arrival. So the race itself, not the angle bucket, is
  // the honest trigger - "3-1" now reads correctly rare (an actual hard
  // roller) instead of appearing on every 77deg play regardless of how
  // close it was fielded.
  //
  // Falls back to the old angle-77 heuristic only when the flight has no
  // real fielded-point geometry to race with (a minimal/synthetic test
  // fixture, not a real resolved play) - every real play reaches this with
  // fieldedDistFt already set by resolveGrounderInterception.
  function firstBaseCoverage(m, flight) {
    if (!flight || flight.fielder !== "1B" || flight.archetype !== "grounder") return "1B";
    if (!m) return flight.angle === 77 ? "P" : "1B";
    var originFt = fieldedPoint(flight);
    if (originFt.x == null || originFt.y == null || !isFinite(originFt.x) || !isFinite(originFt.y)) {
      return flight.angle === 77 ? "P" : "1B";
    }
    var baseFt = BASE_POS_FT["1B"];
    var distFt = Math.hypot(baseFt.x - originFt.x, baseFt.y - originFt.y);
    var returnMs = arrivalTimeS(distFt, fielderProfile(m, "1B", "run")) * 1000;
    var margin = targetMarginMs("forceOut", m.diff);
    return (returnMs <= batterFirstArrivalMs(m) - margin) ? "1B" : "P";
  }

  // Which fielder covers a steal's target base (Alex's ask) - SS for 2B,
  // 3B for 3B. HOME deliberately excluded: the catcher is thrower or
  // already standing there (stealThrowHtml's own carve-out for a steal of
  // home off the pitcher), nobody needs to run anywhere for that one.
  var STEAL_COVER_POSITION = { "2B": "SS", "3B": "3B" };

  function fielderTokensHtml(m, flight, moves, seqDelay) {
    var allPositions = Object.keys(FIELDER_ANCHORS_FT);
    // Every mover starts together at contact (seqDelay - whenever this play's
    // own slide begins - not fieldedMs/ballTravelMs) - see movingFielderTokenHtml's
    // own comment above.
    var startDelay = seqDelay || 0;

    if (!flight) {
      // No batted ball - the only fielder movement worth showing is a steal
      // attempt's covering fielder heading to the bag.
      var steal = stealThrowTarget(m, moves);
      var stealPos = steal && STEAL_COVER_POSITION[steal.base];
      var stealAnchor = stealPos && FIELDER_ANCHORS_FT[stealPos];
      var stealDestFt = stealPos && BASE_POS_FT[steal.base];
      var stealDestSvg = stealPos && (steal.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[steal.base]);
      if (stealAnchor && stealDestFt && stealDestSvg) {
        var stealDistFt = Math.hypot(stealDestFt.x - stealAnchor.x, stealDestFt.y - stealAnchor.y);
        return allPositions.filter(function (pos) { return pos !== stealPos; })
          .map(function (pos) { return idleFielderTokenHtml(pos); }).join("") +
          movingFielderTokenHtml(m, stealPos, [{ toSvg: stealDestSvg, distFt: stealDistFt }], startDelay);
      }
      return allPositions.map(function (pos) { return idleFielderTokenHtml(pos); }).join("");
    }

    var moving = {};
    var moversHtml = "";
    if (!flight.clearedFence) {
      var archetype = flight.archetype;
      var isAir = !!CAUGHT_IN_AIR[archetype];
      var isNewOut = (m.outs_after || 0) > (m.outs_before || 0);
      var isGroundOrAir = flight.fielder && (GROUND_ARCHETYPES[archetype] || isAir);
      // Task 2a (fact 23): computed once, ahead of the branch, so a
      // decorative/contestedSafe leg (a b0534c3 explicit-ThrowOrder hit
      // throw, a sac-fly tag throw, the folded infield-single 1B leg) can
      // route through the chain-mover branch below too, not just a real
      // out - its receiver now gets moved to the bag same as an out-throw's
      // does, instead of standing idle.
      var targets = outThrowTargets(m, moves, flight);
      if (flight.fielder && (targets.length || (isNewOut && isGroundOrAir))) {
        // Same chain fieldingChainDetail builds (same inputs, same
        // coveringPosition rule) - but collapsed differently. That function
        // drops a later touch by the SAME fielder entirely (an unassisted
        // force reads as one combined result+name label, not two) - token
        // movement needs the opposite: the real final destination, so an
        // unassisted fielder visibly continues to the bag they touch rather
        // than stopping at the fielding point (Alex's ask). Merging (keep
        // the LAST base seen per position) rather than dropping gets that
        // without changing fieldingChainDetail's own label-facing contract.
        // The FULL target list now (Task 2a) - previously sliced to
        // realOutThrowCount, which left every decorative leg's receiver
        // out of the chain entirely.
        var relayBases = targets;
        var pickupFt = fieldedPoint(flight);
        var pickupSvg = ftToSvg(pickupFt.x, pickupFt.y);
        // Task 8.3: every cutoff (position) leg in this chain sits on the
        // SAME line - from where the ball was originally fielded to the
        // chain's own eventual base - at the same constant fraction along
        // it (CUTOFF_POSITION_FRAC). Not dynamic mid-flight redirection
        // (round 1 scoped that out and it stays out): a pre-declared spot,
        // computed once here.
        var finalBaseLeg = finalBaseOfChain(relayBases);
        var finalBaseFt = finalBaseLeg ? BASE_POS_FT[finalBaseLeg] : null;
        var chainCutoffFt = finalBaseFt ? cutoffSpotFt(pickupFt, finalBaseFt) : null;
        var relayBaseCount = baseLegs(relayBases).length;
        var rawChain = [{ pos: flight.fielder, base: null, kind: null, cutoffFt: null }];
        relayBases.forEach(function (leg) {
          if (leg.kind === "pos") {
            rawChain.push({ pos: leg.pos, base: null, kind: "pos", cutoffFt: chainCutoffFt });
          } else {
            rawChain.push({
              pos: coveringPosition(leg.base, archetype, flight.angle, flight.fielder, relayBaseCount, m, flight),
              base: leg.base, kind: "base", cutoffFt: null,
            });
          }
        });
        var merged = [];
        rawChain.forEach(function (entry) {
          var prev = merged[merged.length - 1];
          if (prev && prev.pos === entry.pos) {
            prev.base = entry.base; prev.kind = entry.kind; prev.cutoffFt = entry.cutoffFt;
          } else {
            merged.push({ pos: entry.pos, base: entry.base, kind: entry.kind, cutoffFt: entry.cutoffFt });
          }
        });

        merged.forEach(function (e) {
          if (moving[e.pos]) return;
          var anchor = fielderStartAnchorFt(e.pos, flight, m);
          if (!anchor) return;
          // Unassisted (this fielder both touched the ball AND covers the
          // next base themselves, e.base non-null on the SAME entry the
          // primary toucher merged into): two real legs, field it then run
          // the bag - not a straight line from anchor to the base skipping
          // the ball entirely (Alex's report - this was the actual bug).
          // Every other entry (base===null - just fielding, or a cutoff leg;
          // or base!==null but a different fielder receiving a relay, never
          // touched the ball) is one leg, unchanged. e.base is only ever
          // non-null for a base leg, so a cutoff-touching flight.fielder
          // (kind "pos") always falls through to the single-leg branch below.
          var unassisted = e.pos === flight.fielder && e.base !== null;
          var legs;
          if (unassisted) {
            var baseFt = BASE_POS_FT[e.base];
            var baseSvg = e.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[e.base];
            if (!baseFt || !baseSvg) return;
            legs = [
              { toSvg: pickupSvg, distFt: Math.hypot(pickupFt.x - anchor.x, pickupFt.y - anchor.y) },
              { toSvg: baseSvg, distFt: Math.hypot(baseFt.x - pickupFt.x, baseFt.y - pickupFt.y) },
            ];
          } else {
            // Task 8.3: a cutoff leg's destination is its own derived spot,
            // not a named base.
            var destFt = e.kind === "pos" ? e.cutoffFt : (e.base === null ? pickupFt : BASE_POS_FT[e.base]);
            var destSvg = e.kind === "pos" ? (e.cutoffFt ? ftToSvg(e.cutoffFt.x, e.cutoffFt.y) : null)
              : (e.base === null ? pickupSvg : (e.base === "HOME" ? SCENE_BASES.HOME : SCENE_BASES[e.base]));
            if (!destFt || !destSvg) return;
            legs = (e.pos === "P" && e.base === "1B")
              ? pitcherCover1BLegs(anchor, destFt, destSvg)
              : [{ toSvg: destSvg, distFt: Math.hypot(destFt.x - anchor.x, destFt.y - anchor.y) }];
          }
          moving[e.pos] = 1;
          // Task 5 (facts 18/24): ground archetypes now compress the
          // fielding run to fieldedMs (the charge race's own answer),
          // exactly as a fly out already compresses to ballTravelMs -
          // previously only the air case got a deadline at all. Scoped to
          // the ball-toucher's own single-leg entry (e.pos===flight.fielder
          // AND e.base===null - Task 8 note: a cutoff-RECEIVING entry can
          // also have base===null now, e.g. an SS relay man's own entry, so
          // the pos check is load-bearing here, not redundant) same as
          // before; the merged unassisted case (e.base!==null on this same
          // pos) is left undeadlined on purpose - movingFielderTokenHtml
          // would compress the whole 2-leg run proportionally, over-
          // compressing leg 2's honest bag-run, not just leg 1's fielding
          // run. Acceptable v1 (no per-leg deadlines built).
          var deadlineMs = (e.pos === flight.fielder && e.base === null)
            ? startDelay + (isAir ? ballTravelMs(flight) : fieldedMs(flight)) : null;
          // Task 2a point 3: the ball-touching entry (and only that one -
          // relay receivers never read the pitch) carries the same OF
          // read-delay the solo branch below always applied - returns 0 for
          // every non-qualifying play, so infield/out-play chains are
          // unaffected.
          var entryReadDelayMs = (e.pos === flight.fielder) ? ofReadDelayMs(m, flight, anchor) : 0;
          // Task 3 point 1: the ball-touching entry renders on the same
          // "pursuit" profile ofPursuitDeficitMs/the race already use when
          // it qualifies - previously the picture always used "run" while
          // the race used "pursuit", disagreeing about the same fielder's
          // real arrival. Every other entry (a relay receiver) keeps "run".
          var entryKind = (e.pos === flight.fielder && ofPursuitApplies(m, flight)) ? "pursuit" : "run";
          moversHtml += movingFielderTokenHtml(m, e.pos, legs, startDelay + entryReadDelayMs, anchor, deadlineMs, entryKind);
        });
      } else if (flight.fielder) {
        // Fallback only now (Task 2a point 4): fires when outThrowTargets
        // found nothing at all - a plain hit (single/double/triple) with no
        // explicit ThrowOrder and no recorded out - so there's no chain to
        // build; just the one fielder who ends up with the ball (unchanged
        // from the original single-mover behaviour).
        var soloAnchor = fielderStartAnchorFt(flight.fielder, flight, m);
        var fieldedFt = fieldedPoint(flight);
        if (soloAnchor) {
          moving[flight.fielder] = 1;
          var soloDistFt = Math.hypot(fieldedFt.x - soloAnchor.x, fieldedFt.y - soloAnchor.y);
          // The honest pursuit's own read-delay knob (Task 5b point 3) - a
          // bounded late break, applied on top of the play's own shared
          // startDelay, only when the honest race (from wherever this
          // fielder actually starts, real or shaded) would otherwise beat
          // the ball. 0 for every out-of-the-outfield/non-qualifying play.
          var readDelayMs = ofReadDelayMs(m, flight, soloAnchor);
          // Task 5 point 2: a ground-archetype hit charge race also
          // compresses to fieldedMs here - the bunt/infield-single hit
          // charge, never an OF pursuit hit (that late arrival is intended,
          // fact 11; Task 3 owns its throw side, not this token deadline).
          var soloDeadlineMs = GROUND_ARCHETYPES[archetype] ? startDelay + fieldedMs(flight) : null;
          // Task 3 point 1: same profile the race (ofPursuitDeficitMs) and
          // fielderBallArrivalMs already use - this is the main path a
          // pursuit-qualifying double/triple/1BWH single actually renders
          // through (no explicit ThrowOrder to route it into the chain
          // branch above).
          var soloKind = ofPursuitApplies(m, flight) ? "pursuit" : "run";
          moversHtml += movingFielderTokenHtml(m, flight.fielder,
            [{ toSvg: ftToSvg(fieldedFt.x, fieldedFt.y), distFt: soloDistFt }], startDelay + readDelayMs, soloAnchor, soloDeadlineMs, soloKind);
        }
      }
    }
    var driftTargetFt = (flight && !flight.clearedFence) ? fieldedPoint(flight) : null;
    var idleHtml = allPositions.filter(function (pos) { return !moving[pos]; }).map(function (pos) {
      var startFt = fielderStartAnchorFt(pos, flight, m);
      var leg = (driftTargetFt && !IDLE_DRIFT_EXCLUDED_POSITIONS[pos]) ? idleDriftLeg(pos, driftTargetFt, startFt, m, flight) : null;
      return leg ? movingFielderTokenHtml(m, pos, [leg], startDelay, startFt) : idleFielderTokenHtml(pos, startFt);
    }).join("");
    return idleHtml + moversHtml;
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
  //     an air catch); a ground ball out labels at fieldedPoint(flight) -
  //     the real pickup spot after any roll/charge-in, same point
  //     fielderTokensHtml's own convergence animation already targets -
  //     rather than that position's fixed starting depth (Alex's ask: reads
  //     as "here's where they actually made the play," not just "here's
  //     roughly where a fielder in this position usually stands"). The
  //     pairwise de-overlap sweep just below still applies regardless of
  //     which anchor a label came from, so two labels landing close
  //     together (a charge-in fielded well off their normal depth, say)
  //     still get nudged apart the same as before.
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

  function fielderNameLabelsHtml(m, flight, seqDelay) {
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
    var delay = (hasGroundPhase ? fieldedMs(flight) : ballTravelMs(flight)) + (seqDelay || 0);
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
          var picked = fieldedPoint(flight);
          pt = ftToSvg(picked.x, picked.y);
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

  // On-deck slide only (Alex's ask): the base occupants shown ahead of a
  // plate appearance that hasn't happened yet have no fielding chain to
  // anchor off (fielderNameLabelsHtml above is entirely play-driven), just
  // a static base to sit above - same .fielder-name treatment (font/halo)
  // as a real play's fielder labels, single-line, no result line stacked
  // (there's no result yet). on_base_runners (key_moments_build.py's own
  // _trace_base_occupants) is [full_name, last_name, spd] per occupied
  // base, same shape the defense dict already uses, omitted entirely rather
  // than an empty object when nobody's on - no batter label here, Alex's
  // call, this is base occupants only. delay:0 - nothing else on this slide
  // animates in on a stagger for these to match.
  function onDeckRunnerLabelsHtml(m) {
    var runners = m.on_base_runners;
    if (!m.is_on_deck || !runners) return "";
    return ["1B", "2B", "3B"].map(function (base) {
      var entry = runners[base];
      var pt = entry && SCENE_BASES[base];
      if (!pt) return "";
      return '<text class="fielder-name" x="' + pt.x.toFixed(1) + '" y="' + pt.y.toFixed(1) +
        '" dy="-' + LABEL_ANCHOR_OFFSET_PX + '" style="--delay:0ms">' + escapeHtml(entry[1]) + "</text>";
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

  // import_BRC.csv's optional ThrowOrder column (Task 8.1 - the migrated
  // cutoff-capable grammar, one character per leg): h/f/s/t -> HOME/1B/2B/3B
  // (base legs - a real out/safe target, reconciled against the runner
  // exactly as before); 1-9 -> standard scorekeeping position numbers
  // (P,C,1B,2B,3B,SS,LF,CF,RF - built from POSITION_NUMBER's own map, not a
  // separate table) -> position/cutoff legs, decorative only, no runner to
  // reconcile against. Case-insensitive on the base letters. Superseded the
  // old digit-only alphabet (1=1B/2=2B/3=3B/4=HOME) - see
  // tools/migrate_throw_order.py, run once against import_BRC.csv.
  var THROW_ORDER_BASE_LETTER = { h: "HOME", f: "1B", s: "2B", t: "3B" };
  var THROW_ORDER_POSITION_NUMBER = {};
  Object.keys(POSITION_NUMBER).forEach(function (pos) {
    THROW_ORDER_POSITION_NUMBER[String(POSITION_NUMBER[pos])] = pos;
  });
  // Returns an ordered list of typed legs - {kind:"base", base:"HOME"} or
  // {kind:"pos", pos:"SS"} - never a bare base array (every consumer reads
  // typed legs now; baseLegs() below is the "I only care about bases"
  // helper for the many that do). ",", " ", "-" are stripped as
  // separators; any OTHER character makes the WHOLE value invalid - a
  // stray digit under this alphabet is a plausible wrong leg, not noise
  // the old digit-only parser could safely ignore, so this warns and
  // rejects (falls back to the heuristic) rather than silently dropping
  // just that character.
  function parseThrowOrder(raw) {
    if (raw == null) return null;
    var stripped = String(raw).replace(/[,\s-]/g, "");
    if (!stripped) return null;
    var legs = [];
    for (var i = 0; i < stripped.length; i++) {
      var ch = stripped.charAt(i);
      var lower = ch.toLowerCase();
      if (THROW_ORDER_BASE_LETTER[lower]) {
        legs.push({ kind: "base", base: THROW_ORDER_BASE_LETTER[lower] });
      } else if (THROW_ORDER_POSITION_NUMBER[ch]) {
        legs.push({ kind: "pos", pos: THROW_ORDER_POSITION_NUMBER[ch] });
      } else {
        if (typeof console !== "undefined" && console.warn) {
          console.warn("[gameday ThrowOrder] invalid character in ThrowOrder value, falling back to the heuristic:", raw);
        }
        return null;
      }
    }
    var last = legs[legs.length - 1];
    if (last.kind !== "base" && typeof console !== "undefined" && console.warn) {
      console.warn("[gameday ThrowOrder] chain doesn't end in a base leg - decorative-only, no runner to reconcile:", raw);
    }
    return legs;
  }
  // The many consumers that only care about which real bases a chain
  // touches (out-count capping, runner-arrival lookups, scorecard relay
  // bases, ...) - position/cutoff legs carry no base of their own.
  function baseLegs(legs) {
    return (legs || []).filter(function (l) { return l.kind === "base"; }).map(function (l) { return l.base; });
  }
  // Task 8.3 (Alex's call): a cutoff man stands on the straight line from
  // where the ball was fielded to the base he'll eventually relay to, at a
  // constant fraction of that distance - not dynamic mid-flight
  // redirection (out of scope, round 1). Abstracted into its own constant
  // so the fraction is a one-line tune, not a hunt through the geometry.
  var CUTOFF_POSITION_FRAC = 0.5;
  function cutoffSpotFt(originFt, targetBaseFt) {
    if (!originFt || !targetBaseFt) return null;
    return {
      x: originFt.x + (targetBaseFt.x - originFt.x) * CUTOFF_POSITION_FRAC,
      y: originFt.y + (targetBaseFt.y - originFt.y) * CUTOFF_POSITION_FRAC,
    };
  }
  // The chain's own eventual base - the LAST base-typed leg, scanning from
  // the end (a chain normally ends in exactly one base leg; this is
  // defensive against an unusual shape rather than a real expected case).
  function finalBaseOfChain(legs) {
    for (var i = (legs || []).length - 1; i >= 0; i--) {
      if (legs[i].kind === "base") return legs[i].base;
    }
    return null;
  }

  // import_BRC.csv's optional per-position ThrowOrder_* columns - a specific
  // outfielder's own column (ThrowOrder_LF/CF/RF) beats the older, coarser
  // "any outfielder" ThrowOrder_OF column, which is kept alongside them
  // rather than replaced (non-destructive - existing ThrowOrder_OF rows keep
  // working unchanged; a situation can fill in the specific position, the
  // coarse one, or both). Infield/battery positions have no coarser fallback
  // of their own, just their one column. Returns candidate keys in the order
  // they should be tried, most specific first.
  function throwOrderCandidateKeys(pos) {
    return OF_POSITIONS[pos] ? [pos, "OF"] : [pos];
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
     column; a specific outfielder's own column beats the coarser ThrowOrder_OF)
     beats the generic ThrowOrder column, which beats the heuristic. */
  function outThrowTargets(m, moves, flight) {
    if (!flight || flight.clearedFence) return [];
    var byPosition = m.throw_order_by_position;
    var explicit = null;
    if (byPosition) {
      var candidateKeys = throwOrderCandidateKeys(flight.fielder);
      for (var i = 0; i < candidateKeys.length && !explicit; i++) {
        explicit = parseThrowOrder(byPosition[candidateKeys[i]]);
      }
    }
    explicit = explicit || parseThrowOrder(m.throw_order);
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

    // infieldSingleThrowHtml's one surviving job (Task 1a, probe 0.2),
    // folded in here: with no explicit ThrowOrder and no recorded out to
    // derive a target from, an infield single still gets a decorative,
    // always-loses throw to 1B - the general schedule (contestedSafe against
    // the batter's own arrival) draws it from here on. Scoped to
    // infield_single only, exactly as the old function was - a bunt stays
    // throw-less.
    if (!sorted.length && flight.archetype === "infield_single") {
      sorted.push("1B");
    }

    // The heuristic only ever derives real out/safe bases (never a cutoff -
    // that's explicit-ThrowOrder-only data, Task 8) - wrap as typed base
    // legs so every consumer reads one consistent typed-leg contract
    // regardless of which path produced it.
    return sorted.map(function (b) { return { kind: "base", base: b }; });
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
         covers instead), or this is the only relay leg on the play AND the
         honest race (firstBaseCoverage, gameday reconciliation plan 5a) says
         he genuinely can't get back to the bag in time - a real hard-hit
         roller he had to range far for, not a fixed lattice angle (measured
         against the real physics: the old 77-vs-85deg bucket split wasn't
         actually the signal - both sit close enough to his own anchor that
         a routine play always has him back with seconds to spare; what
         actually stranded him was a ball that kept rolling well past his
         own depth before the charge race caught it). Scoped to a
         single-leg play on purpose (Alex's ask, after a bogus 3-6-1 report):
         on a 3-6-3 double play the pitcher never covers first on the RETURN
         throw - the first baseman has had the full time of the SS relay to
         jog back to his own bag, so relayCount>1 always falls through to
         "1B" covers himself instead.
       - 2B is decided by which side of the infield the ball was hit to, NOT
         by who actually fielded it (so a slow roller fielded by the pitcher
         or a charging outfielder still gets a sensible coverer): angle<45
         (the 3B/SS side) -> 2B covers; angle>=45 (dead centre through the
         1B side) -> SS covers. The >=45 half deliberately folds in the
         45deg comebacker-to-the-pitcher tie case, not just the literal
         right side, per Alex's call.
         Fact 22 (gameday-animation-refinements round): that spray-side rule
         answers "which infielder is closer", but on a throw FROM an
         outfielder it's real defensive convention, not proximity, that
         decides - LF's throw to 2B is always taken by the second baseman
         (2B has to hold the bag facing a throw from the batter's left);
         CF/RF's is always taken by the shortstop (mirror reasoning, from
         the batter's right/dead-center). Keyed off the thrower's own
         position when it's an OF; the angle rule stays exactly as before
         for every non-OF thrower. The two rules agree in the common case
         (LF-side balls already have angle<45, RF/CF-side already >=45) -
         the only visible delta is a fielding OF whose identity crosses the
         spray-side boundary (e.g. CF ranging left of 45deg).
     fieldingNotation collapses a fielder covering their own next base right
     back down to a single (unassisted) touch - see there for why that's the
     general unassisted rule rather than a separate angle check.
     m/flight (optional): needed only for the 1B race above - every other
     branch is a pure function of the scalar params, so a caller that can't
     supply them (none currently) still gets a sane fallback via
     firstBaseCoverage's own defensive angle-77 check. */
  function coveringPosition(base, archetype, angle, fielderPos, relayCount, m, flight) {
    if (base === "HOME") return "C";
    if (base === "3B") return (archetype === "bunt" && fielderPos === "3B") ? "SS" : "3B";
    if (base === "1B") {
      if (archetype === "bunt" && fielderPos === "1B") return "2B";
      if (fielderPos === "1B" && relayCount === 1 && firstBaseCoverage(m, flight) === "P") return "P";
      return "1B";
    }
    if (base === "2B") {
      if (OUTFIELD_POSITIONS[fielderPos]) return fielderPos === "LF" ? "2B" : "SS";
      return angle < 45 ? "2B" : "SS";
    }
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
  // Task 8.2: the prefix of a typed-leg chain covering the first n REAL
  // (base) legs, including any position/cutoff legs along the way - a
  // plain array slice(0,n) would miscount once position legs are mixed in
  // (out flags map onto base legs only, never position legs). Stops right
  // after the nth base leg; nothing after it (base or position) is
  // included.
  function firstRealLegs(legs, n) {
    var result = [];
    var baseCount = 0;
    for (var i = 0; i < legs.length && baseCount < n; i++) {
      result.push(legs[i]);
      if (legs[i].kind === "base") baseCount++;
    }
    return result;
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
    var relayLegs = firstRealLegs(outThrowTargets(m, moves, flight), realOutThrowCount(m, flight));
    var relayBaseCount = baseLegs(relayLegs).length;

    // Task 8.2: a position/cutoff leg joins the chain as a touch of its own
    // (a 9-6-2 relay reads "9-6-2" when it records an out) - base is left
    // null for it, same as the ball-toucher's own first entry, since there's
    // no bag to anchor a receiving-fielder label at (v1: falls back to the
    // position's own nominal spot).
    var chain = [{ pos: flight.fielder, base: null }];
    relayLegs.forEach(function (leg) {
      if (leg.kind === "pos") {
        chain.push({ pos: leg.pos, base: null });
      } else {
        chain.push({ pos: coveringPosition(leg.base, archetype, flight.angle, flight.fielder, relayBaseCount, m, flight), base: leg.base });
      }
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
      // No wasOut gate on the ground-archetype branch (Alex's ask): a
      // grounder-family archetype's real out codes (GO/DP/FC/...) always
      // have wasOut=true already, so this is a no-op change for them, but a
      // bunt/infield_single HIT (B1B, IF1B - wasOut=false by definition) now
      // races the same charge-in system instead of falling to resolveHit
      // Pickup's plain dirt-edge cap below - the fielder genuinely gets a
      // shot at it and is just too late, rather than the ball only ever
      // appearing already sitting dead at the fringe.
      if (GROUND_ARCHETYPES[flight.archetype]) {
        resolveGrounderInterception(m, flight, hand);
      } else if (wasOut && CAUGHT_IN_AIR[flight.archetype]) {
        applyAirPositionOverride(m, flight, hand);
      } else if (flight.archetype === "single") {
        // Outfielders race a single too (Alex's ask), scoped to just this
        // one archetype - a double/triple/HR needs to genuinely get past
        // the nearest fielder to track with its own already-locked result.
        resolveSinglePickup(m, flight, hand);
      } else if (!flight.clearedFence) {
        resolveHitPickup(flight);
      }
    }
    var notation = fieldingNotation(m, flight);
    m._fieldingNotation = notation;
    return notation;
  }

  /* Chains a relay's throws end-to-end: throw i doesn't start drawing until
     throw i-1 has actually landed (endMs) plus a catch-and-transfer beat
     (THROW_STAGGER_MS) - never overlapping, so a double play visibly reads
     as thrown, caught, THEN thrown again, not two dashed lines animating at
     once (Alex's ask). Only the first throw's start is a caller-supplied
     anchor; every throw after it is fully determined by the one before.
     drawMsFor(i, leg), when given, picks each throw's own duration (see
     throwSchedule's real-distance closures below) - defaults to the flat
     THROW_DRAW_MS when omitted. drawMsFor may return a bare ms number
     (legacy/simple callers) or, since Task 9.3/9.4, an object
     {drawMs, distFt, throwerPos, toFt} - these ride along on the schedule
     entry itself so reconcileThrowSchedule's slowThrow knob (distFt/
     throwerPos) and the renderer's own endpoint (toFt, Task 8.3's cutoff
     spot) don't need extra parallel arrays threaded through every caller.
     targets: typed legs (Task 8.2) - base is set only for a base leg
     (never a position/cutoff leg); realCount counts against base legs
     ONLY, in order (position legs are never outs - out flags would
     otherwise miscount once the two kinds are interleaved). */
  function sequentialThrowSchedule(targets, firstStartMs, realCount, drawMsFor) {
    var prevEnd = null;
    var baseLegIndex = 0;
    return targets.map(function (leg, i) {
      var start = i === 0 ? firstStartMs : prevEnd + THROW_STAGGER_MS;
      var picked = drawMsFor ? drawMsFor(i, leg) : THROW_DRAW_MS;
      var isObj = picked && typeof picked === "object";
      var draw = isObj ? picked.drawMs : picked;
      var end = start + draw;
      prevEnd = end;
      var isBase = leg.kind === "base";
      var out = isBase && baseLegIndex < realCount;
      if (isBase) baseLegIndex++;
      return {
        base: isBase ? leg.base : null, pos: isBase ? null : leg.pos, kind: leg.kind,
        startMs: start, endMs: end, drawMs: draw, out: out,
        distFt: isObj ? picked.distFt : undefined,
        throwerPos: isObj ? picked.throwerPos : undefined,
        toFt: isObj ? picked.toFt : undefined,
      };
    });
  }

  // Task 10 (facts 15/5, active bug): runners must not visibly pass each
  // other on a shared basepath. A pure pre-pass, single-sourced into both
  // the token render (sceneFieldHtml) and the reconciler's runner-arrival
  // lookups (safeRunnerArrivalMs/forcedOutRunnerArrivalMs, below) - see the
  // fix-map for how each side folds the result in.
  //
  // Scope (deliberately narrower than sceneFieldHtml's full mvDelay tree):
  // batted-ball plays only (flight truthy - a steal has no shared basepath
  // risk in this sense), excluding retreat (a scramble-back, not a forward
  // leg to collide on) and stranded-safe walk-offs (dugout-bound, not
  // basepath movement). Reproduces the SAME mvDelay/legs/legDurMs formula
  // forcedOutRunnerArrivalMs/safeRunnerArrivalMs already use for their own
  // respective out/safe cases - not a parallel model, the same one.
  function runnerMoveTiming(m, flight, moves, mv) {
    if (!flight || mv.retreat) return null;
    var isOut = mv.to === "OUT";
    var strandedSafe = !isOut && !mv.scored && !!m.is_half_inning_final;
    if (strandedSafe) return null;
    var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
    var runDelay = RUNNER_LEAD_MS; // flight is truthy here (guarded above)
    var mvDelay, endOrd;
    if (isOut) {
      var forced = FORCED_OUT_BASE[m.result];
      var candidate = forced === "OWN" ? mv.from : (forced || NEXT_BASE[mv.from] || mv.from);
      var realOutTargets = baseLegs(outThrowTargets(m, moves, flight));
      var forcedBase = realOutTargets.indexOf(candidate) !== -1 ? candidate : null;
      if (!forcedBase) return null; // uncorroborated - no real forward leg, same guard basepathWaypoints itself applies
      endOrd = forcedBase === "HOME" ? 4 : BASE_ORDINAL[forcedBase];
      var before = String(m.obc_before || "000");
      var forcedOnContact = FORCE_TIMING_RESULTS[m.result] && isForcedRunner(mv.from, before);
      var outDelay = ballTravelMs(flight) + OUT_BEAT_MS;
      mvDelay = forcedOnContact ? runDelay : outDelay;
    } else {
      endOrd = mv.scored ? 4 : BASE_ORDINAL[mv.to];
      var catchMs = CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
      mvDelay = catchMs ? catchMs + TAG_UP_MS : runDelay;
    }
    if (startOrd == null || endOrd == null || endOrd <= startOrd) return null;
    var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
    return { mvDelay: mvDelay, startOrd: startOrd, endOrd: endOrd, legDurMs: runnerLegMs(m, mv.from, legs) };
  }
  // A mover's own base-ordinal position at time t (raw units, same
  // pre-seqDelay convention as mvDelay itself) - flat at startOrd before
  // mvDelay, linear through the leg, flat at endOrd once arrived. delayMs/
  // paceScale (both default to none) apply a candidate trailLateBreak/
  // trailSlowPace correction on top, without mutating the timing object -
  // resolvePassing below evaluates many candidates cheaply this way.
  function runnerOrdinalAt(timing, delayMs, paceScale, t) {
    var mvDelay = timing.mvDelay + (delayMs || 0);
    var legDurMs = timing.legDurMs * (paceScale || 1);
    if (t <= mvDelay) return timing.startOrd;
    if (t >= mvDelay + legDurMs) return timing.endOrd;
    return timing.startOrd + (t - mvDelay) / legDurMs * (timing.endOrd - timing.startOrd);
  }
  var RUNNER_MIN_GAP_ORD = 0.1; // ~9ft - close enough to read as "right behind", not "level with"
  // Both movers are piecewise-linear in ordinal-vs-time (flat/ramp/flat), so
  // their difference is piecewise-linear too - its minimum over the whole
  // timeline occurs at one of the two curves' own breakpoints (start/end of
  // each ramp). Checking those 4 candidate times is sufficient; no need to
  // sample the continuous timeline.
  function minPassingGap(leadT, trailT, delayMs, paceScale) {
    var trailStart = trailT.mvDelay + (delayMs || 0);
    var trailEnd = trailStart + trailT.legDurMs * (paceScale || 1);
    var candidates = [leadT.mvDelay, leadT.mvDelay + leadT.legDurMs, trailStart, trailEnd];
    var min = Infinity;
    candidates.forEach(function (t) {
      var gap = runnerOrdinalAt(leadT, 0, 1, t) - runnerOrdinalAt(trailT, delayMs, paceScale, t);
      if (gap < min) min = gap;
    });
    return min;
  }
  // Resolution, bounded and named, applied to the TRAILING runner only, in
  // order (Task 10 point 2): trailLateBreak (delay their start, reusing the
  // reconciler's own RUNNER_LATE_JUMP_MAX_MS bound) first; trailSlowPace
  // (stretch their leg duration, reusing STRETCH_RUNNER_MAX_FRAC) only if a
  // late break alone can't close it; a residual that even the combined
  // bound can't cover renders anyway (verdicts are never at risk, only
  // positions) with a console.warn, same "say so instead of hiding it"
  // policy reconcileThrowSchedule's own unresolved case uses. Bisection
  // (same idiom ofDerivedShadeAnchorFt already uses above) finds the
  // smallest correction that closes the gap, not just the bound itself.
  function resolvePassing(leadT, trailT) {
    if (minPassingGap(leadT, trailT, 0, 1) >= RUNNER_MIN_GAP_ORD) return null;
    var delayMs = RUNNER_LATE_JUMP_MAX_MS;
    if (minPassingGap(leadT, trailT, RUNNER_LATE_JUMP_MAX_MS, 1) >= RUNNER_MIN_GAP_ORD) {
      var lo = 0, hi = RUNNER_LATE_JUMP_MAX_MS;
      for (var i = 0; i < 20; i++) {
        var mid = (lo + hi) / 2;
        if (minPassingGap(leadT, trailT, mid, 1) >= RUNNER_MIN_GAP_ORD) hi = mid; else lo = mid;
      }
      delayMs = hi;
    }
    var paceScale = 1;
    if (minPassingGap(leadT, trailT, delayMs, 1) < RUNNER_MIN_GAP_ORD) {
      var maxScale = 1 + STRETCH_RUNNER_MAX_FRAC;
      paceScale = maxScale;
      if (minPassingGap(leadT, trailT, delayMs, maxScale) >= RUNNER_MIN_GAP_ORD) {
        var slo = 1, shi = maxScale;
        for (var j = 0; j < 20; j++) {
          var smid = (slo + shi) / 2;
          if (minPassingGap(leadT, trailT, delayMs, smid) >= RUNNER_MIN_GAP_ORD) shi = smid; else slo = smid;
        }
        paceScale = shi;
      } else if (typeof console !== "undefined" && console.warn) {
        console.warn("[gameday runner-passing] could not fully close a passing gap even at the bound - " +
          "upstream inputs need a look, rendering with the bounded correction anyway");
      }
    }
    return { delayMs: Math.round(delayMs), paceScale: paceScale };
  }
  // The pre-pass itself: every same-direction pair (a lead mover who started
  // further along the bases, a trailing mover who started behind) on this
  // play's own shared basepath gets checked; the trailing runner's own
  // correction (the largest needed across every lead it might otherwise
  // pass, if more than one) is returned keyed by mv.from. Two movers ending
  // at the SAME base (only possible at HOME, both scoring - Task 1b's own
  // case) are excluded here: "levelling up" at a shared destination is the
  // real outcome, not a passing violation to correct away.
  function runnerPassingAdjustments(m, flight, moves) {
    var result = {};
    if (!flight) return result;
    var timings = moves.map(function (mv) {
      return { mv: mv, timing: runnerMoveTiming(m, flight, moves, mv) };
    }).filter(function (t) { return t.timing != null; });
    for (var i = 0; i < timings.length; i++) {
      for (var j = 0; j < timings.length; j++) {
        if (i === j) continue;
        var lead = timings[i], trail = timings[j];
        if (lead.timing.startOrd <= trail.timing.startOrd) continue;
        if (lead.timing.endOrd === trail.timing.endOrd) continue;
        var adj = resolvePassing(lead.timing, trail.timing);
        if (!adj) continue;
        var existing = result[trail.mv.from];
        result[trail.mv.from] = existing
          ? { delayMs: Math.max(existing.delayMs, adj.delayMs), paceScale: Math.max(existing.paceScale, adj.paceScale) }
          : adj;
      }
    }
    return result;
  }

  // Which tracked move (if any) a real out-throw target base corroborates -
  // the exact forced-base redirect outThrowTargets itself used to produce
  // targetBase, factored out so both the reconciler's runner-arrival lookup
  // and its own runner-identity tag (Stage 4 - who a runnerLateJump/
  // stretchRunner adjustment applies to) read one answer. Falls back to the
  // batter (untracked by deriveRunnerMoves) only in the one case
  // outThrowTargets does the same - an unaccounted non-FC-family out at 1B;
  // recorded>0 guards that (Alex's ask, closing a real-if-harmless latent
  // overreach): every actual call site already only reaches this function
  // when there's a genuine recorded out (throwSchedule only calls it for a
  // lastLeg.out===true leg, which by construction requires recorded>0), so
  // this was never wrong in production - but called directly with a
  // zero-out hit (now possible - see runnerForSafeTarget's own use of
  // targetBase "1B"), the un-guarded version would have wrongly claimed the
  // batter was thrown out at first on a play where nobody's out at all.
  function runnerForOutTarget(m, moves, targetBase) {
    var forced = FORCED_OUT_BASE[m.result];
    var mv = null;
    moves.forEach(function (x) {
      if (x.to !== "OUT" || mv) return;
      var candidate = forced === "OWN" ? x.from : (forced || NEXT_BASE[x.from] || x.from);
      if (candidate === targetBase) mv = x;
    });
    var recorded = (m.outs_after || 0) - (m.outs_before || 0);
    if (!mv && recorded > 0 && targetBase === "1B" && !BATTER_REACHES_FIRST[m.result]) mv = { from: "BATTER" };
    return mv;
  }
  // Honest arrival of the specific runner a real out-throw target base
  // corroborates - the exact mvDelay/legDurMs formula sceneFieldHtml's own
  // per-move timing already uses (forcedOnContact leaves on contact; every
  // other out-bound runner waits for fieldedMs/OUT_BEAT_MS), factored out so
  // throwSchedule's reconciler and the renderer never compute two different
  // answers to "when does this runner get there" for the same play. Returns
  // null when no real runner resolves to this base (the rare caught-line-
  // drive/decorative-HOME edge, or the FC family's own untracked forced
  // runner) - callers must treat that as "nothing honest to reconcile
  // against," not "runner arrives at time zero."
  function forcedOutRunnerArrivalMs(m, flight, moves, targetBase) {
    var mv = runnerForOutTarget(m, moves, targetBase);
    if (!mv) return null;
    var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
    var endOrd = targetBase === "HOME" ? 4 : BASE_ORDINAL[targetBase];
    if (startOrd == null || endOrd == null || endOrd <= startOrd) return null;
    var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
    var runDelay = flight ? RUNNER_LEAD_MS : 0;
    var outDelay = (flight ? ballTravelMs(flight) : 0) + OUT_BEAT_MS;
    var before = String(m.obc_before || "000");
    var forcedOnContact = FORCE_TIMING_RESULTS[m.result] && isForcedRunner(mv.from, before);
    var mvDelay = forcedOnContact ? runDelay : outDelay;
    // Task 10: single-sourced no-passing correction, if this runner needed
    // one to keep from visibly passing a lead runner still ahead of them.
    var passAdj = runnerPassingAdjustments(m, flight, moves)[mv.from];
    var legDurMs = runnerLegMs(m, mv.from, legs) * (passAdj ? passAdj.paceScale : 1);
    return mvDelay + (passAdj ? passAdj.delayMs : 0) + legDurMs;
  }

  // The SAFE-side sibling of runnerForOutTarget - which tracked move (if
  // any) actually reached targetBase safely, for a decorative/contestedSafe
  // throw leg on a play where nobody's out at all (an explicit ThrowOrder on
  // a plain hit, closing a real gap: realOutThrowCount is 0 for a hit, so
  // there's no OUT-typed move to find the runner from at all - without this,
  // that kind of throw rendered from pure forward physics with no
  // reconciliation, no guarantee it wouldn't visibly beat a runner who was
  // never in any danger). Matches whichever move's own real destination is
  // targetBase (scored counts as reaching HOME) - for a normal hit,
  // deriveRunnerMoves already tracks the batter's own BATTER->base move, so
  // this resolves directly in the common case; the BATTER fallback below
  // only covers the same edge deriveRunnerMoves itself doesn't model (see
  // runnerForOutTarget's own comment).
  function runnerForSafeTarget(m, moves, targetBase) {
    var candidates = [];
    moves.forEach(function (x) {
      if (x.to === "OUT") return;
      var reached = x.scored ? "HOME" : x.to;
      if (reached === targetBase) candidates.push(x);
    });
    var mv = null;
    if (candidates.length > 1) {
      // Two runners can only share a destination when both score (Task 1b,
      // probe 0.3) - deriveRunnerMoves orders most-advanced-first, so a
      // plain first-match here returned the LEAD scorer (the earliest
      // arriver), letting a contestedSafe throw home reconcile against the
      // wrong (faster) runner and visibly beat the trailing one the result
      // says scored safely. Reconcile against whichever mover actually
      // arrives latest instead - same Math.max-over-every-safe-mover
      // principle throwSchedule's tag-throw branch already applies above.
      var bestMs = -1;
      candidates.forEach(function (x) {
        var startOrd = x.from === "BATTER" ? 0 : BASE_ORDINAL[x.from];
        var endOrd = targetBase === "HOME" ? 4 : BASE_ORDINAL[targetBase];
        if (startOrd == null || endOrd == null || endOrd <= startOrd) return;
        var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
        var ms = runnerLegMs(m, x.from, legs);
        if (ms > bestMs) { bestMs = ms; mv = x; }
      });
      if (!mv) mv = candidates[0];
    } else {
      mv = candidates[0] || null;
    }
    if (!mv && targetBase === "1B" && !moves.some(function (x) { return x.from === "BATTER"; })) {
      mv = { from: "BATTER" };
    }
    return mv;
  }
  // Honest arrival of the specific runner a decorative/contestedSafe throw
  // leg is chasing, for a play with no real out at all - the safe-side
  // sibling of forcedOutRunnerArrivalMs. No forcedOnContact distinction here
  // (that's specifically about a FORCED runner fleeing a force play, which
  // by definition doesn't apply when nobody's out): a safe runner on a
  // normal hit just runs at the shared RUNNER_LEAD_MS beat, the same
  // mvDelay sceneFieldHtml's own non-tag-up safe-move branch already uses.
  // runnerForSafeTarget now already resolves to the latest-arriving mover
  // when multiple runners share targetBase (Task 1b) - this stays a
  // straight arrival computation for whichever single mv that is.
  function safeRunnerArrivalMs(m, flight, moves, targetBase) {
    var mv = runnerForSafeTarget(m, moves, targetBase);
    if (!mv) return null;
    var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
    var endOrd = targetBase === "HOME" ? 4 : BASE_ORDINAL[targetBase];
    if (startOrd == null || endOrd == null || endOrd <= startOrd) return null;
    var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
    var runDelay = flight ? RUNNER_LEAD_MS : 0;
    // Task 10: single-sourced no-passing correction, same as
    // forcedOutRunnerArrivalMs's own fold-in above.
    var passAdj = runnerPassingAdjustments(m, flight, moves)[mv.from];
    var legDurMs = runnerLegMs(m, mv.from, legs) * (passAdj ? passAdj.paceScale : 1);
    return runDelay + (passAdj ? passAdj.delayMs : 0) + legDurMs;
  }

  /* Pure schedule (A4/A5): throw i originates at the ball's landing point;
     throw i+1 relays from throw i's target base. Kept separate from the
     rendering so the timing race against the runner can be asserted rather
     than eyeballed - see ball_flight_test.py.

     Gameday reconciliation plan (Task 4): every branch below now ends by
     handing its forward-computed schedule to the shared reconciler
     (reconcileThrowSchedule/holdChainTo) instead of a bespoke backward-solve
     or (on the plain out-throw race) no reconciliation at all - goal (a) was
     previously enforced only by the offline test sweep and
     runnerOutMotionHtml's runtime hold-at-bag fallback, never at the
     schedule level itself (plan fact 0.3). */
  function throwSchedule(m, moves, flight) {
    var targets = outThrowTargets(m, moves, flight);
    if (!targets.length) return [];
    // Which legs are a real out vs a decorative tag-up throw nobody's out on
    // (realOutThrowCount) - drives throwHtml's out/safe (red/green) colour,
    // same "which of these throws actually put someone out" question
    // fieldingNotation asks of the identical target list. Counted against
    // base legs only (sequentialThrowSchedule's own convention, Task 8.2) -
    // a position/cutoff leg is never an out.
    var realCount = realOutThrowCount(m, flight);
    // Task 8.3: every position/cutoff leg in this chain resolves to the
    // SAME derived spot - on the line from where the ball was fielded to
    // the chain's own eventual base, at CUTOFF_POSITION_FRAC. relayBaseCount
    // is the coveringPosition relayCount convention (base legs only, same
    // as fieldingChainDetail/relayLegIsUnassisted use it).
    var finalBaseLeg = finalBaseOfChain(targets);
    var finalBaseFt = finalBaseLeg ? BASE_POS_FT[finalBaseLeg] : null;
    var relayBaseCount = baseLegs(targets).length;
    function legPointFt(leg) {
      return leg.kind === "base" ? BASE_POS_FT[leg.base]
        : (finalBaseFt ? cutoffSpotFt(fieldedPoint(flight), finalBaseFt) : null);
    }
    function ptDistFt(a, b) {
      if (!a || !b || !isFinite(a.x) || !isFinite(b.x)) return null;
      return Math.hypot(b.x - a.x, b.y - a.y);
    }
    // Whoever covered/received the PREVIOUS leg throws this one - a base
    // leg resolves through the coverage convention, a position/cutoff leg
    // names its own receiver directly (coveringPosition bypassed).
    function throwerOf(leg) {
      return leg.kind === "pos" ? leg.pos
        : coveringPosition(leg.base, flight.archetype, flight.angle, flight.fielder, relayBaseCount, m, flight);
    }

    // A fly ball/pop-up's throw (SacF/DSacF/FO's decorative "throw home
    // anyway" - outThrowTargets appends it, or an explicit ThrowOrder
    // describes the same throw) never beats anyone - see TAG_THROW_ARCHETYPES.
    // The ball-fielding-only schedule below has no idea the runner had to
    // wait out the catch/tag-up before moving at all, so forward-computing
    // it would draw the throw a good 600ms+ before the runner had actually
    // crossed the plate - reconciled against the slowest safe runner's own
    // arrival as an "uncontested" event (throw must land comfortably late).
    if (flight && TAG_THROW_ARCHETYPES[flight.archetype]) {
      var catchMs = ballTravelMs(flight);
      var runnerArrival = 0;
      moves.forEach(function (mv) {
        if (mv.to === "OUT") return;
        var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
        var endOrd = mv.scored ? 4 : BASE_ORDINAL[mv.to];
        if (startOrd == null || endOrd == null || endOrd <= startOrd) return;
        var legs = Math.min(endOrd - startOrd, RUN_LEG_MS.length - 1);
        runnerArrival = Math.max(runnerArrival, catchMs + TAG_UP_MS + runnerLegMs(m, mv.from, legs));
      });
      // Forward pass: draws at the flat THROW_DRAW_MS/BASE_DIAG_FT model
      // (this decorative throw has no real fielded-point origin to draw
      // real distance from), released after the same catch-to-transfer beat
      // every other throw waits out - the reconciler's own holdRelease then
      // does the rest of the "land comfortably past the runner" work,
      // replacing the old direct backward-solve.
      var tagSchedule = sequentialThrowSchedule(targets, catchMs + THROW_DELAY_MS, realCount, function (i, leg) {
        // Task 9.3: leg 0 has no real fielded-point origin to draw distance
        // from (comment above), but the THROWER is still known - draw the
        // same flat BASE_DIAG_FT model at their own position speed rather
        // than the generic THROW_SPEED_MPH.
        var throwerPos = i === 0 ? flight.fielder : throwerOf(targets[i - 1]);
        var mph = THROW_SPEED_BY_POS[throwerPos] && THROW_SPEED_BY_POS[throwerPos].mph;
        var toFt = legPointFt(leg);
        if (i === 0) return { drawMs: throwDrawMsForFt(BASE_DIAG_FT, mph), throwerPos: throwerPos, toFt: toFt };
        var dist = ptDistFt(legPointFt(targets[i - 1]), toFt);
        return dist == null
          ? { drawMs: THROW_DRAW_MS, throwerPos: throwerPos, toFt: toFt }
          : { drawMs: throwDrawMsForFt(dist, mph), distFt: dist, throwerPos: throwerPos, toFt: toFt };
      });
      tagSchedule.adjustments = reconcileThrowSchedule(tagSchedule, runnerArrival, "uncontested", m.diff, false).adjustments;
      return tagSchedule;
    }

    // A rolling grounder isn't fielded until the resolver's own ground time
    // has actually elapsed - the throw has to wait out that extra beat too,
    // or it'd draw from a spot the ball hasn't visibly reached yet (see
    // throwHtml's origin). fieldedMs (Part 7) replaces the old
    // ballTravelMs+rollMs sum with one physically-timed number.
    var base = fieldedMs(flight) + THROW_DELAY_MS;
    // Task 3 point 3 (facts 14/26): on an OF hit (resolveHitPickup-resolved
    // doubles/triples, an OF single), the rendered fielder can arrive at
    // fieldedPoint well after fieldedMs says the ball's at rest - fact 11's
    // intended pause. Only the THROW must wait for the honest picture;
    // fieldedMs alone used to let the throw draw from a spot the fielder
    // hadn't actually reached yet. Floors (never lowers) base at the same
    // fielderBallArrivalMs the token itself renders on, so the two can never
    // disagree. Scoped off air catches (their own ballTravelMs anchor
    // already accounts for the catch) and off infield charge races (their
    // own fieldedMs already IS the charge race's own arrival answer).
    if (OUTFIELD_POSITIONS[flight.fielder] && !CAUGHT_IN_AIR[flight.archetype]) {
      var honestArrivalMs = fielderBallArrivalMs(m, flight);
      if (honestArrivalMs != null) {
        base = Math.max(fieldedMs(flight), honestArrivalMs) + THROW_DELAY_MS;
      }
    }
    // Real distance per throw (Alex's ask - a relay leg must not take as
    // long as a full corner-to-first throw): throw 0 runs from the ball's
    // actual fielded spot (fieldedPoint), every throw after it from the
    // previous throw's own target base - both real, known points, so both
    // get their own accurate draw time. Falls back to the flat THROW_DRAW_MS/
    // BASE_DIAG_FT model only when the origin isn't resolvable (ball_flight_
    // test.py's minimal A4 timing-race flights, built without a fielder
    // position) - the same conservative number this whole model used
    // everywhere before this.
    var origin0 = fieldedPoint(flight);
    // A leg the same fielder covers themselves (relayLegIsUnassisted, Alex's
    // ask) draws at that fielder's own running pace, not a 90mph throw - a
    // 3B stepping on third himself before relaying to first (a 5-3 DP)
    // shouldn't cross that ground any faster than he'd actually run it.
    // Uses fielderLegDurationsMs - the exact same accelerating-run formula
    // (and real SPD) the fielder's own glove token draws with
    // (movingFielderTokenHtml) - not the flat, unaccelerated
    // runnerDrawMsForFt (Alex's report: after the acceleration model
    // landed, this line marker kept its old flat pace and started visibly
    // beating the glove token that's supposedly the one carrying it).
    var unassisted = relayLegIsUnassisted(m, targets, flight);
    // Task 9.3/8.2: leg 0's thrower is the ball-fielding fielder; every leg
    // after it is thrown by whoever covered/received the PREVIOUS leg
    // (throwerOf, above) - already resolved for the unassisted check below,
    // reused rather than recomputed for the mph lookup.
    var schedule = sequentialThrowSchedule(targets, base, realCount, function (i, leg) {
      var fromPt = i === 0 ? origin0 : legPointFt(targets[i - 1]);
      var toPt = legPointFt(leg);
      var dist = ptDistFt(fromPt, toPt);
      var throwerPos = i === 0 ? flight.fielder : throwerOf(targets[i - 1]);
      if (dist == null) return { drawMs: THROW_DRAW_MS, throwerPos: throwerPos, toFt: toPt };
      if (!unassisted[i]) {
        var mph = THROW_SPEED_BY_POS[throwerPos] && THROW_SPEED_BY_POS[throwerPos].mph;
        return { drawMs: throwDrawMsForFt(dist, mph), distFt: dist, throwerPos: throwerPos, toFt: toPt };
      }
      var coveringPos = throwerOf(leg);
      return { drawMs: fielderLegDurationsMs(m, coveringPos, [{ distFt: dist }])[0], distFt: dist, throwerPos: coveringPos, toFt: toPt };
    });

    // The actual bug fix (plan fact 0.3): the honest forward race against
    // the real runner corroborated by the FINAL leg's own target base - a
    // relay's earlier legs are real chained transfers, only the last leg
    // ever actually races anyone. A real out (schedule's own `out` flag)
    // reconciles as forceOut (throw must beat the runner, today's
    // previously-unenforced-at-runtime gap) against the OUT-side lookup; a
    // leg that isn't a real out - either one that fell outside
    // realOutThrowCount's own cap (a DP whose back end never actually
    // retired anyone), or realOutThrowCount is 0 for the whole play (an
    // explicit ThrowOrder on a plain hit - the general version of that same
    // gap: nobody's out at all, so there's no OUT-typed move to reconcile
    // against, only a SAFE one) - reconciles as contestedSafe instead
    // (throw must honestly lose) against the SAFE-side lookup. This is also
    // how a no-explicit-ThrowOrder infield single's own default 1B leg
    // (outThrowTargets' own default, Task 1a) resolves - folded in here
    // rather than the separate infieldSingleThrowHtml this used to be.
    var lastLeg = schedule[schedule.length - 1];
    var finalRunnerMv = lastLeg.out
      ? runnerForOutTarget(m, moves, lastLeg.base)
      : runnerForSafeTarget(m, moves, lastLeg.base);
    var finalRunnerArrival = lastLeg.out
      ? forcedOutRunnerArrivalMs(m, flight, moves, lastLeg.base)
      : safeRunnerArrivalMs(m, flight, moves, lastLeg.base);
    var scheduleAdjustments = [];
    if (finalRunnerArrival != null) {
      var recon = reconcileThrowSchedule(schedule, finalRunnerArrival,
        lastLeg.out ? "forceOut" : "contestedSafe", m.diff, !!lastLeg.out,
        finalRunnerMv && finalRunnerMv.from);
      scheduleAdjustments = scheduleAdjustments.concat(recon.adjustments);
    }

    // A genuine 3-1 (single relay leg straight to 1B, pitcher covering per
    // coveringPosition's own relayCount===1 gate) must not have the ball
    // visibly beat the pitcher there (Alex's bug report - it was arriving
    // while the pitcher's own token was still partway down the baseline).
    // Both timelines share the same t=0 (contact - fielderStartDelay's own
    // comment, and this function's own "seqDelay added only at the final
    // write" convention) - a second, independent holdRelease floor (not a
    // verdict margin - a "never render past a real token's own arrival"
    // constraint) applied after the runner-margin reconciliation above, so
    // whichever of the two actually requires more hold wins.
    if (targets.length === 1 && targets[0].kind === "base" && targets[0].base === "1B" && flight.fielder === "1B" &&
        coveringPosition("1B", flight.archetype, flight.angle, flight.fielder, 1, m, flight) === "P") {
      var pitcherAdj = holdChainTo(schedule, pitcherCover1BArrivalMs(m), "holdRelease",
        "the ball must not visibly beat the covering pitcher's own token to 1B");
      if (pitcherAdj) scheduleAdjustments.push(pitcherAdj);
    }
    // Stage 4 (sceneFieldHtml, fielderTokensHtml): the runnerLateJump/
    // stretchRunner knobs above don't move anything IN this array (they're
    // a render-layer concern - the runner token's own start delay/pace) -
    // attached here as a non-enumerable-ish extra property so `.map`/
    // `.length`/every existing consumer of this array keeps working
    // untouched, while a caller that wants to honor them can read
    // schedule.adjustments and look up its own runner's own entry by `who`.
    schedule.adjustments = scheduleAdjustments;
    return schedule;
  }

  /* When each throw-corroborated out actually happens - the moment its own
     throw lands (schedule[i].endMs), keyed by target base - shared by
     scorebugOutsHtml (the out-dot pops at this exact moment, Alex's ask)
     and sceneFieldHtml (the runner token turns red at this exact moment,
     same ask - "the corresponding batter/runner who is out" should flip red
     together with the dot, not on the old fixed 3333ms-into-the-run-
     choreography guess). Only covers outs a real throw resolves; a caught-
     in-the-air out or a no-flight out (K) has no entry here - callers fall
     back to the catch moment or the old flat beat themselves. */
  function outThrowEndByBase(m, moves, flight) {
    var schedule = throwSchedule(m, moves, flight);
    var map = {};
    schedule.forEach(function (t) { if (t.out) map[t.base] = t.endMs; });
    return map;
  }

  // The reconciler's own runnerLateJump/stretchRunner adjustments for this
  // play, keyed by runner (mv.from) - Stage 4's own read side of Task 4.4's
  // "these are a render-layer concern" note (throwSchedule computes them,
  // this is where a renderer actually asks for them). Sums both knobs per
  // runner (at most one of each ever fires per reconciliation - see
  // reconcileThrowSchedule's own ordering - so summing is just "the total
  // extra ms this runner's own token needs," not double-counting anything).
  function throwRunnerAdjustmentMs(m, moves, flight, who) {
    var schedule = throwSchedule(m, moves, flight);
    var adjustments = schedule.adjustments || [];
    var total = 0;
    adjustments.forEach(function (a) {
      if (a.who === who && (a.knob === "runnerLateJump" || a.knob === "stretchRunner")) total += a.ms;
    });
    return total;
  }

  /* When a caught-stealing out is actually recorded (the tag, not the
     throw's own release) - null when this play isn't one. A steal never has
     a ball flight, so runDelay is always 0 here, same as sceneFieldHtml's
     own runDelay/outDelay/delayedStartMs would independently compute for
     one; pulled out as its own function so scorebugOutsHtml's dot and
     sceneFieldHtml's runner token read it off one shared formula instead of
     the dot falling back to a generic (and, for this case, wrong) guess. */
  function stealOutAtMs(m, moves) {
    var stealOut = stealThrowTarget(m, moves);
    if (!stealOut || !stealOut.caught) return null;
    var outDelay = OUT_BEAT_MS;
    var delayedStartMs = outDelay + TAG_UP_MS;
    var stealOutDelay = stealOut.delay ? delayedStartMs : outDelay;
    return stealRunnerArrivalMs(m, stealOut.from, true, 0, stealOutDelay) + TAG_UP_MS;
  }

  // The .out-to-first keyframe's own "reaches first" checkpoint (style.css:
  // rnOutToBase, currently 78.72% of its 4233ms total - see that rule's own
  // comment) is where the batter reaches first (A4) - kept as one named
  // function so the throw-beats-runner assertion has a single source of
  // truth to check against. Was RUNNER_LEAD_MS + 0.4706*1700, a magic-
  // fraction-of-the-old-hardcoded-total indirection that happened to equal
  // RUN_LEG_MS[1] (800 at the time) - references it directly now, so this
  // stays correct automatically whenever RUN_LEG_MS changes instead of
  // silently drifting out of sync with a CSS percentage computed by hand
  // from a different constant.
  function batterFirstArrivalMs(m) {
    return RUNNER_LEAD_MS + runnerLegMs(m, "BATTER", 1);
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
  // showLine (optional, default true): Alex's ask - a decorative/safe throw
  // (nobody's actually out) reads as visual clutter with the dashed reveal
  // line on top of an already-busy runner+fielder scene; the animated ball
  // alone already tells the same story. Real out-throws keep the line -
  // callers pass showLine explicitly false only for the safe/uncontested
  // case (throwHtml's own t.out flag). The clip/line machinery is skipped
  // entirely rather than just hidden - the ball's own animation never
  // depended on it.
  function throwLineHtml(x1, y1, x2, y2, cls, startMs, drawMs, fadeAtMs, showLine) {
    var draw = drawMs || THROW_DRAW_MS;
    var clip = "", line = "";
    if (showLine !== false) {
      var len = Math.hypot(x2 - x1, y2 - y1) || 1;
      var angleDeg = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
      var id = "throwClip" + (THROW_CLIP_SEQ++);
      var clipVars = "--len:" + len.toFixed(1) + "px;--delay:" + startMs + "ms;--draw:" + draw + "ms";
      clip = '<clipPath id="' + id + '" clipPathUnits="userSpaceOnUse">' +
        '<rect class="throw-clip-rect" x="' + x1.toFixed(1) + '" y="' + (y1 - 4).toFixed(1) +
        '" width="0" height="8" transform="rotate(' + angleDeg.toFixed(2) + ' ' + x1.toFixed(1) + ' ' + y1.toFixed(1) +
        ')" style="' + clipVars + '"></rect></clipPath>';
      line = '<line class="' + cls + '" x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) +
        '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '" clip-path="url(#' + id + ')"></line>';
    }
    // Alex's ask: every throw is now a moving baseball, not just a dashed
    // line revealing itself - travels x1,y1->x2,y2 over the exact same
    // startMs/draw window the clip-rect above already uses, so the ball and
    // the line's own reveal always agree. Reuses pitchBallHtml's own
    // pitchFly keyframe (a generic --fx/--fy->--tx/--ty translate) and the
    // shared .ball-body styling (wheelBallIconSvg(BALL_R,"ball-body")) - the
    // same baseball everywhere else on the diamond, not a new visual of its
    // own. Every call site (throwHtml's per-leg schedule loop, stealThrowHtml)
    // already computes startMs as an absolute, anchor-included delay - see
    // each caller's own comment - so --pstart needs no further offset here.
    //
    // Alex's report: this used to be visible (sitting at x1,y1) from slide
    // mount, every leg of every throw at once, `both` fill-mode giving it
    // presence during the pre-pstart delay same as everything else here -
    // but unlike the pitch/flight ball, nothing ever faded this IN, so it
    // just sat there the whole time instead of appearing only once actually
    // thrown. Same two-nested-groups split as everywhere else (outer owns
    // position, inner owns opacity): .throw-ball-inner reuses the existing
    // ballAppear keyframe (opacity 0->1, style.css) timed to this same
    // --pstart, so the ball only appears the instant it actually leaves the
    // fielder's hand - "only one ball on the field at a time," since by then
    // whatever came before it (the flight ball, handoffMs'd out at this same
    // moment - ballFlightHtml - or a previous relay leg, fadeAtMs'd out the
    // same way right below) has already faded out at this exact real-world
    // point, not a second, separately-visible ball sitting there too.
    //
    // fadeAtMs (a relay's own leg-to-leg handoff, throwHtml's loop): null on
    // a play's LAST (or only) leg, which just stays settled once it arrives -
    // same "nothing else is coming, let it sit" treatment the flight ball
    // gets when there's no throw at all.
    var ballVars = "--fx:" + x1.toFixed(1) + "px;--fy:" + y1.toFixed(1) + "px;" +
      "--tx:" + x2.toFixed(1) + "px;--ty:" + y2.toFixed(1) + "px;" +
      "--pdur:" + draw + "ms;--pstart:" + startMs + "ms" +
      (fadeAtMs != null ? ";--pfade2:" + fadeAtMs + "ms" : "");
    var ballCls = "throw-ball" + (fadeAtMs != null ? " fades" : "");
    var ball = '<g class="' + ballCls + '" style="' + ballVars + '">' +
      '<g class="throw-ball-inner">' + wheelBallIconSvg(BALL_R, "ball-body") + "</g></g>";
    return clip + line + ball;
  }

  function throwHtml(m, flight, moves, seqDelay) {
    var schedule = throwSchedule(m, moves, flight);
    if (!schedule.length) return "";
    // A grounder is fielded wherever it stops rolling, not at its bounce
    // point - the throw has to originate there, or it visibly starts from
    // empty grass short of the fielder. fieldedPoint follows the ball's real
    // ground-contact direction (Part 1), not the HZ launch bearing.
    var origin = ftToSvg(fieldedPoint(flight).x, fieldedPoint(flight).y);
    var delay = seqDelay || 0;
    return schedule.map(function (t, i) {
      // Task 8.3: a position/cutoff leg's endpoint is its own derived spot
      // (schedule's own toFt, single-sourced from throwSchedule - never
      // recomputed here), not a named base.
      var to = t.base === "HOME" ? SCENE_BASES.HOME
        : t.base ? SCENE_BASES[t.base]
        : (t.toFt ? ftToSvg(t.toFt.x, t.toFt.y) : null);
      if (!to) return "";
      // Red for a throw that puts someone out, green for the rare safe/
      // decorative one (a tag-up run that scores anyway) - Alex's call, same
      // verdict-colour convention the ball itself and a steal attempt use.
      var cls = "throw-line " + (t.out ? "throw-out" : "throw-safe");
      // seqDelay added only here, at the final write - throwSchedule itself
      // (shared with ball_flight_test.py's timing-race assertions) stays a
      // pure, offset-free function of the play/flight alone.
      // Alex's report: "only one baseball on the field at a time" - a relay
      // (a double play's second leg, say) needs the FIRST leg's own ball to
      // fade out right as this one fades in, or both sit there visible at
      // once. Every leg but the last one gets this - the last has nothing
      // after it to hand off to, so throwLineHtml leaves it settled instead
      // (its own fadeAtMs comment).
      var next = schedule[i + 1];
      var fadeAtMs = next ? next.startMs + delay : null;
      var html = throwLineHtml(origin.x, origin.y, to.x, to.y, cls, t.startMs + delay, t.drawMs, fadeAtMs, t.out);
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
  // Alex's ask: a stolen-base runner is shown already taking their lead - 12
  // of the real 90ft basepath - rather than starting flat-footed on the bag,
  // so the animated leg is only the remaining 78ft (sceneFieldHtml's own
  // token rendering, and the arrival math right below, both key off this).
  var STEAL_LEADOFF_FT = 12;
  // Real per-runner accelerating sprint over the shortened leg (Task 3.2,
  // folded onto the shared primitive) - the runner covers less ground in
  // less real time, not the same full-leg duration over a shorter distance
  // (which would just be a slower-looking sprint for no reason).
  // STEAL_LEG_DUR_MS survives as the league-average fallback value only
  // (spd null, or a historical archive built before the spd fields
  // existed) - stealLegMs(m, who) is what every real call site uses now.
  var STEAL_LEG_DUR_MS = runnerDrawMsForFt(BASE_DIST_FT - STEAL_LEADOFF_FT);
  function stealLegMs(m, who) {
    return Math.round(arrivalTimeS(BASE_DIST_FT - STEAL_LEADOFF_FT, runnerProfile(m, who)) * 1000);
  }
  // Alex's ask: instead of a single flat gap, the throw's own margin off the
  // runner's arrival now scales with how decisive the underlying
  // steal_num/throw_num roll was (500-diff, same input stealWheelPace reads -
  // see that function's own comment) - a narrow, bang-bang margin at a
  // near-even diff, opening up toward a comfortably decisive one the further
  // apart the roll. CS keeps arriving early by this amount, SB late by it -
  // same convention as before, just no longer a constant.
  // Min bumped 80->150 (Alex's report: a bang-bang steal sometimes read as
  // out on screen when the result was safe - 80ms wasn't a clearly visible
  // gap once THROW_DRAW_MS's own real draw time is on screen too). Applies
  // both directions per the comment above - a caught-stealing throw now
  // also arrives a bit more decisively early, not just a safe steal later.
  // Folded into MARGIN_POLICY (Task 4.3) - tagOut and contestedSafe share
  // these exact bounds, so either class name gives the identical numbers;
  // tagOut is the CS-family's own class, so it reads truest here.
  function stealThrowMarginMs(diff) {
    return targetMarginMs("tagOut", diff);
  }
  // Alex's ask: the catcher can't release a throw before actually receiving
  // the pitch (now a real, synced arrival - pitchBallHtml/wheelFinishMs) plus
  // a beat to catch it, come up, and get the ball out - stealThrowHtml's own
  // floor on when the throw may start.
  var CATCHER_POP_MS = 250;

  // The runner token's own "reaches the base" moment - stealLegMs(m, who)
  // (the shortened, leadoff-adjusted leg, above) is both a plain legs1
  // advance's full duration AND (per batterFirstArrivalMs's own sibling
  // note) the out-to-base keyframe's first-leg checkpoint, so one formula
  // covers both a safe steal and a caught one.
  function stealRunnerArrivalMs(m, who, isCaught, runDelay, outDelay) {
    return (isCaught ? outDelay : runDelay) + stealLegMs(m, who);
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
    return { base: base, caught: caught, delay: !!mv.delay, from: mv.from };
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

  function stealThrowHtml(m, moves, runDelay, outDelay, seqDelay) {
    var target = stealThrowTarget(m, moves);
    if (!target) return "";
    // Alex's ask: a steal of home where the pitcher (not the catcher) is the
    // thrower doesn't get a separate throw line at all - the pitch itself
    // (pitchBallHtml, already heading to HOME on every play) already IS that
    // throw, so drawing a second one on top would be redundant/wrong (there's
    // no real "catcher receives, then throws home" step happening here).
    if (target.base === "HOME" && stealThrowOrigin(m) === "P") return "";
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
    var arrival = stealRunnerArrivalMs(m, target.from, target.caught, runDelay, effOutDelay);
    // Alex's ask: variable margin instead of a flat gap - a diff near 0 (the
    // steal_num/throw_num roll was nearly even) reads as a real bang-bang
    // play, a diff near 500 (a decisive roll either way) reads as an easy
    // one. 250 (the middle of the diff range) is only a fallback for the
    // formally-possible case target matched on m.result alone with no real
    // steal_num/throw_num on the row.
    var diff = (m.steal_num != null && m.throw_num != null)
      ? Math.abs(signedCirc(m.steal_num, m.throw_num, 1000)) : 250;
    var margin = stealThrowMarginMs(diff);
    var idealArrive = target.caught ? arrival - margin : arrival + margin;
    // Alex's ask: the catcher can't release the throw before the pitch has
    // actually arrived (pitchBallHtml's own wheelFinishMs - CATCHER_POP_MS's
    // own comment) - Math.max only ever pushes idealArrive LATER, never
    // earlier, so this is a floor, not a second target.
    var pitchArriveMs = Math.round(FIELD_SEQUENCE_DELAY_MS / stealWheelPace(m));
    var arrive = Math.max(idealArrive, pitchArriveMs + CATCHER_POP_MS + THROW_DRAW_MS);
    // But that floor must never be allowed to push a caught-stealing's throw
    // past the runner's own arrival - a real out has to keep reading as one
    // regardless of how slow the wheel/how tight the diff-based margin above
    // was. (A safe steal has no equivalent risk - arriving later than
    // idealArrive only ever reads as "even more clearly safe.")
    if (target.caught) arrive = Math.min(arrive, arrival - MARGIN_POLICY.tagOut.minMs);
    var start = Math.max(0, arrive - THROW_DRAW_MS);
    var cls = "throw-line steal-throw " + (target.caught ? "throw-out" : "throw-safe");
    return throwLineHtml(from.x, from.y, to.x, to.y, cls, start + (seqDelay || 0));
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
    CATCH_RETREAT_PENALTY: CATCH_RETREAT_PENALTY, PICKUP_RETREAT_PENALTY: PICKUP_RETREAT_PENALTY,
    launchAngleFor: launchAngleFor,
    stationsLookup: stationsLookup,
    classifySprayBucket: classifySprayBucket, SPRAY_BUCKETS: SPRAY_BUCKETS,
    FENCE_DEPTH_FT: FENCE_DEPTH_FT, fenceTruncatedSamples: fenceTruncatedSamples,
    ordinal: ordinal, deriveRunnerMoves: deriveRunnerMoves,
    outThrowTargets: outThrowTargets, throwSchedule: throwSchedule,
    throwLineHtml: throwLineHtml,
    batterFirstArrivalMs: batterFirstArrivalMs,
    stealThrowTarget: stealThrowTarget, stealRunnerArrivalMs: stealRunnerArrivalMs,
    stealThrowOrigin: stealThrowOrigin, stealThrowHtml: stealThrowHtml, stealOutAtMs: stealOutAtMs,
    stealThrowMarginMs: stealThrowMarginMs, STEAL_LEG_DUR_MS: STEAL_LEG_DUR_MS,
    stealLegMs: stealLegMs, runnerSpd: runnerSpd, runnerProfile: runnerProfile,
    runnerLegMs: runnerLegMs,
    STEAL_LEADOFF_FT: STEAL_LEADOFF_FT, CATCHER_POP_MS: CATCHER_POP_MS,
    pitchBallHtml: pitchBallHtml, PITCH_TRAVEL_MS: PITCH_TRAVEL_MS, PITCH_SPEED_MPH: PITCH_SPEED_MPH,
    walkPitchTargetSvg: walkPitchTargetSvg, WALK_PITCH_OFFSET_FT: WALK_PITCH_OFFSET_FT,
    stealWheelPace: stealWheelPace, WHEEL_PACE_MIN: WHEEL_PACE_MIN, wheelHtml: wheelHtml,
    PITCHER_MOUND_FT: PITCHER_MOUND_FT, FIELD_SEQUENCE_DELAY_MS: FIELD_SEQUENCE_DELAY_MS,
    scorebugOutsHtml: scorebugOutsHtml, outAtMomentsMs: outAtMomentsMs,
    BALK_RESULTS: BALK_RESULTS,
    THROW_DELAY_MS: THROW_DELAY_MS,
    MARGIN_POLICY: MARGIN_POLICY, targetMarginMs: targetMarginMs,
    reconcileThrowSchedule: reconcileThrowSchedule, holdChainTo: holdChainTo,
    forcedOutRunnerArrivalMs: forcedOutRunnerArrivalMs, runnerForOutTarget: runnerForOutTarget,
    runnerMoveTiming: runnerMoveTiming, runnerPassingAdjustments: runnerPassingAdjustments,
    RUNNER_MIN_GAP_ORD: RUNNER_MIN_GAP_ORD,
    safeRunnerArrivalMs: safeRunnerArrivalMs, runnerForSafeTarget: runnerForSafeTarget,
    throwRunnerAdjustmentMs: throwRunnerAdjustmentMs,
    THROW_DRAW_MS: THROW_DRAW_MS, THROW_STAGGER_MS: THROW_STAGGER_MS,
    THROW_SPEED_BY_POS: THROW_SPEED_BY_POS, throwDrawMsForFt: throwDrawMsForFt,
    RUNNER_LEAD_MS: RUNNER_LEAD_MS,
    dirtEdgeFt: dirtEdgeFt, CAUGHT_IN_AIR: CAUGHT_IN_AIR,
    TAG_THROW_ARCHETYPES: TAG_THROW_ARCHETYPES,
    GROUND_ARCHETYPES: GROUND_ARCHETYPES,
    parseThrowOrder: parseThrowOrder, baseLegs: baseLegs, firstRealLegs: firstRealLegs,
    finalBaseOfChain: finalBaseOfChain, cutoffSpotFt: cutoffSpotFt,
    CUTOFF_POSITION_FRAC: CUTOFF_POSITION_FRAC,
    THROW_ORDER_BASE_LETTER: THROW_ORDER_BASE_LETTER, THROW_ORDER_POSITION_NUMBER: THROW_ORDER_POSITION_NUMBER,
    brcExcludes: brcExcludes,
    resolveGrounderInterception: resolveGrounderInterception, resolveSinglePickup: resolveSinglePickup,
    chargeInIntercept: chargeInIntercept, fielderInterceptS: fielderInterceptS,
    FIELDER_CHARGE_FT_PER_S: FIELDER_CHARGE_FT_PER_S, CHARGE_REACTION_S: CHARGE_REACTION_S,
    CHARGE_CANDIDATE_POSITIONS: CHARGE_CANDIDATE_POSITIONS,
    applyAirPositionOverride: applyAirPositionOverride,
    resolveHitPickup: resolveHitPickup,
    applyAngleOverride: applyAngleOverride, clampFairTerritory: clampFairTerritory,
    groundDirPoint: groundDirPoint, fieldedPoint: fieldedPoint,
    throwOrderCandidateKeys: throwOrderCandidateKeys,
    fieldingChain: fieldingChain, fieldingChainDetail: fieldingChainDetail, involvedPositions: involvedPositions,
    fielderLabelHasResult: fielderLabelHasResult, ballFlightHtml: ballFlightHtml,
    ballResultLabelHtml: ballResultLabelHtml,
    fielderNameLabelsHtml: fielderNameLabelsHtml, onDeckRunnerLabelsHtml: onDeckRunnerLabelsHtml,
    fieldingNotation: fieldingNotation,
    sceneDefenseLineHtml: sceneDefenseLineHtml, playSceneHtml: playSceneHtml,
    scoreboardCard: scoreboardCard,
    teamColor: teamColor, teamSecondaryColor: teamSecondaryColor,
    colorDistance: colorDistance, gameTeamColors: gameTeamColors,
    TEAM_COLOR_MIN_DISTANCE: TEAM_COLOR_MIN_DISTANCE,
    OF_POSITIONS: OF_POSITIONS, FIELDER_ANCHORS_FT: FIELDER_ANCHORS_FT,
    INFIELDER_DEPTH_FT: INFIELDER_DEPTH_FT, MIN_ANGLE_FOR_POS: MIN_ANGLE_FOR_POS,
    OUTFIELDER_DEPTH_FT: OUTFIELDER_DEPTH_FT, OF_CANONICAL_ANGLE: OF_CANONICAL_ANGLE,
    HZ_FIELDER_BY_ANGLE: HZ_FIELDER_BY_ANGLE, PITCHER_MIDDLE_EV_MAX_MPH: PITCHER_MIDDLE_EV_MAX_MPH,
    fielderSpd: fielderSpd, spdPaceScale: spdPaceScale,
    accelTimeS: accelTimeS, FIELDER_ACCEL_FT_S2: FIELDER_ACCEL_FT_S2,
    accelDistForTimeS: accelDistForTimeS, idleDriftLeg: idleDriftLeg,
    arrivalTimeS: arrivalTimeS, legDurationsMs: legDurationsMs,
    fielderProfile: fielderProfile, throwProfile: throwProfile,
    chargeFielderArriveS: chargeFielderArriveS,
    pitcherCover1BArrivalMs: pitcherCover1BArrivalMs, pitcherCover1BLegs: pitcherCover1BLegs,
    firstBaseCoverage: firstBaseCoverage,
    fielderLegDurationsMs: fielderLegDurationsMs, movingFielderTokenHtml: movingFielderTokenHtml,
    fielderBallArrivalMs: fielderBallArrivalMs,
    fielderStartAnchorFt: fielderStartAnchorFt,
    ofPursuitApplies: ofPursuitApplies, ofPursuitDeficitMs: ofPursuitDeficitMs,
    ofReadDelayMs: ofReadDelayMs, ofDerivedShadeAnchorFt: ofDerivedShadeAnchorFt,
    ofShadeDirection: ofShadeDirection,
    OF_READ_DELAY_MAX_MS: OF_READ_DELAY_MAX_MS,
    TAG_UP_MS: TAG_UP_MS,
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

  var rnOutArcCounter = 0;

  /* Per-token generated position keyframe for an out-bound runner (Alex's
     report: a caught fly ball's batter ran the full fixed choreography all
     the way to first, only turning red - and starting to walk off - well
     after the ball had actually been caught; a runner "shouldn't start
     heading to the dugout until they're out," and turning red/heading to
     the dugout/being out should all read as the same moment). The old
     rnOutToBase/rnOutRetreat keyframes always played out a FIXED 3333ms
     "run to the base" phase before turning red or walking off, regardless
     of when the real out (outAtMsFor - the catch, or whichever throw
     resolves it) actually happened - fine when the two coincidentally lined
     up, visibly wrong whenever they didn't (a quick catch left the runner
     still "safely" running well past being out; a slow one left them stuck
     waiting at the base with nothing to show for it).
     pathPoints choreographs the pre-out portion as one or more waypoints,
     each `{frac, x, y}` - frac is how far through fullSprintMs (real time to
     cover that leg at sprint speed) it's reached, in increasing order,
     last one normally at frac:1 (a single target base for out-to-base/
     out-to-first) or two (out-retreat's break-halfway-then-scramble-back).
     Real per-play math: walks pathPoints up to outAtMs's own real fraction
     of fullSprintMs, cutting the final leg short (interpolated) if the out
     happens mid-leg - a quick catch/throw now visibly catches the runner
     between bases, not after a full sprint that never happened. If outAtMs
     lags behind the choreographed path (a slow throw arriving after the
     runner's already back at their base), an explicit hold keyframe keeps
     them waiting there rather than snapping ahead. Either way the walk to
     the dugout starts turning-red's own instant (outAtMs) - not a fixed
     beat later - so out/turn-red/dugout-bound are the same moment, and the
     token is still visibly moving (not already faded, per Alex's second
     report) when rnFadeOut picks up shortly after. */
  function runnerOutMotionHtml(fx, fy, pathPoints, dugoutX, dugoutY, fullSprintMs, outAtMs, walkMs) {
    var reachFrac = fullSprintMs > 0 ? clamp(outAtMs / fullSprintMs, 0, 1) : 1;
    var reachAtMs = reachFrac * fullSprintMs;
    var prevFrac = 0, prevX = fx, prevY = fy;
    var stops = [{ ms: 0, x: fx, y: fy }];
    for (var i = 0; i < pathPoints.length; i++) {
      var wp = pathPoints[i];
      if (reachFrac >= wp.frac) {
        stops.push({ ms: wp.frac * fullSprintMs, x: wp.x, y: wp.y });
        prevFrac = wp.frac; prevX = wp.x; prevY = wp.y;
      } else {
        var segSpan = wp.frac - prevFrac;
        var segFrac = segSpan > 0 ? (reachFrac - prevFrac) / segSpan : 0;
        stops.push({ ms: reachAtMs, x: prevX + segFrac * (wp.x - prevX), y: prevY + segFrac * (wp.y - prevY) });
        break;
      }
    }
    var lastStop = stops[stops.length - 1];
    var totalMs = outAtMs + walkMs;
    if (outAtMs > lastStop.ms + 0.01) {
      // The runtime floor beneath the reconciler (plan Task 4.4, point 5) -
      // the runner's own honest sprint finished before the real out moment
      // (outAtMs, from the throw), so they visibly hold at the bag until
      // then rather than snapping ahead. This is what makes goal (a)
      // literally unbreakable regardless of any upstream gap, but the
      // reconciler (Stage 3) is supposed to make it unreachable for any
      // play within its own margin-policy bounds - a real fielded/thrown
      // out that still lands here means the reconciliation upstream didn't
      // honestly close the gap (an "unresolved" adjustment, most likely),
      // worth a look rather than silently relying on this fallback alone.
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[gameday] runner held at bag " + (outAtMs - lastStop.ms).toFixed(0) +
          "ms past their own honest arrival - the reconciler should have closed this; check its adjustments for this play");
      }
      stops.push({ ms: outAtMs, x: lastStop.x, y: lastStop.y });
    }
    stops.push({ ms: totalMs, x: dugoutX, y: dugoutY });

    var name = "rnOut" + (rnOutArcCounter++);
    var kfBody = stops.map(function (s) {
      var pct = totalMs > 0 ? (s.ms / totalMs) * 100 : 0;
      return pct.toFixed(3) + "% { transform: translate(" + s.x.toFixed(1) + "px," + s.y.toFixed(1) + "px); }";
    }).join(" ");
    return { style: "<style>@keyframes " + name + " { " + kfBody + " }</style>", name: name, totalMs: totalMs };
  }

  function sceneFieldHtml(m, flight) {
    var before = String(m.obc_before || "000");
    var after = String(m.obc_after || "000");
    var moves = resolveRunnerMoves(m);
    var dugoutFt = dugoutFor(m);
    var dugoutSvg = ftToSvg(dugoutFt.x, dugoutFt.y);
    // The batter's own token starts a couple feet off dead-center on the
    // plate, toward whichever box their hand stands in (Alex's ask) - every
    // OTHER token/label anchored at "home" (labels, the ball's own contact
    // point) stays at the real plate center; only the batter's own starting
    // point moves.
    var batterHand = effectiveHand(m.batter_hand);
    var batterBoxSvg = batterBoxStartPt(batterHand);

    // Which OUT-bound moves are corroborated by an actual throw, batted-ball
    // (outThrowTargets, already capped to real outs) or steal (stealThrowTarget) -
    // used below to tell a real force/tag-out from a runner simply stranded
    // when the half-inning ended (deriveRunnerMoves' obc-reset artifact,
    // already noted above outThrowTargets - on a strikeout or walk this was
    // showing stranded runners advancing toward a base before "being out",
    // when nothing actually happened to them at all).
    var realOutTargets = baseLegs(outThrowTargets(m, moves, flight));
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
    // Real per-out timing (Alex's ask - "the corresponding batter/runner who
    // is out" should turn red at the exact moment the dot fills, not a fixed
    // guess): outEndByBase is the same map scorebugOutsHtml's dots use, so a
    // runner forced/tagged out at a base neither one drifts out of sync with
    // the other. outAtMsFor falls back to the catch moment (caught in the
    // air, no throw needed), the caught-stealing tag moment, or the old flat
    // outDelay (a no-flight, non-steal out - a strikeout, or an out nothing
    // here corroborates) when a base isn't in the map.
    var outEndByBase = outThrowEndByBase(m, moves, flight);
    // The reconciler's own runnerLateJump/stretchRunner knobs (Task 4.4),
    // keyed by runner - Stage 4's read side: the specific forced-out/
    // contested runner throwSchedule's own reconciliation decided needed a
    // later break or a slower pace to keep the throw honest gets that same
    // extra time folded into their own token's run below (legDurMs), rather
    // than leaving it as inert bookkeeping the runtime hold-at-bag fallback
    // alone had to paper over.
    var runnerAdjMsByWho = {};
    (throwSchedule(m, moves, flight).adjustments || []).forEach(function (a) {
      if (a.knob === "runnerLateJump" || a.knob === "stretchRunner") {
        runnerAdjMsByWho[a.who] = (runnerAdjMsByWho[a.who] || 0) + a.ms;
      }
    });
    // Task 10 (facts 15/5): the same no-passing correction throwSchedule's
    // own reconciliation reads (via safeRunnerArrivalMs/forcedOutRunnerArrivalMs)
    // gets folded into this runner's own token below (mvDelay/legDurMs) -
    // single-sourced, so the picture and the reconciled race can never
    // disagree about where this runner actually is.
    var runnerPassAdjByWho = runnerPassingAdjustments(m, flight, moves);
    // Closure, not called until the moves.map() loop below - stealOut is
    // assigned further down but already final by then, same as every other
    // var this function reads late.
    function outAtMsFor(forcedBase) {
      if (forcedBase && outEndByBase[forcedBase] != null) return outEndByBase[forcedBase];
      if (stealOut && stealOut.caught && forcedBase === stealOut.base) return stealOutAtMs(m, moves);
      return catchMs || outDelay;
    }
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
      ? stealRunnerArrivalMs(m, stealOut.from, true, runDelay, stealOutDelay) + TAG_UP_MS
      : 0;
    // I7: the slowest arriving safe/scoring runner - the base plates' gold
    // "post-play occupancy" fill is delayed until this moment, so a base
    // does not light up before any runner has actually reached it.
    var maxArrival = 0;
    // Alex's ask: hold the whole field sequence (ball, runners, throws,
    // labels) until the DIFF/HZ wheels finish their own animation first.
    // Added only where a delay finally gets written into a --delay/
    // --rdelay/--blight/--sflash value below - runDelay/outDelay/catchMs/
    // delayedStartMs/stealOutDelay/mvDelay etc. all stay in their original,
    // un-offset units throughout, so every race between them (throw vs.
    // runner, catch vs. tag-up, ...) keeps exactly the same margin it
    // already had; this just pushes the whole picture later, uniformly.
    var seqDelay = FIELD_SEQUENCE_DELAY_MS;
    // Alex's ask: a steal attempt's runner breaks the instant the pitcher
    // begins the delivery, not after the wheel-wait every other play's field
    // choreography holds for. Used only for the runner token's own motion/
    // out-at moment, the base's steal flash, and the catcher's throw-down
    // (stealThrowHtml) - every one of those already shares runDelay/outDelay/
    // stealOutDelay as its raw-unit baseline (stealOutAtMs is built the same
    // way), so swapping the shared anchor from seqDelay to 0 for all of them
    // together keeps their existing relative race (throw vs. runner, tag vs.
    // arrival) exactly as tuned - it only moves the whole steal picture
    // earlier, uniformly, same principle as seqDelay's own comment above.
    // Non-steal elements (ball flight, grounder throws, labels) are untouched.
    var runnerSeqDelay = stealOut ? 0 : seqDelay;
    // Alex's ask: a steal's own wheel pace (stealWheelPace, same formula
    // sceneWheelDiffHtml's DIFF wheel instance uses to set --wheel-pace)
    // slows the wheel's real on-screen finish time down as the underlying
    // diff shrinks - wheelFinishMs mirrors that same division so
    // pitchBallHtml's "arrive exactly when the wheel finishes" sync (its own
    // seqDelay param) tracks the wheel's REAL pace instead of the fixed
    // baseline. 1 for anything that isn't a steal, so this is just seqDelay
    // unchanged for every other play.
    var wheelFinishMs = Math.round(FIELD_SEQUENCE_DELAY_MS / stealWheelPace(m));

    // Real timing of this play's own last-recorded out (Alex's ask) - hoisted
    // up here from what used to be only the half-inning pill's own
    // computation below, since a strandedSafe token (tokens.map, further
    // down) needs this same real moment too: whichever out actually ends the
    // half inning, not a fixed guess. Gated on the raw is_half_inning_final
    // flag rather than isHalfEnd's narrower (!is_game_final) version - a
    // stranded runner on the game's own final play still needs their real
    // out-timing, even though the pill itself is skipped there (isHalfEnd,
    // still computed below where it's actually consumed).
    var haloutLastMs = 0;
    if (m.is_half_inning_final) {
      var haloutCount = (m.outs_after || 0) - (m.outs_before || 0);
      var haloutMoments = outAtMomentsMs(m, flight, haloutCount);
      haloutLastMs = haloutMoments.length ? haloutMoments[haloutMoments.length - 1] : 0;
    }

    /* Two nested groups per token, deliberately: the outer one owns position
       (the multi-leg basepath run) and the inner one owns opacity and scale
       (fading out, the batter appearing, the flash on scoring). Both would
       otherwise be competing to animate `transform` on one element, and only
       one of them could win. */
    var tokens = moves.map(function (mv) {
      var from = mv.from === "BATTER" ? batterBoxSvg : SCENE_BASES[mv.from];
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
      // Alex's ask: a stolen-base runner is shown already having taken their
      // lead - STEAL_LEADOFF_FT of the real 90ft path - instead of starting
      // flat-footed on the bag. Only a runner genuinely advancing on a real
      // steal attempt (stealOut - this whole play; !strandedSafe/path.length -
      // an actual leg to interpolate toward, not a no-op "hold" token;
      // !useRetreat - LODP's own separate scramble, never a steal, and
      // per-row anyway so the two can't co-occur in practice). Reassigns
      // `from` in place - every downstream use (--fx/--fy, runnerOutMotionHtml's
      // own start point) picks the shortened leg up for free - and swaps in
      // STEAL_LEG_DUR_MS (real sprint speed over the now-shorter distance,
      // not RUN_LEG_MS[legs]'s full-90ft time over less ground, which would
      // just read as a slower jog) everywhere that duration is used below.
      // Plain SVG-space interpolation toward path[0] - same simplification
      // useRetreat's own midpoint above already relies on; true perspective-
      // correct feet would mean reprojecting through BASE_POS_FT/ftToSvg, not
      // worth it over a 12ft/90ft fraction.
      var isStealAdvance = stealOut && !useRetreat && !strandedSafe && path.length > 0;
      if (isStealAdvance) {
        var leadoffFrac = STEAL_LEADOFF_FT / BASE_DIST_FT;
        from = {
          x: from.x + (path[0].x - from.x) * leadoffFrac,
          y: from.y + (path[0].y - from.y) * leadoffFrac,
        };
      }
      var legDurMs = isStealAdvance ? stealLegMs(m, mv.from) : runnerLegMs(m, mv.from, legs);
      // Stage 4: fold in this specific runner's own reconciler adjustment,
      // if throwSchedule's reconciliation decided one was needed to keep a
      // real out-throw's margin honest (runnerAdjMsByWho, above) - reads as
      // a genuinely slower/later-breaking runner rather than relying solely
      // on the runtime hold-at-bag fallback to paper over the gap.
      legDurMs += runnerAdjMsByWho[mv.from] || 0;
      // Task 10: this runner's own no-passing pace correction (trailSlowPace),
      // if resolvePassing decided one was needed - runnerPassAdjByWho reads
      // the identical adjustment safeRunnerArrivalMs/forcedOutRunnerArrivalMs
      // already folded into the reconciled race above.
      var passAdj = runnerPassAdjByWho[mv.from];
      if (passAdj) legDurMs *= passAdj.paceScale;
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
      // Task 10: this runner's own no-passing start correction (trailLateBreak).
      mvDelay += passAdj ? passAdj.delayMs : 0;
      // stranded-to-dugout's own keyframe ignores --dur (a fixed 1700ms,
      // matching the out choreography's own run-then-leave timing exactly)
      // - legDurMs here would understate how long the token is actually on
      // screen for maxArrival's "don't light a base up before everyone's
      // actually arrived" purpose.
      if (!isOut) maxArrival = Math.max(maxArrival, mvDelay + (strandedSafe ? 1700 : legDurMs));
      var vars = "--fx:" + from.x + "px;--fy:" + from.y + "px;" +
                 "--tx:" + end.x + "px;--ty:" + end.y + "px;" +
                 "--rdelay:" + (mvDelay + runnerSeqDelay) + "ms;";
      path.forEach(function (p, i) {
        vars += "--p" + (i + 1) + "x:" + p.x + "px;--p" + (i + 1) + "y:" + p.y + "px;";
      });
      vars += "--dur:" + legDurMs + "ms";
      var outStyle = "";
      if (isOut) {
        // Real turn-red moment (Alex's ask), same held clock as --rdelay
        // above (+runnerSeqDelay) so it can never land before the token's own
        // run has even started - see outAtMsFor and scorebugOutsHtml's
        // matching dot delay.
        var outAtAbs = outAtMsFor(forcedBase) + runnerSeqDelay;
        vars += ";--outat:" + outAtAbs + "ms";
        // A put-out runner with somewhere to be forced travels there first
        // (or gets cut short partway, or waits at the base for a slow
        // throw - runnerOutMotionHtml sorts out which), THEN turns red AND
        // heads to the dugout in the same instant (Alex's ask). Only when
        // there's an actual leg to run - a plain out-walk (no corroborated
        // base, no partial-advance path) has nothing to interpolate and
        // keeps its simple shared keyframe, fixed up separately in CSS.
        if (path.length) {
          var fullSprintMs = legDurMs;
          // Relative to THIS token's own run start (mvDelay), not the play's
          // absolute clock - runnerOutMotionHtml's fullSprintMs is also "time
          // since this run started," so the two have to share an origin.
          // Clamped at 0: a caught-in-the-air out can resolve before this
          // runner's own (deliberately cautious) mvDelay would have even
          // started them moving - outAtRel:0 correctly shows them heading
          // straight for the dugout from where they started, never running
          // toward a base at all, instead of a negative-time keyframe.
          var outAtRel = Math.max(0, outAtMsFor(forcedBase) - mvDelay);
          var pathPoints = useRetreat
            ? [{ frac: 0.75, x: path[0].x, y: path[0].y }, { frac: 1, x: from.x, y: from.y }]
            : [{ frac: 1, x: path[path.length - 1].x, y: path[path.length - 1].y }];
          var motion = runnerOutMotionHtml(from.x, from.y, pathPoints, dugoutSvg.x, dugoutSvg.y,
            fullSprintMs, outAtRel, RN_OUT_WALK_MS);
          outStyle = motion.style;
          vars += ";animation-name:" + motion.name +
            ";animation-duration:calc(" + motion.totalMs + "ms / var(--play-speed,1))" +
            ";animation-delay:calc(" + (mvDelay + runnerSeqDelay) + "ms / var(--play-speed,1))" +
            ";animation-timing-function:linear;animation-fill-mode:both";
        }
      } else if (strandedSafe) {
        // Alex's report: a safe runner really advancing when their own
        // half-inning ends (obc_before/after says as much, and their own
        // real path here has more than one point to prove it) was showing
        // no visible movement at all - straight from their own base to the
        // dugout, skipping the leg they actually ran. This class used to
        // reference a shared @keyframes (rnOutToBase) that got removed when
        // the out choreography above migrated to its own per-token generated
        // keyframe (runnerOutMotionHtml) - this rule was never brought along
        // with it, so the animation-name simply stopped resolving to
        // anything and the token just sat at its --tx/--ty (the dugout) the
        // whole time. Same generator the out tokens already use.
        //
        // outAtRel used to be pinned to the full sprint time itself (always
        // finish the whole leg, THEN peel off) - Alex's second report: on a
        // caught 3rd out, that let a short advance finish and start walking
        // to the dugout well before the ball was actually shown being
        // caught, and separately gave a longer advance no way to be cut off
        // mid-leg at all when the real out landed partway through it. Now
        // reads the same real haloutLastMs this play's own last out actually
        // lands at (hoisted above, same source the half-inning pill and
        // every isOut token's own --outat already use) - runnerOutMotionHtml
        // already knows how to cut a leg short mid-run (a fast out) or hold
        // at the arrived base until the real moment (a slow one, the old
        // always-finish-the-leg case now falls out of this for free rather
        // than being hardcoded), exactly like an out-bound runner's own run
        // already does; this token just never turns red.
        var fullSprintMs = legDurMs;
        var pathPoints = [{ frac: 1, x: path[path.length - 1].x, y: path[path.length - 1].y }];
        var outAtRel = Math.max(0, haloutLastMs - mvDelay);
        // Alex's third report on this same token: the POSITION animation
        // above now correctly waits for the real catch/throw before
        // diverting, but the .rn-inner FADE-out is a separate CSS animation
        // (style.css) that was still anchored to --rdelay+--dur - --dur is
        // the token's original fixed leg duration, set once up top for
        // every move regardless of branch, not this branch's own newly-real
        // (and often much longer, on a deep fly) motion.totalMs. That let
        // the token visibly fade away on the old short schedule while the
        // ball was still in the air. --outat here is the same real absolute
        // moment (haloutLastMs + runnerSeqDelay) every isOut token's own
        // --outat already carries, letting the CSS rule reuse that exact
        // fallback chain instead of a stranded-only formula.
        vars += ";--outat:" + (haloutLastMs + runnerSeqDelay) + "ms";
        var motion = runnerOutMotionHtml(from.x, from.y, pathPoints, dugoutSvg.x, dugoutSvg.y,
          fullSprintMs, outAtRel, RN_OUT_WALK_MS);
        outStyle = motion.style;
        vars += ";animation-name:" + motion.name +
          ";animation-duration:calc(" + motion.totalMs + "ms / var(--play-speed,1))" +
          ";animation-delay:calc(" + (mvDelay + runnerSeqDelay) + "ms / var(--play-speed,1))" +
          ";animation-timing-function:linear;animation-fill-mode:both";
      }
      // A put-out runner with somewhere to be forced travels there first,
      // THEN turns red, THEN walks a straight line to the dugout (Stage
      // 6a/6b, generalised by I8). legsN is a safe-runner-only class - the
      // out choreography's own keyframe owns --p1 instead, so isOut never
      // gets a legsN class alongside it.
      var outCls = isOut ? (useRetreat ? " out-retreat" : (path.length ? " out-to-base" : " out-walk")) : "";
      var cls = "rn" + (legs && !isOut && !strandedSafe ? " legs" + legs : "") + outCls +
                (strandedSafe ? " stranded-to-dugout" : "") +
                (mv.scored ? " score" : "") + (mv.from === "BATTER" ? " batter" : "");
      return outStyle + '<g class="' + cls + '" style="' + vars + '">' +
        '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
    }).join("");

    // deriveRunnerMoves only tracks RUNNERS, so a play where the batter never
    // reached base yields no token for them at all. Four shapes: a
    // no-plate-appearance play (a steal, a caught stealing, a balk - Alex's
    // ask below), the FC family (BATTER_REACHES_FIRST - safe at first, A3),
    // a batted-ball out (Stage 6b), or no batted ball at all (Stage 6c).
    var noPa = (data.meta.flight && data.meta.flight.no_pa) || [];
    var batterReached = moves.some(function (mv) { return mv.from === "BATTER"; });
    // m.result is only null on the on-deck placeholder (no real play has a
    // null result) - nothing has happened yet, so no phantom batter walking
    // to the dugout for it.
    if (!batterReached && m.result != null) {
      var h = SCENE_BASES.HOME;
      if (noPa.indexOf(m.result) !== -1) {
        // Alex's ask: a stolen base attempt or a balk still has a batter
        // standing in their box the whole time - no PA happened on either,
        // so no motion, no colour change, no walk to the dugout, just
        // present the entire play (--tx/--ty only - .rn's own base rule
        // renders straight from that with no animation class to move it).
        var boxVars = "--tx:" + batterBoxSvg.x + "px;--ty:" + batterBoxSvg.y + "px";
        tokens += '<g class="rn batter" style="' + boxVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
      } else if (BATTER_REACHES_FIRST[m.result]) {
        // A3/F5: the FC family reaches first safely - someone else was
        // forced out. deriveRunnerMoves pairs obc_before/after like-for-like
        // for these codes and never emits a BATTER move, so without this the
        // batter is invisible and the whole play renders static. Plain safe
        // token, not the out choreography - the batter isn't out here.
        var fc1 = SCENE_BASES["1B"];
        var fcVars = "--fx:" + batterBoxSvg.x + "px;--fy:" + batterBoxSvg.y + "px;" +
                     "--tx:" + fc1.x + "px;--ty:" + fc1.y + "px;" +
                     "--p1x:" + fc1.x + "px;--p1y:" + fc1.y + "px;" +
                     "--rdelay:" + (runDelay + seqDelay) + "ms;--dur:" + runnerLegMs(m, "BATTER", 1) + "ms";
        tokens += '<g class="rn legs1 batter" style="' + fcVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
        maxArrival = Math.max(maxArrival, runDelay + runnerLegMs(m, "BATTER", 1));
      } else if (flight) {
        // 6b: runs to first on the normal basepath, cut short (or held
        // waiting at the bag) exactly at the real out moment, THEN turns
        // red and heads for the dugout in that same instant (Alex's ask) -
        // what makes a groundout read differently from a strikeout.
        // outAtMsFor("1B") resolves to the real throw-to-first's arrival
        // when there is one (a grounder), or falls through to the catch
        // moment for a caught-in-the-air out (a fly/line/pop out never has
        // a throw to 1B in the schedule at all) - either way the real
        // moment this specific out was recorded.
        var p1 = SCENE_BASES["1B"];
        var batterOutAtAbs = outAtMsFor("1B") + seqDelay;
        var batterOutAtRel = Math.max(0, outAtMsFor("1B") - runDelay);
        var batterMotion = runnerOutMotionHtml(batterBoxSvg.x, batterBoxSvg.y, [{ frac: 1, x: p1.x, y: p1.y }],
          dugoutSvg.x, dugoutSvg.y, runnerLegMs(m, "BATTER", 1), batterOutAtRel, RN_OUT_WALK_MS);
        var voVars = "--fx:" + batterBoxSvg.x + "px;--fy:" + batterBoxSvg.y + "px;" +
                     "--p1x:" + p1.x + "px;--p1y:" + p1.y + "px;" +
                     "--tx:" + dugoutSvg.x + "px;--ty:" + dugoutSvg.y + "px;" +
                     "--rdelay:" + (runDelay + seqDelay) + "ms;" +
                     "--outat:" + batterOutAtAbs + "ms;" +
                     "animation-name:" + batterMotion.name +
                     ";animation-duration:calc(" + batterMotion.totalMs + "ms / var(--play-speed,1))" +
                     ";animation-delay:calc(" + (runDelay + seqDelay) + "ms / var(--play-speed,1))" +
                     ";animation-timing-function:linear;animation-fill-mode:both";
        tokens += batterMotion.style + '<g class="rn out-to-first batter" style="' + voVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
      } else {
        // 6c: no batted ball (strikeout and friends) - straight from home to
        // the dugout, no trip to first. outAtMsFor(null) falls through to
        // outDelay - identical to this token's own --rdelay, same as before.
        var owVars = "--fx:" + batterBoxSvg.x + "px;--fy:" + batterBoxSvg.y + "px;" +
                     "--tx:" + dugoutSvg.x + "px;--ty:" + dugoutSvg.y + "px;" +
                     "--rdelay:" + (outDelay + seqDelay) + "ms;" +
                     "--outat:" + (outAtMsFor(null) + seqDelay) + "ms";
        tokens += '<g class="rn out-walk batter" style="' + owVars + '">' +
          '<g class="rn-inner"><circle r="' + RUNNER_R + '"></circle></g></g>';
        if (STRIKEOUT_RESULTS[m.result]) {
          var kShort = (data.meta.result_short || {})[m.result] || m.result;
          tokens += '<text class="ball-label" x="' + h.x + '" y="' + h.y +
            '" dx="10" dy="-6" style="--delay:' + (outDelay + seqDelay) + 'ms">' + escapeHtml(kShort) + "</text>";
        }
      }
    }

    // BB/IBB (Alex's ask, same treatment as STRIKEOUT_RESULTS' "K" above):
    // the batter DOES reach first here, so this sits outside the
    // !batterReached block above - the label just anchors at home plate
    // (where the walk was actually drawn) rather than following the batter
    // token down the basepath. Same beat the batter token itself leaves on
    // (runDelay - 0 for any no-flight play, walks included, before seqDelay).
    if (WALK_RESULTS[m.result]) {
      var bbHome = SCENE_BASES.HOME;
      var bbShort = (data.meta.result_short || {})[m.result] || m.result;
      tokens += '<text class="ball-label" x="' + bbHome.x + '" y="' + bbHome.y +
        '" dx="10" dy="-6" style="--delay:' + (runDelay + seqDelay) + 'ms">' + escapeHtml(bbShort) + "</text>";
    }

    // Balk (Alex's ask): no batter token at all here (Balk is in
    // data.meta.flight.no_pa, so the block above never runs) - labels the
    // mound instead, the one fixed point every balk is actually about. Same
    // beat every runner on base starts advancing (runDelay - 0, no flight,
    // before seqDelay).
    if (BALK_RESULTS[m.result]) {
      var bkMound = ftToSvg(0, PITCHER_MOUND_FT);
      var bkShort = (data.meta.result_short || {})[m.result] || m.result;
      tokens += '<text class="ball-label" x="' + bkMound.x + '" y="' + bkMound.y +
        '" dx="10" dy="-6" style="--delay:' + (runDelay + seqDelay) + 'ms">' + escapeHtml(bkShort) + "</text>";
    }

    // B3: a caught-stealing or stolen-base attempt gets a catcher throw and a
    // tag-at-the-bag flash on whichever base was in play - it's the one play
    // type where "the ball beat (or didn't beat) the runner" is the whole
    // story, and it never gets a ball flight to hang a throw off otherwise.
    var stealTarget = stealThrowTarget(m, moves);
    var stealFlashDelay = stealTarget
      ? stealRunnerArrivalMs(m, stealTarget.from, stealTarget.caught, runDelay, stealTarget.delay ? delayedStartMs : outDelay)
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
        '" style="--blight:' + (maxArrival + runnerSeqDelay) + 'ms;--sflash:' + (stealFlashDelay + runnerSeqDelay) + 'ms' +
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
    // Alex's report: the pill used to fade in at a flat delay regardless of
    // when this play's own last out actually lands in the animation -
    // reading as the half-inning being "over" before the out choreography
    // (the throw, the catch, the runner turning red) had even resolved.
    // haloutLastMs (hoisted above tokens.map now, strandedSafe tokens need
    // the same real moment too) is raw/unanchored same as everywhere else
    // here, so runnerSeqDelay (0 on a steal, FIELD_SEQUENCE_DELAY_MS
    // otherwise - that var's own comment) is what turns it into an absolute
    // delay.
    var breakDelayMs = isHalfEnd ? haloutLastMs + runnerSeqDelay : 0;
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
      (isHalfEnd ? "--break-delay:" + breakDelayMs + "ms;" : "");
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
        (SHOW_FIELDER_TOKENS ? fielderTokensHtml(m, flight, moves, seqDelay) : "") +
        pitchBallHtml(m, flight, wheelFinishMs) +
        ballFlightHtml(m, flight, moves, seqDelay) +
        throwHtml(m, flight, moves, seqDelay) +
        stealThrowHtml(m, moves, runDelay, outDelay, runnerSeqDelay) +
        fielderNameLabelsHtml(m, flight, seqDelay) +
        onDeckRunnerLabelsHtml(m) +
        ballResultLabelHtml(m, flight, seqDelay) +
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
    var rawPlays = slide.gamePlays || [];
    var upto = slide.gameIdx;
    // Drop the on-deck "Now Batting" placeholder from the ribbon entirely
    // (Alex's report: reaching it visibly shifted the x-axis) - it isn't a
    // real event, so it should never count toward a half-inning's own
    // segment spacing (segs/xByIdx below) or add its own trailing point to
    // the line. Remaps upto (an index into rawPlays) to the same play's
    // index in the real-only array - or, when the current slide IS the
    // on-deck placeholder itself, to the last real play before it, so that
    // slide's ribbon renders pixel-identical to the last real play's own,
    // exactly where the game actually left off.
    var plays = [];
    var realIdx = [];
    rawPlays.forEach(function (p, i) {
      if (p.is_on_deck) { realIdx[i] = plays.length - 1; return; }
      realIdx[i] = plays.length;
      plays.push(p);
    });
    upto = (upto != null && realIdx[upto] != null) ? realIdx[upto] : upto;
    // Alex's report: the ribbon was missing entirely on a game's first play.
    // This used to require >=2 plays before rendering anything at all - but
    // everything below it already handles a single point correctly (the
    // segment-building loop at `for (var q = 1; q < pts.length; q++)` just
    // never runs when pts.length is 1, leaving fills/strokes empty and the
    // frame/marker rendering alone, exactly per the "no segments yet" comment
    // further down) - only the guard itself was stricter than the code it
    // was guarding actually needed.
    if (!plays.length || upto == null || upto < 0) return "";

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
    var ribbonColors = gameTeamColors(slide.homeAbbr, slide.awayAbbr);
    var homeHex = ribbonColors.home || "#4a6fa5";
    var awayHex = ribbonColors.away || "#9aa4b2";
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
      // No "before" to diff against (the very first plotted play, or the
      // on-deck placeholder, which never gets a win_prob_before - see
      // _next_batter_moment) - fall back to whoever's actually leading right
      // now instead of defaulting to home. gain stays null in that case, so
      // the WPA delta label below is already skipped, not just the team pick.
      var homeGained = homeDelta == null ? wpAfter >= 0.5 : homeDelta >= 0;
      var gainAbbr = homeGained ? slide.homeAbbr : slide.awayAbbr;
      var gainPct = homeGained ? wpAfter : 1 - wpAfter;
      var gain = homeDelta == null ? null : Math.abs(homeDelta) * 100;
      // Only the badge itself needs to stay inside the plot; the readout flips
      // to the other side once the point is far enough right.
      var markLeft = Math.max(1.7, Math.min(98.3, xPct));
      // The ring around the badge is the colour of the team inside it, so the
      // marker reads as belonging to them rather than to the chart - the
      // SAME (possibly-substituted) colour the line/fill above just used for
      // this team, not a fresh independent teamColor() lookup, so the badge
      // can never name a different shade than the curve it's labeling.
      var gainHex = (homeGained ? homeHex : awayHex) || lastHex;
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
  // r/circleClass (Alex's ask): reused at BALL_R with a "ball-body" class for
  // the pitch ball and the flight ball's own marker (pitchBallHtml,
  // ballFlightHtml, throwLineHtml's new moving throw-ball - all three want
  // this exact baseball, sized/bordered identically), not just the wheel's
  // own tiny WHEEL_DOT_R dot. Seam control points scale proportionally
  // (r/WHEEL_DOT_R) rather than living as a second hand-tuned set of
  // constants at the new size - same curve shape at any radius. Defaults
  // reproduce the original wheel-only call exactly (r=WHEEL_DOT_R, no class).
  function wheelBallIconSvg(r, circleClass) {
    var radius = r || WHEEL_DOT_R;
    var k = radius / WHEEL_DOT_R;
    var cls = circleClass ? ' class="' + circleClass + '"' : "";
    function sx(v) { return (v * k).toFixed(2); }
    return '<circle' + cls + ' r="' + radius + '"></circle>' +
      '<path class="wheel-dot-seam" d="M ' + sx(-2.4) + ',' + sx(-1.7) + ' Q ' + sx(-0.8) + ',0 ' +
        sx(-2.4) + ',' + sx(1.7) + '"></path>' +
      '<path class="wheel-dot-seam" d="M ' + sx(2.4) + ',' + sx(-1.7) + ' Q ' + sx(0.8) + ',0 ' +
        sx(2.4) + ',' + sx(1.7) + '"></path>';
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

  // Steal DIFF (Alex's ask): the "offense" role here is a runner breaking for
  // the next base, not a batter at the plate - a bat reads wrong for that.
  // Drawn upright and always facing the same way (no rotate(angleDeg+180) -
  // unlike the bat, this icon has no "point this end out" story, so
  // wheelMarkerHtml skips that rotation for it). Real vector data this time
  // (running_shoe.svg, Alex's file - "Created by Nawicon from the Noun
  // Project", per that file's own credit text), not a by-eye redraw - the
  // path `d` below is copied verbatim from its single source <path>
  // (viewBox "0 0 32 40"), minus its own last two M...Z sub-loops (the two
  // motion-line bars trailing the heel - Alex's ask, this pass, was to drop
  // those; everything before them is the shoe silhouette itself, unedited).
  //
  // The transform centers and scales that path into this file's own small
  // icon-space convention (the bat/ball icons both live in roughly a +-4
  // unit box around the origin): the shoe-only path's real bounding box is
  // x:[2.8,30] y:[2,30] (measured directly, getBBox, after dropping the
  // motion bars - they'd pulled the box further left/down), so
  // translate(-16.4,-16) centers it on its own midpoint first, then
  // scale(0.33) brings that ~28-unit box down to a ~9.2-unit one - Alex's
  // ask to size it up a little from the first pass (2/7, an 8-unit box)
  // landed it right at the bat icon's own ~9.2-unit span (WHEEL_BAT_PATH),
  // rather than an arbitrary bump. stroke-width below is left as-is
  // (declared in this group's own pre-scale coordinate space, same as the
  // path data itself) so it scales up right along with the geometry instead
  // of holding the old, now-comparatively-thinner line weight.
  var WHEEL_SHOE_PATH = "M24,18a1,1,0,0,1-1-1V10a2,2,0,0,0-2-2H19.59A3.59,3.59,0,0,1,16,4.41,2.41,2.41,0,0,0,13.59,2a2.37,2.37,0,0,0-1.71.71L3.71,10.88a3,3,0,0,0,0,4.24L17.12,28.54A5,5,0,0,0,20.66,30H27a3,3,0,0,0,3-3V24A6,6,0,0,0,24,18ZM13.29,4.12a.42.42,0,0,1,.3-.12.42.42,0,0,1,.41.41A5.6,5.6,0,0,0,19.59,10H21v2H19a1,1,0,0,0,0,2h2v2H19a1,1,0,0,0,0,2h2.18A3,3,0,0,0,24,20a4,4,0,0,1,4,4H21.07L7.24,10.17ZM27,28H20.66a3,3,0,0,1-2.12-.88L5.12,13.71a1,1,0,0,1,0-1.42l.71-.71L20,25.71a1.05,1.05,0,0,0,.71.29H28v1A1,1,0,0,1,27,28Z";
  // Just the outer silhouette loop (WHEEL_SHOE_PATH's own first M...Z,
  // before the two inner cutout loops that carve out the collar/tab/stitch
  // detail lines) - Alex's ask: those cutouts read as transparent right now
  // (a single path, nonzero fill rule - the inner loops are real holes, not
  // a colour of their own, so they show whatever's behind the icon: the
  // wheel's own card background, which flips dark in dark mode). A solid
  // white copy of just the outer boundary, layered underneath the full
  // (team-coloured) path in wheelRunnerIconSvg below, shows through exactly
  // those hole regions - the same "colour ink over a white backing" two-tone
  // look the source icon's own line-art style implies, pinned to white
  // rather than left to the theme.
  var WHEEL_SHOE_OUTLINE_PATH = "M24,18a1,1,0,0,1-1-1V10a2,2,0,0,0-2-2H19.59A3.59,3.59,0,0,1,16,4.41,2.41,2.41,0,0,0,13.59,2a2.37,2.37,0,0,0-1.71.71L3.71,10.88a3,3,0,0,0,0,4.24L17.12,28.54A5,5,0,0,0,20.66,30H27a3,3,0,0,0,3-3V24A6,6,0,0,0,24,18Z";
  function wheelRunnerIconSvg() {
    return '<g transform="scale(0.33) translate(-16.4,-16)">' +
      '<path class="wheel-shoe-bg" d="' + WHEEL_SHOE_OUTLINE_PATH + '"></path>' +
      '<path class="wheel-shoe" d="' + WHEEL_SHOE_PATH + '"></path>' +
    "</g>";
  }

  // angleDeg is the marker's own position on the ring, in wheelPt's
  // convention (0 = straight up from centre, clockwise). The bat is drawn
  // barrel-down/handle-up in its own local coordinates, so rotating it by
  // angleDeg+180 swings the handle to point straight in at the wheel's
  // centre and the barrel straight out - perpendicular to the ring at that
  // point, every time, regardless of where the marker lands.
  // offIcon picks which shape the "off" role gets ("bat", the default, or
  // "runner" for a steal - see sceneWheelDiffHtml); offColorHex optionally
  // overrides its fill/stroke with a team's own color (via currentColor -
  // see .wheel-dot-runner in style.css) instead of the fixed wood-bat tones,
  // for the runner icon specifically (Alex's ask). Only the bat rotates to
  // point outward along the ring - a runner has no "this end points out"
  // story the way a bat's barrel does, so it stays upright regardless of
  // where on the ring it lands.
  function wheelMarkerHtml(pt, angleDeg, cls, dotIdxCls, offIcon, offColorHex) {
    var isOff = cls === "off";
    var isRunner = isOff && offIcon === "runner";
    var xf = "translate(" + pt.x.toFixed(2) + "," + pt.y.toFixed(2) + ")" +
      (isOff && !isRunner ? " rotate(" + (angleDeg + 180).toFixed(1) + ")" : "");
    var style = isRunner && offColorHex ? ' style="color:' + escapeHtml(offColorHex) + '"' : "";
    return '<g transform="' + xf + '"><g class="wheel-dot ' + dotIdxCls + ' wheel-dot-' + cls +
      (isRunner ? " wheel-dot-runner" : "") + '"' + style + '">' +
      (isOff ? (isRunner ? wheelRunnerIconSvg() : wheelBatIconSvg()) : wheelBallIconSvg()) +
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
     spray direction does, for either hand. offIcon/offColorHex (Alex's ask)
     swap the "off" role's marker from the default bat to a runner in a
     team's own color, for a steal's DIFF wheel - see wheelMarkerHtml. */
  function wheelHtml(label, v1, v2, mod, cls1, cls2, centerBig, centerSmall, band, arcCls, pinTop, mirrored,
                      offIcon, offColorHex, wheelPace) {
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
    // --wheel-pace (Alex's ask, steals only - stealWheelPace): every timing
    // calc() in style.css's wheel rules divides by this alongside
    // --play-speed, so omitting it here (every non-steal caller) is
    // identical to the explicit 1 a steal's DIFF wheel passes for a blowout
    // diff - var(--wheel-pace,1)'s own fallback covers both the same way.
    var paceStyle = wheelPace && wheelPace !== 1 ? ' style="--wheel-pace:' + wheelPace.toFixed(3) + '"' : "";
    return '<div class="wheel"' + paceStyle + '>' +
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
        wheelMarkerHtml(dot1Pt, deg1, cls1, "wheel-dot-1", offIcon, offColorHex) +
        wheelMarkerHtml(dot2Pt, deg1 + deltaDeg, cls2, "wheel-dot-2", offIcon, offColorHex) +
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
      // Runner (offense, runner marker in the stealing team's own color)
      // breaks first; catcher (defense, baseball marker) throws second - per
      // Alex's spec, "runner # then progress to catcher #". No archetype
      // band - steals don't have one.
      return wheelHtml("DIFF", m.steal_num, m.throw_num, 1000, "off", "def",
        String(Math.abs(signedCirc(m.steal_num, m.throw_num, 1000))), null, null,
        wasOut ? "out" : "hit", null, null, "runner", teamColor(m.off_team_abbr), stealWheelPace(m));
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
  // SPRAY (labelled "HZ" internally until Alex's rename ask): batted balls
  // only (Alex's call) - `flight` truthy is exactly that gate (flightParams
  // returns null for everything else, the same set sceneFlightReadoutHtml
  // below already checks against).
  //
  // flight.angle itself stays 0-90 (0 = dead down the 3B line, 90 = dead
  // down the 1B line) everywhere else in this file - fenceAt, dirtEdgeFt,
  // HZ_FIELDER_BY_ANGLE, coveringPosition, the LF/CF/RF split, all of it is
  // built on that range, so re-basing the value itself would mean touching
  // every one of those. The -45/+45 convention (Alex's ask, to match other
  // sources: -45 = 3B line, 0 = dead center, +45 = 1B line) is display-only,
  // applied right here where the wheel's center label is built - the only
  // place flight.angle ever reaches the screen as a number.
  function sceneWheelHzHtml(m, flight) {
    if (!flight) return "";
    var d1p = lastDigit(m.pitch), d1s = lastDigit(m.swing);
    var mirrored = effectiveHand(m.batter_hand) === "L";
    return wheelHtml("SPRAY", d1p, d1s, 10, "def", "off", (flight.angle - 45).toFixed(0) + "°", null, null,
      "neutral", true, mirrored);
  }

  // Illustrative DIFF/SPRAY wheels for the methodology panel (Alex's ask) -
  // all made-up but internally consistent plays, run through the real
  // sceneWheelDiffHtml/sceneWheelHzHtml (and, where a real LA is needed,
  // flightParams) pipeline rather than static screenshots, so they always
  // match the live wheel styling/theme and never go stale if that styling
  // changes. Four sets, one per methodology-panel spot that needs one -
  // wireMethodology renders each into its own placeholder div, lazily, the
  // first time the panel opens.
  //
  // sceneWheelDiffHtml never actually reads its own `flight` argument (DIFF
  // is pure pitch/swing/diff/result) - null is fine wherever a real one
  // isn't already at hand. sceneWheelHzHtml does need a real flight.angle,
  // so the spray/topped/combined sets run flightParams first.

  // Same result (1B) for all three, band lo/hi = 49/157 (meta.json) - diff
  // alone moves across it: 150 sits past hi (weak, q clamps toward 0), 103
  // sits near the midpoint (average), 40 sits under lo (strong, q clamps to
  // 1). pitch held at 100 throughout so swing alone is doing the work.
  var METHODOLOGY_CONTACT_EXAMPLES = [
    { label: "Weak", m: { result: "1B", pitch: 100, swing: 250, diff: 150 } },
    { label: "Average", m: { result: "1B", pitch: 100, swing: 203, diff: 103 } },
    { label: "Strong", m: { result: "1B", pitch: 100, swing: 140, diff: 40 } },
  ];
  function methodologyContactWheelsHtml() {
    return METHODOLOGY_CONTACT_EXAMPLES.map(function (ex) {
      return '<div class="methodology-wheel-example">' +
        '<div class="methodology-wheel-pair">' + sceneWheelDiffHtml(ex.m, null) + "</div>" +
        '<div class="methodology-wheel-caption">' + escapeHtml(ex.label) + "</div>" +
      "</div>";
    }).join("");
  }

  // Same |diff| (114), opposite sign of the pitch->swing circular delta -
  // swing=314 (pitch+114, onTop=true) vs swing=86 (pitch-114, onTop=false) -
  // isolates the topped/uppercut split from contact quality. diff=114 on
  // the 1B band (lo 49/hi 157) lands q~0.40, NOT the ~0.99 the first version
  // used - Alex's report: near q=1 (excellent contact) the 1B band's own
  // stations converge laTopped/laUppercut to the same value (both 16° at
  // q=0.99/1.0 in meta.json), which defeated the example's whole point.
  // q~0.40's station (7°/12°, meta.json) keeps a real, visible gap. Caption
  // pulls the real resulting LA out of flightParams rather than a hand-typed
  // number, so it can't drift out of sync with the actual station lookup.
  var METHODOLOGY_TOPPED_EXAMPLES = [
    { label: "Above - Topped", m: { result: "1B", pitch: 200, swing: 314, diff: 114, batter_hand: "R" } },
    { label: "Below - Uppercut", m: { result: "1B", pitch: 200, swing: 86, diff: 114, batter_hand: "R" } },
  ];
  function methodologyToppedWheelsHtml() {
    if (!data.meta || !data.meta.flight) return "";
    return METHODOLOGY_TOPPED_EXAMPLES.map(function (ex) {
      var flight = flightParams(ex.m, data.meta.flight);
      var caption = ex.label + (flight ? " (LA " + flight.la.toFixed(0) + "°)" : "");
      return '<div class="methodology-wheel-example">' +
        '<div class="methodology-wheel-pair">' + sceneWheelDiffHtml(ex.m, flight) + "</div>" +
        '<div class="methodology-wheel-caption">' + escapeHtml(caption) + "</div>" +
      "</div>";
    }).join("");
  }

  // pitch held at 105 throughout, only swing's LAST digit changes (1/5/9) -
  // isolates spray direction the same way the contact set isolates diff.
  // Overall diff stays in the high-80s/low-90s for all three (same rough
  // contact quality) so LA/EV don't also swing with it.
  var METHODOLOGY_SPRAY_EXAMPLES = [
    { label: "Pulled", m: { result: "1B", pitch: 105, swing: 191, diff: 86, batter_hand: "R" } },
    { label: "Middle", m: { result: "1B", pitch: 105, swing: 195, diff: 90, batter_hand: "R" } },
    { label: "Opposite Field", m: { result: "1B", pitch: 105, swing: 199, diff: 94, batter_hand: "R" } },
  ];
  function methodologySprayWheelsHtml() {
    if (!data.meta || !data.meta.flight) return "";
    return METHODOLOGY_SPRAY_EXAMPLES.map(function (ex) {
      var flight = flightParams(ex.m, data.meta.flight);
      if (!flight) return "";
      return '<div class="methodology-wheel-example">' +
        '<div class="methodology-wheel-pair">' + sceneWheelHzHtml(ex.m, flight) + "</div>" +
        '<div class="methodology-wheel-caption">' + escapeHtml(ex.label) + "</div>" +
      "</div>";
    }).join("");
  }

  // Two full plays, DIFF+SPRAY together, pushed toward the strong/weak ends
  // of contact quality (q) while keeping diff INSIDE the real band range for
  // that result (Alex's report: a diff outside the band's own lo/hi doesn't
  // correspond to any real historical play for that result, which is more
  // confusing than illustrative even if it exaggerates the point). A pulled
  // single with diff 53, just above the 1B band's lo of 49 (q~0.96, about as
  // clean as real contact for a single gets) and an opposite-field flyout
  // with diff 274, just under the FO band's hi of 277 (q~0.03, about as late
  // as real contact for a flyout gets). Captions lead with contact quality,
  // then direction, matching the wheels' own left-to-right DIFF-then-SPRAY
  // order.
  var METHODOLOGY_COMBINED_EXAMPLES = [
    { label: "Excellent contact, pulled single",
      m: { result: "1B", pitch: 130, swing: 77, diff: 53, batter_hand: "R", outs_before: 0, outs_after: 0 } },
    // pitch 863/swing 137 (not 212/486 - same diff 274, same opposite-field
    // spray) - Alex's report: 212/486 put its two wheel-val labels at deg1
    // 76deg/deg2 175deg (wheelAngleOf/wheelPt, WHEEL top=0deg, clockwise),
    // the second one landing right at the bottom of the ring, the one
    // example Alex flagged as being in the way of a tighter wheel-to-caption
    // gap. 863/137 places them symmetrically at 311deg/49deg instead - both
    // in the upper half, same distance (49deg) either side of top.
    { label: "Weak contact, opposite-field flyout",
      m: { result: "FO", pitch: 863, swing: 137, diff: 274, batter_hand: "R", outs_before: 0, outs_after: 1 } },
  ];
  function methodologyCombinedWheelsHtml() {
    if (!data.meta || !data.meta.flight) return "";
    return METHODOLOGY_COMBINED_EXAMPLES.map(function (ex) {
      var flight = flightParams(ex.m, data.meta.flight);
      if (!flight) return "";
      return '<div class="methodology-wheel-example">' +
        '<div class="methodology-wheel-pair">' + sceneWheelDiffHtml(ex.m, flight) + sceneWheelHzHtml(ex.m, flight) + "</div>" +
        '<div class="methodology-wheel-caption">' + escapeHtml(ex.label) + "</div>" +
      "</div>";
    }).join("");
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
    // On-deck: there's no result yet, so no label to look up - just says
    // who's up (Alex's call: "Now Batting" over "At Bat", to avoid repeating
    // sceneDetailHtml's own "AT BAT" matchup-row label right below it).
    if (m.is_on_deck) {
      // Same "offense" styling every real hitting result pill gets (Alex's
      // ask) - the feed card's own on-deck pill already matched this
      // (resultCat: "hitting" in card()); the slideshow's had its own
      // separate on-deck treatment until now.
      return '<div class="scene-result-line">' +
        '<span class="result-pill offense">Now Batting</span>' +
      "</div>";
    }
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
     offset or the scoreboard ticks before the runner visibly arrives.

     Alex's report (Crimson Chin's 3-run HR, session 4, CAR@MIA bot 2): the
     scorebug's own count-up was firing well before the runner tokens
     actually crossed the plate. Two things were missing, both already
     present in sceneFieldHtml's own per-move timing for the exact same
     runners: (1) the non-caught case had no lead-in at all (0), when a
     normal safe/scoring runner always starts running RUNNER_LEAD_MS after
     slide mount (sceneFieldHtml's own runDelay), not instantly; (2) these
     times are consumed as a RAW, unanchored delay (scoreCellHtml's --at/
     --until go straight into CSS with nothing else added on top - unlike
     scorebugOutsHtml's own dots, whose comment explicitly flags this
     function's times as "raw" for exactly that reason) but were never
     anchored with the same runnerSeqDelay (0 on a steal,
     FIELD_SEQUENCE_DELAY_MS=810ms otherwise) every other raw timing source
     here needs before it's usable as a real delay - recomputed locally
     rather than threaded in as a param, same as scorebugOutsHtml's own
     dotSeqDelay sibling computation. Also now uses STEAL_LEG_DUR_MS instead
     of the full base-to-base RUN_LEG_MS on a steal-of-home run, matching
     sceneFieldHtml's own isStealAdvance case (a scored move is never
     strandedSafe/a retreat, so that condition reduces to just "this play
     has a steal attempt" for scoreArrivals' own purposes). */
  function scoreArrivals(m, flight) {
    var moves = m.runner_moves || deriveRunnerMoves(String(m.obc_before || "000"),
                                  String(m.obc_after || "000"), m.runs || 0);
    var catchMs = flight && CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
    var runDelay = flight ? RUNNER_LEAD_MS : 0;
    var stealOut = stealThrowTarget(m, moves);
    var runnerSeqDelay = stealOut ? 0 : FIELD_SEQUENCE_DELAY_MS;
    var times = [];
    moves.forEach(function (mv) {
      if (!mv.scored) return;
      var legs = basepathWaypoints(mv.from, mv.to, true).length;
      var dur = stealOut ? stealLegMs(m, mv.from) : runnerLegMs(m, mv.from, Math.min(legs, RUN_LEG_MS.length - 1));
      var lead = catchMs ? catchMs + TAG_UP_MS : runDelay;
      times.push(lead + runnerSeqDelay + dur);
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

  // Every out this play actually records, each at its own real moment -
  // outThrowEndByBase's throw arrivals first (a DP's two outs land at two
  // different times, matching two different throws), padded out with the
  // catch moment (caught in the air), the caught-stealing tag moment, or
  // the old flat "ball travel + beat" fallback (a no-flight, non-steal out -
  // K, an uncorroborated retreat/stranded case) for any out neither one
  // accounts for. Sorted so dot i+1's out never reads as happening before
  // dot i's (Alex's ask: no more one shared guess for every out on a multi-
  // out play - see scorebugOutsHtml below, and sceneFieldHtml's own per-
  // runner lookup off the same outThrowEndByBase/stealOutAtMs sources, so
  // the token that's actually out turns red at this identical moment too).
  function outAtMomentsMs(m, flight, count) {
    if (!count) return [];
    var moves = resolveRunnerMoves(m);
    var endByBase = outThrowEndByBase(m, moves, flight);
    var moments = [];
    for (var base in endByBase) if (endByBase.hasOwnProperty(base)) moments.push(endByBase[base]);
    var catchMs = flight && CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
    var stealAt = stealOutAtMs(m, moves);
    var fallback = ballTravelMs(flight) + OUT_BEAT_MS;
    while (moments.length < count) moments.push(catchMs || stealAt || fallback);
    moments.sort(function (a, b) { return a - b; });
    return moments;
  }

  // Outs as plain dots, no "OUTS" label (the header scorebug's own inning
  // row already reads as game state at a glance). Three dots, not the live
  // feed's up-to-two-then-a-badge-swap convention (stateStack) - this is a
  // replay of a completed play, not a live scoreboard, so the third out
  // gets its own dot same as the old sceneOutsHtml did.
  // Dots already on before this play (outs_before) render on immediately -
  // they're not what THIS play's animation is showing. Dots that turn on
  // DURING this play (between outs_before and outs_after) fill in at
  // outAtMomentsMs' own real per-out timing (Alex's ask - was one shared
  // "ball travel + OUT_BEAT_MS" guess for every out on the play, now each
  // dot lands exactly when its own out is actually recorded: the catch, or
  // whichever throw resolves it).
  function scorebugOutsHtml(m, flight) {
    var before = Math.max(0, Math.min(3, m.outs_before || 0));
    var after = Math.max(0, Math.min(3, m.outs_after == null ? before : m.outs_after));
    var moments = outAtMomentsMs(m, flight, after - before);
    // A steal attempt's runner token now moves on runnerSeqDelay (0, not the
    // usual field-sequence hold - sceneFieldHtml's own comment) since it
    // breaks on the pitch itself, not after the wheel - the dot has to hold
    // the exact same anchor or it drifts out of sync with that token's own
    // --outat the moment this is a caught-stealing out.
    var dotSeqDelay = stealThrowTarget(m, resolveRunnerMoves(m)) ? 0 : FIELD_SEQUENCE_DELAY_MS;
    var dots = [0, 1, 2].map(function (i) {
      if (i < before) return '<span class="dot on"></span>';
      // +dotSeqDelay here (unlike scoreArrivals' own raw times): sceneFieldHtml's
      // matching runner token turns red off this exact same outAtMomentsMs/
      // outThrowEndByBase source, and that token's own movement never starts
      // before its --rdelay, which always carries the same held anchor - an
      // un-held dot could otherwise fire before the runner has even appeared
      // at the base it's supposedly being put out at. Keeping both on the
      // same held clock is what makes "the dot and the runner turn red at
      // the very same moment" (Alex's ask) literally true, not just close.
      if (i < after) {
        return '<span class="dot new" style="--delay:' +
          (moments[i - before] + dotSeqDelay) + 'ms"></span>';
      }
      return '<span class="dot"></span>';
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
          scorebugOutsHtml(m, flight) +
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

  // Live-edge placeholder for an in-progress game (Alex's ask, ideas-and-
  // opinions conversation): "who's due up next," not a real play. Runs
  // through the exact same play-scene pipeline as a real play rather than a
  // separate simplified scene (Alex's later call, after seeing the first
  // pass) - flight/pitch/swing/result/diff are all null on this moment, and
  // every sub-component below (wheels, flight readout, defense line, the
  // phantom-batter-walk guard just above) already degrades gracefully given
  // that shape. Only sceneResultPillHtml needs an explicit is_on_deck check
  // (there's no result to look up a label for), and _next_batter_moment
  // carries win_prob_after forward so the ribbon still has a "current odds"
  // marker to show.
  function playSceneHtml(slide) {
    var m = slide.play;
    var flight = flightParams(m, data.meta.flight);
    if (flight) {
      var hand = effectiveHand(m.batter_hand);
      var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
      // No wasOut gate on the ground-archetype branch (Alex's ask): a
      // grounder-family archetype's real out codes (GO/DP/FC/...) always
      // have wasOut=true already, so this is a no-op change for them, but a
      // bunt/infield_single HIT (B1B, IF1B - wasOut=false by definition) now
      // races the same charge-in system instead of falling to resolveHit
      // Pickup's plain dirt-edge cap below - the fielder genuinely gets a
      // shot at it and is just too late, rather than the ball only ever
      // appearing already sitting dead at the fringe.
      if (GROUND_ARCHETYPES[flight.archetype]) {
        resolveGrounderInterception(m, flight, hand);
      } else if (wasOut && CAUGHT_IN_AIR[flight.archetype]) {
        applyAirPositionOverride(m, flight, hand);
      } else if (flight.archetype === "single") {
        // Outfielders race a single too (Alex's ask), scoped to just this
        // one archetype - a double/triple/HR needs to genuinely get past
        // the nearest fielder to track with its own already-locked result.
        resolveSinglePickup(m, flight, hand);
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
    return '<div class="play-scene' + (m.is_on_deck ? " on-deck" : "") + '" data-key="' +
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
      // Same on-deck exclusion as catchUpPlayCount's headline number (Alex's
      // ask) - the "Now Batting" placeholder rides along in g.plays for
      // end-of-game context but was never a real new play.
      var gNewPlayCount = g.plays.filter(function (p) { return !p.is_on_deck; }).length;
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
          " · " + gNewPlayCount + (gNewPlayCount === 1 ? " new play" : " new plays") + "</div>" +
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
     rather than burying in a constant change.

     Flag for Alex #2: FIELD_SEQUENCE_DELAY_MS (810ms, wheels-then-field
     sequencing) is added back in below so the same real reading-time budget
     survives now that the field's own animation starts 810ms later than it
     used to - every slide's total on-screen time grows by that same 810ms.
     For 656 plays that's another ~9 minutes on top of the 48-50 above -
     same kind of runtime tradeoff, surfaced the same way rather than folded
     silently into the base constants. */
  // Key moments no longer get extra read time over a routine play (Alex's
  // ask) - the real-animation-length estimate below already scales dwell up
  // for whatever a specific play's own action actually needs, so a flat
  // "key moments are just more important, give them more time" bonus on top
  // of that was redundant with what the animation already communicates.
  var PLAY_DWELL_MS_ROUTINE = 3600;
  // Extra beat on the play that opens a half-inning, so the break between
  // halves registers instead of the reel running straight through it.
  var HALF_INNING_BONUS_MS = 800;
  // The last play of a half-inning: how much extra dwell that slide gets on
  // top of its normal reading time so the "Mid Nth"/"End Nth" pill (fades in
  // once this play's own last out is actually recorded - sceneFieldHtml's
  // isHalfEnd/breakDelayMs, off the same outAtMomentsMs every out-timing
  // consumer here uses) isn't just barely on screen before auto-advance cuts
  // it off - same reasoning as HALF_INNING_BONUS_MS's own beat, just sized
  // like a title slide's dwell (TITLE_DWELL_MS) since the pill is
  // effectively a title card appearing in place.
  var CF_BREAK_BONUS_MS = 1800;

  // Real-time running/ball flight (Alex's call, ideas-and-opinions
  // conversation) means individual plays now vary hugely in how long their
  // own animation actually takes - a strikeout finishes almost immediately,
  // a bases-loaded double can send a runner the better part of a full real-
  // time circuit (up to RUN_LEG_MS[4], 13+ real seconds). The single fixed
  // "animation always finishes well inside ~2700ms" assumption baked into
  // PLAY_DWELL_MS_ROUTINE/KEY (see that constant's own comment) no longer
  // holds, so this estimates THIS play's own animation length directly
  // instead. Not a byte-for-byte replay of sceneFieldHtml's own per-move
  // timing (forced-on-contact/tag-up/retreat branches, each with their own
  // start delay) - a reasonable UPPER-BOUND estimate of the slowest token's
  // own duration: an out-bound runner always costs the fixed out-
  // choreography total (OUT_CHOREOGRAPHY_MS - the same regardless of which
  // base was targeted, since that animation's shape doesn't scale with
  // distance), a safe runner costs RUN_LEG_MS for however many bases they
  // actually covered. Erring toward a slightly-too-generous estimate (a
  // fixed start-delay margin folded in below) is the safe failure mode here -
  // an animation cut off mid-flight by an early auto-advance reads as
  // broken, a slide that lingers a little past when it strictly needed to
  // doesn't.
  function estimatedRunMs(play) {
    var moves = deriveRunnerMoves(String(play.obc_before || "000"), String(play.obc_after || "000"), play.runs || 0);
    var worst = 0;
    moves.forEach(function (mv) {
      var startOrd = mv.from === "BATTER" ? 0 : BASE_ORDINAL[mv.from];
      if (startOrd == null) return;
      if (mv.to === "OUT") {
        worst = Math.max(worst, OUT_CHOREOGRAPHY_MS);
        return;
      }
      var endOrd = mv.scored ? 4 : BASE_ORDINAL[mv.to];
      if (endOrd == null) return;
      var legs = Math.min(Math.max(endOrd - startOrd, 0), RUN_LEG_MS.length - 1);
      worst = Math.max(worst, runnerLegMs(play, mv.from, legs));
    });
    return worst;
  }

  function slideDwell(slide) {
    var speed = getPlaybackSpeed();
    if (slide.kind !== "play") return TITLE_DWELL_MS / speed;
    var play = slide.play;
    var readBudget = PLAY_DWELL_MS_ROUTINE;
    var isHalfEnd = !!play.is_half_inning_final && !play.is_game_final;
    var flight = flightParams(play, data.meta.flight);
    // A relay's throws are now sequential, not overlapping (Alex's ask -
    // see throwSchedule) - a double play's real throw chain can run well
    // past one throw's worth of time, and the dwell estimate must never
    // undercut that, or the slide would auto-advance mid-relay and cut the
    // second throw off before it's even drawn. Math.max against the old
    // flat single-throw budget rather than replacing it outright, so a play
    // with no throw at all keeps exactly its old (already-conservative)
    // estimate.
    var throwMs = THROW_DELAY_MS + THROW_DRAW_MS;
    var schedule = throwSchedule(play, deriveRunnerMoves(
      String(play.obc_before || "000"), String(play.obc_after || "000"), play.runs || 0), flight);
    if (schedule.length) {
      var lastThrowEnd = Math.max.apply(null, schedule.map(function (t) { return t.endMs; }));
      throwMs = Math.max(throwMs, lastThrowEnd - fieldedMs(flight));
    }
    // Real animation length first (ball travel/ground time plus whichever
    // token takes longest to finish), the same reading-time budget as
    // before on top of THAT rather than assumed already included in it -
    // slightly more generous total dwell than the pre-real-time numbers
    // worked out to, a deliberate tradeoff (see this function's own
    // comment).
    var animMs = fieldedMs(flight) + throwMs + estimatedRunMs(play);
    return (animMs + readBudget + FIELD_SEQUENCE_DELAY_MS + (startsHalfInning(slide) ? HALF_INNING_BONUS_MS : 0) +
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

  // eagerImages (live grid only - see mountLiveSlideInto) has to strip
  // loading="lazy" out of the HTML STRING before it's ever parsed into the
  // DOM, not flip the .loading property on the resulting <img> elements
  // afterward - a browser commits to lazy-loading (queues the image for
  // intersection-based deferral) the moment it parses that attribute while
  // building the element, and mutating the property post-insertion doesn't
  // reliably undo that decision (Alex's report: mutating it after the fact
  // wasn't actually fixing the slow sequential loading).
  function mountSlide(slideEl, slide, prev, eagerImages) {
    var html = catchUpSlideHtml(slide);
    if (eagerImages) html = html.replace(/ loading="lazy"/g, "");
    slideEl.innerHTML = html;
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
      // "one": stay on the same slide - a fresh full dwell, not the leftover
      // of the one that just fired, so it reads as playing again rather than
      // recovering an already-spent timer.
      showCatchUpSlide(getLoopMode() === "one" ? catchUp.index : catchUp.index + 1);
    }, ms);
  }

  function showCatchUpSlide(i) {
    if (i >= catchUp.slides.length) {
      // "all": the run wraps back to the first slide instead of closing -
      // same fresh-fade-in treatment slide 0 gets when the show first opens.
      if (getLoopMode() === "all") { showCatchUpSlide(0); return; }
      closeCatchUp();
      return;
    }
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

    // The closing slide is where the show stops on its own by default - it
    // waits for the user rather than blinking out from under them. On "all"
    // it still gets its own full dwell (the same TITLE_DWELL_MS a title slide
    // gets, since it's the same kind of pause-to-read card) before the wrap
    // above sends the run back to slide 0.
    if (slide.kind === "done") {
      if (getLoopMode() === "all" && !catchUp.paused) {
        scheduleCatchUp(slideDwell(slide));
      } else {
        clearCatchUpTimer();
      }
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
               // "done" only resumes into another dwell on "all" - the loop-
               // back that's about to happen there (see showCatchUpSlide).
               // Every other mode parks on it the same as before.
               (catchUp.slides[catchUp.index].kind !== "done" || getLoopMode() === "all")) {
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
    wireSpeedToggle("catchup-speed");
    wireLoopToggle("catchup-loop");
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
  function ensureSessionPlaysLoaded(session) {
    if (data.playsBySession[session]) return Promise.resolve(data.playsBySession[session]);
    return fetchSeasonJSON("plays_" + pad2(session) + ".json").then(function (rows) {
      data.playsBySession[session] = rows;
      return rows;
    });
  }

  function loadGameReplay(gameCode, session) {
    return ensureSessionPlaysLoaded(session).then(function () {
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
      // "one": stay on the same slide - a fresh full dwell, not the leftover
      // of the one that just fired.
      showReplaySlide(getLoopMode() === "one" ? replay.index : replay.index + 1);
    }, ms);
  }

  function showReplaySlide(i) {
    if (i >= replay.slides.length) {
      // "all": wrap back to the first slide instead of closing - same
      // fresh-fade-in treatment slide 0 gets when the replay first opens.
      if (getLoopMode() === "all") { showReplaySlide(0); return; }
      closeReplay();
      return;
    }
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

    /* A replay ends by holding on the game's actual last play by default,
       not by cutting to a summary card - so unlike Catch Me Up's "done" card,
       the final play has to stop the timer itself, or its dwell would expire,
       the index would run past the end and the overlay would auto-close. On
       "all" it still gets that same dwell first, then the wrap above sends
       the run back to slide 0 instead of holding. */
    if (i === replay.slides.length - 1) {
      if (getLoopMode() === "all" && !replay.paused) {
        scheduleReplay(slideDwell(slide));
      } else {
        clearReplayTimer();
      }
      return;
    }
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
               // The last play only resumes into another dwell on "all" -
               // the loop-back that's about to happen there (see
               // showReplaySlide). Every other mode rests there as before.
               (replay.index !== replay.slides.length - 1 || getLoopMode() === "all")) {
      scheduleReplay(replay.remaining || slideDwell(replay.slides[replay.index]));
    }
  }

  /* A live game's replay is a snapshot taken when it opens - it plays to the
     last recorded play and stops, rather than chasing a game still in progress.
     Same "don't chase a moving target" rule as Catch Me Up's cursor read.
     btn is optional - the live grid's single-game fallback (openLiveGrid)
     has no button of its own to spin a loading state on. */
  function openGameReplay(gameCode, session, btn) {
    if (session == null || isNaN(session)) {
      toast("Pick a session to replay a game.");
      return;
    }
    if (btn) btn.classList.add("loading");
    loadGameReplay(gameCode, session).then(function (plays) {
      if (btn) btn.classList.remove("loading");
      if (!plays.length) { toast("No plays recorded for that game yet."); return; }
      replay.slides = buildGameReplaySlides(plays);
      replay.index = -1;   // no previous slide, so slide 0 gets the full fade in
      replay.paused = false;
      $("replay-pause-hint").hidden = true;
      $("replay-card").classList.remove("paused");
      $("replay-modal").hidden = false;
      showReplaySlide(0);
    }).catch(function () {
      if (btn) btn.classList.remove("loading");
      toast("Could not load that game's plays.");
    });
  }

  function openGameReplayFor(btn) {
    var gameCode = btn.getAttribute("data-replay");
    var tile = btn.closest(".scoreboard-tile");
    var raw = tile && tile.getAttribute("data-session");
    var session = raw ? Number(raw) : filters.session;
    openGameReplay(gameCode, session, btn);
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
    if (btn) btn.classList.add("loading");
    loadGameReplay(gameCode, session).then(function (plays) {
      if (btn) btn.classList.remove("loading");
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
      if (btn) btn.classList.remove("loading");
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

  // ── Live grid: up to 8 games from the current session, each pane cycling
  //    its own field animation independently (Alex's spec - a "TV wall" of
  //    every game in the session rather than one game at a time; a play
  //    finishing in one game must never yank another pane forward early, so
  //    each pane keeps its own timer off its own slideDwell - see
  //    scheduleLivePane). Session-scoped, not a live/final filter - a
  //    finished game gets its own pane too, it just cycles a different
  //    sequence (see buildLiveGridSequence). ────────────────────────────────

  // introQueue/introActivePane stagger multiple games' intros (Alex's ask) -
  // at most one pane runs its own "new data" intro at a time, in the order
  // they were found this refresh cycle (== the panes' own grid order, since
  // that's already leverage-sorted from data.meta.games at open time - see
  // liveGridActivateNextIntro).
  var liveGrid = {
    open: false, panes: [], session: null, refreshTimer: null,
    introQueue: [], introActivePane: null,
  };

  /* Every play this pane will show, in cycling order.
     - Live (no is_game_final on the last real play): key moments, the most
       recent real play, the on-deck "Now Batting" placeholder that already
       rides along as gamePlaysFor's own last entry (_next_batter_moment),
       then every play in order - then loops back to key moments (Alex's
       spec).
     - Finished: key moments, then every play, then loops back to key moments
       ("the next time through the loop they show all plays").
     A game with no key moments yet falls back to every play for that first
     leg too, so the pane always has something to cycle rather than sitting
     empty - each slide's own `pass` ("key" or "other") is what
     mountLivePane's key-icon indicator reads, not is_key_moment directly, so
     a play that happens to be a key moment doesn't light the icon back up
     during the all-plays leg.

     Returns {sequence, resumeIndex}, not a bare array - resumeIndex is where
     the pane should land right after a "Live Look-In" cut card (Alex's ask:
     refreshLiveGridData rebuilds a pane's sequence when its game gets new
     plays, and the cut card should lead straight into that new play, not
     back to the top of the key-moments loop). It points at the "most
     recent real play" slide on a live sequence, or the game's actual last
     play on a finished one - both are "here's what's new" in their own
     branch. */
  function buildLiveGridSequence(game, plays) {
    if (!plays.length) return { sequence: [], resumeIndex: 0 };
    var last = plays[plays.length - 1];
    var onDeck = last.is_on_deck ? last : null;
    var real = onDeck ? plays.slice(0, -1) : plays;
    if (!real.length) {
      var only = onDeck ? [liveGridSlide(onDeck, plays.length - 1, plays, game, "other")] : [];
      return { sequence: only, resumeIndex: 0 };
    }

    var keySlides = [];
    plays.forEach(function (p, i) {
      if (p.is_key_moment && !p.is_on_deck) keySlides.push(liveGridSlide(p, i, plays, game, "key"));
    });
    if (!keySlides.length) {
      keySlides = real.map(function (p, i) { return liveGridSlide(p, i, plays, game, "key"); });
    }

    var allSlides = real.map(function (p, i) { return liveGridSlide(p, i, plays, game, "other"); });
    var isFinal = !!real[real.length - 1].is_game_final;
    if (isFinal) {
      var finalSeq = keySlides.concat(allSlides);
      return { sequence: finalSeq, resumeIndex: finalSeq.length - 1 };
    }

    var mostRecent = liveGridSlide(real[real.length - 1], real.length - 1, plays, game, "other");
    var seq = keySlides.concat([mostRecent]);
    var resumeIndex = seq.length - 1;   // the mostRecent slide just pushed
    if (onDeck) seq.push(liveGridSlide(onDeck, plays.length - 1, plays, game, "other"));
    return { sequence: seq.concat(allSlides), resumeIndex: resumeIndex };
  }

  // Same slide shape buildGameReplaySlides uses per play - reused so
  // mountSlide/catchUpSlideHtml/sceneRibbonHtml (which reads gamePlays/
  // gameIdx to draw the win-probability line before it's hidden below) all
  // work unmodified. pass ("key"/"other") is the one addition, read by
  // mountLivePane to drive the key-icon indicator.
  function liveGridSlide(p, idx, gamePlays, game, pass) {
    return {
      kind: "play", play: p, playNo: idx + 1, total: gamePlays.length,
      gamePlays: gamePlays, gameIdx: idx, ribbonFrom: 0,
      homeAbbr: game.home_team_abbr, awayAbbr: game.away_team_abbr,
      pass: pass,
    };
  }

  /* The scoreboard tile's own wp-bar (sb-foot .wp-bar/.wp-seg, scoreboardCard)
     reused as-is (Alex's ask: "the same bar like on the landing page
     scoreboard view") rather than the ribbon's chart-plus-badge - away% and
     home% flank it, and whichever side this play just helped gets a +WPA
     badge next to its own percentage. Built from win_prob_after/
     win_prob_before/batting_is_home, same raw-play fields sceneRibbonHtml's
     own marker used - wpFragment doesn't apply here, see the note that used
     to sit on this function for why. */
  /* startAwayPct/startHomePct (both null on a pane's first mount) are where
     the away overlay should render BEFORE animating to this play's own
     split - see mountLivePane, which passes the previous slide's split and
     then nudges the overlay to its real scaleX a frame later so the change
     from old to new actually transitions instead of just appearing.
     durationMs is that transition's length - mountLivePane passes the same
     slideDwell this play is actually on screen for (Alex's ask: "time it up
     to last the duration of the entire play"), so the bar finishes settling
     right around when the play itself is done rather than snapping early.
     Returns null (not a string) when there's no win-prob to show, so the
     caller can skip both insertion and the animation step cleanly. */
  function liveGridWpBarHtml(p, homeAbbr, awayAbbr, startAwayPct, startHomePct, durationMs) {
    var hw = homeWpOf(p);
    if (hw == null) return null;
    var homePct = Math.round(hw * 100);
    var awayPct = 100 - homePct;
    var initAway = startAwayPct == null ? awayPct : startAwayPct;
    var initHome = startHomePct == null ? homePct : startHomePct;
    var colors = gameTeamColors(homeAbbr, awayAbbr);
    var awayHex = colors.away || "#9aa4b2";
    var homeHex = colors.home || "#c7ccd3";

    var wpBefore = p.win_prob_before == null ? null
      : (p.batting_is_home ? p.win_prob_before : 1 - p.win_prob_before);
    var homeDelta = wpBefore == null ? null : (hw - wpBefore);
    var gain = homeDelta == null ? null : Math.abs(homeDelta) * 100;
    var awayWpa = (gain != null && homeDelta < 0)
      ? '<span class="live-grid-wpa wpa-pos">+' + gain.toFixed(1) + "</span>" : "";
    var homeWpa = (gain != null && homeDelta >= 0)
      ? '<span class="live-grid-wpa wpa-pos">+' + gain.toFixed(1) + "</span>" : "";

    // WPA rides on the outside of its own side's percentage (Alex's ask) -
    // away's badge left of "NN%", home's right of it - so it never sits
    // between a label and the bar it belongs next to.
    //
    // Every dimension on the bar itself is inline, not class-driven - the
    // stylesheet-only attempts before this weren't showing up for Alex, and
    // inline styles can't be lost to a cascade/specificity/caching issue the
    // way a class rule can.
    //
    // Fixed width, and the bar's own centre pinned to the row's true centre
    // (Alex's ask) - centring the label+bar+label GROUP as a unit (the
    // previous pass) let a WPA badge on only one side push that whole
    // group's centre off-true, dragging the bar sideways with it whenever
    // the two sides' content wasn't the same width. A grid with two equal
    // 1fr side tracks fixes that: both side columns are forced to the same
    // width by the grid algorithm regardless of what's in them, so the
    // middle (auto-width, the bar) column never moves - each label just
    // grows outward from its own side, away from the bar, however long it
    // needs to be. justify-self on each label keeps it hugging the bar's
    // edge rather than drifting to its own column's outer edge.
    // One animated overlay, not two - and it animates transform:scaleX, not
    // width (Alex's report: the width version read as choppy). Width is a
    // layout property; changing it forces a reflow on every frame, and with
    // up to 8 panes animating at once that's 8 reflows/frame competing for
    // the main thread. transform is compositor-only - the browser can run it
    // straight on the GPU without ever touching layout, which is the
    // standard fix for a janky width/left/top animation. The home side needs
    // no element of its own: it's just the bar's own background colour,
    // showing through wherever the away overlay (scaleX from the left edge)
    // doesn't cover.
    var awayFrac = initAway / 100;
    return {
      awayPct: awayPct, homePct: homePct,
      html: '<div class="live-grid-wp" style="display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:8px;width:100%;margin-top:2px;">' +
        '<span class="live-grid-wp-pct" style="justify-self:end;">' + awayWpa + awayPct + "%</span>" +
        '<div style="position:relative;width:150px;height:8px;border-radius:4px;overflow:hidden;' +
          'border:1px solid var(--border);background:' + escapeHtml(homeHex) + ';">' +
          '<div class="live-grid-wp-seg" style="position:absolute;left:0;top:0;width:100%;height:100%;' +
            'transform-origin:left;transform:scaleX(' + awayFrac + ');' +
            'transition:transform ' + (durationMs || 550) + 'ms ease;background:' + escapeHtml(awayHex) + ';"></div>' +
        "</div>" +
        '<span class="live-grid-wp-pct" style="justify-self:start;">' + homePct + "%" + homeWpa + "</span>" +
      "</div>",
    };
  }

  /* A trimmed sceneRoleHtml (6330) - no team logo (redundant once this sits
     right under the real logo in the scorebug) and no star-favorite button
     (Alex's ask - not enough room at this size to make it worth the tap
     target), just "AB: Name"/"P: Name". */
  // Last name only, not the full name (Alex's ask, dropping the whole
  // measure-and-justify approach in favor of just keeping the source text
  // short) - real last names in this data cap out at 18 characters, so
  // anything up to that shows in full; only the one real outlier past 18
  // gets cut, to 15 characters plus an ellipsis.
  var LIVE_GRID_MU_NAME_FULL_MAX = 18;
  var LIVE_GRID_MU_NAME_TRUNCATE_AT = 15;

  function liveGridLastName(name) {
    if (!name) return "-";
    var parts = name.trim().split(/\s+/);
    var last = parts[parts.length - 1];
    return last.length > LIVE_GRID_MU_NAME_FULL_MAX
      ? last.slice(0, LIVE_GRID_MU_NAME_TRUNCATE_AT) + "…"
      : last;
  }

  function liveGridMatchupLineHtml(prefix, name) {
    return '<div class="live-grid-mu">' + prefix + ": " + escapeHtml(liveGridLastName(name)) + "</div>";
  }

  /* logo/abbr/score have to move together as one unit (Alex's report: an
     earlier flex-wrap-based approach let them wrap apart from EACH OTHER
     too whenever a 4-letter team abbreviation made that row too wide for
     the column, dropping the score to its own line) - grouping them into
     their own row is what the team column's own flex-direction:column
     (style.css) depends on: exactly two children, this row and the matchup
     line, so only the matchup line can ever land on a second line. Run once
     per mount, right before the matchup line is inserted - mountSlide just
     rebuilt these three elements fresh, so there's nothing to guard against
     re-wrapping an already-wrapped row. */
  function liveGridWrapTeamRow(teamEl) {
    var row = document.createElement("div");
    row.className = "live-grid-team-row";
    while (teamEl.firstChild) row.appendChild(teamEl.firstChild);
    teamEl.appendChild(row);
  }

  // 10s modal-wide announcement, then 10s more on the specific pane itself
  // (Alex's ask - 20s total, split into a "something happened" beat over
  // the whole grid before narrowing to which game). The Replay flash ahead
  // of the slow-motion rerun is much shorter - the viewer's already looking
  // right at this pane by then.
  var LIVE_GRID_OVERLAY_MS = 10000;
  var LIVE_GRID_PANE_LOOKIN_MS = 10000;
  var LIVE_GRID_REPLAY_CUT_DWELL_MS = 2200;

  /* A transition card - MLN logo + a short label - for the two interstitials
     in a pane's "new data" intro (see refreshLiveGridData/advanceLivePane):
     "Live Look-In" (pulsing, holds a while, meant to actually be noticed) and
     "Replay" (a quick static flash ahead of the slow-motion rerun). Bypasses
     mountSlide/catchUpSlideHtml entirely since this isn't a real slide object
     with a .play, but reuses the same fade-in mechanic (remove "in", force
     reflow, re-add "in") every other slide here uses so it still reads as
     one consistent transition style rather than a one-off. */
  function mountLiveGridCutSlide(pane, text, pulsing) {
    pane.el.classList.remove("in");
    pane.el.innerHTML =
      '<div class="live-grid-cut">' +
        (pulsing ? '<span class="live-grid-cut-dot"></span>' : "") +
        '<img src="favicon.png" alt="" class="live-grid-cut-logo' + (pulsing ? " pulsing" : "") + '">' +
        '<div class="live-grid-cut-text">' + escapeHtml(text) + "</div>" +
      "</div>";
    void pane.el.offsetWidth;
    pane.el.classList.add("in");
    if (pane.keyIconEl) pane.keyIconEl.hidden = true;
    pane.prev = null;   // next real slide after this always gets a full fade in
  }

  // Same key glyph as #header-key-toggle (index.html) - a real <svg>, not a
  // fresh icon, so "cycling through key moments right now" reads as the same
  // concept as the Key Moments toggle elsewhere in the app. Lives as a
  // sibling of the slide container, not inside it - mountSlide replaces the
  // slide container's innerHTML on every mount, so anything meant to persist
  // and just toggle visibility (this icon, unlike the wp-bar/matchup lines,
  // which are cheap to rebuild each time) has to sit outside that subtree.
  function liveGridKeyIconSvg() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="7.5" cy="15.5" r="5.5"></circle>' +
      '<path d="M21 2l-9.6 9.6"></path>' +
      '<path d="M15.5 7.5l3 3L22 7l-3-3"></path>' +
    "</svg>";
  }

  function liveGridPaneHtml(game) {
    return '<div class="live-grid-pane" data-game="' + escapeHtml(game.game_code) + '">' +
      '<div class="catchup-slide" data-role="slide"></div>' +
      '<div class="live-grid-key-icon" data-role="key-icon" hidden title="Key moment">' +
        liveGridKeyIconSvg() +
      "</div>" +
      // Same corner as the key icon, mutually exclusive with it (Alex's ask
      // - the #live-grid-btn treatment, reused here rather than a fresh
      // style) - shown for the whole of a pane's own intro (mountLiveGrid
      // IntroStep/advanceLivePane), not just the cut cards.
      '<div class="live-grid-live-pill" data-role="live-pill" hidden>LIVE</div>' +
    "</div>";
  }

  /* The win-probability ribbon is hidden in a grid pane (style.css) - this
     mounts its replacement, a wp-bar row right under the scorebug (see
     liveGridWpBarHtml above), since that markup isn't part of playSceneHtml's
     own output and has to be inserted after every mount. */
  function mountLivePane(pane) {
    if (!pane.sequence.length) {
      pane.el.innerHTML = '<div class="live-grid-empty">No plays yet</div>';
      if (pane.keyIconEl) pane.keyIconEl.hidden = true;
      return;
    }
    mountLiveSlideInto(pane, pane.sequence[pane.index], false);
  }

  /* The actual per-slide mount work (mountSlide plus the wp-bar/matchup/
     scoring extras every live-grid slide gets) - factored out of
     mountLivePane so a "new data" intro step (advanceLivePane) can mount an
     arbitrary one-off slide (the new play at normal speed, then the same
     play again for the slow-motion replay) the same way normal cycling
     does, without going through pane.sequence/pane.index at all.
     forceFreshFade skips pane.prev for the isSameGameAdvance check - every
     intro step gets a full fade regardless of game_code, since showing the
     same play twice in a row is not a "same game advance" the way stepping
     to the next real play is. */
  function mountLiveSlideInto(pane, slide, forceFreshFade) {
    var prevSlide = forceFreshFade ? null : pane.prev;
    mountSlide(pane.el, slide, prevSlide, true);
    pane.prev = slide;
    if (pane.keyIconEl) pane.keyIconEl.hidden = slide.pass !== "key";
    var scorebug = pane.el.querySelector(".scene-scorebug");
    if (scorebug) {
      // Animate from the previous play's split to this one's (Alex's ask) -
      // start the bar rendering at the old split, then nudge it to the real
      // one a couple frames later so the browser has actually painted the
      // "before" state before the transition has something to animate from.
      // One rAF alone can still land in the same paint the element was
      // created in on some browsers; two is the reliable version of this
      // trick.
      var prevHw = prevSlide && prevSlide.play ? homeWpOf(prevSlide.play) : null;
      var startHome = prevHw == null ? null : Math.round(prevHw * 100);
      var startAway = startHome == null ? null : 100 - startHome;
      // Same dwell this play is actually on screen for (scheduleLivePane
      // times the pane's own next advance off the identical call) - Alex's
      // ask was to have the bar settle around when the play itself finishes,
      // not on a fixed clock of its own.
      var wp = liveGridWpBarHtml(slide.play, slide.homeAbbr, slide.awayAbbr, startAway, startHome, slideDwell(slide));
      if (wp) {
        scorebug.insertAdjacentHTML("afterend", wp.html);
        if (startHome != null) {
          var wpEl = scorebug.nextElementSibling;
          var seg = wpEl.querySelector(".live-grid-wp-seg");
          window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
              if (seg) seg.style.transform = "scaleX(" + (wp.awayPct / 100) + ")";
            });
          });
        }
      }
    }
    // Matchup moves up under each team's own logo (Alex's ask) rather than
    // sitting in its own row below the field - whichever side is batting
    // gets the batter, the other gets the pitcher, so it flips with
    // batting_is_home exactly like the scorebug's own "batting" highlight
    // already does.
    var m = slide.play;
    var awayEl = pane.el.querySelector(".scene-scorebug-team:not(.home)");
    var homeEl = pane.el.querySelector(".scene-scorebug-team.home");
    if (awayEl && homeEl && m) {
      liveGridWrapTeamRow(awayEl);
      liveGridWrapTeamRow(homeEl);
      var awayBatting = !m.batting_is_home;
      awayEl.insertAdjacentHTML("beforeend", awayBatting
        ? liveGridMatchupLineHtml("AB", m.batter_name)
        : liveGridMatchupLineHtml("P", m.pitcher_name));
      homeEl.insertAdjacentHTML("beforeend", awayBatting
        ? liveGridMatchupLineHtml("P", m.pitcher_name)
        : liveGridMatchupLineHtml("AB", m.batter_name));
    }

    // Player-scores follows the fielding description on the same line
    // (Alex's ask) rather than its own row below it - sceneDefenseLineHtml
    // and scoringLine are two separate sibling divs in playSceneHtml with no
    // shared wrapper, and both are flex items of .play-scene, which forces
    // each onto its own row regardless of their own display value - so this
    // folds scoringLine's text into the end of the defense line's own
    // element instead, and scoring-line's row is hidden in CSS.
    var defEl = pane.el.querySelector(".scene-defense");
    var scoreEl = pane.el.querySelector(".scoring-line");
    if (defEl && scoreEl && scoreEl.textContent.trim()) {
      var sep = defEl.textContent.trim() ? " · " : "";
      defEl.insertAdjacentHTML("beforeend",
        sep + '<span class="live-grid-score-inline">' + escapeHtml(scoreEl.textContent) + "</span>");
    }
  }

  /* Mounts one step of a pane's "new data" intro (see refreshLiveGridData,
     which builds pane.introSteps) - either a cut card or a one-off slide
     (the new play at normal speed, then the same play again in slow motion).
     --play-speed is normally only ever set on the root (applyPlaybackSpeedVar
     - one global value every slideshow reads), but a CSS custom property set
     on a descendant overrides what it inherited for that subtree only - so
     overriding it on just this pane's own element is what makes ONE pane's
     replay run at half speed while every other pane (and every other
     slideshow) keeps running at the shared global speed. Cleared again once
     a step doesn't need it, so it never leaks into that pane's own normal
     cycling afterward. */
  function mountLiveGridIntroStep(pane, step) {
    // LIVE pill instead of the key icon for the whole intro, not just the
    // cut cards (Alex's ask) - mountLiveSlideInto's own key-icon toggle
    // still runs underneath this for the "slide" steps, but every intro
    // slide's pass is "other" (never "key"), so it always resolves to
    // hidden there anyway; this is the one place turning the pill on.
    if (pane.liveIconEl) pane.liveIconEl.hidden = false;
    if (step.kind === "cut") {
      pane.el.style.removeProperty("--play-speed");
      mountLiveGridCutSlide(pane, step.text, step.pulsing);
      return;
    }
    if (step.speedMult === 1) {
      pane.el.style.removeProperty("--play-speed");
    } else {
      pane.el.style.setProperty("--play-speed", String(getPlaybackSpeed() * step.speedMult));
    }
    mountLiveSlideInto(pane, step.slide, true);
  }

  /* What happens when a pane's timer fires. Normal cycling just steps to the
     next slide - but a pane with pane.introSteps set (refreshLiveGridData/
     liveGridActivateNextIntro) works through that queue instead, one step
     per tick, before swapping in the refreshed sequence, resuming normal
     cycling from the top (key moments again - Alex's spec), and handing off
     to whichever pane is next in liveGrid.introQueue (Alex's ask: only one
     pane's intro plays at a time, in scoreboard order, not all of them at
     once if a refresh finds several games with new plays). */
  function advanceLivePane(pane) {
    if (pane.introSteps) {
      pane.introIndex++;
      if (pane.introIndex >= pane.introSteps.length) {
        pane.el.style.removeProperty("--play-speed");
        pane.sequence = pane.introSequence;
        pane.index = 0;
        pane.introSteps = null;
        pane.introSequence = null;
        pane.prev = null;
        if (pane.liveIconEl) pane.liveIconEl.hidden = true;
        if (liveGrid.introActivePane === pane) liveGrid.introActivePane = null;
        mountLivePane(pane);
        scheduleLivePane(pane);
        liveGridActivateNextIntro();
        return;
      }
      mountLiveGridIntroStep(pane, pane.introSteps[pane.introIndex]);
      scheduleLivePane(pane);
      return;
    }
    pane.index = (pane.index + 1) % pane.sequence.length;
    mountLivePane(pane);
    scheduleLivePane(pane);
  }

  /* Each pane runs on its own clock - a groundout in one game must not yank
     a still-unfolding double play in another over to its next play early.
     Reuses slideDwell exactly as the single-game replay does (real animation
     length plus reading time, scaled by the same speed setting every
     slideshow here shares), just scheduled per pane instead of once for a
     single active slide. That's the whole "global timing stays global" call -
     one shared speed multiplier, not one shared clock tick. */
  function scheduleLivePane(pane) {
    clearLivePaneTimer(pane);
    var inIntro = !!pane.introSteps;
    // Still schedules through a 1-slide (or empty) sequence while an intro
    // is in flight - those need their own tick to resolve even though
    // there's nothing to "advance" to yet in the normal sense.
    if (pane.sequence.length < 2 && !inIntro) return;
    var dwellMs;
    if (inIntro) {
      // introIndex still -1 means nothing's mounted yet (refreshLiveGridData
      // just queued this pane and kicked its timer) - fire almost
      // immediately so advanceLivePane can mount step 0 rather than reading
      // a step that doesn't exist yet.
      var cur = pane.introIndex >= 0 ? pane.introSteps[pane.introIndex] : null;
      dwellMs = cur ? cur.dwell : 0;
    } else {
      dwellMs = pane.sequence[pane.index] ? slideDwell(pane.sequence[pane.index]) : 0;
    }
    pane.timer = window.setTimeout(function () {
      pane.timer = null;
      advanceLivePane(pane);
    }, dwellMs);
  }

  function clearLivePaneTimer(pane) {
    if (pane.timer) { window.clearTimeout(pane.timer); pane.timer = null; }
  }

  /* landscape (the common case, and the only one this was originally tuned
     for): n<=2 is one row (a 2-game session reads better side-by-side than
     stacked); n>=3 caps at two rows, columns wide enough to fit - reproduces
     Alex's 4->2x2 and 8->2x4 examples exactly, and reflows a mid-count
     playoff session (5, 6, 7 games) the same way rather than needing its
     own special case.
     portrait flips which dimension gets capped at 2, so a tall-narrow
     viewport gets a tall-narrow grid instead of the same wide-short shape
     squeezed into it - the min-width:900px gate is about available WIDTH,
     not orientation, so something like an iPad Pro held in portrait
     (1024px wide) already clears it today and got a landscape-shaped grid
     regardless (Alex's report). */
  function liveGridLayout(n) {
    var portrait = window.innerHeight > window.innerWidth;
    var capped = n <= 2 ? 1 : 2;
    return portrait
      ? { rows: Math.ceil(n / capped), cols: capped }
      : { rows: capped, cols: Math.ceil(n / capped) };
  }

  function applyLiveGridLayout() {
    if (!liveGrid.open || !liveGrid.panes.length) return;
    var layout = liveGridLayout(liveGrid.panes.length);
    var grid = $("live-grid");
    grid.style.gridTemplateColumns = "repeat(" + layout.cols + ", 1fr)";
    grid.style.gridTemplateRows = "repeat(" + layout.rows + ", 1fr)";
  }

  // Same debounce-on-resize pattern as the scoreboard row's own
  // scheduleScoreboardResize - a rotation firing several resize events in
  // quick succession should only reflow the grid once.
  var liveGridResizeTimer;
  function scheduleLiveGridResize() {
    window.clearTimeout(liveGridResizeTimer);
    liveGridResizeTimer = window.setTimeout(applyLiveGridLayout, 150);
  }

  function openLiveGrid() {
    var games = filters.session === null ? [] : ((data.meta.games || {})[String(filters.session)] || []);
    if (!games.length) { toast("No games in this session yet."); return; }
    var session = filters.session;
    // One game (Spotlight) is a real 1-pane grid now, not a fallback to the
    // plain single-game replay (Alex's ask) - it needs the exact same
    // machinery as any other pane to get the Live Look-In intro/background
    // refresh treatment, and liveGridLayout(1) already resolves to a single
    // full-size pane (rows=1,cols=1 either orientation) with no special
    // case needed there.

    var btn = $("live-grid-btn");
    btn.classList.add("loading");
    ensureSessionPlaysLoaded(session).then(function () {
      btn.classList.remove("loading");
      var layout = liveGridLayout(games.length);
      var grid = $("live-grid");
      grid.style.gridTemplateColumns = "repeat(" + layout.cols + ", 1fr)";
      grid.style.gridTemplateRows = "repeat(" + layout.rows + ", 1fr)";
      grid.innerHTML = games.map(liveGridPaneHtml).join("");
      var paneEls = grid.querySelectorAll(".live-grid-pane");
      liveGrid.panes = games.map(function (g, i) {
        var plays = gamePlaysFor(session, g.game_code);
        var built = buildLiveGridSequence(g, plays);
        return {
          gameCode: g.game_code, game: g,
          el: paneEls[i].querySelector('[data-role="slide"]'),
          keyIconEl: paneEls[i].querySelector('[data-role="key-icon"]'),
          liveIconEl: paneEls[i].querySelector('[data-role="live-pill"]'),
          sequence: built.sequence,
          index: 0, prev: null, timer: null,
          introSteps: null, introIndex: -1, introSequence: null,
        };
      });
      liveGrid.panes.forEach(function (pane) {
        mountLivePane(pane);
        scheduleLivePane(pane);
      });
      liveGrid.open = true;
      liveGrid.session = session;
      $("live-grid-modal").hidden = false;
      // Only the active season's own files are ever fetched cache-busted
      // (fetchSeasonJSON/getJSON vs getJSONCached) - a historical season's
      // plays file is immutable, so polling it would just be wasted fetches.
      if (season.active === season.current) {
        clearLiveGridRefreshTimer();
        liveGrid.refreshTimer = window.setInterval(refreshLiveGridData, LIVE_GRID_REFRESH_MS);
      }
    }).catch(function () {
      btn.classList.remove("loading");
      toast("Could not load this session's plays.");
    });
  }

  function closeLiveGrid() {
    liveGrid.panes.forEach(clearLivePaneTimer);
    clearLiveGridRefreshTimer();
    hideLiveGridOverlay();
    liveGrid.open = false;
    liveGrid.panes = [];
    liveGrid.introQueue = [];
    liveGrid.introActivePane = null;
    $("live-grid-modal").hidden = true;
    $("live-grid").innerHTML = "";
  }

  var LIVE_GRID_REFRESH_MS = 5 * 60 * 1000;   // matches the cron's own cadence

  function clearLiveGridRefreshTimer() {
    if (liveGrid.refreshTimer) { window.clearInterval(liveGrid.refreshTimer); liveGrid.refreshTimer = null; }
  }

  function showLiveGridOverlay() {
    var el = $("live-grid-overlay");
    if (!el) return;
    el.hidden = false;
    void el.offsetWidth;
    el.classList.add("in");
  }

  function hideLiveGridOverlay() {
    var el = $("live-grid-overlay");
    if (!el) return;
    el.classList.remove("in");
    el.hidden = true;
  }

  /* Live Look-In (long, pulsing) -> the new play at normal speed -> Replay
     (short flash) -> the same play again at half speed -> Now Batting if
     there is one - then advanceLivePane swaps in built.sequence and resumes
     normal cycling from the top (Alex's spec). Split out of
     refreshLiveGridData so liveGridActivateNextIntro can build a queued
     pane's steps at the moment its turn actually comes up, not upfront when
     the refresh first found it. */
  function liveGridBuildIntroSteps(built) {
    var newestSlide = built.sequence[built.resumeIndex];
    // The on-deck slide always immediately follows "most recent" on a live
    // sequence (buildLiveGridSequence pushes it right after mostRecent) -
    // nothing to check on a sequence that just went final, there's no
    // on-deck to find.
    var afterNewest = built.sequence[built.resumeIndex + 1];
    var onDeckSlide = (afterNewest && afterNewest.play && afterNewest.play.is_on_deck) ? afterNewest : null;
    var normalDwell = slideDwell(newestSlide);
    var steps = [
      { kind: "cut", text: "Live Look-In", pulsing: true, dwell: LIVE_GRID_PANE_LOOKIN_MS },
      { kind: "slide", slide: newestSlide, speedMult: 1, dwell: normalDwell },
      { kind: "cut", text: "Replay", pulsing: false, dwell: LIVE_GRID_REPLAY_CUT_DWELL_MS },
      { kind: "slide", slide: newestSlide, speedMult: 0.5, dwell: normalDwell * 2 },
    ];
    if (onDeckSlide) {
      steps.push({ kind: "slide", slide: onDeckSlide, speedMult: 1, dwell: slideDwell(onDeckSlide) });
    }
    return steps;
  }

  /* Pops the next {pane, built} off liveGrid.introQueue and starts its
     intro - the only place that ever sets pane.introSteps, so at most one
     pane is ever mid-intro at once (Alex's ask: stagger multiple games'
     look-ins rather than run them all at once, in the order they were
     queued - see refreshLiveGridData). Called once the modal-wide overlay's
     10s is up, and again every time a pane's own intro finishes
     (advanceLivePane's "intro complete" branch), which is what actually
     drains the queue one pane at a time. */
  function liveGridActivateNextIntro() {
    if (!liveGrid.open || !liveGrid.introQueue.length) {
      liveGrid.introActivePane = null;
      return;
    }
    var item = liveGrid.introQueue.shift();
    var pane = item.pane;
    liveGrid.introActivePane = pane;
    pane.introSteps = liveGridBuildIntroSteps(item.built);
    pane.introIndex = -1;
    pane.introSequence = item.built.sequence;
    // Let an already-running timer fire on its own schedule (so a play
    // mid-animation isn't cut short) - only kick a pane that had nothing
    // scheduled at all (an empty "No plays yet" pane, the one case
    // scheduleLivePane declines to start a timer for on its own).
    if (!pane.timer) scheduleLivePane(pane);
  }

  /* Polls for new plays ONLY while the grid is open (Alex's ask), and is
     deliberately its own narrow path rather than reloadData()/requestRefresh
     - those call computeCatchUp(), which reads Catch Me Up's cursor, marks
     it seen up to now, and only then computes what's "new". Running that
     here would silently clear the backlog for anyone who's had the page open
     in a background tab, which is exactly the case Alex wants preserved -
     Catch Me Up should still find everything once they come back and open it
     themselves. So this only ever touches data.playsBySession for the one
     session in view and each pane's own sequence - nothing Catch Me Up or
     any other feature reads.

     Panes don't reorder by leverage on refresh (Alex's call) - this doesn't
     even refetch meta.json, so there's no fresh leverage number to reorder
     by, and a pane jumping to a new grid slot while someone's watching it
     would be its own can of worms regardless. */
  function refreshLiveGridData() {
    if (!liveGrid.open) return;
    var session = liveGrid.session;
    if (session == null) return;
    var oldRows = data.playsBySession[session] || [];
    fetchSeasonJSON("plays_" + pad2(session) + ".json").then(function (freshRows) {
      if (!liveGrid.open || liveGrid.session !== session) return;   // closed/changed mid-fetch
      data.playsBySession[session] = freshRows;
      var newlyAffected = [];
      liveGrid.panes.forEach(function (pane) {
        var oldReal = oldRows.filter(function (p) {
          return p.game_code === pane.gameCode && !p.is_on_deck;
        }).length;
        var newForGame = gamePlaysFor(session, pane.gameCode);
        var newReal = newForGame.filter(function (p) { return !p.is_on_deck; }).length;
        if (newReal <= oldReal) return;   // nothing new for this game this cycle
        newlyAffected.push({ pane: pane, built: buildLiveGridSequence(pane.game, newForGame) });
      });
      if (!newlyAffected.length) return;
      // Nothing already in flight - this is a fresh batch, so it gets its
      // own 10s modal-wide announcement before the queue starts draining.
      // A refresh landing mid-stagger (introQueue still has items, or a
      // pane is actively mid-intro) just appends instead - the overlay
      // already ran for this "session" of new arrivals.
      var alreadyRunning = liveGrid.introActivePane || liveGrid.introQueue.length;
      liveGrid.introQueue = liveGrid.introQueue.concat(newlyAffected);
      if (!alreadyRunning) {
        showLiveGridOverlay();
        window.setTimeout(function () {
          hideLiveGridOverlay();
          liveGridActivateNextIntro();
        }, LIVE_GRID_OVERLAY_MS);
      }
    }).catch(function () {});   // offline/stumble this cycle - try again next interval
  }

  // Exposed for manual testing only (same convention as
  // window.KMFlight.filteredPlaysOrdered above) - there's no local cron
  // appending real plays, so from DevTools: edit docs/data/plays_XX.json to
  // add a play (bump play_num past the current max for a game already in
  // the open session, keep is_on_deck off it), then call
  // KMLiveGrid.refreshNow() to check for it immediately instead of waiting
  // out the real 5-minute interval.
  window.KMLiveGrid = { refreshNow: refreshLiveGridData };

  function wireLiveGrid() {
    var modal = $("live-grid-modal");
    if (!modal) return;
    $("live-grid-btn").addEventListener("click", openLiveGrid);
    $("live-grid-close").addEventListener("click", closeLiveGrid);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeLiveGrid(); });
    document.addEventListener("keydown", function (e) {
      if (modal.hidden) return;
      if (e.key === "Escape") closeLiveGrid();
    });
    // orientationchange fires more reliably than resize around an actual
    // rotation on some mobile/tablet browsers - both funnel through the
    // same debounced reflow, and applyLiveGridLayout itself no-ops whenever
    // the grid isn't open, so this is harmless the rest of the time.
    window.addEventListener("resize", scheduleLiveGridResize);
    window.addEventListener("orientationchange", scheduleLiveGridResize);
  }

  function wireReplay() {
    var modal = $("replay-modal");
    if (!modal) return;
    wireFullscreenToggle("replay-modal", "replay-fullscreen");
    wireSpeedToggle("replay-speed");
    wireLoopToggle("replay-loop");
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

  // One merged control for both season and session (Alex's ask - two
  // selects side by side didn't save meaningful width over this, since the
  // combined label repeats the season prefix on every option, but a single
  // control is still one fewer border/padding/gap in the tight header row).
  // Each option's value is "season|session" ("season|" for that season's
  // "All" entry) so one change event carries both. Grouped by
  // season via <optgroup> - archive seasons plus the live one, newest
  // first - built from archiveSeasonsMeta/liveSeasonSessions rather than
  // the active season's own data.meta, so every season's entries are always
  // present regardless of which one is currently active.
  function seasonSessionGroups() {
    return archiveSeasonsMeta
      .concat([{ season: season.current, sessions: liveSeasonSessions }])
      .sort(function (a, b) { return b.season - a.season; });
  }

  // keepSelection: re-select whatever (season.active, filters.session) is
  // right now - used on plain re-renders (e.g. reloadData) where neither
  // has changed. forceSession (may be null for "Full season", or undefined
  // to fall back to the active season's latest session) - used right after
  // a season switch, to land on a specific session instead of always
  // defaulting to the newest.
  function populateSessionSelect(keepSelection, forceSession) {
    var sel = $("session-select");
    var groups = seasonSessionGroups();
    // No archive seasons committed yet: behaves exactly like the old plain
    // session select (no "S13 ·" prefix, no optgroup) - the merged picker
    // only earns its keep once there's more than one season to pick from.
    if (groups.length <= 1) {
      var onlySessions = (groups[0] && groups[0].sessions || []).slice().sort(function (a, b) { return b - a; });
      sel.innerHTML = ['<option value="' + season.current + '|">All</option>'].concat(
        onlySessions.map(function (s) {
          return '<option value="' + season.current + '|' + s + '">Session ' + parseInt(s, 10) + "</option>";
        })
      ).join("");
    } else {
      sel.innerHTML = groups.map(function (g) {
        var label = "S" + g.season;
        var sessions = (g.sessions || []).slice().sort(function (a, b) { return b - a; });
        var opts = ['<option value="' + g.season + '|">' + label + " · All</option>"].concat(
          sessions.map(function (s) {
            return '<option value="' + g.season + '|' + s + '">' + label + " · Session " + parseInt(s, 10) + "</option>";
          })
        );
        return '<optgroup label="' + label + '">' + opts.join("") + "</optgroup>";
      }).join("");
    }

    var activeGroup = groups.filter(function (g) { return g.season === season.active; })[0];
    var activeSessions = (activeGroup && activeGroup.sessions) || [];

    var target;
    if (forceSession !== undefined) {
      target = forceSession;
    } else if (keepSelection && (filters.session === null || activeSessions.indexOf(filters.session) !== -1)) {
      target = filters.session;
    } else {
      target = activeSessions.length ? activeSessions[0] : null;
    }
    filters.session = target;
    sel.value = season.active + "|" + (target === null ? "" : target);
  }

  // Everything reloadData()/boot() already re-render after a fresh data
  // load, replayed here after a season switch. Filter state that can't
  // survive the switch is reset: selectedGame and session (recomputed by
  // populateSessionSelect below) always; team, since abbreviations differ
  // across seasons. playerId is a global human id (Part 0 finding 9) and
  // survives untouched.
  function activateSeasonData(targetSession) {
    rookieIds = null; // rebuild from the newly-active season's roster
    filters.selectedGame = null;
    filters.team = "";
    deselectScoreboardTile();
    var historical = season.active !== season.current;
    // No "Season N archive" label (Alex's ask) - the season+session picker
    // already shows which season is selected, so this would just repeat it.
    // A finished season has no live build timestamp to show in its place.
    $("built-at").textContent = historical ? "" : formatBuiltAt(data.meta.built_at);
    populateSessionSelect(false, targetSession);
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
    if (window.KMFavorites && window.KMFavorites.setPlayers) {
      window.KMFavorites.setPlayers(data.players);
      window.KMFavorites.refreshList();
    }
    // Live-only features: refresh (a finished season never has anything new
    // to fetch) and Catch Me Up (meaningless for a season that's over, and
    // its cursor must not advance just from browsing history).
    var refreshBtn = $("refresh-btn");
    var refreshRow = refreshBtn && refreshBtn.closest(".settings-row");
    if (refreshRow) refreshRow.hidden = historical;
    if (historical) {
      var status = $("refresh-status");
      if (status) status.textContent = "";
      window.clearTimeout(catchUpCaughtUpTimer); // don't let a stale "caught up" fade fire while browsing history
      var banner = $("catchup-banner");
      if (banner) banner.hidden = true;
    } else {
      renderCatchUpBanner(); // shows the loading spinner while data.catchUpGroups is still null
      computeCatchUp().then(function (groups) {
        data.catchUpGroups = groups;
        renderCatchUpBanner();
      });
    }
  }

  // Snapshot/restore data's contents wholesale (Part 2: keep the existing
  // global `data` object as "the active season's data" rather than
  // threading a season key through every data.meta reference).
  function snapshotSeasonData() {
    return {
      moments: data.moments, players: data.players, meta: data.meta,
      playsBySession: data.playsBySession, catchUpGroups: data.catchUpGroups,
    };
  }

  function applySeasonData(snap) {
    data.moments = snap.moments;
    data.players = snap.players;
    data.meta = snap.meta;
    data.playsBySession = snap.playsBySession;
    data.catchUpGroups = snap.catchUpGroups;
  }

  // Switch the active season, fetching (or reusing from cache) that
  // season's key_moments/players/meta. Returns a Promise. The live season
  // is never served from cache - it keeps changing under a long-running
  // tab, so returning to it always re-fetches fresh (bust() + no-store,
  // same as boot/reloadData).
  function setActiveSeason(n, targetSession) {
    if (n === season.active) return Promise.resolve();
    season.cache[season.active] = snapshotSeasonData();

    if (n === season.current) {
      return Promise.all([
        getJSON("data/key_moments.json"),
        getJSON("data/players.json"),
        getJSON("data/meta.json"),
      ]).then(function (res) {
        applySeasonData({ moments: res[0], players: res[1], meta: res[2], playsBySession: {}, catchUpGroups: null });
        season.active = n;
        activateSeasonData(targetSession);
      });
    }

    if (season.cache[n]) {
      applySeasonData(season.cache[n]);
      season.active = n;
      activateSeasonData(targetSession);
      return Promise.resolve();
    }

    var dir = "data/s" + pad2(n) + "/";
    return Promise.all([
      getJSONCached(dir + "key_moments.json"),
      getJSONCached(dir + "players.json"),
      getJSONCached(dir + "meta.json"),
    ]).then(function (res) {
      applySeasonData({ moments: res[0], players: res[1], meta: res[2], playsBySession: {}, catchUpGroups: null });
      season.active = n;
      activateSeasonData(targetSession);
    });
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

    // One merged season+session control - value is "season|session" (empty
    // session part = "All"). Switching within the active season is a plain
    // filter change; switching season goes through setActiveSeason, landing
    // directly on the picked session via its targetSession param instead of
    // defaulting to the season's latest and re-rendering twice.
    $("session-select").addEventListener("change", function (e) {
      var sessSel = e.target;
      var parts = sessSel.value.split("|");
      var n = Number(parts[0]);
      var sess = parts[1] === "" ? null : Number(parts[1]);
      if (n === season.active) {
        filters.session = sess;
        deselectScoreboardTile();
        renderScoreboard();
        renderMaybeLoading();
        return;
      }
      sessSel.disabled = true;
      setActiveSeason(n, sess).then(function () {
        sessSel.disabled = false;
      }).catch(function (err) {
        // Was silently swallowed - a season fails here just as easily from
        // a rendering exception AFTER the files fetch fine (activateSeasonData/
        // render, both of which run inside setActiveSeason's own .then) as
        // from an actual network failure, and only logging it gives any way
        // to tell those apart (Alex's report: "nothing in console pops up").
        console.error("setActiveSeason failed:", err);
        sessSel.disabled = false;
        populateSessionSelect(true);
        toast("Could not load that season.");
      });
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

    // Alex's ask: an X inside each search box (player/result) to clear just
    // that one - shown only once there's actually something to clear, so it
    // doesn't just sit there empty-handed on a fresh page. Called after
    // every place either input's own .value gets set, typed or programmatic
    // alike, so it never drifts out of sync with what's actually in the box.
    function syncClearBtn(inputId, btnId) {
      $(btnId).hidden = !$(inputId).value;
    }

    var playerTimer;
    $("player-input").addEventListener("input", function (e) {
      var v = e.target.value;
      filters.playerId = null;   // typing again invalidates a previous exact pick
      renderPlayerSuggest(v);
      syncClearBtn("player-input", "player-input-clear");
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
      syncClearBtn("player-input", "player-input-clear");
      window.clearTimeout(playerTimer);
      render();
    });

    // mousedown (not click), same reasoning as player-suggest's own pick
    // handler above - fires before the input's blur would otherwise close
    // this out from under it.
    $("player-input-clear").addEventListener("mousedown", function (e) {
      e.preventDefault();
      $("player-input").value = "";
      $("player-input").focus();
      filters.player = "";
      filters.playerId = null;
      $("player-suggest").hidden = true;
      syncClearBtn("player-input", "player-input-clear");
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
      syncClearBtn("result-code-input", "result-code-input-clear");
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
      syncClearBtn("result-code-input", "result-code-input-clear");
      render();
    });

    $("result-code-input-clear").addEventListener("mousedown", function (e) {
      e.preventDefault();
      $("result-code-input").value = "";
      $("result-code-input").focus();
      filters.resultCode = "";
      $("result-code-suggest").hidden = true;
      syncClearBtn("result-code-input", "result-code-input-clear");
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
      syncClearBtn("player-input", "player-input-clear");
      syncClearBtn("result-code-input", "result-code-input-clear");
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
    wireLiveGrid();
    wireSettings();
    wireMethodology();
    syncSpeedButtons();
    syncLoopButtons();
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

  // (i) button - reachable from two places, same simple open/close pattern
  // as Settings/Favorites: the replay modal's own control bar (Deliberately
  // doesn't pause/close the replay behind it - Alex's spec: an overlay on
  // top, not a mode switch, the slideshow keeps advancing underneath) and,
  // since Settings is reachable any time rather than only mid-replay,
  // Settings' own bottom row too (Alex's ask).
  function wireMethodology() {
    var modal = $("methodology-modal");
    if (!modal) return;
    // Originally nested inside #replay-modal (not a top-level sibling like
    // every other modal) specifically so it stays visible when the replay
    // is fullscreened - the Fullscreen API only renders the fullscreened
    // element's own subtree, so anything outside it is invisible no matter
    // its own hidden state or z-index. That same nesting is exactly why
    // opening it from Settings did nothing visible (Alex's report):
    // #replay-modal itself carries `hidden` whenever no replay is open, and
    // `hidden`/display:none on an ancestor hides every descendant
    // regardless of the descendant's own hidden attribute - toggling
    // methodology-modal.hidden had no effect while its parent was display:
    // none. Fix: reparent to wherever is actually correct for the CURRENT
    // context right before each open - back inside #replay-modal (its
    // original spot, captured here before any reparenting happens) only
    // while a replay is genuinely fullscreened, document.body (a normal
    // top-level modal, same as Settings/Favorites) otherwise - which is
    // every open from Settings, since #replay-modal is always hidden there.
    var replayHome = modal.parentNode;
    function closeMethodology() { modal.hidden = true; }
    // Four wheel-example spots, each filled by its own generator - keyed by
    // element id so adding/removing a spot later is a one-line change here.
    var WHEEL_SECTIONS = {
      "methodology-wheels-contact": methodologyContactWheelsHtml,
      "methodology-wheels-topped": methodologyToppedWheelsHtml,
      "methodology-wheels-spray": methodologySprayWheelsHtml,
      "methodology-wheels-combined": methodologyCombinedWheelsHtml,
    };
    function openMethodology() {
      var replayModal = $("replay-modal");
      var replayIsFullscreen = !!replayModal &&
        (document.fullscreenElement === replayModal || document.webkitFullscreenElement === replayModal);
      var targetParent = replayIsFullscreen ? replayHome : document.body;
      if (modal.parentNode !== targetParent) targetParent.appendChild(modal);
      // Rendered lazily on first open, not at boot - by the time either
      // trigger button is reachable at all, data.meta.flight is guaranteed
      // loaded (the replay one only shows mid-replay; the Settings one only
      // shows once the app has booted). childNodes check per section makes
      // this a one-time fill, not a re-render on every open.
      Object.keys(WHEEL_SECTIONS).forEach(function (id) {
        var el = $(id);
        if (el && !el.childNodes.length) el.innerHTML = WHEEL_SECTIONS[id]();
      });
      modal.hidden = false;
    }
    var replayBtn = $("replay-info");
    if (replayBtn) replayBtn.addEventListener("click", openMethodology);
    var settingsBtn = $("settings-info");
    if (settingsBtn) {
      settingsBtn.addEventListener("click", function () {
        // Same "close the panel underneath before opening this one" courtesy
        // wireSettings' own favBtn handler does for Favorites - avoids two
        // modals stacked on top of each other.
        var settingsModal = $("settings-modal");
        if (settingsModal) settingsModal.hidden = true;
        openMethodology();
      });
    }
    $("methodology-close").addEventListener("click", closeMethodology);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeMethodology(); });
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
      archiveSeasonsMeta = (data.meta.archive_seasons || []).slice();
      liveSeasonSessions = (data.meta.sessions || []).slice();
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

  // ── deep links: ?game=130419&play=34 ────────────────────────────────────────
  //
  // Read-only input, parsed once at boot - no pushState/history management in
  // v1 (plan Stage 4 item 4), so closing the replay modal just leaves the
  // param sitting in the address bar, harmless.

  function parseDeepLinkGame() {
    var params = new URLSearchParams(location.search);
    var raw = params.get("game");
    if (!raw) return null;
    var digits = raw.replace(/\D/g, "");
    if (!digits) {
      toast("That link's game code isn't valid.");
      return null;
    }
    var code = digits.padStart(6, "0");
    var gameSeason = parseInt(code.slice(0, 2), 10);
    var session = parseInt(code.slice(2, 4), 10);
    var playRaw = params.get("play");
    var play = playRaw ? parseInt(playRaw.replace(/\D/g, ""), 10) : null;
    if (play != null && isNaN(play)) play = null;
    return { code: code, season: gameSeason, session: session, play: play };
  }

  // Runs after boot's own load has rendered the normal page underneath -
  // resolving the season (switching to it first if it's a committed archive
  // season, toasting if it's neither the live season nor archived) is exactly
  // the flow setActiveSeason + openReplayAtPlay already support for the
  // season selector, reused here rather than duplicated.
  function handleDeepLink() {
    var link = parseDeepLinkGame();
    if (!link) return;
    var playNum = link.play ? Number(link.code) * 1000 + link.play : null;
    function open() {
      openReplayAtPlay(link.code, link.session, playNum, null);
    }
    if (link.season === season.current) {
      open();
    } else if (archiveSeasonsMeta.some(function (s) { return s.season === link.season; })) {
      setActiveSeason(link.season).then(open).catch(function () {
        toast("Could not load that game's season.");
      });
    } else {
      toast("That game's season isn't available.");
    }
  }

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
      season.current = data.meta.season;
      season.active = season.current;
      archiveSeasonsMeta = (data.meta.archive_seasons || []).slice();
      liveSeasonSessions = (data.meta.sessions || []).slice();
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
      }, function () {
        // onNameReady (favorites.js): fires once a name is known, whether
        // from localStorage at boot or typed fresh mid-session - so the
        // "add your name" quiet prompt clears and the real count/reel
        // populate right away instead of needing a page reload (Alex's
        // report - it used to only ever recompute here at boot).
        renderCatchUpBanner();
        computeCatchUp().then(function (groups) {
          data.catchUpGroups = groups;
          renderCatchUpBanner();
        });
      }).then(function () {
        // First paint either way - if a name was already known, onNameReady
        // above already ran (load() resolves before init's own returned
        // promise does), so this is just a harmless repaint of whatever's
        // already current; if not, this is the only place the quiet
        // "add your name" prompt gets its first render.
        renderCatchUpBanner();
      });
      handleDeepLink();
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

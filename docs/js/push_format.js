/* Custom notification layouts: the JS half of tools/push_format.py (same text, see tools/push_format_parity_test.py). Change both together.
 * Used by push.js for the layout editor's live preview; the real alerts are rendered by the sender in Python.
 *
 * A layout is { l: [[component, ...], ...], bs, nb, np, wm, pv } - the options are described in push_format.py. */
(function () {
  "use strict";
  var COMPONENTS = [
    ["score", "Score"], ["inning", "Inning"], ["batter", "Batter"], ["batter_last", "Batter (last name)"], ["result", "Result (1B)"], ["result_full", "Result (Single)"],
    ["pitcher", "vs Pitcher"], ["pitcher_last", "vs Pitcher (last name)"], ["scoring", "Who scored"],
    ["bases", "Bases ◇ ◆ ◇"], ["bases_words", "Bases (words)"], ["outs", "Outs"], ["wpa", "WPA"], ["lev", "Leverage"],
    ["wp", "Win prob"], ["wp_change", "Win prob (before → after)"], ["diff", "Pitch / swing / diff"], ["tags", "Key moment tags"]
  ];
  var MAX_LINES = 5;
  // the one option left: whose side WPA and win probability are from. Bases, names and the win probability wording are components now
  // (bases / bases_words, batter / batter_last, pitcher / pitcher_last, wp / wp_change); the old option values are turned into them by upgrade()
  var DEFAULTS = { pv: "hit" };
  var CHOICES = { pv: ["hit", "feat"] };
  var OPTION_KEYS = ["pv"];
  var SWAPS = [["bs", "words", "bases", "bases_words"], ["nb", "last", "batter", "batter_last"], ["np", "last", "pitcher", "pitcher_last"], ["wm", "change", "wp", "wp_change"]];
  // most notable first; with one tag shown (the default) a play with several gets the first one found here
  var TAG_ORDER = ["walk_off", "game_winning", "triple_play", "zero_diff", "five_hundred_diff", "lead_change", "bases_loaded", "double_play", "steal", "high_leverage", "wpa"];
  var TAG_TEXT = {
    walk_off: "WALK-OFF", game_winning: "GAME-WINNING", triple_play: "TRIPLE PLAY",
    zero_diff: "0 DIFF", five_hundred_diff: "500 DIFF", lead_change: "LEAD CHANGE",
    bases_loaded: "BASES LOADED", double_play: "DOUBLE PLAY", steal: "STEAL",
    high_leverage: "HIGH LEVERAGE", wpa: "BIG WPA"
  };
  var CLASSIC = { l: [["score", "inning"], ["batter"], ["pitcher"], ["scoring"], ["result", "bases", "outs"]] };
  var SPOTLIGHT = { l: [["tags"], ["score", "inning"], ["batter_last", "pitcher_last"], ["scoring"], ["result_full", "bases", "outs"]] };   // the default for key moments (tools/push_format.py SPOTLIGHT)

  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  // an older save: see upgrade() in push_format.py
  function upgrade(fmt) {
    var out = {};
    Object.keys(fmt).forEach(function (k) { if (k !== "r" && k !== "b") out[k] = fmt[k]; });
    if (has(fmt, "r")) {
      var res = fmt.r === "label" ? "result_full" : "result";
      out.l = (fmt.l || []).map(function (line) {
        var n = [];
        (Array.isArray(line) ? line : []).forEach(function (cid) { if (cid === "result") n.push("batter", res); else n.push(cid); });
        return n;
      });
    }
    if (has(fmt, "b") && !has(out, "bs")) out.bs = fmt.b;
    SWAPS.forEach(function (s) {   // an old option value becomes the matching component
      if (out[s[0]] === s[1]) out.l = (out.l || []).map(function (line) { return Array.isArray(line) ? line.map(function (cid) { return cid === s[2] ? s[3] : cid; }) : line; });
    });
    SWAPS.forEach(function (s) { delete out[s[0]]; });
    return out;
  }
  // every option present and valid, keys in a fixed order (push.js compares these to find the active preset)
  function normalize(fmt) {
    fmt = upgrade(fmt || {});
    var out = { l: (fmt.l || []).map(function (line) { return Array.isArray(line) ? line : []; }).slice(0, MAX_LINES) };
    OPTION_KEYS.forEach(function (k) { out[k] = CHOICES[k].indexOf(fmt[k]) !== -1 ? fmt[k] : DEFAULTS[k]; });
    return out;
  }
  function P(l, opts) { var f = { l: l }; Object.keys(opts || {}).forEach(function (k) { f[k] = opts[k]; }); return normalize(f); }
  var PRESETS = [
    ["Classic", P(CLASSIC.l)],
    ["Spotlight", P(SPOTLIGHT.l)],
    ["Headline", P([["batter_last", "result_full"], ["score", "inning"], ["pitcher_last"], ["scoring"], ["bases", "outs"]])],
    ["Slim", P([["score", "inning", "outs"], ["batter", "result"]])],
    ["Analytics", P([["score", "inning"], ["batter", "pitcher_last"], ["result", "scoring"], ["wp", "wpa", "lev"], ["bases", "outs"]])],
    ["Detailed", P([["score", "inning"], ["batter", "pitcher_last"], ["diff"], ["result", "scoring"], ["bases", "outs"]])]
  ];

  // Sample plays for the preview (the fields the renderer reads, as in plays_NN.json; the win probability fields are the batting team's)
  var SAMPLES = [
    { name: "Single", m: { result: "1B", half: "top", inning: 2, away_team_abbr: "SUN", home_team_abbr: "RLY", away_score: 1, home_score: 0,
      batter_name: "Orb Hayes", batter_last: "Hayes", pitcher_name: "Eldritch Dancer", pitcher_last: "Dancer", scoring_names: [], scoring_full: [], obc_after: "001", outs_after: 0,
      featured_name: "Orb Hayes", featured_last: "Hayes", featured_side: "batting", featured_team_abbr: "SUN", off_team_abbr: "SUN",
      wpa: 0.042, win_prob_before: 0.578, win_prob_after: 0.62, featured_wpa: 0.042, featured_wp_after: 0.62,
      leverage_after: 1.3, pitch: 612, swing: 398, diff: 214, tags: [] } },
    { name: "Go-ahead double", m: { result: "2B", half: "bottom", inning: 5, away_team_abbr: "SUN", home_team_abbr: "RLY", away_score: 3, home_score: 4,
      batter_name: "Copernicus Hel", batter_last: "Hel", pitcher_name: "Eldritch Dancer", pitcher_last: "Dancer", scoring_names: ["Hayes", "Kerrigan"], scoring_full: ["Orb Hayes", "Milo Kerrigan"],
      obc_after: "010", outs_after: 1, featured_name: "Copernicus Hel", featured_last: "Hel", featured_side: "batting", featured_team_abbr: "RLY", off_team_abbr: "RLY",
      wpa: 0.187, win_prob_before: 0.523, win_prob_after: 0.71, featured_wpa: 0.187, featured_wp_after: 0.71,
      leverage_after: 2.6, pitch: 477, swing: 382, diff: 95, tags: ["risp", "run_scored", "lead_change", "high_leverage"] } },
    { name: "Strikeout", m: { result: "K", half: "top", inning: 6, away_team_abbr: "SUN", home_team_abbr: "RLY", away_score: 2, home_score: 3,
      batter_name: "Orb Hayes", batter_last: "Hayes", pitcher_name: "Eldritch Dancer", pitcher_last: "Dancer", scoring_names: [], scoring_full: [], obc_after: "011", outs_after: 3,
      featured_name: "Eldritch Dancer", featured_last: "Dancer", featured_side: "fielding", featured_team_abbr: "RLY", off_team_abbr: "SUN",
      counterpart_name: "Orb Hayes", counterpart_last: "Hayes",
      wpa: -0.064, win_prob_before: 0.41, win_prob_after: 0.346, featured_wpa: 0.064, featured_wp_after: 0.654,
      leverage_after: 0.9, pitch: 301, swing: 118, diff: 183, tags: ["risp"] } },
    { name: "Steal", m: { result: "SB2", half: "top", inning: 5, away_team_abbr: "SUN", home_team_abbr: "RLY", away_score: 2, home_score: 2,
      batter_name: "Glorp Banner", batter_last: "Banner", pitcher_name: "Eldritch Dancer", pitcher_last: "Dancer", scoring_names: [], scoring_full: [], obc_after: "010", outs_after: 2,
      runner_name: "Copernicus Hel", runner_last: "Hel", featured_name: "Copernicus Hel", featured_last: "Hel", featured_side: "batting",
      counterpart_name: "Milo Kerrigan", counterpart_last: "Kerrigan", featured_team_abbr: "SUN", off_team_abbr: "SUN",
      wpa: -0.031, win_prob_before: 0.511, win_prob_after: 0.48, featured_wpa: -0.031, featured_wp_after: 0.48,
      leverage_after: 1.1, pitch: 0, swing: 0, diff: null, tags: ["steal"] } },
    { name: "Walk-off HR", m: { result: "HR", half: "bottom", inning: 6, away_team_abbr: "SUN", home_team_abbr: "RLY", away_score: 3, home_score: 4,
      batter_name: "Orb Hayes", batter_last: "Hayes", pitcher_name: "Eldritch Dancer", pitcher_last: "Dancer", scoring_names: ["Hel"], scoring_full: ["Copernicus Hel"],
      obc_after: "000", outs_after: 2, featured_name: "Orb Hayes", featured_last: "Hayes", featured_side: "batting", featured_team_abbr: "RLY", off_team_abbr: "RLY",
      wpa: 0.512, win_prob_before: 0.488, win_prob_after: 1, featured_wpa: 0.512, featured_wp_after: 1,
      leverage_after: null, pitch: 500, swing: 500, diff: 0, tags: ["run_scored", "walk_off", "game_winning", "zero_diff"] } }
  ];

  function tenths(x) {
    var n = Math.floor(Math.abs(x) * 10 + 0.5);
    return (x < 0 && n ? "-" : "") + Math.floor(n / 10) + "." + (n % 10);
  }
  function signedPct(x) {
    var n = Math.floor(Math.abs(x * 100) * 10 + 0.5);
    return (x < 0 && n ? "-" : "+") + Math.floor(n / 10) + "." + (n % 10) + "%";
  }
  function pct(x) { return Math.floor(Math.min(1, Math.max(0, x)) * 100 + 0.5); }
  function bases(m, style) {
    var obc = m.obc_after || "000", i;
    if (style === "words") {
      var on = [], names = [["1st", 2], ["2nd", 1], ["3rd", 0]];
      for (i = 0; i < 3; i++) if (obc.charAt(names[i][1]) === "1") on.push(names[i][0]);
      if (!on.length) return "Bases empty";
      if (on.length === 3) return "Bases loaded";
      return (on.length === 1 ? "Runner on " : "Runners on ") + (on.length === 1 ? on[0] : on[0] + " and " + on[1]);
    }
    return [0, 1, 2].map(function (k) { return obc.charAt(k) === "1" ? "◆" : "◇"; }).join(" ");
  }
  function outs(m) {
    var n = m.outs_after;
    if (n === null || n === undefined) return "";
    return n + " out" + (n === 1 ? "" : "s");
  }
  function nul(x) { return x === null || x === undefined; }
  // a player's name from the row's <key>_name, or <key>_last for the last-name style (full when the row has no last name)
  function name(m, key, style) {
    var full = m[key + "_name"] || "";
    return style === "last" ? (m[key + "_last"] || full) : full;
  }

  // [batter or runner, "vs pitcher" or "vs catcher"] with both names in one style, "full" or "last"
  function people(m, style) {
    var steal = m.result.slice(0, 2) === "SB" || m.result.slice(0, 2) === "CS";
    if (steal) {
      var ckey = m.featured_side === "fielding" ? "featured" : "counterpart";
      return [name(m, m.runner_name ? "runner" : "batter", style), m[ckey + "_name"] ? "vs " + name(m, ckey, style) : ""];
    }
    return [name(m, m.batter_name ? "batter" : "featured", style), m.pitcher_name ? "vs " + name(m, "pitcher", style) : ""];
  }

  function parts(m, labels, short, f) {
    var full = people(m, "full"), last = people(m, "last");
    var nm = m.scoring_names || [];
    var scoring = !nm.length ? "" : (nm.length === 1 ? nm[0] + " scores" : nm.join(", ") + " score");
    var tags = TAG_ORDER.filter(function (t) { return (m.tags || []).indexOf(t) !== -1; }).map(function (t) { return TAG_TEXT[t]; }).slice(0, 1);   // one tag, the most notable
    if (tags.length === 1 && tags[0] === TAG_TEXT.steal && /^(SB|CS)/.test(m.result || "")) tags = [String(labels[m.result] || tags[0]).toUpperCase()];   // a steal names what happened: STOLEN BASE / CAUGHT STEALING
    else if (tags.length === 1 && tags[0] === TAG_TEXT.lead_change && m.home_score === m.away_score) tags = ["TIED UP"];   // the "lead change" that evens the score is a tie: TIED UP
    var lev = m.leverage_after;   // leverage of the situation the play leaves behind
    var abbr, wpa, after, before;
    if (f.pv === "feat") {
      abbr = m.featured_team_abbr; wpa = m.featured_wpa; after = m.featured_wp_after;
      before = nul(after) || nul(wpa) ? null : after - wpa;
    } else {
      abbr = m.off_team_abbr; wpa = m.wpa; after = m.win_prob_after; before = nul(m.win_prob_before) ? null : m.win_prob_before;
    }
    var wp = nul(after) || !abbr ? "" : abbr + " WP " + pct(after) + "%";
    var wpChange = nul(after) || !abbr ? "" : before !== null ? abbr + " WP " + pct(before) + "% → " + pct(after) + "%" : wp;
    return {
      score: m.away_team_abbr + " " + m.away_score + " - " + m.home_team_abbr + " " + m.home_score,
      inning: (m.half === "top" ? "▲" : "▼") + " " + m.inning,
      batter: full[0],
      batter_last: last[0],
      result: (short || {})[m.result] || m.result,
      result_full: (labels || {})[m.result] || m.result,
      pitcher: full[1],
      pitcher_last: last[1],
      scoring: scoring,
      bases: bases(m, "glyph"),
      bases_words: bases(m, "words"),
      outs: outs(m),
      wpa: nul(wpa) ? "" : "WPA " + signedPct(wpa),
      lev: nul(lev) ? "" : "LI " + tenths(lev),
      wp: wp,
      wp_change: wpChange,
      diff: m.pitch && m.swing && !nul(m.diff) ? "Pitch " + m.pitch + " / Swing " + m.swing + " / Diff " + m.diff : "",
      tags: tags.join(" · ")
    };
  }
  function sep(prev, cur) {
    if (prev === "score" && cur === "inning") return " ";
    if ((prev === "batter" || prev === "batter_last") && (cur === "result" || cur === "result_full" || cur === "pitcher" || cur === "pitcher_last")) return " ";
    return " · ";
  }
  function render(m, fmt, labels, short) {
    var f = normalize(fmt), p = parts(m, labels, short, f), lines = [];
    f.l.forEach(function (line) {
      var out = "", prev = "";
      line.forEach(function (cid) {
        var text = typeof cid === "string" && has(p, cid) ? p[cid] : "";
        if (!text) return;
        out += (out ? sep(prev, cid) : "") + text;
        prev = cid;
      });
      if (out) lines.push(out);
    });
    if (!lines.length) return null;
    return { title: lines[0], body: lines.slice(1) };
  }

  window.MLNPushFmt = { COMPONENTS: COMPONENTS, PRESETS: PRESETS, SAMPLES: SAMPLES, CLASSIC: CLASSIC, SPOTLIGHT: SPOTLIGHT, normalize: normalize, render: render };
})();

/* MLN Gameday - career, records and search extras.
 *
 * Everything here reads the derived files career_build.py writes next to the
 * per-season data (data/career/*, data/records.json, data/search/*), each
 * fetched only when the view that needs it is opened:
 *
 *   Stats tab Career / Season scopes  -> data/career/totals.json   (one small file, all players)
 *   Player career panel (game log)    -> data/career/p/<id>.json   (one player's games)
 *   Records tab                       -> data/records.json
 *   Search tab                        -> data/search/sNN.json      (only the seasons in range, on Apply)
 *
 * app.js hands this file a small bridge (MLNExtras.init) for the few things
 * only it knows: the live build version, which season is active, and how to
 * jump to a season/session/game in the existing Plays and Stats views.
 */
(function () {
  "use strict";

  var B = null;           // bridge from app.js
  var cache = {};

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // "&f=" is the data-file format; bump it when a derived file gains fields, so a browser's cached older copy is skipped.
  function getJ(url) {
    if (!cache[url]) {
      cache[url] = fetch(url + "?v=" + encodeURIComponent((B && B.version()) || "") + "&f=3").then(function (r) {
        if (!r.ok) throw new Error(url + ": " + r.status);
        return r.json();
      }).catch(function (e) { delete cache[url]; throw e; });
    }
    return cache[url];
  }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }

  // Game type filter shared by every view here: all | reg (regular season) | post (postseason).
  var GT_LABELS = [["all", "All games"], ["reg", "Regular season"], ["post", "Postseason"]];
  function gtOk(gt, isPost) { return gt === "all" || (gt === "post" ? !!isPost : !isPost); }
  function gtPills(gt, attr) {
    return '<div class="stats-subtabs">' + GT_LABELS.map(function (g) {
      return '<button type="button" class="stats-subtab' + (gt === g[0] ? " active" : "") + '" ' + attr + '="' + g[0] + '">' + g[1] + "</button>";
    }).join("") + "</div>";
  }

  // ── stat math (shared by the Stats tab's aggregate scopes, total rows and the player panel) ──

  var BAT_SUM = ["pa", "ab", "r", "h", "2b", "3b", "hr", "rbi", "bb", "k", "sb", "cs", "gidp", "lob", "sf", "ds", "dn"];
  var PIT_SUM = ["outs", "h", "hr", "bb", "k", "dp", "er", "lob", "w", "l", "sv", "hd", "ds", "dn"];
  var RATE_KEYS = { avg: 1, obp: 1, slg: 1, ops: 1 };

  function ipToOuts(ip) {
    var p = String(ip).split(".");
    return Number(p[0]) * 3 + Number(p[1] || 0);
  }
  function fmtIP(outs) { return Math.floor(outs / 3) + "." + (outs % 3); }
  function r1(x) { return Math.round(x * 10) / 10; }

  function addInto(sum, src, keys) {
    keys.forEach(function (k) { if (src[k]) sum[k] = (sum[k] || 0) + src[k]; });
    if (src.wpa) sum.wpa = r1((sum.wpa || 0) + src.wpa);
  }

  function withBatRates(s) {
    var ab = s.ab || 0, h = s.h || 0, bb = s.bb || 0, sf = s.sf || 0;
    var tb = h + (s["2b"] || 0) + 2 * (s["3b"] || 0) + 3 * (s.hr || 0);
    s.avg = ab ? h / ab : null;
    s.obp = (ab + bb + sf) ? (h + bb) / (ab + bb + sf) : null;
    s.slg = ab ? tb / ab : null;
    s.ops = s.obp != null && s.slg != null ? s.obp + s.slg : null;
    s.dpa = s.dn ? s.ds / s.dn : null;
    return withGaps(s);
  }
  /* Runs / WPA against the calculator model's expectation (absent where a season has no modelled swings). */
  function withGaps(s) {
    var has = s.xr != null || s.rv != null;
    s.rvx = has ? (s.rv || 0) - (s.xr || 0) : null;
    s.wvx = has ? (s.wp || 0) - (s.xw || 0) : null;
    return s;
  }
  function withPitRates(s) {
    var outs = s.outs || 0;
    s.ip = fmtIP(outs);
    s.era = outs ? (s.er || 0) * 6 / (outs / 3) : null;       // per 6 innings: the league's game length
    s.whip = outs ? ((s.bb || 0) + (s.h || 0)) / (outs / 3) : null;
    s.dpa = s.dn ? s.ds / s.dn : null;
    return withGaps(s);
  }

  // The swing-value columns (build-time model: diff_metrics.py). Pitchers get the same numbers negated, so they read as saved.
  function swingCols() { return []; }

  /* Sum a set of Stats-tab rows into one Total line. Works on session rows
     (ip string, no outs) and on aggregate rows (outs) alike. */
  function totalLine(rows, isBatting) {
    var s = {};
    rows.forEach(function (r) {
      var src = r;
      if (!isBatting && r.outs == null && r.ip != null) src = Object.assign({}, r, { outs: ipToOuts(r.ip) });
      addInto(s, src, isBatting ? BAT_SUM : PIT_SUM);
    });
    return isBatting ? withBatRates(s) : withPitRates(s);
  }

  // ── Stats tab: Season / Career scopes from career/totals.json ──────────────────

  var totals = null;      // loaded totals.json
  var totalsLoading = null;

  function loadTotals() {
    if (totals) return Promise.resolve(totals);
    if (!totalsLoading) {
      totalsLoading = getJ("data/career/totals.json").then(function (t) { totals = t; return t; });
      totalsLoading.catch(function () { totalsLoading = null; });
    }
    return totalsLoading;
  }

  var AGG_BAT_COLS = [
    ["team", "Team"], ["g", "G"], ["pa", "PA"], ["ab", "AB"], ["r", "R"], ["h", "H"], ["2b", "2B"], ["3b", "3B"],
    ["hr", "HR"], ["rbi", "RBI"], ["bb", "BB"], ["k", "K"], ["sb", "SB"], ["cs", "CS"], ["gidp", "GIDP"], ["lob", "LOB"],
    ["avg", "AVG", 3], ["obp", "OBP", 3], ["slg", "SLG", 3], ["ops", "OPS", 3], ["dpa", "DPA", 1], ["wpa", "WPA", 1],
  ].concat(swingCols("bat"));
  var AGG_PIT_COLS = [
    ["team", "Team"], ["g", "G"], ["w", "W"], ["l", "L"], ["sv", "SV"], ["hd", "HD"], ["ip", "IP"], ["h", "H"],
    ["er", "ER"], ["bb", "BB"], ["k", "K"], ["hr", "HR"], ["dp", "DP"], ["lob", "LOB"], ["era", "ER6", 2], ["whip", "WHIP", 2],
    ["dpa", "DPA", 1], ["wpa", "WPA", 1],
  ].concat(swingCols("pit"));

  /* Rows for the Stats table: season = one season's totals, else the career
     (optionally from..to). null while totals.json is still loading. */
  function statsAggregate(isBatting, onlySeason, from, to, gt) {
    gt = gt || "all";
    if (!totals) { loadTotals().then(function () { if (B) B.rerenderStats(); }); return null; }
    var lo = onlySeason != null ? onlySeason : (from || 0);
    var hi = onlySeason != null ? onlySeason : (to || 99);
    var side = isBatting ? "b" : "p";
    var rows = [];
    Object.keys(totals.players).forEach(function (pid) {
      var ent = totals.players[pid];
      var sum = {}, g = 0, lastS = -1, team = "";
      Object.keys(ent.s).forEach(function (sn) {
        var n = parseInt(sn, 10);
        if (n < lo || n > hi) return;
        if (!gtOk(gt, sn.charAt(sn.length - 1) === "p")) return;
        var line = ent.s[sn][side];
        if (!line) return;
        addInto(sum, line, isBatting ? BAT_SUM : PIT_SUM);
        g += line.g || 0;
        if (n > lastS) { lastS = n; team = ent.s[sn].t; }
      });
      if (lastS < 0) return;
      if (isBatting && !(sum.pa || sum.sb || sum.cs || sum.r)) return;
      sum.g = g;
      sum.player_id = Number(pid);
      sum.name = ent.n;
      sum.team = team;
      sum.session = "all";
      rows.push(isBatting ? withBatRates(sum) : withPitRates(sum));
    });
    return { rows: rows, cols: isBatting ? AGG_BAT_COLS : AGG_PIT_COLS };
  }

  // ── player career panel ────────────────────────────────────────────────────────

  var PBAT = [["pa", "PA"], ["ab", "AB"], ["r", "R"], ["h", "H"], ["2b", "2B"], ["3b", "3B"], ["hr", "HR"],
    ["rbi", "RBI"], ["bb", "BB"], ["k", "K"], ["sb", "SB"], ["cs", "CS"], ["lob", "LOB"]];
  var PPIT = [["ip", "IP"], ["h", "H"], ["er", "ER"], ["bb", "BB"], ["k", "K"], ["hr", "HR"], ["dp", "DP"], ["lob", "LOB"]];

  var pm = { data: null, side: "bat", season: "all", gt: "all", loading: false, view: "stats", sortSeason: { key: "s", dir: "asc" }, sortLog: { key: "sn", dir: "desc" } };

  function ensureModal() {
    if ($("career-modal")) return;
    var m = document.createElement("div");
    m.className = "modal";
    m.id = "career-modal";
    m.hidden = true;
    m.innerHTML = '<div class="modal-card wide"><div class="modal-head" id="career-head"><img class="ph-logo" id="career-logo" alt="" hidden>' +
      '<div class="ph-left"><div class="ph-name"><strong id="career-title">Player</strong>' +
      '<span class="ph-pos" id="career-pos"></span></div><div class="ph-build" id="career-build"></div></div>' +
      '<button type="button" class="ph-link" id="career-copy" title="Copy link to this player" aria-label="Copy link to this player">' +
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/></svg></button>' +
      '<button type="button" class="ph-x" id="career-close" aria-label="Close">&times;</button></div>' +
      '<div class="modal-body" id="career-body"></div></div>';
    document.body.appendChild(m);
    $("career-close").addEventListener("click", closePlayer);
    $("career-copy").addEventListener("click", function () { if (pm.id) B.copyLink(playerLinkUrl()); });
    m.addEventListener("click", function (e) { if (e.target === m) closePlayer(); });
    document.addEventListener("keydown", function (e) {
      var rm = $("replay-modal");
      if (e.key === "Escape" && !m.hidden && !(rm && !rm.hidden)) closePlayer();   // Escape closes the replay first
    });
    m.addEventListener("click", function (e) {
      var sub = e.target.closest("[data-pside]");
      if (sub) { pm.side = sub.getAttribute("data-pside"); pm.season = "all"; pm.sortSeason = { key: "s", dir: "asc" }; pm.sortLog = { key: "d", dir: "desc" }; renderPlayer(); return; }
      var gtb = e.target.closest("[data-pgt]");
      if (gtb) { pm.gt = gtb.getAttribute("data-pgt"); pm.season = "all"; renderPlayer(); return; }
      var psk = e.target.closest("[data-psk]");
      if (psk) { pm.sortSeason = nextSort(pm.sortSeason, psk.getAttribute("data-psk")); renderPlayer(); return; }
      var plk = e.target.closest("[data-plk]");
      if (plk) { pm.sortLog = nextSort(pm.sortLog, plk.getAttribute("data-plk")); renderPlayer(); return; }
      var pv = e.target.closest("[data-pview]");
      if (pv) { pm.view = pv.getAttribute("data-pview"); renderPlayer(); return; }
      var ssn = e.target.closest("[data-pseason]");
      if (ssn) { var sv = ssn.getAttribute("data-pseason"); pm.season = pm.season === sv ? "all" : sv; renderPlayer(); return; }
      var g = e.target.closest("[data-pgame]");
      if (g) {
        // the game replays on top of this panel, so the player's log is still here when it closes
        B.openPlay({ s: Number(g.getAttribute("data-s")), n: Number(g.getAttribute("data-n")), g: g.getAttribute("data-pgame") });
      }
    });
  }

  // ── Diffs view: where a player's diffs land (20-wide buckets) ──

  // The results the model separates (1B, 1BWH, 2B, GO, DP21 ...), each in the Streamlit scouting colour for it -
  // both come from model.json (gc), so the chart stays in step with the model.
  var modelData = null;
  function DX() { return modelData.gc; }
  // Stack / legend order, bottom to top: the model's order, except the doubles and singles lead with their
  // "with hold-up" variants (2BWH before 2B, 1BWH and 1BWH2 before 1B).
  var DX_FIRST = ["HR", "3B", "2BWH", "2B", "1BWH", "1BWH2", "1B"];
  function dxOrder() {
    var names = DX().map(function (x) { return x[0]; }), out = [];
    DX_FIRST.forEach(function (n) { var i = names.indexOf(n); if (i >= 0) out.push(i); });
    names.forEach(function (n, i) { if (out.indexOf(i) < 0) out.push(i); });
    return out;
  }

  function dxBins(d, side) {
    var tot = [], nc = DX().length;
    for (var c = 0; c < nc; c++) tot.push(new Array(25).fill(0));
    var dist = (d.dist || {})[side] || {};
    Object.keys(dist).forEach(function (sk) {
      var post = sk.charAt(sk.length - 1) === "p", sn = parseInt(sk, 10);
      if (!gtOk(pm.gt, post) || (pm.season !== "all" && String(sn) !== pm.season)) return;
      var arr = dist[sk];
      for (var sc = 0; sc < nc; sc++) for (var b = 0; b < 25; b++) tot[sc][b] += arr[sc * 25 + b] || 0;
    });
    return tot;
  }

  function dxChart(kind, bins, lg, dpa, lgdpa) {
    // Sized to the panel (so the labels stay a readable size at any width): up to 1000px wide on a big screen.
    var W = Math.max(320, Math.min(1000, ($("career-body").clientWidth || 700) - 36));
    // The DPA / League labels get their own band above the plot (two rows when they have to stack), so they
    // never sit on top of the bars.
    var withMk = kind === "count" && (dpa != null || lgdpa != null);
    var T = withMk ? 30 : 12;
    var H = Math.round(W * 0.34) + (withMk ? 18 : 0), L = 40, R = 18, Bm = 26, pw = W - L - R, ph = H - T - Bm, bw = pw / 25;
    var n = [];
    var NC = bins.length;
    for (var b = 0; b < 25; b++) { var t = 0; for (var c = 0; c < NC; c++) t += bins[c][b]; n.push(t); }
    var max = kind === "count" ? Math.max.apply(null, n.concat([1])) : 1;
    var yOf = function (v) { return T + ph - (v / max) * ph; };
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + (kind === "count" ? "Swings by diff" : "Results by diff") + '">';
    var ticks = kind === "count" ? [0, Math.round(max / 2), max] : [0, 0.5, 1];
    ticks.forEach(function (v) {
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + yOf(v) + '" y2="' + yOf(v) + '" class="dx-grid"/><text x="' + (L - 4) + '" y="' + (yOf(v) + 3) + '" text-anchor="end">' +
        (kind === "count" ? v : Math.round(v * 100) + "%") + "</text>";
    });
    for (var i = 0; i < 25; i++) {
      var x = L + i * bw + 1, w = bw - 2, base = 0, lo = i * 20, hi = i === 24 ? 500 : lo + 19;
      var tip = "Diff " + lo + "-" + hi;
      var y0 = T + ph;
      // the whole column (for the space above the stack); each segment below carries its own tip on top of it
      svg += '<rect x="' + x.toFixed(1) + '" y="' + T + '" width="' + w.toFixed(1) + '" height="' + ph + '" fill="transparent"><title>' + tip + "\n" + n[i] + " swing" + (n[i] === 1 ? "" : "s") + "</title></rect>";
      if (n[i]) {
        var ord = dxOrder();
        for (var o2 = 0; o2 < NC; o2++) {
          var c2 = ord[o2];
          var v2 = kind === "count" ? bins[c2][i] : bins[c2][i] / n[i];
          if (!v2) continue;
          var h = (v2 / max) * ph;
          y0 -= h;
          svg += '<rect x="' + x.toFixed(1) + '" y="' + y0.toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + Math.max(h - 0.6, 0.5).toFixed(1) + '" fill="' + DX()[c2][1] + '"' +
            (kind === "share" && n[i] < 5 ? ' opacity="0.4"' : "") + "><title>" + tip + "\n" + bins[c2][i] + " " + DX()[c2][0] + (kind === "share" ? " (" + Math.round(100 * bins[c2][i] / n[i]) + "%)" : "") + "</title></rect>";
        }
      }
    }
    if (kind === "share" && lg) {
      var pts = [];
      for (var j = 0; j < 25; j++) {
        var reach = lg[0][j] + lg[1][j] + lg[2][j] + lg[3][j] + lg[4][j];
        pts.push((L + j * bw + bw / 2).toFixed(1) + "," + yOf(reach).toFixed(1));
      }
      svg += '<polyline points="' + pts.join(" ") + '" class="dx-lg"/>';
    }
    // The two markers: the league's (dotted, grey) and the player's DPA. Labels sit on opposite sides of their
    // lines - the lower value's to its left, the higher value's to its right - so they never run into each other,
    // and stack vertically only when an edge leaves no room on the outside.
    var marks = [[lgdpa, "dx-mk-lg", "League " + (lgdpa == null ? "" : Math.round(lgdpa))],
      [dpa, "dx-mk", "DPA " + (dpa == null ? "" : Math.round(dpa))]].filter(function (m) { return m[0] != null; });
    marks.sort(function (a, b) { return a[0] - b[0]; });
    marks.forEach(function (m, idx) {
      var mx = L + (m[0] / 500) * pw;
      svg += '<line x1="' + mx.toFixed(1) + '" x2="' + mx.toFixed(1) + '" y1="' + T + '" y2="' + (T + ph) + '" class="' + m[1] + '"/>';
      if (kind !== "count") return;
      var left = marks.length > 1 ? idx === 0 : mx > W - 90;
      if (marks.length > 1 && idx === 0 && mx < L + 80) left = false;          // no room on the left edge
      if (marks.length > 1 && idx === 1 && mx > W - 90) left = true;            // no room on the right edge
      var stack = marks.length > 1 && ((idx === 0 && !left) || (idx === 1 && left));
      svg += '<text x="' + (mx + (left ? -5 : 5)).toFixed(1) + '" y="' + (11 + (stack ? idx * 12 : 0)) + '" text-anchor="' + (left ? "end" : "start") +
        '" class="' + m[1] + '-t">' + m[2] + "</text>";
    });
    for (var tk = 0; tk <= 5; tk++) {
      svg += '<text x="' + (L + (tk * 100 / 500) * pw).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle">' + tk * 100 + "</text>";
    }
    return svg + "</svg>";
  }

  // The league's average diff for what the chart is showing: the selected season (or all of them), regular
  // season and/or postseason as filtered - the same slice as the player's own line.
  // League average over the seasons the player actually has swings in (so "All" matches a one-season player's S-view).
  function leagueDpa(d, side) {
    var lg = modelData.lg_s;
    if (!lg) return modelData.lgdpa;
    var mine = {};
    Object.keys(((d && d.dist) || {})[side] || {}).forEach(function (k) { mine[parseInt(k, 10)] = 1; });
    var ds = 0, dn = 0;
    Object.keys(lg).forEach(function (k) {
      var post = k.charAt(k.length - 1) === "p", sn = parseInt(k, 10);
      if (!gtOk(pm.gt, post) || (pm.season !== "all" && String(sn) !== pm.season) || !mine[sn]) return;
      ds += lg[k][0]; dn += lg[k][1];
    });
    return dn ? ds / dn : modelData.lgdpa;
  }

  function renderDiffs(d, tabs, all) {
    var side = pm.side;
    var seasons = {};
    all.forEach(function (r) { seasons[r.s] = 1; });
    var snums = Object.keys(seasons).map(Number).filter(function (n) { return n >= 1; }).sort(function (a, b) { return a - b; });
    var pills = '<div class="stats-subtabs"><button type="button" class="stats-subtab' + (pm.season === "all" ? " active" : "") + '" data-pseason="all">All</button>' +
      snums.map(function (sn) {
        return '<button type="button" class="stats-subtab' + (String(sn) === pm.season ? " active" : "") + '" data-pseason="' + sn + '">S' + sn + "</button>";
      }).join("") + "</div>";
    if (!modelData) {
      $("career-body").innerHTML = tabs + '<div class="stats-empty">Loading diffs...</div>';
      getJ("data/career/model.json").then(function (m) { modelData = m; renderPlayer(); }).catch(function () {
        $("career-body").innerHTML = tabs + '<div class="stats-empty">Could not load the diff model.</div>';
      });
      return;
    }
    var bins = dxBins(d, side);
    var n = 0;
    for (var c = 0; c < bins.length; c++) for (var b = 0; b < 25; b++) n += bins[c][b];
    var rows = all.filter(function (r) { return (pm.season === "all" || String(r.s) === pm.season); });
    var line = lineFrom(rows, side);
    // Three centred rows: HR-BB, SacF-GO, K-DPH1.
    var legend = '<div class="dx-legend">', rowStart = { SacF: 1, K: 1 };
    dxOrder().forEach(function (i) {
      var x = DX()[i];
      if (rowStart[x[0]]) legend += '<div class="dx-legend-break"></div>';
      legend += '<span><i style="background:' + x[1] + '"></i>' + x[0] + "</span>";
    });
    legend += "</div>";
    var body;
    if (!n) body = '<div class="stats-empty">No modelled swings here.</div>';
    else {
      body = '<div class="xt-sub">Diff Distribution <span class="xt-hint">' + n.toLocaleString() + " swings, 20-wide buckets</span></div>" +
        '<div class="dx-chart">' + dxChart("count", bins, null, line.dpa, leagueDpa(d, side)) + "</div>" + legend;
    }
    $("career-body").innerHTML = tabs + '<div class="xt-sub">Season</div>' + pills + '<div class="xt-diffs">' + body + "</div>";
  }

  function closePlayer() { var m = $("career-modal"); if (m) m.hidden = true; }

  // Large screens: size the card once, on open, to the widest of the player's Batting / Pitching Stats views
  // (the Diffs chart just fills whatever width that is), so switching tabs never resizes it.
  function fitCard() {
    var card = $("career-modal").querySelector(".modal-card");
    card.style.width = "";
    if (window.innerWidth <= 700 || !pm.data) return;
    var keep = { side: pm.side, view: pm.view, season: pm.season, gt: pm.gt }, widest = 0;
    ["bat", "pit"].forEach(function (s) {
      if (!pm.data[s].length) return;
      pm.side = s; pm.view = "stats"; pm.season = "all"; pm.gt = "all";
      renderPlayer();
      widest = Math.max(widest, card.getBoundingClientRect().width);
    });
    pm.side = keep.side; pm.view = keep.view; pm.season = keep.season; pm.gt = keep.gt;
    if (widest) card.style.width = Math.ceil(widest) + "px";
  }

  // Header: the player's team colour behind the name, team logo at the right, and primary / secondary
  // position next to the name. Colours come from search/names.json (the team's latest season); text flips
  // between white and near-black by the colour's brightness.
  function styleHead(d) {
    var head = $("career-head"), logo = $("career-logo"), pos = $("career-pos");
    var h = d && d.hdr, t = h && at.teams && at.teams[h.t];
    var hex = t && /^[0-9a-f]{6}$/i.test(t.c1 || "") ? t.c1 : "";
    head.style.background = hex ? "#" + hex : "";
    if (hex) {
      var lum = (0.299 * parseInt(hex.slice(0, 2), 16) + 0.587 * parseInt(hex.slice(2, 4), 16) + 0.114 * parseInt(hex.slice(4, 6), 16)) / 255;
      head.style.color = lum > 0.6 ? "#161715" : "#fff";
    } else head.style.color = "";
    head.classList.toggle("ph-colored", !!hex);
    // Accent (tab underline, active pills, slider) follows the team's secondary colour, else its primary.
    // Near-white picks keep the site red; the text on a filled accent flips by luminance.
    var card = $("career-modal").querySelector(".modal-card"), acc = "";
    [t && t.c2, t && t.c1].some(function (c) {
      if (!/^[0-9a-f]{6}$/i.test(c || "")) return false;
      var l = (0.299 * parseInt(c.slice(0, 2), 16) + 0.587 * parseInt(c.slice(2, 4), 16) + 0.114 * parseInt(c.slice(4, 6), 16)) / 255;
      if (l > 0.8) return false;
      acc = c;
      card.style.setProperty("--acc-ink", l > 0.6 ? "#161715" : "#fff");
      return true;
    });
    if (acc) card.style.setProperty("--red", "#" + acc);
    else { card.style.removeProperty("--red"); card.style.removeProperty("--acc-ink"); }
    if (t && t.logo) { logo.src = t.logo; logo.alt = t.name || ""; logo.hidden = false; } else logo.hidden = true;
    var ps = h ? h.pos.filter(Boolean) : [];
    pos.innerHTML = ps.map(function (x) { return '<span class="ph-pill">' + esc(x) + "</span>"; }).join("");
    // Current (or, for a player no longer rostered, most recent) build: hand, then whichever attributes they have.
    var b = (h && h.bld) || {}, parts = [];
    if (b.hand) parts.push("<span>" + esc(b.hand) + "</span>");
    ["con", "eye", "pwr", "spd", "mov", "cmd", "vel", "awr"].forEach(function (k) {
      if (b[k] != null) parts.push("<span><b>" + k.toUpperCase() + "</b> " + b[k] + "</span>");
    });
    $("career-build").innerHTML = parts.join("");
  }

  function openPlayer(id, name, opts) {
    ensureModal();
    $("career-modal").hidden = false;
    $("career-title").textContent = name || "Player";
    styleHead(null);
    $("career-modal").querySelector(".modal-card").style.width = "";
    $("career-body").innerHTML = '<div class="stats-empty">Loading career...</div>';
    pm = { id: id, data: null, side: "bat", season: "all", gt: "all", loading: true, view: "stats", sortSeason: { key: "s", dir: "asc" }, sortLog: { key: "sn", dir: "desc" } };
    Promise.all([getJ("data/career/p/" + id + ".json"), loadNames()]).then(function (res) {
      var d = res[0];
      pm.data = d;
      pm.loading = false;
      // Open on whichever side has the bigger body of work.
      var batPA = d.bat.reduce(function (a, r) { return a + (r.pa || 0); }, 0);
      pm.side = d.pit.length > d.bat.length || (!batPA && d.pit.length) ? "pit" : "bat";
      // a shared link can ask for a side / view, honoured when this player has rows for it
      if (opts && opts.side && d[opts.side] && d[opts.side].length) pm.side = opts.side;
      if (opts && opts.view === "diffs") pm.view = "diffs";
      $("career-title").textContent = d.name;
      styleHead(d);
      fitCard();
      renderPlayer();
    }).catch(function () {
      $("career-body").innerHTML = '<div class="stats-empty">No career log found for this player.</div>';
    });
  }

  function cell(v, dec, key) {
    if (v == null) return "-";
    if (dec != null) {
      var s = Number(v).toFixed(dec);
      return RATE_KEYS[key] ? s.replace(/^0\./, ".") : s;
    }
    return String(v);
  }

  function lineFrom(rows, side) {
    var s = {};
    rows.forEach(function (r) { addInto(s, r, side === "bat" ? BAT_SUM : PIT_SUM); });
    s.g = rows.length;
    return side === "bat" ? withBatRates(s) : withPitRates(s);
  }

  // Shared sortable-header helpers. A column is [key, label, decimals]; text
  // keys sort A-Z first, everything else high-to-low first.
  var TXT_KEYS = { team: 1, t: 1, o: 1, dec: 1, name: 1, d: 1, bases: 1 };
  function sortRows(rows, st) {
    var dir = st.dir === "asc" ? 1 : -1;
    return rows.slice().sort(function (a, b) {
      var av = a[st.key], bv = b[st.key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      var c = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return c * dir;
    });
  }
  function nextSort(st, key) {
    if (st.key === key) return { key: key, dir: st.dir === "asc" ? "desc" : "asc" };
    return { key: key, dir: TXT_KEYS[key] ? "asc" : "desc" };
  }
  function sortHead(attr, cols, st) {
    return cols.map(function (c) {
      var on = st.key === c[0];
      return '<th class="xt-sortable' + (on ? " active" : "") + (c[0] === "name" ? " stats-col-name" : "") + '" ' + attr + '="' + c[0] + '">' + esc(c[1]) +
        (on ? (st.dir === "asc" ? " \u25B2" : " \u25BC") : "") + "</th>";
    }).join("");
  }
  function fmtDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? m[2] + "/" + m[3] + "/" + m[1] : (iso || "");
  }

  var NULLABLE = { wpa: 1, gsc: 1, dpa: 1, rv: 1, xr: 1, rvx: 1, wp: 1, xw: 1, wvx: 1 };

  function renderPlayer() {
    var d = pm.data;
    if (!d) return;
    var side = pm.side;
    var everything = d[side];
    var all = everything.filter(function (r) { return gtOk(pm.gt, r.po); });
    var cols = side === "bat" ? PBAT : PPIT;
    var hasPost = d.bat.concat(d.pit).some(function (r) { return r.po; });

    // Batting / Pitching are the top-level tabs (only the sides this player has played); Stats / Diffs sit under them.
    var sideTabs = ["bat", "pit"].filter(function (s) { return d[s].length; });
    var tabs = (sideTabs.length ? '<div class="view-tabs xt-ptop">' + sideTabs.map(function (s) {
      return '<button type="button" class="view-tab' + (side === s ? " active" : "") + '" data-pside="' + s + '">' +
        (s === "bat" ? "Batting" : "Pitching") + "</button>";
    }).join("") + "</div>" : "") +
      '<div class="view-tabs xt-ptabs">' + [["stats", "Stats"], ["diffs", "Diffs"]].map(function (v) {
        return '<button type="button" class="view-tab' + (pm.view === v[0] ? " active" : "") + '" data-pview="' + v[0] + '">' + v[1] + "</button>";
      }).join("") + "</div>" + (hasPost ? gtPills(pm.gt, "data-pgt") : "");

    if (all.length && pm.view === "diffs") { renderDiffs(d, tabs, all); return; }

    if (!all.length) {
      $("career-body").innerHTML = tabs + '<div class="stats-empty">No ' + (side === "bat" ? "batting" : "pitching") + " games.</div>";
      return;
    }

    // by-season table (click a season to filter the log; click it again to clear)
    var seasons = {};
    all.forEach(function (r) { (seasons[r.s] = seasons[r.s] || []).push(r); });
    var snums = Object.keys(seasons).map(Number).sort(function (a, b) { return a - b; });
    var seasonCols = side === "bat"
      ? cols.concat([["avg", "AVG", 3], ["obp", "OBP", 3], ["slg", "SLG", 3], ["ops", "OPS", 3], ["dpa", "DPA", 1], ["wpa", "WPA", 1]].concat(swingCols("bat")))
      : [["w", "W"], ["l", "L"], ["sv", "SV"], ["hd", "HD"]].concat(cols).concat([["era", "ER6", 2], ["whip", "WHIP", 2], ["dpa", "DPA", 1], ["wpa", "WPA", 1]]).concat(swingCols("pit"));
    var allSeasonCols = [["s", "Season"], ["team", "Team"], ["g", "G"]].concat(seasonCols);
    var srows = snums.map(function (sn) {
      var rows = seasons[sn], line = lineFrom(rows, side);
      var teams = [];
      rows.forEach(function (r) { if (teams.indexOf(r.t) === -1) teams.push(r.t); });
      line.s = sn;
      line.team = teams.join("/");
      return line;
    });
    srows = sortRows(srows, pm.sortSeason);
    var body = srows.map(function (line) {
      return '<tr class="xt-click' + (String(line.s) === pm.season ? " xt-on" : "") + '" data-pseason="' + line.s + '">' +
        allSeasonCols.map(function (c) {
          return "<td>" + (c[0] === "s" ? "S" + line.s : c[0] === "team" ? esc(line.team) : cell(line[c[0]], c[2], c[0])) + "</td>";
        }).join("") + "</tr>";
    }).join("");
    var career = lineFrom(all, side);
    var foot = '<tr><td>' + (pm.gt === "post" ? "Postseason" : pm.gt === "reg" ? "Regular season" : "Career") + '</td><td></td><td>' + career.g + "</td>" +
      seasonCols.map(function (c) { return "<td>" + cell(career[c[0]], c[2], c[0]) + "</td>"; }).join("") + "</tr>";
    var seasonTable = '<div class="stats-table-wrap xt-table xt-season"><table class="stats-table xt-plain"><thead><tr>' +
      sortHead("data-psk", allSeasonCols, pm.sortSeason) + "</tr></thead><tbody>" + body + "</tbody><tfoot>" + foot + "</tfoot></table></div>";

    // game log
    var shown = all.filter(function (r) { return pm.season === "all" || String(r.s) === pm.season; });
    var gcols = [["d", "Date"], ["sn", "Sn"], ["awr", "Awards"], ["o", "Opp"]].concat(side === "pit" ? [["dec", "Dec"]] : [])
      .concat(cols).concat([["dpa", "DPA", 1], ["wpa", "WPA", 1]]).concat(swingCols(side)).concat([["gsc", "GSc", side === "bat" ? 1 : null]]);
    var grows = shown.map(function (r) {
      var rr = withGaps(Object.assign({}, r, { sn: r.s * 100 + r.n, dpa: r.dn ? (r.ds || 0) / r.dn : null,
        awr: (r.aw === "player" ? 40 : r.aw ? 30 : 0) + (r.ga === "potg" ? 2 : r.ga ? 1 : 0) }));
      if (side === "pit") rr.ipv = r.outs || 0;
      return rr;
    });
    var sortKey = pm.sortLog.key === "ip" ? "ipv" : pm.sortLog.key;
    grows.sort(function (a, b) { return b.sn - a.sn; });   // ties (and games with no recorded date) fall back to newest first
    grows = sortRows(grows, { key: sortKey, dir: pm.sortLog.dir });
    var ghead = sortHead("data-plk", gcols, pm.sortLog);
    var gbody = grows.map(function (r) {
      var rr = side === "pit" ? Object.assign({}, r, { ip: fmtIP(r.outs || 0) }) : r;
      return '<tr class="xt-click" data-pgame="' + esc(r.g) + '" data-s="' + r.s + '" data-n="' + r.n + '" title="Open this game">' +
        gcols.map(function (c) {
          var k = c[0];
          if (k === "d") return "<td>" + (r.d ? esc(fmtDate(r.d)) : "-") + "</td>";
          if (k === "sn") return "<td>S" + r.s + " \u00B7 " + r.n + (r.po ? " (PS)" : "") + "</td>";
          if (k === "awr") return "<td>" + B.awardBadges(r.aw, r.ga) + "</td>";
          if (k === "o") return "<td>" + (r.hm ? "vs " : "@ ") + esc(r.o) + "</td>";
          if (k === "dec") return "<td>" + esc(r.dec || "") + "</td>";
          var v = rr[k];
          return "<td>" + cell(v == null ? (NULLABLE[k] ? null : 0) : v, c[2], k) + "</td>";
        }).join("") + "</tr>";
    }).join("");
    var tl = lineFrom(shown, side);
    var gfoot = "<tr><td>Total</td><td></td><td></td><td>" + shown.length + " G</td>" + (side === "pit" ? "<td></td>" : "") +
      gcols.slice(side === "pit" ? 5 : 4).map(function (c) {
        return "<td>" + (c[0] === "gsc" ? "" : cell(tl[c[0]] == null ? (NULLABLE[c[0]] ? null : 0) : tl[c[0]], c[2], c[0])) + "</td>";
      }).join("") + "</tr>";

    $("career-body").innerHTML = tabs +
      '<div class="xt-sub">By season <span class="xt-hint">click a season to filter the game log, click it again to clear</span></div>' + seasonTable +
      '<div class="xt-sub">Game log' + (pm.season === "all" ? "" : " \u00B7 S" + esc(pm.season)) + '<span class="xt-hint">click a game to open it</span></div>' +
      '<div class="stats-table-wrap xt-table xt-scroll"><table class="stats-table xt-plain"><thead><tr>' + ghead +
      "</tr></thead><tbody>" + gbody + "</tbody><tfoot>" + gfoot + "</tfoot></table></div>";
  }

  // ── Stats tab: team / game / league lines, and every player-session line All-Time ──────────────
  // The Stats tab's own Player table covers one session or a whole season (app.js). Over every season
  // it uses stats_sessions.json (below), and the Team / Game / League granularities are drawn from the
  // records data here: one row per team-session, per game (both teams together) or per league-session.

  var rec = { data: null, games: null, tab: "team", side: "bat", gt: "all", season: "all", session: null, team: "", allTime: false, rookies: false,
    sort: { key: "ops", dir: "desc" }, sorts: {}, shown: 100, el: null, token: 0 };
  // Team, Game and League all open on OPS (batting, best first) or ER6 (pitching, lowest first); a sort the user
  // picks is remembered for that granularity and side.
  function defaultRecSort(side) { return side === "bat" ? { key: "ops", dir: "desc" } : { key: "era", dir: "asc" }; }
  var REC_PAGE = 100;

  var REC_BAT = [["g", "G"], ["pa", "PA"], ["ab", "AB"], ["r", "R"], ["h", "H"], ["2b", "2B"], ["3b", "3B"], ["hr", "HR"], ["rbi", "RBI"],
    ["bb", "BB"], ["k", "K"], ["sb", "SB"], ["cs", "CS"], ["gidp", "GIDP"], ["lob", "LOB"], ["avg", "AVG", 3], ["obp", "OBP", 3], ["slg", "SLG", 3],
    ["ops", "OPS", 3], ["dpa", "DPA", 1], ["wpa", "WPA", 1]].concat(swingCols("bat"));
  var REC_PIT = [["g", "G"], ["ip", "IP"], ["h", "H"], ["er", "ER"], ["bb", "BB"], ["k", "K"], ["hr", "HR"], ["dp", "DP"], ["lob", "LOB"],
    ["era", "ER6", 2], ["whip", "WHIP", 2], ["dpa", "DPA", 1], ["wpa", "WPA", 1]].concat(swingCols("pit"));

  function teamName(season, abbr) {
    if (rec.allTime && at.fr && at.fr[abbr]) return at.fr[abbr].name;      // All-Time shows a team as it is called today
    var s = (rec.data.seasons || []).filter(function (x) { return x.season === season; })[0];
    return (s && s.teams[abbr]) || abbr;
  }

  // bat / pit are the stat lines to add: the whole team's, or just its rookies' when the Rookies filter is on.
  // c = the games / W-L counts to add; bat / pit = the stat lines to add. With Rookies on, c is only the games
  // a rookie played in (this team's own rookies for a team row, either team's for a league row) and the lines are
  // just the rookies'.
  function sumInto(e, c, bat, pit) {
    e.g += c.g; e.rs += c.rs || 0; e.ra += c.ra || 0; e.w += c.w; e.l += c.l;
    Object.keys(bat).forEach(function (k) { e.bat[k] = (e.bat[k] || 0) + bat[k]; });
    Object.keys(pit).forEach(function (k) { e.pit[k] = (e.pit[k] || 0) + pit[k]; });
  }
  function recLines(x) {
    return rec.rookies ? { bat: x.rb || {}, pit: x.rp || {} } : { bat: x.bat, pit: x.pit };
  }
  function recCounts(t, league) {
    if (!rec.rookies) return t;
    return t[(league ? "x" : "o") + (rec.side === "bat" ? "b" : "p")] || null;
  }

  function recSeasonOk(s) {
    if (rec.allTime && rec.sRange && (s < rec.sRange[0] || s > rec.sRange[1])) return false;      // All-Time Seasons slider
    return rec.season === "all" || String(s) === String(rec.season);
  }
  function recSessionOk(n) { return rec.session == null || n === rec.session; }
  // Team filter: a plain abbreviation inside one season, a franchise (current abbreviation) All-Time.
  function recTeamOk(abbr) { return !rec.team || (rec.allTime ? franchiseAbbr(abbr) === rec.team : abbr === rec.team); }

  /* Team / league rows from records.json's team-sessions. A game appears once
     under each team, so a league row's game count is half the sum. */
  function teamRows() {
    var d = rec.data, league = rec.tab === "league";
    var groups = {}, order = [];
    d.teams.forEach(function (t) {
      if (!gtOk(rec.gt, t.po) || !recSeasonOk(t.s) || !recSessionOk(t.n)) return;
      if (!league && !recTeamOk(t.t)) return;
      var ln = recLines(t), cnt = recCounts(t, league);
      if (!cnt) return;                                     // Rookies on and none played in this team's games
      var key = (league ? "L" : t.t) + "|" + t.s + "|" + t.n;
      var e = groups[key];
      if (!e) { e = groups[key] = { s: t.s, n: t.n, abbr: t.t, g: 0, w: 0, l: 0, rs: 0, ra: 0, bat: {}, pit: {} }; order.push(e); }
      sumInto(e, cnt, ln.bat, ln.pit);
    });
    return order.map(function (e) {
      var out = { s: e.s, n: e.n, g: league ? e.g / 2 : e.g, w: e.w, l: e.l, rs: e.rs, ra: e.ra };
      // Rookies on: RS / RA are the rookies' own output - runs they scored, earned runs their pitchers allowed.
      if (rec.rookies) { out.rs = e.bat.r || 0; out.ra = e.pit.er || 0; }
      out.name = league ? "League" : teamName(e.s, e.abbr);
      out.team = league ? "League" : e.abbr;
      var src = rec.side === "bat" ? e.bat : e.pit;
      Object.keys(src).forEach(function (k) { if (k !== "w" && k !== "l") out[k] = src[k]; });
      if (rec.side === "pit") { out.sv = e.pit.sv; out.hd = e.pit.hd; }
      return out;
    });
  }

  /* Game rows from records_games.json: both teams' stats in a game added together. */
  function gameRows() {
    var out = [];
    rec.games.forEach(function (g) {
      if (!gtOk(rec.gt, g.po) || !recSeasonOk(g.s) || !recSessionOk(g.n)) return;
      if (rec.team && !recTeamOk(g.a) && !recTeamOk(g.h)) return;
      var ln = recLines(g);
      var src = rec.side === "bat" ? ln.bat : ln.pit;
      if (rec.rookies && !Object.keys(src).length) return;
      var e = { s: g.s, n: g.n, gc: g.g, g: 1, name: g.a + " " + g.as + " @ " + g.h + " " + g.hs, team: g.a + "/" + g.h };
      Object.keys(src).forEach(function (k) { e[k] = src[k]; });
      out.push(e);
    });
    return out;
  }

  function recColumns() {
    var game = rec.tab === "game", league = rec.tab === "league";
    // The league is the whole entity, so it gets no name column at all.
    var who = league ? [] : [["name", game ? "Game" : "Team"]];
    if (rec.season === "all") who.push(["s", "Season"]);
    who.push(["n", "SN"]);
    var stats = rec.side === "bat" ? REC_BAT.slice() : REC_PIT.slice();
    var extra = [];
    if (rec.tab === "team") extra = [["w", "W"], ["l", "L"]].concat(rec.side === "bat" ? [["rs", "RS"]] : [["ra", "RA"]]);
    if (league) extra = [rec.side === "bat" ? ["rs", "RS"] : ["ra", "RA"]];
    if (game) stats = stats.filter(function (c) { return c[0] !== "g" && c[0] !== "wpa"; });
    else stats = [stats[0]].concat(extra, stats.slice(1));
    return who.concat(stats);
  }

  function recRows() {
    var rows = rec.tab === "game" ? gameRows() : teamRows();
    rows.forEach(function (r) { rec.side === "bat" ? withBatRates(r) : withPitRates(r); });
    return rows;
  }

  function renderRecTable() {
    var box = rec.el;
    if (!box) return;
    var cols = recColumns();
    if (!cols.some(function (c) { return c[0] === rec.sort.key; })) rec.sort = defaultRecSort(rec.side);
    var rows = recRows();
    var skey = rec.sort.key === "ip" ? "outs" : rec.sort.key;
    rows = sortRows(rows, { key: skey, dir: rec.sort.dir });
    var shown = rows.slice(0, rec.shown);
    var head = '<th class="stats-rk">RK</th>' + sortHead("data-rsk", cols, rec.sort);
    var ranks = [], prev;
    rows.forEach(function (r, i) {
      var v = skey === "name" ? String(r.name || "") : r[skey];
      if (i === 0 || v !== prev) { ranks.push(String(i + 1)); prev = v; } else ranks.push("");
    });
    var body = shown.map(function (r, ri) {
      var nav = ' class="xt-click" data-rseason="' + r.s + '" data-rsession="' + r.n + '"' + (r.gc ? ' data-rgame="' + esc(r.gc) + '"' : "");
      return "<tr" + nav + '><td class="stats-rk">' + ranks[ri] + "</td>" + cols.map(function (c) {
        var k = c[0];
        if (k === "name") return '<td class="stats-col-name">' + esc(r.name) + "</td>";
        if (k === "s") return "<td>S" + r.s + "</td>";
        if (k === "n") return "<td>" + r.n + "</td>";
        var v = r[k];
        if (k === "ip") return "<td>" + fmtIP(r.outs || 0) + "</td>";
        if (v == null) return "<td>" + (c[2] != null ? "-" : "0") + "</td>";
        return "<td>" + (k === "wpa" && v > 0 ? "+" : "") + cell(v, c[2], k) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    var more = rows.length > shown.length
      ? '<div class="xt-bar"><button type="button" class="chip" id="rec-more">Show ' + Math.min(REC_PAGE, rows.length - shown.length) + " more (" +
        shown.length + " of " + rows.length.toLocaleString() + ")</button></div>" : "";
    var drawn = rows.length;
    box.innerHTML = rows.length
      ? '<div class="stats-table-wrap stats-agg xt-table"><table class="stats-table xt-plain"><thead><tr>' + head +
        "</tr></thead><tbody>" + body + "</tbody></table></div>" + more
      : '<div class="stats-empty">No ' + (rec.tab === "game" ? "games" : rec.tab === "league" ? "league lines" : "team lines") + " match.</div>";
    // the name column (Team / Game) sticks just right of RK on a phone; RK's width is its offset
    var rk = box.querySelector("th.stats-rk");
    if (rk) document.documentElement.style.setProperty("--stats-rk-w", rk.getBoundingClientRect().width + "px");
  }

  /* Called by the Stats tab for the Team / Game / League granularities. ctx: {gran, side, allTime, season,
     session, gt, team}. Fetches the records data on first use, then draws the table into el. */
  function renderGran(el, ctx) {
    var sig = rec.tab + rec.side + rec.gt + rec.season + rec.session + rec.team + rec.allTime + rec.rookies + rec.sRange;
    rec.tab = ctx.gran; rec.rookies = !!ctx.rookies; rec.sRange = ctx.allTime && ctx.seasons ? ctx.seasons : null; rec.side = ctx.side;
    rec.sort = rec.sorts[rec.tab + rec.side] || defaultRecSort(rec.side); rec.gt = ctx.gt || "all"; rec.allTime = !!ctx.allTime;
    rec.season = ctx.allTime ? "all" : String(ctx.season); rec.session = ctx.allTime ? null : ctx.session;
    rec.team = ctx.team || "";
    if (sig !== rec.tab + rec.side + rec.gt + rec.season + rec.session + rec.team + rec.allTime + rec.rookies + rec.sRange) rec.shown = REC_PAGE;
    rec.el = el;
    var needGames = rec.tab === "game";
    if (!rec.data || (needGames && !rec.games) || (rec.allTime && !at.fr)) {
      var tok = ++rec.token;
      var jobs = [rec.data ? 0 : getJ("data/records.json").then(function (d) { rec.data = d; })];
      if (needGames && !rec.games) jobs.push(getJ("data/records_games.json").then(function (d) { rec.games = d.games; }));
      if (rec.allTime) jobs.push(loadNames());
      Promise.all(jobs).then(function () { if (tok === rec.token && document.body.contains(el)) renderRecTable(); })
        .catch(function () { el.innerHTML = '<div class="stats-empty">Could not load these stats.</div>'; });
      return;
    }
    renderRecTable();
  }

  function wireRecords() {
    var el = $("stats-view");
    el.addEventListener("click", function (e) {
      if (!rec.el || !rec.el.contains(e.target)) return;
      var t = e.target.closest("[data-rsk]");
      if (t) {
        rec.sort = nextSort(rec.sort, t.getAttribute("data-rsk"));
        rec.sorts[rec.tab + rec.side] = rec.sort;
        rec.shown = REC_PAGE; renderRecTable(); return;
      }
      if (e.target.closest("#rec-more")) { rec.shown += REC_PAGE; renderRecTable(); return; }
      t = e.target.closest("tr[data-rseason]");
      if (t) {
        var s = Number(t.getAttribute("data-rseason")), n = Number(t.getAttribute("data-rsession")), g = t.getAttribute("data-rgame");
        if (g) B.goTo(s, n, { gameCode: g });
        else B.goTo(s, n, { view: "stats" });
      }
    });
  }

  // ── every session line, as the Stats tab shows it (the All-Time Player view) ──
  var sa = null, saLoading = null;

  function loadStatsAllTime() {
    if (sa) return Promise.resolve(sa);
    if (!saLoading) {
      saLoading = Promise.all([getJ("data/career/stats_sessions.json"), loadNames()]).then(function (res) {
        var d = res[0];
        var build = function (list, keys) {
          return list.map(function (r) {
            var o = { player_id: r[0], name: d.n[r[0]], season: r[1], session: r[2], po: r[3], team: r[4] };
            for (var i = 0; i < keys.length; i++) if (r[5 + i] != null) o[keys[i]] = r[5 + i];
            o.award = r[5 + keys.length];
            o.game_award = r[6 + keys.length];
            if (r[7 + keys.length]) o.rookie = 1;
            return o;
          });
        };
        sa = { bat: build(d.bat, d.keys.bat), pit: build(d.pit, d.keys.pit) };
        return sa;
      }).catch(function (e) { saLoading = null; throw e; });
    }
    return saLoading;
  }

  function statsAllTime(isBatting, gt) {
    if (!sa) return null;
    var list = isBatting ? sa.bat : sa.pit;
    return gt === "all" ? list : list.filter(function (r) { return gtOk(gt, r.po); });
  }

  function franchiseAbbr(abbr) { return at.fr && at.fr[abbr] ? at.fr[abbr].abbr : abbr; }
  function franchiseList() { return (at.franchises || []).map(function (f) { return { abbr: f.abbr, name: f.name }; }); }

  // ── All-Time (Plays tab, "All-Time" in the season/session dropdown) ──────────────
  // The same filters as the normal feed, but over every play of every season. The
  // panel fetches nothing until Apply; a play index per season (data/search/sNN.json)
  // is then scanned in the browser and the matches listed in a compact sortable table.

  var RESULT_GROUPS = [
    ["g:hit", "All hits", ["1B", "1BWH", "1BWH2", "2B", "2BWH", "3B", "B1B", "B1BWH", "HR", "IF1B"]],
    ["g:hr", "Home runs", ["HR"]],
    ["g:k", "Strikeouts", ["K", "AutoK", "KCS"]],
    ["g:bb", "Walks", ["BB", "IBB", "AutoBB"]],
    ["g:dp", "Double plays", ["DP", "DP21", "DP31", "DPH1", "DPRun", "LODP"]],
    ["g:sb", "Stolen bases", ["SB", "SB2", "SB3", "SB4", "SB32", "SB42", "SB43", "SB432"]],
    ["g:cs", "Caught stealing", ["CS", "CS2", "CS3", "CS4"]],
  ];
  // Same 0-7 order as the Plays tab's on-base chips (app.js OBC_ORDER).
  var OBC_ORDER = ["000", "001", "010", "100", "011", "101", "110", "111"];
  var OBC_LABELS = {
    "000": "Bases empty", "001": "Runner on 1st", "010": "Runner on 2nd", "100": "Runner on 3rd",
    "011": "1st & 2nd", "101": "1st & 3rd", "110": "2nd & 3rd", "111": "Bases loaded",
  };

  var SEARCH_PAGE = 100;
  var SEARCH_CAP = 30000;     // matches above this ask the user to narrow first
  var SLIDESHOW_MAX = 30;     // plays in the slideshow, taken from the top of the sorted results

  // The range sliders: [min, max, step, whether the top end means "and up"].
  var SLIDERS = {
    seasons: { label: "Seasons", step: 1 },
    inning: { label: "Inning", min: 1, max: 9, step: 1, plus: true },
    diff: { label: "Diff", min: 0, max: 500, step: 5 },
    lev: { label: "Leverage", min: 0, max: 6, step: 0.1, plus: true },
    wpa: { label: "WPA", min: 0, max: 100, step: 1, unit: "%" },
  };

  var at = { names: null, form: null, results: null, shown: 0, sortKey: "season", sortDir: "desc", loading: false, note: "", total: 0, capped: false, el: null };

  function allSeasons() { return B.seasonList(); }

  function sliderBounds(k) {
    if (k === "seasons") { var s = allSeasons(); return [s[s.length - 1], s[0]]; }
    return [SLIDERS[k].min, SLIDERS[k].max];
  }

  function defaultForm() {
    var f = {
      side: "all", type: "", km: true, rookies: false, favs: false, team: "", batter: "", pitcher: "", result: "",
      outs: null, obc: null, tags: [], gt: "all",
    };
    Object.keys(SLIDERS).forEach(function (k) { f[k] = sliderBounds(k).slice(); });
    return f;
  }

  // ── shareable links: ?search=<filters> (All-Time) and ?player=<id>[&side=pit][&pview=diffs] ──
  function b64enc(o) {
    return btoa(unescape(encodeURIComponent(JSON.stringify(o)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64dec(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    return JSON.parse(decodeURIComponent(escape(atob(s))));
  }
  function baseUrl() { var u = new URL(location.href); u.search = ""; u.hash = ""; return u; }

  // Only the filters that differ from a fresh form go in the link, so a default search is a short link and a new
  // season later doesn't pin the Seasons slider.
  function searchLinkUrl() {
    var def = defaultForm(), diff = {};
    Object.keys(at.form).forEach(function (k) {
      if (JSON.stringify(at.form[k]) !== JSON.stringify(def[k])) diff[k] = at.form[k];
    });
    var u = baseUrl();
    u.searchParams.set("search", b64enc(diff));
    return u.toString();
  }

  // Called by the app when it opens with ?search=...: load those filters, and run the search once the form is drawn.
  function setSearchFromLink(code) {
    var parsed;
    try { parsed = b64dec(code); } catch (e) { return false; }
    if (!parsed || typeof parsed !== "object") return false;
    var f = defaultForm();
    Object.keys(f).forEach(function (k) { if (parsed[k] !== undefined && (f[k] === null || typeof parsed[k] === typeof f[k])) f[k] = parsed[k]; });
    if (!Array.isArray(f.tags)) f.tags = [];
    at.form = f;
    at.autoRun = true;
    return true;
  }

  function playerLinkUrl() {
    var u = baseUrl();
    u.searchParams.set("player", String(pm.id));
    if (pm.side === "pit") u.searchParams.set("side", "pit");
    if (pm.view === "diffs") u.searchParams.set("pview", "diffs");
    return u.toString();
  }

  function loadNames() {
    if (at.names) return Promise.resolve(at.names);
    return getJ("data/search/names.json").then(function (n) {
      // A copy cached from before the franchise list existed: fetch it fresh once.
      if (n.franchises) return n;
      return fetch("data/search/names.json?v=" + Date.now(), { cache: "no-store" }).then(function (r) { return r.json(); });
    }).then(function (n) {
      at.names = n.n; at.teams = n.teams; at.franchises = n.franchises || [];
      // abbreviation -> its franchise (the team it is today), for the filter and the results table
      at.fr = {};
      at.franchises.forEach(function (fr) { fr.abbrs.forEach(function (a) { at.fr[a] = fr; }); });
      return n;
    });
  }

  function chip(label, attrs, on, extra) {
    return '<button type="button" class="chip' + (extra ? " " + extra : "") + (on ? " active" : "") + '" ' + attrs + ">" + label + "</button>";
  }

  function sliderLabel(k, lo, hi) {
    var d = SLIDERS[k], b = sliderBounds(k);
    if (k === "seasons") return "S" + lo + " - S" + hi;
    var top = d.plus && hi >= b[1] ? hi + "+" : hi + (d.unit || "");
    return lo + (d.unit || "") + " - " + top;
  }

  function sliderHtml(k, f) {
    var d = SLIDERS[k], b = sliderBounds(k), v = f[k];
    var step = d.step;
    return '<div class="filter-group dr-group"><label class="group-label">' + d.label +
      ' <span class="dr-val" id="dr-val-' + k + '">' + sliderLabel(k, v[0], v[1]) + "</span></label>" +
      '<div class="dr" data-dr="' + k + '"><div class="dr-track"><div class="dr-fill"></div></div>' +
      '<input type="range" class="dr-a" min="' + b[0] + '" max="' + b[1] + '" step="' + step + '" value="' + v[0] + '" aria-label="' + d.label + ' from">' +
      '<input type="range" class="dr-b" min="' + b[0] + '" max="' + b[1] + '" step="' + step + '" value="' + v[1] + '" aria-label="' + d.label + ' to"></div></div>';
  }

  function allTimeHtml() {
    var f = at.form;
    var labels = B.resultLabels();
    var groupOpts = RESULT_GROUPS.map(function (g) {
      return '<option value="' + g[0] + '"' + (f.result === g[0] ? " selected" : "") + ">" + g[1] + "</option>";
    }).join("");
    var codeOpts = Object.keys(labels).sort().map(function (c) {
      return '<option value="c:' + esc(c) + '"' + (f.result === "c:" + c ? " selected" : "") + ">" + esc(c) + " - " + esc(labels[c]) + "</option>";
    }).join("");
    var teamOpts = '<option value="">All teams</option>' + at.franchises.map(function (fr) {
      return '<option value="' + esc(fr.abbr) + '"' + (f.team === fr.abbr ? " selected" : "") + ">" + esc(fr.name) + "</option>";
    }).join("");
    var svgs = B.basesSvg();
    var obc = OBC_ORDER.map(function (o) {
      return '<button type="button" class="chip obc-chip' + (f.obc === o ? " active" : "") + '" data-obc="' + o + '" title="' + esc(OBC_LABELS[o]) + '">' + (svgs[o] || o) + "</button>";
    }).join("");
    var tagLabels = B.tagLabels();
    var tags = Object.keys(tagLabels).map(function (slug) {
      return chip(esc(tagLabels[slug]), 'data-tag="' + esc(slug) + '"', f.tags.indexOf(slug) !== -1, "multi");
    }).join("");
    var textf = function (id, ph, val) {
      return '<div class="search-input-wrap"><input type="text" id="' + id + '" placeholder="' + ph + '" autocomplete="off" value="' + esc(val || "") + '"></div>';
    };
    return '<div class="filters-card alltime-card" id="at-card"><div class="filters-body">' +
      // side / type / show
      '<div class="filter-row">' +
        '<div class="filter-group"><label class="group-label">Side</label><div class="chip-row" id="at-side">' +
          [["all", "All"], ["for", "For"], ["against", "Against"]].map(function (s) {
            return chip(s[1], 'data-side="' + s[0] + '"', f.side === s[0]);
          }).join("") + "</div></div>" +
        '<div class="filter-group"><label class="group-label">Type</label><div class="chip-row" id="at-type">' +
          chip("Hitting", 'data-type="hitting"', f.type === "hitting") + chip("Pitching", 'data-type="pitching"', f.type === "pitching") + "</div></div>" +
        '<div class="filter-group"><label class="group-label">Show</label><div class="chip-row" id="at-show">' +
          chip("Key Moments", 'data-show="km"', f.km, "toggle") + chip("Rookies", 'data-show="rookies"', f.rookies, "toggle") +
          chip("Favorites", 'data-show="favs"', f.favs, "toggle") + "</div></div>" +
        '<div class="filter-group"><label class="group-label">Game type</label><div class="chip-row" id="at-gt">' +
        [["all", "All"], ["reg", "Regular"], ["post", "Postseason"]].map(function (g) { return chip(g[1], 'data-gt="' + g[0] + '"', f.gt === g[0]); }).join("") + "</div></div>" +
      "</div>" +
      // team / players
      '<div class="filter-row fields-row">' +
        '<div class="filter-group"><label class="group-label">Team</label><select id="at-team">' + teamOpts + "</select></div>" +
        '<div class="filter-group player-search-group"><label class="group-label">Batter</label>' + textf("at-batter", "Player name", f.batter) + "</div>" +
        '<div class="filter-group player-search-group"><label class="group-label">Pitcher</label>' + textf("at-pitcher", "Player name", f.pitcher) + "</div>" +
      "</div>" +
      // result / outs / bases
      '<div class="filter-row fields-row">' +
        '<div class="filter-group"><label class="group-label">Result</label><select id="at-result"><option value="">Any result</option>' +
          '<optgroup label="Groups">' + groupOpts + '</optgroup><optgroup label="Exact result">' + codeOpts + "</optgroup></select></div>" +
        '<div class="filter-group compact"><label class="group-label">Outs (before)</label><div class="chip-row" id="at-outs">' +
          [0, 1, 2].map(function (o) { return chip(o, 'data-outs="' + o + '"', f.outs === o); }).join("") + "</div></div>" +
        '<div class="filter-group compact"><label class="group-label">On base (before)</label><div class="chip-row obc-chip-row" id="at-obc">' + obc + "</div></div>" +
      "</div>" +
      '<div class="filter-row"><div class="filter-group wide"><label class="group-label">Tags <span class="group-hint">any selected tag matches</span></label>' +
        '<div class="chip-row wrap" id="at-tags">' + tags + "</div></div></div>" +
      // game type + sliders
      '<div class="filter-row dr-row">' + ["seasons", "inning", "diff", "lev", "wpa"].map(function (k) { return sliderHtml(k, f); }).join("") + "</div>" +
      '<div class="filters-footer"><div class="count-text" id="at-count">Set your filters, then press Apply.</div>' +
        '<div class="footer-actions"><button type="button" class="chip active" id="at-apply">Apply</button>' +
        '<button type="button" class="link-btn" id="at-copy" title="Copy a link that applies these filters">Copy link</button>' +
        '<button type="button" class="link-btn" id="at-reset">&#8635; Reset filters</button></div></div>' +
      "</div></div>" +
      '<div id="at-out"></div>';
  }

  function renderAllTime(el) {
    at.el = el;
    if (!at.form) at.form = defaultForm();
    loadNames().then(function () {
      if (!$("at-card")) {
        el.innerHTML = allTimeHtml();
        wireAllTime();
        renderAllTimeOut();
        if (at.autoRun) { at.autoRun = false; applyAllTime(); }
      }
    }).catch(function () { el.innerHTML = '<div class="stats-empty">Could not load the play index.</div>'; });
  }

  function drPaint(dr) {
    var a = dr.querySelector(".dr-a"), b = dr.querySelector(".dr-b");
    var min = Number(a.min), max = Number(a.max);
    var fill = dr.querySelector(".dr-fill");
    fill.style.left = ((Number(a.value) - min) / (max - min) * 100) + "%";
    fill.style.right = (100 - (Number(b.value) - min) / (max - min) * 100) + "%";
  }

  function wireAllTime() {
    var f = at.form;
    var group = function (id, attr, set) {
      $(id).addEventListener("click", function (e) {
        var c = e.target.closest("[" + attr + "]");
        if (!c) return;
        set(c.getAttribute(attr), c);
        Array.prototype.forEach.call($(id).querySelectorAll("[" + attr + "]"), function (x) {
          x.classList.toggle("active", isOn(x));
        });
      });
    };
    var isOn = function (x) {
      if (x.hasAttribute("data-side")) return f.side === x.getAttribute("data-side");
      if (x.hasAttribute("data-type")) return f.type === x.getAttribute("data-type");
      if (x.hasAttribute("data-show")) return !!f[x.getAttribute("data-show")];
      if (x.hasAttribute("data-outs")) return f.outs === Number(x.getAttribute("data-outs"));
      if (x.hasAttribute("data-obc")) return f.obc === x.getAttribute("data-obc");
      if (x.hasAttribute("data-tag")) return f.tags.indexOf(x.getAttribute("data-tag")) !== -1;
      if (x.hasAttribute("data-gt")) return f.gt === x.getAttribute("data-gt");
      return false;
    };
    group("at-side", "data-side", function (v) { if (!$("at-side").classList.contains("off")) f.side = v; });
    group("at-type", "data-type", function (v) { f.type = f.type === v ? "" : v; });
    group("at-show", "data-show", function (v) { f[v] = !f[v]; syncSide(); });
    group("at-outs", "data-outs", function (v) { v = Number(v); f.outs = f.outs === v ? null : v; });
    group("at-obc", "data-obc", function (v) { f.obc = f.obc === v ? null : v; });
    group("at-tags", "data-tag", function (v) {
      var i = f.tags.indexOf(v);
      if (i === -1) f.tags.push(v); else f.tags.splice(i, 1);
    });
    group("at-gt", "data-gt", function (v) { f.gt = v; });
    $("at-team").addEventListener("change", function () { f.team = this.value; syncSide(); });
    $("at-result").addEventListener("change", function () { f.result = this.value; });
    // For / Against only means something once a side-sensitive filter (team, rookies, favorites) is on,
    // exactly like the Plays tab - otherwise it is disabled and reads as All.
    function sideActive() { return !!(f.team || f.rookies || f.favs); }
    function syncSide() {
      var on = sideActive();
      if (!on) f.side = "all";
      Array.prototype.forEach.call($("at-side").querySelectorAll(".chip"), function (c) {
        c.disabled = !on;
        c.classList.toggle("active", f.side === c.getAttribute("data-side"));
      });
      $("at-side").classList.toggle("off", !on);
    }
    syncSide();
    // range sliders
    Array.prototype.forEach.call(at.el.querySelectorAll(".dr"), function (dr) {
      var k = dr.getAttribute("data-dr"), a = dr.querySelector(".dr-a"), b = dr.querySelector(".dr-b");
      drPaint(dr);
      var on = function (which) {
        return function () {
          var lo = Number(a.value), hi = Number(b.value);
          if (lo > hi) { if (which === "a") { lo = hi; a.value = lo; } else { hi = lo; b.value = hi; } }
          f[k] = [lo, hi];
          $("dr-val-" + k).textContent = sliderLabel(k, lo, hi);
          drPaint(dr);
        };
      };
      a.addEventListener("input", on("a"));
      b.addEventListener("input", on("b"));
    });
    // batter / pitcher text + name suggestions
    [["at-batter", "batter"], ["at-pitcher", "pitcher"]].forEach(function (pair) {
      var input = $(pair[0]);
      var box = document.createElement("div");
      box.className = "player-suggest";
      box.hidden = true;
      input.parentNode.parentNode.appendChild(box);
      input.addEventListener("input", function () {
        f[pair[1]] = input.value.trim();
        var needle = input.value.trim().toLowerCase();
        if (needle.length < 2) { box.hidden = true; return; }
        var hits = Object.keys(at.names).filter(function (k) { return at.names[k].toLowerCase().indexOf(needle) !== -1; }).slice(0, 8);
        box.innerHTML = hits.length ? hits.map(function (k) {
          return '<div class="player-suggest-row" data-name="' + esc(at.names[k]) + '">' + esc(at.names[k]) + "</div>";
        }).join("") : '<div class="player-suggest-empty">No players match.</div>';
        box.hidden = false;
      });
      box.addEventListener("mousedown", function (e) {
        var row = e.target.closest(".player-suggest-row");
        if (row) { e.preventDefault(); input.value = row.getAttribute("data-name"); f[pair[1]] = input.value; box.hidden = true; }
      });
      input.addEventListener("blur", function () { setTimeout(function () { box.hidden = true; }, 120); });
    });
    $("at-apply").addEventListener("click", applyAllTime);
    $("at-copy").addEventListener("click", function () { B.copyLink(searchLinkUrl()); });
    $("at-card").addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type === "text") { e.preventDefault(); applyAllTime(); } });
    $("at-reset").addEventListener("click", function () {
      at.form = defaultForm(); at.results = null; at.note = ""; at.capped = false;
      at.el.innerHTML = allTimeHtml();
      wireAllTime();
      renderAllTimeOut();
    });
  }

  function idsMatching(text) {
    if (!text) return null;
    var needle = text.toLowerCase(), ids = {};
    Object.keys(at.names).forEach(function (id) {
      if (at.names[id].toLowerCase().indexOf(needle) !== -1) ids[id] = 1;
    });
    return ids;
  }

  function setCount(msg) { at.note = msg; var n = $("at-count"); if (n) n.textContent = msg; }

  function applyAllTime() {
    if (at.loading) return;
    var f = at.form;
    var seasons = allSeasons().filter(function (s) { return s >= f.seasons[0] && s <= f.seasons[1]; });
    var batterIds = idsMatching(f.batter), pitcherIds = idsMatching(f.pitcher);
    var resSet = null;
    if (f.result) {
      resSet = f.result.indexOf("g:") === 0 ? RESULT_GROUPS.filter(function (g) { return g[0] === f.result; })[0][2] : [f.result.slice(2)];
    }
    var fr = f.team ? at.franchises.filter(function (x) { return x.abbr === f.team; })[0] : null;
    var rng = function (k) {
      var b = sliderBounds(k), v = f[k];
      // a slider left at its end means "no limit" on that side (the top end of Inning/Leverage/Runs is "and up")
      return { lo: v[0] > b[0] ? v[0] : null, hi: v[1] < b[1] ? v[1] : null };
    };
    var inn = rng("inning"), dif = rng("diff"), lev = rng("lev"), wpa = rng("wpa");
    var sideSens = [];
    if (fr) sideSens.push("team");
    if (f.rookies) sideSens.push("rk");
    if (f.favs) sideSens.push("fav");

    at.loading = true;
    $("at-apply").disabled = true;
    at.results = null; at.capped = false;
    renderAllTimeOut();
    setCount("Loading " + seasons.length + " season" + (seasons.length === 1 ? "" : "s") + "...");

    Promise.all(seasons.map(function (s) { return getJ("data/search/s" + pad2(s) + ".json"); })).then(function (files) {
      var hits = [], total = 0;
      files.forEach(function (file) {
        var c = file.cols, D = file.dict, n = file.n;
        var rIdx = null, bIdx = -2, tIdx = null, tagMask = 0;
        if (resSet) { rIdx = {}; D.r.forEach(function (code, i) { if (resSet.indexOf(code) !== -1) rIdx[i] = 1; }); }
        if (f.obc) bIdx = D.b.indexOf(f.obc);
        if (fr) { tIdx = {}; D.ot.forEach(function (a, i) { if (fr.abbrs.indexOf(a) !== -1) tIdx[i] = 1; }); }
        f.tags.forEach(function (t) { var i = (D.tg || []).indexOf(t); if (i !== -1) tagMask |= (1 << i); });
        if (bIdx === -1) return;
        if (f.tags.length && !tagMask) return;
        if (fr && !Object.keys(tIdx).length) return;
        for (var i = 0; i < n; i++) {
          if (f.km && !c.km[i]) continue;
          if (f.gt !== "all" && (f.gt === "post") !== !!c.po[i]) continue;
          if (f.type && (f.type === "hitting") !== !!c.ca[i]) continue;
          if (rIdx && !rIdx[c.r[i]]) continue;
          if (batterIds && !batterIds[c.bi[i]]) continue;
          if (pitcherIds && !pitcherIds[c.pi[i]]) continue;
          if (f.outs != null && c.o[i] !== f.outs) continue;
          if (bIdx >= 0 && c.b[i] !== bIdx) continue;
          if (tagMask && !(c.tg[i] & tagMask)) continue;
          if (inn.lo != null && c.in[i] < inn.lo) continue;
          if (inn.hi != null && c.in[i] > inn.hi) continue;
          if (dif.lo != null || dif.hi != null) {
            if (c.d[i] < 0) continue;
            if (dif.lo != null && c.d[i] < dif.lo) continue;
            if (dif.hi != null && c.d[i] > dif.hi) continue;
          }
          if (lev.lo != null && c.lv[i] < lev.lo * 100) continue;
          if (lev.hi != null && c.lv[i] > lev.hi * 100) continue;
          var aw = Math.abs(c.w[i]);
          if (wpa.lo != null && aw < wpa.lo * 10) continue;
          if (wpa.hi != null && aw > wpa.hi * 10) continue;
          if (sideSens.length) {
            // Which side of the play each filter matches: F = the featured player's side, C = the other one.
            var okF = true, okC = true, okAny = true;
            for (var q = 0; q < sideSens.length; q++) {
              var F, C, kind = sideSens[q];
              if (kind === "team") {
                var ft = c.fo[i] ? c.ot[i] : c.dt[i], ct = c.fo[i] ? c.dt[i] : c.ot[i];
                F = !!tIdx[ft]; C = !!tIdx[ct];
              } else if (kind === "rk") { F = !!(c.rk[i] & 1); C = !!(c.rk[i] & 2); }
              else { F = B.isFavorite(c.fi[i]); C = B.isFavorite(c.ci[i]); }
              okF = okF && F; okC = okC && C; okAny = okAny && (F || C);
            }
            if (f.side === "for" ? !okF : f.side === "against" ? !okC : !okAny) continue;
          }
          total++;
          if (hits.length >= SEARCH_CAP) continue;
          hits.push({
            po: c.po[i], s: file.season, n: c.n[i], g: D.g[c.g[i]], pn: c.pn[i], inn: c.in[i], half: c.hf[i], o: c.o[i], b: D.b[c.b[i]],
            r: D.r[c.r[i]], d: c.d[i], bi: c.bi[i], pi: c.pi[i], ot: D.ot[c.ot[i]], dt: D.ot[c.dt[i]],
            w: c.w[i] / 10, lv: c.lv[i] / 100,
          });
        }
      });
      at.total = total;
      at.capped = total > SEARCH_CAP;
      at.results = at.capped ? null : hits;
      at.shown = SEARCH_PAGE;
      at.sortKey = "season";
      at.sortDir = "desc";
      at.loading = false;
      $("at-apply").disabled = false;
      setCount(at.capped ? total.toLocaleString() + " plays match" : total.toLocaleString() + " play" + (total === 1 ? "" : "s"));
      renderAllTimeOut();
    }).catch(function () {
      at.loading = false;
      $("at-apply").disabled = false;
      setCount("Could not load one of the season files.");
    });
  }

  // Column headers sort the results (click again to flip). Numeric columns
  // start high-to-low, text columns A-Z.
  var SR_COLS = [
    ["season", "Season", "num"], ["n", "Sn", "num"], ["inn", "Inn", "num"], ["o", "Outs", "num"], ["b", "Bases", "txt"],
    ["batter", "Batter", "txt"], ["pitcher", "Pitcher", "txt"], ["r", "Result", "txt"], ["d", "Diff", "num"],
    ["w", "WPA", "num"], ["lv", "Lev", "num"],
  ];

  function sortResults() {
    var key = at.sortKey, dir = at.sortDir === "asc" ? 1 : -1;
    var val = function (r) {
      switch (key) {
        case "batter": return nameOf(r.bi);
        case "pitcher": return nameOf(r.pi);
        case "inn": return r.inn * 2 + r.half;
        default: return r[key];
      }
    };
    at.results.sort(function (a, b) {
      var c;
      if (key === "season") c = a.s - b.s || a.n - b.n || a.pn - b.pn;
      else {
        var av = val(a), bv = val(b);
        c = typeof av === "string" ? av.localeCompare(bv) : av - bv;
        if (!c) c = a.s - b.s || a.n - b.n || a.pn - b.pn;
      }
      return c * dir;
    });
  }

  function nameOf(id) { return (at.names && at.names[id]) || (id ? "Player " + id : ""); }

  // A team as it is today: an old abbreviation (CIN, KYM, ...) is shown as its franchise's current one,
  // with the current full name on hover.
  function teamTag(abbr) {
    var fr = at.fr && at.fr[abbr];
    return '<span class="xt-tm" title="' + esc(fr ? fr.name : abbr) + '">' + esc(fr ? fr.abbr : abbr) + "</span>";
  }

  function renderAllTimeOut() {
    var out = $("at-out");
    if (!out) return;
    if (at.loading) { out.innerHTML = ""; return; }
    if (at.capped) {
      out.innerHTML = '<div class="stats-empty">More than ' + (SEARCH_CAP / 1000) + "K rows match. Narrow the filters and press Apply again.</div>";
      return;
    }
    if (!at.results) {
      out.innerHTML = "";
      return;
    }
    var rows = at.results;
    if (!rows.length) { out.innerHTML = '<div class="stats-empty">No plays match these filters.</div>'; return; }
    sortResults();
    var labels = B.resultLabels();
    var svgs = B.basesSvg();
    var mix = {};
    rows.forEach(function (r) { mix[r.r] = (mix[r.r] || 0) + 1; });
    var top = Object.keys(mix).sort(function (a, b) { return mix[b] - mix[a]; }).slice(0, 6).map(function (k) { return esc(k) + " " + mix[k]; }).join(" &middot; ");
    var shown = rows.slice(0, at.shown);
    var playN = Math.min(SLIDESHOW_MAX, rows.length);
    var html = '<div class="xt-bar"><button type="button" class="tile-replay-btn play-filtered-btn" id="at-play" title="Play these plays" aria-label="Play these plays">' +
      '<svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="8 5 19 12 8 19 8 5"></polygon></svg></button>' +
      "<b>" + rows.length.toLocaleString() + " plays</b>" +
      (rows.length > playN ? '<span class="xt-hint">slideshow plays the first ' + playN + " as sorted</span>" : "") +
      '<span class="xt-hint">' + top + "</span></div>";
    html += '<div class="stats-table-wrap xt-table"><table class="stats-table xt-plain"><thead><tr>' + SR_COLS.map(function (c) {
      var on = at.sortKey === c[0];
      return '<th class="xt-sortable' + (on ? " active" : "") + '" data-sk="' + c[0] + '">' + c[1] + (on ? (at.sortDir === "asc" ? " &#9650;" : " &#9660;") : "") + "</th>";
    }).join("") + "</tr></thead><tbody>" +
      shown.map(function (r, i) {
        return '<tr class="xt-click" data-i="' + i + '" title="Open this play"><td>S' + r.s + "</td><td>" + r.n + (r.po ? " PS" : "") + "</td><td>" + (r.half ? "&#9660;" : "&#9650;") + r.inn + "</td><td>" + r.o +
          "</td><td class=\"xt-obc\" title=\"" + esc(OBC_LABELS[r.b] || r.b) + "\">" + (svgs[r.b] || esc(r.b)) + '</td><td><button type="button" class="xt-link" data-sp="' + r.bi + '">' + esc(nameOf(r.bi)) + "</button> " + teamTag(r.ot) + "</td>" +
          '<td><button type="button" class="xt-link" data-sp="' + r.pi + '">' + esc(nameOf(r.pi)) + "</button> " + teamTag(r.dt) + "</td>" +
          '<td title="' + esc(labels[r.r] || "") + '">' + esc(r.r) + "</td><td>" + (r.d < 0 ? "-" : r.d) + "</td><td>" +
          (r.w > 0 ? "+" : "") + r.w.toFixed(1) + "</td><td>" + r.lv.toFixed(2) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    if (rows.length > shown.length) {
      html += '<div class="xt-bar"><button type="button" class="chip" id="at-more">Show ' + Math.min(SEARCH_PAGE, rows.length - shown.length) + " more (" +
        shown.length + " of " + rows.length.toLocaleString() + ")</button></div>";
    }
    out.innerHTML = html;
    var more = $("at-more");
    if (more) more.addEventListener("click", function () { at.shown += SEARCH_PAGE; renderAllTimeOut(); });
    $("at-play").addEventListener("click", function () {
      B.playSlideshow(rows.slice(0, SLIDESHOW_MAX).map(function (r) { return { s: r.s, n: r.n, g: r.g, pn: r.pn }; }));
    });
    out.querySelector("table").addEventListener("click", function (e) {
      var th = e.target.closest("th[data-sk]");
      if (th) {
        var k = th.getAttribute("data-sk");
        if (at.sortKey === k) at.sortDir = at.sortDir === "asc" ? "desc" : "asc";
        else {
          at.sortKey = k;
          at.sortDir = SR_COLS.filter(function (c) { return c[0] === k; })[0][2] === "txt" ? "asc" : "desc";
        }
        at.shown = SEARCH_PAGE;
        renderAllTimeOut();
        return;
      }
      var pl = e.target.closest("[data-sp]");
      if (pl) { var id = Number(pl.getAttribute("data-sp")); if (id) openPlayer(id, nameOf(id)); return; }
      var tr = e.target.closest("tr[data-i]");
      if (tr) {
        var r = shown[Number(tr.getAttribute("data-i"))];
        B.openPlay({ s: r.s, n: r.n, g: r.g, pn: r.pn });
      }
    });
  }

  // ── public ──────────────────────────────────────────────────────────────────────

  var wired = false;
  window.MLNExtras = {
    init: function (bridge) {
      B = bridge;
      if (!wired) { wireRecords(); wired = true; }
    },
    render: function (view, el) {
      if (view === "alltime") renderAllTime(el);
    },
    renderGran: renderGran,
    statsAllTime: statsAllTime,
    loadStatsAllTime: loadStatsAllTime,
    franchiseAbbr: franchiseAbbr,
    franchises: franchiseList,
    statsAggregate: statsAggregate,
    aggCols: function (isBatting) { return isBatting ? AGG_BAT_COLS : AGG_PIT_COLS; },
    totalLine: totalLine,
    loadTotals: loadTotals,
    totalsSeasons: function () { return totals ? totals.seasons : null; },
    openPlayer: openPlayer,
    setSearchFromLink: setSearchFromLink,
  };
})();

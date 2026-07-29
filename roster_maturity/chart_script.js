(function () {
  const SEASONS = __SEASONS_JSON__;
  const PLAYERS = __PLAYERS_JSON__;
  const TEAM_TOTALS = __TEAM_TOTALS_JSON__;
  const LEAGUE_CAREER = __LEAGUE_CAREER_JSON__;

  // status colors (fixed across light/dark, matching the design system's status palette)
  const COLOR_NEW = "#0ca30c";        // good: arrived this season
  const COLOR_BOTH = "#fab219";       // warning: one-season stint (arrived and departed)
  const COLOR_TRADED = "#ff6700";     // orange: leaves this team, still active elsewhere next season
  const COLOR_RETIRED = "#d03b3b";    // critical: doesn't play anywhere next season
  const COLOR_STABLE = "#898781";     // neutral: continuing, neither arriving nor leaving

  function colorForStatus(status) {
    if (status === "new") return COLOR_NEW;
    if (status === "both") return COLOR_BOTH;
    if (status === "traded_away") return COLOR_TRADED;
    if (status === "retired") return COLOR_RETIRED;
    if (status === "departure") return COLOR_RETIRED; // simplified view: combines both/traded_away/retired
    return COLOR_STABLE;
  }

  function statusOf(p) {
    // "doesn't play anywhere next season" always wins the color, even for a first-year rookie
    if (p.leaving_league) return "retired";
    if (p.new_to_team && p.leaving_after) return "both";
    if (p.new_to_team) return "new";
    if (p.leaving_after) return "traded_away";
    return "stable";
  }

  // simplified-view toggle: collapses the three "leaving" statuses into one "departure" bucket
  let simplifiedView = false;
  function displayStatus(p) {
    const s = statusOf(p);
    if (simplifiedView && (s === "both" || s === "traded_away" || s === "retired")) return "departure";
    return s;
  }

  function statusLine(p) {
    if (p.leaving_league) {
      return p.new_to_team
        ? "Arrived this season; doesn't play anywhere next season (one-season stint)"
        : "Doesn't play anywhere next season (retired / out of the league)";
    }
    if (p.new_to_team && p.leaving_after) return "Arrived this season; moves to another team after it (one-season stint)";
    if (p.new_to_team) return "New to the team this season";
    if (p.leaving_after) return "Leaves this team after this season, still active elsewhere";
    if (p.leaving_unknown) return "Continuing with the team (current roster)";
    return "Continuing with the team, before and after";
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  const svgNS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) {
    const e = document.createElementNS(svgNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  const tooltip = document.getElementById("tooltip");
  function showTip(html, x, y) {
    tooltip.innerHTML = html;
    tooltip.style.left = x + "px";
    tooltip.style.top = (y - 12) + "px";
    tooltip.classList.add("show");
  }
  function hideTip() { tooltip.classList.remove("show"); }

  // ---------- state ----------
  let viewMode = "season"; // "season" | "team" | "player" | "league"
  let tenureMetric = "league"; // "league" (experience anywhere) | "franchise" (experience with this team)
  let movementTier = "rookie"; // "rookie" | "developing" | "vet" -- which tier the League movement chart shows
  let careerScope = "all"; // "all" | "active" | "retired_hiatus" -- which players the career distributions cover
  let seasonIdx = SEASONS.length - 1; // default to most recent season
  let sortField = "median"; // "median" | "pa" | "bf" | "ret" | "alpha"
  let sortDir = "desc"; // "desc" | "asc"
  let currentColumns = [];
  let selectedPlayerId = null;

  // a player's tenure under whichever metric is active
  function playerTenure(p) { return tenureMetric === "franchise" ? p.franchise_tenure : p.tenure; }
  // the tenure=0 tier reads "Rookie" (new to the league) under League, "New" (new to this team) under Franchise
  function tierZeroLabel() { return tenureMetric === "franchise" ? "New" : "Rookie"; }

  // team-season / TOTAL objects carry both metrics nested under `.metrics.league` / `.metrics.franchise` --
  // flatten the active one onto the object once, at construction time, so the rest of the rendering
  // code can keep reading col.median / col.q1 / col.retention_by_tenure etc. unchanged.
  function applyMetric(obj) {
    return Object.assign({}, obj, obj.metrics[tenureMetric]);
  }

  // same flattening for the per-season `league` object (used by the KPI row and the League tab)
  function activeLeague(seasonData) {
    return Object.assign({}, seasonData.league, seasonData.league.metrics[tenureMetric]);
  }

  const ALL_TEAM_NAMES = Array.from(new Set(
    SEASONS.flatMap(s => s.teams.map(t => t.name))
  )).sort();
  let selectedTeamName = ALL_TEAM_NAMES[0];

  const ALL_PLAYERS = Object.entries(PLAYERS)
    .map(([id, p]) => ({ id, name: p.name, seasons: p.history.length }))
    .sort((a, b) => a.name.localeCompare(b.name));

  function sortValue(team, field) {
    if (field === "median") return team.median;
    if (field === "pa") return team.pa_retention;
    if (field === "bf") return team.bf_retention;
    return team.combined_retention; // "ret"
  }

  function sortTeams(teams, field, dir) {
    const arr = teams.slice();
    if (field === "alpha") {
      arr.sort((a, b) => a.abbrev.localeCompare(b.abbrev));
    } else {
      arr.sort((a, b) => {
        const av = sortValue(a, field), bv = sortValue(b, field);
        const an = av == null ? -Infinity : av, bn = bv == null ? -Infinity : bv;
        return (an - bn) || (a.mean - b.mean);
      });
    }
    if (dir === "desc") arr.reverse();
    return arr;
  }

  // columns for "by team" mode: this franchise's entry in every season it appeared in, oldest -> newest,
  // plus a pinned TOTAL column pooling the franchise's whole history into a retention-by-tenure profile
  function columnsForTeam(teamName) {
    const cols = [];
    SEASONS.forEach(s => {
      const team = s.teams.find(t => t.name === teamName);
      if (team) cols.push(Object.assign({}, applyMetric(team), { season: s.league.season }));
    });
    const total = TEAM_TOTALS[teamName];
    if (total) cols.push(Object.assign({ isTotal: true }, applyMetric(total)));
    return cols;
  }

  // columns for "by player" mode: the FULL team-season roster distribution for every team/season
  // this player was part of, oldest -> newest (two columns for a season they were traded mid-year).
  // The player's own dot gets ringed and connected across columns inside renderChart.
  function columnsForPlayer(playerId) {
    const hist = PLAYERS[playerId].history;
    const cols = [];
    hist.forEach(entry => {
      const seasonTeams = SEASONS[entry.season - 1].teams;
      entry.teams.forEach(t => {
        const teamObj = seasonTeams.find(team => team.name === t.team);
        if (teamObj) cols.push(Object.assign({}, applyMetric(teamObj), { season: entry.season }));
      });
    });
    return cols;
  }

  function getColumns() {
    if (viewMode === "season") {
      const cols = sortTeams(SEASONS[seasonIdx].teams.map(applyMetric), sortField, sortDir);
      const total = SEASONS[seasonIdx].league_total;
      if (total) cols.push(Object.assign({ isTotal: true }, applyMetric(total)));
      return cols;
    }
    if (viewMode === "player") return selectedPlayerId ? columnsForPlayer(selectedPlayerId) : [];
    return columnsForTeam(selectedTeamName);
  }

  // ---------- metric toggle (League experience vs. Franchise-specific tenure) ----------
  const metricToggle = document.getElementById("metricToggle");
  const tierControls = document.getElementById("tierControls");
  const tierToggle = document.getElementById("tierToggle");
  const tierRookieBtn = tierToggle.querySelector('[data-tier="rookie"]');
  const careerScopeRow = document.getElementById("careerScopeRow");
  const careerScopeToggle = document.getElementById("careerScopeToggle");
  const careerScopeCount = document.getElementById("careerScopeCount");
  function refreshTierRookieLabel() { tierRookieBtn.textContent = tierZeroLabel(); }
  [...metricToggle.children].forEach(btn => {
    btn.addEventListener("click", () => {
      tenureMetric = btn.dataset.metric;
      [...metricToggle.children].forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      updateLegend();
      refreshTierRookieLabel();
      renderAll();
    });
  });

  // ---------- tier toggle (which experience tier the League movement chart shows) ----------
  [...tierToggle.children].forEach(btn => {
    btn.addEventListener("click", () => {
      movementTier = btn.dataset.tier;
      [...tierToggle.children].forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      renderAll();
    });
  });

  // ---------- career scope toggle (which players the career distributions cover) ----------
  [...careerScopeToggle.children].forEach(btn => {
    btn.addEventListener("click", () => {
      careerScope = btn.dataset.scope;
      [...careerScopeToggle.children].forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      renderAll();
    });
  });

  // ---------- view toggle ----------
  const viewToggle = document.getElementById("viewToggle");
  const seasonTabs = document.getElementById("seasonTabs");
  const teamSelectRow = document.getElementById("teamSelectRow");
  const teamSelect = document.getElementById("teamSelect");
  const playerSearchRow = document.getElementById("playerSearchRow");
  const playerSearchInput = document.getElementById("playerSearch");
  const playerResults = document.getElementById("playerResults");
  const sortControls = document.getElementById("sortControls");
  const sortFieldSelect = document.getElementById("sortFieldSelect");
  const sortDirBtn = document.getElementById("sortDirBtn");
  const sortDirLabel = document.getElementById("sortDirLabel");
  const sortDirIcon = document.getElementById("sortDirIcon");
  const filterRow = document.getElementById("filterRow");
  const chartLegend = document.getElementById("chartLegend");
  const movementSection = document.getElementById("movementSection");
  const movementLegend = document.getElementById("movementLegend");
  const careerLengthSection = document.getElementById("careerLengthSection");
  const franchiseCountSection = document.getElementById("franchiseCountSection");
  const stintLengthSection = document.getElementById("stintLengthSection");
  const simplifyToggleRow = document.getElementById("simplifyToggleRow");
  const chartHeading = document.getElementById("chartHeading");
  const chartNote = document.getElementById("chartNote");
  const drilldownHeading = document.getElementById("drilldownHeading");
  const drilldownNote = document.getElementById("drilldownNote");

  ALL_TEAM_NAMES.forEach(name => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    teamSelect.appendChild(opt);
  });
  teamSelect.value = selectedTeamName;

  [...viewToggle.children].forEach(btn => {
    btn.addEventListener("click", () => {
      viewMode = btn.dataset.mode;
      [...viewToggle.children].forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      applyModeVisibility();
      renderAll();
    });
  });

  teamSelect.addEventListener("change", (e) => {
    selectedTeamName = e.target.value;
    renderAll();
  });

  const LEGEND_CHURN = `
    <span class="legend-item"><span class="legend-swatch stable"></span>Returning</span>
    <span class="legend-item"><span class="legend-swatch new"></span>Arrival</span>
    <span class="legend-item"><span class="legend-swatch both"></span>1-season stint</span>
    <span class="legend-item"><span class="legend-swatch traded"></span>Departure</span>
    <span class="legend-item"><span class="legend-swatch retired"></span>Retired/Hiatus</span>
  `;
  const LEGEND_CHURN_SIMPLE = `
    <span class="legend-item"><span class="legend-swatch stable"></span>Returning</span>
    <span class="legend-item"><span class="legend-swatch new"></span>Arrival</span>
    <span class="legend-item"><span class="legend-swatch retired"></span>Departure</span>
  `;
  function legendComposition() {
    return `
      <span class="legend-item"><span class="legend-swatch tier-rookie"></span>${tierZeroLabel()}</span>
      <span class="legend-item"><span class="legend-swatch tier-tweener"></span>Tweener</span>
      <span class="legend-item"><span class="legend-swatch tier-vet"></span>Vet</span>
    `;
  }

  function legendMovement() {
    return `
      <span class="legend-item"><span class="legend-swatch outcome-same"></span>Returning</span>
      <span class="legend-item"><span class="legend-swatch outcome-traded"></span>Departure</span>
      <span class="legend-item"><span class="legend-swatch outcome-cut"></span>Cut</span>
      <span class="legend-item"><span class="legend-swatch outcome-retired"></span>Retired/Hiatus</span>
    `;
  }

  function updateLegend() {
    if (viewMode === "league") {
      chartLegend.innerHTML = legendComposition();
      movementLegend.innerHTML = legendMovement();
      return;
    }
    chartLegend.innerHTML = simplifiedView ? LEGEND_CHURN_SIMPLE : LEGEND_CHURN;
  }

  function applyModeVisibility() {
    const isTeam = viewMode === "team";
    const isPlayer = viewMode === "player";
    const isSeason = viewMode === "season";
    const isLeague = viewMode === "league";
    seasonTabs.style.display = isSeason ? "flex" : "none";
    teamSelectRow.style.display = isTeam ? "flex" : "none";
    playerSearchRow.style.display = isPlayer ? "flex" : "none";
    sortControls.style.display = isSeason ? "flex" : "none";
    tierControls.style.display = isLeague ? "flex" : "none";
    filterRow.style.display = "block";
    simplifyToggleRow.style.display = isLeague ? "none" : "flex";
    movementSection.style.display = isLeague ? "block" : "none";
    careerScopeRow.style.display = isLeague ? "flex" : "none";
    careerLengthSection.style.display = isLeague ? "block" : "none";
    franchiseCountSection.style.display = isLeague ? "block" : "none";
    stintLengthSection.style.display = isLeague ? "block" : "none";
    updateLegend();

    if (isPlayer) {
      chartHeading.textContent = "Every team-season this player was part of";
      chartNote.textContent = "Each column is the full roster distribution for whichever team this player was on that season (same read as By Team/By Season). Their own dot is ringed and connected across seasons, so you can see whether they moved to more or less experienced rosters.";
      drilldownHeading.textContent = "Roster context, team by team";
      drilldownNote.textContent = "Expand a season to see the full roster they were part of; their own row is highlighted. Search jumps to matching players.";
    } else if (isTeam) {
      chartHeading.textContent = "Distribution of seasons played, per season";
      chartNote.textContent = "Each column is one season for this franchise, oldest → most recent. A dot's size shows how many players share that value and color; box shows the 25th–75th percentile, line is the median, whiskers span min–max. The dashed tick is the league median that season. The rightmost TOTAL column shows this franchise's whole history, one bar per experience level, sized by the % who stayed the following season.";
      drilldownHeading.textContent = "Season-by-season breakdown";
      drilldownNote.textContent = "Expand a season to see every player's tenure that year. Search narrows to matching players.";
    } else if (isLeague) {
      chartHeading.textContent = "League composition, season by season";
      chartNote.textContent = "Each bar is one season, stacked by experience tier league-wide, as a share of that season's players.";
      drilldownHeading.textContent = "Arrivals & departures, season by season";
      drilldownNote.textContent = "Expand a season to see who arrived, who left for another team, and who retired or went on hiatus. Search narrows to matching players.";
    } else {
      chartHeading.textContent = "Distribution of seasons played, per team";
      chartNote.textContent = "A dot's size shows how many players share that value and color (hover for names). Box shows the 25th–75th percentile, line is the median, whiskers span min–max. The rightmost TOTAL column pools every team this season, one bar per experience level, sized by the % who stayed the following season.";
      drilldownHeading.textContent = "Roster-by-roster breakdown";
      drilldownNote.textContent = "Expand a team to see every player's tenure. Search narrows to matching players.";
    }
  }

  // ---------- season tabs ----------
  SEASONS.forEach((s, i) => {
    const btn = document.createElement("button");
    btn.className = "season-tab" + (i === seasonIdx ? " active" : "");
    btn.textContent = "S" + pad2(s.league.season);
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", i === seasonIdx ? "true" : "false");
    btn.addEventListener("click", () => {
      seasonIdx = i;
      [...seasonTabs.children].forEach((c, ci) => {
        c.classList.toggle("active", ci === i);
        c.setAttribute("aria-selected", ci === i ? "true" : "false");
      });
      renderAll();
    });
    seasonTabs.appendChild(btn);
  });

  // ---------- KPI row ----------
  const kpiRow = document.getElementById("kpiRow");
  const eyebrow = document.getElementById("eyebrow");

  function renderKpisSeason() {
    const data = SEASONS[seasonIdx];
    const L = activeLeague(data);
    eyebrow.textContent = "MLN · Season " + L.season;
    const teamByAbbrev = Object.fromEntries(data.teams.map(t => [t.abbrev, t]));
    const arrivals = data.teams.reduce((a, t) => a + t.players.filter(p => p.new_to_team).length, 0);
    const tradedAway = data.teams.reduce((a, t) => a + t.players.filter(p => p.leaving_after && !p.leaving_league).length, 0);
    const retiring = data.teams.reduce((a, t) => a + t.players.filter(p => p.leaving_league).length, 0);
    const naSoon = L.season >= 13;
    const medianLabel = tenureMetric === "franchise" ? "Franchise median tenure" : "League median tenure";
    return [
      { label: "# Players", value: L.n_players, sub: L.n_teams + " teams" },
      { label: medianLabel, value: L.median, sub: "seasons played" },
      { label: "Most seasoned roster", value: L.most_seasoned_team || "N/A", sub: L.most_seasoned_team ? teamByAbbrev[L.most_seasoned_team].name : "" },
      { label: "Least seasoned roster", value: L.least_seasoned_team || "N/A", sub: L.least_seasoned_team ? teamByAbbrev[L.least_seasoned_team].name : "" },
      { label: "New Arrivals", value: arrivals, sub: "" },
      { label: "Departures", value: naSoon ? "N/A" : tradedAway, sub: "" },
      { label: "Retired/Hiatus", value: naSoon ? "N/A" : retiring, sub: "" },
    ];
  }

  function renderKpisTeam(colsWithTotal) {
    eyebrow.textContent = "MLN · " + selectedTeamName;
    const cols = colsWithTotal.filter(c => !c.isTotal);
    if (!cols.length) return [{ label: "No data", value: "N/A", sub: "" }];
    const withRetention = cols.filter(c => c.combined_retention != null);
    let bestRetCol = null, worstRetCol = null;
    withRetention.forEach(c => {
      if (!bestRetCol || c.combined_retention > bestRetCol.combined_retention) bestRetCol = c;
      if (!worstRetCol || c.combined_retention < worstRetCol.combined_retention) worstRetCol = c;
    });
    const arrivalsTotal = cols.reduce((a, c) => a + c.players.filter(p => p.new_to_team).length, 0);
    const tradedTotal = cols.reduce((a, c) => a + c.players.filter(p => p.leaving_after && !p.leaving_league).length, 0);
    const retiringTotal = cols.reduce((a, c) => a + c.players.filter(p => p.leaving_league).length, 0);
    const teamTotal = TEAM_TOTALS[selectedTeamName];
    return [
      {
        label: "Avg Combined Retention",
        value: teamTotal && teamTotal.combined_retention != null ? teamTotal.combined_retention.toFixed(0) + "%" : "N/A",
        sub: "S" + pad2(cols[0].season) + "–S" + pad2(cols[cols.length - 1].season),
      },
      {
        label: "Highest combined retention", value: bestRetCol ? bestRetCol.combined_retention.toFixed(0) + "%" : "N/A",
        sub: bestRetCol ? "S" + pad2(bestRetCol.season) : "",
      },
      {
        label: "Lowest combined retention", value: worstRetCol ? worstRetCol.combined_retention.toFixed(0) + "%" : "N/A",
        sub: worstRetCol ? "S" + pad2(worstRetCol.season) : "",
      },
      { label: "New Arrivals", value: arrivalsTotal, sub: "" },
      { label: "Departures", value: tradedTotal, sub: "" },
      { label: "Retired/Hiatus", value: retiringTotal, sub: "" },
    ];
  }

  function renderKpisPlayer() {
    const playerData = PLAYERS[selectedPlayerId];
    eyebrow.textContent = "MLN · " + playerData.name;
    const hist = playerData.history;
    const totalPA = hist.reduce((a, s) => a + s.teams.reduce((b, t) => b + t.pa, 0), 0);
    const totalBF = hist.reduce((a, s) => a + s.teams.reduce((b, t) => b + t.bf, 0), 0);
    const teamsPlayed = Array.from(new Set(hist.flatMap(s => s.teams.map(t => t.abbrev))));
    const firstSeason = hist[0].season, lastSeason = hist[hist.length - 1].season;
    const lastTeams = hist[hist.length - 1].teams.map(t => t.abbrev).join(", ");
    return [
      { label: "Seasons Played", value: hist.length, sub: "S" + pad2(firstSeason) + "–S" + pad2(lastSeason) },
      { label: "Career PA", value: totalPA, sub: "" },
      { label: "Career BF", value: totalBF, sub: "" },
      { label: "Teams Played For", value: teamsPlayed.length, sub: teamsPlayed.join(", ") },
      { label: "Last Active", value: "S" + pad2(lastSeason), sub: lastTeams },
    ];
  }

  // shared by the League chart/KPIs/drilldown: who arrived, who left for another team,
  // and who left the league entirely, for one season, league-wide
  function seasonMovement(seasonData) {
    const arrivals = [], tradedAway = [], retired = [];
    seasonData.teams.forEach(t => {
      t.players.forEach(p => {
        if (p.new_to_team) arrivals.push({ name: p.name, team: t.abbrev });
        if (p.leaving_league) retired.push({ name: p.name, team: t.abbrev });
        else if (p.leaving_after) tradedAway.push({ name: p.name, team: t.abbrev, other: p.other_teams });
      });
    });
    return { arrivals, tradedAway, retired };
  }

  function renderKpisLeague() {
    eyebrow.textContent = "MLN · League";
    const n = SEASONS.length;
    const latest = activeLeague(SEASONS[n - 1]);
    let totalArrivals = 0, totalTradedAway = 0, totalRetired = 0;
    SEASONS.forEach(s => {
      const m = seasonMovement(s);
      totalArrivals += m.arrivals.length;
      totalTradedAway += m.tradedAway.length;
      totalRetired += m.retired.length;
    });
    const zeroLabel = tierZeroLabel();
    return [
      { label: "Seasons Tracked", value: n, sub: "S01–S" + pad2(n) },
      { label: "Career Players", value: ALL_PLAYERS.length, sub: "all-time, all teams" },
      {
        label: "Latest " + zeroLabel + " Share",
        value: latest.n_players ? Math.round(latest.rookie_count / latest.n_players * 100) + "%" : "N/A",
        sub: "S" + pad2(latest.season) + " · " + latest.rookie_count + " players",
      },
      {
        label: "Latest Vet Share",
        value: latest.n_players ? Math.round(latest.veteran_count / latest.n_players * 100) + "%" : "N/A",
        sub: "S" + pad2(latest.season) + " · " + latest.veteran_count + " players",
      },
      { label: "Total Arrivals", value: totalArrivals, sub: "across all seasons" },
      { label: "Total Departures", value: totalTradedAway + totalRetired, sub: totalTradedAway + " traded · " + totalRetired + " retired/hiatus" },
    ];
  }

  function renderKpis(cols) {
    let kpis;
    if (viewMode === "season") kpis = renderKpisSeason();
    else if (viewMode === "player") kpis = renderKpisPlayer();
    else if (viewMode === "league") kpis = renderKpisLeague();
    else kpis = renderKpisTeam(cols);
    kpiRow.innerHTML = "";
    kpis.forEach(k => {
      const d = document.createElement("div");
      d.className = "kpi";
      d.innerHTML = `<div class="kpi-label">${k.label}</div><div class="kpi-value">${k.value}<small>${k.sub}</small></div>`;
      kpiRow.appendChild(d);
    });
  }

  // ---------- Chart (box-and-jitter, season/team modes) ----------
  const svg = document.getElementById("chart");
  const chartScroll = svg.parentElement;
  const margin = { top: 12, right: 34, bottom: 34, left: 40 };
  const chartH = 440;
  let plotH = chartH - margin.top - margin.bottom;
  const MIN_BAND = 34; // below this, rotate labels so they don't collide
  const ROW_LABELS = ["W-L", "Med", "PA%", "BF%", "Ret%"]; // shared row labels in the left/right margins

  let yMax = 12;
  function yScale(v) { return margin.top + plotH - (v / yMax) * plotH; }

  // simplified-view experience tiers: 3 fixed rungs instead of the continuous 0..yMax scale
  const RUNG_ORDER = ["rookie", "developing", "vet"];
  function rungOf(tenure, vetCutoff) {
    if (tenure === 0) return "rookie";
    if (tenure >= vetCutoff) return "vet";
    return "developing";
  }
  function rungY(rung) {
    if (rung === "rookie") return yScale(0);
    if (rung === "vet") return yScale(yMax);
    return yScale(yMax / 2);
  }
  function rungLabel(rung) {
    if (rung === "rookie") return tierZeroLabel();
    if (rung === "vet") return "Vet";
    return "Tweener";
  }

  function rowValues(col) {
    return [
      (col.wins != null && col.losses != null) ? (col.wins + "-" + col.losses) : "",
      (col.median != null) ? String(col.median) : "",
      (col.pa_retention != null) ? col.pa_retention.toFixed(0) : "",
      (col.bf_retention != null) ? col.bf_retention.toFixed(0) : "",
      (col.combined_retention != null) ? col.combined_retention.toFixed(0) : "",
    ];
  }

  function renderChart(columns) {
    const realColumns = columns.filter(c => !c.isTotal);
    const maxTenureHere = realColumns.length ? Math.max(...realColumns.map(c => c.max), 0) : 0;
    yMax = Math.max(maxTenureHere, 2);
    const gridStep = yMax <= 6 ? 1 : 2;

    svg.innerHTML = "";
    margin.left = simplifiedView ? 80 : 56; // word labels (Rookie/Tweener/Vet) need more room than a number, plus room for the axis title
    const containerW = chartScroll.clientWidth || 720;
    const n = Math.max(columns.length, 1);
    const plotW = Math.max(n * MIN_BAND, containerW - margin.left - margin.right);
    const bandW = plotW / n;
    const totalW = margin.left + plotW + margin.right;
    const rotateLabels = bandW < 48;
    margin.bottom = rotateLabels ? 111 : 101;
    plotH = chartH - margin.top - margin.bottom;
    svg.setAttribute("viewBox", `0 0 ${totalW} ${chartH}`);
    svg.setAttribute("preserveAspectRatio", "none");

    // shared row labels live once in the left/right margins; each column below only shows the bare value
    const abbrevRowY = rotateLabels ? margin.top + plotH + 14 : margin.top + plotH + 20;
    const rowsStartY = rotateLabels ? margin.top + plotH + 32 : margin.top + plotH + 34;
    const rowStep = 13;
    const rowY = ROW_LABELS.map((_, i) => rowsStartY + i * rowStep);
    ROW_LABELS.forEach((label, i) => {
      const leftLbl = el("text", { x: margin.left - 10, y: rowY[i] + 3, "text-anchor": "end", class: "median-sublabel" });
      leftLbl.textContent = label;
      svg.appendChild(leftLbl);
      const rightLbl = el("text", { x: margin.left + plotW + 10, y: rowY[i] + 3, "text-anchor": "start", class: "median-sublabel" });
      rightLbl.textContent = label;
      svg.appendChild(rightLbl);
    });

    // gridlines + y ticks - 3 fixed rungs when simplified, continuous tenure scale otherwise
    if (simplifiedView) {
      RUNG_ORDER.forEach(rung => {
        const y = rungY(rung);
        svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, class: "grid-line" }));
        const t = el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "tick-label" });
        t.textContent = rungLabel(rung);
        svg.appendChild(t);
      });
    } else {
      for (let v = 0; v <= yMax; v += gridStep) {
        const y = yScale(v);
        svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, class: "grid-line" }));
        const t = el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "tick-label" });
        t.textContent = v;
        svg.appendChild(t);
      }
    }

    // simplified view drops the box-and-whisker outline, so add faint vertical rules
    // between columns to keep team/season separation readable
    if (simplifiedView) {
      for (let i = 0; i <= columns.length; i++) {
        const x = margin.left + i * bandW;
        svg.appendChild(el("line", { x1: x, x2: x, y1: margin.top, y2: margin.top + plotH, class: "col-grid-line" }));
      }
    }
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left, y1: margin.top, y2: margin.top + plotH, class: "axis-line" }));
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: margin.top + plotH, y2: margin.top + plotH, class: "axis-line" }));

    const axisTitleX = 12;
    const axisTitleY = margin.top + plotH / 2;
    const axisTitle = el("text", {
      x: axisTitleX, y: axisTitleY, "text-anchor": "middle", class: "axis-title",
      transform: `rotate(-90 ${axisTitleX} ${axisTitleY})`,
    });
    axisTitle.textContent = "Experience (# Seasons)";
    svg.appendChild(axisTitle);

    // player mode: trace the player's own tenure through each team-season, drawn behind the boxes/bubbles
    if (viewMode === "player" && selectedPlayerId) {
      const tracePoints = columns.filter(c => !c.isTotal).map((col, i) => {
        const hp = col.players.find(p => p.player_id === selectedPlayerId);
        if (!hp) return null;
        return { x: margin.left + i * bandW + bandW / 2, y: yScale(playerTenure(hp)) };
      }).filter(Boolean);
      if (tracePoints.length > 1) {
        const d = tracePoints.map((p, i) => (i === 0 ? "M" : "L") + p.x + " " + p.y).join(" ");
        svg.appendChild(el("path", { d, class: "trace-path" }));
      }
    }

    // TOTAL column: pooled retention-by-tenure profile, shown as horizontal bars on the shared y-axis
    // instead of a box-and-whisker. "Simplify view" also collapses this to 3 experience buckets.
    function renderRetentionColumn(col, cx, bandW) {
      const profile = simplifiedView ? col.retention_by_bucket : col.retention_by_tenure;
      const barMaxW = Math.min(70, bandW * 0.72);
      const barLeftX = cx - barMaxW / 2;
      const labelX = cx + barMaxW / 2 + 6;
      const barH = simplifiedView ? 16 : Math.max(5, Math.min(11, (plotH / (yMax + 1)) * 0.55));

      function rowY(row) {
        if (simplifiedView) {
          if (row.bucket === "rookie") return yScale(0);
          if (row.bucket === "vet") return yScale(yMax);
          return yScale(yMax / 2);
        }
        return yScale(row.tenure);
      }
      function rowLabel(row) {
        return simplifiedView
          ? (row.bucket === "rookie" ? tierZeroLabel() + " (0yr)" : row.bucket === "vet" ? "Vet" : "Tweener")
          : (row.tenure + "yr");
      }

      profile.forEach(row => {
        if (!row.total) return;
        const pct = (row.retained / row.total) * 100;
        const y = rowY(row);
        const stayW = Math.max(0, (pct / 100) * barMaxW);

        svg.appendChild(el("rect", { x: barLeftX, y: y - barH / 2, width: barMaxW, height: barH, rx: 2, class: "retention-bar-track" }));
        if (stayW > 0.5) {
          svg.appendChild(el("rect", { x: barLeftX, y: y - barH / 2, width: stayW, height: barH, rx: 2, class: "retention-bar-stay" }));
        }

        const hoverRect = el("rect", { x: barLeftX, y: y - barH / 2, width: barMaxW, height: barH, fill: "transparent", style: "cursor:pointer;" });
        hoverRect.addEventListener("mousemove", (e) => {
          showTip(
            `<div class="tt-title">${col.name} &middot; ${rowLabel(row)}</div>` +
            `<div class="tt-row">${pct.toFixed(0)}% stayed with the team the following season</div>` +
            `<div class="tt-row">${row.retained} of ${row.total} player-seasons</div>`,
            e.clientX, e.clientY
          );
        });
        hoverRect.addEventListener("mouseleave", hideTip);
        svg.appendChild(hoverRect);

        const lbl = el("text", { x: labelX, y: y + 3, "text-anchor": "start", class: "tick-label" });
        lbl.textContent = pct.toFixed(0) + "%";
        svg.appendChild(lbl);
      });
    }

    // simplified view for real (non-total) columns: no box-and-whisker, just one merged bubble
    // per experience rung (Rookie / Tweener / Vet), split by status same as the detailed view
    function renderSimplifiedColumn(col, cx, bandW, boxW) {
      const dotR = Math.max(3, Math.min(4.6, bandW * 0.07));
      const absMaxR = Math.min(13, bandW * 0.2);
      const envelope = Math.min(bandW * 0.8, Math.max(boxW * 2, 60));
      const gap = 2;
      const STATUS_ORDER = ["stable", "new", "departure"];

      const byRung = new Map();
      col.players.forEach(p => {
        const rung = rungOf(playerTenure(p), col.veteran_cutoff);
        const status = displayStatus(p);
        if (!byRung.has(rung)) byRung.set(rung, new Map());
        const statusMap = byRung.get(rung);
        if (!statusMap.has(status)) statusMap.set(status, []);
        statusMap.get(status).push(p);
      });

      byRung.forEach((statusMap, rung) => {
        const bucket = STATUS_ORDER
          .filter(s => statusMap.has(s))
          .map(status => ({ status, rung, players: statusMap.get(status) }));
        const k = bucket.length;
        const maxRBucket = Math.max(2, Math.min(absMaxR, (envelope - gap * (k - 1)) / (2 * k)));

        const radii = bucket.map(group => Math.min(maxRBucket, dotR * Math.sqrt(group.players.length)));
        const totalWidth = radii.reduce((a, r) => a + 2 * r, 0) + gap * (k - 1);
        let x = cx - totalWidth / 2;

        bucket.forEach((group, i) => {
          const r = radii[i];
          x += r;
          placeSimplifiedBubble(x, rungY(rung), r, group);
          x += r + gap;
        });
      });

      function placeSimplifiedBubble(dcx, dcy, r, group) {
        const isHighlighted = viewMode === "player" && selectedPlayerId && group.players.some(p => p.player_id === selectedPlayerId);
        const g = el("circle", { cx: dcx, cy: dcy, r, fill: colorForStatus(group.status), class: "dot" });

        g.addEventListener("mousemove", (e) => {
          const teamLine = (viewMode === "team" || viewMode === "player")
            ? `<div class="tt-row">${col.name} &middot; S${pad2(col.season)} (${col.abbrev})</div>`
            : `<div class="tt-row">${col.name}</div>`;
          const count = group.players.length;
          const title = count === 1 ? group.players[0].name : `${count} players &middot; ${rungLabel(group.rung)}`;
          const nameLines = group.players.map(p => {
            const tradedNote = p.traded ? ` <span style="opacity:.7">(also: ${p.other_teams.join(", ")})</span>` : "";
            return `<div class="tt-row">${p.name}${tradedNote}</div>`;
          }).join("");
          const rungLine = count === 1 ? `<div class="tt-row">${rungLabel(group.rung)}</div>` : "";
          showTip(
            `<div class="tt-title">${title}</div>` +
            nameLines +
            teamLine +
            rungLine +
            `<div class="tt-row">${statusLine(group.players[0])}</div>`,
            e.clientX, e.clientY
          );
        });
        g.addEventListener("mouseleave", hideTip);
        svg.appendChild(g);
        if (isHighlighted) {
          svg.appendChild(el("circle", { cx: dcx, cy: dcy, r: r + 3, class: "highlight-ring" }));
        }
      }
    }

    columns.forEach((col, i) => {
      const cx = margin.left + i * bandW + bandW / 2;
      const boxW = Math.max(10, Math.min(34, bandW * 0.5));
      const capW = Math.max(4, boxW * 0.22);

      if (col.isTotal) {
        renderRetentionColumn(col, cx, bandW);
      } else if (simplifiedView) {
        renderSimplifiedColumn(col, cx, bandW, boxW);
      } else {
        // league median reference tick (compares this team-season to the league that season)
        if (viewMode === "team" || viewMode === "player") {
          const seasonData = SEASONS[col.season - 1];
          if (seasonData) {
            const lm = activeLeague(seasonData).median;
            const ly = yScale(lm);
            svg.appendChild(el("line", {
              x1: cx - boxW * 0.65, x2: cx + boxW * 0.65, y1: ly, y2: ly,
              class: "league-ref-line",
            }));
          }
        }

        // whisker
        svg.appendChild(el("line", { x1: cx, x2: cx, y1: yScale(col.min), y2: yScale(col.max), class: "whisker" }));
        svg.appendChild(el("line", { x1: cx - capW, x2: cx + capW, y1: yScale(col.min), y2: yScale(col.min), class: "whisker" }));
        svg.appendChild(el("line", { x1: cx - capW, x2: cx + capW, y1: yScale(col.max), y2: yScale(col.max), class: "whisker" }));

        // box
        const yTop = yScale(col.q3), yBot = yScale(col.q1);
        svg.appendChild(el("rect", {
          x: cx - boxW / 2, y: yTop, width: boxW, height: Math.max(2, yBot - yTop),
          rx: 4, class: "box-rect"
        }));

        // median
        const ym = yScale(col.median);
        svg.appendChild(el("line", { x1: cx - boxW / 2, x2: cx + boxW / 2, y1: ym, y2: ym, class: "median-line" }));

        // merge players sharing the same tenure + status into one sized bubble;
        // a single bubble at a tenure level sits exactly on cx (aligned with the box),
        // multiple bubbles at the same tenure spread out symmetrically just enough not to touch
        const dotR = Math.max(3, Math.min(4.6, bandW * 0.07));
        const absMaxR = Math.min(13, bandW * 0.2);
        const envelope = Math.min(bandW * 0.8, Math.max(boxW * 2, 60));
        const gap = 2;
        const STATUS_ORDER = ["stable", "new", "both", "traded_away", "retired"];

        const byTenure = new Map();
        col.players.forEach(p => {
          const status = displayStatus(p);
          const tenureVal = playerTenure(p);
          if (!byTenure.has(tenureVal)) byTenure.set(tenureVal, new Map());
          const statusMap = byTenure.get(tenureVal);
          if (!statusMap.has(status)) statusMap.set(status, []);
          statusMap.get(status).push(p);
        });

        byTenure.forEach((statusMap, tenure) => {
          const bucket = STATUS_ORDER
            .filter(s => statusMap.has(s))
            .map(status => ({ status, tenure, players: statusMap.get(status) }));
          const k = bucket.length;
          const maxRBucket = Math.max(2, Math.min(absMaxR, (envelope - gap * (k - 1)) / (2 * k)));

          const radii = bucket.map(group => Math.min(maxRBucket, dotR * Math.sqrt(group.players.length)));
          const totalWidth = radii.reduce((a, r) => a + 2 * r, 0) + gap * (k - 1);
          let x = cx - totalWidth / 2;

          bucket.forEach((group, i) => {
            const r = radii[i];
            x += r;
            placeBubble(x, yScale(tenure), r, group);
            x += r + gap;
          });
        });

        function placeBubble(dcx, dcy, r, group) {
          const isHighlighted = viewMode === "player" && selectedPlayerId && group.players.some(p => p.player_id === selectedPlayerId);
          const g = el("circle", { cx: dcx, cy: dcy, r, fill: colorForStatus(group.status), class: "dot" });

          g.addEventListener("mousemove", (e) => {
            const teamLine = (viewMode === "team" || viewMode === "player")
              ? `<div class="tt-row">${col.name} &middot; S${pad2(col.season)} (${col.abbrev})</div>`
              : `<div class="tt-row">${col.name}</div>`;
            const count = group.players.length;
            const tenureSuffix = tenureMetric === "franchise" ? " with this team" : " played";
            const tenureText = `${group.tenure} prior season${group.tenure === 1 ? "" : "s"}${tenureSuffix}`;
            const title = count === 1
              ? group.players[0].name
              : `${count} players &middot; ${tenureText}`;
            const nameLines = group.players.map(p => {
              const tradedNote = p.traded ? ` <span style="opacity:.7">(also: ${p.other_teams.join(", ")})</span>` : "";
              return `<div class="tt-row">${p.name}${tradedNote}</div>`;
            }).join("");
            const tenureLine = count === 1
              ? `<div class="tt-row">${tenureText}</div>`
              : "";
            showTip(
              `<div class="tt-title">${title}</div>` +
              nameLines +
              teamLine +
              tenureLine +
              `<div class="tt-row">${statusLine(group.players[0])}</div>`,
              e.clientX, e.clientY
            );
          });
          g.addEventListener("mouseleave", hideTip);
          svg.appendChild(g);
          if (isHighlighted) {
            svg.appendChild(el("circle", { cx: dcx, cy: dcy, r: r + 3, class: "highlight-ring" }));
          }
        }
      }

      if (!col.isTotal) {
        // box hover target (invisible wider rect)
        const hoverTarget = el("rect", { x: cx - bandW/2, y: margin.top, width: bandW, height: plotH, fill: "transparent" });
        hoverTarget.addEventListener("mousemove", (e) => {
          const title = (viewMode === "team" || viewMode === "player")
            ? `${col.name} &middot; Season ${col.season} (${col.abbrev})`
            : `${col.name} (${col.abbrev})`;
          const recordLine = (col.wins != null && col.losses != null)
            ? `<div class="tt-row">Record: ${col.wins}-${col.losses}</div>`
            : "";
          const retParts = [];
          if (col.pa_retention != null) retParts.push(col.pa_retention.toFixed(0) + "% PA");
          if (col.bf_retention != null) retParts.push(col.bf_retention.toFixed(0) + "% BF");
          if (col.combined_retention != null) retParts.push(col.combined_retention.toFixed(0) + "% combined");
          const retLine = retParts.length
            ? `<div class="tt-row">Retained from last season: ${retParts.join(" / ")}</div>`
            : "";
          const leagueLine = ((viewMode === "team" || viewMode === "player") && SEASONS[col.season - 1])
            ? `<div class="tt-row">League median that season: ${activeLeague(SEASONS[col.season - 1]).median}</div>`
            : "";
          const vetSuffix = tenureMetric === "franchise" ? "+ seasons with this team" : "+ seasons";
          const zeroWord = tenureMetric === "franchise" ? "new to the team" : "rookies";
          showTip(
            `<div class="tt-title">${title}</div>` +
            recordLine +
            `<div class="tt-row">${col.n} players &middot; median ${col.median} &middot; mean ${col.mean}</div>` +
            `<div class="tt-row">${col.veteran_count} veterans (${col.veteran_cutoff}${vetSuffix}) &middot; ${col.rookie_count} ${zeroWord}</div>` +
            retLine +
            leagueLine,
            e.clientX, e.clientY
          );
        });
        hoverTarget.addEventListener("mouseleave", hideTip);
        svg.insertBefore(hoverTarget, svg.firstChild.nextSibling);
      }

      // x label (abbrev/season) + bare-value rows below it (row words live once in the margins)
      const labelText = col.isTotal ? "TOTAL"
        : viewMode === "player" ? ("S" + pad2(col.season) + " " + col.abbrev)
        : viewMode === "team" ? "S" + pad2(col.season) : col.abbrev;
      const lbl = el("text", {
        x: cx, y: abbrevRowY, "text-anchor": rotateLabels ? "end" : "middle", class: "team-label",
      });
      if (rotateLabels) lbl.setAttribute("transform", `rotate(-45 ${cx} ${abbrevRowY})`);
      lbl.textContent = labelText;
      svg.appendChild(lbl);

      rowValues(col).forEach((val, ri) => {
        if (!val) return;
        const cell = el("text", { x: cx, y: rowY[ri] + 3, "text-anchor": "middle", class: "median-sublabel" });
        cell.textContent = val;
        svg.appendChild(cell);
      });
    });
  }


  // ---------- League charts: composition (season by season) + movement (by experience tier) ----------
  const movementSvg = document.getElementById("movementChart");
  const movementChartScroll = movementSvg.parentElement;
  const movementChartH = 280;

  const MOVEMENT_OUTCOME_ORDER = [
    { key: "same_team", label: "Returning", cls: "outcome-bar-same" },
    { key: "traded", label: "Departure", cls: "outcome-bar-traded" },
    { key: "cut", label: "Cut", cls: "outcome-bar-cut" },
    { key: "retired_hiatus", label: "Retired/Hiatus", cls: "outcome-bar-retired" },
  ];

  function renderLeagueCompositionChart() {
    svg.innerHTML = "";
    margin.left = 74; // room for word labels in the left margin
    const containerW = chartScroll.clientWidth || 720;
    const n = SEASONS.length;
    const bandW = Math.max(46, (containerW - margin.left - margin.right) / n);
    const plotW = bandW * n;
    const totalW = margin.left + plotW + margin.right;
    const barW = Math.max(16, bandW * 0.5);
    const labelH = 34;
    const topH = chartH - margin.top - labelH;
    svg.setAttribute("viewBox", `0 0 ${totalW} ${chartH}`);
    svg.setAttribute("preserveAspectRatio", "none");

    function topY(pct) { return margin.top + topH - (pct / 100) * topH; }
    [0, 25, 50, 75, 100].forEach(pct => {
      const y = topY(pct);
      svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, class: "grid-line" }));
      const t = el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "tick-label" });
      t.textContent = pct + "%";
      svg.appendChild(t);
    });
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left, y1: margin.top, y2: margin.top + topH, class: "axis-line" }));
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: margin.top + topH, y2: margin.top + topH, class: "axis-line" }));

    SEASONS.forEach((s, i) => {
      const cx = margin.left + i * bandW + bandW / 2;
      const L = activeLeague(s);
      const rookie = L.rookie_count, vet = L.veteran_count, total = L.n_players;
      const tweener = Math.max(0, total - rookie - vet);

      let yCursor = margin.top + topH;
      [
        { key: "rookie", label: tierZeroLabel(), val: rookie, cls: "league-seg-rookie" },
        { key: "tweener", label: "Tweener", val: tweener, cls: "league-seg-tweener" },
        { key: "vet", label: "Vet", val: vet, cls: "league-seg-vet" },
      ].forEach(seg => {
        if (!seg.val || !total) return;
        const pct = seg.val / total * 100;
        const segH = (pct / 100) * topH;
        const y = yCursor - segH;
        const rect = el("rect", { x: cx - barW / 2, y, width: barW, height: Math.max(1, segH - 1), class: seg.cls });
        rect.addEventListener("mousemove", (e) => {
          showTip(
            `<div class="tt-title">S${pad2(L.season)} &middot; ${seg.label}</div>` +
            `<div class="tt-row">${pct.toFixed(0)}% (${seg.val} of ${total} players)</div>`,
            e.clientX, e.clientY
          );
        });
        rect.addEventListener("mouseleave", hideTip);
        svg.appendChild(rect);
        yCursor = y;
      });

      const seasonLbl = el("text", { x: cx, y: margin.top + topH + labelH - 10, "text-anchor": "middle", class: "team-label" });
      seasonLbl.textContent = "S" + pad2(L.season);
      svg.appendChild(seasonLbl);
    });
  }

  // pools every player who was the given tier and whose team-of-record this season is their
  // LAST team that season (so a mid-season trade only counts once, from that final team)
  function tierOutcomeCounts(seasonsList) {
    const counts = { same_team: 0, traded: 0, cut: 0, retired_hiatus: 0, total: 0 };
    seasonsList.forEach(s => {
      s.teams.forEach(t => {
        const vetCutoff = t.metrics[tenureMetric].veteran_cutoff;
        t.players.forEach(p => {
          if (p.next_status == null || !p.is_last_team_this_season) return;
          if (movementTier !== "all" && rungOf(playerTenure(p), vetCutoff) !== movementTier) return;
          if (p.next_status === "retired" || p.next_status === "hiatus") counts.retired_hiatus++;
          else counts[p.next_status]++;
          counts.total++;
        });
      });
    });
    return counts;
  }

  function renderLeagueMovementChart() {
    movementSvg.innerHTML = "";
    const mMargin = { top: 12, right: 34, bottom: 34, left: 74 };
    const containerW = movementChartScroll.clientWidth || 720;
    const n = SEASONS.length + 1; // +1 for the pooled TOTAL bar
    const bandW = Math.max(46, (containerW - mMargin.left - mMargin.right) / n);
    const plotW = bandW * n;
    const totalW = mMargin.left + plotW + mMargin.right;
    const barW = Math.max(16, bandW * 0.5);
    const plotH = movementChartH - mMargin.top - mMargin.bottom;

    movementSvg.setAttribute("viewBox", `0 0 ${totalW} ${movementChartH}`);
    movementSvg.setAttribute("preserveAspectRatio", "none");

    function moveY(pct) { return mMargin.top + plotH - (pct / 100) * plotH; }
    [0, 25, 50, 75, 100].forEach(pct => {
      const y = moveY(pct);
      movementSvg.appendChild(el("line", { x1: mMargin.left, x2: mMargin.left + plotW, y1: y, y2: y, class: "grid-line" }));
      const t = el("text", { x: mMargin.left - 10, y: y + 4, "text-anchor": "end", class: "tick-label" });
      t.textContent = pct + "%";
      movementSvg.appendChild(t);
    });
    movementSvg.appendChild(el("line", { x1: mMargin.left, x2: mMargin.left, y1: mMargin.top, y2: mMargin.top + plotH, class: "axis-line" }));
    movementSvg.appendChild(el("line", { x1: mMargin.left, x2: mMargin.left + plotW, y1: mMargin.top + plotH, y2: mMargin.top + plotH, class: "axis-line" }));

    function drawBar(cx, counts, label, isTotal) {
      const lbl = el("text", { x: cx, y: mMargin.top + plotH + 20, "text-anchor": "middle", class: "team-label" });
      lbl.textContent = label;
      movementSvg.appendChild(lbl);

      if (!counts.total) {
        const naLbl = el("text", { x: cx, y: mMargin.top + plotH / 2 + 4, "text-anchor": "middle", class: "tick-label" });
        naLbl.textContent = "N/A";
        movementSvg.appendChild(naLbl);
        return;
      }
      let yCursor = mMargin.top + plotH;
      MOVEMENT_OUTCOME_ORDER.forEach(o => {
        const val = counts[o.key];
        if (!val) return;
        const segH = (val / counts.total) * plotH;
        const y = yCursor - segH;
        const rect = el("rect", {
          x: cx - barW / 2, y, width: barW, height: Math.max(1, segH - 1),
          class: o.cls + (isTotal ? " total-bar-seg" : ""),
        });
        rect.addEventListener("mousemove", (e) => {
          showTip(
            `<div class="tt-title">${label} &middot; ${o.label}</div>` +
            `<div class="tt-row">${(val / counts.total * 100).toFixed(0)}% (${val} of ${counts.total})</div>`,
            e.clientX, e.clientY
          );
        });
        rect.addEventListener("mouseleave", hideTip);
        movementSvg.appendChild(rect);
        yCursor = y;
      });
    }

    SEASONS.forEach((s, i) => {
      const cx = mMargin.left + i * bandW + bandW / 2;
      drawBar(cx, tierOutcomeCounts([s]), "S" + pad2(s.league.season), false);
    });
    const totalCx = mMargin.left + SEASONS.length * bandW + bandW / 2;
    drawBar(totalCx, tierOutcomeCounts(SEASONS), "TOTAL", true);
  }

  // ---------- League career distributions: career length + franchise count + stint length ----------
  const careerLengthSvg = document.getElementById("careerLengthChart");
  const careerLengthScroll = careerLengthSvg.parentElement;
  const franchiseCountSvg = document.getElementById("franchiseCountChart");
  const franchiseCountScroll = franchiseCountSvg.parentElement;
  const stintLengthSvg = document.getElementById("stintLengthChart");
  const stintLengthScroll = stintLengthSvg.parentElement;
  const histChartH = 220;

  function renderHistogramChart(targetSvg, scrollEl, dist, meanVal, medianVal, xLabel, yLabel, tooltipFn) {
    targetSvg.innerHTML = "";
    if (!dist.length) {
      const t = el("text", { x: 20, y: 30, "text-anchor": "start", class: "tick-label" });
      t.textContent = "No players in this scope.";
      targetSvg.appendChild(t);
      return;
    }
    const m = { top: 34, right: 20, bottom: 50, left: 60 };
    const containerW = scrollEl.clientWidth || 720;
    const n = dist.length;
    const bandW = Math.max(36, (containerW - m.left - m.right) / n);
    const plotW = bandW * n;
    const totalW = m.left + plotW + m.right;
    const plotH = histChartH - m.top - m.bottom;
    targetSvg.setAttribute("viewBox", `0 0 ${totalW} ${histChartH}`);
    targetSvg.setAttribute("preserveAspectRatio", "none");

    const maxCount = Math.max(...dist.map(d => d.count), 1);
    const totalN = dist.reduce((a, d) => a + d.count, 0);
    // scale bars against 1.25x the real max, so even the tallest bar leaves headroom inside the
    // plot for its own %-label - keeps every %-label safely below the mean/median labels, which
    // live in the fixed margin above the plot, regardless of which bar the mean/median line hits
    const yMaxScale = maxCount * 1.25;
    function y(v) { return m.top + plotH - (v / yMaxScale) * plotH; }

    const step = maxCount > 400 ? 100 : maxCount > 150 ? 50 : maxCount > 40 ? 20 : 10;
    for (let v = 0; v <= maxCount; v += step) {
      const yy = y(v);
      targetSvg.appendChild(el("line", { x1: m.left, x2: m.left + plotW, y1: yy, y2: yy, class: "grid-line" }));
      const t = el("text", { x: m.left - 10, y: yy + 4, "text-anchor": "end", class: "tick-label" });
      t.textContent = v;
      targetSvg.appendChild(t);
    }
    targetSvg.appendChild(el("line", { x1: m.left, x2: m.left, y1: m.top, y2: m.top + plotH, class: "axis-line" }));
    targetSvg.appendChild(el("line", { x1: m.left, x2: m.left + plotW, y1: m.top + plotH, y2: m.top + plotH, class: "axis-line" }));

    const barW = Math.max(14, bandW * 0.55);
    dist.forEach((d, i) => {
      const cx = m.left + i * bandW + bandW / 2;
      const h = (d.count / yMaxScale) * plotH;
      const barTopY = m.top + plotH - h;
      const rect = el("rect", { x: cx - barW / 2, y: barTopY, width: barW, height: Math.max(1, h), class: "hist-bar" });
      rect.addEventListener("mousemove", (e) => showTip(tooltipFn(d, totalN), e.clientX, e.clientY));
      rect.addEventListener("mouseleave", hideTip);
      targetSvg.appendChild(rect);
      const pctText = (d.count / totalN * 100).toFixed(1) + "%";
      const pctW = pctText.length * 6.4 + 4; // rough width estimate - masks a mean/median line crossing behind the label
      const pctBg = el("rect", { x: cx - pctW / 2, y: barTopY - 17, width: pctW, height: 13, rx: 2, class: "hist-bar-pct-bg" });
      targetSvg.appendChild(pctBg);
      const pctLbl = el("text", { x: cx, y: barTopY - 6, "text-anchor": "middle", class: "hist-bar-pct" });
      pctLbl.textContent = pctText;
      targetSvg.appendChild(pctLbl);
      const lbl = el("text", { x: cx, y: m.top + plotH + 18, "text-anchor": "middle", class: "team-label" });
      lbl.textContent = d.value;
      targetSvg.appendChild(lbl);
    });

    // mean/median reference lines - positioned continuously across the bar bands by value,
    // labels stacked at different heights above the plot so close values don't collide
    const firstVal = dist[0].value;
    function xForValue(v) { return m.left + (v - firstVal) * bandW + bandW / 2; }

    const meanX = xForValue(meanVal);
    targetSvg.appendChild(el("line", { x1: meanX, x2: meanX, y1: m.top, y2: m.top + plotH, class: "ref-line-mean" }));
    const meanLbl = el("text", { x: meanX, y: m.top - 20, "text-anchor": "middle", class: "ref-label-mean" });
    meanLbl.textContent = "Mean " + meanVal;
    targetSvg.appendChild(meanLbl);

    const medianX = xForValue(medianVal);
    targetSvg.appendChild(el("line", { x1: medianX, x2: medianX, y1: m.top, y2: m.top + plotH, class: "ref-line-median" }));
    const medianLbl = el("text", { x: medianX, y: m.top - 8, "text-anchor": "middle", class: "ref-label-median" });
    medianLbl.textContent = "Median " + medianVal;
    targetSvg.appendChild(medianLbl);

    // axis titles
    const xTitle = el("text", { x: m.left + plotW / 2, y: m.top + plotH + 36, "text-anchor": "middle", class: "axis-title" });
    xTitle.textContent = xLabel;
    targetSvg.appendChild(xTitle);

    const yTitleX = 12;
    const yTitleY = m.top + plotH / 2;
    const yTitle = el("text", {
      x: yTitleX, y: yTitleY, "text-anchor": "middle", class: "axis-title",
      transform: `rotate(-90 ${yTitleX} ${yTitleY})`,
    });
    yTitle.textContent = yLabel;
    targetSvg.appendChild(yTitle);
  }

  function renderCareerLengthChart() {
    const block = LEAGUE_CAREER[careerScope];
    renderHistogramChart(
      careerLengthSvg, careerLengthScroll,
      block.career_length_dist, block.career_length_mean, block.career_length_median,
      "Career Length (seasons)", "Count",
      (d, total) => `<div class="tt-title">${d.value} season${d.value === 1 ? "" : "s"}</div>` +
        `<div class="tt-row">${d.count} players (${(d.count / total * 100).toFixed(0)}%)</div>`
    );
  }

  function renderFranchiseCountChart() {
    const block = LEAGUE_CAREER[careerScope];
    renderHistogramChart(
      franchiseCountSvg, franchiseCountScroll,
      block.franchise_count_dist, block.franchise_count_mean, block.franchise_count_median,
      "# Franchises", "Count",
      (d, total) => `<div class="tt-title">${d.value} franchise${d.value === 1 ? "" : "s"}</div>` +
        `<div class="tt-row">${d.count} players (${(d.count / total * 100).toFixed(0)}%)</div>`
    );
  }

  function renderStintLengthChart() {
    const block = LEAGUE_CAREER[careerScope];
    renderHistogramChart(
      stintLengthSvg, stintLengthScroll,
      block.stint_length_dist, block.stint_length_mean, block.stint_length_median,
      "Stint Length (seasons)", "Count",
      (d, total) => `<div class="tt-title">${d.value}-season stint${d.value === 1 ? "" : "s"}</div>` +
        `<div class="tt-row">${d.count} stints (${(d.count / total * 100).toFixed(0)}%)</div>`
    );
  }

  function updateCareerScopeCount() {
    const block = LEAGUE_CAREER[careerScope];
    const label = careerScope === "all" ? "players" : careerScope === "active" ? "active players" : "retired/hiatus players";
    careerScopeCount.textContent = block.player_count + " " + label;
  }

  // ---------- League drilldown: arrivals/departures/retirees per season ----------
  function renderLeagueDrilldown(query) {
    teamGrid.innerHTML = "";
    const q = query.trim().toLowerCase();

    SEASONS.slice().reverse().forEach((s, idxFromEnd) => {
      const L = activeLeague(s);
      const m = seasonMovement(s);
      const filterList = (list) => q ? list.filter(p => p.name.toLowerCase().includes(q)) : list;
      const arrivals = filterList(m.arrivals);
      const tradedAway = filterList(m.tradedAway);
      const retired = filterList(m.retired);
      if (q && !arrivals.length && !tradedAway.length && !retired.length) return;

      const details = document.createElement("details");
      details.className = "team-card";
      if (q || idxFromEnd === 0) details.open = true;

      function group(title, badgeClass, list, showOther) {
        if (!list.length) return "";
        const rows = list.map(p => {
          const otherNote = showOther && p.other && p.other.length ? ` <span style="opacity:.7">(also: ${p.other.join(", ")})</span>` : "";
          return `<tr><td class="name-cell">${p.name}${otherNote}</td><td style="text-align:right;color:var(--text-secondary);">${p.team}</td></tr>`;
        }).join("");
        return `<div style="margin-top:10px;">
          <div style="padding:0 16px;">
            <span class="${badgeClass}">${title.toUpperCase()}</span>
            <span style="font-size:11.5px;color:var(--text-secondary);margin-left:6px;">${list.length}</span>
          </div>
          <table class="roster"><tbody>${rows}</tbody></table>
        </div>`;
      }

      details.innerHTML = `
        <summary>
          <div class="summary-left">
            <span class="team-abbrev">S${pad2(L.season)}</span>
            <span class="team-name">${L.n_players} players &middot; ${L.n_teams} teams</span>
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="chips">
              <span class="chip">${m.arrivals.length} arrivals</span>
              <span class="chip">&middot;</span>
              <span class="chip">${m.tradedAway.length} traded</span>
              <span class="chip">&middot;</span>
              <span class="chip">${m.retired.length} retired/hiatus</span>
            </div>
            <svg class="chevron" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </div>
        </summary>
        ${group("Arrivals", "badge-arrival", arrivals, false)}
        ${group("Traded away", "badge-exit", tradedAway, true)}
        ${group("Retired / Hiatus", "badge-retired", retired, false)}
      `;
      teamGrid.appendChild(details);
    });
  }

  // ---------- Drilldown (season/team modes) ----------
  const teamGrid = document.getElementById("teamGrid");
  const searchBox = document.getElementById("searchBox");

  function renderDrilldown(columnsWithTotal, query) {
    teamGrid.innerHTML = "";
    const q = query.trim().toLowerCase();
    const columns = columnsWithTotal.filter(c => !c.isTotal);

    columns.forEach((col, idx) => {
      const headerLabel = viewMode === "player" ? ("S" + pad2(col.season) + " " + col.abbrev)
        : viewMode === "team" ? ("S" + pad2(col.season)) : col.abbrev;
      const headerTitle = (viewMode === "team" || viewMode === "player") ? (col.name + " (as " + col.abbrev + ")") : col.name;
      // search only narrows by player name now -- a team/abbrev match no longer pulls in its whole roster
      const rowsSource = q ? col.players.filter(p => p.name.toLowerCase().includes(q)) : col.players;
      if (q && !rowsSource.length) return;

      const details = document.createElement("details");
      details.className = "team-card";
      const defaultOpen = viewMode === "player" ? true : (idx === (viewMode === "team" ? columns.length - 1 : 0));
      if (q || (!q && defaultOpen)) details.open = true;

      const rows = rowsSource.map(p => {
        const isTraced = viewMode === "player" && p.player_id === selectedPlayerId;
        const tenureVal = playerTenure(p);
        const pct = Math.round((tenureVal / Math.max(yMax, 1)) * 100);
        const status = statusOf(p);
        const barColor = colorForStatus(status);
        const tradedBadge = p.traded
          ? `<span class="badge-traded" title="Also played for ${p.other_teams.join(', ')}">↔ ${p.other_teams.join(', ')}</span>`
          : "";
        const zeroTitle = tenureMetric === "franchise" ? "First season with this team" : "First overall season";
        const arrivalBadge = tenureVal === 0
          ? `<span class="badge-arrival" title="${zeroTitle}">${tierZeroLabel().toUpperCase()}</span>`
          : (p.new_to_team ? `<span class="badge-arrival" title="${statusLine(p)}">NEW</span>` : "");
        const exitBadge = (status === "both" || status === "traded_away")
          ? `<span class="badge-exit" title="${statusLine(p)}">EXIT</span>`
          : (status === "retired" ? `<span class="badge-retired" title="${statusLine(p)}">RETIRED/HIATUS</span>` : "");
        return `<tr class="${q ? "match" : ""}${isTraced ? " traced-row" : ""}">
          <td class="name-cell">${p.name}${arrivalBadge}${exitBadge}${tradedBadge}</td>
          <td class="tenure-cell">
            <div class="tenure-wrap">
              <div class="tenure-bar-track"><div class="tenure-bar-fill" style="width:${pct}%;background:${barColor}"></div></div>
              <span class="tenure-num">${tenureVal}</span>
            </div>
          </td>
        </tr>`;
      }).join("");

      const countChip = q
        ? `<span class="chip">${rowsSource.length} match${rowsSource.length === 1 ? "" : "es"}</span>`
        : `<span class="chip">${col.n} players</span>`;

      details.innerHTML = `
        <summary>
          <div class="summary-left">
            <span class="team-abbrev">${headerLabel}</span>
            <span class="team-name">${headerTitle}</span>
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="chips">
              ${col.wins != null && col.losses != null ? `<span class="chip">${col.wins}-${col.losses}</span><span class="chip">&middot;</span>` : ""}
              ${countChip}
              <span class="chip">&middot;</span>
              <span class="chip">median ${col.median}</span>
              <span class="chip">&middot;</span>
              <span class="chip">${col.veteran_count} vets</span>
            </div>
            <svg class="chevron" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </div>
        </summary>
        <table class="roster">
          <thead><tr><th>Player</th><th style="text-align:right;">Seasons</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
      teamGrid.appendChild(details);
    });
  }

  // ---------- player search ----------
  function renderPlayerResults(query) {
    const q = query.trim().toLowerCase();
    if (!q) { playerResults.classList.remove("show"); playerResults.innerHTML = ""; return; }
    const matches = ALL_PLAYERS.filter(p => p.name.toLowerCase().includes(q)).slice(0, 12);
    if (!matches.length) {
      playerResults.innerHTML = `<div class="player-result-item" style="cursor:default;">No matches</div>`;
      playerResults.classList.add("show");
      return;
    }
    playerResults.innerHTML = matches.map(p =>
      `<div class="player-result-item" data-id="${p.id}"><span>${p.name}</span><span class="player-result-meta">${p.seasons} season${p.seasons === 1 ? "" : "s"}</span></div>`
    ).join("");
    playerResults.classList.add("show");
  }

  playerSearchInput.addEventListener("input", (e) => renderPlayerResults(e.target.value));
  playerSearchInput.addEventListener("focus", (e) => { if (e.target.value) renderPlayerResults(e.target.value); });
  document.addEventListener("click", (e) => {
    if (e.target !== playerSearchInput && !playerResults.contains(e.target)) playerResults.classList.remove("show");
  });
  playerResults.addEventListener("click", (e) => {
    const item = e.target.closest(".player-result-item");
    if (!item || !item.dataset.id) return;
    selectedPlayerId = item.dataset.id;
    playerSearchInput.value = PLAYERS[selectedPlayerId].name;
    playerResults.classList.remove("show");
    renderAll();
  });

  // ---------- wire up ----------
  document.getElementById("simplifyToggle").addEventListener("change", (e) => {
    simplifiedView = e.target.checked;
    updateLegend();
    redrawChart();
  });

  function redrawChart() {
    if (viewMode === "player" && !selectedPlayerId) return;
    if (viewMode === "league") {
      renderLeagueCompositionChart();
      renderLeagueMovementChart();
      renderCareerLengthChart();
      renderFranchiseCountChart();
      renderStintLengthChart();
      return;
    }
    renderChart(currentColumns);
  }

  function renderAll() {
    if (viewMode === "player" && !selectedPlayerId) {
      eyebrow.textContent = "MLN · Player Trace";
      kpiRow.innerHTML = `<div class="kpi" style="grid-column:1/-1;"><div class="kpi-label">Get started</div><div class="kpi-value" style="font-size:15px;">Search for a player above to trace their career</div></div>`;
      svg.innerHTML = "";
      teamGrid.innerHTML = "";
      return;
    }
    if (viewMode === "league") {
      renderKpis([]);
      renderLeagueCompositionChart();
      renderLeagueMovementChart();
      updateCareerScopeCount();
      renderCareerLengthChart();
      renderFranchiseCountChart();
      renderStintLengthChart();
      renderLeagueDrilldown(searchBox.value);
      return;
    }
    currentColumns = getColumns();
    renderKpis(currentColumns);
    renderChart(currentColumns);
    renderDrilldown(currentColumns, searchBox.value);
  }

  function updateSortDirButton() {
    const isAlpha = sortField === "alpha";
    sortDirLabel.textContent = isAlpha ? (sortDir === "asc" ? "A → Z" : "Z → A") : (sortDir === "desc" ? "Most" : "Least");
    sortDirIcon.textContent = sortDir === "desc" ? "↓" : "↑";
  }
  updateSortDirButton();

  sortFieldSelect.addEventListener("change", (e) => {
    sortField = e.target.value;
    updateSortDirButton();
    renderAll();
  });
  sortDirBtn.addEventListener("click", () => {
    sortDir = sortDir === "desc" ? "asc" : "desc";
    updateSortDirButton();
    renderAll();
  });

  searchBox.addEventListener("input", (e) => {
    if (viewMode === "league") renderLeagueDrilldown(e.target.value);
    else renderDrilldown(currentColumns, e.target.value);
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawChart, 100);
  });
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawChart, 100);
    }).observe(chartScroll);
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawChart, 100);
    }).observe(movementChartScroll);
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawChart, 100);
    }).observe(careerLengthScroll);
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawChart, 100);
    }).observe(franchiseCountScroll);
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawChart, 100);
    }).observe(stintLengthScroll);
  }

  refreshTierRookieLabel();
  applyModeVisibility();
  renderAll();
})();

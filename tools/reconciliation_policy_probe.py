"""
Cross-leg allocation-policy comparison for multi-out plays (follow-up to
reconciliation_corpus_probe.py / gameday-fielding-reconciliation-audit-
implementation-plan.md Task 2).

What this checks: for a multi-out chain (DP31's unassisted-carry-then-throw,
a 6-4-3's two throws, a TP's three legs, ...), reconcileLeg already pools
speed-up headroom ACROSS legs when a later leg is short on time - it drains
whichever leg is currently being solved to ITS OWN ceiling first, then only
reaches backward into an earlier leg's remaining headroom for whatever's
left over (app.js:4465-4480 speedHeadroomMs scan, 4534-4589 the backward
cascade). That's a real cross-leg pool, but it's winner-take-most: the leg
being solved spends first, an earlier leg is only touched for the overflow.

This script does NOT change any of that. It's read-only: for every resolved
(non-unresolved) multi-out chain where the real reconciler actually used a
throw-speed or carry-pace knob, it recomputes each leg's natural (honest,
default-pace) duration and physical ceiling (fastest-possible duration)
using the exact same production formulas (fielderLegDurationsMs,
THROW_SPEED_BY_POS), then asks: given the SAME total time the real
reconciler shaved off this chain, how would that total have been split
across legs under two alternative policies -

  - equal_utilization: every leg spends the same FRACTION of its own
    natural-to-ceiling headroom (proportional-to-headroom water-fill;
    mathematically the same thing as minimizing the worst per-leg
    utilization, in the unconstrained 2-leg case - there's no meaningful
    difference between "proportional" and "minimax" here, so only one
    column is reported for it).
  - throw_favored: throws absorb 3x the "eagerness" of an unassisted carry
    before the carry is touched at all (a sprint-speed change reads more to
    a viewer than a shaved throw mph, so this keeps the carry closer to its
    honest jog for as long as possible).

A play whose TODAY utilization is already close to equal_utilization needed
no help. A play where today's split is lopsided (one leg near its ceiling,
the other barely touched) is the concrete evidence for whether reordering
the pool's priority is worth doing, and by how much. Plays where any leg's
own residualMs > 0 (all pooled headroom already exhausted, still short) are
kept but flagged `unresolved` - no policy split changes those, they're a
constants/physics problem, not an allocation-order one; sort them out when
comparing.

Run from the repo root (needs Playwright + Chromium, same as
reconciliation_corpus_probe.py):
    python tools/reconciliation_policy_probe.py [--out tools/probe_output]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()
SITE_BASE = "https://mlngameday.com/"  # docs/CNAME - deep link format is app.js's parseDeepLinkGame (?game=&play=)

PROBE_JS = """(a) => {
    KMFlight.setProbeFlightTables(a.tables);
    var rows = [];
    var errors = [];
    a.plays.forEach(function (m) {
        try {
            var flight = KMFlight.resolvePlayFlight(m);
            if (!flight) return;
            var before = String(m.obc_before || "000");
            var after = String(m.obc_after || "000");
            var moves = KMFlight.deriveRunnerMoves(before, after, m.runs || 0);
            var schedule = KMFlight.throwSchedule(m, moves, flight);
            if (!schedule || !schedule.length) return;

            var outLegs = [];
            for (var i = 0; i < schedule.length; i++) if (schedule[i].out) outLegs.push(i);
            if (outLegs.length < 2) return;   // single-out plays have nothing to reallocate across

            var adjByLeg = {};
            (schedule.adjustments || []).forEach(function (adj) {
                if (adj.legIndex == null) return;
                (adjByLeg[adj.legIndex] = adjByLeg[adj.legIndex] || []).push(adj);
            });

            // Only care about chains where the reconciler actually leaned on a
            // pace/speed knob somewhere - an untouched chain has nothing to compare.
            var anyPaceKnob = Object.keys(adjByLeg).some(function (idx) {
                return adjByLeg[idx].some(function (adj) {
                    return adj.knob === "speedThrow" || adj.knob === "sprintCarry" ||
                        adj.knob === "slowThrow" || adj.knob === "easeCarry";
                });
            });
            if (!anyPaceKnob) return;

            var legs = outLegs.map(function (i) {
                var leg = schedule[i];
                var legAdjustments = adjByLeg[i] || [];
                var residualMs = 0;
                legAdjustments.forEach(function (adj) { if (adj.knob === "unresolved") residualMs = adj.ms; });
                var naturalMs = null, ceilingMs = null;
                if (leg.distFt != null && leg.throwerPos) {
                    if (leg.unassisted) {
                        naturalMs = KMFlight.fielderLegDurationsMs(m, leg.throwerPos, [{ distFt: leg.distFt }], "run", 1)[0];
                        ceilingMs = KMFlight.fielderLegDurationsMs(m, leg.throwerPos, [{ distFt: leg.distFt }], "run",
                            KMFlight.FIELDER_PACE_SCALE.run.max)[0];
                    } else if (KMFlight.THROW_SPEED_BY_POS[leg.throwerPos]) {
                        var speedRange = KMFlight.THROW_SPEED_BY_POS[leg.throwerPos];
                        naturalMs = leg.distFt / (speedRange.mph * 1.46667) * 1000;
                        ceilingMs = leg.distFt / (speedRange.max * 1.46667) * 1000;
                    }
                }
                return {
                    legIndex: i, base: leg.base, throwerPos: leg.throwerPos, unassisted: !!leg.unassisted,
                    distFt: leg.distFt, finalMs: leg.drawMs, naturalMs: naturalMs, ceilingMs: ceilingMs,
                    residualMs: Math.round(residualMs),
                    knobsUsed: legAdjustments.map(function (adj) { return adj.knob; }).join(","),
                };
            });

            rows.push({
                moment_id: m.moment_id, result: m.result, throw_order: m.throw_order || "",
                game_code: m.game_code, session_number: m.session_number, inning: m.inning, half: m.half,
                off_team_abbr: m.off_team_abbr, def_team_abbr: m.def_team_abbr,
                batter_name: m.batter_name, play_num: m.play_num,
                legs: legs,
            });
        } catch (e) {
            errors.push((m.moment_id || "?") + ": " + e.message);
        }
    });
    return { rows: rows, errors: errors };
}"""


def load_seasons() -> list[tuple[str, list[dict], dict]]:
    seasons: list[tuple[str, list[dict], dict]] = []
    for season_dir in sorted(pathlib.Path("docs/data").glob("s*")):
        km_fp = season_dir / "key_moments.json"
        meta_fp = season_dir / "meta.json"
        if not km_fp.exists() or not meta_fp.exists():
            continue
        moments = json.loads(km_fp.read_text(encoding="utf-8"))
        tables = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]
        seasons.append((season_dir.name, moments, tables))
    return seasons


def water_fill(total_ms: float, items: list[dict]) -> dict[str, float]:
    """Split total_ms across items ({name, headroom, weight}), proportional to
    weight, capping any item at its own headroom and redistributing the
    overflow among the remaining uncapped items (proportional to their
    weight) until the total is placed or every item is capped."""
    remaining_total = total_ms
    open_items = {it["name"]: dict(it) for it in items}
    alloc = {it["name"]: 0.0 for it in items}
    while remaining_total > 0.5 and open_items:
        weight_sum = sum(it["weight"] for it in open_items.values())
        if weight_sum <= 0:
            break
        share = {name: remaining_total * it["weight"] / weight_sum for name, it in open_items.items()}
        newly_capped = []
        overflow = 0.0
        for name, it in open_items.items():
            room = it["headroom"] - alloc[name]
            if share[name] >= room - 1e-9:
                alloc[name] += room
                overflow += share[name] - room
                newly_capped.append(name)
            else:
                alloc[name] += share[name]
        remaining_total = overflow
        for name in newly_capped:
            del open_items[name]
        if not newly_capped and remaining_total > 0.5:
            break  # every remaining item took its full share; nothing left to redistribute
    return alloc


def deep_link(game_code: str, play_num: int) -> str:
    play_seq = play_num % 1000
    return f"{SITE_BASE}?game={game_code}&play={play_seq}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/probe_output", help="output dir for CSVs")
    args = ap.parse_args()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    seasons = load_seasons()
    print(f"Loaded {len(seasons)} season(s).")

    all_rows: list[dict] = []
    all_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_URL)
        page.wait_for_function("window.KMFlight != null")

        for season_name, moments, tables in seasons:
            result = page.evaluate(PROBE_JS, {"plays": moments, "tables": tables})
            for row in result["rows"]:
                row["season"] = season_name
                all_rows.append(row)
            all_errors.extend(f"{season_name}:{e}" for e in result["errors"])
        browser.close()

    print(f"Found {len(all_rows)} multi-out chains with a pace/speed knob in play.")
    if all_errors:
        print(f"[{len(all_errors)}] moments raised exceptions (first 10):")
        for e in all_errors[:10]:
            print(f"  - {e}")

    out_rows = []
    for play in all_rows:
        legs = [l for l in play["legs"] if l["naturalMs"] is not None and l["ceilingMs"] is not None]
        if len(legs) < 2:
            continue  # a cutoff/relay leg with no simple mph/pace model - nothing to compare

        unresolved = any(l["residualMs"] > 0 for l in legs)
        total_used = sum(max(0.0, l["naturalMs"] - l["finalMs"]) for l in legs)
        if total_used < 0.5:
            continue  # nothing was actually shaved off this chain despite a knob "firing"

        # weight = headroom (not 1): a flat weight-1 water-fill splits the SAME
        # absolute ms across legs regardless of how much room each has, which
        # is an equal-ms policy, not an equal-UTILIZATION one. Weighting each
        # leg's first-pass share by its own headroom is what makes alloc_i /
        # headroom_i land on the same fraction k = total_used / sum(headroom)
        # for every uncapped leg - the actual equal-utilization/minimax split.
        equal_items = [{"name": l["legIndex"], "headroom": max(0.0, l["naturalMs"] - l["ceilingMs"]),
                        "weight": max(0.0, l["naturalMs"] - l["ceilingMs"])}
                       for l in legs]
        equal_alloc = water_fill(total_used, equal_items)

        throw_items = [{"name": l["legIndex"], "headroom": max(0.0, l["naturalMs"] - l["ceilingMs"]),
                        "weight": 1 if l["unassisted"] else 3}
                       for l in legs]
        throw_alloc = water_fill(total_used, throw_items)

        header = {
            "season": play["season"], "session": play["session_number"],
            "inning": play["inning"], "half": play["half"],
            "matchup": f"{play['off_team_abbr']} @ {play['def_team_abbr']}",
            "batter": play["batter_name"], "result": play["result"], "throw_order": play["throw_order"],
            "moment_id": play["moment_id"],
            "link": deep_link(play["game_code"], play["play_num"]),
            "unresolved": unresolved,
        }
        for l in legs:
            headroom = max(0.0, l["naturalMs"] - l["ceilingMs"])
            used_today = max(0.0, l["naturalMs"] - l["finalMs"])
            row = dict(header)
            row.update({
                "legIndex": l["legIndex"], "base": l["base"], "throwerPos": l["throwerPos"],
                "role": "carry" if l["unassisted"] else "throw",
                "naturalMs": round(l["naturalMs"]), "finalMs": round(l["finalMs"]),
                "ceilingMs": round(l["ceilingMs"]), "headroomMs": round(headroom),
                "usedMs_today": round(used_today),
                "utilPct_today": round(min(100.0, 100 * used_today / headroom), 1) if headroom > 0 else (
                    100.0 if used_today > 0 else 0.0),
                "usedMs_equalUtil": round(equal_alloc[l["legIndex"]]),
                "utilPct_equalUtil": round(min(100.0, 100 * equal_alloc[l["legIndex"]] / headroom), 1) if headroom > 0 else 0.0,
                "usedMs_throwFavored": round(throw_alloc[l["legIndex"]]),
                "utilPct_throwFavored": round(min(100.0, 100 * throw_alloc[l["legIndex"]] / headroom), 1) if headroom > 0 else 0.0,
                "residualMs": l["residualMs"],
            })
            out_rows.append(row)

    # Sort so the most lopsided real plays - today's per-play utilization spread -
    # surface first; that spread is what a rebalanced policy would actually change.
    by_play: dict[str, list[dict]] = {}
    for r in out_rows:
        by_play.setdefault(r["moment_id"], []).append(r)
    for rs in by_play.values():
        spread = max(r["utilPct_today"] for r in rs) - min(r["utilPct_today"] for r in rs)
        for r in rs:
            r["play_util_spread_today"] = round(spread, 1)
    out_rows.sort(key=lambda r: (-r["play_util_spread_today"], r["moment_id"], r["legIndex"]))

    if out_rows:
        fieldnames = list(out_rows[0].keys())
        with (out_dir / "policy_comparison.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(out_rows)
        print(f"Wrote {len(out_rows)} leg-rows across {len(by_play)} chains to "
              f"{out_dir}/policy_comparison.csv")
        n_unresolved = sum(1 for rs in by_play.values() if rs[0]["unresolved"])
        print(f"  {n_unresolved}/{len(by_play)} chains are 'unresolved' (headroom already exhausted - "
              f"no allocation policy changes these, filter them out when judging reallocation options)")
        spreads = sorted((rs[0]["play_util_spread_today"] for rs in by_play.values()), reverse=True)
        print(f"  today's per-play utilization spread: worst={spreads[0]:.0f}pp, "
              f"median={spreads[len(spreads)//2]:.0f}pp")
    else:
        print("No qualifying multi-out chains found - nothing to compare.")


if __name__ == "__main__":
    main()

"""
Full-corpus dump of every reconciler tuning-knob adjustment (Alex's ask,
follow-up to reconciliation_policy_probe.py/tools/reconciliation_corpus_probe.py):
policy_comparison.csv only ever looks at multi-out chains where a leg came up
genuinely short of headroom - today that's down to a single chain. But a play
can still reconcile HARD (a throw eased or sped to near its mph ceiling, a
runner stretched toward their own max-slowdown bound, a fielder's charge
capped at its slowest allowed pace) and still technically "resolve" with zero
residual - those don't show up in policy_comparison at all, and only show up
in reconciliation_corpus_probe.py's leg_rows.csv as a bare knob NAME with no
magnitude. This probe reads every adjustment (schedule.adjustments, the
throw-schedule side, AND coverage.adjustments, the base-coverage side - see
reconcileLeg/reconcileCoverage in docs/js/app.js) across the WHOLE renderable
corpus (not just multi-out plays), with its real ms/mph/pace magnitude
attached, so an "odd-looking" play that leans hard on one knob to make the
math work can be found by sorting/filtering the CSV rather than eyeballing
the animation.

Two outputs:
  reconciliation_adjustments.csv  - one row per individual knob firing (long
                                     format - every field the knob carries,
                                     nothing summarized away).
  reconciliation_play_summary.csv - one row per play, sorted by how much this
                                     play's reconciler leaned on its knobs in
                                     total (total_abs_ms desc) - the "biggest
                                     targeted reconciliations first" view.

Run from the repo root (needs Playwright + Chromium):
    python tools/reconciliation_knobs_probe.py [--out tools/probe_output]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
from collections import defaultdict

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()
SITE_BASE = "https://mlngameday.com/"

# Knobs whose "ms" is a genuine measure of how hard the knob had to work
# (unlike "unresolved", which is a residual shortfall, not a knob spend, and
# "easeCharge"/"coverEarlyBreak" which use ms:0 as a fired-flag with the real
# magnitude living in a from/to pair instead).
UNRESOLVED_KNOB = "unresolved"

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

            var plan = KMFlight.chainMoverPlan(m, flight, moves);
            var coverage = plan ? KMFlight.reconcileCoverage(m, flight, plan, schedule) : { adjustments: [] };

            var base = {
                moment_id: m.moment_id, result: m.result, throw_order: m.throw_order || "",
                game_code: m.game_code, session_number: m.session_number, inning: m.inning, half: m.half,
                off_team_abbr: m.off_team_abbr, def_team_abbr: m.def_team_abbr,
                batter_name: m.batter_name, play_num: m.play_num,
                legCount: schedule.length,
                fielder: flight.fielder, archetype: flight.archetype,
                chargePaceScale: (flight.fieldingAdjust && flight.fieldingAdjust.paceScale != null)
                    ? flight.fieldingAdjust.paceScale : "",
            };

            (schedule.adjustments || []).forEach(function (adj) {
                rows.push(Object.assign({}, base, {
                    source: "schedule", knob: adj.knob, who: adj.who, legIndex: adj.legIndex,
                    ms: adj.ms, mphFrom: adj.mphFrom, mphTo: adj.mphTo,
                    paceFrom: adj.paceFrom, paceTo: adj.paceTo,
                    paceScaleFrom: adj.paceScaleFrom, paceScaleTo: adj.paceScaleTo,
                    reason: adj.reason,
                }));
            });
            (coverage.adjustments || []).forEach(function (adj) {
                rows.push(Object.assign({}, base, {
                    source: "coverage", knob: adj.knob, who: adj.who, legIndex: adj.legIndex,
                    ms: adj.ms, mphFrom: adj.mphFrom, mphTo: adj.mphTo,
                    paceFrom: adj.paceFrom, paceTo: adj.paceTo,
                    paceScaleFrom: adj.paceScaleFrom, paceScaleTo: adj.paceScaleTo,
                    reason: adj.reason,
                }));
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


def deep_link(game_code: str, play_num: int) -> str:
    return f"{SITE_BASE}?game={game_code}&play={play_num % 1000}"


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
                row["link"] = deep_link(row["game_code"], row["play_num"])
                all_rows.append(row)
            all_errors.extend(f"{season_name}:{e}" for e in result["errors"])
        browser.close()

    print(f"Found {len(all_rows)} knob adjustments across the corpus.")
    if all_errors:
        print(f"[{len(all_errors)}] moments raised exceptions (first 10):")
        for e in all_errors[:10]:
            print(f"  - {e}")

    if not all_rows:
        print("Nothing to write.")
        return

    fieldnames = [
        "season", "moment_id", "link", "result", "throw_order", "game_code",
        "session_number", "inning", "half", "off_team_abbr", "def_team_abbr",
        "batter_name", "play_num", "legCount", "fielder", "archetype", "chargePaceScale",
        "source", "knob", "who", "legIndex", "ms", "mphFrom", "mphTo",
        "paceFrom", "paceTo", "paceScaleFrom", "paceScaleTo", "reason",
    ]
    with (out_dir / "reconciliation_adjustments.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)
    print(f"Wrote {len(all_rows)} rows to {out_dir}/reconciliation_adjustments.csv")

    # --- per-play summary, sorted by how hard the reconciler leaned in total ---
    by_play: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_play[f"{r['season']}:{r['moment_id']}"].append(r)

    summary_rows = []
    for key, rs in by_play.items():
        knobbed = [r for r in rs if r["knob"] != UNRESOLVED_KNOB]
        unresolved = [r for r in rs if r["knob"] == UNRESOLVED_KNOB]
        abs_ms_vals = [abs(r["ms"]) for r in knobbed if r["ms"] not in (None, "")]
        head = rs[0]
        summary_rows.append({
            "season": head["season"], "moment_id": head["moment_id"], "link": head["link"],
            "result": head["result"], "throw_order": head["throw_order"],
            "batter_name": head["batter_name"], "fielder": head["fielder"],
            "n_adjustments": len(knobbed),
            "distinct_knobs": ",".join(sorted(set(r["knob"] for r in knobbed))),
            "total_abs_ms": round(sum(abs_ms_vals)),
            "max_single_ms": round(max(abs_ms_vals)) if abs_ms_vals else 0,
            "had_unresolved": bool(unresolved),
            "unresolved_ms": round(sum(abs(r["ms"]) for r in unresolved)) if unresolved else 0,
            "chargePaceScale": head["chargePaceScale"],
        })
    summary_rows.sort(key=lambda r: (-r["total_abs_ms"], r["season"], r["moment_id"]))

    with (out_dir / "reconciliation_play_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)
    print(f"Wrote {len(summary_rows)} play rows to {out_dir}/reconciliation_play_summary.csv "
          f"(sorted by total_abs_ms desc)")
    print(f"  worst total_abs_ms: {summary_rows[0]['total_abs_ms']}ms "
          f"({summary_rows[0]['season']}:{summary_rows[0]['moment_id']})")


if __name__ == "__main__":
    main()

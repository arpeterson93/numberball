"""
Corpus probe (gameday-fielding-reconciliation-audit-implementation-plan.md
section 5): sweeps the full renderable corpus (docs/data/s*/key_moments.json,
~17.8k moments) through the real reconciliation pipeline - resolvePlayFlight
-> deriveRunnerMoves -> throwSchedule - and measures, per multi-out play's
individual out leg, how far the honest throw misses its own runner's margin.

Stage 5 revision: Stages 2/3/4 have landed - throwSchedule now reconciles
EVERY real out leg via reconcileChain (not just the final one), and the
slow-charge pace knob / coverage pool / sprintCarry-easeCarry knobs are all
real. This version reads schedule.adjustments/schedule.reconcileMetas
directly (both now carry one entry per leg, tagged with legIndex) instead of
the original Stage-0 truncate-and-simulate trick that only existed because
per-leg reconciliation didn't exist yet - so these are the actual production
numbers, not a preview of them. Re-run after Stage 2/3/4 per the plan's own
Stage 5 instruction, to confirm the residual rate landed under the 0.5%
floor (plan 5.3's decision rules).

Also runs the three data assertions the plan calls out as [OPEN] (section
2.7 / 9): FCLead presence in the corpus, runnerForOutTarget base-collision
uniqueness, and the full multi-out result x throw_order shape matrix (feeds
Stage 6's test enumeration) - plus the Task 4 infield-hit trigger-rate sweep
(section 4.2), now reading flight.fieldingAdjust.paceScale directly (Stage
3's real slow-charge knob) instead of re-deriving the trigger by hand.

Run from the repo root (needs Playwright + Chromium):
    python tools/reconciliation_corpus_probe.py [--out tools/probe_output]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
from collections import Counter, defaultdict

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()

PROBE_JS = """(a) => {
    KMFlight.setProbeFlightTables(a.tables);
    var legRows = [];
    var badFC = [];
    var badDup = [];
    var shapeCounts = {};
    var infieldRows = [];
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

            var recorded = (m.outs_after || 0) - (m.outs_before || 0);

            if (recorded >= 2) {
                var shapeKey = m.result + "|" + (m.throw_order || "(none)");
                shapeCounts[shapeKey] = (shapeCounts[shapeKey] || 0) + 1;
            }

            if (m.result === "FCLead") badFC.push(m.moment_id);

            // Scoped to bases that are REAL schedule out-legs (throwSchedule
            // already caps the out-leg count at `recorded` - see the
            // pre-existing "never duplicate a base among OUT-marked legs"
            // sweep in ball_flight_test.py). deriveRunnerMoves' raw OUT list
            // is often longer than that on an inning-ending play (a
            // leftover baserunner whose true fate isn't "out", just "board
            // reset to 000 because outs_after hit 3" - before/after OBC
            // diffing alone can't tell the difference) - counting those as
            // collisions would be noise unrelated to what
            // runnerForOutTarget's first-match rule actually decides.
            var realOutBases = {};
            schedule.forEach(function (t) { if (t.out) realOutBases[t.base] = true; });
            var candidatesByBase = {};
            moves.forEach(function (mv) {
                if (mv.to !== "OUT") return;
                var b = KMFlight.outMoveTargetBase(m, mv);
                if (!realOutBases[b]) return;
                (candidatesByBase[b] = candidatesByBase[b] || []).push(mv.from);
            });
            Object.keys(candidatesByBase).forEach(function (b) {
                if (candidatesByBase[b].length > 1) {
                    badDup.push(m.moment_id + ":" + b + ":" + candidatesByBase[b].join(">"));
                }
            });

            // Stage 5 revision: throwSchedule now reconciles EVERY real out
            // leg via reconcileChain (Task 2), not just the final one - read
            // the real production adjustments/metas directly (both tagged
            // with legIndex) instead of simulating. These are the actual
            // numbers the shipped code produces, not a preview.
            var adjByLeg = {};
            (schedule.adjustments || []).forEach(function (adj) {
                if (adj.legIndex == null) return;
                (adjByLeg[adj.legIndex] = adjByLeg[adj.legIndex] || []).push(adj);
            });
            for (var i = 0; i < schedule.length; i++) {
                if (!schedule[i].out) continue;
                var legAdjustments = adjByLeg[i] || [];
                var knobsUsed = legAdjustments.map(function (adj) { return adj.knob; });
                var residualMs = 0;
                legAdjustments.forEach(function (adj) { if (adj.knob === "unresolved") residualMs = adj.ms; });
                legRows.push({
                    moment_id: m.moment_id, result: m.result, throw_order: m.throw_order || "",
                    legIndex: i, legCount: schedule.length, base: schedule[i].base,
                    isFinal: (i === schedule.length - 1),
                    knobsUsed: knobsUsed.join(","), residualMs: Math.round(residualMs),
                });
            }

            // Task 4 infield-hit trigger sweep (section 4.2) - Stage 3's
            // real slow-charge knob already ran inside resolvePlayFlight
            // (resolveGrounderInterception); flight.fieldingAdjust.paceScale
            // is set exactly when the trigger fired, so read it directly
            // rather than re-deriving the trigger by hand.
            if (KMFlight.GROUND_ARCHETYPES[flight.archetype] && recorded <= 0 && flight.fieldingAdjust) {
                infieldRows.push({
                    moment_id: m.moment_id, result: m.result,
                    trips: flight.fieldingAdjust.paceScale != null,
                    paceScale: flight.fieldingAdjust.paceScale != null ? flight.fieldingAdjust.paceScale : null,
                    cappedFallback: !!flight.fieldingAdjust.cappedFallback,
                });
            }
        } catch (e) {
            errors.push((m.moment_id || "?") + ": " + e.message);
        }
    });
    return { legRows: legRows, badFC: badFC, badDup: badDup, shapeCounts: shapeCounts,
             infieldRows: infieldRows, errors: errors };
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


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/probe_output", help="output dir for CSVs")
    args = ap.parse_args()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    seasons = load_seasons()
    total_moments = sum(len(m) for _, m, _ in seasons)
    print(f"Loaded {len(seasons)} season(s), {total_moments} total moments.")

    all_leg_rows: list[dict] = []
    all_infield_rows: list[dict] = []
    all_bad_fc: list[str] = []
    all_bad_dup: list[str] = []
    shape_counts: Counter = Counter()
    all_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_URL)
        page.wait_for_function("window.KMFlight != null")
        charge_min = page.evaluate("KMFlight.FIELDER_PACE_SCALE.charge.min")

        for season_name, moments, tables in seasons:
            result = page.evaluate(PROBE_JS, {"plays": moments, "tables": tables})
            for row in result["legRows"]:
                row["season"] = season_name
                row["moment_id"] = f"{season_name}:{row['moment_id']}"
                all_leg_rows.append(row)
            for row in result["infieldRows"]:
                row["season"] = season_name
                row["moment_id"] = f"{season_name}:{row['moment_id']}"
                all_infield_rows.append(row)
            all_bad_fc.extend(f"{season_name}:{mid}" for mid in result["badFC"])
            all_bad_dup.extend(f"{season_name}:{mid}" for mid in result["badDup"])
            for k, v in result["shapeCounts"].items():
                shape_counts[k] += v
            all_errors.extend(f"{season_name}:{e}" for e in result["errors"])
        browser.close()

    print(f"Swept {total_moments} moments -> {len(all_leg_rows)} out-leg rows, "
          f"{len(all_infield_rows)} infield-hit rows.")
    if all_errors:
        print(f"\n[{len(all_errors)}] moments raised exceptions during probing (first 10):")
        for e in all_errors[:10]:
            print(f"  - {e}")

    # --- CSV outputs ---
    if all_leg_rows:
        with (out_dir / "leg_rows.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_leg_rows[0].keys()))
            w.writeheader()
            w.writerows(all_leg_rows)
    if all_infield_rows:
        with (out_dir / "infield_rows.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_infield_rows[0].keys()))
            w.writeheader()
            w.writerows(all_infield_rows)
    with (out_dir / "shape_counts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["result", "throw_order", "count"])
        for k, v in sorted(shape_counts.items(), key=lambda kv: -kv[1]):
            result, throw_order = k.split("|", 1)
            w.writerow([result, throw_order, v])

    # --- data assertions (section 2.7 / 9's [OPEN] items) ---
    print("\n--- Data assertions ---")
    print(f"FCLead occurrences in corpus: {len(all_bad_fc)}"
          + (f" (e.g. {all_bad_fc[:5]})" if all_bad_fc else " - absent, existing '2B' default left untouched"))
    print(f"runnerForOutTarget base collisions (two OUT moves -> same base): {len(all_bad_dup)}"
          + (f" (e.g. {all_bad_dup[:5]})" if all_bad_dup else " - none found, first-match rule is safe"))

    print(f"\nMulti-out shapes (result|throw_order) found: {len(shape_counts)} distinct combos")
    for k, v in sorted(shape_counts.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {k}: {v}")

    # --- per-leg residual summary (post Stage 2/3/4 - real production output) ---
    intermediate = [r for r in all_leg_rows if not r["isFinal"]]
    final = [r for r in all_leg_rows if r["isFinal"]]
    print(f"\n--- Out-leg residual summary ({len(all_leg_rows)} out legs total: "
          f"{len(intermediate)} intermediate, {len(final)} final) ---")
    for label, rows in (("intermediate", intermediate), ("final", final)):
        if not rows:
            print(f"  {label}: none")
            continue
        residuals = [r["residualMs"] for r in rows if r["residualMs"] > 0]
        pct_unresolved = 100.0 * len(residuals) / len(rows) if rows else 0.0
        print(f"  {label}: {len(rows)} legs, unresolved (residual>0) after Stage 2/3/4's real knobs: "
              f"{len(residuals)}/{len(rows)} ({pct_unresolved:.2f}%)")
        if residuals:
            print(f"    residual ms percentiles: p50={percentile(residuals, .5):.0f} "
                  f"p90={percentile(residuals, .9):.0f} worst={max(residuals):.0f}")

    knob_counts = Counter()
    for r in all_leg_rows:
        for k in (r["knobsUsed"] or "").split(","):
            if k:
                knob_counts[k] += 1
    print(f"\nKnob usage across all out legs: {dict(knob_counts)}")

    # --- decision rules (plan section 5.3) ---
    print("\n--- Decision rule check (plan 5.3) ---")
    all_out_legs = len(all_leg_rows)
    all_residuals = [r for r in all_leg_rows if r["residualMs"] > 0]
    pct_all = 100.0 * len(all_residuals) / all_out_legs if all_out_legs else 0.0
    print(f"Overall unresolved rate across ALL out legs: {len(all_residuals)}/{all_out_legs} ({pct_all:.2f}%)")
    if pct_all > 1.5:
        print("  -> exceeds 1.5%: first lever is RUNNER_LATE_JUMP_MAX_MS 400->600, "
              "then STRETCH_RUNNER_MAX_FRAC 0.15->0.20 (plan 5.3)")
    elif pct_all > 0.5:
        print("  -> above the 0.5% acceptable floor but under 1.5%: no lever pulled yet, watch it")
    else:
        print("  -> at/under the 0.5% acceptable-residual floor - per the reconciler's own philosophy, "
              "this is the correct outcome, not a bug to chase further")

    # residuals by shape, to spot clustering (5.3's DPH1 catcher-pop example)
    by_result = defaultdict(list)
    for r in all_residuals:
        by_result[r["result"]].append(r["residualMs"])
    if by_result:
        print("  Residuals clustered by result:")
        for result, vals in sorted(by_result.items(), key=lambda kv: -len(kv[1])):
            print(f"    {result}: {len(vals)} unresolved legs, worst={max(vals):.0f}ms")

    # --- infield-hit trigger sweep (Task 4 justification record) ---
    print("\n--- Infield-hit trigger sweep (plan 4.2, Task 4 justification record) ---")
    if all_infield_rows:
        tripped = [r for r in all_infield_rows if r["trips"]]
        pct_trip = 100.0 * len(tripped) / len(all_infield_rows)
        print(f"{len(tripped)}/{len(all_infield_rows)} ground-archetype no-new-out plays trip the "
              f"'honest throw beats the runner' trigger ({pct_trip:.2f}%)")
        if tripped:
            scales = [r["paceScale"] for r in tripped if r["paceScale"] is not None]
            at_min = [s for s in scales if s <= charge_min + 1e-9]
            print(f"  real demanded paceScale (Stage 3's own bisection, not re-derived): "
                  f"p50={percentile(scales,.5):.3f} p90={percentile(scales,.9):.3f} min={min(scales):.3f}")
            print(f"  bottoms out at charge.min ({charge_min}) exactly: {len(at_min)}/{len(scales)} "
                  f"({100.0*len(at_min)/len(scales):.1f}%)" +
                  (" -> widen charge.min per plan 5.3's decision rule" if len(at_min) / len(scales) > 0.5 else
                   " -> under half, charge.min holds for now"))
        both_fired = [r for r in tripped if r["cappedFallback"]]
        print(f"  disjointness check: {len(both_fired)} rows where cappedFallback was ALSO true "
              f"(should always be 0 - the code-level guard makes this structurally impossible)")
    else:
        print("  no qualifying ground-archetype/no-new-out rows found")

    print(f"\nCSV output written to {out_dir}/")


if __name__ == "__main__":
    main()

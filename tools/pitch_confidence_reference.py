"""
League reference table for the "swing confidence" read on the Expected Swing
Score (utils.swing_confidence_summary).

For a sample of pitches across many pitchers - each replayed as-of-that-pitch
with frozen history, the same way tools/pitch_likelihood_calibrate.py samples
them - this records, per metric (OBP/SLG):

  - the edge (utils.swing_confidence_stats) of the combined model's own best
    swing, scored under the generic utils.RESULT_RANGES;
  - what the model's swing and the baseline's swing would each have ACTUALLY
    scored against the pitch thrown (utils.swing_confidence_realized).

and writes tools/swing_confidence_reference.json:

  {"meta": {...},
   "obp": {"quantiles": [edge at 0th..100th pct],
           "tiers": [{"lo_pct", "hi_pct", "n", "edge_mean",
                      "model_realized", "baseline_realized"}, ...]},
   "slg": {...}}

"quantiles" turns a live edge into a league percentile; "tiers" is the
calibration table - does a higher-confidence prediction actually earn more
than the baseline swing would have? If the top tiers' model_realized isn't
clearly above baseline_realized, the percentile is noise.

Reuses pitch_likelihood_calibrate's roster scan and on-disk per-pitcher
cache, so after a calibration run this is mostly compute, not Supabase.

Usage:
    python tools/pitch_confidence_reference.py
    python tools/pitch_confidence_reference.py --points-per-pitcher 60 --max-pitchers 80
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import utils  # noqa: E402
from pitch_likelihood_backtest import (  # noqa: E402
    Config, SIGNAL_BUILDERS, DEFAULT_WEIGHTS, _prepare_step, combine_signals,
)
import pitch_likelihood_calibrate as calib  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent / "swing_confidence_reference.json"
METRICS = ("obp", "slg")


def production_config() -> Config:
    """Same knobs pages/2_Scouting.py and bot_charts.py run live."""
    return Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0,
                  leverage_width=0.5, weights=DEFAULT_WEIGHTS)


def collect(names: list[str], cfg: Config, points_per_pitcher: int, min_history: int) -> list[dict]:
    builders = list(SIGNAL_BUILDERS.values())
    rows: list[dict] = []
    t0 = time.time()
    for i, name in enumerate(names):
        df = calib.get_pitcher_df(name)
        if df.empty:
            continue
        pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
        n_kept = 0
        for cur_id in calib.sample_point_ids(df, min_history, points_per_pitcher):
            idx = pitches.index[pitches["id"] == cur_id]
            if len(idx) == 0:
                continue
            target_row = pitches.loc[int(idx[0])]
            state, baseline, signals = _prepare_step(df[df["id"] < cur_id], builders, cfg, target_row=target_row)
            if state is None or not signals:
                continue
            combined = combine_signals(baseline, signals, cfg.min_effective_n, cfg.weights)
            actual = int(target_row["pitch"])
            row = {"pitcher": name}
            for m in METRICS:
                st = utils.swing_confidence_stats(baseline, combined, utils.RESULT_RANGES, m)
                re = utils.swing_confidence_realized(baseline, combined, actual, utils.RESULT_RANGES, m)
                row[m] = {"edge": st["edge"], "model": re["model"], "baseline": re["baseline"]}
            rows.append(row)
            n_kept += 1
        print(f"  {i + 1}/{len(names)} {name}: {n_kept} points ({time.time() - t0:.0f}s)", file=sys.stderr)
    return rows


def build_reference(rows: list[dict]) -> dict:
    out: dict = {"meta": {
        "n_points": len(rows), "n_pitchers": len({r["pitcher"] for r in rows}),
        "ranges": "utils.RESULT_RANGES", "built": time.strftime("%Y-%m-%d"),
    }}
    for m in METRICS:
        edges = np.array([r[m]["edge"] for r in rows])
        model = np.array([r[m]["model"] for r in rows])
        base = np.array([r[m]["baseline"] for r in rows])
        quantiles = np.percentile(edges, np.arange(101))
        tiers = []
        for lo, hi in utils.SWING_CONFIDENCE_TIERS:
            lo_v, hi_v = quantiles[lo], quantiles[hi]
            mask = (edges >= lo_v) & ((edges <= hi_v) if hi == 100 else (edges < hi_v))
            n = int(mask.sum())
            tiers.append({
                "lo_pct": lo, "hi_pct": hi, "n": n,
                "edge_mean": float(edges[mask].mean()) if n else None,
                "model_realized": float(model[mask].mean()) if n else None,
                "baseline_realized": float(base[mask].mean()) if n else None,
            })
        out[m] = {"quantiles": [round(float(q), 5) for q in quantiles], "tiers": tiers}
    return out


def print_tiers(ref: dict) -> None:
    print(f"\n{ref['meta']['n_points']} points, {ref['meta']['n_pitchers']} pitchers")
    for m in METRICS:
        print(f"\n{m.upper()} - league edge percentile tier -> realized score")
        print(f"  {'tier':>9} {'n':>6} {'edge':>8} {'model':>8} {'baseline':>9} {'lift':>8}")
        for t in ref[m]["tiers"]:
            if not t["n"]:
                continue
            lift = t["model_realized"] - t["baseline_realized"]
            print(f"  {t['lo_pct']:>3}-{t['hi_pct']:<5} {t['n']:>6} {t['edge_mean']:>8.3f} "
                  f"{t['model_realized']:>8.3f} {t['baseline_realized']:>9.3f} {lift:>+8.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-bf", type=int, default=100, help="Minimum batters faced for a pitcher to be sampled")
    ap.add_argument("--points-per-pitcher", type=int, default=60)
    ap.add_argument("--min-history", type=int, default=100)
    ap.add_argument("--max-pitchers", type=int, default=0, help="Cap the roster (0 = everyone)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    names = calib.load_roster(args.min_bf)
    if args.max_pitchers and len(names) > args.max_pitchers:
        rng = np.random.default_rng(args.seed)
        names = sorted(rng.choice(names, size=args.max_pitchers, replace=False).tolist())
    print(f"Sampling {len(names)} pitchers x up to {args.points_per_pitcher} points...", file=sys.stderr)

    rows = collect(names, production_config(), args.points_per_pitcher, args.min_history)
    if not rows:
        print("No points produced.", file=sys.stderr)
        sys.exit(1)
    ref = build_reference(rows)
    OUT_PATH.write_text(json.dumps(ref))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)
    print_tiers(ref)


if __name__ == "__main__":
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import local_plays
    local_plays.enable()
    main()

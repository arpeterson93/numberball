"""
League-wide cooldown rate tables at every bucket size the Scouting page's
Pitch Zone Cooldown / Delta Cooldown sliders offer (bucket 50/100/125/200/
250/500 = radius 25/50/62.5/100/125/250).

COOLDOWN_LEAGUE_RATES / DELTA_COOLDOWN_LEAGUE_RATES in
pitch_likelihood_backtest.py are only valid at radius 50 (what the model
runs at). The cooldown CDF charts' league overlay needs the table fit at the
SAME radius as the pitcher's own curve, so this writes
tools/cooldown_league_rates_by_radius.json:

  {"meta": {...},
   "pitch":       {"25.0": {category: {k: P(resolves in exactly k)}}, ...},
   "delta":       {...}}

Same pooling as pitch_likelihood_calibrate.py fit-cooldown-table (train+val
pitchers from the calibration state file, via the same league_cooldown_rates).

Usage:
    python tools/cooldown_league_tables.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pitch_likelihood_calibrate as calib  # noqa: E402
import utils  # noqa: E402

BUCKETS = [50, 100, 125, 200, 250, 500]
OUT_PATH = Path(__file__).resolve().parent / "cooldown_league_rates_by_radius.json"


def lag_profiles(names: list[str]) -> dict:
    """League per-category mean/median distance-from-trigger by lag (and the
    pooled no-information null), pooling every trigger across the roster -
    the overlay utils.cooldown_lag_distance_chart draws as "League mean"."""
    out: dict = {}
    for domain, col in (("pitch", "pitch"), ("delta", "pitch_circ_delta")):
        pooled_D: dict[str, list] = {}
        pooled_null: dict[str, list] = {}
        for name in names:
            df = calib.get_pitcher_df(name)
            if df.empty or col not in df.columns:
                continue
            for cat, blob in utils.cooldown_lag_distances(df, col).items():
                pooled_D.setdefault(cat, []).append(blob["D"])
                pooled_null.setdefault(cat, []).append(blob["null"])
        out[domain] = {}
        for cat, parts in pooled_D.items():
            D = np.vstack(parts)
            null = np.concatenate(pooled_null[cat])
            out[domain][cat] = {
                "mean": [None if np.isnan(v) else round(float(v), 2) for v in np.nanmean(D, axis=0)],
                "median": [None if np.isnan(v) else round(float(v), 2) for v in np.nanmedian(D, axis=0)],
                "null": round(float(null.mean()), 2), "n": int(D.shape[0]),
            }
            print(f"  lag profile {domain}/{cat}: n={D.shape[0]}, null={null.mean():.1f}, "
                  f"lag1={np.nanmean(D[:, 0]):.1f}", file=sys.stderr)
    return out


def main() -> None:
    state = calib.load_state()
    names = state["train_pitchers"] + state["val_pitchers"]
    out: dict = {"meta": {"n_pitchers": len(names), "built": time.strftime("%Y-%m-%d"), "n": {}},
                 "pitch": {}, "delta": {}}
    t0 = time.time()
    for bucket in BUCKETS:
        radius = bucket / 2
        for domain, col in (("pitch", "pitch"), ("delta", "pitch_circ_delta")):
            pmf, n_by_cat = calib.league_cooldown_rates(names, col, radius)
            out[domain][str(float(radius))] = {
                cat: {str(k): round(v, 5) for k, v in sorted(dist.items())} for cat, dist in pmf.items()}
            out["meta"]["n"][f"{domain}@{radius}"] = n_by_cat
            print(f"  bucket {bucket} ({domain}): n={n_by_cat} ({time.time() - t0:.0f}s)", file=sys.stderr)
    out["lag_profile"] = lag_profiles(names)
    OUT_PATH.write_text(json.dumps(out))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()

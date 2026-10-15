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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pitch_likelihood_calibrate as calib  # noqa: E402

BUCKETS = [50, 100, 125, 200, 250, 500]
OUT_PATH = Path(__file__).resolve().parent / "cooldown_league_rates_by_radius.json"


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
    OUT_PATH.write_text(json.dumps(out))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()

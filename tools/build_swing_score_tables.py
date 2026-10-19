"""Build tools/swing_score_tables.json - league-realistic "score by diff" curves for evaluating suggested swings.

The old evaluation table (pitch_likelihood_calibrate.DEFAULT_SWING_RANGES) was the calculator's output for one made-up
matchup (every stat 3, R/R, bases empty, no outs). The Diff Distribution Lab (tools/diff_lab) instead stacked the exact
calculator ranges of EVERY historical league swing - each swing's own pitcher, batter, outs and runners - into the
league's result mix at every diff d (cache/model.pkl: P8 pools all 24 outs-and-bases states into 8 result classes, PN
keeps the states and the full result list). This turns that mix into the two scores a swing is judged on:

    obp(d) = P(on base | circular diff d)         classes HR, 3B, 2B, 1B (incl. IF1B and the WH ranges), BB
    slg(d) = expected bases at diff d, with utils._SLG_WEIGHTS (HR 4, 3B 3, 2B 2, 1B/IF1B 1, BB 1)

Each is a length-501 array indexed by diff. Two versions are stored: "pooled" (all states, weighted by swings) and
"state_000" (0 outs, bases empty - the calculator state the old neutral table used). "old_neutral" is the previous
table's own curves, kept for a like-for-like comparison.

Run:  python -I tools/build_swing_score_tables.py      (needs tools/diff_lab/cache/model.pkl; rebuild after a lab rebuild)
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import utils  # noqa: E402

OUT = ROOT / "tools/swing_score_tables.json"
MODEL = ROOT / "tools/diff_lab/cache/model.pkl"
META = ROOT / "tools/diff_lab/cache/state_meta.pkl"
CLASS_W = {"obp": [1, 1, 1, 1, 1, 0, 0, 0], "slg": [4, 3, 2, 1, 1, 0, 0, 0]}   # HR 3B 2B 1B BB K GO other-out


def _scores(mix: np.ndarray) -> dict:
    """mix: (8 classes, 501 diffs) share of swings landing on each class at each diff."""
    col = mix.sum(axis=0)
    mix = mix / np.where(col > 0, col, 1.0)             # make every diff's classes sum to exactly 1
    return {m: (np.asarray(w, float)[:, None] * mix).sum(axis=0).tolist() for m, w in CLASS_W.items()}


def main():
    MD = pickle.load(open(MODEL, "rb"))
    GC = pickle.load(open(META, "rb"))["GC"]
    simple = {"HR": 0, "3B": 1, "2B": 2, "2BWH": 2, "1B": 3, "1BWH": 3, "1BWH2": 3, "IF1B": 3, "BB": 4, "K": 5}
    for c in ["GORA", "FC", "FC3rd", "FCH", "GO", "DP", "DP21", "DP31", "DPH1"]:
        simple[c] = 6
    cls = [simple.get(g, 7) for g in GC]
    pn, nn = MD["PN"].astype(float), MD["nn"].astype(float)             # (24 states, 24 results, 501), swings per state
    state0 = np.zeros((8, 501))
    for r, c in enumerate(cls):
        state0[c] += pn[0, r]
    out = {"pooled": _scores(MD["P8"].astype(float)), "state_000": _scores(state0), "swings_per_state": nn.tolist()}
    from pitch_likelihood_calibrate import DEFAULT_SWING_RANGES
    out["old_neutral"] = {m: utils._diff_score_array(DEFAULT_SWING_RANGES, m).tolist() for m in ("obp", "slg")}
    OUT.write_text(json.dumps(out))
    w = np.r_[1.0, np.full(499, 2.0), 1.0] / 1000.0                       # chance weight of each diff 0..500
    print(f"wrote {OUT}")
    print(f"{'table':<12} {'chance OBP':>10} {'chance SLG':>10}   obp(d) at d=0 / 100 / 200 / 300 / 400 / 500")
    for k in ("old_neutral", "pooled", "state_000"):
        o, s = np.array(out[k]["obp"]), np.array(out[k]["slg"])
        print(f"{k:<12} {(o * w).sum():>10.4f} {(s * w).sum():>10.4f}   "
              + " / ".join(f"{o[d]:.2f}" for d in (0, 100, 200, 300, 400, 500)))
    print("swings per state (0 outs: empty, 1B, 2B, 3B, 1&2, 1&3, 2&3, loaded):", [int(x) for x in nn[:8]])


if __name__ == "__main__":
    main()

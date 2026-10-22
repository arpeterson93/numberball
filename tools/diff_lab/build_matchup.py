"""Step 2c: each player's own result-by-diff distribution from the range calculator -> cache/matchup.pkl

    python tools/diff_lab/build_matchup.py

For every hitter in a season, keep the hitter's attributes and run the exact sheet calculator against every
pitcher who threw a modeled swing that season; stack the resulting result ranges, weighting each pitcher by the
batters he faced. That gives the hitter his own result mix at every diff. The same is done for every pitcher
against all the hitters he faced (weighted by plate appearances). Neutral state: bases empty, no outs, normal
play. Players with the same hand and attributes in a season share one curve. Stored as shares in 10-wide diff buckets (51 x 8 simple classes, one byte each).
"""
import os, sys, pickle, collections
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
import hnr_audit as ha

CACHE = HERE / "cache"
MERGE = {"DPRun": "DP", "TP": "DP", "LODP": "DP"}
SIMPLE = {"HR": 0, "3B": 1, "2B": 2, "2BWH": 2, "1B": 3, "1BWH": 3, "1BWH2": 3, "IF1B": 3, "BB": 4, "K": 5,
          "SacF": 7, "DSacF": 7, "FO": 7, "DFO": 7, "PO": 7}
for c in ["GORA", "FC", "FC3rd", "FCH", "GO", "DP", "DP21", "DP31", "DPH1"]:
    SIMPLE[c] = 6
BW = 10
NB = 51        # buckets 0-9 ... 500 (the last holds only diff 500 and 490-499 share the one before)


def class_vec(key):
    """Simple-class id at every diff 0..500 (-1 = a gap or a result the page does not model)."""
    v = np.full(501, -1, dtype=np.int8)
    for name, lo, hi in ha.ranges_for(key):
        c = SIMPLE.get(MERGE.get(name, name))
        v[max(lo, 0):min(hi, 500) + 1] = -1 if c is None else c
    return v


def main():
    plays, pl = ha.load()
    R = pd.read_pickle(CACHE / "state_rows.pkl")
    pid = plays.set_index("id")[["batter_id", "pitcher_id"]]
    R = R.join(pid, on="pid")
    out = {"bat": {}, "pit": {}, "curves": {"bat": [], "pit": []}}
    for season, g in R.groupby("season"):
        pa = g.groupby("batter_id").size()
        bf = g.groupby("pitcher_id").size()
        names = {"bat": g.groupby("batter_id").bat.first(), "pit": g.groupby("pitcher_id").pit.first()}

        def rec(i, side):
            try:
                r = pl.loc[(int(season), int(i))]
            except KeyError:
                return None
            cols = ["con", "eye", "pwr", "spd"] if side == "bat" else ["mov", "cmd", "vel", "awr"]
            if r[cols].isna().any():
                return None
            return r
        bats = {i: rec(i, "bat") for i in pa.index}
        pits = {i: rec(i, "pit") for i in bf.index}
        bats = {i: r for i, r in bats.items() if r is not None}
        pits = {i: r for i, r in pits.items() if r is not None}
        # players with the same hand and attributes share one curve; weight each distinct build by its total BF / PA
        bk_of = {i: (r["hand"], int(r["con"]), int(r["eye"]), int(r["pwr"]), int(r["spd"])) for i, r in bats.items()}
        pk_of = {i: (r["hand"], int(r["mov"]), int(r["cmd"]), int(r["vel"]), int(r["awr"])) for i, r in pits.items()}
        wB = collections.Counter(); wP = collections.Counter()
        for i, k in bk_of.items():
            wB[k] += pa[i]
        for i, k in pk_of.items():
            wP[k] += bf[i]
        acc_b = {k: np.zeros((8, 501)) for k in wB}
        acc_p = {k: np.zeros((8, 501)) for k in wP}
        for bkey in wB:
            for pkey in wP:
                key = (pkey[0], *pkey[1:], bkey[0], *bkey[1:], (None, None, None), 0, False, False)
                v = class_vec(key)
                ok = np.nonzero(v >= 0)[0]
                # the hitter faces this pitcher in proportion to the pitcher's batters faced, and vice versa
                acc_b[bkey][v[ok], ok] += wP[pkey]
                acc_p[pkey][v[ok], ok] += wB[bkey]

        def shrink(a):
            bk = np.zeros((8, NB))
            for k in range(NB):
                lo, hi = k * BW, min(k * BW + BW, 501)
                bk[:, k] = a[:, lo:hi].mean(1)
            return np.round(bk * 255).astype(np.uint8)
        for side, acc, w, kof, nm in (("bat", acc_b, wP, bk_of, names["bat"]), ("pit", acc_p, wB, pk_of, names["pit"])):
            tot = sum(w.values())
            idx = {}
            for k, a in acc.items():
                idx[k] = len(out["curves"][side])
                out["curves"][side].append(shrink(a / max(tot, 1)))
            for i, k in kof.items():
                out[side][f"{int(season)}|{nm.get(i)}"] = idx[k]
        print("season", season, len(bats), "hitters x", len(pits), "pitchers", flush=True)
    pickle.dump(out, open(CACHE / "matchup.pkl", "wb"))
    print({k: len(v) for k, v in out.items() if k != "curves"}, {k: len(v) for k, v in out["curves"].items()})


if __name__ == "__main__":
    main()

"""Step 2b: calculator-model result mix by diff -> cache/model.pkl

    python tools/diff_lab/build_model.py

For every swing in state_rows.pkl the exact sheet calculator gives that swing's full set of result ranges (from its
pitcher, batter, outs and runner speeds). Stacking those ranges, the share of swings whose ranges put diff d on
result r is the league's result mix at d, with no smoothing. Done per outs-and-bases state (24 states) in normal
mode, and in hit-and-run mode for the states where a call is possible (runner on 1st and/or 2nd, under 2 outs).
Also writes the same stack for all states pooled in the 8 simple classes, plus a per-season agreement check
against the observed mix.
"""
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import warnings, logging, pickle, collections
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
import hnr_audit as ha

CACHE = HERE / "cache"
M = pickle.load(open(CACHE / "state_meta.pkl", "rb")); GC = M["GC"]; NG = len(GC)
GI = {g: i for i, g in enumerate(GC)}
MERGE = {"DPRun": "DP", "TP": "DP", "LODP": "DP"}
OBC2BRC = {"000": 0, "001": 1, "010": 2, "100": 3, "011": 4, "101": 5, "110": 6, "111": 7}
HNR_BRC = (1, 2, 4, 5)
D = 501

R = pd.read_pickle(CACHE / "state_rows.pkl")
plays, pl = ha.load()
plays = plays.set_index("id")
R = R.set_index("pid", drop=False)


def key_for(r, se):
    """The calculator inputs for a swing (same as hnr_audit.build), or None if the stats are missing."""
    try:
        b = pl.loc[(se, int(r.batter_id))]
        p = pl.loc[(se, int(r.pitcher_id))]
    except KeyError:
        return None
    if b[["con", "eye", "pwr", "spd"]].isna().any() or p[["mov", "cmd", "vel", "awr"]].isna().any():
        return None
    rs = []
    for val in (r.on_first, r.on_second, r.on_third):
        i = ha.rid(val)
        if i is None:
            rs.append(None)
        else:
            try:
                sp = pl.loc[(se, i)]["spd"]
                rs.append(3 if sp != sp else int(sp))
            except KeyError:
                rs.append(3)
    return (p["hand"], int(p["mov"]), int(p["cmd"]), int(p["vel"]), int(p["awr"]), b["hand"], int(b["con"]),
            int(b["eye"]), int(b["pwr"]), int(b["spd"]), tuple(rs), int(r.outs))


def label_vec(base, hnr):
    """Result class id at every diff 0..500 (-1 where the ranges leave a gap)."""
    v = np.full(D, -1, dtype=np.int16)
    for name, lo, hi in ha.ranges_for(base + (hnr, False)):
        g = GI.get(MERGE.get(name, name))
        if g is None:
            g = -2                                           # a result the page does not model
        v[max(lo, 0):min(hi, 500) + 1] = g
    return v


def main():
    sub = plays.loc[R.pid.values]
    groups = collections.defaultdict(int)                   # (state, key) -> swings
    miss = 0
    for r, se, s8 in zip(sub.itertuples(index=False), R.season.values, (R.outs * 8 + R.brc).values):
        k = key_for(r, int(se))
        if k is None:
            miss += 1
            continue
        groups[(int(s8), k)] += 1
    print(f"{len(groups)} distinct (state, matchup) cells, {miss} swings without full stats")
    cn = np.zeros((24, NG, D), np.float64); nn = np.zeros(24)
    ch = np.zeros((24, NG, D), np.float64); nh = np.zeros(24)
    gap = 0.0
    for n_done, ((s8, k), w) in enumerate(groups.items()):
        v = label_vec(k, False)
        ok = v >= 0
        gap += w * (~ok).sum()
        cn[s8, v[ok], np.nonzero(ok)[0]] += w
        nn[s8] += w
        if s8 % 8 in HNR_BRC and s8 // 8 < 2:
            v2 = label_vec(k, True)
            ok2 = v2 >= 0
            ch[s8, v2[ok2], np.nonzero(ok2)[0]] += w
            nh[s8] += w
        if n_done % 5000 == 0:
            print(n_done, flush=True)
    print("share of (swing, diff) pairs with no / unmodeled result:", gap / (nn.sum() * D))
    PN = cn / np.maximum(nn, 1)[:, None, None]
    PH = ch / np.maximum(nh, 1)[:, None, None]
    # pooled simple classes (HR 3B 2B 1B BB K GO Air) over all states
    SIMPLE = {"HR": 0, "3B": 1, "2B": 2, "2BWH": 2, "1B": 3, "1BWH": 3, "1BWH2": 3, "IF1B": 3, "BB": 4, "K": 5,
              "SacF": 7, "DSacF": 7, "FO": 7, "DFO": 7, "PO": 7}
    for c in ["GORA", "FC", "FC3rd", "FCH", "GO", "DP", "DP21", "DP31", "DPH1"]:
        SIMPLE[c] = 6
    P8 = np.zeros((8, D))
    for g, name in enumerate(GC):
        P8[SIMPLE[name]] += cn[:, g, :].sum(0)
    P8 /= max(nn.sum(), 1)
    pickle.dump(dict(PN=PN.astype(np.float32), PH=PH.astype(np.float32), P8=P8.astype(np.float32), nn=nn, nh=nh),
                open(CACHE / "model.pkl", "wb"))
    print("wrote model.pkl; swings per state:", nn.astype(int).tolist())


if __name__ == "__main__":
    main()

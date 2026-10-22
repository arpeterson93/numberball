"""Step 2d: state-aware own matchup expectation for every swing -> cache/own_state.pkl

    python tools/diff_lab/build_own_state.py

For each swing the exact sheet calculator is run in the swing's real situation (outs, the runners' speeds, and a hit
and run where the evidence says one was on) for the hitter's own attributes against every pitcher in MLN that season
(weighted by batters faced), and for the pitcher's own attributes against every hitter (weighted by plate
appearances). The result is each swing's probability of each result class under the player's own build, i.e. the
state-aware version of build_matchup.py. build_artifact.py turns the probabilities into expected runs and WPA with
the same run-value and win-probability tables as the league mix.
"""
import os, sys, pickle, collections, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools")); sys.path.insert(0, str(HERE))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
import hnr_audit as ha
import build_model as bm

CACHE = HERE / "cache"
GC, GI, NG, MERGE = bm.GC, bm.GI, bm.NG, bm.MERGE
D = 501
HNR_BRC = (1, 2, 4, 5)


def vec(pb, hb, rs, outs, hnr):
    """Result class id at every diff 0..500 for one pitcher build vs one hitter build (-1 gap / unmodeled)."""
    out = ha.calc.compute(pit_hand=pb[0], pit_mov=pb[1], pit_cmd=pb[2], pit_vel=pb[3], pit_awr=pb[4], bat_hand=hb[0],
                          bat_con=hb[1], bat_eye=hb[2], bat_pow=hb[3], bat_spd=hb[4], runner_speeds=rs, outs=outs,
                          hit_and_run=hnr, infield_in=False)
    v = np.full(D, -1, dtype=np.int16)
    for name, w, lo, hi in out["swing_ranges"]:
        g = GI.get(MERGE.get(name, name))
        v[max(lo, 0):min(hi, 500) + 1] = -1 if g is None else g
    return v


def main():
    R = bm.R
    sub = bm.plays.loc[R.pid.values]
    keys = []
    for r, se in zip(sub.itertuples(index=False), R.season.values):
        keys.append(bm.key_for(r, int(se)))
    # hit-and-run evidence swings (same rule as build_artifact.py)
    aud = pd.read_pickle(CACHE / "hnr_audit.pkl")
    aud = aud[((aud.obc.str[2] == "1") | (aud.obc.str[1] == "1")) & (aud.outs < 2)]
    f1 = aud[(aud.result == "1BWH") & aud.n.isin(["1B", "1BWH2"]) & (aud.h == "1BWH")].id
    f2 = aud[(aud.result == "2BWH") & (aud.n == "2B") & (aud.h == "2BWH")].id
    hf = set(int(i) for i in f1) | set(int(i) for i in f2)
    n = len(R)
    PB = np.zeros((n, NG), np.float32); PP = np.zeros((n, NG), np.float32)      # normal play
    HB = {}; HP = {}                                                           # hit-and-run variant (evidence swings)
    seasons = R.season.values; dif = R.d.values; pids = R.pid.values
    t0 = time.time(); calls = 0
    for se in sorted(set(seasons)):
        rows = [i for i in range(n) if seasons[i] == se and keys[i] is not None]
        wP = collections.Counter(); wB = collections.Counter()
        for i in rows:
            k = keys[i]
            wP[k[0:5]] += 1; wB[k[5:10]] += 1
        totP, totB = sum(wP.values()), sum(wB.values())
        groups = collections.defaultdict(list)
        for i in rows:
            k = keys[i]
            elig = int(pids[i]) in hf and R.brc.values[i] in HNR_BRC and R.outs.values[i] < 2
            groups[(k[10], k[11], False)].append(i)
            if elig:
                groups[(k[10], k[11], True)].append(i)
        for (rs, outs, hnr), idx in groups.items():
            idx = np.array(idx)
            d = dif[idx]
            if hnr:
                tgtb = np.zeros((len(idx), NG), np.float32); tgtp = np.zeros((len(idx), NG), np.float32)
            else:
                tgtb = PB[idx]; tgtp = PP[idx]
            byh = collections.defaultdict(list); byp = collections.defaultdict(list)
            for j, i in enumerate(idx):
                k = keys[i]
                byh[k[5:10]].append(j); byp[k[0:5]].append(j)
            for hb, js in byh.items():
                js = np.array(js)
                for pb, w in wP.items():
                    v = vec(pb, hb, rs, outs, hnr); calls += 1
                    c = v[d[js]]; m = c >= 0
                    tgtb[js[m], c[m]] += w / totP
            for pb, js in byp.items():
                js = np.array(js)
                for hb, w in wB.items():
                    v = vec(pb, hb, rs, outs, hnr); calls += 1
                    c = v[d[js]]; m = c >= 0
                    tgtp[js[m], c[m]] += w / totB
            if hnr:
                for j, i in enumerate(idx):
                    HB[int(i)] = tgtb[j]; HP[int(i)] = tgtp[j]
            else:
                PB[idx] = tgtb; PP[idx] = tgtp
        print("season", se, "swings", len(rows), "calls so far", calls, f"{time.time() - t0:.0f}s", flush=True)
    have = np.array([k is not None for k in keys])
    pickle.dump(dict(PB=PB, PP=PP, HB=HB, HP=HP, have=have), open(CACHE / "own_state.pkl", "wb"))
    print("wrote own_state.pkl;", int(have.sum()), "of", n, "swings;", len(HB), "hit-and-run variants")


if __name__ == "__main__":
    main()

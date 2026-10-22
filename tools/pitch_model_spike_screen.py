"""Candidate extra spikes tested INSIDE the deployed pitch-likelihood model on held-out pitchers (no model code is touched).

tools/pitch_signal_screen.py scores candidate spikes against an older, weaker stored model (no number-habit signals, no
exact-value mixture), so its gains can be overstated. This tool uses the same saved walks / split / fitted weights as
tools/pitch_model_ablation.py - the deployed pool plus the real mixture stage - and ADDS one extra point-mass spike at a
time to that stage, with the same online per-pitcher rate (hits / n shrunk toward a league rate with strength m_spike).
League rates for the new references are the pooled hit rate over the TRAIN pitchers' own histories.

Candidates (each is "the next pitch equals ..."; a reference only counts within the same game, like the real spikes):
  prev_pitch2       the pitch before last (control: dropped from the model, change -0.0001 [-0.0016, +0.0016])
  prev_diff         the previous PA's diff (circular |pitch - swing|; 0 is the wheel spot 1000)
  prev_pitch_p500   previous pitch + 500 (the opposite side of the wheel)
  prev_swing_p500   previous swing + 500
  prev_pitch_rev    previous pitch with its 3 digits reversed
Change is vs the deployed model; NEGATIVE = adding the spike helps.

Usage: python -I tools/pitch_model_spike_screen.py [steps_per_pitcher]     (default 400; ~16k steps from the 51 held-out pitchers)

Result (2026-10-22; deployed model log-loss 6.6547; change when ADDED, 95% pitcher-bootstrap CI):
  + prev_pitch2                      -0.0008 [-0.0022, +0.0006]   (confirms dropping it was right)
  + prev_diff                        -0.0054 [-0.0096, -0.0017]
  + prev_pitch_p500                  -0.0053 [-0.0098, -0.0017]
  + prev_swing_p500                  -0.0009 [-0.0024, +0.0004]
  + prev_pitch_rev                   -0.0005 [-0.0021, +0.0008]
  + prev_diff + prev_pitch_p500      -0.0106 [-0.0177, -0.0051]   (55% of pitchers improve)
Not yet added to the deployed model (tools/pitch_likelihood_backtest.apply_mixtures).
"""
from __future__ import annotations

import json
import math
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, "C:/Users/Alex/PycharmProjects/numberball")
sys.path.insert(0, "C:/Users/Alex/PycharmProjects/numberball/tools")
import pitch_model_fit as pf  # noqa: E402
import pitch_model_ablation as ab  # noqa: E402

EPS = 1e-12
GRID = 1000
STEPS_PER_PITCHER = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def _circ(a, b):
    d = np.abs(a - b) % 1000
    return np.minimum(d, 1000 - d)


def _wheel(x):
    return np.where(x == 0, 1000.0, x)


def _rev3(x):
    out = np.full(len(x), np.nan)
    for i, v in enumerate(x):
        if v == v and 1 <= v <= 1000:
            out[i] = int(str(int(v)).zfill(3)[::-1])
    return out


def _p500(x):
    out = np.full(len(x), np.nan)
    ok = (x == x) & (x >= 1) & (x <= 1000)
    out[ok] = (x[ok] + 500 - 1) % 1000 + 1
    return out


class Refs:
    """Per-walk reference tables: for each reference, val[j] = the value predicted for the pitch `lag` after pitch j, and
    cumulative hit / defined counts so the online rate at any step is O(1)."""

    LAG = {"prev_pitch": 1, "prev_swing": 1, "prev_pitch2": 2, "prev_diff": 1, "prev_pitch_p500": 1,
           "prev_swing_p500": 1, "prev_pitch_rev": 1}

    def __init__(self, pv, sw, gm):
        n = len(pv)
        pv = pv.astype(float)
        d = _circ(pv, sw)                                    # NaN where the swing is missing
        vals = {
            "prev_pitch": pv.copy(), "prev_swing": sw.copy(), "prev_pitch2": pv.copy(),
            "prev_diff": np.where(d == d, _wheel(d), np.nan), "prev_pitch_p500": _p500(pv),
            "prev_swing_p500": _p500(sw), "prev_pitch_rev": _rev3(pv),
        }
        self.val, self.cum_hit, self.cum_def, self.gm = vals, {}, {}, gm
        for name, v in vals.items():
            lag = self.LAG[name]
            hit = np.zeros(n)
            dfd = np.zeros(n)
            if n > lag:
                ok = (v[:-lag] == v[:-lag]) & (gm[lag:] == gm[:-lag])
                dfd[:-lag] = ok
                hit[:-lag] = ok & (pv[lag:] == v[:-lag])
            self.cum_hit[name], self.cum_def[name] = np.cumsum(hit), np.cumsum(dfd)

    def at(self, name, t, tgt_game):
        """(reference value now, hits so far, defined so far) for the pitch at index t; value NaN if not applicable."""
        lag = self.LAG[name]
        i = t - 1 - lag                                      # last pair fully inside the history
        h = self.cum_hit[name][i] if i >= 0 else 0.0
        dd = self.cum_def[name][i] if i >= 0 else 0.0
        j = t - lag
        val = self.val[name][j] if j >= 0 and self.gm[j] == tgt_game else np.nan
        return val, h, dd


def mixture(c, refs: Refs, t, tgt, e, m, league, use, exact_hist):
    own = np.bincount((exact_hist - 1) % GRID, minlength=GRID).astype(float)
    q = (1 - e) * c + e * own / own.sum()
    total, add = 0.0, np.zeros(GRID)
    for name in use:
        val, h, dd = refs.at(name, t, tgt)
        if val != val:
            continue
        rate = (h + m * league[name]) / (dd + m)
        total += rate
        add[int(val) - 1] += rate
    return (1 - total) * q + add


def main():
    sp = json.loads((pf.OUT / "split.json").read_text())
    fr = json.loads((pf.OUT / "fit_result.json").read_text())
    tables = pf._train_tables(sp["train"])
    allnames = pf.NAMES + pf.LEGACY
    names = pf.NEW9
    rows_all = [allnames.index(n) for n in names]
    w_full = np.array([fr["weights"][n] for n in names])
    e, m = fr["habit"]["e_exact"], fr["habit"]["m_spike"]
    league = dict(tables["spike_rates"])
    BASE = ["prev_pitch", "prev_swing"]
    CANDS = ["prev_pitch2", "prev_diff", "prev_pitch_p500", "prev_swing_p500", "prev_pitch_rev"]

    # league rates for the new references: pooled hit rate over the TRAIN pitchers' own histories
    tot_h = {n: 0.0 for n in Refs.LAG}
    tot_d = {n: 0.0 for n in Refs.LAG}
    for name in sp["train"]:
        if not (pf.OUT / f"{pf._safe(name)}.npz").exists():
            continue
        wk = pf.Walk(name)
        rf = Refs(wk.pitch.astype(int), wk.swing.astype(float), wk.game)
        for n in Refs.LAG:
            tot_h[n] += rf.cum_hit[n][-1]
            tot_d[n] += rf.cum_def[n][-1]
    mine = {n: tot_h[n] / max(tot_d[n], 1) for n in Refs.LAG}
    print("league rates (pooled train hit rate): " + ", ".join(f"{n} {mine[n] * 1000:.2f}/1000" for n in Refs.LAG))
    print(f"table values for the deployed spikes: prev_pitch {league['prev_pitch'] * 1000:.2f}/1000, "
          f"prev_swing {league['prev_swing'] * 1000:.2f}/1000")
    for n in CANDS:
        league[n] = mine[n]

    variants = {"FULL (deployed)": BASE}
    for cname in CANDS:
        variants[f"+ {cname}"] = BASE + [cname]
    variants["+ prev_diff + prev_pitch_p500"] = BASE + ["prev_diff", "prev_pitch_p500"]
    variants["+ prev_diff + prev_pitch_p500 + prev_swing_p500"] = BASE + ["prev_diff", "prev_pitch_p500", "prev_swing_p500"]

    rng = np.random.default_rng(9)
    loss = {k: [] for k in variants}
    who = []
    checked = 0
    for name in sp["test"]:
        if not (pf.OUT / f"{pf._safe(name)}.npz").exists():
            continue
        wk = pf.Walk(name)
        rf = Refs(wk.pitch.astype(int), wk.swing.astype(float), wk.game)
        picks = sorted(rng.choice(len(wk.steps), min(STEPS_PER_PITCHER, len(wk.steps)), replace=False))
        for si in picks:
            t = int(wk.steps[si])
            kk = int(wk.pitch[t]) - 1
            tgt = float(wk.game[t])
            c = np.exp(pf.pool_logp(wk.B[si:si + 1], wk.R[si:si + 1], w_full, rows_all))[0]
            hist_pv = wk.pitch[:t].astype(int)
            for lab, use in variants.items():
                q = mixture(c, rf, t, tgt, e, m, league, use, hist_pv)
                loss[lab].append(-math.log(q[kk] + EPS))
            if checked < 30:                                   # my base mixture must equal the ablation tool's copy
                q_ref = ab.mixtures(c, hist_pv, wk.swing[:t].astype(float), wk.game[:t], tgt, e, m,
                                    tables["spike_rates"], {"prev_pitch", "prev_swing"})
                q_me = mixture(c, rf, t, tgt, e, m, league, BASE, hist_pv)
                assert np.max(np.abs(q_ref - q_me)) < 1e-9, "base mixture differs from the ablation tool's"
                checked += 1
            who.append(name)
    who = np.array(who)
    base = np.array(loss["FULL (deployed)"])
    print(f"\n{len(base)} held-out steps from {len(np.unique(who))} pitchers; deployed model log-loss {base.mean():.4f}")
    print("change when the spike is ADDED (negative = adding helps); pitcher-bootstrap 95% CI; % of pitchers improved\n")
    boot = np.random.default_rng(4)
    for lab in list(variants)[1:]:
        d = np.array(loss[lab]) - base
        per = np.array([d[who == x].mean() for x in np.unique(who)])
        bs = np.array([boot.choice(per, len(per)).mean() for _ in range(2000)])
        lo, hi = np.percentile(bs, [2.5, 97.5])
        print(f"{lab:48s} {per.mean():+.4f}  CI [{lo:+.4f}, {hi:+.4f}]  pitchers improved {(per < 0).mean() * 100:.0f}%")


if __name__ == "__main__":
    main()

"""Leave-one-out test of the deployed pitch-likelihood model on held-out pitchers (no model code is touched).

For each component, remove it and re-score the held-out steps from tools/pitch_model_fit.py's saved walks:
  - pooled signals (league_fine, own_digits, cooldown pair, delta signals): removed with the OTHER weights left as fitted
    ("fixed"), and with the other weights re-optimised on the TRAIN sample ("refit") - the fair version, since correlated
    signals can pick up each other's weight;
  - exact-value mixture: e = 0;
  - spikes: all off, then each of previous-pitch / previous-swing / previous-diff / previous-pitch+500 off on its own (the
    pitch-before-last spike is gone).
The mixture stage below is a flag-able copy of pitch_likelihood_backtest.apply_mixtures (checked equal to it when every
flag is on). League tables come from TRAIN pitchers only. Change is vs the full model; + = removing it HURTS (the piece helps).

Usage: python -I tools/pitch_model_ablation.py
"""
from __future__ import annotations

import json
import math
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "C:/Users/Alex/PycharmProjects/numberball")
sys.path.insert(0, "C:/Users/Alex/PycharmProjects/numberball/tools")
import pitch_model_fit as pf  # noqa: E402
import pitch_likelihood_backtest as bt  # noqa: E402

EPS = 1e-12
GRID = 1000
STEPS_PER_PITCHER = 150


def mixtures(c, pv, sw, gm, tgt_game, e, m, league, spikes_on, exact_on=True):
    """Flag-able copy of bt.apply_mixtures. spikes_on: subset of {'prev_pitch','prev_swing','prev_diff','prev_pitch_p500'}."""
    n = len(pv)
    if n < 3:
        return c
    own = np.bincount((pv - 1) % GRID, minlength=GRID).astype(float)
    q = ((1 - e) * c + e * own / own.sum()) if exact_on else c
    refs = {"prev_pitch": (pv[-1], pv[1:] == pv[:-1], gm[1:] == gm[:-1], gm[-1]),
            "prev_swing": (sw[-1], pv[1:] == sw[:-1], gm[1:] == gm[:-1], gm[-1])}
    pv_f = pv.astype(float)
    ok_pv = (pv >= 1) & (pv <= 1000)
    raw_d = np.abs(pv_f - sw) % 1000
    d_all = np.where(ok_pv & (sw >= 1) & (sw <= 1000), np.minimum(raw_d, 1000 - raw_d), np.nan)
    diff_v = np.where(d_all == 0, 1000.0, d_all)
    p500_v = np.where(ok_pv, (pv_f + 499) % 1000 + 1, np.nan)
    same = gm[1:] == gm[:-1]
    refs["prev_diff"] = (diff_v[-1], pv_f[1:] == diff_v[:-1], same & ~np.isnan(diff_v[:-1]), gm[-1])
    refs["prev_pitch_p500"] = (p500_v[-1], pv_f[1:] == p500_v[:-1], same & ~np.isnan(p500_v[:-1]), gm[-1])
    total, add = 0.0, np.zeros(GRID)
    for name, (val, hit, defined, ref_game) in refs.items():
        if name not in spikes_on or name not in league or val != val or (tgt_game is not None and ref_game != tgt_game):
            continue
        rate = (float(hit[defined].sum()) + m * league[name]) / (float(defined.sum()) + m)
        total += rate
        add[int(val) - 1] += rate
    return (1 - total) * q + add


def main():
    sp = json.loads((pf.OUT / "split.json").read_text())
    fr = json.loads((pf.OUT / "fit_result.json").read_text())
    tables = pf._train_tables(sp["train"])
    allnames = pf.NAMES + pf.LEGACY
    col = {n: allnames.index(n) for n in allnames}
    names = pf.NEW9
    w_full = np.array([fr["weights"][n] for n in names])
    e, m = fr["habit"]["e_exact"], fr["habit"]["m_spike"]
    league = tables["spike_rates"]
    ALL = {"prev_pitch", "prev_swing", "prev_diff", "prev_pitch_p500"}   # the pitch-before-last spike was dropped

    # train sample for the refits
    rng = np.random.default_rng(1)
    train_w = [pf.Walk(n) for n in sp["train"] if (pf.OUT / f"{pf._safe(n)}.npz").exists()]
    B, R, k = [], [], []
    for wk in train_w:
        pick = rng.choice(len(wk.steps), min(90, len(wk.steps)), replace=False)
        B.append(wk.B[pick]); R.append(wk.R[pick]); k.append(wk.pitch[wk.steps[pick]].astype(int) - 1)
    B, R, k = np.concatenate(B), np.concatenate(R), np.concatenate(k)

    def refit_without(drop):
        keep = [i for i, n in enumerate(names) if n not in drop]
        w, _ = pf.fit_weights(B, R, k, [col[names[i]] for i in keep], w_full[keep])
        full = np.zeros(len(names)); full[keep] = w
        return full

    # pool-weight variants: (label, weights vector over NEW9)
    pool_variants = {"FULL": w_full}
    for label, drop in (("league_fine", {"league_fine"}), ("own_digits", {"own_digits"}),
                        ("both habit signals", {"league_fine", "own_digits"}),
                        ("cooldown + delta cooldown", {"cooldown", "delta_cooldown"}),
                        ("delta signals (4)", {"delta_sequence", "delta_after_result", "delta_after_leverage",
                                               "shadow_delta_after_result"})):
        fixed = w_full.copy()
        for i, n in enumerate(names):
            if n in drop:
                fixed[i] = 0.0
        pool_variants[f"- {label} (fixed)"] = fixed
        pool_variants[f"- {label} (refit)"] = refit_without(drop)
    mix_variants = {"- exact-value mixture": dict(exact_on=False, spikes=ALL),
                    "- all spikes": dict(exact_on=True, spikes=set()),
                    "- spike: previous pitch": dict(exact_on=True, spikes=ALL - {"prev_pitch"}),
                    "- spike: previous swing": dict(exact_on=True, spikes=ALL - {"prev_swing"}),
                    "- spike: previous diff": dict(exact_on=True, spikes=ALL - {"prev_diff"}),
                    "- spike: previous pitch + 500": dict(exact_on=True, spikes=ALL - {"prev_pitch_p500"}),
                    "- both mixtures (pool only)": dict(exact_on=False, spikes=set())}
    rows_all = [col[n] for n in names]

    test_w = [pf.Walk(n) for n in sp["test"] if (pf.OUT / f"{pf._safe(n)}.npz").exists()]
    rng = np.random.default_rng(9)
    loss = {lab: [] for lab in list(pool_variants) + list(mix_variants)}
    who, checked = [], 0
    cfg = bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0, leverage_width=0.5,
                    delta_cooldown_radius=50.0, weights={}, tables=tables, habit=fr["habit"])
    for wk in test_w:
        for si in sorted(rng.choice(len(wk.steps), min(STEPS_PER_PITCHER, len(wk.steps)), replace=False)):
            t = int(wk.steps[si])
            kk = int(wk.pitch[t]) - 1
            pv, sw, gm = wk.pitch[:t].astype(int), wk.swing[:t].astype(float), wk.game[:t]
            tgt = float(wk.game[t])
            Bi, Ri = wk.B[si:si + 1], wk.R[si:si + 1]
            for lab, w in pool_variants.items():
                c = np.exp(pf.pool_logp(Bi, Ri, w, rows_all))[0]
                q = mixtures(c, pv, sw, gm, tgt, e, m, league, ALL)
                loss[lab].append(-math.log(q[kk] + EPS))
                if lab == "FULL" and checked < 25:          # the flag-able copy must equal the real stage
                    hist = pd.DataFrame({"pitch": wk.pitch[:t], "swing": wk.swing[:t], "game_id": wk.game[:t],
                                         "id": np.arange(t)})
                    ref = bt.apply_mixtures(c, hist, {}, cfg, pd.Series({"game_id": wk.game[t]}))
                    assert np.max(np.abs(ref - q)) < 1e-9, "mixture copy differs from apply_mixtures"
                    checked += 1
                    full_c = c
            for lab, f in mix_variants.items():
                q = mixtures(full_c if False else np.exp(pf.pool_logp(Bi, Ri, w_full, rows_all))[0], pv, sw, gm, tgt, e, m,
                             league, f["spikes"], f["exact_on"])
                loss[lab].append(-math.log(q[kk] + EPS))
            who.append(wk.name)
    who = np.array(who)
    base = np.array(loss["FULL"])
    print(f"{len(base)} held-out steps from {len(np.unique(who))} pitchers; full model log-loss {base.mean():.4f}")
    print("change when the piece is REMOVED (+ = removing hurts = the piece helps); pitcher-bootstrap 95% CI; "
          "% of pitchers hurt by removing it\n")
    boot_rng = np.random.default_rng(4)
    out = []
    for lab in list(pool_variants)[1:] + list(mix_variants):
        d = np.array(loss[lab]) - base
        per = np.array([d[who == x].mean() for x in np.unique(who)])
        bs = [boot_rng.choice(per, len(per)).mean() for _ in range(1000)]
        out.append((lab, d.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5), np.mean(per > 0)))
        print(f"  {lab:<34} {d.mean():+.4f}   [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]   "
              f"{np.mean(per > 0):>4.0%}")
    print("\n(the weights tried: refit weights are re-optimised on the train sample with the piece removed)")


if __name__ == "__main__":
    main()

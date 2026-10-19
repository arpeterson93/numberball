"""Second pass of the pitch-signal screen: where does the number-habit gain come from, and do the pieces stack?

Reads the per-pitcher walks saved by tools/pitch_signal_screen.py collect (current model untouched). League statistics
(fine value prior, digit tables, spike rates) are estimated from TRAIN pitchers only and every per-pitcher statistic uses
only that pitcher's earlier pitches, so test-pitcher scores are out-of-sample and prequential.

Variants, each applied on top of the current model's density c:
  L   league fine-structure prior: c * (q_fine / q_smooth)^w   (what every pitcher shares: round numbers, repdigits...)
  D2  per-pitcher last-two-digit habit: c * (smoothed own last-2 share / uniform)^s2
  E   exact past values: (1-e) * c + e * own empirical past values
  S   point-mass spikes on previous pitch / previous swing / pitch before last (online per-pitcher rates, league-shrunk)
Stacks: L, L+D2, L+D2+E, L+D2+E+S.   Also reports gains by pitcher and a pitcher bootstrap CI.
"""
from __future__ import annotations

import math
import sys

import numpy as np

sys.path.insert(0, "C:/Users/Alex/PycharmProjects/numberball/tools")
import pitch_signal_screen as ps  # noqa: E402

EPS = 1e-12
GRID = np.arange(1, 1001)


def kde_wrap(counts: np.ndarray, sigma: float) -> np.ndarray:
    """Circular Gaussian smoothing of a length-1000 count vector (index = value-1), returned as a pmf."""
    k = np.arange(1000)
    d = np.minimum(np.abs(k[:, None] - k[None, :]), 1000 - np.abs(k[:, None] - k[None, :]))
    K = np.exp(-(d ** 2) / (2 * sigma ** 2))
    s = K @ counts
    return s / s.sum()


def main():
    P = [ps.Pitcher(f) for f in sorted(ps.OUT.glob("*.npz"))]
    w0 = ps._weights()
    rng = np.random.default_rng(0)
    order = rng.permutation(len(P))
    train_idx = set(order[: len(P) // 2])
    train_P = [P[i] for i in sorted(train_idx)]
    test_P = [P[i] for i in range(len(P)) if i not in train_idx]
    print(f"{len(train_P)} train / {len(test_P)} test pitchers")

    # ---- league statistics from train pitchers only
    cnt = np.zeros(1000)
    for p in train_P:
        cnt += np.bincount((p.pitch.astype(int) - 1) % 1000, minlength=1000)
    q_fine = (cnt + 0.5) / (cnt + 0.5).sum()
    q_smooth = kde_wrap(cnt + 0.5, 75.0)
    fine_ratio = q_fine / q_smooth                     # index = value-1
    log_fine = np.log(fine_ratio)
    # league refs (point-mass spike rates) from train pitchers
    league_rate = {}
    for r in ("prev_pitch", "prev_swing", "prev_pitch2"):
        h = n = 0
        for p in train_P:
            for t in range(len(p.pitch)):
                v = p.refs(t)[r]
                if v == v:
                    n += 1
                    h += p.pitch[t] == v
        league_rate[r] = h / n
    print("league spike rates (all train steps):", {k: round(v, 4) for k, v in league_rate.items()})

    # ---- per-step pass for a set of pitchers; returns dict variant -> list of (pitcher, ll)
    def run(pitchers, w_L, s2, e_ex, m_sp, use_sp=True, variants=("V0", "L", "LD2", "LD2E", "LD2ES", "D2", "E", "S")):
        out = {v: [] for v in variants}
        who = []
        for p in pitchers:
            counts_val = np.zeros(1000)
            counts_l2 = np.zeros(100)
            hits = {r: 0.0 for r in league_rate}
            dens = {r: 0.0 for r in league_rate}
            steps = {int(i): si for si, i in enumerate(p.steps)}
            for t in range(len(p.pitch)):
                if t in steps:
                    si = steps[t]
                    c = ps.current_density(p, si, w0)
                    k = int(p.pitch[t]) - 1
                    logc = np.log(c + EPS)
                    l2 = counts_l2
                    prob_l2 = (l2 + 1.0) / (l2.sum() + 100)
                    f2 = np.log(prob_l2 * 100)[(GRID) % 100]           # index = value-1
                    own = counts_val / counts_val.sum() if counts_val.sum() else np.full(1000, 1 / 1000)
                    refs = p.refs(t)

                    def sm(lg):
                        lg = lg - lg.max()
                        q = np.exp(lg)
                        return q / q.sum()

                    def ex_mix(q):
                        return (1 - e_ex) * q + e_ex * own

                    def spikes(q):
                        e_tot = 0.0
                        add = np.zeros(1000)
                        for r in league_rate:
                            v = refs[r]
                            if v == v:
                                rate = (hits[r] + m_sp * league_rate[r]) / (dens[r] + m_sp)
                                e_tot += rate
                                add[int(v) - 1] += rate
                        return (1 - e_tot) * q + add

                    qL = sm(logc + w_L * log_fine)
                    qLD2 = sm(logc + w_L * log_fine + s2 * f2)
                    qD2 = sm(logc + s2 * f2)
                    vals = {"V0": c, "L": qL, "LD2": qLD2, "D2": qD2, "E": ex_mix(c), "S": spikes(c)}
                    if "LD2E" in variants:
                        vals["LD2E"] = ex_mix(qLD2)
                    if "LD2ES" in variants:
                        vals["LD2ES"] = spikes(ex_mix(qLD2))
                    for name in variants:
                        out[name].append(-math.log(vals[name][k] + EPS))
                    who.append(p.name)
                # update online stats AFTER scoring pitch t
                v = int(p.pitch[t])
                counts_val[(v - 1) % 1000] += 1
                counts_l2[v % 100] += 1
                refs_t = p.refs(t)
                for r in league_rate:
                    x = refs_t[r]
                    if x == x:
                        dens[r] += 1
                        hits[r] += (p.pitch[t] == x)
        return {k: np.array(v) for k, v in out.items()}, np.array(who)

    # ---- fit hyper-parameters on train pitchers (small grids, sequential)
    def mean_ll(res, v):
        return res[v].mean()

    print("\nfitting on train pitchers...")
    best = {}
    for w_L in (0.0, 0.5, 1.0, 1.5):
        res, _ = run(train_P, w_L, 0.0, 0.0, 150, variants=("V0", "L", "LD2", "LD2E", "LD2ES", "D2", "E", "S"))
        print(f"  w_L={w_L}: train L ll {mean_ll(res, 'L'):.4f} (V0 {mean_ll(res, 'V0'):.4f})")
        best.setdefault("wL", []).append((mean_ll(res, "L"), w_L))
    w_L = min(best["wL"])[1]
    for s2 in (0.5, 1.0, 1.5, 2.0, 3.0):
        res, _ = run(train_P, w_L, s2, 0.0, 150, variants=("V0", "L", "LD2", "LD2E", "LD2ES", "D2", "E", "S"))
        best.setdefault("s2", []).append((mean_ll(res, "LD2"), s2))
        print(f"  w_L={w_L} s2={s2}: train LD2 ll {mean_ll(res, 'LD2'):.4f}")
    s2 = min(best["s2"])[1]
    for e_ex in (0.01, 0.02, 0.04, 0.08, 0.12):
        res, _ = run(train_P, w_L, s2, e_ex, 150, variants=("V0", "L", "LD2", "LD2E", "LD2ES", "D2", "E", "S"))
        best.setdefault("e", []).append((mean_ll(res, "LD2E"), e_ex))
        print(f"  e={e_ex}: train LD2E ll {mean_ll(res, 'LD2E'):.4f}")
    e_ex = min(best["e"])[1]
    print(f"chosen on train: w_L={w_L} s2={s2} e_exact={e_ex}")

    # ---- score the held-out pitchers
    res, who = run(test_P, w_L, s2, e_ex, 150)
    base = res["V0"].mean()
    print(f"\nTEST ({len(res['V0'])} steps): current model {base:.4f}  (uniform {math.log(1000):.4f})")
    for v, label in (("L", "L   league fine-structure prior"), ("D2", "D2  per-pitcher last-two-digit habit"),
                     ("E", "E   own past exact values"), ("S", "S   previous pitch / swing / lag-2 spikes"),
                     ("LD2", "L+D2"), ("LD2E", "L+D2+E"), ("LD2ES", "L+D2+E+S")):
        d = res[v].mean() - base
        # pitcher bootstrap CI of the mean change
        diffs = {}
        for nm in np.unique(who):
            m = who == nm
            diffs[nm] = (res[v][m] - res["V0"][m]).sum(), m.sum()
        names = list(diffs)
        boots = []
        for _ in range(300):
            samp = rng.choice(len(names), len(names))
            num = sum(diffs[names[i]][0] for i in samp)
            den = sum(diffs[names[i]][1] for i in samp)
            boots.append(num / den)
        lo, hi = np.percentile(boots, [2.5, 97.5])
        improved = np.mean([diffs[n][0] / diffs[n][1] < 0 for n in names])
        print(f"  {label:<42} change {d:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  pitchers improved {improved:.0%}")
    return


if __name__ == "__main__":
    main()

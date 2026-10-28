"""Joint refit + out-of-sample comparison of the pitch-likelihood model variants.

collect : per pitcher, walk the live model (current code, excess-cooldown mode) and save each step's baseline density and
          every signal's log-ratio vector log(shrunk_signal / baseline), plus the legacy (unconditional-mass) cooldown
          rows for an A/B. League tables (hazards, fine-structure counts, spike rates) are built from TRAIN pitchers only,
          so a held-out pitcher never feeds his own tables. The train/test split is a fixed pitcher permutation.
fit     : fit the pool weights (bounded L-BFGS on train log-loss, subsampled steps), then the exact-value / spike mixture
          parameters, for each variant:
              OLD          7 old signals, legacy cooldown, old weights            (the deployed model)
              OLD-refit    same 7 signals, legacy cooldown, weights refit
              +EXCESS      cooldown + delta cooldown as excess-over-chance with expiry, weights refit
              +HABIT-POOL  + league_fine + own_digits pooled signals
              FULL         + exact-value and repeat-spike mixtures (apply_mixtures)
          and report held-out log-loss with a pitcher-bootstrap CI.

Usage: python -I tools/pitch_model_fit.py collect [--workers 6]
       python -I tools/pitch_model_fit.py fit
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
OUT = ROOT / "tools/probe_output/pitch_model_fit"
CACHE = ROOT / "tools/diff_lab/cache/plays.pkl"
MIN_HISTORY = 100
MIN_PITCHES = 250
EPS = 1e-12
NAMES = ["pitch_sequence", "delta_sequence", "cooldown", "delta_cooldown", "delta_after_result",
         "delta_after_leverage", "shadow_delta_after_result", "league_fine", "own_digits"]
LEGACY = ["cooldown_pmf", "delta_cooldown_pmf"]
OLD7 = ["pitch_sequence", "delta_sequence", "cooldown_pmf", "delta_cooldown_pmf", "delta_after_result",
        "delta_after_leverage", "shadow_delta_after_result"]
NEW7 = ["pitch_sequence", "delta_sequence", "cooldown", "delta_cooldown", "delta_after_result",
        "delta_after_leverage", "shadow_delta_after_result"]
NEW9 = NEW7 + ["league_fine", "own_digits"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name)


def _split() -> tuple[list[str], list[str]]:
    d = pd.read_pickle(CACHE)
    d = d[(d.play_type == "Swing") & d.pitch.notna()]
    counts = d.groupby("pitcher_name").size()
    names = sorted(counts[counts >= MIN_PITCHES].index.tolist())
    order = np.random.default_rng(0).permutation(len(names))
    train = [names[i] for i in sorted(order[: len(names) // 2])]
    test = [n for n in names if n not in set(train)]
    return train, test


def _train_tables(train_names: list[str]):
    import build_pitch_model_tables as bpt
    import pitch_likelihood_backtest as bt
    d = pd.read_pickle(CACHE)
    ids = d[d.pitcher_name.isin(train_names)].pitcher_id.unique().tolist()
    return bt.prepare_tables(bpt.build_tables(ids))


# ------------------------------------------------------------------------------------------------------ collect

_TT = None


def _collect_one(name: str) -> str:
    global _TT
    warnings.filterwarnings("ignore")
    import logging
    logging.disable(logging.CRITICAL)
    import pitch_likelihood_backtest as bt
    import scouting_data

    if _TT is None:
        _TT = _train_tables(json.loads((OUT / "split.json").read_text())["train"])
    df = scouting_data.load_pitcher_plays(name)
    if df.empty:
        return f"{name}: empty"
    base = dict(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0, leverage_width=0.5,
                delta_cooldown_radius=50.0, weights=dict(bt.DEFAULT_WEIGHTS), tables=_TT)
    cfg = bt.Config(**base)                               # excess cooldown (default mode)
    cfg_legacy = bt.Config(**base, cooldown_mode="pmf")
    builders = list(bt.SIGNAL_BUILDERS.values())
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    n = len(pitches)
    steps, B, R = [], [], []
    for i in range(MIN_HISTORY, n):
        df_hist = df[df["id"] < pitches.loc[i, "id"]]
        state, baseline, signals = bt._prepare_step(df_hist, builders, cfg, target_row=pitches.loc[i])
        if state is None or signals is None:
            continue
        r = np.zeros((len(NAMES) + len(LEGACY), bt.GRID_SIZE), dtype=np.float16)
        sigs = list(signals)
        # legacy-mode cooldown rows for the A/B
        for nm, fn in (("cooldown_pmf", bt.SIGNAL_BUILDERS["cooldown"]), ("delta_cooldown_pmf", bt.SIGNAL_BUILDERS["delta_cooldown"])):
            s = fn(df_hist, state, cfg_legacy)
            if s is not None:
                sigs.append(bt.Signal(nm, s.density, s.n_eff))
        for s in sigs:
            alpha = s.n_eff / (s.n_eff + cfg.min_effective_n)
            shrunk = alpha * s.density + (1 - alpha) * baseline
            idx = (NAMES + LEGACY).index(s.name)
            r[idx] = np.log((shrunk + EPS) / (baseline + EPS)).astype(np.float16)
        steps.append(i)
        B.append(baseline.astype(np.float32))
        R.append(r)
    np.savez_compressed(OUT / f"{_safe(name)}.npz", name=name, steps=np.array(steps), B=np.array(B), R=np.array(R),
                        pitch=pitches["pitch"].astype(float).to_numpy(), swing=pitches["swing"].astype(float).to_numpy(),
                        game=pitches["game_id"].astype(float).to_numpy())
    return f"{name}: {len(steps)} steps"


def collect(workers: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    train, test = _split()
    (OUT / "split.json").write_text(json.dumps({"train": train, "test": test}))
    done = {p.stem for p in OUT.glob("*.npz")}
    todo = [n for n in train + test if _safe(n) not in done]
    print(f"{len(train)} train / {len(test)} test pitchers; {len(todo)} to collect", flush=True)
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(workers) as ex:
        for msg in ex.map(_collect_one, todo):
            print(msg, flush=True)


# ---------------------------------------------------------------------------------------------------------- fit

class Walk:
    def __init__(self, name):
        z = np.load(OUT / f"{_safe(name)}.npz", allow_pickle=True)
        self.name, self.steps, self.B, self.R = name, z["steps"], z["B"], z["R"]
        self.pitch, self.swing, self.game = z["pitch"], z["swing"], z["game"]


def pool_logp(B, R, w, rows):
    """log of the pooled density for stacked steps. B (N,1000), R (N,K,1000), w over `rows` (column indices of R)."""
    lg = np.log(B.astype(np.float64) + EPS) + np.tensordot(R[:, rows, :].astype(np.float64), w, axes=([1], [0]))
    lg -= lg.max(axis=1, keepdims=True)
    return lg - np.log(np.exp(lg).sum(axis=1, keepdims=True))


def fit_weights(B, R, k, rows, w0):
    from scipy.optimize import minimize
    def loss(w):
        lp = pool_logp(B, R, w, rows)
        return -lp[np.arange(len(k)), k].mean()
    res = minimize(loss, w0, method="L-BFGS-B", bounds=[(0.0, 3.0)] * len(w0), options={"maxiter": 60})
    return res.x, res.fun


def mix_ll(walks, w, rows, habit, tables, use_mix=True, sample=None, rng=None):
    """Mean log-loss over the walks' steps with weights w; mixtures via the REAL apply_mixtures."""
    import pitch_likelihood_backtest as bt
    cfg = bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0, leverage_width=0.5,
                    delta_cooldown_radius=50.0, weights={}, tables=tables, habit=habit)
    tot, cnt, per = 0.0, 0, {}
    for wk in walks:
        idxs = range(len(wk.steps))
        if sample is not None:
            idxs = sorted(rng.choice(len(wk.steps), min(sample, len(wk.steps)), replace=False))
        s_tot, s_n = 0.0, 0
        for si in idxs:
            t = int(wk.steps[si])
            Bi, Ri = wk.B[si:si + 1], wk.R[si:si + 1]
            c = np.exp(pool_logp(Bi, Ri, w, rows))[0]
            if use_mix:
                hist = pd.DataFrame({"pitch": wk.pitch[:t], "swing": wk.swing[:t], "game_id": wk.game[:t],
                                     "id": np.arange(t)})
                c = bt.apply_mixtures(c, hist, {}, cfg, pd.Series({"game_id": wk.game[t]}))
            s_tot += -math.log(c[int(wk.pitch[t]) - 1] + EPS)
            s_n += 1
        tot += s_tot
        cnt += s_n
        per[wk.name] = (s_tot, s_n)
    return tot / cnt, per


def fit() -> None:
    import pitch_likelihood_backtest as bt
    sp = json.loads((OUT / "split.json").read_text())
    train_w = [Walk(n) for n in sp["train"] if (OUT / f"{_safe(n)}.npz").exists()]
    test_w = [Walk(n) for n in sp["test"] if (OUT / f"{_safe(n)}.npz").exists()]
    tables = _train_tables(sp["train"])
    rng = np.random.default_rng(1)
    # subsample train steps for weight fitting
    allB, allR, allk = [], [], []
    for wk in train_w:
        pick = rng.choice(len(wk.steps), min(90, len(wk.steps)), replace=False)
        allB.append(wk.B[pick]); allR.append(wk.R[pick]); allk.append(wk.pitch[wk.steps[pick]].astype(int) - 1)
    B, R, k = np.concatenate(allB), np.concatenate(allR), np.concatenate(allk)
    print(f"fit on {len(k)} sampled train steps from {len(train_w)} pitchers; test {sum(len(w.steps) for w in test_w)} steps "
          f"from {len(test_w)} pitchers")
    allnames = NAMES + LEGACY
    col = {n: allnames.index(n) for n in allnames}
    old_w = np.array([bt.DEFAULT_WEIGHTS.get(n.replace("_pmf", ""), 1.0) for n in OLD7])
    old_w = np.array([0.0, 0.068, 0.24, 0.0, 0.143, 0.167, 0.23])    # the deployed weights, in OLD7 order

    results = {}

    def evaluate(label, names, w, habit=None, use_mix=False):
        rows = [col[n] for n in names]
        ll, per = mix_ll(test_w, w, rows, habit or bt.HABIT_DEFAULTS, tables, use_mix=use_mix)
        results[label] = (ll, per)
        print(f"  {label:<22} test log-loss {ll:.4f}   weights {dict(zip(names, np.round(w, 3)))}", flush=True)
        return ll

    print("\nOLD (deployed weights, legacy cooldown):")
    evaluate("OLD", OLD7, old_w)
    for label, names, w0 in (("OLD-refit", OLD7, old_w), ("+EXCESS", NEW7, old_w),
                             ("+HABIT-POOL", NEW9, np.r_[old_w, 1.0, 0.5])):
        rows = [col[n] for n in names]
        w, trn = fit_weights(B, R, k, rows, w0)
        print(f"{label}: train sample log-loss {trn:.4f}")
        evaluate(label, names, w)
        results[label + "_w"] = w
    # mixtures on top of the +HABIT-POOL weights: grid on train sample
    print("\nFULL: fitting mixture parameters on train sample")
    names, w = NEW9, results["+HABIT-POOL_w"]
    rows = [col[n] for n in names]
    best = None
    sample_rng = np.random.default_rng(2)
    for e in (0.02, 0.05, 0.08, 0.12):
        for m in (50.0, 150.0, 400.0):
            ll, _ = mix_ll(train_w, w, rows, {"mixtures": True, "e_exact": e, "m_spike": m}, tables, True, sample=40,
                           rng=sample_rng)
            print(f"  e={e} m={m}: train-sample log-loss {ll:.4f}", flush=True)
            if best is None or ll < best[0]:
                best = (ll, e, m)
    habit = {"mixtures": True, "e_exact": best[1], "m_spike": best[2]}
    print(f"chosen {habit}")
    evaluate("FULL", names, w, habit, True)
    results["habit"] = habit

    # ---- summary with pitcher bootstrap on differences vs OLD
    print("\n=== held-out summary (lower is better; change vs deployed OLD) ===")
    base = results["OLD"][1]
    rng2 = np.random.default_rng(3)
    for label in ("OLD", "OLD-refit", "+EXCESS", "+HABIT-POOL", "FULL"):
        per = results[label][1]
        names_ = list(per)
        num = np.array([per[n][0] - base[n][0] for n in names_])
        den = np.array([per[n][1] for n in names_], dtype=float)
        d = num.sum() / den.sum()
        boots = [num[i].sum() / den[i].sum() for i in (rng2.choice(len(names_), len(names_)) for _ in range(500))]
        lo, hi = np.percentile(boots, [2.5, 97.5])
        print(f"  {label:<14} log-loss {results[label][0]:.4f}  change {d:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  "
              f"pitchers improved {np.mean(num < 0):.0%}")
    out = {"weights": dict(zip(NEW9, [float(x) for x in results["+HABIT-POOL_w"]])), "habit": habit,
           "weights_excess_only": dict(zip(NEW7, [float(x) for x in results["+EXCESS_w"]])),
           "weights_old_refit": dict(zip(OLD7, [float(x) for x in results["OLD-refit_w"]]))}
    (OUT / "fit_result.json").write_text(json.dumps(out, indent=1))
    print("saved fit_result.json")


def swing() -> None:
    """Realized swing value, old model vs new, under several score-by-diff tables (tools/build_swing_score_tables.py):
    the old hand-made neutral table (every stat 3, bases empty), and the league's real result mix by diff from the Diff
    Distribution Lab (all swings pooled, and the 0-outs-bases-empty state). For each held-out step each density (baseline
    KDE / deployed model / new model) suggests the swing with the best expected score (FFT over that table), graded by the
    table's score at the actual diff - an expected value, so no sampling noise from drawing a result."""
    import pitch_likelihood_backtest as bt
    import utils
    sp = json.loads((OUT / "split.json").read_text())
    fit_res = json.loads((OUT / "fit_result.json").read_text())
    score_tabs = json.loads((ROOT / "tools/swing_score_tables.json").read_text())
    tables_to_run = {"old neutral (all stats 3)": score_tabs["old_neutral"],
                     "league mix, all states": score_tabs["pooled"],
                     "league mix, 0 out empty": score_tabs["state_000"]}
    arrs = {k: {m: np.asarray(v[m], float) for m in ("obp", "slg")} for k, v in tables_to_run.items()}
    test_w = [Walk(n) for n in sp["test"] if (OUT / f"{_safe(n)}.npz").exists()]
    tables = _train_tables(sp["train"])
    allnames = NAMES + LEGACY
    col = {n: allnames.index(n) for n in allnames}
    old_w = np.array([0.0, 0.068, 0.24, 0.0, 0.143, 0.167, 0.23])
    new_w = np.array([fit_res["weights"][n] for n in NEW9])
    cfg = bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0, leverage_width=0.5,
                    delta_cooldown_radius=50.0, weights={}, tables=tables, habit=fit_res["habit"])
    rng = np.random.default_rng(5)
    rows_old, rows_new = [col[n] for n in OLD7], [col[n] for n in NEW9]
    keys = [(t, m) for t in arrs for m in ("obp", "slg")]
    res = {k: {"baseline": [], "old": [], "new": [], "pitcher": []} for k in keys}
    for wk in test_w:
        for si in sorted(rng.choice(len(wk.steps), min(60, len(wk.steps)), replace=False)):
            t = int(wk.steps[si])
            Bi, Ri = wk.B[si:si + 1], wk.R[si:si + 1]
            dens = {"baseline": Bi[0].astype(float),
                    "old": np.exp(pool_logp(Bi, Ri, old_w, rows_old))[0]}
            new = np.exp(pool_logp(Bi, Ri, new_w, rows_new))[0]
            hist = pd.DataFrame({"pitch": wk.pitch[:t], "swing": wk.swing[:t], "game_id": wk.game[:t], "id": np.arange(t)})
            dens["new"] = bt.apply_mixtures(new, hist, {}, cfg, pd.Series({"game_id": wk.game[t]}))
            actual = int(wk.pitch[t])
            for (tname, metric) in keys:
                arr = arrs[tname][metric]
                for key, d in dens.items():
                    sw_ = int(np.argmax(utils._scores_via_fft(d, arr))) + 1
                    res[(tname, metric)][key].append(float(arr[utils.circular_diff(actual, sw_)]))
                res[(tname, metric)]["pitcher"].append(wk.name)
    n_p = len(res[keys[0]]["new"])
    print(f"\n=== realized swing value on held-out pitches ({n_p} pitches, {len(test_w)} pitchers) ===")
    for tname in arrs:
        w = np.r_[1.0, np.full(499, 2.0), 1.0] / 1000.0
        print(f"\n{tname}:")
        for metric in ("obp", "slg"):
            r = res[(tname, metric)]
            chance = float((arrs[tname][metric] * w).sum())
            means = {k: float(np.mean(r[k])) for k in ("baseline", "old", "new")}
            names_ = np.array(r["pitcher"])
            diffs = np.array(r["new"]) - np.array(r["old"])
            pv = np.array([diffs[names_ == n].mean() for n in np.unique(names_)])
            boots = [rng.choice(pv, len(pv)).mean() for _ in range(1000)]
            lo, hi = np.percentile(boots, [2.5, 97.5])
            print(f"  {metric.upper()}: random swing {chance:.4f} | baseline {means['baseline']:.4f} | deployed {means['old']:.4f}"
                  f" | new {means['new']:.4f} | new-deployed {means['new'] - means['old']:+.4f} "
                  f"[95% CI {lo:+.4f}, {hi:+.4f}], {np.mean(pv > 0):.0%} of pitchers better")

if __name__ == "__main__":
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import local_plays
    local_plays.enable()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "fit", "swing"])
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    {"collect": lambda: collect(a.workers), "fit": fit, "swing": swing}[a.cmd]()

"""Screen candidate additions to the pitch-likelihood model WITHOUT touching it.

`collect` walks the live model (tools/pitch_likelihood_backtest.py, default config and weights) for each pitcher and
saves, per backtest step, its baseline density and each signal's log-ratio vector log(shrunk_signal / baseline), plus the
reference values a candidate might key on (previous pitch, previous swing, pitch before last, and for a half-inning's
first pitch the last pitch / swing of the previous half inning). Nothing in the model changes.

`screen` then replays those steps offline: the current model is combine_signals' own formula applied to the stored
vectors, and each candidate is a transform of that density, scored prequentially (every prediction uses only earlier
pitches). Candidates:
  1. spike     - point-mass mixture on "same as previous pitch / previous swing / pitch before last / last swing of the
                 previous half inning"
  2. adaptive  - the same spikes with a PER-PITCHER rate estimated online from his own recent history (shrunk to the
                 league rate, recency-discounted), and per-pitcher online reweighting of the model's seven signals
  4. habits    - number habits finer than the KDE's sigma: past exact values, last-two-digits, last digit

Usage:  python -I tools/pitch_signal_screen.py collect [--workers 6] [--min-pitches 250]
        python -I tools/pitch_signal_screen.py screen
        python -I tools/pitch_signal_screen.py incremental   # just section 1 + 1b (fast): each candidate spike added on top of
                                                             # the deployed spikes, with pitcher-bootstrap CIs

Spike references (REF_NAMES): previous pitch / swing / pitch-before-last, the opposing half-inning's last pitch / swing, and
(2026-10-22) the previous PA's diff, diff + 500, and the digit reversal / opposite side (+500) of the previous pitch, swing and
diff. CAUTION: the stored walks hold the older seven-signal model (no number-habit signals, no exact-value mixture), so gains
here can be overstated - pitch-before-last looks helpful in this screen but is ~0 in the deployed model. Confirm anything
promising with tools/pitch_model_spike_screen.py, which adds the candidate to the deployed model itself.
"""
from __future__ import annotations

import argparse
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
OUT = ROOT / "tools/probe_output/pitch_screen"
CACHE = ROOT / "tools/diff_lab/cache/plays.pkl"
MIN_HISTORY = 100
SIGS = ["pitch_sequence", "delta_sequence", "cooldown", "delta_cooldown",
        "delta_after_result", "delta_after_leverage", "shadow_delta_after_result"]
EPS = 1e-12


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name)


# ------------------------------------------------------------------------------------------------------ collect

def _collect_one(name: str) -> str:
    warnings.filterwarnings("ignore")
    import logging
    logging.disable(logging.CRITICAL)
    import pitch_likelihood_backtest as bt
    import scouting_data
    import transition_analysis as ta

    df = scouting_data.load_pitcher_plays(name)
    if df.empty:
        return f"{name}: empty"
    cfg = bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0,
                    leverage_width=0.5, delta_cooldown_radius=50.0, weights=dict(bt.DEFAULT_WEIGHTS))
    builders = list(bt.SIGNAL_BUILDERS.values())
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    n = len(pitches)

    # half-inning-start references from the game-wide play log (the pitcher's own df can't see the other team's rows)
    ev = ta.build_events(pd.read_pickle(CACHE))
    look = ev.set_index("id")[["half_start", "opp_pitch", "opp_swing"]]
    j = look.reindex(pitches["id"])
    opp_pitch = np.where(j["half_start"].fillna(False).astype(bool), j["opp_pitch"], np.nan)
    opp_swing = np.where(j["half_start"].fillna(False).astype(bool), j["opp_swing"], np.nan)

    pitch = pitches["pitch"].astype(float).to_numpy()
    swing = pitches["swing"].astype(float).to_numpy()
    game = pitches["game_id"].astype(float).to_numpy()
    steps, B, R = [], [], []
    for i in range(MIN_HISTORY, n):
        cur_id = pitches.loc[i, "id"]
        state, baseline, signals = bt._prepare_step(df[df["id"] < cur_id], builders, cfg, target_row=pitches.loc[i])
        if state is None or signals is None:
            continue
        r = np.zeros((len(SIGS), bt.GRID_SIZE), dtype=np.float16)
        for s in signals:
            alpha = s.n_eff / (s.n_eff + cfg.min_effective_n)
            shrunk = alpha * s.density + (1 - alpha) * baseline
            r[SIGS.index(s.name)] = np.log((shrunk + EPS) / (baseline + EPS)).astype(np.float16)
        steps.append(i)
        B.append(baseline.astype(np.float32))
        R.append(r)
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / f"{_safe(name)}.npz", name=name, steps=np.array(steps), B=np.array(B), R=np.array(R),
                        pitch=pitch, swing=swing, game=game, opp_pitch=opp_pitch, opp_swing=opp_swing,
                        season=pitches["season"].astype(float).to_numpy())
    return f"{name}: {len(steps)} steps"


def collect(workers: int, min_pitches: int) -> None:
    d = pd.read_pickle(CACHE)
    d = d[(d.play_type == "Swing") & d.pitch.notna()]
    counts = d.groupby("pitcher_name").size()
    names = counts[counts >= min_pitches].sort_values().index.tolist()
    done = {p.stem for p in OUT.glob("*.npz")} if OUT.exists() else set()
    names = [n for n in names if _safe(n) not in done]
    print(f"{len(names)} pitchers to collect ({len(done)} already done)", flush=True)
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(workers) as ex:
        for msg in ex.map(_collect_one, names):
            print(msg, flush=True)


# ------------------------------------------------------------------------------------------------------ screen

def _weights():
    import pitch_likelihood_backtest as bt
    return np.array([bt.DEFAULT_WEIGHTS.get(s, 1.0) for s in SIGS], dtype=np.float64)


def _softmax_log(lg: np.ndarray) -> np.ndarray:
    lg = lg - lg.max()
    p = np.exp(lg)
    return p / p.sum()


def _rev3(v: float) -> float:
    """Digit reversal of a wheel value zero-padded to three digits (045 -> 540; 1000 -> 1)."""
    return float(int(str(int(v)).zfill(3)[::-1]))


def _p500(v: float) -> float:
    """The opposite side of the 1-1000 wheel."""
    return float((int(v) + 500 - 1) % 1000 + 1)


class Pitcher:
    """One pitcher's stored walk."""

    def __init__(self, path):
        z = np.load(path, allow_pickle=True)
        self.name = str(z["name"])
        self.steps = z["steps"]
        self.B, self.R = z["B"], z["R"]
        self.pitch, self.swing, self.game = z["pitch"], z["swing"], z["game"]
        self.opp_pitch, self.opp_swing = z["opp_pitch"], z["opp_swing"]
        self.season = z["season"]

    def refs(self, i: int) -> dict[str, float]:
        """Reference values for the pitch at index i (NaN where not applicable / different game)."""
        same1 = i >= 1 and self.game[i - 1] == self.game[i]
        same2 = i >= 2 and self.game[i - 2] == self.game[i]
        # previous plate appearance's diff (circular |pitch - swing|, 0-500) as a wheel value: 0 is the same spot as
        # 1000; "+500" is the opposite side of the wheel
        d_prev = np.nan
        if same1 and self.swing[i - 1] == self.swing[i - 1]:
            dd = abs(self.pitch[i - 1] - self.swing[i - 1]) % 1000
            d_prev = min(dd, 1000 - dd)
        out = {
            "prev_pitch": self.pitch[i - 1] if same1 else np.nan,
            "prev_swing": self.swing[i - 1] if same1 else np.nan,
            "prev_pitch2": self.pitch[i - 2] if same2 else np.nan,
            "opp_pitch": self.opp_pitch[i], "opp_swing": self.opp_swing[i],
            "prev_diff": (d_prev if d_prev > 0 else 1000.0) if d_prev == d_prev else np.nan,
            "prev_diff_p500": d_prev + 500 if d_prev == d_prev else np.nan,
        }
        # transforms of the previous PA's pitch / swing / diff: digit reversal and the opposite side of the wheel
        for base in ("prev_pitch", "prev_swing", "prev_diff"):
            v = out[base]
            ok = v == v and 1 <= v <= 1000
            out[f"{base}_rev"] = _rev3(v) if ok else np.nan
            if base != "prev_diff":
                out[f"{base}_p500"] = _p500(v) if ok else np.nan
        return out


REF_NAMES = ["prev_pitch", "prev_swing", "prev_pitch2", "opp_pitch", "opp_swing", "prev_diff", "prev_diff_p500",
             "prev_pitch_rev", "prev_pitch_p500", "prev_swing_rev", "prev_swing_p500", "prev_diff_rev"]
SHIPPED_SPIKES = ["prev_pitch", "prev_swing"]   # the spikes in the deployed model (prev_pitch2 was dropped, 89dfc76)


def current_density(p: Pitcher, k: int, w: np.ndarray) -> np.ndarray:
    lg = np.log(p.B[k].astype(np.float64) + EPS) + (w[:, None] * p.R[k].astype(np.float64)).sum(0)
    return _softmax_log(lg)


def _hit_counts(p: Pitcher, ref: str, upto: int) -> tuple[np.ndarray, np.ndarray]:
    """Per earlier pitch index < upto: (was the reference defined, did it hit)."""
    defined = np.zeros(upto, bool)
    hit = np.zeros(upto, bool)
    for t in range(upto):
        v = p.refs(t)[ref]
        if v == v:
            defined[t] = True
            hit[t] = p.pitch[t] == v
    return defined, hit


def screen(only_incremental: bool = False) -> None:
    files = sorted(OUT.glob("*.npz"))
    P = [Pitcher(f) for f in files]
    w0 = _weights()
    print(f"{len(P)} pitchers, {sum(len(p.steps) for p in P)} backtest steps")
    pd.set_option("display.width", 200, "display.float_format", lambda x: f"{x:.4f}")

    # precompute per-step arrays: current density, actual index, refs, and the per-pitcher history needed online
    rng = np.random.default_rng(0)
    order = rng.permutation(len(P))
    train_idx, test_idx = set(order[: len(P) // 2]), set(order[len(P) // 2:])

    steps = []   # one dict per step
    for pi, p in enumerate(P):
        # online hit statistics per reference, over ALL of the pitcher's earlier pitches (not just walk steps)
        defined, hit = {}, {}
        for r in REF_NAMES:
            defined[r], hit[r] = _hit_counts(p, r, len(p.pitch))
        for si, i in enumerate(p.steps):
            c = current_density(p, si, w0)
            steps.append({"pi": pi, "si": si, "i": int(i), "c": c, "k": int(p.pitch[i]) - 1, "refs": p.refs(int(i)),
                          "train": pi in train_idx})
        p._def, p._hit = defined, hit
    print(f"train steps {sum(s['train'] for s in steps)}, test steps {sum(not s['train'] for s in steps)}")

    ll_cur = np.array([-math.log(s["c"][s["k"]] + EPS) for s in steps])
    train = np.array([s["train"] for s in steps])
    print(f"\nCurrent model: mean log-loss train {ll_cur[train].mean():.4f}  test {ll_cur[~train].mean():.4f} "
          f"(uniform = {math.log(1000):.4f})")

    def mix_ll(rates: dict[str, float], mask) -> float:
        tot = 0.0
        cnt = 0
        for s, keep in zip(steps, mask):
            if not keep:
                continue
            e = 0.0
            extra = 0.0
            for r, rate in rates.items():
                v = s["refs"][r]
                if v == v and rate > 0:
                    e += rate
                    if int(v) - 1 == s["k"]:
                        extra += rate
            tot += -math.log((1 - e) * s["c"][s["k"]] + extra + EPS)
            cnt += 1
        return tot / cnt

    # ---------------- 1. global constant spikes: fit each reference's rate on the training pitchers, one at a time
    print("\n=== 1. spike mixture, one global rate per reference (fit on train pitchers, scored on test) ===")
    grid = [0, 0.001, 0.002, 0.004, 0.007, 0.01, 0.015, 0.02, 0.03]
    fitted: dict[str, float] = {}
    rows = []
    for r in REF_NAMES:
        best = min(grid, key=lambda e: mix_ll({r: e}, train))
        fitted[r] = best
        base_t = ll_cur[~train][[steps[j]["refs"][r] == steps[j]["refs"][r] for j in np.where(~train)[0]]].mean()
        defined_test = np.array([steps[j]["refs"][r] == steps[j]["refs"][r] for j in np.where(~train)[0]])
        rows.append((r, best, int(defined_test.sum()), mix_ll({r: best}, ~train) - ll_cur[~train].mean()))
    print(pd.DataFrame(rows, columns=["reference", "fitted rate", "test steps with ref", "test d log-loss (all steps)"]).to_string(index=False))
    both = {r: e for r, e in fitted.items() if e > 0}
    print(f"all together {both}: test log-loss {mix_ll(both, ~train):.4f} (change {mix_ll(both, ~train) - ll_cur[~train].mean():+.4f})")

    # ---------------- 1b. incremental: each candidate added on top of the spikes the deployed model already has
    print(f"\n=== 1b. incremental on top of the shipped spike set {SHIPPED_SPIKES} (test pitchers) ===")
    base_rates = {r: fitted[r] for r in SHIPPED_SPIKES if fitted.get(r, 0) > 0}
    ll_shipped = mix_ll(base_rates, ~train)
    print(f"shipped set alone: test log-loss {ll_shipped:.4f} (change vs no spikes {ll_shipped - ll_cur[~train].mean():+.4f})")
    inc_rows = []
    for r in REF_NAMES:
        if r in SHIPPED_SPIKES:
            continue
        best = min(grid, key=lambda e: mix_ll({**base_rates, r: e}, train))
        defined_test = sum(steps[j]["refs"][r] == steps[j]["refs"][r] for j in np.where(~train)[0])
        inc_rows.append((r, best, defined_test, mix_ll({**base_rates, r: best}, ~train) - ll_shipped))
    print(pd.DataFrame(inc_rows, columns=["candidate", "fitted rate", "test steps with ref",
                                          "d log-loss on top of shipped"]).to_string(index=False))

    # pitcher-level bootstrap CI on each candidate's change (resample TEST PITCHERS; negative = helps)
    def step_ll(rates: dict[str, float], mask):
        out, pis = [], []
        for s, keep in zip(steps, mask):
            if not keep:
                continue
            e = extra = 0.0
            for r, rate in rates.items():
                v = s["refs"][r]
                if v == v and rate > 0:
                    e += rate
                    if int(v) - 1 == s["k"]:
                        extra += rate
            out.append(-math.log((1 - e) * s["c"][s["k"]] + extra + EPS))
            pis.append(s["pi"])
        return np.array(out), np.array(pis)

    def boot_ci(d: np.ndarray, pis: np.ndarray, n_boot: int = 2000):
        ids = np.unique(pis)
        sums = np.array([d[pis == i].sum() for i in ids])
        cnts = np.array([(pis == i).sum() for i in ids])
        rb = np.random.default_rng(1)
        vals = np.empty(n_boot)
        for b in range(n_boot):
            pick = rb.integers(0, len(ids), len(ids))
            vals[b] = sums[pick].sum() / cnts[pick].sum()
        per_pitcher = sums / cnts
        return np.percentile(vals, [2.5, 97.5]), float((per_pitcher < 0).mean())

    print("\n--- 1b CIs: change in test log-loss vs the shipped spikes, 95% bootstrap over test pitchers ---")
    base_ll, base_pis = step_ll(base_rates, ~train)
    fit_new = {r: b for r, b, _, _ in inc_rows if b > 0}
    combos = {r: {**base_rates, r: b} for r, b in fit_new.items()}
    if "prev_diff" in fit_new and "prev_pitch_p500" in fit_new:
        combos["prev_diff + prev_pitch_p500"] = {**base_rates, "prev_diff": fit_new["prev_diff"],
                                                 "prev_pitch_p500": fit_new["prev_pitch_p500"]}
    combos["all new with a fitted rate"] = {**base_rates, **fit_new}
    for name, rates in combos.items():
        ll_c, _ = step_ll(rates, ~train)
        d = ll_c - base_ll
        (lo, hi), frac = boot_ci(d, base_pis)
        print(f"{name:32s} change {d.mean():+.4f}  CI [{lo:+.4f}, {hi:+.4f}]  pitchers improved {frac * 100:.0f}%")
    if only_incremental:
        return

    # ---------------- 2a. per-pitcher online spike rates (shrunk to league, recency discounted)
    print("\n=== 2a. per-pitcher ONLINE spike rates (rate = (hits + m*league) / (n + m), recency-discounted) ===")
    league = {r: max(fitted.get(r, 0.0), 0.002) for r in REF_NAMES}
    for m in (50, 150, 400):
        for decay in (1.0, 0.995, 0.98):
            def online_ll(mask, refs_use):
                tot = cnt = 0
                for s, keep in zip(steps, mask):
                    if not keep:
                        continue
                    p = P[s["pi"]]
                    i = s["i"]
                    e = extra = 0.0
                    for r in refs_use:
                        v = s["refs"][r]
                        if v != v:
                            continue
                        d_, h_ = p._def[r][:i], p._hit[r][:i]
                        if d_.any():
                            ages = np.arange(i - 1, -1, -1)
                            wt = (decay ** ages) * d_
                            n_eff, hits = wt.sum(), (wt * h_).sum()
                        else:
                            n_eff = hits = 0.0
                        rate = (hits + m * league[r]) / (n_eff + m)
                        e += rate
                        if int(v) - 1 == s["k"]:
                            extra += rate
                    tot += -math.log((1 - e) * s["c"][s["k"]] + extra + EPS)
                    cnt += 1
                return tot / cnt
            use = [r for r in REF_NAMES if fitted.get(r, 0) > 0]
            print(f"m={m:>3} decay={decay}: test log-loss {online_ll(~train, use):.4f} "
                  f"(change {online_ll(~train, use) - ll_cur[~train].mean():+.4f}; global-constant {mix_ll(both, ~train) - ll_cur[~train].mean():+.4f})")

    # ---------------- 2b. per-pitcher online reweighting of the seven signals
    print("\n=== 2b. per-pitcher ONLINE signal reweighting (exponentiated-gradient on his own log-loss) ===")
    for eta in (0.0, 0.002, 0.005, 0.01, 0.02):
        for lam in (0.0, 0.01):
            tot = cnt = 0
            for pi, p in enumerate(P):
                if pi in train_idx:
                    continue
                w = w0.copy()
                for si, i in enumerate(p.steps):
                    R = p.R[si].astype(np.float64)
                    lg = np.log(p.B[si].astype(np.float64) + EPS) + (w[:, None] * R).sum(0)
                    c = _softmax_log(lg)
                    k = int(p.pitch[i]) - 1
                    tot += -math.log(c[k] + EPS)
                    cnt += 1
                    if eta > 0:           # d(-log c_k)/dw_s = -(R_s[k] - E_c[R_s]); step against it
                    # (update AFTER scoring this pitch: purely prequential)
                        g = -(R[:, k] - (R * c[None, :]).sum(1))
                        w = np.clip(w - eta * g + lam * (w0 - w), 0.0, 1.5)
            print(f"eta={eta:<6} lambda={lam:<5}: test log-loss {tot / cnt:.4f} (change vs fixed weights "
                  f"{tot / cnt - ll_cur[~train].mean():+.4f})")

    # ---------------- 4. number habits finer than the KDE
    print("\n=== 4. number habits (test pitchers), all prequential ===")
    def habit_ll(kind: str, strength: float, mask) -> float:
        tot = cnt = 0
        for s, keep in zip(steps, mask):
            if not keep:
                continue
            p = P[s["pi"]]
            i = s["i"]
            hist = p.pitch[:i].astype(int)
            c = s["c"]
            k = s["k"]
            if kind == "exact":                       # mixture with his own past exact values
                cnt_v = np.bincount((hist - 1) % 1000, minlength=1000).astype(float)
                h = cnt_v / cnt_v.sum()
                q = (1 - strength) * c + strength * h
            else:                                     # multiplicative digit factor, Dirichlet-smoothed, flat reference
                mod = {"last2": 100, "last1": 10}[kind]
                dig = hist % mod
                cn = np.bincount(dig, minlength=mod).astype(float)
                prob = (cn + 1.0) / (cn.sum() + mod)
                f = (prob * mod)[(np.arange(1, 1001)) % mod]
                lg = np.log(c + EPS) + strength * np.log(f)
                q = _softmax_log(lg)
            tot += -math.log(q[k] + EPS)
            cnt += 1
        return tot / cnt
    base_test = ll_cur[~train].mean()
    for kind, grid_s in (("exact", (0.005, 0.01, 0.02, 0.04)), ("last2", (0.25, 0.5, 1.0)), ("last1", (0.25, 0.5, 1.0))):
        best_tr = min(grid_s, key=lambda sv: habit_ll(kind, sv, train))
        print(f"{kind:>6}: best strength on train {best_tr}  test change {habit_ll(kind, best_tr, ~train) - base_test:+.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "screen", "incremental"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--min-pitches", type=int, default=250)
    a = ap.parse_args()
    if a.cmd == "collect":
        collect(a.workers, a.min_pitches)
    elif a.cmd == "incremental":
        screen(only_incremental=True)
    else:
        screen()

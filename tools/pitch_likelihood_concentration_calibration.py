"""Calibration of the pitch-likelihood model split by how concentrated each pitcher's own pitch distribution is.

Diagnostic only - it imports the live model (tools/pitch_likelihood_backtest.py) and changes nothing in it.
The uniform-floor mixtures below are evaluated post hoc on the model's own combined density, they are NOT part of the model.

Per backtest point (same frozen-history walk as backtest_pitcher) it records:
  - log-loss of baseline / combined / combined mixed with a uniform floor eps in FLOORS
  - PIT of the actual pitch, and the highest-density-region (HDR) mass at the actual pitch
    (the mass of all grid points the model likes at least as much as the actual one; uniform on [0,1] if calibrated)
  - the model's argmax (peak) and how far the actual pitch / previous peak sit from it

Usage:
  python -I tools/pitch_likelihood_concentration_calibration.py select           # pick pitchers -> $OUT/selected.csv
  python -I tools/pitch_likelihood_concentration_calibration.py run [--workers 4] # backtest them -> $OUT/points.csv
  python -I tools/pitch_likelihood_concentration_calibration.py report            # tables from points.csv
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
OUT = ROOT / "tools/probe_output/pitch_calibration"
CACHE = ROOT / "tools/diff_lab/cache/plays.pkl"
FLOORS = (0.0, 0.01, 0.02, 0.05, 0.10)
MIN_HISTORY = 100
MIN_PITCHES = 250


def concentration(pitches: np.ndarray, width: int = 200) -> float:
    """Share of a pitcher's pitches inside his densest circular window of `width` (uniform = width/1000)."""
    h = np.bincount(np.asarray(pitches, int) % 1000, minlength=1000)
    ext = np.concatenate([h, h[:width]])
    cs = np.concatenate([[0], np.cumsum(ext)])
    best = max(cs[i + width] - cs[i] for i in range(1000))
    return best / h.sum()


def select(per_group: int = 12) -> None:
    d = pd.read_pickle(CACHE)
    d = d[(d.play_type == "Swing") & d.pitch.notna()]
    rows = []
    for (pid, nm), s in d.groupby(["pitcher_id", "pitcher_name"]):
        if len(s) >= MIN_PITCHES:
            rows.append({"pitcher_id": pid, "pitcher": nm, "n": len(s), "conc200": concentration(s.pitch.values)})
    t = pd.DataFrame(rows).sort_values("conc200").reset_index(drop=True)
    print(f"{len(t)} pitchers with >= {MIN_PITCHES} pitches; conc200 range {t.conc200.min():.3f}-{t.conc200.max():.3f} "
          f"(uniform = 0.200)")
    mid = len(t) // 2
    pick = pd.concat([
        t.head(per_group).assign(group="low"),
        t.iloc[mid - per_group // 2: mid + per_group // 2].assign(group="mid"),
        t.tail(per_group).assign(group="high"),
    ])
    OUT.mkdir(parents=True, exist_ok=True)
    pick.to_csv(OUT / "selected.csv", index=False)
    t.to_csv(OUT / "all_concentration.csv", index=False)
    print(pick.to_string(index=False))


def _eval_pitcher(name: str) -> pd.DataFrame:
    warnings.filterwarnings("ignore")
    import logging
    logging.disable(logging.CRITICAL)
    import pitch_likelihood_backtest as bt
    import scouting_data

    df = scouting_data.load_pitcher_plays(name)
    if df.empty:
        return pd.DataFrame()
    cfg = bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0,
                    leverage_width=0.5, delta_cooldown_radius=50.0, weights=dict(bt.DEFAULT_WEIGHTS))
    builders = list(bt.SIGNAL_BUILDERS.values())
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    conc = concentration(pitches.pitch.values)
    recs, prev_peak = [], None
    for i in range(MIN_HISTORY, len(pitches)):
        cur_id = pitches.loc[i, "id"]
        state, baseline, signals = bt._prepare_step(df[df["id"] < cur_id], builders, cfg, target_row=pitches.loc[i])
        if state is None or signals is None:
            continue
        comb = bt.combine_signals(baseline, signals, cfg.min_effective_n, cfg.weights)
        actual = int(pitches.loc[i, "pitch"])
        k = actual - 1
        peak = int(comb.argmax()) + 1
        r = {"pitcher": name, "id": cur_id, "i": i, "actual": actual, "conc200": conc,
             "ll_base": -math.log(baseline[k] + 1e-12),
             "pit": float(comb[:k].sum() + comb[k] / 2),
             "hdr": float(comb[comb >= comb[k]].sum()),
             "base_hdr": float(baseline[baseline >= baseline[k]].sum()),
             "peak": peak, "peak_mass": float(comb.max()),
             "entropy": float(-(comb * np.log(comb + 1e-12)).sum()),
             "dist_actual_peak": float(bt._circular_distance(np.array([actual]), np.array([peak], float))[0, 0]),
             "dist_peak_prevpeak": (float(bt._circular_distance(np.array([peak]), np.array([prev_peak], float))[0, 0])
                                    if prev_peak else np.nan),
             "dist_actual_prev": float(bt._circular_distance(np.array([actual]),
                                                             np.array([state["prev_pitch"]], float))[0, 0])}
        for e in FLOORS:
            r[f"ll_floor{e}"] = -math.log((1 - e) * comb[k] + e / 1000 + 1e-12)
        recs.append(r)
        prev_peak = peak
    return pd.DataFrame(recs)


def run(workers: int) -> None:
    names = pd.read_csv(OUT / "selected.csv").pitcher.tolist()
    from concurrent.futures import ProcessPoolExecutor
    parts = []
    with ProcessPoolExecutor(workers) as ex:
        for nm, part in zip(names, ex.map(_eval_pitcher, names)):
            print(f"{nm}: {len(part)} points", flush=True)
            parts.append(part)
            pd.concat(parts).to_csv(OUT / "points.csv", index=False)


def report() -> None:
    p = pd.read_csv(OUT / "points.csv")
    sel = pd.read_csv(OUT / "selected.csv")[["pitcher", "group"]]
    p = p.merge(sel, on="pitcher")
    pd.set_option("display.width", 220, "display.max_columns", 30, "display.float_format", lambda x: f"{x:.4f}")
    p["conc_q"] = pd.qcut(p.conc200, 3, labels=["low", "mid", "high"])  # also pooled by point-level tercile
    for key in ("group",):
        g = p.groupby(key)
        print(f"\n== BY {key} ==  (pitchers: {p.groupby(key).pitcher.nunique().to_dict()}, points: {g.size().to_dict()})")
        t = g.agg(conc=("conc200", "mean"), ll_base=("ll_base", "mean"), ll_floor0=("ll_floor0.0", "mean"),
                  ll_f01=("ll_floor0.01", "mean"), ll_f02=("ll_floor0.02", "mean"), ll_f05=("ll_floor0.05", "mean"),
                  ll_f10=("ll_floor0.1", "mean"), entropy=("entropy", "mean"))
        t["model_vs_base"] = t.ll_base - t.ll_floor0
        print(t.to_string())

        print("\nHDR calibration - share of actual pitches whose HDR mass exceeds each level (nominal = 1 - level):")
        rows = {}
        for k, s in g:
            rows[k] = {f">{lv}": (s.hdr > lv).mean() for lv in (0.5, 0.8, 0.9, 0.95, 0.99)}
        print(pd.DataFrame(rows).T.assign(nominal_note="0.50 0.20 0.10 0.05 0.01").to_string())

        print("\nSame HDR calibration for the pitcher's own BASELINE (what a concentrated history alone would claim):")
        rows = {k: {f">{lv}": (s.base_hdr > lv).mean() for lv in (0.5, 0.8, 0.9, 0.95, 0.99)} for k, s in g}
        print(pd.DataFrame(rows).T.to_string())

        print("\nPIT deciles (share per decile; flat = 0.100):")
        rows = {k: np.histogram(s.pit, bins=10, range=(0, 1))[0] / len(s) for k, s in g}
        print(pd.DataFrame(rows, index=[f"{i/10:.1f}" for i in range(10)]).T.to_string())

        print("\nHow often does the pitcher actually jump away, vs how often does the model's peak jump?")
        t2 = g.agg(actual_vs_prev_med=("dist_actual_prev", "median"),
                   actual_far_share=("dist_actual_prev", lambda x: (x > 250).mean()),
                   peak_vs_prevpeak_med=("dist_peak_prevpeak", "median"),
                   peak_moves_share=("dist_peak_prevpeak", lambda x: (x > 50).mean()),
                   actual_vs_peak_med=("dist_actual_peak", "median"),
                   peak_mass=("peak_mass", "mean"))
        print(t2.to_string())

    print("\n== PER PITCHER (sorted by concentration) ==")
    t3 = p.groupby("pitcher").agg(group=("group", "first"), n=("id", "size"), conc=("conc200", "first"),
                                  ll_base=("ll_base", "mean"), ll_model=("ll_floor0.0", "mean"),
                                  ll_f02=("ll_floor0.02", "mean"), ll_f05=("ll_floor0.05", "mean"),
                                  tail95=("hdr", lambda x: (x > 0.95).mean()), tail99=("hdr", lambda x: (x > 0.99).mean()))
    t3["gain"] = t3.ll_base - t3.ll_model
    print(t3.sort_values("conc").to_string())
    # correlation across pitchers
    print("\nSpearman across pitchers: conc vs tail95 =", round(t3[["conc", "tail95"]].corr("spearman").iloc[0, 1], 3),
          "  conc vs gain =", round(t3[["conc", "gain"]].corr("spearman").iloc[0, 1], 3))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["select", "run", "report"])
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    {"select": select, "run": lambda: run(a.workers), "report": report}[a.cmd]()

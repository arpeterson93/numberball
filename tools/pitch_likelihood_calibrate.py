"""
Multi-pitcher calibration for the pitch-likelihood model
(tools/pitch_likelihood_backtest.py): fits the seven signal weights, then
grid-searches sigma/pitch_width/leverage_width/min_effective_n, each stage
gated by held-out log-loss AND PIT calibration on a split held out from
whatever was fit/selected on - never picked and graded on the same pitchers.

Why a three-way split BY PITCHER, not by point: backtest points within one
pitcher's career are strongly autocorrelated (same growing history), so more
points from the same pitcher isn't much more independent information. The
real test of a hyperparameter choice is whether it generalizes to a pitcher
it never saw, so TRAIN fits, VAL selects among width/sigma candidates (and is
what the grid search is actually gated on), TEST is touched exactly once at
the very end to report the real, un-peeked-at gain.

Why points are SUBSAMPLED per pitcher (not every pitch, like
backtest_pitcher's own full walk): each point's cost scales with how much
history has accumulated by then, so a full career walk is roughly O(n^2) -
one deep pitcher's full backtest (1740 points) took 5m42s in testing. A grid
search needs dozens of full precompute passes, so this samples
--points-per-pitcher points evenly across each pitcher's eligible range
instead of walking every pitch.

Why signal computation is cached separately from weight-fitting: changing a
weight doesn't change any signal's own density, only how combine_signals
pools them - so every point's (baseline, per-signal density/n_eff, actual)
is computed ONCE per widths/sigma setting and reused for however many weight
vectors scipy.optimize tries, instead of redoing the expensive matching+KDE
work per weight guess. min_effective_n is likewise a combine-time knob, not
a signal-computation one, so it's free to re-grid without re-precomputing.

Stages (each reads/writes tools/pitch_likelihood_calib_state.json, so they
can be run as separate, separately-timed/backgrounded invocations):

    python tools/pitch_likelihood_calibrate.py fetch
        [--roster-csv plays_from_pitchers_200+_bf.csv] [--min-pitches 300]
        [--train-frac 0.5] [--val-frac 0.25] [--seed 0]
        Builds the train/val/test pitcher split and warms the per-pitcher
        data cache (one Supabase query per pitcher, then cached to disk).

    python tools/pitch_likelihood_calibrate.py fit-weights
        [--points-per-pitcher 25] [--min-history 30]
        [--sigma 100] [--pitch-width 200] [--cooldown-radius 50] [--delta-cooldown-radius 50]
        [--leverage-width 1.0] [--min-effective-n 20]
        Precomputes TRAIN at the given widths/sigma, fits the seven weights by
        minimizing TRAIN log-loss (bounded [0,3], matching the artifact
        slider's own range), then reports VAL/TEST log-loss and PIT chi2 for
        default (all-1.0) vs fitted weights.

    python tools/pitch_likelihood_calibrate.py fit-widths
        [--sigma-grid 60,100,150] [--pitch-width-grid 150,200,300]
        [--leverage-width-grid 0.5,1.0,1.5] [--min-eff-grid 10,20,30]
        Coordinate-wise search (one parameter at a time, others held at the
        current best) - NOT a full cross-product grid, to keep the number of
        expensive re-precompute passes small. Each candidate re-fits weights
        on TRAIN (warm-started from the previous best) and is scored on VAL.
        min_effective_n candidates reuse whatever precompute the winning
        sigma/pitch_width/leverage_width already produced.

    python tools/pitch_likelihood_calibrate.py report
        Final, single-touch TEST evaluation of whatever config is currently
        saved in the state file - run this once, at the end, not during
        search.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scouting_data  # noqa: E402
import utils  # noqa: E402
from pitch_likelihood_backtest import (  # noqa: E402
    Config, SIGNAL_BUILDERS, _prepare_step, combine_signals, GRID_SIZE,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "tools" / "probe_output" / "calib_cache"
STATE_PATH = ROOT / "tools" / "pitch_likelihood_calib_state.json"
SIGNAL_NAMES = list(SIGNAL_BUILDERS.keys())  # fixed order - same order combine_signals/the artifact use


# --------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(f"Wrote {STATE_PATH}", file=sys.stderr)


# ------------------------------------------------------------------ roster

def scan_bf_counts(names: list[str]) -> dict[str, int]:
    """Batters faced per name = len(scouting_data.load_pitcher_plays(name)) -
    get_plays_for_pitcher(_id) already drops scrimmage games and enrich_df
    already drops steal rows, so every row left is a real plate appearance
    (play_type == "Swing", the user's own definition of BF). Cached to disk
    (one row per name) since scanning the full ~900-name player directory is
    a several-minute, one-Supabase-query-per-name pass - not something to
    redo every time `fetch` runs just because --min-bf changed. Delete
    tools/probe_output/calib_cache/bf_counts.json to force a fresh scan
    (e.g. after a new season's plays land)."""
    cache_path = CACHE_DIR / "bf_counts.json"
    counts: dict[str, int] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    missing = [n for n in names if n not in counts]
    if missing:
        t0 = time.time()
        for i, name in enumerate(missing):
            df = get_pitcher_df(name)
            counts[name] = 0 if df.empty else int(len(df))
            if (i + 1) % 50 == 0 or i + 1 == len(missing):
                print(f"  BF scan {i + 1}/{len(missing)} ({time.time() - t0:.0f}s)", file=sys.stderr)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(counts))
    return counts


def load_roster(min_bf: int) -> list[str]:
    """Live from Supabase (not the local plays_from_pitchers_200+_bf.csv -
    its population is unreproducible, generated by no committed script, and
    the live tables have grown since it was exported), scanning the FULL
    player directory (pitchers, batters, catchers all mixed - most will
    just come back with 0 BF)."""
    names = scouting_data.all_player_names()
    counts = scan_bf_counts(names)
    return sorted(n for n, c in counts.items() if c >= min_bf)


def split_roster(names: list[str], seed: int, train_frac: float, val_frac: float):
    rng = np.random.default_rng(seed)
    shuffled = list(names)
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = int(round(n * train_frac))
    n_val = int(round(n * val_frac))
    return shuffled[:n_train], shuffled[n_train:n_train + n_val], shuffled[n_train + n_val:]


# -------------------------------------------------------------- pitcher data

def _pitcher_cache_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in name)
    return CACHE_DIR / f"pitcher_{safe}.pkl"


def get_pitcher_df(name: str) -> pd.DataFrame:
    """Enriched play history for one pitcher - cached to disk after the
    first (Supabase-backed) load, so every later stage/retry is free. The
    cache is keyed on name only (not on widths/sigma) since the raw rows
    don't change when a hyperparameter does."""
    path = _pitcher_cache_path(name)
    if path.exists():
        return pickle.loads(path.read_bytes())
    df = scouting_data.load_pitcher_plays(name)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(df))
    return df


def sample_point_ids(df: pd.DataFrame, min_history: int, k: int) -> list:
    """Evenly spaced eligible pitch ids across the career (not random, not
    every pitch) - a career-long tendency shift should be represented
    proportionally rather than having an early or late stretch over/under-
    sampled by chance, and evenly-spaced keeps the sample's total compute
    cost predictable (~k x average-history-size) instead of O(n^2)."""
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    if len(pitches) <= min_history:
        return []
    eligible = pitches["id"].iloc[min_history:].to_numpy()
    if len(eligible) <= k:
        return eligible.tolist()
    idx = np.unique(np.linspace(0, len(eligible) - 1, k).round().astype(int))
    return eligible[idx].tolist()


# ----------------------------------------------------------------- precompute

def precompute_split(names: list[str], cfg: Config, points_per_pitcher: int,
                      min_history: int, label: str) -> list[dict]:
    """One {pitcher, actual, baseline, signals} record per sampled point -
    the expensive stage. Everything downstream (weight fitting, log-loss/PIT
    reporting) reuses this without touching df_hist again."""
    builders = [SIGNAL_BUILDERS[n] for n in SIGNAL_NAMES]
    records: list[dict] = []
    t0 = time.time()
    for i, name in enumerate(names):
        df = get_pitcher_df(name)
        if df.empty:
            continue
        point_ids = sample_point_ids(df, min_history, points_per_pitcher)
        pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
        n_kept = 0
        for cur_id in point_ids:
            row_idx = pitches.index[pitches["id"] == cur_id]
            if len(row_idx) == 0:
                continue
            target_row = pitches.loc[int(row_idx[0])]
            df_hist = df[df["id"] < cur_id]
            state, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=target_row)
            if state is None or not signals:
                continue
            records.append({
                "pitcher": name, "actual": int(target_row["pitch"]),
                "baseline": baseline, "signals": signals,
            })
            n_kept += 1
        print(f"  [{label}] {i + 1}/{len(names)} {name}: {n_kept} points "
              f"({time.time() - t0:.0f}s elapsed)", file=sys.stderr)
    print(f"  [{label}] {len(records)} total points from {len(names)} pitchers "
          f"in {time.time() - t0:.0f}s", file=sys.stderr)
    return records


# -------------------------------------------------------------- scoring

def mean_logloss(records: list[dict], weights_by_name: dict, min_effective_n: float) -> float:
    eps = 1e-12
    total = 0.0
    for r in records:
        combined = combine_signals(r["baseline"], r["signals"], min_effective_n, weights_by_name)
        total += -np.log(combined[r["actual"] - 1] + eps)
    return total / len(records)


def pit_values(records: list[dict], weights_by_name: dict, min_effective_n: float) -> list[float]:
    eps = 1e-12
    out = []
    for r in records:
        combined = combine_signals(r["baseline"], r["signals"], min_effective_n, weights_by_name)
        idx = r["actual"] - 1
        out.append(float(combined[:idx].sum() + combined[idx] / 2))
    return out


def pit_chi2_uniform(pits: list[float], n_bins: int = 10) -> float:
    """Chi-square of the PIT histogram against a flat reference - the
    calibration half of the gate alongside log-loss. ~0 = flat/honest;
    large = skewed toward the tails (overconfident) or the middle
    (underconfident, rare here). Not a p-value, just a comparable scalar
    across candidates with the same point count."""
    counts, _ = np.histogram(pits, bins=n_bins, range=(0, 1))
    expected = len(pits) / n_bins
    return float(((counts - expected) ** 2 / expected).sum())


def mean_baseline_logloss(records: list[dict]) -> float:
    eps = 1e-12
    return float(np.mean([-np.log(r["baseline"][r["actual"] - 1] + eps) for r in records]))


def fit_weights(train_records: list[dict], min_effective_n: float, x0: dict | None = None) -> dict:
    x0_vec = [(x0 or {}).get(n, 1.0) for n in SIGNAL_NAMES]

    def objective(x):
        return mean_logloss(train_records, dict(zip(SIGNAL_NAMES, x)), min_effective_n)

    res = minimize(objective, x0_vec, method="L-BFGS-B", bounds=[(0.0, 3.0)] * len(SIGNAL_NAMES))
    return dict(zip(SIGNAL_NAMES, (float(v) for v in res.x)))


def bootstrap_weight_variance(records: list[dict], min_effective_n: float, b: int,
                               x0: dict, rng: np.random.Generator) -> np.ndarray:
    """Within-sample bootstrap: resample these SAME records with replacement
    b times and refit weights each time - estimates how much a weight fit on
    exactly this many points would jitter from sampling noise ALONE, holding
    whatever actually generated these points fixed (one real pitcher's own
    signal depths/shapes). Cheap: only combine_signals + scipy.optimize per
    resample, no new matching/KDE work, since the records are already
    precomputed. Returns a (b, 6) array of weight vectors, one per resample -
    its per-column variance is this pitcher's own noise-floor contribution to
    the cross-pitcher archetype-scatter check."""
    n = len(records)
    out = np.empty((b, len(SIGNAL_NAMES)))
    for i in range(b):
        idx = rng.integers(0, n, size=n)
        sample = [records[j] for j in idx]
        w = fit_weights(sample, min_effective_n, x0=x0)
        out[i] = [w[name] for name in SIGNAL_NAMES]
    return out


def bootstrap_margin_variance(records: list[dict], weights: dict, min_effective_n: float,
                               b: int, rng: np.random.Generator) -> np.ndarray:
    """Within-sample bootstrap of ONE pitcher's margin (baseline logloss -
    fitted logloss, at the FIXED global weights - no per-resample fitting,
    so this is just two mean_logloss evaluations per resample, much cheaper
    than bootstrap_weight_variance). Same noise-floor idea, applied to
    pitcher-report's margin statistic instead of per-pitcher weight fits:
    how much would THIS pitcher's margin jitter from sampling noise alone,
    holding whatever actually generated these points fixed. Returns a
    (b,) array of margins."""
    n = len(records)
    out = np.empty(b)
    for i in range(b):
        idx = rng.integers(0, n, size=n)
        sample = [records[j] for j in idx]
        out[i] = mean_baseline_logloss(sample) - mean_logloss(sample, weights, min_effective_n)
    return out


def circular_distance_grid(actual: int) -> np.ndarray:
    """|grid_value - actual| wrapped at the 1000-wide pitch wheel, for every
    grid point 1..1000 - same wraparound convention as pitch_circ_delta and
    the game's own circular_diff elsewhere. Max is 500 (opposite side of the
    wheel)."""
    grid = np.arange(1, GRID_SIZE + 1)
    d = np.abs(grid - actual)
    return np.minimum(d, GRID_SIZE - d)


def report_distance_split(label: str, records: list[dict], weights: dict, min_effective_n: float,
                           k_list: tuple[int, ...] = (90, 180)) -> dict:
    """Two closeness diagnostics log-loss doesn't report directly, for
    baseline vs. fitted-combined - log-loss only scores probability mass AT
    the exact actual pitch, so it only rewards "close but not exact" as a
    side effect of the KDE's own smoothing; these measure closeness
    directly instead of leaving it implicit:
      - peak distance: circular distance from the density's own argmax (its
        single best guess) to the actual pitch - "how far off was our top
        pick."
      - expected distance: sum(density * circular_distance) - the FULL
        distribution's own expected miss, which also credits a model that
        spreads plausible mass near the truth without peaking exactly on it
        (a peak-only reading can't tell a confident near-miss from a vague
        one).
      - mass within K: sum(density[dist <= K]) for each K in k_list - the
        probability the model itself placed within K wheel-units of the
        actual pitch. K=180 is roughly Numberball's own "gets on base"
        radius, so this reads as "how often would swinging where our model
        pointed have landed a hitter in the good zone," not just raw
        pitch-matching accuracy.
    """
    def _stats(get_density):
        peak_d, exp_d = [], []
        mass = {k: [] for k in k_list}
        for r in records:
            dist = circular_distance_grid(r["actual"])
            density = np.asarray(get_density(r))
            peak_d.append(float(dist[int(np.argmax(density))]))
            exp_d.append(float(np.sum(density * dist)))
            for k in k_list:
                mass[k].append(float(np.sum(density[dist <= k])))
        return (float(np.mean(peak_d)), float(np.mean(exp_d)),
                {k: float(np.mean(v)) for k, v in mass.items()})

    base_peak, base_exp, base_mass = _stats(lambda r: r["baseline"])
    fitted_peak, fitted_exp, fitted_mass = _stats(
        lambda r: combine_signals(r["baseline"], r["signals"], min_effective_n, weights))

    mass_str = "  ".join(f"K={k}: {base_mass[k] * 100:.1f}%->{fitted_mass[k] * 100:.1f}%" for k in k_list)
    print(f"[{label}] peak dist baseline/fitted={base_peak:.1f}/{fitted_peak:.1f}  "
          f"expected dist baseline/fitted={base_exp:.1f}/{fitted_exp:.1f}  mass-within {mass_str}", file=sys.stderr)
    return {
        "split": label,
        "peak_dist_baseline": base_peak, "peak_dist_fitted": fitted_peak,
        "expected_dist_baseline": base_exp, "expected_dist_fitted": fitted_exp,
        "mass_within_baseline": base_mass, "mass_within_fitted": fitted_mass,
    }


def report_split(label: str, records: list[dict], weights: dict, min_effective_n: float) -> dict:
    base_ll = mean_baseline_logloss(records)
    default_ll = mean_logloss(records, {n: 1.0 for n in SIGNAL_NAMES}, min_effective_n)
    fitted_ll = mean_logloss(records, weights, min_effective_n)
    default_chi2 = pit_chi2_uniform(pit_values(records, {n: 1.0 for n in SIGNAL_NAMES}, min_effective_n))
    fitted_chi2 = pit_chi2_uniform(pit_values(records, weights, min_effective_n))
    row = {
        "split": label, "n_points": len(records),
        "logloss_baseline": base_ll, "logloss_default_weights": default_ll,
        "logloss_fitted_weights": fitted_ll,
        "pit_chi2_default_weights": default_chi2, "pit_chi2_fitted_weights": fitted_chi2,
    }
    print(f"[{label}] n={len(records)}  baseline={base_ll:.4f}  "
          f"default-weights={default_ll:.4f}  fitted-weights={fitted_ll:.4f}  "
          f"PIT chi2 default/fitted={default_chi2:.1f}/{fitted_chi2:.1f}", file=sys.stderr)
    return row


# --------------------------------------------------------------------- CLI

def cmd_fetch(args):
    names = load_roster(args.min_bf)
    train, val, test = split_roster(names, args.seed, args.train_frac, args.val_frac)
    print(f"Roster: {len(names)} pitchers with >= {args.min_bf} BF (live from Supabase) -> "
          f"{len(train)} train / {len(val)} val / {len(test)} test", file=sys.stderr)
    t0 = time.time()
    for i, name in enumerate(names):
        get_pitcher_df(name)
        if (i + 1) % 10 == 0 or i + 1 == len(names):
            print(f"  warmed {i + 1}/{len(names)} ({time.time() - t0:.0f}s)", file=sys.stderr)
    state = load_state()
    state.update({
        "min_bf": args.min_bf, "seed": args.seed,
        "train_pitchers": train, "val_pitchers": val, "test_pitchers": test,
    })
    save_state(state)


def cmd_fit_weights(args):
    state = load_state()
    if "train_pitchers" not in state:
        print("Run 'fetch' first.", file=sys.stderr)
        sys.exit(1)
    cfg = Config(sigma=args.sigma, min_effective_n=args.min_effective_n, pitch_width=args.pitch_width,
                 cooldown_radius=args.cooldown_radius, leverage_width=args.leverage_width,
                 delta_cooldown_radius=args.delta_cooldown_radius)

    train_rec = precompute_split(state["train_pitchers"], cfg, args.points_per_pitcher, args.min_history, "train")
    val_rec = precompute_split(state["val_pitchers"], cfg, args.points_per_pitcher, args.min_history, "val")
    test_rec = precompute_split(state["test_pitchers"], cfg, args.points_per_pitcher, args.min_history, "test")

    weights = fit_weights(train_rec, cfg.min_effective_n)
    print("\nFitted weights:", {k: round(v, 3) for k, v in weights.items()}, file=sys.stderr)

    print()
    report_split("train", train_rec, weights, cfg.min_effective_n)
    report_split("val", val_rec, weights, cfg.min_effective_n)
    report_split("test", test_rec, weights, cfg.min_effective_n)

    state.update({
        "points_per_pitcher": args.points_per_pitcher, "min_history": args.min_history,
        "sigma": cfg.sigma, "pitch_width": cfg.pitch_width, "cooldown_radius": cfg.cooldown_radius,
        "delta_cooldown_radius": cfg.delta_cooldown_radius,
        "leverage_width": cfg.leverage_width, "min_effective_n": cfg.min_effective_n,
        "weights": weights,
    })
    save_state(state)


def _parse_grid(s: str) -> list[float]:
    return [float(x) for x in s.split(",")]


def cmd_fit_widths(args):
    state = load_state()
    if "weights" not in state:
        print("Run 'fit-weights' first (need a starting weight vector to warm-start from).", file=sys.stderr)
        sys.exit(1)
    state.pop("fit_widths_history", None)  # dead key name from before resumable checkpointing

    cfg = Config(sigma=state["sigma"], min_effective_n=state["min_effective_n"],
                 pitch_width=state["pitch_width"], cooldown_radius=state["cooldown_radius"],
                 leverage_width=state["leverage_width"],
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    weights = dict(state["weights"])
    ppp, min_hist = state["points_per_pitcher"], state["min_history"]
    train_names, val_names = state["train_pitchers"], state["val_pitchers"]

    # Resumable checkpointing: every candidate's result is saved to the state
    # file the moment it's known, keyed by "param=value" - a kill mid-run (the
    # OS reaping this for memory, a crash, Ctrl-C) loses at most the ONE
    # candidate in flight, not the whole stage. Re-running this command picks
    # up exactly where it left off: a cached "param=value" is reused with no
    # recompute, since the coordinate search is a deterministic function of
    # (candidate value, position in the fixed param order, prior winners) -
    # nothing about what a given candidate means ever changes between runs.
    progress: dict[str, dict] = {
        f"{c['param']}={c['value']}": c for c in state.get("fit_widths_progress", [])
    }

    def checkpoint():
        state["fit_widths_progress"] = list(progress.values())
        state["sigma"], state["pitch_width"] = cfg.sigma, cfg.pitch_width
        state["leverage_width"], state["min_effective_n"] = cfg.leverage_width, cfg.min_effective_n
        state["delta_cooldown_radius"] = cfg.delta_cooldown_radius
        state["weights"] = weights
        save_state(state)

    def eval_candidate(cfg_try: Config, w0: dict, param: str, value) -> tuple[float, float, dict]:
        key = f"{param}={value}"
        cached = progress.get(key)
        if cached is not None:
            print(f"  {param}={value}: (cached) val logloss={cached['val_logloss']:.4f} "
                  f"pit_chi2={cached['val_pit_chi2']:.1f}", file=sys.stderr)
            return cached["val_logloss"], cached["val_pit_chi2"], cached["weights"]
        train_rec = precompute_split(train_names, cfg_try, ppp, min_hist, "train(cand)")
        val_rec = precompute_split(val_names, cfg_try, ppp, min_hist, "val(cand)")
        w = fit_weights(train_rec, cfg_try.min_effective_n, x0=w0)
        val_ll = mean_logloss(val_rec, w, cfg_try.min_effective_n)
        val_chi2 = pit_chi2_uniform(pit_values(val_rec, w, cfg_try.min_effective_n))
        print(f"  {param}={value}: val logloss={val_ll:.4f} pit_chi2={val_chi2:.1f}", file=sys.stderr)
        progress[key] = {"param": param, "value": value, "val_logloss": val_ll,
                          "val_pit_chi2": val_chi2, "weights": w}
        checkpoint()
        return val_ll, val_chi2, w

    coords = [
        ("sigma", _parse_grid(args.sigma_grid),
         lambda v, c: Config(v, c.min_effective_n, c.pitch_width, c.cooldown_radius, c.leverage_width,
                              delta_cooldown_radius=c.delta_cooldown_radius)),
        ("pitch_width", [int(v) for v in _parse_grid(args.pitch_width_grid)],
         lambda v, c: Config(c.sigma, c.min_effective_n, int(v), c.cooldown_radius, c.leverage_width,
                              delta_cooldown_radius=c.delta_cooldown_radius)),
        ("leverage_width", _parse_grid(args.leverage_width_grid),
         lambda v, c: Config(c.sigma, c.min_effective_n, c.pitch_width, c.cooldown_radius, v,
                              delta_cooldown_radius=c.delta_cooldown_radius)),
    ]

    for param_name, grid, make_cfg in coords:
        print(f"\n=== searching {param_name} over {grid} (others held fixed) ===", file=sys.stderr)
        best_ll, best_chi2, best_cfg, best_w = None, None, cfg, weights
        for v in grid:
            cfg_try = make_cfg(v, cfg)
            val_ll, val_chi2, w = eval_candidate(cfg_try, weights, param_name, v)
            if best_ll is None or val_ll < best_ll:
                best_ll, best_chi2, best_cfg, best_w = val_ll, val_chi2, cfg_try, w
        cfg = best_cfg
        weights = best_w
        print(f"  -> best {param_name}: {getattr(cfg, param_name)} (val logloss {best_ll:.4f}, pit_chi2 {best_chi2:.1f})",
              file=sys.stderr)
        checkpoint()

    # min_effective_n is combine-time only - one fresh precompute at the
    # winning sigma/pitch_width/leverage_width covers every candidate below
    # (cheap to redo once here rather than threading train_rec/val_rec
    # through the cache-or-compute branch above for the rare case the
    # overall winner came from a cached candidate).
    print(f"\n=== searching min_effective_n over {args.min_eff_grid} (one shared precompute) ===", file=sys.stderr)
    train_rec = precompute_split(train_names, cfg, ppp, min_hist, "train(min_eff)")
    val_rec = precompute_split(val_names, cfg, ppp, min_hist, "val(min_eff)")
    grid = _parse_grid(args.min_eff_grid)
    best_ll, best_n = None, cfg.min_effective_n
    for n in grid:
        key = f"min_effective_n={n}"
        cached = progress.get(key)
        if cached is not None:
            w, val_ll, val_chi2 = cached["weights"], cached["val_logloss"], cached["val_pit_chi2"]
            print(f"  min_effective_n={n}: (cached) val logloss={val_ll:.4f} pit_chi2={val_chi2:.1f}", file=sys.stderr)
        else:
            w = fit_weights(train_rec, n, x0=weights)
            val_ll = mean_logloss(val_rec, w, n)
            val_chi2 = pit_chi2_uniform(pit_values(val_rec, w, n))
            print(f"  min_effective_n={n}: val logloss={val_ll:.4f} pit_chi2={val_chi2:.1f}", file=sys.stderr)
            progress[key] = {"param": "min_effective_n", "value": n, "val_logloss": val_ll,
                              "val_pit_chi2": val_chi2, "weights": w}
        if best_ll is None or val_ll < best_ll:
            best_ll, best_n, weights = val_ll, n, w
    cfg.min_effective_n = best_n
    print(f"  -> best min_effective_n: {best_n} (val logloss {best_ll:.4f})", file=sys.stderr)
    checkpoint()

    print("\nFinal config:", {k: state[k] for k in
          ("sigma", "pitch_width", "leverage_width", "min_effective_n", "weights")}, file=sys.stderr)
    print("Run 'report' for the single-touch TEST evaluation.", file=sys.stderr)


def cmd_archetype_scatter(args):
    """Fits weights independently PER PITCHER (on their own points only, same
    bounded [0,3] L-BFGS-B fit as the global stage) and reports whether the
    resulting weight vectors show real cross-signal structure - clustering,
    correlation - or just scatter noisily around the global optimum. This is
    a diagnostic for "would archetypes/per-cluster weights be worth it,"
    not a model: each pitcher's own fit here is on far too little data
    (--points-per-pitcher points, ~30 by default) to trust individually, so
    read the AGGREGATE pattern across pitchers, not any one pitcher's row.

    Uses train+val only (test stays untouched, same discipline as the other
    stages) at whatever widths/sigma are currently in the state file, so run
    this after fit-weights (and optionally fit-widths)."""
    state = load_state()
    if "train_pitchers" not in state:
        print("Run 'fetch' first.", file=sys.stderr)
        sys.exit(1)
    cfg = Config(sigma=state.get("sigma", 100.0), min_effective_n=state.get("min_effective_n", 20.0),
                 pitch_width=state.get("pitch_width", 200), cooldown_radius=state.get("cooldown_radius", 50.0),
                 leverage_width=state.get("leverage_width", 1.0),
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    names = state["train_pitchers"] + state["val_pitchers"]
    builders = [SIGNAL_BUILDERS[n] for n in SIGNAL_NAMES]

    rng = np.random.default_rng(args.seed)
    rows = []
    within_vars = []  # one (6,) array per pitcher, only when --bootstrap > 0
    t0 = time.time()
    for i, name in enumerate(names):
        df = get_pitcher_df(name)
        if df.empty:
            continue
        point_ids = sample_point_ids(df, args.min_history, args.points_per_pitcher)
        pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
        records = []
        for cur_id in point_ids:
            row_idx = pitches.index[pitches["id"] == cur_id]
            if len(row_idx) == 0:
                continue
            target_row = pitches.loc[int(row_idx[0])]
            df_hist = df[df["id"] < cur_id]
            st, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=target_row)
            if st is None or not signals:
                continue
            records.append({"pitcher": name, "actual": int(target_row["pitch"]), "baseline": baseline, "signals": signals})
        if len(records) < args.min_points:
            print(f"  [{i + 1}/{len(names)}] skip {name}: only {len(records)} usable points "
                  f"(< {args.min_points})", file=sys.stderr)
            continue
        w = fit_weights(records, cfg.min_effective_n)
        base_ll = mean_baseline_logloss(records)
        fit_ll = mean_logloss(records, w, cfg.min_effective_n)
        row = {"pitcher": name, "n_points": len(records), "logloss_baseline": base_ll, "logloss_fitted": fit_ll}
        row.update({f"w_{n}": w[n] for n in SIGNAL_NAMES})
        boot_note = ""
        if args.bootstrap > 0:
            boot = bootstrap_weight_variance(records, cfg.min_effective_n, args.bootstrap, w, rng)
            pitcher_within_var = boot.var(axis=0, ddof=1)
            within_vars.append(pitcher_within_var)
            row.update({f"bootvar_{n}": pitcher_within_var[j] for j, n in enumerate(SIGNAL_NAMES)})
            boot_note = f"  [boot var mean={pitcher_within_var.mean():.3f}]"
        rows.append(row)
        print(f"  [{i + 1}/{len(names)}] {name}: n={len(records)} "
              f"{ {k: round(v, 2) for k, v in w.items()} }{boot_note} ({time.time() - t0:.0f}s elapsed)", file=sys.stderr)

    out_df = pd.DataFrame(rows)
    out_path = ROOT / "tools" / "probe_output" / "per_pitcher_weights.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path} ({len(out_df)} pitchers)", file=sys.stderr)

    W = out_df[[f"w_{n}" for n in SIGNAL_NAMES]].to_numpy()
    print(f"\nPer-signal weight stats across {len(out_df)} pitchers (own-data fits, bounds [0,3]):")
    for i, n in enumerate(SIGNAL_NAMES):
        col = W[:, i]
        print(f"  {n:28s} mean={col.mean():.3f}  std={col.std():.3f}  "
              f"at-0={np.mean(col < 1e-6) * 100:.0f}%  at-cap={np.mean(col > 2.999) * 100:.0f}%")

    print("\nPairwise correlation between signal weights across pitchers "
          "(structure would show here as off-diagonal |r| well above noise):")
    corr = np.corrcoef(W.T)
    print("                              " + " ".join(f"{n[:10]:>10s}" for n in SIGNAL_NAMES))
    for i, n in enumerate(SIGNAL_NAMES):
        print(f"  {n:28s}" + " ".join(f"{corr[i, j]:10.2f}" for j in range(len(SIGNAL_NAMES))))

    # Cheap structure check: PCA via SVD on standardized weights. If the six
    # signal weights varied independently across pitchers (no archetypes,
    # just per-pitcher noise), explained variance would split roughly evenly
    # (~1/6 each). A couple of components capturing much more than that is
    # evidence of real co-varying structure worth clustering on; a flat
    # spectrum is evidence it's mostly noise.
    Wc = W - W.mean(axis=0)
    std = Wc.std(axis=0)
    std[std == 0] = 1.0
    Wz = Wc / std
    _, s, _ = np.linalg.svd(Wz, full_matrices=False)
    explained = (s ** 2) / (s ** 2).sum()
    print("\nPCA on standardized per-pitcher weight vectors (flat ~1/6 each = no structure, "
          "front-loaded = real co-variation):")
    for i, ev in enumerate(explained):
        print(f"  PC{i + 1}: {ev * 100:.1f}%" + ("  <- component" if i < 2 else ""))

    if args.bootstrap > 0 and within_vars:
        # Classic random-effects decomposition: Var(point estimate across
        # pitchers) = Var(true pitcher effect) + E[Var(estimation noise |
        # pitcher)]. The second term is what the within-pitcher bootstrap
        # just measured directly (resampling each pitcher's OWN points,
        # holding that pitcher fixed); subtracting it off the observed
        # cross-pitcher variance isolates how much of the scatter is real
        # between-pitcher signal versus small-sample noise on 30 points.
        within_vars = np.array(within_vars)  # (n_pitchers, 6)
        mean_within = within_vars.mean(axis=0)
        between_observed = W.var(axis=0, ddof=1)
        implied_true_between = np.maximum(0.0, between_observed - mean_within)
        print(f"\nVariance decomposition (within-pitcher bootstrap, b={args.bootstrap} resamples/pitcher):")
        print(f"  {'signal':28s}{'between (observed)':>20s}{'within (noise floor)':>22s}"
              f"{'implied true between':>24s}{'ratio':>8s}")
        for i, n in enumerate(SIGNAL_NAMES):
            ratio = implied_true_between[i] / mean_within[i] if mean_within[i] > 1e-9 else float("inf")
            print(f"  {n:28s}{between_observed[i]:20.4f}{mean_within[i]:22.4f}"
                  f"{implied_true_between[i]:24.4f}{ratio:8.2f}")
        print("  ratio << 1: scatter is explained by sampling noise alone - no real pitcher-level\n"
              "    variation to pool toward; the global weight vector is already the right model.\n"
              "  ratio >> 1: real variation beyond noise - partial pooling (shrink each pitcher's\n"
              "    weights toward global, by that pitcher's own data depth) would be worth building.")


def cmd_weight_scale_check(args):
    """Checks whether the fitted weights summing to != 1.0 is doing real
    work, or is incidental.

    combine_signals' log_combined(x) = log(baseline(x)) + sum_i w_i *
    log(shrunk_i(x)/baseline(x)) rearranges to:
        log_combined(x) = (1 - sum(w)) * log(baseline(x)) + sum_i w_i * log(shrunk_i(x))
    so sum(w) != 1 is NOT incidental - it's an explicit, nonzero coefficient
    on log(baseline(x)) separate from what shrinkage already routes through
    each shrunk_i. A log-linear pool doesn't require weights to sum to 1 to
    stay a valid (normalized) distribution the way an arithmetic mixture
    would, so this isn't a bug by construction - but whether the specific
    total the fit landed on is EARNING its keep, versus being an unforced
    side effect of fitting 6 bounded weights independently, is an empirical
    question, checked here by comparing the fitted weights against the same
    weights rescaled to sum to exactly 1 (same relative proportions, same
    direction, just the total budget normalized) on held-out val/test."""
    state = load_state()
    cfg = Config(sigma=state["sigma"], min_effective_n=state["min_effective_n"],
                 pitch_width=state["pitch_width"], cooldown_radius=state["cooldown_radius"],
                 leverage_width=state["leverage_width"],
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    fitted = state["weights"]
    total = sum(fitted.values())
    rescaled = {k: v / total for k, v in fitted.items()} if total > 0 else dict(fitted)

    print(f"\nFitted weights sum to {total:.3f}. Rescaled (same proportions, sum=1):")
    print(f"  {'signal':28s}{'fitted':>10s}{'rescaled':>10s}")
    for k in SIGNAL_NAMES:
        print(f"  {k:28s}{fitted.get(k, 0):10.3f}{rescaled.get(k, 0):10.3f}")

    for label, names in (("val", state["val_pitchers"]), ("test", state["test_pitchers"])):
        rec = precompute_split(names, cfg, state["points_per_pitcher"], state["min_history"], label)
        ll_fitted = mean_logloss(rec, fitted, cfg.min_effective_n)
        ll_rescaled = mean_logloss(rec, rescaled, cfg.min_effective_n)
        chi2_fitted = pit_chi2_uniform(pit_values(rec, fitted, cfg.min_effective_n))
        chi2_rescaled = pit_chi2_uniform(pit_values(rec, rescaled, cfg.min_effective_n))
        print(f"\n[{label}] n={len(rec)}")
        print(f"  fitted   (sum={total:.3f}): logloss={ll_fitted:.5f}  pit_chi2={chi2_fitted:.1f}")
        print(f"  rescaled (sum=1.000): logloss={ll_rescaled:.5f}  pit_chi2={chi2_rescaled:.1f}")


def cmd_signal_diagnose(args):
    """Diagnoses whether a signal's large fitted weight reflects real
    information or just an optimization-landscape artifact. A signal whose
    (shrunk) density is already close to baseline contributes
    weight * log(shrunk/baseline) ~= 0 everywhere, REGARDLESS of weight - so
    the fit can land on a large value for it with nothing actually backing
    that number, because the objective barely changes as that weight moves.
    Reports, per requested signal:
      1. KL divergence of its (post-shrinkage) density from baseline,
         averaged over real backtest points - how different from "no
         information at all" the signal actually is, BEFORE any weight is
         applied. Small KL = the signal is close to a uniform/baseline
         nudge no matter what weight multiplies it.
      2. A weight-sensitivity sweep on held-out val: mean logloss at a grid
         of candidate weights for this ONE signal, holding the other five at
         their fitted values. Flat across the grid = the fitted weight isn't
         actually validated by anything; a real minimum near the fitted
         value = it is.
    """
    state = load_state()
    for k in ("sigma", "pitch_width", "cooldown_radius", "leverage_width", "min_effective_n", "weights"):
        if k not in state:
            print(f"Missing '{k}' - run fetch/fit-weights first.", file=sys.stderr)
            sys.exit(1)
    cfg = Config(sigma=state["sigma"], min_effective_n=state["min_effective_n"],
                 pitch_width=state["pitch_width"], cooldown_radius=state["cooldown_radius"],
                 leverage_width=state["leverage_width"],
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    fitted_weights = state["weights"]
    val_rec = precompute_split(state["val_pitchers"], cfg, state["points_per_pitcher"],
                                state["min_history"], "val")

    signals_to_check = args.signals.split(",")
    eps = 1e-12

    print(f"\n=== KL divergence from baseline, per signal (shrunk density vs baseline, nats; "
          f"n={len(val_rec)} val points) ===")
    for name in SIGNAL_NAMES:
        kls = []
        for r in val_rec:
            sig = next((s for s in r["signals"] if s.name == name), None)
            if sig is None:
                continue
            alpha = sig.n_eff / (sig.n_eff + cfg.min_effective_n)
            shrunk = alpha * sig.density + (1 - alpha) * r["baseline"]
            kl = float(np.sum(shrunk * np.log((shrunk + eps) / (r["baseline"] + eps))))
            kls.append(kl)
        if kls:
            flag = "  <-- checking" if name in signals_to_check else ""
            print(f"  {name:28s} mean KL={np.mean(kls):.5f}  median={np.median(kls):.5f}  "
                  f"fitted weight={fitted_weights.get(name, 1.0):.3f}{flag}")

    print(f"\n=== Weight sensitivity sweep (val logloss; other 5 weights held at their fitted values) ===")
    grid = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
    for name in signals_to_check:
        print(f"\n  {name} (fitted={fitted_weights.get(name, 1.0):.3f}):")
        for v in grid:
            w = dict(fitted_weights)
            w[name] = v
            ll = mean_logloss(val_rec, w, cfg.min_effective_n)
            print(f"    weight={v:.2f}: val logloss={ll:.5f}")


def cmd_report(args):
    state = load_state()
    for k in ("sigma", "pitch_width", "cooldown_radius", "leverage_width", "min_effective_n", "weights"):
        if k not in state:
            print(f"Missing '{k}' in state - run fetch/fit-weights (and optionally fit-widths) first.",
                  file=sys.stderr)
            sys.exit(1)
    cfg = Config(sigma=state["sigma"], min_effective_n=state["min_effective_n"],
                 pitch_width=state["pitch_width"], cooldown_radius=state["cooldown_radius"],
                 leverage_width=state["leverage_width"],
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    test_rec = precompute_split(state["test_pitchers"], cfg, state["points_per_pitcher"],
                                 state["min_history"], "test")
    print("\n=== FINAL (test, touched once) ===", file=sys.stderr)
    report_split("test", test_rec, state["weights"], cfg.min_effective_n)
    report_distance_split("test", test_rec, state["weights"], cfg.min_effective_n)


def cmd_pitcher_report(args):
    """Evaluates the CURRENT (fixed, global) weights against each pitcher's
    OWN points individually - no fitting happens here, this only asks "given
    the one model we're shipping, how much does it actually help for THIS
    pitcher." Purely descriptive: it answers "which pitchers follow
    patterns these six signals can see" without feeding back into the model
    at all, unlike archetype-scatter (which asked "should different
    pitchers GET different weights" - already answered no).

    Uses the full roster (train+val+test) since this changes nothing about
    fitting - there's no leakage risk in looking at train pitchers here the
    way there would be if this fed back into a decision."""
    state = load_state()
    if "weights" not in state:
        print("Run 'fit-weights' first.", file=sys.stderr)
        sys.exit(1)
    cfg = Config(sigma=state["sigma"], min_effective_n=state["min_effective_n"],
                 pitch_width=state["pitch_width"], cooldown_radius=state["cooldown_radius"],
                 leverage_width=state["leverage_width"],
                 delta_cooldown_radius=state.get("delta_cooldown_radius", 50.0))
    weights = state["weights"]
    split_of = {n: "train" for n in state["train_pitchers"]}
    split_of.update({n: "val" for n in state["val_pitchers"]})
    split_of.update({n: "test" for n in state["test_pitchers"]})
    names = list(split_of.keys())
    builders = [SIGNAL_BUILDERS[n] for n in SIGNAL_NAMES]

    rng = np.random.default_rng(args.seed)
    rows = []
    within_vars = []  # one scalar per pitcher, only when --bootstrap > 0
    t0 = time.time()
    for i, name in enumerate(names):
        df = get_pitcher_df(name)
        if df.empty:
            continue
        point_ids = sample_point_ids(df, args.min_history, args.points_per_pitcher)
        pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
        records = []
        for cur_id in point_ids:
            row_idx = pitches.index[pitches["id"] == cur_id]
            if len(row_idx) == 0:
                continue
            target_row = pitches.loc[int(row_idx[0])]
            df_hist = df[df["id"] < cur_id]
            st, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=target_row)
            if st is None or not signals:
                continue
            records.append({"actual": int(target_row["pitch"]), "baseline": baseline, "signals": signals})
        if len(records) < args.min_points:
            print(f"  [{i + 1}/{len(names)}] skip {name}: only {len(records)} points "
                  f"(< {args.min_points})", file=sys.stderr)
            continue
        base_ll = mean_baseline_logloss(records)
        fit_ll = mean_logloss(records, weights, cfg.min_effective_n)
        margin = base_ll - fit_ll
        row = {
            "pitcher": name, "split": split_of[name], "n_points": len(records),
            "logloss_baseline": base_ll, "logloss_fitted": fit_ll, "margin": margin,
            "pct_more_mass_on_actual": (float(np.exp(margin)) - 1) * 100,
        }
        boot_note = ""
        if args.bootstrap > 0:
            boot_margins = bootstrap_margin_variance(records, weights, cfg.min_effective_n, args.bootstrap, rng)
            pitcher_within_var = float(boot_margins.var(ddof=1))
            within_vars.append(pitcher_within_var)
            row["boot_margin_std"] = pitcher_within_var ** 0.5
            boot_note = f"  [boot std={pitcher_within_var ** 0.5:.4f}]"
        rows.append(row)
        print(f"  [{i + 1}/{len(names)}] {name} ({split_of[name]}): baseline={base_ll:.4f} "
              f"fitted={fit_ll:.4f} margin={margin:+.4f}{boot_note} ({time.time() - t0:.0f}s elapsed)", file=sys.stderr)

    out_df = pd.DataFrame(rows).sort_values("margin", ascending=False).reset_index(drop=True)
    out_path = ROOT / "tools" / "probe_output" / "per_pitcher_predictability.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path} ({len(out_df)} pitchers, {args.points_per_pitcher} points each - "
          f"individual margins are noisy at this n, read the ranking's shape, not exact row order)",
          file=sys.stderr)

    print(f"\nMargin (baseline logloss - fitted logloss; higher = model helps more) across "
          f"{len(out_df)} pitchers:")
    print(out_df["margin"].describe().to_string())

    n_pos = int((out_df["margin"] > 0).sum())
    print(f"\n{n_pos}/{len(out_df)} pitchers ({n_pos / len(out_df) * 100:.0f}%) improved by the fitted "
          f"model (margin > 0).")

    if args.bootstrap > 0 and within_vars:
        # Same random-effects decomposition as the archetype-scatter check,
        # applied to this one scalar (margin) instead of the 6-dim weight
        # vector: Var(observed margin across pitchers) = Var(true per-
        # pitcher predictability) + E[Var(sampling noise | pitcher)]. The
        # bootstrap just measured the second term directly per pitcher;
        # subtracting its mean off the observed cross-pitcher variance
        # isolates whether "some pitchers are more predictable" is real or
        # just which 50 points each one happened to get sampled.
        mean_within = float(np.mean(within_vars))
        between_observed = float(out_df["margin"].var(ddof=1))
        implied_true_between = max(0.0, between_observed - mean_within)
        ratio = implied_true_between / mean_within if mean_within > 1e-12 else float("inf")
        print(f"\nVariance decomposition of the margin (within-pitcher bootstrap, "
              f"b={args.bootstrap} resamples/pitcher):")
        print(f"  between (observed, across {len(out_df)} pitchers): {between_observed:.6f}")
        print(f"  within (mean sampling-noise floor):                {mean_within:.6f}")
        print(f"  implied true between-pitcher variance:              {implied_true_between:.6f}")
        print(f"  ratio (true / noise):                               {ratio:.2f}")
        print("  ratio << 1: the predictability spread is explained by sampling noise alone - this\n"
              "    ranking isn't trustworthy pitcher-by-pitcher, even directionally.\n"
              "  ratio >> 1: real variation in how predictable pitchers are, beyond noise - the\n"
              "    ranking (at least its shape/extremes) reflects something true.")

    cols = ["pitcher", "split", "n_points", "logloss_baseline", "logloss_fitted", "margin"]
    print(f"\nModel helps MOST (top 10 by margin):")
    print(out_df.head(10)[cols].to_string(index=False))
    print(f"\nModel helps LEAST / hurts (bottom 10 by margin):")
    print(out_df.tail(10)[cols].to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch")
    p.add_argument("--min-bf", type=int, default=100,
                    help="Minimum batters faced (= play_type=='Swing' rows), scanned live from Supabase")
    p.add_argument("--train-frac", type=float, default=0.5)
    p.add_argument("--val-frac", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("fit-weights")
    p.add_argument("--points-per-pitcher", type=int, default=25)
    p.add_argument("--min-history", type=int, default=30)
    p.add_argument("--sigma", type=float, default=100.0)
    p.add_argument("--pitch-width", type=int, default=200)
    p.add_argument("--cooldown-radius", type=float, default=50.0)
    p.add_argument("--delta-cooldown-radius", type=float, default=50.0)
    p.add_argument("--leverage-width", type=float, default=1.0)
    p.add_argument("--min-effective-n", type=float, default=20.0)
    p.set_defaults(func=cmd_fit_weights)

    p = sub.add_parser("fit-widths")
    p.add_argument("--sigma-grid", default="60,100,150")
    p.add_argument("--pitch-width-grid", default="150,200,300")
    p.add_argument("--leverage-width-grid", default="0.5,1.0,1.5")
    p.add_argument("--min-eff-grid", default="10,20,30")
    p.set_defaults(func=cmd_fit_widths)

    p = sub.add_parser("archetype-scatter")
    p.add_argument("--points-per-pitcher", type=int, default=30)
    p.add_argument("--min-history", type=int, default=30)
    p.add_argument("--min-points", type=int, default=20)
    p.add_argument("--bootstrap", type=int, default=0,
                    help="Resamples per pitcher for the within-pitcher noise-floor estimate "
                         "(0 = skip; the variance-decomposition report only runs when > 0)")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_archetype_scatter)

    p = sub.add_parser("pitcher-report")
    p.add_argument("--points-per-pitcher", type=int, default=50)
    p.add_argument("--min-history", type=int, default=30)
    p.add_argument("--min-points", type=int, default=20)
    p.add_argument("--bootstrap", type=int, default=0,
                    help="Resamples per pitcher for the margin noise-floor estimate "
                         "(0 = skip; the variance-decomposition report only runs when > 0)")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_pitcher_report)

    p = sub.add_parser("weight-scale-check")
    p.set_defaults(func=cmd_weight_scale_check)

    p = sub.add_parser("signal-diagnose")
    p.add_argument("--signals", default="cooldown",
                    help="Comma-separated signal name(s) to run the weight-sensitivity sweep on")
    p.set_defaults(func=cmd_signal_diagnose)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

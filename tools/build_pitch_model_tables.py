"""Build pitch_model_tables.json - the league-level tables the pitch-likelihood model ships with.

  cooldown_hazard / delta_cooldown_hazard : {category: {lag: P(a still-open trigger's next pitch lands inside the zone)}}
        pooled over pitchers, radius 50, lags 1..MAX_LAG. This is the CONDITIONAL return rate (a hazard: of the triggers
        still open at that lag, how many return on it), not the unconditional "resolves in exactly k" mass the earlier
        tables held - see the cooldown notes in pitch_likelihood_backtest.py.
  league_counts : how often each pitch value 1..1000 was thrown league-wide (the fine-structure prior: round numbers,
        repdigits...).
  spike_rates   : league rate at which a pitch equals the previous pitch / previous swing / pitch before last / the previous PA's
        diff / the previous pitch + 500 (same game; prev_pitch2 is kept for reference, the deployed model no longer spikes on it).

Usage:  python -I tools/build_pitch_model_tables.py [--out pitch_model_tables.json]
The evaluation harness calls build_tables(pitcher_ids=train_ids) so held-out pitchers never feed their own tables.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import utils  # noqa: E402

CACHE = ROOT / "tools/diff_lab/cache/plays.pkl"
RADIUS = 50
MAX_LAG = 10
MIN_PITCHES = 150
CATS = list(utils.SEQ_RESULT_CATEGORIES)


def _load() -> pd.DataFrame:
    d = pd.read_pickle(CACHE)
    d = d[(d.play_type == "Swing") & d.pitch.notna() & d.game_id.notna() & (d.game_type == "live")]
    return d.sort_values(["pitcher_id", "id"])


def _signed_delta(prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    d = (cur - prev) % 1000
    return np.where(d > 500, d - 1000, d)


def _hazard_counts(vals: np.ndarray, cats: list, radius: int, at_risk: dict, hits: dict) -> None:
    """Accumulate, for each category and lag 1..MAX_LAG, (#triggers at risk, #returning exactly then)."""
    n = len(vals)
    for i in range(n - 1):
        c = cats[i]
        if c is None or c not in at_risk:
            continue
        later = vals[i + 1:i + 1 + MAX_LAG]
        dist = np.abs(later - vals[i])
        dist = np.where(dist > 500, 1000 - dist, dist)
        within = np.where(dist <= radius)[0]
        first = within[0] + 1 if len(within) else None
        top = len(later) if first is None else first
        at_risk[c][1:top + 1] += 1
        if first is not None:
            hits[c][first] += 1


def build_tables(pitcher_ids=None) -> dict:
    d = _load()
    if pitcher_ids is not None:
        d = d[d.pitcher_id.isin(set(pitcher_ids))]
    counts = np.zeros(1000)
    ar = {k: {c: np.zeros(MAX_LAG + 1) for c in CATS} for k in ("pitch", "delta")}
    hi = {k: {c: np.zeros(MAX_LAG + 1) for c in CATS} for k in ("pitch", "delta")}
    spike_hits = np.zeros(5)
    spike_n = np.zeros(5)
    for pid, s in d.groupby("pitcher_id"):
        v = s.pitch.astype(int).to_numpy()
        counts += np.bincount((v - 1) % 1000, minlength=1000)
        if len(s) < MIN_PITCHES:
            continue
        cats_all = [utils.seq_result_category(r) if isinstance(r, str) else None for r in s.result]
        _hazard_counts(v, cats_all, RADIUS, ar["pitch"], hi["pitch"])
        # delta domain: delta INTO each pitch within its game; rows with no delta (a game's first pitch) are dropped,
        # exactly as cooldown_events drops rows with no value
        g = s.game_id.to_numpy()
        same = np.r_[False, g[1:] == g[:-1]]
        delta = np.where(same, _signed_delta(np.r_[v[0], v[:-1]], v), np.nan)
        keep = ~np.isnan(delta)
        _hazard_counts(delta[keep].astype(int), [c for c, k in zip(cats_all, keep) if k], RADIUS,
                       ar["delta"], hi["delta"])
        sw = s.swing.to_numpy(dtype=float)
        for j, (ref, lagk) in enumerate((("pitch", 1), ("swing", 1), ("pitch", 2))):
            idx = np.arange(lagk, len(v))
            ok = g[idx] == g[idx - lagk]
            refv = (v if ref == "pitch" else sw)[idx - lagk]
            valid = ok & ~np.isnan(refv)
            spike_n[j] += valid.sum()
            spike_hits[j] += (v[idx][valid] == refv[valid]).sum()
        # previous PA's diff (circular |pitch - swing|, 0 is the wheel spot 1000) and the previous pitch + 500; pitches / swings
        # outside 1-1000 (autos) leave a reference undefined
        v_f = v.astype(float)
        ok_v = (v >= 1) & (v <= 1000)
        raw_d = np.abs(v_f - sw) % 1000
        d_all = np.where(ok_v & (sw >= 1) & (sw <= 1000), np.minimum(raw_d, 1000 - raw_d), np.nan)
        diff_v = np.where(d_all == 0, 1000.0, d_all)
        p500_v = np.where(ok_v, (v_f + 499) % 1000 + 1, np.nan)
        idx1 = np.arange(1, len(v))
        for j, refv_full in ((3, diff_v), (4, p500_v)):
            refv = refv_full[idx1 - 1]
            valid = (g[idx1] == g[idx1 - 1]) & ~np.isnan(refv)
            spike_n[j] += valid.sum()
            spike_hits[j] += (v_f[idx1][valid] == refv[valid]).sum()
    out = {"radius": RADIUS, "max_lag": MAX_LAG, "league_counts": counts.astype(int).tolist(),
           "spike_rates": {k: float(h / n) if n else 0.0 for k, h, n in
                           zip(("prev_pitch", "prev_swing", "prev_pitch2", "prev_diff", "prev_pitch_p500"), spike_hits, spike_n)}}
    for dom, key in (("pitch", "cooldown_hazard"), ("delta", "delta_cooldown_hazard")):
        out[key] = {c: {str(k): (float(hi[dom][c][k] / ar[dom][c][k]) if ar[dom][c][k] else None)
                        for k in range(1, MAX_LAG + 1)} for c in CATS}
        out[key + "_at_risk"] = {c: {str(k): int(ar[dom][c][k]) for k in range(1, MAX_LAG + 1)} for c in CATS}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "pitch_model_tables.json"))
    a = ap.parse_args()
    t = build_tables()
    Path(a.out).write_text(json.dumps(t))
    chance = (2 * RADIUS + 1) / 1000
    print(f"wrote {a.out}; chance zone mass {chance:.3f}")
    for key in ("cooldown_hazard", "delta_cooldown_hazard"):
        print(key)
        for c in CATS:
            print(f"  {c:>6}", " ".join(f"{t[key][c][str(k)]:.3f}" for k in range(1, 9)))
    print("spike rates", {k: round(v, 4) for k, v in t["spike_rates"].items()}, "league pitches", sum(t["league_counts"]))


if __name__ == "__main__":
    main()

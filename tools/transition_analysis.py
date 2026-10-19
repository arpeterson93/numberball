"""Pitch-transition scouting analysis.

Two questions, both about how a pitcher's pitch relates to a reference value from just before it:

  1. RELIEF ENTRY: a reliever's first pitch vs the replaced teammate's last pitch / the swing on that pitch.
     Split into (a) entered mid-inning and (b) began an inning.
  2. INNING START: any pitcher's first pitch of a half inning vs the last pitch / swing of the previous half
     inning (the one where the pitcher's own team was batting).

In this data one `Swing` row is a whole plate appearance (one pitch, one swing), so "last pitch" and "last swing"
of a plate appearance come from the same row.

Relations measured for each (pitch, reference) pair, on the 1-1000 circle:
  exact match, within +/-k bands, signed difference distribution, and the opposite-side point (ref + 500).
The null is NOT a flat 1/1000: pitches and references are both non-uniform, so the expected hit rate for every
band is taken from cross-pairing each pitch with every reference in the same event class (league density).

Run:  python -I tools/transition_analysis.py          (reads tools/diff_lab/cache/plays.pkl)
Outputs CSVs to tools/probe_output/transitions/.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tools/diff_lab/cache/plays.pkl"
OUT = ROOT / "tools/probe_output/transitions"

BANDS = (0, 5, 10, 25, 50)  # +/- k on the circle; 0 = exact
REFS = ("def_pitch", "def_swing", "opp_pitch", "opp_swing")  # which reference columns each event class has


def wrap(d):
    """Signed circular difference into (-500, 500] on the 1-1000 wheel."""
    d = np.asarray(d, dtype=float) % 1000.0
    return np.where(d > 500, d - 1000.0, d)


def build_events(plays: pd.DataFrame, live_only: bool = True) -> pd.DataFrame:
    """One row per Swing row, annotated with previous-row references and event flags."""
    d = plays[(plays.play_type == "Swing") & plays.game_id.notna() & plays.pitch.notna() & plays.swing.notna()].copy()
    if live_only:
        d = d[d.game_type == "live"]
    d = d.sort_values(["game_id", "play_num"]).reset_index(drop=True)
    g = d.groupby("game_id", sort=False)

    d["hkey"] = d.inning.astype(str) + d.half
    d["prev_hkey"] = g.hkey.shift(1)
    d["half_start"] = d.prev_hkey.isna() | (d.prev_hkey != d.hkey)

    # last row of the previous half inning = immediately preceding row (the pitching team was batting in it)
    d["opp_pitch"] = g.pitch.shift(1).where(d.half_start)
    d["opp_swing"] = g.swing.shift(1).where(d.half_start)

    # previous row by the same defensive team (same game)
    gd = d.groupby(["game_id", "def_team"], sort=False)
    d["prev_pitcher"] = gd.pitcher_id.shift(1)
    d["def_pitch"] = gd.pitch.shift(1)
    d["def_swing"] = gd.swing.shift(1)
    d["relief_entry"] = d.prev_pitcher.notna() & (d.prev_pitcher != d.pitcher_id)
    # first row of this pitcher's appearance, also for starters
    d["app_start"] = d.prev_pitcher.isna() | d.relief_entry
    return d


def event_classes(ev: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Event class -> rows. Each class lists which reference columns apply."""
    relief = ev[ev.relief_entry]
    return {
        "relief_mid_inning": relief[~relief.half_start],
        "relief_inning_start": relief[relief.half_start],
        "inning_start_all": ev[ev.half_start & ev.opp_pitch.notna()],
        "inning_start_starter_only": ev[ev.half_start & ev.opp_pitch.notna() & ~ev.relief_entry],
    }


def class_refs(name: str) -> tuple[str, ...]:
    return ("def_pitch", "def_swing") if name == "relief_mid_inning" else \
           ("def_pitch", "def_swing", "opp_pitch", "opp_swing") if name == "relief_inning_start" else \
           ("opp_pitch", "opp_swing")


def _band_prob_table(refs: np.ndarray, k: int) -> np.ndarray:
    """P(ref within +/-k of x) for x in 1..1000 under the empirical league ref density (circular)."""
    dens = np.bincount(refs.astype(int) % 1000, minlength=1000).astype(float)  # value 1000 -> bin 0
    dens /= dens.sum()
    if k == 0:
        return dens[np.arange(1, 1001) % 1000]
    ext = np.concatenate([dens[-k:], dens, dens[:k]])
    cs = np.concatenate([[0.0], np.cumsum(ext)])
    x = np.arange(1000)  # bin index of value x+1 -> hmm bins are value%1000, value 1 -> bin 1
    out = np.empty(1000)
    for v in range(1, 1001):
        b = v % 1000
        out[v - 1] = cs[b + 2 * k + 1] - cs[b]
    return out


def relation_stats(pitch: np.ndarray, ref: np.ndarray, base_refs: np.ndarray) -> dict:
    """Observed vs expected hit counts per band plus circular-offset (Rayleigh) test."""
    pitch = np.asarray(pitch, float)
    ref = np.asarray(ref, float)
    n = len(pitch)
    res = {"n": n}
    sd = np.abs(wrap(pitch - ref))
    for k in BANDS:
        obs = int((sd <= k).sum())
        p = _band_prob_table(base_refs, k)[pitch.astype(int) - 1]
        exp, var = p.sum(), (p * (1 - p)).sum()
        res[f"obs_{k}"] = obs
        res[f"exp_{k}"] = exp
        res[f"z_{k}"] = (obs - exp) / np.sqrt(var) if var > 0 else np.nan
    # opposite side of the wheel (ref + 500), +/-10
    opp = np.abs(wrap(pitch - ref - 500))
    p = _band_prob_table((base_refs + 500 - 1) % 1000 + 1, 10)[pitch.astype(int) - 1]
    res["obs_opp10"], res["exp_opp10"] = int((opp <= 10).sum()), p.sum()
    var = (p * (1 - p)).sum()
    res["z_opp10"] = (res["obs_opp10"] - p.sum()) / np.sqrt(var) if var > 0 else np.nan
    # constant offset: Rayleigh test on signed difference angle (valid vs uniform differences)
    ang = 2 * np.pi * wrap(pitch - ref) / 1000.0
    C, S = np.cos(ang).mean(), np.sin(ang).mean()
    R = np.hypot(C, S)
    res["mean_offset"] = np.degrees(np.arctan2(S, C)) / 360 * 1000 if n else np.nan
    res["rayleigh_R"] = R
    z = n * R * R
    res["rayleigh_p"] = float(np.exp(-z) * (1 + (2 * z - z * z) / (4 * n))) if n > 1 else np.nan
    res["median_abs"] = float(np.median(sd)) if n else np.nan
    return res


def league_report(classes: dict[str, pd.DataFrame], base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, df in classes.items():
        for ref in class_refs(name):
            sub = df[df[ref].notna()]
            if sub.empty:
                continue
            # baseline density: every ref value of that column in the event class
            r = relation_stats(sub.pitch.values, sub[ref].values, sub[ref].values)
            r.update(event=name, ref=ref)
            rows.append(r)
    return pd.DataFrame(rows).set_index(["event", "ref"])


def control_report(ev: pd.DataFrame) -> pd.DataFrame:
    """Same stats for ordinary mid-inning pitches vs the previous row's swing/pitch (same pitcher, same half)."""
    c = ev[~ev.half_start & ~ev.relief_entry].copy()
    g = ev.groupby(["game_id"], sort=False)
    prev_pitch = g.pitch.shift(1).loc[c.index]
    prev_swing = g.swing.shift(1).loc[c.index]
    rows = []
    for nm, col in (("prev_pitch", prev_pitch), ("prev_swing", prev_swing)):
        m = col.notna()
        r = relation_stats(c.pitch[m].values, col[m].values, col[m].values)
        r.update(event="control_mid_inning", ref=nm)
        rows.append(r)
    return pd.DataFrame(rows).set_index(["event", "ref"])


def pitcher_report(classes: dict[str, pd.DataFrame], min_n: int = 15) -> pd.DataFrame:
    rows = []
    for name, df in classes.items():
        for ref in class_refs(name):
            sub_all = df[df[ref].notna()]
            base = sub_all[ref].values
            for pid, sub in sub_all.groupby("pitcher_id"):
                if len(sub) < min_n:
                    continue
                r = relation_stats(sub.pitch.values, sub[ref].values, base)
                r.update(event=name, ref=ref, pitcher_id=pid, pitcher=sub.pitcher_name.iloc[-1])
                rows.append(r)
    out = pd.DataFrame(rows)
    # Poisson upper-tail p (one-sided, "more hits than chance") - the normal approximation is unusable for rare exact hits
    from scipy.stats import poisson
    for k in (0, 5, 25):
        out[f"p_{k}"] = poisson.sf(out[f"obs_{k}"] - 1, out[f"exp_{k}"].clip(lower=1e-9))
    for pcol, qcol in (("p_0", "q_0"), ("p_5", "q_5"), ("p_25", "q_25"), ("rayleigh_p", "q_ray")):
        out[qcol] = _bh(out[pcol].values)
    return out


def _bh(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, float)
    q = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    pv = p[ok]
    order = np.argsort(pv)
    ranked = pv[order] * len(pv) / (np.arange(len(pv)) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    tmp = np.empty_like(pv)
    tmp[order] = np.minimum(ranked, 1.0)
    q[ok] = tmp
    return q


def diff_histogram(classes: dict[str, pd.DataFrame], bins: int = 20) -> pd.DataFrame:
    """Signed pitch-minus-ref difference histogram (counts per 1000/bins-wide bucket), per class and ref."""
    edges = np.linspace(-500, 500, bins + 1)
    rows = {}
    for name, df in classes.items():
        for ref in class_refs(name):
            sub = df[df[ref].notna()]
            h, _ = np.histogram(wrap(sub.pitch.values - sub[ref].values), bins=edges)
            rows[(name, ref)] = h
    cols = [f"{int(a)}..{int(b)}" for a, b in zip(edges[:-1], edges[1:])]
    return pd.DataFrame(rows, index=cols).T


def main():
    plays = pd.read_pickle(CACHE)
    ev = build_events(plays)
    classes = event_classes(ev)
    OUT.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 250, "display.max_columns", 40, "display.float_format", lambda x: f"{x:.3f}")

    print("Event counts:", {k: len(v) for k, v in classes.items()})
    lg = league_report(classes, ev)
    ctl = control_report(ev)
    both = pd.concat([lg, ctl])
    cols = ["n", "obs_0", "exp_0", "z_0", "obs_10", "exp_10", "z_10", "obs_25", "exp_25", "z_25", "z_50", "z_opp10",
            "mean_offset", "rayleigh_R", "rayleigh_p", "median_abs"]
    print("\nLEAGUE-WIDE (obs vs expected-from-density; z>2 is notable)")
    print(both[cols].to_string())
    both.to_csv(OUT / "league_summary.csv")
    diff_histogram(classes).to_csv(OUT / "diff_histograms.csv")

    pr = pitcher_report(classes)
    pr.to_csv(OUT / "pitcher_summary.csv", index=False)
    show = ["event", "ref", "pitcher", "n", "obs_25", "exp_25", "p_25", "q_25", "obs_0", "p_0", "q_0", "rayleigh_p", "q_ray"]
    print(f"\nPITCHER-LEVEL tests run: {len(pr)}")
    print("\nTop by z_25 (within +/-25):")
    print(pr.sort_values("z_25", ascending=False)[show].head(15).to_string(index=False))
    print("\nTop by exact-match z_0:")
    print(pr.sort_values("p_0")[["event", "ref", "pitcher", "n", "obs_0", "exp_0", "p_0", "q_0"]].head(10).to_string(index=False))
    print("\nTop by Rayleigh (consistent offset):")
    print(pr.sort_values("rayleigh_p")[show].head(10).to_string(index=False))
    print("\nBH-significant (q<0.10) any headline stat:")
    sig = pr[(pr.q_25 < 0.10) | (pr.q_0 < 0.10) | (pr.q_ray < 0.10)]
    print(sig[show].to_string(index=False) if len(sig) else "  none")


if __name__ == "__main__":
    sys.exit(main())

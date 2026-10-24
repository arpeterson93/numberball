"""Do pitchers go NEAR or FAR from their own recent pitches, after a result group or in a leverage group?

Model-free screen on the full play-by-play (needs tools/probe_output/all_plays.pkl, see pitch_prev_pa_follow.py fetch).
For each pitch t of a pitcher (valid 1-1000 pitches only, in id order; pitch 0 autos are dropped) the "recent window" is his
previous N pitches (across games). Two statistics, both against a null that keeps the pitcher's own pitch habits:

  #1 attraction lift   hits = window pitches within +/- r of the pitch he actually threw. Expected hits = sum over window pitches
                       of P(his pitch lands within r of it), P from his own whole-career pitch distribution (exactly the mean of a
                       within-pitcher shuffle of the thrown pitch). lift = hits / expected: >1 toward the recent cluster, <1 away.
  #4 nearest distance  distance from the thrown pitch to the NEAREST window pitch. Null = the same distance for K pitches drawn
                       from his own distribution. Reported as mean distance / null mean distance, and the mean PIT (probability
                       integral transform: share of null draws closer than the observed, mid-rank). PIT < 0.5 = closer to
                       something recent than chance, > 0.5 = in a gap.

Because the null uses his whole-career distribution, favourite numbers and round-number habits cancel out; what is left is
recency (including slow drift over seasons). The OLD-window columns repeat the same test with the window shifted back N pitches
(pitches t-2N .. t-N-1): if that is also ~1.0 / 0.5 the recent effect is the genuine short-memory one.

Groups (rows): ALL; previous-PA result (XBH, BB-1B, Out, K+ from utils.seq_result_category, previous pitch of the SAME game only);
leverage of the pitch itself (Low <0.5, Med 0.5-1.0, High 1.0+, the deployed LEVERAGE_BUCKET_EDGES). Result and leverage are separate
indicators, never crossed. Contrast rows give the bootstrap difference between two groups. Confidence intervals resample PITCHERS.
A '*' marks a CI that excludes the null (1.0 ratio / 0.5 PIT).

Usage:
  python -I tools/pitch_recent_cluster_screen.py
  python -I tools/pitch_recent_cluster_screen.py --window 20 30 --radius 25 50 --min-pitches 300
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import warnings

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKL = ROOT / "tools" / "probe_output" / "all_plays.pkl"
LEV_PKL = ROOT / "tools" / "probe_output" / "lev_cache.pkl"

RES_GROUPS = ["XBH", "BB-1B", "Out", "K+"]
LEV_GROUPS = ["Low", "Med", "High"]
GROUPS = [("ALL", "ALL")] + [("result", g) for g in RES_GROUPS] + [("leverage", g) for g in LEV_GROUPS]
CONTRASTS = [("result", "XBH", "K+"), ("result", "XBH", "Out"), ("result", "BB-1B", "Out"), ("leverage", "High", "Low")]


def circ(a, b):
    d = np.abs(a - b) % 1000
    return np.minimum(d, 1000 - d)


def leverage_series(df: pd.DataFrame) -> pd.Series:
    """Pre-play leverage index per play id (cached: computing it row by row over ~77k plays takes a minute)."""
    if LEV_PKL.exists():
        cached = pd.read_pickle(LEV_PKL)
        if set(df["id"]) <= set(cached.index):
            return cached.loc[df["id"]]
    import utils
    lev = pd.Series(utils.compute_play_leverage(df).to_numpy(), index=df["id"].to_numpy())
    lev.to_pickle(LEV_PKL)
    return lev


def load(min_pitches: int) -> list[dict]:
    if not PKL.exists():
        sys.exit(f"{PKL} not found - run `python -I tools/pitch_prev_pa_follow.py fetch` first")
    import utils
    df = pd.read_pickle(PKL)
    df = df[df["play_type"] == "Swing"].sort_values(["pitcher_id", "id"]).copy()
    df["res_cat"] = df["result"].map(lambda r: utils.seq_result_category(str(r)))
    gb = df.groupby("pitcher_id")
    prev_res, prev_game = gb["res_cat"].shift(1), gb["game_id"].shift(1)
    df["prev_res"] = prev_res.where(prev_game == df["game_id"], "")
    df["prev_res"] = df["prev_res"].fillna("")
    lev = leverage_series(df).to_numpy(float)
    df["lev_grp"] = np.where(np.isnan(lev), "", np.where(lev < 0.5, "Low", np.where(lev < 1.0, "Med", "High")))
    df = df[df["pitch"].between(1, 1000)]
    out = []
    for pid, g in df.groupby("pitcher_id"):
        if len(g) >= min_pitches:
            out.append({"p": g["pitch"].to_numpy(float), "res": g["prev_res"].to_numpy(), "lev": g["lev_grp"].to_numpy()})
    return out


def pitcher_block(p, N: int, radii: list[int], K: int, rng, null: str = "habit", hl: float = 0.0) -> np.ndarray:
    """(m, C) per-pitch statistics for targets t >= 2N. Columns: per radius [obs, exp, old_obs, old_exp], then
    [nn, nn_null, pit, old_nn, old_nn_null, old_pit]."""
    n = len(p)
    win = sliding_window_view(p, N)
    rec, old, tgt = win[N:n - N], win[:n - 2 * N], p[2 * N:]
    m = len(tgt)
    cnt = np.bincount(p.astype(int) % 1000, minlength=1000).astype(float)
    cs = np.concatenate([[0.0], np.cumsum(np.concatenate([cnt, cnt, cnt]))])
    # the null is leave-out: the window's own pitches and the target are removed from his distribution, otherwise every
    # window pitch is "expected" to be near itself and the ratio sits a few % below 1 even with no recency (the OLD column shows it)
    ex_len = {"rec": N + 1, "old": N}
    # recency weights over the window columns (last column = most recent pitch): half-life hl pitches, flat when hl = 0
    wt = 0.5 ** ((N - 1 - np.arange(N)) / hl) if hl else np.ones(N)
    cols = []
    for r in radii:
        w = np.arange(1000) + 1000
        near = cs[w + r + 1] - cs[w - r]   # his pitches within r of wheel spot w
        for key, W in (("rec", rec), ("old", old)):
            ind = (circ(W, tgt[:, None]) <= r).astype(float)   # (m, N) window pitch k is within r of the thrown pitch
            cols.append(ind @ wt)
            if null == "uniform":
                cols.append(np.full(m, wt.sum() * (2 * r + 1) / 1000))
                continue
            pairs = np.empty((m, N))
            for a in range(0, m, 400):
                pairs[a:a + 400] = (circ(W[a:a + 400, :, None], W[a:a + 400, None, :]) <= r).sum(2)
            cols.append(((near[W.astype(int) % 1000] - pairs - ind) / (n - ex_len[key] - 1)) @ wt)
    ar = np.arange(m)
    for key, W, start in (("rec", rec, N + ar), ("old", old, ar)):
        L = ex_len[key]
        u = rng.integers(0, n - L, size=(m, K))
        draws = p[u + (u >= start[:, None]) * L]   # his pitches from other times, skipping the excluded block
        if null == "uniform":
            draws = rng.integers(1, 1001, size=(m, K)).astype(float)
        nn = circ(W, tgt[:, None]).min(1)
        null_mean = np.empty(m)
        pit = np.empty(m)
        for a in range(0, m, 400):
            sl = slice(a, a + 400)
            md = circ(W[sl, None, :], draws[sl, :, None]).min(2)
            null_mean[sl] = md.mean(1)
            pit[sl] = ((md < nn[sl, None]).sum(1) + 0.5 * (md == nn[sl, None]).sum(1)) / K
        cols += [nn.astype(float), null_mean, pit]
    return np.column_stack(cols)


def build_tensor(pitchers: list[dict], N: int, radii: list[int], K: int, seed: int, null: str = "habit", hl: float = 0.0) -> np.ndarray:
    """(P, G, C+1): per pitcher and group, column sums of the per-pitch statistics plus the pitch count last."""
    rng = np.random.default_rng(seed)
    T = []
    for d in pitchers:
        if len(d["p"]) < 2 * N + 50:
            continue
        M = pitcher_block(d["p"], N, radii, K, rng, null, hl)
        res, lev = d["res"][2 * N:], d["lev"][2 * N:]
        rows = []
        for kind, g in GROUPS:
            mask = np.ones(len(M), bool) if kind == "ALL" else ((res == g) if kind == "result" else (lev == g))
            rows.append(np.append(M[mask].sum(0), mask.sum()))
        T.append(rows)
    return np.array(T)


def stats(S: np.ndarray, nr: int) -> dict[str, np.ndarray]:
    """S (..., G, C+1) summed over pitchers -> ratios. Keys: lift{i}, old_lift{i} per radius; nn_ratio, old_nn_ratio, pit, old_pit."""
    out = {}
    for i in range(nr):
        b = 4 * i
        out[f"lift{i}"] = S[..., b] / S[..., b + 1]
        out[f"old_lift{i}"] = S[..., b + 2] / S[..., b + 3]
    b, n = 4 * nr, S[..., -1]
    out["nn_ratio"], out["pit"] = S[..., b] / S[..., b + 1], S[..., b + 2] / n
    out["old_nn_ratio"], out["old_pit"] = S[..., b + 3] / S[..., b + 4], S[..., b + 5] / n
    return out


def fmt(v, lo, hi, null, nd=3):
    star = "*" if (lo > null or hi < null) else " "
    return f"{v:.{nd}f} [{lo:.{nd}f},{hi:.{nd}f}]{star}"


def report(T: np.ndarray, N: int, radii: list[int], boots: int, seed: int, label: str = "") -> None:
    rng = np.random.default_rng(seed + 1)
    P = len(T)
    obs = stats(T.sum(0), len(radii))
    idx = rng.integers(0, P, size=(boots, P))
    bs = stats(np.stack([T[i].sum(0) for i in idx]), len(radii))
    gi = {g: i for i, g in enumerate(GROUPS)}
    n = T.sum(0)[:, -1].astype(int)

    def ci(key, i, j=None):
        arr = bs[key][:, i] if j is None else bs[key][:, i] - bs[key][:, j]
        return np.percentile(arr, [2.5, 97.5])

    print(f"\n=== window N={N}{label}: {P} pitchers, {n[0]} target pitches ===")
    print("\n#1 attraction lift (window hits / expected from his own habits; >1 = toward recent pitches)")
    print(f"{'group':14s} {'n':>6s} " + " ".join(f"{'r=' + str(r) + ' recent':>22s} {'old':>7s}" for r in radii))
    for (kind, g), i in gi.items():
        row = f"{(g if kind == 'ALL' else kind[:3] + ':' + g):14s} {n[i]:6d} "
        for k in range(len(radii)):
            lo, hi = ci(f"lift{k}", i)
            row += f"{fmt(obs[f'lift{k}'][i], lo, hi, 1.0):>22s} {obs[f'old_lift{k}'][i]:7.3f} "
        print(row)
    for kind, a, b in CONTRASTS:
        i, j = gi[(kind, a)], gi[(kind, b)]
        row = f"{'  ' + a + ' - ' + b:14s} {'':6s} "
        for k in range(len(radii)):
            lo, hi = ci(f"lift{k}", i, j)
            row += f"{fmt(obs[f'lift{k}'][i] - obs[f'lift{k}'][j], lo, hi, 0.0):>22s} {'':7s} "
        print(row)

    print("\n#4 nearest recent pitch (distance ratio vs his own habits, and mean PIT; <1 / <0.5 = closer to something recent)")
    print(f"{'group':14s} {'n':>6s} {'dist ratio':>22s} {'old':>7s} {'mean PIT':>22s} {'old':>7s}")
    for (kind, g), i in gi.items():
        l1, h1 = ci("nn_ratio", i)
        l2, h2 = ci("pit", i)
        print(f"{(g if kind == 'ALL' else kind[:3] + ':' + g):14s} {n[i]:6d} {fmt(obs['nn_ratio'][i], l1, h1, 1.0):>22s} "
              f"{obs['old_nn_ratio'][i]:7.3f} {fmt(obs['pit'][i], l2, h2, 0.5):>22s} {obs['old_pit'][i]:7.3f}")
    for kind, a, b in CONTRASTS:
        i, j = gi[(kind, a)], gi[(kind, b)]
        l1, h1 = ci("nn_ratio", i, j)
        l2, h2 = ci("pit", i, j)
        print(f"{'  ' + a + ' - ' + b:14s} {'':6s} {fmt(obs['nn_ratio'][i] - obs['nn_ratio'][j], l1, h1, 0.0):>22s} {'':7s} "
              f"{fmt(obs['pit'][i] - obs['pit'][j], l2, h2, 0.0):>22s}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window", type=int, nargs="+", default=[25, 50], help="number of previous pitches in the recent window")
    ap.add_argument("--null", choices=["habit", "uniform"], default="habit",
                    help="habit = his own leave-out pitch distribution (default); uniform = every wheel spot equally likely (habits NOT controlled; read recent vs OLD)")
    ap.add_argument("--half-life", type=float, nargs="+", default=[0], help="recency weight half-lives in pitches for #1 (0 = flat count); use with a long --window")
    ap.add_argument("--radius", type=int, nargs="+", default=[25, 50, 100])
    ap.add_argument("--min-pitches", type=int, default=200)
    ap.add_argument("--null-draws", type=int, default=64, help="K draws from his own distribution per pitch for the nearest-distance null")
    ap.add_argument("--boots", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    pitchers = load(a.min_pitches)
    print(f"{len(pitchers)} pitchers with >= {a.min_pitches} valid pitches")
    for hl in a.half_life:
        for N in a.window:
            T = build_tensor(pitchers, N, a.radius, a.null_draws, a.seed, a.null, float(hl))
            report(T, N, a.radius, a.boots, a.seed, f", null={a.null}, recency half-life={hl or 'off'}")


if __name__ == "__main__":
    main()

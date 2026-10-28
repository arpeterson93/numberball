"""Does a pitcher's next pitch follow the previous plate appearance's pitch / swing / diff - or a simple transform of them?

Direct, model-free test on the full play-by-play. For each pitcher (>= --min-pairs consecutive valid swing PAs, in id order) and
each candidate, a "hit" is the next pitch landing within --radius of the candidate value built from the PREVIOUS PA's
  p = pitch, s = swing, d = diff (circular |p - s|, 0-500; 0 is the same wheel spot as 1000).
The null is that same pitcher's own pairs with the previous-PA values shuffled (keeps his pitch habits and his diff
distribution, breaks only the link). Reported per candidate: pooled hits vs the null mean, the ratio, z = (obs - null) /
sqrt(null variance), and how many pitchers are individually significant (permutation p < .05; chance is 5%).

Candidates, for each base b in {p, s, d} (wheel values 1..1000):
  id        the value itself (p:id / s:id are the repeat / echo spikes the deployed model already has; d:id = "follows the diff")
  p500      b + 500, the opposite side of the wheel (d:p500 = "diff + 500")
  mirror    1000 - b
  rev3      digits reversed after zero-padding to 3 digits (045 -> 540);  revplain  45 -> 54
  acb bac bca cab cba   the digit rearrangements of the 3-digit value;  any = any non-identity rearrangement
and the arithmetic combos p-s, s-p, p+s, p+d, p-d, s+d, s-d (mod 1000).

Pitches / swings of 0 are autos or unsubmitted pitches (not throws) and steals are not PAs, so both are left out.
A transform that returns its own base value (a palindrome under rev3, 500 under mirror, ...) is just the id candidate, so it
is masked. --exclude additionally drops pairs where the next pitch equals the previous pitch or previous swing (the repeat /
echo spikes the deployed model already has) - the fair test of anything NEW. Under --exclude the p+d / p-d / s+d / s-d rows read
far below chance; that is an artifact (those formulas are the previous pitch or swing or their mirror), not a real avoidance.

Usage:
  python -I tools/pitch_prev_pa_follow.py fetch                      # download every play from Supabase -> tools/probe_output/all_plays.pkl
  python -I tools/pitch_prev_pa_follow.py test                       # all candidates, exact match
  python -I tools/pitch_prev_pa_follow.py test --exclude             # ... without pairs that repeat the previous pitch / echo the previous swing
  python -I tools/pitch_prev_pa_follow.py test --radius 0 5 10 25 50 --only d:id d:p500 --same-game

Findings (2026-10-22, 191 pitchers, 70,871 pairs, seasons 1-13): next == previous diff 365 vs 81 chance (4.5x, 34% of pitchers
significant; not repeats or round numbers); diff + 500 is chance at the pitcher level; previous pitch + 500 411 vs 134 (3.1x, 27% of
pitchers); digit reversal / first-two-digits swaps of the previous pitch ~1.6-2.0x while swapping only the last two digits is ~chance.
tools/pitch_model_spike_screen.py measures whether any of it helps the deployed model.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKL = ROOT / "tools" / "probe_output" / "all_plays.pkl"


def circ(a, b):
    d = np.abs(a - b) % 1000
    return np.minimum(d, 1000 - d)


def wheel(x):
    """Wheel values 1..1000 (0 is the same spot as 1000); NaN stays NaN."""
    x = np.asarray(x, float)
    y = x % 1000
    return np.where(x != x, np.nan, np.where(y == 0, 1000.0, y))


def _digit_variants(x):
    """(n, 5) of acb, bac, bca, cab, cba for the 3-digit zero-padded value abc; NaN for 1000, the value itself, or 0."""
    out = np.full((len(x), 5), np.nan)
    for i, v in enumerate(x):
        if v != v or v >= 1000:
            continue
        a, b, c = str(int(v)).zfill(3)
        for j, t in enumerate((a + c + b, b + a + c, b + c + a, c + a + b, c + b + a)):
            w = int(t)
            if w != int(v) and w >= 1:
                out[i, j] = w
    return out


def _rev3(x):
    return np.array([int(str(int(v)).zfill(3)[::-1]) if v == v else np.nan for v in x], float)


def _revplain(x):
    return np.array([int(str(int(v))[::-1]) if v == v else np.nan for v in x], float)


DIGIT_NAMES = ("acb", "bac", "bca", "cab", "cba")


def candidates(p, s, d) -> dict[str, np.ndarray]:
    """{name: (n, k) array of candidate values for the next pitch}, built from the previous-PA arrays."""
    out: dict[str, np.ndarray] = {}
    for b, x in (("p", p), ("s", s), ("d", d)):
        out[f"{b}:id"] = x[:, None]
        out[f"{b}:p500"] = wheel(x + 500)[:, None]
        out[f"{b}:mirror"] = wheel(1000 - x)[:, None]
        r3, rp = _rev3(x), _revplain(x)
        out[f"{b}:rev3"] = r3[:, None]
        out[f"{b}:revplain"] = rp[:, None]
        V = _digit_variants(x)
        for j, nm in enumerate(DIGIT_NAMES):
            out[f"{b}:{nm}"] = V[:, j][:, None]
        out[f"{b}:any"] = V
    out["p-s"], out["s-p"], out["p+s"] = wheel(p - s)[:, None], wheel(s - p)[:, None], wheel(p + s)[:, None]
    out["p+d"], out["p-d"] = wheel(p + d)[:, None], wheel(p - d)[:, None]
    out["s+d"], out["s-d"] = wheel(s + d)[:, None], wheel(s - d)[:, None]
    # a transform that returns its own base value (a palindrome under rev3, 500 under mirror, ...) is just the id
    # candidate, not a new relationship - mask it
    for nm, base in (("p", p), ("s", s), ("d", d)):
        for key in [k for k in out if k.startswith(nm + ":") and not k.endswith(":id")]:
            C = out[key].copy()
            C[C == base[:, None]] = np.nan
            out[key] = C
    return out


def load_groups(min_pairs: int, exclude: bool, same_game: bool) -> list[dict]:
    if not PKL.exists():
        sys.exit(f"{PKL} not found - run `python -I tools/pitch_prev_pa_follow.py fetch` first")
    df = pd.read_pickle(PKL)
    df = df[df["play_type"] == "Swing"]
    df = df[df["pitch"].between(1, 1000) & df["swing"].between(1, 1000)].copy()
    df["d"] = circ(df["pitch"].astype(float).to_numpy(), df["swing"].astype(float).to_numpy())
    df = df.sort_values(["pitcher_id", "id"])
    groups = []
    for pid, g in df.groupby("pitcher_id"):
        p, s = g["pitch"].to_numpy(float), g["swing"].to_numpy(float)
        d, gm = wheel(g["d"].to_numpy(float)), g["game_id"].to_numpy()
        if len(p) < 2:
            continue
        nxt, pp, ss, dd = p[1:], p[:-1], s[:-1], d[:-1]
        keep = np.ones(len(nxt), bool)
        if same_game:
            keep &= gm[1:] == gm[:-1]
        if exclude:
            keep &= (nxt != pp) & (nxt != ss)
        if keep.sum() < min_pairs:
            continue
        C = candidates(pp, ss, dd)
        groups.append({"nxt": nxt[keep], "cands": {k: v[keep] for k, v in C.items()}})
    return groups


def run_tests(groups: list[dict], radii: list[int], boots: int, only: list[str] | None, seed: int) -> None:
    rng = np.random.default_rng(seed)
    names = [n for n in groups[0]["cands"] if not only or n in only]
    print(f"{len(groups)} pitchers, {sum(len(g['nxt']) for g in groups)} pairs")
    print(f"{'candidate':10s} {'radius':>6s} {'hits':>6s} {'chance':>8s} {'ratio':>6s} {'z':>7s}  pitchers significant (chance 5%)")
    for nm in names:
        for r in radii:
            obs = mean = var = 0.0
            sig = npit = 0
            for g in groups:
                C, nxt = g["cands"][nm], g["nxt"]
                n = len(nxt)
                if n < 50 or not np.isfinite(C).any():
                    continue

                def hits(M):
                    return int((circ(M, nxt[:, None]) <= r).any(axis=1).sum()) if r else int((M == nxt[:, None]).any(axis=1).sum())
                o = hits(C)
                arr = np.array([hits(C[rng.permutation(n)]) for _ in range(boots)], float)
                obs, mean, var = obs + o, mean + arr.mean(), var + arr.var()
                sig += int((1 + (arr >= o).sum()) / (1 + boots) < 0.05)
                npit += 1
            if npit == 0 or mean == 0:
                continue
            z = (obs - mean) / np.sqrt(var) if var > 0 else float("nan")
            print(f"{nm:10s} {r:6d} {int(obs):6d} {mean:8.1f} {obs / mean:6.2f} {z:7.2f}  {sig}/{npit} ({sig / npit * 100:.1f}%)", flush=True)


def fetch() -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import local_plays
    rows = local_plays.plays()      # the local raw copy when it exists, else one live download that creates it
    df = pd.DataFrame(rows)
    if "games" in df.columns:
        g = df["games"].apply(lambda x: x if isinstance(x, dict) else {})
        for k in ("season", "session_number", "game_code"):
            df[f"g_{k}"] = g.apply(lambda d: d.get(k))
        df = df.drop(columns=["games"])
    PKL.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(PKL)
    print(f"saved {len(df)} plays to {PKL}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["fetch", "test"])
    ap.add_argument("--min-pairs", type=int, default=100)
    ap.add_argument("--radius", type=int, nargs="+", default=[0])
    ap.add_argument("--boots", type=int, default=200)
    ap.add_argument("--only", nargs="+", help="restrict to these candidate names, e.g. d:id p:p500")
    ap.add_argument("--exclude", action="store_true", help="drop pairs where next == previous pitch or previous swing (the fair test of new things)")
    ap.add_argument("--same-game", action="store_true", help="only pairs inside one game")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch()
        return
    groups = load_groups(a.min_pairs, a.exclude, a.same_game)
    run_tests(groups, a.radius, a.boots, a.only, a.seed)


if __name__ == "__main__":
    main()

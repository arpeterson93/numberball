"""Checks for bt.apply_mixtures (tools/pitch_likelihood_backtest.py): the exact-value mixture and the four repeat / echo spikes.

  python -I tools/verify_pitch_mixtures.py

Each spike is a point mass at a reference value with rate (his hits + m * league) / (his opportunities + m), counted within one
game. References: previous pitch, previous swing, previous PA's diff (circular |pitch - swing|, 0 = the wheel spot 1000) and
previous pitch + 500. The checks hand-compute those rates on a tiny history and compare to the real function, then test the
edge cases: still sums to 1, spikes skipped across a game boundary, a 0 (auto) pitch leaves the diff / +500 references
undefined, and an old tables file without the two newer league rates still works.
"""
from __future__ import annotations

import json
import pathlib
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import pitch_likelihood_backtest as bt  # noqa: E402

RAW = json.loads((ROOT / "pitch_model_tables.json").read_text())
TABLES = bt.prepare_tables(RAW)
GRID = bt.GRID_SIZE
FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{(' - ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILS.append(name)


def cfg(tables=TABLES, e=0.02, m=150.0):
    return bt.Config(sigma=75.0, min_effective_n=20.0, pitch_width=100, cooldown_radius=50.0, leverage_width=0.5,
                     delta_cooldown_radius=50.0, weights={}, tables=tables,
                     habit={"mixtures": True, "e_exact": e, "m_spike": m})


def hist(pitches, swings, games):
    return pd.DataFrame({"pitch": pitches, "swing": swings, "game_id": games, "id": np.arange(len(pitches))})


def wheel_diff(p, s):
    d = abs(p - s) % 1000
    d = min(d, 1000 - d)
    return 1000 if d == 0 else d


def expected(c, ps, ss, gs, tgt_game, tables, e, m):
    """Independent hand-computation of the mixture for the history (ps, ss, gs) predicting a pitch in game tgt_game."""
    n = len(ps)
    own = np.bincount((np.array(ps) - 1) % GRID, minlength=GRID).astype(float)
    q = (1 - e) * c + e * own / own.sum()
    league = tables["spike_rates"]
    refs = {}
    refs["prev_pitch"] = (ps[-1], [ps[j + 1] == ps[j] for j in range(n - 1)], [gs[j + 1] == gs[j] for j in range(n - 1)], gs[-1])
    refs["prev_swing"] = (ss[-1], [ps[j + 1] == ss[j] for j in range(n - 1)], [gs[j + 1] == gs[j] for j in range(n - 1)], gs[-1])
    dv = [wheel_diff(p, s) if (1 <= p <= 1000 and 1 <= s <= 1000) else None for p, s in zip(ps, ss)]
    p5 = [(p + 499) % 1000 + 1 if 1 <= p <= 1000 else None for p in ps]
    for nm, arr in (("prev_diff", dv), ("prev_pitch_p500", p5)):
        refs[nm] = (arr[-1] if arr[-1] is not None else float("nan"),
                    [arr[j] is not None and ps[j + 1] == arr[j] for j in range(n - 1)],
                    [gs[j + 1] == gs[j] and arr[j] is not None for j in range(n - 1)], gs[-1])
    total, add = 0.0, np.zeros(GRID)
    for nm, (val, hit, dfd, rg) in refs.items():
        if nm not in league or val != val or (tgt_game is not None and rg != tgt_game):
            continue
        rate = (sum(h for h, d in zip(hit, dfd) if d) + m * league[nm]) / (sum(dfd) + m)
        total += rate
        add[int(val) - 1] += rate
    return (1 - total) * q + add


rng = np.random.default_rng(0)
print("apply_mixtures checks")

# 1. hand-computed mixture on a small history; also still a probability distribution
ps = [120, 120, 640, 250, 250, 250, 880, 400, 150, 650, 400, 400]
ss = [20, 500, 180, 250, 1, 750, 880, 150, 400, 90, 800, 150]
gs = [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2]
c = rng.random(GRID)
c /= c.sum()
for tgt in (2.0, 3.0):
    out = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(), pd.Series({"game_id": tgt}))
    exp = expected(c, ps, ss, gs, tgt, TABLES, 0.02, 150.0)
    check(f"matches hand computation (target in game {tgt:.0f})", np.max(np.abs(out - exp)) < 1e-12, f"max diff {np.max(np.abs(out - exp)):.2e}")
    check(f"sums to 1 (target in game {tgt:.0f})", abs(out.sum() - 1) < 1e-12, f"sum {out.sum():.12f}")

# 2. each spike lands on the right value: last pitch 400 / swing 150 -> prev_pitch 400, prev_swing 150, diff 250, +500 = 900
base_only = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(), pd.Series({"game_id": 2.0}))
tb = {k: v for k, v in TABLES.items()}
tb["spike_rates"] = {}
no_spikes = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(tb), pd.Series({"game_id": 2.0}))
for nm, val in (("previous pitch", 400), ("previous swing", 150), ("previous diff", 250), ("previous pitch + 500", 900)):
    check(f"spike on {nm} lands at {val}", base_only[val - 1] > no_spikes[val - 1] * 1.0001)

# 3. a game boundary: the last pitch is in game 2 but the target is in game 3 -> every spike skipped
out3 = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(), pd.Series({"game_id": 3.0}))
check("no spikes when the last pitch is in a different game", np.max(np.abs(out3 - no_spikes)) < 1e-12)

# 4. a 0 (auto) as the last pitch: diff and +500 undefined, but the plain previous-pitch / previous-swing spikes still apply
ps0, ss0 = ps[:-1] + [0], ss[:-1] + [333]
out0 = bt.apply_mixtures(c, hist(ps0, ss0, gs), {}, cfg(), pd.Series({"game_id": 2.0}))
exp0 = expected(c, ps0, ss0, gs, 2.0, TABLES, 0.02, 150.0)
check("0 pitch: matches hand computation", np.max(np.abs(out0 - exp0)) < 1e-12)
check("0 pitch: no spike at the +500 value (500)", out0[499] < base_only[499] * 5)   # sanity: nothing special is added there

# 5. an old tables file (no prev_diff / prev_pitch_p500 rates) still works and equals the two-spike model
old = dict(TABLES)
old["spike_rates"] = {k: v for k, v in TABLES["spike_rates"].items() if k not in ("prev_diff", "prev_pitch_p500")}
out_old = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(old), pd.Series({"game_id": 2.0}))
exp_old = expected(c, ps, ss, gs, 2.0, old, 0.02, 150.0)
check("old tables (two spikes) still work", np.max(np.abs(out_old - exp_old)) < 1e-12 and abs(out_old.sum() - 1) < 1e-12)

# 6. live use: no target row -> the last pitch is assumed to be in the same game
out_live = bt.apply_mixtures(c, hist(ps, ss, gs), {}, cfg(), None)
exp_live = expected(c, ps, ss, gs, None, TABLES, 0.02, 150.0)
check("live prediction (no target row) matches", np.max(np.abs(out_live - exp_live)) < 1e-12)

# 7. random histories: always a probability distribution
bad = 0
for _ in range(200):
    n = int(rng.integers(4, 60))
    p = rng.integers(0, 1001, n).tolist()
    s = rng.integers(0, 1001, n).tolist()
    g = np.sort(rng.integers(1, 4, n)).tolist()
    cc = rng.random(GRID)
    cc /= cc.sum()
    o = bt.apply_mixtures(cc, hist(p, s, g), {}, cfg(), pd.Series({"game_id": float(g[-1])}))
    bad += int(abs(o.sum() - 1) > 1e-9 or (o < 0).any() or not np.isfinite(o).all())
check("200 random histories (incl. zeros) give valid densities", bad == 0, f"{bad} bad")

print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)

"""Per-swing run value, WPA and their calculator-model expectations, for the website's career data.

Every swing (a play with a pitch, a swing and a diff, every season) is scored three ways:

  rv   runs added: run expectancy after the play plus runs scored on it, minus run expectancy before it (RE24)
  wp   win probability added over the swing, batting side, in percentage points
  xr / xw   what the league's result mix at that swing's diff, in that outs-and-bases state, would have produced
            (tools/diff_lab/model.npz: the result mix stacked from every historical swing's calculator ranges)

Hitters are credited with the numbers as they are; pitchers get the same numbers negated (runs and win probability
saved). Used by career_build.py; the Diff Distribution Lab (tools/diff_lab) is the exploratory version of the same.
"""
import os
from functools import lru_cache

import numpy as np

import utils

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "diff_lab", "model.npz")
INNINGS = 6
FIRST_SEASON = 1
BINS = 25          # diff 0-500 in buckets of 20 (500 joins the last bucket)
BIN_W = 20
MERGE = {"DPRun": "DP", "TP": "DP", "LODP": "DP"}
OBC2BRC = {"000": 0, "001": 1, "010": 2, "100": 3, "011": 4, "101": 5, "110": 6, "111": 7}
BRC2OBC = {v: k for k, v in OBC2BRC.items()}
# Simple result classes for the diff chart: HR 3B 2B 1B BB K GO(+DP, FC) Air
SIMPLE = {"HR": 0, "3B": 1, "2B": 2, "2BWH": 2, "1B": 3, "1BWH": 3, "1BWH2": 3, "IF1B": 3, "BB": 4, "K": 5,
          "SacF": 7, "DSacF": 7, "FO": 7, "DFO": 7, "PO": 7}
for _c in ("GORA", "FC", "FC3rd", "FCH", "GO", "DP", "DP21", "DP31", "DPH1"):
    SIMPLE[_c] = 6

_M = None


def model():
    global _M
    if _M is None:
        z = np.load(MODEL_PATH)
        gc = [str(g) for g in z["GC"]]
        _M = {"PN": z["PN"].astype(np.float32), "P8": z["P8"], "TRANS": z["TRANS"], "RVT": np.nan_to_num(z["RVT"]),
              "RVOK": ~np.isnan(z["RVT"]), "RE": z["RE"], "GC": gc, "GI": {g: i for i, g in enumerate(gc)}}
    return _M


def available() -> bool:
    return os.path.exists(MODEL_PATH)


def bin_of(d: int) -> int:
    return min(d // BIN_W, BINS - 1)


def league_bins() -> list:
    """The model's result share per 20-wide diff bucket: [class][bucket], classes as SIMPLE (0..7)."""
    p8 = model()["P8"]
    out = []
    for c in range(8):
        row = []
        for b in range(BINS):
            lo, hi = b * BIN_W, (b + 1) * BIN_W if b < BINS - 1 else 501
            row.append(round(float(p8[c, lo:hi].mean()), 4))
        out.append(row)
    return out


def _wp_before(inning, half, outs, brc, lead):
    return utils.get_win_probability_interpolated(utils.remaining_half_innings(inning, half, INNINGS), outs, BRC2OBC[brc], lead)


def _wp_after(inning, half, outs_a, brc_a, lead_a):
    remh = utils.remaining_half_innings(inning, half, INNINGS)
    if half == "bottom" and remh == 1 and lead_a > 0:
        return 1.0
    if outs_a >= 3:
        rn = remh - 1
        if rn >= 1:
            o = utils.get_win_probability_interpolated(rn, 0, "000", -lead_a)
            return None if o is None else 1.0 - o
        return 1.0 if lead_a > 0 else (0.5 if lead_a == 0 else 0.0)
    return utils.get_win_probability_interpolated(remh, outs_a, BRC2OBC[brc_a], lead_a)


@lru_cache(maxsize=None)
def _ctx_wpa(inning, half, outs, brc, lead):
    """WPA of every possible result in this exact situation (None where unavailable)."""
    m = model()
    wb = _wp_before(inning, half, outs, brc, lead)
    out = np.zeros(len(m["GC"]), dtype=np.float64)
    if wb is None:
        return out, wb
    s8 = outs * 8 + brc
    for c in range(len(m["GC"])):
        runs, oa, br = (int(v) for v in m["TRANS"][s8, c])
        if runs < 0:
            continue
        wa = _wp_after(inning, half, oa, br, lead + runs)
        if wa is not None:
            out[c] = wa - wb
    return out, wb


def swing_values(m: dict):
    """(class index, rv, wpa, xrv, xwpa, simple class) for one play, or None when it is not a modelled swing."""
    d = m.get("diff")
    if d is None or m.get("pitch") is None or m.get("swing") is None:
        return None
    mod = model()
    res = m.get("result")
    g = mod["GI"].get(MERGE.get(res, res))
    brc = OBC2BRC.get(m.get("obc_before"))
    outs = m.get("outs_before")
    if g is None or brc is None or outs is None or outs > 2:
        return None
    runs = m.get("runs") or 0
    home = bool(m.get("batting_is_home"))
    off_after = m["home_score"] if home else m["away_score"]
    def_after = m["away_score"] if home else m["home_score"]
    lead_b = off_after - runs - def_after
    inn, half = m["inning"], m["half"]
    outs_a = m.get("outs_after")
    obc_a = OBC2BRC.get(m.get("obc_after"))
    if outs_a is None or (outs_a < 3 and obc_a is None):
        return None
    re_a = float(mod["RE"][outs_a, obc_a]) if outs_a < 3 else 0.0
    rv = re_a + runs - float(mod["RE"][outs, brc])
    lead_a = lead_b + runs
    wb = _wp_before(inn, half, outs, brc, lead_b)
    if m.get("is_game_final"):
        wa = 1.0 if lead_a > 0 else (0.5 if lead_a == 0 else 0.0)
    else:
        wa = _wp_after(inn, half, min(outs_a, 3), obc_a or 0, lead_a)
    wpa = 0.0 if (wa is None or wb is None) else wa - wb
    s8 = outs * 8 + brc
    pr = mod["PN"][s8, :, min(int(d), 500)]
    xrv = float((pr * mod["RVT"][s8]).sum())
    w, _ = _ctx_wpa(inn, half, outs, brc, lead_b)
    xw = float((pr * w).sum())
    return g, rv, wpa, xrv, xw, SIMPLE.get(MERGE.get(res, res), 7)


def _blank():
    return {"rv": 0.0, "xr": 0.0, "wp": 0.0, "xw": 0.0}


def game_metrics(rows: list, season: int):
    """One game's swing values per hitter and per pitcher, plus each swing's (side, player, bucket, result class
    index into the model's result list) for the diff charts. Returns ({"bat": {pid: {...}}, "pit": {pid: {...}}}, [(side, pid, bucket, class), ...])."""
    out = {"bat": {}, "pit": {}}
    dist = []
    if season < FIRST_SEASON or not available():
        return out, dist
    for m in rows:
        v = swing_values(m)
        if v is None:
            continue
        g, rv, wpa, xrv, xw, _sc = v
        for side, pid, sign in (("bat", m.get("batter_id"), 1), ("pit", m.get("pitcher_id"), -1)):
            if not pid:
                continue
            e = out[side].setdefault(pid, _blank())
            e["rv"] += sign * rv
            e["xr"] += sign * xrv
            e["wp"] += sign * wpa * 100
            e["xw"] += sign * xw * 100
            dist.append((side, pid, bin_of(m["diff"]), g))
    for side in out:
        for e in out[side].values():
            for k in e:
                e[k] = round(e[k], 3)
    return out, dist

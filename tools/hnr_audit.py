"""Replay every historical swing through the exact sheet calculator.

    python tools/hnr_audit.py [season ...]

For each Swing play the calculator is run under each strategy that could apply (normal, hit and run, infield in,
H&R + infield in) and the play's diff is located in each table. Prints the observed-result vs table-label
crosstab, per-season agreement, and writes tools/diff_lab/cache/hnr_audit.pkl for the classifier.
"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
import sheet_calculator as sc

CACHE = ROOT / "tools/diff_lab/cache"
calc = sc.SheetCalculator(sc.load_params_json(ROOT / "sheet_calculator_params.json"))
STATS = ["con", "eye", "pwr", "spd", "mov", "cmd", "vel", "awr"]


def load():
    plays = pd.read_pickle(CACHE / "plays.pkl")
    pl = pd.read_pickle(CACHE / "players.pkl")
    pl = pl[(pl.league == "MLN") & ~pl.s_id.astype(str).str.startswith("R_") & pl.player_id.notna()].copy()
    pl["player_id"] = pl.player_id.astype(int)
    pl["season"] = pl.season.astype(int)
    pl = pl.drop_duplicates(["season", "player_id"], keep="first").set_index(["season", "player_id"])
    return plays, pl


def rid(v):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


_cache = {}


def ranges_for(key):
    r = _cache.get(key)
    if r is None:
        ph, pm, pc, pv, pa, bh, bc, be, bp, bs, rs, outs, hnr, ifin = key
        out = calc.compute(pit_hand=ph, pit_mov=pm, pit_cmd=pc, pit_vel=pv, pit_awr=pa, bat_hand=bh, bat_con=bc,
                           bat_eye=be, bat_pow=bp, bat_spd=bs, runner_speeds=rs, outs=outs, hit_and_run=hnr,
                           infield_in=ifin)
        r = [(n, lo, hi) for n, w, lo, hi in out["swing_ranges"]]
        _cache[key] = r
    return r


def label_at(rngs, diff):
    for n, lo, hi in rngs:
        if lo <= diff <= hi:
            return n
    return None


def build(seasons=None):
    plays, pl = load()
    s = plays[(plays.play_type == "Swing") & plays["diff"].notna() & (plays.game_type == "live")].copy()
    if seasons:
        s = s[s.season.isin(seasons)]
    rows = []
    for r in s.itertuples(index=False):
        se = int(r.season)
        try:
            b = pl.loc[(se, int(r.batter_id))]
            p = pl.loc[(se, int(r.pitcher_id))]
        except KeyError:
            rows.append(None); continue
        if b[["con", "eye", "pwr", "spd"]].isna().any() or p[["mov", "cmd", "vel", "awr"]].isna().any():
            rows.append(None); continue
        rs = []
        for k, val in enumerate((r.on_first, r.on_second, r.on_third)):
            i = rid(val)
            if i is None:
                rs.append(None)
            else:
                try:
                    sp = pl.loc[(se, i)]["spd"]
                    rs.append(3 if sp != sp else int(sp))
                except KeyError:
                    rs.append(3)
        rs = tuple(rs)
        base = (p["hand"], int(p["mov"]), int(p["cmd"]), int(p["vel"]), int(p["awr"]), b["hand"], int(b["con"]),
                int(b["eye"]), int(b["pwr"]), int(b["spd"]), rs, int(r.outs))
        d = int(r.diff)
        labs = {}
        for tag, (h, i) in {"n": (False, False), "h": (True, False), "i": (False, True), "hi": (True, True)}.items():
            labs[tag] = label_at(ranges_for(base + (h, i)), d)
        rows.append((r.id, se, r.result, d, r.obc, int(r.outs), labs["n"], labs["h"], labs["i"], labs["hi"]))
    out = pd.DataFrame([x for x in rows if x], columns=["id", "season", "result", "diff", "obc", "outs", "n", "h", "i", "hi"])
    print(f"{len(s)} swings, {len(out)} with full stats ({len(s) - len(out)} skipped)")
    return out


if __name__ == "__main__":
    seasons = [int(x) for x in sys.argv[1:]] or None
    a = build(seasons)
    a.to_pickle(CACHE / "hnr_audit.pkl")
    print(a.groupby("season").apply(lambda g: (g.result == g.n).mean()).round(3).to_string())
    print(pd.crosstab(a.result, a.n.fillna("NONE")).to_string())

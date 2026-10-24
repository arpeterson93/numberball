"""Replay every historical swing through the sheet calculator and flag posted results the ranges do not imply.

    python tools/result_audit.py [season ...]

Each swing is run under every strategy that could apply (normal, hit and run, infield in, both) with the players'
attributes for that season. A swing agrees when the posted result is what ANY of those tables gives at the play's diff.
A disagreement gets two gaps: how many diff points the pitch/swing would have to move for the posted RESULT to be
right (gap) and for its CLASS (HR 3B 2B 1B BB K Air GO) to be right (class_gap; 0 = same class, so only a variant
such as FO vs SacF or DP vs DP21). 9999 = not in any table. A small class gap is a boundary effect (a build that
moved mid-season, rounding); a big one points at a mis-keyed pitch, swing or result. Intentional walks and bunts skipped.

Reads tools/diff_lab/cache (plays.pkl, players.pkl - refresh with tools/diff_lab/fetch_plays.py / fetch_players.py)
and docs/data/sNN + root plays_*.json for game code / session / inning. Writes tools/probe_output/result_audit.csv.
"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import numpy as np
import pandas as pd
import hnr_audit as ha

MERGE = {"DPRun": "DP", "TP": "DP", "LODP": "DP"}
SIMPLE = {"HR": "HR", "3B": "3B", "2B": "2B", "2BWH": "2B", "1B": "1B", "1BWH": "1B", "1BWH2": "1B", "IF1B": "1B",
          "BB": "BB", "K": "K", "SacF": "Air", "DSacF": "Air", "FO": "Air", "DFO": "Air", "PO": "Air"}
for _c in ("GORA", "FC", "FC3rd", "FCH", "GO", "DP", "DP21", "DP31", "DPH1"):
    SIMPLE[_c] = "GO"
STRATS = {"n": (False, False), "h": (True, False), "i": (False, True), "hi": (True, True)}


def gap_to(rngs, name, d, by_class=False):
    best = 9999
    for n, lo, hi in rngs:
        if (SIMPLE.get(MERGE.get(n, n)) != SIMPLE.get(name)) if by_class else (n != name):
            continue
        best = min(best, 0 if lo <= d <= hi else (lo - d if d < lo else d - hi))
    return best


def site_index():
    """play_num -> (game_code, session, inning, half, away, home) from the website's play files."""
    out = {}
    for f in glob.glob("docs/data/s*/plays_*.json") + glob.glob("docs/data/plays_*.json"):
        with open(f, encoding="utf-8") as fh:
            for r in json.load(fh):
                out[r["play_num"]] = (r["game_code"], r["session_number"], r["inning"], r["half"],
                                      r["away_team_abbr"], r["home_team_abbr"])
    return out


def main():
    seasons = [int(x) for x in sys.argv[1:]] or None
    plays, pl = ha.load()
    s = plays[(plays.play_type == "Swing") & plays["diff"].notna() & (plays.game_type == "live") & (plays.pitch > 0)].copy()
    if seasons:
        s = s[s.season.isin(seasons)]
    rows = []
    for r in s.itertuples(index=False):
        se = int(r.season)
        try:
            b = pl.loc[(se, int(r.batter_id))]
            p = pl.loc[(se, int(r.pitcher_id))]
        except KeyError:
            continue
        if b[["con", "eye", "pwr", "spd"]].isna().any() or p[["mov", "cmd", "vel", "awr"]].isna().any():
            continue
        rs = []
        for val in (r.on_first, r.on_second, r.on_third):
            i = ha.rid(val)
            if i is None:
                rs.append(None)
            else:
                try:
                    sp = pl.loc[(se, i)]["spd"]
                    rs.append(3 if sp != sp else int(sp))
                except KeyError:
                    rs.append(3)
        base = (p["hand"], int(p["mov"]), int(p["cmd"]), int(p["vel"]), int(p["awr"]), b["hand"], int(b["con"]),
                int(b["eye"]), int(b["pwr"]), int(b["spd"]), tuple(rs), int(r.outs))
        d = int(r.diff)
        res = MERGE.get(r.result, r.result)
        if res == "IBB" or (res.startswith("B") and res != "BB"):
            continue        # intentional walks and bunt results come from other tables
        tabs = {t: ha.ranges_for(base + hi) for t, hi in STRATS.items()}
        labs = {t: ha.label_at(tabs[t], d) for t in tabs}
        if res in labs.values():
            continue
        gap = min(gap_to(tabs[t], res, d) for t in tabs)
        cgap = min(gap_to(tabs[t], res, d, True) for t in tabs)
        implied = labs["n"]
        rows.append({"play_num": int(r.play_num), "season": se, "batter": r.batter_name, "pitcher": r.pitcher_name,
                     "pitch": int(r.pitch), "swing": int(r.swing) if r.swing == r.swing else None, "diff": d,
                     "posted": r.result, "implied": implied, "gap": gap, "class_gap": cgap, "outs": int(r.outs), "obc": r.obc,
                     "class_posted": SIMPLE.get(res, "other"), "class_implied": SIMPLE.get(implied, "other")})
    out = pd.DataFrame(rows)
    idx = site_index()
    cols = ["game_code", "session", "inning", "half", "away", "home"]
    info = out.play_num.map(idx)
    for k, c in enumerate(cols):
        out[c] = info.map(lambda x, k=k: x[k] if x else None)
    out["class_changed"] = out.class_posted != out.class_implied
    out = out.sort_values(["class_gap", "gap", "season"], ascending=[False, False, True])
    dest = ROOT / "tools/probe_output/result_audit.csv"
    dest.parent.mkdir(exist_ok=True)
    out.to_csv(dest, index=False)
    tot = s.groupby("season").size()
    print("disagreements by season (of swings):")
    for se, n in out.groupby("season").size().items():
        g = out[out.season == se]
        print(f"  S{se}: {n} of {tot.get(se, 0)} ({n / tot.get(se, 1):.1%}) differ in result; "
              f"{(g.class_gap > 0).sum()} differ in class, {(g.class_gap >= 40).sum()} by 40+ diff points")
    print(f"wrote {dest} ({len(out)} rows)")


if __name__ == "__main__":
    main()

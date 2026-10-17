"""Classify historical swings as hit-and-run / not from the diff, using tools/hnr_audit.py's output.

    python tools/hnr_audit.py      # once (writes the pickle)
    python tools/hnr_classify.py [first_season]   # default 12 (rules for 5-11 drift; see the empty-bases control)

For a swing where the normal table and the H&R table (CON -1, runner SPD +1) give DIFFERENT results at the play's
diff, the observed result says which mode was live. Rate = H&R-only / (H&R-only + normal-only).
Control: with bases empty nobody can be running, so any "H&R-looking" result there is rules drift / noise.
"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
a = pd.read_pickle(ROOT / "tools/diff_lab/cache/hnr_audit.pkl")
first = int(sys.argv[1]) if len(sys.argv) > 1 else 12
a = a[a.season >= first].copy()
a["r1"] = a.obc.str[2] == "1"; a["r2"] = a.obc.str[1] == "1"; a["r3"] = a.obc.str[0] == "1"
inf = a[(a.n != a.h)].copy()
inf["mode"] = "neither"
inf.loc[inf.result == inf.n, "mode"] = "normal"
inf.loc[inf.result == inf.h, "mode"] = "hnr"
elig = inf[inf.r1 | inf.r2]
ctl = inf[inf.obc == "000"]
print(f"seasons >= {first}: {len(a)} swings")
print(f"control, empty bases: {len(ctl)} informative, {(ctl['mode'] == 'hnr').sum()} look like H&R (should be ~0)")
t = elig.groupby("season")["mode"].value_counts().unstack(fill_value=0)
t["hnr_rate"] = (t.get("hnr", 0) / (t.get("hnr", 0) + t.get("normal", 0))).round(3)
print("\nrunner on 1st/2nd, informative swings by season:\n", t.to_string())
for col in ("outs", "obc"):
    g = elig.groupby(col)["mode"].value_counts().unstack(fill_value=0)
    g["hnr_rate"] = (g.get("hnr", 0) / (g.get("hnr", 0) + g.get("normal", 0))).round(3)
    print(f"\nby {col}:\n", g.to_string())

# every observed WH result: could the normal table alone explain it?
wh = a[a.result.isin(["1BWH", "1BWH2", "2BWH"]) & (a.r1 | a.r2)].copy()
wh["verdict"] = "normal explains"
wh.loc[(wh.result != wh.n) & (wh.result == wh.h), "verdict"] = "only H&R explains"
wh.loc[(wh.result != wh.n) & (wh.result != wh.h), "verdict"] = "neither explains"
print("\nobserved WH results:\n", wh.groupby(["result", "verdict"]).size().unstack(fill_value=0).to_string())
out = ROOT / "tools/diff_lab/cache/hnr_classified.pkl"
inf.to_pickle(out)
print("\nwrote", out)

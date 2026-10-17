"""Scan every legal build for Mickey Scales with the rest of the S13 Pioneers lineup held constant.

    python tools/offense_lab/run_mickey.py
"""
import sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import pandas as pd
import offense_sim as o
import data as D
import analysis as A

pl = D.load_players(); plays = D.load_plays()
mix = D.pitcher_mix(13, 25, plays, pl)
roster = D.team_hitters(13, "POR", pl)
by = {r["name"]: r for r in roster.to_dict("records")}
order = ["Mickey Scales", "Jerk McGurkin", "Miikka Jarvinen", "Speedo Meter", "Copenhagen Black", "Cola Spritz",
         "Gal Avant", "Guy Gilwater", "Hard Candy"]
lineup = [D.to_hitter(by[n]) for n in order]
cmix = D.catcher_mix(13, plays, pl)
print('catcher EYE mix:', cmix)
model = o.OffenseModel(catcher_mix=cmix)
cur = (5, 2, 2, 3)
res = []
for slot in (0, 2, 4, 8):      # Mickey leading off, batting 3rd (about where he really bats), 5th, 9th
    lu = list(lineup)
    lu[0], lu[slot] = lu[slot], lu[0]
    idx = [h.name for h in lu].index("Mickey Scales")
    t = time.time()
    rows = A.scan_player(model, lu, idx, mix)
    base = dict(rows)[cur]
    print(f"\nMickey batting {idx + 1}: current 5/2/2/3 = {base:.3f}  ({time.time()-t:.0f}s)")
    print("  top 8 :", ", ".join(f"{'/'.join(map(str, b))} {v - base:+.3f}" for b, v in rows[:8]))
    print("  bottom 3:", ", ".join(f"{'/'.join(map(str, b))} {v - base:+.3f}" for b, v in rows[-3:]))
    res.append((idx + 1, rows))
# stat-level marginal view from the slot-3 scan: average value of each point of each stat
rows = res[1][1]
df = pd.DataFrame([(*b, v) for b, v in rows], columns=["con", "eye", "pwr", "spd", "runs"])
print("\nmean team runs by stat level (slot 3, all builds):")
for c in ["con", "eye", "pwr", "spd"]:
    print(" ", c, df.groupby(c).runs.mean().round(3).to_dict())
df.to_csv(HERE / "mickey_scan_slot3.csv", index=False)

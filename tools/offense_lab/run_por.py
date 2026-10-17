"""First look at the Portland Pioneers S13 hitters in the exact offense model.

    python tools/offense_lab/run_por.py
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
print("legal defense:", D.legal_defense([(by[n]["primary_pos"], by[n]["secondary_pos"]) for n in order]))
cmix = D.catcher_mix(13, plays, pl)
print('catcher EYE mix:', cmix)
model = o.OffenseModel(catcher_mix=cmix)
t = time.time()
base, per_inn = model.expected_runs(lineup, mix, detail=True)
print(f"baseline lineup: {base:.3f} runs / 6 inn vs the S13 league pitcher mix  ({time.time()-t:.1f}s cold)")
t = time.time()
cr = pd.DataFrame(A.credits(model, lineup, mix))
print(f"credits ({time.time()-t:.0f}s), runs per game vs a 3/3/3/3 hitter:")
print(cr.round(3).to_string(index=False))

"""Team-level studies for the S13 Pioneers: homogeneous teams, best-response re-allocation, batting order.

    python tools/offense_lab/run_team.py
"""
import sys, time, json
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import offense_sim as o
import data as D
import analysis as A
from dataclasses import replace

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
fmt = lambda b: "/".join(map(str, b))
out = {}

t = time.time()
base = model.expected_runs(lineup, mix)
print(f"current nine, estimated order: {base:.3f}")

# 1. homogeneous teams: all nine hitters share one build
hom = []
for b in A.builds_sum():
    lu = [A.with_build(h, b) for h in lineup]
    hom.append((b, model.expected_runs(lu, mix)))
hom.sort(key=lambda x: -x[1])
print(f"\n1. everyone the same build ({time.time()-t:.0f}s): best", ", ".join(f"{fmt(b)} {v:.3f}" for b, v in hom[:6]),
      "| worst", ", ".join(f"{fmt(b)} {v:.3f}" for b, v in hom[-3:]))
out["homogeneous_top"] = [(b, v) for b, v in hom[:15]]

# 2. best-response re-allocation: each hitter in turn takes the build that maximises team runs, others fixed
cur = list(lineup)
for rnd in range(1, 5):
    changed = False
    for j in range(9):
        rows = A.scan_player(model, cur, j, mix)
        best_b, best_v = rows[0]
        now = (cur[j].con, cur[j].eye, cur[j].pwr, cur[j].spd)
        if best_b != now and best_v > dict(rows)[now] + 1e-6:
            cur[j] = A.with_build(cur[j], best_b); changed = True
    val = model.expected_runs(cur, mix)
    print(f"2. best-response round {rnd} ({time.time()-t:.0f}s): team {val:.3f}  builds:",
          " ".join(f"{h.name.split()[0]}:{fmt((h.con, h.eye, h.pwr, h.spd))}" for h in cur))
    if not changed:
        break
out["best_response"] = [(h.name, (h.con, h.eye, h.pwr, h.spd)) for h in cur]
out["best_response_value"] = model.expected_runs(cur, mix)

# 3. batting order for the current nine and for the best-response nine
for label, players in (("current builds", lineup), ("best-response builds", cur)):
    bo, bv, worst, n = A.hill_climb_order(model, players, mix, restarts=3)
    print(f"3. {label}: best order {bv:.3f} (worst seen {worst:.3f}, {n} orders tried): ", ", ".join(players[i].name.split()[0] for i in bo))
    out[f"order_{label}"] = ([players[i].name for i in bo], bv, worst)
(HERE / "team_results.json").write_text(json.dumps(out, indent=1, default=str))
print(f"done in {time.time()-t:.0f}s")

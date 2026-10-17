"""Checks for offense_sim.py.

    python tools/verify_offense_sim.py [lineups]

1. import_BRC.csv is internally consistent (runs and outs match the destination tokens).
2. Every (build, state) the calculator can produce has a baserunning row (random lineups, no KeyError).
3. The exact inning solver agrees with an independent event-by-event Monte Carlo (|z| < 3.5).
4. Sanity: a faster / better team scores more; steals off never beats steals on by construction.
5. The run-maximising strategy ('opt') never scores fewer runs than the league-table or win-probability rules, and
   a faster steal speed never lowers a lineup's runs under it.
6. A catcher mix is solved per catcher, and the exact answer still matches both simulations.
"""
import sys, random
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import offense_sim as o

n_lineups = int(sys.argv[1]) if len(sys.argv) > 1 else 6
m = o.OffenseModel()
bad = 0
for (outs, brc, res), (bt, rt, runs, eo) in m.brc.items():
    if res.endswith("Auto"):
        continue
    toks = [bt] + list(rt)
    if sum(1 for t in toks if t == 4) != runs or eo != outs + sum(1 for t in toks if t == 0):
        bad += 1
print("1. baserunning rows:", len(m.brc), "inconsistent:", bad)

rnd = random.Random(11)
def rb():
    while True:
        v = [rnd.randint(1, 5) for _ in range(4)]
        if sum(v) == 12:
            return v
def rpit(): return o.Pitcher(rnd.choice("LR"), *rb())
def rlu(): return [o.Hitter(f"h{i}", rnd.choice("LRS"), *rb()) for i in range(9)]

fails = 0
for k in range(n_lineups):
    lu = rlu()
    opp = rpit() if k % 2 == 0 else o.PitcherMix.of([(rnd.random() + .2, rpit()) for _ in range(4)])
    ex = m.expected_runs(lu, opp)
    mc, se = m.simulate(lu, opp, games=30000, seed=k)
    z = (mc - ex) / se
    fails += abs(z) > 3.5
    print(f"3. {'single' if k % 2 == 0 else 'mix   '} exact {ex:.4f}  MC {mc:.4f} +/- {se:.4f}  z={z:+.2f}")
print("   solver/MC disagreements:", fails)

fast = [o.Hitter(f"f{i}", "R", 3, 3, 3, 3, run_spd=5) for i in range(9)]
slow = [o.Hitter(f"s{i}", "R", 3, 3, 3, 3, run_spd=1) for i in range(9)]
p = o.Pitcher("R", 3, 3, 3, 3)
print("4. runner speed 5 vs 1 (same hitting):", round(m.expected_runs(fast, p), 3), ">", round(m.expected_runs(slow, p), 3))
no_steal = o.OffenseModel(steals=False)
print("   steals on vs off (fast team):", round(m.expected_runs(fast, p), 3), "vs", round(no_steal.expected_runs(fast, p), 3))
bad5 = 0
for k in range(3):
    lu = rlu(); opp = rpit()
    v = {d: o.OffenseModel(decision=d, catcher_mix={5: 9, 3: 1}).expected_runs(lu, opp) for d in ("opt", "re", "wp")}
    ok = v["opt"] >= v["re"] - 1e-9 and v["opt"] >= v["wp"] - 1e-9
    base = o.OffenseModel(catcher_mix={5: 9, 3: 1}).expected_runs(lu, opp)
    from dataclasses import replace
    mono = True
    for j in range(9):
        faster = list(lu); faster[j] = replace(lu[j], steal_spd=min(5, lu[j].sspd + 1))
        slower = list(lu); slower[j] = replace(lu[j], steal_spd=max(1, lu[j].sspd - 1))
        m = o.OffenseModel(catcher_mix={5: 9, 3: 1})
        mono &= m.expected_runs(faster, opp) >= m.expected_runs(lu, opp) - 1e-9 >= m.expected_runs(slower, opp) - 2e-9
    bad5 += (not ok) or (not mono)
    print(f"5. opt {v['opt']:.4f} >= table {v['re']:.4f}, win-prob {v['wp']:.4f}: {ok}; steal speed monotone: {mono}")
mm = o.OffenseModel(catcher_mix={5: 9, 4: 1, 2: 1})
lu = rlu(); opp = rpit()
ex = mm.expected_runs(lu, opp)
mc, se = mm.simulate(lu, opp, games=36000, seed=5)
dd = mm.simulate_detailed(lu, opp, games=24000, seed=5)
z1, z2 = (mc - ex) / se, (dd["mean"] - ex) / dd["se"]
print(f"6. catcher mix: exact {ex:.4f}  simulate z={z1:+.2f}  detailed z={z2:+.2f}")
sys.exit(1 if bad or fails or bad5 or abs(z1) > 3.5 or abs(z2) > 3.5 else 0)

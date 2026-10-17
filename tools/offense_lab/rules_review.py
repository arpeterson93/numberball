"""Cross-check the written rulebook / results chart against import_BRC.csv and the calculator port."""
import sys, os, random, itertools
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import offense_sim as o
import sheet_calculator as sc

brc = o.load_brc()
CHART = {  # valid BRC per result, from the Results chart
 "HR": "01234567", "3B": "01234567", "2B": "01234567", "2BWH": "1457", "1B": "01234567", "1BWH": "124567",
 "1BWH2": "47", "IF1B": "01234567", "BB": "01234567", "GO": "01234567", "GORA": "1234567", "DP": "15",
 "DP21": "4567", "DP31": "47", "DPH1": "7", "DPRun": "5", "FC": "145", "FC3rd": "4", "FCH": "57", "FO": "01234567",
 "DFO": "24", "SacF": "34567", "DSacF": "67", "LODP": "2356", "TP": "47", "K": "01234567", "PO": "01234567",
 "B1B": "01234567", "B1BWH": "124567", "BGO": "0", "BFC": "1234567", "BDP": "123457", "SacB": "1234567"}
print("== 1. import_BRC.csv rows vs chart 'valid BRC' ==")
by = {}
for (outs, b, res), v in brc.items():
    by.setdefault(res, set()).add(b)
for res, exp in CHART.items():
    have = "".join(str(x) for x in sorted(by.get(res, [])))
    if have != exp:
        print(f"  {res:6s} chart={exp:9s} csv={have}")

print("\n== 2. what the calculator can actually produce, by state (random builds, all outs, swing + bunt) ==")
calc = sc.SheetCalculator(sc.load_params_json(ROOT / "sheet_calculator_params.json"))
rnd = random.Random(3)
seen = {}      # (kind,res) -> {brc: set(outs)}
codes = {(0, 0, 0): 0, (1, 0, 0): 1, (0, 1, 0): 2, (0, 0, 1): 3, (1, 1, 0): 4, (1, 0, 1): 5, (0, 1, 1): 6, (1, 1, 1): 7}
for _ in range(1500):
    occ = rnd.choice(list(codes)); outs = rnd.randint(0, 2)
    sp = tuple(rnd.randint(1, 5) if x else None for x in occ)
    kw = dict(pit_hand=rnd.choice("LR"), pit_mov=rnd.randint(1, 5), pit_cmd=rnd.randint(1, 5), pit_vel=rnd.randint(1, 5),
              pit_awr=rnd.randint(1, 5), bat_hand=rnd.choice("LRS"), bat_con=rnd.randint(1, 5), bat_eye=rnd.randint(1, 5),
              bat_pow=rnd.randint(1, 5), bat_spd=rnd.randint(1, 5), runner_speeds=sp, outs=outs,
              hit_and_run=rnd.random() < 0.3, infield_in=rnd.random() < 0.3)
    r = calc.compute(**kw)
    for kind, key in (("swing", "swing_ranges"), ("bunt", "bunt_ranges")):
        for name, w, lo, hi in r[key]:
            if w > 0:
                seen.setdefault(name, {}).setdefault(codes[occ], set()).add(outs)
for res in sorted(seen):
    have = "".join(str(x) for x in sorted(seen[res]))
    exp = CHART.get(res)
    flag = "" if exp is None or set(have) <= set(exp) else f"   <-- produced in states the chart does not list: {''.join(sorted(set(have)-set(exp)))}"
    miss = "" if exp is None or set(exp) <= set(have) else f"   (chart lists but never produced: {''.join(sorted(set(exp)-set(have)))})"
    outs_by = {b: sorted(v) for b, v in seen[res].items()}
    print(f"  {res:6s} states={have:9s} chart={str(exp):9s}{flag}{miss}")
    if res in ("DP", "DP21", "DP31", "DPH1", "DPRun", "TP", "LODP", "DFO", "SacF", "DSacF", "GORA", "FC", "FC3rd", "FCH", "BDP"):
        if any(2 in v for v in seen[res].values()):
            print(f"         ^ produced with 2 outs in states {[b for b, v in seen[res].items() if 2 in v]}")

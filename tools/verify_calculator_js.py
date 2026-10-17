"""Check docs/js/sheet_calculator.js against sheet_calculator.py (which tools/verify_sheet_calculator.py checks
against the sheet itself).  Serves docs/ locally and drives the calculator page headless.

    python tools/verify_calculator_js.py [n_scenarios] [seed]
"""
import json, random, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sheet_calculator as sc
from playwright.sync_api import sync_playwright

n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 3
params = json.loads((ROOT / "docs/data/calculator_params.json").read_text(encoding="utf-8"))
calc = sc.SheetCalculator(params)
rng = random.Random(seed)
pats = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1)]
cases = []
for _ in range(n):
    cases.append(dict(
        pit_hand=rng.choice("RL"), pit_mov=rng.randint(1, 5), pit_cmd=rng.randint(1, 5), pit_vel=rng.randint(1, 5),
        pit_awr=rng.randint(1, 5), bat_hand=rng.choice("RLS"), bat_con=rng.randint(1, 5), bat_eye=rng.randint(1, 5),
        bat_pow=rng.randint(1, 5), bat_spd=rng.randint(1, 5),
        runner_speeds=[rng.randint(1, 5) if q else None for q in rng.choice(pats)], outs=rng.randint(0, 2),
        hit_and_run=rng.random() < 0.25, infield_in=rng.random() < 0.3,
        dynamic=rng.random() < 0.2, include_lotp=rng.random() < 0.1))
srv = subprocess.Popen([sys.executable, "-m", "http.server", "8766", "--directory", str(ROOT / "docs")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
try:
    with sync_playwright() as p:
        pg = p.chromium.launch().new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://localhost:8766/tools/range_calculator.html")
        pg.wait_for_function("typeof S !== 'undefined' && S.params && S.params.A11")
        js = pg.evaluate("""cases => { const c = new SheetCalculator(S.params);
            return cases.map(o => { const r = c.compute(o); return {s: r.swing_ranges, b: r.bunt_ranges}; }); }""", cases)
        steals = pg.evaluate("""() => new SheetCalculator(S.params).compute({pit_hand:'R',pit_mov:3,pit_cmd:3,pit_vel:3,pit_awr:3,
            bat_hand:'R',bat_con:3,bat_eye:3,bat_pow:3,bat_spd:3,runner_speeds:[2,4,5],outs:0,catcher_eye:3}).steals""")
        print("page errors:", errs)
finally:
    srv.terminate()
bad = 0
for case, got in zip(cases, js):
    kw = dict(case, runner_speeds=tuple(case["runner_speeds"]))
    exp = calc.compute(**kw)
    e_s = [list(map(int, (a[1], a[2], a[3]))) + [a[0]] for a in exp["swing_ranges"]]
    g_s = [[a[1], a[2], a[3], a[0]] for a in got["s"]]
    e_b = [list(map(int, (a[1], a[2], a[3]))) + [a[0]] for a in exp["bunt_ranges"]]
    g_b = [[a[1], a[2], a[3], a[0]] for a in got["b"]]
    if e_s != g_s or e_b != g_b:
        bad += 1
        if bad <= 3:
            print("MISMATCH", case, "\n py", e_s, "\n js", g_s)
print(f"{n - bad}/{n} scenarios identical between sheet_calculator.py and sheet_calculator.js")
print("steals sample:", steals)

import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import offense_sim as o
brc = o.load_brc()
NAME = {0: "OUT", 1: "1B", 2: "2B", 3: "3B", 4: "HOME", -1: "-"}
OCC = {0: "", 1: "1", 2: "2", 3: "3", 4: "1,2", 5: "1,3", 6: "2,3", 7: "1,2,3"}
def show(res, outs):
    out = []
    for b in range(8):
        r = brc.get((outs, b, res))
        if not r: continue
        bt, rt, runs, eo = r
        parts = [f"B:{NAME[bt]}"]
        for base, t in zip("123", rt):
            if str(base) in OCC[b].split(","):
                parts.append(f"r{base}:{NAME[t]}")
        out.append(f"   {b}({OCC[b] or 'empty'}): " + " ".join(parts) + f"  runs={runs} outs->{eo}")
    return out
for res in sys.argv[1:]:
    print(f"## {res}")
    base0 = show(res, 0)
    for l in base0: print("  o0" + l)
    for outs in (1, 2):
        cur = show(res, outs)
        diff = [l for l in cur if l.split("outs->")[0].strip() not in [x.split("outs->")[0].strip() for x in base0] or True]
        # only print rows that differ from the 0-out row for the same state, ignoring the ending-outs number
        strip = lambda l: l.split("outs->")[0].strip()
        d0 = {strip(x).split(":")[0]: strip(x) for x in base0}
        for l in cur:
            k = strip(l).split(":")[0]
            if d0.get(k) != strip(l):
                print(f"  o{outs}" + l + "   <-- differs from 0 outs")

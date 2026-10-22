"""Export the calculator-model tables the website needs to a small committed file.

    python tools/diff_lab/export_model.py

Reads cache/model.pkl (build_model.py: result mix at every diff, stacked from the calculator ranges of every
historical swing) and cache/state_meta.pkl (build_state.py: run expectancy and runner-advancement tables) and writes
tools/diff_lab/model.npz, which diff_metrics.py uses (locally and in CI) to give every swing its expected runs and
expected WPA. Re-run only when the model itself is rebuilt.
"""
import pickle
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
M = pickle.load(open(CACHE / "state_meta.pkl", "rb"))
MD = pickle.load(open(CACHE / "model.pkl", "rb"))
TR, RE, cell, GC = M["TR"], M["RE"], M["cell"], M["GC"]


def trans(outs, brc, cls):
    """(runs, outs after, base state after) for a result from a state: the exact table row, else the most common
    observed transition."""
    name = GC[cls]
    t = TR.get((outs, brc, name))
    if t:
        return t[0], t[1], t[2]
    c = cell.get((outs * 8 + brc, name))
    if c:
        (runs, eo, br), _ = c.most_common(1)[0]
        return runs, min(3, outs + eo), br
    return None


TRANS = np.full((24, len(GC), 3), -1, dtype=np.int8)
RVT = np.full((24, len(GC)), np.nan, dtype=np.float32)
RE24 = np.zeros((3, 8), dtype=np.float32)
for (o, b), v in RE.items():
    RE24[o, b] = v
for s in range(24):
    o, b = divmod(s, 8)
    for c in range(len(GC)):
        t = trans(o, b, c)
        if not t:
            continue
        runs, oa, br = t
        TRANS[s, c] = (runs, oa, br)
        RVT[s, c] = (RE[(oa, br)] if oa < 3 else 0.0) + runs - RE[(o, b)]
np.savez_compressed(HERE / "model.npz", PN=MD["PN"].astype(np.float16), P8=MD["P8"].astype(np.float32),
                    TRANS=TRANS, RVT=RVT, RE=RE24, GC=np.array(GC))
print("wrote", HERE / "model.npz", (HERE / "model.npz").stat().st_size // 1024, "KB")

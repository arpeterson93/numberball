"""Studies built on offense_sim: credits, single-player build scans, batting-order and roster search."""
import sys, itertools, random
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataclasses import replace
import numpy as np
import offense_sim as o


def builds_sum(total: int = 12, lo: int = 1, hi: int = 5):
    """Every (con, eye, pwr, spd) with each stat in lo..hi summing to `total`."""
    return [b for b in itertools.product(range(lo, hi + 1), repeat=4) if sum(b) == total]


def with_build(h: o.Hitter, b) -> o.Hitter:
    return replace(h, con=b[0], eye=b[1], pwr=b[2], spd=b[3], run_spd=None)


def credits(model: o.OffenseModel, lineup: list[o.Hitter], opp, avg: int = 3) -> list[dict]:
    """Per-hitter runs per game relative to an average replacement, split by where the value comes from.

    total      : vs replacing him with a 3/3/3/3 hitter (his handedness kept)
    running    : vs giving him baseline running speed (steals and every teammate's ranges while he is on base)
    plate_spd  : vs giving him baseline speed at the plate only (his own 3B/2B/IF1B widths)
    plate_other: contact + eye + power at the plate
    """
    base = model.expected_runs(lineup, opp)
    out = []
    for j, h in enumerate(lineup):
        def swap(**kw):
            lu = list(lineup); lu[j] = replace(h, **kw); return model.expected_runs(lu, opp)
        avg_h = swap(con=avg, eye=avg, pwr=avg, spd=avg, run_spd=avg)
        run_flat = swap(run_spd=avg)                          # own speed as a runner removed
        plate_spd_flat = swap(spd=avg, run_spd=h.rspd)        # own speed at the plate removed, runner speed kept
        plate_flat = swap(con=avg, eye=avg, pwr=avg, spd=avg, run_spd=h.rspd)   # all plate skill removed
        out.append(dict(name=h.name, slot=j + 1, build=f"{h.con}/{h.eye}/{h.pwr}/{h.spd}",
                        total=base - avg_h, running=base - run_flat, plate_spd=base - plate_spd_flat,
                        plate_all=base - plate_flat))
    return out


def scan_player(model, lineup, idx: int, opp, builds=None, hold_run_spd_at_plate=True):
    """Expected team runs/game for every build of the player at `idx`, everyone else fixed."""
    builds = builds_sum() if builds is None else builds
    rows = []
    for b in builds:
        lu = list(lineup); lu[idx] = with_build(lineup[idx], b)
        rows.append((b, model.expected_runs(lu, opp)))
    rows.sort(key=lambda x: -x[1])
    return rows


def hill_climb_order(model, players: list[o.Hitter], opp, start=None, restarts: int = 3, seed: int = 0):
    """Best batting order for exactly these 9 hitters (swap hill-climbing from several starts)."""
    rng = random.Random(seed)
    best_order, best_val = None, -1.0
    starts = [list(range(9)) if start is None else list(start)]
    for _ in range(restarts - 1):
        s = list(range(9)); rng.shuffle(s); starts.append(s)
    cache = {}

    def val(order):
        k = tuple(order)
        if k not in cache:
            cache[k] = model.expected_runs([players[i] for i in order], opp)
        return cache[k]
    for s in starts:
        cur, cv = list(s), val(s)
        improved = True
        while improved:
            improved = False
            for a, b in itertools.combinations(range(9), 2):
                t = list(cur); t[a], t[b] = t[b], t[a]
                v = val(t)
                if v > cv + 1e-9:
                    cur, cv, improved = t, v, True
        if cv > best_val:
            best_order, best_val = cur, cv
    worst = min(cache.values())
    return best_order, best_val, worst, len(cache)

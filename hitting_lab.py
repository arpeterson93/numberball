"""Hitting Lab logic (no Streamlit): studies built on offense_sim.

  * credits       - what each hitter is worth, split into plate and running value
  * scan_player   - every legal build for one hitter with everyone else fixed
  * hill_climb_order - best batting order for nine hitters
  * best_nine     - which nine of a roster to play (optionally requiring a legal defense) and in what order
  * pitcher_mix   - league pitcher builds weighted by batters faced
"""
from __future__ import annotations

import itertools
import random
from dataclasses import replace

import offense_sim as o

INFIELD = ("1B", "2B", "3B", "SS")
OUTFIELD = ("LF", "CF", "RF")
DEFENSE = ("C",) + INFIELD + OUTFIELD


# ----------------------------------------------------------------------------------------------- legality
def can_play(prim: str, sec, pos: str) -> bool:
    """Position eligibility from rulebook section 8."""
    if prim == pos:
        return True
    sec = sec or ""
    if prim in INFIELD:
        if sec in ("UTIL", "INF") and pos in INFIELD:
            return True
        if sec == "C" and pos == "C":
            return True
    if prim == "C":
        if sec in ("CIF", "CINF") and pos in ("1B", "3B"):
            return True
        if sec == "COF" and pos in ("LF", "RF"):
            return True
    if prim in OUTFIELD and pos in OUTFIELD:
        return True
    return False


def legal_defense(players: list[tuple]) -> bool:
    """players = [(primary_pos, secondary_pos), ...] for nine hitters. True when eight of them can fill
    C, 1B, 2B, 3B, SS, LF, CF, RF (the ninth DHs) with at least five at their primary position (rulebook 8.5.2)."""
    n = len(players)
    found = [False]

    def rec(pi, used, prim_count):
        if found[0]:
            return
        if pi == len(DEFENSE):
            found[0] = prim_count >= 5
            return
        pos = DEFENSE[pi]
        for i in range(n):
            if i in used or not can_play(players[i][0], players[i][1], pos):
                continue
            rec(pi + 1, used | {i}, prim_count + (players[i][0] == pos))
            if found[0]:
                return
    rec(0, frozenset(), 0)
    return found[0]


# ----------------------------------------------------------------------------------------------- builds
def builds_sum(total: int = 12, lo: int = 1, hi: int = 5):
    """Every (con, eye, pwr, spd) with each stat in lo..hi summing to `total`."""
    return [b for b in itertools.product(range(lo, hi + 1), repeat=4) if sum(b) == total]


def with_build(h: o.Hitter, b) -> o.Hitter:
    return replace(h, con=b[0], eye=b[1], pwr=b[2], spd=b[3], run_spd=None)


def pitcher_mix(counts: dict, builds: dict, top: int = 20) -> o.PitcherMix:
    """counts: {pitcher_id: batters faced}; builds: {pitcher_id: (hand, mov, cmd, vel, awr)}. Same-build pitchers
    are pooled, the `top` most-faced builds kept and renormalised."""
    acc: dict = {}
    for pid, n in counts.items():
        b = builds.get(pid)
        if b:
            acc[b] = acc.get(b, 0) + n
    best = sorted(acc.items(), key=lambda x: -x[1])[:top]
    return o.PitcherMix.of([(w, o.Pitcher(*k)) for k, w in best])


# ----------------------------------------------------------------------------------------------- studies
def _credit_row(model: o.OffenseModel, lineup: list[o.Hitter], j: int, opp, ref: tuple, base: float,
                tick=None) -> dict:
    """The six counterfactual solves for the hitter in slot j (see credits). tick() is called after each solve."""
    con, eye, pwr, spd = ref
    h = lineup[j]

    def swap(**kw):
        lu = list(lineup); lu[j] = replace(h, **kw)
        v = model.expected_runs(lu, opp)
        if tick:
            tick()
        return v
    own = h.rspd
    avg_h = swap(con=con, eye=eye, pwr=pwr, spd=spd, run_spd=spd, steal_spd=spd)
    run_flat = swap(run_spd=spd, steal_spd=spd)
    steal_flat = swap(run_spd=own, steal_spd=spd)
    range_flat = swap(run_spd=spd, steal_spd=h.sspd)
    plate_spd_flat = swap(spd=spd, run_spd=own, steal_spd=h.sspd)
    plate_flat = swap(con=con, eye=eye, pwr=pwr, spd=spd, run_spd=own, steal_spd=h.sspd)
    return dict(name=h.name, slot=j + 1, build=f"{h.con}/{h.eye}/{h.pwr}/{h.spd}",
                total=base - avg_h, running=base - run_flat, steals=base - steal_flat,
                baserunning=base - range_flat, plate_spd=base - plate_spd_flat, plate_all=base - plate_flat)


def credits(model: o.OffenseModel, lineup: list[o.Hitter], opp, ref: tuple = (3, 3, 3, 3), progress=None) -> list[dict]:
    """Per-hitter runs per game relative to a reference hitter, split by where the value comes from.

    Every number is a marginal counterfactual: the same lineup solved again with ONE thing about ONE hitter
    changed to the reference, everyone else left as they are. The calculator only looks at specific runners (the
    lead runner of 1st/2nd for well-hit singles and doubles, the runner on a given base or the average of several
    for ground-out advancement), and the solver carries each runner's identity through every state, so a runner
    who isn't the one the calculator reads in a state earns nothing there.

    ref = (con, eye, pwr, spd) of the reference hitter; a speed of ref[3] is also the reference runner speed.

    total       : vs replacing him with the reference hitter (handedness kept)
    plate_all   : his CON/EYE/PWR/SPD at the plate, vs the reference at the plate
    plate_spd   : just his SPD at the plate (3B/2B/IF1B widths), vs the reference speed
    steals      : his speed in steal attempts only, vs the reference speed
    baserunning : his speed as a runner in the calculator (teammates' well-hit and ground-out ranges)
    running     : steals and baserunning together
    pa          : his expected plate appearances per game in this lineup
    """
    total = 2 + 6 * len(lineup)
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(done[0] / total)
    base = model.expected_runs(lineup, opp); tick()
    pa = model.expected_pa(lineup, opp); tick()
    out = []
    for j in range(len(lineup)):
        row = _credit_row(model, lineup, j, opp, ref, base, tick)
        row["pa"] = float(pa[j])
        out.append(row)
    return out


def credits_neutral(model: o.OffenseModel, lineup: list[o.Hitter], opp, ref: tuple = (3, 3, 3, 3), slot: int = 4,
                    progress=None) -> list[dict]:
    """Order- and teammate-independent player value: each hitter is measured alone in a lineup of eight reference
    hitters (batting in `slot`, 0-based), so the number reflects his own attributes and nothing about who bats
    around him. 'pa' is his plate appearances per game in that spot, for per-plate-appearance rates."""
    out = []
    total = 8 * len(lineup)
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(done[0] / total)
    for j, h in enumerate(lineup):
        ref_h = lambda i: o.Hitter(f"ref{i}", "R", ref[0], ref[1], ref[2], ref[3])
        lu = [ref_h(i) for i in range(len(lineup))]
        lu[slot] = h
        base = model.expected_runs(lu, opp); tick()
        row = _credit_row(model, lu, slot, opp, ref, base, tick)
        row["slot"] = j + 1
        row["pa"] = float(model.expected_pa(lu, opp)[slot]); tick()
        out.append(row)
    return out


def simulate_neutral(model: o.OffenseModel, lineup: list[o.Hitter], opp, games: int = 10000, seed: int = 1,
                     ref: tuple = (3, 3, 3, 3), slot: int = 4, pitcher_per_game: bool = False, progress=None) -> list[dict]:
    """Simulated runs added for each hitter on his own: he bats in `slot` (0-based) of a lineup of eight reference
    hitters, so nothing about batting order or teammates leaks in. Tallies are per game for that hitter;
    divide by his 'pa' to get per-plate-appearance rates."""
    rows = []
    for j, h in enumerate(lineup):
        lu = [o.Hitter(f"ref{i}", "R", ref[0], ref[1], ref[2], ref[3]) for i in range(len(lineup))]
        lu[slot] = h
        sub = None if not progress else (lambda f, j=j: progress((j + f) / len(lineup)))
        sim = model.simulate_detailed(lu, opp, games=games, seed=seed + j, log_games=0, baseline=ref[3],
                                      pitcher_per_game=pitcher_per_game, progress=sub)
        t = dict(sim["per_game"][slot])
        t.update(name=h.name, slot=j + 1, build=f"{h.con}/{h.eye}/{h.pwr}/{h.spd}")
        rows.append(t)
    if progress:
        progress(1.0)
    return rows


def scan_player(model, lineup, idx: int, opp, builds=None, progress=None):
    """[(build, team runs per game)] for every build of the hitter at `idx`, everyone else fixed, best first."""
    builds = builds_sum() if builds is None else builds
    rows = []
    for n, b in enumerate(builds, start=1):
        lu = list(lineup); lu[idx] = with_build(lineup[idx], b)
        rows.append((b, model.expected_runs(lu, opp)))
        if progress:
            progress(n / len(builds))
    rows.sort(key=lambda x: -x[1])
    return rows


def hill_climb_order(model, players: list[o.Hitter], opp, start=None, restarts: int = 3, seed: int = 0,
                     progress=None):
    """Best batting order for exactly these nine hitters (swap hill-climbing). Returns
    (order as indices into `players`, best runs, worst runs seen, orders tried)."""
    rng = random.Random(seed)
    starts = [list(range(len(players))) if start is None else list(start)]
    for _ in range(restarts - 1):
        s = list(range(len(players))); rng.shuffle(s); starts.append(s)
    cache = {}

    def val(order):
        k = tuple(order)
        if k not in cache:
            cache[k] = model.expected_runs([players[i] for i in order], opp)
            if progress:
                progress(len(cache))
        return cache[k]
    best_order, best_val = None, -1.0
    for s in starts:
        cur, cv = list(s), val(s)
        improved = True
        while improved:
            improved = False
            for a, b in itertools.combinations(range(len(players)), 2):
                t = list(cur); t[a], t[b] = t[b], t[a]
                v = val(t)
                if v > cv + 1e-9:
                    cur, cv, improved = t, v, True
        if cv > best_val:
            best_order, best_val = cur, cv
    return best_order, best_val, min(cache.values()), len(cache)


def best_nine(model, pool: list[dict], opp, require_legal: bool = True, top_k: int = 3, max_pool: int = 13,
              progress=None):
    """Choose which nine of `pool` to play and their order.

    pool: dicts with keys hitter (o.Hitter), prim, sec. Stage 1 scores every nine in pool order; stage 2 optimises
    the order of the best `top_k`. Pools larger than max_pool are first cut to the max_pool best individual hitters
    (by runs gained over a 3/3/3/3 hitter in the pool's own first nine).
    Returns a list of (runs, [pool indices in batting order]) best first, plus a note string."""
    note = ""
    idx = list(range(len(pool)))
    if len(pool) > max_pool:
        base_nine = [pool[i]["hitter"] for i in idx[:9]]
        base = model.expected_runs(base_nine, opp)
        gain = {}
        for i in idx:
            lu = list(base_nine)
            lu[8] = pool[i]["hitter"]
            gain[i] = model.expected_runs(lu, opp) - base
        idx = sorted(sorted(idx, key=lambda i: -gain[i])[:max_pool])
        note = f"Pool of {len(pool)} cut to the {max_pool} strongest individual bats before searching."
    scored = []
    combos = [c for c in itertools.combinations(idx, 9)
              if not require_legal or legal_defense([(pool[i]["prim"], pool[i]["sec"]) for i in c])]
    for n, combo in enumerate(combos, start=1):
        scored.append((model.expected_runs([pool[i]["hitter"] for i in combo], opp), combo))
        if progress and n % 3 == 0:
            progress(0.8 * n / len(combos), f"Scoring nine {n} of {len(combos)}")
    scored.sort(key=lambda x: -x[0])
    out = []
    for r, (_, combo) in enumerate(scored[:top_k]):
        players = [pool[i]["hitter"] for i in combo]
        sub = None if not progress else (lambda tried, r=r: progress(0.8 + 0.2 * (r + min(tried / 120.0, 0.99)) / top_k,
                                                                      f"Ordering finalist {r + 1} of {top_k}"))
        order, val, _, _ = hill_climb_order(model, players, opp, restarts=2, progress=sub)
        out.append((val, [combo[k] for k in order]))
    if progress:
        progress(1.0, "Done")
    out.sort(key=lambda x: -x[0])
    return out, note, len(scored)

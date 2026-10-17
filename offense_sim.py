"""Offense simulation engine for the Numberball hitting lab.

Everything is built on the exact port of the stadium sheet (sheet_calculator.py):

  * Plate appearance: SheetCalculator.compute -> swing result ranges for (pitcher, batter, outs, runner speeds).
    A result's probability is the share of the diff distribution inside its range. The default distribution is two
    independent uniform numbers on the 1000-ring (P(0)=P(500)=1/1000, otherwise 2/1000); an empirical one can be
    passed instead.
  * Baserunning: import_BRC.csv lists, for every (outs, base state, result), where the batter and each runner end
    up, how many runs score and the ending out count (checked against 9,945 real S12-13 swings). Nothing is
    inferred from baseball logic.
  * Steals: safe iff the steal diff <= table[(runner SPD - (pitcher AWR + catcher EYE) / 2)] (checked on S12-13
    plays), so P(safe) = P(diff <= range). With runners on 1st and 2nd the attempt is a double steal decided by
    the runner on 2nd; the trail runner moves up either way.
  * Strategy calls (steal, hit and run): only the offense is simulated, so by default (decision='opt') every call is
    the one that maximises expected runs for THIS lineup, found by policy iteration on the chain's own values.
    decision='re' instead values each option's resulting states at runs plus the league run expectancy table
    (run_expectancy_distribution.csv); decision='wp' uses the win-probability table in a game tied entering the
    inning.
  * Hit and run: allowed with a runner on 1st and/or 2nd (not 3rd alone or bases loaded) and fewer than 2 outs. The
    swing uses the calculator's hit_and_run ranges (batter CON -1, runners SPD +1); a strikeout then triggers the
    steal attempt at the runners' UNADJUSTED speed (safe: the K and then a normal steal result at outs+1; caught:
    the KCS row). It is called when its one-step EV beats a normal swing's.
  * Innings: an inning is an absorbing Markov chain over (current batter, outs, who is on which base). It is solved
    exactly, so there is no sampling noise, and the batting order carries over between innings.
  * Opponent: a single Pitcher, or a PitcherMix (every plate appearance faces a pitcher drawn from the mix).

Not modeled: bunts, infield in, intentional walks, autos, extra innings, the other team's offense.
Runner speed used on the bases (Hitter.run_spd) can differ from the speed used at the plate (Hitter.spd), which
is how a runner's value is separated from his hitting value.
"""
from __future__ import annotations

import copy
import csv
import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix, identity
from scipy.sparse.linalg import splu

import sheet_calculator as sc

ROOT = Path(__file__).resolve().parent
ENGINE_VERSION = 6                # bump when results change shape or meaning (the GM Tools page drops stale cached results)
PA_SPAN = 501                      # diff takes the values 0..500
HNR_BRC = (1, 2, 4, 5)             # base states a hit and run may be called in (runner on 1st and/or 2nd, 3rd holds)
LINEUP = 9

STEAL_DIFFS = [-5, -4.5, -4, -3.5, -3, -2.5, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]
STEAL_TABLE = {
    "2nd": [62, 86, 108, 132, 154, 177, 199, 221, 242, 265, 285, 308, 329, 351, 373, 396, 418, 442, 464, 488, 499],
    "3rd": [7, 19, 32, 48, 61, 76, 87, 100, 110, 120, 130, 140, 150, 163, 174, 189, 202, 218, 233, 251, 259],
}


@dataclass(frozen=True)
class Hitter:
    name: str
    hand: str
    con: int
    eye: int
    pwr: int
    spd: int
    run_spd: int | None = None        # speed used as a RUNNER in the calculator (teammates' ranges); default = spd
    steal_spd: int | None = None      # speed used in steal attempts; default = the runner speed above

    @property
    def rspd(self) -> int:
        return self.spd if self.run_spd is None else self.run_spd

    @property
    def sspd(self) -> int:
        return self.rspd if self.steal_spd is None else self.steal_spd

    def build(self) -> tuple:
        return (self.hand, self.con, self.eye, self.pwr, self.spd)


@dataclass(frozen=True)
class Pitcher:
    hand: str
    mov: int
    cmd: int
    vel: int
    awr: int
    name: str = ""

    def key(self) -> tuple:
        return (self.hand, self.mov, self.cmd, self.vel, self.awr)

    def parts(self):
        return ((1.0, self),)


@dataclass(frozen=True)
class PitcherMix:
    """Every plate appearance faces a pitcher drawn from `items` = ((weight, Pitcher), ...)."""
    items: tuple

    @staticmethod
    def of(pairs) -> "PitcherMix":
        tot = float(sum(w for w, _ in pairs))
        return PitcherMix(tuple((w / tot, p) for w, p in pairs))

    def key(self) -> tuple:
        return tuple((w, p.key()) for w, p in self.items)

    def parts(self):
        return self.items


def ring_weights() -> list[float]:
    return [1.0] + [2.0] * (PA_SPAN - 2) + [1.0]


# ----------------------------------------------------------------------------------------------- baserunning data
_BRC_CODE = {(0, 0, 0): 0, (1, 0, 0): 1, (0, 1, 0): 2, (0, 0, 1): 3, (1, 1, 0): 4, (1, 0, 1): 5, (0, 1, 1): 6,
             (1, 1, 1): 7}
_TOKEN = {"0": 0, "b1": 1, "b2": 2, "b3": 3, "b4": 4}


def _tok(v) -> int:
    s = "" if v is None else str(v).strip()
    if s in ("", "nan", "NaN"):
        return -1
    if s in ("0", "0.0"):
        return 0
    return _TOKEN.get(s, -2)


def load_brc(path: Path = ROOT / "import_BRC.csv") -> dict:
    """(outs, brc, result) -> (batter_token, (r1, r2, r3 tokens), runs, ending_outs).
    Token: 0 out, 1/2/3 ends on that base, 4 scores, -1 not applicable."""
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            parts = (row["Situation"] or "").split("_", 2)
            if len(parts) != 3:
                continue
            try:
                outs, brc = int(parts[0]), int(parts[1])
                runs = int(float(row.get("Runs")))
                eo = int(float(row.get("eOuts")))
            except (TypeError, ValueError):
                continue
            out[(outs, brc, parts[2])] = (_tok(row.get("B")), (_tok(row.get("r1")), _tok(row.get("r2")),
                                                               _tok(row.get("r3"))), runs, eo)
    return out


def load_wp(path: Path = ROOT / "win_probability_table.csv") -> dict:
    wp = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            wp[(int(row["remaining"]), int(row["outs"]), str(row["obc"]).zfill(3), int(row["batting_lead"]))] = \
                float(row["win_prob"])
    return wp


_RE_LABEL = {"Empty": "000", "1B": "001", "2B": "010", "3B": "100", "1&2B": "011", "1&3B": "101", "2&3B": "110",
             "BL": "111"}


def load_re() -> dict:
    """(outs, obc) -> expected runs for the rest of the inning (league run expectancy). Uses the full-precision
    distribution file when present, else the rounded matrix."""
    brc_to_obc = {0: "000", 1: "001", 2: "010", 3: "100", 4: "011", 5: "101", 6: "110", 7: "111"}
    out: dict = {}
    dist = ROOT / "run_expectancy_distribution.csv"
    if dist.exists():
        with open(dist, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                k = (int(row["Outs"]), brc_to_obc[int(row["BRC"])])
                out[k] = out.get(k, 0.0) + int(row["runs_scored"]) * float(row["pct"])
    if len(out) < 24:
        with open(ROOT / "run_expectancy_matrix.csv", newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                out[(int(row["outs"]), _RE_LABEL[row["obc"]])] = float(row["expected_runs"])
    for obc in brc_to_obc.values():
        out[(3, obc)] = 0.0
    return out


def steal_safe_range(base: str, runner_spd: float, pit_awr: float, catcher_eye: float) -> int:
    aa = max(-5.0, min(5.0, runner_spd - (pit_awr + catcher_eye) / 2.0))
    return STEAL_TABLE[base][STEAL_DIFFS.index(aa)]


def _place(tokens, who) -> list:
    """Runner on 1st/2nd/3rd (lineup index or -1) after applying destination tokens to the people in `who`."""
    new = [-1, -1, -1]
    for t, w in zip(tokens, who):
        if w >= 0 and t in (1, 2, 3):
            new[t - 1] = w
    return new


def steal_names(occ):
    """(safe result, caught result, base) for the attempt a base state allows, else None. With runners on 1st and
    2nd it is a double steal decided by the runner on 2nd; with 1st (and maybe 3rd) the runner on 1st goes for 2nd
    and a runner on 3rd holds."""
    if occ[0] and occ[1] and not occ[2]:
        return ("SB32", "CS3", "3rd")
    if occ[0] and not occ[1]:
        return ("SB2", "CS2", "2nd")
    if occ[1] and not occ[2]:
        return ("SB3", "CS3", "3rd")
    return None


class Chain:
    """One lineup against one opponent: every reachable node with its swing transitions as numpy arrays."""

    def __init__(self):
        self.nodes: list = []
        self.index: dict = {}
        self.sw_src = self.sw_dst = self.sw_p = self.sw_runs = self.sw_lead = None
        self.hr_src = self.hr_dst = self.hr_p = self.hr_runs = self.hr_lead = None
        self.elig: list = []            # (node, occ, outs, speeds, steal-row info)
        self.hnr_nodes: list = []       # (node, occ, outs, speeds, ev_normal, ev_hnr)
        self.builds: list = []


# ----------------------------------------------------------------------------------------------- the model
class OffenseModel:
    def __init__(self, params: dict | None = None, *, innings: int = 6, catcher_eye: int = 3, lead: int = 0,
                 bottom: bool = False, steals: bool = True, hit_and_run: bool = True, baseline_spd: int = 3,
                 catcher_mix: dict | None = None, decision: str = "opt",
                 diff_weights: list[float] | None = None, brc: dict | None = None, wp: dict | None = None):
        if params is None:
            params = sc.load_params_json(ROOT / "sheet_calculator_params.json")
        self.calc = sc.SheetCalculator(params)
        self.brc = brc or load_brc()
        self.wp = wp or load_wp()
        self.innings = innings
        # opposing catcher EYE: one value, or {eye: weight} (a league / custom distribution) averaged in every steal
        if catcher_mix:
            tot = float(sum(catcher_mix.values()))
            self.catcher_dist = tuple((int(e), w / tot) for e, w in sorted(catcher_mix.items()))
        else:
            self.catcher_dist = ((int(catcher_eye), 1.0),)
        self.catcher_eye = catcher_eye
        self.lead = lead
        self.bottom = bottom
        self.steals = steals
        self.hnr = hit_and_run
        if decision not in ("opt", "re", "wp"):
            raise ValueError("decision must be 'opt' (run-maximising for this lineup), 're' (league run-expectancy "
                             "table) or 'wp' (win probability)")
        self.decision = decision
        self.re = load_re()
        self.baseline_spd = baseline_spd
        w = np.asarray(ring_weights() if diff_weights is None else diff_weights, dtype=float)
        if w.shape != (PA_SPAN,):
            raise ValueError("diff_weights needs 501 entries (diff 0..500)")
        self.diff_cdf = np.concatenate([[0.0], np.cumsum(w / w.sum())])
        self._pa_cache: dict = {}
        self._steal_cache: dict = {}
        self._hnr_cache: dict = {}

    # ---- plate appearance
    def _pa_single(self, batter: tuple, pit: tuple, outs: int, speeds: tuple, hnr: bool = False) -> tuple:
        key = (batter, pit, outs, speeds, hnr)
        r = self._pa_cache.get(key)
        if r is None:
            out = self.calc.compute(pit_hand=pit[0], pit_mov=pit[1], pit_cmd=pit[2], pit_vel=pit[3], pit_awr=pit[4],
                                    bat_hand=batter[0], bat_con=batter[1], bat_eye=batter[2], bat_pow=batter[3],
                                    bat_spd=batter[4], runner_speeds=speeds, outs=outs, hit_and_run=hnr)
            r = tuple((name, float(self.diff_cdf[hi + 1] - self.diff_cdf[lo]))
                      for name, width, lo, hi in out["swing_ranges"] if width > 0)
            self._pa_cache[key] = r
        return r

    def pa_outcomes(self, batter: tuple, opp, outs: int, speeds: tuple, hnr: bool = False) -> tuple:
        """((result, probability), ...) for one swing. batter=(hand,con,eye,pow,spd); opp = Pitcher or PitcherMix."""
        parts = opp.parts()
        if len(parts) == 1:
            return self._pa_single(batter, parts[0][1].key(), outs, speeds, hnr)
        key = (batter, opp.key(), outs, speeds, hnr)
        r = self._pa_cache.get(key)
        if r is None:
            acc: dict = {}
            for w, p in parts:
                for name, pr in self._pa_single(batter, p.key(), outs, speeds, hnr):
                    acc[name] = acc.get(name, 0.0) + w * pr
            r = tuple(acc.items())
            self._pa_cache[key] = r
        return r

    # ---- steals
    def steal_p(self, base: str, spd: int, opp) -> float:
        """P(safe), averaged over the opposing pitcher mix and the catcher EYE distribution (the win-probability
        test is linear in it)."""
        return sum(w * wc * float(self.diff_cdf[steal_safe_range(base, spd, p.awr, eye) + 1])
                   for w, p in opp.parts() for eye, wc in self.catcher_dist)

    def wp_after(self, rem: int, outs: int, obc: str, lead: int) -> float:
        """Batting team's win probability for a state, including the 3-out case (next half or game end)."""
        lead = max(-10, min(10, lead))
        if outs >= 3:
            nxt = rem - 1
            if nxt < 1:
                return 1.0 if lead > 0 else (0.0 if lead < 0 else 0.5)
            return 1.0 - self.wp[(nxt, 0, "000", max(-10, min(10, -lead)))]
        return self.wp[(max(1, min(12, rem)), outs, obc, lead)]

    @staticmethod
    def _obc(occ: tuple) -> str:
        return f"{int(occ[2])}{int(occ[1])}{int(occ[0])}"           # [3B, 2B, 1B]

    def _value(self, rem: int, outs: int, obc: str, runs: int) -> float:
        """What a state is worth to the offense for strategy calls: runs scored on the play plus the run
        expectancy of the resulting state ('re'), or the resulting win probability ('wp')."""
        if self.decision == "re":
            return runs + self.re[(min(outs, 3), obc)]
        return self.wp_after(rem, outs, obc, self.lead + runs)

    def _dk(self, rem: int) -> int:
        """Cache key for the inning: run expectancy doesn't depend on how many innings are left."""
        return rem if self.decision == "wp" else 0

    def _steal_end(self, outs: int, occ: tuple, res: str):
        brc = _BRC_CODE[tuple(int(x) for x in occ)]
        _, rt, _, eo = self.brc[(outs, brc, res)]
        new = [False, False, False]
        for t in rt:
            if t in (1, 2, 3):
                new[t - 1] = True
        return eo, tuple(new)

    def steal_decision(self, rem: int, outs: int, occ: tuple, opp_key, opp, speeds: tuple):
        """P(safe) when an attempt beats the value of the current state, else None."""
        key = (self._dk(rem), outs, occ, opp_key, speeds)
        if key in self._steal_cache:
            return self._steal_cache[key]
        dec = None
        names = steal_names(occ) if self.steals else None
        if names:
            sb, cs, base = names
            spd = speeds[1] if occ[1] else speeds[0]       # lead runner decides (runner on 2nd if there is one)
            p = self.steal_p(base, spd, opp)
            ok = self._steal_end(outs, occ, sb)
            bad = self._steal_end(outs, occ, cs)
            now = self._value(rem, outs, self._obc(occ), 0)
            ev = p * self._value(rem, ok[0], self._obc(ok[1]), 0) + \
                (1 - p) * self._value(rem, bad[0], self._obc(bad[1]), 0)
            if ev > now:
                dec = p
        self._steal_cache[key] = dec
        return dec

    # ---- swing outcomes
    @staticmethod
    def _obc_of(new) -> str:
        return f"{int(new[2] >= 0)}{int(new[1] >= 0)}{int(new[0] >= 0)}"           # [3B, 2B, 1B]

    def _transitions(self, b: int, outs: int, runners: tuple, builds: list, rsp: list, ssp: list, opp, hnr: bool) -> list:
        """Every outcome of one swing: (probability, next node or None when the inning ends, runs, next leadoff
        when it ends, outs after, base state after). With hnr=True the swing uses the hit-and-run ranges and a
        strikeout triggers the steal attempt."""
        occ = tuple(r >= 0 for r in runners)
        speeds = tuple(rsp[r] if r >= 0 else None for r in runners)
        brc = _BRC_CODE[tuple(int(x) for x in occ)]
        nxt = (b + 1) % LINEUP
        out = []
        for res, p in self.pa_outcomes(builds[b], opp, outs, speeds, hnr):
            if hnr and res == "K":
                sspeeds = tuple(ssp[r] if r >= 0 else None for r in runners)
                out.extend(self._k_then_steal(b, outs, runners, occ, sspeeds, brc, p, opp))
                continue
            row = self.brc.get((outs, brc, res))
            if row is None:
                raise KeyError(f"no baserunning row for {res} with {outs} outs, state {brc}")
            bt, rt, runs, eo = row
            if eo >= 3:
                out.append((p, None, runs, nxt, 3, "000"))
            else:
                new = _place((bt,) + rt, (b,) + runners)
                out.append((p, (nxt, eo, *new), runs, 0, eo, self._obc_of(new)))
        return out

    def _k_then_steal(self, b, outs, runners, occ, speeds, brc, p, opp) -> list:
        """A strikeout on a hit and run: the batter is out, then the lead runner's steal is resolved at his
        UNADJUSTED speed. Safe: the K and a normal steal result at outs+1. Caught: the KCS row."""
        sb, _, base = steal_names(occ)
        ps = self.steal_p(base, speeds[1] if occ[1] else speeds[0], opp)
        nxt = (b + 1) % LINEUP
        res = []
        _, rt, runs, eo = self.brc[(outs + 1, brc, sb)]
        new = _place(rt, runners)
        res.append((p * ps, (nxt, eo, *new), runs, 0, eo, self._obc_of(new)))
        _, rt, runs, eo = self.brc[(outs, brc, "KCS")]
        if eo >= 3:
            res.append((p * (1.0 - ps), None, runs, nxt, 3, "000"))
        else:
            new = _place(rt, runners)
            res.append((p * (1.0 - ps), (nxt, eo, *new), runs, 0, eo, self._obc_of(new)))
        return res

    def _hnr_eligible(self, outs: int, occ: tuple) -> bool:
        return self.hnr and outs < 2 and _BRC_CODE[tuple(int(x) for x in occ)] in HNR_BRC

    def _swing_ev(self, rem: int, trans: list) -> float:
        """One-step value of a swing: each resulting state valued by _value (runs + run expectancy, or win
        probability)."""
        return sum(t[0] * self._value(rem, t[4], t[5], t[2]) for t in trans)

    def hnr_better(self, rem: int, key: tuple, trans_n: list, trans_h: list) -> bool:
        """True when calling the hit and run has a higher EV than swinging normally."""
        k = (self._dk(rem),) + key
        r = self._hnr_cache.get(k)
        if r is None:
            r = self._swing_ev(rem, trans_h) > self._swing_ev(rem, trans_n) + 1e-12
            self._hnr_cache[k] = r
        return r

    # ---- the inning chain
    def _rem(self, inning: int) -> int:
        return max(1, 2 * (self.innings - inning + 1) - (1 if self.bottom else 0))

    def build_chain(self, lineup: list[Hitter], opp) -> Chain:
        builds = [h.build() for h in lineup]
        rsp = [h.rspd for h in lineup]
        ssp = [h.sspd for h in lineup]
        ch = Chain()
        ch.builds = builds
        nodes, index = ch.nodes, ch.index

        def add(nd):
            if nd not in index:
                index[nd] = len(nodes); nodes.append(nd)
            return index[nd]

        for b in range(LINEUP):
            add((b, 0, -1, -1, -1))
        src, dst, pp, runs_l, lead = [], [], [], [], []
        hsrc, hdst, hpp, hruns, hlead = [], [], [], [], []
        i = 0
        while i < len(nodes):
            b, outs, r1, r2, r3 = nodes[i]
            runners = (r1, r2, r3)
            occ = tuple(r >= 0 for r in runners)
            speeds = tuple(rsp[r] if r >= 0 else None for r in runners)
            sspeeds = tuple(ssp[r] if r >= 0 else None for r in runners)
            brc = _BRC_CODE[tuple(int(x) for x in occ)]
            trans = self._transitions(b, outs, runners, builds, rsp, ssp, opp, False)
            for p, nd, runs, ld, _, _ in trans:
                src.append(i); pp.append(p); runs_l.append(runs)
                if nd is None:
                    dst.append(-1); lead.append(ld)
                else:
                    dst.append(add(nd)); lead.append(0)
            if self._hnr_eligible(outs, occ):
                trans_h = self._transitions(b, outs, runners, builds, rsp, ssp, opp, True)
                for p, nd, runs, ld, _, _ in trans_h:
                    hsrc.append(i); hpp.append(p); hruns.append(runs)
                    if nd is None:
                        hdst.append(-1); hlead.append(ld)
                    else:
                        hdst.append(add(nd)); hlead.append(0)
                ch.hnr_nodes.append((i, occ, outs, (speeds, sspeeds), trans, trans_h))
            names = steal_names(occ) if self.steals else None
            if names:
                info = []
                for res in names[:2]:
                    bt, rt, runs, eo = self.brc[(outs, brc, res)]
                    if eo >= 3:
                        info.append((-1, runs, b))              # inning over: this batter leads off next time
                    else:
                        info.append((add((b, eo, *_place(rt, runners))), runs, 0))
                ch.elig.append((i, occ, outs, sspeeds, info))
            i += 1
        ch.sw_src = np.asarray(src); ch.sw_dst = np.asarray(dst); ch.sw_p = np.asarray(pp)
        ch.sw_runs = np.asarray(runs_l, dtype=float); ch.sw_lead = np.asarray(lead)
        ch.hr_src = np.asarray(hsrc, dtype=int); ch.hr_dst = np.asarray(hdst, dtype=int)
        ch.hr_p = np.asarray(hpp, dtype=float); ch.hr_runs = np.asarray(hruns, dtype=float)
        ch.hr_lead = np.asarray(hlead, dtype=int)
        return ch

    def _solve(self, ch: Chain, steal_p: dict, hnr_on):
        """Solve the inning chain for a given set of calls: steal_p maps a node to its steal-success probability
        where a steal is attempted, hnr_on flags the nodes where the hit and run is called. Returns V (expected
        runs from each node to the end of the inning) and A (probability the next inning leads off with each
        batter, from each node)."""
        n = len(ch.nodes)
        stealing = np.zeros(n, dtype=bool)
        s_rows, s_cols, s_vals, s_run_i, s_run_v, s_abs_i, s_abs_c, s_abs_v = [], [], [], [], [], [], [], []
        for i, occ, outs, sspeeds, info in ch.elig:
            p = steal_p.get(i)
            if p is None:
                continue
            stealing[i] = True
            for pw, (d, runs, ld) in ((p, info[0]), (1.0 - p, info[1])):
                s_run_i.append(i); s_run_v.append(pw * runs)
                if d < 0:
                    s_abs_i.append(i); s_abs_c.append(ld); s_abs_v.append(pw)
                else:
                    s_rows.append(i); s_cols.append(d); s_vals.append(pw)
        keep = ~(stealing | hnr_on)[ch.sw_src]
        src, dst, p = ch.sw_src[keep], ch.sw_dst[keep], ch.sw_p[keep]
        runs_arr, lead_arr = ch.sw_runs[keep], ch.sw_lead[keep]
        if hnr_on.any():
            hm = hnr_on[ch.hr_src]
            src = np.concatenate([src, ch.hr_src[hm]]); dst = np.concatenate([dst, ch.hr_dst[hm]])
            p = np.concatenate([p, ch.hr_p[hm]]); runs_arr = np.concatenate([runs_arr, ch.hr_runs[hm]])
            lead_arr = np.concatenate([lead_arr, ch.hr_lead[hm]])
        exp_runs = np.bincount(src, weights=p * runs_arr, minlength=n)
        if s_run_i:
            exp_runs += np.bincount(np.asarray(s_run_i), weights=np.asarray(s_run_v), minlength=n)
        absorb = np.zeros((n, LINEUP))
        end = dst < 0
        np.add.at(absorb, (src[end], lead_arr[end]), p[end])
        if s_abs_i:
            np.add.at(absorb, (np.asarray(s_abs_i), np.asarray(s_abs_c)), np.asarray(s_abs_v))
        mid = ~end
        rows = np.concatenate([src[mid], np.asarray(s_rows, dtype=int)])
        cols = np.concatenate([dst[mid], np.asarray(s_cols, dtype=int)])
        vals = np.concatenate([p[mid], np.asarray(s_vals, dtype=float)])
        Q = coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsc()
        lu = splu((identity(n, format="csc") - Q).tocsc())
        self._last_lu = lu
        return lu.solve(exp_runs), lu.solve(absorb)

    def _opt_solution(self, ch: Chain, opp, eps: float = 1e-9):
        """The run-maximising calls for this lineup, found by policy iteration on the chain itself: start with
        no steals or hit-and-runs, value every option with the current solution's own expected runs from each
        resulting state, switch each node to its best option, and repeat until nothing changes. Returns
        (V, A, steal_p, hnr_on); cached on the chain."""
        cached = getattr(ch, "opt", None)
        if cached is not None:
            return cached
        n = len(ch.nodes)
        sp = {}
        for i, occ, outs, sspeeds, info in ch.elig:
            _, _, base = steal_names(occ)
            sp[i] = self.steal_p(base, sspeeds[1] if occ[1] else sspeeds[0], opp)
        hnr_idx = np.asarray([t[0] for t in ch.hnr_nodes], dtype=int)
        steal_p: dict = {}
        hnr_on = np.zeros(n, dtype=bool)
        V = A = None
        for _ in range(20):
            V, A = self._solve(ch, steal_p, hnr_on)
            Vx = np.append(V, 0.0)                      # index -1 (inning over) is worth 0 more runs
            qn = np.bincount(ch.sw_src, weights=ch.sw_p * (ch.sw_runs + Vx[ch.sw_dst]), minlength=n)
            qh = np.full(n, -np.inf)
            if len(hnr_idx):
                qh_all = np.bincount(ch.hr_src, weights=ch.hr_p * (ch.hr_runs + Vx[ch.hr_dst]), minlength=n)
                qh[hnr_idx] = qh_all[hnr_idx]
            new_steal: dict = {}
            for i, occ, outs, sspeeds, info in ch.elig:
                (d1, r1, _), (d2, r2, _) = info
                p = sp[i]
                qs = p * (r1 + Vx[d1]) + (1.0 - p) * (r2 + Vx[d2])
                if qs > max(qn[i], qh[i]) + eps:
                    new_steal[i] = p
            new_hnr = np.zeros(n, dtype=bool)
            for i in hnr_idx:
                if i not in new_steal and qh[i] > qn[i] + eps:
                    new_hnr[i] = True
            if new_steal.keys() == steal_p.keys() and (new_hnr == hnr_on).all():
                break
            steal_p, hnr_on = new_steal, new_hnr
        ch.opt = (V, A, steal_p, hnr_on)
        self.last_hnr_nodes = int(hnr_on.sum())
        return ch.opt

    def inning_matrices(self, ch: Chain, opp, rem: int):
        """V: expected runs from each node to the end of the inning. A: probability the next inning leads off with
        each batter, from each node. Steal and hit-and-run calls follow self.decision."""
        steal_p, hnr_on = self._policy(ch, opp, rem)
        if self.decision == "opt":
            return self._opt_solution(ch, opp)[:2]
        self.last_hnr_nodes = int(hnr_on.sum())
        return self._solve(ch, steal_p, hnr_on)

    def _policy(self, ch: Chain, opp, rem: int):
        """(steal_p, hnr_on) for one inning under the current decision rule."""
        if self.decision == "opt":
            _, _, steal_p, hnr_on = self._opt_solution(ch, opp)
            return steal_p, hnr_on
        n = len(ch.nodes)
        okey = opp.key()
        steal_p: dict = {}
        for i, occ, outs, sspeeds, info in ch.elig:
            p = self.steal_decision(rem, outs, occ, okey, opp, sspeeds)
            if p is not None:
                steal_p[i] = p
        hnr_on = np.zeros(n, dtype=bool)
        for i, occ, outs, speeds, trans_n, trans_h in ch.hnr_nodes:
            if i not in steal_p and self.hnr_better(rem, (outs, occ, speeds, ch.builds[ch.nodes[i][0]], okey),
                                                    trans_n, trans_h):
                hnr_on[i] = True
        return steal_p, hnr_on

    def _expected_pa_one(self, lineup: list[Hitter], opp) -> np.ndarray:
        """Expected plate appearances per game for each lineup slot (against ONE catcher)."""
        ch = self.build_chain(lineup, opp)
        n = len(ch.nodes)
        batter = np.asarray([nd[0] for nd in ch.nodes])
        pi = np.zeros(LINEUP); pi[0] = 1.0
        pa = np.zeros(LINEUP)
        for k in range(1, self.innings + 1):
            steal_p, hnr_on = self._policy(ch, opp, self._rem(k))
            _, A = self._solve(ch, steal_p, hnr_on)
            R = np.zeros((n, LINEUP))
            for i in range(n):
                if i not in steal_p:                      # a node that steals first has not batted yet
                    R[i, batter[i]] = 1.0
            Vpa = self._last_lu.solve(R)
            pa += pi @ Vpa[:LINEUP]
            pi = pi @ A[:LINEUP]
        return pa

    def expected_pa(self, lineup: list[Hitter], opp) -> np.ndarray:
        """Expected plate appearances per game for each lineup slot, averaged over the catcher mix."""
        kids = self._catcher_models()
        return sum(w * k._expected_pa_one(lineup, opp) for w, k in kids)

    # ---- game level
    def _expected_runs_one(self, lineup: list[Hitter], opp, *, detail: bool = False):
        """Expected runs over the game (all innings, batting order carried over) against ONE catcher."""
        ch = self.build_chain(lineup, opp)
        pi = np.zeros(LINEUP); pi[0] = 1.0
        per_inning = np.zeros(self.innings)
        for k in range(1, self.innings + 1):
            V, A = self.inning_matrices(ch, opp, self._rem(k))
            per_inning[k - 1] = float(pi @ V[:LINEUP])
            pi = pi @ A[:LINEUP]
        return (float(per_inning.sum()), per_inning) if detail else float(per_inning.sum())

    # ---- independent Monte Carlo (cross-check for the exact solver)
    def _simulate_one(self, lineup: list[Hitter], opp, games: int = 20000, seed: int = 1) -> tuple[float, float]:
        """Plain event-by-event simulation; returns (mean runs per game, standard error)."""
        rng = random.Random(seed)
        builds = [h.build() for h in lineup]
        rsp = [h.rspd for h in lineup]
        ssp = [h.sspd for h in lineup]
        okey = opp.key()
        tcache: dict = {}
        chain = opt_steal = opt_hnr = None
        if self.decision == "opt":
            chain = self.build_chain(lineup, opp)
            _, _, opt_steal, opt_hnr = self._opt_solution(chain, opp)

        def trans(b, outs, runners, hnr):
            k = (b, outs, runners, hnr)
            if k not in tcache:
                tcache[k] = self._transitions(b, outs, runners, builds, rsp, ssp, opp, hnr)
            return tcache[k]

        totals = []
        for _ in range(games):
            b = 0
            runs_game = 0
            for inning in range(1, self.innings + 1):
                rem = self._rem(inning)
                outs = 0
                bases = [-1, -1, -1]
                while outs < 3:
                    runners = tuple(bases)
                    occ = tuple(x >= 0 for x in runners)
                    speeds = tuple(rsp[x] if x >= 0 else None for x in runners)
                    sspeeds = tuple(ssp[x] if x >= 0 else None for x in runners)
                    brc = _BRC_CODE[tuple(int(x) for x in occ)]
                    node = chain.index.get((b, outs) + runners) if chain is not None else None
                    if chain is not None:
                        p_safe = opt_steal.get(node)
                    else:
                        p_safe = self.steal_decision(rem, outs, occ, okey, opp, sspeeds) if (occ[0] or occ[1]) else None
                    if p_safe is not None:
                        sb, cs, _ = steal_names(occ)
                        res = sb if rng.random() < p_safe else cs
                        _, rt, runs, eo = self.brc[(outs, brc, res)]
                        runs_game += runs
                        bases = _place(rt, runners)
                        outs = eo
                        continue
                    tn = trans(b, outs, runners, False)
                    use = tn
                    if self._hnr_eligible(outs, occ):
                        th = trans(b, outs, runners, True)
                        if (bool(opt_hnr[node]) if chain is not None else
                                self.hnr_better(rem, (outs, occ, (speeds, sspeeds), builds[b], okey), tn, th)):
                            use = th
                    u, acc = rng.random(), 0.0
                    pick = use[-1]
                    for t in use:
                        acc += t[0]
                        if u < acc:
                            pick = t
                            break
                    _, nd, runs, ld, eo, _ = pick
                    runs_game += runs
                    if nd is None:
                        outs = 3
                        b = ld
                    else:
                        b, outs, r1, r2, r3 = nd
                        bases = [r1, r2, r3]
            totals.append(runs_game)
        arr = np.asarray(totals, dtype=float)
        return float(arr.mean()), float(arr.std(ddof=1) / math.sqrt(len(arr)))

    # ---- detailed Monte Carlo: tallies, runner-speed credit, game log
    def _pa_ranges(self, batter: tuple, pit: tuple, outs: int, speeds: tuple, hnr: bool = False) -> tuple:
        """((result, low, high), ...) diff ranges for one swing against one pitcher."""
        key = ("rng", batter, pit, outs, speeds, hnr)
        r = self._pa_cache.get(key)
        if r is None:
            out = self.calc.compute(pit_hand=pit[0], pit_mov=pit[1], pit_cmd=pit[2], pit_vel=pit[3], pit_awr=pit[4],
                                    bat_hand=batter[0], bat_con=batter[1], bat_eye=batter[2], bat_pow=batter[3],
                                    bat_spd=batter[4], runner_speeds=speeds, outs=outs, hit_and_run=hnr)
            r = tuple((name, lo, hi) for name, width, lo, hi in out["swing_ranges"] if width > 0)
            self._pa_cache[key] = r
        return r

    def _row_value(self, outs: int, brc: int, res: str) -> float:
        """Runs scored on a result plus the run expectancy of the state it leaves."""
        bt, rt, runs, eo = self.brc[(outs, brc, res)]
        if eo >= 3:
            return float(runs)
        occ = [False, False, False]
        for t in (bt,) + tuple(rt):
            if t in (1, 2, 3):
                occ[t - 1] = True
        return runs + self.re[(eo, self._obc(tuple(occ)))]

    def _simulate_detailed_one(self, lineup: list[Hitter], opp, games: int = 20000, seed: int = 1, log_games: int = 1,
                               baseline: int = 3, max_log: int = 150, pitcher_per_game: bool = False,
                               progress=None) -> dict:
        """Play `games` games event by event (the diff and the steal roll are drawn from the 1000-ring, pitchers
        and catchers are drawn per plate appearance / steal from the mixes) and tally what happened.

        pitcher_per_game=True draws one pitcher from the mix per game (the same arm all game) instead of a fresh
        one for every plate appearance.

        Returns per-hitter tallies (plate appearances, hit-and-run calls, steal attempts and results, runs scored,
        RBI, net steal run value, and base-running credit: for every swing with runners on, the same diff is replayed
        with that runner at speed `baseline`; if the result changes, the difference in runs + run expectancy is
        credited to that runner), the runs-per-game array, and a log of the first `log_games` games."""
        rng = random.Random(seed)
        n = len(lineup)
        builds = [h.build() for h in lineup]
        rsp = [h.rspd for h in lineup]
        ssp = [h.sspd for h in lineup]
        names = [h.name for h in lineup]
        okey = opp.key()
        parts = opp.parts()
        pw, ps_ = [w for w, _ in parts], [p for _, p in parts]
        cw, ce = [w for _, w in self.catcher_dist], [e for e, _ in self.catcher_dist]
        keys = ("pa", "hnr", "att", "sb", "cs", "runs", "rbi", "steal_net", "base_credit",
                "ks_att", "ks_sb", "ks_cs", "ks_net", "hit_re")
        tally = [dict.fromkeys(keys, 0.0) for _ in range(n)]
        tcache: dict = {}
        log: list = []

        def ring():
            d = abs(rng.randint(1, 1000) - rng.randint(1, 1000))
            return min(d, 1000 - d)

        def trans(b, outs, runners, hnr):
            k = (b, outs, runners, hnr)
            if k not in tcache:
                tcache[k] = self._transitions(b, outs, runners, builds, rsp, ssp, opp, hnr)
            return tcache[k]

        def bases_txt(runners):
            on = [f"{names[r].split()[-1]}@{i + 1}B" for i, r in enumerate(runners) if r >= 0]
            return ", ".join(on) if on else "bases empty"

        chain = opt_steal = opt_hnr = None
        if self.decision == "opt":
            chain = self.build_chain(lineup, opp)
            _, _, opt_steal, opt_hnr = self._opt_solution(chain, opp)
        game_pitcher = [None]

        def draw_pitcher():
            if pitcher_per_game and game_pitcher[0] is not None:
                return game_pitcher[0]
            return rng.choices(ps_, pw)[0] if len(ps_) > 1 else ps_[0]

        def steal_roll(base, spd):
            pitcher = draw_pitcher()
            eye = rng.choices(ce, cw)[0] if len(ce) > 1 else ce[0]
            return ring() <= steal_safe_range(base, spd, pitcher.awr, eye)

        totals = []
        step = max(1, games // 100)
        for g in range(games):
            if progress is not None and g % step == 0:
                progress(g / games)
            game_pitcher[0] = rng.choices(ps_, pw)[0] if (pitcher_per_game and len(ps_) > 1) else None
            logging_on = g < log_games
            b = 0
            runs_game = 0
            for inning in range(1, self.innings + 1):
                rem = self._rem(inning)
                outs = 0
                bases = [-1, -1, -1]
                while outs < 3:
                    runners = tuple(bases)
                    occ = tuple(x >= 0 for x in runners)
                    speeds = tuple(rsp[x] if x >= 0 else None for x in runners)
                    sspeeds = tuple(ssp[x] if x >= 0 else None for x in runners)
                    brc = _BRC_CODE[tuple(int(x) for x in occ)]
                    pre = (outs, brc)
                    node = chain.index.get((b, outs) + runners) if chain is not None else None
                    if chain is not None:
                        p_dec = opt_steal.get(node)
                    else:
                        p_dec = self.steal_decision(rem, outs, occ, okey, opp, sspeeds) if (occ[0] or occ[1]) else None
                    if p_dec is not None:
                        sb, cs, base = steal_names(occ)
                        runner = runners[1] if occ[1] else runners[0]
                        safe = steal_roll(base, ssp[runner])
                        res = sb if safe else cs
                        before = self.re[(outs, self._obc(occ))]
                        t = tally[runner]
                        t["att"] += 1
                        t["sb" if safe else "cs"] += 1
                        t["steal_net"] += self._row_value(outs, brc, res) - before
                        _, rt, runs, eo = self.brc[(outs, brc, res)]
                        if logging_on and len(log) < max_log:
                            log.append(f"Inn {inning}, {outs} out, {bases_txt(runners)}: {names[runner]} "
                                       f"{'steals ' + base + ' - SAFE' if safe else 'caught stealing ' + base}")
                        runs_game += runs
                        bases = _place(rt, runners)
                        outs = eo
                        continue
                    tn = trans(b, outs, runners, False)
                    hnr = False
                    if self._hnr_eligible(outs, occ):
                        th = trans(b, outs, runners, True)
                        hnr = (bool(opt_hnr[node]) if chain is not None else
                               self.hnr_better(rem, (outs, occ, (speeds, sspeeds), builds[b], okey), tn, th))
                    pitcher = draw_pitcher()
                    pk = pitcher.key()
                    d = ring()
                    ranges = self._pa_ranges(builds[b], pk, outs, speeds, hnr)
                    label = next((nm for nm, lo, hi in ranges if lo <= d <= hi), ranges[-1][0])
                    t = tally[b]
                    t["pa"] += 1
                    note = ""
                    if hnr:
                        t["hnr"] += 1
                    if hnr and label == "K":
                        lead = runners[1] if occ[1] else runners[0]
                        sb, _, base = steal_names(occ)
                        safe = steal_roll(base, ssp[lead])
                        tl = tally[lead]
                        tl["ks_att"] += 1
                        tl["ks_sb" if safe else "ks_cs"] += 1
                        if safe:
                            row = (outs + 1, brc, sb)
                            note = f" then {names[lead]} steals {base} - SAFE"
                        else:
                            row = (outs, brc, "KCS")
                            note = f" then {names[lead]} caught stealing {base}"
                        bt, rt, runs, eo = self.brc[row]
                        t["hit_re"] += self.re[(outs + 1, self._obc(occ))] - self.re[(outs, self._obc(occ))]
                        tl["ks_net"] += self._row_value(*row) - self.re[(outs + 1, self._obc(occ))]
                        new = _place(rt, runners)
                        who = runners
                        tokens = rt
                    else:
                        bt, rt, runs, eo = self.brc[(outs, brc, label)]
                        who = (b,) + runners
                        tokens = (bt,) + tuple(rt)
                        new = _place(tokens, who)
                        actual = self._row_value(outs, brc, label)
                        dre = actual - self.re[(outs, self._obc(occ))]
                        cred = 0.0
                        # runner-speed credit: replay this diff with each runner at the baseline speed
                        if not hnr and (occ[0] or occ[1] or occ[2]):
                            for k in range(3):
                                r = runners[k]
                                if r < 0 or rsp[r] == baseline:
                                    continue
                                cf_speeds = tuple(baseline if i == k else speeds[i] for i in range(3))
                                cf_ranges = self._pa_ranges(builds[b], pk, outs, cf_speeds, False)
                                cf_label = next((nm for nm, lo, hi in cf_ranges if lo <= d <= hi), cf_ranges[-1][0])
                                if cf_label != label and (outs, brc, cf_label) in self.brc:
                                    c = actual - self._row_value(outs, brc, cf_label)
                                    tally[r]["base_credit"] += c
                                    cred += c
                        t["hit_re"] += dre - cred
                    # who scored
                    for tkn, w in zip(tokens, who):
                        if tkn == 4 and w >= 0:
                            tally[w]["runs"] += 1
                    t["rbi"] += runs
                    runs_game += runs
                    if logging_on and len(log) < max_log:
                        log.append(f"Inn {inning}, {outs} out, {bases_txt(runners)}: {names[b]}"
                                   f"{' (hit and run)' if hnr else ''} {label}{note}"
                                   f"{f' - {int(runs)} run' + ('s' if runs > 1 else '') if runs else ''}")
                    if eo >= 3:
                        outs = 3
                        b = (b + 1) % LINEUP
                    else:
                        bases = new
                        outs = eo
                        b = (b + 1) % LINEUP
                if logging_on and len(log) < max_log:
                    log.append(f"--- end of inning {inning}: {int(runs_game)} runs so far")
            totals.append(runs_game)
            if logging_on:
                log.append(f"=== game {g + 1}: {int(runs_game)} runs ===")
        arr = np.asarray(totals, dtype=float)
        scale = 1.0 / games
        per_game = [{k: v * scale for k, v in t.items()} for t in tally]
        for t in per_game:
            t["re_total"] = t["hit_re"] + t["base_credit"] + t["steal_net"] + t["ks_net"]
        return dict(runs=arr, mean=float(arr.mean()), se=float(arr.std(ddof=1) / math.sqrt(len(arr))),
                    games=games, per_game=per_game, names=names, log=log,
                    re_start=self.innings * self.re[(0, "000")])

    # ---- the catcher is known to the manager: solve each catcher separately, then average by how often he plays
    def _catcher_models(self) -> list:
        """[(weight, model)] - one model per catcher EYE in the mix. Each is a shallow copy that shares the
        plate-appearance cache but has its own steal / hit-and-run decision caches, so every strategy call is made
        knowing the catcher."""
        if len(self.catcher_dist) == 1:
            return [(1.0, self)]
        kids = getattr(self, "_kids", None)
        if kids is None:
            kids = []
            for eye, w in self.catcher_dist:
                k = copy.copy(self)
                k.catcher_dist = ((eye, 1.0),)
                k._steal_cache = {}
                k._hnr_cache = {}
                k._kids = None
                kids.append((w, k))
            self._kids = kids
        return kids

    def expected_runs(self, lineup: list[Hitter], opp, *, detail: bool = False):
        """Expected runs over the game (all innings, batting order carried over), averaged over the catcher mix
        with every steal / hit-and-run call made against the catcher actually behind the plate."""
        kids = self._catcher_models()
        if len(kids) == 1:
            return self._expected_runs_one(lineup, opp, detail=detail)
        total, per = 0.0, np.zeros(self.innings)
        for w, k in kids:
            v, pi_ = k._expected_runs_one(lineup, opp, detail=True)
            total += w * v
            per += w * pi_
            self.last_hnr_nodes = k.last_hnr_nodes
        return (total, per) if detail else total

    def simulate(self, lineup: list[Hitter], opp, games: int = 20000, seed: int = 1) -> tuple[float, float]:
        """Plain event-by-event simulation; returns (mean runs per game, standard error)."""
        kids = self._catcher_models()
        if len(kids) == 1:
            return self._simulate_one(lineup, opp, games, seed)
        mean, var = 0.0, 0.0
        for j, (w, k) in enumerate(kids):
            m, se = k._simulate_one(lineup, opp, max(2, int(round(w * games))), seed + j)
            mean += w * m
            var += (w * se) ** 2
        return mean, math.sqrt(var)

    def simulate_detailed(self, lineup: list[Hitter], opp, games: int = 20000, seed: int = 1, log_games: int = 1,
                          baseline: int = 3, max_log: int = 150, pitcher_per_game: bool = False,
                          progress=None) -> dict:
        """simulate_detailed against each catcher in proportion to how often he plays (see _simulate_detailed_one).
        progress, if given, is called with the fraction of games played."""
        kids = self._catcher_models()
        kw = dict(baseline=baseline, max_log=max_log, pitcher_per_game=pitcher_per_game)
        if len(kids) == 1:
            return self._simulate_detailed_one(lineup, opp, games, seed, log_games, progress=progress, **kw)
        main = max(range(len(kids)), key=lambda j: kids[j][0])          # the log comes from the most common catcher
        sizes = [max(2, int(round(w * games))) for w, _ in kids]
        grand = float(sum(sizes))
        results, done = [], 0
        for j, (w, k) in enumerate(kids):
            g = sizes[j]
            sub = None if progress is None else (lambda f, done=done, g=g: progress((done + f * g) / grand))
            results.append((g, k._simulate_detailed_one(lineup, opp, g, seed + j, log_games if j == main else 0,
                                                        progress=sub, **kw)))
            done += g
        total = sum(g for g, _ in results)
        runs = np.concatenate([r["runs"] for _, r in results])
        keys = results[0][1]["per_game"][0].keys()
        per_game = [{key: sum(g * r["per_game"][i][key] for g, r in results) / total for key in keys}
                    for i in range(len(lineup))]
        return dict(runs=runs, mean=float(runs.mean()), se=float(runs.std(ddof=1) / math.sqrt(len(runs))),
                    games=total, per_game=per_game, names=results[0][1]["names"], log=results[main][1]["log"],
                    re_start=results[0][1]["re_start"])

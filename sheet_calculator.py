"""Exact port of the stadium Google Sheet "calculator" tab range logic (Q25:V75, plus the Y:AB assembly).

The sheet is the source of truth.  Every formula below cites the calculator cell it mirrors.  All stadium
specific numbers (the build-offset table A11:N19, range modifiers A40:N43, handedness, WH/GO rate constants,
bunt tables, ...) are read from the workbook's constant cells by `load_params`, so a different ballpark's
sheet gives that ballpark's ranges without code changes.

Blank-cell semantics follow Google Sheets: a blank is None, which is 0 in arithmetic and equal to both 0 and "".

Inputs the sheet gets from other tabs (Gameplay / import_*) are plain arguments here:
  * builds: Gameplay!W12:AA13 -> pitcher hand/MOV/CMD/VEL/AWR, batter hand/CON/EYE/POW/SPD
  * runner speeds: Gameplay!BE19:BE21 looked up in import_Players!N (blank when no runner)
  * outs (calculator!Q5), hit-and-run flag (Q7), infield-in setting (Gameplay!BG4 via AC17)
Live mode only: hit-and-run subtracts 1 from batter CON (Gameplay!W11) and adds 1 to each runner's speed (AC9).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# ---------------------------------------------------------------- params
_CONST_RANGES = ("A11:N21", "A39:N43", "A53:H58", "A74:I77", "A80:N89", "A92:W95", "A98:N102",
                 "A105:N109", "A38:A38")
_SCALARS = ("N9", "D9", "U22", "V21", "I20", "I21", "I22", "C74", "C75", "C76", "C77", "C80", "C81", "C82")
_BUILD_ROWS = range(11, 20)          # A11:N19: Hit, HR, 3B, 2B, IF1B, BB, FO, PO, K
_COLS = "DEFGHIJKLMN"                # offsets -5..+5


def load_params(xlsx_path: str | Path, sheet: str = "calculator") -> dict[str, float | str | None]:
    """Read the calculator tab's constant cells (cached values) into {'A11': 'Hit', ...}."""
    import warnings
    import openpyxl
    from openpyxl.utils import range_boundaries, get_column_letter

    warnings.filterwarnings("ignore")
    ws = openpyxl.load_workbook(xlsx_path, data_only=True)[sheet]
    out: dict[str, float | str | None] = {}
    for rng in _CONST_RANGES:
        c0, r0, c1, r1 = range_boundaries(rng)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                v = ws.cell(r, c).value
                if v is not None:
                    out[f"{get_column_letter(c)}{r}"] = v
    for co in _SCALARS:
        v = ws[co].value
        if v is not None:
            out[co] = v
    return out


def save_params(params: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(params, indent=1, sort_keys=True), encoding="utf-8")


def load_params_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- spreadsheet helpers
def _n(x):
    """Blank / '' -> 0 for arithmetic."""
    return 0 if x is None or x == "" else x


def _blank(x) -> bool:
    return x is None or x == ""


SNAP = True   # ROUNDDOWN treats values within 1e-9 of an integer as that integer (decimal-aware, like ROUND in
#               Sheets).  False = plain IEEE truncation.  Only matters when rate*base lands on a whole number,
#               e.g. 0.08*50 = 3.9999999999999996; tools/verify_sheet_calculator.py lists such states.


def _rd(x: float) -> int:
    """ROUNDDOWN(x, 0): toward zero."""
    if SNAP and abs(x - round(x)) < 1e-9:
        return int(round(x))
    return math.trunc(x)


def _round(x: float) -> int:
    """ROUND(x, 0): half away from zero."""
    return int(math.floor(abs(x) + 0.5 + 1e-12)) * (1 if x >= 0 else -1)


def _clamp5(v):
    return 5 if v > 5 else (-5 if v < -5 else v)


# ---------------------------------------------------------------- the calculator
class SheetCalculator:
    """One evaluation of calculator!Q25:V75 for a given game situation."""

    def __init__(self, params: dict):
        self.p = params

    # constant cell accessor ------------------------------------------------
    def c(self, coord: str):
        return self.p.get(coord)

    def cn(self, coord: str):
        return _n(self.p.get(coord))

    # ----------------------------------------------------------------- tables
    def _tables(self, *, dynamic: bool, pit_stats: dict, bat_stats: dict):
        """Rows 26-36 (cumulative result table) and the helper constants."""
        cn = self.cn
        # rows 11-19 offsets: index by result name; D..H mirror J..N (D11 = N11*-1 ...)
        names = [self.c(f"A{r}") for r in _BUILD_ROWS]
        off: dict[str, list[float]] = {}   # 11 values for -5..+5
        center: dict[str, float] = {}
        for r, nm in zip(_BUILD_ROWS, names):
            right = [cn(f"{col}{r}") for col in "JKLMN"]      # +1..+5
            left = [-v for v in reversed(right)]               # -5..-1
            off[nm] = left + [0.0] + right
            center[nm] = cn(f"I{r}")
        # table D26:N34: cumulative from the center; left side floored at 1
        tbl: dict[str, list[float]] = {}
        for nm in names:
            vals = [0.0] * 11
            vals[5] = center[nm]
            for i in range(6, 11):                              # J26 = I26 + J11 ...
                vals[i] = vals[i - 1] + off[nm][i]
            for i in range(4, -1, -1):                          # H26 = IF(I26+H11<1,1,I26+H11)
                s = vals[i + 1] + off[nm][i]
                vals[i] = 1 if s < 1 else s
            tbl[nm] = vals
        # FO% (row 35) and PO% (row 36); FO_HND = FO row, PO_HND = PO row (rows 32,33)
        fo_h, po_h = tbl["FO"], tbl["PO"]
        if dynamic:
            n97 = self._avg_rate(range(99, 103), bat_stats, pit_stats)
            m104 = self._avg_rate(range(106, 110), bat_stats, pit_stats)
            n104 = (1 - n97) * m104
            fo_pct = [n104] * 11
            po_pct = [1 - m104] * 11
        else:
            fo_pct = [0.0] * 11
            po_pct = [0.0] * 11
            fo_pct[5] = cn("I21")
            po_pct[5] = cn("I22")
            for i in range(4, -1, -1):                         # H35 = (H17/1000*2) + I35
                fo_pct[i] = (off["FO"][i] / 1000) * 2 + fo_pct[i + 1]
                po_pct[i] = ((po_h[i] - po_h[i + 1]) / 1000) * 2 + po_pct[i + 1]
            for i in range(6, 11):                             # J35 = (J17/1000*2) + I35
                fo_pct[i] = (off["FO"][i] / 1000) * 2 + fo_pct[i - 1]
                po_pct[i] = ((po_h[i] - po_h[i - 1]) / 1000) * 2 + po_pct[i - 1]
        return tbl, fo_pct, po_pct

    def _avg_rate(self, rows, bat_stats, pit_stats):
        """B99:B102 / B106:B109 -> AVERAGE: each row picks D..N by the batter-minus-pitcher diff."""
        labels = {"CON": "MOV", "EYE": "CMD", "POW": "VEL", "SPD": "AWR"}
        vals = []
        for r in rows:
            stat = self.c(f"A{r}")
            diff = int(bat_stats[stat] - pit_stats[labels[stat]])
            col = _COLS[diff + 5]                               # MATCH on D98:N98 (-5..5)
            vals.append(self.cn(f"{col}{r}"))
        return sum(vals) / len(vals)

    # ----------------------------------------------------------------- main
    def compute(self, *, pit_hand, pit_mov, pit_cmd, pit_vel, pit_awr,
                bat_hand, bat_con, bat_eye, bat_pow, bat_spd,
                runner_speeds=(None, None, None), outs=0, hit_and_run=False,
                infield_in=False, dynamic: bool | None = None, include_lotp: bool | None = None):
        """Return dict with 'swing' and 'bunt' lists of rows and the assembled final ranges."""
        cn, c = self.cn, self.c
        q5 = int(outs)
        q7 = bool(hit_and_run)
        ac9 = 1 if q7 else 0
        dyn = bool(c("D9")) if dynamic is None else dynamic
        lotp = bool(c("U22")) if include_lotp is None else include_lotp
        # builds as the calculator sees them (X3:AA3, X6:AA6, W3, W6)
        con = bat_con - (1 if q7 else 0)                        # Gameplay!W11 batter CON - IF(Q7,1,0)
        bat = {"CON": con, "EYE": bat_eye, "POW": bat_pow, "SPD": bat_spd}
        pit = {"MOV": pit_mov, "CMD": pit_cmd, "VEL": pit_vel, "AWR": pit_awr}
        # runners Z11:Z13 = speed + AC9 (blank when no runner)
        z = [None if _blank(s) else s + ac9 for s in runner_speeds]
        z1, z2, z3 = z
        zs = [_n(v) for v in z]
        sum_z = sum(zs)
        ac17 = False if q5 == 2 else bool(infield_in and not _blank(z3) and z3 != 0)   # Gameplay!BG4, calc!AC17
        # J61: BRC code of the current runners
        code = (100 if not _blank(z3) else 0) + (10 if not _blank(z2) else 0) + (1 if not _blank(z1) else 0)
        j61 = {0: 0, 1: 1, 10: 2, 11: 4, 100: 3, 101: 5, 110: 6, 111: 7}[code]
        # W7 handedness (N9)
        n9 = cn("N9")
        w7 = 1 if str(bat_hand).upper() == "S" else ((1 - n9) if str(pit_hand).upper() == str(bat_hand).upper() else (1 + n9))
        # X7:AA7 diffs and H80/H81
        aa6, aa3, x3 = bat_spd, pit_awr, pit_mov

        tbl, fo_pct, po_pct = self._tables(dynamic=dyn, pit_stats=pit, bat_stats=bat)

        # SUMIF($A$40:$A$50, label, $I$40:$I$50)
        def sumif(label):
            return sum(cn(f"I{r}") for r in range(40, 51) if c(f"A{r}") == label)

        # V11:V19 matchup (clamped -5..5) and the stat each result uses (A11:C20)
        stat_of = {c(f"A{r}"): (c(f"B{r}"), c(f"C{r}")) for r in range(11, 21)}

        def matchup(label):          # label is the A-column key ("Hit" for 1B)
            b, p = stat_of[label]
            return int(_clamp5(bat[b] - pit[p]))

        v_hr, v_3b, v_2b = matchup("HR"), matchup("3B"), matchup("2B")
        v_1b, v_if, v_bb = matchup("Hit"), matchup("IF1B"), matchup("BB")
        v_fo, v_po, v_k = matchup("FO"), matchup("PO"), matchup("K")
        ix = lambda v: v + 5

        def mx(x):
            return max(1, x)

        w11 = mx(_rd((tbl["HR"][ix(v_hr)] + sumif("HR")) * w7))
        w12 = mx(_rd((tbl["3B"][ix(v_3b)] + sumif("3B")) * w7))
        w13 = mx(_rd((tbl["2B"][ix(v_2b)] + sumif("2B")) * w7))
        # row-40/41/43 lookups for W14
        r40 = cn(f"{_COLS[ix(v_2b)]}40")                        # HLOOKUP(V13, D39:N42, 2)
        awr_idx = int(pit_awr - 3)
        r41 = cn(f"{_COLS[awr_idx + 5]}41") if -5 <= awr_idx <= 5 else None   # HLOOKUP(AA3-3, ., 3)
        if r41 is None:
            raise ValueError("pitcher AWR out of range for HLOOKUP(AA3-3, ...)")
        w14 = mx(_rd(tbl["Hit"][ix(v_1b)] * w7) + cn(f"{_COLS[ix(v_1b)]}43") + r40 + r41 + cn("I42")
                 - (w11 + w12 + w13) + (20 if ac17 else 0))
        w15 = mx(_rd((tbl["IF1B"][ix(v_if)] + sumif("IF1B")) * w7))
        w16 = mx(_rd((tbl["BB"][ix(v_bb)] + sumif("BB")) * w7))
        fo_v, po_v = fo_pct[ix(v_fo)], po_pct[ix(v_po)]
        foV_dbg, poV_dbg = fo_v, po_v
        s16 = w11 + w12 + w13 + w14 + w15 + w16
        w17 = mx(_rd((500 - s16) * fo_v - (500 - s16) * fo_v * po_v))
        w18 = mx(_rd((500 - w16) * fo_v * po_v))                 # sheet uses (500-W16) here, not 500-SUM
        w19 = mx(_rd((tbl["K"][ix(v_k)] + sumif("K")) * w7))
        w21 = 0 if sum_z == 0 else (0 if q5 == 2 else cn("V21"))
        w20 = 500 - (w11 + w12 + w13 + w14 + w15 + w16 + w17 + w18 + w19) - w21 + 1
        base = {"HR": w11, "3B": w12, "2B": w13, "1B": w14, "IF1B": w15, "BB": w16, "FO": w17, "PO": w18,
                "K": w19, "GO": w20, "LO": w21}

        # ---- WH helper block (F75:I76, G75) ----
        c74, c75, c76, c77 = cn("C74"), cn("C75"), cn("C76"), cn("C77")
        g75 = c76 if q5 == 2 else (c77 if q7 else 1)
        f75 = z1 if _blank(z2) else z2                           # IF(Z12="",Z11,Z12)
        if _blank(z1):                                           # F76 = IF(Z11="",,IF(Z11>Z12,Z12,Z11))
            f76 = None
        else:
            f76 = z2 if z1 > _n(z2) else z1                      # blank Z12 reads as 0, so Z11>Z12 -> blank
        g76 = None if _blank(f76) else g75

        def wh(f, g):            # ($C$74+((F-3)*IF((F-3)<0,0.5,1)*C75))*G
            d = f - 3
            return (c74 + (d * (0.5 if d < 0 else 1) * c75)) * g

        if _blank(f75):
            h75 = None
        elif q7:
            h75 = 1 if f75 > 0 else None
        else:
            h75 = wh(f75, g75)
        if _blank(z1):
            h76 = None
        elif q7:
            h76 = 1 if _n(f76) > 0 else None
        elif _blank(f76):
            h76 = None
        else:
            h76 = wh(f76, g76)
        if _blank(z2):
            i75 = 0
        else:
            i75 = 0 if q5 == 2 else c74 + (((f75 - 3) * (0.5 if (f75 - 3) < 0 else 1)) * c75)

        # ---- swing block rows 26-63 ----
        T: dict[int, object] = {}
        V: dict[int, object] = {}
        U: dict[int, int] = {}

        def U_of(label):
            return base[label]

        # HR 26/27
        U[26] = base["HR"]
        T[26] = 1                                               # T27 blank
        V[26] = 1 if _rd(T[26] * U[26]) == 0 else U[26]
        V[27] = None
        # 3B 28/29
        U[28] = base["3B"]; T[28] = 1; V[28] = _rd(T[28] * U[28]); V[29] = None
        # 2B 30-32
        U[30] = U[31] = U[32] = base["2B"]
        # T30 = IF(Y23,1,IFERROR(IF(VALUE(Z11)=0,0,IF(H76="",H75,H76)),0)); Y23 is blank
        if _n(z1) == 0:
            T[30] = 0
        else:
            T[30] = _n(h75) if _blank(h76) else h76
        T[31] = 1 - T[30]
        V[30] = _rd(U[30] * T[30])
        V[31] = U[31] - V[30]                                   # T32 blank -> U31-V30
        V[32] = None
        # 1B 33-36
        U[33] = U[34] = U[35] = U[36] = base["1B"]
        if _n(z1) + _n(z2) == 0:                                # SUM(Z11:Z12)=0
            T[33] = 0
        else:
            T[33] = (0 if _blank(h75) else h75) if _blank(h76) else h76
        if _blank(z2) or _n(z2) == 0 or _blank(z1) or _n(z1) == 0:
            T[34] = 0
        else:
            T[34] = 0 if _n(h75) == _n(h76) else _n(h75) - _n(h76)
        T[35] = 1 - T[33] - T[34]
        U37 = base["IF1B"]
        V[33] = _rd(T[33] * U[33]) + (U37 if (q7 and ac17) else 0)
        V[34] = _rd(T[34] * U[34])
        V[35] = (U[35] - V[33] - V[34]) + (U37 if ac17 else 0)  # T36 blank
        V[36] = None
        # IF1B 37/38
        U[37] = U37
        T[37] = 0 if ac17 else 1
        V[37] = _rd(T[37] * U[37]); V[38] = None
        # BB 39/40
        U[39] = base["BB"]; T[39] = 1; V[39] = _rd(T[39] * U[39]); V[40] = None
        # FO 41-45
        U[41] = U[42] = U[43] = U[44] = U[45] = base["FO"]
        if q5 == 2 or _n(z3) == 0 or _n(z2) == 0:
            T[41] = 0
        else:
            T[41] = i75
        if q5 == 2 or _n(z2) == 0:
            T[42] = 0
        else:
            T[42] = 0 if T[41] > 0 else i75
        if _n(z2) + _n(z3) == 0:
            T[43] = 0
        elif q5 == 2 or _n(z3) == 0:
            T[43] = 0
        else:
            T[43] = 1 - T[41]
        if q5 == 2:
            T[44] = 1
        elif T[43] > 0:
            T[44] = 0
        elif sum_z == 0:
            T[44] = 1
        else:
            T[44] = 1 - T[42] - T[41]
        V[41] = _rd(T[41] * U[41])
        V[42] = _rd(T[42] * U[42])
        V[43] = 0 if T[43] == 0 else U[43] - V[41] - V[42]
        V[44] = 0 if T[43] > 0 else U[44] - V[42]               # T45 blank
        V[45] = None
        # PO 46/47
        U[46] = U[47] = base["PO"]; T[46] = 1; V[46] = _rd(T[46] * U[46]); V[47] = None
        # GO 48-58 (J62:J71 groundout table)
        go = self._go_rates(j61, z1, z2, z3, q5, ac17, bat_spd, aa6)
        order = [(48, "GORA"), (49, "FCH"), (50, "FC"), (51, "GO"), (52, "FC3rd"), (53, "DPRun"), (54, "DP"),
                 (55, "DP21"), (56, "DP31"), (57, "DPH1")]
        for r in range(48, 59):
            U[r] = base["GO"]
        for r, nm in order:
            raw = go.get(nm)                                    # None = blank cell in the table
            if r == 48:
                T[r] = 0 if ac17 else (None if q5 == 2 else raw)
            elif r == 51:
                T[r] = raw                                      # IFERROR(VLOOKUP(...),0), no outs check
            else:
                T[r] = None if q5 == 2 else raw
        # V48..V57
        def tsum(a, b):
            return sum(_n(T.get(i)) for i in range(a, b + 1))

        V[48] = _rd(_n(T[48]) * U[48])
        for r in range(49, 58):
            t = T[r]
            prev = sum(_n(V[i]) for i in range(48, r))
            if r in (49, 50) and _blank(t):
                V[r] = 0
            elif tsum(r + 1, 58) == 0:
                V[r] = U[r] - prev
            else:
                V[r] = _rd(_n(t) * U[r])
        V[58] = None
        # K 59/60, LO 61-63
        U[59] = base["K"]; T[59] = 1
        U[61] = U[62] = U[63] = base["LO"]
        # T62 TP, T63 LOTP, T61 LODP
        T[62] = 0 if q5 > 0 else (0 if _n(z2) == 0 else (0 if _n(z1) == 0 else 1))
        if not lotp:
            T[63] = 0
        elif T[62] > 0 or q5 > 0:
            T[63] = 0
        else:
            T[63] = 0 if sum(1 for v in z if _n(v) > 0) < 2 else 0.25
        if T[62] > 0:
            T[61] = 0
        elif q5 > 1:
            T[61] = 0
        else:
            t = 0 if sum_z == 0 else (0 if _n(z1) > 0 else 1)
            T[61] = t - T[63]
        V[62] = T[62] * U[62]
        V[63] = 0 if T[63] == 0 else (0 if (T[61] + T[62] + T[63]) == 0 else 1)
        V[61] = 0 if (T[61] + T[62] + T[63]) == 0 else U[61] - (V[62] + V[63])
        V[59] = _rd(T[59] * U[59]) + (base["LO"] if (T[61] + T[62] + T[63]) == 0 else 0)
        V[60] = None

        # ---- bunt block rows 67-75 ----
        c80, c81, c82 = cn("C80"), cn("C81"), cn("C82")
        m80 = sum(_n(V.get(r)) for r in range(26, 39)) - 1
        h80 = aa6 - x3
        m81 = _round((1 + (c80 + h80 * c81)) * m80) - c82
        h81 = aa6 - aa3
        f82 = ("Runner Home" if _n(z3) > 0 else "Runner to 3rd" if _n(z2) > 0
               else "Runner to 2nd" if _n(z1) > 0 else "Bunt for Hit")
        hcol = _COLS[min(5, int(h81)) + 5]
        row_of = {c(f"A{r}"): r for r in range(86, 90)}
        if j61 == 0:
            t71 = 0
            t72 = 0
        else:
            t71 = cn(f"{hcol}{row_of[f82]}")
            t72 = cn(f"{hcol}89")
        t73 = 1 - t71 - t72
        u67 = c82
        u68 = m81
        u69 = base["BB"]
        u70 = base["K"]
        u74 = U[62]
        u75 = U[63]
        u71 = 500 - (u67 + u68 + u69 + u70) - (u74 + u75)
        v67, v68, v69, v70 = u67, u68, u69, u70                 # T67:T70 = 1
        v71 = _rd(t71 * u71)
        v72 = _rd(t72 * u71)
        v74 = T[62] * u74
        v75 = T[63] * u75
        v73 = (500 - (v67 + v68 + v69 + v70 + v71 + v72) + 1) - (v74 + v75)
        bunt = [
            ("B1BWH", "B1BWH", 1, 1.0, u67, v67),
            ("B1B", "B1B", 2, 1.0, u68, v68),
            ("BB", "BB", 3, 1.0, u69, v69),
            ("K", "K", 5, 1.0, u70, v70),
            ("SacB", "SacB", 4, t71, u71, v71),
            ("BDP", "BDP", 7, t72, u71, v72),
            ("GO" if j61 == 0 else "BFC", "GO" if j61 == 0 else "BFC", 6, t73, u71, v73),
            ("TP", "TP", 8, T[62], u74, v74),
            ("LOTP", "LOTP", 9, T[63], u75, v75),
        ]

        # ---- swing rows as (Q, R, S, T, U, V) ----
        labels = {
            26: ("HR", "HR", 1), 28: ("3B", "3B", 2), 30: ("2B", "2BWH", 3), 31: ("2B", "2B", 4),
            33: ("1B", "1BWH", 5), 34: ("1B", "1BWH2", 6), 35: ("1B", "1B", 7), 37: ("IF1B", "IF1B", 8),
            39: ("BB", "BB", 9), 41: ("FO", "DSacF", 11), 42: ("FO", "DFO", 12), 43: ("FO", "SacF", 13),
            44: ("FO", "FO", 14), 46: ("PO", "PO", 15), 48: ("GO", "GORA", 10), 49: ("GO", "FCH", 16),
            50: ("GO", "FC", 17), 51: ("GO", "GO", 18), 52: ("GO", "FC3rd", 19), 53: ("GO", "DPRun", 20),
            54: ("GO", "DP", 22), 55: ("GO", "DP21", 23), 56: ("GO", "DP31", 24), 57: ("GO", "DPH1", 25),
            59: ("K", "K", 21), 61: ("LO", "LODP", 30), 62: ("LO", "TP", 31), 63: ("LO", "LOTP", 32),
        }
        swing = []
        for r, (q, sub, order_) in labels.items():
            swing.append({"row": r, "result": q, "sub": sub, "order": order_, "rate": T.get(r),
                          "base": U.get(r), "final": _n(V.get(r))})
        w_by = {s["sub"]: s["final"] for s in swing}
        debug = {                                               # intermediate values for the Manager debug panel
            "d_hit": v_1b, "d_pow": v_hr, "d_spd": v_3b, "d_eye": v_bb, "hnd": round(w7, 3),
            "w_hr": base["HR"], "w_3b": base["3B"], "w_2b": base["2B"], "w_1b": base["1B"],
            "w_if1b": base["IF1B"], "w_bb": base["BB"], "w_k": base["K"],
            "fo_rate": round(foV_dbg, 4), "po_rate": round(poV_dbg, 4), "after_hits": 500 - s16,
            "w_fo": base["FO"], "w_po": base["PO"], "dfo_pct": round(_n(T.get(42)), 4),
            "fo_rows": [(nm, w_by[nm]) for nm in ("DSacF", "DFO", "SacF", "FO") if w_by.get(nm)],
            "s1": z1, "s2": z2, "s3": z3, "lead_spd": f75, "trail_spd": f76,
            "lead_wh": h75 or 0, "trail_wh": h76,
            "2bwh_rate": _n(T.get(30)), "1bwh_rate": _n(T.get(33)), "1bwh2_rate": _n(T.get(34)),
            "if1b_bonus": base["IF1B"] if ac17 else 0,
            "w_2bwh": w_by.get("2BWH", 0), "w_1bwh": w_by.get("1BWH", 0), "w_1bwh2": w_by.get("1BWH2", 0),
            "w_1b_plain": w_by.get("1B", 0),
            "w_go": base["GO"], "dp_base": 0.0, "dp_mult": None,
            "go_detail": [(s["sub"], round(_n(s["rate"]), 4), s["final"]) for s in swing
                          if s["result"] == "GO" and s["final"] > 0],
            "total_hit": m80 + 1, "base_hit": m80, "b1bwh": u67, "b1b": u68,
            "b_tp_base": u74, "b_lotp_base": u75, "b_go_pool": u71, "sacb_rate": t71, "bdp_rate": t72,
            "b_sacb": v71, "b_bdp": v72, "b_tp_final": v74, "b_lotp_final": v75, "b_go": v73,
        }
        return {
            "swing": swing,
            "debug": debug,
            "bunt": [{"row": 67 + i, "result": a, "sub": b, "order": s, "rate": t, "base": u, "final": v}
                     for i, (a, b, s, t, u, v) in enumerate(bunt)],
            "base_ranges": base,
            "swing_ranges": assemble([(s["sub"], s["final"], s["order"]) for s in swing]),
            "bunt_ranges": assemble([(b[1], b[5], b[2]) for b in bunt]),
        }

    # ----------------------------------------------------------------- groundout table
    def _go_rates(self, j61, z1, z2, z3, q5, ac17, bat_spd, aa6):
        """J62:J71 = INDEX(B:I, J61+1) of the dynamic groundout table (rows 62-71).  Missing key = blank."""
        cn = self.cn
        c54, c55, d55, f55, g55 = cn("C54"), cn("C55"), cn("D55"), cn("F55"), cn("G55")
        c56, d56, f56, g56 = cn("C56"), cn("D56"), cn("F56"), cn("G56")
        e57, g58 = cn("E57"), cn("G58")
        zs = [v for v in (z1, z2, z3) if not _blank(v)]
        avg_all = (sum(zs) / len(zs)) if zs else None
        z12 = [v for v in (z1, z2) if not _blank(v)]
        avg12 = (sum(z12) / len(z12)) if z12 else None
        out: dict[str, float] = {}
        k = j61
        if k == 0:                                              # col B: GO = 1 - B66 - B61 = 1
            out["GO"] = 1 - 0 - 0
        elif k == 1:                                            # col C
            c62 = c55 + ((z1 - 3) * c56)
            c67 = c54 - ((aa6 - 3) * 0.1)
            out.update(GORA=c62, FC=1 - c67 - c62, DP=c67)
        elif k == 2:                                            # col D
            d62 = d55 + ((z2 - 3) * d56)
            out.update(GORA=d62, GO=1 - d62)
        elif k == 3:                                            # col E
            e62 = d55 + ((z3 - 3) * d56)
            out.update(GORA=e62, GO=1 - e62)
        elif k == 4:                                            # col F
            f62 = c55 + ((avg12 - 3) * c56)
            f70 = ((c54 - ((aa6 - 3) * 0.1)) / 2) * (1 + e57)
            f69 = f70
            f64 = (1 - f69 - f70 - f62) / 2
            out.update(GORA=f62, FC=f64, FC3rd=f64, DP21=f69, DP31=f70)
        elif k == 5:                                            # col G
            g62 = c55 + ((avg_all - 3) * c56)
            g67 = ((c54 - ((aa6 - 3) * 0.1)) / 2) * (1 + e57)
            g68 = None if q5 > 0 else (1 - (c55 + ((avg_all - 3) * c56)) - g67 - g67) / 2
            g64 = 1 - g67 - g62 - _n(g68)
            out.update(GORA=g62, FC=g64, DP=g67)
            if g68 is not None:
                out["DPRun"] = g68
        elif k == 6:                                            # col H
            h62 = f55 + ((avg_all - 3) * f56)
            out.update(GORA=h62, GO=1 - h62)
        else:                                                   # col I (loaded)
            i62 = g55
            i66 = g58 + ((avg_all - 3) * g56)
            i70 = _rd((1 - i66 - i62) / (4 if ac17 else 3) * 1000) / 1000    # ROUNDDOWN(x, 3)
            i71 = _rd((1 - i66 - i62) / (2 if ac17 else 3) * 1000) / 1000
            i69 = 1 - i62 - i70 - i71 - i66
            out.update(GORA=i62, FCH=i66, DP21=i69, DP31=i70, DPH1=i71)
        return out


def assemble(rows: list[tuple[str, int, int]]) -> list[tuple[str, int, int, int]]:
    """Y26:AB43: QUERY 'Select Sub, Final where Final>0 order by Order' then cumulative low/high."""
    rows = sorted(((n, int(f), o) for n, f, o in rows if f > 0), key=lambda r: r[2])
    out, lo = [], 0
    for sub, final, _ in rows:
        out.append((sub, final, lo, lo + final - 1))
        lo += final
    return out


def compute_ranges(params: dict, **kwargs) -> dict:
    return SheetCalculator(params).compute(**kwargs)

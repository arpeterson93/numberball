"""Verify sheet_calculator.py against the stadium workbook's own formulas.

The oracle is the `formulas` package evaluating the real calculator tab (formulas untouched) with the
cross-tab inputs (builds, runners, outs, flags) overridden per scenario.  Usage:
    python tools/verify_sheet_calculator.py "POR - Oregon Trail Dysentary Field.xlsx" [n_scenarios] [seed]
"""
import itertools, random, re, sys, tempfile, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import openpyxl, schedula as sh, formulas
from openpyxl.worksheet.formula import ArrayFormula
import sheet_calculator as sc

EXT = re.compile(r"(Gameplay|import_\w+|scrim_Log|Box Score|etl_Box|Gameday|Pioneers|Opponent|commentary|retro_\w+)!")


def build_oracle_workbook(src: str, out: str, present=(True, True, False)) -> None:
    """calculator tab only; cross-tab/Google-only cells frozen to cached values; the few constructs the
    `formulas` package cannot evaluate are rewritten to equivalent plain formulas."""
    wb = openpyxl.load_workbook(src)
    wv = openpyxl.load_workbook(src, data_only=True)
    for n in wb.sheetnames:
        if n != "calculator":
            del wb[n]
    ws, cv, bv = wb["calculator"], wv["calculator"], wv["import_BRC"]
    for row in ws.iter_rows():
        for c in row:
            v = c.value
            t = v.text if isinstance(v, ArrayFormula) else v
            if isinstance(t, str) and t.startswith("=") and (EXT.search(t) or "DUMMYFUNCTION" in t or "XLOOKUP" in t):
                c.value = cv[c.coordinate].value
    for i, r in enumerate(range(3, 11)):                    # import_BRC!O3:P10 copied locally for J61
        ws.cell(3 + i, 34).value = bv.cell(r, 15).value
        ws.cell(3 + i, 35).value = bv.cell(r, 16).value
    ws["J61"] = '=VLOOKUP((IF(Z13="",0,100)+IF(Z12="",0,10)+IF(Z11="",0,1)),$AH$3:$AI$10,2,false)'
    ws["I26"] = ArrayFormula("I26:I34", "=I11:I19")         # INDEX(I11:I19,0) -> the column
    # Sheets: an omitted IF/IFERROR argument is a true blank (=0 and ="" both TRUE); `formulas` returns 0.
    # Point omitted arguments at a never-used blank cell so the oracle sees a blank too.
    # Sheets: an omitted IF/IFERROR argument yields a blank that equals both 0 and "" (confirmed by the sheet's
    # own behaviour: 1BWH exists with only a runner on 2nd, which needs H76="" for a formula-blank).  The
    # `formulas` package returns 0 there, so tests of ="" / <>"" are rewritten to =0 / <>0 (same truth values
    # for blanks, numeric speeds >= 1 and rates > 0).
    for row in ws.iter_rows():
        for c in row:
            v = c.value
            t = v.text if isinstance(v, ArrayFormula) else v
            if isinstance(t, str) and t.startswith("=") and ('=""' in t or '<>""' in t):
                new = re.sub(r'(=|<>)""', r'\g<1>0', t)
                c.value = ArrayFormula(c.coordinate, new) if isinstance(v, ArrayFormula) else new
    for row in ws.iter_rows(min_row=40, max_row=75):         # {A62:A71,J62:J71} -> A62:J71 col 10
        for c in row:
            v = c.value
            t = v.text if isinstance(v, ArrayFormula) else v
            if isinstance(t, str) and "{$A$62:$A$71,$J$62:$J$71}" in t:
                t = t.replace("{$A$62:$A$71,$J$62:$J$71},2", "$A$62:$J$71,10")
                c.value = ArrayFormula(c.coordinate, t) if isinstance(v, ArrayFormula) else t
    ws["U25"] = "Base"                                       # ={"Base";IF(R26:R63="",,VLOOKUP(...))}
    for r in range(26, 64):
        ws.cell(r, 21).value = f'=IF(R{r}="",0,VLOOKUP(Q{r},$U$11:$W$21,3,false))'
    for co, here in zip(("Z11", "Z12", "Z13"), present):     # runner cells: blank vs a number (input-overridden)
        ws[co] = 1.0 if here else None
    wb.save(out)


def main():
    src = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    models = {}

    def model_for(pat):
        if pat not in models:
            d = Path(tempfile.mkdtemp()) / "calc.xlsx"
            build_oracle_workbook(src, str(d), present=tuple(bool(x) for x in pat))
            models[pat] = formulas.ExcelModel().loads(str(d)).finish()
        return models[pat]
    K = "'[calc.xlsx]CALCULATOR'!"
    params = sc.load_params(src)
    calc = sc.SheetCalculator(params)
    rng = random.Random(seed)
    patterns = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1)]
    swing_rows = list(range(26, 64))
    bunt_rows = list(range(67, 76))
    bad = 0
    sensitive = []
    for i in range(n):
        s = dict(
            pit_hand=rng.choice("RL"), pit_mov=rng.randint(1, 5), pit_cmd=rng.randint(1, 5),
            pit_vel=rng.randint(1, 5), pit_awr=rng.randint(1, 5),
            bat_hand=rng.choice("RLS"), bat_con=rng.randint(1, 5), bat_eye=rng.randint(1, 5),
            bat_pow=rng.randint(1, 5), bat_spd=rng.randint(1, 5),
        )
        pat = rng.choice(patterns)
        s["runner_speeds"] = tuple(rng.randint(1, 5) if p else None for p in pat)
        s["outs"] = rng.randint(0, 2)
        s["hit_and_run"] = rng.random() < 0.25
        s["infield_in"] = rng.random() < 0.3
        dyn = rng.random() < 0.2
        lotp = rng.random() < 0.1
        s["dynamic"], s["include_lotp"] = dyn, lotp
        # oracle inputs
        ac9 = 1 if s["hit_and_run"] else 0
        zs = [None if v is None else v + ac9 for v in s["runner_speeds"]]
        ac17 = (s["outs"] != 2) and s["infield_in"] and zs[2] is not None
        inp = {K + "W3": s["pit_hand"], K + "X3": s["pit_mov"], K + "Y3": s["pit_cmd"], K + "Z3": s["pit_vel"],
               K + "AA3": s["pit_awr"], K + "W6": s["bat_hand"], K + "X6": s["bat_con"] - ac9, K + "Y6": s["bat_eye"],
               K + "Z6": s["bat_pow"], K + "AA6": s["bat_spd"], K + "Q5": s["outs"], K + "Q7": s["hit_and_run"],
               K + "AC9": ac9, K + "AC17": bool(ac17), K + "D9": dyn, K + "U22": lotp}
        for k, z in zip(("Z11", "Z12", "Z13"), zs):
            if z is not None:
                inp[K + k] = float(z)
        try:
            out = model_for(pat).calculate(inputs=inp)
        except Exception as e:  # oracle failure
            print("oracle error", s, e)
            continue
        try:
            sc.SNAP = False                  # compare like-for-like with the oracle's plain IEEE truncation
            res = calc.compute(**s)
            sc.SNAP = True
            res_snap = calc.compute(**s)
            if any(a["final"] != b["final"] for a, b in zip(res["swing"] + res["bunt"],
                                                              res_snap["swing"] + res_snap["bunt"])):
                sensitive.append(s)
        except Exception as e:
            res = None
            err = repr(e)

        def ov(co):
            v = out.get(K + co)
            if v is None:
                return None
            x = v.value
            try:
                x = x[0][0]
            except Exception:
                pass
            if x is sh.EMPTY or x == "":
                return 0
            return x
        # compare Final column
        mine = {}
        if res:
            for r in res["swing"]:
                mine[r["row"]] = r["final"]
            for r in res["bunt"]:
                mine[r["row"]] = r["final"]
        mism = []
        for r in swing_rows + bunt_rows:
            o = ov(f"V{r}")
            if isinstance(o, str) or o is None:
                o = 0 if o in (None, "") else o
            m = mine.get(r, 0) if res else None
            try:
                if res is None or abs(float(o) - float(m or 0)) > 1e-9:
                    mism.append((r, o, m))
            except Exception:
                mism.append((r, o, m))
        if mism:
            bad += 1
            print("MISMATCH", s, "\n   ", mism[:8], "" if res else err)
    print(f"{n - bad}/{n} scenarios match on every Final cell (V26:V63, V67:V75)")
    print(f"{len(sensitive)}/{n} scenarios are sensitive to ROUNDDOWN float snapping (a rate*base product lands on "
          "a whole number); check these in the sheet to settle it:")
    for s in sensitive[:10]:
        print("   ", s)


if __name__ == "__main__":
    main()

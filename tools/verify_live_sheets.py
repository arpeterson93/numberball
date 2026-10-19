"""Verify sheet_calculator.py against LIVE stadium game sheets (Gameplay tab, gid 533199361).

For every game with a sheet_url it reads the sheet's own calculator inputs (pitcher/batter builds, runner speeds,
outs, hit-and-run / infield-in flags) off the Gameplay tab, recomputes swing + bunt ranges and steal safe ranges with
the Python port, and compares them cell for cell with what the sheet itself displays.

Run:  python -I tools/verify_live_sheets.py            (all games with a sheet_url)
      python -I tools/verify_live_sheets.py 130820     (one game code)
Cell map (0-indexed pandas coords on the raw CSV export):
  pitcher  W12 hand, X12:AA12 MOV CMD VEL AWR       batter  W13 hand, X13:AA13 CON EYE PWR SPD
  runners  AA20:AA22 speeds (1B,2B,3B)              catcher EYE AA17      steal safe ranges AB20:AB22
  outs     X9 ("N outs")                            flags   CC5 hit&run, BG4 infield in
  swing table S17:V34 (name, final, low, high)      bunt table X26:AA34
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import database as db
import sheet_calculator as sc
import offense_sim as osim

GAMEPLAY_GID = "533199361"


def col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def cell(df, ref: str):
    m = re.match(r"([A-Z]+)(\d+)", ref)
    r, c = int(m.group(2)) - 1, col(m.group(1))
    try:
        v = str(df.iloc[r, c]).strip()
    except IndexError:
        return None
    return None if v.lower() in ("nan", "") else v


def num(df, ref, default=None):
    v = cell(df, ref)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def truthy(v) -> bool:
    return str(v).strip().upper() in ("TRUE", "1", "YES")


def table(df, name_col: str, lo_col: str, hi_col: str, r0: int, r1: int):
    out = []
    for r in range(r0, r1 + 1):
        nm = cell(df, f"{name_col}{r}")
        lo, hi = num(df, f"{lo_col}{r}"), num(df, f"{hi_col}{r}")
        if not nm or lo is None or hi is None:
            break
        out.append((nm, lo, hi))
    return out


def _runner_speeds(df) -> tuple:
    """Live mode: the calculator XLOOKUPs the runner ids in BE19:BE21 (1B, 2B, 3B) to import_Players SPD - here the
    SPD (col M) of the same id in the Gameplay lineup block A10:M34. Manual mode reads AA20:AA22 directly."""
    if (cell(df, "D2") or "").lower() == "manual":
        return tuple((lambda v: v if v else None)(num(df, f"AA{r}")) for r in (20, 21, 22))
    spd_by_id = {}
    for r in range(10, 35):
        pid, sp = cell(df, f"A{r}"), num(df, f"M{r}")
        if pid and sp is not None:
            spd_by_id[pid] = sp
    out = []
    for r in (19, 20, 21):
        rid = cell(df, f"BE{r}")
        out.append(spd_by_id.get(rid) if rid and rid not in ("-", "0") else None)
    return tuple(out)


def _outs(df) -> int:
    """X9 holds text such as "2 outs" (or a bare number) - the displayed current outs."""
    m = re.search(r"\d+", cell(df, "X9") or "")
    return int(m.group()) if m else 0


def read_inputs(df) -> dict:
    """Everything the calculator needs, straight off a Gameplay tab."""
    runners = _runner_speeds(df)
    return dict(
        pit_hand=cell(df, "W12") or "R", pit_mov=num(df, "X12"), pit_cmd=num(df, "Y12"),
        pit_vel=num(df, "Z12"), pit_awr=num(df, "AA12"),
        bat_hand=cell(df, "W13") or "R", bat_con=num(df, "X13"), bat_eye=num(df, "Y13"),
        bat_pow=num(df, "Z13"), bat_spd=num(df, "AA13"),
        runner_speeds=tuple(runners), outs=_outs(df),
        hit_and_run=truthy(cell(df, "CC5")), infield_in=truthy(cell(df, "BG4")),
    ), num(df, "AA17"), [num(df, "AB20"), num(df, "AB21"), num(df, "AB22")]


def verify_sheet(sheet_url: str, calc: sc.SheetCalculator, label: str) -> dict:
    sid = re.search(r"/spreadsheets/d/([^/]+)", sheet_url).group(1)
    df = pd.read_csv(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={GAMEPLAY_GID}",
                     header=None, dtype=str)
    kw, catcher_eye, sheet_steal = read_inputs(df)
    res = {"label": label, "inputs": kw, "swing_ok": None, "bunt_ok": None, "steal_ok": None, "notes": []}
    needed = ("pit_mov", "pit_cmd", "pit_vel", "pit_awr", "bat_con", "bat_eye", "bat_pow", "bat_spd")
    if any(kw[k] is None for k in needed):
        res["notes"].append("inputs missing on sheet")
        return res
    out = calc.compute(**kw)
    sheet_swing = table(df, "S", "U", "V", 17, 34)
    sheet_bunt = table(df, "X", "Z", "AA", 26, 34)
    mine_swing = [(n, lo, hi) for n, _f, lo, hi in out["swing_ranges"]]
    mine_bunt = [(n, lo, hi) for n, _f, lo, hi in out["bunt_ranges"]]
    res["swing_ok"] = mine_swing == sheet_swing
    res["bunt_ok"] = mine_bunt == sheet_bunt
    if not res["swing_ok"]:
        res["notes"].append(f"swing sheet={sheet_swing} mine={mine_swing}")
    if not res["bunt_ok"]:
        res["notes"].append(f"bunt sheet={sheet_bunt} mine={mine_bunt}")
    # steal safe ranges (only bases with a runner and a sheet value)
    if catcher_eye is not None:
        bad = []
        # offense_sim models steals of 2nd (runner on 1B) and 3rd (runner on 2B); home steals are not modelled
        for base, spd, sh in zip(("2nd", "3rd", None), kw["runner_speeds"], sheet_steal):
            if base and spd and sh is not None:
                mine = osim.steal_safe_range(base, spd, kw["pit_awr"], catcher_eye)
                if mine != sh:
                    bad.append((base, sh, mine))
        res["steal_ok"] = not bad
        if bad:
            res["notes"].append(f"steal (base, sheet, mine)={bad}")
    return res


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    calc = sc.SheetCalculator(sc.load_params_json(ROOT / "sheet_calculator_params.json"))
    games = [g for g in db.get_games() if g.get("sheet_url")]
    if only:
        games = [g for g in games if str(g.get("game_code")) == only]
    tot = {"swing": [0, 0], "bunt": [0, 0], "steal": [0, 0]}
    targets = [(f"{g.get('game_code')} {g.get('away_team')} @ {g.get('home_team')}", g["sheet_url"]) for g in games]
    # each stadium's scenario sheets (hit-and-run / infield-in / both / sandbox) are the same template, set up with
    # runners and flags, so they exercise the situations idle live sheets do not
    seen = set()
    for g in games:
        base = g["sheet_url"].split("/edit")[0]
        if base in seen:
            continue
        seen.add(base)
        try:
            sheets = db.get_stadium_sheets(base, g.get("season")) or {}
        except Exception:
            sheets = {}
        for k in ("sheet_hnr", "sheet_ifinfield", "sheet_hnr_ifin", "sheet_sandbox"):
            if sheets.get(k):
                targets.append((f"{g.get('home_team')} {k}", sheets[k]))
    for label, url in targets:
        try:
            r = verify_sheet(url, calc, label)
        except Exception as e:  # sheet private / not a game sheet
            print(f"[skip] {label}: {type(e).__name__}: {str(e)[:80]}")
            continue
        for k, key in (("swing", "swing_ok"), ("bunt", "bunt_ok"), ("steal", "steal_ok")):
            if r[key] is not None:
                tot[k][1] += 1
                tot[k][0] += bool(r[key])
        flag = lambda v: "-" if v is None else ("ok" if v else "MISMATCH")
        print(f"{label}: swing {flag(r['swing_ok'])}  bunt {flag(r['bunt_ok'])}  steal {flag(r['steal_ok'])}"
              f"  H&R={r['inputs']['hit_and_run']} IFin={r['inputs']['infield_in']} outs={r['inputs']['outs']}"
              f" runners={r['inputs']['runner_speeds']}")
        for n in r["notes"]:
            print("    ", n)
    print("\nTOTAL matches / checked:", {k: f"{a}/{b}" for k, (a, b) in tot.items()})


if __name__ == "__main__":
    main()

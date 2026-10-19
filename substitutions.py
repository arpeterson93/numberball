"""Substitution analysis for the Manager tab and the Discord /manager strategy call.

For a live game it asks: given the current pitcher, batter, catcher, runners, outs and score, would any legal
substitution improve the best available strategy? Hitting subs (pinch hitter, pinch runner) are scored for the batting
team, pitching subs (relief pitcher) and catching subs (a bench player who makes a better-EYE catcher legal) for the
fielding team. Subs are evaluated one at a time, never together: a hitter is assessed with the pitcher held constant and
vice versa.

Pieces it reuses rather than reimplements:
  * sheet_calculator.SheetCalculator - the exact port of the stadium sheet's calculator tab (swing / bunt / hit-and-run /
    infield-in ranges), here driven with hypothetical players. Verified against live stadium sheets with
    tools/verify_live_sheets.py.
  * manager_calc.build_strategy_table - the Manager tab's own expected runs / win-probability math. It is fed a
    synthetic state whose ranges come from the calculator, so a substitution is priced exactly like the live table.
  * lineup_rules - rulebook Section 8 position eligibility and the pinch-hitter DH rule.

Rules baked in (confirmed by the league): a replaced player cannot return; "used" players are everyone who has appeared
in the game (plays table) or is in the current lineup / on the mound; only players on the team's Active roster are bench
options; position players do not pitch and pitchers do not bat (both would play at 0/0/0/0 and are rare).
"""
from __future__ import annotations

import io
import re
import urllib.parse
from pathlib import Path

import pandas as pd

import lineup_rules
import manager_calc
import sheet_calculator as sc

ROOT = Path(__file__).resolve().parent
_CALC: sc.SheetCalculator | None = None

BANDS_BASE = ("1B", "2B", "3B")          # runner-speed order the calculator uses (Z11:Z13)
_BASE_ORDER = {"3B": 0, "2B": 1, "1B": 2}  # matches utils.parse_gameplay_from_sheet's steal_runners ordering
MIN_WP_GAIN = 0.0005                      # a sub must beat the baseline by at least 0.05 pp of win probability
MIN_ER_GAIN = 0.005                       # (or 0.005 runs when no win-probability table is loaded)


def calculator() -> sc.SheetCalculator:
    global _CALC
    if _CALC is None:
        _CALC = sc.SheetCalculator(sc.load_params_json(ROOT / "sheet_calculator_params.json"))
    return _CALC


# ------------------------------------------------------------------------------------------------ steal ranges

def steal_safe_range(base: str, runner_spd: float, pit_awr: float, catcher_eye: float) -> int:
    """Sheet steal table (calculator!A92:W95): the runner on `base` going to the next one, vs the battery.
    Matchup = runner SPD - (pitcher AWR + catcher EYE) / 2, clamped to the table's -5..+5."""
    p = calculator().p
    row = {"1B": 93, "2B": 94, "3B": 95}[base]          # 2nd / 3rd / Home rows of the table
    aa = max(-5.0, min(5.0, runner_spd - (pit_awr + catcher_eye) / 2.0))
    for c in "CDEFGHIJKLMNOPQRSTUVW":
        if float(p[f"{c}92"]) == aa:
            return int(p[f"{c}{row}"])
    raise ValueError(f"steal matchup {aa} not in table")


# ---------------------------------------------------------------------------------------------------- sheet fetch

def _fetch_csv_gid(sid: str, gid: str) -> pd.DataFrame:
    return pd.read_csv(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}",
                       header=None, dtype=str)


def _fetch_csv_tab(sid: str, tab: str) -> pd.DataFrame:
    return pd.read_csv(f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}",
                       header=None, dtype=str)


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell(df: pd.DataFrame, ref: str) -> str | None:
    m = re.match(r"([A-Z]+)(\d+)$", ref)
    try:
        v = str(df.iloc[int(m.group(2)) - 1, _col(m.group(1))]).strip()
    except (IndexError, AttributeError):
        return None
    return None if v.lower() in ("nan", "") else v


def _clean(name: str | None) -> str:
    """Roster names carry a ' - C' captain tag the lineup / player tables do not."""
    return re.sub(r"\s+-\s+C$", "", (name or "").strip()).strip()


def _pid(v) -> str | None:
    """Normalise a sheet / plays player id ('08021', 8021, '8021.0') to a bare digit string."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "0", "nan", "None"):
        return None
    try:
        return str(int(float(s)))
    except ValueError:
        return None


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def fetch_sheet_context(sheet_url: str) -> dict:
    """Everything the sheet knows about the two teams right now: current lineups, rosters (with the sheet's own 'used'
    flag), every player's attributes, and the live situation block (pitcher / catcher / batter / runners / batting team).
    """
    sid = re.search(r"/spreadsheets/d/([^/]+)", sheet_url).group(1)
    gp = _fetch_csv_gid(sid, "533199361")
    lu = _fetch_csv_tab(sid, "import_Lineups")
    pl = _fetch_csv_tab(sid, "import_Players")

    # live situation block on the Gameplay tab (rows 15-21, cols BD:BG)
    current = {
        "inning": _cell(gp, "BE15"), "batting_team": _cell(gp, "BG15"),
        "pitcher_id": _pid(_cell(gp, "BE16")), "catcher_id": _pid(_cell(gp, "BE17")),
        "batter_id": _pid(_cell(gp, "BE18")),
        "runner_ids": {b: _pid(_cell(gp, f"BE{r}")) for b, r in zip(BANDS_BASE, (19, 20, 21))},
    }

    # players: import_Players (header row 0) -> id, team, name, status, primary, secondary, hand, stats
    players: dict[str, dict] = {}
    for _, r in pl.iloc[1:].iterrows():
        pid = _pid(r[0])
        name = _clean(r[3])
        prim = (str(r[7]) if str(r[7]) != "nan" else "").strip()
        if not pid or not name:
            continue
        players.setdefault(pid, {
            "id": pid, "team": None if str(r[1]) == "nan" else str(r[1]).strip(), "name": name,
            "status": (str(r[6]) if str(r[6]) != "nan" else "").strip(),
            "prim": prim, "sec": (str(r[8]) if str(r[8]) != "nan" else "").strip(),
            "hand": (str(r[9]) if str(r[9]) != "nan" else "R").strip() or "R",
            "con": _int(r[10]), "eye": _int(r[11]), "pwr": _int(r[12]), "spd": _int(r[13]),
            "mov": _int(r[14]), "cmd": _int(r[15]), "vel": _int(r[16]), "awr": _int(r[17]),
        })
    by_team_name: dict[tuple[str, str], dict] = {}
    for p in players.values():
        if p["team"] and p["prim"] not in ("GM", ""):
            by_team_name[(p["team"], p["name"].lower())] = p

    # current lineups (import_Lineups cols AG:AJ, rows 3-20): team, order, player, position
    lineups: dict[str, list[dict]] = {}
    for i in range(2, min(len(lu), 40)):
        team, order, name, pos = (str(lu.iloc[i, j]).strip() for j in (32, 33, 34, 35))
        if team in ("nan", "") or not order.isdigit():
            continue
        lineups.setdefault(team, []).append({"order": int(order), "name": _clean(name), "pos": pos})
    for t in lineups:
        lineups[t].sort(key=lambda x: x["order"])

    # rosters (two side-by-side blocks): flag col, team, name, primary, secondary
    rosters: dict[str, list[dict]] = {}
    for flag_c, tm_c in ((14, 15), (21, 22)):
        for i in range(3, len(lu)):
            tm, name = str(lu.iloc[i, tm_c]).strip(), str(lu.iloc[i, tm_c + 1]).strip()
            if tm in ("nan", "") or name in ("nan", ""):
                continue
            rosters.setdefault(tm, []).append({
                "name": _clean(name), "pos": str(lu.iloc[i, tm_c + 2]).strip(),
                "sec": str(lu.iloc[i, tm_c + 3]).strip(), "used_flag": str(lu.iloc[i, flag_c]).strip() == "Y"})
    return {"current": current, "players": players, "by_team_name": by_team_name,
            "lineups": lineups, "rosters": rosters}


def played_ids(plays: list[dict]) -> set[str]:
    """Every player who has appeared in the game so far (batter, pitcher, catcher, runner, or on base)."""
    out: set[str] = set()
    for p in plays or []:
        for k in ("batter_id", "pitcher_id", "catcher_id", "runner_id", "on_first", "on_second", "on_third"):
            v = _pid(p.get(k))
            if v:
                out.add(v)
    return out


# ------------------------------------------------------------------------------------------- scenario evaluation

def _ranges(rows) -> list[tuple[str, int, int]]:
    return [(n, lo, hi) for n, _f, lo, hi in rows]


def scenario_state(base_state: dict, pitcher: dict, batter: dict, catcher_eye: int,
                   runner_spds: dict[str, int], *, if_in_checked: bool = False) -> dict:
    """A copy of the live Manager state whose ranges / steal ranges come from the calculator for these exact players,
    ready for manager_calc.build_strategy_table."""
    calc = calculator()
    obc, outs = base_state.get("obc", "000"), base_state.get("outs", 0)
    rs = tuple(runner_spds.get(b) for b in BANDS_BASE)
    kw = dict(pit_hand=pitcher["hand"], pit_mov=pitcher["mov"], pit_cmd=pitcher["cmd"], pit_vel=pitcher["vel"],
              pit_awr=pitcher["awr"], bat_hand=batter["hand"], bat_con=batter["con"], bat_eye=batter["eye"],
              bat_pow=batter["pwr"], bat_spd=batter["spd"], runner_speeds=rs, outs=outs)
    normal = calc.compute(**kw)
    steal_runners = []
    for b in ("3B", "2B", "1B"):                      # most advanced runner first, as the live parser orders them
        if runner_spds.get(b):
            steal_runners.append({"base": b, "safe_range": steal_safe_range(b, runner_spds[b], pitcher["awr"], catcher_eye)})
    s = dict(base_state)
    s.update({
        "result_ranges": _ranges(normal["swing_ranges"]), "bunt_ranges": _ranges(normal["bunt_ranges"]),
        "steal_runners": steal_runners, "runner_spds": dict(runner_spds),
        "pitcher_row": pitcher, "batter_row": batter,
        # scenario-sheet ranges are replaced by calculator output for these players
        "if_in_ranges": (_ranges(calc.compute(**kw, infield_in=True)["swing_ranges"])
                         if obc[0] == "1" and outs < 2 else None),
        "hnr_ranges": None, "hnr_ifin_ranges": None, "sandbox_ranges": None,
    })
    if obc in manager_calc._HNR_VALID_OBCS and outs < 2 and steal_runners:
        s["hnr_ranges"] = _ranges(calc.compute(**kw, hit_and_run=True)["swing_ranges"])
        s["hnr_ifin_ranges"] = (_ranges(calc.compute(**kw, hit_and_run=True, infield_in=True)["swing_ranges"])
                                if obc[0] == "1" else None)
    return s


def _best(table: dict) -> dict:
    rows = table["rows"]
    if any(r["exp_wp"] is not None for r in rows):
        return max(rows, key=lambda r: r["exp_wp"] if r["exp_wp"] is not None else -1.0)
    return max(rows, key=lambda r: r["exp_runs"])


def _eval(base_state, run_lookup, pitcher, batter, catcher_eye, runner_spds, if_in_checked) -> dict:
    t = manager_calc.build_strategy_table(
        scenario_state(base_state, pitcher, batter, catcher_eye, runner_spds, if_in_checked=if_in_checked),
        run_lookup, if_in_checked=if_in_checked)
    b = _best(t)
    return {"table": t, "best": b["decision"], "exp_wp": b["exp_wp"], "exp_runs": b["exp_runs"]}


# ---------------------------------------------------------------------------------------------- main entry point

def _stats_ok(p: dict, keys) -> bool:
    return all(p.get(k) is not None for k in keys)


def _bat_total(p: dict) -> int:
    return sum(p.get(k) or 0 for k in ("con", "eye", "pwr", "spd"))


def analyze_substitutions(state: dict, run_lookup: dict, ctx: dict, plays: list[dict] | None = None,
                          if_in_checked: bool = False, top_n: int = 5) -> dict:
    """See module docstring. state = scouting_data.resolve_game_state; ctx = fetch_sheet_context;
    plays = db.get_plays_for_game (for the used-player set).

    Returns {"baseline": {...}, "hitting": [...], "pitching": [...], "catching": [...], "verified": {...},
    "teams": {...}, "notes": [...]}. Each candidate is {"kind", "team", "in", "out", "desc", "best", "exp_wp",
    "exp_runs", "d_wp", "d_runs", "helps"} where d_* are from the BATTING team's point of view (so a defensive sub
    helps when d_* is negative) and helps is from the sub's own team's point of view."""
    cur = ctx["current"]
    players = ctx["players"]
    notes: list[str] = []

    def get(pid):
        return players.get(pid) if pid else None

    pitcher, batter, catcher = get(cur["pitcher_id"]), get(cur["batter_id"]), get(cur["catcher_id"])
    if not (pitcher and batter and catcher and _stats_ok(pitcher, ("mov", "cmd", "vel", "awr")) and
            _stats_ok(batter, ("con", "eye", "pwr", "spd")) and catcher.get("eye") is not None):
        return {"error": "Couldn't read the current pitcher / batter / catcher off the sheet.", "notes": notes}

    bat_team = cur["batting_team"] or batter["team"]
    teams = list(ctx["lineups"])
    field_team = next((t for t in teams if t != bat_team), None)
    runner_ids = cur["runner_ids"]
    runner_spds = {b: (get(rid) or {}).get("spd") for b, rid in runner_ids.items() if rid}
    runner_spds = {b: s for b, s in runner_spds.items() if s}

    used = played_ids(plays or [])
    used |= {x for x in (cur["pitcher_id"], cur["catcher_id"], cur["batter_id"], *runner_ids.values()) if x}
    for t in teams:                                    # everyone in the current lineups has appeared / is in
        for slot in ctx["lineups"][t]:
            p = ctx["by_team_name"].get((t, slot["name"].lower()))
            if p:
                used.add(p["id"])
    flagged = {p["id"] for t in teams for r in ctx["rosters"].get(t, []) if r["used_flag"]
               for p in [ctx["by_team_name"].get((t, r["name"].lower()))] if p}
    if flagged - used:
        notes.append("The sheet's own 'used' flag marks " + ", ".join(sorted(players[i]["name"] for i in flagged - used)) +
                     " who aren't in the plays list yet; treated as used.")
        used |= flagged

    def bench(team: str, want: str) -> list[dict]:
        out, seen_ids = [], set()
        for r in ctx["rosters"].get(team, []):
            p = ctx["by_team_name"].get((team, r["name"].lower()))
            if not p or p["id"] in used or p["status"] != "Active" or p["id"] in seen_ids:
                continue                                  # (a GM who also plays is listed twice on the roster sheet)
            seen_ids.add(p["id"])
            if want == "P" and p["prim"] == "P" and _stats_ok(p, ("mov", "cmd", "vel", "awr")):
                out.append(p)
            elif want == "H" and p["prim"] not in ("P", "GM", "") and _stats_ok(p, ("con", "eye", "pwr", "spd")):
                out.append(p)
        return out

    def lineup_players(team: str) -> list[dict]:
        res = []
        for slot in ctx["lineups"].get(team, []):
            p = ctx["by_team_name"].get((team, slot["name"].lower()))
            res.append({"name": slot["name"], "prim": (p or {}).get("prim"), "sec": (p or {}).get("sec"),
                        "pos": slot["pos"], "p": p})
        return res

    base_eval = _eval(state, run_lookup, pitcher, batter, catcher["eye"], runner_spds, if_in_checked)
    base_wp, base_er = base_eval["exp_wp"], base_eval["exp_runs"]
    use_wp = base_wp is not None

    def entry(kind, team, p_in, p_out, desc, ev, side):
        d_wp = (ev["exp_wp"] - base_wp) if use_wp and ev["exp_wp"] is not None else None
        d_er = ev["exp_runs"] - base_er
        gain = (d_wp if use_wp else d_er) * (1 if side == "bat" else -1)
        thresh = MIN_WP_GAIN if use_wp else MIN_ER_GAIN
        return {"kind": kind, "team": team, "in": p_in["name"], "out": p_out["name"] if p_out else None, "desc": desc,
                "best": ev["best"], "exp_wp": ev["exp_wp"], "exp_runs": ev["exp_runs"],
                "d_wp": d_wp, "d_runs": d_er, "gain": gain, "helps": gain > thresh,
                "bat_change": ((_bat_total(p_in) - _bat_total(p_out)) if p_out and kind in ("PH", "PR", "DEF") else None)}

    hitting: list[dict] = []
    pitching: list[dict] = []
    catching: list[dict] = []

    # ---- hitting subs (batting team): pinch hitter, pinch runner
    lu_b = lineup_players(bat_team)
    dh_b = next((x["name"] for x in lu_b if x["pos"] == "DH"), None)
    cur_batter_slot = next((x for x in lu_b if x["name"] == batter["name"]), None)
    for x in bench(bat_team, "H"):
        dh_ok = {n for n in (dh_b, x["name"]) if n}
        if cur_batter_slot is not None:
            new_lu = [{"name": s["name"], "prim": s["prim"], "sec": s["sec"]} for s in lu_b if s["name"] != batter["name"]]
            new_lu.append({"name": x["name"], "prim": x["prim"], "sec": x["sec"]})
            if lineup_rules.is_legal(new_lu, dh_ok):
                ev = _eval(state, run_lookup, pitcher, x, catcher["eye"], runner_spds, if_in_checked)
                hitting.append(entry("PH", bat_team, x, batter, f"{x['name']} hits for {batter['name']}", ev, "bat"))
        for b, rid in runner_ids.items():
            r = get(rid)
            if not r or b not in runner_spds:
                continue
            slot = next((s for s in lu_b if s["name"] == r["name"]), None)
            if slot is None:
                continue                                     # ghost runner: cannot be pinch-run for
            new_lu = [{"name": s["name"], "prim": s["prim"], "sec": s["sec"]} for s in lu_b if s["name"] != r["name"]]
            new_lu.append({"name": x["name"], "prim": x["prim"], "sec": x["sec"]})
            if lineup_rules.is_legal(new_lu, dh_ok):
                spds = dict(runner_spds)
                spds[b] = x["spd"]
                ev = _eval(state, run_lookup, pitcher, batter, catcher["eye"], spds, if_in_checked)
                hitting.append(entry("PR", bat_team, x, r, f"{x['name']} runs for {r['name']} ({b})", ev, "bat"))

    # ---- pitching subs (fielding team): relief pitcher
    for y in bench(field_team, "P"):
        ev = _eval(state, run_lookup, y, batter, catcher["eye"], runner_spds, if_in_checked)
        pitching.append(entry("RP", field_team, y, pitcher, f"{y['name']} relieves {pitcher['name']}", ev, "field"))

    # ---- catching subs (fielding team): a bench player enters, the legal re-shuffle that maximises catcher EYE
    if runner_spds:
        lu_f = lineup_players(field_team)
        dh_f = next((x["name"] for x in lu_f if x["pos"] == "DH"), None)
        seen: dict[tuple[str, str], dict] = {}
        for x in bench(field_team, "H"):
            for out_slot in lu_f:
                p_out = out_slot["p"]
                if p_out is None:
                    continue
                new_lu = [{"name": s["name"], "prim": s["prim"], "sec": s["sec"]} for s in lu_f if s["name"] != out_slot["name"]]
                new_lu.append({"name": x["name"], "prim": x["prim"], "sec": x["sec"]})
                dh_ok = {n for n in (dh_f, x["name"]) if n}
                if not lineup_rules.is_legal(new_lu, dh_ok):
                    continue
                eyes = {n: (ctx["by_team_name"].get((field_team, n.lower())) or {}).get("eye")
                        for n in lineup_rules.possible_at(new_lu, "C", dh_ok)}
                eyes = {n: e for n, e in eyes.items() if e is not None}
                if not eyes:
                    continue
                c_name = max(eyes, key=lambda n: eyes[n])
                if eyes[c_name] <= catcher["eye"]:
                    continue
                key = (x["name"], c_name)
                cost = _bat_total(p_out)
                # cheapest bat to give up for the same outcome
                if key not in seen or cost < seen[key]["cost"]:
                    seen[key] = {"x": x, "out": p_out, "catcher": c_name, "eye": eyes[c_name], "cost": cost}
        for (xn, cn), s in seen.items():
            ev = _eval(state, run_lookup, pitcher, batter, s["eye"], runner_spds, if_in_checked)
            desc = (f"{xn} in for {s['out']['name']}; {cn} catches (EYE {s['eye']} vs {catcher['eye']})"
                    if cn != xn else f"{xn} in for {s['out']['name']} and catches (EYE {s['eye']} vs {catcher['eye']})")
            catching.append(entry("DEF", field_team, s["x"], s["out"], desc, ev, "field"))

    # ---- verification: does the calculator reproduce the sheet's own ranges for the live situation?
    verified = {"swing": None, "bunt": None, "detail": None}
    calc_live = scenario_state(state, pitcher, batter, catcher["eye"], runner_spds, if_in_checked=if_in_checked)
    if state.get("result_ranges"):
        sheet_swing = [(n, lo, hi) for n, lo, hi in (manager_calc._norm(e) for e in state["result_ranges"])]
        verified["swing"] = sheet_swing == calc_live["result_ranges"]
    if state.get("bunt_ranges"):
        sheet_bunt = [(n, lo, hi) for n, lo, hi in (manager_calc._norm(e) for e in state["bunt_ranges"])]
        verified["bunt"] = sheet_bunt == calc_live["bunt_ranges"]
    if verified["swing"] is False or verified["bunt"] is False:
        verified["detail"] = "calculator ranges differ from the sheet for the live situation - treat results with caution"

    def rank(lst):
        return sorted(lst, key=lambda e: -e["gain"])

    return {
        "baseline": {"best": base_eval["best"], "exp_wp": base_wp, "exp_runs": base_er, "table": base_eval["table"],
                     "pitcher": pitcher["name"], "batter": batter["name"], "catcher": catcher["name"],
                     "catcher_eye": catcher["eye"], "runners": {b: (get(rid) or {}).get("name") for b, rid in runner_ids.items() if rid}},
        "hitting": rank(hitting), "pitching": rank(pitching), "catching": rank(catching),
        "teams": {"batting": bat_team, "fielding": field_team},
        "bench": {t: [p["name"] for p in bench(t, "H")] for t in teams} | {f"{t}_P": [p["name"] for p in bench(t, "P")] for t in teams},
        "verified": verified, "notes": notes, "top_n": top_n, "uses_wp": use_wp,
    }


def analyze_game(game_code: int, if_in_checked: bool = False) -> dict:
    """Convenience wrapper for the bot / scripts: resolve the live state + sheet context + plays and analyse."""
    import database as db
    import scouting_data
    state = scouting_data.resolve_game_state(game_code)
    if not state:
        return {"error": f"Couldn't read game state for {game_code} (no sheet on file)."}
    game = db.get_game_by_code(game_code) or {}
    ctx = fetch_sheet_context(game["sheet_url"])
    plays = db.get_plays_for_game(state["game_id"]) or []
    return analyze_substitutions(state, scouting_data.load_run_lookup(), ctx, plays, if_in_checked=if_in_checked)


def analyze_sheet(sheet_url: str, base_state: dict, run_lookup: dict, plays: list[dict] | None = None,
                  if_in_checked: bool = False) -> dict:
    """Same analysis for callers that already hold the live situation (the Streamlit Manager tab): base_state needs
    obc, outs, inning, half, away_score, home_score and league - the keys manager_calc.build_strategy_table reads."""
    return analyze_substitutions(base_state, run_lookup, fetch_sheet_context(sheet_url), plays, if_in_checked)


def _pct(v, signed=False):
    if v is None:
        return "-"
    return f"{v * 100:+.2f}" if signed else f"{v * 100:.1f}%"


def sub_rows(result: dict, section: str, limit: int | None = None) -> list[dict]:
    """Display rows for one section ("hitting" | "pitching" | "catching"): best candidates first. d_wp / d_runs are
    shown from the SUB'S OWN team's point of view (so + always means the sub helps its team); the strategy column is
    the best the batting team can then do. Only the helpful ones when any exist, else the single least-bad candidate."""
    items = result.get(section) or []
    side = 1 if section == "hitting" else -1
    helpful = [e for e in items if e["helps"]]
    shown = (helpful or items[:1])[: (limit or result.get("top_n") or 5)]
    rows = []
    for e in shown:
        rows.append({
            "Sub": e["desc"], "Best strategy": e["best"], "Exp WP": _pct(e["exp_wp"]),
            "WP vs now (pp)": _pct(None if e["d_wp"] is None else e["d_wp"] * side, signed=True),
            "Exp Runs": f"{e['exp_runs']:.2f}", "Runs vs now": f"{e['d_runs'] * side:+.3f}",
            "Bat change": "-" if e["bat_change"] is None else f"{e['bat_change']:+d}", "helps": e["helps"],
        })
    return rows

"""Expected-value / win-probability math for the Manager tab's strategy table.

Extracted from the nested functions in the Manager tab of pages/2_Scouting.py
(_calc_ev_and_probs, _calc_wp_after, etc.), which closed over page session
state - these take that same state as explicit arguments instead, so the
Discord bot can call the identical math without a Streamlit run.
"""
from __future__ import annotations

import utils


def _norm(entry):
    if isinstance(entry, dict):
        return entry["result"], entry["low"], entry["high"]
    return entry


def _lookup(run_lookup: dict, result: str, obc: str, outs: int):
    """(runs, new_obc, nout_after) from the imported BRC table, falling back
    to utils.advance_runners when a (result, obc, outs) combo isn't in it."""
    entry = run_lookup.get((result, obc, outs))
    if entry is not None and len(entry) == 3:
        return entry
    new_obc, _ = utils.advance_runners(result, obc, outs)
    return 0.0, new_obc, min(outs + utils.outs_added(result), 3)


def calc_ev_and_probs(run_lookup: dict, ranges: list, current_obc: str, current_outs: int):
    """Expected runs (this PA + rest of inning) and P(1R)/P(2R)/P(3+R) for a
    swing/bunt/infield-in range table."""
    ev = 0.0
    tprobs: dict[int, float] = {}
    for entry in (ranges or []):
        r, lo, hi = _norm(entry)
        prob = min((hi - lo + 1) * 2 / 1000, 1.0)
        runs, nobc, nout = _lookup(run_lookup, r, current_obc, current_outs)
        nout = min(nout, 3)
        ner = utils.get_expected_runs(nout, nobc) or 0 if nout < 3 else 0
        ev += prob * (runs + ner)
        imm = int(runs)
        adist = utils._re_dist.get((nout, nobc), {0: 1.0}) if nout < 3 else {0: 1.0}
        for add, p2 in adist.items():
            n = imm + add
            tprobs[n] = tprobs.get(n, 0.0) + prob * p2
    p1r = tprobs.get(1, 0.0)
    p2r = tprobs.get(2, 0.0)
    p3pr = sum(p for n, p in tprobs.items() if n >= 3)
    return ev, p1r, p2r, p3pr


def calc_steal_ev_and_probs(current_obc: str, current_outs: int, safe_range: int):
    safe_prob = min(safe_range * 2 / 1000, 1.0)
    out_prob = 1.0 - safe_prob
    safe_obc, safe_runs = utils.steal_advance(current_obc, current_outs)
    safe_ner = utils.get_expected_runs(current_outs, safe_obc) or 0
    out_obc, _ = utils.steal_cs(current_obc)
    out_nout = min(current_outs + 1, 3)
    out_ner = utils.get_expected_runs(out_nout, out_obc) or 0 if out_nout < 3 else 0
    ev = safe_prob * (safe_runs + safe_ner) + out_prob * out_ner
    tprobs: dict[int, float] = {}
    simm = int(safe_runs)
    sadist = utils._re_dist.get((current_outs, safe_obc), {0: 1.0})
    for add, p2 in sadist.items():
        n = simm + add
        tprobs[n] = tprobs.get(n, 0.0) + safe_prob * p2
    oadist = utils._re_dist.get((out_nout, out_obc), {0: 1.0}) if out_nout < 3 else {0: 1.0}
    for add, p2 in oadist.items():
        tprobs[add] = tprobs.get(add, 0.0) + out_prob * p2
    p1r = tprobs.get(1, 0.0)
    p2r = tprobs.get(2, 0.0)
    p3pr = sum(p for n, p in tprobs.items() if n >= 3)
    return ev, p1r, p2r, p3pr


def _hnr_steal_advance_obc(obc: str) -> tuple[str, int]:
    """Advance the H&R runner on a successful steal-on-K."""
    on_3b, on_2b, on_1b = obc[0] == "1", obc[1] == "1", obc[2] == "1"
    if on_1b and on_2b:
        return "110", 0
    if on_1b:
        return f"{obc[0]}10", 0
    elif on_2b:
        return "100", 0
    elif on_3b:
        return "000", 1
    return obc, 0


def _hnr_steal_cs_obc(obc: str) -> str:
    """OBC after the H&R runner is caught stealing."""
    if obc[1] == "1" and obc[2] == "1":
        return "010"
    if obc[2] == "1":
        return f"{obc[0]}00"
    elif obc[1] == "1":
        return f"{obc[0]}00"
    else:
        return "000"


def calc_ev_hnr_and_probs(run_lookup: dict, current_obc: str, current_outs: int,
                           hnr_ranges: list, hnr_k_steal_safe_rng: int, no_steal: bool = False):
    """EV for hit and run: non-K outcomes use BRC; K steal uses normal speed (no +1 boost).

    no_steal=True collapses the K branch's safe/caught-stealing split into a
    plain K (same run_lookup entry a normal swing's K uses, no runner move,
    no extra out) - models holding the runner on a swinging/called K instead
    of sending him, so the table shows the contact-play upside of the
    hit-and-run without also pricing in the swinging-K caught-stealing risk.
    """
    sp = min(hnr_k_steal_safe_rng * 2 / 1000, 1.0)
    op = 1.0 - sp
    ev = 0.0
    tprobs: dict[int, float] = {}
    for entry in (hnr_ranges or []):
        r, lo, hi = _norm(entry)
        prob = min((hi - lo + 1) * 2 / 1000, 1.0)
        if r == "K" and not no_steal:
            _, _, k_nout = _lookup(run_lookup, "K", current_obc, current_outs)
            k_nout = min(k_nout, 3)
            s_obc, s_runs = _hnr_steal_advance_obc(current_obc)
            s_ner = utils.get_expected_runs(k_nout, s_obc) or 0 if k_nout < 3 else 0
            cs_nout = min(k_nout + 1, 3)
            cs_obc = "000" if cs_nout >= 3 else _hnr_steal_cs_obc(current_obc)
            cs_ner = utils.get_expected_runs(cs_nout, cs_obc) or 0 if cs_nout < 3 else 0
            ev += prob * (sp * (s_runs + s_ner) + op * cs_ner)
            simm = int(s_runs)
            sadist = utils._re_dist.get((k_nout, s_obc), {0: 1.0}) if k_nout < 3 else {0: 1.0}
            for add, p2 in sadist.items():
                n = simm + add
                tprobs[n] = tprobs.get(n, 0.0) + prob * sp * p2
            csdist = utils._re_dist.get((cs_nout, cs_obc), {0: 1.0}) if cs_nout < 3 else {0: 1.0}
            for add, p2 in csdist.items():
                tprobs[add] = tprobs.get(add, 0.0) + prob * op * p2
        else:
            runs, new_obc, nout = _lookup(run_lookup, r, current_obc, current_outs)
            nout = min(nout, 3)
            ner = utils.get_expected_runs(nout, new_obc) or 0 if nout < 3 else 0
            ev += prob * (runs + ner)
            imm = int(runs)
            adist = utils._re_dist.get((nout, new_obc), {0: 1.0}) if nout < 3 else {0: 1.0}
            for add, p2 in adist.items():
                n = imm + add
                tprobs[n] = tprobs.get(n, 0.0) + prob * p2
    p1r = tprobs.get(1, 0.0)
    p2r = tprobs.get(2, 0.0)
    p3pr = sum(p for n, p in tprobs.items() if n >= 3)
    return ev, p1r, p2r, p3pr


def wp_for_state(remaining: int, outs: int, obc: str, batting_lead: int) -> float:
    """WP for the batting team given a post-play state. Handles inning-end team switch."""
    outs = min(outs, 3)
    if outs < 3:
        return utils.get_win_probability_interpolated(remaining, outs, obc, batting_lead) or 0.5
    if remaining > 1:
        return 1.0 - (utils.get_win_probability_interpolated(remaining - 1, 0, "000", -batting_lead) or 0.5)
    return 1.0 if batting_lead > 0 else (0.5 if batting_lead == 0 else 0.0)


def calc_wp_after(run_lookup: dict, ranges: list, current_obc: str, current_outs: int,
                   remaining: int, batting_lead: int) -> float:
    total = 0.0
    for entry in (ranges or []):
        r, lo, hi = _norm(entry)
        prob = min((hi - lo + 1) * 2 / 1000, 1.0)
        runs_f, new_obc, new_outs = _lookup(run_lookup, r, current_obc, current_outs)
        new_bl = batting_lead + int(round(runs_f))
        total += prob * wp_for_state(remaining, min(new_outs, 3), new_obc, new_bl)
    return total


def calc_steal_wp_after(current_obc: str, current_outs: int, steal_ev_rng: int,
                         remaining: int, batting_lead: int) -> float:
    safe_prob = min(steal_ev_rng * 2 / 1000, 1.0)
    out_prob = 1.0 - safe_prob
    safe_obc, safe_runs = utils.steal_advance(current_obc, current_outs)
    cs_obc, _ = utils.steal_cs(current_obc)
    cs_nout = min(current_outs + 1, 3)
    safe_wp = wp_for_state(remaining, current_outs, safe_obc, batting_lead + int(safe_runs))
    cs_wp = wp_for_state(remaining, cs_nout, cs_obc, batting_lead)
    return safe_prob * safe_wp + out_prob * cs_wp


def _stat(row: dict, key: str, default: int = 3) -> int:
    v = row.get(key)
    return int(v) if v is not None else default


def _hand(row: dict) -> str:
    h = str(row.get("hand", "R")).upper()
    return h if h in ("L", "R", "S") else "R"


_HNR_VALID_OBCS = {"001", "010", "011", "101"}

_SANDBOX_COMPARE_FIELDS = [
    ("pitcher", "sandbox_pitcher", "Pitcher"),
    ("batter", "sandbox_batter", "Batter"),
    ("catcher", "sandbox_catcher", "Catcher"),
]


def _safe_ranges_by_base(runners: list | None) -> dict[str, int]:
    return {r["base"]: r.get("safe_range") for r in (runners or []) if r.get("base")}


def compare_sandbox_inputs(state: dict) -> list[str]:
    """Which inputs the sandbox sheet's own Gameday/Gameplay cells disagree on
    vs. the live game's current ones - pitcher/batter/catcher, the hit-and-run
    and infield-in flags, baserunner presence on each base, and (for a base
    occupied in BOTH) that runner's steal safe range. Empty list means the
    sandbox sheet still looks like an unedited clone of the live situation
    (nothing new to show).

    A safe-range diff only fires when a runner is on the same base in both -
    a presence difference on that base is already caught by the "Runner on
    XB" check below, so this never double-reports the same edit."""
    diffs = []
    for live_key, sandbox_key, label in _SANDBOX_COMPARE_FIELDS:
        if state.get(live_key) != state.get(sandbox_key):
            diffs.append(label)

    if (state.get("swing_type") == "Hit and Run") != (state.get("sandbox_swing_type") == "Hit and Run"):
        diffs.append("Hit and Run")

    if bool(state.get("infield_in")) != bool(state.get("sandbox_infield_in")):
        diffs.append("Infield In")

    live_obc = state.get("obc") or "000"
    sandbox_obc = state.get("sandbox_obc") or "000"
    for idx, label in ((2, "Runner on 1B"), (1, "Runner on 2B"), (0, "Runner on 3B")):
        if live_obc[idx] != sandbox_obc[idx]:
            diffs.append(label)

    live_sr = _safe_ranges_by_base(state.get("steal_runners"))
    sandbox_sr = _safe_ranges_by_base(state.get("sandbox_steal_runners"))
    for base, label in (("1B", "1B Safe Range"), ("2B", "2B Safe Range"), ("3B", "3B Safe Range")):
        if base in live_sr and base in sandbox_sr and live_sr[base] != sandbox_sr[base]:
            diffs.append(label)

    return diffs


def build_strategy_table(state: dict, run_lookup: dict, if_in_checked: bool = False,
                          hnr_no_steal: bool = False) -> dict:
    """Build the Manager tab's EV summary table (Decision/Exp WP/Exp Runs/
    P(1R)/P(2R)/P(3+R), one row per applicable strategy) from a resolved game
    state (scouting_data.resolve_game_state).

    HNR and Infield-In ranges use the stadium's own scenario sheets when
    state has them (set by resolve_game_state from db.get_stadium_sheets),
    else fall back to utils.compute_at_bat_ranges from the pitcher/batter's
    stats and on-base runner speeds, same precedence the Manager tab uses.

    Returns {"rows": [{"decision", "exp_wp", "exp_runs", "p1r", "p2r", "p3pr"}, ...],
    "current_wp", "leverage" (either may be None), "baseline_er", "pitcher",
    "batter", "outs", "obc"}.
    """
    current_obc = state.get("obc", "000")
    current_outs = state.get("outs", 0)
    result_ranges = state.get("result_ranges")
    bunt_ranges = state.get("bunt_ranges") or result_ranges

    pitcher_row = state.get("pitcher_row") or {}
    batter_row = state.get("batter_row") or {}
    runner_spds = state.get("runner_spds") or {}
    mgr_kwargs = dict(
        pitcher_hand=_hand(pitcher_row), pitcher_mov=_stat(pitcher_row, "mov"),
        pitcher_cmd=_stat(pitcher_row, "cmd"), pitcher_vel=_stat(pitcher_row, "vel"),
        pitcher_awr=_stat(pitcher_row, "awr"),
        batter_hand=_hand(batter_row), batter_con=_stat(batter_row, "con"),
        batter_eye=_stat(batter_row, "eye"), batter_pow=_stat(batter_row, "pwr"),
        batter_spd=_stat(batter_row, "spd"),
        outs=current_outs, runners_on=current_obc != "000", obc=current_obc,
        runner_1b_spd=runner_spds.get("1B"), runner_2b_spd=runner_spds.get("2B"),
        runner_3b_spd=runner_spds.get("3B"),
    )

    if_in_ranges = state.get("if_in_ranges") or utils.compute_at_bat_ranges(
        bunt=False, hit_and_run=False, infield_in=True, **mgr_kwargs)
    hnr_sheet_ranges = state.get("hnr_ifin_ranges") if if_in_checked else state.get("hnr_ranges")
    hnr_ranges = hnr_sheet_ranges or utils.compute_at_bat_ranges(
        bunt=False, hit_and_run=True, infield_in=if_in_checked, **mgr_kwargs)

    steal_runners = state.get("steal_runners") or []
    has_runners = current_obc != "000" and bool(steal_runners)
    has_hnr = current_obc in _HNR_VALID_OBCS and current_outs < 2 and bool(steal_runners)
    has_if_in = current_obc[0] == "1" and current_outs < 2

    hnr_steal_runner = None
    if has_hnr:
        hnr_steal_runner = (next((r for r in steal_runners if r["base"] == "1B"), None)
                             if current_obc == "101" else (steal_runners[0] if steal_runners else None))
    hnr_normal_rng = hnr_steal_runner["safe_range"] if hnr_steal_runner else 50
    steal_ev_rng = steal_runners[0]["safe_range"] if steal_runners else 50

    inning = state.get("inning", 1)
    half = str(state.get("half", "Top"))
    league = state.get("league", "MLN")
    remaining = utils.remaining_half_innings(inning, half.lower(), utils.game_innings(league))
    batting_lead = (state.get("home_score", 0) - state.get("away_score", 0) if half.lower() == "bottom"
                    else state.get("away_score", 0) - state.get("home_score", 0))

    wp_table_ready = utils.get_win_probability(1, 0, "000", 0) is not None
    current_wp = (utils.get_win_probability_interpolated(remaining, current_outs, current_obc, batting_lead)
                  if wp_table_ready else None)
    leverage = (utils.compute_leverage(result_ranges, remaining, current_outs, current_obc, batting_lead)
                if result_ranges else None)
    baseline_er = utils.get_expected_runs(current_outs, current_obc) or 0

    def _swing_like_row(label, ranges):
        ev, p1r, p2r, p3pr = calc_ev_and_probs(run_lookup, ranges, current_obc, current_outs)
        wp = (calc_wp_after(run_lookup, ranges, current_obc, current_outs, remaining, batting_lead)
              if wp_table_ready else None)
        return {"decision": label, "exp_wp": wp, "exp_runs": ev, "p1r": p1r, "p2r": p2r, "p3pr": p3pr}

    rows = [_swing_like_row("Normal Swing", result_ranges), _swing_like_row("Bunt", bunt_ranges)]

    if has_runners:
        ev, p1r, p2r, p3pr = calc_steal_ev_and_probs(current_obc, current_outs, steal_ev_rng)
        wp = (calc_steal_wp_after(current_obc, current_outs, steal_ev_rng, remaining, batting_lead)
              if wp_table_ready else None)
        rows.append({"decision": "Steal", "exp_wp": wp, "exp_runs": ev, "p1r": p1r, "p2r": p2r, "p3pr": p3pr})

    if has_hnr:
        ev, p1r, p2r, p3pr = calc_ev_hnr_and_probs(run_lookup, current_obc, current_outs,
                                                    hnr_ranges, hnr_normal_rng, hnr_no_steal)
        wp = (calc_hnr_wp_after(run_lookup, current_obc, current_outs, hnr_ranges, hnr_normal_rng,
                                 remaining, batting_lead, hnr_no_steal) if wp_table_ready else None)
        rows.append({"decision": "Hit and Run", "exp_wp": wp, "exp_runs": ev, "p1r": p1r, "p2r": p2r, "p3pr": p3pr})

    if has_if_in:
        rows.append(_swing_like_row("vs. Infield In", if_in_ranges))

    sandbox_diffs: list[str] = []
    if state.get("sandbox_ranges"):
        sandbox_diffs = compare_sandbox_inputs(state)
        if sandbox_diffs:
            sandbox_obc = state.get("sandbox_obc") or "000"
            sb_ev, sb_p1r, sb_p2r, sb_p3pr = calc_ev_and_probs(run_lookup, state["sandbox_ranges"],
                                                                sandbox_obc, current_outs)
            sb_wp = (calc_wp_after(run_lookup, state["sandbox_ranges"], sandbox_obc, current_outs,
                                    remaining, batting_lead) if wp_table_ready else None)
            rows.append({"decision": "Sandbox", "exp_wp": sb_wp, "exp_runs": sb_ev,
                         "p1r": sb_p1r, "p2r": sb_p2r, "p3pr": sb_p3pr})

    return {
        "rows": rows, "current_wp": current_wp, "leverage": leverage, "baseline_er": baseline_er,
        "pitcher": state.get("pitcher"), "batter": state.get("batter"),
        "outs": current_outs, "obc": current_obc, "sandbox_diffs": sandbox_diffs,
    }


def calc_hnr_wp_after(run_lookup: dict, current_obc: str, current_outs: int, hnr_ranges: list,
                       hnr_k_steal_safe_rng: int, remaining: int, batting_lead: int,
                       no_steal: bool = False) -> float:
    """See calc_ev_hnr_and_probs for no_steal."""
    sp = min(hnr_k_steal_safe_rng * 2 / 1000, 1.0)
    op = 1.0 - sp
    total = 0.0
    for entry in (hnr_ranges or []):
        r, lo, hi = _norm(entry)
        prob = min((hi - lo + 1) * 2 / 1000, 1.0)
        if r == "K" and not no_steal:
            _, _, k_nout = _lookup(run_lookup, "K", current_obc, current_outs)
            k_nout = min(k_nout, 3)
            s_obc, s_runs = _hnr_steal_advance_obc(current_obc)
            s_wp = wp_for_state(remaining, k_nout, s_obc, batting_lead + int(s_runs))
            cs_nout_wp = min(k_nout + 1, 3)
            cs_obc = "000" if cs_nout_wp >= 3 else _hnr_steal_cs_obc(current_obc)
            cs_wp = wp_for_state(remaining, cs_nout_wp, cs_obc, batting_lead)
            total += prob * (sp * s_wp + op * cs_wp)
        else:
            runs_f, new_obc, new_outs = _lookup(run_lookup, r, current_obc, current_outs)
            new_bl = batting_lead + int(round(runs_f))
            total += prob * wp_for_state(remaining, min(new_outs, 3), new_obc, new_bl)
    return total

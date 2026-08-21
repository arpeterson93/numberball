"""Derived stats, constants, and chart helpers for Numberball."""
from __future__ import annotations

import math
import sys
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ------------------------------------------------------------------ constants

TEAMS = ["Couriers", "Jammers", "Sharks", "Tridents"]

OBC_OPTIONS = ["000", "001", "010", "100", "011", "101", "110", "111"]

OBC_DISPLAY = {
    "000": "Empty",
    "001": "1B",
    "010": "2B",
    "100": "3B",
    "011": "1B&2B",
    "101": "1B&3B",
    "110": "2B&3B",
    "111": "Loaded",
}

def obc_display(code: str) -> str:
    """Return the friendly display name for a binary OBC code."""
    return OBC_DISPLAY.get(code, code)


def obc_circles(code: str) -> str:
    """Return 3 circle chars for 3rd/2nd/1st base occupancy (filled if runner present)."""
    return " ".join("●" if b == "1" else "○" for b in code)

RESULTS_HITS = ["HR", "3B", "2BWH", "2B", "1BWH2", "1BWH", "1B", "IF1B", "BB"]
RESULTS_OUTS = ["DSacF", "DFO", "SacF", "FO", "PO", "GORA", "FCH", "FC", "GO",
                "FC3rd", "DPRun", "DP", "DP21", "DP31", "DPH1", "K", "LODP", "TP", "LOTP"]
RESULTS = RESULTS_HITS + RESULTS_OUTS

RESULT_CATEGORIES = {
    "OUT": ["GO", "FO", "PO", "K", "FC", "DP", "DPH1", "GORA", "DSacF"],
    "BB/1B": ["BB", "1B", "IF1B"],
    "XBH": ["2B", "3B", "HR"],
}

MEME_NUMBERS = [1, 67, 69, 420, 666, 1000]

# Run Expectancy Matrix: (outs, obc) -> expected runs (overwritten by CSV on load)
RUN_EXPECTANCY = {
    (0, "000"): 0.67, (0, "001"): 1.00, (0, "010"): 1.31, (0, "100"): 1.52,
    (0, "011"): 1.61, (0, "101"): 1.89, (0, "110"): 2.02, (0, "111"): 2.51,
    (1, "000"): 0.39, (1, "001"): 0.65, (1, "010"): 0.79, (1, "100"): 1.01,
    (1, "011"): 1.06, (1, "101"): 1.23, (1, "110"): 1.50, (1, "111"): 1.55,
    (2, "000"): 0.17, (2, "001"): 0.35, (2, "010"): 0.41, (2, "100"): 0.48,
    (2, "011"): 0.63, (2, "101"): 0.72, (2, "110"): 0.82, (2, "111"): 0.98,
}

# BRC int -> OBC string (sequential encoding: 0=empty,1=1B,2=2B,3=3B,4=1&2B,5=1&3B,6=2&3B,7=BL)
_BRC_TO_OBC = {0: "000", 1: "001", 2: "010", 3: "100", 4: "011", 5: "101", 6: "110", 7: "111"}
_OBC_STRING_TO_CODE = {v: k for k, v in _BRC_TO_OBC.items()}

# _re_dist[(outs, obc)] = {runs_scored: probability}
_re_dist: dict[tuple[int, str], dict[int, float]] = {}


def _load_re_distribution() -> None:
    """Load run_expectancy_distribution.csv, updating RUN_EXPECTANCY means in place."""
    global _re_dist
    try:
        _csv = pd.read_csv("run_expectancy_distribution.csv")
        for (outs, brc), grp in _csv.groupby(["Outs", "BRC"]):
            obc_str = _BRC_TO_OBC.get(int(brc))
            if obc_str is None:
                continue
            key = (int(outs), obc_str)
            _re_dist[key] = dict(zip(grp["runs_scored"].astype(int), grp["pct"]))
            RUN_EXPECTANCY[key] = round(float((grp["runs_scored"] * grp["pct"]).sum()), 4)
    except FileNotFoundError:
        pass


_load_re_distribution()


# ── Win Probability lookup ─────────────────────────────────────────────────────
# Keyed on (remaining_half_innings, outs, obc, batting_lead).
# batting_lead is from the batting team's perspective (positive = leading).
# Load from win_probability_table.csv if present; silent no-op if not yet generated.

_WP_LOOKUP: dict[tuple[int, int, str, int], float] = {}
_WP_INNINGS = 6   # standard game length used when computing remaining

LEAGUE_INNINGS: dict[str, int] = {"RLN": 4, "MLN": 6}


def game_innings(league: str) -> int:
    """Return regulation game length in innings for the given league."""
    return LEAGUE_INNINGS.get(str(league).upper(), _WP_INNINGS)


# Sorted index for batting_lead interpolation: (remaining, outs, obc) -> [(lead, prob), ...]
_WP_BY_STATE: dict[tuple[int, int, str], list[tuple[int, float]]] = {}


def _load_wp_table() -> None:
    global _WP_LOOKUP, _WP_BY_STATE
    try:
        _wdf = pd.read_csv("win_probability_table.csv", dtype={"obc": str})
        _wdf["obc"] = _wdf["obc"].str.zfill(3)
        _WP_LOOKUP = {
            (int(r["remaining"]), int(r["outs"]), str(r["obc"]), int(r["batting_lead"])): float(r["win_prob"])
            for _, r in _wdf.iterrows()
        }
        _idx: dict[tuple[int, int, str], list[tuple[int, float]]] = {}
        for (rem, o, obc_s, bl), wp in _WP_LOOKUP.items():
            k = (rem, o, obc_s)
            if k not in _idx:
                _idx[k] = []
            _idx[k].append((bl, wp))
        _WP_BY_STATE = {k: sorted(v) for k, v in _idx.items()}
    except FileNotFoundError:
        pass


_load_wp_table()

# (result, before_obc, outs) -> (runs_scored, new_obc, eouts)
_BRC_RUN_LOOKUP: dict[tuple[str, str, int], tuple[float, str, int]] = {}

# (result, before_obc, outs) -> raw ThrowOrder string, e.g. "fst" (throw to
# 1B, then 2B, then 3B) or "6h" (cutoff through SS, then home) - one
# character per leg, parsed client-side by docs/js/app.js's parseThrowOrder
# (h/f/s/t for HOME/1B/2B/3B, 1-9 for a position/cutoff leg). This module
# never interprets the string itself, just passes it through raw. Column is
# optional - a CSV without it (every one that exists today) just leaves this
# dict empty, and every consumer (get_throw_order below) treats "no entry"
# as "no explicit throw order for this situation," not an error.
_BRC_THROW_ORDER: dict[tuple[str, str, int], str] = {}

# (result, before_obc, outs) -> {"excluded": [...], "default": "SS"}. Lets a
# situation say "if the physics-computed fielder for this play is one of
# these positions, it doesn't make sense - use this one instead." "OF" in
# excluded is a wildcard for any of LF/CF/RF. Both columns optional, same
# no-entry-means-nothing-to-override contract as everything else here.
_BRC_POSITION_OVERRIDE: dict[tuple[str, str, int], dict] = {}

# (result, before_obc, outs) -> {"P": "...", "C": "...", "1B": "...", ...}.
# Per-position throw sequences - which one applies depends on which fielder
# actually ends up credited (after any position override above), not just
# the situation alone, so this stays a dict-of-positions per key rather than
# a single string like the generic ThrowOrder column.
_BRC_THROW_ORDER_BY_POSITION: dict[tuple[str, str, int], dict] = {}

# ThrowOrder_OF (the original, coarse "any outfielder" column) stays alongside
# the newer per-outfielder LF/CF/RF columns rather than being replaced - a
# situation can fill in either, or both (docs/js/app.js's own lookup tries
# the specific position first, then falls back to OF). This is a deliberate,
# non-destructive add: the 43 rows already filled in under ThrowOrder_OF keep
# working unchanged, no CSV data migration needed.
_THROW_ORDER_POSITION_COLUMNS = {
    "P": "ThrowOrder_P", "C": "ThrowOrder_C",
    "1B": "ThrowOrder_1B", "2B": "ThrowOrder_2B", "3B": "ThrowOrder_3B", "SS": "ThrowOrder_SS",
    "LF": "ThrowOrder_LF", "CF": "ThrowOrder_CF", "RF": "ThrowOrder_RF",
    "OF": "ThrowOrder_OF",
}


# (result, before_obc, outs) -> [{"from": "BATTER"|"1B"|"2B"|"3B", "to": "1B"|
# "2B"|"3B"|"HOME"|"OUT", "scored": bool, "assist": "1B"|"2B"|"3B"|"HOME"|None,
# "delay": bool, "retreat": bool}, ...] - the exact per-runner outcome for
# this situation, decoded from import_BRC.csv's B/r1/r2/r3 (Alex's briefing:
# b1/b2/b3 = ends up there - same number as their own starting base means
# they held - b4 = scores, 0 = out), a1/a2/a3 (same b1-b4 token format - the
# base the runner was actually making for, independent of whether rN says
# they got there, were put out short of it, or turned back), and the
# row-level delay/retreat flags (apply to every move on that row - a play
# has one shared timeline, not a per-runner one). Trusted completely when
# present; None when the situation isn't in the table, or its own numbers
# don't reconcile (see _build_runner_moves_for_row) - app.js falls back to
# its diff-based guess either way, same contract as everything else in this
# file.
_BRC_RUNNER_MOVES: dict[tuple[str, str, int], list[dict]] = {}

_MOVE_DEST_BASE = {"b1": "1B", "b2": "2B", "b3": "3B"}


def _decode_move_destination(raw) -> tuple[str, bool] | None:
    """(to, scored) for one B/r1/r2/r3 cell, or None if it's blank or not one
    of the recognised tokens (e.g. pAuto's "g" - a ghost-runner placement,
    not an existing runner's transition, which this whole table shape
    assumes). None is the "don't know" case - callers turn that into either
    "not present" (batter) or "OUT" (a base runner, since deriveRunnerMoves'
    own stranded-runner convention already treats an unresolved base runner
    that way and every downstream consumer already knows what to do with it).
    """
    s = _clean_csv_cell(raw)
    if not s:
        return None
    if s == "0":
        return ("OUT", False)
    if s == "b4":
        return ("HOME", True)
    base = _MOVE_DEST_BASE.get(s)
    return (base, False) if base else None


def _decode_assist_base(raw) -> str | None:
    """The base one a1/a2/a3 cell names, in app.js's "1B"/"2B"/"3B"/"HOME"
    vocabulary - same b1-b4 tokens as B/r1/r2/r3, but there's no "0"/out case
    here (an assist cell names where the runner was headed, not their fate) -
    blank/unrecognised is just None, not a decision either way.
    """
    s = _clean_csv_cell(raw)
    if not s:
        return None
    if s == "b4":
        return "HOME"
    return _MOVE_DEST_BASE.get(s)


# (obc_before string index, r-column, a-column, base label) - obc_before is
# [3B,2B,1B] (index 0/1/2), matching _BRC_TO_OBC's own string convention.
_RUNNER_SLOT_DEFS = [(2, "r1", "a1", "1B"), (1, "r2", "a2", "2B"), (0, "r3", "a3", "3B")]


def _build_runner_moves_for_row(row, before_obc: str, outs_before: int) -> list[dict] | None:
    """Decodes one row into an explicit move list, or None if this row's
    columns aren't trustworthy enough to use - either the runner-tracking
    columns aren't there at all, or the per-runner destinations don't add up
    to the row's own OBC/Runs (a handful of rows have "held" filled in for
    r1/r2 without the OBC column being updated to match, and a few auto/
    ghost-runner codes don't describe an existing runner's move at all).
    Skipping those and falling back to the diff-based heuristic is safer
    than rendering something the row's own numbers disagree with.

    OBC agreement is skipped (not just relaxed) when this play's own eOuts
    shows it reaches 3 - a half-inning-ending play's OBC column means
    "reset to empty for the next half-inning," not "every runner's
    individual fate reconciles to this," so a row that fills in a truthful
    destination for a runner who was simply stranded (e.g. safely advanced
    one base, then the frame ended) would otherwise fail this check for
    disagreeing with a column that was never describing that runner at all.
    Runs still has to reconcile regardless - scoring isn't affected by the
    reset convention.

    A blank rN cell on an inning-ending row (eOuts >= 3) is the sheet's own
    "the final base doesn't matter, the frame resets" shorthand - but aN
    (Alex's briefing) still describes a real fact about the play: where that
    runner should be shown attempting to advance, chosen with eOuts in mind,
    even though it's never credited. So a blank rN defers to aN first (the
    runner is animated running for that base, never scored - Runs/the OBC
    reconciliation below still governs actual credit, unaffected by this)
    and only falls all the way back to a held-their-base move when aN is
    ALSO blank, with nothing at all to go on (e.g. the trailing runner on a
    KCS double-steal attempt whose own out wasn't the one that ended the
    inning). Off any other (non-inning-ending) row a blank rN stays
    untrustworthy and still bails, same as before.
    """
    moves: list[dict] = []
    runs_from_moves = 0
    occ_after = {"1B": False, "2B": False, "3B": False}

    try:
        ends_half_inning = (int(float(row["eOuts"])) if "eOuts" in row.index else outs_before) >= 3
    except (KeyError, ValueError, TypeError):
        return None

    b_dest = _decode_move_destination(row.get("B"))
    if b_dest is not None and b_dest[0] != "OUT":
        # A batter who's out is deliberately NOT added here - the existing
        # out-to-first choreography (sceneFieldHtml's "no BATTER move" branch)
        # already handles that case correctly, and giving it a raw isOut
        # move here would route it through the base-runner force/corroboration
        # path instead, which has no idea what to do with "from": "BATTER".
        moves.append({"from": "BATTER", "to": b_dest[0], "scored": b_dest[1], "assist": None})
        if b_dest[1]:
            runs_from_moves += 1
        elif b_dest[0] in occ_after:
            occ_after[b_dest[0]] = True

    for obc_idx, col, acol, base in _RUNNER_SLOT_DEFS:
        if before_obc[obc_idx] != "1":
            continue
        dest = _decode_move_destination(row.get(col))
        if dest is None:
            if ends_half_inning:
                assist_dest = _decode_assist_base(row.get(acol))
                dest = (assist_dest, False) if assist_dest else (base, False)
            else:
                # Blank/unrecognised for a runner obc_before says WAS there,
                # on a play that ISN'T inning-ending - not "assume they're
                # out," just not enough to trust this row at all. Bailing out
                # entirely leaves it to the diff-based heuristic, which
                # already has its own established stranded/uncorroborated
                # handling for exactly this ambiguity.
                return None
        to, scored = dest
        moves.append({"from": base, "to": to, "scored": scored, "assist": _decode_assist_base(row.get(acol))})
        if scored:
            runs_from_moves += 1
        elif to in occ_after:
            occ_after[to] = True

    try:
        if not ends_half_inning:
            predicted_obc_str = (
                ("1" if occ_after["3B"] else "0") +
                ("1" if occ_after["2B"] else "0") +
                ("1" if occ_after["1B"] else "0")
            )
            if _OBC_STRING_TO_CODE[predicted_obc_str] != int(row["OBC"]):
                return None
        if runs_from_moves != float(row["Runs"]):
            return None
    except (KeyError, ValueError, TypeError):
        return None

    # delay/retreat are single per-row flags, not per-runner columns - a play
    # has one shared timeline (Alex's briefing: "runners should be delayed"
    # describes the play, not one specific runner on it), so every move this
    # row produced carries the same two booleans.
    delay = _clean_csv_cell(row.get("delay")).upper() == "Y"
    retreat = _clean_csv_cell(row.get("retreat")).upper() == "Y"
    for mv in moves:
        mv["delay"] = delay
        mv["retreat"] = retreat

    return moves


def _clean_csv_cell(raw) -> str:
    """A column that's all digits with some blanks reads back from pandas as
    float64 (blanks become NaN) - "1234" round-trips through that as 1234.0.
    Strips that artifact so a digit string (ThrowOrder and friends) survives
    intact instead of shipping "1234.0" down to app.js's parser."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return "" if s.lower() == "nan" else s


def _load_brc_table() -> None:
    global _BRC_RUN_LOOKUP, _BRC_THROW_ORDER, _BRC_POSITION_OVERRIDE, _BRC_THROW_ORDER_BY_POSITION, _BRC_RUNNER_MOVES
    try:
        _bdf = pd.read_csv("import_BRC.csv")
        if "Situation" not in _bdf.columns or "Runs" not in _bdf.columns:
            return
        cols = list(_bdf.columns)
        has_throw_order = "ThrowOrder" in cols
        has_excluded = "ExcludedPositions" in cols and "DefaultPosition" in cols
        has_runner_cols = all(c in cols for c in ("B", "r1", "r2", "r3"))
        position_cols = {pos: col for pos, col in _THROW_ORDER_POSITION_COLUMNS.items() if col in cols}

        lookup: dict[tuple[str, str, int], tuple[float, str, int]] = {}
        throw_lookup: dict[tuple[str, str, int], str] = {}
        override_lookup: dict[tuple[str, str, int], dict] = {}
        by_position_lookup: dict[tuple[str, str, int], dict] = {}
        moves_lookup: dict[tuple[str, str, int], list[dict]] = {}

        for _, row in _bdf.iterrows():
            situation = str(row["Situation"]).strip()
            parts = situation.split("_")
            if len(parts) < 3:
                continue
            try:
                outs     = int(parts[0])
                obc_code = int(parts[1])
                result   = "_".join(parts[2:])
                before_obc = _BRC_TO_OBC.get(obc_code, "000")
                runs       = float(row["Runs"])
                eouts      = int(float(row["eOuts"])) if "eOuts" in cols else 0
                new_obc    = _BRC_TO_OBC.get(int(float(row["OBC"])), "000")
            except (ValueError, TypeError, KeyError):
                continue
            key = (result, before_obc, outs)
            lookup[key] = (runs, new_obc, eouts)

            if has_throw_order:
                throw_order = _clean_csv_cell(row.get("ThrowOrder"))
                if throw_order:
                    throw_lookup[key] = throw_order

            if has_excluded:
                excluded_raw = _clean_csv_cell(row.get("ExcludedPositions"))
                default_raw = _clean_csv_cell(row.get("DefaultPosition")).upper()
                excluded = [p.strip().upper() for p in excluded_raw.split(",") if p.strip()]
                if excluded and default_raw:
                    override_lookup[key] = {"excluded": excluded, "default": default_raw}

            by_position = {}
            for pos, col in position_cols.items():
                val = _clean_csv_cell(row.get(col))
                if val:
                    by_position[pos] = val
            if by_position:
                by_position_lookup[key] = by_position

            if has_runner_cols:
                moves = _build_runner_moves_for_row(row, before_obc, outs)
                if moves is not None:
                    moves_lookup[key] = moves

        _BRC_RUN_LOOKUP = lookup
        _BRC_THROW_ORDER = throw_lookup
        _BRC_POSITION_OVERRIDE = override_lookup
        _BRC_THROW_ORDER_BY_POSITION = by_position_lookup
        _BRC_RUNNER_MOVES = moves_lookup
    except FileNotFoundError:
        pass


_load_brc_table()


def get_throw_order(result: str, obc: str, outs: int) -> str | None:
    """Explicit throw sequence for this (result, before_obc, outs) situation,
    straight from import_BRC.csv's optional ThrowOrder column - e.g. "fst"
    for 1B, then 2B, then 3B. None when the column doesn't exist yet, or
    this particular situation row hasn't been filled in - callers
    (key_moments_build.py) treat that as "no explicit order," not an error.
    """
    return _BRC_THROW_ORDER.get((result, obc, outs))


def get_position_override(result: str, obc: str, outs: int) -> dict | None:
    """{"excluded": [...], "default": "SS"} for this situation, from the
    optional ExcludedPositions/DefaultPosition columns - None when either
    column doesn't exist yet or this situation hasn't been filled in.
    """
    return _BRC_POSITION_OVERRIDE.get((result, obc, outs))


def get_throw_order_by_position(result: str, obc: str, outs: int) -> dict | None:
    """{"P": "...", "1B": "...", ...} for this situation, from the optional
    per-position ThrowOrder_* columns - only positions actually filled in are
    present. None when none of those columns exist yet, or none of them has
    a value for this situation.
    """
    return _BRC_THROW_ORDER_BY_POSITION.get((result, obc, outs))


def get_runner_moves(result: str, obc: str, outs: int) -> list[dict] | None:
    """Explicit per-runner outcome for this (result, before_obc, outs)
    situation, decoded from import_BRC.csv's B/r1/r2/r3 columns - each entry
    is {"from": "BATTER"|"1B"|"2B"|"3B", "to": "1B"|"2B"|"3B"|"HOME"|"OUT",
    "scored": bool}. Trusted completely by callers when present, in place of
    guessing from obc_before/obc_after/runs. None when the columns don't
    exist yet, this situation hasn't been filled in, or the row's own
    numbers didn't reconcile (see _build_runner_moves_for_row) - callers
    fall back to that same diff-based guess either way.
    """
    return _BRC_RUNNER_MOVES.get((result, obc, outs))


def get_win_probability(
    remaining_half_innings: int,
    outs: int,
    obc: str,
    batting_lead: int,
) -> float | None:
    """Return win probability from the batting team's perspective.

    remaining_half_innings: 1-12 for regulation; extras are treated as 2 (top) or 1 (bottom).
    batting_lead: positive = batting team is ahead.
    Returns None if win_probability_table.csv has not been generated yet.
    """
    if not _WP_LOOKUP:
        return None
    key = (
        max(1, min(_WP_INNINGS * 2, int(remaining_half_innings))),
        int(outs),
        str(obc),
        max(-10, min(10, int(batting_lead))),
    )
    return _WP_LOOKUP.get(key)


def remaining_half_innings(inning: int, half: str, innings: int = _WP_INNINGS) -> int:
    """Compute remaining_half_innings for a given game state.

    Counts down from innings*2 at top of 1st to 1 at bottom of last inning.
    Extra innings (beyond regulation) are capped at 2 (top half) or 1 (bottom half).
    """
    hip = (inning - 1) * 2 + (1 if half == "bottom" else 0)
    reg = innings * 2 - hip
    return int(reg) if reg > 0 else (2 if half == "top" else 1)


def _wp_or(x: float | None, default: float = 0.5) -> float:
    """`x if x is not None else default` - every caller below used to write
    `x or default`, which is wrong here: a legitimate win probability can be
    exactly 0.0 (a state that's already decided), and 0.0 is falsy in Python,
    so `or` silently replaced a real "this team has essentially no chance"
    answer with a coin-flip 0.5. That fallback was only ever meant to catch
    a genuinely missing table entry (None) - found via a real leverage bug
    report (a leverage of 2.3+ in a top-4th, 1-out, down-by-10 state, where
    a double/triple's own real post-play win probability is 0.0, not 0.5)."""
    return default if x is None else x


def get_win_probability_interpolated(
    remaining_half_innings_: int,
    outs: int,
    obc: str,
    batting_lead: int,
) -> float | None:
    """Return batting-team win probability, interpolating on batting_lead when no exact match.

    Clamps batting_lead to [-10, 10] then linearly interpolates between the two nearest
    stored values for the same (remaining, outs, obc) state.
    """
    if not _WP_LOOKUP:
        return None
    import bisect
    rem = max(1, min(_WP_INNINGS * 2, int(remaining_half_innings_)))
    o = int(outs)
    obc_s = str(obc)
    bl = max(-10, min(10, int(batting_lead)))

    exact = get_win_probability(rem, o, obc_s, bl)
    if exact is not None:
        return exact

    candidates = _WP_BY_STATE.get((rem, o, obc_s))
    if not candidates:
        return None

    leads = [c[0] for c in candidates]
    wps   = [c[1] for c in candidates]

    if bl <= leads[0]:
        return wps[0]
    if bl >= leads[-1]:
        return wps[-1]

    idx = bisect.bisect_right(leads, bl)
    bl_lo, wp_lo = leads[idx - 1], wps[idx - 1]
    bl_hi, wp_hi = leads[idx],     wps[idx]
    t = (bl - bl_lo) / (bl_hi - bl_lo)
    return wp_lo + t * (wp_hi - wp_lo)


# Result probability distribution for the LI denominator.
# Per-situation diff-band ranges from result_ranges_re24.csv, keyed by
# (outs, obc) -> [(result, low, high), ...]. Same shape as RESULT_RANGES (see
# below) but sliced by game situation instead of being one flat table, so the
# results it offers are the ones that can actually happen in that state - no
# double plays at 2 outs, no sac flies with nobody on third.
_RE24_RANGES: dict[tuple[int, str], list[tuple[str, int, int]]] = {}

# Game-state frequency weights for the LI denominator.
# Loaded from state_frequencies.csv (generated by compute_state_frequencies.py).
# Falls back to equal weights across all states if the CSV is absent.
_STATE_WEIGHTS: dict[tuple[int, int, str], float] = {}

# State-visitation weights for the LI denominator only, summed over every
# batting_lead bucket of simulated_win_probability_table.csv. Deliberately kept
# separate from _STATE_WEIGHTS: batter_optimizer.py reads that one directly for
# an unrelated purpose and has no reason to move when leverage's baseline does.
_SIM_STATE_WEIGHTS: dict[tuple[int, int, str], float] = {}

_AVG_WP_SWING: float | None = None  # computed lazily on first leverage call


def _load_re24_ranges() -> None:
    global _RE24_RANGES
    try:
        _rdf = pd.read_csv("result_ranges_re24.csv", dtype={"obc": str})
        _rdf["obc"] = _rdf["obc"].str.zfill(3)
        ranges: dict[tuple[int, str], list[tuple[str, int, int]]] = {}
        for _, r in _rdf.iterrows():
            key = (int(r["outs"]), str(r["obc"]))
            ranges.setdefault(key, []).append(
                (str(r["Result"]), int(r["Low"]), int(r["High"]))
            )
        _RE24_RANGES = ranges
    except FileNotFoundError:
        pass


def _load_sim_state_weights() -> None:
    global _SIM_STATE_WEIGHTS
    try:
        _sdf = pd.read_csv("simulated_win_probability_table.csv", dtype={"obc": str})
        if not {"remaining", "outs", "obc", "n"}.issubset(_sdf.columns):
            return
        _sdf["obc"] = _sdf["obc"].str.zfill(3)
        grouped = _sdf.groupby(["remaining", "outs", "obc"])["n"].sum()
        _SIM_STATE_WEIGHTS = {
            (int(rem), int(outs), str(obc_s)): float(n)
            for (rem, outs, obc_s), n in grouped.items()
        }
    except FileNotFoundError:
        pass


def _load_state_frequencies() -> None:
    global _STATE_WEIGHTS
    try:
        _sdf = pd.read_csv("state_frequencies.csv", dtype={"obc": str})
        if {"remaining", "outs", "obc", "frequency"}.issubset(_sdf.columns):
            _sdf["obc"] = _sdf["obc"].str.zfill(3)
            _STATE_WEIGHTS = {
                (int(r["remaining"]), int(r["outs"]), str(r["obc"])): float(r["frequency"])
                for _, r in _sdf.iterrows()
            }
    except FileNotFoundError:
        pass


# Ball-flight staging data (Play Scene animation, docs/js/app.js). Neither
# table has anything to do with win probability/leverage - they live here
# only because this module is where every other precomputed-CSV loader lives.
# result -> {archetype, band_lo, band_hi, source}, from result_diff_bands.csv
# (compute_result_diff_bands.py).
_DIFF_BANDS: dict[str, dict] = {}
# Historical seasons 1-4 get their own band set (Alex's ask) - same method
# (compute_result_diff_bands.py --season-start 1 --season-end 4), a separate
# MLN diff sample. Statcast itself has no season dimension (compute_flight_
# ranges.py pulls one real-MLB season, independent of MLN), so la/ev/depth
# are identical between this and _DIFF_BANDS - only band_lo/band_hi/n/source
# (MLN's own diff history) actually differ. get_diff_bands(season) is the one
# place that decides which pool a given season's plays use.
_DIFF_BANDS_S1_4: dict[str, dict] = {}
# result_diff_bands.csv is now the single source for everything ball-flight
# needs per result - the old separate ball_flight_archetypes.csv (la_min/
# la_max/ev_min/ev_max/depth_min/depth_max shared by archetype) is retired:
# every result got its own Statcast-derived la/ev numbers instead of sharing
# one archetype-wide range (real MLB data, filtered per MLN result - see
# result_diff_bands.csv's own flight_source column for which results are
# directly computed ("own") vs too rare to trust and borrowed from a
# similar result ("borrowed:X")). depth_min/depth_max are the one exception
# for grounder/infield_single/bunt-family results: Statcast's hit_distance_sc
# measures where a ground ball first touches the ground, not where it's
# fielded (verified against real hit_location - even shortstop/2B groundouts,
# fielded 111-147ft out per INFIELDER_DEPTH_FT, showed hit_distance_sc medians
# under 15ft), so those specific rows keep the old hand-tuned, real-infield-
# depth-based ranges instead (flagged in their own flight_source entry).
# `archetype` is kept as a plain category
# label - app.js's CAUGHT_IN_AIR/GROUND_ARCHETYPES/TAG_THROW_ARCHETYPES
# still branch on it - not as a lookup key into a second numeric table.
def _nan_to_none(v) -> float | None:
    """A result too rare to compute a real band for (e.g. DP32/FCLead in the
    S1-4 pool - too few real plays in those seasons) lands here as NaN, valid
    in a pandas float column but not valid JSON by spec once serialized -
    Python's json module happily emits a bare NaN token by default
    (allow_nan=True), which then fails browsers' strict JSON.parse() outright
    and takes the WHOLE file down with it (Alex's report: season 1-4
    wouldn't load at all, "Unexpected token 'N'... is not valid JSON").
    None -> JSON null is the safe, exact "no data" representation - these
    seven fields are audit-only per _flight_meta's own docstring, nothing at
    runtime reads them, so there's no behavior riding on the value itself."""
    return None if pd.isna(v) else float(v)


def _read_diff_bands_csv(path: str) -> dict[str, dict]:
    try:
        _bdf = pd.read_csv(path)
    except FileNotFoundError:
        return {}
    return {
        str(r["result"]): {
            "archetype": str(r["archetype"]),
            "band_lo": int(r["band_lo"]),
            "band_hi": int(r["band_hi"]),
            "source": str(r["source"]),
            "la_min": _nan_to_none(r["la_min"]), "la_ideal": _nan_to_none(r["la_ideal"]),
            "la_max": _nan_to_none(r["la_max"]),
            "ev_min": _nan_to_none(r["ev_min"]), "ev_max": _nan_to_none(r["ev_max"]),
            "depth_min": _nan_to_none(r["depth_min"]), "depth_max": _nan_to_none(r["depth_max"]),
        }
        for _, r in _bdf.iterrows()
    }


def _load_result_diff_bands() -> None:
    global _DIFF_BANDS, _DIFF_BANDS_S1_4
    _DIFF_BANDS = _read_diff_bands_csv("result_diff_bands.csv")
    _DIFF_BANDS_S1_4 = _read_diff_bands_csv("result_diff_bands_s1_4.csv")


def get_diff_bands(season: int | None) -> dict[str, dict]:
    """The diff-band table for a given season - _DIFF_BANDS_S1_4 for seasons
    1-4, the regular (5+, and the live current season) _DIFF_BANDS otherwise.
    Falls back to _DIFF_BANDS whenever the 1-4 pool hasn't been generated yet
    (file missing) so an archive build for those seasons still works, just
    without the split.
    """
    if season is not None and season <= 4 and _DIFF_BANDS_S1_4:
        return _DIFF_BANDS_S1_4
    return _DIFF_BANDS


# result -> [station, ...] (sorted by station_idx), from flight_stations.csv
# (compute_flight_ranges.py). Each station is a percentile point along this
# result's real (EV, LA) pairs (drawn from a sample pre-trimmed to the 5th-
# 95th percentile by hit_distance_sc) ranked by their own real Statcast
# distance - the joint-selection replacement for the old independent
# la_min/la_ideal/la_max + ev_min/ev_max marginals (ideas-and-opinions
# conversation: independently-drawn marginals, and later an EV solved
# backward through our own physics, could each produce/report an (EV, LA)
# pair no real batted ball ever was). la_topped/ev_topped/dist_topped and
# la_uppercut/ev_uppercut/dist_uppercut are each ONE real play's own paired
# values, distance included - never mixed across two different real plays
# (an earlier design shared one distance across both topped and uppercut,
# borrowed from whichever different real play happened to sit at that
# station's exact rank; fixed here). Read by docs/js/app.js's stationsLookup
# (which radially rescales the rendered flight to that same real play's own
# distance) via key_moments_build.py's _flight_meta().
_FLIGHT_STATIONS: dict[str, list[dict]] = {}

# (result, spray_bucket) -> [station, ...], the spray-angle-conditioned
# sibling of _FLIGHT_STATIONS above (gameday reconciliation plan, spray-bucket
# stage) - only populated for the results compute_flight_ranges.py actually
# builds bucketed stations for (SPRAY_BUCKETED_RESULTS there: 2B/3B/1BWH/
# 1BWH2), and only for buckets whose own real sample was large enough to
# trust. _FLIGHT_STATIONS[result] (the unconditioned "" pool, unchanged by
# this feature) is every result's own runtime fallback whenever a specific
# (result, bucket) pair has no entry here - see key_moments_build.py's
# _flight_meta and docs/js/app.js's flightParams.
_FLIGHT_STATIONS_BY_SPRAY: dict[tuple[str, str], list[dict]] = {}


def _load_flight_stations() -> None:
    global _FLIGHT_STATIONS, _FLIGHT_STATIONS_BY_SPRAY
    try:
        _sdf = pd.read_csv("flight_stations.csv").sort_values(["result", "station_idx"])
    except FileNotFoundError:
        return
    # Older flight_stations.csv files (pre-spray-bucket) have no spray_bucket
    # column at all - treat every row there as the unconditioned "" pool.
    if "spray_bucket" not in _sdf.columns:
        _sdf["spray_bucket"] = ""
    _sdf["spray_bucket"] = _sdf["spray_bucket"].fillna("")

    def _station_row(r: "pd.Series") -> dict:
        return {
            "station_idx": int(r["station_idx"]), "q": float(r["q"]),
            "la_topped": float(r["la_topped"]), "ev_topped": float(r["ev_topped"]),
            "dist_topped": float(r["dist_topped"]),
            "la_uppercut": float(r["la_uppercut"]), "ev_uppercut": float(r["ev_uppercut"]),
            "dist_uppercut": float(r["dist_uppercut"]),
        }

    unbucketed = _sdf[_sdf["spray_bucket"] == ""]
    _FLIGHT_STATIONS = {
        str(result): [_station_row(r) for _, r in group.iterrows()]
        for result, group in unbucketed.groupby("result")
    }
    bucketed = _sdf[_sdf["spray_bucket"] != ""]
    _FLIGHT_STATIONS_BY_SPRAY = {
        (str(result), str(bucket)): [_station_row(r) for _, r in group.iterrows()]
        for (result, bucket), group in bucketed.groupby(["result", "spray_bucket"])
    }


_load_re24_ranges()
_load_sim_state_weights()
_load_state_frequencies()
_load_result_diff_bands()
_load_flight_stations()


def _wp_post_play(result: str, remaining: int, outs: int, obc: str, batting_lead: int) -> float:
    """WP for the batting team after a single result from (remaining, outs, obc, batting_lead)."""
    entry = _BRC_RUN_LOOKUP.get((result, obc, outs))
    if entry is not None:
        runs_f, new_obc, eouts = entry
        # import_BRC.csv's eOuts is already the ABSOLUTE ending out count for
        # this situation (verified against the sheet: "1_0_1B" - 1 out
        # before, a single, which adds none - has eOuts=1, not 0; "0_0_GO"
        # has eOuts=1, "1_0_GO" has eOuts=2), not a delta to add to `outs`.
        # Adding them double-counted the outs already reflected in eOuts -
        # e.g. a bug report: an ordinary single with the bases empty and 1
        # out was computing new_outs=2, tipping win probability/leverage
        # into nonsense (wp_post_play falling through to a 3-outs branch, or
        # landing on a state get_win_probability_interpolated has thin/zero
        # coverage for, whose legitimate 0.0 then got swallowed by the `or
        # 0.5` fallback below - both read as wildly wrong leverage).
        new_outs = eouts
    else:
        new_obc, runs_int = advance_runners(result, obc, outs)
        runs_f   = float(runs_int)
        new_outs = outs + outs_added(result)
    new_outs = min(new_outs, 3)
    new_bl   = batting_lead + int(round(runs_f))
    if new_outs < 3:
        return _wp_or(get_win_probability_interpolated(remaining, new_outs, new_obc, new_bl))
    if remaining > 1:
        # Handing the bat to the other team for remaining-1. If that next half
        # is the bottom of the last inning and they are already ahead, the game
        # is over before it starts: the bottom half is never played when the
        # home team leads after the top (the same rule
        # key_moments_build._is_walkoff_final applies as its first condition).
        # No table can carry real data for that state, so ask for 1.0 directly
        # rather than looking up a cell that is dead by construction. A table
        # that happens to populate it (today's does, with 1.0) gives the same
        # answer; one that leaves it empty would otherwise get
        # get_win_probability_interpolated's clamp-to-nearest-lead fallback and
        # a badly wrong number.
        if remaining - 1 == 1 and -new_bl > 0:
            return 0.0
        return 1.0 - _wp_or(get_win_probability_interpolated(remaining - 1, 0, "000", -new_bl))
    return 1.0 if new_bl > 0 else (0.5 if new_bl == 0 else 0.0)


def _compute_avg_wp_swing() -> None:
    """Populate _AVG_WP_SWING: frequency-weighted mean expected |WP change| per PA.

    Two inputs feed this baseline, both situational:

    - The per-state result distribution comes from result_ranges_re24.csv's
      slice for that exact (outs, obc), converted to probabilities the same way
      compute_leverage's own numerator does. A flat, pooled distribution would
      credit every state with results it cannot produce - double plays with two
      outs already gone, sac flies with nobody on third.
    - The state weight comes from _SIM_STATE_WEIGHTS (millions of simulated
      visits) rather than _STATE_WEIGHTS's historical snapshot. Missing states
      fall back to weight 1.0, so an absent simulated table degrades to equal
      weighting instead of crashing.

    The win probabilities themselves still come from win_probability_table.csv
    through get_win_probability_interpolated/_wp_post_play, exactly as before.
    """
    global _AVG_WP_SWING
    if not _WP_BY_STATE:
        _AVG_WP_SWING = 0.04
        return
    total      = 0.0
    weight_sum = 0.0
    for (rem, outs, obc_s) in _WP_BY_STATE:
        ranges = _RE24_RANGES.get((outs, obc_s))
        if not ranges:
            continue
        wp_cur = _wp_or(get_win_probability_interpolated(rem, outs, obc_s, 0))
        swing  = sum(
            min((hi - lo + 1) * 2 / 1000, 1.0) * abs(_wp_post_play(res, rem, outs, obc_s, 0) - wp_cur)
            for res, lo, hi in ranges
        )
        w = _SIM_STATE_WEIGHTS.get((rem, outs, obc_s), 1.0)
        total      += w * swing
        weight_sum += w
    _AVG_WP_SWING = total / weight_sum if weight_sum else 0.04


def compute_leverage(
    result_ranges: list,
    remaining: int,
    outs: int,
    obc: str,
    batting_lead: int,
) -> float | None:
    """Leverage Index for the current plate appearance.

    LI = (expected |WP change| for this PA using matchup probabilities)
         / (average expected |WP change| per PA across all game states).

    LI > 1 = higher-than-average stakes; LI < 1 = lower-than-average stakes.
    """
    global _AVG_WP_SWING
    if not _WP_BY_STATE:
        return None
    if _AVG_WP_SWING is None:
        _compute_avg_wp_swing()
    if not _AVG_WP_SWING:
        return None
    wp_cur = get_win_probability_interpolated(remaining, outs, obc, batting_lead)
    if wp_cur is None:
        return None
    numerator = 0.0
    for entry in (result_ranges or []):
        if isinstance(entry, dict):
            res, lo, hi = entry["result"], entry["low"], entry["high"]
        else:
            res, lo, hi = entry
        prob = min((hi - lo + 1) * 2 / 1000, 1.0)
        wp_after = _wp_post_play(res, remaining, outs, obc, batting_lead)
        numerator += prob * abs(wp_after - wp_cur)
    return numerator / _AVG_WP_SWING


def compute_leverage_re24(
    remaining: int,
    outs: int,
    obc: str,
    batting_lead: int,
) -> float | None:
    """Leverage Index using result_ranges_re24.csv's situational diff bands.

    For callers with no stadium-sheet matchup ranges to supply - the Key
    Moments build and the scoreboard - where the alternative is the flat
    RESULT_RANGES template regardless of the game situation. compute_leverage()
    itself is unchanged; this only picks the right slice of ranges to hand it.

    Returns None if result_ranges_re24.csv has no entry for this (outs, obc),
    which is also what compute_leverage returns when it cannot answer.
    """
    ranges = _RE24_RANGES.get((int(outs), str(obc)))
    if not ranges:
        return None
    return compute_leverage(ranges, remaining, outs, obc, batting_lead)


def compute_game_wp_series(
    plays: list[dict],
    game: dict,
    innings: int = _WP_INNINGS,
) -> pd.DataFrame:
    """Compute the home-team win probability AFTER each play.

    Each row's home_wp reflects the game state that resulted from that play,
    so hovering on a HR shows the WP shift caused by the HR.

    Returns a DataFrame with columns:
    play_idx, inn_label, outs, obc, batter, pitcher, result,
    home_score, away_score, home_wp, hover
    """
    away_team = game.get("away_team", "Away")
    home_team = game.get("home_team", "Home")
    # Only treat scores as final when win_team is set - NULL means game is still in progress
    _game_final = game.get("win_team") not in (None, "", "nan")
    final_away = game.get("away_score") if _game_final else None
    final_home = game.get("home_score") if _game_final else None

    home_score = 0
    away_score = 0
    rows: list[dict] = []

    sorted_plays = sorted(plays, key=lambda p: p.get("play_num") or p.get("id") or 0)

    # "Start" point: WP before any play (top of 1st, 0-0)
    rem0 = remaining_half_innings(1, "top", innings)
    wp0  = _wp_or(get_win_probability_interpolated(rem0, 0, "000", 0))
    rows.append({
        "play_idx":   0,
        "inn_label":  "Start",
        "outs":       0,
        "obc":        "000",
        "batter":     "",
        "pitcher":    "",
        "result":     "",
        "home_score": 0,
        "away_score": 0,
        "home_wp":    1.0 - wp0,
        "hover":      "Start of game",
    })

    _game_ended    = False
    _final_home_wp = 0.5

    for i, play in enumerate(sorted_plays):
        inning  = int(play.get("inning") or 1)
        half    = str(play.get("half") or "top")
        outs    = int(play.get("outs") or 0)
        obc_raw = play.get("obc") or "000"
        obc     = str(obc_raw).zfill(3) if not isinstance(obc_raw, int) else _BRC_TO_OBC.get(obc_raw, "000")
        result  = str(play.get("result") or "")
        pitcher = str(play.get("pitcher_name") or "")
        batter  = str(play.get("batter_name") or "")
        is_home = (half == "bottom")

        # Update score first so WP reflects the post-play state
        if result:
            _, runs = advance_runners(result, obc, outs)
            if is_home:
                home_score += int(runs)
            else:
                away_score += int(runs)

        _new_outs_total = outs + outs_added(result)
        _is_late = (inning >= innings)

        # Detect game-ending conditions (final inning and any extra innings):
        # 1. Top half ends (3 outs) with home team leading - bottom never played
        # 2. Home team leads at any point during the bottom half - walk-off
        # 3. Bottom half ends (3 outs) with score not tied - away wins (or home won via #2)
        if not _game_ended and _is_late:
            if (not is_home) and _new_outs_total >= 3 and home_score > away_score:
                _game_ended    = True
                _final_home_wp = 1.0
            elif is_home and home_score > away_score:
                _game_ended    = True
                _final_home_wp = 1.0
            elif is_home and _new_outs_total >= 3 and home_score != away_score:
                _game_ended    = True
                _final_home_wp = 0.0 if away_score > home_score else 1.0

        is_last = (i + 1 >= len(sorted_plays))

        if _game_ended:
            home_wp = _final_home_wp
        elif not is_last:
            nxt         = sorted_plays[i + 1]
            nxt_inning  = int(nxt.get("inning") or inning)
            nxt_half    = str(nxt.get("half") or half)
            nxt_outs    = int(nxt.get("outs") or 0)
            nxt_obc_raw = nxt.get("obc") or "000"
            nxt_obc     = str(nxt_obc_raw).zfill(3) if not isinstance(nxt_obc_raw, int) else _BRC_TO_OBC.get(nxt_obc_raw, "000")
            is_home_nxt = (nxt_half == "bottom")
            bat_score   = home_score if is_home_nxt else away_score
            fld_score   = away_score if is_home_nxt else home_score
            rem         = remaining_half_innings(nxt_inning, nxt_half, innings)
            wp_bat      = _wp_or(get_win_probability_interpolated(rem, nxt_outs, nxt_obc, bat_score - fld_score))
            home_wp     = wp_bat if is_home_nxt else 1.0 - wp_bat
        else:
            # Last play with no detected game-end - approximate from post-play state.
            # If the play ended the inning (new_outs >= 3) the WP table has no outs=3
            # entries, so flip to the opposing team at the start of the next half-inning
            # instead (mirrors what the non-last-play path does via the next play record).
            new_outs  = min(_new_outs_total, 3)
            new_obc_s, _ = advance_runners(result, obc, outs)
            bat_score = home_score if is_home else away_score
            fld_score = away_score if is_home else home_score
            if new_outs >= 3:
                nxt_rem = remaining_half_innings(inning, half, innings) - 1
                if nxt_rem >= 1:
                    opp_wp  = _wp_or(get_win_probability_interpolated(nxt_rem, 0, "000", -(bat_score - fld_score)))
                    home_wp = (1.0 - opp_wp) if is_home else opp_wp
                else:
                    home_wp = 1.0 if (home_score > away_score) else (0.5 if home_score == away_score else 0.0)
            else:
                rem     = remaining_half_innings(inning, half, innings)
                wp_bat  = _wp_or(get_win_probability_interpolated(rem, new_outs, new_obc_s, bat_score - fld_score))
                home_wp = wp_bat if is_home else 1.0 - wp_bat

        inn_lbl   = inning_label(inning, half)
        score_str = f"{away_team} {away_score} - {home_score} {home_team}"
        hover = (
            f"<b>{inn_lbl}</b>  {outs} out  {obc_circles(obc)}<br>"
            f"{batter} vs {pitcher}<br>"
            f"<b>Result: {result}</b><br>"
            f"Score: {score_str}<br>"
            f"{(away_team if (1 - home_wp) >= 0.5 else home_team)} WP: {max(1 - home_wp, home_wp) * 100:.1f}%"
        )

        rows.append({
            "play_idx":   i + 1,
            "inn_label":  inn_lbl,
            "outs":       outs,
            "obc":        obc,
            "batter":     batter,
            "pitcher":    pitcher,
            "result":     result,
            "home_score": home_score,
            "away_score": away_score,
            "home_wp":    home_wp,
            "hover":      hover,
        })

    # Final bookend - only when a game-ending condition was detected from the play data.
    # Uses DB scores if recorded, otherwise the running score tracker.
    if _game_ended:
        _fb_away = int(final_away) if final_away is not None else away_score
        _fb_home = int(final_home) if final_home is not None else home_score
        if not rows or rows[-1]["inn_label"] != "Final":
            rows.append({
                "play_idx":   len(sorted_plays) + 1,
                "inn_label":  "Final",
                "outs":       3,
                "obc":        "000",
                "batter":     "",
                "pitcher":    "",
                "result":     "Final",
                "home_score": _fb_home,
                "away_score": _fb_away,
                "home_wp":    _final_home_wp,
                "hover":      f"Final: {away_team} {_fb_away} - {_fb_home} {home_team}",
            })

    return pd.DataFrame(rows)


# Result ranges: (result, diff_low, diff_high) - from the league result table
RESULT_RANGES = [
    ("HR",    0,   20),
    ("3B",   21,   25),
    ("2BWH", 26,   27),
    ("2B",   28,   54),
    ("1BWH", 55,   60),
    ("1B",   61,  133),
    ("IF1B", 134, 142),
    ("BB",   143, 176),
    ("GORA", 177, 204),
    ("DSacF",205, 207),
    ("SacF", 208, 251),
    ("PO",   252, 271),
    ("FCH",  272, 290),
    ("K",    291, 406),
    ("DP21", 407, 437),
    ("DP31", 438, 467),
    ("DPH1", 468, 500),
]

_RESULT_ZONE_COLORS = {
    # Hits - green spectrum (best → marginal)
    "HR":    "#1a7d35",
    "3B":    "#2ca02c",
    "2BWH":  "#57b857",
    "2B":    "#93d493",
    "1BWH2": "#aedda2",
    "1BWH":  "#c4e8a4",
    "1B":    "#e5f5c3",
    "IF1B":  "#fff7bc",
    "BB":    "#fee391",
    # Soft outs / sac - yellow → orange
    "GORA":  "#fec44f",
    "DSacF": "#fe9929",
    "DFO":   "#fd8c15",
    "SacF":  "#fd7a1a",
    "FO":    "#f56010",
    # Standard outs - orange-red → red
    "PO":    "#f03b20",
    "FCH":   "#d42020",
    "FC":    "#c42020",
    "FC3rd": "#b82020",
    "GO":    "#aa1020",
    "LO":    "#c8102e",
    "K":     "#b10026",
    # Double plays - dark red → maroon
    "DPRun": "#920026",
    "DP":    "#880026",
    "DP21":  "#800026",
    "DP31":  "#5a001a",
    "DPH1":  "#3d0014",
    # Line-out DPs / triple plays - near black
    "LODP":  "#2d000f",
    "TP":    "#220009",
    "LOTP":  "#180006",
}

# Bunt result -> swing equivalent for color lookup
_BUNT_TO_SWING: dict[str, str] = {
    "SacB":   "SacF",
    "DSacB":  "DSacF",
}

def _result_color(result: str) -> str:
    """Return the zone color for a result, mapping bunt variants to their swing equivalents."""
    if result in _RESULT_ZONE_COLORS:
        return _RESULT_ZONE_COLORS[result]
    # Explicit bunt overrides (e.g. SacB -> SacF)
    swing = _BUNT_TO_SWING.get(result)
    if swing:
        return _RESULT_ZONE_COLORS.get(swing, "#cccccc")
    # Generic B-prefix stripping: B1B -> 1B, BFC -> FC, BGO -> GO, etc.
    if result.startswith("B") and result[1:] in _RESULT_ZONE_COLORS:
        return _RESULT_ZONE_COLORS[result[1:]]
    return "#cccccc"


ZONES = [
    (1,   111,  "1-111"),
    (112, 222,  "112-222"),
    (223, 333,  "223-333"),
    (334, 444,  "334-444"),
    (445, 555,  "445-555"),
    (556, 666,  "556-666"),
    (667, 777,  "667-777"),
    (778, 888,  "778-888"),
    (889, 1000, "889-1000"),
]
ZONE_LABELS = [z[2] for z in ZONES]

# Zone grid: displayed high→low, left→right, top→bottom (matches spreadsheet layout)
ZONE_GRID = [
    ["223-333", "112-222", "1-111"],
    ["556-666", "445-555", "334-444"],
    ["889-1000", "778-888", "667-777"],
]

# Delta range buckets (pitch change from previous at-bat)
DELTA_RANGES = [
    (-500, -400, "-500 to -400"),
    (-399, -300, "-399 to -300"),
    (-299, -200, "-299 to -200"),
    (-199, -100, "-199 to -100"),
    (-99,  -50,  "-99 to -50"),
    (-49,    0,  "-49 to 0"),
    (1,     50,  "1 to 50"),
    (51,   100,  "51 to 100"),
    (101,  200,  "101 to 200"),
    (201,  300,  "201 to 300"),
    (301,  400,  "301 to 400"),
    (401,  500,  "401 to 500"),
]

TEAM_ABBREV: dict[str, str] = {
    "CC": "Couriers",
    "JJ": "Jammers",
    "TT": "Tridents",
    "SLS": "Sharks",
}

BRC_TO_OBC: dict[int, str] = {
    0: "000", 1: "001", 2: "010", 3: "011",
    4: "100", 5: "101", 6: "110", 7: "111",
}


# ------------------------------------------------------------------ calculations

def circular_diff(pitch: int, swing: int) -> int:
    """Circular distance on 1-1000 wheel (1 and 1000 are adjacent)."""
    d = abs(pitch - swing)
    return min(d, 1000 - d)


def circular_signed_delta(a: int, b: int) -> int:
    """Signed delta on the 1-1000 wheel using the shortest path. Range: -500 to +500."""
    d = b - a
    if d > 500:
        d -= 1000
    elif d < -500:
        d += 1000
    return d


def _shift_to_domain(lo, hi, dlo, dhi):
    """Widen-and-shift the interval [lo, hi] to sit inside [dlo, dhi], preserving
    its width whenever the domain is wide enough. If it underruns dlo, slide it up
    to [dlo, dlo + width]; if it overruns dhi, slide it down to [dhi - width, dhi];
    if it is wider than the whole domain, clamp to [dlo, dhi]. THE single shared
    implementation of the boundary shift used by both the OBP recommended-bucket
    builders (obp_bounded_partition) and the Stage 6 centered-match retrofit
    (_centered_match_interval) - one convention, not several copies."""
    width = hi - lo
    if width >= dhi - dlo:
        return dlo, dhi
    if lo < dlo:
        return dlo, dlo + width
    if hi > dhi:
        return dhi - width, dhi
    return lo, hi


def _centered_match_interval(center, bucket_size, domain_hi=500):
    """Inclusive [lo, hi] for a centered distance filter, widened-and-shifted to
    stay inside [0, domain_hi] rather than truncating at an edge. Preserves the
    shipped span of 2 * (bucket_size // 2) + 1 integers (half = bucket_size // 2);
    callers replace the `|v - center| <= half` mask with `series.between(lo, hi)`.
    When 2 * half >= domain_hi (e.g. a 500-wide bucket) the interval is the whole
    domain, i.e. unchanged behavior."""
    half = bucket_size // 2
    lo, hi = _shift_to_domain(center - half, center + half, 0, domain_hi)
    return int(lo), int(hi)


def _circ_dist_vec(series: pd.Series, ref: int) -> pd.Series:
    """Vectorized circular distance on [1, 1000]. NaN inputs produce NaN output."""
    d = (series.astype(float) - float(ref)).abs()
    return d.where(d <= 500, 1000.0 - d)


def get_zone(value: int) -> str:
    for lo, hi, label in ZONES:
        if lo <= value <= hi:
            return label
    return "Unknown"


def get_delta_range(delta: float) -> str:
    for lo, hi, label in DELTA_RANGES:
        if lo <= delta <= hi:
            return label
    return "Other"


def _circ_delta_group(group: pd.Series) -> pd.Series:
    vals = group.astype(int).tolist()
    deltas = [float("nan")] + [circular_signed_delta(vals[i - 1], vals[i]) for i in range(1, len(vals))]
    return pd.Series(deltas, index=group.index)


def _wrap_delta2(diff: pd.Series) -> pd.Series:
    """Wrap a raw difference between two signed deltas into (-500, 500],
    mirroring circular_signed_delta but vectorized for a pandas Series. A
    signed delta already lives on a width-1000 range, so the difference of two
    of them wraps the same way a raw position difference does - without the
    wrap, two deltas that are nearly identical in real terms but happen to sit
    on opposite sides of the +/-500 seam (e.g. +498 and -497) would look like a
    ~1000-point swing instead of the ~5-point swing they actually are."""
    d = diff.where(diff <= 500, diff - 1000)
    d = d.where(d >= -500, d + 1000)
    return d


def _approach_group(g: pd.DataFrame) -> pd.Series:
    """1 if pitcher moved closer to prev batter's swing, 0 if further, NaN for first pitch."""
    pitches = g["pitch"].astype(int).tolist()
    swings = g["swing"].astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(g)):
        prev_dist = abs(circular_signed_delta(pitches[i - 1], swings[i - 1]))
        curr_dist = abs(circular_signed_delta(pitches[i], swings[i - 1]))
        results.append(1.0 if curr_dist < prev_dist else 0.0)
    return pd.Series(results, index=g.index)


def _wraparound_group(group: pd.Series) -> pd.Series:
    """1 if pitch crosses the 1000/1 border given a prior pitch in the boundary
    zone (>=850 or <=150), 0 if it didn't cross despite being eligible, NaN if
    ineligible (prior pitch not in the boundary zone) or the group's first
    pitch. Ineligible pitches must be NaN, not 0 - they're excluded from the
    denominator in the career-level wraparound_pct (see compute_pitcher_stats/
    compute_recent_pitcher_stats), so the per-pitch column feeding the rolling
    average in pitcher_ma_figure has to exclude them the same way or the
    moving-average line reads systematically lower than the career total."""
    vals = group.astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(vals)):
        prev, curr = vals[i - 1], vals[i]
        if prev >= 850 or prev <= 150:
            wrapped = (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850)
            results.append(1.0 if wrapped else 0.0)
        else:
            results.append(float("nan"))
    return pd.Series(results, index=group.index)


def _dd_group(group: pd.Series) -> pd.Series:
    """1 if this pitch lands within a circular 50-value window of the
    immediately prior pitch (a 'Double Down'), 0 if not, NaN for the group's
    first pitch (no prior pitch to compare against)."""
    vals = group.astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(vals)):
        results.append(1.0 if circular_diff(vals[i], vals[i - 1]) <= 50 else 0.0)
    return pd.Series(results, index=group.index)


def _td_group(group: pd.Series) -> pd.Series:
    """1 if this pitch AND the one before it both land within a circular
    50-value window anchored on the pitch two spots back (a 'Triple Down'),
    0 if not, NaN if fewer than two prior pitches exist in this pitcher's
    sequence. Anchored on the first pitch of the triple - not a drifting
    chain - so a Triple Down always contains a Double Down (pitch two vs.
    pitch one) but the reverse isn't required. Unconditional: every eligible
    3-pitch window counts toward the denominator, regardless of whether the
    first pair happened to be a DD - TD% is "how often does 3-in-a-row
    happen," not a continuation rate off an existing DD."""
    vals = group.astype(int).tolist()
    results = [float("nan")] * min(2, len(vals))
    for i in range(2, len(vals)):
        anchor = vals[i - 2]
        results.append(1.0 if circular_diff(vals[i - 1], anchor) <= 50
                        and circular_diff(vals[i], anchor) <= 50 else 0.0)
    return pd.Series(results, index=group.index)


_XBH  = {"HR", "3B", "2BWH", "2B"}
_BB1B = {"1BWH2", "1BWH", "1B", "IF1B", "BB"}
_OBR  = _XBH | _BB1B

# Bases per hit result (walks excluded from SLG per baseball convention)
_SLG_WEIGHTS = {
    "HR": 4, "3B": 3, "2BWH": 2, "2B": 2,
    "1BWH2": 1, "1BWH": 1, "1B": 1, "IF1B": 1, "BB": 1,
}

def get_res_category(result: str, diff: int) -> str:
    if diff >= 300:
        return "300+"
    if result in _XBH:
        return "XBH"
    if result in _BB1B:
        return "BB/1B"
    return "OUT"


# Canonical best -> worst result order, reused straight from the zone-color map
# so the prior-result signals can never drift from the established ranking.
_SEQ_RESULT_ORDER = list(_RESULT_ZONE_COLORS.keys())
# K and everything ordered at or worse than it, plus the two DP-adjacent results
# that _RESULT_ZONE_COLORS does not carry (see _DP_RESULTS below).
_SEQ_K_OR_WORSE = set(_SEQ_RESULT_ORDER[_SEQ_RESULT_ORDER.index("K"):]) | {"KCS", "BDP"}
# Fixed display / axis order for the prior-result signals.
SEQ_RESULT_CATEGORIES = ["XBH", "BB-1B", "Out", "K+"]
# Live result strings that carry none of the usual spellings but are plainly one
# of the four buckets. Found by diffing the plays table's distinct results
# against _RESULT_ZONE_COLORS; without these they would silently default to
# "Out" (IBB is a walk, AutoK a strikeout, DP32 a double play, and so on).
_SEQ_RESULT_ALIASES = {
    "IBB": "BB", "AutoBB": "BB", "AutoK": "K", "DP32": "DP",
}


def seq_result_category(result: str) -> str:
    """XBH / BB-1B / Out / K+ bucket for the prior-result Swing Suggestion signals.

    Distinct from get_res_category: no diff>=300 override - this is a pure
    result-string categorization. 'K+' is K and anything ordered at or worse
    than K in _RESULT_ZONE_COLORS's canonical best->worst key order (plus the
    DP-adjacent KCS/BDP, which aren't in that dict). Bunt variants resolve
    through _BUNT_TO_SWING and then the same generic B-prefix stripping
    _result_color uses (B1B -> 1B), so a bunt lands in its swing equivalent's
    bucket. Unrecognized result strings default to 'Out', mirroring
    get_res_category's own default.
    """
    r = _SEQ_RESULT_ALIASES.get(result, result)
    r = _BUNT_TO_SWING.get(r, r)
    if (r not in _RESULT_ZONE_COLORS and r not in _SEQ_K_OR_WORSE
            and r.startswith("B") and r[1:] in _RESULT_ZONE_COLORS):
        r = r[1:]
    if r in _XBH:
        return "XBH"
    if r in _BB1B:
        return "BB-1B"
    if r in _SEQ_K_OR_WORSE:
        return "K+"
    return "Out"


_OUT_RESULTS = {"GO", "FO", "PO", "K", "GORA", "DSacF", "DFO", "SacF", "FC", "LO",
                "LCO", "FC3rd", "FCH", "SacB", "CS", "CS2", "CS3", "CS4",
                "BFC"}
_DP_RESULTS  = {"DP", "DPH1", "DP21", "DP31", "DPRun", "LODP", "BDP", "KCS"}
_TP_RESULTS  = {"TP", "LOTP"}


def outs_added(result: str) -> int:
    if result in _TP_RESULTS:
        return 3
    if result in _DP_RESULTS:
        return 2
    if result in _OUT_RESULTS:
        return 1
    return 0


def get_expected_runs(outs: int, obc: str) -> float | None:
    """Look up expected runs for a given game state."""
    return RUN_EXPECTANCY.get((outs, obc))


def get_run_prob(outs: int, obc: str, exact: int | None = None, at_least: int | None = None) -> float:
    """Return P(runs=exact) or P(runs>=at_least) from the loaded run expectancy distribution."""
    dist = _re_dist.get((outs, obc), {})
    if exact is not None:
        return dist.get(exact, 0.0)
    if at_least is not None:
        return sum(p for r, p in dist.items() if r >= at_least)
    return 0.0


def steal_advance(obc: str, outs: int) -> tuple[str, int]:
    """Return (new_obc, runs_scored) when a steal is safe - ALL runners advance one base."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"
    runs = 1 if on_3b else 0   # 3B runner scores
    n3   = on_2b               # 2B runner advances to 3B
    n2   = on_1b               # 1B runner advances to 2B
    n1   = False               # no new runner enters from outside
    return f"{'1' if n3 else '0'}{'1' if n2 else '0'}{'1' if n1 else '0'}", runs


def steal_cs(obc: str) -> tuple[str, int]:
    """Return (new_obc, runs_scored) when a steal is caught - lead runner removed, others advance."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"
    if on_3b:
        # 3B runner caught; 2B->3B, 1B->2B
        n3, n2, n1 = on_2b, on_1b, False
    elif on_2b:
        # 2B runner caught; 1B->2B
        n3, n2, n1 = False, on_1b, False
    else:
        # 1B runner caught
        n3, n2, n1 = False, False, False
    return f"{'1' if n3 else '0'}{'1' if n2 else '0'}{'1' if n1 else '0'}", 0


def advance_runners(result: str, obc: str, outs_before: int) -> tuple[str, int]:
    """Map a result to new OBC and runs scored.

    Consults import_BRC.csv lookup first for accurate multi-run scenarios;
    falls back to hand-coded logic for unknown results.

    Returns (new_obc, runs_scored)
    """
    entry = _BRC_RUN_LOOKUP.get((result, obc, outs_before))
    if entry is not None:
        runs_f, new_obc, _ = entry
        return new_obc, int(round(runs_f))

    # Fallback: hand-coded approximation for results not in the lookup
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    runs = 0
    new_1b = False
    new_2b = False
    new_3b = False

    if result == "HR":
        runs = (1 if on_1b else 0) + (1 if on_2b else 0) + (1 if on_3b else 0) + 1
    elif result in ("3B",):
        runs = 1 if on_3b else 0
        new_3b = True
    elif result in ("2B", "2BWH"):
        runs = (1 if on_3b else 0) + (1 if on_2b else 0)
        new_3b = on_1b
        new_2b = True
    elif result in ("1B", "1BWH", "IF1B"):
        runs = 1 if on_3b else 0
        new_3b = on_2b
        new_2b = on_1b
        new_1b = True
    elif result == "BB":
        new_1b = True
        if on_1b:
            new_2b = True
            if on_2b:
                new_3b = True
            else:
                new_3b = on_3b
        else:
            new_2b = on_2b
            new_3b = on_3b
    elif result == "SacF":
        runs = 1 if on_3b else 0
        new_3b = False
        new_2b = on_2b
        new_1b = on_1b
    elif result == "CS":
        return steal_cs(obc)
    elif result in _TP_RESULTS:
        pass
    elif result in _OUT_RESULTS:
        new_3b = on_3b
        new_2b = on_2b
        new_1b = on_1b

    return f"{'1' if new_3b else '0'}{'1' if new_2b else '0'}{'1' if new_1b else '0'}", runs


def validate_ab(new: dict, prev: dict | None) -> list[str]:
    """Return a list of baseball-logic warnings for the new AB given the previous one."""
    if prev is None:
        return []

    warnings = []
    p_inn  = int(prev["inning"])
    p_half = prev.get("half", "top")
    p_outs = int(prev["outs"])
    p_res  = prev.get("result", "")

    n_inn  = int(new["inning"])
    n_half = new["half"]
    n_outs = int(new["outs"])

    added         = outs_added(p_res)
    expected_outs = p_outs + added
    same_half     = (n_inn == p_inn and n_half == p_half)

    if same_half:
        if expected_outs >= 3:
            warnings.append(
                f"Previous AB ({p_res}, {p_outs} outs) should have ended the half-inning - "
                f"expected a new half-inning, not the same one."
            )
        elif n_outs != expected_outs:
            warnings.append(
                f"Expected {expected_outs} out(s) based on previous result ({p_res}), "
                f"but got {n_outs}."
            )
    else:
        if n_outs != 0:
            warnings.append(f"New half-inning started but outs = {n_outs} (expected 0).")

        # Half-inning order: top → bottom of same inning → top of next inning
        if n_inn < p_inn:
            warnings.append(f"Inning went backward ({p_inn} → {n_inn}).")
        elif n_inn == p_inn:
            if not (p_half == "top" and n_half == "bottom"):
                warnings.append(
                    f"Unexpected half-inning change within inning {n_inn} "
                    f"({p_half} → {n_half})."
                )
        elif n_inn > p_inn + 1:
            warnings.append(f"Inning jumped by more than one ({p_inn} → {n_inn}).")
        elif n_half != "top":
            warnings.append(
                f"New inning {n_inn} should start at top, not bottom."
            )

    return warnings


def inning_label(inning: int, half: str) -> str:
    prefix = "T" if str(half).lower() == "top" else "B"
    return f"{prefix}{int(inning)}"


def enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    """Add diff, zone, res_category, FP flags, delta, and inning label columns."""
    if df.empty:
        return df
    df = df.copy()
    if "play_type" in df.columns:
        df = df[df["play_type"].str.lower() != "steal"]
    if df.empty:
        return df
    df["half"] = df["half"].fillna("top")

    sw = df["pitch"].notna() & df["swing"].notna()

    # Recompute diff for swing plays; steals already have diff stored from sheet
    if sw.any():
        df.loc[sw, "diff"] = df.loc[sw].apply(
            lambda r: circular_diff(int(r["pitch"]), int(r["swing"])), axis=1
        )

    df["pitch_zone"] = df["pitch"].apply(lambda p: get_zone(int(p)) if pd.notna(p) else None)
    df["swing_zone"] = df["swing"].apply(lambda s: get_zone(int(s)) if pd.notna(s) else None)
    df["res_category"] = df.apply(
        lambda r: get_res_category(r["result"], int(r["diff"])) if pd.notna(r.get("diff")) else "OUT",
        axis=1,
    )
    df["is_meme_pitch"] = df["pitch"].isin(MEME_NUMBERS)
    df["is_meme_swing"] = df["swing"].isin(MEME_NUMBERS)
    df["pitch_last2"] = df["pitch"].apply(
        lambda p: int(str(int(p)).zfill(2)[-2:]) if pd.notna(p) else None
    )
    df["inning_label"] = df.apply(lambda r: inning_label(r["inning"], r["half"]), axis=1)

    df = df.sort_values(["game_id", "id"])
    df["is_fp_inn"] = ~df.duplicated(subset=["game_id", "inning", "half"], keep="first")
    df["is_fp_app"] = ~df.duplicated(subset=["game_id", "pitcher_name"], keep="first")

    # Deltas only meaningful for swing plays
    df["pitch_delta"] = pd.NA
    df["pitch_circ_delta"] = pd.NA
    df["swing_circ_delta"] = pd.NA
    df["pitch_circ_delta2"] = pd.NA
    df["pitch_circ_delta2_signed"] = pd.NA
    df["pitch_approach"] = pd.NA
    df["pitch_wraparound"] = pd.NA
    df["pitch_dd"] = pd.NA
    df["pitch_td"] = pd.NA
    if sw.any():
        sw_df = df[sw]
        gk_pit = (sw_df["game_id"].astype(str) + "|" + sw_df["pitcher_name"].fillna(""))
        gk_bat = (sw_df["game_id"].astype(str) + "|" + sw_df["batter_name"].fillna(""))
        df.loc[sw, "pitch_delta"] = sw_df.groupby(gk_pit)["pitch"].diff()
        df.loc[sw, "pitch_circ_delta"] = sw_df.groupby(
            gk_pit, group_keys=False
        )["pitch"].apply(_circ_delta_group)
        df.loc[sw, "swing_circ_delta"] = sw_df.groupby(
            gk_bat, group_keys=False
        )["swing"].apply(_circ_delta_group)
        df.loc[sw, "pitch_wraparound"] = sw_df.groupby(
            gk_pit, group_keys=False
        )["pitch"].apply(_wraparound_group)
        df.loc[sw, "pitch_dd"] = sw_df.groupby(
            gk_pit, group_keys=False
        )["pitch"].apply(_dd_group)
        df.loc[sw, "pitch_td"] = sw_df.groupby(
            gk_pit, group_keys=False
        )["pitch"].apply(_td_group)
        # Second derivative and approach - re-read df to pick up pitch_circ_delta
        sw_df2 = df[sw]
        gk_pit2 = (sw_df2["game_id"].astype(str) + "|" + sw_df2["pitcher_name"].fillna(""))
        df.loc[sw, "pitch_circ_delta2_signed"] = sw_df2.groupby(
            gk_pit2, group_keys=False
        )["pitch_circ_delta"].apply(lambda g: _wrap_delta2(g.diff()))
        # |Δ²| is the magnitude of the wrapped signed second difference - see
        # `_wrap_delta2`.
        df.loc[sw, "pitch_circ_delta2"] = df.loc[sw, "pitch_circ_delta2_signed"].abs()
        # Use SeriesGroupBy (pitch only) with swing captured via closure to avoid
        # DataFrameGroupBy.apply returning a DataFrame in pandas 2.x
        _sw2_swing = sw_df2["swing"]
        def _approach_fn(pitch_grp: pd.Series) -> pd.Series:
            idx = pitch_grp.index
            pitches = pitch_grp.astype(int).tolist()
            swings  = _sw2_swing.loc[idx].astype(int).tolist()
            results = [float("nan")]
            for i in range(1, len(pitches)):
                prev_dist = abs(circular_signed_delta(pitches[i - 1], swings[i - 1]))
                curr_dist = abs(circular_signed_delta(pitches[i], swings[i - 1]))
                results.append(1.0 if curr_dist < prev_dist else 0.0)
            return pd.Series(results, index=idx)
        df.loc[sw, "pitch_approach"] = sw_df2.groupby(
            gk_pit2, group_keys=False
        )["pitch"].apply(_approach_fn)

    return df


def flatten_games(plays: list[dict]) -> pd.DataFrame:
    """Flatten nested game data from Supabase join into flat columns."""
    rows = []
    for play in plays:
        row = {k: v for k, v in play.items() if k != "games"}
        if play.get("games"):
            g = play["games"]
            row["season"] = g.get("season") or row.get("season")
            row["session_number"] = g.get("session_number")
            row["home_team"] = g.get("home_team")
            row["away_team"] = g.get("away_team")
            row["game_code"] = g.get("game_code")
            # Re-derive off_team/def_team from game records (already full names)
            if row.get("half") == "top":
                row["off_team"] = g.get("away_team")
                row["def_team"] = g.get("home_team")
            else:
                row["off_team"] = g.get("home_team")
                row["def_team"] = g.get("away_team")
        rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_scrimmage(plays: list[dict]) -> pd.DataFrame:
    """Convert flat scrimmage_plays rows into a DataFrame matching flatten_games schema."""
    if not plays:
        return pd.DataFrame()
    df = pd.DataFrame(plays)
    # Synthesize a numeric game_id from scrimmage_code so Scouting filters work
    if "scrimmage_code" in df.columns:
        df["game_id"] = pd.factorize(df["scrimmage_code"])[0] + 1
        df["game_code"] = df["scrimmage_code"]
    # Apply TEAM_ABBREV so team names are always full names
    for _tc in ("def_team", "off_team"):
        if _tc in df.columns:
            df[_tc] = df[_tc].map(lambda t: TEAM_ABBREV.get(t, t) if pd.notna(t) else t)
    return df


# ------------------------------------------------------------------ charts

def zone_heatmap(
    zone_counts: dict[str, int],
    title: str = "Zone Frequency",
    pct: bool = True,
) -> go.Figure:
    """3×3 heatmap of zone frequencies."""
    total = sum(zone_counts.values()) or 1
    z_vals = []
    z_text = []
    for row in ZONE_GRID:
        z_row, t_row = [], []
        for zone in row:
            count = zone_counts.get(zone, 0)
            z_row.append(count / total * 100)
            pct_str = f"{count / total * 100:.1f}%" if pct else ""
            t_row.append(f"<b>{zone}</b><br>{count}{f'<br>{pct_str}' if pct else ''}")
        z_vals.append(z_row)
        z_text.append(t_row)

    fig = go.Figure(go.Heatmap(
        z=z_vals,
        text=z_text,
        texttemplate="%{text}",
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=3,
        ygap=3,
        hovertemplate="%{text}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=260,
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        margin=dict(l=10, r=10, t=45, b=10),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def zone_polar(
    zone_counts: dict[str, int],
    title: str = "Zone Frequency",
    compact: bool = False,
) -> go.Figure:
    """Doughnut polar chart of zone frequencies.

    Zones are laid out clockwise from the top (pitch 1 at 12 o'clock).
    Coloring matches the 3x3 heatmap: blue=least frequent, white=mid, red=most frequent.
    compact=True uses smaller font and omits per-slice counts (for narrow column layouts).
    """
    zones_ordered = [
        "1-111", "112-222", "223-333",
        "334-444", "445-555", "556-666",
        "667-777", "778-888", "889-1000",
    ]
    total = sum(zone_counts.values()) or 1
    n = len(zones_ordered)
    deg_each = 360 / n
    counts = [zone_counts.get(z, 0) for z in zones_ordered]
    min_c, max_c = min(counts), max(counts)

    def _bwr(count: int) -> str:
        # Blue (#2166ac) -> white (#f7f7f7) -> red (#d6604d), matching zone_heatmap colorscale.
        t = (count - min_c) / (max_c - min_c) if max_c > min_c else 0.5
        if t <= 0.5:
            s = t * 2
            rv = int(33 + (247 - 33) * s)
            gv = int(102 + (247 - 102) * s)
            bv = int(172 + (247 - 172) * s)
        else:
            s = (t - 0.5) * 2
            rv = int(247 + (214 - 247) * s)
            gv = int(247 + (96 - 247) * s)
            bv = int(247 + (77 - 247) * s)
        return f"rgb({rv},{gv},{bv})"

    thetas = [i * deg_each + deg_each / 2 for i in range(n)]
    colors = [_bwr(c) for c in counts]
    hovers = [f"{z}<br>{c} ({c / total * 100:.1f}%)" for z, c in zip(zones_ordered, counts)]
    if compact:
        labels = [f"<b>{z}</b><br>{c / total * 100:.1f}%" for z, c in zip(zones_ordered, counts)]
    else:
        labels = [f"<b>{z}</b><br>{c}<br>{c / total * 100:.1f}%" for z, c in zip(zones_ordered, counts)]

    font_size = 8 if compact else 11

    fig = go.Figure()

    fig.add_trace(go.Barpolar(
        r=[1] * n,
        theta=thetas,
        width=[deg_each - 1.5] * n,
        marker_color=colors,
        marker_line_color="rgba(80,80,80,0.6)",
        marker_line_width=0,
        base=0.35,
        hovertext=hovers,
        hovertemplate="%{hovertext}<extra></extra>",
        showlegend=False,
    ))

    fig.add_trace(go.Scatterpolar(
        r=[1.05] * n,
        theta=thetas,
        mode="text",
        text=labels,
        textfont=dict(size=font_size, color="rgba(10,10,10,0.9)"),
        hoverinfo="skip",
        showlegend=False,
    ))

    fig.update_layout(
        title=dict(text=f"{title} (n={total})", x=0.5, xanchor="center", font=dict(size=13)),
        polar=dict(
            angularaxis=dict(
                direction="clockwise",
                rotation=90,
                showticklabels=False,
                showgrid=False,
                linewidth=0,
                ticks="",
            ),
            radialaxis=dict(
                visible=False,
                range=[0, 1.35],
                ticks="",
            ),
            bgcolor="rgba(0,0,0,0)",
        ),
        height=320,
        margin=dict(l=20, r=20, t=45, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        dragmode=False,
    )
    return fig


def _freq_bwr_color(count: float, min_c: float, max_c: float, alpha: float = 1.0) -> str:
    """Blue (least frequent) -> white (mid) -> red (most frequent), matching
    zone_heatmap's colorscale. Same formula as zone_polar's internal _bwr."""
    t = (count - min_c) / (max_c - min_c) if max_c > min_c else 0.5
    if t <= 0.5:
        s = t * 2
        rv = int(33 + (247 - 33) * s)
        gv = int(102 + (247 - 102) * s)
        bv = int(172 + (247 - 172) * s)
    else:
        s = (t - 0.5) * 2
        rv = int(247 + (214 - 247) * s)
        gv = int(247 + (96 - 247) * s)
        bv = int(247 + (77 - 247) * s)
    return f"rgba({rv},{gv},{bv},{alpha})"


def obr_gauge_donut(
    pitch_values: list[int],
    obr_lo: int,
    obr_hi: int,
    swing_val: int,
    padding: int = 100,
    bucket_width: int = 10,
    title: str = "OBR Pitch Frequency",
) -> go.Figure:
    """Speedometer-style partial donut: this pitcher's historical pitch frequency
    across the active OBR plus a padding zone on each side, at bucket_width
    granularity - fine enough to see exactly where thrown pitches thin out near
    the OBR edges, unlike zone_polar's coarse fixed 9-zone full circle.

    Circular over 1-1000. swing_val is always the OBR midpoint by construction
    (obr_lo/obr_hi come from swing +/- obr_max), so it's drawn at 12 o'clock and
    the OBR core spans outward symmetrically from there, with a padding-width
    buffer on each side for edge context. padding is clipped so the total span
    (OBR + both paddings) never exceeds the full circle.
    """
    obr_width = ((obr_hi - obr_lo) % 1000) + 1
    padding = max(0, min(padding, (1000 - obr_width) // 2))

    def _vals(start: int, width: int) -> list[int]:
        return [((start - 1 + k) % 1000) + 1 for k in range(width)]

    def _chunks(vals: list[int], w: int) -> list[list[int]]:
        return [vals[i:i + w] for i in range(0, len(vals), w)] if vals else []

    left_pad_vals  = _vals(((obr_lo - padding - 1) % 1000) + 1, padding) if padding else []
    core_vals      = _vals(obr_lo, obr_width)
    right_pad_vals = _vals(((obr_hi - 1) % 1000) + 2, padding) if padding else []

    counts: dict[int, int] = {}
    for v in pitch_values:
        v = int(v)
        counts[v] = counts.get(v, 0) + 1

    segments = (
        [(c, "pad")  for c in _chunks(left_pad_vals, bucket_width)] +
        [(c, "core") for c in _chunks(core_vals, bucket_width)] +
        [(c, "pad")  for c in _chunks(right_pad_vals, bucket_width)]
    )
    n = len(segments)
    fig = go.Figure()
    total = len(pitch_values)
    if n == 0:
        fig.update_layout(title=dict(text=f"{title} (no data)", x=0.5, xanchor="center"), height=340)
        return fig

    bucket_counts = [sum(counts.get(v, 0) for v in chunk) for chunk, _zone in segments]
    zones = [z for _c, z in segments]
    min_c, max_c = min(bucket_counts), max(bucket_counts)

    # Same theta convention as zone_polar (rotation=90, direction=clockwise
    # below -> theta=0 is 12 o'clock, increasing theta sweeps clockwise). The
    # gap sits at the bottom (theta=180): bucket 0 starts at 225 deg and
    # sweeps clockwise through 270/315/0(top)/45/90 up to 135 deg.
    span = 270.0
    deg_each = span / n
    gap_start = 180.0 + (360.0 - span) / 2.0  # 225 when span=270
    thetas = [(gap_start + (i + 0.5) * deg_each) % 360 for i in range(n)]
    core_idxs = [i for i, z in enumerate(zones) if z == "core"]

    colors, line_colors, line_widths, hovers = [], [], [], []
    for (chunk, zone), cnt in zip(segments, bucket_counts):
        is_core = zone == "core"
        colors.append(_freq_bwr_color(cnt, min_c, max_c, alpha=1.0 if is_core else 0.5))
        line_colors.append("rgba(20,20,20,0.85)" if is_core else "rgba(120,120,120,0.45)")
        line_widths.append(1.4 if is_core else 0.6)
        lo_v, hi_v = chunk[0], chunk[-1]
        lbl = f"{lo_v}" if lo_v == hi_v else f"{lo_v}-{hi_v}"
        pct = f"{cnt / total * 100:.1f}%" if total else "0%"
        hovers.append(f"{lbl}<br>{cnt} ({pct}){' - OBR' if is_core else ''}")

    fig.add_trace(go.Barpolar(
        r=[1] * n,
        theta=thetas,
        width=[deg_each * 0.92] * n,
        base=0.3,
        marker_color=colors,
        marker_line_color=line_colors,
        marker_line_width=line_widths,
        hovertext=hovers,
        hovertemplate="%{hovertext}<extra></extra>",
        showlegend=False,
    ))

    # OBR boundary spokes - dotted green, matching the OBR boundary color used
    # in swing_predictor_chart / manager_color_bar elsewhere on this page.
    if core_idxs:
        b_lo_theta = (gap_start + core_idxs[0] * deg_each) % 360
        b_hi_theta = (gap_start + (core_idxs[-1] + 1) * deg_each) % 360
        for th in (b_lo_theta, b_hi_theta):
            fig.add_trace(go.Scatterpolar(
                r=[0, 1.32], theta=[th, th], mode="lines",
                line=dict(color="#1a7d35", width=1.5, dash="dot"),
                hoverinfo="skip", showlegend=False,
            ))

    # Swing marker at 12 o'clock - the OBR midpoint by construction. Starts
    # outside the donut hole so it doesn't run through the center label.
    fig.add_trace(go.Scatterpolar(
        r=[0.32, 1.05], theta=[0, 0], mode="lines",
        line=dict(color="rgba(0,0,0,0.75)", width=2, dash="dash"),
        hoverinfo="skip", showlegend=False,
    ))
    fig.add_annotation(
        x=0.5, y=0.5, xref="paper", yref="paper",
        text=f"Swing<br><b>{swing_val}</b>", showarrow=False, align="center",
        font=dict(size=11), xanchor="center", yanchor="middle",
    )

    fig.update_layout(
        title=dict(text=f"{title} (n={total}, OBR {obr_lo}-{obr_hi})", x=0.5, xanchor="center", font=dict(size=13)),
        polar=dict(
            angularaxis=dict(direction="clockwise", rotation=90, showticklabels=False,
                              showgrid=False, linewidth=0, ticks=""),
            radialaxis=dict(visible=False, range=[0, 1.35], ticks=""),
            bgcolor="rgba(0,0,0,0)",
        ),
        height=340,
        margin=dict(l=20, r=20, t=55, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        dragmode=False,
    )
    return fig


def delta_histogram(
    deltas: pd.Series,
    title: str = "Pitch Delta Distribution",
    signed: bool = True,
) -> go.Figure:
    """Bar chart of circular deltas. Signed: green/red by direction. Unsigned: neutral blue."""
    deltas = deltas.dropna()
    if deltas.empty:
        return go.Figure()

    if not signed:
        deltas = deltas.abs()

    bin_size = 50
    bins = list(range(-500, 501, bin_size)) if signed else list(range(0, 501, bin_size))
    counts, edges = np.histogram(deltas.astype(float), bins=bins)
    centers = [(edges[i] + edges[i + 1]) / 2 for i in range(len(edges) - 1)]
    total = int(counts.sum())

    if signed:
        colors = [
            "#4CAF50" if c > 25 else "#d6604d" if c < -25 else "#888888"
            for c in centers
        ]
    else:
        colors = ["#4C78A8"] * len(centers)

    hover = [
        f"{int(edges[i]):+d} to {int(edges[i + 1]):+d}: {counts[i]} ({counts[i] / total * 100:.1f}%)"
        for i in range(len(counts))
    ]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=centers, y=counts,
        marker_color=colors,
        marker_line_width=0,
        hovertext=hover, hoverinfo="text",
        name="",
    ))
    mean_val = float(deltas.mean())
    mean_fmt = f"{mean_val:.0f}" if not signed else f"{mean_val:+.0f}"
    fig.add_vline(
        x=mean_val, line_dash="dot",
        line_color="rgba(255,255,100,0.75)", line_width=1.5,
        annotation_text=f"Mean {mean_fmt}",
        annotation_position="top right",
        annotation_font=dict(size=10, color="rgba(255,255,100,0.85)"),
    )
    x_title = "|Δ|" if not signed else "Δ"
    x_range = [-25, 525] if not signed else [-525, 525]
    fig.update_layout(
        title=dict(text=f"{title} (n={total})", x=0.5, xanchor="center"),
        xaxis=dict(title=x_title, tickmode="linear", dtick=100, range=x_range),
        yaxis_title="Count",
        height=300,
        showlegend=False,
        bargap=0.06,
        margin=dict(l=45, r=10, t=52, b=45),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def between_inning_deltas(df: pd.DataFrame, value_col: str = "pitch") -> pd.Series:
    """Signed delta from last pitch of one inning to first pitch of the next, same game and pitcher/batter."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_sw = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    if len(df_sw) < 2:
        return pd.Series(dtype=float)

    df_sw["_inn_key"] = df_sw["inning"].astype(str) + "_" + df_sw["half"].fillna("").astype(str)
    df_sw["_prev_inn"] = df_sw.groupby(["game_id", group_col])["_inn_key"].shift(1)
    df_sw["_prev_val"] = df_sw.groupby(["game_id", group_col])[value_col].shift(1)

    mask = (
        df_sw["_prev_inn"].notna() &
        df_sw["_prev_val"].notna() &
        (df_sw["_inn_key"] != df_sw["_prev_inn"])
    )
    subset = df_sw[mask].copy()
    if subset.empty:
        return pd.Series(dtype=float)

    return subset.apply(
        lambda r: circular_signed_delta(int(r["_prev_val"]), int(r[value_col])), axis=1
    ).dropna()


def between_game_deltas(df: pd.DataFrame, value_col: str = "pitch") -> pd.Series:
    """Signed delta from last pitch of one game to first pitch of the next, same pitcher/batter."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_sw = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    if len(df_sw) < 2:
        return pd.Series(dtype=float)

    df_sw["_prev_game"] = df_sw.groupby([group_col])["game_id"].shift(1)
    df_sw["_prev_val"]  = df_sw.groupby([group_col])[value_col].shift(1)

    mask = (
        df_sw["_prev_game"].notna() &
        df_sw["_prev_val"].notna() &
        (df_sw["game_id"] != df_sw["_prev_game"])
    )
    subset = df_sw[mask].copy()
    if subset.empty:
        return pd.Series(dtype=float)

    return subset.apply(
        lambda r: circular_signed_delta(int(r["_prev_val"]), int(r[value_col])), axis=1
    ).dropna()


def between_game_delta_hint(df: pd.DataFrame, value_col: str = "pitch") -> dict | None:
    """Most likely |Δ| (fixed 100-unit bins) into the first pitch of a new game,
    given this player's historical between-game deltas. Unconditional (no prior
    value to condition on beyond "this is a game transition") - see
    between_game_deltas for the underlying signed-delta computation."""
    deltas = between_game_deltas(df, value_col)
    if deltas.empty:
        return None
    cats = pd.cut(deltas.abs().astype(int), bins=_DELTA_HM_BINS, labels=False,
                  right=True, include_lowest=True).dropna()
    if cats.empty:
        return None
    counts = cats.astype(int).value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {
        "delta_lo": best_bkt * delta_step,
        "delta_hi": (best_bkt + 1) * delta_step,
        "prob": counts.iloc[0] / len(cats),
        "n": int(len(cats)),
        "all_counts": all_counts,
        "delta_bucket_size": delta_step,
    }


def between_inning_delta_hint(df: pd.DataFrame, value_col: str = "pitch") -> dict | None:
    """Same as between_game_delta_hint but for within-game inning transitions."""
    deltas = between_inning_deltas(df, value_col)
    if deltas.empty:
        return None
    cats = pd.cut(deltas.abs().astype(int), bins=_DELTA_HM_BINS, labels=False,
                  right=True, include_lowest=True).dropna()
    if cats.empty:
        return None
    counts = cats.astype(int).value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {
        "delta_lo": best_bkt * delta_step,
        "delta_hi": (best_bkt + 1) * delta_step,
        "prob": counts.iloc[0] / len(cats),
        "n": int(len(cats)),
        "all_counts": all_counts,
        "delta_bucket_size": delta_step,
    }


_HOT_ZONE_LABELS = [
    "1-100", "101-200", "201-300", "301-400", "401-500",
    "501-600", "601-700", "701-800", "801-900", "901-1000",
]


def last_n_chart(
    df: pd.DataFrame,
    n: int = 20,
    pitch_col: str = "pitch",
    swing_col: str = "swing",
    title: str = "Last 20 At-Bats",
) -> go.Figure:
    """Line chart of the last N pitch and swing values in sequence."""
    df_last = df.sort_values("id").tail(n).reset_index(drop=True)
    x = list(range(1, len(df_last) + 1))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=df_last[pitch_col].astype(int),
        mode="lines+markers+text",
        name="Pitch",
        text=df_last[pitch_col].astype(int).astype(str),
        textposition="top center",
        textfont=dict(size=10),
        line=dict(color="#d6604d", width=2),
        marker=dict(size=5),
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df_last[swing_col].astype(int),
        mode="lines+markers+text",
        name="Swing",
        text=df_last[swing_col].astype(int).astype(str),
        textposition="bottom center",
        textfont=dict(size=10),
        line=dict(color="#2166ac", width=2),
        marker=dict(size=5),
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="At-Bat #", tickmode="linear", dtick=1),
        yaxis=dict(range=[0, 1080]),
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=10, t=60, b=40),
    )
    return fig


def last_n_delta_chart(
    df: pd.DataFrame,
    n: int = 20,
    value_col: str = "pitch",
    title: str = "Pitch Delta",
) -> go.Figure:
    """Bar chart of circular signed delta between consecutive pitch/swing values.
    Bar height = abs(circular delta). Green = went higher, red = went lower."""
    df_last = df.sort_values("id").tail(n).reset_index(drop=True)
    vals = df_last[value_col].astype(int).tolist()
    x = list(range(2, len(vals) + 1))
    deltas = [circular_signed_delta(vals[i - 1], vals[i]) for i in range(1, len(vals))]
    linear = [vals[i] - vals[i - 1] for i in range(1, len(vals))]

    colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in deltas]
    hover = [
        f"AB {i}: {vals[i-1]}→{vals[i]}<br>Circular: {deltas[i-1]:+d}<br>Linear: {linear[i-1]:+d}"
        for i in range(1, len(vals))
    ]

    fig = go.Figure(go.Bar(
        x=x,
        y=[abs(d) for d in deltas],
        marker_color=colors,
        text=[f"{d:+d}" for d in deltas],
        textposition="outside",
        textfont=dict(size=10),
        hovertext=hover,
        hoverinfo="text",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="At-Bat #", tickmode="linear", dtick=1),
        yaxis=dict(range=[0, 580], title="Distance"),
        height=250,
        margin=dict(l=40, r=10, t=45, b=40),
        showlegend=False,
    )
    return fig


def last_n_combined_chart(
    df: pd.DataFrame,
    n: int = 20,
    delta_col: str = "pitch",
    title: str = "Last N Pitches",
    swing_offset: bool = False,
    highlight_name: str | None = None,
    segment_games: bool = False,
    tick_weights: list[float] | None = None,
    pannable: bool = False,
    est_delta_overlay: bool = False,
) -> go.Figure:
    """Two-row subplot: pitch+swing lines on top, circular delta bars on bottom, shared x-axis.
    swing_offset: shifts swing markers right by 1 AB to show whether swing predicts next pitch.
    highlight_name: swing markers for that batter use a star symbol.
    segment_games: breaks lines at game boundaries; dashes lines across inning boundaries.
    """
    _df_filtered = df[df["pitch"].notna() & df["swing"].notna()].sort_values("id")
    df_last = _df_filtered.reset_index(drop=True) if pannable else _df_filtered.tail(n).reset_index(drop=True)
    n_actual = len(df_last)
    x_all = list(range(1, n_actual + 1))
    pitches = df_last["pitch"].astype(int).tolist()
    swings  = df_last["swing"].astype(int).tolist()
    results = df_last["result"].tolist() if "result" in df_last.columns else [None] * n_actual
    delta_vals = df_last[delta_col].dropna().astype(int).tolist()

    deltas = [circular_signed_delta(delta_vals[i - 1], delta_vals[i]) for i in range(1, n_actual)]
    linear = [delta_vals[i] - delta_vals[i - 1] for i in range(1, n_actual)]
    x_delta = list(range(2, n_actual + 1))
    colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in deltas]
    hover = [
        f"PA {i}: {delta_vals[i-1]}→{delta_vals[i]}<br>Circular: {deltas[i-1]:+d}<br>Linear: {linear[i-1]:+d}"
        for i in range(1, n_actual)
    ]

    if swing_offset and n_actual > 1:
        swing_x    = list(range(2, n_actual + 1))
        swing_y    = swings[:-1]
        result_offset = results[1:]
        swing_text = [str(s) for s in swing_y]
        n_swing_rows = n_actual - 1
        highlight_mask = (
            df_last["batter_name"].iloc[:-1].eq(highlight_name).tolist()
            if highlight_name and "batter_name" in df_last.columns else [False] * len(swing_x)
        )
    else:
        swing_x    = x_all
        swing_y    = swings
        swing_text = [str(s) for s in swings]
        result_offset = results
        n_swing_rows = n_actual
        highlight_mask = (
            df_last["batter_name"].eq(highlight_name).tolist()
            if highlight_name and "batter_name" in df_last.columns else [False] * n_actual
        )

    # ── segmentation helper ───────────────────────────────────────────────────
    can_segment = (
        segment_games
        and n_actual > 0
        and all(c in df_last.columns for c in ("game_id", "inning", "half"))
    )

    def _segs(xs, ys, n_rows):
        """Return (solid_x, solid_y, dash_x, dash_y).
        Inserts None into solid at game breaks; adds dashed connector pairs for inning breaks.
        n_rows: number of df_last rows that correspond to entries in xs/ys.
        """
        sx, sy, dx, dy = [], [], [], []
        for i, (xi, yi) in enumerate(zip(xs, ys)):
            sx.append(xi); sy.append(yi)
            if i < len(xs) - 1 and i < n_rows - 1:
                r0 = df_last.iloc[i]
                r1 = df_last.iloc[i + 1]
                g0, g1 = r0["game_id"], r1["game_id"]
                if pd.notna(g0) and pd.notna(g1):
                    same_game = g0 == g1
                elif "game_code" in df_last.columns:
                    same_game = r0["game_code"] == r1["game_code"]
                else:
                    same_game = True
                same_inn  = (r0["inning"] == r1["inning"] and r0["half"] == r1["half"])
                if not same_game:
                    sx.append(None); sy.append(None)
                elif not same_inn:
                    sx.append(None); sy.append(None)
                    dx += [xi, xs[i + 1], None]
                    dy += [yi, ys[i + 1], None]
        return sx, sy, dx, dy

    def _text(ys):
        return ["" if v is None else str(int(v)) for v in ys]

    # ── build figure ──────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35], vertical_spacing=0.06,
    )

    # Pitch trace
    if can_segment:
        p_sx, p_sy, p_dx, p_dy = _segs(x_all, pitches, n_actual)
        fig.add_trace(go.Scatter(
            x=p_sx, y=p_sy, mode="lines+markers+text", name="Pitch",
            legendgroup="pitch",
            text=_text(p_sy), textposition="top center", textfont=dict(size=10),
            line=dict(color="#d6604d", width=2), marker=dict(size=5),
        ), row=1, col=1)
        if p_dx:
            fig.add_trace(go.Scatter(
                x=p_dx, y=p_dy, mode="lines",
                legendgroup="pitch", showlegend=False, hoverinfo="skip",
                line=dict(color="#d6604d", width=2, dash="dash"),
            ), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(
            x=x_all, y=pitches, mode="lines+markers+text", name="Pitch",
            legendgroup="pitch",
            text=[str(p) for p in pitches], textposition="top center",
            textfont=dict(size=10), line=dict(color="#d6604d", width=2), marker=dict(size=5),
        ), row=1, col=1)

    # Swing trace
    swing_name = "Swing" + (" (offset +1)" if swing_offset else "")
    if highlight_name and any(highlight_mask):
        # Connecting line (segmented or plain), then separate marker traces
        if can_segment:
            s_sx, s_sy, s_dx, s_dy = _segs(swing_x, swing_y, n_swing_rows)
            fig.add_trace(go.Scatter(
                x=s_sx, y=s_sy, mode="lines", name=swing_name,
                legendgroup="swing",
                line=dict(color="#2166ac", width=2), showlegend=True, hoverinfo="skip",
            ), row=1, col=1)
            if s_dx:
                fig.add_trace(go.Scatter(
                    x=s_dx, y=s_dy, mode="lines",
                    legendgroup="swing", showlegend=False, hoverinfo="skip",
                    line=dict(color="#2166ac", width=2, dash="dash"),
                ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=swing_x, y=swing_y, mode="lines", name=swing_name,
                legendgroup="swing",
                line=dict(color="#2166ac", width=2), showlegend=True, hoverinfo="skip",
            ), row=1, col=1)
        _cx = [x for x, h in zip(swing_x, highlight_mask) if not h]
        _cy = [y for y, h in zip(swing_y, highlight_mask) if not h]
        _ct = [t for t, h in zip(swing_text, highlight_mask) if not h]
        if _cx:
            fig.add_trace(go.Scatter(
                x=_cx, y=_cy, mode="markers+text", text=_ct,
                legendgroup="swing",
                textposition="bottom center", textfont=dict(size=10),
                marker=dict(size=5, color="#2166ac"),
                showlegend=False, name=swing_name, hoverinfo="skip",
            ), row=1, col=1)
        _hx = [x for x, h in zip(swing_x, highlight_mask) if h]
        _hy = [y for y, h in zip(swing_y, highlight_mask) if h]
        _ht = [t for t, h in zip(swing_text, highlight_mask) if h]
        if _hx:
            fig.add_trace(go.Scatter(
                x=_hx, y=_hy, mode="markers+text", text=_ht,
                legendgroup="swing",
                textposition="bottom center", textfont=dict(size=10),
                marker=dict(symbol="star", size=10, color="#2166ac",
                            line=dict(color="white", width=0.5)),
                showlegend=False, name=swing_name, hoverinfo="skip",
            ), row=1, col=1)
    else:
        if can_segment:
            s_sx, s_sy, s_dx, s_dy = _segs(swing_x, swing_y, n_swing_rows)
            fig.add_trace(go.Scatter(
                x=s_sx, y=s_sy, mode="lines+markers+text", name=swing_name,
                legendgroup="swing",
                text=_text(s_sy), textposition="bottom center", textfont=dict(size=10),
                line=dict(color="#2166ac", width=2), marker=dict(size=5),
            ), row=1, col=1)
            if s_dx:
                fig.add_trace(go.Scatter(
                    x=s_dx, y=s_dy, mode="lines",
                    legendgroup="swing", showlegend=False, hoverinfo="skip",
                    line=dict(color="#2166ac", width=2, dash="dash"),
                ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=swing_x, y=swing_y, mode="lines+markers+text", name=swing_name,
                legendgroup="swing",
                text=swing_text, textposition="bottom center",
                textfont=dict(size=10), line=dict(color="#2166ac", width=2), marker=dict(size=5),
            ), row=1, col=1)

    # Bars first (no text — labels added as a separate trace on top of everything)
    fig.add_trace(go.Bar(
        x=x_delta, y=[abs(d) for d in deltas], marker_color=colors,
        hovertext=hover, hoverinfo="text",
        name="Delta", showlegend=False,
    ), row=2, col=1)

    # Estimated delta overlay on top of bars: swing[i] vs pitch[i-1] at each PA position
    if est_delta_overlay and n_actual > 1:
        est_deltas = [
            circular_signed_delta(pitches[j], swings[j + 1])
            for j in range(n_actual - 1)
        ]
        est_colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in est_deltas]
        est_hover  = [
            f"PA {x}: batter swing {swings[j+1]} vs prev pitch {pitches[j]} → est Δ {est_deltas[j]:+d}"
            for j, x in enumerate(x_delta)
        ]
        fig.add_trace(go.Scatter(
            x=x_delta, y=[abs(d) for d in est_deltas],
            mode="lines+markers", name="Est. Δ",
            line=dict(color="rgba(200,200,200,0.55)", width=1.5),
            marker=dict(color=est_colors, size=8, symbol="diamond",
                        line=dict(color="rgba(255,255,255,0.4)", width=0.5)),
            hovertext=est_hover, hoverinfo="text",
        ), row=2, col=1)

    # Delta labels rendered last so they sit above everything including est. delta markers
    fig.add_trace(go.Scatter(
        x=x_delta, y=[abs(d) for d in deltas],
        mode="text",
        text=[f"{d:+d}" for d in deltas],
        textposition="top center",
        textfont=dict(size=10),
        showlegend=False, hoverinfo="skip",
    ), row=2, col=1)

    # Add result labels as text below the delta bars
    result_text = [str(r) if r else "" for r in result_offset]
    result_x = list(range(1, len(result_offset) + 1)) if not swing_offset else list(range(2, len(result_offset) + 2))
    fig.add_trace(go.Scatter(
        x=result_x, y=[-20] * len(result_x),
        mode="text",
        text=result_text,
        textposition="bottom center",
        textfont=dict(size=9, color="gray"),
        showlegend=False,
        hoverinfo="skip",
        xaxis="x2", yaxis="y2",
    ), row=2, col=1)

    # Color-coded weight circles below result labels
    # tick_weights may cover only the last N entries (pannable shows all career PAs)
    if tick_weights is not None and 0 < len(tick_weights) <= n_actual:
        _wt_display = swing_offset and len(tick_weights) > 1
        _wt_vals = tick_weights[1:] if _wt_display else tick_weights
        _n_wt = len(_wt_vals)
        # Align circles to the last _n_wt positions of result_x
        _circle_x = result_x[-_n_wt:] if len(result_x) >= _n_wt else result_x
        _wt_aligned = _wt_vals[-len(_circle_x):]
        if _circle_x:
            _w = np.array(_wt_aligned, dtype=float)
            _wmin, _wmax = _w.min(), _w.max()
            _color_vals = (
                ((_w - _wmin) / (_wmax - _wmin)).tolist()
                if _wmax > _wmin else [0.5] * len(_wt_aligned)
            )
            fig.add_trace(go.Scatter(
                x=_circle_x, y=[-90] * len(_circle_x),
                mode="markers",
                marker=dict(
                    symbol="circle", size=10,
                    color=_color_vals,
                    colorscale=[[0, "#4575b4"], [0.5, "white"], [1, "#d73027"]],
                    cmin=0, cmax=1,
                    showscale=False,
                    line=dict(width=0.5, color="rgba(128,128,128,0.5)"),
                ),
                showlegend=False,
                hoverinfo="skip",
                xaxis="x2", yaxis="y2",
            ), row=2, col=1)

    _view_start = max(0.5, n_actual - 20 + 0.5) if pannable else 0.5
    x_range = [_view_start, n_actual + 0.5]
    fig.update_xaxes(tickmode="linear", dtick=1, range=x_range, showticklabels=False, row=1, col=1)
    fig.update_xaxes(
        title_text="← Older  ·  PA #  ·  Newer →",
        tickmode="linear", dtick=1, range=x_range, row=2, col=1,
    )
    fig.update_yaxes(range=[0, 1080], fixedrange=pannable, row=1, col=1)
    fig.update_yaxes(
        range=[-110, 540], fixedrange=pannable, row=2, col=1,
        tickmode="array", tickvals=[100, 200, 300, 400, 500],
    )

    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=45, r=10, t=60, b=60),
        dragmode="pan" if pannable else False,
        modebar_remove=(
            ["zoom2d", "select2d", "lasso2d", "zoomIn2d", "zoomOut2d",
             "autoScale2d", "resetScale2d", "toImage"]
            if pannable else
            ["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
             "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"]
        ),
    )
    return fig


def _diff_to_result(diff: int, ranges: list | None = None) -> str:
    for result, lo, hi in (ranges or RESULT_RANGES):
        if lo <= diff <= hi:
            return result
    return "?"


def fetch_scenario_ranges(sheet_urls: dict[str, str]) -> dict[str, list | None]:
    """Fetch result ranges from multiple scenario sheet URLs in parallel.

    sheet_urls: mapping of key -> URL, e.g. {"sheet_hnr": "https://..."}
    Returns same keys mapped to parsed range lists, or None if a fetch fails.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, list | None] = {k: None for k in sheet_urls}

    def _fetch(key: str, url: str):
        try:
            ranges, _, _, _, _, _ = parse_result_ranges_from_sheet(url)
            return key, ranges, None
        except Exception as _e:
            return key, None, str(_e)

    valid = {k: v for k, v in sheet_urls.items() if v}
    if not valid:
        return results

    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(valid)) as ex:
        futures = {ex.submit(_fetch, k, v): k for k, v in valid.items()}
        for fut in as_completed(futures):
            key, ranges, err = fut.result()
            results[key] = ranges
            if err:
                errors[key] = err

    results["_errors"] = errors  # type: ignore[assignment]
    return results


_GAMEPLAY_GID = "533199361"
_GAMEDAY_GID = "1498066521"

# Fixed 0-indexed layout of the two swing-range tables on the Gameplay tab of the
# stadium/scrimmage sheets. Positional so a deleted "Result" label can't break
# parsing. Name column; low/high at +2/+3 (one gap column in between).
_NORMAL_HEADER_ROW  = 15   # sheet row 16: Low / High labels
_NORMAL_COL         = 18   # normal-swing name (col S); low = col U, high = col V
_NORMAL_DATA_START  = 16   # sheet row 17
_NORMAL_DATA_END    = 33   # sheet row 34

_BUNT_HEADER_ROW    = 24   # sheet row 25: Low / High labels
_BUNT_COL           = 23   # bunt name (col X); low = col Z, high = col AA
_BUNT_DATA_START    = 25   # sheet row 26
_BUNT_DATA_END      = 33   # sheet row 34


def parse_result_ranges_from_sheet(sheet_url: str):
    """Fetch and parse result range tables from a public Google Sheet.

    Returns (normal_ranges, bunt_ranges, batter_name, pitcher_name, swing_type, infield_in).
    bunt_ranges is None if no second Result table is found.
    Range tables live at fixed positions on the Gameplay tab (gid 533199361);
    player names live on the Gameday tab (gid 1498066521).
    """
    import re
    sheet_id_match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    if not sheet_id_match:
        raise ValueError("Could not parse a Google Sheets ID from the URL.")
    sheet_id = sheet_id_match.group(1)

    def _fetch_raw(gid: str) -> pd.DataFrame:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        return pd.read_csv(url, header=None, dtype=str)

    def _scan_and_parse(df: pd.DataFrame):
        # Gate on the structural Low/High labels to confirm we're reading the
        # range tab (and not a stale/renumbered gid).
        try:
            _lo = str(df.iloc[_NORMAL_HEADER_ROW, _NORMAL_COL + 2]).strip().lower()
            _hi = str(df.iloc[_NORMAL_HEADER_ROW, _NORMAL_COL + 3]).strip().lower()
        except Exception:
            return None, None
        if _lo != "low" or _hi != "high":
            return None, None

        def _table(col, start, end):
            out: list[tuple[str, int, int]] = []
            for i in range(start, min(end, len(df) - 1) + 1):
                if col + 3 >= len(df.columns):
                    break
                name = str(df.iloc[i, col]).strip()
                if not name or name.lower() == "nan":
                    break
                try:
                    lo = int(float(str(df.iloc[i, col + 2]).strip()))
                    hi = int(float(str(df.iloc[i, col + 3]).strip()))
                except (ValueError, IndexError):
                    break
                out.append((name, lo, hi))
            return out or None

        return (
            _table(_NORMAL_COL, _NORMAL_DATA_START, _NORMAL_DATA_END),
            _table(_BUNT_COL, _BUNT_DATA_START, _BUNT_DATA_END),
        )

    def _cell(df, r, c):
        try:
            v = str(df.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else ""
        except Exception:
            return ""

    gp = _fetch_raw(_GAMEPLAY_GID)
    normal_ranges, bunt_ranges = _scan_and_parse(gp)

    if not normal_ranges:
        raise ValueError("Result table found but no rows could be parsed.")

    batter_name = pitcher_name = ""
    try:
        raw_gameday = _fetch_raw(_GAMEDAY_GID)
        batter_name  = _cell(raw_gameday, 11, 7)
        pitcher_name = _cell(raw_gameday, 10, 7)
    except Exception:
        pass

    # Swing type and Infield In toggle also live on the Gameplay tab.
    swing_type = "Normal Swing"
    infield_in = False
    try:
        _st = str(gp.iloc[1, 28]).strip()
        if _st.lower() not in ("nan", ""):
            swing_type = _st
        _ii = str(gp.iloc[2, 28]).strip().upper()
        infield_in = _ii in ("TRUE", "1", "YES")
    except Exception:
        pass

    return normal_ranges, bunt_ranges, batter_name, pitcher_name, swing_type, infield_in


_OBC_CODE_TO_STRING = {
    0: "000",
    1: "001",
    2: "010",
    3: "100",
    4: "011",
    5: "101",
    6: "110",
    7: "111",
}


def parse_gameplay_from_sheet(sheet_url: str) -> dict:
    """Fetch game state from two Gameplay sheet tabs.

    Outs: gid 1498066521 (Gameday), L8 (row 7, col 11)
    Runners: gid 533199361, S6/T6/U6 (row 5, cols 18/19/20) - non-zero = runner present

    Returns dict with keys: outs (int|None), obc (str|None), steal_runners (list).
    """
    import re as _re
    sheet_id_match = _re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    if not sheet_id_match:
        return {"outs": None, "obc": None, "steal_runners": []}
    sheet_id = sheet_id_match.group(1)
    base_url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/export?format=csv&gid="

    def _read_tab(gid):
        try:
            return pd.read_csv(base_url + gid, header=None, dtype=str)
        except Exception:
            return None

    def _cell(raw, r, c):
        try:
            v = str(raw.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else None
        except IndexError:
            return None

    # --- Outs from runner tab (gid 533199361), X9 (row 8, col 23) ---
    outs = None
    raw_gameday = _read_tab("1498066521")  # kept for steal ranges

    # --- Runners + outs from gid 533199361 ---
    # Outs: X9 (row 8, col 23); Runners: S6/T6/U6 (row 5, cols 18/19/20)
    on_1b = on_2b = on_3b = False
    raw_runners = _read_tab("533199361")
    if raw_runners is not None:
        _outs_raw = _cell(raw_runners, 8, 23)
        if _outs_raw:
            _m = _re.search(r'\d+', _outs_raw)
            if _m:
                outs = int(_m.group())
        s6 = _cell(raw_runners, 5, 18)  # S6 - runner on 1B
        t6 = _cell(raw_runners, 5, 19)  # T6 - runner on 2B
        u6 = _cell(raw_runners, 5, 20)  # U6 - runner on 3B
        on_1b = s6 is not None and s6 != "0"
        on_2b = t6 is not None and t6 != "0"
        on_3b = u6 is not None and u6 != "0"

    obc = f"{'1' if on_3b else '0'}{'1' if on_2b else '0'}{'1' if on_1b else '0'}"

    # Steal runner stubs (base only - safe range lookup TBD)
    runners = []
    base_order = {"3B": 0, "2B": 1, "1B": 2}
    for base, present in [("3B", on_3b), ("2B", on_2b), ("1B", on_1b)]:
        if present:
            runners.append({"base": base, "safe_range": _default_safe_rng_for(base, raw_runners)})
    runners.sort(key=lambda r: base_order.get(r["base"], 9))

    # Raw player IDs from S6/T6/U6 - used by caller to look up runner speeds
    runner_ids = {}
    if on_1b and s6 and s6 != "0":
        runner_ids["1B"] = s6
    if on_2b and t6 and t6 != "0":
        runner_ids["2B"] = t6
    if on_3b and u6 and u6 != "0":
        runner_ids["3B"] = u6

    return {"outs": outs, "obc": obc, "steal_runners": runners, "runner_ids": runner_ids}


def _default_safe_rng_for(base: str, raw_runners) -> int:
    """Read steal safe range from AB20:AB22 on runner tab (gid 533199361); default 50."""
    if raw_runners is None:
        return 50
    base_to_row = {"1B": 19, "2B": 20, "3B": 21}
    row_idx = base_to_row.get(base, 21)
    try:
        v = str(raw_runners.iloc[row_idx, 27]).strip()
        if v.lower() not in ("nan", ""):
            return int(float(v))
    except (IndexError, ValueError):
        pass
    return 50


def load_run_lookup_from_csv(csv_path: str = "import_BRC.csv") -> dict[tuple[str, str, int], tuple[float, str, int]]:
    """Load runs, after-OBC, and eOuts from import_BRC.csv.

    Returns dict mapping (result, before_obc, outs) -> (runs_scored, new_obc_code, eouts).
    eOuts = outs added by the play (0=hit/walk, 1=single out, 2=double play).
    Key includes outs because end-of-inning rows clear runners differently.
    """
    import os
    if not os.path.exists(csv_path):
        return {}

    df = pd.read_csv(csv_path)
    lookup: dict[tuple[str, str, int], tuple[float, str, int]] = {}

    if "Situation" not in df.columns or "Runs" not in df.columns:
        return {}

    cols = list(df.columns)

    for _, row in df.iterrows():
        situation = str(row["Situation"]).strip()
        try:
            runs   = float(row["Runs"])
            eouts  = int(float(row["eOuts"])) if "eOuts" in cols else 0
        except (ValueError, TypeError):
            continue

        parts = situation.split("_")
        if len(parts) < 3:
            continue
        try:
            outs      = int(parts[0])
            obc_code  = int(parts[1])
            result    = "_".join(parts[2:])
            before_obc = _OBC_CODE_TO_STRING.get(obc_code, "000")
        except (ValueError, KeyError):
            continue

        try:
            new_obc = _OBC_CODE_TO_STRING[int(float(row["OBC"]))]
        except (ValueError, TypeError, KeyError):
            continue

        lookup[(result, before_obc, outs)] = (runs, new_obc, eouts)

    return lookup


def project_from_deltas(recent_vals: list[int]) -> list[int]:
    """Apply each recent circular delta to the most recent value to get projected next positions."""
    if len(recent_vals) < 2:
        return []
    last_val = recent_vals[-1]
    return [((last_val + circular_signed_delta(recent_vals[i - 1], recent_vals[i]) - 1) % 1000) + 1
            for i in range(1, len(recent_vals))]


def project_from_delta2s(recent_vals: list[int]) -> list[int]:
    """Project pitch values using delta² patterns, branching both +/- for each delta².

    Each delta² is the magnitude of the wrapped signed difference between two
    consecutive signed deltas. For each recent delta², produces two projections: one
    where the last delta grows by that amount and one where it shrinks, covering both
    acceleration and deceleration.
    """
    if len(recent_vals) < 3:
        return []
    deltas  = [circular_signed_delta(recent_vals[i - 1], recent_vals[i]) for i in range(1, len(recent_vals))]
    delta2s = [abs(circular_signed_delta(deltas[i - 1], deltas[i])) for i in range(1, len(deltas))]
    last_val   = recent_vals[-1]
    last_delta = deltas[-1]
    result = []
    for d2 in delta2s:
        for sign in (+1, -1):
            result.append(((last_val + int(last_delta + sign * d2) - 1) % 1000) + 1)
    return result


_BATTING_QUALITY: dict[str, float] = {
    "HR": 1.00, "3B": 0.90, "2BWH": 0.80, "2B": 0.75,
    "1BWH2": 0.65, "1BWH": 0.60, "1B": 0.55, "IF1B": 0.50, "BB": 0.45,
    "GORA": 0.25, "DSacF": 0.30, "DFO": 0.05, "SacF": 0.25, "FO": 0.00,
    "PO": 0.00, "FCH": 0.00, "FC": 0.00, "FC3rd": 0.00,
    "GO": 0.00, "LO": 0.00, "LODP": 0.00,
    "K": 0.00, "DPRun": 0.10, "DP": 0.00, "DP21": 0.00,
    "DP31": 0.00, "DPH1": 0.00, "TP": 0.00, "LOTP": 0.00,
    "B1BWH": 0.60, "B1B": 0.55,
    "SacB": 0.20, "DSacB": 0.25, "BDP": 0.00,
}


def compute_pa_weights(
    df_tail: "pd.DataFrame",
    current_obc: str = "000",
    current_outs: int = 0,
    recency_slider: int = 50,
    result_slider: int = 50,
    state_slider: int = 50,
    g1: float = 34,
    g2: float = 33,
    g3: float = 33,
    result_offset: bool = False,
) -> list[float]:
    """Return per-PA relevance weights aligned with df_tail sorted by id ascending.

    At slider=50 for all factors, weights are uniform regardless of global weights.
    recency_slider: 0=weight older more, 50=equal, 100=weight recent more.
    result_slider:  0=weight good pitching results more, 50=equal, 100=weight good batting results more.
    state_slider:   0=equal, 100=weight PAs with similar OBC+Outs more.
    result_offset:  if True, weight row i by the result of row i-1 (previous pitch/swing).
    """
    import numpy as np
    n = len(df_tail)
    if n == 0:
        return []
    df_s = df_tail.sort_values("id").reset_index(drop=True)

    tr = (recency_slider - 50) / 50.0
    ts = (result_slider  - 50) / 50.0
    te = state_slider / 100.0

    g_total = max(g1 + g2 + g3, 1e-9)
    gn1, gn2, gn3 = g1 / g_total, g2 / g_total, g3 / g_total

    pos = np.linspace(0, 1, n) if n > 1 else np.array([0.5])
    recency_w = np.exp(tr * (2 * pos - 1) * 1.151)

    result_w = np.ones(n)
    for i in range(n):
        src_i = i - 1 if result_offset else i
        if result_offset and src_i < 0:
            q = 0.5  # no previous pitch - neutral weight
        else:
            r = df_s.iloc[src_i].get("result") if "result" in df_s.columns else None
            q = _BATTING_QUALITY.get(str(r) if pd.notna(r) else "", 0.5)
        result_w[i] = np.exp(ts * (2 * q - 1) * 1.151)

    state_w = np.ones(n)
    if "obc" in df_s.columns and "outs" in df_s.columns:
        cur_obc = current_obc.zfill(3)
        for i in range(n):
            row = df_s.iloc[i]
            pa_obc  = str(row["obc"]).zfill(3) if pd.notna(row.get("obc", None)) else "000"
            pa_outs = int(row["outs"]) if pd.notna(row.get("outs", None)) else 0
            obc_sim  = sum(a == b for a, b in zip(cur_obc, pa_obc)) / 3.0
            outs_sim = 1.0 - abs(current_outs - pa_outs) / 2.0
            similarity = 0.5 * obc_sim + 0.5 * outs_sim
            state_w[i] = np.exp(te * (2 * similarity - 1) * 1.151)

    def _norm01(w: "numpy.ndarray") -> "numpy.ndarray":
        wmin, wmax = w.min(), w.max()
        if wmax > wmin:
            return (w - wmin) / (wmax - wmin)
        return np.full_like(w, 0.5)

    combined = gn1 * _norm01(recency_w) + gn2 * _norm01(result_w) + gn3 * _norm01(state_w)
    mean_c = combined.mean()
    if mean_c > 0:
        combined = combined / mean_c
    return combined.tolist()


def _build_weight_array(vals: list[int], weights: list[float] | None = None) -> "numpy.ndarray":
    """Return a length-1000 probability weight array proportional to recent frequency.

    If weights is provided (same length as vals), each occurrence is weighted by its value.
    """
    import numpy as np
    from collections import Counter
    w = np.zeros(1000)
    if not vals:
        w[:] = 1.0 / 1000
        return w
    if weights is not None and len(weights) == len(vals):
        w_total = sum(weights) or 1.0
        for v, wt in zip(vals, weights):
            w[v - 1] += wt / w_total
    else:
        total = len(vals)
        for v, c in Counter(vals).items():
            w[v - 1] += c / total
    return w


def _scores_via_fft(w: "numpy.ndarray", diff_score_arr: "numpy.ndarray") -> "numpy.ndarray":
    """Circular convolution: scores[r] = Σ_v w[v] * diff_score[circ_dist(r+1, v+1)], via FFT."""
    import numpy as np
    kernel = np.array([diff_score_arr[min(d, 1000 - d)] for d in range(1000)])
    return np.real(np.fft.ifft(np.fft.fft(w) * np.fft.fft(kernel)))


def _diff_score_array(result_ranges: list, metric: str,
                       obr_extra: frozenset[str] = frozenset()) -> "numpy.ndarray":
    """Precompute a length-501 score array indexed by circular diff value.

    obr_extra: additional result names counted as on-base for the "obp" metric,
    on top of the standard _OBR set - e.g. {"SacF", "DSacF"} when a manager opts
    to treat sac flies as in-OBR.
    """
    import numpy as np
    obr_set = _OBR | obr_extra
    arr = np.zeros(501)
    for d in range(501):
        r = _diff_to_result(d, result_ranges)
        arr[d] = (1.0 if r in obr_set else 0.0) if metric == "obp" else float(_SLG_WEIGHTS.get(r, 0))
    return arr


def suggest_swing(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    weights: list[float] | None = None,
    obr_extra: frozenset[str] = frozenset(),
) -> tuple[int, float, int, float]:
    """Return (best_val, best_score, counter_val, counter_score) via FFT convolution.

    best: argmax if maximize else argmin. counter: the opposite extreme.
    weights: optional per-value relevance weights (same length as recent_opp_vals).
    """
    import numpy as np
    if not recent_opp_vals:
        return 500, 0.0, 500, 0.0
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals, weights),
        _diff_score_array(result_ranges, metric, obr_extra),
    )
    best_idx = int(np.argmax(scores) if maximize else np.argmin(scores))
    counter_idx = int(np.argmin(scores) if maximize else np.argmax(scores))
    return best_idx + 1, float(scores[best_idx]), counter_idx + 1, float(scores[counter_idx])


def swing_signal_strength(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    weights: list[float] | None = None,
    zone: str = "best",
    obr_extra: frozenset[str] = frozenset(),
) -> float:
    """Return signal strength 0-100%: how concentrated a score zone is.

    zone="best"  measures the target (green) half - scores above midpoint.
    zone="worst" measures the avoid (red) half  - scores below midpoint.
    Smaller hot zone = higher signal. 0% = flat. ~100% = single sharp spike.
    """
    import numpy as np
    if not recent_opp_vals:
        return 0.0
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals, weights),
        _diff_score_array(result_ranges, metric, obr_extra),
    )
    best  = float(np.max(scores) if maximize else np.min(scores))
    worst = float(np.min(scores) if maximize else np.max(scores))
    if (best - worst) < 1e-6:
        return 0.0
    mid = (best + worst) / 2.0
    above = (scores > mid) if zone == "best" else (scores < mid)
    n = len(above)
    # Double the array to catch hot zones that wrap across the 1000/1 boundary
    doubled = np.concatenate([above, above])
    max_run = cur = 0
    for val in doubled:
        if val:
            cur += 1
            if cur > max_run:
                max_run = cur
        else:
            cur = 0
    longest_run = min(max_run, n)
    return (1.0 - longest_run / n) * 100.0


def obp_zone_signal(
    pop: list[int],
    ranges: list,
    weights: list[float] | None,
    maximize: bool = True,
    paired: bool = False,
    obr_extra: frozenset[str] = frozenset(),
) -> dict | None:
    """Best-value zone for an 'OBP recent X' Suggestions row, OBR as a MAX width.

    Anchors on the same FFT-convolved score curve suggest_swing/swing_signal_strength
    use, at its argmax (best_val). Rather than always drawing a fixed best_val +/-
    obr_max band, this grows outward from best_val - independently on each side -
    while the score stays above the best/worst midpoint (the same threshold
    swing_signal_strength uses), capped at obr_max steps per side. A sharp, tight
    peak yields a narrow zone; a flat, undifferentiated region grows all the way
    out to the OBR cap. Either way the zone can never exceed the OBR width, and
    the z-score's baseline (n_bkts) is derived from whatever width actually got
    used - not the OBR cap - so a tight true peak is judged against a harder,
    more informative baseline instead of being diluted against the max width.

    The z-score is self-referential: it tests the SAME population that produced
    best_val (pop) against that same zone, not e.g. raw recent pitches for a
    Δ-projected row - consistent with best_val itself being computed from pop.

    paired: set True for a project_from_delta2s-style population, where pop is
    laid out as consecutive (grow, shrink) pairs from the SAME underlying |Δ²|
    observation. Those two points are a deterministic mirror of one real
    observation, not independent evidence - counting both as separate trials
    would double the true sample size and inflate the z-score. Paired mode
    counts each pair as ONE trial (a hit if EITHER branch lands in the zone),
    keeping n at the true number of independent historical |Δ²| observations.
    best_val still searches the full, un-deduped population - both directions
    are genuinely distinct candidates worth weighing when picking one best swing.

    Returns {"lo", "hi", "z", "n", "prob", "n_bkts"} or None if pop is empty or
    `ranges` has no OBR-classified result (the max width would be undefined).
    """
    if not pop:
        return None
    obr_set = _OBR | obr_extra
    obr_max = max((hi for result, _lo, hi in ranges if result in obr_set), default=0)
    if obr_max <= 0:
        return None
    scores = _scores_via_fft(_build_weight_array(pop, weights), _diff_score_array(ranges, "obp", obr_extra))
    best_idx = int(np.argmax(scores) if maximize else np.argmin(scores))
    worst = float(np.min(scores) if maximize else np.max(scores))
    best = float(np.max(scores) if maximize else np.min(scores))
    best_val = best_idx + 1

    if (best - worst) < 1e-6:
        # Flat score curve - no peak shape to shrink toward, fall back to the
        # full OBR width on both sides (previous fixed-band behavior).
        left = right = obr_max
    else:
        mid = (best + worst) / 2.0
        above = (scores > mid) if maximize else (scores < mid)
        left = 0
        for step in range(1, obr_max + 1):
            if not above[(best_idx - step) % 1000]:
                break
            left = step
        right = 0
        for step in range(1, obr_max + 1):
            if not above[(best_idx + step) % 1000]:
                break
            right = step

    lo = ((best_val - left - 1) % 1000) + 1
    hi = ((best_val + right - 1) % 1000) + 1

    def _in_zone(v):
        return (lo <= v <= hi) if lo <= hi else (v >= lo or v <= hi)

    if paired and len(pop) >= 2:
        n = len(pop) // 2
        in_zone = sum(
            1 for i in range(0, n * 2, 2)
            if _in_zone(pop[i]) or _in_zone(pop[i + 1])
        )
    else:
        n = len(pop)
        in_zone = sum(1 for v in pop if _in_zone(v))

    width = max(left + right, 1)
    n_bkts = 1000.0 / width
    prob = in_zone / n
    z = hint_zscore(prob, n, n_bkts)
    return {"lo": lo, "hi": hi, "z": z, "n": n, "prob": prob, "n_bkts": n_bkts}


def optimal_swing_chart(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    title: str = "Expected Score by Swing Value",
    compact: bool = False,
    weights: list[float] | None = None,
    obr_extra: frozenset[str] = frozenset(),
) -> go.Figure:
    """1-row gradient heatmap showing expected OBP or SLG for every possible swing value.

    Marks both the best value (green vline) and the counter/worst value (orange dotted vline).
    weights: optional per-value relevance weights (same length as recent_opp_vals).
    """
    import numpy as np
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals, weights),
        _diff_score_array(result_ranges, metric, obr_extra),
    )
    best_idx = int(np.argmax(scores) if maximize else np.argmin(scores))
    counter_idx = int(np.argmin(scores) if maximize else np.argmax(scores))
    best_val = best_idx + 1
    best_score = float(scores[best_idx])
    counter_val = counter_idx + 1
    counter_score = float(scores[counter_idx])

    colorscale = "RdYlGn" if maximize else "RdYlGn_r"
    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        z=[scores.tolist()],
        x=list(range(1, 1001)),
        y=[0],
        colorscale=colorscale,
        showscale=not compact,
        colorbar=dict(title=dict(text=metric.upper(), side="right"), thickness=12, len=0.8),
        hovertemplate=f"Swing: %{{x}}<br>Expected {metric.upper()}: %{{z:.3f}}<extra></extra>",
    ))

    # Best vline: two-layer (dark outline + white center)
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=best_val, x1=best_val, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw))
    # Counter vline: two-layer (dark outline + orange center), dotted
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,140,0,0.9)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=counter_val, x1=counter_val, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dot"))

    if compact:
        fig.add_annotation(
            x=best_val, y=0.78, yref="paper",
            text=f"↑{best_val} ({best_score:.3f})",
            showarrow=False, xanchor="left",
            font=dict(color="white", size=8),
            bgcolor="rgba(0,0,0,0.55)", borderpad=0,
        )
        fig.add_annotation(
            x=counter_val, y=0.22, yref="paper",
            text=f"↓{counter_val} ({counter_score:.3f})",
            showarrow=False, xanchor="left",
            font=dict(color="rgba(255,180,80,1)", size=8),
            bgcolor="rgba(0,0,0,0.55)", borderpad=0,
        )
    else:
        fig.add_annotation(
            x=best_val, y=0.75, yref="paper",
            text=f"Best: {best_val}<br>({best_score:.3f})",
            showarrow=True, arrowhead=2, arrowcolor="white", ax=40, ay=0,
            font=dict(color="white", size=9),
            bgcolor="rgba(0,0,0,0.6)", borderpad=2,
        )
        fig.add_annotation(
            x=counter_val, y=0.25, yref="paper",
            text=f"Counter: {counter_val}<br>({counter_score:.3f})",
            showarrow=True, arrowhead=2, arrowcolor="rgba(255,140,0,0.9)", ax=-40, ay=0,
            font=dict(color="rgba(255,180,80,1)", size=9),
            bgcolor="rgba(0,0,0,0.6)", borderpad=2,
        )
    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=10 if not compact else 8),
        ),
        yaxis=dict(visible=False),
        height=110 if compact else 130,
        margin=dict(l=10, r=10 if compact else 80, t=5 if compact else 10, b=35 if compact else 30),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ── Swing suggestion bars figure ────────────────────────────────────────────

def _merge_pitch_arcs(arcs: list[tuple[int | None, int | None]]) -> list[tuple[int, int]]:
    """Merge a Suggestions row's green-zone pitch arcs into the smallest set of
    non-touching, non-overlapping arcs on the 1-1000 wheel.

    Each input arc is (lo, hi); lo > hi means it wraps the 1/1000 seam (mirrors
    _colored_zone's convention). Uses a 1000-slot circular occupancy mask
    rather than analytic interval math so wrapping falls out by construction -
    once two or more arcs paint over the same or adjacent pitches, they render
    (and label) as one continuous zone instead of several abutting rectangles
    with a visible seam and duplicate boundary labels between them.
    """
    occ = [False] * 1000
    seen_any = False
    for lo, hi in arcs:
        if lo is None or hi is None:
            continue
        seen_any = True
        if lo <= hi:
            for v in range(lo, hi + 1):
                occ[v - 1] = True
        else:
            for v in range(lo, 1001):
                occ[v - 1] = True
            for v in range(1, hi + 1):
                occ[v - 1] = True
    if not seen_any:
        return []
    if all(occ):
        return [(1, 1000)]

    start = next(i for i in range(1000) if not occ[i])
    order = [(start + i) % 1000 for i in range(1000)]
    runs, run_start, prev_idx = [], None, None
    for idx in order:
        if occ[idx]:
            if run_start is None:
                run_start = idx
        elif run_start is not None:
            runs.append((run_start, prev_idx))
            run_start = None
        prev_idx = idx
    if run_start is not None:
        runs.append((run_start, prev_idx))
    return [(r0 + 1, r1 + 1) for r0, r1 in runs]


def hint_bars_figure(
    hints: list[dict],
    mode: str = "best",
    mobile: bool = False,
    prior_val: int | None = None,
    prior_val2: int | None = None,
    swing_val: int | None = None,
    obr_lo: int | None = None,
    obr_hi: int | None = None,
) -> go.Figure:
    """Stacked horizontal range bars for swing/pitch suggestions.

    hints keys: Signal, Strength, lo, hi, lo2, hi2, _zone_dist (optional list[int]).
    mode: "best" highlights the top zone(s) in green; "all" colors all 9 ZONES
          by relative frequency using a diverging green/red scale.
    mobile: compact layout - labels inside bars, l/r margins collapsed to ~5px.
    prior_val: most-recent pitch/swing; draws a dotted reference line + ▼ marker.
    """
    n = len(hints)
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[-0.5, n],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    _GRAY   = "rgba(0,0,0,0.85)"
    _GREEN  = "rgba(40,150,55,0.80)"
    _GREEN2 = "rgba(40,150,55,0.80)"
    _bar_half = 0.32 if mobile else 0.40

    def _zone_color(t: float) -> str:
        # t=0 -> black (blends into bar background). t=+1 -> vivid green. t=-1 -> vivid red.
        # Fixed alpha so color saturation alone signals the outlier.
        if t >= 0:
            r = int(30 - 0 * t)
            g = int(30 + 150 * t)
            b = int(30 - 0 * t)
        else:
            r = int(30 + 180 * abs(t))
            g = int(30 - 0 * abs(t))
            b = int(30 - 0 * abs(t))
        return f"rgba({r},{g},{b},0.85)"

    # Scouting-recency stoplight dots, collected per row and drawn as one marker
    # trace after the loop. Sit in the left gutter, just right of the row label.
    _STOP_COLORS = {"green": "#2e7d32", "yellow": "#f9a825", "red": "#c62828"}
    _STOP_DOT_X  = -105
    _stop_dot_y, _stop_dot_c = [], []

    for idx, h in enumerate(hints):
        y   = n - idx - 1
        y0  = y - _bar_half
        y1  = y + _bar_half

        fig.add_shape(type="rect", x0=0.5, x1=1000.5, y0=y0, y1=y1,
                      fillcolor=_GRAY, line=dict(color="rgba(255,255,255,0.35)", width=1))

        lo, hi    = h.get("lo"),  h.get("hi")
        lo2, hi2  = h.get("lo2"), h.get("hi2")
        zone_dist = h.get("_zone_dist")

        def _colored_zone(cz_lo, cz_hi, color):
            if cz_lo is None or cz_hi is None:
                return
            if cz_lo <= cz_hi:
                fig.add_shape(type="rect", x0=cz_lo - 0.5, x1=cz_hi + 0.5,
                              y0=y0, y1=y1, fillcolor=color, line=dict(width=0))
            else:
                fig.add_shape(type="rect", x0=cz_lo - 0.5, x1=1000.5,
                              y0=y0, y1=y1, fillcolor=color, line=dict(width=0))
                fig.add_shape(type="rect", x0=0.5, x1=cz_hi + 0.5,
                              y0=y0, y1=y1, fillcolor=color, line=dict(width=0))

        if mode == "all" and not h.get("_best_zone_only"):
            d2_counts = h.get("_d2sq_counts")
            last_delta_z = h.get("_last_delta_for_zone")
            delta_counts = h.get("_delta_counts")
            prior_pitch_z = h.get("_prior_pitch_for_zone")
            if d2_counts is not None and last_delta_z is not None and prior_pitch_z is not None:
                # Delta^2 row: compound each |Δ²| bucket through the SIGNED
                # delta2_to_pitch_ranges (mirrors project_from_delta2s). Because
                # last_pitch + (L ± d2) sweeps the wheel exactly once across all buckets
                # and both branches, the |Δ²| buckets tile without overlap, so the
                # greenest arcs correspond to Best Zone - same guarantee as the Δ painter.
                total_dc = sum(d2_counts)
                n_dc = len(d2_counts)
                dd2_bkt = h.get("_dd2_bkt_for_zone", 100)
                for di, cnt in enumerate(d2_counts):
                    raw_t = max(-1.0, min(1.0, cnt / total_dc * n_dc - 1.0)) if total_dc >= 5 else 0.0
                    color = _zone_color(raw_t) if total_dc >= 5 else _GRAY
                    for pr in delta2_to_pitch_ranges(prior_pitch_z, last_delta_z, di * dd2_bkt, (di + 1) * dd2_bkt):
                        _colored_zone(pr[0], pr[1], color)
            elif delta_counts is not None and prior_pitch_z is not None:
                # Delta row: paint each delta bucket's exact pitch range by its proportion.
                # Adjacent delta buckets tile the wheel without overlap, so the
                # greenest ranges will directly correspond to Best Zone.
                total_dc = sum(delta_counts)
                n_dc = len(delta_counts)
                dd_bkt = h.get("_dd_bkt_for_zone", 100)
                for di, cnt in enumerate(delta_counts):
                    raw_t = max(-1.0, min(1.0, cnt / total_dc * n_dc - 1.0)) if total_dc >= 5 else 0.0
                    color = _zone_color(raw_t) if total_dc >= 5 else _GRAY
                    r1, r2 = delta_to_pitch_ranges(prior_pitch_z, di * dd_bkt, (di + 1) * dd_bkt)
                    _colored_zone(r1[0], r1[1], color)
                    if r2[0] is not None:
                        _colored_zone(r2[0], r2[1], color)
            elif zone_dist is not None:
                # Non-delta row: color by zone bucket frequency (existing behavior).
                total_dist = sum(zone_dist)
                if total_dist > 0:
                    _zbkt = h.get("_zone_bucket_size", 111)
                    _zn   = 1000 // _zbkt
                    for bi in range(_zn):
                        lo_z  = bi * _zbkt + 1
                        hi_z  = min((bi + 1) * _zbkt, 1000)
                        count = zone_dist[bi] if bi < len(zone_dist) else 0
                        raw_t = max(-1.0, min(1.0, count / total_dist * _zn - 1.0))
                        color = _zone_color(raw_t) if total_dist >= 5 else _GRAY
                        fig.add_shape(type="rect",
                                      x0=lo_z - 0.5, x1=hi_z + 0.5,
                                      y0=y0, y1=y1,
                                      fillcolor=color, line=dict(width=0))
        else:
            def _bound_line(x):
                fig.add_shape(type="line", x0=x, x1=x, y0=y0, y1=y1,
                              line=dict(color="rgba(255,255,255,0.85)", width=1.5))

            def _bound_label(x, text, anchor):
                fig.add_annotation(
                    x=x, y=y, text=f"<b>{text}</b>",
                    showarrow=False,
                    xanchor=anchor, yanchor="middle",
                    font=dict(size=9, color="rgba(255,255,255,0.95)"),
                    bgcolor="rgba(0,0,0,0)",
                )

            # Contiguous/overlapping arms (e.g. a "grow" arc butting up against a
            # "shrink" arc) merge into one zone here, so they paint as a single
            # rectangle with no seam and label only their true outer edges.
            _all_ranges = [(lo, hi), (lo2, hi2)] + list(h.get("extra_ranges", []))
            _merged = _merge_pitch_arcs(_all_ranges)

            for m_lo, m_hi in _merged:
                _colored_zone(m_lo, m_hi, _GREEN)

            _bounds = []
            for m_lo, m_hi in _merged:
                _bound_line(m_lo)
                _bound_line(m_hi)
                _bounds.append((m_lo, "right"))
                _bounds.append((m_hi, "left"))
            _bounds.sort(key=lambda b: b[0])

            # Two DIFFERENT zones can still sit close enough that their outward-
            # facing labels collide in the gap between them (one zone's "hi"
            # label pushes right, the next zone's "lo" label pushes left, into
            # the same few pixels). Cluster boundary values within
            # _LABEL_MIN_GAP of each other and show one combined "lo-hi" label
            # centered in the gap instead of two overlapping ones; the
            # individual boundary lines are still drawn at their exact spots.
            _LABEL_MIN_GAP = 20
            _clusters = []
            for val, anchor in _bounds:
                if _clusters and val - _clusters[-1][-1][0] <= _LABEL_MIN_GAP:
                    _clusters[-1].append((val, anchor))
                else:
                    _clusters.append([(val, anchor)])

            for cluster in _clusters:
                if len(cluster) == 1:
                    _v, _a = cluster[0]
                    _bound_label(_v, _v, _a)
                else:
                    _lo_c, _hi_c = cluster[0][0], cluster[-1][0]
                    fig.add_annotation(
                        x=(_lo_c + _hi_c) / 2, y=y, text=f"<b>{_lo_c}–{_hi_c}</b>",
                        showarrow=False, xanchor="center", yanchor="middle",
                        font=dict(size=9, color="rgba(255,255,255,0.95)"),
                        bgcolor="rgba(0,0,0,0)",
                    )

        signal   = h.get("Signal", "")
        strength = h.get("Strength", "")

        # Scouting-recency stoplight: collect a colored dot for the left gutter.
        _stop_color = _STOP_COLORS.get(h.get("_stoplight"))
        if _stop_color:
            _stop_dot_y.append(y)
            _stop_dot_c.append(_stop_color)

        if mobile:
            _zs = h.get("_zscore")
            _sig_txt = f"<b>{signal}</b>  {_zs:.1f}" if _zs is not None else f"<b>{signal}</b>"
            fig.add_annotation(
                x=3, y=y1,
                text=_sig_txt,
                showarrow=False, xanchor="left", yanchor="bottom",
                font=dict(size=8, color="rgba(255,255,255,0.9)"),
                bgcolor="rgba(0,0,0,0)",
            )
            if strength:
                fig.add_annotation(
                    x=997, y=y1,
                    text=strength,
                    showarrow=False, xanchor="right", yanchor="bottom",
                    font=dict(size=8, color="rgba(255,255,255,0.85)"),
                    bgcolor="rgba(0,0,0,0)",
                )
        else:
            if strength:
                fig.add_annotation(
                    x=1.01, y=y,
                    xref="paper", yref="y",
                    text=strength,
                    showarrow=False, xanchor="left", yanchor="middle",
                    font=dict(size=10, color="rgba(255,255,255,0.92)"),
                    bgcolor="rgba(0,0,0,0)",
                )

    if _stop_dot_y:
        fig.add_trace(go.Scatter(
            x=[_STOP_DOT_X] * len(_stop_dot_y), y=_stop_dot_y,
            mode="markers",
            marker=dict(size=11 if mobile else 14, color=_stop_dot_c,
                        line=dict(color="rgba(255,255,255,0.55)", width=1)),
            showlegend=False, hoverinfo="skip", cliponaxis=False,
        ))

    # Reference lines and labels in three rows above the 1-1000 tick labels:
    #   Row 1 (16 px above plot): prior_val  - most-recent pitch/swing (yellow)
    #   Row 2 (29 px above plot): prior_val2 - 2nd-most-recent (yellow)
    #   Row 3 (44 px above plot): swing_val / obr_lo / obr_hi (blue)
    # Top margin is 60 px: ~14 px tick labels + 3 x 12 px label rows + buffer.
    _t_margin  = 60
    _b_margin  = 8
    _h_val_ref = (n * 38 + 73) if mobile else (n * 44 + 78)
    _plot_h    = max(1, _h_val_ref - _t_margin - _b_margin)

    _Y_LINE   = "rgba(255,230,100,0.30)"
    _Y_LABEL  = "rgba(255,230,100,0.95)"
    _B_LINE   = "rgba(80,160,255,0.35)"
    _B_LABEL  = "rgba(80,190,255,0.95)"

    def _ref_line(x: int, color: str) -> None:
        fig.add_shape(type="line", x0=x, x1=x, y0=-0.5, y1=n,
                      line=dict(color=color, width=2, dash="dot"))

    def _ref_label(x: int, offset_px: float, text: str, color: str) -> None:
        fig.add_annotation(
            x=x, y=1.0 + offset_px / _plot_h,
            xref="x", yref="paper",
            text=text, showarrow=False,
            xanchor="center", yanchor="bottom",
            font=dict(size=8.5, color=color),
            bgcolor="rgba(0,0,0,0)",
        )

    if prior_val is not None:
        _ref_line(prior_val, _Y_LINE)
        _ref_label(prior_val, 16, f"▼{prior_val}", _Y_LABEL)

    if prior_val2 is not None:
        _ref_line(prior_val2, _Y_LINE)
        _ref_label(prior_val2, 29, f"▼{prior_val2}", _Y_LABEL)

    if swing_val is not None:
        _ref_line(swing_val, _B_LINE)
        _ref_label(swing_val, 44, f"▼{swing_val}", _B_LABEL)
    if obr_lo is not None:
        _ref_line(obr_lo, _B_LINE)
        _ref_label(obr_lo, 44, f"◄{obr_lo}", _B_LABEL)
    if obr_hi is not None:
        _ref_line(obr_hi, _B_LINE)
        _ref_label(obr_hi, 44, f"{obr_hi}►", _B_LABEL)

    y_ticks  = list(range(n))
    y_labels = []
    for i in range(n):
        h_i = hints[n - 1 - i]
        sig = h_i.get("Signal", "")
        zs  = h_i.get("_zscore")
        y_labels.append(f"{sig}  {zs:.1f}" if zs is not None else sig)

    if mobile:
        yaxis_cfg  = dict(range=[-0.5, n - 0.3], tickmode="array", tickvals=y_ticks,
                          ticktext=[""] * n, showgrid=False, zeroline=False)
        margin_cfg = dict(l=5, r=5, t=60, b=8)
        height_val = n * 38 + 73
    else:
        yaxis_cfg  = dict(range=[-0.5, n - 0.3], tickmode="array", tickvals=y_ticks,
                          ticktext=y_labels, tickfont=dict(size=11),
                          showgrid=False, zeroline=False, automargin=True)
        margin_cfg = dict(l=170, r=155, t=60, b=8)
        height_val = n * 44 + 78

    fig.update_layout(
        xaxis=dict(
            range=[-120, 1070],
            side="top",
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=10),
        ),
        yaxis=yaxis_cfg,
        height=height_val,
        margin=margin_cfg,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        dragmode=False,
        modebar=dict(remove=["all"]),
    )
    return fig


def sequence_matches(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_val: int,
    prior_val2: int | None = None,
    domain: str = "value",
) -> dict | None:
    """Historical 4-point sequence windows centered on prior_val.

    For every point in the player's history whose value (domain="value"), |Δ|
    (domain="delta"), or |Δ²| (domain="delta2") lands within bucket_size // 2 of
    prior_val and that also has a same-game next value, build a window
    (p2, p1, v, nxt). The (v, nxt) pair
    follows seq2_hint's same-game discipline; p1/p2 are retrospective context
    only and are set to None across a game boundary.

    prior_val2 opts into 3-seq matching: when set, p1 must ALSO land within
    bucket_size // 2 of prior_val2 (same half-width, same circular/linear rule as
    prior_val). Since p1 is a same-game shift(1), this implicitly requires a
    same-game (p1, v, nxt) triple, mirroring seq3_hint's grouped-shift discipline.
    prior_val2=None leaves the 2-seq behavior unchanged. p2 stays unconstrained.

    Matching is ALWAYS centered on prior_val - domain="value" uses circular
    distance on 1-1000, domain="delta"/"delta2" use linear distance on [0, 500].

    Returns {"matches": [...], "n": int} or None if nothing matches. Each match:
      p2, p1 (int|None context), v (matched value), nxt (next value),
      season, session, game_code, result (the next pitch's play result).

    Player-agnostic: domain="value" groups by "pitcher_name" if value_col ==
    "pitch" else "batter_name"; domain="delta" reads the pre-computed
    "{value_col}_circ_delta" column and domain="delta2" reads
    "{value_col}_circ_delta2", both of which enrich_df already guards at each
    game's first pitch(es).
    """
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    gcols = ["game_id", group_col]

    df_s = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    if df_s.empty:
        return None

    if domain in ("delta", "delta2"):
        delta_col = f"{value_col}_circ_delta" if domain == "delta" else f"{value_col}_circ_delta2"
        if delta_col not in df_s.columns:
            return None
        df_s["_v"] = df_s[delta_col].abs()
    else:
        df_s["_v"] = df_s[value_col].astype(float)

    df_s["_nxt"] = df_s.groupby(gcols)["_v"].shift(-1)
    df_s["_p1"] = df_s.groupby(gcols)["_v"].shift(1)
    df_s["_p2"] = df_s.groupby(gcols)["_v"].shift(2)
    if "result" in df_s.columns:
        df_s["_nres"] = df_s.groupby(gcols)["result"].shift(-1)
    else:
        df_s["_nres"] = None

    half = bucket_size // 2
    if domain != "value":
        # Widen-and-shift the centered match on the bounded 0..500 delta/delta2 axis
        # (Decision 10) instead of truncating at the edges.
        _lo, _hi = _centered_match_interval(int(prior_val), bucket_size, 500)
        mask = df_s["_v"].between(_lo, _hi) & df_s["_v"].notna() & df_s["_nxt"].notna()
    else:
        dist = _circ_dist_vec(df_s["_v"], int(prior_val))
        mask = (dist <= half) & df_s["_v"].notna() & df_s["_nxt"].notna()
    if prior_val2 is not None:
        if domain != "value":
            _lo2, _hi2 = _centered_match_interval(int(prior_val2), bucket_size, 500)
            mask &= df_s["_p1"].notna() & df_s["_p1"].between(_lo2, _hi2)
        else:
            dist1 = _circ_dist_vec(df_s["_p1"], int(prior_val2))
            mask &= df_s["_p1"].notna() & (dist1 <= half)
    mt = df_s[mask]
    if mt.empty:
        return None

    has_season = "season" in mt.columns
    has_sess = "session_number" in mt.columns
    has_gc = "game_code" in mt.columns

    matches = []
    for _, r in mt.iterrows():
        _p2, _p1 = r["_p2"], r["_p1"]
        matches.append({
            "p2": int(_p2) if pd.notna(_p2) else None,
            "p1": int(_p1) if pd.notna(_p1) else None,
            "v": int(r["_v"]),
            "nxt": int(r["_nxt"]),
            "season": int(r["season"]) if has_season and pd.notna(r["season"]) else None,
            "session": int(r["session_number"]) if has_sess and pd.notna(r["session_number"]) else None,
            "game_code": r["game_code"] if has_gc and pd.notna(r["game_code"]) else None,
            "result": r["_nres"] if pd.notna(r["_nres"]) else None,
        })
    return {"matches": matches, "n": len(matches)}


# Fixed trace layout for sequence_viewer_figure. The count never varies with
# selection state (a selected bin filters *what's in* the level/marker traces,
# not how many traces exist), so the outcome Bar always lands at this index
# and the page-side click handler can filter on it.
SEQ_VIEWER_LEVELS = 6
SEQ_VIEWER_BAR_TRACE = SEQ_VIEWER_LEVELS + 2  # 6 line levels + markers + current


def _seq_bucket(v: int, group_bucket: int, value_domain: bool) -> tuple[int, float]:
    """Return (bin index, bin center) for a raw value under group_bucket."""
    if value_domain:
        n_bins = 1000 // group_bucket
        idx = min(max((int(v) - 1) // group_bucket, 0), n_bins - 1)
        center = idx * group_bucket + (group_bucket + 1) / 2.0
    else:
        n_bins = 500 // group_bucket
        idx = min(max(int(v) // group_bucket, 0), n_bins - 1)
        center = idx * group_bucket + group_bucket / 2.0
    return idx, center


def sequence_viewer_figure(
    matches: list[dict],
    current_seq: list[int],
    y_range: tuple[int, int],
    y_label: str,
    group_bucket: int,
    selected_bin: int | None = None,
    mode_note: str | None = None,
    mobile: bool = False,
) -> go.Figure:
    """Sequence-match chart: 4-position window on the left, outcome bars on the right.

    matches: list of dicts from sequence_matches (p2, p1, v, nxt + hover metadata).
    current_seq: up to 3 recent values, right-aligned to x=[0,1,2]; the reference
                 value sits at x=2 ("Pitch") and the open x=3 slot is the unknown.
    y_range: fixed y span so geometry does not shift between reruns. y_range[0]
             selects the domain: 1 -> value (1-1000, circular), 0 -> delta (0-500).
    group_bucket: width for grouping the historical paths AND for the outcome bins.
    selected_bin: outcome-bin index to highlight/filter to, or None for no filter.

    Historical paths are grouped by their (p2, p1, v, nxt) bucket signature and
    drawn through bucket centers, volume-encoded into SEQ_VIEWER_LEVELS quantized
    line traces (opacity/width scale with group frequency) so the trace count stays
    small regardless of how many distinct paths exist. The per-match marker trace
    keeps the raw, honest sample so nothing hides behind the grouping.

    When selected_bin is set, the level/marker traces are scoped to ONLY the
    matches landing in that bin (not dimmed alongside everything else), and
    volume levels are recomputed relative to just that subset - so "common vs.
    rare" stays meaningful for whatever slice is being looked at rather than
    diluted against the full history. The subtitle still discloses the total
    n, so sample size is disclosed in text even though it isn't drawn.

    Trace order is fixed (see SEQ_VIEWER_BAR_TRACE): 6 line levels, markers,
    current, outcome bar (always last). Idle traces carry empty arrays so the
    layout - and thus the bar's index - never shifts.
    """
    n = len(matches)
    value_domain = int(y_range[0]) == 1
    n_bins = (1000 // group_bucket) if value_domain else (500 // group_bucket)
    fig = make_subplots(
        rows=1, cols=2, shared_yaxes=True,
        column_widths=[0.85, 0.15], horizontal_spacing=0.02,
    )

    def _path_verts(centers):
        _c2, _c1, _cv, _cn = centers
        _pts = []
        if _c2 is not None:
            _pts.append((0, _c2))
        if _c1 is not None:
            _pts.append((1, _c1))
        _pts.append((2, _cv))
        _pts.append((3, _cn))
        return _pts

    # nxt bucket index for every match, always over the FULL set - the outcome
    # bar on the right always shows the whole distribution (it's the menu
    # being filtered from, not itself filtered).
    _nxt_idx_all = [_seq_bucket(m["nxt"], group_bucket, value_domain)[0] for m in matches]

    _dim = selected_bin is not None
    _active = (
        [m for m, ni in zip(matches, _nxt_idx_all) if ni == selected_bin]
        if _dim else matches
    )

    # Bucket the active set once; reuse for grouping and markers.
    _groups: dict[tuple, dict] = {}
    for m in _active:
        _p2 = _seq_bucket(m["p2"], group_bucket, value_domain) if m["p2"] is not None else (None, None)
        _p1 = _seq_bucket(m["p1"], group_bucket, value_domain) if m["p1"] is not None else (None, None)
        _v = _seq_bucket(m["v"], group_bucket, value_domain)
        _nx = _seq_bucket(m["nxt"], group_bucket, value_domain)
        _key = (_p2[0], _p1[0], _v[0], _nx[0])
        _g = _groups.get(_key)
        if _g is None:
            _groups[_key] = {"count": 1, "centers": (_p2[1], _p1[1], _v[1], _nx[1])}
        else:
            _g["count"] += 1

    # Rank-based (not linear-against-max) normalizer: with a long-tail
    # distribution - a few common paths, many one-off paths, which is the
    # normal case here - dividing by the single busiest group's count crushes
    # nearly everything into the bottom level. Ranking by distinct observed
    # count values instead guarantees the full visual range is used regardless
    # of how skewed the distribution is; equal counts always render identically.
    _distinct_counts = sorted({g["count"] for g in _groups.values()})
    _no_variance = len(_distinct_counts) <= 1
    _rank = {c: i for i, c in enumerate(_distinct_counts)}
    _max_rank = max(len(_distinct_counts) - 1, 1)
    # No variance (every group tied - could be one path repeated N times with
    # nothing else in the set, or dozens of true one-offs and nothing that
    # recurs at all) means there's nothing to RANK against, but the shared
    # count itself still carries information: a tie at count=1 is honestly
    # unremarkable and should render thin even if it's 100% of what's shown;
    # a tie at count=8+ is a real recurring pattern and should render bold
    # even with nothing else to compare it to. Log-anchored against a fixed
    # reference (not the local max) so this doesn't reintroduce the original
    # long-tail-collapse problem the rank approach exists to avoid.
    _no_var_t = min(1.0, math.log(1 + _distinct_counts[0]) / math.log(9)) if _no_variance else 0.0

    # Six quantized volume levels. Colour ramps within a muted blue-gray family
    # toward saturated steel blue (green/red are reserved for good/bad semantics).
    _lvl_x = [[] for _ in range(SEQ_VIEWER_LEVELS)]
    _lvl_y = [[] for _ in range(SEQ_VIEWER_LEVELS)]
    for _g in _groups.values():
        _t = _no_var_t if _no_variance else _rank[_g["count"]] / _max_rank
        _lvl = min(SEQ_VIEWER_LEVELS - 1, int(_t * SEQ_VIEWER_LEVELS))
        _verts = _path_verts(_g["centers"])
        for _x, _y in _verts:
            _lvl_x[_lvl].append(_x)
            _lvl_y[_lvl].append(_y)
        _lvl_x[_lvl].append(None)
        _lvl_y[_lvl].append(None)

    # Widened range (vs. the original 0.15-0.75 alpha / 1.5-6.0px / muted-only
    # color) so adjacent levels are visibly distinct rather than reading as one
    # mass of similarly-weighted lines: near-invisible haze at the low end,
    # bold and saturated at the high end.
    _legend_level = SEQ_VIEWER_LEVELS // 2  # one representative line in the legend
    for _l in range(SEQ_VIEWER_LEVELS):
        _tr = (_l + 1) / SEQ_VIEWER_LEVELS
        _r = int(150 + (40 - 150) * _tr)
        _g_ = int(155 + (125 - 155) * _tr)
        _b = int(160 + (215 - 160) * _tr)
        _alpha = 0.08 + 0.87 * _tr
        _width = 0.75 + 6.25 * _tr
        fig.add_trace(go.Scatter(
            x=_lvl_x[_l], y=_lvl_y[_l], mode="lines",
            line=dict(color=f"rgba({_r},{_g_},{_b},{_alpha:.3f})", width=_width),
            hoverinfo="skip",
            name="Historical matches" if _l == _legend_level else None,
            showlegend=_l == _legend_level,
        ), row=1, col=1)

    # Per-match markers: the raw, honest sample behind the grouped lines,
    # scoped to the same active set (all matches, or just the selected bin).
    _mk_x, _mk_y, _mk_cd = [], [], []
    for m in _active:
        _pts = []
        if m["p2"] is not None:
            _pts.append((0, m["p2"]))
        if m["p1"] is not None:
            _pts.append((1, m["p1"]))
        _pts.append((2, m["v"]))
        _pts.append((3, m["nxt"]))
        _cd = [
            m["season"] if m["season"] is not None else "?",
            m["session"] if m["session"] is not None else "?",
            m["game_code"] if m["game_code"] is not None else "?",
            m["v"], m["nxt"],
            m["result"] if m["result"] is not None else "?",
        ]
        for _x, _y in _pts:
            _mk_x.append(_x)
            _mk_y.append(_y)
            _mk_cd.append(_cd)

    _hover = (
        "S%{customdata[0]} W%{customdata[1]} · %{customdata[2]}<br>"
        "Matched %{customdata[3]} → Next %{customdata[4]}<br>"
        "Result: %{customdata[5]}<extra></extra>"
    )
    fig.add_trace(go.Scatter(
        x=_mk_x, y=_mk_y, mode="markers",
        marker=dict(size=5, color="rgba(150,160,170,0.55)", opacity=0.25),
        customdata=_mk_cd, hovertemplate=_hover,
        showlegend=False, name="Historical markers",
    ), row=1, col=1)

    # Current line: right-aligned so the reference value lands at x=2 ("Pitch").
    # Always added (empty when there is no history) to keep the trace count fixed.
    _seq = list(current_seq)[-3:]
    _k = len(_seq)
    _cur_x = [2 - (_k - 1 - i) for i in range(_k)]
    fig.add_trace(go.Scatter(
        x=_cur_x, y=_seq, mode="lines+markers",
        line=dict(color="rgba(255,230,100,0.95)", width=3, dash="dash"),
        marker=dict(size=8, color="rgba(255,230,100,0.95)"),
        hoverinfo="skip", name="Current", showlegend=True,
    ), row=1, col=1)

    # Right panel: outcome distribution. go.Bar (not Histogram) so we can colour
    # the selected bin per-bar. Bins reuse the nxt indices already computed.
    _counts = np.bincount(_nxt_idx_all, minlength=n_bins) if _nxt_idx_all else np.zeros(n_bins, dtype=int)
    _centers, _lo_hi, _bar_colors = [], [], []
    _accent = "rgba(90,150,210,0.95)"
    _base = "rgba(150,160,170,0.55)"
    for _bi in range(n_bins):
        if value_domain:
            _lo, _hi = _bi * group_bucket + 1, min((_bi + 1) * group_bucket, 1000)
            _centers.append(_bi * group_bucket + (group_bucket + 1) / 2.0)
        else:
            _lo, _hi = _bi * group_bucket, min((_bi + 1) * group_bucket, 500)
            _centers.append(_bi * group_bucket + group_bucket / 2.0)
        _lo_hi.append([_lo, _hi])
        _bar_colors.append(_accent if _bi == selected_bin else _base)
    fig.add_trace(go.Bar(
        x=list(_counts), y=_centers, orientation="h",
        width=group_bucket * 0.9,
        marker=dict(color=_bar_colors),
        customdata=_lo_hi,
        hovertemplate="%{customdata[0]}-%{customdata[1]}: %{x}<extra></extra>",
        showlegend=False, name="Outcomes",
    ), row=1, col=2)

    # Stamp the actual current-sequence value beneath each known position's
    # label (2nd Previous / Previous / current) - reuses the same right-aligned
    # _cur_x/_seq pairing the "Current" line above was drawn from, so the two
    # never disagree. "Next Pitch" (x=3) has no value yet - stays label-only.
    _base_labels = ["2nd Previous", "Previous Pitch", "Pitch", "Next Pitch"]
    _pos_val = dict(zip(_cur_x, _seq))
    _ticktext = [
        f"{lbl}<br>{int(round(_pos_val[i]))}" if i in _pos_val else lbl
        for i, lbl in enumerate(_base_labels)
    ]

    _height = 300 if mobile else 380
    fig.update_xaxes(
        range=[-0.2, 3.2], tickmode="array",
        tickvals=[0, 1, 2, 3],
        ticktext=_ticktext,
        tickfont=dict(size=9 if mobile else 10),
        showgrid=False, zeroline=False, fixedrange=True,
        row=1, col=1,
    )
    fig.update_xaxes(
        showticklabels=False, showgrid=False, zeroline=False,
        fixedrange=True, row=1, col=2,
    )
    fig.update_yaxes(
        range=list(y_range), title_text="", showgrid=True,
        gridcolor="rgba(255,255,255,0.06)", zeroline=False,
        fixedrange=True, automargin=False, row=1, col=1,
    )
    fig.update_yaxes(fixedrange=True, row=1, col=2)

    _sub = f"n={n} historical matches"
    if mode_note:
        _sub = f"{_sub} · {mode_note}"
    if _dim and 0 <= selected_bin < n_bins:
        _slo, _shi = _lo_hi[selected_bin]
        _sub = f"{_sub} · filtered to Next {_slo}-{_shi} ({int(_counts[selected_bin])})"
    fig.update_layout(
        title=dict(
            text=f"{y_label}<br><sup>{_sub}</sup>",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        height=_height,
        # automargin=False on the left y-axis (above) means this l value is
        # the real, final left margin, not just a floor Plotly can expand
        # past - sized to fit a 4-digit "1000" tick label without clipping.
        # b is taller than a single tick line needs - the 2nd Previous/Previous/
        # Pitch labels now carry a second <br> line (the current-sequence value).
        margin=dict(l=45, r=10, t=70, b=44),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
        bargap=0.05,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                    font=dict(size=9)),
        modebar=dict(remove=["all"]),
    )
    return fig


# ── Swing context hint helpers ──────────────────────────────────────────────

def optimal_swing_range(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    weights: list[float] | None = None,
) -> tuple[int, int] | None:
    """Return (lo, hi) of the widest contiguous above-midpoint zone, or None if flat.

    lo and hi are 1-indexed pitch values (1-1000). If lo > hi the zone wraps
    across the 1000/1 boundary.
    """
    if not recent_opp_vals:
        return None
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals, weights),
        _diff_score_array(result_ranges, metric),
    )
    best  = float(np.max(scores) if maximize else np.min(scores))
    worst = float(np.min(scores) if maximize else np.max(scores))
    if (best - worst) < 1e-6:
        return None
    mid = (best + worst) / 2.0
    above = (scores > mid) if maximize else (scores < mid)
    n = len(above)
    doubled = np.concatenate([above, above])
    max_run = cur = 0
    best_start = cur_start = 0
    for i, val in enumerate(doubled):
        if val:
            if cur == 0:
                cur_start = i
            cur += 1
            if cur > max_run:
                max_run = cur
                best_start = cur_start
        else:
            cur = 0
    run = min(max_run, n)
    if run == 0:
        return None
    lo = (best_start % n) + 1
    hi = ((best_start + run - 1) % n) + 1
    return (lo, hi)


def merge_delta_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge adjacent/touching (delta_lo, delta_hi) tuples into consolidated ranges."""
    if not ranges:
        return []
    merged = [list(r) for r in sorted(ranges)]
    result = [merged[0]]
    for lo, hi in merged[1:]:
        if lo <= result[-1][1]:
            result[-1][1] = max(result[-1][1], hi)
        else:
            result.append([lo, hi])
    return [(r[0], r[1]) for r in result]


def delta_to_pitch_ranges(prior_pitch: int, delta_lo: int, delta_hi: int):
    """Convert unsigned |Δ| range to circular pitch ranges.

    Returns two (lo, hi) tuples normally.
    Merge cases (second tuple becomes (None, None)):
      delta_lo == 0   -> positive and negative arms meet at prior_pitch; merge to (neg_lo, pos_hi).
      delta_hi == 500 -> positive and negative arms meet at the antipodal point; merge to (pos_lo, neg_hi).
    """
    pos_lo = (prior_pitch + delta_lo - 1) % 1000 + 1
    pos_hi = (prior_pitch + delta_hi - 1) % 1000 + 1
    neg_lo = (prior_pitch - delta_hi - 1) % 1000 + 1
    neg_hi = (prior_pitch - delta_lo - 1) % 1000 + 1
    if delta_lo == 0:
        # neg_hi == pos_lo == prior_pitch; merge into single range.
        return [(neg_lo, pos_hi), (None, None)]
    if delta_hi == 500:
        # pos_hi == neg_lo == antipodal point; merge into single range.
        return [(pos_lo, neg_hi), (None, None)]
    return [(pos_lo, pos_hi), (neg_lo, neg_hi)]


def delta2_to_pitch_ranges(last_pitch: int, last_delta_signed: int, d2_lo: int, d2_hi: int):
    """Compound a predicted |Δ²| bucket into next-pitch circular arcs.

    Mirrors the established Swing Analyzer projection project_from_delta2s
    (utils.py). Δ² moves the MAGNITUDE of the pitcher's same-direction delta
    (acceleration or deceleration); it never flips direction. Given the last pitch, the
    SIGNED last delta L = circular_signed_delta(prev_pitch, last_pitch), and a predicted
    |Δ²| bucket [d2_lo, d2_hi], the next signed delta lands in [L + d2_lo, L + d2_hi]
    (grow) or [L - d2_hi, L - d2_lo] (shrink). Each signed range maps to ONE circular
    pitch arc off last_pitch via ((last_pitch + nd - 1) % 1000) + 1 - identical to
    project_from_delta2s's inner last_val + (last_delta + sign*d2) step, generalized from
    a point to a bucket. The 1-1000 wheel is fully reachable, so there is NO clipping and
    NO unreachable-bucket case (contrast an unsigned |Δ| formulation).

    Returns 1 or 2 (lo, hi) circular pitch arcs (each may wrap). The grow and shrink
    branches meet at last_pitch + L when d2_lo == 0 and are merged into a single arc.
    """
    def _arc(nd_lo: int, nd_hi: int):
        lo = ((last_pitch + nd_lo - 1) % 1000) + 1
        hi = ((last_pitch + nd_hi - 1) % 1000) + 1
        return (lo, hi)

    if d2_lo == 0:
        # Both branches share the endpoint last_pitch + L; merge to [L - d2_hi, L + d2_hi].
        return [_arc(last_delta_signed - d2_hi, last_delta_signed + d2_hi)]
    grow   = _arc(last_delta_signed + d2_lo, last_delta_signed + d2_hi)
    shrink = _arc(last_delta_signed - d2_hi, last_delta_signed - d2_lo)
    return [grow, shrink]


def seq2_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_val: int,
    centered: bool = False,
) -> dict | None:
    """Most likely next value bucket given prior_val. Returns {lo, hi, prob, n} or None."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    df_s["_next"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_next"])
    if df_s.empty:
        return None
    n_bkts = 1000 // bucket_size
    df_s["_nb"] = ((df_s["_next"].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
    if centered:
        half = bucket_size // 2
        mask = _circ_dist_vec(df_s[value_col].astype(int), int(prior_val)) <= half
        col_data = df_s[mask]["_nb"]
    else:
        prior_bkt = min(max(0, (int(prior_val) - 1) // bucket_size), n_bkts - 1)
        df_s["_cb"] = ((df_s[value_col].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
        col_data = df_s[df_s["_cb"] == prior_bkt]["_nb"]
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    return {
        "lo": best_bkt * bucket_size + 1,
        "hi": min((best_bkt + 1) * bucket_size, 1000),
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
    }


def seq3_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_val_1: int,
    prior_val_2: int,
    centered: bool = False,
) -> dict | None:
    """Most likely 3rd value bucket given prior two values. Returns {lo, hi, prob, n} or None."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    df_s["_n1"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s["_n2"] = df_s.groupby(["game_id", group_col])[value_col].shift(-2)
    df_s = df_s.dropna(subset=["_n1", "_n2"])
    if df_s.empty:
        return None
    n_bkts = 1000 // bucket_size
    df_s["_b3"] = ((df_s["_n2"].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
    if centered:
        half = bucket_size // 2
        m1 = _circ_dist_vec(df_s[value_col].astype(int), int(prior_val_1)) <= half
        m2 = _circ_dist_vec(df_s["_n1"].astype(int), int(prior_val_2)) <= half
        col_data = df_s[m1 & m2]["_b3"]
    else:
        b1 = min(max(0, (int(prior_val_1) - 1) // bucket_size), n_bkts - 1)
        b2 = min(max(0, (int(prior_val_2) - 1) // bucket_size), n_bkts - 1)
        df_s["_b1"] = ((df_s[value_col].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
        df_s["_b2"] = ((df_s["_n1"].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
        col_data = df_s[(df_s["_b1"] == b1) & (df_s["_b2"] == b2)]["_b3"]
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    return {
        "lo": best_bkt * bucket_size + 1,
        "hi": min((best_bkt + 1) * bucket_size, 1000),
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
    }


def seq2_delta_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_delta_abs: int,
    centered: bool = False,
) -> dict | None:
    """Most likely next |Δ| bucket given prior |Δ|. Returns {delta_lo, delta_hi, prob, n} or None."""
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    bins = list(range(0, 501, bucket_size))
    n_bkts = 500 // bucket_size
    df_s = df[df[value_col].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s[delta_col] = df_s.groupby(["game_id", group_col], group_keys=False)[value_col].apply(_circ_delta_group)
    df_s = df_s[df_s[delta_col].notna()].copy()
    df_s["_nd"] = df_s.groupby(["game_id", group_col])[delta_col].shift(-1)
    df_s = df_s.dropna(subset=["_nd"])
    if df_s.empty:
        return None
    df_s["_nb"] = pd.cut(df_s["_nd"].abs().astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    if centered:
        d_abs = df_s[delta_col].abs()
        _lo, _hi = _centered_match_interval(int(prior_delta_abs), bucket_size, 500)
        mask = d_abs.between(_lo, _hi)
        col_data = df_s[mask]["_nb"].dropna().astype(int)
    else:
        prior_bkt_idx = min(max(0, (int(prior_delta_abs) - 1) // bucket_size if prior_delta_abs > 0 else 0), n_bkts - 1)
        df_s["_pb"] = pd.cut(df_s[delta_col].abs().astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        col_data = df_s[df_s["_pb"] == prior_bkt_idx]["_nb"].dropna().astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    best_cnt = int(counts.iloc[0])
    tied = [
        (int(bkt) * bucket_size, (int(bkt) + 1) * bucket_size)
        for bkt, cnt in counts.items()
        if int(cnt) == best_cnt and int(bkt) != best_bkt
    ]
    all_counts = [int(counts.get(i, 0)) for i in range(n_bkts)]
    return {
        "delta_lo": best_bkt * bucket_size,
        "delta_hi": (best_bkt + 1) * bucket_size,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "tied_buckets": tied,
        "all_counts": all_counts,
        "delta_bucket_size": bucket_size,
    }


def seq3_delta_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_delta_1: int,
    prior_delta_2: int,
    centered: bool = False,
) -> dict | None:
    """Most likely 3rd |Δ| bucket given prior two |Δ| values. Returns {delta_lo, delta_hi, prob, n} or None."""
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    bins = list(range(0, 501, bucket_size))
    n_bkts = 500 // bucket_size
    df_s = df[df[value_col].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s[delta_col] = df_s.groupby(["game_id", group_col], group_keys=False)[value_col].apply(_circ_delta_group)
    df_s = df_s[df_s[delta_col].notna()].copy()
    df_s["_d1"] = df_s[delta_col].abs()
    df_s["_d2"] = df_s.groupby(["game_id", group_col])[delta_col].shift(-1).abs()
    df_s["_d3"] = df_s.groupby(["game_id", group_col])[delta_col].shift(-2).abs()
    df_s = df_s.dropna(subset=["_d2", "_d3"]).copy()
    if df_s.empty:
        return None
    df_s["_b3"] = pd.cut(df_s["_d3"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    if centered:
        _lo1, _hi1 = _centered_match_interval(int(prior_delta_1), bucket_size, 500)
        _lo2, _hi2 = _centered_match_interval(int(prior_delta_2), bucket_size, 500)
        m1 = df_s["_d1"].between(_lo1, _hi1)
        m2 = df_s["_d2"].between(_lo2, _hi2)
        col_data = df_s[m1 & m2]["_b3"].dropna().astype(int)
    else:
        b1 = min(max(0, (int(prior_delta_1) - 1) // bucket_size if prior_delta_1 > 0 else 0), n_bkts - 1)
        b2 = min(max(0, (int(prior_delta_2) - 1) // bucket_size if prior_delta_2 > 0 else 0), n_bkts - 1)
        df_s["_b1"] = pd.cut(df_s["_d1"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        df_s["_b2"] = pd.cut(df_s["_d2"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        col_data = df_s[(df_s["_b1"] == b1) & (df_s["_b2"] == b2)]["_b3"].dropna().astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    best_cnt = int(counts.iloc[0])
    tied = [
        (int(bkt) * bucket_size, (int(bkt) + 1) * bucket_size)
        for bkt, cnt in counts.items()
        if int(cnt) == best_cnt and int(bkt) != best_bkt
    ]
    all_counts = [int(counts.get(i, 0)) for i in range(n_bkts)]
    return {
        "delta_lo": best_bkt * bucket_size,
        "delta_hi": (best_bkt + 1) * bucket_size,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "tied_buckets": tied,
        "all_counts": all_counts,
        "delta_bucket_size": bucket_size,
    }


def seq2_delta2_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_d2sq_abs: int,
    centered: bool = False,
) -> dict | None:
    """Most likely next |Δ²| bucket given prior |Δ²|. Returns {d2_lo, d2_hi, prob, n, ...} or None.

    Δ² recomputed fresh per game+pitcher (two-stage, see _fresh_delta2_frame). The dict
    uses d2_* keys (NOT delta_*) on purpose, so a Δ² hint accidentally passed into the
    first-order _delta_row_p raises KeyError instead of silently rendering wrong ranges.
    """
    bins = list(range(0, 501, bucket_size))
    n_bkts = 500 // bucket_size
    _fresh = _fresh_delta2_frame(df, value_col)
    if _fresh is None:
        return None
    df_s, group_col = _fresh
    df_s = df_s[df_s["_d2"].notna()].copy()
    df_s["_nd"] = df_s.groupby(["game_id", group_col])["_d2"].shift(-1)
    df_s = df_s.dropna(subset=["_nd"])
    if df_s.empty:
        return None
    df_s["_nb"] = pd.cut(df_s["_nd"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    if centered:
        _lo, _hi = _centered_match_interval(int(prior_d2sq_abs), bucket_size, 500)
        mask = df_s["_d2"].between(_lo, _hi)
        col_data = df_s[mask]["_nb"].dropna().astype(int)
    else:
        prior_bkt_idx = min(max(0, (int(prior_d2sq_abs) - 1) // bucket_size if prior_d2sq_abs > 0 else 0), n_bkts - 1)
        df_s["_pb"] = pd.cut(df_s["_d2"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        col_data = df_s[df_s["_pb"] == prior_bkt_idx]["_nb"].dropna().astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    best_cnt = int(counts.iloc[0])
    tied = [
        (int(bkt) * bucket_size, (int(bkt) + 1) * bucket_size)
        for bkt, cnt in counts.items()
        if int(cnt) == best_cnt and int(bkt) != best_bkt
    ]
    all_counts = [int(counts.get(i, 0)) for i in range(n_bkts)]
    return {
        "d2_lo": best_bkt * bucket_size,
        "d2_hi": (best_bkt + 1) * bucket_size,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "tied_buckets": tied,
        "all_counts": all_counts,
        "d2_bucket_size": bucket_size,
    }


def seq3_delta2_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    prior_d2sq_1: int,
    prior_d2sq_2: int,
    centered: bool = False,
) -> dict | None:
    """Most likely 3rd |Δ²| bucket given prior two |Δ²| values. Returns {d2_lo, d2_hi, ...} or None.

    Δ² recomputed fresh per game+pitcher (two-stage). Uses d2_* keys deliberately (see
    seq2_delta2_hint). Argument order matches seq3_delta_hint: older value first.
    """
    bins = list(range(0, 501, bucket_size))
    n_bkts = 500 // bucket_size
    _fresh = _fresh_delta2_frame(df, value_col)
    if _fresh is None:
        return None
    df_s, group_col = _fresh
    df_s = df_s[df_s["_d2"].notna()].copy()
    df_s["_e1"] = df_s["_d2"]
    df_s["_e2"] = df_s.groupby(["game_id", group_col])["_d2"].shift(-1)
    df_s["_e3"] = df_s.groupby(["game_id", group_col])["_d2"].shift(-2)
    df_s = df_s.dropna(subset=["_e2", "_e3"]).copy()
    if df_s.empty:
        return None
    df_s["_b3"] = pd.cut(df_s["_e3"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    if centered:
        _lo1, _hi1 = _centered_match_interval(int(prior_d2sq_1), bucket_size, 500)
        _lo2, _hi2 = _centered_match_interval(int(prior_d2sq_2), bucket_size, 500)
        m1 = df_s["_e1"].between(_lo1, _hi1)
        m2 = df_s["_e2"].between(_lo2, _hi2)
        col_data = df_s[m1 & m2]["_b3"].dropna().astype(int)
    else:
        b1 = min(max(0, (int(prior_d2sq_1) - 1) // bucket_size if prior_d2sq_1 > 0 else 0), n_bkts - 1)
        b2 = min(max(0, (int(prior_d2sq_2) - 1) // bucket_size if prior_d2sq_2 > 0 else 0), n_bkts - 1)
        df_s["_b1"] = pd.cut(df_s["_e1"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        df_s["_b2"] = pd.cut(df_s["_e2"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
        col_data = df_s[(df_s["_b1"] == b1) & (df_s["_b2"] == b2)]["_b3"].dropna().astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    best_cnt = int(counts.iloc[0])
    tied = [
        (int(bkt) * bucket_size, (int(bkt) + 1) * bucket_size)
        for bkt, cnt in counts.items()
        if int(cnt) == best_cnt and int(bkt) != best_bkt
    ]
    all_counts = [int(counts.get(i, 0)) for i in range(n_bkts)]
    return {
        "d2_lo": best_bkt * bucket_size,
        "d2_hi": (best_bkt + 1) * bucket_size,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "tied_buckets": tied,
        "all_counts": all_counts,
        "d2_bucket_size": bucket_size,
    }


def diff_to_delta_hint(
    df: pd.DataFrame,
    value_col: str,
    prior_diff_abs: int,
    centered: bool = False,
) -> dict | None:
    """Most likely next |Δ| (100-unit fixed bins) given prior |diff|. Returns {delta_lo, delta_hi, prob, n} or None."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna() & df["diff"].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s["_nv"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_nv"])
    if df_s.empty:
        return None
    df_s["_nd"] = df_s.apply(lambda r: circular_diff(int(r[value_col]), int(r["_nv"])), axis=1)
    df_s["_nc"] = pd.cut(df_s["_nd"], bins=_DELTA_HM_BINS, labels=False, right=True, include_lowest=True)
    df_s = df_s.dropna(subset=["_nc"])
    if df_s.empty:
        return None
    if centered:
        half = _diff_centered_half(int(prior_diff_abs))
        d_abs = df_s["diff"].abs()
        mask = ((d_abs - float(prior_diff_abs)).abs() <= half).fillna(False)
        col_data = df_s[mask]["_nc"].astype(int)
    else:
        df_s["_dc"] = pd.cut(df_s["diff"].abs().astype(int), bins=_DIFF_HM_BINS, labels=_DIFF_HM_LABELS,
                             right=True, include_lowest=True)
        df_s = df_s.dropna(subset=["_dc"])
        prior_cat = pd.cut(pd.Series([prior_diff_abs]), bins=_DIFF_HM_BINS, labels=_DIFF_HM_LABELS,
                           right=True, include_lowest=True).iloc[0]
        col_data = df_s[df_s["_dc"] == prior_cat]["_nc"].astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    delta_step = (_DELTA_HM_BINS[1] - _DELTA_HM_BINS[0])
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {
        "delta_lo": best_bkt * delta_step,
        "delta_hi": (best_bkt + 1) * delta_step,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "all_counts": all_counts,
        "delta_bucket_size": delta_step,
    }


def prior_result_delta_hint(df: pd.DataFrame, value_col: str, prior_category: str) -> dict | None:
    """Most likely next |Δ| (fixed 100-unit bins) given the PREVIOUS plate
    appearance's result category (XBH / BB-1B / Out / K+)."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna() & df["result"].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s["_nv"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_nv"])
    if df_s.empty:
        return None
    df_s["_nd"] = df_s.apply(lambda r: circular_diff(int(r[value_col]), int(r["_nv"])), axis=1)
    df_s["_nc"] = pd.cut(df_s["_nd"], bins=_DELTA_HM_BINS, labels=False, right=True, include_lowest=True)
    df_s = df_s.dropna(subset=["_nc"])
    if df_s.empty:
        return None
    df_s["_rescat"] = df_s["result"].map(seq_result_category)
    col_data = df_s[df_s["_rescat"] == prior_category]["_nc"].astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {
        "delta_lo": best_bkt * delta_step,
        "delta_hi": (best_bkt + 1) * delta_step,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "all_counts": all_counts,
        "delta_bucket_size": delta_step,
    }


def _result_seq_context(df: pd.DataFrame, value_col: str) -> "pd.DataFrame | None":
    """Fresh recompute: one row per plate appearance with '_cat' (this row's own
    result category), '_prev_cat' (the immediately preceding SAME-GAME row's
    category), and '_nc' (next-pitch |Δ| bucket, fixed 100-unit bins). Shared by
    result_seq_delta_hint and result_seq_delta_dist so the two views can never
    drift out of sync with each other."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna() & df["result"].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s["_cat"] = df_s["result"].map(seq_result_category)
    df_s["_prev_cat"] = df_s.groupby(["game_id", group_col])["_cat"].shift(1)
    df_s["_nv"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_nv", "_prev_cat"])
    if df_s.empty:
        return None
    df_s["_nd"] = df_s.apply(lambda r: circular_diff(int(r[value_col]), int(r["_nv"])), axis=1)
    df_s["_nc"] = pd.cut(df_s["_nd"], bins=_DELTA_HM_BINS, labels=False, right=True, include_lowest=True)
    df_s = df_s.dropna(subset=["_nc"])
    return df_s if not df_s.empty else None


def result_seq_delta_hint(df: pd.DataFrame, value_col: str,
                          prior_cat_older: str, prior_cat_newer: str) -> dict | None:
    """Most likely next |Δ| (fixed 100-unit bins) given the result categories of
    the two most recent plate appearances (older first, newer/most-recent
    second - same argument order convention as seq3_delta_hint)."""
    df_s = _result_seq_context(df, value_col)
    if df_s is None:
        return None
    col_data = df_s[(df_s["_prev_cat"] == prior_cat_older) &
                    (df_s["_cat"] == prior_cat_newer)]["_nc"].astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {
        "delta_lo": best_bkt * delta_step,
        "delta_hi": (best_bkt + 1) * delta_step,
        "prob": counts.iloc[0] / len(col_data),
        "n": int(len(col_data)),
        "all_counts": all_counts,
        "delta_bucket_size": delta_step,
    }


def context_zone_hint(
    df: pd.DataFrame,
    value_col: str,
    bucket_size: int,
    mask: "pd.Series",
) -> dict | None:
    """Most likely value bucket for rows matching mask. Returns {lo, hi, prob, n} or None."""
    df_s = df[mask & df[value_col].notna()]
    if df_s.empty:
        return None
    n_bkts = 1000 // bucket_size
    bkts = ((df_s[value_col].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
    counts = bkts.value_counts()
    if counts.empty:
        return None
    best_bkt = int(counts.index[0])
    return {
        "lo": best_bkt * bucket_size + 1,
        "hi": min((best_bkt + 1) * bucket_size, 1000),
        "prob": counts.iloc[0] / len(bkts),
        "n": int(len(bkts)),
    }


def best_zone_hint(df: pd.DataFrame, value_col: str) -> dict | None:
    """Return top-zone hint for the highest-count ZONES grid cell (111-unit buckets).

    Handles ties by collecting all tied zones, merging contiguous ones.
    Returns: {lo, hi, lo2, hi2, prob, n, _zone_dist} or None.
    """
    zone_col = f"{value_col}_zone"
    if zone_col not in df.columns:
        return None
    counts = df[zone_col].dropna().value_counts()
    if counts.empty:
        return None
    total = int(counts.sum())
    top_n = int(counts.iloc[0])

    _idx = {z[2]: i for i, z in enumerate(ZONES)}
    tied = sorted((lbl for lbl, c in counts.items() if c == top_n),
                  key=lambda l: _idx.get(l, 99))

    groups: list[list[str]] = []
    cur: list[str] = [tied[0]] if tied else []
    for lbl in tied[1:]:
        if _idx.get(lbl, 99) == _idx.get(cur[-1], -1) + 1:
            cur.append(lbl)
        else:
            groups.append(cur)
            cur = [lbl]
    if cur:
        groups.append(cur)

    def _bounds(grp: list[str]) -> tuple[int, int]:
        zs = [z for z in ZONES if z[2] in grp]
        return min(z[0] for z in zs), max(z[1] for z in zs)

    lo, hi = _bounds(groups[0])
    lo2, hi2 = _bounds(groups[1]) if len(groups) > 1 else (None, None)
    zone_counts = [int(counts.get(z[2], 0)) for z in ZONES]

    return {
        "lo": lo, "hi": hi,
        "lo2": lo2, "hi2": hi2,
        "prob": top_n / total,
        "n": total,
        "_zone_dist": zone_counts,
    }


def seq2_zone_dist(
    df: pd.DataFrame, value_col: str, bucket_size: int, prior_val: int, centered: bool = False,
) -> list[int] | None:
    """Bucket counts of next value when the prior value falls in the same bucket."""
    n_bkts = 1000 // bucket_size
    sw = df[df[value_col].notna()].sort_values(["game_id", "id"])
    if sw.empty:
        return None
    prev = sw[value_col].shift(1)
    same_game = (sw["game_id"] == sw["game_id"].shift(1)).fillna(False)
    if centered:
        half = bucket_size // 2
        mask = ((_circ_dist_vec(prev, int(prior_val)) <= half) & same_game).fillna(False)
    else:
        prior_bkt = min(max(0, (int(prior_val) - 1) // bucket_size), n_bkts - 1)
        bkts = ((sw[value_col].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
        mask = ((bkts.shift(1) == prior_bkt) & same_game).fillna(False)
    pitches = sw.loc[mask, value_col]
    if pitches.empty:
        return None
    bkt_ids = ((pitches.astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
    c = bkt_ids.value_counts()
    return [int(c.get(i, 0)) for i in range(n_bkts)]


def seq3_zone_dist(
    df: pd.DataFrame, value_col: str, bucket_size: int,
    prior_val_1: int, prior_val_2: int, centered: bool = False,
) -> list[int] | None:
    """Bucket counts of 3rd value when the prior two values fall in matching buckets."""
    n_bkts = 1000 // bucket_size
    sw = df[df[value_col].notna()].sort_values(["game_id", "id"])
    if sw.empty:
        return None
    prev1 = sw[value_col].shift(1)
    prev2 = sw[value_col].shift(2)
    same_game = (
        (sw["game_id"] == sw["game_id"].shift(1)) &
        (sw["game_id"] == sw["game_id"].shift(2))
    ).fillna(False)
    if centered:
        half = bucket_size // 2
        mask = (
            (_circ_dist_vec(prev1, int(prior_val_2)) <= half) &
            (_circ_dist_vec(prev2, int(prior_val_1)) <= half) &
            same_game
        ).fillna(False)
    else:
        b1 = min(max(0, (int(prior_val_1) - 1) // bucket_size), n_bkts - 1)
        b2 = min(max(0, (int(prior_val_2) - 1) // bucket_size), n_bkts - 1)
        bkts = ((sw[value_col].astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
        mask = (
            (bkts.shift(1) == b2) &
            (bkts.shift(2) == b1) &
            same_game
        ).fillna(False)
    pitches = sw.loc[mask, value_col]
    if pitches.empty:
        return None
    bkt_ids = ((pitches.astype(int) - 1) // bucket_size).clip(0, n_bkts - 1)
    c = bkt_ids.value_counts()
    return [int(c.get(i, 0)) for i in range(n_bkts)]


def hint_zscore(prob: float, n: int, n_bkts: int) -> float:
    """Laplace-smoothed binomial Z-score: how many std-devs the observed
    proportion exceeds uniform 1/n_bkts.

    Folds in one pseudo-observation per bucket (alpha=1, the same Dirichlet
    smoothing convention _surprisal_walk uses elsewhere in this module)
    before comparing to baseline: p_smoothed = (hits + 1) / (n + n_bkts), with
    the standard error computed against that same effective sample size
    (n + n_bkts). A handful of lucky/unlucky trials can no longer read as an
    extreme z on their own - the n_bkts pseudo-observations dominate the total
    at small n and pull the estimate toward baseline, then fade out (and the
    result converges to the plain Wald z-score) as n grows past them.
    """
    if n <= 0 or n_bkts <= 0:
        return 0.0
    p0 = 1.0 / n_bkts
    hits = round(prob * n)
    n_eff = n + n_bkts
    p_smoothed = (hits + 1) / n_eff
    std = (p0 * (1 - p0) / n_eff) ** 0.5
    return (p_smoothed - p0) / std if std > 0 else 0.0


def delta_next_zone_dist(
    df: pd.DataFrame, value_col: str, bucket_size: int, prior_delta: int,
    centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Bucket counts of the next pitch when the current pitch's circular delta is in the same bucket."""
    delta_col = f"{value_col}_circ_delta"
    if delta_col not in df.columns or df[value_col].isna().all():
        return None
    d = df[delta_col].abs()
    if centered:
        _lo, _hi = _centered_match_interval(int(prior_delta), bucket_size, 500)
        mask = d.between(_lo, _hi)
    else:
        prior_bkt = (int(prior_delta) - 1) // bucket_size
        mask = ((d - 1) // bucket_size == prior_bkt).fillna(False)
    next_val = df[value_col].shift(-1)
    next_pitches = next_val[mask & next_val.notna()]
    if next_pitches.empty:
        return None
    n_bkts = 1000 // zone_bucket_size
    bkt_ids = ((next_pitches.astype(int) - 1) // zone_bucket_size).clip(0, n_bkts - 1)
    c = bkt_ids.value_counts()
    return [int(c.get(i, 0)) for i in range(n_bkts)]


# ── Scouting-recency stoplight ───────────────────────────────────────────────
# Per Swing Suggestions indication, measure whether a player's recent pitches
# follow or defy the tendencies in their inception-to-date book. Each pitch is
# scored ln(k * p_obs): how much likelier than a random (1/k) bucket the one they
# hit was, using their ITD distribution for p_obs and uniform as the zero
# reference. score > 0 = they hit a bucket their book favors (following it),
# < 0 = a disfavored bucket (defying it), 0 = league-random / no tendency. The
# stoplight is the predominant per-pitch class over the window (a vote, not a
# mean, so one extreme pitch can't hijack the read).
# Cutoffs: score >= scouting_min -> scouting (green), <= anti_max -> anti
# (red), else neutral (yellow). A flat-book pitcher has no favored buckets, so
# every score sits near 0 -> neutral. Tunable with the inspector, not inline.
# Calibrated from MLN history (pitchers with 200+ BF) so each zone gets ~1/3
# of all historical pitches; see scripts/calibrate_stoplight_thresholds.py.
SCOUT_PP_THRESHOLDS = {"scouting_min": +0.11, "anti_max": -0.14}
MIN_SCORED = 3  # need this many career eligible events before showing any light
SCOUT_SCORING_VERSION = 2  # bump when the per-pitch score formula changes


def scouting_cache_sig() -> tuple:
    """Cache-key signature for the page's @st.cache_data stoplight loaders. They
    call into this module, so st.cache_data can't see score-formula or threshold
    edits on its own - passing this into their args busts the cache when either
    changes (so tuning is live)."""
    return (SCOUT_SCORING_VERSION,
            SCOUT_PP_THRESHOLDS["scouting_min"], SCOUT_PP_THRESHOLDS["anti_max"])


def _score_from_probs(p: "np.ndarray", observed: int, p0=None) -> float:
    """Baseline-referenced score = ln(p_obs / p0_obs). > 0 means they hit a
    bucket their book favors (likelier than baseline) = following scouting;
    < 0 means a disfavored bucket = defying it; 0 = baseline.

    p0=None is the equal-width case: baseline is a uniform 1/k bucket, so the
    score reduces to ln(k * p_obs) - bit-identical to the original callers
    (_surprisal_walk / _surprisal_walk_detail). Pass an explicit p0 array (a
    per-bucket baseline, e.g. width-proportional shares that need not sum to a
    uniform 1/k) for the OBP backtest, where buckets have unequal width.

    Worked example (p0=None): p = [0.1, 0.2, 0.2, 0.2, 0.3], k = 5. Hitting the
    30% bucket -> ln(5*0.3) = ln(1.5) = +0.405 (scouting). Hitting the 10%
    bucket -> ln(5*0.1) = ln(0.5) = -0.693 (anti). The uniform 20% bucket ->
    ln(1) = 0.
    """
    if p0 is None:
        return float(np.log(len(p) * p[observed]))
    return float(np.log(p[observed] / p0[observed]))


def _surprisal_walk(context_keys, outcome_buckets, k, alpha=1.0):
    """Walk pitches chronologically, scoring each against its context's ITD
    distribution using ONLY prior pitches (point-in-time), then updating counts.

    Add-alpha smoothing (alpha=1) keeps p_obs off 0. A never-seen context is the
    uniform distribution, so its score is exactly 0 - there is no tendency yet to
    follow or defy. Emits one score (or None for undefined context/outcome) per
    input row.
    """
    counts: dict = {}
    out: list = []
    for ctx, ob in zip(context_keys, outcome_buckets):
        if ctx is None or ob is None:
            out.append(None)
            continue
        arr = counts.get(ctx)
        if arr is None:
            arr = np.zeros(k, dtype=float)
            counts[ctx] = arr
        total = float(arr.sum())
        if total == 0.0:
            # Never-seen context is exactly uniform: no tendency yet, score 0.
            out.append(0.0)
        else:
            p = (arr + alpha) / (total + alpha * k)
            out.append(_score_from_probs(p, int(ob)))
        arr[int(ob)] += 1.0
    return out


_STATE_BY_CLASS = {"scouting": "green", "neutral": "yellow", "anti": "red"}


def _classify_pp(score: float) -> str:
    """Bin one per-pitch score into scouting / neutral / anti (green-positive)."""
    if score >= SCOUT_PP_THRESHOLDS["scouting_min"]:
        return "scouting"
    if score <= SCOUT_PP_THRESHOLDS["anti_max"]:
        return "anti"
    return "neutral"


def _predominant_state(votes: dict):
    """Stoplight = the plurality per-pitch class over the window. A directional
    class tied with neutral at the top still wins (scouting+neutral -> green,
    anti+neutral -> red); scouting tied with anti is a conflict -> yellow; neutral
    alone at the top -> yellow. Empty -> None."""
    total = votes["scouting"] + votes["neutral"] + votes["anti"]
    if total == 0:
        return None
    top = max(votes.values())
    winners = {c for c, v in votes.items() if v == top}
    has_s, has_a = "scouting" in winners, "anti" in winners
    if has_s and has_a:
        return "yellow"   # scouting and anti tied at the top -> conflicting signal
    if has_s:
        return "green"    # scouting alone, or tied with neutral
    if has_a:
        return "red"      # anti alone, or tied with neutral
    return "yellow"       # neutral is the sole top


def _aggregate_recency(scores: list, window_n: int, k: int) -> dict:
    """Vote the last window_n eligible (non-None) scores into scouting/neutral/
    anti and take the predominant class. Gated on MIN_SCORED total eligible
    events across the player's whole history. Also returns rel = exp(mean score)
    = the window's geometric-mean observed-bucket probability relative to the 1/k
    baseline (rel = 1 is at baseline; consistent with the score-based vote so the
    number and the light never point opposite ways). avg (mean score) is kept as
    a cross-check."""
    eligible = [s for s in scores if s is not None]
    n_scored = len(eligible)
    _empty = {"scouting": 0, "neutral": 0, "anti": 0}
    if n_scored < MIN_SCORED:
        return {"avg": None, "rel": None, "n_scored": n_scored, "state": None, "votes": _empty}
    window = eligible[-window_n:] if window_n and window_n > 0 else eligible
    votes = {"scouting": 0, "neutral": 0, "anti": 0}
    for s in window:
        votes[_classify_pp(s)] += 1
    avg = float(np.mean(window))
    rel = float(np.exp(avg))  # exp(avg score) = geo-mean(p_obs)/(1/k); 1.0 == baseline
    return {"avg": avg, "rel": rel, "n_scored": n_scored,
            "state": _predominant_state(votes), "votes": votes}


def _int_or_none(x):
    """Cast a scalar (possibly NaN) to int, or None when undefined."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and np.isnan(x):
            return None
    except TypeError:
        return None
    return int(x)


def _recency_frame(df: pd.DataFrame, value_col: str):
    """Chronological one-row-per-pitch frame for a single player, or None."""
    if df is None or df.empty or value_col not in df.columns:
        return None
    sw = df[df[value_col].notna()].sort_values(["game_id", "id"]).reset_index(drop=True)
    return sw if len(sw) else None


def _recency_indications(sw, value_col: str, hz_bkt: int, dd_bkt: int, dd2_bkt: int | None = None) -> dict:
    """Build {signal: (context_keys, outcome_buckets, k)} for one player frame.

    Shared by the aggregate stoplight and the per-pitch inspector so the two can
    never drift. Fixed-bucket conditioning only (ignores the Centered toggle).
    dd2_bkt defaults to dd_bkt when omitted (keeps positional callers working).
    """
    n = len(sw)
    if dd2_bkt is None:
        dd2_bkt = dd_bkt
    hz_n = max(1, 1000 // hz_bkt)
    dd_n = max(1, 500 // dd_bkt)
    dd2_n = max(1, 500 // dd2_bkt)

    vals = sw[value_col].astype(int).to_numpy()
    game = sw["game_id"].to_numpy()
    same1 = np.zeros(n, dtype=bool)
    if n > 1:
        same1[1:] = game[1:] == game[:-1]
    same2 = np.zeros(n, dtype=bool)
    if n > 2:
        same2[2:] = (game[2:] == game[1:-1]) & (game[1:-1] == game[:-2])

    # Zone bucket (hz_bkt-wide) - outcome for the pitch-sequence indications,
    # mirroring seq2_hint / seq3_hint.
    zone_bkt = np.clip((vals - 1) // hz_bkt, 0, hz_n - 1).astype(int)
    # 9-cell ZONES grid (111-unit; final cell 889-1000) - outcome for the
    # context-zone indications, mirroring best_zone_hint's displayed rows.
    zone9 = np.clip((vals - 1) // 111, 0, 8).astype(int)

    # |Delta| into each pitch (per game; NaN at each game's first pitch), then
    # bucketed two ways: variable dd_bkt bins and fixed 100-unit bins.
    delta_signed = np.full(n, np.nan)
    if n > 1:
        raw = vals[1:].astype(float) - vals[:-1].astype(float)
        raw = np.where(raw > 500, raw - 1000, raw)
        raw = np.where(raw < -500, raw + 1000, raw)
        delta_signed[1:] = np.where(same1[1:], raw, np.nan)
    delta_abs = np.abs(delta_signed)
    delta_bkt = pd.cut(pd.Series(delta_abs), bins=list(range(0, 501, dd_bkt)),
                       labels=False, right=True, include_lowest=True).to_numpy()
    delta100 = pd.cut(pd.Series(delta_abs), bins=_DELTA_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    # Same wrapped delta, but WITHOUT the same-game mask - the cross-boundary
    # deltas the between-game / between-inning signals are made of (that mask is
    # exactly what would otherwise hide their only eligible rows).
    delta_signed_any = np.full(n, np.nan)
    if n > 1:
        delta_signed_any[1:] = raw
    delta_any100 = pd.cut(pd.Series(np.abs(delta_signed_any)), bins=_DELTA_HM_BINS,
                          labels=False, right=True, include_lowest=True).to_numpy()

    # |Delta^2| into each pitch: the magnitude of the wrapped signed difference
    # between two consecutive signed deltas (see _wrap_delta2). NaN at each
    # game's first two pitches. The same2 guard is required (not optional): at a
    # game's 2nd pitch delta_signed[i] and delta_signed[i-1] are both non-NaN
    # but belong to different games, so a bare diff would cross the game boundary.
    d2sq_raw = np.full(n, np.nan)
    if n > 2:
        d2sq_raw[2:] = np.where(same2[2:], delta_signed[2:] - delta_signed[1:-1], np.nan)
    d2sq_raw = np.where(d2sq_raw > 500, d2sq_raw - 1000, d2sq_raw)
    d2sq_raw = np.where(d2sq_raw < -500, d2sq_raw + 1000, d2sq_raw)
    d2sq_abs = np.abs(d2sq_raw)
    d2sq_bkt = pd.cut(pd.Series(d2sq_abs), bins=list(range(0, 501, dd2_bkt)),
                      labels=False, right=True, include_lowest=True).to_numpy()

    # Prior |diff| bucket - context for the diff -> Delta indication.
    diff_abs = sw["diff"].abs().to_numpy() if "diff" in sw.columns else np.full(n, np.nan)
    diff_bkt = pd.cut(pd.Series(diff_abs), bins=_DIFF_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    outs = sw["outs"].to_numpy() if "outs" in sw.columns else np.full(n, np.nan)
    obc = sw["obc"].to_numpy() if "obc" in sw.columns else np.array([None] * n, dtype=object)
    fp_app = sw["is_fp_app"].to_numpy() if "is_fp_app" in sw.columns else np.zeros(n, dtype=bool)
    fp_inn = sw["is_fp_inn"].to_numpy() if "is_fp_inn" in sw.columns else np.zeros(n, dtype=bool)

    # Result category (XBH / BB-1B / Out / K+) - context for the prior-result
    # indications.
    rescat = ([seq_result_category(r) if pd.notna(r) else None for r in sw["result"]]
              if "result" in sw.columns else [None] * n)

    zb = [int(z) for z in zone_bkt]
    zb9 = [int(z) for z in zone9]
    db = [_int_or_none(delta_bkt[i]) for i in range(n)]
    d100 = [_int_or_none(delta100[i]) for i in range(n)]
    da100 = [_int_or_none(delta_any100[i]) for i in range(n)]
    d2b = [_int_or_none(d2sq_bkt[i]) for i in range(n)]
    fb = [_int_or_none(diff_bkt[i]) for i in range(n)]

    ind: dict = {}
    # 2-pitch seq: prev zone bucket (same game) -> zone bucket.
    ind["2-pitch seq"] = (
        [zb[i - 1] if (i >= 1 and same1[i]) else None for i in range(n)], zb, hz_n)
    # 3-pitch seq: (zone t-2, t-1), same game -> zone bucket.
    ind["3-pitch seq"] = (
        [(zb[i - 2], zb[i - 1]) if (i >= 2 and same2[i]) else None for i in range(n)], zb, hz_n)
    # 2-Delta seq: prior |Delta| bucket -> |Delta| bucket.
    ind["2-Δ seq"] = (
        [db[i - 1] if i >= 1 else None for i in range(n)], db, dd_n)
    # 3-Delta seq: (|Delta| t-2, t-1) -> |Delta| bucket.
    ind["3-Δ seq"] = (
        [(db[i - 2], db[i - 1]) if (i >= 2 and db[i - 2] is not None and db[i - 1] is not None) else None
         for i in range(n)], db, dd_n)
    # 2-Delta^2 seq: prior |Delta^2| bucket -> |Delta^2| bucket.
    ind["2-Δ² seq"] = (
        [d2b[i - 1] if i >= 1 else None for i in range(n)], d2b, dd2_n)
    # 3-Delta^2 seq: (|Delta^2| t-2, t-1) -> |Delta^2| bucket.
    ind["3-Δ² seq"] = (
        [(d2b[i - 2], d2b[i - 1]) if (i >= 2 and d2b[i - 2] is not None and d2b[i - 1] is not None) else None
         for i in range(n)], d2b, dd2_n)
    # Prior diff -> Delta: prior |diff| bucket -> |Delta| in fixed 100-unit bins.
    ind["Prior diff → Δ"] = (
        [fb[i - 1] if i >= 1 else None for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
    # Prior result -> Delta: previous plate appearance's result category ->
    # |Delta| in fixed 100-unit bins. Context is un-gated, matching
    # "Prior diff → Δ" - the outcome (d100) is already NaN across a game
    # boundary via the same1 mask upstream.
    ind["Prior result → Δ"] = (
        [rescat[i - 1] if i >= 1 else None for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
    # Result seq -> Delta: the two preceding result categories -> |Delta|.
    # same2 is required here: a two-event momentum read ("XBH then XBH") should
    # reflect an actual in-game sequence, not two results that merely happen to
    # be adjacent in career order.
    ind["Result seq → Δ"] = (
        [(rescat[i - 2], rescat[i - 1])
         if (i >= 2 and same2[i] and rescat[i - 2] is not None and rescat[i - 1] is not None)
         else None for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
    # Between-Game Delta: is this row the first pitch of a new game for this
    # pitcher? -> |Delta| into it, unmasked by the same-game guard.
    ind["Between-Game Δ"] = (
        ["bg" if bool(fp_app[i]) else None for i in range(n)], da100, len(_DELTA_HM_BINS) - 1)
    # Between-Inning Delta: first pitch of a new inning, but NOT also the first
    # pitch of a new game (that case belongs to Between-Game Δ instead - mirrors
    # between_inning_deltas' own game-scoped groupby, which never sees a game's
    # first inning at all).
    ind["Between-Inning Δ"] = (
        ["bi" if (bool(fp_inn[i]) and not bool(fp_app[i])) else None for i in range(n)],
        da100, len(_DELTA_HM_BINS) - 1)
    # The context-zone indications below use the 9-cell ZONES grid (zb9) as the
    # outcome, matching their displayed best_zone_hint rows.
    # Outs: outs value -> zone9 (no sequence, so no game guard).
    ind["Outs"] = (
        [_int_or_none(outs[i]) for i in range(n)], zb9, 9)
    # Base state: empty vs runners-on -> zone9.
    ind["Base state"] = (
        [None if (obc[i] is None or (isinstance(obc[i], float) and np.isnan(obc[i])))
         else ("empty" if str(obc[i]) == "000" else "runners") for i in range(n)], zb9, 9)
    # First pitch of appearance / inning: constant context over eligible rows.
    ind["1st pitch appearance"] = (
        ["fpa" if bool(fp_app[i]) else None for i in range(n)], zb9, 9)
    ind["1st pitch inning"] = (
        ["fpi" if bool(fp_inn[i]) else None for i in range(n)], zb9, 9)
    return ind


def scouting_recency_states(
    df: pd.DataFrame, value_col: str, window_n: int, hz_bkt: int, dd_bkt: int,
    dd2_bkt: int | None = None,
) -> dict:
    """Per-indication scouting-recency stoplight for one player.

    Wired for the pitcher side first; kept value_col-agnostic (pitch vs swing)
    so the batter side is a follow-up, not a rewrite. Returns
    {signal: {"avg": float|None, "n_scored": int, "state": "red"|"yellow"|"green"|None}}
    for the fifteen covered indications. Uses fixed-bucket conditioning only - it
    deliberately ignores the page's Centered toggle, measuring general
    predictability rather than the exact displayed tooltip. dd2_bkt defaults to dd_bkt.
    """
    sw = _recency_frame(df, value_col)
    if sw is None:
        return {}
    ind = _recency_indications(sw, value_col, hz_bkt, dd_bkt, dd2_bkt)
    return {sig: _aggregate_recency(_surprisal_walk(ctx, out, k), window_n, k)
            for sig, (ctx, out, k) in ind.items()}


def _surprisal_walk_detail(context_keys, outcome_buckets, k, alpha=1.0):
    """Like _surprisal_walk but emits the per-pitch calc (or None) for the
    inspector: {ctx, obs, p_obs, H, s, score}. Same point-in-time discipline."""
    counts: dict = {}
    out: list = []
    for ctx, ob in zip(context_keys, outcome_buckets):
        if ctx is None or ob is None:
            out.append(None)
            continue
        arr = counts.get(ctx)
        if arr is None:
            arr = np.zeros(k, dtype=float)
            counts[ctx] = arr
        total = float(arr.sum())
        if total == 0.0:
            p = np.full(k, 1.0 / k)
            score = 0.0
        else:
            p = (arr + alpha) / (total + alpha * k)
            score = _score_from_probs(p, int(ob))
        out.append({
            "ctx": ctx, "obs": int(ob),
            "p_obs": float(p[int(ob)]),
            "H": float(-np.sum(p * np.log(p))),
            "s": float(-np.log(p[int(ob)])),
            "score": score,
        })
        arr[int(ob)] += 1.0
    return out


def _recency_labelers(signal: str, hz_bkt: int, dd_bkt: int, dd2_bkt: int | None = None):
    """Return (context_fmt, outcome_fmt): functions turning a signal's raw bucket
    indices into human-readable ranges/values for the inspector table.
    dd2_bkt defaults to dd_bkt when omitted."""
    if dd2_bkt is None:
        dd2_bkt = dd_bkt

    def zone(b):
        b = int(b)
        return f"{b * hz_bkt + 1}-{min((b + 1) * hz_bkt, 1000)}"

    def zone9(b):
        return ZONES[int(b)][2]

    def delta(b):
        b = int(b)
        return f"{b * dd_bkt}-{(b + 1) * dd_bkt}"

    def delta2sq(b):
        b = int(b)
        return f"{b * dd2_bkt}-{(b + 1) * dd2_bkt}"

    def delta100(b):
        b = int(b)
        return f"{b * 100}-{(b + 1) * 100}"

    def diff(b):
        return _DIFF_HM_LABELS[int(b)]

    def pair(fmt):
        return lambda c: f"{fmt(c[0])} → {fmt(c[1])}"

    def outs(c):
        c = int(c)
        return f"{c} out" if c == 1 else f"{c} outs"

    def base(c):
        return "Empty" if c == "empty" else "Runners on"

    table = {
        "2-pitch seq":          (zone, zone),
        "3-pitch seq":          (pair(zone), zone),
        "2-Δ seq":              (delta, delta),
        "3-Δ seq":              (pair(delta), delta),
        "2-Δ² seq":             (delta2sq, delta2sq),
        "3-Δ² seq":             (pair(delta2sq), delta2sq),
        "Prior diff → Δ":       (diff, delta100),
        "Prior result → Δ":     (lambda c: c, delta100),
        "Result seq → Δ":       (pair(lambda c: c), delta100),
        "Between-Game Δ":       (lambda c: "New game", delta100),
        "Between-Inning Δ":     (lambda c: "New inning", delta100),
        "Outs":                 (outs, zone9),
        "Base state":           (base, zone9),
        "1st pitch appearance": (lambda c: "1st of PA", zone9),
        "1st pitch inning":     (lambda c: "1st of inning", zone9),
    }
    return table.get(signal, (str, str))


def scouting_recency_detail(
    df: pd.DataFrame, value_col: str, signal: str, window_n: int, hz_bkt: int, dd_bkt: int,
    dd2_bkt: int | None = None,
) -> dict:
    """Per-pitch surprisal trace for one indication (inspector view).

    Returns {rows, scores, n_scored, avg, state, window_n, k} where each row
    carries game_id/id plus the {ctx, obs, p_obs, H, s, score, in_window} calc.
    dd2_bkt defaults to dd_bkt.
    """
    empty = {"rows": [], "scores": [], "n_scored": 0, "avg": None,
             "state": None, "window_n": window_n, "k": 0}
    sw = _recency_frame(df, value_col)
    if sw is None:
        return empty
    ind = _recency_indications(sw, value_col, hz_bkt, dd_bkt, dd2_bkt)
    if signal not in ind:
        return empty
    ctx, out, k = ind[signal]
    detail = _surprisal_walk_detail(ctx, out, k)
    gcode = (sw["game_code"] if "game_code" in sw.columns else sw["game_id"]).tolist()
    pv = sw[value_col].astype(int).tolist()
    sv = sw["swing"].tolist() if "swing" in sw.columns else [None] * len(sw)
    dv = sw["diff"].tolist() if "diff" in sw.columns else [None] * len(sw)
    ctx_fmt, obs_fmt = _recency_labelers(signal, hz_bkt, dd_bkt, dd2_bkt)

    def _lbl(fmt, v):
        try:
            return fmt(v)
        except Exception:
            return str(v)

    def _int_or_na(v):
        return int(v) if pd.notna(v) else None

    rows = []
    for i, d in enumerate(detail):
        if d is None:
            continue
        rows.append({"game": gcode[i], "pitch_val": pv[i],
                     "swing_val": _int_or_na(sv[i]), "diff_val": _int_or_na(dv[i]),
                     "ctx_label": _lbl(ctx_fmt, d["ctx"]),
                     "obs_label": _lbl(obs_fmt, d["obs"]), **d})
    scores = [r["score"] for r in rows]
    n_scored = len(scores)
    lo = max(0, n_scored - window_n) if window_n and window_n > 0 else 0
    for j, r in enumerate(rows):
        r["in_window"] = j >= lo
        r["cls"] = _classify_pp(r["score"])
    agg = _aggregate_recency(scores, window_n, k)
    return {"rows": rows, "scores": scores, "n_scored": n_scored,
            "avg": agg["avg"], "rel": agg["rel"], "state": agg["state"],
            "votes": agg["votes"], "window_n": window_n, "k": k}


# ── OBP-recency stoplight (pitcher side) ─────────────────────────────────────
# A categorical bucket-prediction backtest for the three "OBP recent X"
# Suggestions rows. At each historical pitch we recompute that step's own
# recommended value (best_val) from the same FFT-convolved score curve
# obp_zone_signal uses, partition the domain into a fixed slider-width
# recommended bucket plus equal-width neighbors (obp_pitch_partition on the
# circular value domain, obp_bounded_partition on the 0..500 delta / delta2
# domains), then score which bucket the pitcher's REAL outcome landed in against
# his point-in-time outcome history: score = ln(p_pred / p0), p0 = the bucket's
# integer-count share (geometry only, never learned). Because every bucket is a
# fixed width, the Pitch signal reduces to plain equal-width categorical scoring
# (p0 = 1/k) and the chart's 1/k baseline is exact. The stoplight vote/threshold
# machinery (_aggregate_recency, SCOUT_PP_THRESHOLDS) is shared with the sequence
# signals so the page wiring is untouched.

_OBP_SIGNAL_KINDS = {
    "OBP recent pitch": "pitch",
    "OBP recent Δ": "delta",
    "OBP recent Δ²": "delta2",
}


def obp_pitch_partition(best_val: int, width: int) -> "list[tuple]":
    """Partition the 1000-value circle into k = 1000 // width equal-width buckets:
    the recommended arc centered on best_val (label 0) and the rest tiled ascending
    out+1 .. out+(k-1). Returns [(lo, hi, label), ...] with 1-indexed inclusive
    absolute endpoints; a bucket crossing the 1/1000 seam has lo > hi. For even
    widths the center sits left-of-center by one (start = best_val - width // 2).
    Every bucket is exactly width wide, so p0 = width / 1000 = 1 / k for all of them
    - identical to the existing equal-width categorical mechanism."""
    k = 1000 // width
    rec_start = ((best_val - width // 2 - 1) % 1000) + 1  # 1-indexed absolute start
    part = []
    for i in range(k):
        s = (rec_start - 1 + i * width) % 1000
        lo = s + 1
        hi = ((s + width - 1) % 1000) + 1
        part.append((lo, hi, i))
    return part


def obp_bounded_partition(center: int, width: int, domain: int = 500) -> "list[tuple]":
    """Partition the integer domain [0, domain] into width-wide buckets: the
    recommended bucket centered on center (label 0, widened-and-shifted via
    _shift_to_domain to hug an edge rather than truncate) plus FULL buckets tiled
    outward (out-1, out-2, ... below; out+1, out+2, ... above) with at most one
    leftover bucket per side. Returns [(lo, hi, label), ...] sorted by lo with
    inclusive integer endpoints. Convention: buckets are half-open [lo, hi) except
    the single topmost bucket (the one reaching domain), which is closed [lo, domain]
    and carries the extra integer - so integer counts sum to domain + 1 and every
    domain integer maps to exactly one bucket. p0[i] = count_i / (domain + 1); a
    standard bucket's p0 is width / (domain + 1). At most two buckets (the outermost
    leftover on each side) are non-standard, never an interior one; the recommended
    bucket is non-standard only when it itself hugs domain (nothing above it)."""
    rec_lo, rec_hi = _shift_to_domain(center - width // 2,
                                      center - width // 2 + width, 0, domain)
    part = [(rec_lo, rec_hi - 1, 0)]  # inclusive; the top-fold is applied last

    # High side: tile [rec_hi, domain) upward in full width steps, one leftover.
    R_hi = domain - rec_hi
    n_full_hi = R_hi // width
    for j in range(1, n_full_hi + 1):
        lo = rec_hi + (j - 1) * width
        part.append((lo, lo + width - 1, j))
    rem_hi = R_hi - n_full_hi * width
    if rem_hi > 0:
        lo = rec_hi + n_full_hi * width
        part.append((lo, lo + rem_hi - 1, n_full_hi + 1))

    # Low side: tile [0, rec_lo) downward in full width steps, one leftover.
    R_lo = rec_lo
    n_full_lo = R_lo // width
    for j in range(1, n_full_lo + 1):
        hi = rec_lo - (j - 1) * width - 1
        part.append((hi - width + 1, hi, -j))
    rem_lo = R_lo - n_full_lo * width
    if rem_lo > 0:
        part.append((0, rem_lo - 1, -(n_full_lo + 1)))

    part.sort(key=lambda b: b[0])
    lo, hi, lab = part[-1]  # fold the domain endpoint into the single topmost bucket
    part[-1] = (lo, domain, lab)
    return part


def _obp_weight_factors(sw, n_window: int, rel_params) -> dict:
    """Precompute per-pitch quantities for the point-in-time relevance weights,
    once per pitcher/signal-independent - the vectorized equivalent of calling
    compute_pa_weights per historical step. See _obp_window_weights for the
    per-step assembly. rel_params = (recency_slider, result_slider, state_slider,
    g1, g2, g3, result_offset)."""
    (recency_slider, result_slider, state_slider, g1, g2, g3, result_offset) = rel_params
    T = len(sw)
    tr = (recency_slider - 50) / 50.0
    ts = (result_slider - 50) / 50.0
    te = state_slider / 100.0
    g_total = max(g1 + g2 + g3, 1e-9)
    gn = (g1 / g_total, g2 / g_total, g3 / g_total)

    # Recency weight: pure function of window LENGTH (constant with strict full
    # windows), so one vector serves every step - mirrors compute_pa_weights.
    pos = np.linspace(0, 1, n_window) if n_window > 1 else np.array([0.5])
    recency_vec = np.exp(tr * (2 * pos - 1) * 1.151)

    # Result weight: per-pitch batting quality -> weight. The result_offset
    # variant is the quality of the PREVIOUS pitch; the window's first element is
    # forced neutral (q=0.5 -> weight 1.0) in _obp_window_weights, NOT the global
    # predecessor - matching compute_pa_weights' src_i = i-1, i==0 branch.
    if "result" in sw.columns:
        q = sw["result"].map(
            lambda r: _BATTING_QUALITY.get(str(r) if pd.notna(r) else "", 0.5)
        ).to_numpy(dtype=float)
    else:
        q = np.full(T, 0.5)
    rw_no_off = np.exp(ts * (2 * q - 1) * 1.151)
    rw_off_full = np.empty(T)
    rw_off_full[0] = 1.0  # exp(ts*(2*0.5-1)*1.151) with q=0.5 = exp(0) = 1.0
    if T > 1:
        rw_off_full[1:] = rw_no_off[:-1]

    # State weight: 8 obc x 3 outs = 24 states; a 24x24 similarity table gathered
    # per step. NaN defaults match compute_pa_weights (obc "000", outs 0).
    has_state = ("obc" in sw.columns and "outs" in sw.columns)
    if has_state:
        def _obc_idx(s):
            s = str(s).zfill(3)
            return int(s, 2) if len(s) == 3 and set(s) <= {"0", "1"} else 0
        obc_ser = sw["obc"]
        obc_idx = obc_ser.where(obc_ser.notna(), "000").map(_obc_idx).to_numpy()
        outs_int = pd.to_numeric(sw["outs"], errors="coerce").fillna(0).astype(int).clip(0, 2).to_numpy()
        code = (obc_idx * 3 + outs_int).astype(int)
        SIM = np.zeros((24, 24))
        for a in range(24):
            oa, ta = divmod(a, 3)
            bits_a = ((oa >> 2) & 1, (oa >> 1) & 1, oa & 1)
            for b in range(24):
                ob_, tb = divmod(b, 3)
                bits_b = ((ob_ >> 2) & 1, (ob_ >> 1) & 1, ob_ & 1)
                obc_sim = sum(x == y for x, y in zip(bits_a, bits_b)) / 3.0
                outs_sim = 1.0 - abs(ta - tb) / 2.0
                SIM[a, b] = 0.5 * obc_sim + 0.5 * outs_sim
    else:
        code = np.zeros(T, dtype=int)
        SIM = np.full((24, 24), 0.5)

    return {"n_window": n_window, "gn": gn, "te": te,
            "recency_vec": recency_vec, "rw_no_off": rw_no_off,
            "rw_off_full": rw_off_full, "result_offset": bool(result_offset),
            "has_state": has_state, "code": code, "SIM": SIM}


def _obp_window_weights(factors: dict, t: int) -> "np.ndarray":
    """Assemble the (n_window,) relevance weight vector for the window ending
    just before step t (rows [t-n_window : t]), from precomputed factors. The
    point-in-time 'current' state is that of the pitch at t (known before it is
    thrown). Mirrors compute_pa_weights' combine + normalize exactly."""
    n = factors["n_window"]
    lo = t - n
    rec = factors["recency_vec"]
    if factors["result_offset"]:
        res = factors["rw_off_full"][lo:t].copy()
        res[0] = 1.0
    else:
        res = factors["rw_no_off"][lo:t]
    if factors["has_state"]:
        c_t = int(factors["code"][t])
        sims = factors["SIM"][factors["code"][lo:t], c_t]
        sta = np.exp(factors["te"] * (2 * sims - 1) * 1.151)
    else:
        sta = np.ones(n)

    def _norm01(w):
        wmin, wmax = w.min(), w.max()
        if wmax > wmin:
            return (w - wmin) / (wmax - wmin)
        return np.full_like(w, 0.5)

    gn1, gn2, gn3 = factors["gn"]
    combined = gn1 * _norm01(rec) + gn2 * _norm01(res) + gn3 * _norm01(sta)
    mean_c = combined.mean()
    if mean_c > 0:
        combined = combined / mean_c
    return combined


def _pa_weights_point_in_time(sw, t: int, n_window: int, rel_params) -> "np.ndarray":
    """Thin wrapper (precompute + one step) so the Stage 8 test can assert per-step
    equality against a real compute_pa_weights call. The walk uses the precompute
    once and _obp_window_weights per step instead."""
    return _obp_window_weights(_obp_weight_factors(sw, n_window, rel_params), t)


def obp_recency_walk(sw, value_col: str, kind: str, n_window: int, ranges,
                     rel_params, bucket_width: int, alpha: float = 1.0,
                     maximize: bool = True) -> list:
    """Point-in-time bucket-prediction backtest for one OBP signal. Emits one entry
    per row of sw - None for ineligible steps, else a dict with keys best_val,
    implied, rec_lo, rec_hi, bucket_w, k, obs, arc_lo, arc_hi, p_obs, p0_obs, score.

    kind in {"pitch", "delta", "delta2"} selects the population the recommendation
    is built from (raw window / delta-projection / delta2-projection), mirroring the
    live rows including crossing game boundaries inside the window. bucket_width is
    the Decision-1 slider width for this kind (divides 1000 for pitch, 500 otherwise).

    At each eligible step t (strict full window, rows [t-n_window : t]) the score
    curve is rebuilt FRESH from that step's own relevance-weighted window using
    today's live ranges/kernel, the recommended value best_val is picked from the
    argmax plateau by the Decision-4 nearest-reference tie rule, the domain is
    partitioned into a fixed slider-width recommended bucket plus equal-width
    neighbors, and the pitcher's REAL outcome (value / |delta| / |delta2|) is scored
    ln(p_pred/p0) against a point-in-time histogram of prior real outcomes. Relevance
    weights shape the recommendation (the question), not the evidentiary weight of
    what was thrown, so the histogram is updated unweighted - one real trial per
    outcome, after scoring."""
    T = 0 if sw is None else len(sw)
    empty = [None] * T
    if T == 0 or value_col not in sw.columns:
        return empty
    obr_max = int(max((hi for result, _lo, hi in ranges if result in _OBR), default=0))
    if obr_max <= 0 or obr_max >= 1000:
        return empty
    min_len = {"pitch": 1, "delta": 2, "delta2": 3}.get(kind)
    if min_len is None or n_window < min_len or T <= n_window or bucket_width <= 0:
        return empty

    vals = sw[value_col].astype(int).to_numpy()
    game = sw["game_id"].to_numpy()

    # Real per-pitch deltas, recomputed inline (mirrors _recency_indications'
    # discipline) so the tie-break references and scored outcomes never trust a
    # precomputed column. same1/same2 guard game boundaries inside the window.
    same1 = np.zeros(T, dtype=bool)
    if T > 1:
        same1[1:] = game[1:] == game[:-1]
    same2 = np.zeros(T, dtype=bool)
    if T > 2:
        same2[2:] = (game[2:] == game[1:-1]) & (game[1:-1] == game[:-2])
    sd = np.full(T, np.nan)  # signed real delta into each pitch
    if T > 1:
        raw = vals[1:].astype(float) - vals[:-1].astype(float)
        raw = np.where(raw > 500, raw - 1000, raw)
        raw = np.where(raw < -500, raw + 1000, raw)
        sd[1:] = np.where(same1[1:], raw, np.nan)
    abs_d = np.abs(sd)  # real |delta| into each pitch
    d2 = np.full(T, np.nan)  # real |delta2| into each pitch: magnitude of the
    # wrapped signed difference between two consecutive signed deltas.
    if T > 2:
        d2raw = np.where(same2[2:], sd[2:] - sd[1:-1], np.nan)
        d2raw = np.where(d2raw > 500, d2raw - 1000, d2raw)
        d2raw = np.where(d2raw < -500, d2raw + 1000, d2raw)
        d2[2:] = np.abs(d2raw)

    # Kernel from today's ranges (the same box _scores_via_fft uses). The v1 need
    # for a bit-identical transform to match np.argmax's lowest-index tie pick is
    # gone: Decision 4 picks best_val from the whole plateau by an explicit nearest
    # rule, which absorbs epsilon-level fft differences by construction.
    diff_arr = _diff_score_array(ranges, "obp")
    kernel = np.array([diff_arr[min(d, 1000 - d)] for d in range(1000)])
    kfft = np.fft.fft(kernel)

    factors = _obp_weight_factors(sw, n_window, rel_params)
    elig = np.arange(n_window, T)
    n_elig = len(elig)

    # One relevance-weighted weight array per eligible step (same population variants
    # and _build_weight_array the live path uses), transformed in a single batched
    # fft - per-row independent, so identical regardless of the stack size.
    Wmat = np.zeros((n_elig, 1000))
    for r, t in enumerate(elig):
        w_win = vals[t - n_window:t]
        wts = _obp_window_weights(factors, t)
        if kind == "pitch":
            pop = w_win.tolist()
            wv = wts.tolist()
        elif kind == "delta":
            pop = project_from_deltas(w_win.tolist())
            wv = wts[1:].tolist()
        else:  # delta2 - project_from_delta2s emits 2 per delta2; weights x2 to match
            pop = project_from_delta2s(w_win.tolist())
            wv = np.repeat(wts[2:], 2).tolist()
        if not pop or len(wv) != len(pop):
            continue
        Wmat[r] = _build_weight_array(pop, wv)
    S = np.real(np.fft.ifft(np.fft.fft(Wmat, axis=1) * kfft[None, :], axis=1))

    # One point-in-time histogram of real outcomes over the ABSOLUTE domain (pitch:
    # values 1..1000 at index 1..1000; delta/delta2: 0..500). Re-bucketed per step.
    domain = 1000 if kind == "pitch" else 500
    hist = np.zeros(domain + 1)
    total = 0.0
    out = list(empty)
    for r, t in enumerate(elig):
        Srow = S[r]
        peak = Srow.max() if maximize else Srow.min()
        tie = (np.flatnonzero(Srow >= peak - 1e-9) if maximize
               else np.flatnonzero(Srow <= peak + 1e-9))
        cand = tie + 1  # candidate best_val values (1..1000); tie is ascending
        prev = int(vals[t - 1])

        # Decision-4 tie-break: nearest candidate to the kind's reference; when the
        # reference is undefined (game boundary) fall back to the lowest index.
        if kind == "pitch":
            dd = np.abs(cand - prev)
            metric = np.minimum(dd, 1000 - dd)  # circular distance to vals[t-1]
            best_val = int(cand[int(np.argmin(metric))])
        elif kind == "delta":
            if same1[t - 1]:
                cd = cand.astype(float) - prev
                cd = np.where(cd > 500, cd - 1000, np.where(cd < -500, cd + 1000, cd))
                metric = np.abs(cd - sd[t - 1])
                best_val = int(cand[int(np.argmin(metric))])
            else:
                best_val = int(cand[0])
        else:  # delta2
            if same2[t - 1] and not np.isnan(d2[t - 1]):
                cd = cand.astype(float) - prev
                cd = np.where(cd > 500, cd - 1000, np.where(cd < -500, cd + 1000, cd))
                cd2 = cd - sd[t - 1]
                cd2 = np.where(cd2 > 500, cd2 - 1000, np.where(cd2 < -500, cd2 + 1000, cd2))
                implied_d2c = np.abs(cd2)
                metric = np.abs(implied_d2c - d2[t - 1])
                best_val = int(cand[int(np.argmin(metric))])
            else:
                best_val = int(cand[0])

        # Partition + the real outcome tested against it.
        if kind == "pitch":
            part = obp_pitch_partition(best_val, bucket_width)
            outcome = int(vals[t])
            implied = None
        elif kind == "delta":
            if not same1[t]:
                continue
            implied = int(abs(circular_signed_delta(prev, best_val)))
            part = obp_bounded_partition(implied, bucket_width, 500)
            outcome = int(abs_d[t])
        else:  # delta2
            if not same2[t]:
                continue
            implied_delta = circular_signed_delta(prev, best_val)
            implied = int(abs(circular_signed_delta(int(sd[t - 1]), int(implied_delta))))
            part = obp_bounded_partition(implied, bucket_width, 500)
            outcome = int(d2[t])

        k_t = len(part)
        pref = np.concatenate(([0.0], np.cumsum(hist)))  # pref[m] = sum hist[0:m]
        D = np.empty(k_t)
        counts = np.empty(k_t)
        obs_i = 0
        for i, (lo, hi, _lab) in enumerate(part):
            if kind == "pitch" and lo > hi:  # seam-wrapping arc
                D[i] = (pref[domain + 1] - pref[lo]) + (pref[hi + 1] - pref[0])
                counts[i] = domain - lo + 1 + hi
                inside = (outcome >= lo) or (outcome <= hi)
            else:
                D[i] = pref[hi + 1] - pref[lo]
                counts[i] = hi - lo + 1
                inside = lo <= outcome <= hi
            if inside:
                obs_i = i
        p0 = counts / float(domain if kind == "pitch" else domain + 1)
        p_pred = (D + alpha * k_t * p0) / (total + alpha * k_t)

        lo_o, hi_o, lab_o = part[obs_i]
        score = _score_from_probs(p_pred, obs_i, p0)
        rec_lo, rec_hi, _ = next(b for b in part if b[2] == 0)
        out[t] = {"best_val": best_val, "implied": implied,
                  "rec_lo": int(rec_lo), "rec_hi": int(rec_hi),
                  "bucket_w": int(counts[obs_i]), "k": k_t, "obs": int(lab_o),
                  "arc_lo": int(lo_o), "arc_hi": int(hi_o),
                  "p_obs": float(p_pred[obs_i]), "p0_obs": float(p0[obs_i]),
                  "score": float(score)}
        hist[outcome] += 1.0  # real outcome into the histogram AFTER scoring
        total += 1.0
    return out


def obp_recency_states(df: "pd.DataFrame", value_col: str, window_n: int,
                       ranges, rel_params, bucket_widths: dict) -> dict:
    """Per-signal OBP stoplight for one player. Same return shape as
    scouting_recency_states so the page merges the two with dict.update.
    bucket_widths maps 'pitch'/'delta'/'delta2' -> the Decision-1 slider width."""
    sw = _recency_frame(df, value_col)
    if sw is None:
        return {}
    out = {}
    for sig, kind in _OBP_SIGNAL_KINDS.items():
        walk = obp_recency_walk(sw, value_col, kind, window_n, ranges, rel_params,
                                bucket_widths[kind])
        scores = [r["score"] if r else None for r in walk]
        out[sig] = _aggregate_recency(scores, window_n, 1)  # k unused by _aggregate_recency
    return out


def obp_recency_detail(df: "pd.DataFrame", value_col: str, signal: str,
                       window_n: int, ranges, rel_params, bucket_widths: dict) -> dict:
    """Per-pitch OBP backtest trace for one signal (inspector view). Same return
    shape as scouting_recency_detail, with each row additionally carrying p0 and
    bucket_w (Decision-9 disclosure). Top-level k is the CONSTANT standard bucket
    count (domain // width), so the chart's 1/k baseline is the standard-width
    baseline; per-step edge buckets disclose their true width/base per row."""
    empty = {"rows": [], "scores": [], "n_scored": 0, "avg": None, "rel": None,
             "state": None, "votes": {"scouting": 0, "neutral": 0, "anti": 0},
             "window_n": window_n, "k": 0}
    sw = _recency_frame(df, value_col)
    if sw is None or signal not in _OBP_SIGNAL_KINDS:
        return empty
    kind = _OBP_SIGNAL_KINDS[signal]
    bw = bucket_widths[kind]
    walk = obp_recency_walk(sw, value_col, kind, window_n, ranges, rel_params, bw)
    std_k = (1000 // bw) if kind == "pitch" else (500 // bw)
    gcode = (sw["game_code"] if "game_code" in sw.columns else sw["game_id"]).tolist()
    pv = sw[value_col].astype(int).tolist()
    sv = sw["swing"].tolist() if "swing" in sw.columns else [None] * len(sw)
    dv = sw["diff"].tolist() if "diff" in sw.columns else [None] * len(sw)

    def _int_or_na(v):
        return int(v) if pd.notna(v) else None

    rows = []
    for i, d in enumerate(walk):
        if d is None:
            continue
        if kind == "pitch":
            ctx_label = f"rec {d['rec_lo']}-{d['rec_hi']}"
        else:
            ctx_label = f"implied {d['implied']} -> rec {d['rec_lo']}-{d['rec_hi']}"
        obs_label = ("in rec" if d["obs"] == 0
                     else f"out{d['obs']:+d} ({d['arc_lo']}-{d['arc_hi']})")
        rows.append({"game": gcode[i], "pitch_val": pv[i],
                     "swing_val": _int_or_na(sv[i]), "diff_val": _int_or_na(dv[i]),
                     "ctx_label": ctx_label, "obs_label": obs_label, "obs": d["obs"],
                     "p_obs": d["p_obs"], "p0": d["p0_obs"], "bucket_w": d["bucket_w"],
                     "arc_lo": d["arc_lo"], "arc_hi": d["arc_hi"],
                     "score": d["score"]})
    scores = [r["score"] for r in rows]
    n_scored = len(scores)
    lo = max(0, n_scored - window_n) if window_n and window_n > 0 else 0
    for j, r in enumerate(rows):
        r["in_window"] = j >= lo
        r["cls"] = _classify_pp(r["score"])
    agg = _aggregate_recency(scores, window_n, std_k)
    return {"rows": rows, "scores": scores, "n_scored": n_scored,
            "avg": agg["avg"], "rel": agg["rel"], "state": agg["state"],
            "votes": agg["votes"], "window_n": window_n, "k": std_k}


def scouting_score_histogram(scores, avg=None) -> go.Figure:
    """Histogram of the recent-window per-pitch scores for one indication, with
    the three classification bands shaded (red = anti on the left, yellow =
    neutral, green = scouting on the right) and the cutoffs drawn. Tuning aid: you
    set the cutoffs by watching how the window's pitches fall across the bands."""
    an = SCOUT_PP_THRESHOLDS["anti_max"]      # negative cutoff (red on the left)
    sc = SCOUT_PP_THRESHOLDS["scouting_min"]  # positive cutoff (green on the right)
    xs = list(scores) if scores else []
    lo = min(xs + [an]) - 0.3
    hi = max(xs + [sc]) + 0.3
    fig = go.Figure()
    fig.add_vrect(x0=lo, x1=an, fillcolor="#c62828", opacity=0.12, line_width=0)
    fig.add_vrect(x0=an, x1=sc, fillcolor="#f9a825", opacity=0.10, line_width=0)
    fig.add_vrect(x0=sc, x1=hi, fillcolor="#2e7d32", opacity=0.12, line_width=0)
    if scores:
        # Explicit uniform bins whose width evenly divides the neutral band, so
        # both cutoffs fall exactly on bin edges - no bar straddles a cutoff line.
        # Bin count adapts to the data spread like nbinsx would.
        dmin, dmax = min(scores), max(scores)
        gap = sc - an
        span = max(dmax - dmin, gap)
        target = max(6, min(30, len(scores) // 2))
        w = gap / max(1, round(gap * target / span))
        start = an - int(np.ceil((an - dmin) / w)) * w
        end = sc + int(np.ceil((dmax - sc) / w)) * w
        fig.add_trace(go.Histogram(
            x=scores, xbins=dict(start=start, end=end, size=w),
            marker=dict(color="rgba(210,210,210,0.85)")))
    fig.add_vline(x=an, line=dict(color="#c62828", width=2, dash="dash"),
                  annotation_text=f"anti {an:+.2f}", annotation_position="top left")
    fig.add_vline(x=sc, line=dict(color="#2e7d32", width=2, dash="dash"),
                  annotation_text=f"scouting {sc:+.2f}", annotation_position="top right")
    if avg is not None:
        fig.add_vline(x=avg, line=dict(color="rgba(255,255,255,0.7)", width=1, dash="dot"),
                      annotation_text=f"avg {avg:+.2f}", annotation_position="bottom")
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=28, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      showlegend=False, bargap=0.05,
                      xaxis=dict(range=[lo, hi], title="per-pitch score (recent window)"),
                      yaxis_title="count")
    return fig


def scouting_recency_linechart(detail: dict) -> go.Figure:
    """Per-pitch P(observed bucket) over time for one indication - the trend view.

    y = probability the pitcher's book gave the bucket they actually hit; the
    green/yellow/red bands are the classification cutoffs converted from score to
    probability (p = exp(cutoff) / k); the dotted line is the random 1/k baseline;
    the recent voting window is shaded; a moving average traces the trend. x is
    chronological (older left). Pannable, y locked."""
    rows = detail.get("rows", [])
    k = detail.get("k", 0)
    window_n = detail.get("window_n", 0) or 0
    layout = dict(height=280, margin=dict(l=10, r=10, t=20, b=28),
                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                  showlegend=False)
    fig = go.Figure()
    if not rows or not k:
        fig.update_layout(**layout)
        return fig

    n = len(rows)
    x = list(range(1, n + 1))
    # Single P% axis for every signal. k is the standard (equal-width) bucket count,
    # so the 1/k baseline and the exp(cutoff)/k bands are exact for standard buckets;
    # the OBP signals' occasional edge buckets have a different true width, disclosed
    # per-point in the hover (and in the inspector table) rather than by bending the
    # axis.
    yv = [r["p_obs"] * 100.0 for r in rows]
    base = 100.0 / k
    green_lo = float(np.exp(SCOUT_PP_THRESHOLDS["scouting_min"]) / k * 100.0)
    red_hi = float(np.exp(SCOUT_PP_THRESHOLDS["anti_max"]) / k * 100.0)
    y_title = "P(bucket) %"
    ymax = max(max(yv), green_lo) * 1.10
    ymin = max(0.0, min(min(yv), red_hi) * 0.90)

    fig.add_hrect(y0=green_lo, y1=ymax, fillcolor="#2e7d32", opacity=0.10, line_width=0)
    fig.add_hrect(y0=red_hi, y1=green_lo, fillcolor="#f9a825", opacity=0.09, line_width=0)
    fig.add_hrect(y0=ymin, y1=red_hi, fillcolor="#c62828", opacity=0.10, line_width=0)
    fig.add_hline(y=base, line=dict(color="rgba(255,255,255,0.45)", width=1, dash="dot"))
    if window_n and n:
        fig.add_vrect(x0=max(0.5, n - window_n + 0.5), x1=n + 0.5,
                      fillcolor="rgba(255,255,255,0.06)", line_width=0)

    _cls_c = {"scouting": "#2e7d32", "neutral": "#f9a825", "anti": "#c62828"}
    colors = [_cls_c.get(r["cls"], "#9e9e9e") for r in rows]
    # Display-payload only: OBP-backtest rows carry a per-step baseline p0, so
    # disclose the observed bucket's true width and base in the hover (Decision 9).
    # Rows without a p0 key (every non-OBP signal) keep the byte-identical
    # customdata and hover below.
    _disclose = bool(rows) and ("p0" in rows[0])
    if _disclose:
        cd = [[r["pitch_val"], r.get("swing_val"), r.get("diff_val"), r.get("game"),
               r.get("arc_lo"), r.get("arc_hi"), r.get("bucket_w"), r["p0"] * 100.0]
              for r in rows]
        hovertemplate = (
            "pitch %{customdata[0]} · swing %{customdata[1]} · diff %{customdata[2]}"
            "<br>game %{customdata[3]} · P=%{y:.1f}%"
            "<br>bucket %{customdata[4]}-%{customdata[5]} (w=%{customdata[6]})"
            " · base %{customdata[7]:.1f}%<extra></extra>")
    else:
        cd = [[r["pitch_val"], r.get("swing_val"), r.get("diff_val"), r.get("game")] for r in rows]
        hovertemplate = ("pitch %{customdata[0]} · swing %{customdata[1]} · diff %{customdata[2]}"
                         "<br>game %{customdata[3]} · P=%{y:.1f}%<extra></extra>")
    fig.add_trace(go.Scatter(
        x=x, y=yv, mode="lines+markers",
        line=dict(color="rgba(200,200,200,0.45)", width=1),
        marker=dict(size=6, color=colors),
        customdata=cd,
        hovertemplate=hovertemplate,
    ))
    if n >= 3:
        ma_win = min(n, max(5, window_n // 2)) if window_n else min(n, 10)
        ma = pd.Series(yv).rolling(ma_win, min_periods=1).mean().tolist()
        fig.add_trace(go.Scatter(x=x, y=ma, mode="lines", hoverinfo="skip",
                                 line=dict(color="rgba(255,255,255,0.85)", width=2)))

    view = 20  # initial x-window width (keeps mobile readable); pan left for history
    fig.update_layout(
        dragmode="pan",
        xaxis=dict(range=[max(0.5, n - view + 0.5), n + 0.5], title="pitch (older → newer)"),
        yaxis=dict(range=[ymin, ymax], title=y_title, fixedrange=True),
        **layout,
    )
    return fig


def delta3_next_zone_dist(
    df: pd.DataFrame, value_col: str, bucket_size: int,
    prior_delta_1: int, prior_delta_2: int,
    centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Bucket counts of the next pitch when the prior two circular deltas match the given buckets."""
    delta_col = f"{value_col}_circ_delta"
    if delta_col not in df.columns or df[value_col].isna().all():
        return None
    d = df[delta_col].abs()
    if centered:
        _lo1, _hi1 = _centered_match_interval(int(prior_delta_1), bucket_size, 500)
        _lo2, _hi2 = _centered_match_interval(int(prior_delta_2), bucket_size, 500)
        m_curr = d.between(_lo2, _hi2)
        m_prev = d.shift(1).between(_lo1, _hi1)
        mask = m_curr & m_prev
    else:
        b1 = (int(prior_delta_1) - 1) // bucket_size
        b2 = (int(prior_delta_2) - 1) // bucket_size
        bkts = (d - 1) // bucket_size
        mask = ((bkts == b2) & (bkts.shift(1) == b1)).fillna(False)
    next_val = df[value_col].shift(-1)
    next_pitches = next_val[mask & next_val.notna()]
    if next_pitches.empty:
        return None
    n_bkts = 1000 // zone_bucket_size
    bkt_ids = ((next_pitches.astype(int) - 1) // zone_bucket_size).clip(0, n_bkts - 1)
    c = bkt_ids.value_counts()
    return [int(c.get(i, 0)) for i in range(n_bkts)]


def _delta_hist_to_pitch_zones(
    next_deltas: pd.Series,
    bucket_size: int,
    prior_pitch: int,
    zone_bucket_size: int,
) -> list[int] | None:
    """Convert next-delta histogram to pitch zone counts using delta_to_pitch_ranges."""
    if next_deltas.empty:
        return None
    n_bkts = 1000 // zone_bucket_size
    n_delta_bkts = 500 // bucket_size
    bkt_ids = ((next_deltas.astype(int) - 1) // bucket_size).clip(0, n_delta_bkts - 1)
    counts = bkt_ids.value_counts()
    pitch_counts = [0.0] * n_bkts

    def _add_range(lo_r: int, hi_r: int, cnt: float, n_zones: int) -> None:
        contribution = cnt / n_zones
        if lo_r <= hi_r:
            lo_bkt = (lo_r - 1) // zone_bucket_size
            hi_bkt = (hi_r - 1) // zone_bucket_size
            for bkt in range(lo_bkt, min(hi_bkt + 1, n_bkts)):
                pitch_counts[bkt] += contribution
        else:
            # Wrapping range: [lo_r, 1000] + [1, hi_r]
            lo_bkt = (lo_r - 1) // zone_bucket_size
            for bkt in range(lo_bkt, n_bkts):
                pitch_counts[bkt] += contribution
            hi_bkt = (hi_r - 1) // zone_bucket_size
            for bkt in range(0, hi_bkt + 1):
                pitch_counts[bkt] += contribution

    for di in range(n_delta_bkts):
        cnt = int(counts.get(di, 0))
        if cnt == 0:
            continue
        delta_lo = di * bucket_size
        delta_hi = (di + 1) * bucket_size
        r1, r2 = delta_to_pitch_ranges(prior_pitch, delta_lo, delta_hi)
        # Count how many distinct zone buckets this delta maps to (for fair weighting)
        zones_hit: set[int] = set()
        for lo_r, hi_r in [r1, r2]:
            if lo_r is None:
                continue
            if lo_r <= hi_r:
                lo_bkt = (lo_r - 1) // zone_bucket_size
                hi_bkt = (hi_r - 1) // zone_bucket_size
                zones_hit.update(range(lo_bkt, min(hi_bkt + 1, n_bkts)))
            else:
                lo_bkt = (lo_r - 1) // zone_bucket_size
                zones_hit.update(range(lo_bkt, n_bkts))
                hi_bkt = (hi_r - 1) // zone_bucket_size
                zones_hit.update(range(0, hi_bkt + 1))
        n_zones = max(1, len(zones_hit))
        for lo_r, hi_r in [r1, r2]:
            if lo_r is None:
                continue
            _add_range(lo_r, hi_r, cnt, n_zones)

    result = [int(round(c)) for c in pitch_counts]
    return result if any(c > 0 for c in result) else None


def delta_zone_via_delta_hist(
    df: pd.DataFrame, value_col: str, bucket_size: int, prior_delta: int,
    prior_pitch: int, centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Pitch zone dist for 2-delta rows: next-delta histogram converted via prior_pitch."""
    delta_col = f"{value_col}_circ_delta"
    if delta_col not in df.columns or df[value_col].isna().all():
        return None
    # Filter to swing plays only and sort by id, matching seq2_delta_hint.
    # First pitches of each game retain NaN delta, acting as natural game-boundary guards.
    sw = df[df[value_col].notna()].sort_values("id")
    d = sw[delta_col].abs()
    if centered:
        _lo, _hi = _centered_match_interval(int(prior_delta), bucket_size, 500)
        mask = d.between(_lo, _hi)
    else:
        prior_bkt = (int(prior_delta) - 1) // bucket_size
        mask = ((d - 1) // bucket_size == prior_bkt).fillna(False)
    next_d = d.shift(-1)
    return _delta_hist_to_pitch_zones(
        next_d[mask & next_d.notna()], bucket_size, int(prior_pitch), zone_bucket_size
    )


def delta3_zone_via_delta_hist(
    df: pd.DataFrame, value_col: str, bucket_size: int,
    prior_delta_1: int, prior_delta_2: int, prior_pitch: int,
    centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Pitch zone dist for 3-delta rows: next-delta histogram converted via prior_pitch."""
    delta_col = f"{value_col}_circ_delta"
    if delta_col not in df.columns or df[value_col].isna().all():
        return None
    # Filter to swing plays only and sort by id, matching seq3_delta_hint.
    sw = df[df[value_col].notna()].sort_values("id")
    d = sw[delta_col].abs()
    if centered:
        _lo1, _hi1 = _centered_match_interval(int(prior_delta_1), bucket_size, 500)
        _lo2, _hi2 = _centered_match_interval(int(prior_delta_2), bucket_size, 500)
        m_curr = d.between(_lo2, _hi2)
        m_prev = d.shift(1).between(_lo1, _hi1)
        mask = m_curr & m_prev
    else:
        b1 = (int(prior_delta_1) - 1) // bucket_size
        b2 = (int(prior_delta_2) - 1) // bucket_size
        bkts = (d - 1) // bucket_size
        mask = ((bkts == b2) & (bkts.shift(1) == b1)).fillna(False)
    next_d = d.shift(-1)
    return _delta_hist_to_pitch_zones(
        next_d[mask & next_d.notna()], bucket_size, int(prior_pitch), zone_bucket_size
    )


def diff_next_zone_dist(
    df: pd.DataFrame, value_col: str, prior_diff: int,
    centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Bucket counts of the next pitch when the current diff falls in the same quality bucket."""
    if "diff" not in df.columns or df[value_col].isna().all():
        return None
    d = df["diff"].abs()
    if centered:
        half = _diff_centered_half(int(prior_diff))
        mask = ((d - float(prior_diff)).abs() <= half).fillna(False)
    else:
        d_cut = pd.cut(d.astype(int), bins=_DIFF_HM_BINS, labels=False, right=True, include_lowest=True)
        prior_cut = pd.cut(pd.Series([int(prior_diff)]), bins=_DIFF_HM_BINS, labels=False,
                           right=True, include_lowest=True).iloc[0]
        mask = (d_cut == prior_cut).fillna(False)
    next_val = df[value_col].shift(-1)
    next_pitches = next_val[mask & next_val.notna()]
    if next_pitches.empty:
        return None
    n_bkts = 1000 // zone_bucket_size
    bkt_ids = ((next_pitches.astype(int) - 1) // zone_bucket_size).clip(0, n_bkts - 1)
    c = bkt_ids.value_counts()
    return [int(c.get(i, 0)) for i in range(n_bkts)]


def diff_to_delta_zone_dist(
    df: pd.DataFrame, value_col: str, prior_diff: int, prior_pitch: int,
    centered: bool = False, zone_bucket_size: int = 111,
) -> list[int] | None:
    """Zone distribution for Prior-diff->delta row: projects next-delta histogram via prior_pitch.
    Conditions on current diff bucket (same as diff_to_delta_hint) then uses delta projection
    so All Zones aligns with Best Zone for this row type."""
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    if "diff" not in df.columns or delta_col not in df.columns or df[value_col].isna().all():
        return None
    sw = df[df[value_col].notna() & df["diff"].notna()].sort_values("id")
    d_delta = sw[delta_col].abs()
    diff_abs = sw["diff"].abs()
    if centered:
        half = _diff_centered_half(int(prior_diff))
        mask = ((diff_abs - float(prior_diff)).abs() <= half).fillna(False)
    else:
        d_cut = pd.cut(diff_abs.astype(int), bins=_DIFF_HM_BINS, labels=False,
                       right=True, include_lowest=True)
        prior_cut = pd.cut(pd.Series([int(prior_diff)]), bins=_DIFF_HM_BINS, labels=False,
                           right=True, include_lowest=True).iloc[0]
        mask = (d_cut == prior_cut).fillna(False)
    next_d = d_delta.shift(-1)
    return _delta_hist_to_pitch_zones(
        next_d[mask & next_d.notna()], 100, int(prior_pitch), zone_bucket_size
    )


def swing_predictor_chart(
    df: pd.DataFrame,
    swing: int,
    n: int = 20,
    title: str = "Swing Analyzer",
    result_ranges: list | None = None,
    tick_label: str = "Recent Pitches",
    value_col: str = "pitch",
    x_label: str = "Pitch Values",
    ref_label: str = "Swing",
    ref_color: str = "navy",
    tick_weights: list[float] | None = None,
    obr_extra: frozenset[str] = frozenset(),
) -> go.Figure:
    """Color-coded number line for a proposed reference value, with recent pitch/swing values overlaid.
    value_col: column to pull tick marks from ('pitch' for pitcher page, 'swing' for batter page).
    """
    ranges = result_ranges or RESULT_RANGES
    # For each value 1-1000, compute result given the reference (circular diff is symmetric)
    pitch_result = [_diff_to_result(circular_diff(p, swing), ranges) for p in range(1, 1001)]

    # Collapse into contiguous zones
    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()

    # Invisible trace to anchor axes
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    # Build diff-range lookup for legend labels: result → (diff_lo, diff_hi, width)
    diff_info = {r: (lo, hi, hi - lo + 1) for r, lo, hi in ranges}

    # Draw colored rectangles for each zone
    seen: set[str] = set()
    for result, lo, hi in zones:
        color = _result_color(result)
        if result not in seen:
            d_lo, d_hi, w = diff_info.get(result, (0, 0, 0))
            label = f"{result}: {d_lo}–{d_hi} ({w})"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=label, showlegend=True,
            ))
            seen.add(result)
        fig.add_shape(
            type="rect", x0=lo - 0.5, x1=hi + 0.5, y0=0, y1=1,
            fillcolor=color, line=dict(width=0), layer="below",
        )
        if hi - lo >= len(result) * 12:
            fig.add_annotation(
                x=(lo + hi) / 2, y=0.5, text=result,
                showarrow=False, font=dict(size=9, color="white"),
                xanchor="center", yanchor="middle",
            )

    # Tick marks - triangles beneath the colored zone
    # Color: blue=low relevance/old -> white -> red=high relevance/new
    import numpy as _np
    df_last = df.sort_values("id").tail(n)
    vals = df_last[value_col].astype(int).tolist()
    n_vals = len(vals)
    if tick_weights is not None and len(tick_weights) == n_vals and n_vals > 0:
        _w = _np.array(tick_weights, dtype=float)
        _wmin, _wmax = _w.min(), _w.max()
        color_vals = (
            ((_w - _wmin) / (_wmax - _wmin) * (n_vals - 1)).tolist()
            if _wmax > _wmin else [float(n_vals - 1) / 2] * n_vals
        )
    else:
        color_vals = list(range(n_vals))
    fig.add_trace(go.Scatter(
        x=vals, y=[-0.08] * n_vals,
        mode="markers",
        marker=dict(
            symbol="triangle-up", size=9,
            color=color_vals,
            colorscale=[[0, "#4575b4"], [0.5, "white"], [1, "#d73027"]],
            cmin=0, cmax=max(n_vals - 1, 1),
            showscale=False,
            line=dict(width=0.5, color="white"),
        ),
        name="Recent Pitches",
        hovertemplate=f"{value_col.capitalize()}: %{{x}}<extra></extra>",
    ))

    # Delta scale - tick marks above the zone bar showing Δ from the most recent value
    implied_delta = None
    if vals:
        last_val = vals[-1]
        implied_delta = circular_signed_delta(last_val, swing)
        for delta in [-400, -300, -200, -100, 0, 100, 200, 300, 400, 500]:
            abs_pos = ((last_val + delta - 1) % 1000) + 1
            lbl = "±500" if delta == 500 else (f"+{delta}" if delta > 0 else ("0" if delta == 0 else str(delta)))
            is_zero = delta == 0
            fig.add_shape(
                type="line", xref="x", yref="paper",
                x0=abs_pos, x1=abs_pos, y0=1.02, y1=1.09,
                line=dict(
                    color="rgba(128,128,128,0.9)" if is_zero else "rgba(128,128,128,0.5)",
                    width=1.5 if is_zero else 1,
                ),
            )
            fig.add_annotation(
                x=abs_pos, xref="x", y=1.10, yref="paper",
                text=lbl, showarrow=False,
                font=dict(size=11),
                xanchor="center", yanchor="bottom",
            )

        # Delta triangles above zone bar - project each historical delta from most recent value
        delta_col = f"{value_col}_circ_delta"
        if delta_col in df_last.columns:
            delta_raw = df_last[delta_col].tolist()
            top_x, top_idx, top_d = [], [], []
            for i, d in enumerate(delta_raw):
                if not pd.isna(d):
                    d_int = int(d)
                    top_x.append(((last_val + d_int - 1) % 1000) + 1)
                    top_idx.append(i)
                    top_d.append(d_int)
            if top_x:
                top_colors = [color_vals[i] for i in top_idx]
                fig.add_trace(go.Scatter(
                    x=top_x,
                    y=[1.08] * len(top_x),
                    mode="markers",
                    marker=dict(
                        symbol="triangle-down", size=9,
                        color=top_colors,
                        colorscale=[[0, "#4575b4"], [0.5, "white"], [1, "#d73027"]],
                        cmin=0, cmax=max(n_vals - 1, 1),
                        showscale=False,
                        line=dict(width=0.5, color="white"),
                    ),
                    text=[f"Δ{d:+d} → {x}" for d, x in zip(top_d, top_x)],
                    hovertemplate="%{text}<extra></extra>",
                    name="Recent Δ",
                    showlegend=True,
                ))

    # OBR boundary lines - offset clamped so labels stay on-screen at chart edges
    obr_max = max((hi for result, lo, hi in ranges if result in (_OBR | obr_extra)), default=0)
    if obr_max > 0:
        b_lo = ((swing - obr_max - 1) % 1000) + 1
        b_hi = ((swing + obr_max - 1) % 1000) + 1
        for boundary, default_ax in [(b_lo, -40), (b_hi, 40)]:
            ax = 40 if boundary < 120 else (-40 if boundary > 880 else default_ax)
            fig.add_vline(x=boundary, line_dash="dot", line_color="#1a7d35", line_width=1.5)
            fig.add_annotation(
                x=boundary, y=0.82,
                ax=ax, ay=0,
                text=str(boundary),
                showarrow=True, arrowhead=2, arrowsize=0.9, arrowwidth=2,
                arrowcolor="#1a7d35",
                font=dict(color="#1a7d35", size=10, weight="bold"),
                bgcolor="rgba(255,255,255,0.8)",
                borderpad=2,
            )

    # Reference value pill - same y as OBR labels (ay=0), white bg, green text
    pill_text = f"{ref_label} {swing}" + (f"<br>Δ{implied_delta:+d}" if implied_delta is not None else "")
    # Two-layer vline: dark outline first, white center on top → visible on both light and dark backgrounds
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=swing, x1=swing, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=swing, y=0.82,
        text=pill_text,
        showarrow=False,
        xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)",
        borderpad=2,
    )

    # Top axis label ("Pitch Δ" / "Swing Δ") - positioned above the delta tick marks
    delta_axis_label = x_label.replace("Values", "Δ").replace("Value", "Δ")
    fig.add_annotation(
        x=500, xref="x", y=1.20, yref="paper",
        text=f"<b>{delta_axis_label}</b>",
        showarrow=False,
        font=dict(size=11),
        xanchor="center", yanchor="bottom",
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.18, 1.25]),
        height=440,
        margin=dict(l=10, r=25, t=90, b=130),
        legend=dict(
            orientation="h", x=0.5, y=-0.55,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def _normalize_ranges(raw: list) -> list[tuple[str, int, int]]:
    """Convert list of dicts or 3-tuples into a uniform list of (result, lo, hi) tuples."""
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append((entry["result"], entry["low"], entry["high"]))
        else:
            out.append(tuple(entry))
    return out


def manager_color_bar(proposed_value: int, result_ranges: list | None = None,
                      label: str = "Swing", x_label: str = "Swing Values") -> go.Figure:
    """Color-coded number line matching swing_predictor_chart style, without triangles or delta scale."""
    ranges = _normalize_ranges(result_ranges) if result_ranges else RESULT_RANGES
    pitch_result = [_diff_to_result(circular_diff(p, proposed_value), ranges) for p in range(1, 1001)]

    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    diff_info = {r: (lo, hi, hi - lo + 1) for r, lo, hi in ranges}

    seen: set[str] = set()
    for result, lo, hi in zones:
        color = _result_color(result)
        if result not in seen:
            d_lo, d_hi, w = diff_info.get(result, (0, 0, 0))
            _legend_lbl = f"{result}: {d_lo}-{d_hi} ({w})"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=_legend_lbl, showlegend=True,
            ))
            seen.add(result)
        fig.add_shape(
            type="rect", x0=lo - 0.5, x1=hi + 0.5, y0=0, y1=1,
            fillcolor=color, line=dict(width=0), layer="below",
        )
        if hi - lo >= len(result) * 12:
            fig.add_annotation(
                x=(lo + hi) / 2, y=0.5, text=result,
                showarrow=False, font=dict(size=9, color="white"),
                xanchor="center", yanchor="middle",
            )

    obr_max = max((hi for result, lo, hi in ranges if result in _OBR), default=0)
    if obr_max > 0:
        b_lo = ((proposed_value - obr_max - 1) % 1000) + 1
        b_hi = ((proposed_value + obr_max - 1) % 1000) + 1
        for boundary, default_ax in [(b_lo, -40), (b_hi, 40)]:
            ax = 40 if boundary < 120 else (-40 if boundary > 880 else default_ax)
            fig.add_vline(x=boundary, line_dash="dot", line_color="#1a7d35", line_width=1.5)
            fig.add_annotation(
                x=boundary, y=0.82, ax=ax, ay=0, text=str(boundary),
                showarrow=True, arrowhead=2, arrowsize=0.9, arrowwidth=2,
                arrowcolor="#1a7d35",
                font=dict(color="#1a7d35", size=10, weight="bold"),
                bgcolor="rgba(255,255,255,0.8)", borderpad=2,
            )

    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=proposed_value, x1=proposed_value, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=proposed_value, y=0.82, text=f"{label} {proposed_value}",
        showarrow=False, xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)", borderpad=2,
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.1, 1.1]),
        height=260,
        margin=dict(l=10, r=25, t=10, b=130),
        legend=dict(
            orientation="h", x=0.5, y=-0.65,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def bases_diamond_fig(obc: str, outs: int) -> go.Figure:
    """Broadcast-style base diamond with occupied bases highlighted in gold."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    gold        = "#FFD700"
    gold_border = "#FFA500"
    empty       = "#2d2d2d"
    empty_border = "#666666"

    fig = go.Figure()

    # Basepath outline
    fig.add_trace(go.Scatter(
        x=[0.5, 1.0, 0.5, 0.0, 0.5],
        y=[0.0, 0.5, 1.0, 0.5, 0.0],
        mode="lines",
        line=dict(color="#555555", width=1.5),
        showlegend=False, hoverinfo="skip",
    ))

    # Base markers: home=bottom, 1B=right, 2B=top, 3B=left
    for x, y, occupied, label in [
        (0.5, 1.0, on_2b, "2B"),
        (1.0, 0.5, on_1b, "1B"),
        (0.0, 0.5, on_3b, "3B"),
        (0.5, 0.0, False, "H"),
    ]:
        fig.add_trace(go.Scatter(
            x=[x], y=[y],
            mode="markers",
            marker=dict(
                symbol="square",
                size=16,
                color=gold if occupied else empty,
                line=dict(color=gold_border if occupied else empty_border, width=2),
                angle=45,
            ),
            showlegend=False, hoverinfo="skip",
        ))

    # Outs dots below home plate
    for i in range(3):
        filled = i < outs
        fig.add_trace(go.Scatter(
            x=[0.35 + i * 0.15], y=[-0.28],
            mode="markers",
            marker=dict(
                symbol="circle",
                size=8,
                color="#FFD700" if filled else "#2d2d2d",
                line=dict(color="#888888", width=1.5),
            ),
            showlegend=False, hoverinfo="skip",
        ))

    fig.update_layout(
        xaxis=dict(visible=False, range=[-0.25, 1.25]),
        yaxis=dict(visible=False, range=[-0.45, 1.25]),
        width=115, height=130,
        margin=dict(l=0, r=8, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
        showlegend=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def bases_diamond_svg(obc: str, outs: int) -> str:
    """Fixed-size SVG base diamond - immune to Plotly/Streamlit resize events."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    gold, g_bdr  = "#FFD700", "#FFA500"
    empty, e_bdr = "#2d2d2d", "#666666"

    # Base positions (90x100 canvas): 2B=top, 1B=right, 3B=left, home=bottom
    home   = (45, 74)
    first  = (74, 44)
    second = (45, 14)
    third  = (16, 44)

    def base(cx, cy, filled):
        r = 7
        pts = f"{cx},{cy-r} {cx+r},{cy} {cx},{cy+r} {cx-r},{cy}"
        c, b = (gold, g_bdr) if filled else (empty, e_bdr)
        return f'<polygon points="{pts}" fill="{c}" stroke="{b}" stroke-width="2"/>'

    def ln(p1, p2):
        return (f'<line x1="{p1[0]}" y1="{p1[1]}" '
                f'x2="{p2[0]}" y2="{p2[1]}" stroke="#555" stroke-width="1.5"/>')

    path  = ln(home, first) + ln(first, second) + ln(second, third) + ln(third, home)
    bases = base(*home, False) + base(*first, on_1b) + base(*second, on_2b) + base(*third, on_3b)
    dots  = "".join(
        f'<circle cx="{33 + i*12}" cy="91" r="4" '
        f'fill="{"#FFD700" if i < outs else "#2d2d2d"}" stroke="#888" stroke-width="1.5"/>'
        for i in range(3)
    )
    return f'<svg width="90" height="100" xmlns="http://www.w3.org/2000/svg">{path}{bases}{dots}</svg>'


def steal_color_bar(proposed_value: int, safe_range: int,
                    label: str = "Steal", x_label: str = "Steal Values") -> go.Figure:
    """Color bar for a steal attempt: Safe zone vs Out zone."""
    safe_color = "#2ca02c"
    out_color  = "#b10026"

    pitch_result = [
        "Safe" if circular_diff(p, proposed_value) <= safe_range else "Out"
        for p in range(1, 1001)
    ]

    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    seen: set[str] = set()
    for result, lo, hi in zones:
        color = safe_color if result == "Safe" else out_color
        if result not in seen:
            prob = round(safe_range * 2 / 1000 * 100, 1)
            _legend_lbl = f"Safe: diff <= {safe_range} ({prob}%)" if result == "Safe" else f"Out: diff > {safe_range}"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=_legend_lbl, showlegend=True,
            ))
            seen.add(result)
        fig.add_shape(
            type="rect", x0=lo - 0.5, x1=hi + 0.5, y0=0, y1=1,
            fillcolor=color, line=dict(width=0), layer="below",
        )
        if hi - lo >= len(result) * 12:
            fig.add_annotation(
                x=(lo + hi) / 2, y=0.5, text=result,
                showarrow=False, font=dict(size=9, color="white"),
                xanchor="center", yanchor="middle",
            )

    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=proposed_value, x1=proposed_value, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=proposed_value, y=0.82, text=f"{label} {proposed_value}",
        showarrow=False, xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)", borderpad=2,
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.1, 1.1]),
        height=260,
        margin=dict(l=10, r=25, t=10, b=80),
        legend=dict(
            orientation="h", x=0.5, y=-0.45,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ------------------------------------------------------------------ at-bat range calculator
# Derived from MLN Calculator 11.0 formulas (calculator tab).
# OBR helper table: range widths at each differential -5..+5 for each result type.
_DIFFS = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]

_OBR_TABLE: dict[str, list[int]] = {
    "Hit":    [84, 99, 110, 119, 126, 132, 138, 145, 154, 165, 180],
    "HR":     [1,  1,  8,  16,  18,  20,  22,  24,  32,  47,  62],
    "3B":     [1,  1,  3,   4,   5,   6,   7,   8,   9,  11,  14],
    "2B":     [15, 20, 24,  27,  29,  30,  31,  33,  36,  40,  45],
    "IF1B":   [1,  2,  6,   8,   9,  10,  11,  12,  14,  18,  24],
    "BB":     [1,  3, 14,  23,  30,  35,  40,  47,  56,  67,  78],
    "FO_HND": [147,132,121,112, 105, 100,  95,  88,  79,  68,  53],  # /500 = FO%
    "PO_HND": [188,171,158,146, 135, 125, 115, 104,  92,  79,  62],  # /500 = PO%
    "K":      [183,160,142,127, 115, 105,  95,  83,  68,  50,  27],
}

# 1B extra adjustments (rows 40-43 of calculator tab)
_1B_SPD_AWR = {-5:-3,-4:-3,-3:-2,-2:-2,-1:-1,0:0,1:1,2:2,3:2,4:3,5:3}
_1B_PITCH_AWR = {-3:3,-2:2,-1:1,0:0,1:-1,2:-2,3:-3}  # keyed by pitcher_awr-3, clamped -3..3
_1B_HIT_NEG = {-5:5,-4:5,-3:5,-2:5,-1:5,0:3,1:0,2:0,3:0,4:0,5:0}  # row 43

# Steal safe-range table: differential -5..+5 in 0.5 steps (21 values)
STEAL_DIFFS = [-5,-4.5,-4,-3.5,-3,-2.5,-2,-1.5,-1,-0.5,0,0.5,1,1.5,2,2.5,3,3.5,4,4.5,5]
STEAL_TABLE: dict[str, list[int]] = {
    "2nd":  [62,86,108,132,154,177,199,221,242,265,285,308,329,351,373,396,418,442,464,488,499],
    "3rd":  [7,19,32,48,61,76,87,100,110,120,130,140,150,163,174,189,202,218,233,251,259],
    "home": [2,4,7,11,14,18,20,22,23,25,25,27,28,30,32,36,39,43,47,53,55],
}

def steal_safe_range_plus1_spd(safe_range: int, base: str = "1B") -> int:
    """Return the steal safe range after adding +1 to the runner's speed.

    Finds the nearest matching index in STEAL_TABLE for the given base, then
    moves +2 positions (each position = 0.5 speed differential, so +1 speed = +2 steps).
    """
    base_key = {"1B": "2nd", "2B": "3rd", "3B": "home"}.get(base, "2nd")
    table = STEAL_TABLE[base_key]
    idx = min(range(len(table)), key=lambda i: abs(table[i] - safe_range))
    return table[min(idx + 2, len(table) - 1)]


RESULT_COLORS = {
    "HR":    "#e74c3c",
    "3B":    "#e67e22",
    "2B":    "#f1c40f",
    "1B":    "#2ecc71",
    "IF1B":  "#1abc9c",
    "BB":    "#3498db",
    "FO":    "#bdc3c7",
    "PO":    "#95a5a6",
    "GO":    "#7f8c8d",
    "K":     "#2c3e50",
    "LO":    "#6c7a7d",
    "B1BWH": "#27ae60",
    "B1B":   "#52be80",
    "BFC":   "#7f8c8d",
    "SacB":  "#a9cce3",
    "BDP":   "#5d6d7e",
}


def _obr_lookup(key: str, diff: int) -> int:
    idx = _DIFFS.index(max(-5, min(5, diff)))
    return _OBR_TABLE[key][idx]


def compute_at_bat_ranges(
    pitcher_hand: str,
    pitcher_mov: int,
    pitcher_cmd: int,
    pitcher_vel: int,
    pitcher_awr: int,
    batter_hand: str,
    batter_con: int,
    batter_eye: int,
    batter_pow: int,
    batter_spd: int,
    bunt: bool = False,
    hit_and_run: bool = False,
    infield_in: bool = False,
    outs: int = 0,
    runners_on: bool = False,
    obc: str = "000",
    runner_1b_spd: int | None = None,
    runner_2b_spd: int | None = None,
    runner_3b_spd: int | None = None,
    _debug: bool = False,
) -> "list[dict] | tuple[list[dict], dict]":
    """Compute at-bat result ranges from pitcher/batter stats.

    Returns list of dicts: {result, range, low, high}.
    When _debug=True, returns (result_list, debug_dict) with intermediate values.
    Verified against MLN Calculator 11.0.
    hit_and_run: batter CON -1; runner SPD +1 for all dynamic rate calcs.
    infield_in: IF1B removed, +20 to 1B (W14); GORA = 0.
    obc: on-base string "3b2b1b" e.g. "010" = runner on 2B only.
    runner_Xb_spd: runner speeds for WH%, FO, LO, GO subrange splits.
    """
    import math

    def clamp(v, lo, hi):
        return max(lo, min(hi, int(v)))

    def clampf(v, lo=0.0, hi=1.0):
        return max(lo, min(hi, float(v)))

    # OBC bits
    on_3b = len(obc) >= 1 and obc[0] == "1"
    on_2b = len(obc) >= 2 and obc[1] == "1"
    on_1b = len(obc) >= 3 and obc[2] == "1"
    _has_runners = on_3b or on_2b or on_1b or runners_on

    # H&R: batter CON reduced by 1 before any differential calculation
    _batter_con = batter_con - 1 if hit_and_run else batter_con

    # Handedness modifier
    if str(batter_hand).upper() == "S":
        hnd = 1.0
    elif str(pitcher_hand).upper() == str(batter_hand).upper():
        hnd = 0.975
    else:
        hnd = 1.025

    # Stat differentials (clamped -5..+5)
    d_hit = clamp(_batter_con - pitcher_mov, -5, 5)
    d_pow = clamp(batter_pow - pitcher_vel, -5, 5)
    d_spd = clamp(batter_spd - pitcher_awr, -5, 5)
    d_eye = clamp(batter_eye - pitcher_cmd, -5, 5)

    _dbg: dict = {
        "d_hit": d_hit, "d_pow": d_pow, "d_spd": d_spd, "d_eye": d_eye,
        "hnd": round(hnd, 3),
    }

    def w_std(key, diff):
        return max(1, math.floor(_obr_lookup(key, diff) * hnd))

    w_hr   = w_std("HR", d_pow)
    w_3b   = w_std("3B", d_spd)
    w_2b   = w_std("2B", d_spd)
    # Base IF1B width (SPD:AWR diff + handedness) - always computed for bonus transfers
    _base_if1b = w_std("IF1B", d_spd)
    # Slot is removed when infield_in (AC17); not affected by hit_and_run
    w_if1b = 0 if infield_in else _base_if1b
    w_bb   = w_std("BB", d_eye)
    w_k    = w_std("K", d_hit)

    _dbg.update({
        "w_hr": w_hr, "w_3b": w_3b, "w_2b": w_2b, "w_1b": None,
        "w_if1b": w_if1b, "w_bb": w_bb, "w_k": w_k,
    })

    # 1B: Hit-base * handedness + modifiers + constant - XBH
    # +20 fixed when infield_in (Excel W14: IF(AC17,20,0))
    hit_base = math.floor(_obr_lookup("Hit", d_hit) * hnd)
    w_1b = max(1,
        hit_base
        + _1B_HIT_NEG[d_hit]
        + _1B_SPD_AWR[d_spd]
        + _1B_PITCH_AWR.get(clamp(pitcher_awr - 3, -3, 3), 0)
        + 5
        - w_hr - w_3b - w_2b
        + (20 if infield_in else 0)
    )
    _dbg["w_1b"] = w_1b

    # FO and PO: rate-based from d_pow
    fo_rate = _obr_lookup("FO_HND", d_pow) / 500
    po_rate = _obr_lookup("PO_HND", d_pow) / 500
    after_hits = 500 - (w_hr + w_3b + w_2b + w_1b + _base_if1b + w_bb)
    after_bb   = 500 - w_bb
    w_fo = max(1, math.floor(after_hits * fo_rate * (1 - po_rate)))
    w_po = max(1, math.floor(after_bb   * fo_rate * po_rate))

    # LO: 4-wide slot, only with runners on and fewer than 2 outs (W21)
    w_lo = 4 if (_has_runners and outs < 2) else 0

    # GO: remainder of 501 total (0-500 inclusive)
    # Use _base_if1b (not w_if1b): when II, w_if1b=0 but 1B_plain gains +_base_if1b,
    # so the IF1B width still comes out of the GO pool either way.
    w_go = 500 - (w_hr + w_3b + w_2b + w_1b + _base_if1b + w_bb + w_fo + w_po + w_k + w_lo) + 1

    if bunt:
        b1bwh   = 9
        total_hit = w_hr + w_3b + w_2b + w_1b + w_if1b
        base_hit  = total_hit - 1
        spd_mov   = batter_spd - pitcher_mov
        b1b = max(1, round((1 + spd_mov * 0.04) * base_hit) - b1bwh)
        b_bb = w_bb
        b_k  = w_k
        # TP and LOTP: base of 4 each when runners on base (used for SacB/BDP pool)
        _b_tp_base   = 4 if _has_runners else 0
        _b_lotp_base = 4 if _has_runners else 0
        b_go_pool = max(0, 500 - (b1bwh + b1b + b_bb + b_k + _b_tp_base + _b_lotp_base))

        # TP final width: rate (T62) = 1 when outs==0 AND on_1b AND on_2b, else 0
        _b_tp_active = on_1b and on_2b and outs == 0
        _b_tp_final  = 4 if _b_tp_active else 0

        # LOTP final width: rate (T63) = 0.25 when outs==0, TP not active, >=2 runners
        _runner_count = sum(1 for b in [on_1b, on_2b, on_3b] if b)
        _b_lotp_rate  = 0.25 if (_has_runners and not _b_tp_active and outs == 0 and _runner_count >= 2) else 0.0
        _b_lotp_final = math.floor(_b_lotp_rate * _b_lotp_base)

        # SacB rate from A85:N89 lookup keyed on d_spd (batter_spd - pitcher_awr)
        # Bunt use determined by lead runner: on_3b -> Runner Home, on_2b -> Runner to 3rd, else -> Runner to 2nd
        if not _has_runners:
            sacb_rate = 0.0
            bdp_rate  = 0.0
        elif on_3b:
            sacb_rate = clampf(0.06 + 0.01 * d_spd)
            bdp_rate  = clampf(0.10 - 0.02 * d_spd)
        elif on_2b:
            sacb_rate = clampf(0.27 + 0.03 * d_spd)
            bdp_rate  = clampf(0.10 - 0.02 * d_spd)
        else:
            sacb_rate = clampf(0.50 + 0.07 * d_spd)
            bdp_rate  = clampf(0.10 - 0.02 * d_spd)
        b_sacb = math.floor(sacb_rate * b_go_pool)
        b_bdp  = math.floor(bdp_rate  * b_go_pool)
        # BFC = 501 - all other finals (V73 formula); pool uses bases, BFC uses finals
        b_go = max(0, 501 - b1bwh - b1b - b_bb - b_k - b_sacb - b_bdp - _b_tp_final - _b_lotp_final)
        _dbg.update({
            "mode": "bunt",
            "total_hit": total_hit, "base_hit": base_hit,
            "b1bwh": b1bwh, "b1b": b1b,
            "b_tp_base": _b_tp_base, "b_lotp_base": _b_lotp_base,
            "b_tp_final": _b_tp_final, "b_lotp_final": _b_lotp_final,
            "b_go_pool": b_go_pool,
            "sacb_rate": sacb_rate, "bdp_rate": bdp_rate,
            "b_sacb": b_sacb, "b_bdp": b_bdp, "b_go": b_go,
        })
        # BFC when runners on base, GO when bases empty
        _go_label = "BFC" if _has_runners else "GO"
        # Bunt order from calculator S67:S75
        _BUNT_ORDER = ["B1BWH","B1B","BB","SacB","K",_go_label,"BDP","TP","LOTP"]
        _bunt_w: dict[str, int] = {
            "B1BWH": b1bwh, "B1B": b1b, "BB": b_bb, "K": b_k,
            "SacB": b_sacb, _go_label: b_go, "BDP": b_bdp,
            "TP": _b_tp_final, "LOTP": _b_lotp_final,
        }
        rows = [(_n, _bunt_w[_n]) for _n in _BUNT_ORDER if _bunt_w.get(_n, 0) > 0]
    else:
        # Runner effective speeds: H&R gives +1 SPD to all runners for dynamic rate calcs
        def _eff(s: int | None) -> int | None:
            return (s + (1 if hit_and_run else 0)) if s is not None else None

        s1 = _eff(runner_1b_spd) if on_1b else None
        s2 = _eff(runner_2b_spd) if on_2b else None
        s3 = _eff(runner_3b_spd) if on_3b else None

        def _avg(*vals: int | None) -> float:
            v = [x for x in vals if x is not None]
            return float(sum(v)) / len(v) if v else 3.0

        # --- Well Hit % (H75/H76) ---
        # H&R multiplier effectively forces WH% = 1.0; 2-out mult = 3; else 1
        def _wh_rate(spd: int | None) -> float:
            if spd is None:
                return 0.0
            if hit_and_run:
                return 1.0 if spd > 0 else 0.0
            mult = 3.0 if outs == 2 else 1.0
            delta = spd - 3
            return clampf((0.15 + delta * (1.0 if delta >= 0 else 0.5) * 0.07) * mult)

        # F75: lead runner (2B if present, else 1B)
        _lead_spd = s2 if on_2b else (s1 if on_1b else None)
        # F76: trail runner (1B capped at 2B speed; only exists when both runners present)
        _trail_spd: int | None = None
        if on_1b and on_2b:
            _trail_spd = min(
                s1 if s1 is not None else 3,
                s2 if s2 is not None else 3,
            )

        _lead_wh  = _wh_rate(_lead_spd)
        _trail_wh = _wh_rate(_trail_spd) if _trail_spd is not None else None

        # T30: 2BWH rate - 0 if no runner on 1B; else trail_wh or lead_wh
        _2bwh_rate = (_trail_wh if _trail_wh is not None else _lead_wh) if on_1b else 0.0
        # T33: 1BWH rate - 0 if no runners on 1B/2B; else trail_wh or lead_wh
        _1bwh_rate = (_trail_wh if _trail_wh is not None else _lead_wh) if (on_1b or on_2b) else 0.0
        # T34: 1BWH2 rate - only when runners on 1B AND 2B
        _1bwh2_rate = clampf(_lead_wh - (_trail_wh or 0.0)) if (on_1b and on_2b) else 0.0

        # 2B split (V30/V31)
        w_2bwh = math.floor(_2bwh_rate * w_2b)
        w_2b_plain = max(0, w_2b - w_2bwh)

        # 1B split (V33/V34/V35); V33 adds IF1B base width when H&R AND Infield In both on
        # V35 = U35 - V33 - V34 (catch-all, no min) + IF1B base width when Infield In
        _if1b_bonus = _base_if1b if (hit_and_run and infield_in) else 0
        w_1bwh  = math.floor(_1bwh_rate  * w_1b) + _if1b_bonus
        w_1bwh2 = math.floor(_1bwh2_rate * w_1b)
        w_1b_plain = max(0, w_1b - w_1bwh - w_1bwh2) + (_base_if1b if infield_in else 0)

        # --- I75: DFO% (for FO split) - lead runner WH% sans 2-out mult, only if runner on 2B ---
        _dfo_pct = 0.0
        if on_2b and outs < 2:
            _s2_eff = s2 if s2 is not None else 3
            delta = _s2_eff - 3
            _dfo_pct = clampf(0.15 + delta * (1.0 if delta >= 0 else 0.5) * 0.07)

        # --- FO split (T41-T44 / V41-V44) ---
        # DSacF/DFO require runners on 2B AND 3B, <2 outs
        # SacF requires runner on 3B, <2 outs
        # DFO (2B runner tags to 3B) applies whenever runner on 2B, <2 outs
        _fo_rows: list[tuple[str, int]] = []
        if outs == 2 or (not on_2b and not on_3b):
            # No tagging opportunity or 2-out: plain FO
            _fo_rows = [("FO", w_fo)]
        elif on_3b and on_2b:
            # 3B runner always scores; _dfo_pct share of 2B runner also tags (DSacF), rest is SacF
            # DSacF and DFO are mutually exclusive - DFO does not appear when on_3b
            w_dsacf      = math.floor(_dfo_pct * w_fo)
            w_sacf_final = max(0, w_fo - w_dsacf)
            if w_dsacf      > 0: _fo_rows.append(("DSacF", w_dsacf))
            if w_sacf_final > 0: _fo_rows.append(("SacF",  w_sacf_final))
        elif on_3b:
            # Only 3B runner; no 2B to tag - entire FO pool is SacF
            _fo_rows = [("SacF", w_fo)]
        else:  # on_2b and not on_3b: 2B runner can tag to 3B (DFO); no scoring runner
            w_dfo      = math.floor(_dfo_pct * w_fo)
            w_fo_plain = max(0, w_fo - w_dfo)
            if w_dfo      > 0: _fo_rows.append(("DFO", w_dfo))
            if w_fo_plain > 0: _fo_rows.append(("FO",  w_fo_plain))

        # --- LO split (V61/V62) ---
        # TP: runners on 1B AND 2B and 0 outs -> full LO = TP
        # LODP: runners present, no runner on 1B (CSV T61: Z11=1B runner; LODP=0 when Z11>0)
        # When runner on 1B and no TP: LODP=0, LO width reallocated to K (CSV V59)
        _lo_rows: list[tuple[str, int]] = []
        _k_lo_bonus = 0
        if w_lo > 0:
            if on_1b and on_2b and outs == 0:
                _lo_rows = [("TP", w_lo)]
            elif not on_1b:
                _lo_rows = [("LODP", w_lo)]
            else:
                # Runner on 1B (but not TP): LODP rate = 0, reallocated to K (CSV V59)
                _k_lo_bonus = w_lo

        # --- GO split (T48-T57 / V48-V57) ---
        _dp_base = clampf(0.5 - 0.1 * (batter_spd - 3))
        _dp_mult = 0.15  # E57: DP range multiplier for OBC 4

        def _gora_r() -> float:
            """Dynamic GORA rate. 0 when infield_in, outs==2, or no runners."""
            if infield_in or outs == 2 or not _has_runners:
                return 0.0
            if on_1b and not on_2b and not on_3b:       # 001
                return clampf(0.09 + 0.023 * ((s1 or 3) - 3))
            if not on_1b and on_2b and not on_3b:       # 010
                return clampf(0.25 + 0.05  * ((s2 or 3) - 3))
            if not on_1b and not on_2b and on_3b:       # 100
                return clampf(0.25 + 0.05  * ((s3 or 3) - 3))
            if on_1b and on_2b and not on_3b:            # 011
                return clampf(0.09 + 0.023 * (_avg(s1, s2) - 3))
            if on_1b and not on_2b and on_3b:            # 101
                return clampf(0.09 + 0.023 * (_avg(s1, s3) - 3))
            if not on_1b and on_2b and on_3b:            # 110
                return clampf(0.35 + 0.05  * (_avg(s2, s3) - 3))
            if on_1b and on_2b and on_3b:                # 111: equals OBC 5 formula
                return clampf(0.09 + 0.023 * (_avg(s1, s3) - 3))
            return 0.0

        gora_rate = _gora_r()
        w_gora = math.floor(gora_rate * w_go)

        _go_rows:   list[tuple[str, int]] = []
        _go_detail: list[tuple[str, str, int]] = []  # (name, rate_str, width) for debug
        if w_gora > 0:
            _go_rows.append(("GORA", w_gora))
            _go_detail.append(("GORA", f"{gora_rate:.4f}", w_gora))

        _go_rem = w_go - w_gora

        if not _has_runners or outs == 2:
            # No FC/DP/FCH without runners or with 2 outs
            if _go_rem > 0:
                _go_rows.append(("GO", _go_rem))

        elif on_1b and not on_2b and not on_3b:         # 001: GORA + FC + DP
            dp_r  = _dp_base
            fc_r  = clampf(1.0 - gora_rate - dp_r)
            w_fc  = math.floor(fc_r * w_go)
            w_dp  = max(0, _go_rem - w_fc)   # DP is last -> catch-all
            if w_fc > 0: _go_rows.append(("FC", w_fc))
            if w_dp > 0: _go_rows.append(("DP", w_dp))
            _go_detail += [("FC", f"{fc_r:.4f}", w_fc), ("DP", "catch", w_dp)]

        elif not on_1b and on_2b and not on_3b:         # 010: GORA + GO
            if _go_rem > 0: _go_rows.append(("GO", _go_rem))

        elif not on_1b and not on_2b and on_3b:         # 100: GORA + GO
            if _go_rem > 0: _go_rows.append(("GO", _go_rem))

        elif on_1b and on_2b and not on_3b:              # 011: GORA + FC + FC3rd + DP21 + DP31
            dp31_r  = clampf((_dp_base / 2) * (1 + _dp_mult))
            dp21_r  = dp31_r
            fc_half = clampf((1.0 - dp21_r - dp31_r - gora_rate) / 2)
            w_dp21  = math.floor(dp21_r  * w_go)
            w_fc    = math.floor(fc_half  * w_go)
            w_fc3rd = math.floor(fc_half  * w_go)
            # DP31 absorbs the floor-rounding remainder (CSV V55 catch-all)
            w_dp31  = max(0, w_go - w_gora - w_dp21 - w_fc - w_fc3rd)
            if w_fc    > 0: _go_rows.append(("FC",    w_fc))
            if w_fc3rd > 0: _go_rows.append(("FC3rd", w_fc3rd))
            if w_dp21  > 0: _go_rows.append(("DP21",  w_dp21))
            if w_dp31  > 0: _go_rows.append(("DP31",  w_dp31))
            _go_detail += [
                ("FC",    f"{fc_half:.4f}", w_fc),
                ("FC3rd", f"{fc_half:.4f}", w_fc3rd),
                ("DP21",  f"{dp21_r:.4f}",  w_dp21),
                ("DP31",  "catch",          w_dp31),
            ]

        elif on_1b and not on_2b and on_3b:              # 101: GORA + FC + DPRun + DP
            dp_5    = clampf((_dp_base / 2) * (1 + _dp_mult))
            dprun_5 = clampf((1.0 - gora_rate - 2 * dp_5) / 2) if outs == 0 else 0.0
            fc_5    = clampf(1.0 - gora_rate - dp_5 - dprun_5)
            w_dprun_5 = math.floor(dprun_5 * w_go)
            w_fc_5    = math.floor(fc_5    * w_go)
            # DP absorbs the floor-rounding remainder (G64 sheet: FC is a derived rate)
            w_dp_5    = max(0, _go_rem - w_fc_5 - w_dprun_5)
            if w_dprun_5 > 0: _go_rows.append(("DPRun", w_dprun_5))
            if w_fc_5    > 0: _go_rows.append(("FC",    w_fc_5))
            if w_dp_5    > 0: _go_rows.append(("DP",    w_dp_5))
            _go_detail += [
                ("DPRun", f"{dprun_5:.4f}", w_dprun_5),
                ("FC",    f"{fc_5:.4f}",    w_fc_5),
                ("DP",    "catch",          w_dp_5),
            ]

        elif not on_1b and on_2b and on_3b:              # 110: GORA + GO
            if _go_rem > 0: _go_rows.append(("GO", _go_rem))

        elif on_1b and on_2b and on_3b:                  # 111: GORA + FCH + DP21 + DP31 + DPH1
            _avg_spd_13 = _avg(s1, s3)
            fch_r  = clampf(0.15 + 0.025 * (_avg_spd_13 - 3))
            _rem7  = clampf(1.0 - fch_r - gora_rate)
            div_31 = 4 if infield_in else 3
            div_h1 = 2 if infield_in else 3
            dp31_r = math.floor(clampf(_rem7 / div_31) * 1000) / 1000
            dph1_r = math.floor(clampf(_rem7 / div_h1) * 1000) / 1000
            dp21_r = clampf(_rem7 - dp31_r - dph1_r)
            w_fch  = math.floor(fch_r  * w_go)
            w_dp31 = math.floor(dp31_r * w_go)
            w_dp21 = math.floor(dp21_r * w_go)
            # DPH1 is last in _SWING_ORDER -> catch-all
            w_dph1 = max(0, _go_rem - w_fch - w_dp21 - w_dp31)
            if w_fch  > 0: _go_rows.append(("FCH",  w_fch))
            if w_dp21 > 0: _go_rows.append(("DP21", w_dp21))
            if w_dp31 > 0: _go_rows.append(("DP31", w_dp31))
            if w_dph1 > 0: _go_rows.append(("DPH1", w_dph1))
            _go_detail += [
                ("FCH",  f"{fch_r:.4f}",  w_fch),
                ("DP21", f"{dp21_r:.4f}", w_dp21),
                ("DP31", f"{dp31_r:.4f}", w_dp31),
                ("DPH1", "catch",         w_dph1),
            ]

        else:
            # Runners present but OBC not recognized (e.g. runners_on=True, obc="000")
            if _go_rem > 0:
                _go_rows.append(("GO", _go_rem))

        _dbg.update({
            "mode": "hnr" if hit_and_run else ("ifin" if infield_in else "swing"),
            # FO detail
            "fo_rate": round(fo_rate, 4), "po_rate": round(po_rate, 4),
            "after_hits": after_hits, "w_fo": w_fo, "w_po": w_po,
            "dfo_pct": round(_dfo_pct, 4), "fo_rows": list(_fo_rows),
            # WH detail
            "s1": s1, "s2": s2, "s3": s3,
            "lead_spd": _lead_spd, "trail_spd": _trail_spd,
            "lead_wh": round(_lead_wh, 4),
            "trail_wh": round(_trail_wh, 4) if _trail_wh is not None else None,
            "2bwh_rate": round(_2bwh_rate, 4),
            "1bwh_rate": round(_1bwh_rate, 4), "1bwh2_rate": round(_1bwh2_rate, 4),
            "if1b_bonus": _if1b_bonus,
            "w_2bwh": w_2bwh, "w_1bwh": w_1bwh, "w_1bwh2": w_1bwh2, "w_1b_plain": w_1b_plain,
            # GO detail
            "dp_base": round(_dp_base, 4), "dp_mult": _dp_mult,
            "w_go": w_go, "gora_rate": round(gora_rate, 4), "w_gora": w_gora,
            "go_rows": list(_go_rows), "go_detail": list(_go_detail),
        })

        # Fixed stack order from calculator S column (S26:S63)
        # Order 1-9: hits/BB; 10: GORA; 11-14: FO group; 15: PO;
        # 16-20: GO group (non-DP); 21: K; 22-25: DP group; 30-32: LO
        _SWING_ORDER = [
            "HR","3B","2BWH","2B","1BWH","1BWH2","1B","IF1B","BB",
            "GORA",
            "DSacF","DFO","SacF","FO",
            "PO",
            "FCH","FC","GO","FC3rd","DPRun",
            "K",
            "DP","DP21","DP31","DPH1",
            "LODP","TP","LOTP",
        ]

        # Collect all widths into a dict
        _widths: dict[str, int] = {
            "HR": w_hr, "3B": w_3b,
            "2BWH": w_2bwh, "2B": w_2b_plain,
            "1BWH": w_1bwh, "1BWH2": w_1bwh2, "1B": w_1b_plain,
            "IF1B": (w_if1b if not (hit_and_run and infield_in) else 0),
            "BB": w_bb, "PO": w_po, "K": w_k + _k_lo_bonus,
        }
        for _name, _w in _fo_rows:
            _widths[_name] = _widths.get(_name, 0) + _w
        for _name, _w in _lo_rows:
            _widths[_name] = _widths.get(_name, 0) + _w
        for _name, _w in _go_rows:
            _widths[_name] = _widths.get(_name, 0) + _w

        rows = [(_n, _widths[_n]) for _n in _SWING_ORDER if _widths.get(_n, 0) > 0]

    result = []
    pos = 0
    for name, width in rows:
        if width <= 0:
            continue
        result.append({"result": name, "range": width, "low": pos, "high": pos + width - 1})
        pos += width
    if _debug:
        return result, _dbg
    return result


def range_bar_chart(ranges: list[dict], title: str = "") -> go.Figure:
    """Horizontal stacked bar showing each result's share of the 0-500 number line."""
    fig = go.Figure()
    for r in ranges:
        color = _result_color(r["result"]) or RESULT_COLORS.get(r["result"], "#888")
        fig.add_trace(go.Bar(
            x=[r["range"]],
            y=[""],
            orientation="h",
            marker_color=color,
            name=r["result"],
            text=r["result"] if r["range"] > 12 else "",
            textposition="inside",
            insidetextanchor="middle",
            hovertemplate=(
                f"<b>{r['result']}</b><br>"
                f"Range: {r['range']}<br>"
                f"{r['low']} – {r['high']}<extra></extra>"
            ),
            width=0.5,
        ))
    fig.update_layout(
        barmode="stack",
        title=dict(text=title, font=dict(size=13)) if title else None,
        height=110,
        margin=dict(l=5, r=5, t=30 if title else 5, b=5),
        showlegend=False,
        xaxis=dict(
            range=[0, 501],
            tickvals=[0, 100, 200, 300, 400, 500],
            tickfont=dict(size=10),
            title=dict(text="0 → 500", font=dict(size=10)),
        ),
        yaxis=dict(visible=False),
        dragmode=False,
        modebar_remove=["zoom2d","pan2d","select2d","lasso2d","zoomIn2d",
                        "zoomOut2d","autoScale2d","resetScale2d","toImage"],
    )
    return fig


_DIFF_HM_BINS   = [-1, 25, 50, 100, 150, 200, 300, 501]
_DIFF_HM_LABELS = ["0–25", "26–50", "51–100", "101–150", "151–200", "201–300", "301–500"]

_DELTA_HM_BINS   = list(range(0, 501, 100))         # [0, 100, 200, 300, 400, 500]
_DELTA_HM_LABELS = [f"{i}–{i + 100}" for i in range(0, 500, 100)]  # 5 bins


def _diff_centered_half(diff_val: int) -> int:
    """Half-width for centered diff window - half the width of the matching quality bucket."""
    bins = _DIFF_HM_BINS  # [-1, 25, 50, 100, 150, 200, 300, 501]
    for i in range(len(bins) - 1):
        left = 0 if i == 0 else bins[i] + 1
        hi   = bins[i + 1]
        if left <= diff_val <= hi:
            return max(1, (hi - left + 1) // 2)
    return 100


def diff_vs_next_pitch_delta_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch |Δ| vs Prior Diff",
    value_col: str = "pitch",
) -> go.Figure:
    """Heatmap: unsigned next-value delta vs previous play's diff.

    X = prior diff bin; Y = abs circular delta to next value (0 at bottom, 500 at top).
    Only consecutive values from the same player within the same game are counted.
    """
    df_sw = df[df[value_col].notna() & df["diff"].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_sw = df_sw.sort_values(["game_id", group_col, "id"])
    df_sw["_next_val"] = df_sw.groupby(["game_id", group_col])[value_col].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_val"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_next_delta"] = df_sw.apply(
        lambda r: circular_diff(int(r[value_col]), int(r["_next_val"])), axis=1
    )
    df_sw["_diff_cat"] = pd.cut(
        df_sw["diff"].astype(int),
        bins=_DIFF_HM_BINS, labels=_DIFF_HM_LABELS, right=True, include_lowest=True,
    )
    df_sw["_delta_cat"] = pd.cut(
        df_sw["_next_delta"],
        bins=_DELTA_HM_BINS, labels=_DELTA_HM_LABELS, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_delta_cat"], df_sw["_diff_cat"]).reindex(
        index=_DELTA_HM_LABELS, columns=_DIFF_HM_LABELS, fill_value=0
    )
    _col_n = ct.sum(axis=0)
    _row_n = ct.sum(axis=1)
    # Normalize each column to 0–100 % so colour reflects within-column distribution.
    col_totals = _col_n.replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw  = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(_DIFF_HM_LABELS))]
        for i in range(len(_DELTA_HM_LABELS))
    ]
    # Flat raw counts for hover (customdata)
    customdata = z_raw

    annotations = []
    for j, lbl in enumerate(_DIFF_HM_LABELS):
        annotations.append(dict(
            xref="x", yref="paper", x=lbl, y=1.0,
            text=f"{int(_col_n.iloc[j])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="center", yanchor="bottom",
        ))
    for i, lbl in enumerate(_DELTA_HM_LABELS):
        annotations.append(dict(
            xref="paper", yref="y", x=1.0, y=lbl,
            text=f"{int(_row_n.iloc[i])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="left", yanchor="middle",
        ))

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=_DIFF_HM_LABELS,
        y=_DELTA_HM_LABELS,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior diff: %{x}<br>Next pitch |Δ|: %{y}<br>%{z:.1f}% of column (%{customdata} pitches)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior diff (abs)"),
        yaxis=dict(title="Next pitch |Δ|", autorange=True),
        annotations=annotations,
        height=max(360, len(_DELTA_HM_LABELS) * 40 + 110),
        margin=dict(l=80, r=62, t=50, b=70),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def next_pitch_delta_vs_prior_result_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch |Δ| vs Prior Result",
    value_col: str = "pitch",
) -> go.Figure:
    """Heatmap: unsigned next-value delta vs the PREVIOUS plate appearance's
    result category (XBH / BB-1B / Out / K+, see seq_result_category).

    X = prior result category (fixed best->worst order); Y = abs circular delta
    to next value (0 at bottom, 500 at top). Only consecutive plate appearances
    from the same player within the same game are counted.
    """
    df_sw = df[df[value_col].notna() & df["result"].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_sw = df_sw.sort_values(["game_id", group_col, "id"])
    df_sw["_next_val"] = df_sw.groupby(["game_id", group_col])[value_col].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_val"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_next_delta"] = df_sw.apply(
        lambda r: circular_diff(int(r[value_col]), int(r["_next_val"])), axis=1
    )
    df_sw["_res_cat"] = pd.Categorical(
        df_sw["result"].map(seq_result_category), categories=SEQ_RESULT_CATEGORIES
    )
    df_sw["_delta_cat"] = pd.cut(
        df_sw["_next_delta"],
        bins=_DELTA_HM_BINS, labels=_DELTA_HM_LABELS, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_delta_cat"], df_sw["_res_cat"]).reindex(
        index=_DELTA_HM_LABELS, columns=SEQ_RESULT_CATEGORIES, fill_value=0
    )
    _col_n = ct.sum(axis=0)
    _row_n = ct.sum(axis=1)
    # Normalize each column to 0–100 % so colour reflects within-column distribution.
    col_totals = _col_n.replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw  = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(SEQ_RESULT_CATEGORIES))]
        for i in range(len(_DELTA_HM_LABELS))
    ]
    customdata = z_raw

    annotations = []
    for j, lbl in enumerate(SEQ_RESULT_CATEGORIES):
        annotations.append(dict(
            xref="x", yref="paper", x=lbl, y=1.0,
            text=f"{int(_col_n.iloc[j])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="center", yanchor="bottom",
        ))
    for i, lbl in enumerate(_DELTA_HM_LABELS):
        annotations.append(dict(
            xref="paper", yref="y", x=1.0, y=lbl,
            text=f"{int(_row_n.iloc[i])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="left", yanchor="middle",
        ))

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=SEQ_RESULT_CATEGORIES,
        y=_DELTA_HM_LABELS,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior result: %{x}<br>Next pitch |Δ|: %{y}<br>%{z:.1f}% of column (%{customdata} pitches)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior result"),
        yaxis=dict(title="Next pitch |Δ|", autorange=True),
        annotations=annotations,
        height=max(360, len(_DELTA_HM_LABELS) * 40 + 110),
        margin=dict(l=80, r=62, t=50, b=70),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def result_seq_delta_dist(df: pd.DataFrame, value_col: str,
                          prior_cat_older: str, prior_cat_newer: str) -> "go.Figure | None":
    """Single-row heatmap: distribution of the next |Δ| given the result
    categories of the two most recent plate appearances. Returns None when
    there is no data for the given category pair."""
    df_s = _result_seq_context(df, value_col)
    if df_s is None:
        return None
    subset = df_s[(df_s["_prev_cat"] == prior_cat_older) & (df_s["_cat"] == prior_cat_newer)]
    if subset.empty:
        return None
    counts = subset["_nc"].astype(int).value_counts().reindex(range(len(_DELTA_HM_LABELS)), fill_value=0)
    total = int(counts.sum())
    pcts = counts / total * 100 if total > 0 else counts * 0.0
    text = [[f"{pcts[i]:.0f}%" if counts[i] > 0 else "" for i in range(len(_DELTA_HM_LABELS))]]

    fig = go.Figure(go.Heatmap(
        z=[pcts.values.tolist()],
        x=_DELTA_HM_LABELS,
        y=["Next |Δ|"],
        text=text,
        texttemplate="%{text}",
        customdata=[counts.values.tolist()],
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="%{x}<br>%{z:.1f}% (%{customdata} instances)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(
            text=f"Next |Δ|  |  {prior_cat_older} → {prior_cat_newer}  (n={total})",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        xaxis=dict(title=None, side="bottom"),
        yaxis=dict(showticklabels=True),
        height=130,
        margin=dict(l=80, r=10, t=55, b=40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def next_delta_vs_prior_delta_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch Δ vs Prior Pitch Δ",
    value_col: str = "pitch",
    bucket_size: int = 50,
) -> go.Figure:
    """Heatmap: next delta vs prior delta for consecutive plays.

    Shows how pitcher/batter adjusts their next movement based on their previous movement.
    X = prior pitch/swing delta bin; Y = next pitch/swing delta bin.
    bucket_size must divide 500 evenly.
    """
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"

    bins = list(range(0, 501, bucket_size))
    labels = [f"{i}-{i + bucket_size}" for i in range(0, 500, bucket_size)]

    df_sw = df[df[value_col].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    df_sw = df_sw.sort_values(["game_id", group_col, "id"])

    # Always recalculate deltas fresh to ensure proper grouping for filtered data
    df_sw[delta_col] = df_sw.groupby(["game_id", group_col], group_keys=False)[value_col].apply(_circ_delta_group)

    df_sw = df_sw[df_sw[delta_col].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    df_sw["_next_delta"] = df_sw.groupby(["game_id", group_col])[delta_col].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_delta"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_prior_delta_cat"] = pd.cut(
        df_sw[delta_col].abs().astype(int),
        bins=bins, labels=labels, right=True, include_lowest=True,
    )
    df_sw["_next_delta_cat"] = pd.cut(
        df_sw["_next_delta"].abs().astype(int),
        bins=bins, labels=labels, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_next_delta_cat"], df_sw["_prior_delta_cat"]).reindex(
        index=labels, columns=labels, fill_value=0
    )
    _col_n = ct.sum(axis=0)
    _row_n = ct.sum(axis=1)
    col_totals = _col_n.replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(labels))]
        for i in range(len(labels))
    ]
    customdata = z_raw

    annotations = []
    for j, lbl in enumerate(labels):
        annotations.append(dict(
            xref="x", yref="paper", x=lbl, y=1.0,
            text=f"{int(_col_n.get(lbl, 0))}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="center", yanchor="bottom",
        ))
    for i, lbl in enumerate(labels):
        annotations.append(dict(
            xref="paper", yref="y", x=1.0, y=lbl,
            text=f"{int(_row_n.get(lbl, 0))}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="left", yanchor="middle",
        ))

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=labels,
        y=labels,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior |Δ|: %{x}<br>Next |Δ|: %{y}<br>%{z:.1f}% of column (%{customdata} instances)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior |Δ|"),
        yaxis=dict(title="Next |Δ|"),
        annotations=annotations,
        height=max(360, len(labels) * 40 + 110),
        margin=dict(l=80, r=62, t=68, b=70),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def _fresh_delta2_frame(df: pd.DataFrame, value_col: str = "pitch"):
    """Two-stage fresh recompute of |Δ²| for a filtered frame slice.

    Mirrors enrich_df's discipline (utils.py:1000-1005): stage 1 recomputes the signed
    circular delta per game+pitcher (NaN at each game's first pitch); stage 2 differences
    those signed deltas, wraps the result into (-500, 500] via `_wrap_delta2`, and takes
    the magnitude (NaN at each game's first two pitches, since diff of a leading-NaN
    series leaves the first two positions NaN). Because the groupby is per game+pitcher,
    no extra game-boundary guard is needed.

    Returns (df_sw, group_col) where df_sw has a "_d2" column holding |Δ²|, or None when
    fewer than 3 usable value rows remain (a Δ² needs at least 3 same-game pitches).
    """
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"

    df_sw = df[df[value_col].notna()].sort_values(["game_id", group_col, "id"]).copy()
    if len(df_sw) < 3:
        return None
    df_sw[delta_col] = df_sw.groupby(
        ["game_id", group_col], group_keys=False
    )[value_col].apply(_circ_delta_group)
    df_sw["_d2"] = df_sw.groupby(
        ["game_id", group_col], group_keys=False
    )[delta_col].apply(lambda g: _wrap_delta2(g.diff()).abs())
    if df_sw["_d2"].notna().sum() < 1:
        return None
    return df_sw, group_col


def next_delta2_vs_prior_delta2_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch Δ² vs Prior Pitch Δ²",
    value_col: str = "pitch",
    bucket_size: int = 50,
) -> go.Figure:
    """Heatmap: next |Δ²| vs prior |Δ²| for consecutive plays.

    Δ² is the unsigned change between consecutive |Δ| values, recomputed fresh per
    game+pitcher (two-stage, see _fresh_delta2_frame). X = prior |Δ²| bin; Y = next
    |Δ²| bin. bucket_size must divide 500 evenly.
    """
    bins = list(range(0, 501, bucket_size))
    labels = [f"{i}-{i + bucket_size}" for i in range(0, 500, bucket_size)]

    _fresh = _fresh_delta2_frame(df, value_col)
    if _fresh is None:
        return go.Figure()
    df_sw, group_col = _fresh

    df_sw = df_sw[df_sw["_d2"].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    df_sw["_next_d2"] = df_sw.groupby(["game_id", group_col])["_d2"].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_d2"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_prior_d2_cat"] = pd.cut(
        df_sw["_d2"].astype(int),
        bins=bins, labels=labels, right=True, include_lowest=True,
    )
    df_sw["_next_d2_cat"] = pd.cut(
        df_sw["_next_d2"].astype(int),
        bins=bins, labels=labels, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_next_d2_cat"], df_sw["_prior_d2_cat"]).reindex(
        index=labels, columns=labels, fill_value=0
    )
    _col_n = ct.sum(axis=0)
    _row_n = ct.sum(axis=1)
    col_totals = _col_n.replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(labels))]
        for i in range(len(labels))
    ]
    customdata = z_raw

    annotations = []
    for j, lbl in enumerate(labels):
        annotations.append(dict(
            xref="x", yref="paper", x=lbl, y=1.0,
            text=f"{int(_col_n.get(lbl, 0))}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="center", yanchor="bottom",
        ))
    for i, lbl in enumerate(labels):
        annotations.append(dict(
            xref="paper", yref="y", x=1.0, y=lbl,
            text=f"{int(_row_n.get(lbl, 0))}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="left", yanchor="middle",
        ))

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=labels,
        y=labels,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior |Δ²|: %{x}<br>Next |Δ²|: %{y}<br>%{z:.1f}% of column (%{customdata} instances)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior |Δ²|"),
        yaxis=dict(title="Next |Δ²|"),
        annotations=annotations,
        height=max(360, len(labels) * 40 + 110),
        margin=dict(l=80, r=62, t=68, b=70),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def delta2_third_dist(
    df: pd.DataFrame,
    value_col: str = "pitch",
    bucket_size: int = 50,
    init_label: str = "",
    follow_label: str = "",
) -> go.Figure | None:
    """Single-row heatmap: distribution of the 3rd |Δ²| given init->follow |Δ²| pair.

    Δ² recomputed fresh per game+pitcher (two-stage, see _fresh_delta2_frame). A 3-long
    Δ² chain needs 5 same-game pitches. Returns None when there is no data for the given
    label pair.
    """
    n_buckets = 500 // bucket_size
    bins   = list(range(0, 501, bucket_size))
    labels = [f"{i}-{i + bucket_size}" for i in range(0, 500, bucket_size)]

    if init_label not in labels or follow_label not in labels:
        return None

    init_idx   = labels.index(init_label)
    follow_idx = labels.index(follow_label)

    _fresh = _fresh_delta2_frame(df, value_col)
    if _fresh is None:
        return None
    df_sw, group_col = _fresh
    df_sw = df_sw[df_sw["_d2"].notna()].copy()
    if len(df_sw) < 3:
        return None

    df_sw["_d1v"] = df_sw["_d2"]
    df_sw["_d2v"] = df_sw.groupby(["game_id", group_col])["_d2"].shift(-1)
    df_sw["_d3v"] = df_sw.groupby(["game_id", group_col])["_d2"].shift(-2)
    df_sw = df_sw.dropna(subset=["_d2v", "_d3v"]).copy()
    if df_sw.empty:
        return None

    df_sw["_b1"] = pd.cut(df_sw["_d1v"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    df_sw["_b2"] = pd.cut(df_sw["_d2v"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    df_sw["_b3"] = pd.cut(df_sw["_d3v"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)

    subset = df_sw[(df_sw["_b1"] == init_idx) & (df_sw["_b2"] == follow_idx)]
    if subset.empty:
        return None

    counts = subset["_b3"].value_counts().reindex(range(n_buckets), fill_value=0)
    total  = int(counts.sum())
    pcts   = counts / total * 100 if total > 0 else counts * 0.0

    text = [[f"{pcts[i]:.0f}%" if counts[i] > 0 else "" for i in range(n_buckets)]]

    fig = go.Figure(go.Heatmap(
        z=[pcts.values.tolist()],
        x=labels,
        y=["3rd |Δ²|"],
        text=text,
        texttemplate="%{text}",
        customdata=[counts.values.tolist()],
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="%{x}<br>%{z:.1f}% (%{customdata} instances)<extra></extra>",
    ))
    _rotate = n_buckets > 8
    fig.update_layout(
        title=dict(
            text=f"3rd |Δ²|  |  {init_label} → {follow_label}  (n={total})",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        xaxis=dict(title=None, side="bottom", tickangle=-90 if _rotate else 0),
        yaxis=dict(showticklabels=True),
        height=165 if _rotate else 130,
        margin=dict(l=80, r=10, t=55, b=75 if _rotate else 40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def hot_zone_matrix(
    df: pd.DataFrame,
    value_col: str = "pitch",
    group_cols: list[str] | None = None,
    title: str = "Hot Zone Pitch Matrix",
    init_bucket_size: int = 100,
    follow_bucket_size: int = 100,
) -> go.Figure:
    """Heatmap of consecutive pitch/swing zone transitions.
    Initial pitch on x-axis (bottom); following pitch on y-axis (left).
    Both bucket sizes must divide 1000 evenly.
    """
    if group_cols is None:
        group_cols = ["game_id", "pitcher_name"]

    n_init   = 1000 // init_bucket_size
    n_follow = 1000 // follow_bucket_size
    init_labels   = [f"{i * init_bucket_size + 1}-{min((i + 1) * init_bucket_size, 1000)}"     for i in range(n_init)]
    follow_labels = [f"{i * follow_bucket_size + 1}-{min((i + 1) * follow_bucket_size, 1000)}" for i in range(n_follow)]

    df = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    df["_next"] = df.groupby(group_cols)[value_col].shift(-1)
    df = df.dropna(subset=["_next"])

    df["_curr_b"] = ((df[value_col].astype(int) - 1) // init_bucket_size).clip(0, n_init - 1)
    df["_next_b"] = ((df["_next"].astype(int)   - 1) // follow_bucket_size).clip(0, n_follow - 1)

    # rows = following bucket, cols = initial bucket
    matrix = (
        pd.crosstab(df["_next_b"], df["_curr_b"])
        .reindex(index=range(n_follow), columns=range(n_init), fill_value=0)
    )

    # Normalize by column so each initial-pitch column sums to 100%
    _col_n  = matrix.sum(axis=0)
    _row_n  = matrix.sum(axis=1)
    col_totals  = _col_n.replace(0, 1)
    matrix_norm = matrix.div(col_totals, axis=1) * 100
    z_norm = matrix_norm.values.tolist()
    z_raw  = matrix.values.tolist()
    text = [
        [f"{matrix_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(n_init)]
        for i in range(n_follow)
    ]

    annotations = []
    for j, lbl in enumerate(init_labels):
        annotations.append(dict(
            xref="x", yref="paper", x=lbl, y=1.0,
            text=f"{int(_col_n.iloc[j])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="center", yanchor="bottom",
        ))
    for i, lbl in enumerate(follow_labels):
        annotations.append(dict(
            xref="paper", yref="y", x=1.0, y=lbl,
            text=f"{int(_row_n.iloc[i])}",
            showarrow=False,
            font=dict(size=11, color="rgba(255,255,255,0.9)"),
            xanchor="left", yanchor="middle",
        ))

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=init_labels,
        y=follow_labels,
        text=text,
        texttemplate="%{text}",
        customdata=z_raw,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Initial %{x} → Following %{y}<br>%{z:.1f}% of col (%{customdata} pitches)<extra></extra>",
    ))
    fig.update_layout(
        xaxis=dict(title="Initial Pitch", side="bottom"),
        yaxis=dict(title="Following Pitch"),
        annotations=annotations,
        height=max(400, n_follow * 42 + 120),
        margin=dict(l=90, r=65, t=50, b=80),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def hot_zone_third_dist(
    df: pd.DataFrame,
    value_col: str = "pitch",
    group_cols: list[str] | None = None,
    init_bucket_size: int = 100,
    follow_bucket_size: int = 100,
    third_bucket_size: int | None = None,
    init_label: str = "",
    follow_label: str = "",
) -> go.Figure | None:
    """Single-row heatmap showing the 3rd pitch distribution given an initial->following pair.

    Returns None if the label pair isn't found or there's no data for the sequence.
    """
    if group_cols is None:
        group_cols = ["game_id", "pitcher_name"]
    if third_bucket_size is None:
        third_bucket_size = follow_bucket_size

    n_init   = 1000 // init_bucket_size
    n_follow = 1000 // follow_bucket_size
    n_third  = 1000 // third_bucket_size
    init_labels   = [f"{i * init_bucket_size + 1}-{min((i + 1) * init_bucket_size, 1000)}"   for i in range(n_init)]
    follow_labels = [f"{i * follow_bucket_size + 1}-{min((i + 1) * follow_bucket_size, 1000)}" for i in range(n_follow)]
    third_labels  = [f"{i * third_bucket_size + 1}-{min((i + 1) * third_bucket_size, 1000)}"  for i in range(n_third)]

    if init_label not in init_labels or follow_label not in follow_labels:
        return None

    init_idx   = init_labels.index(init_label)
    follow_idx = follow_labels.index(follow_label)

    df = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    df["_next"]  = df.groupby(group_cols)[value_col].shift(-1)
    df["_next2"] = df.groupby(group_cols)[value_col].shift(-2)
    df = df.dropna(subset=["_next", "_next2"])

    df["_curr_b"]  = ((df[value_col].astype(int) - 1) // init_bucket_size).clip(0, n_init - 1)
    df["_next_b"]  = ((df["_next"].astype(int)   - 1) // follow_bucket_size).clip(0, n_follow - 1)
    df["_next2_b"] = ((df["_next2"].astype(int)  - 1) // third_bucket_size).clip(0, n_third - 1)

    subset = df[(df["_curr_b"] == init_idx) & (df["_next_b"] == follow_idx)]
    if subset.empty:
        return None

    counts = subset["_next2_b"].value_counts().reindex(range(n_third), fill_value=0)
    total  = int(counts.sum())
    pcts   = (counts / total * 100)

    text = [[f"{pcts[i]:.0f}%" if counts[i] > 0 else "" for i in range(n_third)]]

    fig = go.Figure(go.Heatmap(
        z=[pcts.values.tolist()],
        x=third_labels,
        y=["3rd Pitch"],
        text=text,
        texttemplate="%{text}",
        customdata=[counts.values.tolist()],
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="%{x}<br>%{z:.1f}% (%{customdata} pitches)<extra></extra>",
    ))
    _rotate = n_third > 8
    fig.update_layout(
        title=dict(
            text=f"3rd Pitch  |  {init_label} -> {follow_label}  (n={total})",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        xaxis=dict(title=None, side="bottom", tickangle=-90 if _rotate else 0),
        yaxis=dict(showticklabels=True),
        height=165 if _rotate else 130,
        margin=dict(l=80, r=10, t=60, b=75 if _rotate else 40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def delta_third_dist(
    df: pd.DataFrame,
    value_col: str = "pitch",
    bucket_size: int = 50,
    init_label: str = "",
    follow_label: str = "",
) -> go.Figure | None:
    """Single-row heatmap: distribution of the 3rd |delta| given init->follow |delta| pair.

    Mirrors the bucketing used by next_delta_vs_prior_delta_heatmap (unsigned, 0..500).
    Returns None when there is no data for the given label pair.
    """
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"

    n_buckets = 500 // bucket_size
    bins   = list(range(0, 501, bucket_size))
    labels = [f"{i}-{i + bucket_size}" for i in range(0, 500, bucket_size)]

    if init_label not in labels or follow_label not in labels:
        return None

    init_idx   = labels.index(init_label)
    follow_idx = labels.index(follow_label)

    df_sw = df[df[value_col].notna()].copy()
    if len(df_sw) < 3:
        return None

    df_sw = df_sw.sort_values(["game_id", group_col, "id"])
    df_sw[delta_col] = df_sw.groupby(
        ["game_id", group_col], group_keys=False
    )[value_col].apply(_circ_delta_group)
    df_sw = df_sw[df_sw[delta_col].notna()].copy()
    if len(df_sw) < 3:
        return None

    df_sw["_d1_abs"] = df_sw[delta_col].abs()
    df_sw["_d2_abs"] = df_sw.groupby(["game_id", group_col])[delta_col].shift(-1).abs()
    df_sw["_d3_abs"] = df_sw.groupby(["game_id", group_col])[delta_col].shift(-2).abs()
    df_sw = df_sw.dropna(subset=["_d2_abs", "_d3_abs"]).copy()
    if df_sw.empty:
        return None

    df_sw["_b1"] = pd.cut(df_sw["_d1_abs"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    df_sw["_b2"] = pd.cut(df_sw["_d2_abs"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)
    df_sw["_b3"] = pd.cut(df_sw["_d3_abs"].astype(int), bins=bins, labels=False, right=True, include_lowest=True)

    subset = df_sw[(df_sw["_b1"] == init_idx) & (df_sw["_b2"] == follow_idx)]
    if subset.empty:
        return None

    counts = subset["_b3"].value_counts().reindex(range(n_buckets), fill_value=0)
    total  = int(counts.sum())
    pcts   = counts / total * 100 if total > 0 else counts * 0.0

    text = [[f"{pcts[i]:.0f}%" if counts[i] > 0 else "" for i in range(n_buckets)]]

    fig = go.Figure(go.Heatmap(
        z=[pcts.values.tolist()],
        x=labels,
        y=["3rd |Δ|"],
        text=text,
        texttemplate="%{text}",
        customdata=[counts.values.tolist()],
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="%{x}<br>%{z:.1f}% (%{customdata} instances)<extra></extra>",
    ))
    _rotate = n_buckets > 8
    fig.update_layout(
        title=dict(
            text=f"3rd |Δ|  |  {init_label} → {follow_label}  (n={total})",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        xaxis=dict(title=None, side="bottom", tickangle=-90 if _rotate else 0),
        yaxis=dict(showticklabels=True),
        height=165 if _rotate else 130,
        margin=dict(l=80, r=10, t=55, b=75 if _rotate else 40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ------------------------------------------------------------------ sheet import helpers

def _safe_int(val) -> int | None:
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def parse_inning(inning_str: str) -> tuple[int, str]:
    """Parse 'T1' → (1, 'top'), 'B3' → (3, 'bottom')."""
    s = str(inning_str).strip().upper()
    if s.startswith("T"):
        try:
            return int(s[1:]), "top"
        except ValueError:
            pass
    if s.startswith("B"):
        try:
            return int(s[1:]), "bottom"
        except ValueError:
            pass
    try:
        return int(s), "top"
    except ValueError:
        return 1, "top"


def _str(val) -> str:
    """Return a clean string or None for blank/nan values."""
    s = str(val).strip() if val is not None else ""
    return s if s and s.lower() != "nan" else None


def read_games_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Games' tab of a public Google Sheet and return a list of game dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Games')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    games = []
    for _, row in df.iterrows():
        # "Game#" column holds the 6-digit code (e.g. 130101); "GameID" is the short code (e.g. JJCC01)
        game_code = _str(row.get("Game#"))
        if not game_code or len(game_code) < 4:
            continue
        try:
            season = int(game_code[:2])
            session_num = int(game_code[2:4])
        except ValueError:
            continue
        away_abbrev = _str(row.get("Away")) or ""
        home_abbrev = _str(row.get("Home")) or ""

        hms = [
            _str(row.get(col))
            for col in ("Honorable Mention", "Honorable Mention.1", "Honorable Mention.2")
        ]

        games.append({
            "game_code": game_code,
            "game_id_short": _str(row.get("GameID")),
            "season": season,
            "session_number": session_num,
            "away_team": TEAM_ABBREV.get(away_abbrev, away_abbrev),
            "home_team": TEAM_ABBREV.get(home_abbrev, home_abbrev),
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "umpire": _str(row.get("Umpire Assignment")),
            "winning_pitcher": _str(row.get("Winning Pitcher")),
            "losing_pitcher": _str(row.get("Losing Pitcher")),
            "save_pitcher": _str(row.get("Save")),
            "hold_1": _str(row.get("Hold")),
            "hold_2": _str(row.get("Hold.1")),
            "player_of_game": _str(row.get("Player of the Game")),
            "honorable_mention_1": hms[0],
            "honorable_mention_2": hms[1],
            "honorable_mention_3": hms[2],
            "start_time": _str(row.get("Start")),
            "end_time": _str(row.get("End")),
            "last_play": _str(row.get("Last Play")),
            "last_inning": _str(row.get("Inning")),
            "last_result": _str(row.get("Last Result")),
            "win_team": _str(row.get("Win")),
            "loss_team": _str(row.get("Loss")),
            "league": _str(row.get("League")),
            "division": _str(row.get("Division")),
            "archive_sheet_id": _str(row.get("Archive Sheet ID")),
        })
    return games


def read_plays_from_sheet(sheet_id: str, tab: str = "Plays (Raw)") -> list[dict]:
    """Read a plays tab from a public Google Sheet and return a list of play dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    plays = []
    for _, row in df.iterrows():
        play_num = _safe_int(row.get("Play"))
        game_code = _str(row.get("Game"))
        if not play_num or not game_code:
            continue

        inning_raw = _str(row.get("Inning")) or "T1"
        inning_num, half = parse_inning(inning_raw)

        play_type = _str(row.get("PlayType"))
        result = _str(row.get("Result"))
        pitch = _safe_int(row.get("Pitch"))
        swing = _safe_int(row.get("Swing"))

        is_steal = (play_type or "").lower() == "steal"
        if not result:
            continue
        if not is_steal and (pitch is None or swing is None):
            continue

        # OBC from runner fields ("-" means empty base)
        on_first  = _str(row.get("OnFirst"))  or "-"
        on_second = _str(row.get("OnSecond")) or "-"
        on_third  = _str(row.get("OnThird"))  or "-"
        brc = (
            (1 if on_first  != "-" else 0)
            | (2 if on_second != "-" else 0)
            | (4 if on_third  != "-" else 0)
        )
        obc = BRC_TO_OBC.get(brc, "Empty")

        plays.append({
            "game_code":  game_code,
            "play_num":   play_num,
            "timestamp":  _str(row.get("Timestamp")),
            "umpire":     _str(row.get("Umpire")),
            "away":       _str(row.get("Away")),
            "home":       _str(row.get("Home")),
            "inning_raw": inning_raw,
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "play_type":  play_type,
            "result":     result,
            "play_code":  _str(row.get("Playcode")),
            "pitcher_id": _safe_int(row.get("Pitcher")),
            "catcher_id": _safe_int(row.get("Catcher")),
            "pos":        _str(row.get("Pos")),
            "batter_id":  _safe_int(row.get("Batter")),
            "on_first":   on_first,
            "on_second":  on_second,
            "on_third":   on_third,
            "scored2":    _str(row.get("scored2")),
            "scored3":    _str(row.get("scored3")),
            "scored4":    _str(row.get("scored4")),
            "er1":        _str(row.get("er1")),
            "er2":        _str(row.get("er2")),
            "er3":        _str(row.get("er3")),
            "er4":        _str(row.get("er4")),
            "pitch":      pitch,
            "swing":      swing,
            "throw_num":  _safe_int(row.get("Throw")),
            "runner_id":  _safe_int(row.get("Runner")),
            "steal_num":  _safe_int(row.get("Steal")),
            # app-computed (half, obc here; rest filled in _sync_plays)
            "inning": inning_num,
            "half":   half,
            "obc":    obc,
        })
    return plays


_LOGO_MANIFEST_CACHE: dict[str, str] | None = None


def _local_logo_url(raw_url: str) -> str:
    """Swap a sheet-provided logo URL for the locally-committed copy, if one
    has already been downloaded (docs/img/logos/manifest.json, built by a
    one-off fetch - see the "download team logos into the repo" work). Falls
    back to the raw external URL untouched for a logo that manifest doesn't
    know about yet (a brand-new team/rebrand), so a new logo still renders
    - it just won't get the same-origin benefit until the manifest is
    refreshed by re-running that fetch.

    Same-origin matters here because every one of these renders on the live
    grid at once (up to 16 logos for an 8-game session) - a third-party host
    caps concurrent connections per origin, which was serializing that
    burst into a visibly sequential load; GitHub Pages serves over HTTP/2,
    which has no such cap. """
    global _LOGO_MANIFEST_CACHE
    if not raw_url:
        return raw_url
    if _LOGO_MANIFEST_CACHE is None:
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "img", "logos", "manifest.json")
        try:
            with open(path, encoding="utf-8") as f:
                _LOGO_MANIFEST_CACHE = json.load(f)
        except (OSError, ValueError):
            _LOGO_MANIFEST_CACHE = {}
    return _LOGO_MANIFEST_CACHE.get(raw_url, raw_url)


def read_teams_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Teams' tab and return a list of team dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    teams = []
    for _, row in df.iterrows():
        abbrev = _str(row.get("Abv"))
        if not abbrev:
            continue
        teams.append({
            "s_team":    abbrev,
            "team_id":   _str(row.get("Team ID")),
            "abbrev":    abbrev,
            "location":  _str(row.get("Location")),
            "team_name": _str(row.get("Team Name")),
            "role_id":   _str(row.get("Role ID")),
            "hype_id":   _str(row.get("Hype ID")),
            "league":    _str(row.get("League")),
            "division":  _str(row.get("Division")),
            "logo_url":  _local_logo_url(_str(row.get("Logo URL"))),
            "name":      _str(row.get("Full Team")) or abbrev,
            "stadium":   _str(row.get("Stadium")),
            "primary_hex":  _str(row.get("Primary Hex")),
            "ballpark_url": _str(row.get("Ballpark URL")),
            "wins":         _safe_int(row.get("W")),
            "losses":       _safe_int(row.get("L")),
            "runs_scored":  _safe_int(row.get("RS")),
            "runs_allowed": _safe_int(row.get("RA")),
        })
    return teams


def _parse_bool(val) -> bool | None:
    s = str(val).strip().lower() if val is not None else ""
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def read_players_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Players' tab by column position (headers are mostly blank)."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Players')}"
    )
    df = pd.read_csv(url, dtype=str, header=0)

    # Columns by position: ID, Team, GM, Gov Name, Last Name, Discord ID, Status,
    # Primary, Secondary, Hand, CON, EYE, PWR, SPD, MOV, CMD, VEL, AWR,
    # Discord Nickname, Rookie?
    _col = lambda i: df.iloc[:, i] if i < len(df.columns) else pd.Series([None] * len(df))

    df2 = pd.DataFrame({
        "player_id":       _col(0),
        "team":            _col(1),
        "gm":              _col(2),
        "name":            _col(3),
        "last_name":       _col(4),
        "discord_id":      _col(5),
        "status":          _col(6),
        "primary_pos":     _col(7),
        "secondary_pos":   _col(8),
        "hand":            _col(9),
        "con":             _col(10),
        "eye":             _col(11),
        "pwr":             _col(12),
        "spd":             _col(13),
        "mov":             _col(14),
        "cmd":             _col(15),
        "vel":             _col(16),
        "awr":             _col(17),
        "discord_nickname": _col(18),
        "is_rookie":       _col(19),
    })

    players = []
    for _, row in df2.iterrows():
        player_id = _safe_int(row["player_id"])
        name = _str(row["name"])
        team = _str(row["team"])
        if not player_id or not name or not team:
            continue
        players.append({
            "player_id":       player_id,
            "s_id":            f"R_{player_id}",  # stable per-human row key (one RLN row per player)
            "team":            team,
            "gm":              _parse_bool(row["gm"]),
            "name":            name,
            "last_name":       _str(row["last_name"]),
            "discord_id":      _str(row["discord_id"]),
            "status":          _str(row["status"]),
            "primary_pos":     _str(row["primary_pos"]),
            "secondary_pos":   _str(row["secondary_pos"]),
            "hand":            _str(row["hand"]),
            "con":             _safe_int(row["con"]),
            "eye":             _safe_int(row["eye"]),
            "pwr":             _safe_int(row["pwr"]),
            "spd":             _safe_int(row["spd"]),
            "mov":             _safe_int(row["mov"]),
            "cmd":             _safe_int(row["cmd"]),
            "vel":             _safe_int(row["vel"]),
            "awr":             _safe_int(row["awr"]),
            "discord_nickname": _str(row["discord_nickname"]),
            "is_rookie":       _parse_bool(row["is_rookie"]),
        })
    return players


def read_mln_teams_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Teams' tab from an MLN archive sheet."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    teams = []
    for _, row in df.iterrows():
        s_team = _str(row.get("S_Team")) or _str(row.get("Abv"))
        if not s_team:
            continue
        teams.append({
            "league":              "MLN",
            "s_team":              s_team,
            "abbrev":              _str(row.get("Abv")),
            "season":              _safe_int(row.get("Season")),
            "sub_league":          _str(row.get("League")),
            "division":            _str(row.get("Division")),
            "team_id":             _str(row.get("Team ID")),
            "location":            _str(row.get("Location")),
            "team_name":           _str(row.get("Team Name")),
            "full_team":           _str(row.get("Full Team")),
            "name":                _str(row.get("Full Team")),
            "stadium":             _str(row.get("Stadium")),
            "primary_hex":         _str(row.get("Primary Hex")),
            "logo_url":            _local_logo_url(_str(row.get("Postimg Logo")) or _str(row.get("Logo URL"))),
            "role_id":             _str(row.get("Role ID")),
            "hype_id":             _str(row.get("Hype ID")),
            "wins":                _safe_int(row.get("W")),
            "losses":              _safe_int(row.get("L")),
            "runs_scored":         _safe_int(row.get("RS")),
            "runs_allowed":        _safe_int(row.get("RA")),
            "ballpark_url":        _str(row.get("Ballpark URL")),
            "ballpark_channel_id": _str(row.get("Ballpark Channel ID")),
            "real_logo":           _str(row.get("Real Logo")),
            "toos":                _str(row.get("ToOS")),
            "ballpark_sheet_id":   _str(row.get("Ballpark SheetID")),
        })
    return teams


def read_mln_players_from_sheet(sheet_id: str, tab: str = "Rosters", season: int | None = None) -> list[dict]:
    """Read a players/rosters tab from an MLN sheet.

    Archive ('Rosters') has named columns (S_ID, Full Player Name, Season, ...).
    Current season ('Players') follows the RLN positional format; pass season= to build s_id.
    """
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"
    )
    df = pd.read_csv(url, dtype=str, header=0)
    df.columns = [c.strip() for c in df.columns]

    # Archive format: named columns with S_ID present
    if "S_ID" in df.columns:
        players = []
        for _, row in df.iterrows():
            s_id = _str(row.get("S_ID"))
            name = _str(row.get("Full Player Name"))
            if not s_id or not name:
                continue
            players.append({
                "league":           "MLN",
                "s_id":             s_id,
                "player_id":        _safe_int(row.get("ID")),  # shared cross-season human id
                "season":           _safe_int(row.get("Season")),
                "name":             name,
                "first_name":       _str(row.get("First Name")),
                "last_name":        _str(row.get("Last Name")),
                "suffix":           _str(row.get("Suffix")),
                "discord_id":       _str(row.get("Discord ID*")),
                "discord_nickname": _str(row.get("Discord Nickname*")),
                "team":             _str(row.get("Team")),
                "gm":               _parse_bool(row.get("GM")),
                "status":           _str(row.get("Status*")),
                "session_added":    _str(row.get("Session*")),
                "primary_pos":      _str(row.get("Primary")),
                "secondary_pos":    _str(row.get("Secondary")),
                "hand":             _str(row.get("HAND")),
                "con":              _safe_int(row.get("CON")),
                "eye":              _safe_int(row.get("EYE")),
                "pwr":              _safe_int(row.get("PWR")),
                "spd":              _safe_int(row.get("SPD")),
                "mov":              _safe_int(row.get("MOV")),
                "cmd":              _safe_int(row.get("CMD")),
                "vel":              _safe_int(row.get("VEL")),
                "awr":              _safe_int(row.get("AWR")),
                "is_rookie":        _parse_bool(row.get("Rookie?")),
            })
        return players

    # RLN positional format: col 0=ID, 1=Team, 2=GM, 3=Name, 4=Last, 5=Discord ID,
    # 6=Status, 7=Primary, 8=Secondary, 9=Hand, 10=CON, 11=EYE, 12=PWR, 13=SPD,
    # 14=MOV, 15=CMD, 16=VEL, 17=AWR, 18=Discord Nickname, 19=Rookie?
    _col = lambda i: df.iloc[:, i] if i < len(df.columns) else pd.Series([None] * len(df))
    players = []
    for _, row in df.iterrows():
        player_id = _safe_int(_col(0)[row.name])
        name = _str(_col(3)[row.name])
        team = _str(_col(1)[row.name])
        if not player_id or not name or not team:
            continue
        s_id = f"{season}_{player_id}" if season is not None else str(player_id)
        players.append({
            "league":           "MLN",
            "s_id":             s_id,
            "player_id":        player_id,  # col 0 ID; shared cross-season human id
            "season":           season,
            "name":             name,
            "last_name":        _str(_col(4)[row.name]),
            "discord_id":       _str(_col(5)[row.name]),
            "discord_nickname": _str(_col(18)[row.name]),
            "team":             team,
            "gm":               _parse_bool(_col(2)[row.name]),
            "status":           _str(_col(6)[row.name]),
            "primary_pos":      _str(_col(7)[row.name]),
            "secondary_pos":    _str(_col(8)[row.name]),
            "hand":             _str(_col(9)[row.name]),
            "con":              _safe_int(_col(10)[row.name]),
            "eye":              _safe_int(_col(11)[row.name]),
            "pwr":              _safe_int(_col(12)[row.name]),
            "spd":              _safe_int(_col(13)[row.name]),
            "mov":              _safe_int(_col(14)[row.name]),
            "cmd":              _safe_int(_col(15)[row.name]),
            "vel":              _safe_int(_col(16)[row.name]),
            "awr":              _safe_int(_col(17)[row.name]),
            "is_rookie":        _parse_bool(_col(19)[row.name]),
        })
    return players


def read_mln_team_abbrev_lookup(sheet_id: str) -> dict[str, str]:
    """Return {abbrev: full_team} for resolving team names in MLN Games/Plays."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return {
        _str(row.get("Abv")): _str(row.get("Full Team"))
        for _, row in df.iterrows()
        if _str(row.get("Abv")) and _str(row.get("Full Team"))
    }


def read_mln_games_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Games' tab from an MLN sheet (current season or archive).

    Detects format by column names: MLN current uses 'Winning Pitcher';
    MLN Archive uses 'WP'. Away/Home contain team abbreviations; caller
    resolves them to full names via read_mln_team_abbrev_lookup().
    """
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Games')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    cols = set(df.columns)

    # MLN current uses long column names; Archive uses short abbreviations
    is_current = "Winning Pitcher" in cols

    games = []
    for _, row in df.iterrows():
        game_num = _safe_int(row.get("Game#"))
        if not game_num:
            continue
        game_code = str(game_num).zfill(6)
        try:
            season = int(game_code[:2])
            session_num = int(game_code[2:4])
        except ValueError:
            continue

        if is_current:
            a_scr       = _safe_int(row.get("a_Scr"))
            h_scr       = _safe_int(row.get("h_Scr"))
            winning_p   = _str(row.get("Winning Pitcher"))
            losing_p    = _str(row.get("Losing Pitcher"))
            save_p      = _str(row.get("Save"))
            hold_1      = _str(row.get("Hold"))
            hold_2      = _str(row.get("Hold.1"))
            potg        = _str(row.get("Player of the Game"))
            hm1         = _str(row.get("Honorable Mention"))
            hm2         = _str(row.get("Honorable Mention.1"))
            hm3         = _str(row.get("Honorable Mention.2"))
            umpire      = _str(row.get("Umpire Assignment"))
            last_play   = _str(row.get("Last Play"))
            last_inn    = _str(row.get("Inning"))
            last_res    = _str(row.get("Last Result"))
            start_time  = _str(row.get("Start"))
            end_time    = _str(row.get("End"))
            division    = _str(row.get("Division"))
            link        = None
        else:
            a_scr       = _safe_int(row.get("a_scr"))
            h_scr       = _safe_int(row.get("h_scr"))
            winning_p   = _str(row.get("WP"))
            losing_p    = _str(row.get("LP"))
            save_p      = _str(row.get("SV"))
            hold_1      = _str(row.get("HD"))
            hold_2      = _str(row.get("HD2"))
            potg        = _str(row.get("PotG"))
            hm1         = _str(row.get("HM1"))
            hm2         = _str(row.get("HM2"))
            hm3         = _str(row.get("HM3"))
            umpire      = _str(row.get("Umpire"))
            last_play   = None
            last_inn    = None
            last_res    = None
            start_time  = None
            end_time    = None
            division    = _str(row.get("Division"))
            link        = _str(row.get("Link"))

        games.append({
            "league":              "MLN",
            "game_code":           game_code,
            "game_id_short":       _str(row.get("GameID")),
            "season":              season,
            "session_number":      session_num,
            "away_team":           _str(row.get("Away")),
            "home_team":           _str(row.get("Home")),
            "away_score":          a_scr,
            "home_score":          h_scr,
            "winning_pitcher":     winning_p,
            "losing_pitcher":      losing_p,
            "save_pitcher":        save_p,
            "hold_1":              hold_1,
            "hold_2":              hold_2,
            "player_of_game":      potg,
            "honorable_mention_1": hm1,
            "honorable_mention_2": hm2,
            "honorable_mention_3": hm3,
            "umpire":              umpire,
            "win_team":            _str(row.get("Win")),
            "loss_team":           _str(row.get("Loss")),
            "last_play":           last_play,
            "last_inning":         last_inn,
            "last_result":         last_res,
            "start_time":          start_time,
            "end_time":            end_time,
            "division":            division,
            "link":                link,
            "type":                _str(row.get("Type")),
        })
    return games


def read_mln_plays_from_sheet(sheet_id: str, tab: str = "Plays", gid: str | None = None) -> list[dict]:
    """Read a plays tab from an MLN sheet. Archive uses 'Plays'; current season uses 'Plays (Raw)'.

    Away/Home contain Team IDs (e.g. T1009); Pitcher/Batter/Catcher/Runner contain
    MLN player IDs. Caller resolves these to names via get_mln_teams/players_for_lookup().

    `gid` (the tab's numeric sheet id, visible in the browser URL as
    `#gid=...` when that tab is selected) routes the fetch through the plain
    CSV export endpoint instead of the gviz query endpoint. This matters for
    the archive's `Pos*` column specifically: gviz infers ONE type per
    column from the data it scans, and since the earliest rows (seasons 1-4)
    store the literal text "0" there, gviz locks the whole column to
    "number" and silently returns null for every later row whose real value
    is a text position code ("SS", "6-4-3", ...) - confirmed directly
    against the sheet's own gviz column-type metadata. The plain export
    endpoint does no such inference; it round-trips the exact cell text, at
    the cost of needing this one caller-supplied gid instead of a tab name.
    """
    import urllib.parse
    if gid:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    else:
        url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"
        )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    plays = []
    for _, row in df.iterrows():
        play_num = _safe_int(row.get("Play"))
        game_raw = _safe_int(row.get("Game"))
        result   = _str(row.get("Result"))
        if not play_num or not game_raw or not result:
            continue
        game_code = str(game_raw).zfill(6)

        inning_raw = _str(row.get("Inning")) or "T1"
        inning_num, half = parse_inning(inning_raw)

        play_type = _str(row.get("PlayType"))
        pitch = _safe_int(row.get("Pitch"))
        swing = _safe_int(row.get("Swing"))

        # OBC: "0" means empty base in MLN
        on_first  = _str(row.get("OnFirst"))  or "0"
        on_second = _str(row.get("OnSecond")) or "0"
        on_third  = _str(row.get("OnThird"))  or "0"
        brc = (
            (1 if on_first  not in ("0", "-") else 0)
            | (2 if on_second not in ("0", "-") else 0)
            | (4 if on_third  not in ("0", "-") else 0)
        )
        cell_obc = BRC_TO_OBC.get(brc, "Empty")

        # Cross-check against Playcode ("Outs_OBC_Result", e.g. "0_5_HR") -
        # its middle token is the league's own canonical base-state code
        # (0=empty, 1-3=one runner in base order, 4-6=two runners, 7=loaded -
        # decoded with _BRC_TO_OBC, the SAME table import_BRC.csv's Situation
        # column uses; the public BRC_TO_OBC above numbers codes 3/4
        # differently and is only for turning these OnFirst/OnSecond/OnThird
        # cells into a string, never for decoding a real league base-state
        # code like this one). Verified against real data (Alex's report):
        # the live sheet's cells agree with Playcode on every row, but the
        # archive sheet disagrees on ~5% of season 1's rows (smaller pockets
        # elsewhere, 0% in season 12).
        #
        # Alex's hierarchy, in order:
        #  1. Same runner COUNT on both sides (just a different base) - trust
        #     the cells outright. They carry a real player id per base;
        #     Playcode's digit is only a count/pattern, so OR-ing the two
        #     here would manufacture a phantom second runner neither source
        #     actually shows (e.g. cells say 2B, Playcode says 1B - both one
        #     runner, not two).
        #  2. Different count - build the occupancy up from Playcode's
        #     pattern instead, keeping every base the cells already have a
        #     real id for and adding whichever base(s) Playcode implies are
        #     missing. Never removes a cell-sourced base, even if that
        #     leaves more runners than Playcode's own count suggests -
        #     losing a real known id is worse than an unresolved surplus.
        # obc_cell_has_extra flags any leftover cells-only base (case 1
        # always, case 2 when the cells simply have more runners than
        # Playcode) for a caller to report on rather than resolve silently.
        play_code = _str(row.get("Playcode"))
        pc_parts = play_code.split("_")
        playcode_obc = None
        if len(pc_parts) >= 2:
            try:
                playcode_obc = _BRC_TO_OBC.get(int(pc_parts[1]))
            except ValueError:
                playcode_obc = None
        obc = cell_obc
        obc_repaired = False
        obc_cell_has_extra = False
        if playcode_obc is not None and cell_obc in _OBC_STRING_TO_CODE and playcode_obc != cell_obc:
            obc_cell_has_extra = any(c == "1" and p == "0" for c, p in zip(cell_obc, playcode_obc))
            if cell_obc.count("1") != playcode_obc.count("1"):
                merged = "".join("1" if (a == "1" or b == "1") else "0" for a, b in zip(cell_obc, playcode_obc))
                if merged != cell_obc:
                    obc = merged
                    obc_repaired = True

        plays.append({
            "league":      "MLN",
            "season":      _safe_int(row.get("Season")),
            "season_type": _str(row.get("Season.1")),
            "game_code":   game_code,
            "play_num":    play_num,
            # Archive column is named "Timestamp*" (current season: plain
            # "Timestamp") - same raw "M/D/YYYY H:MM[:SS]" shape either way,
            # left as-is here; key_moments_build.py's _parse_timestamp does
            # the actual ISO conversion downstream for both.
            "timestamp":   _str(row.get("Timestamp") or row.get("Timestamp*")),
            "umpire":      _str(row.get("Umpire")),
            "away":        _str(row.get("Away")),    # Team ID e.g. T1009
            "home":        _str(row.get("Home")),    # Team ID e.g. T1003
            "inning_raw":  inning_raw,
            "inning":      inning_num,
            "half":        half,
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "play_type":  play_type,
            "result":     result,
            "play_code":  play_code,
            "pitcher_id": _safe_int(row.get("Pitcher")),
            "catcher_id": _safe_int(row.get("Catcher")),
            "pos":        _str(row.get("Pos") or row.get("Pos*")),
            "batter_id":  _safe_int(row.get("Batter")),
            "on_first":   on_first,
            "on_second":  on_second,
            "on_third":   on_third,
            # Diagnostics for the Playcode cross-check above - not consumed
            # by the base-diamond/runner logic itself (obc already carries
            # the repair), just there for a caller to report repair/conflict
            # rates without re-deriving them.
            "_obc_repaired":        obc_repaired,
            "_obc_cell_has_extra":  obc_cell_has_extra,
            "scored2":    _str(row.get("scored2")),
            "scored3":    _str(row.get("scored3")),
            "scored4":    _str(row.get("scored4")),
            "er1":        _str(row.get("er1")),
            "er2":        _str(row.get("er2")),
            "er3":        _str(row.get("er3")),
            "er4":        _str(row.get("er4")),
            "pitch":      pitch,
            "swing":      swing,
            "throw_num":  _safe_int(row.get("Throw")),
            "runner_id":  _safe_int(row.get("Runner")),
            "steal_num":  _safe_int(row.get("Steal")),
            "obc":        obc,
        })
    return plays


# ------------------------------------------------------------------ defensive alignment (fielding-position feature)
#
# Resolves which player occupies each defensive position at a given play, via
# two paths chosen per game (fielding-position-implementation-plan.md):
#   Path A (in-progress games) - live read of the "Lineups" tab.
#   Path B (completed games) - reconstruction from plays.pos (never Lineups,
#     even if the game is still technically present in that tab).
# No persistence and no network I/O outside read_lineups_from_sheet - every
# resolver here takes data as plain arguments so it stays unit testable.

# The 7 positions this feature resolves. P and C are excluded - both are
# already explicit per play (plays.pitcher_id / plays.catcher_id), so neither
# path needs to (or should) resolve them; DH is tracked internally by Path B
# for substitution bookkeeping but is never a real fielding position.
ALIGNMENT_POSITIONS = ("1B", "2B", "3B", "SS", "LF", "CF", "RF")


def read_lineups_from_sheet(sheet_id: str, tab: str = "Lineups") -> list[dict]:
    """Read the 'Lineups' tab of a public Google Sheet and return a list of
    per-slot assignment rows.

    The gviz CSV export for this tab is far messier than the logical column
    table: there's a duplicate unsuffixed column group (Play, Team, Pos,
    Order) that's an input-side artifact only filled on starting-lineup rows
    (ignored - always read the '.1'-suffixed columns, which is why presence
    of 'Team.1' must win over the bare 'Team' when both exist), a 'Game#'
    column that is NOT the 6-digit game code (ignore, join on GameID only),
    and unrelated side tables (roster listings, a "Current Lineups" pivot)
    bleeding into the CSV to the right of 'active'. So columns are selected
    by name, never positionally, and 'active' itself is unused (per design:
    not needed to resolve "who's playing X as of play N").
    """
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    def _col(suffixed: str, bare: str) -> str:
        return suffixed if suffixed in df.columns else bare

    team_col = _col("Team.1", "Team")
    pos_col = _col("Pos.1", "Pos")
    order_col = _col("Order.1", "Order")
    play_col = _col("Play.1", "Play")

    rows = []
    for _, row in df.iterrows():
        game_id_short = _str(row.get("GameID"))
        player_name = _str(row.get("Player"))
        pos = _str(row.get(pos_col))
        seq = _safe_int(row.get(play_col))
        row_num = _safe_int(row.get("Row"))
        if not game_id_short or not player_name or not pos or seq is None:
            continue
        rows.append({
            "game_id_short": game_id_short,
            "row": row_num,
            "team": _str(row.get(team_col)),
            "pos": pos,
            "player_name": player_name,
            "order_slot": _safe_int(row.get(order_col)),
            "seq": seq,
        })
    rows.sort(key=lambda r: (r["game_id_short"], r["row"] if r["row"] is not None else -1))
    return rows


def game_is_final(game: dict | None) -> bool:
    """Has this game actually finished? The repo's one definition (see
    KEY_MOMENTS.md): win_team is set and both scores are set. end_time and
    last_play both keep updating while a game is still in progress, so
    neither can stand in for completion.
    """
    return bool(game and game.get("win_team") and game.get("away_score") is not None
                and game.get("home_score") is not None)


def split_play_num(play_num) -> tuple[int, int]:
    """130317034 -> (130317, 34). Pins the '{game_code}{seq:03d}' contract."""
    n = int(play_num)
    return n // 1000, n % 1000


def make_play_num(game_code, seq) -> int:
    """('130317', 34) -> 130317034."""
    return int(game_code) * 1000 + int(seq)


def lineup_alignment_at(lineup_rows: list[dict], team: str, seq: int,
                         name_to_id: dict[str, int]) -> dict[str, dict]:
    """Path A (in-progress games): who's playing each position as of play
    `seq`, per the live Lineups tab. `lineup_rows` is one game's rows
    (from read_lineups_from_sheet, already filtered to one game_id_short).

    For each batting-order slot, the current occupant is whoever holds that
    slot most recently (highest Row) as of `seq` - and that row's own `pos`
    is their current position, which may differ from the slot's original
    position if they've since shifted without a substitution. Pitchers
    aren't in the batting order (Order.1 blank) and are ignored here - P
    comes from the play row itself, same as C never being resolved by this
    path.
    """
    latest_by_slot: dict[int, dict] = {}
    for r in lineup_rows:
        if r.get("team") != team or r.get("pos") == "P" or r.get("order_slot") is None:
            continue
        if r.get("seq") is None or r["seq"] > seq:
            continue
        slot = r["order_slot"]
        cur = latest_by_slot.get(slot)
        if cur is None or (r.get("row") or -1) > (cur.get("row") or -1):
            latest_by_slot[slot] = r

    # Pivot slot -> pos into pos -> occupant. A collision (two slots landing
    # on the same pos) is a data-quality problem worth surfacing, not a
    # crash - keep the most recently instructed occupant (higher Row).
    by_pos: dict[str, dict] = {}
    for slot, r in latest_by_slot.items():
        pos = r.get("pos")
        if not pos or pos not in ALIGNMENT_POSITIONS:
            continue
        cur = by_pos.get(pos)
        if cur is not None:
            if (r.get("row") or -1) <= (cur.get("row") or -1):
                continue
            print(f"WARNING: lineup collision - {team} {pos} at seq {seq} has both "
                  f"slot {cur.get('order_slot')} (row {cur.get('row')}) and "
                  f"slot {slot} (row {r.get('row')}); keeping the latter", file=sys.stderr)
        by_pos[pos] = r

    result: dict[str, dict] = {}
    for pos, r in by_pos.items():
        name = r["player_name"]
        player_id = name_to_id.get(name)
        if player_id is None:
            print(f"WARNING: lineup name {name!r} ({team} {pos}) has no matching "
                  f"player_id - traded/released player off the current roster tab?",
                  file=sys.stderr)
        result[pos] = {"player_id": player_id, "name": name}
    return result


def next_batter_info(lineup_rows: list[dict], team: str, last_batter_id: int | None,
                      name_to_id: dict[str, int]) -> dict | None:
    """Who's due up next for `team`, per the live Lineups tab - the offense's
    own counterpart to lineup_alignment_at (which resolves defense). Not
    seq-bounded like that resolver: "next batter" is inherently a live,
    right-now question (any substitution already on the sheet should count),
    not "as of a specific past play" - so this always reads the single
    latest row per slot, full stop.

    Finds last_batter_id's own current slot (whichever slot's latest row
    names them), then resolves whoever most recently occupied the next slot
    (mod 9, wrapping 9->1). last_batter_id=None (this team hasn't batted yet
    this game) starts at the leadoff slot. Returns None - never a guess -
    when last_batter_id can't be placed in the order at all (the lineup tab
    hasn't caught up to a very recent substitution yet) or the next slot
    itself has no row on the sheet.
    """
    latest_by_slot: dict[int, dict] = {}
    for r in lineup_rows:
        if r.get("team") != team or r.get("order_slot") is None:
            continue
        slot = r["order_slot"]
        cur = latest_by_slot.get(slot)
        if cur is None or (r.get("row") or -1) > (cur.get("row") or -1):
            latest_by_slot[slot] = r

    if last_batter_id is None:
        next_slot = 1
    else:
        last_slot = next(
            (slot for slot, r in latest_by_slot.items() if name_to_id.get(r["player_name"]) == last_batter_id),
            None,
        )
        if last_slot is None:
            return None
        next_slot = (last_slot % 9) + 1

    r = latest_by_slot.get(next_slot)
    if r is None:
        return None
    name = r["player_name"]
    player_id = name_to_id.get(name)
    if player_id is None:
        print(f"WARNING: lineup name {name!r} ({team}, slot {next_slot}) has no matching "
              f"player_id - traded/released player off the current roster tab?",
              file=sys.stderr)
    return {"order_slot": next_slot, "player_id": player_id, "name": name}


# Row types that occupy their own Play row without representing a new turn
# through the batting order - a steal attempt and a balk both share their
# batter and slot with the at-bat they interrupt, they just don't end it.
# Empirically confirmed against the live sheet (defense_alignment_test.py):
# excluding both is what makes the mod-9 rotation below land cleanly - one
# balk row slipping through (it isn't tagged play_type='Steal', only
# result='Balk') was enough to shift an entire team's computed slots by one
# for the rest of that game. If a future row type turns up with the same
# shape (same batter/slot, doesn't end the PA), it belongs in this check too.
def _is_genuine_turn(play_type, result) -> bool:
    if str(play_type or "").lower() == "steal":
        return False
    if result == "Balk":
        return False
    return True


def reconstruct_defense_timeline(
    team_plays: list[tuple[int, int, str, str, str]],
) -> dict[str, list[tuple[int, int]]]:
    """Path B (completed games): reconstruct one team's defensive alignment
    over the course of a game from their own plate appearances alone -
    `plays.pos` is the batter's own roster position as of that PA, not "who
    fielded this ball", so batting observations are the only signal
    available (Decision 1: never rely on Lineups for completed games, even
    if it's technically still present in the sheet).

    `team_plays`: this team's own plate-appearance rows for one game, each a
    `(seq, batter_id, pos, play_type, result)` tuple - order doesn't matter,
    this sorts by seq itself.

    Batting-order slot is recovered rather than read (the sheet never
    records one): this league runs a strict, uninterrupted 9-slot DH
    rotation with no skips, so a team's Nth genuine turn at the plate (0
    -indexed, PH included - it still consumes a turn) belongs to slot N % 9.
    Validated against every team-half in the live sheet (all internally
    consistent - no player's own at-bats ever computed into two different
    slots) once steals/balks are excluded via `_is_genuine_turn`.

    Within a slot, a run of consecutive turns by the same batter_id is one
    occupancy. A PH turn carries no position by itself - but once a LATER
    occupant of the SAME slot reveals a real position (directly, from their
    own at-bat), that position is known to belong to the slot itself, and
    applies backward to every occupant since the slot's last resolved
    position - even one who only ever pinch-hit and never batted again
    (confirmed against real data: a pinch-hitter who's the slot's very
    first-ever occupant, with the next occupant only revealed at their own
    real position much later, is correctly shown at that position for her
    whole tenure, backfilled all the way to the start of the game - she's
    the first name evidence for that slot exists for at all).

    Returns `{pos: [(seq, player_id), ...]}`, sorted by seq - "occupied by
    this player from this seq onward." A position with no evidence at all,
    forward or backward, has no key - genuinely unresolved, not an assumed
    starter.
    """
    rows = sorted(
        (r for r in team_plays if _is_genuine_turn(r[3], r[4])),
        key=lambda r: r[0],
    )
    slots: dict[int, list[tuple[int, int, str, str, str]]] = {i: [] for i in range(9)}
    for i, row in enumerate(rows):
        slots[i % 9].append(row)

    timeline: dict[str, list[tuple[int, int]]] = {}

    for slot_rows in slots.values():
        if not slot_rows:
            continue
        # One entry per occupancy: a new entry starts whenever the batter_id
        # changes (a real substitution, directly observed - no guessing) or
        # the SAME batter shows a different real position later in their own
        # tenure (an in-game move, no personnel change). `pos` stays None
        # until either this occupant's own at-bat reveals one, or the
        # backward-fill pass below inherits one from whoever comes next.
        entries: list[dict] = []
        current_occupant = None
        is_first_occupancy = True

        for seq, batter_id, pos, _play_type, _result in slot_rows:
            is_real = bool(pos) and pos != "PH"
            if batter_id != current_occupant:
                entry_seq = 1 if is_first_occupancy else seq
                entries.append({"occupant": batter_id, "pos": pos if is_real else None, "entry_seq": entry_seq})
                current_occupant = batter_id
                is_first_occupancy = False
            else:
                last = entries[-1]
                if is_real and last["pos"] != pos:
                    if last["pos"] is None:
                        last["pos"] = pos  # this occupant's first revealed position
                    else:
                        entries.append({"occupant": batter_id, "pos": pos, "entry_seq": seq})
                # else: reconfirming (or still PH) - nothing to record.

        # Backward-fill: an entry with no position of its own inherits the
        # nearest LATER entry's position (any occupant, same slot) - keeping
        # its own occupant and entry_seq. Walking backward means each fill
        # can itself feed the one before it, so a chain of several
        # unresolved occupants in a row all cascade to the same eventual
        # answer in one pass.
        for i in range(len(entries) - 1, -1, -1):
            if entries[i]["pos"] is None:
                for j in range(i + 1, len(entries)):
                    if entries[j]["pos"] is not None:
                        entries[i]["pos"] = entries[j]["pos"]
                        break

        for e in entries:
            if e["pos"] and e["pos"] != "DH":
                timeline.setdefault(e["pos"], []).append((e["entry_seq"], e["occupant"]))

    for entries in timeline.values():
        entries.sort()
    return timeline


def timeline_alignment_at(timelines: dict[str, list[tuple[int, int]]], seq: int,
                           positions=None) -> dict[str, int]:
    """Query a Path B timeline (or timelines) for the occupant of each
    position as of `seq` - the last entry with entry_seq <= seq. No entry
    covering seq means no key in the result (genuinely unresolved, not an
    assumed starter).
    """
    result: dict[str, int] = {}
    keys = positions if positions is not None else timelines.keys()
    for pos in keys:
        occupant = None
        for entry_seq, player_id in timelines.get(pos, ()):
            if entry_seq <= seq:
                occupant = player_id
            else:
                break
        if occupant is not None:
            result[pos] = occupant
    return result


def get_defensive_alignment(game: dict | None, play_num: int, positions=None, *,
                             plays: list[dict] | None = None,
                             lineups: list[dict] | None = None,
                             name_to_id: dict[str, int] | None = None,
                             id_to_name: dict[int, str] | None = None) -> dict[str, dict]:
    """Who's playing each defensive position at `play_num`, dispatching on
    game_is_final. Thin dispatcher - everything is injected, no fetching
    happens in here (the caller controls fetch frequency; see Decision 2's
    "cache briefly" for the live path).

    Defending team is derived from the target play's own half ('top' -> home
    defends, 'bottom' -> away defends) rather than from Team IDs on the play
    rows, since `game`'s home_team/away_team are already the abbreviations
    both paths need (Lineups' Team.1, and the simple half-filter Path B uses
    to isolate a team's own batting rows).

    `positions` narrows the output in both branches; defaults to the full
    7-position ALIGNMENT_POSITIONS set (the build-time caller always wants
    all 7 - see key_moments_build.py - this parameter exists for spot-check
    tooling and future callers that only need one or two).
    """
    target = None
    for p in (plays or ()):
        if p.get("play_num") == play_num:
            target = p
            break
    if target is None:
        return {}

    game_code = target.get("game_code")
    defending_is_home = (target.get("half") or "top") == "top"
    _, seq = split_play_num(play_num)
    wanted = set(positions) if positions is not None else set(ALIGNMENT_POSITIONS)

    if game_is_final(game):
        team_batting_half = "bottom" if defending_is_home else "top"
        team_plays = [
            (split_play_num(p["play_num"])[1], p.get("batter_id"), p.get("pos"),
             p.get("play_type"), p.get("result"))
            for p in (plays or ())
            if p.get("game_code") == game_code and p.get("half") == team_batting_half
        ]
        timelines = reconstruct_defense_timeline(team_plays)
        ids = timeline_alignment_at(timelines, seq, positions=sorted(wanted))
        id_to_name = id_to_name or {}
        return {
            pos: {"player_id": pid, "name": id_to_name.get(pid) or f"Player {pid}"}
            for pos, pid in ids.items()
        }

    team = (game.get("home_team") if defending_is_home else game.get("away_team")) if game else None
    game_id_short = game.get("game_id_short") if game else None
    rows = [r for r in (lineups or ()) if r.get("game_id_short") == game_id_short]
    alignment = lineup_alignment_at(rows, team, seq, name_to_id or {})
    return {pos: v for pos, v in alignment.items() if pos in wanted}


def result_bar(result_counts: dict[str, int], title: str = "Results") -> go.Figure:
    """Horizontal bar chart of result distribution."""
    labels = list(result_counts.keys())
    values = list(result_counts.values())
    total = sum(values) or 1
    pcts = [v / total * 100 for v in values]

    fig = go.Figure(go.Bar(
        x=pcts,
        y=labels,
        orientation="h",
        text=[f"{v} ({p:.1f}%)" for v, p in zip(values, pcts)],
        textposition="outside",
        marker_color="#4C78A8",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis_title="% of ABs",
        height=max(200, len(labels) * 30 + 80),
        margin=dict(l=80, r=80, t=45, b=30),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ── pitcher stats ─────────────────────────────────────────────────────────────

def _wrap_dd_td_counts(pitches: list[int]) -> tuple[int, int, int, int, int, int]:
    """Single pass over one game's ordered pitch sequence for a pitcher, returning
    (wrap_eligible, wrap_hit, dd_eligible, dd_hit, td_eligible, td_hit).

    Wraparound eligibility requires the prior pitch be in the boundary zone
    (>=850 or <=150). DD/TD are always eligible once enough prior pitches
    exist (1 for DD, 2 for TD) - TD is unconditional over every eligible
    3-pitch window, not a continuation rate off an existing DD - see
    _dd_group/_td_group for the same logic applied per-pitch in enrich_df."""
    we = wc = de = dh = te = th = 0
    for i in range(1, len(pitches)):
        prev, curr = pitches[i - 1], pitches[i]
        if prev >= 850 or prev <= 150:
            we += 1
            if (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850):
                wc += 1
        de += 1
        if circular_diff(curr, prev) <= 50:
            dh += 1
        if i >= 2:
            anchor = pitches[i - 2]
            te += 1
            if circular_diff(prev, anchor) <= 50 and circular_diff(curr, anchor) <= 50:
                th += 1
    return we, wc, de, dh, te, th


def compute_pitcher_stats(df: pd.DataFrame) -> list[dict]:
    """Compute per-pitcher behavioral stats from an enriched plays DataFrame.

    Grouped by pitcher_id (the shared human id) so a pitcher who changed names
    gets ONE merged row, labeled with their most-recent name. Rows without a
    pitcher_id fall back to grouping by name."""
    import datetime
    sw = df[df["swing"].notna()].copy()
    if sw.empty:
        return []
    _pidnum = (pd.to_numeric(sw["pitcher_id"], errors="coerce")
               if "pitcher_id" in sw.columns else pd.Series(np.nan, index=sw.index))
    # Rescue: a play with no pitcher_id whose name matches a pitcher that DOES
    # have an id elsewhere is folded into that id's group (no split row).
    _name_pid = {}
    for _nm, _pv in zip(sw["pitcher_name"].astype(str), _pidnum):
        if pd.notna(_pv):
            _name_pid[_nm] = int(_pv)

    def _gk(nm, pv):
        if pd.notna(pv):
            return f"id:{int(pv)}"
        if nm in _name_pid:
            return f"id:{_name_pid[nm]}"
        return f"nm:{nm}"
    sw["_grp"] = [_gk(nm, pv) for nm, pv in zip(sw["pitcher_name"].astype(str), _pidnum)]
    rows = []
    for _gkey, grp in sw.groupby("_grp"):
        _pids = pd.to_numeric(grp.get("pitcher_id"), errors="coerce").dropna() if "pitcher_id" in grp else pd.Series(dtype=float)
        player_id = int(_pids.iloc[0]) if not _pids.empty else None
        # Most-recent name = pitcher_name from the highest-season play in the group.
        if "season" in grp.columns and grp["season"].notna().any():
            pitcher = grp.sort_values("season", na_position="first")["pitcher_name"].iloc[-1]
        else:
            pitcher = grp["pitcher_name"].iloc[-1]
        deltas   = grp["pitch_circ_delta"].dropna()
        delta2s  = grp["pitch_circ_delta2"].dropna()
        approach = grp["pitch_approach"].dropna()
        # Wraparound %: of pitches where the previous pitch was in the boundary zone
        # (>=850 or <=150), how often did they actually cross to the other side?
        # DD %/TD %: of eligible consecutive pitches, how often did they land within
        # a circular 50-value window (see _wrap_dd_td_counts).
        _wrap_eligible = _wrap_crossed = _dd_eligible = _dd_hit = _td_eligible = _td_hit = 0
        for _, game_grp in grp.groupby("game_id"):
            pitches = game_grp.sort_values("id")["pitch"].astype(int).tolist()
            we, wc, de, dh, te, th = _wrap_dd_td_counts(pitches)
            _wrap_eligible += we; _wrap_crossed += wc
            _dd_eligible   += de; _dd_hit        += dh
            _td_eligible   += te; _td_hit        += th
        wraparound_pct = round(_wrap_crossed / _wrap_eligible * 100, 2) if _wrap_eligible else None
        dd_pct = round(_dd_hit / _dd_eligible * 100, 2) if _dd_eligible else None
        td_pct = round(_td_hit / _td_eligible * 100, 2) if _td_eligible else None
        rows.append({
            "pitcher_name":   pitcher,
            "player_id":      player_id,
            "ab_count":       len(grp),
            "avg_abs_delta":  round(float(deltas.abs().mean()), 3) if not deltas.empty else None,
            "avg_delta2":     round(float(delta2s.mean()), 3)      if not delta2s.empty else None,
            "shadow_pct":     round(float(approach.mean() * 100), 2) if not approach.empty else None,
            "meme_rate":      round(float(grp["is_meme_pitch"].mean() * 100), 2),
            "wraparound_pct": wraparound_pct,
            "dd_pct":         dd_pct,
            "td_pct":         td_pct,
            "updated_at":     datetime.datetime.utcnow().isoformat(),
        })
    return rows


def compute_recent_pitcher_stats(df: pd.DataFrame) -> dict:
    """Compute behavioral stats for a single pitcher from a pre-filtered DataFrame."""
    sw = df[df["swing"].notna()]
    if sw.empty:
        return {}
    deltas   = sw["pitch_circ_delta"].dropna()
    delta2s  = sw["pitch_circ_delta2"].dropna()
    approach = sw["pitch_approach"].dropna()
    _we = _wc = _de = _dh = _te = _th = 0
    for _, g in sw.groupby("game_id"):
        pitches = g.sort_values("id")["pitch"].astype(int).tolist()
        we, wc, de, dh, te, th = _wrap_dd_td_counts(pitches)
        _we += we; _wc += wc
        _de += de; _dh += dh
        _te += te; _th += th
    return {
        "avg_abs_delta":  float(deltas.abs().mean())   if not deltas.empty  else None,
        "avg_delta2":     float(delta2s.mean())        if not delta2s.empty else None,
        "shadow_pct":     float(approach.mean() * 100) if not approach.empty else None,
        "meme_rate":      float(sw["is_meme_pitch"].mean() * 100),
        "wraparound_pct": (_wc / _we * 100)            if _we else None,
        "dd_pct":         (_dh / _de * 100)            if _de else None,
        "td_pct":         (_th / _te * 100)            if _te else None,
    }


_PERCENTILE_STATS = [
    ("Avg |Δ|",      "avg_abs_delta",  lambda v: f"{v:.1f}"),
    ("Avg |Δ²|",     "avg_delta2",     lambda v: f"{v:.1f}"),
    ("Shadow %",     "shadow_pct",     lambda v: f"{v:.1f}%"),
    ("Wraparound %", "wraparound_pct", lambda v: f"{v:.1f}%"),
    ("Meme Rate",    "meme_rate",      lambda v: f"{v:.1f}%"),
    ("DD%", "dd_pct",       lambda v: f"{v:.1f}%"),
    ("TD%", "td_pct",       lambda v: f"{v:.1f}%"),
]


def pitcher_percentile_card(
    pitcher_name: str,
    stats_df: pd.DataFrame,
    recent_vals: dict | None = None,
    recent_n: int | None = None,
    player_id: int | None = None,
) -> go.Figure | None:
    """
    Compact pill-bar percentile chart.
    Bar = career percentile in the qualified pool (≥100 AB).
    Gold needle = where recent stats (recent_vals) fall in that same pool.

    Matches the pitcher's stats row by player_id when given (robust to the row
    being stored under a different name than the dropdown's most-recent one),
    falling back to pitcher_name.
    """
    import math

    if stats_df.empty:
        return None

    row = None
    if player_id is not None and "player_id" in stats_df.columns:
        _m = stats_df[stats_df["player_id"] == player_id]
        if not _m.empty:
            row = _m.iloc[0]
    if row is None:
        if pitcher_name not in stats_df["pitcher_name"].values:
            return None
        row = stats_df[stats_df["pitcher_name"] == pitcher_name].iloc[0]

    # Only qualified pitchers form the reference pool for percentile ranks
    _MIN_AB = 100
    qual = stats_df[stats_df["ab_count"] >= _MIN_AB] if "ab_count" in stats_df.columns else stats_df

    def _percentile(val, qual_vals):
        """Return (pct, label) for val within qual_vals."""
        if len(qual_vals) < 2:
            return 50.0, "50"
        if val < qual_vals.min():
            return 0.0, "0-"
        if val > qual_vals.max():
            return 100.0, "100+"
        rank = float((qual_vals < val).sum()) + float((qual_vals == val).sum()) * 0.5
        p = rank / len(qual_vals) * 100
        return p, f"{p:.0f}"

    stat_labels, pcts, raw_vals, bubble_labels = [], [], [], []
    recent_pcts, recent_raw_vals = [], []
    for label, col, fmt in _PERCENTILE_STATS:
        stat_labels.append(label)
        val = row.get(col) if col in stats_df.columns else None
        qual_vals = qual[col].dropna() if col in qual.columns else pd.Series(dtype=float)

        if val is None or (isinstance(val, float) and pd.isna(val)):
            pcts.append(None)
            raw_vals.append("-")
            bubble_labels.append(None)
        else:
            pct, blbl = _percentile(val, qual_vals)
            pcts.append(pct)
            raw_vals.append(fmt(val))
            bubble_labels.append(blbl)

        # Recent value for same stat
        rval = (recent_vals or {}).get(col)
        if rval is not None and not (isinstance(rval, float) and pd.isna(rval)):
            rpct, _ = _percentile(rval, qual_vals)
            recent_pcts.append(rpct)
            recent_raw_vals.append(fmt(rval))
        else:
            recent_pcts.append(None)
            recent_raw_vals.append(None)

    # Reverse: index 0 = bottom so first stat appears at the top
    stat_labels    = list(reversed(stat_labels))
    pcts           = list(reversed(pcts))
    raw_vals       = list(reversed(raw_vals))
    bubble_labels  = list(reversed(bubble_labels))
    recent_pcts    = list(reversed(recent_pcts))
    recent_raw_vals = list(reversed(recent_raw_vals))
    n              = len(stat_labels)

    def _color(p: float, alpha: float = 1.0) -> str:
        """0–50%: medium blue → light blue; 50–100%: light red → medium red."""
        t = max(0.0, min(1.0, p / 100))
        if t <= 0.5:
            t2 = t * 2
            r = int(30  + t2 * (187 - 30))
            g = int(136 + t2 * (222 - 136))
            b = int(229 + t2 * (251 - 229))
        else:
            t2 = (t - 0.5) * 2
            r = int(255 + t2 * (211 - 255))
            g = int(205 + t2 * (47  - 205))
            b = int(210 + t2 * (47  - 210))
        return f"rgba({r},{g},{b},{alpha})"

    _RX = 3.0  # pill corner radius in x data coords (shared by _pill and bubble placement)

    def _pill(x1: float, x2: float, yc: float, ry: float = 0.30, rx: float = _RX, pts: int = 20):
        """Closed polygon path for a pill (stadium) shape in data coordinates."""
        _rx = min(rx, max((x2 - x1) / 2, 0.01))
        right = [math.pi / 2 - k * math.pi / (pts - 1) for k in range(pts)]
        left  = [-math.pi / 2 - k * math.pi / (pts - 1) for k in range(pts)]
        xr = [(x2 - _rx) + _rx * math.cos(t) for t in right]
        yr = [yc + ry * math.sin(t) for t in right]
        xl = [(x1 + _rx) + _rx * math.cos(t) for t in left]
        yl = [yc + ry * math.sin(t) for t in left]
        return xr + xl + [xr[0]], yr + yl + [yr[0]]

    # Two-bar layout: career on top half, recent on bottom half of each row.
    # Stacking by y-offset eliminates all bubble collision regardless of percentile proximity.
    has_recent = any(p is not None for p in recent_pcts)
    _YO  = 0.28   # y offset from row centre for each bar's centre
    _RY  = 0.16   # half-height of each bar (two bars fit in one row with a gap)
    _BR  = 2.0    # bubble radius in x data coords
    _STAT_SPACING = 1.5  # vertical spacing between stat rows (increase to spread stats further apart)

    fig = go.Figure()

    for i in range(n):
        p,   rv,  lbl,  blbl  = pcts[i], raw_vals[i], stat_labels[i], bubble_labels[i]
        rpct, rrv              = recent_pcts[i], recent_raw_vals[i]

        yc = (i * _STAT_SPACING) + _YO  # career bar centre
        yrc = (i * _STAT_SPACING) - _YO  # recent bar centre

        # ── career row ────────────────────────────────────────────────────────
        # Background track
        xb, yb = _pill(0, 100, yc, ry=_RY)
        fig.add_trace(go.Scatter(x=xb, y=yb, fill="toself",
                                 fillcolor="rgba(128,128,128,0.18)",
                                 line=dict(width=0), mode="lines",
                                 showlegend=False, hoverinfo="skip"))
        if p is not None:
            c = _color(max(p, 1.0))
            bx = max(p - _BR, _BR)
            if p > 0.5:
                xf, yf = _pill(0, p, yc, ry=_RY)
                fig.add_trace(go.Scatter(x=xf, y=yf, fill="toself",
                                         fillcolor=c, line=dict(width=0), mode="lines",
                                         showlegend=False,
                                         hovertemplate=f"{lbl} Career: {rv}<br>Pct: {blbl}<extra></extra>"))
            fig.add_trace(go.Scatter(
                x=[bx], y=[yc], mode="markers+text",
                marker=dict(symbol="circle", size=20, color=c,
                            line=dict(width=1.5, color="rgba(255,255,255,0.8)")),
                text=[blbl], textposition="middle center",
                textfont=dict(color="white", size=8),
                cliponaxis=False, showlegend=False, hoverinfo="skip"))
        # Value annotation pinned to right margin via paper coords - no data range needed
        fig.add_annotation(xref="paper", x=1.02, yref="y", y=yc,
                           text=f"<b>{rv}</b>",
                           showarrow=False, xanchor="left", font=dict(size=14))

        # ── recent row ────────────────────────────────────────────────────────
        if has_recent:
            xb2, yb2 = _pill(0, 100, yrc, ry=_RY)
            fig.add_trace(go.Scatter(x=xb2, y=yb2, fill="toself",
                                     fillcolor="rgba(128,128,128,0.12)",
                                     line=dict(width=0), mode="lines",
                                     showlegend=False, hoverinfo="skip"))
            if rpct is not None:
                rc = _color(max(rpct, 1.0), alpha=1.0)
                rbx = max(rpct - _BR, _BR)
                if rpct > 0.5:
                    xrf, yrf = _pill(0, rpct, yrc, ry=_RY)
                    fig.add_trace(go.Scatter(x=xrf, y=yrf, fill="toself",
                                             fillcolor=rc, line=dict(width=0), mode="lines",
                                             showlegend=False,
                                             hovertemplate=f"{lbl} Recent: {rrv}<br>Pct: {rpct:.0f}<extra></extra>"))
                rblbl = "0-" if rpct == 0.0 else ("100+" if rpct == 100.0 else f"{rpct:.0f}")
                fig.add_trace(go.Scatter(
                    x=[rbx], y=[yrc], mode="markers+text",
                    marker=dict(symbol="circle", size=20, color=rc,
                                line=dict(width=1.5, color="rgba(255,255,255,0.7)")),
                    text=[rblbl], textposition="middle center",
                    textfont=dict(color="white", size=8),
                    cliponaxis=False, showlegend=False, hoverinfo="skip"))
                fig.add_annotation(xref="paper", x=1.02, yref="y", y=yrc,
                                   text=f"<i>{rrv}</i>",
                                   showarrow=False, xanchor="left", font=dict(size=14))

    career_ab = row.get("ab_count") if "ab_count" in row.index else None
    career_ab_str = f" ({int(career_ab)} PA)" if career_ab and not pd.isna(career_ab) else ""
    subtitle = (
        f"<br><sup>Top = Career{career_ab_str}  |  Bottom = Recent ({recent_n} PA)</sup>"
        if has_recent else ""
    )
    fig.update_layout(
        title=dict(
            text=f"<b>{pitcher_name}</b> - Behavioral Tendencies{subtitle}",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        yaxis=dict(
            tickvals=[i * _STAT_SPACING for i in range(n)], ticktext=stat_labels,
            showgrid=False, zeroline=False, showline=False,
            tickfont=dict(size=14), range=[-0.6, n * _STAT_SPACING - 0.4],
        ),
        xaxis=dict(
            range=[0, 107], showgrid=False, showticklabels=False,
            showline=False, zeroline=False,
        ),
        height=int((44 if has_recent else 30) * n * _STAT_SPACING + 58),
        margin=dict(l=85, r=65, t=44, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


_MA_METRICS: dict[str, dict] = {
    "avg_delta":      {"label": "Avg |Δ|",   "col": "pitch_circ_delta",  "scale": "abs",   "y_range": [0, 500], "y_title": "Δ"},
    "avg_delta2":     {"label": "Avg |Δ²|",  "col": "pitch_circ_delta2", "scale": "abs",   "y_range": [0, 500], "y_title": "Δ²"},
    "shadow_pct":     {"label": "Shadow %",      "col": "pitch_approach",    "scale": "pct",   "y_range": [0, 100], "y_title": "Shadow %"},
    "wraparound_pct": {"label": "Wraparound %",  "col": "pitch_wraparound",  "scale": "pct",   "y_range": None, "y_title": "Wraparound %"},
    "meme_rate":      {"label": "Meme Rate %",   "col": "is_meme_pitch",     "scale": "pct",   "y_range": None, "y_title": "Meme Rate %"},
    "dd_pct":         {"label": "DD%", "col": "pitch_dd",          "scale": "pct",   "y_range": None, "y_title": "DD%"},
    "td_pct":         {"label": "TD%", "col": "pitch_td",          "scale": "pct",   "y_range": None, "y_title": "TD%"},
}


def pitcher_ma_figure(df: pd.DataFrame, metric: str, window: int = 20) -> go.Figure | None:
    """20-pitch rolling average of a behavioral tendency across a pitcher's filtered history."""
    defn = _MA_METRICS.get(metric)
    if defn is None:
        return None
    col = defn["col"]
    sw = df[df["swing"].notna()].sort_values("id")
    if sw.empty or col not in sw.columns:
        return None
    raw = sw[col].astype(float)
    if defn["scale"] == "pct":
        raw = raw * 100.0
    else:
        raw = raw.abs()
    # Rolling mean tolerant of the scattered NaNs in the metric (min_periods=1
    # keeps the line unbroken), then blank only the first window-1 pitches so it
    # starts at pitch `window` - partial early averages don't distort the y-range.
    ma = raw.rolling(window=window, min_periods=1).mean()
    ma.iloc[:window - 1] = np.nan
    overall_avg = raw.mean()
    x = list(range(1, len(ma) + 1))

    y_range = defn["y_range"]
    if y_range is None:
        ma_max = ma.dropna().max() if not ma.dropna().empty else 0
        y_range = [0, max(float(ma_max) * 1.4, 5.0)]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=ma.tolist(),
        mode="lines",
        line=dict(color="#4ade80", width=2),
        hovertemplate=f"Pitch %{{x}}<br>{defn['y_title']}: %{{y:.1f}}<extra></extra>",
        name=defn["label"],
    ))
    if not pd.isna(overall_avg):
        fig.add_hline(
            y=overall_avg,
            line=dict(color="rgba(255,255,255,0.35)", width=1, dash="dot"),
            annotation_text=f"avg {overall_avg:.1f}",
            annotation_font=dict(color="rgba(255,255,255,0.5)", size=10),
            annotation_position="top right",
        )
    fig.update_layout(
        height=260,
        margin=dict(l=55, r=20, t=20, b=40),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(
            title="Pitch #",
            title_font=dict(color="rgba(255,255,255,0.6)", size=11),
            tickfont=dict(color="rgba(255,255,255,0.55)", size=10),
            gridcolor="rgba(255,255,255,0.07)",
            zerolinecolor="rgba(255,255,255,0.1)",
        ),
        yaxis=dict(
            title=defn["y_title"],
            title_font=dict(color="rgba(255,255,255,0.6)", size=11),
            tickfont=dict(color="rgba(255,255,255,0.55)", size=10),
            gridcolor="rgba(255,255,255,0.07)",
            zerolinecolor="rgba(255,255,255,0.1)",
            range=y_range,
        ),
        hoverlabel=dict(bgcolor="rgba(30,30,30,0.9)", font_size=12),
    )
    return fig


def win_probability_chart(
    wp_df: pd.DataFrame,
    home_team: str = "Home",
    away_team: str = "Away",
    title: str | None = None,
    home_hex: str = "#d6604d",
    away_hex: str = "#2166ac",
) -> go.Figure:
    """Win probability chart - away team at top (y=100%), home team at bottom (y=0%).

    Displays away_wp = 1 - home_wp so the away team's winning region is shaded at the
    top and the home team's region at the bottom.
    wp_df must contain: play_idx, inn_label, home_wp, hover, result columns
    (produced by compute_game_wp_series).
    """
    if wp_df.empty:
        return go.Figure().update_layout(height=420, title=title or "Win Probability")

    def _hex_rgba(hex_c: str, a: float = 0.18) -> str:
        h = hex_c.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{a})"

    x       = wp_df["play_idx"].tolist()
    y       = ((1.0 - wp_df["home_wp"]) * 100).tolist()  # away_wp: high = away winning
    hover   = wp_df["hover"].tolist()
    results = wp_df["result"].tolist() if "result" in wp_df.columns else [""] * len(x)

    # Dual-color fill: above 50 = away winning, below 50 = home winning.
    # Insert exact crossing points so the fill polygon edge tracks the actual
    # line at crossings - without this the polygon bleeds outside the line.
    ix, iy = [x[0]], [y[0]]
    for _i in range(1, len(x)):
        if (y[_i - 1] >= 50.0) != (y[_i] >= 50.0):
            _t  = (50.0 - y[_i - 1]) / (y[_i] - y[_i - 1])
            _cx = x[_i - 1] + _t * (x[_i] - x[_i - 1])
            ix.append(_cx); iy.append(50.0)
        ix.append(x[_i]); iy.append(y[_i])

    fill_x  = ix + list(reversed(ix))
    upper_y = [max(yi, 50.0) for yi in iy] + [50.0] * len(ix)
    lower_y = [50.0] * len(ix) + [min(yi, 50.0) for yi in reversed(iy)]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=fill_x, y=upper_y,
        fill="toself", fillcolor=_hex_rgba(away_hex),
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=fill_x, y=lower_y,
        fill="toself", fillcolor=_hex_rgba(home_hex),
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ))

    fig.add_hline(y=50, line_dash="dash", line_color="rgba(128,128,128,0.5)", line_width=1)

    # Split line into colored segments: away_hex when away leading (y>=50), home_hex otherwise.
    # Interpolate the exact x where the line crosses 50 so segments meet cleanly.
    def _colored_segments(xv, yv):
        segs: list[tuple[bool, list, list]] = []
        if not xv:
            return segs
        is_away = yv[0] >= 50.0
        sx, sy = [xv[0]], [yv[0]]
        for i in range(1, len(xv)):
            new_away = yv[i] >= 50.0
            if new_away != is_away:
                t  = (50.0 - yv[i - 1]) / (yv[i] - yv[i - 1])
                cx = xv[i - 1] + t * (xv[i] - xv[i - 1])
                sx.append(cx); sy.append(50.0)
                segs.append((is_away, sx, sy))
                sx, sy = [cx, xv[i]], [50.0, yv[i]]
                is_away = new_away
            else:
                sx.append(xv[i]); sy.append(yv[i])
        segs.append((is_away, sx, sy))
        return segs

    for _is_away, _sx, _sy in _colored_segments(x, y):
        fig.add_trace(go.Scatter(
            x=_sx, y=_sy,
            mode="lines",
            line=dict(color=away_hex if _is_away else home_hex, width=2.5),
            showlegend=False, hoverinfo="skip",
        ))

    # Markers with per-point color and hover (separate trace, no line)
    _mc = [away_hex if yi >= 50 else home_hex for yi in y]
    fig.add_trace(go.Scatter(
        x=x, y=y,
        mode="markers",
        marker=dict(size=4, color=_mc),
        hovertext=hover,
        hovertemplate="%{hovertext}<extra></extra>",
        showlegend=False,
    ))

    # Key play markers: top plays by absolute WP swing
    _labels = wp_df["inn_label"].tolist() if "inn_label" in wp_df.columns else [""] * len(x)
    _swings = [0.0] + [abs(y[i] - y[i - 1]) for i in range(1, len(y))]
    _swing_candidates = [
        (xi, yi, ri, si)
        for xi, yi, ri, si, lbl in zip(x, y, results, _swings, _labels)
        if lbl not in ("Start", "Final") and si >= 10.0
    ]
    _swing_candidates.sort(key=lambda t: t[3], reverse=True)
    _top_plays = _swing_candidates[:5]
    if _top_plays:
        n_x   = [t[0] for t in _top_plays]
        n_y   = [t[1] for t in _top_plays]
        n_txt = [t[2] for t in _top_plays]
        fig.add_trace(go.Scatter(
            x=n_x, y=n_y,
            mode="markers+text",
            marker=dict(size=9, symbol="star", color="#f5a623"),
            text=n_txt, textposition="top center",
            textfont=dict(size=8, color="#f5a623"),
            name="Key plays", hoverinfo="skip",
        ))

    # Collect half-inning boundaries for divider lines and centered tick labels
    _dividers: list[tuple[int, str]] = []
    prev_inn = None
    if "inn_label" in wp_df.columns:
        for _, row in wp_df.iterrows():
            inn = str(row.get("inn_label") or "")
            if inn and inn != prev_inn:
                if inn == "Start":
                    prev_inn = inn
                    continue
                xi = int(row["play_idx"])
                _dividers.append((max(0, xi - 1), inn))
                prev_inn = inn

    # Draw vertical lines at half-inning boundaries
    for dx, dl in _dividers:
        if dl != "Final" and dx > 0:
            fig.add_shape(
                type="line", xref="x", yref="paper",
                x0=dx, x1=dx, y0=0, y1=1,
                line=dict(color="rgba(128,128,128,0.25)", width=1, dash="dot"),
            )

    # Tick labels centered in each half-inning interval
    max_x_val = max(x) if x else 1
    _non_final = [(dx, dl) for dx, dl in _dividers if dl != "Final"]
    _final_dx  = next((dx for dx, dl in _dividers if dl == "Final"), max_x_val)
    tick_vals: list[float] = []
    tick_text: list[str]   = []
    for i, (dx, dl) in enumerate(_non_final):
        next_dx = _non_final[i + 1][0] if i + 1 < len(_non_final) else _final_dx
        tick_vals.append((dx + next_dx) / 2.0)
        tick_text.append(dl)

    display_title = title or "Win Probability"
    fig.update_layout(
        title=dict(text=display_title, x=0.5, xanchor="center"),
        xaxis=dict(
            tickmode="array",
            tickvals=tick_vals,
            ticktext=tick_text,
            tickfont=dict(size=9),
            showgrid=False,
        ),
        yaxis=dict(
            title=None,
            range=[0, 100],
            tickvals=[0, 50, 100],
            ticktext=["100%", "50%", "100%"],
            showgrid=True,
            gridcolor="rgba(128,128,128,0.15)",
        ),
        height=400,
        showlegend=False,
        margin=dict(l=55, r=10, t=55, b=40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
        annotations=[
            dict(
                text=f"<b>{away_team}</b>",
                x=0, xref="paper", xanchor="left",
                y=100, yref="y", yanchor="top",
                showarrow=False, font=dict(size=10, color=away_hex),
                xshift=-54, yshift=-5,
            ),
            dict(
                text=f"<b>{home_team}</b>",
                x=0, xref="paper", xanchor="left",
                y=0, yref="y", yanchor="bottom",
                showarrow=False, font=dict(size=10, color=home_hex),
                xshift=-54, yshift=5,
            ),
        ],
    )
    return fig

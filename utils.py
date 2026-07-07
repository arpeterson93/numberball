"""Derived stats, constants, and chart helpers for Numberball."""
from __future__ import annotations

import math
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


def _load_brc_table() -> None:
    global _BRC_RUN_LOOKUP
    try:
        _bdf = pd.read_csv("import_BRC.csv")
        if "Situation" not in _bdf.columns or "Runs" not in _bdf.columns:
            return
        cols = list(_bdf.columns)
        lookup: dict[tuple[str, str, int], tuple[float, str, int]] = {}
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
            lookup[(result, before_obc, outs)] = (runs, new_obc, eouts)
        _BRC_RUN_LOOKUP = lookup
    except FileNotFoundError:
        pass


_load_brc_table()


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
# Loaded from result_frequencies.csv (generated by compute_result_frequencies.py).
# Falls back to equal weights over BRC-known results if the CSV is absent.
_LI_AVG_PROBS: dict[str, float] = {}

# Game-state frequency weights for the LI denominator.
# Loaded from state_frequencies.csv (generated by compute_state_frequencies.py).
# Falls back to equal weights across all states if the CSV is absent.
_STATE_WEIGHTS: dict[tuple[int, int, str], float] = {}

_AVG_WP_SWING: float | None = None  # computed lazily on first leverage call


def _load_result_frequencies() -> None:
    global _LI_AVG_PROBS
    try:
        _rdf = pd.read_csv("result_frequencies.csv")
        if "result" in _rdf.columns and "probability" in _rdf.columns:
            _LI_AVG_PROBS = dict(zip(_rdf["result"].astype(str), _rdf["probability"].astype(float)))
            return
    except FileNotFoundError:
        pass
    # Fallback: equal weight over all results present in the BRC lookup
    known = {r for (r, _, _) in _BRC_RUN_LOOKUP} if _BRC_RUN_LOOKUP else set()
    if known:
        w = 1.0 / len(known)
        _LI_AVG_PROBS = {r: w for r in known}


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


_load_result_frequencies()
_load_state_frequencies()


def _wp_post_play(result: str, remaining: int, outs: int, obc: str, batting_lead: int) -> float:
    """WP for the batting team after a single result from (remaining, outs, obc, batting_lead)."""
    entry = _BRC_RUN_LOOKUP.get((result, obc, outs))
    if entry is not None:
        runs_f, new_obc, eouts = entry
        new_outs = outs + eouts
    else:
        new_obc, runs_int = advance_runners(result, obc, outs)
        runs_f   = float(runs_int)
        new_outs = outs + outs_added(result)
    new_outs = min(new_outs, 3)
    new_bl   = batting_lead + int(round(runs_f))
    if new_outs < 3:
        return get_win_probability_interpolated(remaining, new_outs, new_obc, new_bl) or 0.5
    if remaining > 1:
        return 1.0 - (get_win_probability_interpolated(remaining - 1, 0, "000", -new_bl) or 0.5)
    return 1.0 if new_bl > 0 else (0.5 if new_bl == 0 else 0.0)


def _compute_avg_wp_swing() -> None:
    """Populate _AVG_WP_SWING: frequency-weighted mean expected |WP change| per PA.

    Each game state is weighted by how often it appears in real play data
    (from state_frequencies.csv). Falls back to equal weighting if that file
    is absent.
    """
    global _AVG_WP_SWING
    if not _WP_BY_STATE:
        _AVG_WP_SWING = 0.04
        return
    total      = 0.0
    weight_sum = 0.0
    for (rem, outs, obc_s) in _WP_BY_STATE:
        wp_cur = get_win_probability_interpolated(rem, outs, obc_s, 0) or 0.5
        swing  = sum(
            prob * abs(_wp_post_play(res, rem, outs, obc_s, 0) - wp_cur)
            for res, prob in _LI_AVG_PROBS.items()
        )
        w = _STATE_WEIGHTS.get((rem, outs, obc_s), 1.0)
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
    wp0  = get_win_probability_interpolated(rem0, 0, "000", 0) or 0.5
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
            wp_bat      = get_win_probability_interpolated(rem, nxt_outs, nxt_obc, bat_score - fld_score) or 0.5
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
                    opp_wp  = get_win_probability_interpolated(nxt_rem, 0, "000", -(bat_score - fld_score)) or 0.5
                    home_wp = (1.0 - opp_wp) if is_home else opp_wp
                else:
                    home_wp = 1.0 if (home_score > away_score) else (0.5 if home_score == away_score else 0.0)
            else:
                rem     = remaining_half_innings(inning, half, innings)
                wp_bat  = get_win_probability_interpolated(rem, new_outs, new_obc_s, bat_score - fld_score) or 0.5
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
    """1 if pitch crosses the 1000/1 border (850+ ↔ 150-), 0 otherwise, NaN for first."""
    vals = group.astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(vals)):
        prev, curr = vals[i - 1], vals[i]
        wrapped = (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850)
        results.append(1.0 if wrapped else 0.0)
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
    df["pitch_approach"] = pd.NA
    df["pitch_wraparound"] = pd.NA
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
        # Second derivative and approach - re-read df to pick up pitch_circ_delta
        sw_df2 = df[sw]
        gk_pit2 = (sw_df2["game_id"].astype(str) + "|" + sw_df2["pitcher_name"].fillna(""))
        df.loc[sw, "pitch_circ_delta2"] = sw_df2.groupby(
            gk_pit2, group_keys=False
        )["pitch_circ_delta"].apply(lambda g: g.abs().diff().abs())
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


def get_sheet_name(sheet_url: str) -> str:
    """Return the Google Sheet title for display, falling back to a short ID."""
    import re, urllib.request
    match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    if not match:
        return sheet_url[-40:]
    sheet_id = match.group(1)
    try:
        req = urllib.request.Request(
            f"https://docs.google.com/spreadsheets/d/{sheet_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read(8192).decode("utf-8", errors="ignore")
        m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        if m:
            title = re.sub(r"\s*[-–]\s*Google Sheets$", "", m.group(1)).strip()
            if title:
                return title
    except Exception:
        pass
    return f"Sheet {sheet_id[:12]}…"


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


# Fixed 0-indexed layout of the two swing-range tables on the Gameday tab of the
# stadium/scrimmage sheets (both column-header rows share one row; name at <col>,
# low/high at +2/+3). Positional so a deleted "Result" label can't break parsing.
_RANGE_HEADER_ROW = 14   # sheet row 15: Result / Rng / Low / High labels
_RANGE_NORMAL_COL = 6    # normal-swing name (col G); low = col 8 (I), high = col 9 (J)
_RANGE_BUNT_COL   = 11   # bunt name (col L);        low = col 13 (N), high = col 14 (O)


def parse_result_ranges_from_sheet(sheet_url: str):
    """Fetch and parse result range tables from a public Google Sheet.

    Returns (normal_ranges, bunt_ranges, batter_name, pitcher_name).
    bunt_ranges is None if no second Result table is found.
    Tries the gid-based URL first; if no parseable ranges are found, falls
    back to the 'Gameday' tab by name (scrimmage sheets store ranges there).
    """
    import re
    import urllib.parse as _uparse
    sheet_id_match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    gid_match = re.search(r"[?&]gid=(\d+)", sheet_url)
    if not sheet_id_match:
        raise ValueError("Could not parse a Google Sheets ID from the URL.")
    sheet_id = sheet_id_match.group(1)
    gid = gid_match.group(1) if gid_match else "0"

    def _fetch_raw(url: str) -> pd.DataFrame:
        return pd.read_csv(url, header=None, dtype=str)

    def _scan_and_parse(df: pd.DataFrame):
        # The two range tables sit at a fixed position on the Gameday tab (see
        # _RANGE_* constants). Read them positionally rather than by header text,
        # so a deleted "Result" label doesn't break parsing. Gate on the
        # structural Low/High labels to confirm we're on the range tab (and let a
        # wrong tab, e.g. a scrimmage's live tab, fall through to the fallback).
        try:
            _lo = str(df.iloc[_RANGE_HEADER_ROW, _RANGE_NORMAL_COL + 2]).strip().lower()
            _hi = str(df.iloc[_RANGE_HEADER_ROW, _RANGE_NORMAL_COL + 3]).strip().lower()
        except Exception:
            return None, None, None
        if _lo != "low" or _hi != "high":
            return None, None, None

        def _table(col):
            out: list[tuple[str, int, int]] = []
            for i in range(_RANGE_HEADER_ROW + 1, len(df)):
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

        return _table(_RANGE_NORMAL_COL), _table(_RANGE_BUNT_COL), None

    def _cell(df, r, c):
        try:
            v = str(df.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else ""
        except Exception:
            return ""

    # First attempt: gid-based URL
    gid_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    raw = _fetch_raw(gid_url)
    normal_ranges, bunt_ranges, result_headers = _scan_and_parse(raw)

    # If no parseable ranges, fall back to Gameday tab by its known gid
    # (same gid used by parse_gameplay_from_sheet; export URL preserves full column width
    # so _cell row/col offsets match the live-game path)
    if not normal_ranges:
        gameday_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid=1498066521"
        )
        try:
            raw = _fetch_raw(gameday_url)
            normal_ranges, bunt_ranges, result_headers = _scan_and_parse(raw)
        except Exception:
            pass

    if not normal_ranges:
        raise ValueError("Result table found but no rows could be parsed.")

    batter_name  = _cell(raw, 11, 7)
    pitcher_name = _cell(raw, 10, 7)

    # Read swing type and Infield In toggle from Gameplay tab (gid 533199361)
    swing_type = "Normal Swing"
    infield_in = False
    try:
        gameplay_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid=533199361"
        )
        gp = _fetch_raw(gameplay_url)
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

    For each recent delta², produces two projections: one where the last delta grows
    by that amount and one where it shrinks, covering both acceleration and deceleration.
    """
    if len(recent_vals) < 3:
        return []
    deltas  = [circular_signed_delta(recent_vals[i - 1], recent_vals[i]) for i in range(1, len(recent_vals))]
    delta2s = [abs(abs(deltas[i]) - abs(deltas[i - 1])) for i in range(1, len(deltas))]
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


def _diff_score_array(result_ranges: list, metric: str) -> "numpy.ndarray":
    """Precompute a length-501 score array indexed by circular diff value."""
    import numpy as np
    arr = np.zeros(501)
    for d in range(501):
        r = _diff_to_result(d, result_ranges)
        arr[d] = (1.0 if r in _OBR else 0.0) if metric == "obp" else float(_SLG_WEIGHTS.get(r, 0))
    return arr


def suggest_swing(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    weights: list[float] | None = None,
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
        _diff_score_array(result_ranges, metric),
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
        _diff_score_array(result_ranges, metric),
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


def optimal_swing_chart(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    title: str = "Expected Score by Swing Value",
    compact: bool = False,
    weights: list[float] | None = None,
) -> go.Figure:
    """1-row gradient heatmap showing expected OBP or SLG for every possible swing value.

    Marks both the best value (green vline) and the counter/worst value (orange dotted vline).
    weights: optional per-value relevance weights (same length as recent_opp_vals).
    """
    import numpy as np
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals, weights),
        _diff_score_array(result_ranges, metric),
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
            delta_counts = h.get("_delta_counts")
            prior_pitch_z = h.get("_prior_pitch_for_zone")
            if delta_counts is not None and prior_pitch_z is not None:
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
            def _bound(x, label, anchor):
                fig.add_shape(type="line", x0=x, x1=x, y0=y0, y1=y1,
                              line=dict(color="rgba(255,255,255,0.85)", width=1.5))
                fig.add_annotation(
                    x=x, y=y, text=f"<b>{label}</b>",
                    showarrow=False,
                    xanchor=anchor, yanchor="middle",
                    font=dict(size=9, color="rgba(255,255,255,0.95)"),
                    bgcolor="rgba(0,0,0,0)",
                )

            _colored_zone(lo, hi, _GREEN)
            _colored_zone(lo2, hi2, _GREEN2)
            for _er_lo, _er_hi in h.get("extra_ranges", []):
                _colored_zone(_er_lo, _er_hi, _GREEN)

            if lo is not None and hi is not None:
                _bound(lo, lo, "right")
                _bound(hi, hi, "left")
            if lo2 is not None and hi2 is not None:
                _bound(lo2, lo2, "right")
                _bound(hi2, hi2, "left")
            for _er_lo, _er_hi in h.get("extra_ranges", []):
                if _er_lo is not None and _er_hi is not None:
                    _bound(_er_lo, _er_lo, "right")
                    _bound(_er_hi, _er_hi, "left")

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
        half = bucket_size // 2
        d_abs = df_s[delta_col].abs()
        mask = ((d_abs - float(prior_delta_abs)).abs() <= half).fillna(False)
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
        half = bucket_size // 2
        m1 = ((df_s["_d1"] - float(prior_delta_1)).abs() <= half).fillna(False)
        m2 = ((df_s["_d2"] - float(prior_delta_2)).abs() <= half).fillna(False)
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
    """Binomial Z-score: how many std-devs the observed proportion exceeds uniform 1/n_bkts."""
    if n <= 0 or n_bkts <= 0:
        return 0.0
    p0  = 1.0 / n_bkts
    std = (p0 * (1 - p0) / n) ** 0.5
    return (prob - p0) / std if std > 0 else 0.0


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
        half = bucket_size // 2
        mask = ((d - float(prior_delta)).abs() <= half).fillna(False)
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
# Symmetric cutoffs: score >= scouting_min -> scouting (green), <= anti_max ->
# anti (red), else neutral (yellow). A flat-book pitcher has no favored buckets,
# so every score sits near 0 -> neutral. Tunable with the inspector, not inline.
SCOUT_PP_THRESHOLDS = {"scouting_min": +0.15, "anti_max": -0.15}
MIN_SCORED = 3  # need this many career eligible events before showing any light
SCOUT_SCORING_VERSION = 2  # bump when the per-pitch score formula changes


def scouting_cache_sig() -> tuple:
    """Cache-key signature for the page's @st.cache_data stoplight loaders. They
    call into this module, so st.cache_data can't see score-formula or threshold
    edits on its own - passing this into their args busts the cache when either
    changes (so tuning is live)."""
    return (SCOUT_SCORING_VERSION,
            SCOUT_PP_THRESHOLDS["scouting_min"], SCOUT_PP_THRESHOLDS["anti_max"])


def _score_from_probs(p: "np.ndarray", observed: int) -> float:
    """Uniform-referenced score = ln(k * p_obs). > 0 means they hit a bucket
    their book favors (likelier than a random 1/k bucket) = following scouting;
    < 0 means a disfavored bucket = defying it; 0 = league-random.

    Worked example: p = [0.1, 0.2, 0.2, 0.2, 0.3], k = 5. Hitting the 30% bucket
    -> ln(5*0.3) = ln(1.5) = +0.405 (scouting). Hitting the 10% bucket ->
    ln(5*0.1) = ln(0.5) = -0.693 (anti). The uniform 20% bucket -> ln(1) = 0.
    """
    return float(np.log(len(p) * p[observed]))


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


def _recency_indications(sw, value_col: str, hz_bkt: int, dd_bkt: int) -> dict:
    """Build {signal: (context_keys, outcome_buckets, k)} for one player frame.

    Shared by the aggregate stoplight and the per-pitch inspector so the two can
    never drift. Fixed-bucket conditioning only (ignores the Centered toggle).
    """
    n = len(sw)
    hz_n = max(1, 1000 // hz_bkt)
    dd_n = max(1, 500 // dd_bkt)

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
    delta_abs = np.full(n, np.nan)
    if n > 1:
        raw = vals[1:].astype(float) - vals[:-1].astype(float)
        raw = np.where(raw > 500, raw - 1000, raw)
        raw = np.where(raw < -500, raw + 1000, raw)
        delta_abs[1:] = np.where(same1[1:], np.abs(raw), np.nan)
    delta_bkt = pd.cut(pd.Series(delta_abs), bins=list(range(0, 501, dd_bkt)),
                       labels=False, right=True, include_lowest=True).to_numpy()
    delta100 = pd.cut(pd.Series(delta_abs), bins=_DELTA_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    # Prior |diff| bucket - context for the diff -> Delta indication.
    diff_abs = sw["diff"].abs().to_numpy() if "diff" in sw.columns else np.full(n, np.nan)
    diff_bkt = pd.cut(pd.Series(diff_abs), bins=_DIFF_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    outs = sw["outs"].to_numpy() if "outs" in sw.columns else np.full(n, np.nan)
    obc = sw["obc"].to_numpy() if "obc" in sw.columns else np.array([None] * n, dtype=object)
    fp_app = sw["is_fp_app"].to_numpy() if "is_fp_app" in sw.columns else np.zeros(n, dtype=bool)
    fp_inn = sw["is_fp_inn"].to_numpy() if "is_fp_inn" in sw.columns else np.zeros(n, dtype=bool)

    zb = [int(z) for z in zone_bkt]
    zb9 = [int(z) for z in zone9]
    db = [_int_or_none(delta_bkt[i]) for i in range(n)]
    d100 = [_int_or_none(delta100[i]) for i in range(n)]
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
    # Prior diff -> Delta: prior |diff| bucket -> |Delta| in fixed 100-unit bins.
    ind["Prior diff → Δ"] = (
        [fb[i - 1] if i >= 1 else None for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
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
) -> dict:
    """Per-indication scouting-recency stoplight for one player.

    Wired for the pitcher side first; kept value_col-agnostic (pitch vs swing)
    so the batter side is a follow-up, not a rewrite. Returns
    {signal: {"avg": float|None, "n_scored": int, "state": "red"|"yellow"|"green"|None}}
    for the nine covered indications. Uses fixed-bucket conditioning only - it
    deliberately ignores the page's Centered toggle, measuring general
    predictability rather than the exact displayed tooltip.
    """
    sw = _recency_frame(df, value_col)
    if sw is None:
        return {}
    ind = _recency_indications(sw, value_col, hz_bkt, dd_bkt)
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


def _recency_labelers(signal: str, hz_bkt: int, dd_bkt: int):
    """Return (context_fmt, outcome_fmt): functions turning a signal's raw bucket
    indices into human-readable ranges/values for the inspector table."""
    def zone(b):
        b = int(b)
        return f"{b * hz_bkt + 1}-{min((b + 1) * hz_bkt, 1000)}"

    def zone9(b):
        return ZONES[int(b)][2]

    def delta(b):
        b = int(b)
        return f"{b * dd_bkt}-{(b + 1) * dd_bkt}"

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
        "Prior diff → Δ":       (diff, delta100),
        "Outs":                 (outs, zone9),
        "Base state":           (base, zone9),
        "1st pitch appearance": (lambda c: "1st of PA", zone9),
        "1st pitch inning":     (lambda c: "1st of inning", zone9),
    }
    return table.get(signal, (str, str))


def scouting_recency_detail(
    df: pd.DataFrame, value_col: str, signal: str, window_n: int, hz_bkt: int, dd_bkt: int,
) -> dict:
    """Per-pitch surprisal trace for one indication (inspector view).

    Returns {rows, scores, n_scored, avg, state, window_n, k} where each row
    carries game_id/id plus the {ctx, obs, p_obs, H, s, score, in_window} calc.
    """
    empty = {"rows": [], "scores": [], "n_scored": 0, "avg": None,
             "state": None, "window_n": window_n, "k": 0}
    sw = _recency_frame(df, value_col)
    if sw is None:
        return empty
    ind = _recency_indications(sw, value_col, hz_bkt, dd_bkt)
    if signal not in ind:
        return empty
    ctx, out, k = ind[signal]
    detail = _surprisal_walk_detail(ctx, out, k)
    gcode = (sw["game_code"] if "game_code" in sw.columns else sw["game_id"]).tolist()
    pv = sw[value_col].astype(int).tolist()
    sv = sw["swing"].tolist() if "swing" in sw.columns else [None] * len(sw)
    dv = sw["diff"].tolist() if "diff" in sw.columns else [None] * len(sw)
    ctx_fmt, obs_fmt = _recency_labelers(signal, hz_bkt, dd_bkt)

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
    prob = [r["p_obs"] * 100.0 for r in rows]
    base = 100.0 / k
    green_lo = float(np.exp(SCOUT_PP_THRESHOLDS["scouting_min"]) / k * 100.0)
    red_hi = float(np.exp(SCOUT_PP_THRESHOLDS["anti_max"]) / k * 100.0)
    ymax = max(max(prob), green_lo) * 1.10
    ymin = max(0.0, min(min(prob), red_hi) * 0.90)

    fig.add_hrect(y0=green_lo, y1=ymax, fillcolor="#2e7d32", opacity=0.10, line_width=0)
    fig.add_hrect(y0=red_hi, y1=green_lo, fillcolor="#f9a825", opacity=0.09, line_width=0)
    fig.add_hrect(y0=ymin, y1=red_hi, fillcolor="#c62828", opacity=0.10, line_width=0)
    fig.add_hline(y=base, line=dict(color="rgba(255,255,255,0.45)", width=1, dash="dot"))
    if window_n and n:
        fig.add_vrect(x0=max(0.5, n - window_n + 0.5), x1=n + 0.5,
                      fillcolor="rgba(255,255,255,0.06)", line_width=0)

    _cls_c = {"scouting": "#2e7d32", "neutral": "#f9a825", "anti": "#c62828"}
    colors = [_cls_c.get(r["cls"], "#9e9e9e") for r in rows]
    cd = [[r["pitch_val"], r.get("swing_val"), r.get("diff_val"), r.get("game")] for r in rows]
    fig.add_trace(go.Scatter(
        x=x, y=prob, mode="lines+markers",
        line=dict(color="rgba(200,200,200,0.45)", width=1),
        marker=dict(size=6, color=colors),
        customdata=cd,
        hovertemplate=("pitch %{customdata[0]} · swing %{customdata[1]} · diff %{customdata[2]}"
                       "<br>game %{customdata[3]} · P=%{y:.1f}%<extra></extra>"),
    ))
    if n >= 3:
        ma_win = min(n, max(5, window_n // 2)) if window_n else min(n, 10)
        ma = pd.Series(prob).rolling(ma_win, min_periods=1).mean().tolist()
        fig.add_trace(go.Scatter(x=x, y=ma, mode="lines", hoverinfo="skip",
                                 line=dict(color="rgba(255,255,255,0.85)", width=2)))

    view = 20  # initial x-window width (keeps mobile readable); pan left for history
    fig.update_layout(
        dragmode="pan",
        xaxis=dict(range=[max(0.5, n - view + 0.5), n + 0.5], title="pitch (older → newer)"),
        yaxis=dict(range=[ymin, ymax], title="P(bucket) %", fixedrange=True),
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
        half = bucket_size // 2
        m_curr = ((d - float(prior_delta_2)).abs() <= half).fillna(False)
        m_prev = ((d.shift(1) - float(prior_delta_1)).abs() <= half).fillna(False)
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
        half = bucket_size // 2
        mask = ((d - float(prior_delta)).abs() <= half).fillna(False)
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
        half = bucket_size // 2
        m_curr = ((d - float(prior_delta_2)).abs() <= half).fillna(False)
        m_prev = ((d.shift(1) - float(prior_delta_1)).abs() <= half).fillna(False)
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
    obr_max = max((hi for result, lo, hi in ranges if result in _OBR), default=0)
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
            "logo_url":  _str(row.get("Logo URL")),
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
            "logo_url":            _str(row.get("Postimg Logo")),
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
        })
    return games


def read_mln_plays_from_sheet(sheet_id: str, tab: str = "Plays") -> list[dict]:
    """Read a plays tab from an MLN sheet. Archive uses 'Plays'; current season uses 'Plays (Raw)'.

    Away/Home contain Team IDs (e.g. T1009); Pitcher/Batter/Catcher/Runner contain
    MLN player IDs. Caller resolves these to names via get_mln_teams/players_for_lookup().
    """
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
        obc = BRC_TO_OBC.get(brc, "Empty")

        plays.append({
            "league":      "MLN",
            "season":      _safe_int(row.get("Season")),
            "season_type": _str(row.get("Season.1")),
            "game_code":   game_code,
            "play_num":    play_num,
            "timestamp":   _str(row.get("Timestamp")),
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
            "play_code":  _str(row.get("Playcode")),
            "pitcher_id": _safe_int(row.get("Pitcher")),
            "catcher_id": _safe_int(row.get("Catcher")),
            "pos":        _str(row.get("Pos") or row.get("Pos*")),
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
            "obc":        obc,
        })
    return plays


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

def compute_pitcher_stats(df: pd.DataFrame) -> list[dict]:
    """Compute per-pitcher behavioral stats from an enriched plays DataFrame."""
    import datetime
    sw = df[df["swing"].notna()]
    rows = []
    for pitcher, grp in sw.groupby("pitcher_name"):
        deltas   = grp["pitch_circ_delta"].dropna()
        delta2s  = grp["pitch_circ_delta2"].dropna()
        approach = grp["pitch_approach"].dropna()
        # Wraparound %: of pitches where the previous pitch was in the boundary zone
        # (>=850 or <=150), how often did they actually cross to the other side?
        _wrap_eligible = 0
        _wrap_crossed  = 0
        for _, game_grp in grp.groupby("game_id"):
            pitches = game_grp.sort_values("id")["pitch"].astype(int).tolist()
            for i in range(1, len(pitches)):
                prev, curr = pitches[i - 1], pitches[i]
                if prev >= 850 or prev <= 150:
                    _wrap_eligible += 1
                    if (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850):
                        _wrap_crossed += 1
        wraparound_pct = round(_wrap_crossed / _wrap_eligible * 100, 2) if _wrap_eligible else None
        rows.append({
            "pitcher_name":   pitcher,
            "ab_count":       len(grp),
            "avg_abs_delta":  round(float(deltas.abs().mean()), 3) if not deltas.empty else None,
            "avg_delta2":     round(float(delta2s.mean()), 3)      if not delta2s.empty else None,
            "shadow_pct":     round(float(approach.mean() * 100), 2) if not approach.empty else None,
            "meme_rate":      round(float(grp["is_meme_pitch"].mean() * 100), 2),
            "wraparound_pct": wraparound_pct,
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
    _we, _wc = 0, 0
    for _, g in sw.groupby("game_id"):
        pitches = g.sort_values("id")["pitch"].astype(int).tolist()
        for i in range(1, len(pitches)):
            prev, curr = pitches[i - 1], pitches[i]
            if prev >= 850 or prev <= 150:
                _we += 1
                if (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850):
                    _wc += 1
    return {
        "avg_abs_delta":  float(deltas.abs().mean())   if not deltas.empty  else None,
        "avg_delta2":     float(delta2s.mean())        if not delta2s.empty else None,
        "shadow_pct":     float(approach.mean() * 100) if not approach.empty else None,
        "meme_rate":      float(sw["is_meme_pitch"].mean() * 100),
        "wraparound_pct": (_wc / _we * 100)            if _we else None,
    }


_PERCENTILE_STATS = [
    ("Avg |Δ|",      "avg_abs_delta",  lambda v: f"{v:.1f}"),
    ("Avg |Δ²|",     "avg_delta2",     lambda v: f"{v:.1f}"),
    ("Shadow %",     "shadow_pct",     lambda v: f"{v:.1f}%"),
    ("Wraparound %", "wraparound_pct", lambda v: f"{v:.1f}%"),
    ("Meme Rate",    "meme_rate",      lambda v: f"{v:.1f}%"),
]


def pitcher_percentile_card(
    pitcher_name: str,
    stats_df: pd.DataFrame,
    recent_vals: dict | None = None,
    recent_n: int | None = None,
) -> go.Figure | None:
    """
    Compact pill-bar percentile chart.
    Bar = career percentile in the qualified pool (≥100 AB).
    Gold needle = where recent stats (recent_vals) fall in that same pool.
    """
    import math

    if stats_df.empty or pitcher_name not in stats_df["pitcher_name"].values:
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
    "avg_delta":      {"label": "Avg |Delta|",   "col": "pitch_circ_delta",  "scale": "abs",   "y_range": [0, 500], "y_title": "Delta"},
    "avg_delta2":     {"label": "Avg |Delta^2|", "col": "pitch_circ_delta2", "scale": "abs",   "y_range": [0, 500], "y_title": "Delta^2"},
    "shadow_pct":     {"label": "Shadow %",      "col": "pitch_approach",    "scale": "pct",   "y_range": [0, 100], "y_title": "Shadow %"},
    "wraparound_pct": {"label": "Wraparound %",  "col": "pitch_wraparound",  "scale": "pct",   "y_range": None, "y_title": "Wraparound %"},
    "meme_rate":      {"label": "Meme Rate %",   "col": "is_meme_pitch",     "scale": "pct",   "y_range": None, "y_title": "Meme Rate %"},
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
    ma = raw.rolling(window=window, min_periods=1).mean()
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

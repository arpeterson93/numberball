"""Build the static Key Moments feed served from docs/ on GitHub Pages.

Reads the MLN public Google Sheet directly (no Supabase round-trip), replays
every game play-by-play, computes win probability added and leverage with the
existing utils.py machinery, applies the key-moment tag rules, and writes:

    docs/data/key_moments.json   season-wide feed of plays where is_key_moment
    docs/data/plays_<NN>.json    every play of session NN (favorites mode)
    docs/data/players.json       roster lookup for the favorites picker
    docs/data/meta.json          build timestamp, session list, shared lookups

Every play is scored and tagged; the split into two files is only about payload
size and git churn. key_moments.json is what the page loads on boot. The
per-session play files are fetched lazily, and only the in-progress session's
file changes from one build to the next, so finished sessions stop churning.

Run from the repo root (utils.py loads win_probability_table.csv and friends
relative to the working directory):

    python key_moments_build.py

Exits non-zero on any hard error so a GitHub Actions run fails visibly rather
than publishing stale or empty data.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import utils

# ── configuration ─────────────────────────────────────────────────────────────

MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"
CURRENT_SEASON = 13
MLN_INNINGS = utils.LEAGUE_INNINGS.get("MLN", 6)

OUT_DIR = os.path.join("docs", "data")

# A team's secondary color, for when its primary reads as too close to an
# opponent's primary in the same game (docs/js/app.js's gameTeamColors) - not
# a column on the Teams sheet, so it lives here as a hand-maintained constant
# instead. Keyed by the same team abbreviation primary_hex/logo_url already
# use. A team missing from this dict simply has no secondary to fall back to
# (gameTeamColors treats that slot as staying on primary through every tier).
# Alex's reference list, one per current team.
SECONDARY_HEX: dict[str, str] = {
    "ACP": "febc11",
    "MIA": "fb5a98",
    "ASS": "280730",
    "GHG": "34350f",
    "HMH": "d20522",
    "CAR": "43b399",
    "BBEG": "36549c",
    "RLY": "dce14f",
    "KC": "4788ff",
    "SMD": "f08223",
    "SUN": "ac8f35",
    "HFX": "1c4c9a",
    "POR": "085d05",
    "REK": "cd8d01",
    "GRZ": "525252",
    "RC": "8eff1c",
}

# Inclusion thresholds - tuned against real per-session volume, see the
# "threshold" summary printed at the end of a build.
# 1.5 matches SCOREBOARD_HOT_LEVERAGE in docs/js/app.js, so "high leverage"
# means the same number in the feed and on the game cards. Retuned from 2.0
# alongside the leverage denominator refresh (_AVG_WP_SWING now uses
# result_ranges_re24.csv and simulated visitation weights), which lowered every
# leverage number in the app by about 11%.
LEVERAGE_THRESHOLD = 1.5
WPA_THRESHOLD = 0.12   # 12 percentage points of win probability

# Leverage of a game's very first plate appearance - inning 1, top, 0 outs,
# bases empty, tied - used by _scoreboard for a scheduled game that hasn't
# had a play recorded yet. Every not-started game shares this exact same
# state, so it's one constant computed here at import time (utils.
# compute_leverage_re24/remaining_half_innings are pure lookups over the
# bundled RE24 CSV, no sheet I/O) rather than re-derived per game per build.
# Depends only on MLN_INNINGS and result_ranges_re24.csv - update this if
# either ever changes, same as any other derived constant here.
LEVERAGE_AT_GAME_START = round(
    utils.compute_leverage_re24(utils.remaining_half_innings(1, "top", MLN_INNINGS), 0, "000", 0) or 0,
    2,
)

# ── result taxonomy ───────────────────────────────────────────────────────────

HIT_CODES = {
    "1B", "IF1B", "2B", "3B", "HR", "1BWH", "1BWH2", "2BWH", "B1B", "B1BWH",
}

HITTING_CODES = HIT_CODES | {"BB", "IBB", "SB", "SB2", "SB3", "SB4", "Balk"}

PITCHING_CODES = {
    "K", "GO", "FO", "PO", "DP", "DP21", "DP31", "DPH1", "DPRun", "LODP",
    "CS", "CS2", "CS3", "CS4", "GORA", "DFO", "BGO", "TP", "LOTP", "FC",
    "FC3rd", "FCH", "BFC", "LO", "LCO",
}

SAC_CODES = {"SacF", "DSacF", "SacB"}

STEAL_SUCCESS_CODES = {"SB", "SB2", "SB3", "SB4"}
CAUGHT_STEALING_CODES = {"CS", "CS2", "CS3", "CS4"}
DOUBLE_PLAY_CODES = {"DP", "DP21", "DP31", "DPH1", "DPRun", "LODP"}
TRIPLE_PLAY_CODES = {"TP", "LOTP"}

RESULT_LABELS = {
    "1B": "Single",
    "IF1B": "Infield Single",
    "1BWH": "Single",
    "1BWH2": "Single",
    "B1B": "Bunt Single",
    "B1BWH": "Bunt Single",
    "2B": "Double",
    "2BWH": "Double",
    "3B": "Triple",
    "HR": "Home Run",
    "BB": "Walk",
    "IBB": "Intentional Walk",
    "Balk": "Balk",
    "SB": "Stolen Base",
    "SB2": "Stolen Base",
    "SB3": "Steal of Third",
    "SB4": "Steal of Home",
    "CS": "Caught Stealing",
    "CS2": "Caught Stealing",
    "CS3": "Caught Stealing",
    "CS4": "Caught Stealing",
    "K": "Strikeout",
    "GO": "Groundout",
    "BGO": "Bunt Groundout",
    "FO": "Flyout",
    "DFO": "Deep Flyout",
    "PO": "Popout",
    "LO": "Lineout",
    "LCO": "Line Out (Caught)",
    "GORA": "Groundout, Runner Advances",
    "DP": "Double Play",
    "DP21": "Double Play",
    "DP31": "Double Play",
    "DPH1": "Double Play at Home",
    "DPRun": "Double Play (Runner)",
    "LODP": "Lineout Double Play",
    "TP": "Triple Play",
    "LOTP": "Triple Play",
    "FC": "Fielder's Choice",
    "FC3rd": "Fielder's Choice at Third",
    "FCH": "Fielder's Choice at Home",
    "BFC": "Bunt Fielder's Choice",
    "SacF": "Sacrifice Fly",
    "DSacF": "Deep Sacrifice Fly",
    "SacB": "Sacrifice Bunt",
    "bAuto": "Auto (Batter)",
    "pAuto": "Auto (Pitcher)",
}

# Terse on-field marker labels for the ball-flight landing point (C2) -
# RESULT_LABELS is display copy ("Fielder's Choice at Third") and far too long
# at marker size. Anything absent from this table falls back to the raw
# result code, which is already terse for most (1B, 2B, GO, FO, HR, ...).
RESULT_SHORT = {
    "1BWH": "1B", "1BWH2": "1B", "B1B": "1B", "B1BWH": "1B",
    "2BWH": "2B",
    "GORA": "GO", "BGO": "GO",
    "DP21": "DP", "DP31": "DP", "DPH1": "DP", "DPRun": "DP",
    "FC3rd": "FC3", "FCH": "FCH", "FCLead": "FCL",
    "SacF": "SF", "DSacF": "SF", "SacB": "SH",
    "LOTP": "TP", "LCO": "LO",
}

# The complete, closed set of tag slugs. The page renders one filter chip per
# entry, in this order, and a card's own tag pills render in this same order
# (moment_tags() below appends in exactly this sequence) - reordering this
# dict is the one place to change both.
TAG_LABELS = {
    "high_leverage": "High LI",
    "wpa": "WPA",
    "risp": "RISP",
    "run_scored": "Run scored",
    "lead_change": "Lead change",
    "bases_loaded": "Loaded bases",
    "steal": "Steal",
    "double_play": "Double Play",
    "triple_play": "Triple Play",
    "zero_diff": "0 Diff",
    "five_hundred_diff": "500 Diff",
}

# RISP is filterable but does not, by itself, make a play a key moment - a
# RISP play still needs leverage/WPA (or one of the other real triggers) to
# qualify. Every other tag in TAG_LABELS is a real inclusion trigger.
FILTER_ONLY_TAGS = {"risp"}

# Results with no batted ball to animate in the Play Scene ball-flight layer -
# the diamond-only runner animation (deriveRunnerMoves in app.js) still covers
# these unchanged. Must match compute_result_diff_bands.py's NO_FLIGHT set
# exactly; kept as a separate constant (not imported) since that script isn't
# a dependency of the build pipeline.
FLIGHT_EXCLUDED = [
    "K", "BB", "IBB", "AutoBB", "AutoK", "CS", "CS2", "CS3", "CS4",
    "SB", "SB2", "SB3", "SB4", "AutoSB", "Balk", "pAuto", "bAuto",
    "KCS", "SB32", "SB42", "SB43", "SB432",
]


# ── small helpers ─────────────────────────────────────────────────────────────

def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def _safe_player_id(raw) -> int | None:
    """Parse a scored2/scored3/scored4-style cell into a player id, or None.

    These cells use the same '0'/'-'/blank sentinels as on_first/on_second/
    on_third for "nobody".
    """
    s = str(raw or "").strip()
    if not s or s in ("0", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _scoring_names(ref: dict, play: dict, batter_id: int | None) -> list[str]:
    """Last names of runners who scored on this play, in the order they'd
    have crossed the plate: 3rd, then 2nd, then 1st. The page renders these
    as "X scores" (one) or "X, Y, Z score" (more than one).

    The batter's own run is never listed here even if the sheet ever put
    their id in one of these cells (a solo/grand-slam HR's batter is implied
    by the result and the card's headline name already showing them) - the
    scored2/scored3/scored4 cells map to the runner who started the play on
    1st/2nd/3rd respectively, per the sheet's naming.
    """
    seen: set[int] = set()
    names: list[str] = []
    for key in ("scored4", "scored3", "scored2"):  # 3rd, 2nd, 1st
        pid = _safe_player_id(play.get(key))
        if pid is None or pid == batter_id or pid in seen:
            continue
        seen.add(pid)
        names.append(_player_view(ref, pid)["last_name"])
    return names


def _parse_timestamp(raw: str | None) -> str | None:
    """Parse the sheet's 'M/D/YYYY H:MM[:SS]' timestamps into an ISO string.

    The sheet stores wall-clock time with no zone, so no zone is invented here -
    the page renders it as a plain local-looking timestamp.
    """
    if not raw:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def _diamond_svg(obc: str) -> str:
    """Scorebug base diamond as an inline SVG string.

    Geometry ported from utils.bases_diamond_svg: 2B top, 1B right, 3B left.
    Colors come from the page stylesheet via the base-diamond on/off classes.
    """
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"
    bases = (
        (10, 2, 15, 7, on_2b),    # second - top
        (18, 10, 23, 15, on_1b),  # first - right
        (2, 10, 7, 15, on_3b),    # third - left
    )
    rects = "".join(
        f'<rect class="base-diamond {"on" if filled else "off"}" x="{x}" y="{y}" '
        f'width="10" height="10" rx="1.5" transform="rotate(45 {cx} {cy})"/>'
        for x, y, cx, cy, filled in bases
    )
    return f'<svg width="30" height="30" viewBox="0 0 30 30">{rects}</svg>'


def _result_category(result: str) -> str:
    if result in HITTING_CODES:
        return "hitting"
    if result in PITCHING_CODES or result in SAC_CODES:
        return "pitching"
    return "other"


def _effective_category(result: str, runs: int) -> str:
    """Category used for both the result pill/Hitting-Pitching filter and
    who's featured on the card.

    A run-scoring out (a sacrifice fly, a productive groundout, etc.) is
    credited to the batter like a real hitting play - the pitcher isn't the
    story when the batting team just scored. Caught stealing keeps its
    normal category even if some other runner happens to score on the same
    play, since the catcher/runner matchup is still what that play is about.
    """
    if runs > 0 and result not in CAUGHT_STEALING_CODES:
        return "hitting"
    return _result_category(result)


# ── reference data ────────────────────────────────────────────────────────────

def load_reference(sheet_id: str) -> dict:
    """Load teams and players once, indexed for per-play lookups."""
    teams = utils.read_mln_teams_from_sheet(sheet_id)
    players = utils.read_mln_players_from_sheet(sheet_id, tab="Players", season=CURRENT_SEASON)
    if not teams:
        raise RuntimeError("No teams found in the MLN Teams tab.")
    if not players:
        raise RuntimeError("No players found in the MLN Players tab.")

    by_team_id = {t["team_id"]: t for t in teams if t.get("team_id")}
    by_abbrev = {t["abbrev"]: t for t in teams if t.get("abbrev")}
    by_player_id = {p["player_id"]: p for p in players if p.get("player_id")}
    # Reverse of player_id_to_name (pages/1_Games.py's _sync_plays) - the
    # league enforces no duplicate names across different player_ids, so a
    # plain dict lookup is safe (no fuzzy matching / collision handling).
    name_to_id = {p["name"]: p["player_id"] for p in players if p.get("name") and p.get("player_id")}
    return {
        "teams": teams,
        "players": players,
        "team_by_id": by_team_id,
        "team_by_abbrev": by_abbrev,
        "player_by_id": by_player_id,
        "name_to_id": name_to_id,
    }


def _team_view(ref: dict, key: str | None) -> dict:
    """Resolve a Team ID (T1001) or abbreviation (ACP) to display fields."""
    team = ref["team_by_id"].get(key) or ref["team_by_abbrev"].get(key)
    if not team:
        return {"abbrev": key or "", "full": key or "", "sub_league": ""}
    return {
        "abbrev": team.get("abbrev") or key or "",
        "full": team.get("full_team") or team.get("abbrev") or key or "",
        "sub_league": team.get("sub_league") or "",
    }


def _player_view(ref: dict, player_id: int | None) -> dict:
    if not player_id:
        return {"id": None, "name": "", "last_name": "", "rookie": False, "team": "", "hand": ""}
    p = ref["player_by_id"].get(player_id)
    if not p:
        # Traded, released, or otherwise off the current roster tab.
        return {"id": player_id, "name": f"Player {player_id}", "last_name": f"Player {player_id}",
                "rookie": False, "team": "", "hand": ""}
    return {
        "id": player_id,
        "name": p.get("name") or f"Player {player_id}",
        "last_name": p.get("last_name") or p.get("name") or f"Player {player_id}",
        "rookie": bool(p.get("is_rookie")),
        "team": p.get("team") or "",
        "hand": p.get("hand") or "",
    }


def _defense_entry(ref: dict, player_id: int | None, fallback_name: str | None = None) -> list[str] | None:
    """[full_name, last_name] for one resolved defensive position, or None
    when there's nothing to show. Both names come from _player_view (never
    split client-side - see build_moment's defense docstring) except when a
    Lineups-tab name has no matching player_id (traded/released player off
    the current roster tab) - there, fall back to the raw sheet name for
    both slots rather than dropping the label entirely.
    """
    if player_id:
        v = _player_view(ref, player_id)
        return [v["name"], v["last_name"]]
    if fallback_name:
        return [fallback_name, fallback_name]
    return None


def _defense_alignment_for_play(ref: dict, play: dict, half: str, is_final: bool,
                                 home_timeline: dict | None, away_timeline: dict | None,
                                 game_lineup_rows: list[dict]) -> dict[str, list[str]]:
    """One play's full defense dict - P/C straight from the play row (finding
    7: both are filled on every play row, authoritatively, and Path B could
    never see P anyway - pitchers don't bat in this DH league), the other 7
    positions from whichever path this game uses. Always resolves the full
    7-position map, never narrowed to just the positions this specific play's
    fieldingNotation chain needs - which positions a slide needs is a
    client-side (app.js) decision made at render time from the physics, not
    something this build-time step can predict.
    """
    defense: dict[str, list[str]] = {}
    p_entry = _defense_entry(ref, play.get("pitcher_id"))
    if p_entry:
        defense["P"] = p_entry
    c_entry = _defense_entry(ref, play.get("catcher_id"))
    if c_entry:
        defense["C"] = c_entry

    _, seq = utils.split_play_num(play["play_num"])
    defending_is_home = (half == "top")

    if is_final:
        timeline = home_timeline if defending_is_home else away_timeline
        ids = utils.timeline_alignment_at(timeline or {}, seq, positions=utils.ALIGNMENT_POSITIONS)
        for pos, pid in ids.items():
            entry = _defense_entry(ref, pid)
            if entry:
                defense[pos] = entry
    else:
        def_key = play.get("home") if defending_is_home else play.get("away")
        team_abbrev = _team_view(ref, def_key)["abbrev"]
        alignment = utils.lineup_alignment_at(game_lineup_rows, team_abbrev, seq, ref["name_to_id"])
        for pos, v in alignment.items():
            entry = _defense_entry(ref, v["player_id"], fallback_name=v["name"])
            if entry:
                defense[pos] = entry

    return defense


# ── per-game replay ───────────────────────────────────────────────────────────

def _runs_on_play(play: dict, nxt: dict | None, game: dict | None) -> int:
    """Runs scored on this play, from the running score in the sheet.

    a_Scr/h_Scr on a play row are the score BEFORE that play, so the delta to
    the next row is the runs this play produced. The final play of a completed
    game is closed out against the Games tab's final score; anything else falls
    back to the run-advancement model.
    """
    before = (play["away_score"] or 0) + (play["home_score"] or 0)
    if nxt is not None:
        after = (nxt["away_score"] or 0) + (nxt["home_score"] or 0)
    elif game and game.get("away_score") is not None and game.get("home_score") is not None:
        after = (game["away_score"] or 0) + (game["home_score"] or 0)
    else:
        after = None
    if after is not None and after >= before:
        return after - before
    _, runs = utils.advance_runners(play.get("result") or "", play["obc"], play.get("_outs_before", 0))
    return int(runs)


def _game_is_final(game: dict | None) -> bool:
    """Has this game actually finished? Delegates to utils.game_is_final -
    the repo's one definition (KEY_MOMENTS.md), so there's exactly one place
    this logic lives.
    """
    return utils.game_is_final(game)


def _is_final_play(play: dict, game: dict | None, is_last_in_data: bool) -> bool:
    """Is this play the last out of a completed game?

    Cross-checked against the Games tab's last_play so that a plays feed lagging
    behind the games feed cannot promote a mid-game play to the final out.
    """
    if not is_last_in_data or not _game_is_final(game):
        return False
    recorded_last = str(game.get("last_play") or "").strip()
    if recorded_last:
        return recorded_last == str(play["play_num"])
    return True


def replay_game(plays: list[dict], game: dict | None) -> list[dict]:
    """Walk one game's plays in order, returning a state record per play."""
    ordered = sorted(plays, key=lambda p: p["play_num"])
    outs_tracker: dict[tuple, int] = {}
    states: list[dict] = []

    for i, play in enumerate(ordered):
        result = play.get("result") or ""
        inning = int(play.get("inning") or 1)
        half = str(play.get("half") or "top")
        obc_before = str(play.get("obc") or "000")
        if obc_before not in utils.BRC_TO_OBC.values():
            obc_before = "000"

        inn_key = (inning, half)
        outs_before = outs_tracker.setdefault(inn_key, 0)
        outs_after = min(3, outs_before + utils.outs_added(result))
        outs_tracker[inn_key] = outs_after

        play["_outs_before"] = outs_before
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        is_last = nxt is None
        runs = _runs_on_play(play, nxt, game)

        batting_is_home = (half == "bottom")
        away_score = play["away_score"] or 0
        home_score = play["home_score"] or 0
        lead_before = (home_score - away_score) if batting_is_home else (away_score - home_score)
        lead_after = lead_before + runs

        if outs_after >= 3:
            obc_after = "000"
        elif nxt is not None and (nxt.get("inning"), nxt.get("half")) == inn_key:
            obc_after = str(nxt.get("obc") or "000")
        else:
            obc_after, _ = utils.advance_runners(result, obc_before, outs_before)

        remaining = utils.remaining_half_innings(inning, half, MLN_INNINGS)
        wp_before = utils.get_win_probability_interpolated(remaining, outs_before, obc_before, lead_before)

        ended = _is_final_play(play, game, is_last)
        if ended:
            final_bat = (game["home_score"] if batting_is_home else game["away_score"]) or 0
            final_fld = (game["away_score"] if batting_is_home else game["home_score"]) or 0
            wp_after = 1.0 if final_bat > final_fld else (0.5 if final_bat == final_fld else 0.0)
        elif outs_after >= 3:
            rem_next = remaining - 1
            if rem_next >= 1:
                opp_wp = utils.get_win_probability_interpolated(rem_next, 0, "000", -lead_after)
                wp_after = None if opp_wp is None else 1.0 - opp_wp
            else:
                wp_after = 1.0 if lead_after > 0 else (0.5 if lead_after == 0 else 0.0)
        else:
            wp_after = utils.get_win_probability_interpolated(remaining, outs_after, obc_after, lead_after)

        leverage = utils.compute_leverage_re24(
            remaining, outs_before, obc_before, lead_before
        )

        states.append({
            "play": play,
            "inning": inning,
            "half": half,
            "outs_before": outs_before,
            "outs_after": outs_after,
            "obc_before": obc_before,
            "obc_after": obc_after,
            "runs": runs,
            "away_score_before": away_score,
            "home_score_before": home_score,
            "batting_is_home": batting_is_home,
            "lead_before": lead_before,
            "lead_after": lead_after,
            "wp_before": wp_before,
            "wp_after": wp_after,
            "wpa": None if (wp_before is None or wp_after is None) else wp_after - wp_before,
            "leverage": leverage,
            "game_ended": ended,
        })

    return states


# ── tagging ───────────────────────────────────────────────────────────────────

def moment_tags(state: dict) -> list[str]:
    """Tag rules, checked and appended in TAG_LABELS order (that order is
    what both the filter chips and each card's own tag pills render in).

    Every tag except `risp` (see FILTER_ONLY_TAGS) also functions as an
    inclusion rule - any one of those firing makes the play a key moment.
    """
    play = state["play"]
    result = play.get("result") or ""
    diff = play.get("diff")
    wpa = state["wpa"]
    leverage = state["leverage"]
    obc_before = state["obc_before"]
    tags: list[str] = []

    if leverage is not None and leverage >= LEVERAGE_THRESHOLD:
        tags.append("high_leverage")
    if wpa is not None and abs(wpa) >= WPA_THRESHOLD:
        tags.append("wpa")
    if obc_before[0] == "1" or obc_before[1] == "1":
        tags.append("risp")
    if state["runs"] > 0:
        tags.append("run_scored")
    if _sign(state["lead_before"]) != _sign(state["lead_after"]):
        tags.append("lead_change")
    if state["obc_after"] == "111":
        tags.append("bases_loaded")
    if result in STEAL_SUCCESS_CODES or result in CAUGHT_STEALING_CODES:
        tags.append("steal")
    if result in DOUBLE_PLAY_CODES:
        tags.append("double_play")
    if result in TRIPLE_PLAY_CODES:
        tags.append("triple_play")
    if diff == 0:
        tags.append("zero_diff")
    if diff == 500:
        tags.append("five_hundred_diff")
    return tags


def _featured(ref: dict, state: dict, off: dict, deff: dict) -> dict:
    """Which player the card headlines, which side of the play they're on,
    and their counterpart on the other side of the play (shown smaller on
    the card so a favorited player never goes unacknowledged just because
    they weren't the featured one - e.g. the runner on a caught stealing)."""
    play = state["play"]
    result = play.get("result") or ""
    batter = _player_view(ref, play.get("batter_id"))
    pitcher = _player_view(ref, play.get("pitcher_id"))
    catcher = _player_view(ref, play.get("catcher_id"))
    runner = _player_view(ref, play.get("runner_id"))

    if result in STEAL_SUCCESS_CODES and runner["id"]:
        player, side = runner, "batting"
        counterpart = catcher if catcher["id"] else pitcher
    elif result in CAUGHT_STEALING_CODES:
        player, side = (catcher if catcher["id"] else pitcher), "fielding"
        counterpart = runner
    elif _effective_category(result, state["runs"]) == "hitting":
        player, side = batter, "batting"
        counterpart = pitcher
    elif _effective_category(result, state["runs"]) == "pitching":
        player, side = pitcher, "fielding"
        counterpart = batter
    else:
        player, side = batter, "batting"
        counterpart = pitcher

    team = off if side == "batting" else deff
    return {"player": player, "side": side, "team": team,
            "batter": batter, "pitcher": pitcher, "runner": runner,
            "counterpart": counterpart}


def build_moment(ref: dict, state: dict, game: dict | None, tags: list[str],
                  defense: dict[str, list[str]] | None = None) -> dict:
    play = state["play"]
    result = play.get("result") or ""
    game_code = play["game_code"]
    off_key, def_key = ((play.get("away"), play.get("home")) if state["half"] == "top"
                        else (play.get("home"), play.get("away")))
    off = _team_view(ref, off_key)
    deff = _team_view(ref, def_key)
    feat = _featured(ref, state, off, deff)

    wpa = state["wpa"]
    wp_after = state["wp_after"]
    flip = (feat["side"] == "fielding")
    featured_wpa = None if wpa is None else (-wpa if flip else wpa)
    featured_wp_after = None if wp_after is None else ((1.0 - wp_after) if flip else wp_after)

    session_number = int(game_code[2:4]) if len(game_code) >= 4 and game_code[2:4].isdigit() else None
    scoring_names = _scoring_names(ref, play, feat["batter"]["id"])
    position_override = utils.get_position_override(result, state["obc_before"], state["outs_before"])

    # Anything derivable from a small closed set (team names, sub-league,
    # result labels, tag labels, base-diamond SVG) lives in meta.json instead of
    # being repeated on every play - the feed carries every play now, so the
    # per-row cost of a redundant field is paid thousands of times.
    return {
        "moment_id": str(play["play_num"]),
        "play_num": play["play_num"],
        "game_code": game_code,
        "session_number": session_number,
        "timestamp": _parse_timestamp(play.get("timestamp")),

        "inning": state["inning"],
        "half": state["half"],
        "outs_before": state["outs_before"],
        "outs_after": state["outs_after"],
        "obc_before": state["obc_before"],
        "obc_after": state["obc_after"],

        "result": result,
        "result_category": _effective_category(result, state["runs"]),
        "diff": play.get("diff"),
        "pitch": play.get("pitch"),
        "swing": play.get("swing"),
        # Steal-attempt equivalent of pitch/swing - the runner's roll and the
        # catcher's roll (sheet columns "Steal"/"Throw"). Real, resolution is
        # `Safe if circular_diff(throw_num, steal_num) <= safe_range else Out`
        # (utils.py steal_color_bar), but safe_range is a per-runner speed
        # rating not threaded through this pipeline - the pitch/swing wheel
        # (Play Scene) shows the two values and their diff only, no safe-zone.
        "throw_num": play.get("throw_num"),
        "steal_num": play.get("steal_num"),
        # Explicit fielding-throw sequence for this (result, obc_before,
        # outs_before) situation, straight from import_BRC.csv's optional
        # ThrowOrder column (e.g. "1234" = throw to 1B, then 2B, then 3B,
        # then home) - None on every row until that column exists and this
        # situation has one filled in. app.js's outThrowTargets() prefers
        # this over its own before/after-diff heuristic whenever it's set.
        "throw_order": utils.get_throw_order(result, state["obc_before"], state["outs_before"]),
        # If import_BRC.csv's ExcludedPositions/DefaultPosition columns say
        # the physics-computed fielder for this situation doesn't make
        # sense, app.js relocates the landing point to default_position's
        # own real spot on the field instead (both direction and depth).
        # None/None on every row until those columns exist and this
        # situation has them filled in.
        "excluded_positions": position_override["excluded"] if position_override else None,
        "default_position": position_override["default"] if position_override else None,
        # Per-position throw sequences - e.g. {"SS": "21", "P": "41"} - keyed
        # by whichever fielder ends up credited (after the override above),
        # not just the situation alone. Takes priority over throw_order.
        "throw_order_by_position": utils.get_throw_order_by_position(
            result, state["obc_before"], state["outs_before"]
        ),
        # Explicit per-runner outcome for this (result, obc_before,
        # outs_before) situation, decoded straight from import_BRC.csv's B/
        # r1/r2/r3 columns - e.g. [{"from":"1B","to":"2B","scored":false},
        # {"from":"3B","to":"OUT","scored":false}]. None until this situation
        # has those columns filled in and internally consistent (see
        # utils.get_runner_moves) - app.js's deriveRunnerMoves() before/
        # after-diff guess is the fallback either way, same contract as
        # throw_order above.
        "runner_moves": utils.get_runner_moves(result, state["obc_before"], state["outs_before"]),
        "runs": state["runs"],
        "scoring_names": scoring_names,

        "batter_name": feat["batter"]["name"],
        "batter_id": feat["batter"]["id"],
        "batter_hand": feat["batter"]["hand"],
        "pitcher_name": feat["pitcher"]["name"],
        "pitcher_id": feat["pitcher"]["id"],
        "runner_name": feat["runner"]["name"],
        "runner_id": feat["runner"]["id"],

        "featured_name": feat["player"]["name"],
        "featured_id": feat["player"]["id"],
        "featured_side": feat["side"],
        "featured_team_abbr": feat["team"]["abbrev"],
        "featured_wp_after": None if featured_wp_after is None else round(featured_wp_after, 4),
        "featured_wpa": None if featured_wpa is None else round(featured_wpa, 4),

        # Shown smaller on the card, still linked - the other side of the
        # matchup (pitcher for a hitting result, batter for a pitching
        # result, runner<->catcher for steals/caught stealing).
        "counterpart_name": feat["counterpart"]["name"],
        "counterpart_id": feat["counterpart"]["id"],

        "off_team_abbr": off["abbrev"],
        "def_team_abbr": deff["abbrev"],
        "away_team_abbr": _team_view(ref, play.get("away"))["abbrev"],
        "home_team_abbr": _team_view(ref, play.get("home"))["abbrev"],
        "away_score": state["away_score_before"] + (0 if state["batting_is_home"] else state["runs"]),
        "home_score": state["home_score_before"] + (state["runs"] if state["batting_is_home"] else 0),
        "batting_is_home": state["batting_is_home"],

        "win_prob_before": None if state["wp_before"] is None else round(state["wp_before"], 4),
        "win_prob_after": None if wp_after is None else round(wp_after, 4),
        "wpa": None if wpa is None else round(wpa, 4),
        "leverage": None if state["leverage"] is None else round(state["leverage"], 2),

        "rookie": feat["player"]["rookie"],
        "is_key_moment": any(t not in FILTER_ONLY_TAGS for t in tags),
        "tags": tags,
        "is_half_inning_final": state["outs_after"] == 3,
        "is_game_final": state["game_ended"],

        # Who's playing each defensive position, as {"SS": [full, last], ...} -
        # in-progress games from a live Lineups-tab read (as of this build),
        # completed games reconstructed from plays.pos (fielding-position-
        # implementation-plan.md). Field labels want last names; the single-
        # fielder text-line template wants the full name - both come from
        # _player_view, never split client-side. Omitted entirely (not an
        # empty dict) when nothing resolved, so the client's existing
        # missing-data handling (generic anchor + position code, no line)
        # needs no special casing.
        **({"defense": defense} if defense else {}),
    }


# ── build ─────────────────────────────────────────────────────────────────────

def build(sheet_id: str = MLN_SHEET_ID) -> tuple[list[dict], list[dict], dict]:
    """Return (all plays scored and tagged, roster, meta)."""
    ref = load_reference(sheet_id)

    games = utils.read_mln_games_from_sheet(sheet_id)
    if not games:
        raise RuntimeError("No games found in the MLN Games tab.")
    game_by_code = {g["game_code"]: g for g in games if g.get("game_code")}

    plays = utils.read_mln_plays_from_sheet(sheet_id, tab="Plays (Raw)")
    if not plays:
        raise RuntimeError("No plays found in the MLN 'Plays (Raw)' tab.")

    by_game: dict[str, list[dict]] = {}
    for p in plays:
        pitch, swing = p.get("pitch"), p.get("swing")
        pitch = int(pitch) if pitch is not None else None
        swing = int(swing) if swing is not None else None
        p["pitch"], p["swing"] = pitch, swing
        p["diff"] = utils.circular_diff(pitch, swing) if pitch is not None and swing is not None else None
        by_game.setdefault(p["game_code"], []).append(p)

    # Lineups is only ever needed for in-progress games (Decision 1: never
    # rely on it for completed games) - fetched at most once per build run,
    # lazily, the first time a non-final game is actually encountered below.
    lineups_by_game: dict[str, list[dict]] | None = None

    # Every play is scored and tagged; is_key_moment records which ones qualify.
    # The walk has to happen anyway to get WPA, so keeping the non-qualifying
    # rows costs nothing and is what the favorites view browses.
    rows: list[dict] = []
    for game_code in sorted(by_game):
        game = game_by_code.get(game_code)
        game_plays = by_game[game_code]
        is_final = utils.game_is_final(game)

        # Path B: reconstruct once per team per game (not once per play - a
        # fresh reconstruction per play would be quadratic in play count).
        # Each team's own batting rows are a simple half filter, per Decision 3.
        home_timeline = away_timeline = None
        game_lineup_rows: list[dict] = []
        if is_final:
            home_timeline = utils.reconstruct_defense_timeline([
                (utils.split_play_num(p["play_num"])[1], p.get("batter_id"), p.get("pos"),
                 p.get("play_type"), p.get("result"))
                for p in game_plays if p.get("half") == "bottom"
            ])
            away_timeline = utils.reconstruct_defense_timeline([
                (utils.split_play_num(p["play_num"])[1], p.get("batter_id"), p.get("pos"),
                 p.get("play_type"), p.get("result"))
                for p in game_plays if p.get("half") == "top"
            ])
        else:
            if lineups_by_game is None:
                lineups_by_game = {}
                for row in utils.read_lineups_from_sheet(sheet_id):
                    lineups_by_game.setdefault(row["game_id_short"], []).append(row)
            game_lineup_rows = lineups_by_game.get((game or {}).get("game_id_short"), [])

        for state in replay_game(game_plays, game):
            play = state["play"]
            defense = _defense_alignment_for_play(
                ref, play, state["half"], is_final, home_timeline, away_timeline, game_lineup_rows,
            )
            rows.append(build_moment(ref, state, game, moment_tags(state), defense))

    rows.sort(key=lambda m: (m["timestamp"] or "", m["play_num"]), reverse=True)

    roster = sorted(
        (
            {
                "id": p["player_id"],
                "name": p.get("name") or "",
                "team": p.get("team") or "",
                "sub_league": _team_view(ref, p.get("team"))["sub_league"],
                "rookie": bool(p.get("is_rookie")),
            }
            for p in ref["players"] if p.get("player_id") and p.get("name")
        ),
        key=lambda p: p["name"].lower(),
    )

    # Session picker stays play-gated (Alex's call) - a session with zero
    # plays anywhere yet shouldn't be selectable at all. sessions is exactly
    # what it always was, rows-derived only.
    sessions = sorted({m["session_number"] for m in rows if m["session_number"]}, reverse=True)
    sessions_set = set(sessions)
    # But WITHIN a session that does have plays, every game already on the
    # MLN Games tab schedule for it should show on the scoreboard - not just
    # the ones that happen to have a play recorded yet (Alex's report: at
    # the start of a session, only whichever games went first were showing
    # up). Scoped to sessions_set so a team/game whose only session is one
    # with zero plays anywhere doesn't leak into meta.teams either.
    teams_seen = sorted(
        {a for m in rows for a in (m["off_team_abbr"], m["def_team_abbr"]) if a} |
        {g["away_team"] for g in games if g.get("away_team") and g.get("session_number") in sessions_set} |
        {g["home_team"] for g in games if g.get("home_team") and g.get("session_number") in sessions_set}
    )
    meta = {
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sheet_id": sheet_id,
        "season": CURRENT_SEASON,
        "innings": MLN_INNINGS,
        "sessions": sessions,
        "plays_scanned": len(rows),
        "moment_count": sum(1 for m in rows if m["is_key_moment"]),
        "leverage_threshold": LEVERAGE_THRESHOLD,
        "wpa_threshold": WPA_THRESHOLD,

        # Shared lookups, so no play row has to repeat them.
        "teams": {
            abbr: {
                "name": ref["team_by_abbrev"].get(abbr, {}).get("full_team") or abbr,
                "sub_league": ref["team_by_abbrev"].get(abbr, {}).get("sub_league") or "",
                "primary_hex": ref["team_by_abbrev"].get(abbr, {}).get("primary_hex") or "",
                "secondary_hex": SECONDARY_HEX.get(abbr, ""),
                "logo_url": ref["team_by_abbrev"].get(abbr, {}).get("logo_url") or "",
            }
            for abbr in teams_seen
        },
        "result_labels": {r: RESULT_LABELS.get(r, r) for r in sorted({m["result"] for m in rows})},
        "result_short": {r: RESULT_SHORT.get(r, r) for r in sorted({m["result"] for m in rows})},
        "tag_labels": dict(TAG_LABELS),
        "bases_svg": {obc: _diamond_svg(obc) for obc in sorted(utils.BRC_TO_OBC.values())},
        "flight": _flight_meta(),
        # One scoreboard per session, keyed by session number as a string
        # (JSON object keys are always strings) - the page shows whichever
        # one matches its session selector and hides the section entirely
        # for "Full season".
        "games": {str(s): _scoreboard(rows, games, s) for s in sessions},
    }
    return rows, roster, meta


def _flight_meta() -> dict:
    """meta.flight: the ball-flight staging table, read through utils._DIFF_BANDS
    the same way every other precomputed CSV reaches this build, plus the
    exclusion list as data so app.js has one definition of in-scope-for-flight
    rather than a second hardcoded copy of FLIGHT_EXCLUDED.

    Each band now carries its own laMin/laIdeal/laMax/evMin/evMax/depthMin/
    depthMax directly (real, per-MLN-result Statcast-derived numbers) instead
    of a level of indirection through a separate archetype-keyed table - the
    old ball_flight_archetypes.csv is retired. depthMin/depthMax are
    audit-only as of the physics-redesign port
    (ball-flight-3d-physics-redesign-plan.md Part 2.4) - app.js's
    KMTraj.simulateFlight derives landing distance from laIdeal/EV via the
    drag+lift integrator for every archetype, grounders included; these two
    columns are shipped for the test suite/audit tooling only, nothing at
    runtime reads them. `archetype` stays
    on the band as a plain category label: app.js's CAUGHT_IN_AIR/
    GROUND_ARCHETYPES/TAG_THROW_ARCHETYPES still branch on it, just no longer
    use it to look up numbers.

    no_pa is the subset of FLIGHT_EXCLUDED where the batter never had a plate
    appearance at all (a steal, a caught stealing, a balk) - app.js uses it to
    suppress the batter token fallback entirely on those plays, rather than
    walking a batter who did nothing to the dugout."""
    bands = {
        result: {
            "archetype": row["archetype"], "lo": row["band_lo"], "hi": row["band_hi"],
            "laMin": row["la_min"], "laIdeal": row["la_ideal"], "laMax": row["la_max"],
            "evMin": row["ev_min"], "evMax": row["ev_max"],
            "depthMin": row["depth_min"], "depthMax": row["depth_max"],
        }
        for result, row in utils._DIFF_BANDS.items()
    }
    no_pa = sorted(STEAL_SUCCESS_CODES | CAUGHT_STEALING_CODES | {"Balk"})
    return {"bands": bands, "excluded": FLIGHT_EXCLUDED, "no_pa": no_pa}


def _is_walkoff_final(inning: int, half: str, outs_after: int, home_score: int, away_score: int) -> bool:
    """Same three end-of-game conditions as utils.compute_game_wp_series's
    win-probability-graph logic, reused here so the scoreboard can call a
    game final the moment it's mathematically decided rather than waiting on
    the Games tab's Win column to catch up:

    1. Top of the last inning ends with the home team leading - the bottom
       half is never played.
    2. The home team takes the lead at any point during the bottom half -
       walk-off, ends immediately regardless of outs.
    3. The bottom half ends (3 outs) with the score not tied.

    All three only apply from the last scheduled inning on (MLN_INNINGS),
    which also covers extra innings since every half-inning past regulation
    still qualifies.
    """
    if inning < MLN_INNINGS:
        return False
    is_home = half == "bottom"
    if not is_home and outs_after >= 3 and home_score > away_score:
        return True
    if is_home and home_score > away_score:
        return True
    if is_home and outs_after >= 3 and home_score != away_score:
        return True
    return False


def _scoreboard(rows: list[dict], games: list[dict], session: int) -> list[dict]:
    """One tile per game in the given session, from each game's latest play -
    the replay already carries current score/inning/outs/bases, so "live"
    state falls out of the existing per-play computation for free.

    `games` (the full MLN Games-tab schedule, already fetched once in
    build()) fills in a tile for every game in this session that hasn't
    recorded a single play yet - the scheduled matchup at its 0-0/not-yet-
    started state, rather than the game silently missing from the slate
    until its own first play shows up. Alex's report: at the start of a
    session, only whichever games happened to go first were showing up.

    Leverage is the exception: it is NOT carried over from the latest play's
    own `leverage` field, because that value is the leverage of the plate
    appearance that JUST HAPPENED (computed pre-play, from the state before
    that play resolved). What the tile should show is the leverage of the
    plate appearance coming up next - i.e. leverage recomputed from the state
    AFTER the latest play, using the same inning/half/outs/bases advance
    already done below for display. This matters most right after a play that
    ended a half-inning, where the pre-play leverage belongs to a half-inning
    that is already over.

    Finished games sink to the end (leverage forced to 0, since a completed
    game has no "how tense is this right now" to show) rather than being
    dropped, so a session's slate doesn't shrink as games wrap up.
    """
    latest: dict[str, dict] = {}
    for m in rows:
        if m["session_number"] != session:
            continue
        cur = latest.get(m["game_code"])
        if cur is None or m["play_num"] > cur["play_num"]:
            latest[m["game_code"]] = m

    tiles = []
    for m in latest.values():
        wp_after = m["win_prob_after"]  # batting team's perspective
        if wp_after is None:
            home_wp = away_wp = None
        elif m["batting_is_home"]:
            home_wp, away_wp = wp_after, 1.0 - wp_after
        else:
            away_wp, home_wp = wp_after, 1.0 - wp_after

        is_final = m["is_game_final"] or _is_walkoff_final(
            m["inning"], m["half"], m["outs_after"], m["home_score"], m["away_score"]
        )
        inning, half, outs_after, obc_after = m["inning"], m["half"], m["outs_after"], m["obc_after"]
        # The latest play ended its half-inning but the game isn't over -
        # show the next half-inning's empty starting state (no runners, no
        # outs) instead of a dead-end "half over" badge. Top -> bottom of the
        # same inning; bottom -> top of the next one.
        if not is_final and outs_after >= 3:
            if half == "top":
                half = "bottom"
            else:
                half = "top"
                inning += 1
            outs_after = 0
            obc_after = "000"

        # Leverage for the plate appearance following the latest play - see
        # the docstring above. Uses the same (possibly half-inning-advanced)
        # inning/half/outs/bases as the tile's own display state, and the
        # batting team implied by that (possibly flipped) half, with the
        # score already reflecting the latest play's result.
        next_batting_is_home = (half == "bottom")
        lead_now = ((m["home_score"] - m["away_score"]) if next_batting_is_home
                    else (m["away_score"] - m["home_score"]))
        remaining_now = utils.remaining_half_innings(inning, half, MLN_INNINGS)
        leverage_now = (0 if is_final else
                       (utils.compute_leverage_re24(remaining_now, outs_after, obc_after, lead_now) or 0))

        tiles.append({
            "game_code": m["game_code"],
            "away_team_abbr": m["away_team_abbr"],
            "home_team_abbr": m["home_team_abbr"],
            "away_score": m["away_score"],
            "home_score": m["home_score"],
            "inning": inning,
            "half": half,
            "outs_after": outs_after,
            "obc_after": obc_after,
            "is_half_inning_final": False,
            "is_game_final": is_final,
            "leverage": round(leverage_now, 2),
            "away_win_prob": None if away_wp is None else round(away_wp, 4),
            "home_win_prob": None if home_wp is None else round(home_wp, 4),
        })

    # Scheduled games in this session with no play recorded yet - the
    # Games-tab matchup itself, at its plain not-started state. Leverage
    # here is NOT forced to 0 the way a finished game's is (Alex's catch):
    # a finished game genuinely has no "how tense is this right now" left to
    # show, but a game that simply hasn't thrown its first pitch yet does -
    # the leverage of the very first plate appearance is fully determined by
    # the state every game starts from (top 1st, no outs, bases empty,
    # tied), so it's real, computable info, not a placeholder zero.
    # LEVERAGE_AT_GAME_START (module-level) is that same number, computed
    # once at import time rather than per game per build.
    for g in games:
        if g.get("session_number") != session:
            continue
        code = g.get("game_code")
        if not code or code in latest:
            continue
        tiles.append({
            "game_code": code,
            "away_team_abbr": g.get("away_team") or "",
            "home_team_abbr": g.get("home_team") or "",
            "away_score": 0,
            "home_score": 0,
            "inning": 1,
            "half": "top",
            "outs_after": 0,
            "obc_after": "000",
            "is_half_inning_final": False,
            "is_game_final": False,
            "leverage": LEVERAGE_AT_GAME_START,
            "away_win_prob": None,
            "home_win_prob": None,
        })

    tiles.sort(key=lambda g: g["leverage"], reverse=True)
    return tiles


def _write(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")


def _prune_stale_session_files(sessions: list[int]) -> None:
    """Drop plays_NN.json files for sessions that no longer exist in the feed."""
    keep = {f"plays_{s:02d}.json" for s in sessions}
    for name in os.listdir(OUT_DIR):
        if name.startswith("plays_") and name.endswith(".json") and name not in keep:
            os.remove(os.path.join(OUT_DIR, name))


def main() -> None:
    try:
        rows, roster, meta = build()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    moments = [m for m in rows if m["is_key_moment"]]
    if not moments:
        print("FATAL: no key moments produced - refusing to publish an empty feed.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    _write(os.path.join(OUT_DIR, "key_moments.json"), moments)
    _write(os.path.join(OUT_DIR, "players.json"), roster)
    _write(os.path.join(OUT_DIR, "meta.json"), meta)
    for session in meta["sessions"]:
        session_rows = [m for m in rows if m["session_number"] == session]
        _write(os.path.join(OUT_DIR, f"plays_{session:02d}.json"), session_rows)
    _prune_stale_session_files(meta["sessions"])

    counts: dict[str, int] = {}
    per_session: dict[int, list[int]] = {}
    for m in rows:
        for t in m["tags"]:
            counts[t] = counts.get(t, 0) + 1
        if m["session_number"]:
            tally = per_session.setdefault(m["session_number"], [0, 0])
            tally[0] += 1
            tally[1] += 1 if m["is_key_moment"] else 0

    print(f"Scanned {meta['plays_scanned']} plays -> {len(moments)} key moments "
          f"across sessions {meta['sessions']}")
    print(f"  thresholds: leverage >= {LEVERAGE_THRESHOLD}, |wpa| >= {WPA_THRESHOLD}")
    for session in sorted(per_session, reverse=True):
        plays_n, moments_n = per_session[session]
        print(f"  session {session:02d}: {moments_n} moments / {plays_n} plays")
    for tag in TAG_LABELS:
        print(f"  tag {tag:<28} {counts.get(tag, 0)}")
    print(f"Wrote {OUT_DIR}/key_moments.json, players.json, meta.json, "
          f"plays_NN.json x{len(meta['sessions'])}")


if __name__ == "__main__":
    main()

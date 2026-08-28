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

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import utils

# ── configuration ─────────────────────────────────────────────────────────────

MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"
CURRENT_SEASON = 13
MLN_INNINGS = utils.LEAGUE_INNINGS.get("MLN", 6)

# The MLN Historical Archive - a separate public sheet covering seasons 1-12,
# read-only source for historical-seasons-implementation-plan.md. Same gviz
# CSV endpoint, same utils.py readers (archive-aware by column-name
# detection), different sheet id.
ARCHIVE_SHEET_ID = "1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg"
# The archive's "Plays" tab gid (from the sheet's own URL, #gid=...), passed
# to utils.read_mln_plays_from_sheet to route around the gviz Pos* column
# type-inference bug documented on that function - see its docstring.
ARCHIVE_PLAYS_GID = "1141271229"

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

    Base centers sit a bit further out from the shared centroid (15, 12.33)
    than the original (10,2)/(18,10)/(2,10) rects did (Alex's ask: a bit more
    gap between bases) - viewBox grew from a flush "0 0 30 30" to "-2 -2 34
    34" to keep 1B's own rotated corner (its tightest margin) from clipping
    now that it's a little further from center; base squares stay 10x10,
    only the spacing changed.
    """
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"
    bases = (
        (10, 1.4, 15, 6.4, on_2b),     # second - top
        (18.9, 10.3, 23.9, 15.3, on_1b),  # first - right
        (1.1, 10.3, 6.1, 15.3, on_3b),    # third - left
    )
    rects = "".join(
        f'<rect class="base-diamond {"on" if filled else "off"}" x="{x}" y="{y}" '
        f'width="10" height="10" rx="1.5" transform="rotate(45 {cx} {cy})"/>'
        for x, y, cx, cy, filled in bases
    )
    return f'<svg width="30" height="30" viewBox="-2 -2 34 34">{rects}</svg>'


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

def load_reference(sheet_id: str, archive_season: int | None = None,
                    teams_raw: list[dict] | None = None,
                    players_raw: list[dict] | None = None) -> dict:
    """Load teams and players once, indexed for per-play lookups.

    Archive mode (`archive_season` set): both tabs carry every season's rows
    (franchise history - abbreviations and team identities change across
    seasons per Part 0 finding 9 of the historical-seasons plan), so they
    MUST be filtered to this season before indexing by abbrev/id, or teams
    and players from other seasons would collide in by_abbrev/by_player_id.
    `teams_raw`/`players_raw` let `--archive-all` pass in tabs already
    fetched once instead of re-fetching per season.
    """
    if archive_season is not None:
        teams = teams_raw if teams_raw is not None else utils.read_mln_teams_from_sheet(sheet_id)
        teams = [t for t in teams if t.get("season") == archive_season]
        players = players_raw if players_raw is not None else utils.read_mln_players_from_sheet(sheet_id, tab="Rosters")
        players = [p for p in players if p.get("season") == archive_season]
    else:
        teams = teams_raw if teams_raw is not None else utils.read_mln_teams_from_sheet(sheet_id)
        players = players_raw if players_raw is not None else utils.read_mln_players_from_sheet(
            sheet_id, tab="Players", season=CURRENT_SEASON)
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
        return {"id": None, "name": "", "last_name": "", "rookie": False, "team": "", "hand": "",
                "spd": None, "eye": None, "awr": None}
    p = ref["player_by_id"].get(player_id)
    if not p:
        # Traded, released, or otherwise off the current roster tab.
        return {"id": player_id, "name": f"Player {player_id}", "last_name": f"Player {player_id}",
                "rookie": False, "team": "", "hand": "", "spd": None, "eye": None, "awr": None}
    return {
        "id": player_id,
        "name": p.get("name") or f"Player {player_id}",
        "last_name": p.get("last_name") or p.get("name") or f"Player {player_id}",
        "rookie": bool(p.get("is_rookie")),
        "team": p.get("team") or "",
        "hand": p.get("hand") or "",
        # 1-5 scouted speed rating (MLN Calculator convention, 3 = average) -
        # straight off the Players/Rosters tab, same column both current-
        # season and archive reads already parse. None when unresolved (no
        # player_id, or off the roster tab) - the client's own speed-to-ft/s
        # mapping falls back to the average (3) rating in that case, same as
        # every other "traded/released, no real data" gap in this pipeline.
        "spd": p.get("spd"),
        # 1-5 scouted eye/awareness ratings, same convention/columns as spd
        # (utils.read_mln_players_from_sheet already parses both in every
        # format) - Task 14: eye drives a catcher's own steal-throw speed,
        # awr a pitcher's own pickoff leadoff distance. Same None-on-
        # unresolved fallback as spd.
        "eye": p.get("eye"),
        "awr": p.get("awr"),
    }


def _defense_entry(ref: dict, player_id: int | None, fallback_name: str | None = None) -> list | None:
    """[full_name, last_name, spd, eye] for one resolved defensive position
    (or on-base runner - see _trace_base_occupants/runners_on_base), or None
    when there's nothing to show. Names/spd/eye come from _player_view
    (never split/re-derived client-side - see build_moment's defense
    docstring) except when a Lineups-tab name has no matching player_id
    (traded/released player off the current roster tab) - there, fall back
    to the raw sheet name for both name slots and null spd/eye rather than
    dropping the label entirely. eye (Task 14.1) is index 3 - an append,
    not an insert, so every existing entry[0..2] reader (client and tests)
    stays correct unchanged.
    """
    if player_id:
        v = _player_view(ref, player_id)
        return [v["name"], v["last_name"], v["spd"], v["eye"]]
    if fallback_name:
        return [fallback_name, fallback_name, None, None]
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

def _runs_on_play(play: dict, nxt: dict | None, game: dict | None, prev: dict | None = None,
                   archive_mode: bool = False) -> int:
    """Runs scored on this play, from the running score in the sheet.

    Live sheet (`Plays (Raw)`): a_Scr/h_Scr on a play row are the score
    BEFORE that play, so the delta to the NEXT row is the runs this play
    produced. The final play of a completed game is closed out against the
    Games tab's final score; anything else falls back to the run-advancement
    model.

    Archive mode (`archive_mode=True`, per-season - see
    _detect_archive_score_convention): the opposite convention - a_Scr/h_Scr
    is the score AFTER that play resolves (confirmed against real data: a
    3-run HR with runners on 2nd and 3rd already carries the full +3 jump on
    its own row). So there the delta is against the PREVIOUS row instead,
    with 0-0 standing in for "no previous row" (the first play of a game).
    """
    if archive_mode:
        before = ((prev["away_score"] if prev else 0) or 0) + ((prev["home_score"] if prev else 0) or 0)
        after = (play["away_score"] or 0) + (play["home_score"] or 0)
    else:
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


def _detect_archive_score_convention(by_game: dict[str, list[dict]]) -> bool:
    """Does this archive season's a_Scr/h_Scr store the score AFTER each play
    (post-play), or BEFORE it (pre-play, same as the live sheet's Plays (Raw)
    tab)? Re-derived from the data every build rather than assumed, because
    it is NOT constant across the archive: verified directly against the
    sheet, seasons 1-10 are post-play but 11-12 switch to the live
    convention (presumably a scorekeeping process change).

    Samples solo home runs (result == HR, obc_before == "000") - the batter
    is the only run, unambiguous - and checks whether that run already shows
    up in the HR's own row's score total (post-play) or only in the next
    row's (pre-play). Returns True (post-play) on a tie or no samples, since
    every archive season observed so far defaults to post-play.
    """
    post_votes = 0
    pre_votes = 0
    for gp in by_game.values():
        ordered = sorted(gp, key=lambda p: p["play_num"])
        for i, p in enumerate(ordered):
            if p.get("result") != "HR" or p.get("obc") != "000":
                continue
            total_this = (p["away_score"] or 0) + (p["home_score"] or 0)
            total_prev = ((ordered[i - 1]["away_score"] or 0) + (ordered[i - 1]["home_score"] or 0)) if i > 0 else 0
            total_next = None
            if i + 1 < len(ordered):
                nxt = ordered[i + 1]
                total_next = (nxt["away_score"] or 0) + (nxt["home_score"] or 0)
            if total_this == total_prev + 1:
                post_votes += 1
            elif total_next is not None and total_next == total_this + 1:
                pre_votes += 1
    return post_votes >= pre_votes


def _game_is_final(game: dict | None) -> bool:
    """Has this game actually finished? Delegates to utils.game_is_final -
    the repo's one definition (KEY_MOMENTS.md), so there's exactly one place
    this logic lives.
    """
    return utils.game_is_final(game)


def _is_final_play(play: dict, game: dict | None, is_last_in_data: bool, archive_mode: bool = False) -> bool:
    """Is this play the last out of a completed game?

    Live season: cross-checked against the Games tab's last_play so that a
    plays feed lagging behind the games feed cannot promote a mid-game play
    to the final out.

    Archive mode: skips the Games-tab cross-check entirely (Alex's call -
    trust the Plays data). Every archive season is a finished historical
    record already (build() treats every archive game as final regardless of
    what its own Games-tab row says), and season 1 in particular shows the
    Games tab disagreeing with the Plays data's own reconstructed score on
    21 of 75 games - so whatever is literally the last recorded play for
    this game IS the final play, full stop.
    """
    if not is_last_in_data:
        return False
    if archive_mode:
        return True
    if not _game_is_final(game):
        return False
    recorded_last = str(game.get("last_play") or "").strip()
    if recorded_last:
        return recorded_last == str(play["play_num"])
    return True


def replay_game(plays: list[dict], game: dict | None, archive_mode: bool = False) -> list[dict]:
    """Walk one game's plays in order, returning a state record per play.

    `archive_mode` flips the a_Scr/h_Scr convention used to derive runs and
    the pre-play score (see _runs_on_play) - the Historical Archive sheet's
    Plays tab (seasons 1-12) stores those columns post-play, the opposite of
    the live sheet's Plays (Raw) tab.
    """
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
        prev = ordered[i - 1] if i > 0 else None
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        is_last = nxt is None
        runs = _runs_on_play(play, nxt, game, prev=prev, archive_mode=archive_mode)

        batting_is_home = (half == "bottom")
        if archive_mode:
            away_score = (prev["away_score"] if prev else 0) or 0
            home_score = (prev["home_score"] if prev else 0) or 0
        else:
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

        ended = _is_final_play(play, game, is_last, archive_mode=archive_mode)
        if ended:
            if archive_mode:
                # Trust the Plays data's own running score over the Games
                # tab (Alex's call) - away_score/home_score here are already
                # this play's own before-score (see above), so adding this
                # play's runs gives the final total the same way build_moment
                # computes the displayed score.
                final_away = away_score + (0 if batting_is_home else runs)
                final_home = home_score + (runs if batting_is_home else 0)
            else:
                final_away = game["away_score"] or 0
                final_home = game["home_score"] or 0
            final_bat = final_home if batting_is_home else final_away
            final_fld = final_away if batting_is_home else final_home
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
                  defense: dict[str, list[str]] | None = None,
                  runners_on_base: dict[str, list] | None = None) -> dict:
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

    away_score = state["away_score_before"] + (0 if state["batting_is_home"] else state["runs"])
    home_score = state["home_score_before"] + (state["runs"] if state["batting_is_home"] else 0)
    # state["game_ended"] only ever fires once the Games tab's own Win column
    # has caught up (_is_final_play), which can lag well behind the game
    # actually being mathematically decided - the same gap _scoreboard()
    # already patches over with this same _is_walkoff_final call for the
    # scoreboard tile. Applying it here too, on every moment as it's built,
    # keeps is_game_final consistent everywhere it's read: the slideshow's
    # FINAL badge, and _next_batter_moment's on-deck-placeholder gate below
    # (both currently only trusted state["game_ended"]/last_moment's own
    # is_game_final and so stayed mid-inning-looking - Now Batting slide
    # included - for a game that was already over).
    is_final_moment = state["game_ended"] or _is_walkoff_final(
        state["inning"], state["half"], state["outs_after"], home_score, away_score
    )

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
        # ThrowOrder column (e.g. "fst" = throw to 1B, then 2B, then 3B;
        # "6h" = cutoff through SS, then home) - None on every row until
        # that column exists and this situation has one filled in. app.js's
        # outThrowTargets() prefers this over its own before/after-diff
        # heuristic whenever it's set.
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
        "away_score": away_score,
        "home_score": home_score,
        "batting_is_home": state["batting_is_home"],

        "win_prob_before": None if state["wp_before"] is None else round(state["wp_before"], 4),
        "win_prob_after": None if wp_after is None else round(wp_after, 4),
        "wpa": None if wpa is None else round(wpa, 4),
        "leverage": None if state["leverage"] is None else round(state["leverage"], 2),

        "rookie": feat["player"]["rookie"],
        "is_key_moment": any(t not in FILTER_ONLY_TAGS for t in tags),
        "tags": tags,
        "is_half_inning_final": state["outs_after"] == 3,
        "is_game_final": is_final_moment,

        # Who's playing each defensive position, as {"SS": [full, last, spd], ...} -
        # in-progress games from a live Lineups-tab read (as of this build),
        # completed games reconstructed from plays.pos (fielding-position-
        # implementation-plan.md). Field labels want last names; the single-
        # fielder text-line template wants the full name; spd (1-5, None if
        # unresolved) feeds the client's per-fielder charge/range speed - all
        # three come from _player_view, never split/re-derived client-side.
        # Omitted entirely (not an empty dict) when nothing resolved, so the
        # client's existing missing-data handling (generic anchor + position
        # code, average fielding speed) needs no special casing.
        **({"defense": defense} if defense else {}),

        # Real per-player speed for the two identities every play already
        # names outright - batter (about to become a runner) and pitcher
        # (fielding pace, unless overridden - see docs/js/app.js's own flat
        # pitcher-in-field convention). 1-5, None when unresolved.
        "batter_spd": feat["batter"]["spd"],
        "pitcher_spd": feat["pitcher"]["spd"],
        # Task 14.2: pitcher awareness (1-5, None when unresolved) - drives
        # the client's own stealLeadoffFt (a sharper pitcher holds a runner
        # closer to the bag). Sibling of pitcher_spd, same fallback.
        "pitcher_awr": feat["pitcher"]["awr"],

        # Who's standing on each base BEFORE this play resolves, same
        # [full, last, spd] shape as defense (_trace_base_occupants tracked
        # incrementally across the half-inning, build() below) - lets the
        # client give an existing baserunner their own real speed on this
        # play's advance animation, not just the batter becoming a new one.
        # Omitted entirely when nobody's on, same missing-data convention as
        # defense.
        **({"runners_on_base": runners_on_base} if runners_on_base else {}),
    }


# obc_before/obc_after string index order (utils._RUNNER_SLOT_DEFS' own
# comment: "obc_before is [3B,2B,1B] (index 0/1/2)").
_OBC_BASE_ORDER = ("3B", "2B", "1B")


def _trace_base_occupants(half_moments: list[dict]) -> dict[str, dict]:
    """Replays this half-inning's own already-built moments (in play order)
    to reconstruct WHO is actually standing on each currently-occupied base -
    not just the occupancy pattern (obc), which every real play already
    carries, but the specific runner's own identity. Nothing else in this
    pipeline has ever needed that (a real play's own runner tokens render
    anonymously, self-contained within that one play's own before/after diff
    - docs/js/app.js's resolveRunnerMoves/deriveRunnerMoves), but the on-deck
    placeholder needs to label who's on base ahead of a plate appearance that
    hasn't happened yet (Alex's ask) - see _next_batter_moment.

    Uses each moment's own runner_moves (utils._build_runner_moves_for_row) -
    a "from": "BATTER" entry is that moment's own batter reaching base
    (that function's own comment: only added when they're NOT out), every
    other "from" is a real base a previously-tracked runner is leaving,
    which is how identity carries forward leg to leg. A moment with no
    trustworthy runner_moves (that function returned None for it) drops
    tracking for any base whose own occupancy actually changed there rather
    than guessing who's on it now - Alex's own standing rule for this
    placeholder generally (next_batter_info's own docstring: "never a
    guess").
    """
    occupants: dict[str, dict] = {}
    for m in half_moments:
        occupants = _advance_base_occupants(occupants, m)
    return occupants


def _advance_base_occupants(occupants: dict[str, dict], m: dict) -> dict[str, dict]:
    """One replay step of _trace_base_occupants's own algorithm (see there
    for the full rationale) - split out so build()'s main play loop can
    thread this incrementally, play by play, to label a REAL play's own
    on-base runners (runners_on_base) with the same identity tracking that
    already existed only for the synthetic on-deck placeholder.
    """
    moves = m.get("runner_moves")
    before_obc = str(m.get("obc_before") or "000")
    after_obc = str(m.get("obc_after") or "000")
    if moves is None:
        next_occ = dict(occupants)
        for idx, base in enumerate(_OBC_BASE_ORDER):
            if before_obc[idx] != after_obc[idx]:
                next_occ.pop(base, None)
        return next_occ
    next_occ = dict(occupants)
    for mv in moves:
        frm = mv.get("from")
        if frm in next_occ:
            del next_occ[frm]
    for mv in moves:
        frm, to = mv.get("from"), mv.get("to")
        if to not in ("1B", "2B", "3B"):
            continue
        if frm == "BATTER":
            next_occ[to] = {"id": m.get("batter_id"), "name": m.get("batter_name")}
        else:
            ident = occupants.get(frm)
            if ident is not None:
                next_occ[to] = ident
            else:
                next_occ.pop(to, None)
    return next_occ


def _next_batter_moment(ref: dict, game_plays: list[dict], last_state: dict,
                         last_moment: dict, game_lineup_rows: list[dict],
                         game_moments: list[dict]) -> dict | None:
    """Synthesizes an on-deck placeholder moment for an in-progress game's
    live edge - who's due up next, per the live Lineups tab (Alex's ask,
    ideas-and-opinions conversation). Not a real play: no result, no pitch/
    swing, no ball flight. `is_on_deck` is the flag docs/js/app.js's
    stateStack/sceneResultPillHtml/card() checks to swap in a "Now Batting"
    pill and hide result-dependent detail, while the rest of the normal play
    scene/card renders exactly as it would for a real play, since every
    other field here is null-safe throughout that pipeline. is_key_moment is
    always False (Alex's call: this is a regular play, not a curated
    moment) - it still reaches the card feed via plays_NN.json/"every play"
    mode, just never key_moments.json's curated one. Copies
    last_moment (the last REAL moment already built for this game) for
    every field that's just persisted state (score, team names/colors,
    game_code, ...) and overrides only what actually changes for "the
    next, not-yet-happened plate appearance."

    Returns None - never a guess - whenever next_batter_info itself can't
    resolve a name (the Lineups tab hasn't caught up to a very recent
    substitution, or this team's lineup rows aren't on the sheet at all
    yet) rather than showing a stale or wrong name.
    """
    half_ended = last_state["outs_after"] == 3
    inning = last_state["inning"] + 1 if (half_ended and last_state["half"] == "bottom") else last_state["inning"]
    half = ("top" if last_state["half"] == "bottom" else "bottom") if half_ended else last_state["half"]
    outs = 0 if half_ended else last_state["outs_after"]
    obc = "000" if half_ended else last_state["obc_after"]
    batting_is_home = (half == "bottom")

    # Who's actually on base right now, [full_name, last_name] per occupied
    # base - same shape _defense_entry already returns for the defense dict,
    # so the client can reuse identical last-name-label rendering (Alex's
    # ask: label base runners on the on-deck slide the same way fielders are
    # already labeled during a real play). half_ended means the frame resets
    # to empty before this plate appearance - nothing to trace. Otherwise,
    # trace only THIS still-open half-inning's own moments (obc always
    # starts empty at the top of every half-inning, so there's no earlier
    # state to carry in from outside that window).
    on_base_runners: dict[str, list[str]] = {}
    if not half_ended:
        half_moments = sorted(
            (gm for gm in game_moments
             if gm.get("inning") == last_state["inning"] and gm.get("half") == last_state["half"]),
            key=lambda gm: gm["play_num"],
        )
        for base, ident in _trace_base_occupants(half_moments).items():
            entry = _defense_entry(ref, ident.get("id"), ident.get("name"))
            if entry:
                on_base_runners[base] = entry

    play = last_state["play"]
    off = _team_view(ref, play.get("home") if batting_is_home else play.get("away"))
    deff = _team_view(ref, play.get("away") if batting_is_home else play.get("home"))

    # This team's own most recent real at-bat, anywhere earlier in the game
    # (not just this half-inning - the leadoff batter of a brand-new half
    # needs their own last at-bat from whichever earlier inning they hit
    # in), ordered by play_num like replay_game itself sorts on.
    team_half = "bottom" if batting_is_home else "top"
    team_plays = sorted((p for p in game_plays if p.get("half") == team_half), key=lambda p: p["play_num"])
    last_batter_id = team_plays[-1].get("batter_id") if team_plays else None

    next_up = utils.next_batter_info(game_lineup_rows, off["abbrev"], last_batter_id, ref["name_to_id"])
    if next_up is None:
        return None

    # The defensive team's most recent pitching appearance: every row in
    # team_plays above (the offense team's own past at-bats) was pitched by
    # the defense - that's what "defense" means for that half - so its
    # pitcher_id is exactly the defense team's current pitcher. Empty only at
    # a game's very first half-inning turnover, when the team now on defense
    # has never pitched yet in this data (no lineup/starting-pitcher source
    # plumbed in here to guess from) - None rather than last_moment's
    # pitcher_id, which would belong to the team now AT BAT, not pitching.
    cur_pitcher_id = team_plays[-1].get("pitcher_id") if team_plays else None
    pitcher_view = _player_view(ref, cur_pitcher_id)

    # Situational leverage of the at-bat that's about to happen, computed
    # fresh off the current score/outs/OBC (last_moment's own leverage
    # describes a different, already-resolved situation) - same formula
    # replay_game uses for every real play, just fed this moment's state
    # instead of a play row's.
    lead_now = (last_moment["home_score"] - last_moment["away_score"]) if batting_is_home \
        else (last_moment["away_score"] - last_moment["home_score"])
    remaining_now = utils.remaining_half_innings(inning, half, MLN_INNINGS)
    leverage_now = utils.compute_leverage_re24(remaining_now, outs, obc, lead_now)

    # Carry the game's current win probability forward (re-expressed for
    # whichever team is now batting) so the ribbon's marker still has a
    # "current odds" readout to show even though nothing has happened yet on
    # this plate appearance - win_prob_before/wpa stay None (there is no
    # "before" for a play that hasn't happened), which is also what makes
    # the ribbon marker's own gain/WPA label go quiet on its own.
    last_wp_after = last_moment.get("win_prob_after")
    last_batting_is_home = last_moment.get("batting_is_home")
    home_wp = None
    if last_wp_after is not None and last_batting_is_home is not None:
        home_wp = last_wp_after if last_batting_is_home else 1 - last_wp_after
    next_wp_after = None if home_wp is None else (home_wp if batting_is_home else 1 - home_wp)

    # Scouted hand for the up-next batter (Alex's ask: the Now Batting slide
    # should stand them in the correct side of the box) - next_up itself only
    # carries what the Lineups tab has (name/slot/id), so this is its own
    # roster lookup rather than a field already on next_up.
    next_batter_view = _player_view(ref, next_up["player_id"])

    m = dict(last_moment)
    m.update({
        "moment_id": str(last_moment["play_num"]) + "-next",
        "play_num": last_moment["play_num"] + 1,
        "inning": inning, "half": half,
        "outs_before": outs, "outs_after": outs,
        "obc_before": obc, "obc_after": obc,
        "result": None, "result_category": None, "diff": None, "pitch": None, "swing": None,
        "throw_num": None, "steal_num": None, "throw_order": None,
        "excluded_positions": None, "default_position": None, "throw_order_by_position": None,
        "runner_moves": None, "runs": 0, "scoring_names": [],
        "batter_name": next_up["name"], "batter_id": next_up["player_id"],
        "batter_hand": next_batter_view["hand"] or None,
        "pitcher_name": pitcher_view["name"], "pitcher_id": pitcher_view["id"],
        "featured_name": next_up["name"], "featured_id": next_up["player_id"],
        "featured_side": "batting", "featured_team_abbr": off["abbrev"],
        "featured_wp_after": None, "featured_wpa": None,
        "counterpart_name": pitcher_view["name"] or None, "counterpart_id": pitcher_view["id"],
        "off_team_abbr": off["abbrev"], "def_team_abbr": deff["abbrev"],
        "batting_is_home": batting_is_home,
        "win_prob_before": None, "win_prob_after": next_wp_after, "wpa": None,
        "leverage": None if leverage_now is None else round(leverage_now, 2),
        "rookie": False, "is_key_moment": False, "tags": [],
        "is_half_inning_final": False, "is_game_final": False,
        "is_on_deck": True, "on_deck_order_slot": next_up["order_slot"],
    })
    # Omitted entirely (not an empty dict) when nobody's on - same convention
    # as "defense" above, so the client's existing missing-data handling
    # needs no special casing.
    if on_base_runners:
        m["on_base_runners"] = on_base_runners
    return m


def _game_start_on_deck_moment(ref: dict, game: dict, game_lineup_rows: list[dict]) -> dict | None:
    """Synthesizes the very first on-deck placeholder for a scheduled game
    that hasn't recorded a single play yet this session - the away team's
    leadoff batter, top of the 1st, 0-0, bases empty (Alex's report: "Now
    Batting" slides didn't appear at all for a game until it had its own
    first real play, even once the session itself had gone live via some
    other game). Same shape/contract as _next_batter_moment's own
    placeholder (is_on_deck: True, null result/pitch/swing/ball-flight) -
    just built directly from the Games-tab schedule and the Lineups tab
    instead of copied forward from a last real moment, since there isn't one
    yet. Returns None - never a guess - whenever next_batter_info can't
    resolve the away team's leadoff slot (the Lineups tab hasn't been filled
    in for this game yet either).

    Pitcher stays unresolved (id/name empty), same as _next_batter_moment's
    own "first half-inning turnover" edge case - Path A deliberately never
    sources a pitcher from the Lineups tab (lineup_alignment_at's own rule:
    "P comes from the play row itself"), so a game with literally no play
    rows yet has no pitcher to show, consistent with that existing rule
    rather than a new guess.
    """
    game_code = game.get("game_code") or ""
    off = _team_view(ref, game.get("away_team"))
    deff = _team_view(ref, game.get("home_team"))

    next_up = utils.next_batter_info(game_lineup_rows, off["abbrev"], None, ref["name_to_id"])
    if next_up is None:
        return None
    next_batter_view = _player_view(ref, next_up["player_id"])
    pitcher_view = _player_view(ref, None)
    play_num = utils.make_play_num(game_code, 1)

    return {
        "moment_id": str(play_num) + "-next",
        "play_num": play_num,
        "game_code": game_code,
        "session_number": game.get("session_number"),
        "timestamp": _parse_timestamp(game.get("start_time")),

        "inning": 1, "half": "top",
        "outs_before": 0, "outs_after": 0,
        "obc_before": "000", "obc_after": "000",

        "result": None, "result_category": None, "diff": None, "pitch": None, "swing": None,
        "throw_num": None, "steal_num": None, "throw_order": None,
        "excluded_positions": None, "default_position": None, "throw_order_by_position": None,
        "runner_moves": None, "runs": 0, "scoring_names": [],

        "batter_name": next_up["name"], "batter_id": next_up["player_id"],
        "batter_hand": next_batter_view["hand"] or None,
        "pitcher_name": pitcher_view["name"], "pitcher_id": pitcher_view["id"],
        "runner_name": "", "runner_id": None,

        "featured_name": next_up["name"], "featured_id": next_up["player_id"],
        "featured_side": "batting", "featured_team_abbr": off["abbrev"],
        "featured_wp_after": None, "featured_wpa": None,

        "counterpart_name": pitcher_view["name"] or None, "counterpart_id": pitcher_view["id"],

        "off_team_abbr": off["abbrev"], "def_team_abbr": deff["abbrev"],
        "away_team_abbr": off["abbrev"], "home_team_abbr": deff["abbrev"],
        "away_score": 0, "home_score": 0,
        "batting_is_home": False,

        "win_prob_before": None, "win_prob_after": None, "wpa": None,
        "leverage": LEVERAGE_AT_GAME_START,

        "rookie": next_batter_view["rookie"], "is_key_moment": False, "tags": [],
        "is_half_inning_final": False, "is_game_final": False,
        "is_on_deck": True, "on_deck_order_slot": next_up["order_slot"],

        "batter_spd": next_batter_view["spd"], "pitcher_spd": pitcher_view["spd"],
        "pitcher_awr": pitcher_view["awr"],
    }


# ── build ─────────────────────────────────────────────────────────────────────

def build(sheet_id: str = MLN_SHEET_ID, archive_season: int | None = None,
          teams_raw: list[dict] | None = None, players_raw: list[dict] | None = None,
          games_raw: list[dict] | None = None, plays_raw: list[dict] | None = None,
          ) -> tuple[list[dict], list[dict], dict]:
    """Return (all plays scored and tagged, roster, meta).

    `archive_season` set -> build from the MLN Historical Archive sheet
    instead of the live season: reads the archive's `Rosters`/`Teams`/`Games`/
    `Plays` tabs (vs. `Players`/`Teams`/`Games`/`Plays (Raw)` for the current
    season), filters every tab to this season, and every archive game is
    treated as final (Part 0 finding 1 - archive Games always has Win/scores
    populated). The `*_raw` params let `--archive-all` fetch each tab once
    and reuse it across all twelve seasons instead of re-fetching per season.
    """
    ref = load_reference(sheet_id, archive_season, teams_raw, players_raw)

    games = games_raw if games_raw is not None else utils.read_mln_games_from_sheet(sheet_id)
    if archive_season is not None:
        games = [g for g in games if g.get("season") == archive_season]
    if not games:
        raise RuntimeError("No games found in the MLN Games tab.")
    game_by_code = {g["game_code"]: g for g in games if g.get("game_code")}

    plays_tab = "Plays" if archive_season is not None else "Plays (Raw)"
    plays_gid = ARCHIVE_PLAYS_GID if archive_season is not None else None
    plays = plays_raw if plays_raw is not None else utils.read_mln_plays_from_sheet(sheet_id, tab=plays_tab, gid=plays_gid)
    if archive_season is not None:
        plays = [p for p in plays if p.get("season") == archive_season]
    if not plays:
        raise RuntimeError(f"No plays found in the MLN '{plays_tab}' tab.")

    by_game: dict[str, list[dict]] = {}
    for p in plays:
        pitch, swing = p.get("pitch"), p.get("swing")
        pitch = int(pitch) if pitch is not None else None
        swing = int(swing) if swing is not None else None
        p["pitch"], p["swing"] = pitch, swing
        p["diff"] = utils.circular_diff(pitch, swing) if pitch is not None and swing is not None else None
        by_game.setdefault(p["game_code"], []).append(p)

    # See _detect_archive_score_convention's docstring - the archive sheet's
    # a_Scr/h_Scr convention flips between seasons, so this is re-derived per
    # build rather than assumed from archive_season alone. Live season always
    # uses the pre-play convention (archive_mode=False), unconditionally.
    archive_score_post = _detect_archive_score_convention(by_game) if archive_season is not None else False
    if archive_season is not None:
        print(f"  season {archive_season}: archive score convention = "
              f"{'post-play' if archive_score_post else 'pre-play (live-style)'}", file=sys.stderr)
        repaired = sum(1 for p in plays if p.get("_obc_repaired"))
        unresolved = sum(1 for p in plays if p.get("_obc_cell_has_extra"))
        if repaired or unresolved:
            print(f"  season {archive_season}: obc repaired from Playcode on {repaired}/{len(plays)} plays "
                  f"({unresolved} still show a cell-only runner Playcode doesn't confirm - left as-is)",
                  file=sys.stderr)

    # Lineups is only ever needed for in-progress games (Decision 1: never
    # rely on it for completed games) - fetched at most once per build run,
    # lazily, the first time a non-final game is actually encountered below.
    lineups_by_game: dict[str, list[dict]] | None = None

    # Scorecard feature: the same in-progress-game Lineups rows Path A above
    # already resolves per play (_defense_alignment_for_play) also carry the
    # batting-order slot (order_slot) that per-play defense dict throws away -
    # exactly what the scorecard's lineup header needs for a live game (a
    # completed game has no such feed and instead infers its batting order
    # from the plays themselves, client-side). Collected per game_code here
    # and exported below so the JSON's scoreboard tile for that game carries
    # it; trimmed of game_id_short since it's redundant once keyed by code.
    lineup_rows_by_game: dict[str, list[dict]] = {}

    # Every play is scored and tagged; is_key_moment records which ones qualify.
    # The walk has to happen anyway to get WPA, so keeping the non-qualifying
    # rows costs nothing and is what the favorites view browses.
    rows: list[dict] = []
    for game_code in sorted(by_game):
        game = game_by_code.get(game_code)
        game_plays = by_game[game_code]
        if archive_season is not None:
            # Every archive game is final (Part 0 finding 1); the Lineups
            # tab it would otherwise fall back to for a non-final game
            # doesn't exist on the archive sheet. Treat as final even on the
            # handful of data quirks where Win/scores are missing (Part 4
            # risk) rather than crash on a nonexistent tab fetch.
            is_final = True
            if not utils.game_is_final(game):
                print(f"WARNING: archive game {game_code} missing Win/scores; treating as final anyway",
                      file=sys.stderr)
        else:
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
            if game_lineup_rows:
                lineup_rows_by_game[game_code] = [
                    {k: r[k] for k in ("team", "pos", "player_name", "order_slot", "row", "seq")}
                    for r in game_lineup_rows
                ]

        last_state = None
        game_rows_start = len(rows)
        # Same incremental base-occupant tracking _trace_base_occupants
        # already does for the on-deck placeholder (_advance_base_occupants,
        # its own extracted single-step helper), just threaded live through
        # every REAL play instead of replayed once at the end - so a normal
        # play's runner-advance animation can use an existing baserunner's
        # own real spd, not just the batter becoming a new one. Reset at
        # every half-inning boundary (obc always starts empty there, same
        # reasoning _next_batter_moment's own half-inning filter already
        # relies on).
        occ_half_key = None
        occupants: dict[str, dict] = {}
        for state in replay_game(game_plays, game, archive_mode=archive_score_post):
            last_state = state
            play = state["play"]
            defense = _defense_alignment_for_play(
                ref, play, state["half"], is_final, home_timeline, away_timeline, game_lineup_rows,
            )
            half_key = (state["inning"], state["half"])
            if half_key != occ_half_key:
                occupants = {}
                occ_half_key = half_key
            runners_on_base: dict[str, list] = {}
            for base, ident in occupants.items():
                entry = _defense_entry(ref, ident.get("id"), ident.get("name"))
                if entry:
                    runners_on_base[base] = entry
            moment = build_moment(ref, state, game, moment_tags(state), defense, runners_on_base)
            rows.append(moment)
            occupants = _advance_base_occupants(occupants, moment)

        # On-deck placeholder for an in-progress game's live edge (Alex's
        # ask, ideas-and-opinions conversation) - never for a completed game
        # (Path B's reconstructed timeline has no concept of "what happens
        # after the last real play" - there isn't one) or one whose last
        # real play already ended it. Reads rows[-1]'s own is_game_final
        # (which already folds in the _is_walkoff_final inference above)
        # rather than last_state["game_ended"] directly, so a game that's
        # mathematically over but whose Games-tab Win column hasn't caught up
        # yet doesn't grow a "Now Batting" placeholder for a plate appearance
        # that will never happen.
        if not is_final and last_state is not None and rows and not rows[-1]["is_game_final"]:
            # This game's own already-built moments (obc_before/after,
            # runner_moves, batter_name/id - the real fields _next_batter_
            # moment's own base-occupant tracer needs), not the raw game_plays
            # rows those fields don't exist on yet at this point in the
            # pipeline (build_moment is what computes them).
            next_moment = _next_batter_moment(ref, game_plays, last_state, rows[-1], game_lineup_rows,
                                               rows[game_rows_start:])
            if next_moment:
                rows.append(next_moment)

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

    # A "Now Batting" placeholder for every OTHER scheduled game in an
    # already-live session too (Alex's report), not just the ones that
    # happen to have their own first real play yet - archive builds skip
    # this entirely (every archive game already has all its plays, and the
    # archive sheet has no Lineups tab to source a leadoff batter from
    # anyway). game_is_final guards a data-quality edge case (a game marked
    # final on the Games tab - e.g. a forfeit - with no plays recorded at
    # all) from growing a placeholder for a plate appearance that will never
    # happen, same reasoning the real on-deck placeholder above already
    # applies mid-game.
    if archive_season is None:
        for g in games:
            game_code = g.get("game_code")
            if not game_code or game_code in by_game or g.get("session_number") not in sessions_set:
                continue
            if utils.game_is_final(g):
                continue
            if lineups_by_game is None:
                lineups_by_game = {}
                for row in utils.read_lineups_from_sheet(sheet_id):
                    lineups_by_game.setdefault(row["game_id_short"], []).append(row)
            game_lineup_rows = lineups_by_game.get(g.get("game_id_short"), [])
            start_moment = _game_start_on_deck_moment(ref, g, game_lineup_rows)
            if start_moment:
                rows.append(start_moment)

    rows.sort(key=lambda m: (m["timestamp"] or "", m["play_num"]), reverse=True)

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
        "season": archive_season if archive_season is not None else CURRENT_SEASON,
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
                "stadium": ref["team_by_abbrev"].get(abbr, {}).get("stadium") or "",
            }
            for abbr in teams_seen
        },
        # result is None on an on-deck placeholder moment (_next_batter_moment
        # - there's no result yet, it hasn't happened) - excluded here, not
        # just sorted around, since neither lookup needs (or could sensibly
        # have) an entry for "no result at all".
        "result_labels": {r: RESULT_LABELS.get(r, r) for r in sorted({m["result"] for m in rows if m["result"] is not None})},
        "result_short": {r: RESULT_SHORT.get(r, r) for r in sorted({m["result"] for m in rows if m["result"] is not None})},
        "tag_labels": dict(TAG_LABELS),
        "bases_svg": {obc: _diamond_svg(obc) for obc in sorted(utils.BRC_TO_OBC.values())},
        "flight": _flight_meta(archive_season),
        # One scoreboard per session, keyed by session number as a string
        # (JSON object keys are always strings) - the page shows whichever
        # one matches its session selector and hides the section entirely
        # for "Full season".
        "games": {str(s): _scoreboard(rows, games, s, lineup_rows_by_game, ref["name_to_id"]) for s in sessions},
    }
    if archive_season is not None:
        meta["is_archive"] = True
        meta["post_sessions"] = sorted({
            g["session_number"] for g in games
            if g.get("session_number") in sessions_set and g.get("type") == "Post"
        })
    return rows, roster, meta


def _flight_meta(archive_season: int | None = None) -> dict:
    """meta.flight: the ball-flight staging table, read through
    utils.get_diff_bands(archive_season) the same way every other
    precomputed CSV reaches this build, plus the exclusion list as data so
    app.js has one definition of in-scope-for-flight rather than a second
    hardcoded copy of FLIGHT_EXCLUDED.

    Historical seasons 1-4 get their own band_lo/band_hi per result (Alex's
    ask - compute_result_diff_bands.py --season-start 1 --season-end 4),
    selected here by archive_season; every other season (5-12, and the live
    current one) keeps the existing pooled bands. la/ev/depth/stations are
    identical between pools either way - Statcast has no season dimension.

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
    walking a batter who did nothing to the dugout.

    `stations` (from utils._FLIGHT_STATIONS/flight_stations.csv) is the
    joint EV/LA/distance selection table (ideas-and-opinions conversation):
    laMin/laIdeal/laMax/evMin/evMax above are reference/audit only (kept for
    the old worked-example tests and human eyeballing) - app.js's
    stationsLookup reads `stations` instead, picking a real play's own
    paired (LA, EV, distance) at this q rather than two independent marginal
    ranges (or, for one design iteration in between, an EV solved backward
    through our own physics, or a distance shared across two different real
    plays). distTopped/distUppercut are each that same real play's own
    hit_distance_sc - what the runtime radially rescales the rendered flight
    to. Compact keys since this repeats ~100x per result: q, laTopped,
    evTopped, distTopped, laUppercut, evUppercut, distUppercut.

    stationsBySpray (gameday reconciliation spray-bucket stage): a sibling
    key, present ONLY for the results compute_flight_ranges.py actually
    builds spray-conditioned stations for (2B/3B/1BWH/1BWH2 today), one
    `stations`-shaped array per real horizontal-spray bucket
    (utils._FLIGHT_STATIONS_BY_SPRAY). Every other result's band keeps
    exactly today's shape - this is purely additive. `stations` above (the
    unconditioned pool) is unchanged and remains every result's own runtime
    fallback for a bucket with no entry here - see docs/js/app.js's
    flightParams, which reads stationsBySpray[bucket] when present and falls
    back to the top-level `stations` otherwise."""
    def _station_dict(st: dict) -> dict:
        return {
            "q": st["q"],
            "laTopped": st["la_topped"], "evTopped": st["ev_topped"], "distTopped": st["dist_topped"],
            "laUppercut": st["la_uppercut"], "evUppercut": st["ev_uppercut"], "distUppercut": st["dist_uppercut"],
        }

    by_spray: dict[str, dict[str, list]] = {}
    for (result, bucket), stations in utils._FLIGHT_STATIONS_BY_SPRAY.items():
        by_spray.setdefault(result, {})[bucket] = [_station_dict(st) for st in stations]

    bands = {
        result: {
            "archetype": row["archetype"], "lo": row["band_lo"], "hi": row["band_hi"],
            "laMin": row["la_min"], "laIdeal": row["la_ideal"], "laMax": row["la_max"],
            "evMin": row["ev_min"], "evMax": row["ev_max"],
            "depthMin": row["depth_min"], "depthMax": row["depth_max"],
            "stations": [_station_dict(st) for st in utils._FLIGHT_STATIONS.get(result, [])],
            **({"stationsBySpray": by_spray[result]} if result in by_spray else {}),
        }
        for result, row in utils.get_diff_bands(archive_season).items()
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


def _pitcher_decisions_from_game(game: dict | None, name_to_id: dict) -> dict | None:
    """Resolve a Games-tab row's Winning Pitcher/Losing Pitcher/Save/Hold/
    Hold.1 columns (plain names, per utils.read_mln_games_from_sheet) to
    player_ids via the same name_to_id lookup load_reference already builds
    for every other name-keyed field. This is the league's own actual
    decision, not app.js's client-side computeDecisions heuristic (no
    starter-innings minimum, no exact save conditions) - the scorecard falls
    back to that heuristic only when a game has no resolvable decisions here
    (blank columns, or a name that doesn't match the roster).
    """
    if not game:
        return None
    decisions: dict[int, str] = {}
    def _add(name: str | None, code: str) -> None:
        pid = name_to_id.get((name or "").strip())
        if pid:
            decisions[pid] = code
    _add(game.get("winning_pitcher"), "W")
    _add(game.get("losing_pitcher"), "L")
    _add(game.get("save_pitcher"), "SV")
    _add(game.get("hold_1"), "H")
    _add(game.get("hold_2"), "H")
    return decisions or None


def _scoreboard(rows: list[dict], games: list[dict], session: int,
                 lineup_rows_by_game: dict[str, list[dict]] | None = None,
                 name_to_id: dict | None = None) -> list[dict]:
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
    games_by_code = {g["game_code"]: g for g in games if g.get("game_code")}

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

        tile = {
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
        }
        # Scorecard feature (live lineup source): only a non-final game ever
        # has rows here (lineup_rows_by_game is populated only in build()'s
        # is_final=False branch) - omitted entirely for a finished game so
        # its JSON stays exactly as small as before this feature existed.
        if not is_final and lineup_rows_by_game and lineup_rows_by_game.get(m["game_code"]):
            tile["lineup_rows"] = lineup_rows_by_game[m["game_code"]]
        # Games-tab pitcher decisions (W/L/SV/H), same "only when it applies"
        # sizing as lineup_rows above - a non-final game has no decisions yet,
        # and one without resolvable columns just omits the key so app.js
        # knows to fall back to its own heuristic.
        if is_final and name_to_id:
            dec = _pitcher_decisions_from_game(games_by_code.get(m["game_code"]), name_to_id)
            if dec:
                tile["decisions"] = dec
        tiles.append(tile)

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


def _prune_stale_session_files(sessions: list[int], out_dir: str = OUT_DIR) -> None:
    """Drop plays_NN.json files for sessions that no longer exist in the feed.

    Only ever called for the current-season build (out_dir stays the default
    top-level docs/data) - an archive season's dir is a one-time build with
    no iterative churn to prune, and scanning/pruning docs/data itself from
    inside an archive build would risk deleting the live season's files.
    """
    keep = {f"plays_{s:02d}.json" for s in sessions}
    for name in os.listdir(out_dir):
        if name.startswith("plays_") and name.endswith(".json") and name not in keep:
            os.remove(os.path.join(out_dir, name))


def _archive_seasons_on_disk() -> list[dict]:
    """Already-committed docs/data/sNN/ archive dirs, each as {"season":
    N, "sessions": [...]} - how the live build's meta.json tells the client
    what to offer (Part 2: 'discovered by scanning docs/data/s*/ in the
    checkout') without an extra request. Each archive season's own sessions
    list rides along (read straight off that season's already-built
    meta.json) so the client can build its season+session picker for every
    season without fetching each one's data up front.
    """
    if not os.path.isdir(OUT_DIR):
        return []
    seasons = []
    for name in sorted(os.listdir(OUT_DIR)):
        m = re.fullmatch(r"s(\d{2})", name)
        if not m or not os.path.isdir(os.path.join(OUT_DIR, name)):
            continue
        sessions = []
        try:
            with open(os.path.join(OUT_DIR, name, "meta.json"), encoding="utf-8") as fh:
                sessions = json.load(fh).get("sessions", [])
        except (OSError, ValueError):
            pass
        seasons.append({"season": int(m.group(1)), "sessions": sessions})
    return sorted(seasons, key=lambda s: s["season"])


def _fetch_archive_raw(sheet_id: str) -> dict:
    """Fetch every archive tab exactly once. `--archive-all` reuses this
    across all twelve seasons instead of re-downloading the same ~75k-row
    Plays tab (and Teams/Rosters/Games) twelve times.
    """
    return {
        "teams_raw": utils.read_mln_teams_from_sheet(sheet_id),
        "players_raw": utils.read_mln_players_from_sheet(sheet_id, tab="Rosters"),
        "games_raw": utils.read_mln_games_from_sheet(sheet_id),
        "plays_raw": utils.read_mln_plays_from_sheet(sheet_id, tab="Plays", gid=ARCHIVE_PLAYS_GID),
    }


def _build_and_write(sheet_id: str, archive_season: int | None = None, **raw) -> None:
    """Build one season (current, when archive_season is None, or one
    historical archive season) and write its docs/data output. `**raw`
    forwards any of build()'s *_raw pre-fetched-tab params (used by
    --archive-all; empty for a single-season build).
    """
    out_dir = OUT_DIR if archive_season is None else os.path.join(OUT_DIR, f"s{archive_season:02d}")
    label = "current season" if archive_season is None else f"archive season {archive_season}"

    try:
        rows, roster, meta = build(sheet_id, archive_season=archive_season, **raw)
    except Exception as exc:
        print(f"FATAL ({label}): {exc}", file=sys.stderr)
        sys.exit(1)

    moments = [m for m in rows if m["is_key_moment"]]
    if not moments:
        print(f"FATAL ({label}): no key moments produced - refusing to publish an empty feed.", file=sys.stderr)
        sys.exit(1)

    if archive_season is None:
        meta["archive_seasons"] = _archive_seasons_on_disk()

    os.makedirs(out_dir, exist_ok=True)
    _write(os.path.join(out_dir, "key_moments.json"), moments)
    _write(os.path.join(out_dir, "players.json"), roster)
    _write(os.path.join(out_dir, "meta.json"), meta)
    for session in meta["sessions"]:
        session_rows = [m for m in rows if m["session_number"] == session]
        _write(os.path.join(out_dir, f"plays_{session:02d}.json"), session_rows)
    if archive_season is None:
        _prune_stale_session_files(meta["sessions"], out_dir)

    counts: dict[str, int] = {}
    per_session: dict[int, list[int]] = {}
    for m in rows:
        for t in m["tags"]:
            counts[t] = counts.get(t, 0) + 1
        if m["session_number"]:
            tally = per_session.setdefault(m["session_number"], [0, 0])
            tally[0] += 1
            tally[1] += 1 if m["is_key_moment"] else 0

    print(f"[{label}] Scanned {meta['plays_scanned']} plays -> {len(moments)} key moments "
          f"across sessions {meta['sessions']}")
    print(f"  thresholds: leverage >= {LEVERAGE_THRESHOLD}, |wpa| >= {WPA_THRESHOLD}")
    for session in sorted(per_session, reverse=True):
        plays_n, moments_n = per_session[session]
        print(f"  session {session:02d}: {moments_n} moments / {plays_n} plays")
    for tag in TAG_LABELS:
        print(f"  tag {tag:<28} {counts.get(tag, 0)}")
    print(f"Wrote {out_dir}/key_moments.json, players.json, meta.json, "
          f"plays_NN.json x{len(meta['sessions'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static Key Moments feed.")
    parser.add_argument("--archive-season", type=int, default=None, metavar="N",
                         help="Build one historical season (1-12) from the MLN Historical Archive sheet "
                              "into docs/data/sNN/ instead of building the current season.")
    parser.add_argument("--archive-all", action="store_true",
                         help="Build every season found in the archive's Games tab (one fetch of each "
                              "archive tab, reused across all seasons).")
    args = parser.parse_args()

    if args.archive_all:
        raw = _fetch_archive_raw(ARCHIVE_SHEET_ID)
        seasons = sorted({g["season"] for g in raw["games_raw"] if g.get("season")})
        if not seasons:
            print("FATAL: no seasons found in the archive Games tab.", file=sys.stderr)
            sys.exit(1)
        for season in seasons:
            _build_and_write(ARCHIVE_SHEET_ID, archive_season=season, **raw)
        return

    if args.archive_season is not None:
        _build_and_write(ARCHIVE_SHEET_ID, archive_season=args.archive_season)
        return

    _build_and_write(MLN_SHEET_ID, archive_season=None)


if __name__ == "__main__":
    main()

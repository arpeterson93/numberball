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

# Inclusion thresholds - tuned against real per-session volume, see the
# "threshold" summary printed at the end of a build.
LEVERAGE_THRESHOLD = 2.0
WPA_THRESHOLD = 0.12   # 12 percentage points of win probability

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

# The complete, closed set of tag slugs. The page renders one filter chip per
# entry, in this order, so adding a rule means adding it here too.
TAG_LABELS = {
    "zero_diff": "0 Diff",
    "five_hundred_diff": "500 Diff",
    "steal": "Steal",
    "strikeout_risp_inning_end": "Inning-ending K w/ RISP",
    "run_scoring_hit": "Run-scoring hit",
    "bases_loaded": "Bases loaded",
    "lead_change": "Lead change",
    "high_leverage": "High leverage",
}


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
    """Names of runners who scored on this play, in the order they'd have
    crossed the plate: 3rd, then 2nd, then 1st.

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
        names.append(_player_view(ref, pid)["name"])
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
    return {
        "teams": teams,
        "players": players,
        "team_by_id": by_team_id,
        "team_by_abbrev": by_abbrev,
        "player_by_id": by_player_id,
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
        return {"id": None, "name": "", "rookie": False, "team": ""}
    p = ref["player_by_id"].get(player_id)
    if not p:
        # Traded, released, or otherwise off the current roster tab.
        return {"id": player_id, "name": f"Player {player_id}", "rookie": False, "team": ""}
    return {
        "id": player_id,
        "name": p.get("name") or f"Player {player_id}",
        "rookie": bool(p.get("is_rookie")),
        "team": p.get("team") or "",
    }


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
    """Has this game actually finished?

    The Games tab's end_time and last_play both track live and keep updating
    while a game is in progress, so neither can stand in for completion -
    win_team is the field that only appears once the game is over.
    """
    return bool(game and game.get("win_team") and game.get("away_score") is not None
                and game.get("home_score") is not None)


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

        leverage = utils.compute_leverage(
            utils.RESULT_RANGES, remaining, outs_before, obc_before, lead_before
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
    """Inclusion rules - any one firing makes the play a key moment.

    Returned slugs are exactly the keys of TAG_LABELS, in that order.
    """
    play = state["play"]
    result = play.get("result") or ""
    diff = play.get("diff")
    wpa = state["wpa"]
    leverage = state["leverage"]
    obc_before = state["obc_before"]
    tags: list[str] = []

    if diff == 0:
        tags.append("zero_diff")
    if diff == 500:
        tags.append("five_hundred_diff")
    if result in STEAL_SUCCESS_CODES:
        tags.append("steal")
    if result == "K" and state["outs_after"] == 3 and (obc_before[0] == "1" or obc_before[1] == "1"):
        tags.append("strikeout_risp_inning_end")
    if result in HIT_CODES and state["runs"] > 0:
        tags.append("run_scoring_hit")
    if state["obc_after"] == "111":
        tags.append("bases_loaded")
    if _sign(state["lead_before"]) != _sign(state["lead_after"]):
        tags.append("lead_change")
    if ((leverage is not None and leverage >= LEVERAGE_THRESHOLD)
            or (wpa is not None and abs(wpa) >= WPA_THRESHOLD)):
        tags.append("high_leverage")
    return tags


def _featured(ref: dict, state: dict, off: dict, deff: dict) -> dict:
    """Which player the card headlines, and which side of the play they are on."""
    play = state["play"]
    result = play.get("result") or ""
    batter = _player_view(ref, play.get("batter_id"))
    pitcher = _player_view(ref, play.get("pitcher_id"))
    catcher = _player_view(ref, play.get("catcher_id"))
    runner = _player_view(ref, play.get("runner_id"))

    if result in STEAL_SUCCESS_CODES and runner["id"]:
        player, side = runner, "batting"
    elif result in CAUGHT_STEALING_CODES:
        player, side = (catcher if catcher["id"] else pitcher), "fielding"
    elif _result_category(result) == "hitting":
        player, side = batter, "batting"
    elif _result_category(result) == "pitching":
        player, side = pitcher, "fielding"
    else:
        player, side = batter, "batting"

    team = off if side == "batting" else deff
    return {"player": player, "side": side, "team": team,
            "batter": batter, "pitcher": pitcher, "runner": runner}


def build_moment(ref: dict, state: dict, game: dict | None, tags: list[str]) -> dict:
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
        "result_category": _result_category(result),
        "diff": play.get("diff"),
        "runs": state["runs"],
        "scoring_names": scoring_names,

        "batter_name": feat["batter"]["name"],
        "batter_id": feat["batter"]["id"],
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
        "is_key_moment": bool(tags),
        "tags": tags,
        "is_half_inning_final": state["outs_after"] == 3,
        "is_game_final": state["game_ended"],
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
        p["diff"] = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None
        by_game.setdefault(p["game_code"], []).append(p)

    # Every play is scored and tagged; is_key_moment records which ones qualify.
    # The walk has to happen anyway to get WPA, so keeping the non-qualifying
    # rows costs nothing and is what the favorites view browses.
    rows: list[dict] = []
    for game_code in sorted(by_game):
        game = game_by_code.get(game_code)
        for state in replay_game(by_game[game_code], game):
            rows.append(build_moment(ref, state, game, moment_tags(state)))

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

    sessions = sorted({m["session_number"] for m in rows if m["session_number"]}, reverse=True)
    teams_seen = sorted({a for m in rows for a in (m["off_team_abbr"], m["def_team_abbr"]) if a})
    meta = {
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sheet_id": sheet_id,
        "season": CURRENT_SEASON,
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
            }
            for abbr in teams_seen
        },
        "result_labels": {r: RESULT_LABELS.get(r, r) for r in sorted({m["result"] for m in rows})},
        "tag_labels": dict(TAG_LABELS),
        "bases_svg": {obc: _diamond_svg(obc) for obc in sorted(utils.BRC_TO_OBC.values())},
    }
    return rows, roster, meta


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

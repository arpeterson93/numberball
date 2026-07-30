"""Build the static Key Moments feed served from docs/ on GitHub Pages.

Reads the MLN public Google Sheet directly (no Supabase round-trip), replays
every game play-by-play, computes win probability added and leverage with the
existing utils.py machinery, applies the key-moment tag rules, and writes:

    docs/data/key_moments.json   one entry per qualifying play
    docs/data/players.json       roster lookup for the favorites picker
    docs/data/meta.json          build timestamp, season, session list

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

# Human-readable labels for the inclusion rules, shown as "why is this here" chips.
TAG_LABELS = {
    "diff_0": "0 Diff",
    "diff_500": "500 Diff",
    "steal": "Steal",
    "k_risp_inning_end": "K to strand RISP",
    "rbi_hit": "RBI hit",
    "bases_loaded": "Bases loaded",
    "lead_change": "Lead change",
    "high_leverage": "High leverage",
    "big_wpa": "Big swing",
}


# ── small helpers ─────────────────────────────────────────────────────────────

def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


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
    return bool(game and game.get("win_team") and game.get("away_score") is not None
                and game.get("home_score") is not None)


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

        ended = is_last and _game_is_final(game)
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
    """Inclusion rules - any hit makes the play a key moment."""
    play = state["play"]
    result = play.get("result") or ""
    diff = play.get("diff")
    wpa = state["wpa"]
    leverage = state["leverage"]
    obc_before = state["obc_before"]
    tags: list[str] = []

    if diff == 0:
        tags.append("diff_0")
    if diff == 500:
        tags.append("diff_500")
    if result in STEAL_SUCCESS_CODES:
        tags.append("steal")
    if result == "K" and state["outs_after"] == 3 and (obc_before[0] == "1" or obc_before[1] == "1"):
        tags.append("k_risp_inning_end")
    if result in HIT_CODES and state["runs"] > 0:
        tags.append("rbi_hit")
    if state["obc_after"] == "111":
        tags.append("bases_loaded")
    if _sign(state["lead_before"]) != _sign(state["lead_after"]):
        tags.append("lead_change")
    if leverage is not None and leverage >= LEVERAGE_THRESHOLD:
        tags.append("high_leverage")
    if wpa is not None and abs(wpa) >= WPA_THRESHOLD:
        tags.append("big_wpa")
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

    sub_leagues = sorted({s for s in (off["sub_league"], deff["sub_league"]) if s})
    session_number = int(game_code[2:4]) if len(game_code) >= 4 and game_code[2:4].isdigit() else None

    return {
        "moment_id": str(play["play_num"]),
        "play_num": play["play_num"],
        "game_code": game_code,
        "season": CURRENT_SEASON,
        "session_number": session_number,
        "timestamp": _parse_timestamp(play.get("timestamp")),
        "timestamp_raw": play.get("timestamp"),

        "inning": state["inning"],
        "half": state["half"],
        "outs_before": state["outs_before"],
        "outs_after": state["outs_after"],
        "obc_before": state["obc_before"],
        "obc_after": state["obc_after"],
        "bases_svg": _diamond_svg(state["obc_after"]),

        "result": result,
        "result_label": RESULT_LABELS.get(result, result),
        "result_category": _result_category(result),
        "diff": play.get("diff"),
        "runs": state["runs"],

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

        "off_team": off["full"],
        "off_team_abbr": off["abbrev"],
        "def_team": deff["full"],
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

        "sub_leagues": sub_leagues,
        "rookie": feat["player"]["rookie"],
        "tags": tags,
        "tag_labels": [TAG_LABELS.get(t, t) for t in tags],
    }


# ── build ─────────────────────────────────────────────────────────────────────

def build(sheet_id: str = MLN_SHEET_ID) -> tuple[list[dict], list[dict], dict]:
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

    moments: list[dict] = []
    scanned = 0
    for game_code in sorted(by_game):
        game = game_by_code.get(game_code)
        for state in replay_game(by_game[game_code], game):
            scanned += 1
            tags = moment_tags(state)
            if tags:
                moments.append(build_moment(ref, state, game, tags))

    moments.sort(key=lambda m: (m["timestamp"] or "", m["play_num"]), reverse=True)

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

    sessions = sorted({m["session_number"] for m in moments if m["session_number"]}, reverse=True)
    meta = {
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sheet_id": sheet_id,
        "season": CURRENT_SEASON,
        "sessions": sessions,
        "plays_scanned": scanned,
        "moment_count": len(moments),
        "leverage_threshold": LEVERAGE_THRESHOLD,
        "wpa_threshold": WPA_THRESHOLD,
    }
    return moments, roster, meta


def _write(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")


def main() -> None:
    try:
        moments, roster, meta = build()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    if not moments:
        print("FATAL: no key moments produced - refusing to publish an empty feed.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    _write(os.path.join(OUT_DIR, "key_moments.json"), moments)
    _write(os.path.join(OUT_DIR, "players.json"), roster)
    _write(os.path.join(OUT_DIR, "meta.json"), meta)

    counts: dict[str, int] = {}
    for m in moments:
        for t in m["tags"]:
            counts[t] = counts.get(t, 0) + 1
    per_session: dict[int, int] = {}
    for m in moments:
        if m["session_number"]:
            per_session[m["session_number"]] = per_session.get(m["session_number"], 0) + 1

    print(f"Scanned {meta['plays_scanned']} plays -> {len(moments)} key moments "
          f"across sessions {meta['sessions']}")
    print(f"  thresholds: leverage >= {LEVERAGE_THRESHOLD}, |wpa| >= {WPA_THRESHOLD}")
    for session in sorted(per_session, reverse=True):
        print(f"  session {session:02d}: {per_session[session]} moments")
    for tag in sorted(counts, key=lambda t: -counts[t]):
        print(f"  tag {tag:<20} {counts[tag]}")
    print(f"Wrote {OUT_DIR}/key_moments.json, players.json, meta.json")


if __name__ == "__main__":
    main()

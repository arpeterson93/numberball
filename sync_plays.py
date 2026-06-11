"""Standalone sync: RLN Games, RLN Plays, MLN Games, MLN Plays -> Supabase.

Credentials via environment variables (GitHub Actions) or .streamlit/secrets.toml (local):
    SUPABASE_URL  /  supabase_url
    SUPABASE_KEY  /  supabase_key

Usage:
    python sync_plays.py
"""
from __future__ import annotations
import sys
import database as db
import utils

_RLN_SHEET_ID = "1lcgT6np-4O5x83b2JZXjv8REfNDYXE7GMYMZeu5znRY"
_MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"

_GAMES_TABLE_COLS = {
    "game_code", "game_id_short", "league", "season", "session_number",
    "away_team", "home_team",
    "away_score", "home_score", "win_team", "loss_team",
    "umpire",
    "winning_pitcher", "losing_pitcher", "save_pitcher",
    "hold_1", "hold_2",
    "player_of_game",
    "honorable_mention_1", "honorable_mention_2", "honorable_mention_3",
    "start_time", "end_time",
    "last_play", "last_inning", "last_result",
    "division", "archive_sheet_id",
    "sheet_url", "link",
}


# ── RLN ───────────────────────────────────────────────────────────────────────

def sync_rln_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the RLN Games tab."]
    for g in games:
        g["league"] = "RLN"
    try:
        return db.bulk_upsert_games(games), []
    except Exception as e:
        return 0, [str(e)]


def sync_rln_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_plays_from_sheet(sheet_id)
    if not plays:
        return 0, ["No plays found in the RLN Plays (Raw) tab."]

    all_games   = db.get_games()
    all_players = db.get_all_players()
    game_code_to_id   = {g["game_code"]: g["id"] for g in all_games  if g.get("game_code")}
    player_id_to_name = {p["player_id"]: p["name"] for p in all_players
                         if p.get("player_id") and p.get("name")}

    plays_sorted  = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set]   = {}
    seen_pitcher: dict[str, set]   = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc         = play["game_code"]
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not in DB - sync RLN Games first.")
            continue

        pitcher_name = player_id_to_name.get(play.get("pitcher_id"), "")
        batter_name  = player_id_to_name.get(play.get("batter_id"),  "")
        catcher_name = player_id_to_name.get(play.get("catcher_id"))
        runner_name  = player_id_to_name.get(play.get("runner_id"))

        away_full = utils.TEAM_ABBREV.get(play.get("away") or "", play.get("away") or "")
        home_full = utils.TEAM_ABBREV.get(play.get("home") or "", play.get("home") or "")
        off_team, def_team = (away_full, home_full) if play["half"] == "top" else (home_full, away_full)

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        outs = outs_tracker.setdefault(inn_key, 0)
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn  = fp_inn_key    not in seen_inn.setdefault(gc, set())
        is_fp_app  = pitcher_name  not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k != "game_code"},
            "league":       "RLN",
            "game_id":      game_db_id,
            "pitcher_name": pitcher_name,
            "batter_name":  batter_name,
            "catcher_name": catcher_name,
            "runner_name":  runner_name,
            "off_team":     off_team,
            "def_team":     def_team,
            "diff":         diff,
            "outs":         outs,
            "is_fp_inn":    is_fp_inn,
            "is_fp_app":    is_fp_app,
        })

    if not rows:
        return 0, errors
    try:
        return db.bulk_upsert_plays(rows), errors
    except Exception as e:
        return 0, errors + [str(e)]


# ── MLN ───────────────────────────────────────────────────────────────────────

def sync_mln_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_mln_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the MLN Games tab."]
    abbrev_to_full = utils.read_mln_team_abbrev_lookup(sheet_id)
    for g in games:
        g["away_team"] = abbrev_to_full.get(g["away_team"], g["away_team"])
        g["home_team"] = abbrev_to_full.get(g["home_team"], g["home_team"])
        a_scr, h_scr = g.get("away_score"), g.get("home_score")
        if a_scr is not None and h_scr is not None:
            if a_scr > h_scr:
                g["win_team"], g["loss_team"] = g["away_team"], g["home_team"]
            elif h_scr > a_scr:
                g["win_team"], g["loss_team"] = g["home_team"], g["away_team"]
        g["archive_sheet_id"] = sheet_id
    games = [{k: v for k, v in g.items() if k in _GAMES_TABLE_COLS} for g in games]
    try:
        return db.bulk_upsert_games(games), []
    except Exception as e:
        return 0, [str(e)]


def sync_mln_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_mln_plays_from_sheet(sheet_id)
    if not plays:
        return 0, ["No plays found in the MLN Plays tab."]

    all_games = db.get_games()
    game_code_to_id = {g["game_code"]: g["id"] for g in all_games if g.get("game_code")}
    mln_game_codes  = sorted(g["game_code"] for g in all_games
                             if g.get("league") == "MLN" and g.get("game_code"))
    play_game_codes = sorted({p["game_code"] for p in plays})
    if not mln_game_codes:
        return 0, [
            f"No MLN games in DB - sync MLN Games first. "
            f"Plays reference game codes like: {play_game_codes[:5]}"
        ]
    missing = sorted(set(play_game_codes) - set(mln_game_codes))
    diag = ([
        f"DB has {len(mln_game_codes)} MLN game(s) e.g. {mln_game_codes[:5]}",
        f"Plays reference {len(play_game_codes)} game(s) e.g. {play_game_codes[:5]}",
        f"{len(missing)} game code(s) missing from DB e.g. {missing[:5]}",
    ] if missing else [])

    mln_teams   = db.get_mln_teams_for_lookup()
    mln_players = db.get_mln_players_for_lookup()
    team_id_to_full = {t["team_id"]: t["full_team"] for t in mln_teams
                       if t.get("team_id") and t.get("full_team")}
    sid_to_name     = {p["s_id"]: p["name"] for p in mln_players
                       if p.get("s_id") and p.get("name")}

    plays_sorted  = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set]   = {}
    seen_pitcher: dict[str, set]   = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc         = play["game_code"]
        season     = play.get("season") or (int(gc[:2]) if gc and len(gc) >= 2 and gc[:2].isdigit() else None)
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not in DB - sync MLN Games first.")
            continue

        def _sid(pid):
            return f"{season}_{pid}" if season and pid else None

        pitcher_name = sid_to_name.get(_sid(play.get("pitcher_id")), "") or ""
        batter_name  = sid_to_name.get(_sid(play.get("batter_id")),  "") or ""
        catcher_name = sid_to_name.get(_sid(play.get("catcher_id")))
        runner_name  = sid_to_name.get(_sid(play.get("runner_id")))

        away_full = team_id_to_full.get(play.get("away") or "", play.get("away") or "")
        home_full = team_id_to_full.get(play.get("home") or "", play.get("home") or "")
        off_team, def_team = (away_full, home_full) if play["half"] == "top" else (home_full, away_full)

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        outs = outs_tracker.setdefault(inn_key, 0)
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn  = fp_inn_key    not in seen_inn.setdefault(gc, set())
        is_fp_app  = pitcher_name  not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k not in ("game_code", "away", "home")},
            "game_id":      game_db_id,
            "pitcher_name": pitcher_name,
            "batter_name":  batter_name,
            "catcher_name": catcher_name,
            "runner_name":  runner_name,
            "off_team":     off_team,
            "def_team":     def_team,
            "diff":         diff,
            "outs":         outs,
            "is_fp_inn":    is_fp_inn,
            "is_fp_app":    is_fp_app,
        })

    if not rows:
        return 0, diag + errors
    rows = list({(r["play_num"], r.get("league", "MLN")): r for r in rows}.values())
    try:
        return db.bulk_upsert_mln_plays(rows), diag + errors
    except Exception as e:
        return 0, diag + errors + [str(e)]


# ── main ──────────────────────────────────────────────────────────────────────

def _run(label: str, fn, *args) -> bool:
    """Run a sync step, print results, return True if a hard error occurred."""
    print(f"\n=== {label} ===")
    try:
        n, msgs = fn(*args)
        print(f"  Upserted: {n}")
        for m in msgs:
            print(f"  {'ERROR' if 'not in DB' not in m and 'missing' not in m else 'WARN'}: {m}")
        return n == 0 and bool(msgs) and any("not in DB" not in m and "missing" not in m for m in msgs)
    except Exception as e:
        print(f"  FATAL: {e}", file=sys.stderr)
        return True


def main() -> None:
    hard_error = False
    hard_error |= _run("RLN Games",  sync_rln_games,  _RLN_SHEET_ID)
    hard_error |= _run("RLN Plays",  sync_rln_plays,  _RLN_SHEET_ID)
    hard_error |= _run("MLN Games",  sync_mln_games,  _MLN_SHEET_ID)
    hard_error |= _run("MLN Plays",  sync_mln_plays,  _MLN_SHEET_ID)
    print()
    if hard_error:
        print("Sync completed with errors.", file=sys.stderr)
        sys.exit(1)
    print("Sync completed successfully.")


if __name__ == "__main__":
    main()

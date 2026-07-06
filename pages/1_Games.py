"""Sync data from Google Sheets into Supabase."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

_RLN_SHEET_ID = "1lcgT6np-4O5x83b2JZXjv8REfNDYXE7GMYMZeu5znRY"
_MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"

# Update these each season - RLN and MLN plays export sheets have no Season column
_CURRENT_RLN_SEASON = 13
_CURRENT_MLN_SEASON = 13

st.title("Sync Data")

# ------------------------------------------------------------------ sync helpers

def _sync_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the Games tab."]
    # The RLN sheet's "League" column is a spreadsheet boolean, not the league name.
    for g in games:
        g["league"] = "RLN"
        g["game_type"] = "live"
    try:
        n = db.bulk_upsert_games(games)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_plays_from_sheet(sheet_id)
    if not plays:
        return 0, ["No plays found in the Plays (Raw) tab."]

    all_games   = db.get_games()
    all_players = db.get_all_players()
    game_code_to_id  = {g["game_code"]: g["id"] for g in all_games if g.get("game_code")}
    player_id_to_name = {p["player_id"]: p["name"] for p in all_players if p.get("player_id") and p.get("name")}

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    outs_tracker: dict[tuple, int] = {}   # (game_code, inning, half) → outs before this play
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc = play["game_code"]
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not found - sync Games first.")
            continue

        # Player name lookups
        pitcher_name = player_id_to_name.get(play.get("pitcher_id"), "")
        batter_name  = player_id_to_name.get(play.get("batter_id"),  "")
        catcher_name = player_id_to_name.get(play.get("catcher_id"))
        runner_name  = player_id_to_name.get(play.get("runner_id"))

        # off/def team from away/home + inning half
        away_full = utils.TEAM_ABBREV.get(play.get("away") or "", play.get("away") or "")
        home_full = utils.TEAM_ABBREV.get(play.get("home") or "", play.get("home") or "")
        if play["half"] == "top":
            off_team, def_team = away_full, home_full
        else:
            off_team, def_team = home_full, away_full

        # Circular diff
        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        # Outs: running count per half-inning
        inn_key = (gc, play["inning"], play["half"])
        if inn_key not in outs_tracker:
            outs_tracker[inn_key] = 0
        outs = outs_tracker[inn_key]
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        # First-pitch flags (keyed on pitcher_name)
        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn = fp_inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher_name not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k != "game_code"},
            "game_code":    gc,
            "league":       "RLN",
            "game_type":    "live",
            "season":       _CURRENT_RLN_SEASON,
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
        n = db.bulk_upsert_plays(rows)
        return n, errors
    except Exception as e:
        return 0, errors + [str(e)]


def _sync_scrimmage_plays(game: dict) -> tuple[int, list[str]]:
    import re
    sheet_url = game.get("sheet_url") or ""
    m = re.search(r'/spreadsheets/d/([^/]+)', sheet_url)
    if not m:
        return 0, ["Could not extract sheet ID from game sheet_url."]
    sheet_id = m.group(1)

    plays = utils.read_plays_from_sheet(sheet_id, tab="scrim_Log")
    if not plays:
        return 0, ["No plays found in the scrim_Log tab of the scrimmage sheet."]

    all_players = db.get_all_players()
    player_id_to_name = {p["player_id"]: p["name"] for p in all_players if p.get("player_id") and p.get("name")}
    # MLN players have no player_id - resolve via s_id = "{season}_{raw_id}"
    mln_players = db.get_mln_players_for_lookup()
    for _mp in mln_players:
        _sid = _mp.get("s_id", "")
        _nm  = _mp.get("name", "")
        if _sid and _nm and "_" in _sid:
            try:
                _raw_id = int(_sid.split("_", 1)[1])
                player_id_to_name.setdefault(_raw_id, _nm)
            except (ValueError, IndexError):
                pass

    game_db_id = game["id"]
    season = game.get("season") or _CURRENT_MLN_SEASON
    game_code = game.get("game_code") or ""

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc = play.get("game_code") or game_code

        pitcher_name = player_id_to_name.get(play.get("pitcher_id"), "")
        batter_name  = player_id_to_name.get(play.get("batter_id"),  "")
        catcher_name = player_id_to_name.get(play.get("catcher_id"))
        runner_name  = player_id_to_name.get(play.get("runner_id"))

        away_full = utils.TEAM_ABBREV.get(play.get("away") or "", play.get("away") or "")
        home_full = utils.TEAM_ABBREV.get(play.get("home") or "", play.get("home") or "")
        if play["half"] == "top":
            off_team, def_team = away_full, home_full
        else:
            off_team, def_team = home_full, away_full

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        if inn_key not in outs_tracker:
            outs_tracker[inn_key] = 0
        outs = outs_tracker[inn_key]
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn = fp_inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher_name not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k != "game_code"},
            "game_code":    gc,
            "league":       "MLN",
            "game_type":    "scrimmage",
            "game_id":      game_db_id,
            "season":       season,
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
        n = db.bulk_upsert_scrimmage_plays(rows)
        return n, errors
    except Exception as e:
        return 0, errors + [str(e)]


def _sync_teams(sheet_id: str) -> tuple[int, list[str]]:
    teams = utils.read_teams_from_sheet(sheet_id)
    if not teams:
        return 0, ["No teams found in the Teams tab."]
    for t in teams:
        t["season"] = _CURRENT_RLN_SEASON
    try:
        n = db.bulk_upsert_teams(teams)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_players(sheet_id: str) -> tuple[int, list[str]]:
    players = utils.read_players_from_sheet(sheet_id)
    if not players:
        return 0, ["No players found in the Players tab."]
    for p in players:
        p["season"] = _CURRENT_RLN_SEASON
    try:
        n = db.bulk_upsert_players(players)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_mln_teams(sheet_id: str, season_override: int | None = None,
                    force_season: bool = False) -> tuple[int, list[str]]:
    teams = utils.read_mln_teams_from_sheet(sheet_id)
    if not teams:
        return 0, ["No teams found in the MLN Teams tab."]
    _season = season_override or _CURRENT_MLN_SEASON
    for t in teams:
        # force_season (current Export Tables sync) overrides any season carried
        # in from the sheet; archive syncs keep the sheet's per-season value.
        if force_season:
            t["season"] = _season
        else:
            t.setdefault("season", _season)
    deduped = list({t["s_team"]: t for t in teams}.values())
    try:
        n = db.bulk_upsert_mln_teams(deduped)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_mln_players(sheet_id: str, tab: str = "Rosters", season: int | None = None) -> tuple[int, list[str]]:
    players = utils.read_mln_players_from_sheet(sheet_id, tab=tab, season=season)
    if not players:
        return 0, [f"No players found in the MLN {tab} tab."]
    # Deduplicate on s_id - source sheet may list the same player twice in one season.
    # Keep the last occurrence so the most recent data wins within a batch.
    deduped = list({p["s_id"]: p for p in players}.values())
    try:
        n = db.bulk_upsert_mln_players(deduped)
        return n, []
    except Exception as e:
        return 0, [str(e)]


_GAMES_TABLE_COLS = {
    "game_code", "game_id_short", "league", "game_type", "season", "session_number",
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

def _sync_mln_games(sheet_id: str) -> tuple[int, list[str]]:
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
        g["game_type"] = "live"
    games = [{k: v for k, v in g.items() if k in _GAMES_TABLE_COLS} for g in games]
    try:
        n = db.bulk_upsert_games(games)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_mln_plays(sheet_id: str, tab: str = "Plays", season_override: int | None = None) -> tuple[int, list[str]]:
    plays = utils.read_mln_plays_from_sheet(sheet_id, tab=tab)
    if not plays:
        return 0, [f"No plays found in the MLN {tab} tab."]

    all_games = db.get_games()
    game_code_to_id = {str(g["game_code"]).strip(): g["id"] for g in all_games if g.get("game_code") and g.get("id")}
    mln_game_codes_in_db = sorted(
        str(g["game_code"]).strip() for g in all_games if g.get("league") == "MLN" and g.get("game_code")
    )
    play_game_codes = sorted({p["game_code"] for p in plays})
    if not mln_game_codes_in_db:
        return 0, [
            f"No MLN games found in database - sync MLN Games first. "
            f"Plays expect game codes like: {play_game_codes[:5]}"
        ]
    missing = sorted(set(play_game_codes) - set(mln_game_codes_in_db))
    if missing:
        diag = [
            f"DB has {len(mln_game_codes_in_db)} MLN game(s) e.g. {mln_game_codes_in_db[:5]}",
            f"Plays expect {len(play_game_codes)} game(s) e.g. {play_game_codes[:5]}",
            f"{len(missing)} game code(s) missing from DB e.g. {missing[:5]}",
        ]
    else:
        diag = []

    mln_teams = db.get_mln_teams_for_lookup()
    team_id_to_full = {t["team_id"]: t["full_team"] for t in mln_teams if t.get("team_id") and t.get("full_team")}

    mln_players = db.get_mln_players_for_lookup()
    # s_id format: "{season}_{player_id}" e.g. "1_1111"
    sid_to_name = {p["s_id"]: p["name"] for p in mln_players if p.get("s_id") and p.get("name")}

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc = play["game_code"]
        season = season_override or play.get("season") or (int(gc[:2]) if gc and len(gc) >= 2 and gc[:2].isdigit() else None)
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not found - sync MLN Games first.")
            continue

        def _sid(pid):
            return f"{season}_{pid}" if season and pid else None

        pitcher_sid = _sid(play.get("pitcher_id"))
        batter_sid  = _sid(play.get("batter_id"))
        catcher_sid = _sid(play.get("catcher_id"))
        runner_sid  = _sid(play.get("runner_id"))

        pitcher_name = sid_to_name.get(pitcher_sid, "") if pitcher_sid else ""
        batter_name  = sid_to_name.get(batter_sid,  "") if batter_sid  else ""
        catcher_name = sid_to_name.get(catcher_sid)     if catcher_sid else None
        runner_name  = sid_to_name.get(runner_sid)      if runner_sid  else None

        away_full = team_id_to_full.get(play.get("away") or "", play.get("away") or "")
        home_full = team_id_to_full.get(play.get("home") or "", play.get("home") or "")
        if play["half"] == "top":
            off_team, def_team = away_full, home_full
        else:
            off_team, def_team = home_full, away_full

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        if inn_key not in outs_tracker:
            outs_tracker[inn_key] = 0
        outs = outs_tracker[inn_key]
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn = fp_inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher_name not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k not in ("game_code", "away", "home")},
            "game_code":    gc,
            "season":       season,
            "game_type":    "live",
            "game_id":      game_db_id,
            "away":         play.get("away"),
            "home":         play.get("home"),
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
        n = db.bulk_upsert_mln_plays(rows)
        return n, diag + errors
    except Exception as e:
        return 0, diag + errors + [str(e)]


def _show_errors(errs: list[str]) -> None:
    if errs:
        with st.expander(f"{len(errs)} issue(s)"):
            for m in errs[:30]:
                st.caption(m)


def _sync_done(msg: str, errs: list[str]) -> None:
    st.session_state["_sync_msg"] = msg
    if errs:
        st.session_state["_sync_errors"] = errs
    st.cache_data.clear()
    st.rerun()


def _preview_mln_sheet(sheet_id: str, plays_tab: str = "Plays") -> None:
    """Read Games and Plays tabs and show column names, row counts, and sample data."""
    import urllib.parse

    _GAMES_EXPECTED = {
        "Game#", "GameID", "Away", "Home", "a_Scr", "h_Scr",
        "Winning Pitcher", "Losing Pitcher", "Save", "Hold", "Player of the Game",
        "Honorable Mention", "Umpire Assignment",
        "Start", "End", "Last Play", "Inning", "Last Result",
        "Win", "Loss", "Division",
    }
    _PLAYS_EXPECTED = {
        "Play", "Game", "Season", "Result", "Inning",
        "Pitcher", "Batter", "Catcher", "Runner",
        "Away", "Home", "Pitch", "Swing",
        "OnFirst", "OnSecond", "OnThird",
        "a_Scr", "h_Scr", "PlayType", "Steal",
    }

    def _safe_int_local(v):
        try:
            return int(float(str(v).strip()))
        except (ValueError, TypeError):
            return None

    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet="

    for tab_name, expected in [("Games", _GAMES_EXPECTED), (plays_tab, _PLAYS_EXPECTED)]:
        st.markdown(f"#### {tab_name} tab")
        try:
            url = base + urllib.parse.quote(tab_name)
            df = pd.read_csv(url, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            found = set(df.columns)
            missing = expected - found

            st.caption(f"{len(df)} rows · {len(df.columns)} columns")
            st.caption(f"Columns found: `{'`, `'.join(sorted(found))}`")

            if missing:
                st.warning(f"Expected columns not found: `{'`, `'.join(sorted(missing))}`")
            else:
                st.success("All expected columns present.")

            # Row validity check
            if tab_name == "Games":
                is_cur = "Winning Pitcher" in found
                st.info(f"Format detected: **{'MLN current' if is_cur else 'MLN Archive'}** "
                        f"({'Winning Pitcher' if is_cur else 'WP'} branch)")
                req = [c for c in ["Game#"] if c in found]
                if req:
                    valid = df["Game#"].apply(lambda x: _safe_int_local(x) is not None).sum()
                    st.caption(f"Rows with valid Game#: **{valid}** / {len(df)}")
            else:
                req = [c for c in ["Play", "Game", "Result"] if c in found]
                if req:
                    mask = df[req].apply(lambda col: col.notna() & (col.str.strip() != "") & (col.str.strip().str.lower() != "nan"))
                    valid = mask.all(axis=1).sum()
                    skipped = len(df) - valid
                    st.caption(f"Rows with Play + Game + Result: **{valid}** parseable, **{skipped}** would be skipped")
                    if skipped and skipped < 20:
                        bad = df[~mask.all(axis=1)][req + [c for c in ["Inning", "Pitcher", "Batter"] if c in found]]
                        st.caption("Skipped rows:")
                        st.dataframe(bad.head(10), use_container_width=True, hide_index=True)

            with st.expander(f"First 5 rows of {tab_name}"):
                st.dataframe(df.head(5), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Could not read '{tab_name}' tab: {e}")


# ------------------------------------------------------------------ sync results (survive rerun)

if "_sync_msg" in st.session_state:
    st.success(st.session_state.pop("_sync_msg"))
if "_sync_errors" in st.session_state:
    _show_errors(st.session_state.pop("_sync_errors"))

# ------------------------------------------------------------------ MLN current sync

st.subheader("MLN")

_mn1, _mn2, _mn3, _mn4, _mn5 = st.columns(5)

with _mn1:
    if st.button("MLN Teams", key="mln_teams", use_container_width=True):
        with st.spinner("Reading MLN Teams tab…"):
            n, errs = _sync_mln_teams(_MLN_SHEET_ID, force_season=True)
        _sync_done(f"{n} MLN team(s) synced.", errs)

with _mn2:
    if st.button("MLN Players", key="mln_players", use_container_width=True):
        with st.spinner("Reading MLN Players tab…"):
            n, errs = _sync_mln_players(_MLN_SHEET_ID, tab="Players", season=_CURRENT_MLN_SEASON)
        _sync_done(f"{n} MLN player(s) synced.", errs)

with _mn3:
    if st.button("MLN Games", key="mln_games", use_container_width=True):
        with st.spinner("Reading MLN Games tab…"):
            n, errs = _sync_mln_games(_MLN_SHEET_ID)
        _sync_done(f"{n} MLN game(s) synced.", errs)

with _mn4:
    if st.button("MLN Plays", key="mln_plays", use_container_width=True):
        with st.spinner("Reading MLN Plays (Raw) tab…"):
            n, errs = _sync_mln_plays(_MLN_SHEET_ID, tab="Plays (Raw)", season_override=_CURRENT_MLN_SEASON)
        _sync_done(f"{n} MLN play(s) synced.", errs)

with _mn5:
    if st.button("Sync MLN All", key="mln_sync_all", type="primary", use_container_width=True):
        with st.spinner("Syncing all MLN tables (Teams → Players → Games → Plays)…"):
            mt_n,  mt_e  = _sync_mln_teams(_MLN_SHEET_ID, force_season=True)
            mp_n,  mp_e  = _sync_mln_players(_MLN_SHEET_ID, tab="Players", season=_CURRENT_MLN_SEASON)
            mg_n,  mg_e  = _sync_mln_games(_MLN_SHEET_ID)
            mpl_n, mpl_e = _sync_mln_plays(_MLN_SHEET_ID, tab="Plays (Raw)", season_override=_CURRENT_MLN_SEASON)
        _sync_done(
            f"MLN - Teams: {mt_n} · Players: {mp_n} · Games: {mg_n} · Plays: {mpl_n}",
            mt_e + mp_e + mg_e + mpl_e,
        )

if st.button("Preview MLN Sheet", key="mln_preview"):
    with st.spinner("Reading MLN sheet…"):
        _preview_mln_sheet(_MLN_SHEET_ID, plays_tab="Plays (Raw)")

st.divider()

# ------------------------------------------------------------------ RLN sync

st.subheader("RLN")

col_st, col_spl, col_sg, col_sp, col_sa = st.columns(5)

with col_st:
    if st.button("RLN Teams", use_container_width=True):
        with st.spinner("Reading Teams tab…"):
            n, errs = _sync_teams(_RLN_SHEET_ID)
        _sync_done(f"{n} team(s) synced.", errs)

with col_spl:
    if st.button("RLN Players", use_container_width=True):
        with st.spinner("Reading Players tab…"):
            n, errs = _sync_players(_RLN_SHEET_ID)
        _sync_done(f"{n} player(s) synced.", errs)

with col_sg:
    if st.button("RLN Games", use_container_width=True):
        with st.spinner("Reading Games tab…"):
            n, errs = _sync_games(_RLN_SHEET_ID)
        _sync_done(f"{n} game(s) synced.", errs)

with col_sp:
    if st.button("RLN Plays", use_container_width=True):
        with st.spinner("Reading Plays tab…"):
            n, errs = _sync_plays(_RLN_SHEET_ID)
        _sync_done(f"{n} play(s) synced.", errs)

with col_sa:
    if st.button("Sync All", type="primary", use_container_width=True):
        with st.spinner("Syncing all tables…"):
            t_n, t_e = _sync_teams(_RLN_SHEET_ID)
            pl_n, pl_e = _sync_players(_RLN_SHEET_ID)
            g_n, g_e = _sync_games(_RLN_SHEET_ID)
            p_n, p_e = _sync_plays(_RLN_SHEET_ID)
        _sync_done(
            f"Teams: {t_n} · Players: {pl_n} · Games: {g_n} · Plays: {p_n}",
            t_e + pl_e + g_e + p_e,
        )

st.divider()

# ------------------------------------------------------------------ scrimmage sync

st.subheader("Scrimmage")
_scrim_games = db.get_scrimmage_games()
if not _scrim_games:
    st.caption(
        "No scrimmage games found. "
        "Add a row to the games table in Supabase with game_type='scrimmage' and a sheet_url first."
    )
else:
    _scrim_options = {
        f"{g.get('game_code') or 'SCRIM'} - {g.get('away_team', '')} vs {g.get('home_team', '')}": g
        for g in _scrim_games
    }
    _scrim_sel = st.selectbox("Scrimmage game", list(_scrim_options.keys()), key="scrim_game_sel")
    _scrim_game = _scrim_options[_scrim_sel]
    st.caption(f"Sheet URL: {_scrim_game.get('sheet_url') or 'No sheet URL linked'}")
    if st.button("Sync Scrimmage", type="secondary", use_container_width=True, key="sync_scrim_btn"):
        if not _scrim_game.get("sheet_url"):
            st.warning("This scrimmage game has no sheet_url set in Supabase.")
        else:
            with st.spinner("Reading scrimmage sheet..."):
                _sn, _se = _sync_scrimmage_plays(_scrim_game)
            _sync_done(f"{_sn} scrimmage play(s) synced.", _se)

st.divider()

# ------------------------------------------------------------------ MLN archive sync

st.subheader("MLN Archive")
_mln_sid_key = "mln_archive_sheet_id"
if _mln_sid_key not in st.session_state:
    st.session_state[_mln_sid_key] = ""
_mln_col1, _mln_col2 = st.columns([4, 1])
with _mln_col1:
    st.text_input("MLN archive sheet ID", key=_mln_sid_key,
                  placeholder="Google Sheet ID (the long string in the URL)")

_mln_sid = st.session_state.get(_mln_sid_key, "").strip()
_mln_disabled = not bool(_mln_sid)

_mmt, _mmp, _mmg, _mmpl, _mma = st.columns(5)

with _mmt:
    if st.button("MLN Teams", use_container_width=True, disabled=_mln_disabled):
        with st.spinner("Reading MLN Teams tab…"):
            n, errs = _sync_mln_teams(_mln_sid)
        _sync_done(f"{n} MLN team(s) synced.", errs)

with _mmp:
    if st.button("MLN Players", use_container_width=True, disabled=_mln_disabled):
        with st.spinner("Reading MLN Rosters tab…"):
            n, errs = _sync_mln_players(_mln_sid)
        _sync_done(f"{n} MLN player(s) synced.", errs)

with _mmg:
    if st.button("MLN Games", use_container_width=True, disabled=_mln_disabled):
        with st.spinner("Reading MLN Games tab…"):
            n, errs = _sync_mln_games(_mln_sid)
        _sync_done(f"{n} MLN game(s) synced.", errs)

with _mmpl:
    if st.button("MLN Plays", use_container_width=True, disabled=_mln_disabled):
        with st.spinner("Reading MLN Plays tab…"):
            n, errs = _sync_mln_plays(_mln_sid)
        _sync_done(f"{n} MLN play(s) synced.", errs)

with _mma:
    if st.button("Sync MLN All", type="primary", use_container_width=True, disabled=_mln_disabled):
        with st.spinner("Syncing all MLN tables (Teams → Players → Games → Plays)…"):
            mt_n,  mt_e  = _sync_mln_teams(_mln_sid)
            mp_n,  mp_e  = _sync_mln_players(_mln_sid)
            mg_n,  mg_e  = _sync_mln_games(_mln_sid)
            mpl_n, mpl_e = _sync_mln_plays(_mln_sid)
        _sync_done(
            f"MLN - Teams: {mt_n} · Players: {mp_n} · Games: {mg_n} · Plays: {mpl_n}",
            mt_e + mp_e + mg_e + mpl_e,
        )

if st.button("Preview Archive Sheet", key="mln_archive_preview", disabled=_mln_disabled):
    with st.spinner("Reading archive sheet…"):
        _preview_mln_sheet(_mln_sid, plays_tab="Plays")

st.caption("Sync order: Teams → Players → Games → Plays (each step depends on the previous).")

st.divider()

# ── pitcher stats ─────────────────────────────────────────────────────────────
st.subheader("Pitcher Stats")
st.caption("Pre-compute behavioral stats (Avg |Δ|, Avg Δ², Shadow %, Meme Rate, Wraparound %) across all pitchers.")
if st.button("Refresh Pitcher Stats", key="refresh_pitcher_stats"):
    import utils, pandas as pd
    _bar  = st.progress(0, text="Fetching RLN plays…")
    _all_rln = db.get_all_plays(league="RLN")
    _bar.progress(20, text="Fetching MLN plays…")
    _all_mln = db.get_all_plays(league="MLN")
    _bar.progress(40, text="Enriching play data…")
    _dfs = []
    if _all_rln:
        _dfs.append(utils.enrich_df(utils.flatten_games(_all_rln)))
    if _all_mln:
        _dfs.append(utils.enrich_df(utils.flatten_games(_all_mln)))
    if _dfs:
        _bar.progress(70, text="Computing pitcher stats…")
        _combined  = pd.concat(_dfs, ignore_index=True)
        _stat_rows = utils.compute_pitcher_stats(_combined)
        _bar.progress(90, text="Saving to database…")
        _n = db.upsert_pitcher_stats(_stat_rows)
        db.get_pitcher_stats.clear()
        _bar.progress(100, text="Done.")
        st.success(f"Updated stats for {_n} pitcher(s).")
    else:
        _bar.empty()
        st.warning("No play data found.")

st.divider()

# ------------------------------------------------------------------ game list

st.subheader("Games")
if "show_games" not in st.session_state:
    st.session_state["show_games"] = False
if st.button("Show / Hide Game List", key="toggle_games"):
    st.session_state["show_games"] = not st.session_state["show_games"]

if not st.session_state["show_games"]:
    st.stop()

_all_games = db.get_games()
games = [
    g for g in _all_games
    if not g.get("winning_pitcher")
    and (
        (g.get("league") == "RLN" and g.get("season") == _CURRENT_RLN_SEASON)
        or (g.get("league") == "MLN" and g.get("season") == _CURRENT_MLN_SEASON)
    )
]
if not games:
    st.info("No active games this season. Click Sync All above.")
    st.stop()

# Group: league → season → [games], each bucket sorted by id desc
from collections import defaultdict as _dd
_by_league: dict[str, dict[int, list]] = _dd(lambda: _dd(list))
for _g in sorted(games, key=lambda x: x["id"], reverse=True):
    _by_league[_g.get("league") or "RLN"][_g.get("season") or 0].append(_g)

for _league in sorted(_by_league.keys()):
    _seasons = _by_league[_league]
    _league_total = sum(len(v) for v in _seasons.values())
    with st.expander(f"**{_league}** - {_league_total} game(s)", expanded=(_league == "RLN")):
        for _season in sorted(_seasons.keys(), reverse=True):
            _season_games = _seasons[_season]
            with st.expander(f"Season {_season} - {len(_season_games)} game(s)"):
                for g in _season_games:
                    gc = g.get("game_code", "")
                    gc_label = gc or f"S{g['season']} G{g['session_number']}"
                    label = f"**{gc_label}** - {g['away_team']} @ {g['home_team']}"
                    if g.get("away_score") is not None and g.get("home_score") is not None:
                        label += f" ({g['away_score']}–{g['home_score']})"
                    if g.get("start_time"):
                        label += f" · {str(g['start_time'])[:10]}"

                    with st.expander(label):
                        # ── Session sheet URL ─────────────────────────────
                        _url_key = f"sheet_url_{g['id']}"
                        if _url_key not in st.session_state:
                            st.session_state[_url_key] = g.get("sheet_url") or ""
                        _cu, _cb = st.columns([5, 1])
                        with _cu:
                            st.text_input("Session sheet URL", key=_url_key,
                                          placeholder="https://docs.google.com/spreadsheets/d/…")
                        with _cb:
                            st.write("")
                            if st.button("Save", key=f"save_url_{g['id']}", use_container_width=True):
                                db.update_game_sheet_url(g["id"], st.session_state[_url_key].strip() or None)
                                st.toast("Sheet URL saved.")

                        st.divider()

                        # ── Plays ─────────────────────────────────────────
                        plays = db.get_plays_for_game(g["id"])
                        st.caption(f"{len(plays)} play(s) logged")
                        if plays:
                            df = pd.DataFrame(plays)
                            df["half"] = df["half"].fillna("top") if "half" in df.columns else "top"
                            df["diff"] = df.apply(
                                lambda r: utils.circular_diff(int(r["pitch"]), int(r["swing"]))
                                if pd.notna(r.get("pitch")) and pd.notna(r.get("swing")) else None,
                                axis=1,
                            )
                            df["Inn"] = df.apply(
                                lambda r: utils.inning_label(r["inning"], r["half"]), axis=1)
                            df["obc"] = df["obc"].map(utils.obc_display)
                            st.dataframe(
                                df[["Inn", "outs", "obc", "pitcher_name", "batter_name",
                                    "pitch", "swing", "diff", "result"]].rename(columns={
                                    "outs": "Outs", "obc": "Runners",
                                    "pitcher_name": "Pitcher", "batter_name": "Batter",
                                    "pitch": "Pitch", "swing": "Swing",
                                    "diff": "Diff", "result": "Result",
                                }),
                                use_container_width=True,
                                hide_index=True,
                            )
                        if st.button(f"Delete {gc_label}", key=f"del_{g['id']}"):
                            db.delete_game(g["id"])
                            st.rerun()

"""Sync data from Google Sheets into Supabase."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

_RLN_SHEET_ID = "1lcgT6np-4O5x83b2JZXjv8REfNDYXE7GMYMZeu5znRY"

st.title("Sync Data")

# ------------------------------------------------------------------ sync helpers

def _sync_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the Games tab."]
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


def _sync_scrimmage_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_plays_from_sheet(sheet_id)
    if not plays:
        return 0, ["No plays found in the Plays (Raw) tab of the scrimmage sheet."]

    all_players = db.get_all_players()
    player_id_to_name = {p["player_id"]: p["name"] for p in all_players if p.get("player_id") and p.get("name")}

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc = play["game_code"]

        # Try to parse season from scrimmage code (first 2 digits)
        try:
            season = int(gc[:2])
        except (ValueError, TypeError):
            season = None

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
            "scrimmage_code": gc,
            "season":         season,
            "pitcher_name":   pitcher_name,
            "batter_name":    batter_name,
            "catcher_name":   catcher_name,
            "runner_name":    runner_name,
            "off_team":       off_team,
            "def_team":       def_team,
            "diff":           diff,
            "outs":           outs,
            "is_fp_inn":      is_fp_inn,
            "is_fp_app":      is_fp_app,
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
    try:
        n = db.bulk_upsert_teams(teams)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _sync_players(sheet_id: str) -> tuple[int, list[str]]:
    players = utils.read_players_from_sheet(sheet_id)
    if not players:
        return 0, ["No players found in the Players tab."]
    try:
        n = db.bulk_upsert_players(players)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _show_errors(errs: list[str]) -> None:
    if errs:
        with st.expander(f"{len(errs)} issue(s)"):
            for m in errs[:30]:
                st.caption(m)


# ------------------------------------------------------------------ sync results (survive rerun)

if "_sync_msg" in st.session_state:
    st.success(st.session_state.pop("_sync_msg"))
if "_sync_errors" in st.session_state:
    _show_errors(st.session_state.pop("_sync_errors"))

# ------------------------------------------------------------------ sync buttons

col_st, col_spl, col_sg, col_sp, col_sa = st.columns(5)

with col_st:
    if st.button("Sync Teams", use_container_width=True):
        with st.spinner("Reading Teams tab…"):
            n, errs = _sync_teams(_RLN_SHEET_ID)
        st.session_state["_sync_msg"] = f"{n} team(s) synced."
        if errs:
            st.session_state["_sync_errors"] = errs
        st.rerun()

with col_spl:
    if st.button("Sync Players", use_container_width=True):
        with st.spinner("Reading Players tab…"):
            n, errs = _sync_players(_RLN_SHEET_ID)
        st.session_state["_sync_msg"] = f"{n} player(s) synced."
        if errs:
            st.session_state["_sync_errors"] = errs
        st.rerun()

with col_sg:
    if st.button("Sync Games", use_container_width=True):
        with st.spinner("Reading Games tab…"):
            n, errs = _sync_games(_RLN_SHEET_ID)
        st.session_state["_sync_msg"] = f"{n} game(s) synced."
        if errs:
            st.session_state["_sync_errors"] = errs
        st.rerun()

with col_sp:
    if st.button("Sync Plays", use_container_width=True):
        with st.spinner("Reading Plays tab…"):
            n, errs = _sync_plays(_RLN_SHEET_ID)
        st.session_state["_sync_msg"] = f"{n} play(s) synced."
        if errs:
            st.session_state["_sync_errors"] = errs
        st.rerun()

with col_sa:
    if st.button("Sync All", type="primary", use_container_width=True):
        with st.spinner("Syncing all tables…"):
            t_n, t_e = _sync_teams(_RLN_SHEET_ID)
            pl_n, pl_e = _sync_players(_RLN_SHEET_ID)
            g_n, g_e = _sync_games(_RLN_SHEET_ID)
            p_n, p_e = _sync_plays(_RLN_SHEET_ID)
        st.session_state["_sync_msg"] = f"Teams: {t_n} · Players: {pl_n} · Games: {g_n} · Plays: {p_n}"
        all_errs = t_e + pl_e + g_e + p_e
        if all_errs:
            st.session_state["_sync_errors"] = all_errs
        st.rerun()

st.divider()

# ------------------------------------------------------------------ scrimmage sync

st.subheader("Scrimmage Plays")
_ss_key = "scrim_sheet_id"
if _ss_key not in st.session_state:
    st.session_state[_ss_key] = ""
_css1, _css2 = st.columns([4, 1])
with _css1:
    st.text_input("Scrimmage sheet ID", key=_ss_key,
                  placeholder="Google Sheet ID (the long string in the URL)")
with _css2:
    st.write("")
    if st.button("Sync Scrimmage", type="secondary", use_container_width=True):
        _sid = st.session_state[_ss_key].strip()
        if not _sid:
            st.warning("Enter a sheet ID first.")
        else:
            with st.spinner("Reading scrimmage sheet…"):
                _sn, _se = _sync_scrimmage_plays(_sid)
            st.session_state["_sync_msg"] = f"{_sn} scrimmage play(s) synced."
            if _se:
                st.session_state["_sync_errors"] = _se
            st.rerun()

st.divider()

# ------------------------------------------------------------------ game list

games = db.get_games()
if not games:
    st.info("No games yet. Click Sync All above.")
    st.stop()

for g in games:
    gc = g.get("game_code", "")
    gc_label = gc or f"S{g['season']} G{g['session_number']}"
    label = f"**{gc_label}** - {g['away_team']} @ {g['home_team']}"
    if g.get("away_score") is not None and g.get("home_score") is not None:
        label += f" ({g['away_score']}–{g['home_score']})"
    if g.get("start_time"):
        label += f" · {str(g['start_time'])[:10]}"

    with st.expander(label):
        # ── Session sheet URL ─────────────────────────────────────────────
        _url_key = f"sheet_url_{g['id']}"
        if _url_key not in st.session_state:
            st.session_state[_url_key] = g.get("sheet_url") or ""
        _cu, _cb = st.columns([5, 1])
        with _cu:
            st.text_input("Session sheet URL", key=_url_key, placeholder="https://docs.google.com/spreadsheets/d/…")
        with _cb:
            st.write("")
            if st.button("Save", key=f"save_url_{g['id']}", use_container_width=True):
                db.update_game_sheet_url(g["id"], st.session_state[_url_key].strip() or None)
                st.toast("Sheet URL saved.")

        st.divider()

        # ── Plays ─────────────────────────────────────────────────────────
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
            df["Inn"] = df.apply(lambda r: utils.inning_label(r["inning"], r["half"]), axis=1)
            st.dataframe(
                df[["Inn", "outs", "obc", "pitcher_name", "batter_name", "pitch", "swing", "diff", "result"]].rename(columns={
                    "outs": "Outs", "obc": "Runners",
                    "pitcher_name": "Pitcher", "batter_name": "Batter",
                    "pitch": "Pitch", "swing": "Swing", "diff": "Diff", "result": "Result",
                }),
                use_container_width=True,
                hide_index=True,
            )
        if st.button(f"Delete {gc_label}", key=f"del_{g['id']}"):
            db.delete_game(g["id"])
            st.rerun()

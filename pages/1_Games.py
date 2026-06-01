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
        return 0, ["No plays found in the Plays (Converted) tab."]

    all_games = db.get_games()
    game_code_to_id = {g["game_code"]: g["id"] for g in all_games if g.get("game_code")}

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn: dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    errors: list[str] = []
    rows: list[dict] = []

    for play in plays_sorted:
        gc = play["game_code"]
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not found - sync Games first.")
            continue
        inn_key = (play["inning"], play["half"])
        pitcher = play["pitcher_name"]
        is_fp_inn = inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(inn_key)
        seen_pitcher[gc].add(pitcher)
        rows.append({
            **{k: v for k, v in play.items() if k != "game_code"},
            "game_id": game_db_id,
            "is_fp_inn": is_fp_inn,
            "is_fp_app": is_fp_app,
        })

    if not rows:
        return 0, errors

    try:
        n = db.bulk_upsert_plays(rows)
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
        plays = db.get_plays_for_game(g["id"])
        st.caption(f"{len(plays)} play(s) logged")
        if plays:
            df = pd.DataFrame(plays)
            df["half"] = df["half"].fillna("top") if "half" in df.columns else "top"
            df["diff"] = df.apply(lambda r: utils.circular_diff(int(r["pitch"]), int(r["swing"])), axis=1)
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

"""Game management — sync from Google Sheets and view game list."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

_RLN_SHEET_ID = "1lcgT6np-4O5x83b2JZXjv8REfNDYXE7GMYMZeu5znRY"

st.title("Games")

# ------------------------------------------------------------------ sync helpers

def _sync_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the Games tab."]
    synced, errors = 0, []
    for g in games:
        try:
            db.upsert_game(
                game_code=g["game_code"],
                season=g["season"],
                session_number=g["session_number"],
                home_team=g["home_team"],
                away_team=g["away_team"],
                game_num=g.get("game_num"),
                away_score=g.get("away_score"),
                home_score=g.get("home_score"),
            )
            synced += 1
        except Exception as e:
            errors.append(f"{g['game_code']}: {e}")
    return synced, errors


def _sync_plays(sheet_id: str) -> tuple[int, int, list[str]]:
    plays = utils.read_plays_from_sheet(sheet_id)
    if not plays:
        return 0, 0, ["No plays found in the Plays (Converted) tab."]
    all_games = db.get_games()
    game_code_to_id = {g["game_code"]: g["id"] for g in all_games if g.get("game_code")}
    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn: dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    synced = skipped = 0
    errors: list[str] = []
    for play in plays_sorted:
        gc = play["game_code"]
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not found — sync Games first.")
            skipped += 1
            continue
        inn_key = (play["inning"], play["half"])
        pitcher = play["pitcher_name"]
        is_fp_inn = inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(inn_key)
        seen_pitcher[gc].add(pitcher)
        try:
            db.upsert_at_bat(
                game_id=game_db_id,
                play_num=play["play_num"],
                inning=play["inning"],
                half=play["half"],
                outs=play["outs"],
                obc=play["obc"],
                pitcher_team=play["pitcher_team"],
                batter_team=play["batter_team"],
                pitcher_name=play["pitcher_name"],
                batter_name=play["batter_name"],
                pitch=play["pitch"],
                swing=play["swing"],
                result=play["result"],
                is_fp_app=is_fp_app,
                is_fp_inn=is_fp_inn,
            )
            synced += 1
        except Exception as e:
            errors.append(f"Play {play['play_num']}: {e}")
            skipped += 1
    return synced, skipped, errors

# ------------------------------------------------------------------ sync buttons

col_sg, col_sp, col_sa = st.columns(3)

with col_sg:
    if st.button("Sync Games", use_container_width=True):
        with st.spinner("Reading Games tab…"):
            try:
                n, errs = _sync_games(_RLN_SHEET_ID)
                st.success(f"Synced {n} game(s).")
                if errs:
                    st.warning("\n".join(errs))
                st.rerun()
            except Exception as e:
                st.error(str(e))

with col_sp:
    if st.button("Sync Plays", use_container_width=True):
        with st.spinner("Reading Plays (Converted) tab…"):
            try:
                n, skipped, errs = _sync_plays(_RLN_SHEET_ID)
                st.success(f"Synced {n} play(s). {skipped} skipped.")
                if errs:
                    with st.expander("Details"):
                        for m in errs[:20]:
                            st.caption(m)
                st.rerun()
            except Exception as e:
                st.error(str(e))

with col_sa:
    if st.button("Sync All", type="primary", use_container_width=True):
        with st.spinner("Syncing games and plays…"):
            try:
                g_n, g_errs = _sync_games(_RLN_SHEET_ID)
                p_n, p_skip, p_errs = _sync_plays(_RLN_SHEET_ID)
                st.success(f"Synced {g_n} game(s) and {p_n} play(s). {p_skip} skipped.")
                if g_errs or p_errs:
                    with st.expander("Details"):
                        for m in (g_errs + p_errs)[:20]:
                            st.caption(m)
                st.rerun()
            except Exception as e:
                st.error(str(e))

st.divider()

# ------------------------------------------------------------------ manual game creation

with st.expander("Create Game Manually", expanded=False):
    with st.form("new_game"):
        col1, col2, col3 = st.columns(3)
        with col1:
            season = st.number_input("Season", min_value=1, value=13, step=1)
        with col2:
            session_number = st.number_input("Session #", min_value=1, value=1, step=1)
        with col3:
            game_code = st.text_input("Game Code", placeholder="130101")
        col4, col5 = st.columns(2)
        with col4:
            home_team = st.selectbox("Home Team", utils.TEAMS)
        with col5:
            away_team = st.selectbox("Away Team", utils.TEAMS)
        start_date = st.date_input("Date (optional)", value=None)
        if st.form_submit_button("Create Game", type="primary"):
            try:
                db.create_game(
                    season=int(season),
                    session_number=int(session_number),
                    home_team=home_team,
                    away_team=away_team,
                    start_date=start_date,
                    game_code=game_code.strip() or None,
                )
                st.success(f"Created S{season} G{session_number}: {home_team} vs {away_team}")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

st.divider()

# ------------------------------------------------------------------ game list

games = db.get_games()
if not games:
    st.info("No games yet. Click Sync All above.")
    st.stop()

for g in games:
    gc = g.get("game_code", "")
    gc_label = gc or f"S{g['season']} G{g['session_number']}"
    label = f"**{gc_label}** — {g['away_team']} @ {g['home_team']}"
    if g.get("away_score") is not None and g.get("home_score") is not None:
        label += f" ({g['away_score']}–{g['home_score']})"
    if g.get("start_date"):
        label += f" · {g['start_date']}"

    with st.expander(label):
        at_bats = db.get_at_bats_for_game(g["id"])
        st.caption(f"{len(at_bats)} at-bat(s) logged")
        if at_bats:
            df = pd.DataFrame(at_bats)
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

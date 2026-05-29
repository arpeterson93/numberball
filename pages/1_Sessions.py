"""Session management — create and view game sessions."""
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Sessions")

# ------------------------------------------------------------------ create session

with st.expander("Create New Session", expanded=False):
    with st.form("new_session"):
        col1, col2 = st.columns(2)
        with col1:
            season = st.number_input("Season", min_value=1, value=13, step=1)
        with col2:
            session_number = st.number_input("Session #", min_value=1, value=1, step=1)
        col3, col4 = st.columns(2)
        with col3:
            home_team = st.selectbox("Home Team", utils.TEAMS)
        with col4:
            away_team = st.selectbox("Away Team", utils.TEAMS)
        game_date = st.date_input("Date (optional)", value=None)
        sheet_url = st.text_input("Google Sheet URL (optional)", placeholder="https://docs.google.com/spreadsheets/d/...")
        if st.form_submit_button("Create Session", type="primary"):
            try:
                db.create_session(
                    season=int(season),
                    session_number=int(session_number),
                    home_team=home_team,
                    away_team=away_team,
                    game_date=game_date,
                    sheet_url=sheet_url.strip() or None,
                )
                st.success(f"Created S{season} G{session_number}: {home_team} vs {away_team}")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

st.divider()

# ------------------------------------------------------------------ session list

sessions = db.get_sessions()
if not sessions:
    st.info("No sessions yet. Create one above.")
    st.stop()

for s in sessions:
    label = f"**S{s['season']} G{s['session_number']}** — {s['home_team']} vs {s['away_team']}"
    if s.get("game_date"):
        label += f" ({s['game_date']})"

    with st.expander(label):
        at_bats = db.get_at_bats_for_session(s["id"])
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
                width='stretch',
                hide_index=True,
            )
        if st.button(f"Delete session S{s['season']}G{s['session_number']}", key=f"del_{s['id']}"):
            db.delete_session(s["id"])
            st.rerun()

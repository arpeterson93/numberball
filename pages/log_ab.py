"""
Numberball Scouting App
Main page: Log At-Bat (mobile-first)
"""
import streamlit as st
import pandas as pd
import database as db
import utils


st.title("⚾ Numberball")
st.caption("Scouting & Stats")

# ------------------------------------------------------------------ session picker

sessions = db.get_sessions()
if not sessions:
    st.warning("No sessions yet. Go to **Sessions** to create one first.")
    st.stop()

session_options = {
    f"S{s['season']} G{s['session_number']} — {s['home_team']} vs {s['away_team']}": s["id"]
    for s in sessions
}
session_labels = list(session_options.keys())

if "active_session_label" not in st.session_state:
    st.session_state.active_session_label = session_labels[0]

selected_label = st.selectbox(
    "Active Session",
    session_labels,
    index=session_labels.index(st.session_state.active_session_label),
    key="session_select",
)
st.session_state.active_session_label = selected_label
active_session_id = session_options[selected_label]

st.divider()

# ------------------------------------------------------------------ pitch & swing (live diff)

st.subheader("Log At-Bat")

st.markdown("**Numbers**")
col_p, col_s = st.columns(2)
with col_p:
    pitch = st.number_input("Pitch (1-1000)", min_value=1, max_value=1000, value=500, step=1, key="pitch_input")
with col_s:
    swing = st.number_input("Swing (1-1000)", min_value=1, max_value=1000, value=500, step=1, key="swing_input")

diff = utils.circular_diff(int(pitch), int(swing))
st.info(f"Diff: **{diff}** | Pitch Zone: **{utils.get_zone(int(pitch))}**")

st.divider()

# ------------------------------------------------------------------ rest of form

with st.form("ab_form", clear_on_submit=True):
    col1, col2, col3 = st.columns(3)
    with col1:
        inning = st.number_input("Inning", min_value=1, max_value=15, value=1, step=1)
    with col2:
        outs = st.selectbox("Outs", [0, 1, 2])
    with col3:
        obc = st.selectbox("Runners", utils.OBC_OPTIONS)

    st.markdown("**Pitcher**")
    col_pt, col_pn = st.columns([1, 2])
    with col_pt:
        pitcher_team = st.selectbox("Team", utils.TEAMS, key="pt")
    with col_pn:
        known_pitchers = db.get_distinct_pitchers(pitcher_team)
        pitcher_name = st.selectbox(
            "Name",
            ["(new)"] + known_pitchers,
            key="pname_select",
        )
        if pitcher_name == "(new)":
            pitcher_name = st.text_input("Pitcher name", key="pname_new").strip()

    st.markdown("**Batter**")
    col_bt, col_bn = st.columns([1, 2])
    with col_bt:
        batter_team = st.selectbox("Team", utils.TEAMS, key="bt")
    with col_bn:
        known_batters = db.get_distinct_batters(batter_team)
        batter_name = st.selectbox(
            "Name",
            ["(new)"] + known_batters,
            key="bname_select",
        )
        if batter_name == "(new)":
            batter_name = st.text_input("Batter name", key="bname_new").strip()

    result = st.selectbox("Result", utils.RESULTS + ["(other)"])
    if result == "(other)":
        result = st.text_input("Custom result").strip().upper()

    col_fp1, col_fp2 = st.columns(2)
    with col_fp1:
        is_fp_app = st.checkbox("First pitch of appearance")
    with col_fp2:
        is_fp_inn = st.checkbox("First pitch of inning")

    submitted = st.form_submit_button("Submit At-Bat", use_container_width=True, type="primary")

if submitted:
    if not pitcher_name or not batter_name:
        st.error("Pitcher and batter names are required.")
    else:
        db.insert_at_bat(
            session_id=active_session_id,
            inning=int(inning),
            outs=int(outs),
            obc=obc,
            pitcher_team=pitcher_team,
            batter_team=batter_team,
            pitcher_name=pitcher_name,
            batter_name=batter_name,
            pitch=int(pitch),
            swing=int(swing),
            result=result,
            is_fp_app=is_fp_app,
            is_fp_inn=is_fp_inn,
        )
        st.success(f"Logged: {pitcher_name} vs {batter_name} → **{result}** (diff: {diff})")
        st.rerun()

# ------------------------------------------------------------------ recent entries

st.divider()
st.subheader("Recent At-Bats")

recent = db.get_at_bats_for_session(active_session_id)
if recent:
    df = pd.DataFrame(recent).sort_values("id", ascending=False).head(10)
    df["diff"] = df.apply(lambda r: utils.circular_diff(int(r["pitch"]), int(r["swing"])), axis=1)
    display_cols = ["inning", "outs", "obc", "pitcher_name", "batter_name", "pitch", "swing", "diff", "result"]
    st.dataframe(
        df[display_cols].rename(columns={
            "inning": "Inn", "outs": "Outs", "obc": "Runners",
            "pitcher_name": "Pitcher", "batter_name": "Batter",
            "pitch": "Pitch", "swing": "Swing", "diff": "Diff", "result": "Result",
        }),
        use_container_width=True,
        hide_index=True,
    )

    if st.button("Delete last entry", type="secondary"):
        last_id = int(df["id"].iloc[0])
        db.delete_at_bat(last_id)
        st.rerun()
else:
    st.caption("No at-bats logged for this session yet.")

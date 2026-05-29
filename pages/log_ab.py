"""
Numberball Scouting App
Main page: Log At-Bat (mobile-first)
"""
import streamlit as st
import pandas as pd
from streamlit_searchbox import st_searchbox
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

result = st_searchbox(
    lambda q: [r for r in utils.RESULTS if q.upper() in r],
    placeholder="Result (GO, 1B, HR...)",
    key="result_searchbox",
    default_use_searchterm=True,
)

st.divider()

# ------------------------------------------------------------------ rest of form

with st.form("ab_form", clear_on_submit=True):
    col1, col2, col3 = st.columns(3)
    with col1:
        half_choice = st.radio("Half", ["▲ Top", "▼ Bot"], horizontal=True)
        half = "top" if half_choice == "▲ Top" else "bottom"
    with col2:
        inning = st.number_input("Inning", min_value=1, max_value=15, value=1, step=1)
    with col3:
        outs = st.selectbox("Outs", [0, 1, 2])

    obc = st.selectbox("Runners", utils.OBC_OPTIONS)

    st.markdown("**Pitcher**")
    col_pt, col_pn = st.columns([1, 2])
    with col_pt:
        pitcher_team = st.selectbox("Team", utils.TEAMS, key="pt")
    with col_pn:
        pitchers = db.get_players(pitcher_team)
        pitcher_name = st.selectbox("Name", pitchers, key="pname_select") if pitchers else st.text_input("Pitcher name", key="pname_new").strip()

    st.markdown("**Batter**")
    col_bt, col_bn = st.columns([1, 2])
    with col_bt:
        batter_team = st.selectbox("Team", utils.TEAMS, key="bt")
    with col_bn:
        batters = db.get_players(batter_team)
        batter_name = st.selectbox("Name", batters, key="bname_select") if batters else st.text_input("Batter name", key="bname_new").strip()

    submitted = st.form_submit_button("Submit At-Bat", use_container_width=True, type="primary")

if submitted:
    if not pitcher_name or not batter_name:
        st.error("Pitcher and batter names are required.")
    elif not result:
        st.error("Result is required.")
    else:
        # Auto-compute FP flags from existing session data
        existing = db.get_at_bats_for_session(active_session_id)
        is_fp_inn = not any(
            e["inning"] == int(inning) and e.get("half", "top") == half
            for e in existing
        )
        is_fp_app = not any(
            e["pitcher_name"] == pitcher_name
            for e in existing
        )
        db.insert_at_bat(
            session_id=active_session_id,
            inning=int(inning),
            half=half,
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
        inn_label = utils.inning_label(int(inning), half)
        st.success(f"Logged: {inn_label} | {pitcher_name} vs {batter_name} → **{result}** (diff: {diff})")
        st.rerun()

# ------------------------------------------------------------------ recent entries

st.divider()
st.subheader("Recent At-Bats")

recent = db.get_at_bats_for_session(active_session_id)
if recent:
    df = pd.DataFrame(recent).sort_values("id", ascending=False).head(10)
    df["half"] = df["half"].fillna("top")
    df["diff"] = df.apply(lambda r: utils.circular_diff(int(r["pitch"]), int(r["swing"])), axis=1)
    df["Inn"] = df.apply(lambda r: utils.inning_label(r["inning"], r["half"]), axis=1)
    display_cols = ["Inn", "outs", "obc", "pitcher_name", "batter_name", "pitch", "swing", "diff", "result"]
    st.dataframe(
        df[display_cols].rename(columns={
            "outs": "Outs", "obc": "Runners",
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

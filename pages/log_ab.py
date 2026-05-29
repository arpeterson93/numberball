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

if st.session_state.pop("_reset_pending", False):
    st.session_state["pitch_input"] = 500
    st.session_state["swing_input"] = 500
    st.session_state["rpill_hits"] = None
    st.session_state["rpill_outs"] = None

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
st.subheader("Log At-Bat")

# ------------------------------------------------------------------ half / inning / outs / runners

col1, col2, col3 = st.columns(3)
with col1:
    half_choice = st.radio("Half", ["▲ Top", "▼ Bot"], horizontal=True, key="half_input")
    half = "top" if half_choice == "▲ Top" else "bottom"
with col2:
    inning = st.number_input("Inning", min_value=1, max_value=15, value=1, step=1, key="inning_input")
with col3:
    outs = st.selectbox("Outs", [0, 1, 2], key="outs_input")

obc = st.selectbox("Runners", utils.OBC_OPTIONS, key="obc_input")

# ------------------------------------------------------------------ pitcher

st.markdown("**Pitcher**")
col_pt, col_pn = st.columns([1, 2])
with col_pt:
    pitcher_team = st.selectbox("Team", utils.TEAMS, index=None, placeholder="Team", key="pt")
with col_pn:
    pitchers = db.get_players(pitcher_team) if pitcher_team else []
    pitcher_name = st.selectbox("Name", pitchers, index=None, placeholder="Name", key="pname_select")

# ------------------------------------------------------------------ batter

st.markdown("**Batter**")
col_bt, col_bn = st.columns([1, 2])
with col_bt:
    batter_team = st.selectbox("Team", utils.TEAMS, index=None, placeholder="Team", key="bt")
with col_bn:
    batters = db.get_players(batter_team) if batter_team else []
    batter_name = st.selectbox("Name", batters, index=None, placeholder="Name", key="bname_select")

# ------------------------------------------------------------------ numbers (live diff)

st.markdown("**Numbers**")
col_p, col_s = st.columns(2)
with col_p:
    pitch = st.number_input("Pitch (1-1000)", min_value=1, max_value=1000, value=500, step=1, key="pitch_input")
with col_s:
    swing = st.number_input("Swing (1-1000)", min_value=1, max_value=1000, value=500, step=1, key="swing_input")

diff = utils.circular_diff(int(pitch), int(swing))
st.info(f"Diff: **{diff}** | Pitch Zone: **{utils.get_zone(int(pitch))}**")

# ------------------------------------------------------------------ result

st.markdown("**Result**")
hit_pill = st.pills(
    "Hits & Walks",
    utils.RESULTS_HITS,
    key="rpill_hits",
    on_change=lambda: st.session_state.update({"rpill_outs": None}),
)
out_pill = st.pills(
    "Outs",
    utils.RESULTS_OUTS,
    key="rpill_outs",
    on_change=lambda: st.session_state.update({"rpill_hits": None}),
)
result = hit_pill or out_pill or ""

# ------------------------------------------------------------------ submit

def reset_entry():
    st.session_state["_reset_pending"] = True


def do_insert(session_id, inning, half, outs, obc, pitcher_team, batter_team,
              pitcher_name, batter_name, pitch, swing, result, existing):
    is_fp_inn = not any(
        e["inning"] == inning and e.get("half", "top") == half for e in existing
    )
    is_fp_app = not any(e["pitcher_name"] == pitcher_name for e in existing)
    db.insert_at_bat(
        session_id=session_id, inning=inning, half=half, outs=outs, obc=obc,
        pitcher_team=pitcher_team, batter_team=batter_team,
        pitcher_name=pitcher_name, batter_name=batter_name,
        pitch=pitch, swing=swing, result=result,
        is_fp_app=is_fp_app, is_fp_inn=is_fp_inn,
    )


if st.button("Submit At-Bat", type="primary", width='stretch'):
    if not pitcher_name or not batter_name:
        st.error("Pitcher and batter names are required.")
    elif not result:
        st.error("Result is required.")
    else:
        existing = db.get_at_bats_for_session(active_session_id)
        prev = sorted(existing, key=lambda e: e["id"])[-1] if existing else None
        new_ab = {"inning": int(inning), "half": half, "outs": int(outs), "result": result}
        warnings = utils.validate_ab(new_ab, prev)

        if warnings:
            st.session_state["pending_ab"] = dict(
                session_id=active_session_id, inning=int(inning), half=half,
                outs=int(outs), obc=obc, pitcher_team=pitcher_team,
                batter_team=batter_team, pitcher_name=pitcher_name,
                batter_name=batter_name, pitch=int(pitch), swing=int(swing),
                result=result, existing=existing,
            )
            st.session_state["pending_warnings"] = warnings
        else:
            do_insert(active_session_id, int(inning), half, int(outs), obc,
                      pitcher_team, batter_team, pitcher_name, batter_name,
                      int(pitch), int(swing), result, existing)
            inn_label = utils.inning_label(int(inning), half)
            st.success(f"Logged: {inn_label} | {pitcher_name} vs {batter_name} → **{result}** (diff: {diff})")
            reset_entry()
            st.rerun()

if st.session_state.get("pending_warnings"):
    for w in st.session_state["pending_warnings"]:
        st.warning(w)
    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Submit anyway", type="primary", width='stretch'):
            p = st.session_state["pending_ab"]
            do_insert(p["session_id"], p["inning"], p["half"], p["outs"], p["obc"],
                      p["pitcher_team"], p["batter_team"], p["pitcher_name"],
                      p["batter_name"], p["pitch"], p["swing"], p["result"], p["existing"])
            st.session_state.pop("pending_ab", None)
            st.session_state.pop("pending_warnings", None)
            reset_entry()
            st.rerun()
    with col_cancel:
        if st.button("Cancel", width='stretch'):
            st.session_state.pop("pending_ab", None)
            st.session_state.pop("pending_warnings", None)
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
        width='stretch',
        hide_index=True,
    )

    if st.session_state.get("_confirm_delete"):
        st.warning("Delete the last entry? This cannot be undone.")
        col_yes, col_no = st.columns(2)
        with col_yes:
            if st.button("Yes, delete", type="primary", width='stretch'):
                db.delete_at_bat(int(df["id"].iloc[0]))
                st.session_state.pop("_confirm_delete", None)
                st.rerun()
        with col_no:
            if st.button("Cancel", width='stretch'):
                st.session_state.pop("_confirm_delete", None)
                st.rerun()
    else:
        if st.button("Delete last entry", type="secondary"):
            st.session_state["_confirm_delete"] = True
            st.rerun()
else:
    st.caption("No at-bats logged for this session yet.")

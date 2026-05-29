"""Pitcher scouting — zone frequency, delta patterns, situational tendencies."""
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Pitcher Scouting")

# ------------------------------------------------------------------ load & enrich data

@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    raw = db.get_all_at_bats()
    if not raw:
        return pd.DataFrame()
    df = utils.flatten_sessions(raw)
    return utils.enrich_df(df)

df_all = load_data()
if df_all.empty:
    st.info("No at-bats in the database yet.")
    st.stop()

# ------------------------------------------------------------------ filters

with st.sidebar:
    st.header("Filters")
    seasons = sorted(df_all["season"].dropna().unique(), reverse=True)
    selected_seasons = st.multiselect("Season", seasons, default=seasons)

    pitcher_teams = sorted(df_all["pitcher_team"].unique())
    selected_pt = st.selectbox("Pitcher Team", ["All"] + pitcher_teams)

    if selected_pt != "All":
        pitcher_names = sorted(df_all[df_all["pitcher_team"] == selected_pt]["pitcher_name"].unique())
    else:
        pitcher_names = sorted(df_all["pitcher_name"].unique())
    selected_pitcher = st.selectbox("Pitcher", ["All"] + pitcher_names)

    sessions_list = sorted(df_all["session_id"].dropna().unique())
    selected_sessions = st.multiselect("Sessions", sessions_list, default=sessions_list, format_func=lambda x: f"Session {int(x)}")

# Apply filters
df = df_all.copy()
if selected_seasons:
    df = df[df["season"].isin(selected_seasons)]
if selected_pt != "All":
    df = df[df["pitcher_team"] == selected_pt]
if selected_pitcher != "All":
    df = df[df["pitcher_name"] == selected_pitcher]
if selected_sessions:
    df = df[df["session_id"].isin(selected_sessions)]

if df.empty:
    st.warning("No at-bats match the current filters.")
    st.stop()

total = len(df)

# ------------------------------------------------------------------ summary metrics

avg_diff = df["diff"].mean()
meme_rate = df["is_meme_pitch"].mean() * 100
col1, col2, col3 = st.columns(3)
col1.metric("At-Bats", total)
col2.metric("Avg Diff", f"{avg_diff:.1f}")
col3.metric("Meme Rate", f"{meme_rate:.1f}%")

st.divider()

# ------------------------------------------------------------------ overall zone heatmap

st.subheader("Zone Distribution (All)")
zone_counts = df["pitch_zone"].value_counts().to_dict()
st.plotly_chart(utils.zone_heatmap(zone_counts, title="Pitch Zone Frequency"), width='stretch')

# ------------------------------------------------------------------ first pitch zones

st.subheader("First Pitch Tendencies")
col_a, col_b = st.columns(2)
with col_a:
    df_fp_app = df[df["is_fp_app"] == True]
    counts_fpa = df_fp_app["pitch_zone"].value_counts().to_dict() if not df_fp_app.empty else {}
    st.plotly_chart(
        utils.zone_heatmap(counts_fpa, title=f"First Pitch of Appearance (n={len(df_fp_app)})"),
        width='stretch',
        config={"displayModeBar": False},
    )
with col_b:
    df_fp_inn = df[df["is_fp_inn"] == True]
    counts_fpi = df_fp_inn["pitch_zone"].value_counts().to_dict() if not df_fp_inn.empty else {}
    st.plotly_chart(
        utils.zone_heatmap(counts_fpi, title=f"First Pitch of Inning (n={len(df_fp_inn)})"),
        width='stretch',
        config={"displayModeBar": False},
    )

# ------------------------------------------------------------------ zone by out count

st.subheader("Zone by Out Count")
cols = st.columns(3)
for i, out_count in enumerate([0, 1, 2]):
    df_out = df[df["outs"] == out_count]
    counts = df_out["pitch_zone"].value_counts().to_dict() if not df_out.empty else {}
    with cols[i]:
        st.plotly_chart(
            utils.zone_heatmap(counts, title=f"{out_count} Outs (n={len(df_out)})"),
            width='stretch',
        )

# ------------------------------------------------------------------ zone by runners

st.subheader("Zone by Base State")
obc_groups = [
    ("Empty", ["Empty"]),
    ("Runner(s) On", ["1B", "2B", "3B", "1&2B", "1&3B", "2&3B", "BL"]),
]
col_e, col_r = st.columns(2)
for col, (label, obc_vals) in zip([col_e, col_r], obc_groups):
    df_obc = df[df["obc"].isin(obc_vals)]
    counts = df_obc["pitch_zone"].value_counts().to_dict() if not df_obc.empty else {}
    with col:
        st.plotly_chart(
            utils.zone_heatmap(counts, title=f"{label} (n={len(df_obc)})"),
            width='stretch',
        )

# ------------------------------------------------------------------ delta analysis

st.divider()
st.subheader("Pitch Delta (Change from Previous AB)")

deltas = df["pitch_circ_delta"].dropna()
if not deltas.empty:
    st.plotly_chart(utils.delta_histogram(deltas), width='stretch')

    avg_delta = deltas.mean()
    abs_avg_delta = deltas.abs().mean()
    col1, col2, col3 = st.columns(3)
    col1.metric("Avg Delta", f"{avg_delta:+.1f}")
    col2.metric("Avg |Delta|", f"{abs_avg_delta:.1f}")
    col3.metric("# with Delta", len(deltas))
else:
    st.caption("Need at least 2 at-bats from the same pitcher in a session to compute deltas.")

# ------------------------------------------------------------------ meme & last digits

st.divider()
st.subheader("Tendencies")

col_m, col_l = st.columns(2)

with col_m:
    st.markdown("**Meme Pitches (42, 69, 420)**")
    meme_counts = {str(n): int((df["pitch"] == n).sum()) for n in utils.MEME_NUMBERS}
    meme_total = sum(meme_counts.values())
    meme_pct = meme_total / total * 100 if total else 0
    st.metric("Meme Pitches", meme_total, help=f"{meme_pct:.1f}% of all pitches")
    for num, count in meme_counts.items():
        pct = count / total * 100 if total else 0
        st.write(f"  **{num}**: {count} ({pct:.1f}%)")

with col_l:
    st.markdown("**Most Common Last 2 Digits**")
    last2 = df["pitch_last2"].value_counts().head(5)
    if not last2.empty:
        for digits, count in last2.items():
            pct = count / total * 100
            st.write(f"  **{digits:02d}**: {count} ({pct:.1f}%)")

# ------------------------------------------------------------------ result distribution

st.divider()
st.subheader("Results Allowed")

res_counts = df["result"].value_counts().to_dict()
st.plotly_chart(utils.result_bar(res_counts, title="Result Distribution"), width='stretch')

res_cat_counts = df["res_category"].value_counts().to_dict()
st.plotly_chart(utils.result_bar(res_cat_counts, title="Result Category"), width='stretch')

# ------------------------------------------------------------------ last n pitches

st.divider()
st.subheader("Last N Pitches")
n_pitches = st.slider("# of at-bats", 5, 50, 20, key="last_n_pitch")
st.plotly_chart(
    utils.last_n_combined_chart(df, n=n_pitches, delta_col="pitch", title=f"Last {n_pitches} Pitches"),
    width='stretch',
)

# ------------------------------------------------------------------ hot zone matrix

st.divider()
st.subheader("Hot Zone Pitch Matrix")
st.caption("How often each pitch range is followed by each other pitch range.")
bucket_size_p = st.select_slider(
    "Bucket size", options=[50, 100, 125, 200, 250, 500], value=100, key="hz_bucket_pitch"
)
if selected_pitcher != "All":
    group_cols = ["session_id", "pitcher_name"]
else:
    group_cols = ["pitcher_name"]
st.plotly_chart(
    utils.hot_zone_matrix(df, value_col="pitch", group_cols=group_cols, bucket_size=bucket_size_p),
    width='stretch',
)

# ------------------------------------------------------------------ swing predictor

st.divider()
st.subheader("Swing Predictor")
st.caption("Enter a proposed swing to see what result each of this pitcher's pitches would give — and how recent pitches line up.")
col_sw, col_n = st.columns([2, 1])
with col_sw:
    proposed_swing = st.number_input("Proposed Swing", min_value=1, max_value=1000, value=500, step=1, key="pred_swing_p")
with col_n:
    n_pred = st.slider("Recent pitches", 5, 30, 15, key="pred_n_p")
st.plotly_chart(
    utils.swing_predictor_chart(df, swing=int(proposed_swing), n=n_pred),
    width='stretch',
)

# ------------------------------------------------------------------ raw data

with st.expander("Raw At-Bat Data"):
    display = df[["season", "session_id", "inning", "outs", "obc",
                  "pitcher_name", "batter_name", "pitch", "swing", "diff", "result", "res_category"]].copy()
    display.columns = ["Season", "Session", "Inn", "Outs", "Runners",
                       "Pitcher", "Batter", "Pitch", "Swing", "Diff", "Result", "Category"]
    st.dataframe(display, width='stretch', hide_index=True)

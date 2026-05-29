"""Batter scouting — swing zones, hit results, tendencies."""
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Batter Scouting")

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

    batter_teams = sorted(df_all["batter_team"].unique())
    selected_bt = st.selectbox("Batter Team", ["All"] + batter_teams)

    if selected_bt != "All":
        batter_names = sorted(df_all[df_all["batter_team"] == selected_bt]["batter_name"].unique())
    else:
        batter_names = sorted(df_all["batter_name"].unique())
    selected_batter = st.selectbox("Batter", ["All"] + batter_names)

    sessions_list = sorted(df_all["session_id"].dropna().unique())
    selected_sessions = st.multiselect("Sessions", sessions_list, default=sessions_list, format_func=lambda x: f"Session {int(x)}")

# Apply filters
df = df_all.copy()
if selected_seasons:
    df = df[df["season"].isin(selected_seasons)]
if selected_bt != "All":
    df = df[df["batter_team"] == selected_bt]
if selected_batter != "All":
    df = df[df["batter_name"] == selected_batter]
if selected_sessions:
    df = df[df["session_id"].isin(selected_sessions)]

if df.empty:
    st.warning("No at-bats match the current filters.")
    st.stop()

total = len(df)

# ------------------------------------------------------------------ summary metrics

avg_diff = df["diff"].mean()
xbh_rate = (df["res_category"] == "XBH").mean() * 100
obp_rate = df["res_category"].isin(["XBH", "BB/1B"]).mean() * 100
meme_rate = df["is_meme_swing"].mean() * 100

col1, col2, col3, col4 = st.columns(4)
col1.metric("At-Bats", total)
col2.metric("Avg Diff", f"{avg_diff:.1f}")
col3.metric("OB%", f"{obp_rate:.1f}%")
col4.metric("XBH%", f"{xbh_rate:.1f}%")

st.divider()

# ------------------------------------------------------------------ pitch predictor

st.subheader("Pitch Predictor")
st.caption("Enter a proposed pitch to see what result each of this batter's swings would give and how recent swings line up.")

all_sessions = db.get_sessions()
sessions_by_id = {s["id"]: s for s in all_sessions}
sheet_urls = list(dict.fromkeys(
    s["sheet_url"] for sid in selected_sessions
    if (s := sessions_by_id.get(sid)) and s.get("sheet_url")
))

@st.cache_data(ttl=3600)
def _sheet_name(url: str) -> str:
    return utils.get_sheet_name(url)

col_sheet, col_btn = st.columns([3, 1])
with col_sheet:
    if sheet_urls:
        pred_sheet_url = sheet_urls[0] if len(sheet_urls) == 1 else st.selectbox(
            "Session sheet", sheet_urls, key="pred_sheet_sel_b",
            format_func=_sheet_name,
        )
        st.caption(f"Linked: {_sheet_name(pred_sheet_url)}")
    else:
        st.caption("No sheet linked to selected session(s). Using default ranges.")
        pred_sheet_url = None
with col_btn:
    st.write("")
    if sheet_urls and st.button("Pull Ranges", type="secondary", key="pull_ranges_b"):
        try:
            fetched_ranges, fetched_batter = utils.parse_result_ranges_from_sheet(pred_sheet_url)
            st.session_state["pred_result_ranges"] = fetched_ranges
            st.session_state["pred_sheet_batter"] = fetched_batter
            st.success(f"Loaded {len(fetched_ranges)} ranges.")
        except Exception as e:
            st.error(str(e))

result_ranges = st.session_state.get("pred_result_ranges")
batter_name = st.session_state.get("pred_sheet_batter", "")

if result_ranges:
    st.caption(f"Pulled {len(result_ranges)} ranges — Batter: **{batter_name}**")
    col_p, col_n = st.columns([3, 1])
    with col_p:
        proposed_pitch = st.number_input("Proposed Pitch", min_value=1, max_value=1000, value=500, step=1, key="pred_pitch_b")
    with col_n:
        n_pred_b = st.slider("# swings", 5, 50, 15, key="pred_n_b")
    st.plotly_chart(
        utils.swing_predictor_chart(
            df, swing=int(proposed_pitch), n=n_pred_b,
            result_ranges=result_ranges,
            tick_label=f"Last {n_pred_b} swings",
            value_col="swing",
            x_label="Swing Value",
            ref_label="Pitch",
        ),
        width='stretch',
    )
else:
    st.info("Pull ranges from the session sheet above to enable the predictor.")

# ------------------------------------------------------------------ last n swings

st.divider()
st.subheader("Last N Swings")
n_swings = st.slider("# of at-bats", 5, 50, 20, key="last_n_swing")
st.plotly_chart(
    utils.last_n_combined_chart(df, n=n_swings, delta_col="swing", title=f"Last {n_swings} Swings"),
    width='stretch',
)

# ------------------------------------------------------------------ hot zone matrix

st.divider()
st.subheader("Hot Zone Swing Matrix")
st.caption("How often each swing range is followed by each other swing range.")
bucket_size_s = st.select_slider(
    "Bucket size", options=[50, 100, 125, 200, 250, 500], value=100, key="hz_bucket_swing"
)
if selected_batter != "All":
    group_cols = ["session_id", "batter_name"]
else:
    group_cols = ["batter_name"]
st.plotly_chart(
    utils.hot_zone_matrix(df, value_col="swing", group_cols=group_cols, bucket_size=bucket_size_s),
    width='stretch',
)

# ------------------------------------------------------------------ overall swing zone

st.divider()
st.subheader("Swing Zone Distribution (All)")
swing_counts = df["swing_zone"].value_counts().to_dict()
st.plotly_chart(utils.zone_heatmap(swing_counts, title="Swing Zone Frequency"), width='stretch')

# ------------------------------------------------------------------ first pitch swings

st.subheader("First Pitch Swing Tendencies")
col_a, col_b = st.columns(2)
with col_a:
    df_fp_app = df[df["is_fp_app"] == True]
    counts_fpa = df_fp_app["swing_zone"].value_counts().to_dict() if not df_fp_app.empty else {}
    st.plotly_chart(
        utils.zone_heatmap(counts_fpa, title=f"First Pitch of Appearance (n={len(df_fp_app)})"),
        width='stretch',
        config={"displayModeBar": False},
    )
with col_b:
    df_fp_inn = df[df["is_fp_inn"] == True]
    counts_fpi = df_fp_inn["swing_zone"].value_counts().to_dict() if not df_fp_inn.empty else {}
    st.plotly_chart(
        utils.zone_heatmap(counts_fpi, title=f"First Pitch of Inning (n={len(df_fp_inn)})"),
        width='stretch',
        config={"displayModeBar": False},
    )

# ------------------------------------------------------------------ zone by out count

st.subheader("Swing Zone by Out Count")
cols = st.columns(3)
for i, out_count in enumerate([0, 1, 2]):
    df_out = df[df["outs"] == out_count]
    counts = df_out["swing_zone"].value_counts().to_dict() if not df_out.empty else {}
    with cols[i]:
        st.plotly_chart(
            utils.zone_heatmap(counts, title=f"{out_count} Outs (n={len(df_out)})"),
            width='stretch',
        )

# ------------------------------------------------------------------ zone by runners

st.subheader("Swing Zone by Base State")
obc_groups = [
    ("Empty", ["Empty"]),
    ("Runner(s) On", ["1B", "2B", "3B", "1&2B", "1&3B", "2&3B", "BL"]),
]
col_e, col_r = st.columns(2)
for col, (label, obc_vals) in zip([col_e, col_r], obc_groups):
    df_obc = df[df["obc"].isin(obc_vals)]
    counts = df_obc["swing_zone"].value_counts().to_dict() if not df_obc.empty else {}
    with col:
        st.plotly_chart(
            utils.zone_heatmap(counts, title=f"{label} (n={len(df_obc)})"),
            width='stretch',
        )

# ------------------------------------------------------------------ zone by result quality

st.subheader("Swing Zone by Result")
result_groups = [("XBH", ["XBH"]), ("BB/1B", ["BB/1B"]), ("OUT", ["OUT"])]
cols3 = st.columns(3)
for col, (label, cats) in zip(cols3, result_groups):
    df_res = df[df["res_category"].isin(cats)]
    counts = df_res["swing_zone"].value_counts().to_dict() if not df_res.empty else {}
    with col:
        st.plotly_chart(
            utils.zone_heatmap(counts, title=f"{label} (n={len(df_res)})"),
            width='stretch',
        )

# ------------------------------------------------------------------ swing delta

st.divider()
st.subheader("Swing Delta (Change from Previous AB)")

deltas = df["swing_circ_delta"].dropna()

if not deltas.empty:
    st.plotly_chart(utils.delta_histogram(deltas, title="Swing Delta Distribution"), width='stretch')
    col1, col2, col3 = st.columns(3)
    col1.metric("Avg Delta", f"{deltas.mean():+.1f}")
    col2.metric("Avg |Delta|", f"{deltas.abs().mean():.1f}")
    col3.metric("# with Delta", len(deltas))
else:
    st.caption("Need at least 2 at-bats from the same batter in a session to compute deltas.")

# ------------------------------------------------------------------ meme & last digits

st.divider()
st.subheader("Tendencies")

col_m, col_l = st.columns(2)

with col_m:
    st.markdown("**Meme Swings (42, 69, 420)**")
    meme_counts = {str(n): int((df["swing"] == n).sum()) for n in utils.MEME_NUMBERS}
    meme_total = sum(meme_counts.values())
    meme_pct = meme_total / total * 100 if total else 0
    st.metric("Meme Swings", meme_total, help=f"{meme_pct:.1f}% of all swings")
    for num, count in meme_counts.items():
        pct = count / total * 100 if total else 0
        st.write(f"  **{num}**: {count} ({pct:.1f}%)")

with col_l:
    st.markdown("**Most Common Last 2 Digits**")
    last2 = df["swing"].apply(lambda s: int(str(int(s)).zfill(2)[-2:])).value_counts().head(5)
    for digits, count in last2.items():
        pct = count / total * 100
        st.write(f"  **{digits:02d}**: {count} ({pct:.1f}%)")

# ------------------------------------------------------------------ result distribution

st.divider()
st.subheader("Results")

res_counts = df["result"].value_counts().to_dict()
st.plotly_chart(utils.result_bar(res_counts, title="Result Distribution"), width='stretch')

res_cat_counts = df["res_category"].value_counts().to_dict()
st.plotly_chart(utils.result_bar(res_cat_counts, title="Result Category"), width='stretch')

# ------------------------------------------------------------------ raw data

with st.expander("Raw At-Bat Data"):
    display = df[["season", "session_id", "inning", "outs", "obc",
                  "pitcher_name", "batter_name", "pitch", "swing", "diff", "result", "res_category"]].copy()
    display.columns = ["Season", "Session", "Inn", "Outs", "Runners",
                       "Pitcher", "Batter", "Pitch", "Swing", "Diff", "Result", "Category"]
    st.dataframe(display, width='stretch', hide_index=True)

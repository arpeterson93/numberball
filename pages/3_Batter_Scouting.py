"""Batter scouting: swing zones, hit results, tendencies."""
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Batter Scouting")

# ------------------------------------------------------------------ load & enrich data

@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    raw = db.get_all_plays()
    if not raw:
        return pd.DataFrame()
    df = utils.flatten_games(raw)
    return utils.enrich_df(df)

@st.cache_data(ttl=300)
def _load_all_players() -> list:
    return db.get_all_players()

df_all = load_data()
if df_all.empty:
    st.info("No at-bats in the database yet.")
    st.stop()

# Apply auto-filter from cross-page matchup import before sidebar renders
if "_auto_batter_filter" in st.session_state:
    _abf = st.session_state.pop("_auto_batter_filter")
    if _abf.get("team") and _abf["team"] in df_all["off_team"].unique():
        st.session_state["sbt_filter"] = _abf["team"]
    if _abf.get("batter") and _abf["batter"] in df_all["batter_name"].unique():
        st.session_state["sbatter_filter"] = _abf["batter"]

# ------------------------------------------------------------------ filters

with st.sidebar:
    st.header("Filters")
    seasons = sorted(df_all["season"].dropna().unique(), reverse=True)
    selected_seasons = st.multiselect("Season", seasons, default=seasons)

    off_teams = sorted(df_all["off_team"].unique())
    selected_bt = st.selectbox("Batter Team", ["All"] + off_teams, key="sbt_filter")

    if selected_bt != "All":
        batter_names = sorted(df_all[df_all["off_team"] == selected_bt]["batter_name"].unique())
    else:
        batter_names = sorted(df_all["batter_name"].unique())
    selected_batter = st.selectbox("Batter", ["All"] + batter_names, key="sbatter_filter")

    if selected_batter != "All":
        batter_scope = st.radio(
            "Scope", ["Solo", "Full Team"],
            horizontal=True, key="batter_scope",
            help="Solo: only this batter's ABs. Full Team: all ABs from their team.",
        )
    else:
        batter_scope = "Solo"

    games_list = sorted(df_all["game_id"].dropna().unique())
    selected_games = st.multiselect("Games", games_list, default=games_list, format_func=lambda x: f"Game {int(x)}")

# Apply filters
df = df_all.copy()
if selected_seasons:
    df = df[df["season"].isin(selected_seasons)]
if selected_bt != "All":
    df = df[df["off_team"] == selected_bt]
if selected_batter != "All":
    if batter_scope == "Solo":
        df = df[df["batter_name"] == selected_batter]
if selected_games:
    df = df[df["game_id"].isin(selected_games)]

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

pred_mode = st.radio(
    "Matchup source",
    ["Fetch Live Matchup", "Historical Matchup", "Manual Setup"],
    horizontal=True,
    key="pred_mode_b",
)

# ── helpers for calculator modes ─────────────────────────────────────────────

@st.cache_data(ttl=3600)
def _sheet_name(url: str) -> str:
    return utils.get_sheet_name(url)

_all_players_b   = _load_all_players()
_pbyn_b          = {p["name"]: p for p in _all_players_b if p.get("name")}
_all_teams_b     = sorted({
    utils.TEAM_ABBREV.get(p.get("team", ""), p.get("team", "?"))
    for p in _all_players_b if p.get("team")
})
_full_to_abbrev_b = {v: k for k, v in utils.TEAM_ABBREV.items()}
_all_player_names_b = sorted(p["name"] for p in _all_players_b if p.get("name"))
_RUNNER_UNK_B = "⚡ Runner (SPD?)"
_OBC_BASES_B = {
    "Empty":[], "1B":[1], "2B":[2], "3B":[3],
    "1&2B":[1,2], "1&3B":[1,3], "2&3B":[2,3], "BL":[1,2,3],
}

def _players_for_team_b(team_full: str) -> list:
    if team_full == "All":
        return _all_player_names_b
    abbrev = _full_to_abbrev_b.get(team_full, team_full)
    return sorted(p["name"] for p in _all_players_b if p.get("name") and p.get("team") == abbrev)

def _stat_b(p, key, default=3):
    v = p.get(key)
    return int(v) if v is not None else default

def _hand_b(p):
    h = str(p.get("hand", "R")).upper()
    return h if h in ("L", "R", "S") else "R"

# ── session-state defaults (shared with Pitcher Scouting for cross-page load) ─

_CALC_DEF_B = {
    # Batter stats — shared keys; Batter Scouting syncs these from sidebar
    "pred_calc_b_hand": "R", "pred_calc_b_con": 3, "pred_calc_b_eye": 3,
    "pred_calc_b_pow": 3,    "pred_calc_b_spd": 3,
    # Pitcher picker — Batter Scouting specific
    "pred_calc_p_team": "All", "pred_calc_p_name": "-- Manual --",
    # Pitcher stats — shared keys; Pitcher Scouting syncs from sidebar, Batter Scouting uses picker
    "pred_calc_p_hand": "R", "pred_calc_p_mov": 3, "pred_calc_p_cmd": 3,
    "pred_calc_p_vel": 3,    "pred_calc_p_awr": 3,
    # Situation + runners — shared
    "pred_calc_outs": 0, "pred_calc_bunt": False, "pred_calc_hnr": False,
    "pred_calc_1b": "Empty", "pred_calc_2b": "Empty", "pred_calc_3b": "Empty",
}
for _ck, _cv in _CALC_DEF_B.items():
    if _ck not in st.session_state:
        st.session_state[_ck] = _cv

# ── sync batter stats from sidebar selection ──────────────────────────────────

_batter_rec_b = _pbyn_b.get(selected_batter, {}) if selected_batter != "All" else {}
if selected_batter != "All" and st.session_state.get("pred_calc_loaded_batter") != selected_batter:
    st.session_state["pred_calc_loaded_batter"] = selected_batter
    st.session_state["pred_calc_b_hand"] = _hand_b(_batter_rec_b)
    st.session_state["pred_calc_b_con"]  = _stat_b(_batter_rec_b, "con")
    st.session_state["pred_calc_b_eye"]  = _stat_b(_batter_rec_b, "eye")
    st.session_state["pred_calc_b_pow"]  = _stat_b(_batter_rec_b, "pow")
    st.session_state["pred_calc_b_spd"]  = _stat_b(_batter_rec_b, "spd")

# ── callbacks ─────────────────────────────────────────────────────────────────

def _on_calc_p_team_b():
    st.session_state["pred_calc_p_name"] = "-- Manual --"
    for _k, _v in [("pred_calc_p_hand","R"),("pred_calc_p_mov",3),("pred_calc_p_cmd",3),
                   ("pred_calc_p_vel",3),("pred_calc_p_awr",3)]:
        st.session_state[_k] = _v

def _on_calc_pitcher_b():
    name = st.session_state.get("pred_calc_p_name", "-- Manual --")
    if name != "-- Manual --" and name in _pbyn_b:
        pp = _pbyn_b[name]
        st.session_state["pred_calc_p_hand"] = _hand_b(pp)
        st.session_state["pred_calc_p_mov"]  = _stat_b(pp, "mov")
        st.session_state["pred_calc_p_cmd"]  = _stat_b(pp, "cmd")
        st.session_state["pred_calc_p_vel"]  = _stat_b(pp, "vel")
        st.session_state["pred_calc_p_awr"]  = _stat_b(pp, "awr")

def _import_hist_play_b(play_id: int):
    _rows = df[df["id"] == play_id]
    if _rows.empty:
        return
    _pr = _rows.iloc[0]
    p_name = _pr.get("pitcher_name", "")
    pp = _pbyn_b.get(p_name, {})
    p_tf = utils.TEAM_ABBREV.get(pp.get("team", ""), "All")
    if p_tf in _all_teams_b:
        st.session_state["pred_calc_p_team"] = p_tf
    st.session_state["pred_calc_p_name"] = p_name if p_name in _pbyn_b else "-- Manual --"
    st.session_state["pred_calc_p_hand"] = _hand_b(pp) if pp else "R"
    st.session_state["pred_calc_p_mov"]  = _stat_b(pp, "mov") if pp else 3
    st.session_state["pred_calc_p_cmd"]  = _stat_b(pp, "cmd") if pp else 3
    st.session_state["pred_calc_p_vel"]  = _stat_b(pp, "vel") if pp else 3
    st.session_state["pred_calc_p_awr"]  = _stat_b(pp, "awr") if pp else 3
    outs_val = int(_pr.get("outs", 0)) if pd.notna(_pr.get("outs")) else 0
    st.session_state["pred_calc_outs"] = outs_val
    occupied = _OBC_BASES_B.get(str(_pr.get("obc", "Empty")), [])
    for _base in [1, 2, 3]:
        st.session_state[f"pred_calc_{_base}b"] = _RUNNER_UNK_B if _base in occupied else "Empty"
    # Cross-page: auto-filter Pitcher Scouting sidebar to this pitcher
    if p_name in _pbyn_b:
        _pt = utils.TEAM_ABBREV.get(_pbyn_b[p_name].get("team", ""), "All")
        st.session_state["_auto_pitcher_filter"] = {"team": _pt, "pitcher": p_name}

# ── shared widget block for batter stats + pitcher selection + situation ───────

_hand_opts_b   = ["R", "L", "S"]
_runner_opts_b = ["Empty", _RUNNER_UNK_B] + _all_player_names_b

def _render_calc_inputs_b(key_suffix: str = ""):
    """Batter stat overrides + pitcher picker + situation + runners."""
    col_b, col_p = st.columns(2)
    with col_b:
        st.markdown(f"**Batter Stats** · *{selected_batter}*")
        if st.session_state.get("pred_calc_b_hand") not in _hand_opts_b:
            st.session_state["pred_calc_b_hand"] = "R"
        st.selectbox(f"Hand##bh{key_suffix}", _hand_opts_b, key="pred_calc_b_hand")
        st.number_input(f"CON##bc{key_suffix}", min_value=1, max_value=5, key="pred_calc_b_con")
        st.number_input(f"EYE##be{key_suffix}", min_value=1, max_value=5, key="pred_calc_b_eye")
        st.number_input(f"POW##bp{key_suffix}", min_value=1, max_value=5, key="pred_calc_b_pow")
        st.number_input(f"SPD##bs{key_suffix}", min_value=1, max_value=5, key="pred_calc_b_spd")
    with col_p:
        st.markdown("**Pitcher**")
        _p_team_opts = ["All"] + _all_teams_b
        if st.session_state.get("pred_calc_p_team") not in _p_team_opts:
            st.session_state["pred_calc_p_team"] = "All"
        _p_team = st.selectbox(f"Team##pt{key_suffix}", _p_team_opts,
                               key="pred_calc_p_team", on_change=_on_calc_p_team_b)
        _p_name_opts = ["-- Manual --"] + _players_for_team_b(_p_team)
        if st.session_state.get("pred_calc_p_name") not in _p_name_opts:
            st.session_state["pred_calc_p_name"] = "-- Manual --"
        st.selectbox(f"Pitcher##pn{key_suffix}", _p_name_opts,
                     key="pred_calc_p_name", on_change=_on_calc_pitcher_b)
        if st.session_state.get("pred_calc_p_hand") not in _hand_opts_b:
            st.session_state["pred_calc_p_hand"] = "R"
        st.selectbox(f"Hand##ph{key_suffix}", _hand_opts_b, key="pred_calc_p_hand")
        st.number_input(f"MOV##pm{key_suffix}", min_value=1, max_value=5, key="pred_calc_p_mov")
        st.number_input(f"CMD##pc{key_suffix}", min_value=1, max_value=5, key="pred_calc_p_cmd")
        st.number_input(f"VEL##pv{key_suffix}", min_value=1, max_value=5, key="pred_calc_p_vel")
        st.number_input(f"AWR##pa{key_suffix}", min_value=1, max_value=5, key="pred_calc_p_awr")

    st.divider()
    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        if st.session_state.get("pred_calc_outs") not in [0, 1, 2]:
            st.session_state["pred_calc_outs"] = 0
        st.radio(f"Outs##os{key_suffix}", [0, 1, 2], horizontal=True, key="pred_calc_outs")
    with col_s2:
        st.checkbox(f"Bunting?##bu{key_suffix}", key="pred_calc_bunt")
    with col_s3:
        st.checkbox(f"Hit & Run?##hr{key_suffix}", key="pred_calc_hnr")

    st.markdown("**Baserunners**")
    col_1b, col_2b, col_3b = st.columns(3)
    with col_1b:
        if st.session_state["pred_calc_1b"] not in _runner_opts_b:
            st.session_state["pred_calc_1b"] = "Empty"
        _r1 = st.selectbox(f"1B##r1{key_suffix}", _runner_opts_b, key="pred_calc_1b")
        if _r1 in _pbyn_b:
            st.caption(f"SPD: {_stat_b(_pbyn_b[_r1], 'spd')}")
    with col_2b:
        if st.session_state["pred_calc_2b"] not in _runner_opts_b:
            st.session_state["pred_calc_2b"] = "Empty"
        _r2 = st.selectbox(f"2B##r2{key_suffix}", _runner_opts_b, key="pred_calc_2b")
        if _r2 in _pbyn_b:
            st.caption(f"SPD: {_stat_b(_pbyn_b[_r2], 'spd')}")
    with col_3b:
        if st.session_state["pred_calc_3b"] not in _runner_opts_b:
            st.session_state["pred_calc_3b"] = "Empty"
        _r3 = st.selectbox(f"3B##r3{key_suffix}", _runner_opts_b, key="pred_calc_3b")
        if _r3 in _pbyn_b:
            st.caption(f"SPD: {_stat_b(_pbyn_b[_r3], 'spd')}")

def _calc_ranges_from_state_b() -> list:
    _runners = any(st.session_state.get(f"pred_calc_{b}b", "Empty") != "Empty" for b in [1, 2, 3])
    return utils.compute_at_bat_ranges(
        pitcher_hand=st.session_state.get("pred_calc_p_hand", "R"),
        pitcher_mov=int(st.session_state.get("pred_calc_p_mov", 3)),
        pitcher_cmd=int(st.session_state.get("pred_calc_p_cmd", 3)),
        pitcher_vel=int(st.session_state.get("pred_calc_p_vel", 3)),
        pitcher_awr=int(st.session_state.get("pred_calc_p_awr", 3)),
        batter_hand=st.session_state.get("pred_calc_b_hand", "R"),
        batter_con=int(st.session_state.get("pred_calc_b_con", 3)),
        batter_eye=int(st.session_state.get("pred_calc_b_eye", 3)),
        batter_pow=int(st.session_state.get("pred_calc_b_pow", 3)),
        batter_spd=int(st.session_state.get("pred_calc_b_spd", 3)),
        bunt=bool(st.session_state.get("pred_calc_bunt", False)),
        hit_and_run=bool(st.session_state.get("pred_calc_hnr", False)),
        outs=int(st.session_state.get("pred_calc_outs", 0)),
        runners_on=_runners,
    )

# ── mode-specific UI ──────────────────────────────────────────────────────────

result_ranges  = None
pitcher_name_s = ""
batter_name_s  = ""
df_for_pred    = df

if pred_mode == "Fetch Live Matchup":

    with st.expander("Live Matchup Settings", expanded=True):
        all_games_sw = db.get_games()
        games_by_id  = {g["id"]: g for g in all_games_sw}
        sheet_urls   = list(dict.fromkeys(
            g["sheet_url"] for gid in selected_games
            if (g := games_by_id.get(gid)) and g.get("sheet_url")
        ))

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
            if sheet_urls and st.button("Fetch Matchup", type="secondary", key="pull_ranges_b"):
                try:
                    fetched_ranges, fetched_batter, fetched_pitcher = utils.parse_result_ranges_from_sheet(pred_sheet_url)
                    st.session_state["pred_result_ranges"] = fetched_ranges
                    st.session_state["pred_sheet_batter"]  = fetched_batter
                    st.session_state["pred_sheet_pitcher"] = fetched_pitcher
                    if fetched_batter:
                        _all_p_tmp = db.get_all_players()
                        _p_tmp = next((p for p in _all_p_tmp if p["name"] == fetched_batter), None)
                        _ta = _p_tmp.get("team") if _p_tmp else None
                        st.session_state["_auto_batter_filter"] = {
                            "team":   utils.TEAM_ABBREV.get(_ta, _ta),
                            "batter": fetched_batter,
                        }
                    st.toast(f"Loaded {len(fetched_ranges)} ranges.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    result_ranges  = st.session_state.get("pred_result_ranges")
    batter_name_s  = st.session_state.get("pred_sheet_batter", "")
    pitcher_name_s = st.session_state.get("pred_sheet_pitcher", "")
    df_for_pred    = df

elif pred_mode == "Historical Matchup":

    if selected_batter == "All":
        st.warning("Select a specific batter in the sidebar to use Historical Matchup.")
    else:
        with st.expander("Historical Matchup", expanded=True):
            col_hs, col_hg, col_hp = st.columns([1, 2, 3])
            with col_hs:
                _h_seasons = sorted(df["season"].dropna().unique(), reverse=True)
                _h_season  = st.selectbox("Season", _h_seasons, key="pred_hist_season_b")
            _df_hs = df[df["season"] == _h_season]
            with col_hg:
                _h_games = sorted(_df_hs["game_id"].dropna().unique())
                _h_game  = st.selectbox("Game", _h_games,
                                        format_func=lambda x: f"Game {int(x)}",
                                        key="pred_hist_game_b")
            _df_hg = _df_hs[_df_hs["game_id"] == _h_game].sort_values("id")
            with col_hp:
                _h_po = _df_hg[["id","inning","half","outs","obc","pitcher_name","pitch","swing","result"]].copy()
                _h_po["label"] = _h_po.apply(
                    lambda r: (
                        f"#{int(r['id'])}  Inn {int(r['inning'])}{r['half'][0].upper()}  "
                        f"{r['outs']}out  {r['obc']}  vs {r['pitcher_name']}  "
                        f"P:{r['pitch']} S:{r['swing']} → {r['result']}"
                    ), axis=1,
                )
                _h_ids  = _h_po["id"].tolist()
                _h_play = st.selectbox(
                    "Play",
                    [None] + _h_ids,
                    format_func=lambda x: "-- None --" if x is None
                        else _h_po.loc[_h_po["id"] == x, "label"].iloc[0],
                    key="pred_hist_play_b",
                )

            col_hi, col_hinfo = st.columns([1, 5])
            with col_hi:
                if _h_play is not None and st.button("Import Play", type="primary",
                                                      use_container_width=True, key="pred_hist_import_b"):
                    _import_hist_play_b(int(_h_play))
                    st.session_state["pred_hist_loaded_id_b"] = int(_h_play)
                    st.rerun()
            with col_hinfo:
                if _h_play is not None:
                    _hpr = _df_hg[_df_hg["id"] == _h_play].iloc[0]
                    if pd.notna(_hpr.get("pitch")) and pd.notna(_hpr.get("swing")):
                        _had = utils.circular_diff(int(_hpr["pitch"]), int(_hpr["swing"]))
                        st.caption(
                            f"Pitch **{int(_hpr['pitch'])}** · Swing **{int(_hpr['swing'])}** · "
                            f"Diff **{_had}** · Result **{_hpr.get('result','')}**"
                        )

            st.divider()
            _render_calc_inputs_b("h")

        _hist_id_b = st.session_state.get("pred_hist_loaded_id_b")
        _calc_r_b  = _calc_ranges_from_state_b()
        result_ranges  = [(r["result"], r["low"], r["high"]) for r in _calc_r_b]
        pitcher_name_s = st.session_state.get("pred_calc_p_name", "")
        batter_name_s  = selected_batter
        df_for_pred    = df[df["id"] < _hist_id_b] if _hist_id_b else df

        if _hist_id_b:
            _hpr_row = df[df["id"] == _hist_id_b]
            if not _hpr_row.empty:
                _hpr2 = _hpr_row.iloc[0]
                if pd.notna(_hpr2.get("pitch")) and pd.notna(_hpr2.get("swing")):
                    _had2   = utils.circular_diff(int(_hpr2["pitch"]), int(_hpr2["swing"]))
                    _hmatch = next((r for r in _calc_r_b if r["low"] <= _had2 <= r["high"]), None)
                    _hexp   = _hmatch["result"] if _hmatch else "?"
                    _hicon  = "✓" if _hexp == _hpr2.get("result", "") else "≠"
                    st.caption(
                        f"Actual play · Pitch **{int(_hpr2['pitch'])}** · Swing **{int(_hpr2['swing'])}** · "
                        f"Diff **{_had2}** → calculator expects **{_hexp}** {_hicon} actual **{_hpr2.get('result','')}**"
                    )

elif pred_mode == "Manual Setup":

    if selected_batter == "All":
        st.warning("Select a specific batter in the sidebar to use Manual Setup.")
    else:
        with st.expander("Manual Setup", expanded=True):
            _render_calc_inputs_b("m")

        _calc_r_m      = _calc_ranges_from_state_b()
        result_ranges  = [(r["result"], r["low"], r["high"]) for r in _calc_r_m]
        pitcher_name_s = st.session_state.get("pred_calc_p_name", "")
        batter_name_s  = selected_batter
        df_for_pred    = df

# ── pitch predictor chart + optimal pitch ─────────────────────────────────────

if result_ranges:
    _matchup_label = " vs ".join(filter(None, [pitcher_name_s, batter_name_s])) or f"{len(result_ranges)} ranges"
    st.caption(f"Matchup: **{_matchup_label}**")

    if "_pending_pitch_b" in st.session_state:
        st.session_state["pred_pitch_b"] = st.session_state.pop("_pending_pitch_b")
    for _pk_reset in st.session_state.pop("_pills_to_reset_b", []):
        st.session_state[_pk_reset] = None

    col_p, col_n = st.columns([3, 1])
    with col_p:
        if "pred_pitch_b" not in st.session_state:
            st.session_state["pred_pitch_b"] = 500
        proposed_pitch = st.number_input("Proposed Pitch", min_value=1, max_value=1000, step=1, key="pred_pitch_b")
    with col_n:
        n_pred_b = st.slider("# swings", 5, 50, 20, key="pred_n_b")

    _df_tick_b = df_for_pred if not df_for_pred.empty else pd.DataFrame(columns=["id","pitch","swing"])
    _tick_lbl_b = (
        f"Last {n_pred_b} swings (pre-AB)" if pred_mode == "Historical Matchup" and not df_for_pred.empty
        else f"Last {n_pred_b} swings"
    )
    st.plotly_chart(
        utils.swing_predictor_chart(
            _df_tick_b, swing=int(proposed_pitch), n=n_pred_b,
            result_ranges=result_ranges,
            tick_label=_tick_lbl_b,
            value_col="swing",
            x_label="Swing Values",
            ref_label="Pitch",
        ),
        width='stretch',
    )

    st.markdown("**Optimal Pitch**")
    _recent_b = df_for_pred.sort_values("id").tail(n_pred_b)["swing"].dropna().astype(int).tolist() if not df_for_pred.empty else []
    _delta_b  = utils.project_from_deltas(_recent_b)
    _opt_rows_b = [
        ("Based on Recent Swing Values", _recent_b),
        ("Based on Recent Swing Δ",      _delta_b),
    ]
    col_obp_b, col_slg_b = st.columns(2)
    with col_obp_b:
        st.markdown("**OBP**")
        for _i, (_lbl, _vals) in enumerate(_opt_rows_b):
            st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>", unsafe_allow_html=True)
            if _vals:
                _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "obp", False)
                _pk = f"pill_obp_{_i}_b"
                _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                _sel = st.pills("", list(_opts.keys()), key=_pk)
                if _sel:
                    st.session_state["_pending_pitch_b"] = _opts[_sel]
                    st.session_state.setdefault("_pills_to_reset_b", []).append(_pk)
                    st.rerun()
                st.plotly_chart(
                    utils.optimal_swing_chart(_vals, result_ranges, "obp", False, compact=True),
                    use_container_width=True,
                )
    with col_slg_b:
        st.markdown("**SLG**")
        for _i, (_lbl, _vals) in enumerate(_opt_rows_b):
            st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>", unsafe_allow_html=True)
            if _vals:
                _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "slg", False)
                _pk = f"pill_slg_{_i}_b"
                _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                _sel = st.pills("", list(_opts.keys()), key=_pk)
                if _sel:
                    st.session_state["_pending_pitch_b"] = _opts[_sel]
                    st.session_state.setdefault("_pills_to_reset_b", []).append(_pk)
                    st.rerun()
                st.plotly_chart(
                    utils.optimal_swing_chart(_vals, result_ranges, "slg", False, compact=True),
                    use_container_width=True,
                )
elif pred_mode == "Fetch Live Matchup":
    st.info("Fetch a matchup sheet above to enable the predictor.")

# ------------------------------------------------------------------ last n swings

st.divider()
st.subheader("Last N Swings")
col_ln_n_b, col_ln_off_b = st.columns([3, 1])
with col_ln_n_b:
    n_swings = st.slider("# of at-bats", 5, 50, 20, key="last_n_swing")
with col_ln_off_b:
    swing_off_b = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_b",
                           help="+1: shifts swing markers right by one AB to show if current swing predicts next pitch.")
st.plotly_chart(
    utils.last_n_combined_chart(df_for_pred, n=n_swings, delta_col="swing",
                                title=f"Last {n_swings} Swings",
                                swing_offset=(swing_off_b == "+1")),
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
    group_cols = ["game_id", "batter_name"]
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
    display = df[["season", "game_id", "inning", "outs", "obc",
                  "pitcher_name", "batter_name", "pitch", "swing", "diff", "result", "res_category"]].copy()
    display.columns = ["Season", "Game", "Inn", "Outs", "Runners",
                       "Pitcher", "Batter", "Pitch", "Swing", "Diff", "Result", "Category"]
    st.dataframe(display, use_container_width=True, hide_index=True)

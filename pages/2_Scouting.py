"""Pitcher and Batter scouting — combined page with shared matchup setup."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Scouting")

# ── data ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    raw = db.get_all_plays()
    if not raw:
        return pd.DataFrame()
    return utils.enrich_df(utils.flatten_games(raw))

@st.cache_data(ttl=300)
def _load_all_players() -> list:
    return db.get_all_players()

@st.cache_data(ttl=3600)
def _sheet_name(url: str) -> str:
    return utils.get_sheet_name(url)

df_all = load_data()
if df_all.empty:
    st.info("No at-bats in the database yet.")
    st.stop()

_all_players      = _load_all_players()
_pbyn             = {p["name"]: p for p in _all_players if p.get("name")}
_all_teams        = sorted({utils.TEAM_ABBREV.get(p.get("team",""), p.get("team","?"))
                             for p in _all_players if p.get("team")})
_full_to_abbrev   = {v: k for k, v in utils.TEAM_ABBREV.items()}
_all_player_names = sorted(p["name"] for p in _all_players if p.get("name"))

# ── auto-filters from cross-tab imports ──────────────────────────────────────

if "_auto_pitcher_filter" in st.session_state:
    _apf = st.session_state.pop("_auto_pitcher_filter")
    if _apf.get("team") and _apf["team"] in df_all["def_team"].unique():
        st.session_state["spt_filter"] = _apf["team"]
    if _apf.get("pitcher") and _apf["pitcher"] in df_all["pitcher_name"].unique():
        st.session_state["spitcher_filter"] = _apf["pitcher"]

if "_auto_batter_filter" in st.session_state:
    _abf = st.session_state.pop("_auto_batter_filter")
    if _abf.get("team") and _abf["team"] in df_all["off_team"].unique():
        st.session_state["sbt_filter"] = _abf["team"]
    if _abf.get("batter") and _abf["batter"] in df_all["batter_name"].unique():
        st.session_state["sbatter_filter"] = _abf["batter"]

# ── sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filters")
    seasons = sorted(df_all["season"].dropna().unique(), reverse=True)
    selected_seasons = st.multiselect("Season", seasons, default=seasons)
    games_list = sorted(df_all["game_id"].dropna().unique())
    selected_games = st.multiselect("Games", games_list, default=games_list,
                                    format_func=lambda x: f"Game {int(x)}")
    st.divider()
    st.subheader("Pitcher")
    def_teams = sorted(df_all["def_team"].unique())
    selected_pt = st.selectbox("Team", ["All"] + def_teams, key="spt_filter")
    if selected_pt != "All":
        pitcher_names = sorted(df_all[df_all["def_team"] == selected_pt]["pitcher_name"].unique())
    else:
        pitcher_names = sorted(df_all["pitcher_name"].unique())
    selected_pitcher = st.selectbox("Pitcher", ["All"] + pitcher_names, key="spitcher_filter")
    st.divider()
    st.subheader("Batter")
    off_teams = sorted(df_all["off_team"].unique())
    selected_bt = st.selectbox("Team", ["All"] + off_teams, key="sbt_filter")
    if selected_bt != "All":
        batter_names = sorted(df_all[df_all["off_team"] == selected_bt]["batter_name"].unique())
    else:
        batter_names = sorted(df_all["batter_name"].unique())
    selected_batter = st.selectbox("Batter", ["All"] + batter_names, key="sbatter_filter")
    if selected_batter != "All":
        batter_scope = st.radio("Scope", ["Solo", "Full Team"], horizontal=True, key="batter_scope",
                                help="Solo: only this batter. Full Team: all ABs from their team.")
    else:
        batter_scope = "Solo"

# ── filtered DataFrames ───────────────────────────────────────────────────────

_base = df_all.copy()
if selected_seasons:
    _base = _base[_base["season"].isin(selected_seasons)]
if selected_games:
    _base = _base[_base["game_id"].isin(selected_games)]

df_p = _base.copy()
if selected_pt != "All":
    df_p = df_p[df_p["def_team"] == selected_pt]
if selected_pitcher != "All":
    df_p = df_p[df_p["pitcher_name"] == selected_pitcher]

df_b = _base.copy()
if selected_bt != "All":
    df_b = df_b[df_b["off_team"] == selected_bt]
if selected_batter != "All" and batter_scope == "Solo":
    df_b = df_b[df_b["batter_name"] == selected_batter]

# ── shared helpers ────────────────────────────────────────────────────────────

def _players_for_team(team_full: str) -> list[str]:
    if team_full == "All":
        return _all_player_names
    abbrev = _full_to_abbrev.get(team_full, team_full)
    return sorted(p["name"] for p in _all_players if p.get("name") and p.get("team") == abbrev)

def _stat(p: dict, key: str, default: int = 3) -> int:
    v = p.get(key)
    return int(v) if v is not None else default

def _hand(p: dict) -> str:
    h = str(p.get("hand", "R")).upper()
    return h if h in ("L", "R", "S") else "R"

_hand_opts    = ["R", "L", "S"]
_runner_opts  = ["Empty"] + _all_player_names
_OBC_BASES    = {"Empty":[],"1B":[1],"2B":[2],"3B":[3],"1&2B":[1,2],"1&3B":[1,3],"2&3B":[2,3],"BL":[1,2,3]}

# ── session-state defaults ────────────────────────────────────────────────────

_DEFS = {
    "pred_calc_p_team":"All","pred_calc_p_name":"-- Manual --",
    "pred_calc_p_hand":"R","pred_calc_p_mov":3,"pred_calc_p_cmd":3,"pred_calc_p_vel":3,"pred_calc_p_awr":3,
    "pred_calc_b_team":"All","pred_calc_b_name":"-- Manual --",
    "pred_calc_b_hand":"R","pred_calc_b_con":3,"pred_calc_b_eye":3,"pred_calc_b_pow":3,"pred_calc_b_spd":3,
    "pred_calc_outs":0,"pred_calc_bunt":False,"pred_calc_hnr":False,
    "pred_calc_1b":"Empty","pred_calc_2b":"Empty","pred_calc_3b":"Empty",
}
for _k, _v in _DEFS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── auto-sync sidebar → calculator ───────────────────────────────────────────

_pitcher_rec = _pbyn.get(selected_pitcher, {}) if selected_pitcher != "All" else {}
if selected_pitcher != "All" and st.session_state.get("_loaded_pitcher") != selected_pitcher:
    st.session_state["_loaded_pitcher"] = selected_pitcher
    st.session_state.update({
        "pred_calc_p_hand": _hand(_pitcher_rec),
        "pred_calc_p_mov": _stat(_pitcher_rec,"mov"), "pred_calc_p_cmd": _stat(_pitcher_rec,"cmd"),
        "pred_calc_p_vel": _stat(_pitcher_rec,"vel"), "pred_calc_p_awr": _stat(_pitcher_rec,"awr"),
    })

_batter_rec = _pbyn.get(selected_batter, {}) if selected_batter != "All" else {}
if selected_batter != "All" and st.session_state.get("_loaded_batter") != selected_batter:
    st.session_state["_loaded_batter"] = selected_batter
    st.session_state.update({
        "pred_calc_b_hand": _hand(_batter_rec),
        "pred_calc_b_con": _stat(_batter_rec,"con"), "pred_calc_b_eye": _stat(_batter_rec,"eye"),
        "pred_calc_b_pow": _stat(_batter_rec,"pow"), "pred_calc_b_spd": _stat(_batter_rec,"spd"),
    })

# ── callbacks ─────────────────────────────────────────────────────────────────

def _on_p_team():
    st.session_state["pred_calc_p_name"] = "-- Manual --"
    for _k, _v in [("pred_calc_p_hand","R"),("pred_calc_p_mov",3),("pred_calc_p_cmd",3),
                   ("pred_calc_p_vel",3),("pred_calc_p_awr",3)]:
        st.session_state[_k] = _v

def _on_pitcher():
    n = st.session_state.get("pred_calc_p_name","-- Manual --")
    if n != "-- Manual --" and n in _pbyn:
        pp = _pbyn[n]
        st.session_state.update({
            "pred_calc_p_hand": _hand(pp),
            "pred_calc_p_mov": _stat(pp,"mov"), "pred_calc_p_cmd": _stat(pp,"cmd"),
            "pred_calc_p_vel": _stat(pp,"vel"), "pred_calc_p_awr": _stat(pp,"awr"),
        })

def _on_b_team():
    st.session_state["pred_calc_b_name"] = "-- Manual --"
    for _k, _v in [("pred_calc_b_hand","R"),("pred_calc_b_con",3),("pred_calc_b_eye",3),
                   ("pred_calc_b_pow",3),("pred_calc_b_spd",3)]:
        st.session_state[_k] = _v

def _on_batter():
    n = st.session_state.get("pred_calc_b_name","-- Manual --")
    if n != "-- Manual --" and n in _pbyn:
        bp = _pbyn[n]
        st.session_state.update({
            "pred_calc_b_hand": _hand(bp),
            "pred_calc_b_con": _stat(bp,"con"), "pred_calc_b_eye": _stat(bp,"eye"),
            "pred_calc_b_pow": _stat(bp,"pow"), "pred_calc_b_spd": _stat(bp,"spd"),
        })

# ── import helper ─────────────────────────────────────────────────────────────

def _import_play(play_id: int, src_df: pd.DataFrame, fill_side: str):
    row = src_df[src_df["id"] == play_id]
    if row.empty:
        return
    r = row.iloc[0]
    st.session_state["pred_calc_outs"] = int(r.get("outs",0)) if pd.notna(r.get("outs")) else 0
    for _b in [1, 2, 3]:
        st.session_state[f"pred_calc_{_b}b"] = "Empty"
    if fill_side == "batter":
        b_name = r.get("batter_name","")
        bp = _pbyn.get(b_name, {})
        b_tf = utils.TEAM_ABBREV.get(bp.get("team",""), "All")
        if b_tf in _all_teams:
            st.session_state["pred_calc_b_team"] = b_tf
        st.session_state["pred_calc_b_name"] = b_name if b_name in _pbyn else "-- Manual --"
        st.session_state["pred_calc_b_hand"] = _hand(bp) if bp else "R"
        st.session_state["pred_calc_b_con"]  = _stat(bp,"con") if bp else 3
        st.session_state["pred_calc_b_eye"]  = _stat(bp,"eye") if bp else 3
        st.session_state["pred_calc_b_pow"]  = _stat(bp,"pow") if bp else 3
        st.session_state["pred_calc_b_spd"]  = _stat(bp,"spd") if bp else 3
        if b_name in _pbyn:
            st.session_state["_auto_batter_filter"] = {
                "team": utils.TEAM_ABBREV.get(_pbyn[b_name].get("team",""), "All"),
                "batter": b_name,
            }
    else:
        p_name = r.get("pitcher_name","")
        pp = _pbyn.get(p_name, {})
        p_tf = utils.TEAM_ABBREV.get(pp.get("team",""), "All")
        if p_tf in _all_teams:
            st.session_state["pred_calc_p_team"] = p_tf
        st.session_state["pred_calc_p_name"] = p_name if p_name in _pbyn else "-- Manual --"
        st.session_state["pred_calc_p_hand"] = _hand(pp) if pp else "R"
        st.session_state["pred_calc_p_mov"]  = _stat(pp,"mov") if pp else 3
        st.session_state["pred_calc_p_cmd"]  = _stat(pp,"cmd") if pp else 3
        st.session_state["pred_calc_p_vel"]  = _stat(pp,"vel") if pp else 3
        st.session_state["pred_calc_p_awr"]  = _stat(pp,"awr") if pp else 3
        if p_name in _pbyn:
            st.session_state["_auto_pitcher_filter"] = {
                "team": utils.TEAM_ABBREV.get(_pbyn[p_name].get("team",""), "All"),
                "pitcher": p_name,
            }

# ── shared calculator widget block ────────────────────────────────────────────

def _render_calc_inputs():
    col_p, col_b = st.columns(2)
    with col_p:
        p_lbl = "**Pitcher**" + (f" · *{selected_pitcher}*" if selected_pitcher != "All" else "")
        st.markdown(p_lbl)
        _p_team_opts = ["All"] + _all_teams
        if st.session_state.get("pred_calc_p_team") not in _p_team_opts:
            st.session_state["pred_calc_p_team"] = "All"
        _p_team = st.selectbox("Team", _p_team_opts, key="pred_calc_p_team", on_change=_on_p_team)
        _p_name_opts = ["-- Manual --"] + _players_for_team(_p_team)
        if st.session_state.get("pred_calc_p_name") not in _p_name_opts:
            st.session_state["pred_calc_p_name"] = "-- Manual --"
        st.selectbox("Pitcher", _p_name_opts, key="pred_calc_p_name", on_change=_on_pitcher)
        if st.session_state.get("pred_calc_p_hand") not in _hand_opts:
            st.session_state["pred_calc_p_hand"] = "R"
        st.selectbox("Hand", _hand_opts, key="pred_calc_p_hand")
        st.number_input("MOV", min_value=1, max_value=5, key="pred_calc_p_mov")
        st.number_input("CMD", min_value=1, max_value=5, key="pred_calc_p_cmd")
        st.number_input("VEL", min_value=1, max_value=5, key="pred_calc_p_vel")
        st.number_input("AWR", min_value=1, max_value=5, key="pred_calc_p_awr")
    with col_b:
        b_lbl = "**Batter**" + (f" · *{selected_batter}*" if selected_batter != "All" else "")
        st.markdown(b_lbl)
        _b_team_opts = ["All"] + _all_teams
        if st.session_state.get("pred_calc_b_team") not in _b_team_opts:
            st.session_state["pred_calc_b_team"] = "All"
        _b_team = st.selectbox("Team ", _b_team_opts, key="pred_calc_b_team", on_change=_on_b_team)
        _b_name_opts = ["-- Manual --"] + _players_for_team(_b_team)
        if st.session_state.get("pred_calc_b_name") not in _b_name_opts:
            st.session_state["pred_calc_b_name"] = "-- Manual --"
        st.selectbox("Batter", _b_name_opts, key="pred_calc_b_name", on_change=_on_batter)
        if st.session_state.get("pred_calc_b_hand") not in _hand_opts:
            st.session_state["pred_calc_b_hand"] = "R"
        st.selectbox("Hand ", _hand_opts, key="pred_calc_b_hand")
        st.number_input("CON", min_value=1, max_value=5, key="pred_calc_b_con")
        st.number_input("EYE", min_value=1, max_value=5, key="pred_calc_b_eye")
        st.number_input("POW", min_value=1, max_value=5, key="pred_calc_b_pow")
        st.number_input("SPD", min_value=1, max_value=5, key="pred_calc_b_spd")

    st.divider()
    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        if st.session_state.get("pred_calc_outs") not in [0, 1, 2]:
            st.session_state["pred_calc_outs"] = 0
        st.radio("Outs", [0, 1, 2], horizontal=True, key="pred_calc_outs")
    with col_s2:
        st.checkbox("Bunting?", key="pred_calc_bunt")
    with col_s3:
        st.checkbox("Hit & Run?", key="pred_calc_hnr")

    st.markdown("**Baserunners**")
    col_1b, col_2b, col_3b = st.columns(3)
    with col_1b:
        if st.session_state["pred_calc_1b"] not in _runner_opts:
            st.session_state["pred_calc_1b"] = "Empty"
        _r1 = st.selectbox("1B", _runner_opts, key="pred_calc_1b")
        if _r1 != "Empty" and _r1 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r1],'spd')}")
    with col_2b:
        if st.session_state["pred_calc_2b"] not in _runner_opts:
            st.session_state["pred_calc_2b"] = "Empty"
        _r2 = st.selectbox("2B", _runner_opts, key="pred_calc_2b")
        if _r2 != "Empty" and _r2 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r2],'spd')}")
    with col_3b:
        if st.session_state["pred_calc_3b"] not in _runner_opts:
            st.session_state["pred_calc_3b"] = "Empty"
        _r3 = st.selectbox("3B", _runner_opts, key="pred_calc_3b")
        if _r3 != "Empty" and _r3 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r3],'spd')}")

def _calc_ranges() -> list:
    _runners = any(st.session_state.get(f"pred_calc_{b}b","Empty") != "Empty" for b in [1,2,3])
    return utils.compute_at_bat_ranges(
        pitcher_hand=st.session_state.get("pred_calc_p_hand","R"),
        pitcher_mov=int(st.session_state.get("pred_calc_p_mov",3)),
        pitcher_cmd=int(st.session_state.get("pred_calc_p_cmd",3)),
        pitcher_vel=int(st.session_state.get("pred_calc_p_vel",3)),
        pitcher_awr=int(st.session_state.get("pred_calc_p_awr",3)),
        batter_hand=st.session_state.get("pred_calc_b_hand","R"),
        batter_con=int(st.session_state.get("pred_calc_b_con",3)),
        batter_eye=int(st.session_state.get("pred_calc_b_eye",3)),
        batter_pow=int(st.session_state.get("pred_calc_b_pow",3)),
        batter_spd=int(st.session_state.get("pred_calc_b_spd",3)),
        bunt=bool(st.session_state.get("pred_calc_bunt",False)),
        hit_and_run=bool(st.session_state.get("pred_calc_hnr",False)),
        outs=int(st.session_state.get("pred_calc_outs",0)),
        runners_on=_runners,
    )

# ── play picker helper (used inside each tab) ─────────────────────────────────

def _play_picker(src_df: pd.DataFrame, season_key: str, game_key: str, play_key: str):
    """Render season/game/play selectors and return the selected play ID (or None)."""
    if src_df.empty:
        st.caption("No plays available for current filter.")
        return None
    col_hs, col_hg, col_hp = st.columns([1, 2, 3])
    with col_hs:
        _h_seasons = sorted(src_df["season"].dropna().unique(), reverse=True)
        _h_season  = st.selectbox("Season", _h_seasons, key=season_key)
    _df_hs = src_df[src_df["season"] == _h_season]
    with col_hg:
        _h_games = sorted(_df_hs["game_id"].dropna().unique())
        _h_game  = st.selectbox("Game", _h_games, format_func=lambda x: f"Game {int(x)}", key=game_key)
    _df_hg = _df_hs[_df_hs["game_id"] == _h_game].sort_values("id")
    with col_hp:
        _h_po = _df_hg[["id","inning","half","outs","obc","pitcher_name","batter_name","pitch","swing","result"]].copy()
        _h_po["label"] = _h_po.apply(
            lambda r: (
                f"#{int(r['id'])}  Inn {int(r['inning'])}{r['half'][0].upper()}  "
                f"{r['outs']}out  {r['obc']}  {r['pitcher_name']} vs {r['batter_name']}  "
                f"P:{r['pitch']} S:{r['swing']} → {r['result']}"
            ), axis=1,
        )
        _h_ids  = _h_po["id"].tolist()
        _h_play = st.selectbox(
            "Play", [None] + _h_ids,
            format_func=lambda x: "-- None --" if x is None
                else _h_po.loc[_h_po["id"] == x, "label"].iloc[0],
            key=play_key,
        )
    return _h_play

# ── matchup source ────────────────────────────────────────────────────────────

st.subheader("Matchup Setup")
pred_mode = st.radio("Source", ["Fetch Live Matchup", "Historical / Manual"],
                     horizontal=True, key="pred_mode")

result_ranges = None
hist_id       = st.session_state.get("pred_hist_loaded_id") if pred_mode == "Historical / Manual" else None
matchup_label = ""

if pred_mode == "Fetch Live Matchup":
    with st.expander("Live Matchup", expanded=True):
        all_games_sw = db.get_games()
        games_by_id  = {g["id"]: g for g in all_games_sw}
        sheet_urls   = list(dict.fromkeys(
            g["sheet_url"] for gid in selected_games
            if (g := games_by_id.get(gid)) and g.get("sheet_url")
        ))
        col_sh, col_btn = st.columns([3, 1])
        with col_sh:
            if sheet_urls:
                pred_sheet_url = sheet_urls[0] if len(sheet_urls) == 1 else st.selectbox(
                    "Session sheet", sheet_urls, key="pred_sheet_sel",
                    format_func=_sheet_name,
                )
                st.caption(f"Linked: {_sheet_name(pred_sheet_url)}")
            else:
                st.caption("No sheet linked to selected session(s).")
                pred_sheet_url = None
        with col_btn:
            st.write("")
            if sheet_urls and st.button("Fetch Matchup", type="secondary", key="pull_ranges"):
                try:
                    fetched_ranges, fetched_batter, fetched_pitcher = utils.parse_result_ranges_from_sheet(pred_sheet_url)
                    st.session_state["pred_result_ranges"] = fetched_ranges
                    st.session_state["pred_sheet_batter"]  = fetched_batter
                    st.session_state["pred_sheet_pitcher"] = fetched_pitcher
                    for _fname, _fkey, _fteam_key, _ffilter_key in [
                        (fetched_pitcher, "pitcher", "spt_filter", "_auto_pitcher_filter"),
                        (fetched_batter,  "batter",  "sbt_filter", "_auto_batter_filter"),
                    ]:
                        if _fname:
                            _fp = next((p for p in _all_players if p["name"] == _fname), None)
                            _ta = _fp.get("team") if _fp else None
                            st.session_state[_ffilter_key] = {
                                "team": utils.TEAM_ABBREV.get(_ta, _ta), _fkey: _fname,
                            }
                    st.toast(f"Loaded {len(fetched_ranges)} ranges.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    result_ranges = st.session_state.get("pred_result_ranges")
    _sb = st.session_state.get("pred_sheet_batter", "")
    _sp = st.session_state.get("pred_sheet_pitcher", "")
    matchup_label = " vs ".join(filter(None, [_sp, _sb]))

elif pred_mode == "Historical / Manual":
    with st.expander("Matchup Setup", expanded=True):
        _render_calc_inputs()
    _calc_r       = _calc_ranges()
    result_ranges = [(r["result"], r["low"], r["high"]) for r in _calc_r]
    _pn = st.session_state.get("pred_calc_p_name", "")
    _bn = st.session_state.get("pred_calc_b_name", "")
    matchup_label = " vs ".join(p for p in [_pn, _bn] if p and p != "-- Manual --")

# ── ITD slices (shared by both tabs) ──────────────────────────────────────────

df_p_pred = df_p[df_p["id"] < hist_id] if hist_id else df_p
df_b_pred = df_b[df_b["id"] < hist_id] if hist_id else df_b

if matchup_label:
    st.caption(f"Matchup: **{matchup_label}**")

st.divider()

# ── tabs ──────────────────────────────────────────────────────────────────────

tab_p, tab_b = st.tabs(["⚾ Pitcher", "🦇 Batter"])

# ══════════════════════════════════════════════════════════════════════════════
# PITCHER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_p:
    if df_p.empty:
        st.warning("No at-bats match the current pitcher filter.")
    else:
        # ── summary metrics ───────────────────────────────────────────────────
        _p_avg  = df_p_pred["diff"].mean() if not df_p_pred.empty else float("nan")
        _p_meme = df_p_pred["is_meme_pitch"].mean() * 100 if not df_p_pred.empty else 0.0
        _pc1, _pc2, _pc3 = st.columns(3)
        _pc1.metric("At-Bats", len(df_p_pred))
        _pc2.metric("Avg Diff", f"{_p_avg:.1f}" if not pd.isna(_p_avg) else "—")
        _pc3.metric("Meme Rate", f"{_p_meme:.1f}%")

        st.divider()

        # ── optional play import (Historical/Manual mode) ─────────────────────
        if pred_mode == "Historical / Manual":
            with st.expander("Import from Pitcher History", expanded=False):
                st.caption("Import a play to set the ITD cutoff and pre-fill batter stats.")
                _h_play_p = _play_picker(df_p, "pred_hist_season_p", "pred_hist_game_p", "pred_hist_play_p")
                col_hi_p, col_hinfo_p = st.columns([1, 5])
                with col_hi_p:
                    if _h_play_p is not None and st.button("Import Play", type="primary",
                                                           use_container_width=True, key="pred_hist_import_p"):
                        _import_play(int(_h_play_p), df_p, fill_side="batter")
                        st.session_state["pred_hist_loaded_id"] = int(_h_play_p)
                        st.rerun()
                with col_hinfo_p:
                    if _h_play_p is not None:
                        _hpr_p = df_p[df_p["id"] == _h_play_p]
                        if not _hpr_p.empty:
                            _hr = _hpr_p.iloc[0]
                            if pd.notna(_hr.get("pitch")) and pd.notna(_hr.get("swing")):
                                _hd = utils.circular_diff(int(_hr["pitch"]), int(_hr["swing"]))
                                st.caption(
                                    f"P:{int(_hr['pitch'])}  S:{int(_hr['swing'])}  "
                                    f"Diff:{_hd}  → **{_hr.get('result','')}**"
                                )
                if hist_id and result_ranges:
                    _hpr_row = df_p[df_p["id"] == hist_id]
                    if not _hpr_row.empty:
                        _hpr2 = _hpr_row.iloc[0]
                        if pd.notna(_hpr2.get("pitch")) and pd.notna(_hpr2.get("swing")):
                            _had2   = utils.circular_diff(int(_hpr2["pitch"]), int(_hpr2["swing"]))
                            _calc_r2 = _calc_ranges()
                            _hmatch = next((r for r in _calc_r2 if r["low"] <= _had2 <= r["high"]), None)
                            _hexp   = _hmatch["result"] if _hmatch else "?"
                            _hicon  = "✓" if _hexp == _hpr2.get("result","") else "≠"
                            st.caption(
                                f"Loaded play #{hist_id} · Diff **{_had2}** → "
                                f"calc expects **{_hexp}** {_hicon} actual **{_hpr2.get('result','')}**"
                            )

        # ── swing predictor ───────────────────────────────────────────────────
        st.subheader("Swing Predictor")
        st.caption("Enter a proposed swing to see what each of this pitcher's recent pitches would give.")

        if result_ranges:
            if "_pend_swing" in st.session_state:
                st.session_state["pred_swing"] = st.session_state.pop("_pend_swing")
            for _pk in st.session_state.pop("_pills_rst_p", []):
                st.session_state[_pk] = None

            col_sw, col_n = st.columns([3, 1])
            with col_sw:
                if "pred_swing" not in st.session_state:
                    st.session_state["pred_swing"] = 500
                proposed_swing = st.number_input("Proposed Swing", min_value=1, max_value=1000,
                                                 step=1, key="pred_swing")
            with col_n:
                n_pred_p = st.slider("# pitches", 5, 50, 20, key="pred_n_p")

            _df_tick_p = df_p_pred if not df_p_pred.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_p = f"Last {n_pred_p} pitches (pre-AB)" if hist_id and not df_p_pred.empty \
                          else f"Last {n_pred_p} pitches"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_p, swing=int(proposed_swing), n=n_pred_p,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_p),
                width="stretch",
            )

            st.markdown("**Optimal Swing**")
            _recent_p = df_p_pred.sort_values("id").tail(n_pred_p)["pitch"].dropna().astype(int).tolist() \
                        if not df_p_pred.empty else []
            _delta_p  = utils.project_from_deltas(_recent_p)
            _opt_rows_p = [("Based on Recent Pitch Values", _recent_p), ("Based on Recent Pitch Δ", _delta_p)]
            col_obp_p, col_slg_p = st.columns(2)
            with col_obp_p:
                st.markdown("**OBP**")
                for _i, (_lbl, _vals) in enumerate(_opt_rows_p):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "obp", True)
                        _pk = f"pill_obp_{_i}_p"
                        _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_swing"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_p", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(utils.optimal_swing_chart(_vals, result_ranges, "obp", True, compact=True),
                                        use_container_width=True)
            with col_slg_p:
                st.markdown("**SLG**")
                for _i, (_lbl, _vals) in enumerate(_opt_rows_p):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "slg", True)
                        _pk = f"pill_slg_{_i}_p"
                        _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_swing"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_p", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(utils.optimal_swing_chart(_vals, result_ranges, "slg", True, compact=True),
                                        use_container_width=True)
        else:
            if pred_mode == "Fetch Live Matchup":
                st.info("Fetch a matchup sheet above to enable the predictor.")

        # ── last N pitches ────────────────────────────────────────────────────
        st.divider()
        st.subheader("Last N Pitches")
        col_ln_p, col_lo_p = st.columns([3, 1])
        with col_ln_p:
            n_pitches = st.slider("# of at-bats", 5, 50, 20, key="last_n_pitch")
        with col_lo_p:
            swing_off_p = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_p",
                                   help="+1: shifts swing markers right by one AB.")
        st.plotly_chart(
            utils.last_n_combined_chart(df_p_pred, n=n_pitches, delta_col="pitch",
                                        title=f"Last {n_pitches} Pitches",
                                        swing_offset=(swing_off_p == "+1")),
            width="stretch",
        )

        # rebind so all sections below are ITD
        df_p = df_p_pred
        _p_total = len(df_p)

        # ── hot zone ─────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Hot Zone Pitch Matrix")
        st.caption("How often each pitch range is followed by each other pitch range.")
        bucket_p = st.select_slider("Bucket size", options=[50,100,125,200,250,500], value=100, key="hz_bucket_p")
        group_cols_p = ["game_id","pitcher_name"] if selected_pitcher != "All" else ["pitcher_name"]
        st.plotly_chart(utils.hot_zone_matrix(df_p, value_col="pitch",
                                              group_cols=group_cols_p, bucket_size=bucket_p), width="stretch")

        # ── zone distribution ─────────────────────────────────────────────────
        st.divider()
        st.subheader("Zone Distribution (All)")
        st.plotly_chart(utils.zone_heatmap(df_p["pitch_zone"].value_counts().to_dict(),
                                           title="Pitch Zone Frequency"), width="stretch")

        # ── first pitch tendencies ────────────────────────────────────────────
        st.subheader("First Pitch Tendencies")
        col_a_p, col_b_p = st.columns(2)
        with col_a_p:
            _fpa = df_p[df_p["is_fp_app"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpa["pitch_zone"].value_counts().to_dict() if not _fpa.empty else {},
                                               title=f"First Pitch of Appearance (n={len(_fpa)})"),
                            width="stretch", config={"displayModeBar": False})
        with col_b_p:
            _fpi = df_p[df_p["is_fp_inn"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpi["pitch_zone"].value_counts().to_dict() if not _fpi.empty else {},
                                               title=f"First Pitch of Inning (n={len(_fpi)})"),
                            width="stretch", config={"displayModeBar": False})

        # ── zone by out count ─────────────────────────────────────────────────
        st.subheader("Zone by Out Count")
        _cols_p = st.columns(3)
        for _i, _oc in enumerate([0, 1, 2]):
            _dfo = df_p[df_p["outs"] == _oc]
            with _cols_p[_i]:
                st.plotly_chart(utils.zone_heatmap(_dfo["pitch_zone"].value_counts().to_dict() if not _dfo.empty else {},
                                                   title=f"{_oc} Outs (n={len(_dfo)})"), width="stretch")

        # ── zone by base state ────────────────────────────────────────────────
        st.subheader("Zone by Base State")
        _col_e_p, _col_r_p = st.columns(2)
        for _col, (_lbl, _obc_vals) in zip([_col_e_p, _col_r_p], [
            ("Empty", ["Empty"]),
            ("Runner(s) On", ["1B","2B","3B","1&2B","1&3B","2&3B","BL"]),
        ]):
            _df_obc = df_p[df_p["obc"].isin(_obc_vals)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_obc["pitch_zone"].value_counts().to_dict() if not _df_obc.empty else {},
                                                   title=f"{_lbl} (n={len(_df_obc)})"), width="stretch")

        # ── delta ─────────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Pitch Delta (Change from Previous AB)")
        _deltas_p = df_p["pitch_circ_delta"].dropna()
        if not _deltas_p.empty:
            st.plotly_chart(utils.delta_histogram(_deltas_p), width="stretch")
            _dc1, _dc2, _dc3 = st.columns(3)
            _dc1.metric("Avg Delta", f"{_deltas_p.mean():+.1f}")
            _dc2.metric("Avg |Delta|", f"{_deltas_p.abs().mean():.1f}")
            _dc3.metric("# with Delta", len(_deltas_p))
        else:
            st.caption("Need at least 2 at-bats from the same pitcher in a session.")

        # ── tendencies ────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Tendencies")
        _tm_p, _tl_p = st.columns(2)
        with _tm_p:
            st.markdown("**Meme Pitches (42, 69, 420)**")
            _mc = {str(n): int((df_p["pitch"] == n).sum()) for n in utils.MEME_NUMBERS}
            _mt = sum(_mc.values())
            st.metric("Meme Pitches", _mt, help=f"{_mt / _p_total * 100:.1f}% of all pitches" if _p_total else "")
            for _num, _cnt in _mc.items():
                st.write(f"  **{_num}**: {_cnt} ({_cnt / _p_total * 100:.1f}%)" if _p_total else f"  **{_num}**: {_cnt}")
        with _tl_p:
            st.markdown("**Most Common Last 2 Digits**")
            _last2_p = df_p["pitch_last2"].value_counts().head(5)
            for _dig, _cnt in _last2_p.items():
                st.write(f"  **{_dig:02d}**: {_cnt} ({_cnt / _p_total * 100:.1f}%)" if _p_total else f"  **{_dig:02d}**: {_cnt}")

        # ── results ───────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Results Allowed")
        st.plotly_chart(utils.result_bar(df_p["result"].value_counts().to_dict(),
                                         title="Result Distribution"), width="stretch")
        st.plotly_chart(utils.result_bar(df_p["res_category"].value_counts().to_dict(),
                                         title="Result Category"), width="stretch")

        # ── raw data ──────────────────────────────────────────────────────────
        with st.expander("Raw At-Bat Data"):
            _disp_p = df_p[["season","game_id","inning","outs","obc","pitcher_name","batter_name",
                             "pitch","swing","diff","result","res_category"]].copy()
            _disp_p.columns = ["Season","Game","Inn","Outs","Runners","Pitcher","Batter",
                                "Pitch","Swing","Diff","Result","Category"]
            st.dataframe(_disp_p, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# BATTER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_b:
    if df_b.empty:
        st.warning("No at-bats match the current batter filter.")
    else:
        # ── summary metrics ───────────────────────────────────────────────────
        _b_avg  = df_b_pred["diff"].mean() if not df_b_pred.empty else float("nan")
        _b_obp  = df_b_pred["res_category"].isin(["XBH","BB/1B"]).mean() * 100 if not df_b_pred.empty else 0.0
        _b_xbh  = (df_b_pred["res_category"] == "XBH").mean() * 100 if not df_b_pred.empty else 0.0
        _bc1, _bc2, _bc3, _bc4 = st.columns(4)
        _bc1.metric("At-Bats", len(df_b_pred))
        _bc2.metric("Avg Diff", f"{_b_avg:.1f}" if not pd.isna(_b_avg) else "—")
        _bc3.metric("OB%", f"{_b_obp:.1f}%")
        _bc4.metric("XBH%", f"{_b_xbh:.1f}%")

        st.divider()

        # ── optional play import ──────────────────────────────────────────────
        if pred_mode == "Historical / Manual":
            with st.expander("Import from Batter History", expanded=False):
                st.caption("Import a play to set the ITD cutoff and pre-fill pitcher stats.")
                _h_play_b = _play_picker(df_b, "pred_hist_season_b", "pred_hist_game_b", "pred_hist_play_b")
                col_hi_b, col_hinfo_b = st.columns([1, 5])
                with col_hi_b:
                    if _h_play_b is not None and st.button("Import Play", type="primary",
                                                           use_container_width=True, key="pred_hist_import_b"):
                        _import_play(int(_h_play_b), df_b, fill_side="pitcher")
                        st.session_state["pred_hist_loaded_id"] = int(_h_play_b)
                        st.rerun()
                with col_hinfo_b:
                    if _h_play_b is not None:
                        _hpr_b = df_b[df_b["id"] == _h_play_b]
                        if not _hpr_b.empty:
                            _hrb = _hpr_b.iloc[0]
                            if pd.notna(_hrb.get("pitch")) and pd.notna(_hrb.get("swing")):
                                _hdb = utils.circular_diff(int(_hrb["pitch"]), int(_hrb["swing"]))
                                st.caption(
                                    f"P:{int(_hrb['pitch'])}  S:{int(_hrb['swing'])}  "
                                    f"Diff:{_hdb}  → **{_hrb.get('result','')}**"
                                )
                if hist_id and result_ranges:
                    _hpr_row_b = df_b[df_b["id"] == hist_id]
                    if not _hpr_row_b.empty:
                        _hpr2b = _hpr_row_b.iloc[0]
                        if pd.notna(_hpr2b.get("pitch")) and pd.notna(_hpr2b.get("swing")):
                            _had2b   = utils.circular_diff(int(_hpr2b["pitch"]), int(_hpr2b["swing"]))
                            _calc_r2b = _calc_ranges()
                            _hmatchb  = next((r for r in _calc_r2b if r["low"] <= _had2b <= r["high"]), None)
                            _hexpb    = _hmatchb["result"] if _hmatchb else "?"
                            _hiconb   = "✓" if _hexpb == _hpr2b.get("result","") else "≠"
                            st.caption(
                                f"Loaded play #{hist_id} · Diff **{_had2b}** → "
                                f"calc expects **{_hexpb}** {_hiconb} actual **{_hpr2b.get('result','')}**"
                            )

        # ── pitch predictor ───────────────────────────────────────────────────
        st.subheader("Pitch Predictor")
        st.caption("Enter a proposed pitch to see what each of this batter's recent swings would give.")

        if result_ranges:
            if "_pend_pitch" in st.session_state:
                st.session_state["pred_pitch"] = st.session_state.pop("_pend_pitch")
            for _pk in st.session_state.pop("_pills_rst_b", []):
                st.session_state[_pk] = None

            col_pt, col_nb = st.columns([3, 1])
            with col_pt:
                if "pred_pitch" not in st.session_state:
                    st.session_state["pred_pitch"] = 500
                proposed_pitch = st.number_input("Proposed Pitch", min_value=1, max_value=1000,
                                                 step=1, key="pred_pitch")
            with col_nb:
                n_pred_b = st.slider("# swings", 5, 50, 20, key="pred_n_b")

            _df_tick_b = df_b_pred if not df_b_pred.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_b = f"Last {n_pred_b} swings (pre-AB)" if hist_id and not df_b_pred.empty \
                          else f"Last {n_pred_b} swings"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_b, swing=int(proposed_pitch), n=n_pred_b,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_b,
                                            value_col="swing", x_label="Swing Values", ref_label="Pitch"),
                width="stretch",
            )

            st.markdown("**Optimal Pitch**")
            _recent_b = df_b_pred.sort_values("id").tail(n_pred_b)["swing"].dropna().astype(int).tolist() \
                        if not df_b_pred.empty else []
            _delta_b  = utils.project_from_deltas(_recent_b)
            _opt_rows_b = [("Based on Recent Swing Values", _recent_b), ("Based on Recent Swing Δ", _delta_b)]
            col_obp_b, col_slg_b = st.columns(2)
            with col_obp_b:
                st.markdown("**OBP**")
                for _i, (_lbl, _vals) in enumerate(_opt_rows_b):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "obp", False)
                        _pk = f"pill_obp_{_i}_b"
                        _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_pitch"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_b", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(utils.optimal_swing_chart(_vals, result_ranges, "obp", False, compact=True),
                                        use_container_width=True)
            with col_slg_b:
                st.markdown("**SLG**")
                for _i, (_lbl, _vals) in enumerate(_opt_rows_b):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-0.75rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "slg", False)
                        _pk = f"pill_slg_{_i}_b"
                        _opts = {f"↑ {_bv} ({_bs:.3f})": _bv, f"↓ {_cv} ({_cs:.3f})": _cv}
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_pitch"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_b", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(utils.optimal_swing_chart(_vals, result_ranges, "slg", False, compact=True),
                                        use_container_width=True)
        else:
            if pred_mode == "Fetch Live Matchup":
                st.info("Fetch a matchup sheet above to enable the predictor.")

        # ── last N swings ─────────────────────────────────────────────────────
        st.divider()
        st.subheader("Last N Swings")
        col_ln_b, col_lo_b = st.columns([3, 1])
        with col_ln_b:
            n_swings = st.slider("# of at-bats", 5, 50, 20, key="last_n_swing")
        with col_lo_b:
            swing_off_b = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_b",
                                   help="+1: shifts swing markers right by one AB.")
        st.plotly_chart(
            utils.last_n_combined_chart(df_b_pred, n=n_swings, delta_col="swing",
                                        title=f"Last {n_swings} Swings",
                                        swing_offset=(swing_off_b == "+1")),
            width="stretch",
        )

        # rebind so all sections below are ITD
        df_b = df_b_pred
        _b_total = len(df_b)

        # ── hot zone ─────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Hot Zone Swing Matrix")
        st.caption("How often each swing range is followed by each other swing range.")
        bucket_b = st.select_slider("Bucket size", options=[50,100,125,200,250,500], value=100, key="hz_bucket_b")
        group_cols_b = ["game_id","batter_name"] if selected_batter != "All" else ["batter_name"]
        st.plotly_chart(utils.hot_zone_matrix(df_b, value_col="swing",
                                              group_cols=group_cols_b, bucket_size=bucket_b), width="stretch")

        # ── zone distribution ─────────────────────────────────────────────────
        st.divider()
        st.subheader("Swing Zone Distribution (All)")
        st.plotly_chart(utils.zone_heatmap(df_b["swing_zone"].value_counts().to_dict(),
                                           title="Swing Zone Frequency"), width="stretch")

        # ── first pitch swing tendencies ──────────────────────────────────────
        st.subheader("First Pitch Swing Tendencies")
        col_a_b, col_b_b = st.columns(2)
        with col_a_b:
            _fpab = df_b[df_b["is_fp_app"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpab["swing_zone"].value_counts().to_dict() if not _fpab.empty else {},
                                               title=f"First Pitch of Appearance (n={len(_fpab)})"),
                            width="stretch", config={"displayModeBar": False})
        with col_b_b:
            _fpib = df_b[df_b["is_fp_inn"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpib["swing_zone"].value_counts().to_dict() if not _fpib.empty else {},
                                               title=f"First Pitch of Inning (n={len(_fpib)})"),
                            width="stretch", config={"displayModeBar": False})

        # ── zone by out count ─────────────────────────────────────────────────
        st.subheader("Swing Zone by Out Count")
        _cols_b = st.columns(3)
        for _i, _oc in enumerate([0, 1, 2]):
            _dfo_b = df_b[df_b["outs"] == _oc]
            with _cols_b[_i]:
                st.plotly_chart(utils.zone_heatmap(_dfo_b["swing_zone"].value_counts().to_dict() if not _dfo_b.empty else {},
                                                   title=f"{_oc} Outs (n={len(_dfo_b)})"), width="stretch")

        # ── zone by base state ────────────────────────────────────────────────
        st.subheader("Swing Zone by Base State")
        _col_e_b, _col_r_b = st.columns(2)
        for _col, (_lbl, _obc_vals) in zip([_col_e_b, _col_r_b], [
            ("Empty", ["Empty"]),
            ("Runner(s) On", ["1B","2B","3B","1&2B","1&3B","2&3B","BL"]),
        ]):
            _df_obc_b = df_b[df_b["obc"].isin(_obc_vals)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_obc_b["swing_zone"].value_counts().to_dict() if not _df_obc_b.empty else {},
                                                   title=f"{_lbl} (n={len(_df_obc_b)})"), width="stretch")

        # ── zone by result ────────────────────────────────────────────────────
        st.subheader("Swing Zone by Result")
        _cols_res = st.columns(3)
        for _col, (_lbl, _cats) in zip(_cols_res, [("XBH",["XBH"]),("BB/1B",["BB/1B"]),("OUT",["OUT"])]):
            _df_res = df_b[df_b["res_category"].isin(_cats)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_res["swing_zone"].value_counts().to_dict() if not _df_res.empty else {},
                                                   title=f"{_lbl} (n={len(_df_res)})"), width="stretch")

        # ── delta ─────────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Swing Delta (Change from Previous AB)")
        _deltas_b = df_b["swing_circ_delta"].dropna()
        if not _deltas_b.empty:
            st.plotly_chart(utils.delta_histogram(_deltas_b, title="Swing Delta Distribution"), width="stretch")
            _dc1b, _dc2b, _dc3b = st.columns(3)
            _dc1b.metric("Avg Delta", f"{_deltas_b.mean():+.1f}")
            _dc2b.metric("Avg |Delta|", f"{_deltas_b.abs().mean():.1f}")
            _dc3b.metric("# with Delta", len(_deltas_b))
        else:
            st.caption("Need at least 2 at-bats from the same batter in a session.")

        # ── tendencies ────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Tendencies")
        _tm_b, _tl_b = st.columns(2)
        with _tm_b:
            st.markdown("**Meme Swings (42, 69, 420)**")
            _mc_b = {str(n): int((df_b["swing"] == n).sum()) for n in utils.MEME_NUMBERS}
            _mt_b = sum(_mc_b.values())
            st.metric("Meme Swings", _mt_b, help=f"{_mt_b / _b_total * 100:.1f}% of all swings" if _b_total else "")
            for _num, _cnt in _mc_b.items():
                st.write(f"  **{_num}**: {_cnt} ({_cnt / _b_total * 100:.1f}%)" if _b_total else f"  **{_num}**: {_cnt}")
        with _tl_b:
            st.markdown("**Most Common Last 2 Digits**")
            _last2_b = df_b["swing"].apply(lambda s: int(str(int(s)).zfill(2)[-2:])).value_counts().head(5)
            for _dig, _cnt in _last2_b.items():
                st.write(f"  **{_dig:02d}**: {_cnt} ({_cnt / _b_total * 100:.1f}%)" if _b_total else f"  **{_dig:02d}**: {_cnt}")

        # ── results ───────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Results")
        st.plotly_chart(utils.result_bar(df_b["result"].value_counts().to_dict(),
                                         title="Result Distribution"), width="stretch")
        st.plotly_chart(utils.result_bar(df_b["res_category"].value_counts().to_dict(),
                                         title="Result Category"), width="stretch")

        # ── raw data ──────────────────────────────────────────────────────────
        with st.expander("Raw At-Bat Data"):
            _disp_b = df_b[["season","game_id","inning","outs","obc","pitcher_name","batter_name",
                             "pitch","swing","diff","result","res_category"]].copy()
            _disp_b.columns = ["Season","Game","Inn","Outs","Runners","Pitcher","Batter",
                                "Pitch","Swing","Diff","Result","Category"]
            st.dataframe(_disp_b, use_container_width=True, hide_index=True)

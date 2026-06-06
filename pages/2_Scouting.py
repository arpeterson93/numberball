"""Pitcher and Batter scouting - combined page with shared matchup setup."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Scouting")

# ── data ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_rln_plays() -> pd.DataFrame:
    raw = db.get_all_plays(league="RLN")
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=3600)
def _load_mln_plays() -> pd.DataFrame:
    raw = db.get_all_plays(league="MLN")
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=300)
def _load_scrimmage_plays() -> pd.DataFrame:
    raw = db.get_all_scrimmage_plays()
    return utils.enrich_df(utils.flatten_scrimmage(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=300)
def _load_all_players() -> list:
    return db.get_all_players()

@st.cache_data(ttl=3600)
def _load_run_lookup(_v: int = 3) -> dict:
    # (result, before_obc, outs) -> (runs, new_obc, nout_after)
    return utils.load_run_lookup_from_csv("import_BRC.csv")

@st.cache_data(ttl=3600)
def _load_pitcher_stats() -> pd.DataFrame:
    rows = db.get_pitcher_stats()
    return pd.DataFrame(rows) if rows else pd.DataFrame()

@st.cache_data(ttl=3600)
def _sheet_name(url: str) -> str:
    return utils.get_sheet_name(url)

# ── selectors (shown before any data load) ───────────────────────────────────

_src_col, _clr_col = st.columns([5, 1])
with _src_col:
    _source_label = st.radio(
        "Data source", ["Real Games", "Scrimmages", "All"],
        horizontal=True, key="scouting_source",
    )
with _clr_col:
    st.write("")
    if st.button("Refresh data", key="clear_play_cache"):
        _load_rln_plays.clear()
        _load_mln_plays.clear()
        _load_scrimmage_plays.clear()
        _load_pitcher_stats.clear()
        db.get_pitcher_stats.clear()
        st.rerun()
_source_key = {"Real Games": "real", "Scrimmages": "scrimmage", "All": "all"}[_source_label]

_sel_leagues: list[str] = []
if _source_key in ("real", "all"):
    _sel_leagues = st.multiselect(
        "League", ["RLN", "MLN"], default=["RLN"], key="scouting_league",
    )

# ── load only what was selected ──────────────────────────────────────────────

_dfs = []
if _source_key in ("real", "all"):
    if "RLN" in _sel_leagues:
        _df = _load_rln_plays()
        if not _df.empty:
            _dfs.append(_df)
    if "MLN" in _sel_leagues:
        _df = _load_mln_plays()
        if not _df.empty:
            _dfs.append(_df)
if _source_key in ("scrimmage", "all"):
    _df = _load_scrimmage_plays()
    if not _df.empty:
        _dfs.append(_df)

df_all = pd.concat(_dfs, ignore_index=True) if _dfs else pd.DataFrame()

if df_all.empty:
    st.info("No at-bats in the database yet.")
    st.stop()

_all_players      = _load_all_players()
_pbyn             = {p["name"]: p for p in _all_players if p.get("name")}
_all_teams        = sorted({utils.TEAM_ABBREV.get(p.get("team",""), p.get("team","?"))
                             for p in _all_players if p.get("team")})
_full_to_abbrev   = {v: k for k, v in utils.TEAM_ABBREV.items()}
_all_player_names = sorted(p["name"] for p in _all_players if p.get("name"))

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

_hand_opts   = ["R", "L", "S"]
_runner_opts = ["Empty"] + _all_player_names
_pid_to_name = {str(p["player_id"]): p["name"] for p in _all_players if p.get("player_id") and p.get("name")}

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
            "pred_calc_b_pow": _stat(bp,"pwr"), "pred_calc_b_spd": _stat(bp,"spd"),
        })

def _on_srch_pt():
    st.session_state["srch_pitcher"] = "All"

def _on_srch_bt():
    st.session_state["srch_batter"] = "All"

def _on_tab_p_team():
    st.session_state["tab_p_pitcher"] = "All"

def _on_tab_b_team():
    st.session_state["tab_b_batter"] = "All"

# ── import helper ─────────────────────────────────────────────────────────────

def _import_play(play_id: int, src_df: pd.DataFrame):
    row = src_df[src_df["id"] == play_id]
    if row.empty:
        return
    r = row.iloc[0]
    st.session_state["pred_calc_outs"] = int(r.get("outs",0)) if pd.notna(r.get("outs")) else 0
    for _base, _field in [(1, "on_first"), (2, "on_second"), (3, "on_third")]:
        _pid = str(r.get(_field) or "-").strip()
        _name = _pid_to_name.get(_pid, "Empty") if _pid != "-" else "Empty"
        st.session_state[f"pred_calc_{_base}b"] = _name if _name in _runner_opts else "Empty"

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

    b_name = r.get("batter_name","")
    bp = _pbyn.get(b_name, {})
    b_tf = utils.TEAM_ABBREV.get(bp.get("team",""), "All")
    if b_tf in _all_teams:
        st.session_state["pred_calc_b_team"] = b_tf
    st.session_state["pred_calc_b_name"] = b_name if b_name in _pbyn else "-- Manual --"
    st.session_state["pred_calc_b_hand"] = _hand(bp) if bp else "R"
    st.session_state["pred_calc_b_con"]  = _stat(bp,"con") if bp else 3
    st.session_state["pred_calc_b_eye"]  = _stat(bp,"eye") if bp else 3
    st.session_state["pred_calc_b_pow"]  = _stat(bp,"pwr") if bp else 3
    st.session_state["pred_calc_b_spd"]  = _stat(bp,"spd") if bp else 3

    # Auto-populate tab data filters
    _p_def_teams = sorted(df_all["def_team"].unique())
    _b_off_teams = sorted(df_all["off_team"].unique())
    if p_tf in _p_def_teams:
        st.session_state["tab_p_team"] = p_tf
    st.session_state["tab_p_pitcher"] = p_name if p_name in df_all["pitcher_name"].unique() else "All"
    if b_tf in _b_off_teams:
        st.session_state["tab_b_team"] = b_tf
    st.session_state["tab_b_batter"] = b_name if b_name in df_all["batter_name"].unique() else "All"

# ── compact calc inputs (Option D) ────────────────────────────────────────────

def _render_calc_inputs():
    # ── Pitcher ──────────────────────────────────────────────────────────
    st.markdown("**⚾ Pitcher**")
    _p_team_opts = ["All"] + _all_teams
    if st.session_state.get("pred_calc_p_team") not in _p_team_opts:
        st.session_state["pred_calc_p_team"] = "All"
    _rp = st.columns([1, 2])
    with _rp[0]:
        _p_team = st.selectbox("Team", _p_team_opts, key="pred_calc_p_team", on_change=_on_p_team)
    with _rp[1]:
        _p_name_opts = ["-- Manual --"] + _players_for_team(_p_team)
        if st.session_state.get("pred_calc_p_name") not in _p_name_opts:
            st.session_state["pred_calc_p_name"] = "-- Manual --"
        st.selectbox("Pitcher", _p_name_opts, key="pred_calc_p_name", on_change=_on_pitcher)
    if st.session_state.get("pred_calc_p_hand") not in _hand_opts:
        st.session_state["pred_calc_p_hand"] = "R"
    _pa, _pb, _pc = st.columns(3)
    with _pa:
        st.selectbox("Hand", _hand_opts, key="pred_calc_p_hand")
    with _pb:
        st.number_input("MOV", min_value=1, max_value=5, key="pred_calc_p_mov")
    with _pc:
        st.number_input("CMD", min_value=1, max_value=5, key="pred_calc_p_cmd")
    _pd, _pe, _ = st.columns(3)
    with _pd:
        st.number_input("VEL", min_value=1, max_value=5, key="pred_calc_p_vel")
    with _pe:
        st.number_input("AWR", min_value=1, max_value=5, key="pred_calc_p_awr")

    st.divider()

    # ── Batter ───────────────────────────────────────────────────────────
    st.markdown("**🦇 Batter**")
    _b_team_opts = ["All"] + _all_teams
    if st.session_state.get("pred_calc_b_team") not in _b_team_opts:
        st.session_state["pred_calc_b_team"] = "All"
    _rb = st.columns([1, 2])
    with _rb[0]:
        _b_team = st.selectbox("Team ", _b_team_opts, key="pred_calc_b_team", on_change=_on_b_team)
    with _rb[1]:
        _b_name_opts = ["-- Manual --"] + _players_for_team(_b_team)
        if st.session_state.get("pred_calc_b_name") not in _b_name_opts:
            st.session_state["pred_calc_b_name"] = "-- Manual --"
        st.selectbox("Batter", _b_name_opts, key="pred_calc_b_name", on_change=_on_batter)
    if st.session_state.get("pred_calc_b_hand") not in _hand_opts:
        st.session_state["pred_calc_b_hand"] = "R"
    _ba, _bb, _bc = st.columns(3)
    with _ba:
        st.selectbox("Hand ", _hand_opts, key="pred_calc_b_hand")
    with _bb:
        st.number_input("CON", min_value=1, max_value=5, key="pred_calc_b_con")
    with _bc:
        st.number_input("EYE", min_value=1, max_value=5, key="pred_calc_b_eye")
    _bd, _be, _ = st.columns(3)
    with _bd:
        st.number_input("POW", min_value=1, max_value=5, key="pred_calc_b_pow")
    with _be:
        st.number_input("SPD", min_value=1, max_value=5, key="pred_calc_b_spd")

    st.divider()

    # ── Situation ────────────────────────────────────────────────────────
    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        if st.session_state.get("pred_calc_outs") not in [0, 1, 2]:
            st.session_state["pred_calc_outs"] = 0
        st.radio("Outs", [0, 1, 2], horizontal=True, key="pred_calc_outs")
    with col_s2:
        st.checkbox("Bunting?", key="pred_calc_bunt")
    with col_s3:
        st.checkbox("Hit & Run?", key="pred_calc_hnr")

    # ── Baserunners ──────────────────────────────────────────────────────
    st.markdown("**Baserunners**")
    col_1b, col_2b, col_3b = st.columns(3)
    with col_1b:
        if st.session_state.get("pred_calc_1b") not in _runner_opts:
            st.session_state["pred_calc_1b"] = "Empty"
        _r1 = st.selectbox("1B", _runner_opts, key="pred_calc_1b")
        if _r1 != "Empty" and _r1 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r1],'spd')}")
    with col_2b:
        if st.session_state.get("pred_calc_2b") not in _runner_opts:
            st.session_state["pred_calc_2b"] = "Empty"
        _r2 = st.selectbox("2B", _runner_opts, key="pred_calc_2b")
        if _r2 != "Empty" and _r2 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r2],'spd')}")
    with col_3b:
        if st.session_state.get("pred_calc_3b") not in _runner_opts:
            st.session_state["pred_calc_3b"] = "Empty"
        _r3 = st.selectbox("3B", _runner_opts, key="pred_calc_3b")
        if _r3 != "Empty" and _r3 in _pbyn:
            st.caption(f"SPD: {_stat(_pbyn[_r3],'spd')}")

def _calc_ranges() -> list:
    _runners = any(st.session_state.get(f"pred_calc_{b}b", "Empty") != "Empty" for b in [1, 2, 3])
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

# ── play picker helper ────────────────────────────────────────────────────────

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
                f"{'▲' if str(r['half']).lower().startswith('t') else '▼'}{int(r['inning'])}"
                f"  {r.get('result','')}  {r['batter_name']} vs {r['pitcher_name']}"
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

# ── stat display helper (read-only attrs block) ───────────────────────────────

def _player_attrs_md(p: dict, side: str) -> str:
    if not p:
        return ""
    if side == "pitcher":
        h = _hand(p)
        return (f"Hand **{h}** · MOV **{_stat(p,'mov')}** · CMD **{_stat(p,'cmd')}** · "
                f"VEL **{_stat(p,'vel')}** · AWR **{_stat(p,'awr')}**")
    else:
        h = _hand(p)
        return (f"Hand **{h}** · CON **{_stat(p,'con')}** · EYE **{_stat(p,'eye')}** · "
                f"PWR **{_stat(p,'pwr')}** · SPD **{_stat(p,'spd')}**")

# ── matchup source ────────────────────────────────────────────────────────────

st.subheader("Matchup Setup")
pred_mode = st.radio("Source", ["Fetch Live Matchup", "Historical / Manual"],
                     horizontal=True, key="pred_mode")

result_ranges = None
hist_id       = st.session_state.get("pred_hist_loaded_id") if pred_mode == "Historical / Manual" else None
matchup_label = ""

if pred_mode == "Historical / Manual":
    _calc_r       = _calc_ranges()
    result_ranges = [(r["result"], r["low"], r["high"]) for r in _calc_r]
    _pn = st.session_state.get("pred_calc_p_name", "")
    _bn = st.session_state.get("pred_calc_b_name", "")
    matchup_label = " vs ".join(p for p in [_pn, _bn] if p and p != "-- Manual --")

    with st.expander("Import from History", expanded=True):
        # ── Search Filters ────────────────────────────────────────────────────
        st.markdown("**Search Filters**")
        _srch_seasons_all = sorted(df_all["season"].dropna().unique(), reverse=True)
        _srch_games_all   = sorted(df_all["game_id"].dropna().unique())
        _srch_def_teams   = sorted(df_all["def_team"].unique())
        _srch_off_teams   = sorted(df_all["off_team"].unique())

        _sc1, _sc2 = st.columns(2)
        with _sc1:
            srch_seasons = st.multiselect("Season", _srch_seasons_all, default=_srch_seasons_all, key="srch_seasons")
        with _sc2:
            srch_games = st.multiselect("Games", _srch_games_all, default=_srch_games_all, key="srch_games",
                                        format_func=lambda x: f"Game {int(x)}")

        _sc3, _sc4, _sc5, _sc6 = st.columns(4)
        with _sc3:
            srch_pt = st.selectbox("Pitcher Team", ["All"] + _srch_def_teams, key="srch_pt", on_change=_on_srch_pt)
        with _sc4:
            _srch_pitchers = sorted(
                df_all[df_all["def_team"] == srch_pt]["pitcher_name"].unique()
                if srch_pt != "All" else df_all["pitcher_name"].unique()
            )
            srch_pitcher = st.selectbox("Pitcher", ["All"] + _srch_pitchers, key="srch_pitcher")
        with _sc5:
            srch_bt = st.selectbox("Batter Team", ["All"] + _srch_off_teams, key="srch_bt", on_change=_on_srch_bt)
        with _sc6:
            _srch_batters = sorted(
                df_all[df_all["off_team"] == srch_bt]["batter_name"].unique()
                if srch_bt != "All" else df_all["batter_name"].unique()
            )
            srch_batter = st.selectbox("Batter", ["All"] + _srch_batters, key="srch_batter")

        # Build search-filtered df for the play picker
        df_srch = df_all.copy()
        if srch_seasons:
            df_srch = df_srch[df_srch["season"].isin(srch_seasons)]
        if srch_games:
            df_srch = df_srch[df_srch["game_id"].isin(srch_games)]
        if srch_pt != "All":
            df_srch = df_srch[df_srch["def_team"] == srch_pt]
        if srch_pitcher != "All":
            df_srch = df_srch[df_srch["pitcher_name"] == srch_pitcher]
        if srch_bt != "All":
            df_srch = df_srch[df_srch["off_team"] == srch_bt]
        if srch_batter != "All":
            df_srch = df_srch[df_srch["batter_name"] == srch_batter]

        st.divider()

        # ── Play picker ───────────────────────────────────────────────────────
        _h_play = _play_picker(df_srch, "pred_hist_season", "pred_hist_game", "pred_hist_play")
        col_hi, col_hinfo = st.columns([1, 5])
        with col_hi:
            if _h_play is not None and st.button("Import Play", type="primary",
                                                  use_container_width=True, key="pred_hist_import"):
                _import_play(int(_h_play), df_srch)
                st.session_state["pred_hist_loaded_id"] = int(_h_play)
                st.rerun()
        with col_hinfo:
            if _h_play is not None:
                _hpr = df_srch[df_srch["id"] == _h_play]
                if not _hpr.empty:
                    _hr = _hpr.iloc[0]
                    if pd.notna(_hr.get("pitch")) and pd.notna(_hr.get("swing")):
                        _hd = utils.circular_diff(int(_hr["pitch"]), int(_hr["swing"]))
                        st.caption(
                            f"P:{int(_hr['pitch'])}  S:{int(_hr['swing'])}  "
                            f"Diff:{_hd}  → **{_hr.get('result','')}**"
                        )
        if hist_id and result_ranges:
            _hpr_row = df_all[df_all["id"] == hist_id]
            if not _hpr_row.empty:
                _hpr2 = _hpr_row.iloc[0]
                if pd.notna(_hpr2.get("pitch")) and pd.notna(_hpr2.get("swing")):
                    _had2    = utils.circular_diff(int(_hpr2["pitch"]), int(_hpr2["swing"]))
                    _calc_r2 = _calc_ranges()
                    _hmatch  = next((r for r in _calc_r2 if r["low"] <= _had2 <= r["high"]), None)
                    _hexp    = _hmatch["result"] if _hmatch else "?"
                    _hicon   = "✓" if _hexp == _hpr2.get("result","") else "≠"
                    st.caption(
                        f"Loaded play #{hist_id} · Diff **{_had2}** → "
                        f"calc expects **{_hexp}** {_hicon} actual **{_hpr2.get('result','')}**"
                    )

    with st.expander("Matchup Setup", expanded=True):
        _render_calc_inputs()

elif pred_mode == "Fetch Live Matchup":
    with st.expander("Live Matchup", expanded=True):
        all_games_sw = db.get_games()
        sheet_urls   = list(dict.fromkeys(
            g["sheet_url"] for g in all_games_sw if g.get("sheet_url")
        ))

        col_sh, col_btn = st.columns([3, 1])
        with col_sh:
            if sheet_urls:
                pred_sheet_url = st.selectbox(
                    "Session sheet", sheet_urls, key="pred_sheet_sel",
                    format_func=_sheet_name,
                )
            else:
                st.caption("No sheets linked to any games.")
                pred_sheet_url = None
        with col_btn:
            st.write("")
            if sheet_urls and st.button("Fetch Matchup", type="secondary", key="pull_ranges"):
                try:
                    fetched_ranges, fetched_bunt_ranges, fetched_batter, fetched_pitcher = utils.parse_result_ranges_from_sheet(pred_sheet_url)
                    st.session_state["pred_result_ranges"] = fetched_ranges
                    st.session_state["pred_bunt_ranges"]   = fetched_bunt_ranges
                    st.session_state["pred_sheet_batter"]  = fetched_batter
                    st.session_state["pred_sheet_pitcher"] = fetched_pitcher

                    # Auto-populate tab data filters from fetched names
                    if fetched_pitcher and fetched_pitcher in df_all["pitcher_name"].unique():
                        st.session_state["tab_p_pitcher"] = fetched_pitcher
                        _p_teams = df_all[df_all["pitcher_name"] == fetched_pitcher]["def_team"].mode()
                        if not _p_teams.empty:
                            st.session_state["tab_p_team"] = _p_teams.iloc[0]
                    if fetched_batter and fetched_batter in df_all["batter_name"].unique():
                        st.session_state["tab_b_batter"] = fetched_batter
                        _b_teams = df_all[df_all["batter_name"] == fetched_batter]["off_team"].mode()
                        if not _b_teams.empty:
                            st.session_state["tab_b_team"] = _b_teams.iloc[0]

                    fetched_gameplay = utils.parse_gameplay_from_sheet(pred_sheet_url)
                    st.session_state["steal_runner_data"]  = fetched_gameplay["steal_runners"]
                    st.session_state["mgr_sheet_outs"]     = fetched_gameplay["outs"]
                    st.session_state["mgr_sheet_obc"]      = fetched_gameplay["obc"]

                    _bunt_msg  = " + bunt ranges" if fetched_bunt_ranges else ""
                    _runners   = fetched_gameplay["steal_runners"]
                    _steal_msg = f" + {len(_runners)} runner(s)" if _runners else ""
                    _state_msg = (f" + game state ({fetched_gameplay['outs']} outs, "
                                  f"{utils.obc_display(fetched_gameplay['obc'])})"
                                  if fetched_gameplay["outs"] is not None else "")
                    st.toast(f"Loaded {len(fetched_ranges)} ranges{_bunt_msg}{_steal_msg}{_state_msg}.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        result_ranges = st.session_state.get("pred_result_ranges")
        _sb = st.session_state.get("pred_sheet_batter", "")
        _sp = st.session_state.get("pred_sheet_pitcher", "")

        if _sp or _sb:
            _col_sp, _col_sb = st.columns(2)
            with _col_sp:
                if _sp:
                    _sp_rec = _pbyn.get(_sp, {})
                    st.markdown(f"**{_sp}** (P)")
                    st.caption(_player_attrs_md(_sp_rec, "pitcher"))
            with _col_sb:
                if _sb:
                    _sb_rec = _pbyn.get(_sb, {})
                    st.markdown(f"**{_sb}** (B)")
                    st.caption(_player_attrs_md(_sb_rec, "batter"))

        matchup_label = " vs ".join(filter(None, [_sp, _sb]))

# ── ITD slices (shared placeholder - actual slicing is per-tab) ───────────────

st.divider()

# ── tabs ──────────────────────────────────────────────────────────────────────

tab_p, tab_b, tab_m = st.tabs(["⚾ Pitcher", "🦇 Batter", "📊 Manager"])

# ══════════════════════════════════════════════════════════════════════════════
# PITCHER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_p:
    # ── per-tab data filters ──────────────────────────────────────────────────
    _all_seasons_p = sorted(df_all["season"].dropna().unique(), reverse=True)
    _all_games_p   = sorted(df_all["game_id"].dropna().unique())
    _def_teams_p   = sorted(df_all["def_team"].unique())

    with st.expander("Data Filters", expanded=True):
        _pf1, _pf2 = st.columns(2)
        with _pf1:
            tab_p_seasons = st.multiselect("Season", _all_seasons_p, default=_all_seasons_p, key="tab_p_seasons")
        with _pf2:
            tab_p_games = st.multiselect("Games", _all_games_p, default=_all_games_p, key="tab_p_games",
                                         format_func=lambda x: f"Game {int(x)}")
        _pf3, _pf4 = st.columns(2)
        with _pf3:
            if st.session_state.get("tab_p_team", "All") not in (["All"] + _def_teams_p):
                st.session_state["tab_p_team"] = "All"
            tab_p_team = st.selectbox("Pitcher Team", ["All"] + _def_teams_p,
                                      key="tab_p_team", on_change=_on_tab_p_team)
        with _pf4:
            _tab_p_pitchers = sorted(
                df_all[df_all["def_team"] == tab_p_team]["pitcher_name"].unique()
                if tab_p_team != "All" else df_all["pitcher_name"].unique()
            )
            if st.session_state.get("tab_p_pitcher", "All") not in (["All"] + _tab_p_pitchers):
                st.session_state["tab_p_pitcher"] = "All"
            tab_p_pitcher = st.selectbox("Pitcher", ["All"] + _tab_p_pitchers,
                                         key="tab_p_pitcher")

    # Build pitcher tab df
    df_p = df_all.copy()
    if tab_p_seasons:
        df_p = df_p[df_p["season"].isin(tab_p_seasons)]
    if tab_p_games:
        df_p = df_p[df_p["game_id"].isin(tab_p_games)]
    if tab_p_team != "All":
        df_p = df_p[df_p["def_team"] == tab_p_team]
    if tab_p_pitcher != "All":
        df_p = df_p[df_p["pitcher_name"] == tab_p_pitcher]

    df_p_pred = df_p[df_p["id"] < hist_id] if hist_id else df_p

    # ── recent PA window - shared by the percentile card and the Last N chart ──
    n_pitches = st.slider("Recent PA Window", 5, 100, 20, step=5, key="last_n_pitch")

    # ── percentile card ───────────────────────────────────────────────────────
    if tab_p_pitcher != "All":
        _pitcher_stats_df = _load_pitcher_stats()
        _recent_df    = df_p.sort_values("id").tail(n_pitches)
        _recent_stats = utils.compute_recent_pitcher_stats(_recent_df)
        _recent_n     = int(_recent_df["swing"].notna().sum())
        _pct_fig = utils.pitcher_percentile_card(
            tab_p_pitcher, _pitcher_stats_df,
            recent_vals=_recent_stats if _recent_stats else None,
            recent_n=_recent_n if _recent_stats else None,
        )
        if _pct_fig is not None:
            st.plotly_chart(_pct_fig, width="stretch",
                            config={"displayModeBar": False}, key="p_pct_card")
        else:
            st.caption("No career stats on file - run Refresh Pitcher Stats on the Games page.")

    if df_p.empty:
        st.warning("No at-bats match the current pitcher filter.")
    else:
        # ── swing predictor ───────────────────────────────────────────────────
        st.subheader("Swing Predictor")
        st.caption("Enter a proposed swing to see what each of this pitcher's recent pitches would give.")

        if result_ranges:
            if "_pend_swing" in st.session_state:
                st.session_state["pred_swing"] = st.session_state.pop("_pend_swing")
            for _pk in st.session_state.pop("_pills_rst_p", []):
                st.session_state[_pk] = None

            if "pred_swing" not in st.session_state:
                st.session_state["pred_swing"] = 500
            proposed_swing = st.number_input("Proposed Swing", min_value=1, max_value=1000,
                                             step=1, key="pred_swing")

            _df_tick_p = df_p_pred if not df_p_pred.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_p = f"Last {n_pitches} pitches (pre-AB)" if hist_id and not df_p_pred.empty \
                          else f"Last {n_pitches} pitches"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_p, swing=int(proposed_swing), n=n_pitches,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_p),
                width="stretch", key="p_swing_pred",
            )

            st.markdown("**Optimal Swing**")
            _recent_p = df_p_pred.sort_values("id").tail(n_pitches)["pitch"].dropna().astype(int).tolist() \
                        if not df_p_pred.empty else []
            _delta_p  = utils.project_from_deltas(_recent_p)
            _delta2_p = utils.project_from_delta2s(_recent_p)
            _opt_rows_p = [("Based on Recent Pitch Values", _recent_p), ("Based on Recent Pitch Δ", _delta_p),
                           ("Based on Recent Pitch Δ²", _delta2_p)]
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
                                        use_container_width=True, key=f"p_opt_obp_{_i}")
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
                                        use_container_width=True, key=f"p_opt_slg_{_i}")
        else:
            if pred_mode == "Fetch Live Matchup":
                st.info("Select a matchup above to enable the predictor.")

        # ── last N pitches ────────────────────────────────────────────────────
        st.divider()
        _actual_pitches_p = len(df_p_pred.sort_values("id").tail(n_pitches)) if not df_p_pred.empty else 0
        st.subheader(f"Last {_actual_pitches_p} Pitches")
        swing_off_p = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_p",
                               help="+1: shifts swing markers right by one AB.")
        st.plotly_chart(
            utils.last_n_combined_chart(df_p_pred, n=n_pitches, delta_col="pitch",
                                        title=f"Last {_actual_pitches_p} Pitches",
                                        swing_offset=(swing_off_p == "+1"),
                                        segment_games=True),
            width="stretch", key="p_last_n",
        )

        # rebind so all sections below are ITD
        df_p = df_p_pred
        _p_total = len(df_p)

        # Calculate deltas for downstream sections (game-scoped)
        _deltas_p = df_p["pitch_circ_delta"].dropna()
        _delta2_p = df_p["pitch_circ_delta2"].dropna()
        _approach_p = df_p["pitch_approach"].dropna()

        st.divider()
        st.subheader("Next Pitch Delta vs Prior Pitch Delta")
        st.caption("How does a pitcher adjust their next pitch movement based on their previous pitch movement?")
        st.plotly_chart(
            utils.next_delta_vs_prior_delta_heatmap(df_p, title="Next Pitch Δ vs Prior Pitch Δ", value_col="pitch"),
            width="stretch", config={"displayModeBar": False}, key="p_delta_delta_hm",
        )

        st.divider()
        st.subheader("Next Pitch Delta vs Prior Diff")
        st.caption("How does a pitcher adjust their next pitch based on how close the previous swing was?")
        st.plotly_chart(
            utils.diff_vs_next_pitch_delta_heatmap(df_p, title="Next Pitch Δ vs Prior Diff"),
            width="stretch", config={"displayModeBar": False}, key="p_diff_delta_hm",
        )

        # ── hot zone ─────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Hot Zone Pitch Matrix")
        st.caption("How often each pitch range is followed by each other pitch range.")
        bucket_p = st.select_slider("Bucket size", options=[50,100,125,200,250,500], value=100, key="hz_bucket_p")
        group_cols_p = ["game_id","pitcher_name"] if tab_p_pitcher != "All" else ["pitcher_name"]
        st.plotly_chart(utils.hot_zone_matrix(df_p, value_col="pitch",
                                              group_cols=group_cols_p, bucket_size=bucket_p), width="stretch", key="p_hot_zone")

        # ── zone distribution ─────────────────────────────────────────────────
        st.divider()
        st.subheader("Zone Distribution (All)")
        st.plotly_chart(utils.zone_heatmap(df_p["pitch_zone"].value_counts().to_dict(),
                                           title="Pitch Zone Frequency"), width="stretch", key="p_zone_all")

        # ── first pitch tendencies ────────────────────────────────────────────
        st.subheader("First Pitch Tendencies")
        col_a_p, col_b_p = st.columns(2)
        with col_a_p:
            _fpa = df_p[df_p["is_fp_app"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpa["pitch_zone"].value_counts().to_dict() if not _fpa.empty else {},
                                               title=f"First Pitch of Appearance (n={len(_fpa)})"),
                            width="stretch", config={"displayModeBar": False}, key="p_fpa")
        with col_b_p:
            _fpi = df_p[df_p["is_fp_inn"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpi["pitch_zone"].value_counts().to_dict() if not _fpi.empty else {},
                                               title=f"First Pitch of Inning (n={len(_fpi)})"),
                            width="stretch", config={"displayModeBar": False}, key="p_fpi")

        # ── zone by out count ─────────────────────────────────────────────────
        st.subheader("Zone by Out Count")
        _cols_p = st.columns(3)
        for _i, _oc in enumerate([0, 1, 2]):
            _dfo = df_p[df_p["outs"] == _oc]
            with _cols_p[_i]:
                st.plotly_chart(utils.zone_heatmap(_dfo["pitch_zone"].value_counts().to_dict() if not _dfo.empty else {},
                                                   title=f"{_oc} Outs (n={len(_dfo)})"), width="stretch", key=f"p_oc_{_oc}")

        # ── zone by base state ────────────────────────────────────────────────
        st.subheader("Zone by Base State")
        _col_e_p, _col_r_p = st.columns(2)
        for _col, (_lbl, _obc_vals) in zip([_col_e_p, _col_r_p], [
            ("Empty", ["000"]),
            ("Runner(s) On", ["001","010","100","011","101","110","111"]),
        ]):
            _df_obc = df_p[df_p["obc"].isin(_obc_vals)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_obc["pitch_zone"].value_counts().to_dict() if not _df_obc.empty else {},
                                                   title=f"{_lbl} (n={len(_df_obc)})"), width="stretch", key=f"p_obc_{_lbl}")

        # ── delta ─────────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Pitch Delta (Change from Previous AB)")
        if not _deltas_p.empty:
            st.plotly_chart(utils.delta_histogram(_deltas_p), width="stretch", key="p_delta")
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
                _dig = int(_dig)
                st.write(f"  **{_dig:02d}**: {_cnt} ({_cnt / _p_total * 100:.1f}%)" if _p_total else f"  **{_dig:02d}**: {_cnt}")

        # ── results ───────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Results Allowed")
        st.plotly_chart(utils.result_bar(df_p["result"].value_counts().to_dict(),
                                         title="Result Distribution"), width="stretch", key="p_res_dist")
        st.plotly_chart(utils.result_bar(df_p["res_category"].value_counts().to_dict(),
                                         title="Result Category"), width="stretch", key="p_res_cat")

        # ── raw data ──────────────────────────────────────────────────────────
        with st.expander("Raw Plate Appearance Data"):
            _disp_p = df_p[["season","game_id","inning","outs","obc","pitcher_name","batter_name",
                             "pitch","swing","diff","result","res_category"]].copy()
            _disp_p["obc"] = _disp_p["obc"].map(utils.obc_display)
            _disp_p.columns = ["Season","Game","Inn","Outs","Runners","Pitcher","Batter",
                                "Pitch","Swing","Diff","Result","Category"]
            _disp_p = _disp_p.iloc[::-1].reset_index(drop=True)
            st.dataframe(_disp_p, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# BATTER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_b:
    # ── per-tab data filters ──────────────────────────────────────────────────
    _all_seasons_b = sorted(df_all["season"].dropna().unique(), reverse=True)
    _all_games_b   = sorted(df_all["game_id"].dropna().unique())
    _off_teams_b   = sorted(df_all["off_team"].unique())

    with st.expander("Data Filters", expanded=True):
        _bf1, _bf2 = st.columns(2)
        with _bf1:
            tab_b_seasons = st.multiselect("Season", _all_seasons_b, default=_all_seasons_b, key="tab_b_seasons")
        with _bf2:
            tab_b_games = st.multiselect("Games", _all_games_b, default=_all_games_b, key="tab_b_games",
                                         format_func=lambda x: f"Game {int(x)}")
        _bf3, _bf4, _bf5 = st.columns(3)
        with _bf3:
            if st.session_state.get("tab_b_team", "All") not in (["All"] + _off_teams_b):
                st.session_state["tab_b_team"] = "All"
            tab_b_team = st.selectbox("Batter Team", ["All"] + _off_teams_b,
                                      key="tab_b_team", on_change=_on_tab_b_team)
        with _bf4:
            _tab_b_batters = sorted(
                df_all[df_all["off_team"] == tab_b_team]["batter_name"].unique()
                if tab_b_team != "All" else df_all["batter_name"].unique()
            )
            if st.session_state.get("tab_b_batter", "All") not in (["All"] + _tab_b_batters):
                st.session_state["tab_b_batter"] = "All"
            tab_b_batter = st.selectbox("Batter", ["All"] + _tab_b_batters,
                                        key="tab_b_batter")
        with _bf5:
            if tab_b_batter != "All":
                tab_b_scope = st.radio("Scope", ["Solo", "Full Team"], horizontal=True, key="tab_b_scope",
                                       help="Solo: only this batter. Full Team: all ABs from their team.")
            else:
                tab_b_scope = "Solo"

    # Build batter tab df
    df_b = df_all.copy()
    if tab_b_seasons:
        df_b = df_b[df_b["season"].isin(tab_b_seasons)]
    if tab_b_games:
        df_b = df_b[df_b["game_id"].isin(tab_b_games)]
    if tab_b_team != "All":
        df_b = df_b[df_b["off_team"] == tab_b_team]
    if tab_b_batter != "All" and tab_b_scope == "Solo":
        df_b = df_b[df_b["batter_name"] == tab_b_batter]

    df_b_pred = df_b[df_b["id"] < hist_id] if hist_id else df_b

    # ── recent PA window - shared by predictor and Last N chart ───────────────
    n_swings = st.slider("Recent PA Window", 5, 100, 20, step=5, key="last_n_swing")

    if df_b.empty:
        st.warning("No at-bats match the current batter filter.")
    else:
        # ── pitch predictor ───────────────────────────────────────────────────
        st.subheader("Pitch Predictor")
        st.caption("Enter a proposed pitch to see what each of this batter's recent swings would give.")

        if result_ranges:
            if "_pend_pitch" in st.session_state:
                st.session_state["pred_pitch"] = st.session_state.pop("_pend_pitch")
            for _pk in st.session_state.pop("_pills_rst_b", []):
                st.session_state[_pk] = None

            if "pred_pitch" not in st.session_state:
                st.session_state["pred_pitch"] = 500
            proposed_pitch = st.number_input("Proposed Pitch", min_value=1, max_value=1000,
                                             step=1, key="pred_pitch")

            _df_tick_b = df_b_pred if not df_b_pred.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_b = f"Last {n_swings} swings (pre-AB)" if hist_id and not df_b_pred.empty \
                          else f"Last {n_swings} swings"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_b, swing=int(proposed_pitch), n=n_swings,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_b,
                                            value_col="swing", x_label="Swing Values", ref_label="Pitch"),
                width="stretch", key="b_swing_pred",
            )

            st.markdown("**Optimal Pitch**")
            _recent_b = df_b_pred.sort_values("id").tail(n_swings)["swing"].dropna().astype(int).tolist() \
                        if not df_b_pred.empty else []
            _delta_b  = utils.project_from_deltas(_recent_b)
            _delta2_b = utils.project_from_delta2s(_recent_b)
            _opt_rows_b = [("Based on Recent Swing Values", _recent_b), ("Based on Recent Swing Δ", _delta_b),
                           ("Based on Recent Swing Δ²", _delta2_b)]
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
                                        use_container_width=True, key=f"b_opt_obp_{_i}")
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
                                        use_container_width=True, key=f"b_opt_slg_{_i}")
        else:
            if pred_mode == "Fetch Live Matchup":
                st.info("Select a matchup above to enable the predictor.")

        # ── last N swings ─────────────────────────────────────────────────────
        st.divider()
        _actual_swings_b = len(df_b_pred.sort_values("id").tail(n_swings)) if not df_b_pred.empty else 0
        st.subheader(f"Last {_actual_swings_b} Swings")
        swing_off_b = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_b",
                               help="+1: shifts swing markers right by one AB.")

        _hl_name = tab_b_batter if (tab_b_batter != "All" and tab_b_scope == "Full Team") else None
        st.plotly_chart(
            utils.last_n_combined_chart(df_b_pred, n=n_swings, delta_col="swing",
                                        title=f"Last {_actual_swings_b} Swings",
                                        swing_offset=(swing_off_b == "+1"),
                                        highlight_name=_hl_name),
            width="stretch", key="b_last_n",
        )

        # rebind so all sections below are ITD
        df_b = df_b_pred
        _b_total = len(df_b)

        # Calculate swing deltas for downstream sections (game-scoped)
        _deltas_b = df_b["swing_circ_delta"].dropna() if "swing_circ_delta" in df_b.columns else pd.Series(dtype=float)

        st.divider()
        st.subheader("Next Swing Delta vs Prior Swing Delta")
        st.caption("How does a batter adjust their next swing based on their previous swing movement?")
        st.plotly_chart(
            utils.next_delta_vs_prior_delta_heatmap(df_b, title="Next Swing Δ vs Prior Swing Δ", value_col="swing"),
            width="stretch", config={"displayModeBar": False}, key="b_delta_delta_hm",
        )

        st.divider()
        st.subheader("Next Swing Delta vs Prior Diff")
        st.caption("How does a batter adjust their next swing based on how close the previous pitch was?")
        st.plotly_chart(
            utils.diff_vs_next_pitch_delta_heatmap(df_b, value_col="swing", title="Next Swing Δ vs Prior Diff"),
            width="stretch", config={"displayModeBar": False}, key="b_diff_delta_hm",
        )

        # ── hot zone ─────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Hot Zone Swing Matrix")
        st.caption("How often each swing range is followed by each other swing range.")
        bucket_b = st.select_slider("Bucket size", options=[50,100,125,200,250,500], value=100, key="hz_bucket_b")
        group_cols_b = ["game_id","batter_name"] if tab_b_batter != "All" else ["batter_name"]
        st.plotly_chart(utils.hot_zone_matrix(df_b, value_col="swing",
                                              group_cols=group_cols_b, bucket_size=bucket_b), width="stretch", key="b_hot_zone")

        # ── zone distribution ─────────────────────────────────────────────────
        st.divider()
        st.subheader("Swing Zone Distribution (All)")
        st.plotly_chart(utils.zone_heatmap(df_b["swing_zone"].value_counts().to_dict(),
                                           title="Swing Zone Frequency"), width="stretch", key="b_zone_all")

        # ── first pitch swing tendencies ──────────────────────────────────────
        st.subheader("First Pitch Swing Tendencies")
        col_a_b, col_b_b = st.columns(2)
        with col_a_b:
            _fpab = df_b[df_b["is_fp_app"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpab["swing_zone"].value_counts().to_dict() if not _fpab.empty else {},
                                               title=f"First Pitch of Appearance (n={len(_fpab)})"),
                            width="stretch", config={"displayModeBar": False}, key="b_fpa")
        with col_b_b:
            _fpib = df_b[df_b["is_fp_inn"] == True]
            st.plotly_chart(utils.zone_heatmap(_fpib["swing_zone"].value_counts().to_dict() if not _fpib.empty else {},
                                               title=f"First Pitch of Inning (n={len(_fpib)})"),
                            width="stretch", config={"displayModeBar": False}, key="b_fpi")

        # ── zone by out count ─────────────────────────────────────────────────
        st.subheader("Swing Zone by Out Count")
        _cols_b = st.columns(3)
        for _i, _oc in enumerate([0, 1, 2]):
            _dfo_b = df_b[df_b["outs"] == _oc]
            with _cols_b[_i]:
                st.plotly_chart(utils.zone_heatmap(_dfo_b["swing_zone"].value_counts().to_dict() if not _dfo_b.empty else {},
                                                   title=f"{_oc} Outs (n={len(_dfo_b)})"), width="stretch", key=f"b_oc_{_oc}")

        # ── zone by base state ────────────────────────────────────────────────
        st.subheader("Swing Zone by Base State")
        _col_e_b, _col_r_b = st.columns(2)
        for _col, (_lbl, _obc_vals) in zip([_col_e_b, _col_r_b], [
            ("Empty", ["000"]),
            ("Runner(s) On", ["001","010","100","011","101","110","111"]),
        ]):
            _df_obc_b = df_b[df_b["obc"].isin(_obc_vals)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_obc_b["swing_zone"].value_counts().to_dict() if not _df_obc_b.empty else {},
                                                   title=f"{_lbl} (n={len(_df_obc_b)})"), width="stretch", key=f"b_obc_{_lbl}")

        # ── zone by result ────────────────────────────────────────────────────
        st.subheader("Swing Zone by Result")
        _cols_res = st.columns(3)
        for _col, (_lbl, _cats) in zip(_cols_res, [("XBH",["XBH"]),("BB/1B",["BB/1B"]),("OUT",["OUT"])]):
            _df_res = df_b[df_b["res_category"].isin(_cats)]
            with _col:
                st.plotly_chart(utils.zone_heatmap(_df_res["swing_zone"].value_counts().to_dict() if not _df_res.empty else {},
                                                   title=f"{_lbl} (n={len(_df_res)})"), width="stretch", key=f"b_res_{_lbl}")

        # ── delta ─────────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Swing Delta (Change from Previous AB)")
        _deltas_b = df_b["swing_circ_delta"].dropna()
        if not _deltas_b.empty:
            st.plotly_chart(utils.delta_histogram(_deltas_b, title="Swing Delta Distribution"), width="stretch", key="b_delta")
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
            _last2_b = df_b["swing"].dropna().apply(lambda s: int(str(int(s)).zfill(2)[-2:])).value_counts().head(5)
            for _dig, _cnt in _last2_b.items():
                st.write(f"  **{int(_dig):02d}**: {_cnt} ({_cnt / _b_total * 100:.1f}%)" if _b_total else f"  **{int(_dig):02d}**: {_cnt}")

        # ── results ───────────────────────────────────────────────────────────
        st.divider()
        st.subheader("Results")
        st.plotly_chart(utils.result_bar(df_b["result"].value_counts().to_dict(),
                                         title="Result Distribution"), width="stretch", key="b_res_dist")
        st.plotly_chart(utils.result_bar(df_b["res_category"].value_counts().to_dict(),
                                         title="Result Category"), width="stretch", key="b_res_cat")

        # ── raw data ──────────────────────────────────────────────────────────
        with st.expander("Raw Plate Appearance Data"):
            _disp_b = df_b[["season","game_id","inning","outs","obc","pitcher_name","batter_name",
                             "pitch","swing","diff","result","res_category"]].copy()
            _disp_b["obc"] = _disp_b["obc"].map(utils.obc_display)
            _disp_b.columns = ["Season","Game","Inn","Outs","Runners","Pitcher","Batter",
                                "Pitch","Swing","Diff","Result","Category"]
            _disp_b = _disp_b.iloc[::-1].reset_index(drop=True)
            st.dataframe(_disp_b, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# MANAGER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_m:
    if df_all.empty:
        st.warning("No at-bats available for analysis.")
        st.stop()

    # Prefer live game state from sheet fetch; fall back to last play in data
    _sheet_outs = st.session_state.get("mgr_sheet_outs")
    _sheet_obc  = st.session_state.get("mgr_sheet_obc")
    if _sheet_outs is not None and _sheet_obc is not None:
        _current_outs = int(_sheet_outs)
        _current_obc  = _sheet_obc
    else:
        _current_pa   = df_all.iloc[-1]
        _current_outs = int(_current_pa["outs"])
        _current_obc  = _current_pa["obc"]
    _current_er   = utils.get_expected_runs(_current_outs, _current_obc) or 0
    _run_lookup   = _load_run_lookup()


    def _norm(entry):
        if isinstance(entry, dict):
            return entry["result"], entry["low"], entry["high"]
        return entry

    def _lookup(result, obc, outs=None):
        """Return (runs, new_obc, nout_after) from import_BRC.
        nout_after = total outs after the play (eOuts from CSV).
        """
        o = _current_outs if outs is None else outs
        entry = _run_lookup.get((result, obc, o))
        if entry is not None and len(entry) == 3:
            return entry
        new_obc, _ = utils.advance_runners(result, obc, o)
        return 0.0, new_obc, min(o + utils.outs_added(result), 3)

    def _calc_ev(ranges):
        ev = 0.0
        for entry in (ranges or []):
            _r, _dl, _dh = _norm(entry)
            _prob  = min((_dh - _dl + 1) * 2 / 1000, 1.0)
            _runs, _nobc, _nout = _lookup(_r, _current_obc)
            _nout  = min(_nout, 3)
            _ner   = utils.get_expected_runs(_nout, _nobc) or 0 if _nout < 3 else 0
            ev    += _prob * (_runs + _ner)
        return ev

    def _outcome_grid(ranges, obc, outs):
        """Build flat outcome breakdown DataFrame sorted by (ER After + Runs) desc."""
        rows = []
        for entry in (ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)

            if r == "Safe":
                runs = 0
                new_obc_code, _ = utils.steal_advance(obc, outs)
                nout = outs
            elif r == "Out":
                # Caught stealing: lead runner out, other runners still advance one base
                runs = 0
                new_obc_code, _ = utils.steal_cs(obc)
                nout = min(outs + 1, 3)
            else:
                runs, new_obc_code, nout = _lookup(r, obc, outs)
                nout = min(nout, 3)

            er_after = round(utils.get_expected_runs(nout, new_obc_code) or 0, 2) if nout < 3 else 0.0
            total    = runs + er_after

            _display_r = "SB" if r == "Safe" else ("CS" if r == "Out" else r)
            rows.append({
                "_sort":      total,
                "Result":     _display_r,
                "Range":      f"{lo}-{hi}",
                "Prob":       f"{prob * 100:.1f}%",
                "3-2-1":      utils.obc_circles(new_obc_code),
                "Outs After": "End" if nout >= 3 else nout,
                "Runs":       int(runs),
                "ER After":   er_after,
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)

    def _outcome_scatter(grid):
        import plotly.graph_objects as go
        from collections import defaultdict

        probs      = grid["Prob"].str.rstrip("%").astype(float)
        total_runs = grid["Runs"].astype(float) + grid["ER After"].astype(float)

        # Stack probabilities at each unique x value, collect result names
        prob_at: dict[float, float]       = defaultdict(float)
        names_at: dict[float, list[str]]  = defaultdict(list)
        for x, p, r in zip(total_runs, probs, grid["Result"]):
            xr = round(float(x), 3)
            prob_at[xr]  += float(p)
            names_at[xr].append(r)

        xs     = sorted(prob_at.keys())
        ys     = [prob_at[x] for x in xs]
        labels = ["/".join(names_at[x]) for x in xs]

        fig = go.Figure()

        # Stems: vertical lines from y=0 to y=prob for each x
        stem_x, stem_y = [], []
        for x, y in zip(xs, ys):
            stem_x += [x, x, None]
            stem_y += [0, y, None]
        fig.add_trace(go.Scatter(
            x=stem_x, y=stem_y,
            mode="lines",
            line=dict(color="rgba(100,160,255,0.7)", width=2),
            hoverinfo="skip", showlegend=False,
        ))

        # Dots + labels at the top of each stem
        fig.add_trace(go.Scatter(
            x=xs, y=ys,
            mode="markers+text",
            marker=dict(size=9, color="rgba(100,160,255,1)", line=dict(color="white", width=1)),
            text=labels,
            textposition="top center",
            textfont=dict(size=11),
            hovertemplate="%{text}<br>Total: %{x:.3f}<br>Prob: %{y:.1f}%<extra></extra>",
            showlegend=False,
        ))

        fig.update_layout(
            xaxis=dict(title=dict(text="Runs + ER After", font=dict(size=10)),
                       gridcolor="#333", zeroline=False),
            yaxis=dict(title=dict(text="Prob %", font=dict(size=10)), gridcolor="#333",
                       rangemode="tozero", range=[0, max(ys) * 1.35]),
            height=260,
            margin=dict(l=45, r=10, t=30, b=40),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            dragmode=False, showlegend=False,
            modebar_remove=["zoom2d","pan2d","select2d","lasso2d","zoomIn2d",
                            "zoomOut2d","autoScale2d","resetScale2d","toImage"],
        )
        return fig

    def _show_outcome_grid(ranges, obc, outs, key):
        grid = _outcome_grid(ranges, obc, outs)
        if grid.empty:
            return
        st.plotly_chart(_outcome_scatter(grid), use_container_width=True, key=f"{key}_scatter")
        with st.expander("Outcome Breakdown", expanded=False):
            st.dataframe(grid, use_container_width=True, hide_index=True, key=key)

    def _calc_ev_steal(safe_range):
        safe_prob = min(safe_range * 2 / 1000, 1.0)
        out_prob  = 1.0 - safe_prob
        # Safe: all runners advance one base
        safe_obc, safe_runs = utils.steal_advance(_current_obc, _current_outs)
        safe_ner  = utils.get_expected_runs(_current_outs, safe_obc) or 0
        # Caught: lead runner out, other runners still advance one base
        out_obc, _ = utils.steal_cs(_current_obc)
        out_nout = min(_current_outs + 1, 3)
        out_ner  = utils.get_expected_runs(out_nout, out_obc) or 0 if out_nout < 3 else 0
        return safe_prob * (safe_runs + safe_ner) + out_prob * out_ner

    def _hnr_steal_advance_obc(obc: str) -> tuple[str, int]:
        """Advance the H&R runner: 1B->2B if present, else 2B->3B, else 3B->home."""
        on_3b, on_2b, on_1b = obc[0] == "1", obc[1] == "1", obc[2] == "1"
        if on_1b:
            return f"{obc[0]}10", 0
        elif on_2b:
            return "100", 0
        elif on_3b:
            return "000", 1
        return obc, 0

    def _hnr_steal_cs_obc(obc: str) -> str:
        """OBC after the H&R runner is caught stealing."""
        if obc[2] == "1":       # 1B runner caught - remove them, keep 3B
            return f"{obc[0]}00"
        elif obc[1] == "1":     # 2B runner caught
            return f"{obc[0]}00"
        else:                   # 3B runner caught
            return "000"

    def _calc_ev_hnr(hnr_ranges, hnr_steal_safe_rng):
        """EV for hit and run: non-K outcomes use BRC; K outcomes fold in steal attempt."""
        sp = min(hnr_steal_safe_rng * 2 / 1000, 1.0)
        op = 1.0 - sp
        ev = 0.0
        for entry in (hnr_ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)
            if r == "K":
                # Batter K's (1 out) and runner was already going -> steal attempt
                _, _, k_nout = _lookup("K", _current_obc, _current_outs)
                k_nout = min(k_nout, 3)
                s_obc, s_runs = _hnr_steal_advance_obc(_current_obc)
                s_ner  = utils.get_expected_runs(k_nout, s_obc) or 0 if k_nout < 3 else 0
                cs_obc  = _hnr_steal_cs_obc(_current_obc)
                cs_nout = min(k_nout + 1, 3)
                cs_ner  = utils.get_expected_runs(cs_nout, cs_obc) or 0 if cs_nout < 3 else 0
                ev += prob * (sp * (s_runs + s_ner) + op * cs_ner)
            else:
                runs, new_obc, nout = _lookup(r, _current_obc, _current_outs)
                nout = min(nout, 3)
                ner = utils.get_expected_runs(nout, new_obc) or 0 if nout < 3 else 0
                ev += prob * (runs + ner)
        return ev

    _bunt_ranges = st.session_state.get("pred_bunt_ranges") or result_ranges
    _hnr_ranges  = utils.compute_at_bat_ranges(
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
        bunt=False, hit_and_run=True,
        outs=_current_outs,
        runners_on=_current_obc != "000",
    )

    _steal_runners = st.session_state.get("steal_runner_data") or []
    # Default safe range: lead runner from sheet, or fallback to 50
    _default_safe_rng = _steal_runners[0]["safe_range"] if _steal_runners else 50

    # H&R valid only with < 2 outs and a single-runner OBC (1,2,4,5 = 001,010,100,101)
    _HNR_VALID_OBCS = {"001", "010", "100", "101"}
    _has_hnr = _current_obc in _HNR_VALID_OBCS and _current_outs < 2 and bool(_steal_runners)
    if _has_hnr:
        # For OBC 101 (1B+3B), H&R steals with the 1B runner specifically
        _hnr_steal_runner = (
            next((r for r in _steal_runners if r["base"] == "1B"), None)
            if _current_obc == "101"
            else (_steal_runners[0] if _steal_runners else None)
        )
        _hnr_safe_rng = (
            utils.steal_safe_range_plus1_spd(
                _hnr_steal_runner["safe_range"], _hnr_steal_runner["base"]
            )
            if _hnr_steal_runner else 50
        )
    else:
        _hnr_steal_runner = None
        _hnr_safe_rng    = 50

    @st.fragment
    def _manager_fragment():
        # Steal: sheet value drives the color bar; editable input drives the EV table
        _sheet_safe_rng = _default_safe_rng  # locked to sheet pull
        if _steal_runners:
            _steal_opts = {
                f"{r['base']} (range: {r['safe_range']})": r["safe_range"]
                for r in _steal_runners
            }
            _steal_sel = st.selectbox("Stealing runner", list(_steal_opts.keys()),
                                      key="mgr_steal_runner")
            _sheet_safe_rng = _steal_opts[_steal_sel]

        _has_runners = _current_obc != "000" and bool(_steal_runners)

        if _has_runners:
            _steal_ev_rng = st.number_input(
                "Steal Safe Diff Range (for EV)", min_value=1, max_value=500,
                value=int(_sheet_safe_rng), step=1, key="mgr_steal_ev_range",
            )
        else:
            _steal_ev_rng = _sheet_safe_rng

        ev_swing = _calc_ev(result_ranges)
        ev_bunt  = _calc_ev(_bunt_ranges)
        ev_steal = _calc_ev_steal(_steal_ev_rng)                    if _has_runners else None
        ev_hr    = _calc_ev_hnr(_hnr_ranges, _hnr_safe_rng)         if _has_hnr     else None

        # Game state + EV summary table side by side
        _gs_col, _tbl_col = st.columns([1, 2])
        with _gs_col:
            st.caption(f"Baseline ER: {_current_er:.2f}")
            st.markdown(utils.bases_diamond_svg(_current_obc, _current_outs),
                        unsafe_allow_html=True)
        with _tbl_col:
            _decisions = ["Normal Swing", "Bunt"]
            _exp_runs  = [f"{ev_swing:.2f}", f"{ev_bunt:.2f}"]
            _vs_base   = [f"{ev_swing - _current_er:+.2f}", f"{ev_bunt - _current_er:+.2f}"]
            if _has_runners:
                _decisions += ["Steal"]
                _exp_runs  += [f"{ev_steal:.2f}"]
                _vs_base   += [f"{ev_steal - _current_er:+.2f}"]
            if _has_hnr:
                _decisions += ["Hit and Run"]
                _exp_runs  += [f"{ev_hr:.2f}"]
                _vs_base   += [f"{ev_hr - _current_er:+.2f}"]
            st.dataframe(
                pd.DataFrame({"Decision": _decisions, "Exp Runs": _exp_runs, "vs Baseline": _vs_base}),
                use_container_width=True, hide_index=True,
            )

        _proposed = st.number_input("Proposed Value", min_value=1, max_value=1000,
                                    value=500, step=1, key="mgr_proposed")

        st.divider()

        st.subheader("Normal Swing")
        st.plotly_chart(utils.manager_color_bar(int(_proposed), result_ranges,
                        label="Swing", x_label="Swing Values"),
                        use_container_width=True, key="mgr_bar_swing")
        _show_outcome_grid(result_ranges, _current_obc, _current_outs, "grid_swing")

        st.divider()

        st.subheader("Bunt")
        if not st.session_state.get("pred_bunt_ranges"):
            st.caption("No bunt ranges fetched - showing normal ranges as fallback")
        st.plotly_chart(utils.manager_color_bar(int(_proposed), _bunt_ranges,
                        label="Bunt", x_label="Bunt Values"),
                        use_container_width=True, key="mgr_bar_bunt")
        _show_outcome_grid(_bunt_ranges, _current_obc, _current_outs, "grid_bunt")

        if _has_runners:
            st.divider()
            st.subheader("Steal")
            st.caption(f"Color bar uses sheet safe range: {_sheet_safe_rng}")
            st.plotly_chart(utils.steal_color_bar(int(_proposed), int(_sheet_safe_rng),
                            label="Steal", x_label="Steal Values"),
                            use_container_width=True, key="mgr_bar_steal")
            # Steal grid: Safe and Out as the two results
            _steal_ranges_for_grid = [
                ("Safe", 1, _sheet_safe_rng),
                ("Out",  _sheet_safe_rng + 1, 500),
            ]
            _show_outcome_grid(_steal_ranges_for_grid, _current_obc, _current_outs, "grid_steal")

        if _has_hnr:
            st.divider()
            st.subheader("Hit and Run")
            _hnr_base_lbl = _hnr_steal_runner["base"] if _hnr_steal_runner else "?"
            _orig_rng     = _hnr_steal_runner["safe_range"] if _hnr_steal_runner else "?"
            st.caption(
                f"{_hnr_base_lbl} runner steals at spd+1: range {_orig_rng} -> {_hnr_safe_rng}  |  "
                f"K outcome = batter out + steal attempt"
            )
            st.plotly_chart(utils.manager_color_bar(int(_proposed), _hnr_ranges,
                            label="Swing", x_label="Swing Values"),
                            use_container_width=True, key="mgr_bar_hr")
            _show_outcome_grid(_hnr_ranges, _current_obc, _current_outs, "grid_hr")

    _manager_fragment()

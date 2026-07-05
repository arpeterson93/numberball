"""Pitcher and Batter scouting - combined page with shared matchup setup."""
from __future__ import annotations
import streamlit as st
import pandas as pd
import database as db
import utils

_MLN_QS_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"
_MLN_QS_SEASON   = 13

_MLN_QS_GAMES_COLS = {
    "game_code", "game_id_short", "league", "game_type", "season", "session_number",
    "away_team", "home_team",
    "away_score", "home_score", "win_team", "loss_team",
    "umpire",
    "winning_pitcher", "losing_pitcher", "save_pitcher",
    "hold_1", "hold_2",
    "player_of_game",
    "honorable_mention_1", "honorable_mention_2", "honorable_mention_3",
    "start_time", "end_time",
    "last_play", "last_inning", "last_result",
    "division", "archive_sheet_id",
    "sheet_url", "link",
}


def _qs_mln_games() -> tuple[int, list[str]]:
    games = utils.read_mln_games_from_sheet(_MLN_QS_SHEET_ID)
    if not games:
        return 0, ["No games found in the MLN Games tab."]
    abbrev_to_full = utils.read_mln_team_abbrev_lookup(_MLN_QS_SHEET_ID)
    for g in games:
        g["away_team"] = abbrev_to_full.get(g["away_team"], g["away_team"])
        g["home_team"] = abbrev_to_full.get(g["home_team"], g["home_team"])
        a_scr, h_scr = g.get("away_score"), g.get("home_score")
        if a_scr is not None and h_scr is not None:
            if a_scr > h_scr:
                g["win_team"], g["loss_team"] = g["away_team"], g["home_team"]
            elif h_scr > a_scr:
                g["win_team"], g["loss_team"] = g["home_team"], g["away_team"]
        g["archive_sheet_id"] = _MLN_QS_SHEET_ID
        g["game_type"] = "live"
    games = [{k: v for k, v in g.items() if k in _MLN_QS_GAMES_COLS} for g in games]
    try:
        n = db.bulk_upsert_games(games)
        return n, []
    except Exception as e:
        return 0, [str(e)]


def _qs_mln_plays(full: bool = False) -> tuple[int, list[str]]:
    from concurrent.futures import ThreadPoolExecutor
    # These five reads are independent - fetch them concurrently instead of serially.
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_plays   = ex.submit(utils.read_mln_plays_from_sheet, _MLN_QS_SHEET_ID, tab="Plays (Raw)")
        f_games   = ex.submit(db.get_games)
        f_teams   = ex.submit(db.get_mln_teams_for_lookup)
        f_players = ex.submit(db.get_mln_players_for_lookup)
        f_max_pn  = ex.submit(db.get_max_play_num, "MLN")
    plays       = f_plays.result()
    all_games   = f_games.result()
    mln_teams   = f_teams.result()
    mln_players = f_players.result()
    # Incremental sync: only upsert plays past what's already stored. full=True
    # (Full Resync) sets the floor to 0 so every play is re-upserted - use that
    # after correcting an already-synced play.
    max_pn      = 0 if full else f_max_pn.result()

    if not plays:
        return 0, ["No plays found in the MLN Plays (Raw) tab."]
    game_code_to_id = {str(g["game_code"]).strip(): g["id"] for g in all_games if g.get("game_code") and g.get("id")}
    mln_game_codes_in_db = sorted(
        str(g["game_code"]).strip() for g in all_games if g.get("league") == "MLN" and g.get("game_code")
    )
    play_game_codes = sorted({p["game_code"] for p in plays})
    if not mln_game_codes_in_db:
        return 0, [
            f"No MLN games found in database - sync MLN Games first. "
            f"Plays expect game codes like: {play_game_codes[:5]}"
        ]
    missing = sorted(set(play_game_codes) - set(mln_game_codes_in_db))
    diag = (
        [
            f"DB has {len(mln_game_codes_in_db)} MLN game(s) e.g. {mln_game_codes_in_db[:5]}",
            f"Plays expect {len(play_game_codes)} game(s) e.g. {play_game_codes[:5]}",
            f"{len(missing)} game code(s) missing from DB e.g. {missing[:5]}",
        ]
        if missing
        else []
    )

    team_id_to_full = {t["team_id"]: t["full_team"] for t in mln_teams if t.get("team_id") and t.get("full_team")}
    sid_to_name = {p["s_id"]: p["name"] for p in mln_players if p.get("s_id") and p.get("name")}

    plays_sorted = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set] = {}
    seen_pitcher: dict[str, set] = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc = play["game_code"]
        season = _MLN_QS_SEASON or play.get("season") or (int(gc[:2]) if gc and len(gc) >= 2 and gc[:2].isdigit() else None)
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not found - sync MLN Games first.")
            continue

        def _sid(pid):
            return f"{season}_{pid}" if season and pid else None

        pitcher_sid = _sid(play.get("pitcher_id"))
        batter_sid  = _sid(play.get("batter_id"))
        catcher_sid = _sid(play.get("catcher_id"))
        runner_sid  = _sid(play.get("runner_id"))

        pitcher_name = sid_to_name.get(pitcher_sid, "") if pitcher_sid else ""
        batter_name  = sid_to_name.get(batter_sid,  "") if batter_sid  else ""
        catcher_name = sid_to_name.get(catcher_sid)     if catcher_sid else None
        runner_name  = sid_to_name.get(runner_sid)      if runner_sid  else None

        away_full = team_id_to_full.get(play.get("away") or "", play.get("away") or "")
        home_full = team_id_to_full.get(play.get("home") or "", play.get("home") or "")
        if play["half"] == "top":
            off_team, def_team = away_full, home_full
        else:
            off_team, def_team = home_full, away_full

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        if inn_key not in outs_tracker:
            outs_tracker[inn_key] = 0
        outs = outs_tracker[inn_key]
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn = fp_inn_key not in seen_inn.setdefault(gc, set())
        is_fp_app = pitcher_name not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k not in ("game_code", "away", "home")},
            "game_code":    gc,
            "season":       season,
            "game_type":    "live",
            "game_id":      game_db_id,
            "away":         play.get("away"),
            "home":         play.get("home"),
            "pitcher_name": pitcher_name,
            "batter_name":  batter_name,
            "catcher_name": catcher_name,
            "runner_name":  runner_name,
            "off_team":     off_team,
            "def_team":     def_team,
            "diff":         diff,
            "outs":         outs,
            "is_fp_inn":    is_fp_inn,
            "is_fp_app":    is_fp_app,
        })

    if not rows:
        return 0, diag + errors
    # Trackers (outs, first-pitch flags) were computed over every play above; now
    # keep only the plays we actually need to write. Incremental keeps new plays;
    # Full Resync (max_pn=0) keeps them all.
    rows = [r for r in rows if r["play_num"] > max_pn]
    if not rows:
        return 0, diag + errors
    rows = list({(r["play_num"], r.get("league", "MLN")): r for r in rows}.values())
    try:
        n = db.bulk_upsert_mln_plays(rows)
        return n, diag + errors
    except Exception as e:
        return 0, diag + errors + [str(e)]


# Detect narrow viewport (mobile) for responsive hint chart layout
_ua = st.context.headers.get("user-agent", "").lower()
_is_mobile = any(k in _ua for k in ("mobile", "android", "iphone", "ipad", "silk"))

def _run_mln_sync(full: bool) -> None:
    label = "Full resync of MLN Games then Plays..." if full else "Syncing MLN Games then Plays..."
    with st.spinner(label):
        _qs_gn, _qs_gerrs = _qs_mln_games()
        _qs_pn, _qs_perrs = _qs_mln_plays(full=full)
    _tag = " (full)" if full else ""
    st.session_state["_sync_msg"] = f"MLN{_tag} - Games: {_qs_gn} · Plays: {_qs_pn}"
    _qs_all_errs = _qs_gerrs + _qs_perrs
    if _qs_all_errs:
        st.session_state["_sync_errors"] = _qs_all_errs
    # Version-keyed invalidation: bump _data_v (threaded into every play/game
    # loader) instead of st.cache_data.clear(), which needlessly nuked the
    # players/teams/CSV caches that a plays sync never touches.
    st.session_state["_data_v"] = st.session_state.get("_data_v", 0) + 1
    st.session_state.pop("_auto_fetch_done", None)
    st.session_state.pop("pred_result_ranges", None)
    st.rerun()


_title_col, _qs_btn_col = st.columns([5, 1], vertical_alignment="bottom")
with _title_col:
    st.title("Scouting")
with _qs_btn_col:
    _do_sync = st.button("Sync MLN", key="qs_mln_all", use_container_width=True)
    _do_full = st.button(
        "Full Resync", key="qs_mln_full", use_container_width=True,
        help="Re-upserts every play. Use after correcting an already-synced play; "
             "the regular Sync only adds new plays.",
    )
if _do_sync or _do_full:
    _run_mln_sync(full=_do_full)

# Seed radio state once from DB-backed preference so index never fights the widget
if "scouting_view_radio" not in st.session_state:
    _stored = st.session_state.get("scouting_view", "complex")
    st.session_state["scouting_view_radio"] = "Complex" if _stored == "complex" else "Simple"

_view_sel = st.radio(
    "Scouting View", ["Complex", "Simple"],
    horizontal=True, key="scouting_view_radio",
)
_new_view = _view_sel.lower()
if _new_view != st.session_state.get("scouting_view", "complex"):
    st.session_state["scouting_view"] = _new_view
    _uid = st.session_state.get("user_id")
    if _uid:
        try:
            db.upsert_user_preferences(_uid, _new_view)
        except Exception:
            pass
_simple_mode = _new_view == "simple"

# ── MLN quick sync ───────────────────────────────────────────────────────────
if "_sync_msg" in st.session_state:
    st.success(st.session_state.pop("_sync_msg"))
if "_sync_errors" in st.session_state:
    _errs_disp = st.session_state.pop("_sync_errors")
    with st.expander(f"{len(_errs_disp)} issue(s)"):
        for _m in _errs_disp[:30]:
            st.caption(_m)

st.divider()

# ── data ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def _load_pitcher_plays(pitcher_name: str, leagues: tuple[str, ...], data_v: int = 0) -> pd.DataFrame:
    raw = db.get_plays_for_pitcher(pitcher_name, list(leagues) if leagues else None)
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=3600)
def _load_batter_plays(batter_name: str, leagues: tuple[str, ...], data_v: int = 0) -> pd.DataFrame:
    raw = db.get_plays_for_batter(batter_name, list(leagues) if leagues else None)
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=3600)
def _load_team_offense_plays(team_name: str, leagues: tuple[str, ...], data_v: int = 0) -> pd.DataFrame:
    raw = db.get_plays_for_team_offense(team_name, list(leagues) if leagues else None)
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=3600)
def _load_all_games(data_v: int = 0) -> list:
    return db.get_games()

@st.cache_data(ttl=3600)
def _load_scrimmage_plays() -> pd.DataFrame:
    raw = db.get_all_scrimmage_plays()
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()

@st.cache_data(ttl=3600)
def _load_all_players() -> list:
    return db.get_all_players()

@st.cache_data(ttl=3600)
def _load_all_teams_data() -> list:
    return db.get_all_teams()

@st.cache_data(ttl=3600)
def _load_run_lookup(_v: int = 3) -> dict:
    # (result, before_obc, outs) -> (runs, new_obc, nout_after)
    return utils.load_run_lookup_from_csv("import_BRC.csv")

@st.cache_data(ttl=3600)
def _load_pitcher_stats() -> pd.DataFrame:
    rows = db.get_pitcher_stats()
    return pd.DataFrame(rows) if rows else pd.DataFrame()

@st.cache_data(ttl=60)
def _load_game_plays(game_id: int, data_v: int = 0) -> list[dict]:
    return db.get_plays_for_game(game_id)

@st.cache_data(ttl=3600)
def _load_sheet_names(urls: tuple[str, ...]) -> dict[str, str]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(utils.get_sheet_name, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                result[url] = future.result()
            except Exception:
                result[url] = url
    return result

# ── selectors (shown before any data load) ───────────────────────────────────

with st.expander("Data Source & League", expanded=False):
    _source_label = st.radio(
        "Data source", ["Real Games", "Scrimmages", "All"],
        horizontal=True, key="scouting_source",
    )
    _source_key = {"Real Games": "real", "Scrimmages": "scrimmage", "All": "all"}[_source_label]

    _sel_leagues: list[str] = []
    if _source_key in ("real", "all"):
        _sel_leagues = st.multiselect(
            "League", ["RLN", "MLN"], default=["RLN", "MLN"], key="scouting_league",
        )
    else:
        _sel_leagues = []

_leagues_tuple: tuple[str, ...] = tuple(sorted(_sel_leagues))

# ── lightweight metadata (always loaded) ─────────────────────────────────────

all_games_meta = _load_all_games(st.session_state.get("_data_v", 0))
_meta_seasons   = sorted({g["season"] for g in all_games_meta if g.get("season")}, reverse=True)
_meta_def_teams = sorted({t for g in all_games_meta for t in (g.get("home_team") or "", g.get("away_team") or "") if t})

# Scrimmage plays are a small table - still load in full when selected
_scrimmage_df: pd.DataFrame = pd.DataFrame()
if _source_key in ("scrimmage", "all"):
    _scrimmage_df = _load_scrimmage_plays()

_all_players      = _load_all_players()
# Sort by season ascending so later seasons overwrite earlier ones in the name dict
_players_by_season   = sorted(_all_players, key=lambda p: p.get("season") or 0)
_pbyn                = {p["name"]: p for p in _players_by_season if p.get("name")}
_all_player_names    = sorted({p["name"] for p in _all_players if p.get("name")})
# Season-aware lookups for historical play import
_p_by_sid = {p["s_id"]: p for p in _all_players if p.get("s_id")}
_p_by_pid = {str(p["player_id"]): p for p in _all_players if p.get("player_id")}

# Build abbrev -> team_name from DB; latest season overwrites earlier for same abbrev
_teams_by_season     = sorted(_load_all_teams_data(), key=lambda t: t.get("season") or 0)
_abbrev_to_team_name = {
    t["abbrev"]: (t.get("team_name") or t["abbrev"])
    for t in _teams_by_season if t.get("abbrev")
}
_team_name_to_abbrev = {v: k for k, v in _abbrev_to_team_name.items()}
_all_teams           = sorted({
    _abbrev_to_team_name.get(p.get("team", ""), p.get("team", "?"))
    for p in _all_players if p.get("team")
})

@st.cache_data(ttl=3600)
def _load_team_hex() -> dict[str, str]:
    result: dict[str, str] = {}
    for t in db.get_all_teams():
        hex_c = t.get("primary_hex") or ""
        if not hex_c:
            continue
        if not hex_c.startswith("#"):
            hex_c = "#" + hex_c
        for key in ("full_team", "team_name"):
            n = t.get(key) or ""
            if n:
                result[n] = hex_c
    return result

_team_hex_map = _load_team_hex()

# ── shared helpers ────────────────────────────────────────────────────────────

def _players_for_team(team_display: str) -> list[str]:
    if team_display == "All":
        return _all_player_names
    abbrev = _team_name_to_abbrev.get(team_display, team_display)
    return sorted({p["name"] for p in _all_players if p.get("name") and p.get("team") == abbrev})

def _stat(p: dict, key: str, default: int = 3) -> int:
    v = p.get(key)
    return int(v) if v is not None else default

def _hand(p: dict) -> str:
    h = str(p.get("hand", "R")).upper()
    return h if h in ("L", "R", "S") else "R"

_hand_opts   = ["R", "L", "S"]
_runner_opts = ["Empty"] + list(_all_player_names)
_pid_to_name = {str(p["player_id"]): p["name"] for p in _all_players if p.get("player_id") and p.get("name")}

# ── session-state defaults ────────────────────────────────────────────────────

_DEFS = {
    "pred_calc_p_team":"All","pred_calc_p_name":"-- Manual --",
    "pred_calc_p_hand":"R","pred_calc_p_mov":3,"pred_calc_p_cmd":3,"pred_calc_p_vel":3,"pred_calc_p_awr":3,
    "pred_calc_b_team":"All","pred_calc_b_name":"-- Manual --",
    "pred_calc_b_hand":"R","pred_calc_b_con":3,"pred_calc_b_eye":3,"pred_calc_b_pow":3,"pred_calc_b_spd":3,
    "pred_calc_c_eye":3,
    "pred_calc_outs":0,"pred_calc_bunt":False,"pred_calc_hnr":False,"pred_calc_if_in":False,
    "pred_calc_1b":"Empty","pred_calc_2b":"Empty","pred_calc_3b":"Empty",
    "pred_calc_hist_obc":"000",
    "mgr_inning":1,"mgr_half":"Top","mgr_away_score":0,"mgr_home_score":0,
    "mgr_league":"MLN",
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
    st.session_state["pred_calc_outs"] = int(r.get("outs", 0)) if pd.notna(r.get("outs")) else 0
    _half_raw = str(r.get("half", "top")).lower()
    st.session_state["mgr_half"]       = "Bottom" if _half_raw.startswith("b") else "Top"
    st.session_state["mgr_inning"]     = int(r.get("inning", 1)) if pd.notna(r.get("inning")) else 1
    st.session_state["mgr_away_score"] = int(r.get("away_score", 0)) if pd.notna(r.get("away_score")) else 0
    st.session_state["mgr_home_score"] = int(r.get("home_score", 0)) if pd.notna(r.get("home_score")) else 0
    # Store raw obc as fallback for when runner IDs can't be resolved to names
    _raw_obc = str(r.get("obc") or "000")
    st.session_state["pred_calc_hist_obc"] = _raw_obc

    # Season-aware player lookup: MLN uses s_id = "{season}_{raw_id}", RLN uses player_id directly
    _season   = r.get("season")
    _is_mln   = str(r.get("league") or "").upper() == "MLN"

    def _look(raw_id) -> dict:
        if not raw_id or str(raw_id).strip() in ("", "-"):
            return {}
        s = str(raw_id).strip()
        if _is_mln and _season:
            return _p_by_sid.get(f"{_season}_{s}", {})
        return _p_by_pid.get(s, {})

    def _resolve(raw_id, fallback_name: str) -> dict:
        """Look up by season-aware ID; fall back to current-season name lookup."""
        return _look(raw_id) or _pbyn.get(fallback_name, {})

    pp = _resolve(r.get("pitcher_id"),  r.get("pitcher_name",  ""))
    bp = _resolve(r.get("batter_id"),   r.get("batter_name",   ""))
    cp = _resolve(r.get("catcher_id"),  r.get("catcher_name",  ""))

    p_name = pp.get("name") or str(r.get("pitcher_name") or "")
    b_name = bp.get("name") or str(r.get("batter_name")  or "")

    p_tf = _abbrev_to_team_name.get(pp.get("team", ""), "All")
    b_tf = _abbrev_to_team_name.get(bp.get("team", ""), "All")

    # Baserunners - resolve by season-aware ID; obc is the fallback for the calc
    for _base, _field in [(1, "on_first"), (2, "on_second"), (3, "on_third")]:
        _rp   = _look(r.get(_field))
        _rname = _rp.get("name", "") if _rp else ""
        st.session_state[f"pred_calc_{_base}b"] = _rname if _rname in _runner_opts else "Empty"
        # Store season-correct runner speed so _calc_ranges uses the right season's stats
        _rspd_raw = _rp.get("spd") if _rp else None
        if _rspd_raw is not None:
            st.session_state[f"pred_calc_{_base}b_spd"] = int(_rspd_raw)
        else:
            st.session_state.pop(f"pred_calc_{_base}b_spd", None)

    if p_tf in _all_teams:
        st.session_state["pred_calc_p_team"] = p_tf
    st.session_state["pred_calc_p_name"] = p_name if p_name in _pbyn else "-- Manual --"
    st.session_state["pred_calc_p_hand"] = _hand(pp) if pp else "R"
    st.session_state["pred_calc_p_mov"]  = _stat(pp, "mov") if pp else 3
    st.session_state["pred_calc_p_cmd"]  = _stat(pp, "cmd") if pp else 3
    st.session_state["pred_calc_p_vel"]  = _stat(pp, "vel") if pp else 3
    st.session_state["pred_calc_p_awr"]  = _stat(pp, "awr") if pp else 3

    if b_tf in _all_teams:
        st.session_state["pred_calc_b_team"] = b_tf
    st.session_state["pred_calc_b_name"] = b_name if b_name in _pbyn else "-- Manual --"
    st.session_state["pred_calc_b_hand"] = _hand(bp) if bp else "R"
    st.session_state["pred_calc_b_con"]  = _stat(bp, "con") if bp else 3
    st.session_state["pred_calc_b_eye"]  = _stat(bp, "eye") if bp else 3
    st.session_state["pred_calc_b_pow"]  = _stat(bp, "pwr") if bp else 3
    st.session_state["pred_calc_b_spd"]  = _stat(bp, "spd") if bp else 3

    st.session_state["pred_calc_c_eye"] = _stat(cp, "eye") if cp else 3

    # Auto-populate tab data filters
    if p_tf in _all_teams:
        st.session_state["tab_p_team"] = p_tf
    st.session_state["tab_p_pitcher"] = p_name if p_name in _all_player_names else "All"
    if b_tf in _all_teams:
        st.session_state["tab_b_team"] = b_tf
    st.session_state["tab_b_batter"] = b_name if b_name in _all_player_names else "All"

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
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    with col_s1:
        if st.session_state.get("pred_calc_outs") not in [0, 1, 2]:
            st.session_state["pred_calc_outs"] = 0
        st.radio("Outs", [0, 1, 2], horizontal=True, key="pred_calc_outs")
    with col_s2:
        st.checkbox("Bunting?", key="pred_calc_bunt")
    with col_s3:
        _hnr_obc = (
            ("1" if st.session_state.get("pred_calc_3b", "Empty") != "Empty" else "0")
            + ("1" if st.session_state.get("pred_calc_2b", "Empty") != "Empty" else "0")
            + ("1" if st.session_state.get("pred_calc_1b", "Empty") != "Empty" else "0")
        )
        if _hnr_obc == "000":
            _hnr_obc = st.session_state.get("pred_calc_hist_obc", "000") or "000"
        _hnr_valid = (
            st.session_state.get("pred_calc_outs", 0) < 2
            and _hnr_obc in {"001", "010", "011", "101"}
        )
        if not _hnr_valid:
            st.session_state["pred_calc_hnr"] = False
        st.checkbox("Hit & Run?", key="pred_calc_hnr", disabled=not _hnr_valid,
                    help="Only valid with 0-1 outs and runner on 1B, 2B, 1B+2B, or 1B+3B")
    with col_s4:
        _has_3b = (
            st.session_state.get("pred_calc_3b", "Empty") != "Empty"
            or st.session_state.get("pred_calc_hist_obc", "000")[0] == "1"
        )
        _if_in_valid = _has_3b and st.session_state.get("pred_calc_outs", 0) < 2
        if not _if_in_valid:
            st.session_state["pred_calc_if_in"] = False
        st.checkbox("Infield In?", key="pred_calc_if_in", disabled=not _if_in_valid,
                    help="Only valid with runner on 3rd and fewer than 2 outs")
    col_c1, _col_c2, _col_c3 = st.columns(3)
    with col_c1:
        st.number_input("Catcher EYE", min_value=1, max_value=5, key="pred_calc_c_eye")

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

def _calc_ranges(bunt: bool | None = None, hit_and_run: bool | None = None, infield_in: bool | None = None) -> list:
    _r1n = st.session_state.get("pred_calc_1b", "Empty")
    _r2n = st.session_state.get("pred_calc_2b", "Empty")
    _r3n = st.session_state.get("pred_calc_3b", "Empty")
    # Build OBC from named runners, fall back to historical OBC
    _named_obc = (
        ("1" if _r3n != "Empty" else "0")
        + ("1" if _r2n != "Empty" else "0")
        + ("1" if _r1n != "Empty" else "0")
    )
    _hist_obc = st.session_state.get("pred_calc_hist_obc", "000") or "000"
    # Per-bit merge: keep named "1" where resolved; fill unresolved "0" from hist
    _obc = "".join(n if n == "1" else h for n, h in zip(_named_obc, _hist_obc))
    # Runner speeds: named runner -> pbyn lookup; unnamed hist_obc runner -> stored fetch speed
    def _runner_spd(name: str, spd_key: str, obc_bit: bool) -> int | None:
        if not obc_bit:
            return None
        if name != "Empty":
            return _stat(_pbyn.get(name, {}), "spd") if name in _pbyn else None
        return st.session_state.get(spd_key)  # hist_obc runner: speed stored at fetch time
    _r1_spd = _runner_spd(_r1n, "pred_calc_1b_spd", _obc[2] == "1")
    _r2_spd = _runner_spd(_r2n, "pred_calc_2b_spd", _obc[1] == "1")
    _r3_spd = _runner_spd(_r3n, "pred_calc_3b_spd", _obc[0] == "1")
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
        bunt=bunt if bunt is not None else bool(st.session_state.get("pred_calc_bunt", False)),
        hit_and_run=hit_and_run if hit_and_run is not None else bool(st.session_state.get("pred_calc_hnr", False)),
        infield_in=infield_in if infield_in is not None else bool(st.session_state.get("pred_calc_if_in", False)),
        outs=int(st.session_state.get("pred_calc_outs",0)),
        runners_on=_obc != "000",
        obc=_obc,
        runner_1b_spd=_r1_spd,
        runner_2b_spd=_r2_spd,
        runner_3b_spd=_r3_spd,
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
        _h_games = sorted(_df_hs["game_code"].dropna().unique())
        _h_game  = st.selectbox("Game", _h_games, format_func=lambda x: f"Game {x}", key=game_key)
    _df_hg = _df_hs[_df_hs["game_code"] == _h_game].sort_values("id")
    with col_hp:
        _h_po = _df_hg[["id","inning","half","outs","obc","pitcher_name","batter_name","pitch","swing","result"]].copy()
        _h_po["label"] = _h_po.apply(
            lambda r: (
                f"{'▲' if str(r['half']).lower().startswith('t') else '▼'}{int(r['inning'])}"
                f"  {r.get('result','')}  {r['batter_name']} vs {r['pitcher_name']}"
            ), axis=1, result_type="reduce",
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

# Read mode from session state so data logic runs before the expander renders
pred_mode = st.session_state.get("pred_mode", "Fetch Live Matchup")
sheet_urls: list[str] = []
_sheet_label_map: dict[str, str] = {}

result_ranges = None
hist_id       = st.session_state.get("pred_hist_loaded_id") if pred_mode == "Historical / Manual" else None
matchup_label = ""
_sp = _sb = ""

# Allow up to 6 extra innings beyond regulation so extra-inning games don't crash
_mgr_max_inning = utils.game_innings(st.session_state.get("mgr_league", "MLN")) + 6
if st.session_state.get("mgr_inning", 1) > _mgr_max_inning:
    st.session_state["mgr_inning"] = int(_mgr_max_inning)

if pred_mode == "Historical / Manual":
    _calc_r       = _calc_ranges(bunt=False)
    result_ranges = [(r["result"], r["low"], r["high"]) for r in _calc_r]
    _calc_b       = _calc_ranges(bunt=True)
    st.session_state["pred_bunt_ranges"] = [(r["result"], r["low"], r["high"]) for r in _calc_b]
    _pn = st.session_state.get("pred_calc_p_name", "")
    _bn = st.session_state.get("pred_calc_b_name", "")
    matchup_label = " vs ".join(p for p in [_pn, _bn] if p and p != "-- Manual --")

elif pred_mode == "Fetch Live Matchup":
    sheet_urls = list(dict.fromkeys(
        g["sheet_url"] for g in all_games_meta if g.get("sheet_url")
    ))
    _sheet_label_map = _load_sheet_names(tuple(sheet_urls)) if sheet_urls else {}

    # Auto-select Portland Pioneers sheet on first visit
    if "pred_sheet_sel" not in st.session_state and sheet_urls:
        _pioneer_url = next(
            (url for url, name in _sheet_label_map.items()
             if "por - oregon trail dysentary field" in name.lower()),
            sheet_urls[0],
        )
        st.session_state["pred_sheet_sel"] = _pioneer_url

    def _run_fetch(url: str) -> None:
        _fr, _fbr, _fb, _fp, _stype, _sif_in = utils.parse_result_ranges_from_sheet(url)
        st.session_state["pred_result_ranges"]    = _fr
        st.session_state["pred_bunt_ranges"]      = _fbr
        st.session_state["pred_sheet_batter"]     = _fb
        st.session_state["pred_sheet_pitcher"]    = _fp
        st.session_state["pred_sheet_swing_type"] = _stype
        st.session_state["pred_sheet_if_in"]      = _sif_in
        _nl = {n.lower(): n for n in _all_player_names}
        _fpc = _nl.get((_fp or "").lower(), "")
        if _fpc:
            _pp = _pbyn.get(_fpc, {})
            _ptf = _abbrev_to_team_name.get(_pp.get("team", ""), "All")
            st.session_state["tab_p_pitcher"]      = _fpc
            st.session_state["pred_calc_p_name"]   = _fpc if _fpc in _pbyn else "-- Manual --"
            if _ptf in _all_teams:
                st.session_state["pred_calc_p_team"] = _ptf
            if _fpc in _pbyn:
                st.session_state["pred_calc_p_hand"] = _hand(_pp)
                st.session_state["pred_calc_p_mov"]  = _stat(_pp, "mov")
                st.session_state["pred_calc_p_cmd"]  = _stat(_pp, "cmd")
                st.session_state["pred_calc_p_vel"]  = _stat(_pp, "vel")
                st.session_state["pred_calc_p_awr"]  = _stat(_pp, "awr")
        _fbc = _nl.get((_fb or "").lower(), "")
        if _fbc:
            _bp = _pbyn.get(_fbc, {})
            _btf = _abbrev_to_team_name.get(_bp.get("team", ""), "All")
            st.session_state["tab_b_batter"]       = _fbc
            st.session_state["pred_calc_b_name"]   = _fbc if _fbc in _pbyn else "-- Manual --"
            if _btf in _all_teams:
                st.session_state["pred_calc_b_team"] = _btf
            if _fbc in _pbyn:
                st.session_state["pred_calc_b_hand"] = _hand(_bp)
                st.session_state["pred_calc_b_con"]  = _stat(_bp, "con")
                st.session_state["pred_calc_b_eye"]  = _stat(_bp, "eye")
                st.session_state["pred_calc_b_pow"]  = _stat(_bp, "pwr")
                st.session_state["pred_calc_b_spd"]  = _stat(_bp, "spd")
        _gp = utils.parse_gameplay_from_sheet(url)
        st.session_state["steal_runner_data"] = _gp["steal_runners"]
        st.session_state["mgr_sheet_outs"]    = _gp["outs"]
        st.session_state["mgr_sheet_obc"]     = _gp["obc"]
        _mg = next((g for g in all_games_meta if g.get("sheet_url") == url), None)
        if _mg:
            st.session_state["game_tab_game_id"] = _mg["id"]
            st.session_state["game_tab_sel"]     = _mg["id"]
            _fl = _mg.get("league", "MLN")
            st.session_state["mgr_league"] = _fl
            # Resolve runner identities from sheet player IDs -> look up speed
            _season_mg  = _mg.get("season")
            _is_mln_mg  = _fl.upper() == "MLN"
            _runner_ids = _gp.get("runner_ids", {})
            for _base_ltr, _base_key in [("1", "1B"), ("2", "2B"), ("3", "3B")]:
                _rid = _runner_ids.get(_base_key)
                _rp_sheet: dict = {}
                if _rid:
                    if _is_mln_mg and _season_mg:
                        _rp_sheet = _p_by_sid.get(f"{_season_mg}_{_rid}", {})
                    if not _rp_sheet:
                        _rp_sheet = _p_by_pid.get(str(_rid), {})
                _rname = _rp_sheet.get("name", "") if _rp_sheet else ""
                _rspd  = _rp_sheet.get("spd")      if _rp_sheet else None
                st.session_state[f"pred_calc_{_base_ltr}b"] = _rname if _rname in _runner_opts else "Empty"
                if _rspd is not None:
                    st.session_state[f"pred_calc_{_base_ltr}b_spd"] = int(_rspd)
                else:
                    st.session_state.pop(f"pred_calc_{_base_ltr}b_spd", None)
            _fi = utils.game_innings(_fl)
            st.session_state["mgr_away_score"] = int(_mg["away_score"]) if _mg.get("away_score") is not None else 0
            st.session_state["mgr_home_score"] = int(_mg["home_score"]) if _mg.get("home_score") is not None else 0
            _gplays = _load_game_plays(_mg["id"], st.session_state.get("_data_v", 0))
            if _gplays:
                _lp = sorted(_gplays, key=lambda p: p.get("play_num") or p.get("id") or 0)[-1]
                _li, _lh = int(_lp.get("inning") or 1), str(_lp.get("half") or "top").lower()
                _lo = int(_lp.get("outs") or 0)
                _eo = utils.outs_added(str(_lp.get("result") or ""))
                if _lo + _eo >= 3:
                    if _lh == "top":
                        st.session_state["mgr_inning"] = _li
                        st.session_state["mgr_half"]   = "Bottom"
                    else:
                        st.session_state["mgr_inning"] = min(_li + 1, _fi)
                        st.session_state["mgr_half"]   = "Top"
                else:
                    st.session_state["mgr_inning"] = _li
                    st.session_state["mgr_half"]   = "Top" if _lh == "top" else "Bottom"
        st.session_state.pop("mgr_steal_runner", None)

        # Fetch HnR / InfIn / HnR+InfIn scenario sheets in parallel
        # Match the fetched URL against teams.ballpark_url for the current season
        # Strip /edit?... suffix so the base spreadsheet URL matches teams.ballpark_url
        _base_url = url.split("/edit")[0] if "/edit" in url else url
        _stadium_sheets = db.get_stadium_sheets(_base_url, _MLN_QS_SEASON)
        st.session_state["_dbg_base_url"]       = _base_url
        st.session_state["_dbg_stadium_sheets"] = _stadium_sheets
        _scenario_urls = {
            k: _stadium_sheets[k]
            for k in ("sheet_hnr", "sheet_ifinfield", "sheet_hnr_ifin")
            if _stadium_sheets.get(k)
        }
        if _scenario_urls:
            _scenario_ranges = utils.fetch_scenario_ranges(_scenario_urls)
            st.session_state["pred_hnr_ranges"]       = _scenario_ranges.get("sheet_hnr")
            st.session_state["pred_ifinfield_ranges"] = _scenario_ranges.get("sheet_ifinfield")
            st.session_state["pred_hnr_ifin_ranges"]  = _scenario_ranges.get("sheet_hnr_ifin")
            st.session_state["_dbg_scenario_errors"]  = _scenario_ranges.get("_errors", {})
        else:
            for _k in ("pred_hnr_ranges", "pred_ifinfield_ranges", "pred_hnr_ifin_ranges"):
                st.session_state.pop(_k, None)

    # Auto-fetch saved/default sheet on first page load
    _auto_url = st.session_state.get("pred_sheet_sel") or (sheet_urls[0] if sheet_urls else None)
    if "pred_result_ranges" not in st.session_state and "_auto_fetch_done" not in st.session_state and _auto_url:
        _auto_fetch_err = None
        try:
            _run_fetch(_auto_url)
        except Exception as _e:
            _auto_fetch_err = _e
        st.session_state["_auto_fetch_done"] = True
        if _auto_fetch_err:
            st.warning(f"Auto-fetch failed: {_auto_fetch_err}")
        else:
            st.rerun()

    result_ranges = st.session_state.get("pred_result_ranges")
    _sp = st.session_state.get("pred_sheet_pitcher", "")
    _sb = st.session_state.get("pred_sheet_batter", "")
    _sheet_swing_type = st.session_state.get("pred_sheet_swing_type", "Normal Swing")
    _sheet_if_in      = st.session_state.get("pred_sheet_if_in", False)
    matchup_label = " vs ".join(filter(None, [_sp, _sb]))
    _badge_parts = []
    if _sheet_swing_type and _sheet_swing_type != "Normal Swing":
        _badge_parts.append(_sheet_swing_type)
    if _sheet_if_in:
        _badge_parts.append("Infield In")
    if _badge_parts:
        matchup_label += f"  [{' | '.join(_badge_parts)}]"

_has_ranges = bool(result_ranges)

with st.expander("Matchup Setup", expanded=not _has_ranges):
    st.radio("Source", ["Fetch Live Matchup", "Historical / Manual"],
             horizontal=True, key="pred_mode")

    if pred_mode == "Historical / Manual":
        with st.expander("Import from History", expanded=True):
            # ── Search Filters ────────────────────────────────────────────────────
            st.markdown("**Search Filters**")
            _sc1, _sc2 = st.columns(2)
            with _sc1:
                srch_seasons = st.multiselect("Season", _meta_seasons, default=_meta_seasons, key="srch_seasons")

            _sc3, _sc4, _sc5, _sc6 = st.columns(4)
            with _sc3:
                srch_pt = st.selectbox("Pitcher Team", ["All"] + _all_teams, key="srch_pt", on_change=_on_srch_pt)
            with _sc4:
                srch_pitcher = st.selectbox("Pitcher", ["All"] + _players_for_team(srch_pt), key="srch_pitcher")
            with _sc5:
                srch_bt = st.selectbox("Batter Team", ["All"] + _all_teams, key="srch_bt", on_change=_on_srch_bt)
            with _sc6:
                srch_batter = st.selectbox("Batter", ["All"] + _players_for_team(srch_bt), key="srch_batter")

            # Load plays on demand - require pitcher or batter selection
            _dv = st.session_state.get("_data_v", 0)
            if srch_pitcher != "All":
                _srch_raw = _load_pitcher_plays(srch_pitcher, _leagues_tuple, _dv)
            elif srch_batter != "All":
                _srch_raw = _load_batter_plays(srch_batter, _leagues_tuple, _dv)
            else:
                _srch_raw = pd.DataFrame()

            df_srch = _srch_raw.copy()
            if not df_srch.empty:
                if srch_seasons:
                    df_srch = df_srch[df_srch["season"].isin(srch_seasons)]
                if srch_pt != "All":
                    df_srch = df_srch[df_srch["def_team"] == srch_pt]
                if srch_pitcher != "All":
                    df_srch = df_srch[df_srch["pitcher_name"] == srch_pitcher]
                if srch_bt != "All":
                    df_srch = df_srch[df_srch["off_team"] == srch_bt]
                if srch_batter != "All":
                    df_srch = df_srch[df_srch["batter_name"] == srch_batter]

            if _srch_raw.empty and srch_pitcher == "All" and srch_batter == "All":
                st.caption("Select a pitcher or batter above to browse plays.")

            st.divider()

            # ── Play picker ───────────────────────────────────────────────────────
            _h_play = _play_picker(df_srch, "pred_hist_season", "pred_hist_game", "pred_hist_play")
            col_hi, col_hinfo = st.columns([1, 5])
            with col_hi:
                if _h_play is not None and st.button("Import Play", type="primary",
                                                      use_container_width=True, key="pred_hist_import"):
                    _import_play(int(_h_play), df_srch)
                    st.session_state["pred_hist_loaded_id"] = int(_h_play)
                    _imp_row = df_srch[df_srch["id"] == int(_h_play)]
                    if not _imp_row.empty:
                        st.session_state["pred_hist_play_data"] = _imp_row.iloc[0].to_dict()
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
                _hpr2 = st.session_state.get("pred_hist_play_data", {})
                if _hpr2.get("id") == hist_id:
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

        with st.expander("Range Calculator", expanded=True):
            _render_calc_inputs()
            st.divider()
            _gsi1, _gsi2, _gsi3, _gsi4 = st.columns(4)
            with _gsi1:
                st.number_input("Inning", 1, _mgr_max_inning, step=1, key="mgr_inning")
            with _gsi2:
                st.selectbox("Half", ["Top", "Bottom"], key="mgr_half")
            with _gsi3:
                st.number_input("Away Score", 0, 99, step=1, key="mgr_away_score")
            with _gsi4:
                st.number_input("Home Score", 0, 99, step=1, key="mgr_home_score")

    elif pred_mode == "Fetch Live Matchup":
        with st.expander("Live Matchup", expanded=not _has_ranges):
            col_sh, col_btn = st.columns([3, 1])
            with col_sh:
                if sheet_urls:
                    pred_sheet_url = st.selectbox(
                        "Session sheet", sheet_urls, key="pred_sheet_sel",
                        format_func=lambda url: _sheet_label_map.get(url, url),
                    )
                else:
                    st.caption("No sheets linked to any games.")
                    pred_sheet_url = None
            with col_btn:
                st.write("")
                if sheet_urls and st.button("Fetch Matchup", type="secondary", key="pull_ranges"):
                    try:
                        _run_fetch(pred_sheet_url)
                        _uid = st.session_state.get("user_id")
                        if _uid and pred_sheet_url:
                            try:
                                db.upsert_last_sheet_url(_uid, pred_sheet_url)
                            except Exception:
                                pass
                        _bunt_msg  = " + bunt ranges" if st.session_state.get("pred_bunt_ranges") else ""
                        _runners   = st.session_state.get("steal_runner_data", [])
                        _steal_msg = f" + {len(_runners)} runner(s)" if _runners else ""
                        _outs      = st.session_state.get("mgr_sheet_outs")
                        _state_msg = (f" + game state ({_outs} outs, "
                                      f"{utils.obc_display(st.session_state.get('mgr_sheet_obc','000'))})"
                                      if _outs is not None else "")
                        _nr = st.session_state.get("pred_result_ranges", [])
                        st.toast(f"Loaded {len(_nr)} ranges{_bunt_msg}{_steal_msg}{_state_msg}.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

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

            if result_ranges:
                st.divider()
                _gsi1, _gsi2, _gsi3, _gsi4 = st.columns(4)
                with _gsi1:
                    st.number_input("Inning", 1, _mgr_max_inning, step=1, key="mgr_inning")
                with _gsi2:
                    st.selectbox("Half", ["Top", "Bottom"], key="mgr_half")
                with _gsi3:
                    st.number_input("Away Score", 0, 99, step=1, key="mgr_away_score")
                with _gsi4:
                    st.number_input("Home Score", 0, 99, step=1, key="mgr_home_score")

if pred_mode == "Fetch Live Matchup" and result_ranges:
    _gs_obc  = st.session_state.get("mgr_sheet_obc", "000")
    _gs_outs = int(st.session_state.get("mgr_sheet_outs") or 0)
    _gs_inn  = st.session_state.get("mgr_inning", 1)
    _gs_half = st.session_state.get("mgr_half", "Top")
    _gs_tri  = "▲" if _gs_half == "Top" else "▼"
    _gs_away = st.session_state.get("mgr_away_score", 0)
    _gs_home = st.session_state.get("mgr_home_score", 0)
    _gs_gm   = next((g for g in all_games_meta if g.get("id") == st.session_state.get("game_tab_sel")), None)
    _gs_away_lbl = _gs_gm.get("away_team", "Away") if _gs_gm else "Away"
    _gs_home_lbl = _gs_gm.get("home_team", "Home") if _gs_gm else "Home"
    _c_left, _c_right = st.columns([1, 3])
    with _c_left:
        _c_diag, _c_info = st.columns([3, 2])
        with _c_diag:
            st.markdown(utils.bases_diamond_svg(_gs_obc, _gs_outs), unsafe_allow_html=True)
        with _c_info:
            st.markdown(f"**{_gs_tri} {_gs_inn}**")
    with _c_right:
        st.caption(f"{_gs_away_lbl} **{_gs_away}** - {_gs_home_lbl} **{_gs_home}**")
        _c_pit2, _c_bat2 = st.columns(2)
        with _c_pit2:
            if _sp:
                _sp_rec = _pbyn.get(_sp, {})
                st.markdown(f"**{_sp}** (P)")
                st.caption(_player_attrs_md(_sp_rec, "pitcher"))
        with _c_bat2:
            if _sb:
                _sb_rec = _pbyn.get(_sb, {})
                st.markdown(f"**{_sb}** (B)")
                st.caption(_player_attrs_md(_sb_rec, "batter"))

# ── ITD slices (shared placeholder - actual slicing is per-tab) ───────────────

st.divider()

# ── tabs ──────────────────────────────────────────────────────────────────────

tab_p, tab_b, tab_m, tab_g = st.tabs(["⚾ Pitcher", "🦇 Batter", "📊 Manager", "📈 Game"])

# ══════════════════════════════════════════════════════════════════════════════
# PITCHER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_p:
    # ── per-tab data filters ──────────────────────────────────────────────────
    with st.expander("Data Filters", expanded=False):
        _pf1, _pf2 = st.columns(2)
        with _pf1:
            tab_p_seasons = st.multiselect("Season", _meta_seasons, default=_meta_seasons, key="tab_p_seasons")
        _pf3, _pf4 = st.columns(2)
        with _pf3:
            if st.session_state.get("tab_p_team", "All") not in (["All"] + _all_teams):
                st.session_state["tab_p_team"] = "All"
            tab_p_team = st.selectbox("Pitcher Team", ["All"] + _all_teams,
                                      key="tab_p_team", on_change=_on_tab_p_team)
        with _pf4:
            _tab_p_pitchers = _players_for_team(tab_p_team)
            if st.session_state.get("tab_p_pitcher", "All") not in (["All"] + _tab_p_pitchers):
                st.session_state["tab_p_pitcher"] = "All"
            tab_p_pitcher = st.selectbox("Pitcher", ["All"] + _tab_p_pitchers,
                                         key="tab_p_pitcher")

    # Build pitcher tab df on demand
    if tab_p_pitcher != "All":
        _p_dfs = []
        if _source_key in ("real", "all") and _leagues_tuple:
            _p_dfs.append(_load_pitcher_plays(tab_p_pitcher, _leagues_tuple, st.session_state.get("_data_v", 0)))
        if _source_key in ("scrimmage", "all") and not _scrimmage_df.empty:
            _p_scrim = _scrimmage_df[_scrimmage_df.get("pitcher_name", pd.Series(dtype=str)) == tab_p_pitcher] \
                if "pitcher_name" in _scrimmage_df.columns else pd.DataFrame()
            if not _p_scrim.empty:
                _p_dfs.append(_p_scrim)
        df_p = pd.concat(_p_dfs, ignore_index=True) if _p_dfs else pd.DataFrame()
        if not df_p.empty and tab_p_seasons:
            _is_scrim_p = (df_p.get("game_type") == "scrimmage") if "game_type" in df_p.columns else pd.Series(False, index=df_p.index)
            df_p = df_p[df_p["season"].isin(tab_p_seasons) | _is_scrim_p]
        if not df_p.empty and tab_p_team != "All":
            df_p = df_p[df_p["def_team"] == tab_p_team]
    else:
        df_p = pd.DataFrame()

    df_p_pred = df_p[df_p["id"] < hist_id] if hist_id else df_p

    # ── recent PA window - shared by the percentile card and the Last N chart ──
    if tab_p_pitcher != "All":
        n_pitches = st.slider("Recent PA Window", 5, 100, 20, step=5, key="last_n_pitch")
    else:
        n_pitches = st.session_state.get("last_n_pitch", 20)

    # ── percentile card ───────────────────────────────────────────────────────
    if tab_p_pitcher != "All" and not df_p.empty:
        _pitcher_stats_df = _load_pitcher_stats()
        _recent_df    = df_p.sort_values("id").tail(n_pitches)
        _recent_stats = utils.compute_recent_pitcher_stats(_recent_df)
        _recent_n     = int(_recent_df["swing"].notna().sum())
        _pct_fig = utils.pitcher_percentile_card(
            tab_p_pitcher, _pitcher_stats_df,
            recent_vals=_recent_stats if _recent_stats else None,
            recent_n=_recent_n if _recent_stats else None,
        )
        with st.expander("Behavioral Tendencies", expanded=not _simple_mode):
            if _pct_fig is not None:
                st.plotly_chart(_pct_fig, width="stretch",
                                config={"displayModeBar": False}, key="p_pct_card")
            else:
                st.caption("No career stats on file - run Refresh Pitcher Stats on the Games page.")

        with st.expander("Tendencies Over Time", expanded=False):
            @st.fragment
            def _ma_section_p(df):
                _metric = st.radio(
                    "Metric",
                    options=list(utils._MA_METRICS.keys()),
                    format_func=lambda m: utils._MA_METRICS[m]["label"],
                    horizontal=True,
                    label_visibility="collapsed",
                    key="ma_metric_p",
                )
                _fig = utils.pitcher_ma_figure(df, _metric)
                if _fig is not None:
                    st.plotly_chart(_fig, use_container_width=True,
                                    config={"displayModeBar": False}, key="ma_chart_p")
                else:
                    st.caption("Not enough data.")
            _ma_section_p(df_p)

    if df_p.empty:
        if tab_p_pitcher == "All":
            st.info("Select a pitcher in the filters above to load data.")
        else:
            st.warning("No at-bats found for this pitcher with the current filters.")
    else:
        # ── swing suggestions panel ───────────────────────────────────────────
        with st.expander("Swing Suggestions", expanded=True):
            _h_outs   = int(st.session_state.get("mgr_sheet_outs") or 0)
            _h_obc    = st.session_state.get("mgr_sheet_obc") or "000"
            _h_dd_bkt = st.session_state.get("dd_bucket_p", 100)
            _h_hz_bkt = st.session_state.get("hz_init_bucket_p", 200)

            _h_recent = (
                df_p_pred[df_p_pred["pitch"].notna()].sort_values("id").tail(n_pitches)["pitch"]
                .astype(int).tolist()
            ) if not df_p_pred.empty else []
            _h_delta_hist = (
                df_p[df_p["pitch_circ_delta"].notna()].sort_values("id")["pitch_circ_delta"]
                .abs().astype(int).tolist()
            ) if "pitch_circ_delta" in df_p.columns else []
            _h_diff_hist = (
                df_p[df_p["diff"].notna()].sort_values("id")["diff"].abs().astype(int).tolist()
            ) if "diff" in df_p.columns else []

            _h_prior_pitch  = _h_recent[-1]      if len(_h_recent) >= 1 else None
            _h_prior_pitch2 = _h_recent[-2]      if len(_h_recent) >= 2 else None
            _h_prior_delta  = _h_delta_hist[-1]  if len(_h_delta_hist) >= 1 else None
            _h_prior_delta2 = _h_delta_hist[-2]  if len(_h_delta_hist) >= 2 else None
            _h_prior_diff   = _h_diff_hist[-1]   if len(_h_diff_hist)  >= 1 else None

            def _hstr(prob, n, n_bkts):
                pct = prob * 100
                exp = 100.0 / n_bkts
                return f"{pct:.0f}% ({pct-exp:+.0f}%) n={n}"

            _hint_rows_p = []
            _h_dd_n = 500 // _h_dd_bkt
            _h_hz_n = 1000 // _h_hz_bkt
            _hint_centered_p = st.session_state.get("hint_centered_p", True)

            # OBP rows (always at top when matchup is loaded)
            if result_ranges and _h_recent:
                _h_pa_df = (
                    df_p_pred[df_p_pred["pitch"].notna()].sort_values("id").tail(n_pitches)
                    if not df_p_pred.empty else pd.DataFrame()
                )
                _h_rel_kw = dict(
                    recency_slider=st.session_state.get("p_rel_recency", 50),
                    result_slider=st.session_state.get("p_rel_result", 50),
                    state_slider=st.session_state.get("p_rel_state", 0),
                    g1=st.session_state.get("p_rel_g1", 20),
                    g2=st.session_state.get("p_rel_g2", 40),
                    g3=st.session_state.get("p_rel_g3", 40),
                    result_offset=bool(st.session_state.get("p_rel_result_offset", True)),
                )
                _h_wts   = utils.compute_pa_weights(_h_pa_df, _h_obc, _h_outs, **_h_rel_kw) if not _h_pa_df.empty else []
                _h_dwts  = _h_wts[1:] if len(_h_wts) > 1 else None
                _h_d2wts = [w for w in _h_wts[2:] for _ in range(2)] if len(_h_wts) > 2 else None
                _h_dvals  = utils.project_from_deltas(_h_recent)
                _h_d2vals = utils.project_from_delta2s(_h_recent)
                _h_obp_p  = utils.optimal_swing_range(_h_recent,  result_ranges, "obp", True, _h_wts or None)
                _h_obp_d  = utils.optimal_swing_range(_h_dvals,   result_ranges, "obp", True, _h_dwts)
                _h_obp_d2 = utils.optimal_swing_range(_h_d2vals,  result_ranges, "obp", True, _h_d2wts)
                _h_ss_p   = utils.swing_signal_strength(_h_recent,  result_ranges, "obp", True, _h_wts or None)
                _h_ss_d   = utils.swing_signal_strength(_h_dvals,   result_ranges, "obp", True, _h_dwts)  if _h_dvals  else 0.0
                _h_ss_d2  = utils.swing_signal_strength(_h_d2vals,  result_ranges, "obp", True, _h_d2wts) if _h_d2vals else 0.0
                _hint_rows_p.append({"Signal": "OBP recent pitch",
                                     "lo": _h_obp_p[0] if _h_obp_p else None,
                                     "hi": _h_obp_p[1] if _h_obp_p else None,
                                     "Strength": f"{_h_ss_p:.0f}% signal",
                                     "_best_zone_only": True})
                _hint_rows_p.append({"Signal": "OBP recent Δ",
                                     "lo": _h_obp_d[0] if _h_obp_d else None,
                                     "hi": _h_obp_d[1] if _h_obp_d else None,
                                     "Strength": f"{_h_ss_d:.0f}% signal",
                                     "_best_zone_only": True})
                _hint_rows_p.append({"Signal": "OBP recent Δ²",
                                     "lo": _h_obp_d2[0] if _h_obp_d2 else None,
                                     "hi": _h_obp_d2[1] if _h_obp_d2 else None,
                                     "Strength": f"{_h_ss_d2:.0f}% signal",
                                     "_best_zone_only": True})

            def _delta_row_p(signal, h_dict, n_bkts):
                _zs = utils.hint_zscore(h_dict["prob"], h_dict["n"], n_bkts)
                _dc = h_dict.get("all_counts")
                _dd_bkt_z = h_dict.get("delta_bucket_size", _h_dd_bkt)
                extra = []
                if _h_prior_pitch is not None:
                    _all_bkts = [(h_dict["delta_lo"], h_dict["delta_hi"])] + h_dict.get("tied_buckets", [])
                    _merged = utils.merge_delta_ranges(_all_bkts)
                    _arms = []
                    for _mlo, _mhi in _merged:
                        _arms.extend(utils.delta_to_pitch_ranges(_h_prior_pitch, _mlo, _mhi))
                    r1 = _arms[0] if len(_arms) > 0 else (None, None)
                    r2 = _arms[1] if len(_arms) > 1 else (None, None)
                    extra = [a for a in _arms[2:] if a[0] is not None]
                    return {"Signal": signal,
                            "lo": r1[0], "hi": r1[1],
                            "lo2": r2[0], "hi2": r2[1],
                            "extra_ranges": extra,
                            "Strength": _hstr(h_dict["prob"], h_dict["n"], n_bkts),
                            "_zscore": _zs,
                            "_delta_counts": _dc,
                            "_prior_pitch_for_zone": _h_prior_pitch,
                            "_dd_bkt_for_zone": _dd_bkt_z}
                return {"Signal": signal, "lo": None, "hi": None,
                        "extra_ranges": extra,
                        "Strength": _hstr(h_dict["prob"], h_dict["n"], n_bkts),
                        "_zscore": _zs,
                        "_delta_counts": _dc,
                        "_prior_pitch_for_zone": _h_prior_pitch,
                        "_dd_bkt_for_zone": _dd_bkt_z}

            if _h_prior_delta is not None:
                _h = utils.seq2_delta_hint(df_p, "pitch", _h_dd_bkt, _h_prior_delta, centered=_hint_centered_p)
                if _h:
                    _hint_rows_p.append(_delta_row_p("2-Δ seq", _h, _h_dd_n))

            if _h_prior_delta is not None and _h_prior_delta2 is not None:
                _h = utils.seq3_delta_hint(df_p, "pitch", _h_dd_bkt, _h_prior_delta2, _h_prior_delta, centered=_hint_centered_p)
                if _h:
                    _hint_rows_p.append(_delta_row_p("3-Δ seq", _h, _h_dd_n))

            if _h_prior_diff is not None:
                _h = utils.diff_to_delta_hint(df_p, "pitch", _h_prior_diff, centered=_hint_centered_p)
                if _h:
                    _hint_rows_p.append(_delta_row_p("Prior diff → Δ", _h, 5))

            if _h_prior_pitch is not None:
                _h = utils.seq2_hint(df_p, "pitch", _h_hz_bkt, _h_prior_pitch, centered=_hint_centered_p)
                if _h:
                    _hint_rows_p.append({"Signal": "2-pitch seq",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "Strength": _hstr(_h["prob"], _h["n"], _h_hz_n),
                                         "_zone_dist": utils.seq2_zone_dist(df_p, "pitch", _h_hz_bkt, _h_prior_pitch, centered=_hint_centered_p),
                                         "_zone_bucket_size": _h_hz_bkt,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], _h_hz_n)})

            if _h_prior_pitch is not None and _h_prior_pitch2 is not None:
                _h = utils.seq3_hint(df_p, "pitch", _h_hz_bkt, _h_prior_pitch2, _h_prior_pitch, centered=_hint_centered_p)
                if _h:
                    _hint_rows_p.append({"Signal": "3-pitch seq",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "Strength": _hstr(_h["prob"], _h["n"], _h_hz_n),
                                         "_zone_dist": utils.seq3_zone_dist(df_p, "pitch", _h_hz_bkt, _h_prior_pitch2, _h_prior_pitch, centered=_hint_centered_p),
                                         "_zone_bucket_size": _h_hz_bkt,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], _h_hz_n)})

            if "outs" in df_p.columns:
                _h = utils.best_zone_hint(df_p[df_p["outs"] == _h_outs], "pitch")
                if _h:
                    _hint_rows_p.append({"Signal": f"Outs({_h_outs})",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            if "obc" in df_p.columns:
                _obc_is_zero = (_h_obc == "000")
                _obc_mask_p  = df_p["obc"] == "000" if _obc_is_zero else df_p["obc"] != "000"
                _obc_lbl_p   = "Empty" if _obc_is_zero else "Runner(s) on"
                _h = utils.best_zone_hint(df_p[_obc_mask_p], "pitch")
                if _h:
                    _hint_rows_p.append({"Signal": f"Base state ({_obc_lbl_p})",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            _cur_game_id  = st.session_state.get("game_tab_sel")
            _cur_inn      = int(st.session_state.get("mgr_inning", 1))
            _p_last_sw    = df_p[df_p["pitch"].notna()].sort_values("id")
            _p_last_row   = _p_last_sw.iloc[-1] if not _p_last_sw.empty else None
            _p_last_game  = (int(_p_last_row["game_id"]) if _p_last_row is not None
                             and pd.notna(_p_last_row.get("game_id")) else None)
            _p_last_inn   = (int(_p_last_row["inning"]) if _p_last_row is not None
                             and pd.notna(_p_last_row.get("inning")) else None)
            _show_fp_app_p = (_cur_game_id is not None
                              and (_p_last_game is None or _p_last_game != _cur_game_id))
            _show_fp_inn_p = (_cur_game_id is not None
                              and (_p_last_game is None or _p_last_game != _cur_game_id
                                   or _p_last_inn != _cur_inn))

            if "is_fp_inn" in df_p.columns and _show_fp_inn_p:
                _h = utils.best_zone_hint(df_p[df_p["is_fp_inn"] == True], "pitch")
                if _h:
                    _hint_rows_p.append({"Signal": "1st pitch inning",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            if "is_fp_app" in df_p.columns and _show_fp_app_p:
                _h = utils.best_zone_hint(df_p[df_p["is_fp_app"] == True], "pitch")
                if _h:
                    _hint_rows_p.append({"Signal": "1st pitch appearance",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            # Sort non-OBP rows by z-score descending; OBP rows stay pinned at top
            _obp_rows_p = [r for r in _hint_rows_p if r["Signal"].startswith("OBP")]
            _seq_rows_p = [r for r in _hint_rows_p if not r["Signal"].startswith("OBP")]
            _seq_rows_p.sort(key=lambda r: r.get("_zscore", 0.0), reverse=True)
            _hint_rows_p = _obp_rows_p + _seq_rows_p

            if _hint_rows_p:
                _tc1, _tc2 = st.columns(2)
                with _tc1:
                    _hint_all_p = st.toggle("All zones", value=False, key="hint_all_p")
                with _tc2:
                    _hint_centered_p = st.toggle("Centered", value=True, key="hint_centered_p")
                _hp_swing_val = int(st.session_state["pred_swing"]) if "pred_swing" in st.session_state and result_ranges else None
                _hp_obr_lo = _hp_obr_hi = None
                if _hp_swing_val is not None:
                    _hp_obr_max = max((hi for _, _lo, hi in result_ranges if _ in utils._OBR), default=0)
                    if _hp_obr_max:
                        _hp_obr_lo = ((_hp_swing_val - _hp_obr_max - 1) % 1000) + 1
                        _hp_obr_hi = ((_hp_swing_val + _hp_obr_max - 1) % 1000) + 1
                st.plotly_chart(
                    utils.hint_bars_figure(
                        _hint_rows_p,
                        mode="all" if _hint_all_p else "best",
                        mobile=_is_mobile,
                        prior_val=_h_prior_pitch,
                        prior_val2=_h_prior_pitch2,
                        swing_val=_hp_swing_val,
                        obr_lo=_hp_obr_lo,
                        obr_hi=_hp_obr_hi,
                    ),
                    use_container_width=True,
                    config={"displayModeBar": False},
                )
            elif not result_ranges or not _h_recent:
                st.caption("Fetch a matchup and select a pitcher to see suggestions.")
            else:
                st.caption("Not enough history to generate sequence suggestions.")

        # ── swing predictor ───────────────────────────────────────────────────
        st.subheader("Swing Analyzer")
        st.caption("Enter a proposed swing to see what each of this pitcher's recent pitches would give.")
        _lnc_weights_p: list[float] = []

        if result_ranges:
            if "_pend_swing" in st.session_state:
                st.session_state["pred_swing"] = st.session_state.pop("_pend_swing")
            for _pk in st.session_state.pop("_pills_rst_p", []):
                st.session_state[_pk] = None

            with st.expander("Relevance Weighting", expanded=not _simple_mode):
                st.caption("Weight how recent pitches influence the Optimal Swing. At 50 on Recency and Result and 0 on State, weighting is equal for each recent pitch.")
                _prw1, _prw2, _prw3 = st.columns(3)
                with _prw1:
                    st.markdown("**1 - Recency**")
                    st.slider("Older vs Newer", 0, 100, value=50, key="p_rel_recency",
                              help="50=equal. 0=weight older pitches more. 100=weight recent pitches more.")
                    st.number_input("Weight", 0, 100, value=20, step=1, key="p_rel_g1")
                with _prw2:
                    st.markdown("**2 - Result**")
                    st.slider("Pitcher vs Batter", 0, 100, value=50, key="p_rel_result",
                              help="50=equal. 0=upweight good pitching results (K, DP). 100=upweight good batting results (HR, XBH).")
                    st.number_input("Weight ", 0, 100, value=40, step=1, key="p_rel_g2")
                    st.toggle("Previous result", key="p_rel_result_offset", value=True,
                              help="Weight each pitch by the result of the previous pitch instead of its own result.")
                with _prw3:
                    st.markdown("**3 - State**")
                    st.slider("Any vs Similar", 0, 100, value=0, key="p_rel_state",
                              help="50=equal. 100=upweight pitches from similar OBC + outs situations.")
                    st.number_input("Weight  ", 0, 100, value=40, step=1, key="p_rel_g3")
                _prg1 = st.session_state.get("p_rel_g1", 20)
                _prg2 = st.session_state.get("p_rel_g2", 40)
                _prg3 = st.session_state.get("p_rel_g3", 40)
                _prg_tot = max(_prg1 + _prg2 + _prg3, 1)
                st.caption(
                    f"Normalized: Recency {_prg1/_prg_tot*100:.0f}% | "
                    f"Result {_prg2/_prg_tot*100:.0f}% | "
                    f"State {_prg3/_prg_tot*100:.0f}%"
                )

            if "pred_swing" not in st.session_state:
                st.session_state["pred_swing"] = 500
            proposed_swing = st.number_input("Proposed Swing", min_value=1, max_value=1000,
                                             step=1, key="pred_swing")

            # PA weights for relevance-weighted optimal swing and swing predictor coloring
            _pa_df_p = (df_p_pred[df_p_pred["pitch"].notna()].sort_values("id").tail(n_pitches)
                        if not df_p_pred.empty else pd.DataFrame())
            _p_cur_obc  = (st.session_state.get("mgr_sheet_obc") or "000")
            _p_cur_outs = int(st.session_state.get("mgr_sheet_outs") or 0)
            _p_rel_kwargs = dict(
                recency_slider=st.session_state.get("p_rel_recency", 50),
                result_slider=st.session_state.get("p_rel_result",  50),
                state_slider=st.session_state.get("p_rel_state",   50),
                g1=st.session_state.get("p_rel_g1", 20),
                g2=st.session_state.get("p_rel_g2", 40),
                g3=st.session_state.get("p_rel_g3", 40),
                result_offset=bool(st.session_state.get("p_rel_result_offset", True)),
            )
            _pa_weights_p = utils.compute_pa_weights(
                _pa_df_p, _p_cur_obc, _p_cur_outs, **_p_rel_kwargs,
            ) if not _pa_df_p.empty else []
            _lnc_df_p = (df_p_pred[df_p_pred["pitch"].notna() & df_p_pred["swing"].notna()]
                         .sort_values("id").tail(n_pitches)
                         if not df_p_pred.empty else pd.DataFrame())
            _lnc_weights_p = utils.compute_pa_weights(
                _lnc_df_p, _p_cur_obc, _p_cur_outs, **_p_rel_kwargs,
            ) if not _lnc_df_p.empty else []

            _df_tick_p = _pa_df_p if not _pa_df_p.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_p = f"Last {n_pitches} pitches (pre-AB)" if hist_id and not df_p_pred.empty \
                          else f"Last {n_pitches} pitches"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_p, swing=int(proposed_swing), n=n_pitches,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_p,
                                            tick_weights=_pa_weights_p),
                width="stretch", key="p_swing_pred",
            )

            st.markdown("**Optimal Swing**")
            _recent_p = _pa_df_p["pitch"].astype(int).tolist() if not _pa_df_p.empty else []
            _delta_p  = utils.project_from_deltas(_recent_p)
            _delta2_p = utils.project_from_delta2s(_recent_p)
            _delta_weights_p  = (_pa_weights_p[1:] if len(_pa_weights_p) > 1 else None)
            _delta2_weights_p = (
                [w for w in _pa_weights_p[2:] for _ in range(2)]
                if len(_pa_weights_p) > 2 else None
            )
            _opt_rows_p = [
                ("Based on Recent Pitch Values", _recent_p,  _pa_weights_p or None),
                ("Based on Recent Pitch Δ",      _delta_p,   _delta_weights_p),
                ("Based on Recent Pitch Δ²",     _delta2_p,  _delta2_weights_p),
            ]
            st.markdown("""<style>
button[data-testid="stBaseButton-pills"] {
    border: 1.5px solid #2ca02c !important;
}
button[data-testid="stBaseButton-pills"] + button[data-testid="stBaseButton-pills"] {
    border: 1.5px solid #d62728 !important;
}
</style>""", unsafe_allow_html=True)
            if _simple_mode:
                col_obp_p = st.columns(1)[0]
            else:
                col_obp_p, col_slg_p = st.columns(2)
            with col_obp_p:
                st.markdown("**OBP Optimal Swing**" if _simple_mode else "**OBP**")
                for _i, (_lbl, _vals, _wts) in enumerate(_opt_rows_p):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-1.3rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "obp", True, weights=_wts)
                        _sig_p_obp_tgt = utils.swing_signal_strength(_vals, result_ranges, "obp", True, weights=_wts, zone="best")
                        _sig_p_obp_avd = utils.swing_signal_strength(_vals, result_ranges, "obp", True, weights=_wts, zone="worst")
                        _pk = f"pill_obp_{_i}_p"
                        _opts = {
                            f"↑ {_bv} ({_bs:.3f}) · {_sig_p_obp_tgt:.0f}%": _bv,
                            f"↓ {_cv} ({_cs:.3f}) · {_sig_p_obp_avd:.0f}%": _cv,
                        }
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_swing"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_p", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(
                            utils.optimal_swing_chart(_vals, result_ranges, "obp", True,
                                                      compact=True, weights=_wts),
                            use_container_width=True, key=f"p_opt_obp_{_i}")
            if not _simple_mode:
                with col_slg_p:
                    st.markdown("**SLG**")
                    for _i, (_lbl, _vals, _wts) in enumerate(_opt_rows_p):
                        st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-1.3rem'>{_lbl}</div>",
                                    unsafe_allow_html=True)
                        if _vals:
                            _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "slg", True, weights=_wts)
                            _sig_p_slg_tgt = utils.swing_signal_strength(_vals, result_ranges, "slg", True, weights=_wts, zone="best")
                            _sig_p_slg_avd = utils.swing_signal_strength(_vals, result_ranges, "slg", True, weights=_wts, zone="worst")
                            _pk = f"pill_slg_{_i}_p"
                            _opts = {
                                f"↑ {_bv} ({_bs:.3f}) · {_sig_p_slg_tgt:.0f}%": _bv,
                                f"↓ {_cv} ({_cs:.3f}) · {_sig_p_slg_avd:.0f}%": _cv,
                            }
                            _sel = st.pills("", list(_opts.keys()), key=_pk)
                            if _sel:
                                st.session_state["_pend_swing"] = _opts[_sel]
                                st.session_state.setdefault("_pills_rst_p", []).append(_pk)
                                st.rerun()
                            st.plotly_chart(
                                utils.optimal_swing_chart(_vals, result_ranges, "slg", True,
                                                          compact=True, weights=_wts),
                                use_container_width=True, key=f"p_opt_slg_{_i}")
        else:
            if pred_mode == "Fetch Live Matchup":
                st.info("Select a matchup above to enable the predictor.")

        # ── last N pitches ────────────────────────────────────────────────────
        st.divider()
        _actual_pitches_p = len(df_p_pred.sort_values("id").tail(n_pitches)) if not df_p_pred.empty else 0
        st.subheader(f"Last {_actual_pitches_p} Pitches")
        _p_chart_c1, _p_chart_c2 = st.columns([3, 2])
        with _p_chart_c1:
            swing_off_p = st.radio("Swing offset", ["Off", "+1"], horizontal=True, key="swing_off_p",
                                   help="+1: shifts swing markers right by one AB.")
        with _p_chart_c2:
            est_delta_p = st.toggle("Est. Δ overlay", key="est_delta_p", value=False,
                                    help="Shows batter's estimated delta (swing vs prior pitch) as a diamond line on the delta chart.")
        st.plotly_chart(
            utils.last_n_combined_chart(df_p_pred, n=n_pitches, delta_col="pitch",
                                        title=f"Last {_actual_pitches_p} Pitches",
                                        swing_offset=(swing_off_p == "+1"),
                                        segment_games=True,
                                        tick_weights=_lnc_weights_p or None,
                                        pannable=True,
                                        est_delta_overlay=est_delta_p),
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
        with st.expander("Pitch Analysis", expanded=not _simple_mode):
            st.subheader("Next Pitch Delta vs Prior Pitch Delta")
            st.caption("How does a pitcher adjust their next pitch movement based on their previous pitch movement?")
            dd_bucket_p = st.select_slider("Bucket size", options=[25, 50, 100, 125, 250, 500], value=100, key="dd_bucket_p")
            st.plotly_chart(
                utils.next_delta_vs_prior_delta_heatmap(df_p, title="Next Pitch Δ vs Prior Pitch Δ", value_col="pitch", bucket_size=dd_bucket_p),
                width="stretch", config={"displayModeBar": False}, key="p_delta_delta_hm",
            )

            _dd_p_abs_hist = (
                df_p[df_p["pitch_circ_delta"].notna()]
                .sort_values("id")["pitch_circ_delta"].abs().astype(int).tolist()
            )
            _dd_p_def_init   = _dd_p_abs_hist[-2] if len(_dd_p_abs_hist) >= 2 else 250
            _dd_p_def_follow = _dd_p_abs_hist[-1] if _dd_p_abs_hist else 250
            _ddc1_p, _ddc2_p = st.columns(2)
            with _ddc1_p:
                _dd_p_init_val = st.number_input(
                    "Initial |Δ|", min_value=0, max_value=500,
                    value=_dd_p_def_init, step=1, key=f"dd_p_init_{tab_p_pitcher}",
                )
            with _ddc2_p:
                _dd_p_follow_val = st.number_input(
                    "Following |Δ|", min_value=0, max_value=500,
                    value=_dd_p_def_follow, step=1, key=f"dd_p_follow_{tab_p_pitcher}",
                )
            _n_bkts_p = 500 // dd_bucket_p
            _dd_p_ii = min(max(0, (_dd_p_init_val - 1) // dd_bucket_p if _dd_p_init_val > 0 else 0), _n_bkts_p - 1)
            _dd_p_fi = min(max(0, (_dd_p_follow_val - 1) // dd_bucket_p if _dd_p_follow_val > 0 else 0), _n_bkts_p - 1)
            _dd_p_init_lbl   = f"{_dd_p_ii * dd_bucket_p}-{(_dd_p_ii + 1) * dd_bucket_p}"
            _dd_p_follow_lbl = f"{_dd_p_fi * dd_bucket_p}-{(_dd_p_fi + 1) * dd_bucket_p}"
            _third_delta_fig_p = utils.delta_third_dist(
                df_p, value_col="pitch", bucket_size=dd_bucket_p,
                init_label=_dd_p_init_lbl, follow_label=_dd_p_follow_lbl,
            )
            if _third_delta_fig_p:
                st.plotly_chart(_third_delta_fig_p, width="stretch",
                                config={"displayModeBar": False}, key="p_delta_third")
            else:
                st.caption("Not enough data for this delta sequence.")

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
            st.caption("How often each pitch range is followed by each other pitch range. Click a cell to see the 3rd pitch distribution.")
            _hzp_sl1, _hzp_sl2 = st.columns(2)
            with _hzp_sl1:
                init_bucket_p   = st.select_slider("Initial bucket size",   options=[50,100,125,200,250,500], value=200, key="hz_init_bucket_p")
            with _hzp_sl2:
                follow_bucket_p = st.select_slider("Following bucket size", options=[50,100,125,200,250,500], value=200, key="hz_follow_bucket_p")
            group_cols_p = ["game_id","pitcher_name"] if tab_p_pitcher != "All" else ["pitcher_name"]

            st.plotly_chart(
                utils.hot_zone_matrix(df_p, value_col="pitch", group_cols=group_cols_p,
                                      init_bucket_size=init_bucket_p, follow_bucket_size=follow_bucket_p),
                width="stretch", key="p_hot_zone",
            )

            _hz_p_recent     = df_p[df_p["pitch"].notna()].sort_values("id")["pitch"].astype(int).tolist()
            _hz_p_def_init   = max(1, _hz_p_recent[-2]) if len(_hz_p_recent) >= 2 else (max(1, _hz_p_recent[-1]) if _hz_p_recent else None)
            _hz_p_def_follow = max(1, _hz_p_recent[-1]) if _hz_p_recent else None
            _hzp_c1, _hzp_c2 = st.columns(2)
            with _hzp_c1:
                _hz_p_init_val   = st.number_input("Initial pitch",   min_value=1, max_value=1000, value=_hz_p_def_init,   step=1, key=f"hz_p_init_{tab_p_pitcher}",   placeholder="1-1000")
            with _hzp_c2:
                _hz_p_follow_val = st.number_input("Following pitch", min_value=1, max_value=1000, value=_hz_p_def_follow, step=1, key=f"hz_p_follow_{tab_p_pitcher}", placeholder="1-1000")

            if _hz_p_init_val is not None and _hz_p_follow_val is not None:
                _p_ii = (int(_hz_p_init_val)   - 1) // init_bucket_p
                _p_fi = (int(_hz_p_follow_val) - 1) // follow_bucket_p
                _hz_p_init_label   = f"{_p_ii * init_bucket_p + 1}-{min((_p_ii + 1) * init_bucket_p, 1000)}"
                _hz_p_follow_label = f"{_p_fi * follow_bucket_p + 1}-{min((_p_fi + 1) * follow_bucket_p, 1000)}"
                _third_fig_p = utils.hot_zone_third_dist(
                    df_p, value_col="pitch", group_cols=group_cols_p,
                    init_bucket_size=init_bucket_p, follow_bucket_size=follow_bucket_p,
                    init_label=_hz_p_init_label, follow_label=_hz_p_follow_label,
                )
                if _third_fig_p:
                    st.plotly_chart(_third_fig_p, width="stretch",
                                    config={"displayModeBar": False}, key="p_third_dist")
                else:
                    st.caption("Not enough data for this sequence.")

            # ── zone charts (shared polar toggle) ────────────────────────────────
            st.divider()
            @st.fragment
            def _zone_delta_section_p(df_p, _deltas_p):
                _polar_p = st.toggle("Polar view", value=True, key="polar_p")

                st.subheader("Zone Distribution (All)")
                _zone_counts_p = df_p["pitch_zone"].value_counts().to_dict()
                if _polar_p:
                    st.plotly_chart(utils.zone_polar(_zone_counts_p, title="Pitch Zone Frequency"),
                                    width="stretch", key="p_zone_all")
                else:
                    st.plotly_chart(utils.zone_heatmap(_zone_counts_p, title="Pitch Zone Frequency"),
                                    width="stretch", key="p_zone_all")

                # ── first pitch tendencies ────────────────────────────────────────────
                st.subheader("First Pitch Tendencies")
                _fpa = df_p[df_p["is_fp_app"] == True]
                _fpi = df_p[df_p["is_fp_inn"] == True]
                col_a_p, col_b_p = st.columns(2)
                with col_a_p:
                    _fpa_counts = _fpa["pitch_zone"].value_counts().to_dict() if not _fpa.empty else {}
                    if _polar_p:
                        st.plotly_chart(utils.zone_polar(_fpa_counts, title="First Pitch of Appearance"),
                                        width="stretch", config={"displayModeBar": False}, key="p_fpa")
                    else:
                        st.plotly_chart(utils.zone_heatmap(_fpa_counts, title=f"First Pitch of Appearance (n={len(_fpa)})"),
                                        width="stretch", config={"displayModeBar": False}, key="p_fpa")
                with col_b_p:
                    _fpi_counts = _fpi["pitch_zone"].value_counts().to_dict() if not _fpi.empty else {}
                    if _polar_p:
                        st.plotly_chart(utils.zone_polar(_fpi_counts, title="First Pitch of Inning"),
                                        width="stretch", config={"displayModeBar": False}, key="p_fpi")
                    else:
                        st.plotly_chart(utils.zone_heatmap(_fpi_counts, title=f"First Pitch of Inning (n={len(_fpi)})"),
                                        width="stretch", config={"displayModeBar": False}, key="p_fpi")

                # ── zone by out count ─────────────────────────────────────────────────
                st.subheader("Zone by Out Count")
                _cols_p = st.columns(3)
                for _i, _oc in enumerate([0, 1, 2]):
                    _dfo = df_p[df_p["outs"] == _oc]
                    _oc_counts = _dfo["pitch_zone"].value_counts().to_dict() if not _dfo.empty else {}
                    with _cols_p[_i]:
                        if _polar_p:
                            st.plotly_chart(utils.zone_polar(_oc_counts, title=f"{_oc} Outs", compact=True),
                                            width="stretch", key=f"p_oc_{_oc}")
                        else:
                            st.plotly_chart(utils.zone_heatmap(_oc_counts, title=f"{_oc} Outs (n={len(_dfo)})"),
                                            width="stretch", key=f"p_oc_{_oc}")

                # ── zone by base state ────────────────────────────────────────────────
                st.subheader("Zone by Base State")
                _col_e_p, _col_r_p = st.columns(2)
                for _col, (_lbl, _obc_vals) in zip([_col_e_p, _col_r_p], [
                    ("Empty", ["000"]),
                    ("Runner(s) On", ["001","010","100","011","101","110","111"]),
                ]):
                    _df_obc = df_p[df_p["obc"].isin(_obc_vals)]
                    _obc_counts = _df_obc["pitch_zone"].value_counts().to_dict() if not _df_obc.empty else {}
                    with _col:
                        if _polar_p:
                            st.plotly_chart(utils.zone_polar(_obc_counts, title=_lbl),
                                            width="stretch", key=f"p_obc_{_lbl}")
                        else:
                            st.plotly_chart(utils.zone_heatmap(_obc_counts, title=f"{_lbl} (n={len(_df_obc)})"),
                                            width="stretch", key=f"p_obc_{_lbl}")

                # ── pitch delta distributions ─────────────────────────────────────────
                st.subheader("Pitch Delta Distributions")
                st.caption("Left: last pitch of one inning → first of next. Middle: last pitch of one game → first of next. Right: consecutive at-bats within the same game.")
                _inn_deltas_p  = utils.between_inning_deltas(df_p, value_col="pitch")
                _game_deltas_p = utils.between_game_deltas(df_p, value_col="pitch")
                _p_delta_signed = st.toggle("Signed", value=True, key="p_delta_signed",
                                            help="Signed shows +/- direction with green/red. Unsigned shows magnitude only.")
                _bd_c1_p, _bd_c2_p = st.columns(2)
                with _bd_c1_p:
                    if not _inn_deltas_p.empty:
                        st.plotly_chart(
                            utils.delta_histogram(_inn_deltas_p, title="Between-Inning", signed=_p_delta_signed),
                            width="stretch", config={"displayModeBar": False}, key="p_inn_delta",
                        )
                    else:
                        st.caption("Not enough between-inning data.")
                with _bd_c2_p:
                    if not _game_deltas_p.empty:
                        st.plotly_chart(
                            utils.delta_histogram(_game_deltas_p, title="Between-Game", signed=_p_delta_signed),
                            width="stretch", config={"displayModeBar": False}, key="p_game_delta",
                        )
                    else:
                        st.caption("Not enough between-game data.")
                if not _deltas_p.empty:
                    st.plotly_chart(
                        utils.delta_histogram(_deltas_p, title="Previous AB", signed=_p_delta_signed),
                        width="stretch", config={"displayModeBar": False}, key="p_delta",
                    )
                else:
                    st.caption("Need at least 2 at-bats from the same pitcher.")
            _zone_delta_section_p(df_p, _deltas_p)

        # ── tendencies ────────────────────────────────────────────────────────
        st.divider()
        with st.expander("Tendencies", expanded=not _simple_mode):
            _tm_p, _tl_p = st.columns(2)
            with _tm_p:
                st.markdown("**Meme Pitches (1, 67, 69, 420, 666, 1000)**")
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
    with st.expander("Data Filters", expanded=False):
        _bf1, _bf2 = st.columns(2)
        with _bf1:
            tab_b_seasons = st.multiselect("Season", _meta_seasons, default=_meta_seasons, key="tab_b_seasons")
        _bf3, _bf4, _bf5 = st.columns(3)
        with _bf3:
            if st.session_state.get("tab_b_team", "All") not in (["All"] + _all_teams):
                st.session_state["tab_b_team"] = "All"
            tab_b_team = st.selectbox("Batter Team", ["All"] + _all_teams,
                                      key="tab_b_team", on_change=_on_tab_b_team)
        with _bf4:
            _tab_b_batters = _players_for_team(tab_b_team)
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

    # Build batter tab df on demand
    if tab_b_batter != "All" and tab_b_scope == "Full Team" and tab_b_team != "All":
        _b_dfs = []
        if _source_key in ("real", "all") and _leagues_tuple:
            _b_dfs.append(_load_team_offense_plays(tab_b_team, _leagues_tuple, st.session_state.get("_data_v", 0)))
        if _source_key in ("scrimmage", "all") and not _scrimmage_df.empty:
            _b_scrim = _scrimmage_df[_scrimmage_df["off_team"] == tab_b_team] \
                if "off_team" in _scrimmage_df.columns else pd.DataFrame()
            if not _b_scrim.empty:
                _b_dfs.append(_b_scrim)
        df_b = pd.concat(_b_dfs, ignore_index=True) if _b_dfs else pd.DataFrame()
    elif tab_b_batter != "All":
        _b_dfs = []
        if _source_key in ("real", "all") and _leagues_tuple:
            _b_dfs.append(_load_batter_plays(tab_b_batter, _leagues_tuple, st.session_state.get("_data_v", 0)))
        if _source_key in ("scrimmage", "all") and not _scrimmage_df.empty:
            _b_scrim = _scrimmage_df[_scrimmage_df["batter_name"] == tab_b_batter] \
                if "batter_name" in _scrimmage_df.columns else pd.DataFrame()
            if not _b_scrim.empty:
                _b_dfs.append(_b_scrim)
        df_b = pd.concat(_b_dfs, ignore_index=True) if _b_dfs else pd.DataFrame()
    else:
        df_b = pd.DataFrame()

    if not df_b.empty and tab_b_seasons:
        _is_scrim_b = (df_b.get("game_type") == "scrimmage") if "game_type" in df_b.columns else pd.Series(False, index=df_b.index)
        df_b = df_b[df_b["season"].isin(tab_b_seasons) | _is_scrim_b]
    if not df_b.empty and tab_b_team != "All":
        df_b = df_b[df_b["off_team"] == tab_b_team]

    df_b_pred = df_b[df_b["id"] < hist_id] if hist_id else df_b

    # ── recent PA window - shared by predictor and Last N chart ───────────────
    if tab_b_batter != "All":
        n_swings = st.slider("Recent PA Window", 5, 100, 20, step=5, key="last_n_swing")
    else:
        n_swings = st.session_state.get("last_n_swing", 20)

    if df_b.empty:
        if tab_b_batter == "All":
            st.info("Select a batter in the filters above to load data.")
        else:
            st.warning("No at-bats found for this batter with the current filters.")
    else:
        # ── pitch suggestions panel ───────────────────────────────────────────
        with st.expander("Pitch Suggestions", expanded=False):
            _hb_outs   = int(st.session_state.get("mgr_sheet_outs") or 0)
            _hb_obc    = st.session_state.get("mgr_sheet_obc") or "000"
            _hb_dd_bkt = st.session_state.get("dd_bucket_b", 100)
            _hb_hz_bkt = st.session_state.get("hz_init_bucket_b", 200)

            _hb_recent = (
                df_b_pred[df_b_pred["swing"].notna()].sort_values("id").tail(n_swings)["swing"]
                .astype(int).tolist()
            ) if not df_b_pred.empty else []
            _hb_delta_hist = (
                df_b[df_b["swing_circ_delta"].notna()].sort_values("id")["swing_circ_delta"]
                .abs().astype(int).tolist()
            ) if "swing_circ_delta" in df_b.columns else []
            _hb_diff_hist = (
                df_b[df_b["diff"].notna()].sort_values("id")["diff"].abs().astype(int).tolist()
            ) if "diff" in df_b.columns else []

            _hb_prior_swing  = _hb_recent[-1]      if len(_hb_recent) >= 1 else None
            _hb_prior_swing2 = _hb_recent[-2]      if len(_hb_recent) >= 2 else None
            _hb_prior_delta  = _hb_delta_hist[-1]  if len(_hb_delta_hist) >= 1 else None
            _hb_prior_delta2 = _hb_delta_hist[-2]  if len(_hb_delta_hist) >= 2 else None
            _hb_prior_diff   = _hb_diff_hist[-1]   if len(_hb_diff_hist)  >= 1 else None

            def _hbstr(prob, n, n_bkts):
                pct = prob * 100
                exp = 100.0 / n_bkts
                return f"{pct:.0f}% ({pct-exp:+.0f}%) n={n}"

            _hint_rows_b = []
            _hint_centered_b = st.session_state.get("hint_centered_b", True)

            if result_ranges and _hb_recent:
                _hb_pa_df = (
                    df_b_pred[df_b_pred["swing"].notna()].sort_values("id").tail(n_swings)
                    if not df_b_pred.empty else pd.DataFrame()
                )
                _hb_rel_kw = dict(
                    recency_slider=st.session_state.get("b_rel_recency", 50),
                    result_slider=st.session_state.get("b_rel_result", 50),
                    state_slider=st.session_state.get("b_rel_state", 0),
                    g1=st.session_state.get("b_rel_g1", 20),
                    g2=st.session_state.get("b_rel_g2", 40),
                    g3=st.session_state.get("b_rel_g3", 40),
                    result_offset=bool(st.session_state.get("b_rel_result_offset", True)),
                )
                _hb_wts   = utils.compute_pa_weights(_hb_pa_df, _hb_obc, _hb_outs, **_hb_rel_kw) if not _hb_pa_df.empty else []
                _hb_dwts  = _hb_wts[1:] if len(_hb_wts) > 1 else None
                _hb_d2wts = [w for w in _hb_wts[2:] for _ in range(2)] if len(_hb_wts) > 2 else None
                _hb_dvals  = utils.project_from_deltas(_hb_recent)
                _hb_d2vals = utils.project_from_delta2s(_hb_recent)
                _hb_obp_p  = utils.optimal_swing_range(_hb_recent,  result_ranges, "obp", False, _hb_wts or None)
                _hb_obp_d  = utils.optimal_swing_range(_hb_dvals,   result_ranges, "obp", False, _hb_dwts)
                _hb_obp_d2 = utils.optimal_swing_range(_hb_d2vals,  result_ranges, "obp", False, _hb_d2wts)
                _hb_ss_p   = utils.swing_signal_strength(_hb_recent,  result_ranges, "obp", False, _hb_wts or None)
                _hb_ss_d   = utils.swing_signal_strength(_hb_dvals,   result_ranges, "obp", False, _hb_dwts)  if _hb_dvals  else 0.0
                _hb_ss_d2  = utils.swing_signal_strength(_hb_d2vals,  result_ranges, "obp", False, _hb_d2wts) if _hb_d2vals else 0.0
                _hint_rows_b.append({"Signal": "OBP recent swing",
                                     "lo": _hb_obp_p[0] if _hb_obp_p else None,
                                     "hi": _hb_obp_p[1] if _hb_obp_p else None,
                                     "Strength": f"{_hb_ss_p:.0f}% signal",
                                     "_best_zone_only": True})
                _hint_rows_b.append({"Signal": "OBP recent Δ",
                                     "lo": _hb_obp_d[0] if _hb_obp_d else None,
                                     "hi": _hb_obp_d[1] if _hb_obp_d else None,
                                     "Strength": f"{_hb_ss_d:.0f}% signal",
                                     "_best_zone_only": True})
                _hint_rows_b.append({"Signal": "OBP recent Δ²",
                                     "lo": _hb_obp_d2[0] if _hb_obp_d2 else None,
                                     "hi": _hb_obp_d2[1] if _hb_obp_d2 else None,
                                     "Strength": f"{_hb_ss_d2:.0f}% signal",
                                     "_best_zone_only": True})

            _hb_dd_n = 500 // _hb_dd_bkt
            _hb_hz_n = 1000 // _hb_hz_bkt

            def _delta_row_b(signal, h_dict, n_bkts):
                _zs = utils.hint_zscore(h_dict["prob"], h_dict["n"], n_bkts)
                _dc = h_dict.get("all_counts")
                _dd_bkt_z = h_dict.get("delta_bucket_size", _hb_dd_bkt)
                extra = []
                if _hb_prior_swing is not None:
                    _all_bkts = [(h_dict["delta_lo"], h_dict["delta_hi"])] + h_dict.get("tied_buckets", [])
                    _merged = utils.merge_delta_ranges(_all_bkts)
                    _arms = []
                    for _mlo, _mhi in _merged:
                        _arms.extend(utils.delta_to_pitch_ranges(_hb_prior_swing, _mlo, _mhi))
                    r1 = _arms[0] if len(_arms) > 0 else (None, None)
                    r2 = _arms[1] if len(_arms) > 1 else (None, None)
                    extra = [a for a in _arms[2:] if a[0] is not None]
                    return {"Signal": signal,
                            "lo": r1[0], "hi": r1[1],
                            "lo2": r2[0], "hi2": r2[1],
                            "extra_ranges": extra,
                            "Strength": _hbstr(h_dict["prob"], h_dict["n"], n_bkts),
                            "_zscore": _zs,
                            "_delta_counts": _dc,
                            "_prior_pitch_for_zone": _hb_prior_swing,
                            "_dd_bkt_for_zone": _dd_bkt_z}
                return {"Signal": signal, "lo": None, "hi": None,
                        "extra_ranges": extra,
                        "Strength": _hbstr(h_dict["prob"], h_dict["n"], n_bkts),
                        "_zscore": _zs,
                        "_delta_counts": _dc,
                        "_prior_pitch_for_zone": _hb_prior_swing,
                        "_dd_bkt_for_zone": _dd_bkt_z}

            if _hb_prior_delta is not None:
                _h = utils.seq2_delta_hint(df_b, "swing", _hb_dd_bkt, _hb_prior_delta, centered=_hint_centered_b)
                if _h:
                    _hint_rows_b.append(_delta_row_b("2-Δ seq", _h, _hb_dd_n))

            if _hb_prior_delta is not None and _hb_prior_delta2 is not None:
                _h = utils.seq3_delta_hint(df_b, "swing", _hb_dd_bkt, _hb_prior_delta2, _hb_prior_delta, centered=_hint_centered_b)
                if _h:
                    _hint_rows_b.append(_delta_row_b("3-Δ seq", _h, _hb_dd_n))

            if _hb_prior_diff is not None:
                _h = utils.diff_to_delta_hint(df_b, "swing", _hb_prior_diff, centered=_hint_centered_b)
                if _h:
                    _hint_rows_b.append(_delta_row_b("Prior diff → Δ", _h, 5))

            if _hb_prior_swing is not None:
                _h = utils.seq2_hint(df_b, "swing", _hb_hz_bkt, _hb_prior_swing, centered=_hint_centered_b)
                if _h:
                    _hint_rows_b.append({"Signal": "2-pitch seq",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "Strength": _hbstr(_h["prob"], _h["n"], _hb_hz_n),
                                         "_zone_dist": utils.seq2_zone_dist(df_b, "swing", _hb_hz_bkt, _hb_prior_swing, centered=_hint_centered_b),
                                         "_zone_bucket_size": _hb_hz_bkt,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], _hb_hz_n)})

            if _hb_prior_swing is not None and _hb_prior_swing2 is not None:
                _h = utils.seq3_hint(df_b, "swing", _hb_hz_bkt, _hb_prior_swing2, _hb_prior_swing, centered=_hint_centered_b)
                if _h:
                    _hint_rows_b.append({"Signal": "3-pitch seq",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "Strength": _hbstr(_h["prob"], _h["n"], _hb_hz_n),
                                         "_zone_dist": utils.seq3_zone_dist(df_b, "swing", _hb_hz_bkt, _hb_prior_swing2, _hb_prior_swing, centered=_hint_centered_b),
                                         "_zone_bucket_size": _hb_hz_bkt,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], _hb_hz_n)})

            if "outs" in df_b.columns:
                _h = utils.best_zone_hint(df_b[df_b["outs"] == _hb_outs], "swing")
                if _h:
                    _hint_rows_b.append({"Signal": f"Outs({_hb_outs})",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hbstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            if "obc" in df_b.columns:
                _obc_is_zero_b = (_hb_obc == "000")
                _obc_mask_b    = df_b["obc"] == "000" if _obc_is_zero_b else df_b["obc"] != "000"
                _obc_lbl_b     = "Empty" if _obc_is_zero_b else "Runner(s) on"
                _h = utils.best_zone_hint(df_b[_obc_mask_b], "swing")
                if _h:
                    _hint_rows_b.append({"Signal": f"Base state ({_obc_lbl_b})",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hbstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            _cur_game_id_b = st.session_state.get("game_tab_sel")
            _cur_inn_b     = int(st.session_state.get("mgr_inning", 1))
            _b_last_sw     = df_b[df_b["swing"].notna()].sort_values("id")
            _b_last_row    = _b_last_sw.iloc[-1] if not _b_last_sw.empty else None
            _b_last_game   = (int(_b_last_row["game_id"]) if _b_last_row is not None
                              and pd.notna(_b_last_row.get("game_id")) else None)
            _b_last_inn    = (int(_b_last_row["inning"]) if _b_last_row is not None
                              and pd.notna(_b_last_row.get("inning")) else None)
            _show_fp_app_b = (_cur_game_id_b is not None
                              and (_b_last_game is None or _b_last_game != _cur_game_id_b))
            _show_fp_inn_b = (_cur_game_id_b is not None
                              and (_b_last_game is None or _b_last_game != _cur_game_id_b
                                   or _b_last_inn != _cur_inn_b))

            if "is_fp_inn" in df_b.columns and _show_fp_inn_b:
                _h = utils.best_zone_hint(df_b[df_b["is_fp_inn"] == True], "swing")
                if _h:
                    _hint_rows_b.append({"Signal": "1st pitch inning",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hbstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            if "is_fp_app" in df_b.columns and _show_fp_app_b:
                _h = utils.best_zone_hint(df_b[df_b["is_fp_app"] == True], "swing")
                if _h:
                    _hint_rows_b.append({"Signal": "1st pitch appearance",
                                         "lo": _h["lo"], "hi": _h["hi"],
                                         "lo2": _h.get("lo2"), "hi2": _h.get("hi2"),
                                         "Strength": _hbstr(_h["prob"], _h["n"], 9),
                                         "_zone_dist": _h.get("_zone_dist"),
                                         "_zone_bucket_size": 111,
                                         "_zscore": utils.hint_zscore(_h["prob"], _h["n"], 9)})

            # Sort non-OBP rows by z-score descending; OBP rows stay pinned at top
            _obp_rows_b = [r for r in _hint_rows_b if r["Signal"].startswith("OBP")]
            _seq_rows_b = [r for r in _hint_rows_b if not r["Signal"].startswith("OBP")]
            _seq_rows_b.sort(key=lambda r: r.get("_zscore", 0.0), reverse=True)
            _hint_rows_b = _obp_rows_b + _seq_rows_b

            if _hint_rows_b:
                _tb1, _tb2 = st.columns(2)
                with _tb1:
                    _hint_all_b = st.toggle("All zones", value=False, key="hint_all_b")
                with _tb2:
                    _hint_centered_b = st.toggle("Centered", value=True, key="hint_centered_b")
                _hb_swing_val = int(st.session_state["pred_pitch"]) if "pred_pitch" in st.session_state and result_ranges else None
                _hb_obr_lo = _hb_obr_hi = None
                if _hb_swing_val is not None:
                    _hb_obr_max = max((hi for _, _lo, hi in result_ranges if _ in utils._OBR), default=0)
                    if _hb_obr_max:
                        _hb_obr_lo = ((_hb_swing_val - _hb_obr_max - 1) % 1000) + 1
                        _hb_obr_hi = ((_hb_swing_val + _hb_obr_max - 1) % 1000) + 1
                st.plotly_chart(
                    utils.hint_bars_figure(
                        _hint_rows_b,
                        mode="all" if _hint_all_b else "best",
                        mobile=_is_mobile,
                        prior_val=_hb_prior_swing,
                        prior_val2=_hb_prior_swing2,
                        swing_val=_hb_swing_val,
                        obr_lo=_hb_obr_lo,
                        obr_hi=_hb_obr_hi,
                    ),
                    use_container_width=True,
                    config={"displayModeBar": False},
                )
            elif not result_ranges or not _hb_recent:
                st.caption("Fetch a matchup and select a batter to see suggestions.")
            else:
                st.caption("Not enough history to generate sequence suggestions.")

        # ── pitch predictor ───────────────────────────────────────────────────
        st.subheader("Pitch Analyzer")
        st.caption("Enter a proposed pitch to see what each of this batter's recent swings would give.")
        _lnc_weights_b: list[float] = []

        if result_ranges:
            if "_pend_pitch" in st.session_state:
                st.session_state["pred_pitch"] = st.session_state.pop("_pend_pitch")
            for _pk in st.session_state.pop("_pills_rst_b", []):
                st.session_state[_pk] = None

            with st.expander("Relevance Weighting", expanded=not _simple_mode):
                st.caption("Weight how recent swings influence the Optimal Pitch. At 50 on all behavior sliders, weighting is uniform.")
                _brw1, _brw2, _brw3 = st.columns(3)
                with _brw1:
                    st.markdown("**1 - Recency**")
                    st.slider("Older vs Newer", 0, 100, value=50, key="b_rel_recency",
                              help="50=equal. 0=weight older swings more. 100=weight recent swings more.")
                    st.number_input("Weight", 0, 100, value=20, step=1, key="b_rel_g1")
                with _brw2:
                    st.markdown("**2 - Result**")
                    st.slider("Pitcher vs Batter", 0, 100, value=50, key="b_rel_result",
                              help="50=equal. 0=upweight good pitching results (K, DP). 100=upweight good batting results (HR, XBH).")
                    st.number_input("Weight ", 0, 100, value=40, step=1, key="b_rel_g2")
                    st.toggle("Previous result", key="b_rel_result_offset", value=True,
                              help="Weight each swing by the result of the previous swing instead of its own result.")
                with _brw3:
                    st.markdown("**3 - State**")
                    st.slider("Any vs Similar", 0, 100, value=50, key="b_rel_state",
                              help="50=equal. 100=upweight swings from similar OBC + outs situations.")
                    st.number_input("Weight  ", 0, 100, value=40, step=1, key="b_rel_g3")
                _brg1 = st.session_state.get("b_rel_g1", 20)
                _brg2 = st.session_state.get("b_rel_g2", 40)
                _brg3 = st.session_state.get("b_rel_g3", 40)
                _brg_tot = max(_brg1 + _brg2 + _brg3, 1)
                st.caption(
                    f"Normalized: Recency {_brg1/_brg_tot*100:.0f}% | "
                    f"Result {_brg2/_brg_tot*100:.0f}% | "
                    f"State {_brg3/_brg_tot*100:.0f}%"
                )

            if "pred_pitch" not in st.session_state:
                st.session_state["pred_pitch"] = 500
            proposed_pitch = st.number_input("Proposed Pitch", min_value=1, max_value=1000,
                                             step=1, key="pred_pitch")

            # PA weights for relevance-weighted optimal pitch and swing predictor coloring
            _pa_df_b = (df_b_pred[df_b_pred["swing"].notna()].sort_values("id").tail(n_swings)
                        if not df_b_pred.empty else pd.DataFrame())
            _b_cur_obc  = (st.session_state.get("mgr_sheet_obc") or "000")
            _b_cur_outs = int(st.session_state.get("mgr_sheet_outs") or 0)
            _b_rel_kwargs = dict(
                recency_slider=st.session_state.get("b_rel_recency", 50),
                result_slider=st.session_state.get("b_rel_result",  50),
                state_slider=st.session_state.get("b_rel_state",   50),
                g1=st.session_state.get("b_rel_g1", 20),
                g2=st.session_state.get("b_rel_g2", 40),
                g3=st.session_state.get("b_rel_g3", 40),
                result_offset=bool(st.session_state.get("b_rel_result_offset", True)),
            )
            _pa_weights_b = utils.compute_pa_weights(
                _pa_df_b, _b_cur_obc, _b_cur_outs, **_b_rel_kwargs,
            ) if not _pa_df_b.empty else []
            _lnc_df_b = (df_b_pred[df_b_pred["pitch"].notna() & df_b_pred["swing"].notna()]
                         .sort_values("id").tail(n_swings)
                         if not df_b_pred.empty else pd.DataFrame())
            _lnc_weights_b = utils.compute_pa_weights(
                _lnc_df_b, _b_cur_obc, _b_cur_outs, **_b_rel_kwargs,
            ) if not _lnc_df_b.empty else []

            _df_tick_b = _pa_df_b if not _pa_df_b.empty else pd.DataFrame(columns=["id","pitch","swing"])
            _tick_lbl_b = f"Last {n_swings} swings (pre-AB)" if hist_id and not df_b_pred.empty \
                          else f"Last {n_swings} swings"
            st.plotly_chart(
                utils.swing_predictor_chart(_df_tick_b, swing=int(proposed_pitch), n=n_swings,
                                            result_ranges=result_ranges, tick_label=_tick_lbl_b,
                                            value_col="swing", x_label="Swing Values", ref_label="Pitch",
                                            tick_weights=_pa_weights_b),
                width="stretch", key="b_swing_pred",
            )

            st.markdown("**Optimal Pitch**")
            _recent_b = _pa_df_b["swing"].astype(int).tolist() if not _pa_df_b.empty else []
            _delta_b  = utils.project_from_deltas(_recent_b)
            _delta2_b = utils.project_from_delta2s(_recent_b)
            _delta_weights_b  = (_pa_weights_b[1:] if len(_pa_weights_b) > 1 else None)
            _delta2_weights_b = (
                [w for w in _pa_weights_b[2:] for _ in range(2)]
                if len(_pa_weights_b) > 2 else None
            )
            _opt_rows_b = [
                ("Based on Recent Swing Values", _recent_b,  _pa_weights_b or None),
                ("Based on Recent Swing Δ",      _delta_b,   _delta_weights_b),
                ("Based on Recent Swing Δ²",     _delta2_b,  _delta2_weights_b),
            ]
            st.markdown("""<style>
button[data-testid="stBaseButton-pills"] {
    border: 1.5px solid #2ca02c !important;
}
button[data-testid="stBaseButton-pills"] + button[data-testid="stBaseButton-pills"] {
    border: 1.5px solid #d62728 !important;
}
</style>""", unsafe_allow_html=True)
            if _simple_mode:
                col_obp_b = st.columns(1)[0]
            else:
                col_obp_b, col_slg_b = st.columns(2)
            with col_obp_b:
                st.markdown("**OBP Optimal Pitch**" if _simple_mode else "**OBP**")
                for _i, (_lbl, _vals, _wts) in enumerate(_opt_rows_b):
                    st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-1.3rem'>{_lbl}</div>",
                                unsafe_allow_html=True)
                    if _vals:
                        _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "obp", False, weights=_wts)
                        _sig_b_obp_tgt = utils.swing_signal_strength(_vals, result_ranges, "obp", False, weights=_wts, zone="best")
                        _sig_b_obp_avd = utils.swing_signal_strength(_vals, result_ranges, "obp", False, weights=_wts, zone="worst")
                        _pk = f"pill_obp_{_i}_b"
                        _opts = {
                            f"↑ {_bv} ({_bs:.3f}) · {_sig_b_obp_tgt:.0f}%": _bv,
                            f"↓ {_cv} ({_cs:.3f}) · {_sig_b_obp_avd:.0f}%": _cv,
                        }
                        _sel = st.pills("", list(_opts.keys()), key=_pk)
                        if _sel:
                            st.session_state["_pend_pitch"] = _opts[_sel]
                            st.session_state.setdefault("_pills_rst_b", []).append(_pk)
                            st.rerun()
                        st.plotly_chart(
                            utils.optimal_swing_chart(_vals, result_ranges, "obp", False,
                                                      compact=True, weights=_wts),
                            use_container_width=True, key=f"b_opt_obp_{_i}")
            if not _simple_mode:
                with col_slg_b:
                    st.markdown("**SLG**")
                    for _i, (_lbl, _vals, _wts) in enumerate(_opt_rows_b):
                        st.markdown(f"<div style='font-size:0.8rem;opacity:0.6;margin-bottom:-1.3rem'>{_lbl}</div>",
                                    unsafe_allow_html=True)
                        if _vals:
                            _bv, _bs, _cv, _cs = utils.suggest_swing(_vals, result_ranges, "slg", False, weights=_wts)
                            _sig_b_slg_tgt = utils.swing_signal_strength(_vals, result_ranges, "slg", False, weights=_wts, zone="best")
                            _sig_b_slg_avd = utils.swing_signal_strength(_vals, result_ranges, "slg", False, weights=_wts, zone="worst")
                            _pk = f"pill_slg_{_i}_b"
                            _opts = {
                                f"↑ {_bv} ({_bs:.3f}) · {_sig_b_slg_tgt:.0f}%": _bv,
                                f"↓ {_cv} ({_cs:.3f}) · {_sig_b_slg_avd:.0f}%": _cv,
                            }
                            _sel = st.pills("", list(_opts.keys()), key=_pk)
                            if _sel:
                                st.session_state["_pend_pitch"] = _opts[_sel]
                                st.session_state.setdefault("_pills_rst_b", []).append(_pk)
                                st.rerun()
                            st.plotly_chart(
                                utils.optimal_swing_chart(_vals, result_ranges, "slg", False,
                                                          compact=True, weights=_wts),
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
                                        highlight_name=_hl_name,
                                        tick_weights=_lnc_weights_b or None,
                                        pannable=True),
            width="stretch", key="b_last_n",
        )

        # rebind so all sections below are ITD
        df_b = df_b_pred
        _b_total = len(df_b)

        # Calculate swing deltas for downstream sections (game-scoped)
        _deltas_b = df_b["swing_circ_delta"].dropna() if "swing_circ_delta" in df_b.columns else pd.Series(dtype=float)

        st.divider()
        with st.expander("Swing Analysis", expanded=not _simple_mode):
            st.subheader("Next Swing Delta vs Prior Swing Delta")
            st.caption("How does a batter adjust their next swing based on their previous swing movement?")
            dd_bucket_b = st.select_slider("Bucket size", options=[25, 50, 100, 125, 250, 500], value=100, key="dd_bucket_b")
            st.plotly_chart(
                utils.next_delta_vs_prior_delta_heatmap(df_b, title="Next Swing Δ vs Prior Swing Δ", value_col="swing", bucket_size=dd_bucket_b),
                width="stretch", config={"displayModeBar": False}, key="b_delta_delta_hm",
            )

            _dd_b_abs_hist = (
                df_b[df_b["swing_circ_delta"].notna()]
                .sort_values("id")["swing_circ_delta"].abs().astype(int).tolist()
            ) if "swing_circ_delta" in df_b.columns else []
            _dd_b_def_init   = _dd_b_abs_hist[-2] if len(_dd_b_abs_hist) >= 2 else 250
            _dd_b_def_follow = _dd_b_abs_hist[-1] if _dd_b_abs_hist else 250
            _ddc1_b, _ddc2_b = st.columns(2)
            with _ddc1_b:
                _dd_b_init_val = st.number_input(
                    "Initial |Δ|", min_value=0, max_value=500,
                    value=_dd_b_def_init, step=1, key=f"dd_b_init_{tab_b_batter}",
                )
            with _ddc2_b:
                _dd_b_follow_val = st.number_input(
                    "Following |Δ|", min_value=0, max_value=500,
                    value=_dd_b_def_follow, step=1, key=f"dd_b_follow_{tab_b_batter}",
                )
            _n_bkts_b = 500 // dd_bucket_b
            _dd_b_ii = min(max(0, (_dd_b_init_val - 1) // dd_bucket_b if _dd_b_init_val > 0 else 0), _n_bkts_b - 1)
            _dd_b_fi = min(max(0, (_dd_b_follow_val - 1) // dd_bucket_b if _dd_b_follow_val > 0 else 0), _n_bkts_b - 1)
            _dd_b_init_lbl   = f"{_dd_b_ii * dd_bucket_b}-{(_dd_b_ii + 1) * dd_bucket_b}"
            _dd_b_follow_lbl = f"{_dd_b_fi * dd_bucket_b}-{(_dd_b_fi + 1) * dd_bucket_b}"
            _third_delta_fig_b = utils.delta_third_dist(
                df_b, value_col="swing", bucket_size=dd_bucket_b,
                init_label=_dd_b_init_lbl, follow_label=_dd_b_follow_lbl,
            )
            if _third_delta_fig_b:
                st.plotly_chart(_third_delta_fig_b, width="stretch",
                                config={"displayModeBar": False}, key="b_delta_third")
            else:
                st.caption("Not enough data for this delta sequence.")

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
            st.caption("How often each swing range is followed by each other swing range. Click a cell to see the 3rd swing distribution.")
            _hzb_sl1, _hzb_sl2 = st.columns(2)
            with _hzb_sl1:
                init_bucket_b   = st.select_slider("Initial bucket size",   options=[50,100,125,200,250,500], value=200, key="hz_init_bucket_b")
            with _hzb_sl2:
                follow_bucket_b = st.select_slider("Following bucket size", options=[50,100,125,200,250,500], value=200, key="hz_follow_bucket_b")
            group_cols_b = ["game_id","batter_name"] if tab_b_batter != "All" else ["batter_name"]

            st.plotly_chart(
                utils.hot_zone_matrix(df_b, value_col="swing", group_cols=group_cols_b,
                                      init_bucket_size=init_bucket_b, follow_bucket_size=follow_bucket_b),
                width="stretch", key="b_hot_zone",
            )

            _hz_b_recent     = df_b[df_b["swing"].notna()].sort_values("id")["swing"].astype(int).tolist()
            _hz_b_def_init   = max(1, _hz_b_recent[-2]) if len(_hz_b_recent) >= 2 else (max(1, _hz_b_recent[-1]) if _hz_b_recent else None)
            _hz_b_def_follow = max(1, _hz_b_recent[-1]) if _hz_b_recent else None
            _hzb_c1, _hzb_c2 = st.columns(2)
            with _hzb_c1:
                _hz_b_init_val   = st.number_input("Initial swing",   min_value=1, max_value=1000, value=_hz_b_def_init,   step=1, key=f"hz_b_init_{tab_b_batter}",   placeholder="1-1000")
            with _hzb_c2:
                _hz_b_follow_val = st.number_input("Following swing", min_value=1, max_value=1000, value=_hz_b_def_follow, step=1, key=f"hz_b_follow_{tab_b_batter}", placeholder="1-1000")

            if _hz_b_init_val is not None and _hz_b_follow_val is not None:
                _b_ii = (int(_hz_b_init_val)   - 1) // init_bucket_b
                _b_fi = (int(_hz_b_follow_val) - 1) // follow_bucket_b
                _hz_b_init_label   = f"{_b_ii * init_bucket_b + 1}-{min((_b_ii + 1) * init_bucket_b, 1000)}"
                _hz_b_follow_label = f"{_b_fi * follow_bucket_b + 1}-{min((_b_fi + 1) * follow_bucket_b, 1000)}"
                _third_fig_b = utils.hot_zone_third_dist(
                    df_b, value_col="swing", group_cols=group_cols_b,
                    init_bucket_size=init_bucket_b, follow_bucket_size=follow_bucket_b,
                    init_label=_hz_b_init_label, follow_label=_hz_b_follow_label,
                )
                if _third_fig_b:
                    st.plotly_chart(_third_fig_b, width="stretch",
                                    config={"displayModeBar": False}, key="b_third_dist")
                else:
                    st.caption("Not enough data for this sequence.")

            # ── zone distribution ─────────────────────────────────────────────────
            st.divider()
            @st.fragment
            def _zone_delta_section_b(df_b, _deltas_b):
                st.subheader("Swing Zone Distribution (All)")
                _zone_polar_b = st.toggle("Polar view", value=True, key="zone_polar_b")
                _zone_counts_b = df_b["swing_zone"].value_counts().to_dict()
                if _zone_polar_b:
                    st.plotly_chart(utils.zone_polar(_zone_counts_b, title="Swing Zone Frequency"),
                                    width="stretch", key="b_zone_all")
                else:
                    st.plotly_chart(utils.zone_heatmap(_zone_counts_b, title="Swing Zone Frequency"),
                                    width="stretch", key="b_zone_all")

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

                # ── swing delta distributions ─────────────────────────────────────────
                st.subheader("Swing Delta Distributions")
                st.caption("Left: last swing of one inning → first of next. Middle: last swing of one game → first of next. Right: consecutive at-bats within the same game.")
                _inn_deltas_b  = utils.between_inning_deltas(df_b, value_col="swing")
                _game_deltas_b = utils.between_game_deltas(df_b, value_col="swing")
                _b_delta_signed = st.toggle("Signed", value=True, key="b_delta_signed",
                                            help="Signed shows +/- direction with green/red. Unsigned shows magnitude only.")
                _bd_c1_b, _bd_c2_b = st.columns(2)
                with _bd_c1_b:
                    if not _inn_deltas_b.empty:
                        st.plotly_chart(
                            utils.delta_histogram(_inn_deltas_b, title="Between-Inning", signed=_b_delta_signed),
                            width="stretch", config={"displayModeBar": False}, key="b_inn_delta",
                        )
                    else:
                        st.caption("Not enough between-inning data.")
                with _bd_c2_b:
                    if not _game_deltas_b.empty:
                        st.plotly_chart(
                            utils.delta_histogram(_game_deltas_b, title="Between-Game", signed=_b_delta_signed),
                            width="stretch", config={"displayModeBar": False}, key="b_game_delta",
                        )
                    else:
                        st.caption("Not enough between-game data.")
                if not _deltas_b.empty:
                    st.plotly_chart(
                        utils.delta_histogram(_deltas_b, title="Previous AB", signed=_b_delta_signed),
                        width="stretch", config={"displayModeBar": False}, key="b_delta",
                    )
                    _dc1b, _dc2b, _dc3b = st.columns(3)
                    _dc1b.metric("Avg Δ", f"{_deltas_b.mean():+.1f}")
                    _dc2b.metric("Avg |Δ|", f"{_deltas_b.abs().mean():.1f}")
                    _dc3b.metric("n", len(_deltas_b))
                else:
                    st.caption("Need at least 2 at-bats from the same batter.")
            _zone_delta_section_b(df_b, _deltas_b)

        # ── tendencies ────────────────────────────────────────────────────────
        st.divider()
        with st.expander("Tendencies", expanded=not _simple_mode):
            _tm_b, _tl_b = st.columns(2)
            with _tm_b:
                st.markdown("**Meme Swings (1, 67, 69, 420, 666, 1000)**")
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
# GAME TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_g:
    # Build game options list sorted newest first
    _g_opts = [
        g for g in all_games_meta
        if g.get("away_team") and g.get("home_team")
    ]
    _g_opts.sort(key=lambda g: (g.get("season") or 0, g.get("session_number") or 0), reverse=True)
    _g_ids = [g["id"] for g in _g_opts]

    def _game_label(g: dict) -> str:
        gc = g.get("game_code") or f"S{g.get('season','?')} G{g.get('session_number','?')}"
        lbl = f"{gc} - {g.get('away_team','?')} @ {g.get('home_team','?')}"
        if g.get("away_score") is not None and g.get("home_score") is not None:
            lbl += f"  ({g['away_score']}-{g['home_score']})"
        return lbl

    if not _g_ids:
        st.info("No games found in the database.")
    else:
        # On first render, default to the most recent game (index 0).
        # When Fetch Matchup runs, it sets game_tab_sel directly so the widget syncs.
        if "game_tab_sel" not in st.session_state:
            st.session_state["game_tab_sel"] = _g_ids[0]

        _sel_gid = st.selectbox(
            "Game",
            _g_ids,
            format_func=lambda gid: _game_label(next(g for g in _g_opts if g["id"] == gid)),
            key="game_tab_sel",
        )

        _game_meta = next(g for g in _g_opts if g["id"] == _sel_gid)
        _away = _game_meta.get("away_team", "Away")
        _home = _game_meta.get("home_team", "Home")
        _away_s = _game_meta.get("away_score")
        _home_s = _game_meta.get("home_score")

        # Game header
        _score_md = (
            f"**{_away}** {_away_s} - {_home_s} **{_home}**"
            if _away_s is not None else f"**{_away}** @ **{_home}**"
        )
        st.subheader(_score_md)

        _game_plays = _load_game_plays(_sel_gid, st.session_state.get("_data_v", 0))

        if not _game_plays:
            st.info("No plays found for this game - sync plays first.")
        else:
            st.caption(f"{len(_game_plays)} plays")

            # Win probability chart
            _wp_ready = utils.get_win_probability(12, 0, "000", 0) is not None
            if not _wp_ready:
                st.warning("Win probability table not loaded. Run the WP simulation to generate win_probability_table.csv first.")
            else:
                _game_league = _game_meta.get("league", "MLN")
                _wp_df = utils.compute_game_wp_series(_game_plays, _game_meta, innings=utils.game_innings(_game_league))
                _wp_fig = utils.win_probability_chart(
                    _wp_df,
                    home_team=_home,
                    away_team=_away,
                    title="Win Probability",
                    home_hex=_team_hex_map.get(_home, "#d6604d"),
                    away_hex=_team_hex_map.get(_away, "#2166ac"),
                )
                st.plotly_chart(_wp_fig, use_container_width=True,
                                config={"displayModeBar": False}, key="game_wp_chart")

                # Play-by-play table with WP column
                with st.expander("Play-by-Play with Win Probability"):
                    _ppwp = _wp_df[~_wp_df["inn_label"].isin(["Start", "Final"])].copy()
                    _ppwp["WP"] = (_ppwp["home_wp"] * 100).round(1).astype(str) + "%"
                    _ppwp["obc_disp"] = _ppwp["obc"].map(utils.obc_display)
                    _disp_cols = ["play_idx", "inn_label", "outs", "obc_disp",
                                  "batter", "pitcher", "result",
                                  "away_score", "home_score", "WP"]
                    _col_names = ["#", "Inn", "Outs", "Runners",
                                  "Batter", "Pitcher", "Result",
                                  _away, _home, f"{_home} WP"]
                    st.dataframe(
                        _ppwp[_disp_cols].rename(columns=dict(zip(_disp_cols, _col_names))),
                        use_container_width=True, hide_index=True,
                    )

# ══════════════════════════════════════════════════════════════════════════════
# MANAGER TAB
# ══════════════════════════════════════════════════════════════════════════════

with tab_m:
    # Game state: Historical/Manual overrides sheet fetch, which overrides default
    _sheet_outs = st.session_state.get("mgr_sheet_outs")
    _sheet_obc  = st.session_state.get("mgr_sheet_obc")
    if pred_mode == "Historical / Manual":
        _man_outs = st.session_state.get("pred_calc_outs", 0)
        _mr1 = st.session_state.get("pred_calc_1b", "Empty")
        _mr2 = st.session_state.get("pred_calc_2b", "Empty")
        _mr3 = st.session_state.get("pred_calc_3b", "Empty")
        _current_outs = int(_man_outs) if _man_outs is not None else 0
        if any(r != "Empty" for r in [_mr1, _mr2, _mr3]):
            _current_obc = (
                f"{'1' if _mr3 != 'Empty' else '0'}"
                f"{'1' if _mr2 != 'Empty' else '0'}"
                f"{'1' if _mr1 != 'Empty' else '0'}"
            )
        else:
            # Runner names couldn't be resolved - use obc stored from imported play
            _current_obc = st.session_state.get("pred_calc_hist_obc", "000") or "000"
    elif _sheet_outs is not None and _sheet_obc is not None:
        _current_outs = int(_sheet_outs)
        _current_obc  = _sheet_obc
    else:
        _current_outs = 0
        _current_obc  = "000"
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

    def _calc_ev_and_probs(ranges):
        ev = 0.0
        _tprobs: dict[int, float] = {}
        for entry in (ranges or []):
            _r, _dl, _dh = _norm(entry)
            _prob  = min((_dh - _dl + 1) * 2 / 1000, 1.0)
            _runs, _nobc, _nout = _lookup(_r, _current_obc)
            _nout  = min(_nout, 3)
            _ner   = utils.get_expected_runs(_nout, _nobc) or 0 if _nout < 3 else 0
            ev    += _prob * (_runs + _ner)
            _imm   = int(_runs)
            _adist = utils._re_dist.get((_nout, _nobc), {0: 1.0}) if _nout < 3 else {0: 1.0}
            for _add, _p2 in _adist.items():
                _n = _imm + _add
                _tprobs[_n] = _tprobs.get(_n, 0.0) + _prob * _p2
        p1r  = _tprobs.get(1, 0.0)
        p2r  = _tprobs.get(2, 0.0)
        p3pr = sum(p for r, p in _tprobs.items() if r >= 3)
        return ev, p1r, p2r, p3pr

    def _outcome_grid(ranges, obc, outs, remaining=None, batting_lead=0):
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
            row = {
                "_sort":      total,
                "Result":     _display_r,
                "Range":      f"{lo}-{hi}",
                "Prob":       f"{prob * 100:.1f}%",
                "3-2-1":      utils.obc_circles(new_obc_code),
                "Outs After": "End" if nout >= 3 else nout,
                "Runs":       int(runs),
                "ER After":   er_after,
            }
            if remaining is not None and _wp_table_ready:
                new_bl   = batting_lead + int(round(runs))
                wp_after = _wp_for_state(remaining, nout, new_obc_code, new_bl)
                row["Exp WP"] = f"{wp_after * 100:.1f}%"
            rows.append(row)

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

    def _show_outcome_grid(ranges, obc, outs, key, remaining=None, batting_lead=0):
        grid = _outcome_grid(ranges, obc, outs, remaining, batting_lead)
        if grid.empty:
            return
        st.plotly_chart(_outcome_scatter(grid), use_container_width=True, key=f"{key}_scatter")
        with st.expander("Outcome Breakdown", expanded=False):
            st.dataframe(grid, use_container_width=True, hide_index=True, key=key)

    def _show_debug_panel(dbg: dict, label: str):
        if not dbg:
            return
        with st.container(border=True):
            st.caption(f"Debug: {label}")
            _dc1, _dc2 = st.columns(2)
            with _dc1:
                st.dataframe(pd.DataFrame({
                    "Stat": ["d_hit", "d_pow", "d_spd", "d_eye", "hnd"],
                    "Value": [dbg.get("d_hit"), dbg.get("d_pow"), dbg.get("d_spd"),
                              dbg.get("d_eye"), dbg.get("hnd")],
                }), hide_index=True, use_container_width=True)
            with _dc2:
                st.dataframe(pd.DataFrame({
                    "OBR": ["HR", "3B", "2B", "1B", "IF1B", "BB", "K"],
                    "Width": [dbg.get("w_hr"), dbg.get("w_3b"), dbg.get("w_2b"),
                              dbg.get("w_1b"), dbg.get("w_if1b"), dbg.get("w_bb"),
                              dbg.get("w_k")],
                }), hide_index=True, use_container_width=True)
            if dbg.get("mode") == "bunt":
                _bunt_items = [
                    ("total_hit",   dbg.get("total_hit")),
                    ("base_hit",    dbg.get("base_hit")),
                    ("B1BWH",       dbg.get("b1bwh")),
                    ("B1B",         dbg.get("b1b")),
                    ("TP base",     dbg.get("b_tp_base")),
                    ("LOTP base",   dbg.get("b_lotp_base")),
                    ("pool",        dbg.get("b_go_pool")),
                    ("SacB rate",   f"{dbg.get('sacb_rate', 0):.4f}"),
                    ("BDP rate",    f"{dbg.get('bdp_rate', 0):.4f}"),
                    ("SacB",        dbg.get("b_sacb")),
                    ("BDP",         dbg.get("b_bdp")),
                    ("TP final",    dbg.get("b_tp_final")),
                    ("LOTP final",  dbg.get("b_lotp_final")),
                    ("GO/BFC",      dbg.get("b_go")),
                ]
                st.dataframe(pd.DataFrame({
                    "Checkpoint": [x[0] for x in _bunt_items],
                    "Value":      [x[1] for x in _bunt_items],
                }), hide_index=True, use_container_width=True)
            else:
                # --- Well-Hit rates ---
                _wh_items: list[tuple[str, object]] = [
                    ("s1 (1B spd)",   dbg.get("s1")),
                    ("s2 (2B spd)",   dbg.get("s2")),
                    ("s3 (3B spd)",   dbg.get("s3")),
                    ("lead spd",      dbg.get("lead_spd")),
                    ("trail spd",     dbg.get("trail_spd")),
                    ("lead WH%",      f"{dbg.get('lead_wh', 0):.4f}"),
                    ("trail WH%",     f"{dbg.get('trail_wh') or 0:.4f}"),
                    ("2BWH rate",     f"{dbg.get('2bwh_rate', 0):.4f}"),
                    ("1BWH rate",     f"{dbg.get('1bwh_rate', 0):.4f}"),
                    ("1BWH2 rate",    f"{dbg.get('1bwh2_rate', 0):.4f}"),
                    ("IF1B bonus",    dbg.get("if1b_bonus")),
                    ("w_2BWH",        dbg.get("w_2bwh")),
                    ("w_1BWH",        dbg.get("w_1bwh")),
                    ("w_1BWH2",       dbg.get("w_1bwh2")),
                    ("w_1B plain",    dbg.get("w_1b_plain")),
                ]
                # --- FO detail ---
                _fo_items: list[tuple[str, object]] = [
                    ("after_hits",    dbg.get("after_hits")),
                    ("FO rate",       f"{dbg.get('fo_rate', 0):.4f}"),
                    ("PO rate",       f"{dbg.get('po_rate', 0):.4f}"),
                    ("w_FO pool",     dbg.get("w_fo")),
                    ("w_PO",          dbg.get("w_po")),
                    ("DFO%",          f"{dbg.get('dfo_pct', 0):.4f}"),
                ]
                for _fn, _fw in (dbg.get("fo_rows") or []):
                    _fo_items.append((_fn, _fw))
                # --- GO detail ---
                _go_items: list[tuple[str, object]] = [
                    ("GO pool",       dbg.get("w_go")),
                    ("dp_base",       f"{dbg.get('dp_base', 0):.4f}"),
                    ("dp_mult",       dbg.get("dp_mult")),
                ]
                for _gn, _gr, _gw in (dbg.get("go_detail") or []):
                    _go_items.append((f"{_gn} rate", _gr))
                    _go_items.append((f"{_gn} width", _gw))
                _dc3, _dc4, _dc5 = st.columns(3)
                with _dc3:
                    st.dataframe(pd.DataFrame({
                        "Well-Hit":  [x[0] for x in _wh_items],
                        "Value":     [x[1] for x in _wh_items],
                    }), hide_index=True, use_container_width=True)
                with _dc4:
                    st.dataframe(pd.DataFrame({
                        "FO Detail": [x[0] for x in _fo_items],
                        "Value":     [x[1] for x in _fo_items],
                    }), hide_index=True, use_container_width=True)
                with _dc5:
                    st.dataframe(pd.DataFrame({
                        "GO Detail": [x[0] for x in _go_items],
                        "Value":     [x[1] for x in _go_items],
                    }), hide_index=True, use_container_width=True)

    def _hnr_outcome_grid(hnr_ranges, obc, outs, hnr_k_steal_safe_rng, remaining=None, batting_lead=0):
        """Like _outcome_grid but splits K into K+SB and K+CS rows using normal steal speed."""
        sp = min(hnr_k_steal_safe_rng * 2 / 1000, 1.0)
        op = 1.0 - sp
        rows = []
        for entry in (hnr_ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)
            if r == "K":
                _, _, k_nout = _lookup("K", obc, outs)
                k_nout = min(k_nout, 3)
                # K + SB row
                sb_obc, sb_runs = _hnr_steal_advance_obc(obc)
                sb_er = round(utils.get_expected_runs(k_nout, sb_obc) or 0, 2) if k_nout < 3 else 0.0
                sb_row = {
                    "_sort":      sb_runs + sb_er,
                    "Result":     "K+SB",
                    "Range":      f"{lo}-{hi}",
                    "Prob":       f"{prob * sp * 100:.1f}%",
                    "3-2-1":      utils.obc_circles(sb_obc),
                    "Outs After": "End" if k_nout >= 3 else k_nout,
                    "Runs":       int(sb_runs),
                    "ER After":   sb_er,
                }
                if remaining is not None and _wp_table_ready:
                    sb_row["Exp WP"] = f"{_wp_for_state(remaining, k_nout, sb_obc, batting_lead + int(round(sb_runs))) * 100:.1f}%"
                rows.append(sb_row)
                # K + CS row
                cs_nout = min(k_nout + 1, 3)
                cs_obc  = "000" if cs_nout >= 3 else _hnr_steal_cs_obc(obc)
                cs_er   = round(utils.get_expected_runs(cs_nout, cs_obc) or 0, 2) if cs_nout < 3 else 0.0
                cs_row = {
                    "_sort":      cs_er,
                    "Result":     "K+CS",
                    "Range":      f"{lo}-{hi}",
                    "Prob":       f"{prob * op * 100:.1f}%",
                    "3-2-1":      utils.obc_circles(cs_obc),
                    "Outs After": "End" if cs_nout >= 3 else cs_nout,
                    "Runs":       0,
                    "ER After":   cs_er,
                }
                if remaining is not None and _wp_table_ready:
                    cs_row["Exp WP"] = f"{_wp_for_state(remaining, cs_nout, cs_obc, batting_lead) * 100:.1f}%"
                rows.append(cs_row)
            else:
                runs, new_obc_code, nout = _lookup(r, obc, outs)
                nout = min(nout, 3)
                er_after = round(utils.get_expected_runs(nout, new_obc_code) or 0, 2) if nout < 3 else 0.0
                row = {
                    "_sort":      runs + er_after,
                    "Result":     r,
                    "Range":      f"{lo}-{hi}",
                    "Prob":       f"{prob * 100:.1f}%",
                    "3-2-1":      utils.obc_circles(new_obc_code),
                    "Outs After": "End" if nout >= 3 else nout,
                    "Runs":       int(runs),
                    "ER After":   er_after,
                }
                if remaining is not None and _wp_table_ready:
                    row["Exp WP"] = f"{_wp_for_state(remaining, nout, new_obc_code, batting_lead + int(round(runs))) * 100:.1f}%"
                rows.append(row)
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)

    def _show_hnr_outcome_grid(hnr_ranges, obc, outs, hnr_k_steal_safe_rng, key, remaining=None, batting_lead=0):
        grid = _hnr_outcome_grid(hnr_ranges, obc, outs, hnr_k_steal_safe_rng, remaining, batting_lead)
        if grid.empty:
            return
        st.plotly_chart(_outcome_scatter(grid), use_container_width=True, key=f"{key}_scatter")
        with st.expander("Outcome Breakdown", expanded=False):
            st.dataframe(grid, use_container_width=True, hide_index=True, key=key)

    def _calc_steal_ev_and_probs(safe_range):
        safe_prob = min(safe_range * 2 / 1000, 1.0)
        out_prob  = 1.0 - safe_prob
        safe_obc, safe_runs = utils.steal_advance(_current_obc, _current_outs)
        safe_ner  = utils.get_expected_runs(_current_outs, safe_obc) or 0
        out_obc, _ = utils.steal_cs(_current_obc)
        out_nout = min(_current_outs + 1, 3)
        out_ner  = utils.get_expected_runs(out_nout, out_obc) or 0 if out_nout < 3 else 0
        ev = safe_prob * (safe_runs + safe_ner) + out_prob * out_ner
        _tprobs: dict[int, float] = {}
        _simm   = int(safe_runs)
        _sadist = utils._re_dist.get((_current_outs, safe_obc), {0: 1.0})
        for _add, _p2 in _sadist.items():
            _n = _simm + _add
            _tprobs[_n] = _tprobs.get(_n, 0.0) + safe_prob * _p2
        _oadist = utils._re_dist.get((out_nout, out_obc), {0: 1.0}) if out_nout < 3 else {0: 1.0}
        for _add, _p2 in _oadist.items():
            _tprobs[_add] = _tprobs.get(_add, 0.0) + out_prob * _p2
        p1r  = _tprobs.get(1, 0.0)
        p2r  = _tprobs.get(2, 0.0)
        p3pr = sum(p for r, p in _tprobs.items() if r >= 3)
        return ev, p1r, p2r, p3pr

    def _hnr_steal_advance_obc(obc: str) -> tuple[str, int]:
        """Advance the H&R runner on a successful steal-on-K."""
        on_3b, on_2b, on_1b = obc[0] == "1", obc[1] == "1", obc[2] == "1"
        if on_1b and on_2b:
            return "110", 0     # both advance: 1B->2B, 2B->3B
        if on_1b:
            return f"{obc[0]}10", 0  # 1B->2B, 3B stays
        elif on_2b:
            return "100", 0     # 2B->3B
        elif on_3b:
            return "000", 1     # 3B->home
        return obc, 0

    def _hnr_steal_cs_obc(obc: str) -> str:
        """OBC after the H&R runner is caught stealing."""
        if obc[1] == "1" and obc[2] == "1":  # 011: 2B runner caught at 3rd, 1B safely at 2nd
            return "010"
        if obc[2] == "1":       # 1B runner caught (001, 101)
            return f"{obc[0]}00"
        elif obc[1] == "1":     # 2B runner caught (010)
            return f"{obc[0]}00"
        else:                   # 3B runner caught
            return "000"

    def _calc_ev_hnr_and_probs(hnr_ranges, hnr_k_steal_safe_rng):
        """EV for hit and run: non-K outcomes use BRC; K steal uses normal speed (no +1 boost)."""
        sp = min(hnr_k_steal_safe_rng * 2 / 1000, 1.0)
        op = 1.0 - sp
        ev = 0.0
        _tprobs: dict[int, float] = {}
        for entry in (hnr_ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)
            if r == "K":
                _, _, k_nout = _lookup("K", _current_obc, _current_outs)
                k_nout = min(k_nout, 3)
                s_obc, s_runs = _hnr_steal_advance_obc(_current_obc)
                s_ner  = utils.get_expected_runs(k_nout, s_obc) or 0 if k_nout < 3 else 0
                cs_nout = min(k_nout + 1, 3)
                cs_obc  = "000" if cs_nout >= 3 else _hnr_steal_cs_obc(_current_obc)
                cs_ner  = utils.get_expected_runs(cs_nout, cs_obc) or 0 if cs_nout < 3 else 0
                ev += prob * (sp * (s_runs + s_ner) + op * cs_ner)
                _simm  = int(s_runs)
                _sadist = utils._re_dist.get((k_nout, s_obc), {0: 1.0}) if k_nout < 3 else {0: 1.0}
                for _add, _p2 in _sadist.items():
                    _n = _simm + _add
                    _tprobs[_n] = _tprobs.get(_n, 0.0) + prob * sp * _p2
                _csdist = utils._re_dist.get((cs_nout, cs_obc), {0: 1.0}) if cs_nout < 3 else {0: 1.0}
                for _add, _p2 in _csdist.items():
                    _tprobs[_add] = _tprobs.get(_add, 0.0) + prob * op * _p2
            else:
                runs, new_obc, nout = _lookup(r, _current_obc, _current_outs)
                nout = min(nout, 3)
                ner = utils.get_expected_runs(nout, new_obc) or 0 if nout < 3 else 0
                ev += prob * (runs + ner)
                _imm   = int(runs)
                _adist = utils._re_dist.get((nout, new_obc), {0: 1.0}) if nout < 3 else {0: 1.0}
                for _add, _p2 in _adist.items():
                    _n = _imm + _add
                    _tprobs[_n] = _tprobs.get(_n, 0.0) + prob * _p2
        p1r  = _tprobs.get(1, 0.0)
        p2r  = _tprobs.get(2, 0.0)
        p3pr = sum(p for r, p in _tprobs.items() if r >= 3)
        return ev, p1r, p2r, p3pr

    _bunt_ranges      = st.session_state.get("pred_bunt_ranges") or result_ranges
    _bunt_from_sheet  = bool(st.session_state.get("pred_bunt_ranges"))

    # Helper: look up runner speed from session state given a base key and OBC bit
    def _mgr_rspd(base_ltr: str, obc_bit: str) -> int | None:
        if obc_bit != "1":
            return None
        _n = st.session_state.get(f"pred_calc_{base_ltr}b", "Empty")
        if _n != "Empty":
            return _stat(_pbyn.get(_n, {}), "spd") if _n in _pbyn else None
        return st.session_state.get(f"pred_calc_{base_ltr}b_spd")

    _mgr_r1 = _mgr_rspd("1", _current_obc[2])
    _mgr_r2 = _mgr_rspd("2", _current_obc[1])
    _mgr_r3 = _mgr_rspd("3", _current_obc[0])

    _mgr_kwargs = dict(
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
        outs=_current_outs,
        runners_on=_current_obc != "000",
        obc=_current_obc,
        runner_1b_spd=_mgr_r1,
        runner_2b_spd=_mgr_r2,
        runner_3b_spd=_mgr_r3,
    )
    _if_in_checked = bool(st.session_state.get("pred_calc_if_in", False))

    _ifin_from_sheet = bool(st.session_state.get("pred_ifinfield_ranges"))
    _if_in_ranges = (
        st.session_state.get("pred_ifinfield_ranges")
        or utils.compute_at_bat_ranges(
            bunt=False, hit_and_run=False, infield_in=True, **_mgr_kwargs,
        )
    )
    _hnr_sheet_key = "pred_hnr_ifin_ranges" if _if_in_checked else "pred_hnr_ranges"
    _hnr_from_sheet = bool(st.session_state.get(_hnr_sheet_key))
    _hnr_ranges = (
        st.session_state.get(_hnr_sheet_key)
        or utils.compute_at_bat_ranges(
            bunt=False, hit_and_run=True, infield_in=_if_in_checked, **_mgr_kwargs,
        )
    )

    _steal_runners = st.session_state.get("steal_runner_data") or []
    if pred_mode == "Historical / Manual" and _current_obc != "000":
        _p_awr     = int(st.session_state.get("pred_calc_p_awr", 3))
        _c_eye     = int(st.session_state.get("pred_calc_c_eye", 3))
        _def_stat  = (_p_awr + _c_eye) / 2.0
        _steal_to  = {"1B": "2nd", "2B": "3rd", "3B": "home"}
        _base_bit  = {"3B": 0, "2B": 1, "1B": 2}
        _run_key   = {"3B": "pred_calc_3b", "2B": "pred_calc_2b", "1B": "pred_calc_1b"}
        _man_runners = []
        for _base in ("3B", "2B", "1B"):
            if _current_obc[_base_bit[_base]] == "1":
                _rname = st.session_state.get(_run_key[_base], "Empty")
                _rspd  = _stat(_pbyn.get(_rname, {}), "spd")
                _diff  = _rspd - _def_stat
                _tidx  = min(range(len(utils.STEAL_DIFFS)), key=lambda i: abs(utils.STEAL_DIFFS[i] - _diff))
                _man_runners.append({"base": _base, "safe_range": utils.STEAL_TABLE[_steal_to[_base]][_tidx]})
        _steal_runners = _man_runners
    # Default safe range: lead runner from sheet/manual, or fallback to 50
    _default_safe_rng = _steal_runners[0]["safe_range"] if _steal_runners else 50

    # H&R valid with < 2 outs and runner on 1B, 2B, 1B+2B, or 1B+3B
    _HNR_VALID_OBCS = {"001", "010", "011", "101"}
    _has_hnr = _current_obc in _HNR_VALID_OBCS and _current_outs < 2 and bool(_steal_runners)
    if _has_hnr:
        # For OBC 101 (1B+3B), H&R steals with the 1B runner; all others use the lead runner
        _hnr_steal_runner = (
            next((r for r in _steal_runners if r["base"] == "1B"), None)
            if _current_obc == "101"
            else (_steal_runners[0] if _steal_runners else None)
        )
        _hnr_normal_rng = _hnr_steal_runner["safe_range"] if _hnr_steal_runner else 50
    else:
        _hnr_steal_runner = None
        _hnr_normal_rng   = 50

    # Infield In valid when runner on 3rd and fewer than 2 outs
    _has_if_in = _current_obc[0] == "1" and _current_outs < 2

    _wp_table_ready = utils.get_win_probability(1, 0, "000", 0) is not None

    def _wp_for_state(remaining, outs, obc, batting_lead):
        """WP for the batting team given post-play state. Handles inning-end team switch."""
        outs = min(outs, 3)
        if outs < 3:
            return utils.get_win_probability_interpolated(remaining, outs, obc, batting_lead) or 0.5
        if remaining > 1:
            return 1.0 - (utils.get_win_probability_interpolated(remaining - 1, 0, "000", -batting_lead) or 0.5)
        return 1.0 if batting_lead > 0 else (0.5 if batting_lead == 0 else 0.0)

    def _calc_wp_after(ranges, remaining, batting_lead):
        total = 0.0
        for entry in (ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)
            runs_f, new_obc, new_outs = _lookup(r, _current_obc)
            new_bl = batting_lead + int(round(runs_f))
            total += prob * _wp_for_state(remaining, min(new_outs, 3), new_obc, new_bl)
        return total

    def _calc_steal_wp_after(steal_ev_rng, remaining, batting_lead):
        safe_prob = min(steal_ev_rng * 2 / 1000, 1.0)
        out_prob  = 1.0 - safe_prob
        safe_obc, safe_runs = utils.steal_advance(_current_obc, _current_outs)
        cs_obc, _ = utils.steal_cs(_current_obc)
        cs_nout   = min(_current_outs + 1, 3)
        safe_wp = _wp_for_state(remaining, _current_outs, safe_obc, batting_lead + int(safe_runs))
        cs_wp   = _wp_for_state(remaining, cs_nout, cs_obc, batting_lead)
        return safe_prob * safe_wp + out_prob * cs_wp

    def _calc_hnr_wp_after(hnr_ranges, hnr_k_steal_safe_rng, remaining, batting_lead):
        sp = min(hnr_k_steal_safe_rng * 2 / 1000, 1.0)
        op = 1.0 - sp
        total = 0.0
        for entry in (hnr_ranges or []):
            r, lo, hi = _norm(entry)
            prob = min((hi - lo + 1) * 2 / 1000, 1.0)
            if r == "K":
                _, _, k_nout = _lookup("K", _current_obc, _current_outs)
                k_nout = min(k_nout, 3)
                s_obc, s_runs = _hnr_steal_advance_obc(_current_obc)
                s_wp = _wp_for_state(remaining, k_nout, s_obc, batting_lead + int(s_runs))
                cs_nout_wp = min(k_nout + 1, 3)
                cs_obc = "000" if cs_nout_wp >= 3 else _hnr_steal_cs_obc(_current_obc)
                cs_wp  = _wp_for_state(remaining, cs_nout_wp, cs_obc, batting_lead)
                total += prob * (sp * s_wp + op * cs_wp)
            else:
                runs_f, new_obc, new_outs = _lookup(r, _current_obc, _current_outs)
                new_bl = batting_lead + int(round(runs_f))
                total += prob * _wp_for_state(remaining, min(new_outs, 3), new_obc, new_bl)
        return total

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

        # Read game state inputs set in Matchup Setup
        _mgr_inning     = int(st.session_state.get("mgr_inning", 1))
        _mgr_half       = str(st.session_state.get("mgr_half", "Top"))
        _mgr_away_score = int(st.session_state.get("mgr_away_score", 0))
        _mgr_home_score = int(st.session_state.get("mgr_home_score", 0))
        _mgr_remaining  = utils.remaining_half_innings(_mgr_inning, _mgr_half.lower(), utils.game_innings(st.session_state.get("mgr_league", "MLN")))
        _mgr_batting_lead = (
            _mgr_home_score - _mgr_away_score if _mgr_half == "Bottom"
            else _mgr_away_score - _mgr_home_score
        )
        if _wp_table_ready:
            _current_wp = utils.get_win_probability_interpolated(
                _mgr_remaining, _current_outs, _current_obc, _mgr_batting_lead
            ) or 0.5

        ev_swing, s_p1r, s_p2r, s_p3pr = _calc_ev_and_probs(result_ranges)
        ev_bunt,  b_p1r, b_p2r, b_p3pr = _calc_ev_and_probs(_bunt_ranges)
        if _has_runners:
            ev_steal, st_p1r, st_p2r, st_p3pr = _calc_steal_ev_and_probs(_steal_ev_rng)
        else:
            ev_steal = st_p1r = st_p2r = st_p3pr = None
        if _has_hnr:
            ev_hr, hr_p1r, hr_p2r, hr_p3pr = _calc_ev_hnr_and_probs(_hnr_ranges, _hnr_normal_rng)
        else:
            ev_hr = hr_p1r = hr_p2r = hr_p3pr = None
        if _has_if_in:
            ev_ifin, ii_p1r, ii_p2r, ii_p3pr = _calc_ev_and_probs(_if_in_ranges)
        else:
            ev_ifin = ii_p1r = ii_p2r = ii_p3pr = None

        # WP after each decision
        if _wp_table_ready:
            wp_swing = _calc_wp_after(result_ranges, _mgr_remaining, _mgr_batting_lead)
            wp_bunt  = _calc_wp_after(_bunt_ranges,  _mgr_remaining, _mgr_batting_lead)
            wp_steal = _calc_steal_wp_after(_steal_ev_rng, _mgr_remaining, _mgr_batting_lead) if _has_runners else None
            wp_hnr   = _calc_hnr_wp_after(_hnr_ranges, _hnr_normal_rng, _mgr_remaining, _mgr_batting_lead) if _has_hnr else None
            wp_ifin  = _calc_wp_after(_if_in_ranges, _mgr_remaining, _mgr_batting_lead) if _has_if_in else None
        else:
            wp_swing = wp_bunt = wp_steal = wp_hnr = wp_ifin = None

        # EV summary table
        _gs_col, _tbl_col = st.columns([1, 2])
        with _gs_col:
            if _wp_table_ready:
                st.caption(f"Current WP: {_current_wp * 100:.1f}%")
                _li = utils.compute_leverage(result_ranges, _mgr_remaining, _current_outs, _current_obc, _mgr_batting_lead)
                if _li is not None:
                    st.caption(f"Leverage: {_li:.2f}")
            st.caption(f"Baseline ER: {_current_er:.2f}")
        with _tbl_col:
            _decisions = ["Normal Swing", "Bunt"]
            _exp_runs  = [f"{ev_swing:.2f}", f"{ev_bunt:.2f}"]
            _p1r_col   = [f"{s_p1r*100:.1f}%", f"{b_p1r*100:.1f}%"]
            _p2r_col   = [f"{s_p2r*100:.1f}%", f"{b_p2r*100:.1f}%"]
            _p3pr_col  = [f"{s_p3pr*100:.1f}%", f"{b_p3pr*100:.1f}%"]
            _wp_col    = [f"{wp_swing*100:.1f}%" if wp_swing is not None else "-",
                          f"{wp_bunt*100:.1f}%"  if wp_bunt  is not None else "-"]
            if _has_runners:
                _decisions += ["Steal"]
                _exp_runs  += [f"{ev_steal:.2f}"]
                _p1r_col   += [f"{st_p1r*100:.1f}%"]
                _p2r_col   += [f"{st_p2r*100:.1f}%"]
                _p3pr_col  += [f"{st_p3pr*100:.1f}%"]
                _wp_col    += [f"{wp_steal*100:.1f}%" if wp_steal is not None else "-"]
            if _has_hnr:
                _decisions += ["Hit and Run"]
                _exp_runs  += [f"{ev_hr:.2f}"]
                _p1r_col   += [f"{hr_p1r*100:.1f}%"]
                _p2r_col   += [f"{hr_p2r*100:.1f}%"]
                _p3pr_col  += [f"{hr_p3pr*100:.1f}%"]
                _wp_col    += [f"{wp_hnr*100:.1f}%" if wp_hnr is not None else "-"]
            if _has_if_in:
                _decisions += ["vs. Infield In"]
                _exp_runs  += [f"{ev_ifin:.2f}"]
                _p1r_col   += [f"{ii_p1r*100:.1f}%"]
                _p2r_col   += [f"{ii_p2r*100:.1f}%"]
                _p3pr_col  += [f"{ii_p3pr*100:.1f}%"]
                _wp_col    += [f"{wp_ifin*100:.1f}%" if wp_ifin is not None else "-"]
            _tbl_data = {"Decision": _decisions}
            if _wp_table_ready:
                _tbl_data["Exp WP"] = _wp_col
            _tbl_data.update({
                "Exp Runs": _exp_runs,
                "P(1R)": _p1r_col,
                "P(2R)": _p2r_col,
                "P(3+R)": _p3pr_col,
            })
            st.dataframe(
                pd.DataFrame(_tbl_data),
                use_container_width=True, hide_index=True,
            )

        _proposed  = st.number_input("Proposed Value", min_value=1, max_value=1000,
                                    value=500, step=1, key="mgr_proposed")
        _mgr_debug = st.toggle("Debug Info", key="mgr_debug")

        if _mgr_debug:
            _dbg_base_url = st.session_state.get("_dbg_base_url", "(not fetched yet)")
            _dbg_sheets   = st.session_state.get("_dbg_stadium_sheets", {})
            _dbg_sc_errs  = st.session_state.get("_dbg_scenario_errors", {})
            st.caption(f"Stadium lookup URL: `{_dbg_base_url}`")
            st.caption(f"Stadium sheet columns: `{_dbg_sheets}`")
            if _dbg_sc_errs:
                st.caption(f"Scenario fetch errors: `{_dbg_sc_errs}`")
            _, _swing_dbg = utils.compute_at_bat_ranges(
                bunt=False, hit_and_run=False, infield_in=False, _debug=True, **_mgr_kwargs,
            )
            _, _bunt_dbg = utils.compute_at_bat_ranges(
                bunt=True, hit_and_run=False, infield_in=False, _debug=True, **_mgr_kwargs,
            )
            _, _hnr_dbg = utils.compute_at_bat_ranges(
                bunt=False, hit_and_run=True,
                infield_in=bool(st.session_state.get("pred_calc_if_in", False)),
                _debug=True, **_mgr_kwargs,
            )
            _, _ifin_dbg = utils.compute_at_bat_ranges(
                bunt=False, hit_and_run=False, infield_in=True, _debug=True, **_mgr_kwargs,
            )
        else:
            _swing_dbg = _bunt_dbg = _hnr_dbg = _ifin_dbg = {}

        st.divider()

        st.subheader("Normal Swing")
        st.caption("Ranges: from stadium sheet")
        st.plotly_chart(utils.manager_color_bar(int(_proposed), result_ranges,
                        label="Swing", x_label="Swing Values"),
                        use_container_width=True, key="mgr_bar_swing")
        if _mgr_debug:
            _show_debug_panel(_swing_dbg, "Normal Swing")
        _show_outcome_grid(result_ranges, _current_obc, _current_outs, "grid_swing",
                           _mgr_remaining if _wp_table_ready else None, _mgr_batting_lead)

        st.divider()

        st.subheader("Bunt")
        st.caption("Ranges: from stadium sheet" if _bunt_from_sheet else "Ranges: no bunt sheet - using normal swing ranges")
        st.plotly_chart(utils.manager_color_bar(int(_proposed), _bunt_ranges,
                        label="Bunt", x_label="Bunt Values"),
                        use_container_width=True, key="mgr_bar_bunt")
        if _mgr_debug:
            _show_debug_panel(_bunt_dbg, "Bunt")
        _show_outcome_grid(_bunt_ranges, _current_obc, _current_outs, "grid_bunt",
                           _mgr_remaining if _wp_table_ready else None, _mgr_batting_lead)

        if _has_runners:
            st.divider()
            st.subheader("Steal")
            st.caption(f"Color bar uses sheet safe range: {_sheet_safe_rng}")
            st.plotly_chart(utils.steal_color_bar(int(_proposed), int(_sheet_safe_rng),
                            label="Steal", x_label="Steal Values"),
                            use_container_width=True, key="mgr_bar_steal")
            _steal_ranges_for_grid = [
                ("Safe", 1, _sheet_safe_rng),
                ("Out",  _sheet_safe_rng + 1, 500),
            ]
            _show_outcome_grid(_steal_ranges_for_grid, _current_obc, _current_outs, "grid_steal",
                               _mgr_remaining if _wp_table_ready else None, _mgr_batting_lead)

        if _has_hnr:
            st.divider()
            st.subheader("Hit and Run")
            st.caption("Ranges: from stadium sheet" if _hnr_from_sheet else "Ranges: calculated live")
            _hnr_base_lbl = _hnr_steal_runner["base"] if _hnr_steal_runner else "?"
            _orig_rng     = _hnr_steal_runner["safe_range"] if _hnr_steal_runner else "?"
            st.caption(f"K: {_hnr_base_lbl} runner steals at normal range {_orig_rng}")
            st.plotly_chart(utils.manager_color_bar(int(_proposed), _hnr_ranges,
                            label="Swing", x_label="Swing Values"),
                            use_container_width=True, key="mgr_bar_hr")
            if _mgr_debug:
                _show_debug_panel(_hnr_dbg, "Hit and Run")
            _show_hnr_outcome_grid(_hnr_ranges, _current_obc, _current_outs, _hnr_normal_rng,
                                   "grid_hr", _mgr_remaining if _wp_table_ready else None, _mgr_batting_lead)

        if _has_if_in:
            st.divider()
            st.subheader("Infield In (opponent defensive option)")
            st.caption("Ranges: from stadium sheet" if _ifin_from_sheet else "Ranges: calculated live")
            st.caption("+20 to 1B, IF1B shifted to 1B, GORA disabled.")
            st.plotly_chart(utils.manager_color_bar(int(_proposed), _if_in_ranges,
                            label="Swing", x_label="Swing Values"),
                            use_container_width=True, key="mgr_bar_ifin")
            if _mgr_debug:
                _show_debug_panel(_ifin_dbg, "Infield In")
            _show_outcome_grid(_if_in_ranges, _current_obc, _current_outs, "grid_ifin",
                               _mgr_remaining if _wp_table_ready else None, _mgr_batting_lead)

    _manager_fragment()

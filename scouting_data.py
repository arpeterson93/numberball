"""Shared scouting data loaders, used by the Streamlit app and the Discord bot.

Keeping these in one module (rather than duplicating them inside
pages/2_Scouting.py) means both surfaces query and cache plays data the same
way.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

import database as db
import utils


def _retry(fn, attempts: int = 2, delay: float = 1.5):
    """Run fn(), retrying on exception - the public Google Sheets CSV export
    occasionally fails transiently under the handful of near-simultaneous
    fetches resolve_game_state makes. Re-raises the last exception if every
    attempt fails."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last_exc


@st.cache_data(ttl=3600)
def load_all_players() -> list:
    return db.get_all_players()


@st.cache_data(ttl=3600)
def player_dir() -> dict:
    """Group player rows by player_id (the shared cross-season/-league human id).

    Returns:
      name_to_pid  - every name a human has used -> their player_id
      pid_to_name  - player_id -> most-recent name (highest season)
      pid_to_row   - player_id -> most-recent full row
    Rows without a player_id (unsynced legacy) are skipped here; callers fall
    back to name-based lookups for those.
    """
    ordered = sorted(load_all_players(), key=lambda p: p.get("season") or 0)  # ascending
    name_to_pid, pid_to_name, pid_to_row = {}, {}, {}
    for p in ordered:
        pid, nm = p.get("player_id"), p.get("name")
        if pid is None or not nm:
            continue
        name_to_pid[nm] = pid   # later (more recent) season wins for a shared name
        pid_to_name[pid] = nm   # most-recent name for the human
        pid_to_row[pid] = p
    return {"name_to_pid": name_to_pid, "pid_to_name": pid_to_name, "pid_to_row": pid_to_row}


@st.cache_data(ttl=3600)
def all_player_names() -> list[str]:
    """Every player, one most-recent name per human."""
    players = load_all_players()
    names_no_pid = {p["name"] for p in players if p.get("name") and not p.get("player_id")}
    return sorted(set(player_dir()["pid_to_name"].values()) | names_no_pid)


@st.cache_data(ttl=3600)
def load_pitcher_plays(pitcher_name: str, leagues: tuple[str, ...] | None = None, data_v: int = 0) -> pd.DataFrame:
    """A pitcher's full play history, resolved by shared player_id when available."""
    pid = player_dir()["name_to_pid"].get(pitcher_name)
    lg = list(leagues) if leagues else None
    raw = (db.get_plays_for_pitcher_id(pid, lg) if pid is not None
           else db.get_plays_for_pitcher(pitcher_name, lg))
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()


@st.cache_data(ttl=3600)
def load_batter_plays(batter_name: str, leagues: tuple[str, ...] | None = None, data_v: int = 0) -> pd.DataFrame:
    """A batter's full play history, resolved by shared player_id when available."""
    pid = player_dir()["name_to_pid"].get(batter_name)
    lg = list(leagues) if leagues else None
    raw = (db.get_plays_for_batter_id(pid, lg) if pid is not None
           else db.get_plays_for_batter(batter_name, lg))
    return utils.enrich_df(utils.flatten_games(raw)) if raw else pd.DataFrame()


@st.cache_data(ttl=3600)
def load_catcher_plays(catcher_name: str, leagues: tuple[str, ...] | None = None, data_v: int = 0) -> pd.DataFrame:
    """A catcher's full play history, resolved by shared player_id when available."""
    pid = player_dir()["name_to_pid"].get(catcher_name)
    lg = list(leagues) if leagues else None
    raw = (db.get_plays_for_catcher_id(pid, lg) if pid is not None
           else db.get_plays_for_catcher(catcher_name, lg))
    return utils.enrich_catcher_df(utils.flatten_games(raw)) if raw else pd.DataFrame()


def load_pitcher_stats() -> pd.DataFrame:
    # db.get_pitcher_stats() already carries the cache (and is what "Refresh
    # Pitcher Stats" busts) - a second cache layer here would just serve a
    # stale snapshot after a refresh. Building the DataFrame itself is cheap.
    rows = db.get_pitcher_stats()
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=3600)
def load_ma_percentiles() -> dict:
    rows = db.get_ma_percentiles()
    return {r["metric"]: r["percentiles"] for r in rows} if rows else {}


@st.cache_data(ttl=3600)
def load_run_lookup() -> dict:
    # (result, before_obc, outs) -> (runs, new_obc, nout_after)
    return utils.load_run_lookup_from_csv("import_BRC.csv")


_LAST2_ROLE_ID_COL = {"pitch": "pitcher_id", "swing": "batter_id", "throw_num": "catcher_id"}


@st.cache_data(ttl=86400)
def _load_last2_digit_league_data() -> dict[str, dict]:
    """Per pitch/swing/throw_num: the league-wide last-2-digit frequency
    baseline, pooled across every recorded play, plus every player's own
    last2_digit_stats (chi2, best-digit deviation) computed against that
    baseline - the reference population utils.last2_digit_percentiles ranks
    one player against to answer "of the humans, is this one more
    clustered," rather than testing against an abstract random-chance null.

    Real players (unlike a uniform-random generator) systematically favor
    round numbers and repeated digits regardless of any individual
    tendency - the baseline isolates what's unusual about ONE player
    specifically, rather than just rediscovering "humans aren't computers"
    for everyone. Pitch/swing/throw are pooled separately since they're
    different game mechanics (and different id columns - pitcher/batter/
    catcher) that may carry different biases.

    One cached function (rather than separate baseline/reference loaders)
    so both are built from a single pull of the whole plays table - it's
    the expensive part (tens of thousands of rows), not the arithmetic on
    top. A day-long ttl: a population-wide pattern that moves slowly.
    """
    rows = db.get_all_play_values()
    result: dict[str, dict] = {}
    for col, id_col in _LAST2_ROLE_ID_COL.items():
        pooled = [0] * 100
        total = 0
        per_player: dict[object, list[int]] = {}
        for r in rows:
            v = r.get(col)
            if v is None:
                continue
            d = int(str(int(v)).zfill(2)[-2:])
            pooled[d] += 1
            total += 1
            pid = r.get(id_col)
            if pid is not None:
                per_player.setdefault(pid, [0] * 100)[d] += 1
        baseline = [c / total for c in pooled] if total else [0.01] * 100

        chi2_ref, best_dev_ref = [], []
        for counts in per_player.values():
            n = sum(counts)
            if n == 0:
                continue
            _, chi2, best_z = utils.last2_digit_stats(n, dict(enumerate(counts)), baseline)
            chi2_ref.append(chi2)
            best_dev_ref.append(best_z)
        chi2_ref.sort()
        best_dev_ref.sort()

        result[col] = {"baseline": baseline, "chi2_ref": chi2_ref, "best_dev_ref": best_dev_ref}
    return result


def load_last2_digit_baseline() -> dict[str, list[float]]:
    """League-wide last-2-digit frequency baseline for pitch/swing/throw_num -
    see _load_last2_digit_league_data."""
    return {col: data["baseline"] for col, data in _load_last2_digit_league_data().items()}


def load_last2_digit_reference() -> dict[str, dict[str, list[float]]]:
    """Per pitch/swing/throw_num: {"chi2_ref": [...], "best_dev_ref": [...]} -
    the sorted league reference arrays utils.last2_digit_percentiles ranks a
    player's own stats against. See _load_last2_digit_league_data."""
    return _load_last2_digit_league_data()


# Stadium scenario-sheet lookups (HNR/Infield-In) are only tracked for the
# current quick-stats season, same constant pages/2_Scouting.py uses.
_MLN_QS_SEASON = 13


def _resolve_sheet_player(players: list[dict], by_sid: dict, by_pid: dict,
                           raw_id: str | None, season, is_mln: bool) -> dict:
    if not raw_id:
        return {}
    row = by_sid.get(f"{season}_{raw_id}", {}) if is_mln and season else {}
    return row or by_pid.get(str(raw_id), {})


@st.cache_data(ttl=30)
def resolve_game_state(game_code: int) -> dict:
    """Everything the Discord bot needs to drive scouting commands and the
    Manager strategy table off a live game, read straight off its Google
    Sheet scoresheet and the players/teams tables - the same sources
    Streamlit's "Fetch Live Matchup" mode and Manager tab use. A short ttl,
    since this reflects an in-progress game that changes play to play.

    game_code is the user-facing code (e.g. 130820), not the opaque internal
    games.id - that's what shows up in sheets/game logs, so it's what callers
    should ask for.

    Returns {} if no game matches game_code, or it has no sheet_url on file.
    Otherwise a dict with:
      pitcher/batter/catcher   - resolved names (None if unresolved)
      outs/obc                 - current game state
      result_ranges/bunt_ranges - swing ranges, straight off the sheet
      hnr_ranges/if_in_ranges  - from the stadium's scenario sheets when
                                 configured, else computed from stats
      steal_runners            - [{"base", "safe_range"}, ...] off the sheet
      season/league/away_score/home_score - from the games table
    """
    game = db.get_game_by_code(game_code)
    sheet_url = (game or {}).get("sheet_url")
    if not sheet_url:
        return {}

    game_id = game["id"]  # internal id - needed for db.get_plays_for_game below
    season = game.get("season")
    league = game.get("league", "MLN")
    is_mln = str(league).upper() == "MLN"

    state: dict = {
        "game_code": game_code, "game_id": game_id, "season": season, "league": league,
        "away_score": game.get("away_score") or 0, "home_score": game.get("home_score") or 0,
        "pitcher": None, "batter": None, "catcher": None,
        "result_ranges": None, "bunt_ranges": None, "swing_type": "Normal Swing", "infield_in": False,
        "outs": 0, "obc": "000", "steal_runners": [],
        "sandbox_ranges": None, "sandbox_pitcher": None, "sandbox_batter": None, "sandbox_catcher": None,
        "sandbox_swing_type": None, "sandbox_infield_in": None, "sandbox_obc": None,
    }

    names_lower = {n.lower(): n for n in all_player_names()}
    players = load_all_players()
    by_sid = {p["s_id"]: p for p in players if p.get("s_id")}
    by_pid = {str(pid): row for pid, row in player_dir()["pid_to_row"].items()}

    def _sheet_player(raw_id):
        return _resolve_sheet_player(players, by_sid, by_pid, raw_id, season, is_mln)

    try:
        normal_ranges, bunt_ranges, batter_name, pitcher_name, swing_type, infield_in = \
            _retry(lambda: utils.parse_result_ranges_from_sheet(sheet_url))
        state["result_ranges"] = normal_ranges
        state["bunt_ranges"] = bunt_ranges
        state["pitcher"] = names_lower.get((pitcher_name or "").lower())
        state["batter"] = names_lower.get((batter_name or "").lower())
        state["swing_type"] = swing_type
        state["infield_in"] = infield_in
    except Exception:
        pass

    gp = {}
    try:
        gp = _retry(lambda: utils.parse_gameplay_from_sheet(sheet_url))
        state["outs"] = gp.get("outs") or 0
        state["obc"] = gp.get("obc") or "000"
        state["steal_runners"] = gp.get("steal_runners") or []
        state["catcher"] = _sheet_player(gp.get("catcher_id")).get("name")
    except Exception:
        pass

    runner_spds = {}
    for base, raw_id in (gp.get("runner_ids") or {}).items():
        row = _sheet_player(raw_id)
        if row.get("spd") is not None:
            runner_spds[base] = int(row["spd"])
    state["runner_spds"] = runner_spds

    pbyn = {p["name"]: p for p in sorted(players, key=lambda p: p.get("season") or 0) if p.get("name")}
    state["pitcher_row"] = pbyn.get(state["pitcher"], {})
    state["batter_row"] = pbyn.get(state["batter"], {})

    # Inning/half aren't on the sheet - derive them the same way the Manager
    # tab does, from the last play synced to Supabase for this game.
    plays = db.get_plays_for_game(game_id) or []
    if plays:
        last_play = sorted(plays, key=lambda p: p.get("play_num") or p.get("id") or 0)[-1]
        li = int(last_play.get("inning") or 1)
        lh = str(last_play.get("half") or "top").lower()
        lo = int(last_play.get("outs") or 0)
        eo = utils.outs_added(str(last_play.get("result") or ""))
        if lo + eo >= 3:
            inning, half = (li, "Bottom") if lh == "top" else (li + 1, "Top")
        else:
            inning, half = li, "Top" if lh == "top" else "Bottom"
    else:
        inning, half = 1, "Top"
    state["inning"] = inning
    state["half"] = half

    # HNR / Infield-In: prefer the stadium's own scenario sheets (configured
    # per-team in the teams table), falling back to utils.compute_at_bat_ranges
    # from stats when a stadium hasn't set those up - same precedence the
    # Manager tab uses.
    base_url = sheet_url.split("/edit")[0] if "/edit" in sheet_url else sheet_url
    stadium = db.get_stadium_sheets(base_url, _MLN_QS_SEASON) or {}
    scenario_urls = {k: stadium[k] for k in ("sheet_hnr", "sheet_ifinfield", "sheet_hnr_ifin")
                      if stadium.get(k)}
    scenario_ranges = utils.fetch_scenario_ranges(scenario_urls) if scenario_urls else {}
    state["if_in_ranges"] = scenario_ranges.get("sheet_ifinfield")
    state["hnr_ranges"] = scenario_ranges.get("sheet_hnr")
    state["hnr_ifin_ranges"] = scenario_ranges.get("sheet_hnr_ifin")

    # Sandbox: a free-form scenario sheet (same Gameday+Gameplay template as
    # the live sheet) a manager can hand-edit to model something the fixed
    # strategies don't cover (pinch-run, pinch-hit, etc.). Pull its own
    # inputs too, so the caller can tell whether it's actually been changed
    # from the live situation or is still just an unedited clone of it.
    sandbox_url = stadium.get("sheet_sandbox")
    if sandbox_url:
        try:
            sb_normal, _, sb_batter_name, sb_pitcher_name, sb_swing_type, sb_infield_in = \
                _retry(lambda: utils.parse_result_ranges_from_sheet(sandbox_url))
            state["sandbox_ranges"] = sb_normal
            state["sandbox_pitcher"] = names_lower.get((sb_pitcher_name or "").lower())
            state["sandbox_batter"] = names_lower.get((sb_batter_name or "").lower())
            state["sandbox_swing_type"] = sb_swing_type
            state["sandbox_infield_in"] = sb_infield_in
        except Exception:
            pass
        try:
            sb_gp = _retry(lambda: utils.parse_gameplay_from_sheet(sandbox_url))
            state["sandbox_obc"] = sb_gp.get("obc") or "000"
            state["sandbox_catcher"] = _sheet_player(sb_gp.get("catcher_id")).get("name")
        except Exception:
            pass

    return state


def resolve_game_roles(game_code: int) -> dict[str, str | None]:
    """Current pitcher/batter/catcher for a live game. See resolve_game_state."""
    state = resolve_game_state(game_code)
    return {"pitcher": state.get("pitcher"), "batter": state.get("batter"), "catcher": state.get("catcher")}

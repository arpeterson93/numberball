"""
Supabase wrapper for Numberball.
Credentials are read from .streamlit/secrets.toml (local/Streamlit Cloud) or
SUPABASE_URL / SUPABASE_KEY environment variables (GitHub Actions / CLI).
"""
from __future__ import annotations

import functools
import json
import os
import sys
import streamlit as st
from supabase import create_client, Client

try:
    from supabase.lib.client_options import SyncClientOptions as _ClientOptions
except ImportError:                      # older supabase-py
    from supabase.lib.client_options import ClientOptions as _ClientOptions

_CHUNK = 1000  # PostgREST's default page cap; matches it to halve round-trips

# Per-process counters for the paginated reads and bulk upserts below (see stats()). Bytes are only estimated when
# SB_TRACK_BYTES is set, since that re-serialises every page.
_STATS = {"read_calls": 0, "read_rows": 0, "read_bytes": 0, "write_calls": 0, "write_rows": 0}


def stats() -> dict:
    return dict(_STATS)


def _client_tag() -> str:
    """Who is calling, sent as X-Client-Info / User-Agent so Supabase's API logs can be grouped by caller: SB_CLIENT_TAG if
    set (the sync workflow sets sync-<run id>), "streamlit-app" inside Streamlit, else script-<entry script name>."""
    tag = os.environ.get("SB_CLIENT_TAG")
    if tag:
        return tag
    try:
        from streamlit.runtime import exists
        if exists():
            return "streamlit-app"
    except Exception:
        pass
    return "script-" + (os.path.splitext(os.path.basename(sys.argv[0] or ""))[0] or "python")


# Local copy of the plays/players tables for analysis tools (tools/local_plays.py sets NB_LOCAL_PLAYS to the pickle). The
# app and bots never set it, so they always read live. Loaded once per process.
_LOCAL: dict = {}


def _local_data() -> dict | None:
    path = os.environ.get("NB_LOCAL_PLAYS")
    if not path:
        return None
    if path not in _LOCAL:
        import pickle
        with open(path, "rb") as fh:
            _LOCAL[path] = pickle.load(fh)
    return _LOCAL[path]


def _local_plays_for(col: str, value, leagues) -> list[dict] | None:
    """Plays where col == value from the local copy (None when local mode is off), shaped like the live queries:
    ordered by id, nested games dict, scrimmage dropped."""
    d = _local_data()
    if d is None:
        return None
    idx = d.setdefault("_idx", {}).get(col)
    if idx is None:
        idx = {}
        for p in d["plays"]:
            idx.setdefault(p.get(col), []).append(p)
        d["_idx"][col] = idx
    return [p for p in idx.get(value, [])
            if (not leagues or p.get("league") in leagues) and p.get("game_type") != "scrimmage"]


def _local_first(col: str):
    """Decorator for the get_plays_for_* lookups: answer from the local copy when local mode is on."""
    def deco(fn):
        @functools.wraps(fn)
        def inner(value, leagues=None):
            rows = _local_plays_for(col, value, leagues)
            return rows if rows is not None else fn(value, leagues)
        return inner
    return deco


@st.cache_resource
def _client() -> Client:
    url = key = ""
    try:
        url = st.secrets.get("supabase_url", "")
        key = st.secrets.get("supabase_key", "")
    except Exception:
        pass
    url = url or os.environ.get("SUPABASE_URL", "")
    key = key or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "Supabase credentials missing. "
            "Add supabase_url/supabase_key to .streamlit/secrets.toml or set "
            "SUPABASE_URL/SUPABASE_KEY environment variables."
        )
    tag = _client_tag()
    return create_client(url, key, options=_ClientOptions(
        headers={"X-Client-Info": f"numberball/{tag}", "User-Agent": f"numberball/{tag}"}))


def _bulk_upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    """Upsert rows in chunks; returns total rows processed."""
    if not rows:
        return 0
    total = 0
    _STATS["write_rows"] += len(rows)
    for i in range(0, len(rows), _CHUNK):
        _STATS["write_calls"] += 1
        # returning="minimal": the default sends every upserted row back, which counts as Supabase egress
        _client().table(table).upsert(rows[i:i + _CHUNK], on_conflict=on_conflict, returning="minimal").execute()
        total += len(rows[i:i + _CHUNK])
    return total


def _fetch_all(query) -> list[dict]:
    """Paginate through a Supabase query, bypassing the default 1000-row limit."""
    results = []
    offset = 0
    while True:
        batch = query.range(offset, offset + _CHUNK - 1).execute().data
        _STATS["read_calls"] += 1
        _STATS["read_rows"] += len(batch)
        if os.environ.get("SB_TRACK_BYTES"):
            _STATS["read_bytes"] += len(json.dumps(batch, default=str))
        results.extend(batch)
        if len(batch) < _CHUNK:
            break
        offset += _CHUNK
    return results


# ------------------------------------------------------------------ games

def get_games() -> list[dict]:
    return _fetch_all(
        _client().table("games")
        .select("*")
        .order("season", desc=True)
        .order("session_number", desc=True)
    )


def get_game_lookup() -> list[dict]:
    """Just id / game_code / league for every game - the sync's game_code -> id map, without get_games()'s select *."""
    return _fetch_all(
        _client().table("games")
        .select("id, game_code, league")
        .order("season", desc=True)
        .order("session_number", desc=True)
        .order("id")
    )


def get_player_name_lookup() -> list[dict]:
    """Just player_id / name for every player - the RLN sync's id -> name map, without get_all_players()'s 17 columns."""
    return _fetch_all(
        _client().table("players")
        .select("player_id, name")
        .order("name")
        .order("player_id")
    )


def get_game_sheet_urls(game_codes: list) -> dict[str, str]:
    """{game_code: sheet_url} for just these games, only those with a scoresheet link on file."""
    codes = [str(c) for c in game_codes]
    if not codes:
        return {}
    rows = (_client().table("games").select("game_code, sheet_url").in_("game_code", codes)
            .not_.is_("sheet_url", "null").execute().data)
    return {str(r["game_code"]): r["sheet_url"] for r in rows if r.get("sheet_url")}


def get_game(game_id: int) -> dict | None:
    rows = (
        _client().table("games")
        .select("*")
        .eq("id", game_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def get_game_by_code(game_code: int) -> dict | None:
    """Look up a game by its user-facing game_code (e.g. 130820), rather than
    the opaque internal id - game_code is what shows up in sheets/game logs."""
    rows = (
        _client().table("games")
        .select("*")
        .eq("game_code", game_code)
        .execute()
        .data
    )
    return rows[0] if rows else None


def create_game(
    season: int,
    session_number: int,
    home_team: str,
    away_team: str,
    start_date=None,
    sheet_url: str | None = None,
    game_code: str | None = None,
) -> dict:
    return (
        _client().table("games")
        .insert({
            "season": season,
            "session_number": session_number,
            "home_team": home_team,
            "away_team": away_team,
            "start_date": str(start_date) if start_date else None,
            "sheet_url": sheet_url or None,
            "game_code": game_code,
        })
        .execute()
        .data[0]
    )


def bulk_upsert_games(games: list[dict]) -> int:
    """Upsert a list of game dicts keyed on game_code. Returns rows processed."""
    return _bulk_upsert("games", games, "game_code")


def delete_game(game_id: int) -> None:
    _client().table("games").delete().eq("id", game_id).execute()


def update_game_sheet_url(game_id: int, sheet_url: str | None) -> None:
    _client().table("games").update({"sheet_url": sheet_url or None}).eq("id", game_id).execute()


# ------------------------------------------------------------------ plays

def get_all_plays(league: str | None = None) -> list[dict]:
    local = _local_data()
    if local is not None:
        return [p for p in local["plays"] if not league or p.get("league") == league]
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .order("id", desc=False)
    )
    if league:
        q = q.eq("league", league)
    return _fetch_all(q)


def get_plays_for_game(game_id: int) -> list[dict]:
    return (
        _client().table("plays")
        .select("*")
        .eq("game_id", game_id)
        .order("play_num", desc=False)
        .execute()
        .data
    )


def get_all_play_values() -> list[dict]:
    """Every play's pitch/swing/throw_num values plus the pitcher/batter/
    catcher id they belong to - for building a league-wide last-2-digit
    baseline distribution and each player's own reference stats. No games
    join (unlike get_all_plays), since this pulls the entire plays table."""
    local = _local_data()
    if local is not None:
        keep = ("pitch", "swing", "throw_num", "pitcher_id", "batter_id", "catcher_id")
        return [{k: p.get(k) for k in keep} for p in local["plays"]]
    return _fetch_all(_client().table("plays").select(
        "pitch, swing, throw_num, pitcher_id, batter_id, catcher_id"))


@_local_first("pitcher_name")
def get_plays_for_pitcher(pitcher_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("pitcher_name", pitcher_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


@_local_first("pitcher_id")
def get_plays_for_pitcher_id(player_id: int, leagues: list[str] | None = None) -> list[dict]:
    """Plays by pitcher_id - ties a human's history together across name changes."""
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("pitcher_id", player_id)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


@_local_first("catcher_name")
def get_plays_for_catcher(catcher_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("catcher_name", catcher_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


@_local_first("catcher_id")
def get_plays_for_catcher_id(player_id: int, leagues: list[str] | None = None) -> list[dict]:
    """Plays by catcher_id - ties a human's history together across name changes."""
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("catcher_id", player_id)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


@_local_first("batter_name")
def get_plays_for_batter(batter_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("batter_name", batter_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


@_local_first("batter_id")
def get_plays_for_batter_id(player_id: int, leagues: list[str] | None = None) -> list[dict]:
    """Plays by batter_id - ties a human's history together across name changes."""
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("batter_id", player_id)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


def get_plays_for_team_offense(team_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("off_team", team_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    raw = _fetch_all(q)
    return [p for p in raw if p.get("game_type") != "scrimmage"]


def get_existing_play_nums(league: str) -> set[int]:
    """Return the set of play_num values already stored for a league. Drives
    incremental sync via membership (not a max), so gaps or out-of-order play
    numbers can't cause a genuinely new play to be silently skipped."""
    rows = _fetch_all(
        _client().table("plays").select("play_num").eq("league", league)
    )
    return {r["play_num"] for r in rows if r.get("play_num") is not None}


def bulk_upsert_plays(plays: list[dict]) -> int:
    """Upsert a list of play dicts keyed on (play_num, league). Returns rows processed."""
    return _bulk_upsert("plays", plays, "play_num,league")


def bulk_upsert_mln_plays(plays: list[dict]) -> int:
    """Upsert MLN archive plays keyed on (play_num, league). Returns rows processed."""
    return _bulk_upsert("plays", plays, "play_num,league")


def delete_play(play_id: int) -> None:
    _client().table("plays").delete().eq("id", play_id).execute()


# ------------------------------------------------------------------ MLN archive

def get_mln_teams_for_lookup() -> list[dict]:
    """Return MLN team records for name resolution during archive sync."""
    return _fetch_all(
        _client().table("teams")
        .select("team_id, abbrev, full_team")
        .eq("league", "MLN")
    )


def get_mln_players_for_lookup() -> list[dict]:
    """Return MLN player records for name resolution during archive sync."""
    return _fetch_all(
        _client().table("players")
        .select("s_id, name")
        .eq("league", "MLN")
    )


def bulk_upsert_mln_teams(teams: list[dict]) -> int:
    """Upsert MLN teams keyed on s_team. Returns rows processed."""
    return _bulk_upsert("teams", teams, "s_team")


def bulk_upsert_mln_players(players: list[dict]) -> int:
    """Upsert MLN players keyed on s_id. Returns rows processed."""
    return _bulk_upsert("players", players, "s_id")


# ------------------------------------------------------------------ scrimmage

def get_scrimmage_games() -> list[dict]:
    """Return all games with game_type='scrimmage', newest first."""
    return _fetch_all(
        _client().table("games")
        .select("*")
        .eq("game_type", "scrimmage")
        .order("id", desc=True)
    )


def get_all_scrimmage_plays() -> list[dict]:
    """Return all plays with game_type='scrimmage' (stored in the shared plays table)."""
    return _fetch_all(
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("game_type", "scrimmage")
        .order("id", desc=False)
    )


def bulk_upsert_scrimmage_plays(plays: list[dict]) -> int:
    """Upsert scrimmage plays into the shared plays table, keyed on (play_num, game_id)."""
    return _bulk_upsert("plays", plays, "play_num,game_id")


# ------------------------------------------------------------------ teams

def get_all_teams() -> list[dict]:
    return _fetch_all(_client().table("teams").select("*").order("abbrev"))


@st.cache_data(ttl=3600)
def get_mln_teams_for_season(season: int) -> list[dict]:
    """MLN teams that existed in a season: abbrev and full team name (matches plays.off_team / def_team)."""
    rows = _fetch_all(
        _client().table("teams")
        .select("abbrev, full_team")
        .eq("league", "MLN")
        .eq("season", season)
        .order("abbrev")
    )
    return [r for r in rows if r.get("abbrev") and r.get("full_team")]


@st.cache_data(ttl=3600)
def get_pitcher_faced_counts(season: int) -> dict[int, int]:
    """{pitcher player_id: swings faced} for live MLN games in a season - the weights for a league pitcher mix."""
    rows = _fetch_all(
        _client().table("plays")
        .select("pitcher_id")
        .eq("league", "MLN")
        .eq("season", season)
        .eq("play_type", "Swing")
        .eq("game_type", "live")
    )
    counts: dict[int, int] = {}
    for r in rows:
        pid = r.get("pitcher_id")
        if pid is not None:
            counts[int(pid)] = counts.get(int(pid), 0) + 1
    return counts


@st.cache_data(ttl=3600)
def get_catcher_faced_counts(season: int) -> dict[int, int]:
    """{catcher player_id: swings caught} for live MLN games in a season - the weights for a league catcher mix."""
    rows = _fetch_all(
        _client().table("plays")
        .select("catcher_id")
        .eq("league", "MLN")
        .eq("season", season)
        .eq("play_type", "Swing")
        .eq("game_type", "live")
    )
    counts: dict[int, int] = {}
    for r in rows:
        cid = r.get("catcher_id")
        if cid is not None:
            counts[int(cid)] = counts.get(int(cid), 0) + 1
    return counts


@st.cache_data(ttl=3600)
def get_team_swing_order(team_name: str, season: int) -> list[dict]:
    """Light (game_id, play_num, batter_id) swing rows for a team's offense in live MLN games - enough to
    estimate who bats where."""
    return _fetch_all(
        _client().table("plays")
        .select("game_id, play_num, batter_id")
        .eq("league", "MLN")
        .eq("season", season)
        .eq("off_team", team_name)
        .eq("play_type", "Swing")
        .eq("game_type", "live")
        .order("play_num")
    )


@st.cache_data(ttl=3600)
def get_stadium_sheets(ballpark_url: str, season: int) -> dict:
    """Return scenario sheet URLs for the stadium matching ballpark_url and season.

    Columns fetched: sheet_hnr, sheet_ifinfield, sheet_hnr_ifin, sheet_sandbox.
    Returns an empty dict if no matching team row is found.
    """
    rows = _fetch_all(
        _client().table("teams")
        .select("sheet_hnr, sheet_ifinfield, sheet_hnr_ifin, sheet_sandbox")
        .eq("ballpark_url", ballpark_url)
        .eq("season", season)
        .limit(1)
    )
    return rows[0] if rows else {}


def bulk_upsert_teams(teams: list[dict]) -> int:
    """Upsert a list of team dicts keyed on s_team. Returns rows processed."""
    return _bulk_upsert("teams", teams, "s_team")


# ------------------------------------------------------------------ players

@st.cache_data(ttl=300)
def get_all_players() -> list[dict]:
    local = _local_data()
    if local is not None and local.get("players") is not None:
        return local["players"]
    return _fetch_all(
        _client().table("players")
        .select("player_id, s_id, season, name, team, primary_pos, secondary_pos, status, gm, hand, con, eye, pwr, spd, mov, cmd, vel, awr")
        .order("name")
    )


def get_players(team: str | None = None, pos: str | None = None) -> list[str]:
    players = get_all_players()
    if team:
        players = [p for p in players if p.get("team", "").strip() == team.strip()]
    if pos:
        players = [p for p in players if p.get("primary_pos", "").strip() == pos.strip()]
    return [p["name"] for p in players]


def bulk_upsert_players(players: list[dict]) -> int:
    """Upsert a list of RLN player dicts keyed on s_id. Returns rows processed.

    Keyed on s_id (not player_id) so player_id can be a shared, non-unique human
    id across leagues/seasons - RLN rows use s_id 'R_<player_id>'."""
    return _bulk_upsert("players", players, "s_id")


# ------------------------------------------------------------------ pitcher stats

@st.cache_data(ttl=3600)
def get_pitcher_stats() -> list[dict]:
    """Load pre-computed pitcher behavioral stats."""
    return _fetch_all(
        _client().table("pitcher_stats")
        .select("*")
        .order("pitcher_name")
    )


def upsert_pitcher_stats(rows: list[dict]) -> int:
    """Insert pitcher stats rows (call clear_pitcher_stats first for a full
    replace). Plain insert, not upsert on pitcher_name - two different humans can
    share a current name, so the name isn't a valid conflict key; identity is
    player_id and compute produces one row per pitcher."""
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), _CHUNK):
        _client().table("pitcher_stats").insert(rows[i:i + _CHUNK]).execute()
        total += len(rows[i:i + _CHUNK])
    return total


def clear_pitcher_stats() -> None:
    """Delete all pitcher_stats rows. Called before a full recompute so that
    stale rows under a pitcher's old name don't linger and double-count in the
    percentile pool."""
    _client().table("pitcher_stats").delete().gt("id", 0).execute()


# ------------------------------------------------------------------ pitcher MA percentiles

@st.cache_data(ttl=3600)
def get_ma_percentiles() -> list[dict]:
    """Load pre-computed 20-pitch rolling-average percentile tables (one row
    per behavioral metric) - see utils.ma_percentile_rows. Used to grade a
    pitcher's recent rolling-average value against everyone's rolling
    averages instead of the (much smoother) career-average distribution."""
    return _fetch_all(
        _client().table("pitcher_ma_percentiles")
        .select("*")
    )


def upsert_ma_percentiles(rows: list[dict]) -> int:
    """Upsert one row per metric - metric is the natural conflict key, and the
    fixed small set of metrics means no stale-row cleanup is needed."""
    return _bulk_upsert("pitcher_ma_percentiles", rows, "metric")


# ------------------------------------------------------------------ user preferences

def get_user_preferences(user_id: str) -> dict:
    rows = (
        _client().table("user_preferences")
        .select("scouting_view, last_sheet_url")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    return rows[0] if rows else {}


def upsert_user_preferences(user_id: str, scouting_view: str) -> None:
    _client().table("user_preferences").upsert(
        {"user_id": user_id, "scouting_view": scouting_view},
        on_conflict="user_id",
    ).execute()


def upsert_last_sheet_url(user_id: str, sheet_url: str) -> None:
    _client().table("user_preferences").upsert(
        {"user_id": user_id, "last_sheet_url": sheet_url},
        on_conflict="user_id",
    ).execute()

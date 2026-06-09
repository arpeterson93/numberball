"""
Supabase wrapper for Numberball.
Credentials are read from .streamlit/secrets.toml (local) or Streamlit Cloud secrets (deployed).
"""
from __future__ import annotations

import streamlit as st
from supabase import create_client, Client

_CHUNK = 500  # max rows per bulk upsert call


@st.cache_resource
def _client() -> Client:
    url = st.secrets.get("supabase_url", "")
    key = st.secrets.get("supabase_key", "")
    if not url or not key:
        raise RuntimeError(
            "Supabase credentials missing. "
            "Add supabase_url and supabase_key to .streamlit/secrets.toml"
        )
    return create_client(url, key)


def _bulk_upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    """Upsert rows in chunks; returns total rows processed."""
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), _CHUNK):
        _client().table(table).upsert(rows[i:i + _CHUNK], on_conflict=on_conflict).execute()
        total += len(rows[i:i + _CHUNK])
    return total


def _fetch_all(query) -> list[dict]:
    """Paginate through a Supabase query, bypassing the default 1000-row limit."""
    results = []
    offset = 0
    while True:
        batch = query.range(offset, offset + _CHUNK - 1).execute().data
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


def get_game(game_id: int) -> dict | None:
    rows = (
        _client().table("games")
        .select("*")
        .eq("id", game_id)
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


@st.cache_data(ttl=300)
def get_plays_for_pitcher(pitcher_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("pitcher_name", pitcher_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    return _fetch_all(q)


@st.cache_data(ttl=300)
def get_plays_for_batter(batter_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("batter_name", batter_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    return _fetch_all(q)


@st.cache_data(ttl=300)
def get_plays_for_team_offense(team_name: str, leagues: list[str] | None = None) -> list[dict]:
    q = (
        _client().table("plays")
        .select("*, games(season, session_number, home_team, away_team, game_code)")
        .eq("off_team", team_name)
        .order("id", desc=False)
    )
    if leagues:
        q = q.in_("league", leagues)
    return _fetch_all(q)


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


# ------------------------------------------------------------------ scrimmage plays

def get_all_scrimmage_plays() -> list[dict]:
    return _fetch_all(
        _client().table("scrimmage_plays")
        .select("*")
        .order("id", desc=False)
    )


def bulk_upsert_scrimmage_plays(plays: list[dict]) -> int:
    return _bulk_upsert("scrimmage_plays", plays, "play_num")


# ------------------------------------------------------------------ teams

def get_all_teams() -> list[dict]:
    return _fetch_all(_client().table("teams").select("*").order("abbrev"))


def bulk_upsert_teams(teams: list[dict]) -> int:
    """Upsert a list of team dicts keyed on abbrev. Returns rows processed."""
    return _bulk_upsert("teams", teams, "abbrev")


# ------------------------------------------------------------------ players

@st.cache_data(ttl=300)
def get_all_players() -> list[dict]:
    return _fetch_all(
        _client().table("players")
        .select("player_id, name, team, primary_pos, secondary_pos, status, gm, hand, con, eye, pwr, spd, mov, cmd, vel, awr")
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
    """Upsert a list of player dicts keyed on player_id. Returns rows processed."""
    return _bulk_upsert("players", players, "player_id")


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
    """Upsert pitcher stats keyed on pitcher_name. Returns rows processed."""
    return _bulk_upsert("pitcher_stats", rows, "pitcher_name")

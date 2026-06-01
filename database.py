"""
Supabase wrapper for Numberball.
Credentials are read from .streamlit/secrets.toml (local) or Streamlit Cloud secrets (deployed).
"""
from __future__ import annotations

import streamlit as st
from supabase import create_client, Client


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


# ------------------------------------------------------------------ games

def get_games() -> list[dict]:
    return (
        _client().table("games")
        .select("*")
        .order("season", desc=True)
        .order("session_number", desc=True)
        .execute()
        .data
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
    game_num: int | None = None,
    away_score: int | None = None,
    home_score: int | None = None,
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
            "game_num": game_num,
            "away_score": away_score,
            "home_score": home_score,
        })
        .execute()
        .data[0]
    )


def upsert_game(
    game_code: str,
    season: int,
    session_number: int,
    home_team: str,
    away_team: str,
    game_num: int | None = None,
    away_score: int | None = None,
    home_score: int | None = None,
    sheet_url: str | None = None,
) -> dict:
    """Insert or update a game record keyed on game_code."""
    existing = (
        _client().table("games")
        .select("id")
        .eq("game_code", game_code)
        .execute()
        .data
    )
    payload: dict = {
        "game_code": game_code,
        "season": season,
        "session_number": session_number,
        "home_team": home_team,
        "away_team": away_team,
        "game_num": game_num,
        "away_score": away_score,
        "home_score": home_score,
    }
    if sheet_url is not None:
        payload["sheet_url"] = sheet_url
    if existing:
        return (
            _client().table("games")
            .update(payload)
            .eq("game_code", game_code)
            .execute()
            .data[0]
        )
    return (
        _client().table("games")
        .insert(payload)
        .execute()
        .data[0]
    )


def delete_game(game_id: int) -> None:
    _client().table("games").delete().eq("id", game_id).execute()


# ------------------------------------------------------------------ at_bats

def get_all_at_bats() -> list[dict]:
    return (
        _client().table("at_bats")
        .select("*, games(season, session_number, home_team, away_team, game_code, game_num)")
        .order("id", desc=False)
        .execute()
        .data
    )


def get_at_bats_for_game(game_id: int) -> list[dict]:
    return (
        _client().table("at_bats")
        .select("*")
        .eq("game_id", game_id)
        .order("id", desc=False)
        .execute()
        .data
    )


def upsert_at_bat(
    game_id: int,
    play_num: int,
    inning: int,
    half: str,
    outs: int,
    obc: str,
    pitcher_team: str,
    batter_team: str,
    pitcher_name: str,
    batter_name: str,
    pitch: int,
    swing: int,
    result: str,
    is_fp_app: bool = False,
    is_fp_inn: bool = False,
) -> dict:
    """Insert or update an at-bat record keyed on play_num."""
    existing = (
        _client().table("at_bats")
        .select("id")
        .eq("play_num", play_num)
        .execute()
        .data
    )
    payload = {
        "game_id": game_id,
        "play_num": play_num,
        "inning": inning,
        "half": half,
        "outs": outs,
        "obc": obc,
        "pitcher_team": pitcher_team,
        "batter_team": batter_team,
        "pitcher_name": pitcher_name,
        "batter_name": batter_name,
        "pitch": pitch,
        "swing": swing,
        "result": result,
        "is_fp_app": is_fp_app,
        "is_fp_inn": is_fp_inn,
    }
    if existing:
        return (
            _client().table("at_bats")
            .update(payload)
            .eq("play_num", play_num)
            .execute()
            .data[0]
        )
    return (
        _client().table("at_bats")
        .insert(payload)
        .execute()
        .data[0]
    )


def delete_at_bat(at_bat_id: int) -> None:
    _client().table("at_bats").delete().eq("id", at_bat_id).execute()


# ------------------------------------------------------------------ players

@st.cache_data(ttl=300)
def get_all_players() -> list[dict]:
    return _client().table("players").select("name, team, pos").order("name").execute().data


def get_players(team: str | None = None, pos: str | None = None) -> list[str]:
    players = get_all_players()
    if team:
        players = [p for p in players if p.get("team", "").strip() == team.strip()]
    if pos:
        players = [p for p in players if p.get("pos", "").strip() == pos.strip()]
    return [p["name"] for p in players]

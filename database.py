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


# ------------------------------------------------------------------ sessions

def get_sessions() -> list[dict]:
    return (
        _client().table("sessions")
        .select("*")
        .order("season", desc=True)
        .order("session_number", desc=True)
        .execute()
        .data
    )


def get_session(session_id: int) -> dict | None:
    rows = (
        _client().table("sessions")
        .select("*")
        .eq("id", session_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def create_session(season: int, session_number: int, home_team: str, away_team: str, game_date=None, sheet_url: str | None = None) -> dict:
    return (
        _client().table("sessions")
        .insert({
            "season": season,
            "session_number": session_number,
            "home_team": home_team,
            "away_team": away_team,
            "game_date": str(game_date) if game_date else None,
            "sheet_url": sheet_url or None,
        })
        .execute()
        .data[0]
    )


def delete_session(session_id: int) -> None:
    _client().table("sessions").delete().eq("id", session_id).execute()


# ------------------------------------------------------------------ at_bats

def get_all_at_bats() -> list[dict]:
    return (
        _client().table("at_bats")
        .select("*, sessions(season, session_number, home_team, away_team)")
        .order("id", desc=False)
        .execute()
        .data
    )


def get_at_bats_for_session(session_id: int) -> list[dict]:
    return (
        _client().table("at_bats")
        .select("*")
        .eq("session_id", session_id)
        .order("id", desc=False)
        .execute()
        .data
    )


def insert_at_bat(
    session_id: int,
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
    return (
        _client().table("at_bats")
        .insert({
            "session_id": session_id,
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
        })
        .execute()
        .data[0]
    )


def delete_at_bat(at_bat_id: int) -> None:
    _client().table("at_bats").delete().eq("id", at_bat_id).execute()



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

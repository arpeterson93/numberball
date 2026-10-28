"""Live MLN games and plays straight off the Google Sheet.

The sheet's Games and Plays (Raw) tabs are the raw source the plays sync copies into Supabase, so they are always at least
as current as Supabase (the sync runs on a delay) and reading them costs no Supabase egress. PioBall uses this for the live
game, the latest play, grading and the model's newest pitches; everything is cached for seconds so the bot's 60-second
loops and its commands share one read (a whole-tab read takes ~2 s).

Every read keeps serving its last good copy for up to _STALE_OK_S if the sheet briefly can't be read; a longer outage
raises, and callers fall back to the Supabase path.
"""
from __future__ import annotations

import threading
import time

import utils
from sync_plays import _MLN_SHEET_ID as SHEET_ID, build_mln_rows

_STALE_OK_S = 900
_TEAMS_TTL = 3600
_GAMES_TTL = 30
_PLAYS_TTL = 20
_NAMES_TTL = 1800

_cache: dict[str, tuple[float, object]] = {}
_locks: dict[str, threading.Lock] = {}


def _cached(key: str, ttl: float, load):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _locks.setdefault(key, threading.Lock()):      # one thread loads, the others wait and reuse it
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = load()
        except Exception:
            if hit and time.time() - hit[0] < _STALE_OK_S:
                return hit[1]
            raise
        _cache[key] = (time.time(), val)
        return val


def teams() -> dict:
    """{"by_abbrev": {...}, "by_id": {...}} of the sheet's Teams tab rows (full_team, ballpark_url, team_id, abbrev)."""
    def load():
        rows = utils.read_mln_teams_from_sheet(SHEET_ID)
        return {"by_abbrev": {r["abbrev"]: r for r in rows if r.get("abbrev")},
                "by_id": {r["team_id"]: r for r in rows if r.get("team_id")}}
    return _cached("teams", _TEAMS_TTL, load)


def games() -> list[dict]:
    """The Games tab shaped like the games table: full team names, league, and sheet_url = the home team's ballpark
    sheet (the live scoresheet - the same link Supabase's games.sheet_url holds for MLN games). No Supabase id."""
    def load():
        by_abbrev = teams()["by_abbrev"]
        out = []
        for g in utils.read_mln_games_from_sheet(SHEET_ID):
            g = dict(g)
            home = by_abbrev.get(g.get("home_team")) or {}
            for k in ("away_team", "home_team"):
                g[k] = (by_abbrev.get(g.get(k)) or {}).get("full_team") or g.get(k)
            g["game_code"] = str(g["game_code"])
            g["league"] = "MLN"
            g["sheet_url"] = home.get("ballpark_url")
            out.append(g)
        return out
    return _cached("games", _GAMES_TTL, load)


def game(game_code) -> dict | None:
    code = str(game_code)
    return next((g for g in games() if g["game_code"] == code), None)


def current_season() -> int | None:
    return max((g.get("season") or 0 for g in games()), default=None) or None


def _raw_plays() -> list[dict]:
    def load():
        rows = utils.read_mln_plays_from_sheet(SHEET_ID, tab="Plays (Raw)")
        rows.sort(key=lambda p: p["play_num"])
        return rows
    return _cached("plays_raw", _PLAYS_TTL, load)


def plays(game_code=None) -> list[dict]:
    """The sheet's plays in play_num order (one game, or all), plus what the plays table adds that grading needs:
    outs before the play and the batting / fielding team names. Same field names and play_type values as Supabase."""
    def load():
        by_id = teams()["by_id"]
        outs_tracker: dict[tuple, int] = {}
        out = []
        for p in _raw_plays():
            key = (p["game_code"], p["inning"], p["half"])
            outs = outs_tracker.setdefault(key, 0)
            outs_tracker[key] = min(3, outs + utils.outs_added(p.get("result") or ""))
            away = (by_id.get(p.get("away")) or {}).get("full_team") or p.get("away")
            home = (by_id.get(p.get("home")) or {}).get("full_team") or p.get("home")
            off, de = (away, home) if p["half"] == "top" else (home, away)
            out.append({**p, "outs": outs, "off_team": off, "def_team": de})
        return out
    allp = _cached("plays", _PLAYS_TTL, load)
    if game_code is None:
        return allp
    code = str(game_code)
    return [p for p in allp if str(p["game_code"]) == code]


def last_play(game_code) -> dict | None:
    ps = plays(game_code)
    return {"play_num": ps[-1]["play_num"], "timestamp": ps[-1].get("timestamp")} if ps else None


def history_rows() -> list[dict]:
    """Every current-season sheet play built into a full plays-table row (player names, first-pitch flags, diff, ...) with
    the same builder the sync uses, plus the nested games dict the app's queries carry. Needs the players name lookup from
    Supabase (cached for _NAMES_TTL), so unlike plays() it is only used for the model's pitcher history."""
    def load():
        import database as db
        names = _cached("sid_to_name", _NAMES_TTL, lambda: {
            p["s_id"]: p["name"] for p in db.get_mln_players_for_lookup() if p.get("s_id") and p.get("name")})
        team_full = {tid: t["full_team"] for tid, t in teams()["by_id"].items() if t.get("full_team")}
        rows, _ = build_mln_rows(_raw_plays(), {}, names, team_full, require_game=False)
        by_code = {g["game_code"]: g for g in games()}
        for r in rows:
            g = by_code.get(str(r["game_code"])) or {}
            r["games"] = {"season": g.get("season") or r.get("season"), "session_number": g.get("session_number"),
                          "home_team": g.get("home_team"), "away_team": g.get("away_team"),
                          "game_code": r["game_code"]}
        return rows
    return _cached("history_rows", _PLAYS_TTL, load)

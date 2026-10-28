"""Standalone sync: RLN Games, RLN Plays, MLN Games, MLN Plays -> Supabase.

Credentials via environment variables (GitHub Actions) or .streamlit/secrets.toml (local):
    SUPABASE_URL  /  supabase_url
    SUPABASE_KEY  /  supabase_key

Usage:
    python sync_plays.py
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import sys
import time
import database as db
import utils

_RLN_SHEET_ID = "1lcgT6np-4O5x83b2JZXjv8REfNDYXE7GMYMZeu5znRY"
_MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"

# Update these each season - export sheets have no Season column
_CURRENT_RLN_SEASON = 13
_CURRENT_MLN_SEASON = 13

_GAMES_TABLE_COLS = {
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


# ── change tracking ───────────────────────────────────────────────────────────
# The sync runs every 5 minutes and almost nothing changes between runs, so it keeps a small state file (restored /
# saved by the workflow's Actions cache) holding a hash of every row it last wrote plus short-lived copies of the
# Supabase lookup tables. A run then (1) skips a plays tab entirely when the sheet is byte-identical to last time,
# (2) upserts only the rows whose hash changed, and (3) reads the lookups from the file instead of Supabase.
# No state (first run, evicted cache, SYNC_FULL=1, or older than FULL_REFRESH_S) means a full upsert - the same writes
# as before, with returning="minimal" so Supabase sends nothing back.
_STATE_VERSION = 1
_FULL_REFRESH_S = 24 * 3600      # re-upsert everything this often so any drift from the sheets is healed
_GAMES_TTL_S = 3600              # game_code -> id; also refreshed immediately when a play's game is missing
_NAMES_TTL_S = 20 * 60           # id -> name maps: a renamed / newly added player shows up within this window

STATE: "_State | None" = None


def _hash(obj) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


class _State:
    def __init__(self):
        self.path = pathlib.Path(os.environ.get("SYNC_STATE_PATH", ".sync_state/state.json"))
        self.data: dict = {}
        self._loaded = ""
        try:
            self._loaded = self.path.read_text(encoding="utf-8")
            self.data = json.loads(self._loaded)
        except Exception:
            self.data = {}
        if self.data.get("version") != _STATE_VERSION:
            self.data = {}
        forced = os.environ.get("SYNC_FULL", "").strip().lower() in ("1", "true", "yes")
        stale = time.time() - self.data.get("full_at", 0) > _FULL_REFRESH_S
        self.full = forced or stale or not self.data
        if self.full:
            self.data = {"version": _STATE_VERSION, "full_at": time.time(), "lookups": {}, "sections": {}}
        print(f"[state] {'full upsert' if self.full else 'incremental'} run "
              f"({'forced' if forced else 'no/stale state' if stale or not self._loaded else 'state loaded'})")

    # lookups -----------------------------------------------------------------
    def _fresh(self, name: str, ttl: float) -> bool:
        ent = self.data["lookups"].get(name)
        return bool(ent) and time.time() - ent["at"] < ttl

    def fresh(self, *names_ttls: tuple[str, float]) -> bool:
        return all(self._fresh(n, t) for n, t in names_ttls)

    def lookup(self, name: str, fetch, ttl: float, refresh: bool = False) -> tuple[list, bool]:
        """(value, fetched_now). value is JSON-friendly; refetched from Supabase only when stale or refresh=True."""
        if not refresh and self._fresh(name, ttl):
            return self.data["lookups"][name]["v"], False
        value = fetch()
        self.data["lookups"][name] = {"at": time.time(), "v": value}
        return value, True

    # per-section row hashes --------------------------------------------------
    def section(self, name: str) -> dict:
        return self.data["sections"].setdefault(name, {})

    def save(self) -> bool:
        """Write the state file; returns True when it differs from what was loaded (the workflow re-caches only then)."""
        text = json.dumps(self.data, sort_keys=True, default=str)
        changed = text != self._loaded
        if changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        return changed


def _upsert_changed(section: str, rows: list[dict], key: str, upsert) -> tuple[int, list[str]]:
    """Upsert only rows whose hash differs from the last successful write of this section; record the new hashes."""
    sec = STATE.section(section)
    old = {} if STATE.full else sec.get("rows", {})
    new = {str(r[key]): _hash(r) for r in rows}
    changed = [r for r in rows if old.get(str(r[key])) != new[str(r[key])]]
    try:
        n = upsert(changed) if changed else 0
    except Exception as e:
        return 0, [str(e)]
    sec["rows"] = new
    print(f"  {section}: {n} of {len(rows)} rows changed")
    return n, []


def _plays_unchanged(section: str, plays: list[dict], *lookups: tuple[str, float]) -> bool:
    """True when the sheet's plays are identical to the last fully-synced run and the lookups are still fresh."""
    if STATE.full or STATE.section(section).get("raw") != _hash(plays):
        return False
    return STATE.fresh(*lookups)


def _games_lookup(codes: set) -> list[list]:
    """[[id, game_code, league], ...]; re-read from Supabase if a play's game isn't in the cached copy yet."""
    def fetch():
        return [[g["id"], str(g["game_code"]).strip(), g.get("league")]
                for g in db.get_game_lookup() if g.get("game_code") and g.get("id")]
    rows, fetched = STATE.lookup("games", fetch, _GAMES_TTL_S)
    if not fetched and {str(c).strip() for c in codes} - {r[1] for r in rows}:
        rows, _ = STATE.lookup("games", fetch, _GAMES_TTL_S, refresh=True)
    return rows


# ── RLN ───────────────────────────────────────────────────────────────────────

def sync_rln_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the RLN Games tab."]
    for g in games:
        g["league"] = "RLN"
        g["game_type"] = "live"
    return _upsert_changed("rln_games", games, "game_code", db.bulk_upsert_games)


def sync_rln_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_plays_from_sheet(sheet_id)
    if not plays:
        return 0, ["No plays found in the RLN Plays (Raw) tab."]

    raw = _hash(plays)
    if _plays_unchanged("rln_plays", plays, ("games", _GAMES_TTL_S), ("rln_players", _NAMES_TTL_S)):
        print("  rln_plays: sheet unchanged, skipped")
        return 0, []

    game_code_to_id = {code: gid for gid, code, _ in _games_lookup({p["game_code"] for p in plays})}
    names, _ = STATE.lookup("rln_players", lambda: [[p["player_id"], p["name"]] for p in db.get_player_name_lookup()
                                                    if p.get("player_id") and p.get("name")], _NAMES_TTL_S)
    player_id_to_name = dict(names)

    plays_sorted  = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set]   = {}
    seen_pitcher: dict[str, set]   = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc         = play["game_code"]
        game_db_id = game_code_to_id.get(gc)
        if not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not in DB - sync RLN Games first.")
            continue

        pitcher_name = player_id_to_name.get(play.get("pitcher_id"), "")
        batter_name  = player_id_to_name.get(play.get("batter_id"),  "")
        catcher_name = player_id_to_name.get(play.get("catcher_id"))
        runner_name  = player_id_to_name.get(play.get("runner_id"))

        away_full = utils.TEAM_ABBREV.get(play.get("away") or "", play.get("away") or "")
        home_full = utils.TEAM_ABBREV.get(play.get("home") or "", play.get("home") or "")
        off_team, def_team = (away_full, home_full) if play["half"] == "top" else (home_full, away_full)

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        outs = outs_tracker.setdefault(inn_key, 0)
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn  = fp_inn_key    not in seen_inn.setdefault(gc, set())
        is_fp_app  = pitcher_name  not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items() if k != "game_code"},
            "game_code":    gc,
            "league":       "RLN",
            "game_type":    "live",
            "season":       _CURRENT_RLN_SEASON,
            "game_id":      game_db_id,
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
        return 0, errors
    n, errs = _upsert_changed("rln_plays", rows, "play_num", db.bulk_upsert_plays)
    if not errors and not errs:
        STATE.section("rln_plays")["raw"] = raw     # fully synced: next identical sheet read can be skipped
    return n, errors + errs


# ── MLN ───────────────────────────────────────────────────────────────────────

def sync_mln_games(sheet_id: str) -> tuple[int, list[str]]:
    games = utils.read_mln_games_from_sheet(sheet_id)
    if not games:
        return 0, ["No games found in the MLN Games tab."]
    abbrev_to_full = utils.read_mln_team_abbrev_lookup(sheet_id)
    for g in games:
        g["away_team"] = abbrev_to_full.get(g["away_team"], g["away_team"])
        g["home_team"] = abbrev_to_full.get(g["home_team"], g["home_team"])
        a_scr, h_scr = g.get("away_score"), g.get("home_score")
        if a_scr is not None and h_scr is not None:
            if a_scr > h_scr:
                g["win_team"], g["loss_team"] = g["away_team"], g["home_team"]
            elif h_scr > a_scr:
                g["win_team"], g["loss_team"] = g["home_team"], g["away_team"]
        g["archive_sheet_id"] = sheet_id
        g["game_type"] = "live"
    games = [{k: v for k, v in g.items() if k in _GAMES_TABLE_COLS} for g in games]
    return _upsert_changed("mln_games", games, "game_code", db.bulk_upsert_games)


def build_mln_rows(plays: list[dict], game_code_to_id: dict, sid_to_name: dict, team_id_to_full: dict,
                   require_game: bool = True) -> tuple[list[dict], list[str]]:
    """The plays-table row for every MLN sheet play (names, teams, outs before the play, diff and first-pitch flags
    resolved from the lookups). Shared by the sync and by PioBall's live pitcher history, which passes
    require_game=False so a play whose game has no Supabase id yet still builds (game_id stays None)."""
    plays_sorted  = sorted(plays, key=lambda p: p["play_num"])
    seen_inn:     dict[str, set]   = {}
    seen_pitcher: dict[str, set]   = {}
    outs_tracker: dict[tuple, int] = {}
    errors: list[str] = []
    rows:   list[dict] = []

    for play in plays_sorted:
        gc         = play["game_code"]
        season     = play.get("season") or _CURRENT_MLN_SEASON or (int(gc[:2]) if gc and len(gc) >= 2 and gc[:2].isdigit() else None)
        game_db_id = game_code_to_id.get(gc)
        if require_game and not game_db_id:
            errors.append(f"Play {play['play_num']}: game {gc} not in DB - sync MLN Games first.")
            continue

        def _sid(pid):
            return f"{season}_{pid}" if season and pid else None

        pitcher_name = sid_to_name.get(_sid(play.get("pitcher_id")), "") or ""
        batter_name  = sid_to_name.get(_sid(play.get("batter_id")),  "") or ""
        catcher_name = sid_to_name.get(_sid(play.get("catcher_id")))
        runner_name  = sid_to_name.get(_sid(play.get("runner_id")))

        away_full = team_id_to_full.get(play.get("away") or "", play.get("away") or "")
        home_full = team_id_to_full.get(play.get("home") or "", play.get("home") or "")
        off_team, def_team = (away_full, home_full) if play["half"] == "top" else (home_full, away_full)

        pitch, swing = play.get("pitch"), play.get("swing")
        diff = utils.circular_diff(int(pitch), int(swing)) if pitch is not None and swing is not None else None

        inn_key = (gc, play["inning"], play["half"])
        outs = outs_tracker.setdefault(inn_key, 0)
        outs_tracker[inn_key] = min(3, outs + utils.outs_added(play.get("result") or ""))

        fp_inn_key = (play["inning"], play["half"])
        is_fp_inn  = fp_inn_key    not in seen_inn.setdefault(gc, set())
        is_fp_app  = pitcher_name  not in seen_pitcher.setdefault(gc, set())
        seen_inn[gc].add(fp_inn_key)
        if pitcher_name:
            seen_pitcher[gc].add(pitcher_name)

        rows.append({
            **{k: v for k, v in play.items()
               if k not in ("game_code", "away", "home") and not k.startswith("_")},
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
    return rows, errors


def sync_mln_plays(sheet_id: str) -> tuple[int, list[str]]:
    plays = utils.read_mln_plays_from_sheet(sheet_id, tab="Plays (Raw)")
    if not plays:
        return 0, ["No plays found in the MLN Plays (Raw) tab."]

    raw = _hash(plays)
    if _plays_unchanged("mln_plays", plays, ("games", _GAMES_TTL_S), ("mln_teams", _NAMES_TTL_S),
                        ("mln_players", _NAMES_TTL_S)):
        print("  mln_plays: sheet unchanged, skipped")
        return 0, []

    all_games = _games_lookup({p["game_code"] for p in plays})
    game_code_to_id = {code: gid for gid, code, _ in all_games}
    mln_game_codes  = sorted(code for _, code, league in all_games if league == "MLN")
    play_game_codes = sorted({p["game_code"] for p in plays})
    if not mln_game_codes:
        return 0, [
            f"No MLN games in DB - sync MLN Games first. "
            f"Plays reference game codes like: {play_game_codes[:5]}"
        ]
    missing = sorted(set(play_game_codes) - set(mln_game_codes))
    diag = ([
        f"DB has {len(mln_game_codes)} MLN game(s) e.g. {mln_game_codes[:5]}",
        f"Plays reference {len(play_game_codes)} game(s) e.g. {play_game_codes[:5]}",
        f"{len(missing)} game code(s) missing from DB e.g. {missing[:5]}",
    ] if missing else [])

    teams, _ = STATE.lookup("mln_teams", lambda: [[t["team_id"], t["full_team"]] for t in db.get_mln_teams_for_lookup()
                                                  if t.get("team_id") and t.get("full_team")], _NAMES_TTL_S)
    plyrs, _ = STATE.lookup("mln_players", lambda: [[p["s_id"], p["name"]] for p in db.get_mln_players_for_lookup()
                                                    if p.get("s_id") and p.get("name")], _NAMES_TTL_S)
    team_id_to_full = dict(teams)
    sid_to_name     = dict(plyrs)

    rows, errors = build_mln_rows(plays, game_code_to_id, sid_to_name, team_id_to_full)

    if not rows:
        return 0, diag + errors
    rows = list({(r["play_num"], r.get("league", "MLN")): r for r in rows}.values())
    n, errs = _upsert_changed("mln_plays", rows, "play_num", db.bulk_upsert_mln_plays)
    if not errors and not errs:
        STATE.section("mln_plays")["raw"] = raw     # fully synced: next identical sheet read can be skipped
    return n, diag + errors + errs


# ── main ──────────────────────────────────────────────────────────────────────

def _run(label: str, fn, *args) -> bool:
    """Run a sync step, print results, return True if a hard error occurred."""
    print(f"\n=== {label} ===")
    try:
        n, msgs = fn(*args)
        print(f"  Upserted: {n}")
        for m in msgs:
            print(f"  {'ERROR' if 'not in DB' not in m and 'missing' not in m else 'WARN'}: {m}")
        return n == 0 and bool(msgs) and any("not in DB" not in m and "missing" not in m for m in msgs)
    except Exception as e:
        print(f"  FATAL: {e}", file=sys.stderr)
        return True


def _check_credentials() -> bool:
    """Print a credential diagnostic. Returns False if credentials look bad."""
    import os
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    url_ok  = url.startswith("https://") and "supabase" in url and '"' not in url
    key_ok  = len(key) > 20 and '"' not in key
    print(f"[creds] SUPABASE_URL present={bool(url)} starts_https={url.startswith('https://')} "
          f"has_supabase={'supabase' in url} has_quotes={chr(34) in url}")
    print(f"[creds] SUPABASE_KEY present={bool(key)} length={len(key)} has_quotes={chr(34) in key}")
    if not url_ok or not key_ok:
        print("  FATAL: Credential check failed - check GitHub secret values (no quotes, correct format).",
              file=sys.stderr)
        return False
    return True


def main() -> None:
    global STATE
    if not _check_credentials():
        sys.exit(1)
    STATE = _State()
    hard_error = False
    hard_error |= _run("RLN Games",  sync_rln_games,  _RLN_SHEET_ID)
    hard_error |= _run("RLN Plays",  sync_rln_plays,  _RLN_SHEET_ID)
    hard_error |= _run("MLN Games",  sync_mln_games,  _MLN_SHEET_ID)
    hard_error |= _run("MLN Plays",  sync_mln_plays,  _MLN_SHEET_ID)
    changed = STATE.save()
    s = db.stats()
    print(f"[egress] {os.environ.get('SB_CLIENT_TAG', 'sync')}: {s['read_calls']} paged reads / {s['read_rows']} rows "
          f"(~{s['read_bytes'] / 1e6:.2f} MB JSON), {s['write_calls']} write calls / {s['write_rows']} rows")
    if os.environ.get("GITHUB_OUTPUT"):      # tells the workflow whether to re-save its Actions cache
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"state_changed={'true' if changed else 'false'}\n")
    print()
    if hard_error:
        print("Sync completed with errors.", file=sys.stderr)
        sys.exit(1)
    print("Sync completed successfully.")


if __name__ == "__main__":
    main()

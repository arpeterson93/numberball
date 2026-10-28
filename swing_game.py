"""Swing guessing game (PioBall rebuild) - game logic with no Discord imports.

Flow: a user guesses a swing for the next pitch of the live Portland Pioneers
game (/swing guess). The first guess for a pitch opens a swing_pitches row and
snapshots that PA's hitting ranges off the live sheet. When the real pitch
shows up in the synced `plays` table, grade_ready() turns every guess into a
(diff, result) pair against the snapshotted ranges, and stats() rolls graded
guesses up into AVG/OBP/SLG/OPS.

Needs migrate_swing_game.sql applied to Supabase.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

import database as db
import live_sheet
import scouting_data
import utils

TEAM = os.environ.get("SWING_TEAM", "Portland Pioneers")
TEAM_ABBREV = os.environ.get("SWING_TEAM_ABBREV", "POR")
_LIVE_STALE_HOURS = 48

ACTUAL_ID = "actual"
MODEL_ID = "model"
MODEL_NAME = "Model"
ACTUAL_NAME = "Actual"

_HITS = {"1B": 1, "1BWH": 1, "1BWH2": 1, "IF1B": 1, "2B": 2, "2BWH": 2, "3B": 3, "HR": 4}
_WALKS = {"BB", "IBB"}
_SAC_FLY = {"SacF", "DSacF"}


def _t(name: str):
    return db._client().table(name)


# ------------------------------------------------------------- player names

_names: dict[str, str] = {}
_names_loaded = 0.0
_NAMES_TTL = 600


def _discord_names() -> dict[str, str]:
    """Discord user id -> player name from the players table (its discord_id
    column, which the sheet exports sometimes wrap in junk like "123>"). Our
    team's newest season wins, then any other team's newest season."""
    global _names, _names_loaded
    if _names and time.time() - _names_loaded < _NAMES_TTL:
        return _names
    rows = db._fetch_all(_t("players").select("name,team,season,discord_id").not_.is_("discord_id", "null"))
    best: dict[str, tuple] = {}
    for r in rows:
        m = re.search(r"\d{17,20}", str(r.get("discord_id") or ""))
        if not m or not r.get("name"):
            continue
        key = (r.get("team") == TEAM_ABBREV, r.get("season") or 0)
        if m.group() not in best or key > best[m.group()][0]:
            best[m.group()] = (key, r["name"])
    _names = {k: v[1] for k, v in best.items()}
    _names_loaded = time.time()
    return _names


_roster: dict[str, str] = {}
_roster_loaded = 0.0


def roster() -> dict[str, str]:
    """Current Pioneers roster that has a Discord id on file: player name -> Discord
    user id (what swing_guesses stores). Newest season our team appears in."""
    global _roster, _roster_loaded
    if _roster and time.time() - _roster_loaded < _NAMES_TTL:
        return _roster
    rows = db._fetch_all(_t("players").select("name,season,discord_id").eq("team", TEAM_ABBREV)
                         .not_.is_("discord_id", "null"))
    season = max((r.get("season") or 0 for r in rows), default=0)
    out: dict[str, str] = {}
    for r in rows:
        m = re.search(r"\d{17,20}", str(r.get("discord_id") or ""))
        if m and r.get("name") and (r.get("season") or 0) == season:
            out[r["name"]] = m.group()
    _roster, _roster_loaded = out, time.time()
    return out


_our_names: set[str] = set()
_our_names_loaded = 0.0


def is_our_batter(name: str | None) -> bool | None:
    """Is this batter a Pioneer (newest season we appear in)? False for another team's
    batter - the sheet already shows the opponent's next PA before our flag flips.
    None when unknown (no name, or the players lookup failed): don't block on it."""
    global _our_names, _our_names_loaded
    if not name:
        return None
    try:
        if not _our_names or time.time() - _our_names_loaded > _NAMES_TTL:
            rows = db._fetch_all(_t("players").select("name,season").eq("team", TEAM_ABBREV))
            season = max((r.get("season") or 0 for r in rows), default=0)
            _our_names = {str(r["name"]).strip().lower() for r in rows
                          if r.get("name") and (r.get("season") or 0) == season}
            _our_names_loaded = time.time()
    except Exception:
        return None
    return str(name).strip().lower() in _our_names if _our_names else None


def display_name(user_id: str, fallback: str | None) -> str:
    """The Pioneers player this Discord user is, else their Discord name."""
    if str(user_id) == ACTUAL_ID:
        return ACTUAL_NAME  # also relabels rows saved under an older name
    if str(user_id) == MODEL_ID:
        return MODEL_NAME
    try:
        return _discord_names().get(str(user_id)) or fallback or str(user_id)
    except Exception:  # players lookup is a nicety - never block a guess on it
        return fallback or str(user_id)


# ------------------------------------------------------------- live game

def _parse_ts(s: str | None):
    try:
        return datetime.strptime(str(s), "%m/%d/%Y %H:%M")
    except (TypeError, ValueError):
        return None


MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"  # same sheet MLN Gameday builds from
_FINALS_TTL = 60
_finals: set[str] | None = None
_finals_loaded = 0.0


def _sheet_finals() -> set[str] | None:
    """Game codes the sheet's Games tab marks final - the same read and definition
    (utils.game_is_final on the sheet's own Win/scores) MLN Gameday uses. The synced
    games table can't say: the sync fills win_team from the running score. Cached briefly;
    None if the sheet can't be read."""
    global _finals, _finals_loaded
    if _finals is not None and time.time() - _finals_loaded < _FINALS_TTL:
        return _finals
    try:
        _finals = {str(g["game_code"]) for g in live_sheet.games() if utils.game_is_final(g)}
        _finals_loaded = time.time()
        return _finals
    except Exception as e:
        print(f"games sheet read failed, using the synced games table: {e!r}")
        return None


def _game_over(g: dict, finals: set[str] | None) -> bool:
    if finals is not None:
        return str(g["game_code"]) in finals
    # Sheet unreachable: synced win_team is the running leader, so also require the last
    # play to be in or past the last regulation inning (last_inning looks like 'B2').
    if not g.get("win_team"):
        return False
    m = re.search(r"\d+", str(g.get("last_inning") or ""))
    return bool(m) and int(m.group()) >= utils.game_innings(g.get("league") or "MLN")


def find_live_game(override: str | None = None) -> dict | None:
    """The in-progress Pioneers game: unfinished, has a sheet on file, in the
    newest season, and (if it has plays) not stale. override pins a game_code."""
    if override:
        g = db.get_game_by_code(int(override))
        return g
    finals = _sheet_finals()
    games = _candidate_games(finals)
    if not games:
        return None
    season = max(g.get("season") or 0 for g in games)
    best, best_key = None, None
    for g in games:
        if (g.get("season") or 0) != season:
            continue
        last = _last_play(g["game_code"])
        if last:
            ts = _parse_ts(last.get("timestamp"))
            if ts and datetime.now() - ts > timedelta(hours=_LIVE_STALE_HOURS):
                continue
        # games with plays beat games not started; later play_num beats earlier
        key = (1 if last else 0, last["play_num"] if last else 0, -int(g["game_code"]))
        if best_key is None or key > best_key:
            best, best_key = g, key
    return best


def _last_play_db(game_code) -> dict | None:
    rows = (_t("plays").select("play_num,timestamp").eq("game_code", str(game_code))
            .order("play_num", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


# The live game, its plays and its latest play come straight off the MLN sheet (live_sheet): it is the raw source the plays
# sync copies into Supabase, so it is current before the sync runs and reading it costs no Supabase egress. Every helper
# falls back to the Supabase tables if the sheet can't be read.

_SHEET_URL_TTL = 120
_sheet_urls: dict = {"at": 0.0, "codes": None, "urls": {}}


def _sheet_url_flags(codes: list[str]) -> dict[str, str]:
    """{game_code: scoresheet link} from Supabase for just the candidate games (a few rows, cached). The link is set by
    hand on the Games page when a game goes live, so having one is what marks a game as ready for the bot."""
    cache = _sheet_urls
    if cache["codes"] == codes and time.time() - cache["at"] < _SHEET_URL_TTL:
        return cache["urls"]
    urls = db.get_game_sheet_urls(codes)
    cache.update(at=time.time(), codes=list(codes), urls=urls)
    return urls


def _candidate_games(finals: set[str] | None) -> list[dict]:
    """Our unfinished games that have a scoresheet link on file."""
    try:
        cands = [g for g in live_sheet.games()
                 if TEAM in (g.get("home_team"), g.get("away_team")) and not _game_over(g, finals)]
        urls = _sheet_url_flags([g["game_code"] for g in cands])
        return [{**g, "sheet_url": urls[g["game_code"]]} for g in cands if g["game_code"] in urls]
    except Exception as e:
        print(f"games sheet read failed, using the synced games table: {e!r}")
        return [g for g in db.get_games()
                if TEAM in (g.get("home_team"), g.get("away_team"))
                and not _game_over(g, finals) and g.get("sheet_url")]


def _last_play(game_code) -> dict | None:
    try:
        return live_sheet.last_play(game_code)
    except Exception as e:
        print(f"plays sheet read failed, using the synced plays table: {e!r}")
        return _last_play_db(game_code)


def _plays_after(code: str, after: int, limit: int, cols: str) -> list[dict]:
    """This game's plays with play_num > after, oldest first (cols is only used by the Supabase fallback)."""
    try:
        return [p for p in live_sheet.plays(code) if int(p["play_num"]) > int(after)][:limit]
    except Exception as e:
        print(f"plays sheet read failed, using the synced plays table: {e!r}")
        return (_t("plays").select(cols).eq("game_code", str(code)).gt("play_num", after)
                .order("play_num").limit(limit).execute().data)


def recent_plays(code: str, n: int) -> list[dict]:
    """The game's newest n plays, newest first."""
    try:
        return live_sheet.plays(code)[::-1][:n]
    except Exception as e:
        print(f"plays sheet read failed, using the synced plays table: {e!r}")
        return (_t("plays").select("play_num,pitch,swing,result,timestamp").eq("game_code", str(code))
                .order("play_num", desc=True).limit(n).execute().data)


def _game_row(code) -> dict:
    try:
        g = live_sheet.game(code)
        if g:
            return g
    except Exception as e:
        print(f"games sheet read failed, using the synced games table: {e!r}")
    return db.get_game_by_code(int(code)) or {}


def current_season() -> int | None:
    try:
        return live_sheet.current_season()
    except Exception:
        return max((g.get("season") or 0 for g in db.get_games()), default=None)


# ------------------------------------------------------------- open pitch

def _latest_pitch_row(code: str) -> dict | None:
    rows = (_t("swing_pitches").select("*").eq("game_code", code)
            .order("seq", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def get_or_open_pitch(game: dict, channel_id: str | None, our_at_bat: bool = True) -> dict:
    """The open (ungraded) pitch row for this game's next pitch, creating it -
    and snapshotting the PA's ranges off the live sheet - if this is the first
    guess for it. When it isn't our at-bat yet (our_at_bat=False) the row opens
    with no snapshot - the sheet still shows the opponent's PA - and
    refresh_snapshot() fills it in once our "Batter Up!" arrives. Pitches are ordered by seq, so the next pitch opens the
    moment the last one is graded, without waiting for the plays sync."""
    code = str(game["game_code"])
    latest = _latest_pitch_row(code)
    if latest and not latest.get("graded_at"):
        return latest
    seq = (latest["seq"] + 1) if latest else 1

    # The real pitch must land after every play already synced and after any
    # pitch we've already graded (feed-graded pitches may not be synced yet).
    last = _last_play(code)
    after = max(int(last["play_num"]) if last else 0, _max_claimed_play_num(code))

    row = {"game_code": code, "season": game.get("season"), "seq": seq, "after_play_num": after,
           "channel_id": str(channel_id) if channel_id else None}
    if our_at_bat:
        snap = _snapshot(code)
        if is_our_batter(snap.get("batter")) is not False:  # else the sheet is on the opponent's PA
            row.update(snap)
    try:
        try:
            return _t("swing_pitches").insert(row).execute().data[0]
        except Exception:
            if not any(c in row for c in _SITUATION_COLS):
                raise
            return _t("swing_pitches").insert(_without_situation(row)).execute().data[0]
    except Exception:  # lost a race with another first guesser
        return (_t("swing_pitches").select("*").eq("game_code", code)
                .eq("seq", seq).execute().data[0])


def _snapshot(code: str) -> dict:
    """Batter/pitcher/situation and the PA's hitting ranges, off the live sheet."""
    state = scouting_data.resolve_game_state(int(code)) or {}
    swing_type = state.get("swing_type") or "Normal Swing"
    ranges = (state.get("bunt_ranges") if swing_type == "Bunt" else None) \
        or state.get("result_ranges") or utils.RESULT_RANGES
    snap = {"batter": state.get("batter"), "pitcher": state.get("pitcher"),
            "inning": state.get("inning"), "half": state.get("half"),
            "outs": state.get("outs"), "obc": state.get("obc"),
            "swing_type": swing_type, "ranges": [list(r) for r in ranges]}
    game = _game_row(code)
    if game.get("away_team"):
        ours, theirs = (("away_score", "home_score") if game["away_team"] == TEAM
                        else ("home_score", "away_score"))
        snap["lead"] = int(state.get(ours) or 0) - int(state.get(theirs) or 0)  # our lead, for WPA
    snap["league"] = state.get("league")
    return snap


_SITUATION_COLS = ("lead", "league")  # need migrate_swing_game.sql's ALTERs; dropped if not applied yet


def _without_situation(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SITUATION_COLS}


def refresh_snapshot(game_code: str) -> None:
    """Our "Batter Up!" arrived: re-read the live sheet and overwrite the open
    pitch's snapshot (it may have been opened early, during the opponent's at-bat)."""
    latest = _latest_pitch_row(str(game_code))
    if not latest or latest.get("graded_at"):
        return
    clear = getattr(scouting_data.resolve_game_state, "clear", None)
    if clear:
        clear()  # the cached state is up to 30s old - could still be the opponent's PA
    snap = _snapshot(str(game_code))
    if is_our_batter(snap.get("batter")) is False:
        return  # the sheet still shows the opponent's PA - keep what we have, retry on the next call
    if any(latest.get(k) and snap.get(k) and latest[k] != snap[k] for k in ("batter", "pitcher")):
        # new matchup: a model swing frozen for the old one no longer applies
        snap.update({"model_swing": None, "model_metric": None, "model_at": None})
    try:
        _t("swing_pitches").update(snap).eq("id", latest["id"]).execute()
    except Exception:
        _t("swing_pitches").update(_without_situation(snap)).eq("id", latest["id"]).execute()


def open_for_batter_up(game: dict) -> None:
    """Our "Batter Up!": make sure this plate appearance has a pitch row even if nobody
    ever guesses on it, so every pitch against us is recorded, graded and run through
    the model, then snapshot it off the freshly re-read sheet."""
    get_or_open_pitch(game, None, our_at_bat=False)  # the open row if there is one, else a new empty one
    refresh_snapshot(game["game_code"])


def set_channel(pitch_id: int, channel_id) -> None:
    """Remember where the first guess happened, so graded results post there."""
    _t("swing_pitches").update({"channel_id": str(channel_id)}).eq("id", pitch_id).execute()


def close_open_pitch(game_code: str) -> bool:
    """Lock guessing for the game's open pitch (the real pitch has been posted
    in the results thread, but the plays sync may not have caught up yet).
    Returns True if there was an open pitch to lock."""
    rows = (_t("swing_pitches").select("id").eq("game_code", str(game_code))
            .is_("graded_at", "null").is_("closed_at", "null").execute().data)
    for r in rows:
        _t("swing_pitches").update({"closed_at": datetime.utcnow().isoformat()}).eq("id", r["id"]).execute()
    return bool(rows)


def is_closed(pitch_row: dict) -> bool:
    """Closed by close_open_pitch (no longer called from the feed), graded, or the real
    pitch has already synced into plays."""
    if pitch_row.get("closed_at") or pitch_row.get("graded_at"):
        return True
    return _actual_pitch(pitch_row) is not None


def submit_guess(pitch_row: dict, user_id: str, user_name: str, guess: int) -> int | None:
    """Record a guess. One per user per pitch, but a new guess replaces the old one while
    the pitch is open. Returns the guess it replaced, or None if there wasn't one."""
    existing = (_t("swing_guesses").select("id,guess").eq("pitch_id", pitch_row["id"])
                .eq("user_id", str(user_id)).execute().data)
    if existing:
        _t("swing_guesses").update({"guess": int(guess), "user_name": display_name(user_id, user_name)})             .eq("id", existing[0]["id"]).execute()
        return int(existing[0]["guess"])
    try:
        _t("swing_guesses").insert(
            {"pitch_id": pitch_row["id"], "user_id": str(user_id),
             "user_name": display_name(user_id, user_name), "guess": int(guess)}).execute()
    except Exception:  # double-submit race: the unique (pitch_id, user_id) key caught it
        _t("swing_guesses").update({"guess": int(guess)}).eq("pitch_id", pitch_row["id"])             .eq("user_id", str(user_id)).execute()
    return None


def clear_guesses(pitch_id: int, user_id: str | None = None) -> int:
    """Delete the open pitch's guesses (one user's, or everyone's) so they can be re-submitted,
    e.g. after the opposing pitcher changed. Never touches the Actual/Model benchmark rows.
    Returns how many were removed."""
    q = (_t("swing_guesses").select("id").eq("pitch_id", pitch_id)
         .neq("user_id", ACTUAL_ID).neq("user_id", MODEL_ID))
    if user_id is not None:
        q = q.eq("user_id", str(user_id))
    ids = [r["id"] for r in q.execute().data]
    for i in ids:
        _t("swing_guesses").delete().eq("id", i).execute()
    return len(ids)


def count_guesses(pitch_id: int) -> int:
    return len(_t("swing_guesses").select("id").eq("pitch_id", pitch_id).execute().data)


# ------------------------------------------------------------- grading

def _max_claimed_play_num(code: str) -> int:
    rows = (_t("swing_pitches").select("play_num").eq("game_code", code)
            .not_.is_("play_num", "null").order("play_num", desc=True).limit(1).execute().data)
    return int(rows[0]["play_num"]) if rows else 0


def _reconcile(code: str) -> set[int]:
    """Match pitches graded straight from the results feed (no play_num yet) to
    their synced plays row by pitch+swing, so the sync never double-counts
    them. Returns the play_nums already claimed by graded pitches."""
    rows = (_t("swing_pitches").select("id,play_num,pitch,swing,after_play_num").eq("game_code", code)
            .not_.is_("graded_at", "null").order("seq").execute().data)
    claimed = {int(r["play_num"]) for r in rows if r.get("play_num") is not None}
    pending = [r for r in rows if r.get("play_num") is None and r.get("pitch") is not None]
    if not pending:
        return claimed
    plays = _plays_after(code, min(int(r["after_play_num"]) for r in pending), 200, "play_num,play_type,pitch,swing")
    for r in pending:
        for pl in plays:
            n = int(pl["play_num"])
            if (n in claimed or n <= int(r["after_play_num"])
                    or str(pl.get("play_type") or "").lower() == "steal"):
                continue
            if pl.get("pitch") == r["pitch"] and pl.get("swing") == r.get("swing"):
                _t("swing_pitches").update({"play_num": n}).eq("id", r["id"]).execute()
                claimed.add(n)
                break
    return claimed


def _actual_pitch(pitch_row: dict) -> dict | None:
    """First real pitch (swing, not a steal, not already claimed by an earlier
    graded pitch) in plays after the open pitch."""
    claimed = _reconcile(pitch_row["game_code"])
    rows = _plays_after(pitch_row["game_code"], pitch_row["after_play_num"], 20,
                        "play_num,play_type,result,pitch,swing,off_team")
    for r in rows:
        if str(r.get("play_type") or "").lower() == "steal" or int(r["play_num"]) in claimed:
            continue
        if r.get("off_team") and r["off_team"] != TEAM:
            continue  # the opponent's plate appearance - not ours to grade
        if r.get("pitch") is not None:
            return r
    return None


def _sync_hitter_guess(p: dict, swing: int | None) -> None:
    """The hitter's own PioBall swing always ends up being the swing they really took:
    logged if they never submitted one, overwritten if it differed. Skipped when the
    batter isn't on the roster with a Discord id (or the real swing is unknown)."""
    if swing is None or not 1 <= int(swing) <= 1000 or not p.get("batter"):
        return
    want = str(p["batter"]).strip().lower()
    uid = next((u for name, u in roster().items() if name.strip().lower() == want), None)
    if not uid:
        return
    existing = (_t("swing_guesses").select("id,guess").eq("pitch_id", p["id"])
                .eq("user_id", uid).execute().data)
    if existing:
        if int(existing[0]["guess"]) != int(swing):
            _t("swing_guesses").update({"guess": int(swing)}).eq("id", existing[0]["id"]).execute()
        return
    _t("swing_guesses").upsert(
        {"pitch_id": p["id"], "user_id": uid, "user_name": display_name(uid, p["batter"]),
         "guess": int(swing)}, on_conflict="pitch_id,user_id").execute()


def _grade_row(p: dict, pitch: int, swing: int | None, result: str | None,
               play_num: int | None) -> dict | None:
    """Grade every guess on pitch row p against the real pitch, record the
    manager's actual swing as a benchmark row, and mark the pitch graded.
    Returns the message payload, or None if nobody guessed (the pitch, Actual and Model are still recorded)."""
    try:
        _sync_hitter_guess(p, swing)
    except Exception as e:  # a nicety - never block grading on it
        print(f"hitter guess sync failed for pitch {p.get('id')}: {e!r}")
    ranges = [tuple(r) for r in (p.get("ranges") or utils.RESULT_RANGES)]
    guesses = [g for g in _t("swing_guesses").select("*").eq("pitch_id", p["id"]).execute().data
               if g["user_id"] not in (ACTUAL_ID, MODEL_ID)]
    graded = []
    for g in guesses:
        d = utils.circular_diff(pitch, int(g["guess"]))
        res = utils._diff_to_result(d, ranges)
        _t("swing_guesses").update({"diff": d, "result": res}).eq("id", g["id"]).execute()
        graded.append({"user_name": display_name(g["user_id"], g["user_name"]), "guess": g["guess"], "diff": d, "result": res})
    _t("swing_pitches").update({
        "graded_at": datetime.utcnow().isoformat(), "pitch": pitch, "swing": swing,
        "play_num": play_num, "actual_result": result,
    }).eq("id", p["id"]).execute()
    graded.sort(key=lambda g: g["diff"])  # closest guess first
    actual = {"swing": swing, "result": result, "diff": None}
    # The swing our manager actually submitted, as a benchmark row on the same
    # pitches the players guessed on. Real result from the game.
    if swing is not None and 1 <= int(swing) <= 1000:
        d = utils.circular_diff(pitch, int(swing))
        _t("swing_guesses").upsert(
            {"pitch_id": p["id"], "user_id": ACTUAL_ID, "user_name": ACTUAL_NAME,
             "guess": int(swing), "diff": d, "result": result},
            on_conflict="pitch_id,user_id").execute()
        actual["diff"] = d
    # The model's swing, if one was frozen for this pitch from synced data: a second benchmark row.
    model = None
    if p.get("model_swing") is not None:
        md = utils.circular_diff(pitch, int(p["model_swing"]))
        mres = utils._diff_to_result(md, ranges)
        _t("swing_guesses").upsert(
            {"pitch_id": p["id"], "user_id": MODEL_ID, "user_name": MODEL_NAME,
             "guess": int(p["model_swing"]), "diff": md, "result": mres},
            on_conflict="pitch_id,user_id").execute()
        model = {"swing": int(p["model_swing"]), "diff": md, "result": mres}
    if not graded:
        return None  # nobody guessed: the pitch is still recorded and graded, there's just nothing to post
    return {"pitch_row": p, "pitch": pitch, "actual": actual, "model": model, "guesses": graded}


def graded_payload(game_code, seq: int | None = None) -> dict | None:
    """The results payload (same shape _grade_row returns) rebuilt from saved data for a
    graded pitch of this game: pitch number `seq`, or the latest graded one. None if the
    game has no graded pitch (or that seq isn't graded yet)."""
    q = (_t("swing_pitches").select("*").eq("game_code", str(game_code)).not_.is_("graded_at", "null"))
    q = q.eq("seq", seq) if seq else q.order("seq", desc=True).limit(1)
    rows = q.execute().data
    if not rows:
        return None
    p = rows[0]
    pitch = int(p["pitch"])
    ranges = [tuple(r) for r in (p.get("ranges") or utils.RESULT_RANGES)]
    graded = []
    for g in _t("swing_guesses").select("user_id,user_name,guess,diff,result").eq("pitch_id", p["id"]).execute().data:
        if g["user_id"] in (ACTUAL_ID, MODEL_ID) or g.get("diff") is None:
            continue
        graded.append({"user_name": display_name(g["user_id"], g["user_name"]), "guess": g["guess"],
                       "diff": g["diff"], "result": g["result"]})
    graded.sort(key=lambda r: r["diff"])
    swing = p.get("swing")
    actual = {"swing": swing, "result": p.get("actual_result"),
              "diff": utils.circular_diff(pitch, int(swing)) if swing is not None and 1 <= int(swing) <= 1000 else None}
    model = None
    if p.get("model_swing") is not None:
        md = utils.circular_diff(pitch, int(p["model_swing"]))
        model = {"swing": int(p["model_swing"]), "diff": md, "result": utils._diff_to_result(md, ranges)}
    return {"pitch_row": p, "pitch": pitch, "actual": actual, "model": model, "guesses": graded}


def grade_ready() -> list[dict]:
    """Grade every open pitch whose real pitch has landed in the synced plays
    table (the fallback for games the results feed doesn't cover). One payload
    per graded pitch, oldest first; pitches nobody guessed on close silently."""
    out = []
    for p in (_t("swing_pitches").select("*").is_("graded_at", "null").order("seq").execute().data):
        actual = _actual_pitch(p)
        if not actual:
            continue
        g = _grade_row(p, int(actual["pitch"]), actual.get("swing"), actual.get("result"),
                       int(actual["play_num"]))
        if g:
            out.append(g)
    return out


# ------------------------------------------------------------- results feed

_RESULT_RE = re.compile(r"\bResult:\s*\**\s*([A-Za-z0-9]+)")
_PITCH_RE = re.compile(r"\bPitch:\s*\**\s*(\d{1,4})\b")
_SWING_RE = re.compile(r"\bSwing:\s*\**\s*(\d{1,4})\b")
# Posted before the result. Guessing no longer closes on it - only on the result.
_BATTER_RE = re.compile(r"Current Batter:\s*\**\s*([A-Za-z]{2,4})\b")
_SWING_IN_RE = re.compile(r"swing has been swung|your swing is in", re.I)


def parse_feed(text: str) -> dict | None:
    """Classify a results-feed message. {"kind": "result", pitch, swing, result},
    {"kind": "swing_in"}, or None for everything else the ump bot says."""
    m_p, m_r = _PITCH_RE.search(text), _RESULT_RE.search(text)
    if m_p and m_r:
        m_s = _SWING_RE.search(text)
        return {"kind": "result", "pitch": int(m_p.group(1)),
                "swing": int(m_s.group(1)) if m_s else None, "result": m_r.group(1)}
    m_b = _BATTER_RE.search(text)
    if m_b:
        return {"kind": "batter_up", "team": m_b.group(1).upper()}
    if _SWING_IN_RE.search(text):
        return {"kind": "swing_in"}
    return None


def our_team_batting(game: dict) -> bool | None:
    """Fallback for when no "Batter Up!" has been seen yet (bot just started):
    infer from the sheet's inning/half - the half lags the plays sync, so the
    feed is preferred. None if it can't tell."""
    state = scouting_data.resolve_game_state(int(game["game_code"])) or {}
    half = str(state.get("half") or "").lower()
    if half not in ("top", "bottom"):
        return None
    return (game.get("away_team") == TEAM) == (half == "top")


def discard_empty_open(game_code: str) -> None:
    """Drop open pitch rows nobody guessed on, so a stale snapshot (from before
    the half-inning flipped) can't be reused for our next at-bat."""
    for p in (_t("swing_pitches").select("id").eq("game_code", str(game_code))
              .is_("graded_at", "null").execute().data):
        if not _t("swing_guesses").select("id").eq("pitch_id", p["id"]).limit(1).execute().data:
            _t("swing_pitches").delete().eq("id", p["id"]).execute()


def grade_from_feed(game_code: str, parsed: dict) -> dict | None:
    """Grade the game's open pitch straight from a feed result message, without
    waiting for the plays sync. None if there's no open pitch."""
    latest = _latest_pitch_row(str(game_code))
    if not latest or latest.get("graded_at"):
        return None
    return _grade_row(latest, parsed["pitch"], parsed.get("swing"), parsed["result"], None)


def _bases(result: str | None) -> int:
    if result in _HITS:
        return _HITS[result]
    return 1 if result in _WALKS else 0


# ------------------------------------------------------------- stats

# Situation-agnostic run value of each outcome (pooled S1-S12). RE24/WPA versions
# would replace this lookup with one keyed on the base-out state / leverage.
RV_HR, RV_3B, RV_2B, RV_1B, RV_BB = 1.415, 1.025, 0.761, 0.431, 0.310
RV_OUT_IN_PLAY, RV_OUT_NOT_IN_PLAY, RV_MULTI_OUT = -0.309, -0.335, -0.916
_RV = {"HR": RV_HR, "3B": RV_3B, "2B": RV_2B, "2BWH": RV_2B,
       "1B": RV_1B, "1BWH": RV_1B, "1BWH2": RV_1B, "IF1B": RV_1B,
       "BB": RV_BB, "IBB": RV_BB, "K": RV_OUT_NOT_IN_PLAY}
_MULTI_OUT = {"DP", "DP21", "DP31", "DPH1", "DPRun", "LODP", "TP", "LOTP"}


def run_value(result: str | None) -> float | None:
    """Pooled run value of a result; None for an unresolved ('?') result."""
    if result in _RV:
        return _RV[result]
    if result in _MULTI_OUT:
        return RV_MULTI_OUT
    if result in utils.RESULTS:
        return RV_OUT_IN_PLAY  # FO, GO, PO, FC, SacF, GORA, ... - outs on balls in play
    return None


def _transition(result: str, outs: int, obc: str) -> tuple[float, str, int]:
    """(runs scored, new obc, new outs) for a result - same source and rules as
    utils._wp_post_play (import_BRC.csv first, then the hand-coded fallback)."""
    entry = utils._BRC_RUN_LOOKUP.get((result, obc, outs))
    if entry is not None:
        runs, new_obc, new_outs = entry
    else:
        new_obc, runs_i = utils.advance_runners(result, obc, outs)
        runs, new_outs = float(runs_i), outs + utils.outs_added(result)
    return float(runs), new_obc, min(int(new_outs), 3)


def re24_value(result: str, outs: int, obc: str) -> float | None:
    """Runs scored + change in run expectancy, from this base-out state."""
    before = utils.RUN_EXPECTANCY.get((int(outs), str(obc)))
    if before is None or run_value(result) is None:
        return None
    runs, new_obc, new_outs = _transition(result, int(outs), str(obc))
    after = 0.0 if new_outs >= 3 else utils.RUN_EXPECTANCY.get((new_outs, new_obc))
    return None if after is None else runs + after - before


def wpa_value(result: str, pitch_row: dict) -> float | None:
    """Change in the Pioneers' win probability. None if the pitch has no score
    snapshot or the win-probability table is unavailable."""
    p = pitch_row
    if p.get("lead") is None or p.get("inning") is None or p.get("outs") is None or not p.get("obc"):
        return None
    if run_value(result) is None:
        return None
    rem = utils.remaining_half_innings(int(p["inning"]), str(p.get("half") or "top").lower(),
                                       utils.game_innings(p.get("league") or "MLN"))
    outs, obc, lead = int(p["outs"]), str(p["obc"]), int(p["lead"])
    before = utils.get_win_probability_interpolated(rem, outs, obc, lead)
    if before is None:
        return None
    return utils._wp_post_play(result, rem, outs, obc, lead) - before


_PV_CACHE: dict = {}


def pitch_values(pitch_row: dict) -> dict[str, dict]:
    """Per result on this pitch's ranges: flat run value, RE24 and WPA (raw -
    no random-guess baseline). Cached per pitch row."""
    key = pitch_row.get("id")
    if key in _PV_CACHE:
        return _PV_CACHE[key]
    out: dict[str, dict] = {}
    for res, _, _ in pitch_row.get("ranges") or []:
        if res not in out:
            out[res] = {"rv": run_value(res), "re24": None, "wpa": wpa_value(res, pitch_row)}
            if pitch_row.get("outs") is not None and pitch_row.get("obc"):
                out[res]["re24"] = re24_value(res, pitch_row["outs"], pitch_row["obc"])
    _PV_CACHE[key] = out
    return out


def _slash(results: list[str]) -> dict:
    h = bb = sf = tb = 0
    n = len(results)
    for r in results:
        if r in _HITS:
            h += 1
            tb += _HITS[r]
        elif r in _WALKS:
            bb += 1
        elif r in _SAC_FLY:
            sf += 1
    ab = n - bb - sf
    pa_obp = ab + bb + sf
    avg = h / ab if ab else 0.0
    obp = (h + bb) / pa_obp if pa_obp else 0.0
    slg = tb / ab if ab else 0.0
    return {"PA": n, "H": h, "BB": bb, "HR": sum(r == "HR" for r in results),
            "AVG": avg, "OBP": obp, "SLG": slg, "OPS": obp + slg}


def stats(scope: str, game_code: str | None = None, season: int | None = None, by: str = "rv") -> list[dict]:
    """Per-user slash lines plus summed run value, RE24 and WPA of their guesses
    (counting stats that reward giving more reads). Sorted by `by` (rv/re24/wpa/ops).
    scope: "game" (needs game_code), "season" (needs season), or "all"."""
    q = _t("swing_pitches").select("*").not_.is_("graded_at", "null")
    if scope == "game":
        q = q.eq("game_code", str(game_code))
    elif scope == "season":
        q = q.eq("season", season)
    pitches = {r["id"]: r for r in db._fetch_all(q)}
    ids = list(pitches)
    if not ids:
        return []
    by_user: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        for g in db._fetch_all(_t("swing_guesses").select("pitch_id,user_id,user_name,result,diff")
                               .in_("pitch_id", ids[i:i + 200]).not_.is_("result", "null")):
            u = by_user.setdefault(g["user_id"], {"name": g["user_name"], "results": [],
                                                  "rv": 0.0, "re24": 0.0, "wpa": 0.0, "diffs": []})
            u["name"] = display_name(g["user_id"], g["user_name"])
            u["results"].append(g["result"])
            if g.get("diff") is not None:
                u["diffs"].append(g["diff"])
            vals = pitch_values(pitches[g["pitch_id"]])
            mine = vals.get(g["result"])
            if mine:
                for k in ("rv", "re24", "wpa"):
                    if mine[k] is not None:
                        u[k] += mine[k]
    rows = [{"user_id": uid, "name": u["name"], **_slash(u["results"]), "RV": u["rv"], "RE24": u["re24"],
             "WPA": u["wpa"] * 100,  # win-% points
             "DPA": sum(u["diffs"]) / len(u["diffs"]) if u["diffs"] else None}
            for uid, u in by_user.items()]
    key = {"rv": "RV", "re24": "RE24", "wpa": "WPA"}.get(by)
    if key:
        rows.sort(key=lambda r: (-r[key], -r["PA"]))
    else:
        rows.sort(key=lambda r: (-r["OPS"], -r["PA"]))
    return rows


def player_history(user_id, limit: int = 10) -> dict:
    """A player's most recent graded guesses, newest first, each with the raw
    value of the guess's result (run value, RE24, WPA in win-% points) and the
    manager's own result for comparison, plus totals over those rows."""
    gs = (_t("swing_guesses").select("pitch_id,guess,diff,result").eq("user_id", str(user_id))
          .not_.is_("result", "null").order("pitch_id", desc=True).limit(limit).execute().data)
    if not gs:
        return {"rows": [], "totals": None}
    pitches = {r["id"]: r for r in _t("swing_pitches").select("*")
               .in_("id", [g["pitch_id"] for g in gs]).execute().data}
    rows = []
    for g in gs:
        p = pitches.get(g["pitch_id"])
        if not p:
            continue
        v = pitch_values(p).get(g["result"]) or {}
        rows.append({"pitch_row": p, "guess": g["guess"], "diff": g["diff"], "result": g["result"],
                     "pitch": p.get("pitch"), "actual_swing": p.get("swing"),
                     "actual_result": p.get("actual_result"), "rv": v.get("rv"), "re24": v.get("re24"),
                     "actual_rv": run_value(p.get("actual_result")),
                     "wpa": v["wpa"] * 100 if v.get("wpa") is not None else None})
    diffs = [r["diff"] for r in rows if r["diff"] is not None]
    totals = {**_slash([r["result"] for r in rows]),
              "RV": sum(r["rv"] or 0 for r in rows), "RE24": sum(r["re24"] or 0 for r in rows),
              "WPA": sum(r["wpa"] or 0 for r in rows), "DPA": sum(diffs) / len(diffs) if diffs else None}
    return {"rows": rows, "totals": totals}


_BRAND: dict | None = None


def team_brand() -> dict:
    """{"logo_url", "primary_hex"} for TEAM from the teams table (newest season)."""
    global _BRAND
    if _BRAND is None:
        try:
            rows = (_t("teams").select("logo_url,primary_hex,season").eq("full_team", TEAM)
                    .order("season", desc=True).limit(1).execute().data)
            _BRAND = rows[0] if rows else {}
        except Exception:
            _BRAND = {}
    return _BRAND


def player_card(user_id, limit: int = 10, game_code: str | None = None, season: int | None = None) -> dict:
    """Everything the /player card shows: the player's stat line for this game,
    this season and all time, plus their recent guesses (player_history)."""
    uid = str(user_id)

    def mine(rows: list[dict]) -> dict | None:
        return next((r for r in rows if r["user_id"] == uid), None)

    scopes: dict[str, dict | None] = {}
    if game_code:
        scopes["game"] = mine(stats("game", game_code=game_code))
    if season is not None:
        scopes["season"] = mine(stats("season", season=season))
    scopes["all"] = mine(stats("all"))
    return {"history": player_history(uid, limit), "scopes": scopes, "season": season, "game_code": game_code}


def _f(x: float) -> str:
    return f"{x:.3f}".lstrip("0") if x < 1 else f"{x:.3f}"


def format_leaderboard(rows: list[dict], title: str, min_pa: int = 1) -> str:
    rows = [r for r in rows if r["PA"] >= min_pa]
    if not rows:
        return f"**{title}**\nNo graded guesses yet."
    w = max(len(r["name"]) for r in rows[:15])
    head = (f"{'':>2} {'Name':<{w}} {'PA':>3} {'DPA':>5} {'RV':>6} {'RE24':>6} {'WPA%':>6} "
            f"{'AVG':>5} {'OBP':>5} {'SLG':>5} {'OPS':>5}")
    lines = [head] + [
        f"{i:>2} {r['name']:<{w}} {r['PA']:>3} {('-' if r['DPA'] is None else format(r['DPA'], '.1f')):>5} "
        f"{r['RV']:>+6.2f} {r['RE24']:>+6.2f} {r['WPA']:>+6.1f} "
        f"{_f(r['AVG']):>5} {_f(r['OBP']):>5} {_f(r['SLG']):>5} {_f(r['OPS']):>5}"
        for i, r in enumerate(rows[:15], 1)]
    return f"**{title}**\n```\n" + "\n".join(lines) + "\n```"


def format_graded(g: dict) -> str:
    p = g["pitch_row"]
    ctx = f"{p.get('batter') or '?'} vs {p.get('pitcher') or '?'}"
    lines = [f"The pitch is in!", f"It was: **{g['pitch']}**" +
             (f" ({g['actual'].get('result')})" if g["actual"].get("result") else "") + f"  -  {ctx}", ""]
    for r in g["guesses"]:
        lines.append(f"{r['user_name']} guessed **{r['guess']}**, for a diff of **{r['diff']}**: **{r['result']}**")
    a = g["actual"]
    if a.get("swing") is not None and a.get("diff") is not None:
        lines += ["", f"{ACTUAL_NAME} was **{a['swing']}**, for a diff of **{a['diff']}**: **{a.get('result')}**"]
    m = g.get("model")
    if m:
        lines.append(f"{MODEL_NAME} was **{m['swing']}**, for a diff of **{m['diff']}**: **{m['result']}**")
    return "\n".join(lines)


# ------------------------------------------------------------- current PA view

def peek_open_pitch(game: dict) -> dict | None:
    """The game's open (ungraded) pitch row, or None - read-only, unlike
    get_or_open_pitch, so viewing status never creates a row."""
    latest = _latest_pitch_row(str(game["game_code"]))
    return latest if latest and not latest.get("graded_at") else None


HOT_WINDOW = 10   # pitches against us (not a player's own guesses)
HOT_RV = 2.5      # about the top 5% of 10-pitch windows from pure random guessing


def hot_users(user_ids) -> set[str]:
    """Who is on fire: summed run value over the last HOT_WINDOW graded pitches against
    us is at least HOT_RV. Pitches a player skipped count as 0, so the streak decays by
    itself. Nobody is hot until there are HOT_WINDOW graded pitches in PioBall history."""
    pitches = (_t("swing_pitches").select("id").not_.is_("graded_at", "null")
               .order("graded_at", desc=True).limit(HOT_WINDOW).execute().data)
    if len(pitches) < HOT_WINDOW:
        return set()
    totals: dict[str, float] = {}
    for r in (_t("swing_guesses").select("user_id,result").in_("pitch_id", [p["id"] for p in pitches])
              .not_.is_("result", "null").execute().data):
        totals[r["user_id"]] = totals.get(r["user_id"], 0.0) + (run_value(r["result"]) or 0.0)
    return {str(u) for u in user_ids if totals.get(str(u), 0.0) >= HOT_RV}


def current_pa(game: dict) -> dict:
    """Everything /guesses shows: the open pitch's guesses (a plate appearance
    is a single pitch in Numberball). Read-only; pitch_row is None when
    nobody has opened the next pitch yet."""
    p = peek_open_pitch(game)
    if not p:
        return {"pitch_row": None, "guesses": [], "closed": False}
    guesses = (_t("swing_guesses").select("user_name,guess,user_id").eq("pitch_id", p["id"])
               .neq("user_id", ACTUAL_ID).neq("user_id", MODEL_ID).order("guess").execute().data)
    try:
        hot = hot_users({g["user_id"] for g in guesses})
    except Exception:  # the flame is a nicety - never block the guesses table on it
        hot = set()
    for g in guesses:
        g["user_name"] = display_name(g["user_id"], g["user_name"])
        g["hot"] = str(g["user_id"]) in hot
    return {"pitch_row": p, "guesses": guesses, "closed": is_closed(p)}


def format_current_pa(c: dict) -> str:
    p = c["pitch_row"]
    if not p:
        return "No open pitch yet - the first `/guess` opens the next one."
    if p.get("batter"):
        head = (f"**{p.get('batter')} vs {p.get('pitcher') or '?'}** - "
                f"{p.get('half') or ''} {p.get('inning') or ''}, {p.get('outs')} out "
                f"({'CLOSED - pitch is in' if c['closed'] else 'open'})")
    else:
        head = f"**Our next plate appearance** (batter TBD) ({'CLOSED' if c['closed'] else 'open'})"
    lines = [head]
    lines.append("**Active Guesses**")
    if c["guesses"]:
        lines.append("```\n" + "\n".join(f"{g['guess']:>4}  {g['user_name']}" for g in c["guesses"]) + "\n```")
    else:
        lines.append("None yet - `/guess`.")
    return "\n".join(lines)

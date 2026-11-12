"""PioBall response timers: a heads-up in the pioball channel before one of our pitches or swings times out.

The pioneerss results feed drives it (pioball_bot.py passes every parsed feed message to Tracker.observe):
  result posted      -> the next pitch is owed by the fielding side of the next plate appearance. Who that is comes from
                        the Plays (Raw) sheet tab (the last play's inning/half/outs plus the outs its result adds), not from
                        the message, so autos, balks, steals and IBBs all work: each of them is a play row. When we are the
                        fielding side, a pitch timer starts at the result's timestamp, pinging our pitcher from the
                        Lineups tab (its latest "P" row for us).
  "Batter Up!"       -> the pitch is in: the pitch timer ends. If it is our at-bat, a swing timer starts at that
                        timestamp, pinging the batter named in the message. A second ping for the same plate appearance
                        (pinch hitter) keeps the first ping's clock.
  "swing is in"      -> the swing timer ends.
A timer is 12 hours by default; in 6-hour mode (/timer) it is 6 hours, or 10 if it starts or would expire from 12am to 7am ET.
On a no-timer day (12am ET to 7am ET the next day, /notimerday) the clock is paused: the deadline moves out by the overlap.

Two alerts per timer, each sent at most once (a tick that finds both due sends one message):
  soon   - 2 hours or less left
  night  - it is between 10pm and 8am CT and the timer expires before 8am CT

Pure logic plus sheet reads; no Discord code. resolve_pending blocks on network reads, so the bot runs it in a thread.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import live_sheet
import pace
import swing_game
import utils

CENTRAL = ZoneInfo("America/Chicago")
EASTERN = ZoneInfo("America/New_York")
ALERT_WINDOW = timedelta(hours=2)
NIGHT_START_HOUR = 22      # CT: from 10pm ...
NIGHT_END_HOUR = 8         # ... until 8am
PENDING_GIVE_UP = timedelta(minutes=15)       # a result whose play never shows up in the sheet is dropped after this
SHEET_FALLBACK_AFTER = timedelta(minutes=5)   # until then the sheet's last play must match the result; after, it is trusted
ROLE_ID = os.environ.get("PIOBALL_REMINDER_ROLE_ID")   # pinged when no player can be named

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pioball_timer_settings.json")


# ------------------------------------------------------------- settings

# League no-timer days (Season 13 schedule). Each runs from 12am ET on its date to 7am ET the next day; the clock is paused
# for the whole window. Used until /notimerday saves a list of its own.
DEFAULT_NO_TIMER_DAYS = ("2026-07-04", "2026-10-12", "2026-11-11", "2026-11-26", "2026-11-27", "2026-12-31",
                         "2027-01-18", "2027-02-14", "2027-02-15")


def parse_day(text: str) -> date | None:
    """2026-11-11 or 11/11/2026 (or 11/11/26) -> date; None if it isn't one."""
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def load_settings() -> dict:
    """{"hours": 12 | 6, "no_timer_days": sorted ISO date strings}."""
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    raw = data.get("no_timer_days", DEFAULT_NO_TIMER_DAYS)
    days = sorted({d.isoformat() for d in (parse_day(x) for x in raw) if d})
    return {"hours": 6 if data.get("hours") == 6 else 12, "no_timer_days": days}


def save_settings(settings: dict) -> None:
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f)


# ------------------------------------------------------------- clock rules

def timer_length(start: datetime, hours: int) -> timedelta:
    """12 hours; in 6-hour mode 6 hours, or 10 when the timer starts or would expire from 12am to 7am ET."""
    if hours != 6:
        return timedelta(hours=12)
    end = start + timedelta(hours=6)
    overnight = start.astimezone(EASTERN).hour < 7 or end.astimezone(EASTERN).hour < 7
    return timedelta(hours=10) if overnight else timedelta(hours=6)


def no_timer_windows(days: list[str]) -> list[tuple[datetime, datetime]]:
    """UTC (start, end) of each no-timer window, 12am ET on the date to 7am ET the next day, touching ones merged."""
    out: list[tuple[datetime, datetime]] = []
    for d in sorted(parse_day(x) for x in days if parse_day(x)):
        s = datetime(d.year, d.month, d.day, tzinfo=EASTERN).astimezone(timezone.utc)
        nxt = d + timedelta(days=1)
        e = datetime(nxt.year, nxt.month, nxt.day, 7, tzinfo=EASTERN).astimezone(timezone.utc)
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def timer_deadline(start: datetime, hours: int, days: list[str] | None = None) -> tuple[datetime, bool]:
    """(deadline, paused): start plus the timer length, with the clock stopped inside every no-timer window. paused says
    whether any window pushed the deadline out."""
    length = timer_length(start, hours)
    if days is None:
        days = load_settings()["no_timer_days"]
    cur, left = start, length
    for w_start, w_end in no_timer_windows(days):
        if w_end <= cur:
            continue
        if w_start <= cur:
            cur = w_end            # starts inside a window: counting begins when it ends
            continue
        if w_start - cur >= left:
            break                  # finishes before this window starts
        left -= w_start - cur
        cur = w_end
    deadline = cur + left
    return deadline, deadline - start > length


def is_night(now: datetime) -> bool:
    hour = now.astimezone(CENTRAL).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def next_morning(now: datetime) -> datetime:
    """The next 8am CT after now."""
    c = now.astimezone(CENTRAL)
    cut = c.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)
    return cut if c < cut else cut + timedelta(days=1)


def due_alerts(deadline: datetime, now: datetime, sent: set[str]) -> list[str]:
    """Alerts that should go out now and haven't: "soon" and/or "night"."""
    remaining = deadline - now
    if remaining <= timedelta(0):
        return []
    out = []
    if "soon" not in sent and remaining <= ALERT_WINDOW:
        out.append("soon")
    if "night" not in sent and is_night(now) and deadline < next_morning(now):
        out.append("night")
    return out


# ------------------------------------------------------------- message text

def fmt_deadline(deadline: datetime, now: datetime) -> str:
    c, n = deadline.astimezone(CENTRAL), now.astimezone(CENTRAL)
    day = "" if c.date() == n.date() else c.strftime("%a ")
    return f"{day}{c.strftime('%I:%M %p').lstrip('0')} CT"


def fmt_left(left: timedelta) -> str:
    minutes = max(int(left.total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def state_text(state: dict) -> str:
    """"Bottom 7, 1 out, CAR 2 - POR 3" from a state dict (inning, half, outs, away/home + scores; any piece may be missing)."""
    parts = []
    if state.get("inning") is not None and state.get("half"):
        parts.append(f"{str(state['half']).capitalize()} {state['inning']}")
    if state.get("outs") is not None:
        parts.append(f"{state['outs']} out" if state["outs"] == 1 else f"{state['outs']} outs")
    if state.get("away") and state.get("away_score") is not None and state.get("home") and state.get("home_score") is not None:
        parts.append(f"{state['away']} {state['away_score']} - {state['home']} {state['home_score']}")
    return ", ".join(parts)


@dataclass
class Timer:
    kind: str                  # "pitch" | "swing"
    start: datetime
    deadline: datetime
    state: dict
    ping_id: str | None = None
    ping_name: str | None = None
    game_code: str | None = None
    sent: set = field(default_factory=set)
    hours: int = 12            # the length setting the timer was made under
    paused: bool = False       # a no-timer day is included in the deadline

    def lead(self) -> str:
        if self.ping_id:
            return f"<@{self.ping_id}> "
        if ROLE_ID:
            return f"<@&{ROLE_ID}> "
        return f"{self.ping_name}: " if self.ping_name else ""

    def describe(self, now: datetime) -> str:
        """The message body shared by both alerts and /timers: the expiration first, then the game state."""
        label = "Pitch" if self.kind == "pitch" else "Swing"
        text = f"{label} timer expires **{fmt_deadline(self.deadline, now)}** ({fmt_left(self.deadline - now)} left)."
        if self.paused:
            text += " Includes a no-timer day."
        st = state_text(self.state)
        return f"{text} {st}." if st else text

    def alert(self, now: datetime) -> str:
        return f"{self.lead()}{self.describe(now)}"


@dataclass
class Pending:
    """A result the feed posted whose next pitch timer is not worked out yet (the sheet may not have the play)."""
    parsed: dict
    ts: datetime


# ------------------------------------------------------------- sheet reads

def _norm(name) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def roster_discord_id(name: str | None) -> str | None:
    """Discord id of one of our players by name (None when not on file)."""
    if not name:
        return None
    want = _norm(name)
    for n, did in swing_game.roster().items():
        if _norm(n) == want:
            return did
    return None


_lineups: dict = {"at": 0.0, "rows": []}
_lineups_lock = threading.Lock()
_LINEUPS_TTL = 60


def _lineup_rows() -> list[dict]:
    with _lineups_lock:
        if time.time() - _lineups["at"] < _LINEUPS_TTL:
            return _lineups["rows"]
        try:
            _lineups["rows"] = utils.read_lineups_from_sheet(live_sheet.SHEET_ID)
        except Exception as e:
            print(f"lineups tab read failed: {e!r}")   # keep serving the last copy
        _lineups["at"] = time.time()
        return _lineups["rows"]


def our_pitcher(game_row: dict | None) -> str | None:
    """Our current pitcher per the Lineups tab: its latest "P" row for our team in this game."""
    gid = (game_row or {}).get("game_id_short")
    if not gid:
        return None
    best = None
    for r in _lineup_rows():
        if (r.get("game_id_short") == gid and _norm(r.get("team")) == _norm(swing_game.TEAM_ABBREV)
                and r.get("pos") == "P" and r.get("player_name")):
            if best is None or (r.get("row") or -1) > (best.get("row") or -1):
                best = r
    return best["player_name"] if best else None


def game_is_over(game_code: str) -> bool:
    try:
        return utils.game_is_final(live_sheet.game(game_code))
    except Exception:
        return False


# ------------------------------------------------------------- game state from the sheet

def next_state(last: dict, game_row: dict | None) -> dict:
    """State of the plate appearance after `last` (a live_sheet.plays row): inning, half, outs, who bats (off_team) and
    fields (def_team), plus abbreviations and the running score when the Games tab has them. The same rule the Manager tab
    and MLN Gameday use: the outs the result adds either end the half or carry over."""
    inning, half = int(last["inning"]), str(last["half"]).lower()
    outs = int(last.get("outs") or 0) + utils.outs_added(str(last.get("result") or ""))
    off, de = last.get("off_team"), last.get("def_team")
    if outs >= 3:
        inning += 1 if half == "bottom" else 0
        half = "bottom" if half == "top" else "top"
        outs, off, de = 0, de, off
    out = {"inning": inning, "half": half, "outs": outs, "off_team": off, "def_team": de}
    if game_row:
        try:
            abbrev = {t.get("full_team"): a for a, t in live_sheet.teams()["by_abbrev"].items()}
        except Exception:
            abbrev = {}
        away, home = game_row.get("away_team"), game_row.get("home_team")
        out.update(away=abbrev.get(away, away), home=abbrev.get(home, home),
                   away_score=game_row.get("away_score"), home_score=game_row.get("home_score"))
    return out


def _matches(last: dict, parsed: dict) -> bool:
    """Is the sheet's last play the one this feed result reports?"""
    if str(last.get("result") or "") != str(parsed.get("result") or ""):
        return False
    for key in ("pitch", "swing"):
        a, b = last.get(key), parsed.get(key)
        if a is not None and b is not None and int(a) != int(b):
            return False
    return True


def resolve_pending(pending: Pending, game_code: str, hours: int, now: datetime) -> tuple[str, Timer | None]:
    """("timer", Timer) when we owe the next pitch, ("none", None) when we don't (the other team pitches, or the game is
    over), ("retry", None) when the sheet doesn't have the play yet. Blocking: run in a thread."""
    plays = live_sheet.plays(game_code)
    if not plays:
        return "retry", None
    last = plays[-1]
    if not _matches(last, pending.parsed) and now - pending.ts < SHEET_FALLBACK_AFTER:
        return "retry", None
    game_row = live_sheet.game(game_code)
    if utils.game_is_final(game_row):
        return "none", None
    st = next_state(last, game_row)
    if st["def_team"] != swing_game.TEAM:
        return "none", None
    name = our_pitcher(game_row)
    start = pending.ts
    deadline, paused = timer_deadline(start, hours)
    return "timer", Timer("pitch", start, deadline, st, ping_id=roster_discord_id(name), ping_name=name,
                          game_code=str(game_code), hours=hours, paused=paused)


# ------------------------------------------------------------- tracker

class Tracker:
    """The one live timer (or the result still waiting to become one). Event-loop only: observe and the bot's tick run on
    the same thread; resolve_pending (worker thread) only reads what it is handed."""

    def __init__(self) -> None:
        self.timer: Timer | None = None
        self.pending: Pending | None = None

    def observe(self, parsed: dict, text: str, ts: datetime, hours: int) -> None:
        kind = parsed["kind"]
        if kind == "result":
            self.timer = None
            self.pending = Pending(parsed, ts)
        elif kind == "batter_up":
            self.pending = None
            if parsed["team"] != swing_game.TEAM_ABBREV:
                self.timer = None   # their at-bat: our pitch is in, nothing of ours is owed
                return
            state = pace.parse_state(text)
            m = re.search(r"Current Batter:[^\n]*?<@!?(\d{15,25})>", text)
            ping_id = m.group(1) if m else roster_discord_id(state.get("batter"))
            if self.timer is not None and self.timer.kind == "swing":
                # pinch hitter / re-ping: the batting side's clock keeps running from the first ping
                self.timer.state, self.timer.ping_id, self.timer.ping_name = state, ping_id, state.get("batter")
                return
            deadline, paused = timer_deadline(ts, hours)
            self.timer = Timer("swing", ts, deadline, state, ping_id=ping_id, ping_name=state.get("batter"),
                               hours=hours, paused=paused)
        elif kind == "swing_in":
            if self.timer is not None and self.timer.kind == "swing":
                self.timer = None

    def recompute(self) -> None:
        """Re-derive the running timer's deadline (the no-timer days changed). Alerts already sent stay sent."""
        t = self.timer
        if t is not None:
            t.deadline, t.paused = timer_deadline(t.start, t.hours)

    def describe(self, now: datetime) -> str:
        if self.timer is not None:
            who = self.timer.ping_name or "nobody named"
            return f"{self.timer.describe(now)} Pinging: {who}. Alerts sent: {', '.join(sorted(self.timer.sent)) or 'none'}."
        if self.pending is not None:
            return "Last result is in; waiting for the sheet to show whose pitch is next."
        return "No timer running for us."

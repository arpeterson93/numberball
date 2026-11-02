"""Pace of play from the results feed: who held the game up, and for how long.

Every plate appearance is one pitch, and the feed timestamps three of its steps:
  "Batter Up!" (the ump has the pitch and pings the batter)  -> ping
  "A swing has been swung of N!"                              -> swing
  the Pitch/Swing/Result embed                                -> result
which splits the time into
  ping -> swing     the batter's team (batting)
  swing -> result   the ump
  result -> ping    the next pitcher's team (pitching) plus the ump's relay; we can't see the split,
                    so the ump's typical turnaround is carved out of it as an estimate.

Each stretch is counted twice: raw elapsed time, and "active" time where overnight hours count for less
(Central time, ACTIVE_CURVE). Pure functions over feed messages; pioball_bot.py fetches the messages.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import swing_game

CENTRAL = ZoneInfo("America/Chicago")
# How much a quarter hour of the Central-time day counts, from when results actually get logged: more results in a
# slice of the day means more players around, so that time weighs more. Busiest quarter hour = 1.0. Seasons 11-13's play
# timestamps (17,932 results), smoothed around the clock; regenerate with tools/pace_activity_curve.py.
ACTIVE_CURVE = (
    0.429, 0.385, 0.344, 0.306, 0.272, 0.240, 0.213, 0.188,
    0.167, 0.149, 0.135, 0.124, 0.116, 0.111, 0.110, 0.112,
    0.117, 0.126, 0.138, 0.153, 0.171, 0.191, 0.214, 0.239,
    0.266, 0.295, 0.325, 0.355, 0.387, 0.419, 0.452, 0.486,
    0.520, 0.554, 0.589, 0.623, 0.658, 0.692, 0.726, 0.759,
    0.791, 0.821, 0.849, 0.876, 0.900, 0.921, 0.940, 0.957,
    0.970, 0.981, 0.989, 0.995, 0.999, 1.000, 1.000, 0.998,
    0.994, 0.990, 0.985, 0.980, 0.975, 0.970, 0.965, 0.961,
    0.957, 0.954, 0.952, 0.951, 0.951, 0.951, 0.953, 0.955,
    0.958, 0.962, 0.967, 0.971, 0.976, 0.979, 0.981, 0.981,
    0.979, 0.973, 0.964, 0.950, 0.931, 0.907, 0.879, 0.845,
    0.807, 0.765, 0.720, 0.672, 0.623, 0.573, 0.524, 0.476,
)
_BIN_S = 86400 // len(ACTIVE_CURVE)
_DAY_ACTIVE = sum(ACTIVE_CURVE) * _BIN_S

_BATTER_UP_RE = re.compile(r"\*\*\*(?P<banner>.+?)\*\*\*\s*-\s*(?P<ump>[^\n`]+)", re.S)
# The Swing-E bot confirms a swing in a few wordings ("A swing has been swung of N!", "Your swing is in!",
# "Swing is in!"), always after a "Processing swing" notice that doesn't count. Its name is the stable part.
_SWING_CONFIRM_RE = re.compile(r"swing has been swung|swing is in", re.I)
_SWING_BOT_RE = re.compile(r"Swing-E", re.I)
_BATTER_LINE_RE = re.compile(r"Current Batter:\**\s*([A-Za-z]{2,4})\s+(\S+)\s+(.+?)\s*\((?:<@|@)")
_SCORE_RE = re.compile(r"^([A-Za-z]{2,4})\s+(\d+)\s.*?([\u25b2\u25bc])\s*(\d+)\s*$")
_HOME_RE = re.compile(r"^([A-Za-z]{2,4})\s+(\d+)\s.*?(\d)\s+Outs?\s*$")
_WELCOME_RE = re.compile(r"Season\s+\d+\s+Game\s+\d+:.*?-\s*(\d{5,7})\)")


# ------------------------------------------------------------- active-time clock

def _local_naive(ts: datetime) -> datetime:
    return ts.astimezone(CENTRAL).replace(tzinfo=None)


def _cum_active(local: datetime) -> float:
    """Active seconds from local midnight of the epoch day to a naive Central wall-clock time."""
    day = (local.date() - datetime(2000, 1, 1).date()).days
    secs = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    total = day * _DAY_ACTIVE
    full, rem = divmod(secs, _BIN_S)
    return total + sum(ACTIVE_CURVE[:int(full)]) * _BIN_S + rem * ACTIVE_CURVE[min(int(full), len(ACTIVE_CURVE) - 1)]


def active_seconds(a: datetime, b: datetime) -> float:
    """Time between two aware datetimes with each Central-time hour weighted by ACTIVE_CURVE."""
    if b <= a:
        return 0.0
    return _cum_active(_local_naive(b)) - _cum_active(_local_naive(a))


# ------------------------------------------------------------- feed -> plate appearances

def parse_game_code(text: str) -> str | None:
    m = _WELCOME_RE.search(text)
    return m.group(1) if m else None


def parse_state(text: str) -> dict:
    """What the "Batter Up!" scorebug says: pitcher (last name), inning, half, outs, runners and score, plus the
    batter's name and position from the Current Batter line. Missing pieces are left out."""
    out: dict = {}
    m = _BATTER_LINE_RE.search(text)
    if m:
        out["batter_pos"], out["batter"] = m.group(2), m.group(3).strip()
    block = re.search(r"```(.*?)```", text, re.S)
    if not block:
        return out
    lines = [ln.rstrip() for ln in block.group(1).strip("\n").split("\n")]
    if len(lines) >= 4:
        out["pitcher"] = re.sub(r"\s+[\d.]+$|\s+-+$", "", lines[0].strip())
        away = _SCORE_RE.match(lines[2].strip())
        home = _HOME_RE.match(lines[3].strip())
        if away:
            out["away"], out["away_score"] = away.group(1).upper(), int(away.group(2))
            out["half"] = "top" if away.group(3) == "\u25b2" else "bottom"
            out["inning"] = int(away.group(4))
            out["runner_2b"] = "\u25cf" in lines[2]
        if home:
            out["home"], out["home_score"], out["outs"] = home.group(1).upper(), int(home.group(2)), int(home.group(3))
            marks = [c for c in lines[3] if c in "\u25cb\u25cf"]
            if len(marks) == 2:
                out["runner_3b"], out["runner_1b"] = marks[0] == "\u25cf", marks[1] == "\u25cf"
    return out


SESSION_START_HOUR = 11  # Central: sessions technically open at 11am, which starts the first pitcher's clock


def _session_start(first_ping: datetime) -> datetime | None:
    """11am Central on the day of the first ping (None when the game opened before then)."""
    start = first_ping.astimezone(CENTRAL).replace(hour=SESSION_START_HOUR, minute=0, second=0, microsecond=0)
    return start if start < first_ping else None


def build_pas(messages: list[dict]) -> list[dict]:
    """messages: [{"ts": aware datetime, "text": str}] oldest first -> one dict per plate appearance:
    team (batting abbrev), ump, ping, swing (or None), result (or None), pitch, swing_value, outcome."""
    pas: list[dict] = []
    cur: dict | None = None
    for m in messages:
        text, ts = m["text"], m["ts"]
        parsed = swing_game.parse_feed(text)
        if parsed and parsed["kind"] == "batter_up":
            if cur and cur["result"] is None and cur["team"] == parsed["team"]:
                # pinch hitter / re-ping for the same PA: the batting side's clock keeps running from the first ping, and a
                # swing posted before the swap didn't count (the ump handles the result)
                cur["subs"] += 1
                cur["swing"] = None
                cur.setdefault("first_batter", cur.get("batter"))
                cur.update({k: v for k, v in parse_state(text).items() if k in ("batter", "batter_pos")})
                continue
            hdr = _BATTER_UP_RE.search(text)
            cur = {"team": parsed["team"], "ump": (hdr.group("ump").strip() if hdr else None), "ping": ts,
                   "swing": None, "result": None, "pitch": None, "swing_value": None, "outcome": None, "subs": 0,
                   "opened": _session_start(ts) if not pas else None, **parse_state(text)}
            pas.append(cur)
        elif parsed and parsed["kind"] == "result" and parsed["result"] in NOPING_RESULTS and (cur is None or cur["result"]):
            # no pitch, no ping: the stretch since the previous result is the pitching side's (team is filled in below)
            outs = re.search(r"(\d) outs?\b", text)
            cur = {"team": None, "ump": cur["ump"] if cur else None, "ping": None, "swing": None, "result": ts,
                   "pitch": parsed["pitch"], "swing_value": parsed.get("swing"), "outcome": parsed["result"], "subs": 0,
                   "noping": True, "outs": int(outs.group(1)) if outs else None,
                   "opened": _session_start(ts) if not pas else None}
            pas.append(cur)
        elif cur is None:
            continue
        elif _SWING_CONFIRM_RE.search(text) or (_SWING_BOT_RE.search(text) and not re.search(r"processing", text, re.I)):
            if cur["swing"] is None and cur["result"] is None:
                cur["swing"] = ts
            elif cur["result"] is not None:
                cur["moved_on"] = True   # a new swing after the result: a later result is not a correction of this one
        elif parsed and parsed["kind"] == "result":
            if cur["result"] is None:
                cur.update(result=ts, pitch=parsed["pitch"], swing_value=parsed.get("swing"), outcome=parsed["result"])
            elif (ts - cur["result"] <= CORRECTION_WINDOW and not cur.get("moved_on")
                  and {cur["pitch"], cur["swing_value"]} & {parsed["pitch"], parsed.get("swing")} - {None}):
                # the ump re-posted the result (a typo in one number, or pitch and swing swapped), so it shares a value
                # with the first: the later values win, the clock keeps the first post's time
                cur["corrected"] = True
                cur.update(pitch=parsed["pitch"], swing_value=parsed.get("swing"), outcome=parsed["result"])
    return _place_noping(pas)


def _next_batting(p: dict, teams: list[str]) -> str:
    """The side that bats after p's result: the same team unless the outs reach 3."""
    outs = (p.get("outs") or 0) + _OUTS_ADDED.get(p["outcome"], 0)
    return next((t for t in teams if t != p["team"]), p["team"]) if outs >= 3 else p["team"]


def _place_noping(pas: list[dict]) -> list[dict]:
    """Give each no-ping PA the side and scorebug state of the batter who follows it (or, with none after it, work them
    out from the PA before). One with neither is dropped."""
    teams = sorted({p["team"] for p in pas if p["team"]})
    out: list[dict] = []
    for i, p in enumerate(pas):
        if p.get("noping"):
            nxt = next((q for q in pas[i + 1:] if not q.get("noping")), None)
            prev = next((q for q in reversed(out) if q.get("team")), None)
            if nxt:
                p.update({k: nxt[k] for k in ("team", "inning", "half", "pitcher", "away_score", "home_score") if k in nxt})
            elif prev:
                p["team"] = _next_batting(prev, teams)
                if p["team"] != prev["team"] and prev.get("inning") is not None:
                    p["half"] = "bottom" if prev.get("half") == "top" else "top"
                    p["inning"] = prev["inning"] + (0 if prev.get("half") == "top" else 1)
                elif prev.get("inning") is not None:
                    p.update({"inning": prev["inning"], "half": prev.get("half")})
            if not p["team"]:
                continue
        out.append(p)
    return out


# ------------------------------------------------------------- allocation

def _sec(a: datetime, b: datetime) -> tuple[float, float]:
    """(raw seconds, active seconds) between two moments."""
    return max((b - a).total_seconds(), 0.0), active_seconds(a, b)


_OUTS_ADDED = {"K": 1, "GO": 1, "FO": 1, "PO": 1, "SacF": 1, "SacB": 1, "DFO": 1, "KCS": 1,
               "DP": 2, "DP31": 2, "DP21": 2, "DPH1": 2, "LODP": 2, "DSacF": 2, "TP": 3}


# Autos: the side that owed the move ran out the clock, so the play ends on the maximum time and goes against that side.
# bAuto / AutoK are the batter's (scored as strikeouts); pAuto / AutoBB (a walk) / AutoSB (a steal the defense never
# answered) / cAuto (the catcher's) are the pitching side's.
AUTO_RESULTS = {"bAuto": "bat", "AutoK": "bat", "pAuto": "pitch", "AutoBB": "pitch", "AutoSB": "pitch", "cAuto": "pitch",
                "Balk": "pitch"}
# Results that arrive with no Batter Up ping before them: the pitching side's pitch never came (the autos and a balk) or
# isn't thrown (IBB), so the whole stretch since the previous result is the pitching side's.
NOPING_RESULTS = {"pAuto", "AutoBB", "AutoSB", "cAuto", "Balk", "IBB"}
# A second result this soon after the first, with no ping or swing in between and a pitch or swing value in common, is
# the ump's correction of it.
CORRECTION_WINDOW = timedelta(minutes=30)


def auto_side(p: dict) -> str | None:
    """"bat" when the batting side timed out, "pitch" when the pitching side did, else None."""
    return AUTO_RESULTS.get(p.get("outcome"))


def open_stretch(pas: list[dict], now: datetime) -> dict | None:
    """The clock still running at `now`: {"kind": "wait" | "hit" | "ump", "owner": team abbrev or "", "since", "raw", "act"}.
    Ping posted -> batting team; swing posted -> ump; result posted -> the fielding side of the next PA (the ump's relay
    estimate is not split out here: the stretch is shown as it stands). Which side bats next is read from the outs
    the last PA left (None when the outs are unknown and it was not obviously the third)."""
    if not pas or now is None:
        return None
    p = pas[-1]
    teams = sorted({q["team"] for q in pas})
    flip = False
    if p["result"]:
        since, kind = p["result"], "wait"
        nxt = _next_batting(p, teams)
        flip = nxt != p["team"]
        owner = next((t for t in teams if t != nxt), "")
    elif p["swing"]:
        since, kind, owner = p["swing"], "ump", ""
    else:
        since, kind, owner = p["ping"], "hit", p["team"]
    raw, act = _sec(since, now)
    return {"kind": kind, "owner": owner, "since": since, "raw": raw, "act": act, "ump": p["ump"] or "?", "flip": flip}


def analyze(pas: list[dict], now: datetime | None = None) -> dict:
    """Allocate every measurable stretch. Only fully timed PAs count (a ping and a result); a PA with no
    swing message (auto swing, or the message missed) can't be split between batter and ump, so it is
    estimated (see below) and counted in `unsplit`.

    Returns {"teams": {abbrev: {"bat": [(raw, act)...], "pitch": [...]}}, "ump": {name: {...}},
             "relay_est": (raw, act), ...}."""
    done = [i for i, p in enumerate(pas) if p["result"]]
    teams_seen = sorted({p["team"] for p in pas})
    fielding = {t: next((o for o in teams_seen if o != t), None) for t in teams_seen}

    ump_turn = [_sec(p["swing"], p["result"]) for p in pas if p["swing"] and p["result"]]
    turn_raw = statistics.median(r for r, _ in ump_turn) if ump_turn else 0.0
    turn_act = statistics.median(a for _, a in ump_turn) if ump_turn else 0.0

    teams = {t: {"bat": [], "pitch": [], "unsplit": []} for t in teams_seen}
    autos = {t: {"bat": 0, "pitch": 0} for t in teams_seen}
    umps: dict[str, dict] = {}

    def ump_row(name):
        return umps.setdefault(name or "?", {"review": [], "relay": [], "unsplit": []})

    for i, p in enumerate(pas):
        if not p["result"] and not (now and i == len(pas) - 1):
            continue
        side = auto_side(p) if p["result"] else None
        if p.get("noping"):
            f = fielding[p["team"]]
            since = pas[i - 1]["result"] if i > 0 else p.get("opened")
            if since and f:
                teams[f]["pitch"].append(_sec(since, p["result"]))
            if side == "pitch" and f:
                autos[f]["pitch"] += 1
            continue
        u = ump_row(p["ump"])
        if side == "bat":
            autos[p["team"]]["bat"] += 1
        elif side == "pitch" and fielding[p["team"]]:
            autos[fielding[p["team"]]]["pitch"] += 1
        if not p["result"]:
            pass   # the open last PA: its own stretch is res["open"], but the wait before its ping is finished time
        elif p["swing"]:
            teams[p["team"]]["bat"].append(_sec(p["ping"], p["swing"]))
            u["review"].append(_sec(p["swing"], p["result"]))
        elif auto_side(p) == "bat":
            teams[p["team"]]["bat"].append(_sec(p["ping"], p["result"]))   # never swung: the ump had nothing to do
        else:
            # No swing post (conditional / ump-handled swing): ping -> result mixes batter and ump time, so the
            # ump gets their median turnaround and the batting team the rest.
            tot_raw, tot_act = _sec(p["ping"], p["result"])
            ur, ua = min(tot_raw, turn_raw), min(tot_act, turn_act)
            teams[p["team"]]["bat"].append((tot_raw - ur, tot_act - ua))
            teams[p["team"]]["unsplit"].append((tot_raw - ur, tot_act - ua))
            u["review"].append((ur, ua))
            u["unsplit"].append((ur, ua))
        # the stretch before this PA's ping: previous result -> this ping, owned by the fielding team
        since = pas[i - 1]["result"] if i > 0 else p.get("opened")
        if since:
            gap_raw, gap_act = _sec(since, p["ping"])
            relay_raw, relay_act = min(gap_raw, turn_raw), min(gap_act, turn_act)
            f = fielding[p["team"]]
            if f:
                teams[f]["pitch"].append((gap_raw - relay_raw, gap_act - relay_act))
            u["relay"].append((relay_raw, relay_act))
    return {"teams": teams, "umps": umps, "autos": autos, "n_pa": len(done), "turnaround": (turn_raw, turn_act),
            "start": (pas[0]["ping"] or pas[0]["result"]) if pas else None, "end": pas[done[-1]]["result"] if done else None,
            "open": open_stretch(pas, now)}


def _stats(rows: list[tuple[float, float]], extra: tuple[float, float] = (0.0, 0.0)) -> dict:
    """raw / act totals include `extra` (an open stretch); averages and medians stay on the finished rows."""
    n = len(rows)
    rows_raw, rows_act = sum(r for r, _ in rows), sum(a for _, a in rows)
    raw, act = rows_raw + extra[0], rows_act + extra[1]
    return {"n": n, "raw": raw, "act": act,
            "avg_raw": rows_raw / n if n else 0.0, "avg_act": rows_act / n if n else 0.0,
            "med_raw": statistics.median(r for r, _ in rows) if n else 0.0,
            "med_act": statistics.median(a for _, a in rows) if n else 0.0}


def summarize(res: dict) -> dict:
    """Totals, shares and per-PA / per-BF figures ready to print."""
    op = res.get("open")
    ex = lambda kind, owner: (op["raw"], op["act"]) if op and op["kind"] == kind and op["owner"] == owner else (0.0, 0.0)
    teams = {t: {"bat": _stats(v["bat"], ex("hit", t)), "pitch": _stats(v["pitch"], ex("wait", t)),
                 "unsplit": _stats(v["unsplit"])}
             for t, v in res["teams"].items()}
    last_ump = op["ump"] if op else None   # an open ump stretch goes on the ump of the last PA
    umps = {u: {"review": _stats(v["review"], ex("ump", "") if u == last_ump else (0.0, 0.0)),
                "relay": _stats(v["relay"]), "unsplit": _stats(v["unsplit"])}
            for u, v in res["umps"].items()}
    # "unsplit" rows are already inside bat / review, kept only to count how many PAs were estimated.
    total_raw = (sum(t[k]["raw"] for t in teams.values() for k in ("bat", "pitch"))
                 + sum(u["review"]["raw"] + u["relay"]["raw"] for u in umps.values()))
    total_act = (sum(t[k]["act"] for t in teams.values() for k in ("bat", "pitch"))
                 + sum(u["review"]["act"] + u["relay"]["act"] for u in umps.values()))
    return {"teams": teams, "umps": umps, "total_raw": total_raw, "total_act": total_act,
            "n_pa": res["n_pa"], "turnaround": res["turnaround"], "start": res["start"], "end": res["end"],
            "open": op, "autos": res.get("autos", {})}


def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def open_note(s: dict) -> str:
    """Subtitle fragment for a running clock, e.g. ', incl. 2h10m open (HOU pitching)'; empty when nothing is open."""
    op = s.get("open")
    if not op:
        return ""
    who = {"wait": f"{op['owner']} pitching", "hit": f"{op['owner']} batting", "ump": "ump"}[op["kind"]]
    return f", incl. {fmt_dur(op['raw'])} open ({who})"


# ------------------------------------------------------------- game window + report

def is_live(g: dict, now: datetime | None = None) -> bool:
    """A game still being played. The games row can't say so through end_time / win_team (a live game carries a rolling
    end_time, ~5h early, and its current leader); a finished game is the one with a winning pitcher or player of the game.
    A game whose last play is over 14 days old is treated as stale, not live."""
    if g.get("winning_pitcher") or g.get("player_of_game"):
        return False
    end = parse_ts(g.get("end_time"))
    return end is None or ((now or datetime.now(timezone.utc)) - end).days < 14


def parse_ts(value) -> datetime | None:
    """A games-table timestamp ('2026-10-21T12:00:00+00:00') or the sheet's raw '10/21/2026 12:00:00' as an aware datetime, or None."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            ts = datetime.strptime(str(value).strip(), "%m/%d/%Y %H:%M:%S")
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=ZoneInfo("UTC"))

def trim_to_game(messages: list[dict], game_code: str) -> list[dict]:
    """Cut a feed window down to one game: from its "Welcome" post (when the feed has one) to the next game's.
    Older games have no Welcome post, so the whole window is kept."""
    code = str(game_code)
    marks = [(i, parse_game_code(m["text"])) for i, m in enumerate(messages)]
    marks = [(i, c) for i, c in marks if c]
    start = next((i for i, c in marks if c == code), 0)
    end = next((i for i, c in marks if c != code and i > start), len(messages))
    return messages[start:end]


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def format_report(s: dict, title: str) -> str:
    """Plain-text report (wrap in a code block). Team and ump figures only - no individual players."""
    teams, umps = s["teams"], s["umps"]
    tr, ta = s["total_raw"], s["total_act"]
    out = [title,
           f"{s['n_pa']} plate appearances | {fmt_dur(tr)} on the clock, {fmt_dur(ta)} active (Central time, nights discounted)",
           "", "SHARE OF GAME TIME   raw | active"]
    out.append(f"{'':5}{'batting':>13}{'pitching':>13}{'total':>13}")
    for t, v in sorted(teams.items()):
        b, p = v["bat"], v["pitch"]
        cells = [f"{_pct(b['raw'], tr)} | {_pct(b['act'], ta)}", f"{_pct(p['raw'], tr)} | {_pct(p['act'], ta)}",
                 f"{_pct(b['raw'] + p['raw'], tr)} | {_pct(b['act'] + p['act'], ta)}"]
        out.append(f"{t:5}" + "".join(f"{c:>13}" for c in cells))
    u_raw = sum(u["review"]["raw"] + u["relay"]["raw"] for u in umps.values())
    u_act = sum(u["review"]["act"] + u["relay"]["act"] for u in umps.values())
    out.append(f"{'Ump':5}{'':>26}{_pct(u_raw, tr) + ' | ' + _pct(u_act, ta):>13}")

    out += ["", "PER PA (batting) / PER BF (pitching)   avg, median; raw | active"]
    for t, v in sorted(teams.items()):
        for label, key in (("bat", "bat"), ("pitch", "pitch")):
            st = v[key]
            if st["n"]:
                out.append(f"{t:4}{label:6}n={st['n']:<3} avg {fmt_dur(st['avg_raw']):>7} | {fmt_dur(st['avg_act']):>7}"
                           f"   med {fmt_dur(st['med_raw']):>7} | {fmt_dur(st['med_act']):>7}")

    out += ["", "UMPIRE"]
    for name, u in sorted(umps.items(), key=lambda kv: -kv[1]["review"]["raw"]):
        r, rl = u["review"], u["relay"]
        out.append(f"{name[:18]}")
        out.append(f"  swing -> result  n={r['n']:<3} avg {fmt_dur(r['avg_raw']):>7} | {fmt_dur(r['avg_act']):>7}"
                   f"   med {fmt_dur(r['med_raw']):>7} | {fmt_dur(r['med_act']):>7}   total {fmt_dur(r['raw'])} raw")
        out.append(f"  relay (estimate) n={rl['n']:<3} avg {fmt_dur(rl['avg_raw']):>7} | {fmt_dur(rl['avg_act']):>7}"
                   f"   total {fmt_dur(rl['raw'])} raw")
    out += ["", "Pitching = result posted -> next batter pinged, minus an estimated ump relay (the ump's median",
            "swing -> result time), since the pitcher's private submission can't be seen."]
    n_un = sum(v["unsplit"]["n"] for v in teams.values())
    if n_un:
        out.append(f"{n_un} PA had no swing post (conditional / ump-handled): ump credited their median turnaround, rest to the batting team.")
    return "\n".join(out)


def _rgb(hexstr: str) -> tuple[int, int, int] | None:
    h = (hexstr or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _color_distance(a: str, b: str) -> float:
    """Redmean distance, same as colorDistance in docs/js/app.js (missing colour = maximally different)."""
    ca, cb = _rgb(a), _rgb(b)
    if not ca or not cb:
        return float("inf")
    rmean = (ca[0] + cb[0]) / 2
    dr, dg, db = ca[0] - cb[0], ca[1] - cb[1], ca[2] - cb[2]
    return ((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db) ** 0.5


TEAM_COLOR_MIN_DISTANCE = 100   # same threshold as the Gameday scoreboard


def game_sides(game: dict, looks: dict, feed_teams: list[str]) -> list[dict]:
    """[away, home] as {"abbrev", "hex"}: the abbreviation as the feed writes it (teams table first, else
    whichever feed abbreviation the other side isn't) and a colour chosen like Gameday's gameTeamColors:
    primaries unless they clash, then home primary + away secondary, home secondary + away primary, both secondary."""
    from key_moments_build import SECONDARY_HEX
    rows = []
    for key in ("away_team", "home_team"):
        look = looks.get(game.get(key)) or {}
        rows.append({"abbrev": look.get("abbrev"), "primary": (look.get("primary_hex") or "").lstrip("#")})
    for i, r in enumerate(rows):
        if r["abbrev"] not in feed_teams:
            other = rows[1 - i]["abbrev"]
            left = [t for t in feed_teams if t != other]
            r["abbrev"] = left[0] if left else (r["abbrev"] or "?")
    for r, fallback in zip(rows, ("3b7dd8", "e08a3c")):
        r["primary"] = r["primary"] if _rgb(r["primary"]) else fallback
        r["secondary"] = SECONDARY_HEX.get(r["abbrev"]) or r["primary"]
    away, home = rows
    tiers = [(away["primary"], home["primary"]), (away["secondary"], home["primary"]),
             (away["primary"], home["secondary"]), (away["secondary"], home["secondary"])]
    pick = next((t for t in tiers[:-1] if _color_distance(*t) >= TEAM_COLOR_MIN_DISTANCE), tiers[-1])
    return [{"abbrev": away["abbrev"], "hex": pick[0]}, {"abbrev": home["abbrev"], "hex": pick[1]}]


def merge_results(results: list[tuple[dict, str]], ours: str, opp: str = "OPP") -> dict:
    """Pool several games' analyze() results into one: `ours` stays under its abbreviation, every opponent
    is folded into `opp`, and umpires are pooled by name."""
    teams = {ours: {"bat": [], "pitch": [], "unsplit": []}, opp: {"bat": [], "pitch": [], "unsplit": []}}
    umps: dict[str, dict] = {}
    autos = {ours: {"bat": 0, "pitch": 0}, opp: {"bat": 0, "pitch": 0}}
    n_pa, starts, ends = 0, [], []
    for res, us in results:
        for t, v in res.get("autos", {}).items():
            for k in ("bat", "pitch"):
                autos[ours if t == us else opp][k] += v[k]
        for t, v in res["teams"].items():
            dst = teams[ours if t == us else opp]
            for k in ("bat", "pitch", "unsplit"):
                dst[k] += v[k]
        for name, u in res["umps"].items():
            dst_u = umps.setdefault(name, {"review": [], "relay": [], "unsplit": []})
            for k in ("review", "relay", "unsplit"):
                dst_u[k] += u[k]
        n_pa += res["n_pa"]
        starts.append(res["start"])
        ends.append(res["end"])
    return {"teams": teams, "umps": umps, "autos": autos, "n_pa": n_pa, "turnaround": (0.0, 0.0),
            "start": min((s for s in starts if s), default=None), "end": max((e for e in ends if e), default=None)}


def adjust_for_volume(s: dict, a: str, h: str) -> dict[str, dict]:
    """Each side's hitting + pitching time as if both teams had the same number of PAs and BFs (the average of
    the two), keeping each team's own per-PA / per-BF rates. Hitting takes far longer than pitching, so a team
    that simply hit more often (usually the winner) otherwise looks slower than it is. Returns
    {team: {"raw", "act"}}; ump time is left alone."""
    T = s["teams"]
    ref_bat = (T[a]["bat"]["n"] + T[h]["bat"]["n"]) / 2
    ref_pitch = (T[a]["pitch"]["n"] + T[h]["pitch"]["n"]) / 2
    out = {}
    for t in (a, h):
        out[t] = {k: T[t]["bat"]["avg_" + k] * ref_bat + T[t]["pitch"]["avg_" + k] * ref_pitch for k in ("raw", "act")}
    return out


PA_COLUMNS = ["season", "game_code", "matchup", "pa_seq", "inning", "half", "outs", "runners", "score_away", "score_home",
              "batting_team", "fielding_team", "batter", "batter_pos", "pitcher", "ump", "pinch_hit_swaps",
              "pitch", "swing", "diff", "result", "auto", "swing_posted",
              "ping_central", "swing_central", "result_central",
              "hit_raw_min", "hit_active_min", "ump_review_raw_min", "ump_review_active_min",
              "pitch_gap_raw_min", "pitch_gap_active_min", "ump_relay_est_raw_min", "ump_relay_est_active_min",
              "pitching_raw_min", "pitching_active_min", "pa_total_raw_min", "ump_handled", "no_ping", "original_batter", "result_corrected"]


def _min(x: float | None) -> float | str:
    return "" if x is None else round(x / 60.0, 2)


def pa_rows(pas: list[dict], game: dict) -> list[dict]:
    """One row per timed PA, allocated like analyze(): hitting = ping -> swing (or ping -> result minus the ump's
    median turnaround when there is no swing post), ump review = swing -> result, pitching = the gap before this
    PA's ping minus an estimated ump relay. pa_seq is the PA's order within the game."""
    turns = [_sec(p["swing"], p["result"]) for p in pas if p["swing"] and p["result"]]
    t_raw = statistics.median(r for r, _ in turns) if turns else 0.0
    t_act = statistics.median(a for _, a in turns) if turns else 0.0
    teams = sorted({p["team"] for p in pas})
    rows = []
    for i, p in enumerate(pas):
        if not p["result"]:
            continue
        fielding = next((o for o in teams if o != p["team"]), "")
        noping = bool(p.get("noping"))
        total = None if noping else _sec(p["ping"], p["result"])
        hit = review = None
        if noping:
            pass
        elif p["swing"]:
            hit, review = _sec(p["ping"], p["swing"]), _sec(p["swing"], p["result"])
        elif auto_side(p) == "bat":
            hit, review = total, (0.0, 0.0)
        else:
            ur, ua = min(total[0], t_raw), min(total[1], t_act)
            hit, review = (total[0] - ur, total[1] - ua), (ur, ua)
        gap = relay = pitching = None
        since = pas[i - 1]["result"] if i > 0 else p.get("opened")
        if since and noping:
            gap = pitching = _sec(since, p["result"])   # no ping: the whole stretch is the pitching side's
        elif since:
            gap = _sec(since, p["ping"])
            relay = (min(gap[0], t_raw), min(gap[1], t_act))
            pitching = (gap[0] - relay[0], gap[1] - relay[1])
        runners = ",".join(b for b, k in (("1B", "runner_1b"), ("2B", "runner_2b"), ("3B", "runner_3b")) if p.get(k)) or "empty"
        diff = ""
        if p["pitch"] is not None and p["swing_value"] is not None and 1 <= p["swing_value"] <= 1000:
            d = abs(p["pitch"] - p["swing_value"])
            diff = min(d, 1000 - d)

        def local(ts):
            return _local_naive(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

        def pair(v):
            return (_min(v[0]), _min(v[1])) if v else ("", "")

        rows.append({
            "season": game.get("season"), "game_code": game["game_code"],
            "matchup": f"{game.get('away_team')} @ {game.get('home_team')}", "pa_seq": len(rows) + 1,
            "inning": p.get("inning", ""), "half": p.get("half", ""), "outs": p.get("outs", ""), "runners": runners,
            "score_away": p.get("away_score", ""), "score_home": p.get("home_score", ""),
            "batting_team": p["team"], "fielding_team": fielding, "batter": p.get("batter", ""),
            "batter_pos": p.get("batter_pos", ""), "pitcher": p.get("pitcher", ""), "ump": p["ump"] or "",
            "pinch_hit_swaps": p["subs"], "pitch": p["pitch"], "swing": p["swing_value"], "diff": diff,
            "result": p["outcome"], "auto": auto_side(p) or "", "swing_posted": bool(p["swing"]),
            "ping_central": local(p["ping"]), "swing_central": local(p["swing"]), "result_central": local(p["result"]),
            "hit_raw_min": pair(hit)[0], "hit_active_min": pair(hit)[1],
            "ump_review_raw_min": pair(review)[0], "ump_review_active_min": pair(review)[1],
            "pitch_gap_raw_min": pair(gap)[0], "pitch_gap_active_min": pair(gap)[1],
            "ump_relay_est_raw_min": pair(relay)[0], "ump_relay_est_active_min": pair(relay)[1],
            "pitching_raw_min": pair(pitching)[0], "pitching_active_min": pair(pitching)[1],
            "pa_total_raw_min": _min(total[0]) if total else "",
            "ump_handled": not noping and not p["swing"] and auto_side(p) != "bat", "no_ping": noping,
            "original_batter": p.get("first_batter") or "", "result_corrected": bool(p.get("corrected")),
        })
    return rows


def scenarios(pas: list[dict]) -> list[dict]:
    """The PAs whose timing rests on a judgement call (no swing post, a pinch-hit re-ping, a corrected result, a result with
    no ping), as {"inning", "half", "batter", "pitcher", "tags", "bat", "pitch", "ump"} with active seconds allocated
    the way analyze() allocates them."""
    out = []
    for r in pa_rows(pas, {"game_code": ""}):
        tags = [t for t, on in (("ump-handled", r["ump_handled"]), ("pinch-hit re-ping", r["pinch_hit_swaps"]),
                                ("result corrected", r["result_corrected"]), ("no ping", r["no_ping"])) if on]
        if not tags:
            continue
        sec = lambda *k: sum(float(r[x]) for x in k if r[x] != "") * 60.0
        batter = r["batter"]
        if r["original_batter"] and r["original_batter"] != batter:
            batter = f"{batter} (for {r['original_batter']})"
        out.append({"inning": r["inning"], "half": r["half"], "batter": "" if r["no_ping"] else batter,
                    "pitcher": r["pitcher"], "tags": ", ".join(tags), "bat": sec("hit_active_min"),
                    "pitch": sec("pitching_active_min"), "ump": sec("ump_review_active_min", "ump_relay_est_active_min")})
    return out


def half_innings(pas: list[dict], now: datetime | None = None) -> list[dict]:
    """One block per half inning for the inning rows: {"inning", "half", "team", "fielding", "segs", "act", "n"}.
    segs is the half's stretches in order as (kind, owner, active seconds): "wait" (the fielding team's pitching),
    "hit" (the batting team) and "ump", split like analyze() splits them."""
    teams = sorted({p["team"] for p in pas})
    turns = [_sec(p["swing"], p["result"])[1] for p in pas if p["swing"] and p["result"]]
    turn = statistics.median(turns) if turns else 0.0
    blocks: list[dict] = []
    for i, p in enumerate(pas):
        if not p["result"] and not (now and i == len(pas) - 1):
            continue
        fielding = next((o for o in teams if o != p["team"]), "")
        key = (p.get("inning"), p.get("half"))
        if not blocks or (blocks[-1]["inning"], blocks[-1]["half"]) != key:
            blocks.append({"inning": key[0], "half": key[1], "team": p["team"], "fielding": fielding,
                           "segs": [], "act": 0.0, "n": 0})
        b = blocks[-1]
        since = pas[i - 1]["result"] if i > 0 else p.get("opened")
        segs = []
        own_from = 0
        if p.get("noping"):
            if since:
                segs.append(("wait", fielding, _sec(since, p["result"])[1]))   # all of it the pitching side's
        else:
            if since:
                gap = _sec(since, p["ping"])[1]
                relay = min(gap, turn)
                segs += [("wait", fielding, gap - relay), ("ump", "", relay)]
            own_from = len(segs)      # the PA's own stretch (ping -> result) starts here; an auto is marked over it
        if p.get("noping") or not p["result"]:
            pass   # the open last PA: only the finished wait before its ping; the open stretch is added below
        elif p["swing"]:
            segs += [("hit", p["team"], _sec(p["ping"], p["swing"])[1]), ("ump", "", _sec(p["swing"], p["result"])[1])]
        elif auto_side(p) == "bat":
            segs.append(("hit", p["team"], _sec(p["ping"], p["result"])[1]))
        else:
            total = _sec(p["ping"], p["result"])[1]
            segs += [("hit", p["team"], total - min(total, turn)), ("ump", "", min(total, turn))]
        off0 = b["act"] + sum(sec for _, _, sec in segs[:own_from])
        for kind, owner, sec in segs:
            if b["segs"] and b["segs"][-1][0] == kind and b["segs"][-1][1] == owner:
                b["segs"][-1] = (kind, owner, b["segs"][-1][2] + sec)
            else:
                b["segs"].append((kind, owner, sec))
            b["act"] += sec
        b["n"] += 1 if p["result"] else 0
        if p["result"] and auto_side(p):
            b.setdefault("autos", []).append((off0, b["act"], auto_side(p)))
    op = open_stretch(pas, now)
    if op:
        # the open stretch closes the last half shown, unless the last result ended the half: then the wait belongs to
        # the next half (as a finished wait would), so that half gets its own row
        last = pas[-1]
        if blocks and op["flip"] and last.get("inning") is not None:
            top = last.get("half") == "top"
            blocks.append({"inning": last["inning"] + (0 if top else 1), "half": "bottom" if top else "top",
                           "team": next((t for t in teams if t != op["owner"]), ""), "fielding": op["owner"],
                           "segs": [], "act": 0.0, "n": 0})
        if blocks:
            b = blocks[-1]
            parts = [(op["kind"], op["owner"], op["act"])]
            if op["kind"] == "wait":   # it will end up split like a finished wait: ump relay first, then the pitching side
                relay = min(op["act"], turn)
                parts = [("ump", "", relay), ("wait", op["owner"], op["act"] - relay)]
            for kind, owner, sec in parts:
                b["segs"].append(("open_" + kind, owner, sec))
                b["act"] += sec
    return blocks

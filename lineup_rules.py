"""Defensive-lineup legality per the MLN rulebook (Section 8 and the pinch-hitter notes).

Eligibility (Section 8): an infielder (1B/2B/3B/SS) has its primary position plus a secondary of Utility Infield
(UTIL / INF = any infield spot) or Catcher (C); a catcher's secondary is Corner IF (CIF / CINF = 1B, 3B) or Corner OF
(COF = LF, RF); outfielders play any outfield spot; pitchers only pitch. Anyone may DH. Once substitutions start the
"5 players in their primary positions" start-of-game rule no longer applies, so only eligibility is checked here.

A lineup is nine players: eight fielders at C/1B/2B/3B/SS/LF/CF/RF plus the DH. Position players already in the
lineup cannot be moved to the DH (3.c), so the DH slot may only be filled by the current DH or a player who has just
entered; pass those names as `dh_ok`. This module is pure (no I/O) so the Streamlit app, the Discord bot and the
offense lab can share it.
"""
from __future__ import annotations

INFIELD = ("1B", "2B", "3B", "SS")
OUTFIELD = ("LF", "CF", "RF")
FIELD_POSITIONS = ("C",) + INFIELD + OUTFIELD
DEFENSE = FIELD_POSITIONS


def can_play(prim: str | None, sec: str | None, pos: str) -> bool:
    """True if a player with this primary/secondary position may play `pos` in the field (not DH)."""
    prim = (prim or "").strip().upper()
    sec = (sec or "").strip().upper()
    if prim == pos:
        return True
    if prim in INFIELD:
        if sec in ("UTIL", "INF") and pos in INFIELD:
            return True
        if sec == "C" and pos == "C":
            return True
    if prim == "C":
        if sec in ("CIF", "CINF") and pos in ("1B", "3B"):
            return True
        if sec == "COF" and pos in ("LF", "RF"):
            return True
    if prim in OUTFIELD and pos in OUTFIELD:
        return True
    return False


def legal_assignments(players: list[dict], dh_ok: set[str] | None = None, limit: int | None = None):
    """Yield every legal {position: player name} assignment for nine players (dicts with name / prim / sec).

    The ninth position is "DH". dh_ok restricts who may fill it (None = anyone). `limit` stops early."""
    n = len(players)
    if n != 9:
        return
    positions = list(FIELD_POSITIONS) + ["DH"]
    count = 0

    def rec(pi: int, used: int, cur: dict):
        nonlocal count
        if limit is not None and count >= limit:
            return
        if pi == len(positions):
            count += 1
            yield dict(cur)
            return
        pos = positions[pi]
        for i, p in enumerate(players):
            if used >> i & 1:
                continue
            if pos == "DH":
                if dh_ok is not None and p["name"] not in dh_ok:
                    continue
            elif not can_play(p.get("prim"), p.get("sec"), pos):
                continue
            cur[pos] = p["name"]
            yield from rec(pi + 1, used | (1 << i), cur)
            del cur[pos]

    yield from rec(0, 0, {})


def is_legal(players: list[dict], dh_ok: set[str] | None = None) -> bool:
    return len(players) == 9 and _fill(players, list(FIELD_POSITIONS) + ["DH"], dh_ok)


def possible_at(players: list[dict], pos: str, dh_ok: set[str] | None = None) -> set[str]:
    """Names of players who stand at `pos` in at least one legal assignment of the nine.

    Forces each candidate to `pos` and asks whether the other eight can still fill the remaining slots (a first-hit
    search, so it stays fast - unlike enumerating every full assignment)."""
    out: set[str] = set()
    for p in players:
        if pos == "DH":
            if dh_ok is not None and p["name"] not in dh_ok:
                continue
        elif not can_play(p.get("prim"), p.get("sec"), pos):
            continue
        rest = [q for q in players if q is not p]
        slots = [x for x in list(FIELD_POSITIONS) + ["DH"] if x != pos]
        if _fill(rest, slots, dh_ok):
            out.add(p["name"])
    return out


def _fill(players: list[dict], slots: list[str], dh_ok: set[str] | None) -> bool:
    """Backtracking existence check: can `players` fill every slot (one each)?"""
    if not slots:
        return True
    slot, rest_slots = slots[0], slots[1:]
    for i, p in enumerate(players):
        if slot == "DH":
            if dh_ok is not None and p["name"] not in dh_ok:
                continue
        elif not can_play(p.get("prim"), p.get("sec"), slot):
            continue
        if _fill(players[:i] + players[i + 1:], rest_slots, dh_ok):
            return True
    return False

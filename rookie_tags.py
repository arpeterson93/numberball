"""Rookie flags from the plays themselves, not from the Players sheet's own Rookie? column.

A player is a rookie in the first season they appear in the games (a batting or pitching line), and only that
season: anyone already seen in an earlier season is not, and a rostered player with no game yet is not either.
Sheets and Supabase are left alone - this only rewrites the `rookie` flag in the generated docs/data/**/players.json
(the live season and every archive season) that MLN Gameday reads, so the Rookies filters, the All-Time Rookies
lines and the career data all agree.

The first-appearance season comes from each season's meta.json `stats` (every session's batting and pitching lines),
which matches the play-by-play first season for every player id checked against the full plays table (catchers and
runners always have a line too).

Runs from career_build.py before it reads the rosters; `python rookie_tags.py` runs it on its own.
"""
from __future__ import annotations

import json
import os

DATA = os.path.join("docs", "data")


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _season_dirs() -> list[tuple[int, str]]:
    """(season, dir) for the live season plus every committed archive season, oldest first."""
    import re
    out = []
    live = os.path.join(DATA, "meta.json")
    if os.path.exists(live):
        out.append((int(_load(live)["season"]), DATA))
    for name in sorted(os.listdir(DATA)):
        m = re.fullmatch(r"s(\d{2})", name)
        if m and os.path.exists(os.path.join(DATA, name, "meta.json")):
            out.append((int(m.group(1)), os.path.join(DATA, name)))
    return sorted(out)


def debut_seasons() -> dict[int, int]:
    """player id -> the first season they have a batting or pitching line."""
    debut: dict[int, int] = {}
    for season, sdir in _season_dirs():
        stats = _load(os.path.join(sdir, "meta.json")).get("stats") or {}
        for sess in stats.values():
            for side in ("batting", "pitching"):
                for row in sess.get(side) or []:
                    pid = row.get("player_id")
                    if pid and season < debut.get(pid, season + 1):
                        debut[pid] = season
    return debut


def apply() -> dict[int, tuple[int, int]]:
    """Rewrite each season's players.json `rookie` flags; returns {season: (flag changes, rookies after)}."""
    debut = debut_seasons()
    summary: dict[int, tuple[int, int]] = {}
    for season, sdir in _season_dirs():
        path = os.path.join(sdir, "players.json")
        if not os.path.exists(path):
            continue
        roster = _load(path)
        changed = 0
        for p in roster:
            want = debut.get(p.get("id")) == season
            if bool(p.get("rookie")) != want:
                p["rookie"] = want
                changed += 1
        if changed:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(roster, fh, separators=(",", ":"), ensure_ascii=False)
                fh.write("\n")
        summary[season] = (changed, sum(1 for p in roster if p.get("rookie")))
    return summary


if __name__ == "__main__":
    for season, (changed, n) in sorted(apply().items()):
        print(f"  S{season:02d}: {changed} flag(s) changed, {n} rookies")

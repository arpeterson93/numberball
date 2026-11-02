"""Pace-of-play card from a DiscordChatExporter JSON export instead of the live results feed.

Any stadium channel works: the export only needs the ump bot's and Swing-E's posts (the same wording the Pioneers feed
carries). The game is matched from the teams in the feed, or pass --game to pin it.

  python -I tools/pace_from_export.py "<export>.json" out.png [--game 130922]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database
import live_sheet
import pace
import pioball_render
import swing_game


def _ts(s: str) -> datetime:
    """Exporter timestamps carry 2 or 3 fractional digits; fromisoformat on 3.10 wants 6."""
    return datetime.fromisoformat(re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], s))


def _text(m: dict) -> str:
    """Same text the bot builds from a live message: body plus every embed's title, description and fields."""
    parts = [m.get("content") or ""]
    for e in m.get("embeds") or []:
        parts += [e.get("title") or "", e.get("description") or ""]
        for f in e.get("fields") or []:
            parts += [f.get("name") or "", f.get("value") or ""]
    return "\n".join(p for p in parts if p)


def load_messages(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    return sorted(({"ts": _ts(m["timestamp"]), "text": _text(m)} for m in d["messages"]), key=lambda m: m["ts"])


def find_game(teams: list[str], start: datetime, game_code: str | None) -> dict | None:
    """The newest game in the games table between the feed's two teams that began before the first message."""
    if game_code:
        return database.get_game_by_code(int(game_code))
    by_abbrev = live_sheet.teams()["by_abbrev"]
    want = {(by_abbrev.get(t) or {}).get("full_team") for t in teams}
    rows = [g for g in database.get_games() if {g.get("home_team"), g.get("away_team")} == want
            and (pace.parse_ts(g.get("start_time")) or start) <= start]
    return max(rows, key=lambda g: (g.get("season") or 0, g["game_code"]), default=None)


def main() -> None:
    args = sys.argv[1:]
    game_code = args[args.index("--game") + 1] if "--game" in args else None
    path, out = args[0], args[1]
    msgs = load_messages(path)
    pas = pace.build_pas(msgs)
    s = pace.summarize(pace.analyze(pas))
    g = find_game(list(s["teams"]), msgs[0]["ts"], game_code)
    if not g:
        raise SystemExit(f"No game found for teams {list(s['teams'])}; pass --game <code>.")
    looks = swing_game.team_looks([g.get("away_team"), g.get("home_team")], g.get("season"))
    sides = [{"abbrev": x["abbrev"], "color": pioball_render._hex(x["hex"])}
             for x in pace.game_sides(g, looks, list(s["teams"]))]
    title = f"Pace of play - {g.get('away_team')} @ {g.get('home_team')}"
    sub = (f"S{g.get('season')} | game {g['game_code']} | {s['n_pa']} plate appearances | {pace.fmt_dur(s['total_raw'])} "
           f"on the clock, {pace.fmt_dur(s['total_act'])} active")
    pathlib.Path(out).write_bytes(pioball_render.render_pace(s, title, sub, sides[0], sides[1], pace.half_innings(pas),
                                                                    scenarios=pace.scenarios(pas)).getvalue())
    print(f"game {g['game_code']}: {s['n_pa']} PAs -> {out}")


if __name__ == "__main__":
    main()

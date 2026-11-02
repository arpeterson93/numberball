"""League-wide pace of play from a folder of DiscordChatExporter stadium logs.

  python -I tools/pace_league.py <folder of .json exports> [out folder] [--refresh] [--only 130919,130920] [--no-open]

Each .json is one stadium channel, filtered to the ump bot's and Swing-E's posts. For every game with play timestamps
(S11 onward) the game's window is cut out of the channel: from the last result of the previous game in that channel to
the latest play timestamp of this game (plus a minute: sheet times are truncated to the minute). The play timestamps
come from every play row in the game (min and max, so one out-of-order row can't shorten it), and the channel is picked
by which one holds PAs for this game's two teams. Writes, under the out folder (default tools/probe_output/pace_league):

  games/S13/130919.png            one card per game, inning timeline included
  teams/S13/<Team>.png            each team's season: pooled, with one adjusted row per game above
  teams_all/<Team>.png            each team across every season, one adjusted row per season above
  pa_detail.csv                   every timed plate appearance (pace.PA_COLUMNS) for deeper analysis
  games.csv / unmatched.csv       what was found per game, and the games no channel had PAs for

Reads the plays' timestamps from tools/probe_output (plays_sheet_ts.pkl from pace_activity_curve.py, plays_raw.pkl from
local_plays.py fetch) and the games / teams tables from Supabase once (cached in games_cache.pkl / teams_cache.pkl;
--refresh re-reads them). A game with no end time and no winner in the games table is treated as still running: its window runs to
the channel's last message and the stretch open then is drawn striped on the card (--no-open drops it). Nothing here changes the bot or its images.
"""
from __future__ import annotations

import bisect
import csv
import json
import pathlib
import pickle
import re
import sys
from datetime import datetime, timedelta

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pace
import pioball_render

PROBE = ROOT / "tools" / "probe_output"
OUTLIER_DAYS = 30       # games run up to ~16 days (median 9), so only stamps a month off the game's median count as bad data
START_FLOOR = pd.Timedelta(days=7)   # a game's window never starts more than this before its first play
MIN_PAS = 5                      # a channel must hold at least this many of the game's PAs to count
END_PAD = timedelta(seconds=60)  # sheet timestamps are truncated to the minute


# ------------------------------------------------------------------ inputs

def _ts(s: str) -> datetime:
    """Exporter timestamps carry 2 or 3 fractional digits; fromisoformat on 3.10 wants 6."""
    return datetime.fromisoformat(re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], s))


def _text(m: dict) -> str:
    parts = [m.get("content") or ""]
    for e in m.get("embeds") or []:
        parts += [e.get("title") or "", e.get("description") or ""]
        for f in e.get("fields") or []:
            parts += [f.get("name") or "", f.get("value") or ""]
    return "\n".join(p for p in parts if p)


def load_channels(folder: pathlib.Path) -> dict[str, dict]:
    """{channel name: {"msgs": [{"ts", "text"}] oldest first, "t": [epoch seconds]}}. A name seen in several files is merged."""
    chans: dict[str, list] = {}
    for f in sorted(folder.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        name = d.get("channel", {}).get("name") or f.stem
        chans.setdefault(name, []).extend({"ts": _ts(m["timestamp"]), "text": _text(m)} for m in d["messages"])
        print(f"  {f.name}: {len(d['messages'])} messages -> {name}")
    out = {}
    for name, msgs in chans.items():
        uniq = {(m["ts"], m["text"]): m for m in msgs}.values()          # overlapping exports repeat messages
        msgs = sorted(uniq, key=lambda m: m["ts"])
        out[name] = {"msgs": msgs, "t": [m["ts"].timestamp() for m in msgs]}
    return out


def play_windows() -> pd.DataFrame:
    """One row per game with play timestamps: first / last play time (aware, Central) and the teams as the sheet writes them."""
    sheet = pd.read_pickle(PROBE / "plays_sheet_ts.pkl")
    sheet["ts"] = pd.to_datetime(sheet["Timestamp*"].where(sheet["Timestamp*"] != "0"), format="mixed", errors="coerce")
    sheet["season"] = pd.to_numeric(sheet["Season"], errors="coerce")
    a = sheet[sheet.season.isin([11, 12])][["season", "Game", "Away", "Home", "ts"]]
    a.columns = ["season", "game", "away", "home", "ts"]
    p = pd.DataFrame(pickle.load(open(PROBE / "plays_raw.pkl", "rb"))["plays"])
    p = p[p.season == 13]
    b = pd.DataFrame({"season": 13, "game": p["game_code"], "away": p["away"], "home": p["home"],
                      "ts": pd.to_datetime(p["timestamp"], format="mixed", errors="coerce")})
    df = pd.concat([a, b], ignore_index=True).dropna(subset=["ts"])
    df["game"] = df["game"].astype(str)
    df["ts"] = df["ts"].dt.tz_localize(pace.CENTRAL, ambiguous=False, nonexistent="shift_forward")
    g = df.groupby("game").agg(season=("season", "first"), raw_lo=("ts", "min"), raw_hi=("ts", "max"), n_plays=("ts", "size"),
                               away=("away", lambda s: s.mode().iat[0]), home=("home", lambda s: s.mode().iat[0]))
    # A few rows are re-stamped days or weeks off (edits / corrections): game 110104 has 81 plays over 14 days. Bound the
    # game by the min / max of the plays within OUTLIER_DAYS of the game's median stamp; the raw min / max stay in games.csv.
    med = df.groupby("game")["ts"].median()
    keep = df[(df["ts"] - df["game"].map(med)).abs() <= pd.Timedelta(days=OUTLIER_DAYS)]
    tr = keep.groupby("game")["ts"].agg(lo="min", hi="max")
    g = g.join(tr)
    g["n_outliers"] = g.n_plays - keep.groupby("game").size().reindex(g.index).fillna(0).astype(int)
    return g.sort_values("lo")


def _cached(name: str, refresh: bool, fetch):
    path = PROBE / name
    if refresh or not path.exists():
        pickle.dump(fetch(), open(path, "wb"))
    return pickle.load(open(path, "rb"))


def games_table(refresh: bool) -> dict[str, dict]:
    import database
    rows = _cached("games_cache.pkl", refresh, database.get_games)
    return {str(g["game_code"]): g for g in rows}


def team_rows(refresh: bool) -> list[dict]:
    import database
    return _cached("teams_cache.pkl", refresh, lambda: database._client().table("teams")
                   .select("team_id,full_team,name,team_name,abbrev,primary_hex,season,league,ballpark_channel_id").execute().data)


def make_looks(rows: list[dict]):
    """Same lookup as swing_game.team_looks (the given season's row wins, else the newest), without a query per game."""
    by_team: dict[str, list[dict]] = {}
    for r in rows:
        # MLN rows carry full_team; Rookie League rows only name / team_name, and the games table writes their nickname
        for key in {r.get("full_team"), r.get("name") if not r.get("full_team") else None,
                    r.get("team_name") if not r.get("full_team") else None} - {None}:
            by_team.setdefault(key, []).append(r)

    def looks(names: list[str], season: int | None) -> dict[str, dict]:
        out = {}
        for n in names:
            rs = sorted(by_team.get(n, []), key=lambda r: -(r.get("season") or 0))
            pick = next((r for r in rs if season is not None and r.get("season") == season), rs[0] if rs else None)
            if pick:
                out[n] = pick
        return out
    return looks


# ------------------------------------------------------------------ matching

def _game_run(pas: list[dict], abbrevs: set[str]) -> list[dict]:
    """The longest stretch of consecutive PAs batted only by these two teams. A stadium channel also carries the home
    team's other games, so "a PA by one of the two" is not enough: any other team batting ends the run."""
    runs, cur = [], []
    for p in pas:
        if p["team"] in abbrevs:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    runs = [r for r in runs if {p["team"] for p in r} == abbrevs]
    return max(runs, key=lambda r: sum(1 for p in r if p["result"]), default=[])


def cut_game(chans: dict, last_hi: dict, row, abbrevs: set[str], live: bool = False):
    """The channel whose messages between its previous game and this game's last play hold the most PAs for these two
    teams. Returns (channel, those PAs, {channel: PA count}, now) - channel None when none reaches MIN_PAS.
    live (a game with no end time): the window runs to the channel's last message, the PA still waiting on a result is
    kept, and `now` is that last message (the open stretch is measured up to it); otherwise now is None."""
    hi = float("inf") if live else (row.hi + END_PAD).timestamp()
    best, best_pas, counts, best_now = None, [], {}, None
    for name, ch in chans.items():
        a = bisect.bisect_right(ch["t"], max(last_hi.get(name, float("-inf")), (row.lo - START_FLOOR).timestamp()))
        b = bisect.bisect_right(ch["t"], hi)
        if b - a < 2 * MIN_PAS:
            continue
        built = _game_run(pace.build_pas(ch["msgs"][a:b]), abbrevs)
        pas = [p for p in built if p["result"]]
        counts[name] = len(pas)
        if len(pas) >= MIN_PAS and len(pas) > len(best_pas) and {p["team"] for p in pas} == abbrevs:
            best, best_pas = name, built if live else pas
            best_now = ch["msgs"][b - 1]["ts"] if live else None
    return best, best_pas, counts, best_now


# ------------------------------------------------------------------ cards

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def _ump_act(s: dict) -> float:
    return sum(u["review"]["act"] + u["relay"]["act"] for u in s["umps"].values())


def _png(buf, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def team_cards(team: str, recs: list[dict], universe: dict, out: pathlib.Path) -> int:
    """Season cards for one team (one per season it has games in) plus the all-seasons card. Returns images written."""
    from key_moments_build import SECONDARY_HEX
    n_img = 0
    by_season: dict[int, list[dict]] = {}
    for r in sorted(recs, key=lambda r: r["lo"]):
        by_season.setdefault(r["season"], []).append(r)

    def side(r):  # (ours, opp) as {"abbrev", "hex"} for this game
        a, h = r["sides"]
        return (a, h) if r["g"]["away_team"] == team else (h, a)

    def pooled(rs: list[dict], ours_key: str) -> dict:
        return pace.summarize(pace.merge_results([(r["res"], side(r)[0]["abbrev"]) for r in rs], ours_key, "OPP"))

    latest = max(recs, key=lambda r: r["lo"])
    ours_key = side(latest)[0]["abbrev"]
    a_hex = side(latest)[0]["hex"]
    o_hex = SECONDARY_HEX.get(ours_key) or "8a8f98"
    a_side = {"abbrev": ours_key, "color": pioball_render._hex(a_hex)}
    o_side = {"abbrev": "OPP", "color": pioball_render._hex(o_hex)}

    def sub(rs, s, n_all):
        return (f"{len(rs)} of {n_all} games | {s['n_pa']} plate appearances | {pace.fmt_dur(s['total_raw'])} "
                f"on the clock, {pace.fmt_dur(s['total_act'])} active")

    for season, rs in by_season.items():
        s = pooled(rs, ours_key)
        trend = []
        for i, r in enumerate(rs, 1):
            mine, opp = side(r)
            if mine["abbrev"] in r["s"]["teams"] and opp["abbrev"] in r["s"]["teams"]:
                adj = pace.adjust_for_volume(r["s"], mine["abbrev"], opp["abbrev"])
                trend.append((str(i), opp["abbrev"], adj[mine["abbrev"]]["act"], _ump_act(r["s"]), adj[opp["abbrev"]]["act"], opp["hex"]))
        n_all = universe.get((team, season), len(rs))
        buf = pioball_render.render_pace(s, f"Pace of play - {team}, Season {season}", f"S{season} | " + sub(rs, s, n_all),
                                         a_side, o_side, None, trend)
        _png(buf, out / "teams" / f"S{season}" / f"{_safe(team)}.png")
        n_img += 1

    all_recs = [r for rs in by_season.values() for r in rs]
    s = pooled(all_recs, ours_key)
    trend = []
    for season, rs in by_season.items():
        ss = pooled(rs, ours_key)
        if ours_key in ss["teams"] and "OPP" in ss["teams"]:
            adj = pace.adjust_for_volume(ss, ours_key, "OPP")
            trend.append((f"S{season}", "OPP", adj[ours_key]["act"], _ump_act(ss), adj["OPP"]["act"], None))
    seasons = sorted(by_season)
    n_all = sum(universe.get((team, y), len(by_season[y])) for y in seasons)
    buf = pioball_render.render_pace(s, f"Pace of play - {team}, all seasons",
                                     f"S{seasons[0]}-S{seasons[-1]} | " + sub(all_recs, s, n_all), a_side, o_side, None,
                                     trend, "TOTAL GAME TIME, PA/BF ADJ, BY SEASON")
    _png(buf, out / "teams_all" / f"{_safe(team)}.png")
    return n_img + 1


# ------------------------------------------------------------------ main

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # channel names carry emoji; -I ignores PYTHONIOENCODING
    argv = sys.argv[1:]
    refresh = "--refresh" in argv
    use_open = "--no-open" not in argv      # unfinished games: count the stretch running when the export ends
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        only = set(argv[i + 1].split(","))
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    folder = pathlib.Path(args[0])
    out = pathlib.Path(args[1]) if len(args) > 1 else PROBE / "pace_league"
    out.mkdir(parents=True, exist_ok=True)

    print("Loading channels:")
    chans = load_channels(folder)
    wins = play_windows()
    gt = games_table(refresh)
    trows = team_rows(refresh)
    looks = make_looks(trows)
    id_name = {(r["team_id"], int(r["season"])): r.get("full_team") or r.get("name") for r in trows
               if r.get("team_id") and r.get("season") == r.get("season") and r.get("season") is not None}
    from key_moments_build import SECONDARY_HEX  # noqa: F401  (imported early so a missing module fails before the long run)

    universe: dict[tuple[str, int], int] = {}      # (team, season) -> games with play timestamps, for "N of M games"
    for code, w in wins.iterrows():
        g = gt.get(code)
        if g:
            for t in (g["away_team"], g["home_team"]):
                universe[(t, int(w.season))] = universe.get((t, int(w.season)), 0) + 1

    last_hi: dict[str, float] = {}
    recs, game_rows, pa_all, unmatched = [], [], [], []
    todo = wins if not only else wins[wins.index.isin(only)]
    print(f"\n{len(todo)} games with play timestamps; matching against {len(chans)} channels")
    for code, w in todo.iterrows():
        g = gt.get(code)
        if not g:
            unmatched.append({"game_code": code, "reason": "not in games table (plays with no game number, e.g. scrimmages)"})
            continue
        if not g.get("away_team") or not g.get("home_team"):     # a few games rows have no team names: use the plays' team ids
            g = dict(g, away_team=id_name.get((w.away, int(w.season))), home_team=id_name.get((w.home, int(w.season))))
            if not g["away_team"] or not g["home_team"]:
                unmatched.append({"game_code": code, "season": int(w.season), "reason": "no team names and ids unknown"})
                continue
        lk = looks([g["away_team"], g["home_team"]], g.get("season"))      # plays carry team ids; the feed writes abbrevs
        abbrevs = {(lk.get(g["away_team"]) or {}).get("abbrev"), (lk.get(g["home_team"]) or {}).get("abbrev")}
        if None in abbrevs or len(abbrevs) < 2:
            unmatched.append({"game_code": code, "season": int(w.season), "reason": "team abbrev missing in teams table"})
            continue
        live = use_open and pace.is_live(g)
        chan, pas, counts, now = cut_game(chans, last_hi, w, abbrevs, live)
        if not chan:
            unmatched.append({"game_code": code, "season": int(w.season), "teams": "/".join(sorted(abbrevs)),
                              "reason": "no channel with PAs for both teams", "pas_by_channel": json.dumps(counts)})
            continue
        last_hi[chan] = float("inf") if live else (w.hi + END_PAD).timestamp()
        s = pace.summarize(pace.analyze(pas, now))
        if s["n_pa"] < 2:
            unmatched.append({"game_code": code, "season": int(w.season), "reason": "fewer than 2 timed PAs"})
            continue
        sides = pace.game_sides(g, lk, list(s["teams"]))
        title = f"Pace of play - {g['away_team']} @ {g['home_team']}"
        sub_t = (f"S{g['season']} | game {code} | {s['n_pa']} plate appearances | {pace.fmt_dur(s['total_raw'])} "
                 f"on the clock, {pace.fmt_dur(s['total_act'])} active{pace.open_note(s)}")
        card_sides = [{"abbrev": x["abbrev"], "color": pioball_render._hex(x["hex"])} for x in sides]
        _png(pioball_render.render_pace(s, title, sub_t, card_sides[0], card_sides[1], pace.half_innings(pas, now),
                                       scenarios=pace.scenarios(pas)),
             out / "games" / f"S{g['season']}" / f"{code}.png")
        pa_all += pace.pa_rows(pas, g)
        recs.append({"g": g, "season": int(w.season), "lo": w.lo, "res": pace.analyze(pas, now), "s": s, "sides": sides})
        game_rows.append({"game_code": code, "season": int(w.season), "session": g.get("session_number"),
                           "away_team": g["away_team"], "home_team": g["home_team"], "channel": chan,
                           "n_pa": s["n_pa"], "n_plays": int(w.n_plays), "first_play": w.lo.isoformat(), "first_play_raw": w.raw_lo.isoformat(), "last_play_raw": w.raw_hi.isoformat(), "outlier_plays": int(w.n_outliers),
                           "last_play": w.hi.isoformat(), "raw_hours": round(s["total_raw"] / 3600, 2),
                           "active_hours": round(s["total_act"] / 3600, 2)})
        print(f"  {code}  {g['away_team']} @ {g['home_team']}  {s['n_pa']} PAs  [{chan}]")

    n_img = len(recs)
    by_team: dict[str, list[dict]] = {}
    for r in recs:
        for t in (r["g"]["away_team"], r["g"]["home_team"]):
            by_team.setdefault(t, []).append(r)
    for t, rs in by_team.items():
        n_img += team_cards(t, rs, universe, out)

    def write(name: str, rows: list[dict], cols: list[str] | None = None) -> None:
        cols = cols or sorted({k for r in rows for k in r})
        with open(out / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    write("pa_detail.csv", pa_all, pace.PA_COLUMNS)
    write("games.csv", game_rows)
    write("unmatched.csv", unmatched)
    print(f"\n{len(recs)} games matched, {len(unmatched)} not, {len(pa_all)} PAs, {n_img} images -> {out}")


if __name__ == "__main__":
    main()

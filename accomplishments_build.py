"""Player accomplishments for the Awards tab: single-game feats and season league-leader titles.

Runs AFTER career_build.py (the CI workflow does both), reads only built JSON under docs/data, needs no network.

    python accomplishments_build.py

Writes:
    docs/data/accomplishments.json      the league-wide list, one row per accomplishment instance
    docs/data/career/p/<id>.json        patched in place with "acc": [[key, season, game|"", detail, opponent, [play numbers to replay]], ...]

Game feats come from tools/accomplishment_frequency.py (see its docstring for every definition); season titles are
the league-wide leader (ties all credited) of each completed season in HR, RBI, H, SB, pitching K, DPA (lowest),
DBF (highest), batting WPA, pitching WPA, ER6 (earned runs per 6 innings, lowest) and WHIP (lowest). DPA, DBF, ER6 and
WHIP need the qualifiers in tools/season_milestones.py (hitters 2.5 PA per team game, pitchers 1 IP per team game).
The live season has no titles until it finishes (a season is complete once the next one has plays).
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import accomplishment_frequency as af  # noqa: E402
import season_milestones as sm  # noqa: E402

DATA = os.path.join(ROOT, "docs", "data")

# (key, side, stat, higher is better, needs qualifier)
LEADERS = [
    ("lead_hr", "b", "hr", True, False), ("lead_rbi", "b", "rbi", True, False), ("lead_h", "b", "h", True, False),
    ("lead_sb", "b", "sb", True, False), ("lead_k", "p", "k", True, False),
    ("lead_dpa", "b", "dpa", False, True), ("lead_dbf", "p", "dbf", True, True),
    ("lead_wpa_bat", "b", "wpa", True, False), ("lead_wpa_pit", "p", "wpa", True, False),
    ("lead_er6", "p", "er6", False, True), ("lead_whip", "p", "whip", False, True),
]
LABEL = {"hr": "HR", "rbi": "RBI", "h": "H", "sb": "SB", "k": "K", "dpa": "DPA", "dbf": "DBF", "wpa": "WPA", "er6": "ER6", "whip": "WHIP"}
VALUE_FMT = {"dpa": "{:.1f}", "dbf": "{:.1f}", "wpa": "{:.1f}", "er6": "{:.2f}", "whip": "{:.2f}"}


def season_leaders(done_seasons=None):
    """-> [(key, player_id, season, "", detail)] for completed seasons, same tuple shape as the game feats."""
    totals, _awards, _names = sm.load()
    rows = sm.season_rows(totals)
    live = max(rows)
    team_games = {s: max((d["g"] for _, _, d in rows[s]["b"]), default=0) for s in rows}
    out = []
    for s in sorted(rows):
        if s == live:
            continue
        for key, side, stat, hib, qual in LEADERS:
            pool = []
            for pid, _n, d in rows[s][side]:
                v = d.get(stat)
                if v is None:
                    continue
                if qual and not (d["pa"] >= 2.5 * team_games[s] if side == "b" else d["outs"] >= 3 * team_games[s]):
                    continue
                if not qual and stat != "wpa" and not v:
                    continue
                pool.append((pid, v))
            if not pool:
                continue
            best = (max if hib else min)(v for _, v in pool)
            for pid, v in pool:
                if v == best:
                    out.append((key, int(pid), s, "", VALUE_FMT.get(stat, "{:g}").format(v) + " " + LABEL[stat], []))
    return out


EXCLUDE = {"game_winning_result", "multi_hr_3plus"}
BAT_GAME = {"multi_hr_2plus", "hits_4plus", "cycle", "reached_5plus", "golden_sombrero", "rbi_5plus", "sb_3plus"}
PIT_GAME = {"k_10plus", "cg_shutout", "no_hitter", "perfect_game"}
BAT_PLAY = {"grand_slam", "walk_off", "zero_diff_bat", "five_hundred_bat"}   # the hitter's side of the play
PIT_PLAY = {"struck_out_side", "immaculate_inning", "noble_tiger", "zero_diff_pit", "five_hundred_pit"}        # the pitcher's side


def stat_line(row, side):
    """The stat line Game honors shows (career.js awStatLine): a batter's H-AB plus whichever of HR / 3B / 2B / BB / RBI /
    SB / CS / K / DP are nonzero; a pitcher's IP, then H / ER / K / BB / DP when nonzero. A count of one is bare."""
    parts = []

    def cnt(n, label):
        if n > 0:
            parts.append(label if n == 1 else f"{n} {label}")

    g = lambda k: row.get(k, 0) or 0
    if side == "bat":
        parts.append(f"{g('h')}-{g('ab')}")
        for k, label in (("hr", "HR"), ("3b", "3B"), ("2b", "2B"), ("bb", "BB"), ("rbi", "RBI"), ("sb", "SB"), ("cs", "CS"),
                         ("k", "K"), ("gidp", "DP")):
            cnt(g(k), label)
    else:
        parts.append(f"{g('outs') // 3}.{g('outs') % 3} IP")
        for k, label in (("h", "H"), ("er", "ER"), ("k", "K"), ("bb", "BB"), ("dp", "DP")):
            cnt(g(k), label)
    return ", ".join(parts)


def game_rows():
    """(player id, game code, 'bat' | 'pit') -> that player's game row from the career files."""
    out = {}
    for path in glob.glob(os.path.join(DATA, "career", "p", "*.json")):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for side in ("bat", "pit"):
            for r in d.get(side, []):
                out[(af.pid(d["id"]), r["g"], side)] = r
    return out


_SUFFIX = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _last_names():
    """player id -> last name, from the roster file (current players); retired players fall back to the play row's full name."""
    try:
        with open(os.path.join(DATA, "players.json"), encoding="utf-8") as fh:
            return {p["id"]: p.get("last_name") for p in json.load(fh) if p.get("id")}
    except (OSError, ValueError):
        return {}


def _last_of(full):
    """'Ronald Eagle V' -> 'Eagle': the last word that is not a generational suffix."""
    words = [w for w in (full or "").split() if w.lower() not in _SUFFIX]
    return words[-1] if words else ""


def season_teams():
    """(player id, season) -> the team they played the most games for that season, from the career files."""
    seen = {}
    for path in glob.glob(os.path.join(DATA, "career", "p", "*.json")):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for side in ("bat", "pit"):
            for r in d.get(side, []):
                if r.get("t"):
                    c = seen.setdefault((af.pid(d["id"]), r["s"]), {})
                    c[r["t"]] = c.get(r["t"], 0) + 1
    return {k: max(v, key=v.get) for k, v in seen.items()}


def enrich(rows, by_game):
    """(key, id, season, game, detail, pns) -> same shape with the replay plays and the opponent filled in.

    pns are play numbers the replay can open: a game feat gets every play the player took part in that game (batting
    or running for a hitter, pitching for a pitcher); a one-act accomplishment gets just its own play(s). A one-act
    detail reads like "▼6 · 1B": a triangle for top (▲) or bottom (▼) of the inning, the inning, the result.
    """
    out = []
    grows = game_rows()
    lasts = _last_names()
    for key, i, season, game, detail, pns in rows:
        gp = {p["play_num"]: p for p in by_game[game]}
        hitter = key in BAT_GAME or key in BAT_PLAY
        if key in BAT_GAME:
            pns = [p["play_num"] for p in by_game[game] if af.pid(p["batter_id"]) == i or af.pid(p.get("runner_id")) == i]
        elif key in PIT_GAME:
            pns = [p["play_num"] for p in by_game[game] if af.pid(p["pitcher_id"]) == i]
        first = gp.get(pns[0]) if pns else None
        opp = ""
        if first:
            opp = first["def_team_abbr"] if hitter else first["off_team_abbr"]
        if key in BAT_PLAY or key in PIT_PLAY:
            tri = "▲" if first["half"] == "top" else "▼"
            detail = f"{tri}{first['inning']}"
            if key in ("walk_off", "zero_diff_bat", "zero_diff_pit", "five_hundred_bat", "five_hundred_pit"):
                detail += f" · {first['result']}"
            if key.startswith("zero"):
                detail += f" · pitch/swing {first['pitch']}"   # a 0 diff is the same number twice
            elif "diff" in key or key.startswith("five"):
                detail += f" · pitch {first['pitch']}, swing {first['swing']}"
            if key in ("grand_slam", "walk_off"):
                pl = lasts.get(first.get("pitcher_id")) or _last_of(first.get("pitcher_name"))
                if pl:
                    detail += f" · vs {pl}"
        elif key in BAT_GAME or key in PIT_GAME:
            side = "bat" if key in BAT_GAME else "pit"
            row = grows.get((i, game, side))
            detail = stat_line(row, side) if row else ""
        team = ""
        if first:
            team = first["off_team_abbr"] if hitter else first["def_team_abbr"]
        out.append((key, i, season, game, detail, opp, pns, team))
    return out


def build_rows():
    game, _plays, by_game, names = af.compute()
    # Left out on purpose: game_winning_result (about one per game, a career count rather than a badge) and the
    # 3-HR game (the multi-HR game already covers it). An immaculate inning combined across pitchers has no owner.
    rows = [r for r in game if r[1] is not None and r[0] not in EXCLUDE]
    rows = enrich(rows, by_game)
    st = season_teams()
    rows += [(k, i, s, g, d, "", [], st.get((i, s), "")) for k, i, s, g, d, _ in season_leaders()]
    names = dict(names)
    for k, v in sm.load()[2].items():
        names.setdefault(int(k), v)
    return rows, names


def main():
    rows, names = build_rows()
    rows.sort(key=lambda r: (r[0], r[2], str(r[3]), r[1]))   # (team, the 8th field, only rides along to accomplishments.json)
    with open(os.path.join(DATA, "accomplishments.json"), "w", encoding="utf-8") as fh:
        json.dump([[k, i, s, g, d, o, pn, t] for k, i, s, g, d, o, pn, t in rows], fh, separators=(",", ":"), ensure_ascii=False)
    by_player: dict[int, list] = {}
    for k, i, s, g, d, o, pn, _t in rows:
        by_player.setdefault(i, []).append([k, s, g, d, o, pn])
    patched = 0
    for pid, acc in by_player.items():
        path = os.path.join(DATA, "career", "p", f"{pid}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            player = json.load(fh)
        player["acc"] = sorted(acc, key=lambda a: (a[1], a[0]))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(player, fh, separators=(",", ":"), ensure_ascii=False)
            fh.write("\n")
        patched += 1
    print(f"{len(rows)} accomplishments for {len(by_player)} players; patched {patched} player files")


if __name__ == "__main__":
    main()

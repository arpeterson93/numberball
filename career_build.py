"""Derived career / records / search files for MLNGameday.com.

Runs AFTER key_moments_build.py (the CI workflow does both) and only reads the
JSON that build already wrote - every season's docs/data[/sNN]/plays_*.json,
meta.json and players.json - so it needs no network and no sheet access. It
reuses key_moments_build._session_stats for every number, so a career total,
a team's session line and the existing Stats tab can never disagree on what a
hit, an RBI or a game score is.

Writes (all under docs/data/):

    career/p/<player_id>.json   one player's whole-career game log (batting + pitching)
    career/totals.json          every player's per-season totals (Stats tab Career scope)
    career/psessions.json       every player-session line (Records tab, Player view; last two values are WPA and the best single-game Game Score)
    records.json                team-session lines (league = sum of teams)
    records_games.json          one line per game, both teams' stats combined (Records tab, Game view)
    search/sNN.json             slim column-oriented play index per season (Search tab)

Run from the repo root:

    python career_build.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict

import diff_metrics as dm
import key_moments_build as kmb
import utils

DATA = os.path.join("docs", "data")

BAT_KEYS = ["pa", "ab", "r", "h", "2b", "3b", "hr", "rbi", "bb", "k", "sb", "cs", "gidp", "lob", "sf", "ds", "dn"]
PIT_KEYS = ["outs", "h", "hr", "bb", "k", "dp", "er", "lob", "ds", "dn"]
BAT_COUNT = BAT_KEYS  # a batting game row is kept only when one of these (or wpa) is nonzero

def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Every abbreviation a franchise has used (rebrands and relocations), oldest first - the Search / All time team
# filter treats each group as one team and shows its current name. Same history as analyze_player_movement.py.
FRANCHISES = [
    ["ACP"], ["GHG"], ["MAL", "OGO", "RLY"], ["POR"], ["CIN", "ASS"], ["HMH"], ["REK", "OO"], ["SMD"],
    ["LPJ", "BBG", "BBEG"], ["VAN", "HFX"], ["SUN"], ["GRZ"], ["KYM", "CAR"], ["OAK", "SHH", "BUF", "KC"],
    ["BFB", "MIA"], ["NO", "RC"],
]


def season_dirs() -> list[tuple[int, str]]:
    """(season, dir) for the live season plus every committed archive season."""
    out = []
    live_meta = os.path.join(DATA, "meta.json")
    if os.path.exists(live_meta):
        out.append((int(_load(live_meta)["season"]), DATA))
    for name in sorted(os.listdir(DATA)):
        m = re.fullmatch(r"s(\d{2})", name)
        if m and os.path.exists(os.path.join(DATA, name, "meta.json")):
            out.append((int(m.group(1)), os.path.join(DATA, name)))
    return sorted(out)


def post_sessions(meta: dict) -> set[int]:
    """Playoff sessions of one season: the build's own post_sessions (from the
    Games tab's Type column) when present, else the league's convention that
    sessions 15+ are the postseason."""
    ps = meta.get("post_sessions")
    if ps:
        return set(ps)
    return {s for s in meta.get("sessions", []) if s >= 15}


def _round(x):
    return round(x, 1) if isinstance(x, float) else x


def _ip_outs(ip: str) -> int:
    whole, _, frac = str(ip).partition(".")
    return int(whole) * 3 + int(frac or 0)


def _empty(keys):
    return {k: 0 for k in keys}


def _add(into: dict, line: dict, keys) -> None:
    for k in keys:
        v = line.get(k)
        if v:
            into[k] = round(into.get(k, 0) + v, 3) if isinstance(v, float) else into.get(k, 0) + v
    w = line.get("wpa")
    if w:
        into["wpa"] = round(into.get("wpa", 0) + w, 1)


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (0, 0.0, None)}


def game_teams(rows: list[dict], name_to_id: dict) -> dict[int, str]:
    """player_id -> team abbr for one game, from what each id did in its plays
    (batters/runners/scorers are on the offense, pitchers/catchers the defense)."""
    team: dict[int, str] = {}
    for m in rows:
        off, dfn = m["off_team_abbr"], m["def_team_abbr"]
        for pid in (m.get("batter_id"), m.get("runner_id")):
            if pid:
                team.setdefault(pid, off)
        for pid in m.get("scoring_ids") or []:
            if pid:
                team.setdefault(pid, off)
        if m.get("pitcher_id"):
            team.setdefault(m["pitcher_id"], dfn)
        c = (m.get("defense") or {}).get("C")
        if c:
            pid = name_to_id.get(c[0])
            if pid:
                team.setdefault(pid, dfn)
        for pid in m.get("er_ids") or []:
            if pid:
                team.setdefault(pid, dfn)
    return team


def main() -> None:
    t0 = time.time()
    names: dict[int, str] = {}
    player_games: dict[int, dict] = defaultdict(lambda: {"bat": [], "pit": []})
    team_sessions: dict[tuple, dict] = {}
    dists: dict[tuple, list] = {}              # (pid, side, season key) -> one count per model result x 25 diff buckets, flat
    n_res = len(dm.model()["GC"]) if dm.available() else 0
    game_lines: dict[tuple, dict] = {}         # (season, session, game_code) -> both teams' stats combined
    player_sessions: dict[tuple, dict] = {}   # (pid, season, session) -> {"t","bat","pit"}
    seasons_meta = []
    session_stat_rows: list[tuple] = []        # (season, session, side, row, postseason) straight from each season's meta.stats
    rookie_ids: dict[int, set] = {}            # season -> player ids flagged rookie that season
    team_look: dict[str, dict] = {}            # abbr -> colours/logo from the latest season that team existed
    last_pos: dict[int, list] = {}             # pid -> [primary, secondary] from the latest season they were rostered
    active_team: dict[int, str] = {}           # pid -> team, only for players Active/Captain in the newest season
    pos_season: dict[int, int] = {}
    last_bld: dict[int, dict] = {}
    look_season: dict[str, int] = {}
    last_season = max(sn for sn, _ in season_dirs())

    for season, sdir in season_dirs():
        meta = _load(os.path.join(sdir, "meta.json"))
        roster = _load(os.path.join(sdir, "players.json"))
        for p in roster:
            if p.get("id") and p.get("name"):
                names[p["id"]] = p["name"]
            if p.get("id") and p.get("pos1") and season >= pos_season.get(p["id"], 0):
                last_pos[p["id"]] = [p["pos1"], p.get("pos2") or ""]
                last_bld[p["id"]] = {"hand": p.get("hand") or "", **(p.get("bld") or {})}
                pos_season[p["id"]] = season
            if season == last_season and p.get("id") and p.get("status") in ("Active", "Captain") and p.get("team") in (meta.get("teams") or {}):
                active_team[p["id"]] = p["team"]
        for abbr, t in (meta.get("teams") or {}).items():
            if season < look_season.get(abbr, 0):
                continue
            look_season[abbr] = season
            team_look[abbr] = {"c1": t.get("primary_hex") or "", "c2": t.get("secondary_hex") or "", "logo": t.get("logo_url") or ""}
        name_to_id = {p["name"]: p["id"] for p in roster if p.get("id") and p.get("name")}
        ref = {"player_by_id": {p["id"]: {"name": p["name"], "last_name": p.get("last_name")} for p in roster if p.get("id")},
               "name_to_id": name_to_id, "team_by_id": {}, "team_by_abbrev": {}}
        team_names = {a: t.get("name") or a for a, t in (meta.get("teams") or {}).items()}
        post = post_sessions(meta)
        rookie_ids[season] = {p["id"] for p in roster if p.get("rookie") and p.get("id")}
        seasons_meta.append({"season": season, "sessions": meta.get("sessions", []), "teams": team_names,
                             "post": sorted(post)})
        awards_by: dict[tuple, tuple] = {}       # (session, side, player id) -> (session award, game award)
        for sess_key, sess_stats in (meta.get("stats") or {}).items():
            for side in ("batting", "pitching"):
                for r in sess_stats.get(side) or []:
                    session_stat_rows.append((season, int(sess_key), side, r, 1 if int(sess_key) in post else 0))
                    if r.get("award") or r.get("game_award"):
                        awards_by[(int(sess_key), side, r["player_id"])] = (r.get("award"), r.get("game_award"))

        for session in meta.get("sessions", []):
            path = os.path.join(sdir, f"plays_{session:02d}.json")
            if not os.path.exists(path):
                continue
            plays = _load(path)
            by_game: dict[str, list] = defaultdict(list)
            for m in plays:
                if m.get("result") is not None:
                    by_game[m["game_code"]].append(m)
            sb_games = {g["game_code"]: g for g in (meta.get("games") or {}).get(str(session), [])}

            for code, grows in by_game.items():
                g = sb_games.get(code) or {}
                last = max(grows, key=lambda m: m["play_num"])
                away, home = last["away_team_abbr"], last["home_team_abbr"]
                final = bool(g.get("is_game_final") or last.get("is_game_final"))
                a_sc, h_sc = (g.get("away_score"), g.get("home_score")) if g else (last["away_score"], last["home_score"])
                if a_sc is None:
                    a_sc, h_sc = last["away_score"], last["home_score"]
                date = (grows[0].get("timestamp") or "")[:10]
                decisions = {int(k): v for k, v in (g.get("decisions") or {}).items()}
                tmap = game_teams(grows, name_to_id)
                stats = kmb._session_stats(grows, session, season, ref, {}, {})
                _, swing_dist = dm.game_metrics(grows, season)
                for side, pid, bucket, sc in swing_dist:
                    arr = dists.setdefault((pid, side, f"{season}p" if session in post else str(season)), [0] * (n_res * dm.BINS))
                    arr[sc * dm.BINS + bucket] += 1

                gr = game_lines.setdefault((season, session, code), {
                    "s": season, "n": session, "g": code, "po": 1 if session in post else 0, "a": away, "h": home,
                    "as": a_sc or 0, "hs": h_sc or 0, "f": 1 if final else 0, "bat": {}, "pit": {}, "rb": {}, "rp": {}})

                for side, score_for, score_against in ((away, a_sc, h_sc), (home, h_sc, a_sc)):
                    ts = team_sessions.setdefault((season, session, side), {
                        "s": season, "n": session, "t": side, "po": 1 if session in post else 0, "g": 0, "w": 0, "l": 0, "rs": 0, "ra": 0,
                        "bat": {}, "pit": {}, "rb": {}, "rp": {}})
                    # a game still in progress counts (games, runs for/against so far, every stat line); only the
                    # win/loss waits for the final
                    ts["g"] += 1
                    ts["rs"] += score_for or 0
                    ts["ra"] += score_against or 0
                    if final:
                        if (score_for or 0) > (score_against or 0):
                            ts["w"] += 1
                        elif (score_for or 0) < (score_against or 0):
                            ts["l"] += 1

                rk_teams = {"b": set(), "p": set()}      # teams with a rookie batting / pitching line in this game
                for b in stats["batting"]:
                    pid = b["player_id"]
                    team = tmap.get(pid)
                    if not team:
                        continue
                    if not any(b.get(k) for k in BAT_COUNT) and not b.get("wpa"):
                        continue
                    opp = home if team == away else away
                    row = {"s": season, "n": session, "g": code, "d": date, "t": team, "o": opp,
                           "hm": 1 if team == home else 0}
                    if session in post:
                        row["po"] = 1
                    aw = awards_by.get((session, "batting", pid))
                    if aw:
                        if aw[0]:
                            row["aw"] = aw[0]
                        if aw[1]:
                            row["ga"] = aw[1]
                    row.update(_compact({k: b.get(k, 0) for k in BAT_KEYS}))
                    if b.get("wpa"):
                        row["wpa"] = b["wpa"]
                    if b["pa"]:
                        row["gsc"] = b["gsc"]
                    player_games[pid]["bat"].append(row)
                    _add(team_sessions[(season, session, team)]["bat"], b, BAT_KEYS)
                    _add(gr["bat"], b, BAT_KEYS)
                    if pid in rookie_ids.get(season, ()):      # rookie-only lines, for the Stats tab's Rookies filter
                        rk_teams["b"].add(team)
                        _add(team_sessions[(season, session, team)]["rb"], b, BAT_KEYS)
                        _add(gr["rb"], b, BAT_KEYS)
                    ps = player_sessions.setdefault((pid, season, session), {"t": team, "po": 1 if session in post else 0, "bat": {}, "pit": {}})
                    _add(ps["bat"], b, BAT_KEYS)
                    ps["bat"]["g"] = ps["bat"].get("g", 0) + 1
                    if b["pa"]:
                        ps["bat"]["gsm"] = max(ps["bat"].get("gsm", b["gsc"]), b["gsc"])

                for p in stats["pitching"]:
                    pid = p["player_id"]
                    team = tmap.get(pid)
                    if not team:
                        continue
                    line = dict(p)
                    line["outs"] = _ip_outs(p["ip"])
                    opp = home if team == away else away
                    row = {"s": season, "n": session, "g": code, "d": date, "t": team, "o": opp,
                           "hm": 1 if team == home else 0}
                    if session in post:
                        row["po"] = 1
                    aw = awards_by.get((session, "pitching", pid))
                    if aw:
                        if aw[0]:
                            row["aw"] = aw[0]
                        if aw[1]:
                            row["ga"] = aw[1]
                    row.update(_compact({k: line.get(k, 0) for k in PIT_KEYS}))
                    if line.get("wpa"):
                        row["wpa"] = line["wpa"]
                    row["gsc"] = line["gsc"]
                    if pid in decisions:
                        row["dec"] = decisions[pid]
                    player_games[pid]["pit"].append(row)
                    _add(team_sessions[(season, session, team)]["pit"], line, PIT_KEYS)
                    _add(gr["pit"], line, PIT_KEYS)
                    if pid in rookie_ids.get(season, ()):
                        rk_teams["p"].add(team)
                        _add(team_sessions[(season, session, team)]["rp"], line, PIT_KEYS)
                        _add(gr["rp"], line, PIT_KEYS)
                    ps = player_sessions.setdefault((pid, season, session), {"t": team, "po": 1 if session in post else 0, "bat": {}, "pit": {}})
                    _add(ps["pit"], line, PIT_KEYS)
                    ps["pit"]["g"] = ps["pit"].get("g", 0) + 1
                    ps["pit"]["gsm"] = max(ps["pit"].get("gsm", line["gsc"]), line["gsc"])
                    if pid in decisions:
                        d = decisions[pid]
                        key = {"W": "w", "L": "l", "SV": "sv", "H": "hd"}[d]
                        ps["pit"][key] = ps["pit"].get(key, 0) + 1
                        tp = team_sessions[(season, session, team)]["pit"]
                        tp[key] = tp.get(key, 0) + 1

                # Rookies filter: a team's G / W / L counted only over games where a rookie played (RS / RA come from the rookies' own lines).
                # ob / op = games where THIS team had a rookie batting / pitching line; xb / xp = games where either
                # team did (each such game sits under both teams, so the league row halves the game count).
                for side, score_for, score_against in ((away, a_sc, h_sc), (home, h_sc, a_sc)):
                    for k in ("b", "p"):
                        if not rk_teams[k]:
                            continue
                        for pre, on in (("o", side in rk_teams[k]), ("x", True)):
                            if not on:
                                continue
                            c = team_sessions[(season, session, side)].setdefault(pre + k, {"g": 0, "w": 0, "l": 0})
                            c["g"] += 1
                            if final:
                                if (score_for or 0) > (score_against or 0):
                                    c["w"] += 1
                                elif (score_for or 0) < (score_against or 0):
                                    c["l"] += 1
        print(f"  S{season:02d} done ({time.time() - t0:.0f}s)")

    # ---- per-player career logs ------------------------------------------------
    dist_by_player: dict[int, dict] = {}
    for (pid, side, sk), arr in dists.items():
        dist_by_player.setdefault(pid, {"bat": {}, "pit": {}})[side][sk] = arr
    out_dir = os.path.join(DATA, "career", "p")
    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):
            os.remove(os.path.join(out_dir, f))
    hdr_teams: dict[int, str] = {}
    for pid, g in player_games.items():
        for side in ("bat", "pit"):
            g[side].sort(key=lambda r: (r["s"], r["n"], r["d"], r["g"]))
        # Header team: the current team for an active player, else the team (not franchise) with the most games.
        games_by_team: dict[str, set] = defaultdict(set)
        for side in ("bat", "pit"):
            for r in g[side]:
                if r.get("t"):
                    games_by_team[r["t"]].add((r["s"], r["g"]))
        top_team = max(games_by_team, key=lambda t: len(games_by_team[t])) if games_by_team else ""
        hdr_team = active_team.get(pid) or top_team
        hdr_teams[pid] = hdr_team
        hdr = {"t": hdr_team, "pos": last_pos.get(pid, ["", ""]), "act": 1 if pid in active_team else 0,
               "bld": last_bld.get(pid, {})}
        _write(os.path.join(out_dir, f"{pid}.json"), {"id": pid, "name": names.get(pid, f"Player {pid}"), "hdr": hdr, **g,
                                                      "dist": dist_by_player.get(pid, {"bat": {}, "pit": {}})})

    # ---- per-player per-season totals ------------------------------------------
    totals: dict[int, dict] = {}
    for (pid, season, session), ps in player_sessions.items():
        ent = totals.setdefault(pid, {"n": names.get(pid, f"Player {pid}"), "s": {}})
        se = ent["s"].setdefault(f"{season}p" if ps["po"] else str(season), {"t": ps["t"], "b": {}, "p": {}, "_last": 0})
        if session >= se["_last"]:
            se["t"], se["_last"] = ps["t"], session
        for src, dst in ((ps["bat"], se["b"]), (ps["pit"], se["p"])):
            for k, v in src.items():
                dst[k] = round(dst.get(k, 0) + v, 3) if isinstance(v, float) else dst.get(k, 0) + v
    for ent in totals.values():
        for se in ent["s"].values():
            se.pop("_last", None)
            if not se["b"]:
                del se["b"]
            if not se["p"]:
                del se["p"]
    # The diff charts' league overlay: the calculator model's result share per 20-wide diff bucket, plus the
    # league's average diff per PA (hitters) for the DPA marker.
    ds = sum(ps["bat"].get("ds", 0) for ps in player_sessions.values())
    dn = sum(ps["bat"].get("dn", 0) for ps in player_sessions.values())
    # league diff sums per season (a "p" suffix = postseason), so the diff chart can mark the league average for
    # whichever season / game type is showing
    lg_s: dict[str, list] = {}
    for (_pid, season, _session), ps in player_sessions.items():
        key = f"{season}p" if ps["po"] else str(season)
        acc = lg_s.setdefault(key, [0, 0])
        acc[0] += ps["bat"].get("ds", 0)
        acc[1] += ps["bat"].get("dn", 0)
    _write(os.path.join(DATA, "career", "model.json"), {
        "w": dm.BIN_W, "bins": dm.BINS, "p": dm.league_bins() if dm.available() else None,
        # every result the model separates (1B, 1BWH, 2B, GO, DP21 ...) with the Streamlit scouting colour for it
        "gc": [[g, utils._RESULT_ZONE_COLORS.get(g, "#cccccc")] for g in (dm.model()["GC"] if dm.available() else [])],
        "lgdpa": round(ds / dn, 1) if dn else None, "lg_s": lg_s})
    _write(os.path.join(DATA, "career", "totals.json"),
           {"seasons": [s["season"] for s in seasons_meta], "players": totals})

    # ---- every player-session line (Records tab, Player table) -----------------
    pit_keys = PIT_KEYS + ["w", "l", "sv", "hd"]
    psess = {"keys": {"bat": BAT_KEYS, "pit": pit_keys}, "n": {}, "bat": [], "pit": []}
    for (pid, season, session), ps in sorted(player_sessions.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        psess["n"][pid] = names.get(pid, f"Player {pid}")
        for side, keys in (("bat", BAT_KEYS), ("pit", pit_keys)):
            line = ps[side]
            if line:
                psess[side].append([pid, season, session, ps["po"], ps["t"], line.get("g", 0)] +
                                   [line.get(k, 0) for k in keys] + [line.get("wpa", 0), line.get("gsm")])
    _write(os.path.join(DATA, "career", "psessions.json"), psess)

    # ---- every session line as the Stats tab shows it (the All-Time Stats view) ---------------------
    bat_cols = ["gsc", "wpa", "dpa", "ab", "r", "h", "2b", "3b", "hr", "rbi", "bb", "k", "sb", "cs", "gidp", "lob"]
    pit_cols = ["gsc", "wpa", "dpa", "ip", "h", "er", "bb", "k", "hr", "dp", "lob", "w", "l", "sv", "hd"]
    sstats = {"n": {}, "keys": {"bat": bat_cols, "pit": pit_cols}, "bat": [], "pit": []}
    for season, session, side, r, po in sorted(session_stat_rows, key=lambda t: (t[0], t[1])):
        pid = r["player_id"]
        side_key, cols = ("bat", bat_cols) if side == "batting" else ("pit", pit_cols)
        ps = player_sessions.get((pid, season, session))
        team = ps["t"] if ps else ""
        sstats["n"][pid] = r.get("name") or names.get(pid, f"Player {pid}")
        sstats[side_key].append([pid, season, session, po, team] + [r.get(k) for k in cols] +
                                [r.get("award"), r.get("game_award"), 1 if pid in rookie_ids.get(season, ()) else 0])
    _write(os.path.join(DATA, "career", "stats_sessions.json"), sstats)

    # ---- records: team sessions -----------------------
    team_rows = [dict(ts, bat=_compact(ts["bat"]), pit=_compact(ts["pit"]), rb=_compact(ts["rb"]), rp=_compact(ts["rp"]))
                 for ts in team_sessions.values() if ts["g"]]
    team_rows.sort(key=lambda r: (r["s"], r["n"], r["t"]))
    _write(os.path.join(DATA, "records.json"), {
        "seasons": seasons_meta, "teams": team_rows})

    # ---- records: one combined line per game (both teams) ------------------------
    games = [dict(gl, bat=_compact(gl["bat"]), pit=_compact(gl["pit"]), rb=_compact(gl["rb"]), rp=_compact(gl["rp"]))
             for gl in game_lines.values() if gl["bat"] or gl["pit"]]
    games.sort(key=lambda r: (r["s"], r["n"], r["g"]))
    _write(os.path.join(DATA, "records_games.json"), {"games": games})

    # ---- slim search index per season -------------------------------------------
    for season, sdir in season_dirs():
        meta = _load(os.path.join(sdir, "meta.json"))
        cols = {k: [] for k in ("pn", "n", "g", "in", "hf", "o", "b", "r", "d", "bi", "pi", "ot", "dt", "ru", "w", "lv", "pt", "sw", "km", "po",
                                "fi", "ci", "fo", "rk", "tg", "ca")}
        dicts = {"g": {}, "b": {}, "r": {}, "ot": {}, "tg": {}}
        # rookies this season, so a play can say whether its featured / counterpart player was one
        rookies = {p["id"] for p in _load(os.path.join(sdir, "players.json")) if p.get("rookie") and p.get("id")}

        def code(table, v):
            return table.setdefault(v, len(table))

        post = post_sessions(meta)
        for session in meta.get("sessions", []):
            path = os.path.join(sdir, f"plays_{session:02d}.json")
            if not os.path.exists(path):
                continue
            for m in _load(path):
                if m.get("result") is None:
                    continue
                cols["pn"].append(m["play_num"])
                cols["n"].append(session)
                cols["g"].append(code(dicts["g"], m["game_code"]))
                cols["in"].append(m["inning"])
                cols["hf"].append(1 if m["half"] == "bottom" else 0)
                cols["o"].append(m["outs_before"])
                cols["b"].append(code(dicts["b"], m["obc_before"]))
                cols["r"].append(code(dicts["r"], m["result"]))
                cols["d"].append(m["diff"] if m.get("diff") is not None else -1)
                cols["bi"].append(m.get("batter_id") or 0)
                cols["pi"].append(m.get("pitcher_id") or 0)
                cols["ot"].append(code(dicts["ot"], m["off_team_abbr"]))
                cols["dt"].append(code(dicts["ot"], m["def_team_abbr"]))
                cols["ru"].append(m.get("runs") or 0)
                cols["w"].append(int(round((m.get("wpa") or 0) * 1000)))
                cols["lv"].append(int(round((m.get("leverage") or 0) * 100)))
                cols["pt"].append(m["pitch"] if m.get("pitch") is not None else -1)
                cols["sw"].append(m["swing"] if m.get("swing") is not None else -1)
                cols["km"].append(1 if m.get("is_key_moment") else 0)
                cols["po"].append(1 if session in post else 0)
                # who the play favours: featured/counterpart ids, whether the featured player is on offense, rookie bits
                cols["fi"].append(m.get("featured_id") or 0)
                cols["ci"].append(m.get("counterpart_id") or 0)
                cols["fo"].append(1 if m.get("featured_team_abbr") == m["off_team_abbr"] else 0)
                cols["rk"].append((1 if m.get("featured_id") in rookies else 0) | (2 if m.get("counterpart_id") in rookies else 0))
                cols["ca"].append(1 if m.get("result_category") == "hitting" else 0)
                mask = 0
                for t in m.get("tags") or []:
                    mask |= 1 << code(dicts["tg"], t)
                cols["tg"].append(mask)
        lists = {k: [v for v, _ in sorted(t.items(), key=lambda kv: kv[1])] for k, t in dicts.items()}
        _write(os.path.join(DATA, "search", f"s{season:02d}.json"),
               {"season": season, "n": len(cols["pn"]), "dict": lists, "cols": cols})

    # ---- player name table used by the Search tab ------------------------------
    team_info: dict[str, dict] = {}
    for sm in seasons_meta:
        for abbr, nm in sm["teams"].items():
            ti = team_info.setdefault(abbr, {"name": nm, "seasons": []})
            ti["name"] = nm
            ti["seasons"].append(sm["season"])
    franchises = []
    for abbrs in FRANCHISES:
        known = [a for a in abbrs if a in team_info]
        if not known:
            continue
        cur = max(known, key=lambda a: team_info[a]["seasons"][-1])
        franchises.append({"abbr": cur, "name": team_info[cur]["name"], "abbrs": known})
    franchises.sort(key=lambda f: f["name"])
    for abbr, ti in team_info.items():
        ti.update(team_look.get(abbr, {}))
    _write(os.path.join(DATA, "search", "names.json"),
           {"n": {str(k): v for k, v in names.items()}, "teams": team_info, "franchises": franchises,
            "c": {str(k): v for k, v in hdr_teams.items()}})   # players with a career file -> header team (the header search)
    print(f"career_build: {len(player_games)} players, {len(team_rows)} team-sessions, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    if not os.path.isdir(DATA):
        print("FATAL: run from the repo root (docs/data not found).", file=sys.stderr)
        sys.exit(1)
    main()

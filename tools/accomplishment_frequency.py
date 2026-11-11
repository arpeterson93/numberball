"""How often would each candidate player-modal accomplishment be handed out?

Reads only the already-built site data (docs/data/sNN/plays_*.json, docs/data/plays_*.json for the live
season, docs/data/career/p/*.json for per-game RBI / SB), so it needs no network. Re-run after a build:

    python tools/accomplishment_frequency.py

Writes, next to this script under accomplishment_frequency_out/:
    instances.csv   one row per (accomplishment, player, game) - the data a badge UI would read
    summary.csv     per accomplishment: total, distinct players, players with 2+/3+/5+, max for one player,
                    average per season, and the same by season
    summary.md      the same summary as a table, for the README / chat

Definitions (agreed with Alex):
  Batting, one game     multi-HR (2+, 3+ tier), 4+ hits, cycle, grand slam, 5+ RBI, 3+ SB, reached base 5+
                        (H + BB + IBB + AutoBB), golden sombrero (4+ K), walk-off, game-winning result
  Pitching, one game    10+ K, complete-game shutout, no-hitter, perfect game, immaculate half inning
  Complete game         one pitcher recorded every defensive PA for his team in a finished game
  No-hitter / perfect   complete game with no hit allowed / also no BB and no fielder's-choice reach. A CG needs
                        15+ outs recorded, which also drops game 050607 (S5), whose play log is only 3 plays.
  Immaculate inning     a half inning with exactly 3 batters faced, all 3 real strikeouts (result K, not AutoK)
  Walk-off / game-win   the batter of the play carrying the `walk_off` / `game_winning` tag (the repo's own
                        definition, key_moments_build._mark_game_winning), when that play was the batter's PA
                        (a steal or caught stealing that scores the run is not the batter's result)
  K counts              10+ K and the golden sombrero count autos (AutoK, bAuto, as the site's stat lines do);
                        the immaculate inning counts real strikeouts only (result K).
  Not badges (decided)  plain complete games (too common; only the CG shutout, no-hitter and perfect game use
                        the complete-game test). game_winning_result is still output but is ~1 per game, so it
                        reads better as a career count than a badge.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "docs", "data")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accomplishment_frequency_out")

PLAYER_ID_ALIASES = {6006: 3072}  # utils.PLAYER_ID_ALIASES

SINGLES = {"1B", "IF1B", "1BWH", "1BWH2", "B1B", "B1BWH"}
DOUBLES = {"2B", "2BWH"}
TRIPLES = {"3B"}
HOMERS = {"HR"}
HITS = SINGLES | DOUBLES | TRIPLES | HOMERS
WALKS = {"BB", "IBB", "AutoBB"}
REAL_K = {"K"}
SITE_K = {"K", "AutoK", "KCS", "bAuto"}
# Results where the batter reaches without a hit or walk (fielder's choices)
REACH_OTHER = {"FC", "FCH", "FC3rd", "FCLead", "BFC"}
# Plays that are not a plate appearance (runner events, balks, auto notes)
NON_PA = {"SB", "SB2", "SB3", "SB4", "SB32", "SB42", "SB43", "SB432", "AutoSB", "CS", "CS2", "CS3", "CS4",
          "Balk", "pAuto", "bAuto", "cAuto"}


def load_plays() -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "s*", "plays_*.json"))
                       + glob.glob(os.path.join(DATA, "plays_*.json"))):
        season = None
        d = os.path.basename(os.path.dirname(path))
        if d.startswith("s") and d[1:].isdigit():
            season = int(d[1:])
        else:
            with open(os.path.join(DATA, "meta.json"), encoding="utf-8") as fh:
                season = json.load(fh)["season"]
        with open(path, encoding="utf-8") as fh:
            for r in json.load(fh):
                r["_season"] = season
                rows.append(r)
    return rows


def pid(x):
    return PLAYER_ID_ALIASES.get(x, x) if x is not None else None


def compute():
    """-> (instances, plays, by_game, names). instances: (key, player_id, season, game, detail) tuples."""
    plays = load_plays()
    by_game = defaultdict(list)
    for r in plays:
        by_game[r["game_code"]].append(r)
    for g in by_game.values():
        g.sort(key=lambda p: p["play_num"])
    names = {p["id"]: p["name"] for p in json.load(open(os.path.join(DATA, "players.json"), encoding="utf-8"))}

    # Per-game RBI / SB come from the career files so they match the site's own stat lines.
    rbi, sb, career_name = {}, {}, {}
    for path in glob.glob(os.path.join(DATA, "career", "p", "*.json")):
        d = json.load(open(path, encoding="utf-8"))
        i = pid(d["id"])
        career_name[i] = d.get("name", "")
        for g in d.get("bat", []):
            rbi[(i, g["g"])] = g.get("rbi", 0)
            sb[(i, g["g"])] = g.get("sb", 0)

    inst = []  # (key, label, player_id, season, game, detail)

    def add(key, i, season, game, detail="", pns=None):
        inst.append((key, i, season, game, detail, pns or []))

    for game, ps in by_game.items():
        season = ps[0]["_season"]
        final = any(p["is_game_final"] for p in ps)

        # ---- batting box, per batter -------------------------------------------------------
        box = defaultdict(Counter)
        for p in ps:
            if p["result"] in NON_PA or p["result"] is None:
                continue
            b = pid(p["batter_id"])
            r = p["result"]
            c = box[b]
            c["pa"] += 1
            if r in HITS:
                c["h"] += 1
            if r in SINGLES:
                c["1b"] += 1
            if r in DOUBLES:
                c["2b"] += 1
            if r in TRIPLES:
                c["3b"] += 1
            if r in HOMERS:
                c["hr"] += 1
                if p["obc_before"] == "111":
                    add("grand_slam", b, season, game, "", [p["play_num"]])
            if r in WALKS:
                c["bb"] += 1
            if r in REAL_K:
                c["k"] += 1
            if r in SITE_K:
                c["k_site"] += 1
        for b, c in box.items():
            if c["hr"] >= 2:
                add("multi_hr_2plus", b, season, game, f"{c['hr']} HR")
            if c["hr"] >= 3:
                add("multi_hr_3plus", b, season, game, f"{c['hr']} HR")
            if c["h"] >= 4:
                add("hits_4plus", b, season, game, f"{c['h']} H")
            if c["1b"] and c["2b"] and c["3b"] and c["hr"]:
                add("cycle", b, season, game)
            if c["h"] + c["bb"] >= 5:
                add("reached_5plus", b, season, game, f"{c['h'] + c['bb']} times on base")
            if c["k_site"] >= 4:
                add("golden_sombrero", b, season, game, f"{c['k_site']} K")
            if rbi.get((b, game), 0) >= 5:
                add("rbi_5plus", b, season, game, f"{rbi[(b, game)]} RBI")
        # ---- tagged plays: walk-off / game-winning -----------------------------------------
        for p in ps:
            if p["result"] in NON_PA:
                continue
            tags = p.get("tags") or []
            if "walk_off" in tags:
                add("walk_off", pid(p["batter_id"]), season, game, p["result"], [p["play_num"]])
            if "game_winning" in tags:
                add("game_winning_result", pid(p["batter_id"]), season, game, p["result"], [p["play_num"]])
            # 0 and 500 diffs (key_moments_build tags them: both numbers real for a 0 diff), one per side of the play
            if "zero_diff" in tags:
                add("zero_diff_bat", pid(p["batter_id"]), season, game, p["result"], [p["play_num"]])
                add("zero_diff_pit", pid(p["pitcher_id"]), season, game, p["result"], [p["play_num"]])
            if "five_hundred_diff" in tags:
                add("five_hundred_bat", pid(p["batter_id"]), season, game, p["result"], [p["play_num"]])
                add("five_hundred_pit", pid(p["pitcher_id"]), season, game, p["result"], [p["play_num"]])
        # ---- half innings: immaculate ------------------------------------------------------
        halves = defaultdict(list)
        for p in ps:
            if p["result"] in NON_PA or p["result"] is None:
                continue
            halves[(p["inning"], p["half"])].append(p)
        for (inn, half), hp in halves.items():
            if len(hp) == 3 and all(p["result"] == "K" for p in hp):
                pit = {pid(p["pitcher_id"]) for p in hp}
                auto_pitch = any(p["pitch"] == 0 for p in hp)
                add("immaculate_inning", pit.pop() if len(pit) == 1 else None, season, game,
                    f"{half} {inn}" + (" (multiple pitchers)" if len(pit) > 1 else "")
                    + (" (a pitch==0 row)" if auto_pitch else ""), [p["play_num"] for p in hp])

        # ---- pitching, per pitcher-game ------------------------------------------------------
        by_side = defaultdict(list)
        for p in ps:
            by_side[p["def_team_abbr"]].append(p)
        for team, dp in by_side.items():
            pa = [p for p in dp if p["result"] not in NON_PA and p["result"] is not None]
            pitchers = {pid(p["pitcher_id"]) for p in dp if p["pitcher_id"] is not None}
            ks = Counter()
            for p in pa:
                if p["result"] in REAL_K:
                    ks[pid(p["pitcher_id"])] += 1
            ks_site = Counter(pid(p["pitcher_id"]) for p in pa if p["result"] in SITE_K)
            for i, n in ks_site.items():
                if n >= 10:
                    add("k_10plus", i, season, game, f"{n} K")
            if not final or len(pitchers) != 1:
                continue
            (i,) = pitchers
            opp = [p for p in ps if p["def_team_abbr"] == team]
            runs_allowed = sum(p["runs"] or 0 for p in opp)
            hits_allowed = sum(1 for p in pa if p["result"] in HITS)
            walks = sum(1 for p in pa if p["result"] in WALKS)
            outs_recorded = sum(max((p["outs_after"] or 0) - (p["outs_before"] or 0), 0) for p in opp)
            reached_other = any(p["result"] in REACH_OTHER for p in pa)
            if outs_recorded < 15:  # a finished 6-inning game has at least 15 outs by the losing-home / walk-off side
                continue
            if runs_allowed == 0:
                add("cg_shutout", i, season, game)
            if hits_allowed == 0:
                add("no_hitter", i, season, game, f"{walks} BB")
                if walks == 0 and not reached_other:
                    add("perfect_game", i, season, game)

    # 3+ SB: from career rows, only games present in the plays
    games_season = {g: ps[0]["_season"] for g, ps in by_game.items()}
    for (b, g), n in sb.items():
        if n >= 3 and g in games_season:
            add("sb_3plus", b, games_season[g], g, f"{n} SB")

    names = {k: v for k, v in names.items()}
    for k, v in career_name.items():
        names.setdefault(k, v)
    return inst, plays, by_game, names


def main() -> None:
    inst, plays, by_game, names = compute()
    # ---- output ---------------------------------------------------------------------------
    os.makedirs(OUT, exist_ok=True)
    label = lambda i: names.get(i) or str(i)
    with open(os.path.join(OUT, "instances.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["accomplishment", "player_id", "player_name", "season", "game_code", "detail"])
        for key, i, season, game, detail, *_ in sorted(inst, key=lambda t: (t[0], t[2], t[3])):
            w.writerow([key, "" if i is None else i, "" if i is None else label(i), season, game, detail])

    seasons = sorted({p["_season"] for p in plays})
    n_games = Counter(ps[0]["_season"] for ps in by_game.values())
    keys = []
    for k, *_ in inst:
        if k not in keys:
            keys.append(k)
    order = ["multi_hr_2plus", "multi_hr_3plus", "hits_4plus", "cycle", "grand_slam", "rbi_5plus", "sb_3plus",
             "reached_5plus", "golden_sombrero", "walk_off", "game_winning_result",
             "k_10plus", "cg_shutout", "no_hitter", "perfect_game", "immaculate_inning"]
    keys = [k for k in order if k in keys] + [k for k in keys if k not in order]
    rows = []
    for k in keys:
        mine = [t for t in inst if t[0] == k]
        per_player = Counter(t[1] for t in mine if t[1] is not None)
        per_season = Counter(t[2] for t in mine)
        rows.append({
            "accomplishment": k, "total": len(mine), "players": len(per_player),
            "players_2plus": sum(1 for v in per_player.values() if v >= 2),
            "players_3plus": sum(1 for v in per_player.values() if v >= 3),
            "players_5plus": sum(1 for v in per_player.values() if v >= 5),
            "max_one_player": max(per_player.values(), default=0),
            "top": "; ".join(f"{label(i)} x{n}" for i, n in per_player.most_common(3)),
            "no_player": sum(1 for t in mine if t[1] is None),
            **{f"s{s:02d}": per_season.get(s, 0) for s in seasons},
        })
    fields = list(rows[0])
    with open(os.path.join(OUT, "summary.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    md = ["| accomplishment | total | players | 2+ | 3+ | 5+ | max | top |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['accomplishment']} | {r['total']} | {r['players']} | {r['players_2plus']} | "
                  f"{r['players_3plus']} | {r['players_5plus']} | {r['max_one_player']} | {r['top']} |")
    md.append("")
    md.append("Games per season: " + ", ".join(f"S{s}={n_games[s]}" for s in seasons))
    md.append("")
    md.append("| accomplishment | " + " | ".join(f"S{s}" for s in seasons) + " |")
    md.append("|---|" + "---|" * len(seasons))
    for r in rows:
        md.append(f"| {r['accomplishment']} | " + " | ".join(str(r[f's{s:02d}']) for s in seasons) + " |")
    with open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\n{len(plays)} plays, {len(by_game)} games; wrote {OUT}")


if __name__ == "__main__":
    main()

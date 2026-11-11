"""Season milestone + counting-stat leader candidates for the player-modal Awards tab.

Reads docs/data/career/totals.json (every player's per-season regular-season / postseason lines; a "p" suffix is
postseason, ignored here), docs/data/season_awards.json (for the MVP / Cy Young overlap check) and
docs/data/players.json. No network. Run from anywhere:

    python tools/season_milestones.py

Writes under tools/season_milestones_out/:
    ladders.md          for each stat, the value that the top 0.5 / 1 / 2 / 4 / 8 player-seasons PER SEASON reach
                        (a milestone threshold picked near the 1 or 2 row hands out about that many a season),
                        plus how many player-seasons / distinct players clear some round numbers
    leaders.csv         every season's leader (ties all listed) for each stat
    leaders_summary.md  per stat: leader count, distinct players, share who also won that season's MVP / Cy Young;
                        and which players pile up the most leader titles
Completed seasons (S1-S12) feed the thresholds and leader tables; the live season is shown separately.

Qualifiers for rate stats (set from each season's own team-game count, the most games any hitter played):
    hitters   PA >= 2.5 per team game (about 37 of the 48-ish PA a full-time regular gets)
    pitchers  outs >= 3 per team game (1 IP per team game, as MLB)
DPA / DBF are the average diff per PA / per batter faced (diff = distance between pitch and swing, 0-500).
Low diff is good for the hitter, so the hitter leader has the LOWEST DPA and the pitcher leader the HIGHEST DBF.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "season_milestones_out")

# (key, label, side, kind, higher_is_better)   kind: count | rate
STATS = [
    ("hr", "HR", "b", "count", True), ("rbi", "RBI", "b", "count", True), ("h", "Hits", "b", "count", True),
    ("r", "Runs", "b", "count", True), ("sb", "SB", "b", "count", True), ("bb", "BB", "b", "count", True),
    ("2b", "Doubles", "b", "count", True), ("3b", "Triples", "b", "count", True),
    ("tb", "Total bases", "b", "count", True), ("wpa", "WPA (bat)", "b", "count", True),
    ("avg", "AVG", "b", "rate", True), ("obp", "OBP", "b", "rate", True), ("slg", "SLG", "b", "rate", True),
    ("ops", "OPS", "b", "rate", True), ("dpa", "DPA (low)", "b", "rate", False),
    ("w", "Wins", "p", "count", True), ("k", "K (pit)", "p", "count", True), ("sv", "Saves", "p", "count", True),
    ("hd", "Holds", "p", "count", True), ("ip", "IP", "p", "count", True), ("wpa", "WPA (pit)", "p", "count", True),
    ("era", "ERA", "p", "rate", False), ("whip", "WHIP", "p", "rate", False), ("k9", "K/9", "p", "rate", True),
    ("dbf", "DBF (high)", "p", "rate", True),
]
# round milestone candidates to report counts for
ROUND = {
    ("hr", "b"): [3, 4, 5, 6, 8, 10], ("rbi", "b"): [10, 12, 15, 18, 20, 25], ("h", "b"): [15, 18, 20, 22, 25, 30],
    ("r", "b"): [10, 12, 15, 18, 20], ("sb", "b"): [3, 5, 8, 10, 12], ("bb", "b"): [8, 10, 12, 15],
    ("w", "p"): [3, 4, 5, 6, 7, 8], ("k", "p"): [20, 25, 30, 40, 50], ("sv", "p"): [3, 4, 5, 6, 8],
    ("ip", "p"): [20, 25, 30, 35],
}


def load():
    totals = json.load(open(os.path.join(DATA, "career", "totals.json"), encoding="utf-8"))
    awards = json.load(open(os.path.join(DATA, "season_awards.json"), encoding="utf-8"))
    names = {str(p["id"]): p["name"] for p in json.load(open(os.path.join(DATA, "players.json"), encoding="utf-8"))}
    names.update({pid: e["n"] for pid, e in totals["players"].items()})  # retired players are not in players.json
    return totals, awards, names


def season_rows(totals):
    """season -> side -> [(pid, name, stat dict)], regular season only, with derived stats."""
    out = defaultdict(lambda: {"b": [], "p": []})
    for pid, ent in totals["players"].items():
        for skey, se in ent["s"].items():
            if skey.endswith("p"):
                continue
            s = int(skey)
            b = se.get("b")
            if b and b.get("pa"):
                d = {k: b.get(k, 0) for k in ("pa", "ab", "h", "2b", "3b", "hr", "rbi", "r", "sb", "bb", "k", "sf", "ds", "dn", "g")}
                d["wpa"] = b.get("wpa", 0)
                singles = d["h"] - d["2b"] - d["3b"] - d["hr"]
                d["tb"] = singles + 2 * d["2b"] + 3 * d["3b"] + 4 * d["hr"]
                d["avg"] = d["h"] / d["ab"] if d["ab"] else None
                den = d["ab"] + d["bb"] + d["sf"]
                d["obp"] = (d["h"] + d["bb"]) / den if den else None
                d["slg"] = d["tb"] / d["ab"] if d["ab"] else None
                d["ops"] = d["obp"] + d["slg"] if d["obp"] is not None and d["slg"] is not None else None
                d["dpa"] = d["ds"] / d["dn"] if d["dn"] else None
                out[s]["b"].append((pid, ent["n"], d))
            p = se.get("p")
            if p and p.get("outs"):
                d = {k: p.get(k, 0) for k in ("outs", "h", "hr", "bb", "k", "er", "w", "l", "sv", "hd", "ds", "dn", "g")}
                d["wpa"] = p.get("wpa", 0)
                d["ip"] = d["outs"] / 3
                d["era"] = d["er"] * 27 / d["outs"]
                d["er6"] = d["er"] * 18 / d["outs"]  # earned runs per 6 innings (MLN games are 6 innings)
                d["whip"] = (d["h"] + d["bb"]) * 3 / d["outs"]
                d["k9"] = d["k"] * 27 / d["outs"]
                d["dbf"] = d["ds"] / d["dn"] if d["dn"] else None
                out[s]["p"].append((pid, ent["n"], d))
    return out


def main():
    totals, awards, names = load()
    rows = season_rows(totals)
    live = max(rows)
    done = [s for s in sorted(rows) if s != live]
    team_games = {s: max((d["g"] for _, _, d in rows[s]["b"]), default=0) for s in rows}
    won = defaultdict(set)  # (season, award) -> {pid}
    for pid, lst in awards.items():
        for season, aw, *_ in lst:
            won[(season, aw)].add(pid)

    def qualified(s, side, d):
        tg = team_games[s]
        return d["pa"] >= 2.5 * tg if side == "b" else d["outs"] >= 3 * tg

    os.makedirs(OUT, exist_ok=True)
    md = [f"Team games per season (most games any hitter played): "
          + ", ".join(f"S{s}={team_games[s]}" for s in sorted(rows)),
          f"Thresholds and leaders use S1-S{done[-1]}; S{live} is the live season.", ""]
    md.append("## Ladders: value reached by the top N player-seasons per season (counting stats, all players)\n")
    md.append("| stat | top 0.5/season | 1 | 2 | 4 | 8 | max ever | round-number counts (player-seasons >= value, [distinct players]) |")
    md.append("|---|---|---|---|---|---|---|---|")
    for key, label, side, kind, hib in STATS:
        if kind != "count":
            continue
        vals = sorted((d[key] for s in done for _, _, d in rows[s][side] if d.get(key) is not None), reverse=True)
        if not vals:
            continue
        ladder = []
        for per in (0.5, 1, 2, 4, 8):
            idx = max(0, round(per * len(done)) - 1)
            ladder.append(f"{vals[min(idx, len(vals) - 1)]:g}")
        extra = []
        for t in ROUND.get((key, side), []):
            hit = [(pid) for s in done for pid, _, d in rows[s][side] if (d.get(key) or 0) >= t]
            extra.append(f"{t}+: {len(hit)} [{len(set(hit))}]")
        md.append(f"| {label} | " + " | ".join(ladder) + f" | {vals[0]:g} | " + "; ".join(extra) + " |")

    md.append("\n## Ladders: rate stats among qualified player-seasons\n")
    md.append("| stat | qualified player-seasons | top 0.5/season | 1 | 2 | 4 | 8 |")
    md.append("|---|---|---|---|---|---|---|")
    for key, label, side, kind, hib in STATS:
        if kind != "rate":
            continue
        vals = sorted((d[key] for s in done for _, _, d in rows[s][side] if qualified(s, side, d) and d.get(key) is not None),
                      reverse=hib)
        ladder = []
        for per in (0.5, 1, 2, 4, 8):
            idx = max(0, round(per * len(done)) - 1)
            ladder.append(f"{vals[min(idx, len(vals) - 1)]:.3f}" if key not in ("dpa", "dbf", "era", "whip", "k9") else f"{vals[min(idx, len(vals) - 1)]:.2f}")
        md.append(f"| {label} | {len(vals)} | " + " | ".join(ladder) + " |")
    with open(os.path.join(OUT, "ladders.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    # ---- leaders -------------------------------------------------------------------------
    leader_rows = []
    by_stat = defaultdict(list)  # (key, side) -> [(season, pid)]
    for s in done:
        for key, label, side, kind, hib in STATS:
            pool = [(pid, n, d) for pid, n, d in rows[s][side]
                    if d.get(key) is not None and (kind == "count" or qualified(s, side, d))]
            if kind == "count":
                pool = [t for t in pool if t[2][key] != 0 or key in ("wpa",)]
            if not pool:
                continue
            best = (max if hib else min)(d[key] for _, _, d in pool)
            for pid, n, d in pool:
                if d[key] == best:
                    leader_rows.append({"season": s, "stat": label, "side": side, "value": round(best, 3),
                                        "player_id": pid, "player_name": n})
                    by_stat[(key, side)].append((s, pid))
    with open(os.path.join(OUT, "leaders.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(leader_rows[0]))
        w.writeheader()
        w.writerows(leader_rows)

    sm = ["| stat | titles | distinct players | also that season's MVP | also Cy Young | most titles |", "|---|---|---|---|---|---|"]
    title_count = Counter()
    for key, label, side, kind, hib in STATS:
        lst = by_stat.get((key, side), [])
        if not lst:
            continue
        mvp = sum(1 for s, pid in lst if pid in won[(s, "mvp")])
        cy = sum(1 for s, pid in lst if pid in won[(s, "cy_young")])
        c = Counter(pid for _, pid in lst)
        title_count.update(pid for _, pid in lst)
        top = "; ".join(f"{names.get(p, p)} x{n}" for p, n in c.most_common(2) if n > 1) or "-"
        sm.append(f"| {label} | {len(lst)} | {len(c)} | {mvp} | {cy} | {top} |")
    sm.append("")
    sm.append("Leader titles piled up across all the stats above (a player-season sweep counts once per stat):")
    sm.append("")
    for pid, n in title_count.most_common(12):
        mv = sum(1 for s in done if pid in won[(s, "mvp")])
        cy = sum(1 for s in done if pid in won[(s, "cy_young")])
        sm.append(f"- {names.get(pid, pid)}: {n} titles (MVP x{mv}, Cy Young x{cy})")
    sm.append("")
    # do the leaders mostly overlap award winners?
    tot = len(leader_rows)
    ov = sum(1 for r in leader_rows if str(r["player_id"]) in won[(r["season"], "mvp")] | won[(r["season"], "cy_young")])
    sm.append(f"{ov} of {tot} leader titles ({ov / tot:.0%}) went to that season's MVP or Cy Young winner.")
    with open(os.path.join(OUT, "leaders_summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sm) + "\n")
    print("\n".join(md))
    print()
    print("\n".join(sm))


if __name__ == "__main__":
    main()

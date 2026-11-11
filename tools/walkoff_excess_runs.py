"""Walk-off plays where more runs scored than the game needed.

A walk-off ends the game the moment the winning run scores, so on a non-HR walk-off (single, double, walk, sacrifice,
GORA...) any further runners are irrelevant in real life, yet the log credits every run on the play. This lists every
walk-off play whose final margin is 2 or more, i.e. runs that did not matter (a HR is excluded: all its runs really do
score). Scores are rebuilt by adding up each play's `runs`, so the pre- / post-play score convention never matters.

    python tools/walkoff_excess_runs.py

Reads docs/data (built site data); writes tools/walkoff_excess_runs_out/walkoffs.csv.
"""
import csv
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import accomplishment_frequency as af  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "walkoff_excess_runs_out")


def main():
    by_game = defaultdict(list)
    for p in af.load_plays():
        by_game[p["game_code"]].append(p)
    rows = []
    for game, ps in by_game.items():
        ps.sort(key=lambda p: p["play_num"])
        home = away = 0
        for p in ps:
            before_home, before_away = home, away
            r = p["runs"] or 0
            if p["batting_is_home"]:
                home += r
            else:
                away += r
            if "walk_off" not in (p.get("tags") or []):
                continue
            needed = max(before_away - before_home + 1, 0)   # runs that win it from the score before the play
            rows.append({
                "game": game, "season": p["_season"], "play_num": p["play_num"], "result": p["result"],
                "batter": p["batter_name"], "inning": p["inning"], "home_before": before_home, "away_before": before_away,
                "runs_on_play": r, "runs_needed": needed, "excess": max(r - max(needed, 1), 0),
                "final": f"{away}-{home}", "margin": home - away,
            })
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "walkoffs.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    non_hr = [r for r in rows if r["result"] != "HR"]
    over = [r for r in non_hr if r["margin"] >= 2]
    print(f"{len(rows)} walk-offs, {len(non_hr)} not HR; {len(over)} of those finish with a margin of 2+ "
          f"({len(over) / len(non_hr):.0%}), {sum(r['excess'] for r in over)} runs that could not have mattered")
    print("by result:", dict(Counter(r["result"] for r in over).most_common()))
    print("by season:", dict(sorted(Counter(r["season"] for r in over).items())))
    print("excess runs per play:", dict(sorted(Counter(r["excess"] for r in over).items())))
    for r in rows:
        if r["game"] == "090107" and r["play_num"] % 1000 == 116 % 1000 or r["play_num"] == 90107116:
            print("example:", r)


if __name__ == "__main__":
    main()

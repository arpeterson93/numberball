"""Step 3 of 4 for the Immaculate Innings artifact (see build_artifact.py).
All aggregation behind the page: reads data/all_plays_raw.json and data/bases_empty_k.json,
writes data/final_artifact_data.json."""
import json
from collections import defaultdict, Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

rows = json.load(open(DATA / "all_plays_raw.json"))
rows = [r for r in rows if r.get("game_type") != "scrimmage" and r.get("result") and r.get("league") == "MLN"]

# AutoK is an automatic strikeout call with no real pitch/swing roll (diff is
# ~0 by construction, not a genuine near-miss - see excluded_autok below), so
# it's held out of every strikeout-based figure on the page, not just the
# diff-based ones. KCS never appears in this dataset but is included for
# completeness since it is a genuine roll-based strikeout.
STRIKEOUT_CODES = {"K", "KCS"}
ALL_K_CODES = STRIKEOUT_CODES | {"AutoK"}  # informational only, see excluded_autok
NON_TURN_CODES = {"SB", "SB2", "SB3", "SB4", "SB32", "SB42", "SB43", "SB432",
                   "CS", "CS2", "CS3", "CS4", "Balk"}

groups = defaultdict(list)
for r in rows:
    groups[(r["game_id"], r["inning"], r["half"])].append(r)

total_half_innings = len(groups)

# half-innings per season (denominator for rate-normalized season chart)
half_innings_by_season = defaultdict(int)
half_innings_by_inning = defaultdict(int)
for (gid, inning, half), g in groups.items():
    r0 = g[0]
    half_innings_by_season[(r0["league"], r0["season"])] += 1
    half_innings_by_inning[inning] += 1

# leadoff strikeout-streak metric
streak_counts = Counter()
for key, g in groups.items():
    turns = sorted([r for r in g if r["result"] not in NON_TURN_CODES], key=lambda r: r["play_num"])
    streak = 0
    for r in turns:
        if r["result"] in STRIKEOUT_CODES:
            streak += 1
        else:
            break
    for n in (1, 2, 3):
        if streak >= n:
            streak_counts[n] += 1

# informational only: half-innings that would qualify if AutoK counted as a
# real strikeout, but don't once it's held out (used only for the footnote)
excluded_autok = 0
for (game_id, inning, half), g in groups.items():
    turns = sorted([r for r in g if r["result"] not in NON_TURN_CODES], key=lambda r: r["play_num"])
    if len(turns) != 3 or not all(r["result"] in ALL_K_CODES for r in turns):
        continue
    pids = set(r.get("pitcher_id") for r in turns)
    pnames = set(r.get("pitcher_name") for r in turns)
    if len(pids) != 1 or len(pnames) != 1:
        continue
    if any(r["result"] == "AutoK" for r in turns):
        excluded_autok += 1

# immaculate half-innings: exactly 3 batters, all genuine strikeouts, same pitcher, nothing else
occurrences = []
for (game_id, inning, half), g in groups.items():
    turns = sorted([r for r in g if r["result"] not in NON_TURN_CODES], key=lambda r: r["play_num"])
    if len(turns) != 3 or not all(r["result"] in STRIKEOUT_CODES for r in turns):
        continue
    pids = set(r.get("pitcher_id") for r in turns)
    pnames = set(r.get("pitcher_name") for r in turns)
    if len(pids) != 1 or len(pnames) != 1:
        continue
    if any(r.get("diff") is None for r in turns):
        continue
    r0 = turns[0]
    diffs = [r["diff"] for r in turns]
    game_code = r0.get("game_code")
    play_idx = r0["play_num"] % 1000
    occurrences.append({
        "game_id": game_id,
        "game_code": game_code,
        "season": r0.get("season"),
        "league": r0.get("league"),
        "inning": inning,
        "half": half,
        "pitcher_name": r0.get("pitcher_name"),
        "pitcher_id": r0.get("pitcher_id"),
        "team": r0.get("def_team"),
        "batting_team": r0.get("off_team"),
        "diffs": diffs,
        "avg_diff": round(sum(diffs) / len(diffs), 1),
        "min_diff": min(diffs),
        "gameday_url": f"https://mlngameday.com/?game={game_code}&play={play_idx}",
    })

occurrences.sort(key=lambda o: o["min_diff"])

by_pitcher = defaultdict(list)
for o in occurrences:
    by_pitcher[o["pitcher_id"]].append(o)
pitcher_agg = []
for pid, occs in by_pitcher.items():
    mins = [o["min_diff"] for o in occs]
    # display name: the name attached to this pitcher_id's most recent (highest
    # season) occurrence, since a human id can carry more than one name over time
    name = sorted(occs, key=lambda o: o["season"])[-1]["pitcher_name"]
    pitcher_agg.append({
        "pitcher_id": pid,
        "pitcher_name": name,
        "count": len(occs),
        "avg_min_diff": round(sum(mins) / len(mins), 1),
        "best_min_diff": max(mins),
        "closest_min_diff": min(mins),
        "seasons": sorted(set(o["season"] for o in occs)),
    })
pitcher_agg.sort(key=lambda p: (-p["count"], -p["avg_min_diff"]))

by_team = defaultdict(list)
for o in occurrences:
    by_team[o["team"]].append(o)
team_agg = []
for team, occs in by_team.items():
    mins = [o["min_diff"] for o in occs]
    team_agg.append({
        "team": team,
        "count": len(occs),
        "avg_min_diff": round(sum(mins) / len(mins), 1),
    })
team_agg.sort(key=lambda t: -t["count"])

by_batting_team = defaultdict(list)
for o in occurrences:
    by_batting_team[o["batting_team"]].append(o)
batting_team_agg = []
for team, occs in by_batting_team.items():
    mins = [o["min_diff"] for o in occs]
    batting_team_agg.append({
        "team": team,
        "count": len(occs),
        "avg_min_diff": round(sum(mins) / len(mins), 1),
    })
batting_team_agg.sort(key=lambda t: -t["count"])

by_season = defaultdict(list)
for o in occurrences:
    by_season[(o["league"], o["season"])].append(o)
season_agg = []
for (league, season), occs in by_season.items():
    mins = [o["min_diff"] for o in occs]
    denom = half_innings_by_season.get((league, season), 0)
    season_agg.append({
        "league": league,
        "season": season,
        "count": len(occs),
        "avg_min_diff": round(sum(mins) / len(mins), 1),
        "half_innings": denom,
        "rate_per_1000": round(1000 * len(occs) / denom, 2) if denom else None,
    })
season_agg.sort(key=lambda s: (s["league"], s["season"] if s["season"] is not None else -1))

by_inning = defaultdict(int)
for o in occurrences:
    by_inning[o["inning"]] += 1
inning_agg = []
for inning in sorted(set(list(by_inning.keys()) + list(half_innings_by_inning.keys()))):
    denom = half_innings_by_inning.get(inning, 0)
    cnt = by_inning.get(inning, 0)
    inning_agg.append({
        "inning": inning,
        "count": cnt,
        "half_innings": denom,
        "rate_per_1000": round(1000 * cnt / denom, 2) if denom else None,
    })

min_diffs = [o["min_diff"] for o in occurrences]
lo_bound = min(min_diffs)
BIN = 10
bins = list(range(lo_bound, 500, BIN))
hist = []
for b_lo in bins:
    b_hi = min(b_lo + BIN, 500)
    c = sum(1 for m in min_diffs if b_lo <= m < b_hi or (b_hi == 500 and m == 500))
    hist.append({"bin_lo": b_lo, "bin_hi": b_hi, "count": c})

survival = []
for x in range(lo_bound, 501, 1):
    c = sum(1 for m in min_diffs if m > x)
    survival.append({"x": x, "count": c, "pct": round(100 * c / len(min_diffs), 2)})

# ------------------------------------------------------------------
# "Clean strikeout" threshold: every genuine (non-AutoK) strikeout with
# no runners on base before the pitch, season 5+, MLN only. Compared
# against the real-MLB rate of 3-pitch strikeouts (3pK / (L/SO+S/SO),
# baseball-reference.com league pitching-pitches totals) to find the MLN
# diff value above which a strikeout is as "clean" as a real 3-pitch K.
mlb_3pk_rate_by_year = {
    2021: round(7533 / (9701 + 32331), 4),
    2022: round(6981 / (9168 + 31614), 4),
    2023: round(7257 / (9601 + 32160), 4),
    2024: round(7112 / (9762 + 31388), 4),
    2025: round(6915 / (9395 + 31213), 4),
    2026: round(6955 / (9440 + 30754), 4),
}
mlb_avg_rate = sum(mlb_3pk_rate_by_year.values()) / len(mlb_3pk_rate_by_year)

bases_empty_k = json.load(open(DATA / "bases_empty_k.json"))
clean_by_season = defaultdict(list)
for r in bases_empty_k:
    clean_by_season[r["season"]].append(r["diff"])
clean_all = [d for diffs in clean_by_season.values() for d in diffs]

clean_lo = min(clean_all)
n_all = len(clean_all)
desc = sorted(clean_all, reverse=True)
threshold = desc[round(mlb_avg_rate * n_all) - 1]

def survival_for(diffs, lo, hi):
    n = len(diffs)
    return [
        {"x": x, "pct": round(100 * sum(1 for d in diffs if d > x) / n, 2)}
        for x in range(lo, hi + 1)
    ]

clean_k = {
    "mlb_3pk_rate_by_year": mlb_3pk_rate_by_year,
    "mlb_avg_rate": round(mlb_avg_rate, 4),
    "threshold": threshold,
    "lo": clean_lo,
    "combined": {
        "n": n_all,
        "pct_at_or_above_threshold": round(100 * sum(1 for d in clean_all if d >= threshold) / n_all, 2),
        "survival": survival_for(clean_all, clean_lo, 500),
    },
    "by_season": {
        str(season): {
            "n": len(diffs),
            "pct_at_or_above_threshold": round(100 * sum(1 for d in diffs if d >= threshold) / len(diffs), 2),
            "survival": survival_for(diffs, clean_lo, 500),
        }
        for season, diffs in sorted(clean_by_season.items())
    },
}

output = {
    "total_half_innings": total_half_innings,
    "streak_ge1": streak_counts[1],
    "streak_ge2": streak_counts[2],
    "streak_ge3": streak_counts[3],
    "excluded_autok": excluded_autok,
    "occurrences": occurrences,
    "pitcher_agg": pitcher_agg,
    "team_agg": team_agg,
    "batting_team_agg": batting_team_agg,
    "season_agg": season_agg,
    "inning_agg": inning_agg,
    "histogram": hist,
    "survival": survival,
    "min_diff_lo": min(min_diffs),
    "min_diff_hi": 500,
    "clean_k": clean_k,
}

with open(DATA / "final_artifact_data.json", "w") as f:
    json.dump(output, f)

print("total_half_innings", total_half_innings)
print("occurrences", len(occurrences))
print("streaks", streak_counts[1], streak_counts[2], streak_counts[3])
print("closest (lowest min_diff):", occurrences[0])
print("most dominant (highest min_diff):", occurrences[-1])
print("season_agg", season_agg)
print("inning_agg", inning_agg)

"""Build maturity_all_seasons.json from the fetched Supabase CSVs
(data/mln_plays_full.csv, data/mln_teams_wl.csv, data/players_rows.csv).

Run fetch_plays.py / fetch_teams.py / fetch_players.py first to refresh those.

Usage:
    python build_data.py
"""
import math
import os
import pandas as pd
import json
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
PLAYS_PATH = os.path.join(DATA_DIR, "mln_plays_full.csv")
PLAYERS_PATH = os.path.join(DATA_DIR, "players_rows.csv")
TEAMS_WL_PATH = os.path.join(DATA_DIR, "mln_teams_wl.csv")
OUT_PATH = os.path.join(DATA_DIR, "maturity_all_seasons.json")

# (abbrev, season) -> (wins, losses), from the Supabase teams table
_wl_df = pd.read_csv(TEAMS_WL_PATH, dtype={"abbrev": str, "season": int, "wins": "Int64", "losses": "Int64"})
WL_LOOKUP = {
    (row.abbrev, row.season): (row.wins, row.losses)
    for row in _wl_df.itertuples()
}

# Season-by-season abbreviation per franchise (S01..S12), from saved franchise-history memory.
# Seasons beyond S12 reuse the S12 abbrev until a new rebrand is recorded here.
FRANCHISE_SEASON_ABBREV = {
    "Acadia Peregrines":       ["ACP"] * 12,
    "Gas House Gorillas":      ["GHG"] * 12,
    "R'Lyeh Ancients":         ["MAL", "MAL", "MAL", "MAL", "MAL", "MAL", "MAL", "OGO", "OGO", "OGO", "RLY", "RLY"],
    "Portland Pioneers":       [None, "POR", "POR", "POR", "POR", "POR", "POR", "POR", "POR", "POR", "POR", "POR"],
    "Aruba Sea Serpents":      ["CIN", "CIN", "CIN", "CIN", "ASS", "ASS", "ASS", "ASS", "ASS", "ASS", "ASS", "ASS"],
    "Humongous Melonheads":    ["HMH"] * 12,
    "Reykjavik Valkyries":     [None, "REK", "REK", "REK", "OO", "OO", "REK", "REK", "REK", "REK", "REK", "REK"],
    "Sopher McDophers":        ["SMD"] * 12,
    "Baldur's Gate Beholders": ["LPJ", "LPJ", "BBG", "BBG", "BBG", "BBG", "BBG", "BBG", "BBEG", "BBEG", "BBEG", "BBEG"],
    "Halifax Highlanders":     ["VAN", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX", "HFX"],
    "Sunnydale Slayers":       ["SUN"] * 12,
    "Ursa Major Grizzlies":    [None, None, "GRZ", "GRZ", "GRZ", "GRZ", "GRZ", "GRZ", "GRZ", "GRZ", "GRZ", "GRZ"],
    "Carolina Reapers":        ["KYM", "KYM", "KYM", "KYM", "KYM", "KYM", "KYM", "CAR", "CAR", "CAR", "CAR", "CAR"],
    "Kansas City Kitties":     ["OAK", "SHH", "SHH", "BUF", "BUF", "BUF", "BUF", "BUF", "KC", "KC", "KC", "KC"],
    "Miami Fuego":             ["BFB", "BFB", "BFB", "BFB", "BFB", "BFB", "BFB", "BFB", "MIA", "MIA", "MIA", "MIA"],
    "Raccoon City Outbreak":   [None, None, "NO", "NO", "NO", "NO", "NO", "NO", "NO", "RC", "RC", "RC"],
}


def season_abbrev(team_name: str, season: int) -> str:
    row = FRANCHISE_SEASON_ABBREV.get(team_name)
    if row is None:
        return team_name[:3].upper()
    idx = min(season - 1, 11)  # seasons beyond S12 reuse the S12 (index 11) entry
    abbrev = row[idx]
    return abbrev or team_name[:3].upper()

# ---------- name resolution: most recent known name per player_id ----------
players = pd.read_csv(PLAYERS_PATH, dtype=str)
players = players[players["player_id"].notna() & (players["player_id"] != "")]
players = players[players["season"].notna()]
players["season_int"] = players["season"].astype(int)
players_sorted = players.sort_values("season_int")
name_map = players_sorted.groupby("player_id")["name"].last().to_dict()

# per (player_id, season) roster status/team, from the players table (MLN only) - used to tell
# a released/cut player (shows "Free Agent" or team="FA" while still nominally active) apart from
# a genuine retirement/hiatus, for players who stop appearing in plays after a given season.
players_mln = players[players["league"] == "MLN"]
player_status_by_season = {
    (row.player_id, row.season_int): row.status
    for row in players_mln.itertuples()
}
player_team_by_season = {
    (row.player_id, row.season_int): row.team
    for row in players_mln.itertuples()
}


def same_season_departure(player_id, season):
    """If this player's OWN roster row for THIS season already shows they'd left (not
    Active/Captain on a real team), return the departure category - "cut" (Free Agent/Banned)
    or "retired"/"hiatus" (Retired / Hiatus / GM Only). Returns None if they're still shown
    Active/Captain on a real team that season (or there's no roster row at all), meaning
    they finished the season rostered and their fate should be judged going into next season."""
    status = player_status_by_season.get((player_id, season))
    team = player_team_by_season.get((player_id, season))
    if status in ("Free Agent", "Banned"):
        return "cut"
    if status == "Retired":
        return "retired"
    if status in ("Hiatus", "GM Only"):
        return "hiatus"
    if team == "FA" and status not in ("Active", "Captain"):
        return "cut"  # FA team tag without a recognized status - still clearly not rostered
    return None


def next_season_status(player_id, leaving_after, leaving_league, leaving_unknown, season):
    """Classify what happened to a player after this team-season:
    "same_team" / "traded" (still active, elsewhere) / "cut" (released, Free Agent/Banned) /
    "retired" / "hiatus" (Retired / Hiatus / GM Only) - or None if not yet knowable.

    First checks whether the player's OWN roster row for THIS season already shows a mid-season
    departure (knowable even for the season still in progress) before falling back to whether
    they're rostered anywhere entering next season - judged from that very next season only, not
    backfilled with a later season's status."""
    same_season = same_season_departure(player_id, season)
    if same_season is not None:
        return same_season
    if leaving_unknown:
        return None
    if not leaving_after:
        return "same_team"
    if not leaving_league:
        return "traded"
    status_next = player_status_by_season.get((player_id, season + 1))
    team_next = player_team_by_season.get((player_id, season + 1))
    if status_next == "Retired":
        return "retired"
    if status_next in ("Hiatus", "GM Only"):
        return "hiatus"
    if status_next in ("Free Agent", "Banned") or (team_next == "FA" and status_next in ("Active", "Captain")):
        return "cut"
    return "retired"  # unknown/missing status - default to the pre-existing assumption

# ---------- plays ----------
plays = pd.read_csv(PLAYS_PATH, dtype=str)
plays["season"] = plays["season"].astype(int)
plays["id"] = plays["id"].astype(int)

LAST_SEASON = int(plays["season"].max())

# The active (last) season is only a few games in - many players haven't logged a play yet, so
# plays-derived data alone would wrongly look incomplete for them. Roster status (players_rows:
# Active/Captain, real team) fills that gap for the season still in progress.
abbrev_to_franchise_last = {season_abbrev(name, LAST_SEASON): name for name in FRANCHISE_SEASON_ABBREV}
roster_last = players[
    (players["league"] == "MLN")
    & (players["season_int"] == LAST_SEASON)
    & (players["status"].isin(["Active", "Captain"]))
    & (players["team"] != "FA")
].copy()
roster_last["franchise"] = roster_last["team"].map(abbrev_to_franchise_last)
roster_last = roster_last.dropna(subset=["franchise"])
roster_last_by_team = {
    key: set(g["player_id"])
    for key, g in roster_last.groupby("franchise")
}

pitch_side = plays[["pitcher_id", "def_team", "season"]].rename(
    columns={"pitcher_id": "player_id", "def_team": "team"}
)
bat_side = plays[["batter_id", "off_team", "season"]].rename(
    columns={"batter_id": "player_id", "off_team": "team"}
)
appearances = pd.concat([pitch_side, bat_side], ignore_index=True)
appearances = appearances.dropna(subset=["player_id", "team"])
appearances = appearances[appearances["player_id"] != "0"]  # sentinel/placeholder id, not a real player
appearances = appearances.drop_duplicates(subset=["player_id", "team", "season"])

# career-facing stats (franchise tenure, career franchise count, stint lengths) also need this
# season's roster fallback so a player's brand-new, not-yet-played team still counts - kept as a
# SEPARATE copy so appearances/season_sets/team_season_players (which drive leaving/retention
# stats sitewide) stay exactly plays-based, unchanged from today's behavior
_roster_last_rows = pd.DataFrame(
    [{"player_id": pid, "team": team_name, "season": LAST_SEASON}
     for team_name, pid_set in roster_last_by_team.items() for pid in pid_set]
)
appearances_with_current_roster = pd.concat([appearances, _roster_last_rows], ignore_index=True)
appearances_with_current_roster = appearances_with_current_roster.drop_duplicates(subset=["player_id", "team", "season"])

# first play id (lower id = earlier in time) a player has for a given team/season -
# used to order multi-team seasons (trades) chronologically instead of alphabetically
pitch_side_id = plays[["id", "pitcher_id", "def_team", "season"]].rename(
    columns={"pitcher_id": "player_id", "def_team": "team"}
)
bat_side_id = plays[["id", "batter_id", "off_team", "season"]].rename(
    columns={"batter_id": "player_id", "off_team": "team"}
)
appearances_with_id = pd.concat([pitch_side_id, bat_side_id], ignore_index=True).dropna(subset=["player_id", "team"])
appearances_with_id = appearances_with_id[appearances_with_id["player_id"] != "0"]
first_id_map = appearances_with_id.groupby(["player_id", "team", "season"])["id"].min().to_dict()


def first_id(player_id, team, season):
    return first_id_map.get((player_id, team, season), float("inf"))

# ---------- PA / BF retention (workload-weighted, "Steal" rows excluded - not a PA/BF) ----------
swings = plays[plays["play_type"] == "Swing"]
pa_rows = swings[["batter_id", "off_team", "season"]].rename(columns={"batter_id": "player_id", "off_team": "team"}).dropna()
bf_rows = swings[["pitcher_id", "def_team", "season"]].rename(columns={"pitcher_id": "player_id", "def_team": "team"}).dropna()

batters_by_team_season = {key: set(g["player_id"]) for key, g in pa_rows.groupby(["team", "season"])}
pitchers_by_team_season = {key: set(g["player_id"]) for key, g in bf_rows.groupby(["team", "season"])}


def retention_counts(rows_df, team_name, season, returning_lookup):
    """(retained, total) PA/BF rows from team_name's (season - 1), where "retained" means
    that row's player also appeared for team_name in `season`."""
    prior = rows_df[(rows_df["team"] == team_name) & (rows_df["season"] == season - 1)]
    if len(prior) == 0:
        return 0, 0
    returning = returning_lookup.get((team_name, season), set())
    retained = int(prior["player_id"].isin(returning).sum())
    return retained, len(prior)


def build_retention_profiles(observations):
    """observations: iterable of (tenure, leaving_after, leaving_unknown, vet_cutoff) tuples -
    "did a player AT this tenure level stick around next season?", pooled over many team-seasons.
    Rows with an unknown future (the active season) are excluded. Returns (by_tenure, by_bucket)."""
    tenure_totals, tenure_retained = {}, {}
    bucket_totals = {"rookie": 0, "other": 0, "vet": 0}
    bucket_retained = {"rookie": 0, "other": 0, "vet": 0}
    for tenure, leaving_after, leaving_unknown, vet_cutoff in observations:
        if leaving_unknown:
            continue
        stayed = not leaving_after
        tenure_totals[tenure] = tenure_totals.get(tenure, 0) + 1
        tenure_retained[tenure] = tenure_retained.get(tenure, 0) + (1 if stayed else 0)
        bucket = "rookie" if tenure == 0 else ("vet" if tenure >= vet_cutoff else "other")
        bucket_totals[bucket] += 1
        bucket_retained[bucket] += 1 if stayed else 0

    by_tenure = [
        {"tenure": t, "retained": tenure_retained[t], "total": tenure_totals[t]}
        for t in sorted(tenure_totals)
    ]
    by_bucket = [
        {"bucket": b, "retained": bucket_retained[b], "total": bucket_totals[b]}
        for b in ["rookie", "other", "vet"]
    ]
    return by_tenure, by_bucket


def pooled_retention(retained_sum, total_sum):
    return round(retained_sum / total_sum * 100, 1) if total_sum else None


def pooled_median(values):
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2


# distinct seasons played per player_id (for tenure lookups), any team
season_sets = appearances.groupby("player_id")["season"].apply(lambda s: sorted(set(s))).to_dict()


def tenure_entering(player_id, season):
    """Prior seasons played anywhere in the league (league-wide experience)."""
    seasons = season_sets.get(player_id, [])
    return sum(1 for s in seasons if s < season)


# distinct seasons played per (player_id, team) - for franchise-specific tenure. Stints don't need
# to be contiguous: 2 years, a 3-year gap, then back = still 2 prior seasons with that franchise.
# Keyed by team NAME (not the season abbrev), so this is correct across rebrands automatically.
# Uses the roster-fallback-augmented appearances so a player's brand-new, not-yet-played team
# this season still counts.
franchise_season_sets = appearances_with_current_roster.groupby(["player_id", "team"])["season"].apply(lambda s: sorted(set(s))).to_dict()


def franchise_tenure_entering(player_id, team_name, season):
    """Prior seasons played specifically for this franchise."""
    seasons = franchise_season_sets.get((player_id, team_name), [])
    return sum(1 for s in seasons if s < season)


# ---------- player history index (for the "Trace a Player" view) ----------
pa_counts = pa_rows.groupby(["player_id", "team", "season"]).size().reset_index(name="pa")
bf_counts = bf_rows.groupby(["player_id", "team", "season"]).size().reset_index(name="bf")
workload = pd.merge(pa_counts, bf_counts, on=["player_id", "team", "season"], how="outer")
workload["pa"] = workload["pa"].fillna(0).astype(int)
workload["bf"] = workload["bf"].fillna(0).astype(int)
workload = workload[workload["player_id"] != "0"]  # sentinel/placeholder id, not a real player

# same active-season roster fallback as appearances_with_current_roster - a player just signed to
# a team this season who hasn't recorded a PA/BF yet should still show up in their By Player trace
_roster_last_workload_rows = pd.DataFrame(
    [{"player_id": pid, "team": team_name, "season": LAST_SEASON, "pa": 0, "bf": 0}
     for team_name, pid_set in roster_last_by_team.items() for pid in pid_set]
)
workload = pd.concat([workload, _roster_last_workload_rows], ignore_index=True)
workload = workload.drop_duplicates(subset=["player_id", "team", "season"], keep="first")

player_history = {}
for pid, g in workload.groupby("player_id"):
    seasons_list = []
    for season_num, sg in g.groupby("season"):
        sorted_rows = sorted(sg.itertuples(), key=lambda row: first_id(pid, row.team, int(season_num)))
        teams_list = [
            {
                "team": row.team,
                "abbrev": season_abbrev(row.team, int(season_num)),
                "pa": int(row.pa),
                "bf": int(row.bf),
            }
            for row in sorted_rows
        ]
        seasons_list.append({
            "season": int(season_num),
            "teams": teams_list,
            "tenure": tenure_entering(pid, int(season_num)),
        })
    seasons_list.sort(key=lambda x: x["season"])
    player_history[pid] = {"name": name_map.get(pid, pid), "history": seasons_list}


def veteran_cutoff(season: int) -> int:
    """Dynamic 'veteran' tenure threshold, ~50% of the seasons possible so far.
    Floored at 1 so nobody can be a veteran with zero career history (season 1)."""
    return max(1, math.ceil((season - 1) * 0.5))


# per (team, season) -> set of player_ids on that team that season, for arrival/departure checks
team_season_players = {
    key: set(g["player_id"])
    for key, g in appearances.groupby(["team", "season"])
}

# roster_last_by_team (computed above, alongside appearances) fills in the active season's
# not-yet-played rosters
for team_name, pid_set in roster_last_by_team.items():
    team_season_players[(team_name, LAST_SEASON)] = pid_set

def compute_tenure_stats(players_list, tenure_key, vet_cutoff):
    """Box-and-whisker style stats for one team-season, for whichever tenure metric
    (global "tenure" or franchise-specific "franchise_tenure") is requested."""
    tenures = [p[tenure_key] for p in players_list]
    n = len(tenures)
    if n == 0:
        return {"median": 0, "mean": 0, "q1": 0, "q3": 0, "max": 0, "min": 0,
                "rookie_count": 0, "veteran_count": 0, "veteran_cutoff": vet_cutoff}
    s = sorted(tenures)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2
    tseries = pd.Series(tenures)
    return {
        "median": median,
        "mean": round(sum(tenures) / n, 2),
        "q1": round(float(tseries.quantile(0.25)), 2),
        "q3": round(float(tseries.quantile(0.75)), 2),
        "max": max(tenures),
        "min": min(tenures),
        "rookie_count": sum(1 for t in tenures if t == 0),
        "veteran_count": sum(1 for t in tenures if t >= vet_cutoff),
        "veteran_cutoff": vet_cutoff,
    }


all_seasons_out = []
for season in range(1, LAST_SEASON + 1):
    season_apps = appearances[appearances["season"] == season]
    vet_cutoff = veteran_cutoff(season)

    if season == LAST_SEASON:
        team_names_this_season = sorted(roster_last_by_team.keys())
        player_ids_by_team = roster_last_by_team
        teams_by_player = {}
        for team_name, pid_set in roster_last_by_team.items():
            for pid in pid_set:
                teams_by_player.setdefault(pid, []).append(team_name)
    else:
        team_names_this_season = sorted(season_apps["team"].unique())
        player_ids_by_team = {t: set(g["player_id"]) for t, g in season_apps.groupby("team")}
        teams_by_player = {
            pid: sorted(set(g["team"]), key=lambda t: first_id(pid, t, season))
            for pid, g in season_apps.groupby("player_id")
        }

    teams_out = []
    for team_name in team_names_this_season:
        player_ids = sorted(player_ids_by_team[team_name])
        players_list = []
        prev_roster = team_season_players.get((team_name, season - 1), set())
        next_roster = team_season_players.get((team_name, season + 1), set())
        leaving_unknown = season >= LAST_SEASON

        for pid in player_ids:
            tenure = tenure_entering(pid, season)
            franchise_tenure = franchise_tenure_entering(pid, team_name, season)
            player_teams_ordered = teams_by_player.get(pid, [team_name])
            other_teams = [t for t in player_teams_ordered if t != team_name]
            # for a player traded mid-season, only their LAST team that season "counts" for
            # per-person movement stats - same_team/traded is judged against that final team
            is_last_team_this_season = team_name == player_teams_ordered[-1]
            leaving_after = (not leaving_unknown) and (pid not in next_roster)
            still_in_league_next = (season + 1) in season_sets.get(pid, [])
            leaving_league = leaving_after and not still_in_league_next
            next_status = next_season_status(pid, leaving_after, leaving_league, leaving_unknown, season)
            players_list.append({
                "player_id": pid,
                "name": name_map.get(pid, pid),
                "tenure": tenure,
                "franchise_tenure": franchise_tenure,
                "traded": len(other_teams) > 0,
                "other_teams": [season_abbrev(t, season) for t in other_teams],
                "new_to_team": pid not in prev_roster,
                "leaving_after": leaving_after,
                "leaving_league": leaving_league,
                "leaving_unknown": leaving_unknown,
                "next_status": next_status,
                "is_last_team_this_season": is_last_team_this_season,
            })
        players_list.sort(key=lambda p: (-p["tenure"], p["name"]))

        league_stats = compute_tenure_stats(players_list, "tenure", vet_cutoff)
        franchise_stats = compute_tenure_stats(players_list, "franchise_tenure", vet_cutoff)

        team_abbrev = season_abbrev(team_name, season)
        wins, losses = WL_LOOKUP.get((team_abbrev, season), (None, None))
        pa_retained, pa_total = retention_counts(pa_rows, team_name, season, batters_by_team_season)
        bf_retained, bf_total = retention_counts(bf_rows, team_name, season, pitchers_by_team_season)
        pa_retention = round(pa_retained / pa_total * 100, 1) if pa_total else None
        bf_retention = round(bf_retained / bf_total * 100, 1) if bf_total else None
        combined_total = pa_total + bf_total
        combined_retention = round((pa_retained + bf_retained) / combined_total * 100, 1) if combined_total else None
        teams_out.append({
            "abbrev": team_abbrev,
            "name": team_name,
            "wins": None if pd.isna(wins) else int(wins),
            "losses": None if pd.isna(losses) else int(losses),
            "pa_retention": pa_retention,
            "bf_retention": bf_retention,
            "combined_retention": combined_retention,
            "pa_retained": pa_retained,
            "pa_total": pa_total,
            "bf_retained": bf_retained,
            "bf_total": bf_total,
            "players": players_list,
            "n": len(players_list),
            "metrics": {
                "league": league_stats,
                "franchise": franchise_stats,
            },
        })

    teams_out.sort(key=lambda t: (-t["metrics"]["league"]["median"], -t["metrics"]["league"]["mean"]))

    distinct_players_season = sorted(set().union(*player_ids_by_team.values())) if player_ids_by_team else []
    traded_count = sum(1 for pid in distinct_players_season if len(teams_by_player.get(pid, [])) > 1)

    # "league" metric: distinct players league-wide (a traded player counted once), using their
    # league-wide tenure - matches how many real people actually played this season.
    all_tenures_league = [tenure_entering(pid, season) for pid in distinct_players_season]
    n_league = len(all_tenures_league)
    s_league = sorted(all_tenures_league)
    median_league = (
        (s_league[n_league // 2] if n_league % 2 == 1 else (s_league[n_league // 2 - 1] + s_league[n_league // 2]) / 2)
        if n_league else 0
    )

    # "franchise" metric: pooled straight from each team's own roster, so a traded player is
    # counted once per team (same convention as "traded players show up in both teams' charts") -
    # this keeps rookie+tweener+vet always summing to n_players for whichever metric is active.
    all_tenures_franchise = [p["franchise_tenure"] for t in teams_out for p in t["players"]]
    n_franchise = len(all_tenures_franchise)
    s_franchise = sorted(all_tenures_franchise)
    median_franchise = (
        (s_franchise[n_franchise // 2] if n_franchise % 2 == 1 else (s_franchise[n_franchise // 2 - 1] + s_franchise[n_franchise // 2]) / 2)
        if n_franchise else 0
    )

    def best_worst_team(metric_key):
        if not teams_out:
            return None, None
        best = max(teams_out, key=lambda t: (t["metrics"][metric_key]["median"], t["metrics"][metric_key]["mean"]))
        worst = min(teams_out, key=lambda t: (t["metrics"][metric_key]["median"], t["metrics"][metric_key]["mean"]))
        return best["abbrev"], worst["abbrev"]

    most_league, least_league = best_worst_team("league")
    most_franchise, least_franchise = best_worst_team("franchise")

    league = {
        "season": season,
        "n_teams": len(teams_out),
        "traded_count": traded_count,
        "metrics": {
            "league": {
                "n_players": n_league,
                "mean": round(sum(all_tenures_league) / n_league, 2) if n_league else 0,
                "median": median_league,
                "rookie_count": sum(1 for t in all_tenures_league if t == 0),
                "veteran_count": sum(1 for t in all_tenures_league if t >= vet_cutoff),
                "veteran_cutoff": vet_cutoff,
                "max_tenure": max(all_tenures_league) if all_tenures_league else 0,
                "most_seasoned_team": most_league,
                "least_seasoned_team": least_league,
            },
            "franchise": {
                "n_players": n_franchise,
                "mean": round(sum(all_tenures_franchise) / n_franchise, 2) if n_franchise else 0,
                "median": median_franchise,
                "rookie_count": sum(1 for t in all_tenures_franchise if t == 0),
                "veteran_count": sum(1 for t in all_tenures_franchise if t >= vet_cutoff),
                "veteran_cutoff": vet_cutoff,
                "max_tenure": max(all_tenures_franchise) if all_tenures_franchise else 0,
                "most_seasoned_team": most_franchise,
                "least_seasoned_team": least_franchise,
            },
        },
    }

    # league-wide TOTAL column: pools every team's roster this season into one retention-by-tenure profile
    season_observations_league = [
        (p["tenure"], p["leaving_after"], p["leaving_unknown"], t["metrics"]["league"]["veteran_cutoff"])
        for t in teams_out for p in t["players"]
    ]
    season_observations_franchise = [
        (p["franchise_tenure"], p["leaving_after"], p["leaving_unknown"], t["metrics"]["franchise"]["veteran_cutoff"])
        for t in teams_out for p in t["players"]
    ]
    league_by_tenure, league_by_bucket = build_retention_profiles(season_observations_league)
    league_by_tenure_fr, league_by_bucket_fr = build_retention_profiles(season_observations_franchise)

    league_pa_retention = pooled_retention(sum(t["pa_retained"] for t in teams_out), sum(t["pa_total"] for t in teams_out))
    league_bf_retention = pooled_retention(sum(t["bf_retained"] for t in teams_out), sum(t["bf_total"] for t in teams_out))
    league_combined_retained = sum(t["pa_retained"] + t["bf_retained"] for t in teams_out)
    league_combined_total = sum(t["pa_total"] + t["bf_total"] for t in teams_out)
    league_combined_retention = pooled_retention(league_combined_retained, league_combined_total)

    league_total = {
        "abbrev": "TOTAL",
        "name": "League Total",
        "wins": None,
        "losses": None,
        "pa_retention": league_pa_retention,
        "bf_retention": league_bf_retention,
        "combined_retention": league_combined_retention,
        "metrics": {
            "league": {"median": median_league, "retention_by_tenure": league_by_tenure, "retention_by_bucket": league_by_bucket},
            "franchise": {"median": median_franchise, "retention_by_tenure": league_by_tenure_fr, "retention_by_bucket": league_by_bucket_fr},
        },
    }

    all_seasons_out.append({"league": league, "teams": teams_out, "league_total": league_total})
    print(f"season {season}: {len(teams_out)} teams, {n_league} distinct players, {traded_count} traded")

# ---------- per-franchise TOTAL column: pools every season into one retention-by-tenure profile ----------
team_names_all = {t["name"] for s in all_seasons_out for t in s["teams"]}
team_totals = {}
for team_name in team_names_all:
    wins_sum = losses_sum = 0
    pa_retained_sum = pa_total_sum = bf_retained_sum = bf_total_sum = 0
    all_tenures_league = []
    all_tenures_franchise = []
    observations_league = []
    observations_franchise = []
    for s in all_seasons_out:
        for t in s["teams"]:
            if t["name"] != team_name:
                continue
            if t["wins"] is not None:
                wins_sum += t["wins"]
            if t["losses"] is not None:
                losses_sum += t["losses"]
            pa_retained_sum += t["pa_retained"]
            pa_total_sum += t["pa_total"]
            bf_retained_sum += t["bf_retained"]
            bf_total_sum += t["bf_total"]
            for p in t["players"]:
                all_tenures_league.append(p["tenure"])
                all_tenures_franchise.append(p["franchise_tenure"])
                observations_league.append((p["tenure"], p["leaving_after"], p["leaving_unknown"], t["metrics"]["league"]["veteran_cutoff"]))
                observations_franchise.append((p["franchise_tenure"], p["leaving_after"], p["leaving_unknown"], t["metrics"]["franchise"]["veteran_cutoff"]))

    by_tenure_l, by_bucket_l = build_retention_profiles(observations_league)
    by_tenure_f, by_bucket_f = build_retention_profiles(observations_franchise)
    team_totals[team_name] = {
        "abbrev": "TOTAL",
        "name": team_name,
        "wins": wins_sum,
        "losses": losses_sum,
        "pa_retention": pooled_retention(pa_retained_sum, pa_total_sum),
        "bf_retention": pooled_retention(bf_retained_sum, bf_total_sum),
        "combined_retention": pooled_retention(pa_retained_sum + bf_retained_sum, pa_total_sum + bf_total_sum),
        "metrics": {
            "league": {"median": pooled_median(all_tenures_league), "retention_by_tenure": by_tenure_l, "retention_by_bucket": by_bucket_l},
            "franchise": {"median": pooled_median(all_tenures_franchise), "retention_by_tenure": by_tenure_f, "retention_by_bucket": by_bucket_f},
        },
    }

# ---------- career-wide distributions for the League tab (all / active / retired-hiatus) ----------
# "active" = logged a play this (current) season, or currently rostered Active/Captain on a real team
active_via_plays = set(appearances[appearances["season"] == LAST_SEASON]["player_id"])
active_via_roster = set()
for _pid_set in roster_last_by_team.values():
    active_via_roster |= _pid_set
active_player_ids = active_via_plays | active_via_roster

# "retired/hiatus" = not active, and their most recent known roster status says so
latest_status_by_pid = {}
for _row in players_mln.sort_values("season_int").itertuples():
    latest_status_by_pid[_row.player_id] = _row.status  # later rows overwrite -> ends up latest
retired_hiatus_player_ids = {
    pid for pid, status in latest_status_by_pid.items()
    if status in ("Retired", "Hiatus", "Banned", "GM Only") and pid not in active_player_ids
}

# per-player raw values, computed once, then filtered per scope below
franchise_count_by_pid = appearances_with_current_roster.groupby("player_id")["team"].apply(lambda t: len(set(t))).to_dict()
career_length_by_pid = appearances_with_current_roster.groupby("player_id")["season"].apply(lambda s: len(set(s))).to_dict()

# stint lengths: for each (player, franchise), split their seasons into contiguous runs -
# a non-contiguous return to the same franchise starts a new stint rather than extending the old one
stint_lengths_by_pid = {}
for (_pid, _team), _seasons in franchise_season_sets.items():
    if not _seasons:
        continue
    lengths = []
    prev = _seasons[0]
    length = 1
    for s in _seasons[1:]:
        if s == prev + 1:
            length += 1
        else:
            lengths.append(length)
            length = 1
        prev = s
    lengths.append(length)
    stint_lengths_by_pid.setdefault(_pid, []).extend(lengths)


def _dist_and_stats(values):
    dist = [{"value": v, "count": c} for v, c in sorted(Counter(values).items())]
    mean = round(sum(values) / len(values), 2) if values else 0
    return dist, mean, pooled_median(values)


def _build_career_block(pid_filter):
    fc_vals = [v for pid, v in franchise_count_by_pid.items() if pid_filter is None or pid in pid_filter]
    cl_vals = [v for pid, v in career_length_by_pid.items() if pid_filter is None or pid in pid_filter]
    sl_vals = [
        length for pid, lengths in stint_lengths_by_pid.items()
        if pid_filter is None or pid in pid_filter for length in lengths
    ]
    fc_dist, fc_mean, fc_median = _dist_and_stats(fc_vals)
    cl_dist, cl_mean, cl_median = _dist_and_stats(cl_vals)
    sl_dist, sl_mean, sl_median = _dist_and_stats(sl_vals)
    return {
        "player_count": len(fc_vals),
        "franchise_count_dist": fc_dist, "franchise_count_mean": fc_mean, "franchise_count_median": fc_median,
        "career_length_dist": cl_dist, "career_length_mean": cl_mean, "career_length_median": cl_median,
        "stint_length_dist": sl_dist, "stint_length_mean": sl_mean, "stint_length_median": sl_median,
    }


league_career = {
    "all": _build_career_block(None),
    "active": _build_career_block(active_player_ids),
    "retired_hiatus": _build_career_block(retired_hiatus_player_ids),
}

with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(
        {
            "seasons": all_seasons_out, "players": player_history, "team_totals": team_totals,
            "league_career": league_career,
        },
        f, separators=(",", ":"),
    )

print("wrote", OUT_PATH)
print("player_history entries:", len(player_history))
print("team_totals entries:", len(team_totals))

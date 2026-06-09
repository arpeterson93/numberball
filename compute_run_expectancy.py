"""
Compute run expectancy distributions from MLN play-by-play data.

Walk-off half-innings (last bottom-half inning of each game) are excluded
because they are truncated - the home team stops batting once the winning run
scores, so not all run-scoring potential is realized.

Outputs:
  run_expectancy_distribution.csv    - P(runs) for each (outs, brc) state
  run_expectancy_verification.csv    - compare computed means to run_expectancy_matrix.csv
  runs_per_half_inning_by_season.csv - distribution of total runs/half-inning by season
"""

import pandas as pd

BRC_LABEL = {
    0: "Empty", 1: "1B", 2: "2B", 3: "3B",
    4: "1&2B", 5: "1&3B", 6: "2&3B", 7: "BL",
}

# ── Load data ──────────────────────────────────────────────────────────────────

COLS = ["Game", "Inning", "Play", "Outs", "BRC", "Runs"]

archive = pd.read_excel(
    "MLN Historical Archive.xlsx",
    sheet_name="Converted Play Log",
    usecols=["Season"] + COLS,
)
archive["Season"] = archive["Season"].astype(int)

current = pd.read_excel(
    "MLN Export Tables.xlsx",
    sheet_name="Plays (Converted)",
    usecols=COLS,
)
current["Season"] = 12

df = (
    pd.concat([archive, current], ignore_index=True)
    .dropna(subset=["Outs", "BRC", "Runs"])
    .assign(
        Outs=lambda d: d["Outs"].astype(int),
        BRC=lambda d:  d["BRC"].astype(int),
        Runs=lambda d: d["Runs"].astype(int),
    )
    .sort_values(["Game", "Inning", "Play"])
    .reset_index(drop=True)
)

print(f"Combined plays  : {len(df):,}")
print(f"Half-innings    : {df.groupby(['Game','Inning']).ngroups:,}")
print(f"Seasons         : {sorted(df['Season'].unique())}")

# ── Exclude walk-off half-innings ──────────────────────────────────────────────
# The last half-inning of each game where the home team bats (bottom / "B")
# is excluded because it may be truncated by a walk-off.

last_play_per_game = (
    df.groupby("Game")["Play"]
    .max()
    .reset_index()
    .merge(df[["Game", "Play", "Inning"]], on=["Game", "Play"])
)
walkoff_keys = {
    (row.Game, row.Inning)
    for row in last_play_per_game.itertuples()
    if str(row.Inning).startswith("B")
}

df = df[~df.apply(lambda r: (r["Game"], r["Inning"]) in walkoff_keys, axis=1)].copy()

print(f"After walk-off exclusion: {len(df):,} plays, "
      f"{df.groupby(['Game','Inning']).ngroups:,} half-innings "
      f"({len(walkoff_keys):,} removed)")

# ── Runs from each play to end of half-inning ──────────────────────────────────

df["runs_from_here"] = (
    df.groupby(["Game", "Inning"])["Runs"]
    .transform(lambda s: s[::-1].cumsum()[::-1])
)

print(f"Max runs from a state: {df['runs_from_here'].max()}")

# ── Distribution by (Outs, BRC) ────────────────────────────────────────────────

dist = (
    df.groupby(["Outs", "BRC", "runs_from_here"])
    .size()
    .reset_index(name="count")
)
state_totals = dist.groupby(["Outs", "BRC"])["count"].transform("sum")
dist["pct"] = dist["count"] / state_totals
dist["obc"] = dist["BRC"].map(BRC_LABEL)
dist = dist.rename(columns={"runs_from_here": "runs_scored"})
dist = dist[["Outs", "BRC", "obc", "runs_scored", "count", "pct"]].sort_values(
    ["Outs", "BRC", "runs_scored"]
)

dist.to_csv("run_expectancy_distribution.csv", index=False)
print("\nSaved: run_expectancy_distribution.csv")

# ── Verification against run_expectancy_matrix.csv ────────────────────────────

computed_mean = (
    dist.groupby(["Outs", "BRC", "obc"])
    .apply(lambda g: (g["runs_scored"] * g["pct"]).sum(), include_groups=False)
    .reset_index(name="computed_mean")
)
computed_n = (
    dist.groupby(["Outs", "BRC"])["count"].sum().reset_index(name="n_observations")
)
computed_mean = computed_mean.merge(computed_n, on=["Outs", "BRC"])

provided = pd.read_csv("run_expectancy_matrix.csv")

verify = provided.merge(
    computed_mean.rename(columns={"Outs": "outs", "obc": "obc_check"}),
    left_on=["outs", "obc"],
    right_on=["outs", "obc_check"],
    how="left",
).drop(columns=["obc_check"])
verify["diff"] = (verify["computed_mean"] - verify["expected_runs"]).round(4)
verify["computed_mean"] = verify["computed_mean"].round(4)

verify.to_csv("run_expectancy_verification.csv", index=False)
print("Saved: run_expectancy_verification.csv")

print("\nVerification (expected vs computed mean):")
print(verify[["outs", "obc", "expected_runs", "computed_mean", "diff", "n_observations"]].to_string(index=False))

# ── Season summary: distribution of total runs per half-inning ─────────────────

half_innings = (
    df.groupby(["Season", "Game", "Inning"])["Runs"]
    .sum()
    .reset_index(name="runs_scored")
)

season_dist = (
    half_innings.groupby(["Season", "runs_scored"])
    .size()
    .unstack(fill_value=0)
    .reset_index()
)
season_dist.columns = (
    ["season"] + [f"runs_{c}" for c in season_dist.columns[1:]]
)

season_stats = (
    half_innings.groupby("Season")["runs_scored"]
    .agg(half_innings="count", mean_runs="mean", median_runs="median", std_runs="std")
    .reset_index()
    .rename(columns={"Season": "season"})
    .round(4)
)

season_out = season_stats.merge(season_dist, on="season")
season_out.to_csv("runs_per_half_inning_by_season.csv", index=False)
print("\nSaved: runs_per_half_inning_by_season.csv")

print("\nSeason summary:")
print(season_out[["season", "half_innings", "mean_runs", "median_runs"]].to_string(index=False))

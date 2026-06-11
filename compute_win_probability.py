"""
Compute win probability lookup table from MLN play-by-play data.

State: (remaining_half_innings, outs, obc, batting_lead)
  remaining_half_innings: 1-12; extras cap at 2 (top half) or 1 (bottom half)
  outs: 0, 1, 2
  obc: 8 binary baserunner codes "000" ... "111"
  batting_lead: runs ahead (+) or behind (-) from batting team's view, clipped to +/-10

Three-tier smoothing:
  Tier 1 (n >= MIN_N_FULL):   empirical WP Bayesian-blended with logistic prior
  Tier 2 (sparse, RE-bin n >= MIN_N_RE):  RE-collapsed empirical + prior blend
  Tier 3 (very sparse):       logistic prior only

Two isotonic smoothing passes:
  Pass 1 - batting_lead: within each (remaining, outs, obc) bucket, WP must be
           non-decreasing with batting_lead (more runs ahead = higher WP).
  Pass 2 - OBC by RE:    within each (remaining, outs, batting_lead) bucket, WP
           must be non-decreasing as OBC expected runs increases (better base
           state = higher WP). OBC ordering uses outs-specific RE values.
  Pass 3 - batting_lead again: re-enforces lead monotonicity after OBC pass.

Requires: scikit-learn  (pip install scikit-learn)
Output:   win_probability_table.csv
"""

import pandas as pd
import numpy as np
from itertools import product
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression

# ── Constants ─────────────────────────────────────────────────────────────────

INNINGS       = 6
MAX_REMAINING = INNINGS * 2   # 12 half-innings in regulation
MARGIN_CLIP   = 10
MIN_N_FULL    = 10            # min obs for tier-1 empirical
MIN_N_RE      = 5             # min obs for tier-2 RE-collapsed
SHRINK_K      = 20            # Bayesian shrinkage weight (= N equivalent prior obs)

BRC_TO_OBC = {
    0: "000", 1: "001", 2: "010", 3: "100",
    4: "011", 5: "101", 6: "110", 7: "111",
}

# run_expectancy_matrix.csv uses label format for obc
OBC_LABEL_TO_BIN = {
    "Empty": "000", "1B": "001", "2B": "010", "3B": "100",
    "1&2B": "011", "1&3B": "101", "2&3B": "110", "BL": "111",
}

# ── Load play data ─────────────────────────────────────────────────────────────

archive = pd.read_excel(
    "MLN Historical Archive.xlsx",
    sheet_name="Converted Play Log",
    usecols=["Season", "Game", "Inning", "Play", "Outs", "BRC", "Runs"],
)
archive["Season"] = archive["Season"].astype(int)

current = pd.read_excel(
    "MLN Export Tables.xlsx",
    sheet_name="Plays (Converted)",
    usecols=["Game", "Inning", "Play", "Outs", "BRC", "Runs"],
)
current["Season"] = 12

df = (
    pd.concat([archive, current], ignore_index=True)
    .dropna(subset=["Outs", "BRC", "Runs", "Inning", "Game"])
    .assign(
        outs = lambda d: d["Outs"].astype(int),
        brc  = lambda d: d["BRC"].astype(int),
        runs = lambda d: d["Runs"].astype(int),
        game = lambda d: d["Game"].astype(int),
    )
    .drop(columns=["Outs", "BRC", "Runs", "Game"])
    .sort_values(["game", "Play"])
    .reset_index(drop=True)
)

print(f"Loaded {len(df):,} plays from {df['game'].nunique():,} games")

# ── Parse inning into (number, half) ─────────────────────────────────────────

def _parse_inning(val):
    s = str(val).strip().upper()
    half = "bottom" if s.startswith("B") else "top"
    try:
        num = int(s[1:])
    except ValueError:
        num = 1
    return num, half

_parsed    = df["Inning"].map(_parse_inning)
df["inn"]  = [p[0] for p in _parsed]
df["half"] = [p[1] for p in _parsed]

# ── Reconstruct running scores from Runs column ───────────────────────────────
# Top-half runs -> away; bottom-half runs -> home.
# Score entering each play = cumulative runs before this play within the game.

df["_adelta"] = df["runs"].where(df["half"] == "top",    0)
df["_hdelta"] = df["runs"].where(df["half"] == "bottom", 0)
df["a_score"] = df.groupby("game")["_adelta"].cumsum() - df["_adelta"]
df["h_score"] = df.groupby("game")["_hdelta"].cumsum() - df["_hdelta"]

# ── Game outcomes ─────────────────────────────────────────────────────────────
# Final score = entering score of last play + that play's runs.

last = df.groupby("game").last().reset_index()
last["fa"] = last["a_score"] + last["_adelta"]
last["fh"] = last["h_score"] + last["_hdelta"]
last = last[last["fa"] != last["fh"]]   # drop any tied/incomplete games
last["home_won"] = (last["fh"] > last["fa"]).astype(int)

game_outcome = last.set_index("game")["home_won"]
df["home_won"] = df["game"].map(game_outcome)
df = df.dropna(subset=["home_won"]).copy()
df["home_won"] = df["home_won"].astype(int)

n_games = df["game"].nunique()
print(f"After outcome join: {len(df):,} plays from {n_games:,} completed games")
print(f"  Home win rate: {last['home_won'].mean():.3f}")

# ── State variables ───────────────────────────────────────────────────────────

# half_innings_played: 0 = top of 1st, 11 = bottom of 6th, 12+ = extras
df["hip"] = (df["inn"] - 1) * 2 + (df["half"] == "bottom").astype(int)

# remaining_half_innings: counts down 12->1 in regulation;
# extras cap at 2 (top) or 1 (bottom) since we don't know how long they'll run
reg_rem   = MAX_REMAINING - df["hip"]
extra_min = df["half"].map({"top": 2, "bottom": 1})
df["remaining"] = np.where(reg_rem > 0, reg_rem, extra_min).astype(int)

# batting_lead: positive = batting team is ahead
df["batting_lead"] = np.where(
    df["half"] == "top",
    df["a_score"] - df["h_score"],
    df["h_score"] - df["a_score"],
).clip(-MARGIN_CLIP, MARGIN_CLIP).astype(int)

# did batting team win?
df["batting_won"] = np.where(
    df["half"] == "top", 1 - df["home_won"], df["home_won"]
).astype(int)

# obc binary string from brc integer
df["obc"] = df["brc"].map(BRC_TO_OBC)

print(f"\nRemaining half-innings distribution:\n{df['remaining'].value_counts().sort_index().to_string()}")

# ── Run expectancy bins for RE-collapsed fallback ─────────────────────────────

re_raw = pd.read_csv("run_expectancy_matrix.csv")
re_lookup = {
    (int(r["outs"]), OBC_LABEL_TO_BIN.get(str(r["obc"]), str(r["obc"]))): float(r["expected_runs"])
    for _, r in re_raw.iterrows()
}
df["re_bin"] = [
    round(re_lookup.get((o, b), 0.5) * 2) / 2
    for o, b in zip(df["outs"], df["obc"])
]

# ── Fit logistic prior on (batting_lead, remaining) ──────────────────────────

lr = LogisticRegression(max_iter=500, C=1.0)
lr.fit(df[["batting_lead", "remaining"]].values.astype(float), df["batting_won"].values)
print(f"\nLogistic prior: lead coef={lr.coef_[0][0]:.4f}  remaining coef={lr.coef_[0][1]:.4f}")
print(f"  Prior accuracy on training data: {(lr.predict(df[['batting_lead','remaining']].values) == df['batting_won'].values).mean():.3f}")

# ── Pre-aggregate empirical stats ─────────────────────────────────────────────

full_grp = (
    df.groupby(["remaining", "outs", "obc", "batting_lead"])["batting_won"]
    .agg(emp_wp="mean", n="count")
    .reset_index()
)
re_grp = (
    df.groupby(["remaining", "re_bin", "batting_lead"])["batting_won"]
    .agg(re_emp_wp="mean", re_n="count")
    .reset_index()
)

# ── Build full state grid ─────────────────────────────────────────────────────

grid = pd.DataFrame(
    list(product(
        range(1, MAX_REMAINING + 1),
        [0, 1, 2],
        list(BRC_TO_OBC.values()),
        range(-MARGIN_CLIP, MARGIN_CLIP + 1),
    )),
    columns=["remaining", "outs", "obc", "batting_lead"],
)

grid["prior_wp"] = lr.predict_proba(
    grid[["batting_lead", "remaining"]].values.astype(float)
)[:, 1]

grid["re_bin"] = [
    round(re_lookup.get((o, b), 0.5) * 2) / 2
    for o, b in zip(grid["outs"], grid["obc"])
]

grid = (
    grid
    .merge(full_grp, on=["remaining", "outs", "obc", "batting_lead"], how="left")
    .merge(re_grp,   on=["remaining", "re_bin", "batting_lead"],       how="left")
)
grid["n"]    = grid["n"].fillna(0).astype(int)
grid["re_n"] = grid["re_n"].fillna(0).astype(int)

# ── Apply three-tier Bayesian blending ───────────────────────────────────────

n      = grid["n"].values
re_n   = grid["re_n"].values
emp    = grid["emp_wp"].fillna(0.0).values
re_emp = grid["re_emp_wp"].fillna(0.0).values
prior  = grid["prior_wp"].values

blend_full = (n * emp    + SHRINK_K * prior) / (n    + SHRINK_K)
blend_re   = (re_n * re_emp + SHRINK_K * prior) / (re_n + SHRINK_K)

grid["win_prob"] = np.where(n    >= MIN_N_FULL, blend_full,
                   np.where(re_n >= MIN_N_RE,   blend_re, prior))
grid["method"]   = np.where(n    >= MIN_N_FULL, "empirical",
                   np.where(re_n >= MIN_N_RE,   "re_collapsed", "prior"))
grid["n_samples"] = n
grid["n_used"]    = np.where(n >= MIN_N_FULL, n, np.where(re_n >= MIN_N_RE, re_n, 0))

# ── Pass 1: Isotonic on batting_lead ─────────────────────────────────────────
# Within each (remaining, outs, obc) bucket WP must be non-decreasing with lead.

ir = IsotonicRegression(increasing=True, out_of_bounds="clip")
parts = []
for _, grp in grid.groupby(["remaining", "outs", "obc"]):
    g = grp.sort_values("batting_lead").copy()
    g["win_prob"] = np.clip(
        ir.fit_transform(g["batting_lead"].values.astype(float), g["win_prob"].values),
        0.001, 0.999,
    )
    parts.append(g)

result = pd.concat(parts, ignore_index=True)

# ── Pass 2: Isotonic on OBC ordered by expected runs ─────────────────────────
# Within each (remaining, outs, batting_lead) bucket WP must be non-decreasing
# as OBC expected runs increase. RE values are outs-specific from re_lookup.

obc_re = {
    (o, b): re_lookup.get((o, b), 0.0)
    for o in [0, 1, 2]
    for b in BRC_TO_OBC.values()
}

parts2 = []
for (rem, o, lead), grp in result.groupby(["remaining", "outs", "batting_lead"]):
    g = grp.copy()
    g["_re"] = g["obc"].apply(lambda b: obc_re.get((int(o), b), 0.0))
    g = g.sort_values("_re")
    g["win_prob"] = np.clip(
        ir.fit_transform(g["_re"].values.astype(float), g["win_prob"].values),
        0.001, 0.999,
    )
    parts2.append(g.drop(columns=["_re"]))

result = pd.concat(parts2, ignore_index=True)

# ── Pass 3: Re-enforce batting_lead monotonicity after OBC smoothing ──────────

parts3 = []
for _, grp in result.groupby(["remaining", "outs", "obc"]):
    g = grp.sort_values("batting_lead").copy()
    g["win_prob"] = np.clip(
        ir.fit_transform(g["batting_lead"].values.astype(float), g["win_prob"].values),
        0.001, 0.999,
    )
    parts3.append(g)

result = pd.concat(parts3, ignore_index=True)

# ── Save ──────────────────────────────────────────────────────────────────────

out_cols = ["remaining", "outs", "obc", "batting_lead", "win_prob", "n_samples", "n_used", "method"]
out = result[out_cols].copy()
out["obc"] = out["obc"].str.zfill(3)   # preserve leading zeros ("000", "001", etc.)
out.to_csv("win_probability_table.csv", index=False)

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\nSaved: win_probability_table.csv ({len(result):,} rows)")
print(f"\nMethod breakdown:\n{result['method'].value_counts().to_string()}")

print("\nSanity check - 0 outs, bases empty, bottom of 6th (remaining=1):")
chk = result[(result["remaining"] == 1) & (result["outs"] == 0) & (result["obc"] == "000")]
print(chk[["batting_lead", "win_prob", "n_samples", "method"]].to_string(index=False))

print("\nSanity check - tied game (lead=0), 0 outs, bases empty, by inning:")
chk2 = result[(result["batting_lead"] == 0) & (result["outs"] == 0) & (result["obc"] == "000")]
print(chk2[["remaining", "win_prob", "n_samples", "method"]].sort_values("remaining", ascending=False).to_string(index=False))

print("\nSanity check - OBC monotonicity (remaining=6, outs=1, lead=0, all 8 OBC states):")
chk3 = result[(result["remaining"] == 6) & (result["outs"] == 1) & (result["batting_lead"] == 0)].copy()
chk3["re"] = chk3["obc"].apply(lambda b: obc_re.get((1, b), 0.0))
print(chk3[["obc", "re", "win_prob", "n_samples", "method"]].sort_values("re").to_string(index=False))

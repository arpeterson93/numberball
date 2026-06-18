"""
Compute win probability lookup table from MLN play-by-play data.

State: (remaining_half_innings, outs, obc, batting_lead)
  remaining_half_innings: 1-12; extras cap at 2 (top half) or 1 (bottom half)
  outs: 0, 1, 2
  obc: 8 binary baserunner codes "000" ... "111"
  batting_lead: runs ahead (+) or behind (-) from batting team's view, clipped to +/-10

Prior construction (two-layer):
  Layer 1 - Base level: empirical Numberball WP aggregated across all outs/OBC for each
            (remaining, batting_lead), Bayesian-blended with a logistic regression.
            This anchors the absolute probability level to Numberball data.
  Layer 2 - Gregstoll relativities: log-odds shift from Greg Stoll's MLB win expectancy
            data (github.com/gregstoll/baseballstats, Retrosheet 1903-2025) capturing
            how each (outs, obc) state shifts WP vs. bases-empty 0-outs at the same lead.
            Our remaining=R maps to MLB inning 9-(R-1)//2 (remaining=12 ~ MLB top 4th,
            remaining=1 ~ MLB bottom 9th). States with fewer than GREG_MIN_TOTAL samples
            in the gregstoll data fall back to logistic only.

  Full prior = expit(logit(base_wp) + gregstoll_rel)

Three-tier cell blending:
  Tier 1 (n >= MIN_N_FULL):          cell empirical blended with prior
  Tier 2 (n < MIN_N_FULL, re_n >= MIN_N_RE): RE-collapsed empirical blended with prior
  Tier 3 (very sparse):              prior only

Four isotonic safety passes (backup for high-n empirical reversals):
  Pass 1 - batting_lead monotonicity (increasing)
  Pass 2 - OBC by RE ordering (increasing)
  Pass 3 - batting_lead re-enforcement
  Pass 4 - outs monotonicity (decreasing)

Provenance columns in output:
  prior_source  - "gregstoll" if Gregstoll had coverage, "logistic" if not
  tango_weight  - SHRINK_K / (n_used + SHRINK_K): share of blend from prior
  iso_delta     - how much isotonic passes moved win_prob (non-zero = reversal corrected)

Requires: scikit-learn, scipy  (pip install scikit-learn scipy)
Output:   win_probability_table.csv
"""

import pandas as pd
import numpy as np
from itertools import product
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from scipy.special import logit as _logit, expit as _expit

# ── Constants ──────────────────────────────────────────────────────────────────

INNINGS       = 6
MAX_REMAINING = INNINGS * 2   # 12 half-innings in regulation
MARGIN_CLIP   = 10
MIN_N_FULL    = 10            # min obs for tier-1 cell empirical
MIN_N_RE      = 5             # min obs for tier-2 RE-collapsed
SHRINK_K      = 20            # Bayesian shrinkage weight (= N equivalent prior obs)

BRC_TO_OBC = {
    0: "000", 1: "001", 2: "010", 3: "100",
    4: "011", 5: "101", 6: "110", 7: "111",
}

OBC_LABEL_TO_BIN = {
    "Empty": "000", "1B": "001", "2B": "010", "3B": "100",
    "1&2B": "011", "1&3B": "101", "2&3B": "110", "BL": "111",
}

# Maps our OBC binary string to gregstoll's (1B, 2B, 3B) runner tuple.
# Our bit order: "001"=1B only, "010"=2B only, "100"=3B only.
# Gregstoll runner tuple order: (1B_occupied, 2B_occupied, 3B_occupied).
OBC_TO_GREG = {
    "000": (0, 0, 0), "001": (1, 0, 0), "010": (0, 1, 0), "011": (1, 1, 0),
    "100": (0, 0, 1), "101": (1, 0, 1), "110": (0, 1, 1), "111": (1, 1, 1),
}

GREG_MIN_TOTAL = 20  # minimum gregstoll sample count to trust a state's WP

# ── Load Gregstoll win expectancy data ────────────────────────────────────────
# Source: github.com/gregstoll/baseballstats (Retrosheet 1903-2025)
# Key: (inning, isHome, outs, (1B, 2B, 3B), batting_lead)
# Value: (wins, total_games) -- WP = wins / total_games
# isHome: 1 = home team batting, 0 = visiting team batting
# batting_lead: runs ahead (+) or behind (-) from the batting team's view

import re as _re
_greg_pattern = _re.compile(
    r'\((\d+), (\d+), (\d+), \((\d+), (\d+), (\d+)\), (-?\d+)\): \((\d+), (\d+)\)'
)
GREG_WE: dict[tuple, tuple[int, int]] = {}
with open("gregstoll_win_expectancy.txt") as _gf:
    for _line in _gf:
        _m = _greg_pattern.match(_line.strip())
        if _m:
            _ing, _ih, _o, _b1, _b2, _b3, _rd, _w, _t = [int(x) for x in _m.groups()]
            GREG_WE[(_ing, _ih, _o, (_b1, _b2, _b3), _rd)] = (_w, _t)

print(f"Loaded {len(GREG_WE):,} Gregstoll WE entries")


def _greg_batting_wp(remaining: int, outs: int, obc: str, batting_lead: int) -> float | None:
    """
    Batting team WP from the Gregstoll MLB data for a given Numberball game state.
    Returns None if the state has fewer than GREG_MIN_TOTAL samples.
    Our remaining=R maps to MLB inning 9-(R-1)//2; odd remaining = home batting.
    """
    if remaining > MAX_REMAINING:
        return None
    mlb_inn = 9 - (remaining - 1) // 2
    runners = OBC_TO_GREG.get(obc, (0, 0, 0))
    is_home = 1 if remaining % 2 == 1 else 0  # odd = bottom half = home batting
    entry = GREG_WE.get((mlb_inn, is_home, outs, runners, batting_lead))
    if entry is None:
        return None
    wins, total = entry
    if total < GREG_MIN_TOTAL:
        return None
    return wins / total


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

# ── Reconstruct running scores ────────────────────────────────────────────────

df["_adelta"] = df["runs"].where(df["half"] == "top",    0)
df["_hdelta"] = df["runs"].where(df["half"] == "bottom", 0)
df["a_score"] = df.groupby("game")["_adelta"].cumsum() - df["_adelta"]
df["h_score"] = df.groupby("game")["_hdelta"].cumsum() - df["_hdelta"]

# ── Game outcomes ─────────────────────────────────────────────────────────────

last = df.groupby("game").last().reset_index()
last["fa"] = last["a_score"] + last["_adelta"]
last["fh"] = last["h_score"] + last["_hdelta"]
last = last[last["fa"] != last["fh"]]
last["home_won"] = (last["fh"] > last["fa"]).astype(int)

game_outcome = last.set_index("game")["home_won"]
df["home_won"] = df["game"].map(game_outcome)
df = df.dropna(subset=["home_won"]).copy()
df["home_won"] = df["home_won"].astype(int)

n_games = df["game"].nunique()
print(f"After outcome join: {len(df):,} plays from {n_games:,} completed games")
print(f"  Home win rate: {last['home_won'].mean():.3f}")

# ── State variables ───────────────────────────────────────────────────────────

df["hip"] = (df["inn"] - 1) * 2 + (df["half"] == "bottom").astype(int)

reg_rem   = MAX_REMAINING - df["hip"]
extra_min = df["half"].map({"top": 2, "bottom": 1})
df["remaining"] = np.where(reg_rem > 0, reg_rem, extra_min).astype(int)

df["batting_lead"] = np.where(
    df["half"] == "top",
    df["a_score"] - df["h_score"],
    df["h_score"] - df["a_score"],
).clip(-MARGIN_CLIP, MARGIN_CLIP).astype(int)

df["batting_won"] = np.where(
    df["half"] == "top", 1 - df["home_won"], df["home_won"]
).astype(int)

df["obc"] = df["brc"].map(BRC_TO_OBC)

print(f"\nRemaining half-innings distribution:\n{df['remaining'].value_counts().sort_index().to_string()}")

# ── Run expectancy bins (for RE-collapsed tier-2 fallback) ────────────────────

re_raw = pd.read_csv("run_expectancy_matrix.csv")
re_lookup = {
    (int(r["outs"]), OBC_LABEL_TO_BIN.get(str(r["obc"]), str(r["obc"]))): float(r["expected_runs"])
    for _, r in re_raw.iterrows()
}
df["re_bin"] = [
    round(re_lookup.get((o, b), 0.5) * 2) / 2
    for o, b in zip(df["outs"], df["obc"])
]

# ── Logistic prior on (batting_lead, remaining) ───────────────────────────────
# re_bin removed: OBC/outs ordering is now handled by Tango relativities, not by
# including RE in the regression.  The logistic provides the base-level fallback
# for |batting_lead| > 5 where Tango has no coverage.

lr = LogisticRegression(max_iter=500, C=1.0)
lr.fit(df[["batting_lead", "remaining"]].values.astype(float), df["batting_won"].values)
print(f"\nLogistic prior: lead coef={lr.coef_[0][0]:.4f}  remaining coef={lr.coef_[0][1]:.4f}")

# ── Pre-aggregate empirical stats ─────────────────────────────────────────────

# Cell-level: (remaining, outs, obc, batting_lead)
full_grp = (
    df.groupby(["remaining", "outs", "obc", "batting_lead"])["batting_won"]
    .agg(emp_wp="mean", n="count")
    .reset_index()
)

# RE-collapsed: (remaining, re_bin, batting_lead) - tier-2 fallback
re_grp = (
    df.groupby(["remaining", "re_bin", "batting_lead"])["batting_won"]
    .agg(re_emp_wp="mean", re_n="count")
    .reset_index()
)

# Base-level: (remaining, batting_lead) aggregated across all outs/OBC.
# This is the Numberball "anchor" - what we trust about the absolute WP level.
# Tango relativities then apply the outs/OBC shape on top.
base_grp = (
    df.groupby(["remaining", "batting_lead"])["batting_won"]
    .agg(base_emp="mean", n_base="count")
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

grid["re_bin"] = [
    round(re_lookup.get((o, b), 0.5) * 2) / 2
    for o, b in zip(grid["outs"], grid["obc"])
]

grid = (
    grid
    .merge(full_grp, on=["remaining", "outs", "obc", "batting_lead"], how="left")
    .merge(re_grp,   on=["remaining", "re_bin", "batting_lead"],       how="left")
    .merge(base_grp, on=["remaining", "batting_lead"],                 how="left")
)
grid["n"]      = grid["n"].fillna(0).astype(int)
grid["re_n"]   = grid["re_n"].fillna(0).astype(int)
grid["n_base"] = grid["n_base"].fillna(0).astype(int)

# ── Build structured prior ────────────────────────────────────────────────────

# Step 1: logistic base for each cell (used for |lead|>5 and as blend anchor)
logistic_base = lr.predict_proba(
    grid[["batting_lead", "remaining"]].values.astype(float)
)[:, 1]

# Step 2: base-level WP - Numberball aggregate blended with logistic.
# When n_base is large, Numberball dominates; when sparse, logistic takes over.
n_base_arr    = grid["n_base"].values
base_emp_arr  = grid["base_emp"].fillna(0.0).values
base_wp       = np.clip(
    (n_base_arr * base_emp_arr + SHRINK_K * logistic_base) / (n_base_arr + SHRINK_K),
    0.001, 0.999,
)

# Step 3: Gregstoll relativities - log-odds shift for each (outs, obc) vs. 0-out
# bases-empty at the same (remaining, batting_lead).  Encodes how much runners and
# outs shift WP per MLB history, to be applied on top of the Numberball base level.
def _greg_rel(remaining, outs, obc, batting_lead):
    wp  = _greg_batting_wp(remaining, outs,  obc,   batting_lead)
    ref = _greg_batting_wp(remaining, 0,     "000", batting_lead)
    if wp is None or ref is None:
        return 0.0, False
    wp  = max(0.001, min(0.999, wp))
    ref = max(0.001, min(0.999, ref))
    return float(_logit(wp) - _logit(ref)), True

_rels         = [_greg_rel(r, o, b, l)
                 for r, o, b, l in zip(grid["remaining"], grid["outs"],
                                       grid["obc"],       grid["batting_lead"])]
greg_rel_arr  = np.array([x[0] for x in _rels])
greg_cov_arr  = np.array([x[1] for x in _rels])

# Step 4: full prior = Numberball base level + Gregstoll OBC/outs shape
grid["prior_wp"]     = np.clip(_expit(_logit(base_wp) + greg_rel_arr), 0.001, 0.999)
grid["prior_source"] = np.where(greg_cov_arr, "gregstoll", "logistic")

# ── Three-tier Bayesian blending ──────────────────────────────────────────────

n      = grid["n"].values
re_n   = grid["re_n"].values
emp    = grid["emp_wp"].fillna(0.0).values
re_emp = grid["re_emp_wp"].fillna(0.0).values
prior  = grid["prior_wp"].values

blend_full = (n    * emp    + SHRINK_K * prior) / (n    + SHRINK_K)
blend_re   = (re_n * re_emp + SHRINK_K * prior) / (re_n + SHRINK_K)

grid["win_prob"]  = np.where(n    >= MIN_N_FULL, blend_full,
                    np.where(re_n >= MIN_N_RE,   blend_re, prior))
grid["method"]    = np.where(n    >= MIN_N_FULL, "empirical",
                    np.where(re_n >= MIN_N_RE,   "re_collapsed", "prior"))
grid["n_samples"] = n
grid["n_used"]    = np.where(n >= MIN_N_FULL, n, np.where(re_n >= MIN_N_RE, re_n, 0))

# Tango's share of the blend: 1.0 = pure prior, ~0.17 at n=100 with SHRINK_K=20
grid["tango_weight"] = SHRINK_K / (grid["n_used"].values.astype(float) + SHRINK_K)

# Snapshot before isotonic correction for iso_delta tracking
grid["pre_iso_wp"] = grid["win_prob"].copy()

# ── Soft isotonic helper ──────────────────────────────────────────────────────
# PAV (sklearn IsotonicRegression) pools reversed adjacent values to their exact
# mean.  After PAV, a min_gap pass enforces at least 5e-4 separation in the
# correct direction so pooled states are never identical in the output.

def _soft_iso(x: np.ndarray, y: np.ndarray, increasing: bool = True,
              min_gap: float = 5e-4, sample_weight=None) -> np.ndarray:
    ir = IsotonicRegression(increasing=increasing, out_of_bounds="clip")
    kw = {"sample_weight": sample_weight} if sample_weight is not None else {}
    out   = np.clip(ir.fit_transform(x, y, **kw), 0.001, 0.999)
    order = np.argsort(x)
    sb    = out[order]
    if increasing:
        for i in range(1, len(sb)):
            if sb[i] <= sb[i - 1]:
                sb[i] = min(sb[i - 1] + min_gap, 0.999)
    else:
        for i in range(1, len(sb)):
            if sb[i] >= sb[i - 1]:
                sb[i] = max(sb[i - 1] - min_gap, 0.001)
    out[order] = sb
    return out

# ── Pass 1: batting_lead monotonicity ────────────────────────────────────────

parts = []
for _, grp in grid.groupby(["remaining", "outs", "obc"]):
    g = grp.sort_values("batting_lead").copy()
    g["win_prob"] = _soft_iso(g["batting_lead"].values.astype(float), g["win_prob"].values)
    parts.append(g)
result = pd.concat(parts, ignore_index=True)

# ── Pass 2: OBC ordering by run expectancy ───────────────────────────────────

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
    w = (g["n_samples"].values + 1).astype(float)
    g["win_prob"] = _soft_iso(g["_re"].values.astype(float), g["win_prob"].values,
                              sample_weight=w)
    parts2.append(g.drop(columns=["_re"]))
result = pd.concat(parts2, ignore_index=True)

# ── Pass 3: re-enforce batting_lead monotonicity ──────────────────────────────

parts3 = []
for _, grp in result.groupby(["remaining", "outs", "obc"]):
    g = grp.sort_values("batting_lead").copy()
    g["win_prob"] = _soft_iso(g["batting_lead"].values.astype(float), g["win_prob"].values)
    parts3.append(g)
result = pd.concat(parts3, ignore_index=True)

# ── Pass 4: outs monotonicity (decreasing) ───────────────────────────────────

parts4 = []
for _, grp in result.groupby(["remaining", "obc", "batting_lead"]):
    g = grp.sort_values("outs").copy()
    g["win_prob"] = _soft_iso(g["outs"].values.astype(float), g["win_prob"].values,
                              increasing=False)
    parts4.append(g)
result = pd.concat(parts4, ignore_index=True)

# ── Pass 5: cross-half-inning consistency ─────────────────────────────────────
# Enforce WP(R, outs=2, obc, L) >= 1 - WP(R-1, 0, "000", -L) for all R >= 2.
#
# Rationale: after the 3rd out is recorded, the game transitions to
# (R-1, outs=0, bases empty, -L) for the OPPONENT.  From the current batting
# team's perspective that state is worth 1 - WP(R-1, 0, "000", -L).  Getting
# the 3rd out should never INCREASE the batting team's WP - i.e. the batting
# team should never be rooting to make an out.

_state_idx = {
    (int(r), int(o), str(b), int(l)): i
    for i, (r, o, b, l) in enumerate(zip(
        result["remaining"], result["outs"], result["obc"], result["batting_lead"]
    ))
}

wp5      = result["win_prob"].values.copy()
n_raised = 0

for _r in range(2, MAX_REMAINING + 1):
    for _obc in BRC_TO_OBC.values():
        for _lead in range(-MARGIN_CLIP, MARGIN_CLIP + 1):
            ref_i = _state_idx.get((_r - 1, 0, "000", -_lead))
            cur_i = _state_idx.get((_r,     2, _obc,  _lead))
            if ref_i is None or cur_i is None:
                continue
            floor_wp = 1.0 - wp5[ref_i]
            if wp5[cur_i] < floor_wp - 1e-9:
                wp5[cur_i] = min(floor_wp, 0.999)
                n_raised += 1

result["win_prob"] = wp5
print(f"\nPass 5 (cross-half-inning): {n_raised} cells raised to floor")

# Re-enforce outs monotonicity since raising outs=2 may have exceeded outs=1/0.
# PAV + min_gap will propagate any raised floor upward through outs=1 and outs=0.
parts5 = []
for _, grp in result.groupby(["remaining", "obc", "batting_lead"]):
    g = grp.sort_values("outs").copy()
    g["win_prob"] = _soft_iso(g["outs"].values.astype(float), g["win_prob"].values,
                              increasing=False)
    parts5.append(g)
result = pd.concat(parts5, ignore_index=True)

# ── Isotonic delta: how much correction was applied ───────────────────────────

result["iso_delta"] = (result["win_prob"] - result["pre_iso_wp"]).round(6)

# ── Save ──────────────────────────────────────────────────────────────────────

out_cols = [
    "remaining", "outs", "obc", "batting_lead",
    "win_prob",
    "n_samples", "n_used", "method",
    "prior_source", "tango_weight", "iso_delta",
]
out = result[out_cols].copy()
out["obc"] = out["obc"].str.zfill(3)
out.to_csv("win_probability_table.csv", index=False)

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\nSaved: win_probability_table.csv ({len(result):,} rows)")

print(f"\nMethod breakdown:\n{result['method'].value_counts().to_string()}")
print(f"\nPrior source breakdown:\n{result['prior_source'].value_counts().to_string()}")

iso_nonzero = (result["iso_delta"].abs() > 1e-6).sum()
print(f"\nIsotonic corrections applied: {iso_nonzero:,} rows ({iso_nonzero/len(result)*100:.1f}%)")
print(f"  Mean |iso_delta| where corrected: {result.loc[result['iso_delta'].abs()>1e-6,'iso_delta'].abs().mean():.4f}")

print("\nSanity check - tied game (lead=0), 0 outs, bases empty, by remaining:")
chk = result[(result["batting_lead"] == 0) & (result["outs"] == 0) & (result["obc"] == "000")]
print(chk[["remaining", "win_prob", "n_samples", "method", "prior_source", "tango_weight"]]
      .sort_values("remaining", ascending=False).to_string(index=False))

print("\nSanity check - OBC/outs monotonicity (remaining=6, batting_lead=0):")
chk2 = result[(result["remaining"] == 6) & (result["batting_lead"] == 0)].copy()
chk2["re"] = chk2["obc"].apply(lambda b: obc_re.get((int(chk2.loc[chk2["obc"]==b,"outs"].iloc[0]), b), 0.0))
print(chk2[["outs", "obc", "win_prob", "method", "prior_source", "tango_weight", "iso_delta"]]
      .sort_values(["outs", "obc"]).to_string(index=False))

"""
Analyze pitcher behavioral patterns from MLN play-by-play data.

Run from project root:  python analyze_pitcher_patterns.py
Output:  pitcher_patterns.csv   (per-pitcher summary)
         pitcher_patterns_by_result.csv  (post-result delta breakdown)

Patterns computed
-----------------
TRIPLET (3 consecutive pitches, same pitcher, same game, sorted by play#):
  span         - monotonically inc or dec (raw), max-min > 500
  reversal_ud  - up then down (raw linear deltas, no wraparound)
  reversal_du  - down then up (raw linear deltas, no wraparound)
  reversal     - either reversal type
  persistence  - same direction twice in a row (raw linear)
  echo         - p3 within 75 of p1 after p2 moved >= 200 away (raw)

PAIR (2 consecutive pitches, same pitcher, same game):
  wrap         - circular crossing of the 1000/1 boundary
  circ_delta   - mean absolute circular delta
  follow_batter- mean (next_pitch - last_swing) sign: + = chasing batter

GAME-LEVEL (per pitcher per game):
  pitch_range  - max - min of raw pitches in game

All rates are (event count) / (eligible opportunities).
"""

import pandas as pd
import numpy as np

# ── Helpers ───────────────────────────────────────────────────────────────────

def circ_delta(p1, p2):
    """Signed circular delta p1 -> p2 on 1-1000 scale, range -499..+500."""
    raw = (p2 - p1) % 1000
    return raw if raw <= 500 else raw - 1000

def circ_dist(p1, p2):
    """Unsigned circular distance on 1-1000 scale, range 0..500."""
    raw = abs(p2 - p1) % 1000
    return min(raw, 1000 - raw)

def is_wrap(p1, p2):
    """True if the move from p1 to p2 crosses the 1000/1 boundary (shorter arc)."""
    cd = circ_delta(p1, p2)
    ld = p2 - p1
    return cd != ld   # circular path differs from linear path -> wrap occurred

# ── Load data ─────────────────────────────────────────────────────────────────

PLAY_COLS = ["Season", "Game", "Play", "Inning", "Pitcher", "Batter",
             "Pitch", "Swing", "Result", "PlayType"]

print("Loading archive plays...")
try:
    arch = pd.read_excel("MLN Historical Archive.xlsx", sheet_name="Plays",
                         usecols=lambda c: c.strip() in PLAY_COLS)
    arch.columns = [c.strip() for c in arch.columns]
except Exception as e:
    print(f"  Warning: {e}")
    arch = pd.DataFrame(columns=PLAY_COLS)

print("Loading current season plays...")
try:
    curr = pd.read_excel("MLN Export Tables.xlsx", sheet_name="Plays (Raw)",
                         usecols=lambda c: c.strip() in PLAY_COLS)
    curr.columns = [c.strip() for c in curr.columns]
    if "Season" not in curr.columns:
        curr["Season"] = 12
except Exception as e:
    print(f"  Warning: {e}")
    curr = pd.DataFrame(columns=PLAY_COLS)

df = pd.concat([arch, curr], ignore_index=True)
df.columns = [c.strip() for c in df.columns]
print(f"Combined: {len(df):,} plays")

# Load player names from Rosters tab (archive)
name_lookup: dict[tuple, str] = {}
try:
    rosters = pd.read_excel("MLN Historical Archive.xlsx", sheet_name="Rosters")
    rosters.columns = [c.strip() for c in rosters.columns]
    sid_col  = next((c for c in rosters.columns if "S_ID" in c.upper()), None)
    name_col = next((c for c in rosters.columns if "FULL" in c.upper() and "NAME" in c.upper()), None)
    if sid_col and name_col:
        for _, r in rosters.dropna(subset=[sid_col, name_col]).iterrows():
            parts = str(r[sid_col]).split("_")
            if len(parts) == 2:
                try:
                    name_lookup[(int(parts[0]), int(parts[1]))] = str(r[name_col]).strip()
                except ValueError:
                    pass
        print(f"Loaded {len(name_lookup):,} player names from Rosters")
except Exception as e:
    print(f"  Could not load Rosters: {e}")

# ── Clean and filter ──────────────────────────────────────────────────────────

df["Pitch"]   = pd.to_numeric(df.get("Pitch"),  errors="coerce")
df["Swing"]   = pd.to_numeric(df.get("Swing"),  errors="coerce")
df["Play"]    = pd.to_numeric(df.get("Play"),   errors="coerce")
df["Season"]  = pd.to_numeric(df.get("Season"), errors="coerce")
df["Pitcher"] = pd.to_numeric(df.get("Pitcher"),errors="coerce")

# Keep only pitch plays with valid pitch values
play_type_col = "PlayType" if "PlayType" in df.columns else None
if play_type_col:
    df = df[~df[play_type_col].astype(str).str.lower().str.contains("steal", na=False)]
df = df[df["Pitch"].notna() & df["Pitcher"].notna() & df["Season"].notna()].copy()
df["Pitch"]   = df["Pitch"].astype(int)
df["Season"]  = df["Season"].astype(int)
df["Pitcher"] = df["Pitcher"].astype(int)
df = df.sort_values(["Game", "Play"]).reset_index(drop=True)

print(f"After filtering: {len(df):,} pitch plays, "
      f"{df['Game'].nunique():,} games, "
      f"{df[['Season','Pitcher']].drop_duplicates().shape[0]:,} pitcher-seasons")

# ── Build consecutive pairs within (Game, Pitcher) ───────────────────────────

df["_p1"] = df.groupby(["Game", "Pitcher"])["Pitch"].shift(1)
df["_p2"] = df.groupby(["Game", "Pitcher"])["Pitch"].shift(2)
df["_sw1"]= df.groupby(["Game", "Pitcher"])["Swing"].shift(1)   # last batter's swing

# Derived columns for pair-level patterns
has_pair  = df["_p1"].notna()
has_trip  = df["_p1"].notna() & df["_p2"].notna()
has_swing = has_pair & df["_sw1"].notna()

# Linear deltas (raw, no wraparound)
d1_lin = df["_p1"] - df["_p2"]        # p2 -> p1 (older pair)
d2_lin = df["Pitch"] - df["_p1"]      # p1 -> p3 (newer pair)
d_curr = df["Pitch"] - df["_p1"]      # current pair delta (same as d2_lin)

# ── Pattern flags ─────────────────────────────────────────────────────────────

# SPAN: mono inc or dec, raw span > 500
inc_trip = (df["Pitch"] > df["_p1"]) & (df["_p1"] > df["_p2"])
dec_trip = (df["Pitch"] < df["_p1"]) & (df["_p1"] < df["_p2"])
span_raw = df[["Pitch","_p1","_p2"]].max(axis=1) - df[["Pitch","_p1","_p2"]].min(axis=1)
df["_span"]       = has_trip & (inc_trip | dec_trip) & (span_raw > 500)
df["_span_inc"]   = has_trip & inc_trip & (span_raw > 500)
df["_span_dec"]   = has_trip & dec_trip & (span_raw > 500)

# REVERSAL: direction change (raw linear only)
df["_rev_ud"] = has_trip & (d1_lin > 0) & (d2_lin < 0)
df["_rev_du"] = has_trip & (d1_lin < 0) & (d2_lin > 0)
df["_rev"]    = df["_rev_ud"] | df["_rev_du"]

# PERSISTENCE: same direction twice
df["_persist"]= has_trip & (d1_lin > 0) & (d2_lin > 0) | has_trip & (d1_lin < 0) & (d2_lin < 0)

# ECHO: p3 within 75 of p1 after p2 moved >= 200 away
p1_dist = (df["_p1"] - df["_p2"]).abs()
p3_dist = (df["Pitch"] - df["_p2"]).abs()
df["_echo"] = has_trip & (p1_dist >= 200) & (p3_dist <= 75)

# WRAP: crosses 1000/1 boundary (circular path != linear path)
df["_wrap"] = has_pair & df.apply(
    lambda r: is_wrap(int(r["_p1"]), int(r["Pitch"])) if pd.notna(r["_p1"]) else False, axis=1
)

# CIRCULAR DELTA (absolute)
df["_circ_delta"] = df.apply(
    lambda r: circ_dist(int(r["_p1"]), int(r["Pitch"])) if pd.notna(r["_p1"]) else np.nan, axis=1
)

# BATTER FOLLOWING: after batter swings at _sw1, does pitcher move toward or away?
# follow_sign = +1 means pitcher moved toward last swing, -1 means moved away
df["_batter_follow"] = df.apply(
    lambda r: (
        1 if circ_dist(int(r["Pitch"]), int(r["_sw1"])) < circ_dist(int(r["_p1"]), int(r["_sw1"]))
        else -1
    ) if (has_swing[r.name] and pd.notna(r["_p1"])) else np.nan,
    axis=1,
)

# GAME RANGE: recorded at the play level for later aggregation
df["_pitch_range"] = df.groupby(["Game", "Pitcher"])["Pitch"].transform(lambda s: s.max() - s.min())

# POST-RESULT DELTA: circular delta following K vs hit vs other
result_col = "Result" if "Result" in df.columns else None
if result_col:
    df["_prev_result"] = df.groupby(["Game", "Pitcher"])[result_col].shift(1)

# ── Aggregate per (Season, Pitcher) ──────────────────────────────────────────

print("\nAggregating per pitcher...")

def pct(num, denom):
    return round(num / denom * 100, 1) if denom > 0 else np.nan

rows = []
for (season, pitcher_id), grp in df.groupby(["Season", "Pitcher"]):
    n_pitches = len(grp)
    n_games   = grp["Game"].nunique()
    if n_pitches < 10:
        continue

    trip = grp[grp["_p2"].notna()]
    pair = grp[grp["_p1"].notna()]

    n_trip = len(trip)
    n_pair = len(pair)

    span_n     = trip["_span"].sum()
    span_inc_n = trip["_span_inc"].sum()
    span_dec_n = trip["_span_dec"].sum()
    rev_n      = trip["_rev"].sum()
    rev_ud_n   = trip["_rev_ud"].sum()
    rev_du_n   = trip["_rev_du"].sum()
    persist_n  = trip["_persist"].sum()
    echo_n     = trip["_echo"].sum()
    wrap_n     = pair["_wrap"].sum()
    mean_circ  = pair["_circ_delta"].mean()
    mean_range = grp.groupby("Game")["_pitch_range"].first().mean()
    follow_pct = (pair["_batter_follow"] == 1).sum() / pair["_batter_follow"].notna().sum() * 100 \
                 if pair["_batter_follow"].notna().sum() > 0 else np.nan

    # Post-result delta
    if result_col and "_prev_result" in grp.columns:
        after_k   = pair[pair["_prev_result"].astype(str).str.upper() == "K"]["_circ_delta"].mean()
        after_hit = pair[pair["_prev_result"].astype(str).str.upper().isin(["1B","2B","3B","HR"])]["_circ_delta"].mean()
    else:
        after_k = after_hit = np.nan

    rows.append({
        "season":       season,
        "pitcher_id":   pitcher_id,
        "name":         name_lookup.get((season, pitcher_id), f"ID{pitcher_id}"),
        "n_pitches":    n_pitches,
        "n_games":      n_games,
        "mean_pitch":   round(grp["Pitch"].mean(), 1),
        "mean_circ_delta": round(mean_circ, 1),
        "mean_game_range": round(mean_range, 1),

        "span_n":       int(span_n),
        "span_pct":     pct(span_n, n_trip),
        "span_inc_pct": pct(span_inc_n, n_trip),
        "span_dec_pct": pct(span_dec_n, n_trip),

        "reversal_n":   int(rev_n),
        "reversal_pct": pct(rev_n, n_trip),
        "rev_ud_pct":   pct(rev_ud_n, n_trip),
        "rev_du_pct":   pct(rev_du_n, n_trip),

        "persist_pct":  pct(persist_n, n_trip),
        "echo_pct":     pct(echo_n, n_trip),
        "wrap_pct":     pct(wrap_n, n_pair),
        "follow_batter_pct": round(follow_pct, 1) if pd.notna(follow_pct) else np.nan,

        "after_k_delta":   round(after_k,   1) if pd.notna(after_k)   else np.nan,
        "after_hit_delta": round(after_hit, 1) if pd.notna(after_hit) else np.nan,
    })

out = pd.DataFrame(rows).sort_values(["season", "name"])
out.to_csv("pitcher_patterns.csv", index=False)
print(f"Saved: pitcher_patterns.csv  ({len(out)} pitcher-seasons)")

# ── Post-result breakdown (league-wide) ───────────────────────────────────────

if result_col:
    post = (
        df[df["_p1"].notna() & df["_prev_result"].notna()]
        .groupby(df["_prev_result"].astype(str).str.upper())["_circ_delta"]
        .agg(mean_delta="mean", n="count")
        .round(1)
        .sort_values("mean_delta", ascending=False)
    )
    post.to_csv("pitcher_patterns_by_result.csv")
    print(f"Saved: pitcher_patterns_by_result.csv")
    print("\nMean |circular delta| after each result (league-wide):")
    print(post[post["n"] >= 20].to_string())

# ── Summary highlights ────────────────────────────────────────────────────────

print("\n" + "="*65)
print("LEAGUE AVERAGES (pitchers with >= 30 pitches)")
enough = out[out["n_pitches"] >= 30]
for col, label in [
    ("span_pct",          "Span rate (%)"),
    ("reversal_pct",      "Reversal rate (%)"),
    ("persist_pct",       "Persistence rate (%)"),
    ("echo_pct",          "Echo rate (%)"),
    ("wrap_pct",          "Wrap rate (%)"),
    ("follow_batter_pct", "Follow-batter rate (%)"),
    ("mean_circ_delta",   "Mean |circ delta|"),
    ("mean_game_range",   "Mean game range"),
]:
    if col in enough.columns:
        print(f"  {label:<30} {enough[col].mean():>6.1f}")

print("\nTOP 10 BY SPAN RATE (>= 30 pitches):")
print(enough.nlargest(10, "span_pct")[
    ["season","name","n_pitches","span_pct","span_inc_pct","span_dec_pct"]].to_string(index=False))

print("\nTOP 10 BY REVERSAL RATE (>= 30 pitches):")
print(enough.nlargest(10, "reversal_pct")[
    ["season","name","n_pitches","reversal_pct","rev_ud_pct","rev_du_pct"]].to_string(index=False))

print("\nTOP 10 BY PERSISTENCE RATE (>= 30 pitches):")
print(enough.nlargest(10, "persist_pct")[
    ["season","name","n_pitches","persist_pct","mean_circ_delta"]].to_string(index=False))

print("\nTOP 10 BY ECHO RATE (>= 30 pitches):")
print(enough.nlargest(10, "echo_pct")[
    ["season","name","n_pitches","echo_pct","mean_game_range"]].to_string(index=False))

print("\nTOP 10 NARROWEST RANGE (>= 30 pitches, lowest mean_game_range):")
print(enough.nsmallest(10, "mean_game_range")[
    ["season","name","n_pitches","mean_game_range","mean_circ_delta"]].to_string(index=False))

print("\nTOP 10 BATTER FOLLOWERS (>= 30 pitches, highest follow_batter_pct):")
if "follow_batter_pct" in enough.columns:
    print(enough.dropna(subset=["follow_batter_pct"]).nlargest(10, "follow_batter_pct")[
        ["season","name","n_pitches","follow_batter_pct","mean_circ_delta"]].to_string(index=False))

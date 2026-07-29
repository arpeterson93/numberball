# Δ² redefinition: signed circular second difference

## Why

Δ² (the "second derivative" of pitch sequencing) is currently computed as
`| |Δnew| - |Δold| |` - the difference between two *magnitudes*. Because each
magnitude is itself bounded to [0, 500], this quantity's reachable range depends
on Δold: if Δold = 250, Δ² can only ever land in [0, 250]; if Δold = 300, Δ² can
land in [0, 300] but 0-200 is reachable two ways (Δnew = 300±x) and 201-300 only
one way. A pitcher with zero real tendency will still show a skewed Δ²
distribution - concentrated low, sometimes literally unable to reach the top of
whatever bucket grid is drawn - purely from this geometry, not from behavior.
Every surface built on top of it (the heatmap, the sequence hints, the Stoplight
surprisal scoring, the OBP recent Δ² backtest, and the career/tendencies stats)
inherits that bias.

The fix: stop stripping the sign off each delta before differencing. Compute Δ²
as the difference between the two *signed* deltas, then wrap that difference
circularly (same trick already used to compute a signed delta from two pitch
positions) instead of leaving it on an unbounded line. Concretely:

```
Δ² (signed) = circular_signed_delta(Δold, Δold_to_Δnew...)  i.e. wrap(Δnew - Δold) into (-500, 500]
|Δ²|        = abs(that)
```

This is provably unbiased: if Δnew is drawn uniformly at random (the "no
tendency" null), `wrap(Δnew - Δold)` is *exactly* uniform over the full
(-500, 500] range for **any** Δold - verified by hand at the extreme boundary
(Δold = 500) during design. Its absolute value inherits that same
context-independence (up to the same negligible endpoint-rounding effect
first-order |Δ| already has, which is not a problem worth solving). The bias is
gone at the source; nothing downstream needs to know it ever existed.

## Scope decisions (already made - do not re-litigate these)

1. **Display form: unsigned on-screen everywhere ("Option A").** Every surface
   (heatmap, sequence hints, Stoplight signal, career stat, tendencies-over-time
   chart) keeps showing |Δ²| on a 0-500 scale with the exact bucket-size choices
   it has today. This mirrors how first-order Δ already works site-wide: computed
   signed, displayed unsigned, sign reconstructed only at the final
   "which direction do I project" step. **Nothing about bucket ranges, axis
   labels, slider options, or chart layout changes.** Only the formula feeding
   those numbers changes.
2. **OBP recent Δ² is in scope.** It's a live signal on both the pitcher and
   batter Suggestions tabs (`_OBP_STOPLIGHT_SIGNALS`, `pages/2_Scouting.py:271`),
   built on the same broken magnitude-difference math via its own inline
   recompute in `obp_recency_walk`. It gets fixed alongside everything else so
   the site is consistent, not left with one stale signal.
3. **Pure replacement, no new stat.** No "net directional drift" or other new
   signed-Δ² metric gets added anywhere. The signed value is used internally
   (it always was, via `pitch_circ_delta2_signed`) but never becomes its own
   displayed stat.
4. **Batter side is in scope by necessity, not by choice.** There is no
   batter-side Δ² heatmap/hint/Stoplight UI (that machinery is pitcher-only),
   but `project_from_delta2s` is a shared primitive also called from the batter
   "Based on Recent Swing Δ²" Suggestion weighting (`pages/2_Scouting.py:2493,
   2767`). Fixing the shared function fixes both call sites automatically - do
   not attempt to special-case pitcher vs batter here.

## The one new primitive

Add a small vectorized helper right after `_circ_delta_group`
(`utils.py:730-733`):

```python
def _wrap_delta2(diff: pd.Series) -> pd.Series:
    """Wrap a raw difference between two signed deltas into (-500, 500],
    mirroring circular_signed_delta but vectorized for a pandas Series. A
    signed delta already lives on a width-1000 range, so the difference of two
    of them wraps the same way a raw position difference does - without the
    wrap, two deltas that are nearly identical in real terms but happen to sit
    on opposite sides of the +/-500 seam (e.g. +498 and -497) would look like a
    ~1000-point swing instead of the ~5-point swing they actually are."""
    d = diff.where(diff <= 500, diff - 1000)
    d = d.where(d >= -500, d + 1000)
    return d
```

For the two **numpy-array** call sites (`_recency_indications`,
`obp_recency_walk`), do NOT call this pandas-Series helper - those functions
already wrap raw position-deltas inline via
`np.where(x > 500, x - 1000, np.where(x < -500, x + 1000, x))`; apply that same
inline idiom to the delta-of-delta value, matching local style.

For the one **scalar/list** call site (`project_from_delta2s`), do NOT write new
wrap logic at all - `circular_signed_delta(a, b)` (`utils.py:670-677`) already
computes exactly `wrap(b - a)` for two arbitrary numbers, so call it directly on
the two signed deltas.

Three different idioms, one already-correct piece of math, reused three ways -
this is intentional, not an inconsistency to "clean up."

## Required code edits (this is the entire diff)

### 1. `enrich_df` - `utils.py:1031-1039`

This writes the two persisted columns. Today:

```python
        df.loc[sw, "pitch_circ_delta2_signed"] = sw_df2.groupby(
            gk_pit2, group_keys=False
        )["pitch_circ_delta"].apply(lambda g: g.abs().diff())
        # Unsigned is just the magnitude of the signed version - positive means
        # the movement grew (accelerated), negative means it shrank (decelerated).
        df.loc[sw, "pitch_circ_delta2"] = df.loc[sw, "pitch_circ_delta2_signed"].abs()
```

Change the `.apply(...)` body from `lambda g: g.abs().diff()` to
`lambda g: _wrap_delta2(g.diff())`. `g` here is the `pitch_circ_delta` column
(already signed) grouped per game+pitcher, so `g.diff()` is the raw
(unwrapped) difference between consecutive signed deltas - wrap it, don't abs
it first. The following `pitch_circ_delta2 = ...abs()` line is unchanged (it
already does the right thing once its input is correct). Update the comment
above it - it currently describes "grew/shrank" semantics that no longer apply;
replace with something like "|Δ²| is the magnitude of the wrapped signed second
difference - see `_wrap_delta2`."

This is the column already shown, mislabeled, in the Raw Plate Appearance Data
debug table (`pages/2_Scouting.py:2354-2367`, columns "Δ²"/"|Δ²|"). No change
needed in that table's code - it will show correct values automatically once
this column is fixed. Worth a quick visual check during validation.

### 2. `_fresh_delta2_frame` - `utils.py:6271-6297`

Feeds the heatmap, third-dist, and both `seq2_delta2_hint`/`seq3_delta2_hint`.
Today:

```python
    df_sw["_d2"] = df_sw.groupby(
        ["game_id", group_col], group_keys=False
    )[delta_col].apply(lambda g: g.abs().diff().abs())
```

Change to `lambda g: _wrap_delta2(g.diff()).abs()`. Same reasoning as #1 -
`g` is the signed delta column, `g.diff()` is the raw signed second difference,
wrap it, then abs for the unsigned display value.

Update the function's docstring (currently says "stage 2 takes the abs-diff-of-
abs of that") to describe the wrap instead.

### 3. `project_from_delta2s` - `utils.py:2051-2067`

Feeds the pitcher "Optimal Pitch" projection pool AND the batter "Based on
Recent Swing Δ²" projection pool (shared function, see scope decision #4), plus
the OBP backtest's delta2 population (`obp_recency_walk` line ~4464). Today:

```python
    deltas  = [circular_signed_delta(recent_vals[i - 1], recent_vals[i]) for i in range(1, len(recent_vals))]
    delta2s = [abs(abs(deltas[i]) - abs(deltas[i - 1])) for i in range(1, len(deltas))]
```

Change the `delta2s` line to:

```python
    delta2s = [abs(circular_signed_delta(deltas[i - 1], deltas[i])) for i in range(1, len(deltas))]
```

**Do not touch the `for sign in (+1, -1)` branching below it or the function's
"produces two projections" docstring claim - both stay exactly as they are.**
Under the unsigned-display decision, a predicted |Δ²| magnitude still doesn't
say whether the underlying delta grew or shrank, so both branches are still
needed and both are still produced. This is the one place it would be easy to
over-simplify (collapsing to one projection) - don't; that's the signed-display
("Option B") design we explicitly did not choose. Update the docstring's
"unsigned change" language to "wrapped signed difference" for accuracy, keep
everything else.

### 4. `_recency_indications` - `utils.py:3947-3968`

Feeds the "2-Δ² seq" / "3-Δ² seq" Stoplight signals (both the aggregate light
and the per-pitch Inspector). Today it only ever materializes the *unsigned*
`delta_abs` array, then differences that:

```python
    delta_abs = np.full(n, np.nan)
    if n > 1:
        raw = vals[1:].astype(float) - vals[:-1].astype(float)
        raw = np.where(raw > 500, raw - 1000, raw)
        raw = np.where(raw < -500, raw + 1000, raw)
        delta_abs[1:] = np.where(same1[1:], np.abs(raw), np.nan)
    delta_bkt = pd.cut(pd.Series(delta_abs), bins=list(range(0, 501, dd_bkt)),
                       labels=False, right=True, include_lowest=True).to_numpy()
    delta100 = pd.cut(pd.Series(delta_abs), bins=_DELTA_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    # |Delta^2| into each pitch: the unsigned change in |Delta| from one adjustment to
    # the next. NaN at each game's first two pitches. The same2 guard is required (not
    # optional): at a game's 2nd pitch delta_abs[i] and delta_abs[i-1] are both non-NaN
    # but belong to different games, so a bare diff would cross the game boundary.
    d2sq_abs = np.full(n, np.nan)
    if n > 2:
        d2sq_abs[2:] = np.where(same2[2:], np.abs(delta_abs[2:] - delta_abs[1:-1]), np.nan)
    d2sq_bkt = pd.cut(pd.Series(d2sq_abs), bins=list(range(0, 501, dd2_bkt)),
                      labels=False, right=True, include_lowest=True).to_numpy()
```

Replace with (keep a signed array around instead of discarding sign
immediately; `raw` already **is** the wrapped signed delta before the old code
threw it away with `np.abs`):

```python
    delta_signed = np.full(n, np.nan)
    if n > 1:
        raw = vals[1:].astype(float) - vals[:-1].astype(float)
        raw = np.where(raw > 500, raw - 1000, raw)
        raw = np.where(raw < -500, raw + 1000, raw)
        delta_signed[1:] = np.where(same1[1:], raw, np.nan)
    delta_abs = np.abs(delta_signed)
    delta_bkt = pd.cut(pd.Series(delta_abs), bins=list(range(0, 501, dd_bkt)),
                       labels=False, right=True, include_lowest=True).to_numpy()
    delta100 = pd.cut(pd.Series(delta_abs), bins=_DELTA_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()

    # |Delta^2| into each pitch: the magnitude of the wrapped signed difference
    # between two consecutive signed deltas (see _wrap_delta2). NaN at each
    # game's first two pitches. The same2 guard is required (not optional): at a
    # game's 2nd pitch delta_signed[i] and delta_signed[i-1] are both non-NaN
    # but belong to different games, so a bare diff would cross the game boundary.
    d2sq_raw = np.full(n, np.nan)
    if n > 2:
        d2sq_raw[2:] = np.where(same2[2:], delta_signed[2:] - delta_signed[1:-1], np.nan)
    d2sq_raw = np.where(d2sq_raw > 500, d2sq_raw - 1000, d2sq_raw)
    d2sq_raw = np.where(d2sq_raw < -500, d2sq_raw + 1000, d2sq_raw)
    d2sq_abs = np.abs(d2sq_raw)
    d2sq_bkt = pd.cut(pd.Series(d2sq_abs), bins=list(range(0, 501, dd2_bkt)),
                      labels=False, right=True, include_lowest=True).to_numpy()
```

(`np.where` comparisons against NaN entries evaluate to `False` and pass the
NaN through unchanged - same as the existing `raw` wrap above it - no special
NaN handling needed.)

Nothing else in this function changes. `ind["2-Δ² seq"]` / `ind["3-Δ² seq"]`
(lines ~4002-4007) read `d2b`, which is unaffected in structure - it just
receives correct values now.

### 5. `obp_recency_walk` - `utils.py:4427-4526` (three spots)

**5a. Ground-truth Δ² array** (~4434-4436). Today:

```python
    d2 = np.full(T, np.nan)  # real |delta2| into each pitch
    if T > 2:
        d2[2:] = np.where(same2[2:], np.abs(abs_d[2:] - abs_d[1:-1]), np.nan)
```

Replace with (reuse the already-computed signed array `sd`, don't rebuild it
from `abs_d`):

```python
    d2 = np.full(T, np.nan)  # real |delta2| into each pitch: magnitude of the
    # wrapped signed difference between two consecutive signed deltas.
    if T > 2:
        d2raw = np.where(same2[2:], sd[2:] - sd[1:-1], np.nan)
        d2raw = np.where(d2raw > 500, d2raw - 1000, d2raw)
        d2raw = np.where(d2raw < -500, d2raw + 1000, d2raw)
        d2[2:] = np.abs(d2raw)
```

**5b. Candidate tie-break** (~4499-4507). Today:

```python
        else:  # delta2
            if same2[t - 1] and not np.isnan(d2[t - 1]):
                cd = cand.astype(float) - prev
                cd = np.where(cd > 500, cd - 1000, np.where(cd < -500, cd + 1000, cd))
                implied_d2c = np.abs(np.abs(cd) - abs(sd[t - 1]))
                metric = np.abs(implied_d2c - d2[t - 1])
                best_val = int(cand[int(np.argmin(metric))])
            else:
                best_val = int(cand[0])
```

`cd` (the candidate's own wrapped first-order delta from `prev`) is already
correct and unrelated to this bug - leave it. Replace only the `implied_d2c`
line:

```python
                cd2 = cd - sd[t - 1]
                cd2 = np.where(cd2 > 500, cd2 - 1000, np.where(cd2 < -500, cd2 + 1000, cd2))
                implied_d2c = np.abs(cd2)
```

**5c. Recommended-value display/scoring** (~4520-4526). Today:

```python
        else:  # delta2
            if not same2[t]:
                continue
            implied_delta = circular_signed_delta(prev, best_val)
            implied = int(abs(abs(implied_delta) - abs(sd[t - 1])))
            part = obp_bounded_partition(implied, bucket_width, 500)
            outcome = int(d2[t])
```

This is scalar, so reuse `circular_signed_delta` directly instead of hand-
rolling a wrap:

```python
            implied_delta = circular_signed_delta(prev, best_val)
            implied = int(abs(circular_signed_delta(int(sd[t - 1]), int(implied_delta))))
            part = obp_bounded_partition(implied, bucket_width, 500)
            outcome = int(d2[t])
```

`outcome = int(d2[t])` needs no change beyond what 5a already fixed.

## Explicitly NOT changing (verify these stay untouched - do not "clean up" them)

Everything below reads a value that one of the five edits above now computes
correctly, and needs no code change of its own:

- `next_delta2_vs_prior_delta2_heatmap` (`utils.py:6300-6396`) - consumes
  `_fresh_delta2_frame`'s `_d2`.
- `delta2_third_dist` (`utils.py:6399-6478`) - same.
- `seq2_delta2_hint` / `seq3_delta2_hint` (`utils.py:3417-3529`) - same.
- `delta2_to_pitch_ranges` (`utils.py:3202-3229`) - already takes an unsigned
  |Δ²| bucket and the *current* signed last-delta `L` (a first-order delta,
  never a Δ² value) and branches ±. This is unrelated to how Δ² itself gets
  computed and is correct as-is.
- `hint_bars_figure`'s Δ² painter branch (`utils.py:2521-2539`) - compounds
  through `delta2_to_pitch_ranges`, untouched.
- `_delta2_row_p` (`pages/2_Scouting.py:1601-1629`) - consumes
  `seq2_delta2_hint`/`seq3_delta2_hint` output dicts, untouched.
- `_recency_labelers`'s `delta2sq(b)` formatter (`utils.py:~4098-4100`) - still
  `f"{b*dd2_bkt}-{(b+1)*dd2_bkt}"`, buckets stay 0-500, untouched.
- `sequence_matches(..., domain="delta2")` (`utils.py:2732+`) - reads the
  persisted `pitch_circ_delta2` column directly, fixed automatically by edit
  #1.
- The Sequence Viewer Δ² tab, the Pitch Analysis Δ² block, and the
  `dd2_bucket_p` slider (`pages/2_Scouting.py:~1910-1920, ~2145-2180`) - pure
  UI over the above functions.
- `compute_pitcher_stats` / `compute_recent_pitcher_stats` / `_PERCENTILE_STATS`
  / `pitcher_percentile_card` (`utils.py:7403-7520+`) and `_MA_METRICS["avg_delta2"]`
  / `pitcher_ma_figure` (`utils.py:7718-7736+`) - all read the persisted
  `pitch_circ_delta2` column, fixed automatically by edit #1. The Supabase
  `pitcher_stats.avg_delta2` column needs no schema change (still a FLOAT) -
  see rollout step below for why its *values* still need a manual refresh.
- `obp_zone_signal(..., paired=True)` and its callers (pitcher
  `pages/2_Scouting.py:1550`, batter `~2497-2513`, `~2760-2780`) - the
  `paired=True` contract ("two consecutive points from `project_from_delta2s`
  are a deterministic mirror of one real observation, don't double-count them")
  is unchanged, because `project_from_delta2s` still emits exactly two outputs
  per real Δ² observation (scope decision #1 / edit #3's explicit non-change).
  Do not remove or alter `paired=True` anywhere.
- `merge_delta_ranges` as called from `_delta2_row_p` - operates on unsigned
  magnitude bucket tuples on [0,500], still correct.
- `_STOPLIGHT_ORDER`, `_load_pitcher_stoplights`, `_load_pitcher_stoplight_detail`,
  `_stoplight_inspector` (`pages/2_Scouting.py:259-303`) - plumbing only, no
  formula.
- `scripts/calibrate_stoplight_thresholds.py` - no code change; it calls
  `_recency_indications` / `obp_recency_walk` directly, so it inherits the fix
  automatically. It needs to be *re-run* (see rollout below), not edited.

## Rollout / operational steps (do these after the code changes, not instead of them)

1. `python -m py_compile utils.py pages/2_Scouting.py scripts/calibrate_stoplight_thresholds.py`.
2. Click "Refresh Pitcher Stats" on the Games page
   (`pages/1_Games.py:706-725`). This is a manual batch job that upserts the
   `pitcher_stats` table (`avg_delta2` etc.) via `compute_pitcher_stats`; the
   persisted rows reflect whatever formula was live when it was last clicked,
   so every existing row is stale under the old (biased) definition until this
   runs again. No SQL migration needed - same schema, corrected values.
3. Re-run `python scripts/calibrate_stoplight_thresholds.py` (both the default
   sequence mode and `--obp` mode) and read the per-indication p33/p67 report
   for "2-Δ² seq", "3-Δ² seq", and "OBP recent Δ²". `SCOUT_PP_THRESHOLDS`
   (`utils.py:~3777`) is a single shared `{scouting_min, anti_max}` pair across
   all signals, calibrated so each signal lands close to 1/3 in each zone
   (green/yellow/red). Confirm the Δ²-family rows now land reasonably close to
   the other signals' p33/p67 under the *current* shared thresholds (they were
   presumably skewed before, given the bias). Only touch
   `SCOUT_PP_THRESHOLDS` itself if the script's report shows the pooled
   ("ALL") distribution has shifted enough to warrant it - that's a judgment
   call to make from the actual report output, not something to preset here.
4. `clifton_mccullough_pitches.csv` (repo root) has a static "Delta^2" column
   from a prior export. Nothing in the app reads this file's Delta^2 column
   back into live logic (the scoring functions all recompute from raw pitch
   values), so no code depends on it - but if anyone consults this CSV by eye,
   know that its Delta^2 column reflects the old definition and is now stale.
   Regenerate it if it's still actively used for manual review; otherwise leave
   it, low priority.

## Validation

1. **Hand-check the core primitive.** In a scratch REPL:
   `_wrap_delta2` (or `circular_signed_delta` on two deltas) should give:
   - Δold=+150, Δnew=-50 → wrap(-50-150) = wrap(-200) = -200 → |Δ²| = 200
     (matches the worked example from the design conversation).
   - Δold=+490, Δnew=-495 → wrap(-495-490) = wrap(-985) = -985+1000 = 15 →
     |Δ²| = 15 (small - correctly recognizes these as two similarly-huge
     swings on opposite sides of the wrap seam, not a ~1000-point change).
   - Δold=+500 (extreme boundary): confirm `project_from_delta2s`/`_wrap_delta2`
     with a full sweep of Δnew from -499 to +500 produces every |Δ²| value 0-500
     with the expected uniform-ish histogram (no gap, no double-density lump) -
     this is the case explicitly checked during design.
2. **Regression: heatmap sanity.** Pick a heavily-scouted pitcher, load Pitch
   Analysis, confirm `next_delta2_vs_prior_delta2_heatmap` renders (not a blank
   `go.Figure()`), and spot-check a couple of hover cells against a manual
   pandas crosstab computed the same way `_fresh_delta2_frame` now does it.
   Confirm the distribution is no longer suspiciously concentrated only in the
   lowest column regardless of which column you're looking at.
3. **Suggestions worked example.** In the pitcher Swing Suggestions tab, find a
   case with a known recent history, and hand-verify the "2-Δ² seq"/"3-Δ² seq"
   rows' green zones match what `delta2_to_pitch_ranges` should produce given
   the *new* |Δ²| bucket the hint returns (bucket boundaries stay 0-500 as
   before - only which bucket wins should differ from pre-change behavior).
4. **Stoplight Inspector.** Select "2-Δ² seq" in the Inspector, confirm
   Context/Observed labels still render as `X-Y` ranges (unchanged formatter),
   confirm `n_scored` is unaffected (same number of eligible events - this
   change alters *values*, not which pitches are eligible), and confirm the
   green/yellow/red state can differ from before the change (it should, if the
   old bias was real - that's the point).
5. **OBP recent Δ².** Confirm the signal still renders on both pitcher and
   batter Suggestions tabs, still has a Strength and a colored zone in "All
   zones" mode, and re-run the `--obp` calibration report to sanity-check its
   score distribution isn't degenerate (e.g. not all-zero, not saturated).
6. **Regression: everything else untouched.** Confirm "2-pitch seq", "3-pitch
   seq", "2-Δ seq", "3-Δ seq", "Prior diff → Δ", "OBP recent pitch", "OBP
   recent Δ", Outs, Base state, and the first-pitch indications are bit-for-bit
   unaffected (none of their source arrays were touched) - diff the Inspector's
   drill-down table for one of these signals before/after if in doubt.
7. **Raw Plate Appearance Data table.** Open the expander
   (`pages/2_Scouting.py:2354-2367`), confirm the "Δ²" column (signed) and
   "|Δ²|" column now show consistent, correctly-signed values (e.g. "Δ²" should
   equal ±"|Δ²|" and the sign should make sense against the "Δ" column two rows
   apart).

# Monte Carlo win probability / leverage engine - implementation plan

## Why

`win_probability_table.csv` (the table `utils.get_win_probability`/
`get_win_probability_interpolated` read, and therefore everything downstream -
`compute_leverage`, the Key Moments feed, the scoreboard, the batter
optimizer) was built by `compute_win_probability.py` via empirical-Bayesian
blending of real Numberball outcomes with an MLB-relativity-adjusted logistic
prior (see that file's docstring). Alex wants a second, independently-derived
table built by forward Monte Carlo simulation - many simulated games, each
walked play-by-play from per-situation result probabilities, with win/loss
outcome scraped back onto every state the simulation passed through - to
compare against the current table before deciding whether to adopt it.

**This is not a new leverage engine.** `utils.compute_leverage()` and
`utils._compute_avg_wp_swing()` (`utils.py:330-363`, `305-327`) already don't
care how `win_probability_table.csv` was produced - they just read whatever
`_WP_LOOKUP`/`_WP_BY_STATE` holds. Nothing in this plan touches that code.
The actual deliverable is a **new CSV, built a different way, plus a
comparison report** - Opus should resist the temptation to "improve" the
leverage formula itself as part of this work; that is explicitly out of
scope and was not asked for.

Project constraints that apply throughout (existing repo rules):
- No em dashes in any `.py` file - hyphens only.
- No Co-Authored-By trailer if committing.

---

## Resolved decisions (do not re-litigate)

Asked and answered 2026-07-31/08-01:

1. **Result sampling is conditioned on `(outs, obc)`.**
2. **The primary source for that conditioning is `result_ranges_re24.csv`
   (repo root), not historical Supabase counting** - see the next section.
   This was a mid-plan addition once Alex pointed out the file exists;
   it supersedes the original design's plan to drive the simulator directly
   from empirical situational frequencies. Historical frequencies are still
   built, but as a **validation comparison against the theoretical table**,
   not as the simulator's input - "we might want to double check how these
   have compared to actual historical results, but we should converge to
   these if played long enough" (Alex).
3. **Rollout: build and compare, do not replace outright.** The simulated
   table is written to its own filename, never overwrites
   `win_probability_table.csv`. A comparison report is the deliverable that
   lets Alex decide whether/how to adopt it. `compute_win_probability.py`
   is not touched.
4. **Refresh cadence: manual script, same as today.** No new GitHub Action.
5. **Data scope for the historical comparison: MLN only, season 6 onward,
   with season as a CLI input.** `--season-start` defaults to `6`. (This
   scope question only applies to the historical-frequency comparison
   script now, not the simulator itself - `result_ranges_re24.csv` has no
   season/league dimension to scope, it's a fixed theoretical table.)

## Open questions from the first draft - now resolved

The first draft of this plan ended with five open questions for Opus. Alex
asked me to just answer them rather than leave them open, so here they are,
resolved:

1. **Inning-stage (`remaining`) excluded from conditioning - RESOLVED, keep
   it excluded, with more confidence than the first draft had.** The
   original worry was that "no inning-dependence" was an unverified
   assumption. `result_ranges_re24.csv` is named for, and structurally
   matches, the classic sabermetric RE24 framework (run expectancy over the
   24 base/out states) - a methodology that is *inning-agnostic by
   construction*. Its `Rng` distribution demonstrably already varies by
   `outs` (confirmed: DP/TP-eligible results disappear entirely at
   `outs=2`, e.g. no `DP21`/`DP31`/`DPH1`/`DPRun`/`TP`/`LODP` rows for any
   `obc` at `outs=2`, and the freed-up probability mass shifts into
   `GO`/`FO`/other results) and by `obc` (e.g. `GORA` only appears for
   `obc` values with a runner on base; `SacF` only for `obc` with a runner
   on 3rd; `DSacF` only where 2nd and 3rd are both occupied) - so the table
   is already properly situational on the two dimensions that matter
   mechanically. Extending it to also vary by inning would require
   discarding this theoretical table and going back to empirical counting
   (thin cells, noise) for no clearly articulated benefit - not worth it.
2. **Thin-cell smoothing - RESOLVED, now moot for the simulator itself.**
   `result_ranges_re24.csv` has zero sampling noise (it's a closed-form
   probability partition, not observed counts), so there is no thin-cell
   problem for Stage 2 to solve. The historical-frequency comparison script
   (now purely a validation input, not a simulation input) can still hit
   thin cells, but since its only job is "does real play broadly track the
   theoretical table," a noisy cell there just means "not enough games yet
   to judge that cell" - report it as such, no smoothing logic needed.
3. **`--games` sizing - RESOLVED as: pick a starting default, validate it
   with a convergence check rather than a precomputed number.** Default
   `--games=200000`. The two-seed convergence check described in Stage 3
   below is the actual answer to "is this enough," not a fixed number
   decided in advance.
4. **No `utils.py` changes for table-swapping - RESOLVED, confirmed as
   designed.** Manual file rename for the one-off leverage comparison,
   no new configurability added to `utils.py`. Still true, unaffected by
   the re24 addition.
5. **`result_frequencies.csv`/`state_frequencies.csv` (leverage's own
   denominator inputs) stay untouched by this plan - RESOLVED, confirmed
   out of scope.** Still a separate future decision, not this one.

---

## The star of the show: `result_ranges_re24.csv`

Verified this session (see the mechanics review two conversations back for
how `RESULT_RANGES`/`compute_leverage` work today - this file is a different,
better-suited input for the *simulator* specifically):

- **317 rows**, columns `obc,outs,Result,Rng,Low,High`. `obc` is written as
  a bare int with leading zeros stripped by the CSV (`"001"` reads as `1`,
  `"010"` as `10`, `"100"` as `100`, etc.) - **must be loaded with
  `dtype={"obc": str}` then `.str.zfill(3)`**, the exact same gotcha
  `win_probability_table.csv` and `state_frequencies.csv` already have and
  are already handled the same way (`utils.py:108-109`,
  `compute_state_frequencies.py`).
- **24 groups** = every `(obc, outs)` combination (8 obc values x 3 out
  counts), each group's `Rng` column summing to exactly `501` - the full
  circular-diff domain (`0..500` inclusive), matching how `RESULT_RANGES`
  and every other diff-band table in this codebase already partitions that
  domain.
- **Every single row's `(Result, obc, outs)` combination is an exact key in
  `utils._BRC_RUN_LOOKUP`** (verified: 0 of 317 rows fall outside it,
  cross-checked directly against `import_BRC.csv`). This means the
  simulator can sample a result from this table and hand it straight to
  `utils.advance_runners()` with **zero chance** of hitting that function's
  hand-coded fallback path (`utils.py:949-998`) - a stronger guarantee than
  the original plan's Stage 1 could offer without an extra exactness filter,
  and this table needs no such filter at all.
- **Coverage gap, by design, not a bug**: 24 result codes are BRC-known but
  absent from every row of this table -
  `SB, SB2, SB3, SB4, SB32, SB42, SB43, SB432` (steal variants), `CS, CS2,
  CS3, CS4, KCS` (caught stealing variants), `B1B, B1BWH, BGO, BFC, SacB,
  BDP` (bunt variants), `IBB` (intentional walk), `Balk`, `bAuto, pAuto`,
  and `1BWH2`. All of these are events that happen *outside* the normal
  pitch-diff-band mechanic in live play - steals are a separate manager
  decision, bunts use their own range table (`_BUNT_TO_SWING` and the
  "Bunt"/"Hit and Run"/"Infield In" swing-type variants already selectable
  on the Scouting page), IBB/Balk are rule-driven rather than diff-driven.
  **This table models normal-swing at-bats only** - which matches
  `RESULT_RANGES`'s own role as the default "Normal Swing" table today.
  The simulator inherits this same scope; it does not attempt to model
  steal attempts, bunts, or IBB as separate stochastic events layered onto
  an at-bat. Flagging this explicitly so it's a documented modeling
  boundary, not a silently-dropped feature.

---

## Naming / new files

| Thing | Name |
|---|---|
| Simulator's probability source | `result_ranges_re24.csv` (already exists, read directly - no derived intermediate file needed) |
| Historical comparison script | `compute_situational_result_frequencies.py` (repo root, sibling of `compute_result_frequencies.py`/`compute_state_frequencies.py`) |
| Historical comparison output | `situational_result_frequencies.csv` |
| Simulator script | `simulate_win_probability.py` |
| Simulator output | `simulated_win_probability_table.csv` (never `win_probability_table.csv`) |
| Comparison/report script | `compare_win_probability_tables.py` |
| CLI flag (historical comparison script only) | `--season-start` (default `6`) |
| Reused, untouched | `utils.advance_runners`, `utils.outs_added`, `utils.remaining_half_innings`, `utils.game_innings`, `key_moments_build._is_walkoff_final`, `utils.compute_leverage`, `utils._compute_avg_wp_swing` |

---

## Stage 1 - `simulate_win_probability.py` (the simulator itself)

### 1a. Setup

- Load `result_ranges_re24.csv` with `dtype={"obc": str}`, `.str.zfill(3)`
  on the `obc` column (see the gotcha above).
- Build `dist: dict[tuple[int, str], tuple[list[str], list[int]]]` keyed by
  `(outs, obc)` -> `(result_list, rng_weight_list)` - **use the raw `Rng`
  column as the sampling weight directly**, do not convert to a
  probability first. `random.choices(population=result_list,
  weights=rng_weight_list)` (or `numpy` equivalent) samples correctly from
  relative weights without needing them normalized to sum to 1 - the
  `Rng * 2 / 1000` conversion `compute_leverage`'s numerator formula uses
  (`utils.py:360`) matters when computing an absolute probability number,
  not when weighting a random draw, so skip it here. One less thing to get
  slightly wrong (the per-group `Rng` sum of `501` rather than `500` would
  otherwise raise a "do I clip at 1.0 or not" question that simply doesn't
  arise if weights are never normalized in the first place).
- `--games` CLI flag, default `200000` (see the resolved discussion above -
  validated via the convergence check in Stage 3, not a precomputed number).
- `--seed` CLI flag for reproducibility.

### 1b. One simulated game

Initialize `inning=1, half="top", outs=0, obc="000", away_score=0, home_score=0`.
Loop:

1. `remaining = utils.remaining_half_innings(inning, half, key_moments_build.MLN_INNINGS)`
   (import `MLN_INNINGS` from `key_moments_build.py:38` rather than
   re-deriving it - it's already `utils.LEAGUE_INNINGS.get("MLN", 6)`, no
   need for a second source of truth).
2. `batting_is_home = (half == "bottom")`;
   `batting_lead_before = (home_score - away_score) if batting_is_home else (away_score - home_score)`.
3. **Record the pre-play snapshot**: `(remaining, outs, obc, batting_lead_before, batting_is_home)`
   appended to this game's own event list (not the global accumulator yet -
   see 1c on why the win/loss label can't be known until the game ends).
4. Look up `dist.get((outs, obc))`. Given the exactness guarantee above,
   every `(outs, obc)` the simulator can actually reach has a non-empty
   entry - `result_ranges_re24.csv` covers all 24 combinations, and the
   simulator can never produce an `obc`/`outs` pair the BRC-derived
   transitions don't already keep within that same 24-combination space.
   No fallback branch is needed here; if one is ever hit, that is a bug to
   surface loudly (assert, don't silently default), not a runtime case to
   design around.
5. Sample `result` from `dist[(outs, obc)]`.
6. `new_obc, runs = utils.advance_runners(result, obc, outs)`.
7. `new_outs = min(3, outs + utils.outs_added(result))`.
8. Apply `runs` to whichever team is batting (`home_score`/`away_score`).
9. **Game-end check - reuse, do not reimplement.** Import
   `_is_walkoff_final` from `key_moments_build.py` and call it with the
   post-play `(inning, half, new_outs, home_score, away_score)` exactly as
   `key_moments_build._scoreboard()` already does (`key_moments_build.py:725-727`).
   If it returns `True`, the game is over - go to 1c. Otherwise, if
   `new_outs >= 3`, flip half (top->bottom same inning, bottom->top next
   inning), `outs=0, obc="000"`; else `outs=new_outs, obc=new_obc`.
   Continue the loop.

### 1c. Scoring the game's events into the accumulator

Once a game ends, determine the winner (`home_score` vs `away_score` - by
construction of the walk-off/last-inning check, it is never a tie at this
point). For every recorded `(remaining, outs, obc, batting_lead, batting_is_home)`
snapshot from this game:

- `won = 1.0 if (batting_is_home == (home_score > away_score)) else 0.0`
- Clamp `batting_lead` to `[-10, 10]` **only when bucketing into the
  accumulator** (matches the existing table's domain and the clamps already
  present in `get_win_probability`/`get_win_probability_interpolated`,
  `utils.py:181`, `214`) - do not clamp the value used to decide `won`,
  only the key used to file it into the output table.
- Accumulate into a global `dict[(remaining, outs, obc, batting_lead), [win_sum, n]]`.

This two-pass-per-game shape (simulate forward, label backward once the
winner is known) is the only correct way to do this - there is no way to
know a mid-game state's eventual win/loss without finishing the game first,
so do not try to compute WP incrementally during the walk.

### 1d. Output

After all games: for every accumulated key, `win_prob = win_sum / n`. Write
`simulated_win_probability_table.csv` with columns
`remaining,outs,obc,batting_lead,win_prob,n` - the extra `n` column (sample
count backing that cell) is new relative to `win_probability_table.csv`'s
schema, and that's fine: `utils._load_wp_table()` only reads the four named
key columns plus `win_prob` (`utils.py:110-113`), so an extra column is
silently ignored if this file is ever pointed at by that loader. `n` exists
purely so Stage 3's comparison report can weight/flag low-confidence cells -
it is diagnostic, not consumed by any production code path in this plan.

Print a coverage summary: how many `(remaining, outs, obc)` combinations
have data across the full `batting_lead` range `[-10, 10]` vs. only a
partial range, and the count of cells with `n` below some visibility
threshold (e.g. 30).

---

## Stage 2 - `compute_situational_result_frequencies.py` (validation input, not a simulator input)

Mirrors `compute_result_frequencies.py` (BRC-known-result filtering) and
`compute_state_frequencies.py` (Supabase fetch + obc normalization) in
shape; adds `(outs, obc)` grouping in place of the global pool. This exists
purely so Stage 3 can answer "does real play actually track
`result_ranges_re24.csv`'s theoretical distribution" - it does not feed the
simulator.

1. `plays = database.get_all_plays(league="MLN")` (matches the literal
   `"MLN"` string used everywhere else in this codebase -
   `database.py:240,249`, `key_moments_build.py`, `utils.py`'s several
   `"league": "MLN"` writers).
2. Filter to `season >= args.season_start` (default `6`) using the joined
   `games(season, ...)` object already present on each play row
   (`database.py:120-128`). Print a count of rows dropped for
   missing/unparseable season as a sanity check.
3. Derive `outs_before`/`obc_before` the same way
   `compute_state_frequencies.py` already does (obc as either a raw BRC int
   or a zero-padded string, normalize both to `.zfill(3)`).
4. Filter to rows where `(result, obc_before, outs_before)` is a real key in
   `utils._BRC_RUN_LOOKUP` (same discipline `compute_result_frequencies.py`
   already applies at the unconditioned level - here it's applied per
   situation). Print the drop count.
5. Group by `(outs, obc)`, count `result` within each group, normalize to a
   probability within the group.
6. Write `situational_result_frequencies.csv` with columns
   `outs,obc,result,count,probability`, `obc` as the zero-padded 3-char
   string.
7. Print a per-`(outs,obc)` sample-size summary (min/median/max group
   total) so Stage 3 can judge which comparison cells are trustworthy.

---

## Stage 3 - `compare_win_probability_tables.py`

Read-only, produces a report - no file writes beyond an optional
`--out report.csv` dump.

1. **Win-probability table comparison** (the main event): load both
   `win_probability_table.csv` (current) and
   `simulated_win_probability_table.csv` (Stage 1's output), outer-join on
   `(remaining, outs, obc, batting_lead)`, report:
   - Coverage: cells present in one table but not the other.
   - Overall and per-`outs`/per-`obc` mean/median/max absolute `win_prob`
     difference - break out RISP and bases-loaded specifically, since
     those are the states leverage cares most about.
   - Sample-size-weighted difference using `state_frequencies.csv`'s
     `frequency` column, so a discrepancy in a state real games almost
     never reach matters less than one in a common state.
   - Monotonicity spot-check on the simulated table (non-decreasing in
     `batting_lead`; non-increasing in `outs` holding `obc` fixed) - report
     violation count and whether violations cluster in low-`n` cells
     (expected/acceptable) vs. high-`n` cells (would indicate a real bug).
2. **Result-distribution comparison** (the "does history track the
   theoretical table" check Alex asked for): load
   `situational_result_frequencies.csv` (Stage 2) and
   `result_ranges_re24.csv`'s per-`(outs,obc)` weights normalized to
   probabilities, join on `(outs, obc, result)`, report per-cell absolute
   probability difference, flagging any `(outs, obc, result)` combination
   where real play diverges meaningfully from the theoretical rate AND has
   enough historical sample size (per Stage 2's printed group totals) for
   that divergence to be meaningful rather than noise. This is purely
   informational for this plan - a large, well-supported divergence would
   be a reason to revisit whether `result_ranges_re24.csv` still reflects
   current play, but that judgment call belongs to Alex, not to this
   script.
3. **Leverage-specific comparison** (manual, not scripted - see the
   resolved discussion above on why no `utils.py` swapping mechanism gets
   built): temporarily rename files on disk
   (`win_probability_table.csv` -> aside,
   `simulated_win_probability_table.csv` -> `win_probability_table.csv`),
   rerun `python scripts/calibrate_stoplight_thresholds.py` and/or
   `python key_moments_build.py` in a scratch checkout or throwaway branch,
   compare the leverage/`high_leverage` distribution against today's, then
   restore the original file.

---

## Validation

1. **Compile/boot**: `python -m py_compile simulate_win_probability.py compute_situational_result_frequencies.py compare_win_probability_tables.py`.
2. **`result_ranges_re24.csv` sanity** (cheap, do this first): confirm every
   `(obc, outs)` group's `Rng` sums to `501`, confirm zero rows fall outside
   `_BRC_RUN_LOOKUP` (both already verified manually this session, but
   re-verify in-script since this is the simulator's foundation and should
   fail loudly, not silently, if the file ever changes).
3. **Stage 1 hand-check**: with a small `--games` (e.g. 500) for a fast dev
   loop, confirm the script finishes, and add a lightweight in-script
   assertion that every simulated game ends with exactly 3 outs on every
   completed half-inning and a decided (non-tied) final score - mirroring
   the same invariant `key_moments_build.py`'s docstring says its own
   replay is checked against.
4. **Full run**: run Stage 1 at the real `--games` count, note wall-clock
   time, run the two-seed convergence check (rerun with a different
   `--seed`, diff the two output tables - common states should agree
   closely, rare states may still show noise; raise `--games` if rare
   states relevant to leverage - 2 outs, RISP - are still noisy at the
   chosen count), then run Stage 3 and read the report.
5. **Regression**: confirm `win_probability_table.csv` is byte-for-byte
   unchanged (nothing in this plan writes to it), confirm
   `python -m py_compile utils.py` still passes (nothing in `utils.py`
   changes), confirm the live app is unaffected - this entire plan is
   additive/read-only against production code paths until Alex explicitly
   decides to adopt the new table.

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

## Implementation update - first run findings (Opus, 2026-08-01)

Opus implemented the plan and ran it at 2,000,000 games. One real issue
surfaced, plus two design requests from Alex in response - all three
addressed here.

### The 481-gap / `_wp_post_play` finding

**Diagnosis (confirmed correct, not a Monte Carlo quality problem):** the
simulated table has 481 gaps inside the `[-10, 10]` domain, concentrated at
two boundaries:

- `remaining=12` (top of the 1st) with a negative lead - genuinely
  unreachable (the away team, batting first, can't be behind before the
  home team has taken a single at-bat) and, importantly, **never queried**
  by anything in the app - the only lookup at game start uses `lead=0`
  exactly (`utils.compute_game_wp_series`'s `wp0` line), and `remaining`
  only ever counts *down* from 12, so nothing ever transitions *into*
  `remaining=12`. Harmless.
- `remaining=1` (bottom of the last inning) with a **positive** lead - this
  one is not harmless. It represents "the home team is already ahead as
  they're about to bat in the last inning," which by the real walk-off rule
  (`key_moments_build._is_walkoff_final`'s condition 1) means **the game is
  already over and this half-inning is never played** - so no table, empirical
  or simulated, will ever have real data there. `_wp_post_play`
  (`utils.py:286-302`) doesn't know this: its generic "flip perspective for
  the next half-inning" branch (`1.0 - get_win_probability_interpolated(remaining - 1, 0, "000", -new_bl)`)
  queries this dead cell every time a top-of-the-last-inning play ends the
  half with the home team ahead. `get_win_probability_interpolated`'s
  clamp-to-nearest-lead fallback then returns whatever the nearest *populated*
  lead's WP happens to be (e.g. ~0.34) instead of the true answer (1.0 - the
  home team has already won), producing the leverage blowups Opus measured
  (LI 0.00 -> 5.36 at one state; pooled mean LI 0.61 -> 0.95 with this
  artifact included, 0.10/88-crossings/3.4%->3.8% without it).

**Resolution:**

1. **Fix the guard in `_wp_post_play` itself, not by patching either CSV.**
   Detect the specific case - next half-inning is `remaining=1` and the
   about-to-bat team's `new_bl` (from their own perspective, i.e. `-new_bl`
   as currently computed) is positive - and short-circuit to `1.0` directly,
   instead of doing the table lookup at all. This fixes the bug for *every*
   table (today's empirical one and the new simulated one) permanently,
   rather than requiring each table-generation script to remember to
   backfill the same ~20 cells by hand. This is a small, precisely-scoped
   correction to an existing win-probability-lookup edge case - not the kind
   of open-ended "improve the leverage formula" work this plan's "Why"
   section says is out of scope, but it *is* real production `utils.py`
   code, so it's still a change Alex should review before it ships, same as
   Opus already treated it.
2. **Check whether today's live `win_probability_table.csv` has the same
   gap.** If the empirical table also lacks positive-lead data at
   `remaining=1` (likely, since real games never play out that half-inning
   either), this bug already affects production leverage today, independent
   of whether the Monte Carlo table is ever adopted - that's a materially
   different priority (fix now) than "clean up before comparing tables."
3. **No change needed to the `remaining=12` gaps** - confirmed harmless per
   the diagnosis above, leave them as gaps.
4. The rest of the comparison report (the `remaining > 2` figures) is
   unaffected by this issue and can be evaluated independently while the
   `_wp_post_play` fix is reviewed separately.

### Two design changes requested by Alex, addressed together below

1. **Incremental/resumable simulation** - re-running the simulator should
   *add* games to the existing pool by default, not discard prior work and
   start over.
2. **State-seeded rollouts** - added to the plan per the tradeoff discussed
   earlier: instead of (or in addition to) relying on full games organically
   wandering into rare states, directly seed rollouts *at* every
   `(remaining, outs, obc, batting_lead)` cell and simulate forward from
   there, guaranteeing every cell gets controllable sample size regardless
   of real-world rarity.

These two turn out to unify well with each other and with the original
full-game design - see the rewritten Stage 1 below, which replaces the
original single-mode version.

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
| Simulator mode flag | `--mode {full-game, state-seeded, backfill}` (default `full-game`) |
| Simulator sizing flags | `--games` (full-game mode), `--rollouts-per-state` (state-seeded/backfill), `--min-n` (backfill's thinness threshold) |
| Simulator accumulation flags | `--fresh` (discard existing pool and start over; default is to accumulate) |
| Reused, untouched | `utils.advance_runners`, `utils.outs_added`, `utils.remaining_half_innings`, `utils.game_innings`, `key_moments_build._is_walkoff_final`, `utils.compute_leverage`, `utils._compute_avg_wp_swing` |
| New, small, reviewed-separately | `_wp_post_play`'s `remaining=1`/positive-lead guard (see "Implementation update" above) |

---

## Stage 1 - `simulate_win_probability.py` (the simulator itself)

Rewritten from the original single-mode design to add resumable
accumulation and state-seeded rollouts, per Alex's follow-up requests.
**Both new capabilities share the same underlying mechanic as the original
full-game design** - "start at some state, simulate forward to a decided
finish, backfill every state visited along the way with the eventual
winner" - so this is a refactor of Stage 1 into one shared continuation
function called two different ways, not two separate simulators to
maintain.

### 1a. Setup (shared by every mode)

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
  not when weighting a random draw, so skip it here.
- `--seed` CLI flag, **optional** - if omitted, seed from OS entropy
  (`random.seed()` with no argument) so repeated invocations under the
  default accumulate-by-default behavior (1c below) actually add *new*
  games rather than resampling the same ones. Always print whichever seed
  got used (explicit or entropy-derived) so a specific run can be
  reproduced later if needed.

### 1b. The shared continuation function

```
def simulate_from(inning, half, outs, obc, home_score, away_score) -> (events, winner)
```

Loop, starting from whatever state it's called with:

1. `remaining = utils.remaining_half_innings(inning, half, key_moments_build.MLN_INNINGS)`
   (import `MLN_INNINGS` from `key_moments_build.py:38` rather than
   re-deriving it - it's already `utils.LEAGUE_INNINGS.get("MLN", 6)`, no
   need for a second source of truth).
2. `batting_is_home = (half == "bottom")`;
   `batting_lead_before = (home_score - away_score) if batting_is_home else (away_score - home_score)`.
3. **Record the pre-play snapshot**: `(remaining, outs, obc, batting_lead_before, batting_is_home)`
   appended to `events` (not the global accumulator yet - the win/loss
   label isn't known until the whole continuation ends, see 1d).
4. Look up `dist.get((outs, obc))`. Given the exactness guarantee in "The
   star of the show" above, every `(outs, obc)` the simulator can reach
   from a legal starting state has a non-empty entry. No fallback branch is
   needed; if one is ever hit, assert loudly rather than silently defaulting
   - that would mean a state-seeded rollout was started from an illegal
   `(outs, obc)` combination, a bug in the seeding logic (1c), not a runtime
   case to design around.
5. Sample `result` from `dist[(outs, obc)]`.
6. `new_obc, runs = utils.advance_runners(result, obc, outs)`.
7. `new_outs = min(3, outs + utils.outs_added(result))`.
8. Apply `runs` to whichever team is batting.
9. **Game-end check - reuse, do not reimplement.** Import
   `_is_walkoff_final` from `key_moments_build.py`, call it with the
   post-play `(inning, half, new_outs, home_score, away_score)` exactly as
   `key_moments_build._scoreboard()` already does (`key_moments_build.py:725-727`).
   If `True`, return `(events, winner)`. Otherwise, if `new_outs >= 3`, flip
   half (top->bottom same inning, bottom->top next inning), `outs=0,
   obc="000"`; else `outs=new_outs, obc=new_obc`. Continue the loop.

### 1c. Three ways to call it

**Full-game mode (`--mode full-game`, the original design):** call
`simulate_from(1, "top", 0, "000", 0, 0)` - the canonical game-opening
state - once per game, `--games` times (default `200000`). This is the
"natural" distribution: states get visited proportional to how often real
games actually reach them, which is good for common states and slow to
converge on rare ones (the sparse-cell problem discussed when this plan was
first drafted, and part of what produced Opus's first-run gaps beyond the
two boundary-condition ones).

**State-seeded mode (`--mode state-seeded`, new):** enumerate every
`(remaining, outs, obc, batting_lead)` cell in the table's domain -
`remaining` 1-12, `outs` 0-2, `obc` all 8 values, `batting_lead` -18..18 -
10,656 cells (revised from an initial -10..10/6,048-cell domain - see
"`batting_lead` domain: -18..18, matching production" below for why).
For each, reconstruct a concrete starting game state and call
`simulate_from` `--rollouts-per-state` times (default `500`):

- `remaining -> (inning, half)`: invert `remaining_half_innings` within
  regulation - `hip = MLN_INNINGS*2 - remaining`, `inning = hip // 2 + 1`,
  `half = "bottom" if hip % 2 == 1 else "top"`. This is well-defined and
  unique for every `remaining` 1-12 (`MLN_INNINGS=6` means `hip` ranges
  0-11, one value per `remaining`). Extra-inning depth doesn't need
  separate handling - a `remaining=2` cell reached via a real 9th-inning
  top half behaves identically, going forward, to one reached via a
  regulation 6th-inning top half, since the same capping rule governs
  continuation from either; seeding at the regulation-equivalent inning is
  fully valid.
- `obc`, `outs`: used directly, no translation needed.
- `batting_lead -> (home_score, away_score)`: only the *difference* matters
  anywhere downstream (`_is_walkoff_final` and every WP/leverage function
  only ever compares `home_score` to `away_score`, never an absolute value)
  - so pick any consistent baseline: `home_score = max(lead, 0)`,
  `away_score = max(-lead, 0)` where `lead` is from the batting team's
  perspective and sign-flipped back to home/away as needed based on
  `batting_is_home`.
- Every cell gets guaranteed, controllable sample size regardless of
  real-world rarity - this is the direct fix for the sparse-cell problem,
  and, as a side effect, would also produce a sensible (if approximate)
  answer at the `remaining=1`/positive-lead boundary cells discussed above
  even before the `_wp_post_play` guard fix ships (forcing a rollout to
  "play out" that dead half-inning still converges close to 1.0, since the
  home team's existing lead going in makes losing that specific inning
  extremely unlikely) - complementary to, not a substitute for, the
  `_wp_post_play` fix, which is exact where this is merely close.

**Backfill mode (`--mode backfill`, new, the recommended default workflow
after an initial full-game pass):** load the existing
`simulated_win_probability_table.csv`, find every cell with `n <
--min-n` (default `200`), and run state-seeded rollouts (1c above) *only*
for those cells. This avoids paying for all 10,656 cells at full rollout
count every time - most cells get plenty of coverage from a big full-game
run; only the genuinely rare ones need direct seeding.

### `batting_lead` domain: -18..18, matching production (revised 2026-08-01)

The original design clamped `batting_lead` to `[-10, 10]`, copied from the
clamps inside `get_win_probability`/`get_win_probability_interpolated`
(`utils.py:181`, `214` - both do `max(-10, min(10, int(batting_lead)))`
before any lookup). That was an incomplete read of "the existing table's
domain": those two functions clamp every *query* to `[-10, 10]`, but the
*stored* `win_probability_table.csv` file itself actually carries data out
to `[-18, 18]` (37 distinct lead values, confirmed by inspection) - cells
between 11 and 18 in magnitude that the app has apparently never actually
queried, since the query-side clamp cuts them off first. Alex wants the
simulated table to match the *file's* domain, not the narrower query clamp,
so **`batting_lead` is now `[-18, 18]` everywhere in this plan**: the
state-seeded enumeration (10,656 cells, previous section), the accumulation
clamp below, and the coverage-summary range.

**This is a file-level match only, not a behavior change.** `utils.py`'s
query clamps stay at `[-10, 10]` unless and until that's separately decided
- widening the simulated table's stored range doesn't by itself change what
the live app ever looks up. If the cells from 11-18 in magnitude should
actually start getting used (e.g. blowout leverage/WP no longer flattens at
±10), that requires also widening those two clamp lines - a small,
`utils.py`-touching change in the same spirit as the `_wp_post_play` guard
fix above, and one that should get the same explicit review rather than
being bundled into this data-generation work.

### 1d. Accumulation - resumable by default

**Re-running the simulator adds to the existing pool; it does not replace
it, unless `--fresh` is passed.** To make this correct:

- Store the raw win **count**, not just a probability, in the output file -
  add an explicit `win_sum` column (always a whole number, since each
  labeled event contributes exactly `1` or `0`) alongside `n`. `win_prob`
  stays in the file too, as a derived convenience column
  (`win_sum / n`), but accumulation always reconstructs from `win_sum`/`n`
  directly rather than reverse-engineering a sum from a rounded probability
  - avoids any precision loss across repeated accumulation.
- On startup (unless `--fresh`), if `simulated_win_probability_table.csv`
  already exists, load its `(remaining, outs, obc, batting_lead) ->
  (win_sum, n)` rows as the accumulator's starting point; add this run's
  results on top; write the combined totals back to the same file.
- Persist a small sidecar, `simulated_win_probability_meta.json`, tracking
  cumulative totals across all runs (`total_games` for full-game mode,
  `total_rollouts` per mode, the list of seeds used, last-run timestamp) -
  purely informational bookkeeping so it's visible how much total
  simulation backs the current file without having to keep every run's
  terminal output around.
- Clamp `batting_lead` to `[-18, 18]` **only when bucketing into the
  accumulator** (matches `win_probability_table.csv`'s actual stored
  domain, not the narrower `[-10, 10]` query clamp in
  `get_win_probability`/`get_win_probability_interpolated` - see
  "`batting_lead` domain" above) - do not clamp the value used to decide
  `won`, only the key used to file it into the output table.
- `won = 1.0 if (batting_is_home == (home_score > away_score)) else 0.0`
  for every recorded event once a continuation's winner is known - the
  same two-pass "simulate forward, label backward" discipline as the
  original design, unchanged: there is no way to know a mid-continuation
  state's eventual win/loss before the continuation finishes, so don't try
  to compute WP incrementally during the walk.

### 1e. Output

`simulated_win_probability_table.csv` columns:
`remaining,outs,obc,batting_lead,win_prob,win_sum,n`. `utils._load_wp_table()`
only reads the four named key columns plus `win_prob` (`utils.py:110-113`),
so the extra columns are silently ignored if this file is ever pointed at
by that loader - they exist for Stage 3's reporting and for this script's
own resumability, not for production consumption.

Print a coverage summary every run: how many `(remaining, outs, obc)`
combinations have data across the full `batting_lead` range `[-18, 18]` vs.
only a partial range, and the count of cells with `n` below the visibility
threshold - this is exactly the signal that tells you whether to follow up
with `--mode backfill`.

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
3. **Stage 1 hand-check**: with a small `--games` (e.g. 500, full-game mode)
   for a fast dev loop, confirm the script finishes, and add a lightweight
   in-script assertion that every simulated continuation ends with exactly
   3 outs on every completed half-inning and a decided (non-tied) final
   score - mirroring the same invariant `key_moments_build.py`'s docstring
   says its own replay is checked against.
4. **State-seeded hand-check**: run `--mode state-seeded` at a small
   `--rollouts-per-state` (e.g. 20) for one deliberately-chosen cell (e.g.
   `remaining=6, outs=1, obc="010", batting_lead=2`), confirm the
   reconstructed starting `(inning, half, home_score, away_score)` is
   correct by hand, and confirm every rollout's first recorded event
   matches that exact seeded state (a bug in the seeding math would show up
   immediately as the seeded cell itself having the wrong `(remaining,
   outs, obc, batting_lead)` in its own output row).
5. **Resumability check**: run Stage 1 twice in a row at a small `--games`
   count without `--fresh`, confirm `n` roughly doubles across the affected
   cells (not resets), confirm `win_sum <= n` always holds (a violated
   invariant would mean the accumulate-vs-fresh logic is buggy), and confirm
   two consecutive runs use different seeds by default (print them, compare).
6. **Full run**: run Stage 1 in full-game mode at the real `--games` count
   (starting default `200000`, `--fresh` for a clean baseline), note
   wall-clock time, run the two-seed convergence check (rerun with a
   different `--seed`, diff the two output tables - common states should
   agree closely, rare states may still show noise), then run `--mode
   backfill` with the default `--min-n` and confirm the coverage summary's
   thin-cell count drops to (near) zero afterward, then run Stage 3 and
   read the report.
7. **Regression**: confirm `win_probability_table.csv` is byte-for-byte
   unchanged (nothing in Stages 1-3 write to it), confirm
   `python -m py_compile utils.py` still passes, confirm the live app is
   unaffected. (Note: the `_wp_post_play` guard fix from the "Implementation
   update" section above has already landed in `utils.py:300-309` and is a
   real, reviewed production change - Stage 4 below is a second, separate
   production change, not part of Stages 1-3's read-only scope.)

---

## Stage 4 - leverage constant refresh (`win_probability_table.csv` stays exactly as-is in prod)

Requested by Alex once the simulated table and `result_ranges_re24.csv`
were both in hand: **do not adopt the simulated win-probability table** (the
build-vs-compare decision from "Resolved decisions" #3 stays undecided/open)
- instead, use the *pieces this project already produced and trusts* -
`result_ranges_re24.csv` and the simulation's state-visitation counts - to
improve `_AVG_WP_SWING` (the shared leverage denominator) and to give
`key_moments_build.py`'s leverage numerator a properly situational input,
without touching `win_probability_table.csv`, `compute_win_probability.py`,
or `compute_leverage()`'s own signature/behavior at all.

**Important consequence to sign off on before this ships**:
`_AVG_WP_SWING` is one constant shared by *every* `compute_leverage()` call
in the app - Scouting's included, even though Scouting's own `result_ranges`
input isn't touched by this stage. Improving the denominator will shift the
leverage *number* shown on the Scouting page too (a better-calibrated shared
baseline, not a bug), and `LEVERAGE_THRESHOLD = 2.0`
(`key_moments_build.py`) plus any Scouting-side leverage-based tuning need
re-checking against the new baseline, not an assumption that the old
threshold still means the same thing. See validation step 4d below.

### 4a. New situational range lookup - `utils.py`, near `RESULT_RANGES` (`utils.py:536-554`)

```python
_RE24_RANGES: dict[tuple[int, str], list[tuple[str, int, int]]] = {}

def _load_re24_ranges() -> None:
    global _RE24_RANGES
    try:
        df = pd.read_csv("result_ranges_re24.csv", dtype={"obc": str})
        df["obc"] = df["obc"].str.zfill(3)
        ranges: dict[tuple[int, str], list[tuple[str, int, int]]] = {}
        for _, row in df.iterrows():
            key = (int(row["outs"]), row["obc"])
            ranges.setdefault(key, []).append(
                (str(row["Result"]), int(row["Low"]), int(row["High"]))
            )
        _RE24_RANGES = ranges
    except FileNotFoundError:
        pass

_load_re24_ranges()
```

Place the module-level load call alongside `_load_result_frequencies()`/
`_load_state_frequencies()` (`utils.py:282-283`) - same pattern, same
FileNotFoundError-is-a-silent-no-op discipline as every other CSV this file
loads at import time.

### 4b. `compute_leverage_re24` - new function, right after `compute_leverage` (`utils.py:343-363`)

```python
def compute_leverage_re24(remaining: int, outs: int, obc: str, batting_lead: int) -> float | None:
    """Leverage using result_ranges_re24.csv's per-(outs,obc) situational
    diff-band table instead of a caller-supplied matchup range list - for
    contexts with no stadium-sheet ranges available (Key Moments build,
    scoreboard). compute_leverage() itself is unchanged; this just supplies
    the right situational slice of ranges to it."""
    ranges = _RE24_RANGES.get((int(outs), str(obc)))
    if not ranges:
        return None
    return compute_leverage(ranges, remaining, outs, obc, batting_lead)
```

Zero changes to `compute_leverage()`'s own body - it already accepts any
`(result, lo, hi)` list, so this is purely "supply a better list," not "add
a new code path inside the leverage formula." Scouting's call site
(`pages/2_Scouting.py:3962`) is not touched at all.

### 4c. `key_moments_build.py` - swap the two call sites

- `replay_game()`, `key_moments_build.py:401-403`: replace
  `utils.compute_leverage(utils.RESULT_RANGES, remaining, outs_before, obc_before, lead_before)`
  with `utils.compute_leverage_re24(remaining, outs_before, obc_before, lead_before)`.
- `_scoreboard()`, `key_moments_build.py:762` (the "leverage of the state
  following the latest play" fix from earlier): same swap, same arguments
  (`remaining_now, outs_after, obc_after, lead_now`).
- `utils.RESULT_RANGES` stays in `utils.py` untouched - it's still the
  Scouting page's fallback/default template for contexts without a fetched
  matchup, unrelated to this change.

### 4d. `_AVG_WP_SWING` denominator upgrade - `utils.py:318-341`

Two changes to `_compute_avg_wp_swing()`, both additive to the existing
loop structure (the outer loop still walks `_WP_BY_STATE`, i.e. every state
present in **prod's `win_probability_table.csv`** - that table's role as
the source of `wp_cur`/`wp_after` values is completely unchanged):

1. **Per-state result distribution**: replace the flat
   `_LI_AVG_PROBS.items()` iteration with a lookup into `_RE24_RANGES` for
   *that specific state's* `(outs, obc)`, converting each range to a
   probability the same way `compute_leverage`'s own numerator already does
   (`min((hi - lo + 1) * 2 / 1000, 1.0)`) - so "expected swing at this
   state" is computed from results that can actually happen there, not a
   distribution that includes e.g. double-play results at a 2-out state
   where the theoretical table (correctly) has none.
2. **State weighting**: new `_SIM_STATE_WEIGHTS` dict (see 4e), used in
   place of `_STATE_WEIGHTS` **only inside this function** - do not touch
   `_STATE_WEIGHTS`/`state_frequencies.csv` itself, since
   `batter_optimizer.py:55` reads `_utils._STATE_WEIGHTS` directly for an
   unrelated purpose and has no reason to be affected by this leverage-only
   change. (A shared upgrade might be worth doing later - a larger, more
   consistent sample would plausibly help `batter_optimizer.py` too - but
   that's a separate decision for a separate conversation, not bundled in
   here.)

```python
def _compute_avg_wp_swing() -> None:
    global _AVG_WP_SWING
    if not _WP_BY_STATE:
        _AVG_WP_SWING = 0.04
        return
    total = 0.0
    weight_sum = 0.0
    for (rem, outs, obc_s) in _WP_BY_STATE:
        wp_cur = get_win_probability_interpolated(rem, outs, obc_s, 0) or 0.5
        ranges = _RE24_RANGES.get((outs, obc_s), [])
        swing = sum(
            min((hi - lo + 1) * 2 / 1000, 1.0) * abs(_wp_post_play(res, rem, outs, obc_s, 0) - wp_cur)
            for res, lo, hi in ranges
        )
        w = _SIM_STATE_WEIGHTS.get((rem, outs, obc_s), 1.0)
        total += w * swing
        weight_sum += w
    _AVG_WP_SWING = total / weight_sum if weight_sum else 0.04
```

`_LI_AVG_PROBS`/`_load_result_frequencies()`/the `result_frequencies.csv`
load become dead code once this ships (grep confirms zero other consumers,
unlike `_STATE_WEIGHTS`) - remove them rather than leaving unused code
around, per repo convention.

### 4e. `_SIM_STATE_WEIGHTS` - new loader, near `_load_state_frequencies` (`utils.py:268-283`)

```python
_SIM_STATE_WEIGHTS: dict[tuple[int, int, str], float] = {}

def _load_sim_state_weights() -> None:
    global _SIM_STATE_WEIGHTS
    try:
        df = pd.read_csv("simulated_win_probability_table.csv", dtype={"obc": str})
        df["obc"] = df["obc"].str.zfill(3)
        grouped = df.groupby(["remaining", "outs", "obc"])["n"].sum()
        _SIM_STATE_WEIGHTS = {
            (int(rem), int(outs), str(obc_s)): float(n)
            for (rem, outs, obc_s), n in grouped.items()
        }
    except FileNotFoundError:
        pass

_load_sim_state_weights()
```

Sums `n` across every `batting_lead` bucket for each `(remaining, outs,
obc)` - a `(remaining, outs, obc)`-level visitation weight, matching
`_STATE_WEIGHTS`'s own key shape exactly (`utils.py:274-277`), just sourced
from `simulated_win_probability_table.csv` (millions of simulated events)
instead of `state_frequencies.csv` (a static historical snapshot). If that
file is absent (e.g. a checkout that hasn't run Stage 1 yet),
`_SIM_STATE_WEIGHTS` stays empty and `_compute_avg_wp_swing`'s
`.get(key, 1.0)` fallback degrades to equal weighting - a graceful, if less
accurate, fallback rather than a crash.

---

## Validation - Stage 4

1. **Compile/boot**: `python -m py_compile utils.py key_moments_build.py`.
2. **`_RE24_RANGES` sanity**: confirm `compute_leverage_re24` returns `None`
   (not a crash) for any `(outs, obc)` combination not present in
   `result_ranges_re24.csv` - shouldn't happen given the 24-combination
   coverage already verified, but the function should degrade gracefully,
   not assume coverage.
3. **Numerator hand-check**: pick one real Key Moments play, compute its
   leverage by hand using `result_ranges_re24.csv`'s row for that exact
   `(outs, obc)` and compare against `compute_leverage_re24`'s output -
   should match exactly (same formula, same inputs).
4. **Denominator hand-check**: confirm `_AVG_WP_SWING` recomputes to a
   different (and log/print both old and new values, don't just assert
   "different") number than before this stage - if it's identical, something
   didn't wire up (e.g. `_RE24_RANGES`/`_SIM_STATE_WEIGHTS` silently failed
   to load and the function fell through to old-equivalent behavior).
5. **App-wide leverage shift check (the consequence flagged above)**: run
   `key_moments_build.py` and compare the `leverage`/`high_leverage` tag
   counts against a pre-Stage-4 baseline run; separately, load the Scouting
   page for a matchup with real `result_ranges` and confirm its displayed
   leverage number changed too (expected) and is still a sane, non-degenerate
   value (not expected to be wrong, but the *number* moving is the point of
   this check). Re-run `scripts/calibrate_stoplight_thresholds.py` and
   review whether `LEVERAGE_THRESHOLD = 2.0` (`key_moments_build.py`) and
   `WPA_THRESHOLD` still land near their intended percentile targets under
   the new baseline, or need retuning - this is the direct follow-up to the
   consequence flagged at the top of this stage, not optional cleanup.
6. **Regression**: `batter_optimizer.py` still reads the untouched
   `_STATE_WEIGHTS`/`state_frequencies.csv` and produces unchanged output -
   confirm this explicitly given how easy it would be to accidentally wire
   `_SIM_STATE_WEIGHTS` into the wrong place during implementation.
7. **`win_probability_table.csv` untouched**: confirm byte-for-byte, same
   as every other stage in this plan - Stage 4 only ever *reads* it via the
   existing `_WP_LOOKUP`/`_WP_BY_STATE`/`get_win_probability_interpolated`
   machinery, never writes to it.

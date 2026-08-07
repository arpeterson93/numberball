Review the ball-flight physics code in this repo (MLN Gameday, a static-site baseball animation). Do a genuinely independent review first — don't anchor on anything below until Part 2. I want you to rediscover issues on your own if they're there, not just confirm what I already found.

## What to read
- `docs/js/app.js` — the implementation. Key functions/constants, with current line numbers:
  - `HZ_FIELDER_BY_ANGLE` (~1055), `INFIELDER_DEPTH_FT` (~1062), `GROUND_ARCHETYPES` (~1071) — real infield fielding depths (P=60ft, 3B=119ft, 1B=111ft, SS/2B=147ft) and which archetypes count as "touches the ground."
  - `landingPoint` (~1234), `clampToFence` (~1243) — trajectory-to-field-coordinate math.
  - `launchAngleFor` (~1328), `flightParams` (~1340) — computes launch angle (LA), exit velo (EV), and distance (D) per play from `result_diff_bands.csv`'s per-result percentile ranges (`laMin`/`laIdeal`/`laMax`/`evMin`/`evMax`/`depthMin`/`depthMax`) and a `q` value (0..1, position of this play's pitch/swing "diff" within its result's band — 1 = best-timed/hardest contact).
  - `applyPositionOverride` (~1412), `applyGroundBallFielderDepth` (~1430) — post-hoc corrections applied to `flight` right after `flightParams` returns.
  - `ballTravelMs` (~1478), `ROLLOUT_FT` (~1495), `rolloutFraction` (~1500), `STAYS_IN_INFIELD_ARCHETYPES` (~1516), `dirtEdgeFt` (~1517), `groundBallRolloutFt` (~1532) — the rollout system. Give this the most scrutiny.
- `result_diff_bands.csv` — one row per MLN result code, with real Statcast-derived `la_min`/`la_ideal`/`la_max`/`ev_min`/`ev_max`/`depth_min`/`depth_max` plus a `flight_source` column noting provenance.
- `compute_flight_ranges.py` — the script that generates those Statcast-derived columns (via `pybaseball`). Read its module docstring and filter logic — decide for yourself whether the filters and the percentile choices are sound, don't take them as given.
- `ball_flight_test.py` — the existing Playwright-based pure-function test suite. Treat its passing assertions as the current contract; separately judge whether the contract itself is complete.

## Part 1: independent review

Go through the whole ball-flight pipeline (LA/EV/distance computation, launch-angle-to-ideal anchoring, HZ spray angle, fielder-depth capping, rollout, fence clamping, hang time) as if you're seeing it cold. Look for:
- Formulas that are wrong, inconsistent with each other, or inconsistent with what the data actually contains (read `result_diff_bands.csv` itself, not just the code that consumes it).
- Places where a "distance" or "position" value could be the wrong physical quantity (e.g. a landing point silently used as a final/fielded position, or vice versa) — anywhere in the pipeline, not just grounders.
- Boundary conditions: zero/negative values, a percentile range that could invert, a lookup that could miss (null fielder position, unmapped archetype), a cap and a floor fighting each other.
- Whether `ball_flight_test.py` actually covers the pipeline's real risk points, or just the parts that were easy to test.

Write up what you find before reading Part 2.

## Part 2: known context — verify against it, don't just defer to it

Two bugs were already found and fixed today by another Claude session:

1. **`hit_distance_sc` semantics bug.** Statcast's `hit_distance_sc` measures where a batted ball first touches the ground/lands — for a fly ball that's the same as "caught or landed," but for a ground ball it's the *first-bounce point*, not the (much deeper) spot where an infielder actually fields it after the ball rolls/bounces further. Verified against real data: shortstop-fielded groundouts (real fielding depth 147ft) showed `hit_distance_sc` medians under 15ft. Fix: `grounder`/`infield_single`/`bunt`-family results in `result_diff_bands.csv` use hand-tuned real-infield-depth ranges (60-150ft / 45-90ft / 5-25ft) for `depth_min`/`depth_max` instead of the Statcast-derived value; `la_min`/`la_ideal`/`la_max`/`ev_min`/`ev_max` still come from real Statcast data. **Deliberately NOT applied** to `1B`/`1BWH`/`1BWH2`/`2B`/`2BWH`/`3B` even though those results also include some ground-ball-type hits with the same measurement quirk — a live judgment call (keep the real mix of how these results actually happen), not an oversight.
2. **Rollout formula couldn't reach real infield depth.** `groundBallRolloutFt`'s old formula was `rolloutFraction(ev, la) * ROLLOUT_FT` — a flat 34ft max regardless of which fielder is involved. Real infield depths span 60-147ft, so a fixed 34ft ceiling could never bridge the gap for a modest ball assigned to a deep position (confirmed: a moderate-contact grounder landing at 87ft only reached ~102.8ft total, 44ft short of a real shortstop). Fix: rollout now scales as `rolloutFraction(ev, la) * (depth - flight.distance)` — proportional to the actual remaining gap to the assigned fielder's real depth.

Now: did your Part 1 review independently surface either of these? If not, go look specifically at why they were easy to miss on a fresh read — that's useful signal about blind spots in the code's own structure/comments, not just a gap in your pass. Then critique the fixes themselves:
- Is the rollout fix correct at its boundaries — what happens when `flight.distance` already exceeds `depth` (should be pre-capped by `applyGroundBallFielderDepth` to exactly 0 rollout — verify this interaction, don't assume it)? What happens when `HZ_FIELDER_BY_ANGLE` has no entry for a given angle? Does `rolloutFraction`'s own EV/LA normalization constants (`ROLLOUT_EV_LOW/HIGH`, `ROLLOUT_LA_LOW/HIGH`) make sense as one universal scale across every archetype, given everything else in this pipeline is now per-result?
- Is the "deliberately not applied to 1B/2B/3B/etc." call actually right, or does it leave a real, un-flagged inconsistency?
- Does `launchAngleFor`'s ideal-anchoring formula (~1328) actually structurally prevent `laIdeal` from collapsing onto `laMin`/`laMax`, checked against the real CSV data rather than taken on faith?

## What to give back
For each finding (from either part): file + line number, what's wrong or unverified, a concrete failure scenario, and a severity/confidence read. Separate "real bug" from "works today but only by luck/coincidence." Say plainly which of today's two fixes you'd have caught on your own and which you wouldn't have.

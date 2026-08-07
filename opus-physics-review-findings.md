# Ball-flight physics review - findings

Review of the MLN Gameday ball-flight pipeline, in response to
`opus-physics-review-prompt.md`. Date: 2026-08-07. Reviewer: Claude (Opus 5).

Scope: `docs/js/app.js` (LA/EV/distance, HZ spray angle, fielder-depth capping,
rollout, fence clamping, hang time), `result_diff_bands.csv`,
`compute_flight_ranges.py`, `ball_flight_test.py`.

---

## Up front: a caveat that affects Part 2's main question

The working tree already contains both of the fixes described in the prompt's
Part 2 (`result_diff_bands.csv` has the hand-tuned grounder depths,
`app.js:1565` has the gap-scaled rollout). So Part 1 was necessarily a review of
*post-fix* code - neither bug could be "rediscovered" in the literal sense. What
follows is what the fresh read did turn up, plus a calibrated read on the
counterfactual, with evidence rather than a claim (see "Did Part 1 surface the
two known bugs?").

---

# Part 1: independent findings

## F1 - Position override defeats the fielder cap entirely; ball rolls past the fielder it was snapped to

**`docs/js/app.js:1550`** (with `:1412`, `:2899`) - **Real bug - High confidence - Live on real data**

`applyPositionOverride` rewrites `flight.angle` to `atan2(anchor) + 45`, an
arbitrary real number. Every consumer downstream assumes `flight.angle` is on
the 11-value HZ lattice (5, 13, ..., 85). It isn't anymore:

| override target | anchor dist | resulting angle | `Math.round` | `HZ_FIELDER_BY_ANGLE` |
|---|---|---|---|---|
| 3B | 113.4 ft | 3.58 deg | 4 | **miss** |
| SS | 150.4 ft | 29.58 deg | 30 | **miss** |
| 2B | 150.4 ft | 60.42 deg | 60 | **miss** |
| 1B | 113.4 ft | 86.42 deg | 86 | **miss** |
| P | 60.0 ft | 45.00 deg | 45 | hit |

At `app.js:2899` the caller does
`if (!applyPositionOverride(...)) applyGroundBallFielderDepth(...)` - correct and
deliberate. But `groundBallRolloutFt` repeats the HZ lookup independently and has
no idea an override happened. The lookup misses, `depth` is null, the
`if (depth != null)` block is skipped, and **`rollFt` silently falls back to the
old flat `rolloutFraction * ROLLOUT_FT` with `maxReachFt` left at the fence.**

Concrete failure - `DP31` (grounder, `ExcludedPositions=2B`,
`DefaultPosition=3B`, live in `import_BRC.csv`). The override fires on 20/121 of
the (q, angle) grid:

```
q=0.6: snapped to 3B anchor at 113.4ft, then rolls 23.0ft -> 136.4ft
q=1.0: snapped to 3B anchor at 113.4ft, then rolls 27.1ft -> 140.5ft
```

The third baseman is at 119 ft. The ball rolls 17-22 ft *behind* him, and since
`throwHtml:1901` uses the rollout endpoint as `fieldPt`, the throw to first
originates from empty grass past the fielder - the exact failure the override
existed to prevent. `TP` (`SS,2B,1B,P` -> `3B`) is the same path.
`BDP`/`BFC`/`BGO`/`SacB` share the config but are masked (see F8);
`DFO`/`SacF` are masked by the `CAUGHT_IN_AIR` early return.

This is the "cap and floor fighting each other" case, in its nastiest form: the
cap doesn't fight the floor, it silently ceases to exist.

## F2 - `1B`/`2B` depth ranges are first-bounce points consumed as landing points

**`result_diff_bands.csv:29,8`; `compute_flight_ranges.py:68-73`** - **Real bug - High confidence**
*(this is the "deliberately not applied" call - see Part 2)*

`1B`'s filter is `events=='single' & hit_location IN (7,8,9)` - **an outfielder
fielded it** - yet `depth_min` is **27.0 ft**. `2B`'s is 40.0 ft. Those are not
landing points for outfield hits; they are the ground-ball members of the sample
contributing their first-bounce distance.

The docstring's defense is that a legged-out ground ball "belongs in the sample."
That reasoning is sound for `la`/`ev` - correctly noted as measured at contact
and distance-independent - but it does not transfer to depth, because depth is
precisely the column with the semantics defect. The result is that `1B`'s 27->255
ft range is a **mixture of two incompatible physical quantities**, and `q` then
linearly interpolates *between* them, so a mid-`q` single lands at a distance
that is neither a real bounce point nor a real carry distance.

It's currently papered over by the dirt-clearance floor, and the paper is
visible:

```
1B at q=0: lands 27ft, floor forces 131.5ft of rollout -> 158.5ft
```

131 feet of "roll" in a fixed `ROLLOUT_MS = 320` (`app.js:1591`) is roughly
280 mph. The endpoint is plausible; the mechanism producing it is not.

## F3 - `flight.fielder` and the HZ-assigned fielder disagree on 39% of the grounder grid

**`docs/js/app.js:1376, 1416`** - **Real bug - High confidence**

`flight.fielder` is set by `nearestFielder(pt)`. The comments at `:1048-1054` and
`:1063-1071` establish the HZ angle as the authoritative "who fields this
grounder" signal. These are two different answers, and they disagree in 47/121 of
the grounder (q, angle) grid - every shallow grounder resolves to `P` by
proximity regardless of direction:

```
q=0.0 angle=21 D=60.0  HZ says SS but nearestFielder says P
q=0.0 angle=53 D=60.0  HZ says 2B but nearestFielder says P
```

This matters because `applyPositionOverride:1416-1418` makes its exclusion
decision against `flight.fielder` - the proximity answer - not the HZ answer. So
a `DP31` grounder hit toward 2B whose weak contact puts it nearest `P` will
**not** trigger the `2B` exclusion, and a grounder the HZ says is going to `SS`
can trigger an exclusion written about `P`. `applyGroundBallFielderDepth:1440`
only repairs `flight.fielder` when it actually caps; on the no-cap path the wrong
value survives.

## F4 - `rolloutFraction`'s universal EV/LA scale is incoherent with the now per-result pipeline

**`docs/js/app.js:1496-1503`** - **Works today, but the stated intent is defeated - High confidence**

`ROLLOUT_EV_LOW/HIGH = 40/115` and `ROLLOUT_LA_LOW/HIGH = -15/50` are one global
scale, but EV ranges are now per-result and span 22-113 mph. Worse, the two terms
move in *opposite* directions as `q` rises (higher EV increases `evFrac`; LA
moving toward a high `laIdeal` decreases `laFrac`), so they cancel:

```
result      q=0     q=0.5   q=1     spread
1BWH        0.638   0.666   0.691   0.054   <- essentially constant
HR          0.518   0.542   0.563   0.045
2BWH        0.573   0.606   0.635   0.062
BGO (bunt)  0.000   0.000   0.045   0.045   <- floored off the bottom
GO          0.303   0.571   0.808   0.505   <- only result with real range
```

So "harder contact rolls further" is only true for `GO`-shaped results. For every
high-EV hit the rollout is a constant, and for bunts it's zero. **Direct answer to
the Part 2 question: no, one universal scale is not defensible anymore.** The
normalization endpoints should be per-result (`band.evMin`/`band.evMax`,
`band.laMin`/`band.laMax`), which would restore full dynamic range everywhere at
no cost to the `GO` behavior the current constants were tuned against.

## F5 - Two disagreeing sources of truth for infielder depth

**`docs/js/app.js:1042-1046` vs `:1062`** - **Real inconsistency - High confidence - Low impact today**

| pos | `FIELDER_ANCHORS_FT` implies | `INFIELDER_DEPTH_FT` | delta |
|---|---|---|---|
| 3B | 113.4 ft | 119 | **-5.6** |
| 1B | 113.4 ft | 111 | +2.4 |
| SS / 2B | 150.4 ft | 147 | +3.4 |
| P | 60.0 ft | 60 | 0.0 |

The override snaps to one; the cap and rollout bound against the other. This is
the "Field Scale Consistency" problem in miniature: hand-placed anchors
coexisting with a physics-derived depth table. It also explains why F1 is so
close to harmless - had 3B's anchor been placed at exactly 5.0 deg instead of
3.58 deg, the HZ lookup would have hit and returned the right fielder. F1 is a
one-anchor-placement away from working by accident.

## F6 - `compute_flight_ranges.py` silently NaNs out flight columns for unmapped results

**`compute_flight_ranges.py:223-230`** - **Latent, silent, total-failure mode - High confidence**

```python
prev = pd.read_csv(args.out)
for col in flight_cols:
    prev[col] = prev["result"].map(new[col])
```

Any result present in `result_diff_bands.csv` but absent from `_build_filters`
gets **NaN written over its existing flight columns**. The `unmapped` check at
`:154` is computed against `ARCHETYPE_OF`, not against `prev`'s own rows, and it
only `print`s. Add a result via `compute_result_diff_bands.py` before adding a
Statcast filter here, re-run, and `band.laIdeal`/`evMin`/`depthMin` become `null`
-> `LA`/`EV`/`D` all `NaN` -> `landingPoint` returns `NaN` -> the ball renders at
`NaN,NaN` with no error anywhere. Also, on a cold run with no CSV present, `prev`
is built with only a `result` column, so the output loses
`band_lo`/`band_hi`/`archetype` entirely and `q` becomes `NaN` for every play.

## F7 - `isGrounder` disagrees with `GO`/`DP` at the top of their own LA band; timing is identical by coincidence

**`docs/js/app.js:1307, 1371, 1480`** - **Works only by luck - High confidence**

`GO`'s `la_max` is **4.0** and `DP`'s is **4.2**, against
`GROUND_LA_THRESHOLD = 4`. A worst-timed, below-the-ball `GO` computes
`LA = 4.0`, so `isGrounder` is `false` on a literal groundout. `ballTravelMs` then
takes the air branch: `clamp(398 * 0.35, 450, 1400)` = **450 ms**, which is
exactly `GROUNDER_ROLL_MS = 450`. The timing is right purely because two
unrelated constants happen to coincide. Change `HANG_MS_MIN` or
`GROUNDER_ROLL_MS` independently and groundouts start animating at two different
speeds depending on swing timing. The CSS class also flips to `.air` on these
(`:1616`).

The comment at `:1063-1071` anticipates exactly this and routes all *behavioral*
gating through `archetype` - correctly. `ballTravelMs` is the one place that still
reads `isGrounder`.

## F8 - Bunts don't roll only because `ROLLOUT_EV_LOW` sits above their EV range

**`docs/js/app.js:1496` vs `result_diff_bands.csv:2-7`** - **Works only by luck - High confidence**

Bunt EV is 22-47 mph; `ROLLOUT_EV_LOW` is 40. So `evFrac` is 0 for most bunts and
<= 0.06 at best. That is the *only* reason F1 is harmless for
`BDP`/`BFC`/`BGO`/`SacB`, which share the `2B,SS -> 3B` override config. Nudge
`ROLLOUT_EV_LOW` down or bunt EV up and four more result codes inherit F1's
rolls-past-the-fielder behavior.

## F9 - `laIdeal` collapse is prevented upstream, weakly, and not at all in `app.js`

**`docs/js/app.js:1309-1333`; `compute_flight_ranges.py:131-133`** - **Answer to Part 2 Q3: no, not structurally**

The comment at `:1320-1327` asserts `laIdeal` "always sits meaningfully inside the
range." That guarantee does not exist in `app.js` - it's an emergent property of
`_percentiles` returning q10/q50/q90 of one distribution, and it's only a **weak**
inequality (`<=`, not `<`), further eroded by `round(..., 1)`. A tied or
near-degenerate distribution rounds to collapse.

Checked against the real CSV - no collapse today, but the margins are thin:

```
LODP    laMin=  6.0  laIdeal=  9.0  laMax= 21.0   low-margin=3.0
1BWH2   laMin= 10.0  laIdeal= 13.0  laMax= 18.0   low-margin=3.0
```

If `laIdeal` ever equals `laMin`, `launchAngleFor` returns a constant `ideal` for
every `onTop` swing regardless of `q` - the mistimed-swing-reported-as-ideal
failure the comment was written to prevent, silently reintroduced. Nothing asserts
`laMin < laIdeal < laMax`, in the generator or in the test suite.

## F10 - `hangMs` is drag-free vacuum physics, inconsistent with the Statcast depth it sits beside

**`docs/js/app.js:1372`** - **Works today, cosmetically - Medium impact**

`2*v*sin(theta)/g` is dimensionally correct but implies a vacuum range that
contradicts `flight.distance`: an `HR` at `q=1` (EV 110, LA 28 deg) gives a 4.70 s
hang and a **670 ft** implied range against a Statcast `depth_max` of 428 ft.
Harmless only because `HANG_MS_SCALE = 0.35` plus the `450-1400 ms` clamp discards
most of the signal - most fly balls pin to 1400 and most liners to 450. Worth a
comment saying so; it's currently the one quantity in the pipeline that looks
physical but isn't, and a future reader could reasonably use it as one.

## F11 - `diff == null` silently means "worst possible contact"

**`docs/js/app.js:1351`** - **Latent only - High confidence**

`q = diff == null ? 0 : ...` picks the band's *worst* end, not a neutral 0.5. For
`HR` that means `D = 364 ft` against a 375 ft fence - **every null-diff home run
becomes an inside-the-park home run.** Unreachable today: all 46 null-diff plays
in the real feed are steals/balks (`SB2` x32, `CS2` x12, `Balk`, `CS3`), which
have null `pitch`/`swing` and exit at the `:1344` guard first. A play with
pitch/swing but no diff would hit it.

## Test-suite assessment (`ball_flight_test.py`)

The contract is solid where it exists - the golden `flightParams` cases, the
q-clamp endpoints, the 1000x1000 fence-invariant sweep, the exhaustive
`signedCirc` tie proof, and the throw-timing races are all genuinely load-bearing.
But it tests the parts with clean function boundaries and misses the
interactions, which is where every finding above lives:

- **`applyPositionOverride` is exported at `:2008` and never asserted anywhere.**
  F1, F3 and F5 all live in that function's blast radius.
- **No test that `HZ_FIELDER_BY_ANGLE` covers every angle `flight.angle` can
  hold** *after* post-processing. The comment at `:1048-1054` proves the lattice
  property for `flightParams` output and the test suite takes it as settled - but
  `applyPositionOverride` breaks that invariant afterward and nothing rechecks it.
- **No test of the exact-cap -> zero-rollout boundary** that Part 2 asks about
  (verified by hand below; it holds).
- **No test of the dirt-clearance floor branch** at `:1567-1581`, despite that
  branch doing 131 ft of work on a low-`q` single.
- **No data-validity assertions on the CSV itself**: no
  `laMin < laIdeal < laMax`, no `band_lo != band_hi`, no non-null flight columns.
  F6 and F9 would both be caught by three cheap assertions over `meta.json`.
- The two regression guards added with today's fixes are good but each is a
  **single point sample** - one contact quality
  (`ev:74.8, la:-15, distance:87, angle:29`) and one position. F1 sits one
  `angle` value away from that test and is invisible to it.

---

# Part 2: against the known context

## Did Part 1 surface the two known bugs?

Neither, in the literal sense - both fixes were already in the tree, so there was
nothing left to find. On the counterfactual, with evidence rather than assertion:

**Fix 1 (`hit_distance_sc` semantics) - probably would have caught it, and F2 is
the evidence.** The identical root cause turned up independently, on the data the
fix deliberately left alone: `1B` carries `depth_min = 27 ft` under a filter that
requires an *outfielder* to have fielded it. That came from the CSV and the filter
definitions without the Part 2 context. The grounder version is a strictly more
glaring instance of the same read (`GO` depth vs `INFIELDER_DEPTH_FT`'s 147 ft),
so confidence is reasonable - noting the self-serving direction of that inference.

**Fix 2 (flat `ROLLOUT_FT` can't bridge the gap) - probably would not have caught
it.** Worth being specific about why, since that's the blind-spot signal:

1. **Undershoot violates no checkable invariant.** A grounder fielded at 102 ft
   instead of 147 ft looks like a shallow grounder. There is no assertion it can
   fail and nothing on screen that reads as wrong. Compare F1, the *overshoot*
   direction of the same defect, which the fresh read did catch - because "ball
   ends up behind the fielder" is a violation you can state.
2. **The three constants that had to be compared never appear together.**
   `ROLLOUT_FT = 34` is at `:1495`, `INFIELDER_DEPTH_FT`'s 147 at `:1062`, and the
   60-150 depth band in a CSV. Nothing in any of the three comments referenced the
   other two. `ROLLOUT_FT`'s own comment (`:1484-1494`) is entirely about EV/LA
   *feel* and never mentions reaching a fielder at all - it reads as a
   self-contained cosmetic knob, so the natural review question is "is 34 ft a
   plausible roll?" (yes) rather than "is 34 ft enough to reach anyone?"
3. It's a *sufficiency* bug rather than a correctness bug, and sufficiency bugs
   need an external reference number to detect. The fixed code now carries that
   reference in the comment at `:1554-1564`, which is a real structural
   improvement - that comment is why the defect is now obvious on a cold read.

## Critique of the rollout fix

**Boundary: `flight.distance` already >= `depth`.** Verified, not assumed - and it
holds, conditionally. `applyGroundBallFielderDepth:1435-1436` caps to *exactly*
`depth`, so `Math.max(0, depth - flight.distance)` is exactly `0`, `rollFt` is
`0`, and the final `Math.max(0, Math.min(0, depth - depth))` is `0`. Both gates
(`GROUND_ARCHETYPES` + outs increased) are identical between the two functions, so
they can't disagree about whether to engage. **But this only holds when the
override didn't fire** - which is F1.

**Boundary: `HZ_FIELDER_BY_ANGLE` has no entry.** This is F1, and it's the fix's
weakest seam. The fix moved `rollFt`'s assignment *inside* `if (depth != null)`,
so a lookup miss now silently reverts to the pre-fix flat formula **with the fence
as the only ceiling**. Pre-fix, a miss cost you <= 34 ft of under-roll; post-fix, a
miss costs you an uncapped roll past the fielder. The fix is right on the lattice
and strictly worse off it. Minimal hardening: have `groundBallRolloutFt` return 0
when a `GROUND_ARCHETYPES` out has no HZ entry (fail closed), and better, have
`applyPositionOverride` record the position it chose on `flight` so the rollout
reads it directly instead of re-deriving it from a mangled angle.

**`rolloutFraction`'s universal constants.** Covered in F4 - no, they don't hold up
as one scale. This is the piece of the pipeline that didn't get the per-result
treatment everything else did, and the numbers show it's inert for every result
except `GO`.

## Is "deliberately not applied to 1B/2B/3B" right?

**Half right, and it leaves a real un-flagged inconsistency** - F2. The stated
rationale ("keep the real mix of how these results happen") is correct and
well-argued *for `la`/`ev`*, exactly as `compute_flight_ranges.py:23-25` says:
both are measured at contact and carry no distance semantics, so a ground-ball
single's LA/EV are perfectly valid samples. But the rationale is applied to
`depth` too, where it doesn't hold - "keep the real mix" there means keeping a mix
of two different measurements in one distribution and interpolating across the
boundary.

The distinction the codebase already makes correctly for grounders is
*per-column*, not per-result: la/ev from Statcast, depth hand-tuned.
`1B`/`2B`/`3B` need the same per-column split - keep the full sample for la/ev, but
condition the depth percentiles on `bb_type != 'ground_ball'` so `depth_min`
reflects where non-grounder members of the sample actually landed. That preserves
the judgment call's real intent (don't throw away ground-ball singles) while
removing the quantity mismatch. At minimum it should be flagged in
`flight_source`, which currently reads a bare `own` for exactly the rows where the
caveat applies.

## Does `launchAngleFor` structurally prevent `laIdeal` collapse?

**No.** See F9 - the property is real in today's data but is produced upstream by
`_percentiles`, is only a weak inequality, survives 1-decimal rounding by luck,
and is asserted nowhere. The comment at `:1320-1327` states it as a structural
guarantee of the formula, which overstates what the code does.

---

# Ranked summary

| # | Finding | Class | Sev |
|---|---|---|---|
| F1 | Override -> HZ miss -> rollout escapes fielder cap | real bug, live | **High** |
| F2 | `1B`/`2B` depth = first-bounce consumed as landing | real bug, live | **Med-High** |
| F6 | Generator NaNs out unmapped results' flight columns | latent, silent-total | **Med-High** |
| F4 | Universal rollout EV/LA scale inert on 4/5 archetypes | intent defeated | **Med** |
| F3 | `flight.fielder` vs HZ fielder disagree 39% | real bug | **Med** |
| F5 | Two sources of truth for infield depth | inconsistency | **Med** |
| F9 | `laIdeal` collapse unguarded | latent | **Med** |
| F7 | `isGrounder` vs `la_max`=4.0; timing equal by coincidence | luck | **Low-Med** |
| F8 | Bunts don't roll only because `EV_LOW`=40 | luck | **Low-Med** |
| F10 | `hangMs` vacuum physics vs Statcast depth | cosmetic | **Low** |
| F11 | `diff == null` -> worst contact | latent | **Low** |

F1 and F6 are the two to fix before anything else - both are silent, and F6's
failure mode takes out the whole pipeline. F1 and F3 share a root cause and would
come out as one change.

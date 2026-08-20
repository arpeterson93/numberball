# Gameday animation refinements (round 2) - implementation plan

Handoff plan for Sonnet, produced against `gameday-animation-refinements-fable-prompt.md`.
Round 1 (`gameday-reconciliation-implementation-plan.md`) is adopted and working - this
round extends it. **Out of scope, unchanged from round 1: `docs/js/trajectory.js`, the
ball-flight launch physics, and the fair-territory outfield fence behavior.**

**Priority order, restated (inviolable):**
(a) the rendered play always resolves to exactly what the MLN result says happened;
(b) within that, real distances/speeds/reactions;
(c) per-player attribute uniqueness wherever data supports it.

**Line numbers** below are current as of this writing against the working tree **with the
uncommitted Statcast-acceleration diff applied** (see Stage 0). They drift ~+36 from the
prompt's own citations for everything after ~app.js:4160. Re-grep function names before
editing; every anchor here was verified by direct read.

---

## 0. Probe findings (verified against the repo - these override the prompt where they disagree)

These were all checked directly by reading the code / parsing the data, not assumed.

0.1. **The fact-26 diff is present and uncommitted** (`git diff docs/js/app.js`, 45+/9-).
   It contains everything fact 26 describes **plus one thing the prompt does not mention**:
   a new `RUNNER_ACCEL_FT_S2 = 19.2` (app.js:~4278, Statcast home-to-first fit, n=507) now
   used by both `runnerProfile` (4373) and the fielder `"run"` kind in `fielderProfile`
   (4354), replacing the shared `FIELDER_ACCEL_FT_S2=25` for those two. Infield charge
   keeps 25; OF pursuit gets `OF_PURSUIT_ACCEL_FT_S2=10.2` / `OF_PURSUIT_TOP_SPEED_FT_PER_S=26.4`
   (~4294). **Stage 0 commits this diff as-is before anything else builds on it**, and the
   same commit updates `gameday-reconciliation-implementation-plan.md`'s Section 2 fix-map
   rows for `FIELDER_ACCEL_FT_S2` and the OF pursuit constants (the "who updates the round-1
   constants table" question Alex left open - answer: this plan does, in Stage 0). Task 11's
   spec (a top-speed table) is unaffected by the accel split - top speed and acceleration
   are independent knobs.

0.2. **Open Q1 (is `infieldSingleThrowHtml` load-bearing?): yes, but only for one thing.**
   `infieldSingleThrowHtml` (5697) fires only when `outThrowTargets(...).length === 0`
   (guard at 5699). For an `infield_single` hit with no explicit ThrowOrder, the general
   heuristic emits zero targets (it only emits for OUT moves / recorded outs, 5013-5031),
   so `throwSchedule` returns `[]` and the general path draws **no throw at all**. The
   function's sole surviving job is "synthesize a default decorative throw to 1B on an
   infield single." Everything else it does (contestedSafe reconciliation against
   `batterFirstArrivalMs`) is already the general path's behavior. **Disposition: fold and
   delete** - see Task 1 below. Side benefit: the general path gives the flight ball its
   handoff fade (`ballFlightHtml`:3867 reads `throwSchedule`), which the parallel function
   never did, and Task 2's receiver-token movement then applies to infield singles for free.

0.3. **Open Q2 (`runnerForSafeTarget` multi-runner): a real, confirmed misidentification,
   for HOME targets only.** Two runners can only share a destination when both score
   (`reached === "HOME"`, 5364). `resolveRunnerMoves`/`deriveRunnerMoves` order moves
   most-advanced-first (1664-1688), so the `forEach` at 5362 returns the **lead** scorer -
   the *earliest* arriver. A `contestedSafe` throw home on (say) a single scoring runners
   from 2B and 3B then reconciles against the 3B runner's 1-leg arrival, and the trailing
   2B runner's 2-leg arrival lands well after the throw - the throw visibly beats a runner
   the result says scored safely. The tag-throw branch already does this right (5422-5429:
   `Math.max` over every safe mover). Fix in Task 1. Non-HOME safe targets and the
   OUT-side sibling (`runnerForOutTarget`) cannot collide (distinct destination bases) -
   no change there.

0.4. **Open Q3 (does catcher `EYE` reach the client?): no.** `_defense_entry`
   (key_moments_build.py:410-425) emits exactly `[full_name, last_name, spd]`; `_player_view`
   (395-407) never surfaces `eye`. So Task 14.1 needs a small pipeline change, not just JS.
   The good news: `utils.read_mln_players_from_sheet` (utils.py:8312) already parses `EYE`
   (8353) and `AWR` (8359) in **both** the archive (Rosters) and current-season (Players)
   formats, so the data is sitting in `ref` - it's dropped one function before emission.

0.5. **Open Q4 (is pitcher `AWR` queryable?): yes, same source.** `pitcher_spd` is already
   emitted per-moment (key_moments_build.py:817); `pitcher_awr` is a one-line sibling once
   `_player_view` carries `awr`. Both Task 14 wiring tasks are therefore
   pipeline-small + JS-small, plus an archive-rebuild consideration (0.8).

0.6. **Fact 16's fence-shadow hypothesis is FALSE - the shadow is NOT built from
   untruncated samples.** `flightSampleSeries` (3645-3648) substitutes
   `fenceTruncatedSamples` for a cleared-fence HR, and `shadowPts` (3704) maps over that
   same `series.samples`, explicit 100% stop included (3732-3734). The real mechanism: the
   shadow shares the ball's full animation lifecycle (`movementRule(name + "-shadow")`,
   3790-3800), so after the ball fades at the wall the shadow **settles and persists**
   (dimmed `ballSettle`, no fade) at the fence+15ft *ground* point - and that ground point,
   projected at z=0 (`ftToSvg`), lands on screen inside the drawn wall band
   (`fenceWallPathD`, 2012, has real drawn height rising from the fence's ground line).
   A resting shadow painted on the wall's face is exactly "a shadow on the fence."
   Fix design in Task 12 - still small, but it is an opacity/lifecycle fix, not a
   sample-truncation fix.

0.7. **Task 8's migration is bigger than the CSV.** Parsed `import_BRC.csv` directly
   (608 rows): **997 non-empty `ThrowOrder*` values** across all 11 columns
   (`ThrowOrder` 240, `_P/_C/_1B/_2B/_SS/_3B` 65 each, `_OF` 43, `_LF/_CF/_RF` 108 each),
   every one in the old digit-means-base convention with comma separators (`"3,2,1"`,
   `"4"`, ...). The `a1-a3` columns use a separate `b1..b4` token format - no collision, no
   migration needed there. The pipeline is pass-through (utils.py:333 `_clean_csv_cell`
   only strips a `.0` float artifact; `_load_brc_table` 343+ stores raw strings), so
   parsing lives entirely in app.js's `parseThrowOrder`. **But: the committed s01-s12
   archives carry old-convention `throw_order`/`throw_order_by_position` strings baked into
   their `key_moments.json`.** Under the new alphabet, an archived `"3,1"` would silently
   re-parse as positions 1B, P - a plausible-looking, wrong chain, exactly the collision
   fact 8 warns about. The new parser therefore **cannot ship without rebuilding the
   archives from the migrated CSV in the same commit** (the `--archive-all` machinery was
   just exercised by commit `6969745`, so this is routine). The current-season deploy
   rebuilds in CI from HEAD automatically.

0.8. **The editor is `docs/tools/brc_editor.html` (no `.py` exists).** ThrowOrder columns:
   listed at 332/337-338, rendered as free-text inputs at 504-513, per-position candidate
   map at 671-673, user-facing hint at 217, fielder-dot notation rendering around 94/199.
   All touch points for Task 8's cutover.

0.9. **A model inconsistency relevant to Tasks 3/4, worth fixing while there:** the OF
   pursuit *race* (`ofPursuitDeficitMs`, 4163) now runs on the `"pursuit"` profile
   (10.2ft/s², 26.4ft/s, 0.4s reaction), but the OF token *render* still runs on the
   `"run"` profile (`movingFielderTokenHtml` → `fielderLegDurationsMs`, 4462 → 4421:
   27ft/s, 19.2ft/s², plus `OUTFIELDER_REACT_MS` added via `fielderStartDelay`, 4252). The
   picture and the race disagree about the same run. Task 3 fixes this by making
   pursuit-qualifying tokens render on the pursuit profile, so "the fielder's real arrival"
   is one number, not two.

0.10. **`stealThrowHtml` uses the flat `THROW_DRAW_MS` (964ms over 127.3ft), never a real
   distance** (5881, 5888). Coincidentally right for C→2B (exactly the diagonal), ~40%
   too slow-looking for C→3B (90ft). Task 14.1's EYE wiring is the natural moment to give
   steal throws real distance + per-position speed; spec'd there.

---

## 1. Task 1 - Verification findings and gap-closing

### 1a. Fold `infieldSingleThrowHtml` (delete per the round-1 plan's original intent)

Per 0.2, its one load-bearing behavior moves into the general heuristic:

1. `outThrowTargets` (4997): after the explicit-ThrowOrder branch, add a
   default for the infield-single hit - when `flight.archetype === "infield_single"`,
   nothing recorded an out that produces a target, and the heuristic would otherwise
   return `[]`, return `["1B"]`. (Keep it archetype-scoped exactly as the old function
   was - `bunt` hits stay throw-less, matching today.)
2. `throwSchedule` already handles the rest: `realOutThrowCount` is 0, so the leg is
   `out:false` → reconciled `contestedSafe` against `safeRunnerArrivalMs(..., "1B")`,
   which resolves the batter's own move (5367's fallback covers the edge). This is
   numerically the same reconciliation the parallel function ran (5707), same knob.
3. Delete `infieldSingleThrowHtml` (5697-5713), its call site in `sceneFieldHtml`
   (~6710), its `KMFlight` export (5939), and rewrite its two tests
   (ball_flight_test.py:1327-1345) into: "an infield single with no explicit ThrowOrder
   gets a contestedSafe 1B leg from the general schedule" + "an explicit ThrowOrder still
   wins" (the 1316-1325 hit-gap assertion pattern is the template).
4. Check `throwHtml`'s rendering of the new leg matches the old look (green/safe,
   no dashed line per `71f88ef`'s decorative-throw treatment - `t.out` is false, so it
   already routes the same way).

**Interop note (recommended, small):** `firstBaseCoverage` (4571) guards on
`archetype !== "grounder"`, so on an infield single fielded by a ranging 1B,
`coveringPosition` always answers "1B covers himself" and the leg becomes an unassisted
jog. That is acceptable v1 (flag to Alex: extending `firstBaseCoverage` to
`infield_single` would let the P-covers story appear on deep 1B infield singles too -
the race machinery already answers it; one-line archetype-set change if he wants it).

### 1b. Fix `runnerForSafeTarget`'s multi-scorer HOME case

Per 0.3: for a safe target with multiple movers reaching it (HOME only), the throw must
honestly lose to **every** runner the result says was safe - i.e. reconcile against the
**latest** arrival, mirroring the tag-branch precedent (5422-5429).

- `runnerForSafeTarget` (5360): instead of first-match, collect all matches and return
  the one with the most legs to cover (`endOrd - startOrd` largest; equivalently compute
  arrival for each and keep the max - see next line). Keep the BATTER fallback (5367).
- `safeRunnerArrivalMs` (5379): compute arrival per matching mover and return the max;
  keep returning `null` when none match. Keep `runnerForSafeTarget` and this function
  answering consistently (the returned runner is the one knob attributions apply to).
- Test: a synthetic moment with runners on 2B+3B both scoring, explicit ThrowOrder home,
  zero outs - assert the final leg lands ≥ `contestedSafe.minMs` after the **trailing**
  (2B) runner's arrival, not the 3B runner's.

### 1c. Fact-25 regression test (already-correct behavior, pinned)

Add to `ball_flight_test.py` (next to the 1316-1325 block): a CF single
(`archetype: "single"`, no recorded outs, `throw_order: "2"` - note: this literal
becomes `"s"` when Task 8's migration lands; the Stage F commit updates it) - assert
`throwSchedule`'s leg has `out === false` and reconciles `contestedSafe` (throw lands
after the advancing runner's arrival by ≥ minMs), never `forceOut`.

---

## 2. Task 2 - Throw-receiver coverage (all bases) + 2B coverage by thrower

### 2a. Generalize receiver-token movement to non-out plays (all bases at once - fact 23)

`fielderTokensHtml` (4591): today the chain-mover branch (4622-4679) runs only for
`isNewOut && isGroundOrAir`, and slices `relayBases` to `realOutThrowCount` (4632) - so
decorative/contestedSafe legs' receivers never move, and the non-out solo branch
(4680-4698) moves only the ball-toucher. Rework:

1. Compute `var targets = outThrowTargets(m, moves, flight);` once at the top of the
   batted-ball section.
2. Chain branch condition becomes `flight.fielder && (targets.length || (isNewOut && isGroundOrAir))`,
   and `relayBases` uses the **full** `targets` list (drop the `realOutThrowCount` slice) -
   receivers of decorative legs (a `b0534c3` hit throw, a sac-fly tag throw, the folded
   infield-single 1B leg) now get their covering fielder moved to the bag. The merged-chain /
   unassisted / pitcher-cover-legs logic (4637-4675) is already base-agnostic and needs no
   per-base cases.
3. Carry the solo branch's `ofReadDelayMs` application (4694) into the chain path: the
   ball-touching entry's own `startDelay` becomes `startDelay + ofReadDelayMs(m, flight, anchor)`
   (returns 0 for every non-qualifying play, so out-plays are untouched).
4. The solo branch survives only as the fallback for a hit with genuinely no targets
   (plain single/double/triple with no explicit ThrowOrder) - unchanged behavior there.
5. `deadlineMs` handling per Task 5 below (same branch, same edit - land them together).

Risks to check: the chain path uses `fielderStartAnchorFt` + merged-chain movement for
the ball-toucher where the solo path applied the OF read-delay - verify a double with an
explicit ThrowOrder (now chain-routed) still shows the same pursuit motion as one without
(solo-routed). That equivalence is the acceptance test for this refactor.

### 2b. `coveringPosition` 2B branch keys off the thrower (fact 22)

Line 5112: `if (base === "2B") return angle < 45 ? "2B" : "SS";` becomes:

```js
if (base === "2B") {
  if (OUTFIELD_POSITIONS[fielderPos]) return fielderPos === "LF" ? "2B" : "SS";
  return angle < 45 ? "2B" : "SS";   // non-OF throws keep the spray-side rule
}
```

3B (5106) confirmed correct, untouched. Update the convention comment (5090-5096).
Note for review: in the common case the old and new rules agree (LF-side balls have
angle<45 → 2B; RF/CF-side ≥45 → SS) - the visible delta is only plays where the
fielding OF's identity crosses the spray-side boundary (e.g. CF ranging left of 45°),
so expect a small, targeted visual change, not a broad one. Update any test asserting
the angle rule for OF throwers (grep `coveringPosition` in ball_flight_test.py).

---

## 3. Task 3 - Resync outfield-hit throw scheduling to the fielder's real arrival (facts 14/26)

The gap: `throwSchedule`'s grounder/hit branch anchors at
`var base = fieldedMs(flight) + THROW_DELAY_MS` (5450) - pure ball physics. On
`resolveHitPickup`-resolved plays (doubles/triples) and OF singles, the rendered fielder
token can arrive well after the ball rests (now *more* often, post-fact-26), and the
throw draws from a spot the fielder hasn't reached. The pause between ball-at-rest and
fielder arrival is **intended** (fact 11) - only the throw must wait.

Design:

1. **Unify the profile first (0.9).** Give `movingFielderTokenHtml` (4457) an optional
   `profileKind` argument (default `"run"`), threaded to `legDurationsMs(legs,
   fielderProfile(m, pos, kind))`; `fielderTokensHtml` passes `"pursuit"` for the
   ball-touching leg when `ofPursuitApplies(m, flight)`. Note the pursuit profile carries
   its own 0.4s reaction inside `arrivalTimeS`, while the token's start delay adds
   `OUTFIELDER_REACT_MS` via `fielderStartDelay` (4252) - **don't double-count**: when
   rendering with the pursuit profile, pass the base `startDelay` (not
   `fielderStartDelay`) or zero the profile's reaction for rendering; pick one and make
   the arrival function below use the identical convention. This is the one fiddly spot
   in this task - single-source it.
2. **One shared arrival function.** New `fielderBallArrivalMs(m, flight)` (exported to
   KMFlight): the ball-touching fielder's rendered arrival at `fieldedPoint`, in raw
   (pre-seqDelay) units - anchor from `fielderStartAnchorFt`, `ofReadDelayMs` included,
   profile per point 1. Both `fielderTokensHtml`'s leg construction and `throwSchedule`
   must read this one function (or the token must be built *from* it) so the throw can
   never disagree with the picture.
3. **Floor the schedule's anchor.** In `throwSchedule` (5450):
   `var base = Math.max(fieldedMs(flight), fielderBallArrivalMs(m, flight)) + THROW_DELAY_MS;`
   scoped to `OUTFIELD_POSITIONS[flight.fielder] && !CAUGHT_IN_AIR[flight.archetype]`
   (infield charge races already tie `fieldedMs` to the fielder's own race arrival, and
   Task 5's compression handles their token side; air catches key off `ballTravelMs`).
   Applying it at `base` (rather than a post-hoc `holdChainTo` floor) means the
   reconciler sees the honest schedule and its existing knob order does the rest - no
   fight with `holdRelease` (a later base simply means less hold is needed on
   contestedSafe legs, and forceOut legs get the runner-side knobs as designed).
   `ballFlightHtml`'s handoff fade (3867) follows automatically.
4. Tests: for a deep double with an explicit ThrowOrder, assert
   `schedule[0].startMs >= fielderBallArrivalMs(m, flight)`; and the pause invariant -
   the floor only ever *raises* `base`, never trims the intended ball-at-rest wait.

---

## 4. Task 4 - Dive-and-miss motion for XBH near a fielder (re-scope first - facts 10/11/26)

**Gate this behind a visual re-test.** Fact 26's pursuit fix plus Task 3's throw resync
address the measurable share of "OF looks too fast on XBH." Before building anything:
replay the named XBH regression plays (round-1 plan §7 - the Calvin Huff dead-center
double among them) with Stage 0-C landed, and have Alex judge which plays still read
wrong. Only then implement, scoped to plays that still fail the eye test.

Where still warranted, the design (deliberately reusing existing machinery, no new
animation system):

1. **Qualification:** `ofDivePlausible(m, flight)` - the assigned OF's distance from
   `fielderStartAnchorFt` to the ball's *first landing* point (`flight.x/y`) is within
   what the pursuit profile covers in the ball's real hang time plus a small pad
   (`accelDistForTimeS(hangS, ...)` + `DIVE_RANGE_PAD_FT ≈ 8`). Balls landing far from
   anyone, and routine plays, never qualify.
2. **Route:** replace the single leg to `fieldedPoint` with two legs via the existing
   multi-leg `movingFielderTokenHtml` path (the same mechanism the unassisted
   field-then-run case uses, 4664-4667): leg 1 anchor→landing point, leg 2
   landing→`fieldedPoint` (the honest rest-point chase, fact 11).
3. **Timing:** leg 1 must arrive at or just after the ball lands - never before
   (that would contradict the hit): if the natural pursuit-profile duration lands leg 1
   early, stretch **leg 1 only** to `ballTravelMs + DIVE_MISS_MS` (~150ms - a named
   constant, the "glove just misses" beat). Leg 2 runs at natural pursuit pace.
   Total arrival feeds `fielderBallArrivalMs` (Task 3) so the throw stays synced.
4. **Read-delay interplay:** `ofReadDelayMs` still applies to the start; the dive route
   *replaces* the need for extra hesitation on qualifying plays only when the honest
   two-leg time already loses the race - compute the deficit against the two-leg total.
5. No token-sprite "dive" animation this round - route shape and timing only. Flag to
   Alex that a visual dive pose is a separate, later ask.

---

## 5. Task 5 - Extend glove deadline compression to grounder fielding (facts 18/24)

Precisely located; no new mechanism. `movingFielderTokenHtml`'s compression (4464-4473,
only shortens) is invoked once, at 4677: `isAir && e.base === null`. Two wiring changes,
landed together with Task 2a since they edit the same branch:

1. **Chain-mover path** (4677): the ball-touching entry's deadline becomes
   `e.base === null ? startDelay + (isAir ? ballTravelMs(flight) : fieldedMs(flight)) : null`
   - ground archetypes now compress the fielding run to the moment the game logic says
   the ball was fielded (the charge race's own answer), exactly as fly outs already do.
   (For the merged unassisted case - `e.base !== null` on the toucher's own entry - the
   deadline applies conceptually to leg 1 only; `movingFielderTokenHtml` compresses the
   whole run proportionally, which slightly over-compresses leg 2. Acceptable v1;
   note it in the code comment rather than building per-leg deadlines.)
2. **Solo path** (4695): pass a deadline **only for ground archetypes**
   (`GROUND_ARCHETYPES[archetype] ? startDelay + fieldedMs(flight) : null`) - i.e. the
   bunt/infield-single *hit* charge races. Do **not** deadline OF pursuit hits - their
   late arrival is intended (fact 11); Task 3 owns their throw side. This is the
   over-correction trap the prompt warns about - keep the two scopes disjoint.
3. Test: a grounder out whose `fielderLegDurationsMs` naturally exceeds `fieldedMs`
   renders with compressed durations summing to ≤ `fieldedMs` (assert via the generated
   keyframe totals or by exporting the leg-duration computation).

---

## 6. Task 6 - Pitcher charge-in reaction delay (round-1 Stage 5c, fact 12)

**Recommendation: flat pitcher-specific reaction override** (simplest honest model -
"finishing the delivery costs a beat"), not the continuous reaction-window model; keep
`PITCHER_MIDDLE_EV_MAX_MPH` (2926) unchanged (it answers a different question - a ball
too hot to glove at all - and its removal-from-candidates behavior at 2939-2946 stays).

1. New `PITCHER_CHARGE_REACTION_S = 0.45` **provisional** (Alex's "a split second more"
   on top of the shared 0.15 - a few hundred ms, not a full second). Validate before
   finalizing (point 4).
2. Thread per-position reaction through the race: `chargeFielderArriveS` (2775) gains a
   `reactionS` param (defaulting `CHARGE_REACTION_S`); `fielderInterceptS` (2827) passes
   it through; `chargeInIntercept`'s candidate loop (2897) passes
   `pos === "P" ? PITCHER_CHARGE_REACTION_S : CHARGE_REACTION_S`. The camped-candidate
   `distFt===0` exception (reaction only applies when moving - 2764-2774) is untouched:
   a comebacker right at the mound still gets fielded on the spot.
3. Also update `fielderProfile`'s `"charge"` kind reaction for P (4354) so any profile
   consumer agrees with the race.
4. **Validation probe (Sonnet writes, runs once, deletes or parks in tools/):** sweep the
   recorded-play corpus (or the synthetic grid ball_flight_test.py's sweeps use) over the
   mound-3B contested zone (angles ~50-77, grounder archetypes), and report P-vs-3B
   winner shares at reaction 0.15 / 0.30 / 0.45 / 0.60. Pick the smallest value that
   moves the contested zone's plurality to 3B without stripping P of genuine
   comebacker-adjacent plays; present the table to Alex with the recommendation.
5. Test: pin one representative play per side (a 3B-should-win charge at ~0.45; a true
   comebacker P still wins).

---

## 7. Task 7 - Live-ball ricochet off a foul-territory wall (fact 10) - greenfield, blocked on Alex data

**Not a foul ball** - a live, fair ball contacting a wall standing in foul ground; play
continues. Genuinely new geometry. Two hard facts constrain the design:

- **No side-wall geometry exists.** `fenceWallPathD` (2012) draws the fair-arc wall only
  (foul line to foul line); the foul-ground boundary (`boundaryRFt`, ~1823) is a visual
  grass shape with no collision or wall semantics.
- **Today's engine never sends a live ball into foul ground.** `clampFairTerritory`
  (2431) rotates every non-caught batted ball's whole sample series so the landing
  bearing sits within ±45°, and rollout continues radially along the contact bearing -
  so a ball can at most *graze* the foul line, never cross into foul territory where a
  side wall stands.

Design (staged, with an explicit data dependency):

1. **Data from Alex (blocking - do not invent):** the wall segments' real field-plane
   geometry - for each of the two side walls: start/end points in feet (or "a wall
   parallel to the foul line, X ft foul of it, from Y ft to Z ft from home"), plus drawn
   height. Also: which plays he intends to reach it (see point 2 - this decides how much
   of the clamp changes).
2. **Reaching the wall.** Two candidate scopes - present both, Alex picks:
   - *(a) Line-hugging only:* keep `clampFairTerritory` as-is; a ball whose (clamped)
     bearing is within ~1-2° of a foul line and whose roll carries past the wall's start
     distance contacts the wall's field-side face. Smallest change; the wall face must
     then sit effectively on the line.
   - *(b) Real foul-ground rollout:* let the *rollout* (not the landing) carry across
     the foul line down the lines (roll direction already can differ from the bearing
     only if we add it - today it can't), which is a bigger physics change to the ground
     path than this task should absorb. **Recommend (a)** unless Alex specifically wants
     hooking rolls; scope (b) out with a note.
3. **Bounce physics** (plausible-not-exact, matching the project bar): on contact,
   reflect the ground-velocity vector about the wall's normal with a restitution factor
   (`WALL_RESTITUTION ≈ 0.6`, tunable) and continue the existing friction-decay roll
   from the contact point along the reflected direction. Implementation shape: extend
   the ground-phase sampling (`groundPhaseSamples`, 3587) with a segmented path - the
   pre-contact segment along the original direction, post-contact along the reflected
   one - which means `groundDirPoint`/`fieldedPoint`'s single-direction assumption
   (3533-3536) gains a segment-aware variant for these plays. This is the task's real
   engineering: a `groundPathSegments(flight)` abstraction that degenerates to today's
   single segment for every non-ricochet play (zero behavior change elsewhere -
   regression-pin this).
4. **Fielder pursuit:** unchanged machinery - the pursuit/charge races target the final
   rest point via `fieldedPoint`; once point 3 makes that segment-aware, the races and
   Task 3's arrival floor apply as-is.
5. **Result-code scope:** rendered story only - which result codes can ricochet
   (doubles/triples down the line, wall-adjacent singles?) should be data-driven
   (a per-situation flag or derived from bearing+distance), decided with Alex alongside
   the geometry. The verdict is never touched.
6. Draw the walls (`fenceWallPathD`-style band) so the bounce has a visible cause.

Keep this as its own late stage; nothing else in the plan depends on it.

---

## 8. Task 8 - Cutoff throws: new `ThrowOrder` alphabet + mechanic (facts 3/8/9, probe 0.7)

**Scope boundary, restated so nobody over-builds:** this is a *pre-declared* CSV chain
(fielder→cutoff→base known before the play renders), not dynamic mid-flight redirection -
round 1 explicitly scoped true cutoff-man logic out, and that stays true.

### 8.1 Grammar

New alphabet, one character per leg: `h/f/s/t` → HOME/1B/2B/3B (base legs); `1`-`9` →
P,C,1B,2B,3B,SS,LF,CF,RF (position legs - standard scorekeeping numbers; build the
reverse map from the existing `POSITION_NUMBER`). Rewrite `parseThrowOrder` (4964):

- Returns an ordered leg list of typed entries - `{kind:"base", base:"HOME"}` /
  `{kind:"pos", pos:"SS"}` - not a bare base array.
- Accept `,`, space, `-` as separators (stripped); **any other character makes the whole
  value invalid: `console.warn` with the raw value and return `null`** (falls back to
  the heuristic). No silent stripping - under this alphabet a stray digit is a plausible
  wrong leg, not noise. Case-insensitive on the base letters.
- Warn (but accept) when a chain doesn't end in a base leg - a position-final chain has
  no runner to reconcile and renders as decorative legs only.
- `THROW_ORDER_DIGIT_TO_BASE` (4963) is deleted with the old parser.

### 8.2 Typed legs through the machinery (extend `b0534c3`, don't parallel it)

- `outThrowTargets` (4997) returns the typed leg list when explicit; the heuristic path
  wraps its base strings as `{kind:"base"}` entries. Add a `baseLegs(legs)` helper for
  the many consumers that only care about bases: out-count capping (5047-5052),
  `runnerForOutTarget`/`runnerForSafeTarget` lookups, `outThrowEndByBase` (5550),
  `sceneFieldHtml`'s `realOutTargets` corroboration (6261), `fieldingChainDetail`'s
  relay bases. Sonnet: grep every `outThrowTargets(` call site and convert each - this
  is the mechanical bulk of the task.
- `sequentialThrowSchedule` (5284): schedule entries carry the typed target (plus
  `distFt`/`throwerPos` - Task 9.4 needs those anyway). `out` flags map onto **base**
  legs only, in order; position legs are never outs.
- `throwSchedule` (5402): per-leg geometry - a position leg's endpoint is its cutoff
  coordinate (8.3); thrower of leg i is leg i-1's receiver (`coveringPosition` for base
  legs, the position itself for cutoff legs). Final-leg reconciliation unchanged: the
  last **base** leg reconciles against the real runner (forceOut/contestedSafe exactly
  as today); `holdChainTo` shifting the whole chain already handles multi-leg.
- `fielderTokensHtml` (Task 2a's unified chain): a position leg adds that fielder as a
  mover to the cutoff spot (deadline: the leg's arrival there, using the existing
  compression). `coveringPosition` is bypassed for position legs - the CSV named the
  fielder.
- `fieldingChainDetail`/`fieldingNotation` (5152/5188): position legs join the chain as
  touches (a 9-6-2 relay reads "9-6-2" when it records an out) - the adjacent-duplicate
  collapse already handles repeats.
- `relayLegIsUnassisted` (3161): compare against the leg's own resolved position for
  both kinds.

### 8.3 Cutoff coordinates - two options, Alex decides (do not invent placeholders)

- *(a) Hand-authored table:* `CUTOFF_ANCHORS_FT[pos]` (or keyed `[pos][finalBase]`) in
  app.js, values Alex supplies in field feet. Most explicit, more data to maintain.
- *(b) Derived spot:* the point on the throw line (origin → final base leg's bag) at the
  cutoff fielder's canonical depth (project `FIELDER_ANCHORS_FT[pos]` onto the line, or
  intersect the line with the fielder's depth arc). Zero new data, self-consistent with
  the one-scale geometry principle, and it's where a real cutoff man actually stands -
  **recommended**, but it is a modeling choice Alex hasn't blessed: ask, with a sketch
  of both, before implementing.

### 8.4 Migration (one-time, auditable - probe 0.7's inventory)

1. `tools/migrate_throw_order.py`: map `1→f, 2→s, 3→t, 4→h` in all 11 `ThrowOrder*`
   columns of `import_BRC.csv`, preserving comma separators; assert exactly the probed
   value counts convert (240/65×6/43/108×3 non-empty cells, 997 total) and no cell
   contains any other character; write in place. Run once, commit the CSV + script.
2. **Same commit:** the new `parseThrowOrder`, and the s01-s12 archive rebuild
   (`--archive-all`) so no archived JSON carries old-convention strings (0.7's
   collision). Also update `import_BRC.csv`-sourced test literals (the fact-25 test's
   `"2"` → `"s"`, the 1316-1325 block's `throw_order` fixtures, any others grep finds).
3. `docs/tools/brc_editor.html`: update the hint text (217), the fielder-dot notation
   display (94/199 region), and add input validation (`pattern` or on-save check:
   `^[hfstHFST1-9,\s-]*$`) so old-convention values can't quietly re-enter. The editor
   stores raw strings, so no other write-path change is needed - verify by saving a row
   round-trip.
4. `utils.py` needs no change (pass-through confirmed, 0.7) - but note `_clean_csv_cell`'s
   `.0`-stripping stays load-bearing for any *all-digit* new-scheme value (e.g. a
   pure-cutoff chain `"64"`), which pandas can still float-ify.

---

## 9. Task 9 - Perceived closeness on hit throws (facts 4/19/20) - strictly ordered sub-parts

### 9.1 CSS easing → linear (land first, alone, then re-assess)

`docs/css/style.css`: `.throw-clip-rect` (1009-1011) and `.throw-ball` (1021-1023) both
move from `ease` to `linear` **together** - the comment at 1017-1020 confirms they were
deliberately matched, so changing one alone would desync the ball from the line's
leading edge (the exact coupling the prompt flags). Update both rules' comments to cite
the `.fielder` precedent (960-967, same complaint, same fix). No JS change; `--draw`
durations are already the honest `throwDrawMsForFt` numbers. **Re-watch a handful of hit
throws with Alex before touching 9.2.**

### 9.2 Re-assess `MARGIN_POLICY.contestedSafe` (only after 9.1 soaks)

If hit throws still read too close at constant visual speed: raise `contestedSafe`
(3207-3212) only - **provisional recommendation `{minMs: 300, maxMs: 600}`** (from
today's 150/450), the same legibility-widening category as `TAG_THROW_MARGIN_MS`'s
200→400 history. Method: sample real hit plays across diff bands (near-tie ~0, mid ~250,
decisive ~500), record the on-screen gap each produces, and pick the smallest minMs
where a near-tie play still clearly reads safe. `forceOut`/`tagOut`/`uncontested`
untouched. Update the two margin tests (ball_flight_test.py:1485-1494 values, 1316-1325
threshold reads them via KMFlight so they self-adjust).

### 9.3 Per-position throw-speed table (position-only - no per-player arm data exists, fact 4)

Alex's supplied numbers, verbatim:

```js
// avg mph, and the realistic floor/ceiling for the reconciler's speed knob (9.4)
var THROW_SPEED_BY_POS = {
  P:  { mph: 85, min: 80, max: 90 },  C:  { mph: 80, min: 75, max: 90 },
  "1B": { mph: 80, min: 70, max: 85 }, "2B": { mph: 80, min: 70, max: 85 },
  "3B": { mph: 85, min: 80, max: 90 }, SS: { mph: 85, min: 80, max: 90 },
  LF: { mph: 87, min: 75, max: 95 },  CF: { mph: 90, min: 75, max: 95 },
  RF: { mph: 90, min: 75, max: 95 },
};
```

- `throwDrawMsForFt(distFt, mph)` (3123) gains the mph param (default
  `THROW_SPEED_MPH`, which survives as the unknown-thrower fallback and the
  `THROW_DRAW_MS` flat-fallback basis).
- `throwProfile(throwClass, pos)` (4399) reads the table.
- `throwSchedule`'s `drawMsFor` closures (5436-5440, 5472-5479) resolve each leg's
  thrower: leg 0 = `flight.fielder`; leg i = leg i-1's receiver (`coveringPosition`,
  already computed for the unassisted check - reuse, don't recompute).
- `stealThrowHtml`: deferred to Task 14.1 (which rides on this table's `C` row).
- Tests: RF's leg over a fixed distance is faster than C's; unknown fielder falls back
  to 90.

### 9.4 Throw-speed as the primary "land later" reconciler knob (`slowThrow`)

Fact 20: today the only too-early lever is `holdRelease`. Alex's ask: solve for the
single constant mph that lands the throw exactly on the required time, floor-bounded;
hold the release only for the remainder.

- Schedule entries must carry `distFt` and `throwerPos` (added in 9.3/8.2).
- `reconcileThrowSchedule` (3283), `delta > 0` branch, **before** `holdChainTo`:
  for the final leg, `neededDrawMs = required - last.startMs`;
  `neededMph = distFt / (neededDrawMs/1000) / 1.46667`. If
  `neededMph >= THROW_SPEED_BY_POS[throwerPos].min`, set the leg's `drawMs`/`endMs` to
  that speed exactly and record `{knob:"slowThrow", who:last.base, mphFrom, mphTo, ms}`.
  If the floor binds, set the floor speed, recompute, and close the remainder with the
  existing `holdRelease`. (Earlier relay legs keep their table speeds - only the leg
  racing the runner varies.)
- **Hard requirement honored by construction:** the speed is chosen once at scheduling
  time and constant for the leg's whole flight - with 9.1's linear timing the rendered
  ball actually moves at it.
- Applies to every `delta > 0` class (contestedSafe, uncontested, and the rare
  too-early forceOut) - "taking something off the throw" is the physically honest story
  in all three; justify in the code comment against goal (b).
- The pitcher-cover-1B floor (5521-5526) stays `holdChainTo` (it's a token-arrival
  constraint, not a margin) - but note order: it runs after margin reconciliation and
  can re-raise `endMs`; that interplay is unchanged.
- Tests: near-tie contestedSafe picks a slower in-range mph with zero hold; a case whose
  needed mph falls below the floor uses floor speed + holdRelease for the rest; recorded
  knobs match; mph never varies within a leg (single drawMs).

---

## 10. Task 10 - Runners must not pass each other (facts 15/5) - active bug

Land **after** Task 11 (the flatter spread changes how often this fires and how it tunes).

Design - one pure pre-pass, single-sourced into both the tokens and the reconciler's
runner arrivals:

1. **Formulation.** Parametrize each mover's position as base-ordinals (0..4) over time:
   piecewise-linear from its start delay (`mvDelay` shape: `RUNNER_LEAD_MS` /
   `outDelay` / `catchMs+TAG_UP_MS` - factor the existing `sceneFieldHtml` delay
   decision (6330-6346 and the arrival lookups' mirrors at 5339-5343, 5386) into one
   shared `runnerMoveTiming(m, flight, mv)` so there is exactly one answer) through its
   legs at `runnerLegMs` pace. Constraint, for every ordered pair (lead, trail) moving
   the same direction on the shared path while both are active:
   `lead.ordinal(t) - trail.ordinal(t) >= RUNNER_MIN_GAP_ORD` (propose `0.1` ≈ 9ft).
   Checking at both runners' breakpoint times is sufficient (piecewise-linear).
2. **Resolution, bounded and named** (MARGIN_POLICY-knob spirit), applied to the
   **trailing** runner only, in order: `trailLateBreak` - delay their start (≤ 400ms,
   reuse `RUNNER_LATE_JUMP_MAX_MS`); then `trailSlowPace` - stretch their leg durations
   (≤ `STRETCH_RUNNER_MAX_FRAC` 15%); if still violated, `console.warn` and render
   anyway (verdicts are never at risk - only positions).
3. **Single-sourcing (the correctness-critical part):** the adjusted timings must feed
   `safeRunnerArrivalMs`/`forcedOutRunnerArrivalMs` (5332/5379) as well as the token
   `--dur`/delay writes, **before** `throwSchedule` reconciles - otherwise a
   contestedSafe throw reconciled against a pre-adjustment arrival can beat the
   now-slower trailing runner (the 1b fix makes the trailing scorer exactly the one that
   matters). Concretely: new pure `runnerPassingAdjustments(m, flight, moves)` →
   `{[who]: {delayMs, paceScale}}`, consumed by both sides; `throwSchedule` calls it
   (cheap, pure, memoizable per play like `playFieldingNotation`'s pattern).
4. **Residual, accepted + noted:** the reconciler's own `runnerLateJump` (≤400ms) on a
   *lead* runner could in principle re-open a pass; the sweep test below asserts the
   final combined timings, so if a real play shape ever trips it, it surfaces loudly
   rather than silently.
5. **Scope:** same-direction movers only; retreat (`useRetreat`) and stranded-safe
   walk-offs excluded.
6. Tests: slow lead (spd 1) from 2B + fast trail (spd 5) from 1B on a single - trail
   never closes below the gap at any breakpoint; adjustment recorded; a
   different-direction pair gets none; sweep the result-code harness asserting no pair
   ever crosses.

---

## 11. Task 11 - Additive runner-speed model (+1 ft/s per SPD point) - concrete spec

`runnerProfile` (4373), one line:

```js
topSpeedFtPerS: RUNNER_SPRINT_FT_PER_S + (runnerSpd(m, who) - SPD_AVERAGE) * RUNNER_SPD_FT_PER_S_PER_POINT,
```

with `var RUNNER_SPD_FT_PER_S_PER_POINT = 1.0` next to `RUNNER_SPRINT_FT_PER_S` (1640).
Table produced: spd 1..5 → 25/26/27/28/29 ft/s (spread 63.2% → 16%). Null-safety is
already inside `runnerSpd` (2722). `accelFtPerS2` stays `RUNNER_ACCEL_FT_S2` (0.1 -
untouched by this task; the spec is top-speed only).

- **Fielders keep `spdPaceScale`** - do not touch `fielderProfile` (4354) or
  `idleDriftLeg`'s pace line (4113).
- Consumer audit (grep confirmed): `runnerProfile` is the only
  `spdPaceScale(runnerSpd(...))` call site; everything runner-timed flows through it via
  `runnerLegMs` (4390) / `stealLegMs` (5773). `RUN_LEG_MS` (1641) and `STEAL_LEG_DUR_MS`
  (5772) remain the flat league-average *fallback constants* only - unchanged, still
  correct (spd-3 reproduces 27 ft/s exactly under the new formula).
- Update the spd-scaling tests (ball_flight_test.py:1404-1434): the spd-5-faster-than-
  spd-1 assertions hold; add explicit table checks (arrivalTimeS-derived leg times for
  spd 1/3/5); the spd-3 ≡ league-average check must still pass to the millisecond.
- Flag at review: spd-1 runners get visibly faster (20.5→25 ft/s), spd-5 visibly slower
  (33.5→29) - every reconciled race re-times; the reconciler absorbs it, but the named
  regression plays (round-1 §7) deserve one visual pass.

---

## 12. Task 12 - Fence shadow (probe 0.6 - hypothesis overturned, different small fix)

The shadow's samples are already truncated with the ball's; the defect is the shadow
**persisting** at the fence+15ft ground point, whose z=0 projection paints inside the
drawn wall band. Fix: on cleared-fence HRs only, fade the shadow out at the truncation
point while the ball keeps its existing settle (Alex's earlier call that the ball/arc
stays is untouched - only the ground shadow, which is physically hidden behind the wall
once the ball crosses it, goes).

Implementation (contained in `ballArcHtml`, 3697): when `flight.clearedFence`, append
opacity to the shadow's keyframes - `opacity:1` through the last real sample stop,
`opacity:0` at the explicit 100% stop (3732-3734) - so the movement animation's own
`both` fill holds it invisible after. Check the interaction with `ballSettle` in
`movementRule` (3790-3796): ballSettle animates the element too - if its fill wins the
opacity conflict, instead add a `.ball-shadow.clear` override rule (style.css) skipping
ballSettle / adding a dedicated fade timed to the truncation offset. Sonnet: implement
the keyframe-opacity version first, verify on a barely-clears HR (the 378ft/375ft case
from the 3543 comment) and a deep no-doubter, fall back to the CSS-rule version only if
the fill-order fights. Also confirm the `--sscale`/`--stx/--sty` base-rule fallback
(3872, no-animation path) doesn't leave a static shadow on the wall for
reduced-motion - if it does, gate the shadow element's render on `!clearedFence`... no:
the shadow is wanted *during* flight; set the fallback vars' opacity via the same
`.ball-shadow.clear` class instead. Scoped small; do not redesign the shadow system.

---

## 13. Task 13 - Widen idle-fielder drift (explicitly low priority - tuning constants only)

`IDLE_DRIFT_MAX_FT 10 → 12`, `IDLE_DRIFT_DECAY_FT 40 → 110` (4093-4094). Resulting
caps: ~8.3ft at 40ft, ~3.8ft at the infield diagonal (127ft), ~1.2ft at 250ft, ~0.65ft
at 320ft - so an infielder acknowledges an outfield play and the far outfield still
barely moves, with `IDLE_DRIFT_MIN_FT=0.5` (4095) now cutting off around ~350ft instead
of ~110ft. Mechanism, exclusions (C/P), accel-time cap - all untouched. One visual pass
against the original "too uniform/sudden" complaint (comment 4079-4092): the proximity
decay that fixed it is preserved, just slower - if it re-reads as uniform, lower
`DECAY_FT` toward 80 before touching anything else. No tests beyond updating any pinned
constant values.

---

## 14. Task 14 - Catcher `EYE` and pitcher `AWR` into steal mechanics (facts 6/7, probes 0.4/0.5)

Both need small `key_moments_build.py` changes (probes overturned the "EYE already
reaches the client" hope). Pipeline first, JS second; one archive-rebuild decision.

### 14.0 Pipeline

1. `_player_view` (395-407): add `"eye": p.get("eye")` and `"awr": p.get("awr")`.
2. `_defense_entry` (410-425): append eye - entries become `[full, last, spd, eye]`.
   Index-3 append is backward-compatible with every existing `[2]` read (client and
   tests); `runners_on_base` entries get it too, harmlessly.
3. `build_moment`: emit `"pitcher_awr": feat["pitcher"]["awr"]` next to `pitcher_spd`
   (817).
4. **Archive rebuild (ask Alex):** without an s01-s12 rebuild, historical steals fall
   back to average EYE/AWR (the established null-safe convention, same as spd once did).
   Rebuild is routine (`6969745` precedent) - recommend doing it, but it can trail the
   JS landing.

### 14.1 Catcher `EYE` → steal-throw speed (after 9.3 - reads the `C` row)

- New `catcherEye(m)`: `m.defense && m.defense.C && m.defense.C[3]`, null → `SPD_AVERAGE`-
  style fallback (3). New `eyeArmMph(eye)` mapping 1..5 into the C row asymmetrically
  through its real average: below-average `80 - (3-eye)*2.5` (eye 1 → 75), above-average
  `80 + (eye-3)*5` (eye 5 → 90) - piecewise because Alex's range (75-90) isn't centered
  on his average (80); flag the shape for his sign-off.
- `stealThrowHtml` (5844): replace the flat `THROW_DRAW_MS` (0.10) with a real draw -
  `drawMs = throwDrawMsForFt(throwDistFt(originAnchor, target.base), eyeArmMph(catcherEye(m)))`
  - in **both** places it appears: the pitch-arrival floor (5881) and
  `start = arrive - drawMs` (5888). `throwLineHtml`'s draw-duration param must now be
  passed (it currently defaults). The backward-solved `arrive` and the
  `MARGIN_POLICY.tagOut` cap (5887) are untouched - EYE changes how fast the ball
  visibly travels (and therefore release time), never the verdict or the margin.
- Steal-of-home pitcher carve-out (5852) unchanged.
- Tests: eye-5 catcher's draw < eye-1's over the same distance; C→3B now draws faster
  than C→2B (real 90 vs 127ft); missing eye reproduces the 80mph-average timing.

### 14.2 Pitcher `AWR` → leadoff distance

- New `stealLeadoffFt(m)`: `15 - awr` with null-awr → 3, i.e. 10/11/12/13/14ft for awr
  5..1. **Sanity-check confirmed in design: awr=3 (and every unresolved historical row)
  yields exactly today's 12ft** - the formula is calibrated around the current constant,
  strong reason to take `15 - awr` as-is. No clamp needed (awr is 1-5 by schema); cite
  the `(pitcher AWR + catcher EYE)/2` precedent (batter_optimizer.py:414) in the comment.
- Replace `STEAL_LEADOFF_FT` reads: `stealLegMs` (5773-5774) and `sceneFieldHtml`'s
  `leadoffFrac` (6324) take `stealLeadoffFt(m)`; `STEAL_LEG_DUR_MS` (5772) keeps the
  constant as its league-average fallback basis; keep `STEAL_LEADOFF_FT = 12` as that
  fallback constant + KMFlight export, or export the function - Sonnet's call, just keep
  the test surface coherent.
- Tests: awr-5 pitcher → shorter lead → longer steal leg (later arrival) than awr-1;
  null-awr ≡ today's timing exactly.

---

## 15. Fix map (new/changed constants and functions)

| Item | Where | Disposition |
|---|---|---|
| fact-26 diff (`RUNNER_ACCEL_FT_S2` 19.2, `OF_PURSUIT_*` 10.2/26.4, `ofPursuitDeficitMs` on pursuit profile) | app.js ~4163-4380 | commit as-is (Stage 0); round-1 plan doc table updated same commit |
| `infieldSingleThrowHtml` (5697) + export + call | app.js | **delete** - folded into `outThrowTargets` infield-single default + general contestedSafe path (Task 1a) |
| `runnerForSafeTarget`/`safeRunnerArrivalMs` (5360/5379) | app.js | fix: latest-arriving safe mover for shared HOME target (Task 1b) |
| `fielderTokensHtml` chain/solo branches (4622-4698) | app.js | unified: full-target chain, receivers move on non-out plays, readDelay carried, ground deadlines (Tasks 2a/5) |
| `coveringPosition` 2B branch (5112) | app.js | thrower-keyed for OF throws; angle rule kept for non-OF (Task 2b) |
| `movingFielderTokenHtml` (4457) | app.js | + optional `profileKind` (Task 3) |
| new `fielderBallArrivalMs(m, flight)` | app.js | single source: rendered ball-toucher arrival; floors `throwSchedule` base for OF hits (Task 3) |
| new `DIVE_MISS_MS` ≈150 / `DIVE_RANGE_PAD_FT` ≈8 / `ofDivePlausible` | app.js | Task 4, contingent on visual re-test |
| new `PITCHER_CHARGE_REACTION_S` ≈0.45 (probe-validated) | app.js | per-position reaction threaded through `chargeFielderArriveS`/`fielderInterceptS`/`chargeInIntercept` (Task 6); `PITCHER_MIDDLE_EV_MAX_MPH` kept |
| new wall segments + `WALL_RESTITUTION` ≈0.6 + `groundPathSegments` | app.js | Task 7, blocked on Alex geometry |
| `parseThrowOrder` (4964) / `THROW_ORDER_DIGIT_TO_BASE` (4963) | app.js | rewritten typed-leg parser, warn-and-reject on stray chars / deleted (Task 8.1) |
| `outThrowTargets` and every consumer | app.js | typed legs + `baseLegs` helper (Task 8.2) |
| cutoff spots (authored table vs derived-on-line) | app.js | Task 8.3, Alex decides; recommend derived |
| `tools/migrate_throw_order.py` + CSV + archives + editor validation | repo | one-time migration, single commit (Task 8.4) |
| `.throw-clip-rect`/`.throw-ball` easing | style.css 1009/1021 | `ease` → `linear`, together (Task 9.1) |
| `MARGIN_POLICY.contestedSafe` | app.js 3210 | provisional 300/600 pending post-9.1 visual check (Task 9.2) |
| new `THROW_SPEED_BY_POS`; `throwDrawMsForFt(distFt, mph)`; `throwProfile(cls, pos)` | app.js | per-position speeds; `THROW_SPEED_MPH` survives as fallback (Task 9.3) |
| new `slowThrow` reconciler knob | app.js `reconcileThrowSchedule` | primary "land later" lever; `holdRelease` becomes overflow (Task 9.4) |
| new `runnerPassingAdjustments` + `RUNNER_MIN_GAP_ORD` ≈0.1 + `trailLateBreak`/`trailSlowPace` knobs | app.js | no-passing constraint, single-sourced into arrivals + tokens (Task 10) |
| `runnerProfile` top speed; new `RUNNER_SPD_FT_PER_S_PER_POINT = 1.0` | app.js 4373/1640 | additive model, runners only (Task 11) |
| shadow fade on cleared-fence | app.js `ballArcHtml` (+ possibly style.css) | opacity at truncation stop (Task 12) |
| `IDLE_DRIFT_MAX_FT` 10→12, `IDLE_DRIFT_DECAY_FT` 40→110 | app.js 4093-4094 | tuning only (Task 13) |
| `_player_view`/`_defense_entry`/`build_moment` | key_moments_build.py | + eye (entry[3]), + `pitcher_awr` (Task 14.0) |
| new `catcherEye`/`eyeArmMph`; `stealThrowHtml` real-distance draw | app.js | EYE → steal-throw speed (Task 14.1) |
| `STEAL_LEADOFF_FT` → `stealLeadoffFt(m)` = `15 - awr` | app.js 5764+ | awr-3/null ≡ today's 12ft exactly (Task 14.2) |

---

## 16. Staging for execution

Separate commits, in order; each leaves the app working. Test updates land inside their
stage, not at the end.

- **Stage 0** - commit the fact-26 working-tree diff verbatim (verify it's still
  present/uncommitted first; if another session committed it, skip) + update the round-1
  plan's constants table + this plan checked in.
- **Stage A - small high-confidence visual fixes.** Task 9.1 (CSS linear), Task 12
  (shadow fade). Then a visual pass with Alex: re-judge "too close" (gates 9.2) and
  "OF too fast on XBH" (gates Task 4).
- **Stage B - coverage + deadlines + round-1 closeout.** Task 2a+2b, Task 5 (same
  branch), Task 1a (fold/delete), Task 1b (multi-scorer fix), Task 1c (fact-25 test).
- **Stage C - OF throw resync.** Task 3 (profile unification + `fielderBallArrivalMs`
  + base floor). After B so the unified chain branch is the only token path to sync.
- **Stage D - runner model.** Task 11 (speed retune) **then** Task 10
  (passing prevention) - the flatter spread changes 10's firing rate and tuning.
- **Stage E - throw speed family, in sub-order.** 9.2 (only if Stage A's re-watch says
  so) → 9.3 (position table) → 9.4 (`slowThrow` knob).
- **Stage F - ThrowOrder alphabet + cutoffs.** Task 8: parser + typed legs + migration
  script + CSV + archive rebuild + editor validation in one commit; cutoff-spot
  mechanic in a second once Alex answers 8.3. After E (cutoff legs draw at
  position-appropriate speeds).
- **Stage G - steal attributes.** Task 14.0 pipeline → 14.1 (needs 9.3's C row) + 14.2;
  archive rebuild decision recorded either way.
- **Stage H - remaining mechanics, priority-ordered.** Task 6 (pitcher reaction, with
  its probe), Task 4 (only what Stage A's re-test still justifies), Task 13 (low
  priority, anytime), Task 7 (last - blocked on Alex's wall geometry; largest greenfield).

**Named regression plays** (round-1 §7 list still applies): re-check after Stages B, C,
D, and E - especially the Calvin Huff dead-center double (Tasks 3/4 territory), the
3-1 PFP grounder (Task 2a must not disturb the pitcher-cover choreography), a 6-4-3 and
3-6-3 (chain-mover refactor), an infield single (Task 1a fold), and bang-bang steals
both ways (14.1's real-distance draw must not flip any verdict rendering).

---

## 17. Test plan (`ball_flight_test.py`)

Per stage, beyond the per-task notes above:

1. **Stage B:** infield-single fold tests (replace 1327-1345); multi-scorer HOME
   contestedSafe (1b); fact-25 pin (1c); 2B-coverage-by-thrower cases (LF→2B covers,
   CF/RF→SS covers, non-OF keeps angle rule); grounder deadline compression (Task 5).
2. **Stage C:** `schedule[0].startMs >= fielderBallArrivalMs` on a deep double with
   explicit ThrowOrder; floor never *lowers* base.
3. **Stage D:** additive speed table (spd 1/3/5 leg times; spd-3 ≡ RUN_LEG_MS to the
   ms); no-passing sweep over the result-code harness (no pair of same-direction movers
   ever violates the gap at any breakpoint, adjustments recorded and bounded).
4. **Stage E:** margin-table value updates (9.2); per-position draw times (9.3);
   `slowThrow` selection, floor fallback to holdRelease, single-constant-mph invariant
   (9.4). The existing forceOut sweep (586-600) and reconciler unit tests (1439-1494)
   must keep passing with the new knob present (knob order asserted).
5. **Stage F:** new-grammar parser cases (bases, positions, mixed `6h`, separators,
   stray-char → null+warn, position-final warn); migration idempotence (running the
   script twice is a no-op); a migrated real row round-trips through
   `outThrowTargets`/`throwSchedule`; archived-data guard (no old-convention digit-only
   value parses as positions silently - the warn path covers it).
6. **Stage G:** eye/awr fallback identities (null ≡ today's exact timings) and
   monotonicity (eye 5 faster throw; awr 5 shorter lead).
7. **Stage H:** pitcher-reaction pins (one P-wins comebacker, one 3B-wins charge);
   dive-and-miss timing invariant (leg 1 never beats the landing); ricochet reflection
   unit test against a hand-computed bounce + regression pin that non-ricochet plays'
   `fieldedPoint`/`fieldedMs` are bit-identical after the segment refactor.

---

## 18. Open questions for Alex (blocking marked ⛔)

1. ⛔ **Task 7 wall geometry:** side-wall segments (endpoints in field feet, height),
   and whether line-hugging contact (recommended) is the intended scope vs. real
   foul-ground rollout.
2. ⛔ **Task 8.3 cutoff spots:** hand-authored coordinate table vs. derived
   point-on-the-throw-line at the cutoff man's depth (recommended). If authored: supply
   the coordinates.
3. **Task 9.2 margins:** sign off (post-9.1 viewing) on whether contestedSafe widens,
   and on the provisional 300/600.
4. **Task 14 archive rebuild:** rebuild s01-s12 so historical steals get real EYE/AWR,
   or accept league-average fallback there? (Task 8's rebuild is NOT optional and
   happens regardless - this question is only about the eye/awr fields' backfill; doing
   both in one rebuild is the efficient answer if timed together.)
5. **Task 1a follow-on:** extend `firstBaseCoverage` to `infield_single` so a deep
   1B-fielded infield single can show the pitcher covering? (Recommended, one-line
   archetype change, but it's a story change he should bless.)
6. **Task 14.1 `eyeArmMph` shape:** piecewise map through the real 80mph average
   (75/80/90 at eye 1/3/5) vs. simple linear across 75-90 - recommendation is
   piecewise; confirm.
7. **Task 4:** after Stage A's re-watch - which XBH plays still read wrong? (Decides how
   much of the dive-and-miss design gets built.)
8. **Task 6:** confirm the probe-selected pitcher reaction value before it's pinned in
   tests.

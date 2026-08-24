# Gameday Fielding/Reconciliation Audit - Implementation Plan

Produced from `gameday-fielding-reconciliation-audit-fable-prompt.md` (audit performed against the current `docs/js/app.js`, 12,050 lines - all line numbers below verified against this revision). This plan is for Sonnet to implement. Decisions marked **[Alex, confirmed]** were answered directly during planning; decisions marked **[Fable's call]** are reasoned recommendations Sonnet should follow unless a stage's own probe contradicts them; anything marked **[OPEN]** must not be silently resolved - surface it.

Decisions locked with Alex during planning:

1. **Task 2 architecture: sequential per-leg pass** [Alex, confirmed] - walk out legs in chain order, reconcile each against its own runner with the existing water-fill, consuming shared headroom in place. No global joint solver.
2. **Task 5 probe runs as Stage 0** [Alex, confirmed] - Sonnet writes and runs the corpus probe first; constant tuning is gated on its numbers, per the decision rules in section 5.
3. **Task 4's slow direction lives inside the fielding race** [Alex, confirmed] - a bounded slower charge pace solved in `resolveGrounderInterception`, so ball trail / glove / schedule stay one story. Not a render-only ease.
4. **Task 3 backstop: bounded pool + recorded unbounded backstop** [Alex, confirmed] - invariant #4 always holds; when the pooled bounded knobs can't close the gap, the existing token compression closes the rest, but now recorded as a named adjustment with a console warning, matching the throw side's honesty.

Every design below was checked against the 13 non-negotiable invariants in the prompt. The two that drive most of this plan: **#3/#4 independent-per-runner** (Task 2), **#4's force-out "someone is physically there to receive it"** (Task 3), and **#11 relay can't leave before the previous throw lands** (a constraint the new per-leg machinery must actively preserve, called out at each point it's at risk).

---

## Corpus facts (verified during planning, ground truth for Stages 0/6)

The full renderable corpus is local: `docs/data/s*/key_moments.json`, **17,810 moments**, of which **2,177 are multi-out plays** (`outs_after - outs_before >= 2`). Result counts (top relevant): DP 1508, DP21 283, DP31 228, DPH1 100, GO 793, GORA 268, FCH 96, FC 94, IF1B 283, SacF 380, DSacF 46, CS2 505, CS 156. `throw_order` values present: `s` (2026), `s,f` (1682), `f` (1041), `h` (540), `t,f` (228), `h,f` (100), `t` (79), `t,s,f` (11). No TP-family results appear in the corpus's top counts - Stage 0 must enumerate the exact set (tests then cover every shape that actually exists, plus one synthetic TP).

Moment records carry everything the probe needs: `result, diff, obc_before/after, outs_before/after, runs, throw_order, throw_order_by_position, defense, batter_spd, runners_on_base, batter_hand, default_position, excluded_positions, pitch, swing`.

---

## 1. Fielding/anchor layer audit (Task 1)

### Verdict: structurally sound - consolidate the data, keep the sequence

The prompt's worry that the "no genuine crossing" condition might be re-derived per call site is **not the case**: mechanisms #2 (bearing cheat), #3 (depth leeway), #4 (honest re-time), and #5 (depth-pct propagation) all live in ONE block inside `resolveGrounderInterception` (app.js:3322-3387), gated on a single `isCappedFallback` computed once (app.js:3320-3321). Downstream consumers (`fielderStartAnchorFt` app.js:4987, the debug panel) read only the *outputs* (`flight.chargeAnchorFt`, `flight.infieldDepthPct`), never re-derive the condition. The sequencing - bearing, then depth off the bearing-adjusted dirt edge, then re-time from the final anchor, then derive the propagation scalar from the final depth - is genuinely order-dependent and genuinely correct (each step's input is the previous step's output; there is no valid reordering). So: no architectural rework. Three concrete changes:

### 1.1 Merge the flight properties into one named record

Replace the two ad hoc properties with a single record:

```js
flight.fieldingAdjust = {
  anchorFt: {x, y},        // was flight.chargeAnchorFt
  depthPct: 0..1,          // was flight.infieldDepthPct (0 / absent when no leeway fired)
  cappedFallback: bool,    // NEW: was implicit - whether the no-genuine-crossing leeway fired
  paceScale: 0..1,         // NEW (Stage 3, Task 4): slow-direction ease applied to the winner, 1 = none
};
```

Rationale: Stages 3-4 need `cappedFallback` downstream (the pace knob must never fight the positioning leeway - see 4.3), and carrying the gate on the record is the "computed once and threaded through" shape the prompt asks about. Writers: `resolveGrounderInterception` (both the plain-intercept path, `depthPct: 0, cappedFallback: false`, and the leeway path), `resolveSinglePickup` (app.js:3463 - anchorFt only, `depthPct: 0`). Readers to migrate (grep `chargeAnchorFt` and `infieldDepthPct`, currently 8 sites): `fielderStartAnchorFt` (app.js:4997, 5010), the debug `anchorNote` (app.js:9136-9137), and any `ball_flight_test.py` references. Keep the migration mechanical - no behavior change in this step.

### 1.2 Extract the leeway block into a named helper

Extract app.js:3322-3387 into `applyGrounderDeepSetup(m, flight, intercept, gp, maxAlongFt, raceRestFt)` returning `{anchorFt, depthPct, groundTimeS}` (caller assigns). Pure extraction, plus one pinned test (6.4) asserting the sequence on a fixture where reordering bearing/depth would change the answer. The function comment should carry the order-dependence rationale currently spread across inline comments.

### 1.3 Dispositions on the open items in this layer

- **`WINNER_CHEAT_MAX_DEG` (3 deg, never eyeballed)**: keep the value; add it to the existing debug tuning panel (`tuningFields()`, app.js:1328 - the panel already live-edits module vars, so this is a one-entry addition). Eyeballing happens through the panel against real plays, not a calibration pass. [Fable's call]
- **OF-hit depth-side knob (taxonomy gap)**: **do not build it.** Angle-shading (`ofDerivedShadeAnchorFt`) plus `OF_READ_DELAY_MAX_MS` already give bounded, two-knob outfield misses; a depth shade would change catch geometry on air balls (adjacent to out-of-scope trajectory work) and no observed play motivates it. Recorded as a deliberate non-goal; revisit only when a real play fails. [Fable's call]
- **`hustleRunner` vs runner-delay framing (taxonomy)**: same category, two bounded directions - `runnerLateJump` (later, 400ms cap) and `hustleRunner` (earlier, 200ms cap) are the runner-delay knob's two directions; `stretchRunner` is runner-speed's slow direction, per-player SPD its honest base. No code change - one comment block above `RUNNER_HUSTLE_MAX_MS` (app.js:3687) stating the taxonomy mapping, so the next reader sees one system. [Fable's call]
- **Acceleration axis (Claude's addition in the prompt)**: **defer.** The pace-scale range (Task 4) covers every observed need; per-play accel variation adds a second free parameter with no data to ground it and no play demanding it. Noted as a later stretch on `FIELDER_PACE_SCALE`'s comment. [Fable's call]
- **Infield read-delay for hits (Claude's addition)**: **defer, conditionally.** The slow-pace knob (Task 4) is the more honest lever for the same case (the fielder visibly tries and is late, vs. visibly hesitates). Stage 0 measures how often the pace range alone can't close the infield-hit gap; only if that number is material does an `INFIELD_READ_DELAY_MAX_MS` knob get added, pooled with pace via the standard water-fill. [Fable's call, gated on Stage 0]

---

## 2. Multi-out reconciliation design (Task 2 - the core)

### 2.1 The load-bearing property that makes the sequential pass sound

Verified in the current code: the two schedule-moving "earlier" knobs only ever move legs earlier, and moving leg *i* earlier moves every leg after it earlier too (`quickRelease`'s inner loop shifts `j = i..end`, app.js:3932; `speedThrow` shifts every later leg by its gain, app.js:3990). For out legs, the "later" direction is the *compressing* direction, which the floor-only rule skips (app.js:3809-3810). Therefore: **corrections to an earlier out leg never hurt a later out leg's margin, and no out leg ever receives a "later" correction.** The only later-direction consumers are the *final* leg (safe classes) and receiver floors - both handled below. This is why sequential-in-chain-order is near-optimal here and no joint solver is needed. Cross-leg headroom bookkeeping is free: the schedule is mutated in place, so gaps already reclaimed are gone from the next leg's `quickHeadroomMs` scan, and mph already raised trips the existing `mphFromFast >= ceilSpeedRange.max` guard (app.js:3977). State this property in a comment at the top of the new wrapper - it is the correctness argument for the whole design.

### 2.2 `reconcileLeg` - generalize `reconcileThrowSchedule` to an arbitrary leg

File: `docs/js/app.js`. Refactor `reconcileThrowSchedule(schedule, runnerArrivalMs, cls, diff, isOut, runnerWho)` (app.js:3781) into:

```js
function reconcileLeg(schedule, uptoIdx, runnerArrivalMs, cls, diff, isOut, runnerWho, holdFromIdx)
```

Changes from the current body, each small:

- `var last = schedule[schedule.length - 1]` becomes `var leg = schedule[uptoIdx]`; every `last.*` reference follows.
- **delta<0 (must land earlier)**: `quickHeadroomMs` sums gaps only over `gi = 1..uptoIdx` (a gap after the target leg can't make it land earlier), plus the leg-0 sliver (`THROW_DELAY_MS + schedule.setupMs`) exactly as now. The reclaim loop iterates gaps `1..uptoIdx` but keeps shifting `j = i..schedule.length-1` (full suffix - later legs ride along, preserving invariant #11's gap floors). The leg-0 sliver shift stays whole-chain. `speedThrow` cascades backward from `uptoIdx` down to 0 (currently from `schedule.length - 1`); its shift-later-legs loop is already correct as written.
- **delta>0 (must land later)**: only ever reached when `uptoIdx === schedule.length - 1` (out legs skip via floor-only; intermediate legs are always out legs - assert this with a comment and a defensive early-return if `uptoIdx < schedule.length - 1 && delta > 0` after the compressing check, so a future caller can't silently push a mid-chain leg later and break invariant #11). `slowThrow` operates on `leg` as now. `holdChainTo` gains a `fromIdx` parameter (below) and is called with `holdFromIdx`.
- Every adjustment record gains `legIndex: uptoIdx` (debug traceability; `who` stays as-is).
- Keep the exported name `reconcileThrowSchedule` as a thin final-leg wrapper - `ball_flight_test.py` calls it directly in at least 10 places (lines 2092, 2106, 2117, 2125, 2281, 2346, 2354, 2365, 2375, 2449, 2462) and none of those tests should need edits in the refactor stage:

```js
function reconcileThrowSchedule(schedule, runnerArrivalMs, cls, diff, isOut, runnerWho) {
  return reconcileLeg(schedule, schedule.length - 1, runnerArrivalMs, cls, diff, isOut, runnerWho, 0);
}
```

### 2.3 `holdChainTo` gains `fromIdx`

`holdChainTo(schedule, requiredEndMs, knob, reason, fromIdx)` (app.js:3698): shift only legs `fromIdx..end`. `fromIdx = 0` (or omitted) is byte-for-byte today's behavior - the pitcher-cover-1B caller and the tag-throw path pass nothing and are untouched. The gap between `fromIdx-1` and `fromIdx` *grows* - a receiver visibly holding the ball, physically plausible, and invariant #11 is about the gap never going negative, which growing can't violate.

### 2.4 `reconcileChain` - the new per-leg wrapper inside `throwSchedule`

Replace the single final-leg reconciliation block (app.js:6961-6974) with:

```js
function reconcileChain(schedule, m, flight, moves) {
  var adjustments = [];
  var metas = [];
  var lastOutIdx = -1;
  for (var i = 0; i < schedule.length; i++) {
    if (!schedule[i].out) continue;
    var mv = runnerForOutTarget(m, moves, schedule[i].base);
    var arrival = forcedOutRunnerArrivalMs(m, flight, moves, schedule[i].base);
    if (arrival != null) {
      var r = reconcileLeg(schedule, i, arrival, "forceOut", m.diff, true,
        mv && mv.from, lastOutIdx + 1);
      adjustments = adjustments.concat(r.adjustments);
      metas.push(legMeta(i, "forceOut", arrival, ...));   // same fields as today's reconcileMeta, + legIndex
    }
    lastOutIdx = i;
  }
  var finalIdx = schedule.length - 1;
  if (!schedule[finalIdx].out) {
    var smv = runnerForSafeTarget(m, moves, schedule[finalIdx].base);
    var sArrival = safeRunnerArrivalMs(m, flight, moves, schedule[finalIdx].base);
    if (sArrival != null) {
      var rs = reconcileLeg(schedule, finalIdx, sArrival, "contestedSafe", m.diff, false,
        smv && smv.from, lastOutIdx + 1);   // holds shift only the suffix after the last out leg
      adjustments = adjustments.concat(rs.adjustments);
      metas.push(legMeta(finalIdx, "contestedSafe", sArrival, ...));
    }
  }
  return { adjustments: adjustments, metas: metas, lastOutIdx: lastOutIdx };
}
```

Key facts verified for this design:

- **The per-leg runner lookup already exists.** `runnerForOutTarget(m, moves, targetBase)` (app.js:6585) walks every OUT move, maps each through `FORCED_OUT_BASE`/`NEXT_BASE`, and matches `targetBase` - it resolves intermediate bases today; only nobody calls it for them. `forcedOutRunnerArrivalMs` (app.js:6607) likewise. No new lookup machinery is needed; this is the prompt's first bullet answered by existing code.
- **Ordering**: out legs ascending, final safe leg last. Earlier passes can only add margin to later out legs (2.1). The final safe leg's "later" correction is confined to `slowThrow` on the final leg (endMs grows, no earlier leg moves) plus suffix-only `holdChainTo` - so a 6-4-3 whose back end is decorative can no longer undo the 2B force's margin. This is the one genuine conflict in the sequential model and `fromIdx` is its complete resolution.
- **One caveat to verify in implementation**: after an earlier out leg's `quickRelease` reclaims a gap, a later out leg's own pass may find `delta > 0` (its honest throw now lands *earlier* than its band) - the compressing check correctly leaves it alone (blowout accepted, floor-only). No code needed; add a test pinning it (6.1).
- **`speedThrow`'s "whole chain, cascading backward" under multiple targets** (the prompt's third bullet): resolved naturally - each leg's own pass cascades backward *from that leg*; a shared earlier leg (leg 0 of a DP) can be sped up by leg 1's pass, and leg 2's pass then finds it already at ceiling and skips (the existing guard). No leg is ever sped past its thrower's `THROW_SPEED_BY_POS.max`, regardless of how many passes touch it.

### 2.5 `applyReceiverFloors` gets per-out-leg caps

Current signature `applyReceiverFloors(schedule, plan, hardCapMs)` (app.js:6733) caps only the *final* leg's push. With intermediate outs reconciled, a floor shift at leg *i* (suffix shift `j = i..end`) could push a *later out leg* past its own runner. Change: replace `hardCapMs` with a `capByIdx` map built in `throwSchedule` - for every out leg *j* with a known runner arrival, `capByIdx[j] = arrival_j - MARGIN_POLICY.forceOut.minMs`. When shifting from leg *i*, the allowed shift is `min over all capped legs j >= i of (capByIdx[j] - schedule[j].endMs)`, floored at 0. Residual (receiver still later than ball) falls to the token-side backstop exactly as today - which Stage 4 upgrades (section 3). Compute the arrivals once in `throwSchedule` and share the map between `reconcileChain` and `applyReceiverFloors` (they reconcile against the same runners; two lookups would eventually disagree).

### 2.6 Debug/meta plumbing

- `schedule.reconcileMeta` becomes `schedule.reconcileMetas` (array, one per reconciled leg, each with `legIndex`). Keep `schedule.reconcileMeta` assigned to the final entry as a back-compat alias until `sceneDebugHtml` (app.js:9169 area) is updated in the same stage to render one block per leg; then remove the alias and fix any test references.
- `throwRunnerAdjustmentMs` (app.js:7045) already filters by `who` and sums - per-leg runner knobs for *different* runners coexist correctly with zero changes. Verify with a test (6.1) that a DP's two runners each get only their own `runnerLateJump`/`stretchRunner` entries applied to their tokens.
- The tag-throw path (app.js:6812-6871) is untouched - single decorative leg, `uncontested`, already correct.

### 2.7 Interaction risks called out (do not skip these when implementing)

- `FORCED_OUT_BASE.FCLead = "2B"` is a documented unverified default (app.js:5895-5896) and FCLead doesn't appear in the corpus top counts - Stage 0 asserts whether it appears at all; if not, leave as-is with the existing comment. **[OPEN if it appears]**
- `runnerForOutTarget` returns the *first* OUT move matching the base. Two OUT moves mapping to the same base would be a data anomaly; Stage 0 asserts uniqueness across the corpus rather than the code guessing. **[OPEN if violated]**
- `m.diff` is shared by all legs of one play (one roll). All legs use the same diff-scaled margin - deliberate, note in the `reconcileChain` comment.

---

## 3. Base-coverage timing design (Task 3)

Two mechanically distinct cases, per the prompt. Both get the same shape: **pooled bounded knobs first, recorded backstop last** [Alex, confirmed].

### 3.1 Case 1 - covering fielder receiving a throw

**What actually exists today** (a correction to the prompt's framing, verified): the covering fielder is *not* wholly unguaranteed. `applyReceiverFloors` holds the ball for a late receiver (bounded by the out-leg caps), and `fielderTokensHtml`'s §4.3 backstop (app.js:5641-5651) sets the receiver token's `deadlineMs = leg.endMs`, which `fielderMovePacing` (app.js:5306-5314) meets by **silently compressing the run with no bound and no record**. So the transitive guarantee (receiver <= ball <= runner - margin) already holds at render time; what's missing is exactly what Alex's design direction names: the closing mechanism is a single unbounded knob pushed to an extreme, invisible in the debug trail. The work is to put a bounded, pooled, *recorded* correction in front of it.

**New function** `reconcileCoverage(m, flight, plan, schedule)`, called from `fielderTokensHtml` where the §4.3 backstop currently lives (it needs both `plan` and `schedule`; `chainMoverPlan` can't see the schedule, and `throwSchedule` must never read render output - the acyclicity probe 0.5 constraint, restated here). For each plan entry matched by `receiverForLeg` with `deficit = entry.arrivalMs - leg.endMs > 0`:

Pool via `allocateAcrossKnobs(deficit, [...])`:

1. **`coverEarlyBreak`** - headroom = `entry.startMs` (reduce the `ballPassesDepthMs`/`fieldedMs` gate, app.js:5549-5550, toward 0 = break at contact). A coverer breaking for the bag on contact is real baseball; the depth gate is a legibility nicety, exactly the kind of discretionary delay `quickRelease` reclaims on the throw side.
2. **`fielderSprint`** - Task 4's fast direction: headroom = `naturalRunMs - runMsAtMaxPace`, where max pace = the entry's profile pace times `FIELDER_PACE_SCALE[profileKind].max` (section 4). Real, per-player-grounded ceiling, not "however fast makes the math work."

Apply the shares: recompute the entry's `startMs` and leg durations at the eased values (re-run `fielderMovePacing` with the adjusted profile - add an optional `paceScaleOverride` argument rather than a parallel path), and push adjustment records (`knob: "coverEarlyBreak"` / `"fielderSprint"`, `who: entry.pos`) onto `schedule.adjustments` so the debug panel shows them alongside the throw knobs.

**Backstop**: any remaining deficit falls to the existing deadline compression, now recorded as `{knob: "coverCompress", who: pos, ms: residual}` plus a `console.warn` mirroring the reconciler's "unresolved" wording (app.js:4030-4033). Invariant #4 always holds; the warning is the honesty.

**Required-arrival margin**: keep `required = leg.endMs` (arrive with the ball), matching today's floors - no new lead constant now. If plays read as the receiver arriving photo-finish with the ball, a `COVER_LEAD_MS` beat is the named future lever; don't add it speculatively. [Fable's call]

### 3.2 Case 2 - unassisted carry (fielder runs the bag themselves)

The final-leg reconciliation *does* run on unassisted legs today, but both mph knobs skip them (`!last.unassisted` guards, app.js:3825/3860/3967) - so the footrace's only "earlier" lever is `quickRelease`, then it's unresolved. Two new knobs inside `reconcileLeg`, sharing Task 4's pace range so this and Task 4 are one mechanism, not two [per prompt's explicit convergence ask]:

- **`sprintCarry`** (fast): in the delta<0 cascade, where `speedThrow` currently `continue`s on `leg.unassisted`, instead solve the jog `drawMs` down toward `leg.distFt` at the carrier's run-profile pace times `FIELDER_PACE_SCALE.run.max` (the carrier is `leg.throwerPos`, which `throwSchedule` already sets to the covering position for unassisted legs, app.js:6937). Same shape as `speedThrow`: solve exactly, cap at ceiling, record `{knob: "sprintCarry", mphFrom/mphTo replaced by paceFrom/paceTo ft/s}`.
- **`easeCarry`** (slow): mirror in the delta>0 path where `slowThrow` skips unassisted legs - ease the jog toward `FIELDER_PACE_SCALE.run.min` before falling to `holdRelease`.

`unassistedLegTiming` (app.js:5577) reads `leg.drawMs` directly for the token's bag-run leg, so the rendered glove automatically matches the reconciled pace - no render-side change. Verify the leg-2 dwell math (`leg.startMs - (startMs + leg1Dur)`, app.js:5586) still floors at 0 when `sprintCarry` pulls `leg.startMs` earlier.

**Pace ceiling grounding** (the prompt's explicit question - don't just reuse `FIELDER_CHARGE_FT_PER_S`): the unassisted bag-run and coverage sprint use the **run** profile (base 27 ft/s = `RUNNER_SPRINT_FT_PER_S`, per-player scaled), not the charge profile (16 ft/s, a controlled fielding approach) - `fielderProfile`'s existing kind split already encodes the "dead sprint vs controlled charge" distinction the prompt asks about. The new range multiplies each kind's own base, so the sprint ceiling is a sprint ceiling and the charge ceiling is a charge ceiling by construction. See 4.1 for numbers.

---

## 4. Bidirectional fielder movement-speed knob (Task 4)

### 4.1 The constant

```js
// Per-profile-kind bounded pace range, the movement-speed sibling of
// THROW_SPEED_BY_POS - multiplicative around each player's own
// spdPaceScale'd base pace (multiplicative, not absolute ft/s, so the
// per-player SPD uniqueness survives at the bounds instead of every
// player pinning to one shared ceiling).
var FIELDER_PACE_SCALE = {
  charge:  { min: 0.75, max: 1.20 },
  run:     { min: 0.80, max: 1.12 },   // 27 ft/s base -> ~30.2 ft/s max, elite-sprint territory
  pursuit: { min: 0.85, max: 1.10 },
};
```

**These numbers are starting guesses, flagged as such** (same status `WINNER_CHEAT_MAX_DEG`'s own comment holds): the run ceiling is anchored to real elite sprint speed (~30 ft/s statcast-style) which is genuinely grounded; the charge and pursuit bounds are feel numbers. All six values go into the debug tuning panel (`tuningFields()`), and Stage 0's probe reports the *demanded* range per knob across the corpus (how much slowdown infield hits actually need, how much sprint coverage actually needs) - if demanded consistently exceeds the guess, that's the calibration signal, surfaced with numbers instead of tuned per play. [Fable's call on shape; probe-gated on values]

### 4.2 Slow direction - inside the fielding race [Alex, confirmed]

New gated block at the end of `resolveGrounderInterception`, after the existing leeway block, firing only when: ground archetype AND no new out on the play (`outs_after <= outs_before` - covers `infield_single` and bunt *hits*; SacB and every GO/DP-family play records an out and must never be slowed) AND the honest downstream story is "too good":

- **Trigger test** (local, honest, no schedule construction): `fieldedMs + THROW_DELAY_MS + throwDrawMsForFt(dist(fieldedPt, 1B), THROW_SPEED_BY_POS[pos].mph) < batterArrivalMs + targetMarginMs("contestedSafe", m.diff)` - i.e. the honest throw would beat the batter, meaning today's reconciler must lean on `slowThrow`/`holdRelease` to make the recorded hit true. Batter arrival via the same `runnerLegMs` formula `safeRunnerArrivalMs` uses (factor a small shared helper rather than duplicating - the "one formula, two readers" rule this file already follows).
- **Solve**: bisect `paceScale` in `[FIELDER_PACE_SCALE.charge.min, 1]` for the largest slowdown-free value... precisely: the *smallest slowdown* (largest scale) at which the trigger inequality flips - re-running `fielderInterceptS(anchor, flight, gp, maxAlongFt, null, ftPerS * scale, reactionS)` for the *winner only* per step (cheap - 40-step grid). The winner's identity is **kept**, never re-raced: re-racing at eased pace could hand the ball to a different fielder and re-litigate the HZ/BRC assignment upstream, and the eased fielder still fielding it is exactly the story (they got there, just late). The fielded point moves deeper along the roll as pace eases; `maxAlongFt` still caps it (a ground-archetype hit stays on the dirt - same rule as ever), and the arrival is honestly re-timed by the intercept solve itself. If the full `min` scale still doesn't flip the trigger, keep `min` and let `slowThrow`/`holdRelease` close the rest downstream exactly as today - the layers compose, the knob just does the honest share first.
- **Record**: `flight.fieldingAdjust.paceScale = scale` (1.1's record); surface in the debug anchorNote and as a schedule adjustment (`knob: "easeCharge", who: pos`) so the trail is complete.
- **Guard against fighting the leeway** (1.1's `cappedFallback`): the leeway block (deeper start, out plays) and this block (slower pace, hit plays) are disjoint by the outs gate - assert that in a comment and a test; they must never both fire on one play.
- **trajectory.js null-tolerance** (prompt's flagged risk): the eased intercept's `alongFt` comes from `fielderInterceptS` itself, never from an addition-then-subtraction round trip, so `gp.timeAt` inputs stay in-range by construction - but keep the established `gp.timeAt(x) != null ? ... : fallback` guard pattern on any new call site anyway (app.js:3384-3385 is the model).

### 4.3 Fast direction

Consumed exclusively by Task 3's knobs (`fielderSprint` coverage, `sprintCarry` unassisted) - one range, three consumers, exactly the convergence the prompt asks for. **Deliberately NOT applied to the ball-toucher's charge on out plays this round**: the positioning leeway (section A #2/#3) already closes that gap, and layering a second mechanism on the same condition is precisely the incoherence this audit polices. If Stage 0 shows out plays where even full leeway leaves the charge visibly late (the `chainMoverPlan` deadline currently compresses those silently too), the fast direction is the named, already-built lever to wire in - flag, don't build. [Fable's call]

### 4.4 `HOLD_RELEASE_MAX_MS` (the prompt's "worth a second look")

Keep at 20000. It is the invariant-guaranteeing overflow knob - tightening it manufactures unresolvable cases by fiat, and its only large legitimate use (uncontested sac-fly waits, ~13s worst case) genuinely needs the room. The right ceiling-discipline here is Stage 0 reporting the *actual* hold distribution; if real holds cluster under 3s outside the uncontested class, a per-class ceiling becomes a data-backed follow-up. [Fable's call]

---

## 5. Stage 0 - the corpus probe (Task 5, runs FIRST)

**[Alex, confirmed: probe is Stage 0, run by Sonnet before any constant tuning.]**

### 5.1 Prerequisite refactor: `resolvePlayFlight(m)`

Extract `playSceneHtml`'s flight-resolution block (app.js:9580-9605: `flightParams(m, data.meta.flight)` + the archetype dispatch to `resolveGrounderInterception` / `applyAirPositionOverride` / `resolveSinglePickup` / `resolveHitPickup`) into an exported pure function `resolvePlayFlight(m)` returning the resolved `flight` (null when no flight). `playSceneHtml` calls it; the probe and Stage 6's tests call it too. This is the one production-code change Stage 0 makes - a pure extraction.

### 5.2 The probe script

`tools/reconciliation_corpus_probe.py` - Playwright harness copied from `ball_flight_test.py`'s pattern (launch, load the page, `page.evaluate` against `KMFlight`). Per season: load `docs/data/sNN/key_moments.json` + `meta.json`, feed `data.meta.flight` tables in. For every moment with a resolvable flight:

- `flight = resolvePlayFlight(m)`; `moves = deriveRunnerMoves(...)`; `schedule = throwSchedule(m, moves, flight)` (which, pre-Stage-2, still reconciles final-leg-only - the probe measures the *unreconciled intermediate* legs directly).
- Per out leg *i*: `arrival_i = forcedOutRunnerArrivalMs(m, flight, moves, schedule[i].base)`; record `deltaMs = (arrival_i - targetMarginMs("forceOut", m.diff)) - schedule[i].endMs` (negative = leg needs to land earlier), plus that leg's available `quickRelease` headroom (gaps up to *i* + sliver) and `speedThrow` headroom (per-leg mph-to-max gains up to *i*), and the residual after simulating the combined ceiling.
- Final legs: same numbers as the runtime already computes (sanity cross-check against `schedule.reconcileMeta`).
- Infield-hit sweep (Task 4 trigger): for every IF1B/bunt-hit moment, the 4.2 trigger inequality and the pace scale that flips it.
- Data assertions: FCLead presence; `runnerForOutTarget` uniqueness per base; enumeration of every multi-out result shape x `throw_order`/`throw_order_by_position` combo actually present (Stage 6's test matrix).

Output: one CSV (per-leg rows) + a printed summary table (counts, percentile deficits, % unresolved per shape).

### 5.3 Decision rules (pre-committed, so tuning is mechanical, not vibes)

- **If intermediate-leg deficits are common** (expected - that's the finding motivating Task 2): no action, Stage 2 is the fix; the numbers just size it.
- **If post-knob residual ("unresolved") > ~1.5% of out legs**: first lever `RUNNER_LATE_JUMP_MAX_MS` 400 -> 600 (a late read is still real baseball); second `STRETCH_RUNNER_MAX_FRAC` 0.15 -> 0.20. Both stay bounded-tight in spirit; both are panel-tunable for eyeballing.
- **If residuals survive both** and cluster on specific shapes (e.g. DPH1's catcher-pop chain): consider `MARGIN_POLICY.forceOut.minMs` 150 -> 100 *for that check only as last resort*, and say so out loud - the bands were deliberately pinned last round (app.js:3645-3649).
- **Acceptable residual floor**: < 0.5% of out legs, all on genuinely extreme inputs (slowest-diff worst-case grounders) - at that rate the honest `unresolved` warning is the correct outcome, per the reconciler's own philosophy. Above it, constants move.
- **Infield-hit trigger rate**: whatever % of IF1B plays trip the 4.2 trigger is the slow knob's justification record; if the demanded pace scale routinely bottoms out below `charge.min` 0.75, widen `min` before shipping Stage 3 (with the number in the commit message).

---

## 6. Test plan (Task 6)

All in `ball_flight_test.py`, following its existing `check(...)` section pattern. Existing pins stay green throughout: A4 worst-case sweep (line 637), water-fill §13.8 (line 2164), cutoff/stagger pins, hustle/pooling sections.

### 6.1 Per-leg reconciliation sweep (Stage 2's acceptance)

For every multi-out shape x throw-order combo Stage 0 enumerated (plus one synthetic TP fixture even if absent from the corpus), on the A4 worst-case grounder flight:

- every out leg beats its own runner: `arrival_i - schedule[i].endMs >= targetMarginMs floor` for ALL `i`, not just the last (the direct invariant #3/#4 per-runner check);
- inter-leg gaps never negative after all passes (invariant #11);
- a final safe leg loses by >= contestedSafe floor AND every earlier out leg's margin is unchanged by the safe leg's reconciliation (the suffix-hold pin - build one fixture where the old whole-chain `holdChainTo` would have broken leg 1);
- a later out leg finding itself compressing after an earlier leg's quickRelease is left alone (floor-only pin);
- `speedThrow` never pushes any leg past its thrower's max across multiple passes (multi-pass ceiling pin);
- two runners on a DP each receive only their own runner-knob adjustments (`throwRunnerAdjustmentMs` per-who pin);
- `reconcileThrowSchedule` back-compat wrapper: existing direct-call tests unchanged.

### 6.2 Coverage guarantees (Stage 4's acceptance)

- Every receiver entry: post-`reconcileCoverage` arrival <= its leg's `endMs`; `coverEarlyBreak` never below 0 startMs; `fielderSprint` never above `run.max` scale; deficit beyond the pool produces a recorded `coverCompress` adjustment (and the invariant still holds).
- Unassisted: `sprintCarry` floor/ceiling respected; `easeCarry` mirror; `unassistedLegTiming` leg durations still consistent with the schedule; an unassisted final out leg on the worst-case grounder now closes without `unresolved` where the pace ceiling honestly allows.

### 6.3 Bidirectional pace knob (Stage 3's acceptance)

- Real corpus-derived IF1B fixtures where the honest race trips the 4.2 trigger: post-fix the play still renders a hit (throw loses by >= contestedSafe floor), `paceScale` within `[charge.min, 1]`, fielded point still within `maxAlongFt` (dirt), fielded time == the eased intercept's own honest time (no relabeling), and the leeway block did NOT also fire (disjointness pin).
- A GO/DP fixture asserts `paceScale` stays 1 (outs never slowed).

### 6.4 Fielding-layer consolidation (Stage 1's acceptance)

- `flight.fieldingAdjust` migration: old property names gone (grep-pin), debug anchorNote reads the record.
- `applyGrounderDeepSetup` sequencing pin: a fixture where applying depth before bearing would yield a different anchor - assert the shipped order's exact output.

---

## 7. Fix map (Task 1 consolidations + renames, for tracing old -> new)

| Old | New | Kind |
|---|---|---|
| `flight.chargeAnchorFt` | `flight.fieldingAdjust.anchorFt` | rename/merge (Stage 1) |
| `flight.infieldDepthPct` | `flight.fieldingAdjust.depthPct` | rename/merge (Stage 1) |
| *(implicit `isCappedFallback` local)* | `flight.fieldingAdjust.cappedFallback` | now carried on record (Stage 1) |
| leeway block app.js:3322-3387 | `applyGrounderDeepSetup(...)` | extraction, no behavior change (Stage 1) |
| `reconcileThrowSchedule(schedule, ...)` | `reconcileLeg(schedule, uptoIdx, ..., holdFromIdx)` + back-compat wrapper keeping the old name/signature | generalization (Stage 2) |
| final-leg reconciliation block app.js:6961-6974 | `reconcileChain(schedule, m, flight, moves)` | new wrapper (Stage 2) |
| `holdChainTo(schedule, req, knob, reason)` | `+ fromIdx` param (default 0 = old behavior) | extension (Stage 2) |
| `applyReceiverFloors(schedule, plan, hardCapMs)` | `applyReceiverFloors(schedule, plan, capByIdx)` | per-out-leg caps (Stage 2) |
| `schedule.reconcileMeta` | `schedule.reconcileMetas[]` (+ temporary alias) | debug plumbing (Stage 2) |
| §4.3 silent receiver deadline compression | `reconcileCoverage(...)` pool + recorded `coverCompress` backstop | new (Stage 4) |
| `speedThrow`/`slowThrow` `!unassisted` skip | `sprintCarry`/`easeCarry` branches | new knobs (Stage 4) |
| *(none)* | `FIELDER_PACE_SCALE` (+ tuning panel entries) | new constant (Stage 3) |
| *(none)* | `flight.fieldingAdjust.paceScale`, `easeCharge` adjustment | new (Stage 3) |
| `playSceneHtml` inline flight resolution | `resolvePlayFlight(m)` exported | extraction (Stage 0) |
| `WINNER_CHEAT_MAX_DEG` (unchanged value) | + tuning panel entry | tooling (Stage 1) |

New knob names introduced (for the debug trail's vocabulary): `easeCharge` (slow fielding race), `fielderSprint` (coverage run speed-up), `coverEarlyBreak` (coverage earlier start), `coverCompress` (recorded backstop), `sprintCarry` / `easeCarry` (unassisted bag-run pace).

---

## 8. Staging order for Sonnet

- **Stage 0**: `resolvePlayFlight` extraction + corpus probe + summary numbers. Gates: 5.3's decision rules resolved into concrete constant changes (or explicit no-changes) before Stage 5.
- **Stage 1**: fielding-layer consolidation (1.1-1.3) + tests 6.4. Pure refactor - full suite green, zero rendering diffs expected.
- **Stage 2**: per-leg reconciliation (2.2-2.6) + tests 6.1. The core. Land before 3/4 - both later stages hang adjustments off its plumbing.
- **Stage 3**: `FIELDER_PACE_SCALE` + slow direction in the race (4.1-4.2) + tests 6.3. Depends on Stage 1's record.
- **Stage 4**: coverage pool + unassisted knobs (3.1-3.2) + tests 6.2. Depends on Stage 2's adjustments plumbing and Stage 3's range.
- **Stage 5**: constant tuning per Stage 0's decision rules; re-run the probe post-Stage-2/3/4 to confirm residual rates landed under the 0.5% floor.
- **Stage 6**: full corpus-shape test sweep finalized; existing pins re-verified; debug panel entries confirmed live.

## 9. Open questions (do not resolve silently)

1. **FCLead** - if Stage 0 finds it in the corpus, the "2B" default needs verifying against a real play before per-leg reconciliation exercises it. **[OPEN]**
2. **`runnerForOutTarget` uniqueness** - if Stage 0 finds two OUT moves mapping to one base, the first-match rule needs a deliberate tiebreak (likely the trailing runner, mirroring `runnerForSafeTarget`'s latest-arriver rule). **[OPEN]**
3. **Pace-scale bounds** - the 4.1 numbers are starting guesses by design; if Stage 0's demanded-range data contradicts them, the probe's numbers win and the plan's values are revised in the Stage 3 commit. **[flagged, probe-gated]**
4. **Out-play charge lateness** (4.3) - if Stage 0 shows out plays where full positioning leeway still leaves the ball-toucher's charge visibly compressed, wiring the fast pace direction into that deadline is the named follow-up; it is deliberately not in this round's scope. **[flagged]**

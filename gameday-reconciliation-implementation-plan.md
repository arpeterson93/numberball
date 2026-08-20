# Gameday fielder/runner/throw reconciliation - implementation plan

Handoff plan for Sonnet, produced against `gameday-reconciliation-fable-prompt.md`.
Scope: the fielder-pursuit / runner / throw / reaction-time layer of `docs/js/app.js`.
**Out of scope, do not touch:** `docs/js/trajectory.js`, and everything the 2026-08-07
ball-flight redesign already shipped (launch physics, 3D rendering, bounce/rollout,
`resolveGrounderInterception`'s depth-interception mechanics as such). Also out of
scope by Alex's explicit call: a true cutoff-man mechanic (mid-flight redirection).

**Priority order, restated (inviolable):**
(a) the rendered play always resolves to exactly what the MLN result says happened;
(b) within that, everything moves at real distances/speeds/reactions;
(c) per-player attribute uniqueness wherever data supports it.

---

## 0. Verified facts (probed against the repo - do not re-derive, but spot-check if code has moved)

These were checked directly; the plan below depends on them.

1. **Per-runner speed data exists and already reaches the client schema.**
   `key_moments_build.py` emits, on every moment: `batter_spd` (line ~816), and
   `runners_on_base` = `{ "1B"|"2B"|"3B": [full_name, last_name, spd] }` for every
   occupied base (line ~826, built by the same `_defense_entry` the `defense` dict
   uses). So Task 3's per-runner speed is a **JS-side wiring change only** for
   current-season data.
   - **Caveat 1:** the checked-in `docs/data/key_moments.json` is a stale artifact
     (deployment moved to GitHub Actions, commit `acfd989`); it predates the spd
     fields. The live deployed data is built in CI from HEAD and will have them
     (production `fielderSpd` already reads `m.defense[pos][2]` successfully).
   - **Caveat 2:** the **historical-season archives `docs/data/s01`-`s12` are
     committed one-time builds from commit `17c894d`, before any spd field
     existed** - they contain zero `"spd"` keys anywhere. Until they're rebuilt
     with the current pipeline, every per-player speed feature (existing fielder
     pace included) silently falls back to `SPD_AVERAGE` on historical seasons.
     Rebuilding the archives is a required step of this plan (Stage 2), not
     optional polish.

2. **No arm-strength rating exists anywhere in the data.** `database.py:324` is the
   full player attribute list: `con, eye, pwr, spd` (batters), `mov, cmd, vel, awr`
   (pitchers). `vel` is pitch velocity, `awr` is the pitcher's steal-defense
   attribute - neither is a fielder's arm. **`THROW_SPEED_MPH` stays a flat
   league-average constant.** Task 3 point 1's throw half is closed as "no data";
   the design below still gives the timing primitive a throw-class parameter
   (full throw vs. underhand toss) as the extensibility hook the prompt requires.

3. **`THROW_LEAD_MS` is dead in production.** Its only real consumer is the test
   assertion `ball_flight_test.py:583` (`batterFirstArrivalMs() - (lastEnd +
   THROW_LEAD_MS)`); in `app.js` it is defined (3117), mentioned in a comment, and
   exported - never read by any scheduling code. Today a grounder's out-throw is
   **forward-timed** (`fieldedMs + THROW_DELAY_MS + real distance at 90mph`), and
   goal (a) is enforced only by (i) the offline test sweep and (ii)
   `runnerOutMotionHtml`'s runtime fallback of holding the runner token **at the
   bag** until the throw lands and only then turning them red. That fallback never
   contradicts the result *label*, but it can contradict baseball itself on
   screen (a runner standing on first, then called out). This is the sharpest
   version of the problem this refactor fixes.

4. **Both Task 1 candidate architectures already exist in the code**, one special
   case at a time:
   - Forward-then-reconcile (shape A): grounder throw schedule (forward, verdict
     un-enforced at runtime); `movingFielderTokenHtml`'s `deadlineMs` compression
     (forward fielder run, compressed to land exactly on a known catch);
     `throwSchedule`'s pitcher-cover-1B clamp (forward, throw release held until
     the pitcher's real arrival).
   - Construct-backward (shape B): the sac-fly `tagStart` solve, `stealThrowHtml`
     (ideal arrival = runner arrival +/- margin, clamped), `infieldSingleThrowHtml`
     (end fixed just past `batterFirstArrivalMs`, start = whatever's left).

5. **Two disagreeing fielder-motion models coexist.** The interception race
   (`chargeFielderArriveS`/`fielderInterceptS`) uses flat charge speeds
   (16/24 ft/s) plus `CHARGE_REACTION_S`, **no acceleration**; the token renderer
   (`fielderLegDurationsMs`) uses `RUNNER_SPRINT_FT_PER_S` (27) as top speed with
   `FIELDER_ACCEL_FT_S2` (25) acceleration. The same fielder covers the same
   ground under two different equations depending on which subsystem asks.

6. **Runners have no acceleration model and no per-player speed.** `RUN_LEG_MS`
   (app.js:1641) is a flat `legs * 90ft / 27ft/s` table; every runner in the
   league covers 90ft in the same 3333ms from a standing start at constant
   velocity.

7. **A data-grounded "how close should this look" precedent already exists.**
   `stealThrowMarginMs` (app.js:5157) scales the throw's margin off the real
   `steal_num`/`throw_num` roll diff (150ms at a near-tie, 450ms at a decisive
   roll). Batted plays carry the analogous quantity: `m.diff`, the pitch/swing
   diff that already drives contact quality. This is the template Task 4
   promotes to the general policy.

8. **Existing test coverage of this layer is real but constant-coupled.**
   `ball_flight_test.py` already asserts: throw-beats-runner over a play sweep
   (via `throwSchedule` + `THROW_LEAD_MS`), the tag-throw margin
   (`TAG_THROW_MARGIN_MS`), `outThrowTargets` target derivation,
   `stealThrowTarget`, grounder-interception geometry/timing, and the CF shift in
   `fielderStartAnchorFt`. `tools/trajectory_reference.py` is ball-flight only.
   Tests will need updating in lockstep (Stage 6), not just extending.

---

## 1. Task 1 - Architecture decision

**Recommendation: (B), implemented with an honest forward pass as its first
stage.** Call it "one timeline, one reconciler": every play runs one shared
pipeline - forward-simulate all actors from real rates and distances, compare
each contested event's honest margin against the verdict and target margin the
known result requires, then close any gap through a single, ordered, bounded set
of named adjustments. The reconciler runs on **every** play as a uniform stage,
not as an exception branch.

Why this and not pure (A) or pure (B):

- **Pure (A) fails on honesty to the constraint.** There is never a live moment
  where the result is uncertain, so "the sim disagrees with the result" is
  always "the sim's inputs were wrong for this play." A conflict *branch*
  (fire only when things diverge) inevitably grows one case per way physics can
  diverge - which is exactly today's pile, restated. And its worst failure mode
  is visible: a correction that snaps in when the divergence is large.
- **Pure (B) (construct everything backward) fails on realism.** Backward
  construction has no natural place for real per-player rates - you get today's
  `infieldSingleThrowHtml` shape everywhere: end-time fixed, start-time
  "whatever's left over", physics only as a duration formula. Goal (b)/(c)
  require the forward pass to be the substrate.
- **The synthesis is what the codebase is already converging toward** piecemeal:
  `deadlineMs` compression and the pitcher-cover clamp are both "forward first,
  then one bounded adjustment." The refactor's job is to make that the *only*
  mechanism, parameterized, instead of five hand-built instances of it.

Consequence for the "jarring correction" worry: adjustments are computed **before
render** (the whole play is compiled to a timing plan, then rendered once), so
nothing ever snaps mid-animation. Large honest-vs-verdict gaps are handled by
Task 5's upstream fixes (make the *inputs* honest - coverage, assignment,
reaction) so the reconciler's per-play correction stays small; the reconciler's
bounded knobs are the residual, not the main act.

---

## 2. Task 2 - Full audit and fix map

Classification: **(a)** genuine baseball-rule distinction, keep; **(b)** hand-tuned
legibility/drama beat with no physical referent, keep but centralize/document;
**(c)** downstream compensation for an upstream gap, fix at source. Additional
dispositions: **fold** = superseded by the shared primitive/reconciler;
**promote** = becomes the template for the shared mechanism; **delete** = dead.

| Item (app.js line) | What it is | Class | Disposition |
|---|---|---|---|
| `RUNNER_SPRINT_FT_PER_S=27` (1640) | league-avg sprint speed | principled | keep as base rate; scaled per-runner (Stage 2) |
| `RUN_LEG_MS` flat table (1641) | constant-speed runner legs | (c) - no accel, no per-player | fold -> `runnerLegMs(m, who, legs)` on the shared primitive |
| `OUT_CHOREOGRAPHY_MS` (1650) | out-token walk-off budget | (b) | keep |
| `SPD_AVERAGE`/`SPD_PCT_PER_POINT=0.12` (2660) | rating->pace scale (placeholder pct) | principled | keep; Alex grounds the pct in Statcast separately |
| `FIELDER_CHARGE_FT_PER_S=16` (2678) | controlled-charge top speed | principled | keep as a *motion-profile* top speed (Stage 1) |
| `OF_CHARGE_FT_PER_S=24` (2691), superseded by `OF_PURSUIT_TOP_SPEED_FT_PER_S=26.4` + `OF_PURSUIT_ACCEL_FT_S2=10.2` (app.js ~4293-4294, gameday-animation-refinements round) | OF pursuit top speed | principled | Statcast-fit (n=90 jump data) replacement, decoupled from `FIELDER_ACCEL_FT_S2`; see updated row below |
| `CHARGE_REACTION_S=0.15` (2696) | recognize-and-break beat | (a) | keep as profile reaction param |
| `CHARGE_CANDIDATE_POSITIONS` / `OF_...` (2701/2709) | who can win a charge race | (a) | keep |
| `chargeFielderArriveS`/`fielderInterceptS`/`chargeInIntercept` (2721-2853) | the real interception race | principled | keep semantics; arrival math re-based on the shared primitive (Stage 1) |
| `PITCHER_MIDDLE_EV_MAX_MPH=80` (2863) | discrete EV cutoff removing P | (c) - stand-in for a reaction-window model | optional upstream fix (Stage 5c); acceptable to keep with a comment if deferred |
| `ANIM_TIME_SCALE`/`GROUND_TIME_SCALE=1.0` (3019/3152) | vestigial no-op scales | vestige | delete |
| `RUNNER_LEAD_MS=150` (3020) | runner jump after slide mount | (b) | keep, move into the presentation-beats block (Stage 3) |
| `OUT_BEAT_MS=400` (3021) | outs-choreography beat | (b) | keep as fallback-only beat |
| `FIELD_SEQUENCE_DELAY_MS=810` (3043) | wheel-sync hold | (b), out of layer | keep untouched |
| `THROW_DELAY_MS=60` (3046) | field-to-release transfer | (a) - a real transfer beat | keep; unify with `THROW_STAGGER_MS` into one `TRANSFER_MS` profile param; doubles as a reconciler knob's floor |
| `THROW_SPEED_MPH=90` / `THROW_DRAW_MS` (3058) | flat throw velocity | principled | keep flat (fact 0.2); primitive takes a throw-class param (`full`/`toss`) for the Task 3 stretch hook |
| `PITCH_SPEED_MPH`/`PITCH_TRAVEL_MS` (3068) | pitch flight | principled, out of layer | keep |
| `runnerDrawMsForFt` (3085) | flat runner-pace duration | (c) | fold into primitive (it's the unaccelerated twin of `fielderLegDurationsMs`) |
| `relayLegIsUnassisted` (3097) | jog-vs-throw per relay leg | (a) | keep |
| `THROW_STAGGER_MS=50` (3113) | catch-transfer between relay legs | (a) | merge with `THROW_DELAY_MS` -> `TRANSFER_MS` |
| `THROW_LEAD_MS=200` (3117) | test-only required margin | dead in prod | delete; the margin policy (Stage 3) becomes what the test asserts |
| `TAG_UP_MS=80` (3118) | tag-up reaction after catch | (a) | keep |
| `RN_OUT_WALK_MS=650` (3125) | walk-off duration | (b) | keep |
| `TAG_THROW_MARGIN_MS=400` (3132, incl. its 200->400 history) | never-contested tag-throw gap | (b) | fold into margin policy as the `uncontested` class value |
| `IDLE_DRIFT_*` (3855-3858) | idle-fielder lean | (b) | keep (already primitive-based via `accelDistForTimeS`) |
| `OF_SHIFT_ARCHETYPES`/`OF_SHIFT_WH_SINGLE_RESULTS`/`OF_SHIFT_DEG=5`/`OF_SHIFT_ANGLE_MIN/MAX` (3903-3911) | OF start-anchor angular fudge | (c) | superseded by Stage 5b (honest OF pursuit + reaction-sink); delete |
| `IF1B_DEPTH_SHIFT_FT=20` in `fielderStartAnchorFt` (3945) | 1B start-position fudge for PFP | (c) | superseded by Stage 5a (race-based coverage decision); delete |
| `OUTFIELDER_REACT_MS=400` (3964) | OF read-off-the-bat delay | (a) | keep as profile reaction; also the baseline for Stage 5b's hesitation sink |
| `OUTFIELDER_PACE_SCALE=1` (3978) | retired multiplier | vestige | delete |
| `FIELDER_ACCEL_FT_S2=25` + `accelTimeS`/`accelDistForTimeS` (3997) | real accel kinematics | principled | keep; becomes the shared primitive's core (Stage 1). **Update (gameday-animation-refinements round, probe 0.1):** no longer shared by every kind - infield charge/barehand keeps 25; runners and the fielder `"run"` kind now use `RUNNER_ACCEL_FT_S2=19.2` (Statcast home-to-first fit, n=507); OF pursuit uses its own `OF_PURSUIT_ACCEL_FT_S2=10.2` (see `OF_CHARGE_FT_PER_S` row above) |
| `fielderLegDurationsMs` (4023) | accel run over legs, per-player | principled | keep; re-based on primitive |
| `movingFielderTokenHtml` `deadlineMs` compression (4074-4082) | compress run to land on catch | reconcile one-off | fold: becomes the reconciler's `compressRun` knob, applied via the timing plan |
| `PITCHER_COVER_1B_LEGS/CONVERGE_RATE` (4116) | PFP curved path shape | (b) presentation | keep |
| `pitcherCover1BArrivalMs` (4151) | real arrival for the clamp | principled | keep; consumed by reconciler instead of ad-hoc clamp |
| `STEAL_COVER_POSITION` (4160) | who covers a steal | (a) | keep |
| `CAUGHT_IN_AIR` (4447) | air-out archetypes | (a) | keep |
| `TAG_THROW_ARCHETYPES` (4456) | "this throw never beats anyone" fact | (a) - true of the finite scenario set | keep; informs the margin-policy class, not its own timing path |
| `THROW_ORDER` (4460) | lead-runner-first priority | (a) | keep |
| `FORCED_OUT_BASE` (4470) | result-code -> out base | (a) | keep; `FCLead:"2B"` stays flagged unverified (open question for Alex, not for code) |
| `FORCE_TIMING_RESULTS` + `isForcedRunner` (4482/4492) | leaves-on-contact rule | (a) | keep |
| `BATTER_REACHES_FIRST`/`STRIKEOUT_RESULTS`/`WALK_RESULTS`/`BALK_RESULTS` (4503-4519) | result semantics | (a) | keep |
| `parseThrowOrder`/`throwOrderKeyForPosition`/`outThrowTargets` (4529-4618) | throw-target derivation (data-first) | (a) | keep unchanged |
| `coveringPosition`'s `angle===77 && relayCount===1 -> P` gate (4657) | PFP coverage as a lattice special case | (c) - encodes "1B can't get back" as magic angle | superseded by Stage 5a race decision; function keeps its signature for notation |
| `realOutThrowCount` (4683) | real-vs-decorative throw count | (a) | keep |
| `sequentialThrowSchedule`/`throwSchedule` (4832-4944) | the throw timeline | mechanism | re-based: becomes a consumer of the timing plan (Stage 3) |
| sac-fly `tagStart` backward-solve (4863-4884) | backward-solve special | fold | superseded by reconciler + `uncontested` margin class |
| pitcher-cover release clamp (4933-4942) | hold-ball one-off | fold | becomes the reconciler's `holdRelease` knob |
| `outThrowEndByBase`/`outAtMsFor` chain (4955/5485) | out-moment lookup | consumer | keep; reads the timing plan |
| `batterFirstArrivalMs` (4988) | flat batter-to-first time | (c) - flat, per-league | fold -> per-play `runnerArrivalMs(m, "BATTER", ...)` |
| `infieldSingleThrowHtml` + `IF1B_THROW_MARGIN_MS=150` (5071) | parallel loses-on-purpose throw | backward-solve special | fold: a `contested-safe` event in the shared schedule; function deleted |
| `STEAL_LEADOFF_FT=12` (5136) | runner lead-off | (a)-ish real behavior | keep |
| `STEAL_LEG_DUR_MS` (5142) | flat steal leg | (c) | fold -> per-runner via primitive |
| `STEAL_THROW_MARGIN_MIN/MAX_MS` + `stealThrowMarginMs` (5155-5159) | diff-scaled margin | **promote** | this IS the margin policy's template; generalize (Stage 3) |
| `CATCHER_POP_MS=250` (5164) | catch-and-release beat | (a) | keep |
| `stealThrowHtml`'s arrive floor/clamp (5247-5253) | verdict guarantee | fold | becomes a reconciler invariant, shared not bespoke |
| `sceneFieldHtml` `mvDelay`/`outAtMsFor`/`haloutLastMs` chain (5440-5900) | per-token timing assembly | consumer | simplify to read the timing plan; the delay-decision tree shrinks |
| `runnerOutMotionHtml` hold-at-bag-then-red path | runtime verdict fallback | (c) symptom | keep as a defensive floor, but the reconciler guarantees the throw beats the runner so it should become unreachable on out plays; add a console.warn if it ever fires (Stage 3) |

---

## 3. Task 3 - The honest per-player physical model

### 3.1 One shared race primitive (new, `docs/js/app.js`, placed near `accelTimeS`)

One motion model for everything that moves or flies:

```js
// A motion profile: how one actor covers ground.
//   { topSpeedFtPerS, accelFtPerS2, reactionS }
// Fielders and runners are accelerating profiles; a thrown/pitched ball is the
// degenerate profile { topSpeedFtPerS: v, accelFtPerS2: Infinity, reactionS: 0 }
// - same function, no special casing at call sites.
function arrivalTimeS(distFtOrLegs, profile) -> seconds        // scalar or per-leg
function legDurationsMs(legs, profile)      -> [ms, ...]       // momentum carries across legs (same contract fielderLegDurationsMs has today)
```

Profiles are built by small constructors so every current constant keeps its
meaning as a parameter, not a formula fork:

```js
function fielderProfile(m, pos, kind)   // kind: "charge" (16 base, FIELDER_ACCEL_FT_S2=25) | "pursuit" (OF, OF_PURSUIT_TOP_SPEED_FT_PER_S=26.4/OF_PURSUIT_ACCEL_FT_S2=10.2) | "run" (27 base, RUNNER_ACCEL_FT_S2=19.2) - all * spdPaceScale(fielderSpd(m,pos)), reaction CHARGE_REACTION_S or OUTFIELDER_REACT_MS/1000 per kind/pos [updated, gameday-animation-refinements probe 0.1: accel is per-kind, no longer one shared constant]
function runnerProfile(m, who)          // who: "BATTER" | "1B" | "2B" | "3B" -> spd from m.batter_spd / m.runners_on_base[who][2]; top speed RUNNER_SPRINT_FT_PER_S * spdPaceScale(spd); accel RUNNER_ACCEL_FT_S2=19.2 (updated, gameday-animation-refinements probe 0.1: no longer FIELDER_ACCEL_FT_S2); reaction 0 (presentation beats stay separate)
function throwProfile(throwClass)       // "full" -> THROW_SPEED_MPH; "toss" -> a lower TOSS_SPEED_MPH added later (hook only this pass - define the class enum, implement only "full")
```

Re-base existing math on it (behavior-preserving where flagged):

- `accelTimeS`/`accelDistForTimeS` become the primitive's internals (unchanged math).
- `fielderLegDurationsMs` -> `legDurationsMs(legs, fielderProfile(m,pos,"run"))`.
- `chargeFielderArriveS` -> `arrivalTimeS(distFt, fielderProfile(m,pos,"charge"|"pursuit"))`.
  **Deliberate behavior change:** the charge race gains acceleration (today it is
  flat-speed). This slightly lengthens short charges and is *more* honest; the
  16/24 constants become top speeds of the charge/pursuit profiles. Expect small
  shifts in `fieldedDistFt`/`groundTimeS` on charge plays; re-check the named
  regression plays (Section 7). If Alex prefers zero visual churn here, the
  charge profiles can pass `accelFtPerS2: Infinity` initially and flip on later -
  make that a one-line decision, not a design fork.
- `runnerDrawMsForFt` and `RUN_LEG_MS` -> `runnerLegMs(m, who, legs)` built on
  `legDurationsMs`. `RUN_LEG_MS` survives only as the league-average fallback
  (spd null / historical data without spd), which reproduces today's numbers
  exactly for a spd-3 runner minus the new acceleration ramp - see 3.2 risk.

### 3.2 Per-runner speed wiring (closes the verified asymmetry)

- New accessor `runnerSpd(m, who)`: `"BATTER"` -> `m.batter_spd`; a base key ->
  `m.runners_on_base && m.runners_on_base[who] && m.runners_on_base[who][2]`;
  null-safe -> `SPD_AVERAGE` (identical fallback convention to `fielderSpd`).
- Consumers to convert (complete inventory of `RUN_LEG_MS`/`STEAL_LEG_DUR_MS`
  reads): tag-throw runner arrival (4871-4872), `batterFirstArrivalMs` (4989),
  `sceneFieldHtml` token durations (5627, 5652), FC batter token (5838, 5841),
  batter out-to-first (5856), `wpRunnerArrival`-style block (6938), slideDwell
  worst-case budget (7341-7342), `STEAL_LEG_DUR_MS` (5142) and its uses
  (5172, 6938).
- `batterFirstArrivalMs()` becomes `batterFirstArrivalMs(m)` =
  `RUNNER_LEAD_MS + runnerLegMs(m, "BATTER", [90ft])`. Update the KMFlight
  export and both test call sites.
- **Risk (must handle, not hope):** runner durations are currently flat and a
  few CSS-side beats were tuned against them (`OUT_CHOREOGRAPHY_MS`'s hand-sync
  comment, the stranded token's fixed 1700ms at 5698). Safe runners already
  receive `--dur` per token and out tokens use generated keyframes
  (`runnerOutMotionHtml`), so per-play durations flow through - but audit
  `style.css` for any remaining keyframe that hardcodes the old 3333ms/1700ms
  assumptions before landing this stage.
- **Adding acceleration to runners changes every leg duration** (a spd-3 runner:
  90ft goes ~3333ms -> ~3873ms at accel 25). That is more honest (goal b) but
  re-times every play. Alternative: runners keep `accelFtPerS2: Infinity` this
  pass (durations unchanged for spd-3, only per-player top speed varies).
  **Recommendation: take the acceleration now** - Stage 3's reconciler is what
  makes re-timing safe, and doing it once avoids a second global re-tune later.
  Flag to Alex at review either way.
- **Data step:** rebuild the s01-s12 archives with the current
  `key_moments_build.py` so spd fields exist there (fact 0.1 caveat 2). If the
  archive build needs source data unavailable for old seasons, the JS fallback
  already degrades to league average - but then say so in the plan's close-out
  notes rather than leaving it implicit.

### 3.3 Throw model

- Keep `THROW_SPEED_MPH = 90` flat (no data - fact 0.2). All throw durations go
  through `arrivalTimeS(distFt, throwProfile("full"))`, which is numerically
  identical to today's `throwDrawMsForFt`.
- Define the `throwClass` enum and thread it through the schedule entries now
  (`{ base, startMs, endMs, drawMs, out, throwClass }`), defaulting `"full"`.
  The PFP underhand flip becomes a one-line `"toss"` classification later
  (short-distance + covering-fielder heuristics), with no parallel function -
  this is the required extensibility hook, deliberately not implemented.

---

## 4. Task 4 - The reconciliation mechanism

### 4.1 The timing plan (new pure function)

```js
// Compile the whole play's timeline once, before any HTML is built.
// Pure: (m, flight, moves) -> plan. No DOM, no CSS, exported to KMFlight.
function resolvePlayTiming(m, flight, moves) -> {
  fieldedMs,                       // from the existing resolvers, unchanged
  throws: [{ base, startMs, endMs, drawMs, out, throwClass, unassisted }],
  runners: { [who]: { startMs, legsMs, arrivalMs, outAtMs|null } },
  fielders: { [pos]: { legs, startMs, legsMs, deadlineMs|null } },
  adjustments: [{ knob, who, ms, reason }],   // every reconciliation, named - debuggability is a feature
}
```

`throwSchedule`, `outThrowEndByBase`, `sceneFieldHtml`'s `mvDelay` tree,
`fielderTokensHtml`'s deadline, and the steal timing all become *readers* of
this plan. `outThrowTargets`, `coveringPosition`, `deriveRunnerMoves`,
`FORCED_OUT_BASE` etc. keep supplying the *facts*; the plan owns the *clock*.

### 4.2 The forward pass

For each contested event (a throw leg racing a runner to a base; a pursuit
racing a hang time; a steal throw racing a steal), compute honest times from
the primitives: fielded moment (existing resolvers), transfer beats
(`TRANSFER_MS`), throw legs at real distance/speed, runner arrivals at
per-runner rates, covering-fielder arrivals (`pitcherCover1BArrivalMs`
generalized: any receiving fielder's real arrival at the bag).

### 4.3 The verdict + margin policy - "how close should this look"

One exported table + one function, replacing every scattered margin constant:

```js
// Closeness classes, one per kind of contested event:
//   forceOut     - throw must beat runner        (GO, DP legs, FC family)
//   tagOut       - throw must beat runner        (CS family; tag not force - same timing contract)
//   contestedSafe- throw must LOSE to runner     (infield single, safe steal)
//   uncontested  - throw is decorative, runner comfortably safe (SacF/DSacF/FO tag throws)
var MARGIN_POLICY = {
  forceOut:      { minMs: 150, maxMs: 450 },
  tagOut:        { minMs: 150, maxMs: 450 },
  contestedSafe: { minMs: 150, maxMs: 450 },
  uncontested:   { minMs: 400, maxMs: 600 },
};
function targetMarginMs(cls, diff) // linear in |diff|/500 within [min,max], exactly stealThrowMarginMs' shape
```

- **Data grounding:** `|diff|` is the league's own decisiveness number -
  `steal_num`/`throw_num` diff for steals (already wired), `m.diff` (pitch/swing)
  for batted plays. A decisive roll reads as a decisive play; a near-tie reads
  bang-bang. This is the honest answer to "is there real data" - it exists and
  one mechanism already uses it. Acknowledge the caveat in a comment: `m.diff`
  also drove EV/LA upstream, so it is a proxy for play closeness, not a measured
  bag margin - but it is the only league-native number with the right shape, and
  it beats a hand-authored per-result table on both consistency and provenance.
  (`TAG_THROW_MARGIN_MS`'s 200->400 history and `STEAL_THROW_MARGIN_*` carry
  straight into the table's values - the values are kept, their *scatter* is
  what dies.)
- The four current backward-solves map onto classes: sac-fly `tagStart` ->
  `uncontested`; `infieldSingleThrowHtml` -> `contestedSafe` at 1B;
  `stealThrowHtml` -> `tagOut`/`contestedSafe`; grounder outs (previously
  unenforced!) -> `forceOut`.

### 4.4 The reconciler - one ordered, bounded knob set

For each contested event: `deltaMs = requiredArrival - honestArrival` where
`requiredArrival = runnerArrival - targetMargin` (out verdicts) or
`runnerArrival + targetMargin` (safe verdicts). Close `deltaMs` by walking a
fixed knob order, each knob bounded, each application recorded in
`plan.adjustments`:

Throw must land LATER (honest throw too early - play would read uncontested
when it should be close, or out when verdict is safe):
1. `holdRelease` - fielder holds the ball before throwing (raise start, same
   draw). Physically: setting feet, double-clutch. Bound: generous (2000ms) -
   holding a ball is always plausible; this is today's pitcher-cover clamp and
   `tagStart`, generalized.

Throw must land EARLIER (honest throw loses a race the verdict says it won):
1. `quickRelease` - shrink the transfer beat toward a floor (TRANSFER_MS -> 0).
2. `runnerLateJump` - add a bounded reaction delay to the *runner's* start
   (<= ~400ms; a late read off contact is real baseball). Never touches their
   speed - attribute uniqueness (goal c) survives.
3. `compressRun` - scale the *fielder's* leg durations down to meet the fielded
   moment (existing `deadlineMs` mechanism, now driven from the plan). Applies
   to fielder runs, not the ball.
4. `stretchRunner` - last resort, bounded (<= ~15% slower): scale the runner's
   leg durations up. Crosses goal (b) deliberately and only after 1-3; if it
   ever fires beyond its bound, that is by definition an upstream input problem -
   log it (console.warn + adjustment record) so it surfaces in testing instead
   of being silently absorbed. Goal (a) is still never at risk: the runner's
   `outAtMs` and token choreography always follow the verdict regardless.
5. The runtime `runnerOutMotionHtml` hold-at-bag fallback stays as the absolute
   floor beneath all of this (it is what makes (a) literally unbreakable), but
   Stage 3's invariant tests should make it unreachable for in-policy plays.

N-leg relays: reconcile the **final** leg's landing against the verdict, then
propagate backward through the chain (`start[i] = end[i-1] + TRANSFER_MS` holds
by construction; `holdRelease` on leg 0 shifts the whole chain). Unassisted
legs keep their fielder-pace durations (they go through the same primitive).

Steals: `stealThrowHtml`'s ideal-arrival/floor/clamp logic is re-expressed as
this exact pipeline (forward: catcher release after pitch + `CATCHER_POP_MS`;
verdict class `tagOut`/`contestedSafe`; knobs: `holdRelease`/`quickRelease`).
Numerically it should reproduce today's behavior; the point is one mechanism.

---

## 5. Task 5 - Upstream fixes for every (c) item

### 5a. PFP / `IF1B_DEPTH_SHIFT_FT` -> a real coverage decision

Replace the rendered-anchor fudge with the assignment layer knowing the
constraint:

1. New `firstBaseCoverage(m, flight)`: using the primitives - 1B's real return
   time from their **true anchor** (or, more honestly, from their charge
   endpoint at `fieldedPoint`) to the 1B bag, vs. `batterArrival` at first.
   If 1B genuinely can't make it back with at least the play's target margin,
   the pitcher covers (his own `pitcherCover1BArrivalMs` must also make it -
   the reconciler's `holdRelease` already handles the throw side).
2. `coveringPosition`'s `angle===77 && relayCount===1` magic gate delegates to
   this function (keep the signature; notation and tokens keep working).
3. Delete `IF1B_DEPTH_SHIFT_FT` and the 1B branch of `fielderStartAnchorFt` -
   1B starts at his real anchor. The visible "reason the pitcher covers" is now
   the *true* geometry: 1B charging the ball genuinely can't get back, which is
   exactly what the race computes. If on real data the race says 1B *can* get
   back at his canonical depth on the 77-degree play (i.e. today's shift was
   load-bearing for the story, not just the picture), that is a genuine model
   conflict: prefer fixing `INFIELDER_DEPTH_FT["1B"]`/anchor realism over
   reintroducing a render-only shift, and surface it to Alex with the measured
   numbers rather than silently picking. **This is the one place Stage 5 may
   need a real judgment call.**

### 5b. OF shift (`OF_SHIFT_DEG` et al.) -> honest pursuit + a reaction-time sink

Recommended direction (combining the prompt's candidates 1 and 3; rejecting
"keep the shift, derive its size honestly" as still a render-layer fudge):

1. **Run the honest race on every ball that must fall** (double, triple,
   1BWH/1BWH2 singles - every current `OF_SHIFT_ARCHETYPES`/`OF_SHIFT_WH_SINGLE_RESULTS`
   member): nearest outfielder from **true anchor**, `pursuit` profile (24 base
   * their spd, accel, `OUTFIELDER_REACT_MS`), against the ball's real hang time
   + landing point.
2. If the honest pursuit **doesn't** reach the landing point in time: nothing to
   do - real anchors, real speeds, no fudge at all. (Verify with a probe sweep
   how often this already suffices - deep doubles/triples likely pass untouched.)
3. If it **would** reach it (the result says it must not): spend the deficit on
   a named `readDelay` adjustment - a late break, exactly the deficit plus the
   play's target margin, on top of `OUTFIELDER_REACT_MS`. The fielder visibly
   hesitates, breaks late, and arrives just after the bounce - the standard
   real-baseball story for a ball dropping in front of a fielder. Bound it
   (~1500ms of total delay is still a believable bad read).
4. Only if the required hesitation exceeds the bound, fall back to a *derived*
   positional shade: the minimum angular offset (per this fielder's real speed
   and this ball's real geometry) that makes the honest race fail within the
   hesitation bound - i.e. the old shift, but computed per play, applied as a
   pre-pitch defensive-alignment choice (which is what a shade actually is),
   and only when physics forces it.
5. Delete `OF_SHIFT_DEG`, `OF_SHIFT_ARCHETYPES`, `OF_SHIFT_WH_SINGLE_RESULTS`,
   `OF_SHIFT_ANGLE_MIN/MAX` and the OF branch of `fielderStartAnchorFt`; keep
   `fielderStartAnchorFt` itself as the (now nearly pass-through) anchor
   accessor, since Stage 5b step 4 still needs a hook.
   Covers **every** current shift archetype, per the prompt's requirement.

### 5c. `PITCHER_MIDDLE_EV_MAX_MPH` -> reaction-window model (optional, small)

The honest version already almost exists: the pitcher is excluded when the
ball's arrival time at the mound (`gp.timeAt` at ~60ft) is under a human
reaction floor (~0.30-0.35s tuned to preserve today's 80mph boundary). Replaces
a discrete EV cutoff with the physical quantity it stood in for; keep the
handedness-based reassignment logic unchanged. Low risk, do last, skippable
without harming the rest.

---

## 6. Task 6 - Test plan (`ball_flight_test.py`)

Existing coverage (fact 0.8) to **update**:
- The A4 throw-beats-runner sweep: replace `THROW_LEAD_MS` with the margin
  policy - assert every out-class throw lands in
  `[runnerArrival - targetMarginMs(cls, diff) - tol, runnerArrival - MARGIN_POLICY[cls].minMs + tol]`.
- The tag-throw assertion: same form with the `uncontested` class.
- `batterFirstArrivalMs` call sites gain the `m` argument.

New pure-function coverage (all via existing `KMFlight` export pattern):
1. **Primitive:** `arrivalTimeS` closed-form checks (short leg inside accel
   phase, long leg past it, `Infinity` accel degenerates to distance/speed,
   multi-leg momentum carry equals single-run total); profile constructors
   (spd 1/3/5 scaling, null-spd fallback = league average, pitcher charge
   exception preserved).
2. **Per-runner wiring:** `runnerSpd` resolution from `batter_spd` /
   `runners_on_base`; a spd-5 runner's leg strictly faster than spd-1; missing
   fields reproduce league-average timing exactly.
3. **Reconciler invariants, swept over the full situation list** (reuse the
   existing sweep harness that iterates result codes/diff bands):
   - verdict: every event's final schedule satisfies its class's verdict
     direction with at least `minMs` margin - **no exceptions, any result code**;
   - no negative times: every `startMs >= fieldedMs` floor respected, no
     negative draw/leg duration, relay `start[i] >= end[i-1]`;
   - monotone closeness: for fixed play shape, larger `|diff|` never yields a
     smaller margin;
   - bounded knobs: `stretchRunner` never exceeds its cap in the sweep (if it
     does, the play's inputs are wrong - fail loudly);
   - steals reproduce: safe always reads safe, caught always caught, for all
     `steal_num`/`throw_num` extremes including the slow-wheel floor case.
4. **Stage 5:** PFP - 77-degree grounder yields P covering with arrival before
   throw landing; 85-degree yields 1B unassisted; 3-6-3 relay still never has P
   cover the return leg. OF - for each former shift archetype, honest pursuit
   never arrives before landing once `readDelay` is applied; `readDelay` bound
   respected; anchors are the true `FIELDER_ANCHORS_FT`.
5. **Regression pin:** before starting, capture current `throwSchedule` /
   `fieldedMs` / arrival outputs for the named regression plays (Section 7)
   and diff after each stage - intentional changes get asserted new values,
   everything else must hold.

---

## 7. Staging for execution

Land as separate commits, in this order; each stage leaves the app working.

- **Stage 1 - primitive.** Add `arrivalTimeS`/`legDurationsMs`/profiles;
  re-base `fielderLegDurationsMs`, `chargeFielderArriveS`, `runnerDrawMsForFt`.
  Charge-race acceleration decision per 3.1. Tests 6.1.
- **Stage 2 - per-runner speed.** `runnerSpd`, `runnerLegMs`, convert the
  inventoried `RUN_LEG_MS`/`STEAL_LEG_DUR_MS` consumers, `batterFirstArrivalMs(m)`,
  runner-acceleration decision per 3.2, `style.css` audit, archive rebuild.
  Tests 6.2.
- **Stage 3 - timing plan + reconciler + margin policy.** `resolvePlayTiming`,
  `MARGIN_POLICY`/`targetMarginMs`, convert `throwSchedule`(+tag branch),
  `infieldSingleThrowHtml` (delete), steal timing, `deadlineMs`, pitcher-cover
  clamp onto it; delete `THROW_LEAD_MS`, `TAG_THROW_MARGIN_MS`,
  `IF1B_THROW_MARGIN_MS`, `STEAL_THROW_MARGIN_*` (values move into the table).
  Tests 6.3.
- **Stage 4 - consumer simplification.** `sceneFieldHtml`/`fielderTokensHtml`/
  `scorebugOutsHtml` read the plan; `mvDelay` tree shrinks; hold-at-bag warn.
- **Stage 5 - upstream fixes.** 5a PFP, 5b OF shift, 5c pitcher window (optional).
  Tests 6.4.
- **Stage 6 - test updates** land inside each stage above, not at the end.

**Named regression plays** (from the code's own provenance comments - spot-check
visually after Stages 1-3 and 5): the Avant comebacker (session 4, POR@RLY, top
3 - charge-in); Trotter's slow chopper (session 2, BBEG@POR - camped-fielder
race); the 76mph/18-degree/207ft RF single (OF charge split); the Calvin Huff
dead-center double (OF shift direction - becomes a 5b case); a PFP 3-1
grounder at angle 77; a 3-6-3 and a 6-4-3 double play (relay chains); a sac fly
with a tagging runner; a bang-bang and a decisive steal each way (SB/CS); an
infield single; LODP retreat.

---

## 8. Open questions and risks for Alex / the implementer

1. **Runner acceleration now vs. later** (3.2): re-times every play (~16%
   longer per leg at spd-3). Recommended yes; needs Alex's eyes on the result.
2. **Charge-race acceleration** (3.1): small fielded-time shifts on charge
   plays. Recommended yes; one-line revert available.
3. **`stretchRunner` bound** (4.4): the honest-model-can't-get-there escape
   valve. The bound (~15%) is a taste call; anything hitting it in the sweep is
   an upstream bug by definition - decide whether the sweep *fails* or *warns*.
4. **5a judgment call:** if the race says a canonical-depth 1B *can* get back on
   the 77-degree play, the PFP story needs a real input change (1B depth/anchor)
   - measure first, then ask Alex, do not silently re-fudge.
5. **`m.diff` as the closeness driver** (4.3): double-uses a number that already
   drove contact quality. Accepted deliberately (only league-native signal with
   the right shape); if Alex would rather closeness stay authored, swap
   `targetMarginMs`'s diff term for a per-result-class constant table - the
   policy's *structure* is unchanged either way, which is the actual win.
6. **Archive rebuild** (3.2): confirm the s01-s12 build inputs still exist so
   spd fields can be backfilled; otherwise historical seasons stay league-average
   (acceptable, but say so).
7. **`FCLead: "2B"`** stays an unverified default (no diff-band row exists) -
   carried forward, flagged, not resolvable from code.

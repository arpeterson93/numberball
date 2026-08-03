# Ball flight, fielding and Play Scene - refinement round - implementation plan

## Why

`ball-flight-plan.md` shipped. Alex has now watched the feature run against real
plays and produced a list of fixes, calibration corrections and small additions.
This plan turns that list into staged work for Sonnet.

Everything here lives in the static-site layer (`docs/js/app.js`,
`docs/css/style.css`) plus two backend touch-ups (`key_moments_build.py`,
`ball_flight_archetypes.csv`). No Streamlit UI changes.

Project constraints that still apply:
- No em dashes in any `.py` file - hyphens only.
- No Co-Authored-By trailer if committing.
- Every new animated piece extends the existing `prefers-reduced-motion` block.
- The Play Scene has **no JS timers**: every animation is CSS `@keyframes` +
  `animation-delay`, driven by inline custom properties. Nothing in this plan
  may introduce a `setTimeout` into the scene.

### Ground-truth invariant (unchanged, still the single most important rule)

`result`, `obc_before`, `obc_after`, `runs`, `outs_before`, `outs_after` are
inputs. Nothing here may change, override or contradict them. Every decision
below changes only **how** an outcome is staged, never **what** the outcome is.

---

## Verified findings

Checked against the actual working tree, not read-only inference. Where a
finding in `ball-flight-refinements-opus-prompt.md` was wrong, it is corrected
here and the correction is called out explicitly.

### F1. `outThrowTarget()` phantom-throw bug: confirmed, and bigger than stated

`outThrowTarget` (`app.js:1237-1246`) falls through to `return "1B"` whenever
there is no OUT move and the batter did not reach. Traced across the whole feed
(1174 plays), the plays that hit that branch are:

| Result | Shape | Count | Current throw | Correct? |
|---|---|---|---|---|
| `GO` | `000 -> 000` | 170 | `1B` | yes |
| `FO` | `000 -> 000` | 63 | `1B` | **no - phantom** |
| `PO` | `000 -> 000` | 32 | `1B` | **no - phantom** |
| `FO` | `010 -> 010` | 11 | `1B` | **no - phantom** |
| `PO` | `001 -> 001` | 9 | `1B` | **no - phantom** |
| `SacF` | `101 -> 001`, runs=1 | 3 | `1B` | **no - should be HOME or nothing** |
| `FC` | `001 -> 001` | 17 | `1B` | **no - should be 2B** |
| `FC3rd` | `011 -> 011` | 2 | `1B` | **no - should be 3B** |
| `FCH` | `111 -> 111` | 1 | `1B` | **no - should be HOME** |

Strikeouts never reach it: `throwHtml` returns `""` when `flight` is null
(`app.js:1249`), and `K` is in `meta.flight.excluded`.

### F2. **Correction to the prompt (A1):** a sac fly has *no* OUT-bound move

The prompt states "a sac fly ... is unaffected by this change since it already
has an OUT-bound move." It does not. `SacF` `101 -> 001` with `runs=1` produces
`deriveRunnerMoves` output `[3B->HOME, 1B->1B]` - zero OUT moves. The batter is
the out, and the batter is invisible to `deriveRunnerMoves`. A naive A1 fix
would therefore silently delete the throw on every sac fly. Decision A1 below
handles it explicitly.

### F3. **Correction to the prompt (A2):** the common double play yields only *one* OUT move

The prompt's A2 assumes multi-throw choreography falls out of "give every OUT
move its own throw." It does not, for the majority case:

| Result | Shape | Count | OUT moves | Outs recorded |
|---|---|---|---|---|
| `DP` | `001 -> 000` | 16 | 1 (`1B->OUT`) | 2 |
| `DP` | `101 -> 000` | 2 | 2 | 2 |
| `DPH1` | `111 -> 110` | 2 | 1 (`1B->OUT`) | 2 |
| `LODP` | `010 -> 000` | 2 | 1 (`2B->OUT`) | 2 |

The missing out is always the **batter**, who has no move. So A2 needs a second
ingredient: `unaccountedOuts = (outs_after - outs_before) - outMoves.length`.
That value is pure ground truth and needs no new data. With it, the standard
`DP` gets exactly the two throws Alex asked for (force at 2B, then 1B).

### F4. The `DPH1`/`FCH` `"HOME"` special-case is currently dead code for `FCH`

`app.js:1240` sits *inside* the `if (runnerOut)` branch. `FCH` produces no OUT
move (F1's table), so that branch never runs for it. The special-case only ever
fires for `DPH1`. This must move above the OUT-move logic.

### F5. The FC family produces no runner motion at all

`FC` (`001 -> 001`), `FC3rd` (`011 -> 011`), `FCH` (`111 -> 111`) all pair
before/after like-for-like, producing moves such as `[1B->1B]` - nobody moves,
no batter token, no out token. The slide renders a completely static field. The
throw target is only part of the problem; see Decision A3 for what is cheap to
fix and what is not.

### F6. `FCLead` and `LOTP` do not exist in the shipped data

`compute_result_diff_bands.py:42` maps `FCLead` to `grounder`, but
`result_diff_bands.csv` has no `FCLead` row and `meta.flight.bands` has no
`FCLead` key, so `flightParams` returns `null` for it and no flight, and no
throw, renders at all. Same for `LOTP` (in `RESULT_LABELS` and
`TRIPLE_PLAY_CODES`, absent from the bands table and from the feed). Any A3/B6
work on those two codes is currently unreachable; build it anyway (it is two
lines) but do not spend validation effort on it.

### F7. **`CS3` is in the feed and is not in `FLIGHT_EXCLUDED`**

`CS3` appears once in `docs/data/plays_03.json`. `FLIGHT_EXCLUDED`
(`key_moments_build.py:151-154`) lists `CS`/`CS2` but not `CS3`/`CS4`/`SB3`/`SB4`.
It only avoids rendering a flight by accident - it has no bands row. This is a
latent bug: add a band row for `CS3` (a future regeneration might) and it
starts flying a batted ball on a caught stealing. Fix the list.

### F8. Batter token on steals: confirmed, and worse than described

For `SB2` (`001 -> 010`, 30 plays), `deriveRunnerMoves` yields `[1B->2B]`,
`batterReached` is false, `flight` is null, so `sceneFieldHtml`'s 6c branch
(`app.js:1325-1333`) renders a batter token walking from home to the dugout -
on a play with **zero outs**. `CS2`/`CS3` do the same on top of the (correct)
runner out-walk. Confirms B2.

### F9. **The strikeout "starts in the dugout" bug is a CSS fill-mode bug, and it is not limited to strikeouts**

`style.css:589-591`:

```css
.rn.out-walk, .rn.out-to-first{animation-fill-mode:forwards;...}
.rn.out-walk{animation-name:rnOutWalk;animation-duration:650ms;
             animation-delay:calc(var(--rdelay,0s) + 250ms);}
```

`.rn`'s base rule (`style.css:531`) places the token at `--tx/--ty`, i.e. the
**dugout**. With `fill-mode: forwards` and a non-zero delay, the element shows
its *base* position for the whole delay, then snaps to the `from` keyframe. On a
strikeout `--rdelay` is `OUT_BEAT_MS` = 400ms, so the batter sits in the dugout
for 650ms, teleports to home plate, and walks back out. That is exactly Item 19.

The same bug is present, less visibly, on:
- `.rn.out-to-first` (delay 150ms - batter blinks at the dugout before starting),
- `.rn.legs1`-`.rn.legs4` (`style.css:539-542`, delay 150ms - every safe runner
  sits on their destination base for 150ms, then snaps back to their origin).

One-word fix: `forwards` -> `both` on both rule groups.

### F10. Groundout throw timing: the runner beats the throw by 100ms

Measured from the current constants:

| Piece | Constant | Time |
|---|---|---|
| Ball rolls to the fielder | `GROUNDER_ROLL_MS` = 600 | 0 -> 600ms |
| Throw begins | `+ THROW_DELAY_MS` = 150 | 750ms |
| Throw completes | `throwDraw` 300ms (`style.css:523`) | **1050ms** |
| Batter starts | `RUNNER_LEAD_MS` = 150 | 150ms |
| Batter reaches 1B | 47.06% of 1700ms (`style.css:598`) | **950ms** |

The runner arrives 100ms before the ball. Confirms Item 11 and gives A4 an
exact target.

### F11. Sac fly runner leads off before the catch: confirmed, and cheap to fix

`runDelay` (`app.js:1278`) is a single play-wide `RUNNER_LEAD_MS` = 150ms, and
`ballTravelMs` for a fly ball is 450-1400ms. So the tagging runner leaves 300ms
to 1250ms before the catch. **Contrary to the prompt's "harder of the timing
items" note, this is a small change**: `--rdelay` is already written per-token
inside `moves.map` (`app.js:1295`), so making the delay per-move is a
one-expression edit, not new plumbing.

### F12. `throwHtml` originates at the landing point, not the fielder anchor

`app.js:1254`: `var from = ftToSvg(flight.x, flight.y);`. **This resolves the
prompt's open question 8**: removing the fielder-converge token (B1) has no
effect on the throw's visual origin. B1 and A2 are independent and B1 can land
first.

### F13. `RESULT_LABELS` values are far too long for an on-field marker

Live values include `"Fielder's Choice at Third"`, `"Groundout, Runner
Advances"`, `"Lineout Double Play"`, `"Home Run"`. C2 needs a second, terser
table. Note the raw result codes are already terse for most cases.

### F14. **E1 needs no code change - `signedCirc` already implements Alex's rule**

This is the most important correction in this document.

Alex's rule: at the wheel's exact halfway point, anchor on `pitch`'s digit and
pick the direction that does not cross the wrap boundary.

`signedCirc(a, b, mod)` computes `d = b - a`, then wraps only when
`|d| > mod/2` - strictly greater. At the tie `|d| === mod/2`, `d` is left as
`b - a`. And for `a`, `b` both in `0..mod-1`, `b > a` is *exactly* the condition
"the forward path from `a` to `b` does not wrap", while `b < a` is exactly "the
backward path does not wrap." So `b - a` **is** the anchor-and-no-wrap rule.

Verified exhaustively over every tie pair on both wheels (all 10 pairs at
mod 10, all 100 at mod 100): zero disagreements.

Alex's worked example already passes today:

```
signedCirc(3, 8, 10)  === +5     signedCirc(8, 3, 10)  === -5
signedCirc(30, 80, 100) === +50  signedCirc(80, 30, 100) === -50
signedCirc(0, 5, 10)  === +5     signedCirc(5, 0, 10)  === -5
```

The last pair is the assertion `ball-flight-plan.md` Stage 3b called a "known
artifact ... accepted ... must not be fixed." **It is not superseded - it is the
same rule.** Do not change `signedCirc`. What E1 actually buys is a *reframing*
of the plan's comment plus four new regression assertions locking the behaviour
in. Sonnet must not "implement" a tie-break that already exists.

Scale note, so this is not dismissed as trivial: the tie fires on **8.7% of
plays** (98 of 1129) on the ones-digit wheel and 1.4% on the two-digit wheel. It
is worth having assertions on.

### F15. Singles landing in the infield: confirmed, quantified

Landing point tested against the **rendered infield dirt polygon** (the
home/1B/2B/3B diamond, i.e. `|x| + |y - 63.64| <= 63.64` in field feet):

| Result | n | Lands on the dirt | Median distance |
|---|---|---|---|
| `1B` | 173 | 86 (**50%**) | 109 ft |
| `1BWH` | 32 | 9 (28%) | 134 ft |
| `1BWH2` | 1 | 1 | 80 ft |
| `IF1B` | 14 | 14 (100%) - correct, that is what it is | 56 ft |
| `GO` | 258 | 133 (52%) - correct for a grounder | 104 ft |
| `PO` | 55 | 42 (76%) - correct for a pop-up | 81 ft |
| `FO` | 99 | 0 | 326 ft |

Half of all singles land on the infield dirt. Recalibration target confirmed;
see Decision C5 for the tested replacement band.

Related: the `single` archetype's LA band (`-5..10`) puts every real single
between **0.2 and 4.9 degrees**, straddling `GROUND_LA_THRESHOLD` (4), so most
singles are staged as grounders.

### F16. Home runs: 23% are staged as inside-the-park

On real feed data (39 home runs), 30 clear the uniform 375ft fence and **9 (23%)
land inside it**. `ball-flight-plan.md` Open Question 1b budgeted ~0.9% and the
uniform-fence comment predicted "meaningfully higher" - 23% is far past
"meaningfully." An inside-the-park home run on nearly one in four home runs
reads as a broken animation. This is Alex's dial (`FENCE_DEPTH_FT` or the
`home_run` depth band); flagged, with a proposed value in C6.

The `ball_flight_test.py` sweep's "4366 inside / 177 over" is **not** evidence
of a 96% rate - that sweep walks a uniform `(pitch, swing)` grid, where almost
every pair has a large `diff` that a real home run never has. Only the real-feed
number (23%) is meaningful.

### F17. Launch angle is effectively a constant for every good-contact result

Real LA spread per result, from the shipped tables:

| Result | n | LA range today | Archetype band |
|---|---|---|---|
| `HR` | 39 | **29.8 - 30.5** | 25-35 |
| `3B` | 15 | **22.1 - 22.9** | 15-30 |
| `2B` | 59 | **17.6 - 19.1** | 12-25 |
| `1B` | 173 | 0.2 - 4.9 | -5-10 |
| `FO` | 99 | 31.0 - 44.2 | 25-50 |
| `GO` | 258 | -12.9 - 6.4 | -15-8 |

`dLA = signedCirc(firstTwo(pitch), firstTwo(swing), 100)`, and a home run
requires `diff <= ~21`, which forces the two-digit components within ~2 of each
other. So every home run launches at 30.0 +- 0.3 degrees. This is the decisive
input to Decision E3.

### F18. E2's premise does not hold on the data (or in Alex's own example)

Alex's example: pitch 830, swing 770. `firstTwo(830) = 82`,
`firstTwo(770) = 76`, `dLA = signedCirc(82, 76, 100) = -6` - already negative.
The full signed delta `signedCirc(830, 770, 1000) = -60` is also negative. **The
two agree**, so the example does not demonstrate the problem it was offered for.
Alex's description assumed a "3 -> 7" single-digit read, which is `onesDigit`,
the horizontal axis, not `dLA`.

Across the whole feed, `sign(dLA)` and `sign(signedCirc(pitch, swing, 1000))`
disagree on **2 of 818 in-scope plays (0.2%)**. See Decision E2.

### F19. No test files have ever been committed to this repo

`git log --all --name-only` returns zero files matching `*test*`. The suites
`ball-flight-plan.md`'s validation section names (`catchup_test.py`,
`p23_test.py`, `polish_test.py`, `cursor_cap_test.py`, `nav_test.py`) do not
exist and never did - they were transient scratch harnesses. The **only** test
file in the tree is `ball_flight_test.py` (untracked, and it currently passes in
full against the working tree, verified by running it). Plan test work
accordingly: there are no "existing nav tests" to consult about swipe boundary
behaviour (see G1), and `ball_flight_test.py` is the one file to extend.

Note it needs the system Python, not `.venv` - Playwright is installed globally
(`C:\Users\Alex\AppData\Local\Programs\Python\Python310\python.exe`), not in the
project venv.

### F20. Swipe at a boundary is a dead end today

`stepCatchUp` (`app.js:2071-2077`) and `stepReplay` (`app.js:2253-2259`) both
`return` when the target index is out of range, so a swipe past either end does
nothing at all. `syncNav` (`app.js:2014-2019`) disables the matching arrow
button. Confirms the premise of G1.

### F21. Everything else in the prompt's findings list checked out

- `stateStack` (`app.js:370-384`) has `m.inning` and `m.half`. **No ordinal
  formatter exists anywhere in `app.js`** (searched) - I2 needs a new one.
  Also note `stateStack` is shared with `scoreboardCard`, but
  `key_moments_build.py:818` always emits `is_half_inning_final: False` for
  scoreboard games, so I2 only ever affects main-feed cards.
- Replay title slide (`app.js:1891-1904`) and `.catchup-title-teams`
  (`style.css:453-456`, `flex-direction` defaulted to row) - confirmed. The
  class is shared by Catch Me Up's per-game title slide *and* its "all caught
  up" card, so a pure-CSS change hits all three (see I3).
- `viewerZoneAbbr` (`app.js:106-110`) has exactly one consumer,
  `formatMomentTime` (`app.js:119-121`), which itself has exactly one call site,
  `card()` (`app.js:436`). I4 is a three-line change.
- Parenthesis sites: `app.js:667` ("Filters (N active)") and `app.js:856`
  ("Catch Me Up (N new plays)") both confirmed. I found **two more** user-facing
  ones: `app.js:1825` (`" (LI " + leverage + ")"` in the replay recap) and
  `app.js:349` (`"58% (+3.2)"` in the card's WP fragment). Plus
  `formatBuiltAt`'s `"day(s) ago"` (`app.js:133`).
- 3rd-out badge (`app.js:1684`, `style.css:743-746`) - confirmed, and
  `.out-third` already reuses the `outPop` keyframes, so I2's dot swap inherits
  the animation for free.
- `.dm-base.on` gold fill (`app.js:1338-1345`, `style.css:481`) - confirmed
  independent of `.rn` tokens.
- `fielderTokensHtml` (`app.js:1216-1228`) - confirmed working as designed.
- `.out-to-first` (`app.js:1313-1324`) already does Item 12 for the batter;
  Item 27 is the same treatment for other out-bound runners.
- `ballFlightHtml` (`app.js:1195-1214`) carries `.air`/`.ground` and
  `.clear`/`.land` and nothing out-vs-hit.

---

## Naming / new and changed symbols

| Thing | Name | Where |
|---|---|---|
| Ordinal formatter | `ordinal(n)` | `app.js`, near `pad2` |
| Multi-throw target list | `outThrowTargets(m, moves, flight)` (replaces `outThrowTarget`) | `app.js` |
| Throw schedule (pure, testable) | `throwSchedule(m, moves, flight)` -> `[{base, startMs, endMs}]` | `app.js` |
| Batter arrival at 1B (pure) | `batterFirstArrivalMs(flight)` | `app.js` |
| Caught-in-air archetype set | `CAUGHT_IN_AIR = {fly_ball, pop_up, line_drive}` | `app.js` |
| Result -> forced out base overrides | `FORCED_OUT_BASE` | `app.js` |
| Lead-runner throw order | `THROW_ORDER = ["HOME","3B","2B","1B"]` | `app.js` |
| Plays with no plate appearance | `meta.flight.no_pa` (new wire field) | `key_moments_build.py` |
| Terse on-field result labels | `RESULT_SHORT` -> `meta.result_short` | `key_moments_build.py` |
| Fielder-token feature flag | `SHOW_FIELDER_TOKENS = false` | `app.js` |
| Radial field scale (Decision D) | `radialSvg(rFt)` inside `ftToSvg` | `app.js` |
| Pitch/swing wheel readout | `sceneWheelHtml(m)` | `app.js` |
| New throw timing constants | `THROW_DRAW_MS`, `THROW_STAGGER_MS`, `THROW_LEAD_MS` | `app.js` |
| Rollout | `ROLLOUT_FT`, `ROLLOUT_MS` | `app.js` |
| Reused, untouched | `signedCirc`, `deriveRunnerMoves`, `basepathWaypoints`, `flightParams`'s math, `clampToFence`, `nearestFielder`, the meter, the ribbon | - |

---

## Stage A - throw logic and out attribution

All of Stage A lands in one rewritten block replacing `outThrowTarget` +
`throwHtml` (`app.js:1230-1259`). Build it as one unit; the pieces are not
separable.

### A1. No throw on a caught fly ball or pop-up

```js
var CAUGHT_IN_AIR = { fly_ball: 1, pop_up: 1, line_drive: 1 };
```

Branch on `flight.archetype`, not on `flight.isGrounder` - `isGrounder` is an
LA threshold and would misclassify a low line drive. When the archetype is
caught-in-air and there is no OUT-bound move, emit no throw.

**Explicit sac-fly carve-out (per F2).** A caught-in-air play with at least one
`mv.scored === true` move gets exactly one throw, to `HOME` - the tag-up throw,
which is the whole drama of a sac fly. This is my proposal, not a relayed
decision: the prompt assumed sac flies were already covered and they are not.
Affects `SacF` (5), `DSacF`, `DFO` in the current feed.

### A2. One throw per out, in lead-runner order

```js
var THROW_ORDER = ["HOME", "3B", "2B", "1B"];

function outThrowTargets(m, moves, flight) {
  if (!flight || flight.clearedFence) return [];
  var caught = !!CAUGHT_IN_AIR[flight.archetype];
  var targets = [];

  // 1. Result-code override wins outright (A3/B6) - the code names the base.
  var forced = FORCED_OUT_BASE[m.result];

  // 2. One target per OUT-bound move.
  moves.forEach(function (mv) {
    if (mv.to !== "OUT") return;
    if (forced === "OWN") { targets.push(mv.from); return; }        // B6: doubled off
    if (forced) { targets.push(forced); return; }
    targets.push(BASE_PATH[Math.min(3, BASE_ORDINAL[mv.from] + 1)]);
  });

  // 3. Outs the data records that no move accounts for are the batter's (F3).
  var recorded = Math.max(0, (m.outs_after || 0) - (m.outs_before || 0));
  var unaccounted = recorded - targets.length;
  var batterReached = moves.some(function (mv) { return mv.from === "BATTER"; });
  if (unaccounted > 0 && !batterReached && !caught) targets.push("1B");

  // 4. A caught-in-air play with a scoring runner gets the tag-up throw (A1).
  if (!targets.length && caught && moves.some(function (mv) { return mv.scored; })) {
    targets.push("HOME");
  }

  // 5. Dedupe, then sequence lead-runner-first.
  var seen = {};
  return targets.filter(function (b) {
    if (seen[b]) return false; seen[b] = 1; return true;
  }).sort(function (a, b) { return THROW_ORDER.indexOf(a) - THROW_ORDER.indexOf(b); });
}
```

**Which runner is out still comes entirely from `obc_before`/`obc_after`/
`outs_*`.** This changes only the count and the order of throw lines.

Verified output against the real feed:

| Result | Shape | Today | With A1-A3 |
|---|---|---|---|
| `GO` `000->000` (170) | `1B` | `1B` |
| `FO` `000->000` (63) | `1B` | *(none)* |
| `PO` `001->001` (9) | `1B` | *(none)* |
| `DP` `001->000` (16) | `2B` | `2B`, `1B` |
| `DP` `101->000` (2) | `3B` | `3B`, `2B` |
| `DPH1` `111->110` (2) | `HOME` | `HOME`, `1B` |
| `LODP` `010->000` (2) | `3B` | `2B` (B6) |
| `SacF` `101->001` (3) | `1B` | `HOME` |
| `FC` `001->001` (17) | `1B` | `2B` |
| `FC3rd` `011->011` (2) | `1B` | `3B` |
| `FCH` `111->111` (1) | `1B` | `HOME` |

### A3. Fielder's Choice resolves to the base its code names

```js
var FORCED_OUT_BASE = {
  FC: "2B", FC3rd: "3B", FCH: "HOME", FCLead: "2B",
  DPH1: "HOME",
  LODP: "OWN", LOTP: "OWN",   // B6 - the out is behind the runner
};
```

**Answering the prompt's "state which rather than assuming":** the generic
"next base past the runner's `from`" logic lands correctly for **none** of the
four FC codes, because (F1, F5) the FC family produces zero OUT moves and falls
straight to the `"1B"` default. All four need the explicit entry, and the
override must be evaluated *outside* the `if (runnerOut)` branch (F4) or `FCH`
keeps silently missing it as it does today.

`FCLead` is unreachable on current data (F6) - `2B` is a reasonable default for
a "lead runner forced" code with no data to check against; flag it as
unverified in the code comment.

**Known remaining gap, flagged not fixed:** on an FC play, the *runner tokens*
still do not move and no batter token appears (F5), because `deriveRunnerMoves`
pairs `1B->1B`. Fixing that properly requires `key_moments_build.py` to emit the
actual out base, which `ball-flight-plan.md` Stage 6 explicitly froze
("do not change `deriveRunnerMoves`"). Two cheap partial improvements are in
scope here and one is not:

- **In scope:** render a plain batter-runs-to-first token for the FC family
  (they did reach first), so the play is not completely static. Add
  `BATTER_REACHES_FIRST = {FC, FC3rd, FCH, FCLead}` and use it in
  `sceneFieldHtml`'s batter fallback.
- **Out of scope, recommended as a follow-up:** correctly identifying *which*
  runner was retired on an FC. Needs builder support.

### A4. Groundout throw beats the runner

Per F10 the throw currently arrives 100ms late. Make the schedule a pure
function so the race is *asserted*, not eyeballed:

```js
var THROW_DELAY_MS   = 60;    // was 150 - throw leaves almost as the ball is fielded
var THROW_DRAW_MS    = 180;   // was hardcoded 300ms in style.css:523
var THROW_STAGGER_MS = 150;   // gap between throws of a multi-throw play
var THROW_LEAD_MS    = 100;   // required margin: throw must land this early
var GROUNDER_ROLL_MS = 450;   // was 600 - a grounder to an infielder is quick

function throwSchedule(m, moves, flight) {
  var base = ballTravelMs(flight) + THROW_DELAY_MS;
  return outThrowTargets(m, moves, flight).map(function (b, i) {
    var start = base + i * THROW_STAGGER_MS;
    return { base: b, startMs: start, endMs: start + THROW_DRAW_MS };
  });
}

function batterFirstArrivalMs(flight) {
  return RUNNER_LEAD_MS + 0.4706 * 1700;   // the .out-to-first 47.06% keyframe
}
```

Resulting timings for a grounder (`ballTravelMs` = 450):

| Play | Throw 1 | Throw 2 | Batter at 1B | Margin |
|---|---|---|---|---|
| `GO` | 510 -> 690 | - | 950 | 260ms |
| `DP` | 510 -> 690 (2B) | 660 -> 840 (1B) | 950 | 110ms |

Both clear `THROW_LEAD_MS`. `THROW_DRAW_MS` must become a CSS custom property
(`--draw`) set by `throwHtml`, since `style.css:523` hardcodes 300ms today.

All five constants are **tune-after-watching**, like every other timing constant
in this codebase. The assertion (Stage T) is what keeps a re-tune honest.

### A5. `throwHtml` becomes multi-line with relay origins

Throw *i* originates at the ball's landing point; throw *i+1* originates at
throw *i*'s target base - a relay, which is both more realistic and free.

---

## Stage B - choreography removed or changed

### B1. Fielder-converge token off (Item 15)

**Feature-flag, not delete.** Add `var SHOW_FIELDER_TOKENS = false;` next to the
other render constants and guard the call at `app.js:1395`. Reasons: the
function, its CSS (`style.css:505-516`) and its reduced-motion entry are all
correct and working; the throw-from-a-fielder idea in the prompt's open
question 8 may want it back; and a flag is a one-word revert where a deletion is
a re-implementation.

**Dependency resolved (F12):** `throwHtml` originates at the ball's landing
point, never at the fielder anchor. B1 breaks nothing in Stage A and can ship
first.

### B2. No batter movement on steals and caught stealings (Item 7)

Two changes:

1. **`key_moments_build.py`**: add `CS3`, `CS4`, `SB3`, `SB4` to
   `FLIGHT_EXCLUDED` (F7), and emit a new list
   `meta.flight.no_pa = sorted(STEAL_SUCCESS_CODES | CAUGHT_STEALING_CODES | {"Balk"})`.
   Both sets already exist at `key_moments_build.py:68-69`; reuse them rather
   than typing a third copy of the steal codes.
2. **`app.js`**: in `sceneFieldHtml`, skip the batter-token fallback entirely
   when `(data.meta.flight.no_pa || []).indexOf(m.result) !== -1`. The batter
   did nothing; they should render nothing.

I considered deriving this from `unaccountedOuts === 0` instead. Rejected: `GO`
`001 -> 000` (22 plays) also has zero unaccounted outs, and today correctly
shows the batter running to first. A code list is narrower and does not regress
that.

### B3. Caught stealing / stolen base visual - **OPEN, needs Alex's pick**

Alex specified no visual. Three concrete options, in ascending cost:

| # | Option | What it looks like | Cost |
|---|---|---|---|
| **B3-a** | **Tag at the bag** | The runner token travels its normal basepath leg, then on a `CS` the *destination base plate* flashes red and the runner reverses out to the dugout from that base rather than from their origin. On an `SB` the plate flashes green and the token settles with a small scale pop. | Low - one new class on `.dm-base`, reuse `rnOutWalk` with a different `--fx`. |
| **B3-b** | **Chip riding the path** | A small `SB` / `CS` text label rides along with the runner token, fading at the base. | Low-medium - a new `<text>` element following the same `--p1x/--p1y` keyframes. |
| **B3-c** | **Catcher throw line** | Draw a throw line from the catcher anchor (`C` at `(0,-5)` ft, already in `FIELDER_ANCHORS_FT`) to the target base, timed to arrive just before (CS) or just after (SB) the runner. Reuses Stage A's whole throw pipeline. | Medium - requires `throwSchedule` to run on flightless plays, which it currently cannot (`throwHtml` early-returns on `!flight`). |

**My recommendation: B3-c, with B3-a layered on.** It is the only one that
explains *why* the runner is out, it reuses the throw machinery Stage A is
already building, and a steal is the one play type where "the ball beat the
runner" is the entire story. But this is Alex's call - do not build until picked.

### B4. Strikeout batter starts at the plate (Item 19)

Per F9, a CSS one-liner:

```css
.rn.out-walk, .rn.out-to-first{animation-fill-mode:both; ...}   /* was forwards */
.rn.legs1, .rn.legs2, .rn.legs3, .rn.legs4{animation-fill-mode:both; ...}
```

Check after: the reduced-motion block (`style.css:920-928`) sets
`animation:none` on all of these, so the base rule (token at `--tx/--ty`, i.e.
the end state) still wins there. No reduced-motion regression.

### B5. Sac fly waits for the catch (Item 28)

Per F11, `--rdelay` is already per-token. Change `app.js:1278-1295`:

```js
// Was: one play-wide runDelay for every safe runner.
// Now: a runner tagging up on a caught fly cannot leave until the catch.
var catchMs = flight && CAUGHT_IN_AIR[flight.archetype] ? ballTravelMs(flight) : 0;
// inside moves.map:
var mvDelay = isOut ? outDelay
  : (catchMs && mv.scored ? catchMs + TAG_UP_MS : runDelay);
```

`TAG_UP_MS = 80`. Scope it to `mv.scored` rather than to the `SacF`/`DSacF`/
`DFO` result codes: that catches the same set on real data (every scoring runner
on a caught fly is by definition tagging) and does not need a fourth result-code
list to maintain. Flag this widening as my choice, not a relayed decision.

Consequence for dwell: on a max-hangtime fly (`ballTravelMs` 1400) a runner
scoring from third now finishes at 1400 + 80 + `RUN_LEG_MS[1]` 800 = 2280ms.
Within the 3600ms routine dwell. Check the score-ticker: `scoreArrivals`
(`app.js:1757-1767`) computes arrival times from `RUN_LEG_MS` alone and does
**not** know about `--rdelay`, so the scoreboard will tick ~1480ms before the
runner arrives on these plays. **Fix `scoreArrivals` in the same change** -
add the same `mvDelay` offset. Missing this is the most likely way to ship a
subtly wrong B5.

### B6. LODP / LOTP - **cheaper than Alex feared for the throw, more expensive for the choreography**

Split it:

- **The throw target is trivial** and is already folded into A3:
  `FORCED_OUT_BASE.LODP = "OWN"` sends the throw to the runner's *original*
  base. Verified: `LODP` `010 -> 000` goes from `3B` (wrong, today) to `2B`
  (right). ~2 lines. **Ship this in Stage A.**
- **The runner-advances-then-retreats choreography** is a genuinely new motion
  shape (out, then partway forward, then *backward*) with no existing keyframe
  to reuse. It needs a new `rnDoubledOff` keyframe with four stops
  (origin -> 40% toward the next base -> back to origin -> dugout) and its own
  reduced-motion entry. Estimate: 30-40 lines of CSS + JS, plus a visual pass.
  **Only 2 plays in the entire feed (`LODP` x2, `LOTP` x0) would ever show it.**

**Recommendation: ship the throw fix now, defer the choreography.** Two plays in
1174 does not justify a new animation shape in a round that already has 25
items. Flagged as an open question so Alex can overrule.

---

## Stage C - ball landing visuals and calibration

### C1. Landing dot red for an out, green for a hit (Item 24)

`result_category` is already on every play (`m.result_category` is `"hitting"`
or `"pitching"`, `key_moments_build.py:559`). But it is not quite the right
signal: `GORA` and `SacF` are `pitching` and produce an out *and* an advance,
while `FC` is `pitching` and the batter reaches. Use the direct question
instead:

```js
var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
```

Thread it into `ballFlightHtml(m, flight)` and append `" out"` or `" hit"` to
the existing class string (`app.js:1209`). CSS:

```css
.ball.hit{stroke:var(--green);}    .ball.hit + .ball-trail, .ball-trail.hit{stroke:var(--green);}
.ball.out{stroke:var(--red);}      .ball-trail.out{stroke:var(--red);}
```

Keep the white fill so the marker stays visible on the grass fill in both
themes; colour the stroke and the trail only.

Edge case to state in the code: a play can be both (a sac fly is an out *and* a
run). `wasOut` wins - the ball itself was caught.

### C2. Small result label at the landing point (Item 17)

`RESULT_LABELS` is unusable at marker size (F13). Add a second table in
`key_moments_build.py`, emitted as `meta.result_short`, defaulting to the raw
code (already terse for most):

```python
# Terse on-field marker labels - RESULT_LABELS is display copy and too long
# at marker size. Anything absent falls back to the raw result code.
RESULT_SHORT = {
    "1BWH": "1B", "1BWH2": "1B", "B1B": "1B", "B1BWH": "1B",
    "2BWH": "2B",
    "GORA": "GO", "BGO": "GO",
    "DP21": "DP", "DP31": "DP", "DPH1": "DP", "DPRun": "DP",
    "FC3rd": "FC3", "FCH": "FCH", "FCLead": "FCL",
    "SacF": "SF", "DSacF": "SF", "SacB": "SH",
    "LOTP": "TP", "LCO": "LO",
}
```

Max 4 characters. Render as an SVG `<text>` offset from the landing point,
anchored away from home plate so it never overlaps the trail, at ~7px font with
a `paint-order: stroke` halo so it stays legible over the fence line and the
grass.

### C3. Distance label on home runs only (Item 22)

`flight.distance` is already in feet (`ftToSvg` and `FT_PER_UNIT` are the only
conversion, and `flightParams` never leaves feet) - **no unit conversion
needed**, confirmed. Render `Math.round(flight.distance) + " ft"` next to the
marker when `m.result === "HR"`, nowhere else.

Note it renders on inside-the-park home runs too, where the marker is inside the
field - that reads correctly and is worth keeping.

### C4. Rollout on non-grounder hits - **OPEN, my interpretation**

**What is actually there today:** there is no separate rollout. For a grounder,
`ballTravelMs` returns `GROUNDER_ROLL_MS` and the ball travels home -> landing
point over that time. The "short ground-level roll" in `ball-flight-plan.md`
Stage 4e is that whole travel, not a segment past the landing point. So the
answer to the prompt's question is: **interpretation (a) is right, and it needs
new code for both cases, not just for line drives.**

Proposal:

```js
var ROLLOUT_FT = 34;    // how far past the landing point a hit ball carries
var ROLLOUT_MS = 320;
```

For any play where `wasOut` is false and `!flight.clearedFence` (i.e. a hit that
stayed in the park), draw a **second, dimmer trail segment** from the landing
point outward along the same bearing for `min(ROLLOUT_FT, 0.14 * distance)`
feet, and continue the ball marker along it starting at `ballTravelMs(flight)`
over `ROLLOUT_MS` with an ease-out. The landing marker (C1/C2) stays at the
*landing* point, so "where it landed" and "where it was fielded" read as two
different things - which is the point of the item.

**Flagged as open question 5:** this is my reading of "show point of contact to
field, but also rollout somehow," not a relayed decision. Confirm before build.

### C5. Recalibrate the `single` archetype (Item 2)

Tested replacement bands against all 206 real `1B`/`1BWH`/`1BWH2` plays:

| `depth_min`-`depth_max` | Landing on the infield dirt |
|---|---|
| 60-160 (**today**) | 96 / 206 (47%) |
| 100-200 | 16 / 206 (8%) |
| **130-230** | **0 / 206 (0%)** |
| 150-250 | 0 / 206 (0%) |

**Recommended edit to `ball_flight_archetypes.csv`:**

```
single,6,20,75,98,130,230       # was: single,-5,10,75,98,60,160
```

The LA change matters as much as the depth change: at `-5..10` every real single
launches between 0.2 and 4.9 degrees (F15), straddling `GROUND_LA_THRESHOLD`, so
a "single" that lands 160ft away is staged as a grounder that somehow rolled
there. At `6..20` every real single lands between 9.4 and 13.6 degrees - a clean
line drive, never a grounder - and C4's rollout then carries it the extra few
feet. Weak infield hits keep their own archetype (`IF1B` -> `infield_single`,
45-90ft), which is what that archetype is for.

This is a data-file change only. No code moves.

### C6. Inside-the-park home run rate - **flag; the uniform fence cannot be tuned to "rare"**

Per F16 the real rate is **23%** (9 of 39), against a ~1% real-world expectation
and the plan's 0.9% budget.

`ball-flight-plan.md` names the depth band as the honest lever. **Measured, it
does not work under a uniform fence.** Because `q` clamps at 0, **6 of the 39
home runs sit exactly at `depth_min`** (their `diff` is at or past the band's
`hi` of 19). So the rate is effectively binary:

| `home_run` depth band | Inside the park |
|---|---|
| 370-420 (**today**) | 9 / 39 (23%) |
| 373-420 | 6 / 39 (15%) |
| 375-420 | 6 / 39 (15%) |
| **376**-420 or deeper | **0 / 39 (0%)** |
| 350-430 | 14 / 39 (36%) |

There is no `depth_min` that produces a rare-but-live rate while
`FENCE_DEPTH_FT` is a single number: anything at or below 375 keeps the six
pinned-at-`depth_min` home runs inside, and anything above 375 makes the code
path dead. Two real options:

**C6-a (simple, keeps the uniform fence).** `home_run,25,35,98,115,376,430`.
Inside-the-park rate becomes 0%. The `.itp-pill` (`app.js:1736-1737`) and the
`clearedFence` branch stay in place but never fire - harmless, and a future
fence change re-enables them.

**C6-b (restores rarity, costs one function).** Bring back the per-angle fence
profile the original plan specified, which is exactly why `fenceAt(angleDeg)`
was kept as a *function* rather than a bare constant (`app.js:984`) - "nothing
has to change if a per-angle profile ever comes back." Measured on the real 39:

| Fence profile (`centre - (centre - lines) * (|angle-45|/45) ** 1.15`) | Inside the park |
|---|---|
| **330 down the lines / 375 to centre** (the original plan's) | **1 / 39 (3%)** |
| 345 / 375 | 2 / 39 (5%) |
| 330 / 390 | 4 / 39 (10%) |

`clampToFence` already calls `fenceAt(angleDeg)` and needs no change; only
`fencePathD` (`app.js:1105-1113`) would have to draw a sampled path instead of a
single circular arc, roughly 8 lines. Note this also interacts with Decision
D-2: under a radial scale a non-uniform fence is still a well-defined curve, so
the two are compatible.

**Recommendation: C6-b with the original 330/375 profile.** It is the only
option that produces a genuinely rare inside-the-parker, it restores a number
the original plan had already tuned deliberately, and the cost is one function.
But Alex chose the uniform fence for simplicity, so this is explicitly a
reversal to confirm rather than assume - **flag it, do not build it silently**
(open question 9). C6-a is the fallback if the simple circle matters more than
the 3%.

---

## Stage D - field scale - **OPEN, evaluate and recommend**

### The problem, measured

`FIELD_W/H` = 460x370, `FT_PER_UNIT` = 1.4, home at `(230, 330)`, fence at 375ft
= 268 units. `.scene-diamond-wrap` is `min(50%, 230px)` on desktop, so the SVG
renders at ~0.5 CSS px per unit. The infield's half-diagonal (90ft) is 64 units
= **32 CSS px**. The whole infield diamond is about 64x64 CSS px inside a 230px
frame. Alex is right: it is tiny.

### Option D-1: dynamic zoom (Alex's suggestion)

Wrap all field content in a `<g class="field-cam">` and animate
`transform: scale()` on it, `transform-origin` at home plate, with
`animation-delay: ballTravelMs * 0.3`. Start at ~2x (infield-filling), settle at
1x.

- **Pro:** the infield is genuinely large for the first beat of every play, and
  a strikeout or a steal - which never leaves the infield - stays zoomed in for
  the whole slide. That is a real win.
- **Con (cost):** `viewBox` is an attribute and cannot be CSS-animated, so this
  must be a group transform, which scales stroke widths and token radii too. The
  ball, runner tokens and base plates would all visibly shrink mid-play. Fixable
  with `vector-effect="non-scaling-stroke"` for strokes, but radii need
  counter-scaling on every token - a real amount of work spread across five
  render functions.
- **Con (reduced motion):** needs a decision per play - hold the zoomed-in frame
  or snap to zoomed-out? A batted ball's landing point is off-frame when zoomed
  in, so it must snap out, which means reduced-motion users lose the benefit
  entirely.
- **Con (feel):** a camera move under CSS-only timing, competing with six other
  animations already running on the same slide, is the piece most likely to read
  as jittery.

### Option D-2: piecewise radial scale (recommended)

Make `ftToSvg` non-linear in radius: linear inside the infield, compressed
outside it.

```js
var LINEAR_FT   = 150;   // exact scale out to here (the whole infield + a margin)
var LINEAR_UNIT = 0.75;  // ft per SVG unit inside LINEAR_FT
var OUTER_UNIT  = 2.8;   // ft per SVG unit beyond it

function radialSvg(rFt) {
  if (rFt <= LINEAR_FT) return rFt / LINEAR_UNIT;
  return LINEAR_FT / LINEAR_UNIT + (rFt - LINEAR_FT) / OUTER_UNIT;
}

function ftToSvg(xFt, yFt) {
  var r = Math.hypot(xFt, yFt);
  var k = r === 0 ? 0 : radialSvg(r) / r;
  return { x: HOME_SVG.x + xFt * k, y: HOME_SVG.y - yFt * k };
}
```

**Why this works so cleanly here** - the geometry happens to cooperate:

- Every ball flight is **radial from home**, so a straight trail stays a
  straight line under a purely radial map. No curved trails.
- The **fence is at constant radius**, so it stays an exact circular arc -
  `fencePathD` needs only `radialSvg(FENCE_DEPTH_FT)` instead of
  `FENCE_DEPTH_FT / FT_PER_UNIT`.
- Every **basepath leg** (90ft to 127ft) is entirely inside the linear zone, so
  the infield is geometrically exact and the diamond is a true square.
- Both **dugout anchors** (r = 98ft) are inside the linear zone.
- `nearestFielder` works in feet and is untouched.
- **Every render function already routes through `ftToSvg`** - the "single
  conversion point" discipline `ball-flight-plan.md` Stage 4a insisted on is
  exactly what makes this a ~15-line change instead of a rewrite.

Resulting sizes: 1B/3B at 90ft -> 120 units (was 64), 2B at 127ft -> 169 (was
91), fence at 375ft -> 280 (was 268). New canvas `FIELD_W = 430`,
`FIELD_H = 330`, home at `(215, 285)`. The infield's on-screen share roughly
**doubles**.

- **Pro:** static frame, no animation, zero reduced-motion interaction, nothing
  to feel jittery, and the infield is exactly to scale where it matters.
- **Con:** outfield distances are compressed, so a 420ft home run reads only
  modestly deeper than a 300ft double. Mitigate by keeping `OUTER_UNIT` gentle
  and, optionally, drawing a faint 200ft reference arc.
- **Con:** throw lines and the out-walk to the dugout are chords, not radial, so
  they are drawn straight where the map would technically curve them. Visually
  irrelevant at these lengths - a throw line is a straight line anyway.
- **Con:** it is a mild fisheye. A viewer who measures will notice.

### Option D-3: static, bigger infield, deeper crop

Keep `ftToSvg` linear, drop `FT_PER_UNIT` to ~1.0 and let the deepest home runs
run off the top of the frame (clipped, with the fence still drawn).

- **Pro:** trivially simple; distances stay honest where they are visible.
- **Con:** breaks C3's home-run distance marker (it would render off-canvas) and
  makes over-the-fence home runs indistinguishable from each other. Rejected.

### Recommendation

**D-2.** It is the lowest-risk option by a wide margin (one function, no new
animation, no reduced-motion branch), it is the only one that helps
reduced-motion users, and it directly exploits a property of this codebase that
was deliberately built in. D-1 is the better *feel* if Alex wants a camera and
is willing to pay for token counter-scaling; hold it as the fallback.

**Nothing in Stage D gets built until Alex signs off.** Everything else in this
plan is written to be independent of which option wins, except Decision F1,
which needs D's answer for its coordinates.

---

## Stage E - math and formula

### E1. The tie-break rule - **already implemented, do not change `signedCirc`**

Per F14, `signedCirc` already computes exactly Alex's anchor-and-no-wrap rule
at both wheels' halfway points, verified exhaustively. The work is:

1. **No code change to `signedCirc`** (`app.js:1067-1072`).
2. Rewrite its comment to state the rule affirmatively rather than describing the
   `+5`/`-5` behaviour as an accepted artifact.
3. Add four assertions to `ball_flight_test.py` (Stage T).
4. Correct `ball-flight-plan.md` Stage 3b's "known artifact ... must not be
   fixed" note and its Open Question 4, replacing both with the rule. The two
   existing assertions there stay - they now pass *because of* the rule rather
   than in spite of it.

If Sonnet finds itself editing `signedCirc`'s body, it has misread this section.

### E2. LA sign from the overall diff - **investigate only: recommend NO**

**Correcting Alex's example first (F18):** pitch 830 / swing 770 gives
`dLA = signedCirc(82, 76, 100) = -6`, already negative, and the full signed
delta `signedCirc(830, 770, 1000) = -60` is also negative. The two agree, so the
example does not show the problem it was offered for. Alex's "3 -> 7" reading is
`onesDigit` - the *horizontal* axis, not the launch-angle one.

**The underlying question, measured:** across all 818 in-scope plays with a
`pitch`/`swing` pair, `sign(dLA)` and `sign(signedCirc(pitch, swing, 1000))`
disagree on **2 plays (0.2%)**, with 5 more where one side is exactly zero.

**Recommendation: do not change it.** The change would move 2 plays out of 818,
at the cost of a second, differently-derived sign source that would then have to
be kept consistent with the tie-break rule in E1 (which is defined on a wheel
the full-value delta does not share). No measurable benefit, real conceptual
cost.

**But the instinct behind the question is worth surfacing (F17).** If what Alex
is actually reacting to is "every home run looks the same," the sign is not the
problem - the *spread* is. Because a home run needs `diff <= ~21`, the two-digit
components are forced within ~2 of each other and every home run launches at
30.0 +- 0.3 degrees. Same for `3B` (22.1-22.9) and `2B` (17.6-19.1).

Two levers exist if Alex wants spread back, neither implemented here:

| Lever | HR launch-angle spread |
|---|---|
| Today (`firstTwo`, mod 100) | 29.8 - 30.5 (sd 0.16) |
| `lastTwo(v) = (v-1) % 100`, mod 100 | 27.7 - 34.5 (sd 1.48) |
| `tensDigit`, mod 10, scaled to +-5 | comparable spread, and stays independent of the ones digit that already drives the horizontal axis |

The `tensDigit` variant is the cleaner of the two, since `lastTwo` shares the
ones digit with the horizontal axis and would correlate launch angle with pull
direction. **Do not implement either.** This is Alex's call after reading it.

### E3. Distance vs launch-angle optimality - **investigate only: recommend NO**

**(a) Is the band centre near real optimal?** Yes. Statcast's home-run-optimal
launch angle sits around 25-31 degrees; the `home_run` band's centre is 30. No
adjustment needed.

**(b) Should distance taper with `|LA - optimal|`?** **No, and for a decisive
reason: it would be a no-op.** Per F17 every real home run already launches at
30.0 +- 0.3 degrees, i.e. within 0.5 degrees of the band centre. An
LA-proximity factor would multiply every home run by essentially the same
constant. Measured correlation between LA and distance across the 39 real home
runs is 0.111 - effectively noise.

**And the thing Alex actually asked for already happens.** "Should a `dLA = 0`
home run go the farthest?" - today `D = depthMin + q * (depthMax - depthMin)`
and `q = 1` when `diff <= band_lo` (which is 1 for `HR`). So a diff-0 home run
already gets the maximum 420ft, and distance already tapers as `diff` grows.
The taper runs off `diff` rather than off `LA`, but the visible behaviour is
exactly what Alex described.

**Recommendation: change nothing in the distance formula.** If Alex wants
distance to *look* more varied across home runs, the lever is the `home_run`
depth band's width in `ball_flight_archetypes.csv` (currently 370-420, a 50ft
spread), not a new term. Widening to 365-435 would double the visible variation
for a one-line data edit - but interacts directly with C6's inside-the-park
rate, so decide the two together.

Generalising to other hits, as Alex asked: same conclusion, and more strongly.
`2B` and `3B` have even tighter LA spreads than `HR`. Only `GO`/`FO`/`PO` - the
bad-contact results - have real LA variety, and those are not the ones anybody
wants a distance bonus on.

---

## Stage F - new readouts

### F1. Pitch/swing wheel - **depends on Decision D**

New pure-SVG element, built with the same single-conversion-point discipline as
the field canvas (its own `wheelPoint(value)` helper, nothing hand-placed).

**Which wheel:** the **full 1-1000 wheel**. That is the wheel `diff` is actually
computed on and the one the league plays on; the ones-digit and two-digit wheels
are internal staging details a viewer has no reason to see. My choice, since
Alex did not specify.

Shape:
- A circle, radius ~52 units, 1 at the top, running clockwise.
- Two small ticks with labels: `P 407` and `S 412` (dimmed team colours -
  defence for pitch, offence for swing).
- The **shorter** arc between them drawn as a thick highlighted stroke.
- The numeric `diff` centred inside, with a `DIFF` micro-label under it.
- Rendered only when `m.pitch != null && m.swing != null`; nothing otherwise.

**Placement:** the bottom-right foul-territory wedge. Under today's viewBox that
wedge is the triangle `(230,330) - (460,330) - (460,100)` and is genuinely
empty, but it also contains the 1B-side dugout anchor at `(298,348)`. Under
D-2's proposed geometry the equivalent wedge is `(215,285) - (430,285) -
(430,70)` and a radius-52 circle centred at `(355,215)` fits clear of both the
fence arc and the dugout.

**Do not commit coordinates until D is decided.** If Alex picks D-1 (dynamic
zoom), the wheel must live *outside* the zoomed group or it will scale with the
camera - flag that as a build note.

---

## Stage G - navigation

### G1. Swipe at a boundary - **OPEN, needs Alex's confirmation**

Current behaviour verified (F20): a swipe past either end does nothing at all.
There are no existing nav tests (F19) - the prompt's reference to them is
mistaken.

The literal request ("swiping past the last play jumps straight to the very last
play") is ambiguous, because at the last play you are *already* at the last
play. Three readings:

| # | Reading | Behaviour |
|---|---|---|
| **G1-a** | Wrap | A swipe forward at the end jumps to slide 0; a swipe back at the start jumps to the last slide. |
| **G1-b** | Jump to the end from anywhere | Any swipe *at* a boundary is a no-op today; instead, a **long** swipe (>= 3x `SWIPE_MIN_PX`, i.e. 135px) anywhere jumps to the first/last slide. Ordinary swipes keep stepping +-1. |
| **G1-c** | Exit | A swipe forward at the last slide closes the show (Catch Me Up already auto-closes past the end; this just makes the gesture agree). |

**My recommendation: G1-b.** It is the only one that does not make a swipe at
the boundary mean something different from a swipe anywhere else - which is the
UX inconsistency the prompt asked to flag. It also gives Alex a way to skip to
the end of a 656-play Catch Me Up backlog, which is plausibly the real need
behind the request.

Whichever is picked, mirror it into the keyboard handlers
(`app.js:2128-2134`, `2324-2330`) with `Home`/`End`, and keep `syncNav`'s
disabled-arrow logic in agreement.

---

## Stage H - accessibility and dark mode

### H1. Ring on runner tokens in dark mode (Item 30)

Today `.rn circle` (`style.css:532`) strokes with `var(--card)`, which in dark
mode is `#1a2029` - a dark ring on a dark field, i.e. invisible. Add, after the
base rule:

```css
:root[data-theme="dark"] .rn circle{stroke:rgba(232,234,237,0.88);stroke-width:1.6;}
```

Team-colour fill (`--rn-fill`) is untouched. Scoped to the dark selector so
light mode is unaffected, matching the existing `[data-theme="dark"]` override
pattern (`style.css:474`, `775`, `785`).

Check the two overrides that already exist for token fill: `.rn.score circle`
(green) and the `rnTurnRed` keyframe both animate `fill`, not `stroke`, so the
ring survives both states - which is what we want.

---

## Stage I - copy and formatting

Lowest-risk group in the plan. Ship it first.

### I1. Replace the live-recap string (Item 4)

`app.js:1818`. Three candidates:

1. `Caught up - the game is still going`
2. `That's every play so far - more to come`
3. `Still in progress - check back for more`

**Recommendation: #1.** It says both things the current copy says, in half the
words, and drops the possessive-plus-contraction pileup ("That's ... the game's")
that makes the current line hard to scan. Note the `·` separator is already used
throughout this file (`app.js:1820`, `1824`, `1881`); keep it:
`That's everything so far · the game's still going` becomes
`Caught up · the game is still going`.

Low risk either way - if none of these lands, it is Alex's pick.

### I2. `END 5th` / `MID 5th` (Item 5)

New helper, near `pad2` (`app.js:69`) - **no ordinal formatter exists in the
file today** (F21):

```js
function ordinal(n) {
  var v = n % 100;
  if (v >= 11 && v <= 13) return n + "th";
  return n + ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
}
```

Then `stateStack` (`app.js:375-377`):

```js
if (m.is_half_inning_final) {
  // half === "bottom" means the whole inning just closed; "top" means only the
  // first half is done and the same inning continues.
  var lbl = (m.half === "bottom" ? "END " : "MID ") + ordinal(m.inning);
  return '<div class="state-stack"><div class="state-badge">' + lbl + "</div></div>";
}
```

`is_game_final` is checked first (`app.js:371`) and still wins, so a walk-off
shows `FINAL`, not `END 6th`. `scoreboardCard` also calls `stateStack` but
always passes `is_half_inning_final: False` (`key_moments_build.py:818`), so
scoreboard tiles are unaffected.

Assert `ordinal` in the harness: `1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st,
22nd, 23rd, 101st, 111th`.

### I3. Stacked replay title slide (Item 6)

`app.js:1891-1904` plus `style.css:453-456`.

**Confirmed scope question:** `.catchup-title-teams` is used by **three** slide
kinds - `replay-title` (`app.js:1893`), Catch Me Up's per-game `title`
(`app.js:1873`) and the `done` card (`app.js:1886`). A pure
`flex-direction: column` change would restack all three.

**Recommendation: restack all three.** Catch Me Up's per-game title slide is
precisely "the slide that shows the game before the plays slides" for that
slideshow, so Alex's ask applies to it identically. The `done` card holds a
single string and is unaffected in practice by a column direction.

Markup change needed as well as CSS: today the logo and the name are two sibling
inline items, so a bare `flex-direction: column` would put the logo on its own
line above the name. Wrap each logo+name pair:

```js
'<span class="catchup-title-team">' + teamLogoImg(...) + '<span>' + name + '</span></span>'
```

```css
.catchup-title-teams{flex-direction:column;gap:6px;}
.catchup-title-team{display:flex;align-items:center;gap:10px;}
```

The mobile override (`style.css:906-907`) needs no change.

### I4. Drop the timezone abbreviation (Item 20)

Exactly three lines, all in `formatMomentTime` (F21):

- Delete `viewerZoneAbbr` (`app.js:106-110`) - it has no other consumer.
- Delete `var zone = ...` (`app.js:119`) and the `+ (zone ? " " + zone : "")`
  tail (`app.js:121`).

`chicagoOffsetMinutesAt` (`app.js:82-89`) is untouched - it is internal
source-zone math, not display. The remaining `d.getHours()`/`getMinutes()` calls
still report in the **viewer's** local zone, which is the behaviour to keep.

### I5. Parentheses to dot separators (Item 34)

Audited every parenthesised user-facing string in `app.js`. Five sites:

| Line | Current | Change |
|---|---|---|
| 856 | `Catch Me Up (3 new plays)` | `▶ Catch Me Up · 3 new plays` - **yes** |
| 667 | `Filters (2 active)` | `Filters · 2 active` - **yes** |
| 1825 | `Biggest play: Name · Double (LI 3.2)` | `... · Double · LI 3.2` - **yes** |
| 349 | `58% (+3.2)` | leave, or `58% · +3.2` - **flag** |
| 2766 | `Could not load the key moments feed (404)` | leave - technical detail |

Site 349 (`wpFragment`, the main feed's meta-line) is the judgement call: it
reads as a chart-style annotation rather than a count qualifier, and the line
already contains a `·`-free layout. **Recommendation: change it too**, for
consistency with 1825 which has the same shape, but flag it as the one Alex
might want left alone.

Bonus, same area: `formatBuiltAt` (`app.js:133`) renders `"Updated 2 day(s) ago"`.
Fix the pluralisation while in there: `Math.round(mins/1440) + (n === 1 ? " day ago" : " days ago")`.

### I6. Third out becomes a dot (Item 25)

`sceneOutsHtml` (`app.js:1672-1686`). Replace the two-dot map plus the separate
`.out-third` span with a three-dot map:

```js
var dots = [0, 1, 2].map(function (i) {
  var on = i < after;
  return '<span class="out-dot' + (on ? " on" : "") +
    (on && i >= before ? " fresh" : "") + '"></span>';
}).join("");
```

and drop the `(after >= 3 ? '<span class="out-third">...' : "")` tail. `after`
is already clamped to 0-3 (`app.js:1674`), so the `shown = Math.min(after, 2)`
line goes away too.

`.out-third`'s CSS (`style.css:743-746`) can be deleted along with its
reduced-motion entry (`style.css:927`) - `.out-dot.fresh` already carries the
same `outPop` animation, so the third dot inherits the pop for free. Keep the
`.scene-outs.inning-over` class and its styling: that is the "this ended the
inning" signal, and it survives the badge going away.

**Note the comment at `app.js:1666-1671` explains why there were only two dots**
("a third out ends the half-inning - there is no steady state to show it in").
That reasoning applies to the scorebug's live state, not to the Play Scene,
which is a replay of a completed play. Rewrite the comment rather than deleting
it.

### I7. Defer the gold base highlight (Item 26)

`app.js:1338-1345` paints `.dm-base.on` from `obc_after` at slide mount, so the
bases light up before the runners get there. Two options:

- **Cheap:** add `--rdelay`-style timing - give `.dm-base.on` a CSS
  `animation: baseLight 200ms ease var(--blight) both` where `--blight` is the
  slowest runner arrival (`runDelay + RUN_LEG_MS[maxLegs]`), so the plates
  light after the tokens land.
- **Cheapest:** only paint `.on` for bases that were **already occupied before**
  the play (`before[i] === "1" && after[i] === "1"`, i.e. a runner who did not
  move), and let the arriving runners' own tokens communicate the rest.

**Recommendation: the first.** The end state must still show post-play occupancy
(that is what makes the settled field readable), so suppressing it entirely is
wrong; delaying it is exactly what Alex asked for. Reduced motion sets
`animation: none`, and the base rule already paints the final state, so the
end state is correct there for free.

### I8. Partial advance for out-bound runners (Item 27)

Per the prompt's finding 11, this is `.out-to-first`'s missing sibling. Today an
OUT-bound move renders `path = []` (`app.js:1290`) and walks straight from its
origin to the dugout.

Change: for an OUT move, build `basepathWaypoints(mv.from, forcedBase, false)`
where `forcedBase` is the base that move's throw targeted (already computed by
`outThrowTargets`), run that leg at normal speed, *then* turn red, *then* walk
to the dugout - the exact three-beat shape `.out-to-first` already uses. Reuse
`rnOutToFirst`'s keyframe structure with the waypoint swapped, renamed
`rnOutToBase`, and let `.out-to-first` become a special case of it.

Scope I8 **with A2**, not separately - it consumes A2's per-move target list.

---

## Stage T - tests

`ball_flight_test.py` is the only test file that exists (F19) and it currently
passes in full. Extend it rather than creating siblings; the pure functions this
round adds are the same shape as the ones it already covers.

Run it with the **system** Python, not `.venv` (Playwright is only installed
globally):
`C:\Users\Alex\AppData\Local\Programs\Python\Python310\python.exe ball_flight_test.py`

### T1. Extend `window.KMFlight` (`app.js:1172-1177`)

Add `ordinal`, `outThrowTargets`, `throwSchedule`, `batterFirstArrivalMs`, and
the timing constants. Same "test hook only, never read by the page" comment.

### T2. New assertions in `ball_flight_test.py`

**E1 tie-break (four new, plus keep the two existing):**

```
signedCirc(3, 8, 10)   === +5
signedCirc(8, 3, 10)   === -5
signedCirc(30, 80, 100) === +50
signedCirc(80, 30, 100) === -50
```

Plus an exhaustive loop asserting, for every tie pair on both wheels,
`signedCirc(a, b, mod) === (b > a ? mod/2 : -mod/2)`. That is the anchor-and-
no-wrap rule stated directly, and it is 6 lines.

**`ordinal` (I2):** `1st 2nd 3rd 4th 11th 12th 13th 21st 22nd 23rd 101st 111th`.

**`outThrowTargets` (A1-A3, B6)** - drive it from the real shapes in F1/F3's
tables, since those are the ones that actually occur:

| `result` | `obc_before` -> `obc_after` | `runs` | outs | Expect |
|---|---|---|---|---|
| `GO` | `000 -> 000` | 0 | 0->1 | `["1B"]` |
| `FO` | `000 -> 000` | 0 | 0->1 | `[]` |
| `PO` | `001 -> 001` | 0 | 0->1 | `[]` |
| `SacF` | `101 -> 001` | 1 | 0->1 | `["HOME"]` |
| `DP` | `001 -> 000` | 0 | 0->2 | `["2B", "1B"]` |
| `DP` | `101 -> 000` | 0 | 0->2 | `["3B", "2B"]` |
| `DPH1` | `111 -> 110` | 0 | 0->2 | `["HOME", "1B"]` |
| `LODP` | `010 -> 000` | 0 | 0->2 | `["2B"]` |
| `FC` | `001 -> 001` | 0 | 0->1 | `["2B"]` |
| `FC3rd` | `011 -> 011` | 0 | 0->1 | `["3B"]` |
| `FCH` | `111 -> 111` | 0 | 0->1 | `["HOME"]` |
| `HR` | `000 -> 000` | 1 | 0->0 | `[]` |
| `1B` | `000 -> 001` | 0 | 0->0 | `[]` |

Assert **ordering**, not just membership - lead-runner-first is the whole point
of A2.

**A4 timing race (the assertion that keeps a re-tune honest):**

```
for every grounder-archetype case above:
    max(t.endMs for t in throwSchedule(...)) + THROW_LEAD_MS <= batterFirstArrivalMs(flight)
```

**C5 recalibration regression:** re-run F15's infield-dirt sweep as an
assertion - no `1B`/`1BWH`/`1BWH2` in the feed may land inside
`|x| + |y - 63.64| <= 63.64`. This is the check that proves the band edit
worked, and it fails loudly if someone reverts
`ball_flight_archetypes.csv`.

**Existing ground-truth sweep:** keep it. If Decision D-2 lands, it needs no
change - it works in feet, and `radialSvg` is a rendering concern.

### T3. Manual passes (no automation exists or is worth writing)

- Reduced motion on: step through a strikeout, a groundout, a double play, a sac
  fly and a home run; confirm every new piece shows its end state instantly.
- Dark mode: confirm the H1 ring, and that C1's red/green ball strokes separate
  from the grass fill in both themes.
- Phone breakpoint: confirm the new landing label (C2), distance label (C3) and
  wheel readout (F1) do not force `.scene-play-line` to wrap.

---

## Open questions - flag, do not silently resolve

1. **Field scale (Stage D)** - three options laid out, **D-2 (piecewise radial
   scale) recommended**. Nothing gets built until Alex picks. Decision F1 depends
   on the answer.
2. **LA sign from the overall diff (E2)** - investigated. **Recommend no
   change** (0.2% effect, and Alex's example does not show the problem). The
   real finding is that launch angle is a near-constant for every good-contact
   result; two levers offered, neither implemented.
3. **HR distance vs LA optimality (E3)** - investigated. **Recommend no
   change**: every home run already launches at 30.0 +- 0.3 degrees, so an
   LA-proximity factor is a no-op, and diff-0 already produces the farthest home
   run through `q`.
4. **CS/SB visual (B3)** - three options, **B3-c (catcher throw line) plus B3-a
   recommended**. Needs Alex's pick before build.
5. **Rollout on non-grounder hits (C4)** - my interpretation of an ambiguous
   ask, with a concrete proposal. Confirm before build.
6. **Swipe boundary gesture (G1)** - three readings, **G1-b (long swipe jumps
   to either end) recommended** as the only one that keeps a boundary swipe
   consistent with every other swipe. Confirm.
7. **LODP/LOTP choreography (B6)** - the **throw fix is 2 lines and is in Stage
   A**; the advance-then-retreat animation is 30-40 lines of new keyframe work
   for **2 plays in the entire feed**. **Recommend deferring** the choreography.
8. **B1 / A2 dependency** - **resolved, no dependency.** `throwHtml` originates
   at the ball's landing point, not the fielder anchor (F12), so removing the
   fielder token cannot break the throw's origin. B1 ships first, standalone.
9. **New: inside-the-park home run rate (C6)** - measured at **23%** on real
   data against a 0.9% budget, and **the uniform fence cannot be tuned to
   "rare"** (6 of 39 home runs are pinned at `depth_min` by the `q` clamp, so
   the rate is binary: 0% or >=15%). Two options: C6-a, a one-line band edit to
   0%; C6-b, restore the original per-angle fence profile for 3%.
   **Recommend C6-b (330 down the lines / 375 to centre)** - but it reverses
   Alex's "uniform fence for simplicity" call, so it needs confirming, not
   assuming.
10. **New: `single` archetype recalibration (C5)** - a concrete tested band
    (`6,20,...,130,230`) that takes infield-dirt landings from 47% to 0%. Data
    file only, but it changes how every single in the feed looks, so it deserves
    a look before it ships.

---

## Validation

1. **Compile/boot:** `python -m py_compile key_moments_build.py utils.py
   compute_result_diff_bands.py`; `esprima` parse of `docs/js/app.js`.
2. **Backend additivity:** rebuild; diff the `meta.json` key set - exactly two
   new keys (`result_short`, `flight.no_pa`), no removals, no type changes.
   `plays_*.json` unchanged.
3. **Exclusion-list check:** assert every result code present in the feed is
   either in `meta.flight.excluded` or has a `meta.flight.bands` row. This is the
   check that would have caught F7 (`CS3`).
4. **Pure-function tests:** `ball_flight_test.py` as extended in Stage T,
   green.
5. **Throw-target sweep:** for every play in the feed, assert
   `outThrowTargets` returns a base list whose length never exceeds
   `outs_after - outs_before`, and never contains a base twice. That is the
   ground-truth invariant restated for throws.
6. **Ground-truth invariant sweep:** unchanged from
   `ball-flight-plan.md` - no non-HR may clear the fence. Must still pass after
   the C5/C6 band edits.
7. **Real-play spot check:** one play per shape in F1/F3's tables, watched. This
   is what catches a band being wrong rather than the code being wrong.
8. **Reduced motion, dark mode, phone** - Stage T3.
9. **Dwell re-tune:** B5's tag-up delay pushes the slowest sac fly to ~2280ms.
   Confirm nothing is mid-animation at 3600ms routine dwell. If Decision D-1
   (dynamic zoom) is picked instead of D-2, re-time from scratch - the camera
   move adds to every play.

---

## Recommended build order

Six batches, each independently reviewable. Batches 1-3 are all low-risk and
touch nothing Stage A touches, so they can ship while the open questions are
still open.

**Batch 1 - copy and one-line fixes.** No behaviour risk, all visible.
- I1 (recap copy), I2 (`END 5th`/`MID 5th` + `ordinal`), I3 (stacked title),
  I4 (drop timezone), I5 (parens to dots), I6 (third out dot), H1 (dark-mode
  ring), B4 (`fill-mode: both`).
- B4 is in this batch deliberately: it is one word, it fixes Item 19 outright,
  and it also removes the 150ms token flicker on every play.

**Batch 2 - remove and defer.** Pure subtraction.
- B1 (fielder token flag off), B2 (no batter token on steals, plus the
  `FLIGHT_EXCLUDED` fix for `CS3`/`CS4`/`SB3`/`SB4`).

**Batch 3 - calibration.** Small edits, visible on every play.
- C5 (`single` band) - one line in `ball_flight_archetypes.csv`, ships
  immediately.
- C6 - **blocked on open question 9.** C6-a is a one-line band edit; C6-b also
  touches `fenceAt`/`fencePathD`. Do not ship either until Alex picks.
- **Get sign-off on this batch** - it changes how the whole feed looks, and it
  is far cheaper to revisit now than after Stage A is layered on top.

**Batch 4 - throw logic.** The interconnected core; build as one unit.
- A1, A2, A3, A4, A5, B6's throw half, I8 (partial advance for out-bound
  runners). Tests in Stage T land with this batch, not after it.

**Batch 5 - timing and landing visuals.**
- B5 (sac-fly tag-up, **including the `scoreArrivals` fix**), I7 (deferred base
  highlight), C1 (red/green ball), C2 (short label), C3 (HR distance).
- C4 (rollout) joins this batch **only if open question 5 is answered yes**.

**Batch 6 - blocked on Alex.** Nothing here starts before its question is
answered.
- Stage D (field scale) -> then F1 (pitch/swing wheel), which depends on D's
  coordinates.
- B3 (CS/SB visual), G1 (swipe boundary).
- B6's choreography half, if Alex overrules the recommendation to defer it.

E2 and E3 produce no code in any batch - they are written up above and end
there.

# Ball flight, fielding, and out-choreography for Play Scenes - implementation plan

## Why

The Play Scene (`catch-me-up-plan.md` Part 2) currently animates runners moving
around a 200x200 infield diamond. What it never shows is **the ball**: a home
run and a groundout look identical apart from where the tokens end up. Alex
wants a batted ball to visibly leave the bat on a trajectory derived from the
actual pitch/swing numbers, land somewhere real, be fielded, and - where
someone is out - for that out to be legible as a person leaving the field
rather than a token quietly fading.

This is a **purely cosmetic staging layer**. Every result is already fully
determined upstream by the pitch/swing diff mechanic and the `plays` table.
See "Ground-truth invariant" below - it is the single most important constraint
in this document.

Project constraints that apply throughout (existing repo rules):
- No em dashes in any `.py` file - hyphens only.
- No Co-Authored-By trailer if committing.
- `docs/` is the static GitHub Pages site. No Streamlit UI changes anywhere in
  this plan.

---

## Resolved decisions (do not re-litigate)

Worked out interactively with Alex; restated here so Sonnet has them in one
place. Do not re-derive or "improve" any of these.

1. **Scope.** Only batted balls animate. Excluded, keeping today's
   diamond-only behaviour unchanged: `K, BB, IBB, AutoBB, AutoK, CS, CS2, SB,
   SB2, AutoSB, Balk, pAuto, bAuto`. (`CS2` is added to Alex's list - see
   "Verified findings" #2.)
2. **The pitch/swing -> flight math** in "Stage 3" is exact and hand-verified.
   In particular the horizontal axis uses the **last single digit** (mod 10).
   Widening that modulus or dropping `swing` from the formula were both tried
   and explicitly rejected: for low-diff results like HR, any wider modulus
   forces the digit-delta toward zero and clusters every home run at ~45
   degrees.
3. **Launch angle is deliberately correlated with contact quality.** A real
   barrel does cluster near an optimal launch angle. Do not "decorrelate" it.
4. **Distance never comes from the ballistic range formula.** `EV^2 *
   sin(2*LA)/g` overshoots by roughly 2x because it ignores drag (a 109.9mph /
   30 degree drag-free calculation lands near 700ft against a real ~400ft
   expectation). Distance comes from the archetype depth band. The drag-free
   projectile formula is used **only** for hang time, where it is fine.
5. **Fielders are generic.** Nine fixed anchor points, nearest-to-landing wins.
   No names, no lineups, no per-play defensive alignment. That data does not
   exist and is not in scope.
6. **One throw maximum in v1**, to the base the out actually occurred at,
   derived from the existing `deriveRunnerMoves`/`obc_before`/`obc_after`
   logic. Double plays get one throw, not two-throw choreography. Deferred
   deliberately.
7. **Generic rounded outfield wall.** No per-stadium geometry.
8. **Accessibility and pacing follow existing precedent.** Every new animated
   piece extends the existing `prefers-reduced-motion` block; dwell constants
   get another tuning pass.

### Ground-truth invariant (state this in the PR, it is checkable)

`result`, `obc_before`, `obc_after`, `runs`, `outs_before`, `outs_after` are
inputs. Nothing in this feature may change, override, or contradict them. The
flight layer only decides **how** to stage an outcome that already happened.

One concrete place where this is easy to violate, addressed in Stage 4d:
- **A non-home-run whose landing point clears the fence.** A ball that leaves
  the park in the air is a home run by definition, so this genuinely
  contradicts the data and must be prevented.

The mirror case - **a home run that lands short of the fence - is not a
violation.** Per Alex: that is an inside-the-park home run, which is a real
outcome and already staged correctly by the existing runner animation (a
home-run batter's four-leg trip round the bases). Do not clamp it, and do not
treat it as an error. See Stage 4d.

---

## Verified findings that change the shape of this work

I checked these against the live repo and the shipped feed. Two are blockers
the source prompt did not account for.

**1. `pitch` and `swing` never reach the JSON - this blocks the entire
feature.** `key_moments_build.py:611-612` reads `pitch`/`swing` off the sheet,
computes `diff` from them, and then **only `diff` is emitted**. Decision 6 in
the prompt asks for `hand` to be threaded through; that is necessary but not
sufficient. Stage 2 must thread **three** fields: `pitch`, `swing`, and the
batter's `hand`. Without `pitch`/`swing` client-side there is no launch angle
and no horizontal angle at all.

**2. `CS2` is in the feed and is not in either of Decision 1's lists.** The
exclusion list names `CS`; the actual data contains `CS2` (n=11) and no `CS`.
It is a caught-stealing variant and plainly belongs in the exclusion list. I
have added it above. Every other result code in the feed maps cleanly to a
Decision 3 archetype - checked all 26 distinct codes, zero unmapped.

**3. Every in-scope play already has the numbers it needs.** 1051 of 1094 feed
plays carry a non-null `diff`. The 43 that do not are exactly the `SB2` (32)
and `CS2` (11) rows, both excluded from flight anyway. So the
missing-pitch/swing fallback path should exist for safety but will not fire on
real data today.

**4. Batter hand is `R` / `L` / blank - no switch hitters in the roster tab.**
Real distribution: R 2261, L 1851, blank 18. `utils.py:6189` does handle an
`"S"` value elsewhere in the app, so the loader must not assume it cannot
appear. Stage 3 specifies the fallback.

**5. The depth bands overlap the fence in both directions - but only one
direction is a problem.** Measured against candidate fence distances:

| Fence | Home runs landing short | Non-home-runs clearing |
|---|---|---|
| 330 ft | none | 3B, 2B and FlyBall all reach past it |
| 365 ft | none | 3B and FlyBall still clear |
| 400 ft | the 370-390 end of the band | none |

**Home runs landing short is now a resolved non-issue** - those are
inside-the-park home runs (Alex). That frees the fence to sit at a realistic
distance rather than being forced under 370 ft to keep every home run over it.

**Non-home-runs clearing the fence is still a real contradiction** and still
needs the one-directional clamp in Stage 4d. It bites hardest near the foul
lines, where the wall is shortest: a 350 ft double or a 380 ft flyout pulled
down the line would otherwise sail over a 330 ft corner.

---

## Naming / new files

| Thing | Name |
|---|---|
| Diff-percentile band generator | `compute_result_diff_bands.py` (repo root, sibling of `compute_situational_result_frequencies.py`) |
| Diff-percentile band output | `result_diff_bands.csv` |
| Archetype band table | `ball_flight_archetypes.csv` (hand-authored, version-controlled, **not** generated - see Stage 1b) |
| Loaders | `utils._load_result_diff_bands()` -> `_DIFF_BANDS`, `utils._load_flight_archetypes()` -> `_FLIGHT_ARCHETYPES` |
| Wire format | both tables emitted into `docs/data/meta.json` under `meta.flight` |
| New per-play JSON fields | `pitch`, `swing`, `batter_hand` |
| New JS module-level functions | `flightParams(play, tables)`, `signedCirc(a, b, mod)`, `landingPoint(D, angle)`, `nearestFielder(x, y)`, `clampToFence(D, angle, isHR)` |
| New JS render functions | `sceneFieldHtml(m, flight)` (replaces `sceneDiamondHtml`), `ballFlightHtml(flight)`, `fielderTokensHtml(flight)`, `throwHtml(flight, moves)` |
| New JS out-choreography | extends `deriveRunnerMoves` consumers only - see Stage 6 |
| Reused, untouched | `deriveRunnerMoves`, `basepathWaypoints`, `RUN_LEG_MS`, `slideDwell`, `mountSlide`, the leverage meter, the WP ribbon, `utils.circular_diff`, `utils.circular_signed_delta` |

---

## Stage 1 - the two static data assets

### 1a. `compute_result_diff_bands.py` -> `result_diff_bands.csv`

Follows the existing generator convention exactly
(`compute_situational_result_frequencies.py` is the closest sibling: Supabase
fetch, filter, group, write CSV, print a sample-size summary).

1. `plays = database.get_all_plays(league="MLN")`.
2. Filter to `play_type == 'Swing'` and rows with non-null `pitch` and `swing`.
3. `diff = utils.circular_diff(int(pitch), int(swing))`.
4. Group by `result`. For each group compute `n`, `p10`, `p90`.
5. **Low-sample fallback.** Any result with `n < 30` does not get its own band;
   it inherits its archetype's **pooled** band (all rows across every result in
   that archetype, pooled first, then the percentiles taken). Emit the row
   anyway with `source="archetype"` so the fallback is visible in the data
   rather than implied. Known members today: `B1BWH` (n=1), `FCLead` (n=2),
   `SacB` (n=4), `BGO` (n=4), `DP32` (n=6).
6. Print per-result `n` so the fallback set is auditable each run.

Shape - one row per result code, `obc`-free (unlike `result_ranges_re24.csv`,
this table has no situational dimension):

```
result,archetype,n,band_lo,band_hi,source
GO,grounder,1841,180,470,own
SacB,bunt,4,150,455,archetype
```

`--season-start` flag defaulting to `6`, matching
`compute_situational_result_frequencies.py`, so the band reflects current play
rather than the whole history.

### 1b. `ball_flight_archetypes.csv` - hand-authored, not generated

**Open question 4 resolved:** two files, not one. The diff bands are derived
from data and get regenerated; the archetype bands are a tuning surface Alex
will want to edit by hand after watching plays animate (Open Questions 2 and 3
below both land on this file). Mixing a generated table with a hand-tuned one
in a single CSV means a regeneration run silently reverts tuning.

```
archetype,la_min,la_max,ev_min,ev_max,depth_min,depth_max
grounder,-15,8,70,100,60,150
bunt,-10,5,20,45,5,25
infield_single,-10,8,60,85,45,90
line_drive,8,20,90,105,120,250
pop_up,50,75,60,85,40,120
fly_ball,25,50,80,98,250,380
single,-5,10,75,98,60,160
double,12,25,92,104,300,350
triple,15,30,95,108,340,380
home_run,25,35,98,115,370,420
```

The result-to-archetype mapping lives in `result_diff_bands.csv`'s
`archetype` column (written by the generator from a dict in that script), so
there is exactly one place the mapping is defined.

`TP` -> `grounder` is **low confidence** (n=11, median diff ~497, i.e. the
worst-contact end of the range). Keep it in grounder for v1 and note it in the
generator's docstring so a future reader knows it was a judgement call, not a
measurement.

### 1c. Loaders

Mirror `_load_re24_ranges()` in `utils.py` exactly - module-level dict,
`FileNotFoundError` is a silent no-op, called once at import alongside the
other `_load_*` calls:

```python
_DIFF_BANDS: dict[str, dict] = {}          # result -> {archetype, band_lo, band_hi, source}
_FLIGHT_ARCHETYPES: dict[str, dict] = {}   # archetype -> {la_min, ..., depth_max}
```

`key_moments_build.py` reads both through `utils` and emits them into
`meta.json` under a single `flight` key, so `app.js` gets them the same way it
already gets `result_labels`, `tag_labels` and `bases_svg`:

```json
"flight": {
  "bands": {"GO": {"archetype": "grounder", "lo": 180, "hi": 470}, ...},
  "archetypes": {"grounder": {"laMin": -15, ...}, ...},
  "excluded": ["K", "BB", "IBB", "AutoBB", "AutoK", "CS", "CS2", "SB", "SB2", "AutoSB", "Balk", "pAuto", "bAuto"]
}
```

Emitting the exclusion list as data rather than duplicating it in JS means
Decision 1's scope has one definition.

---

## Stage 2 - backend threading (blocker, do this before Stage 3)

Three fields, all additive. Existing consumers of `plays_*.json` read by name,
so nothing breaks.

**2a. `pitch` / `swing`.** `build()` already has them on the raw row
(`key_moments_build.py:611`). Carry them onto the emitted play dict in the same
block that emits `diff`. Coerce to `int` or `None` - never a string.

**2b. `batter_hand`.** `_player_view` (`key_moments_build.py:286`) returns
`{id, name, last_name, rookie, team}`. Add `hand` to that dict, read from
`p.get("hand")` on the roster row (already loaded by
`utils.read_mln_players_from_sheet`, just never surfaced). Then emit
`"batter_hand": feat["batter"]["hand"]` alongside the existing
`batter_id`/`batter_name`.

Adding `hand` to `_player_view` rather than writing a batter-specific lookup
keeps one player-shaping function. It does mean `pitcher`/`runner`/`catcher`
views also carry a `hand` they do not use - harmless, and it leaves the door
open for a pitcher-hand-dependent refinement later without another backend
change.

**2c. Normalisation.** Blank/missing hand (18 players today) and any
unexpected value must resolve to `"R"` at the point of use, not at build time -
keep the raw value in the JSON so the data stays honest. If `"S"` ever appears,
resolve it against the pitcher's hand (bat opposite, the standard convention,
and what `utils.py:6189` already assumes) - specify this in the JS helper, not
the builder.

**Regression check for this stage:** `plays_*.json` gains exactly three keys;
`docs/js/app.js` renders unchanged before any Stage 4 work lands.

---

## Stage 3 - the flight-parameter computation

A pure function group in `app.js`, no DOM, no state. Everything below is
deterministic given `(pitch, swing, diff, result, hand)` plus the two tables.

```js
function signedCirc(a, b, mod)            // generalises utils.circular_signed_delta
function firstTwo(v)                      // Math.floor((v - 1) / 10)      0..99
function onesDigit(v)                     // (v - 1) % 10                  0..9
function effectiveHand(batterHand, pitcherHand)
function flightParams(play, tables)       // -> null when out of scope, else the object below
function landingPoint(D, angleDeg)
function clampToFence(D, angleDeg, isHomeRun)
function nearestFielder(x, y)
```

`flightParams` returns:

```js
{ la, ev, distance, angle, x, y, hangMs, isGrounder, fielder, archetype }
```

### 3a. The math, verbatim

```
dLA    = signedCirc(firstTwo(pitch), firstTwo(swing), 100)    // -50..50
bucket = signedCirc(onesDigit(pitch), onesDigit(swing), 10)   // -5..5

pLaunch = (1 - dLA / 50) / 2                                  // 0..1
LA      = laMin + pLaunch * (laMax - laMin)

frac  = bucket / 5                                            // -1..1
angle = hand === "L" ? 45 - frac * 40 : 45 + frac * 40         // 5..85 degrees
                                                              // 0 = 3B line, 90 = 1B line

q  = 1 - clamp((diff - bandLo) / (bandHi - bandLo), 0, 1)
EV = evMin + q * (evMax - evMin)
D  = depthMin + q * (depthMax - depthMin)

offset = angle - 45
x = D * sin(offset in radians)                                // -x toward 3B
y = D * cos(offset in radians)

isGrounder = LA < GROUND_LA_THRESHOLD                         // 4 degrees, see Open Q 2
hangMs     = isGrounder ? null : 1000 * 2 * (EV * 1.4667) * sin(LA rad) / 32.2
```

`bandLo`/`bandHi` come from **the specific result's own row** in
`result_diff_bands.csv`, never the archetype-pooled band (except where the
generator already substituted it for a low-sample result, which is invisible
here by design).

### 3b. Worked examples for Sonnet to encode as tests

I computed these directly from the formulas above. They assume the archetype
bands in Stage 1b and the stated diff bands. Encode them as exact assertions
(round to 2dp).

| Case | pitch | swing | hand | result | band | Expect |
|---|---|---|---|---|---|---|
| HR, pulled by a RHH | 407 | 412 | R | HR | 2-21 | `dLA=1, bucket=-5, LA=29.90, angle=5.00, q=0.8421, EV=112.32, D=412.1, x=-264.9, y=315.7, hangMs=5100` |
| Same numbers, LHH | 407 | 412 | L | HR | 2-21 | identical except `angle=85.00, x=+264.9` |
| Groundout, RHH | 150 | 631 | R | GO | 180-470 | `dLA=49, bucket=1, LA=-14.77, angle=53.00, q=0, EV=70.00, D=60.0, isGrounder=true, hangMs=null` |
| Flyout, RHH | 220 | 268 | R | FO | 55-240 | `dLA=5, bucket=-2, LA=36.25, angle=29.00, q=1, EV=98.00, D=380.0, x=-104.7, y=365.3, hangMs=5279` |
| Single, LHH | 888 | 801 | L | 1B | 20-150 | `dLA=-8, bucket=3, LA=3.70, angle=21.00, q=0.4846, EV=86.15, D=108.5, isGrounder=true` |

Plus these boundary assertions, which are cheap and catch the errors most
likely to be made:

- `signedCirc(0, 5, 10) === 5` and `signedCirc(5, 0, 10) === -5`. **Both are
  reachable.** A ones-delta of +5 and -5 is the same distance around a 10-wheel
  but maps to opposite foul lines (5 degrees vs 85 degrees). This is not an
  artifact - `signedCirc` resolves the tie by anchoring on `a` and taking
  whichever direction does not cross the wheel's wrap boundary, which is
  exactly the rule (`ball-flight-refinements-plan.md` Finding F14, verified
  exhaustively over every tie pair on both the mod-10 and mod-100 wheels).
  Assert it so the rule is locked in, not just accidentally true.
- `signedCirc(98, 2, 100) === 4` (wraps forward), `signedCirc(2, 98, 100) === -4`.
- `firstTwo(1) === 0`, `firstTwo(1000) === 99`, `onesDigit(1) === 0`,
  `onesDigit(1000) === 9`.
- `flightParams` returns `null` for every code in `meta.flight.excluded`, and
  for any play where `pitch` or `swing` is null.
- `q` clamps: a diff below `bandLo` gives `q === 1`, above `bandHi` gives
  `q === 0`. Neither may produce an out-of-band EV or distance.

**How to run these.** This repo has no JS unit runner. The established pattern
here is a Playwright harness that loads the page and calls the pure functions
through `page.evaluate` - the same approach used to verify `deriveRunnerMoves`,
the basepath geometry and the cursor-capping logic. Write
`ball_flight_test.py` in that style. It needs the functions reachable, so
expose them on a small namespace object (e.g. `window.KMFlight`) **or** accept
a temporary test hook that is stripped before shipping, matching the existing
convention.

---

## Stage 4 - the field canvas

### 4a. Geometry (my proposal, not a relayed decision - Open Question 1)

The current 200x200 viewBox is cropped to the infield and cannot show outfield
depth. Replace with a **400x360** viewBox, home plate at `(200, 330)`, and a
**1 SVG unit = 1.25 ft** scale, so 420 ft of depth maps to 336 units and fits
with margin.

Existing infield anchors rescale to keep the current visual proportions:

| Anchor | Today (200x200) | Proposed (400x360) |
|---|---|---|
| Home | (100, 170) | (200, 330) |
| 1B | (158, 110) | (272, 258) |
| 2B | (100, 50) | (200, 186) |
| 3B | (42, 110) | (128, 258) |

Nine generic fielder anchors (`P, C, 1B, 2B, 3B, SS, LF, CF, RF`) placed at
conventional depths, expressed in **field-plane feet** so `nearestFielder` can
compare against `(x, y)` directly without a coordinate conversion:

```
P   (0, 60)     C  (0, -5)
1B  (75, 85)    2B (40, 145)    SS (-40, 145)   3B (-75, 85)
LF  (-200, 260) CF (0, 320)     RF (200, 260)
```

Everything renders through one `ftToSvg(x, y)` helper. Sonnet should keep that
helper the single conversion point - the existing code's bug history is mostly
coordinate-space confusion (see the WP-ribbon marker in
`catch-me-up-plan.md`'s polish round, which resolved percentages against the
wrong box twice).

### 4b. The fence

Rounded generic wall, drawn as a single SVG arc. Proposed profile: **330 ft
down each line, 375 ft to dead centre**, interpolated as
`centre - (centre - lines) * (|angle - 45| / 45) ** 1.15`. Rendered once, not
computed per play, except by `clampToFence`.

**Centre-field depth is now a tuning dial, and it sets how often an
inside-the-park home run happens.** With the HR depth band at 370-420, any
bearing where the wall is deeper than 370 can produce one. Sweeping the
reachable `(angle, q)` grid:

| Centre-field depth | Inside-the-park share |
|---|---|
| 365-370 ft | 0% - the path never fires |
| **375 ft** | **0.9% - rare, roughly matches reality** |
| 385 ft | 5.4% |
| 400 ft | 16.0% - far too common |

Real inside-the-parkers run well under 1% of home runs, so 375 ft is the
recommendation: rare enough to feel like an event, frequent enough that the
code path is genuinely exercised rather than dead. **400 ft (my first
instinct, and the realistic number for a real park) is wrong here** - it makes
one in six home runs an inside-the-parker, which would read as a broken
animation rather than a rare thrill.

If Alex prefers a deeper, more realistic centre field, the honest lever is to
raise the HR band's `depth_min` in `ball_flight_archetypes.csv` to sit just
over the wall, not to re-introduce the clamp.

### 4c. Layering

Bottom to top: field/grass, fence, infield dirt, base plates, batting-team
watermark, fielder tokens, ball trail, ball, runner tokens. Runner tokens stay
on top - they are what the viewer is following.

### 4d. Enforcing the ground-truth invariant (required - see Finding 5)

`clampToFence(D, angle, isHomeRun)` is **one-directional** and is not optional
polish:

- **Not a home run**: `D = min(D, fenceAt(angle) - 12)`. Nothing that is not a
  home run may ever clear the wall, because a ball leaving the park in the air
  is a home run by definition and that would contradict `result`.
- **Home run**: **no clamp.** Leave the computed distance alone. A home run
  that lands inside the park is an inside-the-park home run - a real outcome,
  not an error.

Apply it in `flightParams`, after the band lookup, before `landingPoint`. It is
a visual clamp only and changes nothing about the play.

**Inside-the-park home runs are a feature, and they mostly work already.** The
batter's four-leg circuit of the bases is existing `basepathWaypoints`
behaviour (`RUN_LEG_MS[4]`, 1700ms), so the runner side needs no new code. Two
things Sonnet should get right:

- The ball stays live in the outfield rather than disappearing at the wall, and
  the fielder converge (Stage 4e) still runs. On an over-the-fence home run the
  ball should instead clear and fade, with no fielder converge - the two cases
  visibly differ, which is the whole point.
- Optional and cheap: when `result === "HR"` and the ball did not clear, add an
  "inside the park" note beside the result pill (Stage 5's readout row is the
  natural home). Derivable from `D < fenceAt(angle)` with no new data. Worth
  doing - an inside-the-parker is rare enough that a viewer will otherwise
  assume the animation glitched.

Test-harness assertion: for every result code in the feed, sweep a grid of
`(pitch, swing)` pairs and assert **no non-HR ever clears the fence**. Do not
assert anything about home runs clearing - both outcomes are legal.

### 4e. Sequencing within one Play Scene

The existing runner tokens start moving at slide mount. The ball must lead
them, or the visual causality is backwards.

Proposed timeline, all pure CSS animations with `animation-delay` (matching the
existing scene, which deliberately has no JS timers - fresh elements per slide
restart their own animations):

| t (ms) | Event |
|---|---|
| 0 | Ball appears at home plate, trail begins |
| 0 - `hangMs` (capped, see below) | Ball travels to the landing point |
| land | Fielder token converges on the ball |
| land + 150 | Throw line draws to the out base, if there is an out |
| 150 | Runner tokens begin (existing `RUN_LEG_MS` behaviour, delayed by 150ms) |
| land + 400 | Outs choreography begins (Stage 6) |

**Hang time must be capped for animation, not used raw.** The worked examples
above produce `hangMs` of 5100 and 5279 - longer than the entire current
routine dwell (2800ms). Propose `flightMs = clamp(hangMs * 0.35, 450, 1400)`,
preserving relative differences (a towering fly still visibly hangs longer than
a liner) without blowing the dwell budget. Flag as tune-after-watching.

---

## Stage 5 - launch angle and exit velocity readout

Alex's addition: show LA and EV on the slide, next to the result.

`.scene-play-line` currently holds the result pill and the diff pill, centred,
directly beneath the outs tracker. Add a compact stat pair after the result
pill, in the same row, styled as muted monospace-ish chips so they read as
telemetry rather than as another tag:

```
[ Home Run ]  29.9°  112 mph
```

- Degrees to 1dp, mph to the nearest whole number - matching how Statcast
  broadcasts read, and enough precision to see two similar plays differ.
- Only rendered when `flightParams` returned non-null. A strikeout shows the
  result pill alone, exactly as today.
- Grounders still show both numbers, including a negative launch angle - a
  `-14.8°` groundout is informative, not an error to hide.
- Suppress on the mobile breakpoint only if it forces the result row to wrap;
  check during Stage 5's visual pass rather than pre-emptively.

This is the cheapest stage and the one that makes Stage 3 auditable by eye, so
it is deliberately sequenced early in the build order.

---

## Stage 6 - out choreography

Alex's addition: an out should read as a person leaving, not a token fading.

Today, `.rn.out .rn-inner` fades in place to `opacity: 0.18` and
`.rn.batter-out` fades in at home then dims. Replace both with a two-beat
sequence:

**6a. A runner put out**: token turns red in place (~250ms), then travels to
the dugout along a straight line (not a basepath - they are leaving the field),
fading out on arrival.

**6b. The batter, out on a batted ball**: runs **to first base** on the normal
basepath first (reusing `basepathWaypoints` and the existing `RUN_LEG_MS[1]`
timing), *then* turns red at first, *then* returns to the dugout. This is the
specific behaviour Alex asked for and it is what makes a groundout read
differently from a strikeout.

**6c. The batter, out without a batted ball** (strikeout and friends - the
Decision 1 exclusion list): straight from home to the dugout, no trip to first.
No ball flight on these plays either, so this is the whole animation.

**Dugout anchors** (my proposal): two, at `(-95, -25)` and `(95, -25)` in
field-plane feet - just foul of each line, behind the bases. The batting team
uses the **1B-side dugout when they are the home team**, 3B side when away.
That is a convention, not a fact about any real park; flag it as such and make
it a one-line constant Sonnet can flip.

**Reduced motion**: end state is "gone". `opacity: 0` at the dugout anchor,
no travel, no colour transition - consistent with how the existing block
handles the current out fade.

This stage touches `deriveRunnerMoves`' **consumers only**. Do not change
`deriveRunnerMoves` itself - its pairing heuristic and documented limitation
(tangled force plays can mis-assign which specific runner moved) are settled,
and the out choreography inherits that limitation unchanged. Worth stating in
the PR: on a rare tangled force play the *wrong token* may walk to the dugout.
That is pre-existing, not introduced here.

---

## Stage 7 - dwell and reduced motion

**7a. Dwell.** The scene now has ball flight, a fielder converge, a throw, and
a two-beat out walk stacked on top of the diamond, meter, ribbon, outs tracker
and score count-up. Current constants (`PLAY_DWELL_MS_ROUTINE = 2800`,
`PLAY_DWELL_MS_KEY = 5200`, `HALF_INNING_BONUS_MS = 800`) were last tuned
before any of this existed.

Starting proposal: routine **3600**, key **6000**. Both are guesses to be
validated by watching a real run, the same discipline every previous dwell
constant went through.

**Flag for Alex, not for Sonnet to resolve:** the Catch Me Up backlog scenario
already runs 39 minutes at the current constants for 656 plays. At 3600/6000 it
goes to roughly 48 minutes. This is a known, previously accepted tradeoff, but
each increase makes it sharper. Surface the new number when the tuning pass
lands rather than burying it.

**7b. Reduced motion.** Extend the existing
`@media (prefers-reduced-motion: reduce)` block to: the ball, its trail, the
fielder token, the throw line, and both out-walk animations. End state instantly
in every case - ball at its landing point, fielder there, throw line drawn,
out runners gone. Never hidden, never frozen mid-flight. The existing block
already establishes this shape; follow it literally.

---

## Open questions - flag, do not silently resolve

1. **Field canvas dimensions and the nine fielder anchors** (Stage 4a) are my
   proposal, not Alex's decision. Worth a look at the first rendered frame
   before Stage 4 is built out.
1b. **Centre-field fence depth** (Stage 4b) is a real product dial, not just
   geometry: it alone decides the inside-the-park home run rate. 375 ft is
   recommended for ~0.9%. Confirm that feels right after watching, and expect
   to revisit it together with the HR depth band rather than in isolation.
2. **Grounder roll duration and deceleration** is explicitly an animation-feel
   call, not a formula. Proposed: 600ms with an ease-out, distance-scaled.
   Tune after watching.
3. **The bunt band is the least-verified row** in the archetype table - lowest
   sample size, no Statcast anchor. Expect to adjust after watching real bunts.
4. ~~The `+5` / `-5` horizontal artifact~~ - resolved, not an artifact. See the
   corrected Stage 3b note above: `signedCirc`'s tie-break at the wheel's exact
   halfway point already implements the anchor-on-`a`/no-wrap rule Alex asked
   for (`ball-flight-refinements-plan.md` Decision E1, Finding F14).
5. **`TP` -> grounder** is a low-confidence placement (n=11).
6. **Hang-time scaling factor** (Stage 4e) - `0.35` is chosen to fit the dwell
   budget, not derived from anything.

---

## Validation

1. **Compile/boot**: `python -m py_compile compute_result_diff_bands.py
   key_moments_build.py utils.py`; `esprima` parse of `docs/js/app.js` (the
   existing JS syntax check in this repo).
2. **Band table correctness**: regenerate `result_diff_bands.csv`; assert every
   result code present in `docs/data/plays_*.json` and not in
   `meta.flight.excluded` has a row; assert every row's `archetype` exists in
   `ball_flight_archetypes.csv`; assert every `n < 30` row has
   `source="archetype"`.
3. **Pure-function tests** (Stage 3b) via a Playwright `page.evaluate` harness,
   including all five worked examples and every boundary assertion.
4. **Ground-truth invariant sweep** (Stage 4d): for every in-scope result code,
   sample a grid of `(pitch, swing)` pairs and assert **no non-HR clears the
   fence**. This is the check that proves the feature cannot contradict the
   data. Deliberately assert nothing about home runs clearing - over the fence
   and inside the park are both legal, so a test that required one would be
   wrong. Separately, confirm both HR cases are reachable in the sweep, so the
   inside-the-park path is known to be exercised rather than dead.
5. **Backend additivity**: rebuild, diff the JSON key set - exactly three new
   keys, no removals, no type changes on existing keys.
6. **Real-play spot check**: pick two real historical plays per archetype, run
   them through the scene, confirm the trajectory reads as that kind of batted
   ball. This is the check that catches a band being wrong rather than the code
   being wrong.
7. **Reduced-motion pass**: OS-level reduced motion on, step through several
   plays, confirm every new piece shows its end state instantly and nothing
   animates.
8. **Dwell re-tune pass**: watch a full run at the new constants; confirm no
   component is still mid-animation when the slide advances.
9. **Regression**: the excluded results (`K`, `BB`, `SB2`, `CS2`) render exactly
   as they do today; Catch Me Up's cursor/banner/pause/close and Game Replay's
   arrows, swipe and recap all still pass their existing suites
   (`catchup_test.py`, `p23_test.py`, `polish_test.py`, `cursor_cap_test.py`,
   `nav_test.py`).

---

## Recommended build order

Each step is checkable before the next depends on it.

1. **Stage 2 (backend threading)** first, alone. It is the blocker, it is
   small, and until `pitch`/`swing`/`batter_hand` are in the JSON nothing else
   can be verified against real plays. Ship and confirm the JSON shape before
   writing any flight code.
2. **Stage 1 (data assets)** - generator, both CSVs, loaders, `meta.json`
   emission. Verifiable on its own by inspecting the CSV and the built
   `meta.json`.
3. **Stage 3 (pure math) + Stage 5 (LA/EV readout)** together. The readout is
   the cheapest possible way to see the math working on real plays, before any
   trajectory rendering exists. **Get sign-off here** - if the numbers look
   wrong on real plays, that is far cheaper to fix now than after the canvas is
   built on top of them.
4. **Stage 4 (field canvas and ball flight)**, including the fence clamp.
   Biggest visual change; needs 1-3 correct underneath it.
5. **Stage 6 (out choreography)**. Independent of the ball flight - it could
   technically ship before Stage 4, and if Stage 4 stalls on geometry
   questions, pulling this forward is reasonable.
6. **Stage 7 (dwell and reduced motion)** last, once there is a complete
   sequence to time and to switch off.

Fielder convergence and the single throw (the tail of Stage 4e) are the most
deferrable pieces in the whole plan. If any stage runs long, cut those to a
follow-up rather than compressing the validation.

# Task: Plan a round of fixes and refinements to the ball-flight-and-fielding Play Scene

You are planning an implementation for **Sonnet** to execute. **Do not write
code.** Produce a staged implementation plan in the same style as
`ball-flight-plan.md` in this repo (resolved decisions restated and not
re-litigated, verified findings against the actual current code, staged
sections, an open-questions section, a validation section, a recommended
build order) - read that file first. It documents the feature this round is
refining, and its conventions (naming, doc structure, how "my proposal, not a
relayed decision" is flagged) are the ones to follow here too.

## Project context

Numberball is a Streamlit + Supabase scouting app (repo:
`C:\Users\Alex\PycharmProjects\numberball`) for a dice-less league where a
pitcher secretly picks an integer 1-1000 ("pitch"), a batter independently
picks one too ("swing"), and `diff = circular_diff(pitch, swing)` buckets
into a play result via a per-situation lookup table. **This mechanic is
load-bearing and out of scope to touch**, except where a decision below
explicitly asks you to change how `pitch`/`swing` map to ball-flight
*staging* parameters (launch angle, horizontal angle) - never the play
result itself. See "Ground-truth invariant," restated below, unchanged from
the original plan.

`docs/` is the static GitHub Pages site - a public game-recap feed with two
slideshow entry points, "Catch Me Up" and "Game Replay," built by
`key_moments_build.py` and rendered by `docs/js/app.js` + `docs/css/style.css`.
All work in this prompt lives in that static-site layer. No Streamlit UI
changes.

## What already shipped

`ball-flight-plan.md` was fully implemented (currently uncommitted working-
tree changes - `git status` shows `docs/js/app.js`, `docs/css/style.css`,
`key_moments_build.py`, `utils.py` all modified, plus the two new data files
`result_diff_bands.csv` and `ball_flight_archetypes.csv`). The Play Scene now
renders a real ball flight: `flightParams()` computes launch angle, exit
velo, distance and landing point from `(pitch, swing, diff, result, hand)`;
`sceneFieldHtml()` renders a 400x360-ish field canvas with a fence, nine
generic fielder anchors, a ball trail, a converging fielder token, at most
one throw line, and a two-beat out-choreography walk to the dugout layered
under the existing runner-token animation. Read `ball-flight-plan.md` in
full - every function named below (`flightParams`, `outThrowTarget`,
`ballFlightHtml`, `sceneFieldHtml`, `deriveRunnerMoves`, `basepathWaypoints`,
`dugoutFor`, `ftToSvg`, `signedCirc`) is defined there and already live in
`docs/js/app.js`.

This is now a **second pass**: Alex has watched the feature run against real
plays and has a list of specific fixes, calibration corrections, and small
new features. Every item below came directly from that review. Nothing here
is hypothetical or speculative except where explicitly marked as an open
question.

### Ground-truth invariant (still applies, unchanged)

`result`, `obc_before`, `obc_after`, `runs`, `outs_before`, `outs_after` are
inputs. Nothing in this feature may change, override, or contradict them -
including the formula change in Decision 9 below, which changes how a
result's flight is *staged*, never what the result *is*.

---

## Verified findings (checked against the current code, ground the decisions below)

1. **`outThrowTarget()` (`app.js:1237`) is the source of the flyout/popout
   phantom throw (Item 1).** When there's no runner forced out and the batter
   didn't reach base, it unconditionally returns `"1B"`. That branch exists
   for a real case (a routine groundout, batter out at first) but fires
   identically for a fly ball or pop-up the batter is simply caught on, where
   no throw happens in real baseball. Fix this by branching on the batted-ball
   archetype (airborne, caught in the air) rather than only on
   `batterReached`.
2. **`outThrowTarget()` picks exactly one base, always in the order
   `deriveRunnerMoves` happens to emit** (`moves.filter(...)[0]`), not "home,
   3rd, 2nd, 1st." For a double play this already produces *a* correct base
   (the play's actual out location per `obc_before`/`obc_after`), just never
   more than one, and with no guarantee it's the *lead* runner's base when
   `deriveRunnerMoves` has more than one OUT move to choose from. Item 8 wants
   every OUT move to get its own throw, thrown in true fielding order.
3. **Singles landing in the infield dirt is a real, measurable calibration
   gap (Item 2).** `ball_flight_archetypes.csv`'s `single` row is
   `la_min=-5, la_max=10, depth_min=60, depth_max=160`. The infield dirt
   boundary in the rendered field (`SCENE_BASES` in `app.js`, `BASE_DIST_FT`
   presumably 90, so 2B sits at `90*sqrt(2) ~= 127ft` from home) sits well
   inside that depth band - so a real fraction of `1B`/`1BWH`/`1BWH2` plays
   are landing on the dirt today. `IF1B` already exists as its own archetype
   (`infield_single`, depth 45-90) specifically for infield hits. The `single`
   archetype's launch-angle band (`-5` to `10`) is low enough to regularly
   produce short, grounder-adjacent distances that don't clear the infield -
   recalibration target for Decision 2.
4. **The "END INNING" badge (`app.js:376`) already has the data it needs.**
   `m.inning` (int) and `m.half` (`"top"`/`"bottom"`) are already on every
   play in `plays_*.json` (`key_moments_build.py:551-552`). No backend change
   needed for Item 4 - purely a JS/label change plus an ordinal formatter
   (`5th`, `1st`, `2nd`, `3rd`, `21st`, etc. - check whether one already
   exists in `app.js` before writing a new one).
5. **The Game Replay title slide (`app.js:1891-1904`, `kind === "replay-title"`)
   is a single flex row**: away logo, away name, `@`, home logo, home name,
   all inline. Item 6 wants this stacked: team 1 on its own line, `@` on its
   own line, team 2 on its own line. This is the `.catchup-title-teams` CSS
   layout (`flex-direction: row` today) plus possibly wrapping each
   logo+name pair in its own block - check `style.css` for `.catchup-title-teams`
   before deciding whether it's a pure CSS change or needs markup restructuring
   too.
6. **Timezone text (Item 20) - two separate call sites, only one is a
   description-yet-suppressible label.** `app.js:80-90` (`CHICAGO_TZ`,
   `timeZoneName: "shortOffset"`) computes the source data's own UTC offset
   for internal math, not display - leave it alone. `app.js:107-108`
   (`timeZoneName: "short"`) is the one producing a visible string like "CDT"/
   "PDT" next to displayed times; find every render call site that
   concatenates that string into user-facing text and drop it, while keeping
   the underlying `Intl.DateTimeFormat` call's implicit local-timezone
   conversion (i.e. still compute the time *in the viewer's zone*, just don't
   print the zone abbreviation).
7. **Parentheses-to-dot Item 34 has (at least) two call sites, not one.**
   `"▶ Catch Me Up (" + count + ... + " new play)"` at `app.js:856`, and a
   second, described but not yet located by me, in the mobile filter panel's
   collapsed-state active-filter count (`activeFilterCount()` is defined at
   `app.js:652` and used near `app.js:666` - confirm the render call site
   uses parens before changing it). Find every other `(...)` count/qualifier
   pattern in `app.js` while you're in there rather than fixing only these two
   - Alex said "couple places I can think of," implying there may be others
   uncaught by this review.
8. **The 3rd-out badge (Item 25) already lives right next to the two dots it
   should match.** `sceneOutsHtml()` (`app.js:1672-1686`) renders
   `<span class="out-dots">` for outs 1-2, then a separate
   `<span class="out-third">3rd</span>` in red text when `after >= 3`. Replace
   that span with a third `.out-dot` sharing the same markup/animation as the
   first two (on/fresh classes), not a new element type.
9. **The pre-runner-move gold base highlight (Item 26) is
   `.dm-base.on` (`app.js:1338-1345`, styled `style.css:481`)**, which paints
   *post-play* occupancy (the `after` OBC string) onto the base plates at
   slide mount, running concurrently with - and visually ahead of - the
   runner tokens that are still animating toward those bases. This is a
   different element from the runner tokens (`.rn`) entirely; removing or
   deferring the gold fill doesn't touch runner-token logic.
10. **The fielder-converge token (Item 15) is `fielderTokensHtml()`
    (`app.js:1216-1228`)**, a circle that travels from its fixed anchor to the
    ball's landing point, timed to arrive at `ballTravelMs(flight)`. This is
    exactly the "dot moving in the outfield" Alex is asking about - it's a
    real, working piece of Stage 4e, not a bug. Alex wants it *removed* (not
    fixed) for now, per the item - flag this in your plan as a scope
    reduction (deferring, not deleting the underlying capability) so it's easy
    to re-enable if the throw-line work in Decision 3 later wants a visible
    fielder to throw *from*.
11. **The batter-out-at-first case (Item 12) already runs to the base first**
    (`app.js:1313-1324`, the `out-to-first` class: batter travels home->first
    on the real basepath, *then* turns out at first, *then* returns to the
    dugout - this is Stage 6b from the original plan and already does what
    Item 12 asks, for the batter specifically). **Item 27 ("show base runners
    partially advancing…") is the batter-out case's *missing sibling* for
    every other forced/tag-out runner** - `deriveRunnerMoves`' OUT-bound moves
    today go straight to the dugout with no partial travel toward the base
    they were forced to. Items 12 and 27 are the same fix applied to two
    different move types; scope both together.
12. **Result labels for the new small marker tag (Item 17) already exist** -
    `RESULT_LABELS` (`key_moments_build.py:73`) is already threaded into
    `meta.json` per `result_labels` and used elsewhere in `app.js`. Reuse it
    rather than inventing a second abbreviation table; check whether its
    existing values are display-length-appropriate for a small on-field
    marker (e.g. is `"Home Run"` too long where `"HR"` is wanted) - if not,
    this may need a second, terser variant.
13. **The ball marker (`ballFlightHtml`, `app.js:1195-1214`) already carries
    an `.air`/`.ground` and `.clear`/`.land` class split but nothing tied to
    out-vs-hit (Item 24).** Adding a red/green distinction is an additional
    class, driven by whatever `flightParams`/the caller already knows about
    whether this play's `result` was an out - that classification already
    exists (it's exactly the diamond-only-animation exclusion/out logic
    elsewhere), just not threaded to this function today.

---

## Decisions (resolved with Alex - build to these, do not re-derive)

Organized by theme, not by Alex's original numbering. Each restates the
original ask; several are elaborated with the verified-findings context
above.

### A. Throw logic and out-attribution

**A1. No throw on a routine fly out or pop out (Item 1).** Update
`outThrowTarget()` so a caught fly ball / pop-up with no runner forced out
returns no target (no throw line), instead of defaulting to `"1B"`. A sac
fly, where a runner *is* tagging and a throw could matter, is unaffected by
this change since it already has an OUT-bound move.

**A2. Multiple throws on double/triple plays, always in lead-runner order
(Item 8).** Every OUT-bound move in `deriveRunnerMoves`' output gets its own
throw line, not just the first one found. Sequence them in the order **home,
3rd, 2nd, 1st** (i.e. always try to get the lead runner first, same as real
defensive priority), while the *which specific bases get outs* still comes
entirely from `obc_before`/`obc_after`/`result` (the existing, unmodified
ground truth) - this decision changes only the throw *choreography order and
count*, never which runners are out. `import_BRC.csv` (loaded into
`_BRC_RUN_LOOKUP` in `utils.py`, consulted for run/OBC transitions) is the
existing source of truth for what actually happened on a play; nothing new
needs to be read from it; the point is that the throw sequence must never
contradict the OUT moves `deriveRunnerMoves` already derives from it.

**A3. Fielder's Choice throws to the correct base (Item 9).** `FC`, `FC3rd`,
`FCH`, `FCLead` each have a specific intended out location baked into the
result code itself (e.g. `FCH` = fielder's choice, throw home). Verify
`outThrowTarget()` (post-A1/A2 changes) resolves each of these four codes to
the base their name implies, not just whatever `deriveRunnerMoves` happens to
report as `runnerOut.from + 1`. `DPH1`/`FCH` already get a `"HOME"`
special-case (`app.js:1240`) - audit whether `FC3rd`/`FCLead` need the same
explicit treatment or whether the generic "next base past the runner's `from`"
logic already happens to land correctly for them; state which in your plan
rather than assuming.

**A4. Groundout throw timing beats the runner (Item 11).** Currently the
throw line and the batter's run to first both key off `ballTravelMs(flight)`
and existing delay constants (`THROW_DELAY_MS`, `RUN_LEG_MS`). Ensure that for
a groundout specifically, the throw's arrival at the base is timed to precede
the batter/runner's arrival at that same base, not just visually close. This
is a dwell/delay-constant tuning task, not new logic - identify exactly which
constants control each side of the race and propose values, flagged
tune-after-watching like every other timing constant in this codebase.

### B. Choreography Alex wants removed or changed

**B1. No fielder-converge dot for now (Item 15, Finding 10).** Remove (or
feature-flag off, your call - state which and why) `fielderTokensHtml()`'s
render call. Do not delete the function outright if A2's multi-throw work
wants a `from` point for each throw line to originate from a fielder-shaped
position instead of the raw landing point - check whether `throwHtml()`
currently uses the fielder anchor or the ball's landing point as its `from`
before deciding whether removing the visible token breaks the throw's visual
origin.

**B2. No "batter walks to the dugout" on stolen base attempts (Item 7).**
`SB`, `SB2`, `AutoSB`, `CS`, `CS2` are all in the flight-excluded set already
(no ball flight renders), but the *diamond-only* animation may still be
routing the batter through a dugout-walk if `deriveRunnerMoves` treats a
caught-stealing out as a `BATTER`-origin move by mistake, or if the batter
token is being rendered at all on these plays. Confirm exactly what currently
draws for a `CS`/`CS2` play and remove whatever incorrectly moves the batter -
the batter didn't do anything on this play, they should stay exactly where
they already were (on-deck/at the plate), never travel.

**B3. Caught stealing / stolen base should "show up nicer" (Item 3).**
Vaguest item on the list - Alex did not specify a concrete visual. Propose 2-3
concrete options in your plan (e.g.: an explicit runner-token race from the
stolen base to the next one with a tag animation at the bag; a small
"SB"/"CS" chip riding along the base path; a distinct color/motion for a
stolen-base attempt vs. every other runner move) and flag this as needing
Alex's pick before Sonnet builds it - do not silently choose one.

**B4. Strikeouts shouldn't start with the batter already in the dugout
(Item 19).** Check where the batter token is first rendered/positioned for a
`K`/strikeout play - if it mounts already at (or animating from) the dugout
anchor instead of starting at the batter's box/home like every other play,
that's the bug. It should read the same as any other play's *start* state;
only the *end* state (out, no ball in play) differs.

**B5. Sac fly waits for the catch before the runner advances (Item 28).**
Today, does the tagging runner's advance-leg animation begin at slide mount
(same `runDelay`/`RUNNER_LEAD_MS` as every other safe-runner move) or does it
already respect `ballTravelMs`? Verify current behavior first. If it starts
early, delay the tagging runner's leg specifically until
`ballTravelMs(flight)` (the moment of the catch) on `SacF`/`DSacF`/`DFO`
results only - every other runner-move timing is unaffected. Flag as the
harder of the timing items, since it requires per-runner (not per-play)
delay logic where today `runDelay` is a single play-wide constant.

**B6. LODP/LOTP choreography (Item 29) - Alex flagged this as possibly
tough.** A line-out double/triple play needs: the fielder catching the ball,
the runner(s) who left their base shown having advanced partway, then a
throw back to their *original* base (not the base they were advancing to) to
get them out for leaving early. This is a genuinely different shape from
every other out on the list (the out base is *behind* the runner's direction
of travel, not ahead of it) - do not fold it into A2's generic
lead-runner-order throw sequencing without checking whether that generic
logic produces the right base here. If it's materially more complex than the
rest of this plan, say so explicitly and propose scoping it as a follow-up
rather than quietly shipping a half-correct version.

### C. Ball landing visuals

**C1. Landing dot color: red for an out, green for a hit (Item 24, Finding
13).** Thread an out/hit boolean into `ballFlightHtml()` (or its caller) and
add the class.

**C2. Small result-abbreviation label next to the landing marker (Item 17,
Finding 12).** e.g. `1B`, `FO`. Reuse `RESULT_LABELS`/`meta.result_labels`
if its values are short enough for on-field use; otherwise propose a second,
terser label set and say so explicitly rather than silently truncating
existing long labels.

**C3. Distance label next to HR landing points only (Item 22).** Not for any
other result. Feet, presumably matching `flight.distance`'s existing units -
confirm no unit conversion is needed given `FT_PER_UNIT`/`ftToSvg` already
work in feet internally.

**C4. Contact-to-field trajectory plus some rollout treatment (Item 16).**
Alex's ask: "on singles and other hits, show point of contact to field, but
also rollout somehow." The current `ballFlightHtml` already draws a trail
line to the landing point and (for grounders, per `isGrounder`) presumably
some ground-level travel already (`ball-flight-plan.md` Stage 4e references a
"short ground-level roll" for `isGrounder` plays). Clarify in your plan
whether this item is: (a) asking for the *existing* grounder-roll treatment
to also extend past the initial landing point for line-drive/fly-ball hits
that aren't flagged `isGrounder` (e.g. a `1B` that lands on the fly and then
rolls a few more feet before the fielder gets to it), or (b) something else.
State your interpretation plainly as a proposal, since the request as written
is genuinely ambiguous and you have more context on the current rendering
code than this prompt can give you.

### D. Field scale (infield vs. outfield)

**Open per Alex - evaluate, don't just implement.** The current field canvas
(`ball-flight-plan.md` Stage 4a: 400x360-ish viewBox, `FT_PER_UNIT`) renders
the infield quite small in order to fit realistic outfield depths (up to
~420ft) in the same frame. Alex's own suggestion was a dynamic zoom: start
tight on the infield, and only zoom out once a batted ball's trajectory
actually leaves the infield dirt. **Evaluate this alongside at least one or
two alternatives** (e.g. a non-linear/log-ish radial scale that compresses
outfield distance more than infield distance so both fit one static frame
without a zoom transition; a fixed always-zoomed-out frame with a slightly
larger infield than today's; anything else you judge worth considering given
what you find in the code) and **recommend one**, with tradeoffs stated
plainly (implementation cost, whether it interacts with the reduced-motion
mode, whether a mid-play viewBox/transform change is visually jarring or
smooth given the existing CSS-only animation architecture - no JS timers,
per `ball-flight-plan.md` Stage 4e). This is a genuinely open design
question - do not silently pick one without laying out the comparison.

### E. Math/formula changes

**E1. Anchor-and-no-wrap tie-break rule for the ±5/±50 sign ambiguity (Item
23).** `signedCirc(a, b, mod)` currently has an artifact, previously
*deliberately accepted and asserted* (`ball-flight-plan.md` Stage 3b:
`signedCirc(0, 5, 10) === 5` and `signedCirc(5, 0, 10) === -5`, "known
artifact... accepted... must not be fixed"). **This decision explicitly
supersedes that acceptance for both wheels the function is used on** (the
horizontal/ones-digit wheel, mod 10, ambiguous when the delta is exactly ±5;
and the launch-angle two-digit wheel, mod 100, ambiguous when the delta is
exactly ±50). Confirmed with Alex: the same rule applies to both.

Rule, restated precisely from Alex's worked example: when the raw delta is
exactly at the wheel's halfway point (sign genuinely ambiguous - `+5`/`-5`
are the same distance around a mod-10 wheel, `+50`/`-50` the same around
mod-100), **break the tie using `pitch`'s digit value as the anchor**, and
pick whichever direction (increasing or decreasing from that anchor) **does
not cross the wheel's own wrap boundary** (9->0 for the mod-10 wheel, 99->0
for the mod-100 wheel).

Worked example (mod-10, Alex's own): pitch's ones digit = 3, swing's ones
digit = 8. Going 3->8 forward (+5) stays within 3..8, no wrap - this is the
chosen direction, so `bucket = +5`. Reversed - pitch's ones digit = 8,
swing's ones digit = 3: forward from 8 would be 8->9->0->1->2->3 (+5),
which *does* cross the 9->0 wrap; backward, 8->7->6->5->4->3 (-5), does not -
so `bucket = -5`.

Encode both directions of this example as exact test assertions
(`signedCirc(3, 8, 10) === 5`, `signedCirc(8, 3, 10) === -5`), plus the
mod-100 analogue at the equivalent halfway point (e.g. `signedCirc(30, 80,
100)` and `signedCirc(80, 30, 100)` - work out and state the expected values
in your plan, don't leave them for Sonnet to derive). Every *other*
`signedCirc` behavior (non-tied deltas) is unchanged - this only touches the
exact-halfway case. Update the Stage 3b assertions that currently assert the
old (now-superseded) behavior.

**E2. (Open question, investigate + recommend only, do not implement) -
should LA's *sign* come from the overall pitch/swing diff's sign rather than
the two-digit delta's own sign (Item 31)?** Alex's example: pitch 830,
swing 770. Today's formula (`dLA = signedCirc(firstTwo(pitch),
firstTwo(swing), 100)`) uses only the first-two-digits component, giving
`dLA = signedCirc(82, 76, 100) = -6` (swing's tens+hundreds digits are below
pitch's) - i.e. an already-negative `dLA` in this example, so re-check Alex's
arithmetic against the real formula before writing this up (Alex's own
description assumed a "3 -> 7" single-digit read, which doesn't match how
`dLA` is actually computed from `firstTwo`, not `onesDigit` - this may be a
misunderstanding worth surfacing back to Alex rather than silently
resolving). The underlying design question is real regardless of the
example's arithmetic: should launch-angle sign track "was the swing number
above or below the pitch number, full value" rather than "was the
first-two-digit component above or below"? Investigate what this would
change across a sample of real historical plays, write up the comparison
(does it change results meaningfully, does it fix or worsen the ±5/±50
tie-break interaction from E1), and give a clear recommendation. **Do not
implement either version** - this is Alex's call after reading your
analysis.

**E3. (Open question, investigate + recommend only, do not implement) - is
a diff of 0 the optimal launch angle for a home run, and should it be (Item
32)?** Today `p_launch = (1 - dLA/50) / 2` maps `dLA = 0` to the *center* of
the archetype's LA band, which for `home_run` (`la_min=25, la_max=35`) is
30°, presumably close to real-world optimal HR launch angle already. Alex's
ask is conceptual: **should distance scale so that a `dLA = 0` home run (or
more generally, the LA closest to true optimal) goes the *farthest*,
tapering outward for any bigger `|dLA|` in either direction** - rather than
today's `D` being driven purely by `q` (the exit-velo-quality signal from
`diff`, independent of how close `LA` landed to optimal). Investigate: (a)
what the real-world optimal HR launch angle is and whether the archetype
band's center already approximates it or should be adjusted, and (b) whether
introducing an LA-proximity-to-optimal factor into the distance formula is
worth the complexity, given Alex also asked whether "the same applies for
other hit results, but just might be less noticeable" - i.e. this could
generalize beyond home runs. Write up findings and a recommendation; do not
change the distance formula.

### F. New readouts

**F1. Pitch/swing values shown on a circle, with the shortest arc
highlighted and the diff shown in the middle (Item 18).** Alex's suggested
placement: "the dead space on the bottom right of the field SVG" - confirm
that dead space exists at whatever viewBox Decision D lands on (this item
depends on D's outcome for where it fits) before committing to exact
coordinates. Propose a concrete small-multiple: a circle (representing the
1-1000 wheel, or the specific digit-wheel relevant to whichever axis is being
illustrated - clarify which in your plan, since both the ones-digit and
two-digit wheels exist and Alex didn't specify which this readout is meant to
visualize) with two marks (pitch, swing) and the shorter of the two arcs
between them highlighted, with the numeric diff in the center. This is a new,
from-scratch SVG element - treat it with the same "single conversion point"
discipline `ftToSvg` established for the field canvas.

### G. Navigation

**G1. Swipe-to-jump to the first/last play (Item 21).** The existing swipe
handler (`app.js:1977-1997`, `SWIPE_MIN_PX`) already steps ±1 per swipe and
explicitly is not currently a dead end at either boundary per the existing
nav tests - confirm current boundary behavior (does a swipe past the last
play currently do nothing, per "make it not a dead end") before designing the
fix. Alex wants: swiping past the last play jumps straight to the very last
play of the game/session in one gesture (not requiring N more swipes), and
the mirror case at the first play. Clarify (in your plan, or by asking Alex
if genuinely unclear) whether this should require a special gesture (e.g. a
long swipe, or swiping twice quickly) or whether *any* swipe attempted at the
boundary should jump - the literal request reads as the latter, which means
a swipe at the boundary behaves differently from every other swipe (jump vs.
step), worth flagging as a UX inconsistency to confirm rather than silently
building.

### H. Accessibility / dark mode

**H1. Light ring around runner tokens in dark mode (Item 30).** Add a
`stroke`/outline to the `.rn` token circles, scoped to
`:root[data-theme="dark"]` (existing pattern, e.g. `style.css:39` and other
`[data-theme="dark"]` overrides already in the file) so it doesn't affect
light mode. Keep team-color fill (already implemented per the
`982ce61 Rescale the leverage meter around LI 1.0; team-color runners`
commit) - this adds a ring, doesn't replace the fill.

### I. Copy and formatting

**I1. Replace "That's everything so far, the game's still going" (Item 4).**
Currently `'<div class="scene-recap live">That's everything so far · the
game's still going</div>'` (`app.js:1818`). Propose 2-3 concrete replacement
strings and pick one, or flag for Alex's pick if none feels clearly better -
this is copy, not logic, low risk either way.

**I2. "END 5th" instead of "END INNING"; "MID 5th" when only the top half is
complete (Item 5, Finding 4).** Use `m.inning` + an ordinal formatter (`5th`,
`1st`, `2nd`, `3rd`, `11th`, `21st`, etc. - check for an existing helper
before writing a new one) and `m.half` to pick `END` (half === "bottom",
i.e. the *whole* inning just finished) vs. `MID` (half === "top", i.e. only
the top half is done and the game continues). This replaces the single
`"END INNING"` string at `app.js:376` with the two-way branch.

**I3. Game Replay title slide: team 1 / `@` / team 2, each on its own line
(Item 6, Finding 5).** Restructure `.catchup-title-teams`
(`app.js:1891-1904`) from its current single-row layout to a vertical stack.
Confirm whether Catch Me Up's equivalent title slides use the same class (and
so would be affected by a pure-CSS change) or whether this is scoped to
`replay-title` only - Alex's request names "the slide that shows the game
before the plays slides," which in Game Replay is `replay-title`; check
whether Catch Me Up has an equivalent per-game divider slide that should get
the same treatment or is explicitly out of scope.

**I4. Remove timezone abbreviation text, keep viewer-local time (Item 20,
Finding 6).** See Finding 6 - locate every render call site using the
`timeZoneName: "short"` formatter's output and drop the abbreviation from
the displayed string, without changing the underlying local-time conversion.

**I5. Replace parentheses with a dot spacer in at least two places (Item 34,
Finding 7).** `"Catch Me Up (N new plays)"` -> a dot-separated form (e.g.
`"Catch Me Up · N new plays"`, matching the `·` separator already used
elsewhere in this file, e.g. the very recap line I1 is replacing). The mobile
filter panel's collapsed active-filter count - locate the actual render call
site near `activeFilterCount()` (`app.js:652`) and confirm it currently
renders with parens before changing it. Audit the rest of `app.js` for any
other `(count)`/`(qualifier)` pattern in user-facing strings while in this
area, per Finding 7 - Alex said "a couple places I can think of," implying
there may be more.

---

## Open questions (flag in your plan, do not silently resolve)

1. **Field scale approach (Decision D)** - genuinely open, evaluate and
   recommend, does not get built until Alex signs off on the recommendation.
2. **LA-sign-from-overall-diff (Decision E2)** - investigate and recommend
   only.
3. **HR-distance-vs-LA-optimality (Decision E3)** - investigate and recommend
   only.
4. **CS/SB "nicer" visual (Decision B3)** - needs Alex's pick among proposed
   options before Sonnet builds it.
5. **Rollout treatment on non-grounder hits (Decision C4)** - your
   interpretation of an ambiguous request; confirm it matches intent before
   Sonnet builds it, or flag it for a quick check-in.
6. **Swipe-to-boundary-jump gesture (Decision G1)** - literal request implies
   an inconsistent gesture-to-action mapping at the boundary; confirm intent.
7. **LODP/LOTP choreography (Decision B6)** - Alex already flagged this as
   possibly too complex for this round; your plan should give an honest
   effort/complexity estimate so Alex can decide whether to scope it in or
   defer it, rather than silently building a partial version.
8. **Whether B1 (removing the fielder-converge token) breaks anything A2
   (multi-throw) or the new throw-from-fielder visual depends on** - resolve
   the ordering/dependency between these two decisions explicitly, since they
   touch the same rendering area.

---

## What to produce

A staged implementation plan, in `ball-flight-plan.md`'s style, covering at
minimum:

1. **A findings section** (like this prompt's own "Verified findings," but
   from your own read of the current, actual code, not mine - confirm or
   correct anything I asserted above; I have not run the app or the test
   suite, only read source).
2. **Section-by-section decisions**, matching or refining the A-I structure
   above, each with exact function/file-level diffs against the current
   `app.js`/`style.css`/`key_moments_build.py`/`utils.py`.
3. **New/changed test coverage.** This repo's established pattern for `app.js`
   pure functions is a Playwright harness calling them via `page.evaluate`
   (`ball_flight_test.py` is the existing example, per `ball-flight-plan.md`
   Stage 3b). New `signedCirc` tie-break behavior (E1), the ordinal formatter
   (I2), and `outThrowTarget`'s new branches (A1-A3) are all pure-function
   changes suited to this pattern - specify which existing test file each
   belongs in vs. needing a new one.
4. **A recommended build order**, staged so low-risk/high-confidence items
   (copy fixes, the 3rd-out dot, the tie-break rule) ship and are checkable
   before touching riskier/more interconnected pieces (multi-throw sequencing,
   the field-scale redesign, the two open investigate-only math questions).
   Group independent items so Sonnet (or Alex reviewing Sonnet's work) can
   sign off in small batches rather than one giant diff.
5. **Explicit call-outs for every open question above** - do not let any of
   them get quietly resolved inside a "decision" section instead of surfaced
   here.

Flag anything uncertain rather than guessing. Where you find that an item in
this prompt rests on a misreading of the current code (my findings above are
based on reading, not running, the code), say so plainly and correct it.

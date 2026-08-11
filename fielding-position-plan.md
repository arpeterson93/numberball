# Fielding position mapping — implementation plan (DRAFT)

Goal: know which specific player is standing at each of the 9 defensive
positions at any given play, for completed games, in-progress games, and the
still-in-progress portion of the *current* half-inning/session — then surface
that on the slideshow.

Today the site explicitly does **not** do this (`docs/js/app.js`
`FIELDER_ANCHORS_FT` comment: "No names, no per-play defensive alignment -
that data doesn't exist"). This plan reverses that decision now that a real
data source exists.

## 1. What the data actually contains (confirmed by reading the live sheets)

**`plays.pos`** (from the Plays (Raw) tab's `Pos` column) is **not** "who
fielded this specific batted ball." It's the *batter's own* roster position,
recorded on their own plate-appearance row. Confirmed against live data:
`Chef Cook` bats twice, both rows say `2B`; `Sonny Wortzik` bats twice, both
`1B`. Values also include `DH` and `PH`. So `pos` is a snapshot of "what
position is this player playing (or DH/PH) as of this plate appearance" -
useful, but two limitations: (a) it only updates when that specific player
bats, so a mid-inning defensive-only substitution is invisible until their
next PA, and (b) it tells you the *batter's* position, not who's playing the
other 8 spots at that moment.

**The "Lineups" tab** (same "Export Tables" sheet as Plays, confirmed present
in both the MLN and RLN sheets) is a genuine per-game substitution log, not
just a live snapshot:

| Column (as fetched) | Meaning |
|---|---|
| `GameID` | short game code, e.g. `GHGRC03` - matches `games.game_id_short` (already synced) |
| `Row` | sequential row number within this game's lineup log |
| `Team.1` | team abbreviation |
| `Pos.1` | position code (`P`,`C`,`1B`...`RF`,`DH`) |
| `Player` | player **name** (not `player_id`) |
| `Order.1` | batting-order slot (1-9; blank for `P`, who doesn't occupy a batting slot - this league uses a DH) |
| `Play.1` | the **per-game play sequence number** this assignment takes effect at (small int: `1, 2, 9, 34, 42, 53...`) |
| `active` | `Y` on the row currently in effect for that slot; blank once superseded |

This is exactly what the user described: a play-numbered log of when each
player starts/stops playing each position. Row 1-20 of a game are the
starting lineups (`Play=1`); every later row is a substitution, timestamped
by the play number it took effect at (e.g. a `Play=34` pitching change).

**Confirmed semantics (Alex):**
- **`Order` is the substitution identity key**, not `Pos`. A new row with a
  given `Order` value is subbing in for whoever previously held *that batting
  order slot* - not necessarily the same position (a slot's occupant can
  change position without a sub, or a new player can take the slot at a new
  position). So reconstructing "who occupies each slot right now" has to
  track history per `(game, team, order)`, then pivot to `Pos` to build the
  position map - not track history per `(game, team, pos)` directly (two
  different order-slot chains could otherwise transiently look like they
  both claim the same position during a swap). `P` is the one exception:
  `Order` is blank for pitchers, so pitching changes are tracked by
  `(game, team, Pos=P)` directly - which lines up with `plays.pitcher_id`
  already being explicit and cross-checkable.
- **`Play` means "effective immediately before this play number"** - i.e. a
  row with `Play=2` means the sub took effect after play 1 finished and
  before play 2 started, so the new assignment is in effect *starting at*
  play 2 inclusive. This matches the "effective_play_num, inclusive start"
  design below - no change needed there, just confirms the interpretation.

**Join key**, needed only at read time now (see §2): `Lineups.Play.1` is a
small per-game sequence number, while `plays.play_num` is the composite
`{game_code}{seq:03d}` (confirmed: Plays (Raw)'s own `Play` column is already
that composite, e.g. `130124034`). So resolving "what does `Lineups.Play.1 =
34` mean in terms of `plays.play_num`" is `f"{game_code}{34:03d}"` once
`GameID` is resolved to `game_code` via `games.game_id_short`. Needs one spot
check against a real live game before relying on it (confirm zero-padding
width and that sequence numbers never restart mid-game, e.g. across extra
innings).

**Player identity:** Lineups gives a **name**, Plays/players give
`player_id`. Confirmed (Alex): the league enforces no duplicate names across
different `player_id`s, so a plain `name -> player_id` lookup (the reverse of
the `player_id_to_name` map `_sync_plays` already builds) is safe with no
collision handling needed.

**Decision: don't persist Lineups data (Alex).** Rather than syncing it to
Supabase, this is used two different ways depending on whether the game is
still being played:
- **While a game is in progress:** read the Lineups tab live and use it as
  the authoritative source, exactly as described above.
- **Once a game is completed:** don't rely on Lineups at all (whether or not
  the sheet still technically has it before the session rolls over) -
  reconstruct from `plays.pos` instead, uniformly. This trades a small amount
  of accuracy on recently-finished games for one consistent, well-tested code
  path instead of a flaky "accurate for a while, then degrades once the
  session rolls" behavior. It also means the reconstruction algorithm (§4) is
  not a rarely-hit legacy fallback - it's the permanent path for *every*
  completed game, so it's worth getting right.

## 2. No persistence - two read paths, gated on game state

No new Supabase table, no sync step. `get_defensive_alignment(game, play_num)`
dispatches on whether `game` is finished (existing `is_game_final`-style
check, per `KEY_MOMENTS.md`'s definition - `win_team` set and play is the
game's last):

**Path A - in-progress game: live Lineups read.**
`utils.read_lineups_from_sheet(sheet_id)` fetches the `Lineups` tab on
demand (cache briefly, e.g. alongside however Plays is already re-fetched for
a live game, to avoid hammering the sheet every slide advance), filtered to
this `GameID`. Resolution:
1. For each `(team, order_slot)` with `order_slot` not null, take the latest
   row (by `Row` desc) where the row's `Play.1`-derived `play_num <= X` -
   this is the current occupant of that batting-order slot, and whatever
   `Pos` value *that* row carries is their current position (may differ from
   the slot's original position if they've shifted since).
2. Separately, for `(team, Pos='P')`, take the latest row the same way -
   pitchers aren't in an order slot.
3. Pivot into a `pos -> player_name -> player_id` map. Flag (don't crash) any
   case where two different order slots resolve to the same `pos` at the same
   play - a scoring error in the sheet or a gap in this logic worth
   revisiting.

**Path B - completed game: reconstruct from `plays.pos`.** Always used once
a game is final, regardless of whether Lineups data is still technically
available. See §3.

## 3. Reconstruction from `plays.pos` (completed games - the primary path)

This is session-requests-checklist item #34, now made concrete, and now the
*permanent* mechanism for every finished game rather than a legacy fallback -
worth the extra care given that:

Two passes over the defensive team's own plate appearances, **in play order
from the start of the game** (each team's plays alternate half-innings, so
pulling just their own `batter_id`/`pos` rows is a simple filter, not a
merge).

**Pass 1 - forward scan, last-observation-carried-forward, with one hard
trigger for "they left."** Maintain `pos -> player` and, in parallel,
`player -> pos` (the reverse - needed to detect the trigger below). Walk
plays in order; for each `(batter_id, pos)` observed:
- If `pos` currently maps to nobody, or already maps to `batter_id`: just
  set/reconfirm `pos -> batter_id` (and `player -> pos` for `batter_id`). No
  signal either way.
- If `pos` currently maps to a **different player** (a teammate): only one
  player can hold a position at once, so this is unambiguous proof the
  previous occupant left `pos` - unlike a player's own later PA at a
  different position, which alone still isn't treated as proof of anything
  (see below). Record that previous occupant as **presumed subbed out of the
  game as of this play** (drop them from `player -> pos` - no known current
  position), then set `pos -> batter_id` as normal.
- Crucially, **a player's own subsequent PA showing a different `pos` does
  not, by itself, clear their old slot.** That old slot only ever gets
  cleared by the hard trigger above (a teammate claiming it). This is
  deliberate, not an oversight: on its own this under-determines whether they
  actually moved or the data's just showing DH/PH noise, so pass 1 doesn't
  act on it alone - it becomes evidence in pass 2 instead, once pass 1 has
  already flagged a real displacement to explain.
- A position nobody's batted from yet has no known occupant - genuinely
  unresolved, not "assume the starter."

**Pass 2 - correction pass, only possible for a completed game (needs the
full log).** For each player pass 1 marked "presumed subbed out at play N":
scan their own remaining plate appearances after play N.
- None found: they really were subbed out (or the game/inning simply ended
  first) - the "no known position from N onward" pass-1 result stands.
- Found, first one at play M showing position Z: they weren't subbed out,
  they moved - correct the record by retroactively assigning them to `Z`
  starting at play N (the displacement play), not just from M onward, on the
  same "assume continuity, no unexplained gaps" logic as pass 1's forward
  fill. (Rare edge case, not specially handled: if `Z` already had a
  *different*, independently-confirmed occupant somewhere in `[N, M)` from
  its own history, this retroactive fill conflicts with that - acceptable
  rough edge given this whole path already trades exact fidelity for "good
  default, no live data to fall back on.")
- This is the only use of "a player's own later PA at a different position"
  as evidence - always in service of resolving a pass-1-flagged displacement,
  never as an independent trigger on its own.

This can't see defense-only substitutions (a player who enters purely as a
fielder and never bats before the game/inning ends) - a real, accepted gap
given the decision in §1 not to persist the more accurate live data.

`get_defensive_alignment(game, play_num) -> dict[pos, {player_id, name}]`
returns no entry only for a position with zero (post-correction) observations
covering that play; every other position resolves to its latest value after
both passes.

## 4. "Haven't been through the order yet" - in-progress games

For the live/current half-inning, before Path A's Lineups read has rows yet
for today's game - this is the expected, common case early in a game.
`get_defensive_alignment` should return partial results (only the positions
actually known), and the slideshow should render those positions with the
generic fixed anchor + position label it uses today (no regression), only
substituting a real name in once one is known. This is a natural degrade, not
a special case to code around.

## 5. Slideshow display

Two separate, complementary display ideas - worth keeping distinct:

- **Field labels, restricted to players actually involved in the play**
  (Alex's refinement): labeling all 9 `FIELDER_ANCHORS_FT` positions every
  slide would get crowded - the infield anchors (C/P/1B/2B/3B/SS) sit close
  together, so six simultaneous name labels risks real overlap. Instead only
  label the fielder(s) who touched *this* ball: the position chain
  `fieldingNotation()` already computes (`docs/js/app.js:2761`, e.g. SS->2B->1B
  for a 6-4-3) for a fielded out, or the single assigned fielder
  (`HZ_FIELDER_BY_ANGLE`/`import_BRC.csv` `DefaultPosition`) for a hit. This
  is a smaller, more valuable win than labeling the whole defense: usually
  1-3 names, spatially separated from each other, and it also shrinks
  `get_defensive_alignment`'s job on the hot path - most slides only need to
  resolve one or two specific positions, not the full 9-position map. A tight
  chain (e.g. a 6-4 double-play throw between adjacent SS/2B anchors) can
  still crowd two labels together - **decided (Alex): offset the labels apart
  when needed**, rather than dropping one or falling back to the text line.
  Only needs to trigger for genuinely close anchor pairs, not every multi-
  fielder play - a simple screen-space proximity check between the labels
  being rendered this slide (push apart along the line between their anchors
  if under some pixel threshold) is enough; no need for a general N-label
  layout solver since the involved-players restriction already caps this at
  a handful of labels, almost always 1-2, occasionally 3.
- **Text line above the field, below the leverage meter**: a short,
  human-readable description of the *defensive context* for this play. Draws
  on the exact same involved-players resolution as the field labels above,
  so the two features share one data need - and by extension, share its
  scope: the line only has something to say when a fielder is actually
  resolved, so plays with none (a walk, a strikeout, a homer - nothing for
  `fieldingNotation()`/the flight-fielder assignment to point at) render no
  line at all, rather than restating the result the scorebug already shows.
  **Wording (Alex): likely needs to vary by play type, and probably needs
  iteration against real examples rather than getting fully nailed down
  up front.** Template by category, reusing categories the codebase already
  has instead of inventing new ones:
  - **Fielded out with a `fieldingNotation()` chain** (`docs/js/app.js:2761`):
    spell the chain out with names, e.g. "6-4-3: Uraz to Lisztpitcher to
    Sexton."
  - **Ball in play, not an out** (a hit or an error) with a single
    flight-resolved fielder (`HZ_FIELDER_BY_ANGLE`/`import_BRC.csv`
    `DefaultPosition`): name just that one fielder, e.g. "Fielded by Al-Wayz
    Buntin (RF)."
  That's a starting split (out-chain vs. single-fielder), not a final
  spec - treat the first real batch of rendered examples as a draft to react
  to and refine, the same iterate-on-screenshots pattern already used
  elsewhere in this project (scorebug redesign, field-geometry rounds).

## 5a. Unresolved: how this data reaches the slideshow at all

`docs/js/app.js` is a static site (GitHub Pages) with no live Python/Supabase
access at render time - everything it shows comes from JSON
`key_moments_build.py` pre-generates and commits (`docs/data/*.json`). Every
piece of this design above (`get_defensive_alignment`, the live Lineups read,
the `plays.pos` reconstruction) is Python. None of it can run client-side as
written. This needs a real answer before implementation, not an assumption:
most likely, `key_moments_build.py` needs to call `get_defensive_alignment`
per play while building the feed and embed the resolved names (or the
minimal fields needed to derive them) into the generated JSON - but that has
knock-on effects worth thinking through (build time, since Path B's
reconstruction runs once per completed game rather than per play if done
right; whether Path A even makes sense in this pipeline, since
`key_moments_build.py` already runs on a schedule against live data
separately from the slideshow's own client-side polling). Flagged here as a
real gap, not resolved - see the Fable prompt's "What to produce" item 4.

## 6. Rollout order

1. Spot-check the `Lineups.Play.1` <-> `plays.play_num` join against one real
   in-progress game end-to-end by hand, to nail the exact composite format
   and confirm no gaps/surprises (extra innings, doubleheaders sharing a
   `GameID`).
2. `get_defensive_alignment(game, play_num, positions=None)` - Path A (live
   Lineups read) first, since it's the simpler of the two and has no
   ambiguity to design around; `positions` narrows resolution to just the
   ones a caller needs (see §5's overlap point).
3. Path B (reconstruction from `plays.pos`) for completed games - the
   fuzzier half, worth its own testing against a few real finished games
   since it's now the permanent path, not a rarely-hit fallback.
4. Field-label rendering for the involved fielder(s) only, with the
   proximity-based offset from §5 for tight chains.
5. The text description line above the field, starting from the two
   templates in §5 and iterating against real rendered examples rather than
   trying to finalize wording before anything is on screen.

No open questions left blocking the start of implementation - text-line
wording (§5) is explicitly an iterate-later item, not a pre-implementation
decision.

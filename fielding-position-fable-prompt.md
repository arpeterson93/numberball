# Task: Plan the fielding-position mapping feature for Numberball

You are revising and finalizing an implementation plan that Alex will hand to
Sonnet to execute manually (not you, and not automatically - Sonnet gets your
finished plan as a document, not this conversation). **Do not write code.**
Everything under "Decisions already made" below is decided - do not
re-litigate it or propose alternatives. Your job is to turn those decisions
into a concrete, staged, file-by-file/function-by-function implementation
plan, surface any gaps or edge cases the design hasn't addressed, and flag
genuine open questions rather than silently picking an answer for one.

A full prose draft of this design already exists at `fielding-position-plan.md`
in the repo root - read it, it's the source this prompt summarizes. This
prompt exists to package that draft as a self-contained task brief; if
anything here and that file ever seem to disagree, the file is more detailed
and the more authoritative of the two.

## Project context

Numberball is a Streamlit + Supabase scouting/stats app (repo:
`C:\Users\Alex\PycharmProjects\numberball`). Core mechanic: a pitcher secretly
picks 1-1000, a batter independently picks 1-1000, `diff = circular_diff(pitch,
swing)` buckets into a result via result-range tables.

Separately from the Streamlit app, `docs/` is a static GitHub Pages site
("Key Moments") built by `key_moments_build.py` from the same Google
Sheets, with no Supabase in its path. `docs/js/app.js` (~5,400 lines) renders
that site, including a slideshow feature ("Catch Me Up" / "Game Replay")
that animates each play on an SVG baseball field. **This feature's display
work lands entirely in that slideshow**, not the Streamlit pages.

## What already exists (read these before planning)

- **`docs/js/app.js`, `FIELDER_ANCHORS_FT`** (~line 1146): nine generic,
  hand/derived fixed anchor points for the 9 defensive positions, explicitly
  documented today as "No names, no per-play defensive alignment - that data
  doesn't exist." This feature reverses that.
- **`docs/js/app.js`, `fieldingNotation(m, flight)`** (~line 2761): already
  computes traditional scorecard notation (`6-4-3`, `F8`, ...) for a fielded
  out, including the ordered chain of positions involved
  (`POSITION_NUMBER`, ~line 1144). This is the existing source for "which
  positions were involved in this specific play."
- **`docs/js/app.js`, `HZ_FIELDER_BY_ANGLE` / `INFIELDER_DEPTH_FT` /
  `CANONICAL_ANGLE`** (~lines 1114-1167), and **`import_BRC.csv`'s
  `DefaultPosition`/`ExcludedPositions` columns**: the existing mechanism for
  assigning a single fielder to a batted ball that stays in play (hit or
  out) - this is the source for "which single fielder is involved" on a
  non-multi-fielder play.
- **`utils.py`, `read_plays_from_sheet()`** (~line 7902): reads the live
  "Plays (Raw)" tab. Its `Pos` column (stored as `plays.pos` in Supabase) is
  **the batter's own roster position as of their own plate appearance**, not
  "who fielded this specific ball" - confirmed against real data (a given
  batter's `pos` is stable across their own PAs; values also include `DH`,
  `PH`). The composite `Play` value in that tab (and `plays.play_num`) is
  `{game_code}{per-game-seq:03d}`, e.g. `130124034` = game `130124`, the
  34th play of that game.
- **`utils.py`, `read_games_from_sheet()`** (~line 7840): the "Games" tab has
  both `Game#` (-> `games.game_code`, numeric, e.g. `130124`) and `GameID`
  (-> `games.game_id_short`, short alphanumeric, e.g. `GHGRC03`) - both
  already synced to Supabase. This is the join key between the Lineups tab
  (below, keyed by `GameID`) and everything else (keyed by `game_code`/
  `play_num`).
- **The "Lineups" tab**, same "Export Tables" Google Sheet as "Plays (Raw)"
  and "Games" (confirmed present in both the MLN and RLN sheets; no existing
  reader function - this feature adds one). Not yet synced anywhere. Columns,
  as fetched via the sheet's gviz CSV export:

  | Column | Meaning |
  |---|---|
  | `GameID` | matches `games.game_id_short` |
  | `Row` | sequential row number within this game's lineup log |
  | `Team.1` | team abbreviation |
  | `Pos.1` | position code (`P`, `C`, `1B`...`RF`, `DH`) |
  | `Player` | player **name** (not `player_id`) |
  | `Order.1` | batting-order slot, 1-9; blank for `P` (this league uses a DH, so the pitcher isn't in the batting order) |
  | `Play.1` | per-game play sequence number this assignment takes effect at, small int (`1, 2, 9, 34, 42, 53...`) - effective *starting at* (inclusive) the play this maps to via the `games` join above |
  | `active` | `Y` on the row currently in effect for that slot; not needed by this design, ignore it |

  Row 1-N of a game are the starting lineups (`Play=1`); every later row is a
  substitution. Confirmed semantics: **`Order` is the substitution identity
  key, not `Pos`** - a new row with a given `Order` value is subbing in for
  whoever previously held *that batting-order slot*, and that slot's
  occupant can change position without a sub, or a new player can take the
  slot at a new position. `P` is the one exception (no `Order`), tracked by
  `Pos='P'` directly - consistent with `plays.pitcher_id` already being
  explicit.
- **`KEY_MOMENTS.md`'s `is_game_final` definition**: `Games` tab's `win_team`
  set *and* the play being that game's last (cross-checked against
  `last_play`) - this repo's existing convention for "is this game over,"
  reuse it rather than inventing a second one.
- **Player identity**: confirmed (Alex) - the league enforces no duplicate
  names across different `player_id`s, so a plain `name -> player_id`
  lookup (mirroring the `player_id_to_name` map already built in
  `pages/1_Games.py`'s `_sync_plays`, just reversed) is safe, no fuzzy
  matching or collision handling needed.
- **`session-requests-checklist.md`, item #34**: this feature is the
  concrete implementation of that noted-but-never-built idea
  ("reconstruct each team's batting order from the plays data").

## Decisions already made (do not re-litigate)

**Decision 1 - no Supabase persistence for Lineups data.** Don't sync the
Lineups tab anywhere. Two different read paths instead, chosen per game:
- **In-progress game:** read the Lineups tab live (cache briefly - avoid
  re-fetching the whole tab on every slide advance) and use it as the
  authoritative source.
- **Completed game:** never rely on Lineups, even if it's technically still
  present in the sheet before the session rolls over - reconstruct from
  `plays.pos` instead, always. This is a deliberate trade: consistent
  behavior over squeezing out extra accuracy on recently-finished games, and
  it means the reconstruction algorithm (Decision 3) is the *permanent* path
  for every completed game, not a rarely-hit fallback - implement and test
  it accordingly.

**Decision 2 - live-read resolution (in-progress games).**
`utils.read_lineups_from_sheet(sheet_id)` reads the tab, filtered to the
current `GameID`. To resolve "who's playing each position as of play X":
1. For each `(team, order_slot)` with `order_slot` not null: take the latest
   row (by `Row` desc) with `Play.1`-derived `play_num <= X` - the current
   occupant of that batting-order slot; whatever `Pos` value *that specific
   row* carries is their current position (may differ from that slot's
   original position if they've since shifted).
2. Separately, `(team, Pos='P')`: latest row the same way (pitchers aren't
   in an order slot).
3. Pivot into `pos -> player_name -> player_id`. If two different order
   slots resolve to the same `pos` at the same play, that's a data-quality
   problem worth surfacing (log/flag), not a crash.

**Decision 3 - reconstruction algorithm (completed games, `plays.pos`).**
Two passes over the defensive team's own plate appearances, in play order
from the start of the game (each team's own rows are a simple filter of
their half-innings, not a merge):

*Pass 1 - forward scan, last-observation-carried-forward, with one hard
displacement trigger.* Maintain `pos -> player` and `player -> pos` in
parallel. For each `(batter_id, pos)` observed, in play order:
- `pos` maps to nobody, or already to `batter_id`: set/reconfirm it. No
  signal.
- `pos` maps to a **different player** (a teammate): only one player can
  hold a position at once, so this is unambiguous proof the previous
  occupant left. Mark that previous occupant **presumed subbed out as of
  this play** (drop from `player -> pos`), then set `pos -> batter_id`.
- A player's own later PA at a *different* position does **not**, by
  itself, clear their old slot - deliberately. On its own it under-
  determines whether they actually moved (vs. DH/PH noise in the data), so
  pass 1 never acts on it alone. It only becomes evidence in pass 2, in
  service of resolving a displacement pass 1 already flagged.
- A position nobody's batted from yet has no known occupant - genuinely
  unresolved, not an assumed starter.

*Pass 2 - correction, using the full completed-game log.* For each player
pass 1 marked "presumed subbed out at play N," scan their own remaining PAs
after N:
- None found: they really were subbed out (or the game/inning ended first)
  - pass 1's result stands.
- Found, first one at play M showing position Z: they weren't subbed out,
  they moved. Correct the record: retroactively assign them to `Z` starting
  at play N (the displacement play, not just from M onward) - same
  "assume continuity" logic as pass 1's forward fill. (Edge case, not
  specially handled: if `Z` already had a different, independently-confirmed
  occupant somewhere in `[N, M)`, this conflicts with that - acceptable rough
  edge for this path, not worth solving further.)

This can't see defense-only substitutions (a fielder who enters without ever
batting before the game/inning ends) - an accepted gap given Decision 1.

`get_defensive_alignment(game, play_num) -> dict[pos, {player_id, name}]`
(used by both paths, dispatching on `is_game_final`) returns no entry only
for a position with zero observations covering that play; every other
position resolves to its latest value.

**Decision 4 - in-progress games before any lineup info exists yet.** Before
Decision 2's live read has any rows yet for today's game (normal, common
early in a game), return partial results - only the positions actually
known - and have the slideshow keep rendering the rest with the existing
generic fixed-anchor + position-code label (no regression), substituting a
real name in only once one resolves. Not a special case to code around.

**Decision 5 - field labels, restricted to players involved in the specific
play.** Do not label all 9 `FIELDER_ANCHORS_FT` positions every slide (the
infield anchors sit close together - six simultaneous labels risks real
overlap). Only label the fielder(s) who touched *this* ball: the
`fieldingNotation()` chain (e.g. SS->2B->1B for a `6-4-3`) for a fielded out,
or the single assigned fielder (`HZ_FIELDER_BY_ANGLE`/`import_BRC.csv`
`DefaultPosition`) for a ball in play that isn't an out. Usually 1-3 names.
This also narrows what `get_defensive_alignment` needs to resolve per slide
- pass it the specific position(s) needed via a `positions` param, not
always the full 9.

**Decision 6 - tight-chain label overlap: offset, don't drop.** A close pair
(e.g. a `6-4` throw between adjacent SS/2B anchors) gets pushed apart along
the anchor-to-anchor line when a screen-space proximity check finds them too
close - a simple pairwise check is enough, no general N-label layout solver,
since Decision 5 already caps this at a small handful of labels (usually
1-2, occasionally 3).

**Decision 7 - text line above the field, below the leverage meter.** Shares
the exact same involved-players resolution as Decision 5, and by extension
its scope: renders nothing for a play with no resolved fielder (walk,
strikeout, homer - nothing for `fieldingNotation()`/the flight-fielder
assignment to point at), rather than restating what the scorebug already
shows. Two starter templates (not a final spec - wording is explicitly an
iterate-later item, see below):
- Fielded out with a `fieldingNotation()` chain: spell it out with names,
  e.g. "6-4-3: Uraz to Lisztpitcher to Sexton."
- Ball in play, not an out, single flight-resolved fielder: name just that
  fielder, e.g. "Fielded by Al-Wayz Buntin (RF)."

## What to produce

A staged, concrete implementation plan covering at minimum:

1. **Empirical validation of the join**, as literally step 1 - before
   writing the rest of the plan in stone, spot-check
   `Lineups.Play.1 <-> plays.play_num` against one real in-progress game by
   hand (fetch both tabs for the same `GameID`/`game_code`, confirm the
   `{game_code}{seq:03d}` composite format holds, and check for surprises:
   extra innings, doubleheaders sharing a `GameID`, zero-padding width).
   Report what you find - if the join doesn't hold as described, that
   changes Decision 2 and needs to be flagged, not silently patched around.
2. **New reader function**: `utils.read_lineups_from_sheet(sheet_id, tab="Lineups")`
   - exact parsing (which raw columns map to which fields, filtering rules
   for real vs. blank/junk rows), mirroring the style of the existing
   `read_plays_from_sheet`/`read_games_from_sheet`.
3. **`get_defensive_alignment(game, play_num, positions=None)`** - where it
   lives (`utils.py`, presumably), its dispatch on `is_game_final`, and the
   concrete implementation of Decisions 2 and 3 as real, testable functions
   (not just prose) - including the `name -> player_id` lookup and the
   `player -> pos` reverse-tracking structure pass 1/pass 2 need.
4. **Wiring into the slideshow**: exactly where in `docs/js/app.js` this
   client-side JS gets the alignment data - does it need a new build-time
   field in the JSON `key_moments_build.py` already generates (most likely,
   since `docs/` has no Python/Supabase access at render time and nothing in
   this design should require adding any), or some other path? This is the
   biggest concrete design gap in the current draft - resolve it explicitly,
   including what changes in `key_moments_build.py` to compute and embed
   `get_defensive_alignment` results (or the minimal data needed to derive
   them client-side) into the generated JSON per play.
5. **Field-label rendering** (Decisions 5-6): where in `sceneFieldHtml`/the
   relevant render function the new labels get added, and the concrete
   proximity-offset implementation.
6. **Text-line rendering** (Decision 7): where it sits in the existing slide
   DOM structure relative to the leverage meter and field canvas, and the
   two starter templates as real code, explicitly marked for wording
   iteration once real examples are visible - don't over-invest in exact
   copy here.
7. **Testing/validation plan**: how to verify, before calling this done,
   that (a) the live-read path (Decision 2) produces correct alignments
   against a real in-progress game, spot-checked by hand; (b) the
   reconstruction path (Decision 3) produces correct alignments against a
   handful of real completed games with known rosters, including at least
   one game with an actual substitution to exercise pass 2's correction
   logic; (c) the field-label overlap offset (Decision 6) actually triggers
   and looks right on a real double-play example.

Flag anything genuinely uncertain (most likely candidates: the exact
`key_moments_build.py`/JSON wiring in step 4, and whatever step 1's join
spot-check turns up) as an explicit open question for Sonnet to resolve
empirically during implementation, rather than guessing and presenting it as
settled.

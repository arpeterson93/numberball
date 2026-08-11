# Fielding-position mapping - implementation plan (FINAL, for Sonnet)

This is the finalized, staged implementation plan for the fielding-position
feature. The design decisions behind it are settled and documented in
`fielding-position-plan.md` (prose rationale) and
`fielding-position-fable-prompt.md` (Decisions 1-7) - do not re-litigate
them. This document is self-contained: everything you need to implement is
specified here, with the two source documents available for background.

**What the feature is, in two sentences:** resolve which specific player is
playing each defensive position at any given play (live Lineups-tab read for
in-progress games, reconstruction from `plays.pos` for completed games), and
surface it on the Key Moments slideshow as (a) name labels on the field for
only the fielder(s) involved in the play and (b) a short text line above the
field spelling out the defensive play with names.

**Repo conventions that apply to all work here:**
- No em dashes in `.py` files - use hyphens (`-`).
- No `Co-Authored-By` trailer in git commits.
- Match the surrounding comment style: comments state constraints and
  rationale the code can't show, at the density of `utils.py` /
  `key_moments_build.py` / `docs/js/app.js`.

---

## 0. Empirical validation of the join - DONE (2026-08-10). Findings.

The prompt's step 1 has been executed against the live MLN sheet
(`key_moments_build.MLN_SHEET_ID`). The join holds exactly as designed, plus
several findings that change implementation details below. Do not re-verify
these from scratch, but the tests in Stage 5 re-assert the load-bearing ones
continuously.

1. **Join confirmed end-to-end.** `Lineups.GameID` (`GHGRC03`) ->
   `Games.Game#` (`130317`) via the Games tab; plays for that game are
   `130317001`..`130317053`, seqs contiguous 1..53 with no restarts (extra
   innings included in the one sequence). Every `Play.1` value in Lineups
   (`1, 2, 9, 34, 42, 53`) maps to an existing `plays.play_num` via
   `int(game_code) * 1000 + seq`.
2. **"Effective at that play, inclusive" confirmed against real subs.**
   GHG's pitching-change row `Play.1=34`: plays seq 33 still has the old
   pitcher (9039, Mama Luigi), seq 34 has the new one (10036, Kyuss
   Kosades). RC's `Play.1=9` change confirms the same (12051 -> 4011 at
   seq 9 exactly).
3. **Zero-padding is `:03d` and safe.** Max observed seq this season is
   ~70; the composite would only break past 999 plays in one game.
4. **No doubleheader hazard.** `GameID` is unique across all 121 Games
   rows this season.
5. **The Lineups tab's real CSV layout is messier than the idealized
   column table.** The gviz CSV export yields ~40 columns. The
   authoritative log columns are exactly: `GameID`, `Row`, `Team.1`,
   `Pos.1`, `Player`, `Order.1`, `Play.1`, `active`. Hazards:
   - There is ALSO an unsuffixed group (`Play`, `Team`, `Pos`, `Order`)
     that is only filled on starting-lineup rows - it is an input-side
     artifact. Ignore it; always read the `.1`-suffixed columns.
   - There is a `Game#` column in this tab that is NOT the 6-digit game
     code (it held `1`). Ignore it; join on `GameID` only.
   - Everything right of `active` is unrelated side tables (roster
     listings, a "Current Lineups" pivot) bleeding into the CSV. The
     reader must select named columns explicitly (never positionally) and
     drop rows where `GameID`, `Player`, `Pos.1`, or `Play.1` is blank.
6. **Lineups holds only the current session's games** (8 GameIDs at time
   of check). It rolls over per session - consistent with Decision 1's
   "never rely on Lineups for completed games".
7. **Major simplification: `Pitcher` and `Catcher` are filled on 1325/1325
   play rows.** So P and C come per-play from the play row itself,
   authoritatively, on both paths - and Path B could never have seen P
   anyway (DH league; pitchers don't bat, so `plays.pos` never observes
   them). Alignment resolution therefore only covers **1B, 2B, 3B, SS,
   LF, CF, RF** (with DH tracked internally for substitution bookkeeping,
   never output). One residual verification is Open Question 1.
8. **`Pos` value inventory:** the 9 position codes plus `DH` (144 rows)
   and `PH` (9 rows) in 1325 plays. Real mid-game position moves exist
   and are rare - two this season: game `130221` batter 9038 played 1B
   (seqs 12, 41, 53) then C (seq 73), and game `130319` batter 10023.
   These are the pass-2 test fixtures for Stage 5.
9. **Timing note:** at time of check all current-session games were
   final, so a build run today never exercises Path A. Live-path
   validation must happen during an active session (Stage 5c has the
   interim harness).

---

## Stage 1 - `utils.py`: reader + the two resolution paths

All new code goes next to the existing sheet readers (`read_games_from_sheet`
at ~7840). No Supabase, no persistence, no network I/O anywhere except the
one reader function - the resolvers take data as arguments so they are unit
testable (Stage 5) and so the build controls fetch frequency.

### 1.1 `read_lineups_from_sheet(sheet_id, tab="Lineups") -> list[dict]`

Mirror `read_games_from_sheet`'s structure (gviz CSV fetch, `_str`/`_safe_int`
cleaning). Per finding 5:

- Read only the named columns `GameID`, `Row`, `Team.1`, `Pos.1`, `Player`,
  `Order.1`, `Play.1`. Prefer the `.1`-suffixed name; fall back to the
  unsuffixed name only if the suffixed one is absent from `df.columns`
  (future-proofing for a sheet cleanup that removes the duplicate headers -
  today the unsuffixed columns exist AND are the wrong ones, so presence of
  `Team.1` must win).
- Emit per row: `{"game_id_short", "row" (int), "team" (abbrev, e.g. GHG),
  "pos", "player_name", "order_slot" (int or None - None for pitchers),
  "seq" (int, from Play.1)}`.
- Skip rows where `game_id_short`, `player_name`, `pos`, or `seq` is
  missing. Ignore `active` entirely (per the design: not needed).
- Return sorted by `(game_id_short, row)`.

### 1.2 `game_is_final(game) -> bool`

The repo's one definition of "is this game over" (KEY_MOMENTS.md
convention): `win_team` set and both scores set. Move the logic of
`key_moments_build._game_is_final` here and have that function delegate to
this one, so there is exactly one definition. (The "play is the game's last"
cross-check stays where it is - in `_is_final_play`, which is about a single
play, not game state.)

### 1.3 Composite helpers

```python
def split_play_num(play_num) -> tuple[int, int]:
    # 130317034 -> (130317, 34)
def make_play_num(game_code, seq) -> int:
    # ("130317", 34) -> 130317034
```

Trivial, but they pin the `{game_code}{seq:03d}` contract in one place and
give the tests something to assert against.

### 1.4 Path A (in-progress): `lineup_alignment_at(lineup_rows, team, seq, name_to_id) -> dict`

Implements Decision 2 verbatim. `lineup_rows` is one game's rows (from 1.1,
already filtered by the caller to one `game_id_short`); `team` is the
defending team's abbreviation; `seq` is the small per-game play number.

1. Filter to `team`. Ignore rows with `pos == "P"` (P comes from the play
   row - finding 7) and rows with `order_slot` None.
2. For each `order_slot` 1..9: among rows with that slot and `row.seq <=
   seq`, take the one with the highest `row` - that's the slot's current
   occupant, and that row's own `pos` is their current position.
3. Pivot to `pos -> occupant`. If two slots resolve to the same `pos`,
   keep the occupant from the higher `row` (most recent instruction) and
   emit a warning to stderr (data-quality problem worth surfacing, not a
   crash - Decision 2 point 3).
4. Drop `DH` from the output; keep only the 7 alignment positions.
5. Resolve names via `name_to_id` (plain dict lookup - league enforces
   unique names). A name with no id resolves to `{"player_id": None,
   "name": name}` plus a stderr warning (traded/released players may be
   off the current roster tab; display only needs the name, so this
   degrades gracefully).

Return `dict[pos, {"player_id", "name"}]` - partial when early-game rows
don't exist yet (Decision 4: partial results are the normal case, not an
error).

### 1.5 Path B (completed): `reconstruct_defense_timeline(team_plays) -> dict[pos, list[tuple[int, int]]]`

Implements Decision 3. `team_plays` is one team's own plate-appearance rows
for one game (this team BATTING: `half == "top"` rows where they are the
away team, `half == "bottom"` where they are home), in seq order, each
carrying `(seq, batter_id, pos)`.

Output: per position, a sorted list of `(seq, player_id)` entries meaning
"occupied by this player from this seq onward" - append an entry only when
the occupant changes. Positions never observed have no key.

**Pass 1** - forward scan, maintaining `pos_to_player` and `player_to_pos`
in parallel, plus `displaced: list[(player_id, seq_N)]`:

- `pos == "PH"`: record nothing in either map. A pinch-hit observation
  carries no defensive information and PH is not an exclusive slot -
  treating it as one would generate false displacements. (Proposed rule -
  Open Question 2; the decisions call PH "noise" but don't pin the
  mechanics.)
- `pos == "DH"`: treat as an exclusive slot exactly like a fielding
  position (only one DH at a time, so the displacement trigger is real
  evidence a player left the game) - but DH never appears in returned
  alignments; it exists in the maps purely for substitution bookkeeping.
- Otherwise, per the spec:
  - `pos` maps to nobody or already to this batter: set/reconfirm both
    maps; append a timeline entry on first claim.
  - `pos` maps to a different teammate: that occupant is **presumed
    subbed out as of this seq** - append `(occupant, seq)` to `displaced`,
    remove them from `player_to_pos`, set the maps to the new batter,
    append a timeline entry `(seq, batter_id)`.
  - A batter observed at a new position does NOT clear their old
    position's map entry (deliberate - the old slot is only ever cleared
    by the displacement trigger). Transiently one player can appear at
    two positions in `pos_to_player`; `player_to_pos` points to their
    latest. Accepted per the design.

**Pass 2** - correction, for each `(player, N)` in `displaced`:

- Scan `team_plays` after seq N for that batter's own PAs, skipping rows
  with `pos == "PH"` (a later pinch-hit appearance doesn't prove they
  stayed on defense - proposed rule, same Open Question 2).
- None found: pass 1 stands (really subbed out, or game ended).
- First found at seq M with position Z: they moved, not subbed - insert
  `(N, player)` into `timeline[Z]`, keeping the list sorted by seq.
  Tie-break when `timeline[Z]` already has an entry at exactly seq N: the
  retro-fill inserts BEFORE the existing entry, so the direct observation
  wins from N onward (deterministic; the `[N, M)` conflict window is the
  design's accepted rough edge, not something to solve).

### 1.6 `timeline_alignment_at(timelines, seq, positions=None) -> dict[pos, player_id]`

Query helper: per position (optionally filtered by `positions`), the last
entry with `entry_seq <= seq`; no entry covering seq means no key in the
result (genuinely unresolved, per Decision 3's closing contract).

### 1.7 `get_defensive_alignment(game, play_num, positions=None, *, plays=None, lineups=None, name_to_id=None, id_to_name=None) -> dict[pos, {player_id, name}]`

The decided public API, as a thin dispatcher with everything injected (no
fetching inside):

- Locate the play row in `plays` by `play_num`; derive the defending team
  from its `half` (`top` -> home defends, `bottom` -> away defends).
- `game_is_final(game)`: build (or accept pre-built - see Stage 2) Path B
  timelines from `plays` for the defending team, query via 1.6, map ids
  to names via `id_to_name`.
- Not final: filter `lineups` to `game["game_id_short"]`, resolve via 1.4
  (defending team's abbreviation, `seq` from `split_play_num`).
- `positions` narrows the output in both branches.

**Design note, resolving the prompt's step-4 tension explicitly:** Decision
5's "pass the specific positions needed" narrowing cannot apply at build
time, because which positions a slide needs is decided client-side by the
physics (`fieldingNotation`/`flight.fielder` run in JS at render time). So
the build always resolves the full 7-position map (plus P/C from the play
row) and embeds it; the involved-positions restriction happens in app.js
when choosing which labels to draw. The `positions` param stays in the
Python API for spot-check tooling and future callers.

---

## Stage 2 - `key_moments_build.py`: compute at build time, embed per play

This resolves the prompt's item 4 (the acknowledged design gap): `docs/` is
static, so the alignment is computed here in Python and embedded in the
generated JSON as **resolved names, per play**. The alternative (embedding a
per-game substitution timeline and resolving client-side) is rejected
because `key_moments.json` rows must render standalone (a moment card's
Play Scene opens without its game's full timeline in hand) and because it
would duplicate resolution logic in JS. In-progress games get alignment
as-of the last scheduled build - the same staleness as every other field on
the site, which is the accepted behavior.

Changes:

1. **`load_reference`**: add `"name_to_id": {p["name"]: p["player_id"] ...}`
   (the reverse of the `player_id_to_name` idea from `pages/1_Games.py`'s
   `_sync_plays`; unique names are league-enforced).
2. **Per-game precompute in `build()`**, inside the existing
   `for game_code in sorted(by_game)` loop, before the `replay_game` walk:
   - Final game (`_game_is_final`, now delegating to `utils.game_is_final`):
     build Path B timelines once per team via
     `utils.reconstruct_defense_timeline` (each team's batting rows are a
     simple filter of `by_game[game_code]` by half). Query per play with
     `utils.timeline_alignment_at`.
   - Non-final game: fetch Lineups ONCE per build run, lazily - only when
     the first non-final game is encountered - via
     `utils.read_lineups_from_sheet(sheet_id)`, grouped by
     `game_id_short`. Per play, call `utils.lineup_alignment_at` with the
     defending team's abbreviation. (Team keys: MLN plays carry Team IDs
     like `T1009`; Lineups carries abbreviations - map via
     `ref["team_by_id"][...]["abbrev"]`.) Decision 2's "cache briefly" is
     trivially satisfied: one fetch per build run, and nothing else
     consumes this data (the display lands only in this build's output).
3. **`build_moment`** gains a `defense` argument, emitted as:
   ```json
   "defense": {"SS": ["Dixon Uraz", "Uraz"], "1B": ["Conor Sexton", "Sexton"], ...}
   ```
   - Value is `[full_name, last_name]` - the field labels want last names
     (fit on the field), the text line's chain template also wants last
     names, and its single-fielder template wants the full name (Decision
     7's own examples use exactly this split). Both come from
     `_player_view`, so no client-side name splitting (which breaks on
     "Skibidi McGyatt Jr.").
   - Compose per play: `P` from the row's already-resolved pitcher view,
     `C` from `_player_view(ref, play.get("catcher_id"))` (finding 7 -
     subject to Open Question 1), the other 7 from the path resolution
     above. Omit unresolved positions; omit the field entirely when empty
     (Decision 4's graceful degrade - the client treats missing as
     unknown and renders what it renders today, no regression).
   - Emit uniformly for every play, including `FLIGHT_EXCLUDED` ones - no
     conditional gating (simpler, and steal plays may want catcher
     labeling in a later iteration).
   - No player ids in the payload - nothing on the client links fielder
     names today. Add ids later only if labels become links.
4. **Payload cost**: ~8 entries at ~30-35 bytes each, roughly 250 bytes per
   row pre-gzip, on rows already ~1.5KB - acceptable, no meta.json changes,
   no new files.

---

## Stage 3 - `docs/js/app.js`: field name labels (Decisions 5-6)

### 3.1 Extract the position chain from `fieldingNotation`

`fieldingNotation(m, flight)` (~line 2761) already builds the ordered,
adjacent-duplicate-collapsed position chain before formatting. Refactor:

- New `fieldingChain(m, flight) -> array|null`: everything from the current
  function's guards (no flight / no fielder / clearedFence / not a
  ground-or-air archetype / no new out) through the `collapsed` array.
  Returns the collapsed position-string array (e.g. `["SS","2B","1B"]`) or
  null.
- `fieldingNotation` becomes: chain = fieldingChain(...); null -> null;
  then only the existing formatting (F/L/P prefixes, "U" suffix,
  join("-")).
- `playFieldingNotation`'s memoization of the string is untouched.

### 3.2 `involvedPositions(m, flight) -> array`

The one shared "who touched this ball" answer (Decisions 5 and 7 both
consume it):

- `fieldingChain(m, flight)` non-null: the chain, deduped to unique
  positions (a chain can revisit a position non-adjacently, e.g. 3-6-3;
  one label per position).
- Else if `flight && !flight.clearedFence && flight.fielder`:
  `[flight.fielder]` - the same fielder whose token already converges in
  `fielderTokensHtml`, so label and animation always agree (for hits this
  is `flightParams`' nearest-anchor assignment; for outs it's the
  HZ/BRC-resolved position - both already the app's single-fielder answer).
- Else `[]` (over-the-fence HR; K/BB/steals and everything else with no
  flight).

### 3.3 `fielderNameLabelsHtml(m, flight)` - render the labels

Called in `sceneFieldHtml`'s SVG assembly (~line 3414), immediately after
the `fielderTokensHtml` line - under the ball trail, throws, and runner
tokens, which stay on top.

- For each `pos` in `involvedPositions`: look up `(m.defense || {})[pos]`;
  skip if absent (partial data early in a live game - nothing renders,
  which is exactly today's behavior, per Decision 4). Label text is the
  last name (`defense[pos][1]`).
- Anchor: `ftToSvg(FIELDER_ANCHORS_FT[pos].x, FIELDER_ANCHORS_FT[pos].y)`.
  Labels sit at the position's anchor per Decision 6 (the offset works
  along the anchor-to-anchor line), not at the fielded point - the ball
  label already owns that spot.
- **Proximity offset (Decision 6, concrete):** collect the label points
  first, then a simple pairwise sweep:
  ```
  var MIN_LABEL_GAP_PX = 34;            // tune-by-eye, Stage 5d
  for two sweeps:                        // 3 labels max; two sweeps settle it
    for each pair (a, b):
      d = distance(a, b)
      if (d > 0 && d < MIN_LABEL_GAP_PX):
        push a and b apart by (MIN_LABEL_GAP_PX - d) / 2 each,
        along the unit vector between the pair's ORIGINAL anchors,
        away from the pair's midpoint
  ```
  No general layout solver - Decision 5 caps this at 1-3 labels.
- Appearance timing: pop in with the same delay logic as the ball label
  (`fieldedMs(flight)` when there's a ground phase, else the flight
  duration) via a `--delay` CSS var - tune-by-eye alongside Stage 5d.
- Markup: `<text class="fielder-name" x=... y=... style="--delay:...">`.

### 3.4 CSS

`docs/css/style.css`: add `.fielder-name` next to `.ball-label` (~line
653), mirroring its font/halo treatment at a slightly smaller size, with
the same delayed pop-in pattern. Add `.fielder-name` to the
reduced-motion override lists (~lines 1287 and 1305) so labels are simply
visible, not animated, when animations are off.

---

## Stage 4 - text line above the field (Decision 7)

### 4.1 `sceneDefenseLineHtml(m, flight)`

New function; inserted in `playSceneHtml` (~line 4170) between
`sceneScorebugHtml(m, flight, newHalf)` and `'<div class="scene-top">'` -
that is "above the field, below the leverage meter" (the meter lives inside
the scorebug's middle column).

- **Always render the container** `<div class="scene-defense">...` - even
  empty - so the slide layout doesn't jump between plays that have and
  lack a line. (Layout-stability suggestion, not a decision: eyeball it in
  Stage 5d and drop the reserved strip if it looks worse than the jump.)
- Content, sharing `involvedPositions` + `m.defense` exactly (Decision 7's
  scope-sharing requirement):
  - `fieldingChain` non-null:
    `fieldingNotation(m, flight) + ": " + <names joined " to ">`, using
    each chain position's last name; a chain position with no resolved
    name renders its position code in place (`"6-4-3: Uraz to 2B to
    Sexton"`); if NO chain position resolves, render the empty container.
  - Else single involved fielder with a resolved name:
    `"Fielded by " + fullName + " (" + pos + ")"`.
  - Else: empty container (walk, strikeout, homer, unresolved - never
    restate what the scorebug already shows).
- **Both templates are explicitly ITERATE-LATER wording** (Decision 7): get
  real rendered examples first, then refine copy per play type - the
  project's established iterate-on-screenshots pattern. Do not gold-plate
  the strings now.

### 4.2 CSS

`.scene-defense`: one short muted line, sized like `.scene-recap`'s
secondary text; fixed min-height if the always-render container survives
4.1's eyeball test.

---

## Stage 5 - testing and validation (before calling any stage done)

### 5a. Unit tests - new `defense_alignment_test.py`

Mirror `ball_flight_test.py`'s runner style. Cover:

- `split_play_num`/`make_play_num` round-trip.
- **League-wide contiguity assertion** (live check, cheap): for every game
  in the current Plays (Raw) tab, seqs are contiguous 1..N - this
  re-verifies finding 1 across all games every run, not just the one
  spot-checked game, and catches any future seq restart surprise.
- Path A resolver on synthetic lineup rows: starting lineup at seq 1; a
  slot sub (the Y.E. Wally shape: same slot, same pos, seq 2); a slot
  occupant changing position without a sub; pitcher rows ignored; the
  same-pos-two-slots collision warns and keeps the later row.
- Path B pass 1: last-observation-carried-forward; the displacement
  trigger; a player's own new-position PA NOT clearing their old slot; an
  unobserved position staying absent.
- Path B pass 2: displaced player with no later PA (result stands);
  displaced player re-observed at Z (retro-fill from N, not M); PH rows
  skipped in both passes; the seq-N tie-break (direct observation wins).

### 5b. Path B vs. Lineups ground truth - the strongest available check

**Time-boxed opportunity:** while a session's completed games are still in
the Lineups tab (before the next session rolls it over), both paths can be
computed for the same finished games and diffed. Script (scratch or test
mode): for each of the current session's games, at every play, compare
`reconstruct_defense_timeline` output against `lineup_alignment_at` output.

- Expected, acceptable mismatches ONLY: defense-only substitutions (Path B
  can't see them - accepted gap), positions with zero batting observations
  (Path B returns nothing where Lineups knows the answer), and PH-related
  edges. Anything else is a real pass-1/pass-2 bug - fix before wiring
  Stage 2.
- Freeze fixtures to CSV for regression while the data exists: game
  `130221` (batter 9038's real 1B -> C move exercises pass 2) and
  `GHGRC03`/`130317` (subs at seqs 2, 9, 34, 42, 53).

### 5c. Live path (Path A) against a real in-progress game

Can only truly run during an active session: run `key_moments_build.py`
mid-session and hand-verify one in-progress game's `defense` fields against
the Lineups tab. Until a session day: harness-check by forcing
`game_is_final` to return False for one completed game and confirming Path
A output matches the 5b fixtures.

### 5d. Visual verification (after a real build, `python -m http.server` in `docs/`)

1. A double-play slide (find one via the `double_play` tag filter) shows
   name labels on the chain positions, and an adjacent-anchor pair (SS/2B
   on a 6-4 pivot) visibly triggers the proximity offset. If no natural
   tight pair is on screen, temporarily raise `MIN_LABEL_GAP_PX` to force
   it and confirm the push-apart direction and magnitude look right.
2. K/BB/HR slides: no field labels, empty text line, no layout jump.
3. A plain single: one label + "Fielded by ... (RF)" line.
4. Reduced-motion mode still shows the labels.
5. Screenshot the text line on ~5 varied plays and iterate wording with
   Alex (Decision 7's explicit iterate-later loop).

---

## Rollout order

Stages in order; each leaves the site working. Stages 1-2 are invisible
until Stage 3 (the JSON just carries an unused field). Run 5a/5b during
Stage 1, 5c/5d after Stages 2-4.

---

## Open questions (resolve empirically during implementation - do not guess-and-settle)

1. **`catcher_id` semantics.** `Catcher` is filled on every play row, but
   verify on 2-3 ordinary (non-steal) plays that it is the DEFENSIVE
   catcher (cross-check against the Lineups tab's C rows for the same
   game). If it turns out to be anything else, drop the play-row shortcut
   for C: fold C back into the alignment-resolution set (7 -> 8 positions)
   in Stages 1-2.
2. **PH handling** (pass 1: no map mutation; pass 2: PH rows skipped) is
   this plan's proposed mechanics, not a made decision. Validate against
   the 9 real PH rows during 5b; if a PH-heavy game produces wrong
   alignments, surface it to Alex rather than silently patching.
3. **Tune-by-eye values**, expected to move during 5d: `MIN_LABEL_GAP_PX`
   (start 34), label appearance delay, last-name-only field labels, the
   always-rendered empty text-line container.
4. **Text-line wording**: explicitly iterate-later (Decision 7). The two
   templates in Stage 4 are starting points to react to on screen.

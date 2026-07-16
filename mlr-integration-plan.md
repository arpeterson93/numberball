# MLR (Major League Redditball) Integration - Implementation Plan

Staged plan for ingesting `https://www.rslashfakebaseball.com/api` as a third league
(`league = 'MLR'`) alongside RLN and MLN. Written for direct execution; each stage ends
with a verification gate. Open questions are collected at the bottom - resolve the
Stage 0 ones empirically BEFORE writing schema or sync code.

---

## Grounding facts (verified in this repo - do not re-derive)

- `players.player_id` is the **shared cross-league HUMAN id** (RLN and MLN use the same
  community ID registry; see `migrate_player_id_shared.sql` and `_player_dir()` in
  `pages/2_Scouting.py:379`). It is intentionally non-unique. **MLR's `playerID` must
  NEVER be written into `players.player_id` or `plays.pitcher_id`/`batter_id`** - doing
  so would create false human-identity groupings, because `_load_pitcher_plays`
  (`pages/2_Scouting.py:254`) queries `plays.pitcher_id` across all selected leagues.
- `players.s_id TEXT UNIQUE` is the universal row-upsert key: RLN rows use `'R_<player_id>'`,
  MLN rows `'<season>_<id>'`. `db.bulk_upsert_players` and `db.bulk_upsert_mln_players`
  both key on `s_id`. MLR follows the same pattern: `s_id = 'MLR_<playerID>'`.
- `teams.s_team TEXT UNIQUE` likewise; `db.bulk_upsert_teams` already keys on `s_team`
  for ALL leagues (database.py:313) despite the older comment in `migrate_multi_league.sql`.
- `plays` already has `UNIQUE (play_num, league)` and `play_num BIGINT` - MLR's `paID`
  (e.g. `10206060006`) fits directly as `play_num` with `league='MLR'`.
- `games.game_code TEXT UNIQUE` is the games upsert key (`on_conflict="game_code"`).
  A league-prefixed synthetic code (`'MLR-S{season}-G{gameID}'`) avoids collision
  without touching the constraint.
- `plays.obc` is stored as 3-char binary strings `'000'..'111'` in **[3rd][2nd][1st]**
  order (see `migrate_obc_codes.sql` and `obc_circles()` in utils.py:32). The old
  `'Empty'`/`'1&2B'` text labels were migrated away.
- `plays.half` values are lowercase `'top'`/`'bottom'` (sync_plays.py:85).
- `enrich_df()` (utils.py:943) recomputes `diff`, `is_fp_inn`, `is_fp_app`, and all
  delta columns at read time, grouping deltas by `(game_id, pitcher_name)` - so stored
  fp flags are best-effort and per-game deltas are league-safe. `sequence_matches`
  (utils.py:2593) and the delta heatmap (utils.py:5349) are also game-grouped.
- The **non-game-grouped** sequence surfaces (the ones this plan must fix) are, on
  `pages/2_Scouting.py`:
  - `swing_predictor_chart` tail (utils.py:4199 `df.sort_values("id").tail(n)`) - pitcher
    call at ~line 1855 (`_df_tick_p`), batter call at ~line 2580 (`_df_tick_b`).
  - The Optimal Swing inputs: `_pa_df_p`/`_lnc_df_p` (~lines 1828-1849) and
    `_recent_p -> utils.project_from_deltas / project_from_delta2s` (~lines 1862-1864),
    which take consecutive raw values across game AND league boundaries. Batter mirror
    exists (~lines 2500-2600).
  - The percentile-card recent window `_recent_df = df_p.sort_values("id").tail(n_pitches)`
    (~line 1334) - values inside are per-game-safe, but the window composition mixes leagues.
- `utils.py` has TWO different int->obc maps: `_BRC_TO_OBC` (line 60, "sequential"
  encoding: 0=empty,1=1B,2=2B,3=3B,4=1&2B,5=1&3B,6=2&3B,7=loaded) and `BRC_TO_OBC`
  (line 656, literal binary). Which one matches MLR's numeric `obc` is Open Question Q1.
- RLN player sync ALREADY reads a Discord ID column (sheet col 5) in
  `read_players_from_sheet` (utils.py:5947/5978), and MLN's archive reader reads
  `"Discord ID*"` (utils.py:6072). Player sync is triggered from `pages/1_Games.py:230`.
  `players.discord_id` being empty in the DB means the sheet column is empty or the
  sync hasn't run since the column was added - determine which in Stage 3.
- The existing GitHub Actions sync (`.github/workflows/sync.yml`) runs **every 5 minutes**.
  That cadence is unacceptable for hammering a community API with per-player requests -
  MLR gets its own hourly workflow (Stage 4).

---

## Stage 0 - Empirical API reconnaissance (no repo changes)

Write a throwaway probe script in the scratchpad (not committed) that pulls
`/api/players` plus batting careers for a sample spanning all seasons (include
playerID 1, a handful of mid-range IDs, and several IDs near max), and answers:

- **Q1 - obc encoding.** Distinguish sequential vs bitmask: find a leadoff triple
  (or any play producing a lone runner on 3rd) and inspect the NEXT PA's `obc`.
  Sequential predicts `3`; bitmask predicts `4`. Cross-check with a lone runner on 2nd
  (sequential `2` vs bitmask `2` - not distinguishing; use 3rd-only and 1st&2nd states).
  Also confirm the value range is 0-7.
- **Q2 - result vocabulary.** Frequency table of every distinct `oldResult` AND
  `exactResult` across the sample, per season. Decide the per-row source rule
  (likely: `exactResult` if non-null else `oldResult`) and build the reconciliation
  map against `RESULTS_HITS`/`RESULTS_OUTS`/`RESULT_CATEGORIES` (utils.py:36-45).
  Watch for MLR-only codes (IBB, Auto K/Auto BB, steals, bunts, CS, sac variants).
- **Q3 - batting vs pitching endpoint dedup.** Pull one pitcher's
  `/plateappearances/pitching/mlr/{id}` and verify every `paID` in it also appears in
  the batting careers of the hitters he faced. Expected: yes -> ingest batting only.
- **Q4 - player enumeration coverage.** Compare the set of `hitterID`/`pitcherID`
  values seen in sampled PAs against the `playerID` set from `/api/players`. If PAs
  reference IDs absent from `/api/players` (retired players pruned from the roster
  dump), the backfill needs a sequential ID probe (fetch `batting/mlr/{i}` for
  i = 1..N, stopping after ~50 consecutive empties) and synthesized player rows from
  PA name fields.
- **Q5 - gameID scoping.** Does the same `gameID` recur across seasons with different
  matchups? Determines whether `game_code` needs season in it (plan assumes yes:
  `'MLR-S{season}-G{gameID}'`; drop the season part only if gameID proves global).
- **Q6 - paID ordering.** Verify that sorting by `paID` equals sorting by
  `(season, session, gameID, playNumber)`. Expected yes (fixed-width composite).
  This underpins the Stage 4 insert-order decision and Stage 5 ordering fix.
- **Q7 - score semantics.** Are `awayScore`/`homeScore` pre-play or post-play? Trace
  one full game; decide how to compute final score for the synthesized `games` row
  (last PA's post-state, or last PA + its runs).
- **Q8 - regulation innings.** Max regulation inning across completed games -> value
  for `LEAGUE_INNINGS["MLR"]` (utils.py:93).
- **Q9 - wheel confirmation.** Find PAs with `|pitch-swing| > 500` and confirm
  `diff == 1000 - |pitch-swing|` (same circular 1-1000 wheel). Also check for
  null pitch/swing rows (auto plays, steals) and how they're marked.
- **Q10 - dataset size.** Count seasons, players, and approximate total PAs, to size
  the backfill run and decide in-memory feasibility (expected: fine).
- **Q11 - MiLR.** Note whether a minor league (`milr`) exists in the API. It is OUT
  OF SCOPE for this pass; record its existence for Alex.

Deliverable: a short findings note (can live in the PR description or a
`docs/`-style comment block in `mlr_api.py`) with the resolved Q1/Q2/Q5-Q9 answers
baked into the Stage 2 constants.

---

## Stage 1 - Schema migration: `migrate_mlr.sql`

New file, run once in the Supabase SQL editor. Contents:

```sql
-- MLR integration migration.
-- MLR ids live in their own ID space; never store them in the shared human-id
-- columns (players.player_id, plays.pitcher_id/batter_id).

-- ── players ───────────────────────────────────────────────────────────────────
-- Row key stays s_id (already UNIQUE): MLR rows use 'MLR_<playerID>'.
ALTER TABLE players
  ADD COLUMN IF NOT EXISTS bat_type      TEXT,
  ADD COLUMN IF NOT EXISTS pitch_type    TEXT,
  ADD COLUMN IF NOT EXISTS pitch_bonus   TEXT,
  ADD COLUMN IF NOT EXISTS tertiary_pos  TEXT,
  ADD COLUMN IF NOT EXISTS reddit_name   TEXT;

-- ── plays ─────────────────────────────────────────────────────────────────────
-- UNIQUE (play_num, league) already exists (migrate_multi_league.sql).
-- pitcher_id/batter_id remain the shared Numberball human id (NULL for MLR rows
-- until the discord crosswalk resolves them). Raw MLR ids go in ext_* columns.
ALTER TABLE plays
  ADD COLUMN IF NOT EXISTS ext_pitcher_id  BIGINT,
  ADD COLUMN IF NOT EXISTS ext_batter_id   BIGINT,
  ADD COLUMN IF NOT EXISTS session         INT,
  ADD COLUMN IF NOT EXISTS result_raw      TEXT,   -- verbatim source result pre-normalization
  ADD COLUMN IF NOT EXISTS rbi             INT,
  ADD COLUMN IF NOT EXISTS run_scored      BOOLEAN;

CREATE INDEX IF NOT EXISTS plays_league_idx          ON plays(league);
CREATE INDEX IF NOT EXISTS plays_ext_pitcher_id_idx  ON plays(league, ext_pitcher_id);
CREATE INDEX IF NOT EXISTS plays_ext_batter_id_idx   ON plays(league, ext_batter_id);

-- ── teams ─────────────────────────────────────────────────────────────────────
-- MLR team abbrevs (MLB-style: STL, COL, WSH...) may collide with existing
-- RLN/MLN abbrevs. All upserts already key on s_team; drop any leftover bare
-- UNIQUE on abbrev so a colliding MLR abbrev cannot fail or overwrite.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
    WHERE con.conrelid = 'public.teams'::regclass
      AND con.contype = 'u'
      AND array_length(con.conkey, 1) = 1
      AND att.attname = 'abbrev'
  LOOP
    EXECUTE format('ALTER TABLE teams DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;
-- (mirror the index-variant drop from migrate_player_id_shared.sql if a unique
-- INDEX rather than constraint exists on teams.abbrev)

-- ── games ─────────────────────────────────────────────────────────────────────
-- No change: game_code stays the unique upsert key; MLR game codes are
-- league-prefixed ('MLR-S2-G60') so they cannot collide with RLN/MLN codes.
```

Notes for the executor:
- Do NOT add a `(abbrev, league)` unique constraint - `s_team` is the row key for
  every league already, and MLN legitimately has one row per (season, franchise).
- Skip a DB-side result-mapping table; the codebase keeps all vocab maps as Python
  constants in `utils.py`/modules, and the mapping is small and static.
- WPA strings (`batterWPA` etc.) and the `pr*` fields are NOT stored - the app
  computes its own WP.

Verification gate: run the migration, then
`SELECT conname FROM pg_constraint WHERE conrelid='public.teams'::regclass AND contype='u';`
-> only `s_team` remains; `\d plays` shows the new columns and indexes.

---

## Stage 2 - New module `mlr_api.py` (fetch + normalize)

Keep MLR-specific code out of the 6,700-line `utils.py`. New top-level module:

```
mlr_api.py
  MLR_BASE = "https://www.rslashfakebaseball.com/api"
  MLR_LEAGUE_CODE = "mlr"          # lowercase, as the API expects
  _session()                       # requests.Session with User-Agent
                                   # "numberball-scout/1.0 (Discord scouting app)",
                                   # timeout=30, retry x3 w/ exponential backoff on
                                   # 5xx/connection errors
  REQUEST_DELAY = 0.3              # seconds between calls - polite pacing

  fetch_players() -> list[dict]                    # GET /players
  fetch_batting_pas(player_id) -> list[dict]       # GET /plateappearances/batting/mlr/{id}

  MLR_OBC_TO_STR: dict[int, str]   # from Stage 0 Q1; if sequential, it equals
                                   # utils._BRC_TO_OBC - define it here explicitly
                                   # anyway (do not import the private map)
  parse_inning(s: str) -> tuple[int, str]          # "B1" -> (1, "bottom"); "T12" -> (12, "top")
  MLR_RESULT_MAP: dict[str, str]   # from Stage 0 Q2: raw MLR result -> Numberball code
  normalize_result(pa: dict) -> tuple[str, str]    # returns (normalized, raw); source
                                                   # rule per Q2 (exactResult ?? oldResult)
  make_game_code(season, game_id) -> str           # "MLR-S{season}-G{gameID}" per Q5
```

`utils.py` changes in this stage:
- `LEAGUE_INNINGS["MLR"] = <Q8 answer>` (utils.py:93).
- Extend `RESULTS_HITS`/`RESULTS_OUTS`/`RESULT_CATEGORIES`/`_RESULT_ZONE_COLORS`/
  `outs_added` ONLY for MLR result codes that survive normalization with no existing
  equivalent (per the Q2 reconciliation). Prefer mapping to existing codes over
  inventing new ones; add new codes only where semantics genuinely differ.
- **Do NOT re-derive `result` from `diff` for MLR rows.** MLR outcomes depend on
  batter/pitcher type matchups (`batType`/`pitchType`), so there is no single
  diff->result table. Store their authoritative result. `swing_predictor_chart`
  continues to paint Numberball's `RESULT_RANGES` - that is correct: the widget
  answers "what would this swing do in OUR league given this player's tendencies."
- Steal/auto-play rows (per Q9): if MLR PAs include non-swing records, set
  `play_type` so `enrich_df`'s existing `play_type != 'steal'` filter (utils.py:949)
  and the `pitch/swing notna` masks handle them; do not let null-pitch rows into
  sequence stats.

No hyphen/em-dash reminder: use plain hyphens in all `.py` comments and strings.

---

## Stage 3 - Discord ID backfill + crosswalk

**3a. Diagnose why `players.discord_id` is empty.** The RLN reader already ingests
sheet col 5 as `discord_id` (utils.py:5947) and player sync runs from the Games page
(`pages/1_Games.py:230`). Check the RLN sheet's Players tab col 5:
- If populated: just re-run the player sync (and the MLN archive player sync, whose
  reader ingests `"Discord ID*"`). Done.
- If empty: **this requires Alex.** Produce `discord_backfill_template.csv` (one
  script, `backfill_discord_ids.py`, with `--export` and `--import` modes): export
  current RLN players (`s_id`, `name`, `team`, blank `discord_id`) for Alex to fill,
  then import upserts by `s_id` only rows where he entered a value. Do not fuzzy-match
  anything. Note in the PR that MLR crosswalk coverage grows as he fills rows -
  partial backfill is fine; unmatched players simply stay league-local.

**3b. Crosswalk function** - `crosswalk_mlr_players()` in the new sync module,
run at the end of every MLR sync (idempotent, cheap):

1. Load `players` rows: MLR rows (`s_id`, `discord_id`, `player_id`) and RLN/MLN rows
   (`discord_id`, `player_id`). Compare as strings (`str(discordID)` was stored;
   `discord_id` is TEXT).
2. For each MLR row with a `discord_id` matching exactly one distinct RLN/MLN
   `player_id`: set that `player_id` on the MLR player row (upsert by `s_id`), and
   `UPDATE plays SET pitcher_id = <pid> WHERE league='MLR' AND ext_pitcher_id = <mlr_id>
   AND pitcher_id IS NULL` (and the batter mirror). The `IS NULL` guard keeps re-runs
   no-op; the Stage 1 `(league, ext_*)` indexes make it fast.
3. If a `discord_id` maps to MULTIPLE distinct Numberball `player_id`s: log and skip.
   Never guess.
4. Effect: `_load_pitcher_plays` (queries by `pitcher_id` with the league filter)
   transparently includes MLR plays for crossover players once MLR is checked in the
   League filter. Non-crossover MLR players resolve by name only.

Supabase-py upsert caveat for the executor: when `sync_mlr_players` upserts roster
rows, **omit the `player_id` key entirely** from the dicts so a routine roster sync
cannot null out a crosswalked `player_id` (PostgREST only writes supplied columns).

---

## Stage 4 - Ingestion: `sync_mlr.py` + hourly workflow

New standalone entrypoint `sync_mlr.py` (same credential pattern as `sync_plays.py`,
imports `mlr_api` and `database`). Keeping it separate from `sync_plays.py` lets it
run on its own schedule - the existing every-5-minutes cron must NOT drive per-player
API sweeps against a community site.

**New `database.py` helpers** (mirror the MLN section):
- `get_mlr_players_for_lookup()` -> `s_id, name, discord_id, player_id, status` where
  `league='MLR'`.
- `get_mlr_game_code_to_id()` -> `{game_code: id}` built ONLY from `league='MLR'` games
  (prefixing makes collisions impossible, but scope the lookup anyway - see the
  lookup-audit below).
- Extend `get_all_players()` select list with `league` and `discord_id` (needed by
  Stage 5 UI filtering and 3b; backward compatible).
- Reuse `get_existing_play_nums("MLR")`, `bulk_upsert_games`, `bulk_upsert_players`
  (s_id-keyed), `bulk_upsert_teams` (s_team-keyed), `bulk_upsert_plays`
  (play_num,league-keyed) as-is.

**`sync_mlr_players()`**
- `mlr_api.fetch_players()` -> rows:
  `{league:'MLR', s_id:f"MLR_{playerID}", name:playerName, team:Team, hand,
    primary_pos:priPos, secondary_pos:secPos, tertiary_pos:tertPos, bat_type:batType,
    pitch_type:pitchType, pitch_bonus:pitchBonus, reddit_name:redditName,
    discord_nickname:discordName, discord_id:str(discordID) if discordID else None,
    status:str(status)}` - NO `player_id` key (see Stage 3 caveat), NO `season`.
- Upsert via `bulk_upsert_players`.
- If Q4 found historical players missing from `/api/players`: synthesize minimal rows
  (`s_id`, `name`, `league`) from PA `hitterName`/`pitcherName` during the plays pass.

**`sync_mlr_teams(pa_team_abbrevs: set[str])`**
- Union of `Team` from the roster dump and `pitcherTeam`/`hitterTeam` seen in PAs ->
  `{league:'MLR', s_team:f"MLR_{abbrev}", abbrev, team_name:abbrev, full_team:abbrev}`.
  No API source for full team names; abbrev doubles as the display name (Open
  Question Q12 - Alex may want to hand-maintain a name map later).

**`sync_mlr_plays(backfill: bool)`**
1. `existing = db.get_existing_play_nums("MLR")`.
2. Target players: from the freshly-synced MLR player rows. Incremental mode
   (default): only `status == active` players - retired careers are frozen, so
   skipping them cuts request volume drastically. Backfill mode (`--backfill` flag /
   workflow input): every player (plus Q4 ID-probing if needed).
3. Per player (with `REQUEST_DELAY` pacing): `fetch_batting_pas(playerID)`; keep PAs
   whose `paID not in existing`. Batting endpoint only - Q3 confirms every PA has
   exactly one batter, so iterating batters covers the league; the pitching endpoint
   would double-pull.
4. **Collect ALL new PAs across players into one list, then `sort(key=paID)` before
   any insert.** This makes `plays.id` insertion order chronological for MLR (Q6),
   which the app's `sort_values("id")` recency logic depends on. Without this, a
   per-player backfill writes player 1's entire career before player 2's and id-order
   within a game is garbage.
5. Transform each PA:
   - `play_num: paID`, `league:'MLR'`, `game_type:'live'`, `season`, `session`,
     `game_code: make_game_code(season, gameID)`
   - `inning, half = parse_inning(pa["inning"])`
   - `obc: MLR_OBC_TO_STR[pa["obc"]]`, `outs: pa["outs"]`
   - `pitch`, `swing`; `diff = utils.circular_diff(pitch, swing)` recomputed - count
     and log any mismatch vs the API's `diff` (validation signal, Q9)
   - `result, result_raw = normalize_result(pa)`
   - `pitcher_name: pitcherName`, `batter_name: hitterName`
   - `ext_pitcher_id: pitcherID`, `ext_batter_id: hitterID`;
     `pitcher_id`/`batter_id`: from the crosswalk dict `{mlr_id: numberball_pid}`
     if resolved, else omit/None
   - `off_team: hitterTeam`, `def_team: pitcherTeam` (abbrevs - MLR has no full names)
   - `away_score`/`home_score` per Q7 semantics; `rbi`, `run_scored: run`
   - `is_fp_inn`/`is_fp_app`: compute best-effort within the batch (MLN-style
     trackers); `enrich_df` recomputes them authoritatively at read time
     (utils.py:976-977), so mid-game incremental gaps are harmless.
6. Games synthesis: group the batch by `game_code`:
   - away/home: from any `top`-half PA, `hitterTeam` = away, `pitcherTeam` = home
   - final scores per Q7; `season`, `session_number: session`, `league:'MLR'`,
     `game_type:'live'`
   - filter dicts through `_GAMES_TABLE_COLS` (reuse/mirror the set in sync_plays.py:22)
   - upsert games FIRST, re-fetch `get_mlr_game_code_to_id()`, then stamp `game_id`
     on each play row. Any play whose game_code is missing -> error list, skip row
     (same pattern as existing syncs).
7. `bulk_upsert_plays(rows)`; then `crosswalk_mlr_players()`; print counts in the
   `_run` style of sync_plays.py.

**Lookup-scoping audit (decision #5) - fix in the same PR:**
- `sync_rln_plays` (sync_plays.py:58-62): build `game_code_to_id` and
  `player_id_to_name` only from `league == 'RLN'` rows (requires `league` in
  `get_all_players()`/`get_games()` selects - `get_games()` already selects `*`).
- `sync_mln_plays` (sync_plays.py:159): scope `game_code_to_id` to `league == 'MLN'`.
- Grep the app for other bare-id dicts fed by multi-league queries; the known-safe
  ones are name-keyed or s_id-keyed.

**Workflow `.github/workflows/sync_mlr.yml`:** copy of sync.yml with
`cron: '17 * * * *'` (hourly, off the top-of-hour stampede), `workflow_dispatch` with
a `backfill` boolean input mapped to `python -u sync_mlr.py --backfill`. On repeated
5xx from the API, warn and exit 0 (a down community site should not page anyone or
trigger retry storms). The one-time backfill (Q10-sized, roughly N_players requests
at 0.3s spacing) is run manually via workflow_dispatch or locally.

---

## Stage 5 - UI: third league option + sequence-integrity fix

**5a. League multiselect** (`pages/2_Scouting.py:430`): options
`["RLN", "MLN", "MLR"]`. Default: keep `["RLN", "MLN"]` (MLR is opt-in scouting
context; also avoids surprising stat shifts for existing users). Alex can flip the
default later - one line.

**5b. Player/team directory pollution.** `_all_players` currently feeds every name
into the dropdowns; a full MLR mirror adds hundreds of MLR-only names. With `league`
now in `get_all_players()`: filter `_all_players` by `_sel_leagues` (when non-empty)
before building `_pbyn`/`_all_player_names`/`_all_teams`
(`pages/2_Scouting.py:449-475`). Crossover players keep appearing under their RLN
name via `_player_dir` (player_id grouping); MLR-only names appear only when MLR is
checked. Same filter for the team list.

**5c. Sequence-league lock.** New page-level pattern, applied to BOTH the pitcher tab
and its batter-tab mirror:

- When `len(_leagues_tuple) > 1` and a specific player is selected, render a small
  `st.selectbox("Sequence league", ...)` above the Swing Analyzer, options = leagues
  actually present in that player's `df`, default = league of the player's
  most-recent play. When only one league is selected (or present), skip the widget.
- Build `_df_seq = df[df["league"] == seq_league]` and use it for every
  raw-sequence consumer:
  - `_pa_df_p`, `_lnc_df_p` (weights + Optimal Swing inputs, ~1828-1849)
  - `_df_tick_p` -> `swing_predictor_chart` (~1855)
  - `_recent_p` -> `project_from_deltas`/`project_from_delta2s` (~1862-1864)
  - the percentile-card `_recent_df` window (~1334) - its values are per-game-safe
    but the "recent N" window should not straddle leagues
  - batter mirrors (~2500-2600), including the team-offense variant
- **Defensive layer inside `swing_predictor_chart`** (utils.py:4131): add
  `seq_league: str | None = None`; before the tail, if a `league` column exists and
  more than one league value is present, filter to `seq_league` if given, else to the
  league of the most-recent row. This guarantees the widget can never blend leagues
  even if a future caller forgets the page-level filter. Append the league to
  `tick_label` when a filter was applied so the user sees which league the tail is from.

**5d. Within-league ordering correctness.** Two changes:
- `enrich_df` (utils.py:975) and `sequence_matches` (utils.py:2630) sort by
  `["game_id", "id"]` for within-game order. For MLR, id-order within a game is only
  correct because of the Stage 4 sort-by-paID insert rule; make it robust by sorting
  `["game_id", "play_num"]` instead - `play_num` is chronological within a game for
  ALL leagues (RLN: global sheet counter; MLN: game_code+seq composite; MLR: paID).
  Verify RLN/MLN outputs are byte-identical on a sample before/after this swap
  (they should be - id and play_num orders agree within a game today).
- Cross-game recency (`sort_values("id").tail(n)` sites listed in 5c): after the 5c
  single-league filter, id-order is valid for RLN/MLN (insert order == chronology)
  and for MLR going forward (Stage 4 rule). A late-corrected old MLR PA would get a
  high id - same exposure RLN already has; accept it.

**5e. `game_innings`/WP:** `LEAGUE_INNINGS["MLR"]` from Stage 2 makes the game viewer
WP series honor MLR game length (`pages/2_Scouting.py:2956`).

**5f. Pitcher-stats percentile pool - decision needed.** The "Refresh Pitcher Stats"
compute (Games page -> `pitcher_stats` table) builds the percentile pool that ranks
every RLN pitcher (utils.py:6497 `qual = ab_count >= 100`). If MLR plays flow in,
hundreds of MLR-only pitchers enter the pool and silently shift every RLN pitcher's
percentile. **Recommendation: exclude MLR from the pitcher_stats compute for now**
(filter `league != 'MLR'` at its input), and revisit per-league pools later. Flag
this to Alex in the PR description; do not silently include MLR.

---

## Stage 6 - Validation (run after backfill + one incremental cycle)

**Collision / identity audits (SQL in Supabase editor):**
1. Cross-league game_id integrity - the single most important check:
   `SELECT count(*) FROM plays p JOIN games g ON g.id = p.game_id WHERE p.league <> g.league;`
   -> must be 0.
2. No MLR raw id leaked into human-id columns:
   `SELECT count(*) FROM plays WHERE league='MLR' AND pitcher_id IS NOT NULL AND pitcher_id NOT IN (SELECT DISTINCT player_id FROM players WHERE league IN ('RLN','MLN') AND player_id IS NOT NULL);`
   -> 0 (every non-null MLR pitcher_id must be a real crosswalked human id). Batter mirror.
3. Every crosswalked MLR player has a discord-matched counterpart:
   `SELECT s_id FROM players p WHERE league='MLR' AND player_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM players q WHERE q.league IN ('RLN','MLN') AND q.player_id = p.player_id AND q.discord_id = p.discord_id);`
   -> empty.
4. Composite-key sanity: `SELECT play_num FROM plays GROUP BY play_num HAVING count(DISTINCT league) > 1;`
   overlaps are ALLOWED (that is what the composite key is for) - just confirm each
   side's game join passes check 1. Also confirm no pre-existing RLN/MLN row's
   `id`/content changed across the MLR backfill (snapshot `count(*)` and `max(id)`
   per league before/after).
5. Vocab completeness: `SELECT DISTINCT result FROM plays WHERE league='MLR';` - every
   value must be in the extended `RESULTS` list; `res_category` non-null for all
   swing plays after `enrich_df`.
6. obc sanity: distribution eyeballs right ('000' most common, no values outside the
   8 codes); manually trace one full MLR game on the site vs stored rows (obc, outs,
   inning, scores, results).
7. diff mismatches logged by the sync == 0 (or each one explained).

**Sync behavior:**
8. Idempotency: run `sync_mlr.py` twice back-to-back; second run inserts 0 plays,
   0 games, and the crosswalk UPDATE touches 0 rows.
9. Incremental: after a live MLR session, one run picks up only the new paIDs
   (check the printed count vs the site).

**Sequence integrity (app-level):**
10. Unit-style test (small committed test or throwaway script): build a synthetic
    DataFrame with one player's plays in two leagues interleaved by `id`; assert the
    Stage 5c filter + the `swing_predictor_chart` defensive layer yield tails
    containing exactly one league, and that `project_from_deltas` input never spans
    the league boundary.
11. Manual: pick a crosswalked player, select RLN+MLR in the app, confirm the
    "Sequence league" selector appears, defaults to their most-recent league, and
    that the Recent Pitches ticks change when toggled; aggregate cards (zone charts,
    result mix) still show the merged multi-league data.
12. Regression: with MLR unchecked, screenshot-compare the pitcher tab for one RLN
    pitcher before/after the whole change set - identical.

---

## Open questions (resolve empirically in Stage 0 unless marked for Alex)

- **Q1** obc encoding: sequential (`3`=3B) vs bitmask (`3`=1st&2nd). Determines `MLR_OBC_TO_STR`.
- **Q2** Full `oldResult`/`exactResult` vocabulary + which field wins per row; the
  MLR->Numberball result map; whether new codes must be added to `RESULTS_*`,
  `RESULT_CATEGORIES`, `outs_added`, `_RESULT_ZONE_COLORS`.
- **Q3** Pitching endpoint fully redundant with batting? (Expected yes -> batting only.)
- **Q4** Does `/api/players` include retired/historical players, or do PAs reference
  IDs missing from it (-> sequential ID probe needed for backfill)?
- **Q5** Is `gameID` per-season or global? (Plan assumes per-season; game_code embeds season.)
- **Q6** Is `paID` chronologically sortable as an integer? (Plan assumes yes; Stage 4
  sorted-insert and Stage 5d depend on it.)
- **Q7** `awayScore`/`homeScore` pre- vs post-play; how to derive final game scores.
- **Q8** MLR regulation innings for `LEAGUE_INNINGS`.
- **Q9** Confirm circular diff on 1-1000; identify steal/auto rows with null pitch/swing.
- **Q10** Dataset size (seasons, players, PA count) for backfill sizing.
- **Q11 (Alex)** MiLR (minor league) exists in the API - out of scope here; ingest later?
- **Q12 (Alex)** MLR full team names have no API source - abbrevs used as display
  names; hand-maintain a name map?
- **Q13 (Alex)** If the RLN sheet's Discord ID column is empty, Stage 3a's manual
  CSV fill is on Alex - crosswalk coverage is gated on this.
- **Q14 (Alex)** Default League multiselect: keep `["RLN","MLN"]` (planned) or include MLR?
- **Q15 (Alex)** Confirm excluding MLR from the pitcher_stats percentile pool (5f).

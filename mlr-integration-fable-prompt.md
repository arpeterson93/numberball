# Task: Plan the MLR (Redditball) league integration for Numberball

You are planning an implementation for Opus to execute. **Do not write code.** Produce a detailed, staged implementation plan (file-by-file, function-by-function where it matters) that Opus can follow directly. Call out open questions or risks you find along the way rather than silently picking an answer.

## Project context

Numberball is a Streamlit + Supabase scouting app (repo: `C:\Users\Alex\PycharmProjects\numberball`). The core game mechanic: a pitcher secretly picks a number 1-1000, a batter independently picks a number 1-1000, and `diff = min(|pitch-swing|, 1000-|pitch-swing|)` (circular distance) determines the outcome via result-range buckets (see `RESULT_RANGES` / `circular_diff()` / `_diff_to_result()` in `utils.py`).

The app already ingests **two leagues** into a shared schema:
- **RLN** — the current live league, synced from a Google Sheet.
- **MLN** — an archived historical league ("Major League Numberball"), 12 seasons, synced from a separate Google Sheet/archive.

Both share these Supabase tables, each carrying a `league` text column (`'RLN'` or `'MLN'`, default `'RLN'`):
- `games` (game_code, league, game_type, season, session_number, teams, scores, etc.)
- `plays` (play_num, game_id FK, pitcher_id, batter_id, pitch, swing, diff, result, inning, half, outs, obc, off_team, def_team, is_fp_inn, is_fp_app, ...)
- `teams` (abbrev, league, season, s_team [MLN archive upsert key], ...)
- `players` (player_id, name, team, league, season, discord_id, discord_nickname, s_id [MLN archive upsert key], ...)

Key existing files:
- `sync_plays.py` — standalone sync script (run via GitHub Actions) with `sync_rln_games`, `sync_rln_plays`, `sync_mln_games`, `sync_mln_plays`. Each league has its own ID-lookup dict built right before use (e.g. `player_id_to_name` for RLN keyed by bare `player_id`; MLN instead looks players up via a season-scoped `s_id` string like `"13_1042"`).
- `database.py` — Supabase client wrapper (`db.bulk_upsert_games`, `db.bulk_upsert_plays`, `db.bulk_upsert_mln_plays`, `db.get_all_players`, `db.get_mln_players_for_lookup`, etc.)
- `utils.py` — all analysis logic: `circular_diff`, `RESULTS_HITS`/`RESULTS_OUTS`/`RESULT_CATEGORIES`, `outs_added`, `TEAM_ABBREV`, zone bucketing, delta/sequence stats (`compute_recent_pitcher_stats`), and chart rendering including `swing_predictor_chart` (the "Recent Pitches" widget).
- `migrate_multi_league.sql` / `migrate_plays.sql` — the migrations that added league support to the four tables.
- `pages/2_Scouting.py` — the scouting UI. Has a League `st.multiselect` (currently `["RLN", "MLN"]`, default both) at line ~431 that feeds a `leagues` tuple through nearly every query/analysis function on the page.

**Important existing behavior to preserve:** sequence-sensitive stats (`compute_recent_pitcher_stats` in `utils.py`) already `groupby("game_id")` before computing pitch-to-pitch deltas, so those are inherently league-safe today (a `game_id` never spans leagues). **But** `swing_predictor_chart()` (`utils.py:~4199`, `df.sort_values("id").tail(n)`) is *not* league-scoped — if a user has multiple leagues checked in the filter, its "Recent Pitches" tail could already interleave leagues. This needs a fix as part of this work, not just for the new league.

## The new data source: MLR (Major League Redditball)

`https://www.rslashfakebaseball.com/api` — a different, unrelated Reddit-hosted fake-baseball community sim. No auth, no documented rate limits, no API stability guarantees (community-run site).

Confirmed endpoints and shapes (fetched live):

**`/api/players`** — full roster dump, 16 fields per record:
```json
{
  "playerID": 1, "playerName": "Thomas Nova", "Team": "STL",
  "batType": "TT", "pitchType": "NH", "pitchBonus": "S", "hand": "Left",
  "priPos": "C", "secPos": "3B", "tertPos": "2B",
  "redditName": "/u/AtomikaNova", "discordName": "daitryu",
  "discordID": 341669113430540301, "status": 1, "posValue": 4
}
```
`playerID` is a small sequential int, starting at 1 — **this WILL collide with RLN's/MLN's own `player_id`/`s_id` numbering**, they are entirely independent ID spaces from different sites.

**`/api/plateappearances/batting/[league]/[playerid]`** (league code is lowercase `mlr`) — this is the important discovery: **it uses the exact same pitch/swing/diff mechanic as Numberball**, not a different simulation:
```json
{
  "paID": 10206060006, "league": "mlr", "season": 2, "session": 6, "gameID": 60,
  "inning": "B1", "inningID": 688, "playNumber": 6, "outs": 1, "obc": 0,
  "awayScore": 0, "homeScore": 0,
  "pitcherTeam": "COL", "pitcherName": "Hank Murphy", "pitcherID": 76,
  "hitterTeam": "WSH", "hitterName": "Thomas Nova", "hitterID": 1,
  "pitch": 605, "swing": 286, "diff": 319,
  "exactResult": null, "oldResult": "K",
  "resultAtNeutral": "...", "resultAllNeutral": "...",
  "rbi": 0, "run": false, "batterWPA": "-2.27%", "pitcherWPA": "2.27%",
  "pr3B": "...", "pr2B": "...", "pr1B": "...", "prAB": "..."
}
```
32 total fields observed. `oldResult` values overlap with Numberball's existing `RESULTS_HITS`/`RESULTS_OUTS` vocab (confirmed `K` matches) but has not been exhaustively diffed against the full list in `utils.py` — flag this as a task (enumerate all distinct `oldResult` values across a sample pull and reconcile against `RESULTS_HITS`/`RESULTS_OUTS`/`RESULT_CATEGORIES`, extending as needed).

There is a parallel `/api/plateappearances/pitching/[league]/[playerid]` — needs a dedup check against the batting endpoint (same plate appearances are very likely visible from both a batter's and a pitcher's career, so pulling both per crossover matchup could double-insert; only iterating the `batting` endpoint across every player may already give full league coverage since every plate appearance has exactly one batter).

There is **no `/api/games` or `/api/teams` endpoint** — game and team rows must be synthesized/aggregated from the plate-appearance records themselves (`gameID`, `season`, `session`, `pitcherTeam`/`hitterTeam`, `awayScore`/`homeScore`).

The unfiltered `/api/plateappearances` (no league/player) appears to return the entire multi-season dataset and is impractical to pull directly — ingestion must go through the per-league-per-player scoped endpoints.

## Decisions already made (do not re-litigate these — plan around them)

1. **Scope: full league mirror.** Ingest all of MLR — every team, game, and play — not just crossover players. This becomes a fully independent third dataset, on the same footing as the MLN archive.

2. **Sequences stay per-league, never blended.** Raw pitch/swing sequences must never mix across leagues, even for a player who appears in both. The existing League multiselect filter (`pages/2_Scouting.py`) should gain `"MLR"` as a third option, same pattern as RLN/MLN. Additionally, fix the `swing_predictor_chart` "Recent Pitches" gap described above — when multiple leagues are selected, that widget needs an explicit way to lock to a single league (a dedicated selector, or auto-restricting to one league at a time) so it can never tail-sample across a league boundary. Summary/aggregate stats (e.g. batting average, zone tendencies) can still be compared or merged across leagues for a crossover player; only *raw sequence* analysis is restricted to one league at a time.

3. **Identity matching: Discord ID exact match only.** No fuzzy name matching. MLR's `discordID` field is the join key against Numberball's `players.discord_id` column. That column already exists (added in `migrate_multi_league.sql`) but **is not currently populated** for existing Numberball players (verified empty in a recent `players_rows.csv` export) — a backfill step is required before any crosswalk can work. Plan how this backfill happens (manual entry UI? one-time script seeded from known Discord usernames? explicitly flag this as needing Alex's manual input since only he knows the current roster's Discord identities).

4. **Ongoing sync from day one.** MLR ingestion should join `sync_rln_*`/`sync_mln_*` in `sync_plays.py`, run on the same GitHub Actions schedule. Because MLR is a live, actively-updated external league you don't control, the sync must be incremental (diff against already-synced `paID`s, not a full re-pull of every player's full career every run) to be a reasonably polite API citizen and to stay fast.

5. **NEW REQUIREMENT — cross-league ID collision safety.** This is the top concern driving this planning pass: **do not trust bare IDs (`player_id`, `team_id`/`abbrev`, `game_id`/`play_num`, or any natural key sourced from an external system) to be globally unique across leagues.** MLR's `playerID`, `gameID`, and team abbreviations are drawn from an entirely separate ID space than RLN/MLN and *will* collide with existing numbers already in use (e.g. MLR `playerID: 1` is a completely different person than whatever Numberball entity already occupies `player_id = 1` or `s_id` ending in `_1`).
   - Every table that stores cross-league data must enforce uniqueness as a **composite of (external id, league)**, never bare id alone. The MLN archive already does this correctly via separate `s_id TEXT UNIQUE` / `s_team TEXT UNIQUE` columns (season+id composite strings) and `plays` already widened its constraint to `UNIQUE (play_num, league)` in `migrate_multi_league.sql`. Generalize this same pattern to MLR — likely an `m_id`/`mlr_id`-style composite key (e.g. `"mlr_" + playerID`) analogous to `s_id`, or extend the existing composite-key columns to be league-generic rather than MLN-specific.
   - Audit **lookup code**, not just table constraints — `sync_rln_plays()` in `sync_plays.py` currently builds `player_id_to_name` keyed by bare `player_id` across *all* players fetched from `db.get_all_players()` with no league filter (line ~61). If MLR players are inserted into the same `players` table with their own `player_id`/`playerID` values, this kind of bare-id dict becomes a silent misattribution risk (the wrong league's player could be resolved for a given numeric ID). Every per-league sync function needs its own league-scoped lookup dict, mirroring how `sync_mln_plays` already scopes lookups via `s_id` rather than reusing the RLN dict.
   - Same concern applies to `game_code_to_id`, team abbreviation resolution (`TEAM_ABBREV` / `team_id_to_full`), etc. — anywhere an external numeric/short id from one league's API is used to key into a table that now holds multiple leagues' worth of the same id space.

## What to produce

A staged implementation plan covering, at minimum:
1. **Schema changes** — exact SQL (new columns, composite unique constraints, any new mapping/reference tables e.g. for obc numeric→text or result-vocab reconciliation) needed to safely add MLR into `games`/`plays`/`teams`/`players` without any risk of ID collision with RLN/MLN data. Be explicit about which existing constraints need to change vs. which new ones need to be added.
2. **Field mapping / normalization plan** — `obc` (MLR sends a numeric bitmask like `0`; Numberball stores text codes like `'Empty'`, `'1&2B'` — figure out and document the mapping), `inning` (MLR sends combined strings like `"B1"`; Numberball stores separate `inning`(int)/`half` columns), and the `oldResult` vocabulary reconciliation against `RESULTS_HITS`/`RESULTS_OUTS`/`RESULT_CATEGORIES` in `utils.py`.
3. **Discord ID backfill plan** — how existing Numberball players get `discord_id` populated before any MLR crosswalk can run, and how newly-ingested MLR players get matched against them once populated.
4. **Ingestion script design** — new functions in (or alongside) `sync_plays.py` mirroring the existing `sync_rln_*`/`sync_mln_*` shape: how to enumerate MLR players/games (via the per-player plate-appearance endpoints, since there's no bulk dump), how to synthesize `games`/`teams` rows from play data, and how incremental re-sync (by `paID`) avoids full re-pulls.
5. **Sequence-integrity fix plan** — concrete change to `swing_predictor_chart` (and any other sequence-sensitive widget you find on `pages/2_Scouting.py`) so "Recent Pitches" and similar raw-sequence views can never blend leagues, plus extending the League multiselect to include MLR.
6. **Validation/testing plan** — how Opus should verify, after implementation, that (a) no MLR row silently overwrote or got matched to an unrelated RLN/MLN row sharing the same raw id, and (b) sequence-based stats for a crossover player never mix pitches from two leagues.

Flag anything you're uncertain about (e.g. whether `pitching` and `batting` endpoints double-count plate appearances, the full `oldResult` vocabulary, the exact `obc` bitmask encoding) as an explicit open question for Opus to resolve empirically during implementation rather than guessing a mapping in the plan.

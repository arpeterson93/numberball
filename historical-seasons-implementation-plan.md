# Historical Seasons + Deep-Linkable Game URLs - Implementation Plan

Handoff plan for the implementing agent. Produced from `historical-seasons-fable-prompt.md`
after direct verification against the repo and the MLN Historical Archive Google Sheet.

Archive sheet ID (provided by Alex, verified publicly readable):
`1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg`

---

## Part 0: Verified facts (do NOT re-derive these; trust them, spot-check at most)

Everything below was checked live against the sheet and the repo on 2026-08-14.

1. **The archive sheet is directly usable by the existing pipeline.** It is
   publicly readable through the same gviz CSV endpoint `utils.py` already
   uses (`/gviz/tq?tqx=out:csv&sheet=...`), and - the big one - **the
   `utils.py` readers are already archive-aware**:
   - `read_mln_plays_from_sheet` (utils.py:8400) - default tab is `"Plays"`,
     which is the archive's tab name (current season passes `tab="Plays (Raw)"`).
     Docstring says exactly this. Archive columns parse cleanly, including
     `Season` and `Season.1` (= `"Regular"` / `"Post"`, mapped to `season_type`).
   - `read_mln_games_from_sheet` (utils.py:8295) - auto-detects archive vs
     current format by column name (`"WP"` vs `"Winning Pitcher"`). Archive
     `Games` has `a_scr`/`h_scr`/`Win`/`Loss` populated, so
     `utils.game_is_final` returns True for every archive game.
   - `read_mln_players_from_sheet` (utils.py:8187) - default tab `"Rosters"`
     (the archive tab); detects the archive's named-column `S_ID` format.
   - `read_mln_teams_from_sheet` (utils.py:8143) - docstring literally says
     "from an MLN archive sheet"; parses the archive `Teams` tab as-is.
   The local `MLN Historical Archive.xlsx` is an export of this same sheet
   (identical tabs/columns/rows) - the sheet is the live source; the xlsx is
   only used by offline analysis scripts and is NOT needed for this work.

2. **The game_code hypothesis is CONFIRMED across all seasons.**
   `game_code` zero-padded to 6 digits is `{season:02d}{session:02d}{game:02d}`.
   Season 1 game `010101`, season 11 `110906`, season 13 `130124`.
   `utils.read_mln_games_from_sheet` already parses it exactly this way
   (utils.py:8319-8324: `zfill(6)`, `season = code[:2]`, `session = code[2:4]`).
   Play numbers are `{game_code}{seq:03d}` (`utils.split_play_num`, utils.py:8562).
   **A single game_code is a complete, self-describing key: season + session +
   game.** Postseason games use session numbers 15+ in the same scheme
   (e.g. `101501` = season 10, session 15, game 01), flagged `Post` in
   `Games.Type` and `Plays."Season.1"`.

3. **Volume.** Archive `Plays` tab: **74,989 rows** across seasons 1-12
   (3,931 in S1, ~6,300-6,700 each for S2-S12, of which 4,277 are postseason).
   `Games` tab: 1,382 games, sessions 1-19. Current season output is ~2.3 MB
   for ~1,486 plays (~1.5 KB/play), so a full naive backfill is ~110-130 MB of
   raw JSON - BUT per-session files stay ~500-700 KB each (today's
   `plays_02.json` is 706 KB), so with per-(season, session) lazy loading no
   individual fetch ever gets bigger than today's. GitHub Pages gzips JSON over
   the wire (~8-10x on this data).

4. **All 12 historical seasons are 6-inning regulation** (verified: modal final
   inning per season = 6 in every season; extras reach 13 innings once, in S9).
   `MLN_INNINGS = 6` and all leverage/WP machinery apply unchanged. Better yet,
   the WP/RE24 tables were themselves **trained on this exact archive**
   (`compute_win_probability.py` / `compute_run_expectancy.py` read
   `MLN Historical Archive.xlsx`), so historical plays are in-distribution -
   open question 5 from the brief resolves to "no retraining needed, out of scope."

5. **Open question 8 (how does mln-reference know game_codes) is already
   answered by existing code:** `docs/js/app.js:19` defines
   `GAME_LINK_BASE = "https://www.mln-reference.com/live/"` and builds outbound
   links as `GAME_LINK_BASE + m.game_code` (app.js:522-526). mln-reference
   already keys games by the same game_code. The reverse link needs no new
   mapping: mln-reference formats `https://<gameday-url>/?game=<game_code>`.

6. **Deployment shape.** `.github/workflows/key_moments.yml` builds
   `docs/data/*.json` fresh in each run (twice hourly + on push + dispatch) and
   deploys `docs/` as a Pages artifact - generated data is never committed.
   `.gitignore` ignores `docs/data/*.json` (top level only), so per-season
   subdirectories `docs/data/s01/...s12/` **can be committed with no gitignore
   change** and will ride along in the Pages artifact (checkout includes them,
   the artifact uploads all of `docs/`).

7. **No URL-reading code exists in app.js** (no `URLSearchParams` /
   `location.hash` / `pushState` anywhere). Deep-linking is net-new. Boot is
   `boot()` at app.js:7462. The jump flow to reuse:
   `openReplayAtPlay(gameCode, session, playNum, btn)` (app.js:6770) ->
   `loadGameReplay(gameCode, session)` (app.js:6533), which lazy-fetches
   `data/plays_<NN>.json` into `data.playsBySession`.

8. **Known archive data gaps (degrade gracefully, don't fight them):**
   - `Pos*` is never populated historically (S1-S4 literal `"0"`, S5-S12 blank)
     -> defensive-alignment reconstruction yields nothing; the "defense" block
     on cards will be empty for historical plays. Acceptable; verify UI renders
     cleanly with it absent.
   - `Timestamp*` is 0/blank for most of history -> play ordering falls back to
     `play_num` (both `build()`'s sort at key_moments_build.py:1053 and the
     client's `filteredPlaysOrdered` already tiebreak on `play_num`). Verify
     `formatMomentTime(null)` renders blank, not garbage.
   - Archive `Umpire` on plays/games is an ID (`U1045`), not a name. The
     archive `Crosswalks` tab likely maps these - optional polish, don't block.
   - `SECONDARY_HEX` (key_moments_build.py:49) covers only the 16 current
     abbrevs; historical teams without one fall back to primary-hex tiers,
     which `gameTeamColors` already handles.
   - Historical player attributes exist (Rosters has per-season CON/EYE/etc.),
     rookie flags exist (`Rookie?`), logos/hex exist per season in `Teams`.

9. **Reference data is per-season and MUST be filtered.** Archive `Teams` (186
   rows) and `Rosters` (3,688 rows) carry a `Season` column; abbreviations and
   team identities change across seasons (franchise history). `load_reference`
   (key_moments_build.py:317) currently builds `by_abbrev`/`by_player_id` maps
   with no season filter - fed raw archive tabs it would collide across
   seasons. Player IDs themselves are global/stable across seasons
   (`{debut_season}{seq}` at debut, kept forever - e.g. pitcher `2078` from S2
   still pitching in S13), so favorites and player filtering work cross-season.

---

## Part 1: Decisions - recommendations awaiting Alex's confirmation

Confirm these with Alex before or during Stage 1; none of them block starting
the build-pipeline work except #1 (which is 95% settled by verification).

1. **Data source: the archive Google Sheet.** (Recommended, near-settled.)
   Alex provided the sheet link; it is public, complete, and the existing
   `utils.py` readers parse it with zero schema mapping. Supabase alternative:
   the `plays`/`games` tables hold the same ~75k rows with a `season` column,
   but `key_moments_build.py` has no Supabase reader path, Actions would need
   credentials, and rows would need reshaping into what `build()` expects.
   Sheet-as-source keeps one pipeline, one code path, no secrets. Only pick
   Supabase if Alex says the sheet is stale relative to Supabase (verification
   suggests the opposite - the sheet is the origin).

2. **Scope of "filter across all of them" (open question 2).** Ship per-season
   browsing first (Stage 1-4); it makes every play in history *reachable*.
   For true cross-season search, recommend a **prebuilt compact index** later
   (Stage 5, option B below) over live Supabase queries. Ask Alex which of the
   three interpretations he wants; do not build Stage 5 until answered.

3. **Season control UI (open question 3)** - three options, recommend (a):
   - **(a) Season `<select>` as a sibling left of the Session select in
     `.title-row`** (docs/index.html:31). Hierarchical: changing Season swaps
     the active dataset and repopulates the Session select. Cheapest, most
     discoverable, matches the prior lean in session-requests-checklist item 31.
     Current season preselected; historical seasons listed as "Season 12" ...
     "Season 1".
   - (b) Season picker inside the Settings modal - keeps the header clean but
     hides the feature; poor discoverability for a headline capability.
   - (c) Separate "Archive" entry page (e.g. `archive.html`) that boots the
     same app pinned to a chosen season - cleanest separation of live vs
     historical concerns, but duplicates boot wiring and splits the audience.
   Mobile note for (a): `.title-row` is already crowded at <600px; the two
   selects may need a flex-wrap or narrower styling pass.

4. **URL format (open question 6): query string, `?game=130419&play=34`.**
   (Recommended.) Conventional shareable-link shape, survives copy/paste and
   unfurling better than hashes, and GitHub Pages serves `index.html`
   regardless of query string. Hash offers no advantage here since nothing
   server-side exists to avoid. Accept unpadded codes too (`10409` -> zfill 6).

5. **Play-level param (open question 7): include it.** `openReplayAtPlay`
   already does the work; the param is nearly free. `?game=` alone starts the
   game replay from the title slide (auto-playing, like the existing per-game
   replay button); adding `&play=<seq>` opens paused at that play (existing
   jump behavior). `<seq>` is the in-game 3-digit sequence; client computes
   `play_num = game_code * 1000 + seq`.

6. **Postseason labeling.** Sessions 15+ are playoffs. Recommend the session
   dropdown label them "Session 15 (Playoffs)" via a `post_sessions` list in
   each season's meta (derived from `Games.Type == "Post"`). Cosmetic - confirm
   wording with Alex whenever convenient.

---

## Part 2: Architecture summary

- **Artifact shape: same JSON contract as today, one directory per historical
  season.** `docs/data/sNN/{key_moments,players,meta,plays_MM}.json` - built
  once, committed to git, never touched by the live rebuild (which keeps
  writing the current season to `docs/data/` exactly as now). No new client
  parser, no second format. Bloat is controlled by lazy loading, not by a
  compact format: nothing historical is fetched until a visitor picks that
  season or follows a deep link, and then only that season's meta/feed plus
  per-session files on demand - each fetch the same size as today's.
- **Current season stays the boot default and its pipeline is untouched.** The
  only live-build change: current `meta.json` gains an `archive_seasons` list
  (discovered by scanning `docs/data/s*/` in the checkout) so the client knows
  what to offer without an extra request.
- **Client gains one concept: the active season**, held alongside a per-season
  in-memory cache. Season switch = fetch-or-reuse that season's
  meta/moments/players, repopulate controls, re-render. Historical fetches skip
  the cache-buster (immutable files should be CDN/browser-cached; the current
  season keeps `bust()` + `no-store`).
- **Deep link = URL param -> season resolve -> season activate -> existing
  jump flow** with a nullable `btn`.

---

## Part 3: Staged implementation

### Stage 1 - build pipeline: archive mode in `key_moments_build.py`

Goal: `python key_moments_build.py --archive-season 12` produces a complete,
valid `docs/data/s12/` from the archive sheet.

1. Add constants: `ARCHIVE_SHEET_ID = "1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg"`.
2. Parametrize `build()` (key_moments_build.py:968) with
   `archive_season: int | None = None`. When set:
   - `load_reference`: call `read_mln_players_from_sheet(sheet_id, tab="Rosters")`
     and filter rows to `p["season"] == archive_season`; filter
     `read_mln_teams_from_sheet` rows to that season before building
     `by_team_id`/`by_abbrev`/`name_to_id`. (Current-season path keeps
     `tab="Players", season=CURRENT_SEASON` exactly as-is.)
   - Plays: `read_mln_plays_from_sheet(sheet_id, tab="Plays")`, filter to
     `p["season"] == archive_season`. Keep postseason rows (they're part of the
     season; sessions 15+).
   - Games: filter `read_mln_games_from_sheet` result to the season
     (`g["season"] == archive_season`).
   - `meta["season"] = archive_season`; add `meta["is_archive"] = True` and
     `meta["post_sessions"] = sorted sessions whose games are all Type "Post"`
     (read `Type` off the archive Games rows - `read_mln_games_from_sheet`
     doesn't currently surface `Type`; add it to the returned dict, archive
     branch only).
   - The Lineups branch (key_moments_build.py:1020-1025) and the on-deck
     placeholder (1042-1051) should be unreachable (every archive game is
     final); assert rather than silently fetch a nonexistent Lineups tab -
     if any archive game is NOT final (missing Win/scores), log it and treat
     as final anyway rather than crash. Expect a handful of data quirks in
     1,382 games.
3. Output routing in `main()`: archive mode writes to
   `os.path.join("docs", "data", f"s{season:02d}")` and **must not** call
   `_prune_stale_session_files` against the top-level dir or touch any
   top-level file. Current mode is unchanged and must never scan/prune the
   `sNN` subdirectories (`_prune_stale_session_files` at 1344 iterates
   filenames only - subdirs don't match `plays_*.json`; verify, don't assume).
4. Add `--archive-all`: one read of the 75k-row Plays tab (it's one HTTP fetch
   either way; per-season fetching x12 would re-download the same tab twelve
   times), then loop seasons 1-12 building each. Roughly: read all tabs once,
   pass pre-fetched frames into `build()` (add optional injected-data params or
   refactor the reads to a small fetch-once helper).
5. Current-season build addition: `meta["archive_seasons"] = sorted([int(d[1:])
   for d in os.listdir(OUT_DIR) if re.fullmatch(r"s\d{2}", d)])` (empty list if
   none) - so the client learns about committed archive dirs at each live build.
6. CLI: extend `main()` argument handling (`argparse` or simple `sys.argv`) -
   no args = current behavior, `--archive-season N`, `--archive-all`.

Files: `key_moments_build.py`, `utils.py` (surface `Type` in games reader,
archive branch).

### Stage 2 - proof of concept + validation (one season end-to-end)

Build **season 12 first** (closest to current-era data quality), then **season
1** (sparsest: no timestamps, `Pos*` = "0", oldest conventions), then **season
9** (contains the 13-inning game - exercises deep-extras WP lookups).

Validation checklist per season (script it once, reuse):
- Row count of built plays == sheet row count for that season (S12: 6,693; S1: 3,931).
- Every game in the season's Games rows appears; every play's game_code exists
  in games; `sessions` list matches expectations (S12 should show regular
  sessions + postseason 15+).
- Zero (or explainably few) `"Player NNN"` / bare-abbrev fallback resolutions -
  count them; a spike means season filtering of reference data is wrong.
- `wp`/`leverage`/`wpa` non-null on essentially all plays; no crash on the
  13-inning game; leverage values sane (spot-check a walk-off).
- File sizes: per-session files in the ~300-800 KB range, season total ~8-12 MB.
- Load the site locally (`python -m http.server` from `docs/`, or the repo's
  existing dev flow) with the built `s12/` present and click through:
  scoreboard, feed, filters, a game replay, ball flight on a historical play.

### Stage 3 - client: season state + season selector UI

In `docs/js/app.js`:

1. **State.** Add `var season = { current: null, active: null, cache: {} };`
   where `data.meta.season` (from boot) sets `current`, `active` starts equal,
   and `cache[n] = { moments, players, meta, playsBySession }`. Simplest
   refactor with minimal diff: keep the existing global `data` object as "the
   active season's data" and swap its contents wholesale on season switch
   (stash/restore via the cache), rather than threading a season key through
   every `data.meta` reference (there are hundreds).
2. **Path helper.** `function dataPath(file) { return season.active ===
   season.current ? "data/" + file : "data/s" + pad2(season.active) + "/" + file; }`
   Route every data fetch through it: boot loads (7464-7467), `reloadData`
   (7373-7375), `loadGameReplay` (6536), `ensurePlaysLoaded` (836-841),
   `loadAllSessions` (858-863), and the catch-up loader if it fetches directly.
3. **Caching semantics.** For historical seasons fetch with plain
   `fetch(url)` - no `bust()`, no `no-store` (immutable files; let the CDN and
   browser cache them). Add a `getJSONCached(url)` beside `getJSON` and choose
   per active-season. In-memory: a season's loaded data lives in
   `season.cache` for the tab's lifetime; switching back is instant.
4. **Season selector.** Add `<select id="season-select">` before the session
   select in `docs/index.html` title-row (option a from Part 1). Populate from
   `meta.archive_seasons` + current: "Season 13 (Live)", "Season 12", ...
   Hidden entirely when `archive_seasons` is empty (site behaves exactly as
   today until a backfill is committed).
5. **`setActiveSeason(n)`** - returns a Promise:
   - If `n === season.active`, resolve.
   - Stash current `data` into cache; if `cache[n]` exists, restore and re-render.
   - Else fetch `key_moments.json`, `players.json`, `meta.json` from the
     season's dir, populate `data`, cache it.
   - Re-render everything `reloadData` re-renders (7381-7392): session select
     (label postseason sessions via `meta.post_sessions`), scoreboard, team
     select, tag/OBC chips, feed. Reset `filters.session` to that season's
     latest session (same rule `populateSessionSelect` applies today).
   - Reset per-season filter state that can't survive the switch: `selectedGame`,
     `session`. Team filter: reset (abbrevs differ across seasons). Player
     filter: keep `playerId` (IDs are global) but re-run the suggest source.
6. **Gate live-only features when `season.active !== season.current`:**
   - Hide/disable the refresh button (`#refresh-btn`) and its status line.
   - Skip Catch Me Up (`computeCatchUp`/banner) - it's "new since last visit,"
     meaningless for a finished season. Don't advance its cursor while browsing
     history.
   - `formatBuiltAt` line: show "Season NN archive" instead of the build
     timestamp, or show the archive build date from that season's meta.
   - Favorites: picker/list follows the active season's `players.json`
     (roster of that season); the favorites store itself is global player IDs
     and needs no change. Verify `KMFavorites.init` tolerates being fed a new
     roster or gate re-init - inspect `docs/js/favorites.js` before deciding.
7. **Timestamp-less rendering:** verify `formatMomentTime(null)` /
   `formatBuiltAt` render blank for historical rows with no timestamps.

### Stage 4 - deep links

In `docs/js/app.js` (+ a small hook in `boot()`):

1. **Parse:** on boot, `var params = new URLSearchParams(location.search);`
   read `game` (required for the feature) and `play` (optional). Normalize:
   digits only, `game.padStart(6, "0")`; `season = +code.slice(0, 2)`,
   `session = +code.slice(2, 4)`. Reject non-numeric with a toast.
2. **Make `btn` optional** in `openReplayAtPlay` (6770) and any shared helper:
   guard the two `btn.classList` calls (`if (btn) ...`). Same for the
   game-level open path (`watchGameReplay`-style flow at ~6747): factor a
   `openGameReplay(gameCode, session, opts)` that both the tile button and the
   deep link call, or reuse `openReplayAtPlay` with `playNum = null` meaning
   "start at slide 0 auto-playing".
3. **Flow:** after boot's initial data load resolves (end of the `.then` at
   7468-7489, so the normal page is rendered underneath):
   - If `season` is the current season: proceed directly.
   - Else if `meta.archive_seasons` includes it: `setActiveSeason(season)` first.
   - Else: toast "That game's season isn't available" - page stays usable.
   - Then `openReplayAtPlay(code, session, play ? code*1000+play : null, null)`.
     `loadGameReplay` fetches exactly that one session's file via `dataPath` -
     this is the "load just this one season's data on demand" path, shared with
     Stage 3 by construction.
   - Failure states already exist and must fire, not blank-screen: unknown
     session file -> `getJSON` rejects -> existing catch shows "Could not load
     that game's plays."; empty `gamePlaysFor` -> "No plays recorded" toast.
     Deep-link-specific wording is a nice-to-have.
4. **Don't** add pushState/history management in v1 - the URL is read-only
   input. Closing the replay modal leaves the param in the bar; harmless.
   (Optional follow-up: a "copy link" button in the replay modal writing
   `?game=...&play=...` via `history.replaceState` + clipboard - propose to
   Alex, don't build unbidden.)
5. **mln-reference side:** no work here beyond telling Alex the format:
   `https://<gameday-host>/?game=<game_code>` - it already has every
   game_code (it keys its own /live/ pages by them).

### Stage 5 - full backfill + rollout

1. Run `--archive-all`, eyeball the validation script output per season.
2. Commit `docs/data/s01` ... `s12` (one commit, ~110-130 MB; each file well
   under GitHub's 100 MB per-file hard limit; Pages site total lands ~135 MB,
   far under the 1 GB Pages ceiling). Remember Alex's rule: no Co-Authored-By
   trailer.
3. Push -> the `docs/**` path trigger redeploys Pages; the next live build's
   `meta.json` advertises `archive_seasons` and the Season select appears.
4. **First shippable slice** (if Alex wants value earlier): Stages 1-4 with
   only season 12 committed - everything works with `archive_seasons: [12]`.
5. **Done means:** every season 1-12 selectable; session/game/play drill-down
   and slideshows work on all of them; a `?game=` link into any season (and the
   current one) opens the right replay; current-season live flow (30-min
   rebuild, refresh button, catch-up, favorites) visibly unchanged.

### Stage 6 (deferred - blocked on Alex's answer to Part 1 #2) - cross-season filtering

Do not start until scoped. Options to present:
- **(A) Nothing beyond per-season browsing** - already shipped by Stage 5.
- **(B) Prebuilt compact cross-season index (recommended if wanted):** a
  build-time `docs/data/all_plays_index.json` with a minimal per-play tuple
  (play_num, result, tags bitmask, leverage, wpa, batter/pitcher id, team
  abbrevs, outs, obc) - ~75k rows at ~50-70 bytes ≈ 4-6 MB (~600 KB gzipped),
  fetched **only** when the user explicitly enters an "All seasons" mode.
  Filter hits render as feed cards; clicking one deep-links into its season via
  the Stage 4 path. Static-only, no server.
- **(C) Live Supabase queries** from the static site (supabase-js + anon key +
  read-only RLS) - most powerful (arbitrary queries), but adds a runtime
  dependency, key management, and a second data-consistency story. Only worth
  it if Alex wants query shapes the index can't serve.

---

## Part 4: Risks / uncertainties for the implementer to verify en route

- **gviz read scale:** the archive Plays tab is one ~75k-row CSV fetch
  (observed working, tens of seconds). Fine for a one-time backfill; keep
  `--archive-all` to a single fetch.
- **Deep extras WP coverage:** confirm no KeyError/None cascade on the S9
  13-inning game (utils lookups may clamp; verify during Stage 2).
- **`replay_game` assumptions:** it was written for live data; watch for
  hidden dependencies on timestamps or current-season conventions when running
  S1 (earliest data). Any play whose `result` code isn't in the current
  taxonomy (RESULT_LABELS etc.) should fall back to the raw code, not crash -
  check for retired result codes in early seasons
  (`sorted(set(archive results)) - set(RESULT_LABELS)` is a 2-minute check).
- **meta duplication:** each season's `meta.json` re-ships `bases_svg` +
  `flight` (~300 KB). 12 copies ≈ 3.6 MB committed - accept it for v1
  (simplest, keeps the client contract identical); factoring shared chunks is
  a later optimization if anyone cares.
- **favorites.js re-init on season switch** - inspect before wiring Stage 3
  step 6; it was written assuming one roster per page load.
- **Header layout on phones** with the extra select (<600px breakpoint).
- **Archive data quirks:** expect a few games with missing winners/scores or
  play-numbering oddities in 1,382 games; the build should log-and-continue,
  and the validation script should surface counts, not hide them.
- **The `nul` file** in the repo root (Windows artifact, already in git
  status) - unrelated; don't commit it with the backfill.

## Part 5: Open questions to relay to Alex (consolidated)

1. Confirm archive-sheet-as-source (Part 1 #1). Near-settled by verification.
2. Which cross-season filtering scope, if any (Part 1 #2 / Stage 6 A-B-C)?
3. Season control placement - default is (a) sibling dropdown unless he objects.
4. Postseason session label wording.
5. Optional "copy link to this play" button in the replay modal - want it?
6. Ship season 12 first as a visible slice, or wait and land all 12 at once?

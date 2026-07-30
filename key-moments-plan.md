# Key Moments GitHub Pages - implementation plan

Static, public "Key Moments" feed for MLN, adapted from `key_moments_mock_v5.html`.
Hosted on GitHub Pages, refreshed from the MLN Google Sheet, with a lightweight
"favorites" layer. This document is the spec for Opus to implement - nothing in
this repo has been changed yet.

Project constraints for every stage (existing repo rules):
- No em dashes in any `.py` file - hyphens only.
- No Co-Authored-By trailer if committing.

---

## 0. TL;DR architecture recommendation

- **Bypass Supabase for this feature.** A Python build script reads the MLN
  Google Sheet directly (same `utils.read_mln_*_from_sheet` helpers
  `sync_plays.py` already uses), computes WPA/leverage/tags with the existing
  `utils.py` win-probability machinery, and writes one static JSON file. No
  Supabase read happens at page-view time.
- **Serve from `docs/` on `main`**, exactly like `irrigation_planner` - no
  gh-pages branch, no separate build tool, the page is the file.
- **Two refresh paths, not one:**
  1. A coarse `schedule:` trigger in GitHub Actions (best-effort backstop -
     see Fact 3 below on why this can't be trusted alone).
  2. An instant **"Refresh now" button** on the page itself, which calls a
     Google Apps Script web app (same pattern as
     `irrigation_planner/apps-script/Code.gs`) that fires a GitHub
     `workflow_dispatch` REST call using a PAT stored in Apps Script Script
     Properties (never exposed to the browser). `workflow_dispatch` runs
     start in seconds - they aren't subject to the `schedule:` trigger's
     documented delay/throttling behavior.
- **Favorites** are a second, independent Apps Script endpoint (same
  Code.gs-style GET/POST protocol) backing a tiny "type your name" pseudo-auth,
  stored in a small dedicated Google Sheet - not the production plays workbook.

---

## 1. Current-tree facts (verified this session)

1. **The Google Sheet -> Supabase sync already exists.** `sync_plays.py` +
   `.github/workflows/sync.yml` sync both RLN (`1lcgT6np...`) and MLN
   (`1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA`) sheets into Supabase on a
   `cron: '*/5 * * * *'` schedule. The sheet Alex linked in chat
   (`19UWrfgp9XkYwD8HW_giPIpGw2x_BjFQXIWCpozowx80`) is **not** the current one
   - **`1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA` (the one already in
   `sync_plays.py`) is authoritative.** `key_moments_build.py` reads from
   this ID.
2. Despite the 5-minute cron, Alex reports the workflow actually fires every
   few hours. This matches GitHub's documented behavior: `schedule:` triggers
   are best-effort and are delayed/dropped under load, especially on
   low-activity repos. **Do not rely on `schedule:` alone for anything
   time-sensitive** - hence the `workflow_dispatch` refresh button in section 0.
3. **The WPA/leverage engine already exists in `utils.py`; this project mostly
   wires it up, it doesn't invent it:**
   - `get_win_probability(remaining_half_innings, outs, obc, batting_lead)` /
     `get_win_probability_interpolated(...)` - table lookup against
     `win_probability_table.csv` (columns `remaining,outs,obc,batting_lead,win_prob`).
   - `remaining_half_innings(inning, half, innings=_WP_INNINGS)` - converts
     game state to the table's `remaining` key.
   - `compute_leverage(result_ranges, remaining, outs, obc, batting_lead)` -
     returns the Leverage Index shown in the mockup's "Leverage 1.1" text and
     the `.lev-bar` color (high/low/neutral).
   - `_wp_post_play(result, remaining, outs, obc, batting_lead)` - WP after a
     single result; the natural building block for per-play WPA (before/after
     diff), though it will need generalizing since key-moment plays span the
     full result taxonomy, not just what it currently covers - verify at
     implementation time.
   - `advance_runners`, `steal_advance`, `steal_cs` - OBC transition logic.
   - `bases_diamond_svg(obc, outs)` - already renders the exact 3-diamond
     scorebug graphic the mockup hand-codes in inline SVG. Port its geometry,
     don't re-derive it.
4. **OBC encoding**: 3-char string, `obc[0]`=3rd base, `obc[1]`=2nd,
   `obc[2]`=1st ("1" = occupied). `"111"` = loaded. RISP = `obc[0]=="1" or
   obc[1]=="1"`.
5. **`plays` table / raw sheet schema** (`migrate_plays.sql`) already carries
   everything a moment card needs per play: `away_score`, `home_score`
   (running score at that play), `inning_raw` -> enriched `inning`/`half`,
   `outs`, `obc`, `diff` (= `utils.circular_diff(pitch, swing)`), `result`,
   `off_team`/`def_team`, `pitcher_name`/`batter_name`, `runner_name`,
   `runner_id`, `steal_num`, `is_fp_inn`/`is_fp_app`. Sequential per-game
   score/obc diffing (no extra state machine needed) gives lead changes and
   "scored a run" detection for free.
6. **`diff` semantics**: `circular_diff(pitch, swing)` over a 1-1000 circular
   domain, max value 500. `diff == 0` = perfect timing; `diff == 500` = exact
   opposite (worst). These are the mockup's "0 Diff" / "500 Diff" pills.
   **Confirmed: exact match, not a band** (`diff == 0` / `diff == 500`
   literally).
7. **Result taxonomy** (`result_frequencies.csv`, 45 distinct codes). Notable
   groupings for filters/tags:
   - Hitting (favors offense): `1B, 2B, 3B, HR, 1BWH, 1BWH2, 2BWH, B1B,
     B1BWH, BB, IBB, SB, SB2, SB3, SB4`
   - Pitching (favors defense): `K, GO, FO, PO, DP, DP21, DP31, DPH1, DPRun,
     LODP, CS, CS2, CS3, CS4, GORA, DFO, BGO, TP, FC, FC3rd, FCH, BFC, Balk`
   - Sac plays: `SacF, DSacF, SacB`
   - Steals specifically: `SB*` (success) vs `CS*` (caught). **Confirmed:
     the "steals" tag is `SB*` only** - a caught steal doesn't tag as
     "steals" but can still surface via the leverage/WPA threshold if it's
     high-stakes enough.
   - `HR` filter chip = literal `result == "HR"`.
   - Minor remaining unknown (non-blocking, resolve during implementation):
     exact meaning of `1BWH`/`2BWH` ("...with hold"?) and `bAuto`/`pAuto` -
     grep repo docs/comments or ask Alex before tagging on them if their
     hitting/pitching classification turns out ambiguous.
8. **Static-site + Google-Sheets-backend precedent already exists in another
   Alex project**: `irrigation_planner/apps-script/Code.gs` +
   `irrigation_planner/apps-script/DEPLOY.md` + `irrigation_planner/docs/`.
   Pattern: one Sheet, one tab, `key | json_blob | updated_at_iso | note`
   columns; `doGet(e)` reads by `?key=`, `doPost(e)` writes a JSON body sent
   as `text/plain` (avoids CORS preflight, Apps Script 302-redirects and
   `fetch()` follows it transparently); deployed as a Web App with "Execute
   as: Me" / "Who has access: Anyone"; the per-user key is the only access
   control (accepted threat model). **Reuse this file near-verbatim** for both
   the refresh-trigger proxy and the favorites backend (see sections 4 and 8).
9. There is also a **manual "build JSON, splice into HTML, publish via
   Artifact tool" precedent**: `roster_maturity/` (`fetch_plays.py`,
   `fetch_teams.py`, `fetch_players.py`, `build_data.py`,
   `splice_artifact.py`, `refresh_all.py`). That flow is Alex-run-locally,
   not automated, and publishes to a Claude Artifact rather than GitHub
   Pages - not the right fit here since the whole point is a public,
   self-refreshing league page, but its `build_data.py` shape (Supabase ->
   normalize -> JSON) is a reasonable template for our build script's I/O
   contract if reading from Supabase; we're reading from the Sheet instead
   per section 0, so treat it as a shape reference only.
10. Repo currently has no `docs/` folder and no `.nojekyll`; GitHub Pages
    hosting needs to be enabled in repo settings (Pages -> Deploy from branch
    -> `main` / `docs`). **Confirmed: `arpeterson93/numberball` is public**,
    so no plan upgrade is needed to turn on Pages.

---

## 2. Decisions (resolved with Alex, 2026-07-29)

All blocking open questions from the first draft are resolved:

1. **Sheet ID**: `1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA` (already in
   `sync_plays.py`) is authoritative, not the link shared in chat.
2. **Key-moment thresholds**: locked in as placeholders, to be tuned once
   real output volume is visible (section 9, stage 3) -
   `LEVERAGE_THRESHOLD = 1.5`, `WPA_THRESHOLD = 0.10` (10 percentage
   points). See section 4.
3. **Repo visibility**: `arpeterson93/numberball` is public. No Pages plan
   blocker.
4. **`0 Diff` / `500 Diff`**: exact match, not a band.
5. **Lead change**: a play into or out of a tie counts as a lead change -
   tag fires on `sign(lead_before) != sign(lead_after)`.
6. **Favorites identity**: bare typed display name is fine, no passphrase.
   Name collisions (two "Alex"s sharing a list) are an accepted tradeoff,
   same threat model as `irrigation_planner`.
7. **Refresh PAT**: Alex will mint a fine-grained PAT (this repo only,
   `Actions: read and write`) and store it in Apps Script Script
   Properties. The `workflow_dispatch` trigger-proxy design in section 3c
   proceeds as designed.

One **non-blocking** item remains open, to resolve during implementation
rather than up front: exact semantics of the `1BWH`/`2BWH`/`bAuto`/`pAuto`
result codes (section 1, fact 7) - only matters if their hitting/pitching
classification turns out ambiguous when real data is inspected.

---

## 3. Data pipeline

### 3a. Build script: `key_moments_build.py` (new, repo root)

Run by GitHub Actions (or locally), not the Streamlit app. Steps:

1. Read MLN Games + MLN Plays directly from the Sheet via the existing
   `utils.read_mln_games_from_sheet(sheet_id)` /
   `utils.read_mln_plays_from_sheet(sheet_id, tab="Plays (Raw)")` /
   `utils.read_mln_team_abbrev_lookup(sheet_id)` helpers `sync_plays.py`
   already calls - no Supabase round-trip.
2. Read MLN players from the Sheet (or from `players_rows.csv` if that's
   already sheet-sourced - confirm which is authoritative) to get
   `name`, `sub_league`, `rookie`, and a stable player id for the favorites
   feature.
3. Re-run the same enrichment `sync_mln_plays()` already does (team
   abbreviation -> full name via `team_id_to_full`, player id -> name,
   `off_team`/`def_team` from `half`, `diff` via `circular_diff`, running
   `outs` via `outs_tracker` + `utils.outs_added`, `is_fp_inn`/`is_fp_app`) -
   either import and call the existing functions from `sync_plays.py`
   directly (preferred, avoids drift) or factor the shared logic out to
   `utils.py` if `sync_plays.py`'s functions are too Supabase-coupled to
   import standalone.
4. Sort each game's plays by `play_num`. Walk them in order, tracking
   `batting_lead` (batting team's score minus opponent's, from
   `away_score`/`home_score`) and `obc`/`outs` per half-inning (reuse
   `outs_tracker` pattern from `sync_plays.py`).
5. For each play compute:
   - `remaining = utils.remaining_half_innings(inning, half)`
   - `wp_before = utils.get_win_probability_interpolated(remaining, outs_before, obc_before, batting_lead_before)`
   - `wp_after` = same lookup using the state immediately after this play
     (next play's before-state, or, for the final play of a game, the
     known 100%/0% outcome)
   - `wpa = wp_after - wp_before` (signed; the mockup shows this as e.g.
     `+4.2` / `-10.3`)
   - `leverage = utils.compute_leverage(result_ranges, remaining, outs_before, obc_before, batting_lead_before)`
   - `lead_before` / `lead_after` for the batting team, to detect lead
     changes per Open Question 5.
6. Apply the key-moment tag rules (section 4) to populate `tags: []` and
   `is_key_moment` per play.
   Also compute two presentation flags per play (section 6d):
   - `is_half_inning_final = (outs_after == 3)`
   - `is_game_final` = this play is the last play (`play_num`) recorded for
     its `game_id`, cross-checked against the game's `end_time`/`last_play`
     field already present in the Games sheet/table (`_GAMES_TABLE_COLS` in
     `sync_plays.py`) so an in-progress game's most-recent play isn't
     mistaken for a true final out.
7. Write `docs/data/key_moments.json` (see section 6 for schema) plus a
   small `docs/data/meta.json` with `{ "built_at": <ISO8601>, "sheet_id":
   ..., "season": ..., "sessions": [...] }` so the page can show a "data as
   of" timestamp and populate the session-selector dropdown.
8. Exit non-zero on any hard error (missing sheet access, empty plays, etc.)
   so the Action run visibly fails rather than silently publishing stale/
   empty data.

### 3b. GitHub Actions workflow: `.github/workflows/key_moments.yml` (new)

- `on: schedule` (coarse backstop, e.g. every 30-60 min - don't copy the
  5-minute cron from `sync.yml`, it doesn't actually run that often per Fact
  2 and a tighter schedule just wastes Action minutes) + `workflow_dispatch`
  (manual and Apps-Script-triggered runs).
- Steps: checkout, setup Python, `pip install -r requirements.txt`, run
  `python key_moments_build.py`, then commit-and-push `docs/data/*.json` if
  changed (e.g. `git diff --quiet || (git commit -am "Refresh key moments" && git push)`,
  using `github-actions[bot]` identity, standard pattern for GH-Actions-writes-
  back-to-repo).
- Needs read access to the Google Sheet - same credential mechanism
  `sync_plays.py`/`utils.py` already use for sheet reads (service account or
  API key - check `utils.read_mln_plays_from_sheet`'s auth path and mirror
  it; this may already be credential-free if the sheet is public-readable
  via a published/export URL - verify before assuming a new secret is
  needed).

### 3c. Instant refresh: Apps Script trigger proxy

New Apps Script project (separate from the favorites one, or the same
project with an `action` query param branch - implementer's call), deployed
as a Web App:

```
doGet(e):
  if e.parameter.action == "trigger_refresh":
    # simple rate limit: refuse if last trigger < 60s ago (Script Properties timestamp)
    UrlFetchApp.fetch(
      "https://api.github.com/repos/arpeterson93/numberball/actions/workflows/key_moments.yml/dispatches",
      { method: "post", headers: { Authorization: "Bearer " + PAT, Accept: "application/vnd.github+json" },
        payload: JSON.stringify({ ref: "main" }), muteHttpExceptions: true }
    )
    return json_({ triggered: true })
```

PAT lives in Script Properties (`PropertiesService.getScriptProperties()`),
set once by Alex in the Apps Script editor, never in a file. Page's
"Refresh now" button calls this endpoint, then polls `docs/data/meta.json`'s
`built_at` (cache-busted with `?t=<timestamp>`) every few seconds for up to
~90s, swapping in fresh data and showing a toast when `built_at` advances;
times out gracefully with "still refreshing, check back shortly" if the
Action run is slow.

---

## 4. Key-moment tagging rules

`is_key_moment = true` if **any** of these hold (OR, not AND). Each rule has
a stable `tags[]` slug - a play can carry multiple tags at once (e.g. a
walk-off HR is both `lead_change` and `high_leverage`). These 8 slugs are
the complete, closed set used for the tag filter chips in section 6e - no
other tag values exist.

| `tags[]` slug | Rule |
|---|---|
| `zero_diff` | `diff == 0` ("0 Diff" pill) |
| `five_hundred_diff` | `diff == 500` ("500 Diff" pill) |
| `steal` | `result in {"SB","SB2","SB3","SB4"}` (successful steal only, not `CS*`) |
| `strikeout_risp_inning_end` | `result == "K" and outs_after == 3 and (obc_before[0]=="1" or obc_before[1]=="1")` |
| `run_scoring_hit` | `result in HIT_CODES and runs_scored_this_play > 0` (`runs_scored_this_play` derived from the `scored2/scored3/scored4` columns already on the raw play row per `migrate_plays.sql`, or from the before/after score delta) |
| `bases_loaded` | `obc_after == "111"` (loading the bases is the moment, not merely already being loaded) |
| `lead_change` | `sign(lead_before) != sign(lead_after)` where lead is the batting team's perspective - a play into or out of a tie counts |
| `high_leverage` | `leverage >= LEVERAGE_THRESHOLD` or `abs(wpa) >= WPA_THRESHOLD`, with **`LEVERAGE_THRESHOLD = 1.5`, `WPA_THRESHOLD = 0.10`** (10 percentage points) as starting placeholders - tune against real per-session volume in implementation stage 3 (section 9), targeting something near the mockup's 37-moments-per-session ballpark |

Filter chips (independent of the inclusion rule, applied client-side or at
build time - implementer's call, but client-side is cheaper since it avoids
rebuilding the whole feed for a UI toggle):

- **Hitting / Pitching**: `result in HITTING_CODES` vs `result in
  PITCHING_CODES` (section 1 fact 7 groupings)
- **HR**: `result == "HR"`
- **League chips (MLN / Galactic / Liberty)**: "MLN" = no filter (all MLN
  plays); "Galactic"/"Liberty" = `player.sub_league` matches (per Fact -
  `players.sub_league` stores `GL`/`LL` already - confirm the two display
  labels map to those two codes)
- **Team**: `off_team == selected or def_team == selected`
- **Player**: substring match on `batter_name`/`pitcher_name`
- **Rookies only**: `player.rookie == true` for whichever side is relevant
  to the result - the batter for hitting results, the pitcher for pitching
  results (so a rookie pitcher's strikeout surfaces even against a veteran
  batter)
- **Favorites only**: batter or pitcher player id in the signed-in user's
  favorites list (section 8)

Sort options (mockup shows Chronological / WPA / Leverage): straightforward
client-side sort over the already-computed fields, no build-time work.

---

## 5. `key_moments.json` schema

```jsonc
{
  "moment_id": "13_00042",          // "<session>_<play_num>" or similar stable id
  "session_number": 13,
  "season": 13,
  "game_code": "...",
  "timestamp": "2026-07-09T18:20:00Z",
  "inning": 4,
  "half": "bottom",                  // drives the up/down triangle indicator
  "outs_before": 1,
  "outs_after": 2,
  "obc_before": "001",
  "obc_after": "000",
  "result": "K",
  "result_category": "pitching",     // "hitting" | "pitching"
  "diff": 12,
  "batter_name": "Thomas Thompson III",
  "batter_id": "13_4821",
  "pitcher_name": "...",
  "pitcher_id": "13_...",
  "off_team": "Humongous Melonheads",
  "off_team_abbr": "HMH",
  "def_team": "Reykjavik Valkyries",
  "def_team_abbr": "REK",
  "away_score": 2,
  "home_score": 6,
  "batting_is_home": true,
  "win_prob_before": 0.568,
  "win_prob_after": 0.61,
  "wpa": 0.042,
  "leverage": 1.1,
  "sub_league": "GL",
  "rookie": false,
  "is_key_moment": true,             // true if tags is non-empty (section 4)
  "tags": ["strikeout_risp_inning_end"],   // 0+ of the 8 slugs in section 4's table
  "is_half_inning_final": false,     // outs_after == 3 (this play ended the half-inning)
  "is_game_final": false             // this is the last play of the game (section 3a step 6)
}
```

`meta.json`:

```jsonc
{ "built_at": "2026-07-29T20:14:03Z", "season": 13, "sessions": [13, 12, 11, ...] }
```

Session selector (Open Question resolved: "full season with a session
selector") reads `meta.sessions` for the dropdown; the page can either load
one `key_moments.json` covering the whole season and filter client-side
(simplest, fine at this data volume - a session is dozens of moments, a
season is maybe a few hundred) or split into per-session files if the season
file gets unwieldy. Recommend starting with one file, split later only if
load time becomes a problem.

---

## 6. Frontend (`docs/index.html`, adapted from `key_moments_mock_v5.html`)

### 6a. Layout changes from the mockup

1. Delete: the `Scores & Standings / Key Moments / Schedule / Rosters /
   League Leaders / Player Stats / Free Agents` tab bar, the `SESSION 13
   LIVE` season header, and the `Draft Class` dropdown + its filter-group
   wrapper.
2. **Title row**: `KEY MOMENTS` heading plus a session `<select>` styled
   inline with it (populated from `meta.json.sessions`, defaulting to the
   most recent), rendered as `Session 3` - **plain integer, no zero-padding**
   (if `session_number` arrives as a zero-padded string from the sheet,
   `parseInt`/strip the leading zero at render time; never display `Session
   03`). Right-aligned in the same row: a humanized last-updated timestamp
   (`meta.json.built_at`) and the "Refresh now" button (section 3c).
3. **Filters card, row 1 (chips only)**: `Result` chip group
   (Hitting/Pitching/HR), `League` chip group (MLN/Galactic/Liberty),
   `Rookies` chip, `Favorites` chip - see 6b for click behavior differences
   between these groups.
4. **Filters card, row 2 (fields only)**: `Team` `<select>` and the `Player`
   text search, left where the old row's Team/Player slots were but now on
   their own line beneath the chip row.
5. **Filters card, row 3 (tag chips)**: all 8 `tags[]` slugs from section 4
   as always-visible, multi-select toggle chips (e.g. "0 Diff", "500 Diff",
   "Steal", "Inning-ending K w/ RISP", "Run-scoring hit", "Bases loaded",
   "Lead change", "High leverage") - see 6b for why this group behaves
   differently from row 1's chips.
6. **Filters footer**: unchanged - count text (`"N key moments"` /
   `"N plays"` depending on the active mode, see 6c) and the reset-filters
   link.
7. **Sort bar**: drop the `"SORT"` label text entirely; keep just the three
   sort chips (Chronological / WPA / Leverage). This group was always
   effectively single-select (a feed only has one sort order at a time), so
   no behavior change, just the label removal.

### 6b. Chip group click behavior

- **Result** and **Rookies**/**Favorites** are independent of each other -
  Rookies and Favorites each toggle on/off on their own and AND together
  with whatever else is active; they are not part of the Result radio group
  despite sitting in the same visual row.
- **Result** (Hitting/Pitching/HR) is a true radio group: clicking a chip
  activates it and deactivates the other two. Clicking the **already-active**
  chip deactivates it, returning to the group's default state - **no chip
  active = no Result filter applied (all categories shown)**. This differs
  from the original mockup, where "Hitting" started pre-selected; the real
  default state is nothing selected.
- **League** (MLN/Galactic/Liberty) is also a radio group, but always has
  exactly one chip active - `MLN` is both a real choice and the group's
  neutral default (all MLN sub-leagues), so there's no separate "none
  active" state to design for here, unlike Result.
- **Rookies** / **Favorites** are simple independent toggle chips (visually
  matching the other chips for consistency, but behaviorally just
  checkboxes) - each fires its own boolean AND condition.
- **Tag chips** (row 3, section 6a item 5) are a third distinct behavior:
  **multi-select, OR'd together** - checking "Steal" and "Bases loaded"
  shows plays with *either* tag, not only plays with both, since a play's
  `tags[]` is not mutually exclusive the way Result/League are. The whole
  tag-chip group then ANDs with everything else active on the page (Result,
  League, Rookies, Favorites, Team, Player). Net: three different chip
  behaviors coexist on one page (radio / independent-toggle /
  multi-select-OR) - worth a subtle visual distinction (e.g. a small
  checkmark vs. dot) so users aren't surprised when tag chips don't
  exclude each other the way Result's do.

### 6c. "Key Moments" toggle - all plays, not just key moments (revised)

The build script emits **every play for the session, not only the ones
tagged as key moments** (section 3a step 6 / section 5 schema both updated
accordingly) - this costs almost nothing extra, since the build script
already walks every play in order to compute WPA/leverage before it can
even decide which ones qualify as key moments.

Superseding the original design (which tied "all plays" to the Favorites
chip specifically): the pool switch is its own toggle chip, **"Key
Moments," active by default**, living alongside Rookies/Favorites in the
toggle-chip group (section 6a item 5 / 6b) - not a radio group member,
just another independent boolean, except its default is on rather than off.
Every other filter, Favorites included, is a plain AND condition on top of
whichever pool is active:

- **Key Moments on** (default): pool = `key_moments.json`'s season-wide
  feed, filtered to the active session. Footer reads `"N key moments"`.
- **Key Moments off**: pool = every play of the active session(s), lazily
  fetched from `plays_NN.json` the first time it's needed (same lazy-load
  mechanism, just triggered by this toggle instead of Favorites). Footer
  reads `"N plays"`.
- **Favorites**, when on, narrows whichever pool is active to plays where
  the batter/pitcher/featured player is on the user's list - it no longer
  changes *which* pool is loaded by itself. Concretely: to browse one
  favorited player's entire session (not just their key moments), turn Key
  Moments off *and* Favorites on - two independent toggles rather than one
  chip doing both jobs. This is a deliberate simplification over the
  original one-chip-does-both design: Favorites answers "whose plays,"
  Key Moments answers "how many of them," and conflating the two meant
  Favorites couldn't be combined with anything else that also wanted the
  full play pool.
- Result / League / Rookies / Team / Player / tag filters still apply as
  AND conditions on top of whichever pool is active, exactly as before.
- Data volume: a full session's plays (low hundreds) is still trivially
  small for a static JSON fetch and client-side `.filter()`; no pagination
  or lazy-loading needed at this scale. Revisit only if a full-season file
  (section 5's "one file, split later if needed" note) turns out slow to
  load on mobile.

### 6d. Data wiring

1. `.moment` rows generated from `key_moments.json` entries instead of
   being hand-written; `.lev-bar` class (`high`/`low`/`neutral`) driven by
   the `leverage` value against the same thresholds used for
   `is_key_moment` tagging; base diamond SVG driven by `obc_after` (port
   `utils.bases_diamond_svg`'s geometry/logic to JS, or keep it
   server-rendered per-moment at build time and embed the SVG string
   directly in the JSON - the latter avoids duplicating the geometry logic
   in two languages and is recommended).
2. **Outs indicator**: beneath the base diamond SVG in `.moment-right`, add
   2 small circles filled left-to-right per **`outs_after`** (consistent
   with the diamond and score block already showing post-play state, not
   pre-play). This is also why the meta-line's 2nd text item can just
   always show Leverage (previous section's decision) - outs now has its
   own dedicated visual, no need to duplicate it as text.
3. **Half-inning-final / game-final states** override the normal diamond +
   outs-circle pair entirely (check `is_game_final` first, since the last
   play of a game is also its final out and should never show the
   half-inning badge instead):
   - `is_game_final == true` -> both the base diamond and the outs circles
     are replaced by a single **`FINAL`** badge.
   - else if `is_half_inning_final == true` (this play recorded the 3rd
     out, game continues) -> both are replaced by a single **`END INNING`**
     badge.
   - otherwise -> normal base diamond + outs circles as described above.
   This also means the 2-outs-circle design never actually needs to
   represent a 3rd out visually (unlike the earlier draft of this section)
   - `outs_after == 3` always routes through one of the two badge states
     instead.
2. All filtering/sorting is client-side over the loaded JSON (no server
   round-trip per interaction) - straightforward array `.filter()`/`.sort()`
   plus a re-render, no framework needed given the page's size.
3. No build tooling (webpack/etc.) - plain HTML/CSS/JS in `docs/`, matching
   `irrigation_planner/docs/`'s approach, deployed as-is by GitHub Pages.

---

## 7. Favorites feature

1. **New Google Sheet** (not the plays workbook), one tab `favorites`,
   columns `key | player_ids_json | updated_at_iso | note` - identical shape
   to `irrigation_planner`'s `configs` tab.
2. **New Apps Script project**, `Code.gs` copied near-verbatim from
   `irrigation_planner/apps-script/Code.gs` (rename `config` ->
   `player_ids`, `SHEET_NAME` -> `"favorites"`). Deploy as Web App,
   "Execute as: Me", "Who has access: Anyone", per
   `irrigation_planner/apps-script/DEPLOY.md`'s steps 1-4 - Opus should
   follow that file's steps directly rather than re-deriving the Apps
   Script deploy flow.
3. **Client (`docs/js/favorites.js`)**: on load, prompt for a display name
   (stored in `localStorage`, entered once), GET
   `?key=<slugified name>&action=favorites`, cache the returned
   `player_ids` list in memory. A "Manage favorites" panel lets the user
   search the player list already present in `key_moments.json` (or a
   separate `docs/data/players.json`) and toggle stars; toggling POSTs the
   updated array back (same `text/plain` JSON-body trick as
   `irrigation_planner`'s `sync.js`, to dodge CORS preflight).
4. **"Favorites" toggle** in the filter bar shows only moments where
   `batter_id` or `pitcher_id` is in the cached favorites list.
5. This is explicitly *not* real auth (matches Open Question 6's accepted
   threat model) - document that clearly in the UI copy ("no password, just
   remembers your name on this device") so nobody mistakes it for account
   security.

---

## 8. Hosting

1. Create `docs/` on `main` (not a `gh-pages` branch) - matches
   `irrigation_planner`. Add `docs/.nojekyll` (Jekyll otherwise mangles
   files/folders starting with `_`, and there's no reason to run Jekyll
   here).
2. Repo Settings -> Pages -> Deploy from branch -> `main` / `docs`. Repo is
   already public (section 2), so no plan upgrade is needed.
3. `key_moments_build.py`'s output (`docs/data/*.json`) is committed by the
   Action (section 3b) - the only thing manually deployed is the Apps
   Script web app URLs pasted into `docs/js/*.js`, exactly as
   `irrigation_planner/apps-script/DEPLOY.md` step 3.5 describes.

---

## 9. Suggested implementation order

1. ~~Resolve open questions with Alex~~ - done, see section 2.
2. `key_moments_build.py` + confirm it can read the Sheet standalone
   (reusing `sync_plays.py`/`utils.py` helpers) and produces sane
   `wpa`/`leverage` numbers against a real recent session - spot-check a
   handful of moments by hand against the mockup's example rows.
3. Tune the placeholder inclusion thresholds (section 4:
   `LEVERAGE_THRESHOLD`/`WPA_THRESHOLD`) against real output volume before
   wiring up the frontend, so the frontend isn't built against a feed that
   turns out to be empty or overwhelming.
4. `docs/index.html` + `docs/js/*.js` static build against a checked-in
   sample `key_moments.json` (no live pipeline needed yet) - get the UI
   right first.
5. `.github/workflows/key_moments.yml` (schedule + workflow_dispatch),
   verify a manual `workflow_dispatch` run produces the JSON and the page
   picks it up.
6. Apps Script refresh-trigger proxy + "Refresh now" button.
7. Favorites Sheet + Apps Script + frontend wiring.
8. Enable GitHub Pages, do an end-to-end smoke test from a phone/incognito
   window (to catch anything that accidentally depended on Alex's own
   browser state).
9. Mobile/small-screen responsiveness pass - see section 10. Do this after
   stage 4's desktop UI is solid, not before - a moving target is harder
   to make responsive than a finished one.

---

## 10. Mobile / small-screen responsiveness (addendum)

Added after initial testing surfaced two concrete problems on a phone
against the mockup's current CSS: filter chip rows run off the right edge
of the screen instead of wrapping, and play/moment rows become very tall.

### 10a. Root causes in the existing mockup CSS

1. `.chip-row{display:flex;flex-wrap:nowrap;gap:8px;}` - `nowrap` is
   exactly why chip groups (Result, League, and now Rookies/Favorites/the
   8 tag chips from this plan's later additions) overflow horizontally on
   a narrow screen instead of wrapping to a second line.
2. `.filters-grid{grid-template-columns: 1.2fr 1.3fr 0.8fr 1fr 1fr;}` only
   collapses to `1fr 1fr` at `@media (max-width: 900px)` - still two
   columns, still cramped on a ~390px phone, and doesn't account for this
   plan's new 3-row filters-card structure (section 6a) at all.
3. `.moment{display:flex;...}` only gets `flex-wrap:wrap` at the same
   900px breakpoint - `.moment-left` and `.moment-right` then each become
   full-width blocks stacked vertically, and `.moment-right`'s own
   children (inning indicator, score block, base diamond, outs
   indicator/badge) keep their desktop sizing, so the stacked block is
   both full-width and tall. This compounds with the new outs-indicator
   and `FINAL`/`END INNING` badge states (section 6d) adding still more
   content to that same right-hand cluster.

### 10b. Breakpoints

Add a phone breakpoint alongside the existing `900px` one:

- `@media (max-width: 900px)` - tablet/narrow-laptop: keep the existing
  `.filters-grid` collapse for this range.
- `@media (max-width: 600px)` - phone: the changes below apply.

### 10c. Filters card: collapsed by default on phone

Alex's call: below 600px, the entire filters card (all 3 rows from section
6a) collapses into a single compact bar by default, rather than always
showing everything expanded.

- Bar reads something like `Filters (2 active) ▾` (count reflects
  however many chips/fields are currently non-default), plus the existing
  Sort control alongside it so sorting stays reachable without expanding
  anything.
- Tapping the bar expands the full filter panel (all 3 rows, chips
  wrapping per 10d) in place, pushing the moments list down; tapping again
  collapses it back. A simple `<details>`/`<summary>`-style disclosure (or
  the JS equivalent) is enough - no modal/bottom-sheet needed.
- Above 600px, the filters card always renders expanded exactly as
  designed in section 6a - the collapse behavior is phone-only.
- Rationale: most phone visitors are browsing the feed, not filtering -
  this keeps the first screen mostly moments, not chips.

### 10d. Chip wrapping (inside the expanded panel)

Once expanded, `.chip-row` switches to `flex-wrap: wrap` below 600px, not
horizontal scroll - a scrollable chip row hides options users won't know
are there, which is a real discoverability problem for something like the
8 tag chips. Each chip group wraps independently onto as many lines as it
needs; group labels (`Result`, `League`, etc.) stay above their own chip
row same as desktop.

### 10e. Compact scorebug strip for `.moment-right`

Below 600px, `.moment-right`'s children shrink rather than stack:

- Base diamond SVG and outs-circle pair (or the `FINAL`/`END INNING`
  badge, section 6d) render at a smaller fixed size.
- Score block font size drops one step; team abbreviations already stay
  3-4 chars so width doesn't grow further.
- Inning indicator (triangle + number) shrinks correspondingly.
- Target: the whole `.moment-right` cluster stays a **single horizontal
  row** at phone widths (aim for roughly 140-160px wide), sitting below
  `.moment-left` as a second line within the card - not wrapping into
  multiple internal lines the way the mockup's current 900px breakpoint
  causes. This is the main fix for "rows become quite tall."
- `.moment-left`'s own content (timestamp, player name + result/diff
  pills, win-probability + leverage meta line) should already wrap
  reasonably via its existing `flex-wrap:wrap` on `.play-line`/`.meta-line`
  - verify pill text doesn't force horizontal overflow at 360-390px widths
  (the narrowest common phones), shrinking pill font-size/padding slightly
  if it does.

### 10f. Title row

Below 600px, the title row (`KEY MOMENTS` heading + session `<select>` +
last-updated timestamp + Refresh button, section 6a item 2) wraps to two
lines: heading + session selector on the first line, timestamp + Refresh
button on a second line beneath - avoids the refresh control getting
squeezed or overflowing next to the heading.

### 10g. Validation checklist

Test against real device widths, not just the media-query breakpoints:
360px and 390px (common Android/iPhone), 428px (larger iPhone), 768px
(iPad portrait). For each:

- No horizontal scroll on `<body>` anywhere on the page (chips, moment
  rows, title row all included).
- A single moment row's total height stays reasonable (rough target:
  well under the mockup's current wrapped-and-stacked result at narrow
  widths).
- The collapsed filters bar and its expand/collapse both work with touch
  (not just mouse hover states, which don't exist on phones - watch for
  any chip/hover-only affordance carried over from the desktop design).

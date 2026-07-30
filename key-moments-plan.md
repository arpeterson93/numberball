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
6. Apply the key-moment tag rules (section 5) to decide inclusion and to
   populate a `tags: []` array per moment (drives the Result/HR filter chips
   and any future "why is this here" UI).
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

A play is included in the feed if **any** of these hold (OR, not AND):

- `diff == 0` ("0 Diff")
- `diff == 500` ("500 Diff")
- `result in {"SB","SB2","SB3","SB4"}` (successful steal)
- Inning-ending strikeout with a runner in scoring position:
  `result == "K" and outs_after == 3 and (obc_before[0]=="1" or obc_before[1]=="1")`
- Any hit that scores at least one run: `result in HIT_CODES and runs_scored_this_play > 0`
  (`runs_scored_this_play` derived from the `scored2/scored3/scored4` columns
  already on the raw play row per `migrate_plays.sql`, or from the
  before/after score delta)
- Bases loaded reached: `obc_after == "111"` (loading the bases is the
  moment, not merely being loaded already)
- Lead change (tie counts as a lead change either direction):
  `sign(lead_before) != sign(lead_after)` where lead is the batting team's
  perspective
- `leverage >= LEVERAGE_THRESHOLD` or `abs(wpa) >= WPA_THRESHOLD`, with
  **`LEVERAGE_THRESHOLD = 1.5`, `WPA_THRESHOLD = 0.10`** (10 percentage
  points) as the starting placeholders - tune against real per-session
  volume in implementation stage 3 (section 9), targeting something near
  the mockup's 37-moments-per-session ballpark

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
  "tags": ["strikeout_risp_inning_end"]   // which inclusion rule(s) fired
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

1. Delete: the `Scores & Standings / Key Moments / Schedule / Rosters /
   League Leaders / Player Stats / Free Agents` tab bar, the `SESSION 13
   LIVE` season header, and the `Draft Class` dropdown + its filter-group
   wrapper. Keep the `Rookies only` checkbox.
2. Add: a session-number `<select>` (populated from `meta.json.sessions`,
   defaulting to the most recent), a "Favorites only" toggle next to the
   existing filter chips, and a "Refresh now" button with a last-updated
   timestamp (`meta.json.built_at`, humanized) near the filters footer.
3. Wire the existing static markup to real data: `.moment` rows generated
   from `key_moments.json` entries instead of being hand-written; `.lev-bar`
   class (`high`/`low`/`neutral`) driven by the `leverage` value against the
   same thresholds used for inclusion; base diamond SVG driven by
   `obc_after` (port `utils.bases_diamond_svg`'s geometry/logic to JS, or
   keep it server-rendered per-moment at build time and embed the SVG string
   directly in the JSON - the latter avoids duplicating the geometry logic
   in two languages and is recommended).
4. All filtering/sorting is client-side over the loaded JSON (no server
   round-trip per interaction) - straightforward array `.filter()`/`.sort()`
   plus a re-render, no framework needed given the page's size.
5. No build tooling (webpack/etc.) - plain HTML/CSS/JS in `docs/`, matching
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

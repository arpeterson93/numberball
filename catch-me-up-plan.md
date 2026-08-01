# "Catch Me Up" - implementation plan

## Why

An animated way to review every play added to the feed since you last had
the page open, grouped game-by-game, tied to the same per-name identity
Favorites already uses (not a new login system, not per-device
`localStorage` tracking - a name typed once, synced via the existing
Apps Script backend, so "caught up" follows you across devices the same way
your favorites list already does).

Project constraints that apply throughout (existing repo rules):
- No em dashes in any `.py` file - hyphens only (this feature is almost
  entirely `docs/js`/`docs/index.html`/`docs/css`/`apps-script/Code.gs`, none
  of which that rule applies to, but any Python touched incidentally still
  follows it).
- No Co-Authored-By trailer if committing.

---

## Resolved decisions (do not re-litigate)

Asked and answered 2026-08-01, all four went with the recommended option:

1. **Cursor timing: advances automatically whenever the page/feed loads**,
   not gated on the user actually opening or finishing the Catch Me Up
   slideshow. Browsing the page normally already counts as "seen." This
   means an accidental open-then-close of the slideshow itself can't lose
   anything - the cursor already moved forward the moment the page loaded,
   independent of what the user does with the button.
2. **Content scope: every new play, all games, chronological/game order -
   no favorites filtering.** The favorites identity is only how the feature
   knows who you are and when you last looked; it is not a content filter.
   (Flagged as a real interaction to watch in validation: combined with
   decision 3's "no cap," a long absence could produce a very long
   slideshow full of routine plays, not just exciting ones - see Open
   Question 1.)
3. **No cap.** Show everything since last visit, however many sessions that
   spans, revisit only if it proves to be a real problem in practice.
4. **Controls: auto-advance, tap-to-pause, close/skip-all.** No manual
   prev/next stepping in v1.

---

## Architecture overview

Four pieces, in dependency order:

1. **Backend**: one new column (`last_seen_iso`) on the existing
   `favorites` Sheet, read as part of the favorites GET response Favorites
   already fetches on boot (no new network round-trip), written via one new
   `action=mark_seen` POST path that's independent of the existing
   favorites-save path (so toggling a star can never clobber the cursor,
   and marking-seen can never clobber the favorites list - see Stage 1's
   warning about this).
2. **Cursor read/write**: `favorites.js` gains `lastSeen()` and
   `markSeenNow()`. App boot reads the OLD cursor, computes and caches the
   "new since" set from it, then writes the new cursor (now) - in that
   order, so the value used to compute what's new is never the value that
   just got written.
3. **Data**: "new since" plays can span multiple sessions -
   `ensurePlaysLoaded()`'s existing lazy per-session fetch pattern
   (`app.js:672`) is reused/generalized rather than reimplemented.
4. **UI**: a full-screen overlay reusing the existing `card(m)` renderer
   (`app.js:400`) for each play, with game-transition title cards between
   groups, auto-advance/pause/close, and a discoverable entry point that
   only makes itself prominent when there's actually something new.

---

## Stage 1 - `apps-script/Code.gs`: `last_seen_iso` column

### 1a. Schema

```js
var HEADERS = ['key', 'player_ids_json', 'updated_at_iso', 'note', 'last_seen_iso'];
```

**Critical correctness requirement - read this before touching
`writeFavorites_`:** that function currently builds a fixed 4-element
`record` array and writes all `HEADERS.length` columns
(`sh.getRange(row, 1, 1, HEADERS.length).setValues([record])`,
`Code.gs` current `writeFavorites_`). If `HEADERS.length` becomes 5 while
`record` stays 4 elements, **every ordinary favorites-star toggle will wipe
`last_seen_iso` back to blank**, since `setValues` writes exactly what's in
the array, dimension-mismatched or not (Apps Script either errors or writes
`undefined`/blank into the extra column, neither of which preserves the
existing value). `writeFavorites_` must read the row's *current*
`last_seen_iso` first (empty string if the row doesn't exist yet) and
include it unchanged as the 5th element of `record`. Symmetrically, the new
`markSeen_` function (1b) must read the row's current `player_ids_json`/
`note` and preserve those when it only intends to update `last_seen_iso`.
Both directions of this preserve-the-other-field discipline are the same
bug shape as SQL's classic "read-modify-write without reading first" - test
both explicitly (validation step 3).

### 1b. `doPost` - new `mark_seen` action, alongside the existing default path

```js
function doPost(e) {
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var key = normalizeKey_(body.key);
    if (!key) {
      return json_({ error: 'missing key' });
    }
    if (body.action === 'mark_seen') {
      return json_(markSeen_(key, body.last_seen || new Date().toISOString()));
    }
    var ids = (body.player_ids || [])
      .map(function (v) { return parseInt(v, 10); })
      .filter(function (v) { return !isNaN(v); });
    return json_(writeFavorites_(key, ids, body.note || ''));
  } catch (err) {
    return json_({ error: String(err) });
  }
}

function markSeen_(key, iso) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = sheet_();
    var row = findRow_(sh, key);
    if (row === -1) {
      // Brand-new key with no favorites row yet - create one with empty
      // favorites rather than forcing the client to favorites-save first.
      sh.appendRow([key, '[]', '', '', iso]);
      return { key: key, last_seen_iso: iso };
    }
    sh.getRange(row, 5, 1, 1).setValue(iso);   // touch only column 5
    return { key: key, last_seen_iso: iso };
  } finally {
    lock.releaseLock();
  }
}
```

Writing only `getRange(row, 5, 1, 1)` for the mark-seen path (rather than
rewriting the whole row like `writeFavorites_` does) sidesteps the
preserve-the-other-columns problem entirely for *this* direction - it's
`writeFavorites_` (1a) that needs the explicit read-then-preserve fix, since
it rewrites the full row for an unrelated reason (saving favorites).

### 1c. `readFavorites_` - include the new column

```js
function readFavorites_(key) {
  var sh = sheet_();
  var row = findRow_(sh, key);
  if (row === -1) {
    return { key: key, player_ids: [], updated_at: null, last_seen_iso: null };
  }
  var vals = sh.getRange(row, 1, 1, HEADERS.length).getValues()[0];
  var ids = [];
  try { ids = JSON.parse(vals[1] || '[]'); } catch (err) { ids = []; }
  return { key: key, player_ids: ids, updated_at: vals[2] || null, last_seen_iso: vals[4] || null };
}
```

### 1d. Deployment - manual step, not covered by `git push`

Apps Script changes are **not** picked up by GitHub Pages the way the rest
of `docs/` is - this needs a manual redeploy in the Apps Script editor
(new version, same Web App URL), same as every other `Code.gs` change to
this project per `apps-script/DEPLOY.md`. Flag this clearly when handing
off - it's easy to ship the client-side pieces (Stages 2-4) and forget the
backend half needs its own separate deploy action.

---

## Stage 2 - `docs/js/favorites.js`: cursor read/write

Add to the module's private `state`: `lastSeenIso: null`. In `load()`
(the existing `GET ?key=<slug>` handler), also capture
`state.lastSeenIso = (data && data.last_seen_iso) || null` from the response
- no new request, this rides the favorites GET that already happens on
`KMFavorites.init()`.

New public methods on the `window.KMFavorites` object (`favorites.js:199`):

```js
lastSeen: function () { return state.lastSeenIso; },
markSeenNow: function () {
  if (!state.key || !endpoint()) return Promise.resolve(null);
  var iso = new Date().toISOString();
  return fetch(endpoint(), {
    method: "POST",
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify({ key: state.key, action: "mark_seen", last_seen: iso }),
  })
    .then(function (r) { return r.json(); })
    .then(function () { state.lastSeenIso = iso; return iso; })
    .catch(function () { return null; });
},
```

Mirrors `save()`'s existing `text/plain` CORS-preflight-dodge trick
(`favorites.js:92-96`) exactly. No-ops cleanly (matches every other
endpoint-dependent path in this file) when no name is set or the endpoint
isn't configured - Catch Me Up simply has nothing to show in that case
(Stage 4 handles the empty/unavailable state).

**No name set at all** (first-time visitor, `state.key` is null): Catch Me
Up's entry point (Stage 4d) should route to the *existing* Favorites modal
(`#favorites-btn`'s `openPanel()`, `favorites.js:169`) rather than building
a second name-prompt UI - that modal already leads with the name input.
Once a name is saved there, retry.

---

## Stage 3 - computing the "new since" set

New function in `docs/js/app.js`, called once after `KMFavorites.init()`
resolves during boot (`app.js` boot flow, near where `renderScoreboard()`/
`populateSessionSelect()` already run post-init):

```js
function computeCatchUp() {
  var cursor = window.KMFavorites && window.KMFavorites.lastSeen();
  if (!cursor) {
    // First-ever visit for this name: nothing to catch up on, but start
    // tracking from now - see Open Question 2 on this default.
    if (window.KMFavorites) window.KMFavorites.markSeenNow();
    return Promise.resolve([]);
  }
  var sessions = (data.meta.sessions || []).slice();
  return Promise.all(sessions.map(function (s) {
    return data.playsBySession[s] ? Promise.resolve(data.playsBySession[s])
      : getJSON("data/plays_" + pad2Session(s) + ".json").then(function (rows) {
          data.playsBySession[s] = rows;
          return rows;
        });
  })).then(function (bySession) {
    var newPlays = [].concat.apply([], bySession).filter(function (p) {
      return p.timestamp && p.timestamp > cursor;
    });
    window.KMFavorites.markSeenNow();   // advance the cursor now that the old
                                        // value has been used, per decision 1
    return groupByGame(newPlays);
  });
}
```

- Fetches **every** session's `plays_NN.json`, not just the currently
  active filter's sessions - decision 3 ("no cap") means correctness here
  matters more than the optimization of guessing which sessions are
  relevant. A typical season is a handful of sessions of a few hundred
  plays each; this is a few small JSON fetches, not a heavy operation. If
  it does turn out to be slow in practice (many-season backlog), the
  optimization is to skip sessions entirely older than the session implied
  by `cursor`'s date - not implemented now, per the "add a cap later only
  if needed" spirit of decision 3.
- Plain string comparison (`p.timestamp > cursor`) works correctly here
  the same way it already does for the existing chronological sort
  (`app.js:275`) - both are naive `"YYYY-MM-DDTHH:MM:SS"` strings in the
  same format, so lexicographic order matches chronological order without
  needing the `parseChicagoNaive` timezone conversion (that conversion is
  purely a *display* concern; comparing two naive strings in the same
  source zone against each other needs no zone math at all).
- `markSeenNow()` is called **after** building `newPlays` from `cursor`,
  never before - reread decision 1's ordering requirement if touching this
  function.

`groupByGame(plays)` - groups by `game_code`, orders groups by each group's
earliest play's `timestamp`, orders plays within a group by `play_num`.
Returns `[{game_code, away_team_abbr, home_team_abbr, session_number,
plays: [...]}, ...]` (team abbreviations available on any play row already,
no extra lookup needed).

`computeCatchUp()`'s result is cached once in memory
(`data.catchUpGroups`, say) for the lifetime of this page load - Stage 4's
slideshow just reads that cache when opened, it doesn't recompute per open.

---

## Stage 4 - the slideshow overlay

### 4a. Markup - `docs/index.html`, new modal alongside the existing
`#modal`/favorites modal

```html
<div class="modal catchup-modal" id="catchup-modal" hidden>
  <div class="catchup-card" id="catchup-card">
    <button type="button" class="catchup-close" id="catchup-close" aria-label="Close">&times;</button>
    <div class="catchup-progress" id="catchup-progress"></div>
    <div class="catchup-slide" id="catchup-slide"></div>
    <div class="catchup-pause-hint" id="catchup-pause-hint" hidden>Paused - tap to resume</div>
  </div>
</div>
```

Full-viewport scrim (reuse `.modal`'s existing `position:fixed;inset:0`
treatment), but `.catchup-card` is styled taller/more presentation-like
than the compact favorites modal - closer to a single centered slide filling
most of the viewport on phone.

### 4b. Slide sequence and timing

Flatten `data.catchUpGroups` into an ordered slide list: one **title
slide** per game (`"Couriers @ Sharks - Session 4"`, `~1.8s` dwell), then
that game's plays in order (each rendered via the existing `card(m)`,
`~3.5s` dwell per play - both numbers are starting points, not carved in
stone, tune during validation). A thin progress bar or `"Play 12 of 47"`
text (`#catchup-progress`) gives a sense of how much is left, which matters
more here than in the main feed precisely because decision 3 means a run
could be long.

### 4c. Controls

- **Auto-advance**: `window.setTimeout` per slide, cleared/rescheduled on
  each advance - same `window.setTimeout`-based pattern already used
  elsewhere in this file (`pollForRebuild`, `toast`'s auto-hide timer)
  rather than introducing a new async pattern.
- **Tap-to-pause**: click/tap anywhere on `.catchup-slide` (excluding the
  close button) toggles a `paused` flag; the pending timeout is cleared on
  pause and a fresh one scheduled (for the *remaining* dwell time, not a
  full reset) on resume. Show `#catchup-pause-hint` while paused.
- **Close/skip-all**: `#catchup-close` click, or the scrim itself (matching
  the existing modal's `if (e.target === modal) closePanel()` convention,
  `favorites.js:179`), tears down the timer and hides the modal
  immediately - explicitly the requirement Alex called out (an accidental
  open must be trivially escapable). Because decision 1 already advanced
  the cursor at page-load time regardless, closing early has **no data
  consequence** - it only stops the animation, nothing about "what's been
  seen" changes as a result of closing vs. finishing.

### 4d. Entry point - a banner, not another header icon

The title-row header cluster is already tight on phone (title + session +
4 icon buttons at 24px each, worked out over several prior rounds) - adding
a 5th icon there is not recommended. Instead: a dismissible banner/button
that appears **only when `computeCatchUp()`'s result is non-empty**,
placed between the title row and the scoreboard section (same general area
as `#refresh-status`), reading something like
`"▶ Catch Me Up (23 new plays)"`. When the result is empty (nothing new,
or no name set yet), either hide the banner entirely or show a quiet
`"You're all caught up"` line - hiding entirely is probably cleaner and is
the recommended default, but this is a small enough UI call that Opus can
use judgment here without checking back in.

### 4e. CSS - `docs/css/style.css`

New rules for `.catchup-modal`/`.catchup-card`/`.catchup-slide`/
`.catchup-progress`/`.catchup-pause-hint`/the entry banner - reuse existing
tokens (`--navy`, `--tint`, `--shadow`, the existing `.modal`/`.modal-card`
base treatment at `style.css` line ~300) rather than inventing a new visual
language. A simple CSS `opacity`/`transform` transition between slides is
enough - this page has no animation library and shouldn't gain one for this.

---

## Naming / new pieces

| Thing | Name |
|---|---|
| Sheet column | `last_seen_iso` (5th column, `favorites` tab) |
| New Apps Script action | `action: "mark_seen"` (POST only) |
| New Apps Script function | `markSeen_(key, iso)` |
| `favorites.js` new public methods | `KMFavorites.lastSeen()`, `KMFavorites.markSeenNow()` |
| `favorites.js` new private state | `state.lastSeenIso` |
| `app.js` new functions | `computeCatchUp()`, `groupByGame(plays)`, the slideshow controller (naming Opus's choice - e.g. `openCatchUp()`/`advanceCatchUp()`/`closeCatchUp()`) |
| New DOM ids | `#catchup-modal`, `#catchup-card`, `#catchup-close`, `#catchup-progress`, `#catchup-slide`, `#catchup-pause-hint`, entry banner id TBD |
| Reused, untouched | `card(m)` (`app.js:400`), `ensurePlaysLoaded()`'s lazy-fetch pattern (`app.js:672`, generalized not copied), `KMFavorites`'s existing name/endpoint/save machinery, the existing `.modal`/`.modal-card` CSS base |

---

## Validation

1. **Compile/boot**: no Python here; sanity-check `Code.gs` changes by
   pasting into the Apps Script editor's own syntax checker before
   deploying (no local Apps Script test runner available).
2. **Column-preservation regression (the critical one, Stage 1a/1b)**:
   (a) star a favorite, confirm `last_seen_iso` in the sheet is unchanged;
   (b) trigger a `mark_seen` write, confirm `player_ids_json`/`note` in the
   sheet are unchanged; (c) do both in sequence in either order, confirm
   neither clobbers the other. This is the one regression that would be
   easy to ship silently broken (Apps Script has no local test harness, so
   this needs a real deployed-endpoint check, not just code review).
3. **Cursor ordering**: confirm `computeCatchUp()` reads the *old* cursor
   before `markSeenNow()` writes the new one - add a temporary `console.log`
   of both values during development and delete before shipping, or write
   a one-off manual test (set a known `last_seen_iso` in the sheet by hand,
   load the page, confirm the catch-up set matches plays after that exact
   timestamp, confirm the sheet now shows a fresh timestamp).
4. **Multi-session fetch**: manually age a test user's `last_seen_iso` back
   by editing the sheet directly to a timestamp several sessions old,
   confirm `computeCatchUp()` fetches and correctly merges plays from every
   affected session, in the right game/play order.
5. **Empty states**: no name set (routes to the Favorites modal, per
   Stage 2's identity note); name set but genuinely nothing new (banner
   hidden or shows the caught-up message, not an empty/broken slideshow);
   first-ever visit for a name (empty catch-up, cursor still gets set - see
   Open Question 2).
6. **Interrupt behavior**: open, let it auto-advance a few slides, tap to
   pause, confirm the timer actually stops (not just visually), resume,
   confirm it continues from the *remaining* time rather than restarting
   the full dwell; close mid-slideshow, confirm no lingering timers keep
   firing after the modal is hidden (a classic `setTimeout`-not-cleared
   leak - watch for a slide silently advancing after the modal is already
   closed).
7. **Long-run smoke test**: manually construct a scenario with plays spread
   across 3+ sessions (a few hundred plays total) and confirm the
   slideshow is still navigable/closeable throughout - this is the direct
   check on Open Question 1's flagged risk.
8. **Regression**: existing Favorites save/load flow untouched and still
   working (star toggles, name changes, cross-device sync); main feed
   rendering/`card(m)` output unchanged for the normal (non-catch-up) path.

---

## Open questions / risks flagged for Opus (do not silently resolve)

1. **"All plays + no cap" could produce a long slideshow after a real
   absence.** Both were explicit, deliberate choices (decisions 2 and 3),
   not defaults to second-guess - but if validation step 7 turns up a
   30-minute slideshow of mostly routine groundouts after a multi-week
   gap, that's worth surfacing back to Alex rather than silently adding a
   cap or switching to key-moments-only. The tap-to-pause/close controls
   are the intended safety valve for this, not a cap - but "intended" and
   "actually fine in practice" aren't the same thing until it's been tried
   with real data.
2. **First-ever-visit default (Stage 3) is "nothing to catch up on,"
   assumed but not explicitly asked.** The alternative (show the whole
   season on a brand-new name) seems clearly worse UX, but this wasn't one
   of the four questions Alex answered - flag it in the handoff rather than
   treating it as equally settled as decisions 1-4.
3. **Entry-point banner placement (Stage 4d) is a judgment call, not a
   resolved decision** - the plan recommends a banner over a 5th header
   icon given the mobile header's already-tight budget, but exact placement/
   copy/styling is left to implementation judgment rather than specified
   pixel-by-pixel like the header work in prior rounds was.
4. **Dwell timing (1.8s title / 3.5s play) is a starting guess**, not
   tuned against a real slideshow - expect to adjust after actually
   watching one run through, the same way `--games` in the Monte Carlo
   plan was a starting default validated after the fact rather than a
   calculated number.

---
---

# Part 2 - immersive redesign

Part 1 shipped and works. Alex's follow-up: make it actually captivating,
not just a slideshow of the same cards from the main feed - "shoot for the
moon." This part replaces how a **play slide** renders (the title/done
slides, pause/close/progress-bar mechanics, and the whole cursor/data
pipeline from Part 1 are unaffected and stay exactly as shipped).

## Why this is more tractable than it sounds

Every data point this needs is **already in the JSON, already shipped, zero
backend changes required**: `obc_before`, `obc_after`, `runs`, `leverage`,
`win_prob_before`, `win_prob_after`, `batting_is_home`, and
`data.meta.leverage_threshold` (from `key_moments_build.py`'s
`meta["leverage_threshold"]`). This is a `docs/js/app.js` +
`docs/css/style.css` (+ maybe new markup in `docs/index.html`) project, full
stop. That's worth knowing up front because "shoot for the moon" visually
does not have to mean "reopen the data pipeline."

## The new unit: a Play Scene replaces `card(m)` reuse

Today, `catchUpSlideHtml()` returns `card(m)` unmodified for play slides
(`app.js:854`) - the exact same compact card the main feed uses. Replace
that call, **for Catch Me Up only**, with a new `playSceneHtml(slide)` that
composes four pieces around the same underlying data `card(m)` already
uses. `card(m)` itself is untouched - the main feed keeps looking exactly
as it does today.

A Play Scene, top to bottom or however Opus lays it out (there's real
screen real estate here, especially on the now-full-height phone
`.catchup-card`, `style.css:424`):

1. **Animated diamond** (new, big, central) - runners visibly move.
2. **Leverage meter** (new, compact) - how much this play mattered.
3. **Win probability ribbon** (new) - the game's shape, growing as you go.
4. **Play detail strip** (mostly reused) - batter/pitcher, result, score,
   diff pills, tags - essentially what `card(m)`/`wpFragment()`
   (`app.js:342`)/`scoreBlock()` (`app.js:352`) already render, restyled to
   sit alongside the three new pieces rather than being the whole slide.

---

## Component 1 - animated diamond

### 1a. Bigger, dedicated markup

Not a reuse of the small pre-baked per-obc SVG strings in
`meta.bases_svg` (those are static occupancy snapshots with no notion of
"before" vs "after," built for the compact scorebug). A new, larger SVG
(or a handful of absolutely-positioned divs over a diamond background - SVG
is recommended, cleaner coordinate math) with four fixed anchor points
(home, 1B, 2B, 3B) and empty "slots" for runner tokens to occupy.

### 1b. The key insight that makes this possible without new backend data

Runners can never pass each other on the bases - a physical rule of the
game, always true. That means the runners on base **before** a play, listed
most-advanced-to-least (3B, 2B, 1B), and the occupied-or-scored destinations
**after** a play (any runs scored, then 3B, 2B, 1B), can be paired off
strictly in order and the pairing is physically valid for the simple cases
(hits, walks, HRs, sac flies, uncomplicated outs with no force play):

```js
function deriveRunnerMoves(obcBefore, obcAfter, runs) {
  var before = [];
  if (obcBefore[0] === "1") before.push("3B");
  if (obcBefore[1] === "1") before.push("2B");
  if (obcBefore[2] === "1") before.push("1B");

  var after = [];
  for (var i = 0; i < runs; i++) after.push("HOME");
  if (obcAfter[0] === "1") after.push("3B");
  if (obcAfter[1] === "1") after.push("2B");
  if (obcAfter[2] === "1") after.push("1B");

  var moves = [];
  var n = Math.min(before.length, after.length);
  for (var i = 0; i < n; i++) moves.push({ from: before[i], to: after[i], scored: after[i] === "HOME" });
  for (var i = n; i < before.length; i++) moves.push({ from: before[i], to: "OUT", scored: false });
  if (after.length > n) moves.push({ from: "BATTER", to: after[n], scored: after[n] === "HOME" });
  return moves;
}
```

**Known limitation - read before treating this as exact.** This pairing is
provably correct for the common cases above, but it is *not* always
correct for force plays / fielder's choices / double plays, where the
specific runner removed from the bases isn't always "whoever was furthest
back" - e.g. a force out at 2nd with runners on 1st and 3rd removes the 1st-
base runner specifically while the runner on 3rd holds, which this pairing
gets right only because the runner counts happen to line up; more tangled
multi-force scenarios can, in principle, mis-assign which token animates
where. This is a genuine data-derivation ambiguity (`obc_before`/
`obc_after`/`runs` alone don't always uniquely determine *which* runner did
what), not a bug to be coded away.

**Two ways to handle this - pick one, don't silently guess:**
- **(a) Ship the heuristic as-is (recommended for v1).** It's exactly
  correct for the large majority of plays (every hit, walk, HR, and
  straightforward out), and a runner token animation that's occasionally
  cosmetically "plausible but unverified" on a rare tangled force play is
  still a large upgrade over a static card - most viewers will never notice
  or care which specific token represents which specific runner on plays
  like that.
- **(b) Push the exact per-runner assignment into `key_moments_build.py`
  instead**, since the server already computes `obc_after` via
  `utils.advance_runners`/the BRC lookup and could, in principle, emit the
  precise assignment as a new field. More correct, more work, and out of
  scope unless (a) turns out to look wrong often enough in practice to be
  worth the backend round-trip.

### 1c. Animating the tokens

Each token is a small circle/dot positioned via `transform: translate(x, y)`
to one of four fixed coordinate pairs (home/1B/2B/3B, defined once as JS
constants shared with the SVG's own anchor points). On slide mount: paint
tokens at their `from` position first (no transition), then on the next
animation frame add a class/inline style moving them to their `to`
position with `transition: transform 900ms ease` - the standard
"paint-then-move" trick needed so the browser doesn't collapse the initial
and final state into one paint. A `scored: true` move continues the token's
path through/past home before fading + a brief scale-up "flash" (ties into
the score ticker, Component 4b); an `OUT` move fades the token out in place
rather than moving it (no transition on position, just opacity).

### 1d. Batter representation

`{from: "BATTER", ...}` moves start the token invisible at home plate and
fade+slide it to its destination alongside everyone else - visually reads
as "the batter's result," no special-casing needed beyond the origin point.

---

## Component 2 - leverage meter

Simplest of the three new pieces. A compact semicircular arc gauge (SVG
`<path>` arc, or a CSS `conic-gradient` masked into a semicircle - either
works, arc-path is more precise for an exact needle position). Scale: 0 at
one end, clamp the display at some ceiling (4 is a reasonable starting
point given values seen in this app's data rarely exceed that) with an
"redlined" zone beyond `data.meta.leverage_threshold` - **reuse that
existing constant and the existing `leverageClass()` function's hot/cold
logic (`app.js:430`) rather than inventing a second threshold scheme**, so
a play that would show the hot scoreboard-tile treatment also redlines
this meter, consistently. When a play crosses the hot threshold, add a
`.pulse` class driving a CSS `@keyframes` glow/scale pulse (2-3 iterations,
then settle) - the dramatic beat for "this one actually mattered."

---

## Component 3 - win probability ribbon

The most stateful of the three - **pick an implementation weight
deliberately, don't default to the harder one by accident.**

### 3a. Data - already available, zero fetches needed

`data.playsBySession[s]` (populated by `loadAllSessions()`, `app.js:697`)
already holds every play of every session in memory once Catch Up has
opened, filtered or not. For a given game group `g`, the ribbon's data is
`data.playsBySession[g.session_number].filter(p => p.game_code === g.game_code)`,
sorted by `play_num` - the *entire* game's plays, not just the new ones.

Each play's stored `win_prob_before`/`win_prob_after` is from the
**batting team's** perspective, which flips inning to inning - convert to a
single stable perspective (home team, matching how
`utils.compute_game_wp_series`/`game_wp_chart` already do it server-side
for the private Scouting-page chart, `utils.py:441`/`8570`) before
plotting: `homeWp = p.batting_is_home ? p.win_prob_after : 1 - p.win_prob_after`.

### 3b. Two ways to animate it - recommend starting with the simpler one

- **(a) Redraw fresh each play slide, animate only the newest segment
  (recommended to start).** Build the full SVG polyline up through and
  including the current play on every slide mount (a fresh element each
  time, fitting the existing `slideEl.innerHTML = ...` replace-per-slide
  architecture with no structural changes to `showCatchUpSlide`,
  `app.js:878`). Give just the last segment (this play's before-> after
  move) a `stroke-dasharray`/`stroke-dashoffset` draw-in animation plus a
  pulsing marker dot at its endpoint. Reads as "the line just grew" without
  needing the ribbon to persist across slide boundaries.
- **(b) One persistent ribbon element per game, live-growing across that
  game's whole run of play slides.** More impressive (a genuinely
  continuous growing line as you step through a game), but requires
  `showCatchUpSlide` to detect "still the same game as the previous slide"
  and update an existing DOM node's drawn length instead of wiping and
  rebuilding the slide's `innerHTML` - a real structural change, not an
  additive one. Worth doing as a v3 polish pass once (a) is shipped and
  the rest of the visual language is settled, not before.

### 3c. Visual treatment

The pre-cursor ("already known") portion of the line renders immediately,
muted/thin (e.g. `var(--muted)`, no glow). The portion from the first new
play onward (whichever plays are actually being shown this Catch Up run)
renders in full color, thicker, with the current-play marker as a small
glowing dot - the same "known vs. new" contrast that makes the moment feel
earned rather than arbitrary.

---

## Component 4 - smaller polish, cheap relative to the above

### 4a. Play detail strip

Mostly `card(m)`'s existing content (`app.js:401`), restyled to sit as a
strip alongside the diamond/meter/ribbon rather than being the whole slide
- batter/pitcher names, result label, diff pills, `why` tags. Reuse the
rendering logic (extract into a shared inner function `card(m)` already
calls, or just call `card(m)` and CSS-position it within the new layout)
rather than duplicating the HTML string building.

### 4b. Score ticker

When `runs > 0` on the current play, animate the scoring team's number in
`scoreBlock()`'s existing `.score-block` (`app.js:352`) - a quick
increment/roll effect (CSS `@keyframes` on a pseudo-counter, or literally
swap the digit with a brief scale+color flash) timed to land right as the
diamond's scoring token reaches home (Component 1c).

### 4c. Tag-driven flourishes (optional, cheap, skip if time-constrained)

`m.tags` (already rendered as `why-tag` pills by `card(m)`) already flags
`lead_change`, `bases_loaded`, `zero_diff`/`five_hundred_diff`, etc. A
`lead_change` tag could trigger a brief full-slide color-flash in the new
leading team's color (`data.meta.teams[abbr].primary_hex` is already
loaded); a title-slide could pick up the away/home team's `primary_hex` as
a subtle background gradient instead of the current flat card background.
Nice-to-have, not load-bearing - cut first if the schedule is tight.

---

## Orchestration changes needed to existing code

- `catchUpSlideHtml()` (`app.js:832`) needs a new branch calling
  `playSceneHtml(slide)` for `kind === "play"` instead of `card(slide.play)`
  directly - title/done slides unchanged.
- If Component 3's option (b) is ever pursued, `showCatchUpSlide()`
  (`app.js:878`) needs to stop unconditionally doing
  `slideEl.innerHTML = catchUpSlideHtml(slide)` and instead diff "am I
  still inside the same game group as last slide" - explicitly deferred
  per 3b above, called out here so it isn't discovered as a surprise later.
- `PLAY_DWELL_MS` (currently `3500`, `app.js:800`) almost certainly needs
  to increase now that a play slide has three new animated things
  happening on it, not just a card fading in - a play that's still
  mid-animation when the auto-advance timer fires reads as broken, not
  captivating. Revalidate this number against the real animation
  durations (diamond ~900ms, ribbon draw-in, score ticker) once built;
  don't leave it at the old card-only value.

## Accessibility - extend, don't bypass, what's already there

`@media (prefers-reduced-motion: reduce)` is already honored for the
existing slide transition (`style.css:431-435`) - "the slideshow still
advances, it just stops sliding and fading while it does." Every new
animated piece (token movement, ribbon draw-in, meter pulse, score ticker)
needs the same treatment added to that same media block: reduced-motion
should still *show* the end state of each component (runners at their
final base, the full line drawn, the final leverage value) instantly,
never animate it. This is not optional polish - it's the same bar the
feature already cleared once and shouldn't regress on.

## Sound - flagged, not committed to

Not in scope for this plan. A subtle sound effect (bat crack, crowd noise
swell on a high-leverage play) could add real drama, but unsolicited audio
on a webpage is a well-known good way to make people close the tab
immediately - if this gets pursued at all, it must default to **off**,
with an explicit opt-in control, and is a separate, smaller follow-up
conversation, not bundled into this visual redesign.

## Recommended build order (stage it, don't ship it all at once)

1. **Animated diamond (Component 1)** - highest visual impact per unit of
   effort, fully self-contained, needs no other component to feel finished
   on its own.
2. **Leverage meter (Component 2)** - smallest, cheapest, reuses an
   existing threshold/class.
3. **Win probability ribbon, option (a) (Component 3a)** - the most
   stateful piece, ship the simpler variant first.
4. **Polish (Component 4)** - score ticker and tag flourishes, cut first
   under time pressure.
5. **Ribbon option (b)** (persistent, live-growing across a game) - only
   after 1-4 are shipped and the rest of the visual language is settled.

Get sign-off after step 1 before building 2-4 - "shoot for the moon"
is the target, but confirming the diamond actually reads as intended before
investing in the meter and ribbon on top of it is cheaper than discovering
a shared assumption was wrong after all three are built.

## Validation

1. **`deriveRunnerMoves` unit checks** - hand-verify against the worked
   examples in this doc (solo HR from empty bases, grand slam from loaded
   bases, a simple 1B-force fielder's choice showing the known-limitation
   case) plus a handful of real plays pulled from actual session data,
   spanning hits, walks, HRs, and a few out variants.
2. **Timing**: watch a real Catch Up run start-to-finish, confirm no
   component is still mid-animation when `PLAY_DWELL_MS` fires the
   auto-advance (revise the constant per the orchestration note above,
   don't just eyeball it once and move on).
3. **Reduced-motion**: toggle the OS-level reduced-motion setting, confirm
   every new component instantly shows its end state with no animation,
   not just the slide-transition behavior that already existed.
4. **Perspective correctness (Component 3)**: for a handful of real games,
   hand-check that `homeWp` conversion against `utils.compute_game_wp_series`'s
   own output for the same game (already used privately in Scouting,
   `pages/2_Scouting.py:3283`) - the two should agree, since they're
   computing the same thing from the same underlying per-play WP values.
5. **Regression**: main feed's `card(m)` rendering completely unchanged
   (Play Scene is additive, Catch-Me-Up-only); Part 1's pause/close/
   progress-bar/cursor mechanics all still behave exactly as before, since
   none of that is being touched.

---
---

# Part 3 - dynamic dwell speed + Game Replay

Two more follow-ups from Alex. The first is a small, cross-cutting
refinement to Part 1's engine; the second is a new entry point (a
per-game "watch the whole thing" replay from the scoreboard) that turns
out to be cheap specifically *because* Parts 1 and 2 already exist.

## 3a. Dynamic dwell speed - key moments get more air time

Today `catchUpDwell()` (`app.js:857`) returns a flat `PLAY_DWELL_MS`
(3500ms) for every play slide, regardless of what happened. Alex's ask:
routine plays should fly by faster, plays that actually mattered should
linger longer - the visual equivalent of a highlight reel's pacing, not a
metronome.

```js
var PLAY_DWELL_MS_ROUTINE = 2000;
var PLAY_DWELL_MS_KEY     = 4500;

function catchUpDwell(slide) {
  if (slide.kind !== "play") return TITLE_DWELL_MS;
  return slide.play.is_key_moment ? PLAY_DWELL_MS_KEY : PLAY_DWELL_MS_ROUTINE;
}
```

`is_key_moment` is already on every play object (it's the same field the
main feed's Key-Moments-only toggle filters on) - no new data needed, and
this is a straight swap of the existing flat constant, not a structural
change. Both numbers are starting guesses, same as the original
`PLAY_DWELL_MS` was - tune after watching a real run, same validation
discipline as Part 1's original dwell timing note.

**Two tuning refinements worth knowing about, not required for v1:**
- A **graduated** version (dwell scales continuously with `leverage`/
  `abs(wpa)` rather than a binary fast/slow split) would reward a 0.4
  LI play with slightly more time than a 0.1 LI one, not just "key or
  not" - more nuanced, more to tune, not what Alex asked for
  literally ("faster for non-key moments and slower for key moments" is a
  binary framing) - mention only as a future option, don't build it now.
- Once Component 1/2/3's animations (Part 2) exist, the *minimum* dwell
  for a key-moment play needs to be at least as long as those animations
  take to finish (this is the same constraint Part 2's own orchestration
  section already flagged for `PLAY_DWELL_MS` generally - `4500ms` above
  is a reasonable floor to start from once those land, not a number
  chosen independently of that constraint).

**This dwell function should be shared, not re-derived, by Game Replay
below** (3b) - it's small, pure, side-effect-free logic, unlike the
stateful timer/pause machinery that Section 3c deliberately recommends
*not* forcibly merging yet.

## 3b. Game Replay - watch an entire game from a scoreboard tile

A clickable icon on each `.scoreboard-tile` that opens the *same kind* of
slideshow as Catch Me Up, but seeded with **every play of one specific
game**, start to finish, rather than "new plays since last visit across
all games." No favorites/name identity involved at all - unlike Catch Me
Up, this isn't per-user progress-tracked, so there's no cursor to read or
write, and it works for any anonymous visitor.

### Why this is cheap right now

If Part 2 has landed, Game Replay gets the animated diamond/leverage
meter/WP ribbon **for free** - it's the same `playSceneHtml(slide)`
renderer, just fed a different slide list. If Part 2 hasn't landed yet,
Game Replay is still buildable today on top of Part 1 alone (plain
`card(m)` slides), which makes it a genuinely good **validation vehicle**
for Part 2's Win Probability Ribbon component specifically - a full-game
replay has no "already known, muted" segment to worry about (Part 2,
Component 3a-3c's hardest design question), the ribbon just draws from
empty to complete over the course of the replay, which is the *simpler*
of the two cases described there. Consider building the plumbing for Game
Replay (this section) before or alongside Part 2's ribbon work, not
strictly after - whichever order Opus finds easier to sequence.

### A real HTML gotcha - read before wiring the click handler

`.scoreboard-tile` is itself a `<button>` element (`app.js:508`,
`'<button type="button" class="scoreboard-tile...'`), and the whole
scoreboard already has a delegated click handler on `#scoreboard`
(`app.js:1184`) that selects/deselects the game filter when any tile is
clicked. **A `<button>` cannot validly contain another `<button>`** per
the HTML content model - browsers will misparse or auto-close a nested
one. Two ways to add a replay icon without hitting this:

- **(a) Convert `.scoreboard-tile` from `<button>` to `<div role="button"
  tabindex="0">`**, with the existing click-to-select behavior wired to
  both `click` and `keydown` (Enter/Space) on the div. Then a real nested
  `<button class="tile-replay-btn">` for the replay icon is valid HTML,
  cleanest long-term, but touches the existing (working, shipped) tile
  markup and its keyboard-activation behavior - re-test tab/Enter/Space
  selection after this change, not just mouse clicks.
- **(b) Keep `.scoreboard-tile` as a `<button>`**, and make the replay
  icon a non-button clickable element inside it (`<span role="button"
  tabindex="0" class="tile-replay-btn">`) with its own delegated click
  *and* keydown handling, checked in the `#scoreboard` listener **before**
  the existing `closest(".scoreboard-tile")` check, calling
  `e.stopPropagation()` so a click on the icon never also triggers the
  tile's own select-game behavior. Smaller diff, slightly less idiomatic
  (a span standing in for a button, same tradeoff `.chip`/`.header-toggle-btn`
  elsewhere in this codebase don't have to make since none of them are
  nested inside another interactive element).

Recommend (a) - it fixes the actual underlying HTML-validity problem
rather than working around it a second time, and the tile's click/keydown
logic is simple enough that re-verifying it after the div conversion is
low-risk. Opus's call if (b) turns out easier to land quickly.

### Data loading

`scoreboardCard(g)`'s `g` object (`app.js:487`) doesn't carry
`session_number` today - the session context is implicit (whichever
session is currently selected when the scoreboard renders). Either read
`filters.session` at click time (simplest - the scoreboard only ever shows
the active session's games, so this is always correct when the tile is
clicked) or add a `data-session="..."` attribute to the tile markup
alongside the existing `data-game`/`data-away`/`data-home` (more
explicit, survives future refactors better). Once the session is known:

```js
function loadGameReplay(gameCode, session) {
  var fetchPromise = data.playsBySession[session]
    ? Promise.resolve(data.playsBySession[session])
    : getJSON("data/plays_" + pad2(session) + ".json").then(function (rows) {
        data.playsBySession[session] = rows;
        return rows;
      });
  return fetchPromise.then(function (rows) {
    return rows.filter(function (p) { return p.game_code === gameCode; })
      .sort(function (a, b) { return a.play_num - b.play_num; });
  });
}
```

Same lazy-fetch-and-cache shape as `loadAllSessions()`/`ensurePlaysLoaded()`
(`app.js:697`/`672`), scoped to one session instead of all of them -
reuse the pattern, don't reinvent it a third time.

### Slide building and the live-vs-finished game distinction

`buildGameReplaySlides(gameCode, plays)`: one title slide (team
logos/names, final score if `is_game_final` on the last play, or an
"In Progress" treatment if not - see below), then every play in order as
a Play Scene slide (`playSceneHtml`, shared with Catch Me Up once Part 2
lands). No inter-game title cards needed here (it's always exactly one
game), so this is structurally simpler than `buildCatchUpSlides` (`app.js:811`).

A **live** (not-yet-final) game's replay is a snapshot at open time, same
"don't chase a moving target" principle already applied to Catch Me Up's
cursor read - it plays through to whatever the last recorded play is and
stops, it does not poll for new plays mid-replay. The closing slide should
read differently depending on which case it is: a finished game gets a
real recap ("FINAL: Couriers 6, Sharks 4" plus maybe a callout to the
single highest-leverage play of the game, trivially computed by scanning
the already-loaded play list for `max(leverage)`); an in-progress game
gets something like "That's everything so far - the game's still going"
rather than a false sense of finality.

### Engine sharing - don't force it yet

Both Catch Me Up and Game Replay end up wanting the same timer/pause/
close/progress-bar mechanics (`scheduleCatchUp`, `clearCatchUpTimer`,
`setCatchUpPaused`, `showCatchUpSlide`, all currently named for and scoped
to Catch Me Up specifically, `app.js:857-935`). Two options:

- **Duplicate a parallel, Game-Replay-specific copy of that machinery now**
  (own state object, own DOM ids reusing the *CSS classes* where the visual
  treatment is identical but not necessarily the same element ids),
  accepting some near-duplicate code in exchange for not touching
  already-shipped, working Catch Me Up code.
- **Generalize the existing engine into a shared `slideshow` module now**,
  parameterized by slide list + DOM ids + dwell function, used by both
  features from day one - less duplication, but a real refactor of shipped
  code with its own regression surface.

Recommend the first option for the initial Game Replay build - this
codebase's own convention throughout this project has been "don't
abstract until there are two real, proven usages to generalize from," and
after Part 1, there is exactly one usage so far. Once Game Replay is
shipped and both features are live and stable, revisit whether the
duplication is annoying enough to be worth de-duplicating - that's a
low-risk cleanup once both call sites already work, versus a
higher-risk refactor done speculatively before the second usage even
exists.

### Validation

1. **Nested-interactive-element check** (whichever of 3b's two options is
   chosen): click the replay icon, confirm the tile's own select-game
   behavior does *not* also fire; click elsewhere on the tile, confirm
   select-game still works normally; keyboard-only pass (Tab to the tile,
   Tab to the replay icon, Enter/Space on each) confirms nothing regressed.
2. **Live game replay**: open Game Replay on a genuinely in-progress game,
   confirm it plays through to the last recorded play and stops cleanly
   with the "still going" closing slide, does not attempt to fetch or show
   plays that happen *during* the replay itself.
3. **Finished game replay**: confirm the recap slide's highest-leverage
   callout matches a hand-computed `max(leverage)` over that game's plays.
4. **Dwell speed** (3a): confirm key-moment plays visibly linger longer
   than routine ones in both Catch Me Up and Game Replay - same shared
   `catchUpDwell`-equivalent function, so this should be one fix that
   benefits both features simultaneously, not two separate ones to tune.
5. **Regression**: Catch Me Up's existing behavior (banner, cursor,
   pause/close) completely unaffected if Game Replay is built via the
   "duplicate, don't merge yet" recommendation above.

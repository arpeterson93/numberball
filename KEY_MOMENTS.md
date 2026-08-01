# Key Moments - operations

A static, public feed of MLN key moments, built from the MLN Google Sheet and
served from `docs/` on GitHub Pages. Supabase is not in the path at any point.

## Pieces

| Path | What it is |
|------|-----------|
| `key_moments_build.py` | Reads the MLN sheet, replays every game, computes WPA/leverage, writes the JSON |
| `docs/index.html`, `docs/css/`, `docs/js/` | The page. No build tooling - plain HTML/CSS/JS |
| `docs/data/*.json` | Generated feed, committed by the Action |

### Generated files

| File | Contents |
|------|----------|
| `key_moments.json` | Season-wide, plays where `is_key_moment` - loaded on boot |
| `plays_NN.json` | Every play of session NN - fetched lazily, only for Favorites mode |
| `players.json` | Roster for the favorites picker |
| `meta.json` | `built_at`, sessions, thresholds, and the shared lookups (`teams`, `result_labels`, `tag_labels`, `bases_svg`) |

Every play is scored and tagged in one pass; the two-file split is purely about
payload and git churn. Anything from a small closed set - team names, result
labels, tag labels, the eight base-diamond SVGs - lives once in `meta.json`
instead of being repeated on every row, which is what keeps a full-season play
file in the hundreds of kilobytes rather than megabytes. Splitting plays per
session also means a finished session's file stops changing, so the twice-hourly
commit only ever rewrites the session currently being played.
| `.github/workflows/key_moments.yml` | Twice-hourly rebuild plus `workflow_dispatch` |
| `apps-script/Code.gs` + `DEPLOY.md` | Refresh-button proxy and favorites store |

## Running the build locally

```
python key_moments_build.py
```

Run it from the repo root - `utils.py` loads `win_probability_table.csv`,
`result_frequencies.csv`, `state_frequencies.csv`, and `import_BRC.csv` relative
to the working directory. No credentials: the sheet is read through the public
gviz CSV endpoint.

To preview the page:

```
python -m http.server 8765 --directory docs
```

then open <http://localhost:8765>.

## Enabling GitHub Pages (one time)

Repo **Settings -> Pages -> Build and deployment -> Deploy from a branch ->
`main` / `/docs`**. `docs/.nojekyll` is already present so Jekyll stays out of it.

The Action needs `contents: write` to push the refreshed JSON back; that is set
in the workflow, but **Settings -> Actions -> General -> Workflow permissions**
must allow read/write for it to take effect.

## What counts as a key moment

`is_key_moment` is true if **any** rule fires. Each firing rule lands in the
play's `tags` array, shows on the card as a small chip, and gets a filter chip in
the page's tag row. These eight slugs are the complete, closed set - adding a
rule means adding it to `TAG_LABELS` in `key_moments_build.py` too, since the
page builds its tag chips from `meta.tag_labels`.

| Tag | Rule |
|-----|------|
| `zero_diff` / `five_hundred_diff` | `diff` is exactly 0 or exactly 500 |
| `steal` | successful steal (`SB`, `SB2`, `SB3`, `SB4`) - a caught steal is not tagged, but can still qualify as `high_leverage` |
| `strikeout_risp_inning_end` | strikeout that ends the inning with a runner on 2nd or 3rd |
| `run_scoring_hit` | a hit that scores at least one run |
| `bases_loaded` | the play *loads* the bases (`obc_after == "111"`) |
| `lead_change` | `sign(lead_before) != sign(lead_after)` - into or out of a tie counts |
| `high_leverage` | `leverage >= LEVERAGE_THRESHOLD` **or** `abs(wpa) >= WPA_THRESHOLD` |

Thresholds live at the top of `key_moments_build.py`. Current values are
`LEVERAGE_THRESHOLD = 2.0` (roughly the 95th percentile of observed leverage)
and `WPA_THRESHOLD = 0.12` (just above the 95th percentile of `abs(wpa)`).

### On volume

The season-13 data through session 03 gives **214 key moments from 1000 plays**,
about 100 per completed 8-game session. The six non-threshold rules alone account
for 172 of those, so `high_leverage` can only move the total between roughly 172
and 250 - the mockup's "37 per session" is not reachable without dropping or
narrowing a rule (`run_scoring_hit` alone fires 91 times). The page defaults to
one session at a time and has filters plus a WPA sort, which is how that volume
stays browsable. Narrowing `run_scoring_hit` (say, to hits that score the tying
or go-ahead run) is the lever to pull if 100 still feels like too many.

## How WPA and leverage are computed

Per play, from the batting team's perspective:

- `wp_before = utils.get_win_probability_interpolated(remaining, outs_before, obc_before, lead_before)`
- `wp_after` uses the **observed** next state, not a model: the runs are the
  score delta to the next play row (the sheet's `a_Scr`/`h_Scr` are pre-play),
  and `obc_after` is the next row's actual base state. An inning-ending play
  flips to the opponent's half (`1 - wp(remaining - 1, 0, "000", -lead_after)`),
  and a completed game's last play resolves to a literal 1.0 or 0.0 from the
  Games tab's final score.
- `wpa = wp_after - wp_before`
- `leverage = utils.compute_leverage(utils.RESULT_RANGES, ...)` - the league-wide
  result table, since a per-matchup range table is not available at build time.

The replay is checked against the sheet: every completed half-inning ends on
exactly 3 outs, and every game's reconstructed final score matches the Games tab.

Cards headline the player who *did* the thing - the batter on a hitting result,
the pitcher on a pitching result, the runner on a steal, the catcher on a caught
stealing - and show win probability from that player's team's side. `wpa` in the
JSON is always the batting team's; `featured_wpa` is the flipped-if-needed value
the card renders.

## Page behavior

### Two pools

- **Favorites off** (the default): the pool is key moments only, and the footer
  reads "N key moments".
- **Favorites on**: the pool becomes *every* play involving a favorited player
  for the selected session, key moment or not, and the footer reads "N plays".
  Favorites replaces the key-moment restriction rather than narrowing it, so a
  favorited player's whole session is visible. This is the only thing that
  triggers a `plays_NN.json` fetch.

A play counts as involving a favorited player if `batter_id`, `pitcher_id`, or
`featured_id` is starred. `featured_id` is in there so a favorited runner's steal
qualifies - the plan says batter-or-pitcher, but on a steal row the Batter column
holds whoever was at the plate, not the runner who did the stealing.

Every other filter (Result, League, Rookies, Team, Player, tags) ANDs on top of
whichever pool is active.

### Three chip behaviors on one page

| Group | Behavior |
|-------|----------|
| Result (Hitting/Pitching/HR) | Radio, but can be fully off - clicking the active chip clears it back to "all categories" |
| League (MLN/Galactic/Liberty) | Radio, always exactly one active; MLN is the neutral "all" default |
| Rookies, Favorites | Independent booleans, each ANDs with everything else |
| Tags (8 chips) | Multi-select, **OR'd with each other**, then AND'd with everything else |

Tag chips are visually distinct - pill-shaped, navy when active, with a leading
checkmark - so it is not a surprise that they stack instead of excluding each
other the way Result does.

### Small screens

Two breakpoints: `900px` (tablet/narrow laptop) and `600px` (phone).

Below 600px the filters card collapses to a single bar reading
`Filters (N active) ▾`, with the sort chips sitting beside it so sorting stays
reachable without expanding anything. Tapping the bar expands all three filter
rows in place. The collapse is phone-only and cannot strand a desktop user:
a `@media (min-width: 601px)` rule force-shows the panel regardless of the
`collapsed` class JS last left on the card, so widening the window always
reveals it.

Chip groups wrap onto as many lines as they need rather than scrolling
horizontally - a scrollable chip row hides options users have no way to know
are there, which matters most for the eight tag chips.

The scorebug shrinks rather than stacking: below 600px `.moment-right` stays a
single ~140px horizontal strip on its own line beneath `.moment-left`, with the
diamond, outs circles, score block, and inning indicator all one size step down.
The leverage bar leaves the flex flow and becomes an absolutely positioned left
edge, which is what keeps it spanning the full card height once the contents
wrap. That absolute treatment starts at the 900px breakpoint - the same one that
enables wrapping - since a wrapped card would otherwise strand the bar alone on
the first line.

The title row wraps at 600px: heading and session selector on the first line,
last-updated and Refresh on the second.

### Scorebug state

The right side of a card shows the base diamond with two outs circles beneath it,
except that both are replaced by a single badge in two cases, checked in this
order:

1. `is_game_final` -> **FINAL**
2. `is_half_inning_final` -> **END INNING**

`is_game_final` is checked first because the last out of a game is also the last
out of a half-inning. Because `outs_after == 3` always routes to one of the two
badges, the outs circles only ever need to represent 0, 1, or 2 - hence two
circles, not three.

`is_game_final` requires the Games tab's `win_team` to be set *and* the play to
be the game's last, cross-checked against that tab's `last_play`. Neither
`end_time` nor `last_play` can stand in for completion on their own: both keep
updating live while a game is in progress. Against the current data this yields
exactly 16 game-finals for the 16 completed games, two of them walk-offs where
`outs_after < 3`.

## Two refresh paths

1. The `schedule:` trigger at `:17` and `:47`. GitHub delays or drops cron on
   low-activity repos, so this is a backstop, not a guarantee.
2. The page's **Refresh now** button, which calls the Apps Script proxy, which
   fires `workflow_dispatch`. Those start within seconds. The page then polls
   `data/meta.json` for a new `built_at` for up to three minutes.

Scheduled runs commit only when the feed actually changed. Manual runs always
commit, so `built_at` advances and the button's poll can tell it finished.

## Known loose ends

- `1BWH` / `2BWH` / `1BWH2` are single and double variants (confirmed from the
  data - a `1BWH` with runners on 2nd and 3rd scored both). They classify as
  hitting, so their exact naming does not affect any rule. `bAuto` / `pAuto`
  appear in `result_frequencies.csv` but not in season-13 play data.
- The sheet's timestamps carry no timezone in the data itself, but they're
  always entered in Central time - `docs/js/app.js`'s `parseChicagoNaive()`
  reinterprets the naive string as `America/Chicago` (DST-aware) and displays
  it converted to the viewer's own browser-detected zone, with that zone's
  abbreviation appended (e.g. "6:20 PM CDT" for a Central viewer, "4:20 PM
  MDT" for a Mountain one, same instant).
- Sub-league filtering matches on either team in the game, not on the player -
  the sheet stores `GL`/`LL` on teams, not players, and interleague games exist.

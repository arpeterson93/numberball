# Key Moments - operations

A static, public feed of MLN key moments, built from the MLN Google Sheet and
served from `docs/` on GitHub Pages. Supabase is not in the path at any point.

## Pieces

| Path | What it is |
|------|-----------|
| `key_moments_build.py` | Reads the MLN sheet, replays every game, computes WPA/leverage, writes the JSON |
| `docs/index.html`, `docs/css/`, `docs/js/` | The page. No build tooling - plain HTML/CSS/JS |
| `docs/data/*.json` | Generated feed, committed by the Action |
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

A play is included if **any** rule fires. Each firing rule lands in the moment's
`tags` array and shows on the card as a small chip.

| Tag | Rule |
|-----|------|
| `diff_0` / `diff_500` | `diff` is exactly 0 or exactly 500 |
| `steal` | successful steal (`SB`, `SB2`, `SB3`, `SB4`) - a caught steal is not tagged, but can still qualify on leverage or WPA |
| `k_risp_inning_end` | strikeout that ends the inning with a runner on 2nd or 3rd |
| `rbi_hit` | a hit that scores at least one run |
| `bases_loaded` | the play *loads* the bases (`obc_after == "111"`) |
| `lead_change` | `sign(lead_before) != sign(lead_after)` - into or out of a tie counts |
| `high_leverage` | `leverage >= LEVERAGE_THRESHOLD` |
| `big_wpa` | `abs(wpa) >= WPA_THRESHOLD` |

Thresholds live at the top of `key_moments_build.py`. Current values are
`LEVERAGE_THRESHOLD = 2.0` (roughly the 95th percentile of observed leverage)
and `WPA_THRESHOLD = 0.12` (just above the 95th percentile of `abs(wpa)`).

### On volume

The season-13 data through session 03 gives **214 moments from 998 plays**, about
100 per completed 8-game session. The seven non-threshold rules alone account for
172 of those, so the two tunable thresholds can only move the total between
roughly 172 and 250 - the mockup's "37 per session" is not reachable without
dropping or narrowing a rule (`rbi_hit` alone fires 91 times). The page defaults
to one session at a time and has filters plus a WPA sort, which is how that
volume stays browsable. Narrowing `rbi_hit` (say, to hits that score the tying or
go-ahead run) is the lever to pull if 100 still feels like too many.

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
- The sheet's timestamps carry no timezone, so the page renders them as-is with
  no zone label rather than inventing one.
- Sub-league filtering matches on either team in the game, not on the player -
  the sheet stores `GL`/`LL` on teams, not players, and interleague games exist.

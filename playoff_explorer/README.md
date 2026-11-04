# MLN Playoff Odds Explorer

Historical playoff odds by record, games back and seed, plus league parity and season paths. Published as a standalone artifact.

1. `python playoff_explorer/build_data.py` reads the archive and live MLN sheets and writes `dist/team_sessions.json` and
   `dist/team_seasons.json`. Standings, games back and seeds use `draft_order`'s rulebook tiebreakers, so the page agrees with the bot.
2. `python playoff_explorer/build_standalone.py` embeds that data in `page_fragment.html` and writes `dist/standalone.html`.
3. Publish `dist/standalone.html` as an Artifact (same file path keeps the same link).

Inputs that rarely change: `score_fix.json` (final scores of S1-S10 games whose Games-tab score is stale, from `fetch_score_fix.py`),
`base.json` (coin-flip parity baseline, from `chance_baseline.py`). Team colors (`TCOL`) are inlined in the page, from
`docs/data/meta.json` and `key_moments_build.SECONDARY_HEX`.

-- Add Double Down % / Triple Down % columns to pitcher_stats. Idempotent - safe
-- to re-run. After running, click "Refresh Pitcher Stats" on the Games page.

ALTER TABLE pitcher_stats
  ADD COLUMN IF NOT EXISTS dd_pct NUMERIC,
  ADD COLUMN IF NOT EXISTS td_pct NUMERIC;

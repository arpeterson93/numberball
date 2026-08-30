-- Add Shadow |Δ| column to pitcher_stats. Idempotent - safe to re-run.
-- After running, click "Refresh Pitcher Stats" on the Games page.

ALTER TABLE pitcher_stats
  ADD COLUMN IF NOT EXISTS avg_shadow_delta NUMERIC;

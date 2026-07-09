-- Tie pitcher_stats to the human (player_id), not the name. Idempotent - safe to
-- re-run. After running, click "Refresh Pitcher Stats" on the Sync Data page.

-- 1. Add the player_id column (reference/grouping value).
ALTER TABLE pitcher_stats
  ADD COLUMN IF NOT EXISTS player_id INT;

-- 2. Drop the UNIQUE(pitcher_name) constraint. Two different humans can share a
--    current name, so the name can't be unique; a pitcher is one row keyed by
--    identity (player_id), regenerated via a full clear + insert on Refresh.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
    WHERE con.conrelid = 'public.pitcher_stats'::regclass
      AND con.contype = 'u'
      AND array_length(con.conkey, 1) = 1
      AND att.attname = 'pitcher_name'
  LOOP
    EXECUTE format('ALTER TABLE pitcher_stats DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;

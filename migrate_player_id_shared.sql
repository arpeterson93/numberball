-- Path A: make player_id a shared, non-unique human id across leagues/seasons.
--
-- Background: player_id is the same number for a human in RLN and MLN and across
-- MLN seasons. Previously player_id was UNIQUE and RLN upserted on it, so MLN
-- rows had to leave player_id NULL. This drops that uniqueness and re-keys RLN
-- rows on s_id ('R_<player_id>'), so player_id can repeat freely and become the
-- grouping key that ties a human's history together through name changes.
--
-- RUN THIS BEFORE deploying the matching code, then re-sync players (RLN +
-- MLN current + MLN archive) so player_id gets populated on existing rows.

-- 1. Drop any single-column UNIQUE constraint on players.player_id (name varies).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
    WHERE con.conrelid = 'public.players'::regclass
      AND con.contype = 'u'
      AND array_length(con.conkey, 1) = 1
      AND att.attname = 'player_id'
  LOOP
    EXECUTE format('ALTER TABLE players DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;

-- ...and any standalone UNIQUE INDEX on player_id (if it was an index, not a constraint).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT i.relname
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = ANY (x.indkey)
    WHERE t.relname = 'players'
      AND x.indisunique AND NOT x.indisprimary
      AND array_length(x.indkey::int2[], 1) = 1
      AND att.attname = 'player_id'
  LOOP
    EXECUTE format('DROP INDEX IF EXISTS %I', r.relname);
  END LOOP;
END $$;

-- 2. Give existing RLN rows the stable s_id the new upsert keys on, so future
--    RLN syncs update these rows instead of inserting duplicates. RLN uses
--    'R_<player_id>' (one row per human); MLN uses '<season>_<id>' - no overlap.
UPDATE players
SET    s_id = 'R_' || player_id
WHERE  league = 'RLN'
  AND  player_id IS NOT NULL
  AND  (s_id IS NULL OR s_id = '');

-- 3. Ensure s_id is UNIQUE (it is the row key for both leagues now). Should
--    already exist from the multi-league migration; add if missing.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.players'::regclass AND contype = 'u' AND conname = 'players_s_id_key'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_attribute att ON att.attrelid = t.oid AND att.attnum = ANY (x.indkey)
    WHERE t.relname = 'players' AND x.indisunique AND att.attname = 's_id'
  ) THEN
    ALTER TABLE players ADD CONSTRAINT players_s_id_key UNIQUE (s_id);
  END IF;
END $$;

-- 4. Sanity checks (run after re-syncing players):
--    a) no RLN row missing its s_id
--       SELECT count(*) FROM players WHERE league='RLN' AND (s_id IS NULL OR s_id='');
--    b) player_id now repeats across a human's rows (RLN + MLN seasons)
--       SELECT player_id, count(*) FROM players WHERE player_id IS NOT NULL
--       GROUP BY player_id HAVING count(*) > 1 ORDER BY count(*) DESC LIMIT 20;

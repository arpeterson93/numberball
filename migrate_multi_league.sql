-- Multi-league schema migration.
-- Adds league tracking and MLN-specific columns to all four tables.
-- Safe to run on existing data — all new columns have defaults or are nullable.
-- Run in Supabase SQL editor.

-- ── games ─────────────────────────────────────────────────────────────────────

ALTER TABLE games
  ADD COLUMN IF NOT EXISTS league   TEXT NOT NULL DEFAULT 'RLN',
  ADD COLUMN IF NOT EXISTS link     TEXT;  -- game thread URL (MLN "Link" column)

-- ── plays ─────────────────────────────────────────────────────────────────────
-- MLN play numbers are 8-digit composites (game_code + seq); RLN are small ints.
-- Collision is unlikely but widen the unique key to (play_num, league) to be safe.

ALTER TABLE plays
  ADD COLUMN IF NOT EXISTS league       TEXT NOT NULL DEFAULT 'RLN',
  ADD COLUMN IF NOT EXISTS season       INT,           -- stored directly for MLN (RLN derives via games join)
  ADD COLUMN IF NOT EXISTS season_type  TEXT;          -- "Regular" vs "Postseason"

ALTER TABLE plays DROP CONSTRAINT IF EXISTS plays_play_num_key;
ALTER TABLE plays ADD  CONSTRAINT plays_play_num_league_key UNIQUE (play_num, league);

-- ── teams ─────────────────────────────────────────────────────────────────────
-- RLN upserts continue keying on abbrev.
-- MLN archive upserts key on s_team ("1_T1001") — added as a separate UNIQUE column.

ALTER TABLE teams
  ADD COLUMN IF NOT EXISTS league        TEXT NOT NULL DEFAULT 'RLN',
  ADD COLUMN IF NOT EXISTS season        INT,
  ADD COLUMN IF NOT EXISTS sub_league    TEXT,   -- MLN sub-league (GL, LL, etc.)
  ADD COLUMN IF NOT EXISTS division      TEXT,
  ADD COLUMN IF NOT EXISTS team_id       TEXT,   -- e.g. "T1001"
  ADD COLUMN IF NOT EXISTS location      TEXT,
  ADD COLUMN IF NOT EXISTS team_name     TEXT,   -- e.g. "Peregrines"
  ADD COLUMN IF NOT EXISTS full_team     TEXT,   -- e.g. "Acadia Peregrines"
  ADD COLUMN IF NOT EXISTS primary_hex   TEXT,
  ADD COLUMN IF NOT EXISTS wins          INT,
  ADD COLUMN IF NOT EXISTS losses        INT,
  ADD COLUMN IF NOT EXISTS runs_scored   INT,
  ADD COLUMN IF NOT EXISTS runs_allowed  INT,
  ADD COLUMN IF NOT EXISTS s_team        TEXT UNIQUE;  -- archive upsert key

-- ── players ───────────────────────────────────────────────────────────────────
-- RLN upserts continue keying on player_id.
-- MLN archive upserts key on s_id ("1_1001") — added as a separate UNIQUE column.

ALTER TABLE players
  ADD COLUMN IF NOT EXISTS league             TEXT NOT NULL DEFAULT 'RLN',
  ADD COLUMN IF NOT EXISTS season             INT,
  ADD COLUMN IF NOT EXISTS suffix             TEXT,   -- Jr, Sr, II, etc.
  ADD COLUMN IF NOT EXISTS first_name         TEXT,
  ADD COLUMN IF NOT EXISTS last_name          TEXT,
  ADD COLUMN IF NOT EXISTS discord_id         TEXT,
  ADD COLUMN IF NOT EXISTS discord_nickname   TEXT,
  ADD COLUMN IF NOT EXISTS session_added      TEXT,   -- session when player was created
  ADD COLUMN IF NOT EXISTS rookie             BOOLEAN,
  ADD COLUMN IF NOT EXISTS s_id               TEXT UNIQUE;  -- archive upsert key

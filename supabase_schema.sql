-- Numberball Supabase schema
-- Run this in the Supabase SQL editor for the numberball project

-- Sessions: one row per game (series of at-bats between two teams)
CREATE TABLE sessions (
    id          BIGSERIAL PRIMARY KEY,
    season      INTEGER NOT NULL,
    session_number INTEGER NOT NULL,
    home_team   TEXT NOT NULL,
    away_team   TEXT NOT NULL,
    game_date   DATE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (season, session_number)
);

-- At-bats: one row per pitch/swing exchange
CREATE TABLE at_bats (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    inning          INTEGER NOT NULL CHECK (inning >= 1),
    outs            INTEGER NOT NULL CHECK (outs BETWEEN 0 AND 2),
    obc             TEXT NOT NULL DEFAULT 'Empty',
    pitcher_team    TEXT NOT NULL,
    batter_team     TEXT NOT NULL,
    pitcher_name    TEXT NOT NULL,
    batter_name     TEXT NOT NULL,
    pitch           INTEGER NOT NULL CHECK (pitch BETWEEN 1 AND 1000),
    swing           INTEGER NOT NULL CHECK (swing BETWEEN 1 AND 1000),
    result          TEXT NOT NULL,
    is_fp_app       BOOLEAN NOT NULL DEFAULT FALSE,
    is_fp_inn       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Allow public read/write (adjust with auth later if needed)
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE at_bats  ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public_all" ON sessions FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "public_all" ON at_bats  FOR ALL USING (true) WITH CHECK (true);

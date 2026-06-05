-- Pre-computed pitcher behavioral stats table.
-- Run once in the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS pitcher_stats (
    id              BIGSERIAL    PRIMARY KEY,
    pitcher_name    TEXT         NOT NULL UNIQUE,
    ab_count        INT          NOT NULL DEFAULT 0,
    avg_abs_delta   FLOAT,
    avg_delta2      FLOAT,
    shadow_pct      FLOAT,
    meme_rate       FLOAT,
    wraparound_pct  FLOAT,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE pitcher_stats ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read"  ON pitcher_stats FOR SELECT USING (true);
CREATE POLICY "public write" ON pitcher_stats FOR ALL    USING (true);

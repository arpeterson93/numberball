-- Pre-computed rolling-average percentile tables, one row per behavioral
-- metric. Lets a pitcher's RECENT (20-pitch rolling average) value be graded
-- against the distribution of everyone's rolling averages instead of the
-- career-average distribution in pitcher_stats - a rolling stat carries far
-- more sampling variance than a career average, so comparing it to career
-- values overstates how extreme a hot or cold stretch looks.
-- Run once in the Supabase SQL editor, then click "Refresh Pitcher Stats" on
-- the Games page to populate it.

CREATE TABLE IF NOT EXISTS pitcher_ma_percentiles (
    metric       TEXT         PRIMARY KEY,
    percentiles  JSONB        NOT NULL,
    n_samples    INT          NOT NULL DEFAULT 0,
    window_size  INT          NOT NULL DEFAULT 20,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE pitcher_ma_percentiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read"  ON pitcher_ma_percentiles FOR SELECT USING (true);
CREATE POLICY "public write" ON pitcher_ma_percentiles FOR ALL    USING (true);

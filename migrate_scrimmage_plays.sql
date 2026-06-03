-- Creates the scrimmage_plays table.
-- Schema mirrors plays but with no FK to games; scrimmage_code is the game identifier.

CREATE TABLE scrimmage_plays (
  id              BIGSERIAL PRIMARY KEY,

  -- Game identification (no FK — scrimmages aren't in the games table)
  scrimmage_code  TEXT,
  season          INT,
  play_num        BIGINT UNIQUE NOT NULL,

  -- From scrimmage sheet (same format as Plays (Raw))
  timestamp       TEXT,
  umpire          TEXT,
  away            TEXT,
  home            TEXT,
  inning_raw      TEXT,
  away_score      INT,
  home_score      INT,
  play_type       TEXT,
  result          TEXT,
  play_code       TEXT,
  pitcher_id      INT,
  catcher_id      INT,
  pos             TEXT,
  batter_id       INT,
  on_first        TEXT,
  on_second       TEXT,
  on_third        TEXT,
  scored2         TEXT,
  scored3         TEXT,
  scored4         TEXT,
  er1             TEXT,
  er2             TEXT,
  er3             TEXT,
  er4             TEXT,
  pitch           INT,
  swing           INT,
  throw_num       INT,
  runner_id       INT,
  steal_num       INT,

  -- Enriched during sync
  pitcher_name    TEXT,
  batter_name     TEXT,
  catcher_name    TEXT,
  runner_name     TEXT,
  off_team        TEXT,
  def_team        TEXT,
  inning          INT,
  half            TEXT,
  obc             TEXT,
  outs            INT,
  diff            INT,
  is_fp_inn       BOOLEAN,
  is_fp_app       BOOLEAN
);

CREATE INDEX ON scrimmage_plays(scrimmage_code);
CREATE INDEX ON scrimmage_plays(pitcher_name);
CREATE INDEX ON scrimmage_plays(batter_name);

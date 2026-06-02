-- Drops and recreates the plays table to match the Plays (Raw) sheet schema.
-- App-computed columns (outs, diff, obc, half, inning, off_team, def_team,
-- pitcher_name, batter_name, catcher_name, runner_name, is_fp_inn, is_fp_app)
-- are populated by the Python sync, not read directly from the sheet.

DROP TABLE IF EXISTS plays;

CREATE TABLE plays (
  id            BIGSERIAL PRIMARY KEY,
  game_id       BIGINT REFERENCES games(id) ON DELETE CASCADE,

  -- From Plays (Raw) sheet
  play_num      BIGINT  UNIQUE NOT NULL,
  game_code     TEXT,
  timestamp     TEXT,
  umpire        TEXT,
  away          TEXT,
  home          TEXT,
  inning_raw    TEXT,
  away_score    INT,
  home_score    INT,
  play_type     TEXT,
  result        TEXT,
  play_code     TEXT,
  pitcher_id    INT,
  catcher_id    INT,
  pos           TEXT,
  batter_id     INT,
  on_first      TEXT,
  on_second     TEXT,
  on_third      TEXT,
  scored2       TEXT,
  scored3       TEXT,
  scored4       TEXT,
  er1           TEXT,
  er2           TEXT,
  er3           TEXT,
  er4           TEXT,
  pitch         INT,
  swing         INT,
  throw_num     INT,
  runner_id     INT,
  steal_num     INT,

  -- Enriched during sync
  pitcher_name  TEXT,
  batter_name   TEXT,
  catcher_name  TEXT,
  runner_name   TEXT,
  off_team      TEXT,
  def_team      TEXT,
  inning        INT,
  half          TEXT,
  obc           TEXT,
  outs          INT,
  diff          INT,
  is_fp_inn     BOOLEAN,
  is_fp_app     BOOLEAN
);

CREATE INDEX ON plays(game_id);
CREATE INDEX ON plays(pitcher_name);
CREATE INDEX ON plays(batter_name);

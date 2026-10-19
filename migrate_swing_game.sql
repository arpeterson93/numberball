-- Swing guessing game (PioBall rebuild). One swing_pitches row per upcoming
-- pitch in a game (keyed on the last play_num seen when it was opened), one
-- swing_guesses row per Discord user per pitch. The hitting ranges for the
-- PA are snapshotted onto swing_pitches when the pitch is opened, since the
-- live sheet moves on the moment the real pitch lands.

CREATE TABLE IF NOT EXISTS swing_pitches (
  id             BIGSERIAL PRIMARY KEY,
  game_code      TEXT   NOT NULL,
  season         INT,
  seq            INT    NOT NULL,             -- pitch order within the game (1, 2, 3...)
  after_play_num BIGINT NOT NULL DEFAULT 0,   -- plays.play_num floor: real pitch is after this
  batter         TEXT,
  pitcher        TEXT,
  inning         INT,
  half           TEXT,
  outs           INT,
  obc            TEXT,
  swing_type     TEXT,
  ranges         JSONB,                       -- [[result, lo, hi], ...]
  channel_id     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at      TIMESTAMPTZ,                 -- guessing locked (pitch seen in thread)
  graded_at      TIMESTAMPTZ,
  pitch          INT,
  swing          INT,                         -- the manager's actual swing
  play_num       BIGINT,
  actual_result  TEXT,
  UNIQUE (game_code, seq)
);

CREATE TABLE IF NOT EXISTS swing_guesses (
  id         BIGSERIAL PRIMARY KEY,
  pitch_id   BIGINT NOT NULL REFERENCES swing_pitches(id) ON DELETE CASCADE,
  user_id    TEXT   NOT NULL,
  user_name  TEXT,
  guess      INT    NOT NULL CHECK (guess BETWEEN 1 AND 1000),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  diff       INT,
  result     TEXT,
  UNIQUE (pitch_id, user_id)
);

CREATE INDEX IF NOT EXISTS swing_pitches_open_idx ON swing_pitches(graded_at) WHERE graded_at IS NULL;
CREATE INDEX IF NOT EXISTS swing_guesses_user_idx ON swing_guesses(user_id);

-- RE24 / WPA scoring: the Pioneers' lead and the league at the snapshot (safe to re-run).
ALTER TABLE swing_pitches ADD COLUMN IF NOT EXISTS lead   INT;
ALTER TABLE swing_pitches ADD COLUMN IF NOT EXISTS league TEXT;

-- Model swing: the combined pitch-likelihood model's recommendation, frozen when first computed from synced data.
ALTER TABLE swing_pitches ADD COLUMN IF NOT EXISTS model_swing   INT;
ALTER TABLE swing_pitches ADD COLUMN IF NOT EXISTS model_metric  TEXT;
ALTER TABLE swing_pitches ADD COLUMN IF NOT EXISTS model_at      TIMESTAMPTZ;

CREATE TABLE players (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    team    TEXT NOT NULL,
    UNIQUE (name, team)
);

ALTER TABLE players ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public_all" ON players FOR ALL USING (true) WITH CHECK (true);

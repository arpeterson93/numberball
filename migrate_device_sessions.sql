-- Non-rotating per-device session tokens (see auth.py / Fable 5 auth change).
-- Only the sha256 hash of each token is stored, so a DB leak cannot leak sessions.
create table if not exists device_sessions (
  token_hash  text primary key,
  user_id     uuid not null,
  user_email  text not null,
  created_at  timestamptz default now(),
  last_seen   timestamptz default now()
);

create index if not exists device_sessions_user_id_idx on device_sessions (user_id);

-- RLS enabled with a permissive policy, matching every other table in this
-- project (players, pitcher_stats, sessions, at_bats). The app connects with the
-- anon key from server-side Streamlit secrets - it is never exposed to browsers,
-- and only token hashes are stored, so USING (true) is safe here. Because the
-- Supabase session is discarded, there is no auth.uid() to filter on, so a
-- stricter policy is not possible with this architecture.
alter table device_sessions enable row level security;
create policy "public_all" on device_sessions for all using (true) with check (true);

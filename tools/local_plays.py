"""Local copy of the Supabase plays + players tables, so analysis tools stop re-downloading them.

Every run of a tool that calls scouting_data.load_pitcher_plays (the pitch-model fits, backtests and screens) used to pull
each pitcher's history from Supabase again, and every download counts against the free plan's egress. This keeps one raw
copy on disk (tools/probe_output/plays_raw.pkl, gitignored) shaped exactly like the live queries, and database.py answers
the get_plays_for_* / get_all_plays / get_all_players calls from it when NB_LOCAL_PLAYS points at the file. The app and
the bots never set that variable, so they always read live.

Tool entry points call enable() (already wired into the pitch-model tools). It prints the copy's age; refresh it when you
need newer plays:

  python -I tools/local_plays.py fetch      # one full download (plays with the games join + players), then every tool is offline
  python -I tools/local_plays.py status     # file size / age / row counts

Run a tool against live data instead with NB_LOCAL_PLAYS= (empty) in the environment.
"""
from __future__ import annotations

import os
import pathlib
import pickle
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH = ROOT / "tools" / "probe_output" / "plays_raw.pkl"


def _age_days() -> float:
    return (time.time() - PATH.stat().st_mtime) / 86400


def enable() -> bool:
    """Point database.py at the local copy for this process (and any workers it spawns). False = going live."""
    if "NB_LOCAL_PLAYS" in os.environ:                 # explicit choice wins; an empty value means "live"
        return bool(os.environ["NB_LOCAL_PLAYS"])
    if not PATH.exists():
        print(f"[local plays] {PATH.name} not found - reading LIVE from Supabase (counts toward egress). "
              f"Create it once with: python -I tools/local_plays.py fetch", file=sys.stderr)
        return False
    os.environ["NB_LOCAL_PLAYS"] = str(PATH)
    print(f"[local plays] using {PATH.name} ({_age_days():.1f} days old); refresh with `local_plays.py fetch`", file=sys.stderr)
    return True


def fetch() -> dict:
    """Download plays (with the games join, like get_all_plays) and players once and save them."""
    os.environ.pop("NB_LOCAL_PLAYS", None)
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("SB_CLIENT_TAG", "script-local_plays-fetch")
    import database as db
    data = {"fetched_at": time.time(), "plays": db.get_all_plays(), "players": db.get_all_players()}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PATH, "wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"saved {len(data['plays'])} plays and {len(data['players'])} players to {PATH} "
          f"({PATH.stat().st_size / 1e6:.1f} MB)")
    return data


def plays() -> list[dict]:
    """The raw plays rows from the local copy, downloading it first if there isn't one."""
    if not PATH.exists():
        return fetch()["plays"]
    with open(PATH, "rb") as fh:
        return pickle.load(fh)["plays"]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "fetch":
        fetch()
    elif PATH.exists():
        with open(PATH, "rb") as fh:
            d = pickle.load(fh)
        print(f"{PATH} - {PATH.stat().st_size / 1e6:.1f} MB, {_age_days():.1f} days old, "
              f"{len(d['plays'])} plays, {len(d['players'])} players")
    else:
        print(f"{PATH} does not exist - run: python -I tools/local_plays.py fetch")

"""Step 1 of 4 for the Immaculate Innings artifact (see build_artifact.py).
Pull the entire plays table (minimal columns) to identify half-innings
where literally nothing but 3 strikeouts (by one pitcher) happened."""
import json
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
import database

client = database._client()

COLS = ("id, game_id, game_code, league, season, game_type, inning, half, "
        "play_num, pitcher_name, pitcher_id, result, diff, def_team, off_team")

def fetch_all():
    results = []
    offset = 0
    chunk = 1000
    while True:
        batch = (
            client.table("plays")
            .select(COLS)
            .order("id", desc=False)
            .range(offset, offset + chunk - 1)
            .execute()
            .data
        )
        results.extend(batch)
        if len(batch) < chunk:
            break
        offset += chunk
        print(f"  fetched {len(results)}...")
    return results

rows = fetch_all()
print(f"Total plays fetched: {len(rows)}")
with open(DATA / "all_plays_raw.json", "w") as f:
    json.dump(rows, f)
print("saved all_plays_raw.json")

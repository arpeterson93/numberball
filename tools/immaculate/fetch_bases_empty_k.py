"""Step 2 of 4 for the Immaculate Innings artifact (see build_artifact.py).
Every genuine bases-empty strikeout diff, season 5+, for the 3-pitch-equivalent section."""
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

COLS = "id, game_id, game_code, season, inning, half, play_num, result, diff, obc, game_type, league"

def fetch_all():
    results = []
    offset = 0
    chunk = 1000
    while True:
        batch = (
            client.table("plays")
            .select(COLS)
            .eq("league", "MLN")
            .eq("result", "K")
            .gte("season", 5)
            .order("id", desc=False)
            .range(offset, offset + chunk - 1)
            .execute()
            .data
        )
        results.extend(batch)
        if len(batch) < chunk:
            break
        offset += chunk
    return results

rows = fetch_all()
print("total K rows (season>=5, MLN):", len(rows))

rows = [r for r in rows if r.get("game_type") != "scrimmage"]
print("after excluding scrimmage:", len(rows))

bases_empty = [r for r in rows if r.get("obc") == "000" and r.get("diff") is not None]
print("bases-empty genuine K rows:", len(bases_empty))

with open(DATA / "bases_empty_k.json", "w") as f:
    json.dump(bases_empty, f)
print("saved bases_empty_k.json")

diffs = [r["diff"] for r in bases_empty]
print("min/max diff:", min(diffs), max(diffs))

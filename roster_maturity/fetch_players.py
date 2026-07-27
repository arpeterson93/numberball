"""Pull the players table (all leagues) from Supabase into data/players_rows.csv.
Both leagues are kept (not just MLN) since a player's most recent name update can
come from an RLN row.

Usage:
    python fetch_players.py
"""
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db

COLS = "player_id,season,league,name,team,status"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "players_rows.csv")


def main():
    client = db._client()
    rows = db._fetch_all(client.table("players").select(COLS))
    print(f"fetched {len(rows)} player rows (all leagues)")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLS.split(","))
        writer.writeheader()
        writer.writerows(rows)
    print("wrote", OUT_PATH)


if __name__ == "__main__":
    main()

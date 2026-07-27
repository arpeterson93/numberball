"""Pull the MLN plays table from Supabase into data/mln_plays_full.csv.

Usage:
    python fetch_plays.py
"""
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db

COLS = "id,pitcher_id,batter_id,off_team,def_team,league,season,game_type,play_type"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mln_plays_full.csv")


def main():
    client = db._client()
    rows = db._fetch_all(client.table("plays").select(COLS).eq("league", "MLN"))
    print(f"fetched {len(rows)} MLN plays rows")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLS.split(","))
        writer.writeheader()
        writer.writerows(rows)
    print("wrote", OUT_PATH)


if __name__ == "__main__":
    main()

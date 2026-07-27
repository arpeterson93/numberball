"""Pull the MLN teams table (win-loss records, per season) from Supabase into
data/mln_teams_wl.csv.

Usage:
    python fetch_teams.py
"""
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db

COLS = "abbrev,name,full_team,wins,losses,season,league"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mln_teams_wl.csv")


def main():
    client = db._client()
    rows = db._fetch_all(client.table("teams").select(COLS).eq("league", "MLN"))
    print(f"fetched {len(rows)} MLN team-season rows")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLS.split(","))
        writer.writeheader()
        writer.writerows(rows)
    print("wrote", OUT_PATH)


if __name__ == "__main__":
    main()

"""
Same integrity audit as plays_data_audit.py, run against the actual Google
Sheets source instead of Supabase - the same two sheets key_moments_build.py
reads to build the MLN Gameday JSON data:
  - MLN_SHEET_ID, tab "Plays (Raw)": current season (season 13), live.
  - ARCHIVE_SHEET_ID, tab "Plays" (gid=ARCHIVE_PLAYS_GID): all historical
    seasons 1-12 in one tab, filtered by its own Season column.

Then diffs the two audits' row-level findings (joined on play_num) to tell
apart three cases per issue:
  - in both        -> a real error already in the source sheet (fix there).
  - Supabase only   -> introduced by the Supabase sync/import path.
  - sheet only      -> either the sheet was edited after the last sync, or
                        Supabase's own repair logic (see plays_data_audit's
                        reconcile_obc) already resolved it on the way in.

Read-only (no writes to the sheet or Supabase). Reuses utils.py's own sheet
reader (utils.read_mln_plays_from_sheet) so this sees exactly the same rows
- including the same cell/Playcode obc repair - key_moments_build.py does,
rather than re-parsing the CSV independently.

Run from the repo root (network access to Google Sheets required):
    python tools/plays_sheet_audit.py [--out tools/probe_output/sheet_audit]
      [--supabase-audit tools/probe_output/audit]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import utils  # noqa: E402
import plays_data_audit as pda  # noqa: E402

MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"
ARCHIVE_SHEET_ID = "1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg"
ARCHIVE_PLAYS_GID = "1141271229"


def fetch_sheet_plays() -> list[dict]:
    print("Fetching current-season sheet (Plays (Raw))...")
    current = utils.read_mln_plays_from_sheet(MLN_SHEET_ID, tab="Plays (Raw)")
    print(f"  {len(current)} rows")
    print("Fetching historical archive sheet (Plays, all 12 seasons)...")
    archive = utils.read_mln_plays_from_sheet(ARCHIVE_SHEET_ID, tab="Plays", gid=ARCHIVE_PLAYS_GID)
    print(f"  {len(archive)} rows")
    combined = current + archive
    for p in combined:
        if p.get("season") is None and p.get("play_num"):
            p["season"] = pda.decompose_play_num(p["play_num"])[0]
    return combined


def run_checks(plays: list[dict], brc: dict) -> dict[str, list[dict]]:
    plays_by_game = defaultdict(list)
    for p in plays:
        plays_by_game[p["game_code"]].append(p)

    by_league_season = defaultdict(list)
    for p in plays:
        by_league_season[(p["league"], p["season"])].append(p)
    post_play_seasons = {ls for ls, rows in by_league_season.items() if pda.detect_score_convention(rows)}
    print("\nSheet score convention by (league, season) - post-play marked *:")
    for ls in sorted(post_play_seasons | set(by_league_season)):
        print(f"  {ls}: {'post-play *' if ls in post_play_seasons else 'pre-play'}")

    results: dict[str, list[dict]] = {}
    results["play_num_gaps"] = pda.check_play_num_gaps(plays_by_game)
    gaps, season_mismatches = pda.check_game_seq_gaps(plays)
    results["game_seq_gaps"] = gaps
    results["play_num_season_mismatches"] = season_mismatches
    results["brc_situation_missing"] = pda.check_brc_missing(plays, brc)

    obc_conflicts, outs_mismatches, result_mismatches = pda.check_row_internal(plays)
    results["obc_unresolved_conflicts"] = obc_conflicts
    results["outs_vs_playcode_mismatches"] = outs_mismatches
    results["result_vs_playcode_mismatches"] = result_mismatches

    baserunner_flow, inning_half_errors, score_flow, _ = pda.check_game_flow(
        plays_by_game, brc, {}, post_play_seasons
    )
    results["baserunner_flow_mismatches"] = baserunner_flow
    results["inning_half_sequence_errors"] = inning_half_errors
    results["score_flow_mismatches"] = score_flow
    return results


def compare(sheet_results: dict[str, list[dict]], supabase_out: pathlib.Path) -> None:
    import csv as _csv

    print("\n=== Supabase vs. sheet comparison (row-level categories, joined on play_num) ===")
    for name, sheet_rows in sheet_results.items():
        sb_path = supabase_out / f"{name}.csv"
        if not sb_path.exists():
            continue
        with open(sb_path, newline="", encoding="utf-8") as f:
            sb_rows = list(_csv.DictReader(f))
        key = "play_num" if any("play_num" in r for r in sheet_rows) else None
        if key is None:
            continue
        sb_keys = {r[key] for r in sb_rows if key in r}
        sheet_keys = {str(r[key]) for r in sheet_rows if key in r}
        sb_keys = {str(k) for k in sb_keys}
        both = sb_keys & sheet_keys
        sb_only = sb_keys - sheet_keys
        sheet_only = sheet_keys - sb_keys
        print(f"  {name}: supabase={len(sb_keys)} sheet={len(sheet_keys)} "
              f"both={len(both)} supabase_only={len(sb_only)} sheet_only={len(sheet_only)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/probe_output/sheet_audit")
    ap.add_argument("--supabase-audit", default="tools/probe_output/audit")
    ap.add_argument("--brc", default="import_BRC.csv")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    brc = pda.load_brc_table(args.brc)
    plays = fetch_sheet_plays()
    print(f"\nTotal sheet plays: {len(plays)}")

    results = run_checks(plays, brc)

    print("\n=== Sheet audit summary ===")
    for name, rows in results.items():
        print(f"  {name}: {len(rows)}")
        pda.write_csv(out_dir / f"{name}.csv", rows)
    print(f"\nCSV reports written to {out_dir}/")

    compare(results, pathlib.Path(args.supabase_audit))


if __name__ == "__main__":
    main()

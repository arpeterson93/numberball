"""
Generate situational_result_frequencies.csv from the Supabase plays table.

Counts how often each result occurs within each (outs, obc) situation, which is
the empirical counterpart to result_ranges_re24.csv's theoretical per-situation
distribution. compare_win_probability_tables.py joins the two so we can see
whether real MLN play is actually converging on the theoretical table.

This is a validation input only. simulate_win_probability.py does NOT read this
file - it samples straight from result_ranges_re24.csv, which has no sampling
noise. Thin cells here just mean "not enough games yet to judge that cell", so
no smoothing is applied.

Sibling of compute_result_frequencies.py (which pools all situations together
for the leverage denominator) and compute_state_frequencies.py (same Supabase
fetch and obc normalization). Neither of those files is touched by this one.

Run from the project root:
    python compute_situational_result_frequencies.py
    python compute_situational_result_frequencies.py --season-start 10
"""
from __future__ import annotations

import argparse

import pandas as pd

import database
import utils  # loads import_BRC.csv into _BRC_RUN_LOOKUP

OUT_CSV = "situational_result_frequencies.csv"
LEAGUE = "MLN"


def _season_of(row: dict) -> int | None:
    """Season from the joined games() object database.get_all_plays selects."""
    game = row.get("games")
    if not isinstance(game, dict):
        return None
    try:
        return int(game.get("season"))
    except (TypeError, ValueError):
        return None


def _obc_of(raw) -> str | None:
    """Normalize obc to a zero-padded binary string, accepting either encoding.

    Plays carry obc as either a raw BRC int (0-7, sequential encoding) or an
    already-binary string, same as compute_state_frequencies.py handles.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (int, float)):
        return utils._BRC_TO_OBC.get(int(raw))
    code = str(raw).strip()
    if not code:
        return None
    if code.isdigit() and len(code) < 3:
        # A bare "1"/"10"/"100" is ambiguous only in appearance: these are
        # binary codes whose leading zeros were stripped somewhere upstream.
        code = code.zfill(3)
    code = code.zfill(3)
    return code if code in utils.OBC_OPTIONS else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season-start", type=int, default=6, help="lowest season to include (default 6)")
    ap.add_argument("--out", default=OUT_CSV, help=f"output CSV path (default {OUT_CSV})")
    args = ap.parse_args()

    print(f"Fetching {LEAGUE} plays from Supabase...")
    plays = database.get_all_plays(league=LEAGUE)
    if not plays:
        print("No plays found.")
        return
    print(f"Fetched {len(plays):,} plays")

    kept: list[tuple[int, str, str]] = []
    dropped_season = 0
    dropped_old = 0
    dropped_state = 0
    dropped_unknown = 0

    for row in plays:
        season = _season_of(row)
        if season is None:
            dropped_season += 1
            continue
        if season < args.season_start:
            dropped_old += 1
            continue

        obc = _obc_of(row.get("obc"))
        try:
            outs = int(row.get("outs"))
        except (TypeError, ValueError):
            outs = None
        if obc is None or outs is None or outs not in (0, 1, 2):
            dropped_state += 1
            continue

        result = str(row.get("result") or "").strip()
        # Same discipline compute_result_frequencies.py applies globally, but
        # per situation: keep only results the BRC lookup can advance runners
        # for from THIS state, so the comparison is against exactly the result
        # space the WP/LI engine models.
        if (result, obc, outs) not in utils._BRC_RUN_LOOKUP:
            dropped_unknown += 1
            continue

        kept.append((outs, obc, result))

    print(f"Dropped {dropped_season:,} plays with a missing/unparseable season")
    print(f"Dropped {dropped_old:,} plays before season {args.season_start}")
    print(f"Dropped {dropped_state:,} plays with an unusable outs/obc")
    print(f"Dropped {dropped_unknown:,} plays whose (result, obc, outs) is not in the BRC lookup")
    if not kept:
        print("Nothing left to count.")
        return

    df = pd.DataFrame(kept, columns=["outs", "obc", "result"])
    counts = (
        df.groupby(["outs", "obc", "result"])
        .size()
        .reset_index(name="count")
    )
    totals = counts.groupby(["outs", "obc"])["count"].transform("sum")
    counts["probability"] = counts["count"] / totals
    counts = counts.sort_values(["outs", "obc", "count"], ascending=[True, True, False])

    counts.to_csv(args.out, index=False)
    print(f"\nSaved {len(counts):,} (outs, obc, result) rows ({len(kept):,} plays) to {args.out}")

    group_totals = counts.groupby(["outs", "obc"])["count"].sum()
    missing = 24 - len(group_totals)
    print(
        f"\nPer-situation sample sizes: min={group_totals.min():,} "
        f"median={int(group_totals.median()):,} max={group_totals.max():,}"
        + (f"  ({missing} situation(s) never observed)" if missing else "")
    )
    print(group_totals.rename("plays").reset_index().to_string(index=False))


if __name__ == "__main__":
    main()

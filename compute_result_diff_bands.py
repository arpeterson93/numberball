"""
Generate result_diff_bands.csv from the Supabase plays table.

Per-result diff percentile bands feed the ball-flight animation's exit-velocity
and distance computation (docs/js/app.js flightParams): a play whose diff sits
near its result's 10th percentile reads as the hardest-hit version of that
result, near the 90th as the softest. See ball-flight-plan.md Stage 1a.

Sibling of compute_situational_result_frequencies.py (same Supabase fetch and
season-filter convention) but with no situational (outs, obc) dimension - this
table has exactly one row per result code.

Run from the project root:
    python compute_result_diff_bands.py
    python compute_result_diff_bands.py --season-start 10
"""
from __future__ import annotations

import argparse

import pandas as pd

import database
import utils

OUT_CSV = "result_diff_bands.csv"
LEAGUE = "MLN"
MIN_SAMPLE = 30

# Result code -> ball-flight archetype (must match ball_flight_archetypes.csv's
# archetype column exactly). This is the one place the mapping is defined -
# key_moments_build.py and app.js both read it from this script's output
# rather than carrying a second copy.
#
# TP -> grounder is low confidence (n=11 in the real feed, median diff ~497 -
# the worst-contact end of the whole range, closer to a badly-mishit
# comebacker than a screaming liner). Kept in grounder for v1; flagged here so
# a future reader knows it was a judgement call, not a measurement.
ARCHETYPE_OF = {
    "GO": "grounder", "GORA": "grounder", "DP": "grounder", "DP21": "grounder",
    "DP31": "grounder", "DP32": "grounder", "DPH1": "grounder", "DPRun": "grounder",
    "FC": "grounder", "FC3rd": "grounder", "FCH": "grounder", "FCLead": "grounder",
    "TP": "grounder",

    "SacB": "bunt", "BGO": "bunt", "BFC": "bunt", "B1B": "bunt", "B1BWH": "bunt",
    "BDP": "bunt",

    "IF1B": "infield_single",

    "LODP": "line_drive",

    "PO": "pop_up",

    "FO": "fly_ball", "SacF": "fly_ball", "DSacF": "fly_ball", "DFO": "fly_ball",

    "1B": "single", "1BWH": "single", "1BWH2": "single",

    "2B": "double", "2BWH": "double",

    "3B": "triple",

    "HR": "home_run",
}

# Excluded from ball flight entirely (Decision 1) - no batted ball, so no diff
# band is meaningful. Kept here only so main() can sanity-check every Swing-type
# result code is accounted for one way or the other.
NO_FLIGHT = {
    "K", "BB", "IBB", "AutoBB", "AutoK", "CS", "CS2", "CS3", "CS4",
    "SB", "SB2", "SB3", "SB4", "AutoSB", "Balk", "pAuto", "bAuto",
    "KCS", "SB32", "SB42", "SB43", "SB432",
}


def _season_of(row: dict) -> int | None:
    game = row.get("games")
    if not isinstance(game, dict):
        return None
    try:
        return int(game.get("season"))
    except (TypeError, ValueError):
        return None


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

    kept: list[tuple[str, int]] = []
    dropped_season = 0
    dropped_old = 0
    dropped_type = 0
    dropped_pitch_swing = 0
    unmapped: set[str] = set()

    for row in plays:
        season = _season_of(row)
        if season is None:
            dropped_season += 1
            continue
        if season < args.season_start:
            dropped_old += 1
            continue
        if str(row.get("play_type") or "") != "Swing":
            dropped_type += 1
            continue

        result = str(row.get("result") or "").strip()
        if result in NO_FLIGHT:
            continue

        pitch, swing = row.get("pitch"), row.get("swing")
        if pitch is None or swing is None:
            dropped_pitch_swing += 1
            continue

        if result not in ARCHETYPE_OF:
            unmapped.add(result)
            continue

        diff = utils.circular_diff(int(pitch), int(swing))
        kept.append((result, diff))

    print(f"Dropped {dropped_season:,} plays with a missing/unparseable season")
    print(f"Dropped {dropped_old:,} plays before season {args.season_start}")
    print(f"Dropped {dropped_type:,} non-Swing plays")
    print(f"Dropped {dropped_pitch_swing:,} Swing plays missing pitch/swing")
    if unmapped:
        print(f"WARNING: {len(unmapped)} result code(s) have no archetype mapping "
              f"and were skipped entirely: {sorted(unmapped)}")
    if not kept:
        print("Nothing left to count.")
        return

    df = pd.DataFrame(kept, columns=["result", "diff"])
    df["archetype"] = df["result"].map(ARCHETYPE_OF)

    # Archetype-pooled bands, computed once up front so every low-sample result
    # has a fallback ready regardless of processing order.
    pooled = (
        df.groupby("archetype")["diff"]
        .agg(n="count", band_lo=lambda s: s.quantile(0.10), band_hi=lambda s: s.quantile(0.90))
    )

    rows = []
    per_result_n = {}
    for result, g in df.groupby("result"):
        n = len(g)
        per_result_n[result] = n
        archetype = ARCHETYPE_OF[result]
        if n < MIN_SAMPLE:
            p = pooled.loc[archetype]
            rows.append({
                "result": result, "archetype": archetype, "n": n,
                "band_lo": int(round(p["band_lo"])), "band_hi": int(round(p["band_hi"])),
                "source": "archetype",
            })
        else:
            rows.append({
                "result": result, "archetype": archetype, "n": n,
                "band_lo": int(round(g["diff"].quantile(0.10))),
                "band_hi": int(round(g["diff"].quantile(0.90))),
                "source": "own",
            })

    out = pd.DataFrame(rows).sort_values(["archetype", "result"])
    out.to_csv(args.out, index=False)
    print(f"\nSaved {len(out)} result rows ({len(kept):,} plays) to {args.out}")

    fallback = out[out["source"] == "archetype"]["result"].tolist()
    print(f"\nPer-result sample sizes (n < {MIN_SAMPLE} falls back to the pooled archetype band):")
    print(out[["result", "archetype", "n", "band_lo", "band_hi", "source"]].to_string(index=False))
    if fallback:
        print(f"\nFallback set ({len(fallback)}): {fallback}")


if __name__ == "__main__":
    main()

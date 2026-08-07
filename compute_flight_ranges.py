"""
Generate result_diff_bands.csv's ball-flight columns (laMin/laIdeal/laMax/
evMin/evMax/depthMin/depthMax) from real MLB Statcast data via pybaseball.

Each MLN result is mapped to a Statcast filter (events/bb_type/hit_location/
launch_speed_angle/des - see FILTERS below) chosen to match what that result
actually represents, not just its shared ball-flight archetype. la_min/
la_max/ev_min/ev_max/depth_min/depth_max are the 10th/90th percentile of the
matching real plays; la_ideal is the 50th (median) - deliberately from the
SAME distribution as the bounds, so it can never collapse onto la_min/la_max
the way an externally-sourced physics constant could (see result_diff_bands.csv's
own comment on this).

One deliberate exception: depth_min/depth_max for grounder/infield_single/
bunt-family results are NOT computed from Statcast's hit_distance_sc.
That column measures where a batted ball first touches the ground - for a
fly ball that's the same as "where it's caught/lands," but for a ground ball
it's the first-bounce point, not the (much deeper) spot where an infielder
actually fields it after the ball rolls/bounces further (verified against
real hit_location: even shortstop-fielded groundouts, fielded 111-147ft out
per app.js's INFIELDER_DEPTH_FT, showed hit_distance_sc medians under 15ft).
Those three archetypes keep the hand-tuned GROUND_TOUCHING_DEPTH ranges
instead, regardless of sample size. la/ev are unaffected - both are measured
at contact, not distance-dependent, so Statcast's numbers for those are fine
even for ground balls.

Sibling of compute_result_diff_bands.py (same output file, disjoint set of
columns) - each script preserves the other's columns on write rather than
overwriting the whole file, since band_lo/band_hi/n/source come from MLN's
own play history (Supabase) while everything here comes from Statcast.

Run from the project root (pulls a full season by default - takes a few
minutes; pybaseball caches each day's pull locally, so a second run for an
overlapping range is fast):
    python compute_flight_ranges.py
    python compute_flight_ranges.py --start 2024-03-01 --end 2024-11-01
"""
from __future__ import annotations

import argparse

import pandas as pd

from compute_result_diff_bands import ARCHETYPE_OF

OUT_CSV = "result_diff_bands.csv"
MIN_SAMPLE = 20

# Hand-tuned, real-infield-depth-based ranges (see module docstring) - never
# derived from hit_distance_sc, no matter how much Statcast data is pulled.
GROUND_TOUCHING_DEPTH = {
    "grounder": (60, 150),
    "infield_single": (45, 90),
    "bunt": (5, 25),
}

PCTL_LO, PCTL_MID, PCTL_HI = 0.10, 0.50, 0.90


def _build_filters(bip: pd.DataFrame) -> dict[str, "pd.Series[bool]"]:
    """One boolean mask per MLN result, over `bip` (Statcast rows where
    type=='X', i.e. a ball actually put in play). See the ideas-and-opinions
    writeup this was designed from for the reasoning behind each one -
    results sharing an archetype often share a filter too (DP/DP21/DP31/...
    all just "events IN double-play-ish AND bb_type=ground_ball": Statcast
    has no field distinguishing which base a double play ran through), but
    a few deliberately don't (LODP excludes grounded_into_double_play - that
    literally means the ball hit the ground, not a lineout; 1B/2B/3B/1BWH/
    1BWH2/2BWH deliberately do NOT exclude bb_type=='ground_ball' even
    though that reuses the same first-bounce-not-fielding-point column
    discussed above - Alex's call: a ground ball that legs out for a hit is
    a real, common way these results happen and belongs in the sample, not
    filtered out for using the same imperfect distance column everything
    else here also has to live with)."""
    is_bunt = bip["des"].astype(str).str.contains("bunt", case=False, na=False)
    not_bunt = ~is_bunt

    fo_fly = bip[(bip["events"] == "field_out") & (bip["bb_type"] == "fly_ball")]
    sf_fly = bip[(bip["events"] == "sac_fly") & (bip["bb_type"] == "fly_ball")]
    fo_median = fo_fly["hit_distance_sc"].median()
    sf_median = sf_fly["hit_distance_sc"].median()

    ground_out = bip["events"].isin(["field_out", "force_out"]) & (bip["bb_type"] == "ground_ball") & not_bunt
    fielders_choice = bip["events"].isin(["fielders_choice", "fielders_choice_out"]) & (bip["bb_type"] == "ground_ball") & not_bunt
    double_play_gb = bip["events"].isin(["grounded_into_double_play", "double_play", "triple_play"]) & (bip["bb_type"] == "ground_ball") & not_bunt

    return {
        "GO": ground_out, "GORA": ground_out,
        "FC": fielders_choice, "FC3rd": fielders_choice, "FCH": fielders_choice,
        "DP": double_play_gb, "DP21": double_play_gb, "DP31": double_play_gb,
        "DPH1": double_play_gb, "DPRun": double_play_gb,
        # Real triple plays are too rare to trust alone (Alex's call) - reuse
        # the DP-family sample rather than a near-empty events=='triple_play' one.
        "TP": double_play_gb,

        "IF1B": (bip["events"] == "single") & bip["hit_location"].isin([1, 2, 3, 4, 5, 6]),

        "1B": (bip["events"] == "single") & bip["hit_location"].isin([7, 8, 9]) & bip["launch_speed_angle"].isin([1, 2, 3, 4]),
        "1BWH2": (bip["events"] == "single") & bip["hit_location"].isin([7, 8, 9]) & (bip["launch_speed_angle"] == 5),
        "1BWH": (bip["events"] == "single") & bip["hit_location"].isin([7, 8, 9]) & (bip["launch_speed_angle"] == 6),

        "2B": (bip["events"] == "double") & bip["launch_speed_angle"].isin([1, 2, 3, 4]),
        "2BWH": (bip["events"] == "double") & bip["launch_speed_angle"].isin([5, 6]),

        "3B": (bip["events"] == "triple"),
        "HR": (bip["events"] == "home_run"),

        "FO": (bip["events"] == "field_out") & (bip["bb_type"] == "fly_ball") & (bip["hit_distance_sc"] < fo_median),
        "DFO": (bip["events"] == "field_out") & (bip["bb_type"] == "fly_ball") & (bip["hit_distance_sc"] >= fo_median),
        "SacF": (bip["events"] == "sac_fly") & (bip["bb_type"] == "fly_ball") & (bip["hit_distance_sc"] < sf_median),
        "DSacF": (bip["events"] == "sac_fly") & (bip["bb_type"] == "fly_ball") & (bip["hit_distance_sc"] >= sf_median),

        "PO": (bip["events"] == "field_out") & (bip["bb_type"] == "popup"),
        # NOT grounded_into_double_play - that's a ball that hit the ground,
        # not a lineout (Alex's correction).
        "LODP": (bip["events"] == "double_play") & (bip["bb_type"] == "line_drive"),

        "B1B": is_bunt & (bip["events"] == "single"),
        "B1BWH": is_bunt & (bip["events"] == "single"),
        "BFC": is_bunt & bip["events"].isin(["fielders_choice", "fielders_choice_out"]),
        "BGO": is_bunt & bip["events"].isin(["field_out", "force_out"]),
        "SacB": (bip["events"] == "sac_bunt"),
        # A bunt double play is rare enough that Statcast alone won't have
        # much of a sample - Alex's call: treat it like BFC/BGO from a
        # contact-quality standpoint (same archetype-pooled fallback below
        # picks this up automatically whenever its own n < MIN_SAMPLE).
        "BDP": is_bunt & bip["events"].isin(["grounded_into_double_play", "double_play"]),
    }


def _percentiles(series: "pd.Series[float]") -> tuple[float, float, float]:
    s = series.dropna()
    return (round(s.quantile(PCTL_LO), 1), round(s.quantile(PCTL_MID), 1), round(s.quantile(PCTL_HI), 1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2025-03-18", help="Statcast pull start date (default: 2025 opening day)")
    ap.add_argument("--end", default="2025-11-05", help="Statcast pull end date (default: end of 2025 postseason)")
    ap.add_argument("--out", default=OUT_CSV, help=f"output CSV path (default {OUT_CSV})")
    args = ap.parse_args()

    import pybaseball as pb  # deferred: heavy import, only needed for this script

    pb.cache.enable()
    print(f"Pulling Statcast {args.start}..{args.end} (a few minutes on a cold cache)...")
    raw = pb.statcast(start_dt=args.start, end_dt=args.end)
    print(f"Pulled {len(raw):,} pitches")

    bip = raw[raw["type"] == "X"].copy()
    print(f"{len(bip):,} balls in play")

    filters = _build_filters(bip)
    unmapped = sorted(set(ARCHETYPE_OF) - set(filters))
    if unmapped:
        print(f"NOTE: {len(unmapped)} result(s) have an archetype mapping but no Statcast "
              f"filter defined here yet, skipped: {unmapped}")

    per_result: dict[str, dict] = {}
    for result, mask in filters.items():
        sub = bip[mask]
        la_lo, la_mid, la_hi = _percentiles(sub["launch_angle"])
        ev_lo, ev_mid, ev_hi = _percentiles(sub["launch_speed"])
        d_lo, _d_mid, d_hi = _percentiles(sub["hit_distance_sc"])
        per_result[result] = {
            "n": len(sub), "archetype": ARCHETYPE_OF[result],
            "la_min": la_lo, "la_ideal": la_mid, "la_max": la_hi,
            "ev_min": ev_lo, "ev_max": ev_hi,
            "depth_min": d_lo, "depth_max": d_hi,
        }

    # Archetype-pooled fallback for any result whose own Statcast sample is
    # too thin to trust (mirrors compute_result_diff_bands.py's pooled-band
    # pattern) - combine every OTHER result sharing the same archetype into
    # one bigger mask rather than hand-picking a single result to borrow from.
    by_archetype: dict[str, list[str]] = {}
    for result, arche in ARCHETYPE_OF.items():
        if result in filters:
            by_archetype.setdefault(arche, []).append(result)

    rows = []
    for result, stats in per_result.items():
        if stats["n"] >= MIN_SAMPLE:
            rows.append({"result": result, "flight_source": "own", **stats})
            continue
        siblings = [r for r in by_archetype[stats["archetype"]] if r != result]
        pooled_mask = pd.Series(False, index=bip.index)
        for sib in siblings:
            pooled_mask |= filters[sib]
        pooled = bip[pooled_mask]
        if len(pooled) < MIN_SAMPLE:
            print(f"WARNING: {result} (n={stats['n']}) and its whole '{stats['archetype']}' "
                  f"archetype pool (n={len(pooled)}) are both too thin - keeping its own "
                  f"(low-confidence) numbers rather than an equally-thin pooled fallback.")
            rows.append({"result": result, "flight_source": f"own (n={stats['n']}, low confidence)", **stats})
            continue
        la_lo, la_mid, la_hi = _percentiles(pooled["launch_angle"])
        ev_lo, ev_mid, ev_hi = _percentiles(pooled["launch_speed"])
        d_lo, _d_mid, d_hi = _percentiles(pooled["hit_distance_sc"])
        rows.append({
            "result": result, "flight_source": f"borrowed:{stats['archetype']} pool (n={stats['n']}<{MIN_SAMPLE})",
            "n": stats["n"], "archetype": stats["archetype"],
            "la_min": la_lo, "la_ideal": la_mid, "la_max": la_hi,
            "ev_min": ev_lo, "ev_max": ev_hi,
            "depth_min": d_lo, "depth_max": d_hi,
        })

    new = pd.DataFrame(rows).set_index("result")

    # Ground-touching archetypes never use hit_distance_sc-derived depth,
    # regardless of sample size - see module docstring.
    for arche, (lo, hi) in GROUND_TOUCHING_DEPTH.items():
        mask = new["archetype"] == arche
        new.loc[mask, "depth_min"] = lo
        new.loc[mask, "depth_max"] = hi
        new.loc[mask, "flight_source"] = new.loc[mask, "flight_source"] + "; depth=hand-tuned (hit_distance_sc is first-bounce point for grounders, not fielding depth)"

    flight_cols = ["la_min", "la_ideal", "la_max", "ev_min", "ev_max", "depth_min", "depth_max", "flight_source"]

    # Preserve band_lo/band_hi/n/source/archetype - compute_result_diff_bands.py's
    # columns, from MLN's own play history, not Statcast - the mirror image of
    # that script's own preserve-on-write logic for these flight columns.
    try:
        prev = pd.read_csv(args.out)
    except FileNotFoundError:
        prev = pd.DataFrame({"result": new.index})
    for col in flight_cols:
        prev[col] = prev["result"].map(new[col])
    prev = prev.sort_values(["archetype", "result"]) if "archetype" in prev.columns else prev
    prev.to_csv(args.out, index=False)

    print(f"\nSaved flight columns for {len(new)} results to {args.out}")
    print(new[["archetype", "n", "la_min", "la_ideal", "la_max", "ev_min", "ev_max", "depth_min", "depth_max"]].to_string())
    borrowed = new[new["flight_source"].str.contains("borrowed", na=False)]
    if len(borrowed):
        print(f"\nBorrowed from an archetype-pooled fallback ({len(borrowed)}): {list(borrowed.index)}")


if __name__ == "__main__":
    main()

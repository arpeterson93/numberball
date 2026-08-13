"""
Generate result_diff_bands.csv's ball-flight columns (laMin/laIdeal/laMax/
evMin/evMax/depthMin/depthMax) from real MLB Statcast data via pybaseball, and
flight_stations.csv - a per-result table of real (EV, LA) pairs ranked by
their own real Statcast distance (hit_distance_sc), so the live pipeline can
pick a target distance from q and read LA *and its own real paired EV* off
real, jointly-observed data (ideas-and-opinions conversation: independent
EV/LA marginals - and, for one design iteration, EV solved backward through
our own physics to hit a distance target - can each produce a pair no real
batted ball ever was, which is what was producing sign-flipped diff-vs-
distance relationships, clamp-clumping, and physically nonsensical parameter
readouts like a 130mph EV solve-ceiling clamp on a real play). See
flight_stations.csv's own header comment for the runtime side of this
(docs/js/app.js's stationsLookup, which now interpolates a real paired
(la, ev) directly - no more solveEvForDistance bisection). The runtime then
renders that real pair through the real drag+lift physics (verified byte-
for-byte against TrajectoryCalculator-new-3D-June2026.xlsx) and radially
rescales the landing point to match the real hit_distance_sc exactly, rather
than trusting our own model's recomputed distance for that pair - the two
can legitimately differ (ball-to-ball Cd/Cl variance the workbook's own
ReadMe admits, and - for anything a fielder stops early - hit_distance_sc
was never going to equal an uninterrupted flight distance in the first
place), and hit_distance_sc is the one that actually happened.

Each MLN result is mapped to a Statcast filter (events/bb_type/hit_location/
launch_speed_angle/des - see FILTERS below) chosen to match what that result
actually represents, not just its shared ball-flight archetype. la_min/
la_max/ev_min/ev_max/depth_min/depth_max are the 10th/90th percentile of the
matching real plays; la_ideal is the 50th (median) - deliberately from the
SAME distribution as the bounds, so it can never collapse onto la_min/la_max
the way an externally-sourced physics constant could (see result_diff_bands.csv's
own comment on this).

depth_min/depth_max are AUDIT/REFERENCE ONLY as of the physics-redesign port
(ball-flight-3d-physics-redesign-plan.md Part 2.4) - la/ev/laMin/laMax/evMin/
evMax on the band itself are audit-only too now that stations are the
primary path. docs/js/trajectory.js's simulateFlight derives the *shape* of
the flight from the real la/ev directly via the drag+lift integrator, for
every archetype including grounders; distance itself is then pinned to the
real hit_distance_sc via a radial scale (see stationsLookup's own comment).

One wrinkle worth flagging even though it's no longer load-bearing for depth_
min/max: for grounder/infield_single/bunt-family results, hit_distance_sc
measures where a batted ball first touches the ground, not where an
infielder actually fields it after the ball rolls/bounces further (verified
against real hit_location: even shortstop-fielded groundouts, fielded
111-147ft out per app.js's INFIELDER_DEPTH_FT, showed hit_distance_sc medians
under 15ft). It IS load-bearing for flight_stations.csv's dist_topped/
dist_uppercut/q though - see that column's own note in _build_stations: this
is exactly the right measure to rank/target by, since it's the real
first-bounce point a fielder actually had to deal with, not how far the ball
could have flown uninterrupted. la/ev are unaffected either way - both are
measured at contact, not distance-dependent.

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
import sys

import pandas as pd

from compute_result_diff_bands import ARCHETYPE_OF

OUT_CSV = "result_diff_bands.csv"
STATIONS_OUT_CSV = "flight_stations.csv"
MIN_SAMPLE = 20

PCTL_LO, PCTL_MID, PCTL_HI = 0.10, 0.50, 0.90

# Applied to every result's real sample before anything else touches it -
# la/ev/depth percentile bounds AND flight_stations both derive from this
# already-trimmed sample (Alex's call). Drops the extreme 5% shortest and 5%
# longest real hit_distance_sc plays before q ever gets a chance to land on
# one - the concrete failure case this closes: a real "1B" single recorded at
# hit_distance_sc=369ft (a deep drive that a great defensive play/wall carom
# held to a single, per the actual Statcast row) is a genuine outlier for
# what a single's distance distribution should look like, and q landing on
# it produced a station whose radial-scale target didn't match its own
# topped/uppercut pair's natural physics at all.
DIST_TRIM_LO, DIST_TRIM_HI = 0.05, 0.95


def _trim_by_distance(df: pd.DataFrame) -> pd.DataFrame:
    d = df["hit_distance_sc"]
    lo, hi = d.quantile(DIST_TRIM_LO), d.quantile(DIST_TRIM_HI)
    return df[(d >= lo) & (d <= hi)]


# Granular by design (Alex's call): a station table is now just a sort +
# local-window read over the real sample, not a per-point physics
# simulation, so there's no speed reason to keep this coarse. 101 (a q step
# of 0.01) gives "thin bands" without over-promising resolution a MIN_SAMPLE=
# 20 result can't actually back up.
N_STATIONS = 101
# The local window each station's topped/uppercut pick is drawn from, sized
# off the result's own real sample (min WINDOW_MIN points, else a fraction of
# n) - small on purpose. There's no physics-simulation noise to smooth over
# anymore (ranking is an exact sort on real hit_distance_sc), so the window's
# only remaining job is "how many real neighbors count as roughly this
# distance" - a wide window is what let a single outlier real play (an
# error-aided double with a 64deg LA, a mis-scored bunt) dominate an entire
# percentile's topped/uppercut read in the old (window-min/max) design.
WINDOW_MIN = 9
WINDOW_FRAC = 0.03
# Percentile-within-window, not true min/max, for the same reason: even a
# small window can still contain one freak real point, and a single point
# should not get to define an entire percentile's "topped" or "uppercut"
# read. Tied to the station's own q (Alex's call), not a fixed pair of
# constants - see the topped_pctl/uppercut_pctl computation inside
# _build_stations for the formula and reasoning.

# Fence-consistency ceiling for every non-HR result (Alex's call,
# ideas-and-opinions conversation): mirrors docs/js/app.js's FENCE_DEPTH_FT
# and clampToFence's own ceiling exactly (zero margin, since the render
# target is now a real Statcast distance, not a forecast) - keep these two in
# sync with that file. Every non-HR result is hard-capped at runtime
# regardless of what its Statcast sample says, so a sample that routinely
# exceeds that cap just produces silent clumping at the cap instead of a
# natural percentile spread below it - this filter keeps the real sample's
# percentile spread meaningful under that cap.
#
# HR gets NO distance floor here (a deliberate change from the previous
# design, which required hit_distance_sc >= FENCE_DEPTH_FT specifically to
# stop a short real HR from being *recomputed* - and inflated - by our own
# model). Now that distance is pinned to the real hit_distance_sc via a
# runtime radial scale instead of recomputed, that risk is gone: a real HR
# a couple feet short of FENCE_DEPTH_FT (including a genuine real inside-the-
# parker) renders reliably at that same real distance against our own
# uniform fence, giving a controlled, real-data-driven inside-the-park rate
# instead of the old design's physics-noise-driven one (verified there:
# 30.9% of simulated outcomes landed short pre-filter, an artifact of the
# recompute step, not a deliberate choice).
FENCE_DEPTH_FT = 375
NON_HR_CLAMP_FT = FENCE_DEPTH_FT


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

        # 2B is every real double, contact quality unfiltered (Alex's call -
        # unlike 1B/1BWH2/1BWH's three-way split above, doubles only split
        # two ways: 2BWH is deliberately the narrow, well-hit-only carve-out,
        # and 2B is meant to be the general case, not "every double that
        # ISN'T well-hit." Restricting it to launch_speed_angle 1-4 pulled
        # its own la_ideal down to a weak-contact-only 13deg (vs. 20deg for
        # 2BWH) - since MLN's own play history calls 2B roughly 8x as often
        # as 2BWH, nearly every double an Alex sees was drawn from that
        # skewed, weak/flare-only sample. Pooling in every contact quality
        # (including the 5/6 solid-contact/barrel ones 2BWH already covers)
        # makes 2B's distribution the real, representative one instead.
        "2B": (bip["events"] == "double"),
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


def _build_stations(sub: pd.DataFrame) -> list[dict]:
    """Rank this result's real (launch_speed, launch_angle, hit_distance_sc)
    triples - already trimmed to the DIST_TRIM_LO/HI percentile range by the
    caller - by their own real hit_distance_sc, not a recomputed physics
    distance, and collapse them into N_STATIONS evenly-spaced percentile
    stations. hit_distance_sc is the right thing to rank/target by
    specifically because it's the real first-bounce point a fielder actually
    had to deal with: for anything a fielder stops early (a grounder, an
    infield single, a bunt), an uninterrupted-flight distance was never
    going to match what actually happened, no matter how faithful the
    physics is (see this module's own docstring).

    At each station, la_topped/la_uppercut are picked from a percentile
    within a local window of the nearest real points by rank, tied to the
    station's own q (Alex's call) rather than a fixed pair of percentiles:
    topped_pctl = max(0.05, 0.5*q), uppercut_pctl = min(0.95, 1 - 0.5*q). At
    q=0 (this result's shortest real distances - weak/mis-hit contact)
    that's the 5th/95th percentile - the widest spread the window offers,
    floored so a single truest-extreme real point never gets to define it
    alone; at q=1 (longest real distances - solid contact) both converge on
    the window's own median LA, on the theory that a well-struck ball's
    launch angle is more consistent than a weakly-hit one's. Neither ever
    crosses 0.5, so topped can never end up picking a higher LA than
    uppercut.

    Each pick is paired with THAT SAME real point's own ev_topped/ev_uppercut
    AND dist_topped/dist_uppercut, never a value borrowed from a different
    real play. That full pairing is the whole point: docs/js/app.js's
    runtime reads la+ev+distance together off one real play, runs the real
    drag+lift physics on that real pair, and radially rescales the result to
    land at THAT SAME play's own real distance - not a shared "station
    target" drawn from whichever different real play happened to sit at this
    window's exact rank (verified failure mode of an earlier design: a
    topped/uppercut pair could be internally consistent with each other
    while still getting radially stretched to a completely different real
    play's distance)."""
    pairs = sub[["launch_speed", "launch_angle", "hit_distance_sc"]].dropna()
    pairs = pairs.sort_values("hit_distance_sc").reset_index(drop=True)

    n = len(pairs)
    window = max(WINDOW_MIN, round(WINDOW_FRAC * n))
    stations = []
    for i in range(N_STATIONS):
        q = i / (N_STATIONS - 1)
        rank = round(q * (n - 1))
        lo = max(0, rank - window // 2)
        hi = min(n, lo + window)
        lo = max(0, hi - window)  # re-clamp so short windows at either edge still get `window` points
        local = pairs.iloc[lo:hi].sort_values("launch_angle").reset_index(drop=True)
        m = len(local)
        topped_pctl = max(0.05, 0.5 * q)
        uppercut_pctl = min(0.95, 1 - 0.5 * q)
        topped_row = local.iloc[round(topped_pctl * (m - 1))]
        uppercut_row = local.iloc[round(uppercut_pctl * (m - 1))]
        stations.append({
            "station_idx": i, "q": round(q, 4),
            "la_topped": round(float(topped_row["launch_angle"]), 1),
            "ev_topped": round(float(topped_row["launch_speed"]), 1),
            "dist_topped": round(float(topped_row["hit_distance_sc"]), 1),
            "la_uppercut": round(float(uppercut_row["launch_angle"]), 1),
            "ev_uppercut": round(float(uppercut_row["launch_speed"]), 1),
            "dist_uppercut": round(float(uppercut_row["hit_distance_sc"]), 1),
        })
    return stations


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2025-03-18", help="Statcast pull start date (default: 2025 opening day)")
    ap.add_argument("--end", default="2025-11-05", help="Statcast pull end date (default: end of 2025 postseason)")
    ap.add_argument("--out", default=OUT_CSV, help=f"output CSV path (default {OUT_CSV})")
    ap.add_argument("--stations-out", default=STATIONS_OUT_CSV, help=f"station-table output CSV path (default {STATIONS_OUT_CSV})")
    args = ap.parse_args()

    import pybaseball as pb  # deferred: heavy import, only needed for this script

    pb.cache.enable()
    print(f"Pulling Statcast {args.start}..{args.end} (a few minutes on a cold cache)...")
    raw = pb.statcast(start_dt=args.start, end_dt=args.end)
    print(f"Pulled {len(raw):,} pitches")

    bip = raw[raw["type"] == "X"].copy()
    print(f"{len(bip):,} balls in play")

    filters = _build_filters(bip)

    # Applied to every filter before any percentile/pooling math sees them,
    # so the archetype-pooled fallback (which OR's sibling filters together
    # below) inherits the same constraint automatically rather than needing
    # its own copy. HR gets no floor - see NON_HR_CLAMP_FT's own comment.
    for result in filters:
        if result != "HR":
            filters[result] = filters[result] & (bip["hit_distance_sc"] <= NON_HR_CLAMP_FT)

    # F6 hardening: an existing CSV row with no filter here would otherwise
    # NaN-overwrite silently on write (prev["result"].map(new[...]) against a
    # result key `new` never got) - fail loudly instead so a result added to
    # ARCHETYPE_OF/the CSV without a matching Statcast filter is caught at
    # generation time, not discovered later as a silent NaN in meta.json.
    try:
        existing_results = set(pd.read_csv(args.out)["result"])
    except FileNotFoundError:
        existing_results = set()
    unmapped_existing = sorted(existing_results - set(filters))
    if unmapped_existing:
        sys.exit(
            f"ERROR: {len(unmapped_existing)} result(s) already in {args.out} have no Statcast "
            f"filter defined in _build_filters: {unmapped_existing}. Add a filter for each before "
            f"regenerating - a silent NaN-overwrite is worse than failing here."
        )

    unmapped = sorted(set(ARCHETYPE_OF) - set(filters))
    if unmapped:
        print(f"NOTE: {len(unmapped)} result(s) have an archetype mapping but no Statcast "
              f"filter defined here yet, skipped: {unmapped}")

    per_result: dict[str, dict] = {}
    sample_by_result: dict[str, pd.DataFrame] = {}
    for result, mask in filters.items():
        sub = _trim_by_distance(bip[mask])
        sample_by_result[result] = sub
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
        pooled = _trim_by_distance(bip[pooled_mask])
        if len(pooled) < MIN_SAMPLE:
            print(f"WARNING: {result} (n={stats['n']}) and its whole '{stats['archetype']}' "
                  f"archetype pool (n={len(pooled)}) are both too thin - keeping its own "
                  f"(low-confidence) numbers rather than an equally-thin pooled fallback.")
            rows.append({"result": result, "flight_source": f"own (n={stats['n']}, low confidence)", **stats})
            continue
        sample_by_result[result] = pooled
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

    # Audit-only annotation (Part 2.4.2): depth_min/depth_max for these three
    # archetypes are real Statcast percentiles like everything else now, but
    # hit_distance_sc measures first ground contact, not fielding depth (see
    # module docstring) - flag it in flight_source so nobody reading the CSV
    # mistakes a ~10ft depth_max for "how far this grounder travels."
    ground_touching_archetypes = {"grounder", "infield_single", "bunt"}
    ground_touching_mask = new["archetype"].isin(ground_touching_archetypes)
    new.loc[ground_touching_mask, "flight_source"] = (
        new.loc[ground_touching_mask, "flight_source"]
        + "; depth=first-bounce point (hit_distance_sc), audit reference only - not a runtime input"
    )

    # F9 hardening: la_min < la_ideal < la_max must hold after rounding for
    # every row - launchAngleFor (docs/js/app.js) divides by (laIdeal-laMin)/
    # (laMax-laIdeal) and a collapsed bound (a tie introduced by the 0.1
    # rounding in _percentiles) would zero one side of that range out
    # entirely. Nudge the offending bound by 0.1 rather than shipping a
    # collapsed range, and say so. (launchAngleFor/laMin/laMax are audit-only
    # / fallback-only now that stations are the primary path, but a stale
    # cached meta.json can still hit the fallback, so this must keep holding.)
    for result in new.index:
        lo, mid, hi = new.at[result, "la_min"], new.at[result, "la_ideal"], new.at[result, "la_max"]
        if lo >= mid:
            new.at[result, "la_min"] = mid - 0.1
            print(f"NOTE: {result} la_min tied/crossed la_ideal ({lo} >= {mid}) - nudged la_min to {mid - 0.1}")
        if hi <= mid:
            new.at[result, "la_max"] = mid + 0.1
            print(f"NOTE: {result} la_max tied/crossed la_ideal ({hi} <= {mid}) - nudged la_max to {mid + 0.1}")
        assert new.at[result, "la_min"] < mid < new.at[result, "la_max"], \
            f"{result}: la_min < la_ideal < la_max still fails after nudging"

    flight_cols = ["la_min", "la_ideal", "la_max", "ev_min", "ev_max", "depth_min", "depth_max", "flight_source"]

    # F6 hardening: a cold run (no existing CSV) must fail rather than emit a
    # bands-less file - band_lo/band_hi/n/source/archetype come from MLN's own
    # play history (compute_result_diff_bands.py), not from this script, so
    # there is nothing sensible to write on a first-ever run here.
    try:
        prev = pd.read_csv(args.out)
    except FileNotFoundError:
        sys.exit(
            f"ERROR: {args.out} does not exist - run compute_result_diff_bands.py first "
            f"(it owns band_lo/band_hi/n/source/archetype; this script only fills in the "
            f"flight columns on top of an existing file)."
        )
    # Preserve band_lo/band_hi/n/source/archetype - compute_result_diff_bands.py's
    # columns, from MLN's own play history, not Statcast - the mirror image of
    # that script's own preserve-on-write logic for these flight columns.
    for col in flight_cols:
        prev[col] = prev["result"].map(new[col])
    prev = prev.sort_values(["archetype", "result"]) if "archetype" in prev.columns else prev
    prev.to_csv(args.out, index=False)

    print(f"\nSaved flight columns for {len(new)} results to {args.out}")
    print(new[["archetype", "n", "la_min", "la_ideal", "la_max", "ev_min", "ev_max", "depth_min", "depth_max"]].to_string())
    borrowed = new[new["flight_source"].str.contains("borrowed", na=False)]
    if len(borrowed):
        print(f"\nBorrowed from an archetype-pooled fallback ({len(borrowed)}): {list(borrowed.index)}")

    # flight_stations.csv: rank the same (own-or-pooled) sample used above by
    # its own real hit_distance_sc and collapse into N_STATIONS percentile
    # stations - see _build_stations' docstring. Built from sample_by_result
    # (the exact sample - own or pooled - each result's la/ev/depth
    # percentiles above were computed from), so the two files never disagree
    # about which real plays back a given result.
    print(f"\nBuilding {N_STATIONS}-station flight tables...")
    station_rows = []
    for result, sample in sample_by_result.items():
        for station in _build_stations(sample):
            station_rows.append({"result": result, **station})
    stations_df = pd.DataFrame(station_rows)
    stations_df.to_csv(args.stations_out, index=False)
    print(f"Saved {N_STATIONS}-station flight tables for {len(sample_by_result)} results to {args.stations_out}")


if __name__ == "__main__":
    main()

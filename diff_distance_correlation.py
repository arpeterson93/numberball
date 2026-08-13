"""
Study: how well does `diff` (expressed as q, this result's own closeness-to-
ideal-timing signal) correlate with the eventual in-air distance and
in-air+rollout total distance produced by the current pipeline?

Sweeps two pipelines:
  - "old": the retired independent-marginal launchAngleFor/EV formula
    (docs/js/app.js's launchAngleFor, ported verbatim below). This is the
    formula whose q-vs-distance correlations first surfaced the sign-flipped/
    clamp-clumping problem (ideas-and-opinions conversation) that motivated
    the stations redesign in compute_flight_ranges.py/app.js.
  - "new": the current joint EV/LA stations pipeline (docs/js/app.js's
    stationsLookup/solveEvForDistance, ported verbatim below, reading
    flight_stations.csv). This is what's live today - sweeping it validates
    the fix actually produced a monotonic q -> distance relationship across
    the full (q, on_top, HZ angle) grid, not just the 4 hand-picked worked
    examples in ball_flight_test.py.

Both pipelines reuse the exact physics integrator from
tools/trajectory_reference.py (the same drag+lift model ported to
docs/js/trajectory.js), so this is a faithful simulation of what the live
game actually produces - not an idealized model.

Run from project root:
    python tools/trajectory_reference.py  # sanity: confirm golden vectors still pass
    python diff_distance_correlation.py [--pipeline old|new|both] [--q-steps-new N]
Output per pipeline: diff_distance_correlation_long_<pipeline>.csv (long
form) and diff_distance_correlation_summary_<pipeline>.csv (per-result
summary). With --pipeline both (default) also writes
diff_distance_correlation_old_vs_new.csv, a per-result comparison on
whichever metric (dist for carry-only archetypes, total otherwise) actually
matters for that result, plus a printed comparison table.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, r"C:\Users\Alex\PycharmProjects\numberball")
sys.path.insert(0, r"C:\Users\Alex\PycharmProjects\numberball\tools")
from trajectory_reference import simulate, ground_path

# Mirrors app.js's CAUGHT_IN_AIR (old pipeline's "caught" set) and
# CARRY_ONLY_ARCHETYPES (new pipeline's - CAUGHT_IN_AIR plus home_run, since
# compute_flight_ranges.py's station tables were ranked by carry-only
# distance for HR too - see that file's CARRY_ONLY_ARCHETYPES comment).
CAUGHT_IN_AIR = {"fly_ball", "pop_up", "line_drive"}
CARRY_ONLY_ARCHETYPES = CAUGHT_IN_AIR | {"home_run"}

HZ_ANGLES = [5, 13, 21, 29, 37, 45, 53, 61, 69, 77, 85]  # the 11 lattice angles (hand='R' side; symmetric for 'L')
Q_STEPS = 101  # old pipeline: closed-form, cheap - dense grid is fine
Q_STEPS_NEW_DEFAULT = 21  # new pipeline: each point is a ~30-iter bisection - coarser grid keeps this tractable

BANDS_CSV = r"C:\Users\Alex\PycharmProjects\numberball\result_diff_bands.csv"
STATIONS_CSV = r"C:\Users\Alex\PycharmProjects\numberball\flight_stations.csv"

# Mirrors docs/js/app.js's EV_SOLVE_LO/HI/TOL_FT/MAX_ITER exactly.
EV_SOLVE_LO, EV_SOLVE_HI, EV_SOLVE_TOL_FT, EV_SOLVE_MAX_ITER = 20, 130, 0.5, 30


def launch_angle_for(la_ideal, la_min, la_max, q, on_top):
    """Verbatim port of docs/js/app.js's (retired) launchAngleFor."""
    if on_top:
        return la_ideal - (1 - q) * (la_ideal - la_min)
    else:
        return la_ideal + (1 - q) * (la_max - la_ideal)


def load_stations(path):
    """result -> [station, ...] sorted by q, keys renamed to match app.js's
    stationsLookup (distFt/laTopped/laUppercut) rather than the CSV's
    snake_case columns."""
    df = pd.read_csv(path).sort_values(["result", "station_idx"])
    out = {}
    for result, group in df.groupby("result"):
        out[result] = [
            {"q": float(r["q"]), "distFt": float(r["distance_ft"]),
             "laTopped": float(r["la_topped"]), "laUppercut": float(r["la_uppercut"])}
            for _, r in group.iterrows()
        ]
    return out


def stations_lookup(stations, q):
    """Verbatim port of docs/js/app.js's stationsLookup."""
    if not stations:
        return None
    first, last = stations[0], stations[-1]
    if q <= first["q"]:
        return {"distFt": first["distFt"], "laTopped": first["laTopped"], "laUppercut": first["laUppercut"]}
    if q >= last["q"]:
        return {"distFt": last["distFt"], "laTopped": last["laTopped"], "laUppercut": last["laUppercut"]}
    for a, b in zip(stations, stations[1:]):
        if a["q"] <= q <= b["q"]:
            t = (q - a["q"]) / (b["q"] - a["q"]) if b["q"] > a["q"] else 0.0
            return {
                "distFt": a["distFt"] + (b["distFt"] - a["distFt"]) * t,
                "laTopped": a["laTopped"] + (b["laTopped"] - a["laTopped"]) * t,
                "laUppercut": a["laUppercut"] + (b["laUppercut"] - a["laUppercut"]) * t,
            }
    return {"distFt": last["distFt"], "laTopped": last["laTopped"], "laUppercut": last["laUppercut"]}


def flight_metrics(ev, la, phi, hand, carry_only):
    """(dist, rollout, total) for one simulated batted ball, or None if the
    integrator itself declined (e.g. a foul/invalid trajectory). total ==
    dist for carry_only archetypes (a caught ball, or a cleared HR, has no
    rollout phase at runtime either) - mirrors app.js's evDistance."""
    sim = simulate(ev, la, phi, hand)
    if sim is None:
        return None
    dist = sim["dist"]
    if carry_only:
        return dist, 0.0, dist
    sh = np.hypot(sim["vx"], sim["vy"])
    gp = ground_path(sh, sim["vz"])
    rollout = gp["restFt"]
    return dist, rollout, dist + rollout


def solve_ev_for_distance(target, la, phi, hand, carry_only):
    """Verbatim port of docs/js/app.js's solveEvForDistance. Returns
    (ev, hit_lo_bracket, hit_hi_bracket) - the bracket flags are new here
    (app.js doesn't need them at runtime) and flag targets that were
    physically infeasible at this LA/phi/hand across the whole EV range, the
    real robustness question this sweep exists to answer."""
    lo, hi = EV_SOLVE_LO, EV_SOLVE_HI
    d_lo = flight_metrics(lo, la, phi, hand, carry_only)
    d_hi = flight_metrics(hi, la, phi, hand, carry_only)
    if d_lo is None or d_hi is None:
        return (lo + hi) / 2, False, False
    d_lo, d_hi = d_lo[2], d_hi[2]
    if target <= d_lo:
        return lo, True, False
    if target >= d_hi:
        return hi, False, True
    for _ in range(EV_SOLVE_MAX_ITER):
        mid = (lo + hi) / 2
        m = flight_metrics(mid, la, phi, hand, carry_only)
        if m is None:
            break
        d_mid = m[2]
        if abs(d_mid - target) <= EV_SOLVE_TOL_FT:
            return mid, False, False
        if d_mid < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, False, False


def run(pipeline, stations_by_result=None, q_steps=Q_STEPS):
    bands = pd.read_csv(BANDS_CSV)
    q_grid = np.linspace(0, 1, q_steps)
    rows = []

    for _, b in bands.iterrows():
        result = b["result"]
        archetype = b["archetype"]
        la_min, la_ideal, la_max = b["la_min"], b["la_ideal"], b["la_max"]
        ev_min, ev_max = b["ev_min"], b["ev_max"]
        carry_only = archetype in (CAUGHT_IN_AIR if pipeline == "old" else CARRY_ONLY_ARCHETYPES)
        stations = (stations_by_result or {}).get(result) if pipeline == "new" else None

        for on_top in (True, False):
            for angle in HZ_ANGLES:
                phi = angle - 45
                for q in q_grid:
                    target = np.nan
                    hit_lo = hit_hi = False
                    if pipeline == "old" or not stations:
                        LA = launch_angle_for(la_ideal, la_min, la_max, q, on_top)
                        EV = ev_min + q * (ev_max - ev_min)
                    else:
                        st = stations_lookup(stations, q)
                        LA = st["laTopped"] if on_top else st["laUppercut"]
                        target = st["distFt"]
                        EV, hit_lo, hit_hi = solve_ev_for_distance(target, LA, phi, "R", carry_only)
                    m = flight_metrics(EV, LA, phi, "R", carry_only)
                    if m is None:
                        continue
                    dist, rollout, total = m
                    rows.append((result, archetype, carry_only, on_top, angle, q, LA, EV,
                                 dist, rollout, total, target, hit_lo, hit_hi))

    df = pd.DataFrame(rows, columns=[
        "result", "archetype", "carry_only", "on_top", "angle", "q",
        "la", "ev", "dist", "rollout", "total", "target", "hit_ev_lo_bracket", "hit_ev_hi_bracket",
    ])
    return df


def r2_of(y, X):
    """Simple OLS R^2 for y ~ X (X includes intercept column already, or pass a design matrix)."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ beta
    ss_res = np.sum((y - pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    return 1 - ss_res / ss_tot if ss_tot > 0 else np.nan


def design_matrix(sub, include_onTop=False, include_angle=False):
    n = len(sub)
    cols = [np.ones(n), sub["q"].values]
    if include_onTop:
        cols.append(sub["on_top"].astype(float).values)
    if include_angle:
        # one-hot the 11 lattice angles (drop first to avoid collinearity with intercept)
        for a in HZ_ANGLES[1:]:
            cols.append((sub["angle"].values == a).astype(float))
    return np.column_stack(cols)


def nonmonotonic_fraction(sub, col):
    """Fraction of (on_top, angle) cells where `col` is NOT monotonically
    non-decreasing in q (i.e. has an interior peak/valley) - direct evidence
    that a smaller diff does not always mean a longer ball within this cell."""
    bad = 0
    total_cells = 0
    peak_qs = []
    for (ot, ang), g in sub.groupby(["on_top", "angle"]):
        g = g.sort_values("q")
        vals = g[col].values
        total_cells += 1
        diffs = np.diff(vals)
        if np.any(diffs < -1e-6):
            bad += 1
            peak_qs.append(g["q"].values[np.argmax(vals)])
    frac = bad / total_cells if total_cells else np.nan
    return frac, peak_qs


def summarize(df, pipeline):
    out = []
    for result, sub in df.groupby("result"):
        archetype = sub["archetype"].iloc[0]
        carry_only = bool(sub["carry_only"].iloc[0])

        r_d, _ = stats.pearsonr(sub["q"], sub["dist"])
        rho_d, _ = stats.spearmanr(sub["q"], sub["dist"])
        r2_d_q = r2_of(sub["dist"].values, design_matrix(sub))
        r2_d_full = r2_of(sub["dist"].values, design_matrix(sub, True, True))
        frac_nm_d, peaks_d = nonmonotonic_fraction(sub, "dist")

        if not carry_only:
            r_t, _ = stats.pearsonr(sub["q"], sub["total"])
            rho_t, _ = stats.spearmanr(sub["q"], sub["total"])
            r2_t_q = r2_of(sub["total"].values, design_matrix(sub))
            r2_t_full = r2_of(sub["total"].values, design_matrix(sub, True, True))
            frac_nm_t, peaks_t = nonmonotonic_fraction(sub, "total")
        else:
            r_t = rho_t = r2_t_q = r2_t_full = frac_nm_t = np.nan
            peaks_t = []

        # primary = whichever metric actually matters for this archetype -
        # dist for carry-only (caught/HR), total otherwise. This is the
        # number the old-vs-new comparison table is built on.
        if carry_only:
            primary_metric, r_p, rho_p, r2_p, r2_p_full, frac_nm_p, peaks_p = (
                "dist", r_d, rho_d, r2_d_q, r2_d_full, frac_nm_d, peaks_d)
        else:
            primary_metric, r_p, rho_p, r2_p, r2_p_full, frac_nm_p, peaks_p = (
                "total", r_t, rho_t, r2_t_q, r2_t_full, frac_nm_t, peaks_t)

        row = {
            "result": result, "archetype": archetype, "carry_only": carry_only,
            "primary_metric": primary_metric,
            "r_primary": r_p, "rho_primary": rho_p, "r2_primary": r2_p, "r2_primary_full": r2_p_full,
            "frac_nonmonotonic_primary": frac_nm_p, "median_peak_q_primary": np.median(peaks_p) if peaks_p else np.nan,
            "r_q_dist": r_d, "rho_q_dist": rho_d, "r2_dist_q_only": r2_d_q, "r2_dist_full": r2_d_full,
            "frac_nonmonotonic_dist": frac_nm_d, "median_peak_q_dist": np.median(peaks_d) if peaks_d else np.nan,
            "r_q_total": r_t, "rho_q_total": rho_t, "r2_total_q_only": r2_t_q, "r2_total_full": r2_t_full,
            "frac_nonmonotonic_total": frac_nm_t, "median_peak_q_total": np.median(peaks_t) if peaks_t else np.nan,
        }
        if pipeline == "new":
            has_target = sub["target"].notna()
            if has_target.any():
                primary = sub["dist"] if carry_only else sub["total"]
                err = (primary - sub["target"]).abs()[has_target]
                row["mean_abs_solve_err_ft"] = err.mean()
                row["max_abs_solve_err_ft"] = err.max()
                row["frac_hit_ev_bracket"] = (sub["hit_ev_lo_bracket"] | sub["hit_ev_hi_bracket"]).mean()
                row["frac_fell_back_to_old_formula"] = (~has_target).mean()
            else:
                row["mean_abs_solve_err_ft"] = row["max_abs_solve_err_ft"] = row["frac_hit_ev_bracket"] = np.nan
                row["frac_fell_back_to_old_formula"] = 1.0
        out.append(row)
    return pd.DataFrame(out).sort_values("r2_primary")


OUT_DIR = os.path.dirname(__file__) or "."


def _run_and_summarize(pipeline, stations_by_result, q_steps):
    df = run(pipeline, stations_by_result, q_steps)
    df.to_csv(os.path.join(OUT_DIR, f"diff_distance_correlation_long_{pipeline}.csv"), index=False)
    summary = summarize(df, pipeline)
    summary.to_csv(os.path.join(OUT_DIR, f"diff_distance_correlation_summary_{pipeline}.csv"), index=False)
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pipeline", choices=["old", "new", "both"], default="both")
    ap.add_argument("--q-steps-new", type=int, default=Q_STEPS_NEW_DEFAULT,
                     help="q-grid resolution for the new (bisection-based, much slower per point) pipeline sweep")
    args = ap.parse_args()

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 25)
    pd.set_option("display.float_format", lambda x: f"{x:.3f}")

    summaries = {}
    if args.pipeline in ("old", "both"):
        print("Sweeping OLD pipeline (retired independent-marginal launchAngleFor/EV formula)...")
        summaries["old"] = _run_and_summarize("old", None, Q_STEPS)
        print(f"  {len(summaries['old'])} results swept.")

    if args.pipeline in ("new", "both"):
        stations_by_result = load_stations(STATIONS_CSV)
        bands_n = len(pd.read_csv(BANDS_CSV))
        n_combos = args.q_steps_new * len(HZ_ANGLES) * 2 * bands_n
        print(f"Sweeping NEW pipeline (stations joint EV/LA: {args.q_steps_new} q-steps x "
              f"{len(HZ_ANGLES)} angles x 2 on_top x {bands_n} results = {n_combos:,} bisections - slow, be patient)...")
        summaries["new"] = _run_and_summarize("new", stations_by_result, args.q_steps_new)
        print(f"  {len(summaries['new'])} results swept.")

    for pipeline, summary in summaries.items():
        print(f"\n=== {pipeline.upper()} pipeline summary (sorted by r2_primary ascending - worst first) ===")
        cols = ["result", "archetype", "primary_metric", "r_primary", "rho_primary", "r2_primary",
                "frac_nonmonotonic_primary"]
        if pipeline == "new":
            cols += ["mean_abs_solve_err_ft", "max_abs_solve_err_ft", "frac_hit_ev_bracket", "frac_fell_back_to_old_formula"]
        print(summary[cols].to_string(index=False))

    if "old" in summaries and "new" in summaries:
        cmp = summaries["old"][["result", "archetype", "primary_metric", "r2_primary", "frac_nonmonotonic_primary"]].rename(
            columns={"r2_primary": "r2_old", "frac_nonmonotonic_primary": "frac_nonmonotonic_old"}
        ).merge(
            summaries["new"][["result", "r2_primary", "frac_nonmonotonic_primary",
                               "frac_hit_ev_bracket", "mean_abs_solve_err_ft", "frac_fell_back_to_old_formula"]].rename(
                columns={"r2_primary": "r2_new", "frac_nonmonotonic_primary": "frac_nonmonotonic_new"}
            ),
            on="result",
        )
        cmp["r2_improvement"] = cmp["r2_new"] - cmp["r2_old"]
        cmp = cmp.sort_values("r2_improvement")
        print("\n=== OLD vs NEW: r2(q -> primary metric) per result ===")
        print(cmp.to_string(index=False))
        cmp.to_csv(os.path.join(OUT_DIR, "diff_distance_correlation_old_vs_new.csv"), index=False)

        n_worse = int((cmp["r2_improvement"] < -0.01).sum())
        n_low_new = int((cmp["r2_new"] < 0.9).sum())
        n_bracket = int((cmp["frac_hit_ev_bracket"] > 0).sum())
        n_fallback = int((cmp["frac_fell_back_to_old_formula"] > 0).sum())
        print(f"\n{n_worse} result(s) got WORSE under the new pipeline (r2 dropped by >0.01).")
        print(f"{n_low_new} result(s) still have r2_new < 0.9 despite the fix.")
        print(f"{n_bracket} result(s) hit the EV-solve bracket edge (target infeasible at some LA/phi) at least once.")
        print(f"{n_fallback} result(s) fell back to the old formula at least once (no/short station table).")

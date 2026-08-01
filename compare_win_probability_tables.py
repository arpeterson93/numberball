"""
Compare the Monte Carlo win probability table against the current one.

Read-only report. Writes nothing unless --out is passed, and never touches
win_probability_table.csv, result_frequencies.csv or state_frequencies.csv.

Two comparisons:

  1. win_probability_table.csv (empirical-Bayesian, built by
     compute_win_probability.py) vs. simulated_win_probability_table.csv
     (forward Monte Carlo, built by simulate_win_probability.py) - coverage,
     absolute win_prob differences overall and by situation, a
     state-frequency-weighted difference so discrepancies in states real games
     rarely reach count for less, and a monotonicity check on the simulated
     table.

  2. situational_result_frequencies.csv (real MLN play, built by
     compute_situational_result_frequencies.py) vs. result_ranges_re24.csv's
     theoretical per-situation distribution - does actual play track the table
     the simulator is driven by? Skipped with a note if that file has not been
     generated. This section is informational: a large, well-supported
     divergence is a reason to revisit whether result_ranges_re24.csv still
     reflects current play, but that call is Alex's, not this script's.

The third comparison in the plan - what the simulated table does to leverage
and the Key Moments feed - is deliberately manual, since neither utils.py nor
compute_win_probability.py gains a table-swapping switch for a one-off check.
On a throwaway branch: move win_probability_table.csv aside, copy
simulated_win_probability_table.csv into its place, rerun
`python scripts/calibrate_stoplight_thresholds.py` and/or
`python key_moments_build.py`, compare the leverage / high_leverage
distribution against today's, then restore the original file.

Run from the project root:
    python compare_win_probability_tables.py
    python compare_win_probability_tables.py --out wp_comparison.csv
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

import utils

CURRENT_CSV = "win_probability_table.csv"
SIMULATED_CSV = "simulated_win_probability_table.csv"
STATE_FREQ_CSV = "state_frequencies.csv"
RANGES_CSV = "result_ranges_re24.csv"
SITUATIONAL_CSV = "situational_result_frequencies.csv"

KEYS = ["remaining", "outs", "obc", "batting_lead"]
RNG_DOMAIN = 501
LOW_N = 30          # simulated cells below this are treated as noisy
RISP = {"010", "100", "011", "101", "110", "111"}
LOADED = "111"
# Both tables store leads out to +/-18, but utils.get_win_probability and
# get_win_probability_interpolated clamp every query to +/-10, so cells beyond
# that are stored-but-never-read until those clamps are widened separately.
QUERY_CLAMP = 10


def _read_wp(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"obc": str})
    df["obc"] = df["obc"].str.strip().str.zfill(3)
    for col in ("remaining", "outs", "batting_lead"):
        df[col] = df[col].astype(int)
    df["win_prob"] = df["win_prob"].astype(float)
    return df


def _z_score(wp_a: float, n_a: int, wp_b: float, n_b: int, size: float) -> float:
    """How many standard errors an inversion is, treating both cells as binomials.

    A pair of cells sitting near 0 or 1 can invert by a few ten-thousandths on
    thousands of games and mean nothing; a pair inverting by 0.06 on 40 games
    each means just as little. This puts both on the same scale so the report
    can separate sampling scatter from an ordering the simulator actually got
    wrong.
    """
    var = 0.0
    for wp, n in ((wp_a, n_a), (wp_b, n_b)):
        if n > 0:
            var += wp * (1.0 - wp) / n
    if var <= 0:
        return float("inf") if size > 0 else 0.0
    return size / np.sqrt(var)


def _sim_se(both: pd.DataFrame) -> pd.Series:
    """Standard error of each simulated cell's win_prob.

    Laplace-smoothed so a cell that happened to go 12-for-12 gets a finite
    error bar instead of claiming certainty.
    """
    if "n" not in both.columns:
        return pd.Series(np.nan, index=both.index)
    n = both["n"].clip(lower=1)
    p = (both["win_prob_sim"] * n + 1.0) / (n + 2.0)
    return np.sqrt(p * (1.0 - p) / n)


def _noise_floor(both: pd.DataFrame) -> pd.Series:
    """Per-cell |difference| expected from the simulator's own sampling error.

    A cell's simulated win_prob is a binomial mean over n games, so it already
    differs from its own true value by roughly sqrt(2/pi) * se in absolute terms
    even if the two tables agree perfectly. Printing the observed difference
    without this alongside it invites reading Monte Carlo scatter as a real
    disagreement between the two methods.
    """
    return np.sqrt(2.0 / np.pi) * _sim_se(both)


def _fmt(series: pd.Series) -> str:
    if series.empty:
        return "no overlapping cells"
    return f"mean={series.mean():.4f}  median={series.median():.4f}  max={series.max():.4f}  p95={series.quantile(0.95):.4f}"


def _report_gap_impact(gaps: pd.DataFrame) -> None:
    """Say which coverage gaps the WP/LI engine would actually walk into.

    A gap at a state no game can reach is harmless on its own - but
    utils._wp_post_play does not only look up the state in front of it. Every
    time a play ends a half-inning it looks up (remaining - 1, 0, "000",
    -new_lead), which flips the sign of the lead. So a gap at
    (remaining, 0, "000", positive lead) is reached constantly, from the other
    dugout, and get_win_probability_interpolated answers it by clamping to the
    nearest lead it does have - which can be a completely different number and
    shows up as an enormous fake win probability swing in the leverage index.
    """
    handoff = gaps[(gaps["outs"] == 0) & (gaps["obc"] == "000")]
    if handoff.empty:
        print("  None of these gaps sit at (outs=0, obc='000'), so the half-inning")
        print("  handoff term in utils._wp_post_play never lands in one.")
        return
    print(f"\n  WARNING: {len(handoff)} gap(s) sit at (outs=0, obc='000'), the state")
    print("  utils._wp_post_play looks up whenever a play ends a half-inning:")
    for remaining, grp in handoff.groupby("remaining"):
        leads = sorted(grp["batting_lead"].tolist())
        print(f"    remaining={remaining}: batting_lead {leads[0]}..{leads[-1]} ({len(leads)} cells)")
    print("  These are queried with the lead sign flipped, so they are hit on ordinary")
    print("  plays even though no game reaches them from the batting side. Until they")
    print("  are backfilled, leverage computed from this table is wrong near those")
    print("  states - see the LI figures in the plan's manual Stage 3 step.")


# ── 1. win probability tables ─────────────────────────────────────────────────

def compare_wp_tables(cur: pd.DataFrame, sim: pd.DataFrame) -> pd.DataFrame:
    joined = cur.merge(
        sim, on=KEYS, how="outer", suffixes=("_cur", "_sim"), indicator=True
    )
    only_cur = joined[joined["_merge"] == "left_only"]
    only_sim = joined[joined["_merge"] == "right_only"]
    both = joined[joined["_merge"] == "both"].copy()
    if "n" in both.columns:
        both["n"] = both["n"].astype(int)
    both["abs_diff"] = (both["win_prob_sim"] - both["win_prob_cur"]).abs()
    both["signed_diff"] = both["win_prob_sim"] - both["win_prob_cur"]
    both["noise"] = _noise_floor(both)
    # How many of the simulated cell's own standard errors the current table
    # sits away from it. This, not raw magnitude, is where the two methods
    # genuinely disagree: a 0.14 gap on 86 games is scatter, a 0.05 gap on
    # 40,000 games is not.
    both["z_diff"] = both["abs_diff"] / _sim_se(both)

    print("=" * 78)
    print("1. WIN PROBABILITY TABLES")
    print("=" * 78)
    print(f"{CURRENT_CSV}:   {len(cur):,} cells")
    print(f"{SIMULATED_CSV}: {len(sim):,} cells")
    print(f"Overlapping:  {len(both):,}")
    print(f"Only in current:   {len(only_cur):,}")
    if len(only_cur):
        # Both tables now span batting_lead -18..18, so anything here is a real
        # coverage gap: a state the simulation never reached.
        print("  Coverage gaps - states the simulation never reached:")
        print(only_cur.groupby(["remaining"]).size().rename("cells").reset_index().to_string(index=False))
        _report_gap_impact(only_cur)
    print(f"Only in simulated: {len(only_sim):,}")
    if len(only_sim):
        print(only_sim.groupby(["remaining", "outs"]).size().to_string())

    if both.empty:
        return both

    print(f"\nAbsolute win_prob difference (all overlapping cells)")
    print(f"  {_fmt(both['abs_diff'])}")
    print(f"  simulated is higher in {int((both['signed_diff'] > 0).sum()):,} cells, "
          f"lower in {int((both['signed_diff'] < 0).sum()):,}")
    if both["noise"].notna().any():
        floor = float(both["noise"].mean())
        print(f"  Monte Carlo noise floor: {floor:.4f} mean |difference| is expected from the")
        print(f"  simulator's own sampling error alone, so read the mean above against that.")
        print(f"  Excess over noise: {both['abs_diff'].mean() - floor:+.4f}")

    # The stored domain runs to +/-18 but utils clamps queries to +/-10, so a
    # difference outside that band is real data with no path to the live app
    # until those clamp lines are widened as their own change. Splitting the
    # two keeps "what would change today" separate from "what is merely on
    # disk", since the outer band is where the tables disagree most.
    inner = both[both["batting_lead"].abs() <= QUERY_CLAMP]
    outer = both[both["batting_lead"].abs() > QUERY_CLAMP]
    print(f"\nSplit by utils' query clamp of +/-{QUERY_CLAMP}:")
    print(f"  within  ({len(inner):,} cells, what the app can actually read)  {_fmt(inner['abs_diff'])}")
    if len(outer):
        print(f"  beyond  ({len(outer):,} cells, stored but never queried today)  {_fmt(outer['abs_diff'])}")

    print("\nBy outs:")
    for outs, grp in both.groupby("outs"):
        print(f"  outs={outs}  {_fmt(grp['abs_diff'])}")

    print("\nBy obc:")
    for obc, grp in both.groupby("obc"):
        print(f"  {obc} ({utils.obc_display(obc):>6})  {_fmt(grp['abs_diff'])}")

    risp = both[both["obc"].isin(RISP)]
    loaded = both[both["obc"] == LOADED]
    print("\nLeverage-critical slices:")
    print(f"  RISP (runner on 2nd or 3rd)  {_fmt(risp['abs_diff'])}")
    print(f"  bases loaded                 {_fmt(loaded['abs_diff'])}")
    risp_2out = risp[risp["outs"] == 2]
    print(f"  RISP with 2 outs             {_fmt(risp_2out['abs_diff'])}")

    if "n" in both.columns:
        thin = both[both["n"] < LOW_N]
        thick = both[both["n"] >= LOW_N]
        print(f"\nBy simulated sample size:")
        print(f"  n <  {LOW_N} ({len(thin):,} cells)  {_fmt(thin['abs_diff'])}")
        print(f"  n >= {LOW_N} ({len(thick):,} cells)  {_fmt(thick['abs_diff'])}")

    _weighted_diff(both)

    has_n = "n" in both.columns
    cols = KEYS + ["win_prob_cur", "win_prob_sim", "signed_diff"] + (["n", "z_diff"] if has_n else [])
    if has_n:
        print("\nStrongest disagreements (ranked by standard errors, not raw size):")
        print(both.nlargest(15, "z_diff")[cols].round(4).to_string(index=False))
        beyond = int((both["z_diff"] >= 3.0).sum())
        print(f"\n{beyond:,} of {len(both):,} cells ({beyond / len(both):.1%}) differ by 3+ standard errors.")
        print("Pure sampling scatter would put roughly 0.3% of cells there.")
    print("\nLargest raw differences (well-sampled cells only):")
    ranked = both[both["n"] >= LOW_N] if has_n else both
    print(ranked.nlargest(15, "abs_diff")[cols].round(4).to_string(index=False))

    return both


def _weighted_diff(both: pd.DataFrame) -> None:
    """Weight each cell's difference by how often real play reaches that state."""
    if not os.path.exists(STATE_FREQ_CSV):
        print(f"\n(skipping frequency-weighted difference - {STATE_FREQ_CSV} not found)")
        return
    freq = pd.read_csv(STATE_FREQ_CSV, dtype={"obc": str})
    freq["obc"] = freq["obc"].str.strip().str.zfill(3)
    freq = freq[["remaining", "outs", "obc", "frequency"]]
    merged = both.merge(freq, on=["remaining", "outs", "obc"], how="left")
    merged["frequency"] = merged["frequency"].fillna(0.0)
    total_w = merged["frequency"].sum()
    if not total_w:
        print(f"\n(skipping frequency-weighted difference - no matching states in {STATE_FREQ_CSV})")
        return
    weighted = float((merged["abs_diff"] * merged["frequency"]).sum() / total_w)
    unweighted = float(merged["abs_diff"].mean())
    print(f"\nState-frequency-weighted mean |difference|: {weighted:.4f}")
    print(f"  (unweighted mean over the same cells:     {unweighted:.4f})")
    if merged["noise"].notna().any():
        weighted_floor = float((merged["noise"] * merged["frequency"]).sum() / total_w)
        print(f"  (weighted Monte Carlo noise floor:        {weighted_floor:.4f})")

    covered = merged[merged["frequency"] > 0]
    print("\nBiggest contributors to the weighted difference:")
    contrib = covered.assign(contribution=covered["abs_diff"] * covered["frequency"] / total_w)
    cols = KEYS + ["win_prob_cur", "win_prob_sim", "frequency", "contribution"]
    print(contrib.nlargest(10, "contribution")[cols].to_string(index=False))


def check_monotonicity(sim: pd.DataFrame, tol: float, z_tol: float) -> None:
    """Two orderings the table should respect regardless of how it was built.

    An inversion only means something if it is too big to be sampling scatter.
    Near the 0/1 asymptotes a pair of cells with thousands of games behind them
    can still invert by 0.0003; a pair with 40 games each can invert by 0.06.
    Neither is a bug. An inversion is reported as material only when it clears
    both an absolute size floor and a binomial noise band, which is what a real
    ordering error in the simulator would look like.
    """
    print("\n" + "-" * 78)
    print(f"Monotonicity spot-check on the simulated table")
    print(f"(material = inversion >= {tol} AND >= {z_tol} standard errors)")
    print("-" * 78)
    has_n = "n" in sim.columns

    lead_rows = []
    for (rem, outs, obc), grp in sim.groupby(["remaining", "outs", "obc"]):
        grp = grp.sort_values("batting_lead")
        wp = grp["win_prob"].to_numpy()
        ns = grp["n"].to_numpy() if has_n else np.full(len(grp), LOW_N)
        leads = grp["batting_lead"].to_numpy()
        for i in range(1, len(wp)):
            if wp[i] < wp[i - 1] - 1e-9:
                size = float(wp[i - 1] - wp[i])
                lead_rows.append({
                    "remaining": int(rem), "outs": int(outs), "obc": obc,
                    "from": int(leads[i - 1]), "to": int(leads[i]),
                    "wp_from": float(wp[i - 1]), "wp_to": float(wp[i]),
                    "size": size, "min_n": int(min(ns[i - 1], ns[i])),
                    "z": _z_score(float(wp[i - 1]), int(ns[i - 1]), float(wp[i]), int(ns[i]), size),
                })

    wide = sim.pivot_table(index=["remaining", "obc", "batting_lead"], columns="outs", values="win_prob")
    wide_n = (sim.pivot_table(index=["remaining", "obc", "batting_lead"], columns="outs", values="n")
              if has_n else None)
    outs_rows = []
    for a, b in ((0, 1), (1, 2)):
        if a not in wide.columns or b not in wide.columns:
            continue
        bad = wide[wide[b] > wide[a] + 1e-9]
        for key, row in bad.iterrows():
            rem, obc, lead = key
            n_a = int(wide_n.loc[key, a]) if wide_n is not None else LOW_N
            n_b = int(wide_n.loc[key, b]) if wide_n is not None else LOW_N
            size = float(row[b] - row[a])
            outs_rows.append({
                "remaining": int(rem), "obc": obc, "batting_lead": int(lead),
                "from": a, "to": b, "wp_from": float(row[a]), "wp_to": float(row[b]),
                "size": size, "min_n": min(n_a, n_b),
                "z": _z_score(float(row[a]), n_a, float(row[b]), n_b, size),
            })

    for label, rows in (("batting_lead (should be non-decreasing)", lead_rows),
                        ("outs (should be non-increasing)", outs_rows)):
        df = pd.DataFrame(rows)
        if df.empty:
            print(f"\n{label}: 0 inversions")
            continue
        material = df[(df["size"] >= tol) & (df["z"] >= z_tol)]
        print(f"\n{label}: {len(df)} inversion(s), of which {len(material)} material")
        print(f"  {int((df['size'] < tol).sum())} smaller than {tol}, "
              f"{int((df['z'] < z_tol).sum())} inside the noise band, "
              f"{int((df['min_n'] < LOW_N).sum())} touching an n<{LOW_N} cell")
        if len(material):
            print(material.sort_values("z", ascending=False).head(10).round(4).to_string(index=False))
        else:
            print("  Nothing survives both filters - all inversions are Monte Carlo scatter.")


# ── 2. result distributions ───────────────────────────────────────────────────

def compare_result_distributions(min_group: int, diff_threshold: float) -> None:
    print("\n" + "=" * 78)
    print("2. RESULT DISTRIBUTIONS - real play vs. result_ranges_re24.csv")
    print("=" * 78)
    if not os.path.exists(SITUATIONAL_CSV):
        print(f"{SITUATIONAL_CSV} not found - run compute_situational_result_frequencies.py first.")
        return

    theo = pd.read_csv(RANGES_CSV, dtype={"obc": str})
    theo["obc"] = theo["obc"].str.strip().str.zfill(3)
    theo = theo.rename(columns={"Result": "result"})
    theo["prob_theoretical"] = theo["Rng"] / RNG_DOMAIN
    theo = theo[["outs", "obc", "result", "prob_theoretical"]]

    obs = pd.read_csv(SITUATIONAL_CSV, dtype={"obc": str})
    obs["obc"] = obs["obc"].str.strip().str.zfill(3)
    obs = obs.rename(columns={"probability": "prob_observed"})
    group_n = obs.groupby(["outs", "obc"])["count"].sum().rename("group_n")

    joined = theo.merge(obs[["outs", "obc", "result", "count", "prob_observed"]],
                        on=["outs", "obc", "result"], how="outer")
    joined = joined.merge(group_n, on=["outs", "obc"], how="left")
    joined["prob_theoretical"] = joined["prob_theoretical"].fillna(0.0)
    joined["prob_observed"] = joined["prob_observed"].fillna(0.0)
    joined["count"] = joined["count"].fillna(0).astype(int)
    joined["group_n"] = joined["group_n"].fillna(0).astype(int)
    joined["abs_diff"] = (joined["prob_observed"] - joined["prob_theoretical"]).abs()

    print(f"Situations compared: {joined[['outs', 'obc']].drop_duplicates().shape[0]} of 24")
    print(f"Overall per-result |probability difference|: {_fmt(joined['abs_diff'])}")

    # result_ranges_re24.csv models normal-swing at-bats only. Steals, caught
    # stealing, bunts, IBB and balks are absent from it by design, but the BRC
    # lookup knows them so they survive into the observed frequencies. Split
    # them out: their share is the rate of off-mechanic events in real play,
    # not a disagreement about the diff-band distribution itself.
    off_mechanic = joined[joined["prob_theoretical"] == 0.0]
    off_share = float(off_mechanic["prob_observed"].sum() / max(joined["prob_observed"].sum(), 1e-9))
    print(f"\nResults observed but absent from {RANGES_CSV} (steals, bunts, IBB and friends):")
    print(f"  {off_mechanic['result'].nunique()} distinct result(s), "
          f"{off_share:.2%} of observed plate appearances")
    if len(off_mechanic):
        by_result = (off_mechanic.groupby("result")["count"].sum()
                     .sort_values(ascending=False).head(12))
        print(f"  {', '.join(f'{r} ({c:,})' for r, c in by_result.items())}")
    print("  These are off-mechanic events the simulator does not model, not evidence")
    print("  that the diff-band distribution has drifted.")

    print("\nTotal variation distance per situation (0 = identical, 1 = disjoint):")
    print("  tvd = all results; tvd_modeled = excluding the off-mechanic results above")
    modeled = joined[joined["prob_theoretical"] > 0.0]
    tvd = (joined.groupby(["outs", "obc"])
           .agg(tvd=("abs_diff", lambda s: s.sum() / 2), plays=("group_n", "first"))
           .reset_index())
    tvd_modeled = (modeled.groupby(["outs", "obc"])["abs_diff"].sum() / 2).rename("tvd_modeled")
    tvd = tvd.merge(tvd_modeled, on=["outs", "obc"], how="left")
    tvd["trustworthy"] = tvd["plays"] >= min_group
    print(tvd.sort_values("tvd", ascending=False).round(4).to_string(index=False))

    flagged = joined[(joined["abs_diff"] >= diff_threshold) & (joined["group_n"] >= min_group)]
    print(f"\nDivergences >= {diff_threshold:.3f} in situations with >= {min_group:,} plays: {len(flagged)}")
    if len(flagged):
        cols = ["outs", "obc", "result", "prob_theoretical", "prob_observed", "abs_diff", "count", "group_n"]
        print(flagged.sort_values("abs_diff", ascending=False)[cols].to_string(index=False))
    thin_groups = int((tvd["plays"] < min_group).sum())
    if thin_groups:
        print(f"\n{thin_groups} situation(s) have fewer than {min_group:,} plays - "
              f"not enough games yet to judge those cells.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", default=CURRENT_CSV)
    ap.add_argument("--simulated", default=SIMULATED_CSV)
    ap.add_argument("--out", default=None, help="optional per-cell comparison CSV dump")
    ap.add_argument("--min-group", type=int, default=200,
                    help="min plays in a situation before a result divergence is flagged (default 200)")
    ap.add_argument("--result-diff", type=float, default=0.02,
                    help="probability difference that counts as a divergence (default 0.02)")
    ap.add_argument("--mono-tol", type=float, default=0.01,
                    help="win_prob inversion size that counts as material (default 0.01)")
    ap.add_argument("--mono-z", type=float, default=3.0,
                    help="standard errors an inversion must clear to count as material (default 3)")
    args = ap.parse_args()

    for path in (args.current, args.simulated):
        if not os.path.exists(path):
            raise SystemExit(f"{path} not found - run simulate_win_probability.py first.")

    cur = _read_wp(args.current)
    sim = _read_wp(args.simulated)

    both = compare_wp_tables(cur, sim)
    check_monotonicity(sim, args.mono_tol, args.mono_z)
    compare_result_distributions(args.min_group, args.result_diff)

    if args.out:
        both.drop(columns=["_merge"]).to_csv(args.out, index=False)
        print(f"\nWrote per-cell comparison to {args.out}")


if __name__ == "__main__":
    main()

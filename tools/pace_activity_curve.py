"""Time-of-day activity curve for pace.py's active-time weights.

Every play row carries the Central wall-clock time its result was logged (the plays sheet's Timestamp). Only S11 onward
have one: S11-S12 come from the archive Plays tab (cached in tools/probe_output), S13 from the local plays copy. More results in a
slice of the day means more players around then, so that slice's time counts for more. This bins the results into quarter
hours, smooths the histogram around the clock (circular Gaussian) so it isn't jagged, and scales it so the busiest quarter
hour is 1.0. Paste the printed ACTIVE_CURVE into pace.py.

  python -I tools/pace_activity_curve.py            # S13 from tools/probe_output/plays_raw.pkl (tools/local_plays.py fetch)
  python -I tools/pace_activity_curve.py --refresh  # re-download the archive sheet first
  python -I tools/pace_activity_curve.py --plot out.png   # raw quarter-hour counts with the smoothed weight over them
"""
from __future__ import annotations

import pathlib
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHEET_ID = "1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg"
SHEET_GID = "1141271229"
SHEET_CACHE = ROOT / "tools" / "probe_output" / "plays_sheet_ts.pkl"
BINS = 96          # quarter hours
SIGMA_H = 1.5      # smoothing width in hours


def curve(minutes_of_day: np.ndarray) -> np.ndarray:
    counts = np.bincount((minutes_of_day // (1440 // BINS)).astype(int), minlength=BINS).astype(float)
    k = np.arange(BINS)
    d = np.minimum(k, BINS - k) * (24 / BINS)                       # circular distance in hours
    kernel = np.exp(-0.5 * (d / SIGMA_H) ** 2)
    smooth = np.array([(counts * np.roll(kernel, i)).sum() for i in range(BINS)])
    return smooth / smooth.max()


def _timestamps(refresh: bool) -> pd.Series:
    """Result times (Central wall clock) for every season that logged them. Sheet cells are 'M/D/YYYY H:MM', some with
    seconds; 0 = not recorded."""
    if refresh or not SHEET_CACHE.exists():
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&gid={SHEET_GID}"
        pd.read_csv(url, dtype=str).to_pickle(SHEET_CACHE)
    sheet = pd.read_pickle(SHEET_CACHE)
    sheet_ts = pd.to_datetime(sheet["Timestamp*"].where(sheet["Timestamp*"] != "0"), format="mixed", errors="coerce")
    plays = pd.DataFrame(pickle.load(open(ROOT / "tools" / "probe_output" / "plays_raw.pkl", "rb"))["plays"])
    s13 = pd.to_datetime(plays.loc[plays["season"] == 13, "timestamp"], format="mixed", errors="coerce")
    sheet_s13 = pd.to_numeric(sheet["Season"], errors="coerce") == 13
    return pd.concat([sheet_ts[~sheet_s13], s13]).dropna()   # the live copy is the fuller S13 source


def plot(minutes: np.ndarray, c: np.ndarray, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    counts = np.bincount((minutes // (1440 // BINS)).astype(int), minlength=BINS)
    x = np.arange(BINS) / 4
    fig, ax = plt.subplots(figsize=(11, 5), facecolor="#212227")
    ax.set_facecolor("#212227")
    ax.bar(x, counts, width=0.25, align="edge", color="#5a6b8c", label="Raw results per quarter hour")
    ax2 = ax.twinx()
    ax2.plot(np.append(x, 24) + 0.125, np.append(c, c[0]), color="#ffb347", lw=2.5, label="Smoothed weight (peak = 1.0)")
    ax2.set_ylim(0, 1.05)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xticklabels([f"{h % 12 or 12}{'a' if h % 24 < 12 else 'p'}" for h in range(0, 25, 2)])
    ax.set_xlabel("Central time of day", color="#ccc")
    ax.set_ylabel("Results logged", color="#ccc")
    ax2.set_ylabel("Active-time weight", color="#ffb347")
    for a in (ax, ax2):
        a.tick_params(colors="#ccc")
        for s in a.spines.values():
            s.set_color("#555")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", facecolor="#2a2b31", edgecolor="#555", labelcolor="#ddd")
    ax.set_title(f"When results get logged ({len(minutes):,} results, S11-S13)", color="#eee", loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=fig.get_facecolor())


def main() -> None:
    ts = _timestamps("--refresh" in sys.argv)
    print(f"# {len(ts)} results, {ts.min():%Y-%m-%d} to {ts.max():%Y-%m-%d}, sigma {SIGMA_H}h")
    minutes = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
    c = curve(minutes)
    if "--plot" in sys.argv:
        plot(minutes, c, sys.argv[sys.argv.index("--plot") + 1])
        return
    print("ACTIVE_CURVE = (")
    for i in range(0, BINS, 8):
        print("    " + ", ".join(f"{v:.3f}" for v in c[i:i + 8]) + ",")
    print(")")
    print("# hourly means:", " ".join(f"{h}:{c[h * 4:h * 4 + 4].mean():.2f}" for h in range(24)))


if __name__ == "__main__":
    main()

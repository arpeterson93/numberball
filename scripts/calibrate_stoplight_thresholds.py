"""Offline calibration for the Swing Suggestions Stoplight per-pitch thresholds.

Replays every pitcher's career chronologically through the exact same
point-in-time scoring pipeline the app uses (utils._recency_indications /
utils._surprisal_walk), one pitcher x one indication at a time, and reports
where the 33rd/67th percentile cut points fall so SCOUT_PP_THRESHOLDS can be
set to put ~1/3 of historical pitches in each stoplight zone.

Usage:
    python scripts/calibrate_stoplight_thresholds.py [csv_path]

csv_path defaults to plays_from_pitchers_200+_bf.csv in the repo root.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import _recency_indications, _surprisal_walk, _classify_pp, SCOUT_PP_THRESHOLDS

# Current page defaults for the pitcher-side stoplight (hz_init_bucket_p / dd_bucket_p).
HZ_BKT = 200
DD_BKT = 100


def load_plays(csv_path: str) -> pd.DataFrame:
    # obc is a zero-padded TEXT code ("000".."111") in the DB - without an
    # explicit dtype, pandas infers it as int64 and silently drops the
    # leading zeros, which corrupts the Base state indication.
    df = pd.read_csv(csv_path, low_memory=False, dtype={"obc": str})
    before = len(df)

    # Mirror utils.enrich_df's steal exclusion - steals never reach the
    # pitch-sequence scoring in the live app.
    df = df[df["play_type"].str.lower() != "steal"]

    # A handful of rows have no game_id (orphaned from a game link); the
    # scoring pipeline sorts/groups by game_id so these can't be placed.
    n_no_game = df["game_id"].isna().sum()
    df = df[df["game_id"].notna()]

    # Mirror _recency_frame's own value_col.notna() filter.
    n_no_pitch = df["pitch"].isna().sum()
    df = df[df["pitch"].notna()]

    print(f"Loaded {before} rows -> {len(df)} after dropping steals, "
          f"{n_no_game} row(s) with no game_id, and {n_no_pitch} row(s) with no pitch.")
    print(f"{df['pitcher_id'].nunique()} distinct pitcher_id careers.\n")
    return df


def score_all(df: pd.DataFrame) -> pd.DataFrame:
    """Walk every pitcher's career through every indication; return one row
    per scored pitch: pitcher_id, indication, score."""
    records = []
    for pid, sub in df.groupby("pitcher_id"):
        sw = sub.sort_values(["game_id", "id"]).reset_index(drop=True)
        ind = _recency_indications(sw, "pitch", HZ_BKT, DD_BKT)
        for sig, (ctx, out, k) in ind.items():
            scores = _surprisal_walk(ctx, out, k)
            for s in scores:
                if s is not None:
                    records.append((pid, sig, s))
    return pd.DataFrame(records, columns=["pitcher_id", "indication", "score"])


def report(scores: pd.DataFrame) -> None:
    cur_min = SCOUT_PP_THRESHOLDS["scouting_min"]
    cur_max = SCOUT_PP_THRESHOLDS["anti_max"]

    def _row(label: str, s: pd.Series) -> dict:
        n = len(s)
        p33, p67 = np.percentile(s, [33.333, 66.667])
        zero_frac = float((s == 0.0).mean())
        cur_anti = float((s <= cur_max).mean())
        cur_scout = float((s >= cur_min).mean())
        new_anti = float((s <= p33).mean())
        new_scout = float((s >= p67).mean())
        return {
            "indication": label, "n": n,
            "anti_max(p33)": round(p33, 4), "scouting_min(p67)": round(p67, 4),
            "%=0": round(zero_frac * 100, 1),
            "cur anti/neu/scout %": f"{cur_anti*100:.1f}/{(1-cur_anti-cur_scout)*100:.1f}/{cur_scout*100:.1f}",
            "new anti/neu/scout %": f"{new_anti*100:.1f}/{(1-new_anti-new_scout)*100:.1f}/{new_scout*100:.1f}",
        }

    rows = [_row("ALL (pooled)", scores["score"])]
    for sig, grp in scores.groupby("indication"):
        rows.append(_row(sig, grp["score"]))

    out = pd.DataFrame(rows).set_index("indication")
    pd.set_option("display.width", 160)
    print(f"Current thresholds: scouting_min={cur_min}, anti_max={cur_max}\n")
    print(out.to_string())


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "plays_from_pitchers_200+_bf.csv"
    plays = load_plays(csv_path)
    scored = score_all(plays)
    report(scored)

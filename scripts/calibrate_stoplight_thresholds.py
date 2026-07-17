"""Offline calibration for the Swing Suggestions Stoplight per-pitch thresholds.

Replays every pitcher's career chronologically through the exact same
point-in-time scoring pipeline the app uses (utils._recency_indications /
utils._surprisal_walk), one pitcher x one indication at a time, and reports
where the 33rd/67th percentile cut points fall so SCOUT_PP_THRESHOLDS can be
set to put ~1/3 of historical pitches in each stoplight zone.

Usage:
    python scripts/calibrate_stoplight_thresholds.py [csv_path]
    python scripts/calibrate_stoplight_thresholds.py [csv_path] --obp

The default (sequence) mode reports the 33rd/67th percentile cut points for the
eleven sequence indications. --obp mode instead replays the three OBP-recency
signals through obp_recency_walk with a synthetic OBR band (widths X in
{50, 100, 150}) and neutral relevance weights, gridded over the real slider bucket
widths per kind, reporting per-signal-and-width score percentiles and the
scouting/neutral/anti shares under the current shared thresholds.

csv_path defaults to plays_from_pitchers_200+_bf.csv in the repo root.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import (_recency_indications, _surprisal_walk, _classify_pp,
                   SCOUT_PP_THRESHOLDS, _recency_frame, obp_recency_walk)

# OBP-mode settings: synthetic OBR band widths and the recent-window default.
OBP_BAND_WIDTHS = [50, 100, 150]
OBP_WINDOW_N = 20
# Neutral relevance weights (recency 50, result 50, state 0 -> uniform).
OBP_NEUTRAL_REL = (50, 50, 0, 33, 33, 33, False)
OBP_SIGNALS = [("OBP recent pitch", "pitch"),
               ("OBP recent Δ", "delta"),
               ("OBP recent Δ²", "delta2")]
# Real slider option values per kind (pitch divides 1000, delta/delta2 divide 500).
OBP_KIND_WIDTHS = {"pitch": [100, 200], "delta": [50, 100], "delta2": [50, 100]}

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


def score_all_obp(df: pd.DataFrame, band: int) -> pd.DataFrame:
    """Walk every pitcher's career through the three OBP signals for one synthetic
    OBR band width, gridded over the real slider bucket widths per kind; return one
    row per scored pitch with score and k."""
    ranges = [("1B", 0, band)]
    records = []
    for pid, sub in df.groupby("pitcher_id"):
        sw = _recency_frame(sub, "pitch")
        if sw is None:
            continue
        for sig, kind in OBP_SIGNALS:
            for width in OBP_KIND_WIDTHS[kind]:
                walk = obp_recency_walk(sw, "pitch", kind, OBP_WINDOW_N, ranges,
                                        OBP_NEUTRAL_REL, width)
                for r in walk:
                    if r is not None:
                        records.append((pid, f"{sig} w={width}", float(r["score"]), int(r["k"])))
    return pd.DataFrame(records, columns=["pitcher_id", "indication", "score", "k"])


def report_obp(scores: pd.DataFrame, band: int) -> None:
    cur_min = SCOUT_PP_THRESHOLDS["scouting_min"]
    cur_max = SCOUT_PP_THRESHOLDS["anti_max"]

    def _row(label: str, grp: pd.DataFrame) -> dict:
        s = grp["score"]
        n = len(s)
        p05, p33, p50, p67, p95 = np.percentile(s, [5, 33.333, 50, 66.667, 95])
        cur_anti = float((s <= cur_max).mean())
        cur_scout = float((s >= cur_min).mean())
        # lights vary across pitchers: fraction of pitcher careers whose plurality
        # class is not neutral (a rough "the light actually moves" check).
        def _plurality(sub_s):
            cls = pd.Series([_classify_pp(v) for v in sub_s])
            return cls.value_counts().idxmax() if len(cls) else "neutral"
        per_pid = grp.groupby("pitcher_id")["score"].apply(_plurality)
        moved = float((per_pid != "neutral").mean()) if len(per_pid) else 0.0
        return {
            "indication": label, "n": n,
            "p5": round(p05, 3), "p33": round(p33, 3), "p50": round(p50, 3),
            "p67": round(p67, 3), "p95": round(p95, 3),
            "cur anti/neu/scout %": f"{cur_anti*100:.1f}/{(1-cur_anti-cur_scout)*100:.1f}/{cur_scout*100:.1f}",
            "pitchers non-neutral %": round(moved * 100, 1),
        }

    rows = [_row("ALL (pooled)", scores)]
    for sig, grp in scores.groupby("indication"):
        rows.append(_row(sig, grp))
    out = pd.DataFrame(rows).set_index("indication")
    pd.set_option("display.width", 200)
    print(f"\n=== OBP mode, OBR band width X={band} (obr_max={band}) ===")
    print(f"Current thresholds: scouting_min={cur_min}, anti_max={cur_max}\n")
    print(out.to_string())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    obp_mode = "--obp" in sys.argv
    csv_path = args[0] if args else "plays_from_pitchers_200+_bf.csv"
    plays = load_plays(csv_path)
    if obp_mode:
        for band in OBP_BAND_WIDTHS:
            report_obp(score_all_obp(plays, band), band)
    else:
        scored = score_all(plays)
        report(scored)

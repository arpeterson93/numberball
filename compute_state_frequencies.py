"""
Generate state_frequencies.csv from the Supabase plays table.

Counts how often each (remaining, outs, obc) game state appears across all plays.
Used by utils._compute_avg_wp_swing() to frequency-weight the LI denominator so
that common early-game states contribute more than rare late-game states.

Run from the project root:
    python compute_state_frequencies.py
"""
import pandas as pd
import database
import utils


def main() -> None:
    print("Fetching plays from Supabase...")
    plays = database.get_all_plays()
    df = pd.DataFrame(plays)

    if df.empty:
        print("No plays found.")
        return

    required = {"inning", "half", "outs", "obc"}
    if not required.issubset(df.columns):
        print(f"Missing columns: {required - set(df.columns)}")
        return

    records = []
    for _, row in df.iterrows():
        try:
            inning = int(row["inning"] or 1)
            half   = str(row["half"] or "top").lower()
            outs   = int(row["outs"] or 0)
            obc_raw = row["obc"]
            if isinstance(obc_raw, (int, float)) and not pd.isna(obc_raw):
                obc = utils._BRC_TO_OBC.get(int(obc_raw), "000")
            else:
                obc = str(obc_raw).zfill(3) if obc_raw else "000"
            rem = utils.remaining_half_innings(inning, half)
            records.append((rem, outs, obc))
        except (ValueError, TypeError):
            continue

    counts = (
        pd.Series(records)
        .value_counts()
        .rename_axis("state")
        .reset_index(name="count")
    )
    counts[["remaining", "outs", "obc"]] = pd.DataFrame(counts["state"].tolist(), index=counts.index)
    counts = counts.drop(columns="state")
    total = counts["count"].sum()
    counts["frequency"] = counts["count"] / total
    counts = counts[["remaining", "outs", "obc", "count", "frequency"]].sort_values(
        ["remaining", "outs", "obc"], ascending=[False, True, True]
    )

    out_path = "state_frequencies.csv"
    counts.to_csv(out_path, index=False)
    print(f"Saved {len(counts)} states ({total:,} plays) to {out_path}")
    print(counts.head(20).to_string(index=False))


if __name__ == "__main__":
    main()

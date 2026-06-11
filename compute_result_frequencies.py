"""
Generate result_frequencies.csv from the Supabase plays table.

Run from the project root:
    python compute_result_frequencies.py

Only includes results that appear in import_BRC.csv (i.e. results the WP/LI
engine knows how to advance runners for). Unknown or rare results are dropped
so the probabilities remain tied to what the leverage calculator can model.
"""
import pandas as pd
import database
import utils  # loads import_BRC.csv into _BRC_RUN_LOOKUP

def main() -> None:
    print("Fetching plays from Supabase...")
    plays = database.get_all_plays()
    df    = pd.DataFrame(plays)

    if df.empty or "result" not in df.columns:
        print("No plays found.")
        return

    # Only keep results the BRC lookup can handle
    known = set(r for (r, _, _) in utils._BRC_RUN_LOOKUP)
    counts = (
        df["result"]
        .dropna()
        .astype(str)
        .str.strip()
        .loc[lambda s: s.isin(known)]
        .value_counts()
        .rename_axis("result")
        .reset_index(name="count")
    )

    total = counts["count"].sum()
    counts["probability"] = counts["count"] / total

    out_path = "result_frequencies.csv"
    counts.to_csv(out_path, index=False)
    print(f"Saved {len(counts)} results ({total:,} plays) to {out_path}")
    print(counts.to_string(index=False))

if __name__ == "__main__":
    main()

"""
One-time migration: rewrite import_BRC.csv's ThrowOrder* columns from the old
digit-only alphabet (1=1B, 2=2B, 3=3B, 4=HOME) to the new cutoff-capable
alphabet docs/js/app.js's parseThrowOrder now expects (h/f/s/t for
HOME/1B/2B/3B base legs; 1-9 for position/cutoff legs - unused by any
existing cell today, since no cell has ever encoded a cutoff man, but the
new alphabet reserves them going forward).

Only digits 1-4 are remapped; every other character (comma, dash, space) is
left exactly as-is - the new parser accepts the same three separators.
Every cell is verified to contain ONLY digits 1-4 and separators before
being touched; migration aborts (no write) if anything else is found,
rather than silently mis-converting an unexpected character.

Idempotent: running this twice is a no-op the second time (the new
alphabet's own letters aren't touched by the digit->letter map, and every
converted cell is checked to be free of any remaining 1-4 digit before
declaring success).

Run once from the repo root:
    python tools/migrate_throw_order.py
"""
from __future__ import annotations

import csv
import pathlib
import re
import sys

CSV_PATH = pathlib.Path("import_BRC.csv")

THROW_ORDER_COLUMNS = [
    "ThrowOrder",
    "ThrowOrder_P", "ThrowOrder_C", "ThrowOrder_1B", "ThrowOrder_2B",
    "ThrowOrder_SS", "ThrowOrder_3B", "ThrowOrder_OF",
    "ThrowOrder_LF", "ThrowOrder_CF", "ThrowOrder_RF",
]

DIGIT_TO_LETTER = {"1": "f", "2": "s", "3": "t", "4": "h"}
VALID_CELL_RE = re.compile(r"^[1234,\s-]*$")
# A cell that's ALREADY been migrated (or was hand-authored under the new
# alphabet) - letters only, no stray digits - is left untouched, making a
# second run a no-op.
NEW_ALPHABET_RE = re.compile(r"^[hfstHFST1-9,\s-]*$")


def migrate_cell(raw: str) -> str:
    return "".join(DIGIT_TO_LETTER.get(ch, ch) for ch in raw)


def main() -> None:
    # newline="" + explicit CRLF terminator on write (matching the file's own
    # existing convention) - a naive splitlines()/default-writer round-trip
    # would silently flip every line to LF-only, turning this into a
    # whole-file rewrite in git diff instead of the intended cells-only one.
    with CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys()) if rows else []

    before_counts = {c: 0 for c in THROW_ORDER_COLUMNS}
    converted_counts = {c: 0 for c in THROW_ORDER_COLUMNS}
    already_migrated = {c: 0 for c in THROW_ORDER_COLUMNS}
    bad_cells: list[tuple[str, str, str]] = []

    for row in rows:
        for col in THROW_ORDER_COLUMNS:
            raw = row.get(col, "")
            if not raw.strip():
                continue
            before_counts[col] += 1
            if NEW_ALPHABET_RE.match(raw) and not any(d in raw for d in "1234"):
                already_migrated[col] += 1
                continue
            if not VALID_CELL_RE.match(raw):
                bad_cells.append((row.get("Situation", "?"), col, raw))
                continue
            row[col] = migrate_cell(raw)
            converted_counts[col] += 1

    total_before = sum(before_counts.values())
    print(f"Non-empty ThrowOrder* cells found: {total_before}")
    for col in THROW_ORDER_COLUMNS:
        print(f"  {col}: {before_counts[col]} present, "
              f"{converted_counts[col]} converted, {already_migrated[col]} already migrated")

    if bad_cells:
        print(f"\nABORTING - {len(bad_cells)} cell(s) contain characters outside "
              f"the old digit alphabet (1-4, comma, dash, space):")
        for situation, col, raw in bad_cells[:20]:
            print(f"  {situation} / {col}: {raw!r}")
        sys.exit(1)

    expected_total = 997
    if total_before != expected_total:
        print(f"\nWARNING: expected {expected_total} non-empty cells (probe 0.7's own "
              f"count) but found {total_before} - the CSV has changed since that probe. "
              f"Proceeding anyway since every cell validated cleanly, but double-check "
              f"this wasn't an unexpected data change.")

    total_converted = sum(converted_counts.values())
    if total_converted == 0:
        print("\nNothing to convert - already migrated. No write.")
        return

    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nConverted {total_converted} cell(s). {CSV_PATH} written in place.")


if __name__ == "__main__":
    main()

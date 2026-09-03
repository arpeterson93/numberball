"""
One-off probe: how many infield singles (archetype "infield_single") does
the reconciliation pipeline currently resolve to flight.fielder === "P",
across every season's full corpus? Answers Alex's question before deciding
whether to widen the existing PITCHER_MIDDLE_EV_MAX_MPH-style reassignment
to cover infield singles too.

Run from the repo root (needs Playwright + Chromium):
    python tools/pitcher_infield_single_probe.py
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()

PROBE_JS = """(a) => {
    KMFlight.setProbeFlightTables(a.tables);
    var rows = [];
    var errors = [];
    a.plays.forEach(function (m) {
        try {
            var flight = KMFlight.resolvePlayFlight(m);
            if (!flight || flight.archetype !== "infield_single") return;
            rows.push({
                moment_id: m.moment_id, result: m.result, fielder: flight.fielder,
                hand: m.batter_hand, ev: flight.ev, angle: flight.angle,
            });
        } catch (e) {
            errors.push((m.moment_id || "?") + ": " + e.message);
        }
    });
    return { rows: rows, errors: errors };
}"""


def load_seasons() -> list[tuple[str, list[dict], dict]]:
    seasons = []
    for season_dir in sorted(pathlib.Path("docs/data").glob("s*")):
        km_fp = season_dir / "key_moments.json"
        meta_fp = season_dir / "meta.json"
        if not km_fp.exists() or not meta_fp.exists():
            continue
        moments = json.loads(km_fp.read_text(encoding="utf-8"))
        tables = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]
        seasons.append((season_dir.name, moments, tables))
    return seasons


def main() -> None:
    seasons = load_seasons()
    total_moments = sum(len(m) for _, m, _ in seasons)
    print(f"Loaded {len(seasons)} season(s), {total_moments} total moments.")

    all_rows: list[dict] = []
    all_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_URL)
        page.wait_for_function("window.KMFlight != null")

        for season_name, moments, tables in seasons:
            result = page.evaluate(PROBE_JS, {"plays": moments, "tables": tables})
            for row in result["rows"]:
                row["season"] = season_name
                row["moment_id"] = f"{season_name}:{row['moment_id']}"
                all_rows.append(row)
            all_errors.extend(f"{season_name}:{e}" for e in result["errors"])
        browser.close()

    print(f"\n{len(all_rows)} total infield_single-archetype plays across the corpus.")
    if all_errors:
        print(f"[{len(all_errors)}] errors during probing (first 10):")
        for e in all_errors[:10]:
            print(f"  - {e}")

    fielder_counts = Counter(r["fielder"] for r in all_rows)
    print("\nfielder breakdown:")
    for pos, n in fielder_counts.most_common():
        print(f"  {pos}: {n} ({100.0 * n / len(all_rows):.1f}%)")

    pitcher_rows = [r for r in all_rows if r["fielder"] == "P"]
    print(f"\n{len(pitcher_rows)} infield singles resolve to fielder=P. By season:")
    by_season = Counter(r["season"] for r in pitcher_rows)
    for season, n in sorted(by_season.items()):
        print(f"  {season}: {n}")

    print("\nSample P rows (up to 15):")
    for r in pitcher_rows[:15]:
        print(f"  {r['moment_id']} result={r['result']} hand={r['hand']} "
              f"ev={r['ev']:.1f} angle={r['angle']:.1f}")


if __name__ == "__main__":
    main()

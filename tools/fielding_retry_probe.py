"""
Validation sweep for resolvePlayFlightReconciled (docs/js/app.js) - the fast-
charge/anchor-retry prototype. For every multi-out ground-archetype play in
the corpus where the honest reconciler already came up short (a throw-
schedule "unresolved" residual, or reconcileCoverage's own coverCompress
backstop), this measures shortfallBefore vs shortfallAfter and reports how
much of that shortfall the retry actually recovers in aggregate - not just
on the one or two plays used to build it.

Run from the repo root (needs Playwright + Chromium):
    python tools/fielding_retry_probe.py [--out tools/probe_output]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()
SITE_BASE = "https://mlngameday.com/"

PROBE_JS = """(a) => {
    KMFlight.setProbeFlightTables(a.tables);
    var rows = [];
    var errors = [];
    a.plays.forEach(function (m) {
        try {
            var r = KMFlight.resolvePlayFlightReconciled(m);
            if (r.shortfallBefore == null || r.shortfallBefore < 1) return;
            rows.push({
                moment_id: m.moment_id, result: m.result, throw_order: m.throw_order || "",
                game_code: m.game_code, session_number: m.session_number, inning: m.inning, half: m.half,
                off_team_abbr: m.off_team_abbr, def_team_abbr: m.def_team_abbr,
                batter_name: m.batter_name, play_num: m.play_num,
                fielder: r.flight.fielder, retried: r.retried,
                shortfallBefore: Math.round(r.shortfallBefore), shortfallAfter: Math.round(r.shortfallAfter),
            });
        } catch (e) {
            errors.push((m.moment_id || "?") + ": " + e.message);
        }
    });
    return { rows: rows, errors: errors };
}"""


def load_seasons() -> list[tuple[str, list[dict], dict]]:
    seasons: list[tuple[str, list[dict], dict]] = []
    for season_dir in sorted(pathlib.Path("docs/data").glob("s*")):
        km_fp = season_dir / "key_moments.json"
        meta_fp = season_dir / "meta.json"
        if not km_fp.exists() or not meta_fp.exists():
            continue
        moments = json.loads(km_fp.read_text(encoding="utf-8"))
        tables = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]
        seasons.append((season_dir.name, moments, tables))
    return seasons


def deep_link(game_code: str, play_num: int) -> str:
    return f"{SITE_BASE}?game={game_code}&play={play_num % 1000}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/probe_output", help="output dir for CSVs")
    args = ap.parse_args()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    seasons = load_seasons()
    print(f"Loaded {len(seasons)} season(s).")

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
                row["link"] = deep_link(row["game_code"], row["play_num"])
                all_rows.append(row)
            all_errors.extend(f"{season_name}:{e}" for e in result["errors"])
        browser.close()

    print(f"Found {len(all_rows)} plays with a real shortfall (unresolved throw or coverCompress).")
    if all_errors:
        print(f"[{len(all_errors)}] moments raised exceptions (first 10):")
        for e in all_errors[:10]:
            print(f"  - {e}")

    if all_rows:
        fieldnames = list(all_rows[0].keys())
        with (out_dir / "fielding_retry_results.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(all_rows)

        total_before = sum(r["shortfallBefore"] for r in all_rows)
        total_after = sum(r["shortfallAfter"] for r in all_rows)
        retried_count = sum(1 for r in all_rows if r["retried"])
        print(f"\nWrote {len(all_rows)} rows to {out_dir}/fielding_retry_results.csv")
        print(f"Retry actually improved: {retried_count}/{len(all_rows)} plays")
        print(f"Total shortfall before: {total_before}ms, after: {total_after}ms "
              f"({100.0 * (total_before - total_after) / total_before:.1f}% recovered)")
        recovered = [r["shortfallBefore"] - r["shortfallAfter"] for r in all_rows if r["retried"]]
        if recovered:
            print(f"Among plays it DID help: median recovery {sorted(recovered)[len(recovered)//2]}ms, "
                  f"max {max(recovered)}ms")
    else:
        print("No shortfall plays found - nothing to validate.")


if __name__ == "__main__":
    main()

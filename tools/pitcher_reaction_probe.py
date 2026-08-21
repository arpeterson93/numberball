"""
Task 6 validation probe (round-1 Stage 5c, fact 12): sweep the real play
corpus's grounder-archetype plays over a handful of candidate
PITCHER_CHARGE_REACTION_S values and report how the P-vs-3B (and P-vs-
everyone) split shifts as the pitcher's own charge-race reaction lengthens.

Answers the practical question directly - "does raising P's reaction move
real plays from P to 3B" - rather than assuming a specific angle range up
front (the plan's own "angles ~50-77" note wasn't run against real data).

Run once from the repo root:
    python tools/pitcher_reaction_probe.py
Park in tools/ or delete once Alex has signed off on the recommendation
(per the plan) - not part of the regular test suite.
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()
CANDIDATE_REACTIONS = [0.15, 0.30, 0.45, 0.60]


def load_real_plays() -> list[tuple[list[dict], dict]]:
    """One (plays, tables) pair per season found - current season plus every
    archived s01-s12 - since flight.meta's stations/bands can differ by
    season and each season's plays must be read against its OWN tables."""
    seasons: list[tuple[list[dict], dict]] = []
    current_plays: list[dict] = []
    for n in (1, 2, 3, 4):
        fp = pathlib.Path(f"docs/data/plays_{n:02d}.json")
        if fp.exists():
            current_plays.extend(json.loads(fp.read_text(encoding="utf-8")))
    current_meta_fp = pathlib.Path("docs/data/meta.json")
    if current_plays and current_meta_fp.exists():
        seasons.append((current_plays, json.loads(current_meta_fp.read_text(encoding="utf-8"))["flight"]))

    for season_dir in sorted(pathlib.Path("docs/data").glob("s*")):
        meta_fp = season_dir / "meta.json"
        if not meta_fp.exists():
            continue
        plays: list[dict] = []
        for fp in sorted(season_dir.glob("plays_*.json")):
            plays.extend(json.loads(fp.read_text(encoding="utf-8")))
        if plays:
            seasons.append((plays, json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]))
    return seasons


def main() -> None:
    seasons = load_real_plays()
    total_plays = sum(len(plays) for plays, _ in seasons)
    print(f"Loaded {len(seasons)} season(s), {total_plays} total plays.")

    all_rows_by_reaction: dict[float, list[dict]] = {r: [] for r in CANDIDATE_REACTIONS}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_URL)
        page.wait_for_function("window.KMFlight != null")

        for season_idx, (plays, tables) in enumerate(seasons):
            results = page.evaluate(
                """(a) => {
                    var out = {};
                    a.reactions.forEach(function (reactionS) {
                        KMFlight.setPitcherChargeReactionS(reactionS);
                        var winners = [];
                        a.plays.forEach(function (m) {
                            try {
                                var flight = KMFlight.flightParams(m, a.tables);
                                if (!flight || flight.clearedFence) return;
                                if (!KMFlight.GROUND_ARCHETYPES[flight.archetype]) return;
                                var hand = m.batter_hand === 'L' ? 'L' : 'R';
                                var nominal = KMFlight.HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];
                                KMFlight.resolveGrounderInterception(m, flight, hand);
                                winners.push({
                                  moment_id: m.moment_id, nominal: nominal || null,
                                  winner: flight.fielder, angle: flight.angle, ev: flight.ev,
                                });
                            } catch (e) { /* a handful of malformed archive rows - skip, not this probe's concern */ }
                        });
                        out[reactionS] = winners;
                    });
                    KMFlight.setPitcherChargeReactionS(a.restoreTo);
                    return out;
                }""",
                {"reactions": CANDIDATE_REACTIONS, "plays": plays, "tables": tables, "restoreTo": 0.45},
            )
            for reactionS in CANDIDATE_REACTIONS:
                rows = results.get(str(reactionS), results.get(reactionS, []))
                # moment_id can collide across seasons - prefix so the later
                # flip-tracking dict stays unambiguous.
                for r in rows:
                    r["moment_id"] = f"s{season_idx}:{r['moment_id']}"
                all_rows_by_reaction[reactionS].extend(rows)
        browser.close()

    print(f"Swept {total_plays} real plays across {len(seasons)} season(s).\n")
    print(f"{'reaction_s':>10} | {'P wins':>7} | {'3B wins':>8} | "
          f"{'nominal=P->won by P':>20} | {'nominal=P->won by 3B':>21}")
    baseline_p_winners = None
    for reactionS in CANDIDATE_REACTIONS:
        rows = all_rows_by_reaction[reactionS]
        winner_counts = Counter(r["winner"] for r in rows)
        nominal_p = [r for r in rows if r["nominal"] == "P"]
        nominal_p_won_by_p = sum(1 for r in nominal_p if r["winner"] == "P")
        nominal_p_won_by_3b = sum(1 for r in nominal_p if r["winner"] == "3B")
        p_wins = winner_counts.get("P", 0)
        b3_wins = winner_counts.get("3B", 0)
        print(f"{reactionS:>10} | {p_wins:>7} | {b3_wins:>8} | "
              f"{nominal_p_won_by_p:>20} | {nominal_p_won_by_3b:>21}")
        if reactionS == 0.15:
            baseline_p_winners = {r["moment_id"] for r in rows if r["winner"] == "P"}

    # Which specific plays flip from P at the shared baseline (0.15) to
    # someone else at each candidate value, and to whom.
    print("\nPlays that flip away from P as reaction increases (baseline reaction_s=0.15):")
    for reactionS in CANDIDATE_REACTIONS:
        if reactionS == 0.15:
            continue
        rows = all_rows_by_reaction[reactionS]
        by_id = {r["moment_id"]: r for r in rows}
        flipped = [by_id[mid] for mid in baseline_p_winners if mid in by_id and by_id[mid]["winner"] != "P"]
        flip_targets = Counter(r["winner"] for r in flipped)
        print(f"  reaction_s={reactionS}: {len(flipped)} of {len(baseline_p_winners)} "
              f"baseline-P plays flip away from P -> {dict(flip_targets)}")


if __name__ == "__main__":
    main()

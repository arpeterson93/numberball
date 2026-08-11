"""
Unit tests for the fielding-position mapping feature (utils.py's Path A/Path B
defensive-alignment resolvers). Mirrors ball_flight_test.py's check()/failures
runner style, but these are pure Python functions - no Playwright needed.

Run from the repo root:
    python defense_alignment_test.py
"""
from __future__ import annotations

import sys

import key_moments_build
import utils

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(f"{label}: got={got!r} want={want!r}")


# ── split_play_num / make_play_num round-trip ──────────────────────────────

def test_play_num_round_trip() -> None:
    print("split_play_num / make_play_num round-trip:")
    check("split_play_num(130317034)", utils.split_play_num(130317034), (130317, 34))
    check("split_play_num(130317001)", utils.split_play_num(130317001), (130317, 1))
    check("make_play_num('130317', 34)", utils.make_play_num("130317", 34), 130317034)
    check("make_play_num(130317, 1)", utils.make_play_num(130317, 1), 130317001)
    n = 130221053
    g, s = utils.split_play_num(n)
    check("round trip split->make", utils.make_play_num(g, s), n)


# ── league-wide contiguity assertion (live check against the real MLN sheet) ─

def test_league_wide_seq_contiguity() -> None:
    print("League-wide seq contiguity (live check against the MLN Plays (Raw) tab):")
    try:
        plays = utils.read_mln_plays_from_sheet(key_moments_build.MLN_SHEET_ID, tab="Plays (Raw)")
    except Exception as exc:
        print(f"  [skip] could not fetch the live sheet: {exc}")
        return
    if not plays:
        print("  [skip] no plays returned")
        return

    by_game: dict[str, list[int]] = {}
    for p in plays:
        game_code, seq = utils.split_play_num(p["play_num"])
        by_game.setdefault(str(game_code), []).append(seq)

    bad_games = []
    for game_code, seqs in by_game.items():
        seqs.sort()
        expected = list(range(1, len(seqs) + 1))
        if seqs != expected:
            bad_games.append((game_code, seqs[:5], expected[:5], len(seqs)))
    check(f"all {len(by_game)} games have contiguous 1..N seqs", bad_games, [])


# ── Path A: lineup_alignment_at ─────────────────────────────────────────────

def test_path_a_lineup_alignment() -> None:
    print("Path A - lineup_alignment_at:")
    name_to_id = {"Alice": 1, "Bob": 2, "Carol": 3, "Alice2": 4, "Pitcher1": 5}

    rows = [
        {"team": "GHG", "row": 1, "pos": "SS", "player_name": "Alice",   "order_slot": 1, "seq": 1},
        {"team": "GHG", "row": 2, "pos": "2B", "player_name": "Bob",     "order_slot": 2, "seq": 1},
        {"team": "GHG", "row": 3, "pos": "P",  "player_name": "Pitcher1", "order_slot": None, "seq": 1},
        # Y.E. Wally shape: same slot, same pos, later seq (a sub with no position change).
        {"team": "GHG", "row": 4, "pos": "SS", "player_name": "Alice2", "order_slot": 1, "seq": 2},
        # Slot 2's occupant (Bob) shifts position without a sub.
        {"team": "GHG", "row": 5, "pos": "3B", "player_name": "Bob",    "order_slot": 2, "seq": 3},
        # A different slot lands on the same pos at the same seq - collision.
        {"team": "GHG", "row": 6, "pos": "3B", "player_name": "Carol",  "order_slot": 3, "seq": 3},
    ]

    at1 = utils.lineup_alignment_at(rows, "GHG", 1, name_to_id)
    check("seq=1: SS", at1.get("SS"), {"player_id": 1, "name": "Alice"})
    check("seq=1: 2B", at1.get("2B"), {"player_id": 2, "name": "Bob"})
    check("seq=1: no pitcher in output", "P" in at1, False)
    check("seq=1: no 3B yet", "3B" in at1, False)

    at2 = utils.lineup_alignment_at(rows, "GHG", 2, name_to_id)
    check("seq=2: SS subs in Alice2", at2.get("SS"), {"player_id": 4, "name": "Alice2"})
    check("seq=2: 2B still Bob", at2.get("2B"), {"player_id": 2, "name": "Bob"})

    at3 = utils.lineup_alignment_at(rows, "GHG", 3, name_to_id)
    check("seq=3: SS still Alice2", at3.get("SS"), {"player_id": 4, "name": "Alice2"})
    # Collision: slot2 (Bob, row 5) and slot3 (Carol, row 6) both land on 3B -
    # the higher row (Carol) wins.
    check("seq=3: 3B collision keeps higher row (Carol)", at3.get("3B"), {"player_id": 3, "name": "Carol"})
    check("seq=3: only one 3B entry", list(at3.values()).count({"player_id": 3, "name": "Carol"}), 1)

    # Unresolved name (traded/released player off the roster tab) degrades to
    # {player_id: None, name} rather than raising.
    at_unknown = utils.lineup_alignment_at(rows, "GHG", 1, {})
    check("unresolved name -> player_id None", at_unknown.get("SS"), {"player_id": None, "name": "Alice"})


# ── Path B: reconstruct_defense_timeline (slot-based, rewritten per Alex's ──
# real-data review of game 130121 - see fielding-resolution-spec.html) ─────
#
# Batting-order slot is recovered by counting a team's own genuine turns
# (index mod 9) rather than read from the data, so every fixture below needs
# a real 9-slot cycle around whatever it's actually testing - a 2-3 row
# fixture can't exercise "the same slot, a cycle later" the way the real
# algorithm sees it. _FILLER_* below is one full cycle's worth of distinct,
# never-reused position codes (the real 9: C/1B/2B/SS/LF/CF/RF/DH, one per
# slot) with slot 3 left open for whatever the test actually cares about -
# so every fixture reads as "one lineup, minus the one slot under test."

# MYSTERY_SLOT is 0 - the first slot processed each cycle - so its own row
# always lands exactly on the cycle's own start_seq (seq_start + 0), which
# is what makes the expected seq numbers in the tests below line up cleanly
# with each _cycle(...) call's own seq_start argument.
_FILLER_POS = {1: "1B", 2: "2B", 3: "SS", 4: "LF", 5: "CF", 6: "RF", 7: "C", 8: "DH"}
_FILLER_BID = {1: 701, 2: 702, 3: 703, 4: 704, 5: 705, 6: 706, 7: 707, 8: 708}
MYSTERY_SLOT = 0  # lands on "3B" once resolved - never used by any filler


def _cycle(seq_start: int, mystery_row: tuple | None) -> tuple[list[tuple], int]:
    """One 9-row cycle (seq_start..seq_start+8): filler at every slot except
    MYSTERY_SLOT, which gets `mystery_row` (batter_id, pos, play_type,
    result) - or is skipped entirely if `mystery_row` is None (only valid
    for the LAST cycle of a fixture, to end a team's plays mid-rotation).
    Returns (rows, next_seq).
    """
    rows = []
    seq = seq_start
    for slot in range(9):
        if slot == MYSTERY_SLOT:
            if mystery_row is not None:
                bid, pos, pt, res = mystery_row
                rows.append((seq, bid, pos, pt, res))
                seq += 1
            continue
        rows.append((seq, _FILLER_BID[slot], _FILLER_POS[slot], "Swing", "GO"))
        seq += 1
    return rows, seq


def test_path_b_slot_basics() -> None:
    print("Path B - slot rotation basics:")

    # First-ever occupancy of a slot backfills to game start (seq 1) - the
    # normal, self-resolving 97% case (no PH ever involved).
    rows, _ = _cycle(1, (900, "3B", "Swing", "GO"))
    timeline = utils.reconstruct_defense_timeline(rows)
    check("first occupancy backfills to seq 1", timeline.get("3B"), [(1, 900)])
    check("alignment before their own row (seq 2, mid-cycle)",
          utils.timeline_alignment_at(timeline, 2, ["3B"]), {"3B": 900})

    # A different batter directly taking over the slot (no PH involved) is
    # observed, not inferred - resolved from their own row's own seq.
    rows1, next_seq = _cycle(1, (900, "3B", "Swing", "GO"))
    rows2, _ = _cycle(next_seq, (901, "3B", "Swing", "K"))
    timeline = utils.reconstruct_defense_timeline(rows1 + rows2)
    check("direct displacement: both entries present", timeline.get("3B"), [(1, 900), (10, 901)])
    check("alignment right before the switch", utils.timeline_alignment_at(timeline, 9, ["3B"]), {"3B": 900})
    check("alignment at the switch", utils.timeline_alignment_at(timeline, 10, ["3B"]), {"3B": 901})

    # Same occupant, different position later (no personnel change): a new
    # entry for the new position; the old one is left exactly as it was -
    # not cleared, not moved. "RF3" is a scratch label, not "SS" - SS is
    # already the filler's own position (slot 3) and would collide.
    rows1, next_seq = _cycle(1, (900, "3B", "Swing", "GO"))
    rows2, _ = _cycle(next_seq, (900, "RF3", "Swing", "K"))
    timeline = utils.reconstruct_defense_timeline(rows1 + rows2)
    check("moved: old position entry untouched", timeline.get("3B"), [(1, 900)])
    check("moved: new position resolved from the move itself", timeline.get("RF3"), [(10, 900)])

    # A position with zero observations - not even indirectly - has no key.
    check("never-observed position has no key", "QQ" in timeline, False)

    # DH is tracked (it's a real, exclusive slot - the filler cycle uses it)
    # but never surfaces in the output.
    check("DH never appears in output", "DH" in timeline, False)


def test_path_b_ph_backward_fill() -> None:
    print("Path B - PH backward-fill (game 130121's real shape):")

    # The exact real scenario, reduced to a fixture: a pinch-hitter is the
    # FIRST-EVER occupant of a slot (no defensive info of her own), and the
    # slot's position is only revealed a full cycle later when a DIFFERENT
    # batter takes it over with a real position - Alex's read of the live
    # data (fielding-resolution-spec.html): the position back-fills all the
    # way to game start, under the PINCH-HITTER's own name, not skipped
    # ahead to whoever eventually reveals it.
    rows1, next_seq = _cycle(1, (900, "PH", "Swing", "LODP"))
    rows2, _ = _cycle(next_seq, (950, "3B", "Swing", "HR"))
    timeline = utils.reconstruct_defense_timeline(rows1 + rows2)
    check("PH occupant inherits the later-revealed position",
          timeline.get("3B"), [(1, 900), (10, 950)])
    check("alignment before the PH row even happens (seq 1)",
          utils.timeline_alignment_at(timeline, 1, ["3B"]), {"3B": 900})
    check("alignment right after the PH row (still the PH batter)",
          utils.timeline_alignment_at(timeline, 4, ["3B"]), {"3B": 900})
    check("alignment once the real position is directly observed",
          utils.timeline_alignment_at(timeline, 10, ["3B"]), {"3B": 950})

    # A PH occupant with NO later resolution at all (the game/data just
    # ends) stays a genuine, accepted gap - no name to show, not even a
    # guess.
    rows, _ = _cycle(1, (900, "PH", "Swing", "LODP"))
    timeline = utils.reconstruct_defense_timeline(rows)
    check("unresolved PH: position never surfaces", list(timeline.keys()).count("3B") if "3B" in timeline else 0, 0)
    all_ids = {pid for entries in timeline.values() for _, pid in entries}
    check("unresolved PH: the PH batter appears nowhere in the output", 900 in all_ids, False)

    # Two PH occupants in a row before any resolution - both cascade to the
    # same eventual answer, each keeping their own name and start point.
    rows1, next_seq = _cycle(1, (900, "PH", "Swing", "LODP"))
    rows2, next_seq = _cycle(next_seq, (910, "PH", "Swing", "FO"))
    rows3, _ = _cycle(next_seq, (950, "3B", "Swing", "HR"))
    timeline = utils.reconstruct_defense_timeline(rows1 + rows2 + rows3)
    check("cascade: all three occupants resolve to 3B",
          timeline.get("3B"), [(1, 900), (10, 910), (19, 950)])

    # PH rows genuinely carry no information beyond "someone new is here" -
    # they never themselves become a queryable position, PH included.
    check("PH is never a key in the output", "PH" in timeline, False)


def main() -> None:
    test_play_num_round_trip()
    test_league_wide_seq_contiguity()
    test_path_a_lineup_alignment()
    test_path_b_slot_basics()
    test_path_b_ph_backward_fill()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All defense-alignment checks passed.")


if __name__ == "__main__":
    main()

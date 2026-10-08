"""Season 14 MLN draft order: standings-based slotting plus a static view of
pick trades made during Season 13.

Draft order is straight (same team ranking in every round, not snake): the
Season 13 standings set a single 1-16 slot order, worse record picking
earlier, (wins asc, then run differential RS-RA asc) - ties beyond that break
alphabetically by abbrev for a deterministic order. Each team's own slot
number is identical across all 4 rounds, so overall pick = (round-1)*16 + slot.

PICK_TRADES is a static, manually-maintained remap of (round, original_team)
-> current_holder, covering every pick that changed hands before S14's
standings-based order existed to assign it. Parsing rule for a trade line
"X receives [pick] from Y (Originally from Z)": the ORIGINAL team is Z when
an "Originally from" clause is present, otherwise Y (the team named directly
after "from") - per Alex, 2026-10-07. Only lines explicitly about Season 14
picks apply here; the many "Season 13 [round] pick" trades in the same ledger
settled the S13 draft (already completed, drafted off S12 standings) and are
not part of this remap.

Resolved from Alex's trade list (2026-10-07):
  - "MIA receives ... S14 1st round pick from ACP"            -> (1, ACP) -> MIA
  - "MIA receives Season 14 1st round pick from SUN"          -> (1, SUN) -> MIA
  - "REK receives Season 14 1st and 2nd round pick from MIA"  -> (1, MIA) -> REK
                                                                  (2, MIA) -> REK
    (MIA held 3 separate S14 1st-rounders at this point - its own, plus the
    two just listed above from ACP/SUN - Alex confirmed the one sent to REK
    was MIA's own, not either of the acquired ones.)
  - "SMD receives Season 13 1.4 from POR" also carried a Season 14 1st-round
    pick from POR in the same deal (Alex, 2026-10-07)          -> (1, POR) -> SMD

No Round 3/4 trades exist yet - add entries here the same way if/when they do.
"""
from __future__ import annotations

ROUNDS = 4
TEAMS_PER_ROUND = 16

PICK_TRADES: dict[tuple[int, str], str] = {
    (1, "ACP"): "MIA",
    (1, "SUN"): "MIA",
    (1, "MIA"): "REK",
    (2, "MIA"): "REK",
    (1, "POR"): "SMD",
}


def _run_diff(team: dict) -> int:
    return (team.get("runs_scored") or 0) - (team.get("runs_allowed") or 0)


def standings_order(teams: list[dict]) -> list[dict]:
    """Worst record first: wins asc, then run differential (RS-RA) asc, then
    abbrev asc as a deterministic tiebreaker beyond that."""
    return sorted(teams, key=lambda t: (t.get("wins") or 0, _run_diff(t), t.get("abbrev") or ""))


def compute_draft_order(teams: list[dict]) -> list[dict]:
    """teams: one row per team for the season being drafted off of (16 rows,
    each with at least abbrev/wins/runs_scored/runs_allowed).

    Returns ROUNDS*TEAMS_PER_ROUND rows, ordered overall 1..64:
      round, pick (1-16, position within the round), overall (1-64),
      team (abbrev holding the pick after trades), original_team (abbrev
      whose standing earned the slot), notes (human-readable trade note, or
      "" if untraded).
    """
    order = standings_order(teams)
    if len(order) != TEAMS_PER_ROUND:
        raise ValueError(f"Expected {TEAMS_PER_ROUND} teams, got {len(order)}")

    # PICK_TRADES is hand-maintained - validate every abbrev it references
    # (both the original team a trade's key points at and the team it hands
    # the pick to) against the Supabase Teams table's own abbrevs, so a typo'd
    # or rebranded abbrev fails loudly here instead of silently never matching
    # (key side) or rendering a bare-string team with no logo/full name
    # (value side).
    current_abbrevs = {t.get("abbrev") for t in order}
    bad = sorted({
        f"({rnd}, {orig!r}) -> {holder!r}"
        for (rnd, orig), holder in PICK_TRADES.items()
        if orig not in current_abbrevs or holder not in current_abbrevs
    })
    if bad:
        raise ValueError(
            "PICK_TRADES references abbrev(s) not found in the current Teams table: " + "; ".join(bad))

    rows = []
    for rnd in range(1, ROUNDS + 1):
        for slot, team in enumerate(order, start=1):
            original = team.get("abbrev") or ""
            holder = PICK_TRADES.get((rnd, original), original)
            notes = f"Originally {original}'s pick" if holder != original else ""
            rows.append({
                "round": rnd,
                "pick": slot,
                "overall": (rnd - 1) * TEAMS_PER_ROUND + slot,
                "team": holder,
                "original_team": original,
                "notes": notes,
            })
    return rows

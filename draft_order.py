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

# S14: MIA receives Guidooooo, SMD receives Swiftie ESPN and a first-round pick with conditionals. The pick is one of the
# first-rounders MIA holds (after PICK_TRADES): the later one if MIA misses the playoffs or Guidooooo retires at the end of
# the season (a retirement overrides the playoffs), the earlier one if MIA makes the playoffs. Set "retires" to True if
# Guidooooo retires; "MIA makes the playoffs" is read from the seeding.
CONDITIONAL_FIRST = {"round": 1, "from": "MIA", "to": "SMD", "retires": False}

PICK_TRADES: dict[tuple[int, str], str] = {
    (1, "ACP"): "MIA",
    (1, "SUN"): "MIA",
    (1, "MIA"): "REK",
    (2, "MIA"): "REK",
    (1, "POR"): "SMD",
}


def _pct(w: int, l: int) -> float:
    return w / (w + l) if w + l else 0.5


class _Book:
    """Regular-season results of one season, from finished games: overall, head-to-head, division, league and
    against-opposing-league records, run differentials, and the win % against teams over .500."""

    def __init__(self, teams: list[dict], games: list[dict]):
        self.team = {t["abbrev"]: t for t in teams}
        self.w = {a: 0 for a in self.team}
        self.l = {a: 0 for a in self.team}
        self.rd = {a: 0 for a in self.team}
        self.lg_rd = {a: 0 for a in self.team}
        self.h2h: dict[tuple[str, str], list[int]] = {}      # (a, b) -> [a's wins, a's losses] against b
        self.div = {a: [0, 0] for a in self.team}
        self.lg = {a: [0, 0] for a in self.team}
        self.opp_lg = {a: [0, 0] for a in self.team}
        self.games: list[dict] = []     # the regular-season games counted, for the tiebreak write-up
        for g in games:
            if g.get("type") == "Post" or g.get("away_score") is None or g.get("home_score") is None:
                continue
            a, h = g.get("away_team"), g.get("home_team")
            if a not in self.team or h not in self.team or g["away_score"] == g["home_score"]:
                continue
            win, lose = (a, h) if g["away_score"] > g["home_score"] else (h, a)
            margin = abs(g["away_score"] - g["home_score"])
            self.games.append({"session": g.get("session_number"), "away": a, "home": h,
                               "away_score": g["away_score"], "home_score": g["home_score"]})
            for me, opp, k in ((win, lose, 0), (lose, win, 1)):
                (self.w if k == 0 else self.l)[me] += 1
                sign = 1 if k == 0 else -1
                self.rd[me] += sign * margin
                self.h2h.setdefault((me, opp), [0, 0])[k] += 1
                same_lg = self.team[me]["sub_league"] == self.team[opp]["sub_league"]
                if same_lg:
                    self.lg[me][k] += 1
                    self.lg_rd[me] += sign * margin
                    if self.team[me]["division"] == self.team[opp]["division"]:
                        self.div[me][k] += 1
                else:
                    self.opp_lg[me][k] += 1

    def pct(self, a: str) -> float:
        return _pct(self.w[a], self.l[a])

    def vs500(self, a: str) -> float:
        w = l = 0
        for (x, y), (xw, xl) in self.h2h.items():
            if x == a and self.pct(y) > 0.5:
                w, l = w + xw, l + xl
        return _pct(w, l)

    def h2h_in(self, a: str, group: list[str]) -> float:
        w = l = 0
        for b in group:
            if b != a:
                xw, xl = self.h2h.get((a, b), (0, 0))
                w, l = w + xw, l + xl
        return _pct(w, l)


def _rec(rec) -> str:
    return f"{rec[0]}-{rec[1]}"


def _signed(n) -> str:
    return f"+{n}" if n > 0 else str(n)


# The rulebook's tiebreak chains (Section 5) are lists of (label, value (higher is better), text shown for a team's value).
def _criteria(bk: _Book, group: list[str]) -> list[tuple[str, callable, callable]]:
    leagues = {bk.team[a]["sub_league"] for a in group}
    divisions = {(bk.team[a]["sub_league"], bk.team[a]["division"]) for a in group}

    def h2h_rec(a):
        w = l = 0
        for b in group:
            if b != a:
                xw, xl = bk.h2h.get((a, b), (0, 0))
                w, l = w + xw, l + xl
        return _rec((w, l))

    h2h = ("head-to-head", lambda a: bk.h2h_in(a, group), h2h_rec)
    rd = ("run differential", lambda a: bk.rd[a], lambda a: _signed(bk.rd[a]))
    div = ("division record", lambda a: _pct(*bk.div[a]), lambda a: _rec(bk.div[a]))
    lg = ("league record", lambda a: _pct(*bk.lg[a]), lambda a: _rec(bk.lg[a]))
    if len(leagues) > 1:     # across leagues (the ToOS home-team order): h2h > run differential > division > other league
        return [h2h, rd, div, ("record vs other league", lambda a: _pct(*bk.opp_lg[a]), lambda a: _rec(bk.opp_lg[a]))]
    if len(group) == 2:
        return [h2h, div, lg, rd]
    tail = [("win % vs .500+", bk.vs500, lambda a: f"{bk.vs500(a):.3f}".lstrip("0")),
            ("league run differential", lambda a: bk.lg_rd[a], lambda a: _signed(bk.lg_rd[a])), rd]
    if len(divisions) == 1:   # three or more from one division: head-to-head and division record still apply
        return [h2h, div, lg] + tail
    return [lg] + tail        # three or more from different divisions skip head-to-head and division record


def _rank(bk: _Book, group: list[str], trail: dict[str, list[str]], events: list[dict]) -> list[str]:
    """group best first, by the rulebook's tiebreakers. After a criterion splits the group the chain restarts from the top
    on each part (the rulebook's 'once a team has been eliminated, go back to the top'). trail collects, per team, each
    criterion that was applied to the group it was in, with the team's own value; a group nothing splits (the home run
    derby) falls back to abbrev order. events gets one entry per split, in the order applied: the criterion, the teams it
    was applied to, each one's value as shown, and the parts it split them into (best first)."""
    if len(group) < 2:
        return list(group)
    for label, fn, show in _criteria(bk, group):
        vals = {a: round(fn(a), 9) for a in group}
        if len(set(vals.values())) > 1:
            for a in group:
                trail.setdefault(a, []).append(f"{label} {show(a)}")
            parts = [[a for a in group if vals[a] == v] for v in sorted(set(vals.values()), reverse=True)]
            events.append({"label": label, "group": list(group), "vals": {a: show(a) for a in group}, "parts": parts})
            out: list[str] = []
            for part in parts:
                out += _rank(bk, part, trail, events)
            return out
    for a in group:
        trail.setdefault(a, []).append("unresolved (home run derby)")
    events.append({"label": "unresolved (home run derby)", "group": list(group), "vals": {}, "parts": [sorted(group)]})
    return sorted(group)


def _order(bk: _Book, group: list[str], ties: list[dict], what: str) -> list[str]:
    """group best first: by win %, ties broken by _rank and logged in ties."""
    out: list[str] = []
    by_pct: dict[float, list[str]] = {}
    for a in group:
        by_pct.setdefault(round(bk.pct(a), 9), []).append(a)
    for pct in sorted(by_pct, reverse=True):
        tied = by_pct[pct]
        trail: dict[str, list[str]] = {}
        events: list[dict] = []
        ranked = _rank(bk, tied, trail, events)
        if len(tied) > 1:
            ties.append({"what": what, "teams": ranked, "record": f"{bk.w[ranked[0]]}-{bk.l[ranked[0]]}", "trail": trail,
                         "events": events,
                         "games": [g for g in bk.games if g["away"] in tied and g["home"] in tied]})
        out += ranked
    return out


def draft_slots(teams: list[dict], games: list[dict], playoffs: list[dict] | None = None) -> tuple[list[str], list[dict], dict[str, int], dict]:
    """The 1-16 draft slot order (first pick first) and the tiebreaks that decided it.

    Seeding follows Section 5: per league, the two division winners are seeds 1-2 and the next two best records are the
    wild cards, seeds 3-4. The eight teams out of the playoffs pick first (worst record first). Teams the playoffs
    have already eliminated come next, a round at a time (wild card round, league championship, the ToOS loser), and
    teams still alive after them are taken to finish by seed: the two seed 4s, the two 3s, the 2s and the 1s - the worse
    record of each group first, the leagues mingling by the ToOS tiebreakers; a decided champion picks last. playoffs is
    the finished Post games: a team's round is how many it has played, and with all eight teams known they are the field.
    ties lists every tie that set a slot or a seed:
    {"what", "teams" (best first), "record", "trail" ({team: the criteria applied to it, with its numbers}), "events", "games" (the games the tied
    teams played each other), "title"}; the playoff seed (1-4) of each team that made the playoffs; and the
    standings layout {"divisions": [(league, division, teams best first)], "wildcard": {league: non-division-winners best first},
    "field": the playoff teams, "finish": {team: "Missed" / "Lost WC" / "Lost LCR" / "Lost ToOS" / "Champion" / "Alive"},
    empty until a playoff game has been played}."""
    bk = _Book(teams, games)
    ties: list[dict] = []
    seeds: dict[str, int] = {}
    layout: dict = {"divisions": [], "wildcard": {}}
    for league in sorted({t["sub_league"] for t in teams}):
        lg = [t["abbrev"] for t in teams if t["sub_league"] == league]
        winners, rest = [], []
        for div in sorted({bk.team[a]["division"] for a in lg}):
            ranked = _order(bk, [a for a in lg if bk.team[a]["division"] == div], ties, f"{league} {div} division")
            winners.append(ranked[0])
            rest += ranked[1:]
            layout["divisions"].append((league, div, ranked))
        winners = _order(bk, winners, ties, f"{league} seeds 1-2")
        wild_all = _order(bk, rest, ties, f"{league} wild cards")
        layout["wildcard"][league] = wild_all
        wild = wild_all[:2]
        for seed, a in enumerate(winners + wild, start=1):
            seeds[a] = seed
    # what the playoffs have settled so far: a team's round is how many playoff games it has played, so the loser of
    # its game went out in that round; the winner of a round-3 game is the champion
    played: dict[str, int] = {}
    lost: dict[int, list[str]] = {1: [], 2: [], 3: []}
    champion = None
    for g in sorted((g for g in (playoffs or []) if g.get("type") == "Post" and g.get("away_score") is not None
                     and g.get("home_score") is not None), key=lambda g: str(g.get("game_code") or "")):
        a, h = g["away_team"], g["home_team"]
        if a not in bk.team or h not in bk.team:
            continue
        for t in (a, h):
            played[t] = played.get(t, 0) + 1
        win, lose = (a, h) if g["away_score"] > g["home_score"] else (h, a)
        lost[min(played[lose], 3)].append(lose)
        if played[win] >= 3:
            champion = win
    field = set(played) if len(played) == 8 else set(seeds)
    out_of_playoffs = [a for a in bk.team if a not in field]
    order: list[str] = _order(bk, out_of_playoffs, ties, "draft, out of the playoffs")[::-1]
    for rnd, label in ((1, "the Wild Card round"), (2, "the League Championship"), (3, "the ToOS")):
        order += _order(bk, lost[rnd], ties, f"draft, eliminated in {label}")[::-1]
    alive = [a for a in field if a not in order and a != champion]
    for seed in (4, 3, 2, 1):
        order += _order(bk, [a for a in alive if seeds.get(a) == seed], ties, f"draft, seed {seed}")[::-1]
    order += _order(bk, [a for a in alive if a not in seeds], ties, "draft, unseeded")[::-1]     # a field my seeds missed
    order += [champion] if champion else []
    layout["field"] = field
    layout["finish"] = {}
    if played:
        names = {1: "Lost WC", 2: "Lost LCR", 3: "Lost ToOS"}
        layout["finish"] = {a: ("Missed" if a not in field else "Champion" if a == champion else "Alive") for a in bk.team}
        for rnd, group in lost.items():
            layout["finish"].update({a: names[rnd] for a in group})
    kept = [t for t in ties if t["what"].startswith("draft") or any(a in seeds for a in t["teams"])]
    for t in kept:
        what = t["what"]
        if what.startswith("draft"):
            slots = [order.index(a) + 1 for a in t["teams"]]
            group = ("Non-Playoff Teams" if "out of" in what else
                     "Eliminated in " + what.split("eliminated in ")[1] if "eliminated in" in what else
                     "Unseeded Playoff Teams" if "unseeded" in what else f"Seed {what[-1]} Finish")
            t["title"] = f"{group} (Picks {min(slots)}-{max(slots)})"
        elif what.endswith("division"):
            t["title"] = what[:-len("division")] + "Division Race"
        elif what.endswith("1-2"):
            t["title"] = what.replace("seeds 1-2", "Seeds 1-2 (Division Winners)")
        else:
            t["title"] = what.replace("wild cards", "Wild Card Spots")
        t["title"] += f": tied at {t['record']}"
    return order, kept, seeds, layout


_STEP_ABBREV = {"head-to-head": "H2H", "division record": "Div", "league record": "Lg", "win % vs .500+": ".500+",
                "league run differential": "Lg RD", "run differential": "RD", "record vs other league": "Other lg",
                "unresolved (home run derby)": "derby"}
TIEBREAK_LEGEND = ["Seed: DW division winner, WC wild card, Out missed the playoffs (picks first). Tiebreak scope: Div division race, "
                   "Seed playoff seeding, Pick draft slot.",
                   "Each step shows that team's own number: H2H record vs the tied teams, Div division record, Lg league record, "
                   ".500+ win % vs .500+ teams, RD run diff."]


def _abbrev_step(step: str) -> str:
    for long, short in _STEP_ABBREV.items():
        if step.startswith(long):
            return short + step[len(long):]
    return step


def tie_label(ties: list[dict], team: str) -> str:
    """Short text for the ties a team was in, with its own number at each step it was applied, e.g.
    'Div: H2H 2-0 · Pick: 3-way Lg 4-2, RD +3' (empty when it had none)."""
    out = []
    for t in ties:
        if team not in t["teams"]:
            continue
        scope = "Pick" if t["what"].startswith("draft") else "Div" if t["what"].endswith("division") else "Seed"
        ways = f"{len(t['teams'])}-way " if len(t["teams"]) > 2 else ""
        out.append(f"{scope}: {ways}{', '.join(_abbrev_step(x) for x in t['trail'][team])}")
    return " · ".join(out)


def conditional_legend() -> str:
    c = CONDITIONAL_FIRST
    return (f"Conditional: {c['to']} gets the later of {c['from']}'s round {c['round']} picks if {c['from']} misses the "
            f"playoffs or Guidooooo retires, else the earlier.")


def games_before_last_session(games: list[dict]) -> list[dict]:
    """games without the latest session that has a finished game - the standings one session back."""
    done = [g for g in games if g.get("away_score") is not None and g.get("home_score") is not None]
    last = max((g["session_number"] for g in done), default=None)
    return [g for g in games if g["session_number"] != last]


def compute_draft_order(teams: list[dict], games: list[dict], prev_games: list[dict] | None = None,
                        trades: bool = True, playoffs: list[dict] | None = None) -> dict:
    """teams: the 16 teams of the season being drafted off of (abbrev, sub_league, division); games: that season's
    finished games (regular season only is counted; see draft_slots for the seeding and tiebreak rules).

    Returns {"rows": ROUNDS*TEAMS_PER_ROUND rows ordered overall 1..64, "ties": [...]}. Each row has:
      round, pick (1-16, position within the round), overall (1-64),
      team (abbrev holding the pick after trades), original_team (abbrev
      whose standing earned the slot), notes (human-readable trade note, or
      "" if untraded), and with prev_games (the same games one session earlier) move = how many picks the slot rose
      (positive: an earlier pick than before; negative: later; 0: unchanged). The slot belongs to the original team, so
      a traded pick carries its original team's move. ties = draft_slots' tiebreak log, and tiebreak = tie_label for
      this row's original team, seed = its playoff seed ("1 DW" .. "4 WC") or "Out"; conditional marks the pick that CONDITIONAL_FIRST moved.
    playoffs = the finished playoff (Post) games: teams already eliminated pick in the order they went out (see draft_slots),
    and each row gets finish ("Missed", "Lost WC", ..., "Champion", "Alive"; "" before any playoff game).
    trades=False skips PICK_TRADES and CONDITIONAL_FIRST (they are for the season being drafted now, not a past one).
    """
    if len(teams) != TEAMS_PER_ROUND:
        raise ValueError(f"Expected {TEAMS_PER_ROUND} teams, got {len(teams)}")
    order, ties, seeds, layout = draft_slots(teams, games, playoffs)
    prev_order = draft_slots(teams, prev_games)[0] if prev_games is not None else []
    prev_slot = {a: i for i, a in enumerate(prev_order, start=1)}

    # PICK_TRADES is hand-maintained - validate every abbrev it references
    # (both the original team a trade's key points at and the team it hands
    # the pick to) against the Supabase Teams table's own abbrevs, so a typo'd
    # or rebranded abbrev fails loudly here instead of silently never matching
    # (key side) or rendering a bare-string team with no logo/full name
    # (value side).
    pick_trades = PICK_TRADES if trades else {}
    current_abbrevs = set(order)
    bad = sorted({
        f"({rnd}, {orig!r}) -> {holder!r}"
        for (rnd, orig), holder in pick_trades.items()
        if orig not in current_abbrevs or holder not in current_abbrevs
    })
    if bad:
        raise ValueError(
            "PICK_TRADES references abbrev(s) not found in the current Teams table: " + "; ".join(bad))

    rows = []
    for rnd in range(1, ROUNDS + 1):
        for slot, original in enumerate(order, start=1):
            holder = pick_trades.get((rnd, original), original)
            notes = f"Originally {original}'s pick" if holder != original else ""
            rows.append({
                "round": rnd,
                "pick": slot,
                "overall": (rnd - 1) * TEAMS_PER_ROUND + slot,
                "team": holder,
                "original_team": original,
                "notes": notes,
                "move": prev_slot[original] - slot if original in prev_slot else None,
                "tiebreak": tie_label(ties, original),
                "finish": layout["finish"].get(original, ""),
                "seed": (f"{seeds[original]} {'DW' if seeds[original] <= 2 else 'WC'}" if original in seeds else
                         "PO" if original in layout["field"] else "Out"),
            })
    if trades:
        _apply_conditional_trade(rows, layout["field"])
    return {"rows": rows, "ties": ties, "bracket": bracket(teams, seeds), "layout": layout, "seeds": seeds}


def bracket(teams: list[dict], seeds: dict[str, int]) -> dict[str, list[str]]:
    """Each league's four playoff teams in seed order (1-2 division winners, 3-4 wild cards), leagues in name order."""
    return {lg: [a for a, _ in sorted(((t["abbrev"], seeds[t["abbrev"]]) for t in teams
                                       if t["sub_league"] == lg and t["abbrev"] in seeds), key=lambda x: x[1])]
            for lg in sorted({t["sub_league"] for t in teams})}


def _apply_conditional_trade(rows: list[dict], field: set[str]) -> None:
    """The S14 MIA -> SMD conditional first (see CONDITIONAL_FIRST): SMD gets the later of MIA's first-round picks when MIA
    misses the playoffs or Guidooooo retires, the earlier one when MIA makes them."""
    c = CONDITIONAL_FIRST
    pool = [r for r in rows if r["round"] == c["round"] and r["team"] == c["from"]]
    if not pool:
        raise ValueError(f"CONDITIONAL_FIRST: {c['from']} holds no round {c['round']} pick")
    later = c["retires"] or c["from"] not in field
    pick = max(pool, key=lambda r: r["overall"]) if later else min(pool, key=lambda r: r["overall"])
    pick["team"] = c["to"]
    pick["notes"] = f"{pick['original_team']}'s pick via {c['from']}, conditional"
    pick["conditional"] = True


def bracket_results(bracket: dict[str, list[str]], games: list[dict]) -> dict:
    """The playoff games already played, placed on bracket (league -> its four teams in seed order): the wild card games
    (1v4, 2v3), each league's championship game between their winners, and the ToOS between the league champions.
    games: finished games, of which the "Post" ones count. Returns {"wc": {league: [game, game]}, "winners": {league: [the
    two wild card winners, None until known]}, "lcr": {league: game}, "champ": {league: team}, "toos": game, "unmatched": how
    many playoff games fit none of those pairings}, each game {"scores": {team: runs}, "winner"} or None."""
    by_pair = {}
    for g in sorted((g for g in games if g.get("type") == "Post" and g.get("away_score") is not None
                     and g.get("home_score") is not None), key=lambda g: str(g.get("game_code") or "")):
        by_pair[frozenset((g["away_team"], g["home_team"]))] = g
    used: set = set()

    def find(a, b):
        g = by_pair.get(frozenset((a, b))) if a and b else None
        if not g:
            return None
        used.add(frozenset((a, b)))
        away_won = g["away_score"] > g["home_score"]
        return {"scores": {g["away_team"]: g["away_score"], g["home_team"]: g["home_score"]},
                "winner": g["away_team"] if away_won else g["home_team"]}

    out: dict = {"wc": {}, "winners": {}, "lcr": {}, "champ": {}}
    for lg, ab in bracket.items():
        wc = [find(ab[0], ab[3]), find(ab[1], ab[2])]
        win = [r["winner"] if r else None for r in wc]
        lcr = find(*win)
        out["wc"][lg], out["winners"][lg], out["lcr"][lg] = wc, win, lcr
        out["champ"][lg] = lcr["winner"] if lcr else None
    champs = list(out["champ"].values())
    out["toos"] = find(*champs) if len(champs) == 2 else None
    out["unmatched"] = len(by_pair) - len(used)
    return out

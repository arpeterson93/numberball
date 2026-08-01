"""
Forward Monte Carlo win probability table for MLN.

An independently-derived alternative to win_probability_table.csv (which
compute_win_probability.py builds by empirical-Bayesian blending of real
Numberball outcomes with an MLB-relativity-adjusted logistic prior). This
script instead simulates games play-by-play from the theoretical per-situation
result distribution in result_ranges_re24.csv, then scrapes the final win/loss
back onto every state each simulated game passed through.

State recorded per plate appearance, matching win_probability_table.csv's key:
  remaining_half_innings: 1-12; extras cap at 2 (top half) or 1 (bottom half)
  outs: 0, 1, 2
  obc: 8 binary baserunner codes "000" ... "111"
  batting_lead: runs ahead (+) or behind (-) from the batting team's view,
                clipped to +/-18 when bucketing

On the +/-18 domain: utils.get_win_probability and
get_win_probability_interpolated both clamp every *query* to +/-10, but the
stored win_probability_table.csv carries data out to +/-18 (37 distinct lead
values). This table matches the file's domain, not the narrower query clamp,
so the two are directly comparable cell for cell. That is a file-level match
only and changes no behavior on its own: until those two clamp lines in
utils.py are widened as their own reviewed change, nothing in the app ever
looks up a lead beyond +/-10.

Three modes, one mechanic
-------------------------
Everything runs through simulate_from(), which starts at any legal game state
and plays forward to a decided finish, recording every state it passes. The
modes only differ in where they start it:

  --mode full-game     Start at the true game opening, --games times. States
                       get sampled in proportion to how often real games reach
                       them: fast convergence on common states, slow on rare
                       ones.
  --mode state-seeded  Enumerate all 10,656 cells, reconstruct a concrete
                       starting game state for each, and run
                       --rollouts-per-state continuations from each directly.
                       Guaranteed, controllable coverage regardless of rarity.
  --mode backfill      state-seeded rollouts, but only for cells already below
                       --min-n. The recommended follow-up to a full-game pass:
                       no rollout budget spent on cells that already have data.

Mixing modes in one table is statistically sound. A cell's value is
P(win | state), and by the Markov property every continuation that passes
through a state is an unbiased sample of that conditional no matter how the
continuation was started. The one place to be careful is the clamped
batting_lead boundary: a +/-18 cell pools every true lead at or beyond 18, and
full-game and state-seeded mode reach it with different mixes of true leads.
Those are the deepest blowout cells, so it does not matter much in practice,
but that is why they are the cells where the two modes can disagree.

Probability source
------------------
result_ranges_re24.csv partitions the 0..500 circular-diff domain into 501
integer slots for each of the 24 (outs, obc) situations, so a plate appearance
is sampled by drawing one slot uniformly - the Rng column is used as a raw
weight, never normalized. Every (Result, obc, outs) row in that file is an
exact key in utils._BRC_RUN_LOOKUP, so runner advancement never falls through
to advance_runners' hand-coded approximation.

Modeling boundary: this is a normal-swing model only. Steals, caught stealing,
bunts, IBB and balks are absent from result_ranges_re24.csv by design (they are
manager decisions or rule events rather than diff-band outcomes) and are not
layered on as separate stochastic events here.

Accumulation
------------
Runs accumulate by default. The output file carries a raw win_sum alongside n
so repeated runs add to the pool without ever reconstructing a count from a
rounded probability. Pass --fresh to discard the existing pool and start over.
Seeds default to OS entropy so a repeated run actually adds new games; the seed
used is always printed. simulated_win_probability_meta.json tracks cumulative
totals across runs.

Writes simulated_win_probability_table.csv - never win_probability_table.csv.
Nothing in utils.py or the live app reads this file; it exists to be compared
against the current table by compare_win_probability_tables.py.

Run from the project root:
    python simulate_win_probability.py --fresh                 # clean baseline
    python simulate_win_probability.py --games 500             # fast dev loop
    python simulate_win_probability.py --mode backfill         # fill thin cells
    python simulate_win_probability.py --mode state-seeded --rollouts-per-state 200
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime, timezone

import pandas as pd

import utils
from key_moments_build import MLN_INNINGS, _is_walkoff_final

RANGES_CSV = "result_ranges_re24.csv"
OUT_CSV = "simulated_win_probability_table.csv"
META_JSON = "simulated_win_probability_meta.json"
PROTECTED_CSV = "win_probability_table.csv"

RNG_DOMAIN = 501          # 0..500 inclusive, the diff domain every range table partitions
LEAD_CLIP = 18            # matches win_probability_table.csv's stored domain, not utils' query clamp
MAX_REMAINING = MLN_INNINGS * 2
LEAD_SLOTS = LEAD_CLIP * 2 + 1
MAX_INNINGS = 200         # runaway guard; a real game never gets close
LOW_N = 30                # cells below this are reported as low-confidence

_OBC_IDX = {obc: i for i, obc in enumerate(utils.OBC_OPTIONS)}
TOTAL_CELLS = MAX_REMAINING * 3 * len(_OBC_IDX) * LEAD_SLOTS


# ── cell indexing ─────────────────────────────────────────────────────────────
# States are accumulated into flat lists rather than a dict of tuple keys: the
# key space is a fixed 6,048 cells and the inner loop runs tens of millions of
# times, so an integer index keeps the hot path free of tuple hashing.

def _cell_base(remaining: int, outs: int, obc: str) -> int:
    """Flat index of (remaining, outs, obc) at batting_lead = -LEAD_CLIP."""
    return (((remaining - 1) * 3 + outs) * len(_OBC_IDX) + _OBC_IDX[obc]) * LEAD_SLOTS


def _build_cell_keys() -> tuple[dict[tuple[int, int, str], int], list[tuple[int, int, str, int]]]:
    """Return (base index by state, flat index -> full key) for the whole grid."""
    bases: dict[tuple[int, int, str], int] = {}
    keys: list[tuple[int, int, str, int]] = [None] * TOTAL_CELLS  # type: ignore[list-item]
    for remaining in range(1, MAX_REMAINING + 1):
        for outs in range(3):
            for obc in utils.OBC_OPTIONS:
                base = _cell_base(remaining, outs, obc)
                bases[(remaining, outs, obc)] = base
                for lead in range(-LEAD_CLIP, LEAD_CLIP + 1):
                    keys[base + lead + LEAD_CLIP] = (remaining, outs, obc, lead)
    return bases, keys


# ── probability source ────────────────────────────────────────────────────────

def load_result_ranges(path: str = RANGES_CSV) -> pd.DataFrame:
    """Load result_ranges_re24.csv with obc restored to a zero-padded string.

    The obc column is written as a bare int, so "001" reads back as 1 and "010"
    as 10 unless it is read as text first - the same gotcha win_probability_table.csv
    and state_frequencies.csv already carry.
    """
    df = pd.read_csv(path, dtype={"obc": str})
    df["obc"] = df["obc"].str.strip().str.zfill(3)
    df["outs"] = df["outs"].astype(int)
    df["Rng"] = df["Rng"].astype(int)
    df["Result"] = df["Result"].astype(str).str.strip()
    return df


def validate_result_ranges(df: pd.DataFrame) -> None:
    """Fail loudly if the simulator's foundation is not what it claims to be.

    Three invariants, all verified by hand before this script was written but
    re-checked here so a future edit to the CSV breaks the run instead of
    silently skewing every simulated game:
      1. All 24 (outs, obc) situations are present.
      2. Each situation's Rng sums to exactly 501 (the full 0..500 domain).
      3. Every (Result, obc, outs) row is an exact key in utils._BRC_RUN_LOOKUP,
         so utils.advance_runners never uses its fallback path.
    """
    if not utils._BRC_RUN_LOOKUP:
        raise RuntimeError("utils._BRC_RUN_LOOKUP is empty - is import_BRC.csv present?")

    expected = {(o, b) for o in range(3) for b in utils.OBC_OPTIONS}
    present = set(zip(df["outs"], df["obc"]))
    missing = expected - present
    if missing:
        raise RuntimeError(f"{RANGES_CSV} is missing situations: {sorted(missing)}")
    extra = present - expected
    if extra:
        raise RuntimeError(f"{RANGES_CSV} has unrecognized situations: {sorted(extra)}")

    sums = df.groupby(["outs", "obc"])["Rng"].sum()
    bad = sums[sums != RNG_DOMAIN]
    if not bad.empty:
        raise RuntimeError(
            f"{RANGES_CSV} Rng must sum to {RNG_DOMAIN} per situation; got:\n{bad.to_string()}"
        )

    unknown = [
        (res, obc, outs)
        for res, obc, outs in zip(df["Result"], df["obc"], df["outs"])
        if (res, obc, outs) not in utils._BRC_RUN_LOOKUP
    ]
    if unknown:
        raise RuntimeError(
            f"{len(unknown)} row(s) of {RANGES_CSV} are not in the BRC lookup: {unknown[:10]}"
        )


def build_outcome_tables(df: pd.DataFrame) -> dict[tuple[int, str], list[tuple]]:
    """Expand each situation into 501 pre-resolved outcome slots.

    Because Rng is an integer partition of the 0..500 domain, a weighted draw
    collapses to picking one of 501 slots uniformly - no cumulative weights, no
    normalization, and the runner advancement for each slot is resolved once at
    startup instead of tens of millions of times in the game loop.

    Each slot is (result, new_obc, runs, new_outs) with new_outs already capped
    at 3.
    """
    tables: dict[tuple[int, str], list[tuple]] = {}
    for (outs, obc), grp in df.groupby(["outs", "obc"]):
        slots: list[tuple] = []
        for result, rng in zip(grp["Result"], grp["Rng"]):
            new_obc, runs = utils.advance_runners(result, obc, outs)
            new_outs = min(3, outs + utils.outs_added(result))
            slots.extend([(result, new_obc, runs, new_outs)] * rng)
        if len(slots) != RNG_DOMAIN:
            raise RuntimeError(f"situation (outs={outs}, obc={obc}) expanded to {len(slots)} slots")
        tables[(outs, obc)] = slots

    # Closure check: every mid-inning state a slot can produce must itself be a
    # situation we can sample from, so the game loop needs no fallback branch.
    for (outs, obc), slots in tables.items():
        for result, new_obc, _runs, new_outs in slots:
            if new_outs < 3 and (new_outs, new_obc) not in tables:
                raise RuntimeError(
                    f"(outs={outs}, obc={obc}) result {result} leads to unsampleable "
                    f"state (outs={new_outs}, obc={new_obc})"
                )
    return tables


def _remaining_cache() -> dict[tuple[int, str], int]:
    """Precompute remaining_half_innings for every inning the guard allows."""
    return {
        (inning, half): utils.remaining_half_innings(inning, half, MLN_INNINGS)
        for inning in range(1, MAX_INNINGS + 2)
        for half in ("top", "bottom")
    }


# ── the shared continuation ───────────────────────────────────────────────────

def simulate_from(
    inning: int,
    half: str,
    outs: int,
    obc: str,
    home_score: int,
    away_score: int,
    tables: dict[tuple[int, str], list[tuple]],
    bases: dict[tuple[int, int, str], int],
    remaining_of: dict[tuple[int, str], int],
    randrange,
) -> tuple[list[int], list[int], bool]:
    """Play forward from any legal state to a decided finish.

    Returns (cells visited while home batted, cells visited while away batted,
    home_won). The caller labels those cells once the winner is known - a
    mid-game state's win probability is not knowable until the continuation
    finishes, so there is no incremental version of this.

    This is the single mechanic behind all three modes. Full-game mode calls it
    at the game opening; state-seeded and backfill call it at a reconstructed
    cell state.
    """
    home_cells: list[int] = []
    away_cells: list[int] = []

    while True:
        batting_is_home = half == "bottom"
        lead = (home_score - away_score) if batting_is_home else (away_score - home_score)
        if lead > LEAD_CLIP:
            lead = LEAD_CLIP
        elif lead < -LEAD_CLIP:
            lead = -LEAD_CLIP

        cell = bases[(remaining_of[(inning, half)], outs, obc)] + lead + LEAD_CLIP
        if batting_is_home:
            home_cells.append(cell)
        else:
            away_cells.append(cell)

        _result, new_obc, runs, new_outs = tables[(outs, obc)][randrange(RNG_DOMAIN)]
        if batting_is_home:
            home_score += runs
        else:
            away_score += runs

        if _is_walkoff_final(inning, half, new_outs, home_score, away_score):
            break
        if new_outs >= 3:
            if half == "top":
                half = "bottom"
            else:
                half = "top"
                inning += 1
                if inning > MAX_INNINGS:
                    raise RuntimeError(f"continuation exceeded {MAX_INNINGS} innings without a decision")
            outs, obc = 0, "000"
        else:
            outs, obc = new_outs, new_obc

    if home_score == away_score:
        raise RuntimeError(
            f"continuation ended tied {home_score}-{away_score} in the {half} of inning {inning}"
        )
    return home_cells, away_cells, home_score > away_score


def seed_state(remaining: int, outs: int, obc: str, batting_lead: int) -> tuple[int, str, int, int]:
    """Reconstruct a concrete (inning, half, home_score, away_score) for a cell.

    remaining -> (inning, half) inverts utils.remaining_half_innings within
    regulation, which is unique for every remaining 1..12. Extra-inning depth
    needs no separate handling: continuation from a remaining=2 cell reached in
    a real 9th behaves identically to one seeded at the regulation 6th, since
    the same capping rule governs both.

    batting_lead -> scores only has to preserve the difference. Nothing
    downstream reads an absolute score: _is_walkoff_final and every WP and
    leverage function compare home to away and nothing else.
    """
    hip = MLN_INNINGS * 2 - remaining
    inning = hip // 2 + 1
    half = "bottom" if hip % 2 == 1 else "top"
    home_lead = batting_lead if half == "bottom" else -batting_lead
    return inning, half, max(home_lead, 0), max(-home_lead, 0)


def accumulate(
    win_sums: list[int],
    counts: list[int],
    home_cells: list[int],
    away_cells: list[int],
    home_won: bool,
) -> None:
    winners, losers = (home_cells, away_cells) if home_won else (away_cells, home_cells)
    for i in winners:
        counts[i] += 1
        win_sums[i] += 1
    for i in losers:
        counts[i] += 1


# ── modes ─────────────────────────────────────────────────────────────────────

def run_full_game(games, win_sums, counts, tables, bases, remaining_of, randrange) -> int:
    plays = 0
    for _ in range(games):
        home_cells, away_cells, home_won = simulate_from(
            1, "top", 0, "000", 0, 0, tables, bases, remaining_of, randrange
        )
        plays += len(home_cells) + len(away_cells)
        accumulate(win_sums, counts, home_cells, away_cells, home_won)
    return plays


def run_state_seeded(
    cells, rollouts, win_sums, counts, tables, bases, remaining_of, randrange
) -> int:
    """Run `rollouts` continuations seeded at each of `cells`.

    Cells are (remaining, outs, obc, batting_lead) tuples.
    """
    plays = 0
    for remaining, outs, obc, lead in cells:
        inning, half, home_score, away_score = seed_state(remaining, outs, obc, lead)
        for _ in range(rollouts):
            home_cells, away_cells, home_won = simulate_from(
                inning, half, outs, obc, home_score, away_score,
                tables, bases, remaining_of, randrange,
            )
            plays += len(home_cells) + len(away_cells)
            accumulate(win_sums, counts, home_cells, away_cells, home_won)
    return plays


def all_cells() -> list[tuple[int, int, str, int]]:
    return [
        (remaining, outs, obc, lead)
        for remaining in range(1, MAX_REMAINING + 1)
        for outs in range(3)
        for obc in utils.OBC_OPTIONS
        for lead in range(-LEAD_CLIP, LEAD_CLIP + 1)
    ]


# ── accumulation across runs ──────────────────────────────────────────────────

def load_existing(path: str, bases: dict[tuple[int, int, str], int]) -> tuple[list[int], list[int]]:
    """Seed the accumulator from a previous run's output.

    Accumulation reconstructs from win_sum, never from the rounded win_prob, so
    repeated runs cannot drift. A table written before win_sum existed is still
    usable: its count is recovered as round(win_prob * n), which is exact for
    any win_prob that was rounded from a real ratio at 4 decimal places and n
    small enough for that rounding to be invertible. That is a migration path
    for one old file, not a mode to rely on, so it says so out loud.
    """
    win_sums = [0] * TOTAL_CELLS
    counts = [0] * TOTAL_CELLS
    if not os.path.exists(path):
        return win_sums, counts

    df = pd.read_csv(path, dtype={"obc": str})
    df["obc"] = df["obc"].str.strip().str.zfill(3)
    if "win_sum" in df.columns:
        sums = df["win_sum"].astype(float).round().astype(int)
    else:
        print(f"  {path} predates the win_sum column - recovering counts from win_prob * n")
        sums = (df["win_prob"].astype(float) * df["n"].astype(int)).round().astype(int)

    for remaining, outs, obc, lead, win_sum, n in zip(
        df["remaining"].astype(int), df["outs"].astype(int), df["obc"],
        df["batting_lead"].astype(int), sums, df["n"].astype(int)
    ):
        if abs(lead) > LEAD_CLIP:
            continue
        idx = bases[(remaining, outs, obc)] + lead + LEAD_CLIP
        win_sums[idx] = int(win_sum)
        counts[idx] = int(n)

    total = sum(counts)
    print(f"  Loaded {int((pd.Series(counts) > 0).sum()):,} existing cells ({total:,} labeled events) from {path}")
    return win_sums, counts


def thin_cells(counts: list[int], keys: list[tuple[int, int, str, int]], min_n: int) -> list[tuple[int, int, str, int]]:
    return [keys[i] for i, n in enumerate(counts) if n < min_n]


def new_meta() -> dict:
    return {"total_games": 0, "total_rollouts": {}, "total_plays": 0, "seeds": [],
            "runs": 0, "lead_clip": LEAD_CLIP}


def load_meta(path: str) -> dict:
    if not os.path.exists(path):
        return new_meta()
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return new_meta()


def require_compatible_pool(table_path: str, meta_path: str) -> None:
    """Refuse to accumulate onto a pool bucketed under a different lead clip.

    The clip is not only a domain bound, it decides which cell an event lands
    in. Under LEAD_CLIP=10 every true lead of 10 or more was filed into the
    boundary cell, so that cell means "10 or more" - which is a different
    quantity from the "exactly 10" the same cell means under LEAD_CLIP=18.
    Merging the two pools would silently blend those meanings in exactly the
    cells where blowout leverage is decided, so require an explicit rebuild.
    """
    if not os.path.exists(table_path):
        return
    prior = load_meta(meta_path).get("lead_clip") if os.path.exists(meta_path) else None
    if prior == LEAD_CLIP:
        return
    detail = (
        f"was built with lead_clip={prior}" if prior is not None
        else f"predates the lead_clip record in {META_JSON}, so its bucketing cannot be verified"
    )
    raise SystemExit(
        f"{table_path} {detail}, but this build clips batting_lead to +/-{LEAD_CLIP}.\n"
        f"Boundary cells mean different things under different clips, so the pools cannot be merged.\n"
        f"Rebuild with --fresh, then run --mode backfill to fill the newly-reachable cells."
    )


def save_meta(path: str, meta: dict, mode: str, seed: int, games: int, rollouts: int, plays: int) -> None:
    meta["total_games"] = meta.get("total_games", 0) + games
    by_mode = meta.setdefault("total_rollouts", {})
    if rollouts:
        by_mode[mode] = by_mode.get(mode, 0) + rollouts
    meta["total_plays"] = meta.get("total_plays", 0) + plays
    meta.setdefault("seeds", []).append(seed)
    meta["runs"] = meta.get("runs", 0) + 1
    meta["lead_clip"] = LEAD_CLIP
    meta["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)


# ── output ────────────────────────────────────────────────────────────────────

def build_table(
    win_sums: list[int],
    counts: list[int],
    keys: list[tuple[int, int, str, int]],
) -> pd.DataFrame:
    rows = [
        {
            "remaining": keys[i][0],
            "outs": keys[i][1],
            "obc": keys[i][2],
            "batting_lead": keys[i][3],
            "win_prob": round(win_sums[i] / n, 4),
            "win_sum": win_sums[i],
            "n": n,
        }
        for i, n in enumerate(counts)
        if n
    ]
    return pd.DataFrame(rows).sort_values(
        ["remaining", "outs", "obc", "batting_lead"], ascending=[False, True, True, False]
    )


def print_coverage(table: pd.DataFrame, min_n: int) -> None:
    per_state = table.groupby(["remaining", "outs", "obc"])["batting_lead"].count()
    complete = int((per_state == LEAD_SLOTS).sum())
    partial = int((per_state < LEAD_SLOTS).sum())
    all_states = MAX_REMAINING * 3 * len(_OBC_IDX)
    untouched = all_states - len(per_state)
    below_low = int((table["n"] < LOW_N).sum())
    below_min = int((table["n"] < min_n).sum()) + (TOTAL_CELLS - len(table))

    print()
    print(f"batting_lead domain:  [-{LEAD_CLIP}, {LEAD_CLIP}] ({LEAD_SLOTS} values)")
    print(f"Cells written:        {len(table):,} of {TOTAL_CELLS:,} possible")
    print(f"  n < {LOW_N}:            {below_low:,}")
    print(f"  n < {min_n} (--min-n):  {below_min:,}  <- what --mode backfill would target")
    print(f"States (remaining, outs, obc): {len(per_state)} of {all_states}")
    print(f"  full lead range:    {complete}")
    print(f"  partial lead range: {partial}")
    print(f"  never reached:      {untouched}")
    print(f"Median n per cell:    {int(table['n'].median()):,}")
    if below_min:
        print(f"\nFollow up with: python simulate_win_probability.py --mode backfill --min-n {min_n}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("full-game", "state-seeded", "backfill"), default="full-game")
    ap.add_argument("--games", type=int, default=200_000,
                    help="games to simulate in full-game mode (default 200000)")
    ap.add_argument("--rollouts-per-state", type=int, default=500,
                    help="continuations per seeded cell (default 500)")
    ap.add_argument("--min-n", type=int, default=200,
                    help="backfill targets cells below this n (default 200)")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed; defaults to OS entropy so repeated runs add new games")
    ap.add_argument("--fresh", action="store_true",
                    help="discard the existing pool and start over (default is to accumulate)")
    ap.add_argument("--out", default=OUT_CSV, help=f"output CSV path (default {OUT_CSV})")
    args = ap.parse_args()

    if args.out.strip().replace("\\", "/").split("/")[-1] == PROTECTED_CSV:
        raise SystemExit(f"refusing to write {PROTECTED_CSV} - this table is built to be compared, not swapped in")
    if args.mode == "full-game" and args.games < 1:
        raise SystemExit("--games must be at least 1")
    if args.mode in ("state-seeded", "backfill") and args.rollouts_per_state < 1:
        raise SystemExit("--rollouts-per-state must be at least 1")
    if args.mode == "backfill" and args.fresh:
        raise SystemExit("--mode backfill reads the existing table to find thin cells; --fresh would erase it")

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2 ** 32)
    rng = random.Random(seed)

    df = load_result_ranges()
    validate_result_ranges(df)
    tables = build_outcome_tables(df)
    bases, keys = _build_cell_keys()
    remaining_of = _remaining_cache()
    print(f"Loaded {len(df)} rows / {len(tables)} situations from {RANGES_CSV}")
    print(f"Mode: {args.mode}   seed: {seed}{' (from OS entropy)' if args.seed is None else ''}")

    meta_path = os.path.join(os.path.dirname(args.out) or ".", META_JSON)
    if args.fresh:
        print("Starting fresh - any existing pool is discarded")
        win_sums, counts = [0] * TOTAL_CELLS, [0] * TOTAL_CELLS
        meta = new_meta()
    else:
        require_compatible_pool(args.out, meta_path)
        win_sums, counts = load_existing(args.out, bases)
        meta = load_meta(meta_path)

    games = rollouts = 0
    cells: list[tuple[int, int, str, int]] = []
    if args.mode == "full-game":
        games = args.games
        print(f"Simulating {games:,} full games...")
    else:
        if args.mode == "backfill":
            if not os.path.exists(args.out):
                print(f"  No {args.out} yet, so every cell counts as thin - this run is "
                      f"equivalent to --mode state-seeded")
            cells = thin_cells(counts, keys, args.min_n)
            if not cells:
                print(f"No cells below n={args.min_n} - nothing to backfill.")
                return
            print(f"Backfilling {len(cells):,} cells below n={args.min_n} "
                  f"at {args.rollouts_per_state:,} rollouts each...")
            if args.rollouts_per_state < args.min_n:
                print(f"  Note: --rollouts-per-state ({args.rollouts_per_state}) is below --min-n "
                      f"({args.min_n}), so a cell starting from zero cannot clear the threshold in")
                print("  one pass. Raise it, or expect to run backfill again.")
        else:
            cells = all_cells()
            print(f"Seeding all {len(cells):,} cells at {args.rollouts_per_state:,} rollouts each...")
        rollouts = len(cells) * args.rollouts_per_state

    t0 = time.perf_counter()
    if args.mode == "full-game":
        plays = run_full_game(games, win_sums, counts, tables, bases, remaining_of, rng.randrange)
    else:
        plays = run_state_seeded(
            cells, args.rollouts_per_state, win_sums, counts, tables, bases, remaining_of, rng.randrange
        )
    elapsed = time.perf_counter() - t0

    units = games if args.mode == "full-game" else rollouts
    label = "games" if args.mode == "full-game" else "rollouts"
    print(
        f"Done in {elapsed:.1f}s - {plays:,} plate appearances "
        f"({plays / max(units, 1):.1f} per continuation, {units / max(elapsed, 1e-9):,.0f} {label}/s)"
    )

    table = build_table(win_sums, counts, keys)
    bad = table[table["win_sum"] > table["n"]]
    if not bad.empty:
        raise RuntimeError(f"win_sum exceeds n in {len(bad)} cell(s) - accumulation is broken")
    table.to_csv(args.out, index=False)
    save_meta(meta_path, meta, args.mode, seed, games, rollouts, plays)
    print(f"Saved {len(table):,} cells to {args.out}")
    print(f"Cumulative across {meta['runs']} run(s): {meta['total_games']:,} full games, "
          f"{sum(meta['total_rollouts'].values()):,} seeded rollouts, {meta['total_plays']:,} plate appearances")
    print_coverage(table, args.min_n)


if __name__ == "__main__":
    main()

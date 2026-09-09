"""
Full-league plays-data integrity audit.

Read-only. Pulls every row from Supabase's `plays` and `games` tables and
cross-checks them for internal and cross-row consistency. Writes one CSV per
issue category to --out (default tools/probe_output/audit).

Conventions this script depends on (verified against live data before
writing this, not assumed):
  - On a `plays` row, obc/outs/on_first/on_second/on_third/away_score/
    home_score are all the state BEFORE this play happens. A play's effect
    (new baserunners, new outs, runs scored) only becomes visible on the
    NEXT row in the same game (or, for a game's last play, in `games`'
    final away_score/home_score).
  - `play_code` is "Outs_ObcCode_Result" (e.g. "1_4_DP"), ObcCode 0-7 using
    the sequential encoding 0=empty,1=1B,2=2B,3=3B,4=1&2B,5=1&3B,6=2&3B,
    7=loaded (see _BRC_INT_TO_OBC below) - the SAME encoding import_BRC.csv's
    own Situation/OBC columns use.
  - `play_num` = season*10_000_000 + game_seq*1000 + play_index, where
    play_index is 1-indexed and contiguous within a game, and game_seq is
    the game's sequence number within its season (verified against several
    games spanning season 8 and season 13).
  - `obc` is not raw sheet data - utils.py's read_mln_plays_from_sheet
    already reconciles on_first/on_second/on_third ("cell" state) against
    play_code's own digit when they disagree: same runner count -> trust
    the cells outright (they carry real player ids); different count ->
    OR the two patterns together, never dropping a cell-known runner. This
    script recomputes that same policy fresh against the columns as they
    stand today, so it only flags conflicts that policy can't resolve -
    not the already-tolerated repair cases.

Run from the repo root:
    python tools/plays_data_audit.py [--out tools/probe_output/audit] [--brc import_BRC.csv]
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import database as db

_BRC_INT_TO_OBC = {0: "000", 1: "001", 2: "010", 3: "100", 4: "011", 5: "101", 6: "110", 7: "111"}
_HALVES = ("top", "bottom")


# --------------------------------------------------------------------------- helpers

def load_brc_table(path: str) -> dict[str, dict]:
    """Situation string -> {runs, eouts, obc_after}. eOuts is import_BRC.csv's
    own absolute ending out count for the situation, not a delta."""
    table: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            situation = (row.get("Situation") or "").strip()
            if not situation:
                continue
            try:
                runs = float(row["Runs"])
                eouts = int(row["eOuts"])
                obc_after = _BRC_INT_TO_OBC[int(row["OBC"])]
            except (KeyError, ValueError):
                continue
            table[situation] = {"runs": runs, "eouts": eouts, "obc_after": obc_after}
    return table


def decompose_play_num(play_num: int) -> tuple[int, int, int]:
    season = play_num // 10_000_000
    rem = play_num % 10_000_000
    game_seq = rem // 1000
    play_idx = rem % 1000
    return season, game_seq, play_idx


def _occupied(v) -> bool:
    return v not in (None, "", "-", "0", 0)


def cell_obc(on_first, on_second, on_third) -> str:
    bits = (1 if _occupied(on_first) else 0) | (2 if _occupied(on_second) else 0) | (4 if _occupied(on_third) else 0)
    return ("1" if bits & 4 else "0") + ("1" if bits & 2 else "0") + ("1" if bits & 1 else "0")


def playcode_parts(play_code) -> tuple[int, str, str] | None:
    if not play_code:
        return None
    parts = str(play_code).split("_")
    if len(parts) < 3:
        return None
    try:
        outs = int(parts[0])
        obc = _BRC_INT_TO_OBC.get(int(parts[1]))
    except ValueError:
        return None
    if obc is None:
        return None
    return outs, obc, "_".join(parts[2:])


def reconcile_obc(cell: str, playcode_obc: str | None) -> tuple[str, bool]:
    """Alex's documented repair policy from utils.py's sheet importer,
    reapplied to the columns as currently stored. Returns (expected_obc,
    was_a_resolvable_conflict)."""
    if playcode_obc is None or cell == playcode_obc:
        return cell, False
    if cell.count("1") != playcode_obc.count("1"):
        merged = "".join("1" if (a == "1" or b == "1") else "0" for a, b in zip(cell, playcode_obc))
        if merged != cell:
            return merged, True
    return cell, False


def next_half(inning: int, half: str) -> tuple[int, str]:
    return (inning, "bottom") if half == "top" else (inning + 1, "top")


def detect_score_convention(rows: list[dict]) -> bool:
    """Does this (league, season)'s away_score/home_score store the total
    AFTER each play (post-play), or BEFORE it (pre-play)?

    Mirrors key_moments_build.py's _detect_archive_score_convention exactly
    (same solo-HR vote, same tie-break) - NOT assumed constant across
    seasons: verified there that MLN seasons 1-10 are post-play but 11+
    (archive seasons 11-12, and the live current-season sheet) are pre-play,
    presumably a scorekeeping process change. Re-derived per (league,
    season) here rather than hardcoding that boundary, same reasoning.
    """
    by_game = defaultdict(list)
    for p in rows:
        by_game[p.get("game_id", p.get("game_code"))].append(p)
    post_votes = pre_votes = 0
    for gp in by_game.values():
        ordered = sorted(gp, key=lambda p: p["play_num"])
        for i, p in enumerate(ordered):
            if p.get("result") != "HR" or p.get("obc") != "000":
                continue
            total_this = (p["away_score"] or 0) + (p["home_score"] or 0)
            total_prev = ((ordered[i - 1]["away_score"] or 0) + (ordered[i - 1]["home_score"] or 0)) if i > 0 else 0
            if total_this == total_prev + 1:
                post_votes += 1
            elif i + 1 < len(ordered):
                nxt = ordered[i + 1]
                total_next = (nxt["away_score"] or 0) + (nxt["home_score"] or 0)
                if total_next == total_this + 1:
                    pre_votes += 1
    return post_votes >= pre_votes


# --------------------------------------------------------------------------- checks

def check_play_num_gaps(plays_by_game: dict) -> list[dict]:
    out = []
    for game_id, rows in plays_by_game.items():
        idxs = []
        for p in rows:
            _, _, idx = decompose_play_num(p["play_num"])
            idxs.append(idx)
        expected = list(range(1, len(idxs) + 1))
        if sorted(idxs) != expected:
            missing = sorted(set(expected) - set(idxs))
            seen = set()
            dupes = sorted({i for i in idxs if i in seen or seen.add(i)})
            out.append({
                "game_id": game_id, "league": rows[0]["league"], "season": rows[0]["season"],
                "game_type": rows[0].get("game_type"), "n_plays": len(idxs),
                "missing_play_idx": ";".join(map(str, missing)),
                "duplicate_play_idx": ";".join(map(str, dupes)),
                "first_play_num": min(p["play_num"] for p in rows),
                "last_play_num": max(p["play_num"] for p in rows),
            })
    return out


def check_game_seq_gaps(plays: list[dict]) -> list[dict]:
    by_league_season = defaultdict(dict)  # (league, season) -> {game_seq: game_id}
    season_mismatches = []
    for p in plays:
        season_enc, game_seq, _ = decompose_play_num(p["play_num"])
        by_league_season[(p["league"], p["season"])][game_seq] = p.get("game_id", p.get("game_code"))
        if p["season"] != season_enc:
            season_mismatches.append({
                "play_id": p.get("id", p["play_num"]), "game_id": p.get("game_id", p.get("game_code")), "play_num": p["play_num"],
                "league": p["league"], "stored_season": p["season"], "play_num_season": season_enc,
            })
    gaps = []
    for (league, season), seq_to_game in by_league_season.items():
        seqs = sorted(seq_to_game)
        if not seqs:
            continue
        full = set(range(seqs[0], seqs[-1] + 1))
        missing = sorted(full - set(seqs))
        if missing:
            gaps.append({
                "league": league, "season": season, "min_game_seq": seqs[0], "max_game_seq": seqs[-1],
                "n_games_present": len(seqs), "missing_game_seq": ";".join(map(str, missing)),
            })
    return gaps, season_mismatches


def check_brc_missing(plays: list[dict], brc: dict) -> list[dict]:
    out = []
    seen = set()
    for p in plays:
        pc = p.get("play_code")
        if pc and pc not in brc and pc not in seen:
            seen.add(pc)
            out.append({"play_code": pc, "example_play_id": p.get("id", p["play_num"]), "example_game_id": p.get("game_id", p.get("game_code")),
                        "result": p.get("result"), "n_occurrences": sum(1 for q in plays if q.get("play_code") == pc)})
    return out


def check_row_internal(plays: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (obc_conflicts, outs_mismatches, result_mismatches)."""
    obc_conflicts, outs_mismatches, result_mismatches = [], [], []
    for p in plays:
        parts = playcode_parts(p.get("play_code"))
        co = cell_obc(p.get("on_first"), p.get("on_second"), p.get("on_third"))
        if parts is None:
            continue
        outs_pc, obc_pc, result_pc = parts
        expected_obc, _ = reconcile_obc(co, obc_pc)
        stored_obc = str(p.get("obc") or "000")
        if stored_obc != expected_obc:
            obc_conflicts.append({
                "play_id": p.get("id", p["play_num"]), "game_id": p.get("game_id", p.get("game_code")), "play_num": p["play_num"],
                "league": p["league"], "season": p["season"], "play_code": p["play_code"],
                "stored_obc": stored_obc, "cell_obc": co, "playcode_obc": obc_pc,
                "reconciled_expected_obc": expected_obc,
                "on_first": p.get("on_first"), "on_second": p.get("on_second"), "on_third": p.get("on_third"),
            })
        if p.get("outs") is not None and int(p["outs"]) != outs_pc:
            outs_mismatches.append({
                "play_id": p.get("id", p["play_num"]), "game_id": p.get("game_id", p.get("game_code")), "play_num": p["play_num"],
                "league": p["league"], "season": p["season"], "play_code": p["play_code"],
                "stored_outs": p.get("outs"), "playcode_outs": outs_pc,
            })
        if p.get("result") and str(p["result"]) != result_pc:
            result_mismatches.append({
                "play_id": p.get("id", p["play_num"]), "game_id": p.get("game_id", p.get("game_code")), "play_num": p["play_num"],
                "league": p["league"], "season": p["season"], "play_code": p["play_code"],
                "stored_result": p.get("result"), "playcode_result": result_pc,
            })
    return obc_conflicts, outs_mismatches, result_mismatches


def check_game_flow(plays_by_game: dict, brc: dict, games_by_id: dict, post_play_seasons: set[tuple[str, int]]):
    """Row-to-row baserunner/outs flow, inning/half sequencing, score flow,
    and final-boxscore reconciliation - all within one pass per game.

    Score-flow direction depends on this game's (league, season) convention
    (see detect_score_convention): post-play seasons already bake this row's
    own runs into its own away_score/home_score, so those are added BEFORE
    comparing; pre-play seasons only reflect this row's runs starting the
    NEXT row, so they're added AFTER comparing.
    """
    baserunner_flow, inning_half_errors, score_flow, final_score_mismatches = [], [], [], []

    for game_id, rows in plays_by_game.items():
        rows = sorted(rows, key=lambda p: p["play_num"])
        post_play = (rows[0]["league"], rows[0]["season"]) in post_play_seasons
        prev = None
        exp_obc, exp_outs = "000", 0
        exp_away, exp_home = rows[0]["away_score"], rows[0]["home_score"]
        pending_runs = None  # runs owed to `side` from the previous play, applied before comparing this row (pre-play only)
        pending_side = None
        last_brc = None

        for p in rows:
            is_new_half = prev is None or (p["inning"], p["half"]) != (prev["inning"], prev["half"])

            if prev is not None and is_new_half:
                expected_next = next_half(prev["inning"], prev["half"])
                if (p["inning"], p["half"]) != expected_next:
                    inning_half_errors.append({
                        "game_id": game_id, "league": p["league"], "season": p["season"],
                        "prev_play_num": prev["play_num"], "prev_inning": prev["inning"], "prev_half": prev["half"],
                        "play_num": p["play_num"], "inning": p["inning"], "half": p["half"],
                        "expected_inning": expected_next[0], "expected_half": expected_next[1],
                    })

            if is_new_half:
                exp_obc, exp_outs = "000", 0

            if p.get("obc") != exp_obc or (p.get("outs") is not None and int(p["outs"]) != exp_outs):
                baserunner_flow.append({
                    "game_id": game_id, "league": p["league"], "season": p["season"],
                    "play_num": p["play_num"], "inning": p["inning"], "half": p["half"],
                    "prev_play_num": prev["play_num"] if prev else None,
                    "expected_obc": exp_obc, "stored_obc": p.get("obc"),
                    "expected_outs": exp_outs, "stored_outs": p.get("outs"),
                    "prev_play_code": prev["play_code"] if prev else None,
                })
                # resync to the row's own state so one root error doesn't cascade
                exp_obc, exp_outs = str(p.get("obc") or "000"), int(p.get("outs") or 0)

            entry = brc.get(p.get("play_code"))
            side = "away" if p["half"] == "top" else "home"

            if post_play:
                # This row's own runs land in its own score - add before comparing.
                if entry is not None:
                    if side == "away":
                        exp_away += entry["runs"]
                    else:
                        exp_home += entry["runs"]
                if p.get("away_score") != exp_away or p.get("home_score") != exp_home:
                    score_flow.append({
                        "game_id": game_id, "league": p["league"], "season": p["season"],
                        "convention": "post_play", "play_num": p["play_num"],
                        "prev_play_num": prev["play_num"] if prev else None,
                        "expected_away_score": exp_away, "stored_away_score": p.get("away_score"),
                        "expected_home_score": exp_home, "stored_home_score": p.get("home_score"),
                        "this_play_code": p.get("play_code"),
                    })
                exp_away, exp_home = p.get("away_score"), p.get("home_score")
            else:
                # Previous row's runs land starting this row - add pending, then compare.
                if pending_side == "away":
                    exp_away += pending_runs
                elif pending_side == "home":
                    exp_home += pending_runs
                if p.get("away_score") != exp_away or p.get("home_score") != exp_home:
                    score_flow.append({
                        "game_id": game_id, "league": p["league"], "season": p["season"],
                        "convention": "pre_play", "play_num": p["play_num"],
                        "prev_play_num": prev["play_num"] if prev else None,
                        "expected_away_score": exp_away, "stored_away_score": p.get("away_score"),
                        "expected_home_score": exp_home, "stored_home_score": p.get("home_score"),
                        "prev_play_code": prev["play_code"] if prev else None,
                    })
                exp_away, exp_home = p.get("away_score"), p.get("home_score")
                pending_runs, pending_side = (entry["runs"], side) if entry is not None else (None, None)

            if entry is not None:
                eouts = entry["eouts"]
                exp_obc = "000" if eouts >= 3 else entry["obc_after"]
                exp_outs = 0 if eouts >= 3 else eouts
            else:
                exp_obc = str(p.get("obc") or "000")  # unknown play_code: placeholder to avoid false positive next loop
                exp_outs = int(p.get("outs") or 0)

            last_brc = entry
            prev = p

        # final boxscore check
        game = games_by_id.get(game_id)
        if game is not None and last_brc is not None:
            side = "away" if rows[-1]["half"] == "top" else "home"
            if post_play:
                final_away, final_home = rows[-1]["away_score"], rows[-1]["home_score"]
            else:
                final_away = rows[-1]["away_score"] + (last_brc["runs"] if side == "away" else 0)
                final_home = rows[-1]["home_score"] + (last_brc["runs"] if side == "home" else 0)
            if game.get("away_score") != final_away or game.get("home_score") != final_home:
                final_score_mismatches.append({
                    "game_id": game_id, "league": rows[-1]["league"], "season": rows[-1]["season"],
                    "game_type": rows[-1]["game_type"],
                    "computed_final_away": final_away, "boxscore_away": game.get("away_score"),
                    "computed_final_home": final_home, "boxscore_home": game.get("home_score"),
                    "last_play_num": rows[-1]["play_num"], "last_play_code": rows[-1]["play_code"],
                })

    return baserunner_flow, inning_half_errors, score_flow, final_score_mismatches


# --------------------------------------------------------------------------- main

def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tools/probe_output/audit")
    ap.add_argument("--brc", default="import_BRC.csv")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading import_BRC.csv...")
    brc = load_brc_table(args.brc)
    print(f"  {len(brc)} situations loaded")

    print("Fetching plays and games from Supabase...")
    plays = db._fetch_all(db._client().table("plays").select("*"))
    games = db._fetch_all(db._client().table("games").select("*"))
    games_by_id = {g["id"]: g for g in games}
    print(f"  {len(plays)} plays, {len(games)} games")

    plays_by_game = defaultdict(list)
    for p in plays:
        plays_by_game[p["game_id"]].append(p)

    by_league_season = defaultdict(list)
    for p in plays:
        by_league_season[(p["league"], p["season"])].append(p)
    post_play_seasons = {ls for ls, rows in by_league_season.items() if detect_score_convention(rows)}
    print("\nScore convention by (league, season) - post-play marked *:")
    for ls in sorted(post_play_seasons | set(by_league_season)):
        print(f"  {ls}: {'post-play *' if ls in post_play_seasons else 'pre-play'}")

    results: dict[str, list[dict]] = {}

    results["play_num_gaps"] = check_play_num_gaps(plays_by_game)
    gaps, season_mismatches = check_game_seq_gaps(plays)
    results["game_seq_gaps"] = gaps
    results["play_num_season_mismatches"] = season_mismatches
    results["brc_situation_missing"] = check_brc_missing(plays, brc)

    obc_conflicts, outs_mismatches, result_mismatches = check_row_internal(plays)
    results["obc_unresolved_conflicts"] = obc_conflicts
    results["outs_vs_playcode_mismatches"] = outs_mismatches
    results["result_vs_playcode_mismatches"] = result_mismatches

    baserunner_flow, inning_half_errors, score_flow, final_score_mismatches = check_game_flow(
        plays_by_game, brc, games_by_id, post_play_seasons
    )
    results["baserunner_flow_mismatches"] = baserunner_flow
    results["inning_half_sequence_errors"] = inning_half_errors
    results["score_flow_mismatches"] = score_flow
    results["game_final_score_mismatches"] = final_score_mismatches

    print("\n=== Audit summary ===")
    for name, rows in results.items():
        print(f"  {name}: {len(rows)}")
        write_csv(out_dir / f"{name}.csv", rows)

    print(f"\nCSV reports written to {out_dir}/")


if __name__ == "__main__":
    main()

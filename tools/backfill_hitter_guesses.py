"""One-off: apply the "hitter's PioBall swing = their real swing" rule to already-graded pitches.

Run from the repo root (dry run by default, add --apply to write):
    python tools/backfill_hitter_guesses.py
    python tools/backfill_hitter_guesses.py --apply
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swing_game as sg  # noqa: E402
import utils  # noqa: E402


def main(apply: bool) -> None:
    pitches = (sg._t("swing_pitches").select("*").not_.is_("graded_at", "null").order("seq").execute().data)
    roster = {n.strip().lower(): u for n, u in sg.roster().items()}
    for p in pitches:
        swing, batter = p.get("swing"), p.get("batter")
        tag = f"pitch id={p['id']} game={p['game_code']} seq={p['seq']} batter={batter} swing={swing}"
        uid = roster.get(str(batter or "").strip().lower())
        if uid is None or swing is None or not 1 <= int(swing) <= 1000:
            print(f"SKIP  {tag} (batter not on roster with a Discord id, or no real swing)")
            continue
        cur = sg._t("swing_guesses").select("guess").eq("pitch_id", p["id"]).eq("user_id", uid).execute().data
        if cur and int(cur[0]["guess"]) == int(swing):
            print(f"OK    {tag} (already matches)")
            continue
        print(f"{'FIX ' if apply else 'WOULD'} {tag}: hitter guess {cur[0]['guess'] if cur else 'none'} -> {swing}")
        if not apply:
            continue
        sg._sync_hitter_guess(p, int(swing))
        ranges = [tuple(r) for r in (p.get("ranges") or utils.RESULT_RANGES)]
        d = utils.circular_diff(int(p["pitch"]), int(swing))
        sg._t("swing_guesses").update({"diff": d, "result": utils._diff_to_result(d, ranges)}) \
            .eq("pitch_id", p["id"]).eq("user_id", uid).execute()


if __name__ == "__main__":
    main("--apply" in sys.argv)

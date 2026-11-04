"""Rebuilds score_fix.json: the final score of each S1-S10 game from its last play in the Supabase plays table.

The archive Games tab has stale scores in about 50 S1-S10 games (24 of them show a tie). Plays scores are post-play through S10
(key_moments_build._detect_archive_score_convention), so a game's last play carries its real final score (away, home). S11 on
needs no fix. Game 050607 has only 3 plays on file, so its score (KYM 2 at HMH 3) was confirmed by hand."""
import sys, json, concurrent.futures as cf
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import database as db, utils

ARCHIVE = '1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg'
games = [g for g in utils.read_mln_games_from_sheet(ARCHIVE) if g['season'] <= 10]
client = db._client()


def last_play(g):
    for _ in range(3):
        try:
            r = (client.table("plays").select("play_num, away_score, home_score").eq("game_code", str(g['game_code']))
                 .order("play_num", desc=True).limit(1).execute().data)
            return str(g['game_code']), (r[0] if r else None)
        except Exception:
            pass
    return str(g['game_code']), None


fix = {}
with cf.ThreadPoolExecutor(6) as ex:
    for code, r in ex.map(last_play, games):
        if r and code != '050607':
            fix[code] = [r['away_score'], r['home_score']]
fix['050607'] = [2, 3]
json.dump(fix, open(HERE / 'score_fix.json', 'w'), separators=(',', ':'), sort_keys=True)
print(len(fix), 'games overridden from the last play')

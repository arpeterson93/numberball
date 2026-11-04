"""Builds the data behind the MLN Playoff Odds Explorer: one row per team per session (record, run differential, games back,
rulebook seed) and one per team-season, for S1-S13. Reads the archive and live MLN sheets; run from anywhere.
Writes playoff_explorer/dist/team_sessions.json and team_seasons.json (then build_standalone.py makes the page)."""
import sys, json, collections, os
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import utils, sync_plays, draft_order
A = '1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg'
T = utils.read_mln_teams_from_sheet(A); G = utils.read_mln_games_from_sheet(A)
T += [dict(t, season=13) for t in utils.read_mln_teams_from_sheet(sync_plays._MLN_SHEET_ID)]
G += utils.read_mln_games_from_sheet(sync_plays._MLN_SHEET_ID)
CUR = 13

# Games-tab scores are stale in some S1-S10 games (all 24 'ties' and ~27 others; none change a winner). The archive's plays scores
# are post-play through S10 (key_moments_build._detect_archive_score_convention: S11+ switch to pre-play), so the last play of a
# game carries its real final score (away, home). Using them reproduces the Teams tab's run differentials (S2, S3 exactly).
SCORE_FIX = json.load(open(HERE / 'score_fix.json'))
for _g in G:
    if str(_g['game_code']) in SCORE_FIX:
        _g['away_score'], _g['home_score'] = SCORE_FIX[str(_g['game_code'])]

# Early seasons have regular-season games that ended with equal scores. The Games tab gives no winner for them, but the Teams
# tab's own records do: each tie was credited to one side. These are the winners recovered from the Teams tab (S1-S3 resolve
# uniquely and reproduce every team's record exactly; S4-S5 leave a couple of one-game differences that come from the
# playoff games the Teams tab folds into those seasons).
TIE_WINNER = {'010102': 'KYM', '010806': 'VAN', '011105': 'SUN', '011106': 'LPJ', '011204': 'SUN',
              '020107': 'HFX', '020203': 'HMH', '020607': 'GHG', '020803': 'SMD', '020907': 'REK', '021406': 'MAL',
              '030302': 'BBG', '030303': 'MAL', '030403': 'GHG', '030502': 'POR', '030706': 'KYM', '031104': 'SUN',
              '040102': 'ACP', '040203': 'POR', '040802': 'HFX', '041205': 'NO', '041308': 'SMD', '050607': 'HMH'}
UNRESOLVED = []
def result(g):
    a, h = g['away_team'], g['home_team']
    if g['away_score'] != g['home_score']:
        return (a, h) if g['away_score'] > g['home_score'] else (h, a)
    w = TIE_WINNER.get(str(g['game_code']))
    if w is None:
        UNRESOLVED.append(g['game_code']); w = h
    return (w, a if w == h else h)
FR = {}
for cur, olds in {'ACP': [], 'GHG': [], 'RLY': ['MAL', 'OGO'], 'POR': [], 'ASS': ['CIN'], 'HMH': [], 'REK': ['OO'], 'SMD': [],
                  'BBEG': ['LPJ', 'BBG'], 'HFX': ['VAN'], 'SUN': [], 'GRZ': [], 'CAR': ['KYM'], 'KC': ['OAK', 'SHH', 'BUF'],
                  'MIA': ['BFB'], 'RC': ['NO']}.items():
    FR[cur] = cur
    for o in olds: FR[o] = cur
ts_rows, tsn_rows = [], []
LRANK = {}
for season in range(1, 14):
    tm = {t['abbrev']: t for t in T if t['season'] == season}
    LRANK.clear()
    gm = [g for g in G if g['season'] == season and utils.game_is_final(g)]
    reg = sorted([g for g in gm if g.get('type') != 'Post'], key=lambda g: (g['session_number'], g['game_code']))
    post = sorted([g for g in gm if g.get('type') == 'Post'], key=lambda g: g['game_code'])
    # playoff outcome
    played, lost, champ = collections.Counter(), {}, None
    rounds = collections.Counter()
    # a playoff round is a session (so teams with byes, as in S2, are placed by the round they lost in, not by games played)
    sess_idx = {x: i + 1 for i, x in enumerate(sorted({g['session_number'] for g in post}))}
    for g in post:
        a, h = g['away_team'], g['home_team']
        for t in (a, h): played[t] += 1
        win, lose = result(g)
        lost[lose] = sess_idx[g['session_number']]
    R = max(sess_idx.values()) if sess_idx else 0
    names = {R: 'Lost ToOS', R - 1: 'Lost LCR', R - 2: 'Lost WC'}
    finish = {}
    for a in tm:
        if season == CUR and not post: finish[a] = 'In progress'
        elif a not in played: finish[a] = 'Missed'
        elif a in lost: finish[a] = names.get(lost[a], 'Lost WC')
        else: finish[a] = 'Champion'
    made = {a: (1 if finish[a] not in ('Missed', 'In progress') else 0) for a in tm}
    # cumulative record by session
    sessions = sorted({g['session_number'] for g in reg})
    W = collections.Counter(); L = collections.Counter(); RD = collections.Counter()
    final = {}
    for s in sessions:
        for g in [g for g in reg if g['session_number'] == s]:
            a, h = g['away_team'], g['home_team']
            win, lose = result(g)
            m = abs(g['away_score'] - g['home_score'])
            W[win] += 1; L[lose] += 1; RD[win] += m; RD[lose] -= m
        gg = {a: (W[a] - L[a]) / 2 for a in tm}
        bgames = []
        for g in reg:
            if g['session_number'] > s: continue
            g2 = dict(g)
            if g2['away_score'] == g2['home_score']:
                w_, _ = result(g)
                g2['away_score' if w_ == g['away_team'] else 'home_score'] += 1
            bgames.append(g2)
        bk = draft_order._Book(list(tm.values()), bgames)
        # division leader and wild card reference, per league
        gb_div, gb_wc = {}, {}
        for lg in sorted({t['sub_league'] for t in tm.values()}):
            teams = [a for a in tm if tm[a]['sub_league'] == lg]
            key = lambda a: (gg[a], RD[a])
            leaders = []
            divs = sorted({tm[a]['division'] for a in teams if tm[a]['division']})
            for d in divs:
                dt = [a for a in teams if tm[a]['division'] == d]
                ld = draft_order._order(bk, dt, [], 'd')[0]; leaders.append(ld)
                for a in dt: gb_div[a] = gg[ld] - gg[a]
                oth = [a for a in dt if a != ld]
                # the leader's number is games ahead of second place (negative); a tie for the lead is 0 for both
                if oth: gb_div[ld] = -(gg[ld] - max(gg[a] for a in oth))
            if len(leaders) == 2:
                rest = draft_order._order(bk, [a for a in teams if a not in leaders], [], 'w')
                ref = rest[1]
                for a in rest: gb_wc[a] = gg[ref] - gg[a]
                order = draft_order._order(bk, leaders, [], 's') + rest
                for i, a in enumerate(order): LRANK[(s, a)] = (i + 1, i + 1 if i < 4 else 0)
        for a in tm:
            ts_rows.append({'season': season, 'team': a, 'league': tm[a]['sub_league'], 'division': tm[a]['division'] or '', 'fr': FR.get(a, a),
                            'session': s, 'wins': W[a], 'losses': L[a], 'rd': RD[a], 'gb_div': gb_div.get(a), 'gb_wc': gb_wc.get(a),
                            'finish': finish[a], 'made': made[a] if finish[a] != 'In progress' else None,
                            'final_wins': None, 'complete': 0 if season == CUR and not post else 1,
                            **({'lrank': LRANK[(s, a)][0], 'seed': LRANK[(s, a)][1]} if (s, a) in LRANK else {})})
    for a in tm:
        tsn_rows.append({'season': season, 'team': a, 'league': tm[a]['sub_league'], 'division': tm[a]['division'] or '', 'fr': FR.get(a, a),
                         'wins': W[a], 'losses': L[a], 'run_diff': RD[a], 'finish': finish[a],
                         'made': made[a] if finish[a] != 'In progress' else None,
                         'complete': 0 if season == CUR and not post else 1})
fw = {(r['season'], r['team']): r['wins'] for r in tsn_rows}
for r in ts_rows: r['final_wins'] = fw[(r['season'], r['team'])]
for r in ts_rows:
    r['gb_div'] = None if r['gb_div'] is None else round(r['gb_div'], 1) + 0.0
    r['gb_wc'] = None if r['gb_wc'] is None else round(r['gb_wc'], 1)
o = str(HERE / 'dist')
os.makedirs(o, exist_ok=True)
json.dump(ts_rows, open(o + '/team_sessions.json', 'w'), separators=(',', ':'))
json.dump(tsn_rows, open(o + '/team_seasons.json', 'w'), separators=(',', ':'))
print(len(ts_rows), len(tsn_rows), os.path.getsize(o + '/team_sessions.json'), os.path.getsize(o + '/team_seasons.json'))
c = collections.Counter((r['season'], r['finish']) for r in tsn_rows)
for s in range(1, 14): print(s, {k[1]: v for k, v in c.items() if k[0] == s})
# headline: teams 4-5 after session 9, seasons 3-12
x = [r for r in ts_rows if r['session'] == 9 and r['wins'] == 4 and r['losses'] == 5 and 3 <= r['season'] <= 12]
print('4-5 after 9:', len(x), sum(r['made'] for r in x))

print('unmapped', sorted({r['team'] for r in ts_rows if r['team'] not in FR}))
print('franchises', len({r['fr'] for r in ts_rows}))
x = [r for r in ts_rows if r['session'] == 14 and 3 <= r['season'] <= 12 and r['gb_div'] is not None and r['gb_div'] < 0]
print('outright leaders after 14', len(x), sum(r['made'] for r in x))

print('unresolved ties (assigned to home):', UNRESOLVED)

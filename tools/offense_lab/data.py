"""Data helpers for the hitting lab: league pitcher mix, team rosters, batting-order estimates, lineup legality."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
import itertools
import pandas as pd
import offense_sim as o

CACHE = ROOT / "tools/diff_lab/cache"
INFIELD = ("1B", "2B", "3B", "SS")
OUTFIELD = ("LF", "CF", "RF")
DEFENSE = ("C",) + INFIELD + OUTFIELD


def load_players() -> pd.DataFrame:
    pl = pd.read_pickle(CACHE / "players.pkl")
    pl = pl[(pl.league == "MLN") & ~pl.s_id.astype(str).str.startswith("R_") & pl.player_id.notna()].copy()
    pl["player_id"] = pl.player_id.astype(int)
    pl["season"] = pl.season.astype(int)
    return pl.drop_duplicates(["season", "player_id"])


def load_plays() -> pd.DataFrame:
    return pd.read_pickle(CACHE / "plays.pkl")


def pitcher_mix(season: int, top: int = 25, plays=None, players=None) -> o.PitcherMix:
    """League pitcher builds for a season weighted by batters faced; the `top` most-faced builds, renormalised."""
    plays = load_plays() if plays is None else plays
    players = load_players() if players is None else players
    s = plays[(plays.season == season) & (plays.play_type == "Swing") & (plays.game_type == "live")]
    p = players[players.season == season].set_index("player_id")
    acc: dict = {}
    for pid, n in s.groupby("pitcher_id").size().items():
        if pid in p.index:
            r = p.loc[pid]
            if r[["mov", "cmd", "vel", "awr"]].notna().all():
                k = (r["hand"] or "R", int(r["mov"]), int(r["cmd"]), int(r["vel"]), int(r["awr"]))
                acc[k] = acc.get(k, 0) + n
    best = sorted(acc.items(), key=lambda x: -x[1])[:top]
    return o.PitcherMix.of([(w, o.Pitcher(*k)) for k, w in best])


def team_hitters(season: int, team: str, players=None) -> pd.DataFrame:
    """Hitters (non-null contact stats) on a team in a season: name, hand, stats, positions."""
    players = load_players() if players is None else players
    q = players[(players.season == season) & (players.team == team) & players.con.notna()]
    return q[["player_id", "name", "hand", "con", "eye", "pwr", "spd", "primary_pos", "secondary_pos"]].reset_index(drop=True)


def to_hitter(row) -> o.Hitter:
    return o.Hitter(row["name"], row["hand"] or "R", int(row["con"]), int(row["eye"]), int(row["pwr"]), int(row["spd"]))


def estimated_order(plays, season: int, team_name: str) -> pd.Series:
    """Average batting slot per batter name: first nine distinct batters of each game, in order of appearance."""
    s = plays[(plays.season == season) & (plays.play_type == "Swing") & (plays.game_type == "live") &
              (plays.off_team == team_name)].sort_values(["game_id", "play_num"])
    slots = []
    for g, grp in s.groupby("game_id"):
        seen = []
        for name in grp.batter_name:
            if name not in seen:
                seen.append(name)
            if len(seen) == 9:
                break
        slots += [(n, i + 1) for i, n in enumerate(seen)]
    return pd.DataFrame(slots, columns=["name", "slot"]).groupby("name").slot.agg(["mean", "count"]).sort_values("mean")


def can_play(prim: str, sec, pos: str) -> bool:
    """Position eligibility from rulebook section 8."""
    if prim == pos:
        return True
    sec = sec or ""
    if prim in INFIELD:
        if sec in ("UTIL", "INF") and pos in INFIELD:
            return True
        if sec == "C" and pos == "C":
            return True
    if prim == "C":
        if sec in ("CIF", "CINF") and pos in ("1B", "3B"):
            return True
        if sec == "COF" and pos in ("LF", "RF"):
            return True
    if prim in OUTFIELD and pos in OUTFIELD:
        return True
    return False


def legal_defense(players: list[tuple]) -> bool:
    """players = [(primary_pos, secondary_pos), ...] for 9 hitters. True when 8 of them can fill C,1B,2B,3B,SS,LF,CF,RF
    (the ninth DHs) with at least 5 of them at their primary position (rulebook 8.5.2)."""
    n = len(players)
    best = [False]

    def rec(pi, used, prim_count, skipped):
        if best[0]:
            return
        if pi == len(DEFENSE):
            best[0] = prim_count >= 5
            return
        pos = DEFENSE[pi]
        for i in range(n):
            if i in used or not can_play(players[i][0], players[i][1], pos):
                continue
            rec(pi + 1, used | {i}, prim_count + (players[i][0] == pos), skipped)
            if best[0]:
                return
    rec(0, frozenset(), 0, 0)
    return best[0]


def catcher_mix(season: int, plays=None, players=None) -> dict:
    """{catcher EYE: swings caught} for a season's live MLN games."""
    plays = load_plays() if plays is None else plays
    players = load_players() if players is None else players
    s = plays[(plays.season == season) & (plays.play_type == "Swing") & (plays.game_type == "live") & plays.catcher_id.notna()]
    eye = players[players.season == season].set_index("player_id")["eye"]
    out: dict = {}
    for cid, n in s.groupby("catcher_id").size().items():
        e = eye.get(int(cid))
        if e == e and e is not None:
            out[int(e)] = out.get(int(e), 0) + int(n)
    return out

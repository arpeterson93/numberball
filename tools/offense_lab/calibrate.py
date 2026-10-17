"""Compare the exact offense model with real run scoring.

    python tools/offense_lab/calibrate.py [season ...]

Per team-season: lineup = its 9 most frequent hitters (batting order = descending PA, a rough proxy), opposing
pitchers = the league's pitcher builds weighted by batters faced. Prints model vs actual runs per half-inning
(actual = score change between consecutive plays, regulation innings only).
"""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import offense_sim as o

seasons = [int(x) for x in sys.argv[1:]] or [12, 13]
CACHE = ROOT / "tools/diff_lab/cache"
plays = pd.read_pickle(CACHE / "plays.pkl")
pl = pd.read_pickle(CACHE / "players.pkl")
pl = pl[(pl.league == "MLN") & ~pl.s_id.astype(str).str.startswith("R_") & pl.player_id.notna()].copy()
pl["player_id"] = pl.player_id.astype(int); pl["season"] = pl.season.astype(int)
pl = pl.drop_duplicates(["season", "player_id"]).set_index(["season", "player_id"])
model = o.OffenseModel()


def actual_runs(df):
    """runs scored on each play = the batting side's score on the next play of the game minus its own."""
    df = df.sort_values(["game_id", "play_num"]).copy()
    bat_score = np.where(df.half == "top", df.away_score, df.home_score).astype(float)
    df["bat_score"] = bat_score
    nxt_game = df.game_id.shift(-1)
    top = (df.half == "top").values
    nxt = np.where(top, df.away_score.shift(-1), df.home_score.shift(-1)).astype(float)
    df["runs"] = np.where(nxt_game == df.game_id, nxt - bat_score, 0.0)
    df.loc[df.runs < 0, "runs"] = 0.0
    return df


rows_all = []
for se in seasons:
    live = plays[(plays.season == se) & (plays.game_type == "live") & (plays.inning <= 6)].copy()
    live = actual_runs(live)
    swings = live[live.play_type == "Swing"]
    hi = live.groupby("off_team").apply(lambda g: g[["game_id", "inning", "half"]].drop_duplicates().shape[0])
    rr = live.groupby("off_team").runs.sum()
    pit = swings.groupby("pitcher_id").size()
    mix = {}
    for pid, n in pit.items():
        try:
            r = pl.loc[(se, int(pid))]
        except KeyError:
            continue
        if r[["mov", "cmd", "vel", "awr"]].isna().any():
            continue
        k = (r["hand"], int(r["mov"]), int(r["cmd"]), int(r["vel"]), int(r["awr"]))
        mix[k] = mix.get(k, 0) + n
    pitchers = [(w, o.Pitcher(*k)) for k, w in sorted(mix.items(), key=lambda x: -x[1])[:25]]
    rows = []
    for team, grp in swings.groupby("off_team"):
        top = grp.groupby("batter_id").size().sort_values(ascending=False).head(9)
        lu = []
        for pid in top.index:
            try:
                r = pl.loc[(se, int(pid))]
            except KeyError:
                continue
            if r[["con", "eye", "pwr", "spd"]].isna().any():
                continue
            lu.append(o.Hitter(r["name"], r["hand"], int(r["con"]), int(r["eye"]), int(r["pwr"]), int(r["spd"])))
        if len(lu) < 9:
            continue
        m = model.expected_runs(lu, pitchers) / model.innings
        rows.append((se, team, hi[team], rr[team] / hi[team], m))
    rows_all += rows
df = pd.DataFrame(rows_all, columns=["season", "team", "half_innings", "actual_per_inn", "model_per_inn"])
for se, g in df.groupby("season"):
    w = g.half_innings
    print(f"S{se}: actual {np.average(g.actual_per_inn, weights=w):.3f}  model {np.average(g.model_per_inn, weights=w):.3f} "
          f"runs/half-inning   team-level correlation {g.actual_per_inn.corr(g.model_per_inn):.2f} (n={len(g)})")
print(df.round(3).to_string(index=False))

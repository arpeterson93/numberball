"""Rebuilds base.json: what the parity measures look like if every game were a coin flip, in the real format (teams paired at
random each session). base[teams][games] = [mean SD of win %, same without the best and worst team, share of teams within 1.5
wins of .500]. The page embeds this file."""
import json
from pathlib import Path
import numpy as np

rng = np.random.default_rng(7)
SIMS = 20000
base = {}
for k in (12, 14, 16):
    sd = np.zeros(17); trim = np.zeros(17); mid = np.zeros(17)
    wins = np.zeros((SIMS, k))
    for n in range(1, 17):
        perm = np.argsort(rng.random((SIMS, k)), axis=1)      # one session: a random perfect matching
        a, b = perm[:, 0::2], perm[:, 1::2]
        win = np.where(rng.random(a.shape) < .5, a, b)         # a coin flip decides each game
        np.put_along_axis(wins, win, np.take_along_axis(wins, win, axis=1) + 1, axis=1)
        w = np.sort(wins, axis=1)
        p = w / n
        sd[n] = p.std(axis=1).mean()
        trim[n] = p[:, 1:-1].std(axis=1).mean()
        mid[n] = (np.abs(w - n / 2) <= 1.5).mean()
    base[k] = {n: [round(float(sd[n]), 4), round(float(trim[n]), 4), round(float(mid[n]), 3)] for n in range(1, 17)}
json.dump(base, open(Path(__file__).resolve().parent / 'base.json', 'w'), separators=(',', ':'))

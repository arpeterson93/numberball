"""Pull every MLN row of the Supabase plays table into tools/diff_lab/cache/plays.pkl.

Step 1 of 3 for the Diff Distribution Lab artifact:
    python tools/diff_lab/fetch_plays.py
    python tools/diff_lab/build_state.py
    python tools/diff_lab/build_artifact.py
"""
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import pandas as pd
import database

rows = database._fetch_all(database._client().table("plays").select("*").eq("league", "MLN"))
df = pd.DataFrame(rows)
df.to_pickle(CACHE / "plays.pkl")
print(df.shape)

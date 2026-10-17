"""Pull the full players table into tools/diff_lab/cache/players.pkl."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import pandas as pd
import database
p = pd.DataFrame(database._fetch_all(database._client().table("players").select("*")))
p.to_pickle(ROOT / "tools/diff_lab/cache/players.pkl")
print(p.shape, list(p.columns))

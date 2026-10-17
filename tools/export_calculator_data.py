"""Export the data behind docs/tools/range_calculator.html.

    python tools/export_calculator_data.py "POR - Oregon Trail Dysentary Field.xlsx"

Writes sheet_calculator_params.json (tracked; used by utils.py) and docs/data/calculator_params.json (the
stadium sheet's constant cells, via sheet_calculator.load_params)
and docs/data/builds.json (MLN player builds per season from the Supabase players table).
"""
import json, os, sys, warnings, logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import sheet_calculator as sc

xlsx = sys.argv[1] if len(sys.argv) > 1 else "POR - Oregon Trail Dysentary Field.xlsx"
params = {k: v for k, v in sc.load_params(xlsx).items() if isinstance(v, (int, float, str, bool))}
_blob = json.dumps(params, separators=(",", ":"))
(ROOT / "docs/data/calculator_params.json").write_text(_blob, encoding="utf-8")   # the web page (docs/data is gitignored)
(ROOT / "sheet_calculator_params.json").write_text(_blob, encoding="utf-8")         # tracked copy used by utils.py
print("params:", len(params), "cells")

import database
rows = database._fetch_all(database._client().table("players").select(
    "s_id,season,name,team,primary_pos,hand,con,eye,pwr,spd,mov,cmd,vel,awr,status"))
out = []
for r in rows:
    if str(r.get("s_id", "")).startswith("R_") or r.get("season") is None or not r.get("name"):
        continue                                    # RLN rows / incomplete
    def i(k):
        v = r.get(k)
        return None if v is None or v != v else int(v)
    out.append([int(r["season"]), r["name"], (r.get("team") or "").strip(), r.get("primary_pos") or "",
                (r.get("hand") or "").strip(), i("con"), i("eye"), i("pwr"), i("spd"), i("mov"), i("cmd"),
                i("vel"), i("awr")])
out.sort(key=lambda x: (-x[0], x[1].lower()))
payload = {"cols": ["season", "name", "team", "pos", "hand", "con", "eye", "pwr", "spd", "mov", "cmd", "vel", "awr"],
           "rows": out}
(ROOT / "docs/data/builds.json").write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                                            encoding="utf-8")
print("builds:", len(out), "rows, seasons", sorted({r[0] for r in out}))

"""Immaculate Innings artifact: rebuild from scratch.

    python tools/immaculate/fetch_all_plays.py        # data/all_plays_raw.json (whole plays table, a few minutes)
    python tools/immaculate/fetch_bases_empty_k.py    # data/bases_empty_k.json
    python tools/immaculate/build_final.py            # data/final_artifact_data.json
    python tools/immaculate/build_artifact.py         # data/immaculate_frames_final.html

Then publish data/immaculate_frames_final.html with the Artifact tool (the live page is
https://claude.ai/artifact/JTRSN76jeU3dNKdsYTUThd; pass that url to update it in place).
Edit the page itself in immaculate_frames.html (the data goes where /*__DATA__*/ is).

Non-ASCII characters in the template are written as unicode escapes, as the published page has them.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def esc(text: str) -> str:
    return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in text)


data = json.load(open(DATA / "final_artifact_data.json", encoding="utf-8"))
html = esc(open(HERE / "immaculate_frames.html", encoding="utf-8").read())
assert "/*__DATA__*/" in html
html = html.replace("/*__DATA__*/", json.dumps(data, separators=(",", ":")))
out = DATA / "immaculate_frames_final.html"
out.write_text(html, encoding="utf-8")
print("wrote", out, len(html))

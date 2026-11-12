"""Square notification icons for the push alerts: docs/data/push_icons/<ABBR>.png, one per team in docs/data/meta.json.

The site's team logos are big (up to ~1 MB) and not square. Android Chrome fetches a notification's icon when the alert is
shown and falls back to a grey letter circle if that is slow or fails, so the alerts point at these small 192x192 copies instead
(the logo fitted inside a white square). Built in the Actions workflow before the deploy, so they always match the current
teams; docs/data/push_icons/ is git-ignored.

  python tools/push_icons.py
"""
from __future__ import annotations

import json
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "data", "push_icons")
SIZE = 192
PAD = 14          # white margin around the logo


def main() -> int:
    with open(os.path.join(DOCS, "data", "meta.json"), encoding="utf-8") as fh:
        teams = json.load(fh).get("teams") or {}
    os.makedirs(OUT, exist_ok=True)
    made = 0
    for abbr, t in teams.items():
        src = os.path.join(DOCS, t.get("logo_url") or "")
        if not t.get("logo_url") or not os.path.isfile(src):
            print(f"push_icons: no logo file for {abbr}")
            continue
        logo = Image.open(src).convert("RGBA")
        logo.thumbnail((SIZE - 2 * PAD, SIZE - 2 * PAD), Image.LANCZOS)
        card = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
        card.alpha_composite(logo, ((SIZE - logo.width) // 2, (SIZE - logo.height) // 2))
        card.convert("RGB").save(os.path.join(OUT, f"{abbr}.png"), optimize=True)
        made += 1
    print(f"push_icons: {made} icons in {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

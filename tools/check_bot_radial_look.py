"""Guard: the Discord bot's radial slice backdrop must keep the app's light pastel look.

Renders the pitches radial the way the bot does (bot_charts.apply_dark_theme -> kaleido PNG) and checks that the polar
disc is white and the wedges are light. This has regressed to dark several times because the wedges are translucent in the
app and their on-screen colour then depends on whatever polar background sits under them; the bot now flattens them to the
solid colours they have over the app's white disc (bot_charts._match_app_slice_backdrop). Run after touching bot_charts.py,
the radial builders in utils.py, or the plotly / kaleido versions:

    python -I tools/check_bot_radial_look.py          (exit code 1 on failure)
"""
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd
from PIL import Image

import bot_charts


def synthetic_history(n=120, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"id": np.arange(1, n + 1), "pitch": rng.integers(1, 1001, n).astype(float),
                         "result": rng.choice(["K", "1B", "GO", "BB", "FO", "2B"], n)})


def main() -> int:
    fig = bot_charts.pitches_radial_fig(synthetic_history())
    bot_charts.apply_dark_theme(fig)
    wedges = [t for t in fig.data if t.type == "barpolar"]
    problems = []
    if fig.layout.polar.bgcolor != "rgb(255,255,255)":
        problems.append(f"polar disc is {fig.layout.polar.bgcolor}, expected white")
    if not wedges or any("rgba" in str(c) for c in wedges[0].marker.color):
        problems.append("wedges are still translucent (their look would depend on the polar background)")
    path = os.path.join(tempfile.gettempdir(), "bot_radial_look_check.png")
    fig.write_image(path, format="png", scale=1.0, width=840, height=600)
    im = np.array(Image.open(path).convert("RGB")).astype(float)
    cx, cy = 378, 323                                     # plot centre at this size
    ring = np.array([im[int(cy + 160 * np.sin(a)), int(cx + 160 * np.cos(a))] for a in np.linspace(0, 2 * np.pi, 48, endpoint=False)])
    lum = ring.mean()
    print(f"mean wedge brightness {lum:.0f}/255 (light pastel is roughly 180-240 depending on the data; the dark regression was ~65, the check fails below 150)")
    if lum < 150:
        problems.append(f"wedges render dark (mean brightness {lum:.0f})")
    print("FAIL: " + "; ".join(problems) if problems else "OK: wedges are light pastel on a white disc")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

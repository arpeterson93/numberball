"""Checks that the Python renderer (tools/push_format.py, used by the sender) and the JS one (docs/js/push_format.js, used by the
live preview) produce identical text, over every preset, every style and the real plays in docs/data.

  pip install quickjs
  python tools/push_format_parity_test.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import sys

import quickjs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_format as pf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    meta = json.load(open(os.path.join(ROOT, "docs", "data", "meta.json"), encoding="utf-8"))
    labels, short = meta["result_labels"], meta["result_short"]
    ctx = quickjs.Context()
    ctx.eval("var window = {};")
    ctx.eval(open(os.path.join(ROOT, "docs", "js", "push_format.js"), encoding="utf-8").read())
    ctx.eval(f"var LABELS = {json.dumps(labels)}, SHORT = {json.dumps(short)};")
    ctx.eval("function run(m, fmt) { var r = window.MLNPushFmt.render(m, fmt, LABELS, SHORT); return JSON.stringify(r ? [r.title, r.body.join('\\n')] : null); }")
    presets = json.loads(ctx.eval("JSON.stringify(window.MLNPushFmt.PRESETS)"))
    samples = json.loads(ctx.eval("JSON.stringify(window.MLNPushFmt.SAMPLES.map(function (s) { return s.m; }))"))
    plays = []
    for f in sorted(glob.glob(os.path.join(ROOT, "docs", "data", "plays_*.json")))[-3:]:
        plays += [m for m in json.load(open(f, encoding="utf-8")) if m.get("result") is not None]
    random.seed(7)
    plays = samples + random.sample(plays, min(1500, len(plays)))
    for m in plays:   # rows from a build before leverage_after existed: borrow the old field so the component is exercised
        m.setdefault("leverage_after", m.get("leverage"))
    rng = random.Random(3)
    layouts = [p[1] for p in presets]
    for key, choices in pf.CHOICES.items():   # every preset under every value of every option
        layouts += [dict(p[1], **{key: v}) for p in presets for v in choices]
    legacy = {"bs": ("glyph", "words", "bad"), "nb": ("full", "last", "bad"), "np": ("full", "last", "bad"), "wm": ("after", "change", "bad")}
    for key, choices in legacy.items():   # saves from before those options became components
        layouts += [dict(p[1], **{key: v}) for p in presets for v in choices]
    layouts += [dict(p[1], bs="words", nb="last", np="last", wm="change") for p in presets]
    layouts += [{"l": [["score", "result", "pitcher"], ["result"]], "b": "emoji", "r": r} for r in ("code", "label", "bad")]   # old saves
    layouts += [{"l": [["score", "inning"], ["bases", "outs"]], "b": b} for b in ("glyph", "emoji", "words", "bad")]
    for _ in range(80):   # random component orders, repeats, blanks and option values
        f = {"l": [[rng.choice(pf.COMPONENTS) for _ in range(rng.randint(0, 5))] for _ in range(rng.randint(1, 5))]}
        for key, choices in {**pf.CHOICES, **legacy}.items():
            if rng.random() < 0.8:
                f[key] = rng.choice(choices)
        layouts.append(f)
    layouts.append({"l": [["nope", "score"], "junk", []], "bs": "bad", "tg": 9, "ns": "full", "pv": None})
    bad = n = 0
    for m in plays:
        mj = json.dumps(m)
        for fmt in layouts:
            py = pf.render(m, fmt, labels, short)
            js = json.loads(ctx.eval(f"run({mj}, {json.dumps(fmt)})"))
            n += 1
            if (list(py) if py else None) != js:
                bad += 1
                if bad <= 5:
                    print("MISMATCH", fmt, "\n  py:", py, "\n  js:", js)
    print(f"{n} renders compared, {bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

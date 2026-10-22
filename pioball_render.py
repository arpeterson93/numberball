"""Renders PioBall's tables (graded pitch, current guesses, leaderboard) as PNGs (Pillow only)."""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

import swing_game
import utils

_BG = (32, 34, 37)
_ROW_A = (43, 45, 49)
_ROW_B = (49, 51, 56)
_ROW_HL = (28, 54, 80)          # the Actual row / a just-added guess
_TEXT = (240, 240, 240)
_MUTED = (160, 164, 170)
_POS = (87, 184, 87)
_NEG = (224, 108, 117)
# Same palette as the Gameday scorebug (docs/css/style.css, dark theme).
_BASE_ON = (110, 168, 240)
_OFF_FILL = (26, 32, 41)
_OFF_STROKE = (74, 83, 97)
_OUT_ON = (200, 16, 46)
_S = 2  # render at 2x so the image stays sharp when Discord scales it

_FONT_FILES = {
    False: ("segoeui.ttf", "arial.ttf", "calibri.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    True: ("segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in _FONT_FILES[bold]:
        try:
            return ImageFont.truetype(name, size * _S)
        except OSError:
            continue
    return ImageFont.load_default(size * _S)


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _ink_on(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (20, 20, 20) if lum > 150 else (255, 255, 255)


def _fmt(v: float, nd: int, suffix: str = "") -> str:
    """Signed number with no "-0.00": anything that rounds to zero prints unsigned."""
    if round(v, nd) == 0:
        return f"{0:.{nd}f}{suffix}"
    return f"{v:+.{nd}f}{suffix}"


def _px(v: float) -> int:
    return int(v * _S)


# ------------------------------------------------------------- header pieces

def _draw_situation(d: ImageDraw.ImageDraw, right: float, top: float, p: dict) -> None:
    """Inning triangle + number, then the base diamond with out dots under it,
    mirroring the Gameday scorebug (utils.bases_diamond geometry, key_moments_build._diamond_svg)."""
    if p.get("inning") is None or not p.get("obc"):
        return
    obc = str(p["obc"]).zfill(3)
    outs = min(int(p.get("outs") or 0), 2)
    dia_w = 34
    ox = right - dia_w
    # diamond: viewBox -2 -2 34 34, three 10x10 squares rotated 45deg (2B top, 1B right, 3B left)
    for (cx, cy), on in (((15, 6.4), obc[1] == "1"), ((23.9, 15.3), obc[2] == "1"), ((6.1, 15.3), obc[0] == "1")):
        x, y, r = ox + cx + 2, top + cy + 2, 5 * 2 ** 0.5
        d.polygon([(_px(x), _px(y - r)), (_px(x + r), _px(y)), (_px(x), _px(y + r)), (_px(x - r), _px(y))],
                  fill=_BASE_ON if on else _OFF_FILL, outline=_BASE_ON if on else _OFF_STROKE, width=_S)
    dots_x = ox + (dia_w - 21) / 2
    for i in range(2):
        cx, cy = dots_x + 4 + i * 13, top + dia_w + 8
        d.ellipse((_px(cx - 4), _px(cy - 4), _px(cx + 4), _px(cy + 4)),
                  fill=_OUT_ON if i < outs else _OFF_FILL, outline=_OUT_ON if i < outs else _OFF_STROKE, width=_S)
    f = _font(22, True)
    num = str(p["inning"])
    tx = ox - 14 - d.textlength(num, font=f) / _S
    d.text((_px(tx), _px(top + 17)), num, font=f, fill=_TEXT, anchor="lm")
    _triangle(d, tx - 12, top + 17, str(p.get("half") or "").lower() == "bottom")


def _triangle(d: ImageDraw.ImageDraw, ax: float, ay: float, bottom: bool, s: float = 1.0) -> None:
    """Inning marker: up triangle = top half, down triangle = bottom half."""
    if bottom:
        pts = [(ax - 7 * s, ay - 5 * s), (ax + 7 * s, ay - 5 * s), (ax, ay + 6 * s)]
    else:
        pts = [(ax - 7 * s, ay + 5 * s), (ax + 7 * s, ay + 5 * s), (ax, ay - 6 * s)]
    d.polygon([(_px(x), _px(y)) for x, y in pts], fill=_TEXT)


# ------------------------------------------------------------- generic table

_EMOJI_FONT: list = []   # [font or None], loaded once


def _emoji_font():
    if not _EMOJI_FONT:
        font = None
        for name in ("seguiemj.ttf", "Segoe UI Emoji", "NotoColorEmoji.ttf"):
            try:
                font = ImageFont.truetype(name, 20 * _S)
                break
            except OSError:
                continue
        _EMOJI_FONT.append(font)
    return _EMOJI_FONT[0]


def _flame(d: ImageDraw.ImageDraw, cx: float, cy: float, h: float = 22) -> None:
    """The 🔥 emoji centred on cx, cy from the system's colour emoji font; a drawn
    flame (outer orange, inner yellow) when no emoji font is installed."""
    font = _emoji_font()
    if font is not None:
        try:
            d.text((_px(cx), _px(cy)), "\U0001F525", font=font, embedded_color=True, anchor="mm")
            return
        except Exception:
            pass

    def shape(pts, col):
        d.polygon([(_px(cx + x * h / 22), _px(cy + y * h / 22)) for x, y in pts], fill=col)
    shape([(0, -11), (2, -7), (5, -3), (7, 2), (6, 6), (3, 9), (0, 11), (-3, 9), (-6, 6), (-7, 2),
           (-5, -2), (-3, -4), (-3, -8)], (244, 110, 22))
    shape([(0, -2), (2, 1), (4, 4), (3, 7), (0, 9), (-3, 7), (-4, 4), (-2, 1)], (255, 200, 60))


def _table(title: str, subtitle: str | None, p: dict | None, cols: list[dict], rows: list[dict],
           footer_h: int = 0, footer=None, as_image: bool = False):
    """cols: {label, w, align: l|r|c, kind: text|rank|chip|signed}. rows: {cells, bg?, bold?}.
    p (a swing_pitches row) adds the inning/bases/outs graphic to the header."""
    pad, row_h, head_h, title_h = 20, 40, 30, (78 if title else 0)
    width = pad * 2 + sum(c["w"] for c in cols)
    height = title_h + head_h + len(rows) * row_h + pad + footer_h
    img = Image.new("RGB", (width * _S, height * _S), _BG)
    d = ImageDraw.Draw(img)

    if title:
        d.text((_px(pad), _px(16)), title, font=_font(24, True), fill=_TEXT)
    if subtitle:
        d.text((_px(pad), _px(48)), subtitle, font=_font(15), fill=_MUTED)
    if p:
        _draw_situation(d, width - pad, 10, p)

    xs, x = [], pad
    for c in cols:
        xs.append(x)
        x += c["w"]

    def anchor_xy(i, align):
        c = cols[i]
        if align == "r":
            return xs[i] + c["w"] - 8, "rm"
        if align == "c":
            return xs[i] + c["w"] / 2, "mm"
        return xs[i], "lm"

    hf, bf, nf = _font(12, True), _font(16, True), _font(16)
    y = title_h
    for i, c in enumerate(cols):
        ax, an = anchor_xy(i, c.get("align", "l"))
        d.text((_px(ax), _px(y + head_h / 2 + 2)), c["label"], font=hf, fill=_MUTED, anchor=an)
    y += head_h

    for ri, row in enumerate(rows):
        bg = row.get("bg") or (_ROW_A if ri % 2 == 0 else _ROW_B)
        d.rectangle((0, _px(y), _px(width), _px(y + row_h)), fill=bg)
        mid = _px(y + row_h / 2)
        for i, c in enumerate(cols):
            val = row["cells"][i]
            if val is None:
                continue
            ax, an = anchor_xy(i, c.get("align", "l"))
            kind = c.get("kind", "text")
            if kind == "inning":
                inn, half = val
                _triangle(d, xs[i] + 8, y + row_h / 2, half == "bottom", 0.8)
                d.text((_px(xs[i] + 20), mid), str(inn), font=nf, fill=_TEXT, anchor="lm")
                continue
            if kind == "chip":
                try:
                    chip = _hex(utils._result_color(val))
                except Exception:
                    chip = (200, 200, 200)
                x0, x1 = xs[i] + (c["w"] - 72) / 2, xs[i] + (c["w"] + 72) / 2
                d.rounded_rectangle((_px(x0), _px(y + 8), _px(x1), _px(y + row_h - 8)), radius=_px(6), fill=chip)
                d.text((_px((x0 + x1) / 2), mid), val, font=bf, fill=_ink_on(chip), anchor="mm")
                continue
            color, font = _TEXT, (bf if (c.get("bold") or (row.get("bold") and i == 1)) else nf)
            if kind == "rank":
                color = _MUTED
            elif kind == "signed":
                try:
                    v = float(str(val).rstrip("%"))
                except ValueError:
                    v = 0.0
                color = _POS if v > 0 else (_NEG if v < 0 else _TEXT)
            text = str(val)
            if c.get("clip"):
                while len(text) > 1 and d.textlength(text, font=font) / _S > c["w"] - 12:
                    text = text[:-1]
                if text != str(val):
                    text = text[:-1] + ".."
            d.text((_px(ax), mid), text, font=font, fill=color, anchor=an)
            if i == 1 and row.get("flame"):  # on fire: a flame just right of the player's name
                _flame(d, ax + d.textlength(text, font=font) / _S + 16, y + row_h / 2)
        y += row_h
    if footer:
        footer(d, y + 10, width)

    if as_image:
        return img
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _last(name: str | None) -> str:
    return (name or "?").split()[-1]


def _matchup_short(p: dict) -> str:
    """Last names only, for table rows: "Gilwater vs Rousseau"."""
    return f"{_last(p.get('batter'))} vs {_last(p.get('pitcher'))}" if p.get("batter") else "Pioneers PA"


def _matchup(p: dict) -> str:
    return f"{p.get('batter')} vs {p.get('pitcher') or '?'}" if p.get("batter") else "Pioneers plate appearance"


# ------------------------------------------------------------- public renderers

def render_graded_table(g: dict) -> io.BytesIO:
    """g is a swing_game.grade payload: pitch, pitch_row, actual, guesses
    (already sorted by lowest diff). The manager's Actual row sits first."""
    p, a = g["pitch_row"], g["actual"]
    pitch = str(g["pitch"])
    cols = [{"label": "#", "w": 36, "kind": "rank"},
            {"label": "PLAYER", "w": 230},
            {"label": "PITCH", "w": 80, "align": "r"},
            {"label": "SWING", "w": 80, "align": "r"},
            {"label": "DIFF", "w": 80, "align": "r", "bold": True},
            {"label": "RESULT", "w": 104, "align": "c", "kind": "chip"}]
    rows = []
    if a.get("swing") is not None and a.get("diff") is not None:
        rows.append({"cells": [None, swing_game.ACTUAL_NAME, pitch, str(a["swing"]), str(a["diff"]),
                               a.get("result") or "?"], "bg": _ROW_HL, "bold": True})
    m = g.get("model")
    if m:
        rows.append({"cells": [None, swing_game.MODEL_NAME, None, str(m["swing"]), str(m["diff"]), m["result"]],
                     "bg": _ROW_HL, "bold": True})
    for i, r in enumerate(g["guesses"], 1):
        rows.append({"cells": [str(i), r["user_name"], None, str(r["guess"]), str(r["diff"]), r["result"]]})
    return _table(_matchup(p), None, p, cols, rows)


def render_guesses_table(c: dict, highlight_user_id: str | None = None) -> io.BytesIO:
    """c is swing_game.current_pa(): the open pitch's guesses, lowest swing first.
    highlight_user_id marks the row that was just added."""
    p = c["pitch_row"]
    cols = [{"label": "#", "w": 36, "kind": "rank"},
            {"label": "PLAYER", "w": 260},
            {"label": "SWING", "w": 100, "align": "r", "bold": True}]
    rows = []
    for i, g in enumerate(c["guesses"], 1):
        hl = highlight_user_id is not None and str(g["user_id"]) == str(highlight_user_id)
        rows.append({"cells": [str(i), g["user_name"], str(g["guess"])], "bg": _ROW_HL if hl else None, "bold": hl,
                     "flame": g.get("hot")})
    sub = "Pitch is in - guessing closed" if c.get("closed") else "Active guesses"
    return _table(_matchup_short(p) if p.get("batter") else "Our next plate appearance", sub, p if p.get("batter") else None,
                  cols, rows)


def render_leaderboard(rows_in: list[dict], title: str, sort_label: str) -> io.BytesIO:
    def signed(v, fmt):
        return format(v, fmt)

    cols = [{"label": "#", "w": 36, "kind": "rank"},
            {"label": "PLAYER", "w": 190},
            {"label": "PA", "w": 52, "align": "r"},
            {"label": "RV", "w": 72, "align": "r", "kind": "signed"},
            {"label": "RE24", "w": 72, "align": "r", "kind": "signed"},
            {"label": "WPA", "w": 72, "align": "r", "kind": "signed"},
            {"label": "AVG", "w": 64, "align": "r"},
            {"label": "OBP", "w": 64, "align": "r"},
            {"label": "SLG", "w": 64, "align": "r"},
            {"label": "OPS", "w": 64, "align": "r", "bold": True}]
    rows = []
    for i, r in enumerate(rows_in[:15], 1):
        rows.append({"cells": [str(i), r["name"], str(r["PA"]), _fmt(r["RV"], 2), _fmt(r["RE24"], 2),
                               _fmt(r["WPA"], 1, "%"),
                               swing_game._f(r["AVG"]), swing_game._f(r["OBP"]),
                               swing_game._f(r["SLG"]), swing_game._f(r["OPS"])]})
    return _table(title, None, None, cols, rows)


def _panel_title(d: ImageDraw.ImageDraw, x: float, y: float, text: str) -> None:
    d.text((_px(x), _px(y)), text, font=_font(12, True), fill=_MUTED, anchor="lm")


def _wheel(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, rows: list[dict]) -> None:
    """The 1-1000 swing wheel (pitch 1 at 12 o'clock, clockwise - the scouting radials'
    layout) with time as radius: the newest plate appearance rides the outer ring and
    older ones step toward the center. On each ring a hollow dot is the real pitch, a
    filled dot (result color) is the guess, and an arc along the ring joins them.
    rows are newest first."""
    import math
    n = len(rows)

    def ring_r(k):
        return r - (r * 0.7) * k / max(n - 1, 1)

    def pt(v, rad):
        a = (v / 1000) * 2 * math.pi
        return _px(cx + rad * math.sin(a)), _px(cy - rad * math.cos(a))

    for k in range(n):  # faint track per plate appearance, the outer one a bit stronger
        rr = ring_r(k)
        d.ellipse((_px(cx - rr), _px(cy - rr), _px(cx + rr), _px(cy + rr)),
                  outline=_OFF_STROKE if k == 0 else (52, 58, 68), width=_S)
    lf = _font(11, True)
    for v, lab in ((1000, "1"), (250, "250"), (500, "500"), (750, "750")):
        d.text(pt(v, r + 14), lab, font=lf, fill=_MUTED, anchor="mm")
    dot = max(2.5, min(5.0, r * 0.7 / max(n - 1, 1) * 0.45))
    for k in range(n - 1, -1, -1):  # draw old first so the newest sits on top
        row = rows[k]
        if row.get("pitch") is None or row.get("guess") is None:
            continue
        rr, col = ring_r(k), _result_rgb(row["result"])
        signed = ((row["guess"] - row["pitch"] + 500) % 1000) - 500
        start, end = (row["pitch"], row["guess"]) if signed >= 0 else (row["guess"], row["pitch"])
        if signed != 0:  # PIL arcs run clockwise from 3 o'clock; ours start at 12
            d.arc((_px(cx - rr), _px(cy - rr), _px(cx + rr), _px(cy + rr)),
                  start=start * 0.36 - 90, end=end * 0.36 - 90, fill=col, width=2 * _S)
        (px_, py_), (gx, gy) = pt(row["pitch"], rr), pt(row["guess"], rr)
        d.ellipse((px_ - dot * _S, py_ - dot * _S, px_ + dot * _S, py_ + dot * _S), fill=_BG, outline=_TEXT, width=_S)
        d.ellipse((gx - dot * _S, gy - dot * _S, gx + dot * _S, gy + dot * _S), fill=col)


def _result_rgb(result: str) -> tuple[int, int, int]:
    try:
        return _hex(utils._result_color(result))
    except Exception:
        return (200, 200, 200)


def _cumulative(d: ImageDraw.ImageDraw, x0: float, y0: float, w: float, h: float,
                mine: list[float | None], actual: list[float | None]) -> None:
    """Running run value (RV) over the shown PAs, oldest to newest: the player's guesses (blue)
    against what the manager's real swings produced on the same pitches (grey).
    Blue above grey = their reads beat the actual swings."""
    grey = (150, 156, 166)

    def cum(vals):
        run, out = 0.0, []
        for v in vals:
            run += v or 0.0
            out.append(run)
        return out

    a, b = cum(mine), cum(actual)
    lo, hi = min(0.0, *a, *b), max(0.0, *a, *b)
    span = (hi - lo) or 1.0
    n = len(a)

    def xy(i, v):
        return _px(x0 + (w * i / (n - 1) if n > 1 else w / 2)), _px(y0 + h - h * (v - lo) / span)

    zy = _px(y0 + h - h * (0 - lo) / span)
    d.line((_px(x0), zy, _px(x0 + w), zy), fill=_OFF_STROKE, width=_S)
    for series, col in ((b, grey), (a, _BASE_ON)):
        pts = [xy(i, v) for i, v in enumerate(series)]
        if len(pts) > 1:
            d.line(pts, fill=col, width=2 * _S)
        for x, y in pts:
            d.ellipse((x - 3 * _S, y - 3 * _S, x + 3 * _S, y + 3 * _S), fill=col)
    for series, col, dy in ((a, _BASE_ON, -10), (b, grey, 10)):
        x, y = xy(n - 1, series[-1])
        d.text((x + 8 * _S, y + dy * _S // 2), f"{series[-1]:+.2f}", font=_font(12, True), fill=col, anchor="lm")
    d.ellipse((_px(x0), _px(y0 - 22), _px(x0 + 8), _px(y0 - 14)), fill=_BASE_ON)
    d.text((_px(x0 + 13), _px(y0 - 18)), "guesses", font=_font(10), fill=_MUTED, anchor="lm")
    d.ellipse((_px(x0 + 70), _px(y0 - 22), _px(x0 + 78), _px(y0 - 14)), fill=grey)
    d.text((_px(x0 + 83), _px(y0 - 18)), "actual swings", font=_font(10), fill=_MUTED, anchor="lm")


def _miss_bars(d: ImageDraw.ImageDraw, x0: float, y0: float, w: float, h: float, rows: list[dict]) -> None:
    """Signed miss (guess minus pitch, around the wheel) per PA, oldest to newest:
    bars above the line = guessed high, below = guessed low. Shows bias."""
    mid = y0 + h / 2
    d.line((_px(x0), _px(mid), _px(x0 + w), _px(mid)), fill=_OFF_STROKE, width=_S)
    n = len(rows)
    bw = min(22.0, w / max(n, 1) - 4)
    for i, r in enumerate(rows):
        if r.get("pitch") is None or r.get("guess") is None:
            continue
        signed = ((r["guess"] - r["pitch"] + 500) % 1000) - 500
        cx = x0 + w * (i + 0.5) / n
        bh = (h / 2) * signed / 500
        d.rectangle((_px(cx - bw / 2), _px(min(mid, mid - bh)), _px(cx + bw / 2), _px(max(mid, mid - bh))),
                    fill=_result_rgb(r["result"]))
    d.text((_px(x0 + w), _px(y0 + 4)), "high", font=_font(10), fill=_MUTED, anchor="rt")
    d.text((_px(x0 + w), _px(y0 + h - 4)), "low", font=_font(10), fill=_MUTED, anchor="rb")


_LOGO_CACHE: dict = {}


def _logo(url: str | None):
    """The team logo (RGBA) from its URL, cached; None if it can't be fetched."""
    if not url:
        return None
    if url not in _LOGO_CACHE:
        try:
            if url.startswith("http"):
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    _LOGO_CACHE[url] = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            else:  # the teams table stores the newest logos as docs-relative paths (img/logos/...)
                here = os.path.dirname(os.path.abspath(__file__))
                _LOGO_CACHE[url] = Image.open(os.path.join(here, "docs", url)).convert("RGBA")
        except Exception:
            _LOGO_CACHE[url] = None
    return _LOGO_CACHE[url]


def render_player(name: str, card: dict) -> io.BytesIO:
    """Baseball-card layout: name banner, stat blocks for this game / season /
    all time, three visualizations of the recent guesses, then the recent-guess
    table. card is swing_game.player_card()."""
    hist = card["history"]
    rows = hist["rows"]                      # newest first
    chron = list(reversed(rows))             # oldest first, for the trend plots
    pad = 20
    cols = [{"label": "#", "w": 34, "kind": "rank"},
            {"label": "INN", "w": 56, "kind": "inning"},
            {"label": "MATCHUP", "w": 230, "clip": True},
            {"label": "PITCH", "w": 66, "align": "r"},
            {"label": "SWING", "w": 66, "align": "r"},
            {"label": "DIFF", "w": 66, "align": "r", "bold": True},
            {"label": "RESULT", "w": 100, "align": "c", "kind": "chip"},
            {"label": "ACTUAL", "w": 100, "align": "c", "kind": "chip"},
            {"label": "RE24", "w": 76, "align": "r", "kind": "signed"},
            {"label": "WPA", "w": 76, "align": "r", "kind": "signed"}]
    trows = []
    for i, r in enumerate(rows, 1):
        p = r["pitch_row"]
        trows.append({"cells": [
            str(i), (p.get("inning"), str(p.get("half") or "").lower()) if p.get("inning") is not None else None,
            _matchup_short(p), str(r["pitch"]) if r["pitch"] is not None else "-", str(r["guess"]),
            "-" if r["diff"] is None else str(r["diff"]), r["result"], r.get("actual_result"),
            "-" if r["re24"] is None else _fmt(r["re24"], 2),
            "-" if r["wpa"] is None else _fmt(r["wpa"], 1, "%")]})
    table = _table("", None, None, cols, trows, as_image=True)
    W = table.width // _S
    banner_h, stats_h, viz_h, label_h = 84, 176, 240, 34
    H = banner_h + stats_h + viz_h + label_h + table.height // _S + pad
    img = Image.new("RGB", (W * _S, H * _S), _BG)
    d = ImageDraw.Draw(img)

    # banner: the name across the top
    brand = swing_game.team_brand()
    try:
        banner = _hex(brand.get("primary_hex") or "004e92")
    except Exception:
        banner = (0, 78, 146)
    d.rectangle((0, 0, _px(W), _px(banner_h)), fill=banner)
    d.text((_px(pad), _px(banner_h / 2)), name, font=_font(40, True), fill=(255, 255, 255), anchor="lm")
    logo = _logo(brand.get("logo_url"))
    if logo:
        lh = banner_h - 16
        lw = max(1, int(logo.width * lh / logo.height))
        mark = logo.resize((lw * _S, lh * _S), Image.LANCZOS)
        img.paste(mark, (_px(W - pad - lw), _px(8)), mark)

    # stat blocks
    sc = card["scopes"]
    blocks = [("THIS GAME", sc.get("game")), (f"SEASON {card['season']}" if card.get("season") is not None
                                              else "SEASON", sc.get("season")), ("ALL TIME", sc.get("all"))]
    bw = (W - pad * 4) / 3
    for bi, (label, t) in enumerate(blocks):
        bx, by = pad + bi * (bw + pad), banner_h + 14
        d.rounded_rectangle((_px(bx), _px(by), _px(bx + bw), _px(by + stats_h - 24)), radius=_px(10), fill=_ROW_A)
        _panel_title(d, bx + 14, by + 16, label)
        if not t:
            d.text((_px(bx + bw / 2), _px(by + 62)), "no graded guesses", font=_font(14), fill=_MUTED, anchor="mm")
            continue
        dpa = "-" if t.get("DPA") is None else f"{t['DPA']:.1f}"
        cells = [("PA", str(t["PA"]), _TEXT), ("DPA", dpa, _TEXT), None,
                 ("RV", _fmt(t["RV"], 2), _POS if t["RV"] > 0 else _NEG if t["RV"] < 0 else _TEXT),
                 ("RE24", _fmt(t["RE24"], 2), _POS if t["RE24"] > 0 else _NEG if t["RE24"] < 0 else _TEXT),
                 ("WPA", _fmt(t["WPA"], 1, "%"), _POS if t["WPA"] > 0 else _NEG if t["WPA"] < 0 else _TEXT)]
        cw = (bw - 28) / 3
        for ci, cell in enumerate(cells):
            if cell is None:
                continue
            lab, val, colr = cell
            cx, cy = bx + 14 + (ci % 3) * cw, by + 34 + (ci // 3) * 38
            d.text((_px(cx), _px(cy)), lab, font=_font(10, True), fill=_MUTED, anchor="lm")
            d.text((_px(cx), _px(cy + 16)), val, font=_font(18, True), fill=colr, anchor="lm")
        d.text((_px(bx + 14), _px(by + 112)), "AVG / OBP / SLG / OPS", font=_font(10, True), fill=_MUTED, anchor="lm")
        d.text((_px(bx + 14), _px(by + 128)),
               " / ".join(swing_game._f(t[k]) for k in ("AVG", "OBP", "SLG", "OPS")),
               font=_font(18, True), fill=_TEXT, anchor="lm")

    # visualizations
    vy = banner_h + stats_h
    pw = (W - pad * 4) / 3
    titles = (f"SWING WHEEL - LAST {len(rows)} (NEWEST OUTSIDE)", "RUNNING RV VS ACTUAL", "MISS DIRECTION (GUESS - PITCH)")
    for pi, t in enumerate(titles):
        px0 = pad + pi * (pw + pad)
        d.rounded_rectangle((_px(px0), _px(vy), _px(px0 + pw), _px(vy + viz_h - 14)), radius=_px(10), fill=_ROW_A)
        _panel_title(d, px0 + 14, vy + 16, t)
    _wheel(d, pad + pw / 2, vy + 128, 80, rows)
    d.ellipse((_px(pad + 16), _px(vy + viz_h - 33), _px(pad + 24), _px(vy + viz_h - 25)), fill=_BG, outline=_TEXT, width=_S)
    d.text((_px(pad + 30), _px(vy + viz_h - 29)), "pitch", font=_font(10), fill=_MUTED, anchor="lm")
    d.ellipse((_px(pad + 66), _px(vy + viz_h - 33), _px(pad + 74), _px(vy + viz_h - 25)), fill=_NEG)
    d.text((_px(pad + 80), _px(vy + viz_h - 29)), "guess", font=_font(10), fill=_MUTED, anchor="lm")
    _cumulative(d, pad * 2 + pw + 20, vy + 62, pw - 88, viz_h - 100, [r["rv"] for r in chron],
                [r.get("actual_rv") for r in chron])
    _miss_bars(d, pad * 3 + pw * 2 + 24, vy + 40, pw - 48, viz_h - 70, chron)

    # recent guesses
    ly = vy + viz_h
    d.text((_px(pad), _px(ly + 14)), "RECENT GUESSES", font=_font(12, True), fill=_MUTED, anchor="lm")
    img.paste(table, (0, _px(ly + label_h - 6)))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

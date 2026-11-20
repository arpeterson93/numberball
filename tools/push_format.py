"""Custom notification layouts (per subscription), the Python half. docs/js/push_format.js is the same renderer for the live preview in
Settings > Notifications; tools/push_format_parity_test.py checks the two give identical text. Change both together.

A layout is  {"l": [[component, ...], ...], plus the options below}
  l   lines, each a list of component ids; the first line that has any text becomes the title (iOS shows it bold), the rest the body
Option (anything missing or unknown falls back to the default, see DEFAULTS):
  pv  whose side WPA and win probability are from: "hit" (the team batting) or "feat" (the team of the player the play is credited to)
Components: batter = the batter's name (the runner's, on a steal), batter_last the last name only; pitcher / pitcher_last the same for "vs Pitcher"
(the catcher, on a steal); bases = diamonds, bases_words = "Runner on 1st"; wp = win probability after the play, wp_change = before -> after;
result = the short code (1B); result_full = the full label (Single). (One key moment tag is shown, the first in TAG_ORDER; who scored is always last names.)
Older saves had the options bs nb np wm (bases / name / win probability wording), "b" (bases and outs together) and "r" with a combined "result";
upgrade() turns those into the current form: an option value becomes the matching component.
"""
from __future__ import annotations

COMPONENTS = ["score", "inning", "batter", "batter_last", "result", "result_full", "pitcher", "pitcher_last", "scoring", "bases", "bases_words", "outs",
              "wpa", "lev", "wp", "wp_change", "diff", "tags"]
MAX_LINES = 5

DEFAULTS = {"pv": "hit"}
CHOICES = {"pv": ("hit", "feat")}
# an old option value -> the component it became: (option, value, component it applied to, the new component)
SWAPS = (("bs", "words", "bases", "bases_words"), ("nb", "last", "batter", "batter_last"),
         ("np", "last", "pitcher", "pitcher_last"), ("wm", "change", "wp", "wp_change"))

# The key moment tags worth naming in an alert, most notable first: with one tag shown (the default) a play with several gets the first
# one found here. risp and run_scored say little on their own and are never shown.
TAG_ORDER = ["walk_off", "game_winning", "triple_play", "zero_diff", "five_hundred_diff", "lead_change", "bases_loaded", "double_play",
             "steal", "high_leverage", "wpa"]
TAG_TEXT = {
    "walk_off": "WALK-OFF", "game_winning": "GAME-WINNING", "triple_play": "TRIPLE PLAY",
    "zero_diff": "0 DIFF", "five_hundred_diff": "500 DIFF", "lead_change": "LEAD CHANGE",
    "bases_loaded": "BASES LOADED", "double_play": "DOUBLE PLAY", "steal": "STEAL",
    "high_leverage": "HIGH LEVERAGE", "wpa": "BIG WPA",
}

CLASSIC = {"l": [["score", "inning"], ["batter"], ["pitcher"], ["scoring"], ["result", "bases", "outs"]]}
SPOTLIGHT = {"l": [["tags"], ["score", "inning"], ["batter_last", "pitcher_last"], ["scoring"], ["result_full", "bases", "outs"]]}   # the default for key moments (the Spotlight preset in push_format.js)


def upgrade(fmt: dict) -> dict:
    """An older save: "b" (bases and outs style in one) becomes bs + os, and "r" (result wording, with a combined "result" component that
    was the name plus the result) becomes the separate batter and result / result_full components."""
    out = {k: v for k, v in fmt.items() if k not in ("r", "b")}
    if "r" in fmt:
        res = "result_full" if fmt.get("r") == "label" else "result"
        lines = []
        for line in fmt.get("l") or []:
            new = []
            for cid in line if isinstance(line, list) else []:
                new += ["batter", res] if cid == "result" else [cid]
            lines.append(new)
        out["l"] = lines
    if "b" in fmt and "bs" not in out:
        out["bs"] = fmt["b"]   # an old "emoji" is no longer a choice, so it matches no swap below
    for opt, value, old, new in SWAPS:   # an old option value becomes the matching component
        if out.get(opt) == value:
            out["l"] = [[new if cid == old else cid for cid in line] if isinstance(line, list) else line for line in (out.get("l") or [])]
    for opt, *_ in SWAPS:
        out.pop(opt, None)
    return out


def normalize(fmt: dict) -> dict:
    """The layout with every option present and valid, keys in a fixed order (the editor compares these to find the active preset)."""
    fmt = upgrade(fmt)
    out = {"l": [line if isinstance(line, list) else [] for line in (fmt.get("l") or [])][:MAX_LINES]}
    for key, default in DEFAULTS.items():
        out[key] = fmt.get(key) if fmt.get(key) in CHOICES[key] else default
    return out


def _tenths(x: float) -> str:
    """One decimal place, rounded half away from zero by integer arithmetic so the JS port matches exactly."""
    n = int(abs(x) * 10 + 0.5)
    return f"{'-' if x < 0 and n else ''}{n // 10}.{n % 10}"


def _signed_pct(x: float) -> str:
    n = int(abs(x * 100) * 10 + 0.5)
    sign = "-" if x < 0 and n else "+"
    return f"{sign}{n // 10}.{n % 10}%"


def _pct(x: float) -> int:
    return int(min(1.0, max(0.0, x)) * 100 + 0.5)


def _bases(m: dict, style: str) -> str:
    obc = m.get("obc_after") or "000"        # 3rd 2nd 1st, '001' = runner on 1st
    if style == "words":
        on = [name for name, i in (("1st", 2), ("2nd", 1), ("3rd", 0)) if obc[i] == "1"]
        if not on:
            return "Bases empty"
        if len(on) == 3:
            return "Bases loaded"
        return ("Runner on " if len(on) == 1 else "Runners on ") + (on[0] if len(on) == 1 else f"{on[0]} and {on[1]}")
    return " ".join("◆" if obc[i] == "1" else "◇" for i in (0, 1, 2))


def _outs(m: dict) -> str:
    n = m.get("outs_after")
    if n is None:
        return ""
    return f"{n} out" + ("" if n == 1 else "s")


def _name(m: dict, key: str, style: str) -> str:
    """A player's name from the row's <key>_name, or <key>_last for the last-name style (full when the row has no last name)."""
    full = m.get(key + "_name") or ""
    return (m.get(key + "_last") or full) if style == "last" else full


def _people(m: dict, style: str) -> tuple[str, str]:
    """(batter or runner, "vs pitcher" or "vs catcher") with both names in one style, "full" or "last"."""
    if m["result"][:2] in ("SB", "CS"):
        ckey = "featured" if m.get("featured_side") == "fielding" else "counterpart"
        return (_name(m, "runner" if m.get("runner_name") else "batter", style),
                f"vs {_name(m, ckey, style)}" if m.get(ckey + "_name") else "")
    return (_name(m, "batter" if m.get("batter_name") else "featured", style),
            f"vs {_name(m, 'pitcher', style)}" if m.get("pitcher_name") else "")


def parts(m: dict, labels: dict, short: dict, f: dict) -> dict[str, str]:
    full, last = _people(m, "full"), _people(m, "last")
    names = m.get("scoring_names") or []   # last names only, so a big inning stays short
    scoring = "" if not names else (f"{names[0]} scores" if len(names) == 1 else f"{', '.join(names)} score")
    tags = [TAG_TEXT[t] for t in TAG_ORDER if t in (m.get("tags") or [])][:1]   # one tag, the most notable
    pitch, swing, diff = m.get("pitch"), m.get("swing"), m.get("diff")
    lev = m.get("leverage_after")   # leverage of the situation the play leaves behind
    if f["pv"] == "feat":
        abbr, wpa, after = m.get("featured_team_abbr"), m.get("featured_wpa"), m.get("featured_wp_after")
        before = None if after is None or wpa is None else after - wpa
    else:
        abbr, wpa, after, before = m.get("off_team_abbr"), m.get("wpa"), m.get("win_prob_after"), m.get("win_prob_before")
    wp = "" if after is None or not abbr else f"{abbr} WP {_pct(after)}%"
    wp_change = "" if after is None or not abbr else (f"{abbr} WP {_pct(before)}% → {_pct(after)}%" if before is not None else wp)
    return {
        "score": f'{m["away_team_abbr"]} {m["away_score"]} - {m["home_team_abbr"]} {m["home_score"]}',
        "inning": f'{"▲" if m["half"] == "top" else "▼"} {m["inning"]}',
        "batter": full[0],
        "batter_last": last[0],
        "result": (short or {}).get(m["result"], m["result"]),
        "result_full": (labels or {}).get(m["result"], m["result"]),
        "pitcher": full[1],
        "pitcher_last": last[1],
        "scoring": scoring,
        "bases": _bases(m, "glyph"),
        "bases_words": _bases(m, "words"),
        "outs": _outs(m),
        "wpa": "" if wpa is None else f"WPA {_signed_pct(wpa)}",
        "lev": "" if lev is None else f"LI {_tenths(lev)}",
        "wp": wp,
        "wp_change": wp_change,
        "diff": f"Pitch {pitch} / Swing {swing} / Diff {diff}" if pitch and swing and diff is not None else "",
        "tags": " · ".join(tags),
    }


def _sep(prev: str, cur: str) -> str:
    if prev == "score" and cur == "inning":
        return " "
    if prev in ("batter", "batter_last") and cur in ("result", "result_full", "pitcher", "pitcher_last"):
        return " "
    return " · "


def render(m: dict, fmt: dict, labels: dict, short: dict) -> tuple[str, str] | None:
    """(title, body) for this play in the subscriber's layout, or None when nothing is left to show (the caller then uses the default)."""
    f = normalize(fmt)
    p = parts(m, labels, short, f)
    lines = []
    for line in f["l"]:
        out, prev = "", ""
        for cid in line:
            text = p.get(cid, "") if isinstance(cid, str) else ""
            if not text:
                continue
            out += (_sep(prev, cid) if out else "") + text
            prev = cid
        if out:
            lines.append(out)
    if not lines:
        return None
    return lines[0], "\n".join(lines[1:])

"""
Parse Discord transaction channel JSON export into a structured CSV.

Two rows per trade (one per team perspective). All transaction types included.
Run: python parse_transactions.py
Output: transactions.csv
"""

import json
import re
import csv

JSON_PATH = "Major League Numberball - Media Channels - transactions [517418827827642369].json"
DRAFT_JSON_PATH = "Major League Numberball - Media Channels - draft-results [518838346588487703].json"
CSV_PATH = "transactions.csv"

# ---------------------------------------------------------------------------
# Season boundary inference (approximate, based on known dates)
# ---------------------------------------------------------------------------
SEASON_BOUNDARIES = [
    ("2018-11-28", "2019-03-01", 2),
    ("2019-03-01", "2019-09-01", 3),
    ("2019-09-01", "2020-06-01", 4),
    ("2020-06-01", "2020-12-01", 5),
    ("2020-12-01", "2021-06-01", 6),
    ("2021-06-01", "2022-01-01", 7),
    ("2022-01-01", "2022-09-01", 8),
    ("2022-09-01", "2023-06-01", 9),
    ("2023-06-01", "2024-01-01", 10),
    ("2024-01-01", "2024-09-01", 11),
    ("2024-09-01", "2025-06-01", 12),
    ("2025-06-01", "2026-12-31", 13),
]

def infer_season(date_str: str) -> int | None:
    for start, end, s in SEASON_BOUNDARIES:
        if start <= date_str < end:
            return s
    return None

# ---------------------------------------------------------------------------
# Markdown / emoji stripping
# ---------------------------------------------------------------------------
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_UNDERLINE = re.compile(r"__(.+?)__")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_CODE = re.compile(r"`(.+?)`")
# Discord custom emoji :name: or :name_name:
_DISCORD_EMOJI = re.compile(r":[a-zA-Z0-9_\-]+:")

def strip_md(text: str) -> str:
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_UNDERLINE.sub(r"\1", text)
    text = _MD_STRIKE.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    return text.strip()

def clean_line(text: str) -> str:
    """Strip markdown, replace Discord :emoji: codes with just the slug, normalize whitespace."""
    text = strip_md(text)
    # Replace :teamname: with just "teamname" (preserves team name references)
    text = re.sub(r":([a-zA-Z0-9_\-]+):", r" \1 ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def extract_team_prefix(line: str) -> tuple[str, str]:
    """
    If line starts with :emoji_code: extract it as the team identifier.
    Returns (team_name, rest_of_line) where team_name is the slug between colons.
    """
    line = line.strip()
    m = re.match(r"^:([a-zA-Z0-9_\-]+):\s*(.*)", line)
    if m:
        return m.group(1), m.group(2).strip()
    return "", line

# ---------------------------------------------------------------------------
# Session / Season markers
# ---------------------------------------------------------------------------
_SESSION = re.compile(r"[Ss]ession\s*#?\s*(\d+)")
_SEASON_MARKER = re.compile(r"[Ss]eason\s*#?\s*(\d+)")

# ---------------------------------------------------------------------------
# Transaction patterns (applied after MD-stripping but before emoji removal
# so team emoji codes can still be used as team identifiers)
# ---------------------------------------------------------------------------

def _ci(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)

# Sign
P_SIGN = _ci(
    r"(?P<team>.+?)\s+(?:have\s+|has\s+)?signs?\s+(?P<player>.+)"
)
P_SIGN_WITH = _ci(
    r"(?P<player>.+?)\s+(?:have\s+|has\s+)?signs?\s+(?:with|to)\s+(?P<team>.+)"
)
P_SIGNED_WITH = _ci(
    r"(?P<player>.+?)\s+signed\s+(?:with|to)\s+(?P<team>.+)"
)

# Re-sign
P_RESIGN = _ci(
    r"(?P<team>.+?)\s+re-?signs?\s+(?P<player>.+)"
)
P_RESIGN_WITH = _ci(
    r"(?P<player>.+?)\s+re-?signs?\s+(?:with|to)\s+(?P<team>.+)"
)

# Release / DFA / Cut
P_RELEASE = _ci(
    r"(?P<team>.+?)\s+(?:release[sd]?|cut[s]?|DFA[sd]?)\s+(?P<player>.+)"
)
P_RELEASED_BY = _ci(
    r"(?P<player>.+?)\s+(?:released|cut|DFAd)\s+by\s+(?P<team>.+)"
)

# Trade - receives line
P_TRADE_HEADER = _ci(r"\bTRADE\b")
P_RECEIVES = _ci(
    r"(?P<team>.+?)\s+[Rr]eceive[s]?:?\s+(?P<assets>.+)"
)
# Old-style: "Team A trades Player to Team B for Player2"
P_TRADE_OLD = _ci(
    r"(?P<team_a>.+?)\s+trades?\s+(?P<asset_a>.+?)\s+(?:to|for)\s+(?P<team_b>.+?)(?:\s+for\s+(?P<asset_b>.+))?"
)

# Retirement (ensure "returning from retirement" doesn't match)
P_RETIRE = _ci(
    r"(?P<player>.+?)\s+(?:retires?|announces?\s+(?:his\s+|her\s+|their\s+)?retirement)"
)
P_UNRETIRE = _ci(
    r"(?P<player>.+?)\s+(?:un-?retires?|comes?\s+out\s+of\s+retirement|returns?\s+from\s+retirement|returning\s+from\s+retirement)"
)

# Hiatus on: many verb forms including "X to hiatus (list)"
P_HIATUS_ON = _ci(
    r"(?:(?P<team>.+?)\s+(?:move[sd]?|place[sd]?|put[s]?)\s+(?P<player>.+?)\s+(?:to|on(?:\s+the)?)\s+hiatus"
    r"|(?P<player2>.+?)\s+(?:goes?\s+on|is\s+placed\s+on)\s+hiatus"
    r"|(?P<player3>.+?)\s+(?:on\s+)?hiatus(?:\s+list)?$)"
)
# Hiatus return - player3 alternatives first (more specific) to avoid greedy team2 match
P_HIATUS_OFF = _ci(
    r"(?:(?P<team>.+?)\s+(?:activate[sd]?|actives?)\s+(?P<player>.+?)\s+from\s+hiatus"
    r"|(?P<player3>.+?)\s+(?:returns?\s+from\s+hiatus(?:\s+list)?|activated\s+from\s+hiatus|off\s+hiatus)"
    r"|(?P<team2>.+?)\s+(?:move[sd]?)\s+(?P<player2>.+?)\s+from\s+hiatus)"
)

# Position switch - handles "primary/secondary position", bare "position to"
P_POS_SWITCH = _ci(
    r"(?P<player>.+?)\s+(?:switch(?:es|ed|ing)?|move[sd]?|convert[sd]?|chang(?:es?|ed|ing)?)"
    r"(?:\s+(?:primary|secondary|their))?\s+(?:position\s+)?(?:from\s+\S+\s+)?(?:to\s+)"
    r"(?P<position>[A-Z0-9/]+(?:\s+[A-Z0-9/]+)?)(?:\s*$|\s*[,\(])"
)

# Name change
P_NAME_CHANGE = _ci(
    r"(?:(?P<old>.+?)\s+(?:now\s+goes?\s+by|is\s+now(?:\s+known\s+as)?|changed?\s+(?:(?:their|his|her)\s+)?name\s+to)\s+(?P<new>.+)"
    r"|(?P<old2>.+?)\s*(?:→|->|=>)\s*(?P<new2>.+))"
)

# GM / role change - also handles "promote X to GM"
P_GM_CHANGE = _ci(
    r"(?P<person>.+?)\s+(?:takes?\s+over|becomes?|is\s+(?:now\s+)?(?:the\s+)?(?:new\s+)?|named|appointed|promot\w*).{0,40}"
    r"(?:GM|General\s+Manager|Captain|co-?GM|owner)\s*(?:of\s+(?:the\s+)?)?(?P<team>.+)?"
)
# "receives Player from Team" - player acquisitions stated without TRADE header
P_RECEIVES_FROM = _ci(
    r"(?P<team>.+?)\s+receive[s]?\s+(?P<player>.+?)\s+from\s+(?P<source>.+)"
)

# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def base_row(date, season, session, raw) -> dict:
    return {
        "date": date,
        "season": season or "",
        "session": session or "",
        "type": "",
        "team": "",
        "player": "",
        "position": "",
        "trade_partner": "",
        "assets_sent": "",
        "assets_received": "",
        "notes": "",
        "raw_message": raw,
        "needs_review": "",
    }

# ---------------------------------------------------------------------------
# Post-process helpers
# ---------------------------------------------------------------------------

_POS_PARENS = re.compile(r"\s*\([^)]*\)\s*$")
_TRAILING_JUNK = re.compile(r"\s*(?:,\s*who\s+.+|&\s*.+|\s+and\s+.+)$", re.IGNORECASE)

def trim_player(name: str) -> str:
    """Remove trailing position notes, conjunctions, and extra whitespace."""
    name = _POS_PARENS.sub("", name)
    name = _TRAILING_JUNK.sub("", name)
    # strip team emoji codes that bled in
    name = _DISCORD_EMOJI.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def extract_position(player_str: str) -> tuple[str, str]:
    """Pull '(position)' out of a player string. Returns (clean_name, position)."""
    m = re.search(r"\(([^)]+)\)", player_str)
    if m:
        pos = m.group(1).strip()
        name = re.sub(r"\s*\([^)]+\)", "", player_str).strip()
        return name, pos
    return player_str, ""

# ---------------------------------------------------------------------------
# Single line parser - returns a row dict or None
# ---------------------------------------------------------------------------

def parse_line(line: str, team_hint: str, date: str, season, session: int | None, raw: str) -> dict | None:
    """
    Try to parse a single line into one transaction row.
    team_hint is the team extracted from :emoji: prefix (may be "" if none).
    Returns None if line doesn't match any pattern.
    """
    # Clean the line (strip md, emojis, normalize)
    lc = clean_line(line)
    if not lc or len(lc) < 4:
        return None

    # Skip pure header/label lines
    if re.match(
        r"^(season|session|free\s+agents?|transactions?|signings?|releases?|retirements?|notes?)\s*:?\s*$",
        lc, re.I
    ):
        return None

    # When team is known from prefix and line starts with a verb, prepend team
    # so standard "team verb player" patterns can match
    _VERB_FIRST = re.compile(
        r"^(re-?signs?|signs?|release[sd]?|cut[s]?|dfa[sd]?|activat\w*|actives?\s|place[sd]?\s|"
        r"move[sd]?\s|receive[sd]?|gets?)\b",
        re.IGNORECASE
    )
    if team_hint and _VERB_FIRST.match(lc):
        lc = team_hint + " " + lc

    row = base_row(date, season, session, raw)

    # -- Unretire (before retire) --
    m = P_UNRETIRE.match(lc)
    if m:
        player = trim_player(m.group("player"))
        row.update({"type": "unretire", "player": player})
        return row

    # -- Hiatus return (before hiatus_on) --
    m = P_HIATUS_OFF.match(lc)
    if m:
        team = ""
        player = ""
        try: team = m.group("team") or ""
        except IndexError: pass
        try: team = team or m.group("team2") or ""
        except IndexError: pass
        team = team.strip() or team_hint
        try: player = m.group("player") or ""
        except IndexError: pass
        try: player = player or m.group("player2") or ""
        except IndexError: pass
        try: player = player or m.group("player3") or ""
        except IndexError: pass
        row.update({"type": "hiatus_return", "team": team, "player": trim_player(player)})
        return row

    # -- Hiatus on --
    m = P_HIATUS_ON.match(lc)
    if m:
        team = ""
        player = ""
        try: team = m.group("team") or ""
        except IndexError: pass
        team = team.strip() or team_hint
        for grp in ("player", "player2", "player3"):
            try:
                v = m.group(grp)
                if v:
                    player = v
                    break
            except IndexError:
                pass
        row.update({"type": "hiatus", "team": team, "player": trim_player(player)})
        return row

    # -- Retirement --
    m = P_RETIRE.match(lc)
    if m:
        player = trim_player(m.group("player"))
        # false-positive guard: if "retirement" appears earlier it's unretire territory
        if "returning from" in lc.lower() or "out of retirement" in lc.lower():
            return None
        row.update({"type": "retirement", "player": player, "team": team_hint})
        return row

    # -- Name change --
    m = P_NAME_CHANGE.match(lc)
    if m:
        old = (m.group("old") or m.group("old2") or "").strip()
        new = (m.group("new") or m.group("new2") or "").strip()
        row.update({"type": "name_change", "player": trim_player(old),
                    "notes": f"new name: {trim_player(new)}"})
        return row

    # -- Position switch --
    m = P_POS_SWITCH.match(lc)
    if m:
        player, pos = extract_position(m.group("player"))
        new_pos = m.group("position").strip()
        row.update({"type": "position_switch", "player": trim_player(player),
                    "position": new_pos, "team": team_hint})
        return row

    # -- GM / role change --
    m = P_GM_CHANGE.search(lc)
    if m and any(kw in lc.lower() for kw in ["gm", "general manager", "captain", "co-gm", "owner", "promot"]):
        person = trim_player(m.group("person") or "")
        team = (m.group("team") or "").strip() or team_hint
        row.update({"type": "gm_change", "player": person, "team": team, "notes": lc})
        return row

    # -- Re-sign (before sign) --
    m = P_RESIGN.match(lc) or P_RESIGN_WITH.match(lc)
    if m:
        if m.lastgroup == "team" and P_RESIGN_WITH.match(lc):
            team = (m.group("team") or team_hint).strip()
            player, pos = extract_position(m.group("player"))
        else:
            team = (m.group("team") or team_hint).strip()
            player, pos = extract_position(m.group("player"))
        row.update({"type": "re_sign", "team": team,
                    "player": trim_player(player), "position": pos})
        return row

    # -- Release / DFA / Cut --
    m = P_RELEASE.match(lc)
    if m:
        team = (m.group("team") or team_hint).strip()
        player, pos = extract_position(m.group("player"))
        row.update({"type": "release", "team": team,
                    "player": trim_player(player), "position": pos})
        return row
    m = P_RELEASED_BY.match(lc)
    if m:
        team = (m.group("team") or team_hint).strip()
        player, pos = extract_position(m.group("player"))
        row.update({"type": "release", "team": team,
                    "player": trim_player(player), "position": pos})
        return row

    # -- Sign (FA pickup) --
    m = P_SIGN.match(lc)
    if m:
        team = (m.group("team") or team_hint).strip()
        player, pos = extract_position(m.group("player"))
        row.update({"type": "sign", "team": team,
                    "player": trim_player(player), "position": pos})
        return row
    m = P_SIGN_WITH.match(lc) or P_SIGNED_WITH.match(lc)
    if m:
        team = (m.group("team") or team_hint).strip()
        player, pos = extract_position(m.group("player"))
        row.update({"type": "sign", "team": team,
                    "player": trim_player(player), "position": pos})
        return row

    # -- Receives from (player acquisition stated without TRADE header) --
    m = P_RECEIVES_FROM.match(lc)
    if m:
        team = (m.group("team") or team_hint).strip()
        player, pos = extract_position(m.group("player"))
        row.update({"type": "trade", "team": team,
                    "assets_received": trim_player(player),
                    "trade_partner": m.group("source").strip(),
                    "position": pos, "needs_review": "Y",
                    "notes": "one-sided receive entry - trade partner assets unknown"})
        return row

    return None

# ---------------------------------------------------------------------------
# Message-level parser
# ---------------------------------------------------------------------------

def parse_message(content: str, date: str, season, raw: str) -> list[dict]:
    rows: list[dict] = []

    # Extract session from content
    session = None
    sm = _SESSION.search(content)
    if sm:
        session = int(sm.group(1))

    # Override season from content marker
    seas_m = _SEASON_MARKER.search(content)
    if seas_m:
        season = int(seas_m.group(1))

    lines = [l for l in content.splitlines() if l.strip()]

    # -----------------------------------------------------------------------
    # TRADE block: message with TRADE keyword OR 2+ "receive" lines
    # -----------------------------------------------------------------------
    _receive_count = len(re.findall(r"\breceive[s]?\b", content, re.IGNORECASE))
    if P_TRADE_HEADER.search(content) or _receive_count >= 2:
        trade_sides: list[tuple[str, str]] = []

        pending_team = ""  # for multiline "Team:\nReceives:\nassets" format
        for line in lines:
            # Check for "Team receives: assets" either from prefix or inline
            team_code, rest = extract_team_prefix(line)
            rest_clean = clean_line(rest)
            team_clean = team_code  # raw slug from prefix

            # "Receives:" with no assets on same line -> assets on next line(s)
            if re.match(r"[Rr]eceive[s]?:?\s*$", rest_clean) and team_clean:
                pending_team = team_clean
                continue

            # If we have a pending team waiting for assets, this line is the assets
            if pending_team and rest_clean:
                assets = clean_line(rest)
                if assets and not re.match(r"[Rr]eceive[s]?:", assets):
                    trade_sides.append((pending_team, assets))
                    pending_team = ""
                    continue

            # "Receives: assets" with team from prefix on same line
            rec_m = re.match(r"[Rr]eceive[s]?:?\s+(?P<assets>.+)", rest_clean)
            if rec_m and team_clean:
                trade_sides.append((team_clean, rec_m.group("assets").strip()))
                pending_team = ""
                continue

            # Inline "Team receives: assets"
            rec_m2 = P_RECEIVES.match(clean_line(line))
            if rec_m2:
                trade_sides.append((
                    strip_md(rec_m2.group("team")).strip(),
                    rec_m2.group("assets").strip(),
                ))
                pending_team = ""
                continue

        if len(trade_sides) >= 2:
            team_a, a_receives = trade_sides[0]
            team_b, b_receives = trade_sides[1]
            r1 = base_row(date, season, session, raw)
            r1.update({"type": "trade", "team": team_a, "trade_partner": team_b,
                        "assets_received": a_receives, "assets_sent": b_receives})
            r2 = base_row(date, season, session, raw)
            r2.update({"type": "trade", "team": team_b, "trade_partner": team_a,
                        "assets_received": b_receives, "assets_sent": a_receives})
            rows.extend([r1, r2])
            for i in range(2, len(trade_sides)):
                team_x, x_receives = trade_sides[i]
                rx = base_row(date, season, session, raw)
                rx.update({"type": "trade", "team": team_x,
                            "assets_received": x_receives,
                            "notes": "3-way trade - verify assets manually",
                            "needs_review": "Y"})
                rows.append(rx)
            # Also parse non-trade lines in the same message (e.g. retirements bundled with trades)
            non_trade_rows = _parse_non_trade_lines(lines, date, season, session, raw, skip_receives=True)
            rows.extend(non_trade_rows)
        else:
            r = base_row(date, season, session, raw)
            r.update({"type": "trade", "needs_review": "Y",
                       "notes": "Trade detected but sides not parsed"})
            rows.append(r)
        return rows

    # -----------------------------------------------------------------------
    # Non-trade messages
    # -----------------------------------------------------------------------
    non_trade_rows = _parse_non_trade_lines(lines, date, season, session, raw)
    rows.extend(non_trade_rows)

    if not rows:
        r = base_row(date, season, session, raw)
        r["needs_review"] = "Y"
        r["notes"] = "Not parsed - review manually"
        rows.append(r)

    return rows


def _parse_non_trade_lines(lines, date, season, session, raw, skip_receives=False) -> list[dict]:
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Extract team from :emoji: prefix
        team_hint, rest = extract_team_prefix(line)

        # Skip session/season headers
        if re.match(r"\*\*.*(?:session|season|transaction|effective).*\*\*", line, re.I):
            continue
        if re.match(r"#+\s+", line):  # markdown header
            continue

        # Skip "Receive:" lines inside trade blocks already handled
        if skip_receives and re.match(r"[Rr]eceive[s]?:", rest.strip()):
            continue

        # Try parsing in order:
        # 1. rest alone (with team_hint for fallback); handles ":team: verb player" format
        # 2. full cleaned line (handles "Team verb Player" plain text)
        rest_clean = clean_line(rest) if rest else ""
        full_clean = clean_line(line)

        if not team_hint:
            # No prefix - just try the full line once
            row = parse_line(full_clean, "", date, season, session, raw)
            if row:
                rows.append(row)
        else:
            # Has :team: prefix - try rest first (verb-first patterns), then full line
            matched = False
            for candidate in [rest_clean, full_clean]:
                if not candidate:
                    continue
                row = parse_line(candidate, team_hint, date, season, session, raw)
                if row:
                    if not row["team"]:
                        row["team"] = team_hint
                    rows.append(row)
                    matched = True
                    break

    return rows

# ===========================================================================
# DRAFT RESULTS PARSER
# ===========================================================================

DRAFT_JSON_PATH = "Major League Numberball - Media Channels - draft-results [518838346588487703].json"

ORDINAL_MAP = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
    "twenty-first": 21, "twenty-second": 22, "twenty-third": 23,
    "twenty-fourth": 24, "twenty-fifth": 25, "twenty-sixth": 26,
    "twenty-seventh": 27, "twenty-eighth": 28, "twenty-ninth": 29,
    "thirtieth": 30,
}

# Standard pick - anchors draft_str on the word "draft" for clean separation
P_DRAFT_PICK = re.compile(
    r"[Ww]ith\s+the\s+(?P<pick_str>.+?)\s+(?:overall\s+)?(?:pick|selection|pic)\s*"
    r"(?:in\s+(?:the\s+)?|of\s+(?:the\s+)?)?"
    r"(?P<draft_str>.+?[Dd]raft)\s*,?\s*"
    r"(?:the\s+)?(?P<team>.+?)\s+(?:select[s]?|choose[s]?|pick[s]?),?\s+"
    r"(?P<player>.+)",
    re.IGNORECASE,
)

# "With pick N pick in the draft, Team selects Player"
P_DRAFT_PICK_ALT = re.compile(
    r"[Ww]ith\s+pick\s+(?P<pick_str>\d+)\s+(?:pick\s+)?in\s+(?:the\s+)?(?P<draft_str>[^,]+?draft)\s*,?\s*"
    r"(?:the\s+)?(?P<team>.+?)\s+(?:select[s]?|choose[s]?|pick[s]?),?\s+"
    r"(?P<player>.+)",
    re.IGNORECASE,
)

# "penultimate selection of N" variant
P_DRAFT_PENULTIMATE = re.compile(
    r"[Ww]ith\s+the\s+penultimate\s+selection\s+of\s+(?P<pick_str>\d+)\s+in\s+(?:the\s+)?(?P<draft_str>[^,]+?draft)\s*,?\s*"
    r"(?:the\s+)?(?P<team>.+?)\s+(?:select[s]?|choose[s]?|pick[s]?),?\s+"
    r"(?P<player>.+)",
    re.IGNORECASE,
)

# Fallback: no draft description - "With the Nth pick [the] Team select Player"
P_DRAFT_PICK_SIMPLE = re.compile(
    r"[Ww]ith\s+the\s+(?P<pick_str>.+?)\s+(?:overall\s+)?(?:pick|selection|pic)\s+"
    r"(?:the\s+)?(?P<team>.+?)\s+(?:select[s]?|choose[s]?|pick[s]?),?\s+"
    r"(?P<player>.+)",
    re.IGNORECASE,
)

# Draft pick trade: "Team trade[s] Nth pick to OtherTeam for [assets]"
P_DRAFT_PICK_TRADE = re.compile(
    r"(?P<team>.+?)\s+(?:have\s+)?trades?\s+(?P<assets_sent>.+?)\s+(?:to|with)\s+(?P<partner>.+?)"
    r"\s+for\s+(?P<assets_recv>.+)",
    re.IGNORECASE,
)

# "Team trade Nth overall to OtherTeam for..." (with :emoji: prefix stripped)
P_DRAFT_PICK_TRADE2 = re.compile(
    r"(?P<team>.+?)\s+trade\s+(?P<assets_sent>.+?)\s+(?:to|with)\s+:?(?P<partner>.+?):?"
    r"\s+(?:for|and)\s+(?P<assets_recv>.+)",
    re.IGNORECASE,
)

# Abstain
P_DRAFT_ABSTAIN = re.compile(
    r"(?P<team>.+?)\s+(?:have\s+)?abstained?\s+(?:all\s+)?remaining\s+picks?",
    re.IGNORECASE,
)


def parse_pick_number(pick_str: str) -> int | None:
    pick_str = pick_str.strip().lower()
    m = re.match(r"^(\d+)", pick_str)
    if m:
        return int(m.group(1))
    return ORDINAL_MAP.get(pick_str)


def parse_draft_label(draft_str: str) -> tuple[str, int | None]:
    """Return (draft_type, season_number). draft_type = 'MLN' or 'WNC'."""
    lower = draft_str.lower().strip()
    if "wnc" in lower:
        return "WNC", None
    m = re.search(r"(?:s|season)\s*(\d+)", lower)
    season = int(m.group(1)) if m else None
    return "MLN", season


def clean_draft_player(raw: str) -> tuple[str, str]:
    """Strip @, position prefix, discord mentions, bold. Returns (name, position)."""
    raw = re.sub(r"<@!?\d+>", "", raw)  # strip discord @mentions
    raw = re.sub(r"@", "", raw)
    raw = strip_md(raw)
    # strip inline emoji codes
    raw = re.sub(r":([a-zA-Z0-9_\-]+):", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    # trailing punctuation
    raw = raw.rstrip(".,!")
    # leading position code: "P Name", "1B Name", "LF Name", "SS/UTIL Name"
    pm = re.match(r"^([A-Z0-9]{1,2}(?:/[A-Z]+)?)\s+(.+)", raw)
    if pm and len(pm.group(1)) <= 7:
        return pm.group(2).strip(), pm.group(1)
    return raw, ""


def parse_draft_messages() -> list[dict]:
    """Parse the draft-results JSON into transaction rows."""
    try:
        with open(DRAFT_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"  Draft JSON not found: {DRAFT_JSON_PATH}")
        return []

    rows: list[dict] = []
    current_draft_type = "MLN"
    current_season: int | None = None

    for msg in data["messages"]:
        content = msg.get("content", "").strip()
        if not content:
            continue
        date_str = msg["timestamp"][:10]
        raw = content

        # Update season from boundary inference
        inferred = infer_season(date_str)
        if inferred and current_draft_type == "MLN":
            current_season = inferred

        # Detect draft type header (WNC vs MLN)
        if re.search(r"\bWNC\b", content, re.I):
            current_draft_type = "WNC"
        elif re.search(r"(?:Season|S)\s*\d+\s+(?:Amateur\s+)?Draft", content, re.I):
            current_draft_type = "MLN"
            m = re.search(r"(?:Season|S)\s*(\d+)", content, re.I)
            if m:
                current_season = int(m.group(1))

        # Skip pure header / admin / incomplete messages
        if re.search(r"(AHEM|concludes|build\s+some|suspense|Hype|IF YOU WEREN|STAY ACTIVE|Let'?s get|draft is complete|draft complete)", content, re.I):
            continue
        if re.match(r"---+", content.strip()):
            continue
        # Skip ellipsis-only picks like "With the eleventh pick..."
        if re.match(r"[Ww]ith the .{1,30}\.\.\.", content.strip()):
            continue

        # ---------------------------------------------------------------
        # Abstain
        # ---------------------------------------------------------------
        am = P_DRAFT_ABSTAIN.match(clean_line(content))
        if am:
            r = base_row(date_str, current_season, None, raw)
            r.update({"type": "draft_abstain", "team": am.group("team").strip(),
                       "notes": f"{current_draft_type} draft abstain"})
            rows.append(r)
            continue

        # ---------------------------------------------------------------
        # Pick trade within draft (has TRADE-like language, no pick pattern)
        # ---------------------------------------------------------------
        is_pick = bool(
            re.search(r"\bwith\s+the\s+", content, re.I) or
            re.search(r"\bwith\s+pick\b", content, re.I)
        )
        is_pick_trade = (
            not is_pick and
            re.search(r"\b(?:trade[sd]?|gives?|swap[ps]?)\b", content, re.I) and
            re.search(r"\bpick\b|\broad\b|\boverall\b|\bfirst\b|\bsecond\b|\bthird\b", content, re.I)
        )
        # Also catch ":team: receive: pick N" blocks
        is_receive_block = (
            not is_pick and
            len(re.findall(r"\breceive[s]?:", content, re.I)) >= 1
        )

        if is_pick_trade or is_receive_block:
            # Reuse the transaction trade-block logic
            sub_rows = parse_message(content, date_str, current_season, raw)
            for r in sub_rows:
                if r["type"] in ("trade", "sign", "release"):
                    r["type"] = "draft_pick_trade"
                r["notes"] = (r["notes"] or "") + f" | {current_draft_type} draft"
            rows.extend(sub_rows)
            continue

        # ---------------------------------------------------------------
        # Standard pick
        # ---------------------------------------------------------------
        # Try primary pattern, alt, penultimate
        lc = clean_line(content)
        pick_m = (P_DRAFT_PICK.search(lc) or P_DRAFT_PICK_ALT.search(lc)
                  or P_DRAFT_PENULTIMATE.search(lc) or P_DRAFT_PICK_SIMPLE.search(lc))

        if pick_m:
            pick_num = parse_pick_number(pick_m.group("pick_str"))
            try:
                raw_draft_str = pick_m.group("draft_str")
            except IndexError:
                raw_draft_str = ""
            draft_type, draft_season = parse_draft_label(raw_draft_str) if raw_draft_str else (current_draft_type, None)
            if draft_season:
                current_season = draft_season
            actual_draft_type = draft_type if draft_type else current_draft_type

            team = pick_m.group("team").strip()
            player_raw = pick_m.group("player")
            player, pos = clean_draft_player(player_raw)

            note = f"Pick {pick_num} | {actual_draft_type}"
            if actual_draft_type == "WNC":
                note += " Draft"
            elif current_season:
                note += f" S{current_season}"

            r = base_row(date_str, current_season, None, raw)
            r.update({
                "type": "draft",
                "team": team,
                "player": player,
                "position": pos,
                "notes": note,
            })
            rows.append(r)
            continue

        # ---------------------------------------------------------------
        # Unknown - skip silently if non-informative, flag otherwise
        # ---------------------------------------------------------------
        lower = content.lower()
        if any(kw in lower for kw in ["select", "choose", "pick", "draft", "trade", "receive"]):
            r = base_row(date_str, current_season, None, raw)
            r.update({"type": "draft", "needs_review": "Y",
                       "notes": f"Draft message not parsed | {current_draft_type}"})
            rows.append(r)

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_TRANSACTION_KWS = re.compile(
    r"\b(sign|release|cut|dfa|trade|retir|hiatus|switch|position|activat|"
    r"name\s+change|gm|captain|receives?|re-sign|resign|unretir)\b",
    re.IGNORECASE,
)

def main():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data["messages"]
    all_rows: list[dict] = []
    current_season: int | None = None

    for msg in messages:
        content = msg.get("content", "").strip()
        if not content:
            continue

        date_str = msg["timestamp"][:10]

        inferred = infer_season(date_str)
        if inferred:
            current_season = inferred

        seas_m = _SEASON_MARKER.search(content)
        if seas_m:
            current_season = int(seas_m.group(1))

        # Skip channel description / meta messages
        if content.startswith("Members of the media can break"):
            continue
        if re.match(r"(Simple log|This channel)", content, re.I):
            continue

        # Skip non-transaction-like messages
        if not _TRANSACTION_KWS.search(content):
            continue

        rows = parse_message(content, date_str, current_season, content)
        all_rows.extend(rows)

    # Parse draft results
    draft_rows = parse_draft_messages()
    all_rows.extend(draft_rows)

    # Sort by date
    all_rows.sort(key=lambda r: r["date"])

    # Write CSV
    fieldnames = [
        "date", "season", "session", "type",
        "team", "player", "position",
        "trade_partner", "assets_sent", "assets_received",
        "notes", "raw_message", "needs_review",
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    total = len(all_rows)
    tx_total = total - len(draft_rows)
    needs_review = sum(1 for r in all_rows if r.get("needs_review") == "Y")
    print(f"Done. {total} rows written to {CSV_PATH}")
    print(f"  Transactions: {tx_total}  |  Draft picks: {len(draft_rows)}")
    print(f"  Needs review: {needs_review} ({needs_review/total*100:.1f}%)")

    from collections import Counter
    types = Counter(r["type"] for r in all_rows)
    for t, count in types.most_common():
        print(f"  {t or '(blank)'}: {count}")

if __name__ == "__main__":
    main()

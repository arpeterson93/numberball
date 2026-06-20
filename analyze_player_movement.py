"""
Cross-reference player movement between the Supabase players table and transactions.csv.

Outputs:
  movement_report.csv  - every season-to-season team change per player, with
                         match status against transactions.csv
"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

import json

import pandas as pd
from supabase import create_client

# ── credentials / paths ───────────────────────────────────────────────────────

SUPABASE_URL = "https://qxwzrbbjvivbpqchbner.supabase.co"
SUPABASE_KEY = "sb_publishable_lnlBqNXEYmwjk2KgJiKeyw_CK2Qb2PR"
TRANSACTIONS_CSV = "transactions.csv"
OUTPUT_CSV = "movement_report.csv"

# Discord JSON exports - used to resolve @handles to discord IDs via the
# mentions array (draft JSON always has mention metadata; transactions JSON
# GMs typed @handle as plain text so mentions are empty, but drafts give us
# enough of the handle->ID map to resolve most players)
DISCORD_JSON_PATHS = [
    "Major League Numberball - Media Channels - transactions [517418827827642369].json",
    "Major League Numberball - Media Channels - draft-results [518838346588487703].json",
]

PLAYER_FUZZY_THRESHOLD = 0.72
TEAM_FUZZY_THRESHOLD   = 0.60

# Manual aliases: Discord team name variants -> canonical abbrev.
# Used before fuzzy matching to handle historical names, nicknames, typos.
TEAM_ALIASES: dict[str, str] = {
    # Halifax (was "Highlanders", renamed to "Helmsmen")
    "Highlanders":                "HFX",
    "highlanders":                "HFX",
    "Halifax Highlanders":        "HFX",
    "Halifax Highalnders":        "HFX",
    "HFX Halifax Highlanders":    "HFX",
    "Halifax Baseball Team":      "HFX",
    "HFX Halifax Helmsmen":       "HFX",
    "Helmsmen":                   "HFX",
    "highlander Halifax Highlanders": "HFX",
    # Aruba (Discord often uses "Aruba" shorthand)
    "Aruba":                      "ASS",
    "SeaSerpents":                "ASS",
    "SeaSerpents Aruba Sea Serpents": "ASS",
    # Kansas City (was "Smoke", renamed to "Kitties")
    "Kansas City Smoke":          "KC",
    "KC Kansas City Smoke":       "KC",
    "KC Kitties":                 "KC",
    # Beholders (BBG and BBEG are the same franchise)
    "Beholder's of Baldur's Gate": "BBEG",
    "Beholders of Baldurs Gate":  "BBEG",
    "beholders":                  "BBEG",
    "BBG":                        "BBG",
    # Early team name variants
    "The Henrik Omegas":          "REK",  # early REK name
    "Rhinos":                     "CIN",  # possibly early CIN name
    "Rhinos CIN":                 "CIN",
    # Abbreviation+name combos that appear in transactions
    "ACP Acadia Peregrines":      "ACP",
    "CAR Carolina Reapers":       "CAR",
    "GHG Gas House Gorillas":     "GHG",
    "HFX Halifax Highlanders":    "HFX",
    "KC Kansas City Kitties":     "KC",
    "MIA Miami Fuego":            "MIA",
    "OGO Ogaki Oni":              "OGO",
    "RLY R'Lyeh Ancients":        "RLY",
    "SUN Sunnydale Slayers":      "SUN",
    # Misc shorthand forms
    "Buff":                       "BUF",
    "Buffalo":                    "BUF",
    "Bullfrogs Buffalo Bullfrogs": "BUF",
    "FlappyBois":                 "BFB",
    "FlappyBois BBFB":            "BFB",
    "FlappyBois Blubs Bay Flappy Bois": "BFB",
    "Crawdads":                   "NO",
    "Crawdads New Orleans":       "NO",
    "Crawdads New Orleans Crawdads": "NO",
    "nola":                       "NO",
    "Gorillas":                   "GHG",
    "Gorillas Gas House Gorillas": "GHG",
    "Grizzlies":                  "GRZ",
    "Grizzlies Ursa Major Grizzlies": "GRZ",
    "grizzlies":                  "GRZ",
    "Ursa Major":                 "GRZ",
    "MASH":                       "KYM",
    "Mashers":                    "KYM",
    "McDophers":                  "SMD",
    "Melonhead":                  "HMH",
    "Melonheads":                 "HMH",
    "Melonheads Humongous Melonheads": "HMH",
    "Oni":                        "OGO",
    "Oni Ogaki Oni":              "OGO",
    "Peregrine Acadia Peregrines": "ACP",
    "Peregrines":                 "ACP",
    "Peregrines Acadia Peregrines": "ACP",
    "Pioneers":                   "POR",
    "Pioneers Portland Pioneers": "POR",
    "pioneers":                   "POR",
    "Raccoon City":               "RC",
    "Reapers":                    "CAR",
    "Reapers Carolina Reapers":   "CAR",
    "rek":                        "REK",
    "Slayers":                    "SUN",
    "Slayers Sunnydale Slayers":  "SUN",
    "slayers":                    "SUN",
    "slayercrown":                "SUN",
    "slayerscrown":               "SUN",
    "The Sunnydale Slayers":      "SUN",
    "Sopher Mcdophers":           "SMD",
    "Waverunners":                "MAL",
    "Waverunners Malibu Waverunners": "MAL",
    "miami":                      "MIA",
    "miami Miami Fuego":          "MIA",
    "oslo":                       "OO",
    "otters":                     "OO",
    "ass":                        "ASS",
    "crawdads":                   "NO",
    "The Originals":              "OAK",
}

# Complete season-by-season franchise history (from league records).
# Format: franchise_name -> {season_int: abbrev}
FRANCHISE_HISTORY: dict[str, dict[int, str]] = {
    "Acadia Peregrines":     {s: "ACP" for s in range(1, 13)},
    "Gas House Gorillas":    {s: "GHG" for s in range(1, 13)},
    "R'lyeh Ancients":       {**{s: "MAL" for s in range(1, 8)}, **{s: "OGO" for s in range(8, 11)}, **{s: "RLY" for s in range(11, 13)}},
    "Portland Pioneers":     {s: "POR" for s in range(2, 13)},
    "Aruba Sea Serpents":    {**{s: "CIN" for s in range(1, 5)}, **{s: "ASS" for s in range(5, 13)}},
    "Humongous Melonheads":  {s: "HMH" for s in range(1, 13)},
    "Reykjavik Valkyries":   {**{s: "REK" for s in range(2, 5)}, **{s: "OO" for s in range(5, 7)}, **{s: "REK" for s in range(7, 13)}},
    "Sopher McDophers":      {s: "SMD" for s in range(1, 13)},
    "Baldur's Gate Beholders": {**{s: "LPJ" for s in range(1, 3)}, **{s: "BBG" for s in range(3, 10)}, **{s: "BBEG" for s in range(10, 13)}},
    "Halifax Helmsmen":      {1: "VAN", **{s: "HFX" for s in range(2, 13)}},
    "Sunnydale Slayers":     {s: "SUN" for s in range(1, 13)},
    "Ursa Major Grizzlies":  {s: "GRZ" for s in range(3, 13)},
    "Carolina Reapers":      {**{s: "KYM" for s in range(1, 8)}, **{s: "CAR" for s in range(8, 13)}},
    "Kansas City Kitties":   {1: "OAK", 2: "SHH", 3: "SHH", **{s: "BUF" for s in range(4, 9)}, **{s: "KC" for s in range(9, 13)}},
    "Miami Fuego":           {**{s: "BFB" for s in range(1, 9)}, **{s: "MIA" for s in range(9, 13)}},
    "Raccoon City Outbreak":  {**{s: "NO" for s in range(3, 10)}, **{s: "RC" for s in range(10, 13)}},
}

# Build season-aware lookup: (abbrev, season) -> franchise name
_ABBREV_SEASON_TO_FRANCHISE: dict[tuple[str, int], str] = {}
for fname, season_map in FRANCHISE_HISTORY.items():
    for season, abbrev in season_map.items():
        _ABBREV_SEASON_TO_FRANCHISE[(abbrev, season)] = fname

def same_franchise_at_seasons(abbrev_a: str, season_a: int, abbrev_b: str, season_b: int) -> bool:
    """True if abbrev_a in season_a and abbrev_b in season_b are the same MLN franchise."""
    fa = _ABBREV_SEASON_TO_FRANCHISE.get((abbrev_a, season_a))
    fb = _ABBREV_SEASON_TO_FRANCHISE.get((abbrev_b, season_b))
    return fa is not None and fa == fb

# ── Supabase client ───────────────────────────────────────────────────────────

client = create_client(SUPABASE_URL, SUPABASE_KEY)
CHUNK = 500


def _fetch_all(query) -> list[dict]:
    results: list[dict] = []
    offset = 0
    while True:
        batch = query.range(offset, offset + CHUNK - 1).execute().data
        results.extend(batch)
        if len(batch) < CHUNK:
            break
        offset += CHUNK
    return results


# ── fetch data ────────────────────────────────────────────────────────────────

print("Fetching teams...")
teams_rows = _fetch_all(
    client.table("teams").select("abbrev, full_team, team_id, league").order("abbrev")
)
print(f"  {len(teams_rows)} team rows")

print("Fetching players (all seasons)...")
players_rows = _fetch_all(
    client.table("players")
    .select("name, team, season, status, league, primary_pos, secondary_pos, s_id, discord_id")
    .order("name")
    .order("season")
)
print(f"  {len(players_rows)} player-season rows")

# ── build Discord handle -> player name lookup ────────────────────────────────
# Chain: @handle (in transaction text)
#     -> discord_id (via mention metadata in Discord JSON files)
#     -> player name (via players table discord_id field)

print("Building Discord handle -> player name lookup...")

# Step 1: discord_id -> canonical player name (prefer most recent season)
discord_id_to_player: dict[str, str] = {}
for p in sorted(players_rows, key=lambda x: int(x.get("season") or 0)):
    did = str(p.get("discord_id") or "").strip()
    pname = (p.get("name") or "").strip()
    if did and pname:
        discord_id_to_player[did] = pname  # last (highest season) wins

# Step 2: nickname/username -> discord_id, from mentions in Discord JSON files
handle_to_discord_id: dict[str, str] = {}
for jpath in DISCORD_JSON_PATHS:
    try:
        with open(jpath, encoding="utf-8") as f:
            jdata = json.load(f)
        for msg in jdata.get("messages", []):
            for m in msg.get("mentions", []):
                uid = str(m.get("id") or "").strip()
                if not uid:
                    continue
                for key in ("nickname", "name"):
                    handle = (m.get(key) or "").strip()
                    if handle:
                        handle_to_discord_id[handle.lower()] = uid
    except Exception:
        pass

# Step 3: combined lookup handle -> player name
handle_to_player: dict[str, str] = {}
for handle, uid in handle_to_discord_id.items():
    pname = discord_id_to_player.get(uid)
    if pname:
        handle_to_player[handle] = pname

print(f"  {len(discord_id_to_player)} players with discord_id")
print(f"  {len(handle_to_discord_id)} Discord handles mapped to user IDs")
print(f"  {len(handle_to_player)} handles resolved to player names")


def _resolve_handle(raw_player: str) -> str:
    """Try to resolve a Discord @handle or raw handle string to a formal player name.

    Returns the resolved name if found, else returns the original string.
    """
    if not raw_player:
        return raw_player
    # Strip leading @ if present
    key = raw_player.lstrip("@").strip().lower()
    return handle_to_player.get(key, raw_player)


# ── build team mappings ───────────────────────────────────────────────────────

abbrev_to_full: dict[str, str] = {}
abbrev_to_team_id: dict[str, str] = {}
team_id_to_abbrevs: dict[str, list[str]] = defaultdict(list)

for t in teams_rows:
    abbrev = (t.get("abbrev") or "").strip()
    full = (t.get("full_team") or "").strip()
    tid = str(t.get("team_id") or "").strip()
    if abbrev and full:
        abbrev_to_full[abbrev] = full
    if abbrev and tid:
        abbrev_to_team_id[abbrev] = tid
        team_id_to_abbrevs[tid].append(abbrev)

# Pairs of abbreviations that belong to the same franchise (same team_id)
same_franchise_pairs: set[frozenset] = set()
for tid, abbrevs in team_id_to_abbrevs.items():
    unique_abbrevs = list(dict.fromkeys(abbrevs))  # deduplicate preserving order
    if len(unique_abbrevs) > 1:
        for i in range(len(unique_abbrevs)):
            for j in range(i + 1, len(unique_abbrevs)):
                pair = frozenset([unique_abbrevs[i], unique_abbrevs[j]])
                if len(pair) == 2:  # skip if same abbrev on both sides
                    same_franchise_pairs.add(pair)

print(f"\nSame-franchise abbreviation pairs ({len(same_franchise_pairs)}):")
for pair in sorted(same_franchise_pairs, key=lambda p: sorted(p)[0]):
    a, b = sorted(pair)
    print(f"  {a} <-> {b}  (team_id={abbrev_to_team_id.get(a, '?')})")

# ── helper: normalize text for fuzzy comparison ───────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# Valid position abbreviations used in Discord transaction player entries
_POSITIONS = (
    "Pitcher", "Catcher", "Shortstop", "Outfielder", "Infielder", "First Baseman",
    "Second Baseman", "Third Baseman", "Right Fielder", "Left Fielder", "Center Fielder",
    "SP", "RP", "P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
    "DH", "OF", "IF", "UT", "UTIL", "CI", "MI", "CI/IF", "CI/UTIL",
)
_POS_ALTS = "|".join(re.escape(p) for p in sorted(_POSITIONS, key=len, reverse=True))
# Matches one or two position codes separated by / or space, e.g. "1B/UTIL", "C/COF", "SS/UTIL"
_POS_PREFIX = re.compile(
    rf"^(?:{_POS_ALTS})(?:/(?:{_POS_ALTS}))?(?:/(?:{_POS_ALTS}))?\s+",
    re.IGNORECASE,
)


def _strip_pos(name: str) -> str:
    m = _POS_PREFIX.match(name)
    if m:
        return name[m.end():].strip()
    return name


_PAREN_SUFFIX = re.compile(r"\s*\(.*\)\s*$")


def _canonical(name: str) -> str:
    """Strip position prefix and parenthetical suffix for fuzzy comparison."""
    name = _strip_pos(name)
    name = _PAREN_SUFFIX.sub("", name).strip()
    return name


def _resolve_team(raw_name: str) -> tuple[str, float]:
    """Return (abbrev, confidence) for a raw team name from transactions.

    Priority:
    1. Exact match in TEAM_ALIASES
    2. Exact match as an abbrev key in abbrev_to_full
    3. Exact full-name match
    4. Fuzzy match against all known names
    """
    if not raw_name:
        return "", 0.0

    # 1. Manual alias override
    if raw_name in TEAM_ALIASES:
        return TEAM_ALIASES[raw_name], 1.0

    # 2. Direct abbreviation match
    if raw_name.upper() in abbrev_to_full:
        return raw_name.upper(), 1.0

    # 3. Exact full name match
    if raw_name in {v: k for k, v in abbrev_to_full.items()}:
        return {v: k for k, v in abbrev_to_full.items()}[raw_name], 1.0

    # 4. Fuzzy match: compare against abbreviation, full name, and nickname (last word)
    best_abbrev, best_score = "", 0.0
    for abbrev, full in abbrev_to_full.items():
        score = max(_sim(raw_name, abbrev), _sim(raw_name, full))
        parts = full.split()
        if len(parts) > 1:
            score = max(score, _sim(raw_name, parts[-1]))
        if score > best_score:
            best_score = score
            best_abbrev = abbrev

    return best_abbrev, best_score


# ── build per-player season history from Supabase ────────────────────────────

SKIP_TEAM = {"", "ret", "ret?", "x", "DRAFT", "FA", "fa"}

mln_players = [
    p for p in players_rows
    if p.get("season") is not None
    and str(p.get("season", "")).isdigit()
    and (p.get("team") or "").strip() not in SKIP_TEAM
]

player_history: dict[str, list[tuple[int, str]]] = defaultdict(list)
for p in mln_players:
    name = (p.get("name") or "").strip()
    if not name:
        continue
    player_history[name].append((int(p["season"]), p["team"].strip()))

for name in player_history:
    player_history[name].sort(key=lambda x: x[0])

# ── load transactions.csv ─────────────────────────────────────────────────────

print(f"\nLoading {TRANSACTIONS_CSV}...")
txn_df = pd.read_csv(TRANSACTIONS_CSV, dtype=str).fillna("")
print(f"  {len(txn_df)} rows")

MOVE_TYPES = {
    "trade", "sign", "re_sign", "release", "cut", "dfa",
    "receives_from", "extension", "draft", "unretire",
}
txn_moves = txn_df[txn_df["type"].str.lower().isin(MOVE_TYPES)].copy()
print(f"  {len(txn_moves)} movement rows after type filter")

# Pre-resolve team abbrev for every unique team name in transactions
txn_team_names = txn_moves["team"].dropna().unique()
txn_team_to_abbrev: dict[str, str] = {}
txn_team_to_score: dict[str, float] = {}
for tname in txn_team_names:
    if not tname:
        continue
    abbrev, score = _resolve_team(tname)
    txn_team_to_abbrev[tname] = abbrev
    txn_team_to_score[tname] = score

# ── extract season-to-season transitions ──────────────────────────────────────

transitions: list[dict] = []
same_franchise_skipped = 0

for name, history in player_history.items():
    for i in range(1, len(history)):
        s_from, team_from = history[i - 1]
        s_to, team_to = history[i]
        if team_from == team_to:
            continue
        # Skip same-franchise moves - use season-aware lookup first, fall back to pairs
        if same_franchise_at_seasons(team_from, s_from, team_to, s_to):
            same_franchise_skipped += 1
            continue
        if frozenset([team_from, team_to]) in same_franchise_pairs:
            same_franchise_skipped += 1
            continue
        transitions.append({
            "player": name,
            "season_from": s_from,
            "season_to": s_to,
            "team_from": team_from,
            "team_from_name": abbrev_to_full.get(team_from, team_from),
            "team_to": team_to,
            "team_to_name": abbrev_to_full.get(team_to, team_to),
        })

print(f"\n{len(transitions)} genuine team changes (skipped {same_franchise_skipped} same-franchise)")

# ── match each transition to a transaction ────────────────────────────────────

print("Matching transitions against transactions...")
SEASON_WINDOW = 1

results: list[dict] = []

for tr in transitions:
    player = tr["player"]
    s_to = tr["season_to"]
    team_to = tr["team_to"]  # abbrev from Supabase

    s_low, s_high = s_to - SEASON_WINDOW, s_to + SEASON_WINDOW

    season_mask = txn_moves["season"].apply(
        lambda s: s.isdigit() and s_low <= int(s) <= s_high
    )
    candidate_txns = txn_moves[season_mask]

    # Find candidate player matches in this season window.
    # Prefer rows where: (1) player score >= threshold AND (2) team matches.
    # Fall back to best player score alone if no team match found.
    team_match_row = None     # best row with both player + team match
    team_match_score = 0.0
    team_match_player = ""

    any_match_row = None      # best row by player score only
    any_match_score = 0.0
    any_match_player = ""

    for _, row in candidate_txns.iterrows():
        row_player = row.get("player", "") or ""
        if not row_player:
            continue
        # Try to resolve Discord handle to formal player name
        resolved_player = _resolve_handle(row_player)
        # Strip position prefix and parenthetical suffix
        clean_player = _canonical(row_player)
        clean_resolved = _canonical(resolved_player)
        canon_target = _canonical(player)
        score = max(
            _sim(player, row_player),
            _sim(player, clean_player),
            _sim(canon_target, clean_player),
            _sim(player, resolved_player),
            _sim(canon_target, clean_resolved),
        )

        if score > any_match_score:
            any_match_score = score
            any_match_player = row_player
            any_match_row = row

        if score >= PLAYER_FUZZY_THRESHOLD:
            txn_team_raw = row.get("team", "") or ""
            ta = txn_team_to_abbrev.get(txn_team_raw, "")
            is_team_match = (
                (ta == team_to)
                or (frozenset([ta, team_to]) in same_franchise_pairs)
                or same_franchise_at_seasons(ta, s_to, team_to, s_to)
            )
            if is_team_match and score > team_match_score:
                team_match_score = score
                team_match_player = row_player
                team_match_row = row

    # Prefer team+player match; fall back to player-only match
    if team_match_row is not None:
        best_txn_row = team_match_row
        best_player_score = team_match_score
        best_player_name = team_match_player
        team_match = "Y"
    else:
        best_txn_row = any_match_row
        best_player_score = any_match_score
        best_player_name = any_match_player
        team_match = "N"

    txn_abbrev = ""
    if best_txn_row is not None and best_player_score >= PLAYER_FUZZY_THRESHOLD:
        txn_team_raw = best_txn_row.get("team", "") or ""
        txn_abbrev = txn_team_to_abbrev.get(txn_team_raw, "")
        if txn_abbrev == team_to or frozenset([txn_abbrev, team_to]) in same_franchise_pairs:
            team_match = "Y"

    has_txn = "Y" if (best_txn_row is not None and best_player_score >= PLAYER_FUZZY_THRESHOLD) else "N"

    # For unmatched rows: also record the best near-miss so Alex can manually verify
    near_miss_player = any_match_player if has_txn == "N" else ""
    near_miss_score  = round(any_match_score, 3) if has_txn == "N" else ""
    near_miss_date   = any_match_row["date"] if (has_txn == "N" and any_match_row is not None) else ""
    near_miss_raw    = (any_match_row["raw_message"] if (has_txn == "N" and any_match_row is not None) else "")[:120]

    # Discord handle for the transition player (if known)
    player_lower = player.lower()
    player_discord_id = next(
        (did for did, pname in discord_id_to_player.items() if pname == player),
        ""
    )
    player_handle = next(
        (h for h, did in handle_to_discord_id.items() if did == player_discord_id),
        ""
    ) if player_discord_id else ""

    results.append({
        "player": player,
        "player_handle": player_handle,
        "season_from": tr["season_from"],
        "season_to": s_to,
        "team_from": tr["team_from"],
        "team_from_name": tr["team_from_name"],
        "team_to": team_to,
        "team_to_name": tr["team_to_name"],
        "has_transaction": has_txn,
        "player_match_score": round(best_player_score, 3),
        "txn_player_matched": best_player_name,
        "txn_date": best_txn_row["date"] if best_txn_row is not None else "",
        "txn_type": best_txn_row["type"] if best_txn_row is not None else "",
        "txn_team_raw": best_txn_row["team"] if best_txn_row is not None else "",
        "txn_team_abbrev": txn_abbrev,
        "team_abbrev_match": team_match,
        "txn_raw": (best_txn_row["raw_message"] if best_txn_row is not None else "")[:120],
        "near_miss_player": near_miss_player,
        "near_miss_score": near_miss_score,
        "near_miss_date": near_miss_date,
        "near_miss_raw": near_miss_raw,
    })

# ── write output ──────────────────────────────────────────────────────────────

df_out = pd.DataFrame(results)
df_out.to_csv(OUTPUT_CSV, index=False)
print(f"\nWrote {len(df_out)} rows to {OUTPUT_CSV}")

# ── summary ───────────────────────────────────────────────────────────────────

matched   = df_out[df_out["has_transaction"] == "Y"]
unmatched = df_out[df_out["has_transaction"] == "N"]
print(f"\nSummary:")
print(f"  Total transitions:             {len(df_out)}")
print(f"  Matched (transaction found):   {len(matched)}")
print(f"  Unmatched (no transaction):    {len(unmatched)}")

# Break down unmatched by season
print(f"\nUnmatched by season:")
for s in sorted(unmatched["season_to"].unique()):
    n = len(unmatched[unmatched["season_to"] == s])
    print(f"  S{s}: {n}")

print(f"\nUnmatched transitions:")
for _, row in unmatched.sort_values(["season_to", "player"]).iterrows():
    print(
        f"  S{row['season_from']}->S{row['season_to']}  "
        f"{row['player']:25s}  "
        f"{row['team_from']:6s} -> {row['team_to']:6s}  "
        f"({row['team_from_name']} -> {row['team_to_name']})"
    )

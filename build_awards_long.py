"""One-time extraction of the league's "MLN Award History" workbook into the long awards table.

Output rows use the MLN Session Awards sheet's existing columns (season, session, award, player_id, player_name)
plus four new ones (league, team, pos, detail), so they can be pasted under the existing rows of that sheet's Sheet1.
Season-level awards leave `session` blank, which utils.read_session_awards_from_sheet already skips.

    python build_awards_long.py [out_dir]

Writes awards_long.csv (the rows to paste) and awards_review.csv (every row whose name-to-id match was not an
exact same-season roster hit, plus the checks below). The workbook only has names, so ids come from the archive
Rosters tab; the Trophy of Objective Superiority expands to every player on that franchise that season (a play
logged for the team, or on its end-of-season roster).
"""
import csv
import difflib
import glob
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

import pandas as pd

import utils

AWARDS_BOOK_ID = "19pDOM05k4wzp2L0KmMGUZh5E6Pd6WStlcwCKdSq9CMk"
ARCHIVE_SHEET_ID = "1H9ES_TL9nC0x-Q3auM6jtLcb6bII--eu4MtcAPoFcqg"

# Champion franchise by season, as the Awards tab names them (checked against the tab at run time).
CHAMPIONS = {
    1: ("Sunnydale Slayers", "SUN"), 2: ("Gas House Gorillas", "GHG"), 3: ("Sunnydale Slayers", "SUN"),
    4: ("Gas House Gorillas", "GHG"), 5: ("Sunnydale Slayers", "SUN"), 6: ("Sopher McDophers", "SMD"),
    7: ("Gas House Gorillas", "GHG"), 8: ("New Orleans Crawdads", "NO"), 9: ("Ursa Major Grizzlies", "GRZ"),
    10: ("Halifax Highlanders", "HFX"), 11: ("Aruba Sea Serpents", "ASS"), 12: ("Sopher McDophers", "SMD"),
}
# Abbreviations one franchise has used (project_franchise_history), for the franchises that have rebranded.
ALIASES = [{"NO", "RC"}, {"HFX", "VAN"}, {"CIN", "ASS"}, {"MAL", "OGO", "RLY"}, {"LPJ", "BBG", "BBEG"},
           {"KYM", "CAR"}, {"BFB", "MIA"}, {"OAK", "SHH", "BUF", "KC"}, {"REK", "OO"}]

AWARD_SLUGS = [  # (header text contains, slug, league)
    ("Trophy of Objective Superiority", "toos", ""),
    ("GM of the Year", "gm_of_year", None),
    ("MVP", "mvp", None),
    ("Cy Young", "cy_young", None),
    ("Rookie of the Year", "roy", None),
    ("Reliever of the Year", "reliever_of_year", ""),
    ("Most Improved", "most_improved", None),
    ("Person of the Year", "person_of_year", ""),
]
POS_CODES = {
    "Catcher": "C", "First Base": "1B", "Second Base": "2B", "Third Base": "3B", "Shortstop": "SS",
    "Outfield": "OF", "Left Field": "LF", "Center Field": "CF", "Right Field": "RF", "Pitchers": "P", "Pitcher": "P",
    "Starting Pitcher": "SP", "Relief Pitcher": "RP", "Designated Hitter": "DH",
}
FIELDS = ["season", "session", "award", "player_id", "player_name", "league", "team", "pos", "detail"]


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def same_franchise(a: str, b: str) -> bool:
    if a == b:
        return True
    return any(a in g and b in g for g in ALIASES)


def read_tab(tab: str) -> list[list[str]]:
    import urllib.parse
    url = (f"https://docs.google.com/spreadsheets/d/{AWARDS_BOOK_ID}/gviz/tq?tqx=out:csv"
           f"&sheet={urllib.parse.quote(tab)}")
    df = pd.read_csv(url, dtype=str, header=None, keep_default_na=False)
    return df.values.tolist()


class Matcher:
    """Name (+ season, + team hint) -> player_id, from the archive Rosters tab."""

    def __init__(self):
        rows = utils.read_mln_players_from_sheet(ARCHIVE_SHEET_ID, tab="Rosters")
        self.by_name = defaultdict(list)       # norm name -> [(season, team, id)]
        self.by_season = defaultdict(dict)      # season -> {norm name: name}
        self.season_name = {}                   # (season, id) -> that season's roster name
        self.roster = defaultdict(list)         # season -> [(team, id, name)]
        for r in rows:
            if r.get("player_id") is None or not r.get("season"):
                continue
            n = norm(r["name"])
            self.by_name[n].append((r["season"], r["team"] or "", r["player_id"]))
            self.by_season[r["season"]][n] = r["name"]
            self.season_name[(r["season"], r["player_id"])] = r["name"]
            self.roster[r["season"]].append((r["team"] or "", r["player_id"], r["name"]))
        self.canonical = {}                     # id -> a display name
        for rs in self.roster.values():
            for _, pid, name in rs:
                self.canonical[pid] = name

    def match(self, name: str, season: int, team: str = ""):
        """(player_id or None, status). status 'exact' is the only one that needs no review."""
        n = norm(name)
        hits = [(t, i) for s, t, i in self.by_name.get(n, []) if s == season]
        ids = {i for _, i in hits}
        if len(ids) == 1:
            return ids.pop(), "exact"
        if len(ids) > 1:
            by_team = {i for t, i in hits if team and same_franchise(t, team)}
            return (by_team.pop(), "exact_by_team") if len(by_team) == 1 else (None, "ambiguous_in_season")
        other = {i for _, _, i in self.by_name.get(n, [])}
        if len(other) == 1:
            return other.pop(), "other_season"
        if len(other) > 1:
            return None, "ambiguous_across_seasons"
        close = difflib.get_close_matches(n, list(self.by_season[season]), n=2, cutoff=0.88)
        if len(close) == 1:
            ids = {i for s, _, i in self.by_name[close[0]] if s == season}
            if len(ids) == 1:
                return ids.pop(), "fuzzy:" + self.by_season[season][close[0]]
        return None, "unmatched"


def team_plays() -> dict:
    """(season, franchise-agnostic team abbrev) -> {player ids with a logged game}, from the built career files."""
    out = defaultdict(set)
    for f in glob.glob(os.path.join("docs", "data", "career", "p", "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for side in ("bat", "pit"):
            for g in d.get(side, []):
                if g.get("s") and g.get("t"):
                    # Career files built before an id alias landed still carry the stray id.
                    out[(g["s"], g["t"])].add(utils.PLAYER_ID_ALIASES.get(d["id"], d["id"]))
    return out


def split_names(cell: str) -> tuple[list[str], str]:
    """'A (SUN) & B (MAL)' / 'A & B (BFB)' -> (['A', 'B'], team hint per the last abbrev seen); parsed per person."""
    people = []
    for part in cell.split(" & "):
        m = re.match(r"^(.*?)\s*\(([A-Z]{2,4})\)\s*$", part.strip())
        people.append((m.group(1), m.group(2)) if m else (part.strip(), ""))
    # 'A & B (BFB)': the abbrev trails only the last name, so it applies to the whole cell.
    tail = next((t for _, t in reversed(people) if t), "")
    return [(n, t or tail) for n, t in people]


def parse_awards_tab(rows):
    """Yields (season, header text, cell) for every 'Season N' / value pair under a group's header."""
    header = {}
    for row in rows:
        for c in (0, 3, 6, 9, 12):
            if c + 1 >= len(row):
                continue
            a, b = row[c].strip(), row[c + 1].strip()
            m = re.fullmatch(r"Season (\d+)", a)
            if m and b:
                yield int(m.group(1)), header.get(c, ""), b
            elif a and not m and not a.startswith("Season "):
                header[c] = a


def parse_all_stars(rows):
    """Yields (season, league, pos, name, detail) from the All-Stars tab's 'Position: A*, B, C**' lines."""
    season = {0: None, 6: None}
    league = {0: "GL", 6: "LL"}
    for row in rows:
        for c in (0, 6):
            cell = row[c].strip() if c < len(row) else ""
            m = re.fullmatch(r"(GALACTIC|LIBERTY) LEAGUE SEASON (\d+)", cell)
            if m:
                season[c] = int(m.group(2))
                continue
            m = re.match(r"^([A-Za-z ]+?):\s*(.+)$", cell)
            if not m or season[c] is None or m.group(1) not in POS_CODES:
                continue
            # A stray "*." in place of "*," (S9 first base) would glue two names together.
            for tok in re.sub(r"\*\.\s", "*, ", m.group(2)).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                detail = "replacement" if tok.endswith("**") else "starter" if tok.endswith("*") else "reserve"
                yield season[c], league[c], POS_CODES[m.group(1)], tok.rstrip("*").strip(), detail


def main(out_dir="."):
    matcher = Matcher()
    plays = team_plays()
    out, review = [], []

    def add(season, award, pid, name, league="", team="", pos="", detail="", status="exact"):
        out.append({"season": season, "session": "", "award": award, "player_id": pid if pid is not None else "",
                    "player_name": name, "league": league, "team": team, "pos": pos, "detail": detail})
        if status != "exact":
            review.append({"season": season, "award": award, "name": name, "team_hint": team, "status": status,
                           "matched_id": pid if pid is not None else "",
                           "matched_name": matcher.canonical.get(pid, "") if pid is not None else ""})

    # Season awards (Awards tab)
    for season, header, cell in parse_awards_tab(read_tab("Awards")):
        slug = next((s for key, s, lg in AWARD_SLUGS if key in header), None)
        if slug is None:
            review.append({"season": season, "award": header, "name": cell, "status": "unknown_award_header"})
            continue
        league = next(lg for key, s, lg in AWARD_SLUGS if s == slug)
        if league is None:
            league = "GL" if header.startswith("Galactic") else "LL" if header.startswith("Liberty") else ""
        if cell.lower().startswith("not awarded"):
            continue
        if slug == "toos":
            name, abbrev = CHAMPIONS[season]
            if cell != name:
                review.append({"season": season, "award": slug, "name": cell, "status": "champion_name_mismatch"})
                continue
            ids = {}
            for (s, t), pids in plays.items():
                if s == season and same_franchise(t, abbrev):
                    for i in pids:
                        ids.setdefault(i, set()).add("played")
            for t, i, _ in matcher.roster[season]:
                if same_franchise(t, abbrev):
                    ids.setdefault(i, set()).add("roster")
            for i in sorted(ids):
                add(season, slug, i, matcher.season_name.get((season, i)) or matcher.canonical.get(i, ""), "", abbrev, "", "+".join(sorted(ids[i])))
            continue
        for person, team in split_names(cell):
            pid, status = matcher.match(person, season, team)
            add(season, slug, pid, person, league, team, "", "", status)

    # All-Stars tab (text lines, S1 onward)
    stars = set()
    for season, league, pos, name, detail in parse_all_stars(read_tab("All-Stars")):
        pid, status = matcher.match(name, season)
        add(season, "all_star", pid, name, league, "", pos, detail, status)
        stars.add((season, league, norm(name)))

    # Cross-check against the "By Season" tab (S11 onward, one row per selection) for names the text lacks.
    for row in read_tab("By Season"):
        for c in (3, 8):
            if len(row) > c + 3 and row[c + 1].startswith("All-Star") and row[c + 3].strip():
                lg, nm = row[c].strip(), row[c + 3].strip()
                season_cell = next((r[1] for r in read_tab("By Season") if r[0] == "Season"), "")
                if season_cell and (int(season_cell), lg, norm(nm)) not in stars:
                    review.append({"season": season_cell, "award": "all_star", "name": nm, "team_hint": lg,
                                   "status": "in_By_Season_not_in_All-Stars_text", "matched_id": "", "matched_name": ""})

    out.sort(key=lambda r: (int(r["season"]), r["award"], r["league"], str(r["player_name"])))
    with open(os.path.join(out_dir, "awards_long.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    with open(os.path.join(out_dir, "awards_review.csv"), "w", newline="", encoding="utf-8") as f:
        cols = ["season", "award", "name", "team_hint", "status", "matched_id", "matched_name"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(review)
    by_award = defaultdict(int)
    for r in out:
        by_award[r["award"]] += 1
    print(len(out), "rows", dict(by_award))
    by_status = defaultdict(int)
    for r in review:
        by_status[r["status"].split(":")[0]] += 1
    print(len(review), "to review", dict(by_status))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")

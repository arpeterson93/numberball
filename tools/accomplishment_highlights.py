"""The league-wide accomplishment list and what stands out in it.

    python tools/accomplishment_highlights.py

Uses accomplishments_build.build_rows() (game feats + season leader titles), so run after the build or any time
docs/data is current. Writes under tools/accomplishment_highlights_out/:
    accomplishments_all.csv   every instance: player, kind, accomplishment, season, game, detail
    highlights.md             the findings (also printed)
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import accomplishments_build as ab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accomplishment_highlights_out")
LABEL = {
    "multi_hr_2plus": "Multi-HR game", "multi_hr_3plus": "3-HR game", "hits_4plus": "4+ hit game", "cycle": "Cycle",
    "grand_slam": "Grand slam", "rbi_5plus": "5+ RBI game", "sb_3plus": "3+ SB game", "reached_5plus": "Reached base 5+",
    "golden_sombrero": "Golden sombrero", "walk_off": "Walk-off", "k_10plus": "10+ K game", "cg_shutout": "CG shutout",
    "no_hitter": "No-hitter", "perfect_game": "Perfect game", "immaculate_inning": "Immaculate inning",
    "lead_hr": "HR leader", "lead_rbi": "RBI leader", "lead_h": "Hits leader", "lead_sb": "SB leader",
    "lead_k": "K leader", "lead_dpa": "DPA leader", "lead_dbf": "DBF leader", "lead_wpa_bat": "WPA leader (bat)",
    "lead_wpa_pit": "WPA leader (pit)", "lead_er6": "ER6 leader", "lead_whip": "WHIP leader",
    "zero_diff_bat": "0 diff (batter)", "zero_diff_pit": "0 diff allowed (pitcher)", "five_hundred_pit": "500 diff (pitcher)",
    "five_hundred_bat": "500 diff against (batter)",
}
PIT = {"k_10plus", "cg_shutout", "no_hitter", "perfect_game", "immaculate_inning", "lead_k", "lead_dbf",
       "lead_wpa_pit", "lead_er6", "lead_whip", "zero_diff_pit", "five_hundred_pit"}
NEGATIVE = {"golden_sombrero", "five_hundred_bat", "zero_diff_pit"}   # the unlucky side of a diff, like the sombrero


def main():
    rows, names = ab.build_rows()
    nm = lambda i: names.get(i) or str(i)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "accomplishments_all.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["player_id", "player_name", "kind", "accomplishment", "season", "game_code", "detail"])
        for k, i, s, g, d, *_ in sorted(rows, key=lambda r: (r[1], r[2], r[0])):
            w.writerow([i, nm(i), "season" if k.startswith("lead_") else "game", LABEL.get(k, k), s, g, d])

    awards = json.load(open(os.path.join(ab.DATA, "season_awards.json"), encoding="utf-8"))
    big = defaultdict(set)  # pid -> {(season, mvp|cy_young|...)}
    for pid, lst in awards.items():
        for season, aw, *_ in lst:
            big[int(pid)].add((season, aw))

    per = defaultdict(list)
    for k, i, s, g, d, *_ in rows:
        per[i].append((k, s, g, d))
    holders = defaultdict(set)
    for k, i, *_ in rows:
        holders[k].add(i)
    total = Counter(i for _, i, *_ in rows)
    # positive accomplishments only for rankings (a sombrero is a badge of shame, listed but not counted toward "most")
    good = {i: [a for a in lst if a[0] not in NEGATIVE] for i, lst in per.items()}
    kinds = {i: Counter(a[0] for a in lst) for i, lst in per.items()}
    md = []
    pr = md.append
    pr(f"{len(rows)} accomplishments across {len(per)} players "
       f"({sum(1 for r in rows if r[0].startswith('lead_'))} season titles, "
       f"{sum(1 for r in rows if not r[0].startswith('lead_'))} game feats).\n")

    pr("## Most accomplishments (sombreros excluded)\n")
    for i, lst in sorted(good.items(), key=lambda kv: -len(kv[1]))[:12]:
        c = Counter(a[0] for a in lst)
        pr(f"- {nm(i)}: {len(lst)} ({len(c)} kinds): " + ", ".join(f"{LABEL[k]} x{n}" if n > 1 else LABEL[k] for k, n in c.most_common()))

    pr("\n## Most different kinds\n")
    for i, c in sorted(kinds.items(), key=lambda kv: -len([k for k in kv[1] if k not in NEGATIVE]))[:8]:
        ks = [k for k in c if k not in NEGATIVE]
        pr(f"- {nm(i)}: {len(ks)} kinds: " + ", ".join(LABEL[k] for k in ks))

    pr("\n## Rarest: accomplishments with 5 or fewer players ever\n")
    for k in sorted(holders, key=lambda k: len(holders[k])):
        if len(holders[k]) <= 5:
            who = [f"{nm(i)} ({', '.join('S%s' % a[1] for a in per[i] if a[0] == k)})" for i in sorted(holders[k])]
            pr(f"- {LABEL[k]}: " + "; ".join(who))

    pr("\n## Two-way: hitting and pitching accomplishments\n")
    for i, lst in per.items():
        b = {a[0] for a in lst if a[0] not in PIT and a[0] not in NEGATIVE}
        p = {a[0] for a in lst if a[0] in PIT}
        if b and p:
            pr(f"- {nm(i)}: " + ", ".join(LABEL[k] for k in sorted(b | p)))

    pr("\n## Several feats in one game (3+)\n")
    gm = defaultdict(list)
    for k, i, s, g, d, *_ in rows:
        if g and k not in NEGATIVE:
            gm[(i, g)].append(k)
    for (i, g), ks in sorted(gm.items(), key=lambda kv: -len(kv[1])):
        if len(ks) >= 3:
            pr(f"- {nm(i)} (game {g}): " + ", ".join(LABEL[k] for k in ks))

    pr("\n## Season sweeps: most leader titles in one season\n")
    sw = defaultdict(list)
    for k, i, s, g, d, *_ in rows:
        if k.startswith("lead_"):
            sw[(i, s)].append(LABEL[k])
    for (i, s), ks in sorted(sw.items(), key=lambda kv: -len(kv[1]))[:8]:
        pr(f"- {nm(i)}, S{s}: {len(ks)}: " + ", ".join(ks))

    pr("\n## Most accomplishments among players with no MVP / Cy Young\n")
    n = 0
    for i, lst in sorted(good.items(), key=lambda kv: -len(kv[1])):
        if not any(a in ("mvp", "cy_young") for _, a in big.get(i, set())):
            c = Counter(a[0] for a in lst)
            pr(f"- {nm(i)}: {len(lst)}: " + ", ".join(f"{LABEL[k]} x{v}" if v > 1 else LABEL[k] for k, v in c.most_common(5)))
            n += 1
            if n == 8:
                break

    pr("\n## MVP / Cy Young winners with the thinnest lists\n")
    winners = sorted({i for i, s in big.items() if any(a in ("mvp", "cy_young") for _, a in s)}, key=lambda i: len(good.get(i, [])))
    for i in winners[:6]:
        pr(f"- {nm(i)}: {len(good.get(i, []))} accomplishments")

    pr("\n## Biggest unique mixes (set of kinds no one else shares; 12 longest)\n")
    sets = defaultdict(list)
    for i, c in kinds.items():
        sets[frozenset(k for k in c if k not in NEGATIVE)].append(i)
    shown = 0
    for ks, ps in sorted(sets.items(), key=lambda kv: -len(kv[0])):
        if len(ps) == 1 and len(ks) >= 4 and shown < 12:
            shown += 1
            pr(f"- {nm(ps[0])}: " + ", ".join(sorted(LABEL[k] for k in ks)))

    pr("\n## Kinds per player\n")
    dist = Counter(len([k for k in c if k not in NEGATIVE]) for c in kinds.values())
    pr(", ".join(f"{n} kind{'s' if n != 1 else ''}: {dist[n]} players" for n in sorted(dist)))
    pr("")
    pr("Per accomplishment: total / players")
    for k in LABEL:
        pr(f"- {LABEL[k]}: {sum(1 for r in rows if r[0] == k)} / {len(holders[k])}")
    text = "\n".join(md)
    open(os.path.join(OUT, "highlights.md"), "w", encoding="utf-8").write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()

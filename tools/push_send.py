"""Send web-push notifications for what is new in this build compared with the live site.

  python tools/push_send.py [--dry-run] [--subs-file subs.json] [--old-base URL_OR_DIR] [--replay N]

Runs in the Build Key Moments workflow after the data is built and BEFORE it deploys, so the live site still holds the
previous build: "new" means in docs/data/ now and not in <old-base>/ (default https://mlngameday.com/data). No state of
its own. If the old data cannot be read (first run, site down) it sends nothing - a baseline, never a flood.

Events (prefs per subscription, see docs/js/push.js):
  team "m"  plays where the team is batting or fielding: 1 = key moments only, 2 = every play
  team "e"  the team's game just ended
  favs      key moments featuring a player on the subscriber's favorites list (name slug in the Apps Script sheet)
Each subscription gets at most MAX_PER_SUB notifications per run (more are folded into one "N new plays" summary), and
notifications carry a per-game tag so a burst collapses on the device.

Environment (GitHub secrets): PUSH_ENDPOINT (the Apps Script /exec URL), PUSH_SECRET, VAPID_PRIVATE_KEY, VAPID_SUBJECT
(mailto: or https: contact URL for the push services). Without them it just reports and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_DIR = os.path.join(ROOT, "docs", "data")
LIVE_DATA = "https://mlngameday.com/data"
SITE = "https://mlngameday.com/"
MAX_PER_SUB = 4
MAX_NEW_EVENTS = 250     # a rebuild that "creates" far more than this is a data reset, not live play: send nothing


def read(base: str, name: str):
    if base.startswith("http"):
        req = urllib.request.Request(f"{base.rstrip('/')}/{name}", headers={"User-Agent": "mln-gameday-push/1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(os.path.join(base, name), encoding="utf-8") as fh:
        return json.load(fh)


def final_games(meta: dict) -> dict:
    """game_code -> game dict for every game that has ended, across the sessions in meta.json."""
    out = {}
    for games in (meta.get("games") or {}).values():
        for g in games:
            if g.get("is_game_final"):
                out[g["game_code"]] = g
    return out


def link(game_code: str, play_num: int | None = None) -> str:
    q = {"game": game_code}
    if play_num is not None:
        q["play"] = str(play_num % 1000)
    return SITE + "?" + urllib.parse.urlencode(q)


def moment_event(m: dict, labels: dict) -> dict:
    res = labels.get(m["result"], m["result"])
    who = m.get("featured_name") or m.get("batter_name") or ""
    half = "Top" if m["half"] == "top" else "Bot"
    score = f'{m["away_team_abbr"]} {m["away_score"]}, {m["home_team_abbr"]} {m["home_score"]}'
    return {
        "teams": {m["off_team_abbr"], m["def_team_abbr"]}, "kind": "m", "key": bool(m.get("is_key_moment")), "player_ids": {m.get("featured_id"), m.get("batter_id"), m.get("pitcher_id"), m.get("runner_id")} - {None},
        "game": m["game_code"], "payload": {
            "title": f'{m["away_team_abbr"]} @ {m["home_team_abbr"]} - {half} {m["inning"]}',
            "body": f"{who}: {res}. {score}", "url": link(m["game_code"], m["play_num"]), "tag": m["game_code"]}}


def final_event(g: dict) -> dict:
    a, h = g["away_team_abbr"], g["home_team_abbr"]
    win = a if g["away_score"] > g["home_score"] else h
    return {"teams": {a, h}, "kind": "e", "player_ids": set(), "game": g["game_code"], "payload": {
        "title": f"Final: {a} {g['away_score']}, {h} {g['home_score']}",
        "body": f"{win} wins" if g["away_score"] != g["home_score"] else "Game tied",
        "url": link(g["game_code"]), "tag": g["game_code"]}}


def wants(sub: dict, ev: dict) -> bool:
    prefs = sub.get("prefs") or {}
    teams = prefs.get("teams") or {}
    for abbr in ev["teams"]:
        level = int((teams.get(abbr) or {}).get(ev["kind"]) or 0)
        if ev["kind"] == "e" and level:
            return True
        if ev["kind"] == "m" and (level >= 2 or (level == 1 and ev["key"])):
            return True
    return bool(ev["kind"] == "m" and ev["key"] and prefs.get("favs") and ev["player_ids"] & {int(x) for x in sub.get("player_ids") or []})


def messages_for(sub: dict, events: list[dict]) -> list[dict]:
    mine = [e["payload"] for e in events if wants(sub, e)]
    if len(mine) <= MAX_PER_SUB:
        return mine
    # one summary for the older ones, then the latest plays (events are in time order)
    return [{"title": f"{len(mine) - MAX_PER_SUB + 1} earlier plays", "body": "Open MLN Gameday to catch up", "url": SITE, "tag": "more"}] + mine[-(MAX_PER_SUB - 1):]


def api(endpoint: str, body: dict | None = None, params: dict | None = None) -> dict:
    if body is None:
        req = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(params or {}))
    else:
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "text/plain;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print who would get what, send nothing")
    ap.add_argument("--subs-file", help="read subscriptions from this JSON file ({subs: [...]}) instead of the Apps Script endpoint")
    ap.add_argument("--old-base", default=LIVE_DATA, help="URL or directory holding the previous build's key_moments.json and meta.json")
    ap.add_argument("--replay", type=int, metavar="N", help="testing: pretend the latest N plays and the latest finished game are new (no previous build needed)")
    args = ap.parse_args()

    new_meta = read(NEW_DIR, "meta.json")
    sessions = (new_meta.get("sessions") or [])[:2]          # the live session and the one before it
    new_plays = {m["moment_id"]: m for sess in sessions for m in read(NEW_DIR, f"plays_{sess:02d}.json") if m.get("result") is not None}
    if args.replay:
        newest = sorted(new_plays.values(), key=lambda m: m["timestamp"])[-args.replay:]
        seen = set(new_plays) - {m["moment_id"] for m in newest}
        old_meta = json.loads(json.dumps(new_meta))
        done = final_games(old_meta)
        if done:
            done[sorted(done)[-1]]["is_game_final"] = False   # game codes are SSNNGG, so the biggest code is the latest game
    else:
        try:
            old_meta = read(args.old_base, "meta.json")
            seen = set()
            for sess in sessions:
                try:
                    seen |= {m["moment_id"] for m in read(args.old_base, f"plays_{sess:02d}.json")}
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:      # a session the live site does not have yet is empty; any other failure is "no baseline"
                        raise
        except Exception as exc:  # noqa: BLE001 - any failure here means "no baseline"
            print(f"push: no previous build to compare with ({exc}); nothing sent")
            return 0

    labels = new_meta.get("result_short") or new_meta.get("result_labels") or {}
    old_final = final_games(old_meta)
    events = [moment_event(m, labels) for m in sorted((m for mid, m in new_plays.items() if mid not in seen), key=lambda m: (m["timestamp"], m["play_num"]))]
    events += [final_event(g) for code, g in final_games(new_meta).items() if code not in old_final]
    print(f"push: {len(events)} new events")
    if not events:
        return 0
    if len(events) > MAX_NEW_EVENTS:
        print("push: far too many at once (data reset?); nothing sent")
        return 0

    endpoint, secret = os.environ.get("PUSH_ENDPOINT", ""), os.environ.get("PUSH_SECRET", "")
    if args.subs_file:
        subs = json.load(open(args.subs_file, encoding="utf-8"))["subs"]
    elif endpoint and secret:
        res = api(endpoint, params={"action": "push_list", "secret": secret})
        if "error" in res:
            print("push: could not list subscriptions:", res["error"])
            return 0
        subs = res["subs"]
    else:
        print("push: PUSH_ENDPOINT / PUSH_SECRET not set; nothing sent")
        return 0

    plan = [(s, messages_for(s, events)) for s in subs]
    plan = [(s, msgs) for s, msgs in plan if msgs]
    print(f"push: {sum(len(m) for _, m in plan)} notifications to {len(plan)} of {len(subs)} subscriptions")
    if args.dry_run:
        for s, msgs in plan:
            for m in msgs:
                print(f"  -> {s.get('key')}: {m['title']} | {m['body']} | {m['url']}")
        return 0

    key, subject = os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_SUBJECT", "")
    if not key or not subject:
        print("push: VAPID_PRIVATE_KEY / VAPID_SUBJECT not set; nothing sent")
        return 0
    from pywebpush import WebPushException, webpush  # imported late so --dry-run needs nothing installed

    dead, ok = [], []
    for s, msgs in plan:
        for m in msgs:
            try:
                webpush(subscription_info=s["subscription"], data=json.dumps(m), vapid_private_key=key, vapid_claims={"sub": subject}, ttl=3600)
                ok.append(s["endpoint"])
            except WebPushException as exc:
                code = getattr(exc.response, "status_code", None)
                print(f"push: {s.get('key')}: {code} {exc}"[:200])
                if code in (404, 410):
                    dead.append(s["endpoint"])
                    break
    if endpoint and secret and not args.subs_file:
        if dead:
            api(endpoint, {"action": "push_prune", "secret": secret, "endpoints": sorted(set(dead))})
        if ok:
            api(endpoint, {"action": "push_ok", "secret": secret, "endpoints": sorted(set(ok))})
    print(f"push: sent {len(ok)}, pruned {len(set(dead))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

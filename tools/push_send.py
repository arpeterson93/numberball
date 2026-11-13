"""Send web-push notifications for what is new in this build compared with the live site.

  python tools/push_send.py [--dry-run] [--subs-file subs.json] [--old-base URL_OR_DIR] [--replay N]

Runs in the Build Key Moments workflow in two steps, so an alert is only sent once its play is really on the site:
  1. before the deploy:  python tools/push_send.py --snapshot DIR   saves the still-live previous build (the baseline)
  2. after the deploy:   python tools/push_send.py --old-base DIR --new-base https://mlngameday.com/data --expect-state JSON
     waits until the live site serves the new build (its build_hash.json matches), then sends what is new.
"new" means in the new data and not in the baseline. With no flags it compares docs/data/ with the live site, which is how
to try it locally. No state of its own. If the baseline cannot be read (first run, site down) it sends nothing - never a flood.

Events (prefs per subscription, see docs/js/push.js):
  team "m"  plays where the team is batting or fielding: 1 = key moments only, 2 = every play
  team "e"  the team's game just ended
  favs      key moments featuring a player on the subscriber's favorites list (name slug in the Apps Script sheet)
Each subscription gets at most MAX_PER_SUB notifications per run (the rest are folded into one "N more new plays" summary), and
every notification stands alone (no tag), so a later play never replaces an earlier one on the device. Android and desktop
Chrome show the batting team's logo (the winner's, for a game end); iOS always shows the app icon and ignores it.

Environment (GitHub secrets): PUSH_ENDPOINT (the Apps Script /exec URL), PUSH_SECRET, VAPID_PRIVATE_KEY, VAPID_SUBJECT
(mailto: or https: contact URL for the push services). Without them it just reports and exits 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_DIR = os.path.join(ROOT, "docs", "data")
LIVE_DATA = "https://mlngameday.com/data"
SITE = "https://mlngameday.com/"
MAX_PER_SUB = 5
SEND_GAP = 1.0           # seconds between rounds of sends, so one device never gets a burst of pushes in the same instant
MAX_NEW_EVENTS = 250     # a rebuild that "creates" far more than this is a data reset, not live play: send nothing


def read(base: str, name: str):
    if base.startswith("http"):
        # a throwaway query keeps the CDN from handing back a copy cached before the deploy; the site is flaky now and then
        # (a 503 cost a run its baseline once), so a few tries before giving up. A 404 is an answer, not a failure.
        for attempt in range(4):
            req = urllib.request.Request(f"{base.rstrip('/')}/{name}?cb={int(time.time())}", headers={"User-Agent": "mln-gameday-push/1"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404 or attempt == 3:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(3)
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


def scoring_line(m: dict) -> str:
    """'Costanza scores' / 'LaMarr, Harrison score' - the same wording as the plays rows on the home page."""
    names = m.get("scoring_names") or []
    if not names:
        return ""
    return f"{names[0]} scores" if len(names) == 1 else f"{', '.join(names)} score"


def diamond(m: dict) -> str:
    """The base state after the play on one row, 3rd 2nd 1st, then the outs:  ◇ ◆ ◇   1 out
    (obc_after is already in that order, 3rd-2nd-1st: '001' = runner on 1st, '100' = runner on 3rd)"""
    obc = m.get("obc_after") or "000"
    dots = " ".join("\u25c6" if obc[i] == "1" else "\u25c7" for i in (0, 1, 2))
    outs = m.get("outs_after")
    return dots if outs is None else f"{dots}   {outs} out" + ("" if outs == 1 else "s")


def icon_for(logos: dict | None, abbr: str | None) -> dict:
    """{"icon": absolute logo URL} for the team, or nothing (the service worker then uses the app icon)."""
    url = (logos or {}).get(abbr or "")
    return {"icon": url} if url else {}


def moment_event(m: dict, logos: dict | None = None) -> dict:
    """Title  HFX 1 - GHG 0 ▲ 1   (▲ top of the inning, ▼ bottom)
    Body   Brody Page HR / vs Pitcher Name (a steal: Runner SB2 / vs Catcher) / Costanza scores (only when someone scored) / the base diamond with the outs"""
    if m["result"][:2] in ("SB", "CS"):    # a steal: runner and result, against the catcher (the featured player when he is the one fielding)
        top = f'{m.get("runner_name") or m.get("batter_name") or ""} {m["result"]}'
        catcher = m.get("featured_name") if m.get("featured_side") == "fielding" else m.get("counterpart_name")
        vs = f"vs {catcher}" if catcher else ""
    else:
        top = f'{m.get("batter_name") or m.get("featured_name") or ""} {m["result"]}'
        vs = f'vs {m["pitcher_name"]}' if m.get("pitcher_name") else ""
    tri = "▲" if m["half"] == "top" else "▼"
    lines = [top, vs, scoring_line(m), diamond(m)]
    return {
        "teams": {m["off_team_abbr"], m["def_team_abbr"]}, "kind": "m", "key": bool(m.get("is_key_moment")), "player_ids": {m.get("featured_id"), m.get("batter_id"), m.get("pitcher_id"), m.get("runner_id")} - {None},
        "game": m["game_code"], "payload": {
            "title": f'{m["away_team_abbr"]} {m["away_score"]} - {m["home_team_abbr"]} {m["home_score"]} {tri} {m["inning"]}',
            "body": "\n".join(x for x in lines if x), "url": link(m["game_code"], m["play_num"]), **icon_for(logos, m["off_team_abbr"])}}


def biggest_play_text(plays: list[dict], labels: dict) -> str:
    """The game's biggest swing by win probability, worded like the end of the game slideshow without the 'Biggest play:' lead
    (app.js buildGameReplaySlides / sceneRecapHtml): 'Brody Page · Home Run · WPA +10.3%'."""
    top = None
    for p in plays:
        if p.get("featured_wpa") is not None and (top is None or abs(p["featured_wpa"]) > abs(top["featured_wpa"])):
            top = p
    if top is None:
        return ""
    delta = top["featured_wpa"] * 100
    return f'{top.get("featured_name") or ""} · {labels.get(top["result"], top["result"])} · WPA {"+" if delta >= 0 else ""}{delta:.1f}%'


def final_event(g: dict, plays: list[dict], labels: dict, logos: dict | None = None) -> dict:
    a, h = g["away_team_abbr"], g["home_team_abbr"]
    return {"teams": {a, h}, "kind": "e", "player_ids": set(), "game": g["game_code"], "payload": {
        "title": f"Final: {a} {g['away_score']} - {h} {g['home_score']}",
        "body": biggest_play_text(plays, labels),
        "url": link(g["game_code"]), **icon_for(logos, a if g["away_score"] > g["home_score"] else h if g["home_score"] > g["away_score"] else None)}}


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
    # the first MAX_PER_SUB in time order, then one summary for the rest
    return mine[:MAX_PER_SUB] + [{"title": f"{len(mine) - MAX_PER_SUB} more new plays", "body": "Open MLN Gameday to catch up", "url": SITE}]


def api(endpoint: str, body: dict | None = None, params: dict | None = None) -> dict:
    if body is None:
        req = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(params or {}))
    else:
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "text/plain;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot(dest: str) -> int:
    """Save the live site's meta.json and the plays files of the two newest sessions into dest, for the post-deploy run to
    diff against. meta.json goes in last, so a half-written baseline never looks complete; with no meta.json there is no baseline."""
    new_meta = read(NEW_DIR, "meta.json")
    os.makedirs(dest, exist_ok=True)
    try:
        old_meta = read(LIVE_DATA, "meta.json")
        for sess in (new_meta.get("sessions") or [])[:2]:
            try:
                rows = read(LIVE_DATA, f"plays_{sess:02d}.json")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                continue
            with open(os.path.join(dest, f"plays_{sess:02d}.json"), "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
        with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(old_meta, fh)
    except Exception as exc:  # noqa: BLE001
        print(f"push: could not save the live data as a baseline ({exc}); the alerts for this build will be skipped")
        return 0
    print(f"push: baseline saved in {dest}")
    return 0


def wait_for_deploy(base: str, state_json: str, tries: int = 24, gap: float = 10.0) -> bool:
    """Poll the live site's build_hash.json until it carries the hash this build wrote (the deploy has gone out)."""
    want = json.loads(state_json).get("hash")
    for i in range(tries):
        try:
            if read(base, "build_hash.json").get("hash") == want:
                print(f"push: the site is serving the new build (after {i * gap:.0f}s)")
                return True
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(gap)
    return False


def who(sub: dict) -> str:
    """A short anonymous id and the push service, enough to trace one device without naming anyone in the log."""
    host = urllib.parse.urlparse(sub["endpoint"]).netloc.split(".")[-2:]
    return f'{hashlib.sha1(sub["endpoint"].encode()).hexdigest()[:6]}@{".".join(host)}'


def send_test(key_name: str, icon_abbr: str = "") -> int:
    """One labelled test alert to each subscription saved under `key_name`, naming the device id in the body so the person can
    see which of their phones is which (the same id the phones' delivery receipts and this log carry)."""
    endpoint, secret = os.environ.get("PUSH_ENDPOINT", ""), os.environ.get("PUSH_SECRET", "")
    vkey, subject = os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_SUBJECT", "")
    if not (endpoint and secret and vkey and subject):
        print("push test: PUSH_ENDPOINT / PUSH_SECRET / VAPID_PRIVATE_KEY / VAPID_SUBJECT not all set")
        return 1
    res = api(endpoint, params={"action": "push_list", "secret": secret})
    if "error" in res:
        print("push test: could not list subscriptions:", res["error"])
        return 1
    mine = [s for s in res["subs"] if s.get("key") == key_name]
    print(f"push test: {len(mine)} subscriptions under that name")
    from pywebpush import WebPushException, webpush
    for s in mine:
        dev = hashlib.sha1(s["endpoint"].encode()).hexdigest()[:6]
        msg = {"title": "MLN Gameday test", "body": f"Device {dev} \u00b7 this is only a test alert" + (f" with the {icon_abbr} icon" if icon_abbr else ""), "url": SITE}
        if icon_abbr:   # the same icon URL the real play alerts carry, to see whether a remote icon changes what iOS does
            msg["icon"] = f"{SITE}data/push_icons/{icon_abbr}.png"
        try:
            resp = webpush(subscription_info=s["subscription"], data=json.dumps(msg), vapid_private_key=vkey, vapid_claims={"sub": subject}, ttl=3600,
                           headers={"Urgency": "high"})
            print(f"push test: {who(s)} {getattr(resp, 'status_code', '?')}")
        except WebPushException as exc:
            print(f"push test: {who(s)} FAILED {getattr(exc.response, 'status_code', None)} {exc}"[:240])
        time.sleep(SEND_GAP)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # the triangles in titles; a Windows console is cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print who would get what, send nothing")
    ap.add_argument("--subs-file", help="read subscriptions from this JSON file ({subs: [...]}) instead of the Apps Script endpoint")
    ap.add_argument("--old-base", default=LIVE_DATA, help="URL or directory holding the previous build's key_moments.json and meta.json")
    ap.add_argument("--new-base", help="URL or directory holding the NEW build's data (default: this checkout's docs/data); the workflow points it at the live site after the deploy")
    ap.add_argument("--expect-state", help="build state JSON from the gate; with an http --new-base, wait until the site's build_hash.json carries the same hash")
    ap.add_argument("--snapshot", metavar="DIR", help="save the live site's current meta.json and plays files into DIR (the baseline for a later run) and exit")
    ap.add_argument("--test", metavar="KEY", help="send one labelled test alert to every subscription saved under this name (the key column) and exit")
    ap.add_argument("--test-icon", metavar="ABBR", default="", help="with --test: give the alert that team's logo as its icon, as the real play alerts do")
    ap.add_argument("--replay", type=int, metavar="N", help="testing: pretend the latest N plays and the latest finished game are new (no previous build needed)")
    args = ap.parse_args()

    if args.test:
        return send_test(args.test, args.test_icon.upper())
    if args.snapshot:
        return snapshot(args.snapshot)
    new_base = args.new_base or NEW_DIR
    if args.expect_state and new_base.startswith("http") and not wait_for_deploy(new_base, args.expect_state):
        print("push: the site never showed the new build; nothing sent")
        return 0
    new_meta = read(new_base, "meta.json")
    sessions = (new_meta.get("sessions") or [])[:2]          # the live session and the one before it
    new_plays = {m["moment_id"]: m for sess in sessions for m in read(new_base, f"plays_{sess:02d}.json") if m.get("result") is not None}
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
                except FileNotFoundError:    # the same, for a saved baseline (the snapshot skips sessions the site did not have)
                    pass
        except Exception as exc:  # noqa: BLE001 - any failure here means "no baseline"
            print(f"push: no previous build to compare with ({exc}); nothing sent")
            return 0

    logos = {abbr: f"{SITE}data/push_icons/{abbr}.png" for abbr, t in (new_meta.get("teams") or {}).items() if t.get("logo_url")}   # tools/push_icons.py
    labels = new_meta.get("result_labels") or {}   # full names, as the slideshow recap uses
    old_final = final_games(old_meta)
    by_game: dict[str, list[dict]] = {}
    for m in new_plays.values():
        by_game.setdefault(m["game_code"], []).append(m)
    events = [moment_event(m, logos) for m in sorted((m for mid, m in new_plays.items() if mid not in seen), key=lambda m: (m["timestamp"], m["play_num"]))]
    events += [final_event(g, by_game.get(code, []), labels, logos) for code, g in final_games(new_meta).items() if code not in old_final]
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
                print(f"  -> {s.get('key')}: {m['title']} | {m['body']!r} | {m['url']}")
        return 0

    key, subject = os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_SUBJECT", "")
    if not key or not subject:
        print("push: VAPID_PRIVATE_KEY / VAPID_SUBJECT not set; nothing sent")
        return 0
    from pywebpush import WebPushException, webpush  # imported late so --dry-run needs nothing installed

    dead, ok, failed = [], [], set()
    rounds = max(len(msgs) for _, msgs in plan)
    for i in range(rounds):
        if i:
            time.sleep(SEND_GAP)
        for s, msgs in plan:
            if i >= len(msgs) or s["endpoint"] in failed:
                continue
            m = msgs[i]
            try:
                resp = webpush(subscription_info=s["subscription"], data=json.dumps(m), vapid_private_key=key, vapid_claims={"sub": subject}, ttl=3600,
                              headers={"Urgency": "high"})   # Apple maps this to immediate delivery; the default can be held back on an idle phone
                ok.append(s["endpoint"])
                print(f"push: {who(s)} {i + 1}/{len(msgs)} {getattr(resp, 'status_code', '?')} {m['title']}")
            except WebPushException as exc:
                code = getattr(exc.response, "status_code", None)
                print(f"push: {who(s)} {i + 1}/{len(msgs)} FAILED {code} {m['title']} {exc}"[:240])
                if code in (404, 410):
                    dead.append(s["endpoint"])
                failed.add(s["endpoint"])
    if endpoint and secret and not args.subs_file:
        if dead:
            api(endpoint, {"action": "push_prune", "secret": secret, "endpoints": sorted(set(dead))})
        if ok:
            api(endpoint, {"action": "push_ok", "secret": secret, "endpoints": sorted(set(ok))})
    print(f"push: sent {len(ok)}, pruned {len(set(dead))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

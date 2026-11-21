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
  favs      plays featuring a player on the subscriber's favorites list (name slug in the Apps Script sheet): 1 = key moments only, 2 = every play
  pitching change  a team replaced its pitcher mid-game (a "Now Batting" placeholder already lists the new pitcher, or a play shows a different
            pitcher than the team's previous one): sent for followed teams (either side) and favorite pitchers, unless prefs.pc is 0; see pitching_changes
  feats     a new game feat or season title (accomplishments.json): 1 = by players on my teams or my favorites, 2 = anyone's; a tap opens
            that player's Awards tab and replays the feat (see feat_event)
"Key moment" is the build's is_key_moment unless the subscription has prefs.tags (its own list of tag slugs): then a play counts
when it carries any of them.
prefs.fmt is the subscriber's own notification layout for plays (tools/push_format.py), prefs.fmtk the one for their key moments (absent = the
Spotlight preset); a play without a layout uses the standard one below. Feat alerts have one fixed format (feat_event).
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
import re
import sys
import time
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_format  # noqa: E402  custom notification layouts (prefs.fmt)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_DIR = os.path.join(ROOT, "docs", "data")
LIVE_DATA = "https://mlngameday.com/data"
SITE = "https://mlngameday.com/"
MAX_PER_SUB = 5
SEND_GAP = 1.0           # seconds between rounds of sends, so one device never gets a burst of pushes in the same instant
LABELS: dict = {}        # meta.json result_labels / result_short, set by main(), for the custom layouts
SHORT: dict = {}
MAX_NEW_FEATS = 40       # more new feats than this at once is a new feat type backfilling its history, not live play: send no feat alerts
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
    return dots if outs is None else f"{dots} · {outs} out" + ("" if outs == 1 else "s")


def icon_for(logos: dict | None, abbr: str | None) -> dict:
    """{"icon": absolute logo URL} for the team, or nothing (the service worker then uses the app icon)."""
    url = (logos or {}).get(abbr or "")
    return {"icon": url} if url else {}


def moment_event(m: dict, logos: dict | None = None) -> dict:
    """Title  HFX 1 - GHG 0 ▲ 1   (▲ top of the inning, ▼ bottom)
    Body   Brody Page / vs Pitcher Name (a steal: Runner / vs Catcher) / Costanza scores (only when someone scored) / HR · the base diamond with the outs
    (the same as the Classic layout in push_format.py)"""
    if m["result"][:2] in ("SB", "CS"):    # a steal: runner and result, against the catcher (the featured player when he is the one fielding)
        top = m.get("runner_name") or m.get("batter_name") or ""
        catcher = m.get("featured_name") if m.get("featured_side") == "fielding" else m.get("counterpart_name")
        vs = f"vs {catcher}" if catcher else ""
    else:
        top = m.get("batter_name") or m.get("featured_name") or ""
        vs = f'vs {m["pitcher_name"]}' if m.get("pitcher_name") else ""
    tri = "▲" if m["half"] == "top" else "▼"
    res = SHORT.get(m["result"], m["result"])
    lines = [top, vs, scoring_line(m), f"{res} · {diamond(m)}"]
    return {
        "teams": {m["off_team_abbr"], m["def_team_abbr"]}, "kind": "m", "key": bool(m.get("is_key_moment")), "tags": set(m.get("tags") or []), "play": m, "player_ids": {m.get("featured_id"), m.get("batter_id"), m.get("pitcher_id"), m.get("runner_id")} - {None},
        "game": m["game_code"], "payload": {
            "title": f'{m["away_team_abbr"]} {m["away_score"]} - {m["home_team_abbr"]} {m["home_score"]} {tri} {m["inning"]}',
            "body": "\n".join(x for x in lines if x), "url": link(m["game_code"], m["play_num"]), **icon_for(logos, m["off_team_abbr"])}}


def pitching_changes(plays: list[dict]) -> dict:
    """key -> pitching change, for every team that replaced its pitcher in a game. Two sources feed the one key (game:team:new pitcher id), so
    a change seen first on the lineup and again on the play that follows is the same change and alerts once:
      - a "Now Batting" placeholder carrying `pitching_change` (the Lineups tab already lists the new pitcher), and
      - the catch-all: a real play whose pitcher differs from the previous play of the same defending team in the same game (the lineup
        change was missed, or the plate appearance came in with it). A team's first pitcher is the starter, never a change.
    The sender has no state of its own, so "new" is a key the previous build did not have (the same function runs over both builds)."""
    out: dict = {}
    by_game: dict[str, list[dict]] = {}
    for m in plays:
        by_game.setdefault(m.get("game_code"), []).append(m)
    for code, rows in by_game.items():
        last: dict = {}   # defending team -> (pitcher id, name) of the pitcher who last threw for it
        for m in sorted((r for r in rows if r.get("result") is not None), key=lambda r: r["play_num"]):
            team, pid = m.get("def_team_abbr"), m.get("pitcher_id")
            if not team or not pid:
                continue
            prev = last.get(team)
            if prev and prev[0] != pid:
                out[f"{code}:{team}:{pid}"] = {"game": code, "team": team, "from_id": prev[0], "from_name": prev[1], "to_id": pid, "to_name": m.get("pitcher_name") or "", "play": m}
            last[team] = (pid, m.get("pitcher_name") or "")
        for m in rows:
            pc = m.get("pitching_change")
            if m.get("is_on_deck") and pc and pc.get("to_id"):
                out.setdefault(f"{code}:{pc['team']}:{pc['to_id']}", {"game": code, "team": pc["team"], "from_id": pc.get("from_id"), "from_name": pc.get("from_name") or "",
                                                                       "to_id": pc["to_id"], "to_name": pc.get("to_name") or "", "play": m})
    return out


def pitching_event(c: dict, logos: dict | None = None) -> dict:
    """Title  Pitching change · POR      Body  Old Pitcher → New Pitcher / HFX 1 - GHG 0 ▲ 1 / the base diamond with the outs, as the plate appearance is about to start
    (a fixed format, like the feat alerts; the layout designer does not cover it)"""
    m = c["play"]
    away, home = m.get("away_score") or 0, m.get("home_score") or 0
    if m.get("result") is not None:   # a real play: its scores are after it, the alert is for the moment before
        runs = m.get("runs") or 0
        if m.get("batting_is_home"):
            home -= runs
        else:
            away -= runs
    tri = "▲" if m["half"] == "top" else "▼"
    state = {"obc_after": m.get("obc_before"), "outs_after": m.get("outs_before")}
    lines = [f'{c["from_name"]} → {c["to_name"]}' if c["from_name"] else c["to_name"],
             f'{m["away_team_abbr"]} {away} - {m["home_team_abbr"]} {home} {tri} {m["inning"]}', diamond(state)]
    teams = {c["team"], m.get("off_team_abbr"), m.get("def_team_abbr")} - {None, ""}
    return {"teams": teams, "kind": "p", "key": False, "tags": set(), "play": m, "player_ids": {c["to_id"], c["from_id"]} - {None},
            "game": c["game"], "sort": (m.get("timestamp") or "", m.get("play_num") or 0), "payload": {
        "title": f'Pitching change · {c["team"]}', "body": "\n".join(x for x in lines if x), "url": link(c["game"], m.get("play_num")), **icon_for(logos, c["team"])}}


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


def feat_titles() -> dict:
    """feat key -> its title, read from the AW_ACC list in docs/js/career.js (the one place the Awards tab's wording is kept)."""
    try:
        src = open(os.path.join(ROOT, "docs", "js", "career.js"), encoding="utf-8").read()
    except OSError:
        return {}
    return dict(re.findall(r'^\s*\["([a-z0-9_]+)", "([^"]+)", "(?:season|game|play)"', src, re.M))


def feat_event(row: list, names: dict, titles: dict, logos: dict | None = None) -> dict:
    """One accomplishments.json row [key, player id, season, game, detail, opponent, play numbers, team] as an alert:
    Title  Brody Page: No-hitter      Body  6.0 IP, 0 H, 7 K / HMH vs RLY   (a season-long feat, HR Leader: 6 HR / SUN · S12)
    The link opens the player's Awards tab and replays the feat."""
    key, pid, season, game, detail, opp, _pns, team = (list(row) + [""] * 8)[:8]
    where = f"{team} vs {opp}" if team and opp else team
    return {"teams": {team} if team else set(), "kind": "f", "player_ids": {int(pid)}, "game": game or "", "payload": {
        "title": f"{names.get(str(pid)) or 'A player'}: {titles.get(key) or key.replace('_', ' ').title()}",
        "body": "\n".join(x for x in (detail, where if game else f"{where} · S{season}" if where else f"S{season}") if x),   # a game or moment feat is dated by when it happens, so only a season-long one names its season
        "url": SITE + "?" + urllib.parse.urlencode({"player": pid, "side": "awd", "feat": f"{key}:{season}:{game or ''}"}, safe=":"), **icon_for(logos, team)}}


def new_feats(old: list, new: list) -> list:
    """The rows in `new` that `old` does not have (a player can earn the same feat twice in a game, so counted, not just compared),
    oldest season and game first."""
    ident = lambda r: (r[0], r[1], r[2], r[3], r[4])  # noqa: E731
    left = Counter(ident(r) for r in old)
    out = []
    for r in new:
        if left[ident(r)] > 0:
            left[ident(r)] -= 1
        else:
            out.append(r)
    return sorted(out, key=lambda r: (r[2], str(r[3])))


def is_key(prefs: dict, ev: dict) -> bool:
    """Whether this play is a key moment for this subscriber. prefs["tags"] (a list of tag slugs) is their own definition: any of
    those tags on the play counts. Without it, the build's is_key_moment applies. The public feed is never affected."""
    tags = prefs.get("tags")
    if isinstance(tags, list):
        return bool(ev["tags"] & set(tags))
    return ev["key"]


def wants(sub: dict, ev: dict) -> bool:
    prefs = sub.get("prefs") or {}
    teams = prefs.get("teams") or {}
    if ev["kind"] == "f":
        level = int(prefs.get("feats") or 0)
        if level >= 2:
            return True
        if level == 1:
            followed = any(int((teams.get(a) or {}).get("m") or 0) or int((teams.get(a) or {}).get("e") or 0) for a in ev["teams"])
            return followed or (int(prefs.get("favs") or 0) > 0 and bool(ev["player_ids"] & {int(x) for x in sub.get("player_ids") or []}))
        return False
    if ev["kind"] == "p":   # on unless the subscriber unticked it (prefs.pc == 0); for a followed team (either side of the game) or a favorite pitcher
        if str(prefs.get("pc", 1)) == "0":
            return False
        if any(int((teams.get(a) or {}).get("m") or 0) >= 1 for a in ev["teams"]):
            return True
        return int(prefs.get("favs") or 0) >= 1 and bool(ev["player_ids"] & {int(x) for x in sub.get("player_ids") or []})
    key = ev["kind"] == "m" and is_key(prefs, ev)
    for abbr in ev["teams"]:
        level = int((teams.get(abbr) or {}).get(ev["kind"]) or 0)
        if ev["kind"] == "e" and level:
            return True
        if ev["kind"] == "m" and (level >= 2 or (level == 1 and key)):
            return True
    favs = int(prefs.get("favs") or 0)   # 0 off, 1 key moments, 2 every play (older saves only ever hold 0 or 1)
    return bool(ev["kind"] == "m" and (favs >= 2 or (favs == 1 and key)) and ev["player_ids"] & {int(x) for x in sub.get("player_ids") or []})


def wire(sub: dict, msg: dict) -> dict:
    """What actually goes over the wire for this subscription. Apple subscriptions get a Declarative Web Push message (WebKit, iOS and
    iPadOS 18.4+): the system builds the notification from it and, on a tap, opens `navigate` itself, with no service worker click
    event in between (which iOS was not always delivering to a closed app). docs/sw.js shows the same alert the old way on an iOS that
    predates it. PUSH_DECLARATIVE=0 turns this off. Everything else (FCM / Android) keeps the plain message."""
    if os.environ.get("PUSH_DECLARATIVE") == "0" or "web.push.apple.com" not in sub["endpoint"]:
        return msg
    return {"web_push": 8030, "notification": {"title": msg["title"], "body": msg.get("body", ""), "navigate": msg.get("url") or SITE}}


def payload_for(sub: dict, ev: dict) -> dict:
    """The event's message, in the subscriber's own layout (prefs.fmt) when they designed one. Same link and icon either way."""
    if ev["kind"] != "m":
        return ev["payload"]
    prefs = sub.get("prefs") or {}
    if is_key(prefs, ev):   # key moments: their own layout, Spotlight until the subscriber designs another
        fmt = prefs.get("fmtk") if isinstance(prefs.get("fmtk"), dict) else push_format.SPOTLIGHT
    else:
        fmt = prefs.get("fmt")
    if not isinstance(fmt, dict):
        return ev["payload"]
    out = push_format.render(ev["play"], fmt, LABELS, SHORT)
    if not out:
        return ev["payload"]
    return {**ev["payload"], "title": out[0], "body": out[1]}


def messages_for(sub: dict, events: list[dict]) -> list[dict]:
    mine = [payload_for(sub, e) for e in events if wants(sub, e)]
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
        try:   # the feats baseline; a live site without the file yet just means no feat alerts this run
            with open(os.path.join(dest, "accomplishments.json"), "w", encoding="utf-8") as fh:
                json.dump(read(LIVE_DATA, "accomplishments.json"), fh)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
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


def send_test(key_name: str, icon_abbr: str = "", sample: bool = False) -> int:
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
    real = None
    if sample:   # a real alert exactly as the sender builds it (title, lines, diamonds, icon), for the newest play on the live site
        meta = read(LIVE_DATA, "meta.json")
        plays = [m for sess in (meta.get("sessions") or [])[:1] for m in read(LIVE_DATA, f"plays_{sess:02d}.json") if m.get("result") is not None]
        newest = sorted(plays, key=lambda m: (m["timestamp"], m["play_num"]))[-1]
        logos = {abbr: f"{SITE}data/push_icons/{abbr}.png" for abbr, t in (meta.get("teams") or {}).items() if t.get("logo_url")}
        real = moment_event(newest, logos)["payload"]
        print(f"push test: sample is play {newest['play_num']}: {real['title']} | {real['body']!r}")
    for s in mine:
        dev = hashlib.sha1(s["endpoint"].encode()).hexdigest()[:6]
        msg = {"title": "MLN Gameday test", "body": f"Device {dev} \u00b7 this is only a test alert" + (f" with the {icon_abbr} icon" if icon_abbr else ""), "url": SITE}
        if real:
            msg = real
        if icon_abbr:   # the same icon URL the real play alerts carry, to see whether a remote icon changes what iOS does
            msg["icon"] = f"{SITE}data/push_icons/{icon_abbr}.png"
        try:
            resp = webpush(subscription_info=s["subscription"], data=json.dumps(wire(s, msg)), vapid_private_key=vkey, vapid_claims={"sub": subject}, ttl=3600,
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
    ap.add_argument("--test-sample", action="store_true", help="with --test: send the newest real play as a real-format alert instead of the labelled test text")
    ap.add_argument("--replay", type=int, metavar="N", help="testing: pretend the latest N plays and the latest finished game are new (no previous build needed)")
    args = ap.parse_args()

    if args.test:
        return send_test(args.test, args.test_icon.upper(), args.test_sample)
    if args.snapshot:
        return snapshot(args.snapshot)
    new_base = args.new_base or NEW_DIR
    if args.expect_state and new_base.startswith("http") and not wait_for_deploy(new_base, args.expect_state):
        print("push: the site never showed the new build; nothing sent")
        return 0
    new_meta = read(new_base, "meta.json")
    sessions = (new_meta.get("sessions") or [])[:2]          # the live session and the one before it
    new_raw = [m for sess in sessions for m in read(new_base, f"plays_{sess:02d}.json")]   # with the "Now Batting" placeholders (pitching changes ride on them)
    new_plays = {m["moment_id"]: m for m in new_raw if m.get("result") is not None}
    old_raw: list[dict] = []
    if args.replay:
        newest = sorted(new_plays.values(), key=lambda m: m["timestamp"])[-args.replay:]
        seen = set(new_plays) - {m["moment_id"] for m in newest}
        old_meta = json.loads(json.dumps(new_meta))
        replayed = {m["moment_id"] for m in newest}
        done = final_games(old_meta)
        if done:
            done[sorted(done)[-1]]["is_game_final"] = False   # game codes are SSNNGG, so the biggest code is the latest game
    else:
        try:
            old_meta = read(args.old_base, "meta.json")
            seen = set()
            for sess in sessions:
                try:
                    old_rows = read(args.old_base, f"plays_{sess:02d}.json")
                    old_raw += old_rows
                    seen |= {m["moment_id"] for m in old_rows}
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
    LABELS.update(labels)
    SHORT.update(new_meta.get("result_short") or {})
    old_final = final_games(old_meta)
    by_game: dict[str, list[dict]] = {}
    for m in new_plays.values():
        by_game.setdefault(m["game_code"], []).append(m)
    events = [moment_event(m, logos) for m in sorted((m for mid, m in new_plays.items() if mid not in seen), key=lambda m: (m["timestamp"], m["play_num"]))]
    new_pc = pitching_changes(new_raw)
    old_pc = {k for k, c in new_pc.items() if c["play"]["moment_id"] not in replayed} if args.replay else set(pitching_changes(old_raw))
    events += sorted((pitching_event(c, logos) for k, c in new_pc.items() if k not in old_pc), key=lambda e: e["sort"])
    events += [final_event(g, by_game.get(code, []), labels, logos) for code, g in final_games(new_meta).items() if code not in old_final]
    if len(events) > MAX_NEW_EVENTS:
        print("push: far too many at once (data reset?); nothing sent")
        return 0
    try:   # new feats: rows in the new accomplishments.json the baseline did not have (no baseline file = none this run)
        new_acc = read(new_base, "accomplishments.json")
        old_acc = new_acc[:-3] if args.replay else read(args.old_base, "accomplishments.json")
        fresh = new_feats(old_acc, new_acc)
        if len(fresh) > MAX_NEW_FEATS:
            print(f"push: {len(fresh)} new feats at once (a new feat type backfilling?); no feat alerts")
            fresh = []
        if fresh:
            names = read(new_base, "search/names.json").get("n") or {}
            titles = feat_titles()
            events += [feat_event(r, names, titles, logos) for r in fresh]
    except Exception as exc:  # noqa: BLE001 - feats are extra; never let them cost the play alerts
        print(f"push: no feat alerts this run ({exc})")
    print(f"push: {len(events)} new events")
    if not events:
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
                resp = webpush(subscription_info=s["subscription"], data=json.dumps(wire(s, m)), vapid_private_key=key, vapid_claims={"sub": subject}, ttl=3600,
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

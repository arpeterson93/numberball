"""Gate for the Build Key Moments workflow: does the site need a rebuild + deploy at all? (writes deploy=true|false and state=<json> to $GITHUB_OUTPUT)

  python tools/should_deploy.py

The workflow fires every ~10 minutes, but most of the time the MLN sheet hasn't changed, and every deploy queues behind the
previous one in the `pages` concurrency group. So before building anything: hash the sheet tabs the build reads (the same
public gviz CSVs, a few seconds of downloading), compare with docs/data/build_hash.json on the live site, and skip the build
and the deploy when nothing changed - except
  - a push (code changes always deploy), and
  - when the live site's last deploy is over HEARTBEAT_MIN old, so the page's "updated" label never goes stale for long.
Any problem reading the sheet or the live site means build + deploy. The build job writes `state` to docs/data/build_hash.json,
which ships with the deploy. Standard library only (the gate job installs nothing).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MLN_SHEET_ID = "1NQ4l0EjwFYVdIjlYIkycYfuWw_jdZKiWsNURTcTy4AA"          # key_moments_build.MLN_SHEET_ID
AWARDS_SHEET_ID = "17Clgc4dBNXCDeD0SaJLF5LPu_1jZMqt-OimYwJv7r4I"      # key_moments_build.SESSION_AWARDS_SHEET_ID
# (sheet, tab) pairs key_moments_build / career_build read for the live season (archive seasons are committed files)
TABS = [(MLN_SHEET_ID, t) for t in ("Plays (Raw)", "Games", "Lineups", "Teams", "Players", "Rosters")] + [(AWARDS_SHEET_ID, "Sheet1")]
LIVE_URL = "https://mlngameday.com/data/build_hash.json"
HEARTBEAT_MIN = 60


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "should-deploy"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def inputs_hash() -> str:
    h = hashlib.sha256()
    for sheet, tab in TABS:
        h.update(f"{sheet}/{tab}\n".encode())
        h.update(_get(f"https://docs.google.com/spreadsheets/d/{sheet}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab)}"))
    return h.hexdigest()


def live_state() -> dict | None:
    try:
        return json.loads(_get(f"{LIVE_URL}?t={int(datetime.now().timestamp())}"))
    except Exception as e:
        print(f"could not read the live build hash ({e!r})")
        return None


def decide(new_hash: str | None, live: dict | None, event: str, now: datetime) -> tuple[bool, str]:
    if event == "push":
        return True, "push: code changes always deploy"
    if not new_hash:
        return True, "could not hash the sheet"
    if not live or "hash" not in live:
        return True, "no live hash to compare with"
    try:
        age = (now - datetime.fromisoformat(live["deployed_at"].replace("Z", "+00:00"))).total_seconds() / 60
    except Exception:
        return True, "live deploy time unreadable"
    if live["hash"] != new_hash:
        return True, "sheet changed"
    if age >= HEARTBEAT_MIN:
        return True, f"sheet unchanged but the last deploy was {age:.0f} min ago (heartbeat)"
    return False, f"sheet unchanged, last deploy {age:.0f} min ago"


def main() -> None:
    now = datetime.now(timezone.utc)
    try:
        new_hash = inputs_hash()
    except Exception as e:
        print(f"sheet read failed ({e!r})")
        new_hash = None
    go, why = decide(new_hash, live_state(), os.environ.get("GITHUB_EVENT_NAME", ""), now)
    state = json.dumps({"hash": new_hash, "deployed_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z")})
    print(f"deploy={str(go).lower()} ({why}); sheet hash {(new_hash or '')[:12]}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"deploy={str(go).lower()}\nstate={state}\n")


if __name__ == "__main__":
    sys.exit(main())

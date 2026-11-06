"""Play clips for PioBall: record MLN Gameday's own replay animation for one play.

Gameday (docs/js/app.js) plays a play's animation in the browser, so the clip is a headless-Chromium recording of its deep
link (https://mlngameday.com/?game=<code>&play=<n>, where play_num = code * 1000 + n) cropped to the replay slide and cut
to the one play. Needs `playwright` with Chromium installed; if `ffmpeg` is on PATH the clip is an mp4 (H.264, plays inline
everywhere), otherwise Playwright's bundled ffmpeg makes a webm (VP8).

The site is rebuilt on a cron (~every 10 minutes), so a fresh result isn't there yet: wait_until_published polls the site's
own data file for the play (matched on pitch/swing after the result's play_num floor), then record_clip runs.

  python -I pioball_clip.py <game_code> <play_n> [speed] [out dir]      record one play by hand
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

SITE = os.environ.get("GAMEDAY_URL", "https://mlngameday.com/").rstrip("/") + "/"
SPEED = float(os.environ.get("PIOBALL_CLIP_SPEED", "1.5"))   # Gameday playback speed: 0.5 / 1 / 1.5 / 2
THEME = os.environ.get("PIOBALL_CLIP_THEME", "dark")          # Gameday theme: dark / light
VIEW_W, VIEW_H = 900, 720            # the replay card is 828 wide inside this
MAX_PLAY_S = 40.0                    # wall-clock cap on one play (at 1x a home run runs ~25s)
MAX_STALL = 0.25                     # more repeated frames than this in the animated part and the recording is thrown away
MAX_BYTES = 8 * 1024 * 1024          # stay under Discord's attachment limit
MOTION = 10                          # changed pixels between two samples that count as the picture moving
BURST = 1000                         # changed pixels above which a frame is a slide transition, not the ball / runners
STILL_S = 1.2                        # seconds of stillness after the action that mark the play as over
HOLD_S = 0.5                         # seconds of the settled picture kept at the end of the clip
class ClipStalled(RuntimeError):
    """The browser was starved of CPU and the recording stalled."""


_one_at_a_time = None                # asyncio.Semaphore(1): one browser at a time, results can arrive in bursts


def deep_link(game_code, play_n: int) -> str:
    return f"{SITE}?game={int(game_code):06d}&play={int(play_n)}"


def play_n_of(game_code, play_num) -> int:
    return int(play_num) - int(game_code) * 1000


# ------------------------------------------------------------------ is the play on the site yet

def site_plays(game_code) -> list[dict]:
    session = int(str(int(game_code)).zfill(6)[2:4])
    url = f"{SITE}data/plays_{session:02d}.json?t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def find_play(game_code, pitch, swing, after_play_num=0, skip=()) -> dict | None:
    """The Gameday play row (with a result) for this game whose pitch and swing match: the oldest play after the floor that isn't in skip."""
    try:
        rows = site_plays(game_code)
    except Exception:
        return None
    code = str(int(game_code)).zfill(6)
    hits = [r for r in rows if str(r.get("game_code")).zfill(6) == code and r.get("result")
            and int(r["play_num"]) > int(after_play_num or 0) and int(r["play_num"]) not in skip
            and (pitch is None or r.get("pitch") == int(pitch)) and (swing is None or r.get("swing") == int(swing))]
    return min(hits, key=lambda r: int(r["play_num"])) if hits else None


async def wait_until_published(game_code, pitch, swing, after_play_num=0, skip=(), timeout_s=35 * 60, every_s=60) -> dict | None:
    """Poll the site's data until the play shows up (the site rebuilds on a ~10 minute cron); None on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = await asyncio.to_thread(find_play, game_code, pitch, swing, after_play_num, skip)
        if row:
            return row
        await asyncio.sleep(every_s)
    return None


# ------------------------------------------------------------------ recording

def _ffmpeg() -> tuple[str, str]:
    """(binary, 'mp4' | 'webm'): the system ffmpeg for mp4 if there is one, else Playwright's own (VP8 webm only)."""
    path = shutil.which("ffmpeg")
    if path:
        return path, "mp4"
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
             os.path.expanduser("~/.cache/ms-playwright"), os.path.expanduser("~/Library/Caches/ms-playwright")]
    for root in filter(None, roots):
        found = sorted(glob.glob(os.path.join(root, "ffmpeg-*", "ffmpeg*")))
        found = [f for f in found if not f.endswith((".txt", ".json"))]
        if found:
            return found[-1], "webm"
    raise RuntimeError("no ffmpeg: install one, or run `playwright install ffmpeg`")


def _encode(src: pathlib.Path, dst: pathlib.Path, ss: float, dur: float, crop: tuple[int, int, int, int], kind: str, ff: str,
            kbps: int) -> None:
    w, h, x, y = (v - v % 2 for v in crop)
    vf = f"crop={w}:{h}:{x}:{y}"
    codec = (["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-preset", "veryfast"]
             if kind == "mp4" else ["-c:v", "libvpx", "-auto-alt-ref", "0"])
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-vf", vf,
           *codec, "-b:v", f"{kbps}k", "-g", "25", "-fps_mode", "cfr", "-r", "25", "-an", str(dst)]   # steady 25 fps: Playwright's timestamps are irregular
    subprocess.run(cmd, check=True, timeout=120)


def _boost_browser() -> None:
    """Give the browser's processes high CPU priority while recording (only for the 10-25 seconds it runs): on a busy machine (the bot, Discord, a game
    feed all share this laptop) a starved Chromium drops frames and the clip stutters or stalls. Best effort."""
    try:
        import psutil
        me = psutil.Process(os.getpid())
        level = psutil.HIGH_PRIORITY_CLASS if os.name == "nt" else -5
        for ch in me.children(recursive=True):
            try:
                ch.nice(level)
            except Exception:
                pass
    except Exception:
        pass


def _last_motion(src: pathlib.Path, ff: str, ss: float, span: float, crop: tuple[int, int, int, int], work: pathlib.Path) -> tuple[float, float]:
    """Seconds (from ss) of the last frame in the span where the picture still moves. The ball / runner dots keep a few dozen
    pixels changing every frame while a play animates and a settled picture changes none, so the long hold Gameday leaves on
    screen after the action is found and cut."""
    from PIL import Image, ImageChops
    frames = work / "frames"
    frames.mkdir(exist_ok=True)
    w, h, x, y = (v - v % 2 for v in crop)
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{ss:.2f}", "-t", f"{span:.2f}", "-i", str(src),
                    "-vf", f"crop={w}:{h}:{x}:{y},scale=414:312", str(frames / "f_%04d.png")], check=True, timeout=120)
    prev, last, quiet = None, 0, []
    for i, f in enumerate(sorted(frames.glob("f_*.png"))):
        im = Image.open(f).convert("L")
        n = ImageChops.difference(im, prev).point(lambda v: 255 if v > 12 else 0).histogram()[255] if prev is not None else 0
        quiet.append(n <= 2)                              # a frame identical to the one before it
        if MOTION < n <= BURST:
            last = i                                      # a big burst (the slide fading out) isn't part of the play
        elif last and i - last >= STILL_S * 25:
            break                                         # settled: ignore the slide change that comes later
        prev = im
    shutil.rmtree(frames, ignore_errors=True)
    live = quiet[:last + 1]
    # while a play animates every frame changes, so a run of identical frames means the browser stalled (it was starved of CPU)
    return last / 25.0, (sum(live) / len(live) if live else 0.0)      # Playwright records at 25 fps


def record_clip(game_code, play_n: int, speed: float = SPEED, expect_batter: str | None = None) -> tuple[bytes, str, float]:
    """Record one play's replay. Returns (video bytes, 'mp4'|'webm', seconds). Blocking - run it in a thread.
    Gameday opens the game's replay from the TOP when it can't find the play, so expect_batter (the play's batter name) is
    checked on screen and the clip is refused if it isn't the one showing."""
    from playwright.sync_api import sync_playwright
    ff, kind = _ffmpeg()
    url = deep_link(game_code, play_n)
    work = pathlib.Path(tempfile.mkdtemp(prefix="pioclip_"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, record_video_dir=str(work),
                                      record_video_size={"width": VIEW_W, "height": VIEW_H}, color_scheme=THEME)
            # Gameday keeps playback speed, loop mode and theme in localStorage; "none" holds on the last play instead of looping
            ctx.add_init_script(f"try{{localStorage.setItem('km_playback_speed','{speed:.2f}');localStorage.setItem('km_loop_mode','none');"
                                f"localStorage.setItem('km_theme','{THEME}')}}catch(e){{}}")
            page = ctx.new_page()
            t0 = time.time()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("#replay-modal:not([hidden]) #replay-slide .catchup-slide, #replay-modal:not([hidden]) #replay-slide *",
                                   timeout=45000)
            _boost_browser()                     # the renderer processes exist by now
            # the modal's chrome (progress label, speed / repeat / close buttons, prev / next arrows) stays out of the clip
            page.add_style_tag(content="#replay-card > :not(#replay-slide){visibility:hidden !important}")
            card = page.query_selector("#replay-card").bounding_box()
            slide = page.query_selector("#replay-slide").bounding_box()
            top = max(card["y"], slide["y"] - 12)
            box = {"x": card["x"], "y": top, "width": card["width"], "height": card["y"] + card["height"] - top}
            label = re.compile(r"Play (\d+) of (\d+)")

            def index() -> int | None:
                m = label.search(page.text_content("#replay-card") or "")
                return int(m.group(1)) if m else None
            if expect_batter and expect_batter.lower() not in page.inner_text("#replay-slide").lower():
                raise RuntimeError(f"replay opened on a different play than {expect_batter!r} (play not on the site?)")
            first = index()
            t_start = time.time() - t0           # the slide is mounted; its animation starts now
            limit = MAX_PLAY_S / max(speed, 0.25)
            # no screenshots while recording (each one flickers a frame into the video): just wait for Gameday to move on
            # to the next play (or the cap), then trim the held, settled tail from the recording itself
            while time.time() - t0 - t_start < limit:
                page.wait_for_timeout(250)
                if first is not None and index() not in (None, first):
                    break
            t_end = time.time() - t0 - 0.6       # stop before the next play's first frame
            video = page.video
            ctx.close()
            src = pathlib.Path(video.path())
            browser.close()
        crop = (round(box["width"]), round(box["height"]), round(box["x"]), round(box["y"]))
        ss = max(0.0, t_start - 0.2)
        last, stalled = _last_motion(src, ff, ss, t_end - ss, crop, work)
        if stalled > MAX_STALL or last < 1.0:
            raise ClipStalled(f"recording stalled ({stalled:.0%} repeated frames, {last:.1f}s of motion)")
        dur = max(2.0, min(t_end - ss, last + HOLD_S))
        dst = work / f"clip.{kind}"
        for kbps in (1500, 800, 400):         # shrink until it fits Discord
            _encode(src, dst, ss, dur, crop, kind, ff, kbps)
            if dst.stat().st_size <= MAX_BYTES:
                break
        return dst.read_bytes(), kind, dur
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def clip_for(game_code, play_n: int, speed: float = SPEED, expect_batter: str | None = None):
    """record_clip off the event loop, one at a time. Returns (bytes, kind, seconds) or None if it failed."""
    global _one_at_a_time
    if _one_at_a_time is None:
        _one_at_a_time = asyncio.Semaphore(1)
    async with _one_at_a_time:
        try:
            for attempt in (1, 2):
                try:
                    return await asyncio.to_thread(record_clip, game_code, play_n, speed, expect_batter)
                except ClipStalled as e:
                    print(f"play clip {game_code} play {play_n}: {e}" + (" - retrying" if attempt == 1 else ""))
                    if attempt == 2:
                        return None
                    await asyncio.sleep(20)         # let whatever starved the browser finish
        except Exception as e:
            print(f"play clip failed ({game_code} play {play_n}): {e!r}")
            return None


if __name__ == "__main__":
    code, n = sys.argv[1], int(sys.argv[2])
    spd = float(sys.argv[3]) if len(sys.argv) > 3 else SPEED
    out = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else pathlib.Path(".")
    t = time.time()
    data, kind, secs = record_clip(code, n, spd)
    path = out / f"clip_{code}_{n}.{kind}"
    path.write_bytes(data)
    print(f"{path}  {len(data) / 1024:.0f} KB  {secs:.1f}s of play  (took {time.time() - t:.0f}s)")

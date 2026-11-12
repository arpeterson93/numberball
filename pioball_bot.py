"""PioBall - swing guessing game bot for the Portland Pioneers.

Separate from discord_bot.py (the scouting bot): own token, own permissions.

Players /guess a swing (1-1000) for the next pitch of the live Pioneers game.
The ump bot's results feed (PIOBALL_RESULTS_CHANNEL_ID) drives it: a "swing is
in" notice locks guessing, and the result message ("Pitch: 129 / Swing: 673 /
Result: K") grades every guess immediately. For games the feed doesn't cover,
a 60s loop grades from Supabase `plays` once the 5-minute sync lands.

Requires PIOBALL_BOT_TOKEN, the Message Content intent (enabled in the Discord
dev portal), and the SUPABASE_URL / SUPABASE_KEY env vars database.py reads.
Optional: PIOBALL_RESULTS_CHANNEL_ID, PIOBALL_GUILD_ID (instant command sync while developing), SWING_TEAM.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
import pace
import pioball_clip
import pioball_render
import pioball_timers
import swing_game
import swing_model



def _load_dotenv(path: str = ".env") -> None:
    """KEY=VALUE lines from a (gitignored) .env next to this file, so the token
    doesn't have to be re-set in every terminal. Real env vars win."""
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        return
    for line in open(full, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
TOKEN = os.environ.get("PIOBALL_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("PIOBALL_BOT_TOKEN is not set. Put PIOBALL_BOT_TOKEN=... in a .env file next to "
                     "pioball_bot.py (it's gitignored), or `set` it in the same window you run the bot from.")
TEST_GUILD_ID = os.environ.get("PIOBALL_GUILD_ID")
RESULTS_CHANNEL_ID = int(os.environ.get("PIOBALL_RESULTS_CHANNEL_ID") or 0)

# message_content is a privileged intent - only used to recognise Pioneers
# pitches in the results thread (see on_message).
# Supabase reuses pooled HTTP/2 connections, and the server (or a network blip) can drop one while it sits idle; postgrest only
# retries on 503/520 responses, so the next request died with "Server disconnected" (a /guess left on "thinking"). Retry a
# dropped connection once on a fresh one, for every Supabase call this process makes.
import httpx
import postgrest._sync.request_builder as _pgrst

_send_with_retry = _pgrst.send_with_retry


def _send_retrying_dropped_connection(req):
    try:
        return _send_with_retry(req)
    except httpx.TransportError as e:
        print(f"supabase connection dropped ({e!r}), retrying once")
        return _send_with_retry(req)


_pgrst.send_with_retry = _send_retrying_dropped_connection

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """A command that fails after deferring would otherwise sit on "thinking" forever: say so, so people can retry."""
    print(f"command {getattr(interaction.command, 'name', '?')} failed: {error!r}")
    msg = "Something went wrong on my end - nothing was saved. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        print(f"could not report the command error: {e!r}")

_game_override: str | None = None
_batting_team: str | None = None   # from the feed's "Batter Up!" (e.g. "CAR"); None until seen
_last_feed_result: dict | None = None   # newest result seen in the feed (either team), for the model sync check
_post_channel: int | None = None   # where graded results go if a pitch row has no channel

_scope_choices = [app_commands.Choice(name="This game", value="game"),
                  app_commands.Choice(name="This season", value="season"),
                  app_commands.Choice(name="All time", value="all")]


async def _our_at_bat(game: dict) -> bool:
    """Is it the Pioneers' plate appearance? The feed's last "Batter Up!" is
    authoritative; before one is seen, fall back to the sheet's inning/half."""
    if _batting_team is not None:
        return _batting_team == swing_game.TEAM_ABBREV
    return bool(await asyncio.to_thread(swing_game.our_team_batting, game))


async def _live_game() -> dict | None:
    return await asyncio.to_thread(swing_game.find_live_game, _game_override)


@bot.tree.command(name="guess", description="Guess the swing (1-1000) for the next pitch of the live Pioneers game")
@app_commands.describe(swing="Your swing guess, 1-1000")
async def guess(interaction: discord.Interaction, swing: app_commands.Range[int, 1, 1000]) -> None:
    # Guesses are public on purpose: the team's guesses help inform the real swing.
    global _post_channel
    await interaction.response.defer()
    game = await _live_game()
    if not game:
        await interaction.followup.send("No live Pioneers game right now.")
        return
    ours = await _our_at_bat(game)  # not yet our at-bat -> a guess for our next one
    pitch_row = await asyncio.to_thread(swing_game.get_or_open_pitch, game, interaction.channel_id, ours)
    if await asyncio.to_thread(swing_game.is_closed, pitch_row):
        await interaction.followup.send(
            "That pitch is already in - guessing is closed. Try again once the next AB is up.")
        return
    if not pitch_row.get("channel_id"):  # opened by "Batter Up!" - results post where the first guess was made
        await asyncio.to_thread(swing_game.set_channel, pitch_row["id"], interaction.channel_id)
    _post_channel = int(pitch_row.get("channel_id") or interaction.channel_id)
    prior = await asyncio.to_thread(swing_game.submit_guess, pitch_row, interaction.user.id,
                                    interaction.user.display_name, swing)
    if prior is not None and prior != swing:
        await interaction.followup.send(f"{interaction.user.mention} changed their guess from **{prior}** to **{swing}**.")
    await _send_guesses(interaction, game, highlight=interaction.user.id)


async def _send_guesses(interaction: discord.Interaction, game: dict, highlight: int | None = None) -> None:
    """The current plate appearance's guesses as a table image; the row just
    added by /guess is highlighted. Shared by /guess and /guesses."""
    c = await asyncio.to_thread(swing_game.current_pa, game)
    if not c["pitch_row"] or not c["guesses"]:
        await interaction.followup.send(swing_game.format_current_pa(c))
        return
    try:
        buf = await asyncio.to_thread(pioball_render.render_guesses_table, c, highlight)
        await interaction.followup.send(file=discord.File(buf, filename="pioball_guesses.png"))
    except Exception as e:
        print(f"guesses image failed, sending text: {e!r}")
        await interaction.followup.send(swing_game.format_current_pa(c))


@bot.tree.command(name="clearguesses", description="Clear guesses on the open pitch so they can be re-submitted (e.g. after a pitching change)")
@app_commands.describe(everyone="Clear everyone's guesses (default: just yours)")
async def clearguesses(interaction: discord.Interaction, everyone: bool = False) -> None:
    await interaction.response.defer()
    game = await _live_game()
    p = await asyncio.to_thread(swing_game.peek_open_pitch, game) if game else None
    if not p:
        await interaction.followup.send("No open pitch to clear guesses on.")
        return
    if await asyncio.to_thread(swing_game.is_closed, p):
        await interaction.followup.send("That pitch is already in - guesses can't be cleared.")
        return
    n = await asyncio.to_thread(swing_game.clear_guesses, p["id"], None if everyone else interaction.user.id)
    who = "everyone's guesses" if everyone else f"{interaction.user.mention}'s guess"
    await interaction.followup.send(f"Cleared {who} ({n}). Re-submit with `/guess`." if n else "Nothing to clear.")


@bot.tree.command(name="guesses", description="Show everyone's guesses for the current plate appearance")
async def guesses(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    game = await _live_game()
    if not game:
        await interaction.followup.send("No live Pioneers game right now.")
        return
    await _send_guesses(interaction, game)


_sort_choices = [app_commands.Choice(name="Run value (RV)", value="rv"),
                 app_commands.Choice(name="RE24 runs added", value="re24"),
                 app_commands.Choice(name="Win probability added (WPA)", value="wpa"),
                 app_commands.Choice(name="OPS", value="ops")]


@bot.tree.command(name="leaderboard", description="RV/RE24/WPA + DPA + AVG/OBP/SLG/OPS leaderboard for swing guesses")
@app_commands.describe(scope="This game, this season, or all time (default: this season)",
                       sort="Rank by RV (default), RE24, WPA or OPS")
@app_commands.choices(scope=_scope_choices, sort=_sort_choices)
async def leaderboard(interaction: discord.Interaction, scope: str = "season", sort: str = "rv") -> None:
    await interaction.response.defer()
    game = await _live_game()
    if scope == "game":
        if not game:
            await interaction.followup.send("No live Pioneers game to show a game leaderboard for.")
            return
        rows = await asyncio.to_thread(swing_game.stats, "game", game_code=game["game_code"], by=sort)
        title = f"PioBall - S{game.get('season')} Game {game['game_code']}"
    elif scope == "season":
        season = (game or {}).get("season")
        if season is None:
            season = await asyncio.to_thread(swing_game.current_season)
        rows = await asyncio.to_thread(swing_game.stats, "season", season=season, by=sort)
        title = f"PioBall - Season {season}"
    else:
        rows = await asyncio.to_thread(swing_game.stats, "all", by=sort)
        title = "PioBall - All-Time"
    if not rows:
        await interaction.followup.send(f"**{title}**\nNo graded guesses yet.")
        return
    try:
        label = next(c.name for c in _sort_choices if c.value == sort)
        buf = await asyncio.to_thread(pioball_render.render_leaderboard, rows, title, label)
        await interaction.followup.send(file=discord.File(buf, filename="pioball_leaderboard.png"))
    except Exception as e:
        print(f"leaderboard image failed, sending text: {e!r}")
        await interaction.followup.send(swing_game.format_leaderboard(rows, title))


async def _roster_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    names = await asyncio.to_thread(swing_game.roster)
    return [app_commands.Choice(name=n, value=n) for n in sorted(names) if current.lower() in n.lower()][:25]


@bot.tree.command(name="player", description="A player's card: game/season/all-time stats and recent guesses")
@app_commands.autocomplete(player=_roster_autocomplete)
@app_commands.describe(player="Pioneers player (default: you)", count="How many recent plate appearances (default 10)")
async def player(interaction: discord.Interaction, player: str | None = None,
                 count: app_commands.Range[int, 1, 25] = 10) -> None:
    await interaction.response.defer()
    if player:
        roster = await asyncio.to_thread(swing_game.roster)
        if player not in roster:
            await interaction.followup.send(f"**{player}** isn't on the Pioneers roster - pick a name from the list.",
                                            ephemeral=True)
            return
        who_id, name = roster[player], player
    else:
        who_id = interaction.user.id
        name = swing_game.display_name(who_id, interaction.user.display_name)
    game = await _live_game()
    season = (game or {}).get("season")
    if season is None:
        season = await asyncio.to_thread(swing_game.current_season)
    card = await asyncio.to_thread(swing_game.player_card, who_id, count,
                                   game["game_code"] if game else None, season)
    if not card["history"]["rows"]:
        await interaction.followup.send(f"No graded guesses for **{name}** yet.")
        return
    try:
        buf = await asyncio.to_thread(pioball_render.render_player, name, card)
        await interaction.followup.send(file=discord.File(buf, filename="pioball_player.png"))
    except Exception as e:
        print(f"player image failed: {e!r}")
        await interaction.followup.send(f"Couldn't render {name}'s card.")


@bot.tree.command(name="results", description="Show the results table for the latest graded pitch (or an earlier one)")
@app_commands.describe(pitch="Which pitch of the game, as numbered in /status (default: the latest graded one)")
async def results(interaction: discord.Interaction, pitch: app_commands.Range[int, 1, 500] | None = None) -> None:
    await interaction.response.defer()
    game = await _live_game()
    if not game:
        await interaction.followup.send("No live Pioneers game right now.")
        return
    g = await asyncio.to_thread(swing_game.graded_payload, game["game_code"], pitch)
    if not g:
        await interaction.followup.send("No graded pitch to show yet." if not pitch
                                        else f"Pitch {pitch} hasn't been graded (or doesn't exist).")
        return
    if not g["guesses"]:
        await interaction.followup.send("Nobody guessed on that pitch.")
        return
    await _post_graded(interaction.followup, g)


_metric_choices = [app_commands.Choice(name="OBP", value="obp"), app_commands.Choice(name="SLG", value="slg"),
                  app_commands.Choice(name="Run value", value="rv")]


@bot.tree.command(name="model", description="The pitch-likelihood model's recommended swing (only once the data is synced)")
@app_commands.describe(metric="What to optimize (default OBP)",
                       public="Post it for everyone (default: just you, so it doesn't sway the guesses)",
                       force="Run even if the plays sync is behind (the answer may be wrong)")
@app_commands.choices(metric=_metric_choices)
async def model(interaction: discord.Interaction, metric: str = "obp", public: bool = False,
                force: bool = False) -> None:
    await interaction.response.defer(ephemeral=not public)
    game = await _live_game()
    if not game:
        await interaction.followup.send("No live Pioneers game right now.", ephemeral=not public)
        return
    try:
        r = await asyncio.to_thread(swing_model.recommend, game, metric, _last_feed_result, force)
    except Exception as e:
        print(f"model failed: {e!r}")
        await interaction.followup.send("The model hit an error - check the bot console.", ephemeral=not public)
        return
    await interaction.followup.send(swing_model.format_recommendation(r), ephemeral=not public)


@bot.tree.command(name="status", description="Show the open pitch and how many guesses are in")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    game = await _live_game()
    if not game:
        await interaction.followup.send("No live Pioneers game right now.", ephemeral=True)
        return
    p = await asyncio.to_thread(swing_game.peek_open_pitch, game)
    if not p:
        await interaction.followup.send(
            f"Game `{game['game_code']}`: no open pitch yet - the first `/guess` opens the next one.", ephemeral=True)
        return
    closed = await asyncio.to_thread(swing_game.is_closed, p)
    n = await asyncio.to_thread(swing_game.count_guesses, p["id"])
    who = (f"{p['batter']} vs {p.get('pitcher') or '?'}, {p.get('half') or ''} {p.get('inning') or ''}, "
           f"{p.get('outs')} out" if p.get("batter") else "our next plate appearance (batter TBD)")
    await interaction.followup.send(
        f"Game `{game['game_code']}`: {who}. "
        f"{_guess_count(n)} - {'CLOSED (pitch is in)' if closed else 'open'}.", ephemeral=True)


@bot.tree.command(name="game", description="Pin a game code (omit to go back to auto-detecting the live Pioneers game)")
@app_commands.describe(game_code="Numberball game code, or leave empty to auto-detect")
async def game(interaction: discord.Interaction, game_code: str | None = None) -> None:
    global _game_override
    _game_override = game_code or None
    await interaction.response.send_message(
        f"Pinned to game `{game_code}`." if game_code else "Back on auto-detect.", ephemeral=True)


@bot.tree.command(name="clip", description="Play back MLN Gameday's animation of a play from its play id")
@app_commands.describe(play_id="Play id: the game code then the play number, e.g. 131020001 (play 1 of game 131020)",
                       speed="Playback speed (default 1x)")
@app_commands.choices(speed=[app_commands.Choice(name=f"{s}x", value=s) for s in (1.0, 1.5, 2.0)])
async def clip_command(interaction: discord.Interaction, play_id: str, speed: app_commands.Choice[float] | None = None) -> None:
    digits = re.sub(r"\D", "", play_id)
    if len(digits) < 7:
        await interaction.response.send_message("Give the play id as the game code then the play number, e.g. `131020001`.", ephemeral=True)
        return
    await interaction.response.defer()
    code, n = int(digits) // 1000, int(digits) % 1000
    rows = await asyncio.to_thread(pioball_clip.site_plays, code)
    row = next((r for r in rows if int(r["play_num"]) == int(digits)), None)
    if not row:
        await interaction.followup.send(f"Play `{digits}` isn't on MLN Gameday (yet) - the site updates about every 10 minutes.")
        return
    out = await pioball_clip.clip_for(code, n, speed.value if speed else pioball_clip.SPEED, expect_batter=row.get("batter_name"))
    if not out:
        await interaction.followup.send("Couldn't record that play - the clip recorder isn't available or failed (see the bot log).")
        return
    data, kind, _ = out
    await interaction.followup.send(f"[Watch this play on MLN Gameday](<{pioball_clip.deep_link(code, n)}>)",
                                    file=discord.File(io.BytesIO(data), filename=f"play_{digits}.{kind}"))


@bot.tree.command(name="pace", description="Pace of play for a game: team and ump time, from the results feed timestamps")
@app_commands.describe(game_code="Numberball game code (default: the live Pioneers game)",
                       include_open="Count the stretch still running right now (default yes; only applies to unfinished games)")
async def pace_command(interaction: discord.Interaction, game_code: str | None = None, include_open: bool = True) -> None:
    await interaction.response.defer()
    g = await asyncio.to_thread(database.get_game_by_code, int(game_code)) if game_code else await _live_game()
    if not g:
        await interaction.followup.send("No game found - give a game code." if not game_code else f"No game `{game_code}` found.")
        return
    if not RESULTS_CHANNEL_ID:
        await interaction.followup.send("The results feed channel isn't configured.")
        return
    # start_time is a date placeholder (12:00 UTC) and end_time runs ~5h early (local time stored as UTC), so pad both ends; trim_to_game cuts the exact game out.
    start = pace.parse_ts(g.get("start_time"))
    if start is None:
        await interaction.followup.send(f"Game `{g['game_code']}` has no start time on file.")
        return
    live = pace.is_live(g)   # a live game's end_time lags the feed (and runs ~5h early), so read right up to now
    end = datetime.now(timezone.utc) if live else pace.parse_ts(g.get("end_time")) or datetime.now(timezone.utc)
    channel = bot.get_channel(RESULTS_CHANNEL_ID) or await bot.fetch_channel(RESULTS_CHANNEL_ID)
    msgs = [{"ts": m.created_at, "text": _message_text(m)}
            async for m in channel.history(limit=None, after=start - timedelta(hours=12),
                                           before=end + timedelta(hours=8), oldest_first=True)]
    msgs = pace.trim_to_game(msgs, g["game_code"])
    pas = pace.build_pas(msgs)
    now = datetime.now(timezone.utc) if include_open and live else None
    s = pace.summarize(pace.analyze(pas, now))
    if s["n_pa"] < 2:
        await interaction.followup.send(f"Not enough timed plate appearances in the feed for game `{g['game_code']}`.")
        return
    looks = await asyncio.to_thread(swing_game.team_looks, [g.get("away_team"), g.get("home_team")], g.get("season"))
    sides = pace.game_sides(g, looks, list(s["teams"]))
    title = f"Pace of play - {g.get('away_team')} @ {g.get('home_team')}"
    subtitle = (f"S{g.get('season')} | game {g['game_code']} | {s['n_pa']} plate appearances | {pace.fmt_dur(s['total_raw'])} "
                f"on the clock, {pace.fmt_dur(s['total_act'])} active{pace.open_note(s)}")
    try:
        sides = [{"abbrev": x["abbrev"], "color": pioball_render._hex(x["hex"])} for x in sides]
        buf = await asyncio.to_thread(pioball_render.render_pace, s, title, subtitle, sides[0], sides[1],
                                     pace.half_innings(pas, now), scenarios=pace.scenarios(pas))
        await interaction.followup.send(file=discord.File(buf, filename="pace.png"))
    except Exception as e:  # fall back to the text report rather than say nothing
        print(f"pace image failed, sending text: {e!r}")
        body = pace.format_report(s, title)
        if len(body) > 1900:
            await interaction.followup.send(file=discord.File(io.BytesIO(body.encode("utf-8")), filename="pace.txt"))
        else:
            await interaction.followup.send(f"```\n{body}\n```")


CLIPS_ON = os.environ.get("PIOBALL_CLIPS") == "1"   # needs playwright + chromium on this machine (pioball_clip.py)
_clip_tasks: set = set()
_clipped: set[int] = set()   # play_nums already clipped, so a repeated pitch/swing pair can't pick the same play twice


def _start_clip(coro) -> None:
    task = asyncio.create_task(coro)
    _clip_tasks.add(task)               # keep a reference so the task isn't collected mid-run
    task.add_done_callback(_clip_tasks.discard)


async def _make_clip(code, pitch, swing, floor, send, caption=None) -> None:
    """Wait for MLN Gameday to have the play (it only rebuilds about every 10 minutes), record its animation, and hand the clip
    to send(text, file). A failure just means no clip."""
    try:
        if not pitch or swing is None:
            print(f"play clip: game {code} result has no pitch/swing (an auto?), no clip")
            return                          # an auto (no pitch / swing to match on)
        print(f"play clip: game {code} pitch {pitch} swing {swing} - waiting for MLN Gameday to have the play (checks every minute)")
        t0 = time.time()
        row = await pioball_clip.wait_until_published(code, pitch, swing, floor, _clipped)
        if not row:
            print(f"play clip skipped: game {code} pitch {pitch} swing {swing} not on Gameday after the wait")
            return
        _clipped.add(int(row["play_num"]))
        n = pioball_clip.play_n_of(code, row["play_num"])
        print(f"play clip: play {n} is on Gameday after {time.time() - t0:.0f}s, recording")
        out = await pioball_clip.clip_for(code, n, expect_batter=row.get("batter_name"))
        if not out:
            return
        data, kind, _ = out
        text = (caption(row) + chr(10) if caption else "") + f"[Watch this play on MLN Gameday](<{pioball_clip.deep_link(code, n)}>)"
        await send(text, discord.File(io.BytesIO(data), filename=f"play.{kind}"))
        print(f"play clip: posted play {n} ({len(data) / 1024:.0f} KB {kind}, {time.time() - t0:.0f}s after the result)")
    except Exception as e:
        print(f"play clip not posted: {e!r}")


async def _post_clip(msg, g: dict) -> None:
    """Our at-bat: reply to the graded results image with the play's clip."""
    p = g["pitch_row"]
    await _make_clip(p.get("game_code"), g.get("pitch"), (g.get("actual") or {}).get("swing"), p.get("after_play_num"),
                     lambda text, f: msg.reply(text, file=f, mention_author=False))


async def _post_pitching_clip(code, parsed: dict) -> None:
    """The opponent's at-bat (our pitching): no PioBall result image to reply to, so the clip goes to the results channel."""
    last = await asyncio.to_thread(swing_game._last_play, code)
    floor = max(0, int(last["play_num"]) - 5) if last else 0        # the sheet already holds this play; a margin covers it

    async def send(text, f):
        channel = bot.get_channel(_post_channel or RESULTS_CHANNEL_ID) or await bot.fetch_channel(_post_channel or RESULTS_CHANNEL_ID)
        await channel.send(text, file=f)

    def caption(r):
        return f"**{r.get('batter_name')}** ({r.get('off_team_abbr')}) vs **{r.get('pitcher_name')}**: {r.get('result')}"
    await _make_clip(code, parsed["pitch"], parsed.get("swing"), floor, send, caption)


async def _post_graded(channel, g: dict) -> None:
    """Results table as an image (closest guess first); plain text if rendering fails."""
    try:
        buf = await asyncio.to_thread(pioball_render.render_graded_table, g)
        msg = await channel.send(file=discord.File(buf, filename="pioball_results.png"))
        if CLIPS_ON:
            _start_clip(_post_clip(msg, g))
    except Exception as e:
        print(f"results image failed, sending text: {e!r}")
        try:
            await channel.send(swing_game.format_graded(g))
        except Exception as e2:  # e.g. the bot can't send in that channel - say so rather than lose it silently
            print(f"results NOT posted to channel {channel.id} (pitch {g['pitch_row'].get('id')}): {e2!r}")


async def _results_channel(pitch_row: dict):
    """Where a graded pitch's results go: the channel of its first guess. get_channel only
    knows what's cached, so fall back to asking Discord; log when it truly can't be found
    (the pitch is already graded by then, so a silent skip would lose the post)."""
    cid = int(pitch_row.get("channel_id") or _post_channel or 0)
    if not cid:
        print(f"results NOT posted for pitch {pitch_row.get('id')}: no channel on file")
        return None
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception as e:
            print(f"results NOT posted for pitch {pitch_row.get('id')}: can't reach channel {cid}: {e!r}")
    return channel


async def _grade_and_post() -> None:
    results = await asyncio.to_thread(swing_game.grade_ready)
    for g in results:
        channel = await _results_channel(g["pitch_row"])
        if channel is not None:
            await _post_graded(channel, g)


@tasks.loop(seconds=60)
async def grade_loop() -> None:
    try:
        await _grade_and_post()
    except Exception as e:  # keep the loop alive through transient Supabase/Discord errors
        print(f"grade loop error: {e!r}")


@grade_loop.before_loop
async def _before_loop() -> None:
    await bot.wait_until_ready()


_model_tried: set[tuple] = set()   # (pitch id, batter) the model has already been computed for (or failed hard on)


@tasks.loop(seconds=60)
async def model_loop() -> None:
    """Compute and freeze the model's swing for our open pitch as soon as the plays
    sync is current - nobody has to ask. It stays hidden until the results post
    (swing_model.recommend saves it; grading adds the Model row)."""
    try:
        game = await _live_game()
        if not game or not await _our_at_bat(game):
            return
        p = await asyncio.to_thread(swing_game.peek_open_pitch, game)
        if (not p or (p["id"], p.get("batter")) in _model_tried or p.get("model_swing") is not None
                or await asyncio.to_thread(swing_game.is_closed, p)):
            return
        r = await asyncio.to_thread(swing_model.recommend, game, "obp", _last_feed_result, False)
        if r["ok"]:
            _model_tried.add((p["id"], p.get("batter")))
        elif "sync" not in r and "isn't up" not in r["reason"]:
            _model_tried.add((p["id"], p.get("batter")))   # a real failure (no history etc.): don't retry every minute
        # else: the data isn't current yet - try again next minute
    except Exception as e:
        print(f"model loop error: {e!r}")


@model_loop.before_loop
async def _model_before_loop() -> None:
    await bot.wait_until_ready()


def _guess_count(n: int) -> str:
    return f"{n} guess" if n == 1 else f"{n} guesses"


def _message_text(message: discord.Message) -> str:
    """Message body plus every embed's title/description/fields - the ump bot
    posts its plays as "Play Overview" embeds. A forwarded message (how the feed
    reaches the Pioneers server) has no content of its own: the original's text and
    embeds live in message.message_snapshots."""
    parts = [message.content or ""]
    sources = [message.embeds]
    for snap in getattr(message, "message_snapshots", None) or []:
        parts.append(snap.content or "")
        sources.append(snap.embeds)
    for embeds in sources:
        for e in embeds:
            parts += [e.title or "", e.description or ""]
            for f in e.fields:
                parts += [f.name or "", f.value or ""]
    return "\n".join(p for p in parts if p)


@bot.event
async def on_message(message: discord.Message) -> None:
    _timer_observe(message)
    await _handle_feed(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    """The ump bot may post a play and fill in the result by editing it. Only act when the edit
    is what made the message parseable, so an edit to an old, already-handled result can never
    grade the next pitch."""
    age = (discord.utils.utcnow() - after.created_at).total_seconds()
    if age < 600 and swing_game.parse_feed(_message_text(before)) is None:
        _timer_observe(after)
        await _handle_feed(after)


# ------------------------------------------------------------- response timers (see pioball_timers.py)

_timers = pioball_timers.Tracker()
_timers_rebuilt = False
_REMINDER_CHANNEL_ENV = int(os.environ.get("PIOBALL_REMINDER_CHANNEL_ID") or 0)   # only for a setup nobody has /guess-ed in


def _timer_observe(message: discord.Message) -> None:
    """Feed one results-feed message to the timer tracker. Never raises: timers must not break grading."""
    try:
        if message.author == bot.user or not RESULTS_CHANNEL_ID:
            return
        if RESULTS_CHANNEL_ID not in (message.channel.id, getattr(message.channel, "parent_id", 0)):
            return
        text = _message_text(message)
        parsed = swing_game.parse_feed(text) if text else None
        if parsed:
            _timers.observe(parsed, text, message.created_at, pioball_timers.load_settings()["hours"])
    except Exception as e:
        print(f"timer observe error: {e!r}")


async def _rebuild_timers() -> None:
    """After a (re)start, replay the newest feed messages so a timer already running is picked up again."""
    channel = bot.get_channel(RESULTS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(RESULTS_CHANNEL_ID)
        except Exception as e:
            print(f"timer rebuild: can't reach the results channel: {e!r}")
            return
    sources = [channel]
    threads = list(getattr(channel, "threads", None) or [])   # games may be posted in threads under the feed channel
    sources += sorted(threads, key=lambda t: t.last_message_id or 0, reverse=True)[:3]
    msgs = []
    for src in sources:
        try:
            async for m in src.history(limit=60):
                msgs.append(m)
        except Exception as e:
            print(f"timer rebuild: can't read {getattr(src, 'id', '?')}: {e!r}")
    msgs.sort(key=lambda m: m.created_at)
    for m in msgs[-80:]:
        _timer_observe(m)
    print(f"timers rebuilt from {len(msgs)} messages: {_timers.describe(datetime.now(timezone.utc))}")


async def _reminder_channel(game: dict):
    cid = _post_channel
    if not cid:
        row = await asyncio.to_thread(swing_game._latest_pitch_row, game["game_code"])
        cid = int((row or {}).get("channel_id") or 0) or _REMINDER_CHANNEL_ENV
    if not cid:
        return None
    return bot.get_channel(cid) or await bot.fetch_channel(cid)


async def _timer_tick() -> None:
    now = datetime.now(timezone.utc)
    game = await _live_game()
    pend = _timers.pending
    if pend is not None:
        if game:
            hours = pioball_timers.load_settings()["hours"]
            status, timer = await asyncio.to_thread(
                pioball_timers.resolve_pending, pend, game["game_code"], hours, now)
        else:
            status, timer = "retry", None
        if _timers.pending is pend:   # nothing newer arrived while the sheet was being read
            if status != "retry":
                _timers.pending, _timers.timer = None, timer
            elif now - pend.ts > pioball_timers.PENDING_GIVE_UP:
                print(f"timer: gave up on result {pend.parsed}, the sheet never showed it")
                _timers.pending = None
    t = _timers.timer
    if t is None or not game:
        return
    t.game_code = t.game_code or str(game["game_code"])
    if now >= t.deadline or await asyncio.to_thread(pioball_timers.game_is_over, t.game_code):
        _timers.timer = None
        return
    due = pioball_timers.due_alerts(t.deadline, now, t.sent)
    if not due:
        return
    channel = await _reminder_channel(game)
    if channel is None:
        print("timer alert not sent: no pioball channel known yet (a /guess sets it, or set PIOBALL_REMINDER_CHANNEL_ID)")
        return
    await channel.send(t.alert(now), allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    t.sent.update(due)
    print(f"timer alert sent {due}: {t.kind} expires {t.deadline.isoformat()}")


@tasks.loop(seconds=60)
async def timer_loop() -> None:
    try:
        await _timer_tick()
    except Exception as e:
        print(f"timer loop error: {e!r}")


@timer_loop.before_loop
async def _timer_before_loop() -> None:
    await bot.wait_until_ready()


@bot.tree.command(name="timer", description="Set the pitch/swing timer length the reminders count from")
@app_commands.describe(hours="12 hours, or 6 hours (10 when it starts or would expire from 12am to 7am ET)")
@app_commands.choices(hours=[app_commands.Choice(name="12 hours (default)", value=12),
                             app_commands.Choice(name="6 hours (10 if overnight ET)", value=6)])
async def timer_command(interaction: discord.Interaction, hours: app_commands.Choice[int]) -> None:
    pioball_timers.save_settings({**pioball_timers.load_settings(), "hours": hours.value})
    await interaction.response.send_message(
        f"Timers are now {'6 hours (10 if it starts or would expire from 12am to 7am ET)' if hours.value == 6 else '12 hours'}, "
        "starting with the next timer.", ephemeral=True)


notimerday = app_commands.Group(name="notimerday", description="League no-timer days (12am ET that day to 7am ET the next): the clock is paused")


def _fmt_days(days: list[str]) -> str:
    return "\n".join(f"{datetime.strptime(d, '%Y-%m-%d'):%a %b} {int(d[8:])}, {d[:4]}" for d in days) or "none"


@notimerday.command(name="add", description="Add a no-timer day (the day the window starts, e.g. 2026-11-26 or 11/26/2026)")
async def notimerday_add(interaction: discord.Interaction, date: str) -> None:
    d = pioball_timers.parse_day(date)
    if d is None:
        await interaction.response.send_message("Couldn't read that date - use 2026-11-26 or 11/26/2026.", ephemeral=True)
        return
    settings = pioball_timers.load_settings()
    settings["no_timer_days"] = sorted({*settings["no_timer_days"], d.isoformat()})
    pioball_timers.save_settings(settings)
    _timers.recompute()
    await interaction.response.send_message(
        f"Added {d.isoformat()}: the clock is paused from 12am ET that day to 7am ET the next.", ephemeral=True)


@notimerday.command(name="remove", description="Remove a no-timer day")
async def notimerday_remove(interaction: discord.Interaction, date: str) -> None:
    d = pioball_timers.parse_day(date)
    settings = pioball_timers.load_settings()
    if d is None or d.isoformat() not in settings["no_timer_days"]:
        await interaction.response.send_message("That date isn't on the list (see /notimerday list).", ephemeral=True)
        return
    settings["no_timer_days"].remove(d.isoformat())
    pioball_timers.save_settings(settings)
    _timers.recompute()
    await interaction.response.send_message(f"Removed {d.isoformat()}.", ephemeral=True)


@notimerday.command(name="list", description="Show the no-timer days")
async def notimerday_list(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"No-timer days (each 12am ET to 7am ET the next day):\n{_fmt_days(pioball_timers.load_settings()['no_timer_days'])}",
        ephemeral=True)


bot.tree.add_command(notimerday)


def _timers_text() -> str:
    hours = pioball_timers.load_settings()["hours"]
    return f"{_timers.describe(datetime.now(timezone.utc))}\nLength setting: {hours}h."


class _PostTimersView(discord.ui.View):
    """The /timers reply is private; this button posts the current status to the channel for everyone."""

    def __init__(self) -> None:
        super().__init__(timeout=900)

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.primary)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True   # the private reply stays private: only its button changes
        await interaction.response.edit_message(view=self)
        try:   # re-read the timer: it may have changed since the private reply was sent
            await interaction.channel.send(_timers_text(), allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError) as e:
            print(f"timers post failed: {e!r}")
            await interaction.followup.send("Couldn't post to the channel.", ephemeral=True)


@bot.tree.command(name="timers", description="Show the pitch/swing timer running for us (private; a button posts it to the channel)")
async def timers_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(_timers_text(), ephemeral=True, view=_PostTimersView())


async def _handle_feed(message: discord.Message) -> None:
    # Results feed: not every message is a play, so only act on a recognised
    # swing-in notice (lock guessing) or result (grade right now).
    if message.author == bot.user or not RESULTS_CHANNEL_ID:
        return
    if RESULTS_CHANNEL_ID not in (message.channel.id, getattr(message.channel, "parent_id", 0)):
        return
    text = _message_text(message)
    if not text:  # nothing readable: say what the message was made of, so the format can be worked out
        print(f"feed message with no readable text: type={message.type} embeds={len(message.embeds)} "
              f"snapshots={len(getattr(message, 'message_snapshots', None) or [])} "
              f"components={len(message.components)} attachments={len(message.attachments)}")
    parsed = swing_game.parse_feed(text)
    if not parsed:
        if re.search(r"pitch|result|swing", text, re.I):  # looks like a play we can't read: show it
            print(f"feed message not recognised: {text[:400]!r}")
        return
    print(f"feed {parsed['kind']}: {parsed}")
    global _batting_team, _last_feed_result
    if parsed["kind"] == "result":
        seen_in = await _live_game()
        _last_feed_result = {"pitch": parsed["pitch"], "swing": parsed.get("swing"), "at": time.time(),
                             "game_code": seen_in["game_code"] if seen_in else None}
    if parsed["kind"] == "batter_up":
        _batting_team = parsed["team"]
        live = await _live_game()
        if live and _batting_team == swing_game.TEAM_ABBREV:
            # Our at-bat is up: capture the batter/pitcher/ranges for any open
            # pitch (guesses may have been taken early, during their at-bat).
            await asyncio.to_thread(swing_game.open_for_batter_up, live)
        return
    if _batting_team not in (None, swing_game.TEAM_ABBREV):
        # swing notices and results for the opponent's at-bats aren't ours to grade; with clips on, a result still gets its play clip
        if CLIPS_ON and parsed["kind"] == "result" and seen_in:
            _start_clip(_post_pitching_clip(seen_in["game_code"], parsed))
        return
    live = await _live_game()
    if not live:
        print(f"feed {parsed['kind']} ignored: no live Pioneers game found")
        return
    if parsed["kind"] == "swing_in":
        return  # guessing stays open until the result arrives (graded), not when the swing is submitted
    g = await asyncio.to_thread(swing_game.grade_from_feed, live["game_code"], parsed)
    if g:
        channel = await _results_channel(g["pitch_row"])
        if channel is not None:
            await _post_graded(channel, g)


@bot.event
async def on_ready() -> None:
    print(f"play clips {'ON' if CLIPS_ON else 'OFF (set PIOBALL_CLIPS=1)'}")
    if not grade_loop.is_running():
        grade_loop.start()
    if not model_loop.is_running():
        model_loop.start()
    global _timers_rebuilt
    if not _timers_rebuilt and RESULTS_CHANNEL_ID:
        _timers_rebuilt = True   # a reconnect must not replay the feed and forget which alerts already went out
        await _rebuild_timers()
    if not timer_loop.is_running():
        timer_loop.start()
    await bot.tree.sync()
    if TEST_GUILD_ID:
        # Commands are global now. A guild copy on top of that shows every command twice in that
        # server's list, so clear any copies an earlier version of this bot registered there.
        guild = discord.Object(id=int(TEST_GUILD_ID))
        bot.tree.clear_commands(guild=guild)
        try:
            await bot.tree.sync(guild=guild)
        except discord.Forbidden:
            print(f"Couldn't clear guild commands in {TEST_GUILD_ID}: the bot isn't in that server, or was "
                  "invited without the applications.commands scope.")
    print(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)

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
import os
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
import pioball_render
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
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

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
            games = await asyncio.to_thread(database.get_games)
            season = max((g.get("season") or 0) for g in games)
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
        games = await asyncio.to_thread(database.get_games)
        season = max((g.get("season") or 0) for g in games)
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


async def _post_graded(channel, g: dict) -> None:
    """Results table as an image (closest guess first); plain text if rendering fails."""
    try:
        buf = await asyncio.to_thread(pioball_render.render_graded_table, g)
        await channel.send(file=discord.File(buf, filename="pioball_results.png"))
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
    await _handle_feed(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    """The ump bot may post a play and fill in the result by editing it. Only act when the edit
    is what made the message parseable, so an edit to an old, already-handled result can never
    grade the next pitch."""
    age = (discord.utils.utcnow() - after.created_at).total_seconds()
    if age < 600 and swing_game.parse_feed(_message_text(before)) is None:
        await _handle_feed(after)


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
        return  # swing notices and results for the opponent's at-bats aren't ours
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
    if not grade_loop.is_running():
        grade_loop.start()
    if not model_loop.is_running():
        model_loop.start()
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

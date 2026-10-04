"""Discord bot exposing Numberball scouting charts as slash commands.

Single-channel bot. Player resolution for /pitcher, /batter, /catcher
commands follows this precedence, mirroring how the Streamlit app's "Fetch
Live Matchup" mode drives its dropdowns but still allows a manual override:

  1. An explicit `name:` on the command - always wins, and becomes the new
     fallback for that role.
  2. The active game set via `/game set` - re-read live off its Google Sheet
     on every call, since who's pitching/batting/catching changes play to
     play.
  3. The last name used for that role (plain globals - this bot lives in one
     channel, so there's no per-channel/per-user state to track).

Requires DISCORD_BOT_TOKEN, plus the SUPABASE_URL / SUPABASE_KEY env vars
database.py already reads for non-Streamlit (CLI) contexts.
"""
from __future__ import annotations

import asyncio
import io
import os

import discord
from discord import app_commands
from discord.ext import commands

import bot_charts
import manager_calc
import scouting_data
import utils

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
# Optional: set this to your test server's id for instant command sync while
# developing (global sync can take up to an hour to show up). Leave unset for
# the real deployment, where commands should be available in any server.
TEST_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

ROLES = ("pitcher", "batter", "catcher")
_LOADERS = {
    "pitcher": scouting_data.load_pitcher_plays,
    "batter": scouting_data.load_batter_plays,
    "catcher": scouting_data.load_catcher_plays,
}

_active_game_code: int | None = None
_last_player: dict[str, str | None] = {r: None for r in ROLES}
_last_hot_zone = {"init_bucket": bot_charts.HOT_ZONE_DEFAULT_BUCKET,
                   "follow_bucket": bot_charts.HOT_ZONE_DEFAULT_BUCKET}


def _write_png(fig, buf) -> None:
    bot_charts.apply_dark_theme(fig)
    # fig.layout.width/height (set via update_layout) are silently ignored by
    # write_image unless passed explicitly here - without this, every figure
    # renders at kaleido's fixed default canvas regardless of what the chart
    # actually needs (most charts don't set an explicit size, so this only
    # changes behavior for ones that do, like the Manager strategy table).
    width, height = fig.layout.width, fig.layout.height
    if width and height:
        fig.write_image(buf, format="png", scale=2, width=width, height=height)
    else:
        fig.write_image(buf, format="png", scale=2)


async def _player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [n for n in scouting_data.all_player_names() if current in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


async def _run_chart_command(interaction: discord.Interaction, role: str, name: str | None,
                              builder, filename: str, **builder_kwargs) -> None:
    await interaction.response.defer()

    def _resolve_and_build():
        player = name or _last_player[role]
        if not name and _active_game_code is not None:
            live_name = scouting_data.resolve_game_roles(_active_game_code).get(role)
            if live_name:
                player = live_name
        if not player:
            return "no_player", None
        _last_player[role] = player

        df = _LOADERS[role](player)
        if df.empty:
            return "no_data", player
        fig = builder(df, **builder_kwargs)
        if fig is None:
            return "no_figs", player
        buf = io.BytesIO()
        _write_png(fig, buf)
        buf.seek(0)
        return "ok", (player, buf)

    status, payload = await asyncio.to_thread(_resolve_and_build)
    if status == "no_figs":
        await interaction.followup.send(f"Not enough data for **{payload}**.", ephemeral=True)
        return
    if status == "no_player":
        await interaction.followup.send(
            f"No {role} specified yet - include `name:`, or set an active game with `/game set`.", ephemeral=True)
        return
    if status == "no_data":
        await interaction.followup.send(f"No plays found for **{payload}**.", ephemeral=True)
        return
    player, buf = payload
    await interaction.followup.send(content=f"**{player}**", file=discord.File(buf, filename=filename))


async def _run_multi_chart_command(interaction: discord.Interaction, role: str, name: str | None,
                                    builder, **builder_kwargs) -> None:
    """Like _run_chart_command, but builder returns {filename: fig|None} and every
    non-None figure is sent as a separate attachment in one message."""
    await interaction.response.defer()

    def _resolve_and_build():
        player = name or _last_player[role]
        if not name and _active_game_code is not None:
            live_name = scouting_data.resolve_game_roles(_active_game_code).get(role)
            if live_name:
                player = live_name
        if not player:
            return "no_player", None
        _last_player[role] = player

        df = _LOADERS[role](player)
        if df.empty:
            return "no_data", player
        figs = builder(df, **builder_kwargs)
        files = []
        for fname, fig in (figs or {}).items():
            if fig is None:
                continue
            buf = io.BytesIO()
            _write_png(fig, buf)
            buf.seek(0)
            files.append((fname, buf))
        if not files:
            return "no_figs", player
        return "ok", (player, files)

    status, payload = await asyncio.to_thread(_resolve_and_build)
    if status == "no_player":
        await interaction.followup.send(
            f"No {role} specified yet - include `name:`, or set an active game with `/game set`.", ephemeral=True)
        return
    if status == "no_data":
        await interaction.followup.send(f"No plays found for **{payload}**.", ephemeral=True)
        return
    if status == "no_figs":
        await interaction.followup.send(f"Not enough data for **{payload}**.", ephemeral=True)
        return
    player, files = payload
    discord_files = [discord.File(buf, filename=fname) for fname, buf in files]
    await interaction.followup.send(content=f"**{player}**", files=discord_files)


def _radial_context_kwargs(prev_value, value_width, default_width, result_cat, leverage,
                            leverage_threshold, first_pitch_appearance, first_pitch_inning) -> dict:
    return dict(
        prev_value=prev_value, value_width=value_width or default_width,
        result_cat=result_cat, leverage=leverage, leverage_threshold=leverage_threshold,
        first_pitch_appearance=first_pitch_appearance, first_pitch_inning=first_pitch_inning,
    )


_value_bucket_choices = [app_commands.Choice(name=str(v), value=v) for v in bot_charts.VALUE_BUCKET_CHOICES]
_delta_bucket_choices = [app_commands.Choice(name=str(v), value=v) for v in bot_charts.DELTA_BUCKET_CHOICES]
_hot_zone_choices = [app_commands.Choice(name=str(v), value=v) for v in bot_charts.HOT_ZONE_BUCKET_CHOICES]
_seq_result_choices = [app_commands.Choice(name=c, value=c) for c in utils.SEQ_RESULT_CATEGORIES]
_steal_result_choices = [app_commands.Choice(name=c, value=c) for c in utils.STEAL_RESULT_CATEGORIES]
_leverage_choices = [app_commands.Choice(name="Low (< threshold)", value="low"),
                      app_commands.Choice(name="High (>= threshold)", value="high")]

_NAME_HELP = "Name (defaults to the active game's {role}, or the last one used)"
_CONTEXT_DESCRIBE = dict(
    prev_value="Filter: previous value", value_width="Bucket width around the previous value",
    result_cat="Filter: previous result category", leverage="Filter: previous play's leverage",
    leverage_threshold="Leverage threshold (default 1.5)",
    first_pitch_appearance="Filter: only the 1st of the appearance",
    first_pitch_inning="Filter: only the 1st of the half-inning",
)

pitcher_group = app_commands.Group(name="pitcher", description="Pitcher scouting charts")
batter_group = app_commands.Group(name="batter", description="Batter scouting charts")
catcher_group = app_commands.Group(name="catcher", description="Catcher scouting charts")
game_group = app_commands.Group(name="game", description="Tie scouting commands to a live game's active players")
manager_group = app_commands.Group(name="manager", description="Manager decision-support tools")


# ── pitcher ──────────────────────────────────────────────────────────────────

@pitcher_group.command(name="hotzone", description="Hot zone pitch matrix")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"),
                        init_bucket="Initial pitch bucket size (defaults to the last one used)",
                        follow_bucket="Following pitch bucket size (defaults to the last one used)")
@app_commands.choices(init_bucket=_hot_zone_choices, follow_bucket=_hot_zone_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_hotzone(interaction: discord.Interaction, name: str | None = None,
                           init_bucket: int | None = None, follow_bucket: int | None = None) -> None:
    if init_bucket is not None:
        _last_hot_zone["init_bucket"] = init_bucket
    if follow_bucket is not None:
        _last_hot_zone["follow_bucket"] = follow_bucket
    await _run_chart_command(interaction, "pitcher", name, bot_charts.hot_zone_fig, "hotzone.png", **_last_hot_zone)


@pitcher_group.command(name="zones", description="Pitch zone frequency")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_zones(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.zone_polar_fig, "zones.png")


@pitcher_group.command(name="shadow", description="Shadow |delta| vs prior diff")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_shadow(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.shadow_delta_fig, "shadow.png")


@pitcher_group.command(name="radial-pitches", description="Recent pitches, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), n="How many recent pitches to show", **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_value_bucket_choices, result_cat=_seq_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_radial_pitches(interaction: discord.Interaction, name: str | None = None,
                                  n: int = bot_charts.RADIAL_DEFAULT_N, prev_value: int | None = None,
                                  value_width: int | None = None, result_cat: str | None = None,
                                  leverage: str | None = None, leverage_threshold: float = 1.5,
                                  first_pitch_appearance: bool = False, first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.VALUE_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "pitcher", name, bot_charts.pitches_radial_fig,
                              "pitches_radial.png", n=n, **ctx)


@pitcher_group.command(name="radial-deltas", description="Recent pitch deltas, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), n="How many recent deltas to show",
                        center_on_prev="Map each delta onto the last actual pitch (implied next pitch)",
                        **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_delta_bucket_choices, result_cat=_seq_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_radial_deltas(interaction: discord.Interaction, name: str | None = None,
                                 n: int = bot_charts.RADIAL_DEFAULT_N, center_on_prev: bool = False,
                                 prev_value: int | None = None, value_width: int | None = None,
                                 result_cat: str | None = None, leverage: str | None = None,
                                 leverage_threshold: float = 1.5, first_pitch_appearance: bool = False,
                                 first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.DELTA_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "pitcher", name, bot_charts.deltas_radial_fig,
                              "deltas_radial.png", n=n, center_on_prev=center_on_prev, **ctx)


@pitcher_group.command(name="radial-delta2", description="Recent pitch delta-squareds, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), n="How many recent delta-squareds to show",
                        center_on_prev="Map each delta-squared onto the last actual pitch (implied next pitch)",
                        **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_delta_bucket_choices, result_cat=_seq_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_radial_delta2(interaction: discord.Interaction, name: str | None = None,
                                 n: int = bot_charts.RADIAL_DEFAULT_N, center_on_prev: bool = False,
                                 prev_value: int | None = None, value_width: int | None = None,
                                 result_cat: str | None = None, leverage: str | None = None,
                                 leverage_threshold: float = 1.5, first_pitch_appearance: bool = False,
                                 first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.DELTA_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "pitcher", name, bot_charts.delta2_radial_fig,
                              "delta2_radial.png", n=n, center_on_prev=center_on_prev, **ctx)


@pitcher_group.command(name="radial-combined", description="Pitches + deltas overlaid, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), n="How many recent points to show",
                        include_delta2="Also overlay implied-delta-squared points", **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_value_bucket_choices, result_cat=_seq_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_radial_combined(interaction: discord.Interaction, name: str | None = None,
                                   n: int = bot_charts.RADIAL_DEFAULT_N, include_delta2: bool = False,
                                   prev_value: int | None = None, value_width: int | None = None,
                                   result_cat: str | None = None, leverage: str | None = None,
                                   leverage_threshold: float = 1.5, first_pitch_appearance: bool = False,
                                   first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.VALUE_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "pitcher", name, bot_charts.combined_radial_fig,
                              "combined_radial.png", n=n, include_delta2=include_delta2, **ctx)


@pitcher_group.command(name="lastn", description="Last N pitches, combined chart")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), n="How many recent pitches to show",
                        show_swings="Also show the batter's swing values (default: pitches only)",
                        swing_offset="Shift swing markers right by one AB",
                        est_delta_overlay="Overlay the batter's estimated delta vs. prior pitch")
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_lastn(interaction: discord.Interaction, name: str | None = None,
                         n: int = bot_charts.RADIAL_DEFAULT_N, show_swings: bool = False,
                         swing_offset: bool = False, est_delta_overlay: bool = False) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.last_n_fig, "last_n.png",
                              n=n, show_swings=show_swings, swing_offset=swing_offset,
                              est_delta_overlay=est_delta_overlay)


@pitcher_group.command(name="delta-heatmap", description="Next pitch delta vs prior pitch delta")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), bucket="Bucket size")
@app_commands.choices(bucket=_delta_bucket_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_delta_heatmap(interaction: discord.Interaction, name: str | None = None,
                                 bucket: int = bot_charts.DELTA_DEFAULT_BUCKET) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.delta_heatmap_fig,
                              "delta_heatmap.png", bucket=bucket)


@pitcher_group.command(name="delta2-heatmap", description="Next pitch delta-squared vs prior delta-squared")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), bucket="Bucket size")
@app_commands.choices(bucket=_delta_bucket_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_delta2_heatmap(interaction: discord.Interaction, name: str | None = None,
                                  bucket: int = bot_charts.DELTA_DEFAULT_BUCKET) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.delta2_heatmap_fig,
                              "delta2_heatmap.png", bucket=bucket)


@pitcher_group.command(name="diff-delta-heatmap", description="Next pitch delta vs prior diff")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_diff_delta_heatmap(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.diff_delta_heatmap_fig, "diff_delta.png")


@pitcher_group.command(name="result-delta-heatmap", description="Next pitch delta vs prior result")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_result_delta_heatmap(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.result_delta_heatmap_fig, "result_delta.png")


@pitcher_group.command(name="result-sequence", description="Next pitch delta following a 2-PA result sequence")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), older="Result 2 plate appearances ago",
                        newer="Most recent plate appearance's result")
@app_commands.choices(older=_seq_result_choices, newer=_seq_result_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_result_sequence(interaction: discord.Interaction, older: str, newer: str,
                                   name: str | None = None) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.result_sequence_fig,
                              "result_sequence.png", older_cat=older, newer_cat=newer)


@pitcher_group.command(name="obr-gauge", description="Pitch frequency across a result range (e.g. an OBR)")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), low="Range low bound (1-1000)",
                        high="Range high bound (1-1000)", swing_value="A specific value to mark on the gauge",
                        bucket="Pitch bucket size", padding="Padding outside the range shown")
async def pitcher_obr_gauge(interaction: discord.Interaction, low: int, high: int, swing_value: int,
                             name: str | None = None, bucket: int = 10, padding: int = 100) -> None:
    await _run_chart_command(interaction, "pitcher", name, bot_charts.obr_gauge_fig, "obr_gauge.png",
                              obr_lo=low, obr_hi=high, swing_val=swing_value, bucket=bucket, padding=padding)


@pitcher_group.command(name="first-pitch", description="Zone on the first pitch of the appearance/inning")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_first_pitch(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "pitcher", name, bot_charts.first_pitch_figs)


@pitcher_group.command(name="zone-by-outs", description="Pitch zone frequency split by out count")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_zone_by_outs(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "pitcher", name, bot_charts.zone_by_outs_figs)


@pitcher_group.command(name="zone-by-base", description="Pitch zone frequency split by base state")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_zone_by_base(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "pitcher", name, bot_charts.zone_by_base_figs)


@pitcher_group.command(name="delta-distributions", description="Between-inning/game/AB pitch delta histograms")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), signed="Show signed (+/-) deltas vs. magnitude only")
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_delta_distributions(interaction: discord.Interaction, name: str | None = None,
                                       signed: bool = True) -> None:
    await _run_multi_chart_command(interaction, "pitcher", name, bot_charts.delta_distributions_figs, signed=signed)


@pitcher_group.command(name="cooldown", description="Pitch zone cooldown - overlay and open-cooldowns radial")
@app_commands.describe(name=_NAME_HELP.format(role="pitcher"), bucket="Bucket size (same as Hot Zone Pitch Matrix)")
@app_commands.choices(bucket=_hot_zone_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def pitcher_cooldown(interaction: discord.Interaction, name: str | None = None,
                            bucket: int = bot_charts.HOT_ZONE_DEFAULT_BUCKET) -> None:
    await _run_multi_chart_command(interaction, "pitcher", name, bot_charts.cooldown_figs, bucket=bucket)


# ── batter ───────────────────────────────────────────────────────────────────

@batter_group.command(name="hotzone", description="Hot zone swing matrix")
@app_commands.describe(name=_NAME_HELP.format(role="batter"),
                        init_bucket="Initial swing bucket size (defaults to the last one used)",
                        follow_bucket="Following swing bucket size (defaults to the last one used)")
@app_commands.choices(init_bucket=_hot_zone_choices, follow_bucket=_hot_zone_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_hotzone(interaction: discord.Interaction, name: str | None = None,
                          init_bucket: int | None = None, follow_bucket: int | None = None) -> None:
    if init_bucket is not None:
        _last_hot_zone["init_bucket"] = init_bucket
    if follow_bucket is not None:
        _last_hot_zone["follow_bucket"] = follow_bucket
    await _run_chart_command(interaction, "batter", name, bot_charts.batter_hot_zone_fig,
                              "hotzone.png", **_last_hot_zone)


@batter_group.command(name="zones", description="Swing zone frequency")
@app_commands.describe(name=_NAME_HELP.format(role="batter"))
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_zones(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "batter", name, bot_charts.batter_zone_polar_fig, "zones.png")


@batter_group.command(name="lastn", description="Last N swings, combined chart")
@app_commands.describe(name=_NAME_HELP.format(role="batter"), n="How many recent swings to show",
                        show_pitch="Also show the pitch values (default: swings only)",
                        swing_offset="Shift swing markers right by one AB")
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_lastn(interaction: discord.Interaction, name: str | None = None,
                        n: int = bot_charts.RADIAL_DEFAULT_N, show_pitch: bool = False,
                        swing_offset: bool = False) -> None:
    await _run_chart_command(interaction, "batter", name, bot_charts.batter_last_n_fig, "last_n.png",
                              n=n, show_pitch=show_pitch, swing_offset=swing_offset)


@batter_group.command(name="delta-heatmap", description="Next swing delta vs prior swing delta")
@app_commands.describe(name=_NAME_HELP.format(role="batter"), bucket="Bucket size")
@app_commands.choices(bucket=_delta_bucket_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_delta_heatmap(interaction: discord.Interaction, name: str | None = None,
                                bucket: int = bot_charts.DELTA_DEFAULT_BUCKET) -> None:
    await _run_chart_command(interaction, "batter", name, bot_charts.batter_delta_heatmap_fig,
                              "delta_heatmap.png", bucket=bucket)


@batter_group.command(name="diff-delta-heatmap", description="Next swing delta vs prior diff")
@app_commands.describe(name=_NAME_HELP.format(role="batter"))
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_diff_delta_heatmap(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "batter", name, bot_charts.batter_diff_delta_heatmap_fig, "diff_delta.png")


@batter_group.command(name="first-pitch", description="Swing zone on the first pitch of the appearance/inning")
@app_commands.describe(name=_NAME_HELP.format(role="batter"))
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_first_pitch(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "batter", name, bot_charts.batter_first_pitch_figs)


@batter_group.command(name="zone-by-outs", description="Swing zone frequency split by out count")
@app_commands.describe(name=_NAME_HELP.format(role="batter"))
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_zone_by_outs(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "batter", name, bot_charts.zone_by_outs_figs, zone_col="swing_zone")


@batter_group.command(name="zone-by-base", description="Swing zone frequency split by base state")
@app_commands.describe(name=_NAME_HELP.format(role="batter"))
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_zone_by_base(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_multi_chart_command(interaction, "batter", name, bot_charts.zone_by_base_figs, zone_col="swing_zone")


@batter_group.command(name="delta-distributions", description="Between-inning/game/AB swing delta histograms")
@app_commands.describe(name=_NAME_HELP.format(role="batter"), signed="Show signed (+/-) deltas vs. magnitude only")
@app_commands.autocomplete(name=_player_autocomplete)
async def batter_delta_distributions(interaction: discord.Interaction, name: str | None = None,
                                      signed: bool = True) -> None:
    await _run_multi_chart_command(interaction, "batter", name, bot_charts.batter_delta_distributions_figs,
                                    signed=signed)


# ── catcher ──────────────────────────────────────────────────────────────────

@catcher_group.command(name="hotzone", description="Hot zone throw matrix")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"),
                        init_bucket="Initial throw bucket size (defaults to the last one used)",
                        follow_bucket="Following throw bucket size (defaults to the last one used)")
@app_commands.choices(init_bucket=_hot_zone_choices, follow_bucket=_hot_zone_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_hotzone(interaction: discord.Interaction, name: str | None = None,
                           init_bucket: int | None = None, follow_bucket: int | None = None) -> None:
    if init_bucket is not None:
        _last_hot_zone["init_bucket"] = init_bucket
    if follow_bucket is not None:
        _last_hot_zone["follow_bucket"] = follow_bucket
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_hot_zone_fig,
                              "hotzone.png", **_last_hot_zone)


@catcher_group.command(name="zones", description="Throw zone frequency")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"))
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_zones(interaction: discord.Interaction, name: str | None = None) -> None:
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_zone_polar_fig, "zones.png")


@catcher_group.command(name="radial-throws", description="Recent throws, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), n="How many recent throws to show", **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_value_bucket_choices, result_cat=_steal_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_radial_throws(interaction: discord.Interaction, name: str | None = None,
                                 n: int = bot_charts.RADIAL_DEFAULT_N, prev_value: int | None = None,
                                 value_width: int | None = None, result_cat: str | None = None,
                                 leverage: str | None = None, leverage_threshold: float = 1.5,
                                 first_pitch_appearance: bool = False, first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.VALUE_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "catcher", name, bot_charts.throws_radial_fig,
                              "throws_radial.png", n=n, **ctx)


@catcher_group.command(name="radial-deltas", description="Recent throw deltas, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), n="How many recent deltas to show",
                        center_on_prev="Map each delta onto the last actual throw (implied next throw)",
                        **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_delta_bucket_choices, result_cat=_steal_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_radial_deltas(interaction: discord.Interaction, name: str | None = None,
                                 n: int = bot_charts.RADIAL_DEFAULT_N, center_on_prev: bool = False,
                                 prev_value: int | None = None, value_width: int | None = None,
                                 result_cat: str | None = None, leverage: str | None = None,
                                 leverage_threshold: float = 1.5, first_pitch_appearance: bool = False,
                                 first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.DELTA_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "catcher", name, bot_charts.throw_deltas_radial_fig,
                              "deltas_radial.png", n=n, center_on_prev=center_on_prev, **ctx)


@catcher_group.command(name="radial-combined", description="Throws + deltas overlaid, radial view")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), n="How many recent points to show", **_CONTEXT_DESCRIBE)
@app_commands.choices(value_width=_value_bucket_choices, result_cat=_steal_result_choices, leverage=_leverage_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_radial_combined(interaction: discord.Interaction, name: str | None = None,
                                   n: int = bot_charts.RADIAL_DEFAULT_N, prev_value: int | None = None,
                                   value_width: int | None = None, result_cat: str | None = None,
                                   leverage: str | None = None, leverage_threshold: float = 1.5,
                                   first_pitch_appearance: bool = False, first_pitch_inning: bool = False) -> None:
    ctx = _radial_context_kwargs(prev_value, value_width, bot_charts.VALUE_DEFAULT_BUCKET, result_cat, leverage,
                                  leverage_threshold, first_pitch_appearance, first_pitch_inning)
    await _run_chart_command(interaction, "catcher", name, bot_charts.throw_combined_radial_fig,
                              "combined_radial.png", n=n, **ctx)


@catcher_group.command(name="lastn", description="Last N throws, combined chart")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), n="How many recent throws to show",
                        show_steals="Also show the runner's steal values (default: throws only)",
                        swing_offset="Shift steal markers right by one attempt",
                        est_delta_overlay="Overlay the runner's estimated delta vs. prior throw")
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_lastn(interaction: discord.Interaction, name: str | None = None,
                         n: int = bot_charts.RADIAL_DEFAULT_N, show_steals: bool = False,
                         swing_offset: bool = False, est_delta_overlay: bool = False) -> None:
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_last_n_fig, "last_n.png",
                              n=n, show_steals=show_steals, swing_offset=swing_offset,
                              est_delta_overlay=est_delta_overlay)


@catcher_group.command(name="delta-heatmap", description="Next throw delta vs prior throw delta")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), bucket="Bucket size")
@app_commands.choices(bucket=_delta_bucket_choices)
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_delta_heatmap(interaction: discord.Interaction, name: str | None = None,
                                 bucket: int = bot_charts.DELTA_DEFAULT_BUCKET) -> None:
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_delta_heatmap_fig,
                              "delta_heatmap.png", bucket=bucket)


@catcher_group.command(name="safe-range-gauge", description="Throw frequency across a steal safe range")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), low="Range low bound (1-1000)",
                        high="Range high bound (1-1000)", steal_value="A specific value to mark on the gauge",
                        bucket="Throw bucket size", padding="Padding outside the range shown")
async def catcher_safe_range_gauge(interaction: discord.Interaction, low: int, high: int, steal_value: int,
                                    name: str | None = None, bucket: int = 10, padding: int = 100) -> None:
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_safe_range_gauge_fig,
                              "safe_range_gauge.png", safe_lo=low, safe_hi=high, steal_val=steal_value,
                              bucket=bucket, padding=padding)


@catcher_group.command(name="delta-distribution", description="Consecutive steal-attempt throw delta histogram")
@app_commands.describe(name=_NAME_HELP.format(role="catcher"), signed="Show signed (+/-) deltas vs. magnitude only")
@app_commands.autocomplete(name=_player_autocomplete)
async def catcher_delta_distribution(interaction: discord.Interaction, name: str | None = None,
                                      signed: bool = True) -> None:
    await _run_chart_command(interaction, "catcher", name, bot_charts.catcher_delta_distribution_fig,
                              "delta_distribution.png", signed=signed)


# ── game (drives the three roles above) ─────────────────────────────────────

@game_group.command(name="set", description="Set the active game - scouting commands default to its current players")
@app_commands.describe(game_code="Numberball game code (e.g. 130820)")
async def game_set(interaction: discord.Interaction, game_code: int) -> None:
    global _active_game_code
    await interaction.response.defer(ephemeral=True)
    roles = await asyncio.to_thread(scouting_data.resolve_game_roles, game_code)
    if not roles or not any(roles.values()):
        await interaction.followup.send(
            f"Couldn't read live player state for game `{game_code}` (no sheet on file, or its cells didn't "
            "match a known player). Active game not changed.", ephemeral=True)
        return
    _active_game_code = game_code
    lines = "\n".join(f"**{role.capitalize()}:** {n}" for role, n in roles.items() if n)
    await interaction.followup.send(
        f"Active game set to `{game_code}`. Scouting commands now default to:\n{lines}", ephemeral=True)


@game_group.command(name="clear", description="Stop tying scouting commands to a live game")
async def game_clear(interaction: discord.Interaction) -> None:
    global _active_game_code
    _active_game_code = None
    await interaction.response.send_message("Active game cleared.", ephemeral=True)


@game_group.command(name="status", description="Show the active game and its current players")
async def game_status(interaction: discord.Interaction) -> None:
    if _active_game_code is None:
        await interaction.response.send_message("No active game set.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    roles = await asyncio.to_thread(scouting_data.resolve_game_roles, _active_game_code)
    lines = "\n".join(f"**{role.capitalize()}:** {n or '(unresolved)'}" for role, n in roles.items())
    await interaction.followup.send(f"Active game: `{_active_game_code}`\n{lines}", ephemeral=True)


@manager_group.command(name="strategy", description="Expected value / win probability by strategy for a game")
@app_commands.describe(game_code="Numberball game code (defaults to the active game set via /game set)",
                        infield_in="Evaluate Hit-and-Run assuming the defense currently has infield in")
async def manager_strategy(interaction: discord.Interaction, game_code: int | None = None,
                            infield_in: bool = False) -> None:
    gid = game_code if game_code is not None else _active_game_code
    if gid is None:
        await interaction.response.send_message(
            "No game code given and no active game set - pass `game_code:` or run `/game set` first.", ephemeral=True)
        return
    await interaction.response.defer()

    def _build():
        state = scouting_data.resolve_game_state(gid)
        if not state:
            return None
        run_lookup = scouting_data.load_run_lookup()
        result = manager_calc.build_strategy_table(state, run_lookup, if_in_checked=infield_in)
        fig = bot_charts.strategy_table_fig(result)
        buf = io.BytesIO()
        _write_png(fig, buf)
        buf.seek(0)
        return buf

    buf = await asyncio.to_thread(_build)
    if buf is None:
        await interaction.followup.send(f"Couldn't read game state for `{gid}` (no sheet on file).", ephemeral=True)
        return
    await interaction.followup.send(file=discord.File(buf, filename="strategy.png"))


bot.tree.add_command(pitcher_group)
bot.tree.add_command(batter_group)
bot.tree.add_command(catcher_group)
bot.tree.add_command(game_group)
bot.tree.add_command(manager_group)


@bot.event
async def on_ready() -> None:
    if TEST_GUILD_ID:
        guild = discord.Object(id=int(TEST_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to test guild {TEST_GUILD_ID}")
    else:
        await bot.tree.sync()
        print("Synced commands globally (may take up to an hour to appear)")
    print(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)

"""Plotly figure builders for the Discord bot's scouting commands.

Thin wrappers around utils.* chart functions, matching the parameters and
defaults used in the Pitcher/Batter/Catcher tabs of pages/2_Scouting.py, so
the bot renders the same charts as the Streamlit app.

The app's "Context Filters" panel lets a user combine a previous-value
bucket, a previous-result category, a leverage bucket, and first-pitch
toggles, narrowing a Pitches/Deltas/Delta2 (or Throws/Deltas) radial
independently of one another. apply_prior_context() mirrors that filter for
ONE value family at a time (bucket_kind="pitch"/"delta"/"delta2") rather than
all three simultaneously, which keeps each slash command to one value+width
pair instead of three. The combined radial command narrows its pitches/delta
groups by the same shared context, but (unlike the app) doesn't accept a
separate bucket per group.
"""
from __future__ import annotations

import io
import os

import pandas as pd
import plotly.graph_objects as go
from PIL import Image, ImageOps

import scouting_data
import utils

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

HOT_ZONE_BUCKET_CHOICES = [50, 100, 125, 200, 250, 500]
HOT_ZONE_DEFAULT_BUCKET = 200

VALUE_BUCKET_CHOICES = [50, 100, 125, 200, 250, 500]
VALUE_DEFAULT_BUCKET = 200
DELTA_BUCKET_CHOICES = [25, 50, 100, 125, 250, 500]
DELTA_DEFAULT_BUCKET = 100

RADIAL_DEFAULT_N = 50

# Streamlit's default dark theme (.streamlit/config.toml: base = "dark", no
# color overrides besides primaryColor) - matched here so charts sent to
# Discord look like the app instead of kaleido's plain white default.
DARK_BG = "#0e1117"
DARK_SECONDARY_BG = "#262730"
DARK_TEXT = "#fafafa"
DARK_GRID = "#3b3d45"


def apply_dark_theme(fig: go.Figure) -> go.Figure:
    """Restyle a figure to Streamlit's dark theme before sending it to
    Discord. Safe to call on any figure - only touches layout-level colors
    (background, default font, axis/grid lines), not trace-level colors a
    chart set on purpose (a heatmap's colorscale, the strategy table's own
    palette, etc.)."""
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=DARK_BG,
        plot_bgcolor=DARK_BG,
        font_color=DARK_TEXT,
        legend=dict(font_color=DARK_TEXT),
    )
    fig.update_xaxes(gridcolor=DARK_GRID, zerolinecolor=DARK_GRID, linecolor=DARK_GRID, color=DARK_TEXT)
    fig.update_yaxes(gridcolor=DARK_GRID, zerolinecolor=DARK_GRID, linecolor=DARK_GRID, color=DARK_TEXT)
    # Polar subplots (radial/zone charts) keep their own bgcolor independent of
    # paper/plot_bgcolor - left alone they render Plotly's white default,
    # which clashes with everything else going dark. Streamlit's own theming
    # handles this for the app, but kaleido's static PNG export (what the bot
    # sends to Discord) never passes through that, so it needs doing here.
    fig.update_polars(bgcolor=DARK_SECONDARY_BG)
    return fig


def _last_value(df: pd.DataFrame, col: str) -> int | None:
    if df.empty or col not in df.columns:
        return None
    s = df.sort_values("id")[col].dropna()
    return int(s.iloc[-1]) if not s.empty else None


def apply_prior_context(
    df: pd.DataFrame, *, bucket_kind: str,
    prev_value: int | None = None, value_width: int | None = None,
    result_cat: str | None = None,
    leverage: str | None = None, leverage_threshold: float = 1.5,
    first_pitch_appearance: bool = False, first_pitch_inning: bool = False,
    pitch_col: str = "pitch", delta_col: str = "pitch_circ_delta",
    delta2_col: str = "pitch_circ_delta2_signed",
    shadow_delta_col: str = "pitch_shadow_delta_signed",
    fp_app_col: str = "is_fp_app", fp_inn_col: str = "is_fp_inn",
    result_category_fn=None,
) -> pd.DataFrame:
    """Narrow df to rows whose prior play matches the given context."""
    active = any([prev_value is not None, result_cat, leverage,
                  first_pitch_appearance, first_pitch_inning])
    if not active or df.empty:
        return df
    df = df.copy()
    if leverage is not None:
        df["_leverage"] = utils.compute_play_leverage(df)
    bucket_kwargs = {}
    if prev_value is not None:
        domain_hi, domain_lo = (1000, 1) if bucket_kind == "pitch" else (500, -500)
        bucket_kwargs[f"prev_{bucket_kind}_bucket"] = utils._centered_match_interval(
            prev_value, value_width, domain_hi=domain_hi, domain_lo=domain_lo)
    return utils.filter_by_prior_context(
        df, prev_result_cat=result_cat, leverage_bucket=leverage,
        leverage_threshold=leverage_threshold,
        first_pitch_appearance=first_pitch_appearance or None,
        first_pitch_inning=first_pitch_inning or None,
        pitch_col=pitch_col, delta_col=delta_col, delta2_col=delta2_col,
        shadow_delta_col=shadow_delta_col,
        fp_app_col=fp_app_col, fp_inn_col=fp_inn_col,
        result_category_fn=result_category_fn,
        **bucket_kwargs,
    )


# ── pitcher ──────────────────────────────────────────────────────────────────

def hot_zone_fig(df: pd.DataFrame, init_bucket: int = HOT_ZONE_DEFAULT_BUCKET,
                  follow_bucket: int = HOT_ZONE_DEFAULT_BUCKET) -> go.Figure:
    return utils.hot_zone_matrix(
        df, value_col="pitch", group_cols=["game_id", "pitcher_name"],
        init_bucket_size=init_bucket, follow_bucket_size=follow_bucket,
    )


def zone_polar_fig(df: pd.DataFrame) -> go.Figure:
    zone_counts = df["pitch_zone"].value_counts().to_dict()
    return utils.zone_polar(zone_counts, title="Pitch Zone Frequency")


def shadow_delta_fig(df: pd.DataFrame) -> go.Figure:
    return utils.shadow_delta_vs_prior_diff_heatmap(df, title="Shadow |Δ| vs Prior Diff")


def shadow_delta_result_fig(df: pd.DataFrame) -> go.Figure:
    return utils.shadow_delta_vs_prior_result_heatmap(df, title="Shadow |Δ| vs Prior Result")


def last_n_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, swing_offset: bool = False,
               est_delta_overlay: bool = False, show_swings: bool = False) -> go.Figure:
    return utils.last_n_combined_chart(df, n=n, delta_col="pitch", title=f"Last {n} Pitches",
                                        swing_offset=swing_offset, segment_games=True, pannable=False,
                                        est_delta_overlay=est_delta_overlay,
                                        show_value=True, show_opp=show_swings)


def delta_heatmap_fig(df: pd.DataFrame, bucket: int = DELTA_DEFAULT_BUCKET) -> go.Figure:
    return utils.next_delta_vs_prior_delta_heatmap(df, title="Next Pitch Δ vs Prior Pitch Δ",
                                                    value_col="pitch", bucket_size=bucket)


def delta2_heatmap_fig(df: pd.DataFrame, bucket: int = DELTA_DEFAULT_BUCKET) -> go.Figure:
    return utils.next_delta2_vs_prior_delta2_heatmap(df, title="Next Pitch Δ² vs Prior Pitch Δ²",
                                                      value_col="pitch", bucket_size=bucket)


def diff_delta_heatmap_fig(df: pd.DataFrame) -> go.Figure:
    return utils.diff_vs_next_pitch_delta_heatmap(df, title="Next Pitch Δ vs Prior Diff", value_col="pitch")


def result_delta_heatmap_fig(df: pd.DataFrame) -> go.Figure:
    return utils.next_pitch_delta_vs_prior_result_heatmap(df, title="Next Pitch Δ vs Prior Result",
                                                           value_col="pitch")


def result_sequence_fig(df: pd.DataFrame, older_cat: str, newer_cat: str) -> go.Figure | None:
    return utils.result_seq_delta_dist(df, "pitch", older_cat, newer_cat)


def obr_gauge_fig(df: pd.DataFrame, obr_lo: int, obr_hi: int, swing_val: int,
                   bucket: int = 10, padding: int = 100) -> go.Figure:
    vals = df[df["pitch"].notna()]["pitch"].astype(int).tolist()
    return utils.obr_gauge_donut(vals, obr_lo, obr_hi, swing_val, padding=padding, bucket_width=bucket)


def first_pitch_figs(df: pd.DataFrame) -> dict[str, go.Figure]:
    fpa = df[df["is_fp_app"] == True]
    fpi = df[df["is_fp_inn"] == True]
    return {
        "first_pitch_appearance.png": utils.zone_polar(
            fpa["pitch_zone"].value_counts().to_dict() if not fpa.empty else {}, title="First Pitch of Appearance"),
        "first_pitch_inning.png": utils.zone_polar(
            fpi["pitch_zone"].value_counts().to_dict() if not fpi.empty else {}, title="First Pitch of Inning"),
    }


def zone_by_outs_figs(df: pd.DataFrame, zone_col: str = "pitch_zone") -> dict[str, go.Figure]:
    figs = {}
    for oc in (0, 1, 2):
        dfo = df[df["outs"] == oc]
        counts = dfo[zone_col].value_counts().to_dict() if not dfo.empty else {}
        figs[f"{oc}_outs.png"] = utils.zone_polar(counts, title=f"{oc} Outs", compact=True)
    return figs


_BASE_STATE_GROUPS = [("Empty", ["000"]),
                       ("Runners On", ["001", "010", "100", "011", "101", "110", "111"])]


def zone_by_base_figs(df: pd.DataFrame, zone_col: str = "pitch_zone") -> dict[str, go.Figure]:
    figs = {}
    for label, obc_vals in _BASE_STATE_GROUPS:
        dfo = df[df["obc"].isin(obc_vals)]
        counts = dfo[zone_col].value_counts().to_dict() if not dfo.empty else {}
        figs[f"{label.lower().replace(' ', '_')}.png"] = utils.zone_polar(counts, title=label)
    return figs


def delta_distributions_figs(df: pd.DataFrame, signed: bool = True) -> dict[str, go.Figure]:
    figs = {}
    inn = utils.between_inning_deltas(df, value_col="pitch")
    if not inn.empty:
        figs["between_inning.png"] = utils.delta_histogram(inn, title="Between-Inning", signed=signed)
    game = utils.between_game_deltas(df, value_col="pitch")
    if not game.empty:
        figs["between_game.png"] = utils.delta_histogram(game, title="Between-Game", signed=signed)
    ab = df["pitch_circ_delta"].dropna() if "pitch_circ_delta" in df.columns else pd.Series(dtype=float)
    if not ab.empty:
        figs["previous_ab.png"] = utils.delta_histogram(ab, title="Previous AB", signed=signed)
    shadow = (df["pitch_shadow_delta_signed"].dropna() if "pitch_shadow_delta_signed" in df.columns
              else pd.Series(dtype=float))
    if not shadow.empty:
        figs["shadow.png"] = utils.delta_histogram(shadow, title="Shadow Δ (Prior Swing)", signed=signed)
    return figs


def strategy_table_fig(result: dict) -> go.Figure:
    """Render the Manager strategy table (manager_calc.build_strategy_table's
    output) as a styled image, matching every other chart this bot sends -
    nicer and more reliable across Discord themes than a monospace text table.
    """
    rows = result["rows"]
    n = len(rows)

    exp_wp_vals = [r["exp_wp"] for r in rows]
    decisions = [r["decision"] for r in rows]
    exp_wp = [f"{v * 100:.1f}%" if v is not None else "-" for v in exp_wp_vals]
    exp_runs = [f"{r['exp_runs']:.2f}" for r in rows]
    p1r = [f"{r['p1r'] * 100:.1f}%" for r in rows]
    p2r = [f"{r['p2r'] * 100:.1f}%" for r in rows]
    p3pr = [f"{r['p3pr'] * 100:.1f}%" for r in rows]

    # Highlight the best option: highest Exp WP when the WP table is loaded, else highest Exp Runs.
    if any(v is not None for v in exp_wp_vals):
        best_idx = max(range(n), key=lambda i: exp_wp_vals[i] if exp_wp_vals[i] is not None else -1.0)
    else:
        best_idx = max(range(n), key=lambda i: rows[i]["exp_runs"])

    sandbox_diffs = result.get("sandbox_diffs") or []

    row_colors = [("#1f3d2a" if i == best_idx else (DARK_SECONDARY_BG if i % 2 == 0 else DARK_BG))
                  for i in range(n)]
    decision_text = []
    for i, d in enumerate(decisions):
        label = f"{d} *" if d == "Sandbox" and sandbox_diffs else d
        decision_text.append(f"<b>{label} ★</b>" if i == best_idx else label)

    header_vals = ["Decision", "Exp WP", "Exp Runs", "P(1R)", "P(2R)", "P(3+R)"]
    cell_vals = [decision_text, exp_wp, exp_runs, p1r, p2r, p3pr]
    col_widths = [150, 70, 80, 60, 60, 65]
    align = ["left"] + ["center"] * 5

    fig = go.Figure(data=[go.Table(
        columnwidth=col_widths,
        header=dict(values=header_vals, fill_color="#085d05", font=dict(color=DARK_TEXT, size=13), align=align,
                    height=32, line_color="#085d05"),
        cells=dict(values=cell_vals, fill_color=[row_colors] * len(cell_vals), align=align,
                   font=dict(size=13, color=DARK_TEXT), height=30, line_color=DARK_GRID),
    )])

    pitcher = result.get("pitcher") or "?"
    batter = result.get("batter") or "?"
    subtitle = [f"Outs: {result['outs']}", f"OBC: {utils.obc_display(result['obc'])}"]
    if result.get("current_wp") is not None:
        subtitle.append(f"WP: {result['current_wp'] * 100:.1f}%")
    if result.get("leverage") is not None:
        subtitle.append(f"Leverage: {result['leverage']:.2f}")
    subtitle.append(f"Baseline ER: {result['baseline_er']:.2f}")

    footnote_height = 0
    if sandbox_diffs:
        footnote_height = 26
        fig.add_annotation(
            text=f"* Sandbox differs from Normal Swing: {', '.join(sandbox_diffs)}",
            xref="paper", yref="paper", x=0.01, y=0, xanchor="left", yanchor="top",
            showarrow=False, font=dict(size=11, color="#9aa0a6"),
        )

    fig.update_layout(
        title=dict(text=f"{pitcher} vs {batter}<br>"
                        f"<span style='font-size:12px;color:#9aa0a6'>{'  ·  '.join(subtitle)}</span>",
                   x=0.03, xanchor="left"),
        margin=dict(l=10, r=10, t=64, b=10 + footnote_height),
        width=sum(col_widths) + 40,
        height=100 + 30 * (n + 1) + footnote_height,
    )
    return fig


def cooldown_figs(df: pd.DataFrame, bucket: int = HOT_ZONE_DEFAULT_BUCKET,
                   by_category: bool = False) -> dict[str, go.Figure]:
    radius = bucket / 2
    events = utils.cooldown_events(df, value_col="pitch", radius=radius)
    if events.empty:
        return {}
    figs = {
        "cooldown_overlay.png": utils.cooldown_cdf_overlay_chart(events),
        "cooldown_radial.png": utils.cooldown_radial_chart(events, radius=radius),
    }
    if by_category:
        for cat in utils.SEQ_RESULT_CATEGORIES:
            cat_events = events[events["category"] == cat]
            resolved = cat_events.loc[cat_events["resolved"], "pitches"]
            live = cat_events.loc[~cat_events["resolved"]]
            if not resolved.empty or not live.empty:
                figs[f"cooldown_{cat}.png"] = utils.cooldown_cdf_chart(resolved, title=cat, live=live)
    return figs


def obr_bounds(swing_val: int, result_ranges: list, obr_extra: frozenset = frozenset()) -> tuple[int, int] | None:
    """(obr_lo, obr_hi) pitch-domain bounds of the On-Base Range for a swing
    value - the OBR radius is the largest hi-distance among XBH/BB-1B (plus
    obr_extra, e.g. SacF/DSacF/GORA) categories, applied symmetrically around
    swing_val on the 1-1000 wheel. Mirrors the Swing Suggestions panel."""
    obr_cats = utils._OBR | obr_extra
    radius = max((hi for r, _lo, hi in result_ranges if r in obr_cats), default=0)
    if radius <= 0:
        return None
    lo = ((swing_val - radius - 1) % 1000) + 1
    hi = ((swing_val + radius - 1) % 1000) + 1
    return lo, hi


def sacf_bounds(swing_val: int, result_ranges: list, obr_radius: int) -> tuple[int, int] | None:
    """(sacf_lo, sacf_hi) - the wider Sac Fly/DSacF range as a second ring
    outside OBR, independent of extend_sacf (which folds SacF into the core
    OBR band itself rather than showing it separately). Only returned when
    it actually extends further than obr_radius already does - redundant
    otherwise, whether because extend_sacf already folded it in or there's
    no SacF/DSacF entry in result_ranges at all. Mirrors the Streamlit page's
    own _sacf_max_p > _obr_max_p gate."""
    radius = max((hi for r, _lo, hi in result_ranges if r in ("SacF", "DSacF")), default=0)
    if radius <= obr_radius:
        return None
    lo = ((swing_val - radius - 1) % 1000) + 1
    hi = ((swing_val + radius - 1) % 1000) + 1
    return lo, hi


def pitches_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, obr: tuple[int, int] | None = None,
                        sacf: tuple[int, int] | None = None, **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="pitch", **context_kwargs)
    n_actual = min(n, int(df_f["pitch"].notna().sum())) if "pitch" in df_f.columns else 0
    obr_lo, obr_hi = obr if obr is not None else (None, None)
    sacf_lo, sacf_hi = sacf if sacf is not None else (None, None)
    return utils.radial_recent_pitches_chart(df_f, n=n, value_col="pitch", title=f"Last {n_actual} Pitches",
                                              obr_lo=obr_lo, obr_hi=obr_hi, sacf_lo=sacf_lo, sacf_hi=sacf_hi)


def deltas_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                       obr: tuple[int, int] | None = None, sacf: tuple[int, int] | None = None,
                       **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="delta", **context_kwargs)
    n_actual = min(n, int(df_f["pitch_circ_delta"].notna().sum())) if "pitch_circ_delta" in df_f.columns else 0
    anchor = _last_value(df, "pitch")
    title = f"Last {n_actual} Implied Pitches" if center_on_prev else f"Last {n_actual} Deltas"
    obr_lo, obr_hi = obr if obr is not None else (None, None)
    sacf_lo, sacf_hi = sacf if sacf is not None else (None, None)
    return utils.radial_recent_deltas_chart(df_f, n=n, delta_col="pitch_circ_delta", value_col="pitch",
                                             title=title, center_on_prev=center_on_prev, anchor=anchor,
                                             obr_lo=obr_lo, obr_hi=obr_hi, sacf_lo=sacf_lo, sacf_hi=sacf_hi)


def delta2_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                       obr: tuple[int, int] | None = None, sacf: tuple[int, int] | None = None,
                       **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="delta2", **context_kwargs)
    n_actual = (min(n, int(df_f["pitch_circ_delta2_signed"].notna().sum()))
                if "pitch_circ_delta2_signed" in df_f.columns else 0)
    anchor = _last_value(df, "pitch")
    anchor_delta = _last_value(df, "pitch_circ_delta")
    title = f"Last {n_actual} Implied Pitches (Δ²)" if center_on_prev else f"Last {n_actual} Delta²s"
    obr_lo, obr_hi = obr if obr is not None else (None, None)
    sacf_lo, sacf_hi = sacf if sacf is not None else (None, None)
    return utils.radial_recent_delta2_chart(df_f, n=n, delta2_col="pitch_circ_delta2_signed",
                                             delta_col="pitch_circ_delta", value_col="pitch",
                                             title=title, center_on_prev=center_on_prev,
                                             anchor=anchor, anchor_delta=anchor_delta,
                                             obr_lo=obr_lo, obr_hi=obr_hi, sacf_lo=sacf_lo, sacf_hi=sacf_hi)


def shadow_delta_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                             obr: tuple[int, int] | None = None, sacf: tuple[int, int] | None = None,
                             **context_kwargs) -> go.Figure:
    # center_on_prev anchors on the batter's last actual SWING, not the
    # pitcher's last pitch like deltas/delta2_radial_fig do - swing + shadow
    # delta is exactly how the upcoming pitch is estimated (see enrich_df's
    # pitch_shadow_delta_signed), so "map onto previous X" means a different
    # anchor column here than it does for those two.
    df_f = apply_prior_context(df, bucket_kind="shadow_delta", **context_kwargs)
    n_actual = (min(n, int(df_f["pitch_shadow_delta_signed"].notna().sum()))
                if "pitch_shadow_delta_signed" in df_f.columns else 0)
    anchor = _last_value(df, "swing")
    title = f"Last {n_actual} Implied Pitches (Shadow)" if center_on_prev else f"Last {n_actual} Shadow Δs"
    obr_lo, obr_hi = obr if obr is not None else (None, None)
    sacf_lo, sacf_hi = sacf if sacf is not None else (None, None)
    return utils.radial_recent_deltas_chart(df_f, n=n, delta_col="pitch_shadow_delta_signed", value_col="swing",
                                             title=title, center_on_prev=center_on_prev, anchor=anchor,
                                             obr_lo=obr_lo, obr_hi=obr_hi, sacf_lo=sacf_lo, sacf_hi=sacf_hi)


def combined_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, include_delta2: bool = False,
                         obr: tuple[int, int] | None = None, sacf: tuple[int, int] | None = None,
                         **context_kwargs) -> go.Figure:
    df_pitches = apply_prior_context(df, bucket_kind="pitch", **context_kwargs)
    df_deltas = apply_prior_context(df, bucket_kind="delta", **context_kwargs)
    df_delta2 = apply_prior_context(df, bucket_kind="delta2", **context_kwargs) if include_delta2 else None
    anchor = _last_value(df, "pitch")
    anchor_delta = _last_value(df, "pitch_circ_delta")
    n_pitches = min(n, int(df_pitches["pitch"].notna().sum())) if "pitch" in df_pitches.columns else 0
    n_deltas = (min(n, int(df_deltas["pitch_circ_delta"].notna().sum()))
                if "pitch_circ_delta" in df_deltas.columns else 0)
    n_delta2 = (min(n, int(df_delta2["pitch_circ_delta2_signed"].notna().sum()))
                if include_delta2 and df_delta2 is not None and "pitch_circ_delta2_signed" in df_delta2.columns
                else 0)
    n_actual = max(n_pitches, n_deltas, n_delta2)
    obr_lo, obr_hi = obr if obr is not None else (None, None)
    sacf_lo, sacf_hi = sacf if sacf is not None else (None, None)
    return utils.radial_combined_chart(df_pitches, df_deltas, n=n, value_col="pitch",
                                        delta_col="pitch_circ_delta", title=f"Last {n_actual} Combined",
                                        anchor=anchor, df_delta2=df_delta2, delta2_col="pitch_circ_delta2_signed",
                                        anchor_delta=anchor_delta, include_delta2=include_delta2,
                                        obr_lo=obr_lo, obr_hi=obr_hi, sacf_lo=sacf_lo, sacf_hi=sacf_hi)


# ── batter ───────────────────────────────────────────────────────────────────

def batter_hot_zone_fig(df: pd.DataFrame, init_bucket: int = HOT_ZONE_DEFAULT_BUCKET,
                         follow_bucket: int = HOT_ZONE_DEFAULT_BUCKET) -> go.Figure:
    return utils.hot_zone_matrix(
        df, value_col="swing", group_cols=["game_id", "batter_name"],
        init_bucket_size=init_bucket, follow_bucket_size=follow_bucket,
    )


def batter_zone_polar_fig(df: pd.DataFrame) -> go.Figure:
    zone_counts = df["swing_zone"].value_counts().to_dict()
    return utils.zone_polar(zone_counts, title="Swing Zone Frequency")


def batter_last_n_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, swing_offset: bool = False,
                       show_pitch: bool = False) -> go.Figure:
    return utils.last_n_combined_chart(df, n=n, delta_col="swing", title=f"Last {n} Swings",
                                        swing_offset=swing_offset, pannable=False,
                                        show_value=show_pitch, show_opp=True)


def batter_delta_heatmap_fig(df: pd.DataFrame, bucket: int = DELTA_DEFAULT_BUCKET) -> go.Figure:
    return utils.next_delta_vs_prior_delta_heatmap(df, title="Next Swing Δ vs Prior Swing Δ",
                                                    value_col="swing", bucket_size=bucket)


def batter_diff_delta_heatmap_fig(df: pd.DataFrame) -> go.Figure:
    return utils.diff_vs_next_pitch_delta_heatmap(df, title="Next Swing Δ vs Prior Diff", value_col="swing")


def batter_first_pitch_figs(df: pd.DataFrame) -> dict[str, go.Figure]:
    fpa = df[df["is_fp_app"] == True]
    fpi = df[df["is_fp_inn"] == True]
    return {
        "first_pitch_appearance.png": utils.zone_polar(
            fpa["swing_zone"].value_counts().to_dict() if not fpa.empty else {}, title="First Pitch of Appearance"),
        "first_pitch_inning.png": utils.zone_polar(
            fpi["swing_zone"].value_counts().to_dict() if not fpi.empty else {}, title="First Pitch of Inning"),
    }


def batter_delta_distributions_figs(df: pd.DataFrame, signed: bool = True) -> dict[str, go.Figure]:
    figs = {}
    inn = utils.between_inning_deltas(df, value_col="swing")
    if not inn.empty:
        figs["between_inning.png"] = utils.delta_histogram(inn, title="Between-Inning", signed=signed)
    game = utils.between_game_deltas(df, value_col="swing")
    if not game.empty:
        figs["between_game.png"] = utils.delta_histogram(game, title="Between-Game", signed=signed)
    ab = df["swing_circ_delta"].dropna() if "swing_circ_delta" in df.columns else pd.Series(dtype=float)
    if not ab.empty:
        figs["previous_ab.png"] = utils.delta_histogram(ab, title="Previous AB", signed=signed)
    return figs


# ── catcher ──────────────────────────────────────────────────────────────────

_CATCHER_CONTEXT_DEFAULTS = dict(
    fp_app_col="is_ft_app", fp_inn_col="is_ft_inn",
    result_category_fn=utils.steal_result_category,
)


def catcher_hot_zone_fig(df: pd.DataFrame, init_bucket: int = HOT_ZONE_DEFAULT_BUCKET,
                          follow_bucket: int = HOT_ZONE_DEFAULT_BUCKET) -> go.Figure:
    return utils.hot_zone_matrix(
        df, value_col="throw_num", group_cols=["catcher_name"], title="Hot Zone Throw Matrix",
        init_bucket_size=init_bucket, follow_bucket_size=follow_bucket,
    )


def catcher_zone_polar_fig(df: pd.DataFrame) -> go.Figure:
    zone_counts = df["throw_zone"].value_counts().to_dict()
    return utils.zone_polar(zone_counts, title="Throw Zone Frequency")


def throws_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, **context_kwargs) -> go.Figure:
    ctx = {"pitch_col": "throw_num", **_CATCHER_CONTEXT_DEFAULTS, **context_kwargs}
    df_f = apply_prior_context(df, bucket_kind="pitch", **ctx)
    n_actual = min(n, int(df_f["throw_num"].notna().sum())) if "throw_num" in df_f.columns else 0
    return utils.radial_recent_pitches_chart(df_f, n=n, value_col="throw_num", title=f"Last {n_actual} Throws")


def throw_deltas_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                             **context_kwargs) -> go.Figure:
    ctx = {"delta_col": "throw_num_circ_delta", **_CATCHER_CONTEXT_DEFAULTS, **context_kwargs}
    df_f = apply_prior_context(df, bucket_kind="delta", **ctx)
    n_actual = (min(n, int(df_f["throw_num_circ_delta"].notna().sum()))
                if "throw_num_circ_delta" in df_f.columns else 0)
    anchor = _last_value(df, "throw_num")
    title = f"Last {n_actual} Implied Throws" if center_on_prev else f"Last {n_actual} Deltas"
    return utils.radial_recent_deltas_chart(df_f, n=n, delta_col="throw_num_circ_delta", value_col="throw_num",
                                             title=title, center_on_prev=center_on_prev, anchor=anchor)


def throw_combined_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, **context_kwargs) -> go.Figure:
    df_throws = apply_prior_context(df, bucket_kind="pitch",
                                     **{"pitch_col": "throw_num", **_CATCHER_CONTEXT_DEFAULTS, **context_kwargs})
    df_deltas = apply_prior_context(df, bucket_kind="delta",
                                     **{"delta_col": "throw_num_circ_delta", **_CATCHER_CONTEXT_DEFAULTS,
                                        **context_kwargs})
    anchor = _last_value(df, "throw_num")
    n_throws = min(n, int(df_throws["throw_num"].notna().sum())) if "throw_num" in df_throws.columns else 0
    n_deltas = (min(n, int(df_deltas["throw_num_circ_delta"].notna().sum()))
                if "throw_num_circ_delta" in df_deltas.columns else 0)
    n_actual = max(n_throws, n_deltas)
    return utils.radial_combined_chart(df_throws, df_deltas, n=n, value_col="throw_num",
                                        delta_col="throw_num_circ_delta", title=f"Last {n_actual} Combined",
                                        anchor=anchor)


def catcher_last_n_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, swing_offset: bool = False,
                        est_delta_overlay: bool = False, show_steals: bool = False) -> go.Figure:
    return utils.last_n_combined_chart(df, n=n, value_col="throw_num", opp_col="steal_num",
                                        delta_col="throw_num", title=f"Last {n} Throws",
                                        swing_offset=swing_offset, segment_games=True, pannable=False,
                                        est_delta_overlay=est_delta_overlay,
                                        value_label="Throw", opp_label="Steal", pa_label="Attempt",
                                        show_value=True, show_opp=show_steals)


def catcher_delta_heatmap_fig(df: pd.DataFrame, bucket: int = DELTA_DEFAULT_BUCKET) -> go.Figure:
    return utils.next_delta_vs_prior_delta_heatmap(df, title="Next Throw Δ vs Prior Throw Δ",
                                                    value_col="throw_num", bucket_size=bucket,
                                                    delta_col="throw_num_circ_delta", group_cols=["catcher_name"])


def catcher_safe_range_gauge_fig(df: pd.DataFrame, safe_lo: int, safe_hi: int, steal_val: int,
                                  bucket: int = 10, padding: int = 100) -> go.Figure:
    vals = df[df["throw_num"].notna()]["throw_num"].astype(int).tolist()
    return utils.obr_gauge_donut(vals, safe_lo, safe_hi, steal_val, padding=padding, bucket_width=bucket,
                                  title="Safe Range Throw Frequency")


def catcher_delta_distribution_fig(df: pd.DataFrame, signed: bool = True) -> go.Figure | None:
    ab = df["throw_num_circ_delta"].dropna() if "throw_num_circ_delta" in df.columns else pd.Series(dtype=float)
    if ab.empty:
        return None
    return utils.delta_histogram(ab, title="Previous AB", signed=signed)


# ── pitcher: percentile card / tendencies over time / sequence viewer / swing analyzer ──

def percentile_card_fig(df: pd.DataFrame, pitcher_name: str, player_id: int | None,
                         stats_df: pd.DataFrame, ma_percentiles: dict, recent_n: int = 50) -> go.Figure | None:
    recent_df = df.sort_values("id").tail(recent_n)
    recent_stats = utils.compute_recent_pitcher_stats(recent_df)
    recent_n_actual = int(recent_df["swing"].notna().sum())
    return utils.pitcher_percentile_card(
        pitcher_name, stats_df,
        recent_vals=recent_stats if recent_stats else None,
        recent_n=recent_n_actual if recent_stats else None,
        player_id=player_id,
        ma_percentiles=ma_percentiles if ma_percentiles else None,
    )


MA_METRIC_CHOICES = list(utils._MA_METRICS.keys())
MA_METRIC_LABELS = {k: v["label"] for k, v in utils._MA_METRICS.items()}


def tendencies_over_time_fig(df: pd.DataFrame, metric: str = "avg_delta") -> go.Figure | None:
    return utils.pitcher_ma_figure(df, metric)


_SEQ_DOMAIN_SPECS = {
    "pitch": dict(col="pitch", hist_col="pitch", abs_val=False, y_range=(1, 1000), y_label="Pitch value",
                  domain="value", default_bucket=HOT_ZONE_DEFAULT_BUCKET),
    "delta": dict(col="pitch_circ_delta", hist_col="pitch_circ_delta", abs_val=True, y_range=(0, 500),
                  y_label="|Δ|", domain="delta", default_bucket=DELTA_DEFAULT_BUCKET),
    "delta2": dict(col="pitch_circ_delta2", hist_col="pitch_circ_delta2", abs_val=True, y_range=(0, 500),
                   y_label="|Δ²|", domain="delta2", default_bucket=DELTA_DEFAULT_BUCKET),
}


def sequence_viewer_fig(df: pd.DataFrame, domain: str = "pitch", match_last: int = 1,
                         bucket: int | None = None) -> go.Figure | None:
    """Pitcher Sequence Viewer: historical 4-point paths matching the pitcher's
    own most recent 1-2 values in the given domain (pitch value, |Δ|, or |Δ²|)."""
    spec = _SEQ_DOMAIN_SPECS[domain]
    bucket = bucket or spec["default_bucket"]
    if df.empty or spec["col"] not in df.columns:
        return None
    hist = df[df[spec["col"]].notna()].sort_values("id")[spec["col"]]
    hist = (hist.abs() if spec["abs_val"] else hist).astype(int).tolist()
    if not hist:
        return None
    prior_val = hist[-1]
    prior_val2 = hist[-2] if len(hist) >= 2 and match_last == 2 else None
    res = utils.sequence_matches(df, "pitch", bucket, prior_val, prior_val2=prior_val2, domain=spec["domain"])
    if not res:
        return None
    mode_note = "matched on last 2 values" if match_last == 2 else None
    return utils.sequence_viewer_figure(
        res["matches"], hist[-3:], spec["y_range"], spec["y_label"], bucket,
        selected_bin=None, mode_note=mode_note, mobile=False,
    )


def swing_analyzer_fig(df: pd.DataFrame, swing_value: int, n: int = 50,
                        result_ranges: list | None = None, obr_extra: frozenset = frozenset()) -> go.Figure:
    pa_df = df[df["pitch"].notna()].sort_values("id").tail(n)
    return utils.swing_predictor_chart(
        pa_df, swing=swing_value, n=n, result_ranges=result_ranges,
        tick_label=f"Last {n} pitches", obr_extra=obr_extra,
    )


_OPTIMAL_SWING_BASES = {"values", "delta", "delta2"}


def optimal_swing_fig(df: pd.DataFrame, n: int = 50, metric: str = "obp", basis: str = "values",
                       result_ranges: list | None = None, obr_extra: frozenset = frozenset()) -> go.Figure | None:
    """Optimal Swing: expected-score curve over every possible swing value,
    built from the pitcher's recent pitches (basis="values"), or their
    implied-next-pitch projections from recent deltas/delta-squareds."""
    recent = df[df["pitch"].notna()].sort_values("id").tail(n)["pitch"].astype(int).tolist()
    if not recent:
        return None
    if basis == "delta":
        vals = utils.project_from_deltas(recent)
    elif basis == "delta2":
        vals = utils.project_from_delta2s(recent)
    else:
        vals = recent
    if not vals:
        return None
    ranges = result_ranges or utils.RESULT_RANGES
    return utils.optimal_swing_chart(vals, ranges, metric, True, obr_extra=obr_extra)


def tendencies_text(df: pd.DataFrame, value_col: str = "pitch", last2_col: str | None = "pitch_last2",
                     label: str = "Pitches") -> str:
    total = len(df)
    meme_counts = {str(n): int((df[value_col] == n).sum()) for n in utils.MEME_NUMBERS}
    meme_total = sum(meme_counts.values())
    lines = [f"**Meme {label}** ({meme_total}{f' - {meme_total/total*100:.1f}%' if total else ''} of all {label.lower()})"]
    for num, cnt in meme_counts.items():
        pct = f" ({cnt/total*100:.1f}%)" if total else ""
        lines.append(f"  **{num}**: {cnt}{pct}")
    lines.append("")
    lines.append("**Most Common Last 2 Digits**")
    if last2_col and last2_col in df.columns:
        last2 = df[last2_col].value_counts().head(5)
    elif value_col in df.columns:
        last2 = df[value_col].dropna().apply(lambda v: int(str(int(v)).zfill(2)[-2:])).value_counts().head(5)
    else:
        last2 = pd.Series(dtype=int)
    for dig, cnt in last2.items():
        pct = f" ({cnt/total*100:.1f}%)" if total else ""
        lines.append(f"  **{int(dig):02d}**: {cnt}{pct}")
    return "\n".join(lines)


def last2_digit_radial_fig(df: pd.DataFrame) -> go.Figure:
    baseline = scouting_data.load_last2_digit_baseline()["pitch"]
    reference = scouting_data.load_last2_digit_reference()["pitch"]
    # Discord's default UI (and most users') is dark, and there's no theme
    # signal to read like Streamlit's st.context.theme - light text reads
    # safely for the overwhelming majority of viewers.
    return utils.last2_digit_radial_chart(df, value_col="pitch", last2_col="pitch_last2",
                                           title="Last 2 Digits", count_label="Pitches",
                                           baseline_probs=baseline, reference=reference, dark_mode=True)


def batter_last2_digit_radial_fig(df: pd.DataFrame) -> go.Figure:
    baseline = scouting_data.load_last2_digit_baseline()["swing"]
    reference = scouting_data.load_last2_digit_reference()["swing"]
    return utils.last2_digit_radial_chart(df, value_col="swing", last2_col=None,
                                           title="Last 2 Digits", count_label="Swings",
                                           baseline_probs=baseline, reference=reference, dark_mode=True)


# ── league ───────────────────────────────────────────────────────────────────

# Column layout for draft_order_fig, exposed so discord_bot.py's logo-paste
# step can compute each row's pixel position from the same numbers the table
# itself was drawn with - column 3 ("") is left blank on purpose, reserved
# as a slot for the team logo pasted in after rendering (go.Table cells are
# text-only, no inline images).
DRAFT_ORDER_COL_WIDTHS = [46, 52, 62, 30, 190, 230, 62, 55]
DRAFT_ORDER_LOGO_COL = 3
DRAFT_ORDER_ROW_HEIGHT = 20
DRAFT_ORDER_HEADER_HEIGHT = 30
DRAFT_ORDER_MARGIN = dict(l=8, r=8, t=52, b=8)
DRAFT_ORDER_SCALE = 1.5


def _fmt_run_diff(rs: int, ra: int) -> str:
    diff = rs - ra
    return f"+{diff}" if diff > 0 else str(diff)


def draft_order_fig(rows: list[dict], teams_by_abbrev: dict[str, dict], title: str = "Draft Order") -> go.Figure:
    """Styled Round/Pick/Overall/Team/Notes/Record/Diff table for
    draft_order.compute_draft_order's output - one row per pick, matching
    every other table this bot sends (see strategy_table_fig). Team name text
    only; discord_bot.py pastes each row's logo into the blank column after
    this renders, using the layout constants above.

    Record/Diff are the ORIGINAL team's (the standing that earned the slot,
    not whoever the pick was traded to) - that's what explains the sort, and
    cycling through all 16 teams once per round means every team's record
    shows up somewhere in the table.
    """
    n = len(rows)
    team_name = lambda ab: (teams_by_abbrev.get(ab) or {}).get("full_team") or ab

    def _record(ab: str) -> str:
        t = teams_by_abbrev.get(ab) or {}
        return f"{t.get('wins') or 0}-{t.get('losses') or 0}"

    def _diff(ab: str) -> str:
        t = teams_by_abbrev.get(ab) or {}
        return _fmt_run_diff(t.get("runs_scored") or 0, t.get("runs_allowed") or 0)

    header_vals = ["Rd", "Pick", "Overall", "", "Team", "Notes", "Record", "Diff"]
    cell_vals = [
        [str(r["round"]) for r in rows],
        [str(r["pick"]) for r in rows],
        [str(r["overall"]) for r in rows],
        [""] * n,
        [team_name(r["team"]) for r in rows],
        [r["notes"] for r in rows],
        [_record(r["original_team"]) for r in rows],
        [_diff(r["original_team"]) for r in rows],
    ]
    align = ["center", "center", "center", "center", "left", "left", "center", "center"]
    row_colors = [DARK_SECONDARY_BG if i % 2 == 0 else DARK_BG for i in range(n)]

    fig = go.Figure(data=[go.Table(
        columnwidth=DRAFT_ORDER_COL_WIDTHS,
        header=dict(values=header_vals, fill_color="#085d05", font=dict(color=DARK_TEXT, size=12), align=align,
                    height=DRAFT_ORDER_HEADER_HEIGHT, line_color="#085d05"),
        cells=dict(values=cell_vals, fill_color=[row_colors] * len(cell_vals), align=align,
                   font=dict(size=11, color=DARK_TEXT), height=DRAFT_ORDER_ROW_HEIGHT, line_color=DARK_GRID),
    )])

    width = sum(DRAFT_ORDER_COL_WIDTHS) + DRAFT_ORDER_MARGIN["l"] + DRAFT_ORDER_MARGIN["r"]
    height = (DRAFT_ORDER_MARGIN["t"] + DRAFT_ORDER_HEADER_HEIGHT
              + DRAFT_ORDER_ROW_HEIGHT * n + DRAFT_ORDER_MARGIN["b"])
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center", font=dict(size=14)),
        margin=DRAFT_ORDER_MARGIN,
        width=width, height=height,
    )
    return fig


def _local_logo_path(logo_url: str | None) -> str | None:
    """logo_url from the teams table is either a manifest-relative local path
    (docs/img/logos/<file> on disk - see utils._local_logo_url) or a raw
    external URL for a team the manifest doesn't cover yet. Only the former
    can be composited in without a network fetch - None skips that row's logo
    rather than guessing or blocking on a download."""
    if not logo_url or logo_url.startswith("http"):
        return None
    path = os.path.join(_REPO_ROOT, "docs", logo_url)
    return path if os.path.isfile(path) else None


def composite_draft_order_logos(png_bytes: bytes, rows: list[dict], teams_by_abbrev: dict[str, dict]) -> bytes:
    """Paste each row's team logo into draft_order_fig's blank logo column.
    Pixel math is derived from the same DRAFT_ORDER_* layout constants the
    table was drawn with, so it stays correct regardless of row count -
    verified against the actual kaleido render, not just the nominal layout
    (kaleido adds no extra cell padding beyond what go.Table was given)."""
    scale = DRAFT_ORDER_SCALE
    logo_x_logical = DRAFT_ORDER_MARGIN["l"] + sum(DRAFT_ORDER_COL_WIDTHS[:DRAFT_ORDER_LOGO_COL])
    logo_w_logical = DRAFT_ORDER_COL_WIDTHS[DRAFT_ORDER_LOGO_COL]
    col_px = logo_w_logical * scale
    row_px = DRAFT_ORDER_ROW_HEIGHT * scale
    x0 = logo_x_logical * scale
    box = int(min(col_px, row_px) * 0.82)

    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    logo_cache: dict[str, Image.Image | None] = {}
    for i, r in enumerate(rows):
        abbrev = r["team"]
        if abbrev not in logo_cache:
            path = _local_logo_path((teams_by_abbrev.get(abbrev) or {}).get("logo_url"))
            logo_cache[abbrev] = Image.open(path).convert("RGBA") if path else None
        logo = logo_cache[abbrev]
        if logo is None:
            continue
        thumb = ImageOps.contain(logo, (box, box), Image.LANCZOS)
        y0 = (DRAFT_ORDER_MARGIN["t"] + DRAFT_ORDER_HEADER_HEIGHT + i * DRAFT_ORDER_ROW_HEIGHT) * scale
        px = int(x0 + (col_px - thumb.width) / 2)
        py = int(y0 + (row_px - thumb.height) / 2)
        im.paste(thumb, (px, py), thumb)

    out = io.BytesIO()
    im.convert("RGB").save(out, format="PNG")
    return out.getvalue()

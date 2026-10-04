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

import pandas as pd
import plotly.graph_objects as go

import utils

HOT_ZONE_BUCKET_CHOICES = [50, 100, 125, 200, 250, 500]
HOT_ZONE_DEFAULT_BUCKET = 200

VALUE_BUCKET_CHOICES = [50, 100, 125, 200, 250, 500]
VALUE_DEFAULT_BUCKET = 200
DELTA_BUCKET_CHOICES = [25, 50, 100, 125, 250, 500]
DELTA_DEFAULT_BUCKET = 100

RADIAL_DEFAULT_N = 20

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


def last_n_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, swing_offset: bool = False,
               est_delta_overlay: bool = False, show_swings: bool = False) -> go.Figure:
    return utils.last_n_combined_chart(df, n=n, delta_col="pitch", title=f"Last {n} Pitches",
                                        swing_offset=swing_offset, segment_games=True, pannable=True,
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


def cooldown_figs(df: pd.DataFrame, bucket: int = HOT_ZONE_DEFAULT_BUCKET) -> dict[str, go.Figure]:
    radius = bucket / 2
    events = utils.cooldown_events(df, value_col="pitch", radius=radius)
    if events.empty:
        return {}
    return {
        "cooldown_overlay.png": utils.cooldown_cdf_overlay_chart(events),
        "cooldown_radial.png": utils.cooldown_radial_chart(events, radius=radius),
    }


def pitches_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="pitch", **context_kwargs)
    n_actual = min(n, int(df_f["pitch"].notna().sum())) if "pitch" in df_f.columns else 0
    return utils.radial_recent_pitches_chart(df_f, n=n, value_col="pitch", title=f"Last {n_actual} Pitches")


def deltas_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                       **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="delta", **context_kwargs)
    n_actual = min(n, int(df_f["pitch_circ_delta"].notna().sum())) if "pitch_circ_delta" in df_f.columns else 0
    anchor = _last_value(df, "pitch")
    title = f"Last {n_actual} Implied Pitches" if center_on_prev else f"Last {n_actual} Deltas"
    return utils.radial_recent_deltas_chart(df_f, n=n, delta_col="pitch_circ_delta", value_col="pitch",
                                             title=title, center_on_prev=center_on_prev, anchor=anchor)


def delta2_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, center_on_prev: bool = False,
                       **context_kwargs) -> go.Figure:
    df_f = apply_prior_context(df, bucket_kind="delta2", **context_kwargs)
    n_actual = (min(n, int(df_f["pitch_circ_delta2_signed"].notna().sum()))
                if "pitch_circ_delta2_signed" in df_f.columns else 0)
    anchor = _last_value(df, "pitch")
    anchor_delta = _last_value(df, "pitch_circ_delta")
    title = f"Last {n_actual} Implied Pitches (Δ²)" if center_on_prev else f"Last {n_actual} Delta²s"
    return utils.radial_recent_delta2_chart(df_f, n=n, delta2_col="pitch_circ_delta2_signed",
                                             delta_col="pitch_circ_delta", value_col="pitch",
                                             title=title, center_on_prev=center_on_prev,
                                             anchor=anchor, anchor_delta=anchor_delta)


def combined_radial_fig(df: pd.DataFrame, n: int = RADIAL_DEFAULT_N, include_delta2: bool = False,
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
    return utils.radial_combined_chart(df_pitches, df_deltas, n=n, value_col="pitch",
                                        delta_col="pitch_circ_delta", title=f"Last {n_actual} Combined",
                                        anchor=anchor, df_delta2=df_delta2, delta2_col="pitch_circ_delta2_signed",
                                        anchor_delta=anchor_delta, include_delta2=include_delta2)


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
                                        swing_offset=swing_offset, pannable=True,
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
                                        swing_offset=swing_offset, segment_games=True, pannable=True,
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

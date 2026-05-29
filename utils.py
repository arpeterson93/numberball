"""Derived stats, constants, and chart helpers for Numberball."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ------------------------------------------------------------------ constants

TEAMS = ["Couriers", "Jammers", "Sharks", "Tridents"]

OBC_OPTIONS = ["Empty", "1B", "2B", "3B", "1&2B", "1&3B", "2&3B", "BL"]

RESULTS_HITS = ["HR", "3B", "2BWH", "2B", "1BWH2", "1BWH", "1B", "IF1B", "BB"]
RESULTS_OUTS = ["DSacF", "DFO", "SacF", "FO", "PO", "GORA", "FCH", "FC", "GO",
                "FC3rd", "DPRun", "DP", "DP21", "DP31", "DPH1", "K", "LODP", "TP", "LOTP"]
RESULTS = RESULTS_HITS + RESULTS_OUTS

RESULT_CATEGORIES = {
    "OUT": ["GO", "FO", "PO", "K", "FC", "DP", "DPH1", "GORA", "DSacF"],
    "BB/1B": ["BB", "1B", "IF1B"],
    "XBH": ["2B", "3B", "HR"],
}

MEME_NUMBERS = [42, 69, 420]

# Result ranges: (result, diff_low, diff_high) — from the league result table
RESULT_RANGES = [
    ("HR",    0,   20),
    ("3B",   21,   25),
    ("2BWH", 26,   27),
    ("2B",   28,   54),
    ("1BWH", 55,   60),
    ("1B",   61,  133),
    ("IF1B", 134, 142),
    ("BB",   143, 176),
    ("GORA", 177, 204),
    ("DSacF",205, 207),
    ("SacF", 208, 251),
    ("PO",   252, 271),
    ("FCH",  272, 290),
    ("K",    291, 406),
    ("DP21", 407, 437),
    ("DP31", 438, 467),
    ("DPH1", 468, 500),
]

_RESULT_ZONE_COLORS = {
    "HR":    "#1a7d35",
    "3B":    "#2ca02c",
    "2BWH":  "#57b857",
    "2B":    "#93d493",
    "1BWH":  "#c4e8a4",
    "1B":    "#e5f5c3",
    "IF1B":  "#fff7bc",
    "BB":    "#fee391",
    "GORA":  "#fec44f",
    "DSacF": "#fe9929",
    "SacF":  "#fd7a1a",
    "PO":    "#f03b20",
    "FCH":   "#d42020",
    "K":     "#b10026",
    "DP21":  "#800026",
    "DP31":  "#5a001a",
    "DPH1":  "#3d0014",
}

ZONES = [
    (1,   111,  "1-111"),
    (112, 222,  "112-222"),
    (223, 333,  "223-333"),
    (334, 444,  "334-444"),
    (445, 555,  "445-555"),
    (556, 666,  "556-666"),
    (667, 777,  "667-777"),
    (778, 888,  "778-888"),
    (889, 1000, "889-1000"),
]
ZONE_LABELS = [z[2] for z in ZONES]

# Zone grid: displayed high→low, left→right, top→bottom (matches spreadsheet layout)
ZONE_GRID = [
    ["223-333", "112-222", "1-111"],
    ["556-666", "445-555", "334-444"],
    ["889-1000", "778-888", "667-777"],
]

# Delta range buckets (pitch change from previous at-bat)
DELTA_RANGES = [
    (-500, -400, "-500 to -400"),
    (-399, -300, "-399 to -300"),
    (-299, -200, "-299 to -200"),
    (-199, -100, "-199 to -100"),
    (-99,  -50,  "-99 to -50"),
    (-49,    0,  "-49 to 0"),
    (1,     50,  "1 to 50"),
    (51,   100,  "51 to 100"),
    (101,  200,  "101 to 200"),
    (201,  300,  "201 to 300"),
    (301,  400,  "301 to 400"),
    (401,  500,  "401 to 500"),
]


# ------------------------------------------------------------------ calculations

def circular_diff(pitch: int, swing: int) -> int:
    """Circular distance on 1-1000 wheel (1 and 1000 are adjacent)."""
    d = abs(pitch - swing)
    return min(d, 1000 - d)


def circular_signed_delta(a: int, b: int) -> int:
    """Signed delta on the 1-1000 wheel using the shortest path. Range: -500 to +500."""
    d = b - a
    if d > 500:
        d -= 1000
    elif d < -500:
        d += 1000
    return d


def get_zone(value: int) -> str:
    for lo, hi, label in ZONES:
        if lo <= value <= hi:
            return label
    return "Unknown"


def get_delta_range(delta: float) -> str:
    for lo, hi, label in DELTA_RANGES:
        if lo <= delta <= hi:
            return label
    return "Other"


def _circ_delta_group(group: pd.Series) -> pd.Series:
    vals = group.astype(int).tolist()
    deltas = [float("nan")] + [circular_signed_delta(vals[i - 1], vals[i]) for i in range(1, len(vals))]
    return pd.Series(deltas, index=group.index)


_XBH  = {"HR", "3B", "2BWH", "2B"}
_BB1B = {"1BWH2", "1BWH", "1B", "IF1B", "BB"}

def get_res_category(result: str, diff: int) -> str:
    if diff >= 300:
        return "300+"
    if result in _XBH:
        return "XBH"
    if result in _BB1B:
        return "BB/1B"
    return "OUT"


_OUT_RESULTS = {"GO", "FO", "PO", "K", "GORA", "DSacF", "FC"}
_DP_RESULTS  = {"DP", "DPH1"}


def outs_added(result: str) -> int:
    if result in _DP_RESULTS:
        return 2
    if result in _OUT_RESULTS:
        return 1
    return 0


def validate_ab(new: dict, prev: dict | None) -> list[str]:
    """Return a list of baseball-logic warnings for the new AB given the previous one."""
    if prev is None:
        return []

    warnings = []
    p_inn  = int(prev["inning"])
    p_half = prev.get("half", "top")
    p_outs = int(prev["outs"])
    p_res  = prev.get("result", "")

    n_inn  = int(new["inning"])
    n_half = new["half"]
    n_outs = int(new["outs"])

    added         = outs_added(p_res)
    expected_outs = p_outs + added
    same_half     = (n_inn == p_inn and n_half == p_half)

    if same_half:
        if expected_outs >= 3:
            warnings.append(
                f"Previous AB ({p_res}, {p_outs} outs) should have ended the half-inning — "
                f"expected a new half-inning, not the same one."
            )
        elif n_outs != expected_outs:
            warnings.append(
                f"Expected {expected_outs} out(s) based on previous result ({p_res}), "
                f"but got {n_outs}."
            )
    else:
        if n_outs != 0:
            warnings.append(f"New half-inning started but outs = {n_outs} (expected 0).")

        # Half-inning order: top → bottom of same inning → top of next inning
        if n_inn < p_inn:
            warnings.append(f"Inning went backward ({p_inn} → {n_inn}).")
        elif n_inn == p_inn:
            if not (p_half == "top" and n_half == "bottom"):
                warnings.append(
                    f"Unexpected half-inning change within inning {n_inn} "
                    f"({p_half} → {n_half})."
                )
        elif n_inn > p_inn + 1:
            warnings.append(f"Inning jumped by more than one ({p_inn} → {n_inn}).")
        elif n_half != "top":
            warnings.append(
                f"New inning {n_inn} should start at top, not bottom."
            )

    return warnings


def inning_label(inning: int, half: str) -> str:
    prefix = "T" if str(half).lower() == "top" else "B"
    return f"{prefix}{int(inning)}"


def enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    """Add diff, zone, res_category, FP flags, delta, and inning label columns."""
    if df.empty:
        return df
    df = df.copy()
    df["half"] = df["half"].fillna("top")
    df["diff"] = df.apply(lambda r: circular_diff(int(r["pitch"]), int(r["swing"])), axis=1)
    df["pitch_zone"] = df["pitch"].apply(lambda p: get_zone(int(p)))
    df["swing_zone"] = df["swing"].apply(lambda s: get_zone(int(s)))
    df["res_category"] = df.apply(lambda r: get_res_category(r["result"], r["diff"]), axis=1)
    df["is_meme_pitch"] = df["pitch"].isin(MEME_NUMBERS)
    df["is_meme_swing"] = df["swing"].isin(MEME_NUMBERS)
    df["pitch_last2"] = df["pitch"].apply(lambda p: int(str(int(p)).zfill(2)[-2:]))
    df["inning_label"] = df.apply(lambda r: inning_label(r["inning"], r["half"]), axis=1)
    # Compute FP flags from insertion order within session
    df = df.sort_values(["session_id", "id"])
    df["is_fp_inn"] = ~df.duplicated(subset=["session_id", "inning", "half"], keep="first")
    df["is_fp_app"] = ~df.duplicated(subset=["session_id", "pitcher_name"], keep="first")
    # Linear delta (for reference)
    df["pitch_delta"] = df.groupby(["session_id", "pitcher_name"])["pitch"].diff()
    # Circular signed delta (shortest path on the 1-1000 wheel)
    df["pitch_circ_delta"] = (
        df.groupby(["session_id", "pitcher_name"], group_keys=False)["pitch"].apply(_circ_delta_group)
    )
    df["swing_circ_delta"] = (
        df.groupby(["session_id", "batter_name"], group_keys=False)["swing"].apply(_circ_delta_group)
    )
    return df


def flatten_sessions(at_bats: list[dict]) -> pd.DataFrame:
    """Flatten nested session data from Supabase join into flat columns."""
    rows = []
    for ab in at_bats:
        row = {k: v for k, v in ab.items() if k != "sessions"}
        if ab.get("sessions"):
            row["season"] = ab["sessions"].get("season")
            row["session_number"] = ab["sessions"].get("session_number")
            row["home_team"] = ab["sessions"].get("home_team")
            row["away_team"] = ab["sessions"].get("away_team")
        rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ------------------------------------------------------------------ charts

def zone_heatmap(
    zone_counts: dict[str, int],
    title: str = "Zone Frequency",
    pct: bool = True,
) -> go.Figure:
    """3×3 heatmap of zone frequencies."""
    total = sum(zone_counts.values()) or 1
    z_vals = []
    z_text = []
    for row in ZONE_GRID:
        z_row, t_row = [], []
        for zone in row:
            count = zone_counts.get(zone, 0)
            z_row.append(count / total * 100)
            pct_str = f"{count / total * 100:.1f}%" if pct else ""
            t_row.append(f"<b>{zone}</b><br>{count}{f'<br>{pct_str}' if pct else ''}")
        z_vals.append(z_row)
        z_text.append(t_row)

    fig = go.Figure(go.Heatmap(
        z=z_vals,
        text=z_text,
        texttemplate="%{text}",
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=3,
        ygap=3,
        hovertemplate="%{text}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=260,
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        margin=dict(l=10, r=10, t=45, b=10),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def delta_histogram(deltas: pd.Series, title: str = "Pitch Delta Distribution") -> go.Figure:
    """Histogram of pitch-to-pitch changes."""
    fig = go.Figure(go.Histogram(
        x=deltas.dropna(),
        nbinsx=30,
        marker_color="#4C78A8",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis_title="Change from Previous Pitch",
        yaxis_title="Count",
        height=280,
        margin=dict(l=40, r=10, t=45, b=40),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


_HOT_ZONE_LABELS = [
    "1-100", "101-200", "201-300", "301-400", "401-500",
    "501-600", "601-700", "701-800", "801-900", "901-1000",
]


def last_n_chart(
    df: pd.DataFrame,
    n: int = 20,
    pitch_col: str = "pitch",
    swing_col: str = "swing",
    title: str = "Last 20 At-Bats",
) -> go.Figure:
    """Line chart of the last N pitch and swing values in sequence."""
    df_last = df.sort_values("id").tail(n).reset_index(drop=True)
    x = list(range(1, len(df_last) + 1))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=df_last[pitch_col].astype(int),
        mode="lines+markers+text",
        name="Pitch",
        text=df_last[pitch_col].astype(int).astype(str),
        textposition="top center",
        textfont=dict(size=9),
        line=dict(color="#d6604d", width=2),
        marker=dict(size=5),
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df_last[swing_col].astype(int),
        mode="lines+markers+text",
        name="Swing",
        text=df_last[swing_col].astype(int).astype(str),
        textposition="bottom center",
        textfont=dict(size=9),
        line=dict(color="#2166ac", width=2),
        marker=dict(size=5),
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="At-Bat #", tickmode="linear", dtick=1),
        yaxis=dict(range=[0, 1080]),
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=10, t=60, b=40),
    )
    return fig


def last_n_delta_chart(
    df: pd.DataFrame,
    n: int = 20,
    value_col: str = "pitch",
    title: str = "Pitch Delta",
) -> go.Figure:
    """Bar chart of circular signed delta between consecutive pitch/swing values.
    Bar height = abs(circular delta). Green = went higher, red = went lower."""
    df_last = df.sort_values("id").tail(n).reset_index(drop=True)
    vals = df_last[value_col].astype(int).tolist()
    x = list(range(2, len(vals) + 1))
    deltas = [circular_signed_delta(vals[i - 1], vals[i]) for i in range(1, len(vals))]
    linear = [vals[i] - vals[i - 1] for i in range(1, len(vals))]

    colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in deltas]
    hover = [
        f"AB {i}: {vals[i-1]}→{vals[i]}<br>Circular: {deltas[i-1]:+d}<br>Linear: {linear[i-1]:+d}"
        for i in range(1, len(vals))
    ]

    fig = go.Figure(go.Bar(
        x=x,
        y=[abs(d) for d in deltas],
        marker_color=colors,
        text=[f"{d:+d}" for d in deltas],
        textposition="outside",
        textfont=dict(size=9),
        hovertext=hover,
        hoverinfo="text",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="At-Bat #", tickmode="linear", dtick=1),
        yaxis=dict(range=[0, 580], title="Distance"),
        height=250,
        margin=dict(l=40, r=10, t=45, b=40),
        showlegend=False,
    )
    return fig


def last_n_combined_chart(
    df: pd.DataFrame,
    n: int = 20,
    delta_col: str = "pitch",
    title: str = "Last N Pitches",
) -> go.Figure:
    """Two-row subplot: pitch+swing lines on top, circular delta bars on bottom, shared x-axis."""
    df_last = df.sort_values("id").tail(n).reset_index(drop=True)
    n_actual = len(df_last)
    x_all = list(range(1, n_actual + 1))
    pitches = df_last["pitch"].astype(int).tolist()
    swings = df_last["swing"].astype(int).tolist()
    delta_vals = df_last[delta_col].astype(int).tolist()

    deltas = [circular_signed_delta(delta_vals[i - 1], delta_vals[i]) for i in range(1, n_actual)]
    linear = [delta_vals[i] - delta_vals[i - 1] for i in range(1, n_actual)]
    x_delta = list(range(2, n_actual + 1))
    colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in deltas]
    hover = [
        f"AB {i}: {delta_vals[i-1]}→{delta_vals[i]}<br>Circular: {deltas[i-1]:+d}<br>Linear: {linear[i-1]:+d}"
        for i in range(1, n_actual)
    ]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.06,
    )

    fig.add_trace(go.Scatter(
        x=x_all, y=pitches, mode="lines+markers+text", name="Pitch",
        text=[str(p) for p in pitches], textposition="top center",
        textfont=dict(size=9), line=dict(color="#d6604d", width=2), marker=dict(size=5),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=x_all, y=swings, mode="lines+markers+text", name="Swing",
        text=[str(s) for s in swings], textposition="bottom center",
        textfont=dict(size=9), line=dict(color="#2166ac", width=2), marker=dict(size=5),
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=x_delta, y=[abs(d) for d in deltas], marker_color=colors,
        text=[f"{d:+d}" for d in deltas], textposition="outside",
        textfont=dict(size=9), hovertext=hover, hoverinfo="text",
        name="Delta", showlegend=False,
    ), row=2, col=1)

    x_range = [0.5, n_actual + 0.5]
    fig.update_xaxes(tickmode="linear", dtick=1, range=x_range, showticklabels=False, row=1, col=1)
    fig.update_xaxes(
        title_text="← Older  ·  At-Bat #  ·  Newer →",
        tickmode="linear", dtick=1, range=x_range, row=2, col=1,
    )
    fig.update_yaxes(range=[0, 1080], row=1, col=1)
    fig.update_yaxes(range=[0, 500], title_text="Delta", row=2, col=1)
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=45, r=10, t=60, b=40),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def _diff_to_result(diff: int) -> str:
    for result, lo, hi in RESULT_RANGES:
        if lo <= diff <= hi:
            return result
    return "?"


def swing_predictor_chart(
    df: pd.DataFrame,
    swing: int,
    n: int = 20,
    title: str = "Swing Predictor",
) -> go.Figure:
    """Color-coded pitch number line for a proposed swing, with recent pitches overlaid."""
    # Build per-pitch result for all 1000 values
    pitch_result = [_diff_to_result(circular_diff(p, swing)) for p in range(1, 1001)]

    # Collapse into contiguous zones
    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()

    # Invisible trace to anchor axes
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    # Draw colored rectangles for each zone
    seen: set[str] = set()
    for result, lo, hi in zones:
        color = _RESULT_ZONE_COLORS.get(result, "#cccccc")
        if result not in seen:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=result, showlegend=True,
            ))
            seen.add(result)
        fig.add_shape(
            type="rect", x0=lo - 0.5, x1=hi + 0.5, y0=0, y1=1,
            fillcolor=color, line=dict(width=0), layer="below",
        )
        if hi - lo >= 15:
            fig.add_annotation(
                x=(lo + hi) / 2, y=0.5, text=result,
                showarrow=False, font=dict(size=9, color="white"),
                xanchor="center", yanchor="middle",
            )

    # Recent pitches as vertical tick marks
    df_last = df.sort_values("id").tail(n)
    pitches = df_last["pitch"].astype(int).tolist()
    fig.add_trace(go.Scatter(
        x=pitches, y=[0.5] * len(pitches),
        mode="markers",
        marker=dict(symbol="line-ns-open", size=22, color="black",
                    line=dict(width=2, color="black")),
        name=f"Last {n} Pitches",
        hovertemplate="Pitch: %{x}<extra></extra>",
    ))

    # Proposed swing marker
    fig.add_vline(x=swing, line_dash="dash", line_color="navy", line_width=2)
    fig.add_annotation(
        x=swing, y=1.08, text=f"Swing {swing}",
        showarrow=False, font=dict(color="navy", size=11),
        xanchor="center",
    )

    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Pitch Value", range=[0.5, 1000.5], tickmode="linear", dtick=100),
        yaxis=dict(visible=False, range=[-0.05, 1.15]),
        height=260,
        margin=dict(l=10, r=10, t=65, b=80),
        legend=dict(
            orientation="h", yanchor="top", y=-0.3,
            xanchor="left", x=0, font=dict(size=9),
        ),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def hot_zone_matrix(
    df: pd.DataFrame,
    value_col: str = "pitch",
    group_cols: list[str] = None,
    title: str = "Hot Zone Pitch Matrix",
    bucket_size: int = 100,
) -> go.Figure:
    """Heatmap of consecutive pitch/swing zone transitions. bucket_size must divide 1000 evenly."""
    if group_cols is None:
        group_cols = ["session_id", "pitcher_name"]

    n_buckets = 1000 // bucket_size
    labels = [f"{i * bucket_size + 1}-{min((i + 1) * bucket_size, 1000)}" for i in range(n_buckets)]

    df = df.sort_values(["session_id", "id"]).copy()
    df["_next"] = df.groupby(group_cols)[value_col].shift(-1)
    df = df.dropna(subset=["_next"])

    df["_curr_b"] = ((df[value_col].astype(int) - 1) // bucket_size).clip(0, n_buckets - 1)
    df["_next_b"] = ((df["_next"].astype(int) - 1) // bucket_size).clip(0, n_buckets - 1)

    matrix = (
        pd.crosstab(df["_curr_b"], df["_next_b"])
        .reindex(index=range(n_buckets), columns=range(n_buckets), fill_value=0)
    )

    z = matrix.values.tolist()
    text = [[str(v) if v > 0 else "" for v in row] for row in z]

    fig = go.Figure(go.Heatmap(
        z=z,
        x=labels,
        y=labels,
        text=text,
        texttemplate="%{text}",
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="From %{y} → %{x}<br>Count: %{z}<extra></extra>",
    ))
    fig.update_layout(
        xaxis=dict(title="Following Pitch", tickangle=45, side="top"),
        yaxis=dict(title="Initial Pitch", autorange="reversed"),
        height=max(400, n_buckets * 42 + 100),
        margin=dict(l=80, r=10, t=80, b=10),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def result_bar(result_counts: dict[str, int], title: str = "Results") -> go.Figure:
    """Horizontal bar chart of result distribution."""
    labels = list(result_counts.keys())
    values = list(result_counts.values())
    total = sum(values) or 1
    pcts = [v / total * 100 for v in values]

    fig = go.Figure(go.Bar(
        x=pcts,
        y=labels,
        orientation="h",
        text=[f"{v} ({p:.1f}%)" for v, p in zip(values, pcts)],
        textposition="outside",
        marker_color="#4C78A8",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis_title="% of ABs",
        height=max(200, len(labels) * 30 + 80),
        margin=dict(l=80, r=80, t=45, b=30),
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig

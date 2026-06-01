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
    # Hits — green spectrum (best → marginal)
    "HR":    "#1a7d35",
    "3B":    "#2ca02c",
    "2BWH":  "#57b857",
    "2B":    "#93d493",
    "1BWH2": "#aedda2",
    "1BWH":  "#c4e8a4",
    "1B":    "#e5f5c3",
    "IF1B":  "#fff7bc",
    "BB":    "#fee391",
    # Soft outs / sac — yellow → orange
    "GORA":  "#fec44f",
    "DSacF": "#fe9929",
    "DFO":   "#fd8c15",
    "SacF":  "#fd7a1a",
    "FO":    "#f56010",
    # Standard outs — orange-red → red
    "PO":    "#f03b20",
    "FCH":   "#d42020",
    "FC":    "#c42020",
    "FC3rd": "#b82020",
    "GO":    "#aa1020",
    "K":     "#b10026",
    # Double plays — dark red → maroon
    "DPRun": "#920026",
    "DP":    "#880026",
    "DP21":  "#800026",
    "DP31":  "#5a001a",
    "DPH1":  "#3d0014",
    # Line-out DPs / triple plays — near black
    "LODP":  "#2d000f",
    "TP":    "#220009",
    "LOTP":  "#180006",
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

TEAM_ABBREV: dict[str, str] = {
    "CC": "Couriers",
    "JJ": "Jammers",
    "TT": "Tridents",
    "SLS": "Sharks",
}

BRC_TO_OBC: dict[int, str] = {
    0: "Empty", 1: "1B", 2: "2B", 3: "1&2B",
    4: "3B", 5: "1&3B", 6: "2&3B", 7: "BL",
}


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
_OBR  = _XBH | _BB1B

# Bases per hit result (walks excluded from SLG per baseball convention)
_SLG_WEIGHTS = {
    "HR": 4, "3B": 3, "2BWH": 2, "2B": 2,
    "1BWH2": 1, "1BWH": 1, "1B": 1, "IF1B": 1, "BB": 1,
}

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
    # Compute FP flags from insertion order within game
    df = df.sort_values(["game_id", "id"])
    df["is_fp_inn"] = ~df.duplicated(subset=["game_id", "inning", "half"], keep="first")
    df["is_fp_app"] = ~df.duplicated(subset=["game_id", "pitcher_name"], keep="first")
    # Linear delta (for reference)
    df["pitch_delta"] = df.groupby(["game_id", "pitcher_name"])["pitch"].diff()
    # Circular signed delta (shortest path on the 1-1000 wheel)
    df["pitch_circ_delta"] = (
        df.groupby(["game_id", "pitcher_name"], group_keys=False)["pitch"].apply(_circ_delta_group)
    )
    df["swing_circ_delta"] = (
        df.groupby(["game_id", "batter_name"], group_keys=False)["swing"].apply(_circ_delta_group)
    )
    return df


def flatten_games(plays: list[dict]) -> pd.DataFrame:
    """Flatten nested game data from Supabase join into flat columns."""
    rows = []
    for play in plays:
        row = {k: v for k, v in play.items() if k != "games"}
        if play.get("games"):
            g = play["games"]
            row["season"] = g.get("season")
            row["session_number"] = g.get("session_number")
            row["home_team"] = g.get("home_team")
            row["away_team"] = g.get("away_team")
            row["game_code"] = g.get("game_code")
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
        dragmode=False,
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
        dragmode=False,
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
        textfont=dict(size=10),
        line=dict(color="#d6604d", width=2),
        marker=dict(size=5),
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df_last[swing_col].astype(int),
        mode="lines+markers+text",
        name="Swing",
        text=df_last[swing_col].astype(int).astype(str),
        textposition="bottom center",
        textfont=dict(size=10),
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
        textfont=dict(size=10),
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
    swing_offset: bool = False,
) -> go.Figure:
    """Two-row subplot: pitch+swing lines on top, circular delta bars on bottom, shared x-axis.
    swing_offset: if True, shifts swing markers right by 1 to show whether swing predicts next pitch.
    """
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

    # With offset: swing[i] is plotted at x = i+2 (next AB slot), pairing it with pitch[i+1]
    if swing_offset and n_actual > 1:
        swing_x = list(range(2, n_actual + 1))
        swing_y = swings[:-1]
        swing_text = [str(s) for s in swing_y]
    else:
        swing_x = x_all
        swing_y = swings
        swing_text = [str(s) for s in swings]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.06,
    )

    fig.add_trace(go.Scatter(
        x=x_all, y=pitches, mode="lines+markers+text", name="Pitch",
        text=[str(p) for p in pitches], textposition="top center",
        textfont=dict(size=10), line=dict(color="#d6604d", width=2), marker=dict(size=5),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=swing_x, y=swing_y, mode="lines+markers+text",
        name="Swing" + (" (offset +1)" if swing_offset else ""),
        text=swing_text, textposition="bottom center",
        textfont=dict(size=10), line=dict(color="#2166ac", width=2), marker=dict(size=5),
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=x_delta, y=[abs(d) for d in deltas], marker_color=colors,
        text=[f"{d:+d}" for d in deltas], textposition="outside",
        textfont=dict(size=10), hovertext=hover, hoverinfo="text",
        name="Delta", showlegend=False,
    ), row=2, col=1)

    x_range = [0.5, n_actual + 0.5]
    fig.update_xaxes(tickmode="linear", dtick=1, range=x_range, showticklabels=False, row=1, col=1)
    fig.update_xaxes(
        title_text="← Older  ·  At-Bat #  ·  Newer →",
        tickmode="linear", dtick=1, range=x_range, row=2, col=1,
    )
    fig.update_yaxes(range=[0, 1080], row=1, col=1)
    fig.update_yaxes(
        range=[0, 540], title_text="Delta", row=2, col=1,
        tickmode="array", tickvals=[100, 200, 300, 400, 500],
    )
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=45, r=10, t=60, b=40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def _diff_to_result(diff: int, ranges: list | None = None) -> str:
    for result, lo, hi in (ranges or RESULT_RANGES):
        if lo <= diff <= hi:
            return result
    return "?"


def get_sheet_name(sheet_url: str) -> str:
    """Return the Google Sheet title for display, falling back to a short ID."""
    import re, urllib.request
    match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    if not match:
        return sheet_url[-40:]
    sheet_id = match.group(1)
    try:
        req = urllib.request.Request(
            f"https://docs.google.com/spreadsheets/d/{sheet_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read(8192).decode("utf-8", errors="ignore")
        m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        if m:
            title = re.sub(r"\s*[-–]\s*Google Sheets$", "", m.group(1)).strip()
            if title:
                return title
    except Exception:
        pass
    return f"Sheet {sheet_id[:12]}…"


def parse_result_ranges_from_sheet(sheet_url: str) -> list[tuple[str, int, int]]:
    """Fetch and parse the result ranges table from a public Google Sheet."""
    import re
    sheet_id_match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    gid_match = re.search(r"[?&]gid=(\d+)", sheet_url)
    if not sheet_id_match:
        raise ValueError("Could not parse a Google Sheets ID from the URL.")
    sheet_id = sheet_id_match.group(1)
    gid = gid_match.group(1) if gid_match else "0"
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

    raw = pd.read_csv(csv_url, header=None, dtype=str)

    # Locate the "Result" header cell
    result_col = header_row = None
    for i in range(len(raw)):
        for j in range(len(raw.columns)):
            if str(raw.iloc[i, j]).strip().lower() == "result":
                result_col, header_row = j, i
                break
        if result_col is not None:
            break
    if result_col is None:
        raise ValueError("Could not find a 'Result' header in the sheet.")

    low_col, high_col = result_col + 2, result_col + 3
    ranges: list[tuple[str, int, int]] = []
    for i in range(header_row + 1, len(raw)):
        name = str(raw.iloc[i, result_col]).strip()
        if not name or name.lower() == "nan":
            break
        try:
            lo = int(float(str(raw.iloc[i, low_col]).strip()))
            hi = int(float(str(raw.iloc[i, high_col]).strip()))
            ranges.append((name, lo, hi))
        except (ValueError, IndexError):
            break
    if not ranges:
        raise ValueError("Result table found but no rows could be parsed.")

    # H12 = row index 11, col index 7 — current batter name
    # H11 = row index 10, col index 7 — current pitcher name
    def _cell(r, c):
        try:
            v = str(raw.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else ""
        except Exception:
            return ""

    batter_name = _cell(11, 7)
    pitcher_name = _cell(10, 7)

    return ranges, batter_name, pitcher_name


def project_from_deltas(recent_vals: list[int]) -> list[int]:
    """Apply each recent circular delta to the most recent value to get projected next positions."""
    if len(recent_vals) < 2:
        return []
    last_val = recent_vals[-1]
    return [((last_val + circular_signed_delta(recent_vals[i - 1], recent_vals[i]) - 1) % 1000) + 1
            for i in range(1, len(recent_vals))]


def _build_weight_array(vals: list[int]) -> "import numpy; numpy.ndarray":
    """Return a length-1000 probability weight array proportional to recent frequency."""
    import numpy as np
    from collections import Counter
    w = np.zeros(1000)
    if not vals:
        w[:] = 1.0 / 1000
        return w
    total = len(vals)
    for v, c in Counter(vals).items():
        w[v - 1] += c / total
    return w


def _scores_via_fft(w: "numpy.ndarray", diff_score_arr: "numpy.ndarray") -> "numpy.ndarray":
    """Circular convolution: scores[r] = Σ_v w[v] * diff_score[circ_dist(r+1, v+1)], via FFT."""
    import numpy as np
    kernel = np.array([diff_score_arr[min(d, 1000 - d)] for d in range(1000)])
    return np.real(np.fft.ifft(np.fft.fft(w) * np.fft.fft(kernel)))


def _diff_score_array(result_ranges: list, metric: str) -> "numpy.ndarray":
    """Precompute a length-501 score array indexed by circular diff value."""
    import numpy as np
    arr = np.zeros(501)
    for d in range(501):
        r = _diff_to_result(d, result_ranges)
        arr[d] = (1.0 if r in _OBR else 0.0) if metric == "obp" else float(_SLG_WEIGHTS.get(r, 0))
    return arr


def suggest_swing(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
) -> tuple[int, float, int, float]:
    """Return (best_val, best_score, counter_val, counter_score) via FFT convolution.

    best: argmax if maximize else argmin. counter: the opposite extreme.
    """
    import numpy as np
    if not recent_opp_vals:
        return 500, 0.0, 500, 0.0
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals),
        _diff_score_array(result_ranges, metric),
    )
    best_idx = int(np.argmax(scores) if maximize else np.argmin(scores))
    counter_idx = int(np.argmin(scores) if maximize else np.argmax(scores))
    return best_idx + 1, float(scores[best_idx]), counter_idx + 1, float(scores[counter_idx])


def optimal_swing_chart(
    recent_opp_vals: list[int],
    result_ranges: list,
    metric: str = "obp",
    maximize: bool = True,
    title: str = "Expected Score by Swing Value",
    compact: bool = False,
) -> go.Figure:
    """1-row gradient heatmap showing expected OBP or SLG for every possible swing value.

    Marks both the best value (green vline) and the counter/worst value (orange dotted vline).
    """
    import numpy as np
    scores = _scores_via_fft(
        _build_weight_array(recent_opp_vals),
        _diff_score_array(result_ranges, metric),
    )
    best_idx = int(np.argmax(scores) if maximize else np.argmin(scores))
    counter_idx = int(np.argmin(scores) if maximize else np.argmax(scores))
    best_val = best_idx + 1
    best_score = float(scores[best_idx])
    counter_val = counter_idx + 1
    counter_score = float(scores[counter_idx])

    colorscale = "RdYlGn" if maximize else "RdYlGn_r"
    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        z=[scores.tolist()],
        x=list(range(1, 1001)),
        y=[0],
        colorscale=colorscale,
        showscale=not compact,
        colorbar=dict(title=dict(text=metric.upper(), side="right"), thickness=12, len=0.8),
        hovertemplate=f"Swing: %{{x}}<br>Expected {metric.upper()}: %{{z:.3f}}<extra></extra>",
    ))

    # Best vline: two-layer (dark outline + white center)
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=best_val, x1=best_val, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw))
    # Counter vline: two-layer (dark outline + orange center), dotted
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,140,0,0.9)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=counter_val, x1=counter_val, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dot"))

    if compact:
        fig.add_annotation(
            x=best_val, y=0.78, yref="paper",
            text=f"↑{best_val} ({best_score:.3f})",
            showarrow=False, xanchor="left",
            font=dict(color="white", size=8),
            bgcolor="rgba(0,0,0,0.55)", borderpad=0,
        )
        fig.add_annotation(
            x=counter_val, y=0.22, yref="paper",
            text=f"↓{counter_val} ({counter_score:.3f})",
            showarrow=False, xanchor="left",
            font=dict(color="rgba(255,180,80,1)", size=8),
            bgcolor="rgba(0,0,0,0.55)", borderpad=0,
        )
    else:
        fig.add_annotation(
            x=best_val, y=0.75, yref="paper",
            text=f"Best: {best_val}<br>({best_score:.3f})",
            showarrow=True, arrowhead=2, arrowcolor="white", ax=40, ay=0,
            font=dict(color="white", size=9),
            bgcolor="rgba(0,0,0,0.6)", borderpad=2,
        )
        fig.add_annotation(
            x=counter_val, y=0.25, yref="paper",
            text=f"Counter: {counter_val}<br>({counter_score:.3f})",
            showarrow=True, arrowhead=2, arrowcolor="rgba(255,140,0,0.9)", ax=-40, ay=0,
            font=dict(color="rgba(255,180,80,1)", size=9),
            bgcolor="rgba(0,0,0,0.6)", borderpad=2,
        )
    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=10 if not compact else 8),
        ),
        yaxis=dict(visible=False),
        height=110 if compact else 130,
        margin=dict(l=10, r=10 if compact else 80, t=5 if compact else 10, b=35 if compact else 30),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def swing_predictor_chart(
    df: pd.DataFrame,
    swing: int,
    n: int = 20,
    title: str = "Swing Predictor",
    result_ranges: list | None = None,
    tick_label: str = "Recent Pitches",
    value_col: str = "pitch",
    x_label: str = "Pitch Value",
    ref_label: str = "Swing",
    ref_color: str = "navy",
) -> go.Figure:
    """Color-coded number line for a proposed reference value, with recent pitch/swing values overlaid.
    value_col: column to pull tick marks from ('pitch' for pitcher page, 'swing' for batter page).
    """
    ranges = result_ranges or RESULT_RANGES
    # For each value 1-1000, compute result given the reference (circular diff is symmetric)
    pitch_result = [_diff_to_result(circular_diff(p, swing), ranges) for p in range(1, 1001)]

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

    # Build diff-range lookup for legend labels: result → (diff_lo, diff_hi, width)
    diff_info = {r: (lo, hi, hi - lo + 1) for r, lo, hi in ranges}

    # Draw colored rectangles for each zone
    seen: set[str] = set()
    for result, lo, hi in zones:
        color = _RESULT_ZONE_COLORS.get(result, "#cccccc")
        if result not in seen:
            d_lo, d_hi, w = diff_info.get(result, (0, 0, 0))
            label = f"{result}: {d_lo}–{d_hi} ({w})"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=label, showlegend=True,
            ))
            seen.add(result)
        fig.add_shape(
            type="rect", x0=lo - 0.5, x1=hi + 0.5, y0=0, y1=1,
            fillcolor=color, line=dict(width=0), layer="below",
        )
        if hi - lo >= len(result) * 12:
            fig.add_annotation(
                x=(lo + hi) / 2, y=0.5, text=result,
                showarrow=False, font=dict(size=9, color="white"),
                xanchor="center", yanchor="middle",
            )

    # Tick marks — triangles beneath the colored zone, blue=oldest → white → red=newest
    df_last = df.sort_values("id").tail(n)
    vals = df_last[value_col].astype(int).tolist()
    n_vals = len(vals)
    fig.add_trace(go.Scatter(
        x=vals, y=[-0.08] * n_vals,
        mode="markers",
        marker=dict(
            symbol="triangle-up", size=9,
            color=list(range(n_vals)),
            colorscale=[[0, "#4575b4"], [0.5, "white"], [1, "#d73027"]],
            showscale=False,
            line=dict(width=0.5, color="white"),
        ),
        name="Recent Pitches",
        hovertemplate=f"{value_col.capitalize()}: %{{x}}<extra></extra>",
    ))

    # Delta scale — tick marks above the zone bar showing Δ from the most recent value
    implied_delta = None
    if vals:
        last_val = vals[-1]
        implied_delta = circular_signed_delta(last_val, swing)
        for delta in [-400, -300, -200, -100, 0, 100, 200, 300, 400, 500]:
            abs_pos = ((last_val + delta - 1) % 1000) + 1
            lbl = "±500" if delta == 500 else (f"+{delta}" if delta > 0 else ("0" if delta == 0 else str(delta)))
            is_zero = delta == 0
            fig.add_shape(
                type="line", xref="x", yref="paper",
                x0=abs_pos, x1=abs_pos, y0=1.02, y1=1.09,
                line=dict(
                    color="rgba(210,210,210,0.95)" if is_zero else "rgba(140,140,140,0.7)",
                    width=1.5 if is_zero else 1,
                ),
            )
            fig.add_annotation(
                x=abs_pos, xref="x", y=1.10, yref="paper",
                text=lbl, showarrow=False,
                font=dict(size=11, color="rgba(180,180,180,0.95)"),
                xanchor="center", yanchor="bottom",
            )

        # Delta triangles above zone bar — project each historical delta from most recent value
        delta_col = f"{value_col}_circ_delta"
        if delta_col in df_last.columns:
            delta_raw = df_last[delta_col].tolist()
            top_x, top_idx, top_d = [], [], []
            for i, d in enumerate(delta_raw):
                if not pd.isna(d):
                    d_int = int(d)
                    top_x.append(((last_val + d_int - 1) % 1000) + 1)
                    top_idx.append(i)
                    top_d.append(d_int)
            if top_x:
                fig.add_trace(go.Scatter(
                    x=top_x,
                    y=[1.08] * len(top_x),
                    mode="markers",
                    marker=dict(
                        symbol="triangle-down", size=9,
                        color=top_idx,
                        colorscale=[[0, "#4575b4"], [0.5, "white"], [1, "#d73027"]],
                        cmin=0, cmax=n_vals - 1,
                        showscale=False,
                        line=dict(width=0.5, color="white"),
                    ),
                    text=[f"Δ{d:+d} → {x}" for d, x in zip(top_d, top_x)],
                    hovertemplate="%{text}<extra></extra>",
                    name="Recent Δ",
                    showlegend=True,
                ))

    # OBR boundary lines — offset clamped so labels stay on-screen at chart edges
    obr_max = max((hi for result, lo, hi in ranges if result in _OBR), default=0)
    if obr_max > 0:
        b_lo = ((swing - obr_max - 1) % 1000) + 1
        b_hi = ((swing + obr_max - 1) % 1000) + 1
        for boundary, default_ax in [(b_lo, -40), (b_hi, 40)]:
            ax = 40 if boundary < 120 else (-40 if boundary > 880 else default_ax)
            fig.add_vline(x=boundary, line_dash="dot", line_color="#1a7d35", line_width=1.5)
            fig.add_annotation(
                x=boundary, y=0.82,
                ax=ax, ay=0,
                text=str(boundary),
                showarrow=True, arrowhead=2, arrowsize=0.9, arrowwidth=2,
                arrowcolor="#1a7d35",
                font=dict(color="#1a7d35", size=10, weight="bold"),
                bgcolor="rgba(255,255,255,0.8)",
                borderpad=2,
            )

    # Reference value pill — same y as OBR labels (ay=0), white bg, green text
    pill_text = f"{ref_label} {swing}" + (f"<br>Δ{implied_delta:+d}" if implied_delta is not None else "")
    # Two-layer vline: dark outline first, white center on top → visible on both light and dark backgrounds
    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=swing, x1=swing, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=swing, y=0.82,
        text=pill_text,
        showarrow=False,
        xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)",
        borderpad=2,
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
        ),
        yaxis=dict(visible=False, range=[-0.18, 1.20]),
        height=420,
        margin=dict(l=10, r=25, t=75, b=120),
        legend=dict(
            orientation="h", x=0.5, y=-0.6,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
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
        group_cols = ["game_id", "pitcher_name"]

    n_buckets = 1000 // bucket_size
    labels = [f"{i * bucket_size + 1}-{min((i + 1) * bucket_size, 1000)}" for i in range(n_buckets)]

    df = df.sort_values(["game_id", "id"]).copy()
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
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ------------------------------------------------------------------ sheet import helpers

def _safe_int(val) -> int | None:
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def parse_inning(inning_str: str) -> tuple[int, str]:
    """Parse 'T1' → (1, 'top'), 'B3' → (3, 'bottom')."""
    s = str(inning_str).strip().upper()
    if s.startswith("T"):
        try:
            return int(s[1:]), "top"
        except ValueError:
            pass
    if s.startswith("B"):
        try:
            return int(s[1:]), "bottom"
        except ValueError:
            pass
    try:
        return int(s), "top"
    except ValueError:
        return 1, "top"


def _str(val) -> str:
    """Return a clean string or None for blank/nan values."""
    s = str(val).strip() if val is not None else ""
    return s if s and s.lower() != "nan" else None


def read_games_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Games' tab of a public Google Sheet and return a list of game dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Games')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    games = []
    for _, row in df.iterrows():
        # "Game#" column holds the 6-digit code (e.g. 130101); "GameID" is the short code (e.g. JJCC01)
        game_code = _str(row.get("Game#"))
        if not game_code or len(game_code) < 4:
            continue
        try:
            season = int(game_code[:2])
            session_num = int(game_code[2:4])
        except ValueError:
            continue
        away_abbrev = _str(row.get("Away")) or ""
        home_abbrev = _str(row.get("Home")) or ""

        hms = [
            _str(row.get(col))
            for col in ("Honorable Mention", "Honorable Mention.1", "Honorable Mention.2")
        ]

        games.append({
            "game_code": game_code,
            "game_id_short": _str(row.get("GameID")),
            "season": season,
            "session_number": session_num,
            "away_team": TEAM_ABBREV.get(away_abbrev, away_abbrev),
            "home_team": TEAM_ABBREV.get(home_abbrev, home_abbrev),
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "umpire": _str(row.get("Umpire Assignment")),
            "winning_pitcher": _str(row.get("Winning Pitcher")),
            "losing_pitcher": _str(row.get("Losing Pitcher")),
            "save_pitcher": _str(row.get("Save")),
            "player_of_game": _str(row.get("Player of the Game")),
            "honorable_mention_1": hms[0],
            "honorable_mention_2": hms[1],
            "honorable_mention_3": hms[2],
            "start_time": _str(row.get("Start")),
            "end_time": _str(row.get("End")),
            "last_play": _str(row.get("Last Play")),
            "last_inning": _str(row.get("Inning")),
            "last_result": _str(row.get("Last Result")),
            "win_team": _str(row.get("Win")),
            "loss_team": _str(row.get("Loss")),
            "league": _str(row.get("League")),
            "division": _str(row.get("Division")),
            "archive_sheet_id": _str(row.get("Archive Sheet ID")),
        })
    return games


def read_plays_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Plays (Converted)' tab and return a list of play dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Plays (Converted)')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    plays = []
    for _, row in df.iterrows():
        # "Play" = full play identifier like 130101001; "Game" = game code like 130101
        play_num = _safe_int(row.get("Play"))
        game_code = _str(row.get("Game"))
        if not play_num or not game_code:
            continue

        inning_num, half = parse_inning(_str(row.get("Inning")) or "T1")
        brc = _safe_int(row.get("BRC"))
        obc = BRC_TO_OBC.get(brc, "Empty") if brc is not None else "Empty"

        pitcher_name = _str(row.get("Pitcher"))
        batter_name = _str(row.get("Batter"))
        # OFF = offensive (batting) team; DEF = defensive (pitching) team
        off_abbrev = _str(row.get("OFF")) or ""
        def_abbrev = _str(row.get("DEF")) or ""
        pitch = _safe_int(row.get("Pitch #"))
        swing = _safe_int(row.get("Swing #"))
        result = _str(row.get("Result"))
        play_type = _str(row.get("PlayType"))

        if not pitcher_name or not batter_name or pitch is None or swing is None or not result:
            continue

        plays.append({
            "game_code": game_code,
            "inning_raw": _str(row.get("Inning")),
            "play_num": play_num,
            "outs": _safe_int(row.get("Outs")) or 0,
            "brc": brc,
            "off_team": TEAM_ABBREV.get(off_abbrev, off_abbrev),
            "def_team": TEAM_ABBREV.get(def_abbrev, def_abbrev),
            "play_type": play_type,
            "pitcher_name": pitcher_name,
            "pitch": pitch,
            "batter_name": batter_name,
            "swing": swing,
            "catcher_name": _str(row.get("Catcher")),
            "throw_num": _safe_int(row.get("Throw #")),
            "runner_name": _str(row.get("Runner")),
            "steal_num": _safe_int(row.get("Steal #")),
            "result": result,
            "runs": _safe_int(row.get("Runs")),
            "pitcher_id": _safe_int(row.get("Pitcher ID")),
            "batter_id": _safe_int(row.get("Batter ID")),
            "catcher_id": _safe_int(row.get("Catcher Id")),
            "runner_id": _safe_int(row.get("Runner ID")),
            "diff": _safe_int(row.get("Diff")),
            "session_num": _safe_int(row.get("Session #")),
            # app-only (set during sync)
            "inning": inning_num,
            "half": half,
            "obc": obc,
        })
    return plays


def read_teams_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Teams' tab and return a list of team dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    teams = []
    for _, row in df.iterrows():
        abbrev = _str(row.get("Abv"))
        if not abbrev:
            continue
        teams.append({
            "team_id": _str(row.get("Team ID")),
            "abbrev": abbrev,
            "location": _str(row.get("Location")),
            "team_name": _str(row.get("Team Name")),
            "role_id": _str(row.get("Role ID")),
            "hype_id": _str(row.get("Hype ID")),
            "league": _str(row.get("League")),
            "division": _str(row.get("Division")),
            "logo_url": _str(row.get("Logo URL")),
            "name": _str(row.get("Full Team")) or abbrev,
            "stadium": _str(row.get("Stadium")),
            "primary_hex": _str(row.get("Primary Hex")),
            "ballpark_url": _str(row.get("Ballpark URL")),
            "wins": _safe_int(row.get("W")),
            "losses": _safe_int(row.get("L")),
            "runs_scored": _safe_int(row.get("RS")),
            "runs_allowed": _safe_int(row.get("RA")),
        })
    return teams


def _parse_bool(val) -> bool | None:
    s = str(val).strip().lower() if val is not None else ""
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def read_players_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Players' tab by column position (headers are mostly blank)."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Players')}"
    )
    df = pd.read_csv(url, dtype=str, header=0)

    # Columns by position: ID, Team, GM, Gov Name, Last Name, Discord ID, Status,
    # Primary, Secondary, Hand, CON, EYE, PWR, SPD, MOV, CMD, VEL, AWR,
    # Discord Nickname, Rookie?
    _col = lambda i: df.iloc[:, i] if i < len(df.columns) else pd.Series([None] * len(df))

    df2 = pd.DataFrame({
        "player_id":       _col(0),
        "team":            _col(1),
        "gm":              _col(2),
        "name":            _col(3),
        "last_name":       _col(4),
        "discord_id":      _col(5),
        "status":          _col(6),
        "primary_pos":     _col(7),
        "secondary_pos":   _col(8),
        "hand":            _col(9),
        "con":             _col(10),
        "eye":             _col(11),
        "pwr":             _col(12),
        "spd":             _col(13),
        "mov":             _col(14),
        "cmd":             _col(15),
        "vel":             _col(16),
        "awr":             _col(17),
        "discord_nickname": _col(18),
        "is_rookie":       _col(19),
    })

    players = []
    for _, row in df2.iterrows():
        player_id = _safe_int(row["player_id"])
        name = _str(row["name"])
        team = _str(row["team"])
        if not player_id or not name or not team:
            continue
        players.append({
            "player_id":       player_id,
            "team":            team,
            "gm":              _parse_bool(row["gm"]),
            "name":            name,
            "last_name":       _str(row["last_name"]),
            "discord_id":      _str(row["discord_id"]),
            "status":          _str(row["status"]),
            "primary_pos":     _str(row["primary_pos"]),
            "secondary_pos":   _str(row["secondary_pos"]),
            "hand":            _str(row["hand"]),
            "con":             _safe_int(row["con"]),
            "eye":             _safe_int(row["eye"]),
            "pwr":             _safe_int(row["pwr"]),
            "spd":             _safe_int(row["spd"]),
            "mov":             _safe_int(row["mov"]),
            "cmd":             _safe_int(row["cmd"]),
            "vel":             _safe_int(row["vel"]),
            "awr":             _safe_int(row["awr"]),
            "discord_nickname": _str(row["discord_nickname"]),
            "is_rookie":       _parse_bool(row["is_rookie"]),
        })
    return players


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
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig

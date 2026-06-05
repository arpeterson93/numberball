"""Derived stats, constants, and chart helpers for Numberball."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ------------------------------------------------------------------ constants

TEAMS = ["Couriers", "Jammers", "Sharks", "Tridents"]

OBC_OPTIONS = ["000", "001", "010", "100", "011", "101", "110", "111"]

OBC_DISPLAY = {
    "000": "Empty",
    "001": "1B",
    "010": "2B",
    "100": "3B",
    "011": "1B&2B",
    "101": "1B&3B",
    "110": "2B&3B",
    "111": "Loaded",
}

def obc_display(code: str) -> str:
    """Return the friendly display name for a binary OBC code."""
    return OBC_DISPLAY.get(code, code)


def obc_circles(code: str) -> str:
    """Return 3 circle chars for 3rd/2nd/1st base occupancy (filled if runner present)."""
    return " ".join("●" if b == "1" else "○" for b in code)

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

# Run Expectancy Matrix: (outs, obc) -> expected runs
RUN_EXPECTANCY = {
    (0, "000"): 0.67, (0, "001"): 1.00, (0, "010"): 1.31, (0, "100"): 1.52,
    (0, "011"): 1.61, (0, "101"): 1.89, (0, "110"): 2.02, (0, "111"): 2.51,
    (1, "000"): 0.39, (1, "001"): 0.65, (1, "010"): 0.79, (1, "100"): 1.01,
    (1, "011"): 1.06, (1, "101"): 1.23, (1, "110"): 1.50, (1, "111"): 1.55,
    (2, "000"): 0.17, (2, "001"): 0.35, (2, "010"): 0.41, (2, "100"): 0.48,
    (2, "011"): 0.63, (2, "101"): 0.72, (2, "110"): 0.82, (2, "111"): 0.98,
}

# Result ranges: (result, diff_low, diff_high) - from the league result table
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
    # Hits - green spectrum (best → marginal)
    "HR":    "#1a7d35",
    "3B":    "#2ca02c",
    "2BWH":  "#57b857",
    "2B":    "#93d493",
    "1BWH2": "#aedda2",
    "1BWH":  "#c4e8a4",
    "1B":    "#e5f5c3",
    "IF1B":  "#fff7bc",
    "BB":    "#fee391",
    # Soft outs / sac - yellow → orange
    "GORA":  "#fec44f",
    "DSacF": "#fe9929",
    "DFO":   "#fd8c15",
    "SacF":  "#fd7a1a",
    "FO":    "#f56010",
    # Standard outs - orange-red → red
    "PO":    "#f03b20",
    "FCH":   "#d42020",
    "FC":    "#c42020",
    "FC3rd": "#b82020",
    "GO":    "#aa1020",
    "LO":    "#c8102e",
    "K":     "#b10026",
    # Double plays - dark red → maroon
    "DPRun": "#920026",
    "DP":    "#880026",
    "DP21":  "#800026",
    "DP31":  "#5a001a",
    "DPH1":  "#3d0014",
    # Line-out DPs / triple plays - near black
    "LODP":  "#2d000f",
    "TP":    "#220009",
    "LOTP":  "#180006",
}

# Bunt result -> swing equivalent for color lookup
_BUNT_TO_SWING: dict[str, str] = {
    "SacB":   "SacF",
    "DSacB":  "DSacF",
}

def _result_color(result: str) -> str:
    """Return the zone color for a result, mapping bunt variants to their swing equivalents."""
    if result in _RESULT_ZONE_COLORS:
        return _RESULT_ZONE_COLORS[result]
    # Explicit bunt overrides (e.g. SacB -> SacF)
    swing = _BUNT_TO_SWING.get(result)
    if swing:
        return _RESULT_ZONE_COLORS.get(swing, "#cccccc")
    # Generic B-prefix stripping: B1B -> 1B, BFC -> FC, BGO -> GO, etc.
    if result.startswith("B") and result[1:] in _RESULT_ZONE_COLORS:
        return _RESULT_ZONE_COLORS[result[1:]]
    return "#cccccc"


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
    0: "000", 1: "001", 2: "010", 3: "011",
    4: "100", 5: "101", 6: "110", 7: "111",
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


def _approach_group(g: pd.DataFrame) -> pd.Series:
    """1 if pitcher moved closer to prev batter's swing, 0 if further, NaN for first pitch."""
    pitches = g["pitch"].astype(int).tolist()
    swings = g["swing"].astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(g)):
        prev_dist = abs(circular_signed_delta(pitches[i - 1], swings[i - 1]))
        curr_dist = abs(circular_signed_delta(pitches[i], swings[i - 1]))
        results.append(1.0 if curr_dist < prev_dist else 0.0)
    return pd.Series(results, index=g.index)


def _wraparound_group(group: pd.Series) -> pd.Series:
    """1 if pitch crosses the 1000/1 border (850+ ↔ 150-), 0 otherwise, NaN for first."""
    vals = group.astype(int).tolist()
    results = [float("nan")]
    for i in range(1, len(vals)):
        prev, curr = vals[i - 1], vals[i]
        wrapped = (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850)
        results.append(1.0 if wrapped else 0.0)
    return pd.Series(results, index=group.index)


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


_OUT_RESULTS = {"GO", "FO", "PO", "K", "GORA", "DSacF", "FC", "LO",
                "LCO", "DFO", "FC3rd"}
_DP_RESULTS  = {"DP", "DPH1", "DP21", "DP31", "DPRun", "LODP", "BDP"}
_TP_RESULTS  = {"TP", "LOTP"}


def outs_added(result: str) -> int:
    if result in _TP_RESULTS:
        return 3
    if result in _DP_RESULTS:
        return 2
    if result in _OUT_RESULTS:
        return 1
    return 0


def get_expected_runs(outs: int, obc: str) -> float | None:
    """Look up expected runs for a given game state."""
    return RUN_EXPECTANCY.get((outs, obc))


def steal_advance(obc: str, outs: int) -> tuple[str, int]:
    """Return (new_obc, runs_scored) for a successful steal (lead runner advances one base)."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    runs = 0
    if on_3b and not on_2b and not on_1b:
        runs, n3, n2, n1 = 1, False, False, False
    elif on_3b and on_2b and not on_1b:
        runs, n3, n2, n1 = 1, False, True,  False
    elif on_3b and not on_2b and on_1b:
        runs, n3, n2, n1 = 1, False, False, True
    elif on_3b and on_2b and on_1b:
        runs, n3, n2, n1 = 1, False, True,  True
    elif on_2b and not on_1b:
        n3, n2, n1 = True, False, False
    elif on_2b and on_1b:
        n3, n2, n1 = True, False, True
    elif on_1b:
        n3, n2, n1 = False, True, False
    else:
        n3, n2, n1 = on_3b, on_2b, on_1b

    return f"{'1' if n3 else '0'}{'1' if n2 else '0'}{'1' if n1 else '0'}", runs


def advance_runners(result: str, obc: str, outs_before: int) -> tuple[str, int]:
    """Map a result to new OBC and runs scored.

    Returns (new_obc, runs_scored)
    """
    # Parse current runners from binary OBC code (3B|2B|1B)
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    runs = 0
    new_1b = False
    new_2b = False
    new_3b = False

    if result == "HR":
        runs = (1 if on_1b else 0) + (1 if on_2b else 0) + (1 if on_3b else 0) + 1
    elif result in ("3B",):
        runs = 1 if on_3b else 0
        new_3b = True
    elif result in ("2B", "2BWH"):
        runs = (1 if on_3b else 0) + (1 if on_2b else 0)
        new_3b = on_1b
        new_2b = True
    elif result in ("1B", "1BWH", "IF1B"):
        runs = 1 if on_3b else 0
        new_3b = on_2b
        new_2b = on_1b
        new_1b = True
    elif result == "BB":
        # Force chain: runner on 1B forced to 2B; if 2B also occupied, 2B runner forced to 3B;
        # if 3B also occupied AND 1B&2B both occupied, 3B runner scores.
        new_1b = True
        if on_1b:
            new_2b = True
            if on_2b:
                new_3b = True
                # 3B runner scores (runs tracked via run_lookup)
            else:
                new_3b = on_3b
        else:
            new_2b = on_2b
            new_3b = on_3b
    elif result == "SacF":
        runs = 1 if on_3b else 0
        new_3b = False
        new_2b = on_2b
        new_1b = on_1b
    elif result in _TP_RESULTS:
        pass  # triple play: all bases cleared, no new runners (new_1b/2b/3b stay False)
    elif result in _OUT_RESULTS:
        new_3b = on_3b
        new_2b = on_2b
        new_1b = on_1b

    return f"{'1' if new_3b else '0'}{'1' if new_2b else '0'}{'1' if new_1b else '0'}", runs


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
                f"Previous AB ({p_res}, {p_outs} outs) should have ended the half-inning - "
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
    if "play_type" in df.columns:
        df = df[df["play_type"].str.lower() != "steal"]
    if df.empty:
        return df
    df["half"] = df["half"].fillna("top")

    sw = df["pitch"].notna() & df["swing"].notna()

    # Recompute diff for swing plays; steals already have diff stored from sheet
    if sw.any():
        df.loc[sw, "diff"] = df.loc[sw].apply(
            lambda r: circular_diff(int(r["pitch"]), int(r["swing"])), axis=1
        )

    df["pitch_zone"] = df["pitch"].apply(lambda p: get_zone(int(p)) if pd.notna(p) else None)
    df["swing_zone"] = df["swing"].apply(lambda s: get_zone(int(s)) if pd.notna(s) else None)
    df["res_category"] = df.apply(
        lambda r: get_res_category(r["result"], int(r["diff"])) if pd.notna(r.get("diff")) else "OUT",
        axis=1,
    )
    df["is_meme_pitch"] = df["pitch"].isin(MEME_NUMBERS)
    df["is_meme_swing"] = df["swing"].isin(MEME_NUMBERS)
    df["pitch_last2"] = df["pitch"].apply(
        lambda p: int(str(int(p)).zfill(2)[-2:]) if pd.notna(p) else None
    )
    df["inning_label"] = df.apply(lambda r: inning_label(r["inning"], r["half"]), axis=1)

    df = df.sort_values(["game_id", "id"])
    df["is_fp_inn"] = ~df.duplicated(subset=["game_id", "inning", "half"], keep="first")
    df["is_fp_app"] = ~df.duplicated(subset=["game_id", "pitcher_name"], keep="first")

    # Deltas only meaningful for swing plays
    df["pitch_delta"] = pd.NA
    df["pitch_circ_delta"] = pd.NA
    df["swing_circ_delta"] = pd.NA
    df["pitch_circ_delta2"] = pd.NA
    df["pitch_approach"] = pd.NA
    df["pitch_wraparound"] = pd.NA
    if sw.any():
        sw_df = df[sw]
        df.loc[sw, "pitch_delta"] = sw_df.groupby(["game_id", "pitcher_name"])["pitch"].diff()
        df.loc[sw, "pitch_circ_delta"] = sw_df.groupby(
            ["game_id", "pitcher_name"], group_keys=False
        )["pitch"].apply(_circ_delta_group)
        df.loc[sw, "swing_circ_delta"] = sw_df.groupby(
            ["game_id", "batter_name"], group_keys=False
        )["swing"].apply(_circ_delta_group)
        df.loc[sw, "pitch_wraparound"] = sw_df.groupby(
            ["game_id", "pitcher_name"], group_keys=False
        )["pitch"].apply(_wraparound_group)
        # Second derivative and approach - re-read df to pick up pitch_circ_delta
        sw_df2 = df[sw]
        df.loc[sw, "pitch_circ_delta2"] = sw_df2.groupby(
            ["game_id", "pitcher_name"], group_keys=False
        )["pitch_circ_delta"].apply(lambda g: g.abs().diff().abs())
        df.loc[sw, "pitch_approach"] = sw_df2.groupby(
            ["game_id", "pitcher_name"], group_keys=False
        )[["pitch", "swing"]].apply(_approach_group)

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
            # Re-derive off_team/def_team from game records (already full names)
            if row.get("half") == "top":
                row["off_team"] = g.get("away_team")
                row["def_team"] = g.get("home_team")
            else:
                row["off_team"] = g.get("home_team")
                row["def_team"] = g.get("away_team")
        rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flatten_scrimmage(plays: list[dict]) -> pd.DataFrame:
    """Convert flat scrimmage_plays rows into a DataFrame matching flatten_games schema."""
    if not plays:
        return pd.DataFrame()
    df = pd.DataFrame(plays)
    # Synthesize a numeric game_id from scrimmage_code so Scouting filters work
    if "scrimmage_code" in df.columns:
        df["game_id"] = pd.factorize(df["scrimmage_code"])[0] + 1
        df["game_code"] = df["scrimmage_code"]
    # Apply TEAM_ABBREV so team names are always full names
    for _tc in ("def_team", "off_team"):
        if _tc in df.columns:
            df[_tc] = df[_tc].map(lambda t: TEAM_ABBREV.get(t, t) if pd.notna(t) else t)
    return df


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
    highlight_name: str | None = None,
    segment_games: bool = False,
) -> go.Figure:
    """Two-row subplot: pitch+swing lines on top, circular delta bars on bottom, shared x-axis.
    swing_offset: shifts swing markers right by 1 AB to show whether swing predicts next pitch.
    highlight_name: swing markers for that batter use a star symbol.
    segment_games: breaks lines at game boundaries; dashes lines across inning boundaries.
    """
    df_last = df[df["pitch"].notna() & df["swing"].notna()].sort_values("id").tail(n).reset_index(drop=True)
    n_actual = len(df_last)
    x_all = list(range(1, n_actual + 1))
    pitches = df_last["pitch"].astype(int).tolist()
    swings  = df_last["swing"].astype(int).tolist()
    results = df_last["result"].tolist() if "result" in df_last.columns else [None] * n_actual
    delta_vals = df_last[delta_col].dropna().astype(int).tolist()

    deltas = [circular_signed_delta(delta_vals[i - 1], delta_vals[i]) for i in range(1, n_actual)]
    linear = [delta_vals[i] - delta_vals[i - 1] for i in range(1, n_actual)]
    x_delta = list(range(2, n_actual + 1))
    colors = ["#4CAF50" if d >= 0 else "#d6604d" for d in deltas]
    hover = [
        f"PA {i}: {delta_vals[i-1]}→{delta_vals[i]}<br>Circular: {deltas[i-1]:+d}<br>Linear: {linear[i-1]:+d}"
        for i in range(1, n_actual)
    ]

    if swing_offset and n_actual > 1:
        swing_x    = list(range(2, n_actual + 1))
        swing_y    = swings[:-1]
        result_offset = results[1:]
        swing_text = [str(s) for s in swing_y]
        n_swing_rows = n_actual - 1
        highlight_mask = (
            df_last["batter_name"].iloc[:-1].eq(highlight_name).tolist()
            if highlight_name and "batter_name" in df_last.columns else [False] * len(swing_x)
        )
    else:
        swing_x    = x_all
        swing_y    = swings
        swing_text = [str(s) for s in swings]
        result_offset = results
        n_swing_rows = n_actual
        highlight_mask = (
            df_last["batter_name"].eq(highlight_name).tolist()
            if highlight_name and "batter_name" in df_last.columns else [False] * n_actual
        )

    # ── segmentation helper ───────────────────────────────────────────────────
    can_segment = (
        segment_games
        and n_actual > 0
        and all(c in df_last.columns for c in ("game_id", "inning", "half"))
    )

    def _segs(xs, ys, n_rows):
        """Return (solid_x, solid_y, dash_x, dash_y).
        Inserts None into solid at game breaks; adds dashed connector pairs for inning breaks.
        n_rows: number of df_last rows that correspond to entries in xs/ys.
        """
        sx, sy, dx, dy = [], [], [], []
        for i, (xi, yi) in enumerate(zip(xs, ys)):
            sx.append(xi); sy.append(yi)
            if i < len(xs) - 1 and i < n_rows - 1:
                r0 = df_last.iloc[i]
                r1 = df_last.iloc[i + 1]
                same_game = r0["game_id"] == r1["game_id"]
                same_inn  = (r0["inning"] == r1["inning"] and r0["half"] == r1["half"])
                if not same_game:
                    sx.append(None); sy.append(None)
                elif not same_inn:
                    sx.append(None); sy.append(None)
                    dx += [xi, xs[i + 1], None]
                    dy += [yi, ys[i + 1], None]
        return sx, sy, dx, dy

    def _text(ys):
        return ["" if v is None else str(int(v)) for v in ys]

    # ── build figure ──────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35], vertical_spacing=0.06,
    )

    # Pitch trace
    if can_segment:
        p_sx, p_sy, p_dx, p_dy = _segs(x_all, pitches, n_actual)
        fig.add_trace(go.Scatter(
            x=p_sx, y=p_sy, mode="lines+markers+text", name="Pitch",
            legendgroup="pitch",
            text=_text(p_sy), textposition="top center", textfont=dict(size=10),
            line=dict(color="#d6604d", width=2), marker=dict(size=5),
        ), row=1, col=1)
        if p_dx:
            fig.add_trace(go.Scatter(
                x=p_dx, y=p_dy, mode="lines",
                legendgroup="pitch", showlegend=False, hoverinfo="skip",
                line=dict(color="#d6604d", width=2, dash="dash"),
            ), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(
            x=x_all, y=pitches, mode="lines+markers+text", name="Pitch",
            legendgroup="pitch",
            text=[str(p) for p in pitches], textposition="top center",
            textfont=dict(size=10), line=dict(color="#d6604d", width=2), marker=dict(size=5),
        ), row=1, col=1)

    # Swing trace
    swing_name = "Swing" + (" (offset +1)" if swing_offset else "")
    if highlight_name and any(highlight_mask):
        # Connecting line (segmented or plain), then separate marker traces
        if can_segment:
            s_sx, s_sy, s_dx, s_dy = _segs(swing_x, swing_y, n_swing_rows)
            fig.add_trace(go.Scatter(
                x=s_sx, y=s_sy, mode="lines", name=swing_name,
                legendgroup="swing",
                line=dict(color="#2166ac", width=2), showlegend=True, hoverinfo="skip",
            ), row=1, col=1)
            if s_dx:
                fig.add_trace(go.Scatter(
                    x=s_dx, y=s_dy, mode="lines",
                    legendgroup="swing", showlegend=False, hoverinfo="skip",
                    line=dict(color="#2166ac", width=2, dash="dash"),
                ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=swing_x, y=swing_y, mode="lines", name=swing_name,
                legendgroup="swing",
                line=dict(color="#2166ac", width=2), showlegend=True, hoverinfo="skip",
            ), row=1, col=1)
        _cx = [x for x, h in zip(swing_x, highlight_mask) if not h]
        _cy = [y for y, h in zip(swing_y, highlight_mask) if not h]
        _ct = [t for t, h in zip(swing_text, highlight_mask) if not h]
        if _cx:
            fig.add_trace(go.Scatter(
                x=_cx, y=_cy, mode="markers+text", text=_ct,
                legendgroup="swing",
                textposition="bottom center", textfont=dict(size=10),
                marker=dict(size=5, color="#2166ac"),
                showlegend=False, name=swing_name, hoverinfo="skip",
            ), row=1, col=1)
        _hx = [x for x, h in zip(swing_x, highlight_mask) if h]
        _hy = [y for y, h in zip(swing_y, highlight_mask) if h]
        _ht = [t for t, h in zip(swing_text, highlight_mask) if h]
        if _hx:
            fig.add_trace(go.Scatter(
                x=_hx, y=_hy, mode="markers+text", text=_ht,
                legendgroup="swing",
                textposition="bottom center", textfont=dict(size=10),
                marker=dict(symbol="star", size=10, color="#2166ac",
                            line=dict(color="white", width=0.5)),
                showlegend=False, name=swing_name, hoverinfo="skip",
            ), row=1, col=1)
    else:
        if can_segment:
            s_sx, s_sy, s_dx, s_dy = _segs(swing_x, swing_y, n_swing_rows)
            fig.add_trace(go.Scatter(
                x=s_sx, y=s_sy, mode="lines+markers+text", name=swing_name,
                legendgroup="swing",
                text=_text(s_sy), textposition="bottom center", textfont=dict(size=10),
                line=dict(color="#2166ac", width=2), marker=dict(size=5),
            ), row=1, col=1)
            if s_dx:
                fig.add_trace(go.Scatter(
                    x=s_dx, y=s_dy, mode="lines",
                    legendgroup="swing", showlegend=False, hoverinfo="skip",
                    line=dict(color="#2166ac", width=2, dash="dash"),
                ), row=1, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=swing_x, y=swing_y, mode="lines+markers+text", name=swing_name,
                legendgroup="swing",
                text=swing_text, textposition="bottom center",
                textfont=dict(size=10), line=dict(color="#2166ac", width=2), marker=dict(size=5),
            ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=x_delta, y=[abs(d) for d in deltas], marker_color=colors,
        text=[f"{d:+d}" for d in deltas], textposition="outside",
        textfont=dict(size=10), hovertext=hover, hoverinfo="text",
        name="Delta", showlegend=False,
    ), row=2, col=1)

    # Add result labels as text below the delta bars
    result_text = [str(r) if r else "" for r in result_offset]
    result_x = list(range(1, len(result_offset) + 1)) if not swing_offset else list(range(2, len(result_offset) + 2))
    fig.add_trace(go.Scatter(
        x=result_x, y=[-20] * len(result_x),
        mode="text",
        text=result_text,
        textposition="bottom center",
        textfont=dict(size=9, color="gray"),
        showlegend=False,
        hoverinfo="skip",
        xaxis="x2", yaxis="y2",
    ), row=2, col=1)

    x_range = [0.5, n_actual + 0.5]
    fig.update_xaxes(tickmode="linear", dtick=1, range=x_range, showticklabels=False, row=1, col=1)
    fig.update_xaxes(
        title_text="← Older  ·  PA #  ·  Newer →",
        tickmode="linear", dtick=1, range=x_range, row=2, col=1,
    )
    fig.update_yaxes(range=[0, 1080], row=1, col=1)
    fig.update_yaxes(
        range=[-60, 540], title_text="Delta", row=2, col=1,
        tickmode="array", tickvals=[100, 200, 300, 400, 500],
    )

    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=45, r=10, t=60, b=60),
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


def parse_result_ranges_from_sheet(sheet_url: str):
    """Fetch and parse result range tables from a public Google Sheet.

    Returns (normal_ranges, bunt_ranges, batter_name, pitcher_name).
    bunt_ranges is None if no second Result table is found.
    """
    import re
    sheet_id_match = re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    gid_match = re.search(r"[?&]gid=(\d+)", sheet_url)
    if not sheet_id_match:
        raise ValueError("Could not parse a Google Sheets ID from the URL.")
    sheet_id = sheet_id_match.group(1)
    gid = gid_match.group(1) if gid_match else "0"
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

    raw = pd.read_csv(csv_url, header=None, dtype=str)

    def _parse_table(start_row, col):
        low_col, high_col = col + 2, col + 3
        out: list[tuple[str, int, int]] = []
        for i in range(start_row + 1, len(raw)):
            name = str(raw.iloc[i, col]).strip()
            if not name or name.lower() == "nan":
                break
            try:
                lo = int(float(str(raw.iloc[i, low_col]).strip()))
                hi = int(float(str(raw.iloc[i, high_col]).strip()))
                out.append((name, lo, hi))
            except (ValueError, IndexError):
                break
        return out or None

    # Find all "Result" header cells
    result_headers: list[tuple[int, int]] = []
    for i in range(len(raw)):
        for j in range(len(raw.columns)):
            if str(raw.iloc[i, j]).strip().lower() == "result":
                result_headers.append((i, j))

    if not result_headers:
        raise ValueError("Could not find a 'Result' header in the sheet.")

    normal_ranges = _parse_table(*result_headers[0])
    if not normal_ranges:
        raise ValueError("Result table found but no rows could be parsed.")

    bunt_ranges = _parse_table(*result_headers[1]) if len(result_headers) > 1 else None

    def _cell(r, c):
        try:
            v = str(raw.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else ""
        except Exception:
            return ""

    batter_name  = _cell(11, 7)
    pitcher_name = _cell(10, 7)

    return normal_ranges, bunt_ranges, batter_name, pitcher_name


_OBC_CODE_TO_STRING = {
    0: "000",
    1: "001",
    2: "010",
    3: "100",
    4: "011",
    5: "101",
    6: "110",
    7: "111",
}


def parse_gameplay_from_sheet(sheet_url: str) -> dict:
    """Fetch game state from two Gameplay sheet tabs.

    Outs: gid 1498066521 (Gameday), L8 (row 7, col 11)
    Runners: gid 533199361, S6/T6/U6 (row 5, cols 18/19/20) - non-zero = runner present

    Returns dict with keys: outs (int|None), obc (str|None), steal_runners (list).
    """
    import re as _re
    sheet_id_match = _re.search(r"/spreadsheets/d/([^/]+)", sheet_url)
    if not sheet_id_match:
        return {"outs": None, "obc": None, "steal_runners": []}
    sheet_id = sheet_id_match.group(1)
    base_url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/export?format=csv&gid="

    def _read_tab(gid):
        try:
            return pd.read_csv(base_url + gid, header=None, dtype=str)
        except Exception:
            return None

    def _cell(raw, r, c):
        try:
            v = str(raw.iloc[r, c]).strip()
            return v if v.lower() not in ("nan", "") else None
        except IndexError:
            return None

    # --- Outs from runner tab (gid 533199361), X9 (row 8, col 23) ---
    outs = None
    raw_gameday = _read_tab("1498066521")  # kept for steal ranges

    # --- Runners + outs from gid 533199361 ---
    # Outs: X9 (row 8, col 23); Runners: S6/T6/U6 (row 5, cols 18/19/20)
    on_1b = on_2b = on_3b = False
    raw_runners = _read_tab("533199361")
    if raw_runners is not None:
        _outs_raw = _cell(raw_runners, 8, 23)
        if _outs_raw:
            _m = _re.search(r'\d+', _outs_raw)
            if _m:
                outs = int(_m.group())
        s6 = _cell(raw_runners, 5, 18)  # S6 - runner on 1B
        t6 = _cell(raw_runners, 5, 19)  # T6 - runner on 2B
        u6 = _cell(raw_runners, 5, 20)  # U6 - runner on 3B
        on_1b = s6 is not None and s6 != "0"
        on_2b = t6 is not None and t6 != "0"
        on_3b = u6 is not None and u6 != "0"

    obc = f"{'1' if on_3b else '0'}{'1' if on_2b else '0'}{'1' if on_1b else '0'}"

    # Steal runner stubs (base only - safe range lookup TBD)
    runners = []
    base_order = {"3B": 0, "2B": 1, "1B": 2}
    for base, present in [("3B", on_3b), ("2B", on_2b), ("1B", on_1b)]:
        if present:
            runners.append({"base": base, "safe_range": _default_safe_rng_for(base, raw_runners)})
    runners.sort(key=lambda r: base_order.get(r["base"], 9))

    return {"outs": outs, "obc": obc, "steal_runners": runners}


def _default_safe_rng_for(base: str, raw_runners) -> int:
    """Read steal safe range from AB20:AB22 on runner tab (gid 533199361); default 50."""
    if raw_runners is None:
        return 50
    base_to_row = {"1B": 19, "2B": 20, "3B": 21}
    row_idx = base_to_row.get(base, 21)
    try:
        v = str(raw_runners.iloc[row_idx, 27]).strip()
        if v.lower() not in ("nan", ""):
            return int(float(v))
    except (IndexError, ValueError):
        pass
    return 50


def load_run_lookup_from_csv(csv_path: str = "import_BRC.csv") -> dict[tuple[str, str, int], tuple[float, str, int]]:
    """Load runs, after-OBC, and eOuts from import_BRC.csv.

    Returns dict mapping (result, before_obc, outs) -> (runs_scored, new_obc_code, eouts).
    eOuts = outs added by the play (0=hit/walk, 1=single out, 2=double play).
    Key includes outs because end-of-inning rows clear runners differently.
    """
    import os
    if not os.path.exists(csv_path):
        return {}

    df = pd.read_csv(csv_path)
    lookup: dict[tuple[str, str, int], tuple[float, str, int]] = {}

    if "Situation" not in df.columns or "Runs" not in df.columns:
        return {}

    cols = list(df.columns)

    for _, row in df.iterrows():
        situation = str(row["Situation"]).strip()
        try:
            runs   = float(row["Runs"])
            eouts  = int(float(row["eOuts"])) if "eOuts" in cols else 0
        except (ValueError, TypeError):
            continue

        parts = situation.split("_")
        if len(parts) < 3:
            continue
        try:
            outs      = int(parts[0])
            obc_code  = int(parts[1])
            result    = "_".join(parts[2:])
            before_obc = _OBC_CODE_TO_STRING.get(obc_code, "000")
        except (ValueError, KeyError):
            continue

        try:
            new_obc = _OBC_CODE_TO_STRING[int(float(row["OBC"]))]
        except (ValueError, TypeError, KeyError):
            continue

        lookup[(result, before_obc, outs)] = (runs, new_obc, eouts)

    return lookup


def project_from_deltas(recent_vals: list[int]) -> list[int]:
    """Apply each recent circular delta to the most recent value to get projected next positions."""
    if len(recent_vals) < 2:
        return []
    last_val = recent_vals[-1]
    return [((last_val + circular_signed_delta(recent_vals[i - 1], recent_vals[i]) - 1) % 1000) + 1
            for i in range(1, len(recent_vals))]


def project_from_delta2s(recent_vals: list[int]) -> list[int]:
    """Apply delta2 (pitch movement acceleration) patterns to project positions."""
    if len(recent_vals) < 3:
        return []
    deltas = [circular_signed_delta(recent_vals[i - 1], recent_vals[i]) for i in range(1, len(recent_vals))]
    delta2s = [abs(abs(deltas[i]) - abs(deltas[i - 1])) for i in range(1, len(deltas))]
    last_val = recent_vals[-1]
    last_delta = deltas[-1]
    return [((last_val + int(last_delta + (delta2s[i] if i < len(delta2s) else delta2s[-1])) - 1) % 1000) + 1
            for i in range(len(delta2s))]


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
    x_label: str = "Pitch Values",
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
        color = _result_color(result)
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

    # Tick marks - triangles beneath the colored zone, blue=oldest → white → red=newest
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

    # Delta scale - tick marks above the zone bar showing Δ from the most recent value
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
                    color="rgba(128,128,128,0.9)" if is_zero else "rgba(128,128,128,0.5)",
                    width=1.5 if is_zero else 1,
                ),
            )
            fig.add_annotation(
                x=abs_pos, xref="x", y=1.10, yref="paper",
                text=lbl, showarrow=False,
                font=dict(size=11),
                xanchor="center", yanchor="bottom",
            )

        # Delta triangles above zone bar - project each historical delta from most recent value
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

    # OBR boundary lines - offset clamped so labels stay on-screen at chart edges
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

    # Reference value pill - same y as OBR labels (ay=0), white bg, green text
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

    # Top axis label ("Pitch Δ" / "Swing Δ") - positioned above the delta tick marks
    delta_axis_label = x_label.replace("Values", "Δ").replace("Value", "Δ")
    fig.add_annotation(
        x=500, xref="x", y=1.20, yref="paper",
        text=f"<b>{delta_axis_label}</b>",
        showarrow=False,
        font=dict(size=11),
        xanchor="center", yanchor="bottom",
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.18, 1.25]),
        height=440,
        margin=dict(l=10, r=25, t=90, b=130),
        legend=dict(
            orientation="h", x=0.5, y=-0.55,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def _normalize_ranges(raw: list) -> list[tuple[str, int, int]]:
    """Convert list of dicts or 3-tuples into a uniform list of (result, lo, hi) tuples."""
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append((entry["result"], entry["low"], entry["high"]))
        else:
            out.append(tuple(entry))
    return out


def manager_color_bar(proposed_value: int, result_ranges: list | None = None,
                      label: str = "Swing", x_label: str = "Swing Values") -> go.Figure:
    """Color-coded number line matching swing_predictor_chart style, without triangles or delta scale."""
    ranges = _normalize_ranges(result_ranges) if result_ranges else RESULT_RANGES
    pitch_result = [_diff_to_result(circular_diff(p, proposed_value), ranges) for p in range(1, 1001)]

    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    diff_info = {r: (lo, hi, hi - lo + 1) for r, lo, hi in ranges}

    seen: set[str] = set()
    for result, lo, hi in zones:
        color = _result_color(result)
        if result not in seen:
            d_lo, d_hi, w = diff_info.get(result, (0, 0, 0))
            _legend_lbl = f"{result}: {d_lo}-{d_hi} ({w})"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=_legend_lbl, showlegend=True,
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

    obr_max = max((hi for result, lo, hi in ranges if result in _OBR), default=0)
    if obr_max > 0:
        b_lo = ((proposed_value - obr_max - 1) % 1000) + 1
        b_hi = ((proposed_value + obr_max - 1) % 1000) + 1
        for boundary, default_ax in [(b_lo, -40), (b_hi, 40)]:
            ax = 40 if boundary < 120 else (-40 if boundary > 880 else default_ax)
            fig.add_vline(x=boundary, line_dash="dot", line_color="#1a7d35", line_width=1.5)
            fig.add_annotation(
                x=boundary, y=0.82, ax=ax, ay=0, text=str(boundary),
                showarrow=True, arrowhead=2, arrowsize=0.9, arrowwidth=2,
                arrowcolor="#1a7d35",
                font=dict(color="#1a7d35", size=10, weight="bold"),
                bgcolor="rgba(255,255,255,0.8)", borderpad=2,
            )

    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=proposed_value, x1=proposed_value, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=proposed_value, y=0.82, text=f"{label} {proposed_value}",
        showarrow=False, xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)", borderpad=2,
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.1, 1.1]),
        height=260,
        margin=dict(l=10, r=25, t=10, b=130),
        legend=dict(
            orientation="h", x=0.5, y=-0.65,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def bases_diamond_fig(obc: str, outs: int) -> go.Figure:
    """Broadcast-style base diamond with occupied bases highlighted in gold."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    gold        = "#FFD700"
    gold_border = "#FFA500"
    empty       = "#2d2d2d"
    empty_border = "#666666"

    fig = go.Figure()

    # Basepath outline
    fig.add_trace(go.Scatter(
        x=[0.5, 1.0, 0.5, 0.0, 0.5],
        y=[0.0, 0.5, 1.0, 0.5, 0.0],
        mode="lines",
        line=dict(color="#555555", width=1.5),
        showlegend=False, hoverinfo="skip",
    ))

    # Base markers: home=bottom, 1B=right, 2B=top, 3B=left
    for x, y, occupied, label in [
        (0.5, 1.0, on_2b, "2B"),
        (1.0, 0.5, on_1b, "1B"),
        (0.0, 0.5, on_3b, "3B"),
        (0.5, 0.0, False, "H"),
    ]:
        fig.add_trace(go.Scatter(
            x=[x], y=[y],
            mode="markers",
            marker=dict(
                symbol="square",
                size=16,
                color=gold if occupied else empty,
                line=dict(color=gold_border if occupied else empty_border, width=2),
                angle=45,
            ),
            showlegend=False, hoverinfo="skip",
        ))

    # Outs dots below home plate
    for i in range(3):
        filled = i < outs
        fig.add_trace(go.Scatter(
            x=[0.35 + i * 0.15], y=[-0.28],
            mode="markers",
            marker=dict(
                symbol="circle",
                size=8,
                color="#FFD700" if filled else "#2d2d2d",
                line=dict(color="#888888", width=1.5),
            ),
            showlegend=False, hoverinfo="skip",
        ))

    fig.update_layout(
        xaxis=dict(visible=False, range=[-0.25, 1.25]),
        yaxis=dict(visible=False, range=[-0.45, 1.25]),
        width=115, height=130,
        margin=dict(l=0, r=8, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
        showlegend=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def bases_diamond_svg(obc: str, outs: int) -> str:
    """Fixed-size SVG base diamond - immune to Plotly/Streamlit resize events."""
    on_3b = obc[0] == "1"
    on_2b = obc[1] == "1"
    on_1b = obc[2] == "1"

    gold, g_bdr  = "#FFD700", "#FFA500"
    empty, e_bdr = "#2d2d2d", "#666666"

    # Base positions (115×130 canvas):  2B=top, 1B=right, 3B=left, home=bottom
    home   = (57, 96)
    first  = (96, 57)
    second = (57, 19)
    third  = (19, 57)

    def base(cx, cy, filled):
        r = 9
        pts = f"{cx},{cy-r} {cx+r},{cy} {cx},{cy+r} {cx-r},{cy}"
        c, b = (gold, g_bdr) if filled else (empty, e_bdr)
        return f'<polygon points="{pts}" fill="{c}" stroke="{b}" stroke-width="2.5"/>'

    def ln(p1, p2):
        return (f'<line x1="{p1[0]}" y1="{p1[1]}" '
                f'x2="{p2[0]}" y2="{p2[1]}" stroke="#555" stroke-width="1.5"/>')

    path  = ln(home, first) + ln(first, second) + ln(second, third) + ln(third, home)
    bases = base(*home, False) + base(*first, on_1b) + base(*second, on_2b) + base(*third, on_3b)
    dots  = "".join(
        f'<circle cx="{46 + i*11}" cy="117" r="5" '
        f'fill="{"#FFD700" if i < outs else "#2d2d2d"}" stroke="#888" stroke-width="1.5"/>'
        for i in range(3)
    )
    return f'<svg width="115" height="130" xmlns="http://www.w3.org/2000/svg">{path}{bases}{dots}</svg>'


def steal_color_bar(proposed_value: int, safe_range: int,
                    label: str = "Steal", x_label: str = "Steal Values") -> go.Figure:
    """Color bar for a steal attempt: Safe zone vs Out zone."""
    safe_color = "#2ca02c"
    out_color  = "#b10026"

    pitch_result = [
        "Safe" if circular_diff(p, proposed_value) <= safe_range else "Out"
        for p in range(1, 1001)
    ]

    zones: list[tuple[str, int, int]] = []
    curr, lo = pitch_result[0], 1
    for p, r in enumerate(pitch_result[1:], 2):
        if r != curr:
            zones.append((curr, lo, p - 1))
            curr, lo = r, p
    zones.append((curr, lo, 1000))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0.5, 1000.5], y=[0.5, 0.5],
        mode="markers", marker=dict(opacity=0),
        showlegend=False, hoverinfo="skip",
    ))

    seen: set[str] = set()
    for result, lo, hi in zones:
        color = safe_color if result == "Safe" else out_color
        if result not in seen:
            prob = round(safe_range * 2 / 1000 * 100, 1)
            _legend_lbl = f"Safe: diff <= {safe_range} ({prob}%)" if result == "Safe" else f"Out: diff > {safe_range}"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=_legend_lbl, showlegend=True,
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

    for _lw, _lc in [(3, "rgba(0,0,0,0.28)"), (1.5, "rgba(255,255,255,0.88)")]:
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=proposed_value, x1=proposed_value, y0=0, y1=1,
                      line=dict(color=_lc, width=_lw, dash="dash"))
    fig.add_annotation(
        x=proposed_value, y=0.82, text=f"{label} {proposed_value}",
        showarrow=False, xanchor="center", yanchor="middle",
        font=dict(color="#1a7d35", size=10, weight="bold"),
        bgcolor="rgba(255,255,255,0.9)", borderpad=2,
    )

    fig.update_layout(
        xaxis=dict(
            range=[0.5, 1000.5],
            tickmode="array",
            tickvals=[1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
            tickfont=dict(size=11),
            title=dict(text=f"<b>{x_label}</b>", font=dict(size=11), standoff=8),
        ),
        yaxis=dict(visible=False, range=[-0.1, 1.1]),
        height=260,
        margin=dict(l=10, r=25, t=10, b=80),
        legend=dict(
            orientation="h", x=0.5, y=-0.45,
            xanchor="center", yanchor="top",
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=9, family="monospace"),
        ),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


# ------------------------------------------------------------------ at-bat range calculator
# Derived from MLN Calculator 11.0 formulas (calculator tab).
# OBR helper table: range widths at each differential -5..+5 for each result type.
_DIFFS = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]

_OBR_TABLE: dict[str, list[int]] = {
    "Hit":    [84, 99, 110, 119, 126, 132, 138, 145, 154, 165, 180],
    "HR":     [1,  1,  8,  16,  18,  20,  22,  24,  32,  47,  62],
    "3B":     [1,  1,  3,   4,   5,   6,   7,   8,   9,  11,  14],
    "2B":     [15, 20, 24,  27,  29,  30,  31,  33,  36,  40,  45],
    "IF1B":   [1,  2,  6,   8,   9,  10,  11,  12,  14,  18,  24],
    "BB":     [1,  3, 14,  23,  30,  35,  40,  47,  56,  67,  78],
    "FO_HND": [147,132,121,112, 105, 100,  95,  88,  79,  68,  53],  # /500 = FO%
    "PO_HND": [188,171,158,146, 135, 125, 115, 104,  92,  79,  62],  # /500 = PO%
    "K":      [183,160,142,127, 115, 105,  95,  83,  68,  50,  27],
}

# 1B extra adjustments (rows 40-43 of calculator tab)
_1B_SPD_AWR = {-5:-3,-4:-3,-3:-2,-2:-2,-1:-1,0:0,1:1,2:2,3:2,4:3,5:3}
_1B_PITCH_AWR = {-3:3,-2:2,-1:1,0:0,1:-1,2:-2,3:-3}  # keyed by pitcher_awr-3, clamped -3..3
_1B_HIT_NEG = {-5:5,-4:5,-3:5,-2:5,-1:5,0:3,1:0,2:0,3:0,4:0,5:0}  # row 43

# Steal safe-range table: differential -5..+5 in 0.5 steps (21 values)
STEAL_DIFFS = [-5,-4.5,-4,-3.5,-3,-2.5,-2,-1.5,-1,-0.5,0,0.5,1,1.5,2,2.5,3,3.5,4,4.5,5]
STEAL_TABLE: dict[str, list[int]] = {
    "2nd":  [62,86,108,132,154,177,199,221,242,265,285,308,329,351,373,396,418,442,464,488,499],
    "3rd":  [7,19,32,48,61,76,87,100,110,120,130,140,150,163,174,189,202,218,233,251,259],
    "home": [2,4,7,11,14,18,20,22,23,25,25,27,28,30,32,36,39,43,47,53,55],
}

RESULT_COLORS = {
    "HR":    "#e74c3c",
    "3B":    "#e67e22",
    "2B":    "#f1c40f",
    "1B":    "#2ecc71",
    "IF1B":  "#1abc9c",
    "BB":    "#3498db",
    "FO":    "#bdc3c7",
    "PO":    "#95a5a6",
    "GO":    "#7f8c8d",
    "K":     "#2c3e50",
    "LO":    "#6c7a7d",
    "B1BWH": "#27ae60",
    "B1B":   "#52be80",
    "BFC":   "#7f8c8d",
    "SacB":  "#a9cce3",
    "BDP":   "#5d6d7e",
}


def _obr_lookup(key: str, diff: int) -> int:
    idx = _DIFFS.index(max(-5, min(5, diff)))
    return _OBR_TABLE[key][idx]


def compute_at_bat_ranges(
    pitcher_hand: str,
    pitcher_mov: int,
    pitcher_cmd: int,
    pitcher_vel: int,
    pitcher_awr: int,
    batter_hand: str,
    batter_con: int,
    batter_eye: int,
    batter_pow: int,
    batter_spd: int,
    bunt: bool = False,
    hit_and_run: bool = False,
    outs: int = 0,
    runners_on: bool = False,
) -> list[dict]:
    """Compute at-bat result ranges from pitcher/batter stats.

    Returns list of dicts: {result, range, low, high}.
    Verified against MLN Calculator 11.0 (Fat Lever vs Cody Anderson example).
    """
    import math

    def clamp(v, lo, hi):
        return max(lo, min(hi, int(v)))

    # Handedness modifier
    if str(batter_hand).upper() == "S":
        hnd = 1.0
    elif str(pitcher_hand).upper() == str(batter_hand).upper():
        hnd = 0.975
    else:
        hnd = 1.025

    # Stat differentials (clamped -5..+5)
    d_hit = clamp(batter_con - pitcher_mov, -5, 5)
    d_pow = clamp(batter_pow - pitcher_vel, -5, 5)
    d_spd = clamp(batter_spd - pitcher_awr, -5, 5)
    d_eye = clamp(batter_eye - pitcher_cmd, -5, 5)

    def w_std(key, diff):
        return max(1, math.floor(_obr_lookup(key, diff) * hnd))

    w_hr = w_std("HR", d_pow)
    w_3b = w_std("3B", d_spd)
    w_2b = w_std("2B", d_spd)
    w_if1b = 0 if hit_and_run else w_std("IF1B", d_spd)
    w_bb = w_std("BB", d_eye)
    w_k = w_std("K", d_hit)

    # 1B: Hit-base * handedness + SPD/AWR modifier + pitcher-AWR modifier + constant - HR - 3B - 2B [+ HnR bonus]
    hit_base = math.floor(_obr_lookup("Hit", d_hit) * hnd)
    hnr_bonus = 20 if hit_and_run else 0
    w_1b = max(1,
        hit_base
        + _1B_HIT_NEG[d_hit]
        + _1B_SPD_AWR[d_spd]
        + _1B_PITCH_AWR.get(clamp(pitcher_awr - 3, -3, 3), 0)
        + 5
        - w_hr - w_3b - w_2b
        + hnr_bonus
    )

    # FO and PO: rate-based, each using the POW vs VEL differential
    fo_rate = _obr_lookup("FO_HND", d_pow) / 500
    po_rate = _obr_lookup("PO_HND", d_pow) / 500
    after_hits = 500 - (w_hr + w_3b + w_2b + w_1b + w_if1b + w_bb)
    after_bb = 500 - w_bb
    w_fo = max(1, math.floor(after_hits * fo_rate * (1 - po_rate)))
    w_po = max(1, math.floor(after_bb * fo_rate * po_rate))

    # LO: 4-wide slot inside GO, only with runners on and fewer than 2 outs
    w_lo = 4 if (runners_on and outs < 2) else 0

    # GO: remainder of 501 total (0-500 inclusive)
    w_go = 500 - (w_hr + w_3b + w_2b + w_1b + w_if1b + w_bb + w_fo + w_po + w_k + w_lo) + 1

    if bunt:
        b1bwh = 9
        total_hit = w_hr + w_3b + w_2b + w_1b + w_if1b
        base_hit = total_hit - 1
        spd_mov = batter_spd - pitcher_mov
        b1b = max(1, round((1 + spd_mov * 0.04) * base_hit) - b1bwh)
        b_bb = w_bb
        b_k = w_k
        b_go = max(1, 500 - (b1bwh + b1b + b_bb + b_k) + 1)
        rows: list[tuple[str, int]] = [
            ("B1BWH", b1bwh), ("B1B", b1b), ("BB", b_bb), ("K", b_k), ("BFC", b_go),
        ]
    else:
        rows = [
            ("HR", w_hr), ("3B", w_3b), ("2B", w_2b), ("1B", w_1b),
        ]
        if not hit_and_run:
            rows.append(("IF1B", w_if1b))
        rows += [("BB", w_bb), ("FO", w_fo), ("PO", w_po)]
        if w_lo > 0:
            rows.append(("LO", w_lo))
        rows += [("GO", w_go), ("K", w_k)]

    result = []
    pos = 0
    for name, width in rows:
        if width <= 0:
            continue
        result.append({"result": name, "range": width, "low": pos, "high": pos + width - 1})
        pos += width
    return result


def range_bar_chart(ranges: list[dict], title: str = "") -> go.Figure:
    """Horizontal stacked bar showing each result's share of the 0-500 number line."""
    fig = go.Figure()
    for r in ranges:
        color = RESULT_COLORS.get(r["result"], "#888")
        fig.add_trace(go.Bar(
            x=[r["range"]],
            y=[""],
            orientation="h",
            marker_color=color,
            name=r["result"],
            text=r["result"] if r["range"] > 12 else "",
            textposition="inside",
            insidetextanchor="middle",
            hovertemplate=(
                f"<b>{r['result']}</b><br>"
                f"Range: {r['range']}<br>"
                f"{r['low']} – {r['high']}<extra></extra>"
            ),
            width=0.5,
        ))
    fig.update_layout(
        barmode="stack",
        title=dict(text=title, font=dict(size=13)) if title else None,
        height=110,
        margin=dict(l=5, r=5, t=30 if title else 5, b=5),
        showlegend=False,
        xaxis=dict(
            range=[0, 501],
            tickvals=[0, 100, 200, 300, 400, 500],
            tickfont=dict(size=10),
            title=dict(text="0 → 500", font=dict(size=10)),
        ),
        yaxis=dict(visible=False),
        dragmode=False,
        modebar_remove=["zoom2d","pan2d","select2d","lasso2d","zoomIn2d",
                        "zoomOut2d","autoScale2d","resetScale2d","toImage"],
    )
    return fig


_DIFF_HM_BINS   = [-1, 25, 50, 100, 150, 200, 300, 501]
_DIFF_HM_LABELS = ["0–25", "26–50", "51–100", "101–150", "151–200", "201–300", "301–500"]

_DELTA_HM_BINS   = list(range(0, 501, 50))          # [0, 50, 100, …, 500]
_DELTA_HM_LABELS = [f"{i}–{i + 50}" for i in range(0, 500, 50)]  # 10 bins


def diff_vs_next_pitch_delta_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch |Δ| vs Prior Diff",
    value_col: str = "pitch",
) -> go.Figure:
    """Heatmap: unsigned next-value delta vs previous play's diff.

    X = prior diff bin; Y = abs circular delta to next value (0 at bottom, 500 at top).
    Only consecutive values from the same player within the same game are counted.
    """
    df_sw = df[df[value_col].notna() & df["diff"].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_sw = df_sw.sort_values(["game_id", group_col, "id"])
    df_sw["_next_val"] = df_sw.groupby(["game_id", group_col])[value_col].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_val"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_next_delta"] = df_sw.apply(
        lambda r: circular_diff(int(r[value_col]), int(r["_next_val"])), axis=1
    )
    df_sw["_diff_cat"] = pd.cut(
        df_sw["diff"].astype(int),
        bins=_DIFF_HM_BINS, labels=_DIFF_HM_LABELS, right=True, include_lowest=True,
    )
    df_sw["_delta_cat"] = pd.cut(
        df_sw["_next_delta"],
        bins=_DELTA_HM_BINS, labels=_DELTA_HM_LABELS, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_delta_cat"], df_sw["_diff_cat"]).reindex(
        index=_DELTA_HM_LABELS, columns=_DIFF_HM_LABELS, fill_value=0
    )
    # Normalize each column to 0–100 % so colour reflects within-column distribution.
    col_totals = ct.sum(axis=0).replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw  = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(_DIFF_HM_LABELS))]
        for i in range(len(_DELTA_HM_LABELS))
    ]
    # Flat raw counts for hover (customdata)
    customdata = z_raw

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=_DIFF_HM_LABELS,
        y=_DELTA_HM_LABELS,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior diff: %{x}<br>Next pitch |Δ|: %{y}<br>%{z:.1f}% of column (%{customdata} pitches)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior diff (abs)", side="bottom"),
        yaxis=dict(title="Next pitch |Δ|", autorange=True),
        height=max(360, len(_DELTA_HM_LABELS) * 40 + 110),
        margin=dict(l=80, r=10, t=50, b=70),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig


def next_delta_vs_prior_delta_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch Δ vs Prior Pitch Δ",
    value_col: str = "pitch",
) -> go.Figure:
    """Heatmap: next delta vs prior delta for consecutive plays.

    Shows how pitcher/batter adjusts their next movement based on their previous movement.
    X = prior pitch/swing delta bin; Y = next pitch/swing delta bin.
    """
    delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"

    df_sw = df[df[value_col].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    df_sw = df_sw.sort_values(["game_id", group_col, "id"])

    # Always recalculate deltas fresh to ensure proper grouping for filtered data
    df_sw[delta_col] = df_sw.groupby(["game_id", group_col], group_keys=False)[value_col].apply(_circ_delta_group)

    df_sw = df_sw[df_sw[delta_col].notna()].copy()
    if len(df_sw) < 2:
        return go.Figure()

    df_sw["_next_delta"] = df_sw.groupby(["game_id", group_col])[delta_col].shift(-1)
    df_sw = df_sw.dropna(subset=["_next_delta"])
    if df_sw.empty:
        return go.Figure()

    df_sw["_prior_delta_cat"] = pd.cut(
        df_sw[delta_col].abs().astype(int),
        bins=_DELTA_HM_BINS, labels=_DELTA_HM_LABELS, right=True, include_lowest=True,
    )
    df_sw["_next_delta_cat"] = pd.cut(
        df_sw["_next_delta"].abs().astype(int),
        bins=_DELTA_HM_BINS, labels=_DELTA_HM_LABELS, right=True, include_lowest=True,
    )

    ct = pd.crosstab(df_sw["_next_delta_cat"], df_sw["_prior_delta_cat"]).reindex(
        index=_DELTA_HM_LABELS, columns=_DELTA_HM_LABELS, fill_value=0
    )
    col_totals = ct.sum(axis=0).replace(0, 1)
    ct_norm = ct.div(col_totals, axis=1) * 100
    z_norm = ct_norm.values.tolist()
    z_raw = ct.values.tolist()
    text = [
        [f"{ct_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(len(_DELTA_HM_LABELS))]
        for i in range(len(_DELTA_HM_LABELS))
    ]
    customdata = z_raw

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=_DELTA_HM_LABELS,
        y=_DELTA_HM_LABELS,
        text=text,
        texttemplate="%{text}",
        customdata=customdata,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="Prior |Δ|: %{x}<br>Next |Δ|: %{y}<br>%{z:.1f}% of column (%{customdata} instances)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        xaxis=dict(title="Prior |Δ|"),
        yaxis=dict(title="Next |Δ|"),
        height=max(360, len(_DELTA_HM_LABELS) * 40 + 110),
        margin=dict(l=80, r=10, t=50, b=70),
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

    df = df[df[value_col].notna()].sort_values(["game_id", "id"]).copy()
    df["_next"] = df.groupby(group_cols)[value_col].shift(-1)
    df = df.dropna(subset=["_next"])

    df["_curr_b"] = ((df[value_col].astype(int) - 1) // bucket_size).clip(0, n_buckets - 1)
    df["_next_b"] = ((df["_next"].astype(int) - 1) // bucket_size).clip(0, n_buckets - 1)

    matrix = (
        pd.crosstab(df["_curr_b"], df["_next_b"])
        .reindex(index=range(n_buckets), columns=range(n_buckets), fill_value=0)
    )

    row_totals = matrix.sum(axis=1).replace(0, 1)
    matrix_norm = matrix.div(row_totals, axis=0) * 100
    z_norm = matrix_norm.values.tolist()
    z_raw  = matrix.values.tolist()
    text = [
        [f"{matrix_norm.iloc[i, j]:.0f}%" if z_raw[i][j] > 0 else ""
         for j in range(n_buckets)]
        for i in range(n_buckets)
    ]

    fig = go.Figure(go.Heatmap(
        z=z_norm,
        x=labels,
        y=labels,
        text=text,
        texttemplate="%{text}",
        customdata=z_raw,
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False,
        xgap=2,
        ygap=2,
        hovertemplate="From %{y} → %{x}<br>%{z:.1f}% of row (%{customdata} pitches)<extra></extra>",
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
    """Read the 'Plays (Raw)' tab and return a list of play dicts."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Plays (Raw)')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    plays = []
    for _, row in df.iterrows():
        play_num = _safe_int(row.get("Play"))
        game_code = _str(row.get("Game"))
        if not play_num or not game_code:
            continue

        inning_raw = _str(row.get("Inning")) or "T1"
        inning_num, half = parse_inning(inning_raw)

        play_type = _str(row.get("PlayType"))
        result = _str(row.get("Result"))
        pitch = _safe_int(row.get("Pitch"))
        swing = _safe_int(row.get("Swing"))

        is_steal = (play_type or "").lower() == "steal"
        if not result:
            continue
        if not is_steal and (pitch is None or swing is None):
            continue

        # OBC from runner fields ("-" means empty base)
        on_first  = _str(row.get("OnFirst"))  or "-"
        on_second = _str(row.get("OnSecond")) or "-"
        on_third  = _str(row.get("OnThird"))  or "-"
        brc = (
            (1 if on_first  != "-" else 0)
            | (2 if on_second != "-" else 0)
            | (4 if on_third  != "-" else 0)
        )
        obc = BRC_TO_OBC.get(brc, "Empty")

        plays.append({
            "game_code":  game_code,
            "play_num":   play_num,
            "timestamp":  _str(row.get("Timestamp")),
            "umpire":     _str(row.get("Umpire")),
            "away":       _str(row.get("Away")),
            "home":       _str(row.get("Home")),
            "inning_raw": inning_raw,
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "play_type":  play_type,
            "result":     result,
            "play_code":  _str(row.get("Playcode")),
            "pitcher_id": _safe_int(row.get("Pitcher")),
            "catcher_id": _safe_int(row.get("Catcher")),
            "pos":        _str(row.get("Pos")),
            "batter_id":  _safe_int(row.get("Batter")),
            "on_first":   on_first,
            "on_second":  on_second,
            "on_third":   on_third,
            "scored2":    _str(row.get("scored2")),
            "scored3":    _str(row.get("scored3")),
            "scored4":    _str(row.get("scored4")),
            "er1":        _str(row.get("er1")),
            "er2":        _str(row.get("er2")),
            "er3":        _str(row.get("er3")),
            "er4":        _str(row.get("er4")),
            "pitch":      pitch,
            "swing":      swing,
            "throw_num":  _safe_int(row.get("Throw")),
            "runner_id":  _safe_int(row.get("Runner")),
            "steal_num":  _safe_int(row.get("Steal")),
            # app-computed (half, obc here; rest filled in _sync_plays)
            "inning": inning_num,
            "half":   half,
            "obc":    obc,
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


def read_mln_teams_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Teams' tab from an MLN archive sheet."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    teams = []
    for _, row in df.iterrows():
        s_team = _str(row.get("S_Team"))
        if not s_team:
            continue
        teams.append({
            "league":       "MLN",
            "s_team":       s_team,
            "abbrev":       _str(row.get("Abv")),
            "season":       _safe_int(row.get("Season")),
            "sub_league":   _str(row.get("League")),
            "division":     _str(row.get("Division")),
            "team_id":      _str(row.get("Team ID")),
            "location":     _str(row.get("Location")),
            "team_name":    _str(row.get("Team Name")),
            "full_team":    _str(row.get("Full Team")),
            "primary_hex":  _str(row.get("Primary Hex")),
            "wins":         _safe_int(row.get("W")),
            "losses":       _safe_int(row.get("L")),
            "runs_scored":  _safe_int(row.get("RS")),
            "runs_allowed": _safe_int(row.get("RA")),
        })
    return teams


def read_mln_players_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Rosters' tab from an MLN archive sheet."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Rosters')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    players = []
    for _, row in df.iterrows():
        s_id = _str(row.get("S_ID"))
        name = _str(row.get("Full Player Name"))
        if not s_id or not name:
            continue
        players.append({
            "league":           "MLN",
            "s_id":             s_id,
            "season":           _safe_int(row.get("Season")),
            "name":             name,
            "first_name":       _str(row.get("First Name")),
            "last_name":        _str(row.get("Last Name")),
            "suffix":           _str(row.get("Suffix")),
            "discord_id":       _str(row.get("Discord ID*")),
            "discord_nickname": _str(row.get("Discord Nickname*")),
            "team":             _str(row.get("Team")),
            "gm":               _parse_bool(row.get("GM")),
            "status":           _str(row.get("Status*")),
            "session_added":    _str(row.get("Session*")),
            "primary_pos":      _str(row.get("Primary")),
            "secondary_pos":    _str(row.get("Secondary")),
            "hand":             _str(row.get("HAND")),
            "con":              _safe_int(row.get("CON")),
            "eye":              _safe_int(row.get("EYE")),
            "pwr":              _safe_int(row.get("PWR")),
            "spd":              _safe_int(row.get("SPD")),
            "mov":              _safe_int(row.get("MOV")),
            "cmd":              _safe_int(row.get("CMD")),
            "vel":              _safe_int(row.get("VEL")),
            "awr":              _safe_int(row.get("AWR")),
            "rookie":           _parse_bool(row.get("Rookie?")),
        })
    return players


def read_mln_team_abbrev_lookup(sheet_id: str) -> dict[str, str]:
    """Return {abbrev: full_team} for resolving team names in MLN Games/Plays."""
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Teams')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return {
        _str(row.get("Abv")): _str(row.get("Full Team"))
        for _, row in df.iterrows()
        if _str(row.get("Abv")) and _str(row.get("Full Team"))
    }


def read_mln_games_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Games' tab from an MLN archive sheet.

    Away/Home columns contain team abbreviations; caller resolves them to full names
    via get_mln_teams_for_lookup() before upserting.
    """
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Games')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    games = []
    for _, row in df.iterrows():
        game_num = _safe_int(row.get("Game#"))
        if not game_num:
            continue
        game_code = str(game_num).zfill(6)
        try:
            season = int(game_code[:2])
            session_num = int(game_code[2:4])
        except ValueError:
            continue
        games.append({
            "league":              "MLN",
            "game_code":           game_code,
            "game_id_short":       _str(row.get("GameID")),
            "season":              season,
            "session_number":      session_num,
            "away_team":           _str(row.get("Away")),   # raw abbrev; caller resolves
            "home_team":           _str(row.get("Home")),   # raw abbrev; caller resolves
            "away_score":          _safe_int(row.get("a_scr")),
            "home_score":          _safe_int(row.get("h_scr")),
            "winning_pitcher":     _str(row.get("WP")),
            "losing_pitcher":      _str(row.get("LP")),
            "save_pitcher":        _str(row.get("SV")),
            "player_of_game":      _str(row.get("PotG")),
            "honorable_mention_1": _str(row.get("HM1")),
            "honorable_mention_2": _str(row.get("HM2")),
            "honorable_mention_3": _str(row.get("HM3")),
            "link":                _str(row.get("Link")),
        })
    return games


def read_mln_plays_from_sheet(sheet_id: str) -> list[dict]:
    """Read the 'Plays' tab from an MLN archive sheet.

    Away/Home contain Team IDs (e.g. T1009); Pitcher/Batter/Catcher/Runner contain
    MLN player IDs. Caller resolves these to names via get_mln_teams/players_for_lookup().
    """
    import urllib.parse
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote('Plays')}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    plays = []
    for _, row in df.iterrows():
        play_num = _safe_int(row.get("Play"))
        game_raw = _safe_int(row.get("Game"))
        result   = _str(row.get("Result"))
        if not play_num or not game_raw or not result:
            continue
        game_code = str(game_raw).zfill(6)

        inning_raw = _str(row.get("Inning")) or "T1"
        inning_num, half = parse_inning(inning_raw)

        play_type = _str(row.get("PlayType"))
        pitch = _safe_int(row.get("Pitch"))
        swing = _safe_int(row.get("Swing"))

        # OBC: "0" means empty base in MLN
        on_first  = _str(row.get("OnFirst"))  or "0"
        on_second = _str(row.get("OnSecond")) or "0"
        on_third  = _str(row.get("OnThird"))  or "0"
        brc = (
            (1 if on_first  not in ("0", "-") else 0)
            | (2 if on_second not in ("0", "-") else 0)
            | (4 if on_third  not in ("0", "-") else 0)
        )
        obc = BRC_TO_OBC.get(brc, "Empty")

        plays.append({
            "league":     "MLN",
            "season":     _safe_int(row.get("Season")),
            "game_code":  game_code,
            "play_num":   play_num,
            "away":       _str(row.get("Away")),    # Team ID e.g. T1009
            "home":       _str(row.get("Home")),    # Team ID e.g. T1003
            "inning":     inning_num,
            "half":       half,
            "away_score": _safe_int(row.get("a_Scr")),
            "home_score": _safe_int(row.get("h_Scr")),
            "play_type":  play_type,
            "result":     result,
            "play_code":  _str(row.get("Playcode")),
            "pitcher_id": _safe_int(row.get("Pitcher")),
            "catcher_id": _safe_int(row.get("Catcher")),
            "batter_id":  _safe_int(row.get("Batter")),
            "on_first":   on_first,
            "on_second":  on_second,
            "on_third":   on_third,
            "scored2":    _str(row.get("scored2")),
            "scored3":    _str(row.get("scored3")),
            "scored4":    _str(row.get("scored4")),
            "pitch":      pitch,
            "swing":      swing,
            "throw_num":  _safe_int(row.get("Throw")),
            "runner_id":  _safe_int(row.get("Runner")),
            "steal_num":  _safe_int(row.get("Steal")),
            "obc":        obc,
        })
    return plays


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


# ── pitcher stats ─────────────────────────────────────────────────────────────

def compute_pitcher_stats(df: pd.DataFrame) -> list[dict]:
    """Compute per-pitcher behavioral stats from an enriched plays DataFrame."""
    import datetime
    sw = df[df["swing"].notna()]
    rows = []
    for pitcher, grp in sw.groupby("pitcher_name"):
        deltas   = grp["pitch_circ_delta"].dropna()
        delta2s  = grp["pitch_circ_delta2"].dropna()
        approach = grp["pitch_approach"].dropna()
        # Wraparound %: of pitches where the previous pitch was in the boundary zone
        # (>=850 or <=150), how often did they actually cross to the other side?
        _wrap_eligible = 0
        _wrap_crossed  = 0
        for _, game_grp in grp.groupby("game_id"):
            pitches = game_grp.sort_values("id")["pitch"].astype(int).tolist()
            for i in range(1, len(pitches)):
                prev, curr = pitches[i - 1], pitches[i]
                if prev >= 850 or prev <= 150:
                    _wrap_eligible += 1
                    if (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850):
                        _wrap_crossed += 1
        wraparound_pct = round(_wrap_crossed / _wrap_eligible * 100, 2) if _wrap_eligible else None
        rows.append({
            "pitcher_name":   pitcher,
            "ab_count":       len(grp),
            "avg_abs_delta":  round(float(deltas.abs().mean()), 3) if not deltas.empty else None,
            "avg_delta2":     round(float(delta2s.mean()), 3)      if not delta2s.empty else None,
            "shadow_pct":     round(float(approach.mean() * 100), 2) if not approach.empty else None,
            "meme_rate":      round(float(grp["is_meme_pitch"].mean() * 100), 2),
            "wraparound_pct": wraparound_pct,
            "updated_at":     datetime.datetime.utcnow().isoformat(),
        })
    return rows


def compute_recent_pitcher_stats(df: pd.DataFrame) -> dict:
    """Compute behavioral stats for a single pitcher from a pre-filtered DataFrame."""
    sw = df[df["swing"].notna()]
    if sw.empty:
        return {}
    deltas   = sw["pitch_circ_delta"].dropna()
    delta2s  = sw["pitch_circ_delta2"].dropna()
    approach = sw["pitch_approach"].dropna()
    _we, _wc = 0, 0
    for _, g in sw.groupby("game_id"):
        pitches = g.sort_values("id")["pitch"].astype(int).tolist()
        for i in range(1, len(pitches)):
            prev, curr = pitches[i - 1], pitches[i]
            if prev >= 850 or prev <= 150:
                _we += 1
                if (prev >= 850 and curr <= 150) or (prev <= 150 and curr >= 850):
                    _wc += 1
    return {
        "avg_abs_delta":  float(deltas.abs().mean())   if not deltas.empty  else None,
        "avg_delta2":     float(delta2s.mean())        if not delta2s.empty else None,
        "shadow_pct":     float(approach.mean() * 100) if not approach.empty else None,
        "meme_rate":      float(sw["is_meme_pitch"].mean() * 100),
        "wraparound_pct": (_wc / _we * 100)            if _we else None,
    }


_PERCENTILE_STATS = [
    ("Avg |Δ|",      "avg_abs_delta",  lambda v: f"{v:.1f}"),
    ("Avg |Δ²|",     "avg_delta2",     lambda v: f"{v:.1f}"),
    ("Shadow %",     "shadow_pct",     lambda v: f"{v:.1f}%"),
    ("Wraparound %", "wraparound_pct", lambda v: f"{v:.1f}%"),
    ("Meme Rate",    "meme_rate",      lambda v: f"{v:.1f}%"),
]


def pitcher_percentile_card(
    pitcher_name: str,
    stats_df: pd.DataFrame,
    recent_vals: dict | None = None,
    recent_n: int | None = None,
) -> go.Figure | None:
    """
    Compact pill-bar percentile chart.
    Bar = career percentile in the qualified pool (≥100 AB).
    Gold needle = where recent stats (recent_vals) fall in that same pool.
    """
    import math

    if stats_df.empty or pitcher_name not in stats_df["pitcher_name"].values:
        return None

    row = stats_df[stats_df["pitcher_name"] == pitcher_name].iloc[0]

    # Only qualified pitchers form the reference pool for percentile ranks
    _MIN_AB = 100
    qual = stats_df[stats_df["ab_count"] >= _MIN_AB] if "ab_count" in stats_df.columns else stats_df

    def _percentile(val, qual_vals):
        """Return (pct, label) for val within qual_vals."""
        if len(qual_vals) < 2:
            return 50.0, "50"
        if val < qual_vals.min():
            return 0.0, "0-"
        if val > qual_vals.max():
            return 100.0, "100+"
        rank = float((qual_vals < val).sum()) + float((qual_vals == val).sum()) * 0.5
        p = rank / len(qual_vals) * 100
        return p, f"{p:.0f}"

    stat_labels, pcts, raw_vals, bubble_labels = [], [], [], []
    recent_pcts, recent_raw_vals = [], []
    for label, col, fmt in _PERCENTILE_STATS:
        stat_labels.append(label)
        val = row.get(col) if col in stats_df.columns else None
        qual_vals = qual[col].dropna() if col in qual.columns else pd.Series(dtype=float)

        if val is None or (isinstance(val, float) and pd.isna(val)):
            pcts.append(None)
            raw_vals.append("-")
            bubble_labels.append(None)
        else:
            pct, blbl = _percentile(val, qual_vals)
            pcts.append(pct)
            raw_vals.append(fmt(val))
            bubble_labels.append(blbl)

        # Recent value for same stat
        rval = (recent_vals or {}).get(col)
        if rval is not None and not (isinstance(rval, float) and pd.isna(rval)):
            rpct, _ = _percentile(rval, qual_vals)
            recent_pcts.append(rpct)
            recent_raw_vals.append(fmt(rval))
        else:
            recent_pcts.append(None)
            recent_raw_vals.append(None)

    # Reverse: index 0 = bottom so first stat appears at the top
    stat_labels    = list(reversed(stat_labels))
    pcts           = list(reversed(pcts))
    raw_vals       = list(reversed(raw_vals))
    bubble_labels  = list(reversed(bubble_labels))
    recent_pcts    = list(reversed(recent_pcts))
    recent_raw_vals = list(reversed(recent_raw_vals))
    n              = len(stat_labels)

    def _color(p: float, alpha: float = 1.0) -> str:
        """0–50%: medium blue → light blue; 50–100%: light red → medium red."""
        t = max(0.0, min(1.0, p / 100))
        if t <= 0.5:
            t2 = t * 2
            r = int(30  + t2 * (187 - 30))
            g = int(136 + t2 * (222 - 136))
            b = int(229 + t2 * (251 - 229))
        else:
            t2 = (t - 0.5) * 2
            r = int(255 + t2 * (211 - 255))
            g = int(205 + t2 * (47  - 205))
            b = int(210 + t2 * (47  - 210))
        return f"rgba({r},{g},{b},{alpha})"

    _RX = 3.0  # pill corner radius in x data coords (shared by _pill and bubble placement)

    def _pill(x1: float, x2: float, yc: float, ry: float = 0.30, rx: float = _RX, pts: int = 20):
        """Closed polygon path for a pill (stadium) shape in data coordinates."""
        _rx = min(rx, max((x2 - x1) / 2, 0.01))
        right = [math.pi / 2 - k * math.pi / (pts - 1) for k in range(pts)]
        left  = [-math.pi / 2 - k * math.pi / (pts - 1) for k in range(pts)]
        xr = [(x2 - _rx) + _rx * math.cos(t) for t in right]
        yr = [yc + ry * math.sin(t) for t in right]
        xl = [(x1 + _rx) + _rx * math.cos(t) for t in left]
        yl = [yc + ry * math.sin(t) for t in left]
        return xr + xl + [xr[0]], yr + yl + [yr[0]]

    # Two-bar layout: career on top half, recent on bottom half of each row.
    # Stacking by y-offset eliminates all bubble collision regardless of percentile proximity.
    has_recent = any(p is not None for p in recent_pcts)
    _YO  = 0.28   # y offset from row centre for each bar's centre
    _RY  = 0.16   # half-height of each bar (two bars fit in one row with a gap)
    _BR  = 2.0    # bubble radius in x data coords
    _STAT_SPACING = 1.5  # vertical spacing between stat rows (increase to spread stats further apart)

    fig = go.Figure()

    for i in range(n):
        p,   rv,  lbl,  blbl  = pcts[i], raw_vals[i], stat_labels[i], bubble_labels[i]
        rpct, rrv              = recent_pcts[i], recent_raw_vals[i]

        yc = (i * _STAT_SPACING) + _YO  # career bar centre
        yrc = (i * _STAT_SPACING) - _YO  # recent bar centre

        # ── career row ────────────────────────────────────────────────────────
        # Background track
        xb, yb = _pill(0, 100, yc, ry=_RY)
        fig.add_trace(go.Scatter(x=xb, y=yb, fill="toself",
                                 fillcolor="rgba(128,128,128,0.18)",
                                 line=dict(width=0), mode="lines",
                                 showlegend=False, hoverinfo="skip"))
        if p is not None:
            c = _color(max(p, 1.0))
            bx = max(p - _BR, _BR)
            if p > 0.5:
                xf, yf = _pill(0, p, yc, ry=_RY)
                fig.add_trace(go.Scatter(x=xf, y=yf, fill="toself",
                                         fillcolor=c, line=dict(width=0), mode="lines",
                                         showlegend=False,
                                         hovertemplate=f"{lbl} Career: {rv}<br>Pct: {blbl}<extra></extra>"))
            fig.add_trace(go.Scatter(
                x=[bx], y=[yc], mode="markers+text",
                marker=dict(symbol="circle", size=20, color=c,
                            line=dict(width=1.5, color="rgba(255,255,255,0.8)")),
                text=[blbl], textposition="middle center",
                textfont=dict(color="white", size=8),
                cliponaxis=False, showlegend=False, hoverinfo="skip"))
        # Value annotation pinned to right margin via paper coords - no data range needed
        fig.add_annotation(xref="paper", x=1.02, yref="y", y=yc,
                           text=f"<b>{rv}</b>",
                           showarrow=False, xanchor="left", font=dict(size=14))

        # ── recent row ────────────────────────────────────────────────────────
        if has_recent:
            xb2, yb2 = _pill(0, 100, yrc, ry=_RY)
            fig.add_trace(go.Scatter(x=xb2, y=yb2, fill="toself",
                                     fillcolor="rgba(128,128,128,0.12)",
                                     line=dict(width=0), mode="lines",
                                     showlegend=False, hoverinfo="skip"))
            if rpct is not None:
                rc = _color(max(rpct, 1.0), alpha=1.0)
                rbx = max(rpct - _BR, _BR)
                if rpct > 0.5:
                    xrf, yrf = _pill(0, rpct, yrc, ry=_RY)
                    fig.add_trace(go.Scatter(x=xrf, y=yrf, fill="toself",
                                             fillcolor=rc, line=dict(width=0), mode="lines",
                                             showlegend=False,
                                             hovertemplate=f"{lbl} Recent: {rrv}<br>Pct: {rpct:.0f}<extra></extra>"))
                rblbl = "0-" if rpct == 0.0 else ("100+" if rpct == 100.0 else f"{rpct:.0f}")
                fig.add_trace(go.Scatter(
                    x=[rbx], y=[yrc], mode="markers+text",
                    marker=dict(symbol="circle", size=20, color=rc,
                                line=dict(width=1.5, color="rgba(255,255,255,0.7)")),
                    text=[rblbl], textposition="middle center",
                    textfont=dict(color="white", size=8),
                    cliponaxis=False, showlegend=False, hoverinfo="skip"))
                fig.add_annotation(xref="paper", x=1.02, yref="y", y=yrc,
                                   text=f"<i>{rrv}</i>",
                                   showarrow=False, xanchor="left", font=dict(size=14))

    career_ab = row.get("ab_count") if "ab_count" in row.index else None
    career_ab_str = f" ({int(career_ab)} PA)" if career_ab and not pd.isna(career_ab) else ""
    subtitle = (
        f"<br><sup>Top = Career{career_ab_str}  |  Bottom = Recent ({recent_n} PA)</sup>"
        if has_recent else ""
    )
    fig.update_layout(
        title=dict(
            text=f"<b>{pitcher_name}</b> - Behavioral Tendencies{subtitle}",
            x=0.5, xanchor="center", font=dict(size=13),
        ),
        yaxis=dict(
            tickvals=[i * _STAT_SPACING for i in range(n)], ticktext=stat_labels,
            showgrid=False, zeroline=False, showline=False,
            tickfont=dict(size=14), range=[-0.6, n * _STAT_SPACING - 0.4],
        ),
        xaxis=dict(
            range=[0, 107], showgrid=False, showticklabels=False,
            showline=False, zeroline=False,
        ),
        height=int((44 if has_recent else 30) * n * _STAT_SPACING + 58),
        margin=dict(l=85, r=65, t=44, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig

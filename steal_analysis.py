"""MLN S12 Steal Analysis
Produces steal_analysis.html with two sections:
  1. Smart steal opportunities by offensive team (all play types, positive-EV)
  2. Actual steal performance by offensive team (play_type=Steal only)

Exclusions:
  - Rows where catcher_eye is missing (counted and reported)
  - Home steal rows (stealing_base=4) excluded from analysis
  - Demetrios Ooga row (play 121415055) flagged as misassigned runner
"""
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

OUTPUT_HTML = "steal_analysis.html"

# ── Load data ─────────────────────────────────────────────────────────────────

df = pd.read_excel("MLN Plays Analysis.xlsx", sheet_name="plays_rows")
mln_raw = df[(df["league"] == "MLN") & (df["season"].isna())].copy()
print(f"MLN S12 total rows: {len(mln_raw)}")

# ── Exclusion: missing catcher_eye ────────────────────────────────────────────

missing_eye = mln_raw[mln_raw["catcher_eye"].isna()]
n_missing_eye = len(missing_eye)
missing_eye_teams = missing_eye["def_team"].value_counts().to_dict()
print(f"Excluded (missing catcher_eye): {n_missing_eye} rows")

mln = mln_raw[mln_raw["catcher_eye"].notna()].copy()
print(f"Working dataset: {len(mln)} rows")

# ── Flag: misassigned runner ──────────────────────────────────────────────────
# Demetrios Ooga (play 121415055): obc=101 (1B+3B), result=CS4
# Logic assigned stealing_runner=1 (1B->2B) but result says runner on 3rd stole home
mismatch_play = 121415055
mismatch_row = mln[mln["play_num"] == mismatch_play]

# ── Flag: safe_range table issue for non-2nd steals ──────────────────────────
# CS3 and home steal rows use 2nd base table instead of 3rd/home table
steal_all = mln[mln["play_type"] == "Steal"].copy()
wrong_table = steal_all[steal_all["result"].str.startswith("CS3", na=False) |
                         steal_all["result"].str.startswith("SB3", na=False)]
home_steals = steal_all[steal_all["stealing_base"] == 4]
print(f"\nRows where safe_range uses wrong table (non-2nd steals): "
      f"{len(wrong_table)} CS3/SB3 rows + {len(home_steals)} home steal rows")

# ── Exclude home steals from analysis ────────────────────────────────────────
mln_no_home = mln[~(mln["play_type"] == "Steal") | (mln["stealing_base"] != 4)].copy()
steal = mln_no_home[mln_no_home["play_type"] == "Steal"].copy()
print(f"Steal rows (excl. home steals): {len(steal)}")

# ── Section 1: Smart steal opportunities (all play types, wp_pos=1) ───────────
# wp_pos=1 means wp_added_steal > 0 (positive EV steal opportunity exists)
# Excludes home steal situations inherently since wp_pos isn't set for those

opp = mln_no_home[mln_no_home["wp_pos"] == 1.0].copy()

smart = (
    opp.groupby("off_team")
    .agg(
        opportunities=("wp_added_steal", "count"),
        sum_wp_gain=("wp_added_steal", "sum"),
        avg_wp_gain=("wp_added_steal", "mean"),
        max_wp_gain=("wp_added_steal", "max"),
    )
    .reset_index()
    .sort_values("sum_wp_gain", ascending=False)
    .reset_index(drop=True)
)
smart["rank"] = smart.index + 1
smart["sum_wp_gain"] = smart["sum_wp_gain"].round(4)
smart["avg_wp_gain"] = smart["avg_wp_gain"].round(4)
smart["max_wp_gain"] = smart["max_wp_gain"].round(4)

print(f"\nSmart steal opportunities (positive EV, all play types):")
print(smart[["off_team","opportunities","sum_wp_gain","avg_wp_gain"]].to_string(index=False))

# How many of those positive-EV opportunities did each team actually attempt?
steal_attempts = (
    steal.groupby("off_team")
    .size()
    .reset_index(name="steal_attempts")
)
smart = smart.merge(steal_attempts, on="off_team", how="left")
smart["steal_attempts"] = smart["steal_attempts"].fillna(0).astype(int)
smart["attempt_rate"] = (smart["steal_attempts"] / smart["opportunities"]).round(3)

# ── Section 2: Actual steal performance ──────────────────────────────────────

steal_perf = (
    steal[steal["steal_result"].notna()]
    .groupby("off_team")
    .agg(
        attempts=("steal_result", "count"),
        sb=("steal_result", lambda x: (x == "SB").sum()),
        cs=("steal_result", lambda x: (x == "CS").sum()),
        sum_wp=("wp_result", "sum"),
        avg_wp=("wp_result", "mean"),
    )
    .reset_index()
)
steal_perf["success_pct"] = (steal_perf["sb"] / steal_perf["attempts"] * 100).round(1)
steal_perf["sum_wp"] = steal_perf["sum_wp"].round(4)
steal_perf["avg_wp"] = steal_perf["avg_wp"].round(4)
steal_perf = steal_perf.sort_values("sum_wp", ascending=False).reset_index(drop=True)

print(f"\nActual steal performance:")
print(steal_perf.to_string(index=False))

# ── Build charts ──────────────────────────────────────────────────────────────

COLORS = {
    "green_dark": "#27ae60", "green_light": "#82e0aa",
    "red_dark": "#c0392b",   "red_light": "#f1948a",
    "blue": "#2980b9", "purple": "#8e44ad",
    "bg": "#f8f9fa", "text": "#2c3e50",
}

# Color scale helper: green for positive, red for negative
def _bar_colors(vals, pos_color="#27ae60", neg_color="#c0392b"):
    return [pos_color if v >= 0 else neg_color for v in vals]

figs = []

# ── 1a. Smart steal opportunities: sum WP gain by team ───────────────────────
fig1a = go.Figure()
fig1a.add_trace(go.Bar(
    x=smart["off_team"],
    y=smart["sum_wp_gain"],
    marker_color=[COLORS["green_dark"]] * len(smart),
    customdata=smart[["opportunities", "steal_attempts", "attempt_rate"]].values,
    hovertemplate=(
        "<b>%{x}</b><br>"
        "Total WP gain if always stolen: <b>%{y:+.4f}</b><br>"
        "Positive-EV opportunities: %{customdata[0]}<br>"
        "Actual steal attempts: %{customdata[1]}<br>"
        "Attempt rate: %{customdata[2]:.1%}"
        "<extra></extra>"
    ),
    text=smart["sum_wp_gain"].apply(lambda v: f"{v:+.3f}"),
    textposition="outside",
    textfont=dict(size=9),
))
fig1a.update_layout(
    title=("Sum of WP Gain if Stolen on Every Positive-EV Opportunity<br>"
           "<sub>MLN S12 - all play types - excludes rows with missing catcher eye "
           "and home steal situations</sub>"),
    xaxis_title="Offensive Team",
    yaxis_title="Total WP Added (if always steals when +EV)",
    height=500,
    xaxis=dict(tickangle=35, tickfont=dict(size=10)),
    margin=dict(b=140, t=80, r=40),
    plot_bgcolor=COLORS["bg"],
)
figs.append(("smart", "Meat on the Bone: Total WP Left by Team", fig1a))

# ── 1b. Opportunities count vs attempt rate scatter ──────────────────────────
fig1b = go.Figure(go.Scatter(
    x=smart["opportunities"],
    y=smart["attempt_rate"],
    mode="markers+text",
    text=smart["off_team"],
    textposition="top center",
    textfont=dict(size=8),
    marker=dict(
        size=smart["sum_wp_gain"] * 800 + 8,
        color=smart["sum_wp_gain"],
        colorscale="RdYlGn",
        showscale=True,
        colorbar=dict(title="Sum WP Gain", thickness=12),
        line=dict(width=1, color="white"),
    ),
    hovertemplate=(
        "<b>%{text}</b><br>"
        "Opportunities: %{x}<br>"
        "Attempt rate: %{y:.1%}<br>"
        "<extra></extra>"
    ),
))
fig1b.add_hline(y=smart["attempt_rate"].mean(), line_dash="dash", line_color="gray",
                annotation_text=f"Avg attempt rate ({smart['attempt_rate'].mean():.1%})",
                annotation_position="right")
fig1b.update_layout(
    title=("Positive-EV Opportunities vs Steal Attempt Rate<br>"
           "<sub>Bubble size = total WP gain available. Teams top-right are aggressive AND have opportunity.</sub>"),
    xaxis_title="Number of Positive-EV Steal Situations",
    yaxis_title="Fraction Actually Attempted",
    height=520,
    margin=dict(t=80, r=120),
)
figs.append(("smart", "Opportunities vs Attempt Rate", fig1b))

# ── 1c. Smart steal summary table ────────────────────────────────────────────
tbl1 = smart[["rank","off_team","opportunities","steal_attempts","attempt_rate","sum_wp_gain","avg_wp_gain","max_wp_gain"]].copy()
row_bg = [["#f0f3f4" if i % 2 else "#ffffff" for i in range(len(tbl1))] for _ in range(len(tbl1.columns))]
fig1c = go.Figure(go.Table(
    header=dict(
        values=["#", "Team", "Opps", "Attempted", "Attempt%", "Sum WP Gain", "Avg WP Gain", "Max WP Gain"],
        fill_color=COLORS["text"],
        font=dict(color="white", size=11),
        align="center", height=30,
    ),
    cells=dict(
        values=[
            tbl1["rank"].tolist(),
            tbl1["off_team"].tolist(),
            tbl1["opportunities"].tolist(),
            tbl1["steal_attempts"].tolist(),
            tbl1["attempt_rate"].apply(lambda v: f"{v:.1%}").tolist(),
            tbl1["sum_wp_gain"].apply(lambda v: f"{v:+.4f}").tolist(),
            tbl1["avg_wp_gain"].apply(lambda v: f"{v:+.4f}").tolist(),
            tbl1["max_wp_gain"].apply(lambda v: f"{v:+.4f}").tolist(),
        ],
        fill_color=row_bg,
        align="center", font=dict(size=10), height=24,
    ),
))
fig1c.update_layout(title="Smart Steal Summary by Team", height=560, margin=dict(t=50))
figs.append(("smart", "Smart Steal Table", fig1c))

# ── 2a. Actual steal WP added by team ────────────────────────────────────────
sp_sorted = steal_perf.sort_values("sum_wp", ascending=False)
fig2a = go.Figure()
fig2a.add_trace(go.Bar(
    x=sp_sorted["off_team"],
    y=sp_sorted["sum_wp"],
    marker_color=_bar_colors(sp_sorted["sum_wp"]),
    customdata=sp_sorted[["attempts","sb","cs","success_pct","avg_wp"]].values,
    hovertemplate=(
        "<b>%{x}</b><br>"
        "Total WP added: <b>%{y:+.4f}</b><br>"
        "Attempts: %{customdata[0]}  (SB: %{customdata[1]}, CS: %{customdata[2]})<br>"
        "Success: %{customdata[3]:.0f}%  |  Avg WP/attempt: %{customdata[4]:+.4f}"
        "<extra></extra>"
    ),
    text=sp_sorted["sum_wp"].apply(lambda v: f"{v:+.3f}"),
    textposition="outside",
    textfont=dict(size=9),
))
fig2a.add_hline(y=0, line_color="black", line_width=1)
fig2a.update_layout(
    title=("Actual Steal WP Added by Team<br>"
           "<sub>MLN S12 - play_type=Steal only - home steals excluded</sub>"),
    xaxis_title="Offensive Team",
    yaxis_title="Total WP Added (actual results)",
    height=500,
    xaxis=dict(tickangle=35, tickfont=dict(size=10)),
    margin=dict(b=140, t=80, r=40),
    plot_bgcolor=COLORS["bg"],
)
figs.append(("actual", "Actual Steal WP Added", fig2a))

# ── 2b. Success % vs avg WP per attempt scatter ──────────────────────────────
fig2b = go.Figure(go.Scatter(
    x=steal_perf["success_pct"],
    y=steal_perf["avg_wp"],
    mode="markers+text",
    text=steal_perf["off_team"],
    textposition="top center",
    textfont=dict(size=8),
    marker=dict(
        size=steal_perf["attempts"] * 2.5 + 8,
        color=steal_perf["sum_wp"],
        colorscale="RdYlGn",
        showscale=True,
        colorbar=dict(title="Sum WP", thickness=12),
        line=dict(width=1, color="white"),
    ),
    hovertemplate=(
        "<b>%{text}</b><br>"
        "Success: %{x:.0f}%<br>"
        "Avg WP/attempt: %{y:+.4f}<br>"
        "<extra></extra>"
    ),
))
fig2b.add_hline(y=0, line_dash="dash", line_color="black",
                annotation_text="Break-even", annotation_position="right")
fig2b.add_vline(x=66.3, line_dash="dot", line_color="gray",
                annotation_text="~66% break-even", annotation_position="top")
fig2b.update_layout(
    title=("Success Rate vs Avg WP per Steal Attempt<br>"
           "<sub>Bubble size = number of attempts. ~66% success rate is break-even for stealing 2nd.</sub>"),
    xaxis_title="Stolen Base Success Rate (%)",
    yaxis_title="Avg WP Added per Attempt",
    height=520,
    margin=dict(t=80, r=120),
)
figs.append(("actual", "Success Rate vs WP Efficiency", fig2b))

# ── 2c. SB/CS breakdown stacked bar ──────────────────────────────────────────
sp2 = steal_perf.sort_values("attempts", ascending=False)
fig2c = go.Figure()
fig2c.add_trace(go.Bar(
    name="Stolen Base",
    x=sp2["off_team"], y=sp2["sb"],
    marker_color=COLORS["green_dark"],
    text=sp2["sb"], textposition="inside", textfont=dict(color="white", size=9),
))
fig2c.add_trace(go.Bar(
    name="Caught Stealing",
    x=sp2["off_team"], y=sp2["cs"],
    marker_color=COLORS["red_dark"],
    text=sp2["cs"], textposition="inside", textfont=dict(color="white", size=9),
))
fig2c.update_layout(
    barmode="stack",
    title="Steal Attempts: SB vs CS by Team",
    xaxis_title="Offensive Team",
    yaxis_title="Count",
    height=450,
    xaxis=dict(tickangle=35, tickfont=dict(size=10)),
    margin=dict(b=140, t=60),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
figs.append(("actual", "SB vs CS Breakdown", fig2c))

# ── 2d. Actual steal performance table ───────────────────────────────────────
sp_tbl = steal_perf[["off_team","attempts","sb","cs","success_pct","sum_wp","avg_wp"]].copy()
row_bg2 = [["#f0f3f4" if i % 2 else "#ffffff" for i in range(len(sp_tbl))] for _ in range(len(sp_tbl.columns))]
fig2d = go.Figure(go.Table(
    header=dict(
        values=["Team", "Attempts", "SB", "CS", "Success%", "Sum WP", "Avg WP/Attempt"],
        fill_color=COLORS["text"],
        font=dict(color="white", size=11),
        align="center", height=30,
    ),
    cells=dict(
        values=[
            sp_tbl["off_team"].tolist(),
            sp_tbl["attempts"].tolist(),
            sp_tbl["sb"].tolist(),
            sp_tbl["cs"].tolist(),
            sp_tbl["success_pct"].apply(lambda v: f"{v:.0f}%").tolist(),
            sp_tbl["sum_wp"].apply(lambda v: f"{v:+.4f}").tolist(),
            sp_tbl["avg_wp"].apply(lambda v: f"{v:+.4f}").tolist(),
        ],
        fill_color=row_bg2,
        align="center", font=dict(size=10), height=24,
    ),
))
fig2d.update_layout(title="Actual Steal Performance by Team", height=520, margin=dict(t=50))
figs.append(("actual", "Actual Performance Table", fig2d))

# ── Write HTML ────────────────────────────────────────────────────────────────

with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
    f.write(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>MLN S12 Steal Analysis</title>"
        "<script src='https://cdn.plot.ly/plotly-latest.min.js'></script>"
        "<style>"
        "body{font-family:sans-serif;margin:20px;background:#f8f9fa;color:#2c3e50}"
        "h1,h2{border-bottom:2px solid #2c3e50;padding-bottom:6px}"
        "h3{margin-top:28px;color:#34495e}"
        "p.meta{color:#7f8c8d;font-size:.9em;margin:4px 0}"
        "p.warn{background:#fef9e7;border-left:4px solid #f39c12;padding:8px 12px;font-size:.9em;margin:12px 0}"
        "p.err{background:#fdedec;border-left:4px solid #c0392b;padding:8px 12px;font-size:.9em;margin:12px 0}"
        "p.ok{background:#eafaf1;border-left:4px solid #27ae60;padding:8px 12px;font-size:.9em;margin:12px 0}"
        "</style></head><body>\n"
    )

    f.write("<h1>MLN S12 Steal Analysis</h1>\n")

    # Data quality notes
    f.write(
        f"<p class='meta'>MLN S12 rows: {len(mln_raw)} | "
        f"Excluded (missing catcher eye): <b>{n_missing_eye}</b> | "
        f"Working dataset: <b>{len(mln)}</b></p>\n"
    )
    f.write(
        f"<p class='warn'>Home steal rows excluded from all analysis: "
        f"<b>{len(home_steals)}</b> rows (stealing_base=4). "
        f"These use the 2nd-base steal table by default which overstates success probability.</p>\n"
    )
    f.write(
        f"<p class='err'><b>Runner mismatch detected - Demetrios Ooga (play 121415055):</b> "
        f"obc=101 (runners on 1st and 3rd), result=CS4 (caught stealing home). "
        f"Formula assigned stealing_runner=1 (1st base runner) but the runner on 3rd "
        f"was attempting to steal home. obc_out, obc_safe, and all WP states for this "
        f"row are computed for the wrong runner/base.</p>\n"
    )
    f.write(
        f"<p class='warn'><b>safe_range table issue:</b> {len(wrong_table)} CS3/SB3 rows "
        f"use the 2nd-base steal table instead of the 3rd-base row, overstating P(safe). "
        f"Affected rows: play nums "
        f"{', '.join(str(x) for x in wrong_table['play_num'].tolist())}.</p>\n"
    )
    f.write(
        "<p class='ok'><b>Logic confirmed correct:</b> def_avg = (pitcher_awr + catcher_eye) / 2. "
        "WP lookup via numeric obc format matches WP tab. "
        "wp_result = wp_safe - wp_before (SB) or wp_out - wp_before (CS). "
        "wp_added_steal = safe_prob x (wp_safe - wp_before) + (1-safe_prob) x (wp_out - wp_before).</p>\n"
    )

    # Section 1
    f.write("<h2>Section 1: Smart Steal Opportunities (Positive-EV, All Play Types)</h2>\n")
    f.write(
        "<p class='meta'>Positive-EV = wp_added_steal > 0. "
        "Sum WP Gain shows how much win probability a team could have added if they always stole "
        "in these situations. Attempt Rate shows how often they actually ran.</p>\n"
    )
    for section, title, fig in figs:
        if section == "smart":
            f.write(f"<h3>{title}</h3>\n")
            f.write(fig.to_html(full_html=False, include_plotlyjs=False))
            f.write("\n")

    # Section 2
    f.write("<h2>Section 2: Actual Steal Performance (play_type=Steal Only)</h2>\n")
    f.write(
        "<p class='meta'>Based on actual steal attempts and results. "
        "WP values are actual WP shifts: SB = wp_safe - wp_before, CS = wp_out - wp_before. "
        "Break-even success rate for stealing 2nd is ~66%.</p>\n"
    )
    for section, title, fig in figs:
        if section == "actual":
            f.write(f"<h3>{title}</h3>\n")
            f.write(fig.to_html(full_html=False, include_plotlyjs=False))
            f.write("\n")

    f.write("</body></html>\n")

print(f"\nWrote {OUTPUT_HTML}")

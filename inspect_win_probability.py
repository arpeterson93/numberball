"""
Visualize win_probability_table.csv for validation.

Run from the project root after compute_win_probability.py:
    python inspect_win_probability.py

Produces three figures saved to the project root:
  wp_inspect_heatmaps.png  - WP by (remaining x lead) for 4 base/out states
  wp_inspect_curves.png    - WP curves by remaining, for key states
  wp_inspect_coverage.png  - method tier coverage across states
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.lines import Line2D

# ── Load ──────────────────────────────────────────────────────────────────────

df = pd.read_csv("win_probability_table.csv", dtype={"obc": str})
df["outs"]         = df["outs"].astype(int)
df["remaining"]    = df["remaining"].astype(int)
df["batting_lead"] = df["batting_lead"].astype(int)
df["obc"]          = df["obc"].str.zfill(3)

print(f"Loaded {len(df):,} rows")
print(df["method"].value_counts().to_string())

ALL_LEADS      = list(range(-10, 11))
ALL_REMAININGS = list(range(12, 0, -1))   # 12 at top of chart, 1 at bottom

STATES = [
    (0, "000", "0 outs, empty"),
    (1, "000", "1 out,  empty"),
    (2, "000", "2 outs, empty"),
    (0, "111", "0 outs, loaded"),
]

WP_CMAP = plt.cm.RdYlBu_r
WP_NORM = mcolors.Normalize(vmin=0, vmax=1)


def get_pivot(outs, obc, value_col="win_prob"):
    sub = df[(df["outs"] == outs) & (df["obc"] == obc)]
    return (
        sub.pivot_table(index="remaining", columns="batting_lead",
                        values=value_col, aggfunc="first")
           .reindex(index=ALL_REMAININGS, columns=ALL_LEADS)
    )


# ── Figure 1: WP heatmaps ────────────────────────────────────────────────────

fig1, axes = plt.subplots(1, 4, figsize=(20, 7), sharey=True)
fig1.suptitle("Win Probability  |  Remaining Half-Innings vs Batting Lead\n(batting team perspective)", fontsize=13)

for ax, (outs, obc, label) in zip(axes, STATES):
    pivot = get_pivot(outs, obc)
    im = ax.imshow(pivot.values, aspect="auto", cmap=WP_CMAP, norm=WP_NORM)

    ax.set_xticks(range(len(ALL_LEADS)))
    ax.set_xticklabels([str(l) if l % 2 == 0 else "" for l in ALL_LEADS], fontsize=7)
    ax.set_yticks(range(len(ALL_REMAININGS)))
    ax.set_yticklabels([str(r) for r in ALL_REMAININGS], fontsize=7)

    for ri, rem in enumerate(ALL_REMAININGS):
        for ci, lead in enumerate(ALL_LEADS):
            val = pivot.loc[rem, lead] if (rem in pivot.index and lead in pivot.columns) else np.nan
            if not np.isnan(val):
                txt_color = "white" if abs(val - 0.5) > 0.28 else "black"
                ax.text(ci, ri, f"{val:.0%}", ha="center", va="center",
                        fontsize=5, color=txt_color)

    ax.set_title(label, fontsize=10)
    ax.set_xlabel("Batting lead (runs)", fontsize=9)
    # vertical line at lead=0
    ax.axvline(x=ALL_LEADS.index(0), color="white", linewidth=1.2, linestyle="--", alpha=0.7)

axes[0].set_ylabel("Remaining half-innings", fontsize=9)
fig1.colorbar(im, ax=axes[-1], label="Win probability", shrink=0.85, format="{x:.0%}")
fig1.tight_layout()
fig1.savefig("wp_inspect_heatmaps.png", dpi=140, bbox_inches="tight")
print("Saved: wp_inspect_heatmaps.png")

# ── Figure 2: WP curve families ──────────────────────────────────────────────

CURVE_STATES = [
    (0, "000", "0 outs, empty bases"),
    (0, "111", "0 outs, bases loaded"),
    (2, "000", "2 outs, empty bases"),
    (2, "001", "2 outs, runner on 1st"),
]
SHOW_REMAINING = [1, 2, 3, 6, 9, 12]
palette = plt.cm.plasma(np.linspace(0.05, 0.95, len(SHOW_REMAINING)))

fig2, axes2 = plt.subplots(2, 2, figsize=(13, 9), sharey=True, sharex=True)
fig2.suptitle("Win Probability Curves  |  Batting Lead vs WP by Remaining Half-Innings", fontsize=13)

for ax, (outs, obc, label) in zip(axes2.flat, CURVE_STATES):
    sub_all = df[(df["outs"] == outs) & (df["obc"] == obc)]
    for color, rem in zip(palette, SHOW_REMAINING):
        sub = sub_all[sub_all["remaining"] == rem].sort_values("batting_lead")
        if not sub.empty:
            ax.plot(sub["batting_lead"], sub["win_prob"], color=color, linewidth=2, label=f"R={rem}")

    ax.axhline(0.5, color="gray", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.axvline(0,   color="gray", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_title(label, fontsize=10)
    ax.set_ylabel("Win probability", fontsize=9)
    ax.set_xlabel("Batting lead (runs)", fontsize=9)
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.grid(alpha=0.2)

legend_handles = [Line2D([0], [0], color=c, linewidth=2, label=f"R={r}")
                  for c, r in zip(palette, SHOW_REMAINING)]
fig2.legend(handles=legend_handles, title="Remaining\nhalf-innings",
            loc="lower center", ncol=len(SHOW_REMAINING), bbox_to_anchor=(0.5, -0.02), fontsize=9)
fig2.tight_layout(rect=[0, 0.06, 1, 1])
fig2.savefig("wp_inspect_curves.png", dpi=140, bbox_inches="tight")
print("Saved: wp_inspect_curves.png")

# ── Figure 3: Method coverage heatmaps ───────────────────────────────────────

METHOD_INT  = {"prior": 0, "re_collapsed": 1, "empirical": 2}
METHOD_CMAP = mcolors.ListedColormap(["#d73027", "#fee090", "#4575b4"])
METHOD_NORM = mcolors.BoundaryNorm([0, 1, 2, 3], METHOD_CMAP.N)

df["method_int"] = df["method"].map(METHOD_INT)

fig3, axes3 = plt.subplots(1, 4, figsize=(20, 7), sharey=True)
fig3.suptitle("Method Coverage  |  Remaining Half-Innings vs Batting Lead\n"
              "Blue = empirical    Yellow = RE-collapsed    Red = prior only", fontsize=13)

for ax, (outs, obc, label) in zip(axes3, STATES):
    pivot_m = get_pivot(outs, obc, value_col="method_int")
    pivot_n = get_pivot(outs, obc, value_col="n_samples")

    im3 = ax.imshow(pivot_m.values, aspect="auto", cmap=METHOD_CMAP, norm=METHOD_NORM)

    ax.set_xticks(range(len(ALL_LEADS)))
    ax.set_xticklabels([str(l) if l % 2 == 0 else "" for l in ALL_LEADS], fontsize=7)
    ax.set_yticks(range(len(ALL_REMAININGS)))
    ax.set_yticklabels([str(r) for r in ALL_REMAININGS], fontsize=7)

    for ri, rem in enumerate(ALL_REMAININGS):
        for ci, lead in enumerate(ALL_LEADS):
            try:
                n = int(pivot_n.loc[rem, lead])
                m = pivot_m.loc[rem, lead]
                if n > 0:
                    txt_color = "white" if m == 2 else "black"
                    ax.text(ci, ri, str(n), ha="center", va="center",
                            fontsize=5, color=txt_color)
            except (KeyError, ValueError):
                pass

    ax.set_title(label, fontsize=10)
    ax.set_xlabel("Batting lead (runs)", fontsize=9)
    ax.axvline(x=ALL_LEADS.index(0), color="white", linewidth=1.2, linestyle="--", alpha=0.5)

axes3[0].set_ylabel("Remaining half-innings", fontsize=9)
cbar3 = fig3.colorbar(im3, ax=axes3[-1], shrink=0.85)
cbar3.set_ticks([0.5, 1.5, 2.5])
cbar3.set_ticklabels(["prior", "re_collapsed", "empirical"])
fig3.tight_layout()
fig3.savefig("wp_inspect_coverage.png", dpi=140, bbox_inches="tight")
print("Saved: wp_inspect_coverage.png")

# ── Text sanity checks ────────────────────────────────────────────────────────

print("\n--- Tied game (lead=0), 0 outs, empty bases, by remaining ---")
chk = df[(df["batting_lead"] == 0) & (df["outs"] == 0) & (df["obc"] == "000")].sort_values("remaining", ascending=False)
print(chk[["remaining", "win_prob", "method", "n_samples"]].to_string(index=False))

print("\n--- Bottom of 6th (remaining=1), 0 outs, empty bases, by lead ---")
chk2 = df[(df["remaining"] == 1) & (df["outs"] == 0) & (df["obc"] == "000")].sort_values("batting_lead")
print(chk2[["batting_lead", "win_prob", "method", "n_samples"]].to_string(index=False))

print("\n--- Monotonicity check ---")
violations = 0
for (rem, outs, obc), grp in df.groupby(["remaining", "outs", "obc"]):
    wps = grp.sort_values("batting_lead")["win_prob"].values
    if any(wps[i] > wps[i + 1] + 1e-6 for i in range(len(wps) - 1)):
        violations += 1
print(f"  Violations: {violations} (expected 0)")

plt.show()

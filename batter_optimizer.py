#!/usr/bin/env python3
"""Batter attribute optimizer for Numberball MLN S12.

Enumerates all valid (con, eye, pwr, spd) combos summing to 12 (each 1-5),
computes expected batting stats against every S12 pitcher, and outputs:
  batter_optimizer.csv          -- batter combos ranked by xRV
  batter_steal_analysis.csv     -- steal EV by (speed, team)
  batter_optimizer.html         -- charts for both sections

Metrics
-------
  xRV         -- Expected Run Value per PA using state-weighted RE linear weights.
                 Linear weights are averaged over all (outs, obc) game states,
                 weighted by their historical frequency from state_frequencies.csv.
                 Falls back to 0-out empty-base weights if CSV is absent.
  OPS         -- Shown for comparison only.

Steal analysis (separate section)
  Steal defense per team = avg over all pitcher-catcher pairs on that team of:
    (pitcher_awr + catcher_eye) / 2
  Steal EV = P(safe) * RE_gain + (1-P(safe)) * RE_loss
  Computed for stealing 2nd (from 1B) for each (runner_spd, team) combination.

Speed/WH note
  Speed boosts 3B/2B/IF1B width and stealing. The WH runner-advancement bonus
  (extra bases on other batters' well-hit balls while already on base) is NOT
  captured -- compute_at_bat_ranges collapses WH results into plain 1B/2B and
  would require full inning-level simulation to properly model.
"""
from __future__ import annotations
import math
import sys
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8")

SUPABASE_URL = "https://qxwzrbbjvivbpqchbner.supabase.co"
SUPABASE_KEY = "sb_publishable_lnlBqNXEYmwjk2KgJiKeyw_CK2Qb2PR"
SEASON       = 12
BATTER_HAND  = "R"

OUTPUT_CSV       = "batter_optimizer.csv"
OUTPUT_STEAL_CSV = "batter_steal_analysis.csv"
OUTPUT_HTML      = "batter_optimizer.html"

# ---------- RE table and state frequencies (via utils if available) ----------

try:
    sys.path.insert(0, ".")
    import utils as _utils
    _RE        = _utils.RUN_EXPECTANCY
    _SW        = _utils._STATE_WEIGHTS      # {(remaining, outs, obc): freq}
    _ADV       = _utils.advance_runners
    _OUTS_ADD  = _utils.outs_added
    _HAS_UTILS = True
except Exception:
    _HAS_UTILS = False
    _RE = {
        (0, "000"): 0.67, (0, "001"): 1.00, (0, "010"): 1.31, (0, "100"): 1.52,
        (0, "011"): 1.61, (0, "101"): 1.89, (0, "110"): 2.02, (0, "111"): 2.51,
        (1, "000"): 0.39, (1, "001"): 0.65, (1, "010"): 0.79, (1, "100"): 1.01,
        (1, "011"): 1.06, (1, "101"): 1.23, (1, "110"): 1.50, (1, "111"): 1.55,
        (2, "000"): 0.17, (2, "001"): 0.35, (2, "010"): 0.41, (2, "100"): 0.48,
        (2, "011"): 0.63, (2, "101"): 0.72, (2, "110"): 0.82, (2, "111"): 0.98,
    }
    _SW       = {}
    _ADV      = None
    _OUTS_ADD = None


def _advance(result: str, obc: str, outs: int) -> tuple[str, int]:
    """Runner advancement wrapper - uses utils if available, else simple fallback."""
    if _ADV:
        return _ADV(result, obc, outs)
    # Minimal fallback for the five result types used in LW computation
    on3 = obc[0] == "1"; on2 = obc[1] == "1"; on1 = obc[2] == "1"
    runs = 0
    if result == "HR":
        runs = (1 if on1 else 0) + (1 if on2 else 0) + (1 if on3 else 0) + 1
        return "000", runs
    if result == "3B":
        runs = (1 if on3 else 0) + (1 if on2 else 0) + (1 if on1 else 0)
        return "100", runs
    if result == "2B":
        runs = (1 if on3 else 0) + (1 if on2 else 0)
        return f"0{'1' if on1 else '0'}1", runs
    if result in ("1B", "BB"):
        runs = 1 if on3 else 0
        return f"{'1' if on2 else '0'}{'1' if on1 else '0'}1", runs
    # OUT: no advancement
    return obc, 0


def _outs_added_simple(result: str) -> int:
    if _OUTS_ADD:
        return _OUTS_ADD(result)
    return 1 if result in ("GO",) else 0


# ---------- State-weighted linear weights ----------

def _compute_lw() -> tuple[dict[str, float], str]:
    """Compute RE linear weights averaged over game state frequencies.

    Returns (lw_dict, source_label) where source_label describes what was used.
    The five keys are: HR, 3B, 2B, 1B, BB, OUT.
    """
    # Aggregate state frequencies over (outs, obc) only, summing across 'remaining'
    outs_obc_freq: dict[tuple[int, str], float] = {}
    if _SW:
        for (rem, outs, obc_s), w in _SW.items():
            if outs >= 3:
                continue
            k = (int(outs), str(obc_s))
            outs_obc_freq[k] = outs_obc_freq.get(k, 0.0) + float(w)

    if not outs_obc_freq:
        # Fall back to single state: 0 outs, empty bases
        outs_obc_freq = {(0, "000"): 1.0}
        source = "0-out empty bases (state_frequencies.csv not found)"
    else:
        source = f"state-weighted ({len(outs_obc_freq)} states from state_frequencies.csv)"

    result_types = ["HR", "3B", "2B", "1B", "BB", "OUT"]
    # Map each to a canonical advance_runners result code
    result_code  = {"HR": "HR", "3B": "3B", "2B": "2B", "1B": "1B", "BB": "BB", "OUT": "GO"}

    accum: dict[str, float] = {k: 0.0 for k in result_types}
    total_w = 0.0

    for (outs, obc), w in outs_obc_freq.items():
        re_before = _RE.get((outs, obc), 0.0)
        for key in result_types:
            code = result_code[key]
            new_obc, runs = _advance(code, obc, outs)
            extra_outs = 1 if key == "OUT" else 0
            new_outs = min(outs + extra_outs, 3)
            re_after = 0.0 if new_outs >= 3 else _RE.get((new_outs, new_obc), 0.0)
            accum[key] += w * (float(runs) + re_after - re_before)
        total_w += w

    lw = {k: v / total_w for k, v in accum.items()} if total_w else {
        "HR": 1.0, "3B": 0.85, "2B": 0.64, "1B": 0.33, "BB": 0.33, "OUT": -0.28
    }
    return lw, source


# ---------- Steal table ----------

_STEAL_DIFFS = [
    -5, -4.5, -4, -3.5, -3, -2.5, -2, -1.5, -1, -0.5,
     0,  0.5,  1,  1.5,  2,  2.5,  3,  3.5,  4,  4.5, 5,
]
_STEAL_TABLE = {
    "2nd": [62, 86, 108, 132, 154, 177, 199, 221, 242, 265,
            285, 308, 329, 351, 373, 396, 418, 442, 464, 488, 499],
}

_RE_1B  = _RE.get((0, "001"), 1.00)
_RE_2B  = _RE.get((0, "010"), 1.31)
_RE_CS1 = _RE.get((1, "000"), 0.39)


def _steal_p_safe(spd: float, defense: float) -> float:
    """P(safe) when stealing 2nd. defense = (pitcher_awr + catcher_eye) / 2."""
    diff = max(-5.0, min(5.0, spd - defense))
    idx  = min(range(len(_STEAL_DIFFS)), key=lambda i: abs(_STEAL_DIFFS[i] - diff))
    return _STEAL_TABLE["2nd"][idx] / 501.0


def _steal_ev_2nd(spd: float, defense: float) -> float:
    """Expected run value of one steal-2nd attempt. Negative = don't steal."""
    p = _steal_p_safe(spd, defense)
    return p * (_RE_2B - _RE_1B) + (1 - p) * (_RE_CS1 - _RE_1B)


# ---------- At-bat mechanic (inlined from utils.py) ----------

_DIFFS = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]
_OBR_TABLE: dict[str, list[int]] = {
    "Hit":    [84,  99, 110, 119, 126, 132, 138, 145, 154, 165, 180],
    "HR":     [ 1,   1,   8,  16,  18,  20,  22,  24,  32,  47,  62],
    "3B":     [ 1,   1,   3,   4,   5,   6,   7,   8,   9,  11,  14],
    "2B":     [15,  20,  24,  27,  29,  30,  31,  33,  36,  40,  45],
    "IF1B":   [ 1,   2,   6,   8,   9,  10,  11,  12,  14,  18,  24],
    "BB":     [ 1,   3,  14,  23,  30,  35,  40,  47,  56,  67,  78],
    "FO_HND": [147, 132, 121, 112, 105, 100,  95,  88,  79,  68,  53],
    "PO_HND": [188, 171, 158, 146, 135, 125, 115, 104,  92,  79,  62],
    "K":      [183, 160, 142, 127, 115, 105,  95,  83,  68,  50,  27],
}
_1B_SPD_AWR   = {-5: -3, -4: -3, -3: -2, -2: -2, -1: -1, 0: 0, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3}
_1B_PITCH_AWR = {-3: 3, -2: 2, -1: 1, 0: 0, 1: -1, 2: -2, 3: -3}
_1B_HIT_NEG   = {-5: 5, -4: 5, -3: 5, -2: 5, -1: 5, 0: 3, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}


def _obr(key: str, diff: int) -> int:
    return _OBR_TABLE[key][_DIFFS.index(max(-5, min(5, diff)))]


def _compute_ranges(
    p_hand: str, p_mov: int, p_cmd: int, p_vel: int, p_awr: int,
    b_con: int, b_eye: int, b_pow: int, b_spd: int,
) -> dict[str, int]:
    """Slot widths (0-500, 501 total) for a bases-empty 0-out PA."""
    if BATTER_HAND.upper() == "S":
        hnd = 1.0
    elif p_hand.upper() == BATTER_HAND.upper():
        hnd = 0.975
    else:
        hnd = 1.025

    cl = lambda v: max(-5, min(5, int(v)))
    d_hit = cl(b_con - p_mov)
    d_pow = cl(b_pow - p_vel)
    d_spd = cl(b_spd - p_awr)
    d_eye = cl(b_eye - p_cmd)

    ws = lambda key, d: max(1, math.floor(_obr(key, d) * hnd))

    w_hr   = ws("HR",   d_pow)
    w_3b   = ws("3B",   d_spd)
    w_2b   = ws("2B",   d_spd)
    w_if1b = ws("IF1B", d_spd)
    w_bb   = ws("BB",   d_eye)
    w_k    = ws("K",    d_hit)

    hit_base = math.floor(_obr("Hit", d_hit) * hnd)
    w_1b = max(1,
        hit_base
        + _1B_HIT_NEG[d_hit]
        + _1B_SPD_AWR[d_spd]
        + _1B_PITCH_AWR.get(max(-3, min(3, p_awr - 3)), 0)
        + 5
        - w_hr - w_3b - w_2b
    )

    fo_rate    = _obr("FO_HND", d_pow) / 500
    po_rate    = _obr("PO_HND", d_pow) / 500
    after_hits = 500 - (w_hr + w_3b + w_2b + w_1b + w_if1b + w_bb)
    after_bb   = 500 - w_bb
    w_fo = max(1, math.floor(after_hits * fo_rate * (1 - po_rate)))
    w_po = max(1, math.floor(after_bb  * fo_rate * po_rate))
    w_go = 500 - (w_hr + w_3b + w_2b + w_1b + w_if1b + w_bb + w_fo + w_po + w_k) + 1

    return {
        "HR": w_hr, "3B": w_3b, "2B": w_2b, "1B": w_1b, "IF1B": w_if1b,
        "BB": w_bb, "FO": w_fo, "PO": w_po, "GO": w_go, "K": w_k,
    }


def _calc_stats(rng: dict[str, int], lw: dict[str, float]) -> dict[str, float]:
    PA  = 501.0
    bb  = rng["BB"]
    s1  = rng["1B"] + rng["IF1B"]
    s2  = rng["2B"]
    s3  = rng["3B"]
    hr  = rng["HR"]
    H   = s1 + s2 + s3 + hr
    AB  = PA - bb
    avg = H / AB if AB else 0.0
    obp = (H + bb) / PA
    slg = (s1 + 2 * s2 + 3 * s3 + 4 * hr) / AB if AB else 0.0

    outs = rng["FO"] + rng["PO"] + rng["GO"] + rng["K"]
    xRV = (
        hr  * lw["HR"]
        + s3 * lw["3B"]
        + s2 * lw["2B"]
        + s1 * lw["1B"]
        + bb * lw["BB"]
        + outs * lw["OUT"]
    ) / PA

    return {
        "BB_pct": bb / PA,
        "1B_pct": s1 / PA,
        "2B_pct": s2 / PA,
        "3B_pct": s3 / PA,
        "HR_pct": hr / PA,
        "H_pct":  H  / PA,
        "AVG":    avg,
        "OBP":    obp,
        "SLG":    slg,
        "OPS":    obp + slg,
        "xRV":    xRV,
    }


# ---------- main ----------

def main() -> None:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Single fetch: all S12 players with team, position, and all relevant attributes
    print("Fetching S12 players from Supabase...")
    resp = sb.table("players").select(
        "name, team, primary_pos, hand, mov, cmd, vel, awr, eye"
    ).eq("season", SEASON).execute()

    pitchers: list[dict] = []
    pitchers_by_team: dict[str, list[dict]] = {}
    catchers_by_team: dict[str, list[dict]] = {}

    for p in resp.data:
        team = str(p.get("team") or "Unknown")
        pos  = (p.get("primary_pos") or "").upper()
        name = p.get("name") or "Unknown"

        # Pitcher: has all four pitching attributes > 0
        try:
            mov, cmd, vel, awr = int(p["mov"]), int(p["cmd"]), int(p["vel"]), int(p["awr"])
            if all(v > 0 for v in (mov, cmd, vel, awr)):
                rec = {"name": name, "team": team, "hand": (p.get("hand") or "R").upper(),
                       "mov": mov, "cmd": cmd, "vel": vel, "awr": awr}
                pitchers.append(rec)
                pitchers_by_team.setdefault(team, []).append(rec)
        except (TypeError, ValueError, KeyError):
            pass

        # Catcher: primary_pos == 'C' with valid eye
        if pos == "C":
            try:
                eye = int(p["eye"])
                if eye > 0:
                    rec = {"name": name, "team": team, "eye": eye}
                    catchers_by_team.setdefault(team, []).append(rec)
            except (TypeError, ValueError, KeyError):
                pass

    n_pitchers  = len(pitchers)
    n_catchers  = sum(len(v) for v in catchers_by_team.values())
    n_p_teams   = len(pitchers_by_team)
    n_c_teams   = len(catchers_by_team)
    print(f"  {n_pitchers} pitchers across {n_p_teams} teams")
    print(f"  {n_catchers} catchers across {n_c_teams} teams")

    # Enumerate all valid batter combos: each 1-5, sum=12
    combos: list[tuple[int, int, int, int]] = []
    for con in range(1, 6):
        for eye in range(1, 6):
            for pwr in range(1, 6):
                spd = 12 - con - eye - pwr
                if 1 <= spd <= 5:
                    combos.append((con, eye, pwr, spd))
    print(f"  {len(combos)} batter attribute combos (con+eye+pwr+spd=12)")

    # State-weighted linear weights
    lw, lw_source = _compute_lw()
    print(f"\nLinear weights ({lw_source}):")
    print(f"  HR={lw['HR']:+.3f}  3B={lw['3B']:+.3f}  2B={lw['2B']:+.3f}  "
          f"1B={lw['1B']:+.3f}  BB={lw['BB']:+.3f}  OUT={lw['OUT']:+.3f}")

    # ── Batter xRV analysis ───────────────────────────────────────────────────
    print("\nComputing xRV for all combos x pitchers...")
    detail_rows: list[dict] = []
    for (con, eye, pwr, spd) in combos:
        for p in pitchers:
            rng = _compute_ranges(p["hand"], p["mov"], p["cmd"], p["vel"], p["awr"],
                                  con, eye, pwr, spd)
            st = _calc_stats(rng, lw)
            detail_rows.append({
                "con": con, "eye": eye, "pwr": pwr, "spd": spd,
                "batter_label": f"{con}-{eye}-{pwr}-{spd}",
                "pitcher": p["name"], "p_team": p["team"],
                **{k: round(v, 4) for k, v in {**rng, **st}.items()},
            })

    detail_df = pd.DataFrame(detail_rows)

    stat_cols = ["BB_pct", "1B_pct", "2B_pct", "3B_pct", "HR_pct", "H_pct",
                 "AVG", "OBP", "SLG", "OPS", "xRV"]
    summary = (
        detail_df
        .groupby(["con", "eye", "pwr", "spd", "batter_label"])[stat_cols]
        .mean()
        .reset_index()
        .sort_values("xRV", ascending=False)
        .reset_index(drop=True)
    )
    summary.index += 1
    summary.insert(0, "rank_xRV", summary.index)
    ops_rank = summary["OPS"].rank(ascending=False, method="first").astype(int)
    summary.insert(2, "rank_OPS", ops_rank)
    summary["xRV_per_500"] = (summary["xRV"] * 500).round(2)
    summary = summary.round(4)
    summary.to_csv(OUTPUT_CSV, index=False)
    print(f"Wrote {len(summary)} rows to {OUTPUT_CSV}")

    print(f"\nTop 10 combos by xRV (state-weighted):")
    print(summary[["batter_label", "xRV", "xRV_per_500", "OPS", "rank_OPS"]].head(10).to_string(index=False))

    # ── Steal analysis ────────────────────────────────────────────────────────
    print("\nComputing steal analysis by team...")

    # For each team, compute steal defense for all pitcher-catcher pairs
    all_teams = sorted(set(pitchers_by_team) | set(catchers_by_team))
    steal_rows: list[dict] = []
    team_steal_ev: dict[str, dict[int, float]] = {}  # team -> {spd -> avg_ev}

    for team in all_teams:
        team_pitchers = pitchers_by_team.get(team, [])
        team_catchers = catchers_by_team.get(team, [])
        if not team_pitchers or not team_catchers:
            continue

        team_steal_ev[team] = {}
        for spd in range(1, 6):
            pair_evs: list[float] = []
            for p in team_pitchers:
                for c in team_catchers:
                    defense = (p["awr"] + c["eye"]) / 2.0
                    ev = _steal_ev_2nd(float(spd), defense)
                    pair_evs.append(ev)
            avg_ev = sum(pair_evs) / len(pair_evs)
            team_steal_ev[team][spd] = round(avg_ev, 4)

            steal_rows.append({
                "team": team,
                "n_pitchers": len(team_pitchers),
                "n_catchers": len(team_catchers),
                "n_pairs": len(pair_evs),
                "avg_pitcher_awr": round(sum(p["awr"] for p in team_pitchers) / len(team_pitchers), 2),
                "avg_catcher_eye": round(sum(c["eye"] for c in team_catchers) / len(team_catchers), 2),
                "avg_steal_defense": round(
                    sum((p["awr"] + c["eye"]) / 2.0
                        for p in team_pitchers for c in team_catchers) / len(pair_evs), 2
                ),
                "runner_spd": spd,
                "steal_ev": round(avg_ev, 4),
                "steal_viable": avg_ev > 0,
            })

    steal_df = pd.DataFrame(steal_rows)
    if not steal_df.empty:
        steal_df.to_csv(OUTPUT_STEAL_CSV, index=False)
        print(f"Wrote {len(steal_df)} rows to {OUTPUT_STEAL_CSV}")

        # Summary: for each speed value, how many teams can you steal against?
        print("\nSteal viability by runner speed (across all teams with pitcher+catcher data):")
        spd_summary = steal_df.groupby("runner_spd").agg(
            avg_ev=("steal_ev", "mean"),
            viable_teams=("steal_viable", "sum"),
            total_teams=("steal_viable", "count"),
        ).reset_index()
        spd_summary["viable_pct"] = (spd_summary["viable_teams"] / spd_summary["total_teams"] * 100).round(1)
        print(spd_summary.to_string(index=False))

    # ── Charts ────────────────────────────────────────────────────────────────
    figs: list[tuple[str, str, go.Figure]] = []  # (section, title, fig)

    # Section 1: xRV batter analysis
    # ── 1a. xRV ranking ──────────────────────────────────────────────────────
    fig_xrv = go.Figure(go.Bar(
        x=summary["batter_label"],
        y=summary["xRV"],
        marker=dict(
            color=summary["xRV"],
            colorscale="RdYlGn",
            showscale=True,
            colorbar=dict(title="xRV", thickness=12),
        ),
        customdata=summary[["xRV_per_500", "OPS", "rank_OPS"]].values,
        hovertemplate=(
            "<b>%{x}</b><br>"
            "xRV: %{y:.4f}<br>"
            "xRV per 500 PA: %{customdata[0]:+.1f} runs<br>"
            "OPS: %{customdata[1]:.3f} (OPS rank #%{customdata[2]})"
            "<extra></extra>"
        ),
        text=summary["xRV"].apply(lambda v: f"{v:+.4f}"),
        textposition="outside",
        textfont=dict(size=7),
    ))
    fig_xrv.update_layout(
        title=(f"xRV per PA -- {lw_source}<br>"
               f"<sub>{BATTER_HAND}-hand batter vs avg S{SEASON} pitcher, "
               f"sorted by xRV descending</sub>"),
        xaxis_title="con-eye-pwr-spd",
        yaxis_title="xRV (runs per PA)",
        height=540,
        xaxis=dict(tickangle=90, tickfont=dict(size=7)),
        margin=dict(b=150, t=80, r=100),
    )
    figs.append(("batting", "xRV Ranking", fig_xrv))

    # ── 1b. xRV vs OPS scatter ───────────────────────────────────────────────
    fig_scat = go.Figure(go.Scatter(
        x=summary["OPS"],
        y=summary["xRV"],
        mode="markers+text",
        text=summary["batter_label"],
        textposition="top center",
        textfont=dict(size=7),
        marker=dict(
            size=8,
            color=summary["spd"],
            colorscale="Blues",
            showscale=True,
            colorbar=dict(title="SPD", thickness=12),
        ),
        hovertemplate=(
            "<b>%{text}</b><br>OPS: %{x:.3f}<br>xRV: %{y:+.4f}<extra></extra>"
        ),
    ))
    fig_scat.update_layout(
        title="OPS vs xRV -- points above the trend have more true value than OPS suggests",
        xaxis_title="OPS",
        yaxis_title="xRV (runs per PA)",
        height=560,
        margin=dict(t=60),
    )
    figs.append(("batting", "OPS vs xRV", fig_scat))

    # ── 1c. Metrics by attribute value ───────────────────────────────────────
    attrs = [("con", "Contact"), ("eye", "Eye"), ("pwr", "Power"), ("spd", "Speed")]
    fig_attrs = make_subplots(rows=2, cols=2, subplot_titles=[n for _, n in attrs])
    lines = [("xRV", "#e74c3c"), ("OPS", "#2c3e50"), ("AVG", "#27ae60"), ("OBP", "#2980b9")]
    for i, (attr, name) in enumerate(attrs):
        r, c = divmod(i, 2)
        grp = summary.groupby(attr)[["xRV", "OPS", "AVG", "OBP"]].mean().reset_index()
        for col_name, color in lines:
            fig_attrs.add_trace(go.Scatter(
                x=grp[attr], y=grp[col_name],
                mode="lines+markers",
                name=col_name,
                line=dict(color=color, width=2),
                marker=dict(size=8),
                legendgroup=col_name,
                showlegend=(i == 0),
            ), row=r + 1, col=c + 1)
        fig_attrs.update_xaxes(title_text=name, tickvals=[1, 2, 3, 4, 5], row=r + 1, col=c + 1)
    fig_attrs.update_layout(
        title="Avg Metrics by Attribute Value (other attributes averaged over)",
        height=540,
    )
    figs.append(("batting", "Metrics vs Attribute", fig_attrs))

    # ── 1d. xRV heatmaps ─────────────────────────────────────────────────────
    for (a1, a2, t1, t2) in [("con", "pwr", "Contact", "Power"),
                              ("eye", "spd", "Eye",     "Speed")]:
        pivot = summary.pivot_table(values="xRV", index=a1, columns=a2, aggfunc="mean")
        fig_hm = go.Figure(go.Heatmap(
            z=pivot.values,
            x=[str(v) for v in pivot.columns],
            y=[str(v) for v in pivot.index],
            colorscale="RdYlGn",
            zmid=float(summary["xRV"].median()),
            text=[[f"{v:+.4f}" for v in row] for row in pivot.values],
            texttemplate="%{text}",
            hovertemplate=f"{t1}=%{{y}}, {t2}=%{{x}}<br>xRV=%{{z:+.4f}}<extra></extra>",
            showscale=True,
        ))
        fig_hm.update_layout(
            title=f"xRV: {t1} vs {t2} (averaged over the other two attributes)",
            xaxis_title=t2, yaxis_title=t1,
            height=400, margin=dict(l=60, r=80, t=60, b=60),
        )
        figs.append(("batting", f"xRV Heatmap {t1} vs {t2}", fig_hm))

    # ── 1e. Top 20 table ──────────────────────────────────────────────────────
    top_n = 20
    top20 = summary.head(top_n)[
        ["rank_xRV", "rank_OPS", "batter_label",
         "con", "eye", "pwr", "spd",
         "xRV", "xRV_per_500", "AVG", "OBP", "SLG", "OPS"]
    ].copy()
    col_heads = ["xRV#", "OPS#", "Combo", "CON", "EYE", "PWR", "SPD",
                 "xRV", "xRV/500PA", "AVG", "OBP", "SLG", "OPS"]
    cells = []
    for col in top20.columns:
        if col == "xRV_per_500":
            cells.append(top20[col].apply(lambda v: f"{v:+.1f}").tolist())
        elif col == "xRV":
            cells.append(top20[col].apply(lambda v: f"{v:+.4f}").tolist())
        elif col in {"AVG", "OBP", "SLG", "OPS"}:
            cells.append(top20[col].apply(lambda v: f"{v:.3f}").tolist())
        else:
            cells.append(top20[col].tolist())
    row_bg = [["#f0f3f4" if i % 2 else "#ffffff" for i in range(top_n)] for _ in top20.columns]
    fig_tbl = go.Figure(go.Table(
        header=dict(values=col_heads, fill_color="#2c3e50",
                    font=dict(color="white", size=11), align="center", height=30),
        cells=dict(values=cells, fill_color=row_bg,
                   align="center", font=dict(size=10), height=26),
    ))
    fig_tbl.update_layout(title=f"Top {top_n} by xRV", height=640, margin=dict(t=60))
    figs.append(("batting", f"Top {top_n} Table", fig_tbl))

    # Section 2: Steal analysis (only if we have data)
    if not steal_df.empty:
        teams_with_data = sorted(team_steal_ev.keys())

        # ── 2a. Steal EV heatmap: runner SPD vs team ──────────────────────────
        spd_vals = [1, 2, 3, 4, 5]
        z_steal = [
            [team_steal_ev[t].get(spd, float("nan")) for t in teams_with_data]
            for spd in spd_vals
        ]
        fig_steal_hm = go.Figure(go.Heatmap(
            z=z_steal,
            x=teams_with_data,
            y=[str(s) for s in spd_vals],
            colorscale=[[0.0, "#c0392b"], [0.5, "#f7f7f7"], [1.0, "#27ae60"]],
            zmid=0,
            text=[[f"{v:+.4f}" if not math.isnan(v) else "" for v in row] for row in z_steal],
            texttemplate="%{text}",
            hovertemplate="Team: %{x}<br>Runner SPD: %{y}<br>Steal EV: %{z:+.4f}<extra></extra>",
            showscale=True,
            colorbar=dict(title="EV (runs)", thickness=12),
        ))
        fig_steal_hm.update_layout(
            title=("Steal 2nd Expected Run Value: Runner SPD vs Opposing Team<br>"
                   "<sub>Green=profitable, Red=don't steal. "
                   "Defense = avg(pitcher AWR + catcher Eye) / 2 per team "
                   "across all pitcher-catcher pairs.</sub>"),
            xaxis_title="Opposing Team",
            yaxis_title="Runner Speed",
            height=400,
            xaxis=dict(tickangle=45),
            margin=dict(l=60, r=100, t=100, b=100),
        )
        figs.append(("steal", "Steal EV: SPD vs Team", fig_steal_hm))

        # ── 2b. Team steal defense bar chart ─────────────────────────────────
        team_defense = steal_df.drop_duplicates("team")[
            ["team", "avg_pitcher_awr", "avg_catcher_eye", "avg_steal_defense", "n_pairs"]
        ].sort_values("avg_steal_defense", ascending=False).reset_index(drop=True)

        fig_def = go.Figure()
        fig_def.add_trace(go.Bar(
            name="Avg pitcher AWR",
            x=team_defense["team"],
            y=team_defense["avg_pitcher_awr"],
            marker_color="#8e44ad",
        ))
        fig_def.add_trace(go.Bar(
            name="Avg catcher EYE",
            x=team_defense["team"],
            y=team_defense["avg_catcher_eye"],
            marker_color="#2980b9",
        ))
        # Overlay line for combined defense
        fig_def.add_trace(go.Scatter(
            name="Combined defense (avg)",
            x=team_defense["team"],
            y=team_defense["avg_steal_defense"],
            mode="markers+lines",
            marker=dict(size=10, color="#c0392b", symbol="diamond"),
            line=dict(color="#c0392b", width=2, dash="dot"),
            yaxis="y",
        ))
        fig_def.update_layout(
            title=("Team Steal Defense: Pitcher AWR + Catcher EYE<br>"
                   "<sub>Combined defense = (pitcher AWR + catcher EYE) / 2 "
                   "averaged over all pitcher-catcher pairs on each team</sub>"),
            xaxis_title="Team",
            yaxis_title="Attribute value",
            barmode="group",
            height=460,
            xaxis=dict(tickangle=45),
            margin=dict(b=120, t=100),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        figs.append(("steal", "Team Steal Defense", fig_def))

        # ── 2c. Steal EV curve by speed ───────────────────────────────────────
        fig_spd = go.Figure()
        for team in teams_with_data:
            evs = [team_steal_ev[team].get(s, 0) for s in spd_vals]
            any_viable = any(e > 0 for e in evs)
            fig_spd.add_trace(go.Scatter(
                x=spd_vals, y=evs,
                mode="lines+markers",
                name=team,
                line=dict(width=1.5 if any_viable else 1,
                          dash="solid" if any_viable else "dot"),
                opacity=0.9 if any_viable else 0.4,
                hovertemplate=f"Team: {team}<br>SPD: %{{x}}<br>Steal EV: %{{y:+.4f}}<extra></extra>",
            ))
        # Add break-even line
        fig_spd.add_hline(y=0, line_dash="dash", line_color="black",
                          annotation_text="Break-even", annotation_position="right")
        fig_spd.update_layout(
            title=("Steal 2nd EV by Runner Speed (per team)<br>"
                   "<sub>Solid lines = any speed profitable; dotted = never profitable against this team. "
                   "Break-even at EV=0.</sub>"),
            xaxis_title="Runner Speed",
            yaxis_title="Steal EV (runs per attempt)",
            xaxis=dict(tickvals=[1, 2, 3, 4, 5]),
            height=500,
            margin=dict(t=100),
        )
        figs.append(("steal", "Steal EV Curves by Team", fig_spd))

        # ── 2d. Steal summary table by speed ──────────────────────────────────
        steal_tbl = spd_summary.copy()
        steal_tbl["avg_ev"] = steal_tbl["avg_ev"].round(4)
        fig_steal_tbl = go.Figure(go.Table(
            header=dict(
                values=["Runner SPD", "Avg Steal EV", "Viable Teams", "Total Teams", "Viable %"],
                fill_color="#2c3e50",
                font=dict(color="white", size=12),
                align="center",
            ),
            cells=dict(
                values=[
                    steal_tbl["runner_spd"].tolist(),
                    steal_tbl["avg_ev"].apply(lambda v: f"{v:+.4f}").tolist(),
                    steal_tbl["viable_teams"].astype(int).tolist(),
                    steal_tbl["total_teams"].astype(int).tolist(),
                    steal_tbl["viable_pct"].apply(lambda v: f"{v:.0f}%").tolist(),
                ],
                fill_color=[["#f0f3f4" if i % 2 else "#ffffff" for i in range(5)]
                            for _ in range(5)],
                align="center",
                font=dict(size=11),
            ),
        ))
        fig_steal_tbl.update_layout(
            title="Steal 2nd Summary by Runner Speed (avg across all teams)",
            height=280,
            margin=dict(t=60),
        )
        figs.append(("steal", "Steal Summary by Speed", fig_steal_tbl))

    # ── Write HTML ────────────────────────────────────────────────────────────
    lw_str = (f"HR={lw['HR']:+.3f} | 3B={lw['3B']:+.3f} | 2B={lw['2B']:+.3f} | "
              f"1B={lw['1B']:+.3f} | BB={lw['BB']:+.3f} | OUT={lw['OUT']:+.3f}")

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(
            "<!DOCTYPE html>\n<html>\n<head>\n"
            '  <meta charset="utf-8">\n'
            "  <title>Numberball Batter Optimizer</title>\n"
            '  <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>\n'
            "  <style>\n"
            "    body { font-family: sans-serif; margin: 20px; background: #f8f9fa; color: #2c3e50; }\n"
            "    h1, h2 { border-bottom: 2px solid #2c3e50; padding-bottom: 6px; }\n"
            "    h3 { margin-top: 28px; color: #34495e; }\n"
            "    p.meta { color: #7f8c8d; font-size: 0.9em; margin: 4px 0; }\n"
            "    p.note { background: #fef9e7; border-left: 4px solid #f39c12; "
            "             padding: 8px 12px; font-size: 0.9em; margin: 12px 0; }\n"
            "  </style>\n"
            "</head>\n<body>\n"
        )
        f.write(f"<h1>Numberball S{SEASON} Batter Attribute Optimizer</h1>\n")
        f.write(
            f'<p class="meta">{n_pitchers} pitchers | {n_catchers} catchers | '
            f"{len(combos)} batter combos | batter hand: {BATTER_HAND} | "
            f"CON/EYE/PWR/SPD each 1-5, sum=12</p>\n"
        )

        # Section 1: batting / xRV
        f.write("<h2>Section 1: Batter Attribute Analysis (xRV)</h2>\n")
        f.write(
            f'<p class="meta">Linear weights ({lw_source}): {lw_str}</p>\n'
            f'<p class="meta">xRV per 500 PA column shows runs difference vs a batter with xRV=0 '
            f"(differences of ~5+ runs per 500 PA are meaningful).</p>\n"
            f'<p class="note">WH baserunning bonus (extra bases on well-hit balls while already on base) '
            f"is NOT captured -- compute_at_bat_ranges collapses WH results into plain 1B/2B and "
            f"would require full inning-level simulation to model.</p>\n"
        )
        for section, title, fig in figs:
            if section == "batting":
                f.write(f"<h3>{title}</h3>\n")
                f.write(fig.to_html(full_html=False, include_plotlyjs=False))
                f.write("\n")

        # Section 2: steal
        if not steal_df.empty:
            f.write("<h2>Section 2: Steal Analysis (separate from batting xRV)</h2>\n")
            f.write(
                f'<p class="meta">Steal defense per team = avg over all pitcher-catcher pairs on that team of '
                f"(pitcher AWR + catcher EYE) / 2.</p>\n"
                f'<p class="meta">Steal EV = P(safe) * RE_gain + (1-P(safe)) * RE_loss '
                f"where RE values are from the {SEASON} run expectancy table.</p>\n"
                f'<p class="note">Break-even P(safe) for stealing 2nd = '
                f"{abs(_RE_CS1 - _RE_1B) / abs((_RE_2B - _RE_1B) - (_RE_CS1 - _RE_1B)):.1%}. "
                f"Stealing 3rd is excluded (STEAL_TABLE max success rate 51.7% &lt; ~71% break-even).</p>\n"
            )
            for section, title, fig in figs:
                if section == "steal":
                    f.write(f"<h3>{title}</h3>\n")
                    f.write(fig.to_html(full_html=False, include_plotlyjs=False))
                    f.write("\n")

        f.write("</body>\n</html>\n")

    print(f"\nWrote charts to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()

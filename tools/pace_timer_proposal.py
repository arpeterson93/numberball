"""Replay the league's real wait times under alternative timers ("timer + dead window") to estimate time savings.

  python -I tools/pace_timer_proposal.py [pa_detail.csv] [out folder]

Input is tools/probe_output/pace_league/pa_detail.csv (from tools/pace_league.py: every timed PA, S11-S13). Output goes to
tools/pace_timer_proposal/ (default): CSVs plus three charts. Findings are written up in tools/pace_timer_proposal/README.md.

A wait is one stretch someone owed a move: a batter's is ping -> swing, a pitcher's is the gap before the batter's ping
(ump relay time already taken out; a no-ping result ends at the result). Times are Central. The timer today is 12 hours
(autos land at ~12h), so waits are capped at 12h and savings are measured against that.

Proposal "T+D": the timer counts T hours a wait, not counting a dead window of D hours a day that each player picks. Here a
player's window is their own quietest D-hour block (fewest wait ends in that block; league window if under 12 waits). The
replay assumes nobody responds any differently (a wait that would pass T counted hours is cut at T) - a floor on savings.
Behaviour scenario: counted time past T/2 shrinks by factor k (people answer earlier), and a share p of the waits that would
still hit the timer answer 30 min before it instead of auto'ing.
"""
from __future__ import annotations

import pathlib
import sys

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAP = 720                    # minutes: today's 12h timer
E = np.arange(1, CAP + 1)
MIN_WAITS = 12               # fewer waits than this: use the league window


def load_waits(path: pathlib.Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for c in ("ping_central", "result_central"):
        d[c] = pd.to_datetime(d[c])
    rows = []
    for r in d.itertuples():
        if pd.notna(r.hit_raw_min) and pd.notna(r.ping_central):
            rows.append(("bat", r.batter, r.ping_central, r.hit_raw_min, r.game_code, r.season, r.batting_team))
        if pd.notna(r.pitching_raw_min):
            end = r.result_central if r.no_ping else r.ping_central
            if pd.notna(end):
                rows.append(("pit", r.pitcher, end - pd.Timedelta(minutes=r.pitching_raw_min), r.pitching_raw_min,
                             r.game_code, r.season, r.fielding_team))
    w = pd.DataFrame(rows, columns=["role", "who", "start", "dur", "game", "season", "team"])
    w["end"] = w.start + pd.to_timedelta(w.dur, unit="m")
    w["startmin"] = w.start.dt.hour * 60 + w.start.dt.minute
    w["endh"] = w.end.dt.hour
    w["base"] = np.minimum(w.dur.values, CAP) / 60          # hours, 12h cap
    return w


class Replay:
    def __init__(self, w: pd.DataFrame):
        self.w = w
        self.base = np.minimum(w.dur.values, CAP).astype(int)
        self.total = self.base.sum() / 60
        self.act = w.groupby("endh").size()
        self._win: dict[int, np.ndarray] = {}

    def window_starts(self, D: int) -> np.ndarray:
        """Per wait: the start hour of its player's quietest D-hour block."""
        if D not in self._win:
            fb = min(range(24), key=lambda h: sum(self.act.get((h + k) % 24, 0) for k in range(D))) if D else 0
            o = {}
            for who, g in self.w.groupby("who"):
                if len(g) < MIN_WAITS or not D:
                    o[who] = fb
                    continue
                c = np.bincount(g.endh, minlength=24)
                o[who] = min(range(24), key=lambda h: sum(c[(h + k) % 24] for k in range(D)))
            self._win[D] = self.w.who.map(o).values
        return self._win[D]

    def counted(self, idx, D):
        """cumulative counted minutes after each elapsed minute, for the waits at idx (dead window excluded)."""
        m = (self.w.startmin.values[idx][:, None] + E[None, :] - 1) % 1440
        dead = ((m - self.window_starts(D)[idx][:, None] * 60) % 1440) < D * 60
        return np.cumsum(~dead, axis=1)

    def savings(self, T, D):
        """per-wait hours saved vs the 12h timer, nothing else changing."""
        s = np.zeros(len(self.w))
        idx = np.where(self.w.dur.values > T * 60)[0]
        if not len(idx):
            return s
        hit = self.counted(idx, D) >= T * 60
        first = np.where(hit.any(1), hit.argmax(1) + 1, 10 ** 6)
        b = self.base[idx]
        ok = first < b
        s[idx[ok]] = (b[ok] - first[ok]) / 60
        return s

    def behaviour(self, T, D, k, p, sfrac=0.5):
        """(elapsed if responds, elapsed if auto, P(respond)) per wait, minutes."""
        sm = T * 60 * sfrac
        e1 = self.base.astype(float)
        e2 = e1.copy()
        pr = np.ones(len(self.w))
        idx = np.where(self.base > sm)[0]
        c = self.counted(idx, D)
        b = self.base[idx]
        ctot = c[np.arange(len(idx)), b - 1]
        cn = np.where(ctot > sm, sm + (ctot - sm) * k, ctot)
        over = cn > T * 60

        def el(cc):
            hit = c >= np.maximum(cc, 1)[:, None]
            return np.minimum(np.where(hit.any(1), hit.argmax(1) + 1, b), b)
        er = np.where(over, el(np.full(len(idx), T * 60 - 30)), np.where(cn < ctot, el(cn), b))
        e1[idx] = er
        e2[idx] = np.where(over, el(np.full(len(idx), T * 60)), er)
        pr[idx] = np.where(over, p, 1.0)
        return e1, e2, pr

    def behaviour_totals(self, T, D, k, p):
        e1, e2, pr = self.behaviour(T, D, k, p)
        saved = (self.base - (pr * e1 + (1 - pr) * e2)).sum() / 60
        autos = ((1 - pr) * (e2 < self.base)).sum()
        return saved, autos


def elapsed_histograms(w, out):
    lab = [f"{i}-{i + 1}" for i in range(12)] + ["12+"]
    b = np.minimum(np.floor(w.base).astype(int), 12)
    sets = {"Batters": w.role == "bat", "Pitchers": w.role == "pit", "Combined": w.role.notna()}
    n = pd.DataFrame({k: np.bincount(b[v], minlength=13) for k, v in sets.items()}, index=lab)
    h = pd.DataFrame({k: np.bincount(b[v], weights=w.base[v], minlength=13) for k, v in sets.items()}, index=lab).round(0)
    n.to_csv(out / "elapsed_counts.csv")
    h.to_csv(out / "elapsed_hours.csv")
    fig, ax = plt.subplots(2, 3, figsize=(15, 7), sharex=True)
    for j, k in enumerate(sets):
        for i, (t, df) in enumerate((("waits", n), ("hours of waiting", h))):
            ax[i, j].bar(range(13), df[k], color=["#4c78a8"] * 8 + ["#e8873a"] * 4 + ["#c0392b"])
            ax[i, j].set_title(f"{k}: {t}", fontsize=10)
            ax[i, j].axvline(7.5, color="k", ls=":", lw=1)
            ax[i, j].set_xticks(range(13))
            ax[i, j].set_xticklabels(lab, fontsize=7, rotation=60)
    plt.tight_layout()
    plt.savefig(out / "elapsed_hist.png", dpi=110)
    plt.close()


def grid(rp, out):
    Ts, Ds = list(range(6, 13)), list(range(7))
    hrs = pd.DataFrame(index=Ts, columns=Ds, dtype=float)
    cut = hrs.copy()
    for T in Ts:
        for D in Ds:
            s = rp.savings(T, D)
            hrs.loc[T, D], cut.loc[T, D] = s.sum(), (s > 0).sum()
    hrs.round(0).to_csv(out / "grid_hours_saved.csv")
    cut.to_csv(out / "grid_waits_cut.csv")
    nw = len(rp.w)
    fig, axs = plt.subplots(1, 2, figsize=(17, 6))
    for ax, M, cm, title, fmt in (
            (axs[0], hrs, "YlOrRd", f"Hours of waiting saved (share of {rp.total:,.0f}h)", lambda v: f"{v:,.0f}h\n{v / rp.total * 100:.1f}%"),
            (axs[1], cut, "PuBu", f"Waits cut (share of {nw:,} waits)", lambda v: f"{v:,.0f}\n{v / nw * 100:.1f}%")):
        ax.imshow(M.values.astype(float), cmap=cm, aspect="auto")
        for i, T in enumerate(Ts):
            for j, D in enumerate(Ds):
                v = M.iloc[i, j]
                ax.text(j, i, fmt(v), ha="center", va="center", fontsize=8, color="white" if v > M.values.max() * 0.65 else "black")
                edge = {8: "#1f4e79", 10: "#2e7d32", 12: "#6a1b9a"}.get(T + D)
                if edge:
                    ax.add_patch(plt.Rectangle((j - .5, i - .5), 1, 1, fill=False, ec=edge, lw=2.5))
        ax.set_xticks(range(7))
        ax.set_xticklabels(Ds)
        ax.set_yticks(range(7))
        ax.set_yticklabels(Ts)
        ax.set_xlabel("dead window (hours/day, each player's quietest)")
        ax.set_ylabel("timer (counted hours)")
        ax.set_title(title, fontsize=10)
    fig.suptitle("Timer x dead window vs today's 12h timer. Outline = max wait: blue 8h, green 10h, purple 12h", fontsize=11)
    plt.tight_layout()
    plt.savefig(out / "grid_both.png", dpi=110)
    plt.close()


def per_game_team(rp, out, T=8, D=4):
    w = rp.w.copy()
    w["saved"] = rp.savings(T, D)
    g = w.groupby("game").agg(season=("season", "first"), waiting_h=("base", "sum"), saved_h=("saved", "sum"))
    g["saved_pct"] = g.saved_h / g.waiting_h * 100
    g.sort_values("waiting_h", ascending=False).round(2).to_csv(out / f"per_game_{T}+{D}.csv")
    t = w.groupby("team").agg(waits=("dur", "size"), waiting_h=("base", "sum"), saved_h=("saved", "sum"), cut=("saved", lambda x: (x > 0).sum()))
    t["saved_pct"] = t.saved_h / t.waiting_h * 100
    t.sort_values("saved_pct", ascending=False).round(2).to_csv(out / f"per_team_{T}+{D}.csv")
    return g


def behaviour_table(rp, out):
    rows = []
    for T, D in ((8, 4), (6, 6)):
        for k in (1.0, 0.9, 0.75, 0.5):
            for p in (0.0, 0.5, 1.0):
                s, a = rp.behaviour_totals(T, D, k, p)
                rows.append((f"{T}+{D}", k, p, round(s), round(s / rp.total * 100, 1), round(a)))
    df = pd.DataFrame(rows, columns=["timer", "tail_factor_k", "share_answer_in_time_p", "hours_saved", "pct_of_waiting", "autos"])
    df.to_csv(out / "behaviour_scenarios.csv", index=False)
    return df


def main():
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools" / "probe_output" / "pace_league" / "pa_detail.csv"
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "tools" / "pace_timer_proposal"
    out.mkdir(parents=True, exist_ok=True)
    w = load_waits(src)
    rp = Replay(w)
    print(f"{len(w):,} waits, {w.who.nunique()} players, {w.game.nunique()} games, {rp.total:,.0f}h of waiting (12h cap)")
    elapsed_histograms(w, out)
    grid(rp, out)
    g = per_game_team(rp, out)
    print(behaviour_table(rp, out).to_string(index=False))
    s = rp.savings(8, 4)
    print(f"8+4: {s.sum():,.0f}h saved ({s.sum() / rp.total * 100:.1f}%), {(s > 0).sum()} waits cut; per game median {g.saved_h.median():.1f}h")


if __name__ == "__main__":
    main()

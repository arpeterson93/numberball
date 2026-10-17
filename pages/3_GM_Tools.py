"""GM Tools - manager-side tools. First up: the Hitting Lab (which builds, lineup and batting order score the most runs)."""
from __future__ import annotations

import json
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import database as db
import hitting_lab as hl
import offense_sim as o

_dark_mode = st.context.theme.type == "dark"
_TEMPLATE = "plotly_dark" if _dark_mode else "plotly_white"
_STATS = ("CON", "EYE", "PWR", "SPD")
_STAT_COLORS = {"CON": "#4c78a8", "EYE": "#54a24b", "PWR": "#e45756", "SPD": "#f2a900"}
_GAMES_PER_SEASON = 14


# ----------------------------------------------------------------------------------------------- progress
def _set_overlay_label(text: str) -> None:
    """The app-wide loading overlay (app.py) shows a label under its spinner; this writes a message into it."""
    components.html("<script>(function(){var l=window.parent.document.getElementById('nb-lbl');"
                    "if(l){l.textContent=" + json.dumps(text) + ";}})();</script>", height=0)


class _Progress:
    """Percent complete shown in the loading overlay (which covers the page while a tool runs) and in an inline bar."""

    def __init__(self, label: str):
        self.label = label
        self.bar = st.progress(0.0, text=f"{label}... 0%")
        self.slot = st.empty()
        self.last = -1
        self._t = 0.0
        self.update(0.0)

    def update(self, frac: float, text: str | None = None) -> None:
        frac = min(max(float(frac), 0.0), 1.0)
        pct = int(round(frac * 100))
        now = time.monotonic()
        if pct == self.last and text is None:
            return
        if self.last >= 0 and frac < 1.0 and now - self._t < 0.15:
            return                                   # at most ~7 updates a second
        self._t = now
        self.last = pct
        msg = f"{text or self.label}... {pct}%"
        self.bar.progress(frac, text=msg)
        with self.slot:
            _set_overlay_label(msg)

    def done(self) -> None:
        self.bar.empty()
        with self.slot:
            _set_overlay_label("Loading\u2026")


# ----------------------------------------------------------------------------------------------- cached data
@st.cache_data(ttl=600, show_spinner=False)
def _players() -> pd.DataFrame:
    df = pd.DataFrame(db.get_all_players())
    df = df[~df["s_id"].astype(str).str.startswith("R_")]
    df = df[df["season"].notna() & df["player_id"].notna()].copy()
    df["season"] = df["season"].astype(int)
    df["player_id"] = df["player_id"].astype(int)
    df["team"] = df["team"].fillna("").astype(str).str.strip()
    return df.drop_duplicates(["season", "player_id"])


@st.cache_resource(show_spinner=False)
def _model(innings: int, catcher_key: tuple, steals: bool, hnr: bool, decision: str, lead: int,
           bottom: bool, engine: int) -> o.OffenseModel:
    """catcher_key = ((eye, weight), ...): the opposing catchers' EYE distribution."""
    return o.OffenseModel(innings=innings, catcher_mix=dict(catcher_key), steals=steals, hit_and_run=hnr,
                          decision=decision, lead=lead, bottom=bottom)


def _pitcher_builds(players: pd.DataFrame, season: int) -> dict:
    p = players[(players["season"] == season) & players["mov"].notna() & players["cmd"].notna()
                & players["vel"].notna() & players["awr"].notna()]
    return {int(r.player_id): ((r.hand or "R") if isinstance(r.hand, str) else "R", int(r.mov), int(r.cmd),
                               int(r.vel), int(r.awr)) for r in p.itertuples()}


@st.cache_resource(show_spinner=False)
def _opponent(spec: tuple):
    """spec = ('mix', season, top) | ('pitcher', season, player_id) | ('custom', hand, mov, cmd, vel, awr)."""
    if spec[0] == "custom":
        return o.Pitcher(spec[1], spec[2], spec[3], spec[4], spec[5])
    players = _players()
    builds = _pitcher_builds(players, spec[1])
    if spec[0] == "pitcher":
        return o.Pitcher(*builds[spec[2]])
    return hl.pitcher_mix(db.get_pitcher_faced_counts(spec[1]), builds, spec[2])


def _default_lineup(season: int, abbrev: str, full_name: str, hitters: pd.DataFrame) -> list[int]:
    """player_ids of the nine hitters who bat most for the team, in their typical batting order."""
    rows = db.get_team_swing_order(full_name, season)
    seen_by_game: dict = {}
    for r in rows:
        seen = seen_by_game.setdefault(r["game_id"], [])
        if r["batter_id"] not in seen and len(seen) < 9:
            seen.append(r["batter_id"])
    slots: dict = {}
    for seen in seen_by_game.values():
        for i, pid in enumerate(seen):
            slots.setdefault(int(pid), []).append(i + 1)
    ids = set(hitters["player_id"].astype(int))
    ranked = sorted((p for p in slots if p in ids), key=lambda p: -len(slots[p]))[:9]
    ranked.sort(key=lambda p: sum(slots[p]) / len(slots[p]))
    for pid in hitters["player_id"].astype(int):
        if len(ranked) >= 9:
            break
        if pid not in ranked:
            ranked.append(pid)
    return ranked[:9]


def _lineup_frame(hitters: pd.DataFrame, ids: list[int]) -> pd.DataFrame:
    by = hitters.set_index("player_id")
    rows = []
    for slot, pid in enumerate(ids, start=1):
        r = by.loc[pid]
        rows.append({"Slot": slot, "Player": r["name"], "Hand": (r["hand"] if isinstance(r["hand"], str) else "R"),
                     "CON": int(r["con"]), "EYE": int(r["eye"]), "PWR": int(r["pwr"]), "SPD": int(r["spd"]),
                     "_pid": int(pid)})
    return pd.DataFrame(rows)


def _sig(*parts) -> str:
    return repr(parts)


# ----------------------------------------------------------------------------------------------- the lab
def hitting_lab() -> None:
    _set_overlay_label("Loading\u2026")
    st.caption(
        "Expected runs for a lineup, solved exactly from the stadium sheet's result ranges, the baserunning chart "
        "and the steal table. Change builds, swap players, and see what each hitter is worth.")

    players = _players()
    seasons = sorted(players[players["con"].notna()]["season"].unique(), reverse=True)
    c1, c2 = st.columns(2)
    season = c1.selectbox("Season", seasons, key="gl_season")
    teams = db.get_mln_teams_for_season(int(season))
    if not teams:
        st.info("No MLN teams found for that season.")
        return
    labels = {t["abbrev"]: f'{t["abbrev"]} - {t["full_team"]}' for t in teams}
    abbrev = c2.selectbox("Team", list(labels), format_func=labels.get, key="gl_team")
    full_name = next(t["full_team"] for t in teams if t["abbrev"] == abbrev)
    hitters = players[(players["season"] == season) & (players["team"] == abbrev) & players["con"].notna()
                      & players["eye"].notna() & players["pwr"].notna() & players["spd"].notna()].reset_index(drop=True)
    if len(hitters) < 9:
        st.info(f"{full_name} has only {len(hitters)} hitters with attributes in season {season}; need at least nine.")
        return

    def _stored(key: str, sig: str):
        """A result saved by a tool button, or None (with a notice) when it was made for different inputs or by an
        older version of the engine."""
        res = st.session_state.get(key)
        if res and res[0] != sig:
            st.warning("Inputs changed since this was computed - run it again.")
            return None
        return res

    # ---- lineup editor
    ver_key = f"gl_ver_{season}_{abbrev}"
    df_key = f"gl_df_{season}_{abbrev}"
    if df_key not in st.session_state:
        with st.spinner("Estimating the usual lineup..."):
            ids = _default_lineup(int(season), abbrev, full_name, hitters)
        st.session_state[df_key] = _lineup_frame(hitters, ids)
    ver = st.session_state.get(ver_key, 0)

    st.subheader("Lineup")
    st.caption("Edit any attribute or batting slot directly. Attributes are the season's builds; changing one is a "
               "what-if, not a change to the player.")
    edited = st.data_editor(
        st.session_state[df_key], key=f"gl_ed_{season}_{abbrev}_{ver}", hide_index=True, use_container_width=True,
        disabled=["Player"], column_order=["Slot", "Player", "Hand", "CON", "EYE", "PWR", "SPD"],
        column_config={
            "Slot": st.column_config.NumberColumn("Slot", min_value=1, max_value=9, step=1, width="small"),
            "Hand": st.column_config.SelectboxColumn("Hand", options=["R", "L", "S"], width="small"),
            **{s: st.column_config.NumberColumn(s, min_value=1, max_value=5, step=1, width="small") for s in _STATS},
        })
    sw1, sw2, sw3 = st.columns([2, 3, 1])
    swap_slot = sw1.selectbox("Replace slot", list(range(1, 10)), key="gl_swap_slot")
    cur_names = set(edited["Player"])
    options = hitters[~hitters["name"].isin(cur_names)]
    swap_name = sw2.selectbox("with", ["-"] + list(options["name"]), key="gl_swap_name")
    if sw3.button("Swap in", use_container_width=True) and swap_name != "-":
        r = options[options["name"] == swap_name].iloc[0]
        new = edited.copy()
        i = new.index[new["Slot"] == swap_slot][0]
        new.loc[i, ["Player", "Hand", "CON", "EYE", "PWR", "SPD", "_pid"]] = [
            r["name"], r["hand"] if isinstance(r["hand"], str) else "R", int(r["con"]), int(r["eye"]),
            int(r["pwr"]), int(r["spd"]), int(r["player_id"])]
        st.session_state[df_key] = new
        st.session_state[ver_key] = ver + 1
        st.rerun()
    if st.button("Reset lineup"):
        del st.session_state[df_key]
        st.rerun()

    if sorted(edited["Slot"].tolist()) != list(range(1, 10)):
        st.error("Batting slots must be 1 to 9 with no repeats.")
        return
    if edited[list(_STATS)].isna().any().any():
        st.error("Fill in every attribute (1 to 5).")
        return
    edited = edited.sort_values("Slot", kind="stable").reset_index(drop=True)
    lineup = [o.Hitter(str(r.Player), r.Hand, int(r.CON), int(r.EYE), int(r.PWR), int(r.SPD)) for r in edited.itertuples()]
    odd = [h.name for h in lineup if h.con + h.eye + h.pwr + h.spd != 12]
    if odd:
        st.caption("Not a 12-point build: " + ", ".join(odd))

    # ---- opponent and options
    st.subheader("Opposing pitching")
    mode = st.radio("Opponent", ["League pitchers", "One pitcher", "Custom pitcher"], horizontal=True,
                    label_visibility="collapsed", key="gl_opp_mode")
    pitch_season = int(season)
    if mode == "League pitchers":
        top = st.slider("Pitcher builds in the mix (most-faced first)", 5, 49, 20, key="gl_top",
                        help="Every plate appearance faces a pitcher drawn from the season's builds, weighted by "
                             "batters faced. More builds is slower and slightly more faithful.")
        spec = ("mix", pitch_season, int(top))
    elif mode == "One pitcher":
        pb = _pitcher_builds(players, pitch_season)
        names = players[players["player_id"].isin(pb)].drop_duplicates("player_id").set_index("player_id")["name"]
        pid = st.selectbox("Pitcher", list(names.index), format_func=lambda i: names[i], key="gl_pitcher")
        spec = ("pitcher", pitch_season, int(pid))
    else:
        p1, p2, p3, p4, p5 = st.columns(5)
        hand = p1.selectbox("Hand", ["R", "L"], key="gl_c_hand")
        spec = ("custom", hand, p2.number_input("MOV", 1, 5, 3, key="gl_c_mov"), p3.number_input("CMD", 1, 5, 3, key="gl_c_cmd"),
                p4.number_input("VEL", 1, 5, 3, key="gl_c_vel"), p5.number_input("AWR", 1, 5, 3, key="gl_c_awr"))
        spec = (spec[0], spec[1], int(spec[2]), int(spec[3]), int(spec[4]), int(spec[5]))
    st.subheader("Opposing catcher")
    st.caption("Catchers only matter for steals: the safe range uses (pitcher AWR + catcher EYE) / 2.")
    cmode = st.radio("Catcher", ["League catchers", "One catcher", "Custom catcher"], horizontal=True,
                     label_visibility="collapsed", key="gl_cat_mode")
    ccounts = db.get_catcher_faced_counts(int(season))
    eye_of = players[(players["season"] == season) & players["eye"].notna()].drop_duplicates("player_id")         .set_index("player_id")
    if cmode == "League catchers":
        eyes: dict = {}
        for cid, n in ccounts.items():
            if cid in eye_of.index:
                e = int(eye_of.loc[cid, "eye"])
                eyes[e] = eyes.get(e, 0) + n
        if not eyes:
            eyes = {5: 1}
        tot = sum(eyes.values())
        catcher_key = tuple(sorted(eyes.items()))
        st.caption("Catcher EYE mix (by plate appearances caught): "
                   + ", ".join(f"{e}: {w / tot:.0%}" for e, w in sorted(eyes.items(), reverse=True)))
    elif cmode == "One catcher":
        cids = [c for c in sorted(ccounts, key=lambda c: -ccounts[c]) if c in eye_of.index]
        if cids:
            cid = st.selectbox("Catcher", cids, key="gl_catcher",
                               format_func=lambda c: f'{eye_of.loc[c, "name"]} (EYE {int(eye_of.loc[c, "eye"])})')
            catcher_key = ((int(eye_of.loc[cid, "eye"]), 1),)
        else:
            st.info("No catchers found for that season; using EYE 5.")
            catcher_key = ((5, 1),)
    else:
        catcher_key = ((int(st.number_input("Catcher EYE", 1, 5, 5, key="gl_c_eye")), 1),)

    with st.expander("Model options"):
        m1, m2 = st.columns(2)
        innings = m1.number_input("Innings", 1, 9, 6, key="gl_inn")
        basis = m2.radio("Steal and hit-and-run calls", ["Best for this lineup", "League run-expectancy table",
                                                          "Win probability"], key="gl_basis",
                         help="Only the offense is simulated, so the default makes every call that maximises "
                              "expected runs for this exact lineup (using the model's own run values). The table "
                              "option values each choice with the league run-expectancy table instead, and win "
                              "probability uses the win-probability table for a game with the lead below.")
        wp_only = basis != "Win probability"
        lead = st.number_input("Lead entering each inning (win probability only)", -10, 10, 0, key="gl_lead",
                               disabled=wp_only)
        steals = st.checkbox("Allow steals", True, key="gl_steals")
        hnr = st.checkbox("Allow hit and run", True, key="gl_hnr",
                          help="Called with a runner on 1st and/or 2nd (not 3rd alone or bases loaded) and fewer "
                               "than 2 outs, when it beats a normal swing.")
        bottom = st.checkbox("Batting in the bottom half (win probability only)", False, key="gl_bottom",
                             disabled=wp_only)
    decision = {"Best for this lineup": "opt", "League run-expectancy table": "re", "Win probability": "wp"}[basis]
    model = _model(int(innings), catcher_key, bool(steals), bool(hnr), decision, int(lead), bool(bottom),
                   o.ENGINE_VERSION)
    with st.spinner("Loading pitchers..."):
        opp = _opponent(spec)

    # ---- the answer
    with st.spinner("Solving the lineup..."):
        runs, per_inn = model.expected_runs(lineup, opp, detail=True)
        ref = model.expected_runs([o.Hitter(f"avg{i}", "R", 3, 3, 3, 3) for i in range(9)], opp)
    st.subheader("Production")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Runs per {int(innings)} innings", f"{runs:.3f}")
    k2.metric("vs all 3/3/3/3", f"{runs - ref:+.3f}")
    k3.metric("Per inning", f"{runs / int(innings):.3f}")
    k4.metric(f"Per {_GAMES_PER_SEASON}-game season", f"{runs * _GAMES_PER_SEASON:.1f}")
    fig = go.Figure(go.Bar(x=[f"{i}" for i in range(1, len(per_inn) + 1)], y=per_inn, marker_color="#4c78a8",
                           hovertemplate="Inning %{x}: %{y:.3f} runs<extra></extra>"))
    fig.update_layout(template=_TEMPLATE, height=220, margin=dict(l=10, r=10, t=10, b=30),
                      xaxis_title="Inning", yaxis_title="Expected runs", showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

    sig_base = _sig(season, abbrev, [(h.name, h.hand, h.con, h.eye, h.pwr, h.spd) for h in lineup], spec,
                    (innings, catcher_key, steals, hnr, decision, lead, bottom, o.ENGINE_VERSION))
    t_credit, t_scan, t_order, t_nine, t_sim = st.tabs(["Player value", "Build scan", "Batting order", "Best nine",
                                                         "Simulate"])

    # ---- player value
    with t_credit:
        st.caption("Runs each hitter adds over a reference hitter, each measured by re-solving the lineup with that "
                   "one thing about that one hitter changed. Plate = his own CON/EYE/PWR/SPD at bat. Steals = his "
                   "speed in steal attempts. Baserunning = his speed as a runner in the calculator: the lead runner "
                   "of 1st/2nd drives well-hit singles and doubles, and ground-out advancement reads the runner on a "
                   "base or the average of the runners, so a runner only earns credit in the states where the "
                   "calculator actually looks at him.")
        c1, c2 = st.columns(2)
        ref_choice = c1.radio("Compare each hitter to", ["Average build (3/3/3/3)", "This lineup's average hitter"],
                              key="gl_ref",
                              help="Player value is a difference between two exact solves, so it needs a reference "
                                   "hitter to mean 'zero'. The Simulate tab's runs added need no reference.")
        ctx = c2.radio("Measured", ["In this lineup", "In a neutral lineup"], key="gl_ctx",
                       help="Neutral: each hitter alone among eight reference hitters, so the number reflects his "
                            "attributes only - not his batting slot or who hits around him.")
        per = st.radio("Show per", ["game", "100 plate appearances"], horizontal=True, key="gl_per")
        if ref_choice.startswith("Average"):
            ref = (3, 3, 3, 3)
        else:
            ref = tuple(int(round(sum(getattr(h, a_) for h in lineup) / len(lineup))) for a_ in ("con", "eye", "pwr", "spd"))
        st.caption("Reference hitter: " + "/".join(map(str, ref)))
        cred_sig = sig_base + _sig(ref, ctx)
        if st.button("Compute player value", key="gl_btn_credit"):
            prog = _Progress("Valuing each hitter")
            fn = hl.credits_neutral if ctx.startswith("In a neutral") else hl.credits
            st.session_state["gl_credit"] = (cred_sig, pd.DataFrame(fn(model, lineup, opp, ref, progress=prog.update)))
            prog.done()
        res = _stored("gl_credit", cred_sig)
        if res:
            raw = res[1]
            scale = (100.0 / raw["pa"]) if per.startswith("100") else 1.0
            d = raw.copy()
            vcols = ["total", "plate_all", "plate_spd", "steals", "baserunning"]
            for c in vcols:
                d[c] = raw[c] * scale
            d = d.rename(columns={"name": "Player", "slot": "Slot", "build": "Build", "total": "Total",
                                  "plate_all": "Plate", "plate_spd": "of which SPD", "steals": "Steals",
                                  "baserunning": "Baserunning", "pa": "PA / game"})
            d["Per season"] = raw["total"] * _GAMES_PER_SEASON
            cols = ["Slot", "Player", "Build", "PA / game", "Total", "Plate", "of which SPD", "Steals",
                    "Baserunning", "Per season"]
            fmt = {c: "{:+.3f}" for c in ["Total", "Plate", "of which SPD", "Steals", "Baserunning"]}
            fmt.update({"PA / game": "{:.2f}", "Per season": "{:+.1f}"})
            st.dataframe(d[cols].style.format(fmt), hide_index=True, use_container_width=True)
            unit = "per 100 plate appearances" if per.startswith("100") else "per game"
            st.caption(f"Value columns are runs {unit}. 'Per season' is the Total over {_GAMES_PER_SEASON} games at "
                       "his plate appearances in the lineup used. Credits are marginal, so they don't have to add "
                       "up exactly: when two runners are on, the calculator reads only one of them (or their "
                       "average), and a faster teammate can make a runner's own speed matter less.")

    # ---- build scan
    with t_scan:
        st.caption("Try every legal 12-point build (each stat 1-5) for one hitter with everyone else fixed.")
        who = st.selectbox("Hitter", [h.name for h in lineup], key="gl_scan_who")
        if st.button("Scan builds", key="gl_btn_scan"):
            idx = [h.name for h in lineup].index(who)
            prog = _Progress("Scanning builds")
            rows = hl.scan_player(model, lineup, idx, opp, progress=prog.update)
            prog.done()
            st.session_state["gl_scan"] = (sig_base, who, rows)
        res = _stored("gl_scan", sig_base)
        if res:
            h0 = next((h for h in lineup if h.name == res[1]), None)
            cur = (h0.con, h0.eye, h0.pwr, h0.spd) if h0 else None
            rows = res[2]
            cur_val = dict(rows).get(cur)
            table = pd.DataFrame([{"Build (CON/EYE/PWR/SPD)": "/".join(map(str, b)), "Runs": v,
                                   "vs current": (v - cur_val) if cur_val is not None else None,
                                   "Per season": ((v - cur_val) * _GAMES_PER_SEASON) if cur_val is not None else None,
                                   "Current": "yes" if b == cur else ""} for b, v in rows])
            st.write(f"**{res[1]}**: best builds (current is {'/'.join(map(str, cur)) if cur else '-'}, "
                     f"{cur_val:.3f} runs)" if cur_val is not None else f"**{res[1]}**")
            show = pd.concat([table.head(10), table[table["Current"] == "yes"], table.tail(3)]).drop_duplicates()
            st.dataframe(show.style.format({"Runs": "{:.3f}", "vs current": "{:+.3f}", "Per season": "{:+.1f}"}),
                         hide_index=True, use_container_width=True)
            levels = pd.DataFrame([(*b, v) for b, v in rows], columns=list(_STATS) + ["runs"])
            overall = levels["runs"].mean()
            fig = go.Figure()
            for s in _STATS:
                g = levels.groupby(s)["runs"].mean() - overall
                fig.add_trace(go.Scatter(x=g.index, y=g.values, mode="lines+markers", name=s,
                                         line=dict(color=_STAT_COLORS[s], width=2),
                                         hovertemplate=f"{s} %{{x}}: %{{y:+.3f}} runs<extra></extra>"))
            fig.update_layout(template=_TEMPLATE, height=260, margin=dict(l=10, r=10, t=30, b=30),
                              title=dict(text="Average team runs by stat level (points taken from the other stats)",
                                         font=dict(size=13)),
                              xaxis_title="Stat level", yaxis_title="Runs vs average build",
                              legend=dict(orientation="h", y=-0.3))
            st.plotly_chart(fig, use_container_width=True)

    # ---- batting order
    with t_order:
        st.caption("Searches for the best order of these nine hitters (swapping pairs until nothing improves).")
        if st.button("Optimize batting order", key="gl_btn_order"):
            prog = _Progress("Trying batting orders")
            order, best, worst, tried = hl.hill_climb_order(
                model, lineup, opp, restarts=3,
                progress=lambda n: prog.update(min(n / 300.0, 0.99), f"Trying batting orders ({n} tried)"))
            prog.done()
            st.session_state["gl_order"] = (sig_base, order, best, worst, tried)
        res = _stored("gl_order", sig_base)
        if res:
            _, order, best, worst, tried = res
            c1, c2, c3 = st.columns(3)
            c1.metric("Best order", f"{best:.3f}", f"{best - runs:+.3f} vs yours")
            c2.metric("Worst order seen", f"{worst:.3f}")
            c3.metric("Orders tried", f"{tried}")
            st.dataframe(pd.DataFrame({"Slot": range(1, 10), "Player": [lineup[i].name for i in order],
                                       "Build": [f"{lineup[i].con}/{lineup[i].eye}/{lineup[i].pwr}/{lineup[i].spd}" for i in order]}),
                         hide_index=True, use_container_width=True)

    # ---- best nine
    with t_nine:
        pool = [{"hitter": o.Hitter(r["name"], r["hand"] if isinstance(r["hand"], str) else "R", int(r["con"]),
                                    int(r["eye"]), int(r["pwr"]), int(r["spd"])),
                 "prim": r["primary_pos"] or "", "sec": r["secondary_pos"]} for _, r in hitters.iterrows()]
        st.caption(f"Picks which nine of {len(pool)} {abbrev} hitters to play, and their order, using each player's "
                   "season build.")
        legal = st.checkbox("Require a legal defense (C, 1B, 2B, 3B, SS, LF, CF, RF plus a DH, 5+ at their primary "
                            "position)", True, key="gl_legal")
        if st.button("Find the best nine", key="gl_btn_nine"):
            prog = _Progress("Scoring every nine")
            out, note, n_sets = hl.best_nine(model, pool, opp, require_legal=legal, progress=prog.update)
            prog.done()
            st.session_state["gl_nine"] = (sig_base, out, note, n_sets, legal)
        res = _stored("gl_nine", sig_base)
        if res:
            _, out, note, n_sets, _ = res
            if note:
                st.caption(note)
            st.caption(f"{n_sets} candidate nines scored.")
            if not out:
                st.info("No legal nine found for this roster.")
            for rank, (val, idxs) in enumerate(out, start=1):
                names = [pool[i]["hitter"].name for i in idxs]
                st.write(f"**#{rank}: {val:.3f} runs**  ({val - runs:+.3f} vs the lineup above)")
                st.write(" > ".join(names))

    # ---- simulate
    with t_sim:
        st.caption("Plays the lineup through thousands of games event by event, so you can see who steals, when "
                   "the hit and run is called, who scores and drives in runs, and how much runs vary from game to "
                   "game. The average matches the exact figure above.")
        s1, s2, s3 = st.columns(3)
        n_games = s1.number_input("Games", 1000, 200000, 20000, step=1000, key="gl_sim_n")
        seed = s2.number_input("Random seed", 0, 10**6, 1, key="gl_sim_seed")
        per_game = s3.checkbox("One pitcher per game", False, key="gl_sim_ppg",
                               help="Off: every plate appearance faces a fresh pitcher drawn from the mix. On: the "
                                    "same arm all game.")
        if st.button("Run simulation", key="gl_btn_sim"):
            prog = _Progress(f"Playing {int(n_games):,} games")
            sim = model.simulate_detailed(lineup, opp, games=int(n_games), seed=int(seed), pitcher_per_game=per_game,
                                          progress=prog.update)
            prog.done()
            st.session_state["gl_sim"] = (sig_base, sim)
        res = _stored("gl_sim", sig_base)
        if res:
            sim = res[1]
            k1, k2, k3 = st.columns(3)
            k1.metric("Simulated runs per game", f"{sim['mean']:.3f}", f"+/- {sim['se']:.3f}", delta_color="off")
            k2.metric("Exact figure", f"{runs:.3f}")
            k3.metric("Std. deviation of runs", f"{sim['runs'].std():.2f}")
            dist = pd.Series(sim["runs"]).value_counts(normalize=True).sort_index()
            figd = go.Figure(go.Bar(x=[int(v) for v in dist.index], y=dist.values, marker_color="#4c78a8",
                                    hovertemplate="%{x} runs: %{y:.1%}<extra></extra>"))
            figd.update_layout(template=_TEMPLATE, height=220, margin=dict(l=10, r=10, t=10, b=30),
                               xaxis_title="Runs in a game", yaxis_title="Share of games", yaxis_tickformat=".0%")
            st.plotly_chart(figd, use_container_width=True)
            rows = []
            for i, t in enumerate(sim["per_game"]):
                rows.append({"Slot": i + 1, "Player": sim["names"][i], "PA": t["pa"], "H&R calls": t["hnr"],
                             "Steal att.": t["att"] + t["ks_att"], "SB": t["sb"] + t["ks_sb"],
                             "CS": t["cs"] + t["ks_cs"], "Hitting": t["hit_re"], "Baserunning": t["base_credit"],
                             "Steals": t["steal_net"] + t["ks_net"], "Runs added": t["re_total"],
                             "Runs scored": t["runs"], "RBI": t["rbi"]})
            tbl = pd.DataFrame(rows)
            st.dataframe(tbl.style.format({c: "{:+.3f}" for c in ("Hitting", "Baserunning", "Steals", "Runs added")}
                                          | {c: "{:.3f}" for c in ("PA", "H&R calls", "Steal att.", "SB", "CS",
                                                                   "Runs scored", "RBI")}),
                         hide_index=True, use_container_width=True)
            team = tbl["Runs added"].sum()
            st.caption(
                f"Per game. Runs added is the change in run expectancy on every play plus the runs that scored "
                f"(RE24), split three ways: Hitting is the batter's share of each swing; Baserunning is the runs a "
                f"runner's speed added on swings, found by replaying the same diff with that runner at speed 3 and "
                f"moving the difference from the batter to the runner (a runner who widens the well-hit ranges "
                f"gains, one who narrows them loses, one who changes nothing gets 0, and the batter keeps the "
                f"rest); Steals is the run value of each steal attempt, including the forced attempt after a "
                f"strikeout on a hit and run. The nine add up to {team:+.3f} = the team's {sim['mean']:.3f} runs "
                f"minus the {sim['re_start']:.3f} an average inning start is worth. Runs scored and RBI are "
                f"plain counts.")
            with st.expander("Play-by-play of the first simulated game"):
                st.text("\n".join(sim["log"]))

        st.markdown("---")
        st.markdown("**Lineup-independent version**")
        st.caption("Plays each hitter on his own, batting fifth in a lineup of eight reference hitters, one hitter "
                   "at a time. Nothing about batting order or who bats around him leaks in, so the runs added "
                   "reflect his attributes only. Shown per 100 plate appearances.")
        n_games_n = st.number_input("Games per hitter", 2000, 100000, 10000, step=1000, key="gl_simn_n")
        ref_sim = (3, 3, 3, 3)
        if st.button("Run lineup-independent simulation", key="gl_btn_simn"):
            prog = _Progress("Simulating each hitter")
            rows_n = hl.simulate_neutral(model, lineup, opp, games=int(n_games_n), seed=int(seed), ref=ref_sim,
                                         pitcher_per_game=per_game, progress=prog.update)
            prog.done()
            st.session_state["gl_simn"] = (sig_base, rows_n)
        res_n = _stored("gl_simn", sig_base)
        if res_n:
            out = []
            for t in res_n[1]:
                k = 100.0 / t["pa"]
                out.append({"Slot": t["slot"], "Player": t["name"], "Build": t["build"],
                            "H&R calls": t["hnr"] * k, "Steal att.": (t["att"] + t["ks_att"]) * k,
                            "Hitting": t["hit_re"] * k, "Baserunning": t["base_credit"] * k,
                            "Steals": (t["steal_net"] + t["ks_net"]) * k, "Runs added": t["re_total"] * k})
            tn = pd.DataFrame(out)
            st.dataframe(tn.style.format({c: "{:+.2f}" for c in ("Hitting", "Baserunning", "Steals", "Runs added")}
                                         | {c: "{:.2f}" for c in ("H&R calls", "Steal att.")}),
                         hide_index=True, use_container_width=True)
            st.caption("Runs per 100 plate appearances against the average state of the game (RE24), so 0 is a "
                       "league-average plate appearance. Each hitter's number has simulation noise of roughly "
                       "+/-0.2 at 10,000 games; raise the games for a tighter read, or use the exact version in "
                       "Player value for noise-free numbers.")

    with st.expander("How this works and what it leaves out"):
        st.markdown(
            "- Each plate appearance uses the stadium sheet's exact result ranges for the batter, the pitcher, the "
            "outs and the runners' speeds. The swing-vs-pitch gap is treated as random, so batters get no credit "
            "for guessing well.\n"
            "- Runners move according to the baserunning chart; a faster runner makes his teammates' singles and "
            "doubles come out well-hit more often.\n"
            "- Steals and hit-and-runs are called whenever they raise the lineup's expected runs (found by solving "
            "for the best call in every situation, so fast runners are never penalised for being allowed to "
            "attempt more). With runners on 1st and 2nd a steal is a double steal decided by the runner on 2nd.\n"
            "- A hit and run (batter CON -1, runners SPD +1) is called the same way. A strikeout then triggers a "
            "steal attempt at the runners' normal speed.\n"
            "- Not modeled: bunts, infield in, intentional walks, fielding, extra innings, and the other team's "
            "offense. Pitchers who bat (0/0/0/0) are not supported.\n"
            "- Attributes are constant through a season (the players table).")


st.title("GM Tools")
(_tab_hit,) = st.tabs(["Hitting Lab"])
with _tab_hit:
    hitting_lab()

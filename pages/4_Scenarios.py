"""Scenario sandbox: compute at-bat ranges from a historical play or manual setup."""
import streamlit as st
import pandas as pd
import database as db
import utils

st.title("Scenarios")
st.caption("Compute result ranges for any pitcher/batter matchup using the MLN Calculator formula.")

# ------------------------------------------------------------------ helpers

@st.cache_data(ttl=300)
def _load_players():
    players = db.get_all_players()
    pitchers = [p for p in players if p.get("mov") is not None or p.get("cmd") is not None]
    batters  = [p for p in players if p.get("con") is not None]
    return players, pitchers, batters

@st.cache_data(ttl=60)
def _load_plays():
    raw = db.get_all_plays()
    if not raw:
        return pd.DataFrame()
    df = utils.flatten_games(raw)
    return df

def _player_label(p):
    return p.get("name", "?")

def _stat(p, key, default=3):
    v = p.get(key)
    return int(v) if v is not None else default

def _hand(p):
    h = str(p.get("hand", "R")).upper()
    return h if h in ("L", "R", "S") else "R"

def _show_ranges(ranges, swap_title="Normal Swing"):
    if not ranges:
        st.warning("Could not compute ranges.")
        return
    df_r = pd.DataFrame(ranges).rename(columns={"result": "Result", "range": "Range", "low": "Low", "high": "High"})
    st.plotly_chart(utils.range_bar_chart(ranges, title=swap_title), width='stretch')
    st.dataframe(df_r, width='stretch', hide_index=True)


# ------------------------------------------------------------------ source selector

tab_hist, tab_manual = st.tabs(["Historical Play", "Manual Setup"])

# ------------------------------------------------------------------ HISTORICAL TAB

with tab_hist:
    df_plays = _load_plays()
    if df_plays.empty:
        st.info("No plays in the database yet.")
    else:
        _, pitchers, batters = _load_players()
        all_players = db.get_all_players()
        player_by_name = {p["name"]: p for p in all_players if p.get("name")}

        col_s, col_g, col_p = st.columns([1, 2, 3])
        with col_s:
            seasons = sorted(df_plays["season"].dropna().unique(), reverse=True)
            sel_season = st.selectbox("Season", seasons, key="sc_season")
        df_s = df_plays[df_plays["season"] == sel_season]
        with col_g:
            games = sorted(df_s["game_id"].dropna().unique())
            sel_game = st.selectbox("Game", games, format_func=lambda x: f"Game {int(x)}", key="sc_game")
        df_g = df_s[df_s["game_id"] == sel_game].sort_values("id")
        with col_p:
            play_options = df_g[["id","inning","half","outs","obc","pitcher_name","batter_name","pitch","swing","result"]].copy()
            play_options["label"] = play_options.apply(
                lambda r: (
                    f"#{int(r['id'])}  Inn {int(r['inning'])}{r['half'][0].upper()}  "
                    f"{r['outs']}out  {r['obc']}  "
                    f"{r['pitcher_name']} vs {r['batter_name']}  "
                    f"P:{r['pitch']} S:{r['swing']} → {r['result']}"
                ), axis=1
            )
            play_ids = play_options["id"].tolist()
            sel_play_id = st.selectbox(
                "Play",
                play_ids,
                format_func=lambda x: play_options.loc[play_options["id"] == x, "label"].iloc[0],
                key="sc_play",
            )

        play_row = df_g[df_g["id"] == sel_play_id].iloc[0]
        pitcher_name = play_row.get("pitcher_name", "")
        batter_name  = play_row.get("batter_name",  "")
        pitcher_p = player_by_name.get(pitcher_name, {})
        batter_p  = player_by_name.get(batter_name,  {})

        st.divider()
        col_pi, col_bi = st.columns(2)
        with col_pi:
            st.markdown(f"**Pitcher:** {pitcher_name}")
            st.caption(
                f"Hand: {_hand(pitcher_p)} | "
                f"MOV: {_stat(pitcher_p,'mov')} | CMD: {_stat(pitcher_p,'cmd')} | "
                f"VEL: {_stat(pitcher_p,'vel')} | AWR: {_stat(pitcher_p,'awr')}"
            )
        with col_bi:
            st.markdown(f"**Batter:** {batter_name}")
            st.caption(
                f"Hand: {_hand(batter_p)} | "
                f"CON: {_stat(batter_p,'con')} | EYE: {_stat(batter_p,'eye')} | "
                f"POW: {_stat(batter_p,'pow')} | SPD: {_stat(batter_p,'spd')}"
            )

        outs_val = int(play_row.get("outs", 0)) if pd.notna(play_row.get("outs")) else 0
        obc = str(play_row.get("obc", "Empty"))
        runners_on = obc != "Empty"

        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            h_bunt  = st.checkbox("Bunting?", key="sc_h_bunt")
        with col_f2:
            h_hnr   = st.checkbox("Hit & Run?", key="sc_h_hnr")
        with col_f3:
            st.caption(f"Outs: {outs_val} | Runners: {obc}")

        if pitcher_p and batter_p:
            ranges = utils.compute_at_bat_ranges(
                pitcher_hand=_hand(pitcher_p),
                pitcher_mov=_stat(pitcher_p, "mov"),
                pitcher_cmd=_stat(pitcher_p, "cmd"),
                pitcher_vel=_stat(pitcher_p, "vel"),
                pitcher_awr=_stat(pitcher_p, "awr"),
                batter_hand=_hand(batter_p),
                batter_con=_stat(batter_p, "con"),
                batter_eye=_stat(batter_p, "eye"),
                batter_pow=_stat(batter_p, "pow"),
                batter_spd=_stat(batter_p, "spd"),
                bunt=h_bunt,
                hit_and_run=h_hnr,
                outs=outs_val,
                runners_on=runners_on,
            )

            if pd.notna(play_row.get("pitch")) and pd.notna(play_row.get("swing")):
                actual_pitch = int(play_row["pitch"])
                actual_swing = int(play_row["swing"])
                actual_diff  = utils.circular_diff(actual_pitch, actual_swing)
                actual_result = play_row.get("result", "")
                st.caption(
                    f"Actual play: Pitch {actual_pitch} | Swing {actual_swing} | "
                    f"Diff {actual_diff} | Result **{actual_result}**"
                )
                # Show what range the actual diff fell in
                match = next((r for r in ranges if r["low"] <= actual_diff <= r["high"]), None)
                if match:
                    expected = match["result"]
                    icon = "✓" if expected == actual_result else "≠"
                    st.caption(f"Diff {actual_diff} falls in **{expected}** range {match['low']}–{match['high']} {icon}")

            label = "Bunt Ranges" if h_bunt else "Normal Swing Ranges"
            _show_ranges(ranges, label)
        else:
            st.info("Player stats not found in DB for one or both players. Sync Players first.")

# ------------------------------------------------------------------ MANUAL TAB

with tab_manual:
    _, pitchers, batters = _load_players()
    all_players_m = db.get_all_players()
    player_by_name_m = {p["name"]: p for p in all_players_m if p.get("name")}

    col_mp, col_mb = st.columns(2)

    with col_mp:
        st.subheader("Pitcher")
        pitcher_names = ["-- Manual --"] + sorted(p["name"] for p in pitchers if p.get("name"))
        sel_pitcher = st.selectbox("Select pitcher", pitcher_names, key="sc_m_pitcher")
        if sel_pitcher == "-- Manual --":
            m_p_hand = st.selectbox("Hand", ["R", "L", "S"], key="sc_m_phand")
            m_p_mov  = st.number_input("MOV", 1, 5, 3, key="sc_m_mov")
            m_p_cmd  = st.number_input("CMD", 1, 5, 3, key="sc_m_cmd")
            m_p_vel  = st.number_input("VEL", 1, 5, 3, key="sc_m_vel")
            m_p_awr  = st.number_input("AWR", 1, 5, 3, key="sc_m_awr")
        else:
            pp = player_by_name_m[sel_pitcher]
            m_p_hand = _hand(pp)
            m_p_mov  = _stat(pp, "mov")
            m_p_cmd  = _stat(pp, "cmd")
            m_p_vel  = _stat(pp, "vel")
            m_p_awr  = _stat(pp, "awr")
            st.caption(f"Hand: {m_p_hand} | MOV: {m_p_mov} | CMD: {m_p_cmd} | VEL: {m_p_vel} | AWR: {m_p_awr}")

    with col_mb:
        st.subheader("Batter")
        batter_names = ["-- Manual --"] + sorted(p["name"] for p in batters if p.get("name"))
        sel_batter = st.selectbox("Select batter", batter_names, key="sc_m_batter")
        if sel_batter == "-- Manual --":
            m_b_hand = st.selectbox("Hand", ["R", "L", "S"], key="sc_m_bhand")
            m_b_con  = st.number_input("CON", 1, 5, 3, key="sc_m_con")
            m_b_eye  = st.number_input("EYE", 1, 5, 3, key="sc_m_eye")
            m_b_pow  = st.number_input("POW", 1, 5, 3, key="sc_m_pow")
            m_b_spd  = st.number_input("SPD", 1, 5, 3, key="sc_m_spd")
        else:
            bp = player_by_name_m[sel_batter]
            m_b_hand = _hand(bp)
            m_b_con  = _stat(bp, "con")
            m_b_eye  = _stat(bp, "eye")
            m_b_pow  = _stat(bp, "pow")
            m_b_spd  = _stat(bp, "spd")
            st.caption(f"Hand: {m_b_hand} | CON: {m_b_con} | EYE: {m_b_eye} | POW: {m_b_pow} | SPD: {m_b_spd}")

    st.divider()
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    with col_s1:
        m_outs = st.radio("Outs", [0, 1, 2], horizontal=True, key="sc_m_outs")
    with col_s2:
        m_runners = st.checkbox("Runners on?", key="sc_m_runners")
    with col_s3:
        m_bunt = st.checkbox("Bunting?", key="sc_m_bunt")
    with col_s4:
        m_hnr  = st.checkbox("Hit & Run?", key="sc_m_hnr")

    ranges_m = utils.compute_at_bat_ranges(
        pitcher_hand=m_p_hand,
        pitcher_mov=int(m_p_mov),
        pitcher_cmd=int(m_p_cmd),
        pitcher_vel=int(m_p_vel),
        pitcher_awr=int(m_p_awr),
        batter_hand=m_b_hand,
        batter_con=int(m_b_con),
        batter_eye=int(m_b_eye),
        batter_pow=int(m_b_pow),
        batter_spd=int(m_b_spd),
        bunt=m_bunt,
        hit_and_run=m_hnr,
        outs=m_outs,
        runners_on=m_runners,
    )

    label_m = "Bunt Ranges" if m_bunt else "Normal Swing Ranges"
    _show_ranges(ranges_m, label_m)

    if not m_bunt and not m_hnr:
        st.divider()
        st.subheader("Bunt Comparison")
        st.caption("How ranges shift if the batter bunts instead.")
        ranges_bunt = utils.compute_at_bat_ranges(
            pitcher_hand=m_p_hand, pitcher_mov=int(m_p_mov), pitcher_cmd=int(m_p_cmd),
            pitcher_vel=int(m_p_vel), pitcher_awr=int(m_p_awr),
            batter_hand=m_b_hand, batter_con=int(m_b_con), batter_eye=int(m_b_eye),
            batter_pow=int(m_b_pow), batter_spd=int(m_b_spd),
            bunt=True, outs=m_outs, runners_on=m_runners,
        )
        _show_ranges(ranges_bunt, "Bunt Ranges")

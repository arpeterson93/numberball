"""The combined pitch-likelihood model's recommended swing, for PioBall.

Same model the scouting app's "Combined Pitch Likelihood" and the bot's
swing-score chart use (bot_charts._pitch_likelihood_prediction), but it only
runs once the inputs are provably current: the model reads the pitcher's
previous pitches/swings, and the inning/half the sheet reports is derived from
the last synced play, so a lagging plays sync gives a wrong answer rather than
a slightly stale one. No Discord imports here."""
from __future__ import annotations

import time
from datetime import datetime

import numpy as np

import bot_charts
import database as db
import scouting_data
import swing_game
import utils

METRICS = ("obp", "slg", "rv")
_FEED_MAX_AGE = 2 * 3600  # a feed result older than this belongs to some other game
_RECENT_PLAYS = 12


def _t(name: str):
    return swing_game._t(name)


def sync_status(game_code, last_result: dict | None = None) -> dict:
    """Is the plays table caught up with everything we know has happened?

    Two checks. (1) every pitch we graded from the results feed has been matched
    to a synced plays row. (2) the newest result the bot saw in the feed
    (either team's - the sheet's inning/half comes from the last synced play)
    is among the newest synced plays. (2) is skipped, and "verified" is False,
    when the bot hasn't seen a result since it started."""
    code = str(game_code)
    swing_game._reconcile(code)  # claim feed-graded pitches that have since synced
    now = datetime.utcnow()
    pending = []
    for r in (_t("swing_pitches").select("seq,pitch,swing,graded_at").eq("game_code", code)
              .not_.is_("graded_at", "null").is_("play_num", "null").execute().data):
        try:  # one that never matched a plays row after 2 hours is a mismatch, not a lagging sync
            age = (now - datetime.fromisoformat(r["graded_at"]).replace(tzinfo=None)).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if r.get("pitch") is not None and age < _FEED_MAX_AGE:
            pending.append(r)
    plays = (_t("plays").select("play_num,pitch,swing,result,timestamp").eq("game_code", code)
             .order("play_num", desc=True).limit(_RECENT_PLAYS).execute().data)
    issues = []
    if pending:
        n = len(pending)
        issues.append(f"{n} graded pitch{'es' if n != 1 else ''} not in the plays table yet")
    # A result from another game (back-to-back sessions) can never appear in this game's plays.
    verified = (bool(last_result) and time.time() - last_result["at"] < _FEED_MAX_AGE
                and str(last_result.get("game_code")) == code)
    if verified:
        seen = any(p.get("pitch") == last_result["pitch"]
                   and (last_result.get("swing") is None or p.get("swing") == last_result["swing"])
                   for p in plays)
        if not seen:
            issues.append(f"the latest result in the feed (pitch {last_result['pitch']}) hasn't synced yet")
    return {"ok": not issues, "issues": issues, "verified": verified,
            "last_play": plays[0] if plays else None}


def _history(pitcher: str):
    """The pitcher's full history straight from Supabase. Deliberately not
    scouting_data.load_pitcher_plays, whose one-hour cache would hide the
    pitches thrown earlier in this very game."""
    pid = scouting_data.player_dir()["name_to_pid"].get(pitcher)
    raw = db.get_plays_for_pitcher_id(pid) if pid is not None else db.get_plays_for_pitcher(pitcher)
    return utils.enrich_df(utils.flatten_games(raw)) if raw else None


def _live_ctx(p: dict) -> dict:
    """live_* kwargs for the leverage signal, from the pitch row's snapshot. The
    snapshot stores our lead; the model wants home/away, and only their
    difference matters."""
    half = str(p.get("half") or "")
    lead = int(p.get("lead") or 0)
    ours, theirs = max(lead, 0), max(-lead, 0)  # we are the batting team
    home_bat = half.lower() == "bottom"
    return dict(live_inning=p.get("inning"), live_half=half or None, live_outs=p.get("outs"),
                live_obc=p.get("obc"), live_home_score=ours if home_bat else theirs,
                live_away_score=theirs if home_bat else ours)


def _scores(density, ranges: list, metric: str):
    if metric in ("obp", "slg"):
        return utils.expected_swing_scores(density, ranges, metric)
    kernel = np.array([swing_game.run_value(utils._diff_to_result(d, ranges)) or 0.0 for d in range(501)])
    return utils._scores_via_fft(np.asarray(density, dtype=float), kernel)


def recommend(game: dict, metric: str = "obp", last_result: dict | None = None, force: bool = False) -> dict:
    """The model's swing for the game's open pitch. Always returns a dict with
    "ok" and "reason" (when not ok). The recommendation is saved on the pitch
    row the first time it is computed from synced data, so the Model row in the
    results table is the swing that was actually on offer, not a later hindsight."""
    code = str(game["game_code"])
    p = swing_game.peek_open_pitch(game)
    if not p or not p.get("batter") or not p.get("pitcher"):
        return {"ok": False, "reason": "Nothing to recommend yet - our next plate appearance isn't up "
                                       "(or the first /guess hasn't opened it)."}
    if swing_game.is_closed(p):
        return {"ok": False, "reason": "That pitch is already in."}
    if p.get("model_swing") is not None and p.get("model_metric") == metric:
        return {"ok": True, "swing": int(p["model_swing"]), "metric": metric, "saved": True, "pitch_row": p}

    sync = sync_status(code, last_result)
    if not sync["ok"] and not force:
        return {"ok": False, "sync": sync,
                "reason": "Not recommending yet - the data isn't current: " + "; ".join(sync["issues"])
                          + ". The plays sync runs every 5 minutes; try again after the next one."}

    # Re-read the sheet now that the plays are synced, so the inning/half/score the
    # model sees reflect the plate appearances just before this one.
    swing_game.refresh_snapshot(code)
    p = swing_game.peek_open_pitch(game) or p
    df = _history(p["pitcher"])
    if df is None or df.empty:
        return {"ok": False, "reason": f"No plays on file for {p['pitcher']}."}
    pl = bot_charts._pitch_likelihood_prediction(df, **_live_ctx(p))
    if pl is None:
        return {"ok": False, "reason": f"Not enough history for {p['pitcher']} (needs at least 2 pitches)."}

    ranges = [tuple(r) for r in (p.get("ranges") or utils.RESULT_RANGES)]
    scores = _scores(pl["combined"], ranges, metric)
    swing = int(np.argmax(scores)) + 1
    base_swing = int(np.argmax(_scores(pl["baseline"], ranges, metric))) + 1
    conf = None
    if metric in ("obp", "slg"):
        try:
            conf = utils.swing_confidence_text(
                utils.swing_confidence_summary(pl["baseline"], pl["combined"], metric, result_ranges=ranges),
                metric.upper())
        except Exception:
            conf = None
    last = df.sort_values("id").iloc[-1]
    out = {"ok": True, "swing": swing, "score": float(scores[swing - 1]), "metric": metric,
           "baseline_swing": base_swing, "confidence": conf, "saved": False, "forced": not sync["ok"],
           "verified": sync["verified"], "n_pitches": int(len(df)), "pitch_row": p,
           "last_pitch": {"pitch": last.get("pitch"), "swing": last.get("swing"), "result": last.get("result")}}
    # Only freeze a recommendation that came from current data, and never overwrite one already
    # on offer (a /model peek at another metric is display-only).
    if sync["ok"] and p.get("model_swing") is None:
        try:
            _t("swing_pitches").update({"model_swing": swing, "model_metric": metric,
                                        "model_at": datetime.utcnow().isoformat()}).eq("id", p["id"]).execute()
            out["saved"] = True
        except Exception:  # migrate_swing_game.sql's model_* columns aren't applied yet
            pass
    return out


def format_recommendation(r: dict) -> str:
    if not r["ok"]:
        return r["reason"]
    p = r["pitch_row"]
    label = {"obp": "OBP", "slg": "SLG", "rv": "run value"}[r["metric"]]
    lines = [f"**Model swing: {r['swing']}**  ({p.get('batter')} vs {p.get('pitcher')}, "
             f"{p.get('half') or ''} {p.get('inning') or ''}, {p.get('outs')} out)"]
    if r.get("saved") and "score" not in r:
        lines.append(f"Saved earlier for this pitch (optimizing {label}).")
        return "\n".join(lines)
    lines.append(f"Optimizing {label}: expected {r['score']:.3f}. Without the pitch context the model would say "
                 f"{r['baseline_swing']}.")
    if r.get("confidence"):
        lines.append(r["confidence"])
    lp = r["last_pitch"]
    lines.append(f"Based on {r['n_pitches']} pitches; the latest it saw: {int(lp['pitch'])}"
                 + (f", swing {int(lp['swing'])}" if lp.get("swing") == lp.get("swing") and lp.get("swing") is not None else "")
                 + (f" ({lp['result']})" if lp.get("result") else "") + ".")
    if r.get("forced"):
        lines.append("**Warning: forced past the sync check - the plays table may be missing recent pitches.**")
    elif not r["verified"]:
        lines.append("Sync check: all graded pitches are in plays. The bot hasn't seen a feed result since it "
                     "started, so the very latest pitch is unconfirmed.")
    else:
        lines.append("Sync check passed: the latest feed result is in the plays table.")
    if not r["saved"] and not r.get("forced"):
        lines.append("(Not saved to the pitch - run migrate_swing_game.sql for the model_* columns.)")
    return "\n".join(lines)

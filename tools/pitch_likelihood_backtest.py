"""
Backtest harness for a next-pitch likelihood model built from stacked
historical-context signals (see conversation/design notes - no doc file yet,
this is the first testable scaffold).

The question this answers: for a pitcher's own history, if you freeze
everything before pitch N and predict pitch N from a handful of context
signals (pitch sequence, delta sequence, delta-after-result,
delta-after-leverage, shadow-delta-after-result, cooldown "due-ness", delta
cooldown "due-ness"), does the combined prediction actually beat just using
that pitcher's unconditioned historical pitch distribution? Measured via
log-loss (lower is better) and a probability-integral-transform (PIT)
histogram, which should come out roughly flat if the predicted densities are
honestly calibrated.

Each signal reduces to a density over the 1-1000 pitch wheel plus an
effective sample size (Signal below). Signals are shrunk toward the
unconditioned baseline individually (by their own n_eff, so a thin signal
can't dominate) and then combined as a log-linear pool (multiplied in
probability space) rather than averaged - the intent, per the design
discussion this came out of, is that each indicator should behave like an
independent piece of evidence: agreement across signals sharpens the
estimate, a strongly dissenting signal can pull it down, not just get
outvoted the way an averaged mixture would let it.

Seven signals so far. Add more via the SIGNAL_BUILDERS list once this set is
shown to actually beat the naive baseline; each addition should be checked
against the same backtest rather than assumed to help.

DEFAULT_WEIGHTS/the sigma/pitch-width/leverage-width CLI defaults below are
NOT guesses - they're tools/pitch_likelihood_calibrate.py's fit, on a roster
scanned live from Supabase (fetch --min-bf 100, i.e. >=100 "Swing" rows as
pitcher_id - currently 94/47/46 train/val/test pitchers) split 50/25/25 by
pitcher. Equal weights (the naive "every signal counts the same" default)
score WORSE than the unconditioned baseline on every split - several of
these signals share conditioning context or matched rows (pitch_sequence/
delta_sequence both derive from the same two prior pitches; delta_after_
result/shadow_delta_after_result AND delta_after_result/delta_after_leverage
both measurably correlated - see tools/pitch_likelihood_calibrate.py
signal-correlation, r=0.36 and r=0.34 respectively, the strongest pairs in
the matrix), so treating them as independent evidence double-counts. The
fitted weights correct for that and beat baseline on held-out test
(log-loss 6.9077 -> 6.9055, PIT chi2 81.0 -> 10.4).

Current fit basis - min_history raised from 30 to 100 (per-pitcher pitch-
index floor for an eligible backtest point, not the roster's own >=100 BF
floor above): points below 100 pitches into a career were judged more noise
than signal. That alone flipped delta_cooldown from the single largest
weight (0.762) to exactly 0.0, and shuffled several others meaningfully -
see this module's git history for the min-history=30 numbers if comparing.
Tried shrinking cooldown_signal/delta_cooldown_signal's own per-category
p_return_now toward a league-wide rate (COOLDOWN_LEAGUE_RATES/
DELTA_COOLDOWN_LEAGUE_RATES below, tools/pitch_likelihood_calibrate.py
fit-cooldown-table) on the theory that thin per-pitcher-per-category hazard
estimates were driving that flip - refit barely moved (delta_cooldown still
0.0, log-loss/chi2 within noise of the unshrunk version), so that wasn't the
explanation; the shrinkage is still correct to have (a real, previously-
unguarded small-sample problem) but isn't what's driving the min-history
sensitivity. Most likely cause not yet confirmed: the train population
itself changes shape under a higher min-history floor (several pitchers
dropped to single-digit eligible points once the first 100 of their own
history were excluded), not pure rate noise - redo this investigation if
pursuing it further, don't assume this comment's guess is right.

pitch_width itself moved from 200 to 100 in an earlier pass (git history) -
200 was picked to make the Hot Zone Pitch Matrix heatmap a clean 5x5 grid,
not for any modeling reason, and covered 20% of the 1-1000 wheel per side;
100 keeps the grid clean (10x10) at a tighter match window, matched
everywhere pitch/delta/delta2 buckets appear in the UI.

Sigma/cooldown_radius/delta_cooldown_radius/leverage_width were NOT re-grid-
searched in any of the refits above, only pitch_width/min_history/the
cooldown-rate shrinkage changed by hand and weights were refit around each -
run fit-widths (tools/pitch_likelihood_calibrate.py fit-widths) if squeezing
out more gain from retuning those against the current combo is worth the
cost. A per-pitcher archetype/clustering check and a within-pitcher
bootstrap variance decomposition both found no pitcher-level structure worth
exploiting beyond this single global fit (an older pass, pre-dating the
width/min-history/cooldown-shrinkage changes above) - see the calibration
script's own docstring before re-splitting pitchers into archetypes or
building partial pooling; redo this fit (not by hand) if SIGNAL_BUILDERS
changes again.

Run from the repo root:
    python tools/pitch_likelihood_backtest.py [--pitcher "Name"] [--min-history 100]
        [--sigma 75] [--min-effective-n 20] [--pitch-width 100] [--cooldown-radius 50]
        [--delta-cooldown-radius 50] [--leverage-width 0.5]

Omitting --pitcher scans every player's pitching history to find the deepest
one - slow (one query per player) but only needs doing once; pass --pitcher
on later runs once you know who that is.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scouting_data
import utils

GRID_SIZE = 1000  # matches the app's 1-1000 pitch-value domain throughout

# delta_after_leverage matches on a THREE-bucket leverage grouping (low / med /
# high) instead of a fixed +/- window around the current leverage: a fixed
# window held ~half of a pitcher's whole history at LI ~0.5 (no information)
# but almost nothing above LI ~1.5. Cuts picked by head-to-head log-loss gain
# over baseline across candidate cut pairs (<0.5 / 0.5-1.0 / 1.0+ won, with
# roughly even-sized groups: ~47% / ~36% / ~16.5% of plays) - redo that
# comparison before moving them.
LEVERAGE_BUCKET_EDGES = (0.5, 1.0)


def leverage_bucket_bounds(leverage: float) -> tuple[float, float]:
    """[lo, hi) of the leverage bucket this leverage falls in (hi=inf for the
    top bucket, lo=0 for the bottom one)."""
    lo = 0.0
    for edge in LEVERAGE_BUCKET_EDGES:
        if leverage < edge:
            return lo, edge
        lo = edge
    return lo, math.inf

# tools/pitch_likelihood_calibrate.py's fit-weights result at sigma=75/
# pitch_width=100/cooldown_radius=50/min_effective_n=20/ (delta_after_leverage
# matching on LEVERAGE_BUCKET_EDGES low/med/high - refit when that replaced the
# old +/-0.25 window; weights moved by <=0.008 each, held-out val/test log-loss
# 6.9080->6.9076 / 6.9055->6.9051)/
# min_history=100/cooldown rate-shrinkage ON - see the module docstring above
# for the held-out numbers and the min-history=30->100 history behind this
# fit. pitch_sequence and delta_cooldown both land at ~0 here (redundant
# with delta_sequence and uninformative once early-career points are
# excluded, respectively, not uninformative in general - see docstring).
# Used as the CLI --weights default; pass --weights to override.
DEFAULT_WEIGHTS = {
    # Refit 2026-10-18 by tools/pitch_model_fit.py (joint fit of all nine pooled signals on 50 TRAIN pitchers, scored on
    # 51 held-out pitchers; league tables built from the train pitchers only). Held-out log-loss 6.9037 -> 6.6792
    # (-0.2245, pitcher-bootstrap 95% CI [-0.30, -0.15], 86% of pitchers improve); realized OBP of the suggested swing
    # +0.022 (CI [+0.003, +0.043]), SLG +0.015 (CI includes 0). Almost all of the gain is the two habit signals and the
    # mixtures below: refitting the ORIGINAL seven signals changed nothing (6.9042), and swapping the cooldowns to
    # excess-over-chance was neutral on log-loss (6.9041) - it is kept because it matches the measured hazards, not
    # because it scored better. delta_cooldown fits to 0: the delta-domain return rate sits at chance at every lag.
    "pitch_sequence": 0.0126,
    "delta_sequence": 0.1177,
    "cooldown": 0.4344,
    "delta_after_result": 0.1626,
    "delta_after_leverage": 0.1177,
    "shadow_delta_after_result": 0.0157,
    "delta_cooldown": 0.0,
    "league_fine": 0.7388,
    "own_digits": 0.5390,
}

# tools/pitch_likelihood_calibrate.py's fit-cooldown-table result (train+val
# pitchers, cooldown_radius=50/delta_cooldown_radius=50) - per-category
# P(a cooldown resolves in exactly k pitches), pooled across every pitcher
# in that roster rather than one pitcher's own (often thin) resolved-sample
# count. cooldown_signal/delta_cooldown_signal blend a pitcher's own
# per-category rate toward these, weighted by how much of that pitcher's
# own data backs it (Config.cooldown_rate_shrinkage_n) - the same shrinkage
# SHAPE combine_signals already uses for a whole signal's n_eff, just one
# level deeper, on the hazard-rate estimate a bump's amplitude comes from
# rather than on the signal's overall density. A k with no entry here (past
# the table's own longest observed cooldown) falls back to the pitcher's own
# rate unshrunk - rare in practice given each category's pooled n is in the
# thousands. Pooled counts per category: BB-1B n=13516, K+ n=12823,
# Out n=20635, XBH n=5905 (pitch-domain); BB-1B n=12407, K+ n=11851,
# Out n=18929, XBH n=5445 (delta-domain). Redo fit-cooldown-table (and
# re-paste both constants) if the roster or either radius ever changes -
# these go stale otherwise, the same way DEFAULT_WEIGHTS would.
COOLDOWN_LEAGUE_RATES = {
    "BB-1B": {1: 0.1124, 2: 0.0787, 3: 0.0761, 4: 0.0731, 5: 0.0679, 6: 0.0630, 7: 0.0575, 8: 0.0511, 9: 0.0438, 10: 0.0411, 11: 0.0377, 12: 0.0322, 13: 0.0298, 14: 0.0242, 15: 0.0237, 16: 0.0198, 17: 0.0188, 18: 0.0164, 19: 0.0127, 20: 0.0122, 21: 0.0107, 22: 0.0094, 23: 0.0081, 24: 0.0088, 25: 0.0069, 26: 0.0061, 27: 0.0058, 28: 0.0061, 29: 0.0044, 30: 0.0051, 31: 0.0049, 32: 0.0024, 33: 0.0040, 34: 0.0026, 35: 0.0027, 36: 0.0023, 37: 0.0021, 38: 0.0018, 39: 0.0016, 40: 0.0013, 41: 0.0011, 42: 0.0008, 43: 0.0014, 44: 0.0007, 45: 0.0012, 46: 0.0003, 47: 0.0006, 48: 0.0005, 49: 0.0002, 50: 0.0004, 51: 0.0004, 52: 0.0003, 53: 0.0002, 54: 0.0004, 55: 0.0002, 56: 0.0001, 57: 0.0003, 58: 0.0002, 59: 0.0002, 60: 0.0001, 61: 0.0001, 63: 0.0001, 64: 0.0001, 66: 0.0001, 67: 0.0001, 69: 0.0001, 71: 0.0001, 72: 0.0001, 75: 0.0001, 76: 0.0001, 79: 0.0001, 84: 0.0001, 88: 0.0001, 128: 0.0001},
    "K+": {1: 0.0944, 2: 0.0870, 3: 0.0805, 4: 0.0757, 5: 0.0678, 6: 0.0611, 7: 0.0572, 8: 0.0509, 9: 0.0472, 10: 0.0417, 11: 0.0363, 12: 0.0336, 13: 0.0306, 14: 0.0255, 15: 0.0222, 16: 0.0198, 17: 0.0193, 18: 0.0161, 19: 0.0139, 20: 0.0135, 21: 0.0105, 22: 0.0104, 23: 0.0079, 24: 0.0076, 25: 0.0062, 26: 0.0058, 27: 0.0064, 28: 0.0042, 29: 0.0054, 30: 0.0041, 31: 0.0034, 32: 0.0034, 33: 0.0030, 34: 0.0030, 35: 0.0034, 36: 0.0023, 37: 0.0021, 38: 0.0012, 39: 0.0010, 40: 0.0017, 41: 0.0014, 42: 0.0011, 43: 0.0012, 44: 0.0005, 45: 0.0007, 46: 0.0009, 47: 0.0009, 48: 0.0004, 49: 0.0009, 50: 0.0002, 51: 0.0005, 52: 0.0005, 53: 0.0002, 54: 0.0002, 56: 0.0001, 57: 0.0001, 58: 0.0003, 59: 0.0002, 60: 0.0003, 61: 0.0001, 62: 0.0002, 63: 0.0002, 64: 0.0001, 65: 0.0001, 66: 0.0002, 67: 0.0002, 69: 0.0002, 70: 0.0001, 71: 0.0001, 73: 0.0001, 74: 0.0001, 75: 0.0001, 77: 0.0001, 79: 0.0001, 81: 0.0001, 85: 0.0002, 86: 0.0001, 90: 0.0002, 94: 0.0001},
    "Out": {1: 0.0968, 2: 0.0806, 3: 0.0815, 4: 0.0754, 5: 0.0686, 6: 0.0630, 7: 0.0602, 8: 0.0535, 9: 0.0451, 10: 0.0390, 11: 0.0384, 12: 0.0326, 13: 0.0294, 14: 0.0245, 15: 0.0214, 16: 0.0205, 17: 0.0182, 18: 0.0169, 19: 0.0146, 20: 0.0115, 21: 0.0106, 22: 0.0109, 23: 0.0099, 24: 0.0083, 25: 0.0077, 26: 0.0063, 27: 0.0057, 28: 0.0059, 29: 0.0046, 30: 0.0039, 31: 0.0041, 32: 0.0039, 33: 0.0026, 34: 0.0023, 35: 0.0019, 36: 0.0021, 37: 0.0027, 38: 0.0024, 39: 0.0013, 40: 0.0010, 41: 0.0014, 42: 0.0010, 43: 0.0006, 44: 0.0005, 45: 0.0005, 46: 0.0008, 47: 0.0006, 48: 0.0003, 49: 0.0003, 50: 0.0003, 51: 0.0004, 52: 0.0003, 53: 0.0002, 54: 0.0002, 55: 0.0001, 56: 0.0002, 57: 0.0002, 58: 0.0003, 59: 0.0001, 60: 0.0001, 61: 0.0000, 62: 0.0001, 63: 0.0002, 64: 0.0001, 65: 0.0000, 66: 0.0001, 67: 0.0003, 68: 0.0001, 69: 0.0000, 70: 0.0001, 74: 0.0000, 81: 0.0000, 90: 0.0000, 94: 0.0000, 104: 0.0001},
    "XBH": {1: 0.1038, 2: 0.0779, 3: 0.0718, 4: 0.0696, 5: 0.0662, 6: 0.0596, 7: 0.0561, 8: 0.0518, 9: 0.0449, 10: 0.0437, 11: 0.0374, 12: 0.0335, 13: 0.0288, 14: 0.0279, 15: 0.0229, 16: 0.0240, 17: 0.0198, 18: 0.0144, 19: 0.0146, 20: 0.0122, 21: 0.0117, 22: 0.0100, 23: 0.0100, 24: 0.0090, 25: 0.0063, 26: 0.0080, 27: 0.0052, 28: 0.0068, 29: 0.0051, 30: 0.0036, 31: 0.0041, 32: 0.0042, 33: 0.0037, 34: 0.0029, 35: 0.0024, 36: 0.0034, 37: 0.0034, 38: 0.0014, 39: 0.0025, 40: 0.0010, 41: 0.0014, 42: 0.0007, 43: 0.0015, 44: 0.0007, 45: 0.0010, 46: 0.0007, 47: 0.0005, 48: 0.0014, 49: 0.0007, 50: 0.0003, 51: 0.0002, 52: 0.0007, 53: 0.0003, 54: 0.0002, 55: 0.0002, 56: 0.0002, 57: 0.0007, 58: 0.0002, 59: 0.0003, 60: 0.0005, 61: 0.0002, 62: 0.0003, 63: 0.0003, 64: 0.0002, 68: 0.0003, 69: 0.0002, 70: 0.0003, 74: 0.0002, 76: 0.0002, 94: 0.0002},
}

DELTA_COOLDOWN_LEAGUE_RATES = {
    "BB-1B": {1: 0.1014, 2: 0.0881, 3: 0.0859, 4: 0.0775, 5: 0.0688, 6: 0.0593, 7: 0.0532, 8: 0.0476, 9: 0.0451, 10: 0.0405, 11: 0.0343, 12: 0.0332, 13: 0.0288, 14: 0.0243, 15: 0.0222, 16: 0.0195, 17: 0.0191, 18: 0.0176, 19: 0.0139, 20: 0.0120, 21: 0.0125, 22: 0.0105, 23: 0.0086, 24: 0.0094, 25: 0.0065, 26: 0.0071, 27: 0.0050, 28: 0.0047, 29: 0.0037, 30: 0.0039, 31: 0.0036, 32: 0.0032, 33: 0.0034, 34: 0.0024, 35: 0.0029, 36: 0.0017, 37: 0.0016, 38: 0.0020, 39: 0.0019, 40: 0.0018, 41: 0.0015, 42: 0.0011, 43: 0.0007, 44: 0.0010, 45: 0.0005, 46: 0.0010, 47: 0.0002, 48: 0.0005, 49: 0.0006, 50: 0.0003, 51: 0.0006, 52: 0.0004, 53: 0.0003, 54: 0.0004, 55: 0.0001, 56: 0.0003, 57: 0.0002, 58: 0.0001, 59: 0.0001, 61: 0.0002, 62: 0.0002, 63: 0.0002, 65: 0.0001, 66: 0.0001, 75: 0.0001, 76: 0.0001, 78: 0.0001, 85: 0.0001, 89: 0.0001, 102: 0.0001},
    "K+": {1: 0.1013, 2: 0.0932, 3: 0.0804, 4: 0.0771, 5: 0.0688, 6: 0.0587, 7: 0.0546, 8: 0.0492, 9: 0.0420, 10: 0.0405, 11: 0.0363, 12: 0.0341, 13: 0.0280, 14: 0.0251, 15: 0.0219, 16: 0.0203, 17: 0.0180, 18: 0.0145, 19: 0.0127, 20: 0.0127, 21: 0.0125, 22: 0.0078, 23: 0.0099, 24: 0.0085, 25: 0.0095, 26: 0.0064, 27: 0.0051, 28: 0.0059, 29: 0.0064, 30: 0.0030, 31: 0.0028, 32: 0.0038, 33: 0.0028, 34: 0.0029, 35: 0.0022, 36: 0.0020, 37: 0.0018, 38: 0.0015, 39: 0.0018, 40: 0.0008, 41: 0.0010, 42: 0.0015, 43: 0.0014, 44: 0.0008, 45: 0.0008, 46: 0.0006, 47: 0.0009, 48: 0.0011, 49: 0.0003, 50: 0.0006, 51: 0.0004, 52: 0.0005, 53: 0.0005, 54: 0.0005, 55: 0.0002, 56: 0.0001, 58: 0.0001, 59: 0.0001, 60: 0.0003, 61: 0.0001, 62: 0.0002, 64: 0.0001, 66: 0.0003, 69: 0.0001, 70: 0.0001, 71: 0.0002, 74: 0.0001, 75: 0.0001, 76: 0.0001, 78: 0.0001, 84: 0.0001},
    "Out": {1: 0.0997, 2: 0.0935, 3: 0.0847, 4: 0.0754, 5: 0.0707, 6: 0.0631, 7: 0.0539, 8: 0.0466, 9: 0.0428, 10: 0.0362, 11: 0.0360, 12: 0.0311, 13: 0.0274, 14: 0.0229, 15: 0.0228, 16: 0.0202, 17: 0.0179, 18: 0.0157, 19: 0.0150, 20: 0.0123, 21: 0.0119, 22: 0.0104, 23: 0.0088, 24: 0.0085, 25: 0.0079, 26: 0.0056, 27: 0.0055, 28: 0.0050, 29: 0.0043, 30: 0.0043, 31: 0.0039, 32: 0.0035, 33: 0.0036, 34: 0.0038, 35: 0.0023, 36: 0.0022, 37: 0.0027, 38: 0.0018, 39: 0.0019, 40: 0.0012, 41: 0.0013, 42: 0.0010, 43: 0.0011, 44: 0.0008, 45: 0.0009, 46: 0.0008, 47: 0.0010, 48: 0.0007, 49: 0.0005, 50: 0.0004, 51: 0.0004, 52: 0.0003, 53: 0.0004, 54: 0.0004, 55: 0.0003, 56: 0.0003, 57: 0.0003, 58: 0.0002, 59: 0.0002, 60: 0.0002, 61: 0.0001, 62: 0.0003, 63: 0.0001, 64: 0.0002, 65: 0.0001, 66: 0.0001, 67: 0.0001, 68: 0.0001, 70: 0.0002, 74: 0.0001, 75: 0.0001, 76: 0.0001, 80: 0.0001, 82: 0.0001, 91: 0.0001, 94: 0.0001, 104: 0.0001, 107: 0.0001},
    "XBH": {1: 0.1006, 2: 0.0878, 3: 0.0819, 4: 0.0770, 5: 0.0674, 6: 0.0564, 7: 0.0520, 8: 0.0483, 9: 0.0430, 10: 0.0415, 11: 0.0356, 12: 0.0312, 13: 0.0285, 14: 0.0274, 15: 0.0242, 16: 0.0200, 17: 0.0189, 18: 0.0180, 19: 0.0151, 20: 0.0119, 21: 0.0108, 22: 0.0099, 23: 0.0107, 24: 0.0081, 25: 0.0075, 26: 0.0068, 27: 0.0053, 28: 0.0051, 29: 0.0042, 30: 0.0051, 31: 0.0039, 32: 0.0050, 33: 0.0018, 34: 0.0026, 35: 0.0031, 36: 0.0024, 37: 0.0017, 38: 0.0022, 39: 0.0013, 40: 0.0020, 41: 0.0013, 42: 0.0007, 43: 0.0013, 44: 0.0013, 45: 0.0009, 46: 0.0011, 47: 0.0004, 48: 0.0004, 49: 0.0011, 50: 0.0004, 51: 0.0004, 52: 0.0002, 53: 0.0004, 54: 0.0007, 55: 0.0004, 56: 0.0004, 57: 0.0004, 58: 0.0002, 59: 0.0004, 60: 0.0002, 62: 0.0002, 63: 0.0002, 64: 0.0002, 69: 0.0004, 77: 0.0002, 80: 0.0002, 103: 0.0002},
}


# --------------------------------------------------------------------- shipped league tables

# pitch_model_tables.json (tools/build_pitch_model_tables.py) holds the league-level tables the newer parts of the
# model use: cooldown HAZARDS (the conditional chance a still-open trigger's next pitch lands in its zone), the league
# pitch-value counts behind the fine-structure prior, and league repeat/echo rates. Rebuild it (not by hand) when the
# play log grows enough to matter; evaluation harnesses rebuild it from TRAIN pitchers only so a held-out pitcher never
# feeds his own table.
TABLES_PATH = pathlib.Path(__file__).resolve().parent.parent / "pitch_model_tables.json"
COOLDOWN_EXPIRE_ELAPSED = utils.COOLDOWN_EXPIRE_ELAPSED   # open triggers older than this carry no information (see utils)
N_EFF_FULL = 1e9              # "pre-shrunk" signals (their own smoothing is built in): combine_signals' alpha -> 1
# fitted with DEFAULT_WEIGHTS above (tools/pitch_model_fit.py): e_exact = weight on his own past exact values, m_spike =
# shrinkage strength of the online repeat / echo rates toward the league rate
HABIT_DEFAULTS = {"mixtures": True, "e_exact": 0.02, "m_spike": 400.0}


def prepare_tables(raw: dict) -> dict:
    """Raw JSON -> the arrays the model reads: hazards keyed by int lag, the fine-structure log-ratio over the wheel
    (league fine pmf / its own sigma-75 smoothing, so it carries ONLY what the pitcher KDE cannot see), spike rates."""
    haz = {dom: {c: {int(k): v for k, v in lags.items() if v is not None} for c, lags in raw[dom].items()}
           for dom in ("cooldown_hazard", "delta_cooldown_hazard")}
    cnt = np.asarray(raw["league_counts"], dtype=float) + 0.5
    q_fine = cnt / cnt.sum()
    q_smooth = _kde_density_counts(cnt, 75.0)
    return {"cooldown_hazard": haz["cooldown_hazard"], "delta_cooldown_hazard": haz["delta_cooldown_hazard"],
            "log_fine": np.log(q_fine / q_smooth), "spike_rates": dict(raw["spike_rates"]),
            "radius": raw.get("radius", 50)}


def _kde_density_counts(counts: np.ndarray, sigma: float) -> np.ndarray:
    idx = np.arange(GRID_SIZE)
    d = np.abs(idx[:, None] - idx[None, :])
    d = np.minimum(d, GRID_SIZE - d)
    k = np.exp(-(d ** 2) / (2 * sigma ** 2))
    out = k @ counts
    return out / out.sum()


_DEFAULT_TABLES: dict | None = None


def default_tables() -> dict:
    global _DEFAULT_TABLES
    if _DEFAULT_TABLES is None:
        _DEFAULT_TABLES = prepare_tables(json.loads(TABLES_PATH.read_text()))
    return _DEFAULT_TABLES


# --------------------------------------------------------------------- kernel / density

def _circular_distance(centers: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """(len(centers), len(grid)) circular distance matrix on [1, GRID_SIZE],
    same wraparound convention as utils._circ_dist_vec - reimplemented here as
    a matrix op (instead of looping _circ_dist_vec per center) since this runs
    once per signal per backtest step."""
    d = np.abs(centers[:, None] - grid[None, :])
    return np.minimum(d, GRID_SIZE - d)


def _kde_density(centers: np.ndarray, sigma: float, weights: np.ndarray | None = None) -> np.ndarray:
    """Wrapped-Gaussian KDE over the 1-1000 wheel. Empty/degenerate input
    falls back to a flat (uniform) density rather than erroring or returning
    NaN - callers treat "no data" the same as "no information," which is
    exactly what a flat density represents."""
    grid = np.arange(1, GRID_SIZE + 1, dtype=float)
    if centers is None or len(centers) == 0:
        return np.full(GRID_SIZE, 1.0 / GRID_SIZE)
    centers = np.asarray(centers, dtype=float)
    d = _circular_distance(centers, grid)
    k = np.exp(-(d ** 2) / (2 * sigma ** 2))
    if weights is not None:
        k = k * np.asarray(weights, dtype=float)[:, None]
    density = k.sum(axis=0)
    total = density.sum()
    if total <= 1e-12:
        return np.full(GRID_SIZE, 1.0 / GRID_SIZE)
    return density / total


# --------------------------------------------------------------------------- signals

class Signal:
    __slots__ = ("name", "density", "n_eff")

    def __init__(self, name: str, density: np.ndarray, n_eff: float):
        self.name = name
        self.density = density
        self.n_eff = n_eff


def build_state(df_hist: pd.DataFrame, target_row: pd.Series | None = None) -> dict | None:
    """Everything the signals below need to know about "right now." Almost
    everything here is derived purely from df_hist (already-frozen history -
    nothing may look past the pitch being predicted) - the one exception is
    target_leverage, which reads the stakes OFF THE TARGET ROW ITSELF
    (its own pre-play outs/obc/score/inning), not its outcome. That's not a
    leak: leverage is knowable before a pitch is thrown, the same way a
    pitcher knows the game state they're about to pitch into - it's exactly
    what the live Manager tab's own Leverage caption shows in real time.
    None if there isn't enough history yet to even ask the question (fewer
    than 2 prior pitches)."""
    pitches = df_hist[df_hist["pitch"].notna()].sort_values("id")
    if len(pitches) < 2:
        return None
    prev_pitch = int(pitches.iloc[-1]["pitch"])
    prev_pitch2 = int(pitches.iloc[-2]["pitch"])
    # The delta INTO prev_pitch (from prev_pitch2) - delta_sequence_signal's
    # match target. Read straight off the frame's own pitch_circ_delta rather
    # than recomputed by hand, so it can never drift from enrich_df's
    # same-game-gated wrapping convention. None at a game's first pitch (no
    # predecessor), same as the column itself.
    prev_delta_raw = pitches.iloc[-1].get("pitch_circ_delta")
    prev_delta = int(prev_delta_raw) if pd.notna(prev_delta_raw) else None

    results = df_hist[df_hist["result"].notna()].sort_values("id")
    prev_result_cat = (utils.seq_result_category(results.iloc[-1]["result"])
                        if not results.empty else None)

    # The most recent plate appearance's own swing - the anchor
    # shadow_delta_after_result_signal projects onto. pitch_shadow_delta is
    # defined relative to a swing (see enrich_df), not to a pitch, so
    # anchoring it on prev_pitch the way the other delta signals do would mix
    # units.
    swings = df_hist[df_hist["swing"].notna()].sort_values("id")
    prev_swing = int(swings.iloc[-1]["swing"]) if not swings.empty else None

    target_leverage = None
    if target_row is not None:
        lev_series = utils.compute_play_leverage(pd.DataFrame([target_row]))
        lev_val = lev_series.iloc[0]
        target_leverage = float(lev_val) if pd.notna(lev_val) else None

    return {
        "prev_pitch": prev_pitch,
        "prev_pitch2": prev_pitch2,
        "prev_delta": prev_delta,
        "prev_result_cat": prev_result_cat,
        "prev_swing": prev_swing,
        "target_leverage": target_leverage,
    }


def pitch_sequence_signal(df_hist: pd.DataFrame, state: dict, width: int, sigma: float) -> Signal | None:
    """Historical pitches whose own immediately-prior pitch lands in a bucket
    centered on the CURRENT most recent pitch - single-step match (reuses
    filter_by_prior_context's prev_pitch_bucket alone), not a 2-pitch
    compound pattern. state["prev_pitch2"] is kept around in build_state for
    a future delta-sequence signal's own single-step match, not used here."""
    b1 = utils._centered_match_interval(state["prev_pitch"], width, domain_hi=1000, domain_lo=1)
    matched = utils.filter_by_prior_context(df_hist, prev_pitch_bucket=b1)
    vals = matched["pitch"].dropna().astype(int).to_numpy()
    if len(vals) == 0:
        return None
    return Signal("pitch_sequence", _kde_density(vals, sigma), float(len(vals)))


def delta_sequence_signal(df_hist: pd.DataFrame, state: dict, width: int, sigma: float) -> Signal | None:
    """One-step delta-sequence match: historical pitches whose own
    immediately-prior DELTA (the move into them, pitch_circ_delta) lands near
    the CURRENT most recent delta (state["prev_delta"], the move into the
    pitcher's actual last pitch) - the delta-domain mirror of
    pitch_sequence_signal's absolute-value match. Where pitch_sequence asks
    "what usually follows this pitch value," this asks "what delta usually
    follows a delta this size."

    A match's own delta is itself a MOVE, not a destination, so (unlike
    pitch_sequence_signal, where the matched row's pitch value is already the
    implied next pitch) it still needs projecting onto today's actual last
    pitch (state["prev_pitch"]) as anchor - same anchor + delta projection
    delta_after_result_signal uses."""
    prev_delta = state.get("prev_delta")
    if prev_delta is None:
        return None
    lo, hi = utils._centered_match_interval(prev_delta, width, domain_hi=500, domain_lo=-500)
    matched = utils.filter_by_prior_context(df_hist, prev_delta_bucket=(lo, hi))
    deltas = matched["pitch_circ_delta"].dropna().astype(int).to_numpy()
    if len(deltas) == 0:
        return None
    implied = ((state["prev_pitch"] + deltas - 1) % 1000) + 1
    return Signal("delta_sequence", _kde_density(implied, sigma), float(len(deltas)))


def delta_after_result_signal(df_hist: pd.DataFrame, state: dict, sigma: float) -> Signal | None:
    """Historical pitch-to-pitch DELTAS (not absolute pitch values) measured
    on the pitch immediately after the same prior-result category (XBH/
    BB-1B/Out/K+), then projected onto an implied next pitch via the CURRENT
    actual most recent pitch (state["prev_pitch"]) as anchor - same
    anchor + delta projection _implied_pitch_points uses elsewhere.

    This is "after this kind of result, how far does the pitcher usually
    move" applied to where they actually are right now, not "after this kind
    of result, what absolute value do they usually land on" - the absolute-
    value version ignores where the pitcher was coming from each historical
    time, so it doesn't generalize the way a relative move does."""
    if not state["prev_result_cat"]:
        return None
    matched = utils.filter_by_prior_context(df_hist, prev_result_cat=state["prev_result_cat"])
    deltas = matched["pitch_circ_delta"].dropna().astype(int).to_numpy()
    if len(deltas) == 0:
        return None
    implied = ((state["prev_pitch"] + deltas - 1) % 1000) + 1
    return Signal("delta_after_result", _kde_density(implied, sigma), float(len(deltas)))


def shadow_delta_after_result_signal(df_hist: pd.DataFrame, state: dict, sigma: float) -> Signal | None:
    """Historical SHADOW deltas (pitch_shadow_delta_signed: circular delta
    from the PREVIOUS plate appearance's own swing to the pitch that follows
    it - see enrich_df, and utils.shadow_delta_vs_prior_result_heatmap for the
    same pairing as a heatmap) measured on the pitch immediately after the
    same prior-result category, projected onto today's own previous swing
    (state["prev_swing"]) as anchor.

    Same "after this kind of result, how far does the pitcher move" question
    as delta_after_result_signal, just measured from where the batter's bat
    actually ended up instead of from the pitcher's own last pitch - shadow
    delta is defined relative to a swing, so anchoring it on prev_pitch the
    way delta_after_result does would mix units."""
    if not state["prev_result_cat"] or state.get("prev_swing") is None:
        return None
    matched = utils.filter_by_prior_context(df_hist, prev_result_cat=state["prev_result_cat"])
    deltas = matched["pitch_shadow_delta_signed"].dropna().astype(int).to_numpy()
    if len(deltas) == 0:
        return None
    implied = ((state["prev_swing"] + deltas - 1) % 1000) + 1
    return Signal("shadow_delta_after_result", _kde_density(implied, sigma), float(len(deltas)))


def _shrunk_return_prob(
    resolved: pd.DataFrame, k: int, category: str,
    league_rates: dict[str, dict[int, float]], shrinkage_n: float,
) -> float:
    """Blends a pitcher's own empirical P(this category's cooldown resolves
    in exactly k pitches) - built from just their own resolved sample,
    however thin - toward the league-wide rate for that category/k
    (COOLDOWN_LEAGUE_RATES/DELTA_COOLDOWN_LEAGUE_RATES), weighted by how much
    of the pitcher's own data backs their own estimate. Same shrinkage SHAPE
    combine_signals already uses for a whole signal's n_eff (alpha = n/(n+
    const)), just applied one level deeper - on the hazard-rate estimate a
    bump's amplitude comes from, not on the signal's overall density. Falls
    back to the pitcher's own rate unshrunk if the league table has no entry
    for this exact k (past its longest observed cooldown - rare given each
    category's pooled n is in the thousands, see the table's own comment)."""
    n_eff_cat = len(resolved)
    p_pitcher = float((resolved["pitches"] == k).sum()) / n_eff_cat if n_eff_cat else 0.0
    p_league = league_rates.get(category, {}).get(k)
    if p_league is None:
        return p_pitcher
    alpha = n_eff_cat / (n_eff_cat + shrinkage_n)
    return alpha * p_pitcher + (1 - alpha) * p_league


def cooldown_signal(df_hist: pd.DataFrame, radius: float,
                     league_rates: dict[str, dict[int, float]], shrinkage_n: float) -> Signal | None:
    """Every CURRENTLY OPEN cooldown trigger contributes its own hypothesis
    for where this pitch could land - an XBH 3 pitches ago AND a K+ 11
    pitches ago can both still be "due" at once, independently, not just
    whichever trigger happens to be most recent. Reuses cooldown_events
    directly rather than re-deriving "what's open" by hand: every unresolved
    row already IS an open trigger going into this pitch.

    Each trigger gets its own bump (its own category's empirical P(a return
    lands exactly now) as the bump's amplitude, the rest spread flat), and
    the signal's overall density is the plain mean of those per-trigger
    densities - since each bump's amplitude already reflects its own
    p_return_now, a trigger more likely to resolve right now naturally comes
    through taller in the average without needing extra weighting. This is
    an ADDITIVE mixture across open triggers specifically (not another
    multiplicative pool): multiple open cooldowns are "any of these could be
    the one that closes," not "all of these must agree."

    p_return_now itself is shrunk toward league_rates (_shrunk_return_prob)
    before it's used - a pitcher with only a handful of resolved cooldowns in
    this trigger's category would otherwise get an amplitude built from a
    tiny, noisy sample with no protection at all (n_eff below only gates how
    much the WHOLE signal counts afterward, in combine_signals - this is a
    separate, earlier shrinkage point on the rate itself).

    n_eff is the MINIMUM n_eff across the triggers' categories (deduped, so
    two open triggers sharing a category don't double-count that category's
    resolved-sample support) - conservative, since the pooled signal is only
    as trustworthy as its least-supported open trigger."""
    events = utils.cooldown_events(df_hist, value_col="pitch", radius=radius)
    if events.empty:
        return None
    open_events = events[~events["resolved"]]
    if open_events.empty:
        return None

    resolved_by_cat: dict[str, pd.DataFrame] = {}

    def _resolved_for(cat: str) -> pd.DataFrame:
        if cat not in resolved_by_cat:
            resolved_by_cat[cat] = events[events["resolved"] & (events["category"] == cat)]
        return resolved_by_cat[cat]

    uniform = np.full(GRID_SIZE, 1.0 / GRID_SIZE)
    densities, n_effs = [], []
    for _, trig in open_events.iterrows():
        resolved = _resolved_for(trig["category"])
        n_eff_cat = len(resolved)
        if n_eff_cat == 0:
            continue
        k = int(trig["pitches"]) + 1
        p_return_now = _shrunk_return_prob(resolved, k, trig["category"], league_rates, shrinkage_n)
        bump = _kde_density(np.array([trig["trigger_pitch"]]), sigma=radius / 2)
        densities.append(p_return_now * bump + (1 - p_return_now) * uniform)
        n_effs.append(n_eff_cat)

    if not densities:
        return None
    density = np.mean(densities, axis=0)
    density = density / density.sum()
    return Signal("cooldown", density, float(min(n_effs)))


def delta_cooldown_signal(df_hist: pd.DataFrame, state: dict, radius: float,
                           league_rates: dict[str, dict[int, float]], shrinkage_n: float) -> Signal | None:
    """The delta-domain mirror of cooldown_signal: triggers and "due-ness"
    are measured on pitch-to-pitch DELTAS (pitch_circ_delta - the move into
    a pitch, not the pitch's own absolute value) instead of absolute pitch
    values. cooldown_events is domain-agnostic (its wraparound math only
    cares about a width-1000 circular range, not where that range's zero
    point sits), so this reuses it unchanged with value_col="pitch_circ_delta"
    - a trigger here is "the pitcher just threw a delta of this size and got
    this result; how long until they moved by a similarly-sized delta again."

    Each open trigger's own delta value is a MOVE, not a destination, so
    (like delta_sequence_signal/delta_after_result_signal) it still needs
    projecting onto today's actual most recent pitch (state["prev_pitch"]) as
    anchor before it can contribute a bump to the pitch-value density -
    unlike cooldown_signal, where the trigger's own value already IS a pitch
    value. Same p_return_now/n_eff/additive-mixture/league-rate-shrinkage
    logic as cooldown_signal otherwise (DELTA_COOLDOWN_LEAGUE_RATES rather
    than COOLDOWN_LEAGUE_RATES - a pitch-domain and delta-domain cooldown
    resolve at different rates, so they get separate shrinkage targets) -
    see its docstring."""
    events = utils.cooldown_events(df_hist, value_col="pitch_circ_delta", radius=radius)
    if events.empty:
        return None
    open_events = events[~events["resolved"]]
    if open_events.empty:
        return None

    resolved_by_cat: dict[str, pd.DataFrame] = {}

    def _resolved_for(cat: str) -> pd.DataFrame:
        if cat not in resolved_by_cat:
            resolved_by_cat[cat] = events[events["resolved"] & (events["category"] == cat)]
        return resolved_by_cat[cat]

    uniform = np.full(GRID_SIZE, 1.0 / GRID_SIZE)
    densities, n_effs = [], []
    for _, trig in open_events.iterrows():
        resolved = _resolved_for(trig["category"])
        n_eff_cat = len(resolved)
        if n_eff_cat == 0:
            continue
        k = int(trig["pitches"]) + 1
        p_return_now = _shrunk_return_prob(resolved, k, trig["category"], league_rates, shrinkage_n)
        implied = ((state["prev_pitch"] + int(trig["trigger_pitch"]) - 1) % 1000) + 1
        bump = _kde_density(np.array([implied]), sigma=radius / 2)
        densities.append(p_return_now * bump + (1 - p_return_now) * uniform)
        n_effs.append(n_eff_cat)

    if not densities:
        return None
    density = np.mean(densities, axis=0)
    density = density / density.sum()
    return Signal("delta_cooldown", density, float(min(n_effs)))


def _zone_masses(radius: float) -> tuple[float, float]:
    """(z, m): the share of a flat density inside a +/-radius zone, and the share of a cooldown bump (a sigma =
    radius/2 wrapped Gaussian) inside it. A bump of weight a over a flat floor puts (1-a)*z + a*m in the zone."""
    z = (2 * int(radius) + 1) / GRID_SIZE
    bump = _kde_density(np.array([500]), sigma=radius / 2)
    grid = np.arange(1, GRID_SIZE + 1)
    m = float(bump[np.abs(grid - 500) <= radius].sum())
    return z, m


def _excess_weight(h: float, z: float, m: float) -> float:
    """Bump weight that makes the zone's total mass equal the measured return rate h: (1-a)*z + a*m = h. The old
    signal used the unconditional mass as the weight, which counted the by-chance landing twice (once in the mass,
    once in the flat floor). Floored slightly below 0: right after a trigger pitchers land in the zone a touch LESS
    than chance, which a small negative weight (a dent, not a bump) represents - bounded so the density stays >= 0."""
    return float(np.clip((h - z) / (m - z), -0.06, 1.0))


def _own_hazard(events: pd.DataFrame, category: str, k: int, league_h: float | None, shrinkage_n: float,
                z: float) -> tuple[float, int]:
    return utils.cooldown_hazard_estimate(events, category, k, league_h, shrinkage_n, z)


def _cooldown_excess(df_hist: pd.DataFrame, state: dict, radius: float, hazard_table: dict, shrinkage_n: float,
                     value_col: str, name: str, delta_anchor: bool) -> Signal | None:
    events = utils.cooldown_events(df_hist, value_col=value_col, radius=radius)
    if events.empty:
        return None
    live = events[(~events["resolved"]) & (events["pitches"] < COOLDOWN_EXPIRE_ELAPSED)]
    if live.empty:
        return None
    z, m = _zone_masses(radius)
    uniform = np.full(GRID_SIZE, 1.0 / GRID_SIZE)
    densities, depths = [], []
    for _, trig in live.iterrows():
        k = int(trig["pitches"]) + 1
        h, at_risk = _own_hazard(events, trig["category"], k, hazard_table.get(trig["category"], {}).get(k),
                                 shrinkage_n, z)
        center = trig["trigger_pitch"]
        if delta_anchor:
            center = ((state["prev_pitch"] + int(center) - 1) % 1000) + 1
        a = _excess_weight(h, z, m)
        densities.append((1 - a) * uniform + a * _kde_density(np.array([center]), sigma=radius / 2))
        depths.append(at_risk)
    density = np.mean(densities, axis=0)
    return Signal(name, density / density.sum(), float(max(min(depths), 1)))


def cooldown_signal_excess(df_hist, state, radius, hazard_table, shrinkage_n):
    return _cooldown_excess(df_hist, state, radius, hazard_table, shrinkage_n, "pitch", "cooldown", False)


def delta_cooldown_signal_excess(df_hist, state, radius, hazard_table, shrinkage_n):
    return _cooldown_excess(df_hist, state, radius, hazard_table, shrinkage_n, "pitch_circ_delta",
                            "delta_cooldown", True)


def _baseline_of(df_hist: pd.DataFrame, cfg) -> np.ndarray:
    return _kde_density(df_hist[df_hist["pitch"].notna()]["pitch"].astype(int).to_numpy(), cfg.sigma)


def league_fine_signal(df_hist: pd.DataFrame, state: dict, cfg) -> Signal | None:
    """What every pitcher shares and the sigma-75 pitcher KDE smooths away: humans pick round, patterned numbers
    (3.9% of all pitches are multiples of 100). Expressed as baseline x league fine-structure factor so, pooled with a
    weight, it multiplies the factor in (a factor, not a distribution: its own smoothing is built in, so n_eff is full)."""
    b = _baseline_of(df_hist, cfg)
    d = b * np.exp(cfg.tables["log_fine"])
    return Signal("league_fine", d / d.sum(), N_EFF_FULL)


def own_digits_signal(df_hist: pd.DataFrame, state: dict, cfg) -> Signal | None:
    """This pitcher's own last-two-digit habit (01, 11, 69, 77, round numbers...): Dirichlet-smoothed share of his
    earlier pitches ending in each two digits, over uniform, applied to the wheel as a factor on his baseline."""
    pv = df_hist[df_hist["pitch"].notna()]["pitch"].astype(int).to_numpy()
    l2 = np.bincount(pv % 100, minlength=100).astype(float)
    factor = np.log(((l2 + 1.0) / (l2.sum() + 100.0)) * 100.0)[np.arange(1, GRID_SIZE + 1) % 100]
    b = _baseline_of(df_hist, cfg)
    d = b * np.exp(factor)
    return Signal("own_digits", d / d.sum(), N_EFF_FULL)


def apply_mixtures(combined: np.ndarray, df_hist: pd.DataFrame, state: dict, cfg,
                   target_row: pd.Series | None = None) -> np.ndarray:
    """Final stage after the pool: two mixtures that are not log-linear in nature.

    1. Exact past values: (1-e)*combined + e*(his own empirical distribution of exact pitch values) - he re-throws
       numbers he has thrown before.
    2. Repeat / echo spikes: point masses on the previous pitch and the previous swing against him, each at a rate
       estimated ONLINE from his own history (hits / n, shrunk toward the league rate with strength m_spike), so a
       pitcher who echoes gets a big spike and one who never does gets about the league's. (A third spike on the pitch
       BEFORE last was dropped: removing it changed held-out log-loss by -0.0001, CI [-0.0016, +0.0016].)
       A reference only counts within the same game (a first pitch of a game has no 'previous pitch' to echo) when the
       target row carries a game_id; live predictions without one assume the last pitch is in the same game."""
    if not cfg.habit.get("mixtures", True):
        return combined
    pitches = df_hist[df_hist["pitch"].notna()].sort_values("id")
    n = len(pitches)
    if n < 3:
        return combined
    pv = pitches["pitch"].astype(int).to_numpy()
    sw = pitches["swing"].astype(float).to_numpy() if "swing" in pitches else np.full(n, np.nan)
    gm = pitches["game_id"].astype(float).to_numpy() if "game_id" in pitches else np.zeros(n)

    e = float(cfg.habit["e_exact"])
    own = np.bincount((pv - 1) % GRID_SIZE, minlength=GRID_SIZE).astype(float)
    q = (1 - e) * combined + e * own / own.sum()

    tgt_game = None
    if target_row is not None and "game_id" in target_row and pd.notna(target_row["game_id"]):
        tgt_game = float(target_row["game_id"])
    league = cfg.tables["spike_rates"]
    m = float(cfg.habit["m_spike"])
    refs = {  # name -> (reference value now, historical hit flags, historical "defined" flags, that reference's game)
        "prev_pitch": (pv[-1], pv[1:] == pv[:-1], gm[1:] == gm[:-1], gm[-1]),
        "prev_swing": (sw[-1], pv[1:] == sw[:-1], gm[1:] == gm[:-1], gm[-1]),
    }
    total, add = 0.0, np.zeros(GRID_SIZE)
    for name, (val, hit, defined, ref_game) in refs.items():
        if val != val or (tgt_game is not None and ref_game != tgt_game):
            continue
        rate = (float(hit[defined].sum()) + m * league[name]) / (float(defined.sum()) + m)
        total += rate
        add[int(val) - 1] += rate
    return (1 - total) * q + add


def predict_combined(df_hist: pd.DataFrame, baseline: np.ndarray, signals: list, state: dict, cfg,
                     target_row: pd.Series | None = None) -> np.ndarray:
    """THE model's final density: the log-linear pool of the signals, then the exact-value / repeat mixtures.
    Every consumer (backtest, calibration, the Streamlit tab, the Discord bot) goes through this one function."""
    pooled = combine_signals(baseline, signals, cfg.min_effective_n, cfg.weights)
    return apply_mixtures(pooled, df_hist, state, cfg, target_row)


def delta_after_leverage_signal(df_hist: pd.DataFrame, state: dict, sigma: float) -> Signal | None:
    """Historical pitch-to-pitch DELTAS (not absolute pitch values) measured
    on pitches thrown in situations whose own leverage (stakes, via
    utils.compute_play_leverage - outs/obc/score/inning, not what just
    happened pitch-wise) falls in the same low / med / high bucket as the
    CURRENT situation's leverage (LEVERAGE_BUCKET_EDGES), then projected onto
    an implied next pitch via the CURRENT actual most recent pitch
    (state["prev_pitch"]) as anchor - same anchor + delta projection
    delta_after_result_signal uses, same reasoning: "under similar stakes,
    how far does the pitcher usually move" applied to where they actually
    are right now, rather than "under similar stakes, what absolute value do
    they land on," which ignores where they're coming from each historical
    time and so doesn't generalize."""
    target_lev = state.get("target_leverage")
    if target_lev is None:
        return None
    df_lev = df_hist.copy()
    df_lev["_leverage"] = utils.compute_play_leverage(df_lev)
    lo, hi = leverage_bucket_bounds(target_lev)
    matched = df_lev[(df_lev["_leverage"] >= lo) & (df_lev["_leverage"] < hi) & df_lev["pitch_circ_delta"].notna()]
    deltas = matched["pitch_circ_delta"].dropna().astype(int).to_numpy()
    if len(deltas) == 0:
        return None
    implied = ((state["prev_pitch"] + deltas - 1) % 1000) + 1
    return Signal("delta_after_leverage", _kde_density(implied, sigma), float(len(deltas)))


SIGNAL_BUILDERS = {
    "pitch_sequence": lambda df_hist, state, cfg: pitch_sequence_signal(df_hist, state, cfg.pitch_width, cfg.sigma),
    "delta_sequence": lambda df_hist, state, cfg: delta_sequence_signal(df_hist, state, cfg.pitch_width, cfg.sigma),
    "cooldown": lambda df_hist, state, cfg: (
        cooldown_signal_excess(df_hist, state, cfg.cooldown_radius, cfg.tables["cooldown_hazard"],
                               cfg.cooldown_rate_shrinkage_n)
        if cfg.cooldown_mode == "excess" else cooldown_signal(
            df_hist, cfg.cooldown_radius, cfg.cooldown_league_rates, cfg.cooldown_rate_shrinkage_n)),
    "delta_after_result": lambda df_hist, state, cfg: delta_after_result_signal(df_hist, state, cfg.sigma),
    "delta_after_leverage": lambda df_hist, state, cfg: delta_after_leverage_signal(
        df_hist, state, cfg.sigma),
    "shadow_delta_after_result": lambda df_hist, state, cfg: shadow_delta_after_result_signal(df_hist, state, cfg.sigma),
    "delta_cooldown": lambda df_hist, state, cfg: (
        delta_cooldown_signal_excess(df_hist, state, cfg.delta_cooldown_radius, cfg.tables["delta_cooldown_hazard"],
                                     cfg.cooldown_rate_shrinkage_n)
        if cfg.cooldown_mode == "excess" else delta_cooldown_signal(
            df_hist, state, cfg.delta_cooldown_radius, cfg.delta_cooldown_league_rates, cfg.cooldown_rate_shrinkage_n)),
    "league_fine": lambda df_hist, state, cfg: league_fine_signal(df_hist, state, cfg),
    "own_digits": lambda df_hist, state, cfg: own_digits_signal(df_hist, state, cfg),
}


# ------------------------------------------------------------------------ combination

def combine_signals(baseline: np.ndarray, signals: list[Signal], min_effective_n: float,
                     weights: dict[str, float] | None = None) -> np.ndarray:
    """Log-linear pool: shrink each signal toward baseline by its own n_eff
    first (so a 2-point signal can't swing the result), then multiply the
    shrunk/baseline ratios together in log-space and renormalize. See the
    design discussion this came from for why multiplicative (not an averaged
    mixture) is the right shape when signals are meant to behave as
    independent evidence."""
    eps = 1e-12
    log_combined = np.log(baseline + eps)
    for s in signals:
        alpha = s.n_eff / (s.n_eff + min_effective_n)
        shrunk = alpha * s.density + (1 - alpha) * baseline
        w = (weights or {}).get(s.name, 1.0)
        log_combined = log_combined + w * np.log((shrunk + eps) / (baseline + eps))
    log_combined -= log_combined.max()  # numerically stable before exponentiating
    combined = np.exp(log_combined)
    return combined / combined.sum()


# ----------------------------------------------------------------------------- backtest

class Config:
    def __init__(self, sigma, min_effective_n, pitch_width, cooldown_radius, leverage_width=1.0,
                 delta_cooldown_radius=50.0, weights=None,
                 cooldown_rate_shrinkage_n=20.0, cooldown_league_rates=None,
                 delta_cooldown_league_rates=None, cooldown_mode="excess", tables=None, habit=None):
        self.sigma = sigma
        self.min_effective_n = min_effective_n
        self.pitch_width = pitch_width
        self.cooldown_radius = cooldown_radius
        # Legacy: delta_after_leverage no longer uses a +/- window (it matches
        # on LEVERAGE_BUCKET_EDGES), so this is accepted and exported for
        # callers/artifacts that still pass it but has no modeling effect.
        self.leverage_width = leverage_width
        self.delta_cooldown_radius = delta_cooldown_radius
        self.weights = weights
        # Separate from min_effective_n deliberately - that shrinks a whole
        # SIGNAL's density toward this pitcher's own baseline once it's
        # built; this shrinks the per-category p_return_now rate INSIDE
        # cooldown_signal/delta_cooldown_signal, toward the league-wide rate
        # for that category, before any bump is even built. Two different
        # noise sources (how much data backs the whole signal vs. how much
        # data backs this one category's hazard estimate), two different
        # shrinkage points - see cooldown_signal's docstring.
        self.cooldown_rate_shrinkage_n = cooldown_rate_shrinkage_n
        # "excess": the cooldown bump's height is how much a still-open trigger's return rate beats chance (hazard-
        # based, expires after COOLDOWN_EXPIRE_ELAPSED pitches); "pmf": the legacy unconditional-mass bump.
        self.cooldown_mode = cooldown_mode
        self.tables = tables if tables is not None else default_tables()
        self.habit = {**HABIT_DEFAULTS, **(habit or {})}
        self.cooldown_league_rates = (
            cooldown_league_rates if cooldown_league_rates is not None else COOLDOWN_LEAGUE_RATES)
        self.delta_cooldown_league_rates = (
            delta_cooldown_league_rates if delta_cooldown_league_rates is not None
            else DELTA_COOLDOWN_LEAGUE_RATES)


def _prepare_step(df_hist: pd.DataFrame, builders: list, cfg: Config, target_row: pd.Series | None = None):
    """One backtest step's worth of computation - shared by backtest_pitcher's
    loop and explain_point, so "what the model actually did" can never drift
    from "what the aggregate stats were computed from". target_row (the row
    AT the pitch being predicted, not in df_hist) is only used for the
    leverage signal's own pre-play game state - see build_state. Returns
    (state, baseline, signals) or (None, None, None) if this point isn't
    eligible (not enough history, or no signal could be built)."""
    state = build_state(df_hist, target_row)
    if state is None:
        return None, None, None
    baseline = _kde_density(df_hist[df_hist["pitch"].notna()]["pitch"].astype(int).to_numpy(), cfg.sigma)
    signals = [s for s in (builder(df_hist, state, cfg) for builder in builders) if s is not None]
    if not signals:
        return state, baseline, None
    return state, baseline, signals


def backtest_pitcher(df: pd.DataFrame, min_history: int, cfg: Config,
                      active_signals: list[str] | None = None) -> pd.DataFrame:
    builders = [SIGNAL_BUILDERS[name] for name in (active_signals or SIGNAL_BUILDERS.keys())]
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    records = []
    eps = 1e-12

    for i in range(min_history, len(pitches)):
        cur_id = pitches.loc[i, "id"]
        df_hist = df[df["id"] < cur_id]

        state, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=pitches.loc[i])
        if state is None or signals is None:
            continue
        combined = predict_combined(df_hist, baseline, signals, state, cfg, target_row=pitches.loc[i])

        actual = int(pitches.loc[i, "pitch"])
        idx = actual - 1
        record = {
            "id": cur_id,
            "actual": actual,
            "n_signals": len(signals),
            "n_eff_min": min(s.n_eff for s in signals),
            "logloss_baseline": -math.log(baseline[idx] + eps),
            "logloss_combined": -math.log(combined[idx] + eps),
            # PIT: cumulative probability mass strictly below the actual value,
            # plus half the mass AT it - the standard tie-corrected rank used
            # to turn a continuous/discretized density into a 0-1 calibration
            # statistic. Flat histogram across many pitches = honest
            # calibration; skewed = systematically over/under-confident.
            "pit_combined": float(combined[:idx].sum() + combined[idx] / 2),
        }
        # Per-signal n_eff too, not just the min across them - lets summarize()
        # report each signal's own typical sample depth, since "the weakest
        # signal was thin" and "pitch_sequence specifically is always thin"
        # call for different fixes.
        for s in signals:
            record["n_eff_" + s.name] = s.n_eff
        records.append(record)

    return pd.DataFrame(records)


def recent_pitch_densities(df: pd.DataFrame, cfg: Config, n: int,
                            active_signals: list[str] | None = None) -> list[dict]:
    """Baseline/per-signal/combined density (plus the actual pitch thrown)
    for each of this pitcher's last n pitches with enough prior history -
    the density-PRESERVING counterpart to backtest_pitcher's full
    logloss-only walk, built for suggested-swing-style backtests
    (utils.suggest_swing_from_density) rather than log-loss, over a short
    recent window rather than a full career. Same per-point computation as
    backtest_pitcher (frozen history strictly before each pitch, target_row
    supplies its own pre-play leverage), just keeping the raw densities
    instead of reducing each point to a log-loss/PIT scalar, and walking
    only the last n eligible points instead of everything from some
    min_history forward. Per-signal densities cost nothing extra to keep -
    _prepare_step already computes them before combine_signals pools them,
    this just stops discarding that list afterward.

    Returns a list ordered oldest-to-newest, each
    {"id", "actual", "baseline", "signals", "combined"} - "signals" is
    [{"name", "density"}, ...], each signal's own RAW (unshrunk) density,
    any subset/order of SIGNAL_BUILDERS depending what had enough context at
    that point. Shorter than n if fewer than n pitches have >= 2 prior
    pitches of history."""
    builders = [SIGNAL_BUILDERS[name] for name in (active_signals or SIGNAL_BUILDERS.keys())]
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    start = max(0, len(pitches) - n)
    out = []
    for i in range(start, len(pitches)):
        cur_id = pitches.loc[i, "id"]
        df_hist = df[df["id"] < cur_id]
        state, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=pitches.loc[i])
        if state is None or signals is None:
            continue
        combined = predict_combined(df_hist, baseline, signals, state, cfg, target_row=pitches.loc[i])
        out.append({
            "id": cur_id, "actual": int(pitches.loc[i, "pitch"]), "baseline": baseline,
            "signals": [{"name": s.name, "density": s.density} for s in signals],
            "combined": combined,
        })
    return out


def _top_k(density: np.ndarray, k: int = 5) -> list[tuple[int, float]]:
    """(pitch value, density) pairs for the k highest-density grid points,
    highest first - "where does this density think the action is"."""
    order = np.argsort(density)[::-1][:k]
    return [(int(i) + 1, float(density[i])) for i in order]


def explain_point(df: pd.DataFrame, cur_id, cfg: Config, active_signals: list[str] | None = None) -> None:
    """Walks through exactly one backtest step in full - the same
    computation _prepare_step/combine_signals do inside backtest_pitcher,
    just printed instead of reduced to a log-loss number. Use this to sanity-
    check or explain a single prediction, not to evaluate the model overall -
    see backtest_pitcher/summarize for that."""
    builders = [SIGNAL_BUILDERS[name] for name in (active_signals or SIGNAL_BUILDERS.keys())]
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    row_idx = pitches.index[pitches["id"] == cur_id]
    if len(row_idx) == 0:
        print(f"No pitch with id={cur_id} found.", file=sys.stderr)
        return
    i = int(row_idx[0])
    actual = int(pitches.loc[i, "pitch"])
    df_hist = df[df["id"] < cur_id]

    print(f"=== Explaining pitch id={cur_id} (#{i + 1} of {len(pitches)} in this pitcher's history) ===\n")
    print(f"Frozen history going into this pitch: {len(df_hist[df_hist['pitch'].notna()])} prior pitches "
          f"(everything with id < {cur_id} - this pitch and everything after it is invisible to the model).\n")

    state, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=pitches.loc[i])
    if state is None:
        print("Not enough history yet for this pitch (fewer than 2 prior pitches) - nothing to explain.")
        return

    print("Circumstance (read off the frozen history):")
    print(f"  Most recent pitch:        {state['prev_pitch']}")
    print(f"  Pitch before that:        {state['prev_pitch2']}")
    print(f"  Most recent PA result:    {state['prev_result_cat'] or '(none yet)'}")
    open_cd = utils.cooldown_events(df_hist, value_col="pitch", radius=cfg.cooldown_radius)
    open_cd = open_cd[~open_cd["resolved"]].sort_values("id") if not open_cd.empty else open_cd
    if not open_cd.empty:
        print(f"  Open cooldown triggers ({len(open_cd)}, ALL feed the cooldown signal):")
        for _, trig in open_cd.iterrows():
            print(f"    - {trig['result']} ({trig['category']}) at pitch {trig['trigger_pitch']}, "
                  f"{int(trig['pitches'])} pitches ago, still not returned within {cfg.cooldown_radius:.0f}")
    else:
        print("  Open cooldown triggers:   (none)")
    open_dcd = utils.cooldown_events(df_hist, value_col="pitch_circ_delta", radius=cfg.delta_cooldown_radius)
    open_dcd = open_dcd[~open_dcd["resolved"]].sort_values("id") if not open_dcd.empty else open_dcd
    if not open_dcd.empty:
        print(f"  Open delta cooldown triggers ({len(open_dcd)}, ALL feed the delta_cooldown signal):")
        for _, trig in open_dcd.iterrows():
            print(f"    - {trig['result']} ({trig['category']}) at delta {int(trig['trigger_pitch']):+d}, "
                  f"{int(trig['pitches'])} pitches ago, still not returned within {cfg.delta_cooldown_radius:.0f}")
    else:
        print("  Open delta cooldown triggers:   (none)")
    lev = state.get("target_leverage")
    print(f"  Leverage of this pitch:    {lev:.2f}" if lev is not None else "  Leverage of this pitch:    n/a")
    print()

    print(f"Baseline (this pitcher's unconditioned history, n={int((df_hist['pitch'].notna()).sum())}):")
    for v, d in _top_k(baseline, 5):
        print(f"  {v:>4d}: {d * 100:.3f}%")
    print()

    if signals is None:
        print("No signal could be built for this context (no historical matches for any of them) - "
              "the combined prediction would just equal the baseline.")
        signals = []
    for s in signals:
        alpha = s.n_eff / (s.n_eff + cfg.min_effective_n)
        print(f"Signal '{s.name}': n_eff={s.n_eff:.0f}  "
              f"(trust weight alpha={alpha:.2f} - {alpha * 100:.0f}% this signal, "
              f"{(1 - alpha) * 100:.0f}% baseline, before this signal is even multiplied against the others)")
        for v, d in _top_k(s.density, 3):
            print(f"    {v:>4d}: {d * 100:.3f}%")
    print()

    combined = (predict_combined(df_hist, baseline, signals, state, cfg, target_row=pitches.loc[i])
                if signals else baseline)
    print("Combined prediction (all signals multiplied together, log-linear pool):")
    for v, d in _top_k(combined, 5):
        flag = "  <-- this happened" if v == actual else ""
        print(f"  {v:>4d}: {d * 100:.3f}%{flag}")
    print()

    idx = actual - 1
    eps = 1e-12
    ll_base = -math.log(baseline[idx] + eps)
    ll_comb = -math.log(combined[idx] + eps)
    pit = float(combined[:idx].sum() + combined[idx] / 2)
    print(f"What actually happened: pitch = {actual}")
    print(f"  Baseline put {baseline[idx] * 100:.4f}% on this value  (log-loss {ll_base:.4f})")
    print(f"  Combined put {combined[idx] * 100:.4f}% on this value  (log-loss {ll_comb:.4f})")
    verdict = "combined scored BETTER than baseline" if ll_comb < ll_base else \
        "combined scored WORSE than baseline" if ll_comb > ll_base else "combined tied baseline"
    print(f"  -> {verdict} on this one pitch (lower log-loss = less surprised by what happened)")
    print(f"  PIT (percentile rank of the actual value within the combined distribution): {pit:.3f}")
    print("     (0.5 = landed right at the median of the predicted distribution; near 0 or 1 = "
          "landed in a tail the model thought was unlikely)")


def build_explain_export(df: pd.DataFrame, pitcher: str, cur_id, cfg: Config,
                          active_signals: list[str] | None = None) -> dict | None:
    """Same one backtest step explain_point walks through, as JSON instead of
    print statements - state, baseline, every active signal's density AND its
    own raw matched inputs (for the 'Pitch Signal Autopsy' artifact's
    per-signal derivation panels), the combined prediction, and the grading
    numbers. The signal densities themselves come straight from
    _prepare_step/combine_signals (the same call backtest_pitcher/
    explain_point make) so they can never drift from what the model actually
    did; only the "raw" arrays below are a second, deliberately duplicated
    read of each signal's own match step (each one just a few lines, mirrored
    from its builder above) so the artifact can show "how the curve was
    built" without threading extra return values through SIGNAL_BUILDERS."""
    builders = [SIGNAL_BUILDERS[n] for n in (active_signals or SIGNAL_BUILDERS.keys())]
    pitches = df[df["pitch"].notna()].sort_values("id").reset_index(drop=True)
    row_idx = pitches.index[pitches["id"] == cur_id]
    if len(row_idx) == 0:
        return None
    i = int(row_idx[0])
    actual = int(pitches.loc[i, "pitch"])
    df_hist = df[df["id"] < cur_id]

    state, baseline, signals = _prepare_step(df_hist, builders, cfg, target_row=pitches.loc[i])
    if state is None or signals is None:
        return None
    combined = predict_combined(df_hist, baseline, signals, state, cfg, target_row=pitches.loc[i])
    sig_by_name = {s.name: s for s in signals}

    events = utils.cooldown_events(df_hist, value_col="pitch", radius=cfg.cooldown_radius)
    open_cd = events[~events["resolved"]].sort_values("id") if not events.empty else events

    delta_events = utils.cooldown_events(df_hist, value_col="pitch_circ_delta", radius=cfg.delta_cooldown_radius)
    open_dcd = delta_events[~delta_events["resolved"]].sort_values("id") if not delta_events.empty else delta_events

    derivation: dict = {}

    if "pitch_sequence" in sig_by_name:
        b1 = utils._centered_match_interval(state["prev_pitch"], cfg.pitch_width, domain_hi=1000, domain_lo=1)
        matched = utils.filter_by_prior_context(df_hist, prev_pitch_bucket=b1)
        derivation["pitch_sequence"] = {
            "anchor": state["prev_pitch"], "bucket": list(b1),
            "raw": matched["pitch"].dropna().astype(int).tolist(),
        }

    if "delta_sequence" in sig_by_name and state.get("prev_delta") is not None:
        lo, hi = utils._centered_match_interval(state["prev_delta"], cfg.pitch_width, domain_hi=500, domain_lo=-500)
        matched = utils.filter_by_prior_context(df_hist, prev_delta_bucket=(lo, hi))
        raw_deltas = matched["pitch_circ_delta"].dropna().astype(int).tolist()
        derivation["delta_sequence"] = {
            "anchor": state["prev_pitch"], "context_anchor": state["prev_delta"], "bucket": [lo, hi],
            "raw_deltas": raw_deltas,
            "raw_implied": [((state["prev_pitch"] + d - 1) % 1000) + 1 for d in raw_deltas],
        }

    if "delta_after_result" in sig_by_name and state["prev_result_cat"]:
        matched = utils.filter_by_prior_context(df_hist, prev_result_cat=state["prev_result_cat"])
        raw_deltas = matched["pitch_circ_delta"].dropna().astype(int).tolist()
        derivation["delta_after_result"] = {
            "anchor": state["prev_pitch"], "result_cat": state["prev_result_cat"],
            "raw_deltas": raw_deltas,
            "raw_implied": [((state["prev_pitch"] + d - 1) % 1000) + 1 for d in raw_deltas],
        }

    if ("shadow_delta_after_result" in sig_by_name and state["prev_result_cat"]
            and state.get("prev_swing") is not None):
        matched = utils.filter_by_prior_context(df_hist, prev_result_cat=state["prev_result_cat"])
        raw_deltas = matched["pitch_shadow_delta_signed"].dropna().astype(int).tolist()
        derivation["shadow_delta_after_result"] = {
            "anchor": state["prev_swing"], "result_cat": state["prev_result_cat"],
            "raw_deltas": raw_deltas,
            "raw_implied": [((state["prev_swing"] + d - 1) % 1000) + 1 for d in raw_deltas],
        }

    if "cooldown" in sig_by_name and not open_cd.empty:
        resolved_by_cat: dict = {}

        def _resolved_for(cat: str) -> pd.DataFrame:
            if cat not in resolved_by_cat:
                resolved_by_cat[cat] = events[events["resolved"] & (events["category"] == cat)]
            return resolved_by_cat[cat]

        triggers = []
        for _, trig in open_cd.iterrows():
            resolved = _resolved_for(trig["category"])
            n_eff_cat = len(resolved)
            k = int(trig["pitches"]) + 1
            p_raw = float((resolved["pitches"] == k).sum()) / n_eff_cat if n_eff_cat else 0.0
            p_return_now = _shrunk_return_prob(resolved, k, trig["category"], cfg.cooldown_league_rates,
                                               cfg.cooldown_rate_shrinkage_n)
            triggers.append({
                "result": trig["result"], "category": trig["category"], "trigger_pitch": int(trig["trigger_pitch"]),
                "pitches_since": int(trig["pitches"]), "k": k, "n_eff": n_eff_cat,
                "p_return_now": p_return_now, "p_return_now_raw": p_raw,
                "p_return_now_league": cfg.cooldown_league_rates.get(trig["category"], {}).get(k),
                "resolved_pitches": resolved["pitches"].astype(int).tolist(),
            })
        derivation["cooldown"] = triggers

    if "delta_cooldown" in sig_by_name and not open_dcd.empty:
        resolved_by_cat_d: dict = {}

        def _resolved_for_d(cat: str) -> pd.DataFrame:
            if cat not in resolved_by_cat_d:
                resolved_by_cat_d[cat] = delta_events[delta_events["resolved"] & (delta_events["category"] == cat)]
            return resolved_by_cat_d[cat]

        triggers_d = []
        for _, trig in open_dcd.iterrows():
            resolved = _resolved_for_d(trig["category"])
            n_eff_cat = len(resolved)
            k = int(trig["pitches"]) + 1
            p_raw = float((resolved["pitches"] == k).sum()) / n_eff_cat if n_eff_cat else 0.0
            p_return_now = _shrunk_return_prob(resolved, k, trig["category"], cfg.delta_cooldown_league_rates,
                                               cfg.cooldown_rate_shrinkage_n)
            implied = ((state["prev_pitch"] + int(trig["trigger_pitch"]) - 1) % 1000) + 1
            triggers_d.append({
                "result": trig["result"], "category": trig["category"],
                "trigger_delta": int(trig["trigger_pitch"]), "implied_pitch": implied,
                "pitches_since": int(trig["pitches"]), "k": k, "n_eff": n_eff_cat,
                "p_return_now": p_return_now, "p_return_now_raw": p_raw,
                "p_return_now_league": cfg.delta_cooldown_league_rates.get(trig["category"], {}).get(k),
                "resolved_pitches": resolved["pitches"].astype(int).tolist(),
            })
        derivation["delta_cooldown"] = triggers_d

    if "delta_after_leverage" in sig_by_name and state.get("target_leverage") is not None:
        df_lev = df_hist.copy()
        df_lev["_leverage"] = utils.compute_play_leverage(df_lev)
        target_lev = state["target_leverage"]
        lo, hi = leverage_bucket_bounds(target_lev)
        matched = df_lev[(df_lev["_leverage"] >= lo) & (df_lev["_leverage"] < hi) & df_lev["pitch_circ_delta"].notna()]
        raw_deltas = matched["pitch_circ_delta"].dropna().astype(int).tolist()
        derivation["delta_after_leverage"] = {
            "anchor": state["prev_pitch"], "target_leverage": target_lev,
            "lo": lo, "hi": None if math.isinf(hi) else hi,
            "width": None if math.isinf(hi) else hi - lo,
            "raw_deltas": raw_deltas,
            "raw_implied": [((state["prev_pitch"] + d - 1) % 1000) + 1 for d in raw_deltas],
        }

    eps = 1e-12
    idx = actual - 1
    return {
        "pitcher": pitcher,
        "id": int(cur_id),
        "n_history": int(df_hist["pitch"].notna().sum()),
        "state": {
            "prev_pitch": state["prev_pitch"], "prev_pitch2": state["prev_pitch2"],
            "prev_result_cat": state["prev_result_cat"], "target_leverage": state.get("target_leverage"),
        },
        "open_triggers": [
            {"result": t["result"], "category": t["category"], "trigger_pitch": int(t["trigger_pitch"]),
             "pitches_since": int(t["pitches"])}
            for _, t in open_cd.iterrows()
        ] if not open_cd.empty else [],
        "open_delta_triggers": [
            {"result": t["result"], "category": t["category"], "trigger_delta": int(t["trigger_pitch"]),
             "pitches_since": int(t["pitches"])}
            for _, t in open_dcd.iterrows()
        ] if not open_dcd.empty else [],
        "cooldown_radius": cfg.cooldown_radius,
        "delta_cooldown_radius": cfg.delta_cooldown_radius,
        "leverage_width": cfg.leverage_width,
        "baseline": baseline.tolist(),
        "baseline_raw": df_hist[df_hist["pitch"].notna()]["pitch"].astype(int).tolist(),
        "signals": [
            {"name": s.name, "n_eff": s.n_eff, "alpha": s.n_eff / (s.n_eff + cfg.min_effective_n),
             "weight": (cfg.weights or {}).get(s.name, 1.0), "density": s.density.tolist()}
            for s in signals
        ],
        "combined": combined.tolist(),
        "actual": actual,
        "logloss_baseline": -math.log(baseline[idx] + eps),
        "logloss_combined": -math.log(combined[idx] + eps),
        "pit": float(combined[:idx].sum() + combined[idx] / 2),
        "min_effective_n": cfg.min_effective_n,
        "sigma": cfg.sigma,
        "pitch_width": cfg.pitch_width,
        "cooldown_bump_width": cfg.cooldown_radius / 2,
        "delta_cooldown_bump_width": cfg.delta_cooldown_radius / 2,
        "derivation": derivation,
    }


def summarize(records: pd.DataFrame) -> None:
    if records.empty:
        print("No backtest points produced - not enough history past --min-history.")
        return

    print(f"Backtest points: {len(records)}")
    mean_base = records["logloss_baseline"].mean()
    mean_comb = records["logloss_combined"].mean()
    print(f"Mean log-loss, baseline (unconditioned):  {mean_base:.4f}")
    print(f"Mean log-loss, combined signals:          {mean_comb:.4f}")
    print(f"Improvement (positive = combined better): {mean_base - mean_comb:.4f}")

    print("\nPIT histogram (deciles; roughly flat counts = well-calibrated):")
    decile = pd.cut(records["pit_combined"], bins=10, labels=False, include_lowest=True)
    for b in range(10):
        n = int((decile == b).sum())
        print(f"  [{b / 10:.1f}-{(b + 1) / 10:.1f}): {n}")

    print("\nLog-loss by weakest-signal n_eff bucket (where does confidence become earned?):")
    bucket = pd.cut(records["n_eff_min"], bins=[-1, 10, 20, 40, float("inf")],
                     labels=["<10", "10-20", "20-40", "40+"])
    grouped = records.groupby(bucket, observed=True)[["logloss_baseline", "logloss_combined"]].mean()
    grouped["n"] = records.groupby(bucket, observed=True).size()
    print(grouped.to_string())

    n_eff_cols = [c for c in records.columns if c.startswith("n_eff_") and c != "n_eff_min"]
    if n_eff_cols:
        print("\nPer-signal n_eff distribution across all backtest points (not just the weakest):")
        stats = records[n_eff_cols].describe(percentiles=[.25, .5, .75]).T
        stats = stats[["min", "25%", "50%", "75%", "max"]].rename(columns={"50%": "median"})
        stats.index = [c.replace("n_eff_", "") for c in stats.index]
        print(stats.round(1).to_string())


def _pick_deepest_pitcher() -> tuple[str | None, int]:
    names = scouting_data.all_player_names()
    best_name, best_n = None, -1
    for i, nm in enumerate(names):
        try:
            df = scouting_data.load_pitcher_plays(nm)
        except Exception:
            continue
        n = int(df["pitch"].notna().sum()) if not df.empty and "pitch" in df.columns else 0
        if n > best_n:
            best_name, best_n = nm, n
        if (i + 1) % 25 == 0:
            print(f"  scanned {i + 1}/{len(names)}... best so far: {best_name} ({best_n} pitches)",
                  file=sys.stderr)
    return best_name, best_n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pitcher", default=None, help="Pitcher name (default: scan for deepest history)")
    ap.add_argument("--min-history", type=int, default=100,
                     help="Skip the first N pitches of a pitcher's career - not enough context yet")
    ap.add_argument("--sigma", type=float, default=75.0, help="KDE kernel width, in wheel units")
    ap.add_argument("--min-effective-n", type=float, default=20.0,
                     help="Shrinkage point toward baseline (your 'relatively confident around 20' gut-check)")
    ap.add_argument("--pitch-width", type=int, default=100, help="Bucket width for the pitch-sequence match")
    ap.add_argument("--cooldown-radius", type=float, default=50.0, help="Radius for the cooldown signal")
    ap.add_argument("--delta-cooldown-radius", type=float, default=50.0,
                     help="Radius (in delta units) for the delta_cooldown signal")
    ap.add_argument("--leverage-width", type=float, default=0.5,
                     help="Legacy / no effect: the leverage signal now matches on LEVERAGE_BUCKET_EDGES "
                          "(low / med / high), not a +/- band")
    ap.add_argument("--weights", default=None,
                     help="Comma-separated name=value overrides for the log-linear pool (default: "
                          "DEFAULT_WEIGHTS, the calibrated fit - see the module docstring), e.g. "
                          "'cooldown=1.0,pitch_sequence=0.5'")
    ap.add_argument("--signals", default=None,
                     help=f"Comma-separated subset of {{{','.join(SIGNAL_BUILDERS)}}} to isolate one at a time "
                          "(default: all)")
    ap.add_argument("--explain", default=None,
                     help="Walk through ONE pitch in full detail instead of running the aggregate backtest: "
                          "'latest' for the most recent eligible pitch, or a specific row id")
    ap.add_argument("--export-json", default=None,
                     help="With --explain, write that same one-pitch explanation to this path as JSON "
                          "(the 'Pitch Signal Autopsy' artifact's data.json shape) instead of printing it")
    args = ap.parse_args()
    active_signals = args.signals.split(",") if args.signals else None
    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        for pair in args.weights.split(","):
            k, v = pair.split("=")
            weights[k.strip()] = float(v)

    if args.pitcher:
        name = args.pitcher
    else:
        print("No --pitcher given - scanning every player for the deepest pitching history "
              "(slow, one-time; pass --pitcher next time)...", file=sys.stderr)
        name, n = _pick_deepest_pitcher()
        if name is None:
            print("No pitcher with any pitch history found.", file=sys.stderr)
            sys.exit(1)
        print(f"Using {name} ({n} pitches)", file=sys.stderr)

    df = scouting_data.load_pitcher_plays(name)
    if df.empty:
        print(f"No plays found for '{name}'.", file=sys.stderr)
        sys.exit(1)

    cfg = Config(sigma=args.sigma, min_effective_n=args.min_effective_n,
                 pitch_width=args.pitch_width, cooldown_radius=args.cooldown_radius,
                 leverage_width=args.leverage_width, delta_cooldown_radius=args.delta_cooldown_radius,
                 weights=weights)

    if args.explain is not None:
        pitches = df[df["pitch"].notna()].sort_values("id")
        if args.explain == "latest":
            cur_id = pitches.iloc[-1]["id"]
        else:
            cur_id = type(pitches["id"].iloc[0])(args.explain)
        if args.export_json:
            payload = build_explain_export(df, name, cur_id, cfg, active_signals)
            if payload is None:
                print("Not enough history yet for this pitch - nothing to export.", file=sys.stderr)
                sys.exit(1)
            with open(args.export_json, "w") as f:
                json.dump(payload, f)
            print(f"Wrote {args.export_json}", file=sys.stderr)
            return
        print(f"=== {name} ===\n")
        explain_point(df, cur_id, cfg, active_signals)
        return

    records = backtest_pitcher(df, args.min_history, cfg, active_signals)
    print(f"\n=== {name} ({'+'.join(active_signals) if active_signals else 'all signals'}) ===")
    summarize(records)


if __name__ == "__main__":
    main()

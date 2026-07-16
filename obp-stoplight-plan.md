# Scouting-recency stoplight for the "OBP recent X" rows - implementation plan

Pitcher-side only. Adds three stoplight signals - `"OBP recent pitch"`, `"OBP recent Δ"`,
`"OBP recent Δ²"` (exactly the Suggestions row names, so the page's Signal -> state
mapping works untouched) - backed by a categorical bucket-prediction backtest per
Decisions A-D. The batter side has no stoplight system; its own "OBP recent Δ" rows
(`pages/2_Scouting.py:2426+`) share names but never touch a states dict - no collision.

Revision 2 of this plan. Two design changes vs. revision 1: (a) the historical zone is
REGROWN per step from that step's own score curve (not a copy of today's shape), which
forces the evidence store into displacement space - see Stage 2b for why; (b) the
backtest population IS weighted via a vectorized `compute_pa_weights` equivalent - the
"unweighted" simplification is withdrawn after costing it (Stage 3).

Project constraints for every stage:
- **No em dashes in any `.py` file** - hyphens only. Avoid the subscript glyph "₀" in
  code/labels; write `p0` / "Base %".
- No Co-Authored-By trailer if committing.

## Current-state facts this plan builds on (verified against the working tree)

1. `obp_zone_signal`'s grow procedure (`utils.py:2273-2289`): from `best_idx`, walk
   outward up to `obr_max` steps per side while the score curve stays above the
   best/worst midpoint; flat curve (`best - worst < 1e-6`) falls back to
   `left = right = obr_max`. The backtest reruns this procedure per historical step
   (vectorized, Stage 1) - so its return dict needs NO new keys and the function is
   untouched; instead the grow rule is mirrored in the backtest and must be kept
   in exact sync (validation test in Stage 8).
2. The live populations cross game boundaries: `_h_recent` is a plain
   `df_p_pred...tail(n_pitches)` (`pages/2_Scouting.py:1457-1460`), and
   `project_from_deltas`/`project_from_delta2s` (`utils.py:2008-2033`) take deltas
   between consecutive list entries with no game awareness. The backtest must mirror
   this (deliberately different from the sequence signals' same-game discipline).
3. `project_from_delta2s` emits consecutive (grow, shrink) pairs; in the backtest the
   population is only used to build the FFT weight array for `best_val` - no z-score,
   no trial counting - so `paired` semantics are irrelevant here.
4. The existing stoplight loaders (`_load_pitcher_stoplights`, page:258-269) score the
   pitcher's FULL real-league history from `_load_pitcher_plays`, ignoring the page's
   season/team/source filters, scrimmage concat, and hist-mode truncation. The OBP
   loaders inherit this discipline (flagged below, not fixed).
5. The page's stoplight-attach loop explicitly skips OBP rows:
   `if _sig.startswith("OBP"): continue` (`pages/2_Scouting.py:1714`). Stage 5 removes
   that skip.
6. `_aggregate_recency(scores, window_n, k)` (`utils.py:3825`) never actually uses `k` -
   it can be reused as-is for the new signals.
7. `window_n` for the existing stoplights IS the "Recent PA Window" slider
   (`n_pitches`) - the same value the live OBP populations are tailed by. So one
   parameter serves both the population window (Decision C) and the vote window.
8. The existing per-step engine special-cases "no history yet" to score 0
   (`utils.py:3783-3785`); the generalized formula reproduces that automatically
   (`p_pred = p0` when the evidence store is empty -> `ln(1) = 0`), so no special case
   is needed, though keeping the fast path is fine.
9. `compute_pa_weights` (`utils.py:2049-2117`) runs two per-row Python loops with
   scalar `.iloc` access (result quality at 2085-2093, state similarity at 2095-2105)
   plus a per-call `sort_values`. Every factor decomposes into per-pitch
   precomputable arrays (Stage 3), which is what makes the weighted backtest cheap.

---

## Stage 1 - utils.py: per-step zone regrowth, batched

At each eligible backtest step `t`, the zone shape is grown FRESH from that step's own
score curve, using today's live `ranges` (hence today's kernel and `obr_max`) per
Decision A - but NOT today's `(left, right)`:

- The Stage 3 walk already batch-computes the full score matrix `S` (`n_eligible x
  1000`) for the argmax. Reuse it for the grow:
  - `best_idx = S.argmax(axis=1)` (argmin when `maximize=False`; pitcher side is
    always True), `best = S[rows, best_idx]`, `worst = S.min(axis=1)`.
  - Flat rows (`best - worst < 1e-6`): `left_t = right_t = obr_max` (mirror
    `obp_zone_signal`'s fallback exactly).
  - Else `mid = (best + worst) / 2`, `above = S > mid[:, None]`; build two
    `(n_eligible, obr_max)` gather matrices with fancy indexing,
    `cols_plus = (best_idx[:, None] + offs) % 1000` and the minus twin for
    `offs = 1..obr_max`; the per-row run length of leading Trues is the grow result:
    `right_t = where(row has a False, argmin(axis=1), obr_max)` (argmin on a boolean
    row returns the first False index = the run length), same for `left_t`.
- `W_t = left_t + right_t + 1` (integer count `lo_t..hi_t` inclusive; same convention
  note as before: `obp_zone_signal`'s internal `width = left + right` for its z-score
  is a display-side convention - the backtest's `p0` must be the true covered
  fraction, so use the `+1` form and say so in a comment).
- `W_t >= 1000` (possible only when `obr_max >= 500`, flat fallback) -> that step is
  ineligible (None), same as any other degenerate guard.

Cost: the gathers are `(n_eligible, obr_max)` numpy ops - for 3,000 steps and
`obr_max ~ 300` that is ~2M element ops, single-digit milliseconds. No Python loop.

---

## Stage 2 - utils.py: partition geometry, displacement pooling, generalized score

### 2a. `obp_bucket_widths(W: int) -> list[int] | None`

Unchanged from revision 1 - pure geometry, unit-testable, now called per step with
`W_t` (memoize in a dict `{W: widths}`; only a handful of distinct `W_t` values occur
per pitcher):

- `W >= 1000` -> None (degenerate; caller skips the step).
- `remaining = 1000 - W`; `n_other = max(1, int(remaining / W + 0.5))` - round half UP
  explicitly (`int(x + 0.5)`), not Python's banker-rounding `round()`.
- Cap: `n_other = min(n_other, OBP_MAX_OTHER_BUCKETS, remaining)`, module constant
  `OBP_MAX_OTHER_BUCKETS = 19` (k <= 20 matches the finest existing signal,
  dd_bkt=25 -> 500/25 = 20; bounds per-step work and worst-case |score|). The
  Stage 7 calibration now yields the empirical `W_t` distribution for free - check it
  before freezing 19.
- `base, rem = divmod(remaining, n_other)`; widths `[W] + [base+1]*rem +
  [base]*(n_other-rem)`. Assert `sum == 1000`.

`p0_t = [w / 1000 for w in obp_bucket_widths(W_t)]`, `k_t = len(widths_t)` - **per
step, purely geometric, never learned.** (This supersedes revision 1's / the spec's
"p0 is FIXED forever" phrasing: what stays fixed is that p0 is geometry-only; its
VALUE now varies with `W_t`.)

### 2b. Why regrowth forces displacement pooling (the honest statistical picture)

The intended generalization - "index 0 = whatever the zone was that day, width
included" - is achievable, but naive relative-index count pooling does NOT survive a
varying `W_t`, for two concrete reasons:

1. **Variable k.** `n_other` is a function of `W_t`, so the count vector's length
   changes per step: an observation recorded as "index 4 of 7" has no defined meaning
   at a step whose partition has 3 buckets. Any fix (truncate, renormalize, pad) is
   ad hoc mass surgery.
2. **Null-invariance breaks.** Position-variation is harmless because it leaves the
   null untouched: a uniform-random pitch lands in a W-wide zone with probability
   W/1000 wherever the zone sits, so pooled relative-index counts have exactly
   width-proportional expectation under "no tendency". Width-variation changes the
   null per step: pooled `counts[0]/total` estimates the AVERAGE historical hit rate
   under the historical mix of widths (`mean_t W_t/1000`), but the score compares it
   against the CURRENT step's `p0_t[0] = W_t/1000`. If widths drift (zones wide early,
   narrow late, or vice versa), the expected score under the null is nonzero - a
   biased light that "detects" a tendency that is pure geometry drift.

So the mechanism changes: **store the evidence as raw displacements, re-bucket per
step.**

- Canonical displacement at each scored step: `u_t = (v_t - best_val_t) % 1000` -
  the observed pitch's circular offset from that step's zone center.
- Evidence store: a single 1000-bin histogram `disp_hist[u] += 1` (updated AFTER
  scoring, as always).
- At step `t`, the partition in displacement space is: zone = `u in [0, right_t] or
  u >= 1000 - left_t`; other buckets tile ascending from `u = right_t + 1` with
  `widths_t[1:]`. Bucket evidence `D_t[i]` = sum of `disp_hist` over bucket i's
  arc(s) - O(k_t) range-sums off a prefix sum of `disp_hist` (recompute the prefix
  sum per step, O(1000); ~3M ops total, negligible).

Why this is the right generalization and not a different design:

- **It reduces exactly to relative-index counting when the shape is constant** - a
  displacement's bucket membership never changes, so `D_t[i]` IS `counts[i]`.
  Revision 1's fixed-shape design is the degenerate case of this one.
- **Null-invariance is restored.** Under "no tendency", displacements are uniform on
  0..999 regardless of what the zone looked like on any given day, so
  `E[D_t[i]]/total = widths_t[i]/1000 = p0_t[i]` at every step, expected score 0.
  This is the same property that made position-variation safe, recovered for
  width-variation - i.e. the user's "same kind of generalization" intuition holds,
  but only in displacement space.
- **Variable k dissolves**: the histogram is partition-agnostic; each step aggregates
  it under its own `k_t`-bucket geometry with no mass loss.

One semantic nuance to document (docstring + a line in the Inspector caption): the
per-step prediction `p_pred_t[0]` is "probability of landing in TODAY's zone given
his historical displacement behavior", not "his historical hit rate of each day's own
zone". Those differ when widths vary, and the former is the statistically coherent
prediction target (the latter is a different random variable per step and reintroduces
the broken null). The scored OBSERVATION each step is still "which of that day's
buckets he hit", and P(in recommended zone) remains reportable at every step.

### 2c. Generalize `_score_from_probs` in place (unchanged from revision 1)

`_score_from_probs(p, observed, p0=None)` (`utils.py:3751`): `p0=None` -> exact
current `ln(k * p[observed])`; `p0` given -> `ln(p[observed] / p0[observed])`. No
`SCOUT_SCORING_VERSION` bump (bit-identical for existing callers);
`_surprisal_walk`/`_surprisal_walk_detail` untouched.

### 2d. The per-step update rule (exact formulas, displacement version)

State: `disp_hist[0..999]` (floats - see weighting note below), scalar `total`.

At each eligible step `t`:

```
left_t, right_t, best_val_t   from Stage 1 (that step's own curve, today's kernel)
widths_t = obp_bucket_widths(left_t + right_t + 1);  skip step if None
p0_t[i]  = widths_t[i] / 1000;  k_t = len(widths_t)
D_t[i]   = sum of disp_hist over bucket i's displacement arc(s)
p_pred_t[i] = (D_t[i] + alpha * k_t * p0_t[i]) / (total + alpha * k_t)   # alpha = 1.0
u_t  = (v_t - best_val_t) % 1000;  ob = bucket index of u_t
score_t = ln(p_pred_t[ob] / p0_t[ob])
disp_hist[u_t] += 1; total += 1                                          # AFTER scoring
```

Backward-compatibility check (keep as a comment): constant shape -> `D_t = counts`
and `p0 = 1/k` -> `(counts[i] + alpha) / (total + alpha*k)` - the existing
`_surprisal_walk` smoothing exactly. Pseudo-count is `alpha * k_t * p0_t[i]` (NOT
`alpha * p0_t[i]`), denominator `total + alpha * k_t` (NOT `total + alpha`) - the
spec flags the unscaled version as the tempting wrong guess.

`p0_t` vs `p_pred_t` naming discipline unchanged: two arrays, never one variable
reused for both.

### 2e. Toy worked example (encode as asserts in a scratchpad script during Stage 8)

Three steps with a varying shape, `alpha = 1`:

| step | shape (left/right) | W_t | widths_t | p0_t | disp_hist before | D_t | p_pred_t | u_t -> obs | p_obs | p0[obs] | score | class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 200/199 | 400 | [400,300,300] | [.4,.3,.3] | empty (total 0) | [0,0,0] | = p0 = [.400,.300,.300] | u=100 -> 0 (zone: u<=199 or u>=800) | .400 | .400 | 0.000 | neutral |
| 2 | 125/124 | 250 | [250,250,250,250] | [.25 x4] | {100: 1} (total 1) | [1,0,0,0] (u=100 <= 124 -> in zone) | (D + 4*.25)/(1+4) = [.400,.200,.200,.200] | u=300 -> 1 (others: [125,374],[375,624],[625,874]) | .200 | .250 | ln(.8) = -0.223 | anti |
| 3 | 200/199 | 400 | [400,300,300] | [.4,.3,.3] | {100:1, 300:1} (total 2) | [1,1,0] (100 in zone; 300 in [200,499]) | (D + 3*p0)/(2+3) = [.440,.380,.180] | u=50 -> 0 | .440 | .400 | ln(1.1) = +0.095 | neutral |

Step 3 is the instructive one twice over: the SAME displacement evidence (100, 300) is
re-bucketed under a different partition than the one it was scored under at step 2,
and a zone hit scores only mildly positive because the book has him at 44% vs a 40%
width-share - the `p_pred` vs `p0` distinction working as designed.

---

## Stage 3 - utils.py: the backtest walk (now weighted)

### `obp_recency_walk(sw, value_col, kind, n_window, ranges, rel_params, alpha=1.0, maximize=True) -> list[dict | None]`

One function serving both the aggregate light and the Inspector detail - a deliberate
simplification vs. the existing `_surprisal_walk` / `_surprisal_walk_detail` fork (the
expensive part is shared, a stripped twin saves nothing). Takes the `_recency_frame`
frame `sw` (not bare vals - it needs the `result`/`obc`/`outs` columns for weighting),
`kind` in `{"pitch", "delta", "delta2"}`, `n_window` (= `window_n` = `n_pitches`),
TODAY's live `ranges`, and `rel_params` = the seven Relevance Weighting inputs
`(recency_slider, result_slider, state_slider, g1, g2, g3, result_offset)`.

Output: one entry per row, aligned like `_surprisal_walk`'s output - `None` for
ineligible steps, else `{"best_val", "zone_lo", "zone_hi", "W", "k", "obs", "p_obs",
"p0_obs", "score"}`.

Per-step population (Decision C):

- `w_t = vals[t - n_window : t]` - strictly-before-t, no lookahead by construction.
- **Eligibility (recommendation - flag as a judgment call):** full window
  (`t >= n_window`) plus the projector minimum (`"delta"`: len >= 2, `"delta2"`:
  len >= 3). Strict full windows keep every step statistically comparable to today's
  live computation and sacrifice only the first `n_pitches` career pitches. The
  spec's "too early to form a full window -> no score" wording supports strict.
- population: `w_t` for `"pitch"`; `project_from_deltas(w_t)` / `project_from_delta2s(w_t)`
  otherwise. Both just read the list and anchor on its last element - confirmed safe
  on sliding windows, O(len), stateless. They take deltas across game boundaries
  inside the window - identical to the live rows (fact #2).

### Weighted populations - the revision-1 simplification is withdrawn. Cost analysis:

The live rows weight the population via `compute_pa_weights` with the current
Relevance sliders and the current obc/outs. The point-in-time-honest backtest
equivalent: at each step `t`, weight the window with the CURRENT slider values
(live UI state, constant across steps) but THAT STEP's own obc/outs as the "current"
state - i.e. `sw["obc"][t]` / `sw["outs"][t]`, the base/out state the pitch at `t`
was actually thrown into (known before the pitch: no lookahead).

**Naive cost (do NOT do this):** one `compute_pa_weights` call per eligible step -
weights are signal-independent (the live page computes `_h_wts` once and derives
`_h_dwts = wts[1:]`, `_h_d2wts` = each of `wts[2:]` duplicated x2 - mirror that), so
T calls, not 3T. Per call: `sort_values` (~150-300 us) + two Python loops of
`n_window` scalar `.iloc` accesses (~10-20 us each). For T = 3,000, `n_window` = 100:
3,000 x (200 x ~15 us + ~250 us) = 3,000 x ~3.3 ms = **~10 s**. Even at
`n_window` = 20 it is ~2.5-3 s. That is 10-30x the FFT budget and WOULD have flipped
Stage 6's "no striding needed" conclusion - which is exactly why revision 1 punted on
weights.

**Vectorized cost (do this):** every factor decomposes into per-pitch quantities
precomputable ONCE per pitcher, O(T), before the step loop:

- `recency_w`: a pure function of window LENGTH and the recency slider
  (`exp(tr * (2*linspace(0,1,n) - 1) * 1.151)`). With strict full windows, n is
  constant -> ONE precomputed `(n_window,)` vector reused at every step.
- `result_w`: per-pitch batting quality `q[i]` via
  `sw["result"].map(_BATTING_QUALITY).fillna(0.5)` (vectorized string map), then
  `rw[i] = exp(ts * (2*q[i] - 1) * 1.151)` - one `(T,)` array. Per window: a slice.
  **Fidelity trap:** `result_offset=True` uses `src_i = i - 1` WITHIN the window,
  with the window's FIRST element getting neutral `q = 0.5` -> weight `exp(0) = 1.0`
  (`utils.py:2087-2089`) - NOT the global predecessor's quality. So the vectorized
  version slices the globally shifted array and then overwrites element 0 of each
  window slice with 1.0. Replicate exactly or the equality test in Stage 8 fails.
- `state_w`: obc has 8 values x outs has 3 -> 24 distinct states. Precompute a
  per-pitch state code `c[i]` in 0..23 (with the same NaN defaults as
  `compute_pa_weights`: obc "000", outs 0) and a 24x24 similarity table
  `SIM[a][b] = 0.5*obc_sim + 0.5*outs_sim`; per step:
  `sw_state = exp(te * (2*SIM[c[window], c[t]] - 1) * 1.151)` - a vectorized gather.
- Per step: `_norm01` each factor over the window slice, combine with gn1/gn2/gn3,
  mean-normalize - O(n_window) numpy, exactly mirroring `utils.py:2107-2117`
  including the degenerate `wmax == wmin -> 0.5` branch and the `mean_c > 0` guard.

Total: T x O(n_window) vectorized ops = ~300k element ops for T=3,000, n=100 ->
**tens of milliseconds**. The weighted backtest costs roughly nothing on top of the
FFT once vectorized. `compute_pa_weights` itself stays untouched (live path); the
backtest gets a private `_pa_weights_point_in_time(...)` helper, with a Stage 8 test
asserting per-step equality against real `compute_pa_weights` calls.

The FFT weight matrix rows then become weighted deposits
(`np.add.at(row, pop_idx, w_variant)`) instead of unit counts; argmax is invariant to
the per-window normalization so nothing else changes. The Stage 2d evidence update
stays UNWEIGHTED (`disp_hist[u_t] += 1`): weights shape the per-step
population/best_val (what the recommendation would have been), not the evidentiary
weight of what the pitcher then actually threw - each observed pitch is one real
trial. Keep `disp_hist` float anyway for future flexibility, and state this
weights-affect-the-question-not-the-evidence line in the docstring.

### Wrappers mirroring the existing shapes

- `obp_recency_states(df, value_col, window_n, ranges, rel_params) -> dict` - for
  each of the three signals: `_recency_frame` -> walk -> `_aggregate_recency([r["score"]
  if r else None ...], window_n, k)`. Same return shape as `scouting_recency_states`,
  merged on the page with `dict.update`. (Still NOT bolted onto
  `scouting_recency_states` - different cache invalidation profile, Stage 5.)
- `obp_recency_detail(df, value_col, signal, window_n, ranges, rel_params) -> dict` -
  same return shape as `scouting_recency_detail` (`rows, scores, n_scored, avg, rel,
  state, votes, window_n, k`) so `_stoplight_inspector`, `scouting_score_histogram`,
  and the linechart work unmodified. Each row carries the standard keys plus `p0`
  (that row's `p0_t[obs]`), `ratio` (`p_obs/p0` = `exp(score)`), `zone_lo`, `zone_hi`,
  `W`. Top-level `k`: report the LAST eligible step's `k_t` (it varies now; it is
  display-only - fact #6 - so document the choice).
  - Labels built INLINE (the static `_recency_labelers` table cannot express per-step
    geometry; it stays untouched). `ctx_label = f"zone {zone_lo}-{zone_hi} (W={W})"`;
    `obs_label = "in zone"` for index 0, else `f"out+{obs} ({arc_lo}-{arc_hi})"` with
    the observed bucket's absolute arc at that step (wrap-aware).

---

## Stage 4 - utils.py: `scouting_recency_linechart` ratio mode (Decision D)

Unchanged from revision 1. **Scope call: one function, auto-selected mode - not a
uniform relabel, not a fork.** If the detail rows carry a `"p0"` key (only the OBP
signals'), plot `ratio = p_obs / p0`; otherwise take the exact existing `p_obs * 100`
path, byte-identical for every existing signal. The "apply uniformly" option is NOT a
pure relabeling - y values and hover text ("P=30.0%" -> "1.50x") would visibly change
for all eleven existing signals.

Ratio branch: baseline hline at `1.0`; bands `exp(scouting_min)` (~1.116x) /
`exp(anti_max)` (~0.869x) - constants again, as Decision D promises (note this is
cleanly true even with per-step `k_t`/`p0_t`, which is part of why the ratio axis is
the right unit); y-title `"P(bucket) / baseline"`; same padding + moving-average
logic on the ratio series; hover surfaces `ratio`, `p_obs` %, `p0` %, `obs_label`,
plus the existing pitch/swing/diff/game customdata. The stoplight itself is untouched -
`cls`/votes/state come from `score` upstream of the chart.

---

## Stage 5 - pages/2_Scouting.py: wiring

### 5a. Constants and loaders (near line 258-273)

- `_OBP_STOPLIGHT_SIGNALS = ["OBP recent pitch", "OBP recent Δ", "OBP recent Δ²"]`;
  prepend to `_STOPLIGHT_ORDER` (the OBP rows are pinned at the top of Suggestions).
- Two new cached loaders (separate from the sequence ones ON PURPOSE - the sequence
  caches must survive matchup changes; these can't):

```
@st.cache_data(ttl=3600)
def _load_pitcher_obp_stoplights(pitcher_name, leagues, data_v, window_n,
                                 ranges_key: tuple, rel_key: tuple, sig: tuple) -> dict
@st.cache_data(ttl=3600)
def _load_pitcher_obp_stoplight_detail(pitcher_name, leagues, data_v, signal, window_n,
                                       ranges_key: tuple, rel_key: tuple, sig: tuple) -> dict
```

Both call `_load_pitcher_plays(...)` then the Stage 3 wrappers.
`ranges_key = tuple(tuple(r) for r in active_ranges)`;
`rel_key = (recency, result, state, g1, g2, g3, result_offset)` read from the same
session-state keys the live block reads (`pages/2_Scouting.py:1492-1499`);
`sig = utils.scouting_cache_sig()`. `window_n` is `n_pitches` (fact #7).
No `zone_specs_key` - regrowth means today's zone shape is no longer an input, which
also makes the cache MORE stable than revision 1's (it no longer busts when new
pitches move today's zone; `data_v` covers new data). The cost of including
`rel_key`: dragging a Relevance slider cold-recomputes the OBP lights (~1s worst
case, Stage 6) while the sequence lights stay cached - acceptable for an
expander-tucked control, and it is the price of point-in-time-honest weighting.

### 5b. Gating (in the OBP row block, ~1486-1526)

No zone-spec plumbing anymore. Run the OBP stoplights whenever a matchup is loaded
(`result_ranges`) and `active_ranges` has an OBR band (`obr_max > 0` - reuse the same
check `obp_zone_signal` performs; if it fails, the walk returns all-None and the
signals show white dots with n=0 via `states.get(_s, {})`). Signals whose live row
dict is None still render their Suggestions row without a dot, and the Inspector
selectbox omits n_scored=0 signals - no extra empty-state code.

### 5c. Merging states (stoplight block at ~1704-1729)

Inside `if tab_p_pitcher != "All":`, after the existing `_stop_states =
_load_pitcher_stoplights(...)`: if `result_ranges`,
`_stop_states.update(_load_pitcher_obp_stoplights(...))`.

Then **delete the `if _sig.startswith("OBP"): continue` skip at line 1714**. The
standard path (`_key = _sig`; attach `_r["_stoplight"]`; append to `_order_seen`) now
handles OBP rows - names match exactly and they are in `_STOPLIGHT_ORDER`.
`hint_bars_figure`'s dot rendering is generic over `_stoplight` - zero figure changes.

### 5d. Inspector (`_stoplight_inspector`, line 275+)

- Add parameters carrying `ranges_key` and `rel_key`; pass from the call site.
- At the detail load (line 302): `if _sel in _OBP_STOPLIGHT_SIGNALS:` -> the OBP
  detail loader, else the existing call.
- Caption fix (line 309): `_base = 100/k` is wrong framing for unequal, per-step
  buckets. For OBP signals say `"width-proportional baseline (k varies per step)"`
  and include one line of the Stage 2b nuance ("measures displacement behavior vs
  each day's recommended zone").
- **Drill-down table (Q3):** keep existing columns (Context = that step's zone with
  W, Observed = in-zone / out+N with arc, Prob % = raw `p_obs` - still surfaced per
  Decision D). Add `"Base %"` (`p0 * 100`) and `"Ratio"` (`"{:.2f}x"`) only when rows
  carry them (`"p0" in _r`), so existing signals' table is unchanged.

---

## Stage 6 - performance & caching (Q1)

- **Still no sampling/striding - but only because of two specific vectorizations.**
  Per signal, for a 3,000-pitch career: batched `(T, 1000)` rFFT/irFFT + argmax
  (tens of ms); batched zone regrowth via `(T, obr_max)` boolean gathers (single-digit
  ms, Stage 1); vectorized point-in-time weights (tens of ms once per walk - shared
  across the three signals if the walk is refactored to accept precomputed weights,
  or recomputed thrice, still cheap); displacement prefix sums O(T x 1000) (~3M ops,
  ms). All three signals comfortably under ~1s cold.
- The naive alternatives that would have broken this: per-step `compute_pa_weights`
  calls (~10 s at n_window=100 - the Stage 3 analysis) and a per-step Python grow
  loop (~1-2M interpreted iterations). Neither is acceptable; both are designed out,
  not sampled around.
- Caching at the page loaders (5a): pitcher/leagues/data_v + `window_n` + `ranges_key`
  + `rel_key` + `scouting_cache_sig()`. Fallback if profiling ever contradicts the
  estimate (>20k-pitch frame): stride `best_val`/shape recomputation every 5 steps -
  do not build speculatively.

---

## Stage 7 - threshold calibration (Q2) - do this BEFORE trusting the lights

Extend `scripts/calibrate_stoplight_thresholds.py` (CSV-driven from
`plays_from_pitchers_200+_bf.csv`, repo root - no DB needed) with an `--obp` mode:

- No matchup ranges in the CSV, so use a synthetic-but-representative kernel:
  `ranges = [("1B", 0, X)]` (verify `"1B"` in `_OBR`; `_diff_score_array(..., "obp")`
  only tests OBR membership). Grid over the band width X in {50, 100, 150} - X sets
  `obr_max`, and with regrowth the zone shapes now EMERGE per step instead of being a
  fixed input, so the old fixed-W grid is gone. Use neutral `rel_params` (all sliders
  50/state 0 -> uniform weights) for the baseline run; optionally one non-neutral run
  to confirm weighting does not skew the score distribution.
- Report, per kind x X: score percentiles, the scouting/neutral/anti shares under
  current `SCOUT_PP_THRESHOLDS` (side by side with an existing equal-width signal
  from the same script run), AND the empirical `W_t` / `k_t` distributions - this is
  the Q-cap evidence for `OBP_MAX_OTHER_BUCKETS = 19`.
- **Acceptance criterion (proposal - confirm with Alex):** each class >= ~15% overall,
  no class > 70%, lights vary across pitchers. If badly skewed, do NOT retune the
  shared constants; add a per-signal threshold override consulted only for OBP
  signals - and only if the data demands it.
- Caveats to record: synthetic kernel is a proxy; spot-check in-app on 2-3 real
  pitchers via the existing `scouting_score_histogram`. And the behavioral flag from
  revision 1 stands: single always-on context -> `p_pred` converges over a career ->
  stickier lights than the sequence signals. Property, not bug; eyeball it.

---

## Stage 8 - validation

1. **Geometry unit checks**: `obp_bucket_widths` reproduces the spec examples
   (W=328 -> [328, 336, 336]; W=270 -> [270, 244, 243, 243]); widths sum to 1000;
   W=1000 -> None; cap behavior at W=982+ sane. Every u in 0..999 maps to exactly one
   bucket for a few random (left, right) shapes.
2. **Toy-example asserts**: encode the Stage 2e table verbatim (three steps, varying
   shape, displacement re-bucketing); scores match to 1e-9.
3. **Grow-rule equality**: for a sample of real steps, the batched Stage 1
   (left_t, right_t) must equal a direct per-step transcription of
   `obp_zone_signal`'s grow loop on the same score row - including the flat-curve
   fallback and the obr_max cap.
4. **Weights equality**: for a sample of steps, `_pa_weights_point_in_time`'s window
   weights must exactly match `compute_pa_weights(window_df, obc_t, outs_t,
   **rel_params)` - this is the guard on the result_offset first-element trap and the
   `_norm01` degenerate branch.
5. **Equal-width regression**: after generalizing `_score_from_probs`, rerun the
   calibration script and diff per-signal score percentiles against a pre-change run -
   identical.
6. **Point-in-time safety**: prefix property - `obp_recency_walk` on `sw.iloc[:m]`
   equals the first m entries of the full run, for several m (catches whole-history
   normalization, weight lookahead, or windowing off-by-ones).
7. **Null-invariance smoke test** (new, enabled by the displacement design): feed
   synthetic uniform-random pitches through the walk - scores should hover near 0 and
   the light should be neutral/none. This directly tests the Stage 2b claim that
   regrowth + displacement pooling keeps the null unbiased even as W_t wanders.
8. **In-app**: matchup loaded for a long-history pitcher -> three Inspector rows with
   lights; dots on the OBP Suggestions rows; ratio-axis trend chart centered on 1.0
   with ~0.87x/1.12x bands; drill-down gains Base %/Ratio for OBP signals only;
   existing signals pixel-identical; no matchup -> white dots, no crash. Time the
   cold loader on the longest-history pitcher (expect < ~1s).
9. **Cache-key check**: change the swing type (changes `active_ranges`) -> OBP lights
   recompute, sequence lights hit cache; drag a Relevance slider -> OBP lights
   recompute (~1s, expected per 5a), sequence lights hit cache; new pitch data
   (`data_v`) -> both recompute.

---

## Follow-as-is vs. simplify - explicit calls

Follow the spec as written: the bucket-count formula (2a), the `alpha*k*p0` smoothing
and `ln(p_pred/p0)` score (2d), generalizing `_score_from_probs` in place (2c),
ratio-axis trend chart (Stage 4), reusing `_aggregate_recency`/`MIN_SCORED`/
`SCOUT_PP_THRESHOLDS` pending Stage 7.

Revised per Alex's direction (this revision): per-step zone regrowth instead of
pinning today's (left, right) - with the displacement-space evidence store as the
mechanism that keeps it statistically sound (2b); weighted backtest populations via a
vectorized `compute_pa_weights` equivalent instead of the unweighted simplification
(Stage 3) - the cost objection dissolves under precomputation (~10 s naive -> tens of
ms), leaving only the rel_key cache-churn tradeoff, accepted in 5a.

Simplifications still recommended: single walk function, no walk/detail fork
(Stage 3); hybrid auto-mode linechart (Stage 4); separate page loaders rather than
extending `scouting_recency_states` (5a).

Interpretation calls Opus should keep unless Alex objects: `W_t = left_t + right_t + 1`
(true covered fraction, Stage 1); strict full-window eligibility (Stage 3);
round-half-up for `n_other` (2a); `OBP_MAX_OTHER_BUCKETS = 19` pending Stage 7's
empirical W_t/k_t distributions; evidence updates unweighted (weights shape the
recommendation, not the evidence - Stage 3); detail `k` = last step's `k_t` (Stage 3).

## Open questions (explicit, for Opus/Alex)

- **Q-cap**: `OBP_MAX_OTHER_BUCKETS = 19` - now checkable directly from Stage 7's
  emitted `W_t`/`k_t` distributions instead of ad-hoc logging; confirm the cap is
  nearly dead code before freezing.
- **Q-thresholds (Q2)**: reuse `SCOUT_PP_THRESHOLDS` only if Stage 7's class shares
  look healthy; per-signal overrides are the fallback, never retuning the shared
  constants.
- **Q-slider-churn**: `rel_key` in the cache key means Relevance slider drags
  cold-recompute the OBP lights (~1s). Accepted in 5a; if it annoys in practice, the
  escape hatch is pinning the backtest to neutral rel_params (constant key) - a
  one-line change that trades honesty for stability. Alex's call after feeling it.
- **Q-history scope (inherited)**: like the existing stoplights, the backtest scores
  the FULL real-league history - ignoring season/team/source filters, scrimmages, and
  hist-mode truncation, even though the live OBP zones come from the
  filtered/truncated frame. Inherited inconsistency, flagged not fixed.
- **Q-eligibility**: strict full-window (recommended) vs projector-minimum - cheap to
  flip inside `obp_recency_walk` if Alex prefers scoring early-career pitches.

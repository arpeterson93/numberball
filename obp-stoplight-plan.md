# OBP recent X stoplight - implementation plan v2 (supersedes v1 entirely)

Pitcher-side only. The v1 design (variable OBR-derived zone width, per-step regrowth,
displacement re-anchoring, ratio-axis chart) SHIPPED and is in the working tree - v2
is therefore a rework of live code, not a green-field build. This plan specifies both
what to build and what to delete. Signals stay `"OBP recent pitch"`, `"OBP recent Δ"`,
`"OBP recent Δ²"`.

Project constraints for every stage:
- **No em dashes in any `.py` file** - hyphens only. No subscript glyphs; write `p0`.
- No Co-Authored-By trailer if committing.

## Current-tree facts (verified 2026-07-17)

1. **v1 is fully landed**: `obp_bucket_widths`/`OBP_MAX_OTHER_BUCKETS` (`utils.py:4179`,
   `3747`), `obp_recency_walk` (`utils.py:4307` - batched FFT, zone regrowth,
   displacement histogram), `obp_recency_states`/`obp_recency_detail`
   (`utils.py:4466`/`4481`), `_obp_weight_factors`/`_obp_window_weights`/
   `_pa_weights_point_in_time` (`utils.py:4200-4304`), ratio mode inside
   `scouting_recency_linechart` (`utils.py:4594-4636`), page loaders
   `_load_pitcher_obp_stoplights`/`_load_pitcher_obp_stoplight_detail`
   (`pages/2_Scouting.py:277-288`), `_STOPLIGHT_ORDER` prepend (page:271-274), the
   inspector OBP branch + caption + Base %/Ratio table columns (page:318-385), and the
   states merge (page:1757-1761).
2. **Slider invariant that makes Decision 1 clean**: every option of all three sliders
   divides its domain evenly - `hz_follow_bucket_p` options `[50,100,125,200,250,500]`
   all divide 1000; `dd_bucket_p`/`dd2_bucket_p` options `[25,50,100,125,250,500]` all
   divide 500. So the standard bucket count `k = domain // width` is exact and the
   chart's existing `1/k` baseline IS the standard-width baseline - Decision 8 needs
   almost no chart work (Stage 4).
3. **Signed Δ² exists**: `pitch_circ_delta2_signed` (`utils.py:984, 1004-1009`);
   `circular_signed_delta` at `utils.py:670`. `project_from_deltas`/
   `project_from_delta2s` (`utils.py:2008-2033`) anchor on the window's last element
   and cross game boundaries - the projections mirror the live rows.
4. **Q1 is ANSWERED, empirically** (benchmark run on this machine, synthetic
   3,000-pitch career, real code from the tree):
   - Naive per-step `compute_pa_weights`: **2.29 ms/step at n_window=20 -> 6.8 s
     total; 9.01 ms/step at n_window=100 -> 26.1 s total.** (Two Python `.iloc` loops
     + a `sort_values` per call; ~2 x n_window x 3,000 interpreted iterations.)
   - The v1 vectorized path (`_obp_weight_factors` once + `_obp_window_weights` per
     step): **131 ms (n=20) / 151 ms (n=100) total**, and the two paths agree to
     **max abs diff 0.0** (bitwise).
   - Conclusion: Decision 6 is finalized as "keep the v1 vectorized helpers verbatim"
     - they are unaffected by the v2 redesign, already point-in-time honest (each
     step's own obc/outs via the 24-state code array), already prefix-safe (all
     per-pitch factor arrays use only same-row or previous-row data). No striding, no
     batching changes, nothing to decide.
5. **Decision 10's listed targets** and their exact mask sites:
   `seq2_delta_hint` (`utils.py:3298`), `seq3_delta_hint` (3351-3352, two masks),
   `seq2_delta2_hint` (3409), `seq3_delta2_hint` (3466-3467, two masks),
   `sequence_matches` delta/delta2 branch (2766 and the `prior_val2` mask at 2772).
   A full `<= half` sweep also found the same truncating pattern in FOUR more
   delta-domain helpers and THREE diff-domain helpers - see Stage 6 for the
   include/flag split.
6. `scripts/calibrate_stoplight_thresholds.py` imports `OBP_MAX_OTHER_BUCKETS`
   (lines 33, 175-177) and reports v1 `W_t`/`k_t` cap stats - it breaks if the
   constant is deleted without updating it (Stage 8).
7. `_aggregate_recency(scores, window_n, k)` still ignores `k`;
   `_score_from_probs(p, observed, p0=None)` (`utils.py:3759`) already has the
   generalized form - Decision 7 confirmed, no changes needed to either, nor to
   `SCOUT_PP_THRESHOLDS`/`_classify_pp`/`MIN_SCORED`.

---

## Stage 1 - utils.py: shared boundary-shift primitive + the two partition builders

### 1a. `_shift_to_domain(lo, hi, dlo, dhi) -> (lo, hi)`

THE single shared implementation of widen-and-shift (Decision 10's "one
implementation... not four+ copies"): if the requested interval underruns `dlo`,
slide it up to `[dlo, dlo + (hi - lo)]`; if it overruns `dhi`, slide down to
`[dhi - (hi - lo), dhi]`; if it exceeds the whole domain, clamp to `[dlo, dhi]`.
Width (hi - lo) is preserved in every non-degenerate case. Used by: the stoplight's
recommended bucket (1c), and every Stage 6 retrofit site (via 1d).

### 1b. `obp_pitch_partition(best_val, width) -> list of (lo, hi, label)`

Circular domain, width | 1000 (fact #2), so k = 1000 // width equal buckets, zero
edge cases:
- Recommended arc: `lo = best_val - width // 2`, covering exactly `width` integers
  (for even widths the center sits at position width // 2 - left-of-center by one;
  state the convention in the docstring). Wraps the 1/1000 seam via mod, like the Hot
  Zone Matrix.
- Tile `out+1 .. out+(k-1)` ascending (the one consistent rotational direction;
  matches the v1 detail rows' existing `out+N` labeling).
- All buckets exactly `width` wide -> `p0[i] = width / 1000 = 1/k` for every bucket.
  For the Pitch signal the machinery is therefore EXACTLY the existing equal-width
  categorical mechanism.

### 1c. `obp_bounded_partition(center, width, domain=500) -> list of (lo, hi, label)`

Delta/Δ² domain, integers 0..500. Conventions to pin explicitly (the prompt's worked
example mixes `[ )` and `[ ]` - define them once):
- Buckets are half-open `[lo, hi)` except the highest bucket, closed `[lo, 500]`.
  Every full bucket covers exactly `width` integers; the whole partition covers all
  501 integers exactly once. `p0[i] = (integer count of bucket i) / 501` - sums to
  exactly 1; a standard bucket's p0 is width/501, within 0.2% of 1/k, and the true
  per-bucket p0 is always disclosed per Decision 9, so the approximation is visible,
  never hidden.
- Recommended bucket: naive `[center - width//2, center - width//2 + width)`; apply
  `_shift_to_domain` -> hugs 0 or 500 at full width (widen-and-shift, never
  truncate).
- Low side: `R_lo = rec_lo - 0`; tile `R_lo // width` FULL buckets downward from
  `rec_lo` (`out-1` adjacent to rec, then `out-2`, ...); if `R_lo % width > 0`, ONE
  outermost leftover bucket `[0, R_lo % width)`. High side mirrored upward from
  `rec_hi` with the outermost leftover ending at 500.
- Unit-test against the prompt's worked examples verbatim:
  - `center=100, width=100`: rec `[50,150)`; low: one leftover `out-1=[0,50)`; high:
    `out+1=[150,250)`, `out+2=[250,350)`, `out+3=[350,450)`, `out+4=[450,500]`
    (leftover, 51 integers). k=6.
  - `center=25, width=100` (the shift case): naive `[-25,75)` -> rec `[0,100)`; no
    low side; high: `out+1..out+4` = `[100,200) [200,300) [300,400) [400,500]`. k=5.
  - Assert: integer counts sum to 501 (or 1000 for 1b), at most 2 non-standard
    buckets, never the recommended one, never an interior one.
- Note k varies by construction between `500//width` and `500//width + 1` depending
  on alignment - bounded, tiny, and the per-row p0 disclosure (Decision 9) covers it.

### 1d. `_centered_match_interval(center, bucket_size, domain_hi=500) -> (lo, hi)`

The retrofit-facing wrapper for Stage 6: existing shipped semantics are
`|v - center| <= half` with `half = bucket_size // 2` - an inclusive interval
spanning `2*half + 1` integers. Keep that span, apply `_shift_to_domain(center-half,
center+half, 0, domain_hi)`, return inclusive `(lo, hi)`; call sites replace the
distance mask with `series.between(lo, hi)`. (When `2*half >= domain_hi` - the
500-wide slider - the interval is the whole domain, unchanged behavior.)

### 1e. Deletions

Remove `obp_bucket_widths` and `OBP_MAX_OTHER_BUCKETS` (only callers: the v1 walk and
the calibration script - fact #6; update both). The natural-bound argument replaces
the cap: worst case is `dd_bucket=25` -> ~21 buckets, `hz_follow=50` -> 20.

---

## Stage 2 - utils.py: rewrite `obp_recency_walk` (same name/shape, new internals)

Signature: `obp_recency_walk(sw, value_col, kind, n_window, ranges, rel_params,
bucket_width, alpha=1.0, maximize=True)` - one new parameter, the Decision-1 width
for this kind. Output stays one entry per row (None = ineligible), so
states/detail/aggregation plumbing is undisturbed.

### Kept from v1 (verbatim)

- Eligibility skeleton: strict full window, `elig = range(n_window, T)`.
- The weight machinery: `_obp_weight_factors` once, `_obp_window_weights` per step,
  population variants (`pitch`: wts; `delta`: `wts[1:]`; `delta2`:
  `np.repeat(wts[2:], 2)`) - fact #4 closes Q1 in its favor.
- The batched FFT: `Wmat` rows via `_build_weight_array(pop, wv)`, one
  `fft/ifft` over the stack, kernel from today's live `ranges`. One simplification:
  the v1 comments about needing bit-identical transforms to match `np.argmax`'s
  lowest-index tie pick are OBSOLETE - Decision 4 replaces implicit argmax
  tie-breaking with an explicit rule (below), which absorbs epsilon-level FFT
  differences by construction. Rewrite those comments.

### Deleted from v1

The entire zone-regrowth block (mid/above/run-length gathers, `left_t`/`right_t`/
`W_arr`), the displacement re-anchoring (`u = (val - bval) % 1000`), the
`widths_cache` over variable W, and the `zone_lo`/`zone_hi`-from-grown-arms output.

### New per-step logic

Precompute once from `vals = sw[value_col]` and `game = sw["game_id"]` (mirror
`_recency_indications`' inline recompute discipline rather than trusting the
precomputed delta columns - same `same1`/`same2` masks, same reasoning):
- `same1[t]` (game[t] == game[t-1]), `same2[t]` (t-2, t-1, t one game).
- Real signed delta into t: `sd[t] = circular_signed_delta(vals[t-1], vals[t])` where
  `same1[t]`, else undefined. Real `|Δ|[t] = |sd[t]|`; real
  `|Δ²|[t] = ||sd[t]| - |sd[t-1]||` where `same2[t]`.

At each eligible step t (window `w_t = vals[t-n_window : t]`, weights from factors):

1. **best_val_t with Decision-4 tie-breaking.** Tie set = `flatnonzero(S[r] >=
   S[r].max() - 1e-9)` (the OBP kernel is a flat-topped box, so plateau ties are the
   NORM, not an edge case - hundreds of tied indices per row are expected). Pick:
   - `pitch`: candidate minimizing circular distance to `vals[t-1]`.
   - `delta`: candidate minimizing `|circular_signed_delta(vals[t-1], cand) - sd[t-1]|`
     (numeric distance in signed implied-delta space to the last REAL signed delta,
     which needs `same1[t-1]`).
   - `delta2`: candidate minimizing `|implied_d2(cand) - real |Δ²|[t-1]|` where
     `implied_d2(cand) = ||circular_signed_delta(vals[t-1], cand)| - |sd[t-1]||`
     (needs the real Δ² at t-1, i.e. `same2[t-1]`).
   - Fallback when the reference is undefined (game boundary): lowest index
     (documented; matches old np.argmax behavior). Secondary tie -> lowest index.
   A small Python loop over rows is fine (numpy ops per row; ms scale).
2. **Partition + outcome per kind:**
   - `pitch`: partition = `obp_pitch_partition(best_val_t, bucket_width)`; outcome =
     real `vals[t]`. Eligible whenever the window is full.
   - `delta`: `implied_delta_t = circular_signed_delta(vals[t-1], best_val_t)`;
     partition = `obp_bounded_partition(abs(implied_delta_t), bucket_width)`; outcome
     = real `|Δ|[t]` - requires `same1[t]`, else the step is None.
   - `delta2`: `implied_delta2_t = ||implied_delta_t| - |sd[t-1]||` (Decision 2's
     formula; needs `same1[t-1]` for `sd[t-1]`); partition =
     `obp_bounded_partition(implied_delta2_t, bucket_width)`; outcome = real
     `|Δ²|[t]` - requires `same2[t]`, else None.
3. **Evidence + scoring - real historical values, re-bucketed per step.** Keep ONE
   point-in-time histogram per walk over the ABSOLUTE domain (1..1000 for pitch,
   0..500 for delta/Δ²), holding the real outcomes scored so far. At step t:
   `D_t[i]` = histogram mass inside bucket i (prefix-sum interval sums, the same code
   shape as the v1 walk's bucket sums);
   `p_pred = (D_t + alpha * k_t * p0_t) / (total + alpha * k_t)`;
   `score = _score_from_probs(p_pred, obs, p0_t)`; then add the real outcome to the
   histogram AFTER scoring. This is the "how exactly" call this plan makes (the
   prompt leaves evidence bookkeeping unstated): absolute-domain bucketing is chosen
   over relative-index (`out+N`) count pooling because (a) it is the literal reading
   of Decision 2 - the bucket is "tested against the pitcher's REAL historical
   delta sequence"; (b) it needs no slot-identity bookkeeping across steps whose
   partitions sit at different alignments; (c) for the Pitch signal it reduces
   EXACTLY to `_surprisal_walk`'s single-context equal-width arithmetic (all buckets
   width/1000 = 1/k), which is the "plain equal-width categorical prediction"
   Decision 1 promises; (d) it reuses the v1 walk's already-shipped prefix-sum code.
   Flag for Alex only if disagreeing with (a)-(d); this is an implementation detail,
   not a re-litigated decision.
4. **Output row keys** (consumed by `obp_recency_detail`): `best_val`,
   `implied` (None for pitch; the implied |Δ| or |Δ²| otherwise), `rec_lo`, `rec_hi`,
   `bucket_w` (the observed bucket's true integer width), `k`, `obs` (signed relative
   label index, 0 = recommended, +N above, -N below), `arc_lo`, `arc_hi` (observed
   bucket's absolute range), `p_obs`, `p0_obs`, `score`.

Runtime budget (per signal, 3,000-pitch career): FFT batch tens of ms + weights
~150 ms (fact #4) + tie-break loop + per-step prefix sums over <=1001 bins (~3M ops)
tens of ms -> comfortably under half a second; all three signals under ~1s cold. No
sampling/striding - Q1's answer stands.

---

## Stage 3 - utils.py: `obp_recency_states` / `obp_recency_detail` updates

- Both gain the per-kind width: pass a mapping (e.g. `bucket_widths: dict` keyed
  `pitch`/`delta`/`delta2`, or a 3-tuple in `_OBP_SIGNAL_KINDS` order) down to the
  walk. `_OBP_SIGNAL_KINDS` (utils.py:4172) unchanged.
- `obp_recency_states`: unchanged otherwise (`_aggregate_recency(scores, window_n,
  1)`; k is ignored - fact #7).
- `obp_recency_detail`: top-level `k` becomes the CONSTANT standard bucket count
  `domain // bucket_width` (1000// for pitch, 500// for the others) - this is what
  makes the unmodified linechart's `1/k` baseline "calibrated to the STANDARD bucket
  width" per Decision 8 (fact #2: widths divide domains evenly, so 1/k is exact for
  standard buckets).
  - Row labels: `ctx_label = f"rec {rec_lo}-{rec_hi}"` for pitch;
    `f"implied {implied} -> rec {rec_lo}-{rec_hi}"` for delta/Δ². `obs_label` =
    `"in rec"` for obs 0, else `f"out{obs:+d} ({arc_lo}-{arc_hi})"` - signed now
    (bidirectional tiling below/above on the bounded domains; pitch keeps
    positive-only labels).
  - Rows keep `p0` and gain `bucket_w` (Decision 9's disclosure payload - the
    existing Base %/Ratio table columns key on `"p0" in rows[0]` and keep working).

---

## Stage 4 - utils.py: `scouting_recency_linechart` - remove ratio mode, extend hover

Per Decision 8, DELETE the `ratio_mode` branch outright (utils.py:4594-4636's
conditional structure collapses back to the single P% path - baseline `100/k`, bands
`exp(cutoff)/k*100`, y-title "P(bucket) %"). No dormant ratio code remains.

Per Decision 9, the hover must disclose the observed bucket's true width/p0 for
every point of the new signals: when rows carry `p0` (only the OBP signals'), extend
the customdata with `bucket_w`, `p0*100`, and `obs_label`, and append one hover line
like `bucket {arc_lo}-{arc_hi} (w={bucket_w}) · base {p0:.1f}%`. Rows without `p0`
(all eleven existing signals) keep the byte-identical existing customdata and hover.
This is the ONLY conditional left in the chart, and it is display-payload only - the
axis, baseline, bands, MA, and layout are single-path again.

Page caption (`pages/2_Scouting.py:333-335`): replace the v1 OBP wording
("width-proportional baseline (k varies per step)...") with
`f"standard-width baseline {100/k:.0f}% (k={k}); edge-bucket points show their true
base in hover/table"`.

---

## Stage 5 - pages/2_Scouting.py: threading the widths

- Loader signatures (page:277-288) gain `widths_key: tuple` (it doubles as the cache
  key): `(hz_follow, dd, dd2)`. Detail loader likewise. Both forward into the
  Stage 3 wrappers.
- Call sites: build once near the existing `_obp_rel_key` construction -
  `_obp_widths_key = (int(st.session_state.get("hz_follow_bucket_p", 200)),
  int(_h_dd_bkt), int(_h_dd2_bkt))` (the dd/dd2 values are already read at
  page:~1480-1482; hz_follow's default 200 matches the slider at page:2184 and the
  session-state-read-before-widget pattern already used at page:1880). Pass at the
  states merge (page:1758-1761) and into `_stoplight_inspector` (signature at
  page:290-291 gains `widths_key`, forwarded at the detail call, page:320-321).
- Consequence to note: dragging any of the three bucket sliders now busts the OBP
  stoplight cache (sub-second recompute per Stage 2's budget) - same accepted
  tradeoff as `rel_key`, and it is semantically REQUIRED (the width defines the
  signal).
- Everything else on the page (order, merge, dots, table) already works from v1.

---

## Stage 6 - the Decision 10 retrofit (shared `_centered_match_interval`)

Replace the truncating distance masks with the shifted-interval mask at the DECIDED
sites (fact #5):

1. `seq2_delta_hint` centered branch (utils.py:3298).
2. `seq3_delta_hint` centered branch - BOTH masks, `_d1` and `_d2` (3351-3352).
3. `seq2_delta2_hint` centered branch (3409).
4. `seq3_delta2_hint` centered branch - both masks (3466-3467).
5. `sequence_matches` `domain != "value"` branch - both the `prior_val` mask (2766)
   and the `prior_val2` mask (2772). The circular `domain == "value"` path is
   untouched (no edges).

**Recommended additions (flag to Alex, include unless vetoed):** the same pattern
lives in four more delta-domain helpers that back the SAME Suggestions rows' zone
distributions and "All zones" painting - `delta_next_zone_dist` (3713),
`delta3_next_zone_dist` (4672-4673), `delta_zone_via_delta_hist` (4766),
`delta3_zone_via_delta_hist` (4790-4791). Leaving them truncating while their
headline hints widen would make a boundary-adjacent "2-Δ seq" row compute its
probability from one population and paint its zone distribution from a different,
narrower one - an internal inconsistency the retrofit is supposed to eliminate.
These fall squarely under the prompt's "every existing delta/delta² distance-based
match in the app" even though its bullet list names only three.

**Open question (genuinely outside the wording):** three DIFF-domain matchers share
the pattern on the bounded 0..500 diff axis - `diff_to_delta_hint` (3518),
`diff_next_zone_dist` (4814), `diff_to_delta_zone_dist` (4845), backing the
"Prior diff → Δ" row. Diff is not delta/Δ², so this is not decided; recommend
including for one-convention-everywhere, but get Alex's yes/no rather than assuming.

Behavior-shift note for validation (Stage 9): Δ² contexts near 0 are the COMMON case
(|Δ²| clusters small), so expect visible match-count/probability shifts on the Δ²
surfaces especially - that is the intended outcome, not a regression.

---

## Stage 7 - Q1 resolution (formal)

Answered by measurement (fact #4): naive per-step `compute_pa_weights` costs 6.8 s
(n_window=20) to 26.1 s (n_window=100) per 3,000-pitch career and would dominate
everything; the landed vectorized equivalents cost 131-151 ms with bitwise-identical
output. Decision 6 therefore keeps the v1 weight helpers exactly as-is; the per-step
loops in `compute_pa_weights` itself stay untouched (live path only). The benchmark
script lives in the session scratchpad (`bench_weights.py`) - port its equality
assertion into the validation suite (Stage 9.5).

---

## Stage 8 - scripts/calibrate_stoplight_thresholds.py

- Remove the `OBP_MAX_OTHER_BUCKETS` import and the `W_t`/`k_t`-cap reporting
  (lines 33, 172-177) - both concepts die with v1.
- Point the `--obp` mode at the new walk signature: grid over the real slider option
  values per kind (e.g. pitch width in {100, 200}, delta/Δ² width in {50, 100}) with
  the synthetic single-band OBR kernel it already uses; report score percentiles and
  class shares under `SCOUT_PP_THRESHOLDS` per kind x width, side by side with an
  existing equal-width signal. Same acceptance framing as before (no class > 70%,
  each >= ~15%, lights vary across pitchers); per-signal threshold override remains
  the fallback, never retuning the shared constants.

---

## Stage 9 - validation

1. **Partition unit tests** (Stage 1): both prompt worked examples verbatim
   (center=100/width=100 -> the exact 6-bucket layout; center=25/width=100 -> shift
   to `[0,100)`); a high-edge shift case (center=490); sums to 501/1000 integers;
   <= 2 non-standard buckets, never rec/interior; every domain integer maps to
   exactly one bucket; pitch partition wraps the seam correctly (best_val=990,
   width=200).
2. **Reduction check** (Stage 2.3's claim): for `kind="pitch"`, feed the walk's
   observed bucket indices into `_surprisal_walk` with a constant context and assert
   the score sequences match exactly - proves the "plain equal-width categorical,
   same as existing signals" property end to end.
3. **Prefix invariance** (point-in-time safety): `obp_recency_walk(sw.iloc[:m], ...)`
   equals the first m entries of the full run for several m, per kind - catches
   window off-by-ones, weight lookahead, evidence-before-scoring bugs, and any
   accidental use of future data in tie-breaking references.
4. **Tie-break determinism**: construct a flat-plateau case (uniform window
   population) and assert the chosen best_val is the reference-nearest plateau
   member, not the lowest index; assert the documented fallback when the reference
   is undefined.
5. **Weights equality**: port the benchmark's assertion (naive `compute_pa_weights`
   vs `_pa_weights_point_in_time`, several steps, exact match) - guards the
   result_offset first-element and `_norm01` degenerate branches forever.
6. **Decision 10 before/after (NOT a no-regression check)**: for 2-3 real
   boundary-adjacent contexts, run each retrofitted function before and after and
   record the shift - e.g. `seq2_delta_hint(centered, prior=10, bucket=100)`: old
   effective match `[0,60]` vs new `[0,100]`; expect n to grow and prob/z to move.
   Same for `sequence_matches` on the Delta and Delta² tabs (match counts grow near
   the edges), and one Δ² case near 0 (the common case). Away from the edges
   (`half <= prior <= 500 - half`), assert results are IDENTICAL - the retrofit must
   be a strict boundary-only change.
7. **In-app**: long-history pitcher + loaded matchup -> three OBP rows with dots;
   Inspector chart shows raw P% with the standard-width baseline; hover on any point
   shows bucket range/width/base; table keeps Base %/Ratio; existing eleven signals
   byte-identical (chart AND table); no matchup -> white dots, no crash. Drag each
   of the three bucket sliders -> OBP lights recompute (sub-second), sequence lights
   stay cached; drag a Relevance slider -> same. Cold-load timing on the
   longest-history pitcher (expect < ~1s).
8. **Live-vs-backtest divergence disclosure** (not a bug, verify it reads sanely):
   the Suggestions row still draws `obp_zone_signal`'s OBR-grown zone; the
   Inspector's rec bucket is the slider-width bucket around a (possibly different,
   per Decision 4's tie rule) best_val. Confirm the Inspector labels make this
   legible (`rec {lo}-{hi}` vs the row's zone) rather than looking like a bug.

---

## Follow-as-is vs. interpretation calls

Decided by the prompt, built exactly as written: slider-tied fixed widths per row
(D1); real-outcome testing with implied delta/Δ² conversion (D2); centered partition
with widen-and-shift and floor-full-then-one-leftover tiling (D3, both worked
examples reproduced as tests); nearest-candidate tie-breaking (D4); strict window
timing (D5); point-in-time weighting (D6, shape settled by the Stage 7 measurement);
unchanged scoring machinery (D7); raw-P% chart with ratio mode deleted (D8); per-point
width/p0 disclosure (D9); the three-site + sequence_matches retrofit (D10).

Interpretation calls made here (keep unless Alex objects):
- **Evidence bookkeeping = absolute-domain histogram of real outcomes, re-bucketed
  per step** (Stage 2.3, with the four-point rationale). The alternative
  (relative-index pooling) is strictly more bookkeeping for less fidelity to D2.
- Bucket interval conventions: half-open with closed top; p0 = integer-count/501 on
  the delta domains; pitch center sits left-of-center by one for even widths
  (Stage 1).
- Tie tolerance 1e-9; lowest-index fallback when the tie reference is undefined
  (Stage 2.1).
- Detail `k` = constant standard count `domain // width` (Stage 3) - this is what
  lets Decision 8 use the chart unmodified.
- Retrofit scope: the four delta-domain zone-dist helpers are IN (recommended,
  flagged); the three diff-domain helpers are an explicit open question.

## Open questions

- **Q-diff-retrofit**: include `diff_to_delta_hint` / `diff_next_zone_dist` /
  `diff_to_delta_zone_dist` (diff domain, 0..500) in the widen-and-shift retrofit?
  Recommended yes for one-convention-everywhere; outside Decision 10's literal
  wording, so needs Alex's call.
- **Q-thresholds**: unchanged from before - reuse `SCOUT_PP_THRESHOLDS` if the
  Stage 8 class shares look healthy; per-signal override only if the data demands.
- **Q-tie-tolerance**: 1e-9 chosen for the plateau tie set; if calibration shows
  plateaus with genuine sub-1e-9 structure (unlikely with a 0/1 box kernel), revisit.

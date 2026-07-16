# Δ² (second-order delta) sequence feature - implementation plan

Pitcher-side only. Adds Δ² analogs of the existing first-order |Δ| tooling across three
surfaces: Pitch Analysis, Swing Suggestions (+ stoplight/Inspector), Sequence Viewer.
Do not touch the batter side (`df_b`, "Swing Analysis" expander, batter Suggestions).

Project constraints that apply to every stage:
- **No em dashes in any `.py` file** - use hyphens. ("Δ" and "²" are fine; the page
  already uses the literal string "OBP recent Δ²" at `pages/2_Scouting.py:1504`, so the
  "Δ²" glyph is established.)
- No Co-Authored-By trailer if committing.

## Naming (fixed for the whole feature - avoids the `_h_prior_delta2` collision)

`_h_prior_delta2` (`pages/2_Scouting.py:1455`) already means "the |Δ| from two pitches
ago". It stays untouched. Everything second-order uses a `d2sq` / `delta2` split:

| Thing | Name |
|---|---|
| utils heatmap fn | `next_delta2_vs_prior_delta2_heatmap` |
| utils third-dist fn | `delta2_third_dist` |
| utils hint fns | `seq2_delta2_hint`, `seq3_delta2_hint` |
| utils compounding helper | `delta2_to_delta_ranges` |
| utils shared recompute helper | `_fresh_delta2_frame` (private) |
| hint dict keys | `d2_lo`, `d2_hi`, `tied_buckets`, `all_counts`, `d2_bucket_size` |
| stoplight signals | `"2-Δ² seq"`, `"3-Δ² seq"` |
| new engine param | `dd2_bkt` (everywhere `dd_bkt` is threaded today) |
| page bucket widget key | `dd2_bucket_p` |
| page history vars | `_h_d2sq_hist`, `_h_prior_d2sq` (last Δ²), `_h_prior_d2sq2` (Δ² from two steps ago) |
| page bucket vars | `_h_dd2_bkt`, `_h_dd2_n` |
| page row builder | `_delta2_row_p` |
| Pitch Analysis input vars | `_dd2_p_abs_hist`, `_dd2_p_def_init`, `_dd2_p_def_follow`, `_dd2_p_init_val`, `_dd2_p_follow_val`, widget keys `dd2_p_init_{tab_p_pitcher}` / `dd2_p_follow_{tab_p_pitcher}` |
| chart keys | `p_delta2_delta2_hm`, `p_delta2_third`, `sv_delta2_chart` |

Never name any new variable `*_delta2` bare on the page side; the `d2sq` infix is the
disambiguator against the existing "value two steps ago" convention.

## The two-stage fresh recompute (used by all four new utils analysis functions)

Mirrors `enrich_df`'s discipline exactly (`utils.py:1000-1005`):

```
df_sw = df[df[value_col].notna()].sort_values(["game_id", group_col, "id"]).copy()
df_sw[delta_col] = df_sw.groupby(["game_id", group_col], group_keys=False)[value_col].apply(_circ_delta_group)
df_sw["_d2"] = df_sw.groupby(["game_id", group_col], group_keys=False)[delta_col].apply(lambda g: g.abs().diff().abs())
```

- Stage 1 leaves NaN at each game's first pitch; stage 2's `.diff()` therefore leaves
  NaN at the first two pitches of each game automatically. No extra game-boundary guard
  is needed because the groupby is already per game+pitcher.
- `delta_col = "pitch_circ_delta" if value_col == "pitch" else "swing_circ_delta"`,
  `group_col = "pitcher_name" ... else "batter_name"` - keep the value_col parameter for
  symmetry with the existing functions even though this feature only calls with "pitch".
- Factor this into one private helper `_fresh_delta2_frame(df, value_col) ->
  (df_sw, group_col) | None` placed near `next_delta_vs_prior_delta_heatmap`, returning
  None when fewer than 3 usable rows. The four new functions
  (`next_delta2_vs_prior_delta2_heatmap`, `delta2_third_dist`, `seq2_delta2_hint`,
  `seq3_delta2_hint`) all start from it. Do NOT retrofit the existing Δ functions to use
  it - leave them untouched (zero regression risk).

Δ² is bounded [0, 500] exactly like |Δ| (abs difference of two values each in [0, 500]),
so all bucket math (`bins = range(0, 501, bucket_size)`, `n_bkts = 500 // bucket_size`,
option set `[25, 50, 100, 125, 250, 500]`) carries over unchanged.

---

## Stage 1 - utils.py: heatmap + third-value distribution

### 1a. `next_delta2_vs_prior_delta2_heatmap(df, title="Next Pitch Δ² vs Prior Pitch Δ²", value_col="pitch", bucket_size=50)`

Place directly after `next_delta_vs_prior_delta_heatmap` (`utils.py:5440`). Copy that
function's body (`utils.py:5337-5440`) with these changes:
- Replace the single-stage delta recompute with `_fresh_delta2_frame`; the working
  series is `df_sw["_d2"]` instead of `df_sw[delta_col]`.
- Drop rows where `_d2` is NaN, then `_next_d2 = groupby(["game_id", group_col])["_d2"].shift(-1)`,
  dropna. (Consecutive Δ² pair needs 4 same-game pitches; the dropna chain enforces that
  without explicit length checks, but keep the early `len < 2` guards returning
  `go.Figure()` like the original.)
- `pd.cut` both series with `right=True, include_lowest=True` into the same
  `0..500 / bucket_size` labels.
- Axis titles: x `"Prior |Δ²|"`, y `"Next |Δ²|"`. Hovertemplate:
  `"Prior |Δ²|: %{x}<br>Next |Δ²|: %{y}<br>..."`. Everything else (column-normalized
  percentages, margin counts as annotations, colorscale, layout) identical.

### 1b. `delta2_third_dist(df, value_col="pitch", bucket_size=50, init_label="", follow_label="")`

Copy `delta_third_dist` (`utils.py:5614-5699`):
- Start from `_fresh_delta2_frame`; `_d1_abs/_d2_abs/_d3_abs` become the Δ² value and
  its `shift(-1)` / `shift(-2)` within game+pitcher (a triple needs 5 same-game pitches;
  again the dropna chain enforces it - keep a `len < 3` early guard on the frame for
  parity, it is just a cheap short-circuit).
- y label `"3rd |Δ²|"`, title `f"3rd |Δ²|  |  {init_label} → {follow_label}  (n={total})"`.
- Returns None when the (init, follow) cell is empty, same contract.

---

## Stage 2 - utils.py: hint functions + compounding helper

### 2a. `seq2_delta2_hint(df, value_col, bucket_size, prior_d2sq_abs, centered=False)` and `seq3_delta2_hint(df, value_col, bucket_size, prior_d2sq_1, prior_d2sq_2, centered=False)`

Place directly after `seq3_delta_hint` (`utils.py:3226`). Copy the bodies of
`seq2_delta_hint` / `seq3_delta_hint` (`utils.py:3121-3225`) with:
- `_fresh_delta2_frame` recompute; the conditioned/predicted series is `_d2`.
- Same centered vs fixed-bucket branches (linear distance, `half = bucket_size // 2`).
- Return dict keys renamed: `{"d2_lo", "d2_hi", "prob", "n", "tied_buckets",
  "all_counts", "d2_bucket_size"}`. `tied_buckets` stays a list of `(lo, hi)` Δ²-bucket
  tuples. **Deliberately different keys from the Δ hints** so a Δ² hint passed into the
  first-order `_delta_row_p` fails loudly (KeyError) instead of silently rendering wrong
  pitch ranges.

### 2b. `delta2_to_pitch_ranges(last_pitch: int, last_delta_signed: int, d2_lo: int, d2_hi: int) -> list[tuple[int, int]]`

**REVISED (mirrors `project_from_delta2s`, utils.py:2017-2033).** The earlier draft of
this helper (`delta2_to_delta_ranges`, unsigned |Δ| -> up/down magnitude arms ->
`delta_to_pitch_ranges` both-arms) is WRONG: it discards the sign of the current delta
and emits up to 4 pitch arms per Δ² bucket, including opposite-direction pitches the
canonical projector never produces. It also caused the arm-clutter concern (open
question #2). Do not use it.

The canonical model (`project_from_delta2s`) keeps the SIGNED last delta `L =
circular_signed_delta(prev_pitch, last_pitch)` and forms the next signed delta `L ± d2`,
then circular-adds to `last_pitch`. Δ² only moves the magnitude of the same-direction
delta (acceleration/deceleration); it never flips direction. Mirror that here.

Place directly after `delta_to_pitch_ranges` (`utils.py:3041`). Semantics: given the
last pitch, the SIGNED last delta `L`, and a predicted |Δ²| bucket `[d2_lo, d2_hi]`,
return the next-pitch circular arcs:
- `+` branch: signed next-delta range `[L + d2_lo, L + d2_hi]`.
- `-` branch: signed next-delta range `[L - d2_hi, L - d2_lo]`.
- Each signed range `[nd_lo, nd_hi]` becomes ONE circular pitch arc
  `((last_pitch + nd_lo - 1) % 1000 + 1, (last_pitch + nd_hi - 1) % 1000 + 1)`. A signed
  next-delta of any size wraps cleanly on the 1-1000 wheel, so there is NO clipping and
  NO unreachable-bucket case (contrast the old unsigned helper).
- When `d2_lo == 0` the two arcs meet at `last_pitch + L`; merge overlapping/touching
  arcs. Return 1 or 2 `(lo, hi)` tuples (each a possibly-wrapping circular arc).
- The two arcs never both collapse to empty, so this never returns `[]` (unlike the old
  helper). Callers get exactly 2 arms per Δ² bucket (1 after the `d2_lo == 0` merge).
- Docstring must state this is the SIGNED-delta circular projection on the 1-1000 wheel,
  identical in spirit to `project_from_delta2s`'s inner `last_val + (last_delta +
  sign*d2)` step, generalized from a point to a `[d2_lo, d2_hi]` bucket.

Merging wrapping circular arcs is not what `merge_delta_ranges` does (that is linear on
[0,500]). Merge the two arcs by checking whether the second arc's start falls inside the
first arc's circular span; if the two branches are disjoint just return both. Keep the
merge logic local to this helper.

---

## Stage 3 - utils.py: stoplight engine

All new parameters take `dd2_bkt: int | None = None` with a `None -> dd_bkt` fallback in
the body. That keeps `scripts/calibrate_stoplight_thresholds.py:62` (positional call
with 4 args) working unmodified; the app always passes it explicitly.

### 3a. `_recency_indications(sw, value_col, hz_bkt, dd_bkt, dd2_bkt=None)` (`utils.py:3589`)

After the existing `delta_abs`/`delta_bkt` block (`utils.py:3615-3626`), add the inline
Δ² recompute:

```
dd2_n = max(1, 500 // dd2_bkt)
d2sq_abs = np.full(n, np.nan)
if n > 2:
    d2sq_abs[2:] = np.where(same2[2:], np.abs(delta_abs[2:] - delta_abs[1:-1]), np.nan)
d2sq_bkt = pd.cut(pd.Series(d2sq_abs), bins=list(range(0, 501, dd2_bkt)),
                  labels=False, right=True, include_lowest=True).to_numpy()
d2b = [_int_or_none(d2sq_bkt[i]) for i in range(n)]
```

The `same2` guard is required, not optional: at the second pitch of a game,
`delta_abs[i]` is non-NaN and `delta_abs[i-1]` (the previous game's last delta) is also
non-NaN, so a bare `delta_abs[i] - delta_abs[i-1]` would silently cross the game
boundary. `same2` (rows i-2, i-1, i all one game) is exactly the validity condition for
a Δ² at row i.

New entries appended right after `ind["3-Δ seq"]` (`utils.py:3657`), mirroring the Δ
pair verbatim:

```
ind["2-Δ² seq"] = (
    [d2b[i - 1] if i >= 1 else None for i in range(n)], d2b, dd2_n)
ind["3-Δ² seq"] = (
    [(d2b[i - 2], d2b[i - 1]) if (i >= 2 and d2b[i - 2] is not None and d2b[i - 1] is not None) else None
     for i in range(n)], d2b, dd2_n)
```

(None contexts and None outcomes are already skipped by `_surprisal_walk` /
`_surprisal_walk_detail`, so no other engine change is needed - the scoring, window,
and green/yellow/red thresholds are inherited automatically.)

### 3b. `_recency_labelers(signal, hz_bkt, dd_bkt, dd2_bkt=None)` (`utils.py:3729`)

Add a `delta2sq(b)` formatter (same shape as `delta(b)` but with `dd2_bkt`), and table
rows:

```
"2-Δ² seq":             (delta2sq, delta2sq),
"3-Δ² seq":             (pair(delta2sq), delta2sq),
```

### 3c. Pass-throughs

- `scouting_recency_states(df, value_col, window_n, hz_bkt, dd_bkt, dd2_bkt=None)`
  (`utils.py:3678`): forward to `_recency_indications`. Update the docstring's "nine
  covered indications" to eleven.
- `scouting_recency_detail(df, value_col, signal, window_n, hz_bkt, dd_bkt, dd2_bkt=None)`
  (`utils.py:3774`): forward to both `_recency_indications` and `_recency_labelers`.

---

## Stage 4 - utils.py: `sequence_matches` domain="delta2" (`utils.py:2593`)

Minimal extension, no recompute:

- Column selection: `if domain in ("delta", "delta2"): delta_col =
  f"{value_col}_circ_delta" if domain == "delta" else f"{value_col}_circ_delta2"`, keep
  the `delta_col not in df_s.columns -> return None` guard and `_v = abs()`.
- Distance branches at `utils.py:2651-2660`: change `if domain == "delta"` to
  `if domain != "value"` (Δ² matching is linear on [0, 500], same as Δ).
- Update the docstring (domain list + note that "delta2" reads
  `{value_col}_circ_delta2`).

**Recompute question - resolved, no fresh recompute needed.** The viewer calls run on
`df_p` *before* the `df_p = df_p_pred` rebind at `pages/2_Scouting.py:1961`. That
`df_p` is a concat of fully-`enrich_df`'d frames (`_load_pitcher_plays:256` and
`_load_scrimmage_plays:373` both enrich), filtered only by `season` and `def_team`
(`pages/2_Scouting.py:1315-1319`) - both game-level attributes, so whole games survive
and the per-game-group `pitch_circ_delta2` values remain internally valid. This is the
identical trust the existing `domain="delta"` tab already places in
`pitch_circ_delta`. (Even a future move to `df_p_pred` would stay safe: `id < hist_id`
truncates trailing rows, which cannot invalidate earlier same-game diffs.)

Also note `_p1`/`_p2` context and `_nxt` inside `sequence_matches` are same-game
shifts of `_v` - with `_v = Δ²` NaN on the first two pitches of each game, the existing
notna masks handle everything; no change beyond the two branches above.

---

## Stage 5 - pages/2_Scouting.py: Swing Suggestions + stoplight threading

### 5a. History variables (insert alongside `pages/2_Scouting.py:1437-1456`)

Next to `_h_dd_bkt` (line 1437):
```
_h_dd2_bkt = st.session_state.get("dd2_bucket_p", 100)
```
(Same read-before-widget-exists pattern as `dd_bucket_p`; the slider is created later in
the Pitch Analysis expander, default 100 on first render.)

Next to `_h_delta_hist` (line 1444-1447):
```
_h_d2sq_hist = (
    df_p[df_p["pitch_circ_delta2"].notna()].sort_values("id")["pitch_circ_delta2"]
    .abs().astype(int).tolist()
) if "pitch_circ_delta2" in df_p.columns else []
```
Next to lines 1452-1456:
```
_h_prior_d2sq  = _h_d2sq_hist[-1] if len(_h_d2sq_hist) >= 1 else None
_h_prior_d2sq2 = _h_d2sq_hist[-2] if len(_h_d2sq_hist) >= 2 else None
```
Next to `_h_dd_n` (line 1464): `_h_dd2_n = 500 // _h_dd2_bkt`.

### 5b. `_delta2_row_p(signal, h_dict, n_bkts)` - new row builder (place right after `_delta_row_p`, line 1539)

Do NOT extend `_delta_row_p`; the compounding path is different enough that a sibling
helper is clearer. Body:

1. `_zs = utils.hint_zscore(h_dict["prob"], h_dict["n"], n_bkts)`.
2. Compute the SIGNED last delta. This needs the prior TWO pitches (not the unsigned
   `_h_prior_delta`): guard on `_h_prior_pitch is not None and _h_prior_pitch2 is not
   None`, then `_L = utils.circular_signed_delta(_h_prior_pitch2, _h_prior_pitch)`.
   (`_h_prior_pitch2` is `_h_recent[-2]`, `_h_prior_pitch` is `_h_recent[-1]` - the two
   most recent pitches, exactly the pair `project_from_delta2s` uses for its
   `last_delta`.) If either prior pitch is missing, fall to the step-3 fallback.
   - `_all_bkts = [(h_dict["d2_lo"], h_dict["d2_hi"])] + h_dict.get("tied_buckets", [])`
   - `_merged_d2 = utils.merge_delta_ranges(_all_bkts)` (Δ² magnitude buckets are linear
     on [0,500], so this linear merge is correct HERE - it is only the pitch-arc merge
     inside `delta2_to_pitch_ranges` that is circular).
   - Compound: `_arms = []`; for each merged Δ² bucket `(lo, hi)`, extend with
     `utils.delta2_to_pitch_ranges(_h_prior_pitch, _L, lo, hi)` (the signed compounder
     from Stage 2b - each bucket yields 1-2 circular pitch arcs, already valid pitch
     ranges, no `(None, None)` placeholders to filter). Then de-dup/merge is unnecessary
     unless tied buckets are adjacent; leaving them as separate arms is fine (each is a
     distinct green zone).
   - Unpack: `r1 = _arms[0] if _arms else (None, None)`, `r2 = _arms[1] if len(_arms) > 1
     else (None, None)`, `extra = list(_arms[2:])`.
   - Return the same dict shape as `_delta_row_p`'s success branch: `Signal, lo, hi,
     lo2, hi2, extra_ranges, Strength (_hstr(...)), _zscore` - and **SET the Δ²
     all-zones painter keys** (RESOLVED open question #1 - build the correct painter
     now, do NOT leave gray): `_d2sq_counts = h_dict.get("all_counts")`,
     `_last_delta_for_zone = _L`, `_prior_pitch_for_zone = _h_prior_pitch`,
     `_dd2_bkt_for_zone = h_dict.get("d2_bucket_size", _h_dd2_bkt)`.
     **Do NOT set `_delta_counts`** - that key routes to the first-order painter and
     would paint wrong ranges. The new Δ²-specific painter branch (Stage 6a below)
     keys off `_d2sq_counts`. Because the signed projection `last_pitch + (L ± d2)`
     sweeps the whole wheel exactly once as d2 ranges [0,500] over both signs, the Δ²
     buckets partition the pitch wheel without overlap, so proportion-shaded tiling is
     correct and "greenest = best zone" holds.
   - With the signed compounder there is always at least 1 arm (the wheel is always
     reachable), so the empty-`_arms` case only arises from the missing-prior-pitch
     guard, which is handled by step 3.
3. Else (either prior pitch missing): return the fallback dict (Signal, lo/hi None,
   Strength, `_zscore`), mirroring `_delta_row_p`'s else branch minus the zone keys.

**Arm count decision (RESOLVED open question #2):** surface ALL arms. With the signed
compounder each Δ² bucket yields exactly 2 arms (the `+` and `-` acceleration branches),
merged to 1 when `d2_lo == 0` - NOT the 4 the old unsigned draft produced. A single best
bucket is 2 arms; each tied bucket adds at most 2 more. `hint_bars_figure` already
renders every `extra_ranges` entry with colored zone + boundary labels
(`utils.py:2445-2457`), same as the Δ rows accept via tied `extra_ranges`. No truncation.

### 5c. Hint rows (insert after the 3-Δ seq block, line 1549)

```
if _h_prior_d2sq is not None:
    _h = utils.seq2_delta2_hint(df_p, "pitch", _h_dd2_bkt, _h_prior_d2sq, centered=_hint_centered_p)
    if _h:
        _hint_rows_p.append(_delta2_row_p("2-Δ² seq", _h, _h_dd2_n))

if _h_prior_d2sq is not None and _h_prior_d2sq2 is not None:
    _h = utils.seq3_delta2_hint(df_p, "pitch", _h_dd2_bkt, _h_prior_d2sq2, _h_prior_d2sq, centered=_hint_centered_p)
    if _h:
        _hint_rows_p.append(_delta2_row_p("3-Δ² seq", _h, _h_dd2_n))
```

(Argument order for seq3 mirrors the Δ call at line 1547: older value first.)
Rows then sort by z-score with everything else (line 1640); no ordering work needed.

### 5d. Stoplight threading

- `_STOPLIGHT_ORDER` (line 271-272): insert `"2-Δ² seq", "3-Δ² seq"` immediately after
  `"3-Δ seq"`.
- `_load_pitcher_stoplights` (line 259-262): add `dd2_bkt: int` parameter after
  `dd_bkt` (it doubles as the cache key) and pass to
  `utils.scouting_recency_states(df, "pitch", window_n, hz_bkt, dd_bkt, dd2_bkt)`.
- `_load_pitcher_stoplight_detail` (line 265-269): same, forwarding to
  `scouting_recency_detail`.
- `_stoplight_inspector` (line 275): add `dd2_bkt` between `dd_bkt` and `states`; pass
  it in the internal `_load_pitcher_stoplight_detail` call (line 302-303).
- Call sites: line 1649-1652 (`_load_pitcher_stoplights(..., int(_h_dd_bkt),
  int(_h_dd2_bkt), utils.scouting_cache_sig())`) and line 1706-1709
  (`_stoplight_inspector(..., int(_h_dd_bkt), int(_h_dd2_bkt), _stop_states,
  _insp_order)`).
- The Signal -> state mapping loop (lines 1654-1668) needs no change: `"2-Δ² seq"` /
  `"3-Δ² seq"` are exact-match keys like the Δ pair, so the stoplight dot and the
  Inspector ordering pick them up automatically.

### 5e. utils.py `hint_bars_figure` - Δ² "All zones" painter branch (RESOLVED open question #1)

Add a Δ²-specific branch to the `mode == "all" and not _best_zone_only` block in
`hint_bars_figure` (`utils.py:2398`), placed BEFORE the existing `_delta_counts` branch
so a Δ² row (which carries `_d2sq_counts` but not `_delta_counts`) is caught first; Δ
rows (no `_d2sq_counts`) fall through to the existing `elif _delta_counts` / `elif
zone_dist` chain unchanged.

```
d2_counts     = h.get("_d2sq_counts")
last_delta_z  = h.get("_last_delta_for_zone")
prior_pitch_z = h.get("_prior_pitch_for_zone")
if d2_counts is not None and last_delta_z is not None and prior_pitch_z is not None:
    total_dc = sum(d2_counts)
    n_dc     = len(d2_counts)
    dd2_bkt  = h.get("_dd2_bkt_for_zone", 100)
    for di, cnt in enumerate(d2_counts):
        raw_t = max(-1.0, min(1.0, cnt / total_dc * n_dc - 1.0)) if total_dc >= 5 else 0.0
        color = _zone_color(raw_t) if total_dc >= 5 else _GRAY
        for pr in delta2_to_pitch_ranges(prior_pitch_z, last_delta_z, di * dd2_bkt, (di + 1) * dd2_bkt):
            _colored_zone(pr[0], pr[1], color)
elif <existing _delta_counts branch> ...
```

This mirrors the existing first-order painter (`utils.py:2405-2414`) - same `total_dc >=
5` gate, same proportion->`raw_t`->`_zone_color` shading, same `_colored_zone` for the
circular arc - but compounds through the SIGNED `delta2_to_pitch_ranges` instead of
`delta_to_pitch_ranges`. Because the signed projection `last_pitch + (L ± d2)` sweeps
the wheel exactly once across all buckets and both branches, adjacent Δ² buckets tile
without overlap and the greenest arcs correspond to Best Zone, exactly as the Δ painter's
comment promises for first-order buckets.

---

## Stage 6 - pages/2_Scouting.py: Pitch Analysis UI

**Insertion point decision: after the complete existing Δ block - i.e. after the
`delta_third_dist` chart / "Not enough data" caption ending at line 2009, immediately
before the `st.divider()` at line 2011.** Rationale: the existing sub-structure
(heatmap -> Initial/Following inputs -> third-dist) is one semantic unit; inserting the
Δ² heatmap after line 1977 would wedge it between the Δ heatmap and the Δ inputs that
belong to it. The Δ² block reproduces the whole unit, introduced by its own divider:

```
st.divider()
st.subheader("Next Pitch Delta² vs Prior Pitch Delta²")
st.caption("How does the size of the pitcher's adjustment change from one pitch to the next?")
dd2_bucket_p = st.select_slider("Bucket size ", options=[25, 50, 100, 125, 250, 500], value=100, key="dd2_bucket_p")
st.plotly_chart(
    utils.next_delta2_vs_prior_delta2_heatmap(df_p, title="Next Pitch Δ² vs Prior Pitch Δ²", value_col="pitch", bucket_size=dd2_bucket_p),
    width="stretch", config={"displayModeBar": False}, key="p_delta2_delta2_hm",
)
```

Note the slider label needs a trailing space (or any distinct label) - Streamlit
forbids two widgets with identical label + type unless keys differ; keys DO differ
here (`dd2_bucket_p`), so an identical label is technically fine, but a distinct label
("Bucket size (Δ²)" is clearer anyway) avoids user confusion. Recommend
`"Bucket size (Δ²)"`.

Then the inputs + third-dist, mirroring lines 1979-2009 with Δ² history (this section
runs after the `df_p = df_p_pred` rebind at line 1961, same as the Δ block):

```
_dd2_p_abs_hist = (
    df_p[df_p["pitch_circ_delta2"].notna()]
    .sort_values("id")["pitch_circ_delta2"].abs().astype(int).tolist()
)
_dd2_p_def_init   = _dd2_p_abs_hist[-2] if len(_dd2_p_abs_hist) >= 2 else 250
_dd2_p_def_follow = _dd2_p_abs_hist[-1] if _dd2_p_abs_hist else 250
```

Two columns with `st.number_input("Initial |Δ²|", 0..500, key=f"dd2_p_init_{tab_p_pitcher}")`
and `"Following |Δ²|"` (`key=f"dd2_p_follow_{tab_p_pitcher}"`), then the same
bucket-index -> label math as lines 1996-2000 using `dd2_bucket_p`, then
`utils.delta2_third_dist(df_p, value_col="pitch", bucket_size=dd2_bucket_p,
init_label=..., follow_label=...)` rendered with `key="p_delta2_third"`, with the same
"Not enough data for this delta sequence." caption fallback.

---

## Stage 7 - pages/2_Scouting.py: Sequence Viewer third tab

- Line 1722-1727 (`_sv_mode` radio `on_change`): add
  `st.session_state.pop("sv_delta2_chart", None)` to the callback tuple.
- Line 1759: `_sv_pitch_tab, _sv_delta_tab, _sv_delta2_tab = st.tabs(["Pitch #", "Delta", "Delta²"])`.
- New block after the Delta tab (mirror lines 1773-1784):

```
with _sv_delta2_tab:
    if _h_prior_d2sq is None or (_sv_use2 and _h_prior_d2sq2 is None):
        st.caption("No historical matches for this context.")
    else:
        _sv_res = utils.sequence_matches(
            df_p, "pitch", _h_dd2_bkt, _h_prior_d2sq,
            prior_val2=_h_prior_d2sq2 if _sv_use2 else None, domain="delta2")
        if not _sv_res:
            st.caption("No historical matches for this context.")
        else:
            _sv_fragment(_sv_res["matches"], _h_d2sq_hist[-3:], (0, 500), "|Δ²|",
                         _h_dd2_bkt, "sv_delta2_chart", _is_mobile, _sv_note)
```

`_sv_fragment` and `sequence_viewer_figure` are already domain-agnostic
(y_range/y_label/group_bucket parameters, fixed `SEQ_VIEWER_BAR_TRACE` index), so
click-to-filter, the Clear-filter button, and the mode note come for free.

Note this Sequence Viewer block runs BEFORE the line-1961 rebind, so `df_p` here is the
full pitcher frame - which is exactly why reading the precomputed column is safe (see
Stage 4).

---

## Stage 8 - Validation

1. **Compile/boot:** `python -m py_compile utils.py pages/2_Scouting.py`, then
   `streamlit run` and load the Scouting page for a pitcher with a long history.
2. **Heatmap sanity (a):** pick a heavily-scouted pitcher. Check the Δ² heatmap's
   column-count annotations sum to (career swing-pitch count minus 3x games-with-4+
   pitches... approximately: total Δ² pairs). Cross-check exactly with a scratchpad
   pandas script: load the same frame via `_load_pitcher_plays`-equivalent, run the
   two-stage recompute, `pd.crosstab` next vs prior Δ² buckets, and compare a few cells
   against hover values. Confirm the distribution is non-degenerate (not one giant
   0-bucket column - if it is, check the abs().diff().abs() ordering wasn't flipped).
3. **Compounding hand-check (b):** worked example to verify in the Suggestions tab -
   prior pitch 800, current |Δ| 150, predicted Δ² bucket [100, 200], no ties:
   - Step A: next |Δ| in [0, 50] (down arm 150-[100,200], clipped) or [250, 350].
   - Step B: [0, 50] from 800 -> merged single arm (750, 850) (delta_lo == 0 merge
     case); [250, 350] from 800 -> (50, 150) wrap arm and (450, 550).
   - Expect exactly three green arms: 750-850, 50-150, 450-550. Verify boundary labels
     and that "All zones" mode shows a plain gray bar for the Δ² rows (no repainting).
   - Also verify an unreachable-bucket case renders a Signal+Strength row with no arms
     and no exception.
4. **Stoplight discipline (c):** in the Inspector, select "2-Δ² seq": confirm
   (i) n_scored equals career Δ² transitions (each game contributes
   `max(0, pitches_in_game - 3)` scored events for 2-seq: context needs Δ²[i-1], outcome
   Δ²[i], both needing 3 pitches - spot-check one short game by hand);
   (ii) the Context/Observed labels use `dd2_bkt`-sized ranges and change when the Δ²
   slider changes (proves the cache key threads through);
   (iii) changing `dd_bucket_p` alone does NOT change the Δ² signals' labels (proves
   independence); (iv) the green/yellow/red state uses the same window_n and vote
   thresholds as "2-Δ seq" (both go through the shared `_aggregate_recency` /
   `_surprisal_walk` - verify by eyeballing that scores land in the same 0-centered
   scale in the drill-down table).
5. **Sequence Viewer (d):** on the Delta² tab, confirm the current-sequence markers
   match the last values of `pitch_circ_delta2` for that pitcher (cross-check against
   the Last N chart's pitch values by hand-computing two deltas and their difference);
   click an outcome bar -> paths filter, Clear filter restores, switching "Match on"
   mode clears the selection (the on_change pop).
6. **Regression sweep:** Δ heatmap/third-dist/2-Δ/3-Δ rows unchanged at default state;
   batter tab renders (no signature it uses changed - the batter side calls
   `seq2_delta_hint`/`seq3_delta_hint`/`next_delta_vs_prior_delta_heatmap` which are
   untouched, and has no stoplight loader); `python scripts/calibrate_stoplight_thresholds.py`
   still imports and runs (it now also calibrates the two new signals with
   `dd2_bkt = dd_bkt` via the None default - acceptable).

---

## Open questions - RESOLVED (Alex, 2026-07-15)

1. **"All zones" painting for Δ² rows -> BUILD THE CORRECT PAINTER NOW** (no gray
   fallback). Implemented in Stage 5e as a `_d2sq_counts` branch in `hint_bars_figure`,
   compounding through the SIGNED `delta2_to_pitch_ranges`. `_delta2_row_p` sets
   `_d2sq_counts` / `_last_delta_for_zone` / `_prior_pitch_for_zone` /
   `_dd2_bkt_for_zone` (Stage 5b).
2. **Arm clutter -> render ALL arms**, and it is a non-issue now: switching the
   compounder to the signed `project_from_delta2s` model makes each Δ² bucket yield
   exactly 2 arms (1 when `d2_lo == 0`), not the old unsigned draft's 4. See Stage 2b /
   5b.
3. **Slider label -> "Bucket size (Δ²)"** (Stage 6).
4. **`delta2_third_dist` sample sizes -> keep the caption fallback**, no change. Defaults
   are the pitcher's own last two Δ² values, so the seeded cell is normally populated.
5. **`_h_d2sq_hist` source frame -> mirror `df_p`** (match the existing Δ behavior
   exactly). Live mode is unaffected; the historical-mode leak is inherited knowingly and
   NOT fixed here (would require changing the existing Δ read too - out of scope for this
   feature).

## Design correction absorbed into the plan above (do not revert)

The original draft's `delta2_to_delta_ranges` (unsigned |Δ| -> up/down magnitude arms ->
`delta_to_pitch_ranges`) did NOT mirror the established Swing Analyzer Δ² projection
(`project_from_delta2s`, `utils.py:2017-2033`) and produced spurious opposite-direction
arms. It is replaced everywhere by `delta2_to_pitch_ranges(last_pitch, last_delta_signed,
d2_lo, d2_hi)`, which keeps the SIGNED last delta and forms `L ± d2` before the circular
pitch add - identical to `project_from_delta2s`'s inner step, generalized to a bucket.
Stages 2b, 5b, and 5e all reflect this.

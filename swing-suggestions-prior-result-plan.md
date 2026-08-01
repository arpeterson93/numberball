# Prior-result / boundary-crossing Swing Suggestions - implementation plan

Pitcher-side only (`df_p`, "Swing Suggestions" panel, "Pitch Analysis" expander,
Stoplight/Inspector). Do not touch the batter side (`df_b`, "Swing Analysis"
expander, batter Suggestions) - same scope discipline as the Δ² feature
(`delta2-sequence-plan.md`), which this plan follows closely in style and
conventions.

Four features, in the order the user described them:

1. **Between-Game Δ** - promote the existing `between_game_deltas()` descriptive
   histogram into a full Swing Suggestion row + Stoplight/Inspector signal.
2. **Between-Inning Δ** - same, for `between_inning_deltas()`.
3. **Prior result → Δ** - a new heatmap (Pitch Analysis) + Suggestion row +
   Stoplight signal, conditioning the next pitch's |Δ| on the *result category*
   of the immediately preceding plate appearance.
4. **Result seq → Δ** - the 2-step extension of #3: conditions on the result
   categories of the *two* preceding plate appearances (e.g. "XBH then XBH", "Out
   then Out"), surfaced as a Pitch-Analysis distribution view (mirroring
   `delta_third_dist`'s Initial/Following UX, but with category dropdowns instead
   of number inputs) + Suggestion row + Stoplight signal.

Project constraints that apply throughout (same as every prior plan in this repo):
- No em dashes in any `.py` file - use hyphens. ("Δ" is fine, already established.)
- No Co-Authored-By trailer if committing.
- Always recompute fresh from raw columns inside functions that may receive a
  filtered/sliced `df_p` (documented reason elsewhere in the codebase: "Always
  recalculate deltas fresh to ensure proper grouping for filtered data").

---

## Resolved decisions (do not re-litigate)

Asked and answered 2026-07-31, plus one follow-up correction from Alex:

1. **Item 3 taxonomy**: reuse an existing-style OUT/BB-1B/XBH taxonomy rather than
   inventing per-result granularity (HR/3B/2B/1B/BB/Out) or a diff-based one.
2. **Item 4 context**: result-category buckets (not diff-quality buckets) - this
   is what makes "got shellacked XBH, XBH" / "two outs in a row" visible.
3. **Taxonomy correction (supersedes the initial "share one 3-bucket taxonomy"
   answer)**: the final taxonomy is **four** buckets, shared by items 3 and 4:
   **XBH, BB-1B, Out, K+** - where "K+" is K and anything *at or worse than* K in
   the codebase's canonical result-quality ordering. GORA/SacF/DSacF etc. are
   NOT specially pulled into a "run-scoring" bucket (that idea from the earlier
   question is dropped) - they simply land wherever the canonical order puts them
   (all of them sort better than K, so they fall into the plain "Out" bucket).
4. **Items 1 & 2 scope**: full parity with the existing signal families -
   Suggestion row AND Stoplight/Inspector wiring (green/yellow/red state,
   drill-down table), gated so the row only appears when the upcoming pitch
   actually is the first of a new game/inning.

### The canonical "result order" and the XBH / BB-1B / Out / K+ split

The codebase already has a comprehensive, ordered (best -> worst) mapping of every
result string: `_RESULT_ZONE_COLORS` (`utils.py:556-591`), whose key order goes
from `"HR"` (green) through hits/BB, soft outs, standard outs, `"K"`, double
plays, down to `"LOTP"` (near-black). This is "the order we have for results"
Alex referenced. Reuse its key order directly rather than inventing a new
ranking - one source of truth, no drift risk.

- **XBH** = `_XBH` (`utils.py:812`) = `{"HR", "3B", "2BWH", "2B"}` - unchanged,
  reuse as-is.
- **BB-1B** = `_BB1B` (`utils.py:813`) = `{"1BWH2", "1BWH", "1B", "IF1B", "BB"}` -
  unchanged, reuse as-is. (Note: this is the *plain* BB-1B set, NOT the
  GORA/SacF/DSacF-augmented version floated in the earlier question - that idea
  is superseded per decision #3 above.)
- **K+** = every result ordered at-or-after `"K"` in `_RESULT_ZONE_COLORS`'s key
  order, i.e. `{"K", "DPRun", "DP", "DP21", "DP31", "DPH1", "LODP", "TP", "LOTP"}`,
  **plus** `"KCS"` and `"BDP"` (in `_DP_RESULTS`, `utils.py:835`, but not present
  in `_RESULT_ZONE_COLORS` - both are DP-adjacent, clearly K-or-worse, added
  explicitly).
- **Out** = everything else that isn't XBH/BB-1B/K+ - i.e. every result ordered
  *better* than `"K"` that also isn't a hit/BB: `{"GORA", "DSacF", "DFO", "SacF",
  "FO", "PO", "FCH", "FC", "FC3rd", "GO", "LO"}`, plus any result string this
  page has never seen before (safe default - mirrors `get_res_category`'s own
  default-to-`"OUT"` behavior for unmapped cases).
- **Bunt variants** (`"SacB"`, `"DSacB"`): resolve through `_BUNT_TO_SWING`
  (`utils.py:594-597`) *before* categorizing, exactly like `_result_color` already
  does (`utils.py:599-600`) - so a bunt sac fly lands in the same bucket as its
  swing equivalent (`"SacF"`/`"DSacF"`, both "Out").
- **CS/CS2/CS3/CS4/BFC** (in `_OUT_RESULTS`, `utils.py:832-834`, but not in
  `_RESULT_ZONE_COLORS`): these are baserunning outs, not DP-adjacent - fall
  through to the "Out" default. Do not add them to K+.

### New primitive - place near `get_res_category` (`utils.py:822-829`)

```python
_SEQ_RESULT_ORDER = list(_RESULT_ZONE_COLORS.keys())  # best -> worst, canonical
_SEQ_K_OR_WORSE = set(_SEQ_RESULT_ORDER[_SEQ_RESULT_ORDER.index("K"):]) | {"KCS", "BDP"}
_SEQ_RESULT_CATEGORIES = ["XBH", "BB-1B", "Out", "K+"]  # fixed display/axis order

def seq_result_category(result: str) -> str:
    """XBH / BB-1B / Out / K+ bucket for the prior-result Swing Suggestion
    signals. Distinct from get_res_category: no diff>=300 override - this is a
    pure result-string categorization. 'K+' is K and anything ordered at or
    worse than K in _RESULT_ZONE_COLORS's canonical best->worst key order (plus
    the DP-adjacent KCS/BDP, which aren't in that dict). Unrecognized result
    strings default to 'Out', mirroring get_res_category's own default."""
    r = _BUNT_TO_SWING.get(result, result)
    if r in _XBH:
        return "XBH"
    if r in _BB1B:
        return "BB-1B"
    if r in _SEQ_K_OR_WORSE:
        return "K+"
    return "Out"
```

`_SEQ_RESULT_ORDER`/`_SEQ_K_OR_WORSE` are module-level constants computed once at
import time (same pattern as `_DIFF_HM_BINS`/`_DELTA_HM_BINS`, `utils.py:6353-6357`).

---

## Naming (fixed for the whole feature)

| Thing | Name |
|---|---|
| taxonomy fn | `seq_result_category` |
| taxonomy constants | `_SEQ_RESULT_ORDER`, `_SEQ_K_OR_WORSE`, `_SEQ_RESULT_CATEGORIES` |
| item 3 heatmap fn | `next_pitch_delta_vs_prior_result_heatmap` |
| item 3 hint fn | `prior_result_delta_hint` |
| item 4 shared context helper | `_result_seq_context` (private) |
| item 4 hint fn | `result_seq_delta_hint` |
| item 4 dist-view fn | `result_seq_delta_dist` |
| item 1 hint fn | `between_game_delta_hint` |
| item 2 hint fn | `between_inning_delta_hint` |
| stoplight signals | `"Prior result → Δ"`, `"Result seq → Δ"`, `"Between-Game Δ"`, `"Between-Inning Δ"` |
| page history vars | `_h_result_hist`, `_h_prior_rescat`, `_h_prior_rescat2` |
| page gating vars (reused, already exist) | `_show_fp_app_p`, `_show_fp_inn_p` |
| item 4 page selectbox keys | `rs_older_p`, `rs_newer_p` |
| chart keys | `p_result_delta_hm`, `p_result_seq_dist` |

None of the four new hint functions take a `centered` parameter. `diff_to_delta_hint`
and the seq2/seq3 Δ hints take `centered` because they condition on a *continuous*
prior value (a window can be centered around it). All four new signals here
condition on a *discrete* category (or, for items 1-2, nothing at all beyond
eligibility) - there is no window to center, so omitting the parameter avoids a
dead/no-op kwarg at call sites. `hint_centered_p` (the page's global toggle)
simply doesn't apply to these four rows, exactly like it already doesn't apply to
`"Outs"`, `"Base state"`, `"1st pitch appearance"`, `"1st pitch inning"`.

**Why no new painter / no `hint_bars_figure` changes are needed:** all four new
hint functions return the *exact* same dict shape as `diff_to_delta_hint`
(`{delta_lo, delta_hi, prob, n, all_counts, delta_bucket_size}`), so their
Suggestion rows are built by calling the **existing, unmodified** `_delta_row_p`
helper (`pages/2_Scouting.py:1626-1655`) - same as `"Prior diff → Δ"` already
does. `_delta_row_p` already sets `_delta_counts` / `_prior_pitch_for_zone` /
`_dd_bkt_for_zone`, which `hint_bars_figure`'s existing `_delta_counts` painter
branch (`utils.py:~2405-2414`, untouched by the Δ² plan) already paints
correctly via `delta_to_pitch_ranges`. This is a meaningfully smaller lift than
the Δ² feature, which needed a bespoke painter branch because its compounding
model was different (signed Δ² projection vs a flat |Δ| bucket).

---

## Stage A - Items 1 & 2: Between-Game Δ / Between-Inning Δ

### A1. `utils.py` hint functions - place after `between_game_deltas`
(`utils.py:1533-1554`)

Both reuse the existing descriptive functions directly (no new fresh-recompute
logic needed - `between_game_deltas`/`between_inning_deltas` already do it):

```python
def between_game_delta_hint(df: pd.DataFrame, value_col: str = "pitch") -> dict | None:
    """Most likely |Δ| (fixed 100-unit bins) into the first pitch of a new game,
    given this player's historical between-game deltas. Unconditional (no prior
    value to condition on beyond "this is a game transition") - see
    between_game_deltas for the underlying signed-delta computation."""
    deltas = between_game_deltas(df, value_col)
    if deltas.empty:
        return None
    cats = pd.cut(deltas.abs().astype(int), bins=_DELTA_HM_BINS, labels=False,
                  right=True, include_lowest=True).dropna()
    if cats.empty:
        return None
    counts = cats.astype(int).value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {"delta_lo": best_bkt * delta_step, "delta_hi": (best_bkt + 1) * delta_step,
            "prob": counts.iloc[0] / len(cats), "n": int(len(cats)),
            "all_counts": all_counts, "delta_bucket_size": delta_step}


def between_inning_delta_hint(df: pd.DataFrame, value_col: str = "pitch") -> dict | None:
    """Same as between_game_delta_hint but for within-game inning transitions."""
    deltas = between_inning_deltas(df, value_col)
    if deltas.empty:
        return None
    cats = pd.cut(deltas.abs().astype(int), bins=_DELTA_HM_BINS, labels=False,
                  right=True, include_lowest=True).dropna()
    if cats.empty:
        return None
    counts = cats.astype(int).value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {"delta_lo": best_bkt * delta_step, "delta_hi": (best_bkt + 1) * delta_step,
            "prob": counts.iloc[0] / len(cats), "n": int(len(cats)),
            "all_counts": all_counts, "delta_bucket_size": delta_step}
```

(Yes, this duplicates a few lines between the two functions and with
`diff_to_delta_hint`'s tail - that's consistent with the codebase's existing
style: `diff_to_delta_hint`/`seq2_delta_hint`/`seq3_delta_hint` etc. each have
their own near-identical tail rather than sharing a helper. Do not "clean this
up" into a shared helper as part of this feature - out of scope, matches
precedent.)

### A2. `_recency_indications` - new entries (`utils.py:4207-4320`)

The existing `raw` array (`utils.py:4241-4244`, computed *before* the `same1`
mask is applied to produce `delta_signed`) is already exactly the cross-boundary
wrapped delta these two signals need - it just gets thrown away today. Keep an
unmasked copy:

```python
delta_signed_any = np.full(n, np.nan)
if n > 1:
    delta_signed_any[1:] = raw
delta_any100 = pd.cut(pd.Series(np.abs(delta_signed_any)), bins=_DELTA_HM_BINS,
                      labels=False, right=True, include_lowest=True).to_numpy()
da100 = [_int_or_none(delta_any100[i]) for i in range(n)]
```

Place this right after the existing `delta100 = pd.cut(...)` line (`utils.py:4248-4249`),
before the `d2sq_raw` block - `raw` is still in scope there and hasn't been
reassigned. Then two new `ind[...]` entries, placed after `ind["Prior diff → Δ"]`
(`utils.py:4304-4305`):

```python
# Between-Game Delta: is this row the first pitch of a new game for this
# pitcher? -> |Delta| into it, unmasked by the same-game guard (that guard is
# exactly what would otherwise hide this signal's only eligible rows).
ind["Between-Game Δ"] = (
    ["bg" if bool(fp_app[i]) else None for i in range(n)], da100, len(_DELTA_HM_BINS) - 1)
# Between-Inning Delta: first pitch of a new inning, but NOT also the first
# pitch of a new game (that case belongs to Between-Game Δ instead - mirrors
# between_inning_deltas' own game-scoped groupby, which never sees a game's
# first inning at all).
ind["Between-Inning Δ"] = (
    ["bi" if (bool(fp_inn[i]) and not bool(fp_app[i])) else None for i in range(n)],
    da100, len(_DELTA_HM_BINS) - 1)
```

`fp_app`/`fp_inn` are already computed at `utils.py:4272-4273`. No new columns,
no new function parameters.

### A3. `_recency_labelers` - new rows (`utils.py:4375-4427`)

```python
"Between-Game Δ":   (lambda c: "New game",   delta100),
"Between-Inning Δ": (lambda c: "New inning", delta100),
```

Add next to the `"Prior diff → Δ"` row (`utils.py:4421`).

### A4. `_STOPLIGHT_ORDER` (`pages/2_Scouting.py:272-274`)

Append at the end (after `"1st pitch inning"`):

```python
_STOPLIGHT_ORDER = _OBP_STOPLIGHT_SIGNALS + [
    "2-pitch seq", "3-pitch seq", "2-Δ seq", "3-Δ seq", "2-Δ² seq", "3-Δ² seq",
    "Prior diff → Δ", "Prior result → Δ", "Result seq → Δ",
    "Outs", "Base state", "1st pitch appearance", "1st pitch inning",
    "Between-Game Δ", "Between-Inning Δ"]
```

(This one line also covers Stage B/C's two new signals - see those stages for
where they're built.) No changes to `_load_pitcher_stoplights` /
`_load_pitcher_stoplight_detail` / `_stoplight_inspector` signatures - unlike
the Δ² feature, none of these six new signals need a new bucket-size cache-key
parameter (all use the fixed `_DELTA_HM_BINS`, same as `"Prior diff → Δ"`).

### A5. Page wiring (`pages/2_Scouting.py`)

`_show_fp_app_p` / `_show_fp_inn_p` are computed at lines 1757-1769, *after* the
current `"Prior diff → Δ"` append block (1707-1710). Insert the two new
Suggestion-row appends right after that computation, before the existing
`"1st pitch inning"` block (line 1771):

```python
if _show_fp_app_p:
    _h = utils.between_game_delta_hint(df_p, "pitch")
    if _h:
        _hint_rows_p.append(_delta_row_p("Between-Game Δ", _h, 5))

if _show_fp_inn_p and not _show_fp_app_p:
    _h = utils.between_inning_delta_hint(df_p, "pitch")
    if _h:
        _hint_rows_p.append(_delta_row_p("Between-Inning Δ", _h, 5))
```

`_show_fp_app_p`/`_show_fp_inn_p` are exactly the right gates already: the
former is true only when the upcoming game differs from the pitcher's last
recorded game (a new "appearance"); the latter is true when either the game
*or* the inning differs. `and not _show_fp_app_p` on the second isolates the
"same game, new inning" case, matching `between_inning_deltas`' own game-scoped
semantics (Stage A2's rationale).

---

## Stage B - Item 3: Prior result → Δ

### B1. `utils.py` heatmap - place after `diff_vs_next_pitch_delta_heatmap`
(`utils.py:6371-6470`)

Copy that function's body with these changes:
- Filter on `df[value_col].notna() & df["result"].notna()` instead of `...&
  df["diff"].notna()`.
- Categorize with `df_sw["_res_cat"] = pd.Categorical(df_sw["result"].map(seq_result_category),
  categories=_SEQ_RESULT_CATEGORIES)` instead of the `pd.cut(...diff...)` line.
- `pd.crosstab(df_sw["_delta_cat"], df_sw["_res_cat"]).reindex(index=_DELTA_HM_LABELS,
  columns=_SEQ_RESULT_CATEGORIES, fill_value=0)` instead of reindexing on
  `_DIFF_HM_LABELS`.
- `x=_SEQ_RESULT_CATEGORIES` on the `go.Heatmap` call instead of `x=_DIFF_HM_LABELS`.
- Column-count annotation loop iterates `_SEQ_RESULT_CATEGORIES` instead of
  `_DIFF_HM_LABELS`.
- `xaxis=dict(title="Prior result")` instead of `"Prior diff (abs)"`.
- Hovertemplate: `"Prior result: %{x}<br>Next pitch |Δ|: %{y}<br>..."`.
- Everything else (row-count annotations, normalization, colorscale, layout,
  `xgap`/`ygap`) identical.

```python
def next_pitch_delta_vs_prior_result_heatmap(
    df: pd.DataFrame,
    title: str = "Next Pitch |Δ| vs Prior Result",
    value_col: str = "pitch",
) -> go.Figure:
    """Heatmap: unsigned next-value delta vs the PREVIOUS plate appearance's
    result category (XBH / BB-1B / Out / K+, see seq_result_category).

    X = prior result category (fixed best->worst order); Y = abs circular delta
    to next value (0 at bottom, 500 at top). Only consecutive plate appearances
    from the same player within the same game are counted.
    """
    ...
```

### B2. `utils.py` hint function - place after `diff_to_delta_hint` (`utils.py:3823-3867`)

```python
def prior_result_delta_hint(df: pd.DataFrame, value_col: str, prior_category: str) -> dict | None:
    """Most likely next |Δ| (fixed 100-unit bins) given the PREVIOUS plate
    appearance's result category (XBH / BB-1B / Out / K+)."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna() & df["result"].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s["_nv"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_nv"])
    if df_s.empty:
        return None
    df_s["_nd"] = df_s.apply(lambda r: circular_diff(int(r[value_col]), int(r["_nv"])), axis=1)
    df_s["_nc"] = pd.cut(df_s["_nd"], bins=_DELTA_HM_BINS, labels=False, right=True, include_lowest=True)
    df_s = df_s.dropna(subset=["_nc"])
    if df_s.empty:
        return None
    df_s["_rescat"] = df_s["result"].map(seq_result_category)
    col_data = df_s[df_s["_rescat"] == prior_category]["_nc"].astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {"delta_lo": best_bkt * delta_step, "delta_hi": (best_bkt + 1) * delta_step,
            "prob": counts.iloc[0] / len(col_data), "n": int(len(col_data)),
            "all_counts": all_counts, "delta_bucket_size": delta_step}
```

Note this predicts the delta from *this* plate appearance's pitch to the *next*
one, conditioned on *this* PA's own result - i.e. "given how the last PA just
ended, what's the pitcher's next pitch likely to look like relative to the one
they just threw." Matches Alex's "based on the previous result from the last PA"
exactly.

### B3. `_recency_indications` - new entry (`utils.py:4207-4320`)

Build the per-row category array once, right after the `diff_bkt`/`fb` block
(`utils.py:4266-4280`):

```python
rescat = ([seq_result_category(r) if pd.notna(r) else None for r in sw["result"]]
          if "result" in sw.columns else [None] * n)
```

New entry after `ind["Prior diff → Δ"]` (`utils.py:4304-4305`):

```python
ind["Prior result → Δ"] = (
    [rescat[i - 1] if i >= 1 else None for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
```

No game-boundary gating on the context (matches `"Prior diff → Δ"`'s own
un-gated `fb[i-1]` context) - the *outcome* (`d100[i]`) is what's naturally
NaN'd across a game boundary via the existing `same1` masking upstream, exactly
as it already is for `"Prior diff → Δ"`. Same behavior, same reasoning, zero new
edge cases.

### B4. `_recency_labelers` - new row (`utils.py:4375-4427`)

```python
"Prior result → Δ": (lambda c: c, delta100),
```

### B5. Page wiring (`pages/2_Scouting.py`)

History vars - add next to `_h_prior_diff` (`pages/2_Scouting.py:1561-1571`):

```python
_h_result_hist = (
    df_p[df_p["result"].notna()].sort_values("id")["result"].tolist()
) if "result" in df_p.columns else []
_h_prior_rescat  = utils.seq_result_category(_h_result_hist[-1]) if _h_result_hist else None
_h_prior_rescat2 = utils.seq_result_category(_h_result_hist[-2]) if len(_h_result_hist) >= 2 else None
```

Suggestion row - add right after the `"Prior diff → Δ"` block (`pages/2_Scouting.py:1707-1710`),
before the `"2-pitch seq"` block:

```python
if _h_prior_rescat is not None:
    _h = utils.prior_result_delta_hint(df_p, "pitch", _h_prior_rescat)
    if _h:
        _hint_rows_p.append(_delta_row_p("Prior result → Δ", _h, 5))
```

(The `"Result seq → Δ"` append, using `_h_prior_rescat2` too, is Stage C5 -
placed immediately after this block.)

Pitch Analysis UI - insert right after the existing `"Next Pitch Delta vs Prior
Diff"` block ends (`pages/2_Scouting.py:2239-2245`), before the `"Hot Zone Pitch
Matrix"` divider (line 2248):

```python
st.divider()
st.subheader("Next Pitch Delta vs Prior Result")
st.caption("How does a pitcher adjust their next pitch based on how the previous plate appearance ended?")
st.plotly_chart(
    utils.next_pitch_delta_vs_prior_result_heatmap(df_p, title="Next Pitch Δ vs Prior Result"),
    width="stretch", config={"displayModeBar": False}, key="p_result_delta_hm",
)
```

(Item 4's 2-step view, Stage C6, goes immediately after this.)

---

## Stage C - Item 4: Result seq → Δ

### C1. `utils.py` shared context helper - place after `prior_result_delta_hint` (Stage B2)

Both the hint function and the distribution-view function need the identical
"pair of consecutive same-game result categories -> next |Δ| bucket" frame.
Factor it once (mirrors how `_fresh_delta2_frame` is shared across four Δ²
functions in the earlier plan):

```python
def _result_seq_context(df: pd.DataFrame, value_col: str) -> pd.DataFrame | None:
    """Fresh recompute: one row per plate appearance with '_cat' (this row's own
    result category), '_prev_cat' (the immediately preceding SAME-GAME row's
    category), and '_nc' (next-pitch |Δ| bucket, fixed 100-unit bins). Shared by
    result_seq_delta_hint and result_seq_delta_dist so the two views can never
    drift out of sync with each other."""
    group_col = "pitcher_name" if value_col == "pitch" else "batter_name"
    df_s = df[df[value_col].notna() & df["result"].notna()].sort_values(["game_id", group_col, "id"]).copy()
    df_s["_cat"] = df_s["result"].map(seq_result_category)
    df_s["_prev_cat"] = df_s.groupby(["game_id", group_col])["_cat"].shift(1)
    df_s["_nv"] = df_s.groupby(["game_id", group_col])[value_col].shift(-1)
    df_s = df_s.dropna(subset=["_nv", "_prev_cat"])
    if df_s.empty:
        return None
    df_s["_nd"] = df_s.apply(lambda r: circular_diff(int(r[value_col]), int(r["_nv"])), axis=1)
    df_s["_nc"] = pd.cut(df_s["_nd"], bins=_DELTA_HM_BINS, labels=False, right=True, include_lowest=True)
    df_s = df_s.dropna(subset=["_nc"])
    return df_s if not df_s.empty else None
```

`_prev_cat`'s `groupby(...).shift(1)` naturally returns NaN at each game's first
row (dropped by the `dropna`), giving the same-game guarantee for the pair
without any explicit boolean mask - same two-stage discipline used everywhere
else in this codebase (`_fresh_delta2_frame`'s stage-1/stage-2 NaN propagation
is the canonical example).

### C2. `utils.py` hint function - place right after `_result_seq_context`

```python
def result_seq_delta_hint(df: pd.DataFrame, value_col: str,
                          prior_cat_older: str, prior_cat_newer: str) -> dict | None:
    """Most likely next |Δ| (fixed 100-unit bins) given the result categories of
    the two most recent plate appearances (older first, newer/most-recent
    second - same argument order convention as seq3_delta_hint)."""
    df_s = _result_seq_context(df, value_col)
    if df_s is None:
        return None
    col_data = df_s[(df_s["_prev_cat"] == prior_cat_older) &
                    (df_s["_cat"] == prior_cat_newer)]["_nc"].astype(int)
    if col_data.empty:
        return None
    counts = col_data.value_counts()
    best_bkt = int(counts.index[0])
    delta_step = _DELTA_HM_BINS[1] - _DELTA_HM_BINS[0]
    n_d_bkts = len(_DELTA_HM_BINS) - 1
    all_counts = [int(counts.get(i, 0)) for i in range(n_d_bkts)]
    return {"delta_lo": best_bkt * delta_step, "delta_hi": (best_bkt + 1) * delta_step,
            "prob": counts.iloc[0] / len(col_data), "n": int(len(col_data)),
            "all_counts": all_counts, "delta_bucket_size": delta_step}
```

### C3. `utils.py` distribution-view function - place right after `result_seq_delta_hint`

Mirrors `delta_third_dist`'s single-row-heatmap rendering (`utils.py:6955-7040`)
exactly, but the "cell" being inspected is a (older, newer) category pair
instead of an (init, follow) |Δ| bucket pair:

```python
def result_seq_delta_dist(df: pd.DataFrame, value_col: str,
                          prior_cat_older: str, prior_cat_newer: str) -> go.Figure | None:
    """Single-row heatmap: distribution of the next |Δ| given the result
    categories of the two most recent plate appearances. Returns None when
    there is no data for the given category pair."""
    df_s = _result_seq_context(df, value_col)
    if df_s is None:
        return None
    subset = df_s[(df_s["_prev_cat"] == prior_cat_older) & (df_s["_cat"] == prior_cat_newer)]
    if subset.empty:
        return None
    counts = subset["_nc"].astype(int).value_counts().reindex(range(len(_DELTA_HM_LABELS)), fill_value=0)
    total = int(counts.sum())
    pcts = counts / total * 100 if total > 0 else counts * 0.0
    text = [[f"{pcts[i]:.0f}%" if counts[i] > 0 else "" for i in range(len(_DELTA_HM_LABELS))]]
    fig = go.Figure(go.Heatmap(
        z=[pcts.values.tolist()], x=_DELTA_HM_LABELS, y=["Next |Δ|"],
        text=text, texttemplate="%{text}", customdata=[counts.values.tolist()],
        colorscale=[[0, "#2166ac"], [0.5, "#ffffff"], [1, "#d6604d"]],
        showscale=False, xgap=2, ygap=2,
        hovertemplate="%{x}<br>%{z:.1f}% (%{customdata} instances)<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=f"Next |Δ|  |  {prior_cat_older} → {prior_cat_newer}  (n={total})",
                   x=0.5, xanchor="center", font=dict(size=13)),
        xaxis=dict(title=None, side="bottom"),
        yaxis=dict(showticklabels=True),
        height=130, margin=dict(l=80, r=10, t=55, b=40),
        dragmode=False,
        modebar_remove=["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d",
                        "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"],
    )
    return fig
```

(Only 5 x-labels here, always - unlike `delta_third_dist`'s variable bucket
count, so skip the `_rotate` tick-angle branching entirely; fixed
`height=130`/no rotation.)

### C4. `_recency_indications` - new entry (`utils.py:4207-4320`)

Reuses `rescat` (Stage B3) and `same2` (already computed, `utils.py:4226-4228`).
Add right after `ind["Prior result → Δ"]`:

```python
ind["Result seq → Δ"] = (
    [(rescat[i - 2], rescat[i - 1])
     if (i >= 2 and same2[i] and rescat[i - 2] is not None and rescat[i - 1] is not None) else None
     for i in range(n)], d100, len(_DELTA_HM_BINS) - 1)
```

`same2[i]` (all three of rows i-2, i-1, i in the same game) is the guard here -
deliberately more conservative than `"Prior result → Δ"`'s ungated single-step
context. Rationale: a two-event momentum read ("XBH then XBH", "Out then Out")
should reflect an actual in-game sequence, not two results that happen to be
adjacent in career order but come from different games (rest days / different
opponents would contaminate the read). This mirrors why the Δ² feature's
`same2` guard exists (`delta2-sequence-plan.md` Stage 3a) and why
`_result_seq_context`'s per-game `groupby(...).shift(1)` naturally enforces the
same thing at the hint-function level (Stage C1).

### C5. `_recency_labelers` - new row (`utils.py:4375-4427`)

```python
"Result seq → Δ": (pair(lambda c: c), delta100),
```

(`pair(fmt)` already exists, `utils.py:4404-4405`.)

### C6. Page wiring (`pages/2_Scouting.py`)

Suggestion row - immediately after Stage B5's `"Prior result → Δ"` append
block:

```python
if _h_prior_rescat is not None and _h_prior_rescat2 is not None:
    _h = utils.result_seq_delta_hint(df_p, "pitch", _h_prior_rescat2, _h_prior_rescat)
    if _h:
        _hint_rows_p.append(_delta_row_p("Result seq → Δ", _h, 5))
```

(Argument order mirrors the existing `seq3_delta_hint` call at line 1693:
older value first, most-recent second.)

Pitch Analysis UI - immediately after Stage B5's new heatmap block, still
before the `"Hot Zone Pitch Matrix"` divider:

```python
st.divider()
st.subheader("Next Pitch Delta following a Result Sequence")
st.caption("Two plate appearances ago, then the most recent one - what does the pitcher's next adjustment look like?")
_rs_c1_p, _rs_c2_p = st.columns(2)
with _rs_c1_p:
    _rs_older_p = st.selectbox("Result 2 PAs ago", utils._SEQ_RESULT_CATEGORIES, key="rs_older_p")
with _rs_c2_p:
    _rs_newer_p = st.selectbox("Most recent result", utils._SEQ_RESULT_CATEGORIES, key="rs_newer_p")
_rs_fig_p = utils.result_seq_delta_dist(df_p, "pitch", _rs_older_p, _rs_newer_p)
if _rs_fig_p:
    st.plotly_chart(_rs_fig_p, width="stretch", config={"displayModeBar": False}, key="p_result_seq_dist")
else:
    st.caption("Not enough data for this result sequence.")
```

Default the two selectboxes to the pitcher's actual last two result categories
(`_h_prior_rescat2`, `_h_prior_rescat`) rather than always defaulting to index 0,
mirroring how the Δ/Δ² Initial/Following inputs default from the pitcher's own
history (`pages/2_Scouting.py:2170-2171`, `2211-2212`) - use `st.selectbox(...,
index=utils._SEQ_RESULT_CATEGORIES.index(_h_prior_rescat2) if _h_prior_rescat2
else 0, ...)` and similarly for the second box. Guard the `.index(...)` call in
case history is too short (falls back to `0`).

Exposing `utils._SEQ_RESULT_CATEGORIES` (currently named with a leading
underscore, i.e. "private") from a page module is a minor style wrinkle -
flagging it as a call for Opus to either rename it public (drop the underscore)
or add a tiny public alias/getter. Not worth blocking the plan on; either
resolution is fine.

---

## Stage D - Validation

1. **Compile/boot**: `python -m py_compile utils.py pages/2_Scouting.py`, then
   `streamlit run` and load the Scouting page for a pitcher with a long history
   across multiple games.
2. **Taxonomy hand-check**: in a scratch REPL, call `utils.seq_result_category`
   on a handful of results spanning all four buckets - `"HR"` -> XBH, `"1B"` ->
   BB-1B, `"GO"` -> Out, `"K"` -> K+, `"DP21"` -> K+, `"SacB"` -> Out (via the
   bunt remap to `"SacF"`), and an unrecognized string -> Out (default).
3. **Heatmap sanity (item 3)**: pick a heavily-scouted pitcher, confirm
   `next_pitch_delta_vs_prior_result_heatmap` renders with all four columns
   populated (not all-zero in any column for a pitcher with enough history), and
   spot-check a hover cell against a manual `pandas` crosstab built the same way.
4. **Distribution view sanity (item 4)**: pick a (older, newer) pair you know has
   several instances for the test pitcher (e.g. "Out", "Out"), confirm
   `result_seq_delta_dist` renders and its total `n` matches a manual filter of
   the same pitcher's plate-by-play data by hand-walking a few games.
5. **Suggestions worked example**: for a pitcher whose last PA result and last
   two PA results are known, confirm `"Prior result → Δ"` and `"Result seq → Δ"`
   rows appear in Swing Suggestions with sensible green zones (cross-check the
   predicted |Δ| bucket against the corresponding heatmap column / dist-view
   cell for the same context - they should agree, since both read from
   equivalent freshly-recomputed frames).
6. **Between-game/inning gating**: manually walk through the "Fetch Live
   Matchup" or "Historical/Manual" flow to a state where the upcoming pitch is
   (a) the first of a brand-new game for this pitcher, (b) the first of a new
   inning in an already-in-progress game, (c) neither. Confirm `"Between-Game Δ"`
   appears only in case (a), `"Between-Inning Δ"` only in case (b), and neither
   appears in case (c) - matching how `"1st pitch appearance"`/`"1st pitch
   inning"` already behave for the same gating variables.
7. **Stoplight/Inspector discipline**: in the Inspector, select each of the six
   new signals in turn. Confirm (a) `n_scored` is sane (between-game/inning
   signals should have noticeably fewer scored events than the per-pitch
   signals - spot-check by counting `is_fp_app`/`is_fp_inn` rows by hand for one
   pitcher); (b) Context/Observed labels render as expected strings ("New game"/
   "New inning" for items 1-2, "XBH"/"BB-1B"/"Out"/"K+" and pairs thereof for
   items 3-4, delta ranges for all outcomes); (c) green/yellow/red states use
   the same window_n/vote-threshold machinery as every other signal (shared
   `_aggregate_recency`/`_surprisal_walk` - no special-casing needed, verify by
   eyeballing the drill-down table's score scale matches other signals').
8. **Regression sweep**: every existing signal (`"2-pitch seq"` through `"1st
   pitch inning"`, all Δ/Δ² rows, `"Prior diff → Δ"`, the existing Pitch
   Analysis heatmaps) renders bit-for-bit unchanged at default state; batter tab
   is completely untouched (none of its call sites reference anything new here);
   `python scripts/calibrate_stoplight_thresholds.py` still imports and runs (it
   iterates whatever `_recency_indications` returns, so it picks up the six new
   signals automatically - no script changes needed, but worth re-running once
   to confirm it doesn't choke on the new `str`-valued context keys used by
   items 3-4, since every other indication's context keys are `int`/tuple-of-int
   or a small fixed string set like `"empty"`/`"runners"`/`"fpa"`/`"fpi"` - the
   new `"XBH"`/`"BB-1B"`/`"Out"`/`"K+"` string keys should behave identically to
   those, but confirm rather than assume).

---

## Open questions / risks flagged for Opus (do not silently resolve)

1. **`utils._SEQ_RESULT_CATEGORIES` privacy** (Stage C6) - trivial naming
   wrinkle, either fix is fine, see above.
2. **`.apply(..., axis=1)` for `circular_diff`** - every new/reused function here
   that computes a next-delta (`prior_result_delta_hint`, `_result_seq_context`,
   `next_pitch_delta_vs_prior_result_heatmap`) uses the same row-wise `.apply`
   pattern already used by `diff_to_delta_hint`/`diff_vs_next_pitch_delta_heatmap`.
   That's consistent with precedent, but it's the same performance profile as
   those existing functions (O(n) Python-level calls, not vectorized) - not a
   regression, just flagging that if a future perf pass ever vectorizes
   `circular_diff` call sites, these three new ones should be included.
3. **Result strings outside `_RESULT_ZONE_COLORS`** - `seq_result_category`
   defaults anything unrecognized to `"Out"`. If this league's actual `result`
   column ever contains a string not in `_RESULT_ZONE_COLORS`, `_BUNT_TO_SWING`,
   `_XBH`, `_BB1B`, or the manual `{"KCS","BDP"}` addition (e.g. a future new
   result type), it will silently land in "Out" rather than erroring. Matches
   `get_res_category`'s existing default-to-OUT behavior, so consistent with
   precedent, but worth a one-time sanity check against the real `plays` table's
   distinct `result` values before shipping (`SELECT DISTINCT result FROM plays`
   or equivalent, diffed against `_RESULT_ZONE_COLORS`'s keys) to make sure
   nothing common is falling into the default bucket unintentionally.
4. **Sample sizes for `"Result seq → Δ"` / item 4's dist view** - a same-game
   two-step categorical sequence is a fairly narrow slice (4x4 = 16 possible
   pair combinations, further split into 5 outcome buckets = 80 cells total)
   competing for a single pitcher's career sample. Expect many (older, newer)
   pairs to have thin or zero data for pitchers who haven't been scouted
   extensively - the existing "Not enough data" caption fallback (Stage C6)
   handles this gracefully, but don't be surprised if this row/view is often
   empty for lightly-scouted pitchers. No code change implied - just a
   heads-up so it isn't mistaken for a bug during validation.

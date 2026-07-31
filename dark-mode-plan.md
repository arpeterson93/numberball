# Dark mode - implementation plan

Target: `docs/` (the static Key Moments feed - `docs/index.html`, `docs/css/style.css`,
`docs/js/app.js`, `docs/js/favorites.js`). No Python/build changes needed - this is
entirely a client-side feature.

## Goal

A manual light/dark toggle in the UI, defaulting to the visitor's OS preference on
first visit, that overrides it once they've picked explicitly. Persisted per-device
via localStorage, following the same pattern `favorites.js` already uses for its
`km_display_name`/`km_favorite_ids` keys.

## Open decision before starting: sync scope

The user asked whether this should be "stored like favorites," which today means two
things bundled together:

1. **localStorage** - instant, per-device, always on.
2. **Apps Script sync keyed by display name** - so the same typed name shares state
   across devices (see `docs/js/favorites.js` and `apps-script/DEPLOY.md`).

**Recommendation: localStorage only, skip (2).** A favorites list represents real
curation effort worth protecting across devices; a theme preference is a single toggle
that costs nothing to re-set on a new device. Adding it to the Apps Script payload
also couples an unrelated concern (visual preference) to the favorites backend's
data model for little benefit. If the user pushes back and wants cross-device sync,
see "Optional: cross-device sync" at the end - it's a small extension of the same
`favorites.js` save/load path, not a redesign.

Confirm this with the user before or during implementation; the rest of this plan
assumes localStorage-only.

## Step 1 - CSS variable inventory

`docs/css/style.css` already centralizes its palette in `:root`:

```css
:root{
  --navy: #0f2238;
  --navy-2: #16233a;
  --red: #c8102e;
  --red-dark: #a10d26;
  --blue: #1550a1;
  --bg: #eef1f4;
  --card: #ffffff;
  --border: #e0e4e9;
  --text: #1a1d24;
  --muted: #6b7280;
  --green: #1e7d32;
  --gold: #d9a300;
}
```

That's the easy part - a `:root[data-theme="dark"]{ ... }` block redefining these
twelve covers most of the page for free, since the majority of rules already
reference them via `var(...)`.

The problem is the rules that **don't**. Grepped inventory of every hardcoded hex in
the file today (line numbers will drift as the file changes - re-grep with
`grep -noE '#[0-9a-fA-F]{3,6}' docs/css/style.css` before starting):

| Selector | Hardcoded value(s) | What it needs |
|---|---|---|
| `.session-select` | `background:#fff` | → `var(--card)` |
| `.refresh-btn` | `background:#fff` | → `var(--card)` |
| `.scoreboard-tile:hover` | `border-color:#c7ccd3` | new `--border-hover` var or dark-specific override |
| `.sb-lev` | `background:#f3f5f8` | new `--tint` var (light neutral fill, used 4x below) |
| `.sb-lev.hot` | `background:#fdeef0;border-color:#f0b8c0` | new `--tint-red`/`--tint-red-border` vars |
| `.player-suggest-row:hover, .active` | `background:#f3f5f8` | reuse `--tint` |
| `.chip` | `background:#fff` | → `var(--card)` |
| `.chip.active` | `color:#fff` | fine as literal white in both themes (active chip bg is `--red`, always dark enough) - verify contrast, likely no change needed |
| `.chip:disabled.active` | `background:#fff` | → `var(--card)` |
| `select, input[type=text]` | `background:#fff` | → `var(--card)` |
| `.lev-bar.neutral` | `background:#fff` | → `var(--card)` |
| `.star-btn` | `color:#c7ccd3` | new `--star-off` var |
| `.result-pill.offense, .diff-pill.zero` | `border-color:#b9d0f0;background:#eef4fd` | new `--tint-blue`/`--tint-blue-border` vars |
| `.result-pill.defense, .diff-pill.five` | `border-color:#f0b8c0;background:#fdeef0` | reuse `--tint-red`/`--tint-red-border` from above |
| `.why-tag` | `background:#f3f5f8` | reuse `--tint` |
| `.base-diamond.off` | `fill:#fff;stroke:#c7ccd3` | new `--diamond-off-fill`/`--diamond-off-stroke` vars - **white fill will look wrong on a dark card**, this is the one guaranteed-visible bug if skipped |
| `.outs-dots .dot` | `border:1px solid #c7ccd3;background:#fff` | reuse diamond-off vars or a shared `--dot-off` |
| `.state-badge` | `background:#f3f5f8` | reuse `--tint` |
| `.state-badge.final` | `color:#fff` | fine as literal (bg is `--navy`, always dark) |
| `.toast` | `color:#fff` | fine as literal (bg is `--navy`) |

Net new variables to add to `:root` (light values) and redefine under
`:root[data-theme="dark"]` (dark values):

```
--tint            (neutral fill: badges, hover rows, tag pills)
--tint-red        / --tint-red-border    (defense pill, hot leverage badge)
--tint-blue       / --tint-blue-border   (offense pill)
--border-hover    (scoreboard tile hover border)
--star-off        (unfilled star icon)
--diamond-off-fill / --diamond-off-stroke  (empty base + empty outs dot)
```

Do the sweep by replacing every hardcoded value above with the matching variable
first (functionally a no-op refactor - re-screenshot to confirm nothing visually
changed in light mode), *then* add the dark block. Keeping those as separate commits
makes it trivial to tell "did I break light mode" from "does dark mode look right"
if something's off.

## Step 2 - the dark palette itself

No fixed values prescribed here - pick something that reads as a deliberate dark
theme, not just inverted light-mode values (e.g. `--bg` shouldn't be pure black,
`--card` should sit one step above it, `--border` needs enough contrast against both
to still read as a border). Rough shape to aim for, adjust by eye:

```css
:root[data-theme="dark"]{
  --bg: #12161c;
  --card: #1a2029;
  --border: #2b323d;
  --text: #e8eaed;
  --muted: #9aa4b2;
  /* --navy/--navy-2/--red/--red-dark/--blue/--green/--gold likely stay as-is or
     get slightly desaturated/lightened for contrast against the darker --card -
     check each one against WCAG contrast on the new --card, not the old --bg */
  --tint: #232b36;
  --tint-red: #3a1e22; --tint-red-border: #5c2c33;
  --tint-blue: #1c2a3d; --tint-blue-border: #2c3f57;
  --border-hover: #3a4250;
  --star-off: #4a5361;
  --diamond-off-fill: #1a2029; --diamond-off-stroke: #4a5361;
}
```

Treat these as a starting point, not a spec - the actual QA pass (Step 5) is what
determines whether they're right.

## Step 3 - team-colored elements (no CSS change, just verify)

`teamColor()` / `teamLogoImg()` in `docs/js/app.js` pull `primary_hex` and `logo_url`
straight from `data.meta.teams` and inline them via `style="background:..."` /
`<img src=...>` - these are data-driven, not part of the light/dark palette, and need
no code change. Two things to actually verify visually once dark mode exists:

- **Team hex colors against the new dark backgrounds** - `.lev-bar`, `.wp-seg`, the
  scoreboard's WP split bar. A light pastel team color that read fine on white may
  wash out or clash on the new `--card`. This is inherent to team-color data, not
  fixable in CSS - just confirm nothing becomes unreadable, and if a specific team's
  hex is a problem, that's a data issue not a code issue.
- **Team logos** - hotlinked PNGs from Postimg, background unknown per-image. Spot
  check a few in dark mode; a logo with a transparent background and dark line art
  could disappear against the new dark `--card`. If problematic, the cheap fix is a
  small white/light circular backdrop behind `.team-logo-inline-img` /
  `.sb-logo` in dark mode only - don't do this preemptively, only if the QA pass
  actually shows a broken logo.

## Step 4 - the toggle itself

New file `docs/js/theme.js`, self-contained IIFE exposing `window.KMTheme`, mirroring
`favorites.js`'s shape:

```js
(function () {
  "use strict";
  var KEY = "km_theme";  // stored value: "light" | "dark" - absent means "follow OS"

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function apply(theme) {
    // theme is "light" | "dark" - the resolved value, not the stored preference
    document.documentElement.setAttribute("data-theme", theme);
  }

  function resolve() {
    return stored() || (systemPrefersDark() ? "dark" : "light");
  }

  function set(theme) {
    // theme: "light" | "dark" | null (null = clear override, follow OS again)
    try {
      if (theme) window.localStorage.setItem(KEY, theme);
      else window.localStorage.removeItem(KEY);
    } catch (e) { /* private browsing - in-memory only for this load */ }
    apply(theme || (systemPrefersDark() ? "dark" : "light"));
  }

  function toggle() {
    set(resolve() === "dark" ? "light" : "dark");
  }

  apply(resolve());  // as early as possible - see index.html note below

  window.KMTheme = { toggle: toggle, current: resolve, set: set };
})();
```

**Flash-of-wrong-theme note**: `favorites.js`/`app.js` load at the end of `<body>`,
which is fine for their job but would cause a visible light→dark flash on every dark
mode load if `theme.js` waited that long. Load `theme.js` in `<head>`, before
`css/style.css`'s `<link>` if possible (or immediately after) - it's a tiny synchronous
script, the cost is negligible, and applying `data-theme` before first paint is the
whole point.

`docs/index.html` changes:
- Add `<script src="js/theme.js"></script>` in `<head>`, before the stylesheet link.
- Add a toggle button somewhere in `.title-actions` (next to `#built-at` /
  `#refresh-btn` - that row already exists for page-level controls, matches its
  purpose). Something like:
  ```html
  <button type="button" class="theme-toggle" id="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode">🌙</button>
  ```
  Wire it in `app.js`'s `wireControls()`: `$("theme-toggle").addEventListener("click", window.KMTheme.toggle);`
  Swap the icon (🌙/☀️) based on `window.KMTheme.current()` on click and on boot.

## Step 5 - QA checklist

Screenshot both themes at desktop (1280px) and phone (480px) width, same approach
used earlier in this session (Playwright + `python -m http.server 8765 --directory docs`).
Check every one of these surfaces in dark mode specifically, since each has its own
background/border assumptions:

- [ ] Filters card - all chip states (active/inactive/disabled - the Side group's
      disabled state especially, it's a subtle opacity effect that may need retuning)
- [ ] Play card - result pills, diff pills, why-tags, WPA color text (green/red on
      the new `--card`), the leverage bar, base diamond (on **and** off states),
      outs dots (on **and** off states)
- [ ] Scoreboard tile - WP bar, leverage badge (hot **and** neutral), FINAL/FINAL-N
      badge, selected-tile border, team logos
- [ ] Favorites modal - overlay scrim, list rows, search input
- [ ] Toast
- [ ] Player suggestion dropdown, hover state
- [ ] Empty state, loading state
- [ ] Toggle itself - icon swap, and that a hard refresh doesn't flash light before
      dark (see Step 4 note)

## Effort estimate

- Step 1 (variable sweep, no-op refactor): mechanical, ~20-30 min once the inventory
  above is in hand.
- Step 2 (actual dark values + Step 3 verification): the real judgment-call work,
  probably a few iterations against screenshots. Budget the most time here.
- Step 4 (toggle + persistence): small, ~15 min, direct precedent to copy from.
- Step 5 (QA pass): as thorough as the checklist above, budget real time - this is
  where a rushed job shows.

Whole thing is a half-day task for someone unfamiliar with the codebase, less for
someone who's already touched `style.css` recently (i.e. this session's context).

## Optional: cross-device sync (only if the user asks for it)

If localStorage-only turns out to not be enough, `favorites.js`'s `save()`/`load()`
already POST/GET a JSON payload to the Apps Script endpoint keyed by
`state.key` (the slugified display name). Adding a `theme` field to that same
payload and reading it back in `KMTheme` on `KMFavorites`-name-set would work, but
means `theme.js` now depends on a display name being set first (todayable via
`KMFavorites.name()`) - which reintroduces the flash-of-wrong-theme problem, since
that name isn't known until `localStorage` is read in `favorites.js`'s own init.
This is meaningfully more complexity than it's worth for a preference this cheap to
re-set; only take this on if explicitly requested.

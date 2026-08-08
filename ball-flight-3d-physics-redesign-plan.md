# Ball-flight physics + 3D-perspective redesign - implementation plan

Prepared 2026-08-07 in response to `ball-flight-3d-physics-redesign-fable-prompt.md`.
Written for Sonnet to execute. Everything below marked **VERIFIED** was checked by
probe scripts this session (workbook formula extraction via openpyxl, a Python
reimplementation validated against the workbook's own cached outputs, and an
angle-convention probe); nothing load-bearing is assumed.

Sources read in full: `physics-model-overview-for-redesign.md`,
`opus-physics-review-findings.md` (F1-F11), `opus-physics-review-prompt.md`,
`docs/js/app.js` (flight/scene code, ~980-2990), `result_diff_bands.csv`,
`compute_flight_ranges.py`, `ball_flight_test.py`, `import_BRC.csv`,
`docs/css/style.css` (animation/reduced-motion blocks),
`TrajectoryCalculator-new-3D-June2026.xlsx` (`BattedBallTrajectory-1`, formulas
extracted cell-by-cell), `baseballflightsim.jpg`.

Mission constraint, restated once and binding everywhere below: the result code,
`obc_before/obc_after/runs/outs_*` are immovable inputs. Every mechanism in this
plan only decides *how* an already-decided outcome gets its physical story. No
stage below may change which fielder the data requires, how many outs happen, or
whether a hit is a hit.

---

## Part 0 - The verified physics spec (what the workbook actually computes)

This section is the port target. It was extracted formula-by-formula from
`BattedBallTrajectory-1` and **VERIFIED** by reimplementing it in Python and
reproducing the workbook's own cached outputs to 4 decimal places (all five:
xf, yf, hang time, distance, bearing - diff 0.0000 on each). The prompt's
physics reference is correct but incomplete in four places that matter; those
are called out inline. Cell coordinates are for re-verification only.

### 0.1 Constants and atmosphere (fixed defaults)

```
temp_F = 75          -> temp_C = (5/9)*(temp_F-32)
elev_ft = 0          -> elev_m = elev_ft/3.2808
pressure_inHg = 29.92 -> pressure_mm = 29.92*1000/39.37
RH = 50              (percent)
beta = 0.0001217     (per meter, barometric elevation decay)
SVP = 4.5841*exp((18.687 - temp_C/234.5)*temp_C/(257.14 + temp_C))      [D20]
rho_kg = 1.2929*(273/(temp_C+273)*(pressure_mm*exp(-beta*elev_m)
         - 0.3783*RH*SVP/100)/760)                                       [D2]
rho_lb = rho_kg*0.06261                                                  [D1]
mass_oz = 5.125, circ_in = 9.125
const = 0.07182*rho_lb*(5.125/mass_oz)*(circ_in/9.125)^2                 [D4]
       = 0.005316103027433 at these defaults (pin this in a test)
cd0 = 0.3008, cdspin = 0.0292                                            [G2,G3]
cl0 = 0.583, cl1 = 2.333, cl2 = 1.12                                     [G6-G8]
tau = 10000 s, dt = 0.01 s, g = 32.174 ft/s^2
```

Since the atmosphere never varies (no per-park data in MLN), **precompute
`const` as a literal** with a comment showing the derivation; do not ship the
humidity math to the browser.

### 0.2 Initial conditions

Workbook as shipped: `x0=0, y0=2, z0=6` (B3-B5). The y0=2 (contact point 2 ft
in front of the plate) is standard. **z0=6 ft is not** - Alan Nathan's public
calculator defaults to ~3 ft contact height, and 6 ft is likely a leftover
manual edit. Recommendation: app uses `CONTACT_HEIGHT_FT = 3.0`, `CONTACT_Y0_FT
= 2.0`, both exported constants. Golden test vectors below cover both z0=6 (the
workbook-parity vector) and z0=3 (app-default vectors). Open question OQ-1.

### 0.3 Inputs and derived spin

Inputs: `ev` (mph), `la` = theta (deg), `phi` (deg, see Part 1 for the
convention), `hand` ('R'/'L').

```
sign = (hand === 'R') ? +1 : -1                                          [D19]
backspin wb = -763 + 120*theta + 21*phi*sign        (rpm)                [G4]
sidespin ws = -sign*849 - 94*phi                    (rpm)                [G5]
spin = sqrt(wb^2 + ws^2)                            (rpm)                [I5]
v0 = ev*1.467                                       (ft/s)
v0x = v0*cos(theta)*sin(phi)
v0y = v0*cos(theta)*cos(phi)
v0z = v0*sin(theta)
```

**Spin vector decomposition (omitted from the prompt's reference; required):**

```
wx = ( wb*cos(phi) - ws*sin(theta)*sin(phi)) * pi/30    (rad/s)          [D10]
wy = (-wb*sin(phi) - ws*sin(theta)*cos(phi)) * pi/30                     [D11]
wz = ( ws*cos(theta)                       ) * pi/30                     [D12]
omega  = spin*pi/30                                  (rad/s)             [D13]
romega = (circ_in/(2*pi))*omega/12                   (ft/s)              [D14]
```

(The workbook also has a gyrospin term `wg`; it is 0 and can be dropped.)

### 0.4 Per-step computation (columns L-AE, rows 32+)

With wind = 0 the airspeed equals the speed: `vmag = sqrt(vx^2+vy^2+vz^2)`.

```
decay = exp(-t / (tau*146.7/vmag))
L  = spin * decay                       # spin magnitude, decayed      [L col]
cd = cd0 + (cdspin*L/1000)*decay        # NOTE decay applied twice     [P col]
M  = (L*pi/30)*(circ_in/(2*pi))/12      # transverse surface speed ft/s [M col]
S  = M/vmag                             # spin factor                  [Q col]
cl = cl2*S/(cl0 + cl1*S)                                               [R col]
adragx = -const*cd*vmag*vx   (same for y; z uses vz)                   [U-W]
k = const*(cl/omega)*vmag
aMagx = k*(wy*vz - wz*vy)                                              [Y]
aMagy = k*(wz*vx - wx*vz)                                              [Z]
aMagz = k*(wx*vy - wy*vx)                                              [AA]
ax = adragx + aMagx;  ay = ...;  az = adragz + aMagz - 32.174          [AB-AD]
```

Four details the prompt's reference under-specifies, all **VERIFIED** against
the sheet:

1. **`flag = 0` in the shipped workbook** (G10). The L-column formula is
   `sqrt(spin^2 - flag*(spin_parallel)^2)*decay` - with flag=0 the model uses
   **total** spin for both the Cd-spin term and the spin factor S, not the
   perpendicular component the prompt's reference describes. Port flag=0
   behavior (total spin); it is what the cached, Statcast-calibrated outputs
   use. Do not implement the perpendicular projection at all - dead code.
2. **The Magnus term uses the *initial* spin vector (wx,wy,wz) and initial
   total `omega` throughout the flight** - only the scalar magnitude in Cd/S
   decays. The spin axis never rotates. Replicate exactly.
3. **Cd double-applies the decay factor** (L already contains one `decay`, the
   P-column formula multiplies by `decay` again). With tau=10000 s this is
   ~1.0 for the whole flight (worst case exp(-2*7s/10000s) ≈ 0.9986), but
   replicate it anyway - bit-parity with the reference implementation is what
   makes the golden tests trivial. Do NOT "simplify" the decay away.
4. **Euler step order**: position uses the same row's acceleration
   (`x += vx*dt + 0.5*ax*dt^2`), then velocity (`vx += ax*dt`), then the next
   row recomputes accelerations from the new state. Replicate this exact
   order.

### 0.5 Landing detection and interpolation

The sheet flags the first row where `z_n > 0 && z_(n+1) <= 0` (AE column,
`height` resolves to an empty cell = 0) and linearly interpolates every output
between those two rows:

```
frac = z_(n+1) / (z_(n+1) - z_n)         # E26, with height=0
value_landing = value_(n+1) - frac*(value_(n+1) - value_n)
```

Apply this interpolation to t, x, y, **and vx, vy, vz** - the ground-contact
velocity vector is a first-class output (Task 3's input), not an afterthought.

### 0.6 Integration method and performance - decision

**Keep explicit Euler at dt = 0.01 s, exactly as the workbook.** Reasons:

- The Statcast calibration of cd0/cdspin/cl* was done *through* this
  integrator. A "better" integrator (RK4) would be a slightly *different*
  model than the one that was calibrated, and would break bit-parity with the
  Python reference that generates golden vectors.
- Cost is negligible: worst case ~660 steps (a 6.6 s pop-up), ~30 flops/step.
  The Python probe ran a full integration in ~1 ms; JS will be well under
  that. One integration per play render (plus one short re-run when a
  direction override fires) is invisible next to the existing per-slide
  innerHTML rebuild.
- Cap the loop at t = 15 s (safety bound; nothing real gets close).

### 0.7 Golden validation vectors (from the verified Python reference)

Pin these in the test suite. The first row is the workbook-parity vector (its
inputs are the workbook's own as-shipped inputs; expected values are the
workbook's cached outputs). All others use z0=3, y0=2.

| ev | la | phi | hand | z0 | dist ft | hang s | x ft | y ft |
|----|----|-----|------|----|---------|--------|------|------|
| 100 | 30 | 0 | L | 6 | 402.3273 | 5.6230 | -39.8523 | +400.3486 |
| 100 | 28 | 0 | R | 3 | 401.8992 | 5.3468 | +40.5529 | +399.8480 |
| 103 | 25 | +20 | R | 3 | 376.2823 | 4.6524 | +207.9698 | +313.5873 |
| 103 | 25 | -20 | L | 3 | 376.2823 | 4.6524 | -207.9698 | +313.5873 |
| 110 | 28 | 0 | R | 3 | 447.9520 | 5.8368 | +49.9129 | +445.1625 |
| 85 | -11 | 0 | R | 3 | 15.7691 | 0.1142 | +0.0460 | +15.7690 |

Contact-velocity components for the last two (Task 3's input contract):
EV 110/LA 28 lands with v = (+9.5229, +53.5836, -63.0522) ft/s; the EV 85/
LA -11 grounder lands with v = (+0.7954, +118.6677, -28.6847) ft/s.

(These are exact outputs of the Part 9 reference implementation; Sonnet can
regenerate/extend the set with that script when writing the JS parity tests.)

---

## Part 1 - Angle-convention mapping (VERIFIED, not assumed)

### The mapping

> **phi = HZ_angle - 45, and the batter's actual hand is passed to the spin
> formulas unchanged. Handedness is NOT applied a second time to phi.**

### Why this is right (probe evidence, not inference)

Both systems were checked for what they *physically* mean:

- App: `landingPoint(D, angleDeg)` computes `offset = angleDeg - 45`,
  `x = D*sin(offset)`, `y = D*cos(offset)`, with +x toward 1B. So the app's
  HZ angle is "45 = dead center, +toward 1B line (90), -toward 3B line (0)",
  i.e. `HZ - 45` is a signed offset from center, positive toward 1B.
- Workbook: `v0x = v0*cos(theta)*sin(phi)`, `v0y = v0*cos(theta)*cos(phi)`,
  bearing = `atan2(x, y)` - so phi is *also* a signed offset from dead center
  with +toward 1B (the +45 = 1B-line convention in its docs matches its own
  math). Same sign convention, same axis. Therefore `phi = HZ - 45` exactly.

Hand: the app folds hand into the HZ angle itself
(`angle = hand==='L' ? 45 - frac*40 : 45 + frac*40`) - by the time you have
`flight.angle`, the mirroring has already happened and the angle is a plain
physical field direction. The workbook keeps phi physical and folds hand into
spin only (`sign` in the backspin/sidespin formulas). So the correct
composition is: physical direction from the (already-mirrored) HZ angle,
actual hand into the spin formulas. Probe results confirming no
double-counting and no cancellation:

```
hand=R bucket=-5 -> HZ= 5 -> phi=-40 -> lands x=-293.2 (3B line side)  ✓
hand=R bucket= 0 -> HZ=45 -> phi=  0 -> lands x= +41.0 (center, small drift)
hand=R bucket=+5 -> HZ=85 -> phi=+40 -> lands x=+283.7 (1B line side)  ✓
hand=L bucket=+5 -> HZ= 5 -> phi=-40 -> lands x=-283.7 (exact mirror of R +5)
(R, +phi) vs (L, -phi) at identical ev/la: mirror error 0.0000 ft in x,
0.0000 ft in y, 0.00000 s in hang - an exact mirror pair.  ✓
```

Two consequences to write into code comments:

1. **Sidespin drift means the landing bearing is not the HZ angle.** A
   dead-center (HZ=45, phi=0) fly ball for a RHH lands at bearing ≈ +5.8
   deg (~41 ft toward RF at 400 ft) - and the same-hand +/-phi pair is
   deliberately asymmetric (R at phi=+20 carries 379 ft, R at phi=-20 carries
   411 ft). This is real physics (pull-side vs opposite-field spin) and is
   wanted. For grounder-family LA the flight is so short (0.06-0.8 s) that
   drift at first bounce is under ~1 ft - the infield interception logic in
   Part 4 may treat the HZ angle as the ball's path direction. For air balls,
   consumers that need "where did it land" must use the integrator's (x, y),
   never `landingPoint(D, angle)` - after this port, `landingPoint`'s only
   remaining legitimate uses are field geometry (fence arc, dirt edge, foul
   lines) and points along the *ground* path (rollout), which follows the
   ground-contact velocity direction, not the launch bearing.
2. **`effectiveHand`'s fallback ('R' for missing/switch) already runs before
   the HZ formula**; pass that same resolved hand to the physics. One
   resolution point, used twice.

---

## Part 2 - Task 1: the trajectory module

### 2.1 Where it lives

New file `docs/js/trajectory.js`, loaded from `docs/index.html` immediately
before `js/app.js` (line ~321). Plain IIFE + `window.KMTraj` export, exactly
mirroring app.js's `window.KMFlight` pattern (no modules/bundler exists in
this static site, don't introduce one). Contents: pure functions only - no
DOM, no state, no knowledge of results/archetypes/fielders. app.js gates on
`window.KMTraj` being present the same way the test suite waits on
`KMFlight`.

Rationale for a separate file over another app.js section: app.js is 4,250
lines; the physics is the one piece with zero coupling to the app's data
shapes, and a separate file gives the Playwright suite a clean seam
(`window.KMTraj`) and keeps the reference-implementation diff (Python vs JS)
reviewable side by side.

### 2.2 Interface

```js
window.KMTraj = {
  // Part 0's integrator, verbatim.
  simulateFlight(evMph, laDeg, phiDeg, hand, opts) -> {
    distance,            // ft, home-plane distance sqrt(x^2+y^2) at z=0
    hangS,               // seconds to first z=0 crossing
    landing: {x, y},     // ft, field plane (+x toward 1B, +y toward CF)
    contactVel: {vx, vy, vz},   // ft/s at the z=0 crossing (vz < 0)
    samples: [{t, x, y, z}, ...],  // every Nth integration step + the exact
                                   // interpolated landing point as the last
                                   // entry (N chosen so samples.length <= 48)
    apexFt,              // max z, for CSS class/label decisions
  },
  // Part 3's bounce/roll model.
  groundPath(speedFt, vzFt, opts) -> { ... see Part 3 ... },
  CONTACT_HEIGHT_FT, CONTACT_Y0_FT, DT_S,
};
```

`opts` carries `{z0, dt}` overrides for tests only. `samples` is what Task 2
animates; keep every step's cost in mind and downsample to <= 48 entries
(every 12th-14th step for a long fly ball), always appending the exact
interpolated landing sample last so the path visibly ends at the landing
point.

### 2.3 Rewiring `flightParams` (app.js ~1347)

Unchanged: result gating, band lookup, `q`, `onTop`, `launchAngleFor` (LA),
EV interpolation, bucket -> HZ angle, `effectiveHand`. These are the
Stage-1/2 contracts and the mission constraint's anchor - do not touch.

Changed:

```js
var EV = band.evMin + q*(band.evMax - band.evMin);        // unchanged
var LA = launchAngleFor(band, q, onTop);                  // unchanged
var angle = ...;                                          // unchanged (HZ)
var sim = KMTraj.simulateFlight(EV, LA, angle - 45, hand);
var D = clampToFence(sim.distance, angle, isHomeRun);     // clamp survives
```

- `D = band.depthMin + q*(...)` (line 1372) is deleted - the only runtime
  consumer of depthMin/depthMax goes away.
- `hangMs = 1000*sim.hangS` for every ball including grounders (F10 fixed;
  the old vacuum formula and the `isGrounder ? null` special case go away).
- `flight.x/y` come from `sim.landing` when unclamped. When `clampToFence`
  engages (non-HR whose carry exceeds fence-12), scale the landing point and
  the *samples* radially to the clamped distance (multiply x,y of every
  sample by `D/sim.distance`, leave z and t alone) - a cheap squash that
  keeps the arc continuous; exact re-integration is not worth it for a
  visual. Set a `flight.clamped` boolean so the audit in 2.6 can count these.
- Keep `flight.isGrounder` only if something still reads it after Part 7's
  timing rewrite removes its last behavioral use; prefer deriving the CSS
  `ground`/`air` class from `sim.apexFt < ~8` or the archetype. Aim to delete
  the flag (F7's root).
- New outputs on `flight`: `contactVel`, `samples`, `apexFt`, `hangS`.

Call count note: `flightParams` runs once per slide render (playSceneHtml,
line ~2987) and its result is threaded everywhere - no caching needed.

### 2.4 Migration: `result_diff_bands.csv` / `compute_flight_ranges.py`

Decision: **depth_min/depth_max stay in the CSV but are demoted to
audit-only.** They are real Statcast percentiles (for air balls) and the
regression tests and the distribution audit (2.6) want them as a reference;
they cost nothing to keep. But nothing at runtime reads them anymore, and
`key_moments_build.py::_flight_meta` (line ~748-775) may keep shipping them
unchanged (zero-risk; the test suite reads meta.json). Concretely:

1. `compute_flight_ranges.py`: keep computing depth percentiles; update the
   module docstring and the `flight_source` note to say depth is
   audit/reference only, not a runtime input.
2. The hand-tuned `GROUND_TOUCHING_DEPTH` override (60-150/45-90/5-25):
   **retire it.** Its two historical jobs are both gone: (a) landing distance
   for grounders now comes from the integrator - and **VERIFIED**, the
   physics naturally produces the short first-bounce point the hand-tuning
   was papering over (grounder-family LA/EV ranges integrate to first
   bounces of ~3-70 ft across the q/onTop grid, vs the hand-tuned 60-150
   "fielded depth" range, which was never a landing distance at all); (b)
   fielded depth now comes from Part 4's per-position depth. Write the real
   Statcast first-bounce percentiles back into depth_min/depth_max for these
   rows too (as audit reference), with a `flight_source` note. This also
   resolves F2's *mechanism* for 1B/2B: their depth columns stop being
   consumed as landing points entirely, so the first-bounce-vs-landing
   mixture in those samples stops mattering at runtime.
3. Generator hardening while touching this file (F6, F9 - in scope):
   - Fail (raise, non-zero exit) if any result present in the existing CSV
     has no filter in `_build_filters` - never NaN-overwrite silently.
   - Fail on a cold run with no existing CSV rather than emitting a
     bands-less file.
   - Assert `la_min < la_ideal < la_max` after rounding for every row; on a
     tie, nudge the bound by 0.1 rather than shipping a collapsed range, and
     print what was nudged.
4. Out of scope, separate follow-up (flag to Alex, don't do here): the
   1B/1BWH/1BWH2 and 2B/2BWH overlapping-population filter correction
   (source doc's "Alex's correction, not yet implemented") - it changes the
   LA/EV inputs, which is orthogonal to this redesign, and doing both at
   once muddies the before/after audit.

### 2.5 The grounder-family exception - answered

Once distance is integrated from LA/EV, the special-cased depth table is not
needed at all (see 2.4.2). The full causal chain for a grounder becomes:
LA/EV (Statcast, per-result, q-interpolated) -> first bounce + ground-contact
velocity (integrator) -> roll (Part 3) -> interception depth/time (Part 4).
Each stage is physics or explicit data; no hand-tuned distance remains
anywhere in the grounder path.

### 2.6 Distribution-shift audit (required implementation step, not optional)

**VERIFIED and quantified:** deriving D from LA/EV shifts the distance
distributions relative to today's depth interpolation. Probe results over
every result's (q=0 top / q=0 under / q=1) corners, z0=3, phi=0:

- Air-ball results run modestly hot at q=1: HR 448 vs depth_max 428, FO 305
  vs 294, DFO 403 vs 380, SacF 346 vs 315. Acceptable; physics wins.
- Hit-family tails now reach the wall: 1BWH at q=0/under integrates to 415 ft
  and 2BWH/3B q=1 to 414-428 ft carry - `clampToFence` turns these into
  wall balls (land at 363). A single that one-hops the wall is a legal
  story, but the *frequency* changes vs today (their old depthMax was
  247-398). Audit, don't pre-tune.
- HR's low tail: q=0 integrates to ~376-383 ft vs fence 375 - inside-the-park
  HR rate will shift relative to the old depthMin=364 interpolation (fewer
  ITP HRs, since 376+ clears). The fenceAt comment (app.js ~1090) already
  asks for exactly this watch.

Deliverable: a small throwaway audit script (Python, reusing the Part 9
reference implementation) that sweeps every result x q in {0,...,1} x onTop x
the 11 HZ angles and reports, per result: derived min/median/max distance,
fence-clamp rate, ITP-HR rate, vs the CSV depth band. Run it once during
implementation, paste the table into the PR description, and let Alex judge
feel-level knobs (z0, per-result EV/LA source corrections) from data instead
of anecdotes. Runtime code gets no per-result fudge factors in this pass.

---

## Part 3 - Task 3: bounce/roll model (`KMTraj.groundPath`)

### 3.1 Model

Input: ground-contact speed along the ground `sh = hypot(vx, vy)` (ft/s),
vertical speed `vz` (ft/s, negative), both from `simulateFlight.contactVel`.
The ground path is 1-D along the ground-direction unit vector
`(vx, vy)/sh` from the landing point (Part 1's drift note: this direction,
not the HZ launch bearing, is the roll direction - for grounders they differ
by well under a degree, for a line-drive single by a few degrees).

Phase 1 - bounces (while the rebound height exceeds a threshold):

```
E_REST_V = 0.42     // vertical restitution per bounce (dirt/grass blend)
F_RETAIN_H = 0.72   // horizontal speed retained per bounce
HOP_MIN_FT = 0.5    // rebound apex below this -> rolling
vz_up(i) = E_REST_V * |vz_down(i)|
hop: vacuum parabola, t_i = 2*vz_up/g, len_i = sh_i*t_i, apex_i = vz_up^2/(2g)
sh_(i+1) = F_RETAIN_H * sh_i ; vz_down(i+1) = vz_up(i)
```

Vacuum hops, deliberately: hop airtimes are ~0.1-0.4 s and the coefficients
are invented tunables anyway - re-running the drag ODE per hop would add cost
and false precision to a model whose constants are feel-calibrated. (The
*flight* stays fully modeled; only the post-contact hops are simplified.)

Phase 2 - rolling, constant deceleration to rest:

```
ROLL_DECEL = 12.0   // ft/s^2 (grass/dirt rolling+skidding blend)
reach = sh_roll^2 / (2*ROLL_DECEL);  duration = sh_roll/ROLL_DECEL
```

All four constants exported as named tunables with a comment block saying
they are feel knobs calibrated to the acceptance checks in 3.3, not measured
physics.

### 3.2 Interface

```js
groundPath(sh, vz, opts) -> {
  restFt,            // total ground distance from landing to rest
  totalS,            // time from landing to rest
  distAt(tS),        // piecewise s(t), monotonic
  timeAt(distFt),    // inverse; null if distFt > restFt
  hops: [{lenFt, apexFt, tS}, ...],   // for Task 2's hop rendering
}
```

`timeAt` is the load-bearing output: Part 4 asks "when does the ball cross
the assigned fielder's depth," Part 7 turns that into animation ms, and the
throw choreography hangs off it. Implement `distAt/timeAt` analytically
(closed-form per phase), not by sampling.

### 3.3 Acceptance checks (become tests)

Using **VERIFIED** contact velocities from the integrator:

- GO-shaped (EV 85, LA -11): contact sh ≈ 118.7 ft/s at 15.8 ft. Ball must
  cross a 147 ft SS depth in roughly 1.0-1.8 s of ground time and still be
  moving (real-world feel: routine grounder to SS ~1.5 s). It will - and its
  unconstrained `restFt` will be far beyond the infield, which is *correct*
  (a grounder nobody fields does reach the outfield); interception, not the
  roll model, is what stops it.
- Bunt-shaped (EV 35, LA -30): contact sh ≈ 43.6 ft/s at 6.8 ft. Must reach
  P's 60 ft depth slowly (>= ~1 s) or die first - either is acceptable; what
  must NOT happen is a bunt racing out to SS depth. This replaces F8's
  luck-based gate with physics: bunts roll short because 22-47 mph EV gives
  low ground speed, not because a threshold constant happens to sit above
  their EV.
- Weak grounder (EV 60, LA +2): contact sh ≈ 81 ft/s at 45 ft; rest well
  short of 147 -> exercises the charge-in branch in Part 4.

### 3.4 Archetype gate (unchanged boundary)

`CAUGHT_IN_AIR` outs never enter groundPath - the existing early return in
the rollout/roll pipeline is preserved as-is. Everything else that stays in
the park gets a ground path (hits included - see 4.6).

---

## Part 4 - Task 4: unified grounder interception

One function replaces five disagreeing mechanisms (`HZ_FIELDER_BY_ANGLE`
stays as data; `applyPositionOverride`, `applyGroundBallFielderDepth`,
`groundBallRolloutFt`'s depth lookup, `nearestFielder`-for-grounders, and
`INFIELDER_DEPTH_FT`-vs-anchors dualism are all subsumed).

### 4.1 One source of truth for infield geometry (resolves F5)

`INFIELDER_DEPTH_FT` (P 60, 1B 111, 3B 119, SS 147, 2B 147) is the
researched, documented number - keep it as the authority. Derive the five
infield anchors from it instead of hand-placing them:

```js
var CANONICAL_ANGLE = { "3B": 9, SS: 29, P: 45, "2B": 61, "1B": 81 };
// midpoint of each position's HZ bucket set: {5,13} {21,29,37} {45} {53,61,69} {77,85}
FIELDER_ANCHORS_FT[pos] = landingPoint(INFIELDER_DEPTH_FT[pos], CANONICAL_ANGLE[pos]);
```

Spot check: 3B becomes (-69.9, 96.3) vs today's hand-placed (-75, 85) - a
~13 ft visual move, well within "fielders stand in slightly different spots"
tolerance. Outfield + C anchors stay hand-placed (no depth table exists for
them). This is the [[feedback_field_scale_consistency]] fix: one
real-world-unit source, geometry derived from it.

Also export `MIN_ANGLE_FOR_POS = { "3B": 5, SS: 21, P: 45, "2B": 53, "1B": 77 }`
(minimum lattice angle mapping to each position - the override target per the
source doc's decided proposal).

### 4.2 The resolver

```js
/* Ground-archetype outs only (GROUND_ARCHETYPES && outs increased), called
   from playSceneHtml right after flightParams. Decides, in order:
   who fields it -> at what corrected direction -> at what depth -> when. */
function resolveGrounderInterception(m, flight, hand) {
  // (1) Position, before any physics: HZ answer, then the BRC check.
  var hzPos = HZ_FIELDER_BY_ANGLE[Math.round(flight.angle)];   // always hits: angle is lattice-valued here
  var pos = hzPos, angle = flight.angle;
  if (brcExcludes(m, hzPos) && m.default_position) {
    pos = m.default_position;
    angle = MIN_ANGLE_FOR_POS[pos];          // direction override, LA/EV untouched
    reSimulate(flight, angle, hand);         // re-run simulateFlight with phi=angle-45,
                                             // same LA/EV -> new landing/samples/contactVel
  }
  // (2) Ground path from real contact velocity.
  var gp = KMTraj.groundPath(hypot(flight.contactVel.vx, flight.contactVel.vy),
                             flight.contactVel.vz);
  // (3) Interception: fielder's depth crossing, or the ball dies first.
  var depth = INFIELDER_DEPTH_FT[pos];
  var alongFt = depth - flight.distance;     // ground distance to the fielder's depth
  var fieldedFt, groundTimeS;
  if (alongFt <= 0) {                        // landed at/past the fielder's depth
    fieldedFt = 0; groundTimeS = 0;          //  -> fielded on the short hop where it lands
  } else if (alongFt <= gp.restFt) {
    fieldedFt = alongFt; groundTimeS = gp.timeAt(alongFt);
  } else {                                   // dies short: fielder charges, fields it at rest
    fieldedFt = gp.restFt; groundTimeS = gp.totalS;
  }
  flight.fielder = pos;                      // THE fielder, everywhere (F3)
  flight.fieldedDistFt = flight.distance + fieldedFt;
  flight.groundTimeS = groundTimeS;
  flight.rollSamples = ...;                  // ground-path samples for Task 2 (hops + roll)
}
```

Notes, each mapping to a finding:

- `brcExcludes` tests the **HZ answer** (or, post-override, the resolved
  position), never `nearestFielder`'s proximity answer - F3's root cause.
  `flightParams` stops calling `nearestFielder` for ground archetypes
  entirely; `flight.fielder` is written in exactly one place for grounders.
- The angle override re-runs the integrator with the same LA/EV (the mission
  constraint: only direction changes; distance/hang shift a few feet via the
  spin's phi-dependence, which is correct physics, and LA/EV - the
  Stage-2-derived quantities - are bit-identical). The overridden angle is a
  *lattice value* by construction, so every downstream lattice lookup hits -
  F1 can no longer exist structurally: there is no post-hoc angle rewrite,
  no independent re-lookup in the rollout, and no fence-capped fallback path.
- The cap semantics of `applyGroundBallFielderDepth` ("fielded on the way
  through") and its charge-in semantics ("left alone if short") both survive,
  but as *time/position along one physical path* instead of two disagreeing
  distance patches. `flight.distance` (the landing/bounce point) is no longer
  overwritten by the cap - the *fielded* point is a separate, additional
  fact (`fieldedDistFt`), which is what the throw origin and label anchors
  actually wanted all along.
- Ball-never-past-the-fielder invariant: `fieldedFt <= alongFt` by
  construction in every branch - assert it in tests over the full grid.

### 4.3 Live BRC configs this must handle (**VERIFIED** against current import_BRC.csv)

Batted-ball override rows now in the sheet: `BDP/BFC/BGO/SacB` (excl 2B,SS ->
3B), `TP` (excl SS,2B,1B,P -> 3B) - all ground archetypes, handled by 4.2 -
and `DFO/SacF` (excl LF -> CF), which are **caught-in-air outfield**
configs. (The DP31 config cited in the Opus review is no longer present in
the current sheet; the mechanism must still handle its shape, since the sheet
is Alex-editable data - see [[explicit-brc-over-heuristics]].) Steal-family
rows (C/P) keep flowing through `stealThrowOrigin`, untouched.

For the caught-in-air overrides, replace `applyPositionOverride`'s
snap-to-anchor with the same *direction-override* principle: if the assigned
fielder (for air balls: `nearestFielder` on the landing point, unchanged) is
excluded, clamp the HZ angle into the default outfielder's angular third
(LF: [5,33], CF: [33,57], RF: [57,85] - nearest edge), re-run the
integrator with the same LA/EV, and set `flight.fielder` to the default. The
catch point stays a physically consistent landing for that LA/EV, and the
distance is no longer teleported to the anchor's exact depth (today a SacF
overridden to CF snaps to exactly (0,320) regardless of its own EV/LA).

### 4.4 Consumers audit (every reader of the old mechanisms)

| Consumer | Today | After |
|---|---|---|
| `playSceneHtml` ~2988 | `applyPositionOverride` else `applyGroundBallFielderDepth` | one call: `resolveGrounderInterception` (ground outs) / air-override branch (4.3) |
| `groundBallRolloutFt` ~1539 | re-derives HZ pos + depth, gap-scaled heuristic | deleted; roll = `flight.rollSamples`/`fieldedDistFt` from the resolver |
| `rolloutFraction`, `ROLLOUT_*`, `ROLLOUT_MS`, `ROLLOUT_FT` | global heuristic | deleted (F4, F8 superseded) |
| `throwHtml` ~1901 `fieldPt` | landing + rollout ft along HZ angle | `fieldedDistFt` along the ground direction (exact interception point) |
| `throwSchedule` ~1887 | `ballTravelMs + rollMs + THROW_DELAY_MS` | `fieldedMs(flight) + THROW_DELAY_MS` (Part 7) |
| `ballFlightHtml` rollout segment | `rolloutHtml` line + `ROLLOUT_MS` | ground-path render from `rollSamples` (Part 6) |
| `fielderTokensHtml` (disabled) | anchor -> flight.x/y | anchor -> fielded point; keep behind `SHOW_FIELDER_TOKENS` |
| label anchors ~1647 | `distance + rollFt` | `fieldedDistFt` (outs) / rest-or-pickup point (hits) |
| `outThrowTargets`/`throwOrderKeyForPosition` | `flight.fielder` (proximity) | `flight.fielder` (resolver) - same read, now-correct value |
| `nearestFielder` | all archetypes | air/hit plays only; never ground-archetype outs |

Sonnet: grep `flight.fielder`, `nearestFielder`, `groundBallRolloutFt`,
`applyPositionOverride`, `applyGroundBallFielderDepth`, `ROLLOUT` and verify
this table is exhaustive before deleting anything.

### 4.5 Outfield scope decision (flagged, with recommendation)

**Recommendation: infield outs + the 4.3 air-override are the full scope of
the *assignment* mechanism this pass; do NOT build an outfield
depth-reconciliation for catches.** Justification: for a caught air ball the
catch point *is* the fielded point - the fielder token converges to it and
the throw originates from it; there is no second "real depth" to reconcile
against, so the infield problem (lattice-assigned fielder vs independently
computed distance) structurally does not exist. The one outfield gap that
does exist - where a ground-through *hit* gets picked up - is handled by the
roll model, cheaply:

### 4.6 Hits (no out recorded) - pickup rule

For hits that stay in the park, the ball needs a visible end point (labels,
rollout, plausibility) but no out choreography. Rule: run `groundPath` from
the landing contact velocity; the ball is picked up at the first crossing of
the assigned outfielder's radial depth (outfielder by angle-third of the HZ
angle; radial depth = `hypot(anchor.x, anchor.y)` for that OF), *if* the roll
segment crosses it from below; otherwise at rest; always capped at
`fenceAt(angle) - 2`. Infield-archetype hits (`STAYS_IN_INFIELD_ARCHETYPES`:
bunt, infield_single) skip the OF rule and just use rest (they die on the
dirt by construction of their EV range - now physically, not by threshold
luck). The dirt-clearance floor (`dirtEdgeFt + DIRT_CLEAR_MARGIN_FT`,
~1586): keep it as a runtime floor on the pickup point for non-infield hits,
unchanged - **VERIFIED** the physics makes it nearly dead (a q=0 1B lands at
~30 ft but with ~70 mph ground speed, rolling far past the dirt), but it is
cheap insurance and its test exists.

---

## Part 5 - What happened to each timing/heuristic knob (bridge to Part 7)

| Knob | Disposition |
|---|---|
| `GROUNDER_ROLL_MS = 450` | retired - grounder ball time = scaled physics (Part 7) |
| `HANG_MS_SCALE/MIN/MAX` | replaced by `ANIM_TIME_SCALE` + the same min/max clamps, applied to physics hang |
| `ROLLOUT_FT/EV_LOW/EV_HIGH/LA_LOW/LA_HIGH/ROLLOUT_MS` | deleted with `rolloutFraction` |
| `THROW_DELAY/DRAW/STAGGER/LEAD_MS`, `TAG_UP_MS`, `TAG_THROW_MARGIN_MS`, `RUN_LEG_MS`, `OUT_BEAT_MS`, `RUNNER_LEAD_MS` | preserved exactly - runner/throw choreography is not this redesign's business |
| `DIRT_CLEAR_MARGIN_FT`, `dirtEdgeFt` | preserved (4.6) |
| `FENCE_DEPTH_FT`, `fenceAt`, `clampToFence` | preserved (2.3) |
| `GROUND_LA_THRESHOLD`/`isGrounder` | delete if Part 7 lands as specced; CSS class derives from apex/archetype |

## Part 7 - Animation-time mapping (needed by Tasks 1/3/4 jointly)

(Numbered out of order to keep the prompt's task numbers 1-4 clean above.)

Physical times are now real: fly balls hang 4-6.6 s, grounders reach the
fielder in ~0.8-1.9 s (flight + ground time). The animation runs stylized
time: runner-to-first is 800 ms (real ~4.3 s, ratio ~0.19), and the whole
choreography (throw beats runner by `THROW_LEAD_MS`, etc.) is asserted
against those stylized runner times. So physics time must be *scaled, once,
in one place*:

```js
var ANIM_TIME_SCALE = 0.22;   // one knob: physics seconds -> animation ms factor
function ballTravelMs(flight)  { return flight ? clamp(flight.hangMs*ANIM_TIME_SCALE, HANG_MS_MIN, HANG_MS_MAX) : 0; }
function fieldedMs(flight)     { return ballTravelMs(flight) + flight.groundTimeS*1000*ANIM_TIME_SCALE; }
```

- Sanity: a 5.35 s HR hang * 0.22 = 1177 ms (today: pinned at 1400); a GO
  (0.11 s flight + ~1.3 s ground) * 0.22 = ~310 ms (today: flat 450 ms).
  Both in today's feel range - `ANIM_TIME_SCALE` in 0.20-0.30 with the
  existing 450/1400 clamps kept is the starting point; it is *the* new
  feel knob and must be a named export.
- `ballTravelMs` loses its `isGrounder` branch (F7 fixed: no more
  two-constants-coinciding luck; a `GO` at LA 4.0 and LA 3.9 animate
  identically because they *are* nearly identical physics).
- Every current `ballTravelMs` consumer (throwSchedule ~1866/1887, outDelay
  ~2089, delayedStartMs ~2098, scoreArrivals ~2912, fielderTokensHtml) keeps
  its call; `throwSchedule`'s grounder branch swaps `ballTravelMs + rollMs`
  for `fieldedMs`. The A4/tag-throw race invariants must then be re-validated
  over the *real* grid, not one mock (Part 10).
- The ball's keyframe timeline (Part 6) uses the same scale so the ball, the
  throws, and the runners share one clock.

---

## Part 6 - Task 2: perspective field view

### 6.1 Projection approach - decision and rationale

**A single analytic pinhole projection function in JS** (world ft -> screen
px), not CSS 3D transforms. Reasons: (a) the ball needs true z rendering -
a point *off* the field plane - which a CSS `rotateX` of the flat SVG cannot
produce at all; (b) CSS 3D would skew every token and text label, requiring
per-token counter-transforms in a second coordinate system - exactly the
"two coordinate spaces drifting" family this codebase's bug history warns
about; (c) the codebase already funnels *every* placement through one
conversion point (`ftToSvg`, ~1000, by explicit design), so swapping that
function's internals is the minimal-blast-radius path and preserves the
architecture invariant that there is exactly one ft->px mapping.

```js
// World: x ft toward 1B, y ft toward CF, z ft up, origin = home plate.
// Camera: at (0, -CAM_BACK, CAM_UP), looking at (0, LOOK_Y, 0), no roll.
var CAM_BACK = 180, CAM_UP = 250, LOOK_Y = 170, FOCAL = 300; // px-ish, tune
function projectFt(x, y, z) {
  // camera-space basis: right = +x; forward f = normalize(0, LOOK_Y+CAM_BACK, -CAM_UP)
  // up u = f x right (precomputed constants)
  var dy = y + CAM_BACK, dz = z - CAM_UP;
  var depth  =  FWD_Y*dy + FWD_Z*dz;          // distance along view axis
  var upComp =  UP_Y*dy  + UP_Z*dz;
  return { x: SCREEN_CX + FOCAL * x / depth,
           y: SCREEN_CY - FOCAL * upComp / depth,
           depth: depth };                     // depth kept for z-ordering if needed
}
function ftToSvg(xFt, yFt) { return projectFt(xFt, yFt, 0); }  // signature preserved
```

`FWD_*`/`UP_*` are precomputed literals from the camera constants. The four
camera constants are feel knobs; Sonnet tunes them against
`baseballflightsim.jpg` with these acceptance criteria: (1) whole fence arc
inside the viewBox with margin for a 428 ft HR's fade point; (2) home plate
bottom-center, dirt circle visibly elliptical as in the reference; (3) the
2B-to-home line stays vertical on screen (camera has no roll/yaw by
construction); (4) a 100 ft-apex fly ball's arc top reads clearly above the
fence line. Consider widening the viewBox aspect toward the reference image's
(~460x300); `scene-diamond-wrap`/CSS sizing must be checked with it (OQ-4).

Keep **every** field-plane computation (fence clamp, dirt edge, interception
depths, roll distances) in world feet exactly as today - the projection is
render-only. That rule is what keeps Tasks 1/3/4 testable without a DOM.

### 6.2 Geometry re-derivation checklist (each currently assumes overhead view)

- `HOME_SVG` constant -> becomes `projectFt(0,0,0)`; `FT_PER_UNIT` dies with
  it (nothing may multiply by a flat scale anymore - grep for `FT_PER_UNIT`
  and `/ FT_PER_UNIT`: fencePathD radius, infieldDirtHtml radius, markSize,
  watermark placement).
- `SCENE_BASES_ON_LINE` / `insetTowardSecond` / `SCENE_BASES` /
  `homePlateCorner`: already route through `ftToSvg` - re-derive
  automatically. The `BASE_R` inset happens in screen px today; acceptable
  as-is (token-radius-scale nudge).
- `fencePathD` (~1260): the fence is a world-space circle; under perspective
  it is no longer an SVG circular arc. Rebuild as a sampled polyline path
  (`landingPoint(FENCE_DEPTH_FT, a)` for a = 0..90 step ~3, through
  `ftToSvg`). Same for `infieldDirtHtml`'s arc. Foul lines stay two-point
  lines (straight lines project to straight lines).
- Optionally (cheap, big readability win, matches the reference image): draw
  the fence as a short *wall* - the same polyline duplicated at z=8 ft via
  `projectFt(x, y, 8)` and filled between - so an over-the-fence HR visibly
  crosses above it.
- `platePath`, `dm-field` diamond, watermark box, dugout points, all runner
  keyframe endpoints (`SCENE_BASES` consumers), throw line endpoints, steal
  throw origins: all route through `ftToSvg`/`SCENE_BASES` already - they
  re-derive for free. Anything reading raw `FIELD_W/FIELD_H` for placement
  (grass rect, viewBox) gets reviewed by hand: the grass becomes a polygon
  ringing the projected field (or the fence polygon + foul territory), with
  the area above the horizon left as page background, per the reference.
- Token sizes (`RUNNER_R`, `BALL_R`, ...): keep constant-px (billboarded
  2D tokens per the decided scope). Optionally scale by `1/depth` later; not
  this pass.

### 6.3 Ball arc rendering (true 3D)

Source: `flight.samples` (<= 48 (t,x,y,z) points, Part 2.2), plus
`flight.rollSamples` hop/roll points (Part 4). Project each sample:
`p_i = projectFt(x_i, y_i, z_i)`.

Mechanism - **per-play generated CSS keyframes**, staying inside the
codebase's stated animation architecture (pure CSS keyframes, wholesale
innerHTML replacement, one reduced-motion switch - comment at ~976):

1. `ballFlightHtml` emits a `<style>` block with a uniquely named
   `@keyframes` (`kmArc-<moment_id>`): one stop per sample,
   `offset% = t_i / t_total`, `transform: translate(px_i, py_i)`. Because the
   offsets carry the real sample times, the ball follows the true speed
   profile (fast off the bat, hanging at apex) with no easing tricks -
   `animation-timing-function: linear` between stops.
2. The `.ball` element keeps its class and its `--tx/--ty` end-state custom
   properties (set to the final sample) so the *existing* base rule
   (`transform: translate(var(--tx),var(--ty))`) still parks the ball at its
   end point when animations are off. The generated rule only supplies
   `animation-name/duration`.
3. **Reduced-motion**: the existing block (~style.css 1183+) kills `.ball`
   et al. with equal-specificity later rules; a generated
   `.ball.kmArc-x { animation-name: ... }` selector would out-rank it. Fix:
   add `animation: none !important` to the reduced-motion block's ball/trail
   entries (one-line change, and it makes the existing intent explicit).
4. The trail (`.ball-trail`) becomes a `<path>` through the projected flight
   samples (polyline `d`), drawn with the existing `trailDraw` dash
   technique - compute the polyline length in JS (sum of segment lengths)
   for `--len`, since `getTotalLength()` isn't available at string-building
   time. Class name and custom-property contract (`--len/--dur`) unchanged
   so the CSS keeps working.
5. Ground phase: hops from `rollSamples` join the same keyframes list (the
   arc flattens into decaying hops, then a straight roll - visually the
   "bounce, bounce, roll to the fielder" the whole redesign is for). The
   separate `.ball-rollout` element and `ROLLOUT_MS` disappear; one ball,
   one continuous timeline from contact to fielded/rest. The
   `.ball-rollout-trail` line may stay (ground path projects to a straight
   line) with its endpoint at the fielded/pickup point.
6. Cleared-fence HRs: keep today's fade-at-wall behavior - truncate samples
   at the fence crossing + ~15 ft and reuse `ballClearFade`.
7. Fielder-token converge (`fielderTokensHtml`, currently disabled), labels
   (`ball-label`/`ball-dist`), and throw lines all just consume projected
   points + Part 7 times; no structural change.

Timing hookup: `animation-duration` for the arc = `ballTravelMs(flight)`
(+ scaled ground time for the ground phase keyframes) - the same numbers
`throwSchedule` uses, so the throw leaves when the ball visually arrives.

### 6.4 What deliberately stays 2D

Fielders, runners, throw lines, base plates, labels: flat tokens/lines at
projected *ground* positions (decided scope). Throw lines become straight
screen lines between projected points - fine (a throw's arc is out of
scope). Wheel markers, meters, ribbon: untouched.

---

## Part 8 - Build sequence (hard dependencies, landable checkpoints)

Dependency spine: **Task 1 -> (Task 3, Task 4, Task 2's arc)**; Task 4's
position logic is data-only but its depth-crossing needs Task 3's `timeAt`;
Part 7 needs 1+3+4; Task 2 needs 1's samples (and 4's rollSamples for hops)
but nothing needs Task 2.

1. **Stage A - physics core (landable alone).** `docs/js/trajectory.js`:
   `simulateFlight` + `groundPath`. Python reference implementation (Part 9)
   generates golden vectors; Playwright tests assert parity + mirror + HZ
   mapping. No app.js changes yet - zero user-visible change.
2. **Stage B - generator/CSV migration (landable alone, in parallel with A).**
   compute_flight_ranges.py hardening (F6/F9 guards), depth demotion
   docstrings, regenerated CSV (depth as audit columns incl. real grounder
   first-bounce percentiles). No runtime reads change.
3. **Stage C - flightParams port + resolver (the big cut-over, one PR).**
   Rewire flightParams to KMTraj (2.3); add `resolveGrounderInterception` +
   air-override (Part 4); delete the five old mechanisms per the 4.4 table;
   Part 7 timing (`ANIM_TIME_SCALE`, `fieldedMs`). Rendering still flat 2D -
   the flat view renders the new physics fine (landing point + straight
   rollout), which is exactly what makes this stage verifiable: run the full
   test suite + the 2.6 audit + eyeball real plays *before* any visual
   rewrite. This stage resolves F1-F5, F7, F8, F10.
4. **Stage D - perspective view (pure visual).** Part 6: projection,
   geometry re-derivation, arc/hop keyframes, trail path, fence wall,
   reduced-motion hardening. No physics/timing semantics change - diffable
   as "same numbers, new pixels."
5. **Stage E - calibration pass.** Tune `ANIM_TIME_SCALE`, camera constants,
   bounce/roll knobs against real plays; re-run timing-race sweeps; Alex
   reviews the 2.6 audit table and the feel.

Each stage ends green on `python ball_flight_test.py`.

---

## Part 9 - Python reference implementation (for golden vectors)

Sonnet: place this at `tools/trajectory_reference.py` (or regenerate it; it
is the exact probe that achieved 4-decimal parity with the workbook this
session). Use it to (a) emit the golden-vector JSON consumed by the test
suite, (b) run the 2.6 audit. Keep hyphens, not em dashes, in the .py file.

```python
import math

TEMP_C = (5 / 9) * (75 - 32)
PRESSURE_MM = 29.92 * 1000 / 39.37
SVP = 4.5841 * math.exp((18.687 - TEMP_C / 234.5) * TEMP_C / (257.14 + TEMP_C))
RHO_LB = 0.06261 * 1.2929 * (273 / (TEMP_C + 273) * (PRESSURE_MM - 0.3783 * 50 * SVP / 100) / 760)
CONST = 0.07182 * RHO_LB  # mass/circ terms are 1 at the standard ball
CD0, CDSPIN = 0.3008, 0.0292
CL0, CL1, CL2 = 0.583, 2.333, 1.12
TAU, DT, G, CIRC = 10000.0, 0.01, 32.174, 9.125

def simulate(ev_mph, la_deg, phi_deg, hand, z0=3.0, y0=2.0, dt=DT):
    th, ph = math.radians(la_deg), math.radians(phi_deg)
    sign = 1.0 if hand == "R" else -1.0
    wb = -763 + 120 * la_deg + 21 * phi_deg * sign
    ws = -sign * 849 - 94 * phi_deg
    spin = math.hypot(wb, ws)
    v0 = ev_mph * 1.467
    vx = v0 * math.cos(th) * math.sin(ph)
    vy = v0 * math.cos(th) * math.cos(ph)
    vz = v0 * math.sin(th)
    wx = (wb * math.cos(ph) - ws * math.sin(th) * math.sin(ph)) * math.pi / 30
    wy = (-wb * math.sin(ph) - ws * math.sin(th) * math.cos(ph)) * math.pi / 30
    wz = (ws * math.cos(th)) * math.pi / 30
    omega = spin * math.pi / 30
    x, y, z, t = 0.0, y0, z0, 0.0
    while t < 15.0:
        vmag = math.sqrt(vx * vx + vy * vy + vz * vz)
        decay = math.exp(-t / (TAU * 146.7 / vmag))
        L = spin * decay
        cd = CD0 + (CDSPIN * L / 1000) * decay
        S = ((L * math.pi / 30) * (CIRC / (2 * math.pi)) / 12) / vmag
        cl = CL2 * S / (CL0 + CL1 * S)
        k = CONST * (cl / omega) * vmag
        ax = -CONST * cd * vmag * vx + k * (wy * vz - wz * vy)
        ay = -CONST * cd * vmag * vy + k * (wz * vx - wx * vz)
        az = -CONST * cd * vmag * vz + k * (wx * vy - wy * vx) - G
        nx = x + vx * dt + 0.5 * ax * dt * dt
        ny = y + vy * dt + 0.5 * ay * dt * dt
        nz = z + vz * dt + 0.5 * az * dt * dt
        nvx, nvy, nvz = vx + ax * dt, vy + ay * dt, vz + az * dt
        if z > 0 and nz <= 0:
            frac = nz / (nz - z)
            lerp = lambda b, a: b - frac * (b - a)
            xf, yf, tf = lerp(nx, x), lerp(ny, y), lerp(t + dt, t)
            return {"dist": math.hypot(xf, yf), "hang_s": tf, "x": xf, "y": yf,
                    "vx": lerp(nvx, vx), "vy": lerp(nvy, vy), "vz": lerp(nvz, vz)}
        x, y, z, t = nx, ny, nz, t + dt
        vx, vy, vz = nvx, nvy, nvz
    return None
```

(Workbook-parity check for this exact code: `simulate(100, 30, 0, "L", z0=6.0)`
must return dist=402.3273, hang_s=5.6230, x=-39.8523, y=400.3486.)

The JS port must follow the same operation order so parity tolerances can be
tight (1e-6 relative). Note `CONST` here folds in mass=5.125/circ=9.125
being the standard ball; if the JS keeps the general formula, assert the
literal 0.005316103027433 matches.

---

## Part 10 - Fix map (explicit disposition, F1-F11)

| # | Finding | Disposition |
|---|---|---|
| F1 | Override breaks HZ lattice; rollout escapes fielder cap | **Superseded (Stage C).** No post-hoc angle rewrite exists; overrides set a lattice angle by construction; the rollout never re-derives the fielder; `fieldedFt <= alongFt` invariant tested over the grid. |
| F2 | 1B/2B depth = first-bounce consumed as landing | **Fixed structurally (Stage C).** depth columns are audit-only; landing derives from LA/EV. (The separate LA/EV *filter* correction for 1B-family stays a flagged follow-up, 2.4.4.) |
| F3 | `flight.fielder` vs HZ fielder disagree 39% | **Fixed (Stage C).** Resolver is the single writer for ground outs; exclusion checks read the resolver/HZ answer; `nearestFielder` demoted to air/hit plays. |
| F4 | Global rollout EV/LA scale incoherent | **Superseded (Stage C).** `rolloutFraction` and its four constants deleted; roll derives from per-play contact velocity. |
| F5 | Two sources of truth for infield depth | **Fixed (Stage C).** `INFIELDER_DEPTH_FT` authoritative; infield anchors derived from it + canonical angles (4.1). |
| F6 | Generator silently NaNs unmapped results | **Fixed (Stage B).** Hard failure on unmapped results and cold runs (2.4.3). |
| F7 | `isGrounder` vs la_max boundary; timing equal by luck | **Fixed (Stage C).** `ballTravelMs` keys off physics + one scale; `GROUNDER_ROLL_MS` retired; `isGrounder` deleted or demoted to nothing behavioral. |
| F8 | Bunts don't roll only because EV_LOW sits above them | **Superseded (Stage C).** Bunts roll short because 22-47 mph EV physically yields low ground speed (3.3 acceptance check). |
| F9 | `laIdeal` collapse unguarded | **Fixed (Stage B).** Generator asserts/nudges; test suite asserts over meta.json. `launchAngleFor` itself unchanged. |
| F10 | Vacuum hang time | **Fixed (Stage C).** Hang from the drag+lift integration, one consistent source with distance. |
| F11 | `diff == null` -> worst contact | **Opportunistic one-liner (Stage C), flagged:** change default to `q = 0.5` (neutral median story rather than worst-case) + a unit test. Currently unreachable on real data; if Alex prefers, dropping it to out-of-scope is harmless. |

---

## Part 11 - Test plan (`ball_flight_test.py`)

Existing structure (Playwright, pure functions via `window.KMFlight`) stays;
add `window.KMTraj` to the wait gate. Changes:

**New: physics parity block.**
- Golden vectors (Part 9 JSON) vs `KMTraj.simulateFlight`, rtol 1e-6, incl.
  the workbook-parity vector (z0=6) and contact-velocity components.
- Mirror property: for a grid of (ev, la, |phi|), (R,+phi) vs (L,-phi) equal
  in dist/hang, opposite in x (exact to fp noise).
- HZ mapping: phi = angle-45; HZ 45 -> |bearing| small; HZ 5/85 land on the
  correct sides; L/R same-bucket mirror (the Part 1 table as assertions).
- `const` literal check; samples: last sample equals the interpolated
  landing; sample times strictly increasing; z >= 0.

**New: groundPath block.**
- Monotonic `distAt`; `timeAt(distAt(t)) == t` round-trip; the 3.3
  acceptance cases (GO reaches 147 ft in 1.0-1.9 s; bunt never does;
  weak grounder dies short).

**New: resolver grid sweep** (replaces the two single-point regression
guards, which are deleted with the code they guard).
- For every ground result x 11 angles x q in {0,.25,.5,.75,1} x both hands x
  (override on/off using the live BRC configs): assert `flight.fielder` is
  the resolver's position everywhere it appears; fielded point never beyond
  the assigned depth (F1 regression, now as an invariant not a point);
  override case: LA/EV bit-identical pre/post, angle == MIN_ANGLE_FOR_POS,
  angle on the lattice; exclusion checks never consult proximity (assert a
  DP31-shaped synthetic config with a shallow-landing grounder still
  triggers - F3's exact counterexample).
- Timing race: re-run the A4 "throw beats runner" sweep with *real* flights
  (worst-case: the slowest grounder in the grid feeding `fieldedMs`), not
  mock flight objects - mocks can no longer represent timing. Same for the
  tag-throw margin sweep.

**Updated: existing blocks.**
- Worked-example golden cases: LA/EV/angle assertions unchanged (that path
  didn't move); distance/hangMs/x/y expectations regenerated from the Python
  reference (annotate each with the generating command).
- Fence-invariant 1000x1000 sweep: unchanged in form; now exercises the
  integrator (keep the stride; ~4.6k integrations ≈ seconds).
- CSV validity (new, F6/F9): over meta.json bands - non-null flight columns,
  `laMin < laIdeal < laMax`, `band_lo != band_hi`.
- Dirt-clearance test: keep, now asserting the 4.6 pickup floor.

**New: projection block (DOM-light).**
- Pure-function tests on `projectFt`: home -> bottom-center px; straightness
  (foul-line samples collinear within epsilon); depth ordering (larger y ->
  smaller screen y); left/right symmetry.
- DOM smoke test: render one play slide; assert a `kmArc-` keyframes block
  exists, the `.ball-trail` path has >= N points, fence path bbox inside the
  viewBox. Visual fidelity to the reference image stays a manual eyeball in
  Stage E - do not attempt pixel assertions.

---

## Part 12 - Open questions for Sonnet/Alex (decide during implementation)

- **OQ-1 Contact height:** workbook ships z0=6 ft (almost certainly a stray
  edit; Nathan's default is ~3). Plan says 3.0. Confirm with Alex; it shifts
  every carry ~2-4 ft.
- **OQ-2 Hit-tail wall balls:** 1BWH/2BWH/3B band edges now reach the wall
  (2.6). Accept the new frequency, or trim those results' EV/LA tails in the
  CSV? Decide from the audit table, not in advance.
- **OQ-3 `ANIM_TIME_SCALE` value** (0.20-0.30 start) and whether grounders
  want their own min clamp so bunts don't animate comically slowly.
- **OQ-4 ViewBox aspect + camera constants** (6.1 acceptance criteria are
  the contract; exact numbers are taste).
- **OQ-5 Bounce/roll coefficients** (E_REST_V/F_RETAIN_H/ROLL_DECEL/HOP_MIN):
  defaults given; calibrate in Stage E against the 3.3 checks and eyeball.
- **OQ-6 F11 default** (q=0.5 vs leave as-is) - one line either way.
- **OQ-7 Outfield pickup rule for hits (4.6):** recommended in; if it reads
  wrong on gap doubles (picked up "too shallow"), fall back to rest-point
  only and revisit.
- **OQ-8 Fence wall rendering** (6.2): recommended yes (cheap, sells the HR
  clearance); skip if it clutters.
- **NOT open:** the angle convention (Part 1 - verified), the integrator
  choice (0.6 - decided), depth columns' runtime role (2.4 - decided),
  infield depth source of truth (4.1 - decided).

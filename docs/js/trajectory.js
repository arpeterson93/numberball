/* Ball-flight physics core (ball-flight-3d-physics-redesign-plan.md Part 2).
 * Pure functions only - no DOM, no state, no knowledge of results/archetypes/
 * fielders. Plain IIFE + window.KMTraj export, mirroring app.js's
 * window.KMFlight pattern (no bundler exists in this static site).
 *
 * simulateFlight is a verbatim port of the drag+lift Euler integrator
 * extracted from TrajectoryCalculator-new-3D-June2026.xlsx (BattedBallTrajectory-1),
 * reproduced to 4-decimal parity with the workbook's own cached outputs - see
 * tools/trajectory_reference.py for the Python reference that generated the
 * golden vectors ball_flight_test.py checks this file against. The two
 * implementations must follow the same operation order; do not "simplify"
 * anything here without re-deriving parity.
 */
(function () {
  "use strict";

  // ── Part 0.1: constants and atmosphere (fixed defaults; never vary per park,
  // so the humidity math is precomputed once as a literal rather than shipped
  // to the browser on every load). ─────────────────────────────────────────
  var TEMP_C = (5 / 9) * (75 - 32);
  var PRESSURE_MM = 29.92 * 1000 / 39.37;
  var SVP = 4.5841 * Math.exp((18.687 - TEMP_C / 234.5) * TEMP_C / (257.14 + TEMP_C));
  var RHO_LB = 0.06261 * 1.2929 * (273 / (TEMP_C + 273) * (PRESSURE_MM - 0.3783 * 50 * SVP / 100) / 760);
  var CONST = 0.07182 * RHO_LB;   // mass/circ terms are 1 at the standard ball (5.125oz/9.125in)
  // Pinned per the plan (Part 0.1) - if this literal ever drifts, the
  // integrator's inputs changed and every golden vector needs regenerating.
  var CONST_EXPECTED = 0.005316103027433;
  if (Math.abs(CONST - CONST_EXPECTED) > 1e-12) {
    throw new Error("KMTraj: CONST literal mismatch (" + CONST + " vs " + CONST_EXPECTED + ")");
  }
  var CD0 = 0.3008, CDSPIN = 0.0292;
  var CL0 = 0.583, CL1 = 2.333, CL2 = 1.12;
  var TAU = 10000.0, DT_S = 0.01, G = 32.174, CIRC = 9.125;
  var MAX_T_S = 15.0;   // safety bound; nothing real gets close

  // Part 0.2: Nathan's public-calculator default (~3ft) rather than the
  // workbook's as-shipped z0=6 (almost certainly a stray manual edit - OQ-1).
  var CONTACT_HEIGHT_FT = 3.0;
  var CONTACT_Y0_FT = 2.0;

  var MAX_SAMPLES = 48;

  /* Part 0.3-0.5: one full drag+lift integration from contact to first ground
   * contact (linearly interpolated). opts: {z0, dt} overrides for tests only. */
  function simulateFlight(evMph, laDeg, phiDeg, hand, opts) {
    opts = opts || {};
    var z0 = opts.z0 != null ? opts.z0 : CONTACT_HEIGHT_FT;
    var y0 = opts.y0 != null ? opts.y0 : CONTACT_Y0_FT;
    var dt = opts.dt != null ? opts.dt : DT_S;

    var th = laDeg * Math.PI / 180, ph = phiDeg * Math.PI / 180;
    var sign = hand === "R" ? 1 : -1;
    var wb = -763 + 120 * laDeg + 21 * phiDeg * sign;
    var ws = -sign * 849 - 94 * phiDeg;
    var spin = Math.sqrt(wb * wb + ws * ws);
    var v0 = evMph * 1.467;
    var vx = v0 * Math.cos(th) * Math.sin(ph);
    var vy = v0 * Math.cos(th) * Math.cos(ph);
    var vz = v0 * Math.sin(th);
    var wx = (wb * Math.cos(ph) - ws * Math.sin(th) * Math.sin(ph)) * Math.PI / 30;
    var wy = (-wb * Math.sin(ph) - ws * Math.sin(th) * Math.cos(ph)) * Math.PI / 30;
    var wz = (ws * Math.cos(th)) * Math.PI / 30;
    var omega = spin * Math.PI / 30;

    var x = 0, y = y0, z = z0, t = 0;
    var apexFt = z0;
    var rows = [{ t: t, x: x, y: y, z: z }];

    while (t < MAX_T_S) {
      var vmag = Math.sqrt(vx * vx + vy * vy + vz * vz);
      var decay = Math.exp(-t / (TAU * 146.7 / vmag));
      var L = spin * decay;
      var cd = CD0 + (CDSPIN * L / 1000) * decay;
      var S = ((L * Math.PI / 30) * (CIRC / (2 * Math.PI)) / 12) / vmag;
      var cl = CL2 * S / (CL0 + CL1 * S);
      var k = CONST * (cl / omega) * vmag;
      var ax = -CONST * cd * vmag * vx + k * (wy * vz - wz * vy);
      var ay = -CONST * cd * vmag * vy + k * (wz * vx - wx * vz);
      var az = -CONST * cd * vmag * vz + k * (wx * vy - wy * vx) - G;

      // Euler step order (Part 0.4.4): position from THIS row's acceleration,
      // then velocity, then the next row recomputes from the new state.
      var nx = x + vx * dt + 0.5 * ax * dt * dt;
      var ny = y + vy * dt + 0.5 * ay * dt * dt;
      var nz = z + vz * dt + 0.5 * az * dt * dt;
      var nvx = vx + ax * dt, nvy = vy + ay * dt, nvz = vz + az * dt;

      if (z > 0 && nz <= 0) {
        var frac = nz / (nz - z);
        var xf = nx - frac * (nx - x);
        var yf = ny - frac * (ny - y);
        var tf = (t + dt) - frac * dt;
        var vxf = nvx - frac * (nvx - vx);
        var vyf = nvy - frac * (nvy - vy);
        var vzf = nvz - frac * (nvz - vz);
        rows.push({ t: tf, x: xf, y: yf, z: 0 });
        return {
          distance: Math.sqrt(xf * xf + yf * yf),
          hangS: tf,
          landing: { x: xf, y: yf },
          contactVel: { vx: vxf, vy: vyf, vz: vzf },
          samples: downsample(rows),
          apexFt: apexFt,
        };
      }

      x = nx; y = ny; z = nz; t = t + dt;
      vx = nvx; vy = nvy; vz = nvz;
      if (z > apexFt) apexFt = z;
      rows.push({ t: t, x: x, y: y, z: z });
    }

    return null;   // never lands within MAX_T_S - not reachable on real inputs
  }

  // Every Nth row (keeping row 0), always ending on the exact interpolated
  // landing sample (the last element pushed onto `rows` by the caller).
  function downsample(rows) {
    if (rows.length <= MAX_SAMPLES) return rows;
    var landing = rows[rows.length - 1];
    var body = rows.slice(0, rows.length - 1);
    var stride = Math.ceil(body.length / (MAX_SAMPLES - 1));
    var out = [];
    for (var i = 0; i < body.length; i += stride) out.push(body[i]);
    if (out[out.length - 1] !== landing) out.push(landing);
    return out;
  }

  // ── Part 3: bounce/roll model ──────────────────────────────────────────
  // Feel knobs (OQ-5), not measured physics - calibrated against the 3.3
  // acceptance checks (GO reaches SS depth in 1.0-1.8s of ground time and is
  // still moving; a bunt never races out to SS depth; a weak grounder
  // assigned to SS dies well short of 147ft, exercising the charge-in
  // branch). Final feel-level calibration against real plays is Stage E.
  var E_REST_V = 0.42;     // vertical restitution per bounce (dirt/grass blend)
  var F_RETAIN_H = 0.6;    // horizontal speed retained per bounce
  var HOP_MIN_FT = 0.5;    // rebound apex below this -> rolling
  var ROLL_DECEL = 20.0;   // ft/s^2, constant deceleration while rolling

  /* Vacuum hops, deliberately (Part 3.1): hop airtimes are ~0.1-0.4s and the
   * coefficients are feel-calibrated tunables anyway - re-running the drag
   * ODE per hop would add cost and false precision. Only the post-contact
   * hops are simplified; the flight itself stays fully modeled. */
  function groundPath(sh, vz, opts) {
    opts = opts || {};
    var eRestV = opts.eRestV != null ? opts.eRestV : E_REST_V;
    var fRetainH = opts.fRetainH != null ? opts.fRetainH : F_RETAIN_H;
    var hopMinFt = opts.hopMinFt != null ? opts.hopMinFt : HOP_MIN_FT;
    var rollDecel = opts.rollDecel != null ? opts.rollDecel : ROLL_DECEL;

    var hops = [];
    var segments = [];   // {kind, t0, d0, speed, dur, vzUp (hops only)}
    var shI = sh, vzDown = Math.abs(vz);
    var tCursor = 0, dCursor = 0;

    while (true) {
      var vzUp = eRestV * vzDown;
      var apex = (vzUp * vzUp) / (2 * G);
      if (apex < hopMinFt) break;
      var tI = 2 * vzUp / G;
      var lenI = shI * tI;
      hops.push({ lenFt: lenI, apexFt: apex, tS: tI });
      segments.push({ kind: "hop", t0: tCursor, d0: dCursor, speed: shI, dur: tI, vzUp: vzUp });
      tCursor += tI; dCursor += lenI;
      shI = fRetainH * shI;
      vzDown = vzUp;
    }

    var shRoll = shI;
    var rollDuration = rollDecel > 0 ? shRoll / rollDecel : 0;
    var rollReach = rollDecel > 0 ? (shRoll * shRoll) / (2 * rollDecel) : 0;
    segments.push({ kind: "roll", t0: tCursor, d0: dCursor, speed: shRoll, dur: rollDuration });
    var restFt = dCursor + rollReach;
    var totalS = tCursor + rollDuration;

    function distAt(tS) {
      tS = Math.max(0, tS);
      if (tS >= totalS) return restFt;
      for (var i = 0; i < segments.length; i++) {
        var seg = segments[i];
        if (tS <= seg.t0 + seg.dur) {
          var dtLocal = tS - seg.t0;
          if (seg.kind === "hop") return seg.d0 + seg.speed * dtLocal;
          return seg.d0 + seg.speed * dtLocal - 0.5 * rollDecel * dtLocal * dtLocal;
        }
      }
      return restFt;
    }

    function timeAt(distFt) {
      if (distFt > restFt) return null;
      if (distFt <= 0) return 0;
      for (var i = 0; i < segments.length; i++) {
        var seg = segments[i];
        var segLen = seg.kind === "hop" ? seg.speed * seg.dur : rollReach;
        if (distFt <= seg.d0 + segLen + 1e-9) {
          var local = distFt - seg.d0;
          if (seg.kind === "hop") return seg.t0 + (seg.speed > 0 ? local / seg.speed : 0);
          // local = speed*t - 0.5*decel*t^2 -> solve for t (the physical root)
          var a = -0.5 * rollDecel, b = seg.speed, c = -local;
          if (a === 0) return seg.t0 + (b > 0 ? local / b : 0);
          var disc = Math.max(0, b * b - 4 * a * c);
          var tLocal = (-b + Math.sqrt(disc)) / (2 * a);
          return seg.t0 + tLocal;
        }
      }
      return totalS;
    }

    // Height above ground at real time tS since landing - the vacuum
    // parabola during a hop, 0 while rolling. Used only for rendering
    // (Part 6.3's hop arcs); distAt/timeAt never need it.
    function heightAt(tS) {
      tS = Math.max(0, tS);
      for (var i = 0; i < segments.length; i++) {
        var seg = segments[i];
        if (tS <= seg.t0 + seg.dur) {
          if (seg.kind !== "hop") return 0;
          var dtLocal = tS - seg.t0;
          return Math.max(0, seg.vzUp * dtLocal - 0.5 * G * dtLocal * dtLocal);
        }
      }
      return 0;
    }

    return { restFt: restFt, totalS: totalS, distAt: distAt, timeAt: timeAt, heightAt: heightAt, hops: hops };
  }

  window.KMTraj = {
    simulateFlight: simulateFlight,
    groundPath: groundPath,
    CONTACT_HEIGHT_FT: CONTACT_HEIGHT_FT,
    CONTACT_Y0_FT: CONTACT_Y0_FT,
    DT_S: DT_S,
  };
})();

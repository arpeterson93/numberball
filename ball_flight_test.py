"""
Playwright harness for the ball-flight pure functions exposed on window.KMFlight
(docs/js/app.js). Loads the live page, calls the pure functions through
page.evaluate, and asserts against the worked examples and boundary cases in
ball-flight-plan.md Stage 3b.

No Python JS unit runner exists in this repo - this mirrors the established
pattern used to verify deriveRunnerMoves/basepathWaypoints/the cursor-capping
logic elsewhere in this project.

Run from the repo root:
    python ball_flight_test.py
"""
from __future__ import annotations

import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

PAGE_URL = pathlib.Path("docs/index.html").resolve().as_uri()

failures: list[str] = []


def check(label: str, got, want, tol: float = 0.01) -> None:
    ok = abs(got - want) <= tol if isinstance(want, (int, float)) else got == want
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(f"{label}: got={got!r} want={want!r}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_URL)
        page.wait_for_function("window.KMFlight != null && window.KMTraj != null")

        print("KMTraj physics parity (golden vectors vs the Python reference):")
        golden_fp = pathlib.Path("tools/golden_vectors.json")
        golden_vectors = json.loads(golden_fp.read_text(encoding="utf-8")) if golden_fp.exists() else []
        if not golden_vectors:
            print("  [skip] tools/golden_vectors.json not found - run `python tools/trajectory_reference.py --emit tools/golden_vectors.json`")
        for v in golden_vectors:
            r = page.evaluate(
                "(v) => KMTraj.simulateFlight(v.ev, v.la, v.phi, v.hand, {z0: v.z0})",
                v,
            )
            label = f"ev={v['ev']} la={v['la']} phi={v['phi']} hand={v['hand']} z0={v['z0']}"
            check(f"{label} distance", r["distance"], v["dist"], tol=0.01)
            check(f"{label} hangS", r["hangS"], v["hang_s"], tol=0.001)
            check(f"{label} landing.x", r["landing"]["x"], v["x"], tol=0.01)
            check(f"{label} landing.y", r["landing"]["y"], v["y"], tol=0.01)
            check(f"{label} apexFt", r["apexFt"], v["apex_ft"], tol=0.01)
            check(f"{label} contactVel.vx", r["contactVel"]["vx"], v["vx"], tol=0.01)
            check(f"{label} contactVel.vy", r["contactVel"]["vy"], v["vy"], tol=0.01)
            check(f"{label} contactVel.vz", r["contactVel"]["vz"], v["vz"], tol=0.01)
            # samples: last sample is the interpolated landing; times strictly
            # increasing; z never negative.
            samples = r["samples"]
            check(f"{label} samples last == landing x", samples[-1]["x"], v["x"], tol=0.01)
            check(f"{label} samples last == landing y", samples[-1]["y"], v["y"], tol=0.01)
            check(f"{label} samples last z == 0", samples[-1]["z"], 0, tol=1e-9)
            check(f"{label} samples count <= 48", len(samples) <= 48, True)
            strictly_increasing = all(samples[i]["t"] < samples[i + 1]["t"] for i in range(len(samples) - 1))
            check(f"{label} sample times strictly increasing", strictly_increasing, True)
            nonneg_z = all(s["z"] >= -1e-9 for s in samples)
            check(f"{label} sample z always >= 0", nonneg_z, True)

        print("\nKMTraj mirror property (R,+phi) vs (L,-phi):")
        mirror_result = page.evaluate(
            """() => {
                var bad = [];
                [85, 100, 110].forEach(function (ev) {
                  [-15, 5, 28].forEach(function (la) {
                    [0, 15, 30].forEach(function (phi) {
                      var rR = KMTraj.simulateFlight(ev, la, phi, 'R');
                      var rL = KMTraj.simulateFlight(ev, la, -phi, 'L');
                      if (!rR || !rL) { bad.push([ev, la, phi, 'null result']); return; }
                      var dDist = Math.abs(rR.distance - rL.distance);
                      var dHang = Math.abs(rR.hangS - rL.hangS);
                      var dX = Math.abs(rR.landing.x + rL.landing.x);
                      var dY = Math.abs(rR.landing.y - rL.landing.y);
                      if (dDist > 1e-6 || dHang > 1e-6 || dX > 1e-6 || dY > 1e-6) {
                        bad.push([ev, la, phi, dDist, dHang, dX, dY]);
                      }
                    });
                  });
                });
                return bad;
            }"""
        )
        check("mirror property holds over the (ev,la,phi) grid", len(mirror_result), 0)

        print("\nKMTraj HZ-angle mapping (phi = HZ - 45, Part 1):")
        hz_center = page.evaluate("KMTraj.simulateFlight(100, 28, 45 - 45, 'R')")
        check("HZ=45 (dead center) lands with small |bearing|", abs(hz_center["landing"]["x"]) < 60, True)
        hz_5 = page.evaluate("KMTraj.simulateFlight(100, 28, 5 - 45, 'R')")
        check("HZ=5 (3B line) lands with x < 0", hz_5["landing"]["x"] < 0, True)
        hz_85 = page.evaluate("KMTraj.simulateFlight(100, 28, 85 - 45, 'R')")
        check("HZ=85 (1B line) lands with x > 0", hz_85["landing"]["x"] > 0, True)
        # HZ=5 (phi=-40) and HZ=85 (phi=+40) are the mirror pair at the same
        # hand ('R' here) - opposite phi mirrors exactly (see the mirror
        # property block above); a same-HZ, opposite-hand pair does NOT
        # mirror at the KMTraj level, since hand only flips spin sign, not
        # phi - the HZ->phi mapping is hand-independent by construction
        # (Part 1: hand mirroring happens in app.js's HZ-angle formula, one
        # level up from here).
        hz_85_L = page.evaluate("KMTraj.simulateFlight(100, 28, 85 - 45, 'L')")
        check("HZ=5,R / HZ=85,L mirror (opposite phi, x)", hz_5["landing"]["x"], -hz_85_L["landing"]["x"], tol=1e-6)
        check("HZ=5,R / HZ=85,L mirror (opposite phi, distance)", hz_5["distance"], hz_85_L["distance"], tol=1e-6)

        print("\nKMTraj.groundPath (Part 3):")
        gp_go_contact = page.evaluate("KMTraj.simulateFlight(85, -11, 0, 'R')")
        gp_go = page.evaluate(
            "(c) => KMTraj.groundPath(Math.hypot(c.vx, c.vy), c.vz)",
            gp_go_contact["contactVel"],
        )
        along_ss = 147 - gp_go_contact["distance"]
        t_to_ss = page.evaluate(
            "(a) => KMTraj.groundPath(Math.hypot(a.c.vx, a.c.vy), a.c.vz).timeAt(a.along)",
            {"c": gp_go_contact["contactVel"], "along": along_ss},
        )
        check("GO-shaped reaches SS depth (147ft) within 1.0-1.9s of ground time",
              t_to_ss is not None and 1.0 <= t_to_ss <= 1.9, True)
        check("GO-shaped still moving past SS depth (restFt > alongFt)", gp_go["restFt"] > along_ss, True)

        gp_bunt_contact = page.evaluate("KMTraj.simulateFlight(35, -30, 0, 'R')")
        gp_bunt = page.evaluate(
            "(c) => KMTraj.groundPath(Math.hypot(c.vx, c.vy), c.vz)",
            gp_bunt_contact["contactVel"],
        )
        check("bunt-shaped never races out to SS depth (147ft)",
              gp_bunt_contact["distance"] + gp_bunt["restFt"] < 147, True)

        gp_weak_contact = page.evaluate("KMTraj.simulateFlight(60, 2, 0, 'R')")
        gp_weak = page.evaluate(
            "(c) => KMTraj.groundPath(Math.hypot(c.vx, c.vy), c.vz)",
            gp_weak_contact["contactVel"],
        )
        check("weak grounder (SS-assigned) rests well short of 147ft",
              gp_weak_contact["distance"] + gp_weak["restFt"] < 147, True)

        monotonic_result = page.evaluate(
            """(a) => {
                var gp = KMTraj.groundPath(a.sh, a.vz);
                var bad = 0;
                var prev = -1;
                for (var t = 0; t <= gp.totalS + 0.5; t += 0.05) {
                  var d = gp.distAt(t);
                  if (d < prev - 1e-9) bad++;
                  prev = d;
                }
                var roundtrip = [];
                for (var tt = 0.1; tt < gp.totalS; tt += gp.totalS / 10) {
                  var dist = gp.distAt(tt);
                  var back = gp.timeAt(dist);
                  roundtrip.push(Math.abs(back - tt));
                }
                return { bad: bad, maxRoundtripErr: Math.max.apply(null, roundtrip) };
            }""",
            {
                "sh": (gp_go_contact["contactVel"]["vx"] ** 2 + gp_go_contact["contactVel"]["vy"] ** 2) ** 0.5,
                "vz": gp_go_contact["contactVel"]["vz"],
            },
        )
        check("distAt is monotonic non-decreasing", monotonic_result["bad"], 0)
        check("timeAt(distAt(t)) round-trips within 5ms", monotonic_result["maxRoundtripErr"] < 0.005, True)

        print("\nBoundary assertions:")
        check("signedCirc(0,5,10)", page.evaluate("KMFlight.signedCirc(0,5,10)"), 5)
        check("signedCirc(5,0,10)", page.evaluate("KMFlight.signedCirc(5,0,10)"), -5)
        check("signedCirc(98,2,100)", page.evaluate("KMFlight.signedCirc(98,2,100)"), 4)
        check("signedCirc(2,98,100)", page.evaluate("KMFlight.signedCirc(2,98,100)"), -4)
        check("firstTwo(1)", page.evaluate("KMFlight.firstTwo(1)"), 0)
        check("firstTwo(1000)", page.evaluate("KMFlight.firstTwo(1000)"), 99)
        check("onesDigit(1)", page.evaluate("KMFlight.onesDigit(1)"), 0)
        check("onesDigit(1000)", page.evaluate("KMFlight.onesDigit(1000)"), 9)
        # lastDigit: HZ's own digit extraction, deliberately NOT shifted like
        # onesDigit - 1000 lands on 0 (not 9), so a raw pitch/swing pair reads
        # as a literal digit subtraction (Alex's ask).
        check("lastDigit(1)", page.evaluate("KMFlight.lastDigit(1)"), 1)
        check("lastDigit(9)", page.evaluate("KMFlight.lastDigit(9)"), 9)
        check("lastDigit(10)", page.evaluate("KMFlight.lastDigit(10)"), 0)
        check("lastDigit(1000)", page.evaluate("KMFlight.lastDigit(1000)"), 0)
        check("lastDigit(220)", page.evaluate("KMFlight.lastDigit(220)"), 0)
        check("lastDigit(225)", page.evaluate("KMFlight.lastDigit(225)"), 5)
        # The exact case Alex raised: pitch=220, swing=225 should read as a
        # plain "+5" (swing 5 to the right of pitch), not silently flip sign
        # at the wheel's halfway tie the way onesDigit's -1 shift would.
        check(
            "signedCirc(lastDigit(220), lastDigit(225), 10) reads as literal +5",
            page.evaluate("KMFlight.signedCirc(KMFlight.lastDigit(220), KMFlight.lastDigit(225), 10)"),
            5,
        )

        tables = {
            "excluded": [
                "K", "BB", "IBB", "AutoBB", "AutoK", "CS", "CS2", "CS3", "CS4",
                "SB", "SB2", "SB3", "SB4", "AutoSB", "Balk", "pAuto", "bAuto",
            ],
            # Each band carries its own laMin/laIdeal/laMax/evMin/evMax/
            # depthMin/depthMax directly (result_diff_bands.csv's real shape -
            # the old separate archetype-keyed range table is retired). These
            # illustrative mock numbers aren't the real Statcast-derived ones;
            # they only need to be internally consistent enough to validate
            # the formula's own mechanics.
            "bands": {
                "HR": {"archetype": "home_run", "lo": 2, "hi": 21,
                       "laMin": 25, "laIdeal": 28, "laMax": 35, "evMin": 98, "evMax": 115, "depthMin": 370, "depthMax": 420},
                "GO": {"archetype": "grounder", "lo": 180, "hi": 470,
                       "laMin": -15, "laIdeal": 8, "laMax": 8, "evMin": 70, "evMax": 100, "depthMin": 60, "depthMax": 150},
                "FO": {"archetype": "fly_ball", "lo": 55, "hi": 240,
                       "laMin": 25, "laIdeal": 28, "laMax": 50, "evMin": 80, "evMax": 98, "depthMin": 250, "depthMax": 380},
                # C5 recalibration: was -5,10,...,60,160 - half of real singles
                # landed on the infield dirt. See the infield-dirt sweep below.
                "1B": {"archetype": "single", "lo": 20, "hi": 150,
                       "laMin": 6, "laIdeal": 12, "laMax": 20, "evMin": 75, "evMax": 98, "depthMin": 130, "depthMax": 230},
            },
        }

        print("\nflightParams scope checks:")
        for code in tables["excluded"]:
            r = page.evaluate(
                "(t) => KMFlight.flightParams({result: t.c, pitch: 100, swing: 200, batter_hand: 'R', diff: 50}, t.tables)",
                {"c": code, "tables": tables},
            )
            check(f"excluded({code}) is null", r, None)
        r = page.evaluate(
            "(t) => KMFlight.flightParams({result: 'HR', pitch: null, swing: 200, batter_hand: 'R', diff: 5}, t)",
            tables,
        )
        check("null pitch -> null", r, None)

        # la/angle/ev assertions are unchanged - that gating/wheel path never
        # moved in the physics-redesign port (Part 2.3). distance/x/y/hangMs
        # used to be hand-computed literals against the OLD depthMin+q*(...)
        # /vacuum-hangtime formula; that formula is gone (Part 2.3/2.4), so
        # those fields are checked against a fresh, independent
        # KMTraj.simulateFlight(ev, la, angle-45, hand) call plus the same
        # clampToFence step flightParams itself applies - this is the actual
        # port contract (did flightParams wire EV/LA/angle/hand into KMTraj
        # correctly and apply clamp/scale right), not a re-test of the
        # integrator's own correctness (that's the golden-vector parity block
        # above, checked independently against tools/trajectory_reference.py).
        cases = [
            {
                # la: q = 1 - clamp((5-2)/(21-2)) = 16/19 = 0.8421 (close to the
                # band's low end, i.e. well-timed contact); onTop (swing > pitch,
                # all digits) -> LA = 28 - (1-q)*(28-25) = 27.53, close to but
                # not quite laIdeal since this diff isn't AT the band's floor.
                "label": "HR pulled RHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "R", "diff": 5},
                "want": {"la": 27.53, "angle": 5.00, "ev": 112.32},
            },
            {
                "label": "HR same numbers LHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "L", "diff": 5},
                "want": {"la": 27.53, "angle": 85.00},
            },
            {
                # la: q clamps to 0 (diff 481 is past the GO band's own hi=470),
                # so LA bottoms out at grounder's laMin (-15) regardless of
                # direction - the "least ideal" end of the formula's range.
                "label": "Groundout RHH",
                "play": {"result": "GO", "pitch": 150, "swing": 631, "batter_hand": "R", "diff": 481},
                "want": {"la": -15.0, "angle": 53.00, "ev": 70.00},
            },
            {
                # la: diff 48 is below the FO band's own lo=55, so q clamps to 1 -
                # LA lands exactly on fly_ball's laIdeal (28), independent of
                # direction (the (1-q) deviation term is zero at q=1).
                "label": "Flyout RHH",
                "play": {"result": "FO", "pitch": 220, "swing": 268, "batter_hand": "R", "diff": 48},
                "want": {"la": 28.0, "angle": 29.00, "ev": 98.00},
            },
            {
                # la: q = 1 - clamp((87-20)/(150-20)) = 1 - 67/130 = 0.4846; swing
                # (801) < pitch (888) on the full 1000-wheel -> "below" the pitch
                # (uppercut) -> LA = 12 + (1-q)*(20-12) = 16.12.
                "label": "Single LHH",
                "play": {"result": "1B", "pitch": 888, "swing": 801, "batter_hand": "L", "diff": 87},
                "want": {"la": 16.12, "angle": 21.00, "ev": 86.15},
            },
        ]

        print("\nWorked examples:")
        for c in cases:
            print(f" {c['label']}:")
            hand = c["play"]["batter_hand"]
            r = page.evaluate("(a) => KMFlight.flightParams(a.play, a.tables)", {"play": c["play"], "tables": tables})
            for key, want in c["want"].items():
                tol = 0.005 if key in ("la", "angle") else 0.15
                check(f"{c['label']}.{key}", r[key], want, tol=tol)

            oracle = page.evaluate(
                """(a) => {
                    var sim = KMTraj.simulateFlight(a.ev, a.la, a.angle - 45, a.hand);
                    var isHomeRun = a.result === 'HR';
                    var D = KMFlight.clampToFence(sim.distance, a.angle, isHomeRun);
                    var scale = D / sim.distance;
                    return {
                        distance: D, x: sim.landing.x * scale, y: sim.landing.y * scale,
                        hangMs: 1000 * sim.hangS, apexFt: sim.apexFt,
                    };
                }""",
                {"ev": r["ev"], "la": r["la"], "angle": r["angle"], "hand": hand, "result": c["play"]["result"]},
            )
            check(f"{c['label']}.distance matches independent KMTraj+clampToFence oracle", r["distance"], oracle["distance"], tol=0.01)
            check(f"{c['label']}.x matches oracle", r["x"], oracle["x"], tol=0.01)
            check(f"{c['label']}.y matches oracle", r["y"], oracle["y"], tol=0.01)
            check(f"{c['label']}.hangMs matches oracle", r["hangMs"], oracle["hangMs"], tol=0.01)
            check(f"{c['label']}.apexFt matches oracle", r["apexFt"], oracle["apexFt"], tol=0.01)
            check(f"{c['label']}.hangMs is a real number (F10: no more null-for-grounders)", r["hangMs"] is not None, True)

        print("\nq-clamp checks:")
        r_below = page.evaluate(
            "(t) => KMFlight.flightParams({result:'HR', pitch:500, swing:501, batter_hand:'R', diff:-5}, t)", tables
        )
        r_above = page.evaluate(
            "(t) => KMFlight.flightParams({result:'HR', pitch:500, swing:501, batter_hand:'R', diff:9999}, t)", tables
        )
        check("diff below band -> q=1 -> max EV", r_below["ev"], 115.0)
        check("diff above band -> q=0 -> min EV", r_above["ev"], 98.0)

        print("\nGround-truth invariant sweep (non-HR never clears the uniform fence):")
        sweep_result = page.evaluate(
            """(t) => {
                var fence = KMFlight.FENCE_DEPTH_FT;
                var violations = 0, checked = 0, hrOver = 0, hrInside = 0;
                var nonHrCodes = Object.keys(t.bands).filter(r => r !== 'HR');
                for (var p = 1; p <= 1000; p += 13) {
                  for (var s = 1; s <= 1000; s += 17) {
                    nonHrCodes.forEach(function (code) {
                      var diff = Math.min(Math.abs(p - s), 1000 - Math.abs(p - s));
                      var fp = KMFlight.flightParams({result: code, pitch: p, swing: s, batter_hand: 'R', diff: diff}, t);
                      checked++;
                      if (fp && fp.distance > fence) violations++;
                    });
                    var diff = Math.min(Math.abs(p - s), 1000 - Math.abs(p - s));
                    var hr = KMFlight.flightParams({result: 'HR', pitch: p, swing: s, batter_hand: 'R', diff: diff}, t);
                    if (hr) { if (hr.clearedFence) hrOver++; else hrInside++; }
                  }
                }
                return {violations: violations, checked: checked, hrOver: hrOver, hrInside: hrInside};
            }""",
            tables,
        )
        print(f"  checked {sweep_result['checked']} non-HR (pitch,swing,result) combos")
        check("non-HR fence violations", sweep_result["violations"], 0)
        print(f"  HR sweep: {sweep_result['hrOver']} over the fence, {sweep_result['hrInside']} inside the park")
        check("HR over-the-fence reachable", sweep_result["hrOver"] > 0, True)
        check("HR inside-the-park reachable", sweep_result["hrInside"] > 0, True)

        # ── Refinements round (ball-flight-refinements-plan.md) ─────────────

        print("\nE1 tie-break assertions (signedCirc at the wheel's exact halfway point):")
        check("signedCirc(3,8,10)", page.evaluate("KMFlight.signedCirc(3,8,10)"), 5)
        check("signedCirc(8,3,10)", page.evaluate("KMFlight.signedCirc(8,3,10)"), -5)
        check("signedCirc(30,80,100)", page.evaluate("KMFlight.signedCirc(30,80,100)"), 50)
        check("signedCirc(80,30,100)", page.evaluate("KMFlight.signedCirc(80,30,100)"), -50)
        tie_result = page.evaluate(
            """() => {
                var bad = [];
                [10, 100].forEach(function (mod) {
                  for (var a = 0; a < mod; a++) {
                    var b = (a + mod / 2) % mod;
                    var got = KMFlight.signedCirc(a, b, mod);
                    var want = b > a ? mod / 2 : -mod / 2;
                    if (got !== want) bad.push([mod, a, b, got, want]);
                  }
                });
                return bad;
            }"""
        )
        check("every tie pair on both wheels matches anchor-and-no-wrap", len(tie_result), 0)

        print("\nordinal():")
        ordinal_cases = {
            1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th",
            13: "13th", 21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th",
        }
        for n, want in ordinal_cases.items():
            check(f"ordinal({n})", page.evaluate(f"KMFlight.ordinal({n})"), want)

        print("\noutThrowTargets (A1-A3, B6) - real play shapes from the refinements plan:")
        # archetype is whatever flight.archetype would be for that result - a
        # mock flight object is enough here, outThrowTargets only reads
        # .archetype and .clearedFence off it.
        throw_cases = [
            ("GO", "000", "000", 0, 0, 1, "grounder", ["1B"]),
            ("FO", "000", "000", 0, 0, 1, "fly_ball", []),
            ("PO", "001", "001", 0, 0, 1, "pop_up", []),
            ("SacF", "101", "001", 1, 0, 1, "fly_ball", ["HOME"]),
            ("DP", "001", "000", 0, 0, 2, "grounder", ["2B", "1B"]),
            # NEXT_BASE fix: a runner already on 3B who's OUT was headed
            # home, not "forced to stay at 3rd" (BASE_PATH's old
            # Math.min(3, ...) capping bug - see docs/js/app.js's NEXT_BASE).
            ("DP", "101", "000", 0, 0, 2, "grounder", ["HOME", "2B"]),
            ("DPH1", "111", "110", 0, 0, 2, "grounder", ["HOME", "1B"]),
            ("LODP", "010", "000", 0, 0, 2, "line_drive", ["2B"]),
            ("FC", "001", "001", 0, 0, 1, "grounder", ["2B"]),
            ("FC3rd", "011", "011", 0, 0, 1, "grounder", ["3B"]),
            ("FCH", "111", "111", 0, 0, 1, "grounder", ["HOME"]),
            ("HR", "000", "000", 1, 0, 0, "home_run", []),
            ("1B", "000", "001", 0, 0, 0, "single", []),
        ]
        for result, before, after, runs, outs_before, outs_after, archetype, want in throw_cases:
            m = {"result": result, "outs_before": outs_before, "outs_after": outs_after}
            flight = {"archetype": archetype, "clearedFence": False}
            got = page.evaluate(
                """(a) => {
                    var moves = KMFlight.deriveRunnerMoves(a.before, a.after, a.runs);
                    return KMFlight.outThrowTargets(a.m, moves, a.flight);
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"outThrowTargets({result} {before}->{after})", got, want)

        print("\nstealThrowTarget (B3-c - catcher throw on steal attempts):")
        steal_cases = [
            ("SB2", "001", "010", {"base": "2B", "caught": False, "delay": False}),
            ("SB3", "010", "100", {"base": "3B", "caught": False, "delay": False}),
            ("CS2", "001", "000", {"base": "2B", "caught": True, "delay": False}),
            ("CS", "010", "000", {"base": "3B", "caught": True, "delay": False}),
            ("GO", "000", "000", None),
        ]
        for result, before, after, want in steal_cases:
            got = page.evaluate(
                """(a) => {
                    var moves = KMFlight.deriveRunnerMoves(a.before, a.after, 0);
                    return KMFlight.stealThrowTarget({result: a.result}, moves);
                }""",
                {"before": before, "after": after, "result": result},
            )
            check(f"stealThrowTarget({result} {before}->{after})", got, want)

        print("\nA4 timing race (every grounder throw must beat the runner to the bag):")
        print("worst-case flight: a real, physics-derived slow grounder (Part 3.3's GO-shaped")
        print("contact, near the top of its 1.0-1.9s ground-time acceptance range), not a mock:")
        worst_case_go = page.evaluate("KMTraj.simulateFlight(85, -11, 0, 'R')")
        # groundTimeS the resolver would actually hand to fieldedMs: time to
        # reach the deepest infield depth (SS/2B, 147ft) along the ground -
        # NOT groundPath's own totalS-to-complete-rest (that's only reached
        # on a charge-in, which by definition means a SHORT, not slow, ground
        # time - using totalS here would test a scenario the resolver never
        # actually produces).
        worst_case_along = 147 - worst_case_go["distance"]
        worst_case_ground_time_s = page.evaluate(
            "(a) => KMTraj.groundPath(Math.hypot(a.c.vx, a.c.vy), a.c.vz).timeAt(a.along)",
            {"c": worst_case_go["contactVel"], "along": worst_case_along},
        )
        for result, before, after, runs, outs_before, outs_after, archetype, want in throw_cases:
            if archetype != "grounder" or not want:
                continue
            m = {"result": result, "outs_before": outs_before, "outs_after": outs_after}
            flight = {
                "archetype": archetype, "clearedFence": False,
                "hangMs": 1000 * worst_case_go["hangS"], "groundTimeS": worst_case_ground_time_s,
            }
            margin = page.evaluate(
                """(a) => {
                    var moves = KMFlight.deriveRunnerMoves(a.before, a.after, a.runs);
                    var schedule = KMFlight.throwSchedule(a.m, moves, a.flight);
                    var lastEnd = Math.max.apply(null, schedule.map(t => t.endMs));
                    return KMFlight.batterFirstArrivalMs() - (lastEnd + KMFlight.THROW_LEAD_MS);
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"throw beats runner by >=0ms ({result} {before}->{after})", margin >= 0, True)

        print("\nTag-throw timing (no outfield-assist scenario exists yet, so a fly")
        print("ball/pop-up's throw - SacF's decorative 'throw home anyway' - must")
        print("never beat the safe runner it's chasing):")
        for result, before, after, runs, outs_before, outs_after, archetype, want in throw_cases:
            if archetype not in ("fly_ball", "pop_up") or not want:
                continue
            m = {"result": result, "outs_before": outs_before, "outs_after": outs_after}
            flight = {"archetype": archetype, "clearedFence": False, "hangMs": 4700}
            margin = page.evaluate(
                """(a) => {
                    var moves = KMFlight.deriveRunnerMoves(a.before, a.after, a.runs);
                    var schedule = KMFlight.throwSchedule(a.m, moves, a.flight);
                    var catchMs = KMFlight.ballTravelMs(a.flight);
                    var runnerArrival = 0;
                    moves.forEach(function (mv) {
                        if (mv.to === "OUT") return;
                        var startOrd = mv.from === "BATTER" ? 0 : KMFlight.BASE_ORDINAL[mv.from];
                        var endOrd = mv.scored ? 4 : KMFlight.BASE_ORDINAL[mv.to];
                        if (startOrd == null || endOrd == null || endOrd <= startOrd) return;
                        var legs = Math.min(endOrd - startOrd, KMFlight.RUN_LEG_MS.length - 1);
                        runnerArrival = Math.max(runnerArrival, catchMs + KMFlight.TAG_UP_MS + (KMFlight.RUN_LEG_MS[legs] || 0));
                    });
                    var lastEnd = Math.max.apply(null, schedule.map(t => t.endMs));
                    return lastEnd - (runnerArrival + KMFlight.TAG_THROW_MARGIN_MS);
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"runner beats throw by >=0ms ({result} {before}->{after})", margin >= 0, True)

        print("\nThrow-target sweep (ground-truth invariant restated for throws):")
        meta_fp = pathlib.Path("docs/data/meta.json")
        real_plays: list[dict] = []
        for n in (1, 2, 3):
            fp = pathlib.Path(f"docs/data/plays_{n:02d}.json")
            if fp.exists():
                real_plays.extend(json.loads(fp.read_text(encoding="utf-8")))
        if meta_fp.exists() and real_plays:
            real_tables = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]
            sweep = page.evaluate(
                """(a) => {
                    var bad = [];
                    a.plays.forEach(function (m) {
                        var flight = KMFlight.flightParams(m, a.tables);
                        var moves = KMFlight.deriveRunnerMoves(
                            String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                        var targets = KMFlight.outThrowTargets(m, moves, flight);
                        var recorded = Math.max(0, (m.outs_after || 0) - (m.outs_before || 0));
                        if (targets.length > recorded) bad.push(m.moment_id + ": too many targets");
                        var seen = {};
                        targets.forEach(function (b) {
                            if (seen[b]) bad.push(m.moment_id + ": dup base " + b);
                            seen[b] = 1;
                        });
                    });
                    return bad;
                }""",
                {"plays": real_plays, "tables": real_tables},
            )
            print(f"  swept {len(real_plays)} real plays")
            check("throw targets never exceed outs recorded, never duplicate a base", len(sweep), 0)
            for bad in sweep[:10]:
                print(f"    - {bad}")
        else:
            print("  [skip] no real feed data / meta.json found on disk")

        print("\nFull-pipeline sweep (Stage C cut-over): every real play run through the")
        print("exact dispatch playSceneHtml uses (flightParams -> resolver/air-override/")
        print("resolveHitPickup), asserting no exceptions, no NaN outputs, and the")
        print("fieldedFt <= alongFt / fieldedDistFt >= distance invariant everywhere:")
        if meta_fp.exists() and real_plays:
            pipeline = page.evaluate(
                """(a) => {
                    var bad = [];
                    var counts = { groundOut: 0, airOut: 0, hit: 0, cleared: 0 };
                    a.plays.forEach(function (m) {
                        try {
                            var flight = KMFlight.flightParams(m, a.tables);
                            if (!flight) return;
                            var hand = KMFlight.effectiveHand(m.batter_hand);
                            var wasOut = (m.outs_after || 0) > (m.outs_before || 0);
                            if (KMFlight.GROUND_ARCHETYPES[flight.archetype] && wasOut) {
                                KMFlight.resolveGrounderInterception(m, flight, hand);
                                counts.groundOut++;
                                if (flight.fieldedDistFt < flight.distance - 1e-6) bad.push(m.moment_id + ": fieldedDistFt < distance");
                            } else if (wasOut && KMFlight.CAUGHT_IN_AIR[flight.archetype]) {
                                KMFlight.applyAirPositionOverride(m, flight, hand);
                                counts.airOut++;
                            } else if (!flight.clearedFence) {
                                KMFlight.resolveHitPickup(flight);
                                counts.hit++;
                                if (flight.fieldedDistFt < flight.distance - 1e-6) bad.push(m.moment_id + ": fieldedDistFt < distance (hit)");
                            } else {
                                counts.cleared++;
                            }
                            ["la", "ev", "distance", "angle", "x", "y", "hangMs", "apexFt"].forEach(function (k) {
                                if (typeof flight[k] !== "number" || isNaN(flight[k])) bad.push(m.moment_id + ": NaN/non-number " + k);
                            });
                            var travelMs = KMFlight.ballTravelMs(flight);
                            var fielded = KMFlight.fieldedMs(flight);
                            if (isNaN(travelMs) || isNaN(fielded)) bad.push(m.moment_id + ": NaN timing");
                            if (fielded < travelMs - 1e-6) bad.push(m.moment_id + ": fieldedMs < ballTravelMs");
                        } catch (e) {
                            bad.push(m.moment_id + ": EXCEPTION " + e.message);
                        }
                    });
                    return { bad: bad, counts: counts };
                }""",
                {"plays": real_plays, "tables": real_tables},
            )
            c = pipeline["counts"]
            print(f"  {c['groundOut']} ground outs, {c['airOut']} air outs, {c['hit']} hits, {c['cleared']} cleared-fence HRs")
            check("full pipeline: no exceptions, no NaN, fieldedDistFt/timing invariants hold", len(pipeline["bad"]), 0)
            for bad in pipeline["bad"][:10]:
                print(f"    - {bad}")
        else:
            print("  [skip] no real feed data / meta.json found on disk")

        print("\nCSV validity (F6/F9 - ball-flight-3d-physics-redesign-plan.md Part 2.4/11):")
        print("depth_min/depth_max are audit-only now (nothing at runtime reads them - a")
        print("grounder's landing distance comes from KMTraj.simulateFlight's LA/EV")
        print("integration, same as every other archetype), so a low depthMax on a")
        print("grounder-family result (its real hit_distance_sc first-bounce percentile,")
        print("not a fielding depth) is expected and correct, not a regression:")
        if meta_fp.exists():
            bands = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]["bands"]
            for result, band in bands.items():
                for col in ("laMin", "laIdeal", "laMax", "evMin", "evMax", "depthMin", "depthMax"):
                    check(f"{result}.{col} is non-null", band.get(col) is not None, True)
                check(f"{result} laMin < laIdeal < laMax", band["laMin"] < band["laIdeal"] < band["laMax"], True)
                check(f"{result} band_lo != band_hi", band["lo"] != band["hi"], True)
        else:
            print("  [skip] no meta.json found on disk")

        print("\nResolver grid sweep (physics-redesign plan Part 4/11): one function")
        print("replaces five disagreeing mechanisms - assert its invariants hold over")
        print("every HZ angle x contact-quality combination, not just one point:")
        resolver_sweep = page.evaluate(
            """(t) => {
                var bad = [];
                var band = t.bands.GO;
                var angles = [5, 13, 21, 29, 37, 45, 53, 61, 69, 77, 85];
                ["R", "L"].forEach(function (hand) {
                  angles.forEach(function (angle) {
                    for (var q = 0; q <= 1; q += 0.25) {
                      var la = band.laIdeal + (1 - q) * (band.laMax - band.laIdeal);
                      var ev = band.evMin + q * (band.evMax - band.evMin);
                      var sim = KMTraj.simulateFlight(ev, la, angle - 45, hand);
                      var flight = {
                        la: la, ev: ev, distance: sim.distance, angle: angle,
                        x: sim.landing.x, y: sim.landing.y, contactVel: sim.contactVel,
                        archetype: "grounder",
                      };
                      var m = {outs_before: 0, outs_after: 1};
                      KMFlight.resolveGrounderInterception(m, flight, hand);
                      var hzPos = KMFlight.HZ_FIELDER_BY_ANGLE[Math.round(angle)];
                      if (flight.fielder !== hzPos) bad.push("angle=" + angle + " hand=" + hand + ": fielder " + flight.fielder + " != HZ answer " + hzPos);
                      var alongFt = flight.fieldedDistFt - flight.distance;
                      if (alongFt < -1e-6) bad.push("angle=" + angle + " hand=" + hand + ": fieldedDistFt before landing point");
                      var depth = KMFlight.INFIELDER_DEPTH_FT[flight.fielder];
                      if (flight.fieldedDistFt > depth + 1e-6 && flight.fieldedDistFt < flight.distance + 1e6) {
                        // fielded past the assigned depth is only OK if the ball landed past it already
                        if (sim.distance <= depth) bad.push("angle=" + angle + " hand=" + hand + ": fielded past assigned depth " + depth);
                      }
                    }
                  });
                });
                return bad;
            }""",
            tables,
        )
        check("resolver: fielder always matches the HZ answer, fielded point never beyond assigned depth", len(resolver_sweep), 0)

        print("\nResolver BRC override (F1/F3): a DP31-shaped synthetic config (excludes")
        print("SS/2B/1B/P, default 3B) on a shallow-landing grounder still triggers,")
        print("re-simulates at a lattice angle, and leaves LA/EV untouched:")
        override_result = page.evaluate(
            """(t) => {
                var band = t.bands.GO;
                var hand = "R";
                var angle = 45;  // HZ answer would be P
                var la = band.laIdeal, ev = band.evMin;
                var sim = KMTraj.simulateFlight(ev, la, angle - 45, hand);
                var flight = {
                    la: la, ev: ev, distance: sim.distance, angle: angle,
                    x: sim.landing.x, y: sim.landing.y, contactVel: sim.contactVel,
                    archetype: "grounder",
                };
                var m = {
                    outs_before: 0, outs_after: 1,
                    excluded_positions: ["SS", "2B", "1B", "P"], default_position: "3B",
                };
                KMFlight.resolveGrounderInterception(m, flight, hand);
                return {
                    fielder: flight.fielder, angle: flight.angle, la: flight.la, ev: flight.ev,
                    onLattice: KMFlight.MIN_ANGLE_FOR_POS["3B"] === flight.angle,
                };
            }""",
            tables,
        )
        check("BRC override fires and assigns the default position", override_result["fielder"], "3B")
        check("BRC override lands on the default position's lattice angle", override_result["onLattice"], True)
        check("BRC override leaves LA untouched", override_result["la"], tables["bands"]["GO"]["laIdeal"], tol=1e-6)
        check("BRC override leaves EV untouched", override_result["ev"], tables["bands"]["GO"]["evMin"], tol=1e-6)

        print("\nC5 infield-dirt regression: no real 1B/1BWH/1BWH2 may land on the dirt")
        real_singles = []
        for n in (1, 2, 3):
            fp = pathlib.Path(f"docs/data/plays_{n:02d}.json")
            if not fp.exists():
                continue
            for p in json.loads(fp.read_text(encoding="utf-8")):
                if p.get("result") in ("1B", "1BWH", "1BWH2") and p.get("pitch") is not None and p.get("swing") is not None:
                    real_singles.append(p)
        print(f"  {len(real_singles)} real singles found across plays_01/02/03.json")
        print("Physics-redesign note: the raw LANDING point (flightParams' x/y) is no")
        print("longer what this checks - a low-LA single can legitimately bounce short,")
        print("on the dirt, and then skid/roll well past it before anyone picks it up")
        print("(Part 2.5/4.6 - the old hand-tuned depth formula never modeled that skid at")
        print("all, so it had to keep every single's LANDING point off the dirt instead).")
        print("What must still never happen is the ball being PICKED UP on the dirt -")
        print("checked against resolveHitPickup's fielded point, same as playSceneHtml")
        print("computes for every real hit that stays in the park:")
        # Real bands (result_diff_bands.csv via meta.json), not the small
        # illustrative mock table above - the mock's 1B row is only
        # internally consistent enough to validate flightParams' formula
        # mechanics, not calibrated for a physically-real dirt check against
        # real play data.
        dirt_tables = real_tables if "real_tables" in dir() else tables
        if real_singles and "real_tables" in dir():
            dirt_count = page.evaluate(
                """(a) => {
                    var n = 0;
                    a.plays.forEach(function (p) {
                        var fp = KMFlight.flightParams(p, a.tables);
                        if (!fp) return;
                        KMFlight.resolveHitPickup(fp);
                        var picked = KMFlight.fieldedPoint(fp);
                        // Infield dirt polygon: the home/1B/2B/3B diamond, in
                        // field-plane feet (ball-flight-refinements-plan.md F15).
                        if (Math.abs(picked.x) + Math.abs(picked.y - 63.64) <= 63.64) n++;
                    });
                    return n;
                }""",
                {"plays": real_singles, "tables": dirt_tables},
            )
            check("real singles picked up on the infield dirt", dirt_count, 0)
        else:
            print("  [skip] no real single data / real tables found on disk")

        browser.close()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All ball-flight checks passed.")


if __name__ == "__main__":
    main()

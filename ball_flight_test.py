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
import math
import pathlib
import re
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
            # the old separate archetype-keyed range table is retired) - these
            # are reference/audit only now, kept only as launchAngleFor's
            # fallback path. `stations` is the primary path (joint EV/LA/
            # distance selection, ideas-and-opinions conversation): 3 clean
            # synthetic percentile points (q=0/0.5/1) per band, illustrative
            # numbers chosen to be hand-interpolable, not the real Statcast-
            # derived ones - they only need to be internally consistent
            # enough to validate stationsLookup's own interpolation
            # arithmetic. Each topped/uppercut entry is a real-play-shaped
            # (la, ev, dist) TRIPLE read together, not an EV solved backward
            # through physics and not a distance shared with a different
            # play at the same rank (an earlier design's bug) - topped and
            # uppercut deliberately get their own distinct dist values below
            # to exercise that.
            "bands": {
                "HR": {"archetype": "home_run", "lo": 2, "hi": 21,
                       "laMin": 25, "laIdeal": 28, "laMax": 35, "evMin": 98, "evMax": 115, "depthMin": 370, "depthMax": 420,
                       "stations": [
                           # q=0's targets sit below the fence (345/355 < 375)
                           # on purpose, matching the real generated data's own
                           # shape (its own q=0 station lands under the fence
                           # too) - the ground-truth invariant sweep below
                           # checks inside-the-park HRs are still reachable,
                           # not just over-the-fence ones (Alex's ground-truth
                           # invariant: both are legal outcomes for a HR).
                           {"q": 0.0, "laTopped": 25, "evTopped": 100, "distTopped": 345, "laUppercut": 32, "evUppercut": 95, "distUppercut": 355},
                           {"q": 0.5, "laTopped": 27, "evTopped": 108, "distTopped": 405, "laUppercut": 30, "evUppercut": 103, "distUppercut": 415},
                           {"q": 1.0, "laTopped": 28, "evTopped": 112, "distTopped": 435, "laUppercut": 29, "evUppercut": 107, "distUppercut": 445},
                       ]},
                "GO": {"archetype": "grounder", "lo": 180, "hi": 470,
                       "laMin": -15, "laIdeal": 8, "laMax": 8, "evMin": 70, "evMax": 100, "depthMin": 60, "depthMax": 150,
                       "stations": [
                           {"q": 0.0, "laTopped": -30, "evTopped": 40, "distTopped": 35, "laUppercut": 10, "evUppercut": 60, "distUppercut": 45},
                           {"q": 0.5, "laTopped": -20, "evTopped": 55, "distTopped": 145, "laUppercut": 8, "evUppercut": 75, "distUppercut": 155},
                           {"q": 1.0, "laTopped": -15, "evTopped": 65, "distTopped": 275, "laUppercut": 6, "evUppercut": 90, "distUppercut": 285},
                       ]},
                "FO": {"archetype": "fly_ball", "lo": 55, "hi": 240,
                       "laMin": 25, "laIdeal": 28, "laMax": 50, "evMin": 80, "evMax": 98, "depthMin": 250, "depthMax": 380,
                       "stations": [
                           {"q": 0.0, "laTopped": 35, "evTopped": 75, "distTopped": 195, "laUppercut": 55, "evUppercut": 70, "distUppercut": 205},
                           {"q": 0.5, "laTopped": 30, "evTopped": 85, "distTopped": 275, "laUppercut": 45, "evUppercut": 80, "distUppercut": 285},
                           {"q": 1.0, "laTopped": 28, "evTopped": 95, "distTopped": 345, "laUppercut": 40, "evUppercut": 90, "distUppercut": 355},
                       ]},
                # C5 recalibration: was -5,10,...,60,160 - half of real singles
                # landed on the infield dirt. See the infield-dirt sweep below.
                "1B": {"archetype": "single", "lo": 20, "hi": 150,
                       "laMin": 6, "laIdeal": 12, "laMax": 20, "evMin": 75, "evMax": 98, "depthMin": 130, "depthMax": 230,
                       "stations": [
                           {"q": 0.0, "laTopped": 2, "evTopped": 70, "distTopped": 45, "laUppercut": 25, "evUppercut": 80, "distUppercut": 55},
                           {"q": 0.5, "laTopped": 8, "evTopped": 85, "distTopped": 145, "laUppercut": 18, "evUppercut": 90, "distUppercut": 155},
                           {"q": 1.0, "laTopped": 10, "evTopped": 90, "distTopped": 245, "laUppercut": 15, "evUppercut": 95, "distUppercut": 255},
                       ]},
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

        # la/ev/angle assertions are exact (hand/script-derived from the mock
        # `stations` tables above via linear interpolation - stationsLookup's
        # own arithmetic; la and ev now come straight off the same real-play-
        # shaped pair, no solving involved). distance/x/y/hangMs are checked
        # against a fresh, independent KMTraj.simulateFlight(ev, la, angle-45,
        # hand) call plus the same radial-scale-to-real-distance + distanceCap steps
        # flightParams itself applies - this is the port contract (did
        # flightParams wire the station's (la, ev) into KMTraj correctly and
        # scale the result to the station's real distance right), not a
        # re-test of the integrator's own correctness (that's the golden-
        # vector parity block above).
        cases = [
            {
                # q = 1 - clamp((5-2)/(21-2)) = 16/19 = 0.842105...; onTop
                # (swing > pitch, all digits) -> topped end. q sits between
                # the HR band's q=0.5 and q=1 mock stations, t=(q-0.5)/0.5 =
                # 13/19 -> la = 27 + 1*t = 27.684211, ev = 108 + 4*t =
                # 110.736842, dist(topped) = 405 + 30*t = 425.526316.
                "label": "HR pulled RHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "R", "diff": 5},
                "want": {"la": 27.684211, "ev": 110.736842, "angle": 5.00, "dist": 425.526316},
            },
            {
                # Same q/onTop/la/ev/dist as above (none depend on hand) -
                # only angle (HZ mirrors for LHH) differs.
                "label": "HR same numbers LHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "L", "diff": 5},
                "want": {"la": 27.684211, "ev": 110.736842, "angle": 85.00, "dist": 425.526316},
            },
            {
                # q clamps to 0 (diff 481 is past the GO band's own hi=470) -
                # hits the q=0 mock station exactly, no interpolation. onTop
                # (swing 631 > pitch 150) -> la = laTopped = -30, ev =
                # evTopped = 40, dist = distTopped = 35.
                "label": "Groundout RHH",
                "play": {"result": "GO", "pitch": 150, "swing": 631, "batter_hand": "R", "diff": 481},
                "want": {"la": -30.0, "ev": 40.0, "angle": 53.00, "dist": 35.0},
            },
            {
                # diff 48 is below the FO band's own lo=55, so q clamps to 1 -
                # hits the q=1 mock station exactly. onTop (swing 268 > pitch
                # 220) -> la = laTopped = 28, ev = evTopped = 95,
                # dist = distTopped = 345.
                "label": "Flyout RHH",
                "play": {"result": "FO", "pitch": 220, "swing": 268, "batter_hand": "R", "diff": 48},
                "want": {"la": 28.0, "ev": 95.0, "angle": 29.00, "dist": 345.0},
            },
            {
                # q = 1 - clamp((87-20)/(150-20)) = 1 - 67/130 = 0.484615...;
                # swing (801) < pitch (888) on the full 1000-wheel -> "below"
                # the pitch (uppercut). q sits between the 1B band's q=0 and
                # q=0.5 mock stations, t=q/0.5 = 63/65 -> la = 25 + (18-25)*t
                # = 18.215385, ev = 80 + (90-80)*t = 89.692308,
                # dist(uppercut) = 55 + (155-55)*t = 151.923077.
                "label": "Single LHH",
                "play": {"result": "1B", "pitch": 888, "swing": 801, "batter_hand": "L", "diff": 87},
                "want": {"la": 18.215385, "ev": 89.692308, "angle": 21.00, "dist": 151.923077},
            },
        ]

        print("\nWorked examples:")
        for c in cases:
            print(f" {c['label']}:")
            hand = c["play"]["batter_hand"]
            archetype = tables["bands"][c["play"]["result"]]["archetype"]
            r = page.evaluate("(a) => KMFlight.flightParams(a.play, a.tables)", {"play": c["play"], "tables": tables})
            check(f"{c['label']}.la", r["la"], c["want"]["la"], tol=0.005)
            check(f"{c['label']}.ev (real paired station value, not solved)", r["ev"], c["want"]["ev"], tol=0.005)
            check(f"{c['label']}.angle", r["angle"], c["want"]["angle"], tol=0.005)
            check(f"{c['label']}.targetDist is THIS play's own real distance (not a shared station value)", r["targetDist"], c["want"]["dist"], tol=0.005)

            oracle = page.evaluate(
                """(a) => {
                    var raw = KMTraj.simulateFlight(a.ev, a.la, a.angle - 45, a.hand);
                    var sim = KMFlight.clampFairTerritory(raw, a.archetype);
                    var isHomeRun = a.result === 'HR';
                    var D = KMFlight.distanceCap({distance: a.target, landing: sim.landing}, a.angle, isHomeRun);
                    var scale = D / sim.distance;
                    return {
                        distance: D, x: sim.landing.x * scale, y: sim.landing.y * scale,
                        hangMs: 1000 * sim.hangS, apexFt: sim.apexFt,
                    };
                }""",
                {
                    "ev": r["ev"], "la": r["la"], "angle": r["angle"], "hand": hand,
                    "result": c["play"]["result"], "archetype": archetype, "target": c["want"]["dist"],
                },
            )
            check(f"{c['label']}.distance matches independent radial-scale-to-real-distance oracle", r["distance"], oracle["distance"], tol=0.01)
            check(f"{c['label']}.distance == this play's real distance (no fence clamp in play here)", r["distance"], c["want"]["dist"], tol=0.01)
            check(f"{c['label']}.x matches oracle", r["x"], oracle["x"], tol=0.01)
            check(f"{c['label']}.y matches oracle", r["y"], oracle["y"], tol=0.01)
            check(f"{c['label']}.hangMs matches oracle", r["hangMs"], oracle["hangMs"], tol=0.01)
            check(f"{c['label']}.apexFt matches oracle", r["apexFt"], oracle["apexFt"], tol=0.01)
            check(f"{c['label']}.hangMs is a real number (F10: no more null-for-grounders)", r["hangMs"] is not None, True)

        print("\nstationsLookup checks (boundary + midpoint interpolation, called directly):")
        hr_stations = tables["bands"]["HR"]["stations"]
        s_lo = page.evaluate("(a) => KMFlight.stationsLookup({stations: a}, -0.3)", hr_stations)
        check("q below first station clamps to first station's distTopped", s_lo["distTopped"], 345.0, tol=0.005)
        check("q below first station clamps to first station's distUppercut", s_lo["distUppercut"], 355.0, tol=0.005)
        check("q below first station clamps to first station's laTopped", s_lo["laTopped"], 25.0, tol=0.005)
        check("q below first station clamps to first station's evTopped", s_lo["evTopped"], 100.0, tol=0.005)
        s_hi = page.evaluate("(a) => KMFlight.stationsLookup({stations: a}, 1.4)", hr_stations)
        check("q above last station clamps to last station's distTopped", s_hi["distTopped"], 435.0, tol=0.005)
        check("q above last station clamps to last station's distUppercut", s_hi["distUppercut"], 445.0, tol=0.005)
        check("q above last station clamps to last station's evUppercut", s_hi["evUppercut"], 107.0, tol=0.005)
        s_exact = page.evaluate("(a) => KMFlight.stationsLookup({stations: a}, 0.5)", hr_stations)
        check("q exactly on a station hits it exactly (distTopped)", s_exact["distTopped"], 405.0, tol=0.005)
        check("q exactly on a station hits it exactly (distUppercut)", s_exact["distUppercut"], 415.0, tol=0.005)
        check("q exactly on a station hits it exactly (laUppercut)", s_exact["laUppercut"], 30.0, tol=0.005)
        check("q exactly on a station hits it exactly (evUppercut)", s_exact["evUppercut"], 103.0, tol=0.005)
        s_mid = page.evaluate("(a) => KMFlight.stationsLookup({stations: a}, 0.25)", hr_stations)
        # Midpoint of the q=0/q=0.5 mock stations: distTopped = 345 + (405-345)*0.5 = 375.
        check("q midway between two stations interpolates linearly (distTopped)", s_mid["distTopped"], 375.0, tol=0.005)
        check("q midway between two stations interpolates linearly (distUppercut)", s_mid["distUppercut"], 385.0, tol=0.005)
        check("q midway between two stations interpolates linearly (laTopped)", s_mid["laTopped"], 26.0, tol=0.005)
        check("q midway between two stations interpolates linearly (evTopped)", s_mid["evTopped"], 104.0, tol=0.005)
        check("q midway between two stations interpolates linearly (evUppercut)", s_mid["evUppercut"], 99.0, tol=0.005)
        s_none = page.evaluate("() => KMFlight.stationsLookup({stations: []}, 0.5)")
        check("empty stations array falls back to null (launchAngleFor path)", s_none, None)

        print("\nq-clamp checks (HR band, q clamps to a mock station exactly):")
        r_below = page.evaluate(
            "(t) => KMFlight.flightParams({result:'HR', pitch:500, swing:501, batter_hand:'R', diff:-5}, t)", tables
        )
        r_above = page.evaluate(
            "(t) => KMFlight.flightParams({result:'HR', pitch:500, swing:501, batter_hand:'R', diff:9999}, t)", tables
        )
        check("diff below band -> q=1 -> hits last station's la exactly", r_below["la"], 28.0, tol=0.005)
        check("diff above band -> q=0 -> hits first station's la exactly", r_above["la"], 25.0, tol=0.005)

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

        print("\nField-geometry refinement: every flightParams landing stays inside")
        print("the drawn field boundary (distanceCap/boundaryRFt) - a caught foul")
        print("fly/pop is exempt from clampFairTerritory (real sidespin drift, a")
        print("deliberate artifact) and can land well past the foul line, but it")
        print("must never plot into the void beyond the grass/warning-track shape:")
        boundary_result = page.evaluate(
            """(t) => {
                var worst = -1, worstInfo = null, checked = 0;
                // HR is deliberately exempt (distanceCap/clampToFence both skip
                // it - a home run is supposed to clear the drawn fence/boundary,
                // that's the entire point of the result).
                var codes = Object.keys(t.bands).filter(function (r) { return r !== 'HR'; });
                for (var p = 1; p <= 1000; p += 7) {
                  for (var s = 1; s <= 1000; s += 11) {
                    var diff = Math.min(Math.abs(p - s), 1000 - Math.abs(p - s));
                    codes.forEach(function (code) {
                      var fp = KMFlight.flightParams({result: code, pitch: p, swing: s, batter_hand: 'R', diff: diff}, t);
                      if (!fp) return;
                      checked++;
                      var offset = Math.atan2(fp.x, fp.y) * 180 / Math.PI;
                      var maxR = KMFlight.boundaryRFt(offset);
                      var d = Math.hypot(fp.x, fp.y);
                      var over = d - maxR;
                      if (over > worst) { worst = over; worstInfo = {code: code, p: p, s: s, d: d, maxR: maxR, offset: offset}; }
                    });
                  }
                }
                return {worst: worst, worstInfo: worstInfo, checked: checked};
            }""",
            tables,
        )
        print(f"  checked {boundary_result['checked']} (pitch,swing,result) combos, worst overshoot {boundary_result['worst']:.3f}ft")
        if boundary_result["worst"] > 0.05:
            print(f"  worst case: {boundary_result['worstInfo']}")
        check("no flightParams landing falls outside the field boundary", boundary_result["worst"] <= 0.05, True)

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
                    return KMFlight.baseLegs(KMFlight.outThrowTargets(a.m, moves, a.flight));
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"outThrowTargets({result} {before}->{after})", got, want)

        print("\nThrowOrder grammar - bases, positions, mixed cutoff chains, invalid input (Task 8.1):")
        grammar = page.evaluate(
            """() => {
                return {
                  plainBase: KMFlight.parseThrowOrder('h'),
                  allBases: KMFlight.parseThrowOrder('h,f,s,t'),
                  positionOnly: KMFlight.parseThrowOrder('6'),
                  mixedCutoff: KMFlight.parseThrowOrder('6h'),
                  separators: KMFlight.parseThrowOrder('6-h'),
                  caseInsensitive: KMFlight.parseThrowOrder('H'),
                  strayChar: KMFlight.parseThrowOrder('6z'),
                  nullInput: KMFlight.parseThrowOrder(null),
                  emptyInput: KMFlight.parseThrowOrder(''),
                };
            }"""
        )
        check("a single base letter parses to one base leg",
              grammar["plainBase"], [{"kind": "base", "base": "HOME"}])
        check("all four base letters parse in order",
              grammar["allBases"],
              [{"kind": "base", "base": "HOME"}, {"kind": "base", "base": "1B"},
               {"kind": "base", "base": "2B"}, {"kind": "base", "base": "3B"}])
        check("a bare position number parses to one position leg (decorative-only, warns)",
              grammar["positionOnly"], [{"kind": "pos", "pos": "SS"}])
        check("a mixed cutoff chain (position then base) parses both legs in order",
              grammar["mixedCutoff"], [{"kind": "pos", "pos": "SS"}, {"kind": "base", "base": "HOME"}])
        check("a dash separator parses identically to no separator",
              grammar["separators"], grammar["mixedCutoff"])
        check("base letters are case-insensitive", grammar["caseInsensitive"], [{"kind": "base", "base": "HOME"}])
        check("a stray invalid character rejects the whole value (falls back to the heuristic)",
              grammar["strayChar"], None)
        check("null input parses to null", grammar["nullInput"], None)
        check("empty string input parses to null", grammar["emptyInput"], None)

        print("\nCutoff spot geometry - a constant fraction of the throw line (Task 8.3):")
        cutoff = page.evaluate(
            """() => {
                var origin = {x: 0, y: 100};
                var base = {x: 0, y: 0};
                var spot = KMFlight.cutoffSpotFt(origin, base);
                return { spot: spot, frac: KMFlight.CUTOFF_POSITION_FRAC, nullOrigin: KMFlight.cutoffSpotFt(null, base) };
            }"""
        )
        check("the cutoff spot sits at CUTOFF_POSITION_FRAC of the way from origin to base",
              cutoff["spot"]["y"], 100 * (1 - cutoff["frac"]))
        check("CUTOFF_POSITION_FRAC defaults to 50% (Alex's ask)", cutoff["frac"], 0.5)
        check("a missing origin/base returns null rather than a garbage point", cutoff["nullOrigin"], None)

        print("\nA cutoff-assisted explicit ThrowOrder threads a position leg through the full schedule (Task 8.2):")
        cutoff_chain = page.evaluate(
            """() => {
                // CF fields a double, throws through SS (cutoff) to 3B.
                var m = {result: '3B', outs_before: 0, outs_after: 0, diff: 250, throw_order: '6t'};
                var moves = KMFlight.deriveRunnerMoves('000', '000', 0);
                var flight = {
                  archetype: 'double', fielder: 'CF', clearedFence: false,
                  x: 0, y: 300, distance: 300, fieldedDistFt: 300, groundTimeS: 0.4,
                  contactVel: { vx: 0, vy: 90, vz: -5 },
                };
                var targets = KMFlight.outThrowTargets(m, moves, flight);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                return {
                  legKinds: targets.map(function (l) { return l.kind; }),
                  scheduleLen: schedule.length,
                  leg0Base: schedule[0].base, leg0Pos: schedule[0].pos,
                  leg1Base: schedule[1] && schedule[1].base,
                  leg0Out: schedule[0].out, leg1Out: schedule[1] && schedule[1].out,
                  leg1ThrowerPos: schedule[1] && schedule[1].throwerPos,
                };
            }"""
        )
        check("the chain has one position leg then one base leg",
              cutoff_chain["legKinds"], ["pos", "base"])
        check("the schedule has two legs", cutoff_chain["scheduleLen"], 2)
        check("leg 0 targets the cutoff spot (no base)", cutoff_chain["leg0Base"], None)
        check("leg 0's own position is the named cutoff man (SS)", cutoff_chain["leg0Pos"], "SS")
        check("leg 1 targets the real base (3B)", cutoff_chain["leg1Base"], "3B")
        check("a position/cutoff leg is never an out", cutoff_chain["leg0Out"], False)
        check("leg 1 is thrown by the cutoff man (SS), not CF directly",
              cutoff_chain["leg1ThrowerPos"], "SS")

        print("\nstealThrowTarget (B3-c - catcher throw on steal attempts):")
        steal_cases = [
            ("SB2", "001", "010", {"base": "2B", "caught": False, "delay": False, "from": "1B"}),
            ("SB3", "010", "100", {"base": "3B", "caught": False, "delay": False, "from": "2B"}),
            ("CS2", "001", "000", {"base": "2B", "caught": True, "delay": False, "from": "1B"}),
            ("CS", "010", "000", {"base": "3B", "caught": True, "delay": False, "from": "2B"}),
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
                    var lastLeg = schedule[schedule.length - 1];
                    var runnerArrival = KMFlight.forcedOutRunnerArrivalMs(a.m, a.flight, moves, lastLeg.base);
                    if (runnerArrival == null) return null;
                    return (runnerArrival - lastLeg.endMs) - KMFlight.MARGIN_POLICY.forceOut.minMs;
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            if margin is None:
                # FC family: the actual forced-out runner (2B/3B/HOME) is
                # someone deriveRunnerMoves has no move object for at all -
                # the batter reached first SAFELY on these, so there is no
                # honest per-player arrival to reconcile against from this
                # synthetic moves-only test fixture (plan's own flagged gap -
                # FORCED_OUT_BASE's redirect targets a runner the data model
                # doesn't track). Nothing to assert; not a regression.
                print(f"  [skip] throw beats runner ({result} {before}->{after}): forced runner untracked (FC family)")
                continue
            check(f"throw beats runner by >=forceOut.minMs ({result} {before}->{after})", margin >= -0.5, True)

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
                        runnerArrival = Math.max(runnerArrival, catchMs + KMFlight.TAG_UP_MS + KMFlight.runnerLegMs(a.m, mv.from, legs));
                    });
                    var lastEnd = Math.max.apply(null, schedule.map(t => t.endMs));
                    // Round 3 Task 3: hustleRunner (delta>0/"land later" pool)
                    // moves the runner's OWN rendered arrival earlier by its
                    // ms - the true invariant is against that render-layer-
                    // adjusted arrival, not the raw pre-hustle one.
                    var hustleMs = 0;
                    (schedule.adjustments || []).forEach(function (adj) {
                        if (adj.knob === "hustleRunner") hustleMs += adj.ms;
                    });
                    var adjustedArrival = runnerArrival + hustleMs; // hustleMs is recorded negative
                    return (lastEnd - adjustedArrival) - KMFlight.MARGIN_POLICY.uncontested.minMs;
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"runner beats throw by >=uncontested.minMs ({result} {before}->{after})", margin >= -0.5, True)

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
                        // Task 1b/b0534c3 (fact 0.2): an explicit ThrowOrder
                        // on a hit with ZERO real outs is legitimate -
                        // decorative/contestedSafe legs beyond the outs
                        // actually recorded (e.g. an OF single throwing to
                        // 2B just to hold the batter) are expected now, so
                        // the invariant is scoped to the schedule's own
                        // OUT-marked legs specifically (throwSchedule's
                        // per-base-leg-ordinal out-flag, Task 8.2), not the
                        // raw target count.
                        var recorded = Math.max(0, (m.outs_after || 0) - (m.outs_before || 0));
                        var schedule = KMFlight.throwSchedule(m, moves, flight);
                        var outLegs = schedule.filter(function (t) { return t.out; }).map(function (t) { return t.base; });
                        if (outLegs.length > recorded) bad.push(m.moment_id + ": too many OUT-marked legs");
                        var seen = {};
                        outLegs.forEach(function (b) {
                            if (seen[b]) bad.push(m.moment_id + ": dup base among OUT-marked legs " + b);
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

        print("\nField-geometry refinement, real bands (Alex's field-geometry refinement")
        print("request + the foul-catch-containment follow-up): dense pitch/swing sweep")
        print("against the REAL result_diff_bands.csv-derived bands (meta.json), whose")
        print("PO/FO/DFO/SacF/DSacF ranges are the ones that actually stress sidespin")
        print("drift - the mock `tables` sweep above uses a smaller illustrative band")
        print("set, this is the real-data version of the same check:")
        if meta_fp.exists() and real_plays:
            real_boundary = page.evaluate(
                """(t) => {
                    var worst = -1, worstInfo = null, checked = 0;
                    var codes = Object.keys(t.bands).filter(function (r) { return r !== 'HR'; });
                    for (var p = 1; p <= 1000; p += 5) {
                      for (var s = 1; s <= 1000; s += 9) {
                        var diff = Math.min(Math.abs(p - s), 1000 - Math.abs(p - s));
                        codes.forEach(function (code) {
                          var fp = KMFlight.flightParams({result: code, pitch: p, swing: s, batter_hand: 'R', diff: diff}, t);
                          if (!fp) return;
                          checked++;
                          var offset = Math.atan2(fp.x, fp.y) * 180 / Math.PI;
                          var maxR = KMFlight.boundaryRFt(offset);
                          var over = Math.hypot(fp.x, fp.y) - maxR;
                          if (over > worst) { worst = over; worstInfo = {code: code, p: p, s: s, offset: offset, maxR: maxR}; }
                        });
                      }
                    }
                    return { worst: worst, worstInfo: worstInfo, checked: checked };
                }""",
                real_tables,
            )
            print(f"  checked {real_boundary['checked']} combos against real bands, worst overshoot {real_boundary['worst']:.3f}ft")
            if real_boundary["worst"] > 0.05:
                print(f"  worst case: {real_boundary['worstInfo']}")
            check("no real-band flightParams landing falls outside the field boundary", real_boundary["worst"] <= 0.05, True)
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

        print("\nnearestFielder retreat-bias checks (Alex's report: catching/picking up a")
        print("ball is asymmetric - coming IN toward home is easier than retreating away")
        print("from it - anchored to explicit ground-coverage-share anecdotes, not an")
        print("arbitrary tuned constant):")
        check("CATCH_RETREAT_PENALTY == 70/30 RF/2B ground share", page.evaluate("KMFlight.CATCH_RETREAT_PENALTY"), 7 / 3, tol=0.001)
        check("PICKUP_RETREAT_PENALTY == 80/20 RF/2B ground share", page.evaluate("KMFlight.PICKUP_RETREAT_PENALTY"), 4.0, tol=0.001)

        fielder_bias = page.evaluate(
            """() => {
                var anchors = KMFlight.FIELDER_ANCHORS_FT;
                var b2 = anchors['2B'], rf = anchors['RF'];
                function along(t) {
                    return { x: b2.x + t * (rf.x - b2.x), y: b2.y + t * (rf.y - b2.y) };
                }
                // t=0.25 sits between the catch boundary (t=0.3, where
                // 0.25*2.333 == 0.75) and the pickup boundary (t=0.2, where
                // 0.25*4 > 0.75 already) - raw distance still favors 2B, the
                // catch penalty isn't quite strong enough to flip it, but the
                // pickup penalty is. A single point that discriminates all
                // three modes at once.
                var mid = along(0.25);
                var near2B = along(0.05), nearRF = along(0.95);
                return {
                    rawD2B: Math.hypot(b2.x - mid.x, b2.y - mid.y),
                    rawDRF: Math.hypot(rf.x - mid.x, rf.y - mid.y),
                    noPenalty: KMFlight.nearestFielder(mid.x, mid.y, 1),
                    catchPenalty: KMFlight.nearestFielder(mid.x, mid.y, KMFlight.CATCH_RETREAT_PENALTY),
                    pickupPenalty: KMFlight.nearestFielder(mid.x, mid.y, KMFlight.PICKUP_RETREAT_PENALTY),
                    obviously2B: KMFlight.nearestFielder(near2B.x, near2B.y, KMFlight.PICKUP_RETREAT_PENALTY),
                    obviouslyRF: KMFlight.nearestFielder(nearRF.x, nearRF.y, KMFlight.CATCH_RETREAT_PENALTY),
                };
            }"""
        )
        check("sanity: raw distance to 2B/RF really is closer to 2B at t=0.25", fielder_bias["rawD2B"] < fielder_bias["rawDRF"], True)
        check("unbiased (penalty=1) comparison picks 2B (raw-nearest)", fielder_bias["noPenalty"], "2B")
        check("catch penalty (2.33x) not quite enough to flip it - still 2B", fielder_bias["catchPenalty"], "2B")
        check("pickup penalty (4x) flips it to RF - chasing a rolling ball favors the deeper fielder more", fielder_bias["pickupPenalty"], "RF")
        check("a point clearly in 2B's own territory still resolves to 2B despite the bias", fielder_bias["obviously2B"], "2B")
        check("a point clearly in RF's own territory still resolves to RF despite the bias", fielder_bias["obviouslyRF"], "RF")

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
                      // Pitcher-EV-threshold (Alex's ask): above the cutoff, a
                      // dead-middle (45deg) grounder is no longer the HZ
                      // answer's own "P" - it's divvied to 2B (RHH)/SS (LHH)
                      // instead, a deliberate exception to this invariant.
                      var expectedPos = hzPos;
                      if (hzPos === "P" && ev > KMFlight.PITCHER_MIDDLE_EV_MAX_MPH) {
                        expectedPos = hand === "L" ? "SS" : "2B";
                      }
                      if (flight.fielder !== expectedPos) bad.push("angle=" + angle + " hand=" + hand + " ev=" + ev + ": fielder " + flight.fielder + " != expected " + expectedPos);
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

        print("\nPitcher-EV-threshold candidate exclusion (real bug report: a 99mph, -6deg")
        print("comebacker with a short 17ft first-bounce distance still resolved to P -")
        print("reassigning the nominal HZ answer away from P isn't enough on its own if P")
        print("is still in the charge-in race and just closer to a short, shallow ball on")
        print("pure geometry; this needs P pulled out of the candidate pool entirely):")
        hot_comebacker = page.evaluate(
            """(a) => {
                var out = {};
                ["R", "L"].forEach(function (hand) {
                  var sim = KMTraj.simulateFlight(99.24, -6, 0, hand);
                  var flight = {
                    la: -6, ev: 99.24, distance: sim.distance, angle: 45,
                    x: sim.landing.x, y: sim.landing.y, contactVel: sim.contactVel,
                    archetype: "grounder",
                  };
                  var m = {outs_before: 0, outs_after: 1};
                  KMFlight.resolveGrounderInterception(m, flight, hand);
                  out[hand] = {fielder: flight.fielder, distance: sim.distance};
                });
                return out;
            }"""
        )
        print(f"  distance={hot_comebacker['R']['distance']:.1f}ft")
        check("hot comebacker (RHH, EV>threshold): fielder is 2B, not P", hot_comebacker["R"]["fielder"], "2B")
        check("hot comebacker (LHH, EV>threshold): fielder is SS, not P", hot_comebacker["L"]["fielder"], "SS")

        print("\nOutfield shade direction (real bug report: a Calvin Huff double")
        print("landed at flight.angle exactly 45 - a lattice tie - even though the real")
        print("simulated x/y clearly wasn't a tie, so the tie-break picked the wrong")
        print("shift direction; must use the true simulated bearing, not the lattice).")
        print("Task 5b folded the old flat shift into ofDerivedShadeAnchorFt's own")
        print("fallback - this now tests ofShadeDirection directly, the piece that")
        print("carries the original bug fix forward:")
        shift_case = page.evaluate(
            """() => {
                var flight = {archetype: "double", angle: 45, fielder: "CF", x: 19.92, y: 243.34};
                return { direction: KMFlight.ofShadeDirection(flight) };
            }"""
        )
        check("CF shades away (negative direction) from a ball landing right of center",
              shift_case["direction"], -1)

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

        # ── Stage D: perspective projection (physics-redesign plan Part 6/11) ──

        print("\nprojectFt pure-function checks:")
        home_px = page.evaluate("KMFlight.projectFt(0, 0, 0)")
        check("home plate projects to screen-x center", home_px["x"], 230.0, tol=0.5)
        home_field_h = page.evaluate("KMFlight.FIELD_H")
        check("home plate sits in the bottom fifth of the viewBox", home_px["y"] > home_field_h * 0.8, True)

        straightness = page.evaluate(
            """() => {
                // Foul-line samples (any fixed HZ angle) should be collinear in
                // screen space - straight world lines project to straight lines.
                var pts = [];
                for (var d = 20; d <= 375; d += 20) {
                    var ft = KMFlight.landingPoint(d, 90);
                    pts.push(KMFlight.ftToSvg(ft.x, ft.y));
                }
                var a = pts[0], b = pts[pts.length - 1];
                var maxDev = 0;
                pts.forEach(function (p) {
                    // perpendicular distance from p to line a-b
                    var num = Math.abs((b.y - a.y) * p.x - (b.x - a.x) * p.y + b.x * a.y - b.y * a.x);
                    var den = Math.hypot(b.y - a.y, b.x - a.x) || 1;
                    maxDev = Math.max(maxDev, num / den);
                });
                return maxDev;
            }"""
        )
        check("foul-line samples are collinear on screen (straight lines stay straight)", straightness < 0.5, True)

        depth_order = page.evaluate(
            """() => {
                var near = KMFlight.ftToSvg(0, 50);
                var far = KMFlight.ftToSvg(0, 300);
                return { near: near.y, far: far.y };
            }"""
        )
        check("a farther ground point projects higher on screen (smaller y)", depth_order["far"] < depth_order["near"], True)

        symmetry = page.evaluate(
            """() => {
                var left = KMFlight.ftToSvg(-100, 200);
                var right = KMFlight.ftToSvg(100, 200);
                var center = KMFlight.ftToSvg(0, 200);
                return {
                    yMatch: Math.abs(left.y - right.y) < 1e-6,
                    xMirror: Math.abs((center.x - left.x) - (right.x - center.x)) < 1e-6,
                };
            }"""
        )
        check("left/right symmetry: same y", symmetry["yMatch"], True)
        check("left/right symmetry: mirrored x around center", symmetry["xMirror"], True)

        fence_bbox = page.evaluate(
            """() => {
                var d = KMFlight.fencePathD();
                var nums = d.match(/-?[0-9.]+/g).map(Number);
                var xs = [], ys = [];
                for (var i = 0; i < nums.length; i += 2) { xs.push(nums[i]); ys.push(nums[i + 1]); }
                return { minX: Math.min.apply(null, xs), maxX: Math.max.apply(null, xs),
                         minY: Math.min.apply(null, ys), maxY: Math.max.apply(null, ys) };
            }"""
        )
        print(f"  fence bbox: {fence_bbox}")
        check("fence arc stays within the viewBox width (with margin)", 0 <= fence_bbox["minX"] and fence_bbox["maxX"] <= 460, True)

        print("\nScorecard fielding notation (KMFlight.fieldingNotation) - representative")
        print("shapes worked out with Alex directly, plus the sac-fly decorative-throw")
        print("exclusion regression (a routine SacF must read F9, never F9-2):")
        # (m, flight) pairs are the minimal fields fieldingNotation/outThrowTargets/
        # resolveRunnerMoves actually read - throw_order is supplied explicitly on
        # most cases so each one tests coveringPosition's rules directly rather than
        # also depending on deriveRunnerMoves' own heuristics.
        notation_cases = [
            ("plain GO, SS fields, throws to 1B", "6-3",
             {"result": "GO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "SS", "archetype": "grounder", "angle": 29, "clearedFence": False}),
            ("unassisted 1B (fields and steps on the bag himself, 85deg)", "3U",
             {"result": "GO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "1B", "archetype": "grounder", "angle": 85, "clearedFence": False}),
            ("PFP: 1B fields at 77deg, pitcher covers first", "3-1",
             {"result": "GO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "1B", "archetype": "grounder", "angle": 77, "clearedFence": False}),
            ("bunt fielded by 1B, 2B covers first", "3-4",
             {"result": "BGO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "1B", "archetype": "bunt", "angle": 81, "clearedFence": False}),
            ("bunt fielded by 3B, force at third, SS covers", "5-6",
             {"result": "BFC", "outs_before": 0, "outs_after": 1, "obc_before": "110", "obc_after": "011", "runs": 0, "throw_order": "t"},
             {"fielder": "3B", "archetype": "bunt", "angle": 9, "clearedFence": False}),
            ("double play, SS fields (6-4-3)", "6-4-3",
             {"result": "DP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0, "throw_order": "s,f"},
             {"fielder": "SS", "archetype": "grounder", "angle": 29, "clearedFence": False}),
            ("double play, 2B fields (4-6-3)", "4-6-3",
             {"result": "DP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0, "throw_order": "s,f"},
             {"fielder": "2B", "archetype": "grounder", "angle": 61, "clearedFence": False}),
            ("comebacker to the pitcher at 45deg, SS covers second (tie-break)", "1-6",
             {"result": "FC", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "010", "runs": 0, "throw_order": "s"},
             {"fielder": "P", "archetype": "grounder", "angle": 45, "clearedFence": False}),
            ("fielder's choice, SS forces the lead runner at 2nd", "6-4",
             {"result": "FC", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "010", "runs": 0, "throw_order": "s"},
             {"fielder": "SS", "archetype": "grounder", "angle": 29, "clearedFence": False}),
            ("LODP, SS lines out and doubles off 1B - real 2-chain", "6-3",
             {"result": "LODP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0,
              "runner_moves": [{"from": "1B", "to": "OUT", "scored": False}]},
             {"fielder": "SS", "archetype": "line_drive", "angle": 29, "clearedFence": False}),
            ("LODP, 1B lines out and doubles off 1B himself - unassisted", "L3",
             {"result": "LODP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0,
              "runner_moves": [{"from": "1B", "to": "OUT", "scored": False}]},
             {"fielder": "1B", "archetype": "line_drive", "angle": 81, "clearedFence": False}),
            ("double play at home, 3B fields, force at home then relay to 1st", "5-2-3",
             {"result": "DPH1", "outs_before": 1, "outs_after": 3, "obc_before": "111", "obc_after": "000", "runs": 0, "throw_order": "h,f"},
             {"fielder": "3B", "archetype": "grounder", "angle": 9, "clearedFence": False}),
            ("routine flyout, no throw", "F8",
             {"result": "FO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "CF", "archetype": "fly_ball", "angle": 45, "clearedFence": False}),
            ("routine popout, no throw", "P4",
             {"result": "PO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "2B", "archetype": "pop_up", "angle": 61, "clearedFence": False}),
            ("sac fly: decorative tag-up throw must NOT read as a putout at home, and gets the S prefix", "SF9",
             {"result": "SacF", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "000", "runs": 1, "throw_order": "h"},
             {"fielder": "RF", "archetype": "fly_ball", "angle": 69, "clearedFence": False}),
            ("double sac fly (DSacF) also gets the S prefix", "SF7",
             {"result": "DSacF", "outs_before": 0, "outs_after": 1, "obc_before": "001", "obc_after": "000", "runs": 1, "throw_order": "h"},
             {"fielder": "LF", "archetype": "fly_ball", "angle": 29, "clearedFence": False}),
            ("strikeout has no batted ball - no notation", None,
             {"result": "K", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             None),
            ("clean single - no putout, no notation", None,
             {"result": "1B", "outs_before": 0, "outs_after": 0, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "CF", "archetype": "single", "angle": 45, "clearedFence": False}),
        ]
        for label, want, m, flight in notation_cases:
            got = page.evaluate("(a) => KMFlight.fieldingNotation(a.m, a.flight)", {"m": m, "flight": flight})
            check(label, got, want)

        print("\n2B coverage keyed off the thrower for OF throws (Task 2b, fact 22):")
        cov2b = page.evaluate(
            """() => {
                return {
                  lf: KMFlight.coveringPosition('2B', 'single', 20, 'LF', 1, null, null),
                  cf: KMFlight.coveringPosition('2B', 'single', 20, 'CF', 1, null, null),
                  rf: KMFlight.coveringPosition('2B', 'single', 20, 'RF', 1, null, null),
                  nonOfLowAngle: KMFlight.coveringPosition('2B', 'grounder', 20, 'SS', 1, null, null),
                  nonOfHighAngle: KMFlight.coveringPosition('2B', 'grounder', 60, '3B', 1, null, null),
                };
            }"""
        )
        check("LF throwing to 2B: the second baseman covers", cov2b["lf"], "2B")
        check("CF throwing to 2B: the shortstop covers", cov2b["cf"], "SS")
        check("RF throwing to 2B: the shortstop covers", cov2b["rf"], "SS")
        check("non-OF thrower keeps the angle rule (angle<45 -> 2B)", cov2b["nonOfLowAngle"], "2B")
        check("non-OF thrower keeps the angle rule (angle>=45 -> SS)", cov2b["nonOfHighAngle"], "SS")

        print("\nPFP coverage - real race, not a lattice angle (Task 5a):")
        print("measured against KMTraj-simulated grounders assigned to 1B (angle 77 bucket):")
        pfp = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '000', obc_after: '000', runs: 0, diff: 250};
                function resolved(ev, la) {
                  var sim = KMTraj.simulateFlight(ev, la, 32, 'R');
                  var flight = {
                    ev: ev, la: la, angle: 77, distance: sim.distance,
                    x: sim.landing.x, y: sim.landing.y,
                    contactVel: sim.contactVel, archetype: 'grounder', clearedFence: false,
                  };
                  KMFlight.resolveGrounderInterception(m, flight, 'R');
                  return flight;
                }
                var routine = resolved(80, 0);
                var hardRoller = resolved(110, 10);
                return {
                  routineFielder: routine.fielder,
                  routineCoverage: KMFlight.firstBaseCoverage(m, routine),
                  routineNotation: KMFlight.fieldingNotation(m, routine),
                  hardFielder: hardRoller.fielder,
                  hardCoverage: KMFlight.firstBaseCoverage(m, hardRoller),
                  hardNotation: KMFlight.fieldingNotation(m, hardRoller),
                };
            }"""
        )
        check("routine 77deg grounder is still fielded by 1B", pfp["routineFielder"], "1B")
        check("routine 77deg grounder: 1B covers himself (honest return easily beats the batter)",
              pfp["routineCoverage"], "1B")
        check("routine 77deg grounder reads unassisted, not 3-1", pfp["routineNotation"], "3U")
        check("hard-hit 77deg roller is still fielded by 1B", pfp["hardFielder"], "1B")
        check("hard-hit 77deg roller: pitcher covers (1B honestly can't get back)", pfp["hardCoverage"], "P")
        check("hard-hit 77deg roller reads 3-1", pfp["hardNotation"], "3-1")

        print("\nRound 3 Task 4/§13.3: the general per-leg receiver floor subsumes the")
        print("deleted pitcher-cover-1B special case - the 3-1 PFP throw must still not")
        print("visibly beat the pitcher's own token to the bag:")
        pfp31 = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '000', obc_after: '000', runs: 0, diff: 250};
                var sim = KMTraj.simulateFlight(110, 10, 32, 'R');
                var flight = {
                  ev: 110, la: 10, angle: 77, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, archetype: 'grounder', clearedFence: false,
                };
                KMFlight.resolveGrounderInterception(m, flight, 'R');
                var moves = KMFlight.deriveRunnerMoves('000', '000', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                return {
                  fielder: flight.fielder,
                  coverage: KMFlight.firstBaseCoverage(m, flight),
                  scheduleEndMs: schedule[schedule.length - 1].endMs,
                  pitcherArrivalMs: KMFlight.pitcherCover1BArrivalMs(m),
                  adjustments: schedule.adjustments.map(function (a) { return a.knob; }),
                };
            }"""
        )
        check("3-1 fixture: 1B still fields it", pfp31["fielder"], "1B")
        check("3-1 fixture: pitcher still covers", pfp31["coverage"], "P")
        check("general floor: throw's final endMs >= pitcher's own arrival at 1B (never beats the covering token)",
              pfp31["scheduleEndMs"] >= pfp31["pitcherArrivalMs"] - 0.5, True)
        print(f"  scheduleEndMs={pfp31['scheduleEndMs']:.1f} pitcherArrivalMs={pfp31['pitcherArrivalMs']:.1f} "
              f"adjustments={pfp31['adjustments']}")

        print("\nRound 3 Task 4: every leg's own ball must not beat its receiver's token")
        print("(6-4-3 DP relay - SS fields, throws to 2B, relay to 1B):")
        dp643 = page.evaluate(
            """() => {
                var m = {result: 'DP', outs_before: 0, outs_after: 2, obc_before: '001', obc_after: '000',
                         runs: 0, throw_order: 's,f', diff: 250};
                var sim = KMTraj.simulateFlight(85, -8, 20, 'R');
                var flight = {
                  ev: 85, la: -8, angle: 29, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, fielder: 'SS', archetype: 'grounder', clearedFence: false,
                };
                var moves = KMFlight.deriveRunnerMoves('001', '000', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                return {
                  legs: schedule.map(function (leg) {
                    var receiver = KMFlight.receiverForLeg(KMFlight.chainMoverPlan(m, flight, moves), leg);
                    return {
                      base: leg.base, pos: leg.pos, endMs: leg.endMs,
                      receiverArrivalMs: receiver ? receiver.arrivalMs : null,
                    };
                  }),
                };
            }"""
        )
        for leg in dp643["legs"]:
            if leg["receiverArrivalMs"] is None:
                continue
            ok = leg["endMs"] >= leg["receiverArrivalMs"] - 0.5
            check(f"6-4-3 leg to {leg['base'] or leg['pos']}: ball (endMs={leg['endMs']:.1f}) doesn't beat "
                  f"receiver (arrivalMs={leg['receiverArrivalMs']:.1f})", ok, True)

        print("\nRound 3 Task 1 (§13.5): ballPassesDepthMs - real geometry, not a flat delay:")
        depth_sim = page.evaluate("KMTraj.simulateFlight(95, 20, 20, 'R')")
        depth_ft = 147.0
        hand_t_ms = None
        samples = depth_sim["samples"]
        for i, s in enumerate(samples):
            r = math.hypot(s["x"], s["y"])
            if r >= depth_ft:
                if i == 0:
                    hand_t_ms = s["t"] * 1000
                else:
                    prev = samples[i - 1]
                    prev_r = math.hypot(prev["x"], prev["y"])
                    frac = 0 if r == prev_r else (depth_ft - prev_r) / (r - prev_r)
                    hand_t_ms = (prev["t"] + (s["t"] - prev["t"]) * frac) * 1000
                break
        got_gate_ms = page.evaluate(
            "(a) => KMFlight.ballPassesDepthMs({contactVel: a.sim.contactVel, samples: a.sim.samples, distance: a.sim.distance}, a.depth)",
            {"sim": depth_sim, "depth": depth_ft},
        )
        check("air-phase crossing matches a hand-interpolated sample crossing (147ft)", got_gate_ms, hand_t_ms, tol=0.5)

        print("\nRound 3 Task 1: covering-fielder startMs gate wired into chainMoverPlan -")
        print("OF single, SS covers 2B (gated on real ball-passes-depth geometry):")
        of_single_gate = page.evaluate(
            """() => {
                var sim = KMTraj.simulateFlight(95, 12, 20, 'R');
                var flight = {
                  ev: 95, la: 12, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, archetype: 'single', clearedFence: false,
                };
                KMFlight.resolveHitPickup(flight);
                var m = {result: '1B', outs_before: 0, outs_after: 0, obc_before: '000', obc_after: '100',
                         runs: 0, throw_order: 's', diff: 250};
                var moves = KMFlight.deriveRunnerMoves('000', '100', 0);
                var plan = KMFlight.chainMoverPlan(m, flight, moves);
                var ssEntry = plan && plan.filter(function (e) { return e.pos === 'SS'; })[0];
                var anchor = ssEntry ? KMFlight.fielderStartAnchorFt('SS', flight, m) : null;
                var handGate = anchor ? KMFlight.ballPassesDepthMs(flight, Math.hypot(anchor.x, anchor.y)) : null;
                return {
                  fielder: flight.fielder,
                  ssStartMs: ssEntry ? ssEntry.startMs : null,
                  handGate: handGate,
                  fieldedMs: KMFlight.fieldedMs(flight),
                };
            }"""
        )
        print(f"  fielder={of_single_gate['fielder']} ssStartMs={of_single_gate['ssStartMs']} "
              f"handGate={of_single_gate['handGate']} fieldedMs={of_single_gate['fieldedMs']}")
        if of_single_gate["ssStartMs"] is not None and of_single_gate["handGate"] is not None:
            expected = min(of_single_gate["handGate"], of_single_gate["fieldedMs"])
            check("SS's own startMs equals the depth-gate (capped at fieldedMs)", of_single_gate["ssStartMs"], expected, tol=0.5)
            check("the gate is a real positive delay, not start-at-contact (OF single, real depth to cross)",
                  of_single_gate["ssStartMs"] > 0, True)
        else:
            print("  [skip] SS not in this fixture's chain plan (throw_order/coverage resolved differently)")

        print("\nRound 3 Task 1: 6-4-3 DP - both middle infielders still start at 0")
        print("(the ball never passes their own depth on a routine grounder):")
        dp643_gate = page.evaluate(
            """() => {
                var m = {result: 'DP', outs_before: 0, outs_after: 2, obc_before: '001', obc_after: '000',
                         runs: 0, throw_order: 's,f', diff: 250};
                var sim = KMTraj.simulateFlight(85, -8, 20, 'R');
                var flight = {
                  ev: 85, la: -8, angle: 29, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, fielder: 'SS', archetype: 'grounder', clearedFence: false,
                };
                var moves = KMFlight.deriveRunnerMoves('001', '000', 0);
                var plan = KMFlight.chainMoverPlan(m, flight, moves);
                return plan.map(function (e) { return {pos: e.pos, startMs: e.startMs}; });
            }"""
        )
        for e in dp643_gate:
            check(f"6-4-3 entry {e['pos']}: startMs stays 0 (ball never passes middle-infield depth)", e["startMs"], 0, tol=0.5)

        print("\nAlex's report fix: a comebacker fielded well short of every coverer's own depth -")
        print("ground-archetype coverers now skip ballPassesDepthMs entirely and get a flat")
        print("INFIELD_COVER_BREAK_MS reaction beat instead (flight.fielder is already resolved,")
        print("so a covering infielder reads it off the bat immediately):")
        comebacker_gate = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '100', obc_after: '000',
                         runs: 0, throw_order: 's', diff: 250};
                var sim = KMTraj.simulateFlight(70, 0, 0, 'R');
                var flight = {
                  ev: 70, la: 0, angle: 45, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, archetype: 'grounder', clearedFence: false,
                };
                KMFlight.resolveGrounderInterception(m, flight, 'R');
                var moves = KMFlight.deriveRunnerMoves('100', '000', 0);
                var plan = KMFlight.chainMoverPlan(m, flight, moves);
                return {
                  fielder: flight.fielder,
                  entries: (plan || []).map(function (e) { return {pos: e.pos, startMs: e.startMs}; }),
                };
            }"""
        )
        print(f"  fielder={comebacker_gate['fielder']} entries={comebacker_gate['entries']}")
        infield_cover_break_ms = page.evaluate("KMFlight.INFIELD_COVER_BREAK_MS")
        for e in comebacker_gate["entries"]:
            if e["pos"] == comebacker_gate["fielder"]:
                continue
            check(f"comebacker coverer {e['pos']}: flat INFIELD_COVER_BREAK_MS reaction beat, not the old depth gate",
                  e["startMs"], infield_cover_break_ms, tol=0.5)

        print("\nRound 3 Task 5 (§13.6): unassisted (3U) entry gets real 3-leg timing -")
        print("glove and ball travel to the bag together:")
        unassisted3u = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '000', obc_after: '000', runs: 0, diff: 250};
                var sim = KMTraj.simulateFlight(80, 0, 32, 'R');
                var flight = {
                  ev: 80, la: 0, angle: 77, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, archetype: 'grounder', clearedFence: false,
                };
                KMFlight.resolveGrounderInterception(m, flight, 'R');
                var moves = KMFlight.deriveRunnerMoves('000', '000', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var plan = KMFlight.chainMoverPlan(m, flight, moves);
                var entry = plan.filter(function (e) { return e.pos === flight.fielder && e.base !== null; })[0];
                var scheduleLeg = schedule.filter(function (t) { return t.unassisted; })[0];
                var legs = entry ? KMFlight.unassistedLegTiming(m, entry, schedule, flight) : null;
                return {
                  fielder: flight.fielder, coverage: KMFlight.firstBaseCoverage(m, flight),
                  fieldedMs: KMFlight.fieldedMs(flight),
                  scheduleLegDrawMs: scheduleLeg ? scheduleLeg.drawMs : null,
                  legs: legs,
                };
            }"""
        )
        print(f"  fielder={unassisted3u['fielder']} coverage={unassisted3u['coverage']} "
              f"fieldedMs={unassisted3u['fieldedMs']:.1f} scheduleLegDrawMs={unassisted3u['scheduleLegDrawMs']}")
        if unassisted3u["coverage"] == unassisted3u["fielder"] and unassisted3u["legs"]:
            legs = unassisted3u["legs"]
            check("unassisted entry resolves to exactly 3 legs", len(legs), 3)
            check("leg-3 (pickup->bag) duration equals the schedule's own unassisted leg drawMs (ball and glove travel together)",
                  legs[2]["durMs"], unassisted3u["scheduleLegDrawMs"], tol=0.5)
            check("leg-1 (anchor->pickup) duration is <= fieldedMs (never later than the ball being fielded)",
                  legs[0]["durMs"] <= unassisted3u["fieldedMs"] + 0.5, True)
            check("leg-2 (dwell at pickup) duration is non-negative", legs[1]["durMs"] >= -0.5, True)
            check("leg-2 (dwell) is a zero-distance stop at the pickup point", legs[1]["distFt"], 0)
        else:
            print("  [skip] this fixture didn't resolve to a self-covering 1B (routine-vs-hard-roller boundary drifted)")

        print("\nRound 3 Task 5: an injected large holdRelease is absorbed by the dwell leg,")
        print("not by slowing leg-3's honest ball-paced run:")
        unassisted3u_hold = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '000', obc_after: '000', runs: 0, diff: 250};
                var sim = KMTraj.simulateFlight(80, 0, 32, 'R');
                var flight = {
                  ev: 80, la: 0, angle: 77, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, archetype: 'grounder', clearedFence: false,
                };
                KMFlight.resolveGrounderInterception(m, flight, 'R');
                var moves = KMFlight.deriveRunnerMoves('000', '000', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var plan = KMFlight.chainMoverPlan(m, flight, moves);
                var entry = plan.filter(function (e) { return e.pos === flight.fielder && e.base !== null; })[0];
                var scheduleLeg = schedule.filter(function (t) { return t.unassisted; })[0];
                if (!scheduleLeg) return null;
                var before = KMFlight.unassistedLegTiming(m, entry, schedule, flight);
                // Inject an artificially large holdRelease onto the schedule leg
                // (simulating what §4's floors could apply) and re-derive.
                var injected = 900;
                scheduleLeg.startMs += injected; scheduleLeg.endMs += injected;
                var after = KMFlight.unassistedLegTiming(m, entry, schedule, flight);
                return {
                  before: before, after: after, injected: injected,
                  drawMsUnchanged: scheduleLeg.drawMs,
                };
            }"""
        )
        if unassisted3u_hold:
            b, a = unassisted3u_hold["before"], unassisted3u_hold["after"]
            check("injecting a holdRelease grows the dwell leg by roughly the injected amount",
                  a[1]["durMs"] - b[1]["durMs"], unassisted3u_hold["injected"], tol=1)
            check("leg-3's own duration is untouched by the injected hold (still the ball's honest pace)",
                  a[2]["durMs"], b[2]["durMs"], tol=0.5)
        else:
            print("  [skip] no unassisted leg on this fixture's schedule")

        print("\nRound 3 follow-up: hustleRunner wired into throwRunnerAdjustmentMs -")
        print("the render-side channel a hustled runner's own token reads (previously only")
        print("runnerLateJump/stretchRunner were, so a hustled runner never visibly moved):")
        hustle_wire = page.evaluate(
            """() => {
                var sim = KMTraj.simulateFlight(60, 3, 10, 'R');
                var flight = {
                  ev: 60, la: 3, distance: sim.distance,
                  x: sim.landing.x, y: sim.landing.y,
                  contactVel: sim.contactVel, samples: sim.samples, archetype: 'single', clearedFence: false,
                };
                KMFlight.resolveHitPickup(flight);
                var m = {result: '1B', outs_before: 0, outs_after: 0, obc_before: '100', obc_after: '110',
                         runs: 0, throw_order: 's', diff: 250, batter_spd: 1};
                var moves = KMFlight.deriveRunnerMoves('100', '110', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var hustleAdj = (schedule.adjustments || []).filter(function (a) { return a.knob === 'hustleRunner'; })[0];
                if (!hustleAdj) return null;
                var got = KMFlight.throwRunnerAdjustmentMs(m, moves, flight, hustleAdj.who);
                // Manual filter over the same adjustments array (all three
                // render-facing knobs) - the correctness property
                // throwRunnerAdjustmentMs must always match.
                var expected = (schedule.adjustments || [])
                  .filter(function (a) { return a.who === hustleAdj.who &&
                    (a.knob === 'runnerLateJump' || a.knob === 'stretchRunner' || a.knob === 'hustleRunner'); })
                  .reduce(function (s, a) { return s + a.ms; }, 0);
                return { hustleMs: hustleAdj.ms, who: hustleAdj.who, got: got, expected: expected };
            }"""
        )
        if hustle_wire:
            print(f"  who={hustle_wire['who']} hustleMs={hustle_wire['hustleMs']} "
                  f"throwRunnerAdjustmentMs={hustle_wire['got']}")
            check("hustleRunner's negative ms is included in throwRunnerAdjustmentMs's total",
                  hustle_wire["got"], hustle_wire["expected"], tol=0.5)
            check("throwRunnerAdjustmentMs is actually negative here (the runner reads as faster, not zero)",
                  hustle_wire["got"] < 0, True)
        else:
            print("  [skip] this fixture no longer produces a hustleRunner adjustment (pooling tuned since)")

        print("\nOF honest pursuit + read-delay sink (Task 5b):")
        ofpursuit = page.evaluate(
            """() => {
                var m = {result: 'double'};
                function resolved(ev, la, phi, archetype, result) {
                  var sim = KMTraj.simulateFlight(ev, la, phi, 'R');
                  var flight = {
                    ev: ev, la: la, distance: sim.distance,
                    x: sim.landing.x, y: sim.landing.y,
                    contactVel: sim.contactVel, archetype: archetype, clearedFence: false,
                  };
                  KMFlight.resolveHitPickup(flight);
                  return flight;
                }
                // A deep double - honest pursuit already loses on its own,
                // confirmed by the plan's own probe sweep (97/100 sampled
                // cases needed no correction at all).
                var deep = resolved(100, 25, 10, 'double', 'double');
                var deepAnchor = KMFlight.fielderStartAnchorFt(deep.fielder, deep, {result: 'double'});
                var deepTrueAnchor = KMFlight.FIELDER_ANCHORS_FT[deep.fielder];
                // A shallow, borderline double - the one shape the sweep
                // found the honest pursuit actually winning (by ~1.1s, well
                // inside the 1500ms read-delay bound).
                var shallow = resolved(80, 15, 15, 'double', 'double');
                var shallowM = {result: 'double'};
                var shallowDeficitBefore = KMFlight.ofPursuitDeficitMs(shallowM, shallow, KMFlight.FIELDER_ANCHORS_FT[shallow.fielder]);
                var shallowReadDelay = KMFlight.ofReadDelayMs(shallowM, shallow, KMFlight.FIELDER_ANCHORS_FT[shallow.fielder]);
                var shallowAnchor = KMFlight.fielderStartAnchorFt(shallow.fielder, shallow, shallowM);
                // Grounder/infield_single archetypes never qualify - this is
                // an outfield-only mechanic.
                var grounder = resolved(85, -8, 5, 'grounder', 'GO');
                return {
                  deepFielder: deep.fielder,
                  deepAnchorIsTrue: deepAnchor.x === deepTrueAnchor.x && deepAnchor.y === deepTrueAnchor.y,
                  deepReadDelay: KMFlight.ofReadDelayMs({result:'double'}, deep, deepTrueAnchor),
                  shallowFielder: shallow.fielder,
                  shallowDeficitBefore: Math.round(shallowDeficitBefore),
                  shallowReadDelay: Math.round(shallowReadDelay),
                  shallowApplies: KMFlight.ofPursuitApplies(shallowM, shallow),
                  grounderApplies: KMFlight.ofPursuitApplies({result:'GO'}, grounder),
                  READ_DELAY_MAX: KMFlight.OF_READ_DELAY_MAX_MS,
                };
            }"""
        )
        check("a deep double's honest pursuit needs no correction (true anchor, no read delay)",
              ofpursuit["deepAnchorIsTrue"] and ofpursuit["deepReadDelay"] == 0, True)
        check("a qualifying single/double/triple in the outfield is recognized", ofpursuit["shallowApplies"], True)
        check("a grounder never qualifies for the OF pursuit mechanic", ofpursuit["grounderApplies"], False)
        print(f"  shallow double: honest deficit {ofpursuit['shallowDeficitBefore']}ms (positive = fielder would beat the ball), "
              f"read delay applied {ofpursuit['shallowReadDelay']}ms")
        check("a shallow double's honest pursuit would beat the ball before correction",
              ofpursuit["shallowDeficitBefore"] > 0, True)
        check("the read delay closes the gap exactly (no more, no less than the deficit)",
              ofpursuit["shallowReadDelay"], ofpursuit["shallowDeficitBefore"], tol=1)
        check("the read delay stays within its own bound", ofpursuit["shallowReadDelay"] <= ofpursuit["READ_DELAY_MAX"], True)

        print("\nOF throw resync - the throw never draws before the fielder's own rendered arrival (Task 3):")
        of_resync = page.evaluate(
            """() => {
                function resolved(ev, la, phi, archetype, result) {
                  var sim = KMTraj.simulateFlight(ev, la, phi, 'R');
                  var flight = {
                    ev: ev, la: la, distance: sim.distance,
                    x: sim.landing.x, y: sim.landing.y,
                    contactVel: sim.contactVel, archetype: archetype, clearedFence: false,
                  };
                  KMFlight.resolveHitPickup(flight);
                  return flight;
                }
                // Deep double, explicit ThrowOrder so throwSchedule actually
                // builds a leg (a plain hit with no ThrowOrder draws no
                // throw at all - see outThrowTargets).
                var deep = resolved(100, 25, 10, 'double', 'double');
                var m = {result: '2B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 's'};
                var moves = KMFlight.deriveRunnerMoves('000', '010', 0);
                var arrival = KMFlight.fielderBallArrivalMs(m, deep);
                var schedule = KMFlight.throwSchedule(m, moves, deep);
                var honestFieldedMs = KMFlight.fieldedMs(deep);
                // fielderBallArrivalMs must agree with what the token itself
                // renders (single-sourcing, Task 3 point 2): render the same
                // ball-touching leg through movingFielderTokenHtml with the
                // identical inputs fielderTokensHtml's solo branch would use,
                // and compare its own --delay+--dur sum.
                var anchor = KMFlight.fielderStartAnchorFt(deep.fielder, deep, m);
                var fieldedFt = KMFlight.fieldedPoint(deep);
                var svgFrom = {x: 0, y: 0};
                var distFt = Math.hypot(fieldedFt.x - anchor.x, fieldedFt.y - anchor.y);
                var kind = KMFlight.ofPursuitApplies(m, deep) ? 'pursuit' : 'run';
                var html = KMFlight.movingFielderTokenHtml(m, deep.fielder,
                  [{toSvg: svgFrom, distFt: distFt}], 0, anchor, null, kind);
                var delayMatch = html.match(/--delay:(\\d+)ms/);
                var durMatch = html.match(/--dur:(\\d+)ms/);
                var renderedArrivalMs = (delayMatch ? Number(delayMatch[1]) : null) +
                  (durMatch ? Number(durMatch[1]) : null);
                return {
                  fielder: deep.fielder,
                  arrival: arrival == null ? null : Math.round(arrival),
                  renderedArrivalMs: renderedArrivalMs,
                  scheduleBaseMs: schedule.length ? Math.round(schedule[0].startMs) : null,
                  honestFieldedMs: Math.round(honestFieldedMs),
                };
            }"""
        )
        print(f"  fielder={of_resync['fielder']} fielderBallArrivalMs={of_resync['arrival']} "
              f"rendered(--delay+--dur)={of_resync['renderedArrivalMs']} "
              f"scheduleBase={of_resync['scheduleBaseMs']} fieldedMs={of_resync['honestFieldedMs']}")
        check("fielderBallArrivalMs matches the token's own rendered arrival exactly (single-sourced)",
              of_resync["arrival"], of_resync["renderedArrivalMs"])
        check("the throw's first leg never starts before the fielder's own honest rendered arrival",
              of_resync["scheduleBaseMs"] >= of_resync["arrival"], True)

        print("\nHorizontal-spray bucket classification + station selection:")
        spray = page.evaluate(
            """() => {
                var buckets = KMFlight.SPRAY_BUCKETS.map(function (b) { return b[0]; });
                var classifications = {
                  center: KMFlight.classifySprayBucket(0),
                  lf: KMFlight.classifySprayBucket(-27),
                  rf: KMFlight.classifySprayBucket(27),
                  lfGap: KMFlight.classifySprayBucket(-13.5),
                  rfGap: KMFlight.classifySprayBucket(13.5),
                  thirdLine: KMFlight.classifySprayBucket(-40),
                  firstLine: KMFlight.classifySprayBucket(40),
                  farFoul: KMFlight.classifySprayBucket(60),
                };
                // Every real angle flightParams can actually produce (5..85,
                // i.e. offset -40..40) must classify into some real bucket -
                // no gaps in the reachable range.
                var reachableGapFound = null;
                for (var off = -40; off <= 40; off += 0.5) {
                  if (KMFlight.classifySprayBucket(off) == null) { reachableGapFound = off; break; }
                }

                // flightParams-level: a synthetic band with a stationsBySpray
                // entry for one bucket (a single, distinctive station at every
                // q) must be picked over the unconditioned band.stations when
                // the play's own angle lands in that bucket, and must fall
                // back to band.stations when it lands in a DIFFERENT bucket
                // with no stationsBySpray entry.
                function flatStation(la) {
                  return { q: 0, laTopped: la, evTopped: 90, distTopped: 380,
                           laUppercut: la, evUppercut: 90, distUppercut: 380 };
                }
                var band = {
                  lo: 0, hi: 500, archetype: "double",
                  stations: [flatStation(20)],
                  stationsBySpray: { CF: [flatStation(35)] },
                };
                var tables = { bands: { "2B": band }, excluded: [] };
                // pitch/swing chosen so lastDigit bucket=0 -> angle=45 (dead
                // center, CF) - has a stationsBySpray entry.
                var centerPlay = { result: "2B", pitch: 100, swing: 100, diff: 250, batter_hand: "R" };
                var centerFlight = KMFlight.flightParams(centerPlay, tables);
                // pitch/swing chosen so bucket=4 (unambiguous - clear of the
                // +/-5 circular-wraparound boundary) -> angle=77 (offset
                // +32, RF) - no stationsBySpray entry, must fall back.
                var otherPlay = { result: "2B", pitch: 104, swing: 100, diff: 250, batter_hand: "R" };
                var otherFlight = KMFlight.flightParams(otherPlay, tables);
                return {
                  buckets: buckets, classifications: classifications,
                  reachableGapFound: reachableGapFound,
                  centerLa: centerFlight.la, otherLa: otherFlight.la,
                };
            }"""
        )
        check("7 spray buckets defined", len(spray["buckets"]), 7)
        check("dead center classifies as CF", spray["classifications"]["center"], "CF")
        check("LF's own canonical bearing classifies as LF", spray["classifications"]["lf"], "LF")
        check("RF's own canonical bearing classifies as RF", spray["classifications"]["rf"], "RF")
        check("LF-CF alley midpoint classifies as LF_GAP", spray["classifications"]["lfGap"], "LF_GAP")
        check("CF-RF alley midpoint classifies as RF_GAP", spray["classifications"]["rfGap"], "RF_GAP")
        check("-40deg (the app's own reachable extreme) classifies as 3B_LINE", spray["classifications"]["thirdLine"], "3B_LINE")
        check("+40deg (the app's own reachable extreme) classifies as 1B_LINE", spray["classifications"]["firstLine"], "1B_LINE")
        check("a genuine foul-side outlier past +/-45deg classifies as no bucket", spray["classifications"]["farFoul"], None)
        check("every angle flightParams can actually roll (+/-40deg) lands in some real bucket",
              spray["reachableGapFound"], None)
        check("a play landing in a bucket WITH its own stationsBySpray entry reads that bucket's station",
              spray["centerLa"], 35)
        check("a play landing in a bucket with NO stationsBySpray entry falls back to band.stations",
              spray["otherLa"], 20)

        print("\nThrowOrder_LF/CF/RF (specific outfielder beats the coarser ThrowOrder_OF):")
        throw_keys = page.evaluate(
            """() => {
                return {
                  lf: KMFlight.throwOrderCandidateKeys('LF'),
                  firstBase: KMFlight.throwOrderCandidateKeys('1B'),
                };
            }"""
        )
        check("an outfield position tries its own column first, then OF", throw_keys["lf"], ["LF", "OF"])
        check("an infield/battery position has no OF fallback", throw_keys["firstBase"], ["1B"])
        throw_pref = page.evaluate(
            """() => {
                var m = {result: '2B', outs_before: 0, outs_after: 0};
                var flight = {fielder: 'LF', clearedFence: false};
                var moves = [];
                var specificWins = KMFlight.baseLegs(KMFlight.outThrowTargets(
                  Object.assign({}, m, {throw_order_by_position: {LF: 'f', OF: 's'}}), moves, flight));
                var fallsBackToOf = KMFlight.baseLegs(KMFlight.outThrowTargets(
                  Object.assign({}, m, {throw_order_by_position: {OF: 't'}}), moves, flight));
                return {specificWins: specificWins, fallsBackToOf: fallsBackToOf};
            }"""
        )
        check("ThrowOrder_LF wins over ThrowOrder_OF when both are set", throw_pref["specificWins"], ["1B"])
        check("falls back to ThrowOrder_OF when no LF-specific value is set", throw_pref["fallsBackToOf"], ["3B"])

        print("\nExplicit ThrowOrder on a hit with zero real outs (closing the general gap):")
        hit_gap = page.evaluate(
            """() => {
                var m = {result: '1B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 'f'};
                var moves = KMFlight.deriveRunnerMoves('000', '001', 0);
                var flight = {archetype: 'single', fielder: 'CF', clearedFence: false, groundTimeS: 0.3, hangMs: 1500};
                var outLookup = KMFlight.runnerForOutTarget(m, moves, '1B');
                var safeLookup = KMFlight.runnerForSafeTarget(m, moves, '1B');
                var safeArrival = KMFlight.safeRunnerArrivalMs(m, flight, moves, '1B');
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var lastLeg = schedule[schedule.length - 1];
                var hustleMs = 0;
                (schedule.adjustments || []).forEach(function (adj) { if (adj.knob === "hustleRunner") hustleMs += adj.ms; });
                return {
                  outLookupFound: outLookup != null,
                  safeLookupFrom: safeLookup && safeLookup.from,
                  safeArrival: Math.round(safeArrival),
                  lastLegOut: lastLeg.out,
                  lastLegEndMs: Math.round(lastLeg.endMs),
                  hustleMs: hustleMs,
                  contestedSafeMin: KMFlight.MARGIN_POLICY.contestedSafe.minMs,
                };
            }"""
        )
        check("the OUT-side lookup finds nothing on a play with zero real outs (the old gap)",
              hit_gap["outLookupFound"], False)
        check("the SAFE-side lookup finds the batter's own real move to 1B", hit_gap["safeLookupFrom"], "BATTER")
        check("the final leg reads as decorative (out: false), not a real out", hit_gap["lastLegOut"], False)
        # Round 3 Task 3: hustleRunner (delta>0 pool) may move the runner's
        # own rendered arrival earlier by its ms (recorded negative) - the
        # true invariant reconciles against that adjusted arrival.
        check("the throw is reconciled: lands at least contestedSafe.minMs after the batter's real arrival",
              (hit_gap["lastLegEndMs"] - (hit_gap["safeArrival"] + hit_gap["hustleMs"])) >= hit_gap["contestedSafeMin"] - 1, True)

        print("\nMulti-scorer HOME target reconciles against the TRAILING runner, not the lead scorer (Task 1b, probe 0.3):")
        multi_scorer = page.evaluate(
            """() => {
                var m = {result: '1B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 'h'};
                var moves = KMFlight.deriveRunnerMoves('110', '000', 2);
                var flight = {archetype: 'single', fielder: 'CF', clearedFence: false, groundTimeS: 0.3, hangMs: 1500};
                var mv = KMFlight.runnerForSafeTarget(m, moves, 'HOME');
                var arrival = KMFlight.safeRunnerArrivalMs(m, flight, moves, 'HOME');
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var lastLeg = schedule[schedule.length - 1];
                var hustleMs = 0;
                (schedule.adjustments || []).forEach(function (adj) { if (adj.knob === "hustleRunner") hustleMs += adj.ms; });
                return {
                  mvFrom: mv && mv.from,
                  arrival: Math.round(arrival),
                  lastLegEndMs: Math.round(lastLeg.endMs),
                  hustleMs: hustleMs,
                  contestedSafeMin: KMFlight.MARGIN_POLICY.contestedSafe.minMs,
                };
            }"""
        )
        check("resolves to the trailing (2B, further-behind) runner, not the lead (3B) scorer",
              multi_scorer["mvFrom"], "2B")
        check("the throw lands at least contestedSafe.minMs after the TRAILING runner's arrival",
              (multi_scorer["lastLegEndMs"] - (multi_scorer["arrival"] + multi_scorer["hustleMs"])) >= multi_scorer["contestedSafeMin"] - 1, True)

        print("\nFact-25 regression pin: a CF single with no recorded outs never renders as a forceOut (Task 1c):")
        fact25 = page.evaluate(
            """() => {
                // A runner on 1B advances to 2B on the single (batter to 1B) -
                // throw_order 's' (2B, Task 8's alphabet) draws a decorative
                // throw chasing that advancing runner to 2B.
                var m = {result: '1B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 's'};
                var moves = KMFlight.deriveRunnerMoves('001', '011', 0);
                var flight = {archetype: 'single', fielder: 'CF', clearedFence: false, groundTimeS: 0.3, hangMs: 1500};
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var lastLeg = schedule[schedule.length - 1];
                var arrival = KMFlight.safeRunnerArrivalMs(m, flight, moves, '2B');
                var hustleMs = 0;
                (schedule.adjustments || []).forEach(function (adj) { if (adj.knob === "hustleRunner") hustleMs += adj.ms; });
                return {
                  legOut: lastLeg.out,
                  legBase: lastLeg.base,
                  lastLegEndMs: Math.round(lastLeg.endMs),
                  arrival: Math.round(arrival),
                  hustleMs: hustleMs,
                  contestedSafeMin: KMFlight.MARGIN_POLICY.contestedSafe.minMs,
                };
            }"""
        )
        check("a plain CF single's own throw leg is never a forceOut", fact25["legOut"], False)
        check("the leg targets 2B (the advancing runner)", fact25["legBase"], "2B")
        check("it reconciles contestedSafe against the advancing runner's own arrival",
              (fact25["lastLegEndMs"] - (fact25["arrival"] + fact25["hustleMs"])) >= fact25["contestedSafeMin"] - 1, True)

        print("\nGrounder deadline compression - a fielding run never outruns fieldedMs (Task 5, facts 18/24):")
        deadline_grounder = page.evaluate(
            """() => {
                var m = {result: 'GO', outs_before: 0, outs_after: 1, diff: 250, batter_spd: 1};
                var flight = {
                  archetype: 'grounder', fielder: 'SS', clearedFence: false,
                  x: -80, y: 60, distance: 100, fieldedDistFt: 100, groundTimeS: 1.8,
                  contactVel: { vx: -30, vy: 20, vz: -2 }, angle: 29,
                };
                var fieldedMs = KMFlight.fieldedMs(flight);
                var legs = [{ toSvg: {x: 100, y: 100}, distFt: 140 }];
                var durs = KMFlight.legDurationsMs(legs, KMFlight.fielderProfile(m, 'SS', 'run'));
                var naturalMs = durs.reduce(function (a, b) { return a + b; }, 0);
                var html = KMFlight.movingFielderTokenHtml(m, 'SS', legs, 0, {x: -80, y: 60}, fieldedMs);
                var durMatch = html.match(/--dur:(\\d+)ms/);
                var compressedMs = durMatch ? Number(durMatch[1]) : null;
                return {
                  fieldedMs: Math.round(fieldedMs), naturalMs: Math.round(naturalMs),
                  compressedMs: compressedMs, naturalExceedsFielded: naturalMs > fieldedMs,
                };
            }"""
        )
        check("this fixture's natural (uncompressed) run duration exceeds fieldedMs, so compression is exercised",
              deadline_grounder["naturalExceedsFielded"], True)
        check("movingFielderTokenHtml compresses the run to at or under fieldedMs when given that deadline",
              deadline_grounder["compressedMs"] <= deadline_grounder["fieldedMs"] + 1, True)

        print("\nAn infield single with no explicit ThrowOrder gets a decorative contestedSafe 1B leg (Task 1a fold):")
        if1b_default = page.evaluate(
            """() => {
                var flight = {
                  archetype: 'infield_single', fielder: '3B', clearedFence: false,
                  x: 20, y: 30, distance: 36, fieldedDistFt: 40, groundTimeS: 0.5,
                  contactVel: { vx: 5, vy: 30, vz: -2 },
                };
                var moves = KMFlight.deriveRunnerMoves('000', '001', 0);
                var mNoOrder = {result: 'IF1B', outs_before: 0, outs_after: 0, diff: 250};
                var mWithOrder = {result: 'IF1B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 'f'};
                var targetsNoOrder = KMFlight.baseLegs(KMFlight.outThrowTargets(mNoOrder, moves, flight));
                var scheduleNoOrder = KMFlight.throwSchedule(mNoOrder, moves, flight);
                var lastLeg = scheduleNoOrder[scheduleNoOrder.length - 1];
                var targetsWithOrder = KMFlight.baseLegs(KMFlight.outThrowTargets(mWithOrder, moves, flight));
                return {
                  targetsNoOrder: targetsNoOrder,
                  lastLegOut: lastLeg.out,
                  lastLegBase: lastLeg.base,
                  targetsWithOrder: targetsWithOrder,
                };
            }"""
        )
        check("no explicit ThrowOrder still gets a default 1B target on an infield single",
              if1b_default["targetsNoOrder"], ["1B"])
        check("that leg is decorative (out: false), reconciled contestedSafe not forceOut",
              if1b_default["lastLegOut"], False)
        check("that leg targets 1B", if1b_default["lastLegBase"], "1B")
        check("an explicit ThrowOrder still wins over the infield-single default",
              if1b_default["targetsWithOrder"], ["1B"])

        print("\nDecorative/safe throws skip the dashed line, keeping only the ball (Alex's ask):")
        line_vis = page.evaluate(
            """() => {
                var withLine = KMFlight.throwLineHtml(0, 0, 100, 100, 'throw-line throw-out', 0, 200, null, true);
                var withoutLine = KMFlight.throwLineHtml(0, 0, 100, 100, 'throw-line throw-safe', 0, 200, null, false);
                var stealDefault = KMFlight.throwLineHtml(0, 0, 100, 100, 'throw-line steal-throw throw-safe', 0, 200);
                return {
                  outHasLine: withLine.indexOf('<line') !== -1,
                  outHasBall: withLine.indexOf('throw-ball') !== -1,
                  safeHasLine: withoutLine.indexOf('<line') !== -1,
                  safeHasClipPath: withoutLine.indexOf('<clipPath') !== -1,
                  safeHasBall: withoutLine.indexOf('throw-ball') !== -1,
                  stealStillHasLine: stealDefault.indexOf('<line') !== -1,
                };
            }"""
        )
        check("a real out-throw still shows the dashed line", line_vis["outHasLine"], True)
        check("a real out-throw still shows the ball", line_vis["outHasBall"], True)
        check("a decorative/safe throw (showLine=false) has no dashed line", line_vis["safeHasLine"], False)
        check("a decorative/safe throw has no clip-path either (not just hidden, never built)", line_vis["safeHasClipPath"], False)
        check("a decorative/safe throw still shows the ball", line_vis["safeHasBall"], True)
        check("a steal throw (no showLine arg passed) keeps the line by default - unscoped, unchanged", line_vis["stealStillHasLine"], True)

        print("\nShared race primitive (gameday reconciliation plan, Task 3.1):")
        prim = page.evaluate(
            """() => {
                var shortAccel = KMFlight.arrivalTimeS(5, {topSpeedFtPerS: 27, accelFtPerS2: 25, reactionS: 0});
                var closedShort = Math.sqrt(2 * 5 / 25);
                var longAccel = KMFlight.arrivalTimeS(200, {topSpeedFtPerS: 27, accelFtPerS2: 25, reactionS: 0});
                var accelDistFt = (27 * 27) / (2 * 25);
                var accelTimeToTopS = 27 / 25;
                var closedLong = accelTimeToTopS + (200 - accelDistFt) / 27;
                var flat = KMFlight.arrivalTimeS(90, {topSpeedFtPerS: 27, accelFtPerS2: Infinity, reactionS: 0});
                var reaction = KMFlight.arrivalTimeS(2, {topSpeedFtPerS: 16, accelFtPerS2: 25, reactionS: 0.15});
                var noMoveNoReaction = KMFlight.arrivalTimeS(0, {topSpeedFtPerS: 16, accelFtPerS2: 25, reactionS: 0.15});
                // Multi-leg momentum carry: two 45ft legs back-to-back must sum to
                // exactly the single 90ft run's own total time (no re-acceleration
                // from a dead stop at the waypoint).
                var legs = KMFlight.legDurationsMs([{distFt: 45}, {distFt: 45}], {topSpeedFtPerS: 27, accelFtPerS2: 25, reactionS: 0});
                var single = KMFlight.legDurationsMs([{distFt: 90}], {topSpeedFtPerS: 27, accelFtPerS2: 25, reactionS: 0});
                return {
                  shortAccel: shortAccel, closedShort: closedShort,
                  longAccel: longAccel, closedLong: closedLong,
                  flat: flat, reaction: reaction, noMoveNoReaction: noMoveNoReaction,
                  legsSum: legs[0] + legs[1], single: single[0],
                };
            }"""
        )
        check("arrivalTimeS short leg (inside accel phase) matches closed form", prim["shortAccel"], prim["closedShort"], tol=1e-6)
        check("arrivalTimeS long leg (past accel phase) matches closed form", prim["longAccel"], prim["closedLong"], tol=1e-6)
        check("arrivalTimeS with accelFtPerS2:Infinity degenerates to distance/speed", prim["flat"], 90 / 27, tol=1e-6)
        check("arrivalTimeS charges a one-time reaction beat before moving", prim["reaction"], 0.15 + math.sqrt(2 * 2 / 25), tol=1e-6)
        check("arrivalTimeS charges no reaction beat for zero distance", prim["noMoveNoReaction"], 0, tol=1e-6)
        check("legDurationsMs multi-leg momentum carry equals single-run total", prim["legsSum"], prim["single"], tol=1)

        print("\nPer-runner speed wiring (Task 3.2):")
        spd = page.evaluate(
            """() => {
                var m5 = {batter_spd: 5, runners_on_base: {"1B": ["A", "A", 1], "2B": ["B", "B", 5]}};
                var mNull = {batter_spd: null, runners_on_base: {}};
                return {
                  batterSpd5: KMFlight.runnerSpd(m5, "BATTER"),
                  spd1On1B: KMFlight.runnerSpd(m5, "1B"),
                  spd5On2B: KMFlight.runnerSpd(m5, "2B"),
                  missingBatter: KMFlight.runnerSpd(mNull, "BATTER"),
                  missingRunner: KMFlight.runnerSpd(mNull, "1B"),
                  legMsSpd5: KMFlight.runnerLegMs(m5, "BATTER", 1),
                  legMsSpd1: KMFlight.runnerLegMs({batter_spd: 1}, "BATTER", 1),
                  legMsLeagueAvg: KMFlight.runnerLegMs(mNull, "BATTER", 1),
                  legMsExplicitAvg: KMFlight.runnerLegMs({batter_spd: 3}, "BATTER", 1),
                  legMsZero: KMFlight.runnerLegMs(mNull, "BATTER", 0),
                  RUN_LEG_MS_1: KMFlight.RUN_LEG_MS[1],
                };
            }"""
        )
        check("runnerSpd resolves batter_spd for BATTER", spd["batterSpd5"], 5)
        check("runnerSpd resolves runners_on_base[who][2]", spd["spd1On1B"], 1)
        check("runnerSpd resolves a second runner independently", spd["spd5On2B"], 5)
        check("runnerSpd falls back to SPD_AVERAGE for a missing batter_spd", spd["missingBatter"], 3)
        check("runnerSpd falls back to SPD_AVERAGE for a missing runner entry", spd["missingRunner"], 3)
        check("a spd-5 runner's leg is strictly faster than a spd-1 runner's", spd["legMsSpd5"] < spd["legMsSpd1"], True)
        check("missing spd fields (historical archive) fall back to SPD_AVERAGE timing exactly",
              spd["legMsLeagueAvg"], spd["legMsExplicitAvg"], tol=1)
        print(f"  (RUN_LEG_MS[1] flat legacy value {spd['RUN_LEG_MS_1']}ms retained only as the old fallback "
              f"constant - runnerLegMs's own SPD_AVERAGE timing is now {spd['legMsExplicitAvg']}ms, "
              f"acceleration included, per the plan's 3.2 deliberate re-timing decision)")
        check("runnerLegMs(..., 0) is 0 (no bases covered)", spd["legMsZero"], 0)

        print("\nAdditive runner-speed model - +1ft/s per SPD point (Task 11):")
        speed_table = page.evaluate(
            """() => {
                var speeds = {};
                [1, 2, 3, 4, 5].forEach(function (s) {
                  speeds[s] = KMFlight.runnerProfile({batter_spd: s}, "BATTER").topSpeedFtPerS;
                });
                return speeds;
            }"""
        )
        for spd_pt, want_speed in [(1, 25), (2, 26), (3, 27), (4, 28), (5, 29)]:
            check(f"spd-{spd_pt} runner top speed", speed_table[str(spd_pt)], want_speed)
        check("spd-3 reproduces RUNNER_SPRINT_FT_PER_S exactly (league average, unchanged)",
              speed_table["3"], 27)

        print("\nRunners must not pass each other (Task 10, facts 15/5):")
        passing = page.evaluate(
            """() => {
                // A force at 2B (FCLead - not in FORCE_TIMING_RESULTS, so the
                // forced runner waits for outDelay, a late start) while the
                // batter (fast, spd 5) safely reaches 1B on the shared beat
                // right after contact - the batter's own destination (1B) is
                // exactly where the still-stationary forced runner starts.
                var m = {
                  result: 'FCLead', outs_before: 0, outs_after: 1, diff: 250,
                  obc_before: '001', throw_order: 's', batter_spd: 5,
                };
                var moves = [
                  { from: '1B', to: 'OUT', scored: false },
                  { from: 'BATTER', to: '1B', scored: false },
                ];
                var flight = { archetype: 'grounder', fielder: 'SS', clearedFence: false, groundTimeS: 0.5, hangMs: 3300, angle: 29 };
                var adjustments = KMFlight.runnerPassingAdjustments(m, flight, moves);
                var leadT = KMFlight.runnerMoveTiming(m, flight, moves, moves[0]);
                var trailT = KMFlight.runnerMoveTiming(m, flight, moves, moves[1]);
                // A more severe version (much later forced-out start) exceeds
                // even the combined bound (400ms + 15%) - still renders with
                // the bounded correction applied, not silently ignored.
                var severeFlight = Object.assign({}, flight, { hangMs: 5000 });
                var severeAdj = KMFlight.runnerPassingAdjustments(m, severeFlight, moves)['BATTER'];
                // Recompute the gap at trail's own (possibly adjusted) breakpoints,
                // same 4-breakpoint sufficiency the implementation itself relies on.
                function ordinalAt(t, delayMs, paceScale, timeVar) {
                  var mvDelay = t.mvDelay + (delayMs || 0);
                  var legDurMs = t.legDurMs * (paceScale || 1);
                  if (timeVar <= mvDelay) return t.startOrd;
                  if (timeVar >= mvDelay + legDurMs) return t.endOrd;
                  return t.startOrd + (timeVar - mvDelay) / legDurMs * (t.endOrd - t.startOrd);
                }
                var adj = adjustments['BATTER'];
                var d = adj ? adj.delayMs : 0, p = adj ? adj.paceScale : 1;
                var trailStart = trailT.mvDelay + d, trailEnd = trailStart + trailT.legDurMs * p;
                var breakpoints = [leadT.mvDelay, leadT.mvDelay + leadT.legDurMs, trailStart, trailEnd];
                var minGap = Math.min.apply(null, breakpoints.map(function (bt) {
                  return ordinalAt(leadT, 0, 1, bt) - ordinalAt(trailT, d, p, bt);
                }));
                // A different-direction pair (nothing else on base) gets no
                // adjustment at all - only one mover, nothing to collide with.
                var soloMoves = [{ from: 'BATTER', to: '1B', scored: false }];
                var soloAdj = KMFlight.runnerPassingAdjustments(m, flight, soloMoves);
                return {
                  adjustedBatter: !!adj,
                  delayMs: d, paceScale: p,
                  minGapAfterAdjustment: minGap,
                  MIN_GAP: KMFlight.RUNNER_MIN_GAP_ORD,
                  soloHasNoAdjustment: Object.keys(soloAdj).length === 0,
                  severeDelayMs: severeAdj.delayMs, severePaceScale: severeAdj.paceScale,
                  // Read the real constants rather than hardcoding a copy -
                  // Stage 5 (fielding-reconciliation-audit plan 5.3) tunes
                  // these from the corpus probe's own residual rate, and a
                  // hardcoded expectation here would go stale every time.
                  RUNNER_LATE_JUMP_MAX_MS: KMFlight.RUNNER_LATE_JUMP_MAX_MS,
                  STRETCH_MAX_SCALE: 1 + KMFlight.STRETCH_RUNNER_MAX_FRAC,
                };
            }"""
        )
        print(f"  batter adjustment: delayMs={passing['delayMs']} paceScale={round(passing['paceScale'], 4)} "
              f"minGapAfterAdjustment={round(passing['minGapAfterAdjustment'], 4)} (>= {passing['MIN_GAP']} required)")
        check("the fast trailing batter got a no-passing correction", passing["adjustedBatter"], True)
        check("the correction actually closes the gap to at least RUNNER_MIN_GAP_ORD",
              passing["minGapAfterAdjustment"] >= passing["MIN_GAP"] - 1e-6, True)
        check("a lone mover with nothing to collide with gets no adjustment", passing["soloHasNoAdjustment"], True)
        print(f"  severe case: delayMs={passing['severeDelayMs']} paceScale={round(passing['severePaceScale'], 4)} "
              f"(exceeds the bound - still renders with the capped correction, not left unbounded)")
        check("a severe violation hits the trailLateBreak bound",
              passing["severeDelayMs"], passing["RUNNER_LATE_JUMP_MAX_MS"], tol=1)
        check("a severe violation hits the trailSlowPace bound",
              passing["severePaceScale"], passing["STRETCH_MAX_SCALE"], tol=0.001)

        print("\nReconciler + margin policy (Task 4.3/4.4):")
        recon = page.evaluate(
            """() => {
                var minAt0 = KMFlight.targetMarginMs("forceOut", 0);
                var maxAt500 = KMFlight.targetMarginMs("forceOut", 500);
                var midAt250 = KMFlight.targetMarginMs("forceOut", 250);
                var uncontestedMin = KMFlight.targetMarginMs("uncontested", 0);

                // Verdict direction, out class: an honest throw that lands
                // just past the required margin - Round 3 Task 3: the
                // deficit is now POOLED across quickRelease/runnerLateJump/
                // stretchRunner by equal utilization, not exhausted through
                // quickRelease alone first.
                var lateSchedule = [{base: "1B", startMs: 750, endMs: 850, drawMs: 100, out: true}];
                var lateRunnerArrival = 1100; // targetMarginMs(forceOut,250)=300 -> required=800; deficit=50ms
                var lateResult = KMFlight.reconcileThrowSchedule(lateSchedule, lateRunnerArrival, "forceOut", 250, true);
                var lateFinal = lateSchedule[lateSchedule.length - 1];
                var lateKnobMs = {};
                lateResult.adjustments.forEach(function (a) { lateKnobMs[a.knob] = a.ms; });

                // Verdict direction, safe class: an honest throw that ARRIVES
                // TOO EARLY (beats a runner who's supposed to be safe) must be
                // held until it lands at least minMs after the runner's own
                // RENDERED arrival - Round 3 Task 3: hustleRunner (pooled
                // with slowThrow) may absorb part of the gap first, moving
                // that rendered arrival earlier before holdRelease sizes the
                // remainder.
                var earlySchedule = [{base: "1B", startMs: 100, endMs: 300, drawMs: 200, out: false}];
                var earlyRunnerArrival = 1000;
                var earlyResult = KMFlight.reconcileThrowSchedule(earlySchedule, earlyRunnerArrival, "contestedSafe", 250, false);
                var earlyFinal = earlySchedule[earlySchedule.length - 1];
                var earlyKnobMs = {};
                earlyResult.adjustments.forEach(function (a) { earlyKnobMs[a.knob] = a.ms; });
                var earlyHustleMs = earlyKnobMs.hustleRunner || 0;

                // Bounded knobs: a deficit far beyond what quickRelease (a tiny
                // THROW_DELAY_MS sliver) + the runner-side bounds could ever
                // honestly close must surface as an "unresolved" adjustment,
                // not silently vanish.
                var hopelessSchedule = [{base: "2B", startMs: 5000, endMs: 5050, drawMs: 50, out: true}];
                var hopelessResult = KMFlight.reconcileThrowSchedule(hopelessSchedule, 100, "forceOut", 250, true);
                var knobs = hopelessResult.adjustments.map(a => a.knob);

                // No-op: an honest schedule already sitting exactly at the
                // required margin must not be touched at all.
                var exactMargin = KMFlight.targetMarginMs("forceOut", 250);
                var exactEnd = 1200 - exactMargin;
                var exactSchedule = [{base: "1B", startMs: exactEnd - 200, endMs: exactEnd, drawMs: 200, out: true}];
                var exactResult = KMFlight.reconcileThrowSchedule(exactSchedule, 1200, "forceOut", 250, true);

                return {
                  minAt0: minAt0, maxAt500: maxAt500, midAt250: midAt250, uncontestedMin: uncontestedMin,
                  lateFinalEnd: lateFinal.endMs, lateKnobMs: lateKnobMs,
                  earlyFinalEnd: earlyFinal.endMs, earlyKnobMs: earlyKnobMs, earlyHustleMs: earlyHustleMs,
                  hopelessKnobs: knobs, exactAdjCount: exactResult.adjustments.length,
                };
            }"""
        )
        check("targetMarginMs(forceOut, 0) equals the class's own minMs", recon["minAt0"], 150)
        check("targetMarginMs(forceOut, 500) equals the class's own maxMs", recon["maxAt500"], 450)
        check("targetMarginMs is monotone in |diff|", recon["midAt250"] > recon["minAt0"] and recon["midAt250"] < recon["maxAt500"], True)
        check("uncontested's own minMs is looser than forceOut's", recon["uncontestedMin"] > recon["minAt0"], True)
        # Round 3 Task 3: the 50ms deficit is now pooled across quickRelease/
        # runnerLateJump/stretchRunner by equal utilization (headrooms
        # 60/400/165 -> u=50/625=0.08 -> shares ~4.8/32/13.2, summing back
        # to the deficit exactly - allocateAcrossKnobs's own correctness
        # property, re-derived here against a concrete fixture).
        late_total = (850 - recon["lateFinalEnd"]) + recon["lateKnobMs"].get("runnerLateJump", 0) + recon["lateKnobMs"].get("stretchRunner", 0)
        check("forceOut reconciliation: pooled quickRelease+runnerLateJump+stretchRunner closes the 50ms deficit exactly",
              late_total, 50, tol=0.5)
        check("forceOut reconciliation used all three pooled knobs (equal-utilization split, not sequential exhaustion)",
              all(k in recon["lateKnobMs"] for k in ("quickRelease", "runnerLateJump", "stretchRunner")), True)
        check("forceOut reconciliation: quickRelease's own share matches the proportional water-fill split (~4.8ms)",
              recon["lateKnobMs"]["quickRelease"], 4.8, tol=0.5)
        # earlyRunnerArrival's own RENDERED arrival is hustled earlier first
        # (hustleRunner pooled with slowThrow, delta>0 path) - the honest
        # invariant is against that adjusted arrival, not the original one.
        check("contestedSafe reconciliation: throw loses to the runner's own rendered (hustle-adjusted) arrival by >= contestedSafe.minMs",
              (recon["earlyFinalEnd"] - (1000 + recon["earlyHustleMs"])) >= 150 - 0.5, True)
        check("contestedSafe reconciliation used the holdRelease knob (throw needed to be later)",
              "holdRelease" in recon["earlyKnobMs"], True)
        check("contestedSafe reconciliation also used hustleRunner (slowThrow has no headroom on this synthetic leg - no distFt/throwerPos - so the whole pool went to hustleRunner)",
              "hustleRunner" in recon["earlyKnobMs"], True)
        check("an unclosable deficit surfaces as an explicit 'unresolved' adjustment, not silently absorbed",
              "unresolved" in recon["hopelessKnobs"], True)
        check("a schedule already exactly at its required margin is left untouched", recon["exactAdjCount"], 0)

        print("\nRound 3 Task 3 (§13.8): allocateAcrossKnobs - equal-utilization water-fill:")
        allocate_test = page.evaluate(
            """() => {
                // Worked example straight from the plan: 500ms deficit against
                // headrooms {gaps:100, jump:400, stretch:300} -> u=0.625 ->
                // per-knob shares 62.5/250/187.5, summing exactly to 500.
                var worked = KMFlight.allocateAcrossKnobs(500, [
                  {name: "gaps", headroomMs: 100, weight: 1},
                  {name: "jump", headroomMs: 400, weight: 1},
                  {name: "stretch", headroomMs: 300, weight: 1},
                ]);
                // Sum invariant, no saturation: totalMs <= total headroom.
                var underCap = KMFlight.allocateAcrossKnobs(120, [
                  {name: "a", headroomMs: 200, weight: 1},
                  {name: "b", headroomMs: 300, weight: 1},
                ]);
                // Sum invariant, WITH saturation: totalMs > total headroom -
                // every knob maxes out, sum == total headroom (the residual
                // is left for the caller's own overflow knob).
                var overCap = KMFlight.allocateAcrossKnobs(1000, [
                  {name: "a", headroomMs: 50, weight: 1},
                  {name: "b", headroomMs: 80, weight: 1},
                ]);
                // Weighted saturation (future-retune mechanism, not used at
                // weight=1 this round): a higher-weighted knob reaches its
                // own headroom first even though the other knob has far
                // more room left - it saturates and drops out, the rest
                // re-solves for u across what's left (still sums to
                // totalMs, no relaxation of coverage).
                var weighted = KMFlight.allocateAcrossKnobs(200, [
                  {name: "tiny", headroomMs: 20, weight: 5},
                  {name: "big", headroomMs: 500, weight: 1},
                ]);
                return {
                  worked: worked, workedSum: worked.reduce((a, b) => a + b, 0),
                  underCap: underCap, underCapSum: underCap.reduce((a, b) => a + b, 0),
                  overCap: overCap, overCapSum: overCap.reduce((a, b) => a + b, 0),
                  weighted: weighted, weightedSum: weighted.reduce((a, b) => a + b, 0),
                };
            }"""
        )
        check("worked example: gaps share ~62.5ms", allocate_test["worked"][0], 62.5, tol=0.5)
        check("worked example: jump share ~250ms", allocate_test["worked"][1], 250, tol=0.5)
        check("worked example: stretch share ~187.5ms", allocate_test["worked"][2], 187.5, tol=0.5)
        check("worked example: shares sum exactly to the 500ms deficit", allocate_test["workedSum"], 500, tol=0.5)
        check("under-cap: allocated sum equals totalMs exactly (no saturation)", allocate_test["underCapSum"], 120, tol=0.5)
        check("over-cap: allocated sum equals the FULL headroom pool (both knobs saturate)", allocate_test["overCapSum"], 130, tol=0.5)
        check("over-cap: knob a saturates at its own headroom (50)", allocate_test["overCap"][0], 50, tol=0.5)
        check("over-cap: knob b saturates at its own headroom (80)", allocate_test["overCap"][1], 80, tol=0.5)
        check("weighted saturation: the higher-weighted tiny knob saturates at its own headroom (20)",
              allocate_test["weighted"][0], 20, tol=0.5)
        check("weighted saturation: the big knob does NOT saturate (180 < its own 500 headroom)",
              allocate_test["weighted"][1] < 500, True)
        check("weighted saturation: shares still sum exactly to totalMs (200) despite one knob saturating early",
              allocate_test["weightedSum"], 200, tol=0.5)

        print("\nRound 3 Task 6 (§13.9): OF_THROW_SETUP_MS present on the hit AND tag paths,")
        print("absent on infield throws; reclaimable by quickRelease:")
        print("(schedule.setupMs is the direct, reconciliation-independent signal - the raw")
        print("schedule[0].startMs can additionally be shifted later by unrelated margin/")
        print("receiver-floor reconciliation, so that's checked as a floor, not an exact value)")
        of_setup = page.evaluate(
            """() => {
                function ofSingle() {
                  var sim = KMTraj.simulateFlight(95, 12, 20, 'R');
                  var flight = {
                    ev: 95, la: 12, distance: sim.distance,
                    x: sim.landing.x, y: sim.landing.y,
                    contactVel: sim.contactVel, samples: sim.samples, archetype: 'single', clearedFence: false,
                  };
                  KMFlight.resolveHitPickup(flight);
                  return flight;
                }
                var flight = ofSingle();
                var m = {result: '1B', outs_before: 0, outs_after: 0, diff: 250, throw_order: 's'};
                var moves = KMFlight.deriveRunnerMoves('000', '100', 0);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var honestArrival = KMFlight.fielderBallArrivalMs(m, flight);
                var floor = Math.max(KMFlight.fieldedMs(flight), honestArrival) + KMFlight.THROW_DELAY_MS + KMFlight.OF_THROW_SETUP_MS;

                // Infield grounder - no OF setup add, unchanged to the ms.
                var infSim = KMTraj.simulateFlight(85, -8, 20, 'R');
                var infFlight = {
                  ev: 85, la: -8, angle: 29, distance: infSim.distance,
                  x: infSim.landing.x, y: infSim.landing.y,
                  contactVel: infSim.contactVel, samples: infSim.samples, archetype: 'grounder', clearedFence: false,
                };
                var infM = {result: 'GO', outs_before: 0, outs_after: 1, obc_before: '000', obc_after: '000', runs: 0, diff: 250};
                KMFlight.resolveGrounderInterception(infM, infFlight, 'R');
                var infMoves = KMFlight.deriveRunnerMoves('000', '000', 0);
                var infSchedule = KMFlight.throwSchedule(infM, infMoves, infFlight);

                // Tag-throw path (sac fly) - same OF setup add.
                var tagFlight = {archetype: 'fly_ball', clearedFence: false, hangMs: 4000, fielder: 'RF'};
                var tagM = {result: 'SacF', outs_before: 0, outs_after: 1, obc_before: '100', obc_after: '000', runs: 1, throw_order: 'h', diff: 250};
                var tagMoves = KMFlight.deriveRunnerMoves('100', '000', 1);
                var tagSchedule = KMFlight.throwSchedule(tagM, tagMoves, tagFlight);

                return {
                  ofScheduleStart: schedule[0].startMs, ofFloor: floor, ofSetupMs: schedule.setupMs,
                  infSetupMs: infSchedule.setupMs,
                  tagSetupMs: tagSchedule.setupMs,
                };
            }"""
        )
        check("OF single: schedule[0].startMs >= fieldedMs/honest-arrival floor + THROW_DELAY_MS + OF_THROW_SETUP_MS",
              of_setup["ofScheduleStart"] >= of_setup["ofFloor"] - 0.5, True)
        check("OF single: schedule.setupMs records the applied OF_THROW_SETUP_MS", of_setup["ofSetupMs"], 500)
        check("infield grounder: schedule.setupMs is 0 (no fielder is an outfielder)", of_setup["infSetupMs"], 0)
        check("tag-throw (sac fly, OF fielder): schedule.setupMs records the applied OF_THROW_SETUP_MS", of_setup["tagSetupMs"], 500)

        print("\nRound 3 Task 6: quickRelease can reclaim the OF setup (constructed delta<0 case):")
        of_reclaim = page.evaluate(
            """() => {
                var schedule = [{base: '2B', startMs: 2000, endMs: 2100, drawMs: 100, out: true, distFt: 130, throwerPos: 'CF'}];
                schedule.setupMs = KMFlight.OF_THROW_SETUP_MS;
                var required = 1900; // needs to land 200ms earlier
                var result = KMFlight.reconcileThrowSchedule(schedule, required + KMFlight.MARGIN_POLICY.forceOut.minMs, 'forceOut', 0, true);
                var quickAdj = result.adjustments.filter(a => a.knob === 'quickRelease')[0];
                return { quickMs: quickAdj ? quickAdj.ms : 0, finalEnd: schedule[0].endMs };
            }"""
        )
        check("quickRelease's reclaimed amount can exceed plain THROW_DELAY_MS (OF setup adds real headroom)",
              of_reclaim["quickMs"] > 60, True)

        print("\nRound 3 Task 7 (§13.9): CUTOFF_TRANSFER_MS follows a position-leg receiver,")
        print("a plain base-leg relay keeps THROW_STAGGER_MS:")
        cutoff_gap = page.evaluate(
            """() => {
                // SS cutoff (position number 6), then home (base leg 'h') -
                // the pause after the cutoff must be CUTOFF_TRANSFER_MS, not
                // THROW_STAGGER_MS. THROW_ORDER_POSITION_NUMBER maps '6'->SS
                // (standard scorekeeping numbers).
                var m = {result: 'SacF', outs_before: 0, outs_after: 1, obc_before: '001', obc_after: '000', runs: 1,
                         throw_order: '6h', diff: 250};
                var flight = {archetype: 'fly_ball', clearedFence: false, hangMs: 4000, fielder: 'CF'};
                var moves = KMFlight.deriveRunnerMoves('001', '000', 1);
                var schedule = KMFlight.throwSchedule(m, moves, flight);
                var targets = KMFlight.outThrowTargets(m, moves, flight);
                var gaps = [];
                for (var i = 1; i < schedule.length; i++) gaps.push(schedule[i].startMs - schedule[i - 1].endMs);
                return { targetKinds: targets.map(t => t.kind), gaps: gaps };
            }"""
        )
        print(f"  targetKinds={cutoff_gap['targetKinds']} gaps={cutoff_gap['gaps']}")
        if len(cutoff_gap["gaps"]) >= 1 and cutoff_gap["targetKinds"][0] == "pos":
            check("gap after the cutoff (position) leg equals CUTOFF_TRANSFER_MS (150)", cutoff_gap["gaps"][0], 150, tol=0.5)
        else:
            print("  [skip] this fixture's ThrowOrder didn't resolve to a cutoff-then-base chain")

        print("\nRound 3 Task 8 (§13.10): a flat companion shadow under every thrown AND")
        print("pitched ball - markup smoke test:")
        shadow_markup = page.evaluate(
            """() => {
                var throwHtml = KMFlight.throwLineHtml(0, 0, 100, 0, "throw-line throw-out", 0, 500);
                var pitchHtml = KMFlight.pitchBallHtml({result: "1B"}, {samples: [{x: 0, y: 2, z: 3}]}, 0);
                function countClass(html, cls) {
                  return (html.match(new RegExp('class="' + cls, "g")) || []).length;
                }
                return {
                  throwShadowCount: countClass(throwHtml, "throw-ball-shadow"),
                  pitchShadowCount: countClass(pitchHtml, "pitch-ball-shadow"),
                  throwHasBall: throwHtml.indexOf("throw-ball-inner") !== -1,
                  pitchHasBall: pitchHtml.indexOf("pitch-ball-inner") !== -1,
                };
            }"""
        )
        check("throwLineHtml markup contains exactly one throw-ball-shadow", shadow_markup["throwShadowCount"], 1)
        check("pitchBallHtml markup contains exactly one pitch-ball-shadow", shadow_markup["pitchShadowCount"], 1)
        check("throwLineHtml still renders the ball itself (shadow didn't replace it)", shadow_markup["throwHasBall"], True)
        check("pitchBallHtml still renders the ball itself (shadow didn't replace it)", shadow_markup["pitchHasBall"], True)

        print("\nRound 3 Task 2 (§13.7): floor-only reconciliation - an honestly-decisive")
        print("play is left completely alone, even reading as a blowout:")
        task2 = page.evaluate(
            """() => {
                // Decisive forceOut: honest end is 1.2s BEFORE the required
                // margin point (more decisive than the band asks for) -
                // pre-Task-2 this would have been HELD BACK toward the band
                // (holdRelease/slowThrow); now it must be left alone.
                var decisiveForceOut = [{base: "1B", startMs: 300, endMs: 500, drawMs: 200, out: true,
                                          distFt: 130, throwerPos: "SS"}];
                var decisiveResult = KMFlight.reconcileThrowSchedule(decisiveForceOut, 2000, "forceOut", 250, true);
                var decisiveFinal = decisiveForceOut[decisiveForceOut.length - 1];

                // Honestly-very-late uncontested (a decorative sac-fly throw
                // that lands a full second past the comfortable-safe band) -
                // pre-Task-2 this would have been pulled EARLIER toward the
                // 400-600 band; now it must be left alone too.
                var lateUncontested = [{base: "HOME", startMs: 1000, endMs: 2500, drawMs: 1500, out: false}];
                var lateResult = KMFlight.reconcileThrowSchedule(lateUncontested, 1000, "uncontested", 250, false);
                var lateFinal = lateUncontested[lateUncontested.length - 1];

                // Near-tie forceOut (honest gap inside the band, deficit
                // small enough for quickRelease's own THROW_DELAY_MS-sized
                // budget to close outright) still widens exactly as before
                // Task 2 - the compressing-direction skip must not swallow
                // the genuine bang-bang correction case.
                var nearTieForceOut = [{base: "1B", startMs: 600, endMs: 800, drawMs: 200, out: true,
                                         distFt: 130, throwerPos: "SS"}];
                var nearTieRunnerArrival = 1060; // targetMarginMs(forceOut,250)=300 -> required=760; deficit=40ms
                var nearTieResult = KMFlight.reconcileThrowSchedule(nearTieForceOut, nearTieRunnerArrival, "forceOut", 250, true);
                var nearTieFinal = nearTieForceOut[nearTieForceOut.length - 1];
                var nearTieKnobMs = {};
                nearTieResult.adjustments.forEach(function (a) { nearTieKnobMs[a.knob] = a.ms; });

                // Wrong-side-winning contestedSafe: the honest throw beats
                // the safe runner outright - must still be corrected (never
                // skipped by the compressing check, which only applies to
                // delta<0 for a safe class).
                var wrongSideSafe = [{base: "1B", startMs: 100, endMs: 400, drawMs: 300, out: false}];
                var wrongSideResult = KMFlight.reconcileThrowSchedule(wrongSideSafe, 500, "contestedSafe", 250, false);
                var wrongSideFinal = wrongSideSafe[wrongSideSafe.length - 1];
                var wrongSideHustleMs = 0;
                wrongSideResult.adjustments.forEach(function (a) { if (a.knob === "hustleRunner") wrongSideHustleMs += a.ms; });

                return {
                  decisiveAdjCount: decisiveResult.adjustments.length, decisiveFinalEnd: decisiveFinal.endMs,
                  lateAdjCount: lateResult.adjustments.length, lateFinalEnd: lateFinal.endMs,
                  nearTieAdjCount: nearTieResult.adjustments.length, nearTieFinalEnd: nearTieFinal.endMs,
                  nearTieKnobMs: nearTieKnobMs,
                  wrongSideFinalEnd: wrongSideFinal.endMs, wrongSideHustleMs: wrongSideHustleMs,
                  contestedSafeMin: KMFlight.MARGIN_POLICY.contestedSafe.minMs,
                  contestedSafeMax: KMFlight.MARGIN_POLICY.contestedSafe.maxMs,
                };
            }"""
        )
        check("decisive forceOut: zero adjustments (left completely alone)", task2["decisiveAdjCount"], 0)
        check("decisive forceOut: schedule's own endMs is untouched", task2["decisiveFinalEnd"], 500)
        check("honestly-late uncontested: zero adjustments (not pulled toward the 400-600 band)", task2["lateAdjCount"], 0)
        check("honestly-late uncontested: schedule's own endMs is untouched", task2["lateFinalEnd"], 2500)
        check("near-tie forceOut still widens (compressing-skip doesn't swallow the genuine correction)",
              task2["nearTieAdjCount"] > 0, True)
        # Round 3 Task 3: the 40ms deficit is now pooled across
        # quickRelease/runnerLateJump/stretchRunner (equal utilization),
        # not exhausted through quickRelease alone - the correctness
        # property is that schedule-shift + render-side knob shares sum
        # back to the original deficit exactly.
        near_tie_total = (800 - task2["nearTieFinalEnd"]) + task2["nearTieKnobMs"].get("runnerLateJump", 0) + task2["nearTieKnobMs"].get("stretchRunner", 0)
        # tol widened from 0.5 (Stage 5, fielding-reconciliation-audit plan
        # 5.3): three independent Math.round()s, one per pooled knob, each
        # up to ~0.5ms off - RUNNER_LATE_JUMP_MAX_MS/STRETCH_RUNNER_MAX_FRAC's
        # own Stage 5 retune shifted this fixture's exact headroom split
        # enough to expose the accumulated rounding that was always possible
        # here, previously masked by this fixture's own numbers happening to
        # round cleanly under the old constants.
        check("near-tie forceOut: pooled knobs close the 40ms deficit exactly", near_tie_total, 40, tol=1.5)
        check("wrong-side-winning contestedSafe: throw still lands >= contestedSafe.minMs after the runner's own rendered (hustle-adjusted) arrival",
              (task2["wrongSideFinalEnd"] - (500 + task2["wrongSideHustleMs"])) >= 150 - 0.5, True)
        check("contestedSafe.minMs pinned at 150 (a future widening must be a deliberate test change)",
              task2["contestedSafeMin"], 150)
        check("contestedSafe.maxMs pinned at 450 (a future widening must be a deliberate test change)",
              task2["contestedSafeMax"], 450)

        print("\nPer-position throw speed table (Task 9.3):")
        speed_pos = page.evaluate(
            """() => {
                var distFt = 130;
                return {
                  rfMs: KMFlight.throwDrawMsForFt(distFt, KMFlight.THROW_SPEED_BY_POS.RF.mph),
                  cMs: KMFlight.throwDrawMsForFt(distFt, KMFlight.THROW_SPEED_BY_POS.C.mph),
                  unknownMs: KMFlight.throwDrawMsForFt(distFt),
                  ninetyMs: KMFlight.throwDrawMsForFt(distFt, 90),
                };
            }"""
        )
        check("RF's throw over a fixed distance is faster than C's (90 vs 80 mph)",
              speed_pos["rfMs"] < speed_pos["cMs"], True)
        check("an unknown/unspecified thrower falls back to 90mph (THROW_SPEED_MPH)",
              speed_pos["unknownMs"], speed_pos["ninetyMs"])

        print("\nslowThrow reconciler knob - real mph, not just a held release (Task 9.4),")
        print("rewritten to Round 3 Task 3's pooled utilization semantics (§13.8):")
        slow_throw = page.evaluate(
            """() => {
                var margin = KMFlight.targetMarginMs('contestedSafe', 250);
                var distFt = 127.28;
                var pos = 'SS'; // mph 85, min 80, max 90
                var naturalMph = KMFlight.THROW_SPEED_BY_POS[pos].mph;
                var naturalDrawMs = distFt / (naturalMph * 1.46667) * 1000;
                var floorDrawMs = distFt / (80 * 1.46667) * 1000;
                var slowHeadroomMs = floorDrawMs - naturalDrawMs;

                // In-range case: the needed mph (82) sits inside [min,max] -
                // slowThrow has real headroom here, so Task 3 pools it with
                // hustleRunner (equal utilization) instead of slowThrow
                // closing the whole gap alone.
                var neededMphA = 82;
                var neededDrawMsA = distFt / (neededMphA * 1.46667) * 1000;
                var scheduleA = [{base: '1B', startMs: 0, endMs: naturalDrawMs, drawMs: naturalDrawMs, out: false, distFt: distFt, throwerPos: pos}];
                var deltaA = neededDrawMsA - naturalDrawMs;
                var runnerArrivalA = neededDrawMsA - margin;
                var resultA = KMFlight.reconcileThrowSchedule(scheduleA, runnerArrivalA, 'contestedSafe', 250, false);
                var knobsA = resultA.adjustments.map(a => a.knob);
                var slowAdjA = resultA.adjustments.filter(a => a.knob === 'slowThrow')[0];
                var hustleAdjA = resultA.adjustments.filter(a => a.knob === 'hustleRunner')[0];

                // Floor-bound case: the needed mph (50) is well below SS's own
                // min (80), and beyond even the pooled slowThrow+hustleRunner
                // headroom combined - both saturate at their own bound and
                // holdRelease closes the residual on top.
                var neededMphB = 50;
                var neededDrawMsB = distFt / (neededMphB * 1.46667) * 1000;
                var scheduleB = [{base: '1B', startMs: 0, endMs: naturalDrawMs, drawMs: naturalDrawMs, out: false, distFt: distFt, throwerPos: pos}];
                var runnerArrivalB = neededDrawMsB - margin;
                var resultB = KMFlight.reconcileThrowSchedule(scheduleB, runnerArrivalB, 'contestedSafe', 250, false);
                var knobsB = resultB.adjustments.map(a => a.knob);
                var slowAdjB = resultB.adjustments.filter(a => a.knob === 'slowThrow')[0];
                var hustleAdjB = resultB.adjustments.filter(a => a.knob === 'hustleRunner')[0];

                return {
                  deltaA: deltaA, slowHeadroomMs: slowHeadroomMs,
                  finalEndA: scheduleA[0].endMs, naturalDrawMs: naturalDrawMs,
                  knobsA: knobsA, mphToA: slowAdjA && slowAdjA.mphTo,
                  hustleMsA: hustleAdjA ? hustleAdjA.ms : 0,
                  knobsB: knobsB, mphToB: slowAdjB && slowAdjB.mphTo,
                  finalEndB: scheduleB[0].endMs, requiredB: neededDrawMsB,
                  hustleMsB: hustleAdjB ? hustleAdjB.ms : 0,
                  runnerHustleMaxMs: KMFlight.RUNNER_HUSTLE_MAX_MS,
                };
            }"""
        )
        check("in-range case: slowThrow has real headroom on this leg (distFt/throwerPos known, not unassisted)",
              slow_throw["slowHeadroomMs"] > 0, True)
        check("in-range case: hustleRunner joined the pool (nonzero slowThrow headroom shares the gap with it)",
              "hustleRunner" in slow_throw["knobsA"], True)
        check("in-range case: slowThrow also fired", "slowThrow" in slow_throw["knobsA"], True)
        check("in-range case: no holdRelease needed (delta fits inside the pool's combined headroom)",
              "holdRelease" not in slow_throw["knobsA"], True)
        # Correctness property: schedule-shift (slowThrow's own contribution)
        # plus the hustled ms together close the ORIGINAL delta exactly -
        # the same total coverage sequential exhaustion used to guarantee,
        # just split between the throw and the runner now.
        in_range_total = (slow_throw["finalEndA"] - slow_throw["naturalDrawMs"]) + abs(slow_throw["hustleMsA"])
        check("in-range case: pooled slowThrow+hustleRunner close the delta exactly", in_range_total, slow_throw["deltaA"], tol=1)
        check("floor-bound case: slowThrow clamps to the position's own floor (80)", slow_throw["mphToB"], 80)
        check("floor-bound case: hustleRunner also saturates at its own bound (RUNNER_HUSTLE_MAX_MS)",
              abs(slow_throw["hustleMsB"]), slow_throw["runnerHustleMaxMs"], tol=0.5)
        check("floor-bound case: holdRelease closes the residual on top of both saturated knobs",
              "holdRelease" in slow_throw["knobsB"], True)
        check("floor-bound case: the combined knobs still land exactly on the hustle-adjusted required time",
              slow_throw["finalEndB"], slow_throw["requiredB"] + slow_throw["hustleMsB"], tol=1)

        print("\nCatcher EYE -> steal-throw speed (Task 14.1):")
        eye_test = page.evaluate(
            """() => {
                function mDefense(eye) {
                  return { defense: { C: ['Catcher Name', 'Name', 3, eye] } };
                }
                return {
                  eye1Mph: KMFlight.eyeArmMph(KMFlight.catcherEye(mDefense(1))),
                  eye3Mph: KMFlight.eyeArmMph(KMFlight.catcherEye(mDefense(3))),
                  eye5Mph: KMFlight.eyeArmMph(KMFlight.catcherEye(mDefense(5))),
                  missingEyeMph: KMFlight.eyeArmMph(KMFlight.catcherEye({defense: {}})),
                  noDefenseMph: KMFlight.eyeArmMph(KMFlight.catcherEye({})),
                };
            }"""
        )
        check("eye 1 -> 75mph (below-average floor)", eye_test["eye1Mph"], 75)
        check("eye 3 -> 80mph (average)", eye_test["eye3Mph"], 80)
        check("eye 5 -> 90mph (above-average ceiling)", eye_test["eye5Mph"], 90)
        check("missing eye on an otherwise-resolved catcher falls back to 80mph (average)",
              eye_test["missingEyeMph"], 80)
        check("no defense data at all falls back to 80mph (average) - same as missing eye",
              eye_test["noDefenseMph"], 80)
        check("eye is monotonically increasing 1->3->5", eye_test["eye1Mph"] < eye_test["eye3Mph"] < eye_test["eye5Mph"], True)

        print("\nstealThrowHtml draws C->3B faster than C->2B (real distance, Task 14.1):")
        steal_draw = page.evaluate(
            """() => {
                var moves2b = KMFlight.deriveRunnerMoves('001', '010', 0);
                var moves3b = KMFlight.deriveRunnerMoves('010', '100', 0);
                var m = {result: 'SB2', steal_num: 500, throw_num: 500, defense: {C: ['C', 'C', 3, 3]}};
                var html2b = KMFlight.stealThrowHtml(Object.assign({}, m, {result: 'SB2'}), moves2b, 150, 550, 0);
                var m3 = Object.assign({}, m, {result: 'SB3'});
                var html3b = KMFlight.stealThrowHtml(m3, moves3b, 150, 550, 0);
                function drawMs(html) {
                  var mt = html.match(/--pdur:(\\d+)ms/);
                  return mt ? Number(mt[1]) : null;
                }
                return { draw2b: drawMs(html2b), draw3b: drawMs(html3b) };
            }"""
        )
        print(f"  C->2B drawMs={steal_draw['draw2b']} C->3B drawMs={steal_draw['draw3b']}")
        check("C->3B (shorter, real distance) draws faster than C->2B", steal_draw["draw3b"] < steal_draw["draw2b"], True)

        print("\nPitcher AWR -> steal leadoff distance (Task 14.2):")
        awr_test = page.evaluate(
            """() => {
                return {
                  awr1: KMFlight.stealLeadoffFt({pitcher_awr: 1}),
                  awr3: KMFlight.stealLeadoffFt({pitcher_awr: 3}),
                  awr5: KMFlight.stealLeadoffFt({pitcher_awr: 5}),
                  missingAwr: KMFlight.stealLeadoffFt({}),
                  STEAL_LEADOFF_FT: KMFlight.STEAL_LEADOFF_FT,
                };
            }"""
        )
        check("awr 1 -> 14ft (a weaker pickoff threat, longer lead)", awr_test["awr1"], 14)
        check("awr 3 -> 12ft (average)", awr_test["awr3"], 12)
        check("awr 5 -> 10ft (a sharp pickoff threat, shorter lead)", awr_test["awr5"], 10)
        check("missing awr falls back to exactly today's flat 12ft constant",
              awr_test["missingAwr"], awr_test["STEAL_LEADOFF_FT"])
        check("awr is monotonically decreasing 1->3->5 (higher awr, shorter lead)",
              awr_test["awr1"] > awr_test["awr3"] > awr_test["awr5"], True)

        print("\nPitcher charge-in reaction (Task 6, probe-validated against 76,724 real plays -")
        print("see tools/pitcher_reaction_probe.py):")
        pitcher_reaction = page.evaluate(
            """() => {
                // Real flip case (s01 moment 10204007): a near-dead soft
                // roller right at the plate - the pitcher's own 0.15s
                // reaction still wins it, but the probe-selected 0.45s
                // correctly hands it to 3B instead (this exact play flips
                // between those two values in the real corpus sweep).
                var flipM = {result: 'GO', batter_hand: 'L'};
                var flipFlight = {
                  archetype: 'grounder', angle: 21, distance: 2,
                  x: -0.4087064727077347, y: 1.9577944271978103,
                  contactVel: { vx: -15.633437920733332, vy: 34.5757694707328, vz: -60.942363937660154 },
                };
                var flipResult = Object.assign({}, flipFlight);
                KMFlight.resolveGrounderInterception(flipM, flipResult, 'L');

                // True comebacker: hard-hit, short, dead center (P's own
                // nominal HZ bucket) - the probe's own nominal=P P-vs-3B
                // split (522/22) never moved across any tested reaction
                // value, confirmed here directly: P still wins even at the
                // pinned 0.45s.
                var comebackerM = {result: 'GO', batter_hand: 'R'};
                var comebackerFlight = {
                  archetype: 'grounder', angle: 45, distance: 20,
                  x: 0, y: 20, contactVel: { vx: 0, vy: -95, vz: -3 },
                };
                var comebackerResult = Object.assign({}, comebackerFlight);
                KMFlight.resolveGrounderInterception(comebackerM, comebackerResult, 'R');

                return {
                  flipWinner: flipResult.fielder,
                  comebackerWinner: comebackerResult.fielder,
                  PITCHER_CHARGE_REACTION_S: KMFlight.PITCHER_CHARGE_REACTION_S,
                };
            }"""
        )
        check("the pinned reaction value is the probe-selected 0.45s",
              pitcher_reaction["PITCHER_CHARGE_REACTION_S"], 0.45)
        check("a 3B-should-win charge (real flip case) resolves to 3B at the pinned reaction",
              pitcher_reaction["flipWinner"], "3B")
        check("a true comebacker still resolves to P at the pinned reaction",
              pitcher_reaction["comebackerWinner"], "P")

        print("\nDOM smoke test (one play slide renders a kmArc keyframe + ball-trail path):")
        smoke = page.evaluate(
            """(a) => {
                var flight = KMFlight.flightParams(a.play, a.tables);
                if (!flight) return null;
                var hand = KMFlight.effectiveHand(a.play.batter_hand);
                if (!flight.clearedFence) KMFlight.resolveHitPickup(flight);
                // ballFlightHtml/ballArcHtml aren't exported (render-only, no DOM
                // dependency) - reconstruct the same sample series + projection
                // path here to smoke-test the actual data shape they consume.
                var series = flight.samples;
                var hasSamples = Array.isArray(series) && series.length >= 2;
                var lastZero = series[series.length - 1].z === 0;
                return { hasSamples: hasSamples, lastZero: lastZero, sampleCount: series.length };
            }""",
            {
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "R", "diff": 5},
                "tables": tables,
            },
        )
        check("flight.samples exist and end exactly at landing (z=0)", smoke is not None and smoke["hasSamples"] and smoke["lastZero"], True)
        if smoke:
            print(f"  sample count: {smoke['sampleCount']} (<=48 per Part 2.2)")
            check("samples count <= 48", smoke["sampleCount"] <= 48, True)

        print("\nFielding-reconciliation audit, Stage 1 (6.4) - flight.fieldingAdjust")
        print("consolidation, old chargeAnchorFt/infieldDepthPct properties retired:")
        app_js_src = pathlib.Path("docs/js/app.js").read_text(encoding="utf-8")
        check("flight.chargeAnchorFt no longer appears anywhere in app.js",
              "flight.chargeAnchorFt" in app_js_src, False)
        check("flight.infieldDepthPct no longer appears anywhere in app.js",
              "flight.infieldDepthPct" in app_js_src, False)
        migration = page.evaluate(
            """() => {
                // outs_before/outs_after must reflect a real out here (as any
                // actual GO always does) - omitting them left this fixture
                // reading (0||0)<=(0||0) as true, which spuriously satisfied
                // resolveGrounderInterception's own no-out chargeEase gate
                // (meant only for the infield_single/bunt HIT case) and let
                // applyChargeEase's retreat step fire a nonzero depthPct here
                // too, unrelated to what this check is actually pinning.
                var m = { result: "GO", batter_hand: "R", outs_before: 0, outs_after: 1 };
                // A moderate grounder shallow enough that the honest charge-in
                // race finds a genuine mid-roll crossing (never hits the dirt-
                // edge fallback) - the "leeway never fires" case, so depthPct/
                // cappedFallback should read their own defaults.
                var flight = {
                    archetype: "grounder", angle: 35, distance: 50,
                    x: 50 * Math.sin((35 - 45) * Math.PI / 180), y: 50 * Math.cos((35 - 45) * Math.PI / 180),
                    contactVel: { vx: -15, vy: -50, vz: -3 },
                };
                KMFlight.resolveGrounderInterception(m, flight, "R");
                return {
                    hasFieldingAdjust: !!flight.fieldingAdjust,
                    hasAnchorFt: !!(flight.fieldingAdjust && flight.fieldingAdjust.anchorFt),
                    depthPct: flight.fieldingAdjust && flight.fieldingAdjust.depthPct,
                    cappedFallback: flight.fieldingAdjust && flight.fieldingAdjust.cappedFallback,
                    hasLegacyCharge: "chargeAnchorFt" in flight,
                    hasLegacyDepth: "infieldDepthPct" in flight,
                };
            }"""
        )
        check("resolveGrounderInterception populates flight.fieldingAdjust.anchorFt",
              migration["hasFieldingAdjust"] and migration["hasAnchorFt"], True)
        check("fieldingAdjust.depthPct defaults to 0 when the leeway never fires",
              migration["depthPct"], 0)
        check("fieldingAdjust.cappedFallback defaults to false when the leeway never fires",
              migration["cappedFallback"], False)
        check("no legacy flight.chargeAnchorFt property on the resolved flight",
              migration["hasLegacyCharge"], False)
        check("no legacy flight.infieldDepthPct property on the resolved flight",
              migration["hasLegacyDepth"], False)

        print("\nFielding-reconciliation audit, Stage 1 (6.4) - applyGrounderDeepSetup's")
        print("bearing-then-depth sequencing is order-dependent, pinned against a hand-")
        print("reimplementation of both the shipped order and the (unused) wrong order:")
        seq = page.evaluate(
            """() => {
                // Grid-searched for maximum bearing-vs-depth divergence under
                // WINNER_CHEAT_MAX_DEG's own small (3deg) cap - the ordering
                // only ever matters by a few feet by construction (a bigger
                // cap would make it matter more), so this fixture is chosen
                // specifically to make that real, nonzero effect visible.
                var trueAnchor = KMFlight.landingPoint(60, 10);
                var capPoint = KMFlight.landingPoint(200, 20);
                var flight = { x: 0, y: 0, contactVel: { vx: capPoint.x, vy: capPoint.y, vz: 0 } };
                var intercept = { anchorFt: trueAnchor, alongFt: 200, atS: 2.5 };
                var gp = { timeAt: function (x) { return Math.abs(x - 200) < 1e-6 ? 3.7 : null; } };

                var shipped = KMFlight.applyGrounderDeepSetup({}, flight, intercept, gp, null, null);

                // Hand-reimplementation of the SHIPPED order using only the exposed
                // primitives: bearing first (off the TRUE anchor), depth off THAT
                // bearing-adjusted dirt edge second - applyGrounderDeepSetup's own
                // documented sequence.
                var capPointFt = KMFlight.groundDirPoint(flight, intercept.alongFt);
                var bearingFirst = KMFlight.capBearingTowardFt(intercept.anchorFt, capPointFt, KMFlight.WINNER_CHEAT_MAX_DEG);
                var bearingFirstDeg = 45 + Math.atan2(bearingFirst.x, bearingFirst.y) * 180 / Math.PI;
                var trueDepthFt = Math.hypot(intercept.anchorFt.x, intercept.anchorFt.y);
                var capPointDepthFt = Math.hypot(capPointFt.x, capPointFt.y);
                var rightOrderDepth = Math.max(trueDepthFt, Math.min(KMFlight.dirtEdgeFt(bearingFirstDeg), capPointDepthFt));
                var rightOrder = KMFlight.landingPoint(rightOrderDepth, bearingFirstDeg);

                // The (never-shipped) WRONG order: depth off the TRUE bearing's own
                // dirt edge FIRST, bearing shifted second - built only to prove the
                // two orders diverge on this fixture, never called by production code.
                var trueAnchorDeg = 45 + Math.atan2(intercept.anchorFt.x, intercept.anchorFt.y) * 180 / Math.PI;
                var wrongOrderDepth = Math.max(trueDepthFt, Math.min(KMFlight.dirtEdgeFt(trueAnchorDeg), capPointDepthFt));
                var depthFirstAnchor = KMFlight.landingPoint(wrongOrderDepth, trueAnchorDeg);
                var wrongOrder = KMFlight.capBearingTowardFt(depthFirstAnchor, capPointFt, KMFlight.WINNER_CHEAT_MAX_DEG);

                return {
                    shippedAnchor: shipped.anchorFt, shippedDepthPct: shipped.depthPct,
                    shippedGroundTimeS: shipped.groundTimeS,
                    rightOrder: rightOrder, wrongOrder: wrongOrder,
                };
            }"""
        )
        check("applyGrounderDeepSetup.anchorFt.x matches the hand-reimplemented shipped (bearing-then-depth) order",
              seq["shippedAnchor"]["x"], seq["rightOrder"]["x"], tol=1e-6)
        check("applyGrounderDeepSetup.anchorFt.y matches the hand-reimplemented shipped order",
              seq["shippedAnchor"]["y"], seq["rightOrder"]["y"], tol=1e-6)
        wrong_diff = abs(seq["shippedAnchor"]["x"] - seq["wrongOrder"]["x"]) + abs(seq["shippedAnchor"]["y"] - seq["wrongOrder"]["y"])
        check("shipped bearing-then-depth order diverges from the (unused) depth-then-bearing order on this fixture",
              wrong_diff > 0.5, True)
        check("applyGrounderDeepSetup uses gp.timeAt's own resolved value for groundTimeS when it resolves",
              seq["shippedGroundTimeS"], 3.7, tol=1e-6)

        print("\nFielding-reconciliation audit, Stage 2 (6.1) - per-leg reconciliation")
        print("sweep. A hand-built 2-leg DP schedule (1B runner forced out at 2B, batter")
        print("forced out at 1B) with a real deficit on BOTH legs - the direct test that")
        print("intermediate legs now get reconciled at all (pre-Stage-2 only the final")
        print("leg ever did):")
        dp_fixture = page.evaluate(
            """() => {
                var m = { result: "DP", diff: 250, outs_before: 0, outs_after: 2, obc_before: "001", obc_after: "000" };
                var moves = KMFlight.deriveRunnerMoves("001", "000", 0);
                var flight = { archetype: "grounder", fielder: "SS", clearedFence: false, groundTimeS: 0.5, hangMs: 0 };
                // Deficits sized (post equal-split-by-side redesign, see the
                // reconciler's own comment above reconcileLeg's delta<0 branch)
                // so BOTH sides' pools engage AND speedThrow still fires on
                // leg 1 - large enough that quickRelease/runnerLateJump/
                // stretchRunner alone don't fully absorb it.
                var schedule = [
                    { base: "2B", startMs: 3600, endMs: 4581, drawMs: 981, out: true, throwerPos: "SS", distFt: 60 },
                    { base: "1B", startMs: 4631, endMs: 6212, drawMs: 1581, out: true, throwerPos: "2B", distFt: 127 },
                ];
                var result = KMFlight.reconcileChain(schedule, m, flight, moves);
                return {
                    schedule: schedule, adjustments: result.adjustments, metas: result.metas,
                    capByIdx: result.capByIdx, lastOutIdx: result.lastOutIdx,
                    THROW_SPEED_SS: KMFlight.THROW_SPEED_BY_POS["SS"], THROW_SPEED_2B: KMFlight.THROW_SPEED_BY_POS["2B"],
                };
            }"""
        )
        check("both legs got their own reconcileChain meta entry (legIndex 0 and 1)",
              sorted(mt["legIndex"] for mt in dp_fixture["metas"]), [0, 1])
        check("leg 0 (intermediate, non-final) actually received adjustments - pre-Stage-2 it received none",
              any(a["legIndex"] == 0 for a in dp_fixture["adjustments"]), True)
        check("leg 1 (final) also received adjustments, independently of leg 0's own pass",
              any(a["legIndex"] == 1 for a in dp_fixture["adjustments"]), True)
        no_legindex = [a for a in dp_fixture["adjustments"] if a.get("legIndex") is None]
        check("every adjustment (all knobs, both legs) carries a legIndex - debug traceability", len(no_legindex), 0)
        # Per-runner attribution (6.1's DP two-runner pin): leg 0's forced
        # runner is the 1B runner, leg 1's is the batter - runnerLateJump/
        # stretchRunner must only ever be attributed to the leg's own runner.
        leg0_runner_knobs = {a["who"] for a in dp_fixture["adjustments"]
                              if a["legIndex"] == 0 and a["knob"] in ("runnerLateJump", "stretchRunner")}
        leg1_runner_knobs = {a["who"] for a in dp_fixture["adjustments"]
                              if a["legIndex"] == 1 and a["knob"] in ("runnerLateJump", "stretchRunner")}
        check("leg 0's runner-side knobs are attributed only to the 1B runner", leg0_runner_knobs, {"1B"})
        check("leg 1's runner-side knobs are attributed only to the BATTER", leg1_runner_knobs, {"BATTER"})
        # Multi-pass speedThrow ceiling pin: both legs' speedThrow adjustments
        # (if any) must never exceed their own thrower's own ceiling mph.
        speed_adjs = [a for a in dp_fixture["adjustments"] if a["knob"] == "speedThrow"]
        check("at least one speedThrow adjustment fired on this fixture (worth pinning)", len(speed_adjs) > 0, True)
        ceilings = {"SS": dp_fixture["THROW_SPEED_SS"]["max"], "2B": dp_fixture["THROW_SPEED_2B"]["max"]}
        over_ceiling = [a for a in speed_adjs if a["mphTo"] > ceilings[dp_fixture["schedule"][a["legIndex"]]["throwerPos"]]]
        check("no speedThrow adjustment ever exceeds its own leg's thrower ceiling mph, across both passes",
              len(over_ceiling), 0)
        check("inter-leg gap (leg 1 start - leg 0 end) stays non-negative after both legs' own passes (invariant #11)",
              dp_fixture["schedule"][1]["startMs"] - dp_fixture["schedule"][0]["endMs"] >= -0.5, True)
        check("capByIdx carries one entry per real out leg (both legs here)",
              sorted(int(k) for k in dp_fixture["capByIdx"].keys()), [0, 1])

        print("\nFielding-reconciliation audit, Stage 2 (6.1) - floor-only pin: a later out")
        print("leg that finds itself compressing (an even bigger blowout) after an earlier")
        print("leg's own quickRelease shift must be left completely alone, not pulled back")
        print("into the margin band:")
        floor_only = page.evaluate(
            """() => {
                var m = { result: "DP", diff: 250, outs_before: 0, outs_after: 2, obc_before: "001", obc_after: "000" };
                var moves = KMFlight.deriveRunnerMoves("001", "000", 0);
                var flight = { archetype: "grounder", fielder: "SS", clearedFence: false, groundTimeS: 0.5, hangMs: 0 };
                // leg 0: a real 150ms deficit, no throwerPos (isolates the
                // water-fill pool's own quickRelease share, ~49ms here, as the
                // sole schedule-moving contributor - deterministic, verified).
                // leg 1: pre-correction delta is a small genuine deficit
                // (NOT compressing) - after inheriting leg 0's whole-chain
                // quickRelease sliver shift, it flips to compressing.
                var schedule = [
                    { base: "2B", startMs: 3636, endMs: 4036, drawMs: 400, out: true },
                    { base: "1B", startMs: 4046, endMs: 4141, drawMs: 95, out: true },
                ];
                var gapPre = schedule[1].startMs - schedule[0].endMs;
                var result = KMFlight.reconcileChain(schedule, m, flight, moves);
                return {
                    schedule: schedule, adjustments: result.adjustments, gapPre: gapPre,
                    gapPost: schedule[1].startMs - schedule[0].endMs,
                };
            }"""
        )
        leg1_adjustments = [a for a in floor_only["adjustments"] if a["legIndex"] == 1]
        check("leg 1's pre-existing gap to leg 0 was non-negative (a valid schedule to begin with)",
              floor_only["gapPre"] >= 0, True)
        check("leg 0 (the earlier leg) did receive its own correction",
              any(a["legIndex"] == 0 for a in floor_only["adjustments"]), True)
        check("leg 1 (now compressing after inheriting leg 0's shift) receives ZERO adjustments - floor-only",
              len(leg1_adjustments), 0)
        check("the gap between leg 0 and leg 1 is preserved exactly (both shifted together by quickRelease's sliver)",
              floor_only["gapPost"], floor_only["gapPre"], tol=0.5)

        print("\nFielding-reconciliation audit, Stage 2 (6.1) - suffix-hold pin: a final")
        print("decorative/contestedSafe leg's own 'land later' hold must shift only the")
        print("suffix after the last reconciled out leg, never reach back and re-time an")
        print("out leg's own already-locked-in margin (the one genuine conflict the")
        print("sequential model has - holdFromIdx is its complete resolution). Built on a")
        print("fielder's-choice shape (lead runner forced out at 2B, batter safe at 1B):")
        suffix_hold = page.evaluate(
            """() => {
                var m = { result: "FC", diff: 250, outs_before: 0, outs_after: 1 };
                var moves = [{from: "1B", to: "OUT", scored: false}, {from: "BATTER", to: "1B", scored: false}];
                var flight = { archetype: "grounder", fielder: "SS", clearedFence: false, groundTimeS: 0.5, hangMs: 0 };
                // leg 0 (out, 2B): constructed to sit EXACTLY on its own
                // required margin - zero slack, so any later shift at all
                // would break it. leg 1 (safe, 1B, final, no throwerPos so
                // slowThrow can't help): arrives 400ms too early, needing a
                // real hold - big enough that hustleRunner's own 200ms cap
                // can't cover it alone, forcing holdChainTo to actually fire.
                var schedule = [
                    { base: "2B", startMs: 3786, endMs: 3886, drawMs: 100, out: true },
                    { base: "1B", startMs: 3936, endMs: 4086, drawMs: 150, out: false },
                ];
                var leg0EndBefore = schedule[0].endMs;
                var result = KMFlight.reconcileChain(schedule, m, flight, moves);
                return {
                    schedule: schedule, adjustments: result.adjustments,
                    leg0EndBefore: leg0EndBefore, capByIdx: result.capByIdx,
                };
            }"""
        )
        check("leg 0 (the earlier out leg, already exactly at its own margin) is completely untouched by leg 1's own later hold",
              suffix_hold["schedule"][0]["endMs"], suffix_hold["leg0EndBefore"], tol=1e-6)
        check("leg 1 (the final safe leg) did receive a holdRelease to close its own deficit",
              any(a["legIndex"] == 1 and a["knob"] == "holdRelease" for a in suffix_hold["adjustments"]), True)
        check("holdRelease's own adjustment is attributed to leg 1, not leg 0",
              all(a["legIndex"] == 1 for a in suffix_hold["adjustments"] if a["knob"] == "holdRelease"), True)
        check("capByIdx carries an entry only for the real out leg (leg 0), never the final safe leg",
              sorted(int(k) for k in suffix_hold["capByIdx"].keys()), [0])

        print("\nFielding-reconciliation audit, Stage 2 (6.1) - reconcileThrowSchedule")
        print("back-compat wrapper: byte-for-byte reconcileLeg(schedule, last, ..., 0):")
        wrapper_check = page.evaluate(
            """() => {
                var scheduleA = [{ base: "1B", startMs: 700, endMs: 850, drawMs: 150, out: true }];
                var scheduleB = [{ base: "1B", startMs: 700, endMs: 850, drawMs: 150, out: true }];
                var viaWrapper = KMFlight.reconcileThrowSchedule(scheduleA, 1300, "forceOut", 250, true, "1B");
                var viaDirect = KMFlight.reconcileLeg(scheduleB, 0, 1300, "forceOut", 250, true, "1B", 0);
                return {
                    scheduleA: scheduleA, scheduleB: scheduleB,
                    adjA: viaWrapper.adjustments, adjB: viaDirect.adjustments,
                };
            }"""
        )
        check("reconcileThrowSchedule produces the identical final schedule state as reconcileLeg(.., last, .., 0)",
              wrapper_check["scheduleA"][0]["endMs"], wrapper_check["scheduleB"][0]["endMs"], tol=1e-9)
        check("reconcileThrowSchedule produces the identical adjustments as its reconcileLeg equivalent",
              [a["knob"] for a in wrapper_check["adjA"]], [a["knob"] for a in wrapper_check["adjB"]])

        print("\nFielding-reconciliation audit, Stage 3 (6.3) - bidirectional pace knob.")
        print("Real corpus-derived IF1B fixtures (docs/data/s01) whose honest charge race")
        print("trips the 4.2 trigger (the race would beat the batter to first, meaning the")
        print("downstream reconciler would otherwise have had to lean on slowThrow/")
        print("holdRelease alone to sell the recorded hit):")
        s01_km_fp = pathlib.Path("docs/data/s01/key_moments.json")
        s01_meta_fp = pathlib.Path("docs/data/s01/meta.json")
        if s01_km_fp.exists() and s01_meta_fp.exists():
            s01_moments = json.loads(s01_km_fp.read_text(encoding="utf-8"))
            s01_tables = json.loads(s01_meta_fp.read_text(encoding="utf-8"))["flight"]
            page.evaluate("(t) => KMFlight.setProbeFlightTables(t)", s01_tables)
            # Two verified real corpus moments whose honest charge race
            # trips the trigger by different margins (10403048 barely,
            # 10402043 more substantially). charge.min itself is read
            # dynamically (Stage 5, plan 5.3, widened 0.75->0.60 off the
            # probe's own real data) rather than hardcoded, so a future
            # retune doesn't go stale here.
            charge_min = page.evaluate("KMFlight.FIELDER_PACE_SCALE.charge.min")
            for mid in ("10403048", "10402043"):
                m = next((mm for mm in s01_moments if mm["moment_id"] == mid), None)
                if not m:
                    print(f"  [skip] moment {mid} not found in docs/data/s01/key_moments.json")
                    continue
                pace = page.evaluate(
                    """(m) => {
                        var flight = KMFlight.resolvePlayFlight(m);
                        var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                        var schedule = KMFlight.throwSchedule(m, moves, flight);
                        var lastLeg = schedule[schedule.length - 1];
                        var arrival = KMFlight.safeRunnerArrivalMs(m, flight, moves, lastLeg.base);
                        var marginSafe = KMFlight.targetMarginMs("contestedSafe", m.diff);
                        var maxAlongFt = KMFlight.dirtEdgeFt(flight.angle) - flight.distance;
                        var fieldedAlongFt = flight.fieldedDistFt - flight.distance;
                        var honestGroundTimeS = flight.groundPath.timeAt(fieldedAlongFt);
                        return {
                            paceScale: flight.fieldingAdjust.paceScale, cappedFallback: flight.fieldingAdjust.cappedFallback,
                            lastLegEndMs: lastLeg.endMs, arrival: arrival, marginSafe: marginSafe,
                            fieldedAlongFt: fieldedAlongFt, maxAlongFt: maxAlongFt,
                            groundTimeS: flight.groundTimeS, honestGroundTimeS: honestGroundTimeS,
                            adjustments: schedule.adjustments,
                        };
                    }""",
                    m,
                )
                print(f"  {mid}: paceScale={pace['paceScale']}")
                check(f"{mid}: the slow-charge trigger actually fired (paceScale is set)",
                      pace["paceScale"] is not None, True)
                check(f"{mid}: paceScale within [charge.min, 1)",
                      charge_min - 1e-9 <= pace["paceScale"] < 1, True)
                # hustleRunner (reconcileLeg's own delta>0 pool) moves the
                # runner's own internal required-arrival target earlier by its
                # recorded ms (always <= 0) before holdRelease closes the
                # remainder exactly - safeRunnerArrivalMs above is the honest,
                # pre-hustle arrival, so it has to be folded back in here or
                # this check compares against a target the schedule itself
                # was never actually reconciled against (surfaced once the
                # retreat step above started making hustleRunner fire on
                # plays that used to need charge-easing alone).
                hustle_ms = sum(a["ms"] for a in pace["adjustments"] if a["knob"] == "hustleRunner")
                check(f"{mid}: the play still renders as a hit - throw loses by >= contestedSafe floor",
                      pace["lastLegEndMs"] - pace["arrival"] - hustle_ms >= pace["marginSafe"] - 0.5, True)
                check(f"{mid}: fielded point stays within maxAlongFt (never rolls past the dirt edge)",
                      pace["fieldedAlongFt"] <= pace["maxAlongFt"] + 0.5, True)
                check(f"{mid}: recorded groundTimeS is never earlier than the ball's own honest natural time there (no relabeling)",
                      pace["groundTimeS"] >= pace["honestGroundTimeS"] - 0.5, True)
                check(f"{mid}: the leeway block did NOT also fire (disjointness pin)",
                      pace["cappedFallback"], False)
        else:
            print("  [skip] docs/data/s01 not found on disk")

        print("\nFielding-reconciliation audit, Stage 3 (6.3) - disjointness pin (synthetic):")
        print("a fixture engineered to trip the leeway block's own cappedFallback, with the")
        print("outs gate also open (no new out) - the slow-charge knob must never fire")
        print("alongside it:")
        disjoint = page.evaluate(
            """() => {
                var m = { result: "GO", batter_hand: "R", outs_before: 0, outs_after: 0, diff: 250 };
                var flight = {
                    archetype: "grounder", angle: 35, distance: 90,
                    x: 90 * Math.sin((35 - 45) * Math.PI / 180), y: 90 * Math.cos((35 - 45) * Math.PI / 180),
                    contactVel: { vx: -30, vy: -70, vz: -3 },
                };
                KMFlight.resolveGrounderInterception(m, flight, "R");
                return { fieldingAdjust: flight.fieldingAdjust };
            }"""
        )
        check("the leeway block's own cappedFallback fired on this fixture (precondition)",
              disjoint["fieldingAdjust"]["cappedFallback"], True)
        check("the slow-charge knob did NOT also fire (paceScale stays unset) - disjointness pin",
              disjoint["fieldingAdjust"].get("paceScale") is None, True)

        print("\nFielding-reconciliation audit, Stage 3 (6.3) - a real GO/DP fixture asserts")
        print("paceScale stays unset (outs never get slowed):")
        if s01_km_fp.exists():
            go_dp = next((mm for mm in s01_moments
                          if mm["result"] in ("GO", "DP") and (mm.get("outs_after", 0) - mm.get("outs_before", 0)) >= 1), None)
            if go_dp:
                go_result = page.evaluate(
                    """(m) => {
                        var flight = KMFlight.resolvePlayFlight(m);
                        return flight ? { archetype: flight.archetype, fieldingAdjust: flight.fieldingAdjust } : null;
                    }""",
                    go_dp,
                )
                if go_result:
                    print(f"  {go_dp['moment_id']} ({go_dp['result']}): {go_result}")
                    check("a real out play (GO/DP) never sets paceScale - the outs gate holds",
                          go_result["fieldingAdjust"].get("paceScale") is None, True)
                else:
                    print("  [skip] no resolvable flight on the chosen GO/DP moment")
            else:
                print("  [skip] no GO/DP moment found in docs/data/s01")

        print("\nFielding-reconciliation audit, Stage 4 (6.2) - coverage pool")
        print("(reconcileCoverage): a covering fielder's own bounded, pooled, recorded")
        print("correction, replacing the old silent/unbounded §4.3 deadline compression.")
        print("SS covering 2B (34.7ft, natural run 2290ms raw incl. start delay):")
        coverage = page.evaluate(
            """() => {
                var m = { result: "GO", diff: 250 };
                var anchor = KMFlight.FIELDER_ANCHORS_FT.SS;
                var baseFt = KMFlight.BASE_POS_FT["2B"];
                var dist = Math.hypot(baseFt.x - anchor.x, baseFt.y - anchor.y);
                var legs = [{ toSvg: { x: 0, y: 0 }, distFt: dist }];
                var natPacing = KMFlight.fielderMovePacing(m, "SS", legs, 300, null, "run");
                var naturalArrival = natPacing.delayMs + natPacing.totalMs;

                function fixture(deficitMs) {
                    var plan = [{
                        pos: "SS", base: "2B", kind: "base", cutoffFt: null, anchor: anchor, legs: legs,
                        startMs: 300, deadlineMs: null, profileKind: "run", arrivalMs: naturalArrival,
                    }];
                    var schedule = [{ base: "2B", kind: "base", startMs: 0, endMs: naturalArrival - deficitMs, drawMs: 100, out: true }];
                    var result = KMFlight.reconcileCoverage(m, {}, plan, schedule);
                    // Simulate fielderTokensHtml's own follow-up backstop pass (the
                    // real §4.3 deadline compression, now against this already-eased
                    // baseline) to verify the FULL invariant, not just the pool's own share.
                    var backstop = KMFlight.fielderMovePacing(m, plan[0].pos, plan[0].legs, plan[0].startMs, schedule[0].endMs, plan[0].profileKind, plan[0].paceScaleOverride);
                    return {
                        entry: plan[0], adjustments: result.adjustments, legEndMs: schedule[0].endMs,
                        backstopArrivalMs: backstop.delayMs + backstop.totalMs,
                    };
                }
                return { naturalArrival: naturalArrival, small: fixture(100), huge: fixture(1000) };
            }"""
        )
        small, huge = coverage["small"], coverage["huge"]
        check("small deficit (100ms): the pool alone closes it - post-reconcileCoverage arrival <= leg's endMs",
              small["entry"]["arrivalMs"] <= small["legEndMs"] + 0.5, True)
        check("small deficit: no coverCompress needed (pool was enough)",
              any(a["knob"] == "coverCompress" for a in small["adjustments"]), False)
        check("coverEarlyBreak never pushes startMs below 0 (huge deficit saturates it exactly at 0)",
              huge["entry"]["startMs"] >= -0.5, True)
        check("fielderSprint never exceeds FIELDER_PACE_SCALE.run.max (huge deficit saturates exactly at max)",
              huge["entry"]["paceScaleOverride"] <= 1.12 + 1e-6, True)
        check("huge deficit (1000ms, exceeds the ~354ms combined pool): backstop.arrival still equals leg.endMs exactly - invariant #4 always holds",
              huge["backstopArrivalMs"], huge["legEndMs"], tol=0.5)
        check("huge deficit produces a recorded coverCompress adjustment (the honest backstop, not silent)",
              any(a["knob"] == "coverCompress" and a["ms"] > 0 for a in huge["adjustments"]), True)

        print("\nFielding-reconciliation audit, Stage 4 (6.2) - unassisted carry knobs")
        print("(sprintCarry/easeCarry) inside reconcileLeg, sharing FIELDER_PACE_SCALE.run")
        print("with reconcileCoverage's fielderSprint. 3B fielding a force at 3rd himself")
        print("(65ft unassisted leg, natural jog 3111ms):")
        carry = page.evaluate(
            """() => {
                var m = { result: "FC3rd", diff: 250 };
                var distFt = 65;
                var natMs = KMFlight.fielderLegDurationsMs(m, "3B", [{ distFt: distFt }], "run")[0];
                var legEndMs0 = 500 + natMs;
                function mkSchedule(isOut) {
                    return [{ base: "3B", startMs: 500, endMs: legEndMs0, drawMs: natMs, out: isOut, throwerPos: "3B", distFt: distFt, unassisted: true }];
                }
                var margin = KMFlight.targetMarginMs(m.result === "FC3rd" ? "forceOut" : "forceOut", 250);
                var marginOut = KMFlight.targetMarginMs("forceOut", 250);
                var marginSafe = KMFlight.targetMarginMs("contestedSafe", 250);

                // sprintCarry (fast, isOut=true, delta<0): a - a deficit past
                // quickRelease's own 500ms sliver headroom (so the fielding
                // side's own share actually reaches sprintCarry) but still
                // fully closeable between the fielding and running sides'
                // own equal shares (no unresolved); b - a much larger deficit,
                // saturates sprintCarry at run.max exactly, honest residual
                // reported once both sides' own real ceilings are exhausted.
                // (Sized empirically post equal-split-by-side redesign - see
                // reconcileLeg's own delta<0 branch comment - since quickRelease
                // now only ever gets a fraction of a small deficit, not the
                // whole thing.)
                function runFast(runnerArrival) {
                    var sched = mkSchedule(true);
                    var res = KMFlight.reconcileLeg(sched, 0, runnerArrival, "forceOut", 250, true, "2B", 0, m);
                    return { leg: sched[0], adjustments: res.adjustments };
                }
                var a = runFast(legEndMs0 - 1400 + marginOut);
                var b = runFast(legEndMs0 - 2000 + marginOut);

                // easeCarry (slow, isOut=false/contestedSafe, delta>0): c - small
                // deficit, closes without a hold; d - large deficit, saturates at
                // run.min exactly, holdRelease closes the honest remainder.
                function runSlow(runnerArrival) {
                    var sched = mkSchedule(false);
                    var res = KMFlight.reconcileLeg(sched, 0, runnerArrival, "contestedSafe", 250, false, "2B", 0, m);
                    return { leg: sched[0], adjustments: res.adjustments };
                }
                var c = runSlow(legEndMs0 - marginSafe + 100);
                var d = runSlow(legEndMs0 - marginSafe + 900);

                var maxMs = KMFlight.fielderLegDurationsMs(m, "3B", [{ distFt: distFt }], "run", KMFlight.FIELDER_PACE_SCALE.run.max)[0];
                var minMs = KMFlight.fielderLegDurationsMs(m, "3B", [{ distFt: distFt }], "run", KMFlight.FIELDER_PACE_SCALE.run.min)[0];
                return { natMs: natMs, maxMs: maxMs, minMs: minMs, a: a, b: b, c: c, d: d };
            }"""
        )
        a, b, c, d, maxMs, minMs = carry["a"], carry["b"], carry["c"], carry["d"], carry["maxMs"], carry["minMs"]
        check("sprintCarry (small deficit): leg closes without any 'unresolved' - the pace ceiling honestly allows it",
              any(adj["knob"] == "unresolved" for adj in a["adjustments"]), False)
        check("sprintCarry (small deficit): a real sprintCarry adjustment fired",
              any(adj["knob"] == "sprintCarry" for adj in a["adjustments"]), True)
        check("sprintCarry (large deficit): leg's drawMs never drops below the run.max ceiling's own ms",
              b["leg"]["drawMs"] >= maxMs - 0.5, True)
        check("sprintCarry (large deficit): drawMs saturates AT the ceiling exactly (fully used, not overshot)",
              b["leg"]["drawMs"], maxMs, tol=1)
        check("sprintCarry (large deficit): the honest excess surfaces as 'unresolved', not silently absorbed",
              any(adj["knob"] == "unresolved" for adj in b["adjustments"]), True)
        check("easeCarry (small deficit): leg closes with no holdRelease needed",
              any(adj["knob"] == "holdRelease" for adj in c["adjustments"]), False)
        check("easeCarry (small deficit): a real easeCarry adjustment fired",
              any(adj["knob"] == "easeCarry" for adj in c["adjustments"]), True)
        check("easeCarry (large deficit): leg's drawMs never exceeds the run.min floor's own ms",
              d["leg"]["drawMs"] <= minMs + 0.5, True)
        check("easeCarry (large deficit): drawMs saturates AT the floor exactly (fully used, not overshot)",
              d["leg"]["drawMs"], minMs, tol=1)
        check("easeCarry (large deficit): holdRelease closes the honest remainder on top of the saturated floor",
              any(adj["knob"] == "holdRelease" for adj in d["adjustments"]), True)

        print("\nFielding-reconciliation audit, Stage 6 - full corpus sweep: every real")
        print("play's own out legs, through the exact production pipeline (resolvePlayFlight")
        print("-> deriveRunnerMoves -> throwSchedule), asserting the two invariants that must")
        print("ALWAYS hold - inter-leg gaps never negative (#11), and every out leg either")
        print("beats its own runner by the margin floor OR is honestly flagged 'unresolved'")
        print("(never silently violated) - tracking, not gating, the residual rate itself")
        print("(tools/reconciliation_corpus_probe.py's own job, see Stage 5's findings):")
        if meta_fp.exists() and real_plays:
            # resolvePlayFlight reads data.meta.flight off the page's own module
            # scope (not an argument) - the Stage 3 tests above pointed it at
            # s01's own tables via setProbeFlightTables and never reset it, so
            # this section (and the debug-panel smoke test after it) must
            # explicitly repoint it at this feed's own real_tables first, or
            # every play here silently resolves against the wrong season's
            # EV/LA/distance bands.
            page.evaluate("(t) => KMFlight.setProbeFlightTables(t)", real_tables)
            corpus_sweep = page.evaluate(
                """(a) => {
                    var bad = [];
                    var shapesSeen = {};
                    var totalOutLegs = 0, unresolvedLegs = 0;
                    a.plays.forEach(function (m) {
                        try {
                            var flight = KMFlight.resolvePlayFlight(m);
                            if (!flight) return;
                            var moves = KMFlight.deriveRunnerMoves(
                                String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                            var schedule = KMFlight.throwSchedule(m, moves, flight);
                            if (!schedule.length) return;
                            var recorded = (m.outs_after || 0) - (m.outs_before || 0);
                            if (recorded >= 2) shapesSeen[m.result + "|" + (m.throw_order || "(none)")] = true;

                            for (var g = 1; g < schedule.length; g++) {
                                if (schedule[g].startMs < schedule[g - 1].endMs - 0.5) {
                                    bad.push(m.moment_id + ": negative gap at leg " + g + " (invariant #11)");
                                }
                            }

                            var adjByLeg = {};
                            (schedule.adjustments || []).forEach(function (adj) {
                                if (adj.legIndex == null) return;
                                (adjByLeg[adj.legIndex] = adjByLeg[adj.legIndex] || []).push(adj);
                            });
                            for (var i = 0; i < schedule.length; i++) {
                                if (!schedule[i].out) continue;
                                totalOutLegs++;
                                var legAdj = adjByLeg[i] || [];
                                if (legAdj.some(function (a) { return a.knob === "unresolved"; })) {
                                    unresolvedLegs++;
                                    continue;
                                }
                                var arrival = KMFlight.forcedOutRunnerArrivalMs(m, flight, moves, schedule[i].base);
                                if (arrival == null) continue;
                                // Fielding-reconciliation-audit follow-up: runnerLateJump/
                                // stretchRunner now genuinely retarget this leg's own honest
                                // runner arrival LATER (never just cosmetic), each bounded by
                                // its own real cap - fold their recorded shares for THIS leg
                                // back into the threshold (auditable from the adjustments
                                // themselves, not re-derived), and verify each stayed within
                                // its own bound while at it. applyReceiverFloors (Task 2,
                                // section 2.5) may since have pushed the leg later still,
                                // capped at MARGIN_POLICY.forceOut.minMs (the loosest honest
                                // margin, capByIdx's own documented contract) off THIS
                                // retargeted arrival, not the original one.
                                var jumpMs = 0, stretchMs = 0;
                                legAdj.forEach(function (a) {
                                    if (a.knob === "runnerLateJump") jumpMs += a.ms;
                                    if (a.knob === "stretchRunner") stretchMs += a.ms;
                                });
                                if (jumpMs > KMFlight.RUNNER_LATE_JUMP_MAX_MS + 0.5) {
                                    bad.push(m.moment_id + ": leg " + i + " runnerLateJump (" + jumpMs +
                                        "ms) exceeds RUNNER_LATE_JUMP_MAX_MS");
                                }
                                if (stretchMs > arrival * KMFlight.STRETCH_RUNNER_MAX_FRAC + 0.5) {
                                    bad.push(m.moment_id + ": leg " + i + " stretchRunner (" + stretchMs +
                                        "ms) exceeds STRETCH_RUNNER_MAX_FRAC of this runner's own arrival");
                                }
                                var retargetedArrival = arrival + jumpMs + stretchMs;
                                var minMargin = KMFlight.MARGIN_POLICY.forceOut.minMs;
                                if (schedule[i].endMs > retargetedArrival - minMargin + 0.5) {
                                    bad.push(m.moment_id + ": leg " + i +
                                        " silently violates even the loosest honest margin (not flagged unresolved)");
                                }
                            }
                        } catch (e) {
                            bad.push((m.moment_id || "?") + ": EXCEPTION " + e.message);
                        }
                    });
                    return {
                        bad: bad, shapeCount: Object.keys(shapesSeen).length,
                        totalOutLegs: totalOutLegs, unresolvedLegs: unresolvedLegs,
                    };
                }""",
                {"plays": real_plays, "tables": real_tables},
            )
            print(f"  swept {len(real_plays)} real plays, {corpus_sweep['shapeCount']} distinct multi-out "
                  f"shapes, {corpus_sweep['totalOutLegs']} out legs")
            check("no exceptions, no negative inter-leg gaps, every out leg's margin either "
                  "holds or is honestly flagged unresolved (never silently violated)",
                  len(corpus_sweep["bad"]), 0)
            for bad in corpus_sweep["bad"][:10]:
                print(f"    - {bad}")
            total = corpus_sweep["totalOutLegs"]
            pct = 100.0 * corpus_sweep["unresolvedLegs"] / total if total else 0.0
            print(f"  unresolved rate on this feed: {corpus_sweep['unresolvedLegs']}/{total} ({pct:.2f}%) - "
                  f"tracked here, not gated at the 0.5% floor (Stage 5's own still-open finding)")
        else:
            print("  [skip] no real feed data / meta.json found on disk")

        print("\nFielding-reconciliation audit, Stage 6 - debug panel smoke test: sceneDebugHtml")
        print("and fielderTokensHtml render without throwing on a real 2-leg DP, and the panel")
        print("shows the per-leg content Stage 2/3/4 added (not just the old final-leg-only view):")
        if meta_fp.exists() and real_plays:
            dp_m = next((p for p in real_plays if p["result"] in ("DP", "DP21", "DP31")
                         and (p.get("outs_after", 0) - p.get("outs_before", 0)) >= 2), None)
            if dp_m:
                panel = page.evaluate(
                    """(a) => {
                        var flight = KMFlight.resolvePlayFlight(a.m);
                        if (!flight) return null;
                        var moves = KMFlight.deriveRunnerMoves(
                            String(a.m.obc_before || "000"), String(a.m.obc_after || "000"), a.m.runs || 0);
                        var debugHtml = KMFlight.sceneDebugHtml(a.m, flight);
                        var tokensHtml = KMFlight.fielderTokensHtml(a.m, flight, moves, 0);
                        return { debugHtml: debugHtml, tokensHtml: tokensHtml };
                    }""",
                    {"m": dp_m},
                )
                check(f"sceneDebugHtml rendered real HTML for {dp_m['moment_id']} ({dp_m['result']}) with no exception",
                      bool(panel and panel["debugHtml"] and len(panel["debugHtml"]) > 0), True)
                check("the panel shows a per-leg 'Reconciliation verdict (leg N)' block (Stage 2, section 2.6), not just one",
                      panel["debugHtml"].count("Reconciliation verdict (leg") >= 1, True)
                check("the knobs table carries the new 'leg' column header (Stage 2, section 2.6)",
                      "<th>leg</th>" in panel["debugHtml"], True)
                check("fielderTokensHtml (which runs reconcileCoverage internally) rendered with no exception",
                      bool(panel and panel["tokensHtml"] and len(panel["tokensHtml"]) > 0), True)
            else:
                print("  [skip] no 2-leg DP moment found in the live feed")
        else:
            print("  [skip] no real feed data / meta.json found on disk")

        print("\nFielding-reconciliation audit, real-play report fix (round 2) -")
        print("returnsAfterThrow: a fielder who fields the ball, throws it away, then")
        print("must return to cover a base later in the same chain (KC@RC S13/Sess4,")
        print("Jazzy Jeff batting, moment 130422032 - 1B fields, throws to 2B for the")
        print("force, then must get back to cover 1B himself for the relay). Verifies")
        print("both real-play reports: the token actually moves for the return trip")
        print("(chainMoverPlan's own seen[] dedup used to silently drop it entirely),")
        print("and it doesn't start moving until AFTER its own throw has released")
        print("(the dwell - without it, the token visibly starts toward the bag while")
        print("still supposedly fielding/throwing):")
        # This moment lives in key_moments.json (the curated feed), not the
        # plays_*.json files real_plays/real_tables were built from above -
        # loaded directly here; real_tables (docs/data/meta.json) still
        # applies, same current season.
        km_fp = pathlib.Path("docs/data/key_moments.json")
        return_cover_m = None
        if km_fp.exists():
            km_moments = json.loads(km_fp.read_text(encoding="utf-8"))
            return_cover_m = next((p for p in km_moments if p["moment_id"] == "130422032"), None)
        if return_cover_m:
            rc = page.evaluate(
                """(m) => {
                    var flight = KMFlight.resolvePlayFlight(m);
                    var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                    var schedule = KMFlight.throwSchedule(m, moves, flight);
                    var plan = KMFlight.chainMoverPlan(m, flight, moves);
                    var coverage = KMFlight.reconcileCoverage(m, flight, plan, schedule);
                    var entry = plan.filter(function (e) { return e.pos === "1B"; })[0];
                    var legs = entry ? KMFlight.returnCoverLegTiming(m, entry, schedule, flight) : null;
                    return {
                        entry: entry, legs: legs, coverageAdjustments: coverage.adjustments,
                        throwLeg0EndMs: schedule[0].endMs, receiveLegEndMs: schedule[schedule.length - 1].endMs,
                    };
                }""",
                return_cover_m,
            )
            check("1B gets a real plan entry flagged returnsAfterThrow (chainMoverPlan's own seen[] dedup no longer silently drops it)",
                  bool(rc["entry"] and rc["entry"].get("returnsAfterThrow")), True)
            check("1B's plan entry has 2 legs (field, then cover) - chainMoverPlan's own raw build",
                  len(rc["entry"]["legs"]) if rc["entry"] else 0, 2)
            check("returnCoverLegTiming resolves 3 legs (field, dwell, cover)",
                  len(rc["legs"]) if rc["legs"] else 0, 3)
            if rc["legs"]:
                leg1, leg2, leg3 = rc["legs"]
                check("leg 2 (the dwell) has a real, positive duration - not a zero-time pass-through",
                      leg2["durMs"] > 0, True)
                check("the dwell ends exactly when 1B's own first throw actually releases (schedule[0].endMs) - "
                      "he never starts toward the cover base before releasing his own throw",
                      rc["entry"]["startMs"] + leg1["durMs"] + leg2["durMs"], rc["throwLeg0EndMs"], tol=1)
                cum_arrival = rc["entry"]["startMs"] + leg1["durMs"] + leg2["durMs"] + leg3["durMs"]
                check("1B's own token arrives at the bag by the time the relay throw actually lands (invariant #4)",
                      cum_arrival <= rc["receiveLegEndMs"] + 0.5, True)
            check("reconcileCoverage found a real deficit here and recorded it honestly (coverCompress) - "
                  "not silently missed because chainMoverPlan's own pre-dwell arrivalMs looked comfortably early",
                  any(a["knob"] == "coverCompress" for a in rc["coverageAdjustments"]), True)
        else:
            print("  [skip] moment 130422032 not found in the live feed")

        # Alex's report: a non-ball-toucher infielder covering a base on a
        # ground-archetype play (1B covering first while SS/2B/3B fields the
        # grounder) broke way too late - the old ballPassesDepthMs gate is a
        # pure radial-distance check with no sense of direction, so a
        # grounder hit clear across the infield still eventually "passes"
        # 1B's own depth (capped at fieldedMs), meaning 1B often didn't
        # start moving until the OTHER fielder had ALREADY fielded the ball.
        # Real moment 130424049 (KC@RC S13/Sess4, Barney Turbo batting - SS
        # fields a routine grounder, 1B covers first unassisted): the old
        # gate would have held 1B at the plate until ~844ms in (ball fielded
        # at ~1253ms) - real baseball has 1B breaking the instant the ball's
        # read off the bat as going to SS, with no cost to arriving early.
        print("\nAlex's report fix: a non-ball-toucher covering infielder (1B covering first while")
        print("SS/2B/3B fields the grounder) breaks immediately, not gated on ballPassesDepthMs -")
        print("moment 130424049 (SS fields, 1B covers first):")
        cover_break_m = next((p for p in km_moments if p["moment_id"] == "130424049"), None) if km_fp.exists() else None
        if cover_break_m:
            cb = page.evaluate(
                """(m) => {
                    var flight = KMFlight.resolvePlayFlight(m);
                    var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                    var plan = KMFlight.chainMoverPlan(m, flight, moves);
                    var entry = plan.filter(function (e) { return e.pos === "1B"; })[0];
                    var oldGateWouldBe = entry ? KMFlight.ballPassesDepthMs(flight, Math.hypot(entry.anchor.x, entry.anchor.y)) : null;
                    return {
                        fielder: flight.fielder, entryStartMs: entry ? entry.startMs : null,
                        fieldedMs: KMFlight.fieldedMs(flight), oldGateWouldBe: oldGateWouldBe,
                    };
                }""",
                cover_break_m,
            )
            print(f"  fielder={cb['fielder']} 1B startMs={cb['entryStartMs']} "
                  f"fieldedMs={cb['fieldedMs']:.0f} old-gate-would-be={cb['oldGateWouldBe']}")
            check("1B is a covering (non-ball-toucher) entry on this play", cb["fielder"] != "1B", True)
            check("1B breaks on the flat INFIELD_COVER_BREAK_MS reaction beat",
                  cb["entryStartMs"], infield_cover_break_ms, tol=0.5)
            check("1B now breaks well before the ball is even fielded elsewhere (the reported bug's own symptom)",
                  cb["entryStartMs"] < cb["fieldedMs"] - 200, True)
            check("the old radial depth gate would have held 1B far later than the new flat reaction beat "
                  "(confirms this play actually exercises the fixed code path, not a no-op)",
                  cb["oldGateWouldBe"] > cb["entryStartMs"] + 200, True)
        else:
            print("  [skip] moment 130424049 not found in the live feed")

        # Alex's ask: INFIELD_GLOVE_REACH_FT widened how often applyChargeEase's
        # own paceScale (bottoming out at charge.min) still isn't enough to
        # make a real infield-single/bunt-hit play look plausible - before this,
        # the reconciler fell straight through to leaning on slowThrow/
        # hustleRunner/holdRelease downstream, which for several real corpus
        # plays meant a multi-second holdRelease (the fielder just standing on
        # the ball). The retreat step (capBearingAwayFromFt + depth pushed
        # toward the dirt edge, mirroring applyGrounderDeepSetup's own leeway
        # in reverse) gives the reconciler a more honest lever first. Real
        # moment 130117037 (current feed): before the retreat step existed,
        # this play needed a 1741ms holdRelease; verify the retreat now fires
        # and that holdRelease drops sharply as a result.
        print("\nAlex's ask: applyChargeEase's own retreat step (position pushed away from")
        print("the ball when charge.min pace alone isn't enough) - moment 130117037:")
        retreat_m = next((p for p in real_plays if str(p.get("moment_id") or p.get("play_num")) == "130117037"), None)
        if retreat_m:
            rt = page.evaluate(
                """(m) => {
                    var flight = KMFlight.resolvePlayFlight(m);
                    var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                    var schedule = KMFlight.throwSchedule(m, moves, flight);
                    var maxAlongFt = KMFlight.dirtEdgeFt(flight.angle) - flight.distance;
                    var fieldedAlongFt = flight.fieldedDistFt - flight.distance;
                    return {
                        paceScale: flight.fieldingAdjust.paceScale,
                        chargeRetreat: !!flight.fieldingAdjust.chargeRetreat,
                        depthPct: flight.fieldingAdjust.depthPct,
                        adjustments: schedule.adjustments,
                        fieldedAlongFt: fieldedAlongFt, maxAlongFt: maxAlongFt,
                    };
                }""",
                retreat_m,
            )
            hold_ms = next((a["ms"] for a in rt["adjustments"] if a["knob"] == "holdRelease"), 0)
            print(f"  paceScale={rt['paceScale']} chargeRetreat={rt['chargeRetreat']} depthPct={rt['depthPct']} "
                  f"holdRelease={hold_ms} fieldedAlongFt={rt['fieldedAlongFt']:.1f} maxAlongFt={rt['maxAlongFt']:.1f}")
            charge_min_now = page.evaluate("KMFlight.FIELDER_PACE_SCALE.charge.min")
            check("charge pace is maxed at charge.min before the retreat step even considers firing",
                  rt["paceScale"], charge_min_now, tol=1e-9)
            check("the retreat step actually fired (charge.min alone wasn't enough)",
                  rt["chargeRetreat"], True)
            check("depthPct lands in [0, 1] - a real bounded retreat, not an unbounded shove",
                  0 <= rt["depthPct"] <= 1, True)
            check("holdRelease dropped from the pre-retreat 1741ms this exact play used to need",
                  hold_ms < 1741, True)
            check("Alex's ask: the retreated fielder still genuinely fields the ball short of the dirt "
                  "edge - never retreated so far the ball would just cap there untouched",
                  rt["fieldedAlongFt"] < rt["maxAlongFt"] - 0.5, True)
        else:
            print("  [skip] moment 130117037 not found in the live feed")

        print("\ncapBearingAwayFromFt is the exact mirror of capBearingTowardFt - same clamped")
        print("step magnitude, opposite rotation direction:")
        mirror = page.evaluate(
            """() => {
                var anchor = {x: 30, y: 130};
                var target = {x: 60, y: 140};
                var toward = KMFlight.capBearingTowardFt(anchor, target, 3);
                var away = KMFlight.capBearingAwayFromFt(anchor, target, 3);
                function deg(p) { return 45 + Math.atan2(p.x, p.y) * 180 / Math.PI; }
                var anchorDeg = deg(anchor);
                return {
                    towardStepDeg: deg(toward) - anchorDeg,
                    awayStepDeg: deg(away) - anchorDeg,
                    towardDepth: Math.hypot(toward.x, toward.y),
                    awayDepth: Math.hypot(away.x, away.y),
                    anchorDepth: Math.hypot(anchor.x, anchor.y),
                };
            }"""
        )
        check("capBearingAwayFromFt steps the same magnitude as capBearingTowardFt, opposite sign",
              mirror["awayStepDeg"], -mirror["towardStepDeg"], tol=1e-6)
        check("capBearingAwayFromFt never changes depth - bearing only",
              mirror["awayDepth"], mirror["anchorDepth"], tol=1e-9)

        # Alex's report: an infield_single up the middle (Ornn Mistborn, GHG@ACP
        # S13/Sess4, bot 1, moment 130417007) rested at the exact dirt-edge
        # cap with the fielder still parked at their raw default anchor - the
        # leeway/sprint-retry block above used to read flight.archetype ===
        # "grounder" only, the one place left that hadn't been widened
        # alongside maxAlongFt/applyChargeEase when bunt/infield_single were
        # unified into the same "no genuine crossing" race. Verify the widened
        # gate now pulls the fielder's anchor toward the ball for this exact
        # archetype too.
        print("\nAlex's report fix: infield_single/bunt now get the same leeway/sprint-retry")
        print("treatment as grounder when the honest race never finds a genuine crossing -")
        print("moment 130417007 (GHG@ACP S13/Sess4, Ornn Mistborn, IF1B up the middle):")
        plays04_fp = pathlib.Path("docs/data/plays_04.json")
        if plays04_fp.exists():
            plays04 = json.loads(plays04_fp.read_text(encoding="utf-8"))
            ornn_m = next((p for p in plays04 if p["moment_id"] == "130417007"), None)
            if ornn_m:
                og = page.evaluate(
                    """(m) => {
                        var flight = KMFlight.resolvePlayFlight(m);
                        var maxAlongFt = KMFlight.dirtEdgeFt(flight.angle) - flight.distance;
                        var fieldedAlongFt = flight.fieldedDistFt - flight.distance;
                        var defaultAnchorDepth = Math.hypot(
                            KMFlight.FIELDER_ANCHORS_FT[flight.fielder].x,
                            KMFlight.FIELDER_ANCHORS_FT[flight.fielder].y);
                        var actualAnchorDepth = Math.hypot(
                            flight.fieldingAdjust.anchorFt.x, flight.fieldingAdjust.anchorFt.y);
                        var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                        var plan = KMFlight.chainMoverPlan(m, flight, moves);
                        var entry = plan.filter(function (e) { return e.pos === flight.fielder; })[0];
                        return {
                            archetype: flight.archetype, fielder: flight.fielder,
                            cappedFallback: flight.fieldingAdjust.cappedFallback,
                            fieldedAlongFt: fieldedAlongFt, maxAlongFt: maxAlongFt,
                            defaultAnchorDepth: defaultAnchorDepth, actualAnchorDepth: actualAnchorDepth,
                            entryArrivalMs: entry ? entry.arrivalMs : null,
                            entryDeadlineMs: entry ? entry.deadlineMs : null,
                        };
                    }""",
                    ornn_m,
                )
                print(f"  archetype={og['archetype']} fielder={og['fielder']} cappedFallback={og['cappedFallback']} "
                      f"defaultAnchorDepth={og['defaultAnchorDepth']:.1f} actualAnchorDepth={og['actualAnchorDepth']:.1f} "
                      f"entryArrivalMs={og['entryArrivalMs']} entryDeadlineMs={og['entryDeadlineMs']}")
                check("this play is the infield_single archetype (not plain grounder) - the exact "
                      "archetype the old gate excluded", og["archetype"], "infield_single")
                check("the leeway fired (cappedFallback true) - infield_single now goes through the "
                      "same block a comparable groundout already did", og["cappedFallback"], True)
                check("the fielder's own anchor moved meaningfully closer to the ball's actual line "
                      "than their raw default position", og["actualAnchorDepth"] > og["defaultAnchorDepth"] + 10, True)
                # Unlike applyChargeEase's own retreat step (which must never
                # push a fielder so far the ball caps untouched), this leeway
                # runs precisely BECAUSE the ball is already resting at the
                # dirt edge (isCappedFallback's own definition) - it never
                # moves the ball's own rest point, only pulls the fielder's
                # anchor closer to that already-fixed point. Resting exactly
                # AT maxAlongFt here is correct, not a bug.
                check("fielded point never rolls past the dirt edge (it's expected to sit exactly on it here)",
                      og["fieldedAlongFt"] <= og["maxAlongFt"] + 0.5, True)
                check("the glove's own computed arrival is comfortably before the ball actually stops rolling",
                      og["entryArrivalMs"] < og["entryDeadlineMs"] - 50, True)
            else:
                print("  [skip] moment 130417007 not found in docs/data/plays_04.json")
        else:
            print("  [skip] docs/data/plays_04.json not found on disk")

        # Alex's report: on this same DP31 (Tor Tilla, POR@RLY S13/Sess4, top 2,
        # moment 130418010 - 3B fields deep, touches 3rd for the first force
        # out, throws to 1st for the second), the batter visibly beat the
        # throw to first "by quite a bit." throwSchedule's own reconciliation
        # DID retarget the batter (runnerLateJump/stretchRunner, keyed "BATTER"
        # in schedule.adjustments) - but sceneFieldHtml's own !batterReached
        # fallback (used whenever resolveRunnerMoves's raw data, preferred over
        # deriveRunnerMoves, never lists an explicit BATTER move - exactly this
        # play's shape) rendered the batter's out-to-first sprint via a plain
        # runnerLegMs(m,"BATTER",1) call that never read that adjustment at
        # all. The token reached the base early (69.9% through the animation)
        # and idled there until the out actually registered (89.3%) - visibly
        # "beats the throw and waits." Verify the retargeting now reaches this
        # render path: no more early-arrival plateau.
        print("\nAlex's report fix: the batter's own !batterReached render path now folds in")
        print("runnerAdjMsByWho too, not just the main moves.map() loop - moment 130418010")
        print("(Tor Tilla DP31, 3B fields deep + touches 3rd + throws to 1st):")
        tortilla_m = next((p for p in plays04 if p.get("moment_id") == "130418010"), None) if plays04_fp.exists() else None
        if tortilla_m:
            scene_html = page.evaluate("(m) => KMFlight.playSceneHtml({play: m})", tortilla_m)
            batter_match = re.search(r'class="rn out-to-first batter"[^>]*animation-name:(rnOut\d+)', scene_html)
            check("the batter's own out-to-first token exists in the rendered scene", bool(batter_match), True)
            if batter_match:
                anim_name = batter_match.group(1)
                kf_match = re.search(r"@keyframes " + re.escape(anim_name) + r" \{(.*?)\}\s*</style>", scene_html, re.S)
                check(f"found the {anim_name} keyframe block", bool(kf_match), True)
                if kf_match:
                    stops = re.findall(r"translate\(([\d.]+)px,([\d.]+)px\)", kf_match.group(1))
                    print(f"  {anim_name} stops: {stops}")
                    # The old bug's own tell: two CONSECUTIVE stops landing on
                    # the exact same point (arrived early at frac X, held
                    # there until the real out at frac Y > X). A healthy
                    # retargeted run goes straight from the box to the base
                    # to the dugout - no repeated point in a row.
                    has_dup_hold = any(stops[i] == stops[i + 1] for i in range(len(stops) - 1))
                    check("no early-arrival hold (no two consecutive keyframe stops at the same point) - "
                          "the batter no longer reaches the base early and waits for the out",
                          has_dup_hold, False)
        else:
            print("  [skip] moment 130418010 not found in docs/data/plays_04.json")

        # Alex's report (same play, same session): the ball visibly ran ahead
        # of the 3B glove during the carry to third - "doesn't visually
        # appear in the glove... the ball is ahead of the glove." Root cause:
        # unassistedLegTiming's own leg1 (the fielding portion) was capped at
        # the HONEST fieldedMs(flight), but the out-reconciler's own
        # quickRelease/sprintCarry had already pulled this leg's own
        # schedule.startMs meaningfully earlier (a legitimate, non-edge-case
        # outcome for a tight force out) - the ball marker (throwHtml, which
        # reads schedule.startMs directly) started moving toward third before
        # the glove's own render had even finished reaching the pickup point.
        # Verify the glove now reaches the pickup point, and third base, at
        # the exact same absolute moments the ball marker does.
        print("\nAlex's report fix: the 3B glove's own carry-to-third timing now exactly")
        print("matches the ball marker's own (unassistedLegTiming capped at the reconciled")
        print("schedule.startMs, not the honest fieldedMs) - moment 130418010:")
        if tortilla_m:
            sync_check = page.evaluate(
                """(m) => {
                    var flight = KMFlight.resolvePlayFlight(m);
                    var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                    var schedule = KMFlight.throwSchedule(m, moves, flight);
                    var plan = KMFlight.chainMoverPlan(m, flight, moves);
                    var entry = plan.filter(function (e) { return e.pos === flight.fielder; })[0];
                    var legs = KMFlight.unassistedLegTiming(m, entry, schedule, flight);
                    return {
                        entryStartMs: entry.startMs, legs: legs,
                        scheduleStartMs: schedule[0].startMs, scheduleEndMs: schedule[0].endMs,
                    };
                }""",
                tortilla_m,
            )
            leg1, leg2, leg3 = sync_check["legs"]
            gloveReachesPickupMs = sync_check["entryStartMs"] + leg1["durMs"] + leg2["durMs"]
            gloveArrivesMs = gloveReachesPickupMs + leg3["durMs"]
            print(f"  glove reaches pickup at {gloveReachesPickupMs}ms (ball departs at {sync_check['scheduleStartMs']}ms), "
                  f"glove arrives at base at {gloveArrivesMs}ms (ball arrives at {sync_check['scheduleEndMs']}ms)")
            check("the glove reaches the pickup point at the exact moment the ball marker departs for third",
                  gloveReachesPickupMs, sync_check["scheduleStartMs"], tol=1)
            check("the glove arrives at third base at the exact moment the ball marker does",
                  gloveArrivesMs, sync_check["scheduleEndMs"], tol=1)

        # Alex's report (same play): "the ball teleports a bit into the
        # glove." Root cause: ballArcHtml's own handoffMs only ever drove the
        # fade-out timing - the ball's own keyframe SAMPLES (and the CSS
        # --dur built from them) still ran the full honest roll
        # (fieldedMs(flight)), regardless of whether a following throw's own
        # startMs (handoffMs) landed earlier. Invisible as long as a handoff
        # could only ever land AFTER the honest roll finished - not true once
        # unassistedLegTiming's own fix (this same report) lets an out-
        # reconciler's quickRelease pull a carry's departure earlier than
        # fieldedMs. When it does, the rolling ball used to fade out mid-roll
        # while a separate throw-ball simultaneously appeared at the TRUE
        # rest point ahead of it. Verify the roll now compresses (ground
        # phase only) to finish exactly at the handoff moment instead.
        print("\nAlex's report fix: the rolling ball's own animation compresses to finish")
        print("exactly at the handoff moment, not mid-roll - moment 130418010:")
        if tortilla_m:
            teleport_check = page.evaluate(
                """(m) => {
                    var flight = KMFlight.resolvePlayFlight(m);
                    var moves = KMFlight.deriveRunnerMoves(String(m.obc_before || "000"), String(m.obc_after || "000"), m.runs || 0);
                    var throwSched = KMFlight.throwSchedule(m, moves, flight);
                    var handoffMs = throwSched.length ? Math.min.apply(null, throwSched.map(function (t) { return t.startMs; })) : null;
                    var arc = KMFlight.ballArcHtml(m, flight, handoffMs);
                    return {
                        handoffMs: handoffMs, fieldedMs: KMFlight.fieldedMs(flight),
                        arcTotalMs: arc.totalS * 1000, arcEndPt: arc.endPt,
                    };
                }""",
                tortilla_m,
            )
            print(f"  handoffMs={teleport_check['handoffMs']} honestFieldedMs={teleport_check['fieldedMs']} "
                  f"arc.totalS(ms)={teleport_check['arcTotalMs']}")
            check("the honest fieldedMs is genuinely later than the handoff here (precondition - "
                  "confirms this play actually exercises the compression, not a no-op)",
                  teleport_check["fieldedMs"] > teleport_check["handoffMs"] + 50, True)
            check("the ball's own animation duration compresses to match the handoff moment exactly, "
                  "not the honest (later) fieldedMs",
                  teleport_check["arcTotalMs"], teleport_check["handoffMs"], tol=1)

        # Alex's ask: a runner doubled off a caught line drive (LODP/LOTP)
        # needs to break at contact, reverse exactly at the real catch
        # moment, and the fielding side must be genuinely reconciled against
        # that real retreat-arrival - not the three gaps found investigating
        # this: mvDelay used outDelay (assumes the ball's already down,
        # holding the runner motionless until roughly the catch itself),
        # the reversal was a fixed cosmetic 75%-of-duration breakpoint with
        # no connection to ballTravelMs, and the schedule leg was never
        # raced against the runner at all (runnerArrivalMs stayed null -
        # forcedOutRunnerArrivalMs's own ordinal math rejects a same-base
        # "advance" outright). Real moment 130121014 (Anna De Neko, LODP -
        # 2B breaks toward 3rd on the liner, 2B fielder catches it and
        # throws to SS covering 2nd for the second out).
        print("\nAlex's ask: LODP/LOTP retreat runners now break at contact, reverse at the")
        print("real catch moment, and get genuinely reconciled against the fielding side -")
        print("moment 130121014 (Anna De Neko, LODP):")
        plays01_fp = pathlib.Path("docs/data/plays_01.json")
        if plays01_fp.exists():
            plays01 = json.loads(plays01_fp.read_text(encoding="utf-8"))
            lodp_m = next((p for p in plays01 if p.get("moment_id") == "130121014"), None)
            if lodp_m:
                lr = page.evaluate(
                    """(m) => {
                        var flight = KMFlight.resolvePlayFlight(m);
                        var moves = KMFlight.resolveRunnerMoves(m);
                        var mv = KMFlight.runnerForOutTarget(m, moves, "2B");
                        var outDist = KMFlight.retreatOutDistFt(m, flight, mv);
                        var arrival = KMFlight.retreatRunnerArrivalMs(m, flight, moves, "2B");
                        var schedule = KMFlight.throwSchedule(m, moves, flight);
                        var margin = KMFlight.MARGIN_POLICY.forceOut.minMs;
                        return {
                            mv: mv, outDist: outDist, arrival: arrival,
                            ballTravelMs: KMFlight.ballTravelMs(flight),
                            leg0EndMs: schedule[0].endMs, marginFloor: margin,
                        };
                    }""",
                    lodp_m,
                )
                print(f"  outDist={lr['outDist']:.1f}ft ballTravelMs={lr['ballTravelMs']:.0f} "
                      f"arrival={lr['arrival']:.0f} leg0EndMs={lr['leg0EndMs']:.0f}")
                check("runnerForOutTarget resolves the real retreat move (mv.retreat true)",
                      lr["mv"]["retreat"], True)
                check("the runner breaks for real distance during the air time - more than a token "
                      "few feet, less than the full 90ft to the next base",
                      0 < lr["outDist"] < 90, True)
                check("retreatRunnerArrivalMs now returns a real number (this leg used to skip "
                      "reconciliation entirely - runnerArrivalMs stayed null)",
                      lr["arrival"] is not None, True)
                check("the fielding side (ball + covering fielder) genuinely beats the runner back "
                      "to the base by at least the loosest permissible margin",
                      lr["arrival"] - lr["leg0EndMs"] >= lr["marginFloor"] - 0.5, True)

                scene_html = page.evaluate("(m) => KMFlight.playSceneHtml({play: m})", lodp_m)
                retreat_match = re.search(r'class="rn out-retreat"[^>]*style="([^"]*)"', scene_html)
                check("the retreat runner's own token renders in the scene", bool(retreat_match), True)
                if retreat_match:
                    vars_str = retreat_match.group(1)
                    rdelay_match = re.search(r"--rdelay:([\d.]+)ms", vars_str)
                    check("the runner's own render-start (rdelay minus the play's shared seqDelay) is "
                          "a contact-based reaction beat, not the old outDelay (which would be well "
                          "over 2000ms for this play)",
                          float(rdelay_match.group(1)) < 1000 if rdelay_match else None, True)
            else:
                print("  [skip] moment 130121014 not found in docs/data/plays_01.json")
        else:
            print("  [skip] docs/data/plays_01.json not found on disk")

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

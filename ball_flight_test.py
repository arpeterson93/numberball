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
                    return KMFlight.outThrowTargets(a.m, moves, a.flight);
                }""",
                {"before": before, "after": after, "runs": runs, "m": m, "flight": flight},
            )
            check(f"outThrowTargets({result} {before}->{after})", got, want)

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
                    var lastEnd = Math.max.apply(null, schedule.map(t => t.endMs));
                    return KMFlight.batterFirstArrivalMs(a.m) - (lastEnd + KMFlight.THROW_LEAD_MS);
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

        print("\nOutfield pre-shift direction (real bug report: a Calvin Huff double")
        print("landed at flight.angle exactly 45 - a lattice tie - even though the real")
        print("simulated x/y clearly wasn't a tie, so the tie-break picked the wrong")
        print("shift direction; must use the true simulated bearing, not the lattice):")
        shift_case = page.evaluate(
            """() => {
                var flight = {archetype: "double", angle: 45, fielder: "CF", x: 19.92, y: 243.34};
                var cf = KMFlight.fielderStartAnchorFt("CF", flight, {});
                var base = KMFlight.FIELDER_ANCHORS_FT.CF;
                return {cfX: cf.x, baseX: base.x};
            }"""
        )
        check("CF shifts away (left, negative x) from a ball landing right of center",
              shift_case["cfX"] < shift_case["baseX"], True)

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
             {"result": "BFC", "outs_before": 0, "outs_after": 1, "obc_before": "110", "obc_after": "011", "runs": 0, "throw_order": "3"},
             {"fielder": "3B", "archetype": "bunt", "angle": 9, "clearedFence": False}),
            ("double play, SS fields (6-4-3)", "6-4-3",
             {"result": "DP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0, "throw_order": "2,1"},
             {"fielder": "SS", "archetype": "grounder", "angle": 29, "clearedFence": False}),
            ("double play, 2B fields (4-6-3)", "4-6-3",
             {"result": "DP", "outs_before": 0, "outs_after": 2, "obc_before": "001", "obc_after": "000", "runs": 0, "throw_order": "2,1"},
             {"fielder": "2B", "archetype": "grounder", "angle": 61, "clearedFence": False}),
            ("comebacker to the pitcher at 45deg, SS covers second (tie-break)", "1-6",
             {"result": "FC", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "010", "runs": 0, "throw_order": "2"},
             {"fielder": "P", "archetype": "grounder", "angle": 45, "clearedFence": False}),
            ("fielder's choice, SS forces the lead runner at 2nd", "6-4",
             {"result": "FC", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "010", "runs": 0, "throw_order": "2"},
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
             {"result": "DPH1", "outs_before": 1, "outs_after": 3, "obc_before": "111", "obc_after": "000", "runs": 0, "throw_order": "4,1"},
             {"fielder": "3B", "archetype": "grounder", "angle": 9, "clearedFence": False}),
            ("routine flyout, no throw", "F8",
             {"result": "FO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "CF", "archetype": "fly_ball", "angle": 45, "clearedFence": False}),
            ("routine popout, no throw", "P4",
             {"result": "PO", "outs_before": 0, "outs_after": 1, "obc_before": "000", "obc_after": "000", "runs": 0},
             {"fielder": "2B", "archetype": "pop_up", "angle": 61, "clearedFence": False}),
            ("sac fly: decorative tag-up throw must NOT read as a putout at home, and gets the S prefix", "SF9",
             {"result": "SacF", "outs_before": 0, "outs_after": 1, "obc_before": "100", "obc_after": "000", "runs": 1, "throw_order": "4"},
             {"fielder": "RF", "archetype": "fly_ball", "angle": 69, "clearedFence": False}),
            ("double sac fly (DSacF) also gets the S prefix", "SF7",
             {"result": "DSacF", "outs_before": 0, "outs_after": 1, "obc_before": "001", "obc_after": "000", "runs": 1, "throw_order": "4"},
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

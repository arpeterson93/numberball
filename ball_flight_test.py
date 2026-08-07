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
        page.wait_for_function("window.KMFlight != null")

        print("Boundary assertions:")
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

        cases = [
            {
                # la: q = 1 - clamp((5-2)/(21-2)) = 16/19 = 0.8421 (close to the
                # band's low end, i.e. well-timed contact); onTop (swing > pitch,
                # all digits) -> LA = 28 - (1-q)*(28-25) = 27.53, close to but
                # not quite laIdeal since this diff isn't AT the band's floor.
                "label": "HR pulled RHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "R", "diff": 5},
                "want": {"la": 27.53, "angle": 5.00, "ev": 112.32, "distance": 412.1, "x": -264.9, "y": 315.7, "hangMs": 4728.73},
            },
            {
                "label": "HR same numbers LHH",
                "play": {"result": "HR", "pitch": 407, "swing": 412, "batter_hand": "L", "diff": 5},
                "want": {"la": 27.53, "angle": 85.00, "x": 264.9},
            },
            {
                # la: q clamps to 0 (diff 481 is past the GO band's own hi=470),
                # so LA bottoms out at grounder's laMin (-15) regardless of
                # direction - the "least ideal" end of the formula's range.
                "label": "Groundout RHH",
                "play": {"result": "GO", "pitch": 150, "swing": 631, "batter_hand": "R", "diff": 481},
                "want": {"la": -15.0, "angle": 53.00, "ev": 70.00, "distance": 60.0, "isGrounder": True, "hangMs": None},
            },
            {
                # Raw (pre-clamp) distance would be 380.0 ft per the plan's Stage 3a
                # formula in isolation, but Stage 4d requires clampToFence to run
                # inside flightParams (non-HR may never clear the fence), and this
                # FO pulled toward the line at 380ft raw would clear the uniform
                # 375ft wall. Expected values below are post-clamp:
                # min(380, 375-12)=363, with x/y recomputed from D=363 at the same
                # 29 degree angle.
                # la: diff 48 is below the FO band's own lo=55, so q clamps to 1 -
                # LA lands exactly on fly_ball's laIdeal (28), independent of
                # direction (the (1-q) deviation term is zero at q=1).
                "label": "Flyout RHH",
                "play": {"result": "FO", "pitch": 220, "swing": 268, "batter_hand": "R", "diff": 48},
                "want": {"la": 28.0, "angle": 29.00, "ev": 98.00, "distance": 363.0, "x": -100.06, "y": 348.94},
            },
            {
                # Recomputed for C5's recalibrated single archetype (was
                # -5,10,...,60,160 - la/distance/isGrounder below reflect the
                # new 6,20,...,130,230 band; angle/ev are archetype-independent
                # and unchanged from the original worked example.
                # la: q = 1 - clamp((87-20)/(150-20)) = 1 - 67/130 = 0.4846; swing
                # (801) < pitch (888) on the full 1000-wheel -> "below" the pitch
                # (uppercut) -> LA = 12 + (1-q)*(20-12) = 16.12.
                "label": "Single LHH",
                "play": {"result": "1B", "pitch": 888, "swing": 801, "batter_hand": "L", "diff": 87},
                "want": {"la": 16.12, "angle": 21.00, "ev": 86.15, "distance": 178.46, "isGrounder": False},
            },
        ]

        print("\nWorked examples:")
        for c in cases:
            print(f" {c['label']}:")
            r = page.evaluate("(a) => KMFlight.flightParams(a.play, a.tables)", {"play": c["play"], "tables": tables})
            for key, want in c["want"].items():
                got = r.get(key.replace("distance", "distance")) if key != "distance" else r["distance"]
                got = r[key]
                tol = 0.005 if key in ("la", "angle") else (2.0 if key == "hangMs" else 0.15)
                if want is None or isinstance(want, bool):
                    check(f"{c['label']}.{key}", got, want, tol=0)
                else:
                    check(f"{c['label']}.{key}", got, want, tol=tol)

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
            ("SB2", "001", "010", {"base": "2B", "caught": False}),
            ("SB3", "010", "100", {"base": "3B", "caught": False}),
            ("CS2", "001", "000", {"base": "2B", "caught": True}),
            ("CS", "010", "000", {"base": "3B", "caught": True}),
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
        for result, before, after, runs, outs_before, outs_after, archetype, want in throw_cases:
            if archetype != "grounder" or not want:
                continue
            m = {"result": result, "outs_before": outs_before, "outs_after": outs_after}
            flight = {"archetype": archetype, "clearedFence": False, "isGrounder": True, "hangMs": None}
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
            flight = {"archetype": archetype, "clearedFence": False, "hangMs": None}
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

        print("\nGrounder depth sanity (regression guard): Statcast's hit_distance_sc")
        print("measures a ground ball's first bounce, not its fielding depth - a naive")
        print("re-derivation would collapse depthMax back down near that bounce point")
        print("(observed ~78ft for GO) instead of a realistic infield depth (INFIELDER_")
        print("DEPTH_FT tops out at 147ft for SS/2B):")
        if meta_fp.exists():
            bands = json.loads(meta_fp.read_text(encoding="utf-8"))["flight"]["bands"]
            GROUND_TOUCHING_MIN_DEPTH_MAX = {
                "GO": 100, "GORA": 100, "FC": 100, "FC3rd": 100, "FCH": 100,
                "DP": 100, "DP21": 100, "DP31": 100, "DPH1": 100, "DPRun": 100, "TP": 100,
                "IF1B": 60,
            }
            for result, floor in GROUND_TOUCHING_MIN_DEPTH_MAX.items():
                band = bands.get(result)
                if not band:
                    continue
                check(f"{result} depthMax >= {floor}ft (real infield depth, not bounce point)",
                      band["depthMax"] >= floor, True)
        else:
            print("  [skip] no meta.json found on disk")

        print("\nGrounder rollout sanity (regression guard): a flat ROLLOUT_FT max can't")
        print("bridge the gap between a modest landing point and a real infield depth -")
        print("a moderate-contact GO assigned to shortstop (147ft, the deepest infield")
        print("position) must roll out to noticeably more than half that depth, not just")
        print("its own short landing point plus a small constant:")
        moderate_go_flight = page.evaluate(
            """() => {
                var flight = {ev: 74.8, la: -15, distance: 87, angle: 29, archetype: 'grounder'};
                var m = {outs_before: 0, outs_after: 1};
                var roll = KMFlight.groundBallRolloutFt(m, flight);
                return {roll: roll, total: flight.distance + roll};
            }"""
        )
        # Old flat-ROLLOUT_FT formula gave this exact contact quality only
        # 0.463*34 =~ 15.7ft of roll (total ~102.8ft) - well short of a real
        # shortstop's 147ft. The gap-scaled fix should meaningfully beat that.
        check("moderate-contact GO (SS-assigned) rolls out further than the old flat-max formula would",
              moderate_go_flight["roll"] > 20, True)
        check("moderate-contact GO (SS-assigned) total distance closes meaningfully more of the gap to SS's real 147ft depth",
              moderate_go_flight["total"] > 110, True)

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
        if real_singles:
            dirt_count = page.evaluate(
                """(a) => {
                    var n = 0;
                    a.plays.forEach(function (p) {
                        var fp = KMFlight.flightParams(p, a.tables);
                        if (!fp) return;
                        // Infield dirt polygon: the home/1B/2B/3B diamond, in
                        // field-plane feet (ball-flight-refinements-plan.md F15).
                        if (Math.abs(fp.x) + Math.abs(fp.y - 63.64) <= 63.64) n++;
                    });
                    return n;
                }""",
                {"plays": real_singles, "tables": tables},
            )
            check("real singles landing on the infield dirt", dirt_count, 0)
        else:
            print("  [skip] no real single data found on disk")

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

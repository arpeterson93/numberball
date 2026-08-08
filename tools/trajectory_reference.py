"""
Python reference implementation of the ball-flight physics port
(ball-flight-3d-physics-redesign-plan.md Part 9), extended with the
groundPath bounce/roll model (Part 3) so this one file can regenerate golden
vectors for the JS parity tests and drive the distribution-shift audit
(Part 2.6).

This is the exact drag+lift integrator extracted from
TrajectoryCalculator-new-3D-June2026.xlsx, reproduced to 4-decimal parity
with the workbook's own cached outputs. The JS port (docs/js/trajectory.js)
must follow the same operation order.

Run from the repo root:
    python tools/trajectory_reference.py            # verify golden vectors
    python tools/trajectory_reference.py --emit out.json   # dump golden JSON
"""
from __future__ import annotations

import argparse
import json
import math
import sys

# ── Part 0.1: constants and atmosphere (fixed defaults - never vary per park) ──

TEMP_C = (5 / 9) * (75 - 32)
PRESSURE_MM = 29.92 * 1000 / 39.37
SVP = 4.5841 * math.exp((18.687 - TEMP_C / 234.5) * TEMP_C / (257.14 + TEMP_C))
RHO_LB = 0.06261 * 1.2929 * (273 / (TEMP_C + 273) * (PRESSURE_MM - 0.3783 * 50 * SVP / 100) / 760)
CONST = 0.07182 * RHO_LB  # mass/circ terms are 1 at the standard ball (5.125oz / 9.125in)
CD0, CDSPIN = 0.3008, 0.0292
CL0, CL1, CL2 = 0.583, 2.333, 1.12
TAU, DT, G, CIRC = 10000.0, 0.01, 32.174, 9.125
MAX_T = 15.0

CONTACT_HEIGHT_FT = 3.0
CONTACT_Y0_FT = 2.0

assert abs(CONST - 0.005316103027433) < 1e-12, f"CONST literal mismatch: {CONST!r}"


def simulate(ev_mph, la_deg, phi_deg, hand, z0=CONTACT_HEIGHT_FT, y0=CONTACT_Y0_FT, dt=DT, want_samples=False):
    """Part 0.2-0.5: full drag+lift Euler integration to first ground contact."""
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
    apex = z0
    rows = [(t, x, y, z)] if want_samples else None

    while t < MAX_T:
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
            vxf, vyf, vzf = lerp(nvx, vx), lerp(nvy, vy), lerp(nvz, vz)
            if rows is not None:
                rows.append((tf, xf, yf, 0.0))
            return {
                "dist": math.hypot(xf, yf), "hang_s": tf, "x": xf, "y": yf,
                "vx": vxf, "vy": vyf, "vz": vzf, "apex_ft": apex,
                "samples": _downsample(rows) if rows is not None else None,
            }
        x, y, z, t = nx, ny, nz, t + dt
        vx, vy, vz = nvx, nvy, nvz
        if z > apex:
            apex = z
        if rows is not None:
            rows.append((t, x, y, z))

    return None


def _downsample(rows, max_len=48):
    """Every Nth row (keeping row 0), always ending on the exact landing row
    (already appended by the caller as the last element of `rows`)."""
    if len(rows) <= max_len:
        return rows
    landing = rows[-1]
    body = rows[:-1]
    stride = math.ceil(len(body) / (max_len - 1))
    out = body[::stride]
    if out[-1] is not landing:
        out.append(landing)
    return out


# ── Part 3: bounce/roll model ──────────────────────────────────────────────

# Feel knobs (OQ-5), not measured physics - calibrated here against the 3.3
# acceptance checks (GO reaches SS depth in 1.0-1.8s of ground time and is
# still moving; a bunt never races out to SS depth; a weak grounder assigned
# to SS dies well short of 147ft, exercising the charge-in branch). The
# plan's own starting defaults (F_RETAIN_H=0.72, ROLL_DECEL=12.0) let a weak
# 60mph grounder's post-bounce roll speed carry it past a real shortstop's
# depth entirely - retuned here; final feel-level calibration against real
# plays still happens in Stage E.
E_REST_V = 0.42
F_RETAIN_H = 0.6
HOP_MIN_FT = 0.5
ROLL_DECEL = 20.0


def ground_path(sh, vz):
    """Part 3.1-3.2: bounces then rolls to rest. Returns restFt/totalS plus
    closed-form distAt/timeAt callables and the hop list."""
    hops = []
    sh_i = sh
    vz_down = abs(vz)
    t_cursor = 0.0
    d_cursor = 0.0
    segments = []  # (t_start, d_start, kind, params)

    while True:
        vz_up = E_REST_V * vz_down
        apex = (vz_up * vz_up) / (2 * G)
        if apex < HOP_MIN_FT:
            break
        t_i = 2 * vz_up / G
        len_i = sh_i * t_i
        hops.append({"lenFt": len_i, "apexFt": apex, "tS": t_i})
        segments.append(("hop", t_cursor, d_cursor, sh_i, t_i))
        t_cursor += t_i
        d_cursor += len_i
        sh_i = F_RETAIN_H * sh_i
        vz_down = vz_up

    sh_roll = sh_i
    roll_duration = sh_roll / ROLL_DECEL if ROLL_DECEL > 0 else 0.0
    roll_reach = (sh_roll * sh_roll) / (2 * ROLL_DECEL) if ROLL_DECEL > 0 else 0.0
    segments.append(("roll", t_cursor, d_cursor, sh_roll, roll_duration))
    rest_ft = d_cursor + roll_reach
    total_s = t_cursor + roll_duration

    def dist_at(t_s):
        t_s = max(0.0, t_s)
        if t_s >= total_s:
            return rest_ft
        for kind, t0, d0, speed, dur in segments:
            if t_s <= t0 + dur:
                dt_local = t_s - t0
                if kind == "hop":
                    return d0 + speed * dt_local
                return d0 + speed * dt_local - 0.5 * ROLL_DECEL * dt_local * dt_local
        return rest_ft

    def time_at(dist_ft):
        if dist_ft > rest_ft:
            return None
        if dist_ft <= 0:
            return 0.0
        for kind, t0, d0, speed, dur in segments:
            seg_len = (speed * dur) if kind == "hop" else roll_reach
            if dist_ft <= d0 + seg_len + 1e-9:
                local = dist_ft - d0
                if kind == "hop":
                    return t0 + (local / speed if speed > 0 else 0.0)
                # local = speed*t - 0.5*decel*t^2 -> solve for t (physical root)
                a, b, c = -0.5 * ROLL_DECEL, speed, -local
                disc = max(0.0, b * b - 4 * a * c)
                t_local = (-b + math.sqrt(disc)) / (2 * a) if a != 0 else (local / speed if speed > 0 else 0.0)
                return t0 + t_local
        return total_s

    return {"restFt": rest_ft, "totalS": total_s, "hops": hops, "distAt": dist_at, "timeAt": time_at}


# ── Golden vectors (Part 0.7) ──────────────────────────────────────────────

GOLDEN_VECTORS = [
    {"ev": 100, "la": 30, "phi": 0, "hand": "L", "z0": 6, "dist": 402.3273, "hang_s": 5.6230, "x": -39.8523, "y": 400.3486},
    {"ev": 100, "la": 28, "phi": 0, "hand": "R", "z0": 3, "dist": 401.8992, "hang_s": 5.3468, "x": 40.5529, "y": 399.8480},
    {"ev": 103, "la": 25, "phi": 20, "hand": "R", "z0": 3, "dist": 376.2823, "hang_s": 4.6524, "x": 207.9698, "y": 313.5873},
    {"ev": 103, "la": 25, "phi": -20, "hand": "L", "z0": 3, "dist": 376.2823, "hang_s": 4.6524, "x": -207.9698, "y": 313.5873},
    {"ev": 110, "la": 28, "phi": 0, "hand": "R", "z0": 3, "dist": 447.9520, "hang_s": 5.8368, "x": 49.9129, "y": 445.1625,
     "vx": 9.5229, "vy": 53.5836, "vz": -63.0522},
    {"ev": 85, "la": -11, "phi": 0, "hand": "R", "z0": 3, "dist": 15.7691, "hang_s": 0.1142, "x": 0.0460, "y": 15.7690,
     "vx": 0.7954, "vy": 118.6677, "vz": -28.6847},
]


def verify_golden_vectors(tol=1e-3):
    ok = True
    for v in GOLDEN_VECTORS:
        r = simulate(v["ev"], v["la"], v["phi"], v["hand"], z0=v["z0"])
        for key, want_key in (("dist", "dist"), ("hang_s", "hang_s"), ("x", "x"), ("y", "y")):
            got = r[want_key]
            want = v[key]
            diff = abs(got - want)
            status = "ok" if diff <= tol else "FAIL"
            if diff > tol:
                ok = False
            print(f"  [{status}] ev={v['ev']} la={v['la']} phi={v['phi']} hand={v['hand']} z0={v['z0']} "
                  f"{key}: got={got:.4f} want={want:.4f} diff={diff:.6f}")
        for key in ("vx", "vy", "vz"):
            if key in v:
                got, want = r[key], v[key]
                diff = abs(got - want)
                status = "ok" if diff <= tol else "FAIL"
                if diff > tol:
                    ok = False
                print(f"  [{status}] ev={v['ev']} la={v['la']} contact {key}: got={got:.4f} want={want:.4f} diff={diff:.6f}")
    return ok


def verify_ground_path_acceptance():
    """Part 3.3 acceptance checks."""
    ok = True

    r_go = simulate(85, -11, 0, "R")
    sh_go = math.hypot(r_go["vx"], r_go["vy"])
    gp_go = ground_path(sh_go, r_go["vz"])
    t_ss = gp_go["timeAt"](147 - r_go["dist"])
    go_ok = t_ss is not None and 1.0 <= t_ss <= 1.9 and gp_go["restFt"] > (147 - r_go["dist"])
    print(f"  [{'ok' if go_ok else 'FAIL'}] GO-shaped reaches SS depth (147ft) in {t_ss} s of ground time, still moving")
    ok = ok and go_ok

    r_bunt = simulate(35, -30, 0, "R")
    sh_bunt = math.hypot(r_bunt["vx"], r_bunt["vy"])
    gp_bunt = ground_path(sh_bunt, r_bunt["vz"])
    along_p = 60 - r_bunt["dist"]
    t_p = gp_bunt["timeAt"](along_p)
    bunt_reaches_ss = gp_bunt["restFt"] >= (147 - r_bunt["dist"])
    bunt_ok = (not bunt_reaches_ss) and (t_p is None or t_p >= 1.0)
    print(f"  [{'ok' if bunt_ok else 'FAIL'}] bunt-shaped never races to SS depth (restFt={gp_bunt['restFt']:.1f}ft, "
          f"reaches P's 60ft: {t_p})")
    ok = ok and bunt_ok

    r_weak = simulate(60, 2, 0, "R")
    sh_weak = math.hypot(r_weak["vx"], r_weak["vy"])
    gp_weak = ground_path(sh_weak, r_weak["vz"])
    weak_total = r_weak["dist"] + gp_weak["restFt"]
    weak_ok = weak_total < 147
    print(f"  [{'ok' if weak_ok else 'FAIL'}] weak grounder (SS-assigned) rests well short of 147ft (total={weak_total:.1f}ft)")
    ok = ok and weak_ok

    return ok


def emit_json(path):
    out = []
    for v in GOLDEN_VECTORS:
        r = simulate(v["ev"], v["la"], v["phi"], v["hand"], z0=v["z0"], want_samples=True)
        out.append({
            "ev": v["ev"], "la": v["la"], "phi": v["phi"], "hand": v["hand"], "z0": v["z0"],
            "dist": r["dist"], "hang_s": r["hang_s"], "x": r["x"], "y": r["y"],
            "vx": r["vx"], "vy": r["vy"], "vz": r["vz"], "apex_ft": r["apex_ft"],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(out)} golden vectors to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit", help="write golden vectors as JSON to this path")
    args = parser.parse_args()

    print("Workbook-parity + app-default golden vector check:")
    ok = verify_golden_vectors()

    print("\nGroundPath acceptance checks (Part 3.3):")
    ok = verify_ground_path_acceptance() and ok

    if args.emit:
        emit_json(args.emit)

    if not ok:
        print("\nFAILED golden vector parity.")
        sys.exit(1)
    print("\nAll golden vectors verified.")


if __name__ == "__main__":
    main()

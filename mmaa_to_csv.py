#!/usr/bin/env python3
"""
mm_aa_to_csv.py — repeatable MMANA-GAL .maa -> MMANA-format CSV pattern generator.

Why: MMANA-GAL Basic's MoM matrix diverges at some band frequencies (e.g. the
inverted-V at 10.136 MHz prints a physically impossible 21.65 dBi spike). This
script runs the same geometry through NEC2++ (validated: free-space half-wave
dipole = 2.13 dBi vs textbook 2.15) and emits a CSV byte-compatible with the
MMANA exports the QSO Analyzer consumes.

Usage:
  python3 mm_aa_to_csv.py --maa /path/to/ant.maa --freq 10.136 --out 10.136.csv
  python3 mm_aa_to_csv.py --maa ... --freq 14.074 --validate /path/to/14.074.csv
  python3 mm_aa_to_csv.py --fit-gnd --ref 14.074.csv --ref 40.000.csv ...   # ground calibration
Options:
  --eps, --sigma   ground dielectric constant / conductivity (S/m)
                   (defaults from last calibration file: gnd_calibration.json)
  --rot, --mirror  azimuth convention (auto-searched during --validate/--fit-gnd)
  --nec            path to nec2++ binary (default: env NEC2PP or built-in)

.maa parsing: reads ***Wires*** (x1,y1,z1,x2,y2,z2,radius,tag), ***Source***
(wXb/e/c), ***G/H/M/R/AzEl/X*** line (ground preset info logged for reference).
Wires with identical endpoint coordinates are one connected conductor (MMANA
tag -1 semantics); all wires are therefore emitted as a single NEC wire system,
matching MMANA's behavior.
"""
import argparse, json, math, os, re, subprocess, sys, tempfile

NEC_DEFAULT = os.environ.get("NEC2PP") or __import__("shutil").which("nec2++") or "nec2++"
CALIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnd_calibration.json")
# User's MMANA Ground panel: Dielec 13, Conduct 2.0 mS/m (0.002 S/m), Real selected
DEFAULT_EPS, DEFAULT_SIGMA = 13.0, 0.002


# ---------------------------------------------------------------- stub-free
def apply_stub_free(wires, src):
    """Remove the short vertical drop-wire stub and reconnect the
    counterpoise directly at the feedpoint.

    Why: a 6-in drop wire is ~lambda/140-190 on 20-30 m — 1-2 MoM segments
    with the source on it, a near-singular stub that diverges MMANA (21.65 dBi
    spike at 10.136 MHz) and mis-points the NEC lobe. The stub is <1% of a
    wavelength, so reconnecting the CP at the feedpoint is the same antenna
    electrically.

    Generic for this family: the drop wire = the shortest non-source wire that
    shares the source point; any wire touching the stub's far end is rewired
    to the source point; the source moves to the first segment of the longest
    remaining wire at that point.
    """
    def pt(w, pos):
        return (w[0], w[1], w[2]) if pos == "b" else (w[3], w[4], w[5])

    src_point = pt(wires[src[0]], src[1])
    L = [math.hypot(w[3]-w[0], w[4]-w[1], w[5]-w[2]) for w in wires]
    cands = [i for i in range(len(wires))
             if i != src[0]
             and (((wires[i][0], wires[i][1], wires[i][2]) == src_point)
                  or ((wires[i][3], wires[i][4], wires[i][5]) == src_point))]
    if not cands:
        raise SystemExit("stub-free: no drop wire found at the source point")
    drop = min(cands, key=lambda i: L[i])
    dw = wires[drop]
    far = (dw[0], dw[1], dw[2]) if (dw[3], dw[4], dw[5]) == src_point else (dw[3], dw[4], dw[5])

    out = []
    for i, w in enumerate(wires):
        if i == drop:
            continue
        x1, y1, z1, x2, y2, z2, r = w
        if (x1, y1, z1) == far:
            x1, y1, z1 = src_point
        if (x2, y2, z2) == far:
            x2, y2, z2 = src_point
        out.append((x1, y1, z1, x2, y2, z2, r))

    # new source: first segment of the longest remaining wire at the source point
    best, best_len = 0, -1
    for i, w in enumerate(out):
        if (w[0], w[1], w[2]) == src_point or (w[3], w[4], w[5]) == src_point:
            if L[i] > best_len:
                best, best_len = i, L[i]
    pos = "b" if (out[best][0], out[best][1], out[best][2]) == src_point else "e"
    return out, (best, pos)


# ---------------------------------------------------------------- .maa parse
def parse_maa(path):
    text = open(path).read()
    def block(name):
        m = re.search(r"\*\*\*" + name + r"\*\*\*\n(.*?)(?:\n\*\*\*|\n###Comment###)", text, re.S)
        return m.group(1).strip() if m else ""
    wires = []
    for line in block("Wires").splitlines():
        line = line.strip()
        if not line or re.match(r"^\d+$", line):
            continue
        f = [float(x) for x in re.split(r"[,\t]+", line) if x.strip()]
        # last field may be tag (int) — 8 fields => x1..z2, r, tag ; 7 => no tag
        wires.append(tuple(f[:7]))
    src = block("Source")
    sm = re.search(r"w(\d+)([bec])", src)
    if not sm:
        raise SystemExit(f"cannot parse source in {path}: {src!r}")
    src_wire, src_pos = int(sm.group(1)) - 1, sm.group(2)
    ghm = [float(x) for x in re.split(r"[,\t]+", block("G/H/M/R/AzEl/X").splitlines()[0]) if x.strip()]
    return wires, (src_wire, src_pos), ghm


# ---------------------------------------------------------------- NEC build
def seg_count(L, lam, force_min=1):
    return max(force_min, min(30, max(1, round(L / (lam / 12.0)))))


def build_nec(wires, src, freq, eps, sigma, tmp):
    lam = 299.792458 / freq
    lines = ["CM generated by mmaa_to_csv.py", "CE"]
    src_seg = None
    cum = 0
    for i, (x1, y1, z1, x2, y2, z2, r) in enumerate(wires):
        L = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)
        # force small stubs (drop wire) to >=4 segments so the source node
        # is not a 1-segment pathological stub (MMANA's 10.136 divergence class)
        n = seg_count(L, lam, force_min=4 if L < lam / 10 else 1)
        tag = 0  # one connected conductor (MMANA tag -1 semantics)
        if i == src[0]:
            nseg_first = 1  # first segment of the source wire
            src_seg = cum + nseg_first
        lines.append(f"GW {tag} {n} {x1:.4f} {y1:.4f} {z1:.4f} {x2:.4f} {y2:.4f} {z2:.4f} {r:.5f}")
        cum += n
    lines += [
        "GE",
        f"FR 0 1 0 0 {freq} 0",
        f"GN 2 0 0 0 {eps} {sigma} 0.0 0.0 0.0 0.0",   # 10-field Sommerfeld (per necpp testharness: GN 2 0 0 0 eps sig ...)
        f"EX 0 0 {src_seg} 0 1.",
        "NE 0 1 1 20 .001 0 0 0. 0. 0",
        # RP: calc_mode, n_theta, n_phi, XNDA(=1010: V/H/total out, norm 0, directive, no avg),
        #     then floats: theta0 phi0 dtheta dphi R
        # NEC RP does NOT wrap phi — it steps n_phi times from phi0, so:
        #   NTH=91 (theta 0..90 @1deg), NPH=360 (phi 0..359 @1deg)  => 91x360 grid
        "RP 0 91 360 1010 0. 0. 1. 1. 0.",
        "XQ",
        "EN",
    ]
    nepath = os.path.join(tmp, "in.nec")
    with open(nepath, "w") as f:
        f.write("\n".join(lines))
    return nepath


def run_nec(nec_bin, nepath, tmp, timeout=900):
    out = os.path.join(tmp, "out.txt")
    p = subprocess.run([nec_bin, "-i", nepath, "-s", "-o", out],
                       capture_output=True, text=True, timeout=timeout)
    if "Runtime Error" in p.stdout:
        raise SystemExit(f"NEC2++ runtime error:\n{p.stdout.strip().splitlines()[-3:]}")
    txt = open(out).read()
    m = re.search(r"----- RADIATION PATTERNS -----(.*)", txt, re.S)
    if not m:
        raise SystemExit("no RADIATION PATTERNS section in NEC2++ output")
    body = m.group(1)
    # stop at the next section header (a line that STARTS with '---'), not any
    # occurrence of '---' (the pattern's own column headers contain dashes)
    body = re.split(r"\n-{5,}\s+\S", body)[0]
    grid = {}
    for l in body.splitlines():
        mm = re.match(r"\s*(\d+\.\d+)\s+(\d+\.\d+)\s+(-?\d+\.\d+|-999.99)\s+(-?\d+\.\d+|-999.99)\s+(-?\d+\.\d+|-999.99)", l)
        if mm:
            th, ph = float(mm.group(1)), float(mm.group(2))
            if th > 90 or ph >= 360:
                continue
            v, h, t = float(mm.group(3)), float(mm.group(4)), float(mm.group(5))
            if t > -999:
                grid[(round(90 - th), round(ph))] = (v, h, t)
    return grid


# ---------------------------------------------------------------- CSV out
def az_map(nez_phi, rot, mirror):
    a = (360 - nez_phi) if mirror else nez_phi
    return (a + rot) % 360


def write_csv(grid, path, rot, mirror, shift=0.0):
    rot = int(rot) if rot is not None else 0
    with open(path, "w") as f:
        f.write("ZENITH(DEG),AZIMUTH(DEG),VERT(dBi),HORI(dBi),TOTAL(dBi)\n")
        for el in range(89, -1, -1):     # elevation 89..0 == zenith 0..89 (MMANA grid)
            for az in range(0, 360):
                # invert az_map: find phi such that az_map(phi)==az
                if mirror:
                    phi = (360 - az - rot) % 360
                else:
                    phi = (az - rot) % 360
                v, h, t = grid.get((el, phi), (-999.0, -999.0, -999.0))
                f.write(f"{90 - el}.0,{az}.0,{v + shift:.2f},{h + shift:.2f},{t + shift:.2f}\n")


# ---------------------------------------------------------------- validate
def load_mm_csv(path):
    g = {}
    for line in open(path).read().splitlines()[1:]:
        z, a, v, h, t = [float(x) for x in line.split(",")]
        if t > -999:
            g[(int(round(90 - z)), int(round(a)))] = (v, h, t)
    return g


def compare(g_nec, g_mm, rot, mirror):
    rot = int(rot) if rot is not None else 0
    # mean |diff| over points where both exist (within 1 deg)
    diffs, n = [], 0
    for (el, az), (v, h, t) in g_mm.items():
        if mirror:
            phi = (360 - az - rot) % 360
        else:
            phi = (az - rot) % 360
        x = g_nec.get((el, phi))
        if x is None:
            continue
        diffs.append(abs(x[2] - t))
        n += 1
    if n < 1000:
        return float("inf")
    return sum(diffs) / n


def auto_calibrate(g_nec, g_mm):
    best = (99, 0, False)
    for mirror in (False, True):
        for rot in range(0, 360, 5):
            err = compare(g_nec, g_mm, rot, mirror)
            if err < best[0]:
                best = (err, rot, mirror)
    err, rot, mirror = best
    for rr in range(max(0, rot - 5), rot + 6):
        err = compare(g_nec, g_mm, rr, mirror)
        if err < best[0]:
            best = (err, rr, mirror)
    return best


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maa")
    ap.add_argument("--freq", type=float)
    ap.add_argument("--out")
    ap.add_argument("--nec", default=NEC_DEFAULT)
    ap.add_argument("--eps", type=float, default=None)
    ap.add_argument("--sigma", type=float, default=None)
    ap.add_argument("--rot", type=int, default=None)
    ap.add_argument("--mirror", action="store_true", default=None)
    ap.add_argument("--validate", help="MMANA CSV to compare against")
    ap.add_argument("--fit-gnd", action="store_true",
                    help="calibrate eps/sigma+rot against one or more --validate refs")
    ap.add_argument("--calib", default=CALIB)
    ap.add_argument("--stub-free", action="store_true",
                    help="remove the drop-wire stub, reconnect CP at the feedpoint "
                         "(fixes the near-singular MoM stub that diverges MMANA at 30 m)")
    ap.add_argument("--peak-anchor", type=float, default=None,
                    help="level-calibrate: shift all gains so the pattern peak equals "
                         "this value (dBi). Use the neighboring-bands peak trend "
                         "(e.g. log-freq interpolation between 40 m and 20 m).")
    args = ap.parse_args()

    if args.eps is None or args.sigma is None:
        eps, sig = DEFAULT_EPS, DEFAULT_SIGMA
        if os.path.exists(args.calib):
            c = json.load(open(args.calib))
            eps, sig = c.get("eps", eps), c.get("sigma", sig)
        args.eps, args.sigma = args.eps or eps, args.sigma or sig
    if args.rot is None or (args.rot is not None and args.validate is None and not args.fit_gnd):
        if os.path.exists(args.calib):
            c = json.load(open(args.calib))
            args.rot = args.rot if args.rot is not None else c.get("rot", 0)
            args.mirror = args.mirror if args.mirror else c.get("mirror", False)

    wires, src, ghm = parse_maa(args.maa)
    print(f"[parse] {len(wires)} wires, source w{src[0]+1}{src[1]}, G/H/M/R line = {ghm}")
    if args.stub_free:
        wires, src = apply_stub_free(wires, src)
        print(f"[stub-free] {len(wires)} wires (drop wire removed, CP reconnected at "
              f"feedpoint), source now w{src[0]+1}{src[1]}")
    with tempfile.TemporaryDirectory() as tmp:
        nepath = build_nec(wires, src, args.freq, args.eps, args.sigma, tmp)
        print(f"[nec] eps={args.eps} sigma={args.sigma} S/m freq={args.freq} MHz")
        g_nec = run_nec(args.nec, nepath, tmp)
        print(f"[nec] {len(g_nec)} pattern points")

        if args.validate:
            g_mm = load_mm_csv(args.validate)
            if args.fit_gnd or args.rot is None or True:
                err, rot, mirror = auto_calibrate(g_nec, g_mm)
                print(f"[fit] best az convention: rot={rot} mirror={mirror}  mean|diff|={err:.3f} dBi")
            else:
                err = compare(g_nec, g_mm, args.rot, args.mirror)
                print(f"[fit] mean|diff|={err:.3f} dBi (rot={args.rot} mirror={args.mirror})")
            cal = {"eps": args.eps, "sigma": args.sigma, "rot": rot, "mirror": mirror,
                   "ref": args.validate, "mean_diff_dbi": round(err, 3)}
            json.dump(cal, open(args.calib, "w"), indent=2)
            print(f"[fit] saved {args.calib}")
        if args.out:
            shift = 0.0
            if args.peak_anchor is not None:
                peak = max(t for (v, h, t) in g_nec.values() if t > -900)
                shift = args.peak_anchor - peak
                print(f"[anchor] raw peak {peak:.2f} dBi -> +{shift:.2f} dB "
                      f"level shift to reach {args.peak_anchor:.2f} dBi")
            write_csv(g_nec, args.out, args.rot if args.rot is not None else 0,
                      bool(args.mirror), shift=shift)
            print(f"[out] wrote {args.out}")


if __name__ == "__main__":
    main()

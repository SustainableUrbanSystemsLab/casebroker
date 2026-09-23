"""Refuse a box domain that, turned to face the wind, leaves the terrain sheet.

build-case turns a BOX domain about its own centre by 180 - theta so its fixed
inlet faces each wind direction. The terrain is an open sheet, and it seals the
domain floor only where it spans the box. Turned 45 degrees, the +/-1300 m box's
corners reach ~1838 m, past the +/-1304 m sheet: the space under the terrain
then connects to the flow, and snappyHexMesh keeps it as fluid. Measured on
v2-1410516cea4c5d7b (fixed-box-1008/of12-v3): 64% of the core had mesh under
the ground in all four diagonal directions, 0% in the four axis-aligned ones --
the solve had a path beneath the terrain, and nothing in its logs said so.

A cylinder does not turn (its inlet patches are chosen per direction), so only a
box is checked. Exit codes are run_case.sh's: 0 fits, 3 does not (the caller
decides what that means), 2 usage.

  python domain_check.py terrain.stl --domain '{"min":[..],"max":[..]}' --directions 0,45
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys


def sheet_bounds(path: str) -> tuple[float, float, float, float]:
    """(xmin, xmax, ymin, ymax) of an STL, binary or ascii, without numpy: this
    runs under the runner's bare interpreter."""
    with open(path, "rb") as fh:
        data = fh.read()
    xs, ys = [], []
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * n:
            for k in range(n):
                v = struct.unpack_from("<9f", data, 84 + 50 * k + 12)
                xs += v[0::3]
                ys += v[1::3]
            return min(xs), max(xs), min(ys), max(ys)
    for line in data.decode("utf-8", "ignore").splitlines():
        p = line.split()
        if p and p[0] == "vertex":
            xs.append(float(p[1]))
            ys.append(float(p[2]))
    if not xs:
        raise ValueError(f"{path}: no vertices")
    return min(xs), max(xs), min(ys), max(ys)


def rotation_deg(direction_deg: float) -> float:
    """The turn build-case gives the box for a wind direction (BuildCaseCommand
    .BoxRotationDegrees in Eddy3D): 180 - theta."""
    return 180.0 - direction_deg


def footprint(dmin, dmax, rot_deg: float) -> tuple[float, float, float, float]:
    """(xmin, xmax, ymin, ymax) of the box's plan after turning it about its centre."""
    cx, cy = (dmin[0] + dmax[0]) / 2, (dmin[1] + dmax[1]) / 2
    hx, hy = (dmax[0] - dmin[0]) / 2, (dmax[1] - dmin[1]) / 2
    c, s = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    ex = abs(hx * c) + abs(hy * s)
    ey = abs(hx * s) + abs(hy * c)
    return cx - ex, cx + ex, cy - ey, cy + ey


def leaks(domain: dict, directions, sheet, tol: float = 1e-6) -> list[str]:
    """One line per direction whose turned box leaves the sheet in plan."""
    if (domain.get("shape") or "box").lower() != "box":
        return []
    xmin, xmax, ymin, ymax = sheet
    bad = []
    for d in directions:
        fx0, fx1, fy0, fy1 = footprint(domain["min"], domain["max"], rotation_deg(float(d)))
        over = max(xmin - fx0, fx1 - xmax, ymin - fy0, fy1 - ymax)
        if over > tol:
            bad.append(f"{float(d):g} deg: the turned box reaches {over:.0f} m past the terrain sheet")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("terrain")
    ap.add_argument("--domain", required=True, help="the domain block, JSON")
    ap.add_argument("--directions", required=True, help="comma separated, degrees")
    a = ap.parse_args()
    try:
        domain = json.loads(a.domain)
        dirs = [float(x) for x in a.directions.split(",") if x.strip()]
    except ValueError as e:
        print(f"domain_check: {e}", file=sys.stderr)
        return 2
    bad = leaks(domain, dirs, sheet_bounds(a.terrain))
    for line in bad:
        print(line)
    return 3 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

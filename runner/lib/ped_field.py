"""The pedestrian wind field: OpenFOAM's iso-surface sample, read onto the grid.

ped_grid.py writes the dictionary; OpenFOAM cuts a distanceSurface h m above the
terrain in every direction case and writes U on it (cellPoint, per vertex); this
reads each surface back onto the regular 2 m lattice and puts the result where a
person or a model can use it. Two outputs, for two readers:

  pedestrian/U.npz        the DATASET: U (ux, uy, uz) at every grid point, every
                          direction, every height, float32, NaN where there was
                          no fluid (inside a building). This ships in the archive.
  <case_id>.wfld          the VIEWER bundle: |U| per direction at the published
                          height, quantised to a byte, plus a decimated vector
                          field. A few MB, gzip-compressed, read by the dashboard.

Reading the surface onto the grid is linear interpolation inside the surface's
own triangles. Each triangle lies within one cell's tet decomposition and its
vertices carry cellPoint values, so this reproduces cellPoint at the grid point
rather than estimating anything anew -- it is what ParaView draws for the same
slice. The triangles are used as they are (never re-triangulated, which would
bridge across a building), so a grid point inside a building falls in no
triangle and stays NaN.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import re
import struct
from pathlib import Path

import numpy as np

FUNCTION = "pedestrianSurface"
# The height the viewer bundle carries. 1.75 m is the published pedestrian height;
# 1.5 m is sampled alongside so the campaign stays comparable with what the runner
# has always meant by pedestrian height (see run_case.sh, PEDESTRIAN_H).
VIEW_HEIGHT = 1.75
# Arrows every 8th grid point: 63 x 63 over the core at 2 m, 16 m apart, about one
# arrow per street width -- denser is unreadable at dashboard size.
VECTOR_STRIDE = 8
# Byte 255 is "no sample". 0..254 spans [0, vmax] for the whole case, so every
# direction shares one colour scale and cycling directions does not rescale.
NODATA = 255


def load_grid(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as fh:
        grid = json.load(fh)
    for key in ("nx", "ny", "x0", "y0", "spacing_m", "heights_m", "surfaces"):
        if key not in grid:
            raise ValueError(f"{path}: grid description has no {key!r}")
    return grid


def read_vtk(path: str | os.PathLike, field: str = "U") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(points (n, 3), triangles (t, 3), values (n, k)) from an OpenFOAM legacy
    ascii VTK surface. Polygons are fanned into triangles; they are convex (cut
    from a cell), so a fan is exact."""
    text = Path(path).read_text(encoding="ascii", errors="replace")
    head = text[:256].splitlines()
    if len(head) < 3 or head[2].strip().upper() != "ASCII":
        raise ValueError(f"{path}: not an ascii legacy VTK file")
    toks = text.split()
    try:
        ip = toks.index("POINTS")
    except ValueError:
        raise ValueError(f"{path}: no POINTS") from None
    n = int(toks[ip + 1])
    pts = np.array(toks[ip + 3:ip + 3 + 3 * n], dtype=np.float64).reshape(n, 3)
    if n == 0:
        return pts, np.empty((0, 3), np.int64), np.empty((0, 3))
    ig = toks.index("POLYGONS")
    size = int(toks[ig + 2])
    conn = np.array(toks[ig + 3:ig + 3 + size], dtype=np.int64)
    tris = []
    k = 0
    while k < size:
        c = int(conn[k])
        poly = conn[k + 1:k + 1 + c]
        for q in range(1, c - 1):
            tris.append((poly[0], poly[q], poly[q + 1]))
        k += c + 1
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    # POINT_DATA, never CELL_DATA: per-face values would be a face average, not
    # cellPoint at the vertex -- that is what `interpolate true` in the dictionary
    # buys, and a surface written without it is refused here rather than read.
    try:
        ipd = toks.index("POINT_DATA")
    except ValueError:
        raise ValueError(f"{path}: no POINT_DATA -- was the surface written with "
                         "`interpolate true`?") from None
    for j in range(ipd, len(toks) - 3):
        if toks[j] == field and toks[j + 3] in ("float", "double"):
            comps, count = int(toks[j + 1]), int(toks[j + 2])
            if count != n:
                raise ValueError(f"{path}: {field} has {count} values for {n} points")
            vals = np.array(toks[j + 4:j + 4 + comps * n], dtype=np.float64).reshape(n, comps)
            return pts, tris, vals
    raise ValueError(f"{path}: no point field {field!r}")


def rasterize(pts: np.ndarray, tris: np.ndarray, vals: np.ndarray, grid: dict,
              target: np.ndarray | None = None, floor: np.ndarray | None = None,
              chunk: int = 200_000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Linear interpolation of per-vertex `vals` at every grid point that lies
    inside one of the surface's triangles (in plan). Returns (values (ny, nx, k),
    z (ny, nx), under (ny, nx) bool); NaN outside every triangle.

    Where a grid point is inside more than one triangle, the one kept is the one
    whose height is closest to `target` (grade + h) -- or, with no target, the
    lowest. More than one is not only a shared edge: the distance is UNSIGNED,
    so wherever the mesh has cells below the terrain the surface is cut there
    too, h under the ground. That happened on the v3 box: its diagonal
    directions rotate the +/-1300 m box so its corners reach ~1840 m, past the
    +/-1304 m sheet, which then no longer seals the floor and snappy keeps the
    volume under the terrain as fluid. Measured on v2-1410516cea4c5d7b case_045:
    "lowest" put every value 2h below its label (median -3.00 m at 1.5 m).
    `under` marks grid points with any hit below `floor` (the terrain), so that
    mesh defect is reported rather than silently stepped around.
    """
    nx, ny, dx = int(grid["nx"]), int(grid["ny"]), float(grid["spacing_m"])
    x0, y0 = float(grid["x0"]), float(grid["y0"])
    k = vals.shape[1]
    tgt = None if target is None else np.asarray(target, dtype=np.float64).ravel()
    flr = None if floor is None else np.asarray(floor, dtype=np.float64).ravel()
    best_score = np.full(ny * nx, np.inf)
    best_z = np.full(ny * nx, np.nan)
    under = np.zeros(ny * nx, dtype=bool)
    out = np.full((ny * nx, k), np.nan)
    for s in range(0, len(tris), chunk):
        t = tris[s:s + chunk]
        P = pts[t]                                              # (m, 3, 3)
        lo, hi = P[:, :, :2].min(axis=1), P[:, :, :2].max(axis=1)
        i0 = np.maximum(np.ceil((lo[:, 0] - x0) / dx - 1e-9), 0).astype(np.int64)
        i1 = np.minimum(np.floor((hi[:, 0] - x0) / dx + 1e-9), nx - 1).astype(np.int64)
        j0 = np.maximum(np.ceil((lo[:, 1] - y0) / dx - 1e-9), 0).astype(np.int64)
        j1 = np.minimum(np.floor((hi[:, 1] - y0) / dx + 1e-9), ny - 1).astype(np.int64)
        ni, nj = i1 - i0 + 1, j1 - j0 + 1
        counts = np.where((ni > 0) & (nj > 0), ni * nj, 0)
        if counts.sum() == 0:
            continue
        ti = np.repeat(np.arange(len(t)), counts)
        start = np.cumsum(counts) - counts
        r = np.arange(counts.sum()) - start[ti]
        ii = i0[ti] + r % ni[ti]
        jj = j0[ti] + r // ni[ti]
        gx, gy = x0 + ii * dx, y0 + jj * dx
        a, b, c = P[ti, 0], P[ti, 1], P[ti, 2]
        det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(det) > 1e-12
        det = np.where(ok, det, 1.0)
        w1 = ((b[:, 1] - c[:, 1]) * (gx - c[:, 0]) + (c[:, 0] - b[:, 0]) * (gy - c[:, 1])) / det
        w2 = ((c[:, 1] - a[:, 1]) * (gx - c[:, 0]) + (a[:, 0] - c[:, 0]) * (gy - c[:, 1])) / det
        w3 = 1.0 - w1 - w2
        eps = 1e-9
        inside = ok & (w1 >= -eps) & (w2 >= -eps) & (w3 >= -eps)
        if not inside.any():
            continue
        ti, ii, jj = ti[inside], ii[inside], jj[inside]
        w1, w2, w3 = w1[inside], w2[inside], w3[inside]
        tt = t[ti]
        z = w1 * pts[tt[:, 0], 2] + w2 * pts[tt[:, 1], 2] + w3 * pts[tt[:, 2], 2]
        v = (w1[:, None] * vals[tt[:, 0]] + w2[:, None] * vals[tt[:, 1]]
             + w3[:, None] * vals[tt[:, 2]])
        lin = jj * nx + ii
        if flr is not None:
            under[lin[z < flr[lin]]] = True
        score = np.abs(z - tgt[lin]) if tgt is not None else z
        # The best hit per grid point: sort by (point, score) and keep each first.
        order = np.lexsort((score, lin))
        lin, z, v, score = lin[order], z[order], v[order], score[order]
        first = np.ones(len(lin), bool)
        first[1:] = lin[1:] != lin[:-1]
        lin, z, v, score = lin[first], z[first], v[first], score[first]
        better = score < best_score[lin]
        best_score[lin[better]] = score[better]
        best_z[lin[better]] = z[better]
        out[lin[better]] = v[better]
    return out.reshape(ny, nx, k), best_z.reshape(ny, nx), under.reshape(ny, nx)


def latest_sample_dir(case_dir: Path) -> Path | None:
    """postProcessing/pedestrianSurface/<time>/ with the largest time, or None."""
    times = []
    for d in (case_dir / "postProcessing" / FUNCTION).glob("*"):
        try:
            times.append((float(d.name), d))
        except ValueError:
            continue
    return max(times)[1] if times else None


def directions(study_dir: Path) -> list[dict]:
    """One record per direction case: its name, its angle (degrees, the
    meteorological FROM-direction), and its inlet profile where known.

    eddy3d-study.json is the authority: it carries the exact angle (11.25 deg is
    case_011 on disk) and the per-direction z0. A study without it falls back to
    the angle in the directory name, which is exact for the 45-degree recipes.
    """
    manifest = study_dir / "eddy3d-study.json"
    dirs: list[dict] = []
    abl: dict = {}
    if manifest.is_file():
        with open(manifest, encoding="utf-8") as fh:
            m = json.load(fh)
        abl = m.get("abl") or {}
        for d in m.get("directions") or []:
            name = d.get("caseName")
            if name and (study_dir / name).is_dir():
                dirs.append({"case": name, "deg": float(d["degrees"]),
                             "uref": d.get("uref", abl.get("uref")),
                             "zref": d.get("zref", abl.get("zref")),
                             "z0": d.get("z0", abl.get("z0"))})
    if not dirs:
        for p in sorted(study_dir.glob("case_*")):
            mt = re.fullmatch(r"case_(\d+)", p.name)
            if p.is_dir() and mt:
                dirs.append({"case": p.name, "deg": float(mt.group(1)),
                             "uref": abl.get("uref"), "zref": abl.get("zref"), "z0": abl.get("z0")})
    return sorted(dirs, key=lambda d: d["deg"])


def u_ref(uref, zref, z0, h: float) -> float | None:
    """The undisturbed inlet speed at height h above grade: the same log law the
    inlet imposes, U(z) = (U*/kappa) ln((z + z0)/z0), scaled from (Uref, Zref).
    This is the denominator of the amplification U/U_ref -- what the site does to
    the wind, independent of how hard the inlet blew."""
    try:
        uref, zref, z0 = float(uref), float(zref), float(z0)
    except (TypeError, ValueError):
        return None
    if not (uref > 0 and zref > 0 and z0 > 0):
        return None
    return uref * math.log((h + z0) / z0) / math.log((zref + z0) / z0)


def collect(study_dir: str | os.PathLike, grid_path: str | os.PathLike,
            terrain_stl: str | os.PathLike | None = None) -> tuple[np.ndarray, dict]:
    """Every direction's sample, as U[direction, height, y, x, component], and
    the description a reader needs.

    A direction that produced no sample is kept, all-NaN, and named in `missing`
    -- dropping it would shift every later direction's index and silently
    relabel the field. With the terrain sheet, every sampled point's height above
    grade is also checked against the height it is labelled with, because a
    sample at the wrong height looks exactly like a correct one.
    """
    study = Path(study_dir)
    grid = load_grid(grid_path)
    heights = [float(h) for h in grid["heights_m"]]
    name_for = {float(h): name for name, h in grid["surfaces"].items()}
    dirs = directions(study)
    if not dirs:
        raise ValueError(f"{study}: no case_* directions")
    ny, nx = int(grid["ny"]), int(grid["nx"])
    ground = None
    if terrain_stl:
        import ped_grid
        hf = ped_grid.heightfield(ped_grid.read_stl(str(terrain_stl)))
        xs = float(grid["x0"]) + float(grid["spacing_m"]) * np.arange(nx)
        ys = float(grid["y0"]) + float(grid["spacing_m"]) * np.arange(ny)
        gx, gy = np.meshgrid(xs, ys, indexing="xy")
        ground = ped_grid.drape(hf, gx, gy)
    field = np.full((len(dirs), len(heights), ny, nx, 3), np.nan, dtype=np.float32)
    missing, coverage, above = [], {}, {}
    for di, d in enumerate(dirs):
        sample_dir = latest_sample_dir(study / d["case"])
        d["time"] = sample_dir.name if sample_dir else None
        for hi, h in enumerate(heights):
            key = f"{d['case']}@{h:g}"
            path = sample_dir / f"{name_for[h]}.vtk" if sample_dir else None
            if path is None or not path.is_file():
                missing.append(key)
                continue
            pts, tris, vals = read_vtk(path)
            if ground is not None:
                u, z, under = rasterize(pts, tris, vals[:, :3], grid, target=ground + h, floor=ground)
            else:
                u, z, under = rasterize(pts, tris, vals[:, :3], grid)
            field[di, hi] = u
            coverage[key] = round(float(np.isfinite(u[:, :, 0]).mean()), 5)
            if ground is not None and np.isfinite(z).any():
                err = (z - ground)[np.isfinite(z)] - h
                above[key] = {"median_m": round(float(np.median(err)), 4),
                              "p99_abs_m": round(float(np.percentile(np.abs(err), 99)), 4),
                              # Share of the core with mesh UNDER the terrain: a
                              # domain the sheet did not seal (see rasterize).
                              "under_terrain": round(float(under.mean()), 5)}
    for d in dirs:
        d["u_ref"] = {f"{h:g}": u_ref(d["uref"], d["zref"], d["z0"], h) for h in heights}
    meta = {
        "format": "pedestrian-field/1",
        "grid": {k: grid[k] for k in ("nx", "ny", "x0", "y0", "spacing_m", "half_m") if k in grid},
        "heights_m": heights,
        "directions": dirs,
        "axes": ["direction", "height", "y", "x", "component"],
        "components": ["ux", "uy", "uz"],
        "frame": "metres east (+x) / north (+y) of the site centre; height above grade",
        "sampler": "OpenFOAM distanceSurface over the terrain sheet (cellPoint, per vertex), "
                   "linear in the surface's own triangles onto the grid",
        "coverage": coverage,
        "height_error": above,
        "missing": missing,
    }
    return field, meta


def write_dataset(field: np.ndarray, meta: dict, out_dir: str | os.PathLike) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "U.npz", U=field)
    with open(out / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return out / "U.npz"


def bundle(field: np.ndarray, meta: dict, case_id: str, height: float = VIEW_HEIGHT,
           stride: int = VECTOR_STRIDE) -> bytes:
    """The viewer's file: gzip of

        b"WFLD" | u32 version=1 | u32 header length | header JSON (utf-8)
        | per direction: nx*ny bytes of |U| (row 0 = SOUTH edge), then
          vny*vnx*2 int16 (ux, uy in cm/s, interleaved; -32768 = no data)

    Byte-quantised magnitude is enough for a colour map (the step is vmax/254,
    about 0.03 m/s on a typical case) and keeps 32 directions to a few MB; the
    exact values are in U.npz.
    """
    heights = [float(h) for h in meta["heights_m"]]
    hi = min(range(len(heights)), key=lambda k: abs(heights[k] - height))
    u = field[:, hi]                                    # (ndir, ny, nx, 3)
    mag = np.sqrt(u[..., 0] ** 2 + u[..., 1] ** 2 + u[..., 2] ** 2)
    finite = np.isfinite(mag)
    vmax = float(np.percentile(mag[finite], 99.9)) if finite.any() else 1.0
    vmax = max(vmax, 1e-3)
    q = np.full(mag.shape, NODATA, dtype=np.uint8)
    q[finite] = np.clip(np.rint(mag[finite] / vmax * 254), 0, 254).astype(np.uint8)

    # Arrows at the centre of each stride x stride block, so they sit evenly
    # inside the core rather than hugging its south-west edge.
    off = stride // 2
    sub = u[:, off::stride, off::stride, :2]
    vec = np.where(np.isfinite(sub), np.rint(sub * 100), -32768)
    vec = np.clip(vec, -32768, 32767).astype("<i2")

    g = meta["grid"]
    h = heights[hi]
    header = {
        "format": "wfld/1", "case_id": case_id,
        "nx": int(g["nx"]), "ny": int(g["ny"]), "x0": g["x0"], "y0": g["y0"],
        "spacing_m": g["spacing_m"], "height_m": h,
        "vmax": vmax, "nodata": NODATA,
        "vector": {"stride": stride, "offset": off,
                   "nx": int(sub.shape[2]), "ny": int(sub.shape[1]), "scale": 0.01,
                   "nodata": -32768},
        "directions": [{"deg": d["deg"], "case": d["case"],
                        "u_ref": d["u_ref"].get(f"{h:g}"), "uref": d.get("uref"),
                        "zref": d.get("zref"), "z0": d.get("z0")}
                       for d in meta["directions"]],
        "coverage": [meta["coverage"].get(f"{d['case']}@{h:g}") for d in meta["directions"]],
        # Per direction: how far the values sit from their label, and how much
        # of the core had mesh under the terrain -- shown beside the field so a
        # defective direction is not read as a clean one.
        "checks": [(meta.get("height_error") or {}).get(f"{d['case']}@{h:g}") for d in meta["directions"]],
        "missing": meta.get("missing", []),
    }
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = [b"WFLD", struct.pack("<II", 1, len(hb)), hb]
    for k in range(q.shape[0]):
        body.append(q[k].tobytes())
        body.append(vec[k].tobytes())
    return gzip.compress(b"".join(body), compresslevel=6, mtime=0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("study", help="the study directory holding case_*/ and eddy3d-study.json")
    ap.add_argument("--grid", required=True, help="grid.json written by ped_grid.py")
    ap.add_argument("--out", required=True, help="directory for U.npz + meta.json")
    ap.add_argument("--terrain", help="the terrain sheet, to check every sample's height above grade")
    ap.add_argument("--bundle", help="also write the viewer bundle to this path")
    ap.add_argument("--case-id", default="", help="recorded in the bundle")
    ap.add_argument("--remove-raw", action="store_true",
                    help="delete the .vtk surfaces once they are safely in U.npz")
    a = ap.parse_args()

    field, meta = collect(a.study, a.grid, a.terrain)
    write_dataset(field, meta, a.out)
    if a.bundle:
        Path(a.bundle).write_bytes(bundle(field, meta, a.case_id or Path(a.study).name))
    if a.remove_raw:
        # Only after both files are written: the surface is the one copy until then.
        for f in glob.glob(os.path.join(a.study, "case_*", "postProcessing", FUNCTION, "*", "*.vtk")):
            os.remove(f)
    cov = list(meta["coverage"].values())
    err = [v["p99_abs_m"] for v in meta["height_error"].values()]
    print(f"ped_field: {field.shape[0]} directions x {field.shape[1]} heights, "
          f"coverage {min(cov) if cov else 0:.3f}-{max(cov) if cov else 0:.3f}"
          + (f", height error p99 <= {max(err):.3f} m" if err else "")
          + f", missing {len(meta['missing'])} -> {a.out}")
    return 0 if not meta["missing"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

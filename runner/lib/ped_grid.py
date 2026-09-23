"""The pedestrian sampling grid: a REGULAR lattice draped over the terrain.

A viewer that cycles wind directions, and a dataset that compares cases, need
the same points in every direction and every case: a 2 m lattice over the
1008 m core, h m above grade. OpenFOAM has no "regular grid draped on a
surface" primitive, and sampling 500k explicit points is far too slow (see
surfaces_dict). So the work is split:

  here           the terrain sheet, checked and cropped to the core, and a
                 foamPostProcess dictionary cutting a distanceSurface h m above
                 it -- OpenFOAM's own cellPoint values on an iso-surface;
  ped_field.py   that surface read back onto the lattice, triangle by triangle.

The PHYSICS stays in OpenFOAM: nothing about the field is estimated twice.

Why the terrain lookup can be exact: the sheet is written by the geometry
builder from a REGULAR height lattice (MetaFOAM.Site's Extrude.TerrainSheet over
a fixed stride -- `terrain_stride_m` in the site report, 8 m on the v2
campaign), so recovering that lattice turns the drape into a bilinear lookup.
ped_field uses it to check that every sample really sits h above grade, and
`covers` uses it to refuse a sheet that does not span the core. Regularity is
CHECKED, not assumed: a sheet that is not a lattice is refused by name.

WHICH SHEET. The input is the geometry builder's own `<site>_terrain.stl`, NOT
the `ground.stl` staged into the mesh case. They are not the same surface: the
mesher partitions the terrain by land-cover class into rough_core_* and
rough_ring_* patches so each can carry its own roughness, and `ground.stl` is
only what is LEFT of the sheet afterwards. Measured on v2-000c178c579bf034,
ground.stl holds 45,314 of the sheet's 211,250 triangles and every one of them
lies between radius 924 m and 1301 m -- the outer ring band, with nothing at all
under the 1008 m sampled core. The campaign's first pedestrian sampler cut its
distanceSurface from exactly that file, and so sampled nothing inside the core,
silently. `covers` below refuses it by name.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
from dataclasses import dataclass

import numpy as np


def read_stl(path: str) -> np.ndarray:
    """Triangles as (n, 3, 3). Reads both STL flavours: the geometry builder
    writes binary, blueCFD's staged copy of ground.stl is ascii."""
    with open(path, "rb") as fh:
        head = fh.read(84)
        if len(head) < 84:
            raise ValueError(f"{path}: too short to be an STL")
        # A binary STL declares its triangle count in bytes 80-84, and the file
        # is then exactly 84 + 50n. An ascii file starting "solid" can still
        # have anything in those bytes, so the LENGTH is what decides.
        count = struct.unpack("<I", head[80:84])[0]
        fh.seek(0, 2)
        size = fh.tell()
        if size == 84 + 50 * count:
            fh.seek(84)
            raw = np.frombuffer(fh.read(50 * count), dtype=np.uint8).reshape(count, 50)
            return raw[:, 12:48].copy().view("<f4").reshape(count, 3, 3).astype(np.float64)

    with open(path, "r", errors="replace") as fh:
        verts = [
            [float(p) for p in line.split()[1:4]]
            for line in fh
            if line.lstrip().startswith("vertex")
        ]
    if not verts or len(verts) % 3:
        raise ValueError(f"{path}: {len(verts)} ascii vertices is not a whole number of triangles")
    return np.asarray(verts, dtype=np.float64).reshape(-1, 3, 3)


@dataclass(frozen=True)
class HeightField:
    """A regular terrain lattice: z[j, i] at x0 + i*dx, y0 + j*dy."""

    x0: float
    y0: float
    dx: float
    dy: float
    z: np.ndarray

    @property
    def stride(self) -> tuple[float, float]:
        return self.dx, self.dy


def heightfield(tris: np.ndarray, tol: float = 1e-3) -> HeightField:
    """Recover the regular lattice the terrain sheet was written from.

    Refuses, naming the reason, when the vertices are not a lattice or when the
    lattice has holes -- both would make the bilinear lookup below quietly wrong
    rather than absent.
    """
    pts = tris.reshape(-1, 3)
    xs = np.unique(np.round(pts[:, 0] / tol) * tol)
    ys = np.unique(np.round(pts[:, 1] / tol) * tol)
    if xs.size < 2 or ys.size < 2:
        raise ValueError("terrain is a single row or column of vertices, not a sheet")

    ddx, ddy = np.diff(xs), np.diff(ys)
    dx, dy = float(np.median(ddx)), float(np.median(ddy))
    # "Regular" has to be judged against the spacing, not an absolute epsilon: an
    # 8 m stride carries float32 rounding of ~1e-6 m from the STL, and a sheet
    # that is merely DENSE somewhere is not a lattice we can index into.
    if not (np.allclose(ddx, dx, rtol=1e-3, atol=tol) and np.allclose(ddy, dy, rtol=1e-3, atol=tol)):
        raise ValueError(
            f"terrain vertices are not on a regular lattice "
            f"(x spacing {ddx.min():.3f}-{ddx.max():.3f} m, y {ddy.min():.3f}-{ddy.max():.3f} m): "
            "the drape needs the builder's height grid, so this sheet cannot be sampled this way"
        )

    ix = np.rint((pts[:, 0] - xs[0]) / dx).astype(np.int64)
    iy = np.rint((pts[:, 1] - ys[0]) / dy).astype(np.int64)
    z = np.full((ys.size, xs.size), np.nan)
    z[iy, ix] = pts[:, 2]
    if np.isnan(z).any():
        missing = int(np.isnan(z).sum())
        raise ValueError(f"terrain lattice has {missing} empty node(s): the sheet is not a full grid")
    return HeightField(float(xs[0]), float(ys[0]), dx, dy, z)


def drape(hf: HeightField, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Terrain z at (x, y), bilinear on the lattice. Points outside it clamp to
    the edge -- the sheet spans 1304 m and the core is 504, so this only ever
    bites a caller asking for something the campaign does not sample."""
    fx = np.clip((np.asarray(x, dtype=np.float64) - hf.x0) / hf.dx, 0, hf.z.shape[1] - 1)
    fy = np.clip((np.asarray(y, dtype=np.float64) - hf.y0) / hf.dy, 0, hf.z.shape[0] - 1)
    i0 = np.floor(fx).astype(np.int64)
    j0 = np.floor(fy).astype(np.int64)
    i1 = np.minimum(i0 + 1, hf.z.shape[1] - 1)
    j1 = np.minimum(j0 + 1, hf.z.shape[0] - 1)
    tx, ty = fx - i0, fy - j0
    return (hf.z[j0, i0] * (1 - tx) * (1 - ty) + hf.z[j0, i1] * tx * (1 - ty)
            + hf.z[j1, i0] * (1 - tx) * ty + hf.z[j1, i1] * tx * ty)


def covers(hf: HeightField, half: float) -> None:
    """Refuse a sheet that does not span the core we are about to sample.

    The drape clamps outside the lattice, so a sheet that stops short would
    return the edge height for every point beyond it -- a flat apron, silently,
    over however much of the core is missing.
    """
    x1 = hf.x0 + hf.dx * (hf.z.shape[1] - 1)
    y1 = hf.y0 + hf.dy * (hf.z.shape[0] - 1)
    if hf.x0 > -half or x1 < half or hf.y0 > -half or y1 < half:
        raise ValueError(
            f"terrain spans x {hf.x0:.0f}..{x1:.0f}, y {hf.y0:.0f}..{y1:.0f} m, "
            f"which does not cover the +/-{half:.0f} m core: this is the wrong surface "
            "(the mesh's ground.stl is the land-cover leftover, not the terrain sheet -- "
            "use the builder's <site>_terrain.stl)"
        )


def grid_axis(half: float, spacing: float) -> np.ndarray:
    """Cell-centre coordinates spanning [-half, half].

    Centres, not nodes: a node grid puts samples exactly ON the core boundary,
    where half the stencil is outside the sampled region, and makes the count
    odd (505 at 2 m) for no gain. 504 centres at 2 m tile the core exactly.
    """
    n = int(round(2 * half / spacing))
    if n < 1:
        raise ValueError(f"spacing {spacing} m does not fit inside a {2 * half} m core")
    return -half + spacing * (np.arange(n) + 0.5)


def core_sheet(tris: np.ndarray, half: float, margin: float = 24.0) -> np.ndarray:
    """The terrain triangles within `margin` of the core -- the surface the
    pedestrian sample is cut against.

    Cropped because the cut is made wherever the surface is: the whole sheet
    would put three quarters of the sample in the buffer, which nothing reads.
    The unsigned distance rounds off around the sheet's boundary; a 24 m margin
    (three terrain strides) keeps that rounding well outside the grid.
    """
    lim = half + margin
    keep = (np.abs(tris[:, :, :2]) <= lim).all(axis=(1, 2))
    return tris[keep]


def write_stl(path: str, tris: np.ndarray) -> None:
    """Binary STL. Normals are left zero: OpenFOAM recomputes them from the
    vertices, and the distance it measures is unsigned."""
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["v"] = tris
    with open(path, "wb") as fh:
        fh.write(b"eddy3d pedestrian core sheet".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        fh.write(rec.tobytes())


def surface_name(h: float) -> str:
    """`1.75` -> `ped175`: a surface name OpenFOAM accepts (no dot, no sign)."""
    return "ped" + format(h, "g").replace(".", "").replace("-", "m")


def surfaces_dict(heights: list[float], stl_name: str = "pedCore.stl",
                  fields: tuple[str, ...] = ("U",)) -> str:
    """The foamPostProcess dictionary: one distanceSurface per height.

    A surface, not a point list. A `sets` or `probes` sample of the 2 m grid
    costs one cell search per point, and OpenFOAM 12's search on a
    snappyHexMesh mesh runs ~15 ms a point whatever the point (Eddy3D,
    ProbeFunctionObject.cs). At 508,032 points it never finished: measured on
    v2-1410516cea4c5d7b (4.8M cells), 20 min serial and 3.5 min per rank on 8
    ranks, both still searching when stopped. The iso-surface is computed cell
    by cell instead: the same core, 8 ranks, 32 s including the mesh load.

    `interpolate true` puts cellPoint values on the surface's vertices, and each
    surface triangle lies within one cell's tet decomposition, so the linear
    interpolation ped_field.py does inside a triangle is what cellPoint gives
    there -- and what ParaView draws for the same slice. The grid is read off
    the surface, not re-estimated from it. `vtk`, not `raw`, because raw drops
    the triangles, and the triangles are what keep buildings as holes.

    Still run under -parallel on the decomposed case: serial, the cell search
    tree alone is minutes on a campaign mesh.
    """
    entries = []
    for h in heights:
        entries.append(f"""        {surface_name(h)}
        {{
            // {h:g} m above grade: the unsigned distance to the terrain SHEET
            // (open, so it cannot be signed). Below the sheet there is no
            // mesh, so only the surface above it is cut.
            type            distanceSurface;
            surfaceType     triSurfaceMesh;
            file            "{stl_name}";
            distance        {h:g};
            signed          false;
            interpolate     true;
        }}""")
    joined = "\n".join(entries)
    return f"""FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      pedGridFO;
}}
// Written by runner/lib/ped_grid.py; ped_field.py reads the result back onto a
// regular grid, so every direction and every case share the same points.
pedestrianSurface
{{
    type            surfaces;
    libs            ("libsampling.so");
    writeControl    writeTime;
    surfaceFormat   vtk;
    interpolationScheme cellPoint;
    fields          ({" ".join(fields)});
    surfaces
    (
{joined}
    );
}}
"""


def build(stl_path: str, half: float, spacing: float):
    """The checked terrain lattice, the grid axes, and the sheet cropped to the core."""
    tris = read_stl(stl_path)
    hf = heightfield(tris)
    covers(hf, half)
    return hf, grid_axis(half, spacing), grid_axis(half, spacing), core_sheet(tris, half)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stl", help="the builder's terrain sheet, <site>_terrain.stl "
                                "(NOT the mesh's ground.stl -- see the module docstring)")
    ap.add_argument("--half", type=float, default=504.0, help="core half-extent, m (default 504)")
    ap.add_argument("--spacing", type=float, default=2.0, help="grid spacing, m (default 2)")
    ap.add_argument("--heights", default="1.5,1.75", help="metres above grade, comma separated")
    ap.add_argument("--out", required=True, help="the foamPostProcess dictionary to write")
    ap.add_argument("--out-stl", required=True,
                    help="the cropped sheet; the dictionary names it by basename, so it "
                         "belongs in each direction case's constant/triSurface/")
    ap.add_argument("--grid-json", required=True, help="where to record the grid the reader needs")
    a = ap.parse_args()

    heights = [float(h) for h in a.heights.split(",") if h.strip()]
    hf, xs, ys, sheet = build(a.stl, a.half, a.spacing)
    write_stl(a.out_stl, sheet)
    with open(a.out, "w") as fh:
        fh.write(surfaces_dict(heights, os.path.basename(a.out_stl)))
    with open(a.grid_json, "w") as fh:
        json.dump({
            "half_m": a.half, "spacing_m": a.spacing, "heights_m": heights,
            "nx": int(xs.size), "ny": int(ys.size),
            "x0": float(xs[0]), "y0": float(ys[0]),
            "terrain_stride_m": list(hf.stride),
            "surfaces": {surface_name(h): h for h in heights},
            "order": "row-major, x fastest, y ascending",
        }, fh, indent=2)

    print(f"ped_grid: {xs.size}x{ys.size} grid at {a.spacing:g} m, heights {heights}, "
          f"core sheet {len(sheet)} triangles on a {hf.stride[0]:g} m lattice -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



"""Reading OpenFOAM's pedestrian surfaces onto the grid, against surfaces whose answer is known.

The failure worth fearing is a field that is complete, plausible and wrong: a
value placed on the wrong grid point, a building bridged over by interpolation,
a direction dropped so the next one takes its label. Each test writes a surface
in the layout OpenFOAM's vtk writer uses, carrying a U that is LINEAR in x and y
-- so a correct triangle-linear read reproduces it exactly anywhere inside the
surface -- and checks the gathered array against that function.
"""

from __future__ import annotations

import gzip
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner" / "lib"))

import ped_field  # noqa: E402
import ped_grid  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "runner" / "lib" / "ped_field.py"


def grid(n=8, spacing=2.0, heights=(1.5, 1.75)):
    half = n * spacing / 2
    return {"half_m": half, "spacing_m": spacing, "heights_m": list(heights),
            "nx": n, "ny": n, "x0": -half + spacing / 2, "y0": -half + spacing / 2,
            "surfaces": {ped_grid.surface_name(h): h for h in heights},
            "order": "row-major, x fastest, y ascending"}


def U(x, y, deg, h):
    """Linear in x and y, and different per direction and height, so a value in
    the wrong slot of any axis cannot match."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    return np.stack([0.01 * x + deg, 0.02 * y + h, 0.001 * deg + 0 * x], axis=-1)


def ground(x, y):
    return 3.0 + 0.05 * np.asarray(x) - 0.02 * np.asarray(y)


def mesh(half, step=3.0, seed=0, hole=None):
    """A jittered triangulated sheet over +/-half, coarser than the grid, the
    way an iso-surface cut from 3 m cells is. Quads are left as quads (the
    writer emits polygons) so the reader's fan is exercised. `hole` removes
    every polygon touching a box -- a building."""
    rng = np.random.default_rng(seed)
    n = int(np.ceil(2 * half / step)) + 1
    xs = np.linspace(-half - 1, half + 1, n)
    gx, gy = np.meshgrid(xs, xs, indexing="xy")
    jit = (xs[1] - xs[0]) * 0.2
    gx = gx + rng.uniform(-jit, jit, gx.shape) * (np.abs(gx) < half)
    gy = gy + rng.uniform(-jit, jit, gy.shape) * (np.abs(gy) < half)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    polys = []
    for j in range(n - 1):
        for i in range(n - 1):
            a, b, c, d = j * n + i, j * n + i + 1, (j + 1) * n + i + 1, (j + 1) * n + i
            if hole is not None:
                q = pts[[a, b, c, d]]
                (x0, y0), (x1, y1) = hole
                if ((q[:, 0] > x0) & (q[:, 0] < x1) & (q[:, 1] > y0) & (q[:, 1] < y1)).any():
                    continue
            polys.append([a, b, c, d] if (i + j) % 3 == 0 else None)
            if polys[-1] is None:
                polys[-1] = [a, b, c]
                polys.append([a, c, d])
    return pts, polys


def write_vtk(path: Path, pts2, polys, deg, h, z_of=ground, cell_data=False):
    z = z_of(pts2[:, 0], pts2[:, 1]) + h
    pts = np.column_stack([pts2, z])
    vals = U(pts2[:, 0], pts2[:, 1], deg, h)
    size = sum(len(p) + 1 for p in polys)
    out = ["# vtk DataFile Version 2.0", "sampleSurface", "ASCII", "DATASET POLYDATA",
           f"POINTS {len(pts)} float"]
    out += [" ".join(f"{v:.6g}" for v in row) for row in pts]
    out.append(f"POLYGONS {len(polys)} {size}")
    out += [" ".join(map(str, [len(p), *p])) for p in polys]
    if cell_data:
        out += [f"CELL_DATA {len(polys)}", "FIELD attributes 1", f"U 3 {len(polys)} float"]
        out += ["1 2 3"] * len(polys)
    else:
        out += [f"POINT_DATA {len(pts)}", "FIELD attributes 1", f"U 3 {len(pts)} float"]
        out += [" ".join(f"{v:.9g}" for v in row) for row in vals]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


def study(tmp_path, g, degs=(0, 45, 90), skip=(), manifest=True, time="560", hole=None):
    """A study as the solver leaves it: case_<deg>/postProcessing/pedestrianSurface/<time>/<surface>.vtk"""
    root = tmp_path / "study"
    half = g["nx"] * g["spacing_m"] / 2
    pts2, polys = mesh(half, hole=hole)
    for deg in degs:
        case = root / f"case_{deg:03d}"
        (case / "system").mkdir(parents=True)
        if deg in skip:
            continue
        for name, h in g["surfaces"].items():
            write_vtk(case / "postProcessing" / "pedestrianSurface" / time / f"{name}.vtk",
                      pts2, polys, deg, h)
    if manifest:
        (root / "eddy3d-study.json").write_text(json.dumps({
            "directions": [{"degrees": d, "caseName": f"case_{d:03d}", "uref": 5} for d in degs],
            "abl": {"uref": 5, "zref": 10, "z0": 0.5}}))
    gp = tmp_path / "grid.json"
    gp.write_text(json.dumps(g))
    xs = g["x0"] + g["spacing_m"] * np.arange(g["nx"])
    gx, gy = np.meshgrid(xs, xs, indexing="xy")
    return root, gp, gx, gy


# ── reading one surface onto the grid ────────────────────────────────────────

def test_a_linear_field_is_reproduced_exactly_at_every_grid_point(tmp_path):
    g = grid(n=16)
    root, gp, gx, gy = study(tmp_path, g)
    field, meta = ped_field.collect(root, gp)

    assert field.shape == (3, 2, 16, 16, 3)
    for di, deg in enumerate((0, 45, 90)):
        for hi, h in enumerate((1.5, 1.75)):
            assert np.allclose(field[di, hi], U(gx, gy, deg, h), atol=1e-4), (deg, h)
    assert meta["missing"] == []
    assert all(v == 1.0 for v in meta["coverage"].values())


def test_a_building_stays_a_hole_instead_of_being_bridged(tmp_path):
    # A re-triangulation (Delaunay over the vertices) would span the gap and
    # invent wind inside the building. The surface's own triangles do not.
    g = grid(n=16)
    hole = ((-6.0, -4.0), (5.0, 6.0))
    root, gp, gx, gy = study(tmp_path, g, degs=(0,), hole=hole)
    field, meta = ped_field.collect(root, gp)

    deep = (gx > -2) & (gx < 1) & (gy > 0) & (gy < 2)          # well inside the removed box
    assert deep.any()
    assert np.isnan(field[0, 0][deep]).all()
    ok = np.isfinite(field[0, 0, :, :, 0])
    assert np.allclose(field[0, 0][ok], U(gx, gy, 0, 1.5)[ok], atol=1e-4)
    assert meta["coverage"]["case_000@1.5"] < 1.0


def two_sheets(ground_z=10.0, h=1.75):
    """The unsigned distance cut on BOTH sides of the terrain: the real sheet h
    above it, and a phantom h below it wherever the mesh has cells under the
    ground (the v3 box's diagonal directions, v2-1410516cea4c5d7b case_045)."""
    pts2, polys = mesh(6.0)
    n = len(pts2)
    above = np.column_stack([pts2, np.full(n, ground_z + h)])
    below = np.column_stack([pts2, np.full(n, ground_z - h)])
    tris = []
    for p in polys:
        for q in range(1, len(p) - 1):
            tris.append((p[0], p[q], p[q + 1]))
    tris = np.array(tris)
    pts = np.vstack([below, above])
    tris = np.vstack([tris, tris + n])
    vals = np.vstack([np.full((n, 1), 9.0), np.full((n, 1), 2.0)])   # 9 = the phantom
    return pts, tris, vals


def test_the_sheet_at_grade_plus_h_wins_over_one_under_the_terrain():
    # "Keep the lowest" -- the obvious rule, and the first one written -- put
    # every value of case_045 2h below its label. The label is the tie-breaker.
    g = grid(n=6)
    pts, tris, vals = two_sheets()
    ground = np.full((6, 6), 10.0)
    u, z, under = ped_field.rasterize(pts, tris, vals, g, target=ground + 1.75, floor=ground)
    assert np.allclose(u, 2.0) and np.allclose(z, 11.75)
    assert under.all(), "the mesh under the terrain is reported, not silently stepped around"


def test_without_a_terrain_the_lowest_sheet_is_kept_and_nothing_is_flagged():
    g = grid(n=6)
    pts, tris, vals = two_sheets()
    u, z, under = ped_field.rasterize(pts, tris, vals, g)
    assert np.allclose(z, 8.25) and not under.any()


def test_a_surface_written_without_point_values_is_refused(tmp_path):
    # CELL_DATA is one value per face -- a face average, not cellPoint at the
    # vertex. Read as if it were, it would pass for a field.
    pts2, polys = mesh(4.0)
    p = tmp_path / "s.vtk"
    write_vtk(p, pts2, polys, 0, 1.5, cell_data=True)
    with pytest.raises(ValueError, match="POINT_DATA"):
        ped_field.read_vtk(p)


def test_every_sample_is_checked_against_its_height_above_grade(tmp_path):
    # A surface at the wrong height looks exactly like a right one. With the
    # terrain sheet, collect measures it.
    g = grid(n=8)
    root, gp, _, _ = study(tmp_path, g, degs=(0,))
    stl = tmp_path / "terrain.stl"
    tris = []
    xs = np.arange(-24.0, 24.1, 8.0)
    for j in range(len(xs) - 1):
        for i in range(len(xs) - 1):
            q = [(xs[a], xs[b], float(ground(xs[a], xs[b]))) for a, b in
                 ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))]
            tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
    ped_grid.write_stl(str(stl), np.asarray(tris, dtype=np.float32))

    _, meta = ped_field.collect(root, gp, stl)
    for key, err in meta["height_error"].items():
        assert abs(err["median_m"]) < 1e-3 and err["p99_abs_m"] < 1e-3, key
        assert err["under_terrain"] == 0.0, key

    # And a surface cut at the wrong height is measured, not passed: shift one
    # direction's sheet up by a metre and the check says so.
    pts2, polys = mesh(8.0)
    write_vtk(root / "case_000" / "postProcessing" / "pedestrianSurface" / "560" / "ped15.vtk",
              pts2, polys, 0, 2.5)
    _, meta = ped_field.collect(root, gp, stl)
    assert meta["height_error"]["case_000@1.5"]["median_m"] == pytest.approx(1.0, abs=1e-3)


# ── directions ───────────────────────────────────────────────────────────────

def test_a_direction_without_a_sample_keeps_its_slot(tmp_path):
    # Dropping it would make case_090's field read as case_045's.
    g = grid()
    root, gp, gx, gy = study(tmp_path, g, skip=(45,))
    field, meta = ped_field.collect(root, gp)

    assert [d["deg"] for d in meta["directions"]] == [0, 45, 90]
    assert np.isnan(field[1]).all()
    assert np.allclose(field[2, 0], U(gx, gy, 90, 1.5), atol=1e-4)
    assert "case_045@1.5" in meta["missing"]


def test_the_study_manifest_is_the_authority_on_the_angle(tmp_path):
    # 11.25 deg lives on disk as case_011; the directory name alone would say 11.
    g = grid(n=4)
    root, gp, _, _ = study(tmp_path, g, degs=(0, 11))
    m = json.loads((root / "eddy3d-study.json").read_text())
    m["directions"][1]["degrees"] = 11.25
    m["directions"][1]["z0"] = 0.03
    (root / "eddy3d-study.json").write_text(json.dumps(m))

    _, meta = ped_field.collect(root, gp)
    assert [d["deg"] for d in meta["directions"]] == [0, 11.25]
    assert meta["directions"][1]["z0"] == 0.03
    assert meta["directions"][0]["z0"] == 0.5, "falls back to the study's ABL z0"


def test_without_a_manifest_the_directory_name_gives_the_angle(tmp_path):
    g = grid(n=4)
    root, gp, _, _ = study(tmp_path, g, degs=(0, 90, 270), manifest=False)
    _, meta = ped_field.collect(root, gp)
    assert [d["deg"] for d in meta["directions"]] == [0, 90, 270]


def test_the_latest_sample_time_is_the_one_used(tmp_path):
    g = grid(n=4)
    root, gp, gx, gy = study(tmp_path, g, degs=(0,), time="400")
    pts2, polys = mesh(4.0)
    for name, h in g["surfaces"].items():
        write_vtk(root / "case_000" / "postProcessing" / "pedestrianSurface" / "560" / f"{name}.vtk",
                  pts2, polys, 7, h)
    field, meta = ped_field.collect(root, gp)
    assert np.allclose(field[0, 0], U(gx, gy, 7, 1.5), atol=1e-4)
    assert meta["directions"][0]["time"] == "560"


def test_the_reference_speed_is_the_inlet_log_law_at_that_height():
    # U(z) = Uref ln((z+z0)/z0) / ln((zref+z0)/z0): at zref it is Uref, and it
    # falls toward the ground.
    assert ped_field.u_ref(5, 10, 0.5, 10) == pytest.approx(5)
    got = ped_field.u_ref(5, 10, 0.5, 1.75)
    assert got == pytest.approx(5 * math.log(2.25 / 0.5) / math.log(10.5 / 0.5))
    assert got < 5
    assert ped_field.u_ref(5, 10, None, 1.75) is None, "no z0, no reference: not a guess"


# ── the viewer bundle ────────────────────────────────────────────────────────

def read_bundle(blob):
    raw = gzip.decompress(blob)
    assert raw[:4] == b"WFLD"
    version, hlen = struct.unpack("<II", raw[4:12])
    header = json.loads(raw[12:12 + hlen])
    return version, header, raw[12 + hlen:]


def test_the_viewer_bundle_round_trips_magnitude_holes_and_vectors(tmp_path):
    g = grid(n=16)
    root, gp, gx, gy = study(tmp_path, g, degs=(0, 90), hole=((-6.0, -4.0), (5.0, 6.0)))
    field, meta = ped_field.collect(root, gp)

    version, h, body = read_bundle(ped_field.bundle(field, meta, "v2-abc", stride=4))
    assert version == 1 and h["case_id"] == "v2-abc"
    assert h["height_m"] == 1.75, "the published height"
    n, vn = 16 * 16, h["vector"]["nx"] * h["vector"]["ny"]
    assert len(body) == 2 * (n + 4 * vn)

    hole = ~np.isfinite(field[0, 1, :, :, 0])
    assert hole.any()
    for k, deg in enumerate((0, 90)):
        block = body[k * (n + 4 * vn):(k + 1) * (n + 4 * vn)]
        q = np.frombuffer(block[:n], np.uint8).reshape(16, 16)
        truth = np.linalg.norm(U(gx, gy, deg, 1.75), axis=-1)
        assert (q[hole] == ped_field.NODATA).all(), "a building is no-data, not zero wind"
        got = q[~hole].astype(float) / 254 * h["vmax"]
        assert np.allclose(np.minimum(got, h["vmax"]), np.minimum(truth[~hole], h["vmax"]),
                           atol=h["vmax"] / 254 + 1e-6)
        vec = np.frombuffer(block[n:], "<i2").reshape(h["vector"]["ny"], h["vector"]["nx"], 2)
        o = h["vector"]["offset"]
        assert vec[0, 0, 0] / 100 == pytest.approx(U(gx, gy, deg, 1.75)[o, o, 0], abs=0.006)
    assert h["directions"][0]["u_ref"] == pytest.approx(ped_field.u_ref(5, 10, 0.5, 1.75))


# ── the command line, as the runner calls it ─────────────────────────────────

def test_the_cli_writes_the_dataset_and_removes_surfaces_only_on_request(tmp_path):
    g = grid(n=4)
    root, gp, _, _ = study(tmp_path, g, degs=(0, 90))
    out = tmp_path / "ped"
    run = lambda *extra: subprocess.run(  # noqa: E731
        [sys.executable, str(MODULE), str(root), "--grid", str(gp), "--out", str(out),
         "--bundle", str(tmp_path / "v.wfld"), *extra], capture_output=True, text=True)

    p = run()
    assert p.returncode == 0, p.stderr
    assert np.load(out / "U.npz")["U"].shape == (2, 2, 4, 4, 3)
    assert json.loads((out / "meta.json").read_text())["format"] == "pedestrian-field/1"
    assert list(root.rglob("*.vtk")), "surfaces kept by default"
    p = run("--remove-raw")
    assert p.returncode == 0, p.stderr
    assert not list(root.rglob("*.vtk"))


def test_the_cli_says_so_when_a_direction_is_missing(tmp_path):
    g = grid(n=4)
    root, gp, _, _ = study(tmp_path, g, degs=(0, 90), skip=(90,))
    p = subprocess.run([sys.executable, str(MODULE), str(root), "--grid", str(gp),
                        "--out", str(tmp_path / "ped")], capture_output=True, text=True)
    assert p.returncode == 3
    assert "missing 2" in p.stdout

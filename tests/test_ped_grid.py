"""The pedestrian sampling grid, against terrain whose answer is known.

The drape is the one piece of this that can be silently wrong: a sampler that
returns plausible numbers at the wrong HEIGHT above ground looks exactly like a
correct one until somebody compares two cases. So every test here builds a
terrain whose z is a closed-form function of (x, y) and checks the draped point
against that function, rather than against another run of the same code.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner" / "lib"))

import ped_grid  # noqa: E402

MODULE = Path(__file__).resolve().parents[1] / "runner" / "lib" / "ped_grid.py"


def sheet(fn, half=32.0, stride=8.0):
    """A terrain sheet over a regular lattice, z = fn(x, y), split into triangles
    the way an extruded height grid is."""
    n = int(round(2 * half / stride)) + 1
    xs = -half + stride * np.arange(n)
    ys = -half + stride * np.arange(n)
    tris = []
    for j in range(n - 1):
        for i in range(n - 1):
            p = [(xs[a], ys[b], fn(xs[a], ys[b])) for a, b in
                 ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))]
            tris.append([p[0], p[1], p[2]])
            tris.append([p[0], p[2], p[3]])
    return np.asarray(tris, dtype=np.float64)


def write_binary_stl(path: Path, tris: np.ndarray):
    with open(path, "wb") as fh:
        fh.write(b"eddy3d test terrain".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 1.0))
            for v in t:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")


def write_ascii_stl(path: Path, tris: np.ndarray):
    out = ["solid ascii_stl"]
    for t in tris:
        out.append(" facet normal 0 0 1\n  outer loop")
        out += [f"   vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in t]
        out.append("  endloop\n endfacet")
    out.append("endsolid")
    path.write_text("\n".join(out))


FLAT = lambda x, y: 100.0                      # noqa: E731
TILTED = lambda x, y: 100.0 + 0.05 * x         # noqa: E731  a 5 % slope
SADDLE = lambda x, y: 100.0 + 0.002 * x * y    # noqa: E731  bilinear, so exact on the lattice


# ── reading the sheet ────────────────────────────────────────────────────────

def test_both_stl_flavours_read_to_the_same_triangles(tmp_path):
    # The builder writes binary; blueCFD's staged ground.stl is ascii. A reader
    # that silently mangled one would drape against garbage.
    tris = sheet(TILTED)
    write_binary_stl(tmp_path / "b.stl", tris)
    write_ascii_stl(tmp_path / "a.stl", tris)

    binary = ped_grid.read_stl(str(tmp_path / "b.stl"))
    ascii_ = ped_grid.read_stl(str(tmp_path / "a.stl"))

    assert binary.shape == tris.shape
    assert np.allclose(binary, tris, atol=1e-3)
    assert np.allclose(ascii_, tris, atol=1e-3)


def test_an_ascii_file_whose_header_bytes_look_like_a_count_is_still_ascii(tmp_path):
    # The control for the flavour test: "solid" says nothing, and bytes 80-84 of
    # an ascii file are just more text. Only the file LENGTH decides.
    write_ascii_stl(tmp_path / "a.stl", sheet(FLAT))
    assert ped_grid.read_stl(str(tmp_path / "a.stl")).shape[1:] == (3, 3)


# ── recovering the lattice ───────────────────────────────────────────────────

def test_the_builders_stride_is_recovered_from_the_sheet(tmp_path):
    hf = ped_grid.heightfield(sheet(SADDLE, half=32.0, stride=8.0))
    assert hf.stride == pytest.approx((8.0, 8.0))
    assert hf.z.shape == (9, 9)


def test_a_sheet_that_is_not_a_lattice_is_REFUSED_not_guessed():
    # The failure this protects against: nearest-vertex on a non-lattice sheet
    # returns a plausible height that is wrong by (slope x half the spacing),
    # which no output would show. Refusing names the sheet instead.
    tris = sheet(TILTED)
    tris[0, 0, 0] += 3.1                     # one vertex off the grid
    with pytest.raises(ValueError, match="not on a regular lattice"):
        ped_grid.heightfield(tris)


def test_a_lattice_with_a_hole_is_refused():
    tris = sheet(TILTED)
    with pytest.raises(ValueError, match="empty node"):
        ped_grid.heightfield(tris[:-6])      # drop a corner's triangles


# ── the drape itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn,name", [(FLAT, "flat"), (TILTED, "tilted"), (SADDLE, "saddle")])
def test_the_draped_height_matches_the_terrain_it_was_built_from(fn, name):
    # Closed form, not a second run of the same code. All three are bilinear in
    # each cell, so the lattice lookup is exact rather than merely close.
    hf = ped_grid.heightfield(sheet(fn))
    rng = np.random.default_rng(0)
    x = rng.uniform(-31, 31, 500)
    y = rng.uniform(-31, 31, 500)

    got = ped_grid.drape(hf, x, y)

    assert np.allclose(got, [fn(a, b) for a, b in zip(x, y)], atol=1e-6), name


def test_the_cropped_sheet_is_the_terrain_under_the_core_and_nothing_else(tmp_path):
    # The cut is made wherever the surface is, so the sheet handed to OpenFOAM
    # decides where the sample lands: the core plus a margin, unchanged in z.
    tris = sheet(TILTED, half=64.0, stride=8.0)
    core = ped_grid.core_sheet(tris, half=24.0, margin=8.0)

    assert 0 < len(core) < len(tris)
    assert np.abs(core[:, :, :2]).max() <= 32.0
    assert np.allclose(core[:, :, 2], TILTED(core[:, :, 0], core[:, :, 1])), "z untouched"
    # And it still covers the core, or the sample would stop short of its edge.
    ped_grid.covers(ped_grid.heightfield(core), half=24.0)


def test_the_written_sheet_reads_back_as_the_same_triangles(tmp_path):
    tris = ped_grid.core_sheet(sheet(SADDLE), half=16.0)
    ped_grid.write_stl(str(tmp_path / "core.stl"), tris)
    assert np.allclose(ped_grid.read_stl(str(tmp_path / "core.stl")), tris, atol=1e-4)


# ── the grid the viewer indexes into ─────────────────────────────────────────

def test_the_core_is_tiled_exactly_by_cell_centres():
    axis = ped_grid.grid_axis(504.0, 2.0)
    assert axis.size == 504
    assert axis[0] == pytest.approx(-503.0)
    assert axis[-1] == pytest.approx(503.0)
    assert np.allclose(np.diff(axis), 2.0)
    # Centres, so nothing sits on the boundary where half the stencil is outside.
    assert abs(axis).max() < 504.0


# ── the dictionary OpenFOAM reads ────────────────────────────────────────────

def test_the_dictionary_cuts_one_terrain_following_surface_per_height():
    text = ped_grid.surfaces_dict([1.5, 1.75], "pedCore.stl")

    assert "pedestrianSurface" in text and "type            surfaces;" in text
    assert "ped15" in text and "ped175" in text
    assert text.count("type            distanceSurface;") == 2
    assert "distance        1.75;" in text and "distance        1.5;" in text
    assert text.count('file            "pedCore.stl";') == 2
    # Unsigned: the sheet is open and cannot be signed (OpenFOAM fatals).
    assert text.count("signed          false;") == 2
    # Values on the vertices, cellPoint -- the reader needs POINT data, and
    # refuses a surface written without it.
    assert text.count("interpolate     true;") == 2
    assert "interpolationScheme cellPoint;" in text
    # vtk keeps the triangles; raw would drop them, and with them the holes.
    assert "surfaceFormat   vtk;" in text


def test_a_surface_name_survives_being_an_openfoam_keyword():
    assert ped_grid.surface_name(1.75) == "ped175"
    assert ped_grid.surface_name(1.5) == "ped15"
    assert "." not in ped_grid.surface_name(0.25)


# ── end to end, as the runner invokes it ─────────────────────────────────────

def test_the_cli_writes_the_dictionary_the_sheet_and_the_grid(tmp_path):
    write_binary_stl(tmp_path / "ground.stl", sheet(TILTED))
    out, meta, stl = tmp_path / "pedGridFO", tmp_path / "grid.json", tmp_path / "pedCore.stl"

    proc = subprocess.run(
        [sys.executable, str(MODULE), str(tmp_path / "ground.stl"),
         "--half", "16", "--spacing", "2", "--heights", "1.5,1.75",
         "--out", str(out), "--out-stl", str(stl), "--grid-json", str(meta)],
        capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert "ped175" in out.read_text()
    assert '"pedCore.stl"' in out.read_text(), "the dictionary names the sheet it was written with"
    assert len(ped_grid.read_stl(str(stl))) > 0
    grid = json.loads(meta.read_text())
    assert grid["nx"] == grid["ny"] == 16
    assert grid["heights_m"] == [1.5, 1.75]
    assert grid["surfaces"] == {"ped15": 1.5, "ped175": 1.75}
    assert grid["terrain_stride_m"] == [8.0, 8.0]
    assert grid["x0"] == pytest.approx(-15.0)
    assert grid["order"].startswith("row-major")


def test_a_spacing_that_does_not_fit_is_refused_rather_than_rounded():
    with pytest.raises(ValueError, match="does not fit"):
        ped_grid.grid_axis(1.0, 4.0)


# ── the wrong surface ────────────────────────────────────────────────────────

def test_a_sheet_that_does_not_reach_the_core_is_refused_by_name(tmp_path):
    """The field failure this module was written against.

    The mesher partitions the terrain by land-cover class, and `ground.stl` is
    the leftover. On v2-000c178c579bf034 every one of its 45,314 triangles lies
    between radius 924 m and 1301 m -- nothing under the 1008 m core. Draping
    against it would clamp to the lattice edge and return a flat apron over the
    whole sampled area, which plots as a perfectly smooth field rather than as
    an error.
    """
    # A sheet that exists only as an outer band, like ground.stl.
    write_binary_stl(tmp_path / "ring.stl", sheet(FLAT, half=32.0, stride=8.0))
    hf = ped_grid.heightfield(ped_grid.read_stl(str(tmp_path / "ring.stl")))

    with pytest.raises(ValueError, match="does not cover"):
        ped_grid.covers(hf, half=504.0)
    with pytest.raises(ValueError, match="ground.stl"):
        ped_grid.covers(hf, half=504.0)          # and it names the likely mistake

    with pytest.raises(ValueError, match="does not cover"):
        ped_grid.build(str(tmp_path / "ring.stl"), half=504.0, spacing=2.0)


def test_a_sheet_that_exactly_reaches_the_core_is_accepted(tmp_path):
    # The control: the guard must not reject the real geometry, where the sheet
    # spans 1304 m and the core is 504.
    write_binary_stl(tmp_path / "t.stl", sheet(TILTED, half=32.0, stride=8.0))
    hf = ped_grid.heightfield(ped_grid.read_stl(str(tmp_path / "t.stl")))
    ped_grid.covers(hf, half=32.0)
    ped_grid.covers(hf, half=16.0)

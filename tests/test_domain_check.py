"""A box domain turned to face the wind must stay on the terrain sheet.

Measured on v2-1410516cea4c5d7b (fixed-box-1008/of12-v3): the +/-1300 m box
turned for 45/135/225/315 deg reached past the +/-1304 m sheet, the sheet no
longer sealed the floor, and 64% of the core had mesh under the ground in those
four directions -- 0% in the four axis-aligned ones. These tests pin that
geometry, and the runner's refusal to build it.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner" / "lib"))

import domain_check  # noqa: E402

SHEET = (-1304.0, 1304.0, -1304.0, 1304.0)
V3_BOX = {"min": [-1300, -1300, -10], "max": [1300, 1300, 600]}
EIGHT = [0, 45, 90, 135, 180, 225, 270, 315]


def test_the_v3_box_leaks_in_exactly_its_four_diagonal_directions():
    bad = domain_check.leaks(V3_BOX, EIGHT, SHEET)
    assert [line.split(" ")[0] for line in bad] == ["45", "135", "225", "315"]
    # ~1838 m against a 1304 m sheet: over 500 m past it, not a rounding matter.
    assert all(int(line.split("reaches ")[1].split(" ")[0]) > 500 for line in bad)


def test_the_axis_directions_fit_so_the_smoke_spec_still_runs():
    smoke = json.loads((ROOT / "runner" / "smoke_spec.json").read_text())
    assert domain_check.leaks(smoke["domain"], smoke["wind"]["directions"], SHEET) == []
    assert domain_check.leaks(V3_BOX, [0, 90, 180, 270], SHEET) == []


def test_a_box_small_enough_to_turn_is_accepted_in_every_direction():
    # Half-diagonal inside the sheet: 900 * sqrt(2) = 1273 < 1304.
    small = {"min": [-900, -900, 0], "max": [900, 900, 600]}
    assert domain_check.leaks(small, [i * 11.25 for i in range(32)], SHEET) == []


def test_a_cylinder_is_not_turned_and_not_checked():
    assert domain_check.leaks({"shape": "cylinder", "min": [-1e4] * 3, "max": [1e4] * 3},
                              EIGHT, SHEET) == []


def write_sheet(path, half=1304.0):
    tris = [((-half, -half, 0), (half, -half, 0), (half, half, 0)),
            ((-half, -half, 0), (half, half, 0), (-half, half, 0))]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for v in t:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")


def test_the_cli_reads_the_sheet_and_exits_3_on_a_leak(tmp_path):
    write_sheet(tmp_path / "t.stl")
    assert domain_check.sheet_bounds(str(tmp_path / "t.stl")) == pytest.approx(SHEET)
    run = lambda dirs: subprocess.run(  # noqa: E731
        [sys.executable, str(ROOT / "runner" / "lib" / "domain_check.py"), str(tmp_path / "t.stl"),
         "--domain", json.dumps(V3_BOX), "--directions", dirs], capture_output=True, text=True)
    assert run("0,90").returncode == 0
    p = run("0,45")
    assert p.returncode == 3 and p.stdout.startswith("45 deg")


def test_the_runner_hands_a_recipe_case_back_instead_of_building_the_box():
    """It used to ignore the recipe and derive the v3 box for any case -- so a
    cyl-1008/of12-v4 case leased by a PACE worker would have been solved on the
    leaking box and archived labelled v4. 69 releases it, refunded, for a node
    that builds the recipe."""
    script = (ROOT / "runner" / "run_case.sh").read_text(encoding="utf-8", errors="replace")
    guard = script.index('HAS_DOMAIN=$(')
    # Before the geometry is meshed or anything is built from the spec.
    assert guard < script.index('"$CLI" build-case')
    block = script[guard:script.index("\nfi\n", guard)]
    assert "node_unfit" in block
    assert 'lib/domain_check.py" "$SCRATCH/terrain.stl"' in script

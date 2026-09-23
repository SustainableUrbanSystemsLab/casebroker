"""The worker <-> runner contract.

`script_runner` is the seam where the CFD plugs in: the broker never learns what
OpenFOAM is, and the runner never learns what a lease is. Everything crossing that
seam is a stdout convention and an exit code, which is exactly the kind of
interface that rots silently -- so it is pinned here, including the failure paths,
which are the ones that matter at 40,000 cases.

The distinction that earns its own test is retryable vs fatal. A node dying is
retryable; a tile whose STL is not watertight will fail identically on every
machine in the fleet, and cycling it through three workers helps nobody. Exit 64
is the agreed "do not retry" code.
"""

from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402
from casebroker.worker import FatalCaseError, script_runner  # noqa: E402


def write_script(tmp_path, body: str) -> str:
    # A python script rather than a shell one: these tests must run on the Windows
    # workstation as well as on the clusters, and sys.executable is always here.
    p = tmp_path / "runner.py"
    p.write_text(body, encoding="utf-8")
    launcher = tmp_path / ("runner.cmd" if os.name == "nt" else "runner.sh")
    if os.name == "nt":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{p}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{p}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


LEASE = {"case_id": "v2-abc", "lease_id": "L1", "attempt": 1,
         "spec": {"lat": 34.0, "lon": -84.0}}


def test_the_last_stdout_line_is_the_result(tmp_path):
    runner = script_runner(write_script(tmp_path, """
import json, os
print("meshing...")            # chatter before the result is normal and ignored
print("solving...")
print(json.dumps({"result_uri": "file:///out/" + os.environ["CASE_ID"],
                  "bytes": 17, "metrics": {"cells": 1858222}}))
"""))
    out = runner(LEASE, None)
    assert out["result_uri"] == "file:///out/v2-abc"
    assert out["metrics"]["cells"] == 1858222


def test_the_spec_reaches_the_script(tmp_path):
    runner = script_runner(write_script(tmp_path, """
import json, os, sys
spec = json.loads(os.environ["CASE_SPEC"])
stdin = json.loads(sys.stdin.read())
assert spec["lat"] == 34.0, spec
assert stdin["case_id"] == os.environ["CASE_ID"]
assert os.environ["LEASE_ID"] == "L1"
print(json.dumps({"result_uri": "ok"}))
"""))
    assert runner(LEASE, None)["result_uri"] == "ok"


def test_exit_64_is_fatal_and_everything_else_is_retryable(tmp_path):
    fatal = script_runner(write_script(tmp_path, """
import sys
print("geometry is not watertight", file=sys.stderr)
sys.exit(64)
"""))
    with pytest.raises(FatalCaseError) as e:
        fatal(LEASE, None)
    assert "not watertight" in str(e.value), "the reason must survive to the broker"

    transient = script_runner(write_script(tmp_path, """
import sys
print("image pull failed", file=sys.stderr)
sys.exit(1)
"""))
    with pytest.raises(RuntimeError) as e2:
        transient(LEASE, None)
    assert not isinstance(e2.value, FatalCaseError), "exit 1 must stay retryable"


def test_silence_and_garbage_are_both_errors_not_silent_successes(tmp_path):
    empty = script_runner(write_script(tmp_path, "pass\n"))
    with pytest.raises(RuntimeError, match="no output"):
        empty(LEASE, None)

    garbage = script_runner(write_script(tmp_path, 'print("all done!")\n'))
    with pytest.raises(RuntimeError, match="not JSON"):
        garbage(LEASE, None)


def test_a_fatal_runner_quarantines_the_case_on_its_first_attempt(tmp_path):
    """The behaviour the exit code exists for, checked against the real database
    rather than against the exception alone."""
    conn = db.connect(str(tmp_path / "b.sqlite"))
    cid = ids.case_id(34.0, -84.0, "r1")
    db.add_cases(conn, [{"case_id": cid, "spec": {}, "recipe": "r1",
                         "city_cluster": "c", "split": "train"}])
    lease = db.lease(conn, "w")[0]

    # What run_forever does with a FatalCaseError.
    db.fail(conn, lease.lease_id, "geometry is not watertight", retryable=False)

    row = conn.execute("SELECT state, attempts FROM cases").fetchone()
    assert row["state"] == "quarantined"
    assert row["attempts"] == 1, "a fatal case must not consume three workers first"
    assert db.lease(conn, "w2") == [], "and it must not be handed out again"


def test_run_forever_maps_a_fatal_runner_error_to_a_non_retryable_failure(tmp_path):
    """The link the other tests leave untested: run_forever must translate
    FatalCaseError into fail(retryable=False), not into an ordinary retry. If it
    does not, a broken tile burns three workers on every machine in the fleet."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    from casebroker.worker import Worker

    app = create_app(db_path=str(tmp_path / "w.sqlite"), tokens=None)
    client = TestClient(app)
    client.post("/v1/cases", json=[{"lat": 34.0, "lon": -84.0, "recipe": "r",
                                    "city_cluster": "c", "spec": {}}])

    w = Worker("http://testserver", None, worker_id="w1", heartbeat_seconds=3600)
    w.http = client                      # drive the app in-process, no socket
    w._post = lambda path, payload, retries=4: client.post(path, json=payload)

    def boom(lease, worker):
        raise FatalCaseError("STL is not watertight")

    w.run_forever(boom, idle_backoff=0, max_idle_polls=1)

    from casebroker import db as dbm
    row = dbm.connect(app.state.db_path).execute(
        "SELECT state, attempts, last_error FROM cases").fetchone()
    assert row["state"] == "quarantined", "a fatal runner error must not be retried"
    assert row["attempts"] == 1
    assert "not watertight" in row["last_error"]


def test_the_pedestrian_sample_follows_the_terrain_instead_of_one_flat_plane():
    """The label an ML model trains on is wind speed at pedestrian height ABOVE
    GRADE, and grade is not a constant on a site with relief.

    The runner used to sample one horizontal plane at `terrain_zmax + 1.5`.
    Measured on the campaign's own Nanjing tile, whose terrain spans
    -24.4 .. 38.5 m, that plane sat a MEDIAN 47.6 m above the ground and came
    within 10 m of it over 0.2% of the surface -- so every "pedestrian" sample
    on a site with terrain was free-stream flow tens of metres up. It was
    invisible precisely because such a slice renders perfectly plausibly, and
    because up there two independently converged meshes agree to 1.6% (on the
    real pedestrian surface they differ by 24%).

    A string check is weak evidence about generated bash, so this only guards
    the regressions a rerun would not notice. The dictionary itself is tested in
    test_ped_grid.py and the read-back in test_ped_field.py.
    """
    script = (pathlib.Path(__file__).resolve().parents[1] / "runner" / "run_case.sh").read_text(
        encoding="utf-8", errors="replace")
    # The surface is cut against the builder's WHOLE sheet, cropped by ped_grid,
    # never the mesh case's ground.stl: that one is the land-cover leftover with
    # no triangle under the core, and the sampler cut from it sampled nothing.
    assert 'lib/ped_grid.py" "$SCRATCH/terrain.stl"' in script
    assert 'cp -f "$ROOT/pedCore.stl" constant/triSurface/pedCore.stl' in script
    assert "triSurface/ground.stl" not in script
    # Sampled on the decomposed case: serial, OpenFOAM's cell search alone is
    # minutes on a campaign mesh.
    assert "foamPostProcess -dict system/pedGridFO" in script
    sample = script[script.index("foamPostProcess -dict system/pedGridFO"):]
    assert sample[:sample.index("\n")].rstrip(" \\").endswith("-parallel > ped.log 2>&1")
    # Read back with the terrain, so every value's height above grade is measured.
    assert '--terrain "$SCRATCH/terrain.stl"' in script
    # The old formulation, in either spelling, must not come back.
    assert "planeType       pointAndNormal" not in script
    assert "TERRAIN_ZMAX) + 1.5" not in script


def test_the_result_line_says_whether_the_case_has_a_pedestrian_field(tmp_path):
    """A case without a field has to be visible on the dashboard, not found when
    somebody opens it. Runs the runner's own closing snippet."""
    import json
    import subprocess

    script = (pathlib.Path(__file__).resolve().parents[1] / "runner" / "run_case.sh").read_text(
        encoding="utf-8", errors="replace")
    start = script.index('"$PY" - "$ARCHIVE"')
    snippet = script[script.index("\n", start) + 1:script.index("\nPY", start)]
    archive = tmp_path / "v2-abc.tar.gz"
    archive.write_bytes(b"x")
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"heights_m": [1.5, 1.75], "grid": {"spacing_m": 2.0},
                                "directions": [{}, {}],
                                "coverage": {"case_000@1.5": 0.94, "case_090@1.5": 0.91},
                                "missing": ["case_090@1.75"]}))

    def ped(status, meta_path):
        env = dict(os.environ, GEO_REPORT="", PED_META=str(meta_path))
        if status is not None:
            env["PED_STATUS"] = status
        else:
            env.pop("PED_STATUS", None)
        out = subprocess.run([sys.executable, "-", str(archive), "4", "321", "0", "case_000=500:1"],
                             input=snippet, text=True, capture_output=True, env=env, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])["metrics"].get("pedestrian")

    p = ped("incomplete", meta)
    assert p["status"] == "incomplete"
    assert p["coverage_min"] == 0.91 and p["directions"] == 2
    assert p["missing"] == ["case_090@1.75"]
    assert ped("no-grid", tmp_path / "absent.json") == {"status": "no-grid"}
    # A runner that predates the field says nothing, which is what tells the two apart.
    assert ped(None, meta) is None


def test_the_runner_reports_which_building_source_it_meshed(tmp_path):
    """The broker draws a finished case's inspector from the source its mesh was
    built from, and only the runner can know that: it is in the geometry report
    beside the STLs the run meshed. Without it, a case meshed from Overture was
    drawn from GBA.

    Runs the runner's own closing snippet -- the heredoc that prints the result
    line -- against a report of each shape the builder writes, rather than
    string-matching bash.
    """
    import json
    import subprocess

    script = (pathlib.Path(__file__).resolve().parents[1] / "runner" / "run_case.sh").read_text(
        encoding="utf-8", errors="replace")
    start = script.index('"$PY" - "$ARCHIVE"')
    snippet = script[script.index("\n", start) + 1:script.index("\nPY", start)]
    archive = tmp_path / "v2-abc.tar.gz"
    archive.write_bytes(b"not really a tarball")

    def metrics(report):
        env = dict(os.environ, GEO_REPORT="")
        if report is not None:
            path = tmp_path / "v2-abc.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            env["GEO_REPORT"] = str(path)
        out = subprocess.run([sys.executable, "-", str(archive), "4", "321", "0", "case_000=500:1"],
                             input=snippet, text=True, capture_output=True, env=env, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])["metrics"]

    # The builder's GBA path tags itself (benchmark/real_cities/watertight.py) ...
    assert metrics({"height_source": "gba-lod1", "height_kind": "predicted"})["height_source"] == "gba-lod1"
    # ... its Overture path does not, and carries tile_lod()'s provenance instead.
    assert metrics({"height_provenance": {"tagged": 17}, "frac_measured_height": 0.015})["height_source"] == "overture"
    # No report (STLs staged by the spec), or one showing neither: nothing, not a guess.
    assert "height_source" not in metrics(None)
    assert "height_source" not in metrics({"terrain_z_min": 3.5})
    # And the rest of the result line is what it was.
    m = metrics(None)
    assert (m["stage"], m["ranks"], m["resumed"], m["converged"]) == ("archived", 4, False, True)
    assert m["latest_time"] == {"case_000": "500"}

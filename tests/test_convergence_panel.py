"""How the case page says a direction converged.

The gate on a node says "converged" for three different ends -- the solver met its
residual tolerance, the wall shear stopped moving though the tolerance was not met,
or the direction merely ran to its iteration cap with residuals finite and not
rising -- and the solve report carries only the word. A page that prints the word
over p = 1.15e-4 and epsilon = 7e-3 leaves its reader unable to tell which it was
(a case on the live broker, 2026-10-09: "its not clear that this is converged").
The node's verdict entry for each direction says; the broker keeps it
(GET /v1/cases/{id}/parts) and the page used never to read it.

The reading is pure, so the check runs the SHIPPED JavaScript on the entries a node
writes (Eddy3D's NativeCaseRunner / TestConvergenceEvidence). The rest is wiring,
and what the page depends on from the broker.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
PW = "a-sufficiently-long-passphrase"
W = {"Authorization": "Bearer w"}


def _body(start: str, end: str = "\n  }\n") -> str:
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


def test_the_three_ways_to_be_converged_are_told_apart():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")
    # "2,000", not "2.000": the numbers are formatted for the reader's locale, so the check pins one.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    result = subprocess.run([node, str(ROOT / "tests" / "convergence_check.js")], cwd=ROOT,
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "are told apart" in result.stdout


def test_the_open_case_is_asked_for_its_verdicts_with_its_curves_and_never_a_thermal_one():
    assert "ensureVerdicts(openDetailId, cases.find((c) => c.case_id === openDetailId));" in SRC
    ensure = _body("function ensureVerdicts(")
    assert "isThermal(row)" in ensure, "a Radiance surface-temperature case has no solve to judge"
    assert "/parts" in ensure and "encodeURIComponent(id)" in ensure
    # As the residual curves: often while a node is on the case, rarely after, and a failing
    # endpoint neither blanks what is on screen nor is asked again at once.
    assert "RS_LIVE_MS" in ensure and "RS_IDLE_MS" in ensure
    assert "partsFailed" in ensure and "have.at = Date.now()" in ensure
    # The mesh is not a direction.
    assert 'part.part !== "mesh"' in ensure
    # Redrawn when what a reader sees changed, not on every answer.
    assert "changed" in ensure and "redrawOpenCase(id)" in ensure


def test_every_place_that_names_how_a_direction_ended_reads_the_verdict():
    solve = _body("function solveSectionHtml(")
    assert "verdictsOf(c.case_id)" in solve
    assert "convergenceTally(" in solve and "rs-tally" in solve, "the case in one line"
    assert solve.index("+ convergence + curve") > 0, "above the curve, where the eye starts"
    assert "convergenceOf(" in solve and "conv.label" in solve, "the finished-directions list"
    inner = _body("function residualInner(")
    assert "convergenceOf(" in inner and "rs-verdict" in inner, "the note under the picker"
    assert "rs-eps" in inner and "epsilon is drawn but never stops a solve" in inner
    # The picker names the basis too, not the bare word.
    assert "convergenceOf(fin[name].status" in inner
    # The old reading -- the word as the node sent it -- is only the fallback.
    assert 'String(fin[name].status)' not in inner


def test_the_kinds_the_page_invents_have_colours():
    cls = _body("function solveStatusClass(")
    assert 'v === "stationary"' in cls and 'v === "capped"' in cls
    # Hitting the cap is a warning, not a pass: the gate lets it through, its own record says it did not meet either test.
    assert cls.index('"capped"') > cls.index('"plateaued"') - 40


@pytest.fixture()
def admin(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "c.sqlite"), tokens=["w"], readonly_tokens=[]))
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    return c


def test_a_verdict_as_a_node_writes_it_reaches_the_page_whole(admin):
    """What the page reads is the entry NativeCaseRunner puts in the manifest, so the
    keys it reads have to come back from the broker exactly as they went in --
    including the evidence numbers and a null converged_by."""
    admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": "cyl-1008/of12-v6",
                                              "city_cluster": "atl"}])
    got = admin.post("/v1/lease", headers=W, json={"worker_id": "foam"}).json()[0]
    case = got["case_id"]
    mesh = "a1" * 32
    base = {"lease_id": got["lease_id"], "case_id": case, "archive": f"{case}.x.tar.gz", "bytes": 5}
    assert admin.post("/v1/parts", headers=W, json={**base, "part": "mesh", "sha256": mesh}).status_code == 200

    shear = {"solved_by": "1.16.0.827+3a76491b", "exit": 0, "converged": True, "met_residual_control": False,
             "converged_by": "fieldStationarity", "verdict": "converged", "warm_start": None,
             "last_residuals": {"Ux": 8.46e-7, "p": 1.15e-4, "k": 1.16e-6, "epsilon": 7.3e-3},
             "worst_residual_field": "epsilon", "worst_residual": 7.3e-3, "last_iteration": 942, "end_time": 2000}
    capped = {"solved_by": "1.16.0.827+3a76491b", "exit": 0, "converged": False, "met_residual_control": False,
              "converged_by": None, "verdict": "ended-without-meeting-tolerances",
              "last_residuals": {"p": 3.2e-3}, "last_iteration": 2000, "end_time": 2000}
    for part, verdict, sha in (("case_349", shear, "c3" * 32), ("case_350", capped, "c4" * 32)):
        r = admin.post("/v1/parts", headers=W, json={**base, "part": part, "sha256": sha, "mesh_sha256": mesh,
                                                       "verdict": verdict})
        assert r.status_code == 200, r.text

    parts = {p["part"]: p for p in admin.get(f"/v1/cases/{case}/parts").json()["parts"]}
    assert parts["case_349"]["verdict"] == shear
    assert parts["case_350"]["verdict"] == capped
    assert parts["mesh"]["verdict"] is None, "the mesh is not judged, and the page skips it"
    # And a visitor through a share link reads the same: the verdict is part of the case, not of the admin.
    link = admin.post("/v1/shares", json={"label": "friend"}).json()
    friend = TestClient(admin.app)
    assert friend.post("/v1/auth/share", json={"token": link["token"]}).status_code == 200
    assert friend.get(f"/v1/cases/{case}/parts").json()["parts"][1]["verdict"] == shear


def test_the_tolerance_a_direction_ran_with_is_drawn_and_said():
    """The verdict entry carries `residual_control` (the direction's own fvSolution), so the
    page can say "met its tolerance of 1e-4 on p, U and k" and draw the line it was held to."""
    inner = _body("function residualInner(")
    assert "residualTolerances(entry)" in inner and "tol });" in inner
    assert inner.index("const entry = ") < inner.index("residualSvg(s, {"), "read before the chart is drawn"
    assert "dashed: the tolerance it ran with" in inner
    svg = _body("function residualSvg(")
    assert 'class="rs-tol"' in svg and "esc(toleranceText([t]))" in svg

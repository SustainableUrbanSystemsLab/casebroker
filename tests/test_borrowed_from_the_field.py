"""Five things a case page and a fleet panel say now, in the broker's own words.

* what a build KNOWS, against what the queue needs -- a target that does not
  know a queued recipe would have every node on it refuse those cases;
* a case's STAGES, read off the progress trail it left: how long each took,
  which one a failure landed in, where a running case is;
* PULLING a case off its node on purpose, requeued or parked, with the node
  told at its next heartbeat;
* a failure that names the stage it happened in and carries both streams' last
  lines (the worker's half is in test_runner_failure_text);
* LABELS on a case: free key/values posted with it, filtered by later.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, stages  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
V3, V4 = "fixed-box-1008/of12-v3", "cyl-1008/of12-v4"
OLD, NEW = "1.14.0.827+aaaaaaaa", "1.14.0.827+bbbbbbbb"
SHA = "ab" * 32
W = {"Authorization": "Bearer w"}
T0 = 1_800_000_000


def case(i: int, recipe: str = V3) -> dict:
    return {"case_id": f"c{i:03d}", "spec": {"lat": 1.0, "lon": 2.0, "recipe": recipe},
            "recipe": recipe, "city_cluster": "x", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "b.sqlite"))


@pytest.fixture()
def admin(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=[]))
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    return c


# -- 1. what a build knows ---------------------------------------------------------

def test_a_target_that_does_not_know_a_queued_recipe_is_refused_unless_forced(admin, conn):
    admin.post("/v1/cases", headers=W, json=[
        {"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"},
        {"lat": 33.9, "lon": -84.3, "recipe": V3, "city_cluster": "atl"}])
    # A node on NEW has said what it knows: v3 only.
    admin.post("/v1/lease", headers=W, json={"worker_id": "n1", "build": NEW, "platform": "win-x64",
                                             "recipes": [V3], "count": 0 or 1})
    admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA})
    r = admin.put("/v1/releases/target", json={"build": NEW})
    assert r.status_code == 409 and V4 in r.json()["detail"] and "1 queued" in r.json()["detail"]
    got = admin.get("/v1/releases").json()
    row = {b["build"]: b for b in got["builds"]}[NEW]
    assert (row["knows"], row["missing_recipes"]) == ([V3], [V4])
    assert got["queue_recipes"][V4] == 1
    assert admin.put("/v1/releases/target", json={"build": NEW, "force": True}).status_code == 200


def test_a_build_nobody_has_described_is_not_refused(admin):
    """Unknown is not the same as ignorant: a Python worker declares no recipes."""
    admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    admin.post("/v1/lease", headers=W, json={"worker_id": "py", "build": NEW, "platform": "linux-x64"})
    admin.post("/v1/releases", json={"build": NEW, "platform": "linux-x64", "file": "E3D", "sha256": SHA})
    assert admin.put("/v1/releases/target", json={"build": NEW}).status_code == 200
    row = {b["build"]: b for b in admin.get("/v1/releases").json()["builds"]}[NEW]
    assert (row["knows"], row["missing_recipes"]) == (None, [])


# -- 2. stages ---------------------------------------------------------------------

def test_the_grammar_names_a_stage_and_keeps_it_for_lines_that_name_none():
    assert stages.stage_of("site geometry") == "geometry"
    assert stages.stage_of("build-case") == "build-case"
    assert stages.stage_of("mesh 3/5 · 03_snappyHexMesh") == "mesh"
    assert stages.stage_of("solve 3/8 dirs · iter 412/2000") == "solve"
    assert stages.stage_of("convergence gate") == "gate"
    assert stages.stage_of("archiving with 2 unconverged of 8") == "archive"
    assert stages.stage_of("resuming") == "resume"
    # What runner/run_case.sh and older nodes still send.
    assert stages.stage_of("step 3/5: 03_snappyHexMesh") == "mesh"
    assert stages.stage_of("step 1/1: 01_foamRun") == "solve"
    assert stages.stage_of("step 2/2: 05_reconstructPar") == "archive"
    assert stages.stage_of("case_270 [3/8 dirs] iter 412 p=3.2e-05 (2.3 h)") == "solve"
    assert stages.stage_of("alive") is None and stages.stage_of("mesh warning: 3 failed checks") == "mesh"


def test_a_case_page_says_how_long_each_stage_took_and_which_one_failed(conn):
    db.add_cases(conn, [case(1)])
    got = db.lease(conn, "foam-1", now=T0)[0]
    for dt, line in ((10, "site geometry"), (30, "build-case"), (90, "mesh 1/3 · 01_blockMesh"),
                     (200, "mesh 3/3 · 03_snappyHexMesh"), (400, "solve 0/8 dirs · starting"),
                     (1000, "solve 2/8 dirs · iter 500/2000")):
        db.heartbeat(conn, got.lease_id, detail=line, now=T0 + dt)
    running = db.get_case(conn, "c001")
    assert [s["stage"] for s in running["stages"]] == ["geometry", "build-case", "mesh", "solve"]
    assert [s["seconds"] for s in running["stages"]][:3] == [20, 60, 310]
    assert running["current"] == "solve" and running["failed_in"] is None
    assert running["stages"][-1]["detail"] == "solve 2/8 dirs · iter 500/2000"

    db.fail(conn, got.lease_id, "solver died", now=T0 + 1500)
    failed = db.get_case(conn, "c001")
    assert failed["failed_in"] == "solve" and failed["current"] is None
    assert failed["stages"][-1]["seconds"] == 1100

    # The next attempt is its own: a resume starts at the mesh it kept. The same
    # machine only gets a case it failed back once FAIL_COOLDOWN_SECONDS have passed.
    t2 = T0 + 1500 + db.FAIL_COOLDOWN_SECONDS + 1
    again = db.lease(conn, "foam-1", now=t2)[0]
    db.heartbeat(conn, again.lease_id, detail="resuming", now=t2 + 10)
    db.heartbeat(conn, again.lease_id, detail="solve 2/8 dirs · iter 10/2000", now=t2 + 100)
    second = db.get_case(conn, "c001")
    assert [(s["attempt"], s["stage"]) for s in second["stages"]][-2:] == [(2, "resume"), (2, "solve")]


# -- 3. pulling a case off its node ------------------------------------------------------

def test_pulling_a_case_off_its_node_tells_the_node_to_stop_and_refunds_the_attempt(admin):
    admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V3, "city_cluster": "atl"}])
    got = admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    r = admin.post(f"/v1/cases/{got['case_id']}/cancel", json={"reason": "wrong mesh settings"})
    assert r.status_code == 200 and r.json() == {"case_id": got["case_id"], "worker_id": "foam-1", "state": "pending"}
    # The node hears at its next heartbeat, and its result is refused after that.
    assert admin.post("/v1/heartbeat", headers=W, json={"lease_id": got["lease_id"]}).status_code == 409
    assert admin.post("/v1/complete", headers=W, json={"lease_id": got["lease_id"], "result_uri": "file:///x"}).status_code == 409
    c = admin.get(f"/v1/cases/{got['case_id']}", headers=W).json()
    assert (c["state"], c["attempts"]) == ("pending", 0), "the attempt is refunded"
    trail = admin.get(f"/v1/cases/{got['case_id']}", headers=W).json()
    assert trail["stages"] == []                                       # nothing reported yet
    assert admin.post(f"/v1/cases/{got['case_id']}/cancel", json={}).status_code == 409, "not leased any more"
    assert admin.post("/v1/cases/nope/cancel", json={}).status_code == 404


def test_parking_a_pulled_case_puts_it_where_reopen_looks_with_the_reason(admin):
    admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V3, "city_cluster": "atl"}])
    got = admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    r = admin.post(f"/v1/cases/{got['case_id']}/cancel", json={"reason": "site is a lake", "park": True})
    assert r.status_code == 200 and r.json()["state"] == "quarantined"
    c = admin.get(f"/v1/cases/{got['case_id']}", headers=W).json()
    assert c["state"] == "quarantined" and "site is a lake" in c["last_error"] and "ada" in c["last_error"]
    assert admin.post("/v1/cases/reopen?dry_run=false", headers=W).json()["reopened"] == 1


def test_pulling_a_case_off_its_node_needs_an_admin(admin):
    admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V3, "city_cluster": "atl"}])
    got = admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    anonymous = TestClient(admin.app)
    assert anonymous.post(f"/v1/cases/{got['case_id']}/cancel", headers=W, json={}).status_code in (401, 403)


# -- 5. labels ---------------------------------------------------------------------------

def test_labels_ride_along_with_a_case_and_filter_the_browser(admin):
    r = admin.post("/v1/cases", headers=W, json=[
        {"lat": 33.8, "lon": -84.4, "recipe": V3, "city_cluster": "atl", "labels": {"campaign": "v2-pilot", "batch": "a"}},
        {"lat": 33.9, "lon": -84.3, "recipe": V3, "city_cluster": "atl", "labels": {"campaign": "v2-pilot", "batch": "b"}},
        {"lat": 34.0, "lon": -84.2, "recipe": V3, "city_cluster": "atl"}])
    assert (r.json()["added"], r.json()["labelled"]) == (3, 2)
    page = admin.get("/v1/cases?label=campaign:v2-pilot", headers=W).json()
    assert page["total"] == 2 and all(c["labels"]["campaign"] == "v2-pilot" for c in page["cases"])
    assert admin.get("/v1/cases?label=batch:b", headers=W).json()["total"] == 1
    assert admin.get("/v1/cases?label=batch", headers=W).json()["total"] == 2, "a key alone: every case carrying it"
    assert admin.get("/v1/cases", headers=W).json()["total"] == 3
    one = admin.get("/v1/cases?label=batch:a&include_spec=false", headers=W).json()["cases"][0]
    assert admin.get(f"/v1/cases/{one['case_id']}", headers=W).json()["labels"] == {"campaign": "v2-pilot", "batch": "a"}


def test_reposting_a_case_rewrites_its_labels_and_bad_labels_are_refused(admin):
    body = [{"lat": 33.8, "lon": -84.4, "recipe": V3, "city_cluster": "atl", "labels": {"batch": "a"}}]
    admin.post("/v1/cases", headers=W, json=body)
    body[0]["labels"] = {"batch": "b", "note": "relabelled"}
    r = admin.post("/v1/cases", headers=W, json=body)
    assert (r.json()["added"], r.json()["skipped"], r.json()["labelled"]) == (0, 1, 1)
    c = admin.get("/v1/cases?include_spec=false", headers=W).json()["cases"][0]
    assert c["labels"] == {"batch": "b", "note": "relabelled"}
    bad = [{"lat": 34.1, "lon": -84.5, "recipe": V3, "city_cluster": "x", "labels": {"bad key!": "v"}}]
    assert admin.post("/v1/cases", headers=W, json=bad).status_code == 422
    bad[0]["labels"] = {"k": "x" * 65}
    assert admin.post("/v1/cases", headers=W, json=bad).status_code == 422


def test_purging_a_case_takes_its_labels_with_it(conn):
    db.add_cases(conn, [{**case(1), "labels": {"batch": "a"}}])
    db.purge_cases(conn, recipe=V3, expect=1, dry_run=False)
    assert conn.execute("SELECT COUNT(*) AS n FROM case_labels").fetchone()["n"] == 0

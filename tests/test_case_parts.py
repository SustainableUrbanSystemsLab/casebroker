"""A case survives the machine that was solving it.

COD-359-38 was switched off on 2026-09-24 with 7 of 32 directions of
v2-00e76e426bea6d52 solved, and all 7 were lost with it. A node now ships the
mesh and each finished direction to the Syncthing master as it goes, reports
each one here, and the next node to lease the case is told what already exists.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
V4 = "cyl-1008/of12-v4"
CASE = "v2-00e76e426bea6d52"
MESH, OTHER_MESH = "a1" * 32, "b2" * 32
W = {"Authorization": "Bearer w"}


def _case() -> dict:
    return {"case_id": CASE, "spec": {"lat": 1.0, "lon": 2.0, "recipe": V4},
            "recipe": V4, "city_cluster": "x", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "p.sqlite"))
    db.add_cases(c, [_case()])
    return c


def _ship(conn, lease, part, sha, mesh=MESH):
    return db.report_part(conn, lease.lease_id, CASE, part, f"{CASE}.{part}.tar.gz", sha, 1000, mesh)


def test_the_next_node_is_told_the_mesh_and_the_directions_the_last_one_shipped(conn):
    first = db.lease(conn, "cod-359-38")[0]
    assert first.parts == ()
    assert _ship(conn, first, "mesh", MESH) == "ok"
    assert _ship(conn, first, "case_000", "c0" * 32) == "ok"
    assert _ship(conn, first, "case_011", "c1" * 32) == "ok"

    # The machine is switched off; its lease runs out and another node takes the case.
    conn.execute("UPDATE cases SET lease_expires=0 WHERE case_id=?", (CASE,))
    second = db.lease(conn, "foam-1")[0]
    assert [p["part"] for p in second.parts] == ["mesh", "case_000", "case_011"], "mesh first"
    mesh = second.parts[0]
    assert (mesh["sha256"], mesh["archive"], mesh["worker_id"]) == (MESH, f"{CASE}.mesh.tar.gz", "cod-359-38")
    assert all(p["mesh_sha256"] == MESH for p in second.parts)


def test_a_direction_solved_on_another_mesh_is_refused(conn):
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    assert _ship(conn, lease, "case_000", "c0" * 32, mesh=OTHER_MESH) == "stale_mesh"
    assert [p["part"] for p in db.case_parts(conn, CASE)] == ["mesh"]


def test_a_new_mesh_drops_every_part_of_the_old_one(conn):
    # One case must never be answered on two meshes.
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    _ship(conn, lease, "case_000", "c0" * 32)
    assert _ship(conn, lease, "mesh", OTHER_MESH) == "ok"
    assert [(p["part"], p["sha256"]) for p in db.case_parts(conn, CASE)] == [("mesh", OTHER_MESH)]
    events = [r["event"] for r in conn.execute("SELECT event FROM events WHERE case_id=?", (CASE,))]
    assert "parts_reset" in events


def test_the_same_part_again_replaces_it(conn):
    # A direction `run` solved again ships again under the same name.
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    _ship(conn, lease, "case_000", "c0" * 32)
    _ship(conn, lease, "case_000", "d0" * 32)
    assert [p["sha256"] for p in db.case_parts(conn, CASE) if p["part"] == "case_000"] == ["d0" * 32]


def test_only_the_current_lease_holder_reports_and_only_real_names(conn):
    lease = db.lease(conn, "w")[0]
    assert db.report_part(conn, "not-a-lease", CASE, "mesh", f"{CASE}.mesh.tar.gz", MESH) == "gone"
    assert db.report_part(conn, lease.lease_id, "another-case", "mesh", "x.tar.gz", MESH) == "gone"
    for part, archive, sha in [("../etc", "x.tar.gz", MESH), ("mesh", "../x.tar.gz", MESH),
                               ("mesh", "x.tar.gz", "nothex")]:
        assert db.report_part(conn, lease.lease_id, CASE, part, archive, sha) == "invalid"
    assert db.case_parts(conn, CASE) == []


def test_parts_outlive_a_fresh_claim_and_can_be_reset(conn):
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    db.release(conn, lease.lease_id, reason="mesh unavailable")
    assert [p["part"] for p in db.lease(conn, "v")[0].parts] == ["mesh"], \
        "a fresh claim forgets telemetry, never what reached the master"
    assert db.reset_parts(conn, CASE, by="ada") == 1
    assert db.case_parts(conn, CASE) == []


def test_the_master_is_told_to_offer_a_mesh_only_while_someone_else_continues_the_case(conn):
    first = db.lease(conn, "cod-359-38")[0]
    _ship(conn, first, "mesh", MESH)
    assert db.syncthing_view(conn)["continuations"] == [], \
        "its own node is still solving it, and holds the mesh itself"

    conn.execute("UPDATE cases SET lease_expires=0 WHERE case_id=?", (CASE,))
    db.lease(conn, "foam-1")
    assert db.syncthing_view(conn)["continuations"] == [
        {"case_id": CASE, "archive": f"{CASE}.mesh.tar.gz", "sha256": MESH}]

    conn.execute("UPDATE cases SET state='done' WHERE case_id=?", (CASE,))
    assert db.syncthing_view(conn)["continuations"] == [], "done: nobody needs it any more"


def test_a_node_that_cannot_fetch_a_mesh_is_not_handed_a_case_that_has_one(conn):
    lease = db.lease(conn, "cod-359-38")[0]
    _ship(conn, lease, "mesh", MESH)
    db.release(conn, lease.lease_id)
    assert db.lease(conn, "pace-1", can_continue=False) == [], "it would give it back, forever"
    assert [g.case_id for g in db.lease(conn, "foam-1", can_continue=True)] == [CASE]


def test_a_direction_carries_its_verdict_to_the_node_that_finishes_the_case(conn):
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    verdict = {"converged": False, "verdict": "plateaued", "residuals": {"p": 3e-4}}
    assert db.report_part(conn, lease.lease_id, CASE, "case_000", f"{CASE}.case_000.tar.gz", "c0" * 32,
                          10, MESH, verdict=verdict) == "ok"
    got = {p["part"]: p for p in db.case_parts(conn, CASE)}
    assert got["case_000"]["verdict"] == verdict
    assert got["mesh"]["verdict"] is None
    assert db.report_part(conn, lease.lease_id, CASE, "case_001", f"{CASE}.case_001.tar.gz", "c1" * 32,
                          10, MESH, verdict={"x": "y" * 40000}) == "invalid", "bounded like telemetry"


@pytest.fixture()
def admin(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=[]))
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    return c


def test_the_api_takes_parts_hands_them_out_with_the_lease_and_lets_an_admin_reset_them(admin):
    r = admin.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    assert r.status_code == 200, r.text
    got = admin.post("/v1/lease", headers=W, json={"worker_id": "cod-359-38"}).json()[0]
    assert got["parts"] == []
    case = got["case_id"]

    body = {"lease_id": got["lease_id"], "case_id": case, "part": "mesh",
            "archive": f"{case}.mesh.tar.gz", "sha256": MESH, "bytes": 5}
    assert admin.post("/v1/parts", headers=W, json=body).status_code == 200
    stale = {**body, "part": "case_000", "archive": f"{case}.case_000.tar.gz", "sha256": "c0" * 32,
             "mesh_sha256": OTHER_MESH}
    assert admin.post("/v1/parts", headers=W, json=stale).status_code == 409
    assert admin.post("/v1/parts", headers=W, json={**body, "lease_id": "gone"}).status_code == 409
    assert admin.post("/v1/parts", headers=W, json={**body, "part": "../x"}).status_code == 422

    admin.post("/v1/release", headers=W, json={"lease_id": got["lease_id"]})
    again = admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    assert [p["part"] for p in again["parts"]] == ["mesh"]
    assert admin.get(f"/v1/cases/{case}/parts", headers=W).json()["parts"][0]["sha256"] == MESH

    assert admin.delete(f"/v1/cases/{case}/parts").json()["dropped"] == 1
    assert admin.get(f"/v1/cases/{case}/parts", headers=W).json()["parts"] == []

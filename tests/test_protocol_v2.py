"""Protocol 2: capabilities, the node's machine, its stage, and handing a case on.

Each addition is optional on the wire -- an older node sends none of it and is
leased exactly as before -- so every test here also pins what happens WITHOUT
the field.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

T0 = 1_000_000
SHA = "a" * 64


def case(case_id, priority=100):
    return {"case_id": case_id, "spec": {}, "recipe": "r", "city_cluster": "x",
            "split": "train", "priority": priority, "max_attempts": 3}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "p.sqlite"))


def take(conn, worker, now, host=None, **kw):
    got = db.lease(conn, worker, count=1, now=now, host=host or worker, **kw)
    return got[0] if got else None


def meshed_at_broker(conn, case_id, cells=None):
    """What a case another node started looks like: its mesh reported, and held."""
    conn.execute("INSERT INTO case_parts(case_id, part, archive, sha256, reported_at)"
                 " VALUES (?, 'mesh', ?, ?, ?)", (case_id, f"{case_id}.mesh.tar.gz", SHA, T0))
    conn.execute("INSERT INTO case_blobs(case_id, part, sha256, bytes, stored_at)"
                 " VALUES (?, 'mesh', ?, 1, ?)", (case_id, SHA, T0))
    if cells is not None:
        conn.execute("UPDATE cases SET mesh_cells = ? WHERE case_id = ?", (cells, case_id))


# -- capabilities ------------------------------------------------------------------

def test_healthz_says_what_this_broker_supports(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "h.sqlite"), tokens=[], readonly_tokens=[]))
    h = c.get("/healthz").json()
    assert h["protocol"] == 2
    assert h["features"] == sorted(h["features"])
    for name in ("telemetry", "residuals", "parts", "fields", "field_backfill", "parts_wanted",
                 "node_release", "heartbeat_stage", "handoff", "hardware", "machine_scoped_leases"):
        assert name in h["features"], name
    assert "part_store" not in h["features"], "no store configured"


def test_part_store_is_listed_only_where_there_is_one(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "h.sqlite"), tokens=[], readonly_tokens=[],
                              parts_dir=str(tmp_path / "parts")))
    assert "part_store" in c.get("/healthz").json()["features"]


def test_the_lease_keeps_what_the_node_declares(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "w.sqlite"), tokens=[], readonly_tokens=[]))
    c.post("/v1/cases", json=[{"lat": 35, "lon": 139, "recipe": "v2-wind", "city_cluster": "tokyo"}])
    r = c.post("/v1/lease", json={"worker_id": "lab-1", "features": ["handoff", "continue_from_broker"],
                                  "cpus": 36, "mem_gb": 127.6})
    assert r.status_code == 200, r.text
    w = next(w for w in c.get("/v1/status").json()["workers"] if w["worker_id"] == "lab-1")
    assert (w["features"], w["cpus"], w["mem_gb"]) == (["continue_from_broker", "handoff"], 36, 127.6)
    # An older node says nothing, and the row says it said nothing.
    c.post("/v1/lease", json={"worker_id": "lab-1"})
    w = next(w for w in c.get("/v1/status").json()["workers"] if w["worker_id"] == "lab-1")
    assert (w["features"], w["cpus"], w["mem_gb"]) == (None, None, None)


@pytest.mark.parametrize("body", [{"features": ["Has Spaces"]}, {"cpus": 0}, {"mem_gb": 0.1},
                                  {"features": ["x"] * 33}])
def test_a_malformed_declaration_is_refused(tmp_path, body):
    c = TestClient(create_app(db_path=str(tmp_path / "w.sqlite"), tokens=[], readonly_tokens=[]))
    assert c.post("/v1/lease", json={"worker_id": "lab-1", **body}).status_code == 422


def test_continue_from_broker_in_features_counts_as_the_old_flag(conn):
    db.add_cases(conn, [case("A", 10)])
    meshed_at_broker(conn, "A")
    # A node that says nothing about continuing is from before parts and may
    # take anything; one that says it cannot continue is kept from A...
    assert take(conn, "n0", T0, can_continue_from_broker=False) is None
    # ...and a node that says so in `features` alone is handed it.
    assert take(conn, "n1", T0, features=["continue_from_broker"]).case_id == "A"


# -- hardware ------------------------------------------------------------------------

def test_started_work_goes_first(conn):
    # B sorts after A by id, but B is half done: a node that can continue it takes it first.
    db.add_cases(conn, [case("A"), case("B")])
    meshed_at_broker(conn, "B")
    assert take(conn, "n1", T0, features=["continue_from_broker"]).case_id == "B"
    # A node that cannot continue still gets the fresh one, as before.
    assert take(conn, "n2", T0, can_continue_from_broker=False).case_id == "A"


def test_a_node_too_small_for_a_known_mesh_is_not_handed_it(conn):
    db.add_cases(conn, [case("A", 10), case("B", 20)])
    conn.execute("UPDATE cases SET mesh_cells = 10000000 WHERE case_id = 'A'")   # ~20 GB
    assert take(conn, "small", T0, mem_gb=8).case_id == "B"
    assert take(conn, "big", T0, mem_gb=64).case_id == "A"


def test_a_node_that_declares_no_memory_is_not_gated(conn):
    db.add_cases(conn, [case("A")])
    conn.execute("UPDATE cases SET mesh_cells = 10000000 WHERE case_id = 'A'")
    assert take(conn, "old", T0).case_id == "A"


def test_small_nodes_do_not_mesh_when_the_campaign_says_so(conn):
    db.add_cases(conn, [case("A"), case("B")])
    meshed_at_broker(conn, "B")
    assert take(conn, "laptop", T0, cpus=4, features=["continue_from_broker"]).case_id == "B"
    db.set_setting(conn, "small_node_cpus", "8")
    assert take(conn, "laptop-2", T0, cpus=4, features=["continue_from_broker"]) is None, \
        "A needs meshing and B is held: nothing for a 4-core box"
    assert take(conn, "ws", T0, cpus=36, features=["continue_from_broker"]).case_id == "A"
    assert db.list_releases(conn)["small_node_cpus"] == 8


def test_mesh_telemetry_records_the_cell_count_and_it_outlives_the_attempt(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "n1", T0)
    assert db.post_telemetry(conn, a.lease_id, "A", "mesh", {"total_cells": 4_700_000}, now=T0 + 10) == "ok"
    assert db.release(conn, a.lease_id, "preempted", now=T0 + 20)
    take(conn, "n2", T0 + 30)          # a fresh claim clears telemetry...
    row = conn.execute("SELECT telemetry, mesh_cells FROM cases WHERE case_id = 'A'").fetchone()
    assert row["telemetry"] is None and row["mesh_cells"] == 4_700_000, "...but not what the site is"


# -- the stage of a progress line -----------------------------------------------------

def test_the_nodes_stage_wins_over_reading_the_line(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "n1", T0)
    assert db.heartbeat(conn, a.lease_id, 900, "snapping 4/7 · castellated", now=T0 + 10, stage="mesh")
    assert db.heartbeat(conn, a.lease_id, 900, "case_013 iter 412/2000", now=T0 + 20, stage="solve")
    got = db.get_case(conn, "A")
    assert [s["stage"] for s in got["stages"]] == ["mesh", "solve"]
    assert got["current"] == "solve"


def test_without_a_stage_the_line_is_read_as_before(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "n1", T0)
    assert db.heartbeat(conn, a.lease_id, 900, "mesh 3/5 · 03_snappyHexMesh", now=T0 + 10)
    assert db.heartbeat(conn, a.lease_id, 900, "something new", now=T0 + 20, stage="unheard-of")
    assert [s["stage"] for s in db.get_case(conn, "A")["stages"]] == ["mesh"]


def test_the_heartbeat_route_takes_a_stage(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "s.sqlite"), tokens=[], readonly_tokens=[]))
    c.post("/v1/cases", json=[{"lat": 35, "lon": 139, "recipe": "v2-wind", "city_cluster": "tokyo"}])
    lease = c.post("/v1/lease", json={"worker_id": "n1"}).json()[0]
    ok = c.post("/v1/heartbeat", json={"lease_id": lease["lease_id"], "detail": "x", "stage": "geometry"})
    assert ok.status_code == 200, ok.text
    assert c.post("/v1/heartbeat", json={"lease_id": lease["lease_id"], "stage": "NOT A STAGE"}).status_code == 422
    assert c.get(f"/v1/cases/{lease['case_id']}").json()["current"] == "geometry"


# -- handing a case on -------------------------------------------------------------------

def test_a_handed_off_case_goes_to_another_machine_first(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "small-1", T0, host="box1")
    assert db.release(conn, a.lease_id, "handoff: solved 4 of 32 directions here", now=T0 + 60, handoff=True)
    row = conn.execute("SELECT state, attempts FROM cases WHERE case_id = 'A'").fetchone()
    assert (row["state"], row["attempts"]) == ("pending", 0), "refunded like any release"
    assert take(conn, "small-1", T0 + 61, host="box1", resume_case_ids=["A"]) is None, \
        "the node that let go does not get it straight back, as a resume either"
    assert take(conn, "small-1-2", T0 + 62, host="box1") is None, "nor does its host"
    b = take(conn, "ws", T0 + 63, host="box2")
    assert b.case_id == "A"


def test_after_the_cooldown_the_same_node_may_continue(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "small-1", T0, host="box1")
    assert db.release(conn, a.lease_id, "handoff", now=T0 + 60, handoff=True)
    later = T0 + 60 + db.HANDOFF_COOLDOWN_SECONDS + 1
    assert take(conn, "small-1", later, host="box1", resume_case_ids=["A"]).case_id == "A"


def test_a_plain_release_is_not_a_handoff(conn):
    db.add_cases(conn, [case("A")])
    a = take(conn, "n1", T0, host="box1")
    assert db.release(conn, a.lease_id, "preempted", now=T0 + 60)
    assert take(conn, "n1", T0 + 61, host="box1", resume_case_ids=["A"]).case_id == "A", \
        "a preempted node resumes its own checkpoint at once"


def test_the_release_route_takes_handoff_and_the_trail_says_so(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "r.sqlite"), tokens=[], readonly_tokens=[]))
    c.post("/v1/cases", json=[{"lat": 35, "lon": 139, "recipe": "v2-wind", "city_cluster": "tokyo"}])
    lease = c.post("/v1/lease", json={"worker_id": "n1", "host": "box1"}).json()[0]
    assert c.post("/v1/heartbeat", json={"lease_id": lease["lease_id"], "detail": "solve 4/32 dirs",
                                         "stage": "solve"}).status_code == 200
    r = c.post("/v1/release", json={"lease_id": lease["lease_id"], "reason": "handoff: 4 of 32",
                                    "handoff": True})
    assert r.status_code == 200, r.text
    got = c.get(f"/v1/cases/{lease['case_id']}").json()
    assert got["state"] == "pending"
    assert got["stages"][-1]["ended_at"] is not None, "a handoff ends the run's stage"
    assert c.post("/v1/lease", json={"worker_id": "n1", "host": "box1"}).json() == []

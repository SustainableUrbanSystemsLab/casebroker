"""The broker keeps a case's parts itself, uploaded in chunks by the node that made them.

A part (the mesh, a finished direction, the case's archive) used to reach a
Syncthing master only, the broker holding its hash and name; that master is gone
(2026-10-06). With a part store configured the node uploads it here, through
Cloudflare (100 MB a request at most, hence chunks) and the host's reverse proxy,
and the next node to continue the case fetches the mesh from the broker.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, partstore  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
V4 = "cyl-1008/of12-v4"
W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PARTS_RESERVE_GB", "0")
    app = create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"],
                     parts_dir=str(tmp_path / "parts"))
    c = TestClient(app)
    r = c.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    assert r.status_code == 200, r.text
    lease = c.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    return c, lease["case_id"], lease["lease_id"], tmp_path / "parts"


def _report(c, case, lease_id, part, content, mesh_sha=None):
    body = {"lease_id": lease_id, "case_id": case, "part": part, "archive": f"{case}.{part}.tar.gz",
            "sha256": _sha(content), "bytes": len(content)}
    if mesh_sha:
        body["mesh_sha256"] = mesh_sha
    r = c.post("/v1/parts", headers=W, json=body)
    assert r.status_code == 200, r.text


def _start(c, case, part, content, headers=W):
    return c.post(f"/v1/cases/{case}/parts/{part}/upload", headers=headers,
                  json={"sha256": _sha(content), "bytes": len(content)})


def _put(c, case, part, content, offset, chunk):
    return c.put(f"/v1/cases/{case}/parts/{part}/upload", headers=W,
                 params={"offset": offset, "bytes": len(content)}, content=chunk)


def _upload(c, case, part, content, chunk=7):
    """What a node does: ask where the upload stands, send from there, then wait."""
    start = _start(c, case, part, content)
    assert start.status_code == 200, start.text
    offset = start.json()["offset"]
    while offset < len(content) and start.json()["state"] in ("absent", "partial"):
        r = _put(c, case, part, content, offset, content[offset:offset + chunk])
        assert r.status_code == 200, r.text
        offset = r.json()["offset"]
    return _wait_stored(c, case, part, content)


def _wait_stored(c, case, part, content, timeout=10.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        state = _start(c, case, part, content).json()
        if state["state"] not in ("verifying",):
            return state
        time.sleep(0.02)
    raise AssertionError("never verified")


def test_a_part_goes_up_in_chunks_resumes_where_it_stood_and_comes_back_byte_for_byte(broker):
    c, case, lease_id, root = broker
    mesh = os.urandom(50)
    _report(c, case, lease_id, "mesh", mesh)

    first = _start(c, case, "mesh", mesh).json()
    assert first["state"] == "absent" and first["offset"] == 0
    assert first["max_chunk_bytes"] <= 100 * 1000 * 1000, "under Cloudflare's 100 MB a request"

    assert _put(c, case, "mesh", mesh, 0, mesh[:20]).json() == {"state": "partial", "offset": 20}
    # A retried chunk that HAD arrived: the broker says where it stands, and nothing is doubled.
    retry = _put(c, case, "mesh", mesh, 0, mesh[:20])
    assert retry.status_code == 409 and retry.json()["detail"]["offset"] == 20
    # A node that restarts asks, and resumes from there.
    assert _start(c, case, "mesh", mesh).json() == {**first, "state": "partial", "offset": 20}
    assert _upload(c, case, "mesh", mesh)["state"] == "stored"

    got = c.get(f"/v1/cases/{case}/parts/mesh/blob", headers=R)
    assert got.status_code == 200 and got.content == mesh
    assert got.headers["etag"] == f'"{_sha(mesh)}"'
    assert got.headers["content-type"] == "application/gzip"
    part = c.get(f"/v1/cases/{case}/parts/mesh/blob", headers={**R, "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == mesh[10:20], "a fetch resumes, too"
    assert (root / "objects" / _sha(mesh)[:2] / _sha(mesh)).read_bytes() == mesh
    assert not any((root / "incoming").iterdir())

    # The node continuing this case is told the broker has the mesh.
    parts = c.get(f"/v1/cases/{case}/parts", headers=R).json()["parts"]
    assert parts[0]["part"] == "mesh" and parts[0]["at_broker"] is True
    c.post("/v1/release", headers=W, json={"lease_id": lease_id})
    nxt = c.post("/v1/lease", headers=W, json={"worker_id": "foam-2"}).json()[0]
    assert nxt["parts"][0]["at_broker"] is True


def test_only_the_bytes_the_node_reported_are_taken(broker):
    c, case, lease_id, _ = broker
    mesh = os.urandom(40)
    assert _start(c, case, "mesh", mesh).status_code == 404, "a part with no report"
    _report(c, case, lease_id, "mesh", mesh)
    other = os.urandom(40)
    assert _start(c, case, "mesh", other).status_code == 409, "another hash"
    r = c.post(f"/v1/cases/{case}/parts/mesh/upload", headers=W, json={"sha256": _sha(mesh), "bytes": 41})
    assert r.status_code == 409, "another size"
    assert _start(c, case, "../etc", mesh).status_code in (404, 422)
    assert _start(c, case, "mesh", mesh, headers=R).status_code in (401, 403), "read scope cannot upload"

    # Bytes that are not the content they claim: refused when verified, and dropped.
    assert _start(c, case, "mesh", mesh).json()["state"] == "absent"
    assert _put(c, case, "mesh", mesh, 0, other).json()["state"] == "verifying"
    after = _wait_stored(c, case, "mesh", mesh)
    assert after["state"] == "absent" and after["offset"] == 0
    assert "hashes to" in after["last_failure"]
    assert c.get(f"/v1/cases/{case}/parts/mesh/blob", headers=R).status_code == 404


def test_a_chunk_cannot_run_past_the_part_or_past_the_chunk_limit(broker, monkeypatch):
    c, case, lease_id, _ = broker
    mesh = os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    _start(c, case, "mesh", mesh)
    assert _put(c, case, "mesh", mesh, 0, mesh + b"x").status_code == 422
    monkeypatch.setattr(partstore, "MAX_CHUNK", 10)
    assert _put(c, case, "mesh", mesh, 0, mesh[:11]).status_code == 413
    assert _put(c, case, "mesh", mesh, 0, mesh[:10]).json()["offset"] == 10, "nothing of a refused chunk stayed"


def test_a_store_that_would_cut_into_its_reserve_refuses_before_the_first_byte(broker):
    c, case, lease_id, _ = broker
    mesh = os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    c.app.state.parts.reserve_bytes = 1 << 62
    r = _start(c, case, "mesh", mesh)
    assert r.status_code == 507, r.text
    assert r.json()["detail"]["reserve_bytes"] == 1 << 62
    assert _put(c, case, "mesh", mesh, 0, mesh[:10]).status_code == 507, "and at every chunk"


def test_the_store_stops_at_its_own_size_limit_whatever_the_disk_has_free(broker):
    c, case, lease_id, _ = broker
    mesh, d0 = os.urandom(30), os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    _report(c, case, lease_id, "case_000", d0, mesh_sha=_sha(mesh))
    c.app.state.parts.max_bytes = 50
    assert _upload(c, case, "mesh", mesh)["state"] == "stored"          # 30 of 50
    r = _start(c, case, "case_000", d0)                                   # 60 > 50
    assert r.status_code == 507 and r.json()["detail"]["max_bytes"] == 50
    assert c.get("/v1/storage", headers=R).json()["parts_store"]["max_bytes"] == 50


def test_a_kind_the_broker_does_not_keep_is_declined_not_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PARTS_KEEP", "mesh,archive")
    monkeypatch.setenv("CASEBROKER_PARTS_RESERVE_GB", "0")
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"],
                              parts_dir=str(tmp_path / "parts")))
    c.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    lease = c.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    case = lease["case_id"]
    mesh, d0 = os.urandom(20), os.urandom(20)
    _report(c, case, lease["lease_id"], "mesh", mesh)
    _report(c, case, lease["lease_id"], "case_000", d0, mesh_sha=_sha(mesh))
    declined = _start(c, case, "case_000", d0)
    assert declined.status_code == 200 and declined.json()["state"] == "declined"
    assert _put(c, case, "case_000", d0, 0, d0).status_code == 409
    assert _upload(c, case, "mesh", mesh)["state"] == "stored"


def test_the_archive_and_every_part_make_the_case_stored_at_the_broker(broker):
    c, case, lease_id, _ = broker
    mesh, d0, d1 = os.urandom(30), os.urandom(30), os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    _report(c, case, lease_id, "case_000", d0, mesh_sha=_sha(mesh))
    _report(c, case, lease_id, "case_001", d1, mesh_sha=_sha(mesh))
    archive = os.urandom(25)
    assert _start(c, case, "archive", archive).status_code == 409, "no archive before the case is done"
    done = c.post("/v1/complete", headers=W, json={"lease_id": lease_id, "case_id": case,
                                                    "result_uri": f"file:///C:/wind/done/{case}.tar.gz",
                                                    "sha256": _sha(archive), "bytes": len(archive), "metrics": {}})
    assert done.status_code == 200, done.text

    for part, content in (("mesh", mesh), ("case_000", d0), ("archive", archive)):
        assert _upload(c, case, part, content)["state"] == "stored"
    held = c.get(f"/v1/cases/{case}/blobs", headers=R).json()
    assert [p["part"] for p in held["parts"]] == ["mesh", "case_000", "archive"]
    assert held["complete"] is False, "case_001 is not here yet"
    assert c.get("/v1/custody", headers=R).json()["missing_archive"] == 1

    assert _upload(c, case, "case_001", d1)["state"] == "stored"
    assert c.get(f"/v1/cases/{case}/blobs", headers=R).json()["complete"] is True
    receipt = [r for r in c.get(f"/v1/cases/{case}/receipts", headers=R).json()["receipts"]
               if r["location"] == "broker"]
    assert receipt and receipt[0]["sha256"] == _sha(archive)
    assert receipt[0]["bytes"] == len(mesh) + len(d0) + len(d1) + len(archive)
    assert c.get("/v1/custody", headers=R).json()["missing_archive"] == 0

    store = c.get("/v1/storage", headers=R).json()["parts_store"]
    assert store["enabled"] and store["objects"] == 4 and store["held"]["cases_complete"] == 1
    assert c.get("/healthz").json()["parts_store"] is True


def test_a_replaced_mesh_orphans_the_old_parts_and_a_sweep_removes_them(broker):
    c, case, lease_id, root = broker
    c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    mesh, d0 = os.urandom(30), os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    _report(c, case, lease_id, "case_000", d0, mesh_sha=_sha(mesh))
    _upload(c, case, "mesh", mesh)
    _upload(c, case, "case_000", d0)

    new_mesh = os.urandom(30)
    _report(c, case, lease_id, "mesh", new_mesh)          # the case is meshed again
    assert c.get(f"/v1/cases/{case}/blobs", headers=R).json()["parts"] == []

    assert c.post("/v1/parts/sweep").json()["orphans"] == 0, "a file verified a moment ago is not an orphan yet"
    for f in (root / "objects").glob("*/*"):
        os.utime(f, (time.time() - 7200, time.time() - 7200))
    dry = c.post("/v1/parts/sweep").json()
    assert dry["dry_run"] and dry["orphans"] == 2
    assert len(list((root / "objects").glob("*/*"))) == 2, "a dry run deletes nothing"
    real = c.post("/v1/parts/sweep", params={"dry_run": "false"}).json()
    assert real["orphans"] == 2 and not list((root / "objects").glob("*/*"))

    _upload(c, case, "mesh", new_mesh)
    gone = c.delete(f"/v1/cases/{case}/parts/mesh/blob")
    assert gone.status_code == 200 and gone.json()["file_removed"] is True
    assert c.get(f"/v1/cases/{case}/parts/mesh/blob", headers=R).status_code == 404


def test_without_a_store_the_routes_say_so_and_nodes_carry_on(tmp_path, monkeypatch):
    monkeypatch.delenv("CASEBROKER_PARTS_DIR", raising=False)
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"]))
    c.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    lease = c.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    mesh = os.urandom(10)
    _report(c, lease["case_id"], lease["lease_id"], "mesh", mesh)
    assert _start(c, lease["case_id"], "mesh", mesh).status_code == 404
    assert c.get("/healthz").json()["parts_store"] is False
    assert c.get("/v1/storage", headers=R).json()["parts_store"] == {"enabled": False}
    assert c.get(f"/v1/cases/{lease['case_id']}/parts", headers=R).json()["parts"][0]["at_broker"] is False


def test_a_continued_case_goes_only_to_a_node_that_can_fetch_its_mesh_from_the_broker(broker):
    """A case with a mesh on record can be continued only from the broker's copy of that mesh:
    a node that can fetch from the broker is handed the ones whose mesh the broker holds -- and
    not the ones whose mesh it never got, which nobody can give."""
    c, case, lease_id, _ = broker
    r = c.post("/v1/cases", headers=W, json=[{"lat": 40.7, "lon": -74.0, "recipe": V4, "city_cluster": "nyc"}])
    assert r.status_code == 200, r.text
    other = c.post("/v1/lease", headers=W, json={"worker_id": "foam-2"}).json()[0]
    mesh_a, mesh_b = os.urandom(20), os.urandom(20)
    _report(c, case, lease_id, "mesh", mesh_a)
    _report(c, other["case_id"], other["lease_id"], "mesh", mesh_b)
    assert _upload(c, case, "mesh", mesh_a)["state"] == "stored"         # only the first is at the broker
    for lid in (lease_id, other["lease_id"]):
        c.post("/v1/release", headers=W, json={"lease_id": lid})

    def lease(**flags):
        return c.post("/v1/lease", headers=W, json={"worker_id": "pace-1", "count": 5, **flags}).json()

    assert lease(can_continue=False) == [], "no broker: neither"
    assert lease(can_continue=True) == [], "the Syncthing master is gone: true no longer counts"
    got = lease(can_continue=False, can_continue_from_broker=True)
    assert [g["case_id"] for g in got] == [case]
    assert got[0]["parts"][0]["at_broker"] is True


def test_a_node_sweeping_its_done_folder_is_told_what_the_broker_still_wants(broker):
    """Parts that reached nobody -- shipped before the broker kept parts, or while it was down --
    are what a sweep asks after: every reported part not held, and a done case's archive."""
    c, case, lease_id, _ = broker
    mesh, d0 = os.urandom(30), os.urandom(30)
    _report(c, case, lease_id, "mesh", mesh)
    _report(c, case, lease_id, "case_000", d0, mesh_sha=_sha(mesh))

    def wanted(*ids):
        r = c.post("/v1/parts/wanted", headers=W, json={"case_ids": list(ids)})
        assert r.status_code == 200, r.text
        return r.json()

    got = wanted(case, "v2-nobody-has-heard-of", case)
    assert got["enabled"] is True and list(got["cases"]) == [case], "an unknown case is left out"
    row = got["cases"][case]
    assert row["state"] == "leased"
    assert [(w["part"], w["sha256"], w["bytes"]) for w in row["wanted"]] == [
        ("mesh", _sha(mesh), len(mesh)), ("case_000", _sha(d0), len(d0))], "mesh first; no archive before done"
    assert row["wanted"][0]["archive"] == f"{case}.mesh.tar.gz"

    assert _upload(c, case, "mesh", mesh)["state"] == "stored"
    archive = os.urandom(25)
    done = c.post("/v1/complete", headers=W, json={"lease_id": lease_id, "case_id": case,
                                                    "result_uri": f"file:///C:/wind/done/{case}.tar.gz",
                                                    "sha256": _sha(archive), "bytes": len(archive), "metrics": {}})
    assert done.status_code == 200, done.text
    row = wanted(case)["cases"][case]
    assert [(w["part"], w["sha256"]) for w in row["wanted"]] == [("case_000", _sha(d0)), ("archive", _sha(archive))]

    for part, content in (("case_000", d0), ("archive", archive)):
        assert _upload(c, case, part, content)["state"] == "stored"
    assert wanted(case)["cases"][case]["wanted"] == [], "everything it knows of is held"
    assert c.post("/v1/parts/wanted", headers=R, json={"case_ids": [case]}).status_code in (401, 403), \
        "a node's question: write scope, like the uploads it leads to"


def test_a_kind_the_broker_does_not_keep_is_not_wanted_and_without_a_store_nothing_is(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PARTS_RESERVE_GB", "0")
    monkeypatch.setenv("CASEBROKER_PARTS_KEEP", "mesh,archive")
    c = TestClient(create_app(db_path=str(tmp_path / "k.sqlite"), tokens=["w"], readonly_tokens=["r"],
                              parts_dir=str(tmp_path / "parts")))
    c.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    lease = c.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    mesh, d0 = os.urandom(30), os.urandom(30)
    _report(c, lease["case_id"], lease["lease_id"], "mesh", mesh)
    _report(c, lease["case_id"], lease["lease_id"], "case_000", d0, mesh_sha=_sha(mesh))
    row = c.post("/v1/parts/wanted", headers=W, json={"case_ids": [lease["case_id"]]}).json()["cases"][lease["case_id"]]
    assert [w["part"] for w in row["wanted"]] == ["mesh"], "directions are not kept here"

    monkeypatch.delenv("CASEBROKER_PARTS_DIR", raising=False)
    bare = TestClient(create_app(db_path=str(tmp_path / "k.sqlite"), tokens=["w"], readonly_tokens=["r"]))
    assert bare.post("/v1/parts/wanted", headers=W, json={"case_ids": [lease["case_id"]]}).json() == \
        {"enabled": False, "cases": {lease["case_id"]: {"state": "leased", "wanted": [], "fields": []}}}, \
        "no part wanted without a store; the fields are still listed, for a backfill"

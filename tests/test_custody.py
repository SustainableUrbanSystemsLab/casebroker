"""Custody: what has ARRIVED where results are kept, beside what the node said it did.

`done` is the node's word -- POST /v1/complete names an archive on the node's own disk
and its sha256. Whether the archive reached a place it is kept intact, and whether every
direction's pedestrian field reached the database, is a different question with a
different answerer, so it is kept per artifact (case_artifacts) and never folded into
`state` (Patrick, 2026-10-01). Found the night it was decided: two dev-built nodes had
finished directions against a broker that did not take fields yet, and nothing would ever
have said those cases were missing them.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

from test_case_fields import umag  # noqa: E402

WIND, THERMAL = "cyl-1008/of12-v6", "surf-1008/rad6R0P2-fft-v2"
SHA = "a" * 64
OTHER = "b" * 64
T0 = 1_000_000


def case(i: int, recipe: str = WIND) -> dict:
    return {"case_id": f"c{i:03d}", "spec": {"lat": 1.0 + i, "lon": 2.0, "recipe": recipe},
            "recipe": recipe, "city_cluster": "x", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "c.sqlite"))


def finish(conn, i: int, recipe: str = WIND, directions: int = 2, fields: int = 0, now: int = T0) -> str:
    """A case leased, given `directions` wind directions in its telemetry, `fields` of them
    stored, and completed with archive hash SHA."""
    db.add_cases(conn, [case(i, recipe)])
    lease = db.lease(conn, "foam-1", now=now, recipes=[recipe])[0]
    if recipe.startswith("cyl-"):
        assert db.post_telemetry(conn, lease.lease_id, lease.case_id, "solve",
                                 {"directions_total": directions}, now=now) == "ok"
        for d in range(fields):
            name = f"case_{d * 90:03d}"
            assert db.put_field(conn, lease.lease_id, lease.case_id, name,
                                umag([1.0] * 6, direction=name, case_id=lease.case_id), now=now) == "ok"
    assert db.complete(conn, lease.lease_id, f"file:///C:/wind/done/{lease.case_id}.tar.gz",
                       sha256=SHA, nbytes=123, now=now + 1)
    return lease.case_id


# -- receipts ------------------------------------------------------------------------

def test_an_archive_receipt_is_recorded_when_its_hash_is_the_one_the_node_reported(conn):
    cid = finish(conn, 1)
    got = db.record_receipt(conn, cid, "archive", "master", SHA.upper(), size=123, path="done/c001.tar.gz",
                            by="master-scan", now=T0 + 10)
    assert got["sha256"] == SHA, "hex is compared and kept lower-case"
    assert db.case_receipts(conn, cid) == [{"kind": "archive", "location": "master", "bytes": 123,
                                            "sha256": SHA, "path": "done/c001.tar.gz",
                                            "received_at": T0 + 10, "reported_by": "master-scan"}]
    # Reporting it again replaces the receipt; there is still one.
    db.record_receipt(conn, cid, "archive", "master", SHA, size=124, now=T0 + 20)
    assert [r["bytes"] for r in db.case_receipts(conn, cid)] == [124]


def test_a_different_hash_is_a_corrupted_transfer_and_is_refused(conn):
    cid = finish(conn, 1)
    with pytest.raises(db.ReceiptRefused) as e:
        db.record_receipt(conn, cid, "archive", "master", OTHER)
    assert e.value.status == 409 and "corrupted" in str(e.value)
    assert db.case_receipts(conn, cid) == []


def test_only_a_done_case_has_an_archive_to_receive(conn):
    db.add_cases(conn, [case(1)])
    with pytest.raises(db.ReceiptRefused) as e:
        db.record_receipt(conn, "c001", "archive", "master", SHA)
    assert e.value.status == 409 and "pending" in str(e.value)
    with pytest.raises(db.ReceiptRefused) as e:
        db.record_receipt(conn, "nope", "archive", "master", SHA)
    assert e.value.status == 404


@pytest.mark.parametrize("kind, location, sha, size", [
    ("tarball", "master", SHA, None), ("archive", "laptop", SHA, None),
    ("archive", "master", "xyz", None), ("archive", "master", SHA, -1)])
def test_a_malformed_receipt_is_refused_before_anything_is_written(conn, kind, location, sha, size):
    cid = finish(conn, 1)
    with pytest.raises(db.ReceiptRefused) as e:
        db.record_receipt(conn, cid, kind, location, sha, size=size)
    assert e.value.status == 422
    assert db.case_receipts(conn, cid) == []


# -- custody -------------------------------------------------------------------------

def test_a_done_case_is_in_custody_until_its_archive_and_every_field_have_arrived(conn):
    cid = finish(conn, 1, directions=2, fields=1)
    view = db.custody(conn, now=T0 + 100)
    assert (view["done"], view["stored"], view["missing_archive"], view["missing_fields"]) == (1, 0, 1, 1)
    [row] = view["cases"]
    assert row["missing"] == ["archive", "fields"] and (row["fields"], row["fields_expected"]) == (1, 2)

    lease_free = db.record_receipt(conn, cid, "archive", "master", SHA, now=T0 + 50)
    assert lease_free["kind"] == "archive"
    assert db.custody(conn, now=T0 + 100)["cases"][0]["missing"] == ["fields"]


def test_a_case_with_its_archive_and_all_its_fields_is_stored(conn):
    cid = finish(conn, 1, directions=2, fields=2)
    db.record_receipt(conn, cid, "archive", "master", SHA)
    view = db.custody(conn, now=T0 + 100)
    assert (view["done"], view["stored"], view["total"], view["cases"]) == (1, 1, 0, [])


def test_a_thermal_case_needs_only_its_archive(conn):
    cid = finish(conn, 1, recipe=THERMAL)
    assert db.custody(conn, now=T0 + 100)["cases"][0]["missing"] == ["archive"]
    db.record_receipt(conn, cid, "archive", "master", SHA)
    assert db.custody(conn, now=T0 + 100)["stored"] == 1


def test_a_case_still_syncing_is_left_out_of_the_list_but_not_the_counts(conn):
    finish(conn, 1, now=T0)
    finish(conn, 2, now=T0 + 3000)
    view = db.custody(conn, older_than=3600, now=T0 + 3602)
    assert [c["case_id"] for c in view["cases"]] == ["c001"], "only what finished more than an hour ago"
    assert view["missing_archive"] == 2


def test_the_custody_view_is_one_recipe_at_a_time_when_asked(conn):
    finish(conn, 1, recipe=WIND)
    finish(conn, 2, recipe=THERMAL)
    assert db.custody(conn, recipe=THERMAL, now=T0 + 100)["done"] == 1
    assert db.custody(conn, now=T0 + 100)["done"] == 2


def test_a_case_that_never_said_how_many_directions_it_had_is_judged_on_its_archive(conn):
    db.add_cases(conn, [case(1)])
    lease = db.lease(conn, "foam-1", now=T0, recipes=[WIND])[0]
    assert db.complete(conn, lease.lease_id, "file:///x.tar.gz", sha256=SHA, now=T0 + 1)
    row = db.custody(conn, now=T0 + 100)["cases"][0]
    assert row["missing"] == ["archive"] and row["fields_expected"] is None


@pytest.mark.parametrize("telemetry, want", [
    ({"solve": {"directions_total": 32}}, 32), ({"mesh": {"directions": 8}}, 8),
    ({"mesh": {"directions": ["case_000", "case_090"]}}, 2), ({"solve": {"directions_total": True}}, None),
    ({}, None), (None, None)])
def test_the_expected_field_count_is_read_off_the_telemetry(telemetry, want):
    assert db.expected_fields(WIND, telemetry) == want
    assert db.expected_fields(THERMAL, telemetry) is None


# -- over HTTP -----------------------------------------------------------------------

W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}


def test_the_endpoints_record_refuse_and_list(tmp_path):
    app = create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"])
    c = TestClient(app)
    assert c.post("/v1/cases", json=[{"lat": 33.8, "lon": -84.4, "recipe": WIND, "city_cluster": "atl"}],
                  headers=W).status_code == 200
    lease = c.post("/v1/lease", json={"worker_id": "foam-1", "count": 1}, headers=W).json()[0]
    cid = lease["case_id"]
    assert c.post("/v1/complete", json={"lease_id": lease["lease_id"], "result_uri": "file:///x.tar.gz",
                                         "sha256": SHA, "bytes": 9}, headers=W).status_code == 200

    view = c.get("/v1/custody", headers=R).json()
    assert view["missing_archive"] == 1 and view["cases"][0]["case_id"] == cid

    assert c.post(f"/v1/cases/{cid}/receipts", json={"sha256": OTHER}, headers=W).status_code == 409
    assert c.post(f"/v1/cases/{cid}/receipts", json={"sha256": SHA}, headers=R).status_code in (401, 403), \
        "read scope cannot vouch for an archive"
    r = c.post(f"/v1/cases/{cid}/receipts", json={"sha256": SHA, "bytes": 9, "path": "done/x.tar.gz"}, headers=W)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "archive" and r.json()["location"] == "master"

    assert c.get(f"/v1/cases/{cid}/receipts", headers=R).json()["receipts"][0]["sha256"] == SHA
    assert c.get(f"/v1/cases/{cid}", headers=R).json()["receipts"][0]["path"] == "done/x.tar.gz"
    assert c.get("/v1/custody", headers=R).json()["missing_archive"] == 0


def test_a_case_is_not_stored_until_the_store_holds_every_part_its_nodes_shipped(conn):
    """The archive leaves out what was shipped as parts -- the mesh, each direction -- so a case
    whose archive arrived but whose direction did not is not on the broker. Before a disk that
    still holds results is wiped (PACE's scratch, at the end of a semester), this is the list."""
    db.add_cases(conn, [case(7)])
    lease = db.lease(conn, "ice-4709-0", now=T0, recipes=[WIND])[0]
    mesh, d0, d1 = "c" * 64, "d" * 64, "e" * 64
    for part, sha in (("mesh", mesh), ("case_000", d0), ("case_090", d1)):
        db.report_part(conn, lease.lease_id, lease.case_id, part, f"{lease.case_id}.{part}.tar.gz", sha,
                       size=100, mesh_sha256=None if part == "mesh" else mesh, now=T0)
    assert db.complete(conn, lease.lease_id, f"file:///storage/ice1/0/3/pkastner3/windcomfort/done/{lease.case_id}.tar.gz",
                       sha256=SHA, nbytes=123, now=T0 + 1)
    db.record_blob(conn, lease.case_id, "mesh", mesh, 100, now=T0 + 2)
    db.record_blob(conn, lease.case_id, "case_000", d0, 100, now=T0 + 2)
    db.record_blob(conn, lease.case_id, "archive", SHA, 123, now=T0 + 2)

    view = db.custody(conn, now=T0 + 100)
    row = view["cases"][0]
    assert "parts" in row["missing"] and row["parts_missing"] == ["case_090"]
    assert (view["missing_parts"], view["missing_parts_bytes"]) == (1, 100)
    assert view["by_location"] == {"file:///storage/ice1/0/3/pkastner3/windcomfort/done": 1}

    db.record_blob(conn, lease.case_id, "case_090", d1, 100, now=T0 + 3)
    row = db.custody(conn, now=T0 + 100)["cases"]
    assert not row or "parts" not in row[0]["missing"]


def test_the_cli_says_whether_everything_is_on_the_broker(tmp_path, capsys):
    import socket
    import threading
    import time

    import uvicorn

    from casebroker import cli

    app = create_app(db_path=str(tmp_path / "cli.sqlite"), tokens=["w"], readonly_tokens=["r"])
    conn = db.connect(app.state.db_path)
    finish(conn, 1, directions=0)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        while not server.started:
            time.sleep(0.05)
        rc = cli.main(["custody", "--broker", f"http://127.0.0.1:{port}", "--token", "r"])
        out = capsys.readouterr().out
        assert rc == 1 and "NOT everything is on the broker yet" in out and "c001" in out
        db.record_blob(conn, "c001", "archive", SHA, 123)
        rc = cli.main(["custody", "--broker", f"http://127.0.0.1:{port}", "--token", "r"])
        out = capsys.readouterr().out
        assert rc == 0 and "every finished case is on the broker" in out
    finally:
        server.should_exit = True

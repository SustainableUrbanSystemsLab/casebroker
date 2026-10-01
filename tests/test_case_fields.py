"""The broker holds the pedestrian wind field itself, not a pointer to it.

Every other artefact of a case lives on the Syncthing master and the broker
records where; the published field -- |U| at 1.75 m above grade on the 2 m
grid, per direction -- is the one the campaign is FOR, and a megabyte per
direction fits the fleet's own database now (Patrick, 2026-09-28). A node
sends it the moment a direction finishes, under its lease; anyone with read
scope gets it back as the same bytes.
"""
from __future__ import annotations

import gzip
import json
import math
import pathlib
import struct
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

V4 = "cyl-1008/of12-v4"
CASE = "v2-00e76e426bea6d52"
W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}


def umag(values, nx=3, ny=2, height=1.75, direction="case_000", case_id=CASE, **extra) -> bytes:
    """A umag/1 blob as the node writes it (MetaFOAM.Deploy.PedestrianField.Encode)."""
    header = {"format": "umag/1", "case_id": case_id, "direction": direction, "deg": 0.0,
              "height_m": height, "nx": nx, "ny": ny, "x0": -2.0, "y0": -1.0, "spacing_m": 2.0,
              "coverage": 0.8, "u_ref": 3.1, "umag_p999": 5.5}
    header.update(extra)
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    body = struct.pack("<%df" % len(values), *values)
    return gzip.compress(b"UMAG" + struct.pack("<II", 1, len(hb)) + hb + body, mtime=0)


def _case() -> dict:
    return {"case_id": CASE, "spec": {"lat": 1.0, "lon": 2.0, "recipe": V4},
            "recipe": V4, "city_cluster": "x", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "p.sqlite"))
    db.add_cases(c, [_case()])
    return c


@pytest.fixture()
def client(tmp_path):
    """An HTTP client over a broker holding one pending case, leased to foam-1.
    Yields (client, case_id, lease_id): over HTTP the id is the broker's to
    derive from the coordinates, so the test reads it back off the lease."""
    app = create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"])
    c = TestClient(app)
    r = c.post("/v1/cases", json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}], headers=W)
    assert r.status_code == 200, r.text
    lease = c.post("/v1/lease", json={"worker_id": "foam-1", "count": 1}, headers=W).json()[0]
    return c, lease["case_id"], lease["lease_id"]


VALUES = [1.0, 2.0, float("nan"), 4.0, 5.5, 0.0]


def test_the_lease_holder_stores_a_field_and_it_is_read_back_byte_for_byte(conn):
    lease = db.lease(conn, "foam-1")[0]
    blob = umag(VALUES)
    assert db.put_field(conn, lease.lease_id, CASE, "case_000", blob) == "ok"

    got = db.case_field(conn, CASE, "case_000")
    assert got["blob"] == blob and got["bytes"] == len(blob)
    assert (got["nx"], got["ny"], got["x0"], got["y0"], got["spacing_m"]) == (3, 2, -2.0, -1.0, 2.0)
    assert (got["height_m"], got["coverage"], got["u_ref"], got["deg"], got["umag_p999"]) == (1.75, 0.8, 3.1, 0.0, 5.5)
    assert got["worker_id"] == "foam-1"
    listed = db.case_fields(conn, CASE)
    assert [(f["direction"], f["height_m"]) for f in listed] == [("case_000", 1.75)]
    assert "blob" not in listed[0], "the listing carries no bytes"


def test_a_stale_or_foreign_lease_is_refused_and_nothing_is_stored(conn):
    first = db.lease(conn, "foam-1")[0]
    conn.execute("UPDATE cases SET lease_expires=0 WHERE case_id=?", (CASE,))
    second = db.lease(conn, "foam-2")[0]
    assert db.put_field(conn, first.lease_id, CASE, "case_000", umag(VALUES)) == "gone"
    assert db.put_field(conn, second.lease_id, "v2-other", "case_000", umag(VALUES, case_id="v2-other")) == "gone"
    assert db.put_field(conn, second.lease_id, CASE, "case_000", umag(VALUES),
                        worker_ok=lambda w: w == "someone-else") == "gone"
    assert db.case_fields(conn, CASE) == []


def test_a_direction_solved_again_replaces_its_field(conn):
    lease = db.lease(conn, "foam-1")[0]
    assert db.put_field(conn, lease.lease_id, CASE, "case_000", umag(VALUES)) == "ok"
    again = umag([9.0] * 6)
    assert db.put_field(conn, lease.lease_id, CASE, "case_000", again) == "ok"
    assert db.case_field(conn, CASE, "case_000")["blob"] == again
    assert len(db.case_fields(conn, CASE)) == 1


def test_two_heights_are_two_rows_and_the_published_one_is_the_default(conn):
    lease = db.lease(conn, "foam-1")[0]
    assert db.put_field(conn, lease.lease_id, CASE, "case_000", umag(VALUES, height=1.5)) == "ok"
    assert db.put_field(conn, lease.lease_id, CASE, "case_000", umag(VALUES, height=1.75)) == "ok"
    assert [f["height_m"] for f in db.case_fields(conn, CASE)] == [1.5, 1.75]
    assert db.case_field(conn, CASE, "case_000")["height_m"] == 1.75
    assert db.case_field(conn, CASE, "case_000", 1.5)["height_m"] == 1.5
    assert db.case_field(conn, CASE, "case_000", 2.0) is None, "an exact height asked for is exact"


@pytest.mark.parametrize("bad, why", [
    (b"not gzip at all", "gzip"),
    (gzip.compress(b"WFLD" + b"\0" * 20), "UMAG"),
    (gzip.compress(b"UMAG" + struct.pack("<II", 2, 0)), "version"),
    (gzip.compress(b"UMAG" + struct.pack("<II", 1, 3) + b"{}}"), "JSON"),
    (gzip.compress(b"UMAG" + struct.pack("<II", 1, 2) + b"{}"), "format"),
])
def test_a_blob_that_is_not_a_field_is_named_for_what_is_wrong(conn, bad, why):
    lease = db.lease(conn, "foam-1")[0]
    out = db.put_field(conn, lease.lease_id, CASE, "case_000", bad)
    assert out.startswith("invalid:") and why in out, out
    assert db.case_fields(conn, CASE) == []


def test_a_body_that_does_not_match_its_header_is_refused(conn):
    lease = db.lease(conn, "foam-1")[0]
    short = umag(VALUES[:5])                                   # header says 3 x 2
    out = db.put_field(conn, lease.lease_id, CASE, "case_000", short)
    assert out.startswith("invalid:") and "20 bytes" in out and "24" in out, out
    lying = umag(VALUES, direction="case_045")
    assert "case_045" in db.put_field(conn, lease.lease_id, CASE, "case_000", lying)
    assert db.put_field(conn, lease.lease_id, CASE, "mesh", umag(VALUES, direction="mesh")) == "invalid: not a direction name"


def test_the_blob_is_stored_and_served_verbatim_over_http(client):
    client, CASE, lease_id = client
    blob = umag(VALUES, case_id=CASE)
    r = client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
                   content=blob, headers={**W, "Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json() == {"ok": True, "bytes": len(blob)}

    listed = client.get(f"/v1/cases/{CASE}/fields", headers=R).json()["fields"]
    assert [(f["direction"], f["height_m"], f["nx"], f["ny"]) for f in listed] == [("case_000", 1.75, 3, 2)]
    assert client.get(f"/v1/cases/{CASE}", headers=R).json()["fields"] == listed, \
        "the case record says which fields it has"

    got = client.get(f"/v1/cases/{CASE}/fields/case_000", headers=R)
    assert got.status_code == 200 and got.content == blob
    assert got.headers["content-type"].startswith("application/octet-stream")
    assert got.headers["x-field-height-m"] == "1.75"
    raw = gzip.decompress(got.content)
    hlen = struct.unpack("<I", raw[8:12])[0]
    vals = struct.unpack("<6f", raw[12 + hlen:])
    assert vals[:2] == (1.0, 2.0) and math.isnan(vals[2]) and vals[3:] == (4.0, 5.5, 0.0)

    again = client.get(f"/v1/cases/{CASE}/fields/case_000", headers={**R, "If-None-Match": got.headers["etag"]})
    assert again.status_code == 304
    assert client.get(f"/v1/cases/{CASE}/fields/case_045", headers=R).status_code == 404


def test_write_scope_is_needed_to_put_and_a_lost_lease_answers_409(client):
    client, CASE, lease_id = client
    blob = umag(VALUES, case_id=CASE)
    assert client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
                      content=blob, headers=R).status_code in (401, 403), "read scope cannot write"
    assert client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": "nope"},
                      content=blob, headers=W).status_code == 409
    assert client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
                      content=b"junk", headers=W).status_code == 422
    assert client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
                      content=b"", headers=W).status_code == 422


def test_a_body_past_the_limit_is_413_before_it_is_looked_at(client, monkeypatch):
    monkeypatch.setattr(db, "FIELD_MAX_BYTES", 100)
    client, CASE, lease_id = client
    r = client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
                   content=b"x" * 101, headers=W)
    assert r.status_code == 413


def test_storage_reports_the_fields_table(client):
    client, CASE, lease_id = client
    client.put(f"/v1/cases/{CASE}/fields/case_000", params={"lease_id": lease_id},
               content=umag(VALUES, case_id=CASE), headers=W)
    tables = {t["name"]: t for t in client.get("/v1/storage", headers=R).json()["tables"]}
    assert tables["case_fields"]["rows"] == 1

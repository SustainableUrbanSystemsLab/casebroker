"""The broker runs ONE event loop, and a database call made on it holds every request.

In production `db._locked` takes one of `CASEBROKER_PG_POOL` (4) Postgres sessions and
waits for it with no timeout. The three async routes -- a part's chunk, a direction's
field, a field's backfill -- made their database calls on the event loop itself, so
whenever the sync routes held every session nothing at all was accepted or answered.
Heartbeats then ran past a node's 30 s, and a node from before Eddy3D 529fa324 lost its
whole heartbeat to that: 33 v6 cases were quarantined "its lease expired" on
2026-10-10..11, every stall the nodes logged beside a 250-325 MB part upload.

So every database call an async route makes must run on a worker thread. Each test wraps
the database entry points the route reaches and notes whether an event loop was running
on the thread that called them.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import pathlib
import struct
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

V4 = "cyl-1008/of12-v4"
W = {"Authorization": "Bearer w"}


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture()
def calls(monkeypatch):
    """(entry point, ran on the event loop) for every database call the routes make."""
    seen: list[tuple[str, bool]] = []
    for name in ("put_field", "backfill_field", "worker_token_owner", "session_user", "blob_expected"):
        real = getattr(db, name)

        def spy(*a, _real=real, _name=name, **kw):
            seen.append((_name, _on_loop()))
            return _real(*a, **kw)
        monkeypatch.setattr(db, name, spy)
    return seen


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PARTS_RESERVE_GB", "0")
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"],
                              parts_dir=str(tmp_path / "parts")))
    r = c.post("/v1/cases", headers=W, json=[{"lat": 33.8, "lon": -84.4, "recipe": V4, "city_cluster": "atl"}])
    assert r.status_code == 200, r.text
    lease = c.post("/v1/lease", headers=W, json={"worker_id": "foam-1"}).json()[0]
    return c, lease["case_id"], lease["lease_id"], tmp_path / "a.sqlite"


def _field(case_id: str, direction: str = "case_000") -> bytes:
    header = {"format": "umag/1", "case_id": case_id, "direction": direction, "deg": 0.0,
              "height_m": 1.75, "nx": 3, "ny": 2, "x0": -2.0, "y0": -1.0, "spacing_m": 2.0,
              "coverage": 0.8, "u_ref": 3.1, "umag_p999": 5.5}
    hb = json.dumps(header, separators=(",", ":")).encode()
    body = struct.pack("<6f", 1.0, 2.0, 3.0, 4.0, 5.5, 0.0)
    return gzip.compress(b"UMAG" + struct.pack("<II", 1, len(hb)) + hb + body, mtime=0)


def _on_the_loop(calls):
    return [name for name, on in calls if on]


def test_a_directions_field_is_stored_off_the_event_loop(broker, calls):
    c, case, lease_id, _ = broker
    r = c.put(f"/v1/cases/{case}/fields/case_000", headers=W, params={"lease_id": lease_id},
              content=_field(case))
    assert r.status_code == 200, r.text
    assert ("put_field", False) in calls, calls
    assert _on_the_loop(calls) == [], calls


def test_a_backfilled_field_is_stored_off_the_event_loop(broker, calls):
    c, case, lease_id, path = broker
    side = db.connect(str(path))
    assert db.complete(side, lease_id, f"file:///done/{case}.tar.gz", "ab" * 32, 10, {}) is True
    r = c.put(f"/v1/cases/{case}/fields/case_000/backfill", headers=W, content=_field(case))
    assert r.status_code == 200, r.text
    assert ("backfill_field", False) in calls, calls
    assert _on_the_loop(calls) == [], calls


def test_a_parts_chunks_are_taken_off_the_event_loop(broker, calls):
    c, case, lease_id, _ = broker
    mesh = b"a mesh, as small as a test can make one"
    sha = hashlib.sha256(mesh).hexdigest()
    r = c.post("/v1/parts", headers=W, json={"lease_id": lease_id, "case_id": case, "part": "mesh",
                                             "archive": f"{case}.mesh.tar.gz", "sha256": sha,
                                             "bytes": len(mesh)})
    assert r.status_code == 200, r.text
    assert c.post(f"/v1/cases/{case}/parts/mesh/upload", headers=W,
                  json={"sha256": sha, "bytes": len(mesh)}).status_code == 200
    calls.clear()
    for offset in range(0, len(mesh), 16):
        r = c.put(f"/v1/cases/{case}/parts/mesh/upload", headers=W,
                  params={"offset": offset, "bytes": len(mesh)}, content=mesh[offset:offset + 16])
        assert r.status_code == 200, r.text
    # Who sent it (the receipt's `by`) is asked on every chunk; never on the loop.
    assert ("worker_token_owner", False) in calls, calls
    assert _on_the_loop(calls) == [], calls

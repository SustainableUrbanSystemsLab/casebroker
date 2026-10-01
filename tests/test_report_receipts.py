"""scripts/report_receipts.py: the master's half of custody, against the real broker app.

It hashes only what the broker lists as missing, reports a case whose archive (and every part
its manifest names) is on the master, skips one still syncing, and its receipt is checked by the
broker against the hash the node reported -- so a corrupted copy is refused, not recorded.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tarfile

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casebroker.app import create_app  # noqa: E402

_spec = importlib.util.spec_from_file_location("report_receipts", ROOT / "scripts" / "report_receipts.py")
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

W = {"Authorization": "Bearer w"}
WIND = "cyl-1008/of12-v6"


def write_archive(done: pathlib.Path, case_id: str, payload: bytes = b"solved") -> str:
    """A case archive as a node hands it off: <case>/manifest.json inside <case>.tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in ((f"{case_id}/manifest.json", json.dumps({"case_id": case_id}).encode()),
                           (f"{case_id}/result.bin", payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    path = done / f"{case_id}.tar.gz"
    path.write_bytes(buf.getvalue())
    return hashlib.sha256(buf.getvalue()).hexdigest()


class AppBroker(rr.Broker):
    """The script's Broker, talking to the real app through TestClient."""

    def __init__(self, client: TestClient):
        super().__init__("http://test", "w")
        self.client = client

    def call(self, method, path, body=None):
        r = self.client.request(method, path, json=body, headers=W)
        if r.status_code >= 400:
            import urllib.error
            raise urllib.error.HTTPError(path, r.status_code, r.text, None, io.BytesIO(r.content))
        return r.json()


def broker_with_done_cases(tmp_path, n: int):
    app = create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"])
    c = TestClient(app)
    ids = []
    for i in range(n):
        assert c.post("/v1/cases", json=[{"lat": 10.0 + i, "lon": 20.0, "recipe": WIND, "city_cluster": f"c{i}"}],
                      headers=W).status_code == 200
        lease = c.post("/v1/lease", json={"worker_id": "foam-1", "count": 1, "recipes": [WIND]}, headers=W).json()[0]
        ids.append((lease["case_id"], lease["lease_id"]))
    return c, ids


def complete(c, lease_id: str, sha: str):
    assert c.post("/v1/complete", json={"lease_id": lease_id, "result_uri": "file:///x.tar.gz", "sha256": sha},
                  headers=W).status_code == 200


def test_a_complete_archive_is_reported_and_the_rest_are_named(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    c, ids = broker_with_done_cases(tmp_path, 3)
    (here, l1), (syncing, l2), (elsewhere, l3) = ids
    complete(c, l1, write_archive(done, here))
    # Only a part has arrived for the second: still syncing.
    (done / f"{syncing}.case_000.tar.gz").write_bytes(b"part")
    complete(c, l2, "c" * 64)
    complete(c, l3, "d" * 64)                       # never reaches this master

    dry = rr.report(AppBroker(c), done, post=False, pause=0)
    assert dry["reported"] == [here] and c.get("/v1/custody", headers=W).json()["missing_archive"] == 3, \
        "a dry run reports nothing"

    out = rr.report(AppBroker(c), done, post=True, pause=0)
    assert (out["reported"], out["syncing"], out["not_here"]) == ([here], [syncing], [elsewhere])
    view = c.get("/v1/custody", headers=W).json()
    assert view["missing_archive"] == 2 and here not in [x["case_id"] for x in view["cases"]]
    receipt = c.get(f"/v1/cases/{here}/receipts", headers=W).json()["receipts"][0]
    assert receipt["path"] == f"{here}.tar.gz" and receipt["bytes"] == (done / f"{here}.tar.gz").stat().st_size


def test_a_corrupted_copy_is_refused_by_the_broker_and_left_on_the_list(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    c, [(cid, lease_id)] = broker_with_done_cases(tmp_path, 1)
    complete(c, lease_id, write_archive(done, cid, b"what the node archived"))
    write_archive(done, cid, b"what arrived")       # the copy on the master differs
    out = rr.report(AppBroker(c), done, post=True, pause=0)
    assert out["reported"] == [] and len(out["refused"]) == 1 and "409" in out["refused"][0]
    assert c.get("/v1/custody", headers=W).json()["missing_archive"] == 1

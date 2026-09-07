"""A retried `complete` must not be mistaken for a zombie.

worker._post retries on transport error. If the broker commits a complete but the
response is lost, the identical call arrives again carrying a lease_id the first
write already nulled. That used to answer 409 -- the code for "another worker owns
this now" -- so the worker logged a false failure for work that had landed.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from casebroker.app import create_app  # noqa: E402

CASE = {"lat": 34.0, "lon": -84.0, "recipe": "fixed-box-1008/of12", "city_cluster": "atlanta"}


def _client(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "i.sqlite"), tokens=["t"]))
    c.headers.update({"Authorization": "Bearer t"})
    return c


def test_identical_retry_of_a_landed_complete_is_200(tmp_path):
    c = _client(tmp_path)
    c.post("/v1/cases", json=[CASE])
    lease = c.post("/v1/lease", json={"worker_id": "w", "count": 1}).json()[0]
    body = {"lease_id": lease["lease_id"], "result_uri": "s3://wind-v2/one.tar"}
    assert c.post("/v1/complete", json=body).status_code == 200
    # the response was lost; the worker sends the very same call again
    assert c.post("/v1/complete", json=body).status_code == 200
    assert c.get(f"/v1/cases/{lease['case_id']}").json()["state"] == "done"


def test_a_different_result_for_a_finished_lease_is_still_refused(tmp_path):
    """Idempotency must not become 'anyone holding an old lease_id may overwrite'."""
    c = _client(tmp_path)
    c.post("/v1/cases", json=[CASE])
    lease = c.post("/v1/lease", json={"worker_id": "w", "count": 1}).json()[0]
    ok = {"lease_id": lease["lease_id"], "result_uri": "s3://wind-v2/one.tar"}
    assert c.post("/v1/complete", json=ok).status_code == 200
    r = c.post("/v1/complete", json={**ok, "result_uri": "s3://wind-v2/OTHER.tar"})
    assert r.status_code == 409
    assert c.get(f"/v1/cases/{lease['case_id']}").json()["result_uri"] == "s3://wind-v2/one.tar"

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


def test_a_retry_is_not_confirmed_by_a_different_case(tmp_path):
    """The duplicate check used to match on result_uri alone.

    It exists so that a `complete` whose RESPONSE was lost can be retried: the
    first write nulls the lease, so the retry's lookup misses and would be
    refused with 409, which the worker reads as "another worker took this" and
    logs as a failure for work that actually landed.

    Matching on the URI alone meant ANY done case carrying that URI answered for
    this one. A runner deriving its result_uri from anything less unique than the
    case -- a template, a constant, a date -- would have retries on case B
    silently confirmed by case A's row, reporting work as landed that never ran.
    """
    from casebroker import db

    conn = db.connect(str(tmp_path / "d.sqlite"))
    db.add_cases(conn, [
        {"case_id": "A", "spec": {"lat": 1, "lon": 2}, "recipe": "r",
         "city_cluster": "x", "lcz": "LCZ1", "split": "train",
         "priority": 100, "max_attempts": 3},
        {"case_id": "B", "spec": {"lat": 3, "lon": 4}, "recipe": "r",
         "city_cluster": "x", "lcz": "LCZ1", "split": "train",
         "priority": 100, "max_attempts": 3}])

    held = {g.case_id: g for g in db.lease(conn, "w1", count=2)}
    assert set(held) == {"A", "B"}

    # A finishes, writing a URI a careless runner reuses.
    assert db.complete(conn, held["A"].lease_id, "s3://bucket/SHARED", case_id="A")

    # B's solve dies instead, so its lease stops resolving -- the same state a
    # `complete` whose response was lost would retry into.
    db.fail(conn, held["B"].lease_id, "transport died", retryable=True)
    b = held["B"]

    # The retry names its own case, so the shared URI cannot answer for it.
    assert db.complete(conn, b.lease_id, "s3://bucket/SHARED", case_id="B") is False
    assert db.get_case(conn, "B")["state"] != "done"


def test_a_genuine_lost_response_is_still_confirmed(tmp_path):
    """The idempotency this check exists for must keep working."""
    from casebroker import db

    conn = db.connect(str(tmp_path / "e.sqlite"))
    db.add_cases(conn, [
        {"case_id": "A", "spec": {"lat": 1, "lon": 2}, "recipe": "r",
         "city_cluster": "x", "lcz": "LCZ1", "split": "train",
         "priority": 100, "max_attempts": 3}])
    got = db.lease(conn, "w1", count=1)[0]
    assert db.complete(conn, got.lease_id, "s3://bucket/A", case_id="A")
    # Same worker, same lease, same result: the response was lost, not the work.
    assert db.complete(conn, got.lease_id, "s3://bucket/A", case_id="A") is True


def test_a_worker_without_case_id_still_completes(tmp_path):
    """Additive: a worker built before this field must be unaffected."""
    from casebroker import db

    conn = db.connect(str(tmp_path / "f.sqlite"))
    db.add_cases(conn, [
        {"case_id": "A", "spec": {"lat": 1, "lon": 2}, "recipe": "r",
         "city_cluster": "x", "lcz": "LCZ1", "split": "train",
         "priority": 100, "max_attempts": 3}])
    got = db.lease(conn, "w1", count=1)[0]
    assert db.complete(conn, got.lease_id, "s3://bucket/A")
    assert db.get_case(conn, "A")["state"] == "done"

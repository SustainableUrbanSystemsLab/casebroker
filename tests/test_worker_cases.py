"""GET /v1/workers/{id}/cases: every case one worker took, and how each go ended.

For debugging a machine. `fail` and `complete` null cases.lease_worker, so the only
record of who solved or failed what is the events trail; this reads it.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from casebroker.app import create_app

CASE = {"lat": 34.0, "lon": -84.0, "recipe": "fixed-box-1008/of12", "city_cluster": "atlanta"}


def _client(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "w.sqlite"), tokens=["t"]))
    c.headers.update({"Authorization": "Bearer t"})
    return c


def _take(c, worker):
    return c.post("/v1/lease", json={"worker_id": worker, "count": 1}).json()[0]


def _posted(c, n):
    c.post("/v1/cases", json=[{**CASE, "lat": 34.0 + i / 100} for i in range(n)])


def test_a_worker_lists_what_it_finished_and_what_it_failed(tmp_path):
    c = _client(tmp_path)
    _posted(c, 3)
    ok, bad, running = _take(c, "w1"), _take(c, "w1"), _take(c, "w1")
    assert c.post("/v1/complete", json={"lease_id": ok["lease_id"], "result_uri": "s3://x/1.tar"}).status_code == 200
    assert c.post("/v1/fail", json={"lease_id": bad["lease_id"], "error": "disk full",
                                    "retryable": False}).status_code == 200
    body = c.get("/v1/workers/w1/cases").json()
    assert body["total"] == 3
    by = {r["case_id"]: r for r in body["cases"]}
    assert by[ok["case_id"]]["outcome"] == "done"
    assert by[bad["case_id"]]["outcome"] == "quarantined"
    assert by[bad["case_id"]]["detail"] == "disk full"
    assert by[running["case_id"]]["outcome"] == "running"
    assert by[running["case_id"]]["held_by"] == "w1"
    assert body["counts"] == {"done": 1, "failed": 0, "quarantined": 1, "released": 0,
                              "stale-released": 0, "cancelled": 0, "running": 1}
    assert body["errors"][0]["error"] == "disk full" and body["errors"][0]["n"] == 1


def test_another_workers_cases_are_not_in_the_list(tmp_path):
    c = _client(tmp_path)
    _posted(c, 2)
    mine, theirs = _take(c, "w1"), _take(c, "w2")
    ids = [r["case_id"] for r in c.get("/v1/workers/w1/cases").json()["cases"]]
    assert ids == [mine["case_id"]] and theirs["case_id"] not in ids


def test_outcome_filter_and_paging(tmp_path):
    c = _client(tmp_path)
    _posted(c, 4)
    for i in range(4):
        lease = _take(c, "w1")
        if i % 2:
            c.post("/v1/complete", json={"lease_id": lease["lease_id"], "result_uri": f"s3://x/{i}.tar"})
        else:
            c.post("/v1/fail", json={"lease_id": lease["lease_id"], "error": "boom", "retryable": False})
    done = c.get("/v1/workers/w1/cases", params={"outcome": "done"}).json()
    assert done["total"] == 2 and {r["outcome"] for r in done["cases"]} == {"done"}
    first = c.get("/v1/workers/w1/cases", params={"limit": 3}).json()
    rest = c.get("/v1/workers/w1/cases", params={"limit": 3, "offset": 3}).json()
    assert len(first["cases"]) == 3 and len(rest["cases"]) == 1
    assert len({r["case_id"] for r in first["cases"] + rest["cases"]}) == 4
    # a stale bookmark's unknown filter shows rows rather than none
    assert c.get("/v1/workers/w1/cases", params={"outcome": "nonsense"}).json()["total"] == 4


def test_an_unknown_worker_is_404_and_a_known_one_without_cases_is_empty(tmp_path):
    c = _client(tmp_path)
    assert c.get("/v1/workers/nobody/cases").status_code == 404
    _posted(c, 1)
    _take(c, "w1")
    c.post("/v1/lease", json={"worker_id": "idle", "count": 1})
    empty = c.get("/v1/workers/idle/cases").json()
    assert empty["total"] == 0 and empty["cases"] == [] and empty["worker"]["worker_id"] == "idle"

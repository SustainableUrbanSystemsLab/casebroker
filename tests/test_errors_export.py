"""GET /v1/errors: every failing case in one answer, for a bug report."""
from fastapi.testclient import TestClient

from casebroker import db
from casebroker.app import create_app

W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}


def _client(tmp_path):
    return TestClient(create_app(str(tmp_path / "b.sqlite"), tokens=["w"], readonly_tokens=["r"]))


def _cases(c, n):
    # Munich: on land, so the ingest gate keeps them.
    body = [{"lat": 48.10 + i * 0.01, "lon": 11.57, "recipe": "r", "city_cluster": "munich",
             "max_attempts": 2} for i in range(n)]
    assert c.post("/v1/cases", json=body, headers=W).json()["added"] == n


def _fail(c, worker, error, retryable=True):
    lease = c.post("/v1/lease", json={"worker_id": worker, "host": "node-" + worker}, headers=W).json()[0]
    assert c.post("/v1/fail", json={"lease_id": lease["lease_id"], "error": error,
                                    "retryable": retryable}, headers=W).status_code == 200
    return lease["case_id"]


def test_no_errors_is_an_empty_answer_not_a_404(tmp_path):
    c = _client(tmp_path)
    _cases(c, 2)
    assert c.get("/v1/errors", headers=R).json() == {"total": 0, "returned": 0,
                                                     "truncated": False, "cases": []}


def test_every_failed_attempt_is_there_with_the_machine_it_failed_on(tmp_path):
    c = _client(tmp_path)
    _cases(c, 1)
    cid = _fail(c, "a", "snappyHexMesh exited 137")
    assert _fail(c, "b", "solve exited 1: FOAM FATAL ERROR\nmaximum number of iterations exceeded") == cid

    data = c.get("/v1/errors", headers=R).json()
    assert data["total"] == 1
    case = data["cases"][0]
    assert case["case_id"] == cid and case["state"] == "quarantined" and case["attempts"] == 2
    # The final error in full, and the FIRST attempt's different one -- which
    # last_error alone would have lost.
    assert case["last_error"].endswith("maximum number of iterations exceeded")
    assert [(h["event"], h["worker_id"], h["host"], h["detail"].split("\n")[0]) for h in case["history"]] == [
        ("failed", "a", "node-a", "snappyHexMesh exited 137"),
        ("quarantined", "b", "node-b", "solve exited 1: FOAM FATAL ERROR"),
    ]


def test_quarantined_cases_come_first_and_a_limit_says_it_truncated(tmp_path):
    c = _client(tmp_path)
    _cases(c, 2)
    dead = _fail(c, "a", "tile not published", retryable=False)
    retrying = _fail(c, "a", "transient")          # the more RECENT of the two
    assert retrying != dead

    data = c.get("/v1/errors?limit=1", headers=R).json()
    assert (data["total"], data["returned"], data["truncated"]) == (2, 1, True)
    assert data["cases"][0]["case_id"] == dead, "what will never run again outranks what is about to be retried"
    assert [x["case_id"] for x in c.get("/v1/errors", headers=R).json()["cases"]] == [dead, retrying]


def test_a_case_that_later_succeeds_drops_out(tmp_path):
    c = _client(tmp_path)
    _cases(c, 1)
    _fail(c, "a", "transient")
    lease = c.post("/v1/lease", json={"worker_id": "a"}, headers=W).json()[0]
    assert c.post("/v1/complete", json={"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                                        "result_uri": "file:///x"}, headers=W).status_code == 200
    assert c.get("/v1/errors", headers=R).json()["total"] == 0


def test_it_needs_a_credential(tmp_path):
    assert _client(tmp_path).get("/v1/errors").status_code == 401


def test_thousands_of_histories_are_one_pass_not_a_query_per_case(tmp_path):
    c = _client(tmp_path)
    _cases(c, 1200)                       # past the 500-id chunk, twice
    conn = db.connect(str(tmp_path / "b.sqlite"))
    for _ in range(1200):
        lease = db.lease(conn, "w", 1)[0]
        db.fail(conn, lease.lease_id, "boom", retryable=False)
    data = c.get("/v1/errors?limit=5000", headers=R).json()
    assert data["returned"] == 1200 and all(len(x["history"]) == 1 for x in data["cases"])
    assert data["cases"][0]["lat"] is not None

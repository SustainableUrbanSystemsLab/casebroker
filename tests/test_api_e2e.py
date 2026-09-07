"""API and end-to-end tests.

The unit tests in ``test_db.py`` prove the SQL is right. These prove the thing
workers actually talk to is right, and the end-to-end test runs a REAL uvicorn
server with REAL worker clients over REAL HTTP -- because the failure this whole
component exists to prevent (two machines simulating the same case for six hours)
is a concurrency failure, and a concurrency failure is not observable in a test
that calls functions in one thread.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def broker(tmp_path):
    """A freshly-bound app per test. No module reloading, no shared globals --
    each test owns its database outright."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    application = create_app(db_path=str(tmp_path / "api.sqlite"),
                             tokens=["secret-a", "secret-b"])
    c = TestClient(application)
    c.headers.update({"Authorization": "Bearer secret-a"})
    return c


@pytest.fixture()
def client(broker):
    return broker


def _cases(n=5):
    return [{"lat": 40.7 + i * 0.01, "lon": -74.0, "recipe": "fixed-cyl-500/of12",
             "city_cluster": "nyc" if i < 3 else "chi", "lcz": "LCZ1",
             "spec": {"dirs": [0, 45, 90]}} for i in range(n)]


def test_auth_is_enforced(client):
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": ""}).status_code == 401
    # A second configured token is equally valid -- rotation without downtime.
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": "Bearer secret-b"}).status_code == 200


def test_healthz_says_whether_auth_is_on(client):
    body = client.get("/healthz").json()
    assert body["ok"] and body["auth"] == "token"


def test_add_lease_complete_roundtrip(client):
    assert client.post("/v1/cases", json=_cases(5)).json() == {"added": 5, "skipped": 0}
    # Re-posting the identical list adds nothing: this is the "grow the dataset"
    # path, and it must be safe to run twice.
    assert client.post("/v1/cases", json=_cases(5)).json() == {"added": 0, "skipped": 5}

    got = client.post("/v1/lease", json={"worker_id": "w1", "count": 2}).json()
    assert len(got) == 2
    assert all(g["spec"]["dirs"] == [0, 45, 90] for g in got)

    r = client.post("/v1/complete", json={
        "lease_id": got[0]["lease_id"], "result_uri": "s3://b/x.npz",
        "bytes": 123, "metrics": {"cells": 1858222}})
    assert r.status_code == 200

    st = client.get("/v1/status").json()
    assert st["by_state"]["done"] == 1
    case = client.get(f"/v1/cases/{got[0]['case_id']}").json()
    assert case["state"] == "done" and case["result_bytes"] == 123


def test_superseded_lease_is_rejected_with_409(client):
    client.post("/v1/cases", json=_cases(1))
    got = client.post("/v1/lease", json={"worker_id": "w1", "lease_seconds": 60}).json()[0]
    # Forcibly expire, then let another worker take it.
    from casebroker import db as dbm
    con = dbm.connect(client.app.state.db_path)
    con.execute("UPDATE cases SET lease_expires = 0")
    other = client.post("/v1/lease", json={"worker_id": "w2"}).json()[0]
    assert other["case_id"] == got["case_id"]

    for path, payload in (
        ("/v1/heartbeat", {"lease_id": got["lease_id"]}),
        ("/v1/complete", {"lease_id": got["lease_id"], "result_uri": "x"}),
        ("/v1/fail", {"lease_id": got["lease_id"], "error": "late"}),
        ("/v1/release", {"lease_id": got["lease_id"]}),
    ):
        assert client.post(path, json=payload).status_code == 409, path


def test_splits_are_assigned_by_city_not_by_tile(client):
    """Geographic leakage guard: every tile of one city lands in one split."""
    client.post("/v1/cases", json=_cases(5))
    from casebroker import db as dbm
    rows = dbm.connect(client.app.state.db_path).execute(
        "SELECT city_cluster, COUNT(DISTINCT split) d FROM cases GROUP BY city_cluster"
    ).fetchall()
    assert rows and all(r["d"] == 1 for r in rows), \
        "a city's tiles were split across train/test -- that leaks geometry"


# -- real server, real workers, real HTTP -------------------------------------

@pytest.mark.timeout(120) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_many_workers_never_duplicate_a_case(tmp_path, monkeypatch):
    import uvicorn
    import httpx

    os.environ["CASEBROKER_FAKE_SECONDS"] = "0.01"
    from casebroker.app import create_app
    from casebroker.worker import Worker, echo_runner
    application = create_app(db_path=str(tmp_path / "e2e.sqlite"), tokens=["tok"])

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                           log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("server did not come up")

    n_cases, n_workers = 60, 8
    cases = [{"lat": 40.0 + i * 0.001, "lon": -74.0, "recipe": "r",
              "city_cluster": f"c{i % 7}", "spec": {}} for i in range(n_cases)]
    r = httpx.post(base + "/v1/cases", json=cases,
                   headers={"Authorization": "Bearer tok"}, timeout=30)
    assert r.json()["added"] == n_cases

    done_by: list[tuple[str, str]] = []
    lock = threading.Lock()

    def run_worker(k):
        w = Worker(base, "tok", worker_id=f"w{k}", lease_seconds=120,
                   heartbeat_seconds=3600)

        def runner(lease, worker):
            out = echo_runner(lease, worker)
            with lock:
                done_by.append((lease["case_id"], f"w{k}"))
            return out
        w.run_forever(runner, idle_backoff=1, max_idle_polls=3)

    threads = [threading.Thread(target=run_worker, args=(k,)) for k in range(n_workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)

    # Ask the server everything BEFORE stopping it -- a status query after
    # should_exit is a connection-refused, not a finding.
    st = httpx.get(base + "/v1/status", headers={"Authorization": "Bearer tok"},
                   timeout=10).json()
    server.should_exit = True
    t.join(timeout=15)

    ids_done = [c for c, _ in done_by]
    dupes = {c for c in ids_done if ids_done.count(c) > 1}
    assert not dupes, f"{len(dupes)} case(s) simulated more than once: {sorted(dupes)[:5]}"
    assert len(set(ids_done)) == n_cases, (
        f"only {len(set(ids_done))}/{n_cases} cases were simulated")
    assert st["by_state"].get("done") == n_cases
    # More than one worker actually participated, or the test proved nothing
    # about concurrency.
    assert len({w for _, w in done_by}) > 1

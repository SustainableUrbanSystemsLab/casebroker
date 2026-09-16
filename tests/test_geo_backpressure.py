"""Site previews are bounded, because their memory ceilings are per call.

Each `/footprints` request builds its own DuckDB with its own budget and its own
GDAL cache. That is a ceiling on ONE request, not on the process -- and the
endpoint is sync, so it holds a threadpool slot for the 8-15 seconds the remote
reads take while the next request starts its own everything.
"""

from __future__ import annotations

import threading
import time

import pytest

from casebroker import footprints


def test_only_one_preview_runs_at_a_time():
    assert footprints.GEO_CONCURRENCY >= 1
    inside, peak, lock = [0], [0], threading.Lock()

    def work():
        with footprints.exclusive():
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.05)
            with lock:
                inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert peak[0] <= footprints.GEO_CONCURRENCY, (
        f"{peak[0]} previews ran at once against a limit of "
        f"{footprints.GEO_CONCURRENCY}; each one carries its own DuckDB budget")


def test_the_slot_is_released_even_when_the_body_raises():
    with pytest.raises(ValueError):
        with footprints.exclusive():
            raise ValueError("boom")
    # Still acquirable, i.e. not leaked.
    with footprints.exclusive():
        pass


def test_a_full_queue_refuses_rather_than_hanging(monkeypatch):
    """Backpressure, not a hang. A viewer waiting forever is its own outage."""
    monkeypatch.setattr(footprints, "GEO_QUEUE_SECONDS", 0.05)
    held = threading.Event()
    release = threading.Event()

    def hog():
        with footprints.exclusive():
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(footprints.GeoBusy):
            with footprints.exclusive():
                pass
    finally:
        release.set()
        t.join(timeout=5)


def test_the_endpoint_answers_503_not_500_when_busy(tmp_path, monkeypatch):
    """A queued-out preview is the service protecting itself, and says so."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app

    monkeypatch.setattr(footprints, "GEO_QUEUE_SECONDS", 0.05)
    app = create_app(db_path=str(tmp_path / "b.sqlite"), tokens=["s"])
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer s"})
    c.post("/v1/cases", json=[{"lat": 33.75, "lon": -84.39, "recipe": "r",
                               "city_cluster": "x", "lcz": "LCZ1",
                               "spec": {"dirs": [0]}}])
    cid = c.get("/v1/cases?limit=1").json()["cases"][0]["case_id"]

    held, release = threading.Event(), threading.Event()

    def hog():
        with footprints.exclusive():
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert held.wait(timeout=5)
    try:
        r = c.get(f"/v1/cases/{cid}/footprints")
        assert r.status_code == 503, r.status_code
        assert r.headers.get("Retry-After") == "30"
    finally:
        release.set()
        t.join(timeout=5)

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


def test_the_request_threadpool_is_bounded():
    """anyio defaults to 40 sync handlers at once.

    That is sized for a machine, not for a 512 MB instance with a ~280 MB
    resident floor -- and it buys nothing here, because db._LOCK serialises the
    database work those handlers do. What it buys is 40 request bodies in flight
    and a queue that grows until the platform intervenes.

    The limiter is per EVENT LOOP, so this runs the app's own startup helper
    inside one loop and reads it back there. Reading it from a different loop
    would see a fresh limiter and pass or fail for reasons unrelated to the code.
    """
    import anyio
    import anyio.to_thread

    from casebroker import app as appmod

    assert 1 <= appmod.REQUEST_CONCURRENCY <= 40

    async def scenario():
        before = anyio.to_thread.current_default_thread_limiter().total_tokens
        appmod._apply_thread_limit()
        return before, anyio.to_thread.current_default_thread_limiter().total_tokens

    before, after = anyio.run(scenario)
    assert before == 40, "anyio's default moved; the rationale needs rechecking"
    assert after == appmod.REQUEST_CONCURRENCY


def test_the_app_wires_the_limit_into_its_lifespan():
    """A helper nothing calls is not a bound."""
    import inspect

    from casebroker import app as appmod

    src = inspect.getsource(appmod.create_app)
    assert "_apply_thread_limit()" in src, "create_app never applies the limit"
    assert "lifespan=" in src, "not wired through the lifespan"

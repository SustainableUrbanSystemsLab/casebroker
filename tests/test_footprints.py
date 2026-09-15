"""The case inspector: the footprints endpoint, and the dashboard panel that draws it.

No network. The building and terrain reads are replaced, because what these pin
is the broker's own behaviour around them -- which reads run together, how many
run for one case -- and what the page claims about the picture it draws.
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture()
def broker(tmp_path):
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    application = create_app(db_path=str(tmp_path / "footprints.sqlite"),
                             tokens=["secret-a"])
    c = TestClient(application)
    c.headers.update({"Authorization": "Bearer secret-a"})
    return c


def _one_case(client) -> str:
    assert client.post("/v1/cases", json=[{
        "lat": 32.0603, "lon": 118.7969, "recipe": "fixed-cyl-500/of12",
        "city_cluster": "nanjing", "lcz": "LCZ4"}]).json()["added"] == 1
    return client.get("/v1/cases").json()["cases"][0]["case_id"]


def _gba(lat, lon):
    return {"type": "FeatureCollection", "release": "GBA.LoD1",
            "source": "globalbuildingatlas", "height_kind": "predicted",
            "centre": [lat, lon], "half_m": 520.0, "n": 0, "features": []}


def _no_overture(lat, lon):
    # Reached only if the fake GBA read raised; failing here turns that into a
    # 502 the test reports, instead of a real Overture download.
    raise AssertionError("fell back to Overture")


def test_concurrent_opens_of_one_case_share_one_building_query(broker, monkeypatch):
    """The dashboard rebuilds its geometry panel on every refresh, and a second
    viewer can open the same case. Each used to start its own read of the same
    remote bytes while the first was still under way, seconds apiece. The later
    request has to wait for the first, then answer from the cache it wrote."""
    from casebroker import footprints
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_gba(lat, lon):
        calls.append((lat, lon))
        started.set()
        release.wait(10)
        return _gba(lat, lon)

    monkeypatch.setattr(footprints, "fetch_gba", slow_gba)
    monkeypatch.setattr(footprints, "terrain", lambda lat, lon: {"source": "flat"})
    monkeypatch.setattr(footprints, "fetch", _no_overture)
    url = f"/v1/cases/{_one_case(broker)}/footprints"

    got = {}
    first = threading.Thread(target=lambda: got.__setitem__("first", broker.get(url)))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=lambda: got.__setitem__("second", broker.get(url)))
    second.start()
    # Time for the second request to reach the query if nothing holds it back.
    # A correct broker passes however long this is, so it cannot fail spuriously.
    time.sleep(0.5)
    release.set()
    first.join(10)
    second.join(10)

    assert len(calls) == 1
    assert got["first"].status_code == 200 and got["second"].status_code == 200
    assert got["first"].json()["cached"] is False
    assert got["second"].json()["cached"] is True


def test_terrain_is_read_alongside_the_buildings_not_after_them(broker, monkeypatch):
    """In sequence, a first look cost the SUM of two remote reads that each swing
    by seconds from site to site. This GBA read will not finish until the
    terrain read has started, which only a broker running both at once allows."""
    from casebroker import footprints
    terrain_started = threading.Event()

    def gba_waiting_on_terrain(lat, lon):
        if not terrain_started.wait(5):
            raise RuntimeError("terrain was not being read while GBA was")
        return _gba(lat, lon)

    def terrain(lat, lon):
        terrain_started.set()
        return {"source": "gedtm30", "relief_m": 12.0, "min_m": 1.0, "max_m": 13.0}

    monkeypatch.setattr(footprints, "fetch_gba", gba_waiting_on_terrain)
    monkeypatch.setattr(footprints, "terrain", terrain)
    monkeypatch.setattr(footprints, "fetch", _no_overture)

    r = broker.get(f"/v1/cases/{_one_case(broker)}/footprints")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "globalbuildingatlas"
    assert r.json()["terrain"]["source"] == "gedtm30"


def test_dashboard_geometry_panel_says_what_it_draws(broker):
    """After the broker switched to GBA the panel still announced "Overture, same
    release the runner meshes", counted "querying Overture…", and called GBA's
    predicted heights measured. And its counter wrote through one page-wide id,
    so two loads alternating in the same span made the time jump about."""
    html = broker.get("/", headers={"Authorization": ""}).text
    assert "same release the runner meshes" not in html
    assert "querying Overture" not in html
    assert "mesh_source" in html, "the panel must name the mesh's source, not assume one"
    assert "predicted height" in html
    assert 'id="geoTick"' not in html


# -- which source a case is drawn from ----------------------------------------

RECIPE = "fixed-cyl-500/of12"
PRE_SWITCH_ROW = {"type": "FeatureCollection", "release": "2026-08-19.0",
                  "centre": [32.0603, 118.7969], "half_m": 520.0, "n": 0, "features": []}


def _case(client, lat, lon) -> str:
    from casebroker import ids
    assert client.post("/v1/cases", json=[{
        "lat": lat, "lon": lon, "recipe": RECIPE, "city_cluster": "c",
        "lcz": "LCZ4"}]).json()["added"] == 1
    return ids.case_id(lat, lon, RECIPE)


def _finish(client, metrics) -> str:
    """Lease the one pending case and complete it with these metrics, as a worker
    relaying its runner's result line does."""
    lease = client.post("/v1/lease", json={"worker_id": "w1"}).json()[0]
    r = client.post("/v1/complete", json={
        "lease_id": lease["lease_id"], "result_uri": f"file:///done/{lease['case_id']}",
        "metrics": metrics})
    assert r.status_code == 200, r.text
    return lease["case_id"]


def _sources(monkeypatch, fail=()):
    """Both building reads replaced. Returns the list each call is recorded in."""
    from casebroker import footprints
    calls = []

    def gba(lat, lon):
        calls.append("gba")
        if "gba" in fail:
            raise RuntimeError("source.coop unreachable")
        return _gba(lat, lon)

    def overture(lat, lon):
        calls.append("overture")
        if "overture" in fail:
            raise RuntimeError("overture download failed")
        # What footprints.fetch returns: no `source` of its own.
        return {**PRE_SWITCH_ROW, "centre": [lat, lon]}

    monkeypatch.setattr(footprints, "fetch_gba", gba)
    monkeypatch.setattr(footprints, "fetch", overture)
    monkeypatch.setattr(footprints, "terrain", lambda lat, lon: {"source": "flat"})
    return calls


def _seen(body):
    # .get, so a response that says nothing fails as a wrong answer, not a KeyError.
    return body.get("source"), body.get("mesh_source"), body.get("mesh_source_basis")


def test_a_finished_case_is_drawn_from_the_source_its_run_reported(broker, monkeypatch):
    """The failure this exists for: a case meshed from Overture was drawn from GBA,
    because the endpoint tried GBA first whatever the mesh had been built from --
    a picture of different buildings than the mesh holds, which looks like a check."""
    calls = _sources(monkeypatch)
    _case(broker, 32.0603, 118.7969)
    overture = _finish(broker, {"stage": "archived", "height_source": "overture"})
    _case(broker, 33.7490, -84.3880)
    gba = _finish(broker, {"stage": "archived", "height_source": "gba-lod1"})

    body = broker.get(f"/v1/cases/{overture}/footprints").json()
    assert calls == ["overture"], "a case meshed from Overture must be read from Overture"
    assert _seen(body) == ("overture", "overture", "reported")
    body = broker.get(f"/v1/cases/{gba}/footprints").json()
    assert calls == ["overture", "gba"], "each read from its own source, and only that"
    assert _seen(body) == ("globalbuildingatlas", "globalbuildingatlas", "reported")


def test_a_case_not_finished_is_drawn_from_gba(broker, monkeypatch):
    calls = _sources(monkeypatch)
    case = _case(broker, 32.0603, 118.7969)
    assert _seen(broker.get(f"/v1/cases/{case}/footprints").json()) == \
        ("globalbuildingatlas", "globalbuildingatlas", "not_done")
    assert calls == ["gba"]


def test_a_finished_case_that_never_reported_is_dated_against_the_switch(broker, monkeypatch):
    """Every case already done will never report a source. One that finished
    before the builder could mesh GBA was meshed from Overture -- certain, in that
    direction only. One that finished after is drawn from GBA and labelled an
    assumption: an old checkout, or cached geometry, meshes Overture after the
    switch too."""
    from casebroker import db, footprints
    calls = _sources(monkeypatch)
    now = db._now
    monkeypatch.setattr(db, "_now", lambda: footprints.GBA_BUILDER_SINCE - 3600)
    _case(broker, 32.0603, 118.7969)
    before = _finish(broker, {"stage": "archived"})
    monkeypatch.setattr(db, "_now", now)
    _case(broker, 33.7490, -84.3880)
    after = _finish(broker, {"stage": "archived"})

    assert _seen(broker.get(f"/v1/cases/{before}/footprints").json()) == \
        ("overture", "overture", "before_gba")
    assert _seen(broker.get(f"/v1/cases/{after}/footprints").json()) == \
        ("globalbuildingatlas", "globalbuildingatlas", "unreported")
    assert calls == ["overture", "gba"]


def test_a_source_the_broker_cannot_draw_is_not_mistaken_for_one_it_can():
    from casebroker import footprints
    # Reported outranks the date, even for a case that finished long before.
    assert footprints.mesh_source("done", {"height_source": "osm"}, 0) == \
        ("globalbuildingatlas", "unrecognized")


def test_a_cached_picture_from_the_wrong_source_is_queried_again(broker, monkeypatch):
    """A row cached before the switch carries no `source`, and is Overture. It was
    served for good -- including for cases still pending, which GBA will mesh."""
    import json
    from casebroker import db
    calls = _sources(monkeypatch)
    case = _case(broker, 32.0603, 118.7969)
    db.put_footprints(db.connect(broker.app.state.db_path), case,
                      json.dumps(PRE_SWITCH_ROW), 0)

    body = broker.get(f"/v1/cases/{case}/footprints").json()
    assert (body.get("source"), body["cached"]) == ("globalbuildingatlas", False)
    body = broker.get(f"/v1/cases/{case}/footprints").json()
    assert (body.get("source"), body["cached"]) == ("globalbuildingatlas", True), \
        "the right picture, once fetched, is what the cache keeps"
    assert calls == ["gba"]


def test_a_pre_switch_row_still_answers_for_a_case_meshed_from_overture(broker, monkeypatch):
    import json
    from casebroker import db
    calls = _sources(monkeypatch)
    _case(broker, 32.0603, 118.7969)
    case = _finish(broker, {"stage": "archived", "height_source": "overture"})
    db.put_footprints(db.connect(broker.app.state.db_path), case,
                      json.dumps(PRE_SWITCH_ROW), 0)

    body = broker.get(f"/v1/cases/{case}/footprints").json()
    assert calls == [] and body["cached"] is True
    assert _seen(body) == ("overture", "overture", "reported"), \
        "`source` is stated, not left for the reader to infer from its absence"


def test_when_the_mesh_source_cannot_be_read_the_other_answers_and_says_so(broker, monkeypatch):
    """An inspector that 502s is useless exactly when someone is trying to find out
    why a case looks wrong. But a fallback picture is not the mesh's: the response
    must say so, and the cache must not keep serving it."""
    calls = _sources(monkeypatch, fail=("overture",))
    _case(broker, 32.0603, 118.7969)
    case = _finish(broker, {"stage": "archived", "height_source": "overture"})

    r = broker.get(f"/v1/cases/{case}/footprints")
    assert r.status_code == 200, r.text
    body = r.json()
    assert _seen(body) == ("globalbuildingatlas", "overture", "reported")
    assert body["fallback_from"].startswith("overture unavailable: overture download failed")
    assert calls == ["overture", "gba"]

    broker.get(f"/v1/cases/{case}/footprints")
    assert calls == ["overture", "gba", "overture", "gba"], "a fallback is retried, not cached"

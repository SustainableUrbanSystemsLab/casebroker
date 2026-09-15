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
    assert "GlobalBuildingAtlas, the source the runner meshes" in html
    assert "predicted height" in html
    assert 'id="geoTick"' not in html

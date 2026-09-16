"""What the site preview does when a source has nothing to say.

Every test here is network-free: the three fetchers are monkeypatched, because
what is being pinned is the endpoint's JUDGEMENT about their answers, not the
answers. That judgement is where the bugs were.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from casebroker import footprints
from casebroker.app import create_app


@pytest.fixture()
def broker(tmp_path):
    app = create_app(db_path=str(tmp_path / "fp.sqlite"), tokens=["secret-a"])
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer secret-a"})
    return c


_seq = iter(range(1, 10_000))


def _one_case(client, lat=33.75, lon=-84.39):
    """Add one case on land and return its id.

    Tagged with a unique cluster and looked up by it rather than taking the
    first row of the list: the list is ordered by last-touched, two cases added
    in the same second tie, and a test that silently asserted against the wrong
    one would pass for the wrong reason.

    The latitude is nudged per call because case_id is derived from the
    coordinates -- two calls at the same point are the SAME case, and the second
    add is an idempotent skip rather than a new row. It stays inside the Atlanta
    tile, so every case here passes the admission gate, which is required: an
    ocean case can no longer be added through the API at all.
    """
    n = next(_seq)
    tag = f"c{n}"
    lat = round(lat + n * 0.013, 6)
    client.post("/v1/cases", json=[{"lat": lat, "lon": lon,
                                    "recipe": "fixed-cyl-500/of12",
                                    "city_cluster": tag, "lcz": "LCZ1",
                                    "spec": {"dirs": [0]}}])
    cases = client.get(f"/v1/cases?city_cluster={tag}&limit=1").json()["cases"]
    assert len(cases) == 1
    return cases[0]["case_id"]


def _land(monkeypatch):
    monkeypatch.setattr(footprints, "terrain",
                        lambda *a, **k: {"source": "gedtm30", "relief_m": 12.0,
                                         "min_m": 1.0, "max_m": 13.0, "n": 2,
                                         "half_m": 1304.0, "grid": [0, 1, 2, 3]})
    monkeypatch.setattr(footprints, "canopy",
                        lambda *a, **k: {"source": "meta-wri-chm-v1", "n": 2,
                                         "half_m": 1304.0, "frac_canopy": 0.25,
                                         "max_height_m": 18.0, "grid": [0, 0, 9, 0]})


def _ocean(monkeypatch):
    monkeypatch.setattr(footprints, "terrain",
                        lambda *a, **k: {"source": "flat", "half_m": 1304.0,
                                         "detail": "GEDTM30 has no data at this site"})
    monkeypatch.setattr(footprints, "canopy",
                        lambda *a, **k: {"source": "none", "half_m": 1304.0,
                                         "detail": "the canopy model publishes no tile here"})


def test_a_site_with_no_published_tile_is_an_answer_not_a_502(broker, monkeypatch):
    """The bug this file exists for.

    GBA publishes 922 tiles of a possible 2,592; the rest are ocean and ice. A
    404 on the tile URL used to propagate as `502 building query failed`, which
    took the terrain and the canopy down with it -- so the panel showed a
    transport error for a case whose real problem is that it is in the Atlantic.
    """
    def gap(lat, lon, **k):
        raise footprints.TileNotPublished("w040_n10_w035_n05")
    monkeypatch.setattr(footprints, "fetch_gba", gap)
    _ocean(monkeypatch)
    # Seeded on land and made to answer "no tile" by the patch above, because
    # the admission gate now refuses to add an ocean case at all. The preview
    # still has to handle one: the 5,000 cases drawn before the gate existed are
    # in production, and a coastal point inside a published 5-degree tile can
    # fail the exact question while passing the coarse one.

    r = broker.get(f"/v1/cases/{_one_case(broker)}/footprints")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 0 and body["tile_published"] is False
    assert body["tile"] == "w040_n10_w035_n05"
    # The other two layers survived, and together they say "not on land".
    assert body["terrain"]["source"] == "flat"
    assert body["canopy"]["source"] == "none"


def test_a_missing_tile_is_cached_but_an_outage_is_not(broker, monkeypatch):
    """An empty tile and an unreachable mirror mean opposite things.

    One is a fact about the site and costs nothing to keep; the other is a fact
    about today, and caching it would freeze a transport blip into a permanent
    empty city.
    """
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: (_ for _ in ()).throw(
                            footprints.TileNotPublished("w040_n10_w035_n05")))
    _ocean(monkeypatch)
    cid = _one_case(broker)
    assert broker.get(f"/v1/cases/{cid}/footprints").json()["cached"] is False
    assert broker.get(f"/v1/cases/{cid}/footprints").json()["cached"] is True

    # Now an outage, on a different case.
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: (_ for _ in ()).throw(
                            RuntimeError("connection reset")))
    _land(monkeypatch)
    cid2 = _one_case(broker, lat=33.75, lon=-84.39)
    first = broker.get(f"/v1/cases/{cid2}/footprints").json()
    assert "connection reset" in first["buildings_error"]
    # Terrain and trees are real and were fetched independently of the failure.
    assert first["terrain"]["source"] == "gedtm30"
    assert first["canopy"]["frac_canopy"] == 0.25
    # Not cached: reopening retries rather than serving the failure forever.
    assert broker.get(f"/v1/cases/{cid2}/footprints").json()["cached"] is False


def test_a_payload_from_an_older_build_is_a_miss(broker, monkeypatch):
    """Why nobody saw any trees.

    The cache is keyed on case_id alone and has no schema column, so a payload
    written before terrain and canopy existed was served forever -- indefinitely
    answering "no trees" for every case anyone had already opened, which is
    indistinguishable from a treeless world. Here the stale row is written
    directly, exactly as the previous build left it.
    """
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: {"type": "FeatureCollection",
                                               "features": [], "n": 0,
                                               "source": "globalbuildingatlas",
                                               "release": "GBA.LoD1",
                                               "centre": [lat, lon]})
    _land(monkeypatch)
    cid = _one_case(broker, lat=33.75, lon=-84.39)
    fresh = broker.get(f"/v1/cases/{cid}/footprints").json()
    assert fresh["payload_v"] == footprints.PAYLOAD_VERSION
    assert "canopy" in fresh and "terrain" in fresh

    stale = {k: v for k, v in fresh.items()
             if k not in ("payload_v", "terrain", "canopy", "cached", "fetched_at")}
    import sqlite3
    with sqlite3.connect(broker.app.state.db_path) as raw:
        raw.execute("UPDATE footprints SET geojson=? WHERE case_id=?",
                    (json.dumps(stale), cid))

    served = broker.get(f"/v1/cases/{cid}/footprints").json()
    assert served["cached"] is False, "a pre-rasters payload was served as a hit"
    assert served["canopy"]["frac_canopy"] == 0.25
    assert served["terrain"]["source"] == "gedtm30"


def test_the_version_check_rejects_a_foreign_stamp(broker, monkeypatch, tmp_path):
    """The stamp has to be compared, not merely written."""
    calls = {"n": 0}

    def counted(lat, lon, **k):
        calls["n"] += 1
        return {"type": "FeatureCollection", "features": [], "n": 0,
                "source": "globalbuildingatlas", "release": "GBA.LoD1",
                "centre": [lat, lon]}

    monkeypatch.setattr(footprints, "fetch_gba", counted)
    _land(monkeypatch)
    cid = _one_case(broker, lat=33.75, lon=-84.39)
    broker.get(f"/v1/cases/{cid}/footprints")
    assert calls["n"] == 1
    broker.get(f"/v1/cases/{cid}/footprints")
    assert calls["n"] == 1                       # served from cache

    monkeypatch.setattr(footprints, "PAYLOAD_VERSION",
                        footprints.PAYLOAD_VERSION + 1)
    broker.get(f"/v1/cases/{cid}/footprints")
    assert calls["n"] == 2, "a payload stamped for another build was reused"


def test_json_payload_stays_serialisable(broker, monkeypatch):
    """Whatever the branch, what goes into the cache must round-trip."""
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: (_ for _ in ()).throw(
                            footprints.TileNotPublished("w040_n10_w035_n05")))
    _ocean(monkeypatch)
    body = broker.get(f"/v1/cases/{_one_case(broker)}/footprints").json()
    assert json.loads(json.dumps(body)) == body
    assert body["terrain"]["source"] and body["canopy"]["source"]


def test_a_raster_host_outage_is_not_frozen_into_the_cache(broker, monkeypatch):
    """Why the trees never came back.

    `transient` was set only when the BUILDINGS fetch failed. terrain() and
    canopy() never raise -- they return {"source": "unavailable"} -- so a canopy
    read that timed out once was cached at the current payload version and
    served as a treeless site for the life of the case. "unavailable" and
    "unknown" are facts about today; only "flat" and "none" are facts about the
    site, and only those may be cached.
    """
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: {"type": "FeatureCollection",
                                               "features": [], "n": 0,
                                               "source": "globalbuildingatlas",
                                               "release": "GBA.LoD1",
                                               "centre": [lat, lon]})
    calls = {"n": 0}

    def flaky_canopy(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"source": "unavailable", "detail": "curl: timeout"}
        return {"source": "meta-wri-chm-v1", "n": 2, "half_m": 1304.0,
                "frac_canopy": 0.3, "max_height_m": 20.0, "grid": [0, 9, 9, 0]}

    monkeypatch.setattr(footprints, "canopy", flaky_canopy)
    monkeypatch.setattr(footprints, "terrain",
                        lambda *a, **k: {"source": "gedtm30", "relief_m": 5.0,
                                         "min_m": 0.0, "max_m": 5.0, "n": 2,
                                         "half_m": 1304.0, "grid": [0, 1, 2, 3]})
    cid = _one_case(broker, lat=33.75, lon=-84.39)

    first = broker.get(f"/v1/cases/{cid}/footprints").json()
    assert first["canopy"]["source"] == "unavailable"
    assert first["cached"] is False

    # Reopening must RETRY, not serve the outage back.
    second = broker.get(f"/v1/cases/{cid}/footprints").json()
    assert calls["n"] == 2, "the outage was served from cache instead of retried"
    assert second["canopy"]["source"] == "meta-wri-chm-v1"
    assert second["canopy"]["frac_canopy"] == 0.3

    # And a real answer IS cached.
    third = broker.get(f"/v1/cases/{cid}/footprints").json()
    assert third["cached"] is True and calls["n"] == 2


def test_a_genuine_gap_is_still_cached(broker, monkeypatch):
    """'flat' and 'none' are facts about the site, and must not be re-fetched."""
    monkeypatch.setattr(footprints, "fetch_gba",
                        lambda lat, lon, **k: {"type": "FeatureCollection",
                                               "features": [], "n": 0,
                                               "source": "globalbuildingatlas",
                                               "release": "GBA.LoD1",
                                               "centre": [lat, lon]})
    calls = {"n": 0}

    def gap(*a, **k):
        calls["n"] += 1
        return {"source": "none", "half_m": 1304.0,
                "detail": "the canopy model publishes no tile here"}

    monkeypatch.setattr(footprints, "canopy", gap)
    monkeypatch.setattr(footprints, "terrain",
                        lambda *a, **k: {"source": "flat", "half_m": 1304.0,
                                         "detail": "GEDTM30 has no data at this site"})
    cid = _one_case(broker, lat=33.75, lon=-84.39)
    broker.get(f"/v1/cases/{cid}/footprints")
    assert broker.get(f"/v1/cases/{cid}/footprints").json()["cached"] is True
    assert calls["n"] == 1

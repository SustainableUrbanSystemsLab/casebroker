"""Cases that are not on land, refused at the door.

The draw has put sites in Antarctica and in the open Atlantic. The sampler's
LCZ raster reads snow, ice and open water as built classes and its purity test
cannot catch that -- a uniformly misread ice sheet is 100% "pure" -- and its
polar gate is a latitude limit, which by construction says nothing about
7.5N 37.5W.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app
from casebroker.footprints import gba_tile_for, on_land


@pytest.fixture()
def broker(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "g.sqlite"), tokens=["s"]))
    c.headers.update({"Authorization": "Bearer s"})
    return c


def _case(lat, lon, tag="x"):
    return {"lat": lat, "lon": lon, "recipe": "fixed-cyl-500/of12",
            "city_cluster": tag, "lcz": "LCZ1", "spec": {"dirs": [0]}}


# -- the mask itself ---------------------------------------------------------

def test_open_water_and_ice_are_not_land():
    assert not on_land(7.5, -37.5)         # mid-Atlantic: the case that 502'd
    assert not on_land(0.0, -160.0)        # mid-Pacific
    assert not on_land(-77.85, 166.67)     # Ross Island, Antarctica
    assert not on_land(1.0, 2.0)           # Gulf of Guinea


def test_real_cities_survive_including_the_arctic_ones():
    """The gate must not cost the campaign a class it is short of.

    The sampler keeps the Arctic deliberately -- Norilsk, Murmansk, Tromso and
    Utqiagvik are genuine urban fabric -- so a land mask that quietly dropped
    them would be trading one bias for another.
    """
    for lat, lon in ((33.749, -84.388),    # Atlanta
                     (32.060, 118.797),    # Nanjing
                     (69.649, 18.956),     # Tromso
                     (69.333, 88.218),     # Norilsk
                     (71.290, -156.789),   # Utqiagvik
                     (64.147, -21.942),    # Reykjavik
                     (-33.868, 151.209)):  # Sydney
        assert on_land(lat, lon), (lat, lon)


def test_impossible_coordinates_are_not_land():
    assert not on_land(95.0, 0.0)
    assert not on_land(0.0, 200.0)
    assert not on_land(float("nan"), 0.0)


# -- the gate ----------------------------------------------------------------

def test_a_water_case_is_dropped_and_named(broker):
    r = broker.post("/v1/cases", json=[_case(7.5, -37.5, "atlantic")])
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert body["rejected_not_on_land"] == 1
    ex = body["rejected_examples"][0]
    assert ex["city_cluster"] == "atlantic"
    assert ex["tile"] == gba_tile_for(7.5, -37.5) == "w040_n10_w035_n05"
    assert broker.get("/v1/cases?limit=50").json()["cases"] == []


def test_the_rest_of_the_batch_still_lands(broker):
    """A 5,000-case draw with twenty bad sites should land the other 4,980.

    Refusing the whole batch would be the loudest signal and the wrong one: the
    sampler's loader raises on a rejected batch, so one ocean site would block a
    whole draw.
    """
    batch = [_case(33.75 + i * 0.01, -84.39, f"atl{i}") for i in range(4)]
    batch.insert(2, _case(7.5, -37.5, "atlantic"))
    batch.append(_case(-77.85, 166.67, "mcmurdo"))
    body = broker.post("/v1/cases", json=batch).json()
    assert body["added"] == 4
    assert body["rejected_not_on_land"] == 2
    assert len(broker.get("/v1/cases?limit=50").json()["cases"]) == 4


def test_a_clean_draw_says_so_rather_than_staying_silent(broker):
    """The key is always present, so a caller reads it without a version check."""
    body = broker.post("/v1/cases", json=[_case(33.75, -84.39)]).json()
    assert body["rejected_not_on_land"] == 0
    assert "rejected_examples" not in body


def test_the_examples_are_a_sample_not_the_batch(broker):
    body = broker.post("/v1/cases",
                       json=[_case(7.5, -37.5 + i * 0.01, f"o{i}") for i in range(30)]).json()
    assert body["rejected_not_on_land"] == 30
    assert len(body["rejected_examples"]) == 5


# -- the sweep over cases that predate the gate ------------------------------

def _seed_legacy(broker, points):
    """Insert cases straight into the database, bypassing the admission gate.

    Which is the situation being tested: these rows are what a draw from before
    the gate existed left behind, and they cannot be created through the API any
    more.
    """
    import json
    import sqlite3
    from casebroker import ids
    now = 1_700_000_000
    with sqlite3.connect(broker.app.state.db_path) as raw:
        for lat, lon, tag in points:
            cid = ids.case_id(lat, lon, "r")
            raw.execute(
                "INSERT OR IGNORE INTO cases (case_id, spec, recipe, city_cluster,"
                " lcz, split, priority, max_attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, json.dumps({"lat": lat, "lon": lon}), "r", tag, "LCZ1",
                 "train", 100, 3, now, now))
    return raw


def test_the_audit_reports_before_it_touches_anything(broker):
    _seed_legacy(broker, [(7.5, -37.5, "atlantic"), (-77.85, 166.67, "mcmurdo"),
                          (33.75, -84.39, "atlanta")])
    body = broker.post("/v1/cases/land-audit").json()
    assert body["dry_run"] is True
    assert body["scanned_not_on_land"] == 2
    assert body["quarantined"] == 0
    assert {e["city_cluster"] for e in body["examples"]} == {"atlantic", "mcmurdo"}
    # Nothing moved.
    states = {c["case_id"]: c["state"]
              for c in broker.get("/v1/cases?limit=50").json()["cases"]}
    assert set(states.values()) == {"pending"}


def test_the_audit_quarantines_only_the_water_cases(broker):
    _seed_legacy(broker, [(7.5, -37.5, "atlantic"), (-77.85, 166.67, "mcmurdo"),
                          (33.75, -84.39, "atlanta")])
    body = broker.post("/v1/cases/land-audit?dry_run=false").json()
    assert body["quarantined"] == 2

    by_tag = {c["city_cluster"]: c["state"]
              for c in broker.get("/v1/cases?limit=50").json()["cases"]}
    assert by_tag["atlantic"] == "quarantined"
    assert by_tag["mcmurdo"] == "quarantined"
    assert by_tag["atlanta"] == "pending", "a land case must not be touched"


def test_quarantined_cases_are_not_leased(broker):
    """The point of the sweep: no worker spends 66 core-hours on the ocean."""
    _seed_legacy(broker, [(7.5, -37.5, "atlantic")])
    assert len(broker.post("/v1/lease", json={"worker_id": "w", "count": 5}).json()) == 1
    broker.post("/v1/cases/land-audit?dry_run=false")
    # Released from its lease and parked, so a fresh worker gets nothing.
    assert broker.post("/v1/lease", json={"worker_id": "w2", "count": 5}).json() == []


def test_the_audit_is_idempotent(broker):
    _seed_legacy(broker, [(7.5, -37.5, "atlantic")])
    assert broker.post("/v1/cases/land-audit?dry_run=false").json()["quarantined"] == 1
    again = broker.post("/v1/cases/land-audit?dry_run=false").json()
    assert again["scanned_not_on_land"] == 0 and again["quarantined"] == 0


def test_a_finished_case_is_left_alone(broker):
    """A done case already cost its core-hours; re-labelling it rewrites history."""
    _seed_legacy(broker, [(7.5, -37.5, "atlantic")])
    got = broker.post("/v1/lease", json={"worker_id": "w", "count": 1}).json()[0]
    broker.post("/v1/complete", json={"case_id": got["case_id"],
                                      "lease_id": got["lease_id"],
                                      "result_uri": "s3://x", "metrics": {}})
    body = broker.post("/v1/cases/land-audit?dry_run=false").json()
    assert body["scanned_not_on_land"] == 0
    assert broker.get("/v1/cases?limit=5").json()["cases"][0]["state"] == "done"

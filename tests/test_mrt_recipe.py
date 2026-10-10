"""A case built on another case (docs/mrt.md).

An MRT case reads the site's finished surface-temperature archive, so it must
not be handed out before that case is done. The gate is a column of its own
(`needs_case`), checked by the lease query; the API refuses a need the broker
has no case for; the single-case record says where the needed case stands; and
the stage grammar knows the recipe's one phase of its own, the long-wave pass.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, stages  # noqa: E402
from casebroker.app import create_app  # noqa: E402

SURF, MRT, WIND = "surf-1008/rad6R0P2-fft-v2", "mrt-1008/rad6R0P2-solarcal-v1", "cyl-1008/of12-v6"


def row(i: int, recipe: str, needs: str | None = None) -> dict:
    return {"case_id": f"c{i:03d}", "spec": {"lat": 1.0, "lon": 2.0, "recipe": recipe},
            "recipe": recipe, "city_cluster": "city", "split": "train", "needs_case": needs}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "m.sqlite"))
    db.add_cases(c, [row(1, SURF), row(2, MRT, needs="c001")])
    return c


def test_a_case_that_needs_another_is_not_leased_until_that_one_is_done(conn):
    assert db.lease(conn, "rad", count=5, recipes=[MRT]) == []
    lease = db.lease(conn, "rad", count=1, recipes=[SURF])[0]
    assert lease.case_id == "c001"
    assert db.lease(conn, "rad", count=5, recipes=[MRT]) == [], "leased is not done"
    db.complete(conn, lease.lease_id, "file:///c001.tar.gz", "a" * 64, 10, {})
    got = db.lease(conn, "rad", count=5, recipes=[MRT])
    assert [c.case_id for c in got] == ["c002"]


def test_a_need_that_is_parked_or_missing_is_said_on_the_case(conn):
    assert db.get_case(conn, "c002")["needs_state"] == "pending"
    assert db.get_case(conn, "c001")["needs_case"] is None
    assert db.get_case(conn, "c001")["needs_state"] is None
    db.add_cases(conn, [row(3, MRT, needs="c999")])
    assert db.get_case(conn, "c003")["needs_state"] == "missing"
    assert db.lease(conn, "rad", count=5, recipes=[MRT]) == [], "a missing need is never met"


def test_the_api_takes_needs_and_refuses_a_case_it_does_not_have(tmp_path):
    app = create_app(str(tmp_path / "a.sqlite"), ["w"], ["r"])
    with TestClient(app) as client:
        h = {"Authorization": "Bearer w"}
        surf = {"lat": 33.7, "lon": -84.4, "recipe": SURF, "city_cluster": "atlanta", "spec": {}}
        out = client.post("/v1/cases", json=[surf], headers=h).json()
        assert out["added"] == 1
        surf_id = client.get("/v1/cases?recipe=" + SURF, headers=h).json()["cases"][0]["case_id"]
        mrt = {"lat": 33.7, "lon": -84.4, "recipe": MRT, "city_cluster": "atlanta",
               "needs": surf_id, "spec": {"surf_case": surf_id}}
        assert client.post("/v1/cases", json=[mrt], headers=h).json()["added"] == 1
        mrt_id = client.get("/v1/cases?recipe=" + MRT, headers=h).json()["cases"][0]["case_id"]
        record = client.get(f"/v1/cases/{mrt_id}", headers=h).json()
        assert record["needs_case"] == surf_id and record["needs_state"] == "pending"

        bad = dict(mrt, lat=34.0, needs="v2-0000000000000000")
        r = client.post("/v1/cases", json=[bad], headers=h)
        assert r.status_code == 422 and "no such case" in r.json()["detail"]
        # Refused before anything in the batch was written.
        assert client.get("/v1/cases?recipe=" + MRT, headers=h).json()["total"] == 1

        # A node declaring the recipe is handed nothing while the surface case is pending.
        lease = client.post("/v1/lease", json={"worker_id": "rad", "count": 5, "recipes": [MRT]}, headers=h)
        assert lease.status_code == 200 and lease.json() == []


def test_the_radiance_recipes_have_no_pedestrian_field():
    assert db.expected_fields(MRT, {"solve": {"directions_total": 8}}) is None
    assert db.expected_fields(SURF, {"solve": {"directions_total": 8}}) is None
    assert db.expected_fields(WIND, {"solve": {"directions_total": 8}}) == 8


@pytest.mark.parametrize("line, stage", [
    ("scene · 251,904 sensors · surface temperatures of v2-1", "scene"),
    ("trace 12/63 chunks · 3,904 daylight hours", "trace"),
    ("longwave 12/63 chunks · 400 rays", "longwave"),
    ("archiving", "archive"),
])
def test_the_mrt_phases_are_stages(line, stage):
    assert stages.stage_of(line) == stage
    assert "longwave" in stages.STAGES


def test_the_trace_is_what_an_mrt_eta_extrapolates():
    assert db._solve_fraction("trace 12/63 chunks · 3,904 daylight hours") == pytest.approx(12 / 63)
    assert db._solve_fraction("longwave 12/63 chunks · 400 rays") is None

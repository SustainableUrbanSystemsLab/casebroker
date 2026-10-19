"""Parking named PENDING cases: the inverse of reopen.

Field case, 2026-10-19: the sampler's scan mirrored every tile west of
Greenwich across the equator, and 280 pending cases sat at the wrong
latitude (New York's tiles in Patagonia). Nothing could take a pending case
out of the queue by id: cancel pulls only a LEASED case, land-audit only one
off land, and purge deletes the row and its trail.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app
from casebroker.ids import case_id


@pytest.fixture()
def broker(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "p.sqlite"), tokens=["s"]))
    c.headers.update({"Authorization": "Bearer s"})
    return c


R = "fixed-cyl-500/of12"
SITES = [(33.749, -84.388), (32.060, 118.797), (-33.868, 151.209), (35.68, 139.69)]


def _add(c):
    body = [{"lat": a, "lon": b, "recipe": R, "city_cluster": f"c{i}", "lcz": "LCZ1"}
            for i, (a, b) in enumerate(SITES)]
    assert c.post("/v1/cases", json=body).json()["added"] == len(SITES)
    return [case_id(a, b, R) for a, b in SITES]


def _state(c, cid):
    return c.get(f"/v1/cases/{cid}").json()


def _park(c, ids, **params):
    return c.post("/v1/cases/park", params=[("case_id", i) for i in ids] + list(params.items()))


def test_a_dry_run_changes_nothing(broker):
    ids = _add(broker)
    got = _park(broker, ids[:2], reason="misplaced").json()
    assert got["matched"] == 2 and got["parked"] == 0 and got["dry_run"] is True
    assert {_state(broker, i)["state"] for i in ids} == {"pending"}


def test_only_pending_cases_are_parked_and_reopen_brings_them_back(broker):
    ids = _add(broker)
    leased = broker.post("/v1/lease", json={"worker_id": "w"}).json()[0]["case_id"]
    pending = [i for i in ids if i != leased]

    got = _park(broker, ids, reason="sampler-v3 west mirror", dry_run="false").json()

    assert got["parked"] == 3 and got["skipped"] == {"leased": 1}
    assert _state(broker, leased)["state"] == "leased", "a live solve is cancel's job, never this one's"
    for i in pending:
        case = _state(broker, i)
        assert case["state"] == "quarantined"
        assert "sampler-v3 west mirror" in case["last_error"]
    back = broker.post("/v1/cases/reopen", params=[("error_contains", "west mirror"), ("dry_run", "false")]).json()
    assert back["reopened"] == 3


def test_limit_bounds_the_writes_and_says_so(broker):
    ids = _add(broker)
    got = _park(broker, ids, reason="r", dry_run="false", limit=2).json()
    assert got["matched"] == 4 and got["parked"] == 2 and got["capped"] is True


def test_a_reason_is_required(broker):
    ids = _add(broker)
    assert _park(broker, ids[:1]).status_code == 422
    assert _park(broker, [], reason="r").status_code == 422

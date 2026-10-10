"""Two kinds of recipe in one campaign, seen one at a time.

Until 2026-09 every recipe was a CFD wind recipe, and the broker pooled them:
one set of counts, one ETA, one dataset. The Radiance surface-temperature
recipe (docs/thermal.md) is a different training set with a different
throughput: a case takes an hour where a CFD case takes days. Pooled, its ETA
describes neither campaign and its run times rank every thermal case at the
bottom of the distribution. So the counts, the ETA, the case browser and the
dataset can each be scoped to one recipe, and the stages and the progress
grammar know the thermal phases.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import dataset, db, stages  # noqa: E402
from casebroker.app import create_app  # noqa: E402

WIND, THERMAL = "cyl-1008/of12-v5", "surf-1008/rad6R0P2-fft-v2"
T0 = 1_000_000


def row(i: int, recipe: str) -> dict:
    return {"case_id": f"c{i:03d}", "spec": {"lat": 1.0, "lon": 2.0, "recipe": recipe},
            "recipe": recipe, "city_cluster": f"city{i % 3}", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "v.sqlite"))
    db.add_cases(c, [row(1, WIND), row(2, WIND), row(3, WIND), row(4, THERMAL), row(5, THERMAL)])
    return c


def finish(conn, worker, recipe, now, seconds):
    got = db.lease(conn, worker, 1, 900, now=now, recipes=[recipe])[0]
    assert db.complete(conn, got.lease_id, "file:///r.tar.gz", metrics={"case_seconds": seconds}, now=now + 1)
    return got.case_id


# -- status --------------------------------------------------------------------

def test_status_scoped_to_a_recipe_counts_and_projects_that_recipe_alone(conn):
    for i in range(2):
        finish(conn, f"rad-{i}", THERMAL, T0 + i, 3600)

    everything = db.status(conn, now=T0 + 100)
    thermal = db.status(conn, now=T0 + 100, recipe=THERMAL)
    wind = db.status(conn, now=T0 + 100, recipe=WIND)

    assert everything["by_state"] == {"pending": 3, "done": 2}
    assert thermal["by_state"] == {"done": 2} and thermal["remaining"] == 0
    assert wind["by_state"] == {"pending": 3} and wind["done_last_24h"] == 0
    # Pooled, the thermal throughput projected an ETA for three wind cases that
    # nothing had touched.
    assert everything["eta_days"] == 1.5 and wind["eta_days"] is None
    # Every recipe is listed whatever the scope, for the dashboard's selector.
    assert thermal["by_recipe"] == {WIND: {"pending": 3}, THERMAL: {"done": 2}}
    assert thermal["recipe"] == THERMAL and everything["recipe"] is None


def test_status_says_which_recipes_each_worker_declares(conn):
    """The Recipes column: a worker's declared recipes ride on its status row, parsed,
    and a worker that declares none says None -- the dashboard then explains that one
    from the release policy, and strikes through the queued recipes the other lacks."""
    db.lease(conn, "foam-1", count=1, build="b1", recipes=[WIND])
    db.lease(conn, "rad-1", count=1, build="b1", recipes=[WIND, THERMAL])
    db.lease(conn, "old-1", count=1)
    workers = {w["worker_id"]: w for w in db.status(conn)["workers"]}
    assert workers["foam-1"]["recipes"] == [WIND]
    assert workers["rad-1"]["recipes"] == [WIND, THERMAL]
    assert workers["old-1"]["recipes"] is None
    # ...and the queue's per-recipe states are there, unscoped, for the line
    # above the table to count against.
    assert set(db.status(conn, recipe=WIND)["by_recipe"]) == {WIND, THERMAL}


def test_the_case_browser_filters_by_recipe(conn):
    page = db.list_cases(conn, recipe=THERMAL)
    assert page["total"] == 2 and {c["recipe"] for c in page["cases"]} == {THERMAL}
    assert db.list_cases(conn)["total"] == 5


def test_the_endpoints_take_the_recipe(tmp_path):
    app = create_app(str(tmp_path / "api.sqlite"), ["w"], ["r"])
    with TestClient(app) as c:
        conn = db.connect(str(tmp_path / "api.sqlite"))
        db.add_cases(conn, [row(1, WIND), row(2, THERMAL)])
        auth = {"Authorization": "Bearer r"}
        st = c.get("/v1/status", params={"recipe": THERMAL}, headers=auth).json()
        assert st["by_state"] == {"pending": 1} and set(st["by_recipe"]) == {WIND, THERMAL}
        page = c.get("/v1/cases", params={"recipe": WIND}, headers=auth).json()
        assert [x["case_id"] for x in page["cases"]] == ["c001"]
        app.state.dataset.invalidate()        # computed at start-up, before these cases
        ds = c.get("/v1/dataset", params={"recipe": THERMAL}, headers=auth).json()
        assert ds["cases"] == 1 and ds["counts"]["recipe"] == {THERMAL: 1}
        # A recipe no case carries is an empty campaign, not the whole one.
        assert c.get("/v1/dataset", params={"recipe": "nope"}, headers=auth).json()["cases"] == 0


# -- the dataset -------------------------------------------------------------------

def test_one_pass_answers_for_the_campaign_and_for_each_recipe():
    rows = [{"case_id": f"c{i}", "state": "done", "split": "train", "lcz": "LCZ2",
             "recipe": WIND if i < 4 else THERMAL, "spec": "{}", "telemetry": "{}",
             "metrics": json.dumps({"case_seconds": 300_000 + i if i < 4 else 3_600 + i})}
            for i in range(6)]

    agg = dataset.compute(rows, now=T0)

    assert agg.public["cases"] == 6
    assert agg.for_recipe(WIND).public["cases"] == 4
    assert agg.for_recipe(THERMAL).public["metrics"]["case_seconds"]["all"]["n"] == 2
    assert agg.for_recipe(None) is agg


def test_a_case_is_ranked_among_cases_of_its_own_recipe():
    """A one-hour Radiance case against week-long CFD solves sat at the bottom of
    every run-time distribution, which says nothing about either."""
    rows = [{"case_id": f"c{i}", "state": "done", "split": "train", "lcz": "LCZ2",
             "recipe": WIND if i < 4 else THERMAL, "spec": "{}", "telemetry": "{}",
             "metrics": json.dumps({"case_seconds": 300_000 + i if i < 4 else 3_600 + i})}
            for i in range(6)]
    agg = dataset.compute(rows, now=T0)
    slowest_thermal = dict(rows[5], recipe=THERMAL)

    pooled = agg.percentiles(slowest_thermal)["case_seconds"]["all"]
    own = agg.for_recipe(THERMAL).percentiles(slowest_thermal)["case_seconds"]["all"]

    assert pooled < 50 < own


# -- stages and progress ----------------------------------------------------------

@pytest.mark.parametrize("line, stage", [
    ("site geometry", "geometry"),
    ("scene · 312,440 sensors", "scene"),
    ("trace 12/36 chunks · 3,904 daylight hours", "trace"),
    ("surface 30/36 chunks", "surface"),
    ("archiving", "archive"),
])
def test_the_thermal_phases_are_stages(line, stage):
    assert stages.stage_of(line) == stage


def test_the_trace_is_what_a_thermal_eta_extrapolates():
    assert db._solve_fraction("trace 12/36 chunks · 3,904 daylight hours") == pytest.approx(12 / 36)
    # The thousands separator in the detail is not a pair.
    assert db._solve_fraction("trace 0/36 chunks") == 0.0
    # The admittance solve is short; it is not what an ETA extrapolates.
    assert db._solve_fraction("surface 30/36 chunks") is None
    assert db._solve_fraction("scene · 312,440 sensors") is None


def test_a_thermal_case_gets_an_eta_from_its_trace(conn):
    got = db.lease(conn, "rad-1", 1, 900, now=T0, recipes=[THERMAL])[0]
    db.heartbeat(conn, got.lease_id, 900, detail="trace 3/36 chunks", now=T0 + 60)
    db.heartbeat(conn, got.lease_id, 900, detail="trace 9/36 chunks", now=T0 + 60 + 1200)

    eta = db._solve_eta(conn, got.case_id, T0)

    # 6 chunks in 20 minutes: the other 27 take 90 more.
    assert eta is not None and eta["at"] == T0 + 60 + 1200 + 27 * 200

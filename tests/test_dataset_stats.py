"""GET /v1/dataset and a case's `percentiles`: the campaign as a dataset.

The numbers are checked against values worked out by hand from the rows the
test wrote, not against the implementation's own helpers, wherever a helper
would make the test agree with itself.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import dataset, db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

# Captured at import, before conftest's autouse fixture swaps it for an inline
# call: the threaded path is what production runs, and one test drives it.
IN_BACKGROUND = dataset._in_background

TOKEN, READ = "w-secret", "r-secret"

# On land, one per country, far enough apart that the nearest-town search is
# not what decides the country.
NYC, BERLIN, PARIS = (40.71, -74.00), (52.52, 13.40), (48.86, 2.35)


@pytest.fixture(autouse=True)
def _fresh_country_memo():
    """The country memo lives for the process; a test that stubs places.locate
    must not leave its answers behind for the next one."""
    dataset._COUNTRY.clear()
    yield
    dataset._COUNTRY.clear()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "ds.sqlite")
    app = create_app(db_path=path, tokens=[TOKEN], readonly_tokens=[READ])
    clock = _Clock()
    app.state.dataset.clock = clock
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c, db.connect(path), clock


def _seed(conn, rows):
    """Cases written straight into the table, with whatever telemetry, metrics
    and state each row names. `rows`: dicts with case_id plus optional lcz,
    state, split, recipe, where=(lat, lon), telemetry, metrics."""
    db.add_cases(conn, [{"case_id": r["case_id"], "recipe": r.get("recipe", "v2"),
                         "city_cluster": "c", "lcz": r.get("lcz"),
                         "split": r.get("split", "train"),
                         "spec": {"lat": r.get("where", NYC)[0], "lon": r.get("where", NYC)[1]}}
                        for r in rows])
    for r in rows:
        conn.execute("UPDATE cases SET state=?, telemetry=?, metrics=? WHERE case_id=?",
                     (r.get("state", "pending"),
                      json.dumps(r["telemetry"]) if "telemetry" in r else None,
                      json.dumps(r["metrics"]) if "metrics" in r else None, r["case_id"]))


def _site(**urban):
    return {"site": {"urban_form": urban, "at": 1, "worker": "w"}}


# -- auth and shape --------------------------------------------------------------------

def test_the_dataset_is_read_scope(env):
    c, _, _ = env
    assert c.get("/v1/dataset", headers={"Authorization": ""}).status_code == 401
    got = c.get("/v1/dataset", headers={"Authorization": f"Bearer {READ}"})
    assert got.status_code == 200
    body = got.json()
    assert set(body) == {"generated_at", "cases", "counts", "metrics"}
    assert set(body["counts"]) == {"state", "split", "lcz", "recipe", "country"}
    assert list(body["metrics"]) == [m.key for m in dataset.METRICS]


def test_an_empty_campaign_lists_every_metric_with_nothing_in_it(env):
    """A metric with no values yet is still listed -- n=0, empty histogram --
    so a client can lay out its page before the first case reports."""
    c, _, _ = env
    body = c.get("/v1/dataset").json()
    assert body["cases"] == 0
    for key, m in body["metrics"].items():
        assert m["all"]["n"] == 0 and m["all"]["hist"] == [] and m["bins"] == [], key
        assert m["all"]["median"] is None and m["by_lcz"] == {}
        assert m["group"] in {"urban", "site", "mesh", "run"} and m["label"]


def test_a_metric_nobody_has_reported_is_listed_empty_beside_one_that_has(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": f"c{i}", "lcz": "LCZ2", "telemetry": _site(bcr=0.1 * i)}
                 for i in range(1, 4)])
    m = c.get("/v1/dataset").json()["metrics"]
    assert m["bcr"]["all"]["n"] == 3
    assert m["solve_seconds"]["all"] == {"n": 0, "min": None, "p10": None, "p25": None,
                                         "median": None, "p75": None, "p90": None,
                                         "max": None, "mean": None, "hist": []}
    assert m["solve_seconds"]["bins"] == [] and m["solve_seconds"]["by_lcz"] == {}


# -- counts ----------------------------------------------------------------------------

def test_counts_by_state_split_lcz_recipe_and_country(env):
    c, conn, _ = env
    _seed(conn, [
        {"case_id": "a", "lcz": "LCZ1", "state": "done", "split": "train", "where": NYC},
        {"case_id": "b", "lcz": "LCZ1", "state": "pending", "split": "test", "where": NYC},
        {"case_id": "c", "lcz": "LCZ6", "state": "leased", "split": "train", "where": BERLIN,
         "recipe": "v3"},
        {"case_id": "d", "lcz": None, "state": "done", "split": "val", "where": PARIS},
    ])
    body = c.get("/v1/dataset").json()
    assert body["cases"] == 4
    counts = body["counts"]
    assert counts["state"] == {"done": 2, "leased": 1, "pending": 1}
    assert counts["split"] == {"train": 2, "test": 1, "val": 1}
    assert counts["lcz"] == {"LCZ1": 2, "LCZ6": 1, "unknown": 1}
    assert counts["recipe"] == {"v2": 3, "v3": 1}
    # places.locate, the same offline lookup the case inspector's Location row uses.
    assert counts["country"] == {"United States of America": 2, "France": 1, "Germany": 1}
    # Largest first, so a client can take the head without sorting.
    assert list(counts["state"].values()) == sorted(counts["state"].values(), reverse=True)


def test_countries_past_the_top_40_are_summed_into_other(env, monkeypatch):
    c, conn, _ = env
    # 45 countries, the i-th with i+1 cases: the five smallest go to "other".
    monkeypatch.setattr(dataset.places, "locate",
                        lambda lat, lon: {"country": f"Country{int(lat):02d}"})
    rows, n = [], 0
    for i in range(45):
        for j in range(i + 1):
            rows.append({"case_id": f"k{n}", "where": (float(i), float(j) / 100)})
            n += 1
    _seed(conn, rows)
    country = c.get("/v1/dataset").json()["counts"]["country"]
    assert len(country) == 41 and "other" in country
    assert country["other"] == 1 + 2 + 3 + 4 + 5
    assert "Country00" not in country and country["Country44"] == 45
    assert sum(country.values()) == n


def test_a_site_with_no_coordinates_counts_as_an_unknown_country(env):
    c, conn, _ = env
    db.add_cases(conn, [{"case_id": "nowhere", "recipe": "v2", "city_cluster": "c",
                         "split": "train", "spec": {"dirs": [0]}}])
    assert c.get("/v1/dataset").json()["counts"]["country"] == {"unknown": 1}


def test_the_top_40_does_not_depend_on_row_order_and_unknown_takes_no_slot(monkeypatch):
    """41 countries of 2 cases each: which 40 are named used to follow the
    order rows came out of the table (most_common() breaks ties by first
    sight), which a heartbeat's UPDATE reshuffles on Postgres. And "unknown"
    took one of the 40 named slots."""
    monkeypatch.setattr(dataset.places, "locate",
                        lambda lat, lon: {"country": f"C{int(lat):02d}"})

    def row(i, lat):
        return {"case_id": f"r{i}", "state": "pending", "split": "train", "lcz": None,
                "recipe": "r", "spec": json.dumps({"lat": lat, "lon": i / 1000}),
                "telemetry": None, "metrics": None}

    rows = [row(i, float(i // 2)) for i in range(82)]                 # C00..C40, 2 each
    rows += [{**row(100 + i, 0.0), "spec": "{}"} for i in range(3)]   # 3 without coordinates
    forward = dataset.compute(list(rows)).public["counts"]["country"]
    dataset._COUNTRY.clear()
    backward = dataset.compute(list(reversed(rows))).public["counts"]["country"]
    assert forward == backward
    named = [k for k in forward if k not in ("unknown", "other")]
    assert len(named) == dataset.TOP_COUNTRIES
    # Ties broken by name: C00..C39 named, C40 in "other".
    assert named == [f"C{i:02d}" for i in range(40)]
    assert forward["unknown"] == 3 and forward["other"] == 2
    assert sum(forward.values()) == len(rows)


# -- distributions -----------------------------------------------------------------------

def test_the_bins_are_p1_to_p99_so_an_outlier_cannot_squash_the_plot(env):
    c, conn, _ = env
    # 101 heights 0..100 m and one 10 km "building" (a bad height prediction).
    rows = [{"case_id": f"h{i}", "lcz": "LCZ4", "telemetry": _site(bht_m=float(i))}
            for i in range(101)]
    rows.append({"case_id": "tower", "lcz": "LCZ4", "telemetry": _site(bht_m=10_000.0)})
    _seed(conn, rows)
    m = c.get("/v1/dataset").json()["metrics"]["bht_m"]
    values = sorted([float(i) for i in range(101)] + [10_000.0])
    # numpy.quantile(values, [0.01, 0.99]) with linear interpolation, by hand:
    # h = 101 * 0.01 = 1.01 -> 1 + 0.01 * (2 - 1); h = 101 * 0.99 = 99.99 -> 99 + 0.99 * 1
    p1, p99 = 1.01, 99.99
    assert len(m["bins"]) == 25
    assert m["bins"][0] == pytest.approx(p1) and m["bins"][-1] == pytest.approx(p99)
    assert all(a < b for a, b in zip(m["bins"], m["bins"][1:]))
    hist = m["all"]["hist"]
    assert len(hist) == 24 and sum(hist) == len(values)
    # Outside the edges go to the END bins, not off the plot: 0 and 1 below p1,
    # 100 and the tower above p99.
    assert hist[0] >= 2 and hist[-1] >= 2
    assert m["all"]["max"] == 10_000.0 and m["all"]["min"] == 0.0
    assert m["all"]["median"] == pytest.approx(50.5)
    assert m["all"]["mean"] == pytest.approx(sum(values) / len(values))


def test_every_reference_set_shares_the_metric_s_bins(env):
    c, conn, _ = env
    rows = [{"case_id": f"a{i}", "lcz": "LCZ1", "telemetry": _site(bcr=0.30 + 0.01 * i)}
            for i in range(20)]
    rows += [{"case_id": f"b{i}", "lcz": "LCZ9", "telemetry": _site(bcr=0.02 + 0.005 * i)}
             for i in range(10)]
    _seed(conn, rows)
    m = c.get("/v1/dataset").json()["metrics"]["bcr"]
    edges = m["bins"]
    assert set(m["by_lcz"]) == {"LCZ1", "LCZ9"}
    for lcz, group, vals in (("LCZ1", m["by_lcz"]["LCZ1"], [0.30 + 0.01 * i for i in range(20)]),
                             ("LCZ9", m["by_lcz"]["LCZ9"], [0.02 + 0.005 * i for i in range(10)])):
        assert group["n"] == len(vals) and len(group["hist"]) == 24
        # Recount against the published edges: the group was binned on THEM,
        # not on a range of its own.
        expect = [0] * 24
        for v in vals:
            i = sum(1 for e in edges[1:-1] if v >= e)
            expect[i] += 1
        assert group["hist"] == expect, lcz
    # Per bin, the LCZ histograms add up to the whole campaign's.
    total = [a + b for a, b in zip(m["by_lcz"]["LCZ1"]["hist"], m["by_lcz"]["LCZ9"]["hist"])]
    assert total == m["all"]["hist"]


def test_the_lcz_breakdown_has_its_own_statistics(env):
    c, conn, _ = env
    rows = [{"case_id": f"x{i}", "lcz": "LCZ2", "telemetry": _site(svf=v)}
            for i, v in enumerate([0.2, 0.4, 0.6])]
    rows += [{"case_id": f"y{i}", "lcz": "LCZ8", "telemetry": _site(svf=v)}
             for i, v in enumerate([0.8, 0.9])]
    rows.append({"case_id": "z", "lcz": None, "telemetry": _site(svf=0.5)})
    _seed(conn, rows)
    m = c.get("/v1/dataset").json()["metrics"]["svf"]
    assert m["all"]["n"] == 6
    # A case without an LCZ is in "all" and in no LCZ group.
    assert set(m["by_lcz"]) == {"LCZ2", "LCZ8"}
    two, eight = m["by_lcz"]["LCZ2"], m["by_lcz"]["LCZ8"]
    assert (two["n"], two["min"], two["median"], two["max"]) == (3, 0.2, pytest.approx(0.4), 0.6)
    assert (eight["n"], eight["median"]) == (2, pytest.approx(0.85))
    assert two["p25"] == pytest.approx(0.3) and two["p75"] == pytest.approx(0.5)


def test_one_value_still_has_ascending_bins_with_it_in_the_middle(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": "only", "lcz": "LCZ3", "telemetry": _site(ar=1.5)}])
    m = c.get("/v1/dataset").json()["metrics"]["ar"]
    assert len(m["bins"]) == 25 and all(a < b for a, b in zip(m["bins"], m["bins"][1:]))
    assert m["all"]["hist"][12] == 1 and sum(m["all"]["hist"]) == 1


# -- where each number comes from ------------------------------------------------------------

def test_telemetry_first_then_the_completion_metrics(env):
    c, conn, _ = env
    _seed(conn, [
        # Telemetry wins where both have it.
        {"case_id": "both", "lcz": "LCZ1", "state": "done",
         "telemetry": {**_site(bcr=0.5), "mesh": {"total_cells": 2_000_000, "meshes": {
             "mesh": {"max_skewness": 3.1, "max_non_orthogonality": 61.0},
             "mesh_fine": {"max_skewness": 4.2, "max_non_orthogonality": 55.0}}}},
         "metrics": {"urban_form": {"bcr": 0.9, "rar": 0.12}, "mesh_cells": 1,
                     "case_seconds": 7200, "mesh_seconds": 1800, "solve_seconds": 5400}},
        # A case that finished before telemetry existed: metrics only.
        {"case_id": "old", "lcz": "LCZ1", "state": "done",
         "metrics": {"urban_form": {"bcr": 0.2}, "mesh_cells": 900_000, "case_seconds": 3600}},
        # Run times are for DONE cases: a failed case's describe the failure.
        {"case_id": "failed", "lcz": "LCZ1", "state": "pending",
         "metrics": {"case_seconds": 99_999}},
    ])
    m = c.get("/v1/dataset").json()["metrics"]
    assert sorted([m["bcr"]["all"]["min"], m["bcr"]["all"]["max"]]) == [0.2, 0.5]
    assert m["rar"]["all"]["n"] == 1                     # from metrics, telemetry lacked it
    assert (m["total_cells"]["all"]["min"], m["total_cells"]["all"]["max"]) == (900_000, 2_000_000)
    # The worst mesh of the case, not the first or an average.
    assert m["max_skewness"]["all"]["max"] == 4.2
    assert m["max_non_orthogonality"]["all"]["max"] == 61.0
    assert m["case_seconds"]["all"]["n"] == 2 and m["case_seconds"]["all"]["max"] == 7200
    assert m["solve_seconds"]["all"]["n"] == 1
    assert m["n_buildings"]["all"]["n"] == 0


def test_site_metrics_come_from_the_site_report(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": "s", "lcz": "LCZ5", "telemetry": {"site": {
        "urban_form": {}, "n_buildings": 87, "terrain_relief_m": 14.5,
        "canopy_fraction": 0.22, "dem": "gedtm30"}}}])
    m = c.get("/v1/dataset").json()["metrics"]
    assert m["n_buildings"]["all"]["max"] == 87
    assert m["terrain_relief_m"]["all"]["max"] == 14.5
    assert m["canopy_fraction"]["all"]["max"] == 0.22


def test_values_that_are_not_finite_numbers_are_ignored(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": "n1", "telemetry": _site(bcr="0.4")},
                 {"case_id": "n2", "telemetry": _site(bcr=True)},
                 {"case_id": "n3", "telemetry": _site(bcr=None)},
                 {"case_id": "n4", "telemetry": _site(bcr=0.25)}])
    conn.execute("UPDATE cases SET telemetry='not json' WHERE case_id='n1'")
    assert c.get("/v1/dataset").json()["metrics"]["bcr"]["all"]["n"] == 1


def test_a_number_too_large_for_a_float_is_ignored_not_fatal(env, monkeypatch):
    """JSON has no integer limit. math.isfinite(10**400) RAISES, so one such
    value posted as telemetry made every aggregate fail -- /v1/dataset 500 on
    every request, each one re-reading the whole campaign, for as long as the
    row existed."""
    c, conn, _ = env
    db.add_cases(conn, [{"case_id": "big", "recipe": "v2", "city_cluster": "c",
                         "split": "train", "spec": {"lat": NYC[0], "lon": NYC[1]}}])
    lease = db.lease(conn, "w")[0]
    body = ('{"lease_id": "%s", "case_id": "big", "kind": "site", "data": '
            '{"n_buildings": 1%s, "terrain_relief_m": 12.5}}' % (lease.lease_id, "0" * 400))
    r = c.post("/v1/telemetry", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 200
    loads = []
    real = db.dataset_rows
    monkeypatch.setattr(db, "dataset_rows", lambda conn, **kw: loads.append(1) or real(conn, **kw))
    for _ in range(2):
        got = c.get("/v1/dataset")
        assert got.status_code == 200
    assert len(loads) == 1
    m = got.json()["metrics"]
    assert m["n_buildings"]["all"]["n"] == 0 and m["terrain_relief_m"]["all"]["n"] == 1
    case = c.get("/v1/cases/big").json()
    assert set(case["percentiles"]) == {"terrain_relief_m"}


def test_extreme_finite_values_cannot_overflow_the_statistics(env):
    """Two values near the float limit: their sum (the mean) and difference
    (the bin width) are infinite -- math.fsum raised, and ±1.7e308 made NaN
    bins. Past dataset.MAX_MAGNITUDE a value is ignored; within it, nothing
    overflows."""
    c, conn, _ = env
    _seed(conn, [{"case_id": "hi", "telemetry": {"site": {"terrain_relief_m": 1.7e308}}},
                 {"case_id": "lo", "telemetry": {"site": {"terrain_relief_m": -1.7e308}}},
                 {"case_id": "a", "telemetry": {"site": {"terrain_relief_m": 1e300}}},
                 {"case_id": "b", "telemetry": {"site": {"terrain_relief_m": -1e300}}},
                 {"case_id": "c", "telemetry": {"site": {"terrain_relief_m": 1e300}}}])
    got = c.get("/v1/dataset")
    assert got.status_code == 200
    m = got.json()["metrics"]["terrain_relief_m"]
    assert m["all"]["n"] == 3 and m["all"]["max"] == 1e300 and m["all"]["min"] == -1e300
    assert all(isinstance(e, float) for e in m["bins"]) and len(m["bins"]) == 25
    assert m["all"]["mean"] == pytest.approx(1e300 / 3)


# -- percentiles on one case ---------------------------------------------------------------

def test_a_case_s_percentiles_are_exact(env):
    c, conn, _ = env
    # Ten cases, bcr 0.1..1.0; the LCZ1 half is the lower five.
    rows = [{"case_id": f"p{i}", "lcz": "LCZ1" if i < 5 else "LCZ6",
             "telemetry": _site(bcr=round(0.1 * (i + 1), 2))} for i in range(10)]
    _seed(conn, rows)
    got = c.get("/v1/cases/p3").json()                       # bcr 0.4, LCZ1
    assert got["telemetry"]["site"]["urban_form"] == {"bcr": 0.4}
    pct = got["percentiles"]
    assert set(pct) == {"bcr"}
    # 3 of 10 below it and 1 equal: (3 + 0.5) / 10. Among LCZ1: (3 + 0.5) / 5.
    assert pct["bcr"] == {"value": 0.4, "all": 35.0, "lcz": 70.0}
    # The top of the campaign is not 100: half of itself counts.
    assert c.get("/v1/cases/p9").json()["percentiles"]["bcr"]["all"] == 95.0


def test_ties_count_half(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": f"t{i}", "lcz": "LCZ1", "telemetry": _site(bcr=v)}
                 for i, v in enumerate([0.1, 0.2, 0.2, 0.2, 0.9])])
    # 1 below, 3 equal: (1 + 1.5) / 5.
    assert c.get("/v1/cases/t2").json()["percentiles"]["bcr"]["all"] == 50.0


def test_a_case_without_an_lcz_or_values_is_ranked_honestly(env):
    c, conn, _ = env
    _seed(conn, [{"case_id": "free", "lcz": None, "telemetry": _site(bcr=0.3)},
                 {"case_id": "other", "lcz": "LCZ1", "telemetry": _site(bcr=0.6)},
                 {"case_id": "bare", "lcz": "LCZ1"}])
    free = c.get("/v1/cases/free").json()["percentiles"]
    assert free["bcr"]["lcz"] is None and free["bcr"]["all"] == 25.0
    bare = c.get("/v1/cases/bare").json()
    assert bare["percentiles"] == {} and bare["telemetry"] == {}


def test_a_value_newer_than_the_aggregate_is_ranked_against_it(env):
    """The case's own value is read live; the population is the cached one."""
    c, conn, clock = env
    _seed(conn, [{"case_id": f"q{i}", "lcz": "LCZ1", "telemetry": _site(bcr=0.1 * (i + 1))}
                 for i in range(4)])
    c.get("/v1/dataset")                                   # cache: 0.1 .. 0.4
    conn.execute("UPDATE cases SET telemetry=? WHERE case_id='q0'",
                 (json.dumps(_site(bcr=0.95)),))
    assert c.get("/v1/cases/q0").json()["percentiles"]["bcr"] == {
        "value": 0.95, "all": 100.0, "lcz": 100.0}


# -- the cache -------------------------------------------------------------------------------

def test_the_aggregate_is_cached_for_60_seconds(env):
    c, conn, clock = env
    _seed(conn, [{"case_id": "first"}])
    first = c.get("/v1/dataset").json()
    assert first["cases"] == 1
    _seed(conn, [{"case_id": "second"}])
    clock.t += 59.0
    assert c.get("/v1/dataset").json()["cases"] == 1       # still the cached answer
    clock.t += 1.0
    assert c.get("/v1/dataset").json()["cases"] == 2       # 60 s on: recomputed


def test_the_case_endpoint_shares_the_cache(env, monkeypatch):
    c, conn, clock = env
    _seed(conn, [{"case_id": "shared", "telemetry": _site(bcr=0.5)}])
    reads = []
    real = db.dataset_rows
    monkeypatch.setattr(db, "dataset_rows", lambda conn, **kw: reads.append(1) or real(conn, **kw))
    c.get("/v1/dataset")
    c.get("/v1/cases/shared")
    c.get("/v1/cases/shared")
    assert len(reads) == 1
    clock.t += 60
    c.get("/v1/cases/shared")
    assert len(reads) == 2


def _lock_is_free() -> bool:
    """Whether db._LOCK can be taken from ANOTHER thread: it is reentrant, so
    the calling thread would always get it."""
    seen = {}

    def other():
        seen["free"] = db._LOCK.acquire(timeout=2)
        if seen["free"]:
            db._LOCK.release()
    t = threading.Thread(target=other)
    t.start()
    t.join()
    return seen["free"]


def test_the_statistics_are_computed_outside_the_lock(env, monkeypatch):
    """Short locked reads, and the counting with db._LOCK free, so a
    campaign-wide aggregation does not stall every worker's heartbeat behind it."""
    c, conn, _ = env
    _seed(conn, [{"case_id": "l1", "telemetry": _site(bcr=0.5)}])
    seen = []
    real = dataset._case
    monkeypatch.setattr(dataset, "_case", lambda row: seen.append(_lock_is_free()) or real(row))
    assert c.get("/v1/dataset").status_code == 200
    assert seen == [True]


def test_the_campaign_is_read_a_page_at_a_time(env, monkeypatch):
    """Read whole, the table measured +300 MB on Postgres at 30,000 cases. By
    pages, each its own short locked read, with the lock free in between."""
    c, conn, _ = env
    monkeypatch.setattr(db, "DATASET_PAGE_ROWS", 2)
    _seed(conn, [{"case_id": f"g{i}", "lcz": "LCZ1", "telemetry": _site(bcr=0.1 * (i + 1))}
                 for i in range(5)])
    pages = []
    real = db.dataset_rows

    def paged(conn, **kw):
        out = real(conn, **kw)
        pages.append((kw.get("after"), [r["case_id"] for r in out], _lock_is_free()))
        return out
    monkeypatch.setattr(db, "dataset_rows", paged)
    body = c.get("/v1/dataset").json()
    assert body["cases"] == 5 and body["metrics"]["bcr"]["all"]["n"] == 5
    assert [p[:2] for p in pages] == [(None, ["g0", "g1"]), ("g1", ["g2", "g3"]), ("g3", ["g4"])]
    assert all(p[2] for p in pages)


def test_a_failed_computation_is_remembered_for_the_ttl(env, monkeypatch):
    """A row that breaks the statistics used to cost a full campaign read on
    EVERY /v1/dataset and every case GET, forever. Now once per TTL."""
    c, conn, clock = env
    _seed(conn, [{"case_id": "f1", "telemetry": _site(bcr=0.5)}])
    loads = []
    real = db.dataset_rows
    monkeypatch.setattr(db, "dataset_rows", lambda conn, **kw: loads.append(1) or real(conn, **kw))
    monkeypatch.setattr(dataset, "_country", lambda spec: 1 / 0)
    for _ in range(3):
        r = c.get("/v1/dataset")
        assert r.status_code == 503 and r.headers["retry-after"] == "60"
        assert c.get("/v1/cases/f1").json()["percentiles"] == {}
    assert len(loads) == 1
    clock.t += 60
    assert c.get("/v1/dataset").status_code == 503
    assert len(loads) == 2


def test_a_failure_after_a_success_serves_the_last_good_aggregate(env, monkeypatch):
    c, conn, clock = env
    _seed(conn, [{"case_id": "k1", "telemetry": _site(bcr=0.5)}])
    good = c.get("/v1/dataset").json()
    monkeypatch.setattr(dataset, "_country", lambda spec: 1 / 0)
    clock.t += 60
    assert c.get("/v1/dataset").json() == good
    assert c.get("/v1/cases/k1").json()["percentiles"]["bcr"]["all"] == 50.0


def test_a_case_never_waits_for_the_aggregate():
    """A cold aggregate is seconds of work at campaign scale, and a request
    waiting on it holds a threadpool slot that leases and heartbeats need. The
    case's GET takes what there is -- nothing, the first time -- and the
    refresh runs in the background."""
    gate, started, loads = threading.Event(), threading.Event(), []

    def load():
        loads.append(1)
        started.set()
        gate.wait(5)
        return [{"case_id": "c", "state": "done", "split": "train", "lcz": None,
                 "recipe": "r", "spec": "{}", "telemetry": json.dumps(_site(bcr=0.5)),
                 "metrics": None}]

    cache = dataset.DatasetCache(load, ttl=60, spawn=IN_BACKGROUND)
    assert cache.peek() is None                  # returned while the load is blocked
    assert started.wait(5)
    assert cache.peek() is None and len(loads) == 1   # one refresh, not one per call
    gate.set()
    agg = cache.get()                            # waits: there is nothing to serve yet
    assert agg.public["cases"] == 1 and cache.peek() is agg and len(loads) == 1


def test_a_stale_aggregate_is_served_while_another_request_refreshes_it():
    clock = _Clock()
    gate, loads = threading.Event(), []

    def load():
        loads.append(1)
        if len(loads) > 1:
            gate.wait(5)
        return [{"case_id": f"c{len(loads)}", "state": "done", "split": "train",
                 "lcz": None, "recipe": "r", "spec": "{}", "telemetry": None,
                 "metrics": None}] * len(loads)

    cache = dataset.DatasetCache(load, ttl=60, clock=clock, spawn=IN_BACKGROUND)
    first = cache.get()
    clock.t += 60
    assert cache.peek() is first                 # stale, served; refresh started
    assert cache.get() is first                  # the refresh is running: not waited for
    gate.set()
    for _ in range(100):
        if cache.peek() is not first:
            break
        threading.Event().wait(0.02)
    assert cache.get().public["cases"] == 2 and len(loads) == 2


def test_concurrent_requests_share_one_computation(tmp_path, monkeypatch):
    loads = []
    gate = threading.Event()

    def load():
        loads.append(1)
        gate.wait(2)
        return [{"case_id": "c", "state": "done", "split": "train", "lcz": None,
                 "recipe": "r", "spec": "{}", "telemetry": None, "metrics": None}]

    cache = dataset.DatasetCache(load, ttl=60)
    out = []
    threads = [threading.Thread(target=lambda: out.append(cache.get())) for _ in range(5)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert len(loads) == 1 and len({id(a) for a in out}) == 1


def test_the_dashboard_s_fallback_labels_match_the_registry():
    """The dashboard draws a case's ranks with its own copy of the registry's
    labels, units and groups while /v1/dataset cannot be read (METRIC_INFO in
    dashboard.html). A metric added here and not there prints under its bare
    key; a unit changed here and not there prints wrong."""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "casebroker" / "static" / "dashboard.html").read_text()
    block = src.split("const METRIC_INFO = {", 1)[1].split("\n  };", 1)[0]
    rows = re.findall(r'^\s*(\w+): \["([^"]*)", (null|"[^"]*"), "(\w+)"\],$', block, re.M)
    got = [(k, label, None if unit == "null" else unit.strip('"'), group)
           for k, label, unit, group in rows]
    assert got == [(m.key, m.label, m.unit, m.group) for m in dataset.METRICS]

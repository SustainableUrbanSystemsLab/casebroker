"""The residual curves of a CFD case.

A solve report carries the NEWEST residual of each field, which cannot tell a
direction that fell three decades from one that has sat at 1e-3 for an hour. The
case page needs the curve, so a node sends one (telemetry kind `residuals`) and
the broker keeps one per wind direction apart from the telemetry, which holds
only the latest of each kind. A node that predates the kind still gets a coarse
curve, assembled from the successive `solve` reports.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

WRITE, READ = "w-secret", "r-secret"
FIELDS = ("Ux", "Uy", "Uz", "epsilon", "k", "p")


def _cases(n=3):
    return [{"lat": 40.70 + i * 0.01, "lon": -74.0, "recipe": "v2", "city_cluster": "nyc",
             "lcz": "LCZ1", "spec": {"dirs": [0, 90]}} for i in range(n)]


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "res.sqlite")
    c = TestClient(create_app(db_path=path, tokens=[WRITE], readonly_tokens=[READ]))
    c.headers.update({"Authorization": f"Bearer {WRITE}"})
    assert c.post("/v1/cases", json=_cases()).json()["added"] == 3
    return c, path


def _lease(c, worker="node-a", count=1):
    got = c.post("/v1/lease", json={"worker_id": worker, "count": count}).json()
    assert len(got) == count
    return got


def _series(direction="case_112", n=5, start=1, step=100, **extra):
    """A falling curve of n points in every field, the shape a node sends."""
    its = [start + i * step for i in range(n)]
    out = {"direction": direction, "iterations": its,
           "fields": {f: [10.0 ** -(1 + i * 0.5 + j * 0.1) for i in range(n)]
                      for j, f in enumerate(FIELDS)},
           "end_time": 2000, "total": n, "complete": False}
    out.update(extra)
    return out


def _post(c, lease, data, kind="residuals", case_id=None, **headers):
    body = {"lease_id": lease["lease_id"], "case_id": case_id or lease["case_id"],
            "kind": kind, "data": data}
    return c.post("/v1/telemetry", json=body, headers=headers or None)


def _get(c, case_id, direction=None, **headers):
    params = {"direction": direction} if direction else None
    return c.get(f"/v1/cases/{case_id}/residuals", params=params, headers=headers or None)


def _solve(direction, iteration, residuals, end_time=2000):
    return {"directions_total": 32, "directions_done": 10, "current": direction,
            "iteration": iteration, "end_time": end_time, "residuals": residuals, "finished": {}}


# -- what is stored, and where -----------------------------------------------------

def test_a_series_is_stored_per_direction_and_read_back(env):
    c, _ = env
    lease = _lease(c, "foam-3")[0]
    assert _post(c, lease, _series("case_112", n=5)).json() == {"ok": True}
    got = _get(c, lease["case_id"]).json()
    assert got["case_id"] == lease["case_id"] and got["direction"] == "case_112"
    (entry,) = got["directions"]
    assert entry["direction"] == "case_112" and entry["source"] == "trace"
    assert entry["n"] == 5 and entry["iteration"] == 401 and entry["end_time"] == 2000
    assert entry["worker"] == "foam-3" and entry["reported_at"] > 1_700_000_000
    s = got["series"]
    assert s["iterations"] == [1, 101, 201, 301, 401]
    assert set(s["fields"]) == set(FIELDS) and all(len(v) == 5 for v in s["fields"].values())
    assert s["total"] == 5 and s["complete"] is False


def test_it_is_not_telemetry_and_costs_no_kind(env):
    """One per direction, 32 to a case: it cannot live among the 16 kinds a case
    may hold, nor be returned in the case record every list refresh re-reads."""
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, _series()).status_code == 200
    assert c.get(f"/v1/cases/{lease['case_id']}").json()["telemetry"] == {}
    # Sixteen real kinds still fit after it.
    for i in range(db.TELEMETRY_MAX_KINDS):
        assert _post(c, lease, {"n": i}, kind=f"kind_{i}").status_code == 200
    assert _post(c, lease, _series("case_011")).status_code == 200
    assert len(_get(c, lease["case_id"]).json()["directions"]) == 2


def test_posting_a_direction_replaces_that_direction_only(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _series("case_000", n=3))
    _post(c, lease, _series("case_011", n=4))
    _post(c, lease, _series("case_000", n=6, complete=True))
    got = _get(c, lease["case_id"], "case_000").json()
    assert [(d["direction"], d["n"]) for d in got["directions"]] == [("case_000", 6), ("case_011", 4)]
    assert got["series"]["complete"] is True and len(got["series"]["iterations"]) == 6
    other = _get(c, lease["case_id"], "case_011").json()["series"]
    assert len(other["iterations"]) == 4 and other["complete"] is False


def test_without_a_direction_the_series_is_the_one_reported_last(env):
    c, path = env
    lease = _lease(c)[0]
    _post(c, lease, _series("case_000"))
    _post(c, lease, _series("case_011"))
    raw = db.connect(path)
    raw.execute("UPDATE case_residuals SET reported_at = 100 WHERE direction = 'case_000'")
    raw.execute("UPDATE case_residuals SET reported_at = 200 WHERE direction = 'case_011'")
    assert _get(c, lease["case_id"]).json()["direction"] == "case_011"
    raw.execute("UPDATE case_residuals SET reported_at = 300 WHERE direction = 'case_000'")
    assert _get(c, lease["case_id"]).json()["direction"] == "case_000"


def test_a_direction_that_has_no_series_answers_null_not_404(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _series("case_000"))
    got = _get(c, lease["case_id"], "case_999")
    assert got.status_code == 200
    assert got.json()["direction"] == "case_999" and got.json()["series"] is None
    assert len(got.json()["directions"]) == 1


def test_a_case_with_no_series_is_empty_and_an_unknown_case_is_404(env):
    c, _ = env
    lease = _lease(c)[0]
    got = _get(c, lease["case_id"])
    assert got.status_code == 200
    assert got.json() == {"case_id": lease["case_id"], "directions": [], "direction": None,
                          "series": None}
    assert _get(c, "no-such-case").status_code == 404


# -- the numbers --------------------------------------------------------------------

def test_values_are_kept_to_four_significant_digits(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, {"direction": "case_000", "iterations": [1, 2, 3],
                     "fields": {"p": [0.012345678901234, 8.634567e-05, 1.0]}})
    p = _get(c, lease["case_id"]).json()["series"]["fields"]["p"]
    assert p == [0.01235, 8.635e-05, 1]


def test_a_diverging_field_is_null_and_the_record_can_still_be_served(env):
    """OpenFOAM prints `nan` for a diverging residual. Stored as it came, the
    case's GET could not be serialised again."""
    c, _ = env
    lease = _lease(c)[0]
    raw = json.dumps({"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                      "kind": "residuals",
                      "data": {"direction": "case_000", "iterations": [1, 2, 3],
                               "fields": {"p": [0.5, float("nan"), float("inf")],
                                          "Ux": [0.1, 0.2, None]}}})
    r = c.post("/v1/telemetry", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 200
    got = _get(c, lease["case_id"])
    assert got.status_code == 200
    assert got.json()["series"]["fields"] == {"p": [0.5, None, None], "Ux": [0.1, 0.2, None]}


def test_iterations_that_go_backwards_are_cut_to_the_last_run(env):
    """The numerics ladder restarts a direction from 0 on a safer rung and tees
    into the same log: the log read whole is the failed run, then this one."""
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, {"direction": "case_000", "iterations": [1, 50, 100, 1, 50, 99],
                     "fields": {"p": [1, 0.1, 0.01, 1, 0.2, 0.05]}, "total": 6})
    s = _get(c, lease["case_id"]).json()["series"]
    assert s["iterations"] == [1, 50, 99] and s["fields"]["p"] == [1, 0.2, 0.05]
    # total says the series was cut from MORE than it now holds, never fewer.
    assert s["total"] == 6


def test_total_is_dropped_when_it_cannot_be_true(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _series(n=5, total=3))
    assert _get(c, lease["case_id"]).json()["series"]["total"] is None


def test_an_absurd_iteration_cannot_cost_the_node_its_solve_report(env):
    """The iteration lands in a REAL column, a 32-bit float on Postgres where 1e300
    is an error -- inside the transaction that also holds the solve report."""
    c, _ = env
    lease = _lease(c)[0]
    huge = {"directions_total": 32, "directions_done": 0, "current": "case_112",
            "iteration": 1e300, "end_time": 1e300, "residuals": {"p": 0.1}, "finished": {}}
    assert _post(c, lease, huge, kind="solve").status_code == 200
    assert c.get(f"/v1/cases/{lease['case_id']}").json()["telemetry"]["solve"]["current"] == "case_112"
    assert _get(c, lease["case_id"]).json()["directions"] == []
    # And as a series: refused, not stored.
    assert _post(c, lease, {"direction": "case_000", "iterations": [1, 1e300], "fields": {"p": [1, 0.5]}}).status_code == 422
    # A cap that is nonsense is dropped from a series that is otherwise fine.
    assert _post(c, lease, _series("case_000", n=3, end_time=1e300)).status_code == 200
    assert _get(c, lease["case_id"]).json()["directions"][0]["end_time"] is None


@pytest.mark.parametrize("bad", [
    {"direction": "mesh"},                                   # a part, not a direction
    {"direction": "../etc"},
    {"direction": "case_" + "x" * 40},
    {"direction": 112},
    {"direction": None},
    {"iterations": []},
    {"iterations": "1,2,3"},
    {"iterations": [1, 2, "three", 4, 5]},
    {"iterations": [1, 2, True, 4, 5]},
    {"iterations": [1, 2, None, 4, 5]},
    {"fields": {}},
    {"fields": {"p": [1, 2, 3]}},                            # not as long as iterations
    {"fields": {"p": "nope"}},
    {"fields": {"p": [1, 2, "x", 4, 5]}},
    {"fields": {"p": [1, 2, True, 4, 5]}},
    {"fields": {"1bad": [1, 2, 3, 4, 5]}},
    {"fields": {"has space": [1, 2, 3, 4, 5]}},
    {"fields": {f"f{i}": [1] * 5 for i in range(db.RESIDUAL_MAX_FIELDS + 1)}},
])
def test_a_record_that_is_not_a_series_is_422_and_writes_nothing(env, bad):
    c, _ = env
    lease = _lease(c)[0]
    r = _post(c, lease, {**_series("case_000", n=5), **bad})
    assert r.status_code == 422, r.text
    assert "residuals" in r.json()["detail"]
    assert _get(c, lease["case_id"]).json()["directions"] == []


def test_a_series_over_the_telemetry_limit_is_413(env):
    c, _ = env
    lease = _lease(c)[0]
    n = db.RESIDUAL_MAX_POINTS
    big = {"direction": "case_000", "iterations": list(range(n)),
           "fields": {f: [0.123456789012345] * n for f in FIELDS}}
    assert len(json.dumps(big)) > db.TELEMETRY_MAX_BYTES
    assert _post(c, lease, big).status_code == 413
    assert _get(c, lease["case_id"]).json()["directions"] == []


def test_more_points_than_a_chart_needs_is_refused(env):
    c, _ = env
    lease = _lease(c)[0]
    n = db.RESIDUAL_MAX_POINTS + 1
    assert _post(c, lease, {"direction": "case_000", "iterations": list(range(n)),
                            "fields": {"p": [1] * n}}).status_code == 422


def test_a_case_holds_series_for_a_bounded_number_of_directions(env):
    c, _ = env
    lease = _lease(c)[0]
    for i in range(db.RESIDUAL_MAX_DIRECTIONS):
        assert _post(c, lease, _series(f"case_{i:03d}", n=2)).status_code == 200
    assert _post(c, lease, _series("case_extra", n=2)).status_code == 413
    # One it already has is still replaced.
    assert _post(c, lease, _series("case_000", n=3)).status_code == 200
    assert len(_get(c, lease["case_id"]).json()["directions"]) == db.RESIDUAL_MAX_DIRECTIONS


# -- who may write and read ----------------------------------------------------------

def test_writing_needs_write_scope_and_reading_needs_read(env):
    c, _ = env
    lease = _lease(c)[0]
    cid = lease["case_id"]
    assert _post(c, lease, _series(), Authorization="").status_code == 401
    assert _post(c, lease, _series(), Authorization=f"Bearer {READ}").status_code == 401
    assert _post(c, lease, _series()).status_code == 200
    assert _get(c, cid, Authorization="").status_code == 401
    assert _get(c, cid, Authorization=f"Bearer {READ}").status_code == 200


def test_a_superseded_lease_is_409_and_writes_nothing(env):
    c, path = env
    zombie = _lease(c, "node-a")[0]
    raw = db.connect(path)
    raw.execute("UPDATE cases SET lease_expires = 0 WHERE case_id = ?", (zombie["case_id"],))
    fresh = next(l for l in _lease(c, "node-b", count=3) if l["case_id"] == zombie["case_id"])
    assert _post(c, zombie, _series("case_000")).status_code == 409
    assert _get(c, zombie["case_id"]).json()["directions"] == []
    assert _post(c, fresh, _series("case_000")).status_code == 200
    assert _get(c, zombie["case_id"]).json()["directions"][0]["worker"] == "node-b"


def test_a_lease_of_another_case_is_409(env):
    c, _ = env
    a, b = _lease(c, "node-a", count=2)
    assert _post(c, a, _series(), case_id=b["case_id"]).status_code == 409
    assert _get(c, b["case_id"]).json()["directions"] == []


# -- a node that sends no series: the broker builds a coarse one ---------------------

def test_solve_reports_build_a_coarse_curve_for_a_node_without_series(env):
    c, _ = env
    lease = _lease(c, "old-node")[0]
    for it, p in [(100, 0.1), (250, 0.02), (400, 0.005)]:
        _post(c, lease, _solve("case_112", it, {"Ux": p / 2, "p": p}), kind="solve")
    got = _get(c, lease["case_id"]).json()
    (entry,) = got["directions"]
    assert entry["source"] == "reports" and entry["n"] == 3 and entry["iteration"] == 400
    assert entry["end_time"] == 2000
    s = got["series"]
    assert s["iterations"] == [100, 250, 400]
    assert s["fields"] == {"Ux": [0.05, 0.01, 0.0025], "p": [0.1, 0.02, 0.005]}


def test_the_solve_report_itself_is_unchanged(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _solve("case_112", 100, {"p": 0.1}), kind="solve")
    solve = c.get(f"/v1/cases/{lease['case_id']}").json()["telemetry"]["solve"]
    assert solve["current"] == "case_112" and solve["residuals"] == {"p": 0.1}


def test_a_repeated_iteration_is_not_a_new_point(env):
    c, _ = env
    lease = _lease(c)[0]
    for _ in range(3):
        _post(c, lease, _solve("case_112", 100, {"p": 0.1}), kind="solve")
    assert _get(c, lease["case_id"]).json()["directions"][0]["n"] == 1


def test_an_iteration_that_goes_backwards_starts_the_curve_over(env):
    """A rung of the numerics ladder restarting the direction."""
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _solve("case_112", 900, {"p": 0.5}), kind="solve")
    _post(c, lease, _solve("case_112", 950, {"p": 0.6}), kind="solve")
    _post(c, lease, _solve("case_112", 30, {"p": 1.0}), kind="solve")
    s = _get(c, lease["case_id"]).json()["series"]
    assert s["iterations"] == [30] and s["fields"] == {"p": [1]}


def test_a_field_that_appears_later_is_null_before_it(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _solve("case_112", 10, {"Ux": 0.5, "p": 0.9}), kind="solve")
    _post(c, lease, _solve("case_112", 20, {"Ux": 0.4, "p": 0.8, "k": 0.3}), kind="solve")
    _post(c, lease, _solve("case_112", 30, {"Ux": 0.3, "p": 0.7}), kind="solve")
    f = _get(c, lease["case_id"]).json()["series"]["fields"]
    assert f == {"Ux": [0.5, 0.4, 0.3], "p": [0.9, 0.8, 0.7], "k": [None, 0.3, None]}


@pytest.mark.parametrize("solve", [
    {"current": None, "iteration": 10, "residuals": {"p": 0.1}},          # between directions
    {"current": "case_112", "iteration": None, "residuals": {"p": 0.1}},
    {"current": "case_112", "iteration": 10, "residuals": None},          # nothing parsed yet
    {"current": "case_112", "iteration": 10, "residuals": {}},
    {"current": "case_112", "iteration": 10, "residuals": {"p": None}},   # diverged: nothing to plot
    {"current": "mesh", "iteration": 10, "residuals": {"p": 0.1}},
    {"current": "../x", "iteration": 10, "residuals": {"p": 0.1}},
    {"current": 5, "iteration": 10, "residuals": {"p": 0.1}},
    {"current": "case_112", "iteration": "ten", "residuals": {"p": 0.1}},
    {"current": "case_112", "iteration": 10, "residuals": [0.1]},
    {"iteration": 10, "residuals": {"p": 0.1}},
])
def test_a_report_with_nothing_to_plot_adds_nothing_and_is_still_stored(env, solve):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, solve, kind="solve").status_code == 200
    assert _get(c, lease["case_id"]).json()["directions"] == []
    assert "solve" in c.get(f"/v1/cases/{lease['case_id']}").json()["telemetry"]


def test_a_curve_made_of_reports_is_bounded(env):
    c, path = env
    lease = _lease(c)[0]
    n = db.RESIDUAL_SAMPLE_MAX
    for i in range(1, n + 21):
        _post(c, lease, _solve("case_112", i, {"p": 1.0 / i}), kind="solve")
    s = _get(c, lease["case_id"]).json()["series"]
    assert len(s["iterations"]) == n and s["iterations"][-1] == n + 20
    assert s["iterations"][0] == 21, "the oldest go first"


def test_the_nodes_own_series_is_never_added_to_by_a_report(env):
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _series("case_112", n=5))
    _post(c, lease, _solve("case_112", 9999, {"p": 0.5}), kind="solve")
    got = _get(c, lease["case_id"]).json()
    assert got["directions"][0]["source"] == "trace" and got["directions"][0]["n"] == 5
    assert 9999 not in got["series"]["iterations"]


def test_the_nodes_own_series_replaces_one_made_of_reports(env):
    """The node updates mid-direction: from its next report on, it is the solver's
    own curve, not the coarse one."""
    c, _ = env
    lease = _lease(c)[0]
    _post(c, lease, _solve("case_112", 100, {"p": 0.1}), kind="solve")
    assert _get(c, lease["case_id"]).json()["directions"][0]["source"] == "reports"
    _post(c, lease, _series("case_112", n=7))
    got = _get(c, lease["case_id"]).json()
    assert got["directions"][0]["source"] == "trace" and len(got["series"]["iterations"]) == 7


# -- how long it lives ----------------------------------------------------------------

CASE = "v2-00e76e426bea6d52"
MESH = "a1" * 32


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "life.sqlite"))
    db.add_cases(c, [{"case_id": CASE, "spec": {"lat": 1.0, "lon": 2.0}, "recipe": "v2",
                      "city_cluster": "x", "split": "train"}])
    return c


def _store(conn, lease, direction, n=3):
    assert db.post_telemetry(conn, lease.lease_id, CASE, "residuals", _series(direction, n=n)) == "ok"


def _dirs(conn):
    return [d["direction"] for d in db.case_residuals(conn, CASE)["directions"]]


def _ship(conn, lease, part, sha, mesh=MESH):
    assert db.report_part(conn, lease.lease_id, CASE, part, f"{CASE}.{part}.tar.gz", sha, 1000, mesh) == "ok"


def test_a_resume_keeps_every_curve(conn):
    lease = db.lease(conn, "w")[0]
    _store(conn, lease, "case_000")
    _store(conn, lease, "case_011")
    again = db.lease(conn, "w", resume_case_ids=[CASE])[0]
    assert again.case_id == CASE and _dirs(conn) == ["case_000", "case_011"]


def test_a_fresh_claim_starts_over_but_keeps_the_curve_of_a_direction_the_case_holds(conn):
    """The next node takes the case over from the broker's copy and solves only
    the directions not yet shipped, so a shipped direction's curve is still the
    one that describes its result. An unshipped one belonged to the attempt that
    is over."""
    first = db.lease(conn, "cod-359-38")[0]
    _ship(conn, first, "mesh", MESH)
    _ship(conn, first, "case_000", "c0" * 32)
    for d in ("case_000", "case_011", "case_022"):
        _store(conn, first, d)
    conn.execute("UPDATE cases SET lease_expires=0 WHERE case_id=?", (CASE,))
    db.lease(conn, "foam-1")
    assert _dirs(conn) == ["case_000"]


def test_a_fresh_claim_of_a_case_with_nothing_shipped_clears_them_all(conn):
    first = db.lease(conn, "w")[0]
    _store(conn, first, "case_000")
    db.release(conn, first.lease_id, reason="preempted")
    db.lease(conn, "v")
    assert _dirs(conn) == []


def test_a_new_mesh_and_a_parts_reset_drop_the_curves_of_the_old_one(conn):
    lease = db.lease(conn, "w")[0]
    _ship(conn, lease, "mesh", MESH)
    _ship(conn, lease, "case_000", "c0" * 32)
    _store(conn, lease, "case_000")
    _ship(conn, lease, "mesh", "b2" * 32, mesh="b2" * 32)
    assert _dirs(conn) == []
    _store(conn, lease, "case_011")
    assert db.reset_parts(conn, CASE, by="ada") == 1
    assert _dirs(conn) == []


def test_purging_a_case_takes_its_curves_with_it(conn):
    lease = db.lease(conn, "w")[0]
    _store(conn, lease, "case_000")
    out = db.purge_cases(conn, recipe="v2", expect=1)
    assert out["deleted"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM case_residuals").fetchone()["n"] == 0

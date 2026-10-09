"""A machine that cannot run anything must not cost the campaign its cases.

COD-PKAST-7865 ran a worker with Docker Desktop stopped on 2026-09-19. It leased
case after case and failed every one of them with "Docker daemon is not running",
charging an attempt each time; three of those quarantine a site that nothing is
wrong with. ``runner/run_case.sh`` has carried the warning since the first ICE
run -- a wrongly fatal error "silently removes a site from the campaign with no
way back short of editing the database" -- and it had no way back.

What is pinned here is the broker's half: it can put back what a broken node
quarantined, and only that. (The node's half -- a machine that cannot run gives
the case back as a RELEASE and stops -- is Eddy3D's NodeWorker, tested there;
the Python runner that first carried it is retired.) Each has its control: the
ordinary failure path must still charge the case, or a genuinely broken site
would cycle the fleet for ever.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402


def make_db(tmp_path, n=3):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    rows = []
    for i in range(n):
        lat, lon = 34.0 + i * 0.01, -84.0
        rows.append({
            "case_id": ids.case_id(lat, lon, "r1"),
            "spec": {"lat": lat, "lon": lon, "dirs": [0]},
            "recipe": "r1", "city_cluster": "city0", "lcz": "LCZ6", "split": "train",
        })
    db.add_cases(conn, rows)
    return conn


def quarantine_one(conn, worker_id="ws-01", error="Docker daemon is not running"):
    """Burn a case's attempts as the field failure did: one machine failing it
    retryably until the broker gives up on it. The broker now hands a case a
    machine just failed to a DIFFERENT machine first (db.FAIL_COOLDOWN_SECONDS),
    so one broken node only gets the case back once each cooldown has passed --
    the clock moves past it between attempts."""
    case_id = None
    for _ in range(3):
        now = conn.execute("SELECT COALESCE(MAX(ts), 0) AS t FROM events").fetchone()["t"] \
            + db.FAIL_COOLDOWN_SECONDS + 60
        leased = db.lease(conn, worker_id, 1, 900, now=now)
        assert leased, "expected a case to lease"
        assert case_id in (None, leased[0].case_id), "the same case, attempt after attempt"
        case_id = leased[0].case_id
        db.fail(conn, leased[0].lease_id, error, retryable=True, now=now + 60)
    row = conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()
    assert dict(row)["state"] == "quarantined", "the fixture must reproduce the quarantine"
    return case_id


# -- the runner's half ---------------------------------------------------------

def test_a_case_quarantined_by_a_broken_node_can_be_put_back(tmp_path):
    conn = make_db(tmp_path)
    case_id = quarantine_one(conn)

    out = db.reopen_cases(conn, error_contains="Docker daemon", dry_run=False)

    assert out["reopened"] == 1
    row = dict(conn.execute("SELECT state, attempts FROM cases WHERE case_id=?", (case_id,)).fetchone())
    assert row["state"] == "pending"
    # RESET, not decremented: the three failures said nothing about this case, and
    # leaving them counted would quarantine it again on the first real one.
    assert row["attempts"] == 0
    trail = [dict(r)["event"] for r in conn.execute(
        "SELECT event FROM events WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
    assert "reopened" in trail, "the decision has to stay auditable"


def test_the_scan_changes_nothing_until_it_is_asked_to(tmp_path):
    """dry_run defaults to TRUE, as it does for the land audit and for purge:
    finding out how bad it is must not be the same keystroke as fixing it."""
    conn = make_db(tmp_path)
    case_id = quarantine_one(conn)

    out = db.reopen_cases(conn, error_contains="Docker daemon")

    assert out["matched"] == 1 and out["reopened"] == 0
    assert out["examples"][0]["case_id"] == case_id
    assert "Docker daemon" in out["examples"][0]["last_error"]
    assert dict(conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone())["state"] \
        == "quarantined"


def test_it_reopens_only_what_that_node_broke(tmp_path):
    """The filter is the point. "Undo what the broken box did" must not also
    release the sites that are genuinely degenerate."""
    conn = make_db(tmp_path, n=6)
    bad_node = quarantine_one(conn, error="Docker daemon is not running")
    bad_site = quarantine_one(conn, error="snappyHexMesh produced no cells")

    out = db.reopen_cases(conn, error_contains="Docker daemon", dry_run=False)

    assert out["reopened"] == 1
    states = {cid: dict(conn.execute("SELECT state FROM cases WHERE case_id=?", (cid,)).fetchone())["state"]
              for cid in (bad_node, bad_site)}
    assert states[bad_node] == "pending"
    assert states[bad_site] == "quarantined", "a real bad site must stay parked"


def test_a_done_case_is_never_dragged_back_into_the_pool(tmp_path):
    conn = make_db(tmp_path)
    leased = db.lease(conn, "ws-01", 1, 900)
    db.complete(conn, leased[0].lease_id, "file:///r.tar.gz")

    db.reopen_cases(conn, dry_run=False)

    row = dict(conn.execute("SELECT state FROM cases WHERE case_id=?", (leased[0].case_id,)).fetchone())
    assert row["state"] == "done"


# -- the dashboard's half ------------------------------------------------------

def test_a_heartbeat_keeps_the_worker_out_of_Offline(tmp_path):
    """The dashboard calls a worker Offline after 300 s without `last_seen`, and
    only lease, complete and fail used to move it. A node solving a three-hour
    case heartbeats every five minutes exactly as designed and went Offline five
    minutes in -- reporting a healthy fleet as a dead one."""
    conn = make_db(tmp_path)
    leased = db.lease(conn, "ws-01", 1, 900, now=1_000_000)
    seen_at_lease = dict(conn.execute(
        "SELECT last_seen FROM workers WHERE worker_id='ws-01'").fetchone())["last_seen"]

    db.heartbeat(conn, leased[0].lease_id, 900, now=seen_at_lease + 600)

    seen_now = dict(conn.execute(
        "SELECT last_seen FROM workers WHERE worker_id='ws-01'").fetchone())["last_seen"]
    assert seen_now == seen_at_lease + 600, "the heartbeat IS the liveness signal for a long solve"


def test_the_endpoint_needs_a_credential_and_changes_nothing_by_default(tmp_path):
    """What the dashboard's Reopen card calls. Write auth, and the same dry_run
    default the database function has -- a scan must never be the keystroke that
    changes production."""
    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    app = create_app(str(tmp_path / "api.sqlite"), ["w"], ["r"])
    with TestClient(app) as c:
        assert c.post("/v1/cases/reopen").status_code in (401, 403)

        auth = {"Authorization": "Bearer w"}
        body = c.post("/v1/cases/reopen", headers=auth).json()
        assert body["dry_run"] is True and body["reopened"] == 0
        # A read token can see the campaign but must not change it. The code is
        # this broker's business (it answers 401 for a wrong-scope token); what
        # this pins is that it is refused.
        assert c.post("/v1/cases/reopen", headers={"Authorization": "Bearer r"}).status_code in (401, 403)



def test_the_fleet_view_can_say_which_case_a_worker_is_holding(tmp_path):
    """The workers table has no in-flight state of its own, so /v1/status folds the
    case a worker currently holds -- and its latest progress line -- onto the row."""
    conn = make_db(tmp_path)
    leased = db.lease(conn, "ws-01", 1, 900)[0]
    db.heartbeat(conn, leased.lease_id, 900, detail="solve 3/8 dirs · iter 412/2000")

    worker = next(w for w in db.status(conn)["workers"] if w["worker_id"] == "ws-01")

    assert worker["current_case"] == leased.case_id
    assert worker["current_progress"] == "solve 3/8 dirs · iter 412/2000"


def test_a_worker_between_cases_still_appears_with_nothing_in_flight(tmp_path):
    """The control for the subquery: an idle worker must not drop off the fleet
    view, which is what a join instead of a correlated subquery would have done."""
    conn = make_db(tmp_path)
    leased = db.lease(conn, "ws-01", 1, 900)[0]
    db.complete(conn, leased.lease_id, "file:///r.tar.gz")

    worker = next(w for w in db.status(conn)["workers"] if w["worker_id"] == "ws-01")

    assert worker["current_case"] is None
    assert worker["cases_done"] == 1


def test_the_reopen_limit_bounds_what_it_CHANGES(tmp_path):
    """`limit` used to cap only the examples listed back, while the write loop
    ran over every match. So a dry run reporting "matched: 12, here are 3" and
    the same call with dry_run=False reopened all twelve -- the one tool for
    undoing damage was capable of a bigger surprise than the damage.

    Repeated calls must drain the backlog, so the cap has to be deterministic
    (case_id order) rather than arbitrary.
    """
    conn = make_db(tmp_path, n=12)
    for _ in range(12):
        quarantine_one(conn)
    assert db.list_cases(conn, state="quarantined", limit=50)["total"] == 12

    peek = db.reopen_cases(conn, error_contains="Docker daemon", limit=3)
    assert peek["matched"] == 12
    assert len(peek["examples"]) == 3
    assert peek["capped"] is True, "a dry run must say the backlog is larger than the cap"
    assert peek["reopened"] == 0

    first = db.reopen_cases(conn, error_contains="Docker daemon", dry_run=False, limit=3)
    assert first["reopened"] == 3, "the cap bounds the WRITES"
    assert first["capped"] is True
    still = db.list_cases(conn, state="quarantined", limit=50)["total"]
    assert still == 9, f"nine must still be quarantined, found {still}"

    # Deterministic order, so calling again makes progress rather than
    # re-picking the same three.
    second = db.reopen_cases(conn, error_contains="Docker daemon", dry_run=False, limit=3)
    assert second["reopened"] == 3
    assert db.list_cases(conn, state="quarantined", limit=50)["total"] == 6

    rest = db.reopen_cases(conn, error_contains="Docker daemon", dry_run=False, limit=50)
    assert rest["reopened"] == 6
    assert rest["capped"] is False, "nothing was held back this time"
    assert db.list_cases(conn, state="quarantined", limit=50)["total"] == 0

"""A worker stopping a case at its own clock is not the case failing.

Nodes stopped a case at --case-timeout (24 h by default) and reported it as a
retryable failure, and the attempt charged at lease time was never given back.
A cyl-1008/of12-v4 case is ~800 core-hours (23.7 h on 36 ranks), so a slower
worker was stopped mid-solve and charged, and three of those quarantined a site
that was solving fine. Such a stop is now refunded while the case's progress
line was still changing; a wedged case is charged exactly as before.
"""

from __future__ import annotations

import pytest

from casebroker import db

HOUR = 3600
NODE_TIMEOUT = "case exceeded 86400s and was stopped"
RUNNER_TIMEOUT = "runner exceeded 86400s and was killed\n(tail of stderr)"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.sqlite"))
    db.add_cases(c, [{"case_id": "A", "spec": {"lat": 33.75, "lon": -84.39}, "recipe": "r",
                      "city_cluster": "x", "lcz": "LCZ6", "split": "train",
                      "priority": 100, "max_attempts": 3}])
    return c


def lease_and_progress(conn, t0, lines):
    got = db.lease(conn, "w1", count=1, now=t0)[0]
    for dt, line in lines:
        assert db.heartbeat(conn, got.lease_id, detail=line, now=t0 + dt)
    return got


def row(conn):
    return db.get_case(conn, "A")


@pytest.mark.parametrize("message", [NODE_TIMEOUT, RUNNER_TIMEOUT])
def test_a_timeout_of_a_case_still_moving_is_refunded(conn, message):
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(2 * HOUR, "solve 3/32 dirs"), (23 * HOUR, "solve 17/32 dirs")])
    assert row(conn)["attempts"] == 1
    assert db.fail(conn, got.lease_id, message, retryable=True, now=t0 + 24 * HOUR)
    r = row(conn)
    assert r["state"] == "pending"
    assert r["attempts"] == 0, "the worker's own clock stopped it: not the case's attempt"
    assert "exceeded 86400s" in r["last_error"], "the reason is still shown"
    ev = conn.execute("SELECT event, detail FROM events WHERE case_id='A' ORDER BY id DESC LIMIT 1").fetchone()
    assert ev["event"] == "released" and "still progressing" in ev["detail"]


def test_a_timeout_of_a_case_whose_progress_had_stopped_is_charged(conn):
    # Wedged: the line last changed 20 h before the stop.
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(4 * HOUR, "solve 3/32 dirs")])
    assert db.fail(conn, got.lease_id, NODE_TIMEOUT, retryable=True, now=t0 + 24 * HOUR)
    r = row(conn)
    assert r["state"] == "pending" and r["attempts"] == 1


def test_a_timeout_with_no_progress_ever_reported_is_charged(conn):
    t0 = 1_000_000
    got = db.lease(conn, "w1", count=1, now=t0)[0]
    assert db.fail(conn, got.lease_id, NODE_TIMEOUT, retryable=True, now=t0 + 24 * HOUR)
    assert row(conn)["attempts"] == 1


def test_an_ordinary_failure_is_untouched(conn):
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(1 * HOUR, "solve 1/32 dirs")])
    assert db.fail(conn, got.lease_id, "solve exited 1: FPE", retryable=True, now=t0 + 2 * HOUR)
    assert row(conn)["attempts"] == 1


def test_a_fatal_report_is_never_turned_into_a_refund(conn):
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(23 * HOUR, "solve 30/32 dirs")])
    assert db.fail(conn, got.lease_id, NODE_TIMEOUT, retryable=False, now=t0 + 24 * HOUR)
    assert row(conn)["state"] == "quarantined"


def test_refunds_are_capped_so_a_case_too_big_for_every_worker_still_quarantines(conn):
    t = 1_000_000
    states = []
    for i in range(db.TIMEOUT_REFUNDS_MAX + 3):
        got = db.lease(conn, "w1", count=1, now=t)
        if not got:
            break
        db.heartbeat(conn, got[0].lease_id, detail=f"solve {i}/32 dirs", now=t + 23 * HOUR)
        db.fail(conn, got[0].lease_id, NODE_TIMEOUT, retryable=True, now=t + 24 * HOUR)
        states.append((row(conn)["state"], row(conn)["attempts"]))
        t += 25 * HOUR
    # Three refunded stops, then three charged ones, then quarantine.
    assert [s for s, _ in states[:db.TIMEOUT_REFUNDS_MAX]] == ["pending"] * db.TIMEOUT_REFUNDS_MAX
    assert states[-1][0] == "quarantined"

"""A lease cannot be renewed forever, however healthy the heartbeats look.

`lease_expires` answers "did a worker speak recently", and heartbeat pushes it
forward every few minutes for as long as the PROCESS is alive. That catches a
worker that dies -- preemption, walltime, a dropped node -- within the TTL.

It cannot catch a worker that is alive, reporting, and simply never finishing,
because the heartbeat thread in worker.py runs independently of the runner
subprocess. A solve that wedges keeps renewing its own lease, so the case is
never reclaimed and the allocation burns to walltime. That is the gap this cap
closes, and it is not hypothetical -- it is the same failure the runner timeout
addresses from the worker side.
"""

from __future__ import annotations

import pytest

from casebroker import db

DAY = 86400


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "age.sqlite"))
    db.add_cases(c, [{
        "case_id": "A", "spec": {"lat": 33.75, "lon": -84.39}, "recipe": "r",
        "city_cluster": "x", "lcz": "LCZ6", "split": "train",
        "priority": 100, "max_attempts": 5}])
    return c


def test_a_lease_records_when_it_started(conn):
    got = db.lease(conn, "w1", count=1, now=1_000_000)[0]
    row = db.get_case(conn, "A")
    assert row["leased_at"] == 1_000_000
    assert row["lease_expires"] > 1_000_000
    assert got.case_id == "A"


def test_heartbeat_does_not_move_leased_at(conn):
    """The whole point: it is an AGE, not a liveness signal.

    If heartbeat touched it, a wedged worker would keep resetting its own clock
    and the cap could never fire -- which is exactly the bug being fixed.
    """
    got = db.lease(conn, "w1", count=1, now=1_000_000)[0]
    for t in (1_000_600, 1_001_200, 1_001_800):
        assert db.heartbeat(conn, got.lease_id, now=t) is True
    row = db.get_case(conn, "A")
    assert row["leased_at"] == 1_000_000, "heartbeat reset the lease age"
    assert row["lease_expires"] > 1_001_800, "heartbeat did not extend the TTL"


def test_a_worker_heartbeating_past_seven_days_loses_the_case(conn):
    """The case the existing TTL structurally cannot catch."""
    got = db.lease(conn, "w1", count=1, now=1_000_000)[0]
    # Six days of perfectly healthy heartbeats: still its case.
    assert db.heartbeat(conn, got.lease_id, now=1_000_000 + 6 * DAY) is True
    assert db.get_case(conn, "A")["state"] == "leased"

    # Past seven, the same heartbeat is refused and the case goes back.
    assert db.heartbeat(conn, got.lease_id, now=1_000_000 + 7 * DAY + 1) is False
    row = db.get_case(conn, "A")
    assert row["state"] == "pending"
    assert row["lease_id"] is None and row["leased_at"] is None


def test_the_release_is_recorded_with_its_reason(conn):
    got = db.lease(conn, "w1", count=1, now=1_000_000)[0]
    db.heartbeat(conn, got.lease_id, now=1_000_000 + 8 * DAY)
    events = db.case_events(conn, "A") if hasattr(db, "case_events") else None
    if events is None:
        rows = conn.execute(
            "SELECT event, detail FROM events WHERE case_id='A' ORDER BY id").fetchall()
        events = [dict(r) for r in rows]
    released = [e for e in events if e["event"] == "released"]
    assert released, "nothing recorded why the case came back"
    assert "abandoned" in released[-1]["detail"]


def test_another_worker_can_claim_a_stale_lease_even_if_it_has_not_expired(conn):
    """The reclaim must not depend on the wedged worker ever calling again.

    A worker can stop heartbeating without releasing -- SIGKILL, a severed
    network -- and then lease_expires eventually catches it. But a worker that
    set a very long TTL and then wedged would otherwise hold the case until that
    TTL, which can be far beyond seven days.
    """
    db.lease(conn, "w1", count=1, lease_seconds=90 * DAY, now=1_000_000)
    # Not expired -- the TTL runs for 90 days -- but older than the cap.
    later = 1_000_000 + 7 * DAY + 60
    got = db.lease(conn, "w2", count=1, now=later)
    assert [g.case_id for g in got] == ["A"], "a stale lease blocked the pool"
    row = db.get_case(conn, "A")
    assert row["lease_worker"] == "w2"
    assert row["leased_at"] == later


def test_a_reclaimed_case_spends_an_attempt(conn):
    """It is a retry like any other, so a case that wedges repeatedly quarantines."""
    db.lease(conn, "w1", count=1, now=1_000_000)
    assert db.get_case(conn, "A")["attempts"] == 1
    db.lease(conn, "w2", count=1, now=1_000_000 + 8 * DAY)
    assert db.get_case(conn, "A")["attempts"] == 2


def test_an_ordinary_case_is_untouched(conn):
    """Seven days is far beyond any real case; this must never fire normally."""
    got = db.lease(conn, "w1", count=1, now=1_000_000)[0]
    # A long solve: three wall-hours, heartbeating throughout.
    for t in range(1_000_000, 1_000_000 + 3 * 3600, 300):
        assert db.heartbeat(conn, got.lease_id, now=t) is True
    assert db.complete(conn, got.lease_id, "s3://b/A", case_id="A") is True
    row = db.get_case(conn, "A")
    assert row["state"] == "done" and row["leased_at"] is None


def test_the_cap_is_configurable():
    import os
    assert db.MAX_LEASE_AGE_SECONDS == 7 * DAY
    assert "CASEBROKER_MAX_LEASE_AGE" in open(
        os.path.join(os.path.dirname(db.__file__), "db.py")).read()


def test_a_lease_predating_the_column_starts_its_clock(tmp_path):
    """NULL means "unknown", not "not old".

    Cases leased before this column existed are the ones most likely to be
    stuck, so leaving them exempt would miss precisely the population the cap
    was added for. The first heartbeat stamps them instead.
    """
    import json
    import sqlite3

    path = str(tmp_path / "legacy.sqlite")
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA.replace("    leased_at      INTEGER,\n", ""))
    now = 1_000_000
    raw.execute(
        "INSERT INTO cases (case_id,spec,recipe,city_cluster,lcz,split,state,"
        "lease_id,lease_worker,lease_expires,attempts,max_attempts,created_at,updated_at)"
        " VALUES ('OLD',?,'r','x','LCZ6','train','leased','L','w1',?,1,3,?,?)",
        (json.dumps({"lat": 33.75, "lon": -84.39}), now + 10 ** 7, now, now))
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert db.get_case(conn, "OLD")["leased_at"] is None

    # First contact stamps it and is allowed through.
    assert db.heartbeat(conn, "L", now=now) is True
    assert db.get_case(conn, "OLD")["leased_at"] == now

    # From then on the cap applies normally.
    assert db.heartbeat(conn, "L", now=now + 8 * DAY) is False
    assert db.get_case(conn, "OLD")["state"] == "pending"

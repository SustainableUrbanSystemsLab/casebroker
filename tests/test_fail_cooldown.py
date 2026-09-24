"""A case a machine just failed goes to a different machine first.

On 2026-09-23 every quarantine in the campaign had spent its three attempts on
ONE machine within minutes -- v2-00ed64225d4979d7 on cod-359-40-2 at 18:37,
18:44 and 18:51, v2-0057457805ddf4bf three times on cod-359-38 inside a minute
-- because the worker that failed a case asked for work at once and the case was
first in the queue. For FAIL_COOLDOWN_SECONDS after a failure, no worker on that
host is handed the case again, fresh or as a resume; any other machine is.
"""

from __future__ import annotations

import pytest

from casebroker import db

HOUR = 3600
T0 = 1_000_000


def case(case_id, priority):
    return {"case_id": case_id, "spec": {}, "recipe": "r", "city_cluster": "x",
            "split": "train", "priority": priority, "max_attempts": 3}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.sqlite"))
    db.add_cases(c, [case("A", 10), case("B", 20)])
    return c


def take(conn, worker, host, now, **kw):
    got = db.lease(conn, worker, count=1, now=now, host=host, **kw)
    return got[0] if got else None


def test_the_machine_that_failed_a_case_gets_the_next_one_and_another_machine_gets_it(conn):
    a = take(conn, "m1", "box1", T0)
    assert a.case_id == "A"
    assert db.fail(conn, a.lease_id, "solve exited 1: FPE", retryable=True, now=T0 + 60)
    nxt = take(conn, "m1", "box1", T0 + 61)
    assert nxt.case_id == "B", "the failing machine asks at once, and must not get A straight back"
    other = take(conn, "m2", "box2", T0 + 120)
    assert other.case_id == "A" and other.attempt == 2


def test_every_worker_on_the_failing_host_waits(conn):
    # cod-358-21 and cod-358-21-2: one machine, one build, one way to fail.
    a = take(conn, "m1", "box1", T0)
    assert db.fail(conn, a.lease_id, "solve exited 1: FPE", retryable=True, now=T0 + 60)
    assert take(conn, "m1-2", "box1", T0 + 61).case_id == "B"
    assert take(conn, "m3", "box3", T0 + 62).case_id == "A"


def test_a_one_machine_fleet_still_retries_after_the_cooldown(conn):
    a = take(conn, "m1", "box1", T0)
    assert db.fail(conn, a.lease_id, "solve exited 1: FPE", retryable=True, now=T0 + 60)
    assert take(conn, "m1", "box1", T0 + 61).case_id == "B"
    assert take(conn, "m1", "box1", T0 + 120) is None, "A is cooling down, B is held"
    later = take(conn, "m1", "box1", T0 + 60 + db.FAIL_COOLDOWN_SECONDS + 1)
    assert later.case_id == "A" and later.attempt == 2


def test_asking_to_resume_a_case_this_machine_just_failed_does_not_bring_it_back(conn):
    # v2-0057457805ddf4bf: the node kept its half-deleted case on disk and asked
    # for it back twice in the same minute, meeting the same leftovers each time.
    a = take(conn, "m1", "box1", T0)
    assert db.fail(conn, a.lease_id, "The process cannot access the file 'case_000'",
                   retryable=True, now=T0 + 60)
    got = take(conn, "m1", "box1", T0 + 61, resume_case_ids=["A"])
    assert got.case_id == "B"
    assert db.get_case(conn, "A")["state"] == "pending"


def test_a_refunded_timeout_still_goes_back_to_its_own_machine(conn):
    # A refund is 'released', not 'failed': that machine holds the checkpoint.
    a = take(conn, "m1", "box1", T0)
    assert db.heartbeat(conn, a.lease_id, detail="solve 17/32 dirs", now=T0 + 23 * HOUR)
    assert db.fail(conn, a.lease_id, "case exceeded 86400s and was stopped",
                   retryable=True, now=T0 + 24 * HOUR)
    back = take(conn, "m1", "box1", T0 + 24 * HOUR + 1, resume_case_ids=["A"])
    assert back.case_id == "A"


def test_three_attempts_are_three_machines(tmp_path):
    c = db.connect(str(tmp_path / "one.sqlite"))
    db.add_cases(c, [case("A", 10)])
    t = T0
    failed_so_far = []
    for worker, host in (("m1", "box1"), ("m2", "box2"), ("m3", "box3")):
        # Every machine that already failed it asks first, and is turned away.
        for earlier, earlier_host in failed_so_far:
            assert take(c, earlier, earlier_host, t) is None, earlier
        got = take(c, worker, host, t)
        assert got is not None and got.case_id == "A", worker
        assert db.fail(c, got.lease_id, "solve exited 1: FPE", retryable=True, now=t + 60)
        failed_so_far.append((worker, host))
        t += 120
    r = db.get_case(c, "A")
    assert (r["state"], r["attempts"]) == ("quarantined", 3)
    workers = [e["worker_id"] for e in c.execute(
        "SELECT worker_id FROM events WHERE case_id='A' AND event='leased' ORDER BY id").fetchall()]
    assert workers == ["m1", "m2", "m3"]

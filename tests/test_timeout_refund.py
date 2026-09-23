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


# What a node sends when ONE step runs out of budget -- verbatim from production
# (v2-003a9149ad953d85, 2026-09-23) for the pre-00dfaba2 builds, and the later
# builds' wording, which adds the clocks' verdict.
OLD_STEP_TIMEOUT = (
    "solve exited 1: Collecting uniform files\n\nEnd\n\n\n"
    "=== case_000: the solver reached endTime without printing that the solution converged"
    " - escalating to the 'default' numerics path ===\n"
    "--- step 3/3: 01_foamRun\n"
    "case_000 (attempt 2/3) failed at step 01_foamRun: Batch 'C:\\Users\\n\\cases\\v2-x\\case_000"
    "\\Run_headless.bat' timed out after 240 minutes.\n"
    "case_000: the solver log has no End line - nothing finished to judge - not retried;"
    " a harder numerics path cannot fix it.")
NEW_STEP_TIMEOUT = (
    "solve exited 1: case_000 (attempt 1/5) failed at step 01_foamRun: Batch 'C:\\n\\case_000"
    "\\Run_headless.bat' timed out after 720 minutes. the solver's own clock reached 700 min of the"
    " 720 min this step allows, so it RAN the whole time: 4 rank(s) are too slow for this direction"
    " inside that budget -- give the case more cores, or raise --step-timeout. The case itself is fine")
CONTAINER_STEP_TIMEOUT = "case_003 failed at step 01_foamRun: Container command timed out after 720 minutes."


def test_an_old_builds_240_minute_step_kill_is_refunded(conn):
    # The production shape: the line said "starting" once, rung 1 ran 1.7 h to
    # endTime, rung 2 was killed at 240 min -- 5.7 h after the line last changed.
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(60, "solve 0/32 dirs · starting")])
    assert db.fail(conn, got.lease_id, OLD_STEP_TIMEOUT, retryable=True, now=t0 + int(5.7 * HOUR))
    r = row(conn)
    assert (r["state"], r["attempts"]) == ("pending", 0)
    assert "timed out after 240 minutes" in r["last_error"]


def test_a_step_that_ran_its_whole_budget_is_judged_on_the_line_before_it(conn):
    # A build that reports once per direction: the line changed when case_000
    # began, then a 30-min rung and a full 720-min step. 12.5 h is outside the
    # 12 h window on its own; the step's own budget is what keeps it inside.
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(60, "solve 0/32 dirs · starting")])
    assert db.fail(conn, got.lease_id, NEW_STEP_TIMEOUT, retryable=True, now=t0 + 60 + int(12.5 * HOUR))
    assert (row(conn)["state"], row(conn)["attempts"]) == ("pending", 0)


def test_a_container_step_kill_is_refunded(conn):
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(HOUR, "solve 3/32 dirs")])
    assert db.fail(conn, got.lease_id, CONTAINER_STEP_TIMEOUT, retryable=True, now=t0 + 13 * HOUR)
    assert row(conn)["attempts"] == 0


def test_a_step_kill_long_after_the_line_stopped_is_charged(conn):
    # 30 h without a new line is more than the 4 h step plus the 12 h window.
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(60, "solve 3/32 dirs")])
    assert db.fail(conn, got.lease_id, OLD_STEP_TIMEOUT, retryable=True, now=t0 + 30 * HOUR)
    assert row(conn)["attempts"] == 1


def test_a_timeout_that_is_not_a_step_budget_is_charged(conn):
    t0 = 1_000_000
    got = lease_and_progress(conn, t0, [(HOUR, "solve 3/32 dirs")])
    assert db.fail(conn, got.lease_id, "Error: pulling openfoam:12 timed out after 10 minutes.",
                   retryable=True, now=t0 + 2 * HOUR)
    assert row(conn)["attempts"] == 1


def test_an_old_build_looping_on_one_case_is_ended_by_the_cap(conn):
    # An old build fails every v4 case the same way: "starting", then a 240-min
    # kill. Each lease's first line is its own evidence (it is recorded even
    # when it repeats the last attempt's), so the cap is what ends the loop:
    # three refunds, then charged attempts, then quarantine -- never forever.
    t = 1_000_000
    states = []
    for _ in range(db.TIMEOUT_REFUNDS_MAX + 4):
        got = db.lease(conn, "w1", count=1, now=t)
        if not got:
            break
        assert db.heartbeat(conn, got[0].lease_id, detail="solve 0/32 dirs · starting", now=t + 60)
        db.fail(conn, got[0].lease_id, OLD_STEP_TIMEOUT, retryable=True, now=t + int(5.7 * HOUR))
        states.append((row(conn)["state"], row(conn)["attempts"]))
        t += 6 * HOUR
    assert states == [("pending", 0)] * db.TIMEOUT_REFUNDS_MAX + [
        ("pending", 1), ("pending", 2), ("quarantined", 3)]


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

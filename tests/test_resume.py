"""Same-worker resume and the progress line on a case.

Both exist because of the same week of running real cases: a solve cut by an
8 h walltime restarted from zero (no checkpoint, and no way to ask for its own
case back), and "how far along is it" could only be answered by ssh-ing to the
node and grepping a log. A restarted worker now asks for the cases it has a
checkpoint for, and every heartbeat carries one line the dashboard can show.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402


def make_db(tmp_path, n=6):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    rows = []
    for i in range(n):
        lat, lon = 34.0 + i * 0.01, -84.0
        rows.append({"case_id": ids.case_id(lat, lon, "r1"),
                     "spec": {"lat": lat, "lon": lon}, "recipe": "r1",
                     "city_cluster": "city%d" % (i % 2), "lcz": "LCZ6",
                     "split": ids.split_for("city%d" % (i % 2))})
    db.add_cases(conn, rows)
    return conn


def events(conn, case_id):
    """Lease-lifecycle events only -- add_cases logs a 'created' for every row,
    which is not what these tests are about."""
    return [r["event"] for r in conn.execute(
        "SELECT event FROM events WHERE case_id=? AND event != 'created' ORDER BY id",
        (case_id,))]


def test_a_restarted_worker_gets_its_own_case_back_without_spending_an_attempt(tmp_path):
    conn = make_db(tmp_path)
    first = db.lease(conn, "ws-01", now=1000)[0]
    assert first.attempt == 1

    # The worker died and came back while its lease is still live. It asks for
    # the case by id, gets it ahead of everything else, and the attempt count
    # does not move: this is a continuation, not a retry.
    again = db.lease(conn, "ws-01", resume_case_ids=[first.case_id], now=1100)
    assert [g.case_id for g in again] == [first.case_id]
    assert again[0].attempt == 1
    assert again[0].lease_id != first.lease_id
    assert events(conn, first.case_id) == ["leased", "resumed"]

    # The previous process's lease is dead: a zombie heartbeat gets "stop".
    assert db.heartbeat(conn, first.lease_id, now=1200) is False
    assert db.heartbeat(conn, again[0].lease_id, now=1200) is True


def test_a_case_is_never_resumed_by_a_different_worker(tmp_path):
    conn = make_db(tmp_path)
    mine = db.lease(conn, "ws-01", now=1000)[0]
    other = db.lease(conn, "ws-02", resume_case_ids=[mine.case_id], now=1100)
    # ws-02 still gets work -- just not that case, whose checkpoint lives on ws-01.
    assert len(other) == 1
    assert other[0].case_id != mine.case_id
    row = db.get_case(conn, mine.case_id)
    assert row["lease_worker"] == "ws-01"


def test_resume_ids_come_first_but_an_ordinary_pending_case_still_costs_an_attempt(tmp_path):
    conn = make_db(tmp_path)
    ordered = [r["case_id"] for r in conn.execute(
        "SELECT case_id FROM cases ORDER BY priority, case_id")]
    last = ordered[-1]
    # Never leased, but the worker has a checkpoint for it (say, an earlier
    # release refunded the attempt). It jumps the priority order, and since it
    # is a fresh claim it is attempt 1 like any other.
    got = db.lease(conn, "ws-01", resume_case_ids=[last], now=1000)
    assert [g.case_id for g in got] == [last]
    assert got[0].attempt == 1
    assert events(conn, last) == ["resumed"]


def test_unknown_or_done_resume_ids_are_ignored_and_the_rest_of_the_count_is_filled(tmp_path):
    conn = make_db(tmp_path, n=3)
    got = db.lease(conn, "ws-01", count=2, resume_case_ids=["nope", "also-nope"], now=1000)
    assert len(got) == 2


def test_the_latest_progress_line_rides_on_the_case_row(tmp_path):
    conn = make_db(tmp_path, n=2)
    g = db.lease(conn, "ws-01", now=1000)[0]
    assert db.get_case(conn, g.case_id)["last_progress"] is None

    db.heartbeat(conn, g.lease_id, detail="case_270 [0/8 dirs] iter 12 p=3.0e-01", now=1300)
    db.heartbeat(conn, g.lease_id, detail="case_270 [0/8 dirs] iter 412 p=3.2e-05", now=1600)

    one = db.get_case(conn, g.case_id)
    assert one["last_progress"] == "case_270 [0/8 dirs] iter 412 p=3.2e-05"
    assert one["last_progress_at"] == 1600

    page = db.list_cases(conn, state="leased")
    assert page["cases"][0]["case_id"] == g.case_id
    assert page["cases"][0]["last_progress"].endswith("iter 412 p=3.2e-05")
    # A plain "alive" heartbeat with no detail leaves the line alone.
    db.heartbeat(conn, g.lease_id, now=1900)
    assert db.get_case(conn, g.case_id)["last_progress_at"] == 1600

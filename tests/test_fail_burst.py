"""A machine that fails case after case is drained, and what it charged can be refunded.

COD-358-21's disk filled on 2026-09-26. Its node went on leasing, a case every
four seconds, and failed each with "There is not enough space on the disk": 663
cases in 38 minutes, each charged an attempt. Nothing stopped it -- the node
could not tell a full disk from a broken site, and the broker never looked at
the rate -- and nothing could undo it, because reopen reached only QUARANTINED
cases and these were all still pending at 1 of 3.

Two halves are pinned here. The broker drains a worker that fails
db.FAIL_BURST_CASES different cases within db.FAIL_BURST_SECONDS (5 in 10 min),
which works for every node build in the field. And reopen takes
``include_pending``, so the attempts a broken machine charged can be given back
without waiting for the cases to be quarantined first. Each has its controls:
a machine failing at a healthy rate must keep working, and a real failure must
keep its charge.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402

T0 = 1_000_000
DISK_FULL = "There is not enough space on the disk. : 'C:\\work\\v2-0003\\constant\\polyMesh\\points'"


def make_db(path, splits):
    conn = db.connect(str(path))
    db.add_cases(conn, [{
        "case_id": ids.case_id(34.0 + i * 0.01, -84.0, "r1"),
        "spec": {"lat": 34.0 + i * 0.01, "lon": -84.0, "dirs": [0]},
        "recipe": "r1", "city_cluster": "city0", "lcz": "LCZ6", "split": split,
    } for i, split in enumerate(splits)])
    return conn


def fail_next(conn, worker_id, error, now, split="train"):
    """Lease the next case as `worker_id` and fail it four seconds later, as the
    node on the full disk did."""
    leased = db.lease(conn, worker_id, 1, 900, splits=[split], now=now)
    assert leased, "expected a case to lease"
    assert db.fail(conn, leased[0].lease_id, error, retryable=True, now=now + 4)
    return leased[0].case_id


def drained(conn, worker_id):
    row = dict(conn.execute("SELECT drain, drain_reason FROM workers WHERE worker_id=?",
                            (worker_id,)).fetchone())
    return bool(row["drain"]), row["drain_reason"]


def case(conn, case_id, *cols):
    return dict(conn.execute("SELECT " + ", ".join(cols) + " FROM cases WHERE case_id=?",
                             (case_id,)).fetchone())


# -- the drain -----------------------------------------------------------------

def test_five_cases_failed_within_ten_minutes_drain_the_worker(tmp_path):
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 8)
    for i in range(5):
        fail_next(conn, "cod-358-21", DISK_FULL, now=T0 + 60 * i)

    drain, reason = drained(conn, "cod-358-21")
    assert drain, "the fifth failure in ten minutes is the machine, not five bad sites"
    # The node prints this, and the dashboard's Drained badge shows it: it must
    # say who drained it and what the machine was failing with.
    assert "drained by the broker" in reason and "5 cases failed" in reason
    assert "not enough space on the disk" in reason
    trail = [dict(r)["detail"] for r in conn.execute("SELECT detail FROM events WHERE event='drain'")]
    assert trail and trail[0].startswith("cod-358-21: drained by the broker")

    # No sixth case for it; the rest of the fleet goes on as before.
    assert db.lease(conn, "cod-358-21", 1, 900, now=T0 + 400) == []
    assert db.lease(conn, "ws-02", 1, 900, now=T0 + 400)


def test_four_failures_are_not_a_burst(tmp_path):
    """The control: a healthy node fails a few cases a day, and one bad stretch
    of sites must not take it out of the fleet."""
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 6)
    for i in range(4):
        fail_next(conn, "ws-01", "snappyHexMesh produced no cells", now=T0 + 60 * i)

    assert drained(conn, "ws-01") == (False, None)
    assert db.lease(conn, "ws-01", 1, 900, now=T0 + 300)


def test_the_same_five_failures_spread_over_an_hour_are_not_a_burst(tmp_path):
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 6)
    for i in range(5):
        fail_next(conn, "ws-01", "snappyHexMesh produced no cells", now=T0 + 900 * i)

    assert drained(conn, "ws-01") == (False, None)


def test_the_count_is_per_worker(tmp_path):
    """Two nodes failing three cases each are two machines with a bad stretch, not
    one broken machine."""
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 8)
    for i in range(3):
        fail_next(conn, "ws-01", "snappyHexMesh produced no cells", now=T0 + 60 * i)
        fail_next(conn, "ws-02", "snappyHexMesh produced no cells", now=T0 + 60 * i + 30)

    assert drained(conn, "ws-01")[0] is False and drained(conn, "ws-02")[0] is False


def test_quarantines_found_at_lease_time_are_not_the_holder_s_failures(tmp_path):
    """A lease that ran out on a spent case is quarantined by whoever asks next,
    and named after the worker that held it -- which has usually just died. Five
    of those are not five failures by it, and counting them would drain it the
    moment it came back and failed one case of its own."""
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 7)
    mine = db.lease(conn, "gone", 1, 900, now=T0 - 100)[0]
    spent = [dict(r)["case_id"] for r in conn.execute(
        "SELECT case_id FROM cases WHERE state='pending' ORDER BY case_id LIMIT 5")]
    conn.executemany(
        "UPDATE cases SET state='leased', lease_id=?, lease_worker='gone', lease_expires=?,"
        " leased_at=?, attempts=max_attempts WHERE case_id=?",
        [("L-" + cid, T0 - 1, T0 - 1000, cid) for cid in spent])

    db.lease(conn, "finder", 10, 900, now=T0)
    assert {case(conn, cid, "state")["state"] for cid in spent} == {"quarantined"}
    db.fail(conn, mine.lease_id, "snappyHexMesh produced no cells", now=T0 + 10)

    assert drained(conn, "gone") == (False, None)


def test_an_undrain_starts_the_count_over(tmp_path):
    """An operator who fixed the machine and undrained it must not see it drained
    again by its first failure, on the strength of the burst they just dealt with."""
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 8)
    for i in range(5):
        fail_next(conn, "ws-01", DISK_FULL, now=T0 + 30 * i)
    assert drained(conn, "ws-01")[0]

    db.set_worker_drain(conn, "ws-01", False, by="operator", now=T0 + 200)
    fail_next(conn, "ws-01", "snappyHexMesh produced no cells", now=T0 + 260)

    assert drained(conn, "ws-01") == (False, None)


def test_a_drain_an_operator_set_is_left_as_they_wrote_it(tmp_path):
    """The burst rule drains a worker that is not drained; it does not overwrite
    the reason an operator gave."""
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 8)
    fail_next(conn, "ws-01", DISK_FULL, now=T0)
    db.set_worker_drain(conn, "ws-01", True, "new GPU", by="operator", now=T0 + 10)
    # A draining worker can still fail the case it holds, and a node restarted
    # onto the old one resumes it; forge four more failures by it directly.
    for i in range(4):
        cid = dict(conn.execute("SELECT case_id FROM cases WHERE state='pending' AND attempts=0"
                                " ORDER BY case_id LIMIT 1").fetchone())["case_id"]
        conn.execute("UPDATE cases SET state='leased', lease_id=?, lease_worker='ws-01',"
                     " lease_expires=?, leased_at=?, attempts=1 WHERE case_id=?",
                     ("L-%d" % i, T0 + 9000, T0 + 20 + i, cid))
        db.fail(conn, "L-%d" % i, DISK_FULL, now=T0 + 30 + i)

    assert drained(conn, "ws-01") == (True, "new GPU")


# -- the refund ----------------------------------------------------------------

def test_include_pending_refunds_what_a_broken_machine_charged(tmp_path):
    conn = make_db(tmp_path / "b.sqlite", ["train"] * 3 + ["test"])
    real = fail_next(conn, "ws-02", "snappyHexMesh produced no cells", now=T0, split="test")
    charged = [fail_next(conn, "cod-358-21", DISK_FULL, now=T0 + 60 * i) for i in range(3)]

    # What the 09-26 cascade left: nothing quarantined, so reopen alone reached none.
    assert db.reopen_cases(conn, error_contains="not enough space")["matched"] == 0

    peek = db.reopen_cases(conn, error_contains="not enough space", include_pending=True)
    assert (peek["matched"], peek["reopened"]) == (3, 0)
    assert {e["state"] for e in peek["examples"]} == {"pending"}

    out = db.reopen_cases(conn, error_contains="not enough space", include_pending=True,
                          dry_run=False)

    assert out["reopened"] == 3
    for cid in charged:
        assert case(conn, cid, "state", "attempts", "last_error") == \
            {"state": "pending", "attempts": 0, "last_error": None}
    kept = case(conn, real, "attempts", "last_error")
    assert kept["attempts"] == 1 and "snappyHexMesh" in kept["last_error"], \
        "a real failure keeps its charge"
    trail = [dict(r)["event"] for r in conn.execute(
        "SELECT event FROM events WHERE case_id=? ORDER BY id", (charged[0],))]
    assert trail[-1] == "reopened", "the refund stays auditable"


def test_a_case_running_again_keeps_its_attempt(tmp_path):
    """A case another machine has leased since is running, and its attempt is its own."""
    conn = make_db(tmp_path / "b.sqlite", ["train"])
    cid = fail_next(conn, "cod-358-21", DISK_FULL, now=T0)
    again = db.lease(conn, "ws-02", 1, 900, now=T0 + 60)[0]

    out = db.reopen_cases(conn, error_contains="not enough space", include_pending=True,
                          dry_run=False)

    assert (out["matched"], out["reopened"]) == (0, 0)
    assert case(conn, cid, "state", "attempts", "lease_id") == \
        {"state": "leased", "attempts": 2, "lease_id": again.lease_id}


class _Counting:
    """The connection, counting its statements: each is a round trip on Postgres."""

    def __init__(self, conn):
        self.conn, self.n = conn, 0

    def execute(self, *a):
        self.n += 1
        return self.conn.execute(*a)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _refund_statements(tmp_path, n_cases, monkeypatch):
    monkeypatch.setattr(db, "FAIL_BURST_CASES", 0)      # one machine fails them all
    conn = make_db(tmp_path / ("b%d.sqlite" % n_cases), ["train"] * n_cases)
    for i in range(n_cases):
        fail_next(conn, "cod-358-21", DISK_FULL, now=T0 + 60 * i)
    counting = _Counting(conn)
    out = db.reopen_cases(counting, error_contains="not enough space", include_pending=True,
                          dry_run=False, limit=1000)
    assert out["reopened"] == n_cases
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE event='reopened'").fetchone()["n"] == n_cases
    assert conn.execute("SELECT COUNT(*) AS n FROM cases WHERE attempts=0 AND last_error IS NULL"
                        " AND state='pending'").fetchone()["n"] == n_cases
    return counting.n


def test_a_large_refund_is_a_few_statements_not_two_per_case(tmp_path, monkeypatch):
    """Every statement is a round trip to Supabase -- about 80 ms from production --
    under the lock every lease, heartbeat and /healthz ping waits on. Refunding
    COD-358-21's 663 cases took one UPDATE and one INSERT per case, ~1,300 round
    trips: the broker answered nothing for well over a minute and came back 502
    with nothing applied (2026-09-26). The count must not grow with the cases."""
    assert _refund_statements(tmp_path, 40, monkeypatch) == _refund_statements(tmp_path, 3, monkeypatch)


def test_include_pending_will_not_forgive_everything(tmp_path):
    """Unselected, it would refund every real failure in the queue as well."""
    conn = make_db(tmp_path / "b.sqlite", ["train"])
    fail_next(conn, "ws-01", "snappyHexMesh produced no cells", now=T0)

    with pytest.raises(ValueError):
        db.reopen_cases(conn, include_pending=True, dry_run=False)


def test_the_endpoint_passes_include_pending_through(tmp_path):
    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    path = tmp_path / "api.sqlite"
    app = create_app(str(path), ["w"], ["r"])
    auth = {"Authorization": "Bearer w"}
    with TestClient(app) as c:
        conn = make_db(path, ["train"] * 2)
        cid = fail_next(conn, "cod-358-21", DISK_FULL, now=T0)

        assert c.post("/v1/cases/reopen", headers=auth,
                      params={"include_pending": "true"}).status_code == 422
        peek = c.post("/v1/cases/reopen", headers=auth, params={
            "include_pending": "true", "error_contains": "not enough space"}).json()
        assert (peek["matched"], peek["dry_run"]) == (1, True)
        done = c.post("/v1/cases/reopen", headers=auth, params={
            "include_pending": "true", "error_contains": "not enough space",
            "dry_run": "false"}).json()
        assert done["reopened"] == 1
        assert case(conn, cid, "attempts")["attempts"] == 0
        # Without it, the endpoint answers as before: quarantined cases only.
        assert c.post("/v1/cases/reopen", headers=auth, params={
            "error_contains": "snappy"}).json()["matched"] == 0

"""Lease-semantics tests.

Every test here corresponds to something that WILL happen during a 40,000-solve
campaign across ICE, Phoenix and the lab workstation: two workers asking at the
same moment, a Phoenix job preempted mid-solve, a node dying without warning, a
case whose geometry is broken and can never succeed, and the same case list
being re-imported when the dataset grows. None of them is exotic at this scale.
"""

from __future__ import annotations

import sys
import pathlib
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402


def make_db(tmp_path, n=10, cities=3):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    rows = []
    for i in range(n):
        lat, lon = 34.0 + i * 0.01, -84.0
        city = "city%d" % (i % cities)
        rows.append({
            "case_id": ids.case_id(lat, lon, "r1"),
            "spec": {"lat": lat, "lon": lon, "dirs": [0, 45]},
            "recipe": "r1",
            "city_cluster": city,
            "lcz": "LCZ6",
            "split": ids.split_for(city),
        })
    db.add_cases(conn, rows)
    return conn


def test_add_cases_is_idempotent(tmp_path):
    conn = make_db(tmp_path, 5)
    again = db.add_cases(conn, [{
        "case_id": ids.case_id(34.0, -84.0, "r1"),
        "spec": {"lat": 34.0, "lon": -84.0}, "recipe": "r1",
        "city_cluster": "city0", "split": "train",
    }])
    assert again == {"added": 0, "skipped": 1}
    assert db.status(conn)["by_state"]["pending"] == 5


def test_a_case_is_never_handed_to_two_workers(tmp_path):
    """The whole point of the broker. Ten cases, eight threads racing."""
    conn_path = str(tmp_path / "b.sqlite")
    make_db(tmp_path, 10).close()

    seen: list[str] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def grab(k):
        try:
            c = db.connect(conn_path)
            got = db.lease(c, "w%d" % k, count=3)
            with lock:
                seen.extend(g.case_id for g in got)
            c.close()
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=grab, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(seen) == len(set(seen)), "a case was leased twice: %r" % (
        sorted({c for c in seen if seen.count(c) > 1}),)
    assert len(seen) == 10, "every case should have been handed out exactly once"


def test_expired_lease_returns_to_the_pool(tmp_path):
    """A node that dies without warning. Nobody reports anything; the case must
    come back anyway."""
    conn = make_db(tmp_path, 1)
    t0 = 1_000_000
    first = db.lease(conn, "dead-worker", lease_seconds=60, now=t0)
    assert len(first) == 1
    # Still held while the lease is live.
    assert db.lease(conn, "other", now=t0 + 30) == []
    # Reclaimed once it lapses.
    second = db.lease(conn, "other", now=t0 + 61)
    assert len(second) == 1
    assert second[0].case_id == first[0].case_id
    assert second[0].attempt == 2, "a reclaim consumes an attempt"


def test_release_refunds_the_attempt(tmp_path):
    """Phoenix preemption. The case was fine; the worker just lost its node, so
    the attempt must not count against max_attempts."""
    conn = make_db(tmp_path, 1)
    lease = db.lease(conn, "phoenix-w1")[0]
    assert lease.attempt == 1
    assert db.release(conn, lease.lease_id, "SIGTERM: preempted")
    again = db.lease(conn, "ice-w1")[0]
    assert again.attempt == 1, "released case must not burn a retry"


def test_retryable_failure_requeues_then_quarantines(tmp_path):
    conn = make_db(tmp_path, 1)
    for expected in (1, 2, 3):
        lease = db.lease(conn, "w")[0]
        assert lease.attempt == expected
        assert db.fail(conn, lease.lease_id, "solver diverged", retryable=True)
    # Retries are spent; the next claim parks it instead of looping forever.
    assert db.lease(conn, "w") == []
    assert db.status(conn)["by_state"] == {"quarantined": 1}


def test_fatal_failure_quarantines_immediately(tmp_path):
    conn = make_db(tmp_path, 1)
    lease = db.lease(conn, "w")[0]
    assert db.fail(conn, lease.lease_id, "geometry not watertight", retryable=False)
    assert db.status(conn)["by_state"] == {"quarantined": 1}


def test_a_stale_lease_cannot_report_results(tmp_path):
    """The dangerous one: a preempted worker wakes up, finishes, and tries to
    report a case that has since been re-leased and possibly already completed by
    someone else. Its write must be rejected, not silently applied."""
    conn = make_db(tmp_path, 1)
    t0 = 1_000_000
    zombie = db.lease(conn, "zombie", lease_seconds=60, now=t0)[0]
    fresh = db.lease(conn, "fresh", now=t0 + 61)[0]
    assert fresh.case_id == zombie.case_id

    assert db.complete(conn, zombie.lease_id, "file:///zombie") is False
    assert db.heartbeat(conn, zombie.lease_id) is False
    assert db.fail(conn, zombie.lease_id, "late") is False
    assert db.release(conn, zombie.lease_id) is False

    assert db.complete(conn, fresh.lease_id, "file:///good", nbytes=42)
    row = conn.execute("SELECT state, result_uri FROM cases").fetchone()
    assert (row["state"], row["result_uri"]) == ("done", "file:///good")


def test_heartbeat_extends_the_lease(tmp_path):
    conn = make_db(tmp_path, 1)
    t0 = 1_000_000
    lease = db.lease(conn, "w", lease_seconds=60, now=t0)[0]
    assert db.heartbeat(conn, lease.lease_id, lease_seconds=600, now=t0 + 50)
    # Would have lapsed under the original 60 s lease; the renewal holds it.
    assert db.lease(conn, "thief", now=t0 + 100) == []


def test_split_filter_restricts_what_a_worker_gets(tmp_path):
    # 30 distinct city clusters, so all three splits are actually represented --
    # and the target split is read back from the fixture rather than assumed,
    # since which city hashes to which split is not ours to predict.
    conn = make_db(tmp_path, 30, cities=30)
    present = {r["split"]: r["n"] for r in conn.execute(
        "SELECT split, COUNT(*) n FROM cases GROUP BY split")}
    assert set(present) == {"train", "val", "test"}, present

    got = db.lease(conn, "w", count=100, splits=["test"])
    assert len(got) == present["test"]
    placeholders = ",".join("?" for _ in got)
    rows = conn.execute(
        "SELECT DISTINCT split FROM cases WHERE case_id IN (%s)" % placeholders,
        [g.case_id for g in got]).fetchall()
    assert [r["split"] for r in rows] == ["test"]

    # And the untouched splits are still available to the next worker.
    rest = db.lease(conn, "w2", count=100)
    assert len(rest) == present["train"] + present["val"]


def test_status_reports_no_eta_before_anything_finishes(tmp_path):
    conn = make_db(tmp_path, 4)
    st = db.status(conn)
    assert st["remaining"] == 4
    assert st["eta_days"] is None, "an ETA with no completions would be invented"


def test_events_record_every_transition(tmp_path):
    conn = make_db(tmp_path, 1)
    lease = db.lease(conn, "w")[0]
    db.fail(conn, lease.lease_id, "boom")
    lease2 = db.lease(conn, "w")[0]
    db.complete(conn, lease2.lease_id, "file:///r")
    events = [r["event"] for r in conn.execute("SELECT event FROM events ORDER BY id")]
    assert events == ["created", "leased", "failed", "leased", "done"]


def test_lease_records_which_machine_is_working_the_case(tmp_path):
    """"What machine produced this" has to be answerable from the workers table,
    not just guessed from a worker_id string."""
    conn = make_db(tmp_path, 1)
    db.lease(conn, "phoenix-w1", host="atl1-1-02-005-11-1", cluster="phoenix-slurm")
    row = conn.execute("SELECT host, cluster FROM workers WHERE worker_id=?",
                       ("phoenix-w1",)).fetchone()
    assert (row["host"], row["cluster"]) == ("atl1-1-02-005-11-1", "phoenix-slurm")


def test_lease_refreshes_host_and_cluster_on_a_later_call(tmp_path):
    """A worker_id that resumes on a different machine (a restarted SLURM job,
    say) must not leave the ops UI pointing at where it USED to run."""
    conn = make_db(tmp_path, 2)
    db.lease(conn, "w1", host="node-a", cluster="ice")
    db.lease(conn, "w1", host="node-b", cluster="ice")
    row = conn.execute("SELECT host FROM workers WHERE worker_id=?", ("w1",)).fetchone()
    assert row["host"] == "node-b"


def test_list_cases_pages_most_recently_touched_first(tmp_path):
    conn = make_db(tmp_path, 5, cities=5)
    all_ids = [r["case_id"] for r in
              conn.execute("SELECT case_id FROM cases ORDER BY case_id")]
    # add_cases stamps created_at/updated_at from the real wall clock, which a
    # synthetic t0 in the past would sort BEHIND -- rebase every row to a known
    # baseline first so the lease/complete calls below control the ordering.
    t0 = 1_000_000
    conn.execute("UPDATE cases SET created_at=?, updated_at=?", (t0, t0))
    # Touch two cases out of creation order, with explicit timestamps a second
    # apart, so "most recently touched" and "most recently created" disagree
    # in a way that would catch the wrong ORDER BY column.
    db.lease(conn, "w", count=1, now=t0 + 1)
    later = db.lease(conn, "w2", count=1, now=t0 + 2)[0]
    db.complete(conn, later.lease_id, "file:///x", now=t0 + 3)

    page = db.list_cases(conn, limit=2)
    assert page["total"] == 5
    assert len(page["cases"]) == 2
    assert page["cases"][0]["case_id"] == later.case_id, \
        "the just-completed case should sort first"

    rest = db.list_cases(conn, limit=2, offset=2)
    assert len(rest["cases"]) == 2
    seen = {c["case_id"] for c in page["cases"]} | {c["case_id"] for c in rest["cases"]}
    assert seen <= set(all_ids)


def test_list_cases_filters_by_state_and_split(tmp_path):
    conn = make_db(tmp_path, 6, cities=6)
    lease = db.lease(conn, "w")[0]
    db.complete(conn, lease.lease_id, "file:///done-one")

    done_only = db.list_cases(conn, state="done")
    assert done_only["total"] == 1
    assert done_only["cases"][0]["state"] == "done"

    pending_only = db.list_cases(conn, state="pending")
    assert pending_only["total"] == 5

    by_split = db.list_cases(conn, split="train")
    assert all(c["split"] == "train" for c in by_split["cases"])
    assert by_split["total"] == len(
        [r for r in conn.execute("SELECT 1 FROM cases WHERE split='train'")])


def test_list_cases_limit_is_clamped_not_trusted(tmp_path):
    """A dashboard bug (or a hostile client) asking for limit=100000 must not
    turn one page load into "SELECT * FROM 40,000 rows"."""
    conn = make_db(tmp_path, 3)
    page = db.list_cases(conn, limit=100000)
    assert page["limit"] <= 200
    page0 = db.list_cases(conn, limit=0)
    assert page0["limit"] >= 1

"""The Postgres engine, verified against a REAL database over the network.

Skipped entirely unless ``CASEBROKER_TEST_PG_DSN`` is set — these tests hit a real
Postgres instance (Supabase in practice) and are not meant to run in an ordinary
`pytest` invocation with no such database configured. That mirrors this project's
own rule for engine-gated tests elsewhere: a claim about a real external system is
only worth trusting once it has actually been run against that system.

Every test here opens its OWN connection per simulated worker, deliberately unlike
``test_db.py``'s SQLite tests (which share one process-local connection protected by
an in-process lock). Multiple real connections is the only way to exercise what
Postgres actually adds: ``FOR UPDATE SKIP LOCKED`` correctness ACROSS connections,
which a single shared connection can never touch regardless of how many threads
call into it.

All test data is prefixed with a per-run UUID and deleted in a module-scoped
teardown, because this may be the same database the real campaign uses -- it must
be left exactly as it was found, tables included but empty of test rows.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402

DSN = os.environ.get("CASEBROKER_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set CASEBROKER_TEST_PG_DSN to a real Postgres URL to run these")

RUN = uuid.uuid4().hex[:8]


def prefix(s: str) -> str:
    return f"pgtest-{RUN}-{s}"


@pytest.fixture(scope="module", autouse=True)
def cleanup_after_module():
    """Runs once per module; deletes every row this run created, whatever the
    outcome of the tests. Nothing else in the shared database is touched."""
    yield
    conn = db.connect(DSN)
    like = f"pgtest-{RUN}-%"
    conn.execute("DELETE FROM events WHERE case_id LIKE ? OR worker_id LIKE ?", (like, like))
    conn.execute("DELETE FROM cases WHERE case_id LIKE ?", (like,))
    conn.execute("DELETE FROM workers WHERE worker_id LIKE ?", (like,))


def fresh_conn():
    """A new, independent connection -- what a separate worker process/machine
    would actually have. Sharing one connection across "workers" would test
    nothing about cross-connection locking."""
    return db.connect(DSN)


def seed(n: int, tag: str) -> list[str]:
    conn = fresh_conn()
    rows = []
    ids_out = []
    for i in range(n):
        lat, lon = 34.0 + i * 0.001, -84.0
        cid = prefix(f"{tag}-{i}")
        ids_out.append(cid)
        rows.append({"case_id": cid, "spec": {"i": i}, "recipe": "pgtest",
                     "city_cluster": prefix(f"city{i % 5}"), "split": "train"})
    r = db.add_cases(conn, rows)
    assert r["added"] == n
    return ids_out


def test_connects_and_reports_a_real_engine():
    conn = fresh_conn()
    assert isinstance(conn, db.PgConnection)


def test_add_lease_complete_roundtrip_over_a_real_connection():
    cid = seed(1, "roundtrip")[0]
    conn = fresh_conn()
    leased = db.lease(conn, prefix("w1"))
    assert [l.case_id for l in leased] == [cid]
    assert db.heartbeat(conn, leased[0].lease_id, detail="ok")
    assert db.complete(conn, leased[0].lease_id, "file:///pgtest", metrics={"n": 1})
    st = db.status(conn)
    assert st["by_state"].get("done", 0) >= 1


def test_many_real_connections_racing_never_double_lease():
    """End-to-end: many HTTP-style callers hitting the real deployed system never
    see a case double-assigned. This does NOT test FOR UPDATE SKIP LOCKED in
    isolation -- db.lease() is @_locked, a module-level Python lock that
    serialises EVERY call within this one process regardless of which connection
    or thread calls it, so no amount of threading here can make two lease() calls'
    SQL genuinely overlap. (Confirmed the hard way: this test, even with a
    threading.Barrier forcing all twelve threads to start at once, still passed
    with FOR UPDATE SKIP LOCKED deliberately deleted from lease() -- because
    _LOCK had already serialised them before either one's SQL ran.)

    What it DOES prove, honestly: the whole stack -- HTTP-shaped concurrent
    calls, real network round-trips to Supabase, the _LOCK-serialised
    single-process deployment this campaign actually runs as -- produces no
    double-lease and no dropped case. See
    test_for_update_skip_locked_lets_a_second_transaction_see_a_different_row
    for the test that isolates the SQL clause itself, bypassing _LOCK entirely,
    which is what actually failed when the clause was removed.
    """
    n_cases, n_workers = 60, 12
    case_ids = seed(n_cases, "race")

    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()
    errors: list[Exception] = []
    barrier = threading.Barrier(n_workers, timeout=30)

    def worker(k: int):
        try:
            conn = fresh_conn()               # this worker's OWN connection,
            barrier.wait()                    # established BEFORE the synchronised start
            got = db.lease(conn, prefix(f"racer-{k}"), count=6)
            with lock:
                claimed.extend((g.case_id, k) for g in got)
        except Exception as e:                # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    claimed_ids = [c for c, _ in claimed]
    dupes = {c for c in claimed_ids if claimed_ids.count(c) > 1}
    assert not dupes, f"double-leased over real connections: {sorted(dupes)}"
    assert set(claimed_ids) == set(case_ids), (
        f"expected all {n_cases} claimed exactly once, got {len(set(claimed_ids))}")


def test_a_superseded_lease_cannot_write_across_real_connections():
    """A lease reclaimed by a SECOND real connection must reject writes from the
    FIRST -- the same guarantee test_db.py proves for SQLite, here proven across
    genuinely independent network connections and FOR UPDATE rather than a
    whole-database lock."""
    cid = seed(1, "superseded")[0]
    conn_a = fresh_conn()
    conn_b = fresh_conn()

    zombie = db.lease(conn_a, prefix("zombie"), lease_seconds=60)[0]
    # Force expiry as an operator/clock would see it, then let a DIFFERENT
    # connection reclaim the row.
    conn_a.execute("UPDATE cases SET lease_expires = 0 WHERE case_id = ?", (cid,))
    fresh = db.lease(conn_b, prefix("fresh"))[0]
    assert fresh.case_id == cid

    assert db.heartbeat(conn_a, zombie.lease_id) is False
    assert db.complete(conn_a, zombie.lease_id, "file:///zombie") is False
    assert db.fail(conn_a, zombie.lease_id, "late") is False
    assert db.release(conn_a, zombie.lease_id) is False

    assert db.complete(conn_b, fresh.lease_id, "file:///real", nbytes=1)
    row = conn_b.execute("SELECT state, result_uri FROM cases WHERE case_id=?",
                         (cid,)).fetchone()
    assert (row["state"], row["result_uri"]) == ("done", "file:///real")


def test_idempotent_add_cases_over_a_real_connection():
    rows = [{"case_id": prefix("idem-0"), "spec": {}, "recipe": "pgtest",
             "city_cluster": prefix("idem-city"), "split": "train"}]
    conn = fresh_conn()
    assert db.add_cases(conn, rows) == {"added": 1, "skipped": 0}
    assert db.add_cases(conn, rows) == {"added": 0, "skipped": 1}


def test_splits_still_assigned_by_city_against_postgres():
    conn = fresh_conn()
    rows = conn.execute(
        "SELECT city_cluster, COUNT(DISTINCT split) d FROM cases WHERE case_id LIKE ?"
        " GROUP BY city_cluster", (f"pgtest-{RUN}-%",)).fetchall()
    assert rows and all(r["d"] == 1 for r in rows)


def test_for_update_skip_locked_lets_a_second_transaction_see_a_different_row():
    """Isolates the exact clause lease() depends on, with hand-controlled
    transaction boundaries on two RAW connections -- bypassing db.py's
    module-level _LOCK entirely (it only guards the public db.* functions, not a
    bare psycopg connection), which is what makes this deterministic rather than
    dependent on thread-scheduling luck the way the multi-threaded test above
    turned out to be.

    Confirmed to FAIL when FOR UPDATE SKIP LOCKED is stripped from this test's
    own query text: without it, B's SELECT sees the row A is holding instead of
    skipping to the other one, which is precisely the race that would let two
    workers both believe they had claimed the same case.

    Note LIMIT is not "peek at N, keep one" -- FOR UPDATE locks EVERY row the
    SELECT returns, not just whichever one the caller later decides to act on.
    So A must ask for exactly 1 (mirroring a real lease(count=1) call) to leave
    the second seeded row genuinely free for B to see; an earlier draft of this
    test had A request LIMIT 2 and then only look at one of the two rows it had
    actually locked, so B correctly saw NOTHING free and the test failed for a
    reason that had nothing to do with the clause it meant to isolate.
    """
    ids2 = seed(2, "skiplock")
    like = f"pgtest-{RUN}-skiplock-%"
    a = fresh_conn()._raw
    b = fresh_conn()._raw
    a.autocommit = False
    b.autocommit = False

    def select(cur, limit):
        cur.execute(
            "SELECT case_id FROM cases WHERE case_id LIKE %s"
            " ORDER BY case_id LIMIT %s FOR UPDATE SKIP LOCKED", (like, limit))
        return {r["case_id"] for r in cur.fetchall()}

    try:
        with a.cursor() as ca:
            a_rows = select(ca, 1)
            assert a_rows, "fixture: at least one seeded row should be free"
            assert a_rows < set(ids2)              # exactly one, not both
            a_claim = next(iter(a_rows))            # A's transaction stays OPEN,
                                                     # still holding only this row

            with b.cursor() as cb:
                b_rows = select(cb, 2)

            assert a_claim not in b_rows, (
                f"B saw {a_claim!r} even though A holds it FOR UPDATE -- "
                "SKIP LOCKED did not exclude it")
            assert b_rows == set(ids2) - {a_claim}, "B should get exactly the other row"
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()


def test_a_dead_connection_is_replaced_rather_than_poisoning_the_process():
    """A dropped Postgres connection must not 500 every request until a redeploy.

    create_app() opens ONE connection at startup and every route closes over it.
    When SSL enforcement was switched on at Supabase it terminated the
    connections established before it -- and the running service then answered
    500 to every query for the rest of its life, while /healthz, which touches no
    database, kept reporting ok. Only a manual redeploy cleared it.
    """
    conn = fresh_conn()
    assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1

    conn._raw.close()                      # exactly what the server side did
    assert conn._raw.closed

    # The SAME wrapper object -- every route's closure still points at it -- but
    # the connection underneath has been replaced.
    assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
    assert not conn._raw.closed


def test_a_bad_query_does_not_trigger_a_reconnect():
    """Only transport failures reconnect; a broken query must still just fail."""
    import psycopg
    conn = fresh_conn()
    before = conn._raw
    with pytest.raises(psycopg.Error):
        conn.execute("SELECT * FROM a_table_that_does_not_exist")
    assert conn._raw is before, "a SQL error must not churn the connection"

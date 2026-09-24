"""One shared connection, many threads: what must never interleave.

`create_app` opens ONE connection at startup and every route closes over it.
FastAPI runs sync endpoints in a threadpool, so that object is touched from many
threads at once, and `db._LOCK` is the only thing making that safe. A function
that touches `conn` without holding the lock is not a style problem -- it can
land inside another thread's open transaction.
"""

from __future__ import annotations

import ast
import pathlib
import threading

import pytest

from casebroker import db

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _conn_functions():
    """Every module-level db.py function whose first argument is `conn`."""
    src = (ROOT / "casebroker" / "db.py").read_text()
    tree = ast.parse(src)
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        args = [a.arg for a in n.args.args]
        if not args or args[0] != "conn":
            continue
        decs = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                for d in n.decorator_list}
        yield n.name, n.lineno, "_locked" in decs


# Helpers that only ever run INSIDE an already-locked caller, or at startup
# before the app serves anything. _LOCK is reentrant, so locking them too would
# be harmless -- they are listed rather than decorated so that adding a new
# public function without the decorator FAILS rather than passing quietly.
_ALLOWED_UNLOCKED = {
    "_event",              # called only from within locked writers
    "_by_lease",           # ditto: heartbeat/complete/fail/release
    "_refund_timeout",     # ditto: called only from fail, under its lock
    "_still_progressing",  # ditto: called only from heartbeat, under its lock
    "_count_for_build",    # ditto: complete/fail, inside their transaction
    "_setting",            # read inside the locked release functions
    "_set_setting",        # the core of set_setting; also called by set_target/roll_back
    "_set_target",         # inside set_target, promote_canary, roll_back
    "_live_workers",       # inside list_releases, platform_gaps, release_in_use
    "_note_build_change",  # inside lease(), in its transaction
    "_attach_labels",      # inside list_cases and get_case
    "_recipe_knowledge",   # inside list_releases and recipe_gaps
    "_existing_tables",    # schema bring-forward, under _LOCK in connect()
    "_existing_columns",
    "reconcile_columns",
    "widen_columns",       # ditto: called only from apply_schema
    "drop_retired_settings",  # ditto
    "apply_schema",
    "schema_version",
}


def test_every_public_db_function_holds_the_lock():
    """The invariant that was broken.

    `get_case`, `get_footprints`, `fleet` and `status` each read the shared
    connection straight from a request handler with no lock. `status` is the
    worst of them: it backs `/v1/status` AND the `/healthz` probe, so it runs on
    a timer, in production, against the same connection a `lease()` may have
    open a transaction on.
    """
    offenders = [f"db.py:{ln} {name}" for name, ln, locked in _conn_functions()
                 if not locked and name not in _ALLOWED_UNLOCKED]
    assert not offenders, (
        "these touch the shared connection without db._LOCK: " + ", ".join(offenders))


def test_the_app_never_touches_the_connection_directly():
    """Same invariant, one layer up.

    app.py held two raw `conn.execute` calls -- one in the footprints handler,
    one in the /healthz database probe. Both bypassed the lock entirely, and the
    probe is unauthenticated and polled by the platform's health check.
    """
    src = (ROOT / "casebroker" / "app.py").read_text()
    bad = [i for i, line in enumerate(src.splitlines(), 1)
           if "conn.execute(" in line and not line.lstrip().startswith("#")]
    assert not bad, (
        "app.py must go through db.py's locked helpers, not the raw connection; "
        f"offending lines: {bad}")


# -- the failure this actually causes ----------------------------------------

class _FlakyRaw:
    """A psycopg-shaped connection that dies once, mid-transaction."""

    def __init__(self, log, die_on):
        self.log, self.die_on, self.n, self.closed = log, die_on, 0, False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.n += 1
        if self.n == self.die_on:
            import psycopg
            raise psycopg.OperationalError("server closed the connection")
        self.log.append((id(self), sql))
        return self

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_reconnect_never_happens_inside_an_open_transaction():
    """The data-integrity bug in one test.

    `lease()` runs BEGIN, several UPDATEs, then COMMIT. If the connection dies in
    the middle and `_reconnect()` swaps the underlying connection in place, the
    COMMIT runs on a BRAND NEW connection with no transaction open -- so the
    UPDATEs are gone with the old connection, while `lease()` returns its Lease
    objects to the worker as though they had been written. The worker believes it
    holds cases the database still lists as pending, and the next worker leases
    the same ones.

    Reconnecting is right; reconnecting *here* is not. The transaction is already
    doomed, so the honest outcome is to fail this call and repair for the next.
    """
    psycopg = pytest.importorskip("psycopg")
    log: list = []
    raw = _FlakyRaw(log, die_on=3)
    conn = db.PgConnection(raw, dsn="postgresql://unused")

    reconnects = []
    conn._reconnect = lambda: (reconnects.append(1), False)[1]   # never succeeds

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE cases SET state='leased' WHERE case_id='a'")
    with pytest.raises(psycopg.OperationalError):
        conn.execute("UPDATE cases SET state='leased' WHERE case_id='b'")

    assert not reconnects, (
        "reconnected while a transaction was open: a later COMMIT would land on "
        "a fresh connection and silently discard the transaction's writes")


def test_a_reconnect_still_happens_outside_a_transaction():
    """The repair must not be lost, only deferred out of the danger zone."""
    pytest.importorskip("psycopg")
    log: list = []
    conn = db.PgConnection(_FlakyRaw(log, die_on=1), dsn="postgresql://unused")
    reconnects = []
    conn._reconnect = lambda: (reconnects.append(1), False)[1]

    with pytest.raises(Exception):
        conn.execute("SELECT 1")
    assert reconnects, "a dead connection outside a transaction must be repaired"


def test_concurrent_readers_do_not_interleave_with_a_writer(tmp_path):
    """End to end on SQLite: readers hammering while a writer holds the lock."""
    conn = db.connect(str(tmp_path / "c.sqlite"))
    db.add_cases(conn, [{
        "case_id": f"c{i}", "spec": {"lat": 34.0, "lon": -84.0}, "recipe": "r",
        "city_cluster": "x", "lcz": "LCZ1", "split": "train",
        "priority": 100, "max_attempts": 3} for i in range(40)])

    errors: list = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                db.status(conn)
                db.get_case(conn, "c1")
                db.fleet(conn)
                db.get_footprints(conn, "c1")
            except Exception as e:                       # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        seen = set()
        for i in range(25):
            for got in db.lease(conn, f"w{i}", count=1):
                assert got.case_id not in seen, "the same case was leased twice"
                seen.add(got.case_id)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    assert not errors, f"reader blew up against the shared connection: {errors[0]!r}"


def test_a_fleet_under_contention_never_double_leases(tmp_path):
    """The invariant, at the HTTP layer, with the readers that used to break it.

    The db-level test above covers the lock. This one covers the whole stack:
    many workers leasing/heartbeating/completing while other threads hammer
    /healthz, /v1/status and /v1/cases -- which are precisely the unlocked
    readers that could land inside a lease() transaction, and /healthz is the one
    the platform itself polls on a timer.

    Kept small enough to stay in the normal suite. Run against a live uvicorn
    with 400 cases, 24 workers and 8 readers it completes in about a second with
    zero duplicates and flat memory.
    """
    import collections
    import threading

    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    n_cases, n_workers, n_readers = 60, 8, 4
    client = TestClient(create_app(db_path=str(tmp_path / "load.sqlite"),
                                   tokens=["s"]))
    client.headers.update({"Authorization": "Bearer s"})
    added = client.post("/v1/cases", json=[
        {"lat": 33.0 + i * 0.01, "lon": -84.0, "recipe": "r",
         "city_cluster": f"c{i % 5}", "lcz": "LCZ6", "spec": {"dirs": [0]}}
        for i in range(n_cases)]).json()
    assert added["added"] == n_cases, added

    seen: collections.Counter = collections.Counter()
    errors: list = []
    lock = threading.Lock()
    stop = threading.Event()

    def work(wid):
        try:
            while not stop.is_set():
                got = client.post("/v1/lease",
                                  json={"worker_id": f"w{wid}", "count": 2}).json()
                if not got:
                    return
                for g in got:
                    with lock:
                        seen[g["case_id"]] += 1
                for g in got:
                    client.post("/v1/heartbeat", json={"lease_id": g["lease_id"]})
                    client.post("/v1/complete", json={
                        "lease_id": g["lease_id"], "case_id": g["case_id"],
                        "result_uri": f"s3://b/{g['case_id']}", "metrics": {}})
        except Exception as e:                           # noqa: BLE001
            with lock:
                errors.append(f"worker{wid}: {type(e).__name__}: {e}")

    def read():
        try:
            while not stop.is_set():
                client.get("/healthz")
                client.get("/v1/status")
                client.get("/v1/cases?limit=20")
        except Exception as e:                           # noqa: BLE001
            with lock:
                errors.append(f"reader: {type(e).__name__}: {e}")

    workers = [threading.Thread(target=work, args=(i,)) for i in range(n_workers)]
    readers = [threading.Thread(target=read, daemon=True) for _ in range(n_readers)]
    for t in workers + readers:
        t.start()
    for t in workers:
        t.join(timeout=120)
    stop.set()
    for t in readers:
        t.join(timeout=10)

    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, f"the same case was leased more than once: {dupes}"
    assert not errors, f"request failed under contention: {errors[0]}"
    assert client.get("/v1/status").json()["by_state"].get("done") == n_cases


class _RecordingRaw:
    """A psycopg-shaped connection that records what it was asked to run."""

    def __init__(self):
        self.calls, self.closed = [], False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self


def test_nul_characters_never_reach_postgres():
    """A /v1/fail error from `wsl.exe` output (UTF-16 read as UTF-8) carries a
    NUL after every character; Postgres TEXT refuses U+0000 and the whole report
    answered 500. The shim drops the NULs from string parameters, nothing else."""
    raw = _RecordingRaw()
    conn = db.PgConnection(raw)
    conn.execute("UPDATE cases SET last_error=?, attempts=? WHERE case_id=?",
                 ("W\x00S\x00L\x00", 2, "c1"))
    sql, params = raw.calls[-1]
    assert params == ("WSL", 2, "c1")
    assert "%s" in sql and "?" not in sql

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
    "_existing_tables",    # schema bring-forward, under _LOCK in connect()
    "_existing_columns",
    "reconcile_columns",
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

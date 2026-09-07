"""Storage for the case broker: SQLite for local dev/tests, Postgres for production.

Every statement lives in this one module, so the storage engine is a property of
what string you hand :func:`connect` — a file path opens SQLite, a
``postgres(ql)://`` DSN opens Postgres — and nothing above this module (the API
layer, the worker, the tests that talk through the public functions) needs to
know which one it got. That is the promise this module makes and the reason the
two engines share one set of function names and one :class:`Lease` shape.

Why two engines rather than migrating outright: SQLite needs no network and no
credentials, so the 24-plus tests that exercise lease semantics run in
milliseconds with zero external dependencies — exactly what you want for the
property that matters most (**two workers can never be handed the same case**)
to be cheap to re-verify on every change. Postgres is what a real campaign
deploys against, because a managed database survives a service restart or
redeploy where a container's local disk does not, and because the whole point of
centralising state is that MANY processes on MANY machines hit it at once, which
a single SQLite file (correctly) serialises down to one writer at a time.

The two engines therefore do not share identical SQL for the one place it would
be actively wrong to pretend they could: **claiming work under concurrency**.
SQLite's ``BEGIN IMMEDIATE`` takes a whole-database write lock immediately, so
concurrent callers simply queue up one at a time. Postgres instead uses
``SELECT ... FOR UPDATE SKIP LOCKED`` — a per-ROW lock that lets two callers claim
two different rows without blocking each other, which is the entire reason to
move off one file in the first place. See :func:`lease` for the two branches,
and :func:`_by_lease` for the matching row lock the read side needs so a
heartbeat in flight cannot be quietly outraced by a reclaim.

State machine
-------------
    pending --lease--> leased --complete--> done
       ^                  |
       |                  +--fail(retryable), or lease expiry--> pending
       |                  +--fail(fatal), or attempts > max----> quarantined
       +--release (graceful preemption) --------------------------+

Lease expiry is what makes this safe on Phoenix's preemptible ``embers`` QOS: a
worker killed without warning simply stops renewing, and the case returns to the
pool on its own. Nothing has to notice the death.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    spec           TEXT NOT NULL,
    recipe         TEXT NOT NULL,
    city_cluster   TEXT NOT NULL,
    lcz            TEXT,
    split          TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 100,
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    lease_id       TEXT,
    lease_worker   TEXT,
    lease_expires  INTEGER,
    last_error     TEXT,
    result_uri     TEXT,
    result_sha256  TEXT,
    result_bytes   INTEGER,
    metrics        TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- The claim query filters on state and orders by (priority, case_id); this index
-- is what keeps a lease O(log n) instead of a scan over 30,000 rows.
CREATE INDEX IF NOT EXISTS idx_cases_claim ON cases(state, priority, case_id);
CREATE INDEX IF NOT EXISTS idx_cases_lease ON cases(lease_id);
CREATE INDEX IF NOT EXISTS idx_cases_split ON cases(split, state);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT,
    cluster      TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    cases_done   INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0
);

-- Append-only audit trail. Every transition lands here, so "why is this case
-- still pending after three days" stays answerable after the fact.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    case_id   TEXT,
    worker_id TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(event, ts);
"""

# Same schema, Postgres-flavoured: no PRAGMAs (meaningless there), and the
# events table's autoincrement id is a SERIAL rather than SQLite's
# INTEGER PRIMARY KEY AUTOINCREMENT. Everything else -- types, IF NOT EXISTS,
# indexes, the ON CONFLICT syntax used elsewhere -- is valid, identical DDL/DML
# on both engines.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    spec           TEXT NOT NULL,
    recipe         TEXT NOT NULL,
    city_cluster   TEXT NOT NULL,
    lcz            TEXT,
    split          TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 100,
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    lease_id       TEXT,
    lease_worker   TEXT,
    lease_expires  INTEGER,
    last_error     TEXT,
    result_uri     TEXT,
    result_sha256  TEXT,
    result_bytes   INTEGER,
    metrics        TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_claim ON cases(state, priority, case_id);
CREATE INDEX IF NOT EXISTS idx_cases_lease ON cases(lease_id);
CREATE INDEX IF NOT EXISTS idx_cases_split ON cases(split, state);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT,
    cluster      TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    cases_done   INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id        SERIAL PRIMARY KEY,
    ts        INTEGER NOT NULL,
    case_id   TEXT,
    worker_id TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(event, ts);
"""


@dataclass(frozen=True)
class Lease:
    case_id: str
    lease_id: str
    expires_at: int
    spec: dict[str, Any]
    attempt: int


# One process-wide lock around every statement. FastAPI runs sync endpoints in a
# threadpool, so the shared connection is touched from many threads. For SQLite
# this is what makes a shared, check_same_thread=False connection safe at all;
# for Postgres it is pure belt-and-suspenders (correctness across PROCESSES/
# machines comes from FOR UPDATE SKIP LOCKED in the database itself, not from
# this in-process lock) but costs nothing to keep uniform across both engines.
_LOCK = threading.RLock()


class PgConnection:
    """Thin shim so call sites written for SQLite keep working against Postgres.

    Two things this repo's other modules and its own tests do against the raw
    connection, unchanged, that this class exists to keep true: pass ``?``
    positional placeholders (SQLite's style; psycopg wants ``%s``), and iterate a
    cursor's result directly or call ``.fetchone()``/``.fetchall()`` on it and
    index a row by column name (``row["state"]``) the way ``sqlite3.Row`` allows.
    ``?`` never appears inside a string LITERAL anywhere in this module's SQL --
    only ever as a placeholder -- so a blind text substitution is safe here
    without a real SQL parser.

    ``BEGIN IMMEDIATE`` is SQLite-only syntax (Postgres has no ``IMMEDIATE``
    transaction mode and would raise a syntax error on it), so that one literal
    is translated to plain ``BEGIN``; the row-level locking SQLite gets for free
    from the whole-database lock, Postgres gets instead from ``FOR UPDATE`` /
    ``FOR UPDATE SKIP LOCKED`` in the specific queries that need it (see
    :func:`lease` and :func:`_by_lease`).
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PgCursor":
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            sql = "BEGIN"
        cur = self._raw.cursor()
        cur.execute(sql.replace("?", "%s"), tuple(params) if params else None)
        return _PgCursor(cur)

    def executescript(self, sql: str) -> None:
        with self._raw.cursor() as cur:
            cur.execute(sql)


class _PgCursor:
    """Wraps a psycopg cursor (dict-row factory) with the bit of sqlite3.Cursor's
    surface this module actually uses: fetchone/fetchall/rowcount/iteration."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


def connect(path_or_dsn: str):
    """SQLite for a file path, Postgres for a ``postgres(ql)://`` DSN.

    The caller (``CASEBROKER_DB`` in practice) decides the engine purely by what
    string it passes; nothing else in this module, or above it, branches on
    which one it got except the handful of statements in this file that
    genuinely differ between the two.
    """
    if path_or_dsn.startswith(("postgres://", "postgresql://")):
        return _connect_postgres(path_or_dsn)

    # check_same_thread=False because the connection is shared across the
    # threadpool; _LOCK is what makes that safe.
    conn = sqlite3.connect(path_or_dsn, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _LOCK:
        conn.executescript(SCHEMA)
    return conn


def _connect_postgres(dsn: str) -> PgConnection:
    import psycopg
    from psycopg.rows import dict_row

    # autocommit=True mirrors SQLite's isolation_level=None: every statement runs
    # standalone until an explicit BEGIN starts a transaction that a later
    # COMMIT/ROLLBACK closes, which is exactly the pattern every function below
    # already uses. dict_row makes a fetched row support row["col"], matching
    # sqlite3.Row.
    #
    # prepare_threshold=None turns off psycopg's automatic server-side prepared
    # statements. Measured against Supabase's pooler (port 6543, PgBouncer in
    # TRANSACTION mode): after ~5 uses of the same query text psycopg names and
    # prepares it server-side, but a transactional pooler can route the NEXT
    # transaction to a different backend connection than the one that prepared
    # it -- and to one where a DIFFERENT client's session already used that same
    # generated name for something else, which raised
    # "prepared statement \"_pg3_0\" already exists" here on exactly the sixth
    # call. Every statement in this module is either a one-off or cheap enough
    # that losing server-side preparation costs nothing worth trading for a
    # connection that is correct on a transactional pooler.
    raw = psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                          prepare_threshold=None)
    wrapped = PgConnection(raw)
    with _LOCK:
        wrapped.executescript(PG_SCHEMA)
    return wrapped


def _now() -> int:
    return int(time.time())


def _event(conn, case_id, worker_id, event, detail=None, now=None) -> None:
    conn.execute(
        "INSERT INTO events(ts, case_id, worker_id, event, detail) VALUES (?,?,?,?,?)",
        (now or _now(), case_id, worker_id, event, detail),
    )


# -- ingest -------------------------------------------------------------------

def _locked(fn):
    """Serialise a public entry point against the shared connection."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _LOCK:
            return fn(*a, **kw)
    return wrapper


@_locked
def add_cases(conn, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Append cases. Idempotent by case_id, so re-adding an existing case is a
    no-op -- which is what makes "extend the dataset by 10,000" a safe, repeatable
    command rather than a one-shot migration you must not run twice."""
    now = _now()
    added = skipped = 0
    columns = (" (case_id, spec, recipe, city_cluster, lcz, split, priority,"
              "  max_attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    # Same idempotent-insert intent, two dialects: SQLite's OR IGNORE clause sits
    # on INSERT itself, Postgres's sits after the VALUES list as ON CONFLICT.
    insert_sql = (
        "INSERT INTO cases" + columns + " ON CONFLICT (case_id) DO NOTHING"
        if isinstance(conn, PgConnection)
        else "INSERT OR IGNORE INTO cases" + columns
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            cur = conn.execute(
                insert_sql,
                (r["case_id"], json.dumps(r["spec"], sort_keys=True), r["recipe"],
                 r["city_cluster"], r.get("lcz"), r["split"], r.get("priority", 100),
                 r.get("max_attempts", 3), now, now),
            )
            if cur.rowcount:
                added += 1
                _event(conn, r["case_id"], None, "created", r["recipe"], now)
            else:
                skipped += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"added": added, "skipped": skipped}


# -- lease / report -----------------------------------------------------------

@_locked
def lease(conn, worker_id: str, count: int = 1,
          lease_seconds: int = 3600, splits: list[str] | None = None,
          now: int | None = None, host: str | None = None,
          cluster: str | None = None) -> list[Lease]:
    """Atomically claim up to ``count`` cases.

    Expired leases are reclaimed by the same statement that hands out fresh work,
    so a crashed or preempted worker's cases re-enter the pool with no reaper
    process and no operator action.
    """
    now = now or _now()
    expires = now + lease_seconds
    out: list[Lease] = []
    is_pg = isinstance(conn, PgConnection)

    conn.execute("BEGIN IMMEDIATE")
    try:
        params: list[Any] = [now]
        split_sql = ""
        if splits:
            placeholders = ",".join("?" for _ in splits)
            split_sql = " AND split IN (" + placeholders + ")"
            params.extend(splits)
        params.append(count)

        # SQLite already has the whole database exclusively locked by BEGIN
        # IMMEDIATE above, so no per-row locking clause is needed or valid there.
        # Postgres instead locks only the rows this call is about to claim, and
        # SKIPS any row a concurrent lease() or an in-flight heartbeat/complete/
        # fail/release already holds (see _by_lease) rather than blocking on it --
        # which is the entire point of moving off one file that serialises
        # everything to begin with.
        lock_clause = " FOR UPDATE SKIP LOCKED" if is_pg else ""
        rows = conn.execute(
            "SELECT case_id, spec, attempts, max_attempts FROM cases"
            " WHERE (state = 'pending' OR (state = 'leased' AND lease_expires < ?))"
            + split_sql +
            " ORDER BY priority ASC, case_id ASC LIMIT ?" + lock_clause,
            params,
        ).fetchall()

        for row in rows:
            attempt = row["attempts"] + 1
            if attempt > row["max_attempts"]:
                # Poison case: its retries are spent. Park it rather than let it
                # cycle forever through every worker in the fleet.
                conn.execute(
                    "UPDATE cases SET state='quarantined', lease_id=NULL,"
                    " lease_worker=NULL, lease_expires=NULL, updated_at=?"
                    " WHERE case_id=?", (now, row["case_id"]))
                _event(conn, row["case_id"], worker_id, "quarantined",
                       "attempts exhausted (%d)" % row["max_attempts"], now)
                continue

            lease_id = uuid.uuid4().hex
            conn.execute(
                "UPDATE cases SET state='leased', lease_id=?, lease_worker=?,"
                " lease_expires=?, attempts=?, updated_at=? WHERE case_id=?",
                (lease_id, worker_id, expires, attempt, now, row["case_id"]))
            _event(conn, row["case_id"], worker_id, "leased", "attempt %d" % attempt, now)
            out.append(Lease(case_id=row["case_id"], lease_id=lease_id,
                             expires_at=expires, spec=json.loads(row["spec"]),
                             attempt=attempt))

        # host/cluster are refreshed on every lease call (not just insert): the
        # same worker_id can in principle move machines across a restart, and a
        # stale "where did this run" answer is worse than a slightly redundant
        # write on every poll.
        conn.execute(
            "INSERT INTO workers(worker_id, host, cluster, first_seen, last_seen)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(worker_id) DO UPDATE SET"
            " last_seen=excluded.last_seen, host=excluded.host, cluster=excluded.cluster",
            (worker_id, host, cluster, now, now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _by_lease(conn, lease_id: str):
    """The row a lease_id currently owns, row-locked under Postgres.

    The lock is what stops a concurrent lease() from reclaiming this exact row
    (via its own FOR UPDATE SKIP LOCKED, which will skip a row this transaction
    holds) for the whole duration of a heartbeat/complete/fail/release call --
    the same guarantee SQLite gets for free from BEGIN IMMEDIATE's whole-database
    lock, here narrowed to the one row that is actually contended.
    """
    sql = "SELECT * FROM cases WHERE lease_id=? AND state='leased'"
    if isinstance(conn, PgConnection):
        sql += " FOR UPDATE"
    return conn.execute(sql, (lease_id,)).fetchone()


@_locked
def heartbeat(conn, lease_id: str, lease_seconds: int = 3600,
              detail: str | None = None, now: int | None = None) -> bool:
    """Extend a lease. Returns False when the lease is gone -- the worker must
    then STOP working that case, because someone else may already own it."""
    now = now or _now()
    row = _by_lease(conn, lease_id)
    if row is None:
        return False
    conn.execute("UPDATE cases SET lease_expires=?, updated_at=? WHERE lease_id=?",
                 (now + lease_seconds, now, lease_id))
    if detail:
        _event(conn, row["case_id"], row["lease_worker"], "progress", detail, now)
    return True


@_locked
def complete(conn, lease_id: str, result_uri: str,
             sha256: str | None = None, nbytes: int | None = None,
             metrics: dict[str, Any] | None = None, now: int | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE cases SET state='done', result_uri=?, result_sha256=?,"
            " result_bytes=?, metrics=?, lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, last_error=NULL, updated_at=? WHERE case_id=?",
            (result_uri, sha256, nbytes, json.dumps(metrics or {}, sort_keys=True),
             now, row["case_id"]))
        conn.execute(
            "UPDATE workers SET cases_done = cases_done + 1, last_seen=? WHERE worker_id=?",
            (now, row["lease_worker"]))
        _event(conn, row["case_id"], row["lease_worker"], "done", result_uri, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


@_locked
def fail(conn, lease_id: str, error: str, retryable: bool = True,
         now: int | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            conn.execute("ROLLBACK")
            return False
        exhausted = row["attempts"] >= row["max_attempts"]
        state = "pending" if (retryable and not exhausted) else "quarantined"
        conn.execute(
            "UPDATE cases SET state=?, lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, last_error=?, updated_at=? WHERE case_id=?",
            (state, error[:4000], now, row["case_id"]))
        conn.execute(
            "UPDATE workers SET cases_failed = cases_failed + 1, last_seen=? WHERE worker_id=?",
            (now, row["lease_worker"]))
        _event(conn, row["case_id"], row["lease_worker"],
               "failed" if state == "pending" else "quarantined", error[:500], now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


@_locked
def release(conn, lease_id: str, reason: str = "released",
            now: int | None = None) -> bool:
    """Hand a case back untouched, without burning a retry.

    This is the preemption path: a SIGTERM'd worker calls it and the case becomes
    available immediately instead of sitting unavailable until its TTL runs out.
    The attempt is refunded because nothing about the case was wrong.
    """
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, attempts=MAX(attempts - 1, 0), updated_at=?"
            " WHERE case_id=?", (now, row["case_id"]))
        _event(conn, row["case_id"], row["lease_worker"], "released", reason, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# -- observability ------------------------------------------------------------

@_locked
def status(conn, now: int | None = None) -> dict[str, Any]:
    now = now or _now()
    by_state = {r["state"]: r["n"] for r in
                conn.execute("SELECT state, COUNT(*) n FROM cases GROUP BY state")}
    by_split = {r["split"] + "/" + r["state"]: r["n"] for r in
                conn.execute("SELECT split, state, COUNT(*) n FROM cases GROUP BY split, state")}
    stale = conn.execute(
        "SELECT COUNT(*) n FROM cases WHERE state='leased' AND lease_expires < ?",
        (now,)).fetchone()["n"]
    done_24h = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE event='done' AND ts > ?",
        (now - 86400,)).fetchone()["n"]
    remaining = by_state.get("pending", 0) + by_state.get("leased", 0)
    return {
        "by_state": by_state,
        "by_split": by_split,
        "expired_leases": stale,
        "done_last_24h": done_24h,
        "remaining": remaining,
        # None, not a fabricated infinity: with no completions the rate is unknown.
        "eta_days": round(remaining / done_24h, 1) if done_24h else None,
        "workers": [dict(r) for r in conn.execute(
            "SELECT * FROM workers ORDER BY last_seen DESC LIMIT 50")],
    }


@_locked
def list_cases(conn, state: str | None = None, split: str | None = None,
               city_cluster: str | None = None, limit: int = 50,
               offset: int = 0) -> dict[str, Any]:
    """A page of cases for the dashboard's case browser, most-recently-touched
    first -- that ordering is what makes "what just happened" the default view
    rather than an arbitrary slice of a 40,000-row table.

    Returns both the page and the total matching count, so a client can render
    "N of M" and page controls without a second round trip.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, params = [], []
    if state:
        where.append("state = ?"); params.append(state)
    if split:
        where.append("split = ?"); params.append(split)
    if city_cluster:
        where.append("city_cluster = ?"); params.append(city_cluster)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) n FROM cases" + clause, params).fetchone()["n"]
    rows = conn.execute(
        "SELECT * FROM cases" + clause + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    return {"cases": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

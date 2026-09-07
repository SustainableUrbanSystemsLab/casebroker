"""SQLite storage for the case broker.

SQLite rather than Postgres for the prototype, but every statement lives in this
one module so the swap is a single file. WAL mode plus ``BEGIN IMMEDIATE`` around
the claim buys the one property that actually matters with many workers: **two
workers can never be handed the same case**.

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


@dataclass(frozen=True)
class Lease:
    case_id: str
    lease_id: str
    expires_at: int
    spec: dict[str, Any]
    attempt: int


# One process-wide lock around every statement. FastAPI runs sync endpoints in a
# threadpool, so the shared connection is touched from many threads; SQLite
# serialises writers regardless, and holding the lock here means a concurrent
# reader can never observe a half-applied claim. The critical sections are
# microseconds, so this is not a throughput ceiling at 30,000 cases.
_LOCK = threading.RLock()


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False because the connection is shared across the
    # threadpool; _LOCK is what makes that safe.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _LOCK:
        conn.executescript(SCHEMA)
    return conn


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
def add_cases(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Append cases. Idempotent by case_id, so re-adding an existing case is a
    no-op -- which is what makes "extend the dataset by 10,000" a safe, repeatable
    command rather than a one-shot migration you must not run twice."""
    now = _now()
    added = skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO cases"
                " (case_id, spec, recipe, city_cluster, lcz, split, priority,"
                "  max_attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
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
def lease(conn: sqlite3.Connection, worker_id: str, count: int = 1,
          lease_seconds: int = 3600, splits: list[str] | None = None,
          now: int | None = None) -> list[Lease]:
    """Atomically claim up to ``count`` cases.

    Expired leases are reclaimed by the same statement that hands out fresh work,
    so a crashed or preempted worker's cases re-enter the pool with no reaper
    process and no operator action.
    """
    now = now or _now()
    expires = now + lease_seconds
    out: list[Lease] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        params: list[Any] = [now]
        split_sql = ""
        if splits:
            placeholders = ",".join("?" for _ in splits)
            split_sql = " AND split IN (" + placeholders + ")"
            params.extend(splits)
        params.append(count)
        rows = conn.execute(
            "SELECT case_id, spec, attempts, max_attempts FROM cases"
            " WHERE (state = 'pending' OR (state = 'leased' AND lease_expires < ?))"
            + split_sql +
            " ORDER BY priority ASC, case_id ASC LIMIT ?",
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

        conn.execute(
            "INSERT INTO workers(worker_id, first_seen, last_seen) VALUES (?,?,?)"
            " ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen",
            (worker_id, now, now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _by_lease(conn: sqlite3.Connection, lease_id: str):
    return conn.execute(
        "SELECT * FROM cases WHERE lease_id=? AND state='leased'", (lease_id,)).fetchone()


@_locked
def heartbeat(conn: sqlite3.Connection, lease_id: str, lease_seconds: int = 3600,
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
def complete(conn: sqlite3.Connection, lease_id: str, result_uri: str,
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
def fail(conn: sqlite3.Connection, lease_id: str, error: str, retryable: bool = True,
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
def release(conn: sqlite3.Connection, lease_id: str, reason: str = "released",
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
def status(conn: sqlite3.Connection, now: int | None = None) -> dict[str, Any]:
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

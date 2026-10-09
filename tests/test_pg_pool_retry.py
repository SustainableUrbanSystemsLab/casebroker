"""PgPool's retry of a transaction Postgres aborted to settle a race.

With one connection under one lock two transactions never met in the database.
With a pool they can, and Postgres settles a deadlock or a serialization failure
by aborting one of them (SQLSTATE 40P01 / 40001). The loser's work is rolled
back whole, so the call is run again -- but only a WHOLE call: one nested inside
a transaction its caller opened must raise, because the caller's work went too.
No database needed: the pool is given a fake connection factory.
"""
from __future__ import annotations

import pytest

from casebroker import db


class _Aborted(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Raw:
    closed = False

    def close(self):
        pass


@pytest.fixture()
def pool():
    return db.PgPool("postgresql://unused", 2, connect=_Raw)


def test_a_deadlocked_call_is_run_again(pool, monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    calls = []

    @db._locked
    def write(conn):
        calls.append(1)
        if len(calls) == 1:
            raise _Aborted("40P01")
        return "ok"

    assert write(pool) == "ok" and len(calls) == 2


def test_it_gives_up_after_the_retries(pool, monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)

    @db._locked
    def write(conn):
        raise _Aborted("40001")

    with pytest.raises(_Aborted):
        write(pool)


def test_any_other_error_is_raised_at_once(pool):
    calls = []

    @db._locked
    def write(conn):
        calls.append(1)
        raise ValueError("a bug, not a race")

    with pytest.raises(ValueError):
        write(pool)
    assert len(calls) == 1


def test_a_call_nested_in_an_open_transaction_is_not_rerun_alone(pool, monkeypatch):
    """The inner call raises straight through to the caller that opened the
    transaction; it is that WHOLE call which runs again, inner and all."""
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    inner_runs, outer_runs = [], []

    @db._locked
    def inner(conn):
        inner_runs.append(1)
        raise _Aborted("40P01")

    @db._locked
    def outer(conn):
        outer_runs.append(1)
        with pool.session() as s:
            s._in_tx = True          # what conn.execute("BEGIN") would have set
            try:
                return inner(conn)
            finally:
                s._in_tx = False     # what its ROLLBACK would have cleared

    with pytest.raises(_Aborted):
        outer(pool)
    assert len(outer_runs) == db._PG_RETRIES + 1
    assert len(inner_runs) == len(outer_runs), "never re-run on its own inside the transaction"


def test_nested_calls_share_one_session(pool):
    seen = []

    @db._locked
    def inner(conn):
        seen.append(conn._local.conn)

    @db._locked
    def outer(conn):
        seen.append(conn._local.conn)
        inner(conn)

    outer(pool)
    assert seen[0] is seen[1] and seen[0] is not None
    assert getattr(pool._local, "conn", None) is None, "returned to the pool afterwards"

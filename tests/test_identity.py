"""Accounts, sessions and per-machine tokens, exercised against a real database.

The credential model these replace was a single shared secret in an environment
variable: every machine carried the same one, a human pasted that same
worker-grade secret into a browser to see the dashboard, and revoking one
machine meant rotating all of them plus a redeploy. The tests below are written
around the properties that failure had.
"""

from __future__ import annotations

import pytest

from casebroker import auth, db


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "identity.sqlite"))


# -- passwords ----------------------------------------------------------------

def test_a_password_is_never_stored_and_never_hashes_the_same_twice():
    """Per-user salt: two people who choose the same password must not be
    visibly identical in a database dump."""
    a = auth.hash_password("hunter2")
    b = auth.hash_password("hunter2")
    assert a != b
    assert "hunter2" not in a
    assert auth.verify_password("hunter2", a)
    assert auth.verify_password("hunter2", b)


def test_a_corrupt_hash_fails_the_login_rather_than_raising():
    """A malformed row must deny access, not 500 -- an exception here is both an
    outage and a signal that the row exists."""
    for junk in ("", "not-a-hash", "scrypt$bad", "bcrypt$1$2$3$4$5"):
        assert auth.verify_password("anything", junk) is False


def test_the_work_factors_travel_with_the_hash():
    """So they can be raised later without invalidating existing passwords."""
    h = auth.hash_password("x")
    scheme, n, r, p, _salt, _dk = h.split("$")
    assert scheme == "scrypt"
    assert int(n) >= 2 ** 14 and int(r) == 8 and int(p) == 1


# -- sessions -----------------------------------------------------------------

def test_a_session_identifies_its_user_and_expires(conn):
    u = db.create_user(conn, "ada", auth.hash_password("pw"))
    tok = auth.new_token()
    db.start_session(conn, u["id"], auth.hash_token(tok), expires_at=2_000)

    assert db.session_user(conn, auth.hash_token(tok), now=1_000)["username"] == "ada"
    # Expiry is checked on read, not swept by a background job -- a sweep that
    # stopped running would silently extend every session forever.
    assert db.session_user(conn, auth.hash_token(tok), now=2_001) is None


def test_only_the_session_hash_is_stored(conn):
    """A database dump must not hand over live sessions."""
    u = db.create_user(conn, "ada", auth.hash_password("pw"))
    tok = auth.new_token()
    db.start_session(conn, u["id"], auth.hash_token(tok), expires_at=auth.session_expiry())
    rows = conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert rows and rows[0][0] != tok


def test_logging_out_takes_effect_immediately(conn):
    u = db.create_user(conn, "ada", auth.hash_password("pw"))
    tok = auth.new_token()
    db.start_session(conn, u["id"], auth.hash_token(tok), expires_at=auth.session_expiry())
    db.end_session(conn, auth.hash_token(tok))
    assert db.session_user(conn, auth.hash_token(tok)) is None


# -- per-machine tokens -------------------------------------------------------

def test_a_machine_token_names_the_machine_that_used_it(conn):
    """The thing a shared secret could never answer: which box was that?"""
    tok = auth.new_token()
    db.create_worker_token(conn, "lab-ws-02", auth.hash_token(tok), created_by="ada")
    assert db.worker_token_owner(conn, auth.hash_token(tok))["name"] == "lab-ws-02"


def test_using_a_token_records_when_it_was_last_seen(conn):
    tok = auth.new_token()
    db.create_worker_token(conn, "lab-ws-02", auth.hash_token(tok))
    assert db.list_worker_tokens(conn)[0]["last_seen_at"] is None
    db.worker_token_owner(conn, auth.hash_token(tok), now=1234)
    assert db.list_worker_tokens(conn)[0]["last_seen_at"] == 1234


def test_revoking_one_machine_leaves_the_others_working(conn):
    """The whole point of per-machine credentials. Under the shared secret this
    was impossible: revoking one machine meant rotating every machine."""
    keep, drop = auth.new_token(), auth.new_token()
    db.create_worker_token(conn, "keeper", auth.hash_token(keep))
    db.create_worker_token(conn, "stolen", auth.hash_token(drop))

    assert db.revoke_worker_token(conn, "stolen") is True
    assert db.worker_token_owner(conn, auth.hash_token(drop)) is None
    assert db.worker_token_owner(conn, auth.hash_token(keep))["name"] == "keeper"


def test_revoking_twice_reports_that_it_was_already_revoked(conn):
    tok = auth.new_token()
    db.create_worker_token(conn, "ws", auth.hash_token(tok))
    assert db.revoke_worker_token(conn, "ws") is True
    assert db.revoke_worker_token(conn, "ws") is False


def test_an_unknown_token_belongs_to_nobody(conn):
    assert db.worker_token_owner(conn, auth.hash_token(auth.new_token())) is None


def test_no_users_is_what_opens_first_run_setup(conn):
    assert db.count_users(conn) == 0
    db.create_user(conn, "ada", auth.hash_password("pw"))
    assert db.count_users(conn) == 1

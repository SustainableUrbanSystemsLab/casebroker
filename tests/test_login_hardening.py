"""The login endpoint is unauthenticated, and scrypt is expensive on purpose.

That combination is the whole problem. Every attempt costs ~100 ms of CPU and
~16 MB of RAM by design -- which is what makes a stolen hash expensive to attack
-- so anything that lets an anonymous caller run unbounded attempts turns the
defence into a remote resource exhaustion against a 512 MB instance that already
sits at ~280 MB once the geo libraries are resident.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from casebroker import auth
from casebroker.app import create_app


@pytest.fixture()
def broker(tmp_path):
    app = create_app(db_path=str(tmp_path / "l.sqlite"))
    c = TestClient(app)
    c.post("/v1/auth/setup", json={"username": "real", "password": "a-long-password"})
    return c


def _ms(fn, n=5):
    best = 1e9
    for _ in range(n):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1000


def test_an_unknown_username_costs_the_same_as_a_known_one():
    """The decoy hash used to be BUILT on every request.

    `hash_password("decoy")` is itself a full scrypt, so a miss ran scrypt twice
    and a hit ran it once -- measured at exactly 2.0x. The decoy exists to stop
    response time enumerating accounts, and in that shape it was what enumerated
    them, just with the sign flipped. Precomputed once, a miss and a hit are both
    a single verify.
    """
    real = auth.hash_password("a-real-password")
    hit = _ms(lambda: auth.verify_password("guess", real))
    miss = _ms(lambda: auth.verify_password("guess", auth.DECOY_HASH))
    assert 0.6 < miss / hit < 1.6, f"miss/hit ratio {miss / hit:.2f} leaks account existence"


def test_the_decoy_is_built_once_not_per_request():
    assert auth.DECOY_HASH is auth.DECOY_HASH
    assert auth.DECOY_HASH.startswith("scrypt$")
    # And it must never verify against anything a caller could send.
    assert not auth.verify_password("decoy", auth.DECOY_HASH)
    assert not auth.verify_password("", auth.DECOY_HASH)


def test_rotating_usernames_cannot_escape_the_throttle(broker):
    """The bypass.

    The throttle keyed on `username|client`, and the username is attacker-chosen
    and need not exist -- so every attempt with a fresh username landed in a
    fresh bucket and the limit never applied. Unlimited attempts, each one a full
    scrypt, from an endpoint that needs no credential.
    """
    codes = [broker.post("/v1/auth/login",
                         json={"username": f"nobody{i}", "password": "x"}).status_code
             for i in range(40)]
    assert 429 in codes, "rotating the username walked straight past the throttle"
    # And it should bite well before 40 attempts.
    assert codes.index(429) < 30, f"throttle engaged only after {codes.index(429)} attempts"


def test_a_real_account_is_still_reachable_from_a_clean_address(broker):
    """The throttle must not lock out the person who owns the broker."""
    assert broker.post("/v1/auth/login",
                       json={"username": "real", "password": "a-long-password"}
                       ).status_code == 200


def test_a_successful_login_clears_the_run(broker):
    for _ in range(3):
        broker.post("/v1/auth/login", json={"username": "real", "password": "wrong"})
    assert broker.post("/v1/auth/login",
                       json={"username": "real", "password": "a-long-password"}
                       ).status_code == 200
    for _ in range(3):
        broker.post("/v1/auth/login", json={"username": "real", "password": "wrong"})
    assert broker.post("/v1/auth/login",
                       json={"username": "real", "password": "a-long-password"}
                       ).status_code == 200


def test_concurrent_password_checks_are_bounded(broker):
    """scrypt is 16 MB a go; the threadpool is 40 wide.

    Nothing capped how many ran at once, so 40 simultaneous logins could ask for
    ~640 MB on a 512 MB instance. The cap is what stops an unauthenticated
    endpoint being able to OOM the service.
    """
    from casebroker import app as appmod
    assert hasattr(appmod, "PASSWORD_CONCURRENCY"), "no bound on concurrent scrypt"
    assert 1 <= appmod.PASSWORD_CONCURRENCY <= 8


def test_every_scrypt_call_in_app_goes_through_the_bound():
    """Structural, because bounding one call site is not bounding scrypt.

    Login was capped first, which left four other places running it unbounded --
    including the password CHANGE path, reachable by any logged-in user and
    repeatable. A cap on one endpoint is a cap on one endpoint.
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "casebroker" / "app.py").read_text()
    lines = src.splitlines()
    # The two wrappers are allowed to call auth.* -- they are the bound.
    inside_wrapper = set()
    for i, line in enumerate(lines):
        if re.match(r"def _(hash|verify)_password\(", line):
            for j in range(i, min(i + 8, len(lines))):
                inside_wrapper.add(j)

    bad = [f"app.py:{i + 1}" for i, line in enumerate(lines)
           if re.search(r"\bauth\.(hash|verify)_password\(", line)
           and i not in inside_wrapper]
    assert not bad, (
        "scrypt called outside _hash_password/_verify_password at: " + ", ".join(bad))


def test_the_wrappers_actually_hold_the_semaphore():
    import inspect

    from casebroker import app as appmod

    for fn in (appmod._hash_password, appmod._verify_password):
        assert "_password_slots" in inspect.getsource(fn), fn.__name__


def test_the_wrappers_still_compute_the_right_answer():
    from casebroker import app as appmod

    h = appmod._hash_password("a-long-enough-password")
    assert appmod._verify_password("a-long-enough-password", h) is True
    assert appmod._verify_password("wrong", h) is False

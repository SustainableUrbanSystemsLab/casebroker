"""Token scopes: named by capability, self-describing, and back-compatible.

The old variables said what a token WAS, not what it GRANTED: `CASEBROKER_TOKENS`
is read+write, but only this codebase said so, while its partner was explicitly
`READONLY`. Holding a token, the only way to discover its capability was to
attempt a mutating call against a live broker and see whether it 401'd.

These lock in the replacement: `*_WRITE_TOKENS` / `*_READ_TOKENS`, the legacy
spellings still working, a refusal to guess when both are set and disagree, and
`/v1/whoami` answering the question directly.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from casebroker.app import create_app  # noqa: E402


def client(tmp_path, **env):
    import os
    for k in ("CASEBROKER_WRITE_TOKENS", "CASEBROKER_TOKENS",
              "CASEBROKER_READ_TOKENS", "CASEBROKER_READONLY_TOKENS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        return TestClient(create_app(db_path=str(tmp_path / "a.sqlite")))
    finally:
        for k in env:
            os.environ.pop(k, None)


def test_whoami_reports_write_for_a_write_token(tmp_path):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    got = c.get("/v1/whoami", headers={"Authorization": "Bearer w"}).json()
    assert got["scope"] == "write"


def test_whoami_reports_read_for_a_read_token(tmp_path):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    got = c.get("/v1/whoami", headers={"Authorization": "Bearer r"}).json()
    assert got["scope"] == "read"


def test_whoami_is_200_with_scope_none_for_a_bad_token(tmp_path):
    """200, not 401: 'wrong credential' must be distinguishable from 'server down'."""
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w")
    r = c.get("/v1/whoami", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 200
    assert r.json()["scope"] == "none"


def test_whoami_says_open_when_no_tokens_are_configured(tmp_path):
    c = client(tmp_path)
    got = c.get("/v1/whoami").json()
    assert got["scope"] == "write" and got["auth"] == "OPEN"


def test_whoami_agrees_with_what_the_gates_actually_enforce(tmp_path):
    """The point of whoami is that it cannot drift from the real dependencies."""
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    for token, scope in (("w", "write"), ("r", "read")):
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/v1/whoami", headers=h).json()["scope"] == scope
        # read is allowed for both; write only for the write token
        assert c.get("/v1/status", headers=h).status_code == 200
        wrote = c.post("/v1/cases", headers=h, json=[]).status_code
        assert (wrote != 401) is (scope == "write"), f"{scope} token write={wrote}"


@pytest.mark.parametrize("legacy,canonical", [
    ("CASEBROKER_TOKENS", "write"),
    ("CASEBROKER_READONLY_TOKENS", "read"),
])
def test_legacy_variable_names_still_work(tmp_path, legacy, canonical):
    """No deployment may break on the rename -- production sets the old names."""
    c = client(tmp_path, **{legacy: "tok"})
    assert c.get("/v1/whoami", headers={"Authorization": "Bearer tok"}
                 ).json()["scope"] == canonical


def test_both_spellings_set_and_disagreeing_is_refused(tmp_path):
    """Never resolve by precedence: the failure mode is a 'revoked' token still working."""
    with pytest.raises(RuntimeError, match="both are set|both set"):
        client(tmp_path, CASEBROKER_WRITE_TOKENS="new", CASEBROKER_TOKENS="old")


def test_both_spellings_set_and_agreeing_is_fine(tmp_path):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="same", CASEBROKER_TOKENS="same")
    assert c.get("/v1/whoami", headers={"Authorization": "Bearer same"}
                 ).json()["scope"] == "write"


def test_healthz_reports_scope_counts_but_never_values(tmp_path):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w1,w2", CASEBROKER_READ_TOKENS="r1")
    body = c.get("/healthz")
    assert body.json()["scopes"] == {"write": 2, "read": 1}
    for secret in ("w1", "w2", "r1"):
        assert secret not in body.text


# Every mutating endpoint, with a WELL-FORMED body. The body shape matters: FastAPI
# validates it before the auth dependency runs, so a malformed request answers 422
# without auth ever being consulted. Probing with a bad body therefore proves
# nothing about whether the endpoint is guarded -- an easy way to convince yourself
# a hole exists, or worse, that one does not.
MUTATING = [
    ("/v1/lease", {"worker_id": "probe", "count": 1}),
    ("/v1/cases", []),
    ("/v1/heartbeat", {"case_id": "x", "worker_id": "probe"}),
    ("/v1/complete", {"case_id": "x", "worker_id": "probe", "result_uri": "s3://x"}),
    ("/v1/fail", {"case_id": "x", "worker_id": "probe", "error": "e"}),
    ("/v1/release", {"case_id": "x", "worker_id": "probe"}),
]


@pytest.mark.parametrize("path,body", MUTATING)
def test_anonymous_cannot_mutate(tmp_path, path, body):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    assert c.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path,body", MUTATING)
def test_a_read_token_cannot_mutate(tmp_path, path, body):
    """The read token is a real credential boundary, not a UI that hides buttons."""
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    r = c.post(path, json=body, headers={"Authorization": "Bearer r"})
    assert r.status_code == 401, f"{path} accepted a read-only token ({r.status_code})"


def test_whoami_adds_no_oracle_that_the_guarded_endpoints_lack(tmp_path):
    """whoami is unauthenticated by design; it must not be a NEW disclosure.

    Anything it reveals about a candidate token -- valid or not -- was already
    readable off /v1/status's 200-vs-401. It labels that answer rather than making
    a caller infer it; the underlying disclosure is identical.
    """
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="w", CASEBROKER_READ_TOKENS="r")
    for token, valid in (("w", True), ("r", True), ("bogus", False)):
        h = {"Authorization": f"Bearer {token}"}
        via_status = c.get("/v1/status", headers=h).status_code == 200
        via_whoami = c.get("/v1/whoami", headers=h).json()["scope"] != "none"
        assert via_status == via_whoami == valid


def test_whoami_never_returns_a_token_value(tmp_path):
    c = client(tmp_path, CASEBROKER_WRITE_TOKENS="supersecret", CASEBROKER_READ_TOKENS="alsosecret")
    for h in ({}, {"Authorization": "Bearer supersecret"}, {"Authorization": "Bearer nope"}):
        body = c.get("/v1/whoami", headers=h).text
        assert "supersecret" not in body and "alsosecret" not in body

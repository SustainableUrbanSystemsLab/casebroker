"""A machine asks to join; an admin approves in the browser.

This is what `E3D --setup-sim-node` talks to. The enrolment it replaces
had an admin type their password on every simulation node -- shared cluster
logins and lab boxes, which is the wrong place for it. Here nothing secret is
typed on the node at all, and the design goes one step further than the textbook
device flow: the NODE generates the token and sends only its hash, so there is no
moment at which the broker holds a readable credential.
"""

from __future__ import annotations

import hashlib
import pathlib
import secrets

import pytest
from fastapi.testclient import TestClient

from casebroker import db
from casebroker.app import create_app


@pytest.fixture()
def world(tmp_path):
    path = tmp_path / "pair.sqlite"
    app = create_app(db_path=str(path))
    admin, node = TestClient(app), TestClient(app)
    admin.post("/v1/auth/setup", json={"username": "pk", "password": "a-long-password"})
    return admin, node, path


def _token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _start(node, name="lab-ws-01", token_hash=None, **kw):
    raw, h = _token()
    r = node.post("/v1/pair/start", json={"name": name, "token_hash": token_hash or h,
                                          "host": "ws01.lab", "platform": "win-x64", **kw})
    return r, raw


def _poll(node, code, raw):
    return node.post("/v1/pair/poll", json={"user_code": code},
                     headers={"Authorization": f"Bearer {raw}"})


def test_the_whole_flow(world):
    admin, node, _ = world
    r, raw = _start(node)
    assert r.status_code == 200, r.text
    code = r.json()["user_code"]
    assert len(code) == 9 and code[4] == "-"
    assert r.json()["verification_url"].endswith("/?pair=" + code)

    assert _poll(node, code, raw).json()["status"] == "pending"
    # the token does NOT work before approval
    assert node.post("/v1/lease", json={"worker_id": "lab-ws-01"},
                     headers={"Authorization": f"Bearer {raw}"}).status_code == 401

    pending = admin.get("/v1/pair/pending").json()["pending"]
    assert [p["name"] for p in pending] == ["lab-ws-01"]
    assert pending[0]["user_code"] == code and pending[0]["host"] == "ws01.lab"

    assert admin.post(f"/v1/pair/{code}/approve").json() == {"status": "approved"}
    assert _poll(node, code, raw).json()["status"] == "approved"

    # and now it is an ordinary per-machine credential
    who = node.get("/v1/whoami", headers={"Authorization": f"Bearer {raw}"}).json()
    assert who["scope"] == "write" and who["auth"] == "machine" and who["machine"] == "lab-ws-01"
    assert node.post("/v1/lease", json={"worker_id": "lab-ws-01"},
                     headers={"Authorization": f"Bearer {raw}"}).status_code == 200
    assert admin.get("/v1/pair/pending").json()["pending"] == []


def test_the_raw_token_never_reaches_the_database(world):
    """The reason the node mints its own token.

    In the textbook device flow the SERVER mints it, so it has to sit somewhere
    readable between "approve" and the node's next poll. That would be the one
    place a live credential could be read back out of this database, and the
    purge tooling means dumps get taken.
    """
    admin, node, path = world
    r, raw = _start(node)
    admin.post(f"/v1/pair/{r.json()['user_code']}/approve")
    node.get("/v1/whoami", headers={"Authorization": f"Bearer {raw}"})
    blob = b"".join(f.read_bytes() for f in pathlib.Path(path).parent.glob("pair.sqlite*"))
    assert raw.encode() not in blob, "the raw credential is on disk"
    assert hashlib.sha256(raw.encode()).hexdigest().encode() in blob


def test_only_the_machine_that_asked_can_poll(world):
    admin, node, _ = world
    r, raw = _start(node)
    code = r.json()["user_code"]
    other, _h = _token()
    wrong = _poll(node, code, other)
    unknown = _poll(node, "ZZZZ-ZZZZ", raw)
    nobody = node.post("/v1/pair/poll", json={"user_code": code})
    # identical answers, so the endpoint cannot be used to find live codes
    assert wrong.status_code == unknown.status_code == nobody.status_code == 404
    assert wrong.json() == unknown.json() == nobody.json()


def test_a_denied_machine_gets_nothing(world):
    admin, node, _ = world
    r, raw = _start(node)
    code = r.json()["user_code"]
    assert admin.post(f"/v1/pair/{code}/deny").json() == {"status": "denied"}
    assert _poll(node, code, raw).json()["status"] == "denied"
    assert node.get("/v1/whoami",
                    headers={"Authorization": f"Bearer {raw}"}).json()["scope"] == "none"
    # and a second click cannot turn a denial into an approval
    assert admin.post(f"/v1/pair/{code}/approve").json() == {"status": "denied"}


@pytest.mark.parametrize("who", ["operator", "viewer", "anonymous"])
def test_only_an_admin_may_approve(world, who):
    """A credential that could approve machines could mint credentials."""
    admin, node, _ = world
    r, _raw = _start(node)
    code = r.json()["user_code"]
    other = TestClient(admin.app)
    if who != "anonymous":
        admin.post("/v1/users", json={"username": who, "password": "another-long-one",
                                      "role": who})
        other.post("/v1/auth/login", json={"username": who, "password": "another-long-one"})
    assert other.post(f"/v1/pair/{code}/approve").status_code in (401, 403)
    assert other.get("/v1/pair/pending").status_code in (401, 403)


def test_a_machine_token_cannot_approve_another_machine(world):
    admin, node, _ = world
    r1, raw1 = _start(node, name="node-a")
    admin.post(f"/v1/pair/{r1.json()['user_code']}/approve")
    r2, _ = _start(node, name="node-b")
    assert node.post(f"/v1/pair/{r2.json()['user_code']}/approve",
                     headers={"Authorization": f"Bearer {raw1}"}).status_code in (401, 403)


def test_a_request_expires(world, monkeypatch):
    admin, node, _ = world
    monkeypatch.setattr(db, "PAIRING_TTL_SECONDS", 600)
    r, raw = _start(node)
    code = r.json()["user_code"]
    conn_now = db._now
    monkeypatch.setattr(db, "_now", lambda: conn_now() + 601)
    assert admin.post(f"/v1/pair/{code}/approve").status_code == 410
    assert _poll(node, code, raw).json()["status"] == "expired"
    assert admin.get("/v1/pair/pending").json()["pending"] == []


def test_a_name_with_a_live_credential_is_refused_at_the_node(world):
    """Told to the person at the machine, not discovered by the admin later."""
    admin, node, _ = world
    r, _ = _start(node, name="taken")
    admin.post(f"/v1/pair/{r.json()['user_code']}/approve")
    again, _ = _start(node, name="taken")
    assert again.status_code == 409 and "Revoke" in again.json()["detail"]


def test_a_revoked_machine_can_pair_again_under_its_own_name(world):
    admin, node, _ = world
    r, raw_old = _start(node, name="phoenix")
    admin.post(f"/v1/pair/{r.json()['user_code']}/approve")
    admin.delete("/v1/workers/tokens/phoenix")
    r2, raw_new = _start(node, name="phoenix")
    assert r2.status_code == 200
    admin.post(f"/v1/pair/{r2.json()['user_code']}/approve")
    auth = lambda t: {"Authorization": f"Bearer {t}"}          # noqa: E731
    assert node.get("/v1/whoami", headers=auth(raw_new)).json()["machine"] == "phoenix"
    assert node.get("/v1/whoami", headers=auth(raw_old)).json()["scope"] == "none"


def test_rerunning_setup_replaces_the_earlier_request(world):
    admin, node, _ = world
    r1, raw1 = _start(node, name="box")
    r2, _raw2 = _start(node, name="box")
    pending = admin.get("/v1/pair/pending").json()["pending"]
    assert [p["user_code"] for p in pending] == [r2.json()["user_code"]]
    assert _poll(node, r1.json()["user_code"], raw1).json()["status"] == "superseded"


@pytest.mark.parametrize("name", ["", "a b", "x'); alert(1)//", "<img src=x>", "-lead", "n" * 65,
                                  "../etc", "naïve"])
def test_the_name_is_a_strict_charset(world, name):
    """It comes from an unauthenticated machine and lands in an admin's browser."""
    _admin, node, _ = world
    _raw, h = _token()
    assert node.post("/v1/pair/start", json={"name": name, "token_hash": h}).status_code == 422


def test_the_hash_must_be_a_sha256(world):
    _admin, node, _ = world
    for bad in ("", "abc", "G" * 64, "a" * 63, "A" * 64):
        assert node.post("/v1/pair/start",
                         json={"name": "n", "token_hash": bad}).status_code == 422, bad


def test_pairing_requests_are_throttled_per_address(world):
    _admin, node, _ = world
    codes = [_start(node, name=f"n{i}")[0].status_code for i in range(14)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]


def test_the_queue_is_bounded(world, monkeypatch):
    _admin, node, _ = world
    monkeypatch.setattr(db, "MAX_PENDING_PAIRINGS", 2)
    assert [_start(node, name=f"q{i}")[0].status_code for i in range(3)] == [200, 200, 429]


def test_a_paired_credential_still_obeys_the_lease_rule(world):
    """Paired as `lab`, so it may lease as `lab` or `lab-7`, never as `phoenix`."""
    admin, node, _ = world
    r, raw = _start(node, name="lab")
    admin.post(f"/v1/pair/{r.json()['user_code']}/approve")
    h = {"Authorization": f"Bearer {raw}"}
    assert node.post("/v1/lease", json={"worker_id": "lab-7"}, headers=h).status_code == 200
    assert node.post("/v1/lease", json={"worker_id": "phoenix"}, headers=h).status_code == 403


def test_the_code_is_forgiving_about_how_it_is_typed(world):
    admin, node, _ = world
    r, raw = _start(node)
    code = r.json()["user_code"]
    for typed in (code.lower(), code.replace("-", ""), f" {code} "):
        assert _poll(node, typed, raw).status_code == 200, typed

"""Accounts, sessions and per-machine tokens over HTTP.

What this replaces: one shared secret in an environment variable that every
machine carried, that a human pasted into a browser to see the dashboard, and
that could only be revoked by rotating every machine and redeploying. Each test
below pins a property that model could not provide.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app


@pytest.fixture()
def fresh(tmp_path):
    """A brand-new deployment: no accounts, no env tokens. This is the state a
    first-run visitor arrives in."""
    app = create_app(db_path=str(tmp_path / "auth.sqlite"), tokens=[], readonly_tokens=[])
    return TestClient(app)


@pytest.fixture()
def legacy(tmp_path):
    """A deployment still carrying the OLD shared env token, which is how
    production looks right now."""
    app = create_app(db_path=str(tmp_path / "legacy.sqlite"), tokens=["shared-secret"])
    return TestClient(app)


PW = "a-sufficiently-long-passphrase"


# -- first run ----------------------------------------------------------------

def test_a_fresh_deployment_asks_to_be_set_up(fresh):
    st = fresh.get("/v1/auth/state").json()
    assert st["needs_setup"] is True
    assert st["user"] is None


def test_setup_creates_the_admin_and_logs_them_straight_in(fresh):
    r = fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "ada"
    # The response set a session cookie, so the very next call is authenticated
    # without anyone pasting anything.
    assert fresh.get("/v1/auth/state").json()["user"] == "ada"
    assert fresh.get("/v1/auth/state").json()["needs_setup"] is False


def test_setup_closes_permanently_after_the_first_account(fresh):
    """It is the one endpoint that cannot require auth, so it must shut itself."""
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    r = fresh.post("/v1/auth/setup", json={"username": "mallory", "password": PW})
    assert r.status_code == 409
    assert "log in" in r.json()["detail"]


def test_setup_refuses_a_short_password(fresh):
    r = fresh.post("/v1/auth/setup", json={"username": "ada", "password": "short"})
    assert r.status_code == 422 or r.status_code == 400


# -- login --------------------------------------------------------------------

def test_login_then_logout(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/auth/logout")
    assert fresh.get("/v1/auth/state").json()["user"] is None

    r = fresh.post("/v1/auth/login", json={"username": "ada", "password": PW})
    assert r.status_code == 200
    assert fresh.get("/v1/auth/state").json()["user"] == "ada"

    fresh.post("/v1/auth/logout")
    assert fresh.get("/v1/auth/state").json()["user"] is None


def test_a_wrong_password_is_rejected(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/auth/logout")
    r = fresh.post("/v1/auth/login", json={"username": "ada", "password": "wrong-password"})
    assert r.status_code == 401


def test_a_wrong_username_says_the_same_thing_as_a_wrong_password(fresh):
    """The message must not enumerate accounts."""
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/auth/logout")
    bad_user = fresh.post("/v1/auth/login", json={"username": "nobody", "password": PW})
    bad_pw = fresh.post("/v1/auth/login", json={"username": "ada", "password": "nope-not-this"})
    assert bad_user.status_code == bad_pw.status_code == 401
    assert bad_user.json()["detail"] == bad_pw.json()["detail"]


def test_the_session_cookie_is_not_readable_by_javascript(fresh):
    r = fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


# -- a session replaces pasting a worker token into a browser -----------------

def test_a_logged_in_human_can_read_the_campaign_without_any_token(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert fresh.get("/v1/status").status_code == 200
    assert fresh.get("/v1/cases").status_code == 200


def test_an_account_closes_a_deployment_that_had_no_env_tokens(fresh):
    """Before setup this service is open (that is what /healthz calls OPEN).
    Creating an account must end that, or the setup step would leave the door
    exactly as wide as it found it."""
    assert fresh.get("/v1/status").status_code == 200      # open
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/auth/logout")
    assert fresh.get("/v1/status").status_code == 401      # closed


# -- per-machine tokens -------------------------------------------------------

def test_issuing_a_machine_token_shows_it_exactly_once(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    r = fresh.post("/v1/workers/tokens", json={"name": "lab-ws-02"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token

    # Listing shows the machine but never the credential again: only its hash
    # was stored.
    listed = fresh.get("/v1/workers/tokens").json()["tokens"]
    assert [t["name"] for t in listed] == ["lab-ws-02"]
    assert token not in r.request.url.__str__()
    assert all(token not in str(t) for t in listed)


def test_a_machine_token_authenticates_a_worker(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    token = fresh.post("/v1/workers/tokens", json={"name": "lab-ws-02"}).json()["token"]
    fresh.post("/v1/auth/logout")

    # No session now -- the bearer token alone must carry a worker.
    assert fresh.get("/v1/status").status_code == 401
    r = fresh.get("/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_revoking_one_machine_takes_effect_immediately_and_spares_the_rest(fresh):
    """The property the shared secret could not offer: revoke one box without
    touching the others, and without a redeploy."""
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    keep = fresh.post("/v1/workers/tokens", json={"name": "keeper"}).json()["token"]
    drop = fresh.post("/v1/workers/tokens", json={"name": "stolen"}).json()["token"]

    assert fresh.delete("/v1/workers/tokens/stolen").status_code == 200
    # Drop the admin session first: with one, the request is authorised as the
    # HUMAN and would pass whatever the bearer token says -- which is correct
    # behaviour, and would hide whether revocation actually took.
    fresh.post("/v1/auth/logout")

    assert fresh.get("/v1/status", headers={"Authorization": f"Bearer {drop}"}).status_code == 401
    assert fresh.get("/v1/status", headers={"Authorization": f"Bearer {keep}"}).status_code == 200


def test_reissuing_for_the_same_machine_is_refused(fresh):
    """Silently minting a second credential would strand whichever one the box
    is actually using."""
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/workers/tokens", json={"name": "lab-ws-02"})
    r = fresh.post("/v1/workers/tokens", json={"name": "lab-ws-02"})
    assert r.status_code == 409
    assert "revoke it first" in r.json()["detail"]


def test_a_machine_token_cannot_mint_more_machine_tokens(fresh):
    """A worker credential that could issue credentials would defeat the whole
    point of issuing them per machine."""
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    token = fresh.post("/v1/workers/tokens", json={"name": "lab-ws-02"}).json()["token"]
    fresh.post("/v1/auth/logout")

    hdr = {"Authorization": f"Bearer {token}"}
    assert fresh.post("/v1/workers/tokens", json={"name": "sneaky"}, headers=hdr).status_code == 401
    assert fresh.get("/v1/workers/tokens", headers=hdr).status_code == 401
    assert fresh.delete("/v1/workers/tokens/lab-ws-02", headers=hdr).status_code == 401


def test_revoking_a_machine_that_has_no_token_is_a_404(fresh):
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert fresh.delete("/v1/workers/tokens/never-existed").status_code == 404


# -- the transition -----------------------------------------------------------

def test_the_old_shared_env_token_keeps_working(legacy):
    """The fleet is running on one of these right now. An auth change that
    stranded live workers mid-lease would be a worse outcome than carrying both
    models for a while."""
    r = legacy.get("/v1/status", headers={"Authorization": "Bearer shared-secret"})
    assert r.status_code == 200
    assert legacy.get("/v1/status").status_code == 401


def test_setup_is_still_offered_on_a_deployment_that_has_env_tokens(legacy):
    """Otherwise the only way to adopt accounts would be to first remove the
    credential the running fleet depends on.

    Offered, but no longer to ANYONE: a broker already holding a write token
    makes the operator present it. See the takeover test below for why.
    """
    assert legacy.get("/v1/auth/state").json()["needs_setup"] is True
    assert legacy.get("/v1/auth/state").json()["env_tokens"] is True
    assert legacy.post("/v1/auth/setup",
                       json={"username": "ada", "password": PW},
                       headers={"Authorization": "Bearer shared-secret"}
                       ).status_code == 200


def test_a_stranger_cannot_claim_the_admin_account_on_a_token_secured_broker(legacy):
    """The shape production was actually in: CASEBROKER_WRITE_TOKENS set, no
    account yet. /v1/auth/setup cannot require a session -- there is nobody to
    log in as -- so it used to hand the permanent admin account, and with it the
    power to mint machine credentials, to the first anonymous caller who found
    the form."""
    r = legacy.post("/v1/auth/setup",
                    json={"username": "mallory", "password": PW})
    assert r.status_code == 403
    assert "write token" in r.json()["detail"]
    assert legacy.get("/v1/auth/state").json()["needs_setup"] is True


def test_a_read_token_cannot_claim_the_admin_account(tmp_path):
    """A credential that cannot change the campaign must not be able to create
    the account that can."""
    app = create_app(db_path=str(tmp_path / "ro.sqlite"),
                     tokens=["write-secret"], readonly_tokens=["read-secret"])
    c = TestClient(app)
    r = c.post("/v1/auth/setup", json={"username": "mallory", "password": PW},
               headers={"Authorization": "Bearer read-secret"})
    assert r.status_code == 403


def test_setup_stays_open_when_the_deployment_has_no_credential_at_all(fresh):
    """A laptop, or a broker behind a firewall, has nothing to present."""
    assert fresh.get("/v1/auth/state").json()["setup_token_required"] is False
    assert fresh.post("/v1/auth/setup",
                      json={"username": "ada", "password": PW}).status_code == 200


def test_a_non_ascii_credential_does_not_500_an_unauthenticated_endpoint(legacy):
    """hmac.compare_digest refuses a non-ASCII str with TypeError, and the
    bearer header is attacker-chosen -- the server decodes it as latin-1, so any
    byte becomes a character. One \xe9 used to 500 /healthz, which needs no
    credential to reach and which the uptime badge polls."""
    header = {"Authorization": "Bearer caf\xe9".encode("latin-1")}
    assert legacy.get("/healthz", headers=header).status_code == 200
    assert legacy.get("/v1/whoami", headers=header).json()["scope"] == "none"
    assert legacy.get("/v1/status", headers=header).status_code == 401

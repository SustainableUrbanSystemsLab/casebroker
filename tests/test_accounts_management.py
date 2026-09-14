"""Accounts beyond the first one, and telling the truth about auth posture.

Two gaps this pins shut. The first is that a deployment had exactly one account
for its whole life: /v1/auth/setup closed permanently after the first success and
nothing else could create, delete, or repassword an account, so a forgotten
password meant raw SQL against production and a second operator was impossible.

The second is that `role` was stored, returned by every auth response, and read
by nothing -- so a "viewer" was a full admin -- and that /healthz and /v1/whoami
judged the auth posture from the environment token buckets ALONE. A broker
secured entirely by accounts therefore reported `auth: OPEN` and handed
`scope: write` to any string at all, which is precisely backwards, and it broke
both documented deploy gates (`casebroker health`, `casebroker token check`)
along with the per-machine worker bootstrap that runs the latter.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
PW2 = "another-sufficiently-long-one"


@pytest.fixture()
def fresh(tmp_path):
    """No accounts, no env tokens -- what a first-run visitor arrives at."""
    return TestClient(create_app(db_path=str(tmp_path / "a.sqlite"),
                                 tokens=[], readonly_tokens=[]))


@pytest.fixture()
def admin(fresh):
    """Set up and logged in as the first admin."""
    r = fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert r.status_code == 200, r.text
    return fresh


def _login(client, username, password):
    return client.post("/v1/auth/login",
                       json={"username": username, "password": password})


# -- a second operator --------------------------------------------------------

def test_an_admin_can_create_a_second_account(admin):
    r = admin.post("/v1/users", json={"username": "bob", "password": PW2})
    assert r.status_code == 200, r.text
    assert r.json() == {"username": "bob", "role": "viewer"}
    assert {u["username"] for u in admin.get("/v1/users").json()["users"]} == {"ada", "bob"}


def test_a_new_account_is_a_viewer_unless_admin_is_asked_for(admin):
    """The default leans to the lesser privilege: adding a colleague to watch
    the campaign should not silently hand over the keys."""
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/users", json={"username": "cleo", "password": PW2, "role": "admin"})
    roles = {u["username"]: u["role"] for u in admin.get("/v1/users").json()["users"]}
    assert roles == {"ada": "admin", "bob": "viewer", "cleo": "admin"}


def test_a_duplicate_username_is_refused(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    r = admin.post("/v1/users", json={"username": "bob", "password": PW2})
    assert r.status_code == 409


def test_an_unknown_role_is_refused(admin):
    r = admin.post("/v1/users", json={"username": "bob", "password": PW2, "role": "root"})
    assert r.status_code == 400


def test_the_second_account_can_actually_log_in(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    r = _login(admin, "bob", PW2)
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


# -- what a viewer may and may not do ----------------------------------------

def test_a_viewer_can_read_the_campaign(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    assert admin.get("/v1/status").status_code == 200


def test_a_viewer_cannot_change_the_campaign(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    r = admin.post("/v1/lease", json={"worker_id": "w", "count": 1})
    assert r.status_code == 403


def test_a_viewer_cannot_mint_machine_credentials(admin):
    """The role column existed from the start and authorised nothing, so a
    'viewer' could issue worker tokens -- which would make the role decorative."""
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    assert admin.post("/v1/workers/tokens", json={"name": "box"}).status_code == 403
    assert admin.get("/v1/users").status_code == 403


def test_a_viewer_cannot_create_accounts(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    r = admin.post("/v1/users", json={"username": "eve", "password": PW2, "role": "admin"})
    assert r.status_code == 403


# -- passwords ----------------------------------------------------------------

def test_you_can_change_your_own_password_and_stay_logged_in(admin):
    r = admin.post("/v1/users/ada/password",
                   json={"current_password": PW, "new_password": PW2})
    assert r.status_code == 200, r.text
    # The session survives -- set_password revokes every session, so the
    # endpoint has to re-issue the caller's or it would log you out of the tab
    # you changed it in.
    assert admin.get("/v1/users").status_code == 200
    admin.post("/v1/auth/logout")
    assert _login(admin, "ada", PW2).status_code == 200
    assert _login(admin, "ada", PW).status_code == 401


def test_changing_your_own_password_requires_the_current_one(admin):
    r = admin.post("/v1/users/ada/password",
                   json={"current_password": "wrong-one-entirely", "new_password": PW2})
    assert r.status_code == 403
    r = admin.post("/v1/users/ada/password", json={"new_password": PW2})
    assert r.status_code == 403


def test_an_admin_can_reset_a_forgotten_password_without_knowing_it(admin):
    """The recovery path that did not exist: previously a forgotten password
    meant editing the database by hand."""
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    r = admin.post("/v1/users/bob/password", json={"new_password": "a-brand-new-passphrase"})
    assert r.status_code == 200, r.text
    admin.post("/v1/auth/logout")
    assert _login(admin, "bob", "a-brand-new-passphrase").status_code == 200


def test_a_viewer_cannot_reset_someone_elses_password(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    r = admin.post("/v1/users/ada/password", json={"new_password": "yet-another-passphrase"})
    assert r.status_code == 403


def test_a_password_change_revokes_that_accounts_other_sessions(tmp_path):
    """The reason to change a password in a hurry is that someone else may have
    it; leaving their fortnight-long session alive would make the change
    cosmetic."""
    app = create_app(db_path=str(tmp_path / "b.sqlite"), tokens=[], readonly_tokens=[])
    owner, thief = TestClient(app), TestClient(app)
    owner.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert _login(thief, "ada", PW).status_code == 200
    assert thief.get("/v1/users").status_code == 200
    owner.post("/v1/users/ada/password",
               json={"current_password": PW, "new_password": PW2})
    assert thief.get("/v1/users").status_code == 401


def test_a_short_new_password_is_refused(admin):
    r = admin.post("/v1/users/ada/password",
                   json={"current_password": PW, "new_password": "short"})
    assert r.status_code == 422


# -- not locking everyone out -------------------------------------------------

def test_the_last_admin_cannot_be_deleted(admin):
    r = admin.delete("/v1/users/ada")
    assert r.status_code == 409
    assert "only admin" in r.json()["detail"]


def test_the_last_admin_cannot_be_demoted(admin):
    r = admin.post("/v1/users/ada/role", json={"role": "viewer"})
    assert r.status_code == 409


def test_an_admin_can_be_deleted_once_another_exists(admin):
    admin.post("/v1/users", json={"username": "cleo", "password": PW2, "role": "admin"})
    assert admin.delete("/v1/users/ada").status_code == 200
    assert _login(admin, "ada", PW).status_code == 401


def test_promoting_and_demoting(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    assert admin.post("/v1/users/bob/role", json={"role": "admin"}).status_code == 200
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    assert admin.get("/v1/users").status_code == 200      # bob is an admin now


def test_deleting_an_account_ends_its_sessions(tmp_path):
    app = create_app(db_path=str(tmp_path / "c.sqlite"), tokens=[], readonly_tokens=[])
    boss, bob = TestClient(app), TestClient(app)
    boss.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    boss.post("/v1/users", json={"username": "bob", "password": PW2})
    _login(bob, "bob", PW2)
    assert bob.get("/v1/status").status_code == 200
    boss.delete("/v1/users/bob")
    assert bob.get("/v1/status").status_code == 401


def test_deleting_an_unknown_account_is_a_404(admin):
    assert admin.delete("/v1/users/nobody").status_code == 404


# -- telling the truth about the auth posture ---------------------------------

def test_healthz_does_not_call_an_account_secured_broker_open(fresh):
    assert fresh.get("/healthz").json()["auth"] == "OPEN"
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    body = fresh.get("/healthz").json()
    # Was "OPEN" here, which made `casebroker health` -- documented as a deploy
    # gate that exits non-zero when auth is off -- fail against a broker that
    # was in fact locked down.
    assert body["auth"] == "accounts"
    assert body["accounts"] is True


def test_healthz_still_reports_token_posture_and_never_a_value(tmp_path):
    app = create_app(db_path=str(tmp_path / "d.sqlite"), tokens=["w1"], readonly_tokens=["r1"])
    body = TestClient(app).get("/healthz")
    assert body.json()["auth"] == "token"
    assert "w1" not in body.text and "r1" not in body.text


def test_whoami_stops_calling_an_account_secured_broker_open(fresh):
    assert fresh.get("/v1/whoami").json()["scope"] == "write"     # genuinely open
    fresh.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    fresh.post("/v1/auth/logout")
    anon = fresh.get("/v1/whoami", headers={"Authorization": "Bearer garbage"}).json()
    assert anon["scope"] == "none"


def test_whoami_recognises_a_session(admin):
    body = admin.get("/v1/whoami").json()
    assert body == {"scope": "write", "auth": "session", "user": "ada", "role": "admin"}


def test_whoami_reports_a_viewer_as_read_only(admin):
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")
    _login(admin, "bob", PW2)
    assert admin.get("/v1/whoami").json()["scope"] == "read"


def test_whoami_recognises_a_per_machine_token(admin):
    """The bug that broke the documented worker onboarding end to end:
    setup_windows.ps1 runs `casebroker token check --expect write`, which asks
    this endpoint, and a dashboard-issued machine token came back
    `scope: none` -- so the bootstrap aborted on the credential the docs told
    you to use."""
    issued = admin.post("/v1/workers/tokens", json={"name": "lab-box"}).json()["token"]
    admin.post("/v1/auth/logout")
    body = admin.get("/v1/whoami", headers={"Authorization": f"Bearer {issued}"}).json()
    assert body["scope"] == "write"
    assert body["auth"] == "machine"
    assert body["machine"] == "lab-box"


def test_whoami_reports_a_revoked_machine_token_as_none(admin):
    issued = admin.post("/v1/workers/tokens", json={"name": "lab-box"}).json()["token"]
    admin.delete("/v1/workers/tokens/lab-box")
    admin.post("/v1/auth/logout")     # else the session cookie answers first
    body = admin.get("/v1/whoami", headers={"Authorization": f"Bearer {issued}"}).json()
    assert body["scope"] == "none"


def test_whoami_agrees_with_the_gates_for_every_principal(admin):
    """The property that matters: whoami must not claim a capability the real
    dependencies refuse, for any credential shape."""
    issued = admin.post("/v1/workers/tokens", json={"name": "box"}).json()["token"]
    admin.post("/v1/users", json={"username": "bob", "password": PW2})
    admin.post("/v1/auth/logout")

    for headers, cookies in [({"Authorization": f"Bearer {issued}"}, None),
                             ({"Authorization": "Bearer nonsense"}, None)]:
        scope = admin.get("/v1/whoami", headers=headers).json()["scope"]
        wrote = admin.post("/v1/lease", json={"worker_id": "w", "count": 1},
                           headers=headers)
        assert (wrote.status_code != 401 and wrote.status_code != 403) == (scope == "write")


# -- the first-run race -------------------------------------------------------

def test_setup_needs_the_bootstrap_secret_when_one_is_configured(tmp_path):
    """/v1/auth/setup cannot require a session -- there is nobody to
    authenticate as yet -- so on a public deployment the first stranger to find
    it becomes the permanent sole admin. CASEBROKER_SETUP_TOKEN closes that."""
    app = create_app(db_path=str(tmp_path / "e.sqlite"), tokens=[], readonly_tokens=[],
                     setup_token="the-bootstrap-secret")
    c = TestClient(app)
    assert c.get("/v1/auth/state").json()["setup_token_required"] is True
    assert c.post("/v1/auth/setup",
                  json={"username": "mallory", "password": PW}).status_code == 403
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW,
                                       "setup_token": "the-bootstrap-secret"})
    assert r.status_code == 200, r.text


def test_the_bootstrap_secret_is_also_accepted_as_a_bearer_header(tmp_path):
    app = create_app(db_path=str(tmp_path / "f.sqlite"), tokens=[], readonly_tokens=[],
                     setup_token="the-bootstrap-secret")
    c = TestClient(app)
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW},
               headers={"Authorization": "Bearer the-bootstrap-secret"})
    assert r.status_code == 200, r.text


def test_without_a_bootstrap_secret_first_run_stays_open(fresh):
    """A laptop or a broker behind a firewall should not need one."""
    assert fresh.get("/v1/auth/state").json()["setup_token_required"] is False
    assert fresh.post("/v1/auth/setup",
                      json={"username": "ada", "password": PW}).status_code == 200


# -- online guessing ----------------------------------------------------------

def test_repeated_failed_logins_are_throttled(admin):
    admin.post("/v1/auth/logout")
    codes = [_login(admin, "ada", "wrong-password-here").status_code
             for _ in range(12)]
    assert 401 in codes and codes[-1] == 429


def test_the_throttle_does_not_touch_a_correct_password(admin):
    admin.post("/v1/auth/logout")
    for _ in range(3):
        _login(admin, "ada", "wrong-password-here")
    assert _login(admin, "ada", PW).status_code == 200
    # A success clears the counter, so a later slip does not inherit it.
    admin.post("/v1/auth/logout")
    assert _login(admin, "ada", PW).status_code == 200


def test_the_setup_token_is_accepted_from_either_carrier_independently(tmp_path):
    """Giving the header precedence would mean a stale bearer token left in a
    client's config blocked a correct value in the body."""
    app = create_app(db_path=str(tmp_path / "g.sqlite"), tokens=[], readonly_tokens=[],
                     setup_token="the-bootstrap-secret")
    c = TestClient(app)
    r = c.post("/v1/auth/setup",
               json={"username": "ada", "password": PW,
                     "setup_token": "the-bootstrap-secret"},
               headers={"Authorization": "Bearer a-stale-unrelated-token"})
    assert r.status_code == 200, r.text


def test_a_wrong_setup_token_in_both_carriers_is_still_refused(tmp_path):
    app = create_app(db_path=str(tmp_path / "h.sqlite"), tokens=[], readonly_tokens=[],
                     setup_token="the-bootstrap-secret")
    c = TestClient(app)
    r = c.post("/v1/auth/setup",
               json={"username": "mallory", "password": PW, "setup_token": "wrong"},
               headers={"Authorization": "Bearer also-wrong"})
    assert r.status_code == 403

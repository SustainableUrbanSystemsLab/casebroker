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

    # Each credential leases as the identity it is entitled to. A machine token
    # leasing as ANOTHER machine is refused, but that is an identity check on
    # the payload, not a statement about the credential's scope -- conflating
    # the two here would make this test assert the opposite of what it means.
    for headers, worker_id in [({"Authorization": f"Bearer {issued}"}, "box"),
                               ({"Authorization": "Bearer nonsense"}, "box")]:
        scope = admin.get("/v1/whoami", headers=headers).json()["scope"]
        wrote = admin.post("/v1/lease", json={"worker_id": worker_id, "count": 1},
                           headers=headers)
        assert (wrote.status_code not in (401, 403)) == (scope == "write")


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


def test_the_login_throttle_holds_under_concurrent_attempts(admin):
    """Reading the counter, deciding, and recording a failure from separate
    critical sections would let N concurrent attempts all read the same
    under-limit count and sail past together. FastAPI runs sync endpoints in a
    threadpool, so that race is reachable."""
    import concurrent.futures as cf
    admin.post("/v1/auth/logout")
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        codes = [f.result().status_code for f in
                 [pool.submit(_login, admin, "ada", "wrong-password-here")
                  for _ in range(24)]]
    # However the 24 interleave, no more than the limit may be answered 401.
    assert codes.count(401) <= 10
    assert 429 in codes


def test_a_non_ascii_setup_token_is_refused_rather_than_crashing(tmp_path):
    app = create_app(db_path=str(tmp_path / "na.sqlite"), tokens=[], readonly_tokens=[],
                     setup_token="the-bootstrap-secret")
    c = TestClient(app)
    r = c.post("/v1/auth/setup",
               json={"username": "ada", "password": PW, "setup_token": "café-☕"})
    assert r.status_code == 403


def test_a_viewer_cookie_does_not_veto_a_valid_write_token(tmp_path):
    """A viewer's cookie rides along on every request from that browser.
    Rejecting on sight refused a request that also carried a perfectly good
    write credential, so the viewer session vetoed a stronger one."""
    app = create_app(db_path=str(tmp_path / "veto.sqlite"),
                     tokens=["a-shared-write-secret"], readonly_tokens=[])
    c = TestClient(app)
    c.post("/v1/auth/setup", json={"username": "ada", "password": PW},
           headers={"Authorization": "Bearer a-shared-write-secret"})
    c.post("/v1/users", json={"username": "bob", "password": PW2})
    c.post("/v1/auth/logout")
    assert _login(c, "bob", PW2).status_code == 200      # bob's cookie is now set

    # Same request: viewer cookie AND a valid write token.
    r = c.post("/v1/lease", json={"worker_id": "w", "count": 1},
               headers={"Authorization": "Bearer a-shared-write-secret"})
    assert r.status_code == 200, r.text
    # Without the token the viewer is still refused, and told why.
    r = c.post("/v1/lease", json={"worker_id": "w", "count": 1})
    assert r.status_code == 403
    assert "viewer" in r.json()["detail"]


def test_healthz_survives_the_accounts_query_failing(tmp_path, monkeypatch):
    """Every database touch on /healthz has to fail SOFT. An unguarded account
    count made it answer 500 during an outage instead of reporting the outage --
    and the Dockerfile HEALTHCHECK reads a 500 as unhealthy, so a database blip
    would restart-loop a broker that is itself fine."""
    import casebroker.app as appmod
    from casebroker import db as dbmod

    app = create_app(db_path=str(tmp_path / "outage.sqlite"),
                     tokens=["tok"], readonly_tokens=[])
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/healthz").status_code == 200

    def dead(*a, **k):
        raise RuntimeError("connection is closed")

    monkeypatch.setattr(dbmod, "count_users", dead)
    # Jump past the 30s probe cache, or the pre-outage answer is simply replayed.
    real_monotonic = appmod.time.monotonic
    monkeypatch.setattr(appmod.time, "monotonic",
                        lambda: real_monotonic() + 10_000)

    r = c.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["accounts"] is None        # unknowable, not guessed


def test_healthz_says_unknown_rather_than_open_when_it_cannot_tell(tmp_path, monkeypatch):
    """Reporting OPEN when the account query failed would raise a false alarm
    about auth during what is really a database problem -- and `casebroker
    health` exits non-zero on OPEN."""
    import casebroker.app as appmod
    from casebroker import db as dbmod

    app = create_app(db_path=str(tmp_path / "unknown.sqlite"),
                     tokens=[], readonly_tokens=[])
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/healthz").json()["auth"] == "OPEN"

    monkeypatch.setattr(dbmod, "count_users",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("closed")))
    real_monotonic = appmod.time.monotonic
    monkeypatch.setattr(appmod.time, "monotonic",
                        lambda: real_monotonic() + 10_000)
    assert c.get("/healthz").json()["auth"] == "unknown"


def test_a_machine_token_cannot_lease_as_a_different_machine(admin):
    """The dashboard has always said the token name "must match the machine's
    CASEBROKER_WORKER_ID" and nothing enforced it, so every row in the Machines
    list and the workers table was a claim rather than a fact -- which is the
    attribution that issuing one credential per box exists to provide."""
    issued = admin.post("/v1/workers/tokens", json={"name": "lab-ws-02"}).json()["token"]
    admin.post("/v1/auth/logout")
    headers = {"Authorization": f"Bearer {issued}"}

    assert admin.post("/v1/lease", json={"worker_id": "lab-ws-02", "count": 1},
                      headers=headers).status_code == 200
    r = admin.post("/v1/lease", json={"worker_id": "someone-elses-box", "count": 1},
                   headers=headers)
    assert r.status_code == 403
    assert "lab-ws-02" in r.json()["detail"]


def test_a_shared_env_token_may_still_lease_as_any_worker(tmp_path):
    """Env tokens are shared BY DESIGN -- one value across the fleet -- so there
    is no machine identity for a worker id to contradict. Enforcing there would
    strand every worker the live campaign is running on."""
    app = create_app(db_path=str(tmp_path / "shared.sqlite"), tokens=["shared-secret"])
    c = TestClient(app)
    headers = {"Authorization": "Bearer shared-secret"}
    for worker_id in ("phoenix-01", "ice-07", "lab-ws-02"):
        assert c.post("/v1/lease", json={"worker_id": worker_id, "count": 1},
                      headers=headers).status_code == 200


def test_a_cluster_credential_covers_every_worker_id_under_its_name(admin):
    """SLURM names the workers, not the admin: every task runs as
    `phoenix-<job>-<task>` (slurm/phoenix_worker.sbatch), so a credential per
    task is impossible, and an exact-match rule left clusters on the shared
    token forever -- the un-revocable, un-attributed model per-machine
    credentials exist to replace. A credential covers its own name and
    everything under it. The dash is load-bearing."""
    issued = admin.post("/v1/workers/tokens", json={"name": "phoenix"}).json()["token"]
    admin.post("/v1/auth/logout")
    headers = {"Authorization": f"Bearer {issued}"}

    def lease_as(worker_id):
        return admin.post("/v1/lease", json={"worker_id": worker_id, "count": 1},
                          headers=headers).status_code

    assert lease_as("phoenix") == 200
    assert lease_as("phoenix-1234567-3") == 200
    assert lease_as("phoenixville") == 403      # a longer name is a different machine
    assert lease_as("ice-1234567-3") == 403     # another cluster entirely
    assert lease_as("lab-phoenix") == 403       # under, not merely containing


def test_a_worker_whose_credential_is_refused_exits_instead_of_spinning(admin, monkeypatch):
    """A 401 or 403 on lease was caught by the same handler as a network blip
    and retried every idle_backoff seconds for the whole SLURM walltime -- an
    array job holding twenty allocations with "[warn] lease failed: 403"
    scrolling past. Waiting does not fix a credential."""
    from casebroker import worker as wmod

    issued = admin.post("/v1/workers/tokens", json={"name": "lab-ws-02"}).json()["token"]
    admin.post("/v1/auth/logout")
    headers = {"Authorization": f"Bearer {issued}"}

    # Under the old behaviour this loop never ends, so bound it: a sleep is the
    # retry, and more than a couple of them is the bug reproduced.
    sleeps = []

    def counted_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise AssertionError("still retrying a refused credential after 3 backoffs")
    monkeypatch.setattr(wmod.time, "sleep", counted_sleep)

    w = wmod.Worker("http://testserver", None, worker_id="someone-elses-box",
                    heartbeat_seconds=3600)
    w._post = lambda path, payload, retries=4: admin.post(path, json=payload, headers=headers)

    def runner(lease, worker):
        raise AssertionError("no lease should ever have been handed out")

    with pytest.raises(wmod.CredentialRefused) as refused:
        w.run_forever(runner, idle_backoff=0, max_idle_polls=5)
    assert "403" in str(refused.value) and "lab-ws-02" in str(refused.value)
    assert sleeps == [], "a refused credential must not be retried at all"


def test_worker_main_exits_2_on_a_refused_credential(monkeypatch):
    """The exit code is the deliverable: it is what makes an sbatch log end
    with `exit=2` and the sentence that explains it, instead of the job
    holding its nodes to the wall clock."""
    from casebroker import worker as wmod

    def refuse(self, *args, **kwargs):
        raise wmod.CredentialRefused("broker refused this credential (403): nope")
    monkeypatch.setattr(wmod.Worker, "run_forever", refuse)

    assert wmod.main(["--broker", "http://broker.invalid", "--worker-id", "w"]) == 2


def test_a_revoked_machine_name_can_be_issued_again(admin):
    """The documented recovery for a box that has lost its credential --
    `casebroker worker setup --rotate`, and Revoke then Issue in the dashboard
    -- is revoke-then-reissue. It answered 409: revoke MARKS the row rather
    than deleting it (so last_seen_at and who issued it survive), and
    UNIQUE(name) then refused the re-issue too. The 409 even said "revoke it
    first", advice that could not succeed. A machine could therefore never get
    a working credential back under its own worker id -- and the worker id is
    what /v1/lease now enforces."""
    first = admin.post("/v1/workers/tokens", json={"name": "lab-ws-02"}).json()["token"]

    # While it is live, re-issuing is still refused: replacing it silently would
    # strand whichever token the box is actually running on.
    clash = admin.post("/v1/workers/tokens", json={"name": "lab-ws-02"})
    assert clash.status_code == 409
    assert "live credential" in clash.json()["detail"]

    assert admin.delete("/v1/workers/tokens/lab-ws-02").status_code == 200
    again = admin.post("/v1/workers/tokens", json={"name": "lab-ws-02"})
    assert again.status_code == 200, again.text
    second = again.json()["token"]
    assert second != first

    # One row per machine still, and not reported as long-lost: inheriting the
    # revoked token's last_seen_at would show a machine as alive on the strength
    # of a credential that no longer works.
    rows = [t for t in admin.get("/v1/workers/tokens").json()["tokens"]
            if t["name"] == "lab-ws-02"]
    assert len(rows) == 1 and rows[0]["revoked_at"] is None
    assert rows[0]["last_seen_at"] is None

    # Log out BEFORE judging the tokens: the session cookie this client still
    # carries authenticates a request on its own, so a revoked bearer token
    # would answer 200 here and prove nothing.
    admin.post("/v1/auth/logout")
    assert admin.post("/v1/lease", json={"worker_id": "lab-ws-02", "count": 1},
                      headers={"Authorization": f"Bearer {second}"}).status_code == 200
    assert admin.post("/v1/lease", json={"worker_id": "lab-ws-02", "count": 1},
                      headers={"Authorization": f"Bearer {first}"}).status_code == 401


# -- the operator role: writes the campaign, manages nothing ------------------
#
# Before it existed, `admin` was the only role that could write, so "let this
# person run the campaign" and "let this person delete every account including
# yours" were the same grant. Every one of these was previously impossible to
# express.

def _as_operator(admin, username="bob"):
    """An operator account, and a client logged in as them.

    A SEPARATE TestClient, because the admin fixture's client carries the admin
    session cookie -- reusing it would authorise every call below as the admin
    and prove nothing about the role.
    """
    r = admin.post("/v1/users", json={"username": username, "password": PW2,
                                      "role": "operator"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "operator"
    c = TestClient(admin.app)
    assert c.post("/v1/auth/login",
                  json={"username": username, "password": PW2}).status_code == 200
    return c


def _seed(client, n=2):
    cases = [{"lat": 35.0 + i, "lon": 139.0 + i, "recipe": "v2-wind",
              "city_cluster": "tokyo"} for i in range(n)]
    r = client.post("/v1/cases", json=cases)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_operator_can_run_the_campaign(admin):
    """The whole point of the role: the daily work, with no admin rights."""
    op = _as_operator(admin)

    assert op.get("/v1/status").status_code == 200
    assert op.get("/v1/cases").status_code == 200
    _seed(op, 2)

    leased = op.post("/v1/lease", json={"worker_id": "ws-01", "count": 1}).json()
    assert len(leased) == 1
    lease_id = leased[0]["lease_id"]
    assert op.post("/v1/heartbeat", json={"lease_id": lease_id, "detail": "iter 1"}).status_code == 200
    assert op.post("/v1/complete", json={"lease_id": lease_id,
                                         "result_uri": "file:///r.tar.gz"}).status_code == 200

    second = op.post("/v1/lease", json={"worker_id": "ws-01", "count": 1}).json()
    assert op.post("/v1/release", json={"lease_id": second[0]["lease_id"]}).status_code == 200
    assert op.post("/v1/fleet", json={"cluster": "lab", "queued": 3, "running": 1}).status_code == 200


def test_an_operator_cannot_purge_the_campaign(admin):
    """DELETE /v1/cases takes the cases, their events and their footprints. It
    is the one campaign operation with nothing behind it, so it stays with the
    people who manage the deployment rather than the people who run it."""
    op = _as_operator(admin)
    _seed(op, 2)

    r = op.delete("/v1/cases?dry_run=false&expect=2")
    assert r.status_code == 403
    assert "operator" in r.json()["detail"] and "admin" in r.json()["detail"]
    # A dry run is still a delete call, and is refused at the same gate --
    # reporting what WOULD be deleted is not a privilege an operator has.
    assert op.delete("/v1/cases").status_code == 403
    assert admin.get("/v1/cases").json()["total"] == 2, "nothing may have been removed"


def test_an_operator_manages_neither_accounts_nor_machines(admin):
    """An operator that could mint a machine credential would be an admin with
    extra steps: the credential can write the campaign and outlives the account
    that issued it, so revoking the person would not revoke what they left."""
    op = _as_operator(admin)

    assert op.get("/v1/users").status_code == 403
    assert op.post("/v1/users", json={"username": "mallory", "password": PW2,
                                      "role": "admin"}).status_code == 403
    assert op.post("/v1/users/ada/role", json={"role": "viewer"}).status_code == 403
    assert op.delete("/v1/users/ada").status_code == 403

    assert op.get("/v1/workers/tokens").status_code == 403
    assert op.post("/v1/workers/tokens", json={"name": "sneaky"}).status_code == 403
    assert op.delete("/v1/workers/tokens/anything").status_code == 403


def test_an_operator_can_still_change_their_own_password(admin):
    """Managing nobody else must not mean being unable to manage yourself."""
    op = _as_operator(admin)
    r = op.post("/v1/users/bob/password",
                json={"current_password": PW2, "new_password": "a-brand-new-long-one"})
    assert r.status_code == 200, r.text
    # And still not anyone else's.
    assert op.post("/v1/users/ada/password",
                   json={"new_password": "not-your-account-to-reset"}).status_code == 403


def test_a_viewer_is_unchanged_by_the_new_role(admin):
    """The middle role must not have quietly widened the bottom one."""
    admin.post("/v1/users", json={"username": "val", "password": PW2, "role": "viewer"})
    v = TestClient(admin.app)
    assert v.post("/v1/auth/login", json={"username": "val", "password": PW2}).status_code == 200

    assert v.get("/v1/status").status_code == 200
    r = v.post("/v1/cases", json=[{"lat": 1.0, "lon": 2.0, "recipe": "v2-wind",
                                   "city_cluster": "tokyo"}])
    assert r.status_code == 403 and "viewer" in r.json()["detail"]
    assert v.delete("/v1/cases").status_code == 403
    assert v.get("/v1/users").status_code == 403


def test_promoting_an_operator_to_admin_grants_the_rest(admin):
    """Roles have to be a live check, not something baked into the session at
    login: the promotion must take effect without the account signing in again."""
    op = _as_operator(admin)
    assert op.get("/v1/users").status_code == 403

    assert admin.post("/v1/users/bob/role", json={"role": "admin"}).status_code == 200
    assert op.get("/v1/users").status_code == 200
    assert op.post("/v1/workers/tokens", json={"name": "now-allowed"}).status_code == 200


def test_the_last_admin_cannot_be_demoted_to_operator_either(admin):
    """The guard counted admins, and an operator is not one -- but a third role
    is exactly the kind of change that turns a two-way check into a hole."""
    r = admin.post("/v1/users/ada/role", json={"role": "operator"})
    assert r.status_code == 409
    assert "only admin" in r.json()["detail"]
    assert admin.get("/v1/users").status_code == 200, "ada must still be an admin"

    # With a second admin it is allowed, which is what makes the guard a guard
    # rather than a permanent ban.
    admin.post("/v1/users", json={"username": "cleo", "password": PW2, "role": "admin"})
    assert admin.post("/v1/users/ada/role", json={"role": "operator"}).status_code == 200


def test_an_unknown_role_is_refused(admin):
    r = admin.post("/v1/users", json={"username": "x", "password": PW2, "role": "superuser"})
    assert r.status_code == 400
    assert "operator" in r.json()["detail"], "the message should name the real roles"


def test_auth_state_publishes_the_roles_the_broker_accepts(fresh):
    """The dashboard's role pickers are built from this. Restating the list in
    the page would let the two drift, and the drift would surface as a 400 at
    the moment someone is trying to add a colleague."""
    from casebroker import db
    assert fresh.get("/v1/auth/state").json()["roles"] == list(db.ROLES)
    assert "operator" in db.ROLES


def test_a_write_token_may_still_purge(tmp_path):
    """The deliberate carve-out. A machine or env write token could always call
    this, and narrowing it here would strand the documented curl in
    operations.md without making anything safer -- the holder can simply use the
    token. What changed is that an operator SESSION is not enough."""
    app = create_app(db_path=str(tmp_path / "purge.sqlite"), tokens=["shared-secret"])
    c = TestClient(app)
    hdr = {"Authorization": "Bearer shared-secret"}
    c.post("/v1/cases", json=[{"lat": 1.0, "lon": 2.0, "recipe": "v2-wind",
                               "city_cluster": "tokyo"}], headers=hdr)
    r = c.request("DELETE", "/v1/cases?dry_run=false&expect=1", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1

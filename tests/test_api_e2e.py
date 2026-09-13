"""API and end-to-end tests.

The unit tests in ``test_db.py`` prove the SQL is right. These prove the thing
workers actually talk to is right, and the end-to-end test runs a REAL uvicorn
server with REAL worker clients over REAL HTTP -- because the failure this whole
component exists to prevent (two machines simulating the same case for six hours)
is a concurrency failure, and a concurrency failure is not observable in a test
that calls functions in one thread.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def broker(tmp_path):
    """A freshly-bound app per test. No module reloading, no shared globals --
    each test owns its database outright."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    application = create_app(db_path=str(tmp_path / "api.sqlite"),
                             tokens=["secret-a", "secret-b"])
    c = TestClient(application)
    c.headers.update({"Authorization": "Bearer secret-a"})
    return c


@pytest.fixture()
def client(broker):
    return broker


def _cases(n=5):
    return [{"lat": 40.7 + i * 0.01, "lon": -74.0, "recipe": "fixed-cyl-500/of12",
             "city_cluster": "nyc" if i < 3 else "chi", "lcz": "LCZ1",
             "spec": {"dirs": [0, 45, 90]}} for i in range(n)]


def test_auth_is_enforced(client):
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": ""}).status_code == 401
    # A second configured token is equally valid -- rotation without downtime.
    assert client.post("/v1/lease", json={"worker_id": "w"},
                       headers={"Authorization": "Bearer secret-b"}).status_code == 200


def test_healthz_says_whether_auth_is_on(client):
    body = client.get("/healthz").json()
    assert body["ok"] and body["auth"] == "token"


def test_add_lease_complete_roundtrip(client):
    assert client.post("/v1/cases", json=_cases(5)).json() == {"added": 5, "skipped": 0}
    # Re-posting the identical list adds nothing: this is the "grow the dataset"
    # path, and it must be safe to run twice.
    assert client.post("/v1/cases", json=_cases(5)).json() == {"added": 0, "skipped": 5}

    got = client.post("/v1/lease", json={"worker_id": "w1", "count": 2}).json()
    assert len(got) == 2
    assert all(g["spec"]["dirs"] == [0, 45, 90] for g in got)

    r = client.post("/v1/complete", json={
        "lease_id": got[0]["lease_id"], "result_uri": "s3://b/x.npz",
        "bytes": 123, "metrics": {"cells": 1858222}})
    assert r.status_code == 200

    st = client.get("/v1/status").json()
    assert st["by_state"]["done"] == 1
    case = client.get(f"/v1/cases/{got[0]['case_id']}").json()
    assert case["state"] == "done" and case["result_bytes"] == 123


def test_superseded_lease_is_rejected_with_409(client):
    client.post("/v1/cases", json=_cases(1))
    got = client.post("/v1/lease", json={"worker_id": "w1", "lease_seconds": 60}).json()[0]
    # Forcibly expire, then let another worker take it.
    from casebroker import db as dbm
    con = dbm.connect(client.app.state.db_path)
    con.execute("UPDATE cases SET lease_expires = 0")
    other = client.post("/v1/lease", json={"worker_id": "w2"}).json()[0]
    assert other["case_id"] == got["case_id"]

    for path, payload in (
        ("/v1/heartbeat", {"lease_id": got["lease_id"]}),
        ("/v1/complete", {"lease_id": got["lease_id"], "result_uri": "x"}),
        ("/v1/fail", {"lease_id": got["lease_id"], "error": "late"}),
        ("/v1/release", {"lease_id": got["lease_id"]}),
    ):
        assert client.post(path, json=payload).status_code == 409, path


def test_splits_are_assigned_by_city_not_by_tile(client):
    """Geographic leakage guard: every tile of one city lands in one split."""
    client.post("/v1/cases", json=_cases(5))
    from casebroker import db as dbm
    rows = dbm.connect(client.app.state.db_path).execute(
        "SELECT city_cluster, COUNT(DISTINCT split) d FROM cases GROUP BY city_cluster"
    ).fetchall()
    assert rows and all(r["d"] == 1 for r in rows), \
        "a city's tiles were split across train/test -- that leaks geometry"


def test_lease_reports_host_and_cluster_through_to_the_workers_table(client):
    """A dashboard viewer asking "what machine produced this" reads the workers
    table, which is only useful if /v1/lease actually threads host/cluster down
    to db.lease() rather than dropping them on the floor at the API boundary."""
    client.post("/v1/cases", json=_cases(1))
    client.post("/v1/lease", json={"worker_id": "phx-w1", "host": "atl1-1-02-005",
                                   "cluster": "phoenix-slurm"})
    workers = client.get("/v1/status").json()["workers"]
    w = next(w for w in workers if w["worker_id"] == "phx-w1")
    assert (w["host"], w["cluster"]) == ("atl1-1-02-005", "phoenix-slurm")


def test_lease_without_host_or_cluster_still_works(client):
    """Older worker builds, or a worker run by hand, must not be rejected just
    because they do not know their own machine."""
    client.post("/v1/cases", json=_cases(1))
    r = client.post("/v1/lease", json={"worker_id": "w1"})
    assert r.status_code == 200 and len(r.json()) == 1


def test_list_cases_endpoint_is_gated_and_paginated(client):
    client.post("/v1/cases", json=_cases(5))

    assert client.get("/v1/cases", headers={"Authorization": ""}).status_code == 401

    page = client.get("/v1/cases?limit=2").json()
    assert page["total"] == 5
    assert len(page["cases"]) == 2

    rest = client.get("/v1/cases?limit=2&offset=2").json()
    assert len(rest["cases"]) == 2
    ids_seen = {c["case_id"] for c in page["cases"]} | {c["case_id"] for c in rest["cases"]}
    assert len(ids_seen) == 4, "two non-overlapping pages should cover four distinct cases"


def test_list_cases_endpoint_filters_by_state(client):
    client.post("/v1/cases", json=_cases(3))
    got = client.post("/v1/lease", json={"worker_id": "w1"}).json()[0]
    client.post("/v1/complete", json={"lease_id": got["lease_id"], "result_uri": "x"})

    done = client.get("/v1/cases?state=done").json()
    assert done["total"] == 1
    assert done["cases"][0]["case_id"] == got["case_id"]
    assert done["cases"][0]["state"] == "done"

    pending = client.get("/v1/cases?state=pending").json()
    assert pending["total"] == 2


def test_a_completed_cases_metrics_name_the_worker_host_and_result_location(client):
    """End-to-end through the real worker, not just the API: run_forever's
    metrics enrichment is what actually answers "what machine produced this
    case and where did it end up" on a finished case."""
    from casebroker.worker import Worker

    client.post("/v1/cases", json=_cases(1))
    w = Worker("http://testserver", None, worker_id="w1", host="node-7",
              cluster="ice-slurm", heartbeat_seconds=3600)
    w.http = client
    w._post = lambda path, payload, retries=4: client.post(path, json=payload)

    def runner(lease, worker):
        return {"result_uri": "file:///results/" + lease["case_id"], "bytes": 42,
                "metrics": {"stage": "solve-only"}}

    w.run_forever(runner, idle_backoff=0, max_idle_polls=1)

    done = client.get("/v1/cases?state=done").json()["cases"]
    assert len(done) == 1
    case = done[0]
    assert case["result_uri"].startswith("file:///results/")
    import json as _json
    metrics = _json.loads(case["metrics"])
    assert metrics["worker"] == "w1"
    assert metrics["host"] == "node-7"
    assert metrics["cluster"] == "ice-slurm"


# -- real server, real workers, real HTTP -------------------------------------

@pytest.mark.timeout(120) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_many_workers_never_duplicate_a_case(tmp_path, monkeypatch):
    import uvicorn
    import httpx

    os.environ["CASEBROKER_FAKE_SECONDS"] = "0.01"
    from casebroker.app import create_app
    from casebroker.worker import Worker, echo_runner
    application = create_app(db_path=str(tmp_path / "e2e.sqlite"), tokens=["tok"])

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                           log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("server did not come up")

    n_cases, n_workers = 60, 8
    cases = [{"lat": 40.0 + i * 0.001, "lon": -74.0, "recipe": "r",
              "city_cluster": f"c{i % 7}", "spec": {}} for i in range(n_cases)]
    r = httpx.post(base + "/v1/cases", json=cases,
                   headers={"Authorization": "Bearer tok"}, timeout=30)
    assert r.json()["added"] == n_cases

    done_by: list[tuple[str, str]] = []
    lock = threading.Lock()

    def run_worker(k):
        w = Worker(base, "tok", worker_id=f"w{k}", lease_seconds=120,
                   heartbeat_seconds=3600)

        def runner(lease, worker):
            out = echo_runner(lease, worker)
            with lock:
                done_by.append((lease["case_id"], f"w{k}"))
            return out
        w.run_forever(runner, idle_backoff=1, max_idle_polls=3)

    threads = [threading.Thread(target=run_worker, args=(k,)) for k in range(n_workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)

    # Ask the server everything BEFORE stopping it -- a status query after
    # should_exit is a connection-refused, not a finding.
    st = httpx.get(base + "/v1/status", headers={"Authorization": "Bearer tok"},
                   timeout=10).json()
    server.should_exit = True
    t.join(timeout=15)

    ids_done = [c for c, _ in done_by]
    dupes = {c for c in ids_done if ids_done.count(c) > 1}
    assert not dupes, f"{len(dupes)} case(s) simulated more than once: {sorted(dupes)[:5]}"
    assert len(set(ids_done)) == n_cases, (
        f"only {len(set(ids_done))}/{n_cases} cases were simulated")
    assert st["by_state"].get("done") == n_cases
    # More than one worker actually participated, or the test proved nothing
    # about concurrency.
    assert len({w for _, w in done_by}) > 1


def test_dashboard_is_served_with_no_auth_but_data_stays_gated(broker):
    """The dashboard shell carries no secrets -- it prompts for a token client-side and
    calls the JSON API with it, exactly like any other API client. So the page itself
    must load with no Authorization header, while the data it displays stays behind
    the same check as every other endpoint.

    The ``broker`` fixture pins ``Authorization: Bearer secret-a`` as a default header
    for every request it sends (that is what makes the OTHER tests in this file
    convenient to write), so this test overrides it to "" per-call to actually
    exercise the unauthenticated path rather than accidentally re-proving the
    authenticated one.
    """
    r = broker.get("/", headers={"Authorization": ""})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Wind v2 Case Broker</title>" in r.text
    # The page's own fetch() calls carry the token; the page load itself must not.
    assert "secret-a" not in r.text and "secret-b" not in r.text

    # And the data it calls stays protected -- unchanged behaviour, just confirmed
    # from the same fixture the dashboard test lives beside.
    assert broker.get("/v1/status", headers={"Authorization": ""}).status_code == 401
    assert broker.get("/v1/status").status_code == 200   # the fixture's default token


def test_dashboard_token_field_is_discoverable_as_a_password_field(broker):
    """A browser's password manager only offers to save a credential when it
    sees type=password on an input that sits inside a real <form> with a
    submit control -- a bare input, even one typed "password", is frequently
    not enough on its own. Pinned so a future edit cannot silently drop the
    <form> wrapper or the autocomplete hint and lose that behaviour."""
    html = broker.get("/", headers={"Authorization": ""}).text
    assert '<form id="connectForm"' in html
    assert 'id="token"' in html and 'type="password"' in html
    assert 'autocomplete="current-password"' in html
    # The token field must actually be INSIDE the form, not merely present
    # somewhere on the page.
    form_start = html.index('<form id="connectForm"')
    form_end = html.index("</form>", form_start)
    assert 'id="token"' in html[form_start:form_end]
    assert 'type="submit"' in html[form_start:form_end], \
        "a submit control is part of what makes a browser recognise this as a login form"


def test_dashboard_auto_refresh_defaults_to_checked(broker):
    """auto-refresh must be ON out of the box, not something a first-time
    visitor has to notice and enable."""
    html = broker.get("/", headers={"Authorization": ""}).text
    i = html.index('id="autoRefresh"')
    tag = html[html.rindex("<", 0, i):html.index(">", i) + 1]
    assert "checked" in tag
    assert "60" in tag or "every 60s" in html


def test_dashboard_serves_a_case_browser(broker):
    """The click-through case list this dashboard exists to provide: a table
    of cases plus the id-lookup fallback, both wired to real element ids the
    script binds event listeners to."""
    html = broker.get("/", headers={"Authorization": ""}).text
    for needed in ('id="casesTable"', 'id="casesBody"', 'id="caseState"',
                  'id="casesPrev"', 'id="casesNext"', 'id="caseId"', 'id="lookup"'):
        assert needed in html, needed


def test_dashboard_notification_toggle_is_present_and_off_by_default(broker):
    """Desktop notifications are opt-IN: the browser only grants permission from
    a user gesture, and a dashboard that asked on load would be denied by default
    in every modern browser AND be obnoxious. So the checkbox must exist, must be
    unchecked, and the request must hang off its change event."""
    html = broker.get("/", headers={"Authorization": ""}).text
    i = html.index('id="notifyDone"')
    tag = html[html.rindex("<", 0, i):html.index(">", i) + 1]
    assert "checked" not in tag, "notifications must be opt-in, not on by default"
    assert 'id="notifyLabel"' in html
    # requestPermission has to be reachable from the toggle's own handler.
    assert "requestNotifyPermission" in html
    assert "Notification.requestPermission()" in html


def test_dashboard_notifier_baselines_before_it_announces_anything(broker):
    """The failure this guards against: enabling notifications on a campaign with
    5,000 finished cases and being told about all of them. The first poll after
    enabling only records what is already done; only what finishes AFTER that is
    news -- so the enable path must reset the baseline to null, and the poll must
    return early when it is."""
    html = broker.get("/", headers={"Authorization": ""}).text
    assert "notifySeen = null" in html
    assert "if (notifySeen === null)" in html


# -- read-only tokens: a link you can safely send to a friend ----------------

@pytest.fixture()
def scoped_broker(tmp_path):
    """A broker with BOTH a full worker token and a separate read-only one --
    the shape a real deployment uses once a share link exists."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    application = create_app(db_path=str(tmp_path / "ro.sqlite"),
                             tokens=["worker-secret"], readonly_tokens=["friend-link-token"])
    return TestClient(application)


def test_readonly_token_can_read_but_not_write(scoped_broker):
    ro = {"Authorization": "Bearer friend-link-token"}
    full = {"Authorization": "Bearer worker-secret"}
    scoped_broker.post("/v1/cases", json=_cases(2), headers=full)

    # Every read endpoint the dashboard actually calls: accessible.
    assert scoped_broker.get("/v1/status", headers=ro).status_code == 200
    assert scoped_broker.get("/v1/cases", headers=ro).status_code == 200
    cid = scoped_broker.get("/v1/cases", headers=full).json()["cases"][0]["case_id"]
    assert scoped_broker.get(f"/v1/cases/{cid}", headers=ro).status_code == 200

    # Every mutating endpoint: rejected outright -- not merely hidden by the
    # dashboard UI, actually refused by the API a curl could hit directly.
    for method, path, payload in (
        ("post", "/v1/cases", _cases(1)),
        ("post", "/v1/lease", {"worker_id": "sneaky"}),
        ("post", "/v1/heartbeat", {"lease_id": "x"}),
        ("post", "/v1/complete", {"lease_id": "x", "result_uri": "y"}),
        ("post", "/v1/fail", {"lease_id": "x", "error": "y"}),
        ("post", "/v1/release", {"lease_id": "x"}),
    ):
        r = getattr(scoped_broker, method)(path, json=payload, headers=ro)
        assert r.status_code == 401, f"{path} should reject a read-only token, got {r.status_code}"


def test_full_token_still_does_everything_a_readonly_token_cannot(scoped_broker):
    """Adding read-only tokens must not narrow what the worker token can do."""
    full = {"Authorization": "Bearer worker-secret"}
    assert scoped_broker.post("/v1/cases", json=_cases(1), headers=full).status_code == 200
    assert scoped_broker.post("/v1/lease", json={"worker_id": "w1"}, headers=full).status_code == 200
    assert scoped_broker.get("/v1/status", headers=full).status_code == 200


def test_readonly_only_deployment_locks_out_writes_rather_than_opening_them(tmp_path):
    """A deployment that configures ONLY readonly_tokens (no worker tokens at
    all -- an unusual but real config someone could reach for) must not fall
    back to "no tokens configured means auth is off" for writes. Writes stay
    locked no matter what is presented, because nothing is in `tokens`."""
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    application = create_app(db_path=str(tmp_path / "rounly.sqlite"),
                             tokens=[], readonly_tokens=["only-a-viewer"])
    c = TestClient(application)
    ro = {"Authorization": "Bearer only-a-viewer"}
    assert c.get("/v1/status", headers=ro).status_code == 200
    assert c.post("/v1/lease", json={"worker_id": "w"}, headers=ro).status_code == 401
    # And the read-only token itself cannot be used as if it were a write
    # token even by accident -- there is no code path where it validates
    # against `tokens`.
    assert c.post("/v1/lease", json={"worker_id": "w"},
                  headers={"Authorization": "Bearer only-a-viewer"}).status_code == 401


def test_healthz_discloses_whether_a_readonly_tier_exists(scoped_broker, tmp_path):
    body = scoped_broker.get("/healthz").json()
    assert body["auth"] == "token"
    assert body["readonly_auth"] is True

    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    plain = TestClient(create_app(db_path=str(tmp_path / "plain.sqlite"), tokens=["only-one"]))
    assert plain.get("/healthz").json()["readonly_auth"] is False


def test_dashboard_serves_a_shareable_readonly_deep_link(broker):
    """"send this to a friend" means a URL of the form ?token=...&ro=1 that
    pre-fills the token field, connects with no typing, and shows a banner --
    pinned as markup/script behaviour here since there is no browser in this
    test process to actually load the page and click through it. The actual
    read-only ENFORCEMENT is server-side (see test_readonly_token_can_read_but_not_write);
    this only checks that the page can consume the link that feature exists for."""
    html = broker.get("/", headers={"Authorization": ""}).text
    assert 'id="roBanner"' in html
    assert "URLSearchParams(location.search)" in html
    assert 'urlParams.get("token")' in html
    assert 'urlParams.get("ro")' in html
    assert "history.replaceState" in html, \
        "the token must not be left sitting in the visible address bar"
    # And the operator-side half: a way to actually GENERATE such a link.
    # The read-only token is no longer pasted into the page: the dashboard asks
    # the broker for it (GET /v1/share-token, write-auth) and builds the link.
    assert 'id="copyRoLink"' in html
    assert "/v1/share-token" in html
    assert 'id="copyRoLink"' in html


def test_dashboard_has_a_favicon(broker):
    """A bookmarked/shared dashboard tab is otherwise indistinguishable from
    every other blank-icon browser tab -- pinned so a future edit to <head>
    cannot silently drop it."""
    html = broker.get("/", headers={"Authorization": ""}).text
    head = html[:html.index("</head>")]
    assert 'rel="icon"' in head


def test_redact_db_target_masks_only_the_password():
    """/healthz is deliberately unauthenticated so infrastructure health checks
    work with no token -- which is exactly why nothing it returns may ever carry
    a credential. Found the hard way: an early version returned CASEBROKER_DB
    verbatim, so hitting /healthz against a real Postgres deployment printed the
    live database password in plain text, no auth required to trigger it.

    A fake DSN is used here on purpose -- this test must never construct or
    reference a real credential."""
    from casebroker.app import _redact_db_target
    fake = "postgresql://appuser:hunter2-not-a-real-secret@db.example.com:6543/postgres"
    out = _redact_db_target(fake)
    assert "hunter2-not-a-real-secret" not in out
    assert out == "postgresql://appuser:***@db.example.com:6543/postgres"
    # The parts that are NOT secret stay visible -- healthz still needs to
    # answer "which database is this even pointed at".
    assert "db.example.com" in out and "appuser" in out


def test_redact_db_target_passes_a_sqlite_path_through_unchanged(tmp_path):
    """A local file path is not a secret; redaction must not mangle it."""
    from casebroker.app import _redact_db_target
    p = str(tmp_path / "campaign.sqlite")
    assert _redact_db_target(p) == p


def test_healthz_route_actually_calls_the_redaction_helper(tmp_path, monkeypatch):
    """The bug was never in the redaction function alone -- it was the ROUTE
    returning db_path directly instead of routing through it. A SQLite path here
    (no network needed at all) with a monkeypatched spy proves the wiring, not
    just the helper."""
    from fastapi.testclient import TestClient
    from casebroker import app as app_module
    calls = []
    monkeypatch.setattr(app_module, "_redact_db_target",
                        lambda s: calls.append(s) or "REDACTED-FOR-TEST")
    db_path = str(tmp_path / "x.sqlite")
    application = app_module.create_app(db_path=db_path, tokens=None)
    body = TestClient(application).get("/healthz").json()
    assert body["db"] == "REDACTED-FOR-TEST"
    assert calls == [db_path]


def test_healthz_reports_database_reachability_separately_from_liveness(tmp_path):
    """A broker that started but cannot reach its database must not read healthy.

    /healthz is what the uptime badge polls, and it answered only "did the
    process start?" -- which it does perfectly well against a database it cannot
    authenticate to. The badge then reads "live" for a broker that could not have
    served a single case, and nothing outside the service can tell the two apart.

    `ok` deliberately stays True in the broken case: the PROCESS is up, and
    collapsing the two fields would leave a reader unable to distinguish
    "service down" from "database down", which is the whole point of db_ok.
    """
    import casebroker.db as dbmod
    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    real_connect = dbmod.connect

    class LosesTheDatabase:
        """Real connection for setup, dead for the health probe."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if sql.strip() == "select 1":
                raise RuntimeError("server closed the connection unexpectedly")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    healthy = create_app(str(tmp_path / "ok.sqlite"), ["w"], ["r"])
    with TestClient(healthy) as c:
        assert c.get("/healthz").json()["db_ok"] is True

    dbmod.connect = lambda target: LosesTheDatabase(real_connect(target))
    try:
        broken = create_app(str(tmp_path / "dead.sqlite"), ["w"], ["r"])
    finally:
        dbmod.connect = real_connect

    with TestClient(broken) as c:
        body = c.get("/healthz").json()
        # The failure is reported, not raised: a probe that 500s tells an
        # operator less than one that answers False, and tells a badge nothing.
        assert body["db_ok"] is False
        assert body["ok"] is True


def test_healthz_never_discloses_why_the_database_is_unreachable(tmp_path):
    """The probe is unauthenticated, so the failure REASON must not leak.

    A connection error carries the host, the role and the TLS posture. None of
    that is ours to publish to anyone who can reach the service.
    """
    import casebroker.db as dbmod
    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    real_connect = dbmod.connect
    secret = "password authentication failed for user postgres.tenantref"

    class Leaky:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if sql.strip() == "select 1":
                raise RuntimeError(secret)
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    dbmod.connect = lambda target: Leaky(real_connect(target))
    try:
        app = create_app(str(tmp_path / "leaky.sqlite"), ["w"], ["r"])
    finally:
        dbmod.connect = real_connect

    with TestClient(app) as c:
        raw = c.get("/healthz").text
    assert "tenantref" not in raw and "authentication" not in raw


# -- purging a superseded campaign ------------------------------------------

def test_purge_is_a_dry_run_unless_the_destructive_form_is_asked_for(broker):
    """The default has to be the safe one. A half-remembered curl, or a client
    that drops an unfamiliar query parameter, must report what it WOULD delete
    rather than deleting it."""
    broker.post("/v1/cases", json=_cases(5))
    r = broker.delete("/v1/cases")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["deleted"] == 0
    assert body["matched"] >= 1, "the fixture campaign should have cases to match"
    assert broker.get("/v1/status").json()["by_state"], "nothing may have been deleted"


def test_purge_refuses_when_the_expected_count_disagrees(broker):
    """The interlock that matters. A filter that is subtly wrong -- a renamed
    recipe, a state spelled differently from the column -- otherwise deletes
    either everything or nothing, silently. Stating the expected row count turns
    that into a loud refusal with nothing touched."""
    broker.post("/v1/cases", json=_cases(5))
    matched = broker.delete("/v1/cases").json()["matched"]
    r = broker.delete(f"/v1/cases?dry_run=false&expect={matched + 1}")
    body = r.json()
    assert body["deleted"] == 0
    assert "refusing to delete" in body["error"]
    assert broker.get("/v1/cases").json()["total"] == matched, "campaign untouched"


def test_purge_deletes_cases_and_their_events_together(broker):
    """Events outliving their cases would corrupt every later count, so the
    delete is one transaction over both tables."""
    broker.post("/v1/cases", json=_cases(5))
    matched = broker.delete("/v1/cases").json()["matched"]
    r = broker.delete(f"/v1/cases?dry_run=false&expect={matched}")
    body = r.json()
    assert body["deleted"] == matched
    assert broker.get("/v1/cases").json()["total"] == 0
    st = broker.get("/v1/status").json()
    assert not st["by_state"], f"campaign should be empty, got {st['by_state']}"


def test_purge_can_be_scoped_to_one_recipe(broker):
    """Republishing under a new recipe name is the non-destructive path, so the
    destructive one has to be able to target exactly the superseded recipe and
    leave the new campaign alone."""
    broker.post("/v1/cases", json=_cases(5))
    before = broker.get("/v1/cases").json()["total"]
    r = broker.delete("/v1/cases?recipe=no-such-recipe&dry_run=false&expect=0")
    assert r.json()["deleted"] == 0
    assert broker.get("/v1/cases").json()["total"] == before

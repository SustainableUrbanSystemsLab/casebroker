"""Browser push: the broker tells a closed tab or a phone what happened.

What each test pins is a property the in-tab notifications could not have, or a
way push could hurt: a reader making the broker POST into the server's LAN, a
revoked link still being told the campaign, a condition announced every 30 s for
a day, a notice the claim lets two processes both send. tick() is driven
directly with a fake sender; one test runs the real pywebpush encryption and
decrypts what would have gone over the wire.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from casebroker import db, push
from casebroker.app import create_app
from dashboard_page import served_page

PW = "a-sufficiently-long-passphrase"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def browser(n: int = 1, host: str = "fcm.googleapis.com"):
    """What a browser's PushSubscription.toJSON() looks like, and its private key."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    sub = {"endpoint": f"https://{host}/fcm/send/device-{n}-{'a' * 40}",
           "keys": {"p256dh": b64(pub), "auth": b64(auth)}}
    return sub, key, auth


class Outbox:
    """A push service that answers ``status`` (an int, or a function of the row)."""

    def __init__(self, status=201):
        self.sent: list[tuple[str, dict, int, str]] = []
        self.status = status

    def __call__(self, sub, payload, *, ttl, urgency):
        self.sent.append((sub["endpoint"], payload, ttl, urgency))
        return self.status(sub) if callable(self.status) else self.status

    def kinds(self, endpoint=None):
        return [p["kind"] for e, p, _, _ in self.sent if endpoint in (None, e)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CASEBROKER_PUSH", "CASEBROKER_VAPID_PRIVATE_KEY", "CASEBROKER_VAPID_SUBJECT",
                 "CASEBROKER_PUSH_SILENT_MINUTES", "CASEBROKER_PUSH_STALL_HOURS", "CASEBROKER_PUSH_HOSTS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "push.sqlite"), tokens=["w-token"],
                      readonly_tokens=["r-token"])


@pytest.fixture()
def conn(app):
    return db.connect(app.state.db_path)


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w-token"})
    assert r.status_code == 200, r.text
    return c


def login_as(app, admin, username, role):
    assert admin.post("/v1/users", json={"username": username, "password": PW, "role": role}).status_code == 200
    c = TestClient(app)
    assert c.post("/v1/auth/login", json={"username": username, "password": PW}).status_code == 200
    return c


def reader(app):
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer r-token"})
    return c


def subscribe(client, sub, events=None, **extra):
    body = {"subscription": sub, **extra}
    if events is not None:
        body["events"] = events
    return client.post("/v1/push/subscriptions", json=body)


def add_cases(conn, n, prefix="c"):
    db.add_cases(conn, [{"case_id": f"{prefix}{i}", "spec": {"lat": 32.06, "lon": 118.79}, "recipe": "r",
                         "city_cluster": "x", "lcz": "LCZ1", "split": "train", "priority": 100,
                         "max_attempts": 3} for i in range(n)])


def later() -> int:
    """A tick's clock: past LAG_SECONDS, so the events just written are readable."""
    return int(time.time()) + push.LAG_SECONDS + 5


# -- subscribing ------------------------------------------------------------------

def test_a_reader_subscribes_reads_changes_and_removes_its_own_browser(app):
    c = reader(app)
    sub, _, _ = browser()
    r = subscribe(c, sub)
    assert r.status_code == 200, r.text
    # The defaults, less what a viewer may not have and what is off by default.
    assert r.json()["status"] == "created"
    assert r.json()["events"] == ["case_done", "case_quarantined", "worker_drained", "update_failed",
                                  "worker_unfit", "campaign_stalled", "queue_empty"]
    got = c.get("/v1/push/subscriptions", params={"endpoint": sub["endpoint"]}).json()
    assert got["events"] == r.json()["events"] and got["role"] == "viewer"
    assert sub["keys"]["auth"] not in json.dumps(got), "the keys are never handed back"

    put = c.put("/v1/push/subscriptions", json={"endpoint": sub["endpoint"],
                                                "events": ["queue_empty", "worker_silent", "nonsense"]})
    assert put.json()["events"] == ["worker_silent", "queue_empty"], "catalog order, unknown kinds dropped"

    assert subscribe(c, sub, ["case_done"]).json()["status"] == "updated", "the same endpoint replaces"
    assert c.request("DELETE", "/v1/push/subscriptions", json={"endpoint": sub["endpoint"]}).json() == {"deleted": True}
    assert c.get("/v1/push/subscriptions", params={"endpoint": sub["endpoint"]}).status_code == 404


def test_nobody_but_its_owner_can_see_change_test_or_remove_a_subscription(app, admin):
    sub, _, _ = browser()
    assert subscribe(reader(app), sub).status_code == 200
    for other in (admin, login_as(app, admin, "vic", "viewer")):
        assert other.get("/v1/push/subscriptions", params={"endpoint": sub["endpoint"]}).status_code == 404
        assert other.put("/v1/push/subscriptions", json={"endpoint": sub["endpoint"], "events": []}).status_code == 404
        assert other.post("/v1/push/test", json={"endpoint": sub["endpoint"]}).status_code == 404
        assert other.request("DELETE", "/v1/push/subscriptions",
                             json={"endpoint": sub["endpoint"]}).status_code == 404
    assert TestClient(app).post("/v1/push/subscriptions", json={"subscription": sub}).status_code == 401


def test_admin_only_kinds_are_dropped_silently_for_everyone_else(app, admin):
    everything = [k.key for k in push.EVENTS]
    a, _, _ = browser(1)
    assert subscribe(admin, a, everything).json()["events"] == everything
    v, _, _ = browser(2)
    got = subscribe(login_as(app, admin, "vic", "viewer"), v, everything).json()["events"]
    assert "pair_request" not in got and "store_nearly_full" not in got
    assert "case_done" in got and "worker_silent" in got
    catalog = {e["key"]: e for e in reader(app).get("/v1/push/events").json()["events"]}
    assert catalog["pair_request"]["allowed"] is False and catalog["case_done"]["allowed"] is True


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x" + "a" * 20,           # not https
    "https://192.168.1.1/admin/" + "a" * 20,                     # the server's LAN
    "https://169.254.169.254/latest/meta-data/" + "a" * 10,      # a cloud metadata address
    "https://localhost/v1/cases?" + "a" * 20,
    "https://casebroker.eddy3d.com/v1/cases/" + "a" * 20,
    "https://fcm.googleapis.com.evil.example/x" + "a" * 20,      # a suffix is not the host
    "https://evilpush.apple.com.example/x" + "a" * 20,
    "https://user:pw@fcm.googleapis.com/fcm/send/" + "a" * 20,   # credentials in the URL
    "https://fcm.googleapis.com:8443/fcm/send/" + "a" * 20,      # another port on the host
    "https://push.apple.com/x" + "a" * 20,                       # the wildcard needs a label
])
def test_an_endpoint_that_is_not_a_push_service_is_refused(app, endpoint):
    sub, _, _ = browser()
    sub["endpoint"] = endpoint
    r = subscribe(reader(app), sub)
    assert r.status_code == 422, (endpoint, r.text)


def test_the_push_services_browsers_use_are_accepted(monkeypatch):
    for ok in ("https://fcm.googleapis.com/fcm/send/abc", "https://updates.push.services.mozilla.com/wpush/v2/abc",
               "https://web.push.apple.com/QF7abc", "https://api.push.apple.com/3/device/abc",
               "https://wns2-par02p.notify.windows.com/w/?token=abc"):
        assert push.check_endpoint(ok) is None, ok
    assert push.check_endpoint("https://push.example.org/abc")
    monkeypatch.setenv("CASEBROKER_PUSH_HOSTS", "push.example.org, *.example.net")
    assert push.check_endpoint("https://push.example.org/abc") is None
    assert push.check_endpoint("https://a.example.net/abc") is None
    assert push.check_endpoint("https://example.net/abc")


def test_keys_that_are_not_a_browser_s_are_refused(app):
    sub, _, _ = browser()
    sub["keys"]["p256dh"] = b64(b"\x04" + b"x" * 40)
    assert subscribe(reader(app), sub).status_code == 422
    sub, _, _ = browser()
    sub["keys"]["auth"] = b64(b"x" * 15) + "AAAA"
    assert subscribe(reader(app), sub).status_code == 422


def test_a_rotated_subscription_takes_the_old_one_s_kinds_and_replaces_it(app):
    c = reader(app)
    old, _, _ = browser(1)
    subscribe(c, old, ["queue_empty"])
    new, _, _ = browser(2)
    r = subscribe(c, new, replaces=old["endpoint"])
    assert r.json()["events"] == ["queue_empty"]
    assert c.get("/v1/push/subscriptions", params={"endpoint": old["endpoint"]}).status_code == 404


def test_one_credential_cannot_fill_the_table(conn):
    def add(i, who="token:read:x"):
        sub, _, _ = browser(i)
        db.save_push_subscription(conn, sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], [],
                                  who.split(":", 1)[0], who.split(":", 1)[1], "viewer", None, now=1000 + i,
                                  per_subscriber=3, total=5)
        return sub["endpoint"]
    first = add(0)
    rest = [add(i) for i in range(1, 4)]
    held = {s["endpoint"] for s in db.push_subscriptions(conn)}
    assert held == set(rest) and first not in held, "the least recently used went"
    add(10, "user:bob"), add(11, "user:bob")
    with pytest.raises(ValueError):
        add(12, "user:eve")


# -- the broker-wide switches ----------------------------------------------------------

def test_an_admin_switches_a_kind_off_for_everyone_and_nobody_else_can(app, admin, conn):
    assert reader(app).put("/v1/push/policy", json={"events": {"case_done": False}}).status_code == 401
    assert login_as(app, admin, "op", "operator").put(
        "/v1/push/policy", json={"events": {"case_done": False}}).status_code == 403
    assert admin.put("/v1/push/policy", json={"events": {"bogus": False}}).status_code == 422
    r = admin.put("/v1/push/policy", json={"events": {"case_done": False}})
    assert r.status_code == 200
    states = {e["key"]: e["broker"] for e in r.json()["events"]}
    assert states["case_done"] is False and states["queue_empty"] is True
    audit = sqlite3.connect(app.state.db_path).execute(
        "SELECT worker_id, detail FROM events WHERE event = 'setting'").fetchall()
    assert any(who == "ada" and "push_policy" in d for who, d in audit), "the switch is audited"

    sub, _, _ = browser()
    subscribe(admin, sub, ["case_done", "case_quarantined"])
    push.tick(conn, later(), send=Outbox())                          # the cursor's baseline
    add_cases(conn, 2)
    a, b = db.lease(conn, "w1", count=2)
    db.complete(conn, a.lease_id, "file:///a", case_id=a.case_id)
    db.fail(conn, b.lease_id, "exit 64", retryable=False)
    out = Outbox()
    push.tick(conn, later(), send=out)
    assert out.kinds() == ["case_quarantined"], "case_done is switched off broker-wide"


def test_casebroker_push_0_switches_push_off(app, conn, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PUSH", "0")
    c = reader(app)
    assert c.get("/v1/push/key").json()["enabled"] is False
    assert subscribe(c, browser()[0]).status_code == 409
    assert push.tick(conn, later(), send=Outbox())["skipped"] == "switched off"


# -- what the events table turns into ------------------------------------------------

def test_tick_turns_the_events_db_records_into_notices(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, [k.key for k in push.EVENTS])
    out = Outbox()
    push.tick(conn, later(), send=out)
    assert out.sent == [], "the first tick starts from now: the campaign's history is not news"

    add_cases(conn, 4)                       # one stays pending: no "queue empty" here
    done, broken, _ = db.lease(conn, "w1", count=3, host="COD-1")
    db.complete(conn, done.lease_id, "file:///done", case_id=done.case_id)
    db.fail(conn, broken.lease_id, "geometry degenerate", retryable=False)
    db.create_pairing(conn, "ABCD2345", "COD-NEW", "f" * 64, host="lab-pc", platform="win-x64")
    db.node_release(conn, "w1", "win-x64", "1.0+aaa", failed_build="1.1+bbb", failed_reason="would not start")
    push.tick(conn, later(), send=out)

    by = {p["kind"]: (p, ttl, urgency) for _, p, ttl, urgency in out.sent}
    assert list(by) == ["pair_request", "case_done", "case_quarantined", "update_failed"], "catalog order"
    p, _, _ = by["case_done"]
    assert p["title"] == "Case finished" and done.case_id in p["body"] and "China" in p["body"]
    assert p["url"] == f"/#case={done.case_id}" and p["tag"] == "casebroker-done"
    p, _, _ = by["case_quarantined"]
    assert "geometry degenerate" in p["body"] and p["url"] == f"/#case={broken.case_id}"
    p, ttl, urgency = by["pair_request"]
    assert p["title"] == "COD-NEW asks to join" and "lab-pc" in p["body"]
    assert "ABCD" not in json.dumps(p), "the code is on the Machines tab, not in the notice"
    assert ttl == db.PAIRING_TTL_SECONDS and urgency == "high", "useless once the request lapses"
    p, _, urgency = by["update_failed"]
    assert "1.1+bbb" in p["body"] and "would not start" in p["body"] and urgency == "high"

    out2 = Outbox()
    push.tick(conn, later(), send=out2)
    assert out2.sent == [], "each event is announced once"


def test_the_broker_s_own_drain_is_announced_and_an_operator_s_is_not(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["worker_drained"])
    push.tick(conn, later(), send=Outbox())
    db.set_worker_drain(conn, "w0", True, "maintenance", by="ada")       # no row: nothing to drain
    add_cases(conn, db.FAIL_BURST_CASES)
    leases = db.lease(conn, "w-bad", count=db.FAIL_BURST_CASES)
    for got in leases:
        db.fail(conn, got.lease_id, "docker daemon is not running", retryable=True)
    db.set_worker_drain(conn, "w-bad", False, by="ada")
    db.set_worker_drain(conn, "w-bad", True, "looking at it", by="ada")
    out = Outbox()
    push.tick(conn, later(), send=out)
    assert out.kinds() == ["worker_drained"]
    p = out.sent[0][1]
    assert p["title"] == "Machine drained: w-bad" and "docker daemon" in p["body"]


def test_an_operator_s_own_sweep_is_not_announced_as_quarantines(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["case_quarantined"])
    push.tick(conn, later(), send=Outbox())
    add_cases(conn, 2)
    db.quarantine_not_on_land(conn, lambda lat, lon: False, dry_run=False)
    out = Outbox()
    push.tick(conn, later(), send=out)
    assert out.sent == []


def test_a_tick_s_finished_cases_arrive_as_one_batch(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["case_done"])
    push.tick(conn, later(), send=Outbox())
    add_cases(conn, 5)
    for got in db.lease(conn, "w1", count=5):
        db.complete(conn, got.lease_id, "file:///x", case_id=got.case_id)
    out = Outbox()
    push.tick(conn, later(), send=out)
    assert len(out.sent) == 1
    p = out.sent[0][1]
    assert p["title"] == "5 cases finished" and p["count"] == 5 and len(p["items"]) == 5
    assert p["body"].endswith("and 2 more") and p["url"] == "/#state=done"
    assert p["many"] == {"title": "{n} cases finished", "url": "/#state=done"}, "what sw.js folds with"
    assert p["renotify"] is False, "a second batch updates the first without a sound"
    assert len(json.dumps(p).encode()) < 3000, "well inside a push service's 4 KB"


def test_a_row_committed_late_is_not_skipped(app, conn):
    """A row stamped inside the lag stops the reader, even when a later id is older."""
    now = int(time.time())
    add_cases(conn, 1)
    start = db.push_snapshot(conn, now)["newest_event"]
    raw = sqlite3.connect(app.state.db_path)
    raw.execute("INSERT INTO events(ts, case_id, worker_id, event) VALUES (?,?,?,?)", (now - 2, "c0", "w", "done"))
    raw.execute("INSERT INTO events(ts, case_id, worker_id, event) VALUES (?,?,?,?)", (now - 60, "c0", "w", "done"))
    raw.commit()
    rows, cursor = db.push_events_after(conn, start, ["done"], before=now - push.LAG_SECONDS)
    assert rows == [] and cursor == start, "the younger row with the lower id holds the cursor"
    rows, cursor = db.push_events_after(conn, start, ["done"], before=now + 60)
    assert len(rows) == 2 and cursor == start + 2


# -- standing conditions: once, and again only after they clear ------------------------

def test_an_empty_queue_is_announced_once_and_again_after_it_refills(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["queue_empty"])
    add_cases(conn, 1)
    push.tick(conn, later(), send=Outbox())
    (got,) = db.lease(conn, "w1")
    db.complete(conn, got.lease_id, "file:///x", case_id=got.case_id)
    out = Outbox()
    push.tick(conn, later(), send=out)
    push.tick(conn, later(), send=out)
    assert out.kinds() == ["queue_empty"]
    add_cases(conn, 1, prefix="more")
    push.tick(conn, later(), send=out)
    (got,) = db.lease(conn, "w1")
    db.complete(conn, got.lease_id, "file:///y", case_id=got.case_id)
    push.tick(conn, later(), send=out)
    assert out.kinds() == ["queue_empty", "queue_empty"]


def test_a_silent_machine_is_announced_once_and_re_arms_when_it_is_heard_from(app, admin, conn, monkeypatch):
    sub, _, _ = browser()
    subscribe(admin, sub, ["worker_silent"])
    add_cases(conn, 2)
    t0 = int(time.time())
    (got,) = db.lease(conn, "COD-7", now=t0 - 3600)
    out = Outbox()
    push.tick(conn, t0, send=out)
    push.tick(conn, t0 + 30, send=out)
    assert out.kinds() == ["worker_silent"]
    p = out.sent[0][1]
    assert p["title"] == "Machine silent: COD-7" and got.case_id in p["body"] and "1 h" in p["body"]
    assert p["url"] == f"/#case={got.case_id}"
    db.heartbeat(conn, got.lease_id, now=t0 + 60)
    push.tick(conn, t0 + 90, send=out)
    assert out.kinds() == ["worker_silent"], "heard from again: cleared, nothing sent"
    push.tick(conn, t0 + 60 + push.silent_seconds() + 1, send=out)
    assert out.kinds() == ["worker_silent", "worker_silent"]


def test_the_part_store_nearly_full_fires_at_90_and_re_arms_below_85(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["store_nearly_full"])
    tb = 1024 ** 4

    def usage(used):
        return lambda: SimpleNamespace(objects=1, bytes=used, incoming_bytes=0, free_bytes=10 * tb,
                                       reserve_bytes=tb, max_bytes=int(1.5 * tb))
    out = Outbox()
    for used, sends in ((0.5, 0), (0.95, 1), (0.88, 1), (0.92, 1), (0.80, 1), (0.91, 2)):
        push.tick(conn, later(), send=out, store_usage=usage(int(used * 1.5 * tb)))
        assert len(out.sent) == sends, used
    assert "of its 1.5 TB limit" in out.sent[0][1]["body"]
    # Close to the reserve counts the same, whatever the limit says.
    low = SimpleNamespace(objects=1, bytes=0, incoming_bytes=0, free_bytes=int(1.05 * tb),
                          reserve_bytes=tb, max_bytes=None)
    assert push.store_level(low) >= push.STORE_FIRE
    assert "reserve" in push._store_notice(low).body


def test_a_campaign_that_finishes_nothing_for_hours_is_announced_once(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["campaign_stalled"])
    add_cases(conn, 2)
    t0 = int(time.time())
    db.lease(conn, "w1", now=t0 - 7 * 3600)            # 7 h ago, and nothing finished since
    out = Outbox()
    push.tick(conn, t0, send=out)
    push.tick(conn, t0 + 30, send=out)
    assert out.kinds() == ["campaign_stalled"]
    assert "none has finished yet" in out.sent[0][1]["body"]


def test_a_fleet_that_just_started_after_a_quiet_week_is_not_stalled(app, conn):
    add_cases(conn, 2)
    t0 = int(time.time())
    (old,) = db.lease(conn, "w1", now=t0 - 8 * 86400)
    db.complete(conn, old.lease_id, "file:///x", case_id=old.case_id, now=t0 - 7 * 86400)
    db.lease(conn, "w2", now=t0 - 3600)
    found, _ = push._conditions(db.push_snapshot(conn, t0 - 1200), t0, None, {})
    assert "campaign_stalled" not in [n.kind for n in found]


# -- delivery ---------------------------------------------------------------------------

def test_a_410_drops_the_subscription_and_other_failures_count_up(app, admin, conn):
    gone, _, _ = browser(1)
    flaky, _, _ = browser(2)
    subscribe(admin, gone, ["queue_empty"])
    subscribe(admin, flaky, ["queue_empty"])
    out = Outbox(lambda s: 410 if s["endpoint"] == gone["endpoint"] else 503)
    notice = [push.Notice("queue_empty", "Queue empty", "x", "/")]
    counts = push.deliver(conn, notice, int(time.time()), send=out)
    assert counts == {"sent": 0, "failed": 1, "dropped": 1}
    assert db.push_subscription(conn, gone["endpoint"]) is None
    row = db.push_subscription(conn, flaky["endpoint"])
    assert row["failures"] == 1 and "503" in row["last_error"]
    for _ in range(push.DROP_AFTER - 1):
        push.deliver(conn, notice, int(time.time()), send=out)
    assert db.push_subscription(conn, flaky["endpoint"]) is None, "ten refusals in a row"


def test_a_success_resets_the_failure_count(app, admin, conn):
    sub, _, _ = browser()
    subscribe(admin, sub, ["queue_empty"])
    notice = [push.Notice("queue_empty", "Queue empty", "x", "/")]
    push.deliver(conn, notice, 100, send=Outbox(500))
    push.deliver(conn, notice, 200, send=Outbox(201))
    row = db.push_subscription(conn, sub["endpoint"])
    assert row["failures"] == 0 and row["last_error"] is None and row["last_sent_at"] == 200


def test_a_revoked_link_a_deleted_account_and_a_demoted_admin_are_told_nothing_more(app, admin, conn):
    link = admin.post("/v1/shares", json={"label": "Alice"}).json()
    friend = TestClient(app)
    friend.post("/v1/auth/share", json={"token": link["token"]})
    s_friend, _, _ = browser(1)
    assert subscribe(friend, s_friend, ["queue_empty"]).status_code == 200
    bob = login_as(app, admin, "bob", "admin")
    s_bob, _, _ = browser(2)
    subscribe(bob, s_bob, ["queue_empty", "pair_request"])
    vic = login_as(app, admin, "vic", "viewer")
    s_vic, _, _ = browser(3)
    subscribe(vic, s_vic, ["queue_empty"])

    admin.delete(f"/v1/shares/{link['id']}")
    admin.post("/v1/users/bob/role", json={"role": "viewer"})
    admin.delete("/v1/users/vic")
    notices = [push.Notice("queue_empty", "Queue empty", "x", "/"),
               push.Notice("pair_request", "COD asks to join", "x", "/#settings=machines", "COD")]
    out = Outbox()
    push.deliver(conn, notices, int(time.time()), send=out, role_of=app.state.push_role_of)
    assert out.kinds(s_bob["endpoint"]) == ["queue_empty"], "no longer an admin"
    assert out.kinds(s_friend["endpoint"]) == [] and out.kinds(s_vic["endpoint"]) == []
    left = {s["endpoint"] for s in db.push_subscriptions(conn)}
    assert left == {s_bob["endpoint"]}, "the link's and the account's subscriptions are gone"


def test_an_env_token_taken_out_of_the_environment_ends_its_subscriptions(tmp_path):
    path = str(tmp_path / "t.sqlite")
    before = create_app(db_path=path, readonly_tokens=["old-read"])
    c = TestClient(before)
    c.headers.update({"Authorization": "Bearer old-read"})
    sub, _, _ = browser()
    assert subscribe(c, sub).status_code == 200
    row = db.push_subscription(db.connect(path), sub["endpoint"])
    assert row["subscriber_kind"] == "token" and "old-read" not in row["subscriber"]
    assert before.state.push_role_of(row) == "viewer"
    after = create_app(db_path=path, readonly_tokens=["new-read"])
    assert after.state.push_role_of(row) is None


def test_two_processes_sharing_a_tick_announce_it_once(app, admin, conn, monkeypatch):
    sub, _, _ = browser()
    subscribe(admin, sub, ["case_done"])
    add_cases(conn, 2)
    push.tick(conn, later(), send=Outbox())
    (got,) = db.lease(conn, "w1")
    db.complete(conn, got.lease_id, "file:///x", case_id=got.case_id)
    stale = db.push_setting(conn, db.PUSH_STATE_KEY)
    first, second = Outbox(), Outbox()
    push.tick(conn, later(), send=first)
    # The other process read the state before this one claimed it.
    real = db.push_setting
    monkeypatch.setattr(db, "push_setting", lambda c, k: stale if k == db.PUSH_STATE_KEY else real(c, k))
    result = push.tick(conn, later(), send=second)
    assert first.kinds() == ["case_done"] and second.sent == []
    assert result["skipped"] == "another process claimed this tick"


# -- the wire: real encryption, a real VAPID signature -----------------------------------

def test_the_vapid_key_is_made_once_kept_and_never_written_to_the_trail(app, conn):
    k1 = reader(app).get("/v1/push/key").json()["public_key"]
    assert len(base64.urlsafe_b64decode(k1 + "=")) == 65
    again = create_app(db_path=app.state.db_path)
    assert TestClient(again).get("/v1/push/key").json()["public_key"] == k1, "a redeploy keeps the key"
    private = sqlite3.connect(app.state.db_path).execute(
        "SELECT value FROM settings WHERE key = 'push_vapid_private'").fetchone()[0]
    trail = " ".join(str(r) for r in sqlite3.connect(app.state.db_path).execute("SELECT * FROM events"))
    assert private not in trail


def test_the_environment_s_key_wins(app, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("CASEBROKER_VAPID_PRIVATE_KEY", b64(key.private_numbers().private_value.to_bytes(32, "big")))
    want = b64(key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint))
    assert reader(app).get("/v1/push/key").json()["public_key"] == want


def test_the_vapid_subject_is_the_dashboard_s_public_origin_unless_configured(app, conn, monkeypatch):
    assert push.subject(conn) == push.DEFAULT_SUBJECT
    c = reader(app)

    def from_page(n, origin, host):
        r = c.post("/v1/push/subscriptions", json={"subscription": browser(n)[0]},
                   headers={"Origin": origin, "Host": host})
        assert r.status_code == 200, r.text
    from_page(1, "http://localhost:8765", "localhost:8765")
    assert push.subject(conn) == push.DEFAULT_SUBJECT, "a local page teaches it nothing"
    from_page(2, "https://elsewhere.example.org", "broker.example.org")
    assert push.subject(conn) == push.DEFAULT_SUBJECT, "not the host the request reached"
    from_page(3, "https://broker.example.org", "broker.example.org")
    assert push.subject(conn) == "https://broker.example.org"
    # Apple refuses a localhost subject, so a configured one is ignored, not used.
    monkeypatch.setenv("CASEBROKER_VAPID_SUBJECT", "mailto:me@localhost")
    assert push.subject(conn) == "https://broker.example.org"
    monkeypatch.setenv("CASEBROKER_VAPID_SUBJECT", "mailto:ops@example.org")
    assert push.subject(conn) == "mailto:ops@example.org"


def test_what_goes_over_the_wire_is_encrypted_signed_and_decrypts_to_the_notice(app, conn, monkeypatch):
    import http_ece
    import requests

    sub, key, auth = browser(host="web.push.apple.com")
    sub["p256dh"], sub["auth"] = sub["keys"]["p256dh"], sub["keys"]["auth"]
    seen = {}

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        seen.update(url=url, data=data, headers=dict(headers), timeout=timeout, redirects=self.max_redirects)
        return SimpleNamespace(status_code=201, text="", headers={})
    monkeypatch.setattr(requests.Session, "post", post)
    payload = {"kind": "queue_empty", "title": "Queue empty", "body": "No case is pending."}
    got = push.make_sender(conn)(sub, payload, ttl=600, urgency="high")
    assert got == push.Delivery(201)
    assert seen["url"] == sub["endpoint"] and seen["redirects"] == 0, "a redirect is never followed"
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["ttl"] == "600" and h["urgency"] == "high" and h["content-encoding"] == "aes128gcm"
    assert h["authorization"].startswith("vapid t=") and push.public_key(conn) in h["authorization"]
    assert b"No case is pending" not in seen["data"], "the push service sees ciphertext only"
    plain = http_ece.decrypt(seen["data"], private_key=key, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == payload


def test_the_test_button_sends_now_and_says_what_the_push_service_said(app, monkeypatch):
    c = reader(app)
    sub, _, _ = browser()
    subscribe(c, sub, ["queue_empty"])
    sent = []
    monkeypatch.setattr(push, "make_sender", lambda conn, timeout=10.0: (
        lambda s, p, ttl, urgency: (sent.append(p), 201)[1]))
    r = c.post("/v1/push/test", json={"endpoint": sub["endpoint"]}).json()
    assert r == {"ok": True, "status": 201, "error": None, "dropped": False}
    assert sent[0]["kind"] == "test" and "the queue runs dry" in sent[0]["body"]
    monkeypatch.setattr(push, "make_sender", lambda conn, timeout=10.0: (lambda s, p, ttl, urgency: 410))
    r = c.post("/v1/push/test", json={"endpoint": sub["endpoint"]}).json()
    assert r["ok"] is False and r["dropped"] is True
    assert c.get("/v1/push/subscriptions", params={"endpoint": sub["endpoint"]}).status_code == 404


# -- the browser's half ----------------------------------------------------------------

def test_the_service_worker_is_served_at_the_root_uncached(app):
    r = TestClient(app).get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-cache"
    for needed in ("showNotification", "notificationclick", "openWindow", "pushsubscriptionchange"):
        assert needed in r.text, needed


def test_the_service_worker_shows_folds_and_opens_the_right_place():
    """The worker as shipped, against a stubbed browser (tests/sw_check.js)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests" / "sw_check.js")], cwd=root,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "never another site" in result.stdout


def test_the_manifest_lets_ios_install_the_dashboard(app):
    r = TestClient(app).get("/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert r.json()["display"] == "standalone" and r.json()["start_url"] == "/"
    page = served_page(TestClient(app))
    assert '<link rel="manifest" href="/manifest.webmanifest"' in page
    for needed in ('id="pushToggle"', "/v1/push/subscriptions", 'register("/sw.js"', "Add to Home Screen"):
        assert needed in page, needed


def test_the_notifier_runs_only_when_asked(tmp_path, monkeypatch):
    quiet = create_app(db_path=str(tmp_path / "q.sqlite"))
    with TestClient(quiet):
        assert quiet.state.push_notifier is None, "never under test unless a test asks"
    asked = create_app(db_path=str(tmp_path / "a.sqlite"), push_notifier=True)
    with TestClient(asked):
        n = asked.state.push_notifier
        assert n is not None and n._thread.is_alive()
    assert n._stop.is_set()
    monkeypatch.setenv("CASEBROKER_PUSH", "0")
    off = create_app(db_path=str(tmp_path / "o.sqlite"), push_notifier=True)
    with TestClient(off):
        assert off.state.push_notifier is None

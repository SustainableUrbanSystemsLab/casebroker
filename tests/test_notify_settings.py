"""Settings -> Notifications: an admin configures the ntfy topic in the dashboard."""
import pytest
from fastapi.testclient import TestClient

from casebroker import notify
from casebroker.app import create_app

PW = "correct horse battery staple"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    for k in notify.ENV.values():
        monkeypatch.delenv(k, raising=False)
    c = TestClient(create_app(str(tmp_path / "s.sqlite"), tokens=[], readonly_tokens=[]))
    assert c.post("/v1/auth/setup", json={"username": "ada", "password": PW}).status_code == 200
    return c


def test_admin_sets_the_topic_and_events_and_the_secret_is_never_echoed(admin):
    r = admin.put("/v1/notify", json={"url": "https://ntfy.sh/casebroker-9f3a2b7c1d",
                                      "events": ["done", "quarantined"], "token": "tk_secret"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["enabled"] and v["url"] == "https://ntfy.sh/case…"
    assert v["events"] == ["done", "quarantined"] and v["token_set"] is True
    assert "9f3a2b7c1d" not in r.text and "tk_secret" not in r.text
    assert admin.get("/v1/notify").json()["source"]["url"] == "settings"


def test_only_sent_fields_change_and_empty_clears_back_to_env(admin, monkeypatch):
    admin.put("/v1/notify", json={"url": "https://ntfy.sh/aaaaaaaa"})
    admin.put("/v1/notify", json={"events": ["started"]})
    assert admin.get("/v1/notify").json()["enabled"] is True, "the URL survived an events-only update"
    monkeypatch.setenv("CASEBROKER_NOTIFY_URL", "https://ntfy.sh/envtopic")
    v = admin.put("/v1/notify", json={"url": ""}).json()
    assert v["source"]["url"] == "env" and v["url"] == "https://ntfy.sh/envt…"


@pytest.mark.parametrize("body", [{"url": "ftp://x"}, {"url": "file:///etc/passwd"},
                                  {"url": "http://169.254.169.254/latest/meta-data"},
                                  {"url": "http://localhost:5432/x"}, {"url": "https://user:pw@ntfy.sh/x"},
                                  {"url": "http://ntfy.sh/topic", "token": "t"},
                                  {"events": ["done", "exploded"]}, {"public_url": "javascript:alert(1)"}])
def test_bad_input_is_refused(admin, body):
    assert admin.put("/v1/notify", json=body).status_code == 422


def test_a_write_token_is_not_an_admin(tmp_path):
    c = TestClient(create_app(str(tmp_path / "s.sqlite"), tokens=["w"]))
    h = {"Authorization": "Bearer w"}
    assert c.get("/v1/notify", headers=h).status_code in (401, 403)
    assert c.put("/v1/notify", json={"url": "https://ntfy.sh/x"}, headers=h).status_code in (401, 403)
    assert c.post("/v1/notify/test", headers=h).status_code in (401, 403)


def test_a_self_hosted_server_is_allowed_only_when_the_operator_says_so(admin, monkeypatch):
    assert admin.put("/v1/notify", json={"url": "https://ntfy.example.org/t"}).status_code == 422
    monkeypatch.setenv("CASEBROKER_NOTIFY_ALLOWED_HOSTS", "ntfy.example.org")
    assert admin.put("/v1/notify", json={"url": "https://ntfy.example.org/t"}).status_code == 200


def test_moving_the_topic_to_another_host_drops_the_token(admin, monkeypatch):
    monkeypatch.setenv("CASEBROKER_NOTIFY_ALLOWED_HOSTS", "ntfy.example.org")
    admin.put("/v1/notify", json={"url": "https://ntfy.sh/aaaaaaaa", "token": "tk"})
    assert admin.put("/v1/notify", json={"url": "https://ntfy.sh/bbbbbbbb"}).json()["token_set"] is True
    assert admin.put("/v1/notify", json={"url": "https://ntfy.example.org/c"}).json()["token_set"] is False


def test_the_audit_trail_records_the_change_not_the_secret(admin):
    from casebroker import db
    admin.put("/v1/notify", json={"url": "https://ntfy.sh/casebroker-9f3a2b7c1d", "token": "tk_secret"})
    conn = admin.app.state.conn if hasattr(admin.app.state, "conn") else None
    rows = admin.get("/v1/auth/state")  # keep the client warm; read the table directly below
    import sqlite3, glob
    path = [p for p in glob.glob(str(__import__("pathlib").Path(admin.app.state.db_path)))][0]
    trail = [r[0] for r in sqlite3.connect(path).execute("SELECT detail FROM events WHERE event='setting'")]
    assert any(t.startswith("notify_url = https://ntfy.sh/case") for t in trail)
    assert not any("9f3a2b7c1d" in t or "tk_secret" in t for t in trail)


def test_the_test_button_reports_what_happened(admin, monkeypatch):
    assert admin.post("/v1/notify/test").json() == {"ok": False, "error": "no ntfy topic configured"}
    sent = []
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: sent.append(a))
    admin.put("/v1/notify", json={"url": "https://ntfy.sh/aaaaaaaa"})
    # send_test takes the sender as a default argument bound at import; call through the module
    # function the route uses, with the patched sender.
    r = notify.send_test({"notify_url": "https://ntfy.sh/aaaaaaaa"}, sender=lambda *a, **k: sent.append(a))
    assert r == {"ok": True} and sent and sent[-1][1] == "Test from the case broker"
    boom = notify.send_test({"notify_url": "https://ntfy.sh/aaaaaaaa"},
                            sender=lambda *a, **k: (_ for _ in ()).throw(OSError("10.0.0.5:22 open")))
    assert boom["ok"] is False and "10.0.0.5" not in boom["error"], "errors are coarse, never str(exception)"

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
                                  {"events": ["done", "exploded"]}, {"public_url": "javascript:alert(1)"}])
def test_bad_input_is_refused(admin, body):
    assert admin.put("/v1/notify", json=body).status_code == 422


def test_a_write_token_is_not_an_admin(tmp_path):
    c = TestClient(create_app(str(tmp_path / "s.sqlite"), tokens=["w"]))
    h = {"Authorization": "Bearer w"}
    assert c.get("/v1/notify", headers=h).status_code in (401, 403)
    assert c.put("/v1/notify", json={"url": "https://ntfy.sh/x"}, headers=h).status_code in (401, 403)
    assert c.post("/v1/notify/test", headers=h).status_code in (401, 403)


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
                            sender=lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")))
    assert boom["ok"] is False and "unreachable" in boom["error"]

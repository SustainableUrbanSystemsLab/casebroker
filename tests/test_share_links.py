"""Share links: how an admin shows the dashboard to someone with no account.

What this replaces: a `viewer` account (a password to invent and hand over) or
CASEBROKER_READ_TOKENS (one shared secret in the host's environment: no name, no
expiry, and taking it back is a redeploy). Each test pins a property those could
not give -- made in the browser, for a named person, for a stated time, read-only
by construction, and ended with a click.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from casebroker import db
from casebroker.app import create_app

PW = "a-sufficiently-long-passphrase"


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "share.sqlite"), tokens=[], readonly_tokens=[])


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    assert r.status_code == 200, r.text
    return c


def login_as(app, admin, username, role):
    admin.post("/v1/users", json={"username": username, "password": PW, "role": role})
    c = TestClient(app)
    assert c.post("/v1/auth/login", json={"username": username, "password": PW}).status_code == 200
    return c


def make_link(admin, label="Alice", **body):
    r = admin.post("/v1/shares", json={"label": label, **body})
    assert r.status_code == 200, r.text
    return r.json()


def friend_who_opened(app, link):
    """A browser with nothing but the link: the dashboard's first request."""
    c = TestClient(app)
    r = c.post("/v1/auth/share", json={"token": link["token"]})
    assert r.status_code == 200, r.text
    return c


# -- making one -----------------------------------------------------------------

def test_the_response_that_makes_a_link_is_the_only_time_it_can_be_read(app, admin):
    link = make_link(admin)
    assert link["token"] and len(link["token"]) >= 32
    assert link["url"].endswith("#share=" + link["token"]), "the secret rides in the fragment"
    assert "?" not in link["url"], "never in the query string, where the server's log would keep it"
    assert link["state"] == "live" and link["created_by"] == "ada"

    listed = admin.get("/v1/shares").json()["shares"]
    assert [s["label"] for s in listed] == ["Alice"]
    assert link["token"] not in admin.get("/v1/shares").text
    assert "token_hash" not in listed[0], "not even the hash: nothing in the list can sign in"

    # And the database holds only a hash.
    raw = sqlite3.connect(app.state.db_path)
    dump = " ".join(str(v) for row in raw.execute("SELECT * FROM share_links") for v in row)
    assert link["token"] not in dump


def test_a_link_lasts_a_week_unless_told_otherwise(app, admin):
    link = make_link(admin)
    assert 6.9 * 86400 < link["expires_at"] - link["created_at"] < 7.1 * 86400
    short = make_link(admin, "Bob", ttl_seconds=3600)
    assert short["expires_at"] - short["created_at"] == 3600
    forever = make_link(admin, "Carol", ttl_seconds=None)
    assert forever["expires_at"] is None


@pytest.mark.parametrize("body", [
    {"label": ""},
    {"label": "   "},
    {"label": "two\nlines"},
    {"label": "x" * 81},
    {"label": "Alice", "ttl_seconds": 10},
    {"label": "Alice", "ttl_seconds": 0},
    {"label": "Alice", "ttl_seconds": 366 * 86400},
])
def test_a_link_needs_a_plain_label_and_a_sane_lifetime(admin, body):
    assert admin.post("/v1/shares", json=body).status_code in (400, 422)
    assert admin.get("/v1/shares").json()["shares"] == []


def test_only_an_admin_can_make_list_or_revoke_links(app, admin):
    link = make_link(admin)
    operator = login_as(app, admin, "olga", "operator")
    viewer = login_as(app, admin, "vic", "viewer")
    nobody = TestClient(app)
    for who in (nobody, viewer, operator):
        assert who.post("/v1/shares", json={"label": "x"}).status_code in (401, 403)
        assert who.get("/v1/shares").status_code in (401, 403)
        assert who.delete(f"/v1/shares/{link['id']}").status_code in (401, 403)
    assert len(admin.get("/v1/shares").json()["shares"]) == 1, "none of that changed anything"


def test_a_machine_credential_cannot_make_links(app, admin):
    """A worker token that could mint ways in for strangers would defeat issuing
    them per machine."""
    machine = admin.post("/v1/workers/tokens", json={"name": "lab-1"}).json()["token"]
    r = TestClient(app).post("/v1/shares", json={"label": "x"},
                             headers={"Authorization": f"Bearer {machine}"})
    assert r.status_code in (401, 403)


def test_at_most_so_many_links_are_live(app, admin, monkeypatch):
    monkeypatch.setattr(db, "MAX_ACTIVE_SHARE_LINKS", 3)
    made = [make_link(admin, f"p{i}") for i in range(3)]
    r = admin.post("/v1/shares", json={"label": "one too many"})
    assert r.status_code == 409 and "revoke" in r.json()["detail"]
    admin.delete(f"/v1/shares/{made[0]['id']}")
    make_link(admin, "fits now")


# -- using one -------------------------------------------------------------------

def test_opening_a_link_gives_a_cookie_the_page_cannot_read(app, admin):
    link = make_link(admin)
    c = TestClient(app)
    r = c.post("/v1/auth/share", json={"token": link["token"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    cookie = r.headers["set-cookie"].lower()
    assert "wsb_share=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_a_visitor_through_a_link_can_read_the_campaign(app, admin):
    friend = friend_who_opened(app, make_link(admin))
    for path in ("/v1/status", "/v1/cases", "/v1/errors", "/v1/storage", "/v1/releases"):
        assert friend.get(path).status_code == 200, path


def test_the_page_is_told_it_is_a_read_only_visitor(app, admin):
    link = make_link(admin, "Private note about Alice")
    friend = friend_who_opened(app, link)
    state = friend.get("/v1/auth/state").json()
    assert state["user"] is None and state["role"] == "viewer"
    assert state["share"]["expires_at"] == link["expires_at"]
    who = friend.get("/v1/whoami").json()
    assert who["scope"] == "read" and who["auth"] == "share"
    # The label is the admin's note on the link, not something the holder is told.
    assert "Alice" not in friend.get("/v1/auth/state").text
    assert "Alice" not in friend.get("/v1/whoami").text


def test_a_visitor_through_a_link_can_change_nothing(app, admin):
    friend = friend_who_opened(app, make_link(admin))
    refused = [
        friend.post("/v1/cases", json=[{"lat": 1.0, "lon": 1.0, "recipe": "r", "city_cluster": "c"}]),
        friend.post("/v1/lease", json={"worker_id": "friend"}),
        friend.post("/v1/heartbeat", json={"lease_id": "x"}),
        friend.post("/v1/complete", json={"lease_id": "x", "result_uri": "y"}),
        friend.post("/v1/fail", json={"lease_id": "x", "error": "y"}),
        friend.delete("/v1/cases?expect=0&dry_run=false"),
    ]
    for r in refused:
        assert r.status_code == 403, (r.request.url, r.status_code, r.text)
        assert "read-only" in r.json()["detail"]
    # Nor can it reach identity: not accounts, not machines, not more links.
    for r in (friend.get("/v1/users"), friend.get("/v1/workers/tokens"),
              friend.get("/v1/shares"), friend.post("/v1/shares", json={"label": "x"}),
              friend.post("/v1/users", json={"username": "m", "password": PW, "role": "admin"})):
        assert r.status_code in (401, 403), (r.request.url, r.status_code)
    assert [u["username"] for u in admin.get("/v1/users").json()["users"]] == ["ada"]
    assert len(admin.get("/v1/shares").json()["shares"]) == 1


def test_the_token_reads_as_a_bearer_too_and_still_writes_nothing(app, admin):
    link = make_link(admin)
    script = TestClient(app)
    h = {"Authorization": f"Bearer {link['token']}"}
    assert script.get("/v1/status", headers=h).status_code == 200
    assert script.get("/v1/whoami", headers=h).json()["scope"] == "read"
    assert script.post("/v1/lease", json={"worker_id": "w"}, headers=h).status_code == 403
    assert script.get("/v1/status").status_code == 401, "and without it, nothing"


def test_opening_a_link_never_logs_an_admin_out(app, admin):
    """They can already see more than the link shows, and a second identity in
    one browser would make "log out" mean two things."""
    link = make_link(admin)
    r = admin.post("/v1/auth/share", json={"token": link["token"]})
    assert r.status_code == 200 and r.json()["already_signed_in"] is True
    assert "set-cookie" not in r.headers
    state = admin.get("/v1/auth/state").json()
    assert state["user"] == "ada" and state["role"] == "admin" and state["share"] is None
    row = admin.get("/v1/shares").json()["shares"][0]
    # And their preview is not the friend looking: "opened" and "last seen" are
    # how an admin learns the PERSON has.
    assert row["opened"] == 0 and row["last_used_at"] is None


def test_logging_out_leaves_no_link_cookie_behind(app, admin):
    friend = friend_who_opened(app, make_link(admin))
    assert friend.get("/v1/status").status_code == 200
    assert friend.post("/v1/auth/logout").status_code == 200
    assert friend.get("/v1/status").status_code == 401
    assert friend.get("/v1/auth/state").json()["share"] is None


# -- ending one --------------------------------------------------------------------

def test_revoking_ends_a_link_on_its_holders_next_request(app, admin):
    link = make_link(admin)
    friend = friend_who_opened(app, link)
    script_headers = {"Authorization": f"Bearer {link['token']}"}
    assert friend.get("/v1/status").status_code == 200

    assert admin.delete(f"/v1/shares/{link['id']}").json() == {"id": link["id"], "revoked": True}

    assert friend.get("/v1/status").status_code == 401
    assert TestClient(app).get("/v1/status", headers=script_headers).status_code == 401
    assert friend.get("/v1/auth/state").json()["share"] is None
    r = TestClient(app).post("/v1/auth/share", json={"token": link["token"]})
    assert r.status_code == 410 and "withdrawn" in r.json()["detail"]
    # It stays on the list, saying what became of it.
    row = admin.get("/v1/shares").json()["shares"][0]
    assert row["state"] == "revoked" and row["revoked_by"] == "ada"
    assert admin.delete(f"/v1/shares/{link['id']}").status_code == 404, "once"


def test_revoking_one_link_leaves_the_others_alone(app, admin):
    a, b = make_link(admin, "A"), make_link(admin, "B")
    fa, fb = friend_who_opened(app, a), friend_who_opened(app, b)
    admin.delete(f"/v1/shares/{a['id']}")
    assert fa.get("/v1/status").status_code == 401
    assert fb.get("/v1/status").status_code == 200


def test_a_link_stops_working_when_it_expires(app, admin, monkeypatch):
    link = make_link(admin, ttl_seconds=3600)
    friend = friend_who_opened(app, link)
    assert friend.get("/v1/status").status_code == 200

    monkeypatch.setattr(db, "_now", lambda: link["created_at"] + 3601)
    assert friend.get("/v1/status").status_code == 401
    r = TestClient(app).post("/v1/auth/share", json={"token": link["token"]})
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert admin.get("/v1/shares").json()["shares"][0]["state"] == "expired"


def test_a_link_made_until_revoked_outlives_its_cookie(app, admin, monkeypatch):
    link = make_link(admin, ttl_seconds=None)
    friend = friend_who_opened(app, link)
    monkeypatch.setattr(db, "_now", lambda: link["created_at"] + 5 * 365 * 86400)
    assert friend.get("/v1/status").status_code == 200


def test_a_link_this_broker_does_not_know_is_refused_and_throttled(app):
    c = TestClient(app)
    for _ in range(25):
        assert c.post("/v1/auth/share", json={"token": "not-a-real-link-token"}).status_code == 401
    r = c.post("/v1/auth/share", json={"token": "not-a-real-link-token"})
    assert r.status_code == 429


def test_a_dead_link_does_not_use_up_the_throttle(app, admin):
    """Holding a revoked link proves it was given to you; retrying it is not guessing."""
    link = make_link(admin)
    admin.delete(f"/v1/shares/{link['id']}")
    c = TestClient(app)
    for _ in range(40):
        assert c.post("/v1/auth/share", json={"token": link["token"]}).status_code == 410


def test_a_truncated_link_is_refused_at_the_door(app):
    assert TestClient(app).post("/v1/auth/share", json={"token": "abc"}).status_code == 422


# -- the record --------------------------------------------------------------------

def test_the_list_says_when_a_link_was_opened_and_how_often(app, admin):
    link = make_link(admin)
    assert admin.get("/v1/shares").json()["shares"][0]["opened"] == 0
    friend_who_opened(app, link)
    friend_who_opened(app, link)
    row = admin.get("/v1/shares").json()["shares"][0]
    assert row["opened"] == 2 and row["last_used_at"] is not None and row["state"] == "live"


def test_old_dead_links_are_swept_but_recent_ones_stay(tmp_path):
    conn = db.connect(str(tmp_path / "sweep.sqlite"))
    t = 1_000_000
    old = db.create_share_link(conn, "old", "h-old", "ada", expires_at=t + 10, now=t)
    gone = db.create_share_link(conn, "gone", "h-gone", "ada", now=t)
    db.revoke_share_link(conn, gone["id"], "ada", now=t + 5)
    keep = db.create_share_link(conn, "keep", "h-keep", "ada", now=t)
    later = t + db.SHARE_LINK_KEEP_SECONDS + 100
    assert db.purge_share_links(conn, now=later) == 2
    assert [s["label"] for s in db.list_share_links(conn, now=later)] == ["keep"]
    assert old["id"] != keep["id"]

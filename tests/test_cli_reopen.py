"""`casebroker reopen`: quarantined cases back in the pool as an admin, from a CI step.

The fleet's admin password lives in a CI secret (Eddy3D's broker-ops workflow); the CLI takes it on
stdin, says what matches first, and reopens only with --apply, in batches until nothing more does.
"""
from __future__ import annotations

import io
import pathlib
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import auth, cli, db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

WIND = "cyl-1008/of12-v6"


@pytest.fixture()
def broker(tmp_path):
    import uvicorn
    app = create_app(db_path=str(tmp_path / "r.sqlite"), tokens=["w"], readonly_tokens=["r"])
    conn = db.connect(app.state.db_path)
    db.create_user(conn, "ops", auth.hash_password("s3cret-pw"), role="admin")
    db.create_user(conn, "viewer", auth.hash_password("s3cret-pw"), role="viewer")
    db.add_cases(conn, [{"case_id": f"c{i:03d}", "spec": {"lat": 1.0 + i, "lon": 2.0, "recipe": WIND},
                          "recipe": WIND, "city_cluster": "x", "split": "train", "max_attempts": 1}
                         for i in range(3)])
    for i, why in enumerate(["the heartbeat stalled; its lease expired", "its lease expired", "a broken site"]):
        lease = db.lease(conn, "foam-1", recipes=[WIND])[0]
        db.fail(conn, lease.lease_id, why, retryable=False)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield conn, f"http://127.0.0.1:{port}"
    server.should_exit = True


def run(monkeypatch, args, password="s3cret-pw"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    return cli.main(args)


def states(conn):
    return {r["case_id"]: r["state"] for r in conn.execute("SELECT case_id, state FROM cases").fetchall()}


def test_a_dry_run_says_what_matches_and_changes_nothing(broker, monkeypatch, capsys):
    conn, url = broker
    rc = run(monkeypatch, ["reopen", "--error-contains", "its lease expired", "--broker", url,
                           "--username", "ops", "--password-stdin"])
    out = capsys.readouterr().out
    assert rc == 0 and "2 quarantined case(s) match" in out and "pass --apply" in out
    assert set(states(conn).values()) == {"quarantined"}


def test_apply_reopens_only_the_matching_cases_with_their_attempts_refunded(broker, monkeypatch, capsys):
    conn, url = broker
    rc = run(monkeypatch, ["reopen", "--error-contains", "its lease expired", "--limit", "1", "--apply",
                           "--broker", url, "--username", "ops", "--password-stdin"])
    out = capsys.readouterr().out
    assert rc == 0 and "reopened 2 case(s)" in out, out
    assert states(conn) == {"c000": "pending", "c001": "pending", "c002": "quarantined"}
    rc = run(monkeypatch, ["reopen", "--case-id", "c002", "--apply", "--broker", url,
                           "--username", "ops", "--password-stdin"])
    assert rc == 0 and states(conn)["c002"] == "pending"


def test_it_needs_an_admin_and_a_selection(broker, monkeypatch, capsys):
    conn, url = broker
    assert run(monkeypatch, ["reopen", "--broker", url, "--username", "ops", "--password-stdin"]) == 2
    assert run(monkeypatch, ["reopen", "--error-contains", "lease", "--apply", "--broker", url,
                             "--username", "viewer", "--password-stdin"]) == 1
    assert run(monkeypatch, ["reopen", "--error-contains", "lease", "--apply", "--broker", url,
                             "--username", "ops", "--password-stdin"], password="wrong") == 1
    assert set(states(conn).values()) == {"quarantined"}

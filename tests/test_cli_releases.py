"""`casebroker release ...`: the catalog from a terminal or a CI step.

The E3D node build writes release.json; registering it meant pasting it into
the dashboard after every build, a step someone had to remember. These pin the
CLI half: the file is read as the workflow wrote it, the login is an admin's
(and a CI step can pipe the password in), and the terminal can do what the
panel's buttons do.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import cli  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
OLD, NEW = "1.14.0.827+aaaaaaaa", "1.14.0.827+bbbbbbbb"
SHA = "ab" * 32
W = {"Authorization": "Bearer w"}
BASE = ["--broker", "http://broker.invalid", "--username", "ada"]


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "b.sqlite"), tokens=["w"], readonly_tokens=[])
    admin = TestClient(app)
    r = admin.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    # The CLI's HTTP goes through _call; route it into the app, cookies and all,
    # on a session of its own so the test's admin client is not what logs out.
    session = TestClient(app)

    def fake_call(opener, broker_url, method, path, payload=None, timeout=30.0):
        headers = dict(getattr(opener, "addheaders", None) or [])
        r = session.request(method, path, json=payload, headers=headers)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body
    monkeypatch.setattr(cli, "_call", fake_call)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PW)
    return admin


def publish(c, build, *platforms):
    for p in platforms:
        assert c.post("/v1/releases", json={"build": build, "platform": p, "file": "f", "sha256": SHA}).status_code == 200


def test_register_reads_the_workflows_release_json(broker, tmp_path):
    rows = [{"build": NEW, "platform": "win-x64", "file": f"E3D-{NEW}-win-x64.exe", "sha256": SHA},
            {"build": NEW, "platform": "linux-x64", "file": f"E3D-{NEW}-linux-x64", "sha256": SHA}]
    f = tmp_path / "release.json"
    f.write_text(json.dumps(rows), encoding="utf-8")
    assert cli.main(["release", "register", str(f), *BASE, "--notes", "dense sites no longer die"]) == 0
    got = broker.get("/v1/releases", headers=W).json()
    assert sorted((r["platform"], r["notes"], r["added_by"]) for r in got["releases"]) == [
        ("linux-x64", "dense sites no longer die", "ada"), ("win-x64", "dense sites no longer die", "ada")]


def test_the_password_can_come_from_stdin_for_a_ci_step(broker, tmp_path, monkeypatch):
    f = tmp_path / "release.json"
    f.write_text(json.dumps({"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA}))
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": pytest.fail("must not prompt"))
    assert cli.main(["release", "register", str(f), *BASE, "--password-stdin"]) == 0
    assert broker.get("/v1/releases", headers=W).json()["releases"][0]["build"] == NEW


def test_a_viewer_cannot_change_what_the_fleet_runs(broker, tmp_path, capsys):
    assert broker.post("/v1/users", json={"username": "vic", "password": PW, "role": "viewer"}).status_code == 200
    f = tmp_path / "release.json"
    f.write_text(json.dumps({"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA}))
    assert cli.main(["release", "register", str(f), "--broker", "http://broker.invalid", "--username", "vic"]) == 1
    assert "needs an admin" in capsys.readouterr().err
    assert broker.get("/v1/releases", headers=W).json()["releases"] == []


def test_a_file_that_is_not_release_json_is_refused_before_any_login(broker, tmp_path, capsys):
    f = tmp_path / "release.json"
    f.write_text("not json")
    assert cli.main(["release", "register", str(f), *BASE]) == 2
    f.write_text("[]")
    assert cli.main(["release", "register", str(f), *BASE]) == 2


def test_list_prints_the_target_and_where_the_fleet_is(broker, capsys):
    broker.post("/v1/lease", headers=W, json={"worker_id": "foam-1", "build": OLD, "platform": "win-x64"})
    publish(broker, OLD, "win-x64")
    publish(broker, NEW, "win-x64")
    assert broker.put("/v1/releases/target", json={"build": NEW}).status_code == 200
    assert cli.main(["release", "list", "--broker", "http://broker.invalid", "--token", "w"]) == 0
    out = capsys.readouterr().out
    assert f"target   : {NEW}" in out and "1 behind" in out and OLD in out


def test_target_promote_and_rollback_from_the_terminal(broker, capsys):
    broker.post("/v1/lease", headers=W, json={"worker_id": "foam-1", "build": OLD, "platform": "win-x64"})
    publish(broker, OLD, "win-x64")
    publish(broker, NEW, "win-x64")
    assert cli.main(["release", "target", OLD, *BASE, "--apply", "direction"]) == 0
    assert broker.put("/v1/workers/foam-1/target", json={"build": NEW}).status_code == 200
    assert cli.main(["release", "promote", "foam-1", *BASE]) == 0
    got = broker.get("/v1/releases", headers=W).json()
    assert (got["target_build"], got["target_apply"], got["previous_target"]) == (NEW, "direction", OLD)
    assert cli.main(["release", "rollback", "--block", *BASE]) == 0
    got = broker.get("/v1/releases", headers=W).json()
    assert (got["target_build"], got["blocked_builds"]) == (OLD, [NEW])
    # A target some live platform has no file for is refused; --force insists.
    broker.post("/v1/lease", headers=W, json={"worker_id": "mac", "build": OLD, "platform": "osx-arm64"})
    assert cli.main(["release", "target", NEW, *BASE]) == 1
    assert "no file for osx-arm64" in capsys.readouterr().err
    assert cli.main(["release", "target", NEW, "--force", *BASE]) == 0
    assert cli.main(["release", "target", "none", *BASE]) == 0
    assert broker.get("/v1/releases", headers=W).json()["target_build"] is None

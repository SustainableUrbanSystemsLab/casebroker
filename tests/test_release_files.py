"""The broker holds the builds the nodes run, and the nodes fetch them from it.

It used to name a build and its hash and never serve the file: each node took it
from a release share somebody filled. Since the Syncthing master was retired
(2026-10-06) nobody fills them, so a node told to move to a new build waited at
"the release share does not hold X yet" for ever, and a SLURM job's E3D was only
ever updated by a copy by hand. The files now live in the part store: uploaded in
chunks by whoever registers the build, fetched by the nodes over the channel they
already authenticate on, and checked against the hash the ADMIN registered.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import pathlib
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import cli, db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
B1, B2, B3 = "1.17.0.827+aaaaaaaa", "1.17.0.827+bbbbbbbb", "1.17.0.827+cccccccc"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _file(build: str, platform: str, size: int = 3000) -> bytes:
    # Different content per build and platform, as real executables are.
    seed = f"{build}/{platform}".encode()
    return (seed * (size // len(seed) + 1))[:size]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_PARTS_RESERVE_GB", "0")
    monkeypatch.setenv("CASEBROKER_RELEASE_KEEP_BUILDS", "2")
    return create_app(db_path=str(tmp_path / "r.sqlite"), tokens=[], readonly_tokens=[],
                      parts_dir=str(tmp_path / "parts"))


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    assert c.post("/v1/auth/setup", json={"username": "ada", "password": PW}).status_code == 200
    return c


def _register(admin, build, platform="linux-x64", content=None):
    content = content if content is not None else _file(build, platform)
    name = f"E3D-{build}-{platform}"
    r = admin.post("/v1/releases", json={"build": build, "platform": platform, "file": name,
                                         "sha256": _sha(content)})
    assert r.status_code == 200, r.text
    return content


def _upload(client, build, content, platform="linux-x64", chunk=1000):
    """What the CLI does: ask where the upload stands, send from there, wait for 'stored'."""
    where = f"/v1/releases/{build}/{platform}/upload"
    r = client.post(where, json={"sha256": _sha(content), "bytes": len(content)})
    assert r.status_code == 200, r.text
    state = r.json()
    while state["state"] in ("absent", "partial"):
        off = state["offset"]
        r = client.put(where, params={"offset": off, "bytes": len(content)}, content=content[off:off + chunk])
        assert r.status_code == 200, r.text
        state = r.json()
    deadline = time.time() + 10
    while state["state"] != "stored":
        assert time.time() < deadline, state
        time.sleep(0.05)
        state = client.post(where, json={"sha256": _sha(content), "bytes": len(content)}).json()
    return state


def _machine(admin, name="lab-1"):
    token = admin.post("/v1/workers/tokens", json={"name": name}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# -- the round trip -----------------------------------------------------------------------

def test_a_registered_build_is_uploaded_once_and_a_node_fetches_it(app, admin):
    content = _register(admin, B1)
    assert _upload(admin, B1, content)["state"] == "stored"
    assert admin.put("/v1/releases/target", json={"build": B1}).status_code == 200

    node = TestClient(app)
    h = _machine(admin)
    told = node.get("/v1/node/release", headers=h,
                    params={"worker_id": "lab-1", "platform": "linux-x64", "build": "1.16.0.827+old"}).json()
    assert told["target_build"] == B1 and told["sha256"] == _sha(content)
    # Percent-encoded: a build name carries a "+", which a query string would read as a space.
    assert told["url"] == "/v1/releases/1.17.0.827%2Baaaaaaaa/linux-x64/file"
    assert told["bytes"] == len(content)

    got = node.get(told["url"], headers=h)
    assert got.status_code == 200 and got.content == content
    assert got.headers["etag"] == f'"{_sha(content)}"' and got.headers["x-sha256"] == _sha(content)


def test_a_node_resumes_a_download_where_it_stopped(app, admin):
    content = _register(admin, B1)
    _upload(admin, B1, content)
    h = _machine(admin)
    part = TestClient(app).get(f"/v1/releases/{B1}/linux-x64/file", headers={**h, "Range": "bytes=1000-"})
    assert part.status_code == 206
    assert part.content == content[1000:]
    same = TestClient(app).get(f"/v1/releases/{B1}/linux-x64/file",
                               headers={**h, "If-None-Match": f'"{_sha(content)}"'})
    assert same.status_code == 304


def test_the_catalog_says_which_files_the_broker_holds(admin):
    content = _register(admin, B1)
    _register(admin, B1, "win-x64")
    _upload(admin, B1, content)
    rows = {r["platform"]: r for r in admin.get("/v1/releases").json()["releases"]}
    assert rows["linux-x64"]["stored"] is True and rows["linux-x64"]["bytes"] == len(content)
    assert rows["win-x64"]["stored"] is False and rows["win-x64"]["bytes"] is None
    assert admin.get("/v1/releases").json()["release_files"] is True


def test_a_node_is_told_no_url_until_the_broker_holds_its_file(app, admin):
    _register(admin, B1)
    admin.put("/v1/releases/target", json={"build": B1})
    told = TestClient(app).get("/v1/node/release", headers=_machine(admin),
                               params={"worker_id": "lab-1", "platform": "linux-x64", "build": "x"}).json()
    assert told["file"] and told["url"] is None and told["bytes"] is None, "a share may still hold it"
    gone = TestClient(app).get(f"/v1/releases/{B1}/linux-x64/file", headers=_machine(admin, "lab-2"))
    assert gone.status_code == 410 and "--upload" in gone.json()["detail"]


def test_healthz_advertises_release_files_only_with_a_store(tmp_path, app):
    assert "release_files" in TestClient(app).get("/healthz").json()["features"]
    bare = create_app(db_path=str(tmp_path / "bare.sqlite"), tokens=[], readonly_tokens=[], parts_dir="")
    assert "release_files" not in TestClient(bare).get("/healthz").json()["features"]


# -- who may do what ---------------------------------------------------------------------------

def test_the_upload_must_be_the_content_the_admin_registered(admin):
    _register(admin, B1)
    other = b"not the registered build" * 50
    r = admin.post(f"/v1/releases/{B1}/linux-x64/upload", json={"sha256": _sha(other), "bytes": len(other)})
    assert r.status_code == 409 and "register the file you mean" in r.json()["detail"]
    r = admin.post(f"/v1/releases/{B2}/linux-x64/upload", json={"sha256": _sha(other), "bytes": len(other)})
    assert r.status_code == 404, "an unregistered build has nothing to upload against"


def test_bytes_that_do_not_hash_to_the_registration_are_dropped(admin):
    content = _register(admin, B1)
    forged = b"x" * len(content)
    where = f"/v1/releases/{B1}/linux-x64/upload"
    admin.post(where, json={"sha256": _sha(content), "bytes": len(content)})
    admin.put(where, params={"offset": 0, "bytes": len(content)}, content=forged)
    deadline = time.time() + 10
    while True:
        st = admin.post(where, json={"sha256": _sha(content), "bytes": len(content)}).json()
        if st["state"] != "verifying":
            break
        assert time.time() < deadline
        time.sleep(0.05)
    assert st["state"] == "absent" and "hashes to" in st["last_failure"]


def test_only_an_admin_uploads_and_a_reader_cannot_fetch_executables(app, admin):
    content = _register(admin, B1)
    h = _machine(admin)
    machine = TestClient(app)
    r = machine.post(f"/v1/releases/{B1}/linux-x64/upload", headers=h,
                     json={"sha256": _sha(content), "bytes": len(content)})
    assert r.status_code in (401, 403), "a machine token could otherwise plant a build"
    _upload(admin, B1, content)

    admin.post("/v1/users", json={"username": "vic", "password": PW, "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/v1/auth/login", json={"username": "vic", "password": PW})
    assert viewer.get(f"/v1/releases/{B1}/linux-x64/file").status_code == 403
    link = admin.post("/v1/shares", json={"label": "a friend"}).json()
    friend = TestClient(app)
    friend.post("/v1/auth/share", json={"token": link["token"]})
    assert friend.get(f"/v1/releases/{B1}/linux-x64/file").status_code in (401, 403)
    assert machine.get(f"/v1/releases/{B1}/linux-x64/file", headers=h).status_code == 200


def test_without_a_store_there_is_nothing_to_upload_or_fetch(tmp_path):
    bare = create_app(db_path=str(tmp_path / "bare.sqlite"), tokens=[], readonly_tokens=[], parts_dir="")
    c = TestClient(bare)
    c.post("/v1/auth/setup", json={"username": "ada", "password": PW})
    content = _register(c, B1)
    assert c.post(f"/v1/releases/{B1}/linux-x64/upload",
                  json={"sha256": _sha(content), "bytes": len(content)}).status_code == 404
    c.put("/v1/releases/target", json={"build": B1})
    told = c.get("/v1/node/release", params={"worker_id": "w", "platform": "linux-x64", "build": "x"}).json()
    assert told["url"] is None
    assert c.get("/v1/releases").json()["release_files"] is False


# -- what the store keeps ------------------------------------------------------------------------

def test_only_the_newest_builds_and_the_ones_in_use_keep_their_files(app, admin):
    """A build is ~535 MB and every push to dev makes one: the store keeps the newest few
    (CASEBROKER_RELEASE_KEEP_BUILDS, 2 here) and whatever the fleet still needs."""
    store = app.state.parts
    files = {}
    for b in (B1, B2):
        files[b] = _register(admin, b)
        _upload(admin, b, files[b])
    admin.put("/v1/releases/target", json={"build": B1})
    admin.put("/v1/releases/target", json={"build": B2})       # B1 is now the roll-back target
    files[B3] = _register(admin, B3)
    _upload(admin, B3, files[B3])
    deadline = time.time() + 5
    while not all(store.has(_sha(files[b])) for b in (B2, B3)):
        assert time.time() < deadline
        time.sleep(0.05)
    assert store.has(_sha(files[B1])), "the previous target is where a roll back goes"

    admin.put("/v1/releases/target", json={"build": B3})       # previous is now B2; B1 is needed by nothing
    b4 = "1.17.0.827+dddddddd"
    files[b4] = _register(admin, b4)
    _upload(admin, b4, files[b4])
    deadline = time.time() + 5
    while store.has(_sha(files[B1])):
        assert time.time() < deadline, "the oldest build nothing needs lets its file go"
        time.sleep(0.05)
    assert all(store.has(_sha(files[b])) for b in (B2, B3, b4))
    # The catalog still names it, and it can be uploaded again.
    assert any(r["build"] == B1 for r in admin.get("/v1/releases").json()["releases"])


def test_the_target_keeps_its_file_however_many_builds_come_after_it(app, admin):
    store = app.state.parts
    first = _register(admin, B1)
    _upload(admin, B1, first)
    admin.put("/v1/releases/target", json={"build": B1})
    for b in (B2, B3, "1.17.0.827+dddddddd"):
        _upload(admin, b, _register(admin, b))
    time.sleep(0.3)
    assert store.has(_sha(first)), "the fleet is still being told to fetch it"


def test_a_sweep_leaves_release_files_alone(app, admin):
    content = _register(admin, B1)
    _upload(admin, B1, content)
    path = app.state.parts.object_path(_sha(content))
    old = time.time() - 7200
    os.utime(path, (old, old))                       # past the sweep's one-hour grace
    swept = admin.post("/v1/parts/sweep", params={"dry_run": "false"}).json()
    assert swept["orphans"] == 0 and path.is_file()


def test_removing_a_build_removes_its_files(app, admin):
    content = _register(admin, B1)
    _upload(admin, B1, content)
    out = admin.delete(f"/v1/releases/{B1}").json()
    assert out == {"removed": 1, "files_removed": 1}
    assert not app.state.parts.has(_sha(content))


# -- the CLI a CI step runs ------------------------------------------------------------------------

def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _serving(app):
    """The app on a real socket, as a CI runner meets it: the CLI speaks urllib, not TestClient."""
    import uvicorn

    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        deadline = time.time() + 30
        while not server.started:
            assert time.time() < deadline
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True


def _dist(tmp_path, platforms=("linux-x64", "win-x64"), size=2500):
    import json

    folder = tmp_path / "dist"
    folder.mkdir()
    rows = []
    for platform in platforms:
        content = _file(B1, platform, size)
        name = f"E3D-{B1}-{platform}" + (".exe" if platform == "win-x64" else "")
        (folder / name).write_bytes(content)
        rows.append({"build": B1, "platform": platform, "file": name, "sha256": _sha(content)})
    (folder / "release.json").write_text(json.dumps(rows))
    return folder


def _register_upload(monkeypatch, broker, folder) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    return cli.main(["release", "register", str(folder / "release.json"), "--broker", broker,
                     "--username", "ada", "--password-stdin", "--upload", str(folder)])


def test_register_upload_sends_every_file_in_chunks(tmp_path, monkeypatch, app, admin):
    from casebroker import partstore

    # Small chunks, so the file goes up in several of them as a 200 MB build would.
    monkeypatch.setattr(partstore, "SUGGESTED_CHUNK", 700)
    folder = _dist(tmp_path)
    with _serving(app) as broker:
        assert _register_upload(monkeypatch, broker, folder) == 0
    stored = {r["platform"]: r["stored"] for r in admin.get("/v1/releases").json()["releases"]}
    assert stored == {"linux-x64": True, "win-x64": True}


def test_register_upload_rides_out_a_dropped_connection_and_a_deploy(tmp_path, monkeypatch, app, admin, capsys):
    """A CI runner's upload meets the broker restarting under Watchtower (502 from the proxy)
    and a reset connection: it waits, asks where the upload stands, and goes on."""
    import urllib.error

    from casebroker import partstore

    monkeypatch.setattr(partstore, "SUGGESTED_CHUNK", 700)
    monkeypatch.setattr(cli, "_pause", lambda s: None)
    real, calls = cli._put_bytes, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise urllib.error.URLError("connection reset by peer")
        if len(calls) == 3:
            return 502, {}
        return real(*a, **kw)

    monkeypatch.setattr(cli, "_put_bytes", flaky)
    folder = _dist(tmp_path, platforms=("linux-x64",))
    with _serving(app) as broker:
        assert _register_upload(monkeypatch, broker, folder) == 0
    assert admin.get("/v1/releases").json()["releases"][0]["stored"] is True
    err = capsys.readouterr().err
    assert "connection reset by peer; asking again in 5 s" in err and "502; asking again in 10 s" in err


def test_register_upload_gives_up_on_a_broker_that_stays_down(tmp_path, monkeypatch, app, admin):
    from casebroker import partstore

    monkeypatch.setattr(partstore, "SUGGESTED_CHUNK", 700)
    monkeypatch.setattr(cli, "_pause", lambda s: None)
    monkeypatch.setattr(cli, "_put_bytes", lambda *a, **kw: (503, {"detail": "down"}))
    folder = _dist(tmp_path, platforms=("linux-x64",))
    with _serving(app) as broker:
        assert _register_upload(monkeypatch, broker, folder) == 1


def test_register_upload_refuses_a_file_that_is_not_the_registered_one(tmp_path, monkeypatch, app, admin):
    import json

    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "E3D-x").write_bytes(b"what is on disk")
    (folder / "release.json").write_text(json.dumps([{"build": B1, "platform": "linux-x64", "file": "E3D-x",
                                                      "sha256": _sha(b"what release.json claims")}]))
    with _serving(app) as broker:
        assert _register_upload(monkeypatch, broker, folder) == 1
    assert admin.get("/v1/releases").json()["releases"][0]["stored"] is False


# -- the release panel -------------------------------------------------------------------------------

DASH = (pathlib.Path(__file__).resolve().parents[1] / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")


def test_the_release_panel_says_which_files_the_broker_holds():
    i = DASH.index("function renderReleases(")
    body = DASH[i:DASH.index("\n  }\n", i)]
    assert "heldHint(b.build, rows)" in body and "r.release_files" in body
    assert "--upload" in body, "what to do about a target whose files are not here"


def test_hidden_means_hidden_even_on_a_button():
    """Roll back and the kill switch stay `hidden` until there is a previous target; a
    button's display: inline-flex used to win over the attribute and show them anyway."""
    assert "[hidden] { display: none !important; }" in DASH
    i = DASH.index("function renderReleases(")
    assert '$("relRollbackBtn").hidden = !back;' in DASH[i:i + 4000]

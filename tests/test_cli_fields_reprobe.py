"""`casebroker fields reprobe`: stored pedestrian fields replaced by a re-probe, as an admin.

Eddy3D's reprobe_case.py samples a v9 case's direction parts again with
cellPointWallModified and writes out/<case>/fields/<direction>.umag. This sends each file
to the case and direction its header names, behind the admin login scripts/broker-admin.sh
supplies: a dry run unless --apply, and "unchanged" for bytes the broker holds already, so
an interrupted run is simply run again.
"""
from __future__ import annotations

import io
import json
import pathlib
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import auth, cli, db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

V9 = "cyl-1008/of12-v9"
CASE = "v2-002c849ccb119c51"
NAN = float("nan")
REPROBE = {"interpolation": "cellPointWallModified", "source": "eddy3d-reprobe"}


def umag(values, direction, **extra) -> bytes:
    """A umag/1 container, as the broker stores it and reprobe_case.py writes it."""
    header = {"format": "umag/1", "case_id": CASE, "direction": direction, "deg": 0.0, "height_m": 1.75,
              "nx": 3, "ny": 2, "x0": -2.0, "y0": -1.0, "spacing_m": 2.0, "coverage": 0.8, "u_ref": 3.1}
    header.update(extra)
    hb = json.dumps(header).encode()
    return b"UMAG" + struct.pack("<II", 1, len(hb)) + hb + struct.pack("<6f", *values)


@pytest.fixture()
def broker(tmp_path):
    """A broker over HTTP holding one v9 case whose node stored two directions with cellPoint."""
    import uvicorn
    app = create_app(db_path=str(tmp_path / "f.sqlite"), tokens=["w"], readonly_tokens=["r"])
    conn = db.connect(app.state.db_path)
    db.create_user(conn, "ops", auth.hash_password("s3cret-pw"), role="admin")
    db.create_user(conn, "viewer", auth.hash_password("s3cret-pw"), role="viewer")
    db.add_cases(conn, [{"case_id": CASE, "spec": {"lat": 23.0, "lon": 57.5, "recipe": V9},
                         "recipe": V9, "city_cluster": "x", "split": "train"}])
    lease = db.lease(conn, "ice-123-0", recipes=[V9])[0]
    for d in ("case_000", "case_001"):
        assert db.put_field(conn, lease.lease_id, CASE, d, umag([1, 2, NAN, 4, 5, 0], d)) == "ok"
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield conn, f"http://127.0.0.1:{port}"
    server.should_exit = True


def write(root: pathlib.Path, direction: str, values=(1.1, 2.2, NAN, 4.4, 5.9, 0.3)) -> pathlib.Path:
    p = root / CASE / "fields" / f"{direction}.umag"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(umag(values, direction, **REPROBE))
    return p


def run(monkeypatch, args, password="s3cret-pw"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    monkeypatch.setattr(cli, "_pause", lambda seconds: None)      # a 5xx is retried; never wait on it here
    return cli.main(args)


def stored(conn, direction: str) -> bytes:
    return db.case_field(conn, CASE, direction)["blob"]


def test_a_dry_run_says_what_it_would_replace_and_changes_nothing(broker, tmp_path, monkeypatch, capsys):
    conn, url = broker
    out = tmp_path / "out"
    write(out, "case_000"), write(out, "case_001")
    before = {d: stored(conn, d) for d in ("case_000", "case_001")}
    rc = run(monkeypatch, ["fields", "reprobe", str(out), "--broker", url, "--username", "ops", "--password-stdin"])
    text = capsys.readouterr().out
    assert rc == 0, text
    assert text.count("would replace") == 2 and "no interpolation named" in text and "pass --apply" in text
    assert {d: stored(conn, d) for d in before} == before


def test_apply_replaces_each_field_and_a_second_run_finds_them_unchanged(broker, tmp_path, monkeypatch, capsys):
    conn, url = broker
    out = tmp_path / "out"
    files = {d: write(out, d) for d in ("case_000", "case_001")}
    args = ["fields", "reprobe", str(out), "--apply", "--broker", url, "--username", "ops", "--password-stdin"]
    assert run(monkeypatch, args) == 0
    assert capsys.readouterr().out.count(": replaced ") == 2
    for d, p in files.items():
        assert stored(conn, d) == p.read_bytes()
    events = conn.execute("SELECT worker_id FROM events WHERE case_id=? AND event='field_reprobed'",
                          (CASE,)).fetchall()
    assert [e["worker_id"] for e in events] == ["ops", "ops"], "the event names the admin"
    assert run(monkeypatch, args) == 0, "an interrupted run is simply run again"
    assert capsys.readouterr().out.count("unchanged") == 2


def test_a_refused_field_fails_the_run_and_the_rest_still_go(broker, tmp_path, monkeypatch, capsys):
    conn, url = broker
    out = tmp_path / "out"
    good = write(out, "case_000")
    write(out, "case_001", values=(NAN, 2.2, 3.3, 4.4, 5.9, 0.3))       # another NaN pattern: another surface
    rc = run(monkeypatch, ["fields", "reprobe", str(out), "--apply", "--broker", url,
                           "--username", "ops", "--password-stdin"])
    cap = capsys.readouterr()
    assert rc == 1, cap
    assert "case_001: refused (409)" in cap.err and "case_000: replaced" in cap.out
    assert stored(conn, "case_000") == good.read_bytes()
    assert stored(conn, "case_001") == umag([1, 2, NAN, 4, 5, 0], "case_001")


def test_it_needs_an_admin_and_every_file_readable_before_it_sends_any(broker, tmp_path, monkeypatch, capsys):
    conn, url = broker
    out = tmp_path / "out"
    write(out, "case_000")
    assert run(monkeypatch, ["fields", "reprobe", str(out), "--apply", "--broker", url,
                             "--username", "viewer", "--password-stdin"]) == 1
    (out / "junk.umag").write_bytes(b"not a field")
    assert run(monkeypatch, ["fields", "reprobe", str(out), "--apply", "--broker", url,
                             "--username", "ops", "--password-stdin"]) == 2
    assert run(monkeypatch, ["fields", "reprobe", str(tmp_path / "nowhere"), "--broker", url,
                             "--username", "ops", "--password-stdin"]) == 2
    assert stored(conn, "case_000") == umag([1, 2, NAN, 4, 5, 0], "case_000"), "nothing was sent"

"""Push notifications: a case started, began meshing/solving, finished, failed, was quarantined."""
import threading
import time

import pytest

from casebroker import db, notify


def _case(conn, cid="A", lat=48.137, lon=11.575, max_attempts=3):
    db.add_cases(conn, [{"case_id": cid, "spec": {"lat": lat, "lon": lon}, "recipe": "r",
                         "city_cluster": "m", "lcz": "LCZ2", "split": "train",
                         "priority": 100, "max_attempts": max_attempts}])


class Sink:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, url, title, body, tags, token=None, click=None):
        if self.fail:
            raise OSError("ntfy unreachable")
        self.sent.append({"url": url, "title": title, "body": body, "tags": tags,
                          "token": token, "click": click})


def _notifier(conn, sink, **kw):
    return notify.Notifier(conn, "https://ntfy.sh/topic", sender=sink, **kw)


def test_the_whole_life_of_a_case_is_announced_phase_by_phase(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    sink = Sink()
    n = _notifier(conn, sink, public_url="https://broker.example.org/")

    lease = db.lease(conn, "node-1", 1)[0]
    n.poll_once()
    assert sink.sent[-1]["title"] == "Case started"
    assert "A started · Munich, Germany · node-1" in sink.sent[-1]["body"]
    assert sink.sent[-1]["click"] == "https://broker.example.org"

    db.heartbeat(conn, lease.lease_id, 3600, "mesh 1/5 · 01_blockMesh")
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 3/5 · 03_snappyHexMesh")   # same phase: no second notice
    n.poll_once()
    assert sink.sent[-1]["title"] == "Case meshing" and len(sink.sent) == 2

    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/32 dirs · starting")
    db.heartbeat(conn, lease.lease_id, 3600, "solve 1/32 dirs · iter 12/2000")
    n.poll_once()
    assert sink.sent[-1]["title"] == "Case solving" and len(sink.sent) == 3

    db.complete(conn, lease.lease_id, "file:///x", case_id="A")
    n.poll_once()
    assert sink.sent[-1]["title"] == "Case finished"
    assert sink.sent[-1]["tags"] == "white_check_mark"


def test_many_moments_in_one_poll_are_one_message(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    for i in range(3):
        _case(conn, f"C{i}", lat=48.1 + i * 0.01)
    sink = Sink()
    n = _notifier(conn, sink)
    leases = db.lease(conn, "node-1", 3)
    db.complete(conn, leases[0].lease_id, "file:///x", case_id=leases[0].case_id)
    n.poll_once()
    assert len(sink.sent) == 1
    assert sink.sent[0]["title"] == "1 finished, 3 started"
    assert sink.sent[0]["body"].splitlines()[0].startswith(leases[0].case_id + " finished")


def test_a_failure_says_why_and_a_quarantine_is_its_own_notice(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn, max_attempts=1)
    sink = Sink()
    n = _notifier(conn, sink, events={"quarantined", "failed"})
    lease = db.lease(conn, "node-1", 1)[0]
    db.fail(conn, lease.lease_id, "solve exited 1: FOAM FATAL ERROR\n  maximum iterations", retryable=True)
    n.poll_once()
    assert sink.sent[-1]["title"] == "Case quarantined", "attempts exhausted: the retry becomes a quarantine"
    assert "FOAM FATAL ERROR maximum iterations" in sink.sent[-1]["body"]


def test_events_not_asked_for_are_not_sent(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    sink = Sink()
    n = _notifier(conn, sink, events={"done"})
    lease = db.lease(conn, "node-1", 1)[0]
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 1/5 · 01_blockMesh")
    n.poll_once()
    assert sink.sent == []
    db.complete(conn, lease.lease_id, "file:///x", case_id="A")
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case finished"]


def test_a_restart_does_not_replay_history(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    lease = db.lease(conn, "node-1", 1)[0]
    db.complete(conn, lease.lease_id, "file:///x", case_id="A")
    sink = Sink()
    _notifier(conn, sink).poll_once()           # a fresh process starts at the newest event
    assert sink.sent == []


def test_a_failed_delivery_is_retried_not_lost(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    sink = Sink(fail=True)
    n = _notifier(conn, sink)
    db.lease(conn, "node-1", 1)
    assert n.poll_once() == 0
    sink.fail = False
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case started"]


def test_a_phase_change_survives_a_restart_between_the_two_lines(tmp_path):
    """The previous phase comes from the database, so a notifier that never saw the
    meshing line still knows solving is new -- and does not re-announce meshing."""
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    lease = db.lease(conn, "node-1", 1)[0]
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 5/5 · 06_checkMesh")
    sink = Sink()
    n = _notifier(conn, sink)                   # "restarted" here
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 5/5 · 06_checkMesh (resumed)")
    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/8 dirs · starting")
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case solving"]


def test_off_unless_configured_and_the_env_is_read(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    monkeypatch.delenv("CASEBROKER_NOTIFY_URL", raising=False)
    assert notify.from_env(conn) is None
    monkeypatch.setenv("CASEBROKER_NOTIFY_URL", "https://ntfy.sh/secret-topic")
    monkeypatch.setenv("CASEBROKER_NOTIFY_EVENTS", "done, quarantined, bogus")
    monkeypatch.setenv("CASEBROKER_NOTIFY_INTERVAL", "1")
    n = notify.from_env(conn)
    assert n.events == {"done", "quarantined"} and n.interval == 5.0


@pytest.mark.parametrize("line, phase", [
    ("mesh 3/5 · 03_snappyHexMesh", "meshing"), ("solve 2/8 dirs · iter 1/2", "solving"),
    ("archiving", None), ("case_270 [3/8 dirs] iter 412", None), (None, None)])
def test_the_phase_is_read_from_the_nodes_grammar(line, phase):
    assert notify._phase(line) == phase


def test_the_real_sender_speaks_ntfy(tmp_path):
    """Against a local HTTP server: the body is the message, the rest are headers."""
    import http.server
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            got["headers"] = dict(self.headers)
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.handle_request, daemon=True); t.start()
    notify.send_ntfy(f"http://127.0.0.1:{srv.server_port}/topic", "Case finished", "A finished · Munich",
                     "white_check_mark", token="tok", click="https://b")
    t.join(5); srv.server_close()
    assert got["body"] == "A finished · Munich"
    assert got["headers"]["Title"] == "Case finished" and got["headers"]["Tags"] == "white_check_mark"
    assert got["headers"]["Authorization"] == "Bearer tok" and got["headers"]["Click"] == "https://b"

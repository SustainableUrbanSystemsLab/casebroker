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
            raise notify.SendError("connection refused", transient=True)
        self.sent.append({"url": url, "title": title, "body": body, "tags": tags,
                          "token": token, "click": click})


class Clock:
    """Wall time plus an offset the test moves: rows are read with a few seconds'
    lag, so a poll 'now' must be a little after the events it should see."""
    def __init__(self):
        self.offset = notify.LAG_SECONDS + 1

    def __call__(self):
        return time.time() + self.offset


def _notifier(conn, sink, **kw):
    kw.setdefault("clock", Clock())
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


def test_a_transient_failure_is_retried_and_a_permanent_one_dropped(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    sink, clock = Sink(fail=True), Clock()
    n = _notifier(conn, sink, clock=clock)
    db.lease(conn, "node-1", 1)
    assert n.poll_once() == 1 and sink.sent == [] and len(n.pending) == 1
    sink.fail = False
    n.poll_once()
    assert sink.sent == [], "not before its backoff"
    clock.offset += 31
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case started"] and n.pending == []

    class Refused(Sink):
        def __call__(self, *a, **k):
            raise notify.SendError("ntfy answered 400", transient=False)
    _case(conn, "B", lat=48.2)
    n.sender = Refused()
    db.lease(conn, "node-1", 1)
    clock.offset += 10
    n.poll_once()
    assert n.pending == [], "a permanent failure is dropped, not retried forever"


def test_the_cursor_survives_a_restart_and_two_instances_never_both_send(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    for i in range(2):
        _case(conn, f"C{i}", lat=48.1 + i * 0.01)
    a_sink, b_sink = Sink(), Sink()
    a = _notifier(conn, a_sink)
    db.lease(conn, "node-1", 1)
    a.poll_once()
    db.lease(conn, "node-1", 1)          # happens while "a" is being replaced
    b = _notifier(conn, b_sink)          # the new process: resumes from the saved cursor
    b.poll_once(); a.poll_once()
    assert len(a_sink.sent) == 1 and len(b_sink.sent) == 1, "the second lease announced once, by one of them"


def test_a_long_absence_starts_from_now_instead_of_replaying(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    _notifier(conn, Sink())
    monkeypatch.setattr(notify, "REPLAY_MAX_ROWS", 0)
    db.lease(conn, "node-1", 1)
    sink = Sink()
    _notifier(conn, sink).poll_once()
    assert sink.sent == []


def test_a_big_batch_stays_under_ntfys_message_limit(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    for i in range(40):
        _case(conn, f"C{i:02d}", lat=48.0 + i * 0.01, max_attempts=1)
    sink = Sink()
    n = _notifier(conn, sink)
    for lease in db.lease(conn, "COD-PKAST-7865", 40):
        db.fail(conn, lease.lease_id, "solve exited 1: " + "FOAM FATAL ERROR " * 40, retryable=True)
    n.poll_once()
    body = sink.sent[-1]["body"]
    assert len(body.encode("utf-8")) <= notify.BODY_LIMIT
    assert "more" in body.splitlines()[-1]


def test_solving_is_announced_once_per_attempt_despite_free_text_lines(tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    sink = Sink()
    n = _notifier(conn, sink)
    lease = db.lease(conn, "node-1", 1)[0]
    for line in ("mesh 1/5 · 01_blockMesh", "solve 0/8 dirs · starting", "convergence gate",
                 "solve 1/8 dirs · iter 3/2000"):
        db.heartbeat(conn, lease.lease_id, 3600, line)
    n.poll_once()
    assert sink.sent[-1]["title"] == "1 started, 1 meshing, 1 solving" or \
        sorted(l.split(" ")[1] for l in sink.sent[-1]["body"].splitlines()) == ["meshing", "solving", "started"]
    # A retry meshes again: that is a new phase start, not a repeat.
    db.fail(conn, lease.lease_id, "boom", retryable=True)
    lease = db.lease(conn, "node-2", 1)[0]
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 1/5 · 01_blockMesh")
    n.poll_once()
    assert "A meshing" in sink.sent[-1]["body"]


def test_a_phase_change_is_decided_from_the_database(tmp_path):
    """The previous phase comes from the database, so a notifier that never saw the
    meshing line still knows solving is new -- and does not re-announce meshing."""
    conn = db.connect(str(tmp_path / "n.sqlite"))
    _case(conn)
    lease = db.lease(conn, "node-1", 1)[0]
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 5/5 · 06_checkMesh")
    sink = Sink()
    n = _notifier(conn, sink)
    n.poll_once()
    sink.sent.clear()
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 5/5 · 06_checkMesh (resumed)")
    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/8 dirs · starting")
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case solving"]


def test_nothing_is_sent_until_a_topic_is_configured_and_the_backlog_is_skipped(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "n.sqlite"))
    for k in notify.ENV.values():
        monkeypatch.delenv(k, raising=False)
    _case(conn)
    sink = Sink()
    n = notify.from_env(conn)
    n.sender, n.clock = sink, Clock()
    db.lease(conn, "node-1", 1)
    n.poll_once()
    assert sink.sent == [], "no topic: silent"
    # Switched on in Settings: applies on the next poll, and does not announce
    # what happened while it was off.
    db.set_setting(conn, "notify_url", "https://ntfy.sh/topic")
    n.poll_once()
    assert sink.sent == []
    _case(conn, "B", lat=48.2)
    db.lease(conn, "node-1", 1)
    n.poll_once()
    assert [m["title"] for m in sink.sent] == ["Case started"]
    assert sink.sent[0]["url"] == "https://ntfy.sh/topic"


def test_settings_win_over_the_environment_field_by_field(monkeypatch):
    monkeypatch.setenv("CASEBROKER_NOTIFY_URL", "https://ntfy.sh/from-env")
    monkeypatch.setenv("CASEBROKER_NOTIFY_EVENTS", "done, quarantined, bogus")
    monkeypatch.delenv("CASEBROKER_NOTIFY_TOKEN", raising=False)
    cfg = notify.resolve({})
    assert cfg["url"] == "https://ntfy.sh/from-env" and cfg["source"]["url"] == "env"
    assert cfg["events"] == ["done", "quarantined"]
    cfg = notify.resolve({"notify_url": "https://ntfy.sh/from-ui", "notify_events": '["started"]'})
    assert cfg["url"] == "https://ntfy.sh/from-ui" and cfg["source"]["url"] == "settings"
    assert cfg["events"] == ["started"] and cfg["source"]["token"] is None


def test_the_topic_is_masked_for_display():
    assert notify.mask("https://ntfy.sh/casebroker-9f3a2b7c1d") == "https://ntfy.sh/case…"
    assert notify.mask(None) is None


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


def test_a_redirect_is_not_followed_and_the_body_is_not_read_unbounded():
    import http.server
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/elsewhere"); self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.handle_request, daemon=True); t.start()
    with pytest.raises(notify.SendError) as e:
        notify.send_ntfy(f"http://127.0.0.1:{srv.server_port}/topic", "t", "b", "x", token="secret")
    t.join(5); srv.server_close()
    assert hits == ["/topic"] and not e.value.transient and "redirect" in str(e.value)

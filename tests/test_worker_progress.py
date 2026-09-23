"""What the worker reads off the local disk: the runner's progress line and
the resume markers. No broker involved -- these are the client's own files."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker.worker import Worker  # noqa: E402


def make_worker(tmp_path, **kw):
    return Worker("http://broker.invalid", None, worker_id="ws-01", **kw)


def test_heartbeat_detail_is_the_runner_line_or_alive(tmp_path):
    pf = tmp_path / "progress.txt"
    w = make_worker(tmp_path, progress_file=str(pf))
    assert w.progress_detail() == "alive"          # nothing written yet
    pf.write_text("case_270 [1/8 dirs] iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)\n")
    assert w.progress_detail() == "case_270 [1/8 dirs] iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)"
    pf.write_text("x" * 1000)
    assert len(w.progress_detail()) == 400          # bounded, whatever the runner wrote
    assert make_worker(tmp_path).progress_detail() == "alive"


def test_resume_ids_are_only_this_workers_markers(tmp_path):
    cases = tmp_path / "cases"
    for case, owner in (("v2-aaa", "ws-01"), ("v2-bbb", "ws-02"), ("v2-ccc", "ws-01")):
        d = cases / case
        d.mkdir(parents=True)
        (d / "resume.json").write_text(json.dumps({"case_id": case, "worker_id": owner}))
    (cases / "v2-ddd").mkdir()
    (cases / "v2-ddd" / "resume.json").write_text("{not json")
    (cases / "v2-eee").mkdir()                       # a checkpoint dir with no marker

    w = make_worker(tmp_path, cases_dir=str(cases))
    assert w.resume_case_ids() == ["v2-aaa", "v2-ccc"]
    assert make_worker(tmp_path).resume_case_ids() == []
    assert make_worker(tmp_path, cases_dir=str(tmp_path / "missing")).resume_case_ids() == []


# -- surviving a broker restart ------------------------------------------------

class _FlakyTransport:
    """Answers 503 for the first `outages` calls, then 200. Stands in for a
    platform redeploy: the service is briefly gone, then it is back."""

    def __init__(self, outages):
        self.outages = outages
        self.calls = 0

    def handle_request(self, request):
        import httpx
        self.calls += 1
        if self.calls <= self.outages:
            return httpx.Response(503, text="service restarting")
        return httpx.Response(200, json={"ok": True})


def _worker_with(transport, monkeypatch):
    import httpx
    w = Worker("http://broker.invalid", None, worker_id="ws-01")
    w.http = httpx.Client(base_url="http://broker.invalid", transport=transport)
    w._current_lease = "lease-1"
    slept = []
    monkeypatch.setattr("casebroker.worker.time.sleep", slept.append)
    return w, slept


def test_completing_a_case_survives_a_broker_restart(monkeypatch):
    """The one call whose failure throws away real work.

    The case is solved and the archive is on disk; only the broker has not
    been told. If this gives up, the lease expires and hours of CFD are
    recomputed elsewhere. A credential rotation restarts the service, so the
    retry budget has to outlast a redeploy rather than the ~30 s that every
    other call needs.
    """
    transport = _FlakyTransport(outages=6)
    w, slept = _worker_with(transport, monkeypatch)

    w.complete("file:///archive.zip", sha256="deadbeef", nbytes=10)

    assert transport.calls == 7, "must keep trying across the whole outage"
    assert sum(slept) > 120, f"budget must outlast a redeploy, spanned {sum(slept)}s"


def test_a_revoked_lease_is_not_retried_even_by_complete(monkeypatch):
    """The long budget must not blunt a definitive answer. A 409 means the
    lease is gone; retrying it for four minutes would only delay the worker
    finding out, and the case belongs to someone else by then."""
    import httpx

    class Gone:
        calls = 0

        def handle_request(self, request):
            Gone.calls += 1
            return httpx.Response(409, text="lease reclaimed")

    w, slept = _worker_with(Gone(), monkeypatch)
    try:
        w.complete("file:///archive.zip")
    except Exception:
        pass                                  # raise_for_status or LeaseLost
    assert Gone.calls == 1, "a 409 is definitive, not a transient failure"
    assert slept == [], "and must not have waited at all"


def test_an_unchanged_heartbeat_detail_does_not_write_another_event(tmp_path):
    """The worker heartbeats every 5 minutes for a multi-hour solve, and sends
    "alive" until the runner has written a progress line -- so without this a
    six-hour case left ~72 identical rows saying nothing the one before it did
    not, and a 30,000-case campaign carried millions for the life of the
    campaign. A stalled solver dedupes the same way, which is the honest
    record: nothing happened."""
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    from casebroker import db

    conn = db.connect(str(tmp_path / "hb.sqlite"))
    db.add_cases(conn, [{"case_id": "c1", "spec": {}, "recipe": "r",
                         "city_cluster": "atl", "split": "train"}])
    lease = db.lease(conn, "lab-ws-02", 1)[0]

    def rows():
        return conn.execute("SELECT count(*) AS n FROM events "
                            "WHERE event = 'progress'").fetchone()["n"]

    for _ in range(12):
        assert db.heartbeat(conn, lease.lease_id, 900, "alive")
    assert rows() == 1

    for _ in range(10):
        db.heartbeat(conn, lease.lease_id, 900, "case_270 iter 412 p=3.2e-05")
    assert rows() == 2

    db.heartbeat(conn, lease.lease_id, 900, "case_270 iter 512 p=1.1e-05")
    assert rows() == 3


def test_a_new_lease_records_its_first_line_even_when_it_repeats_the_last_attempts(tmp_path):
    """Dedupe is per lease. A node before Eddy3D ae59812f says "solve 0/32 dirs
    · starting" for hours; when attempt 2 opened with the line attempt 1 had
    ended on, nothing was recorded for the whole attempt -- no current stage on
    the dashboard, and the case's progress time was the previous attempt's."""
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    from casebroker import db

    conn = db.connect(str(tmp_path / "hb3.sqlite"))
    db.add_cases(conn, [{"case_id": "c1", "spec": {}, "recipe": "r",
                         "city_cluster": "atl", "split": "train"}])
    line = "solve 0/32 dirs · starting"
    t0 = 1_000_000
    first = db.lease(conn, "w1", 1, now=t0)[0]
    assert db.heartbeat(conn, first.lease_id, 900, line, now=t0 + 60)
    assert db.fail(conn, first.lease_id, "solve exited 1: FPE", retryable=True, now=t0 + 6 * 3600)

    t1 = t0 + 7 * 3600
    second = db.lease(conn, "w1", 1, now=t1)[0]
    for k in range(5):
        assert db.heartbeat(conn, second.lease_id, 900, line, now=t1 + 60 + 300 * k)

    n = conn.execute("SELECT count(*) AS n FROM events WHERE event = 'progress'").fetchone()["n"]
    assert n == 2, "once for each lease, and still once within one"
    case = db.get_case(conn, "c1")
    assert case["last_progress_at"] == t1 + 60, "this attempt's line, not the last attempt's"
    assert case["current"] == "solve"
    assert case["stages"][-1]["attempt"] == 2 and case["stages"][-1]["ended_at"] is None


def test_deduping_never_shortens_the_lease_itself(tmp_path):
    """The heartbeat's real job is extending the lease. Skipping a duplicate
    EVENT must not skip the extension, or a quiet solve would lose its case."""
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    from casebroker import db

    conn = db.connect(str(tmp_path / "hb2.sqlite"))
    db.add_cases(conn, [{"case_id": "c1", "spec": {}, "recipe": "r",
                         "city_cluster": "atl", "split": "train"}])
    lease = db.lease(conn, "lab-ws-02", 1, lease_seconds=60)[0]
    first = conn.execute("SELECT lease_expires AS e FROM cases").fetchone()["e"]
    assert db.heartbeat(conn, lease.lease_id, 7200, "alive")
    assert db.heartbeat(conn, lease.lease_id, 7200, "alive")   # the duplicate
    later = conn.execute("SELECT lease_expires AS e FROM cases").fetchone()["e"]
    assert later > first

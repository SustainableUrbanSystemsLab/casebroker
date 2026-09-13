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

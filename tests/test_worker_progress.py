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

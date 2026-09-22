"""A failure names the stage it happened in and carries both streams' last lines.

The message kept stderr OR stdout, so a runner that logged its stages to stdout
and died with an empty stderr reported an exit code and nothing else, and the
reader had to open the archive to learn it died meshing.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import worker  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the runner under test is a bash script")


def make_script(tmp_path, body: str) -> str:
    p = tmp_path / "runner.sh"
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def test_a_failure_says_the_stage_and_shows_both_streams(tmp_path):
    pf = tmp_path / "progress.txt"
    pf.write_text("solve 3/8 dirs · iter 412/2000\n", encoding="utf-8")
    w = worker.Worker("http://broker.invalid", None, worker_id="ws-01", progress_file=str(pf))
    script = make_script(tmp_path, 'echo "stage: meshing"; echo "stage: solving"; '
                                   'echo "FOAM FATAL ERROR: boom" >&2; exit 1\n')
    run = worker.script_runner(script, timeout=30, capture_lines=50)
    with pytest.raises(RuntimeError) as e:
        run({"case_id": "c1", "lease_id": "L", "spec": {}}, w)
    msg = str(e.value)
    assert msg.startswith('runner exited 1: during "solve 3/8 dirs · iter 412/2000"')
    assert "--- stderr, last 1 lines ---\nFOAM FATAL ERROR: boom" in msg
    assert "--- stdout, last 2 lines ---\nstage: meshing\nstage: solving" in msg


def test_without_a_progress_line_the_stage_is_left_out(tmp_path):
    script = make_script(tmp_path, 'echo "only stdout"; exit 3\n')
    run = worker.script_runner(script, timeout=30)
    with pytest.raises(RuntimeError) as e:
        run({"case_id": "c1", "lease_id": "L", "spec": {}}, None)
    msg = str(e.value)
    assert msg.startswith("runner exited 3:") and "during" not in msg
    assert "--- stdout, last 1 lines ---\nonly stdout" in msg
    assert "stderr" not in msg, "an empty stream is left out, not labelled empty"


def test_the_exit_codes_that_mean_something_still_do(tmp_path):
    run = worker.script_runner(make_script(tmp_path, "exit 64\n"), timeout=30)
    with pytest.raises(worker.FatalCaseError):
        run({"case_id": "c1", "lease_id": "L", "spec": {}}, None)
    run = worker.script_runner(make_script(tmp_path, "exit 69\n"), timeout=30)
    with pytest.raises(worker.NodeUnfitError):
        run({"case_id": "c1", "lease_id": "L", "spec": {}}, None)

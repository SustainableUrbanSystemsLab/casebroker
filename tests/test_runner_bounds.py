"""A case runs for hours on a machine nobody is watching. Both ends need a bound.

The heartbeat thread is INDEPENDENT of the runner subprocess, which is what
makes an unbounded runner expensive rather than merely slow: a hung solve keeps
getting its lease renewed, so the broker never reclaims the case, the worker
never moves on, and the allocation burns to walltime with nothing to show. It
self-corrected only when SLURM killed the job.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time

import pytest

from casebroker import worker as w


def _script(tmp_path, body: str):
    p = tmp_path / ("s.cmd" if os.name == "nt" else "s.sh")
    p.write_text(textwrap.dedent(body))
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def lease():
    return {"case_id": "c1", "lease_id": "l1", "spec": {"lat": 1, "lon": 2}}


class _NoWorker:
    progress_file = None
    cases_dir = None
    worker_id = "w"


def test_a_normal_runner_still_works(tmp_path, lease):
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        echo "some chatter"
        echo '{"result_uri": "s3://bucket/x", "metrics": {"a": 1}}'
        """)
    out = w.script_runner(s)(lease, _NoWorker())
    assert out["result_uri"] == "s3://bucket/x"
    assert out["metrics"]["a"] == 1


def test_a_hung_runner_is_killed_rather_than_heartbeated_forever(tmp_path, lease):
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        sleep 120
        echo '{"result_uri": "never"}'
        """)
    t0 = time.time()
    with pytest.raises(RuntimeError, match="exceeded"):
        w.script_runner(s, timeout=2)(lease, _NoWorker())
    assert time.time() - t0 < 45, "the timeout did not actually fire"


def test_a_timeout_is_retryable_not_fatal(tmp_path, lease):
    """A hang says nothing about the case; another machine may well finish it.

    Only exit code 64 means "this case is broken", and a killed process does not
    get to report one.
    """
    s = _script(tmp_path, "#!/usr/bin/env bash\nsleep 120\n")
    with pytest.raises(RuntimeError) as ei:
        w.script_runner(s, timeout=2)(lease, _NoWorker())
    assert not isinstance(ei.value, w.FatalCaseError)


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_the_whole_process_group_dies_with_it(tmp_path, lease):
    """A solve is mpirun and its ranks, not one process.

    Killing only the direct child leaves the ranks holding the cores this worker
    is about to ask for again.
    """
    marker = tmp_path / "grandchild_alive"
    s = _script(tmp_path, f"""\
        #!/usr/bin/env bash
        ( while true; do touch {marker}; sleep 0.2; done ) &
        sleep 120
        """)
    with pytest.raises(RuntimeError, match="exceeded"):
        w.script_runner(s, timeout=2)(lease, _NoWorker())
    time.sleep(1.5)
    if marker.exists():
        marker.unlink()
    time.sleep(1.5)
    assert not marker.exists(), "a grandchild outlived the timeout and kept running"


def test_a_chatty_runner_does_not_grow_the_heap(tmp_path, lease):
    """capture_output held the whole OpenFOAM log in the worker's RAM.

    Only two things are ever read from it: the last stdout line, and a tail of
    stderr for the error message.
    """
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        for i in $(seq 1 20000); do echo "noise line $i with some padding to make it wide"; done
        echo '{"result_uri": "s3://bucket/x"}'
        """)
    out = w.script_runner(s, capture_lines=50)(lease, _NoWorker())
    assert out["result_uri"] == "s3://bucket/x", (
        "the JSON result line must survive the bound, since it is always last")


def test_the_error_tail_survives_a_failing_chatty_runner(tmp_path, lease):
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        for i in $(seq 1 5000); do echo "stderr noise $i" >&2; done
        echo "THE ACTUAL PROBLEM" >&2
        exit 3
        """)
    with pytest.raises(RuntimeError) as ei:
        w.script_runner(s, capture_lines=100)(lease, _NoWorker())
    assert "THE ACTUAL PROBLEM" in str(ei.value)
    assert "exited 3" in str(ei.value)


def test_exit_64_is_still_fatal(tmp_path, lease):
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        echo "bad tile" >&2
        exit 64
        """)
    with pytest.raises(w.FatalCaseError):
        w.script_runner(s)(lease, _NoWorker())


def test_a_runner_that_ignores_stdin_is_fine(tmp_path, lease):
    """Closing stdin on a runner that never reads it must not raise BrokenPipe."""
    s = _script(tmp_path, """\
        #!/usr/bin/env bash
        exec 0<&-
        echo '{"result_uri": "s3://ok"}'
        """)
    assert w.script_runner(s)(lease, _NoWorker())["result_uri"] == "s3://ok"

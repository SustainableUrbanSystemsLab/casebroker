"""The worker <-> runner contract.

`script_runner` is the seam where the CFD plugs in: the broker never learns what
OpenFOAM is, and the runner never learns what a lease is. Everything crossing that
seam is a stdout convention and an exit code, which is exactly the kind of
interface that rots silently -- so it is pinned here, including the failure paths,
which are the ones that matter at 40,000 cases.

The distinction that earns its own test is retryable vs fatal. A node dying is
retryable; a tile whose STL is not watertight will fail identically on every
machine in the fleet, and cycling it through three workers helps nobody. Exit 64
is the agreed "do not retry" code.
"""

from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402
from casebroker.worker import FatalCaseError, script_runner  # noqa: E402


def write_script(tmp_path, body: str) -> str:
    # A python script rather than a shell one: these tests must run on the Windows
    # workstation as well as on the clusters, and sys.executable is always here.
    p = tmp_path / "runner.py"
    p.write_text(body, encoding="utf-8")
    launcher = tmp_path / ("runner.cmd" if os.name == "nt" else "runner.sh")
    if os.name == "nt":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{p}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{p}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


LEASE = {"case_id": "v2-abc", "lease_id": "L1", "attempt": 1,
         "spec": {"lat": 34.0, "lon": -84.0}}


def test_the_last_stdout_line_is_the_result(tmp_path):
    runner = script_runner(write_script(tmp_path, """
import json, os
print("meshing...")            # chatter before the result is normal and ignored
print("solving...")
print(json.dumps({"result_uri": "file:///out/" + os.environ["CASE_ID"],
                  "bytes": 17, "metrics": {"cells": 1858222}}))
"""))
    out = runner(LEASE, None)
    assert out["result_uri"] == "file:///out/v2-abc"
    assert out["metrics"]["cells"] == 1858222


def test_the_spec_reaches_the_script(tmp_path):
    runner = script_runner(write_script(tmp_path, """
import json, os, sys
spec = json.loads(os.environ["CASE_SPEC"])
stdin = json.loads(sys.stdin.read())
assert spec["lat"] == 34.0, spec
assert stdin["case_id"] == os.environ["CASE_ID"]
assert os.environ["LEASE_ID"] == "L1"
print(json.dumps({"result_uri": "ok"}))
"""))
    assert runner(LEASE, None)["result_uri"] == "ok"


def test_exit_64_is_fatal_and_everything_else_is_retryable(tmp_path):
    fatal = script_runner(write_script(tmp_path, """
import sys
print("geometry is not watertight", file=sys.stderr)
sys.exit(64)
"""))
    with pytest.raises(FatalCaseError) as e:
        fatal(LEASE, None)
    assert "not watertight" in str(e.value), "the reason must survive to the broker"

    transient = script_runner(write_script(tmp_path, """
import sys
print("image pull failed", file=sys.stderr)
sys.exit(1)
"""))
    with pytest.raises(RuntimeError) as e2:
        transient(LEASE, None)
    assert not isinstance(e2.value, FatalCaseError), "exit 1 must stay retryable"


def test_silence_and_garbage_are_both_errors_not_silent_successes(tmp_path):
    empty = script_runner(write_script(tmp_path, "pass\n"))
    with pytest.raises(RuntimeError, match="no output"):
        empty(LEASE, None)

    garbage = script_runner(write_script(tmp_path, 'print("all done!")\n'))
    with pytest.raises(RuntimeError, match="not JSON"):
        garbage(LEASE, None)


def test_a_fatal_runner_quarantines_the_case_on_its_first_attempt(tmp_path):
    """The behaviour the exit code exists for, checked against the real database
    rather than against the exception alone."""
    conn = db.connect(str(tmp_path / "b.sqlite"))
    cid = ids.case_id(34.0, -84.0, "r1")
    db.add_cases(conn, [{"case_id": cid, "spec": {}, "recipe": "r1",
                         "city_cluster": "c", "split": "train"}])
    lease = db.lease(conn, "w")[0]

    # What run_forever does with a FatalCaseError.
    db.fail(conn, lease.lease_id, "geometry is not watertight", retryable=False)

    row = conn.execute("SELECT state, attempts FROM cases").fetchone()
    assert row["state"] == "quarantined"
    assert row["attempts"] == 1, "a fatal case must not consume three workers first"
    assert db.lease(conn, "w2") == [], "and it must not be handed out again"

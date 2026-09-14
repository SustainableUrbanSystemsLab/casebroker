"""The progress line the heartbeat carries.

Pinned against a synthetic solver log because the one misread it exists to
prevent is easy to reintroduce: p is solved several times per outer iteration,
and counting every "Solving for p" line instead of the first one per step
turns a clean descent into an apparent oscillation.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner" / "lib"))

import progress  # noqa: E402

LOG = """\
Starting time loop

Time = 1s

DILUPBiCGStab:  Solving for Ux, Initial residual = 1, Final residual = 0.01, No Iterations 1
DILUPBiCGStab:  Solving for Uy, Initial residual = 1, Final residual = 0.01, No Iterations 1
DICPCG:  Solving for p, Initial residual = 1, Final residual = 0.009, No Iterations 40
DICPCG:  Solving for p, Initial residual = 0.2, Final residual = 0.002, No Iterations 60
DICPCG:  Solving for p, Initial residual = 0.05, Final residual = 0.0005, No Iterations 80
ExecutionTime = 10 s  ClockTime = 10 s

Time = 2s

DILUPBiCGStab:  Solving for Ux, Initial residual = 0.4, Final residual = 0.004, No Iterations 1
DICPCG:  Solving for p, Initial residual = 0.3, Final residual = 0.003, No Iterations 40
DICPCG:  Solving for p, Initial residual = 0.02, Final residual = 0.0002, No Iterations 60
DICPCG:  Solving for p, Initial residual = 0.001, Final residual = 1e-05, No Iterations 80
ExecutionTime = 20 s  ClockTime = 20 s
"""


def test_first_solve_per_outer_iteration_is_the_one_reported():
    info = progress.parse_log(LOG)
    assert info["iteration"] == 2
    # 0.3 is the FIRST p solve of step 2 -- not 0.001, the last corrector stage,
    # which is what a naive "last match" parser returns.
    assert info["p"] == 0.3
    assert info["Ux"] == 0.4
    assert info["converged"] is False


def test_converged_only_when_the_solver_says_so():
    assert progress.parse_log(LOG + "\nSIMPLE solution converged in 2 iterations\n")["converged"] is True


def test_empty_or_pre_solve_log_says_nothing(tmp_path):
    assert progress.parse_log("") == {}
    assert progress.parse_log("Create time\nCreate mesh\n") == {}
    p = tmp_path / "12.log"
    p.write_text("Create time\n")
    assert progress.summarize(str(p)) == ""
    assert progress.summarize(str(tmp_path / "missing.log")) == ""


def test_summary_line_is_compact(tmp_path):
    p = tmp_path / "12.log"
    p.write_text(LOG)
    line = progress.summarize(str(p), "case_270", 8280.0)
    assert line == "case_270 iter 2 p=3.0e-01 Ux=4.0e-01 (2.3 h)"


def test_cli_prints_the_line_and_never_fails_on_a_missing_log(tmp_path, capsys):
    assert progress.main([str(tmp_path / "nope.log")]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert progress.main([str(tmp_path / "nope.log"), "--json"]) == 0
    assert capsys.readouterr().out.strip() == "{}"


# -- the solver's own trace (docs/e3d-contract.md) ----------------------------

def _trace(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


def test_the_trace_is_preferred_over_scraping_the_log(tmp_path):
    """E3D knows where its own outer-iteration boundaries are; parse_log has to
    infer them from interleaved corrector stages, and that inference is exactly
    what once read a monotone descent as a two-decade oscillation."""
    trace = _trace(tmp_path / "e3d-trace.jsonl", [
        {"iteration": 411, "residuals": {"p": 9.9e-05, "Ux": 9.9e-07}},
        {"iteration": 412, "residuals": {"p": 3.2e-05, "Ux": 8.1e-07}},
    ])
    log = tmp_path / "12.log"
    log.write_text("Time = 1\nSolving for p, Initial residual = 1\n")
    line = progress.summarize(str(log), "case_270", 8280, trace)
    assert "iter 412" in line and "p=3.2e-05" in line
    assert "iter 1" not in line              # the log was not consulted


def test_an_e3d_that_writes_no_trace_still_reports_progress(tmp_path):
    """The fallback is not deprecated: an older solver must keep working."""
    log = tmp_path / "12.log"
    log.write_text("Time = 1\nSolving for p, Initial residual = 3.2e-05\n")
    assert "iter 1" in progress.summarize(str(log), None, None, None)
    # A path that does not exist is the same as none.
    assert "iter 1" in progress.summarize(
        str(log), None, None, str(tmp_path / "absent.jsonl"))


def test_a_truncated_final_record_falls_back_rather_than_failing(tmp_path):
    """A crash mid-write leaves a partial line. That costs one heartbeat's
    detail and nothing else -- progress is decoration on a heartbeat."""
    path = tmp_path / "e3d-trace.jsonl"
    path.write_text('{"iteration": 411, "residuals": {"p": 9.9e-05}}\n{"iter')
    log = tmp_path / "12.log"
    log.write_text("Time = 1\nSolving for p, Initial residual = 1.0e-03\n")
    line = progress.summarize(str(log), None, None, str(path))
    assert "iter 1" in line and "1.0e-03" in line


def test_the_trace_is_read_from_the_end_not_scanned(tmp_path):
    """It grows for the whole solve and is read on a 60-second timer, so the
    cost of reporting progress must not grow with the progress reported."""
    path = _trace(tmp_path / "e3d-trace.jsonl", [
        {"iteration": i, "residuals": {"p": 1e-2 / i}} for i in range(1, 5001)])
    assert progress.parse_trace_line(progress.last_line(path))["iteration"] == 5000
    # last_line reads a bounded window, never the whole file.
    assert len(progress.last_line(path)) < 8192


def test_convergence_is_carried_through_from_the_trace(tmp_path):
    trace = _trace(tmp_path / "e3d-trace.jsonl",
                   [{"iteration": 900, "residuals": {"p": 1e-6}, "converged": True}])
    assert "CONVERGED" in progress.summarize(str(tmp_path / "nope.log"), None, None, trace)


def test_unknown_keys_and_missing_fields_are_tolerated(tmp_path):
    """The schema can grow without breaking an older reader; only `iteration`
    is required."""
    assert progress.parse_trace_line(
        '{"iteration": 7, "something_new": [1,2], "phase": "mesh"}'
    ) == {"iteration": 7, "converged": False}
    for junk in ("", "not json", "[]", "{}", '{"residuals": {"p": 1}}'):
        assert progress.parse_trace_line(junk) == {}
